# Tiles example app (capacity fan-out, CPU image processing).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

# opencv-python-headless needs no system GUI libs.
RUN pip install --no-cache-dir \
    "livepeer-gateway>=1.0.0" \
    opencv-python-headless numpy aiohttp

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
