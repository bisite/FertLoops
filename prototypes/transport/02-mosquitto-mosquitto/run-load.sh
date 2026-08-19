#!/usr/bin/env bash
# Second prototype round for issue #4: the recommended Mosquitto configuration
# at the real horizon. One command, five phases, one verdict.
#
#   ./run-load.sh                       # 518 400 messages = 30 days at 12 tables
#   TARGET_MSGS=259200 ./run-load.sh    # 15 days, the actual requirement
#
# Scenarios, as agreed on issue #4:
#   S1  RSS per queued message, sampled while the queue is built up.
#   S2  loss while a full backlog drains, with live traffic competing.
#   S3  restart of the broker with a large persistence file.
#   S4  the control command path, hub -> edge, across the outage.
#
# S5 (real power cut) is deliberately absent: `docker kill` does not flush the
# host page cache, so it cannot answer that question and pretending otherwise
# would be the worst possible outcome. It needs a Raspberry Pi.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

export FL_PROFILE=load
# The load services sit behind a compose profile so `up -d simulator consumer`
# still means the round-one outage experiment and nothing else.
export COMPOSE_PROFILES=load
export TARGET_MSGS="${TARGET_MSGS:-518400}"
export N_DEVICES="${N_DEVICES:-12}"
export DRAIN_RATE_HZ="${DRAIN_RATE_HZ:-20}"
export CMD_COUNT="${CMD_COUNT:-60}"
export CAPACITY="${CAPACITY:-800000}"
export MSGS_15D="${MSGS_15D:-259200}"
export MSGS_30D="${MSGS_30D:-518400}"

PROJECT=fl-proto-mosq
EDGE=fl-proto-mosq-edge-broker
HUB=fl-proto-mosq-hub-broker
LINK_NET="${PROJECT}_link"

WARM_SECS="${WARM_SECS:-12}"
KILL_DOWN_SECS="${KILL_DOWN_SECS:-5}"
SAMPLE_EVERY="${SAMPLE_EVERY:-5}"
FILL_MAX_SECS="${FILL_MAX_SECS:-1800}"
DRAIN_MAX_SECS="${DRAIN_MAX_SECS:-1800}"
# The hub counts as caught up when it is within this many messages of what the
# edge broker PUBACKed. At 20 msg/s of live traffic a gap of 60 is ~3 s of lag.
DRAIN_GAP="${DRAIN_GAP:-60}"
DRAIN_STALL_SECS="${DRAIN_STALL_SECS:-90}"

CKPT="$(mktemp)"
TIMINGS="$(mktemp)"
log() { printf '\n[%(%H:%M:%S)T] === %s\n' -1 "$*"; }
step() { printf '[%(%H:%M:%S)T]     %s\n' -1 "$*"; }

cleanup() {
  local rc=$?
  log "limpieza (docker compose down -v)"
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$CKPT" "$TIMINGS"
  exit $rc
}
trap cleanup EXIT

# --- instrumentation -------------------------------------------------------
# RSS comes from the host's /proc, not from `docker stats`: the cgroup figure
# includes page cache, and with autosave rewriting a 180 MB file every 5 s the
# page cache would swamp the number we actually want.
edge_rss_kb() {
  local pid
  pid="$(docker inspect -f '{{.State.Pid}}' "$EDGE" 2>/dev/null || echo 0)"
  [[ "$pid" == 0 ]] && { echo 0; return; }
  awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0
}

edge_db_bytes() {
  docker exec "$EDGE" sh -c \
    'stat -c %s /mosquitto/data/mosquitto.db 2>/dev/null || echo 0' 2>/dev/null \
    | tr -dc '0-9' || echo 0
}

# The broker's own count of what it holds, independent of what the publisher
# believes. Only sampled at milestones: it costs a round trip through $SYS.
#
# The topic MUST reach mosquitto_sub with a literal `$SYS`. Outer double quotes
# with \$ so this bash does not expand it, inner single quotes so the shell
# inside the container does not either -- getting that wrong silently subscribes
# to `/broker/...`, which never receives anything and just burns the timeout.
edge_store_count() {
  docker exec "$EDGE" sh -c \
    "mosquitto_sub -t '\$SYS/broker/store/messages/count' -C 1 -W 6 -i fl-probe 2>/dev/null" \
    2>/dev/null | tr -dc '0-9' || echo ""
}

