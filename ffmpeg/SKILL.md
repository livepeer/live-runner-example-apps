---
name: ffmpeg
description: Process audio/video on the Livepeer network via the livepeer/ffmpeg runner — transcode, clip, thumbnail, extract audio, make a GIF, crop, convert container, or probe metadata. Use when an agent needs to transform or inspect a media file.
---

# ffmpeg (Livepeer capability)

Media operations exposed as a single Livepeer app, `livepeer/ffmpeg`. The agent
picks an **operation** and sends a file; the runner runs a vetted ffmpeg command
and returns the result. One app, many ops — you choose the op per call.

## When to use

- **transcode** — re-encode a video to a target height/codec (e.g. shrink 1080p → 480p).
- **clip** — cut a `[start, end]` segment out of a video (fast, no re-encode).
- **thumbnail** — grab a single still frame at a timestamp.
- **extract_audio** — pull the audio track out as AAC/`.m4a`.
- **gif** — convert a (short) video to an animated GIF.
- **crop** — crop the frame to a width×height region at an offset.
- **convert** — change container format (`mp4`/`mkv`/`mov`); e.g. a `.webm` recording → mp4.
- **probe** — inspect the file; returns JSON metadata (codecs, resolution, duration…).

If the task isn't one of these, this skill doesn't apply.

## How to call it

1. **Reserve a session** for app `livepeer/ffmpeg` (the SDK's `reserve_session`, or
   POST the orchestrator's reserve endpoint). You get a `session_id` and `app_url`.
2. **POST `{app_url}/run`** with JSON: the `op`, the input file as base64 in
   `input_b64`, and the op's params at the top level.
3. The response returns the output as base64 in `output_b64` plus its `media_type`.

`GET {app_url}/ops` returns this same op/param schema as JSON (the machine-readable
half of this doc) — read it if you want to discover ops/params programmatically.

> Media is base64'd into JSON so it rides the standard paid call path. Keep inputs
> small (short clips); large media should use URLs + object storage instead.

## Operations

### `transcode` → `video/mp4`
Re-encode to H.264. **Omit `height` to keep the source resolution** (e.g. keep 4K);
lower `quality` (CRF/CQ) means higher quality.

| param | type | default | notes |
| --- | --- | --- | --- |
| `height` | int | — (keep source) | 16–4320; omit to keep source resolution |
| `quality` | int | 23 | CRF/CQ — lower is higher quality (18 high, 28 small) |
| `encoder` | enum | server default | `libx264` (CPU) or `h264_nvenc` (GPU) |

```json
{ "op": "transcode", "input_b64": "<...>", "quality": 18 }
```

### `clip` → `video/mp4`
Cut `[start, end]` seconds without re-encoding (stream copy — fast).

| param | type | default | notes |
| --- | --- | --- | --- |
| `start` | number | 0 | seconds |
| `end` | number | — (required) | seconds; must be > `start` |

```json
{ "op": "clip", "input_b64": "<...>", "start": 3, "end": 8 }
```

### `thumbnail` → `image/jpeg`
Extract a single JPEG frame at a timestamp.

| param | type | default | notes |
| --- | --- | --- | --- |
| `at` | number | 0 | seconds |

```json
{ "op": "thumbnail", "input_b64": "<...>", "at": 1.5 }
```

### `extract_audio` → `audio/mp4`
Extract the audio track as AAC (`.m4a`). The input must have an audio stream.

```json
{ "op": "extract_audio", "input_b64": "<...>" }
```

### `gif` → `image/gif`
Convert to an animated GIF (palette-optimized). Keep clips short.

| param | type | default | notes |
| --- | --- | --- | --- |
| `fps` | int | 12 | 1–30 |
| `height` | int | 240 | 16–1080 (width keeps aspect) |

```json
{ "op": "gif", "input_b64": "<...>", "fps": 12, "height": 240 }
```

### `crop` → `video/mp4`
Crop to `width`×`height` at offset (`x`, `y`); re-encoded H.264.

| param | type | default | notes |
| --- | --- | --- | --- |
| `width` | int | — (required) | px |
| `height` | int | — (required) | px |
| `x` | int | 0 | left offset (px) |
| `y` | int | 0 | top offset (px) |

```json
{ "op": "crop", "input_b64": "<...>", "width": 640, "height": 480, "x": 0, "y": 0 }
```

### `convert` → container (`mp4`/`mkv`/`mov`)
Change container format. `mkv` remuxes (fast, keeps codecs like vp9/opus); `mp4` and
`mov` re-encode to H.264/AAC so **any input works** (e.g. a `.webm` screen recording).

| param | type | default | notes |
| --- | --- | --- | --- |
| `format` | enum | `mp4` | `mp4`, `mkv`, or `mov` |
| `quality` | int | 23 | CRF/CQ for the mp4/mov re-encode; lower is higher quality |

```json
{ "op": "convert", "input_b64": "<...>", "format": "mp4", "quality": 18 }
```

### `probe` → `application/json`
Inspect the file with ffprobe. **Returns metadata, not media** — the response carries
an `analysis` object (format + streams: codecs, resolution, duration, bitrate) instead
of `output_b64`. Useful for an agent to reason about a file before acting.

```json
{ "op": "probe", "input_b64": "<...>" }
```

## Response shape

Media ops return base64 output:
```json
{ "output_b64": "<...>", "bytes": 12345, "media_type": "video/mp4" }
```
`probe` returns metadata instead:
```json
{ "op": "probe", "analysis": { "format": { }, "streams": [ ] } }
```

On bad params or an ffmpeg failure: `{ "error": "..." }` with HTTP 400.

## Errors

- `unknown op` — the `op` isn't one listed by `GET /ops`.
- param errors (e.g. `end must be greater than start`, `crop requires width and height`,
  `encoder must be one of [...]`).
- `ffmpeg failed: ...` — the input wasn't valid media (or, for `extract_audio`, had no
  audio track) or the op couldn't run.
