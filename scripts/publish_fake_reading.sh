#!/usr/bin/env bash
# Publishes a fake ESP32 reading frame to Mosquitto for local testing.
# Usage: ./scripts/publish_fake_reading.sh [devID]
set -euo pipefail

DEV_ID="${1:-A4:CF:12:34:56:78}"
TIMESTAMP="$(date '+%d/%m/%Y %H:%M:%S')"

PAYLOAD=$(cat <<EOF
{
  "devID": "${DEV_ID}",
  "Timestamp": "${TIMESTAMP}",
  "Control": {
    "Sample_per_minute": 6,
    "Inv": { "On": 1, "Freq": 50 },
    "Restart": 0,
    "Valve": 56
  },
  "Data": {
    "pH": 6.8,
    "CE": 1850.3,
    "Solar": 645.2,
    "Volume": 20,
    "THC": { "T": 22.55, "H": 57.89, "C": 1.85 },
    "TH": { "T": 22.23, "H": 64.25 },
    "Errors": { "ADC": 0, "Pulses": 0, "I2C": 0, "Inverter": 0, "Inverter_State": 0 }
  }
}
EOF
)

# QoS 1: the broker only queues messages for an offline durable subscriber
# (Bento's clean_session:false session) when they're published at QoS >= 1.
# Whatever publishes real readings later must do the same or store-and-forward
# silently does nothing during a Bento restart/outage.
docker exec fertloops-mosquitto mosquitto_pub -q 1 -t "fertloops/${DEV_ID}/reading" -m "${PAYLOAD}"
echo "Published to fertloops/${DEV_ID}/reading:"
echo "${PAYLOAD}"
