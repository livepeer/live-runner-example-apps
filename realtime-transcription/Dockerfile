# Realtime-transcription app: an aiohttp WebSocket server wrapping faster-whisper
# that self-registers as a Live Runner. GPU only: large-v3-turbo is the model that
# is both accurate and fast enough to keep pace with a live stream, and it needs a
# CUDA runtime (CTranslate2 wants cuBLAS + cuDNN).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# CTranslate2 loads cuBLAS/cuDNN from the nvidia pip wheels, so no CUDA base image
# is needed -- the host driver comes in via the compose `deploy` reservation.
# livepeer-gateway SDK isn't on PyPI yet; install from Git.
RUN pip install --no-cache-dir \
    faster-whisper numpy \
    nvidia-cublas-cu12 nvidia-cudnn-cu12 \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 5005

CMD ["python", "runner.py"]
