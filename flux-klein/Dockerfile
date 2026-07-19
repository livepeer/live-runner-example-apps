# Self-contained realtime FLUX Klein app for the new Livepeer runner.
#
# Depends only on pip packages + our flux_klein.py/runner.py — NOT the Daydream
# Scope platform. FLUX.2-klein-4B needs a diffusers build that ships
# Flux2KleinPipeline (not yet in a stable release), so diffusers is installed
# from git. CUDA 12.8 / torch 2.7.1 to match the GPU wheels.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
ENV HF_HUB_ENABLE_HF_TRANSFER=1

# Python 3.12 (the SDK declares >=3.12; FLUX/diffusers run fine there) + git for
# the pip-from-git installs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl git \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python \
    && python -m pip --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Torch (CUDA 12.8 wheels) first so the diffusers install below doesn't pull a
# CPU build over the top.
RUN python -m pip install --no-cache-dir \
        torch==2.7.1+cu128 torchvision==0.22.1+cu128 \
        --index-url https://download.pytorch.org/whl/cu128

# diffusers from git: Flux2KleinPipeline (and the flux2 internals refine_frame
# reaches into) is not in a stable diffusers release yet. transformers/accelerate
# for the Qwen3 text encoder + device management.
RUN python -m pip install --no-cache-dir \
        "diffusers @ git+https://github.com/huggingface/diffusers.git" \
        transformers accelerate \
        av aiohttp pillow numpy hf_transfer \
        "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@rs/live-runner-session-payments"

WORKDIR /app
COPY flux_klein.py runner.py ./

# HF model weights are cached under /models/hf; mount a host dir so they persist
# across restarts and don't need to be re-downloaded (~15 GB for klein-4B).
ENV HUGGINGFACE_HUB_CACHE=/models/hf
EXPOSE 8720

ENTRYPOINT ["python", "runner.py"]
