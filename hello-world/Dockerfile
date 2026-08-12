# Hello-world example app (http server).
FROM python:3.12-slim

# Flush stdout/stderr immediately so output isn't block-buffered in `docker logs`.
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# livepeer-gateway SDK isn't on PyPI yet; install from Git.
RUN pip install --no-cache-dir \
    "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@main"

WORKDIR /app
COPY runner.py client.py ./

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
