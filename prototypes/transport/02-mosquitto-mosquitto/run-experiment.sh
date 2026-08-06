#!/usr/bin/env bash
# One command, five phases, one verdict. See ../README.md for the protocol.
#
#   ./run-experiment.sh defaults         minimal bridge config
#   ./run-experiment.sh hardened         the correction proposed by issue #4
#   ./run-experiment.sh queue-probe      hardened, but max_queued_messages left
#                                        at its 1000 default
#   ./run-experiment.sh queue-probe-50   same with the cap set to 50, as the
#                                        control that proves the cap governs
#                                        the bridge queue
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PROFILE="${1:-}"
if [[ -z "$PROFILE" || ! -f "config/$PROFILE/edge.conf" ]]; then
  echo "usage: $0 <profile>" >&2
  echo "available: $(ls config | tr '\n' ' ')" >&2
  exit 64
fi
export FL_PROFILE="$PROFILE"

PROJECT=fl-proto-mosq
EDGE=fl-proto-mosq-edge-broker
LINK_NET="${PROJECT}_link"

# Phase durations, in seconds. P1 at 20 msg/s = 1200 messages, which crosses
# Mosquitto's 1000-message default cap on purpose.
P0_SECS=${P0_SECS:-10}
P1_SECS=${P1_SECS:-60}
P2_DOWN_SECS=${P2_DOWN_SECS:-6}     # how long the edge broker stays killed
P2_SECS=${P2_SECS:-20}
P3_PUBLISH_SECS=${P3_PUBLISH_SECS:-20}
MIN_DRAIN_SECS=${MIN_DRAIN_SECS:-45}
P3_DRAIN_MAX_SECS=${P3_DRAIN_MAX_SECS:-70}

log() { printf '\n[%(%H:%M:%S)T] === %s\n' -1 "$*"; }
step() { printf '[%(%H:%M:%S)T]     %s\n' -1 "$*"; }

LOG_DUMP=""
cleanup() {
  local rc=$?
  log "limpieza (docker compose down -v)"
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  [[ -n "$LOG_DUMP" ]] && rm -f "$LOG_DUMP" || true
  exit $rc
}
trap cleanup EXIT

set_phase() {
  # Atomic rename so the simulator never reads a half-written phase file.
  docker compose exec -T simulator sh -c \
    "printf '%s' '$1' > /results/.phase.tmp && mv /results/.phase.tmp /results/phase"
}

wait_healthy() {
  local name="$1" timeout="${2:-60}" i st
  for ((i = 0; i < timeout * 2; i++)); do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo none)"
    [[ "$st" == healthy ]] && return 0
    sleep 0.5
  done
  echo "timeout esperando a que $name este healthy" >&2
  return 1
}

received_count() {
  docker compose exec -T consumer sh -c 'wc -l < /results/received.jsonl 2>/dev/null || echo 0' \
    | tr -dc '0-9'
}

log "perfil: $PROFILE   (proyecto docker: $PROJECT, sin puertos al host)"
step "config de borde: config/$PROFILE/edge.conf"
step "config de hub:   config/$PROFILE/hub.conf"

log "estado limpio previo"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true

log "construyendo la imagen de cliente"
docker compose build --quiet simulator

# The hub goes up FIRST and is waited on. If the edge broker starts while the
# hub is still booting, the bridge's first connect fails and Mosquitto backs
# off for up to ~30 s (restart_timeout jitter) -- which was silently eating the
# first seconds of P0 and polluting the numbers.
log "levantando el hub primero (para que el bridge conecte al primer intento)"
docker compose up -d hub-broker >/dev/null
wait_healthy fl-proto-mosq-hub-broker
step "hub healthy"

log "levantando el broker de borde"
docker compose up -d edge-broker >/dev/null
wait_healthy "$EDGE"
step "borde healthy"

log "levantando simulador y consumidor"
docker compose up -d simulator consumer >/dev/null
sleep 2

# ------------------------------------------------ PW: esperar al bridge vivo
log "PW -- esperando a que el bridge entregue en vivo antes de empezar a medir"
set_phase PW
bridge_live=0
for ((i = 0; i < 60; i++)); do
  if [[ "$(received_count)" -gt 0 ]]; then bridge_live=1; break; fi
  sleep 1
done
if [[ "$bridge_live" != 1 ]]; then
  echo "FATAL: el bridge no entrego nada en 60s con el enlace arriba" >&2
  docker compose logs --no-log-prefix edge-broker | tail -40 >&2
  exit 1
