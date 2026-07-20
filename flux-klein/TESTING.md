# Testing flux-klein against an orchestrator

This app is deployed on **pon's orchestrator** and can be exercised end to end
without a GPU on your side — you only run the host-side client, which streams
video to the runner and reads the transformed video back.

- **Discovery URL:** `http://154.61.61.108:8787/discovery`
- **App id:** `livepeer-example/flux-klein`
- **Transport:** trickle (video in `-in`, video out `-out`, control over `/update`)
- **Path used here:** offchain / free (no signer, no on-chain payments)

> The orchestrator serves plain **HTTP** on port 8787 — use `http://`, not
> `https://` (the discovery upgrades to HTTPS and fails otherwise).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python env + deps)
- `ffmpeg` (encodes the input; `smoke-test.py` also uses it to synthesize a clip)
- `mpv` **or** `ffplay` — only for live viewing
- A webcam at `/dev/video0` (Linux v4l2) — only for the webcam demos

## Setup

```bash
cd flux-klein
uv sync            # creates .venv with the client deps (no GPU stack)
```

Confirm the app is live on the orch:

```bash
curl -s http://154.61.61.108:8787/discovery \
  | grep -o 'livepeer-example/flux-klein' | head -1
```

`flux-klein` runs in **persistent** mode with capacity 1 per runner. If every
runner shows `capacity_available: 0`, a session is already streaming (or a prior
one leaked) — wait and retry.

## 1. Automated smoke test (pass/fail)

Reserves a session, publishes a short generated clip, reads the output, checks it
decodes, then tears down. Exits non-zero on failure.

```bash
.venv/bin/python smoke-test.py                       # generated testsrc clip
.venv/bin/python smoke-test.py --webcam --frames 240 # from your webcam
.venv/bin/python smoke-test.py --input clip.mp4 --stats-interval 2
```

Expected (against pon's orch, RTX 4000 Ada):

```
first output bytes after ~1.7s
stats: state=ready session="session_xxxx"
PASS flux-klein smoke test (sent=240 frames, out=~380000B, decoded=~19 frames)
```

Use `--input -` to pipe an MPEG-TS stream in (`ffmpeg ... -f mpegts - | ... --input -`).

## 2. Interactive — see it in realtime

One-command live view (webcam → flux-klein → your screen):

```bash
./webcam-demo.sh                                   # default prompt, live window
./webcam-demo.sh "an oil painting"                 # custom prompt
./webcam-demo.sh "a neon cyberpunk city" save      # save a clip and open it
```

Or drive the client directly and pipe to a low-latency player:

```bash
.venv/bin/python client.py --webcam \
  --discovery http://154.61.61.108:8787/discovery \
  --prompt "a psychedelic landscape, vivid colors" \
  --fps 8 --output - \
  | mpv --no-cache --untimed --profile=low-latency -
```

`--output -` streams MPEG-TS to stdout; the player shows each frame as it
arrives. No `--max-frames` ⇒ runs until ctrl-c. Swap in
`ffplay -fflags nobuffer -flags low_delay -framedrop -` if you prefer ffplay.

### Change prompt / params live

Re-prompt on a schedule (seconds=value). Also `seed:N` and `blend:X`:

```bash
.venv/bin/python client.py --webcam \
  --discovery http://154.61.61.108:8787/discovery \
  --max-frames 300 \
  --reprompt "3=a neon cyberpunk city" \
  --reprompt "6=a watercolor painting" \
  --reprompt "9=blend:0.7"
```

### File input instead of a webcam

```bash
.venv/bin/python client.py clip.mp4 \
  --discovery http://154.61.61.108:8787/discovery \
  --prompt "an oil painting" --output flux-klein-out.ts
mpv flux-klein-out.ts
```

## Performance note

pon's orch runs flux-klein on an **NVIDIA RTX 4000 Ada**, which measures at
**~2 output fps** here — low latency, low framerate (choppy but live). The PR's
~19 fps figure is on an RTX 6000 Pro; throughput scales with the GPU, not the
client. `GET <app_url>/stats` reports live input/inference/output rates during a
stream.

## Troubleshooting

| Symptom                              | Cause / fix                                                                                                  |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `0 output bytes`, `decoded 0 frames` | Input too short — a 4-step feedback model can't flush a trickle segment in <1s. Use ≥5–8s (`--frames 240`+). |
| `WRONG_VERSION_NUMBER` / SSL error   | You used `https://` — the orch is plain HTTP on 8787.                                                        |
| `flux-klein not in discovery`        | App not loaded on that orch, or wrong discovery URL.                                                         |
| reserve hangs / no capacity          | All runners at `capacity_available: 0` — a session is in use; retry shortly.                                 |
| webcam `Input/output error`          | Device busy or wrong node; try `--webcam /dev/video2` or check `v4l2-ctl --list-devices`.                    |
