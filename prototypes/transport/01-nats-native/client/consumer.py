"""Hub consumer: stands in for the ingest process on the VPS.

Also does the hub-side setup, because in the jetstream-mirror profile the hub is
the only place from which both JetStream domains are reachable:

  * it creates the edge stream FRAMES on the leaf, cross-domain, by addressing
    `$JS.edge.API` over the leafnode link -- which is in itself a check that
    cross-domain JetStream administration works through a leaf connection;
  * it creates FRAMES_MIRROR locally in the `hub` domain as a mirror of FRAMES.

Every frame that arrives is appended to received.jsonl with its arrival order
and arrival time, which is what lets the verifier measure reordering rather
than just loss.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time

import nats
from nats.js import api as js_api

import common
from common import log

NATS_URL = os.environ["NATS_URL"]
SUBJECT_FILTER = os.environ.get("SUBJECT_FILTER", "fertloops.>")
EDGE_DOMAIN = os.environ.get("EDGE_DOMAIN", "edge")
HUB_DOMAIN = os.environ.get("HUB_DOMAIN", "hub")
EDGE_STREAM = os.environ.get("EDGE_STREAM", "FRAMES")
HUB_STREAM = os.environ.get("HUB_STREAM", "FRAMES_MIRROR")
EDGE_MAX_MSGS = int(os.environ.get("EDGE_MAX_MSGS", "-1"))


class Consumer:
    def __init__(self) -> None:
        self.profile = common.profile()
        self.received = common.open_jsonl("received.jsonl")
        self.events = common.open_jsonl("consumer-events.jsonl")
        self.mirror_state = common.open_jsonl("mirror-state.jsonl")
        self.stop = asyncio.Event()
        self.nc: nats.NATS | None = None
        self.count = 0

    def _event(self, kind: str, detail: str = "") -> None:
        common.write_record(self.events, {"t": time.time(), "kind": kind, "detail": detail})
        log(f"connection event: {kind} {detail}")

    async def connect(self) -> None:
        # nats-py requires the callbacks to be coroutine functions.
        async def on_error(exc):
            self._event("error", repr(exc))

        async def on_disconnected():
            self._event("disconnected")

        async def on_reconnected():
            self._event("reconnected")

        async def on_closed():
            self._event("closed")

        self.nc = await nats.connect(
            NATS_URL,
            name="fl-consumer",
            max_reconnect_attempts=-1,
            reconnect_time_wait=0.25,
            connect_timeout=3,
            error_cb=on_error,
            disconnected_cb=on_disconnected,
            reconnected_cb=on_reconnected,
            closed_cb=on_closed,
        )
        self._event("connected", NATS_URL)

    def _on_frame(self, subject: str, data: bytes, stream_seq: int | None) -> None:
        self.count += 1
        try:
            seq = json.loads(data)["seq"]
        except Exception:  # noqa: BLE001 - a malformed frame is still an arrival
            seq = None
        common.write_record(
            self.received,
            {
                "order": self.count,
                "t_recv": time.time(),
                "seq": seq,
                "subject": subject,
                "stream_seq": stream_seq,
            },
        )

    # -- naive profile: plain core subscription, no durability anywhere
    async def run_naive(self) -> None:
        async def handler(msg):
            self._on_frame(msg.subject, msg.data, None)

        await self.nc.subscribe(SUBJECT_FILTER, cb=handler)
        await self.nc.flush(timeout=5)
        log(f"READY profile={self.profile} core subscription on {SUBJECT_FILTER}")
        await self.stop.wait()

    # -- jetstream-mirror profile
    async def setup_streams(self):
        js_edge = self.nc.jetstream(domain=EDGE_DOMAIN, timeout=10)
        js_hub = self.nc.jetstream(domain=HUB_DOMAIN, timeout=10)

        # The edge stream: file storage so it survives the SIGKILL in P2, and a
        # message cap so overflow behaviour is observable inside a 60 s outage.
        # discard=old is the JetStream default and is stated explicitly here
        # because which end gets dropped is one of the things being measured.
        edge_cfg = js_api.StreamConfig(
            name=EDGE_STREAM,
            subjects=[SUBJECT_FILTER],
            storage=js_api.StorageType.FILE,
            retention=js_api.RetentionPolicy.LIMITS,
            discard=js_api.DiscardPolicy.OLD,
            max_msgs=EDGE_MAX_MSGS,
            # Seconds, not nanoseconds: nats-py multiplies this by 1e9 itself,
            # and the server rejects the request outright if you pre-convert.
            # 2 minutes of dedup history is plenty, since the publisher retries
            # the same Nats-Msg-Id for at most PER_FRAME_BUDGET_S.
            duplicate_window=120,
        )
        await self._ensure_stream(js_edge, edge_cfg, f"{EDGE_DOMAIN}/{EDGE_STREAM}")

        # The hub mirror. `external.api` is what makes this cross-domain: the
        # hub sends its mirror requests to $JS.edge.API instead of its own
        # $JS.API, and those travel down the leafnode connection.
        hub_cfg = js_api.StreamConfig(
            name=HUB_STREAM,
            storage=js_api.StorageType.FILE,
            retention=js_api.RetentionPolicy.LIMITS,
            max_msgs=-1,
            mirror=js_api.StreamSource(
                name=EDGE_STREAM,
                external=js_api.ExternalStream(api=f"$JS.{EDGE_DOMAIN}.API"),
            ),
        )
        await self._ensure_stream(js_hub, hub_cfg, f"{HUB_DOMAIN}/{HUB_STREAM}")
        return js_hub

    async def _ensure_stream(self, js, config, label: str) -> None:
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            try:
                info = await js.add_stream(config)
                log(f"stream ready {label}: max_msgs={info.config.max_msgs} discard={info.config.discard}")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                # str() carries the server's description; repr() does not, and
                # the JetStream errors that matter here are all in the text.
                log(f"stream {label} not ready yet: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1.0)
        raise SystemExit(f"could not create stream {label}: {type(last).__name__}: {last}")

    async def poll_mirror(self, js_hub) -> None:
        """Snapshot the mirror's own view once a second.

        `sources`/`mirror` info carries `lag` and `active`, i.e. how far behind
        the mirror is and how long since it last heard from the source. That is
        the only place the stall during the outage is visible as a number.
        """
        while not self.stop.is_set():
            try:
                info = await js_hub.stream_info(HUB_STREAM)
                mirror = info.mirror
                common.write_record(
                    self.mirror_state,
                    {
                        "t": time.time(),
                        "messages": info.state.messages,
                        "first_seq": info.state.first_seq,
                        "last_seq": info.state.last_seq,
                        "mirror_lag": getattr(mirror, "lag", None) if mirror else None,
                        "mirror_active_ns": getattr(mirror, "active", None) if mirror else None,
                        "mirror_error": getattr(mirror, "error", None) if mirror else None,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                common.write_record(
                    self.mirror_state, {"t": time.time(), "err": type(exc).__name__}
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def run_jetstream(self) -> None:
        js_hub = await self.setup_streams()

        async def handler(msg):
            self._on_frame(msg.subject, msg.data, msg.metadata.sequence.stream)
            await msg.ack()

        # Durable push consumer bound explicitly to the mirror stream: a mirror
        # declares no subjects of its own, so subject-based stream lookup cannot
        # find it and the stream name has to be given.
        await js_hub.subscribe(
            SUBJECT_FILTER,
            cb=handler,
            stream=HUB_STREAM,
            durable="ingest",
            manual_ack=True,
            config=js_api.ConsumerConfig(
                deliver_policy=js_api.DeliverPolicy.ALL,
                ack_policy=js_api.AckPolicy.EXPLICIT,
                max_ack_pending=2000,
            ),
        )
        asyncio.create_task(self.poll_mirror(js_hub))
        log(f"READY profile={self.profile} durable consumer on {HUB_DOMAIN}/{HUB_STREAM}")
        await self.stop.wait()

    async def report(self) -> None:
        while not self.stop.is_set():
            log(f"PROGRESS received={self.count}")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await self.connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        asyncio.create_task(self.report())
        if self.profile == common.PROFILE_NAIVE:
            await self.run_naive()
        else:
            await self.run_jetstream()
        log(f"FINAL received={self.count}")
        await self.nc.drain()


if __name__ == "__main__":
    asyncio.run(Consumer().run())
