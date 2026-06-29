"""Runtime trust helpers for Workbench-mediated specialist calls."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_TRUSTED_GEMINI_BINARY_NAMES = frozenset({"gemini", "gemini.cmd", "gemini.exe"})


def specialist_dev_escape_enabled() -> bool:
    return os.environ.get("MEDNOTES_ALLOW_DEV_ESCAPE", "").strip() == "1"


def gemini_binary_identity(binary: str) -> str:
    value = binary.strip()
    if not value:
        return ""
    if "/" in value or "\\" in value:
        return str(Path(value).expanduser().resolve(strict=False))
    resolved = shutil.which(value)
    return str(Path(resolved).resolve(strict=False)) if resolved else value


def gemini_binary_is_public_trusted(binary: str) -> bool:
    value = binary.strip()
    if not value:
        return False
    if value in _TRUSTED_GEMINI_BINARY_NAMES:
        return True
    default = shutil.which("gemini")
    if not default:
        return False
    default_identity = str(Path(default).resolve(strict=False))
    return gemini_binary_identity(value) == default_identity


def gemini_binary_override_block_reason(binary: str) -> str:
    if gemini_binary_is_public_trusted(binary):
        return ""
    if specialist_dev_escape_enabled():
        return ""
    return "specialist_runner_untrusted_gemini_binary"


def transcript_command_untrusted_gemini_binary(command: object) -> str:
    if not isinstance(command, list) or not command:
        return "missing_command"
    binary = command[0]
    if not isinstance(binary, str) or not binary.strip():
        return "missing_binary"
    return "" if gemini_binary_is_public_trusted(binary) else binary.strip()
