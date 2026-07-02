# Packaging-only image for daydream's StreamDiffusion realtime-img2img demo.
#
# Unlike ../streamdiffusion, this image contains NO Livepeer code: it just runs the
# fork's FastAPI WebSocket server unmodified, and the orchestrator reverse-proxies it.
# The base layers are identical to ../streamdiffusion/Dockerfile so Docker reuses that
# build's cache; we only add the repo checkout (for demo/realtime-img2img) + server deps.
#
# NOTE: the fork's own Dockerfile clones the UPSTREAM cumulo-autumn repo, not the fork,
# so it would not run this rewritten server. We clone the fork at the pinned commit.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
ENV HF_HUB_ENABLE_HF_TRANSFER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl git \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python \
    && python -m pip --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        torch==2.7.1+cu128 torchvision==0.22.1+cu128 torchaudio==2.7.1+cu128 \
        --index-url https://download.pytorch.org/whl/cu128
RUN python -m pip install --no-cache-dir \
        "streamdiffusion[tensorrt,controlnet,ipadapter] @ git+https://github.com/daydreamlive/StreamDiffusion.git@94b9b96cb8a17d401ffbce516393d6482326ce62"
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python -m streamdiffusion.tools.install-tensorrt

# The pip package doesn't ship the demo/ dir, so clone the fork (pinned) for the server.
RUN git clone https://github.com/daydreamlive/StreamDiffusion.git /src \
    && cd /src && git checkout 94b9b96cb8a17d401ffbce516393d6482326ce62

# Server deps for demo/realtime-img2img (the inference stack came with streamdiffusion).
RUN python -m pip install --no-cache-dir \
        fastapi==0.115.0 "uvicorn[standard]==0.32.0" markdown2 python-multipart PyYAML compel hf_transfer
# compel drags in a newer transformers/hub that drops MT5Tokenizer and breaks the
# diffusers import; pin back to the versions the streamdiffusion stack expects.
RUN python -m pip install --no-cache-dir \
        transformers==4.56.0 huggingface_hub==0.35.0

# Engines + HF cache under /models (mount a host dir so they persist / can be prebuilt).
# The demo writes engines to a RELATIVE ./engines dir (it ignores --engine-dir), so
# symlink that onto the mounted volume to persist compiled engines across rebuilds.
ENV HF_HOME=/models/hf HUGGINGFACE_HUB_CACHE=/models/hf
WORKDIR /src/demo/realtime-img2img
RUN rm -rf engines && ln -s /models/engines engines
EXPOSE 7860

# --api-only: skip the built Node frontend (a Livepeer client drives the API directly).
CMD ["python", "main.py", "--host=0.0.0.0", "--port=7860", "--acceleration=tensorrt", "--api-only"]
