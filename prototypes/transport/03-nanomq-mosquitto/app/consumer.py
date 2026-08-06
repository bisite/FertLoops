"""Subscribes at the Mosquitto hub and records every frame that arrives.

Stands in for whatever ingests readings on the VPS. It stays connected for the
whole run, so anything missing from its record was lost between the simulator and
the hub -- not by the consumer.

`clean_session=False` plus a fixed client id: if the consumer itself blips, the
hub keeps the session (see `persistent_client_expiration` in mosquitto.conf) and
the blip does not get charged to the edge broker.

Output: $RESULTS_DIR/received.jsonl, appended and flushed per message so the
verifier can read it while this process is still running.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import sys
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

BROKER_HOST = os.environ["BROKER_HOST"]
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
TOPIC = os.environ["TOPIC"]
CLIENT_ID = os.environ.get("CLIENT_ID", "fl-hub-consumer")
QOS = int(os.environ.get("QOS", "1"))
RESULTS_DIR = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))

stopping = False


def log(message: str) -> None:
    print(f"[consumer] {message}", flush=True)


def main() -> int:
    global stopping

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ready_marker = RESULTS_DIR / "consumer.ready"
    ready_marker.unlink(missing_ok=True)
    received_path = RESULTS_DIR / "received.jsonl"
    received_path.write_text("")
    handle = received_path.open("a", encoding="utf-8")
    arrival = {"n": 0}

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            log(f"connected to {BROKER_HOST}:{BROKER_PORT}, subscribing to {TOPIC}")
            client.subscribe(TOPIC, qos=QOS)
        else:
            log(f"connect refused: {reason_code}")

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        log(f"subscribed: {reason_code_list}")
        ready_marker.write_text(f"{time.time():.6f}\n")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        log(f"disconnected (rc={reason_code})")

    def on_message(client, userdata, message):
        now = time.time()
        arrival["n"] += 1
        try:
            seq = json.loads(message.payload)["seq"]
        except (ValueError, KeyError, TypeError):
            seq = None
        handle.write(
            json.dumps(
                {
                    "arrival": arrival["n"],
                    "seq": seq,
                    "t_recv": now,
                    "qos": message.qos,
                    "dup": bool(message.dup),
                    "retain": bool(message.retain),
                }
            )
            + "\n"
        )
        handle.flush()

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        clean_session=False,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=2)

    def handle_signal(signum, frame):
        global stopping
        stopping = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    deadline = time.monotonic() + 60
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
    while not stopping:
        time.sleep(0.2)

    log(f"stopping after {arrival['n']} messages")
    client.loop_stop()
    handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
