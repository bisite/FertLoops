#!/usr/bin/env bash
# One command, five phases, one verdict. Prototype 03: NanoMQ edge -> Mosquitto hub.
#
#   ./run-experiment.sh defaults    # emqx/nanomq:latest, nothing tuned
#   ./run-experiment.sh hardened    # emqx/nanomq:0.25.5-slim, SQLite cache on
#
# Runs entirely in containers, publishes no host port, and cleans up after itself.
set -euo pipefail

cd "$(dirname "$0")"

PROFILE="${1:-}"
case "$PROFILE" in
  defaults)
    NANOMQ_IMAGE="emqx/nanomq:latest"
    ;;
  hardened)
    # Only the -slim and -full variants are built with SQLite. Pinned to an exact
    # version tag rather than latest-slim so the finding stays reproducible.
    NANOMQ_IMAGE="emqx/nanomq:0.25.5-slim"
    ;;
  *)
    echo "usage: $0 <defaults|hardened>" >&2
    exit 2
    ;;
esac
export NANOMQ_IMAGE PROFILE

PROJECT="fl-proto-nanomq"
EDGE="${PROJECT}-edge"
HUB="${PROJECT}-hub"
CONSUMER="${PROJECT}-consumer"
SIMULATOR="${PROJECT}-simulator"
LINK_NET="${PROJECT}_link"
EDGE_NET="${PROJECT}_edge"

MOSQUITTO_IMAGE="eclipse-mosquitto@sha256:9cfdd46ad59f3e3e5f592f6baf57ab23e1ad00605509d0f5c1e9b179c5314d87"

WARMUP_SECONDS=10
OUTAGE_SECONDS=60
POWERCUT_SECONDS=20
RESTORE_SECONDS=90
# The simulator keeps publishing 30 s into P3 on purpose: measuring reordering at
# restore requires live traffic racing the drained backlog.
SIM_SECONDS=$((WARMUP_SECONDS + OUTAGE_SECONDS + POWERCUT_SECONDS + 30))
RATE=20
# SIGKILL, then this long before restarting -- the broker is genuinely dead in
# between, which is what a power cut looks like.
EDGE_DOWN_SECONDS=6

WORK="$(mktemp -d)"
MAIN_VERDICT="$WORK/verdict-main.txt"
OVERFLOW_VERDICT="$WORK/verdict-overflow.txt"
PERSIST_VERDICT="$WORK/verdict-persist.txt"
SQLITE_REPORT="$WORK/sqlite-probe.txt"
: >"$MAIN_VERDICT"; : >"$OVERFLOW_VERDICT"; : >"$PERSIST_VERDICT"; : >"$SQLITE_REPORT"

