# Echo app (trickle realtime video)

A realtime video app on the Livepeer network: it receives a live video stream over **trickle** channels, optionally transforms each frame (gray / invert / blur), and echoes it back. This is the **live/stateful** path — continuous media over trickle, not request/response — so the app embeds the SDK and self-registers (dynamic).

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-sample/echo`               |
| Transport    | trickle (realtime video in/out)      |
| Registration | dynamic (self-registers via the SDK) |
| Port         | 8989                                 |

Ported from the [`livepeer-python-gateway` echo example](https://github.com/livepeer/livepeer-python-gateway/tree/ja/live-runner/examples/echo) by Josh Allmann; only change is a `--host` flag so the runner binds `0.0.0.0` in a container. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared setup are in the [repo README](../README.md).

## How it's wired

The app exposes HTTP `/echo` (start a session, create trickle `in`/`out` channels) and `/update` (change the transform mid-stream), both reverse-proxied through the orchestrator. The client reserves a session, publishes video frames into the `in` channel, and reads the transformed output from the `out` channel. Frame decode/encode is PyAV; the transforms are OpenCV.

Because the live path is stateful (channels live for the session), the app runs the SDK and is **dynamic** — there's no static-runner equivalent for continuous media.

## Run offchain (free)

Needs a sample video file (any mp4/mov with a video stream).

```sh
docker compose up -d --build              # orchestrator + echo app (first build pulls PyAV/OpenCV)
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/echo registered
uv run client.py --blur ~/samples/clip.mp4   # publishes the clip, writes the echoed result to echo-out.ts
docker compose down
```

- `--blur` sweeps the blur radius while streaming (drives `/update` live). Drop it for a plain echo.
- `--radius N` sets the initial blur strength; `--max-frames N` stops early.
- Pipe to a player instead of a file: `uv run client.py --blur --output - ~/samples/clip.mp4 | ffplay -`.

## Run on-chain (paid)

Unlike the per-call HTTP examples, the **live path bills the session per second of wall-clock** while it's open: the orchestrator meters the open session and drops it when the balance runs dry. So the client keeps it funded for the whole stream with a continuous payment loop (`run_session_payments`, started right after `reserve_session`). Layer the on-chain overlay to add a remote signer, run the orchestrator on-chain, and register echo with a price. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --blur --signer http://localhost:7936 ~/samples/clip.mp4
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

`--payment-interval` (default 3s) sets how often the client tops up the session; keep it at or below the orchestrator's payment interval so the balance stays ahead, and tune it to your deployment.
