"""Shared primitives for deterministic Wiki workflow operations."""
from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Generic exit codes + the MedOpsError hierarchy live in the framework; the
# linker-specific code is domain-owned so the shared kernel stays product-free.
from mednotes.kernel.errors import (  # noqa: F401
    EXIT_IO,
    EXIT_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    CollisionError,
    FileWriteError,
    MedOpsError,
    MissingPathError,
    ValidationError,
)

EXIT_LINKER = 6

MIGRATION_PLAN_SCHEMA = "medical-notes-workbench.taxonomy-migration-plan.v1"
MIGRATION_RECEIPT_SCHEMA = "medical-notes-workbench.taxonomy-migration-receipt.v1"
SUBAGENT_PLAN_SCHEMA = "medical-notes-workbench.subagent-plan.v1"
WIKI_HEALTH_FIX_SCHEMA = "medical-notes-workbench.wiki-health-fix.v1"
BLOCKER_RESOLUTION_SCHEMA = "medical-notes-workbench.blocker-resolution.v1"
NOTE_MERGE_PLAN_SCHEMA = "medical-notes-workbench.note-merge-plan.v1"
NOTE_MERGE_APPLY_SCHEMA = "medical-notes-workbench.note-merge-apply.v1"


# Exit codes and the MedOpsError hierarchy now live in mednotes.kernel.errors
# (framework); re-exported above for the many domain callers of wiki.common.


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def wiki_cli_base_command() -> str:
    """Return an executable command for the current Wiki CLI entrypoint."""
    shim = Path(__file__).resolve().parents[4] / "scripts" / "mednotes" / "wiki" / "cli.py"
    return "uv run python " + shlex.quote(str(shim))


def wiki_cli_command(*args: object) -> str:
    return " ".join([wiki_cli_base_command(), *(shlex.quote(str(arg)) for arg in args)])


# Forma RELATIVA-ao-checkout dos caminhos canônicos, para instruções voltadas ao
# agent que mostram um caminho relativo (vs. o absoluto do wiki_cli_base_command
# acima). Fonte única: ao reorganizar a árvore, atualize SÓ aqui.
# Ver ADR-0001 regra 10 (layout macio).
WIKI_CLI_RELPATH = "bundle/scripts/mednotes/wiki/cli.py"
DOCS_RELPATH = "bundle/docs"
SKILLS_RELPATH = "bundle/skills"


def wiki_cli_relative_command(command: str = "") -> str:
    """`uv run python <cli> <command>` relativo ao checkout, para instruções ao agent."""
    return f"uv run python {WIKI_CLI_RELPATH} {command}".rstrip()
