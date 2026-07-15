#!/usr/bin/env python3
"""Generate a small synthetic screen recording to test the app with.

Draws a fake checkout form where a "ZIP code is required" error appears —
enough visual structure for the analyzer to sample frames and build a report.
No Livepeer code here; it's just test-input generation.
"""

from __future__ import annotations

import argparse

import cv2
import numpy as np

WIDTH, HEIGHT = 1280, 720


def _frame(t: float, seconds: float) -> np.ndarray:
    img = np.full((HEIGHT, WIDTH, 3), (245, 246, 248), dtype=np.uint8)
    cv2.rectangle(img, (390, 120), (890, 600), (255, 255, 255), -1)
    cv2.rectangle(img, (390, 120), (890, 600), (210, 210, 214), 2)
    cv2.putText(img, "Acme Checkout", (430, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 46), 2)
    cv2.rectangle(img, (430, 220), (850, 270), (235, 236, 240), -1)
    cv2.putText(img, "Card number  4242 4242 4242 4242", (445, 252),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (90, 90, 98), 1)
    cv2.rectangle(img, (430, 300), (850, 350), (235, 236, 240), -1)
    cv2.putText(img, "ZIP code", (445, 332), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (150, 150, 158), 1)
    # The bug appears in the second half of the recording.
    if t > seconds / 2:
        cv2.putText(img, "Error: ZIP code is required", (430, 420),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 220), 2)
        button_color = (200, 200, 204)  # disabled
    else:
        button_color = (90, 170, 90)
    cv2.rectangle(img, (430, 480), (620, 540), button_color, -1)
    cv2.putText(img, "Pay $49", (465, 518), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a synthetic demo screen recording.")
    parser.add_argument("out", nargs="?", default="demo.mp4")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--fps", type=float, default=2.0)
    args = parser.parse_args()

    writer = cv2.VideoWriter(
        args.out, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (WIDTH, HEIGHT)
    )
    if not writer.isOpened():
        raise SystemExit(f"Could not create video: {args.out}")
    for i in range(int(args.seconds * args.fps)):
        writer.write(_frame(i / args.fps, args.seconds))
    writer.release()
    print(args.out)


if __name__ == "__main__":
    main()
