# screen-agent app

A real, pip-installed application on the Livepeer network: send a screen recording, get back a structured bug report (markdown report + timeline + summary JSON). The pipeline samples keyframes, runs OCR / UI parsing / VLM reasoning over them, and writes the report — here it runs with deterministic fallback engines so the example needs no GPU; on a GPU host the same wrapper serves PaddleOCR + OmniParser + Qwen2.5-VL.

What this example adds over `hello-world`: the app logic is an **external package** ([screen-agent-mvp](https://github.com/emranemran/screen-agent-mvp)), not code written for the example. The Dockerfile pip-installs it and runs its bundled `screen-agent-runner` entry point — the pattern for putting an *existing* project on the network: the package's [`livepeer_runner.py`](https://github.com/emranemran/screen-agent-mvp/blob/main/src/screen_agent_mvp/livepeer_runner.py) is ~150 lines of aiohttp + `register_runner()` around an unchanged pure function.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-example/screen-agent`      |
| Runner mode  | persistent (single-shot by nature)   |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (JSON, base64 mp4 in)           |
| Port         | 8989                                 |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md).

> [!NOTE]
> This app currently runs in **persistent** mode. It will switch to **single-shot** once [#5](https://github.com/livepeer/live-runner-example-apps/issues/5) ships. Keep it offchain until then — per-second billing overbills a request/response app.

## How it's wired

The app is **dynamically registered**: the packaged runner self-registers via `register_runner` and exposes `POST /analyze` (`{"video_b64": ..., "preset": ...}` → `{"report_markdown", "summary", "timeline"}`), reverse-proxied through the orchestrator. The client calls it with `reserve_session` → `call_runner` → `stop_runner_session` ([client.py](client.py)) — grep `# Livepeer:` for the exact calls. Two things worth stealing:

- the runner does the analysis in `asyncio.to_thread`, so heartbeats keep flowing during a long CPU/GPU-bound call;
- the client passes `timeout=600` to `call_runner` — the SDK default is 5s, which no analysis survives.

## Run offchain (free)

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/screen-agent registered
uv run demo_video.py demo.mp4                                        # or bring any .mp4/.webm screen recording
uv run client.py demo.mp4 --discovery https://localhost:8935/discovery
# → prints the bug report; bundle saved to screen-agent-run/
docker compose down
```

The container runs with `--force-fallback` (no GPU or weights needed), so the report is the deterministic template. For real model-written reports, run the runner on a GPU host with the models installed and `--strict-models` — see the [screen-agent-mvp README](https://github.com/emranemran/screen-agent-mvp#readme).

## Run without Docker

Start an orchestrator built from `ja/live-runner`, then install and run the packaged runner directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
pip install "screen-agent-mvp @ git+https://github.com/emranemran/screen-agent-mvp@main" \
            "livepeer-gateway @ git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner" aiohttp
screen-agent-runner --orchestrator https://localhost:8935 --orchSecret abcdef \
  --app-id livepeer-example/screen-agent --force-fallback
uv run client.py demo.mp4
```
