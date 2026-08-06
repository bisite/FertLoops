# Simulator, consumer and verifier all live in one image -- they share the frame
# builder and the results-file format, and this is throwaway code.
#
# python:3.13-slim pinned by digest (resolves to Python 3.13.14), same
# pin-by-digest convention the repo's existing sandbox uses for images whose tags
# move.
FROM python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# paho-mqtt 2.x: the CallbackAPIVersion argument is mandatory in 2.x, so this pin
# and the client construction in simulator.py/consumer.py have to move together.
RUN pip install --no-cache-dir paho-mqtt==2.1.0

WORKDIR /app
COPY frame.py simulator.py consumer.py verifier.py dbrows.py ./
