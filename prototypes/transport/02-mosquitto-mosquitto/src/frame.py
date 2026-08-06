"""The real ESP32 -> Raspberry Pi frame, plus an experiment-only `seq` field.

Shape taken from docs/trama-de-datos-riego.md and from the sandbox script
scripts/publish_fake_reading.sh on research/mqtt-timescaledb-bento-sandbox, so
the payload size is representative (queue caps in Mosquitto are counted in
messages, but max_queued_bytes is counted in bytes and we want the byte figure
to be honest too).

`seq` does NOT exist in the real protocol. It is a monotonic counter that makes
loss, duplication and reordering exactly measurable.
"""

import json
import math
import time

DEV_ID = "A4:CF:12:34:56:78"
TOPIC = f"fertloops/{DEV_ID}/reading"


def build(seq: int) -> bytes:
    """Build frame number `seq`. Values wobble so payloads are not identical."""
    phase = seq / 40.0
    frame = {
        "devID": DEV_ID,
        "Timestamp": time.strftime("%d/%m/%Y %H:%M:%S"),
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
