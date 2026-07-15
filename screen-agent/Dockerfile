# screen-agent example app (video → bug report).
#
# Unlike the other examples, the app logic lives in an external pip package
# (screen-agent-mvp); this image just installs it and runs its bundled
# live-runner entry point. This demonstrates wrapping an EXISTING project —
# the common real-world case — rather than an app written for the example.
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# The app package, plus its live-runner glue deps (aiohttp + the SDK, which
# isn't on PyPI yet). screen-agent-mvp keeps them in a dependency group, so
# they're installed explicitly here.
RUN pip install --no-cache-dir \
    "screen-agent-mvp @ git+https://github.com/emranemran/screen-agent-mvp@main" \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner" \
    "aiohttp>=3.10,<4"

EXPOSE 8989

# --force-fallback: deterministic heuristic/template engines, so the example
# runs anywhere with no GPU or model weights. On a GPU host with the models
# installed, drop the flag (or use --strict-models) for real VLM analysis —
# see https://github.com/emranemran/screen-agent-mvp#readme.
ENTRYPOINT ["screen-agent-runner"]
