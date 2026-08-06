#!/usr/bin/env bash
# One command, five phases, one verdict block.
#
#   ./run-experiment.sh naive
#   ./run-experiment.sh jetstream-mirror
#
# Optional: LEAF_MAX_MSGS caps the edge JetStream stream, in messages.
# Defaults to -1 (uncapped). Set it to 1000 to match the Mosquitto default the
# sibling prototypes hit, which makes the overflow behaviour comparable:
#
#   LEAF_MAX_MSGS=1000 ./run-experiment.sh jetstream-mirror
#
# Nothing is installed on the host and no host port is published: every client
# is a container on the compose networks.
set -euo pipefail

cd "$(dirname "$0")"

PROFILE="${1:-}"
case "$PROFILE" in
  naive)
    HUB_CONF=hub-naive.conf
    LEAF_CONF=leaf-naive.conf
    ;;
  jetstream-mirror)
    HUB_CONF=hub-jetstream.conf
    LEAF_CONF=leaf-jetstream.conf
    ;;
  *)
    echo "usage: $0 {naive|jetstream-mirror}" >&2
    exit 2
    ;;
esac

export PROFILE HUB_CONF LEAF_CONF
export LEAF_MAX_MSGS="${LEAF_MAX_MSGS:--1}"

PROJECT=fl-proto-nats
LINK_NET="${PROJECT}_link"
LEAF_CT="${PROJECT}-leaf"
CONSUMER_CT="${PROJECT}-consumer"

# Phase durations, from the shared protocol in ../README.md. Overridable only so
# the harness itself can be debugged quickly; any number that goes in the README
# must come from a run with these defaults, or it is not comparable with the
# sibling prototypes.
P0_S="${P0_S:-10}"
P1_S="${P1_S:-60}"
P2_DOWN_S="${P2_DOWN_S:-5}"      # how long the leaf stays dead after SIGKILL
P2_S="${P2_S:-20}"               # total length of the power-cut phase
P3_MAX_S="${P3_MAX_S:-90}"
P3_MIN_S="${P3_MIN_S:-20}"           # never declare "drained" before this
DRAIN_QUIET_S="${DRAIN_QUIET_S:-6}"  # no new arrivals for this long => drained

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
now() { date +%s.%N; }

