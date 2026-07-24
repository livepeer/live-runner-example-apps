# Tiles example app (capacity fan-out, CPU image processing).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git.
# opencv-python-headless needs no system GUI libs.
ARG SDK_REF=ja/live-runner
ADD "https://api.github.com/repos/livepeer/livepeer-python-gateway/commits/${SDK_REF}" /tmp/sdk-ref.json
RUN pip install --no-cache-dir \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@${SDK_REF}" \
    opencv-python-headless numpy aiohttp

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
