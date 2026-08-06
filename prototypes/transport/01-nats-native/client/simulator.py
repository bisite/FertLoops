"""Edge simulator: stands in for the Raspberry Pi agent reading the ESP32.

Generates one frame every 1/RATE_HZ seconds with a strictly monotonic `seq`,
hands it to an in-process outbox queue, and a single worker publishes them to
the leaf node in order.

The producer/worker split exists so the generation cadence stays exactly at
RATE_HZ even when a publish blocks: a real agent keeps reading the UART while
the broker is unreachable, and the number of frames generated must not depend
on how slow the transport happens to be. Otherwise the "published" total would
silently shrink whenever the transport misbehaves, which is precisely the thing
being measured.

Per profile:
  naive            core NATS publish, then an explicit flush so we can record
                   whether the publisher gets ANY signal at all.
  jetstream-mirror JetStream publish against the `edge` domain, with
                   Nats-Msg-Id for dedup, retried in order until acked.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import nats
from nats.errors import Error as NatsError

import common
from common import log
from frame import build_frame, subject_for

NATS_URL = os.environ["NATS_URL"]
DEV_ID = os.environ.get("DEV_ID", "A4:CF:12:34:56:78")
RATE_HZ = float(os.environ.get("RATE_HZ", "20"))
EDGE_DOMAIN = os.environ.get("EDGE_DOMAIN", "edge")
EDGE_STREAM = os.environ.get("EDGE_STREAM", "FRAMES")

# JetStream publish ack timeout. Short on purpose: during the P2 window the leaf
# is gone and we want the failure recorded quickly, not after a 5 s default.
PUBLISH_TIMEOUT_S = 0.5
RETRY_SLEEP_S = 0.2
# Give up on a single frame after this long. Only a safety valve; the longest
# outage in the protocol is the ~6 s the leaf takes to be killed and restarted.
PER_FRAME_BUDGET_S = 45.0

# The naive profile does a flush per frame to probe for feedback. Same timeout
# so the two profiles react on the same timescale.
FLUSH_TIMEOUT_S = 0.5


class Simulator:
    def __init__(self) -> None:
        self.profile = common.profile()
        self.subject = subject_for(DEV_ID)
        self.queue: asyncio.Queue[tuple[int, float, bytes]] = asyncio.Queue()
        self.published = common.open_jsonl("published.jsonl")
        self.events = common.open_jsonl("publisher-events.jsonl")
        self.stream_state = common.open_jsonl("edge-stream-state.jsonl")
        self.stop = asyncio.Event()
        self.nc: nats.NATS | None = None
        self.js = None
        self.generated = 0
        self.acked = 0
        self.failed = 0
        self.attempt_errors = 0

    # -- connection callbacks: these are the publisher's only out-of-band
    # -- notification channel, so every one of them is recorded.
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
            name="fl-simulator",
            max_reconnect_attempts=-1,
            reconnect_time_wait=0.25,
            connect_timeout=3,
            allow_reconnect=True,
            error_cb=on_error,
            disconnected_cb=on_disconnected,
            reconnected_cb=on_reconnected,
            closed_cb=on_closed,
        )
        self._event("connected", NATS_URL)
        if self.profile == common.PROFILE_JETSTREAM:
            # Address the leaf's own JetStream explicitly by domain. Without the
            # domain the request would go to the unqualified `$JS.API`, which is
            # ambiguous the moment two JetStream systems share an account.
            self.js = self.nc.jetstream(domain=EDGE_DOMAIN, timeout=5)
            await self._wait_for_stream()

    async def _wait_for_stream(self) -> None:
        """The hub creates the edge stream cross-domain; wait until it exists."""
        deadline = time.monotonic() + 60
        last: object = None
        while time.monotonic() < deadline:
            try:
                info = await self.js.stream_info(EDGE_STREAM)
                log(f"edge stream {EDGE_STREAM} ready: {info.config.max_msgs=} {info.config.discard=}")
                return
            except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
                await asyncio.sleep(0.5)
                last = exc
        raise SystemExit(f"edge stream {EDGE_STREAM} never appeared: {last!r}")

    # -- producer: strict cadence, never blocked by the transport
    async def produce(self) -> None:
        period = 1.0 / RATE_HZ
        start = time.monotonic()
        seq = 0
        while not self.stop.is_set():
            seq += 1
            now = time.time()
            self.queue.put_nowait((seq, now, build_frame(DEV_ID, seq, now)))
            self.generated = seq
            target = start + seq * period
            delay = target - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    # -- worker: publishes in order, one frame at a time
    async def publish_loop(self) -> None:
        while True:
            if self.stop.is_set() and self.queue.empty():
                return
            try:
                seq, t_gen, payload = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self.profile == common.PROFILE_NAIVE:
                await self._publish_core(seq, t_gen, payload)
            else:
                await self._publish_jetstream(seq, t_gen, payload)

    async def _publish_core(self, seq: int, t_gen: float, payload: bytes) -> None:
        """Core NATS: publish, then flush, and record whatever we learn.

        A flush is a PING/PONG round trip to the leaf. If it returns cleanly the
        leaf has acknowledged every byte we wrote -- which is the strongest
        signal core NATS can give a publisher, and, as the results show, says
        nothing whatsoever about the message reaching the hub.
        """
        record = {"seq": seq, "t_gen": t_gen, "attempts": 1}
        try:
            await self.nc.publish(self.subject, payload)
            await self.nc.flush(timeout=FLUSH_TIMEOUT_S)
            record["status"] = "sent"
            record["t_done"] = time.time()
            record["connected"] = self.nc.is_connected
            self.acked += 1
        except Exception as exc:  # noqa: BLE001
            record["status"] = "failed"
            record["err"] = type(exc).__name__
            record["t_done"] = time.time()
            record["connected"] = self.nc.is_connected
            self.failed += 1
            self.attempt_errors += 1
        common.write_record(self.published, record)

    async def _publish_jetstream(self, seq: int, t_gen: float, payload: bytes) -> None:
        """JetStream: retry the same frame, same Nats-Msg-Id, until acked."""
        msg_id = f"{DEV_ID}-{seq}"
        headers = {"Nats-Msg-Id": msg_id}
        deadline = time.monotonic() + PER_FRAME_BUDGET_S
        attempts = 0
        last_err = ""
        while True:
            attempts += 1
            try:
                ack = await self.js.publish(
                    self.subject, payload, timeout=PUBLISH_TIMEOUT_S, headers=headers
                )
                common.write_record(
                    self.published,
                    {
                        "seq": seq,
                        "t_gen": t_gen,
                        "t_done": time.time(),
                        "status": "acked",
                        "attempts": attempts,
                        "stream_seq": ack.seq,
                        "duplicate": bool(getattr(ack, "duplicate", False)),
                        "connected": self.nc.is_connected,
                    },
                )
                self.acked += 1
                return
            except (NatsError, asyncio.TimeoutError, OSError) as exc:
                self.attempt_errors += 1
                last_err = type(exc).__name__
                if attempts == 1 or attempts % 10 == 0:
                    self._event("publish_error", f"seq={seq} attempt={attempts} err={last_err}")
                if time.monotonic() > deadline:
                    common.write_record(
                        self.published,
                        {
                            "seq": seq,
                            "t_gen": t_gen,
                            "t_done": time.time(),
                            "status": "failed",
                            "attempts": attempts,
                            "err": last_err,
                            "connected": self.nc.is_connected,
                        },
                    )
                    self.failed += 1
                    return
                await asyncio.sleep(RETRY_SLEEP_S)

    # -- observability
    async def poll_stream_state(self) -> None:
        """Snapshot the edge stream once a second.

        This is the direct evidence for the overflow question: with a capped
        stream you can watch first_seq climb while `messages` stays pinned at
        the cap, which tells you which end of the queue is being dropped
        without having to infer it from the gaps.
        """
        if self.profile != common.PROFILE_JETSTREAM:
            return
        while not self.stop.is_set():
            try:
                info = await self.js.stream_info(EDGE_STREAM)
                common.write_record(
                    self.stream_state,
                    {
                        "t": time.time(),
                        "messages": info.state.messages,
                        "bytes": info.state.bytes,
                        "first_seq": info.state.first_seq,
                        "last_seq": info.state.last_seq,
                        "consumer_count": info.state.consumer_count,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                common.write_record(
                    self.stream_state, {"t": time.time(), "err": type(exc).__name__}
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def report(self) -> None:
        while not self.stop.is_set():
            log(
                f"PROGRESS generated={self.generated} acked={self.acked} "
                f"failed={self.failed} attempt_errors={self.attempt_errors} "
                f"outbox={self.queue.qsize()}"
            )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await self.connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        log(f"PUBLISHING profile={self.profile} subject={self.subject} rate={RATE_HZ}Hz")
        tasks = [
            asyncio.create_task(self.produce()),
            asyncio.create_task(self.publish_loop()),
            asyncio.create_task(self.poll_stream_state()),
            asyncio.create_task(self.report()),
        ]
        await self.stop.wait()
        # Give the worker a moment to drain the outbox so the tail of the run is
        # not reported as transport loss when it was really a shutdown race.
        await asyncio.sleep(2)
        for task in tasks:
            task.cancel()
        log(
            f"FINAL generated={self.generated} acked={self.acked} failed={self.failed} "
            f"attempt_errors={self.attempt_errors} outbox_left={self.queue.qsize()}"
        )
        common.write_record(
            self.events,
            {
                "t": time.time(),
                "kind": "summary",
                "generated": self.generated,
                "acked": self.acked,
                "failed": self.failed,
                "attempt_errors": self.attempt_errors,
                "outbox_left": self.queue.qsize(),
            },
        )
        if self.nc is not None:
            await self.nc.drain()


if __name__ == "__main__":
    asyncio.run(Simulator().run())
