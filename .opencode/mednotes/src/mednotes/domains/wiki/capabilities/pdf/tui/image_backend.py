"""Terminal image backend detection."""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageBackend:
    name: str
    can_render: bool


def detect(*, preferred: str = "auto", env: Mapping[str, str] | None = None) -> ImageBackend:
    env = env or os.environ
    if preferred == "none":
        return ImageBackend("none", False)
    if preferred in {"sixel", "tgp", "halfcell", "chafa", "ascii"}:
        return ImageBackend(preferred, preferred != "ascii" or True)
    term = (env.get("TERM") or "").lower()
    if "sixel" in term:
        return ImageBackend("sixel", True)
    if env.get("KITTY_WINDOW_ID"):
        return ImageBackend("tgp", True)
    if shutil.which("chafa"):
        return ImageBackend("chafa", True)
    return ImageBackend("none", False)
