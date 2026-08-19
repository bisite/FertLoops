#!/usr/bin/env python3
"""The VPS ingest process, at fleet size.

Durable session, QoS 1, no client-side cap -- a truncated consumer queue would
be indistinguishable from a truncated broker queue, which is the thing being
measured.

Keeps a bit per sequence number instead of a line per delivery, and reports
progress through recv-state.json so run-load.sh can watch the drain without
counting lines in a 180 MB file.
"""

from __future__ import annotations

import json
import os
import signal
import time

import paho.mqtt.client as mqtt

import loadlib

BROKER_HOST = os.environ.get("BROKER_HOST", "hub-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
CAPACITY = int(os.environ.get("CAPACITY", "800000"))
TOPIC_FILTER = os.environ.get("TOPIC_FILTER", "fertloops/+/reading")

received = loadlib.SeqBitmap(CAPACITY)
state = {
    "deliveries": 0,
    "duplicates": 0,
    "inversions": 0,
    "max_seq": 0,
    "first_t": None,
    "last_t": None,
    "qos_seen": {},
}
stopping = False


def main() -> None:
    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[recv] connected to {BROKER_HOST}: {reason_code}", flush=True)
        client.subscribe(TOPIC_FILTER, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[recv] DISCONNECTED: {reason_code}", flush=True)

    def on_message(client, userdata, msg):
        now = time.time()
        try:
            seq = json.loads(msg.payload)["seq"]
        except (ValueError, KeyError):
            return
        state["deliveries"] += 1
        if not received.add(seq):
            state["duplicates"] += 1
        if seq < state["max_seq"]:
            state["inversions"] += 1
        else:
            state["max_seq"] = seq
        if state["first_t"] is None:
            state["first_t"] = now
        state["last_t"] = now
        q = str(msg.qos)
        state["qos_seen"][q] = state["qos_seen"].get(q, 0) + 1

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="fl-load-consumer",
        clean_session=False,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
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

    last_print = 0.0
    while not stopping:
        loadlib.write_json(
            "recv-state.json",
            {
                "unique": received.count,
                "deliveries": state["deliveries"],
                "duplicates": state["duplicates"],
                "inversions": state["inversions"],
                "max_seq": state["max_seq"],
                "last_t": state["last_t"],
                "t": time.time(),
            },
        )
        now = time.time()
        if now - last_print >= 10:
            last_print = now
            print(
                f"[recv] unique {received.count} deliveries {state['deliveries']} "
                f"dups {state['duplicates']} max_seq {state['max_seq']}",
                flush=True,
            )
        time.sleep(0.5)

    client.loop_stop()
    upto = max(state["max_seq"], 1)
    loadlib.write_json(
        "recv-final.json",
        {
            "unique": received.count,
            "deliveries": state["deliveries"],
            "duplicates": state["duplicates"],
            "inversions": state["inversions"],
            "max_seq": state["max_seq"],
            "first_t": state["first_t"],
            "last_t": state["last_t"],
            "qos_seen": state["qos_seen"],
            "gaps": received.gaps(upto),
        },
    )
    print(f"[recv] final: unique {received.count}, dups {state['duplicates']}", flush=True)


if __name__ == "__main__":
    main()
