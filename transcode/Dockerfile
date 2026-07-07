# Unified transcoding app: aiohttp server that self-registers as a Live Runner and
# serves both the batch (VOD, ffmpeg CLI) and live (trickle, PyAV) surfaces.
# CPU by default (libx264/libx265/libsvtav1 in Debian's ffmpeg); for GPU AV1 set
# --av1-encoder av1_nvenc and use a CUDA-enabled base + ffmpeg (see README).
FROM python:3.12-slim

# ffmpeg = the batch engine (CLI); PyAV (below) carries the live/trickle path.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# rs/media-publish-encoder-opts (livepeer-python-gateway#35): per-track bitrate +
# codec-aware encoder options, so the live path honors bitrate/profile + AV1/HEVC.
RUN pip install --no-cache-dir \
    "av" \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@rs/media-publish-encoder-opts"

WORKDIR /app
COPY runner.py profiles.py engine.py ./

EXPOSE 8990

CMD ["python", "runner.py"]
