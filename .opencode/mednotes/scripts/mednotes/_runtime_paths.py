"""Bootstrap de sys.path para os entry-points distribuídos (chamados por caminho).

Os scripts em ``bundle/scripts`` são invocados por caminho
(``uv run python .../X.py``), sem o pacote instalado. Este helper garante que
tanto o runtime (``bundle/scripts/mednotes`` — pacotes wiki/flashcards/...)
quanto as libs (``bundle/src`` — o namespace ``mednotes.*``) estejam no
``sys.path``. Fonte ÚNICA do path-bootstrap (ADR-0001 regra 10): em vez de cada
entry recalcular ``parents[N]``, todos chamam :func:`ensure_runtime_paths`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_RUNTIME = Path(__file__).resolve().parent      # bundle/scripts/mednotes (pacotes do bounded context)
_LIB_SRC = _RUNTIME.parents[1] / "src"          # bundle/src (namespace mednotes.*)


def ensure_runtime_paths() -> None:
    """Põe o runtime e as libs no sys.path (idempotente)."""
    for root in (_RUNTIME, _LIB_SRC):
        marker = str(root)
        if marker not in sys.path:
            sys.path.insert(0, marker)
