#!/usr/bin/env python3
"""Direct test client for the audio-diarized-transcription-runner.

Standard-library only — no venv, no pip. Uploads an audio file to the bounded
OpenAI-compatible route with diarization ON, then prints the speaker-labeled
transcript so you can see BOTH capabilities (transcription + speaker separation)
in one response.

    python3 client.py sample.wav              # 2 speakers auto-detected
    python3 client.py sample.wav --num-speakers 2
    python3 client.py sample.wav --raw        # dump the full JSON

Talks straight to the runner (default http://localhost:8080). It does NOT go
through a Livepeer orchestrator: the bounded route is a multipart file upload,
which the current gateway SDK's JSON-only call_runner can't forward yet. See
README.md ("Testing through an orchestrator").
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _multipart_body(boundary: str, audio_path: Path, fields: dict[str, str]) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(audio_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_path")
    parser.add_argument("--runner-url", default="http://localhost:8080")
    parser.add_argument(
        "--num-speakers", type=int, help="Exact speaker count, if known."
    )
    parser.add_argument("--max-speakers", type=int, default=8)
    parser.add_argument(
        "--raw", action="store_true", help="Print the full JSON response."
    )
    args = parser.parse_args()

    audio_path = Path(args.audio_path)
    if not audio_path.is_file():
        sys.stderr.write(f"no such file: {audio_path}\n")
        return 2

    boundary = f"----livepeer-{uuid.uuid4().hex}"
    fields = {
        "model": "nemo-diarized-transcription-meeting-v0",
        "language": "en",
        "preset": "meeting",
        "response_format": "verbose_json",
        "diarization": "true",
        "timestamp_granularities[]": "segment,word",
        "include_words": "true",
        "max_speakers": str(args.max_speakers),
    }
    if args.num_speakers:
        fields["num_speakers"] = str(args.num_speakers)

    request = urllib.request.Request(
        f"{args.runner_url.rstrip('/')}/v1/audio/transcriptions",
        data=_multipart_body(boundary, audio_path, fields),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        # First call downloads NeMo weights — can take ~1 min.
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        sys.stderr.write(error.read().decode("utf-8") + "\n")
        return error.code
    except urllib.error.URLError as error:
        sys.stderr.write(f"could not reach runner at {args.runner_url}: {error}\n")
        return 1

    if args.raw:
        print(json.dumps(body, indent=2))
        return 0

    diar = body.get("diarization", {})
    print(f"speakers detected: {diar.get('speaker_count')}")
    print("\nspeaker-labeled transcript:")
    print(body.get("speaker_labeled_text", "").rstrip())
    print("\nsegments:")
    for seg in body.get("segments", []):
        print(
            f"  [{seg.get('start'):6.2f}-{seg.get('end'):6.2f}] {seg.get('speaker')}: {seg.get('text')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
