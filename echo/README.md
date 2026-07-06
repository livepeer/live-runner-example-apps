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

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes `POST /echo` (start a session, open trickle `in`/`out` channels with `create_trickle_channels`) and `POST /update` (change the transform mid-stream), both reverse-proxied through the orchestrator. The client calls it with `reserve_session` → `MediaPublish`/`MediaOutput` → `stop_runner_session` ([client.py](client.py)) — reserve a session, publish frames into `in`, read the transformed output from `out`, release. Grep `# Livepeer:` in either file to see the exact calls. Frame decode/encode is PyAV; the transforms are OpenCV.

echo registers **dynamically** as the natural fit for a stateful app that already embeds the SDK (heartbeats, capacity, lifecycle). Trickle itself isn't tied to dynamic, though: `create_trickle_channels` rides the orchestrator's per-request `Livepeer-Session-Control` header, so a static runner exposing the same endpoints could open channels too.

## Run offchain (free)

Start the stack and confirm the runner registered:

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/echo registered
```

The input is a file path, or `-` to read an MPEG-TS stream from stdin (so you can pipe in anything ffmpeg produces); the output is a file, or `-` to write the echoed stream to stdout (pipe it to a player).

**From a file** — writes the result to `echo-out.ts`:

```sh
uv run client.py --mode blur --discovery https://localhost:8935/discovery ~/samples/bbb_720p.mp4
```

**Live from ffmpeg's test pattern** — no file needed; watch the test counter echo back in real time:

```sh
ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 -c:v libx264 -tune zerolatency -preset ultrafast -pix_fmt yuv420p -f mpegts - \
  | uv run client.py - --mode blur --discovery https://localhost:8935/discovery --output - \
  | ffplay -fflags nobuffer -flags low_delay -framedrop -i -
```

**From a webcam** — same pipe; just point ffmpeg at your camera. Device numbers vary (a bare `/dev/video0` often isn't the camera, and some cameras expose several nodes), so find and confirm yours first:

```sh
v4l2-ctl --list-devices              # list cameras and their /dev/videoN
ffplay -f v4l2 -i /dev/video0        # preview a node to confirm it's your camera (q to quit); try video1, video2, ...

ffmpeg -f v4l2 -input_format mjpeg -framerate 30 -video_size 1280x720 -i /dev/video0 \
  -c:v libx264 -tune zerolatency -preset ultrafast -pix_fmt yuv420p -f mpegts - \
  | uv run client.py - --mode blur --discovery https://localhost:8935/discovery --output - \
  | ffplay -fflags nobuffer -flags low_delay -framedrop -i -
```

Swap `/dev/video0` for your node. If that size/format isn't supported, list the camera's modes with `ffmpeg -f v4l2 -list_formats all -i /dev/videoN`. macOS: `-f avfoundation -framerate 30 -i 0`; Windows: `-f dshow -i video="<name>"`.

The `ffplay` low-delay flags (`-fflags nobuffer -flags low_delay -framedrop`) keep the preview close to realtime; drop them and it buffers.

- `--mode` picks the transform: `echo` (passthrough, the default), `gray`, `invert`, or `blur`. Use `--mode blur` on any command above to see the echo visibly transform the stream.
- `blur` sweeps the radius `0 -> max -> 0` live (driving `/update`); `--blur-period N` sets the seconds per sweep cycle (default 2; larger is slower). `gray`/`invert` are static.
- `--radius N` sets the initial blur strength, `--max-frames N` stops early.

Stop the stack with `docker compose down`.
