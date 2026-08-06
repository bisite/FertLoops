"""Builds the ESP32 UART reading frame used as the experiment payload.

Shape comes from docs/trama-de-datos-riego.md and from the payload in
scripts/publish_fake_reading.sh on the research/mqtt-timescaledb-bento-sandbox
branch, so the message size measured here matches a real reading.

The only addition is the top-level `seq` field: a monotonic counter that exists
purely so the verifier can measure loss, duplication and reordering exactly.
It is NOT part of the real protocol.
"""

from __future__ import annotations

import json
import math
import time


def build_frame(dev_id: str, seq: int, now: float | None = None) -> bytes:
    """Return one reading frame as JSON bytes, with `seq` stamped on it."""
    now = time.time() if now is None else now
    # Timestamp in the RTC format the ESP32 emits: "DD/MM/YYYY HH:MM:SS".
    stamp = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(now))

    # Slow sinusoids so the payload is not byte-identical every time; the
    # amplitudes stay inside the ranges documented for each sensor.
    phase = seq / 120.0
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
            "pH": round(6.8 + 0.4 * math.sin(phase), 2),
            "CE": round(1850.3 + 120.0 * math.sin(phase / 3), 2),
            "Solar": round(645.2 + 200.0 * math.sin(phase / 7), 2),
            "Volume": 20,
            "THC": {
                "T": round(22.55 + 1.5 * math.sin(phase / 5), 2),
                "H": round(57.89 + 3.0 * math.sin(phase / 4), 2),
                "C": round(1.85 + 0.15 * math.sin(phase / 6), 2),
            },
            "TH": {
                "T": round(22.23 + 1.2 * math.sin(phase / 5), 2),
                "H": round(64.25 + 4.0 * math.sin(phase / 4), 2),
            },
            "Errors": {
                "ADC": 0,
                "Pulses": 0,
                "I2C": 0,
                "Inverter": 0,
                "Inverter_State": 1,
            },
        },
    }
    return json.dumps(frame, separators=(",", ":")).encode()


def subject_for(dev_id: str) -> str:
    """NATS subject for a device's readings.

    MQTT topics use `/`; NATS subjects use `.`, and `:` is legal in a token but
    confusing, so the MAC separators are dropped.
    """
    return f"fertloops.{dev_id.replace(':', '')}.reading"
