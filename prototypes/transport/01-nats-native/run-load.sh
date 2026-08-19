#!/usr/bin/env bash
# Second prototype round for issue #4, NATS side: the leaf/hub JetStream mirror
# topology at the real horizon.
#
#   ./run-load.sh                       # 518 400 messages = 30 days at 12 tables
#   TARGET_MSGS=259200 ./run-load.sh    # 15 days, the actual requirement
#
# Narrower than the Mosquitto load round on purpose. The criterion agreed on
# issue #4 makes Mosquitto the default winner unless it fails a threshold, so
# what this run has to establish is where NATS could plausibly beat it:
#
#   S1  what a real backlog costs the leaf in RSS and in disk. JetStream keeps
#       it on the SSD; Mosquitto keeps it in RAM. That is the asymmetry.
#   S2  whether the ~26 s mirror resume round one measured against a
#       1 000-message backlog is a constant or scales with the backlog.
#   S3  how long the leaf takes to come back with a large file store.
#
# S4 (the command return path) is not built here: it needs a second stream
# mirrored the other way, which is exactly the topology cost under evaluation.
# S5 (real power cut) needs hardware. Both are reported as not measured.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# The compose file interpolates these with `:?`, so they must be set for it to
# parse at all. PROFILE is unused by the load clients (they do not call
# common.profile()); the config files are what actually select the topology.
export PROFILE=jetstream-mirror
export HUB_CONF=hub-load.conf
export LEAF_CONF=leaf-load.conf
export LEAF_MAX_MSGS=-1
export COMPOSE_PROFILES=load

export TARGET_MSGS="${TARGET_MSGS:-518400}"
export N_DEVICES="${N_DEVICES:-12}"
export DRAIN_RATE_HZ="${DRAIN_RATE_HZ:-20}"
export CAPACITY="${CAPACITY:-800000}"
export MSGS_15D="${MSGS_15D:-259200}"
export MSGS_30D="${MSGS_30D:-518400}"

PROJECT=fl-proto-nats
LEAF=fl-proto-nats-leaf
HUB=fl-proto-nats-hub
LINK_NET="${PROJECT}_link"

WARM_SECS="${WARM_SECS:-12}"
KILL_DOWN_SECS="${KILL_DOWN_SECS:-5}"
SAMPLE_EVERY="${SAMPLE_EVERY:-5}"
FILL_MAX_SECS="${FILL_MAX_SECS:-1800}"
DRAIN_MAX_SECS="${DRAIN_MAX_SECS:-1800}"
DRAIN_GAP="${DRAIN_GAP:-60}"
DRAIN_STALL_SECS="${DRAIN_STALL_SECS:-120}"

CKPT="$(mktemp)"
TIMINGS="$(mktemp)"
log() { printf '\n[%(%H:%M:%S)T] === %s\n' -1 "$*"; }
step() { printf '[%(%H:%M:%S)T]     %s\n' -1 "$*"; }

cleanup() {
  local rc=$?
  log "limpieza (docker compose down -v)"
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$LINK_NET" >/dev/null 2>&1 || true
  rm -f "$CKPT" "$TIMINGS"
  exit $rc
}
trap cleanup EXIT

leaf_rss_kb() {
  local pid
  pid="$(docker inspect -f '{{.State.Pid}}' "$LEAF" 2>/dev/null || echo 0)"
  [[ "$pid" == 0 ]] && { echo 0; return; }
  awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0
}

leaf_store_bytes() {
  docker exec "$LEAF" sh -c 'du -sb /data 2>/dev/null | cut -f1' 2>/dev/null \
    | tr -dc '0-9' || echo 0
}

state_field() {  # state_field <file> <dotted.key>
  docker compose exec -T load-consumer sh -c "cat /results/$1 2>/dev/null || echo {}" \
    2>/dev/null | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
    for k in '$2'.split('.'):
        d = d.get(k) if isinstance(d, dict) else None
    print('' if d is None else d)
except Exception:
    print('')" 2>/dev/null || echo ""
}

