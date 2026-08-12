# Echo example app (trickle realtime video).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

# livepeer-gateway SDK isn't on PyPI yet; install from Git. av (PyAV) ships its
# own ffmpeg; opencv-python-headless needs no system GUI libs.
RUN pip install --no-cache-dir \
    "livepeer-gateway>=1.0.0" \
    av opencv-python-headless aiohttp

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
