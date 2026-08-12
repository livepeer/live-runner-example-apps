# Realtime-transcription example app (persistent WebSocket, GPU speech-to-text).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git. CTranslate2 (under
# faster-whisper) loads cuBLAS/cuDNN from the nvidia wheels, so no CUDA base image
# is needed -- the host driver comes in via the compose `deploy` reservation.
RUN pip install --no-cache-dir \
    faster-whisper numpy \
    nvidia-cublas-cu12 nvidia-cudnn-cu12 \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@main"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
