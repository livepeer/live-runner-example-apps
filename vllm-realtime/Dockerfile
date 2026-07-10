# vllm-realtime example app (the transcription bridge, not vLLM itself).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git. Trickle lives on the
# ja/live-runner branch. `websockets` is for the vLLM realtime backend; `av`,
# `aiohttp` come transitively with the SDK.
RUN pip install --no-cache-dir \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner" \
    websockets

WORKDIR /app
COPY runner.py transcriber.py client.py ./

EXPOSE 5000

# ENTRYPOINT (not CMD) so the compose `command:` passes runner flags straight
# through, matching the other examples in this repo.
ENTRYPOINT ["python", "runner.py"]
