# Throwaway image shared by the simulator, the consumer and the verifier.
FROM python:3.13-slim

# paho-mqtt 2.x: the callback API version has to be passed explicitly, which
# the sources here do.
RUN pip install --no-cache-dir "paho-mqtt==2.1.0"

WORKDIR /app
COPY src/ /app/

# -u so the per-phase progress log is not swallowed by stdout buffering when
# run-experiment.sh tails these containers.
ENTRYPOINT ["python", "-u"]
