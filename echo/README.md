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

Start the stack and confirm the runner registered:

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/echo registered
```

The client publishes a video stream into the echo and writes the echoed result back. The input is a file path, or `-` to read an MPEG-TS stream from stdin (so you can pipe in anything ffmpeg produces). The output is a file, or `-` to write the echoed stream to stdout (pipe it to a player).

**From a file** — writes the result to `echo-out.ts`:

```sh
uv run client.py --blur --discovery https://localhost:8935/discovery ~/samples/bbb_720p.mp4
```

**Live from ffmpeg's test pattern** — no file needed; watch the test counter echo back in real time:

```sh
ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 -c:v libx264 -pix_fmt yuv420p -f mpegts - \
  | uv run client.py - --discovery https://localhost:8935/discovery --output - \
  | ffplay -
```

**From a webcam** — same pipe; just point ffmpeg at your camera. Device numbers vary (a bare `/dev/video0` often isn't the camera, and some cameras expose several nodes), so find and confirm yours first:

```sh
v4l2-ctl --list-devices              # list cameras and their /dev/videoN
ffplay -f v4l2 -i /dev/video0        # preview a node to confirm it's your camera (q to quit); try video1, video2, ...

ffmpeg -f v4l2 -input_format mjpeg -framerate 30 -video_size 1280x720 -i /dev/video0 \
  -c:v libx264 -pix_fmt yuv420p -f mpegts - \
  | uv run client.py - --discovery https://localhost:8935/discovery --output - \
  | ffplay -
```

Swap `/dev/video0` for your node. If that size/format isn't supported, list the camera's modes with `ffmpeg -f v4l2 -list_formats all -i /dev/videoN`. macOS: `-f avfoundation -framerate 30 -i 0`; Windows: `-f dshow -i video="<name>"`.

Stop the stack with `docker compose down`.

- `--blur` sweeps the blur radius live (drives `/update`); drop it for a plain echo. `--radius N` sets the initial strength, `--max-frames N` stops early.
- Add `--blur` to any of the above to see the echo visibly transform the stream, not just pass it through.
