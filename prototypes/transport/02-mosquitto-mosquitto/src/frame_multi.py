"""The real ESP32 frame, parameterised by device.

Identical in shape to frame.py (round one) but the devID is a parameter, since
the load round models twelve drainage tables instead of one. Byte size is
unchanged at ~347 B, which is what makes the two rounds comparable.

`seq` does NOT exist in the real protocol. It is a monotonic counter that makes
loss, duplication and reordering exactly measurable.
"""

import json
import math
import time

_TS_CACHE = {"at": 0.0, "value": ""}


def _timestamp() -> str:
    # strftime on every frame is ~15 % of the publisher's CPU at fill rate, and
    # the field only has one-second resolution anyway.
    now = time.time()
    if now - _TS_CACHE["at"] >= 0.5:
        _TS_CACHE["at"] = now
        _TS_CACHE["value"] = time.strftime("%d/%m/%Y %H:%M:%S")
    return _TS_CACHE["value"]


def build(seq: int, dev_id: str) -> bytes:
    """Build frame `seq` for `dev_id`. Values wobble so payloads differ."""
    phase = seq / 40.0
    frame = {
        "devID": dev_id,
        "Timestamp": _timestamp(),
        # Experiment-only field.
        "seq": seq,
        "Control": {
            "Sample_per_minute": 6,
            "Inv": {"On": 1, "Freq": 50},
            "Restart": 0,
            "Valve": 56,
        },
        "Data": {
            "pH": round(6.8 + 0.2 * math.sin(phase), 2),
            "CE": round(1850.3 + 25.0 * math.sin(phase / 3), 2),
            "Solar": round(645.2 + 80.0 * math.sin(phase / 7), 2),
            "Volume": 20,
            "THC": {
                "T": round(22.55 + 0.5 * math.sin(phase / 5), 2),
                "H": round(57.89 + 1.5 * math.sin(phase / 4), 2),
                "C": 1.85,
            },
            "TH": {
                "T": round(22.23 + 0.5 * math.sin(phase / 6), 2),
                "H": round(64.25 + 2.0 * math.sin(phase / 8), 2),
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
