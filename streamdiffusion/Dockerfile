# Self-contained StreamDiffusion app for the new Livepeer runner.
#
# Depends only on pip packages + our sd.py/runner.py — NOT the ai-runner image.
# Pins mirror the upstream StreamDiffusion stack (CUDA 12.8 / torch 2.7.1 / the
# daydreamlive StreamDiffusion fork) so the TensorRT path matches; the streamdiffusion
# + tensorrt stack is version-sensitive, so keep these aligned if you bump them.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
ENV HF_HUB_ENABLE_HF_TRANSFER=1

# Python 3.11 (StreamDiffusion requires 3.11) + git for the pip-from-git install.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl git \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python \
    && python -m pip --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Torch (CUDA 12.8 wheels) first, then StreamDiffusion + TensorRT extras.
RUN python -m pip install --no-cache-dir \
        torch==2.7.1+cu128 torchvision==0.22.1+cu128 torchaudio==2.7.1+cu128 \
        --index-url https://download.pytorch.org/whl/cu128
RUN python -m pip install --no-cache-dir \
        "streamdiffusion[tensorrt,controlnet,ipadapter] @ git+https://github.com/daydreamlive/StreamDiffusion.git@94b9b96cb8a17d401ffbce516393d6482326ce62"
# opencv (pulled in by streamdiffusion's controlnet preprocessing) needs libGL.
# Separate layer so it doesn't invalidate the cached torch/streamdiffusion installs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python -m streamdiffusion.tools.install-tensorrt

# New-runner SDK + media/serving deps (av ships its own ffmpeg).
# --ignore-requires-python: the SDK declares >=3.12 but uses no 3.12-only
# features; StreamDiffusion pins us to 3.11, and the SDK runs fine there.
RUN python -m pip install --no-cache-dir --ignore-requires-python \
        "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@rs/live-runner-session-payments" \
        av aiohttp pillow numpy hf_transfer

WORKDIR /app
COPY sd.py runner.py ./

# Engines + HF cache live under /models (mount a host dir so they persist/are prebuilt).
ENV SD_ENGINE_DIR=/models/engines HUGGINGFACE_HUB_CACHE=/models/hf
EXPOSE 8900

CMD ["python", "runner.py", "--host=0.0.0.0"]
