#!/usr/bin/env python3
"""The Raspberry Pi side of the control path.

Subscribes to fertloops/+/cmd on the EDGE broker -- i.e. it stands in for the
UART reader receiving a setpoint and translating it into the ESP32 control
frame. Records arrival order and time so the verifier can tell whether commands
issued during the outage arrive at all, arrive once, and arrive in order.
"""

from __future__ import annotations

import json
import os
import signal
import time

import paho.mqtt.client as mqtt

import loadlib

BROKER_HOST = os.environ.get("BROKER_HOST", "edge-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))

arrivals: list[dict] = []
stopping = False


def main() -> None:
    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[cmdrecv] connected to {BROKER_HOST}: {reason_code}", flush=True)
        client.subscribe("fertloops/+/cmd", qos=1)

    def on_message(client, userdata, msg):
        try:
            cmd_id = json.loads(msg.payload)["cmd_id"]
        except (ValueError, KeyError):
            return
        arrivals.append(
            {
                "cmd_id": cmd_id,
                "order": len(arrivals) + 1,
                "t": time.time(),
                "qos": msg.qos,
                "dup": bool(msg.dup),
                "topic": msg.topic,
            }
        )
        print(f"[cmdrecv] cmd {cmd_id} qos={msg.qos} dup={msg.dup}", flush=True)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="fl-cmd-consumer",
        clean_session=False,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.max_queued_messages_set(0)
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    def handle_stop(signum, stack):
        global stopping
        stopping = True

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not stopping:
        loadlib.write_json("cmds-received.json", {"arrivals": arrivals})
        time.sleep(0.5)

    client.loop_stop()
    loadlib.write_json("cmds-received.json", {"arrivals": arrivals})
    print(f"[cmdrecv] final: {len(arrivals)} commands", flush=True)


if __name__ == "__main__":
    main()
