# Overlay: published ComfyStream image + this folder's live-runner integration.
# The comfystream package and ComfyUI workspace already live in the base image.
ARG BASE_IMAGE=livepeer/comfystream:latest
FROM ${BASE_IMAGE}

WORKDIR /app

# livepeer-gateway's generated pb2 needs protobuf >= 6.31.1. ComfyUI pins
# protobuf<5; the live-runner path still runs with protobuf 6.
RUN /bin/bash -lc 'source /workspace/miniconda3/etc/profile.d/conda.sh \
  && conda activate comfystream \
  && pip install --no-cache-dir \
    "protobuf>=6.31.1" \
    "livepeer-gateway>=1.0.0" \
    av'

COPY runner.py /app/runner.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8991

ENTRYPOINT ["/app/entrypoint.sh"]
