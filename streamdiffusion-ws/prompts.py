"""Curated, SFW prompt bank for the auto-cycler.

Deliberately *not* an LLM/prompt-model: for a public live stream you want
deterministic, safe output, so every token here is hand-vetted. It's a template
combinator (style x modifier) — small banks, large variety. Because this is
img2img restyle, prompts are **style-forward** (the subject comes from the video),
not scene descriptions.

Edit the lists to taste; keep them SFW.
"""
from __future__ import annotations

import random

STYLES = [
    "watercolor painting, soft washes",
    "van Gogh oil painting, thick swirling brushstrokes",
    "Japanese ukiyo-e woodblock print",
    "anime cel shading, clean linework",
    "cyberpunk neon, glowing signage",
    "synthwave retrowave, chrome and sunset",
    "impressionist painting, dappled light",
    "8-bit pixel art",
    "low-poly 3D render",
    "stained glass window, leaded glass",
    "claymation, plasticine texture",
    "origami paper craft, folded paper",
    "charcoal sketch, smudged shading",
    "comic book ink, bold halftone",
    "psychedelic fractal art",
    "art deco poster, geometric gold",
    "vaporwave aesthetic, pastel gradients",
    "oil pastel drawing",
    "mosaic tile art",
    "papercut layered art, paper shadows",
    "gouache storybook illustration",
    "blueprint schematic, cyan lines on navy",
    "chalk pastel on black paper",
    "art nouveau, flowing organic lines",
    "cubist abstraction, fragmented planes",
    "pointillism, tiny dots of color",
    "autumn forest oil painting",
    "frosted ice and crystal, glassy",
]

MODIFIERS = [
    "vivid saturated colors",
    "soft pastel palette",
    "dramatic golden-hour light",
    "cool moonlit blue tones",
    "dreamy glowing haze",
    "high contrast, bold shadows",
    "warm cozy tones",
    "iridescent shimmer",
]

SUFFIX = "highly detailed, masterpiece"


def random_prompt(prev: str | None = None) -> str:
    """A random style x modifier prompt; avoids repeating the previous one."""
    for _ in range(8):
        prompt = f"{random.choice(STYLES)}, {random.choice(MODIFIERS)}, {SUFFIX}"
        if prompt != prev:
            return prompt
    return prompt
