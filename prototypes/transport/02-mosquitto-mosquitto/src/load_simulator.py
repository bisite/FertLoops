#!/usr/bin/env python3
"""The Raspberry Pi's UART reader, at the real fleet size.

One MQTT client, twelve topics. That is deliberate and it is the accurate
model: the real system has ONE reader process on the Pi polling twelve ESP32s
over UART, not twelve independent publishers. It matters because Mosquitto's
queue is per-client, so what governs the outage buffer is the bridge's single
local client, not how many drainage tables feed it.

Phases, driven by $RESULTS_DIR/phase:

  FILL   publish as fast as backpressure allows until TARGET_MSGS have been
         PUBACKed by the edge broker, then idle. This is the queue being built
         up to a 15- or 30-day backlog.
  DRAIN  publish at DRAIN_RATE_HZ. This is live traffic arriving while the
         broker works off the backlog -- the case that cost round one 100 % of
         P3 when the queue was capped.
  stop   write the accounting and exit.
"""

from __future__ import annotations

import os
import threading
import time

import paho.mqtt.client as mqtt

import frame_multi
import loadlib

BROKER_HOST = os.environ.get("BROKER_HOST", "edge-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
TARGET_MSGS = int(os.environ.get("TARGET_MSGS", "518400"))
DRAIN_RATE_HZ = float(os.environ.get("DRAIN_RATE_HZ", "20"))
N_DEVICES = int(os.environ.get("N_DEVICES", "12"))
# Cap on (handed to paho) - (PUBACKed). Without it the publisher's own memory
# becomes the experiment, which is not what is being measured.
INFLIGHT_CAP = int(os.environ.get("INFLIGHT_CAP", "20000"))
CAPACITY = TARGET_MSGS + int(os.environ.get("HEADROOM", "200000"))

pending: dict[int, int] = {}
early_acks: set[int] = set()
acked = loadlib.SeqBitmap(CAPACITY)
lock = threading.Lock()


def main() -> None:
    devices = loadlib.device_ids(N_DEVICES)
    topics = [f"fertloops/{d}/reading" for d in devices]
    marks: list[dict] = []

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[sim] connected to {BROKER_HOST}: {reason_code}", flush=True)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[sim] DISCONNECTED: {reason_code}", flush=True)

    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        with lock:
            seq = pending.pop(mid, None)
            if seq is None:
                early_acks.add(mid)
                return
            acked.add(seq)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="fl-load-simulator",
        # Transient on purpose: what the *broker* keeps is the measurement, so
        # the publisher is not allowed to hide a loss in a session of its own.
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    # Default is 20. The fill would take an hour at 20 in flight over a
    # container network; this governs the publisher, not the bridge, so it does
    # not touch what is being measured.
    client.max_inflight_messages_set(1000)
    client.max_queued_messages_set(0)
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    for _ in range(150):
        if client.is_connected():
            break
        time.sleep(0.1)

    seq = 0
    attempted = 0
    refused = 0
    phase = loadlib.read_phase("FILL")
    marks.append({"phase": phase, "first_seq": 1, "t": time.time()})
    print(f"[sim] {N_DEVICES} devices, target {TARGET_MSGS} acked frames", flush=True)

    t_start = time.time()
    last_report = 0.0
    filled_at = None
    next_at = time.monotonic()

    while True:
        new_phase = loadlib.read_phase(phase)
        if new_phase != phase:
            marks.append({"phase": new_phase, "first_seq": seq + 1, "t": time.time()})
            print(f"[sim] phase {phase} -> {new_phase} at seq {seq}", flush=True)
            phase = new_phase
            next_at = time.monotonic()
            if phase == "stop":
                break

        with lock:
            n_acked = acked.count
        if phase == "FILL" and n_acked >= TARGET_MSGS:
            if filled_at is None:
                filled_at = time.time()
                rate = TARGET_MSGS / max(filled_at - t_start, 1e-6)
                print(
                    f"[sim] FILL complete: {n_acked} acked in "
                    f"{filled_at - t_start:.1f}s ({rate:.0f} msg/s)",
                    flush=True,
                )
            time.sleep(0.2)
            loadlib.write_json(
                "sim-state.json",
                {"phase": phase, "seq": seq, "attempted": attempted,
                 "acked": n_acked, "refused": refused, "filled": True},
            )
            continue

        # Backpressure: never let the publisher's own backlog become the story.
        if attempted - n_acked >= INFLIGHT_CAP:
            time.sleep(0.01)
            continue

        seq += 1
        if seq >= CAPACITY:
            print("[sim] capacity reached, stopping", flush=True)
            break
        payload = frame_multi.build(seq, devices[(seq - 1) % N_DEVICES])
        if client.is_connected():
            info = client.publish(topics[(seq - 1) % N_DEVICES], payload, qos=1)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                with lock:
                    if info.mid in early_acks:
                        early_acks.discard(info.mid)
                        acked.add(seq)
                    else:
                        pending[info.mid] = seq
                attempted += 1
            else:
                refused += 1
        else:
            # The edge broker is dead (S3). A real Pi has nowhere to put this
            # frame either unless the reader spools it itself -- a separate
            # design decision, so it is counted separately and never confused
            # with a broker-side loss.
            refused += 1

        now = time.time()
        if now - last_report >= 1.0:
            last_report = now
            loadlib.write_json(
                "sim-state.json",
                {"phase": phase, "seq": seq, "attempted": attempted,
                 "acked": n_acked, "refused": refused, "filled": filled_at is not None},
            )
            if seq % 25000 < 200:
                print(f"[sim] seq {seq} attempted {attempted} acked {n_acked}", flush=True)

        # FILL is the only phase that runs flat out. Every other phase (WARM
        # before the cut, HOLD across the SIGKILL, DRAIN while the backlog is
        # worked off) publishes at the cadence a real Pi would, so that live
        # traffic competing with a draining queue is part of the measurement.
        if phase != "FILL" and DRAIN_RATE_HZ > 0:
            next_at += 1.0 / DRAIN_RATE_HZ
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()

    time.sleep(3)
    client.loop_stop()
    with lock:
        n_acked = acked.count
        ack_ranges = acked.ranges(seq)
    loadlib.write_json(
        "sim-final.json",
        {
            "produced": seq,
            "attempted": attempted,
            "acked": n_acked,
            "refused": refused,
            "target": TARGET_MSGS,
            "devices": N_DEVICES,
            "fill_seconds": (filled_at - t_start) if filled_at else None,
            "marks": marks,
            "acked_ranges": ack_ranges,
        },
    )
    print(f"[sim] done: produced {seq}, acked {n_acked}, refused {refused}", flush=True)


if __name__ == "__main__":
    main()
