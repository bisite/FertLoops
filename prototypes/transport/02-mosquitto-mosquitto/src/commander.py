#!/usr/bin/env python3
"""Publishes control commands from the VPS towards the Raspberry Pi.

This is the half of issue #4 that round one never measured: readings travel
edge -> hub, but a fertigation system also has to send a setpoint hub -> edge,
and the interesting case is the command issued while the Pi is unreachable.

Runs once (`docker compose run --rm commander`) and exits. Publishes at QoS 1
to fertloops/<devID>/cmd on the HUB broker; the bridge is subscribed there with
a persistent session, so the hub must queue these until the Pi comes back.

The payload is the real ESP32 control frame from docs/trama-de-datos-riego.md,
plus a `cmd_id` field that exists only for the experiment.
"""

from __future__ import annotations

import json
import os
import time

import paho.mqtt.client as mqtt

import loadlib

BROKER_HOST = os.environ.get("BROKER_HOST", "hub-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
CMD_COUNT = int(os.environ.get("CMD_COUNT", "60"))
CMD_RATE_HZ = float(os.environ.get("CMD_RATE_HZ", "5"))
N_DEVICES = int(os.environ.get("N_DEVICES", "12"))

acked: set[int] = set()


def build_command(cmd_id: int, valve: int) -> bytes:
    return json.dumps(
        {
            "cmd_id": cmd_id,
            "Control": {"Inv": {"On": 1, "Freq": 50}, "Valve": valve},
        },
        separators=(",", ":"),
    ).encode()


def main() -> None:
    devices = loadlib.device_ids(N_DEVICES)
    pending: dict[int, int] = {}

    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        cmd_id = pending.pop(mid, None)
        if cmd_id is not None:
            acked.add(cmd_id)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="fl-commander",
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.on_publish = on_publish
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    sent = []
    for cmd_id in range(1, CMD_COUNT + 1):
        dev = devices[(cmd_id - 1) % N_DEVICES]
        info = client.publish(
            f"fertloops/{dev}/cmd", build_command(cmd_id, 30 + cmd_id % 60), qos=1
        )
        pending[info.mid] = cmd_id
        sent.append({"cmd_id": cmd_id, "dev": dev, "t": time.time()})
        time.sleep(1.0 / CMD_RATE_HZ)

    time.sleep(3)
    client.loop_stop()
    loadlib.write_json(
        "cmds-sent.json",
        {"count": CMD_COUNT, "acked_by_hub": sorted(acked), "sent": sent},
    )
    print(f"[cmd] published {CMD_COUNT} commands, {len(acked)} PUBACKed by the hub", flush=True)


if __name__ == "__main__":
    main()
