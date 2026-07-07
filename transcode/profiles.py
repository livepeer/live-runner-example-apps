"""go-livepeer native transcoding profile contract → ffmpeg args.

The config is go-livepeer's `ffmpeg.JsonProfile` (sent as `{"profiles": [...]}`),
so this container is a drop-in for the native transcoding pipeline. Fields:

  name, width, height, bitrate, fps, fpsDen, profile, gop, encoder,
  colorDepth, chromaFormat, quality

This module is the single source of truth for both surfaces (batch + live):
`normalize()` parses one native profile, and `video_args()` maps it to ffmpeg
video flags. Same input config → same encode, whichever transport carries it.
"""

from __future__ import annotations

from typing import Any

# encoder (native) -> ffmpeg encoder + output container/extension.
_ENCODERS = {
    "": ("libx264", "mp4"),
    "h264": ("libx264", "mp4"),
    "h265": ("libx265", "mp4"),
    "hevc": ("libx265", "mp4"),
    "vp8": ("libvpx", "webm"),
    "vp9": ("libvpx-vp9", "webm"),
    "av1": ("libsvtav1", "mkv"),  # GPU: override encoder=av1_nvenc via --av1-encoder
}

# native H.264 profile name -> ffmpeg -profile:v value.
_H264_PROFILE = {
    "": None,
    "h264baseline": "baseline",
    "h264main": "main",
    "h264high": "high",
    "h264constrainedhigh": "high",
    "h264high444profile": "high444",
}


def normalize(raw: dict[str, Any], av1_encoder: str = "libsvtav1") -> dict[str, Any]:
    """Validate + fill defaults for one native JsonProfile."""
    width = int(raw.get("width", 0) or 0)
    height = int(raw.get("height", 0) or 0)
    if width <= 0 and height <= 0:
        raise ValueError("profile needs width and/or height")
    enc_key = str(raw.get("encoder", "") or "").lower()
    if enc_key not in _ENCODERS:
        raise ValueError(f"unsupported encoder {raw.get('encoder')!r}")
    encoder, ext = _ENCODERS[enc_key]
    if enc_key == "av1":
        encoder = av1_encoder
    name = str(raw.get("name") or "") or (f"{height}p" if height else f"{width}w")
    return {
        "name": name,
        "width": width,
        "height": height,
        "bitrate": int(raw.get("bitrate", 0) or 0),
        "fps": int(raw.get("fps", 0) or 0),
        "fpsDen": int(raw.get("fpsDen", 0) or 0) or 1,
        "profile": str(raw.get("profile", "") or ""),
        "gop": str(raw.get("gop", "") or ""),
        "encoder": encoder,
        "encoder_key": enc_key or "h264",
        "quality": int(raw.get("quality", 0) or 0),
        "colorDepth": int(raw.get("colorDepth", 0) or 0),
        "chromaFormat": int(raw.get("chromaFormat", 0) or 0),
        "ext": ext,
    }


# Native lpms enum values (github.com/livepeer/lpms ffmpeg.go), carried as ints
# in the profile JSON: ChromaSubsampling 420=0, 422=1, 444=2; ColorDepthBits
# 8Bit=0, 10Bit=2. NB: colorDepth is the enum value, not the bit count.
_CHROMA = {0: "420", 1: "422", 2: "444"}
_COLOR_DEPTH = {0: 8, 2: 10}


def _pix_fmt(color_depth: int, chroma: int) -> str | None:
    sub = _CHROMA.get(chroma, "420")
    depth = _COLOR_DEPTH.get(color_depth, 8)
    if depth == 8 and sub == "420":
        return None  # encoder default (yuv420p)
    return f"yuv{sub}p" + (f"{depth}le" if depth > 8 else "")


def video_args(p: dict[str, Any]) -> list[str]:
    """ffmpeg video flags for one normalized profile (scale + codec + rate control + gop)."""
    args: list[str] = []

    # resolution — 0 on either axis keeps aspect (-2 = even, preserve aspect).
    w = p["width"] or -2
    h = p["height"] or -2
    args += ["-vf", f"scale={w}:{h}"]

    args += ["-c:v", p["encoder"]]

    # H.264/H.265 profile.
    prof = _H264_PROFILE.get(p["profile"].lower(), None)
    if prof and p["encoder_key"] in ("h264", "h265", "hevc"):
        args += ["-profile:v", prof]

    # rate control: bitrate (CBR-ish) if set, else quality (CRF/CQ) if set, else sane default.
    if p["bitrate"] > 0:
        args += ["-b:v", str(p["bitrate"]), "-maxrate", str(p["bitrate"]),
                 "-bufsize", str(p["bitrate"] * 2)]
    else:
        crf = p["quality"] or _default_crf(p["encoder_key"])
        if p["encoder"] == "av1_nvenc":
            args += ["-cq", str(crf)]
        elif p["encoder_key"] == "av1":
            args += ["-crf", str(crf), "-preset", "8"]  # SVT-AV1 preset (speed/size)
        elif p["encoder_key"] in ("vp8", "vp9"):
            args += ["-crf", str(crf), "-b:v", "0"]
        else:
            args += ["-crf", str(crf)]

    # frame rate.
    if p["fps"] > 0:
        args += ["-r", f"{p['fps']}/{p['fpsDen']}"]

    # GOP / keyframe interval: "intra" = all-intra; a number = seconds between keyframes.
    gop = p["gop"].lower()
    if gop == "intra":
        args += ["-g", "1"]
    elif gop:
        try:
            secs = float(gop)
            args += ["-force_key_frames", f"expr:gte(t,n_forced*{secs})"]
        except ValueError:
            pass

    pix = _pix_fmt(p["colorDepth"], p["chromaFormat"])
    if pix:
        args += ["-pix_fmt", pix]

    return args


def ffmpeg_profile(p: dict[str, Any]) -> str | None:
    """Native H.264/H.265 profile -> ffmpeg -profile:v value (None if N/A)."""
    prof = _H264_PROFILE.get(p["profile"].lower())
    return prof if prof and p["encoder_key"] in ("h264", "h265", "hevc") else None


def _default_crf(encoder_key: str) -> int:
    return {"av1": 30, "vp9": 32, "vp8": 10, "h265": 26}.get(encoder_key, 23)


def audio_args(ext: str) -> list[str]:
    # Re-encode audio to a container-appropriate codec (parity with the VOD example).
    return ["-c:a", "libopus", "-b:a", "128k"] if ext in ("webm", "mkv") else ["-c:a", "aac", "-b:a", "128k"]
