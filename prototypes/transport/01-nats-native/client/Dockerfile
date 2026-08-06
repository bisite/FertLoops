# Simulator / consumer / verifier for the native-NATS prototype.
# Slim Python base pinned to an exact patch tag; nats-py pinned to an exact
# release so a rerun measures the same client behaviour.
FROM python:3.13.14-slim-trixie

RUN pip install --no-cache-dir nats-py==2.15.0

WORKDIR /app
COPY frame.py common.py simulator.py consumer.py verifier.py ./

CMD ["python", "-u", "simulator.py"]
