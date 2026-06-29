"""Paths for the local PDF library state."""
from __future__ import annotations

import os
from pathlib import Path

APP_STATE_SUBDIR = Path(".mednotes")
PDF_LIBRARY_SUBDIR = Path("pdf-library")


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home()).expanduser()


def app_state_dir() -> Path:
    return home_dir() / APP_STATE_SUBDIR


def app_home() -> Path:
    return app_state_dir() / PDF_LIBRARY_SUBDIR


def app_config_path() -> Path:
    return app_state_dir() / "config.toml"


def database_path(base: Path | None = None) -> Path:
    root = base or app_home()
    return root / "library.sqlite3"


def extension_root() -> Path:
    from mednotes.platform.paths import extension_root as _root

    return _root()
