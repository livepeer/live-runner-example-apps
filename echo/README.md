# Echo app (trickle realtime video)

A realtime video app on the Livepeer network: it receives a live video stream over **trickle** channels, optionally transforms each frame (gray / invert / blur), and echoes it back. This is the **live/stateful** path — continuous media over trickle, not request/response — so the app embeds the SDK and self-registers (dynamic).

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-sample/echo`               |
| Runner mode  | persistent (held-open session)       |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | trickle (realtime video in/out)      |
| Port         | 8989                                 |

Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared setup are in the [repo README](../README.md).

## How it's wired

The app exposes HTTP `/echo` (start a session, create trickle `in`/`out` channels) and `/update` (change the transform mid-stream), both reverse-proxied through the orchestrator. The client reserves a session, publishes video frames into the `in` channel, and reads the transformed output from the `out` channel. Frame decode/encode is PyAV; the transforms are OpenCV.

Because the live path is stateful (channels live for the session), the app runs the SDK and is **dynamic** — there's no static-runner equivalent for continuous media.

## Run offchain (free)

Needs a sample video file (any mp4/mov with a video stream).

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/echo registered
uv run client.py --blur --discovery https://localhost:8935/discovery ~/samples/bbb_720p.mp4   # writes the echoed result to echo-out.ts
docker compose down
```

- `--blur` sweeps the blur radius while streaming (drives `/update` live). Drop it for a plain echo.
- `--radius N` sets the initial blur strength; `--max-frames N` stops early.
- Pipe to a player instead of a file: `uv run client.py --blur --output - ~/samples/clip.mp4 | ffplay -`.
