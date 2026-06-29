"""Load the central vault guard for the enrichment workflow.

O vault guard agora é uma lib importável (``mednotes.platform.vault_guard``);
não há mais necessidade de caçar ``vault_guard.py`` no sys.path (o bootstrap
antigo morreu com a consolidação em bundle/src — ADR-0001 regra 10).
"""
from __future__ import annotations

from pathlib import Path

from mednotes.platform.vault_guard import VaultGuardError, require_vault_guard

__all__ = ["VaultGuardError", "require_enrich_guard"]


def require_enrich_guard(target: Path, *, command: str) -> None:
    require_vault_guard(target, workflow="/mednotes:enrich", command=command)
