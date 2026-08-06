"""Builds the ESP32 UART frame the Raspberry Pi gateway would republish.

Shape and field semantics come from docs/trama-de-datos-riego.md and from the
payload in scripts/publish_fake_reading.sh on branch
research/mqtt-timescaledb-bento-sandbox.

The only addition is `seq`: a monotonic counter used to measure loss, duplicates
and reordering exactly. It does NOT exist in the real protocol.
"""

from __future__ import annotations

import json
import math
import time


def build_frame(dev_id: str, seq: int, now: float | None = None) -> bytes:
    """Return one reading frame as UTF-8 JSON bytes.

    Sensor values wobble deterministically with `seq` so the payload is not a
    constant string -- a broker that deduplicated identical payloads would
    otherwise flatter itself.
    """
    now = time.time() if now is None else now
    stamp = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(now))
    wobble = math.sin(seq / 17.0)

    frame = {
        "seq": seq,
        "devID": dev_id,
        "Timestamp": stamp,
        "Control": {
            "Sample_per_minute": 6,
            "Inv": {"On": 1, "Freq": 50},
            "Restart": 0,
            "Valve": 56,
        },
        "Data": {
            "pH": round(6.8 + 0.2 * wobble, 3),
            "CE": round(1850.3 + 40.0 * wobble, 2),
            "Solar": round(645.2 + 60.0 * wobble, 2),
            "Volume": 20,
            "THC": {
                "T": round(22.55 + wobble, 2),
                "H": round(57.89 + 2.0 * wobble, 2),
                "C": round(1.85 + 0.05 * wobble, 3),
            },
            "TH": {"T": round(22.23 + wobble, 2), "H": round(64.25 + 2.0 * wobble, 2)},
            "Errors": {
                "ADC": 0,
                "Pulses": 0,
                "I2C": 0,
                "Inverter": 0,
                "Inverter_State": 1,
            },
        },
    }
    return json.dumps(frame, separators=(",", ":")).encode("utf-8")
