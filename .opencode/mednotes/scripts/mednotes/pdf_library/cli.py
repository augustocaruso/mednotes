#!/usr/bin/env python3
"""Entry-point fino do PDF library CLI. A lógica vive em mednotes.domains.wiki.capabilities.pdf.cli."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_paths import ensure_runtime_paths

ensure_runtime_paths()

from mednotes.domains.wiki.capabilities.pdf.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
