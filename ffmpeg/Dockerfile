# ffmpeg tool app: an aiohttp server wrapping ffmpeg that self-registers as a
# Live Runner. CPU (libx264) by default so it runs anywhere; for an NVIDIA GPU layer
# docker-compose.gpu.yml (the apt ffmpeg already has h264_nvenc — see README).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git.
RUN pip install --no-cache-dir \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner"

WORKDIR /app
COPY runner.py ./

EXPOSE 5000

CMD ["python", "runner.py"]
