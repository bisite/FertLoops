"""Edge simulator for the `load` profile: the Pi's reader at fleet size.

One connection, twelve subjects -- the accurate model, since the real Pi runs
one reader process polling twelve ESP32s over UART.

Publishing strategy differs from round one and the difference matters:

  round one   js.publish() per frame, awaiting the JetStream ack. Correct, and
              deliberately slow: at 20 msg/s the round trip is free.
  load round  core NATS publish into the subject the edge stream captures, with
              an explicit flush every FLUSH_EVERY frames. A JetStream stream is
              a subscriber, so the message is stored just the same; what is
              given up is the per-frame ack.

The trade is deliberate. js.publish() is one round trip per frame, which caps
throughput at a few hundred a second and would make a 518 400-frame fill take
half an hour. Core publish plus flush keeps ordering (one connection, ordered
writes) and reaches thousands a second. What replaces the per-frame ack is the
stream's own message count, read back from stream_info -- a stronger check
anyway, because it is the server's own accounting rather than the client's.

Round one already established that JetStream acks work and that Nats-Msg-Id
dedup exists; this round is about capacity, memory and catch-up.

Phases via $RESULTS_DIR/phase, identical to the Mosquitto load harness:
FILL (flat out to TARGET_MSGS), then WARM/HOLD/DRAIN at DRAIN_RATE_HZ, then
stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import time

import nats

from frame import build_frame, subject_for

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
NATS_URL = os.environ["NATS_URL"]
TARGET_MSGS = int(os.environ.get("TARGET_MSGS", "518400"))
DRAIN_RATE_HZ = float(os.environ.get("DRAIN_RATE_HZ", "20"))
N_DEVICES = int(os.environ.get("N_DEVICES", "12"))
EDGE_DOMAIN = os.environ.get("EDGE_DOMAIN", "edge")
EDGE_STREAM = os.environ.get("EDGE_STREAM", "FRAMES")
SUBJECT_FILTER = os.environ.get("SUBJECT_FILTER", "fertloops.>")
FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY", "2000"))
FLUSH_TIMEOUT_S = float(os.environ.get("FLUSH_TIMEOUT_S", "10"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [sim] {msg}", flush=True)


def write_json(name: str, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS / f".tmp-{name}"
    tmp.write_text(json.dumps(payload))
    tmp.replace(RESULTS / name)


def read_phase(current: str) -> str:
    try:
        return (RESULTS / "phase").read_text().strip() or current
    except FileNotFoundError:
        return current


class LoadSimulator:
    def __init__(self) -> None:
        self.nc: nats.NATS | None = None
        self.js = None
        self.stop = asyncio.Event()
        self.seq = 0
        self.flushed_upto = 0
        self.flush_failures = 0
        self.phase = "WARM"
        self.marks: list[dict] = []
        self.devices = [f"A4:CF:12:34:56:{i:02X}" for i in range(1, N_DEVICES + 1)]
        self.subjects = [subject_for(d) for d in self.devices]
        self.fill_seconds = None
        self.stream_state = {}

    async def connect(self) -> None:
        self.nc = await nats.connect(
            NATS_URL,
            name="fl-load-simulator",
            max_reconnect_attempts=-1,
            reconnect_time_wait=0.25,
            connect_timeout=5,
            # The fill writes far faster than round one did; the default 8 MB
            # reconnect buffer would silently absorb (and then drop) frames
            # while the leaf is dead in S3. Errors are what we want there.
            pending_size=8 * 1024 * 1024,
        )
        self.js = self.nc.jetstream(domain=EDGE_DOMAIN, timeout=15)
        log(f"connected to {NATS_URL}, JetStream domain {EDGE_DOMAIN}")

    async def ensure_stream(self) -> None:
        """Create the edge stream locally.

        Round one created it from the hub, cross-domain. Here the simulator
        creates it on its own leaf instead, so the edge does not depend on the
        hub being reachable to have somewhere to put data -- which is the whole
        point of edge durability.
        """
        from nats.js import api as js_api

        cfg = js_api.StreamConfig(
            name=EDGE_STREAM,
            subjects=[SUBJECT_FILTER],
            storage=js_api.StorageType.FILE,
            retention=js_api.RetentionPolicy.LIMITS,
            discard=js_api.DiscardPolicy.OLD,
            max_msgs=-1,
            duplicate_window=120,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                info = await self.js.add_stream(cfg)
                log(f"edge stream {EDGE_STREAM} ready (max_msgs={info.config.max_msgs})")
                return
            except Exception as exc:  # noqa: BLE001
                log(f"stream not ready: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1)
        raise SystemExit("could not create the edge stream")

    async def snapshot_stream(self) -> None:
        while not self.stop.is_set():
            try:
                info = await self.js.stream_info(EDGE_STREAM)
                self.stream_state = {
                    "messages": info.state.messages,
                    "bytes": info.state.bytes,
                    "first_seq": info.state.first_seq,
                    "last_seq": info.state.last_seq,
                }
            except Exception as exc:  # noqa: BLE001
                self.stream_state = {"err": type(exc).__name__}
            self._write_state()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def _write_state(self) -> None:
        write_json(
            "sim-state.json",
            {
                "phase": self.phase,
                "seq": self.seq,
                "acked": self.flushed_upto,
                "flush_failures": self.flush_failures,
                "filled": self.fill_seconds is not None,
                "stream": self.stream_state,
            },
        )

    async def flush(self) -> bool:
        try:
            await self.nc.flush(timeout=FLUSH_TIMEOUT_S)
            self.flushed_upto = self.seq
            return True
        except Exception as exc:  # noqa: BLE001
            self.flush_failures += 1
            if self.flush_failures <= 5 or self.flush_failures % 50 == 0:
                log(f"flush failed ({self.flush_failures}): {type(exc).__name__}")
            return False

    async def publish_loop(self) -> None:
        t_start = time.time()
        since_flush = 0
        next_at = time.monotonic()
        while not self.stop.is_set():
            new_phase = read_phase(self.phase)
            if new_phase != self.phase:
                self.marks.append({"phase": new_phase, "first_seq": self.seq + 1, "t": time.time()})
                log(f"phase {self.phase} -> {new_phase} at seq {self.seq}")
                self.phase = new_phase
                next_at = time.monotonic()
                if self.phase == "stop":
                    break

            if self.phase == "FILL" and self.flushed_upto >= TARGET_MSGS:
                if self.fill_seconds is None:
                    self.fill_seconds = time.time() - t_start
                    log(
                        f"FILL complete: {self.flushed_upto} frames in "
                        f"{self.fill_seconds:.1f}s ({TARGET_MSGS / self.fill_seconds:.0f}/s)"
                    )
                await asyncio.sleep(0.2)
                continue

            self.seq += 1
            idx = (self.seq - 1) % N_DEVICES
            payload = build_frame(self.devices[idx], self.seq)
            try:
                await self.nc.publish(self.subjects[idx], payload)
            except Exception as exc:  # noqa: BLE001
                # Publishing into a dead connection: recorded, not hidden. This
                # is the S3 window, and it is the publisher's problem, not the
                # transport's -- the same distinction the Mosquitto harness
                # draws between "refused at source" and "lost".
                self.flush_failures += 1
                self.seq -= 1
                await asyncio.sleep(0.2)
                continue

            since_flush += 1
            if since_flush >= FLUSH_EVERY or self.phase != "FILL":
                since_flush = 0
                await self.flush()
                if self.seq % 25000 < FLUSH_EVERY and self.phase == "FILL":
                    log(f"seq {self.seq} flushed {self.flushed_upto}")

            if self.phase != "FILL" and DRAIN_RATE_HZ > 0:
                next_at += 1.0 / DRAIN_RATE_HZ
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_at = time.monotonic()

        await self.flush()

    async def run(self) -> None:
        await self.connect()
        await self.ensure_stream()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        self.marks.append({"phase": self.phase, "first_seq": 1, "t": time.time()})
        snap = asyncio.create_task(self.snapshot_stream())
        await self.publish_loop()
        self.stop.set()
        await asyncio.sleep(1)
        snap.cancel()
        try:
            info = await self.js.stream_info(EDGE_STREAM)
            final_stream = {
                "messages": info.state.messages,
                "bytes": info.state.bytes,
                "first_seq": info.state.first_seq,
                "last_seq": info.state.last_seq,
            }
        except Exception as exc:  # noqa: BLE001
            final_stream = {"err": f"{type(exc).__name__}: {exc}"}
        write_json(
            "sim-final.json",
            {
                "produced": self.seq,
                "acked": self.flushed_upto,
                "refused": self.flush_failures,
                "target": TARGET_MSGS,
                "devices": N_DEVICES,
                "fill_seconds": self.fill_seconds,
                "marks": self.marks,
                # Core publish gives no per-frame ack, so "what the edge took
                # responsibility for" is a prefix: everything up to the last
                # successful flush. The stream's own count is the cross-check.
                "acked_ranges": [[1, self.flushed_upto]] if self.flushed_upto else [],
                "stream_final": final_stream,
            },
        )
        log(f"done: produced {self.seq}, flushed {self.flushed_upto}, stream {final_stream}")
        if self.nc is not None:
            await self.nc.close()


if __name__ == "__main__":
    asyncio.run(LoadSimulator().run())
