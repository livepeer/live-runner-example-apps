"""ffmpeg batch transcode engine — one input, a native profile ladder, N renditions.

Shared by the batch (VOD) surface. Runs one ffmpeg per rendition (robust and
parallelizable); the live surface decodes once and fans out over trickle instead.
"""

from __future__ import annotations

import os
import subprocess

import profiles as prof

# ffmpeg binary. Intel VA-API AV1 on Arc needs the patched media stack that
# jellyfin-ffmpeg ships (distro ffmpeg's scale_vaapi/av1_qsv are broken there), so
# Dockerfile.intel points FFMPEG_BIN at it; NVENC/CPU builds use plain ffmpeg.
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

# VA-API render node (Intel). Override per host: an Arc dGPU is often renderD129,
# an integrated GPU renderD128.
RENDER_DEVICE = os.environ.get("RENDER_DEVICE", "/dev/dri/renderD128")

# VA-API only: decode + scale on the GPU too (not just encode), so the whole
# pipeline stays on VA surfaces and the CPU is left near-idle. Off = sw decode +
# GPU encode, which is more forgiving on odd/corrupt inputs. Dockerfile.intel
# defaults it on.
HWACCEL_DECODE = os.environ.get("HWACCEL_DECODE", "0") == "1"


def transcode_file(input_path: str, raw_profiles: list[dict], out_dir: str,
                   av1_encoder: str = "libsvtav1", h264_encoder: str = "libx264",
                   h265_encoder: str = "libx265", gpu_index: int | None = None) -> list[dict]:
    """Transcode `input_path` into one file per profile under `out_dir`.

    Returns a rendition list: [{name, path, width, height, encoder, bytes}, ...].
    Raises RuntimeError with ffmpeg stderr on failure.
    """
    if not raw_profiles:
        raise ValueError("no profiles given")
    # One ffmpeg per rendition: simple and trivially parallelizable. Production
    # would decode once and fan out (ffmpeg split filter) to save the repeat decode.
    renditions: list[dict] = []
    for raw in raw_profiles:
        p = prof.normalize(raw, av1_encoder=av1_encoder, h264_encoder=h264_encoder, h265_encoder=h265_encoder)
        out_path = os.path.join(out_dir, f"{p['name']}.{p['ext']}")
        cmd = [FFMPEG, "-y"]
        if "nvenc" in p["encoder"]:
            cmd += ["-hwaccel", "cuda"]
            if gpu_index is not None:
                cmd += ["-hwaccel_device", str(gpu_index)]
        elif "vaapi" in p["encoder"]:
            if HWACCEL_DECODE:
                # decode + scale + encode all on the GPU (VA surfaces end to end).
                cmd += ["-hwaccel", "vaapi", "-hwaccel_device", RENDER_DEVICE,
                        "-hwaccel_output_format", "vaapi"]
            else:
                # sw decode, GPU encode (video_args uploads the frame to a VA surface).
                cmd += ["-vaapi_device", RENDER_DEVICE]
        cmd += ["-i", input_path]
        cmd += prof.video_args(p, gpu_index=gpu_index,
                               hw_decode=(HWACCEL_DECODE and "vaapi" in p["encoder"]))
        cmd += prof.audio_args(p["ext"])
        if p["ext"] == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += [out_path]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            msg = result.stderr.decode(errors="replace")[-800:]
            raise RuntimeError(f"ffmpeg failed for rendition {p['name']}: {msg}")
        renditions.append({
            "name": p["name"],
            "path": out_path,
            "ext": p["ext"],
            "width": p["width"],
            "height": p["height"],
            "encoder": p["encoder"],
            "bytes": os.path.getsize(out_path),
        })
    return renditions
