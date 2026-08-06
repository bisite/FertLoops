"""Bits shared by the simulator, the consumer and the verifier."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any, TextIO

PROFILE_NAIVE = "naive"
PROFILE_JETSTREAM = "jetstream-mirror"


def results_dir() -> pathlib.Path:
    path = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile() -> str:
    value = os.environ.get("PROFILE", "")
    if value not in (PROFILE_NAIVE, PROFILE_JETSTREAM):
        raise SystemExit(f"PROFILE must be {PROFILE_NAIVE!r} or {PROFILE_JETSTREAM!r}, got {value!r}")
    return value


def open_jsonl(name: str) -> TextIO:
    """Open a results file for append, line buffered.

    Line buffering matters: the simulator and the leaf broker get SIGKILLed
    during the experiment, and anything still sitting in a Python buffer would
    be lost and would show up as a fake transport gap.
    """
    return (results_dir() / name).open("a", buffering=1, encoding="utf-8")


def write_record(handle: TextIO, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def read_jsonl(name: str) -> list[dict[str, Any]]:
    path = results_dir() / name
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn last line can only happen if the writer was killed
                # mid-write; skip it rather than abort the whole verification.
                continue
    return records


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stdout, flush=True)
