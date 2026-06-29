#!/usr/bin/env python3
"""Compatibility CLI alias for Wiki graph audit."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_paths import ensure_runtime_paths  # noqa: E402

ensure_runtime_paths()

from mednotes.domains.wiki.api import graph_main as main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