step()  { printf '\n\033[1;36m=== %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '  \033[1;33m!  %s\033[0m\n' "$*"; }

cleanup() {
  local rc=$?
  step "limpieza"
  docker rm -f "${PROJECT}-sqlite-probe" >/dev/null 2>&1 || true
  docker volume rm -f "${PROJECT}-probe-data" >/dev/null 2>&1 || true
  NANOMQ_CONF="nanomq-${PROFILE}.conf" docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $rc
}
trap cleanup EXIT INT TERM

compose() { docker compose "$@"; }

now_epoch() { date +%s.%3N; }

# Waits until a plain MQTT CONNECT to $2 from inside network $1 succeeds.
wait_broker() {
  local network="$1" host="$2" tries="${3:-60}" i
  for i in $(seq 1 "$tries"); do
    if docker run --rm --network "$network" "$MOSQUITTO_IMAGE" \
        mosquitto_pub -h "$host" -p 1883 -t 'fl/readiness' -m x -q 0 >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

edge_state()  { docker inspect -f '{{.State.Status}}' "$EDGE" 2>/dev/null || echo gone; }
edge_exit()   { docker inspect -f '{{.State.ExitCode}}' "$EDGE" 2>/dev/null || echo '?'; }
# NanoMQ traps SIGSEGV itself and logs "sig_handler: signal signumber: 11
# received!" before dying, so the crash is visible in the container log even
# though the exit code is a plain 1.
edge_segv()   { docker logs "$EDGE" 2>&1 | grep -c 'signumber: 11' || true; }
edge_msglost() { docker logs "$EDGE" 2>&1 | grep -ci 'Msg lost' || true; }

EDGE_CRASHED=no
EDGE_CRASH_NOTE=""
ROWS_BEFORE_KILL="(no medido)"
ROWS_AFTER_KILL="(no medido)"
ROWS_AFTER_RESTART="(no medido)"

check_edge_alive() {
  local where="$1" state; state="$(edge_state)"
  if [ "$state" != running ]; then
    EDGE_CRASHED=yes
    EDGE_CRASH_NOTE="el broker de borde murio en ${where} (estado=${state} exit=$(edge_exit), SIGSEGV x$(edge_segv))"
    warn "$EDGE_CRASH_NOTE"
    docker logs "$EDGE" 2>&1 | grep -iE "Msg lost|ctx_msgs|signumber" | tail -6 | sed 's/^/       /' || true
    return 1
  fi
  return 0
}

wait_marker() {
  local container="$1" path="$2" i
  for i in $(seq 1 240); do
    if docker exec "$container" test -f "$path" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  echo "marker $path never appeared in $container" >&2
  return 1
}

read_marker() { docker exec "$1" cat "$2" | tr -d '[:space:]'; }

received_count() {
  docker exec "$CONSUMER" sh -c 'wc -l < /results/received.jsonl 2>/dev/null || echo 0' \
    | tr -d '[:space:]'
}

# Row counts of the bridge cache database, read straight off the edge-data
# volume. `t_client_msg` is where queued bridge messages would sit, so this is
# the direct answer to "is the disk cache actually holding anything?".
sqlite_rows() {
  docker run --rm -v "${PROJECT}_edge-data:/data" fl-proto-nanomq-app:local \
    python -u /app/dbrows.py 2>/dev/null || echo "(no se pudo inspeccionar el volumen)"
}

link_attached() {
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$EDGE" 2>/dev/null \
    | tr ' ' '\n' | grep -qx "$LINK_NET"
}

cut_link() {
  docker network disconnect "$LINK_NET" "$EDGE"
}

restore_link() {
  docker network connect "$LINK_NET" "$EDGE"
}

# ---------------------------------------------------------------------------
# Preflight: is bridge persistence really absent from the default image?
#
# Sub-check A: mounted_file_path points at a directory that does not exist.
#              No SQLite -> starts fine. SQLite -> panics at bridge init.
# Sub-check B: same config with the directory present.
#              No SQLite -> stays empty. SQLite -> mqtt_client.db appears.
# ---------------------------------------------------------------------------
sqlite_probe() {
  step "PREFLIGHT  sonda de persistencia SQLite en $NANOMQ_IMAGE"
  local name="${PROJECT}-sqlite-probe"
  local vol="${PROJECT}-probe-data"
  local conf="$PWD/nanomq/nanomq-sqlite-probe.conf"

  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm -f "$vol" >/dev/null 2>&1 || true

  # --- A: directory absent -----------------------------------------------
  docker run -d --name "$name" --network none \
    -v "$conf:/etc/nanomq.conf:ro" \
    "$NANOMQ_IMAGE" nanomq start --conf /etc/nanomq.conf >/dev/null
  sleep 5
  local state_a exit_a logs_a accepted_a panic_a
  state_a="$(docker inspect -f '{{.State.Status}}' "$name")"
  exit_a="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
  logs_a="$(docker logs "$name" 2>&1 || true)"
  accepted_a=no
  if grep -q 'bridge.sqlite.disk_cache_size' <<<"$logs_a"; then accepted_a=yes; fi
  panic_a=no
  if grep -qi "panic: Can't open database" <<<"$logs_a"; then panic_a=yes; fi
  docker rm -f "$name" >/dev/null 2>&1 || true

  info "A) mounted_file_path inexistente -> estado=$state_a exit=$exit_a"
  info "   clave bridges.mqtt.cache aceptada por el parser: $accepted_a"
  info "   panic al abrir la base de datos: $panic_a"

  # --- B: directory present ----------------------------------------------
  docker volume create "$vol" >/dev/null
  docker run -d --name "$name" --network none \
    -v "$conf:/etc/nanomq.conf:ro" -v "$vol:/nanomq/data" \
    "$NANOMQ_IMAGE" nanomq start --conf /etc/nanomq.conf >/dev/null
  sleep 5
  local state_b listing db_b
  state_b="$(docker inspect -f '{{.State.Status}}' "$name")"
  listing="$(docker run --rm -v "$vol:/data" "$MOSQUITTO_IMAGE" ls -la /data 2>&1 || true)"
  db_b=no
  if grep -q 'mqtt_client.db' <<<"$listing"; then db_b=yes; fi
  local logs_b; logs_b="$(docker logs "$name" 2>&1 || true)"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm -f "$vol" >/dev/null 2>&1 || true

  info "B) mounted_file_path presente     -> estado=$state_b"
  info "   se crea mqtt_client.db: $db_b"

  local verdict
  if [ "$db_b" = yes ]; then
    verdict="SQLite PRESENTE en $NANOMQ_IMAGE: la cache de bridge funciona de verdad."
  elif [ "$accepted_a" = yes ]; then
    verdict="SQLite AUSENTE en $NANOMQ_IMAGE, y FALLA EN SILENCIO: el parser acepta bridges.mqtt.cache, lo imprime como bridge.sqlite.* en el arranque, y no crea nada."
  else
    verdict="SQLite AUSENTE en $NANOMQ_IMAGE y la clave ni se reconoce."
  fi
  warn "$verdict"

  {
    echo "imagen                                    : $NANOMQ_IMAGE"
    echo "A) dir inexistente  estado/exit           : $state_a / $exit_a"
    echo "A) clave bridges.mqtt.cache aceptada      : $accepted_a"
    echo "A) panic 'Can't open database'            : $panic_a"
    echo "B) dir presente     estado                : $state_b"
    echo "B) se crea mqtt_client.db                 : $db_b"
    echo "VEREDICTO                                 : $verdict"
    echo
    echo "--- lineas de log relevantes (sub-check A) ---"
    grep -iE "bridge.sqlite|panic|BUG|unable to open" <<<"$logs_a" | head -12 || true
    echo
    echo "--- lineas de log relevantes (sub-check B) ---"
    grep -iE "bridge.sqlite|panic|BUG|unable to open" <<<"$logs_b" | head -12 || true
    echo
    echo "--- contenido de mounted_file_path (sub-check B) ---"
    echo "$listing"
  } >"$SQLITE_REPORT"
}

# ---------------------------------------------------------------------------
# Brings up hub + edge + consumer with the given NanoMQ config and waits until
# the whole path is ready to carry traffic.
# ---------------------------------------------------------------------------
bring_up() {
  local conf="$1"
  export NANOMQ_CONF="$conf"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  compose up -d mosquitto-hub nanomq-edge >/dev/null
  wait_broker "$LINK_NET" mosquitto-hub || { echo "hub never came up" >&2; return 1; }
  wait_broker "$EDGE_NET" nanomq-edge || { echo "edge never came up" >&2; return 1; }
  compose up -d consumer >/dev/null
  wait_marker "$CONSUMER" /results/consumer.ready
  info "hub y borde arriba, consumidor suscrito"
}

drain() {
  local budget="$1" stable_needed="$2"
  local last=-1 stable=0 elapsed=0 count
  while [ "$elapsed" -lt "$budget" ]; do
    count="$(received_count)"
    if [ "$count" = "$last" ]; then
      stable=$((stable + 2))
      [ "$stable" -ge "$stable_needed" ] && break
    else
      stable=0
      info "drenando... recibidos=$count (t+${elapsed}s)"
    fi
    last="$count"
    sleep 2
    elapsed=$((elapsed + 2))
  done
  info "drenaje terminado: recibidos=$(received_count) tras ${elapsed}s"
}

# ---------------------------------------------------------------------------
# The five phases.
# ---------------------------------------------------------------------------
main_experiment() {
  step "P0  warmup (${WARMUP_SECONDS}s) -- enlace arriba, se comprueba el camino completo"
  bring_up "nanomq-${PROFILE}.conf"

  RATE_PER_SECOND="$RATE" RUN_SECONDS="$SIM_SECONDS" compose up -d simulator >/dev/null
  wait_marker "$SIMULATOR" /results/simulator.ready
  local t0; t0="$(read_marker "$SIMULATOR" /results/simulator.ready)"
  info "simulador publicando a ${RATE} msg/s durante ${SIM_SECONDS}s (t0=$t0)"
  sleep "$WARMUP_SECONDS"
  info "recibidos al final de P0: $(received_count)"

  step "P1  corte (${OUTAGE_SECONDS}s) -- docker network disconnect ${LINK_NET} ${EDGE}"
  local t1; t1="$(now_epoch)"
  cut_link
  info "enlace cortado; el simulador sigue publicando contra el borde"
  sleep "$OUTAGE_SECONDS"
  info "recibidos al final de P1: $(received_count)  (deberia seguir igual que en P0)"
  info "mensajes que el bridge declara perdidos en el log: $(edge_msglost)"
  check_edge_alive "P1" || true
  ROWS_BEFORE_KILL="$(sqlite_rows)"
  info "cache SQLite con el enlace caido, antes del SIGKILL:"
  info "  $ROWS_BEFORE_KILL"

  step "P2  corte de corriente (${POWERCUT_SECONDS}s) -- docker kill del broker de borde"
  local t2; t2="$(now_epoch)"
  if [ "$(edge_state)" = running ]; then
    docker kill --signal=SIGKILL "$EDGE" >/dev/null
    info "SIGKILL enviado (no SIGTERM: un apagado limpio escribiria la persistencia y no probaria nada)"
  else
    warn "no hace falta SIGKILL: el broker de borde ya estaba caido (ver P1). Se arranca de nuevo igualmente."
  fi
  sleep "$EDGE_DOWN_SECONDS"
  ROWS_AFTER_KILL="$(sqlite_rows)"
  info "cache SQLite con el broker muerto:"
  info "  $ROWS_AFTER_KILL"
  # `docker start`, not `compose up`: the container (and therefore the edge-data
  # volume with the SQLite cache) must be reused, not recreated.
  docker start "$EDGE" >/dev/null
  # Verified locally: `docker start` does NOT re-attach a network that was
  # removed with `docker network disconnect`, so the link stays down across the
  # restart. Checked anyway, because relying on that would be fragile.
  if link_attached; then
    warn "docker reconecto ${EDGE} a ${LINK_NET} al arrancar; se vuelve a cortar"
    cut_link
  else
    info "${EDGE} sigue desconectado de ${LINK_NET} tras el arranque"
  fi
  if wait_broker "$EDGE_NET" nanomq-edge 30; then
    info "broker de borde de vuelta, enlace todavia caido"
  else
    warn "el broker de borde no vuelve a aceptar conexiones tras el arranque"
  fi
  sleep $((POWERCUT_SECONDS - EDGE_DOWN_SECONDS))
  check_edge_alive "P2" || true
  ROWS_AFTER_RESTART="$(sqlite_rows)"
  info "cache SQLite tras el rearranque:"
  info "  $ROWS_AFTER_RESTART"

  step "P3  restauracion (<=${RESTORE_SECONDS}s) -- docker network connect"
  local t3; t3="$(now_epoch)"
  if link_attached; then
    info "el enlace ya estaba conectado"
  else
    restore_link
  fi
  info "enlace restaurado; se espera a que drene"
  # If the edge broker died earlier, bring it back before measuring the drain.
  # In a real deployment something (systemd, Docker's restart policy, a person)
  # would restart it, and "nothing drained because the broker was dead" is a
  # weaker statement than "the broker came back and still had nothing to drain".
  if [ "$(edge_state)" != running ]; then
    warn "el broker de borde esta caido al empezar P3: se arranca para poder medir el drenaje"
    docker start "$EDGE" >/dev/null
    if wait_broker "$EDGE_NET" nanomq-edge 30; then
      info "broker de borde arriba de nuevo"
    else
      warn "el broker de borde no vuelve"
    fi
  fi
  docker wait "$SIMULATOR" >/dev/null
  info "simulador terminado"
  docker logs "$SIMULATOR" 2>&1 | tail -3 | sed 's/^/    /'
  drain "$RESTORE_SECONDS" 10

  step "P4  verificacion"
  local far_future=9999999999
  export PHASES_JSON
  PHASES_JSON="$(cat <<JSON
[{"name":"P0 warmup","start":$t0,"end":$t1,"link_down":false},
 {"name":"P1 corte","start":$t1,"end":$t2,"link_down":true},
 {"name":"P2 corte corriente","start":$t2,"end":$t3,"link_down":true},
 {"name":"P3 restauracion","start":$t3,"end":$far_future,"link_down":false}]
JSON
)"
  PHASES_JSON="$(tr -d '\n' <<<"$PHASES_JSON")"
  compose run --rm --no-deps verifier | tee "$MAIN_VERDICT"

  {
    echo "-- Estabilidad del broker de borde -----------------------------------"
    echo "  murio durante el experimento              : $EDGE_CRASHED"
    if [ -n "$EDGE_CRASH_NOTE" ]; then echo "  $EDGE_CRASH_NOTE"; fi
    echo "  lineas 'Msg lost' en el log del bridge    : $(edge_msglost)"
    echo "  SIGSEGV registrados por NanoMQ (signal 11): $(edge_segv)"
    echo "  estado final del contenedor de borde      : $(edge_state) exit=$(edge_exit)"
    echo
    echo "-- Cache SQLite de bridge (filas en mounted_file_path) ---------------"
    echo "  con el enlace caido, antes del SIGKILL : $ROWS_BEFORE_KILL"
    echo "  con el broker muerto                   : $ROWS_AFTER_KILL"
    echo "  tras el rearranque                     : $ROWS_AFTER_RESTART"
    echo "======================================================================"
  } | tee -a "$MAIN_VERDICT"

  step "logs del broker de borde (transiciones del bridge)"
  docker logs "$EDGE" 2>&1 \
    | grep -ivE "print_bridge_conf|print_conf" \
    | grep -iE "bridge_tcp|sqlite|reconnect|Msg lost|ctx_msgs|resending|cached msg|signumber|panic|started successfully" \
    | tail -25 | sed 's/^/    /' || true
}

# ---------------------------------------------------------------------------
# P5: which end of the queue gets discarded on overflow. Tiny limits so the
# answer shows up in seconds. The docs do not state this for either
# max_send_queue_len or disk_cache_size.
# ---------------------------------------------------------------------------
overflow_probe() {
  step "P5  sonda de desbordamiento (limites minusculos: que extremo se descarta)"
  bring_up "nanomq-overflow-${PROFILE}.conf"

  local t_down; t_down="$(now_epoch)"
  cut_link
  info "enlace cortado antes de publicar nada"
  RATE_PER_SECOND=20 RUN_SECONDS=12 MESSAGE_LIMIT=200 compose up -d simulator >/dev/null
  wait_marker "$SIMULATOR" /results/simulator.ready
  docker wait "$SIMULATOR" >/dev/null
  info "200 mensajes publicados con el enlace caido"

  info "estado del borde tras publicar con el enlace caido: $(edge_state) exit=$(edge_exit)"
  local t_up; t_up="$(now_epoch)"
  restore_link
  drain 40 8

  PROFILE="${PROFILE}-overflow" \
  PHASES_JSON="$(printf '[{"name":"corte","start":%s,"end":%s,"link_down":true},{"name":"restauracion","start":%s,"end":9999999999,"link_down":false}]' "$t_down" "$t_up" "$t_up")" \
    compose run --rm --no-deps verifier | tee "$OVERFLOW_VERDICT"

  {
    echo "-- Limites usados en la sonda ----------------------------------------"
    grep -E '^[[:space:]]*(max_send_queue_len|disk_cache_size|flush_mem_threshold)' \
      "nanomq/nanomq-overflow-${PROFILE}.conf" | sed 's/^[[:space:]]*/  /'
    echo "  estado del borde al terminar: $(edge_state) exit=$(edge_exit)"
    echo "  lineas 'Msg lost' en el log  : $(edge_msglost)"
    echo "  SIGSEGV                     : $(edge_segv)"
    echo "======================================================================"
  } | tee -a "$OVERFLOW_VERDICT"
}

# ---------------------------------------------------------------------------
# P6: the same outage as P1 but with NO power cut, using the profile's real
# config. Without this the main run cannot separate "the queue could not hold the
# backlog" from "the SIGKILL threw the backlog away" -- they both show up as a
# hole in the same place. Run it and the two causes come apart.
# ---------------------------------------------------------------------------
persistence_probe() {
  step "P6  sonda de persistencia (mismo corte que P1, pero SIN corte de corriente)"
  bring_up "nanomq-${PROFILE}.conf"

  local t0 t_down t_up
  RATE_PER_SECOND="$RATE" RUN_SECONDS=50 MESSAGE_LIMIT=1000 compose up -d simulator >/dev/null
  wait_marker "$SIMULATOR" /results/simulator.ready
  t0="$(read_marker "$SIMULATOR" /results/simulator.ready)"
  sleep 5
  info "recibidos antes del corte: $(received_count)"

  t_down="$(now_epoch)"
  cut_link
  info "enlace cortado; se publica el resto del minuto sin matar el broker"
  docker wait "$SIMULATOR" >/dev/null
  info "estado del borde al terminar de publicar: $(edge_state) exit=$(edge_exit)"
  info "cache SQLite justo antes de restaurar el enlace:"
  info "  $(sqlite_rows)"

  t_up="$(now_epoch)"
  if [ "$(edge_state)" != running ]; then
    warn "el broker de borde murio durante el corte, sin que nadie lo matara"
    docker start "$EDGE" >/dev/null
    wait_broker "$EDGE_NET" nanomq-edge 30 || true
  fi
  restore_link
  drain 60 10

  PROFILE="${PROFILE}-persistencia" \
  PHASES_JSON="$(printf '[{"name":"antes del corte","start":%s,"end":%s,"link_down":false},{"name":"corte","start":%s,"end":%s,"link_down":true},{"name":"restauracion","start":%s,"end":9999999999,"link_down":false}]' "$t0" "$t_down" "$t_down" "$t_up" "$t_up")" \
    compose run --rm --no-deps verifier | tee "$PERSIST_VERDICT"

  {
    echo "-- Contexto de la sonda ----------------------------------------------"
    echo "  misma configuracion que el perfil ${PROFILE}, sin docker kill"
    echo "  estado del borde al terminar : $(edge_state) exit=$(edge_exit)"
    echo "  SIGSEGV                      : $(edge_segv)"
    echo "  cache SQLite al terminar     : $(sqlite_rows)"
    echo "======================================================================"
  } | tee -a "$PERSIST_VERDICT"
}

# ---------------------------------------------------------------------------
step "prototipo 03-nanomq-mosquitto  perfil=${PROFILE}  imagen=${NANOMQ_IMAGE}"
docker pull "$NANOMQ_IMAGE" >/dev/null
NANOMQ_CONF="nanomq-${PROFILE}.conf" compose build >/dev/null
info "imagen del simulador construida"

sqlite_probe
main_experiment
overflow_probe
persistence_probe

step "RESUMEN FINAL  perfil=${PROFILE}"
echo
echo "########## sonda SQLite (¿existe la persistencia de bridge?) ##########"
cat "$SQLITE_REPORT"
echo
echo "########## experimento principal (5 fases) ##########"
cat "$MAIN_VERDICT"
echo
echo "########## sonda de desbordamiento (¿que extremo se descarta?) ##########"
cat "$OVERFLOW_VERDICT"
echo
echo "########## sonda de persistencia (corte SIN corte de corriente) ##########"
cat "$PERSIST_VERDICT"
