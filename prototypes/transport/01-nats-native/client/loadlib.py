"""Shared helpers for the load round (S1-S4).

The outage round wrote one JSON line per frame. At 518 400 frames that file
becomes the bottleneck of the experiment itself -- the thing being measured
would be Python's write throughput, not the broker's. So the load clients keep
a bit per sequence number and emit ranges at the end. 518 400 bits is 64 KB.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))


class SeqBitmap:
    """A set of sequence numbers, one bit each. seq is 1-based."""

    __slots__ = ("_bits", "_capacity", "count")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._bits = bytearray((capacity >> 3) + 2)
        self.count = 0

    def add(self, seq: int) -> bool:
        """Record `seq`. Returns False if it was already present (a duplicate)."""
        if seq < 1 or seq > self._capacity:
            return False
        byte, bit = seq >> 3, seq & 7
        if self._bits[byte] >> bit & 1:
            return False
        self._bits[byte] |= 1 << bit
        self.count += 1
        return True

    def __contains__(self, seq: int) -> bool:
        if seq < 1 or seq > self._capacity:
            return False
        return bool(self._bits[seq >> 3] >> (seq & 7) & 1)

    def ranges(self, upto: int) -> list[list[int]]:
        """Present sequences as inclusive [start, end] ranges."""
        out: list[list[int]] = []
        bits = self._bits
        start = -1
        for seq in range(1, upto + 1):
            present = bits[seq >> 3] >> (seq & 7) & 1
            if present and start < 0:
                start = seq
            elif not present and start >= 0:
                out.append([start, seq - 1])
                start = -1
        if start >= 0:
            out.append([start, upto])
        return out

    def gaps(self, upto: int) -> list[list[int]]:
        """The complement of ranges(): what never arrived."""
        out: list[list[int]] = []
        bits = self._bits
        start = -1
        for seq in range(1, upto + 1):
            present = bits[seq >> 3] >> (seq & 7) & 1
            if not present and start < 0:
                start = seq
            elif present and start >= 0:
                out.append([start, seq - 1])
                start = -1
        if start >= 0:
            out.append([start, upto])
        return out


def write_json(name: str, payload: dict) -> None:
    """Atomic write, so a reader never sees a half-written state file."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    target = RESULTS / name
    fd, tmp = tempfile.mkstemp(dir=str(RESULTS), prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, target)


def read_json(name: str, default: dict | None = None) -> dict:
    try:
        with open(RESULTS / name) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default if default is not None else {}


def read_phase(current: str) -> str:
    try:
        return (RESULTS / "phase").read_text().strip() or current
    except FileNotFoundError:
        return current


def device_ids(n: int) -> list[str]:
    """n synthetic ESP32 MACs, one per drainage table."""
    return [f"A4:CF:12:34:56:{i:02X}" for i in range(1, n + 1)]
