#!/usr/bin/env python3
"""Entry-point fino do Wiki CLI. A lógica mora em mednotes.domains.wiki.cli.

Skills/commands invocam este caminho (scripts/mednotes/wiki/cli.py); aqui só
bootstrapamos o sys.path (bundle/src) e delegamos.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_paths import ensure_runtime_paths

ensure_runtime_paths()

from mednotes.domains.wiki.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
