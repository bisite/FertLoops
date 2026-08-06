#!/usr/bin/env python3
"""Stands in for the Raspberry Pi's UART reader.

Publishes ESP32 frames to the *local* (edge) broker at a fixed rate and records
three separate facts, because conflating them is how prototypes end up lying:

  published.jsonl  every frame the source produced, with `sent` = whether the
                   client was connected and handed it to the broker at all.
  acks.jsonl       every frame the edge broker actually PUBACKed. This -- not
                   the attempt count -- is the "published" figure the verifier
                   compares against, because it is the set the broker took
                   responsibility for.
  phases.jsonl     when each experiment phase started, on the simulator's clock.

The current phase is read from $RESULTS_DIR/phase, which run-experiment.sh
rewrites atomically. Writing "stop" there ends the run.
"""

import os
import pathlib
import threading
import time

import paho.mqtt.client as mqtt

import frame

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
BROKER_HOST = os.environ.get("BROKER_HOST", "edge-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
RATE_HZ = float(os.environ.get("RATE_HZ", "20"))
CLIENT_ID = os.environ.get("CLIENT_ID", "fl-simulator")
PUBLISH_QOS = int(os.environ.get("PUBLISH_QOS", "1"))

PHASE_FILE = RESULTS / "phase"

# mid -> seq. Guarded by `lock` because on_publish runs on the network thread
# and can fire before publish() has returned to us.
pending: dict[int, int] = {}
early_acks: set[int] = set()
lock = threading.Lock()


def read_phase(current: str) -> str:
    try:
        return PHASE_FILE.read_text().strip() or current
    except FileNotFoundError:
        return current


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    attempts = open(RESULTS / "published.jsonl", "a", buffering=1)
    acks = open(RESULTS / "acks.jsonl", "a", buffering=1)
    phases = open(RESULTS / "phases.jsonl", "a", buffering=1)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[simulator] connected to {BROKER_HOST}: {reason_code}", flush=True)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[simulator] DISCONNECTED: {reason_code}", flush=True)

    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        now = time.time()
        with lock:
            seq = pending.pop(mid, None)
            if seq is None:
                early_acks.add(mid)
                return
        acks.write('{"seq":%d,"t":%.6f}\n' % (seq, now))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        # A transient publisher: the point of the experiment is what the
        # *broker* keeps, so the client is not allowed to hide losses in a
        # session of its own.
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=15)
    client.loop_start()

    # Wait for the first connection before starting the cadence, so seq 1 is
    # not spuriously counted as "refused at source".
    for _ in range(100):
        if client.is_connected():
            break
        time.sleep(0.1)

    period = 1.0 / RATE_HZ
    seq = 0
    # PW, not P0: until run-experiment.sh has confirmed the bridge is delivering
    # live, nothing published is part of the measurement. Defaulting to P0 here
    # silently attributed the pre-warmup frames to the warmup phase.
    phase = read_phase("PW")
    phases.write('{"phase":"%s","t":%.6f}\n' % (phase, time.time()))
    print(f"[simulator] publishing at {RATE_HZ} msg/s, QoS {PUBLISH_QOS}", flush=True)

    next_at = time.monotonic()
    sent_count = 0
    unsent_count = 0
    while True:
        new_phase = read_phase(phase)
        if new_phase != phase:
            phase = new_phase
            phases.write('{"phase":"%s","t":%.6f}\n' % (phase, time.time()))
            print(
                f"[simulator] phase -> {phase} "
                f"(handed off {sent_count}, refused {unsent_count})",
                flush=True,
            )
            if phase == "stop":
                break

        seq += 1
        payload = frame.build(seq)
        now = time.time()
        if client.is_connected():
            info = client.publish(frame.TOPIC, payload, qos=PUBLISH_QOS)
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS
            if ok:
                with lock:
                    if info.mid in early_acks:
                        early_acks.discard(info.mid)
                        acks.write('{"seq":%d,"t":%.6f}\n' % (seq, now))
                    else:
                        pending[info.mid] = seq
                sent_count += 1
            else:
                unsent_count += 1
        else:
            # The local broker is down (phase P2). A real Pi has nowhere to put
            # this frame either unless it buffers on disk itself, which is a
            # different design decision -- recorded separately so it is never
            # confused with a broker-side loss.
            ok = False
            unsent_count += 1
        attempts.write(
            '{"seq":%d,"t":%.6f,"phase":"%s","sent":%s}\n'
            % (seq, now, phase, "true" if ok else "false")
        )

        next_at += period
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Fell behind (broker restart storm): resynchronise rather than
            # burst, so the cadence stays honest.
            next_at = time.monotonic()

    # Give in-flight PUBACKs a moment to land before we stop counting.
    time.sleep(2)
    client.loop_stop()
    print(
        f"[simulator] done: {seq} frames produced, {sent_count} handed to the "
        f"broker, {unsent_count} refused (broker unreachable)",
        flush=True,
    )
    for f in (attempts, acks, phases):
        f.close()


if __name__ == "__main__":
    main()