sim_state() {  # sim_state <key>
  docker compose exec -T load-consumer sh -c 'cat /results/sim-state.json 2>/dev/null || echo {}' \
    2>/dev/null | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null || echo ""
}

recv_state() {  # recv_state <key>
  docker compose exec -T load-consumer sh -c 'cat /results/recv-state.json 2>/dev/null || echo {}' \
    2>/dev/null | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null || echo ""
}

# LAST_ACKED is the publisher's running PUBACK count. It is the x-axis of the
# RSS slope: every checkpoint gets one, whereas the broker's own store count
# costs a $SYS round trip and is only sampled at milestones as corroboration.
LAST_ACKED=0
checkpoint() {  # checkpoint <label> [store]
  local label="$1" want_store="${2:-no}" rss db store=""
  rss="$(edge_rss_kb)"; db="$(edge_db_bytes)"
  [[ "$want_store" == store ]] && store="$(edge_store_count)"
  printf '{"label":"%s","t":%s,"rss_kb":%s,"db_bytes":%s,"acked":%s,"store_count":%s}\n' \
    "$label" "$(date +%s)" "${rss:-0}" "${db:-0}" "${LAST_ACKED:-0}" "${store:-null}" >> "$CKPT"
  step "checkpoint $label: RSS $((${rss:-0} / 1024)) MB, db $((${db:-0} / 1000000)) MB, acked ${LAST_ACKED}${store:+, en store $store}"
}

set_phase() {
  docker compose exec -T load-consumer sh -c \
    "printf '%s' '$1' > /results/.phase.tmp && mv /results/.phase.tmp /results/phase"
  step "fase -> $1"
}

wait_healthy() {
  local name="$1" timeout="${2:-120}" i st
  for ((i = 0; i < timeout * 2; i++)); do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo none)"
    [[ "$st" == healthy ]] && return 0
    sleep 0.5
  done
  echo "timeout esperando a que $name este healthy" >&2
  return 1
}

