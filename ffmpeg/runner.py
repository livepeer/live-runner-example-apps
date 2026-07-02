#!/usr/bin/env python3
"""ffmpeg as an agent tool — a multi-op Live Runner app.

One app, many operations: the request names an `op` (transcode / clip /
thumbnail) plus structured params; the runner maps each to a *vetted ffmpeg
command template* (never raw args) and returns the result. This is the
**tool -> agent capability** pattern:

  - one app (`livepeer/ffmpeg`), op chosen per request — not one app per op
  - `GET /ops` advertises the machine-readable op schema
  - SKILL.md is the agent-facing contract (when/how to call each op)
  - command templates are the security boundary (no arbitrary flags)

Single-op cousin: `../vod-transcode` (just transcode). The transport /
registration / mode choices are identical and deliberate:

  - transport    : HTTP request/response (no live media plane)
  - registration : dynamic — self-registers via the SDK, like hello-world
  - mode         : persistent (the default); single-shot by nature, but
                   single-shot payment isn't wired yet (go-livepeer#3955)

Wire protocol on POST /run:
  request : {"op": "clip", "input_b64": "...", "start": 3, "end": 8}
  response: {"output_b64": "...", "bytes": N, "media_type": "video/mp4"}

Media is base64'd into JSON so it rides the standard buffered call path (and its
payment), like vod-transcode. Fine for short clips; production passes URLs +
object storage instead of inlining bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
from contextlib import suppress
from typing import Any

from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
APP_ID = "livepeer/ffmpeg"
ENCODERS = ("libx264", "h264_nvenc")  # allowlist — never pass an arbitrary -c:v
# Default video encoder when the caller doesn't specify one. Operators enabling GPU
# set FFMPEG_DEFAULT_ENCODER=h264_nvenc so transcodes use the GPU by default.
_default_encoder = os.environ.get("FFMPEG_DEFAULT_ENCODER", "libx264").strip()
DEFAULT_ENCODER = _default_encoder if _default_encoder in ENCODERS else "libx264"

log = logging.getLogger("ffmpeg")


# --- params: validate to typed values; raising ValueError -> 400 to client ----
def _int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(params.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer")
    if not lo <= val <= hi:
        raise ValueError(f"{key} must be in [{lo}, {hi}]")
    return val


def _num(params: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        val = float(params.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not lo <= val <= hi:
        raise ValueError(f"{key} must be in [{lo}, {hi}]")
    return val


# --- ops: each validates params and returns (argv, out_path | None, media_type).
# out_path None means the op writes its result to stdout (probe -> JSON). Command
# templates are fixed; params are validated — never pass arbitrary flags.
def _venc(encoder: str, quality: int = 23) -> list[str]:
    """Video-encoder args with the right preset + quality per encoder.
    quality is CRF (libx264) / CQ (nvenc): lower = higher quality (~18 high, 28 small).
    """
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]
    return [
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-rc",
        "vbr",
        "-cq",
        str(quality),
        "-b:v",
        "0",
    ]


def _transcode(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    encoder = str(p.get("encoder", DEFAULT_ENCODER))
    if encoder not in ENCODERS:
        raise ValueError(f"encoder must be one of {list(ENCODERS)}")
    quality = _int(p, "quality", 23, 0, 51)
    out = os.path.join(d, "out.mp4")
    argv = ["ffmpeg", "-y", "-i", src]
    if "height" in p:  # optional — omit to keep the source resolution (e.g. keep 4K)
        argv += ["-vf", f"scale=-2:{_int(p, 'height', 720, 16, 4320)}"]
    argv += [*_venc(encoder, quality), "-c:a", "aac", "-movflags", "+faststart", out]
    return argv, out, "video/mp4"


def _clip(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    start = _num(p, "start", 0.0, 0.0, 86400.0)
    end = _num(p, "end", start + 5.0, 0.0, 86400.0)
    if end <= start:
        raise ValueError("end must be greater than start")
    out = os.path.join(d, "out.mp4")
    return (
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start}",
            "-i",
            src,
            "-t",
            f"{end - start}",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            out,
        ],
        out,
        "video/mp4",
    )


def _thumbnail(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    at = _num(p, "at", 0.0, 0.0, 86400.0)
    out = os.path.join(d, "out.jpg")
    return (
        ["ffmpeg", "-y", "-ss", f"{at}", "-i", src, "-frames:v", "1", "-q:v", "2", out],
        out,
        "image/jpeg",
    )


def _extract_audio(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    out = os.path.join(d, "out.m4a")
    return (
        ["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "aac", "-b:a", "192k", out],
        out,
        "audio/mp4",
    )


def _gif(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    fps = _int(p, "fps", 12, 1, 30)
    height = _int(p, "height", 240, 16, 1080)
    vf = (
        f"fps={fps},scale=-2:{height}:flags=lanczos,"
        "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    )
    out = os.path.join(d, "out.gif")
    return ["ffmpeg", "-y", "-i", src, "-vf", vf, "-loop", "0", out], out, "image/gif"


def _crop(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    if "width" not in p or "height" not in p:
        raise ValueError("crop requires width and height")
    w = _int(p, "width", 0, 1, 16384)
    h = _int(p, "height", 0, 1, 16384)
    x = _int(p, "x", 0, 0, 16384)
    y = _int(p, "y", 0, 0, 16384)
    out = os.path.join(d, "out.mp4")
    return (
        [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-vf",
            f"crop={w}:{h}:{x}:{y}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out,
        ],
        out,
        "video/mp4",
    )


CONTAINERS = {"mp4": "video/mp4", "mkv": "video/x-matroska", "mov": "video/quicktime"}


def _convert(p: dict, src: str, d: str) -> tuple[list[str], str, str]:
    fmt = str(p.get("format", "mp4")).lower()
    if fmt not in CONTAINERS:
        raise ValueError(f"format must be one of {list(CONTAINERS)}")
    quality = _int(p, "quality", 23, 0, 51)
    out = os.path.join(d, f"out.{fmt}")
    if fmt == "mkv":  # universal container — fast remux, keeps codecs (e.g. vp9/opus)
        argv = ["ffmpeg", "-y", "-i", src, "-c", "copy", out]
    else:  # mp4 / mov need H.264/AAC — re-encode so any input (incl. webm) works
        argv = [
            "ffmpeg",
            "-y",
            "-i",
            src,
            *_venc(DEFAULT_ENCODER, quality),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            out,
        ]
    return argv, out, CONTAINERS[fmt]


def _probe(p: dict, src: str, d: str) -> tuple[list[str], None, str]:
    return (
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            src,
        ],
        None,
        "application/json",
    )


OPS: dict[str, dict[str, Any]] = {
    "transcode": {
        "build": _transcode,
        "returns": "video/mp4",
        "desc": "Re-encode to H.264. Optional target height (omit to keep source resolution); quality sets CRF/CQ.",
        "params": {
            "height": {
                "type": "int",
                "range": [16, 4320],
                "note": "omit to keep source resolution",
            },
            "quality": {
                "type": "int",
                "default": 23,
                "range": [0, 51],
                "note": "CRF/CQ — lower is higher quality (18 high, 28 small)",
            },
            "encoder": {
                "type": "enum",
                "values": list(ENCODERS),
                "default": DEFAULT_ENCODER,
            },
        },
    },
    "clip": {
        "build": _clip,
        "returns": "video/mp4",
        "desc": "Cut [start, end] seconds without re-encoding (stream copy).",
        "params": {
            "start": {"type": "number", "default": 0, "unit": "seconds"},
            "end": {"type": "number", "required": True, "unit": "seconds"},
        },
    },
    "thumbnail": {
        "build": _thumbnail,
        "returns": "image/jpeg",
        "desc": "Extract a single JPEG frame at a timestamp.",
        "params": {"at": {"type": "number", "default": 0, "unit": "seconds"}},
    },
    "extract_audio": {
        "build": _extract_audio,
        "returns": "audio/mp4",
        "desc": "Extract the audio track as AAC/.m4a (input must have audio).",
        "params": {},
    },
    "gif": {
        "build": _gif,
        "returns": "image/gif",
        "desc": "Convert to an animated GIF (palette-optimized).",
        "params": {
            "fps": {"type": "int", "default": 12, "range": [1, 30]},
            "height": {"type": "int", "default": 240, "range": [16, 1080]},
        },
    },
    "crop": {
        "build": _crop,
        "returns": "video/mp4",
        "desc": "Crop to width x height at offset (x, y); re-encoded H.264.",
        "params": {
            "width": {"type": "int", "required": True},
            "height": {"type": "int", "required": True},
            "x": {"type": "int", "default": 0},
            "y": {"type": "int", "default": 0},
        },
    },
    "convert": {
        "build": _convert,
        "returns": "video/<format>",
        "desc": "Change container: mkv remuxes (fast, keeps codecs); mp4/mov re-encode to H.264/AAC so any input (e.g. webm) works.",
        "params": {
            "format": {"type": "enum", "values": list(CONTAINERS), "default": "mp4"},
            "quality": {
                "type": "int",
                "default": 23,
                "range": [0, 51],
                "note": "CRF/CQ for the mp4/mov re-encode; lower is higher quality",
            },
        },
    },
    "probe": {
        "build": _probe,
        "returns": "application/json",
        "desc": "Inspect the file with ffprobe; returns JSON format + stream info.",
        "params": {},
    },
}


def _run_op(op: str, data: bytes, params: dict) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in")
        with open(src, "wb") as f:
            f.write(data)
        argv, out, media_type = OPS[op]["build"](params, src, d)  # may raise ValueError
        proc = subprocess.run(argv, check=True, capture_output=True)
        if out is None:  # probe -> JSON on stdout
            return proc.stdout, media_type
        with open(out, "rb") as f:
            return f.read(), media_type


# --- HTTP --------------------------------------------------------------------
async def _handle_ops(request: web.Request) -> web.Response:
    """Machine-readable op schema — the programmatic half of SKILL.md."""
    ops = {
        name: {"desc": s["desc"], "params": s["params"], "returns": s["returns"]}
        for name, s in OPS.items()
    }
    return web.json_response({"app": APP_ID, "ops": ops})


async def _handle_run(request: web.Request) -> web.Response:
    payload = await request.json()
    op = str(payload.get("op", "")).strip()
    if op not in OPS:
        raise web.HTTPBadRequest(text=f"unknown op {op!r}; GET /ops lists {list(OPS)}")
    if "input_b64" not in payload:
        raise web.HTTPBadRequest(text="missing input_b64")
    data = base64.b64decode(payload["input_b64"])
    log.info("op=%s input=%d bytes", op, len(data))
    try:
        out, media_type = await asyncio.to_thread(_run_op, op, data, payload)
    except ValueError as exc:  # invalid params
        return web.json_response({"error": str(exc)}, status=400)
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode(errors="replace")[-500:]
        return web.json_response({"error": f"ffmpeg failed: {msg}"}, status=400)
    if media_type == "application/json":  # probe — return parsed metadata, not a blob
        return web.json_response({"op": op, "analysis": json.loads(out)})
    return web.json_response(
        {
            "output_b64": base64.b64encode(out).decode(),
            "bytes": len(out),
            "media_type": media_type,
        }
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ffmpeg multi-op Live Runner (tool -> agent)."
    )
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--price",
        type=int,
        default=0,
        help="Price in USD per pixels-per-unit (0 = free).",
    )
    parser.add_argument(
        "--pixels-per-unit", type=int, default=1, help="Scale factor for the price."
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            price_per_unit=args.price,
            pixels_per_unit=args.pixels_per_unit,
        )
        log.info(
            "registered runner_id=%s app=%s ops=%s default_encoder=%s",
            app["registration"].runner_id,
            APP_ID,
            list(OPS),
            DEFAULT_ENCODER,
        )

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()

    app = web.Application(client_max_size=512 * 1024 * 1024)  # allow modest uploads
    app.router.add_get("/ops", _handle_ops)
    app.router.add_post("/run", _handle_run)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
