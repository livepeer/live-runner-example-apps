# VOD transcoding app: an aiohttp server wrapping ffmpeg that self-registers as
# a Live Runner. CPU (libx264) by default so it runs anywhere; for GPU set
# --encoder h264_nvenc and use a CUDA-enabled ffmpeg image (see README).
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
