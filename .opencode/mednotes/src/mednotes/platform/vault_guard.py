"""Shared vault guard checks for official mutating CLIs."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from mednotes.kernel.base import ContractModel
from mednotes.platform.paths import user_state_dir

LEASE_SUBDIR = Path("vault-guard") / "leases"
LEASE_SCHEMA = "medical-notes-workbench.vault-guard-lease.v1"
BLOCK_SCHEMA = "medical-notes-workbench.vault-guard-block.v1"
LEASE_REFRESH_MINUTES = 12 * 60
EXIT_VALIDATION = 3


class _VaultGuardLeaseFields(ContractModel):
    """Typed lease fields used to decide whether vault mutation is guarded."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: str = Field(default="", alias="schema")
    status: str = ""
    vault_dir: str = ""
    expires_at: str = ""


class VaultGuardError(RuntimeError):
    """Raised when a mutating command targets the configured vault without a guard."""

    exit_code = EXIT_VALIDATION

    def __init__(self, vault_dir: Path, *, workflow: str | None = None, command: str | None = None) -> None:
        self.vault_dir = Path(vault_dir)
        self.workflow = workflow or ""
        self.command = command or ""
        super().__init__("vault_guard_required")

    def to_payload(self) -> dict[str, object]:
        workflow = self.workflow or "<workflow>"
        recovery_command = (
            "uv run python scripts/vault/vault_git.py run-start "
            f"--agent gemini-cli --workflow {workflow} --json"
        )
        return {
            "schema": BLOCK_SCHEMA,
            "status": "blocked_vault_guard_required",
            "blocked_reason": "vault_guard_required",
            "human_message": (
                "Bloqueei esta alteração porque ainda não existe ponto de restauração ativo para este run."
            ),
            "next_action": (
                "Abrir um ponto de restauração para este run e repetir a operação. "
                "Faça isso uma vez por lote, não por nota."
            ),
            "agent_message": (
                "Abra o guard com run-start uma vez por lote no começo do workflow, execute todas as mutações, "
                "e feche com run-finish uma vez por lote no final."
            ),
            "recovery_command": recovery_command,
            "required_inputs": ["agent", "workflow"],
            "human_decision_required": False,
            "workflow": self.workflow,
            "command": self.command,
        }


def active_guard_exists(vault_dir: Path) -> bool:
    return any(_matching_active_guard_leases(vault_dir))


def refresh_active_guard(vault_dir: Path, *, workflow: str, command: str) -> bool:
    now = datetime.now(UTC)
    refreshed_until = now + timedelta(minutes=LEASE_REFRESH_MINUTES)
    refreshed = False
    for path, payload, expires_at in _matching_active_guard_leases(vault_dir, now=now):
        if expires_at < refreshed_until:
            payload["expires_at"] = _dt_iso(refreshed_until)
        payload["last_seen_at"] = _dt_iso(now)
        payload["last_seen_workflow"] = workflow
        payload["last_seen_command"] = command
        _write_json(path, payload)
        refreshed = True
    return refreshed


def _matching_active_guard_leases(
    vault_dir: Path,
    *,
    now: datetime | None = None,
) -> list[tuple[Path, dict[str, Any], datetime]]:
    target = _norm_path(vault_dir)
    if not target:
        return []
    observed_at = now or datetime.now(UTC)
    leases: list[tuple[Path, dict[str, Any], datetime]] = []
    for path in _lease_dir().glob("*.json"):
        payload = _read_json(path)
        if not payload:
            continue
        lease = _VaultGuardLeaseFields.model_validate(payload)
        if lease.schema_id != LEASE_SCHEMA:
            continue
        if lease.status != "active":
            continue
        lease_vault = _norm_path(Path(lease.vault_dir))
        if not _inside_or_same(target, lease_vault):
            continue
        expires_at = _parse_dt(lease.expires_at)
        if expires_at is None:
            continue
        if expires_at <= observed_at:
            continue
        leases.append((path, payload, expires_at))
    return leases


def require_vault_guard(vault_dir: Path, *, workflow: str, command: str) -> None:
    if _maintainer_bypass_enabled():
        return
    if not _targets_configured_vault(vault_dir):
        return
    if not refresh_active_guard(vault_dir, workflow=workflow, command=command):
        raise VaultGuardError(vault_dir, workflow=workflow, command=command)


def _state_dir() -> Path:
    return user_state_dir()


def _lease_dir() -> Path:
    return _state_dir() / LEASE_SUBDIR


def _configured_vault_path() -> Path | None:
    try:
        text = (_state_dir() / "vault.path").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return Path(stripped)
    return None


def _targets_configured_vault(vault_dir: Path) -> bool:
    configured = _configured_vault_path()
    if configured is None:
        return False
    return _inside_or_same(_norm_path(vault_dir), _norm_path(configured))


def _norm_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve(strict=False))
    except OSError:
        return str(path.expanduser().absolute())


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if os.name == "nt":
        left = left.lower()
        right = right.lower()
    return left == right


def _inside_or_same(candidate: str, root: str) -> bool:
    if not candidate or not root:
        return False
    if os.name == "nt":
        candidate = candidate.lower()
        root = root.lower()
    root_prefix = root.rstrip("/\\") + os.sep
    return candidate == root or candidate.startswith(root_prefix)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dt_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _maintainer_bypass_enabled() -> bool:
    return (
        os.environ.get("MEDNOTES_VAULT_GUARD_DISABLE") == "1"
        and bool(os.environ.get("MEDNOTES_VAULT_GUARD_DISABLE_REASON", "").strip())
    )