# Finer grained than the healthcheck (2 s interval): polls the listener itself,
# which is what "the broker is back" actually means when it has a large
# persistence file to reload first. Each probe costs up to 2 s of its own, so
# `attempts` is a count of probes, not of seconds.
wait_listening() {
  local name="$1" attempts="${2:-200}" i
  for ((i = 0; i < attempts; i++)); do
    if docker exec "$name" sh -c \
         "mosquitto_sub -t '\$SYS/broker/uptime' -C 1 -W 2 -i fl-ready >/dev/null 2>&1"; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

# --- run -------------------------------------------------------------------
log "ronda de carga: $TARGET_MSGS mensajes objetivo, $N_DEVICES mesas de drenaje"
step "= $((TARGET_MSGS / (N_DEVICES * 1440))) dias de corte a 1 muestra/60 s por mesa"

log "estado limpio previo"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true

log "construyendo la imagen de cliente"
# `simulator` and the load services share one image and one build context;
# building the unprofiled service keeps this independent of profile handling.
docker compose build --quiet simulator

log "levantando el hub primero (2 vCPU / 4 GB, como el VPS real)"
docker compose up -d hub-broker >/dev/null
docker update --cpus 2 --memory 4g --memory-swap 4g "$HUB" >/dev/null 2>&1 \
  || step "AVISO: no se pudieron aplicar limites al hub"
wait_healthy "$HUB"
step "hub healthy"

log "levantando el broker de borde"
docker compose up -d edge-broker >/dev/null
wait_healthy "$EDGE"
step "borde healthy"

log "levantando consumidores"
docker compose up -d load-consumer cmd-consumer >/dev/null
sleep 3
set_phase WARM
docker compose up -d load-simulator >/dev/null

# --- WARM: confirmar entrega en vivo antes de romper nada
log "WARM (${WARM_SECS}s) -- confirmando que el camino completo entrega en vivo"
live=0
for ((i = 0; i < 60; i++)); do
  n="$(recv_state unique)"
  [[ -n "$n" && "$n" -gt 0 ]] && { live=1; break; }
  sleep 1
done
[[ "$live" == 1 ]] || { echo "FATAL: el bridge no entrego nada con el enlace arriba" >&2; exit 1; }
c1="$(recv_state unique)"; sleep 3; c2="$(recv_state unique)"
[[ "$c2" -gt "$c1" ]] || { echo "FATAL: el bridge no entrega en vivo ($c1 -> $c2)" >&2; exit 1; }
step "entrega en vivo confirmada ($c1 -> $c2 en el hub)"
sleep "$WARM_SECS"
# Seed LAST_ACKED before the baseline sample: WARM traffic has already been
# PUBACKed and delivered live, so counting it as queue depth would bias the
# slope's x-axis by a couple of hundred messages.
LAST_ACKED="$(sim_state acked)"; [[ -z "$LAST_ACKED" ]] && LAST_ACKED=0
checkpoint baseline store

# --- S1: corte y llenado hasta el objetivo
log "S1 -- corte del enlace y llenado de la cola hasta $TARGET_MSGS"
docker network disconnect "$LINK_NET" "$EDGE"
step "enlace cortado (docker network disconnect $LINK_NET $EDGE)"
set_phase FILL
t_fill_start="$(date +%s)"
milestone=0
for ((t = 0; t < FILL_MAX_SECS; t += SAMPLE_EVERY)); do
  sleep "$SAMPLE_EVERY"
  acked="$(sim_state acked)"; filled="$(sim_state filled)"
  [[ -z "$acked" ]] && acked=0
  LAST_ACKED="$acked"
  # Milestone checkpoints (with the broker's own store count) at each 15-day
  # multiple, so the slope is anchored on the two horizons that matter.
  if [[ "$acked" -ge "$MSGS_15D" && "$milestone" -lt 1 ]]; then
    milestone=1; checkpoint fill-15d store
  else
    checkpoint "fill-$acked"
  fi
  [[ "$filled" == "True" || "$filled" == "true" ]] && break
done
t_fill="$(( $(date +%s) - t_fill_start ))"
step "llenado terminado en ${t_fill}s"
checkpoint fill-final store

# --- S4 (emision): comandos publicados mientras la Pi es inalcanzable
log "S4 -- publicando $CMD_COUNT comandos en el hub con la Pi desconectada"
docker compose run --rm --no-deps commander || step "AVISO: el commander fallo"

# --- S3: SIGKILL con la cola llena
log "S3 -- SIGKILL al broker de borde con la cola llena"
set_phase HOLD
sleep 2
db_at_kill="$(edge_db_bytes)"
rss_at_kill="$(edge_rss_kb)"
step "en el momento del kill: RSS $((rss_at_kill / 1024)) MB, db $((db_at_kill / 1000000)) MB"
docker kill --signal=SIGKILL "$EDGE" >/dev/null
step "SIGKILL enviado (no SIGTERM: un apagado limpio escribiria la persistencia)"
sleep "$KILL_DOWN_SECS"
t_restart_start="$(date +%s.%N)"
docker compose start edge-broker >/dev/null
if wait_listening "$EDGE" 400; then
  # awk, not bc: bc prints ".287" for sub-second values and a leading dot is
  # not valid JSON, which silently broke the verifier's timings.json.
  t_restart="$(awk -v a="$(date +%s.%N)" -v b="$t_restart_start" 'BEGIN{printf "%.3f", a - b}')"
  step "broker escuchando de nuevo tras ${t_restart}s (cargando ${db_at_kill} bytes de persistencia)"
else
  t_restart=-1
  step "FALLO: el broker no volvio a escuchar en 600s"
fi
if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$EDGE" \
   | grep -q "$LINK_NET"; then
  step "AVISO: el reinicio reconecto $LINK_NET; el corte no se mantuvo"
fi
sleep 3
checkpoint post-restart store

# --- S2: restauracion y drenaje, con trafico en vivo compitiendo
log "S2 -- enlace restaurado; drenando con trafico en vivo a ${DRAIN_RATE_HZ} msg/s"
n_before_drain="$(recv_state unique)"; [[ -z "$n_before_drain" ]] && n_before_drain=0
set_phase DRAIN
docker network connect "$LINK_NET" "$EDGE"
t_drain_start="$(date +%s.%N)"
# "Drained" cannot be "no new arrivals": the simulator is still publishing live
# traffic at DRAIN_RATE_HZ, exactly as a real Pi would, so arrivals never stop.
# The backlog is gone when the hub has caught up with what the edge broker
# PUBACKed -- i.e. when the gap between the two closes.
cur=0; caught=0; stalled=0; min_gap=999999999; t_caught=""
for ((t = 0; t < DRAIN_MAX_SECS; t++)); do
  cur="$(recv_state unique)"; [[ -z "$cur" ]] && cur=0
  acked_now="$(sim_state acked)"; [[ -z "$acked_now" ]] && acked_now=0
  LAST_ACKED="$acked_now"
  gap=$((acked_now - cur))
  if [[ "$gap" -le "$DRAIN_GAP" ]]; then
    caught=$((caught + 1))
    [[ "$caught" == 1 ]] && t_caught="$(date +%s.%N)"
    [[ "$caught" -ge 5 ]] && break
  else
    caught=0
  fi
  # A gap that stops shrinking means something is lost, not slow. Break rather
  # than sit out DRAIN_MAX_SECS, and let the verifier say what went missing.
  if [[ "$gap" -lt "$min_gap" ]]; then
    min_gap="$gap"; stalled=0
  else
    stalled=$((stalled + 1))
    if [[ "$stalled" -ge "$DRAIN_STALL_SECS" ]]; then
      step "AVISO: el hueco lleva ${stalled}s sin reducirse (gap $gap); se da por estancado"
      break
    fi
  fi
  (( t % 10 == 0 )) && step "drenando... $cur unicos en el hub (faltan $gap)"
  sleep 1
done
[[ -z "$t_caught" ]] && t_caught="$(date +%s.%N)"
t_drain="$(awk -v a="$t_caught" -v b="$t_drain_start" 'BEGIN{printf "%.3f", a - b}')"
n_after_drain="$cur"
step "hub al dia en $n_after_drain unicos tras ${t_drain}s desde la reconexion"
checkpoint post-drain store

# --- parada y verificacion
log "parando el simulador y volcando la contabilidad"
set_phase stop
sleep 6
docker stop -t 25 fl-proto-mosq-load-consumer fl-proto-mosq-cmd-consumer >/dev/null 2>&1 || true
sleep 2

printf '{"fill_seconds":%s,"restart_seconds":%s,"drain_seconds":%s,"drain_backlog":%s,"db_at_kill_bytes":%s,"rss_at_kill_kb":%s}\n' \
  "$t_fill" "$t_restart" "$t_drain" "$((n_after_drain - n_before_drain))" \
  "$db_at_kill" "$rss_at_kill" > "$TIMINGS"

# The results volume is only reachable from a container, so ship the two
# host-side files in before the verifier runs.
docker compose run --rm --no-deps -T -v "$CKPT:/in/checkpoints.jsonl:ro" -v "$TIMINGS:/in/timings.json:ro" \
  --entrypoint sh load-verifier -c 'cp /in/checkpoints.jsonl /in/timings.json /results/'

echo
echo "--- log del broker de borde: cola / persistencia / errores ---"
docker compose logs --no-log-prefix edge-broker 2>/dev/null | grep -v fl-ready \
  | grep -iE 'drop|queue|full|saving in-memory|persisten|error|warn|version|restor' | tail -40 \
  || echo "  (ninguna linea coincide)"
echo "--- (fin del extracto) ---"
echo
echo "--- log del broker de borde: ciclo de vida del bridge ---"
docker compose logs --no-log-prefix edge-broker 2>/dev/null \
  | grep -iE 'bridge|Connecting|Socket error|EOF' | tail -20 || true
echo "--- (fin del extracto) ---"

log "verificacion"
docker compose run --rm --no-deps load-verifier
