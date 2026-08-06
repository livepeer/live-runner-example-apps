# Streaming-ASR app: an aiohttp WebSocket server wrapping faster-whisper that
# self-registers as a Live Runner. CPU by default so it runs anywhere; --device
# cuda needs a CUDA-enabled base image (faster-whisper wants cuBLAS + cuDNN).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# faster-whisper (CTranslate2) runs on CPU out of the box with compute_type int8.
# livepeer-gateway SDK isn't on PyPI yet; install from Git.
RUN pip install --no-cache-dir \
    faster-whisper numpy \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner"

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 5005

CMD ["python", "runner.py"]
