"""Publishes ESP32 reading frames to the NanoMQ edge broker at a fixed cadence.

Stands in for the Raspberry Pi gateway: it keeps polling the ESP32 and
republishing regardless of what the uplink to the VPS is doing.

Accounting rule that makes the numbers mean something: a frame counts as
*published* only once the edge broker has PUBACKed it. If the edge broker is not
reachable at all (phase P2, while it is SIGKILLed) the simulator does not hand the
frame to paho either -- it records the attempt as `no_conn` and moves on. Letting
paho's own out-queue absorb those would measure paho's buffer instead of the
broker's, which is not the question.

Output: $RESULTS_DIR/published.jsonl, one JSON object per attempt.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from frame import build_frame

BROKER_HOST = os.environ["BROKER_HOST"]
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
TOPIC = os.environ["TOPIC"]
DEV_ID = os.environ["DEV_ID"]
CLIENT_ID = os.environ.get("CLIENT_ID", "fl-edge-simulator")
QOS = int(os.environ.get("QOS", "1"))
RATE_PER_SECOND = float(os.environ.get("RATE_PER_SECOND", "20"))
RUN_SECONDS = float(os.environ.get("RUN_SECONDS", "120"))
MESSAGE_LIMIT = int(os.environ.get("MESSAGE_LIMIT", "0"))  # 0 = no limit
RESULTS_DIR = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
ACK_GRACE_SECONDS = float(os.environ.get("ACK_GRACE_SECONDS", "15"))
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONNECT_TIMEOUT_SECONDS", "60"))

lock = threading.Lock()
attempts: dict[int, dict] = {}  # seq -> record
mid_to_seq: dict[int, int] = {}
connected = threading.Event()


def log(message: str) -> None:
    print(f"[simulator] {message}", flush=True)


def on_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code == 0:
        connected.set()
        log(f"connected to {BROKER_HOST}:{BROKER_PORT}")
    else:
        log(f"connect refused: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    connected.clear()
    log(f"disconnected (rc={reason_code}), paho will retry")


def on_publish(client, userdata, mid, reason_code, properties):
    now = time.time()
    with lock:
        seq = mid_to_seq.get(mid)
        if seq is None:
            return
        record = attempts[seq]
        record["acked"] = True
        record["t_ack"] = now


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ready_marker = RESULTS_DIR / "simulator.ready"
    ready_marker.unlink(missing_ok=True)

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        # clean_session=True: the gateway app deliberately has no local store.
        # Everything durable must come from the broker, which is what we measure.
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    client.reconnect_delay_set(min_delay=1, max_delay=2)

    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    while True:
        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
            break
        except OSError as exc:
            if time.monotonic() > deadline:
                log(f"giving up connecting to {BROKER_HOST}: {exc}")
                return 1
            time.sleep(0.5)

    client.loop_start()
    if not connected.wait(timeout=CONNECT_TIMEOUT_SECONDS):
        log("never got CONNACK")
        return 1

    # Plain epoch float: run-experiment.sh reads this to anchor phase P0 to the
    # instant the first frame could have been published, not to when it started
    # the container.
    ready_marker.write_text(f"{time.time():.6f}\n")

    interval = 1.0 / RATE_PER_SECOND
    started = time.monotonic()
    started_wall = time.time()
    seq = 0
    log(
        f"publishing to {TOPIC} at {RATE_PER_SECOND} msg/s for {RUN_SECONDS}s "
        f"(qos={QOS}, limit={MESSAGE_LIMIT or 'none'})"
    )

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= RUN_SECONDS:
            break
        if MESSAGE_LIMIT and seq >= MESSAGE_LIMIT:
            break

        seq += 1
        now = time.time()
        record = {"seq": seq, "t_pub": now, "acked": False, "t_ack": None, "rc": None}
        with lock:
            attempts[seq] = record

        if connected.is_set():
            payload = build_frame(DEV_ID, seq, now)
            info = client.publish(TOPIC, payload, qos=QOS, retain=False)
            with lock:
                record["rc"] = int(info.rc)
                mid_to_seq[info.mid] = seq
            if QOS == 0 and info.rc == mqtt.MQTT_ERR_SUCCESS:
                # No PUBACK exists at QoS 0; handing it to the socket is all the
                # confirmation there is.
                with lock:
                    record["acked"] = True
                    record["t_ack"] = now
        else:
            with lock:
                record["rc"] = "no_conn"

        target = started + seq * interval
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    log(f"stopped publishing after {seq} attempts; waiting up to {ACK_GRACE_SECONDS}s for PUBACKs")
    grace_end = time.monotonic() + ACK_GRACE_SECONDS
    while time.monotonic() < grace_end:
        with lock:
            pending = sum(1 for r in attempts.values() if not r["acked"] and r["rc"] == 0)
        if pending == 0:
            break
        time.sleep(0.5)

    client.loop_stop()
    try:
        client.disconnect()
    except OSError:
        pass

    with lock:
        records = [attempts[k] for k in sorted(attempts)]
        acked = sum(1 for r in records if r["acked"])
        no_conn = sum(1 for r in records if r["rc"] == "no_conn")

    out = RESULTS_DIR / "published.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    meta = {
        "started_wall": started_wall,
        "finished_wall": time.time(),
        "attempts": len(records),
        "acked": acked,
        "no_conn": no_conn,
        "rate_per_second": RATE_PER_SECOND,
        "run_seconds": RUN_SECONDS,
        "qos": QOS,
        "topic": TOPIC,
    }
    (RESULTS_DIR / "simulator.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    log(f"attempts={len(records)} acked_by_edge_broker={acked} not_even_handed_off={no_conn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