cleanup() {
  local status=$?
  say "cleanup"
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  # Belt and braces: the compose network is gone by now, but if the run died
  # mid-outage the leaf could still be detached from a network that survived.
  docker network rm "$LINK_NET" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_for_log() {
  # wait_for_log <service> <pattern> <timeout_s>
  local service="$1" pattern="$2" timeout="$3" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if docker compose logs "$service" 2>/dev/null | grep -q -- "$pattern"; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "timed out waiting for '$pattern' in $service logs" >&2
  docker compose logs "$service" >&2 || true
  return 1
}

received_count() {
  docker compose exec -T consumer sh -c 'wc -l < /results/received.jsonl 2>/dev/null || echo 0' \
    | tr -d '[:space:]'
}

link_attached() {
  docker inspect -f '{{range $net, $_ := .NetworkSettings.Networks}}{{$net}} {{end}}' "$LEAF_CT" \
    2>/dev/null | grep -q "$LINK_NET"
}

say "profile=$PROFILE  leaf=$LEAF_CONF  hub=$HUB_CONF  leaf_stream_max_msgs=$LEAF_MAX_MSGS"

say "setup: tearing down any previous run and building"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
docker compose build --quiet

say "setup: starting the two NATS servers"
docker compose up -d nats-hub nats-leaf
info "waiting for both to report healthy"
for ct in "$PROJECT-hub" "$LEAF_CT"; do
  waited=0
  until [ "$(docker inspect -f '{{.State.Health.Status}}' "$ct")" = healthy ]; do
    sleep 1
    waited=$((waited + 1))
    [ "$waited" -gt 60 ] && { echo "$ct never became healthy" >&2; exit 1; }
  done
  info "$ct healthy"
done

say "setup: starting the hub consumer (it also creates the streams)"
docker compose up -d consumer
wait_for_log consumer READY 90
info "consumer ready"

say "setup: starting the edge simulator"
docker compose up -d simulator
wait_for_log simulator PUBLISHING 90
info "simulator publishing"

T0=$(now)

say "P0 warmup (${P0_S}s): link up, confirming the whole path works"
sleep "$P0_S"
info "received so far: $(received_count)"
T1=$(now)

say "P1 outage (${P1_S}s): disconnecting the leaf from ${LINK_NET}"
docker network disconnect "$LINK_NET" "$LEAF_CT"
info "leaf networks now: $(docker inspect -f '{{range $n, $_ := .NetworkSettings.Networks}}{{$n}} {{end}}' "$LEAF_CT")"
sleep "$P1_S"
info "received so far: $(received_count)"
T2=$(now)

say "P2 power cut (${P2_S}s): SIGKILL on the leaf, link still down"
docker kill --signal=KILL "$LEAF_CT" >/dev/null
info "leaf killed (SIGKILL, no clean shutdown, no chance to flush)"
sleep "$P2_DOWN_S"
docker start "$LEAF_CT" >/dev/null
info "leaf restarted"
if link_attached; then
  # Confirmed locally that `docker start` does NOT re-attach a network that was
  # removed with `docker network disconnect`, so this branch should never fire.
  # Kept as a guard because the measurement would be meaningless if it did.
  info "WARNING: docker re-attached $LINK_NET on start, disconnecting again"
  docker network disconnect "$LINK_NET" "$LEAF_CT"
fi
sleep "$((P2_S - P2_DOWN_S))"
info "received so far: $(received_count)"
T3=$(now)

say "P3 restore (<= ${P3_MAX_S}s): reconnecting the leaf to ${LINK_NET}"
before=$(received_count)
docker network connect "$LINK_NET" "$LEAF_CT"
info "arrivals before reconnect: $before"
info "waiting for the drain to start and then go quiet for ${DRAIN_QUIET_S}s"
last=-1
quiet=0
waited=0
# Wall clock, not iteration count: each poll shells into a container, so a
# counted loop would overrun the phase budget by 10 percent or so.
p3_deadline=$(( $(date +%s) + P3_MAX_S ))
while [ "$(date +%s)" -lt "$p3_deadline" ]; do
  waited=$(( P3_MAX_S - (p3_deadline - $(date +%s)) ))
  count=$(received_count)
  if [ "$count" = "$last" ]; then
    quiet=$((quiet + 1))
  else
    [ "$quiet" -gt 0 ] && info "t+${waited}s arrived: $count"
    quiet=0
  fi
  last="$count"
  # Two guards. The minimum, because a link that has just come back looks
  # exactly like a finished drain for the first few seconds. And "count must
  # have moved", because if the backlog never drains at all -- which is a real
  # outcome, not a bug -- the phase has to run its full length before we can
  # honestly say nothing came back.
  if [ "$quiet" -ge "$DRAIN_QUIET_S" ] && [ "$waited" -ge "$P3_MIN_S" ] && [ "$count" -gt "$before" ]; then
    break
  fi
  sleep 1
done
info "drain settled at $(received_count) received after ${waited}s (was $before at reconnect)"
T4=$(now)

say "P4 verification"
docker compose stop simulator >/dev/null
info "simulator stopped, giving the hub 5s to catch up with the tail"
sleep 5
docker compose stop consumer >/dev/null

PHASES=$(printf '[["P0",%s,%s],["P1",%s,%s],["P2",%s,%s],["P3",%s,%s]]' \
  "$T0" "$T1" "$T1" "$T2" "$T2" "$T3" "$T3" "$T4")

PHASES="$PHASES" docker compose run --rm -T -e "PHASES=$PHASES" verifier

# UTC, because the containers have no TZ set and their logs are in UTC. Printing
# local time here would silently offset every correlation by the host's zone.
say "phase boundaries in UTC, to read the server logs against"
for label_ts in "P0 start:$T0" "P1 link cut:$T1" "P2 SIGKILL:$T2" "P3 link back:$T3" "P4 verify:$T4"; do
  info "$(printf '%-14s %sZ' "${label_ts%%:*}" "$(date -u -d "@${label_ts##*:}" '+%H:%M:%S')")"
done

say "leaf server log: link and JetStream events"
docker compose logs --no-log-prefix nats-leaf \
  | grep -iE "leafnode|Starting nats-server|Restored|Starting restore|Server is ready|slow consumer|dropped" \
  || info "(no matching lines)"

say "hub server log: link and JetStream events"
docker compose logs --no-log-prefix nats-hub \
  | grep -iE "leafnode|Restored|Starting restore|slow consumer|dropped|mirror" \
  || info "(no matching lines)"
