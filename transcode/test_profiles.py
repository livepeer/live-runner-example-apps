"""Unit tests for the native-profile -> ffmpeg mapping (the contract).

Pure logic, no ffmpeg needed. These pin the fidelity to go-livepeer's
JsonProfile so the "drop-in for the native pipeline" claim stays true.

    python3 -m pytest test_profiles.py -q
"""

from __future__ import annotations

import pytest

import profiles as prof


def test_encoder_and_container_mapping():
    assert prof.normalize({"height": 720})["encoder"] == "libx264"  # default H264
    assert prof.normalize({"height": 720})["ext"] == "mp4"
    assert prof.normalize({"height": 720, "encoder": "H265"})["encoder"] == "libx265"
    assert prof.normalize({"height": 720, "encoder": "VP9"})["ext"] == "webm"
    av1 = prof.normalize({"height": 720, "encoder": "AV1"}, av1_encoder="av1_nvenc")
    assert av1["encoder"] == "av1_nvenc" and av1["ext"] == "mkv"


def test_scale_keeps_aspect_when_axis_zero():
    # 0 on an axis -> -2 (even, preserve aspect)
    assert "-vf" in prof.video_args(prof.normalize({"height": 360}))
    args = prof.video_args(prof.normalize({"width": 640, "height": 0}))
    assert "scale=640:-2" in args
    args = prof.video_args(prof.normalize({"height": 360}))
    assert "scale=-2:360" in args


def test_rate_control_bitrate_then_quality_then_default():
    a = prof.video_args(prof.normalize({"height": 720, "bitrate": 3_000_000}))
    assert "-b:v" in a and "3000000" in a
    q = prof.video_args(prof.normalize({"height": 720, "quality": 27}))
    assert "-crf" in q and "27" in q
    d = prof.video_args(prof.normalize({"height": 720}))
    assert "-crf" in d and "23" in d  # x264 default


def test_h264_profile_mapping():
    a = prof.video_args(prof.normalize({"height": 720, "profile": "H264High"}))
    assert a[a.index("-profile:v") + 1] == "high"
    # a codec with no such profile ignores it
    assert prof.ffmpeg_profile(prof.normalize({"height": 720, "encoder": "AV1", "profile": "H264High"})) is None


def test_gop_intra_and_seconds():
    intra = prof.video_args(prof.normalize({"height": 720, "gop": "intra"}))
    assert intra[intra.index("-g") + 1] == "1"
    secs = prof.video_args(prof.normalize({"height": 720, "gop": "2.0"}))
    assert any("n_forced*2.0" in x for x in secs)


def test_pix_fmt_matches_lpms_enum():
    # chroma: 0=420, 1=422, 2=444 ; colorDepth: 0=8bit, 2=10bit (enum values!)
    assert prof._pix_fmt(0, 0) is None          # 8-bit 420 -> encoder default
    assert prof._pix_fmt(0, 1) == "yuv422p"
    assert prof._pix_fmt(0, 2) == "yuv444p"
    assert prof._pix_fmt(2, 0) == "yuv420p10le"  # 10-bit is enum 2, not 10
    assert prof._pix_fmt(2, 2) == "yuv444p10le"


def test_rejects_profile_without_dimensions():
    with pytest.raises(ValueError):
        prof.normalize({"name": "bad"})
