# Echo example app (trickle realtime video).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git. av (PyAV) ships its
# own ffmpeg; opencv-python-headless needs no system GUI libs.
RUN pip install --no-cache-dir \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner" \
    av opencv-python-headless aiohttp

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

CMD ["python", "runner.py"]
