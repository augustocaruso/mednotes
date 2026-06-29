#!/usr/bin/env python3
"""Public CLI alias for workflow feedback reports."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_paths import ensure_runtime_paths

ensure_runtime_paths()

from mednotes.platform.feedback.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
