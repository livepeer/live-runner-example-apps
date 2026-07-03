#!/usr/bin/env python3
# Self-contained StreamDiffusion driver.
#
# Wraps ONLY the upstream `streamdiffusion` library (StreamDiffusionWrapper) for
# realtime prompt-driven img2img. No ai-runner code. The wrapper builds/loads
# its own TensorRT engines into ENGINE_DIR, so this module is the entire
# inference dependency surface of the example.
#
# CLI:  python sd.py --build [--model ...] [--width 512] [--height 512]
#       Constructs the wrapper with build_engines_if_missing=True and exits,
#       compiling TensorRT engines for this GPU into ENGINE_DIR (the slow step,
#       done once). build_engines.sh runs this inside the container.
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

ENGINE_DIR = os.environ.get("SD_ENGINE_DIR", "/models/engines")

# Sensible realtime defaults; sd-turbo is light enough for one 3090.
DEFAULT_MODEL = os.environ.get("SD_MODEL", "stabilityai/sd-turbo")
DEFAULT_PROMPT = "a psychedelic landscape, vivid colors, intricate details"
DEFAULT_NEGATIVE = "low quality, blurry, flat"
DEFAULT_T_INDEX = [32, 45]  # 2 denoise steps — realtime sweet spot for sd-turbo


class StreamDiffusion:
    """Minimal realtime img2img over StreamDiffusionWrapper."""

    def __init__(self) -> None:
        self.wrapper = None
        self.width = 512
        self.height = 512
        self.prompt = DEFAULT_PROMPT

    def load(
        self,
        *,
        model: str = DEFAULT_MODEL,
        prompt: str = DEFAULT_PROMPT,
        negative_prompt: str = DEFAULT_NEGATIVE,
        width: int = 512,
        height: int = 512,
        t_index_list: Optional[list[int]] = None,
        acceleration: str = "tensorrt",
        num_inference_steps: int = 50,
        guidance_scale: float = 1.2,
        delta: float = 0.5,
        seed: int = 789,
        build_engines: bool = False,
    ) -> None:
        from streamdiffusion import StreamDiffusionWrapper

        self.width, self.height, self.prompt = width, height, prompt
        self.wrapper = StreamDiffusionWrapper(
            model_id_or_path=model,
            t_index_list=t_index_list or DEFAULT_T_INDEX,
            frame_buffer_size=1,
            width=width,
            height=height,
            warmup=10,
            acceleration=acceleration,
            mode="img2img",
            output_type="pt",
            use_denoising_batch=True,
            cfg_type="self",
            do_add_noise=False,          # ai-runner: skip per-step noise -> less frame flicker
            seed=seed,
            engine_dir=ENGINE_DIR,
            build_engines_if_missing=build_engines,
        )
        self.wrapper.prepare(
            prompt=prompt,
            negative_prompt=negative_prompt,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            delta=delta,
        )

    def update_prompt(self, prompt: str) -> None:
        if self.wrapper is None:
            return
        self.prompt = prompt
        # prepare() re-encodes the prompt; cheap relative to model reload.
        self.wrapper.prepare(prompt=prompt)

    def process(self, rgb: np.ndarray) -> np.ndarray:
        """(H,W,3) uint8 RGB in -> (H,W,3) uint8 RGB out."""
        if self.wrapper is None:
            raise RuntimeError("pipeline not loaded")
        img = self.wrapper.preprocess_image(Image.fromarray(rgb))
        # preprocess_image outputs [-1,1], but the pipeline expects [0,1] (the
        # deprecation warning in the logs). Rescale so the model gets correctly-ranged
        # input -> noticeably better output. (Matches ai-runner's denormalize step.)
        img = ((img + 1.0) / 2.0).clamp(0.0, 1.0)
        # Prime the stateful streaming pipeline once so the first frames aren't cold.
        if not getattr(self, "_primed", False):
            self._primed = True
            for _ in range(int(getattr(self.wrapper, "batch_size", 1) or 1)):
                self.wrapper(image=img)
        out = self.wrapper(image=img)              # output_type="pt" -> tensor [0,1]
        if isinstance(out, list):
            out = out[0]
        t = out.detach()
        if t.dim() == 4:
            t = t.squeeze(0)                        # (C,H,W)
        t = t.permute(1, 2, 0).clamp(0, 1).mul(255).byte().cpu().numpy()  # (H,W,3)
        return np.ascontiguousarray(t)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StreamDiffusion engine build / smoke test.")
    p.add_argument("--build", action="store_true", help="Build TensorRT engines for this GPU and exit.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--width", type=int, default=512)
    p.add_argument("--height", type=int, default=512)
    p.add_argument("--acceleration", default="tensorrt")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    Path(ENGINE_DIR).mkdir(parents=True, exist_ok=True)
    sd = StreamDiffusion()
    print(f"Loading StreamDiffusion model={args.model} {args.width}x{args.height} "
          f"acceleration={args.acceleration} (build_engines={args.build}) into {ENGINE_DIR}")
    sd.load(model=args.model, width=args.width, height=args.height,
            acceleration=args.acceleration, build_engines=args.build)
    # Smoke-test one frame so a build also validates inference.
    out = sd.process(np.zeros((args.height, args.width, 3), dtype=np.uint8))
    print(f"OK: produced frame {out.shape} dtype={out.dtype}")


if __name__ == "__main__":
    main()