LAST_ACKED=0
checkpoint() {  # checkpoint <label>
  local label="$1" rss store msgs
  rss="$(leaf_rss_kb)"; store="$(leaf_store_bytes)"
  msgs="$(state_field sim-state.json stream.messages)"
  printf '{"label":"%s","t":%s,"rss_kb":%s,"store_bytes":%s,"acked":%s,"stream_msgs":%s}\n' \
    "$label" "$(date +%s)" "${rss:-0}" "${store:-0}" "${LAST_ACKED:-0}" "${msgs:-null}" >> "$CKPT"
  step "checkpoint $label: RSS $((${rss:-0} / 1024)) MB, store $((${store:-0} / 1000000)) MB, acked ${LAST_ACKED}${msgs:+, en stream $msgs}"
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

# /healthz on the monitoring port, polled directly: finer than the 2 s
# healthcheck interval, which matters when timing a restart.
wait_ready() {
  local name="$1" attempts="${2:-400}" i
  for ((i = 0; i < attempts; i++)); do
    if docker exec "$name" sh -c 'wget -q -O- http://127.0.0.1:8222/healthz' >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

log "ronda de carga NATS: $TARGET_MSGS mensajes objetivo, $N_DEVICES mesas de drenaje"
step "= $((TARGET_MSGS / (N_DEVICES * 1440))) dias de corte a 1 muestra/60 s por mesa"

log "estado limpio previo"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true

log "construyendo la imagen de cliente"
docker compose build --quiet load-simulator

log "levantando el hub primero (2 vCPU / 4 GB, como el VPS real)"
docker compose up -d nats-hub >/dev/null
docker update --cpus 2 --memory 4g --memory-swap 4g "$HUB" >/dev/null 2>&1 \
  || step "AVISO: no se pudieron aplicar limites al hub"
wait_healthy "$HUB"
step "hub healthy"

log "levantando el leaf node"
docker compose up -d nats-leaf >/dev/null
wait_healthy "$LEAF"
step "leaf healthy"

log "levantando consumidor (crea el espejo) y simulador (crea el stream de borde)"
docker compose up -d load-consumer >/dev/null
sleep 3
set_phase WARM
docker compose up -d load-simulator >/dev/null

log "WARM (${WARM_SECS}s) -- confirmando que el camino completo entrega en vivo"
live=0
for ((i = 0; i < 90; i++)); do
  n="$(state_field recv-state.json unique)"
  [[ -n "$n" && "$n" -gt 0 ]] && { live=1; break; }
  sleep 1
done
[[ "$live" == 1 ]] || { echo "FATAL: el espejo no entrego nada con el enlace arriba" >&2; exit 1; }
c1="$(state_field recv-state.json unique)"; sleep 3; c2="$(state_field recv-state.json unique)"
[[ "$c2" -gt "$c1" ]] || { echo "FATAL: el espejo no entrega en vivo ($c1 -> $c2)" >&2; exit 1; }
step "entrega en vivo confirmada ($c1 -> $c2 en el hub)"
sleep "$WARM_SECS"
LAST_ACKED="$(state_field sim-state.json acked)"; [[ -z "$LAST_ACKED" ]] && LAST_ACKED=0
checkpoint baseline

log "S1 -- corte del enlace y llenado del stream de borde hasta $TARGET_MSGS"
docker network disconnect "$LINK_NET" "$LEAF"
step "enlace cortado (docker network disconnect $LINK_NET $LEAF)"
set_phase FILL
t_fill_start="$(date +%s)"
for ((t = 0; t < FILL_MAX_SECS; t += SAMPLE_EVERY)); do
  sleep "$SAMPLE_EVERY"
  acked="$(state_field sim-state.json acked)"; [[ -z "$acked" ]] && acked=0
  LAST_ACKED="$acked"
  filled="$(state_field sim-state.json filled)"
  checkpoint "fill-$acked"
  [[ "$filled" == "True" || "$filled" == "true" ]] && break
done
t_fill="$(( $(date +%s) - t_fill_start ))"
step "llenado terminado en ${t_fill}s"
checkpoint fill-final

log "S3 -- SIGKILL al leaf con el stream lleno"
set_phase HOLD
sleep 2
store_at_kill="$(leaf_store_bytes)"
rss_at_kill="$(leaf_rss_kb)"
step "en el momento del kill: RSS $((rss_at_kill / 1024)) MB, store $((store_at_kill / 1000000)) MB"
docker kill --signal=SIGKILL "$LEAF" >/dev/null
sleep "$KILL_DOWN_SECS"
t_restart_start="$(date +%s.%N)"
docker compose start nats-leaf >/dev/null
if wait_ready "$LEAF" 800; then
  t_restart="$(awk -v a="$(date +%s.%N)" -v b="$t_restart_start" 'BEGIN{printf "%.3f", a - b}')"
  step "leaf listo de nuevo tras ${t_restart}s (con ${store_at_kill} bytes de file store)"
else
  t_restart=-1
  step "FALLO: el leaf no volvio a estar listo"
fi
if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$LEAF" \
   | grep -q "$LINK_NET"; then
  step "AVISO: el reinicio reconecto $LINK_NET; el corte no se mantuvo"
fi
sleep 5
checkpoint post-restart

log "S2 -- enlace restaurado; midiendo la reanudacion del espejo"
n_before="$(state_field recv-state.json unique)"; [[ -z "$n_before" ]] && n_before=0
set_phase DRAIN
docker network connect "$LINK_NET" "$LEAF"
t_drain_start="$(date +%s.%N)"
t_first=""; cur=0; caught=0; stalled=0; min_gap=999999999; t_caught=""
for ((t = 0; t < DRAIN_MAX_SECS; t++)); do
  cur="$(state_field recv-state.json unique)"; [[ -z "$cur" ]] && cur=0
  acked_now="$(state_field sim-state.json acked)"; [[ -z "$acked_now" ]] && acked_now=0
  LAST_ACKED="$acked_now"
  if [[ -z "$t_first" && "$cur" -gt "$n_before" ]]; then
    t_first="$(date +%s.%N)"
    step "primera entrega nueva tras $(awk -v a="$t_first" -v b="$t_drain_start" 'BEGIN{printf "%.1f", a-b}')s"
  fi
  gap=$((acked_now - cur))
  if [[ "$gap" -le "$DRAIN_GAP" ]]; then
    caught=$((caught + 1))
    [[ "$caught" == 1 ]] && t_caught="$(date +%s.%N)"
    [[ "$caught" -ge 5 ]] && break
  else
    caught=0
  fi
  if [[ "$gap" -lt "$min_gap" ]]; then
    min_gap="$gap"; stalled=0
  else
    stalled=$((stalled + 1))
    if [[ "$stalled" -ge "$DRAIN_STALL_SECS" ]]; then
      step "AVISO: el hueco lleva ${stalled}s sin reducirse (gap $gap); se da por estancado"
      break
    fi
  fi
  (( t % 10 == 0 )) && step "recuperando... $cur unicos en el hub (faltan $gap)"
  sleep 1
done
[[ -z "$t_caught" ]] && t_caught="$(date +%s.%N)"
[[ -z "$t_first" ]] && t_first="$t_caught"
t_catchup="$(awk -v a="$t_caught" -v b="$t_drain_start" 'BEGIN{printf "%.3f", a - b}')"
t_first_rel="$(awk -v a="$t_first" -v b="$t_drain_start" 'BEGIN{printf "%.3f", a - b}')"
step "hub al dia en $cur unicos tras ${t_catchup}s desde la reconexion"
checkpoint post-catchup

log "parando y volcando la contabilidad"
set_phase stop
sleep 8
docker stop -t 30 fl-proto-nats-load-consumer >/dev/null 2>&1 || true
sleep 2

printf '{"fill_seconds":%s,"restart_seconds":%s,"catchup_seconds":%s,"first_delivery_seconds":%s,"drain_backlog":%s,"store_at_kill_bytes":%s,"rss_at_kill_kb":%s}\n' \
  "$t_fill" "$t_restart" "$t_catchup" "$t_first_rel" "$((cur - n_before))" \
  "$store_at_kill" "$rss_at_kill" > "$TIMINGS"

docker compose run --rm --no-deps -T -v "$CKPT:/in/checkpoints.jsonl:ro" -v "$TIMINGS:/in/timings.json:ro" \
  --entrypoint sh load-verifier -c 'cp /in/checkpoints.jsonl /in/timings.json /results/'

echo
echo "--- log del leaf: jetstream / espejo / errores ---"
docker compose logs --no-log-prefix nats-leaf 2>/dev/null \
  | grep -iE 'jetstream|mirror|leafnode|error|warn|slow|restor|recover' | tail -40 || true
echo "--- (fin del extracto) ---"
echo
echo "--- log del hub: espejo ---"
docker compose logs --no-log-prefix nats-hub 2>/dev/null \
  | grep -iE 'mirror|leafnode|error|warn|slow' | tail -25 || true
echo "--- (fin del extracto) ---"

log "verificacion"
docker compose run --rm --no-deps load-verifier
