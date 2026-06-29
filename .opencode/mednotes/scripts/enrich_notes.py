#!/usr/bin/env python3
"""Public CLI alias for the image enrichment workflow.

Thin entrypoint: it only resolves the vendored library path and delegates to
``enrich.workflow.cli``. The enrichment library lives at the canonical
``src/enrich`` and is vendored into the extension's ``src/`` dir at build time;
here we put that dir on ``sys.path`` so the import resolves in the shipped
extension (and falls through to the editable install in dev).
See ADR-0001 rule 10 (soft layout).
"""
from __future__ import annotations

import sys
from pathlib import Path

_VENDORED_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_VENDORED_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDORED_SRC))

from mednotes.domains.wiki.flows.enrich.workflow.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
