#!/usr/bin/env python3
"""Fail if an example's restated orchestrator command has drifted from the shared one.

`compose.orchestrator.yml` and `compose.onchain.yml` define the orchestrator once and
examples pull it in with `extends`. Static runners cannot: they need an extra
`-liveRunnerConfig` flag, and `extends` replaces a command list rather than appending
to it, so they restate the whole thing. This checks those copies still match.

Run directly, or via pre-commit. Exits non-zero and prints what differs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

# Flags a copy may add to the shared command. Anything else is drift.
ALLOWED_EXTRA = {"-liveRunnerConfig"}

SHARED = {
    "compose.yml": Path("compose.orchestrator.yml"),
    "compose.onchain.yml": Path("compose.onchain.yml"),
}


def _command(path: Path) -> list[str] | None:
    doc = yaml.safe_load(path.read_text()) or {}
    service = (doc.get("services") or {}).get("orchestrator") or {}
    command = service.get("command")
    return command if isinstance(command, list) else None


def _flag(item: str) -> str:
    return str(item).split("=", 1)[0]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "*/compose.yml", "*/compose.onchain.yml"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    problems: list[str] = []
    checked = 0
    for rel in sorted(tracked):
        path = root / rel
        copy = _command(path)
        if copy is None:  # uses `extends` alone, nothing to drift
            continue
        shared_path = root / SHARED[Path(rel).name]
        shared = _command(shared_path) or []
        checked += 1

        shared_by_flag = {_flag(i): i for i in shared}
        copy_by_flag = {_flag(i): i for i in copy}

        for flag, item in shared_by_flag.items():
            if flag not in copy_by_flag:
                problems.append(f"{rel}: missing {item!r} (in {shared_path.name})")
            elif copy_by_flag[flag] != item:
                problems.append(
                    f"{rel}: {flag} is {copy_by_flag[flag]!r}, "
                    f"{shared_path.name} has {item!r}"
                )
        for flag, item in copy_by_flag.items():
            if flag not in shared_by_flag and flag not in ALLOWED_EXTRA:
                problems.append(f"{rel}: unexpected {item!r} not in {shared_path.name}")

    if problems:
        print("orchestrator command drift:\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\nThese examples restate the shared command because they add "
            f"{sorted(ALLOWED_EXTRA)} and `extends` cannot append to a list. "
            "Re-copy the shared command, or widen ALLOWED_EXTRA if the new flag "
            "is deliberate."
        )
        return 1

    print(f"orchestrator command: {checked} restated copies match the shared files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
