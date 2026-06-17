# Realtime trickle transcoding app: decodes a trickle video stream with PyAV,
# rescales each frame, and re-encodes to a trickle output. PyAV bundles ffmpeg,
# so no system ffmpeg is needed. CPU; self-registers as a Live Runner.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# av (PyAV) for decode/encode; livepeer-gateway SDK from Git (not on PyPI yet).
RUN pip install --no-cache-dir \
    av \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner"

WORKDIR /app
COPY runner.py ./

EXPOSE 8990

CMD ["python", "runner.py"]
