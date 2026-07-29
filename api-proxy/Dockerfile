# api-proxy example app (http server). No Livepeer code — the orchestrator
# attaches it statically via runners.json.
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir aiohttp

WORKDIR /app
COPY runner.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