fi
# Not enough that something arrived: it could be a queue flush. Require the
# count to keep growing, i.e. live delivery.
c1="$(received_count)"; sleep 2; c2="$(received_count)"
if [[ "$c2" -le "$c1" ]]; then
  echo "FATAL: el bridge entrego una vez pero no esta entregando en vivo" >&2
  exit 1
fi
step "bridge entregando en vivo ($c1 -> $c2 recibidos)"

# ---------------------------------------------------------------- P0 warmup
log "P0 warmup (${P0_SECS}s) -- enlace arriba, comprobando el camino completo"
set_phase P0
n_before_p0="$(received_count)"
sleep "$P0_SECS"
n0="$(received_count)"
step "recibidos en el hub durante P0: $((n0 - n_before_p0))"

# ----------------------------------------------------------------- P1 corte
log "P1 corte (${P1_SECS}s) -- docker network disconnect $LINK_NET $EDGE"
set_phase P1
docker network disconnect "$LINK_NET" "$EDGE"
step "enlace cortado; el simulador sigue publicando contra el borde"
sleep "$P1_SECS"
n1="$(received_count)"
step "recibidos acumulados tras P1: $n1 (no deberia haber crecido)"

# ------------------------------------------------------- P2 corte de corriente
if [[ "${SKIP_P2_KILL:-0}" == "1" ]]; then
  # Control run: same timeline, no SIGKILL. Used to read the queue-overflow
  # boundary with nothing else able to remove messages from the queue.
  log "P2 (${P2_SECS}s) -- SKIP_P2_KILL=1: NO se mata el broker (pasada de control)"
  set_phase P2
  sleep "$P2_SECS"
else
  log "P2 corte de corriente (${P2_SECS}s) -- SIGKILL al broker de borde"
  set_phase P2
  docker kill --signal=SIGKILL "$EDGE" >/dev/null
  step "SIGKILL enviado (no SIGTERM: un apagado limpio escribiria la persistencia)"
  sleep "$P2_DOWN_SECS"
  step "arrancando de nuevo el broker de borde, con el enlace todavia caido"
  docker compose start edge-broker >/dev/null
  wait_healthy "$EDGE" 30
  step "borde healthy de nuevo"
  if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$EDGE" \
     | grep -q "$LINK_NET"; then
    echo "AVISO: el reinicio reconecto la red $LINK_NET; el corte no se mantuvo" >&2
  fi
  sleep $((P2_SECS - P2_DOWN_SECS))
fi

# ----------------------------------------------------------- P3 restauracion
log "P3 restauracion -- docker network connect $LINK_NET $EDGE"
set_phase P3
docker network connect "$LINK_NET" "$EDGE"
step "enlace restaurado; el simulador publica ${P3_PUBLISH_SECS}s mas"
sleep "$P3_PUBLISH_SECS"
set_phase stop
# Mosquitto's default restart_timeout is decorrelated jitter with base 5 s and
# cap 30 s, so after `network connect` the bridge can legitimately sit idle for
# up to ~30 s before it even tries. A drain loop that stops as soon as the
# count is stable would call that "drained" and report a fake total, so the
# loop refuses to conclude before MIN_DRAIN_SECS.
step "simulador detenido; drenando (minimo ${MIN_DRAIN_SECS}s, maximo ${P3_DRAIN_MAX_SECS}s)"
prev=-1
stable=0
for ((t = 0; t < P3_DRAIN_MAX_SECS; t++)); do
  cur="$(received_count)"
  if [[ "$cur" == "$prev" ]]; then
    stable=$((stable + 1))
  else
    stable=0
    step "drenando... $cur recibidos"
  fi
  prev="$cur"
  if [[ "$stable" -ge 8 && "$t" -ge "$MIN_DRAIN_SECS" ]]; then break; fi
  sleep 1
done
step "drenaje estable en $prev recibidos tras ${t}s de espera"

# ------------------------------------------------------------ P4 verificacion
log "P4 verificacion"

LOG_DUMP="$(mktemp)"
docker compose logs --no-log-prefix edge-broker 2>/dev/null \
  | grep -v healthcheck > "$LOG_DUMP" || true

echo
echo "--- log del broker de borde: descartes / cola / persistencia / errores ---"
grep -iE 'drop|queue|full|saving in-memory|persisten|error|warn' "$LOG_DUMP" \
  | head -30 || echo "  (ninguna linea coincide)"
echo "--- (fin del extracto) ---"
echo
echo "--- log del broker de borde: ciclo de vida del bridge ---"
grep -iE 'bridge|mosquitto version|opening|Connecting|Socket error|EOF' "$LOG_DUMP" \
  | head -30 || echo "  (ninguna linea coincide)"
echo "--- (fin del extracto) ---"
echo

docker compose run --rm --no-deps verifier
