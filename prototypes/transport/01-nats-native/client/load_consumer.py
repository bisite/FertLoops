"""Hub consumer for the `load` profile, plus the hub-side mirror setup.

Two jobs:

  1. Create FRAMES_MIRROR in the `hub` domain as a mirror of the edge stream,
     addressing $JS.edge.API across the leafnode link. This is the topology
     round one identified as the only documented path to edge durability, and
     the thing whose operational cost is being weighed against Mosquitto's.

  2. Consume it and account for what arrived, by bitmap rather than by line --
     at 518 400 frames a line per delivery would make the consumer the
     bottleneck instead of the transport.

mirror-state.jsonl is the important artefact here. It snapshots the mirror's
own `lag` and `active` fields once a second, which is where the ~26 s resume
that round one measured with a 1 000-message backlog becomes visible -- and
where this round finds out whether that 26 s is a constant or scales with the
size of the backlog.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import time

import nats
from nats.js import api as js_api

import loadlib

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
NATS_URL = os.environ["NATS_URL"]
SUBJECT_FILTER = os.environ.get("SUBJECT_FILTER", "fertloops.>")
EDGE_DOMAIN = os.environ.get("EDGE_DOMAIN", "edge")
HUB_DOMAIN = os.environ.get("HUB_DOMAIN", "hub")
EDGE_STREAM = os.environ.get("EDGE_STREAM", "FRAMES")
HUB_STREAM = os.environ.get("HUB_STREAM", "FRAMES_MIRROR")
CAPACITY = int(os.environ.get("CAPACITY", "800000"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [recv] {msg}", flush=True)


class LoadConsumer:
    def __init__(self) -> None:
        self.nc: nats.NATS | None = None
        self.stop = asyncio.Event()
        self.received = loadlib.SeqBitmap(CAPACITY)
        self.deliveries = 0
        self.duplicates = 0
        self.inversions = 0
        self.max_seq = 0
        self.first_t = None
        self.last_t = None
        self.mirror_log = (RESULTS / "mirror-state.jsonl").open("a", buffering=1)

    async def connect(self) -> None:
        self.nc = await nats.connect(
            NATS_URL,
            name="fl-load-consumer",
            max_reconnect_attempts=-1,
            reconnect_time_wait=0.25,
            connect_timeout=5,
        )
        log(f"connected to {NATS_URL}")

    async def setup_mirror(self):
        js_hub = self.nc.jetstream(domain=HUB_DOMAIN, timeout=30)
        cfg = js_api.StreamConfig(
            name=HUB_STREAM,
            storage=js_api.StorageType.FILE,
            retention=js_api.RetentionPolicy.LIMITS,
            max_msgs=-1,
            mirror=js_api.StreamSource(
                name=EDGE_STREAM,
                external=js_api.ExternalStream(api=f"$JS.{EDGE_DOMAIN}.API"),
            ),
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                await js_hub.add_stream(cfg)
                log(f"mirror {HUB_DOMAIN}/{HUB_STREAM} -> {EDGE_DOMAIN}/{EDGE_STREAM} ready")
                return js_hub
            except Exception as exc:  # noqa: BLE001
                log(f"mirror not ready: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1)
        raise SystemExit("could not create the mirror stream")

    async def poll_mirror(self, js_hub) -> None:
        while not self.stop.is_set():
            try:
                info = await js_hub.stream_info(HUB_STREAM)
                m = info.mirror
                self.mirror_log.write(
                    json.dumps(
                        {
                            "t": time.time(),
                            "messages": info.state.messages,
                            "bytes": info.state.bytes,
                            "first_seq": info.state.first_seq,
                            "last_seq": info.state.last_seq,
                            "lag": getattr(m, "lag", None) if m else None,
                            "active_ns": getattr(m, "active", None) if m else None,
                            "error": str(getattr(m, "error", None)) if m else None,
                        }
                    )
                    + "\n"
                )
            except Exception as exc:  # noqa: BLE001
                self.mirror_log.write(
                    json.dumps({"t": time.time(), "err": type(exc).__name__}) + "\n"
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def write_state(self) -> None:
        while not self.stop.is_set():
            loadlib.write_json(
                "recv-state.json",
                {
                    "unique": self.received.count,
                    "deliveries": self.deliveries,
                    "duplicates": self.duplicates,
                    "inversions": self.inversions,
                    "max_seq": self.max_seq,
                    "t": time.time(),
                },
            )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await self.connect()
        js_hub = await self.setup_mirror()

        async def handler(msg):
            now = time.time()
            try:
                seq = json.loads(msg.data)["seq"]
            except Exception:  # noqa: BLE001
                await msg.ack()
                return
            self.deliveries += 1
            if not self.received.add(seq):
                self.duplicates += 1
            if seq < self.max_seq:
                self.inversions += 1
            else:
                self.max_seq = seq
            if self.first_t is None:
                self.first_t = now
            self.last_t = now
            await msg.ack()

        await js_hub.subscribe(
            SUBJECT_FILTER,
            cb=handler,
            stream=HUB_STREAM,
            durable="ingest",
            manual_ack=True,
            config=js_api.ConsumerConfig(
                deliver_policy=js_api.DeliverPolicy.ALL,
                ack_policy=js_api.AckPolicy.EXPLICIT,
                max_ack_pending=20000,
            ),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        tasks = [
            asyncio.create_task(self.poll_mirror(js_hub)),
            asyncio.create_task(self.write_state()),
        ]
        log("READY")
        while not self.stop.is_set():
            await asyncio.sleep(5)
            log(f"unique {self.received.count} deliveries {self.deliveries} max_seq {self.max_seq}")
        for t in tasks:
            t.cancel()
        loadlib.write_json(
            "recv-final.json",
            {
                "unique": self.received.count,
                "deliveries": self.deliveries,
                "duplicates": self.duplicates,
                "inversions": self.inversions,
                "max_seq": self.max_seq,
                "first_t": self.first_t,
                "last_t": self.last_t,
                "gaps": self.received.gaps(max(self.max_seq, 1)),
            },
        )
        log(f"final: unique {self.received.count}, dups {self.duplicates}")
        await self.nc.close()


if __name__ == "__main__":
    asyncio.run(LoadConsumer().run())
