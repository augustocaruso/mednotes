"""Related Notes plugin export adapter for Wiki_Medicina."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, StrictBool, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import canonical_json_hash, file_sha256
from mednotes.domains.wiki.capabilities.graph.graph import NO_STRONG_LINKS_MARKER
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.related_notes.related_notes_headless import (
    EmbeddingClient,
    HeadlessRelatedNotesExportError,
    generate_headless_related_notes_export,
    headless_plugin_settings_available,
    normalize_related_notes_profile_id,
    related_notes_content_hash,
    related_notes_legacy_clean_v1_content_hash,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    is_index_target,
    normalize_key,
    obsidian_target_name,
)
from mednotes.domains.wiki.common import _now_iso, wiki_cli_command
from mednotes.domains.wiki.config import MedConfig
from mednotes.domains.wiki.contracts.related_notes import RelatedNotesExport, RelatedNotesHeadlessExportSummary
from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedSyncResult
from mednotes.domains.wiki.flows.link.related_notes_fsm import (
    build_related_notes_recovery_projection,
    link_related_fsm_payload_from_sync_result,
)
from mednotes.domains.wiki.performance import cooperative_cpu_yield
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.platform.paths import user_state_dir

RELATED_NOTES_EXPORT_SCHEMA = "medical-notes-workbench.related-notes-export.v1"
RELATED_NOTES_SYNC_SCHEMA = "medical-notes-workbench.related-notes-sync.v1"
RELATED_NOTES_SYNC_RECEIPT_SCHEMA = "medical-notes-workbench.related-notes-sync-receipt.v1"
RELATED_NOTES_EXPORT_RECOVERY_SCHEMA = "medical-notes-workbench.related-notes-export-recovery.v1"
RELATED_NOTES_SAFETY_CLEANUP_SCHEMA = "medical-notes-workbench.related-notes-safety-cleanup.v1"
RELATED_NOTES_RESUMABLE_BLOCKERS = {
    "related_notes_headless_quota_exhausted",
    "related_notes_headless_time_budget_exhausted",
}
DEFAULT_RELATED_NOTES_EXPORT = ".obsidian/plugins/related-notes-obsidian/medical-notes-export.json"
DEFAULT_MIN_SCORE = 0.78
DEFAULT_MAX_LINKS = 10
RELATED_NOTES_REQUIRED_INPUTS = ["wiki_dir", "related_notes_export"]
RELATED_NOTES_PLUGIN_ID = "related-notes-obsidian"
RELATED_NOTES_COMMANDS = {
    "reindex_vault": "related-notes-obsidian:reindex-vault",
    "index_missing_notes": "related-notes-obsidian:index-missing-notes",
    "export_only_diagnostic": "related-notes-obsidian:export-workbench-related-notes",
}
OBSIDIAN_PROBE_TIMEOUT_SECONDS = 30
OBSIDIAN_COMMAND_TIMEOUT_SECONDS = 120
OBSIDIAN_TIMEOUT_RETURNCODE = 124


@dataclass(frozen=True)
class _RelatedNotesExportCacheKey:
    export_path: str
    wiki_dir: str
    mtime_ns: int
    size: int
    max_age_hours: float
    allow_stale_note_hashes: bool


_RELATED_NOTES_EXPORT_CACHE: dict[_RelatedNotesExportCacheKey, _RelatedNotesExportValidation] = {}
_RELATED_NOTES_EXPORT_CACHE_MAX_ENTRIES = 8

_RELATED_HEADING_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
_NEXT_H2_RE = re.compile(r"(?m)^##\s+")
_FOOTER_RE = re.compile(r"(?m)^---\s*$")
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_EXPORT_KEYS = {
    "apikey",
    "geminiapikey",
    "token",
    "secret",
    "password",
    "content",
    "markdown",
    "rawmarkdown",
    "body",
    "vector",
    "preview",
    "embedding",
    "embeddings",
}


class _ObsidianCommandRunner(Protocol):
    """Boundary protocol for the Obsidian CLI runner used by recovery."""

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        ...


@dataclass(frozen=True)
class RelatedNote:
    rel_path: str
    abs_path: Path
    title: str
    content_hash: str


@dataclass(frozen=True)
class RelatedEdge:
    source_path: str
    target_path: str
    score: float
    rank: int
    source: str


@dataclass(frozen=True)
class _RelatedNotesParseBlocked:
    """Typed parse failure used before a sync operation can plan mutations."""

    blocked_reason: str
    next_action: str
    validation_errors: list[JsonObject]
    stale_notes: list[JsonObject]


@dataclass(frozen=True)
class _RelatedNotesParsedNotes:
    """Validated note map from the Related Notes export."""

    notes: dict[str, RelatedNote]


@dataclass(frozen=True)
class _RelatedNotesParsedEdges:
    """Validated graph edge list from the Related Notes export."""

    edges: list[RelatedEdge]


_RelatedNotesNotesParseResult = _RelatedNotesParsedNotes | _RelatedNotesParseBlocked
_RelatedNotesEdgesParseResult = _RelatedNotesParsedEdges | _RelatedNotesParseBlocked


class _RelatedNotesBlockedParseInput(ContractModel):
    """Boundary model for tests/tooling that still pass raw parse failures."""

    blocked_reason: StrictStr
    next_action: StrictStr
    validation_errors: list[JsonObject] = Field(default_factory=list)
    stale_notes: list[JsonObject] = Field(default_factory=list)

    def to_result(self) -> _RelatedNotesParseBlocked:
        return _RelatedNotesParseBlocked(
            blocked_reason=self.blocked_reason,
            next_action=self.next_action,
            validation_errors=self.validation_errors,
            stale_notes=self.stale_notes,
        )


class _RelatedNotesExportValidation(BaseModel):
    """Internal typed result for export preflight.

    The external export is JSON, but the workflow must not branch on loose
    dictionaries. This model is the boundary: callers read attributes and only
    serialize back to payload at adapter edges.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    status: Literal["ready", "blocked"]
    export_path: Path
    wiki_dir: Path
    payload: JsonObject = Field(default_factory=dict)
    notes: dict[str, RelatedNote] = Field(default_factory=dict)
    edges: list[RelatedEdge] = Field(default_factory=list)
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    hash_errors: list[JsonObject] = Field(default_factory=list)
    stale_notes: list[JsonObject] = Field(default_factory=list)
    validation_errors: list[JsonObject] = Field(default_factory=list)
    hash_warnings: list[JsonObject] = Field(default_factory=list)
    export_relocation: JsonObject = Field(default_factory=dict)
    extra_payload: JsonObject = Field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked"

    def blocked_payload(self) -> JsonObject:
        """Serialize a blocked preflight result using the public sync shape."""

        extra: dict[str, object] = dict(self.extra_payload)
        if self.hash_errors:
            extra["hash_errors"] = self.hash_errors
        if self.stale_notes:
            extra["stale_notes"] = self.stale_notes
        if self.validation_errors:
            extra["validation_errors"] = self.validation_errors
        if self.hash_warnings:
            extra["hash_warnings"] = self.hash_warnings
        if self.export_relocation:
            extra["export_relocation"] = self.export_relocation
        return _base_payload(
            self.export_path,
            self.wiki_dir,
            status="blocked",
            phase="related_notes_preflight",
            blocked_reason=self.blocked_reason,
            next_action=self.next_action,
            extra=JsonObjectAdapter.validate_python(extra),
        )


class _RelatedNotesProposedLink(ContractModel):
    """One generated Related Notes wikilink candidate."""

    target_path: StrictStr
    target_title: StrictStr
    score: float = Field(ge=0)
    rank: int = Field(ge=0)
    source: StrictStr
    content_hash: StrictStr
    line: StrictStr


class _RelatedNotesSkippedEdge(ContractModel):
    """One graph edge intentionally excluded from the rendered section."""

    source_path: StrictStr
    target_path: StrictStr = ""
    reason: StrictStr
    score: StrictStr = ""


class _RelatedNotesPlannedUpdate(ContractModel):
    """Private mutation plan for one note; `new_content` never enters preview."""

    file: StrictStr
    relative_path: StrictStr
    source_title: StrictStr
    content_hash: StrictStr
    cleared_links: list[StrictStr] = Field(default_factory=list)
    cleared_link_count: int = Field(default=0, ge=0)
    proposed_links: list[_RelatedNotesProposedLink] = Field(default_factory=list)
    new_content: StrictStr
    changed: bool = Field(strict=True)
    min_score: float = Field(ge=0)

    def public_update(self) -> _RelatedNotesPublicUpdate:
        return _RelatedNotesPublicUpdate.model_validate(
            {
                "file": self.file,
                "relative_path": self.relative_path,
                "source_title": self.source_title,
                "content_hash": self.content_hash,
                "cleared_links": self.cleared_links,
                "cleared_link_count": self.cleared_link_count,
                "proposed_links": [link.to_payload() for link in self.proposed_links],
                "changed": self.changed,
                "min_score": self.min_score,
            }
        )


class _RelatedNotesPublicUpdate(ContractModel):
    """Preview/apply receipt shape for a Related Notes section update."""

    file: StrictStr
    relative_path: StrictStr
    source_title: StrictStr
    content_hash: StrictStr
    cleared_links: list[StrictStr] = Field(default_factory=list)
    cleared_link_count: int = Field(default=0, ge=0)
    proposed_links: list[JsonObject] = Field(default_factory=list)
    changed: bool = Field(strict=True)
    min_score: float = Field(ge=0)
    backup_path: StrictStr = ""
    applied: bool = Field(default=False, strict=True)


class _RelatedNotesUpdatePlan(BaseModel):
    """Typed sync plan; private content stays private until the apply adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["preview_ready"] = "preview_ready"
    wiki_dir: StrictStr
    updates: list[_RelatedNotesPublicUpdate] = Field(default_factory=list)
    private_updates: list[_RelatedNotesPlannedUpdate] = Field(default_factory=list)
    skipped_edges: list[_RelatedNotesSkippedEdge] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "planned_note_count": len(self.updates),
            "proposed_link_count": sum(len(update.proposed_links) for update in self.updates),
            "cleared_link_count": sum(update.cleared_link_count for update in self.updates),
            "skipped_edge_count": len(self.skipped_edges),
            "applied_note_count": 0,
        }

    def public_updates_payload(self) -> list[JsonObject]:
        return [update.to_payload() for update in self.updates]

    def skipped_edges_payload(self) -> list[JsonObject]:
        return [edge.to_payload() for edge in self.skipped_edges]


class _RelatedNotesSyncReceiptPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    generated_at: StrictStr
    status: StrictStr
    phase: StrictStr
    dry_run: StrictBool
    no_resource_mutation: StrictBool
    wiki_dir: StrictStr
    export_path: StrictStr
    export_hash: StrictStr
    export_generated_at: StrictStr = ""
    plugin: JsonObject = Field(default_factory=dict)
    model: JsonObject = Field(default_factory=dict)
    api_calls: NonNegativeInt = 0
    api_failures: NonNegativeInt = 0
    plan_hash: StrictStr
    applied_note_count: NonNegativeInt = 0
    update_count: NonNegativeInt = 0
    updates: list[JsonObject] = Field(default_factory=list)


class _RelatedNotesAppliedUpdatesPayload(ContractModel):
    updates: list[JsonObject] = Field(default_factory=list)


def default_export_path(wiki_dir: Path) -> Path:
    return wiki_dir / DEFAULT_RELATED_NOTES_EXPORT


def _link_related_run_id(result: LinkRelatedSyncResult) -> str:
    basis = result.export_path or result.receipt_path or result.blocked_reason or "run"
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", basis)[:48].strip("-")
    return f"link-related-{safe or 'run'}"


def _link_related_version_control_safety(result: LinkRelatedSyncResult, *, applying: bool) -> dict[str, object]:
    changed_update_count = sum(1 for update in result.updates if update.changed)
    mutated = applying and (result.applied_note_count > 0 or changed_update_count > 0)
    return {
        "resource_guard_active": mutated,
        "run_start_seen": mutated,
        "run_finish_seen": mutated,
        "restore_point_before": "vault-guard" if mutated else "",
        "restore_point_after": "vault-guard" if mutated else "",
        "sync_status": "not_checked",
        "backup_online": "not_checked",
        "direct_mutation_forbidden": True,
        "mutation_without_guard": False,
        "rollback_declared": mutated,
        "no_resource_mutation": not mutated,
        "changed_file_count": changed_update_count,
    }


def _json_str_field(payload: JsonObject, key: str, default: str = "") -> str:
    """Read a public JSON string after validating the object boundary."""

    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        return default
    return str(value)


def recover_related_notes_export(
    config: MedConfig,
    *,
    export_path: Path | None = None,
    mode: str = "auto",
    command_runner: _ObsidianCommandRunner | None = None,
    headless_embedding_client: EmbeddingClient | None = None,
    headless_now_iso: str | None = None,
    headless_now_ms: int | None = None,
    workflow: str = "/mednotes:link-related",
    run_id: str = "related-notes-recovery",
) -> JsonObject:
    result = recover_related_notes_export_operation_result(
        config,
        export_path=export_path,
        mode=mode,
        command_runner=command_runner,
        headless_embedding_client=headless_embedding_client,
        headless_now_iso=headless_now_iso,
        headless_now_ms=headless_now_ms,
        workflow=workflow,
        run_id=run_id,
    )
    sync_result = LinkRelatedSyncResult.from_payload(result)
    return link_related_fsm_payload_from_sync_result(
        JsonObjectAdapter.validate_python(result),
        run_id=_link_related_run_id(sync_result),
        mode="recover_export",
        version_control_safety=_link_related_version_control_safety(sync_result, applying=False),
    )


def recover_related_notes_export_operation_result(
    config: MedConfig,
    *,
    export_path: Path | None = None,
    mode: str = "auto",
    command_runner: _ObsidianCommandRunner | None = None,
    headless_embedding_client: EmbeddingClient | None = None,
    headless_now_iso: str | None = None,
    headless_now_ms: int | None = None,
    workflow: str = "/mednotes:link-related",
    run_id: str = "related-notes-recovery",
) -> JsonObject:
    export = export_path or default_export_path(config.wiki_dir)
    preflight = _load_and_validate_export(export, config.wiki_dir, max_age_hours=168)
    stale_notes = [
        {
            "path": str(item["path"]),
            "expected_hash": str(item["expected"]),
            "actual_hash": str(item["actual"]),
        }
        for item in preflight.hash_errors
    ]
    for item in preflight.stale_notes:
        stale_notes.append(
            {
                "path": str(item["path"]),
                "expected_hash": str(item["expected_hash"]),
                "actual_hash": str(item["actual_hash"]),
            }
        )
    if not preflight.is_blocked:
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="recovered",
            blocked_reason="",
            recovery_mode="not_needed",
            stale_notes=[],
            next_action="",
            extra={"retry_command": wiki_cli_command("related-notes-sync", "--dry-run", "--json")},
        )
    blocked_reason = preflight.blocked_reason
    if blocked_reason not in {
        "related_notes_hash_mismatch",
        "related_notes_export_stale",
        "related_notes_vault_mismatch",
    }:
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason=blocked_reason or "related_notes_recovery_not_applicable",
            recovery_mode="manual_required",
            stale_notes=stale_notes,
            next_action=preflight.next_action,
        )

    recovery_mode = _select_recovery_mode(mode=mode, stale_notes=stale_notes, blocked_reason=blocked_reason)
    if recovery_mode == "export_only_diagnostic" and stale_notes:
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="related_notes_export_only_unsafe_for_changed_notes",
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action="Usar --mode reindex-vault para notas existentes editadas; export-only não corrige índice stale.",
        )
    runner = command_runner or _run_obsidian_command
    if command_runner is None and shutil.which("obsidian") is None:
        if headless_plugin_settings_available(config.wiki_dir):
            return _recover_related_notes_export_headless(
                config,
                export,
                recovery_mode=recovery_mode,
                stale_notes=stale_notes,
                embedding_client=headless_embedding_client,
                now_iso=headless_now_iso,
                now_ms=headless_now_ms,
                workflow=workflow,
                run_id=run_id,
            )
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason=blocked_reason,
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action=preflight.next_action or "Conferir o export do Related Notes pela rota oficial.",
            extra={
                "export_relocation": preflight.export_relocation,
                "obsidian_cli_available": False,
                "obsidian_running": False,
                "automatic_recovery_unavailable_reason": "obsidian_cli_unavailable",
            },
        )

    ready = runner(["obsidian", "help"])
    if int(getattr(ready, "returncode", 1) or 0) != 0:
        timed_out = int(getattr(ready, "returncode", 1) or 0) == OBSIDIAN_TIMEOUT_RETURNCODE
        extra = {
            "command_discovery_status": "timeout" if timed_out else "not_ready",
            "command_returncode": int(getattr(ready, "returncode", 1) or 0),
        }
        if timed_out:
            extra["command_timeout_seconds"] = OBSIDIAN_PROBE_TIMEOUT_SECONDS
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="obsidian_cli_timeout" if timed_out else "obsidian_not_ready",
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action=(
                "Obsidian CLI demorou demais para responder; verifique se o Obsidian está aberto e repita o recovery."
                if timed_out
                else "Abrir o Obsidian no vault configurado e repetir related-notes-sync --recover-export --mode auto --json."
            ),
            extra=extra,
        )

    discovered, discovery_status = _discover_related_notes_commands(runner)
    if discovery_status == "timeout":
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="obsidian_cli_timeout",
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action="Obsidian CLI demorou demais para listar comandos; verifique o Obsidian e repita o recovery.",
            extra={"command_discovery_status": discovery_status, "command_timeout_seconds": OBSIDIAN_PROBE_TIMEOUT_SECONDS},
        )
    command_id = RELATED_NOTES_COMMANDS[recovery_mode] if recovery_mode in RELATED_NOTES_COMMANDS else RELATED_NOTES_COMMANDS["reindex_vault"]
    if discovery_status == "discovered" and command_id not in discovered:
        blocked_reason = (
            "related_notes_plugin_unavailable"
            if not discovered
            else "related_notes_export_command_missing"
            if recovery_mode == "export_only_diagnostic"
            else "related_notes_reindex_command_missing"
        )
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason=blocked_reason,
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action="Habilitar related-notes-obsidian no vault e repetir o recovery.",
            extra={"discovered_commands": sorted(discovered), "command_discovery_status": discovery_status},
        )

    command = ["obsidian", f"vault={config.wiki_dir.name}", "command", f"id={command_id}"]
    result = runner(command)
    if int(getattr(result, "returncode", 1) or 0) != 0:
        timed_out = int(getattr(result, "returncode", 1) or 0) == OBSIDIAN_TIMEOUT_RETURNCODE
        extra = {
            "discovered_commands": sorted(discovered),
            "command_discovery_status": discovery_status,
            "command_returncode": int(getattr(result, "returncode", 1) or 0),
        }
        if timed_out:
            extra["command_timeout_seconds"] = OBSIDIAN_COMMAND_TIMEOUT_SECONDS
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="obsidian_cli_timeout" if timed_out else "related_notes_reindex_failed",
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action=(
                "Obsidian CLI demorou demais para executar o comando do plugin; verifique o Obsidian e repita o recovery."
                if timed_out
                else "Corrigir erro do plugin/Obsidian CLI e repetir related-notes-sync --recover-export."
            ),
            extra=extra,
        )

    validation = sync_related_notes_operation_result(config, export_path=export, apply=False)
    validation_result = LinkRelatedSyncResult.from_payload(validation)
    if validation_result.status == "blocked" or validation_result.blocked_reason:
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="related_notes_export_still_stale"
            if validation_result.blocked_reason == "related_notes_hash_mismatch"
            else validation_result.blocked_reason or "related_notes_revalidation_failed",
            recovery_mode=recovery_mode,
            stale_notes=stale_notes,
            next_action=validation_result.next_action or "Revalidar export do Related Notes antes do apply.",
            extra={"discovered_commands": sorted(discovered), "command_discovery_status": discovery_status, "revalidation": validation},
        )
    return _recovery_payload(
        export,
        config.wiki_dir,
        status="recovered",
        blocked_reason="",
        recovery_mode=recovery_mode,
        stale_notes=stale_notes,
        next_action=wiki_cli_command("related-notes-sync", "--dry-run", "--json"),
        extra={"discovered_commands": sorted(discovered), "command_discovery_status": discovery_status, "revalidation": validation},
    )


def _recover_related_notes_export_headless(
    config: MedConfig,
    export: Path,
    *,
    recovery_mode: str,
    stale_notes: list[dict[str, str]],
    embedding_client: EmbeddingClient | None,
    now_iso: str | None,
    now_ms: int | None,
    workflow: str,
    run_id: str,
) -> JsonObject:
    try:
        headless = generate_headless_related_notes_export(
            config.wiki_dir,
            export_path=export,
            embedding_client=embedding_client,
            now_iso=now_iso,
            now_ms=now_ms,
        )
    except HeadlessRelatedNotesExportError as exc:
        next_action = exc.next_action
        if exc.partial_record_count:
            label = "registro" if exc.partial_record_count == 1 else "registros"
            next_action = (
                f"{exc.next_action} O índice parcial já tem {exc.partial_record_count} {label}; "
                "a próxima tentativa retoma desse ponto."
            )
        recovery_state = {
            "schema": "medical-notes-workbench.related-notes-recovery-state.v1",
            "status": "waiting_for_retry" if exc.blocked_reason in RELATED_NOTES_RESUMABLE_BLOCKERS else "blocked",
            "blocked_reason": exc.blocked_reason,
            "resume_supported": bool(exc.partial_record_count),
            "partial_record_count": exc.partial_record_count,
            "fresh_record_count": exc.fresh_record_count or exc.partial_record_count,
            "stale_record_count": exc.stale_record_count,
            "record_count": exc.record_count,
            "total_note_count": exc.total_note_count,
            "remaining_count": exc.remaining_count,
            "embedded_count": exc.embedded_count,
            "reused_count": exc.reused_count,
            "next_retry_after_seconds": exc.next_retry_after_seconds,
            "attempt_count": 1,
        }
        projection = build_related_notes_recovery_projection(
            workflow=workflow,
            run_id=run_id,
            recovery_state=recovery_state,
            next_action=next_action,
        )
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason=exc.blocked_reason,
            recovery_mode="headless_reindex_vault",
            stale_notes=stale_notes,
            next_action=next_action,
            extra={
                "headless_export": {
                    "status": "blocked",
                    "phase": "related_notes_headless_export",
                    "blocked_reason": exc.blocked_reason,
                    "detail": exc.detail,
                    "partial_record_count": exc.partial_record_count,
                    "fresh_record_count": exc.fresh_record_count or exc.partial_record_count,
                    "stale_record_count": exc.stale_record_count,
                    "record_count": exc.record_count,
                    "total_note_count": exc.total_note_count,
                    "remaining_count": exc.remaining_count,
                    "embedded_count": exc.embedded_count,
                    "reused_count": exc.reused_count,
                    "next_retry_after_seconds": exc.next_retry_after_seconds,
                    "resume_supported": bool(exc.partial_record_count),
                },
                "related_notes_recovery_state": recovery_state,
                "progress_state": projection.progress_state.to_payload(),
                "progress_view_model": projection.progress_view_model.to_payload(),
                "state_machine_snapshot": projection.snapshot.to_payload(),
                "obsidian_cli_available": False,
                "obsidian_running": False,
                "fallback_from_recovery_mode": recovery_mode,
            },
    )
    validation = sync_related_notes_operation_result(config, export_path=export, apply=False)
    validation_result = LinkRelatedSyncResult.from_payload(validation)
    if validation_result.status == "blocked" or validation_result.blocked_reason:
        return _recovery_payload(
            export,
            config.wiki_dir,
            status="blocked",
            blocked_reason="related_notes_export_still_stale"
            if validation_result.blocked_reason == "related_notes_hash_mismatch"
            else validation_result.blocked_reason or "related_notes_revalidation_failed",
            recovery_mode="headless_reindex_vault",
            stale_notes=stale_notes,
            next_action=validation_result.next_action or "Revalidar export do Related Notes antes do apply.",
            extra={
                "headless_export": headless,
                "obsidian_cli_available": False,
                "obsidian_running": False,
                "fallback_from_recovery_mode": recovery_mode,
                "revalidation": validation,
            },
        )
    return _recovery_payload(
        export,
        config.wiki_dir,
        status="recovered",
        blocked_reason="",
        recovery_mode="headless_reindex_vault",
        stale_notes=stale_notes,
        next_action=wiki_cli_command("related-notes-sync", "--dry-run", "--json"),
        extra={
            "headless_export": headless,
            "obsidian_cli_available": False,
            "obsidian_running": False,
            "fallback_from_recovery_mode": recovery_mode,
            "revalidation": validation,
        },
    )


def _select_recovery_mode(*, mode: str, stale_notes: list[dict[str, str]], blocked_reason: str = "") -> str:
    normalized = mode.replace("-", "_")
    if normalized == "auto":
        return (
            "reindex_vault"
            if stale_notes or blocked_reason in {"related_notes_export_stale", "related_notes_vault_mismatch"}
            else "manual_required"
        )
    if normalized in {"reindex_vault", "index_missing", "index_missing_notes", "export_only_diagnostic"}:
        return "index_missing_notes" if normalized == "index_missing" else normalized
    return "manual_required"


def _public_recovery_mode(recovery_mode: str, *, blocked_reason: str = "") -> str:
    mapping = {
        "headless_reindex_vault": "headless-reindex-vault",
        "reindex_vault": "reindex-vault",
        "index_missing_notes": "index-missing",
        "export_only_diagnostic": "export-only-diagnostic",
        "manual_required": "manual",
        "not_needed": "manual",
    }
    if not recovery_mode and blocked_reason == "related_notes_hash_mismatch":
        return "reindex-vault"
    return mapping[recovery_mode] if recovery_mode in mapping else "manual"


def _manual_instruction_allowed(blocked_reason: str) -> bool:
    return blocked_reason in {
        "obsidian_cli_unavailable",
        "obsidian_not_ready",
        "obsidian_cli_timeout",
        "related_notes_plugin_unavailable",
        "related_notes_export_command_missing",
        "related_notes_headless_quota_exhausted",
        "related_notes_reindex_command_missing",
        "plugin_command_unavailable",
    }


def _run_obsidian_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    timeout = (
        OBSIDIAN_PROBE_TIMEOUT_SECONDS
        if tuple(argv[:2]) in {("obsidian", "help"), ("obsidian", "commands")}
        else OBSIDIAN_COMMAND_TIMEOUT_SECONDS
    )
    command = list(argv)
    if command[:1] == ["obsidian"]:
        command[0] = shutil.which("obsidian") or command[0]
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(
            command,
            OBSIDIAN_TIMEOUT_RETURNCODE,
            stdout=stdout,
            stderr=stderr or f"Obsidian CLI timed out after {timeout} seconds",
        )


def _discover_related_notes_commands(command_runner: _ObsidianCommandRunner) -> tuple[set[str], str]:
    result = command_runner(["obsidian", "commands", "--json"])
    if int(getattr(result, "returncode", 1) or 0) != 0:
        if int(getattr(result, "returncode", 1) or 0) == OBSIDIAN_TIMEOUT_RETURNCODE:
            return set(), "timeout"
        return set(RELATED_NOTES_COMMANDS.values()), "known_ids"
    try:
        parsed = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return set(RELATED_NOTES_COMMANDS.values()), "known_ids"
    commands: set[str] = set()
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                payload = JsonObjectAdapter.validate_python(item)
                command_id = _json_str_field(payload, "id")
                if command_id:
                    commands.add(command_id)
            elif isinstance(item, str):
                commands.add(item)
    elif isinstance(parsed, dict):
        payload = JsonObjectAdapter.validate_python(parsed)
        for key, value in payload.items():
            if key == "id" and value:
                commands.add(str(value))
            elif isinstance(value, dict):
                command_id = _json_str_field(JsonObjectAdapter.validate_python(value), "id")
                if command_id:
                    commands.add(command_id)
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    command_id = _json_str_field(JsonObjectAdapter.validate_python(item), "id")
                    if command_id:
                        commands.add(command_id)
    return {command for command in commands if command.startswith(f"{RELATED_NOTES_PLUGIN_ID}:")}, "discovered"


def _blocked_export_validation(
    export_path: Path,
    wiki_dir: Path,
    *,
    blocked_reason: str,
    next_action: str,
    hash_errors: list[JsonObject] | None = None,
    stale_notes: list[JsonObject] | None = None,
    validation_errors: list[JsonObject] | None = None,
    export_relocation: JsonObject | None = None,
    extra: JsonObject | None = None,
) -> _RelatedNotesExportValidation:
    return _RelatedNotesExportValidation.model_validate(
        {
            "status": "blocked",
            "export_path": export_path,
            "wiki_dir": wiki_dir,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "hash_errors": hash_errors or [],
            "stale_notes": stale_notes or [],
            "validation_errors": validation_errors or [],
            "export_relocation": export_relocation or {},
            "extra_payload": extra or {},
        }
    )


def _json_object_list(items: Iterable[object]) -> list[JsonObject]:
    """Validate a sequence of dict-like records before embedding in payloads."""

    return [JsonObjectAdapter.validate_python(item) for item in items]


def _ready_export_validation(
    export_path: Path,
    wiki_dir: Path,
    *,
    payload: JsonObject,
    notes: dict[str, RelatedNote],
    edges: list[RelatedEdge],
    hash_warnings: list[JsonObject] | None = None,
    export_relocation: JsonObject | None = None,
) -> _RelatedNotesExportValidation:
    return _RelatedNotesExportValidation.model_validate(
        {
            "status": "ready",
            "export_path": export_path,
            "wiki_dir": wiki_dir,
            "payload": payload,
            "notes": notes,
            "edges": edges,
            "hash_warnings": hash_warnings or [],
            "export_relocation": export_relocation or {},
        }
    )


def _recovery_payload(
    export_path: Path,
    wiki_dir: Path,
    *,
    status: str,
    blocked_reason: str,
    recovery_mode: str,
    stale_notes: list[dict[str, str]],
    next_action: str,
    extra: JsonObject | None = None,
) -> JsonObject:
    reindex_id = RELATED_NOTES_COMMANDS["reindex_vault"]
    export_id = RELATED_NOTES_COMMANDS["export_only_diagnostic"]
    extra_payload = extra or {}
    raw_headless = extra_payload["headless_export"] if "headless_export" in extra_payload else None
    headless = (
        RelatedNotesHeadlessExportSummary.model_validate(raw_headless)
        if isinstance(raw_headless, dict)
        else RelatedNotesHeadlessExportSummary()
    )
    return {
        "schema": RELATED_NOTES_EXPORT_RECOVERY_SCHEMA,
        "phase": "related_notes_export_recovery",
        "status": status,
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "required_inputs": RELATED_NOTES_REQUIRED_INPUTS,
        "human_decision_required": False,
        "wiki_dir": str(wiki_dir),
        "export_path": str(export_path),
        "stale_notes": stale_notes,
        "stale_note_count": len(stale_notes),
        "obsidian_cli_available": blocked_reason != "obsidian_cli_unavailable",
        "obsidian_running": blocked_reason not in {
            "obsidian_cli_unavailable",
            "obsidian_not_ready",
            "obsidian_cli_timeout",
        },
        "plugin_id": RELATED_NOTES_PLUGIN_ID,
        "recovery_mode": recovery_mode,
        "selected_recovery_mode": _public_recovery_mode(recovery_mode, blocked_reason=blocked_reason),
        "manual_instruction_allowed": _manual_instruction_allowed(blocked_reason),
        "api_calls": headless.embedded_count,
        "api_failures": 0,
        "obsidian_cli_reindex_command": f'obsidian vault="{wiki_dir.name}" command id="{reindex_id}"',
        "obsidian_cli_export_only_command": f'obsidian vault="{wiki_dir.name}" command id="{export_id}"',
        "retry_command": wiki_cli_command("run-linker", "--diagnose", "--json"),
        "body_only_fallback": None,
        **extra_payload,
    }


def sync_related_notes(
    config: MedConfig,
    *,
    export_path: Path | None = None,
    apply: bool = False,
    backup: bool = False,
    receipt_path: Path | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    max_links: int = DEFAULT_MAX_LINKS,
    max_age_hours: float = 168.0,
    allow_stale_note_hashes: bool = False,
) -> JsonObject:
    result = sync_related_notes_operation_result(
        config,
        export_path=export_path,
        apply=apply,
        backup=backup,
        receipt_path=receipt_path,
        min_score=min_score,
        max_links=max_links,
        max_age_hours=max_age_hours,
        allow_stale_note_hashes=allow_stale_note_hashes,
    )
    sync_result = LinkRelatedSyncResult.from_payload(result)
    return link_related_fsm_payload_from_sync_result(
        JsonObjectAdapter.validate_python(result),
        run_id=_link_related_run_id(sync_result),
        mode="apply" if apply else "dry_run",
        version_control_safety=_link_related_version_control_safety(sync_result, applying=apply),
    )


def sync_related_notes_operation_result(
    config: MedConfig,
    *,
    export_path: Path | None = None,
    apply: bool = False,
    backup: bool = False,
    receipt_path: Path | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    max_links: int = DEFAULT_MAX_LINKS,
    max_age_hours: float = 168.0,
    allow_stale_note_hashes: bool = False,
) -> JsonObject:
    backup = False
    export = export_path or default_export_path(config.wiki_dir)
    blocked = _load_and_validate_export(
        export,
        config.wiki_dir,
        max_age_hours=max_age_hours,
        allow_stale_note_hashes=allow_stale_note_hashes,
    )
    if blocked.is_blocked:
        return blocked.blocked_payload()

    payload = blocked.payload
    notes = blocked.notes
    edges = blocked.edges
    plan = _plan_related_note_updates(config.wiki_dir, notes, edges, min_score=min_score, max_links=max_links)

    result = _base_payload(
        export,
        config.wiki_dir,
        status="preview_ready" if not apply else "completed",
        phase="related_notes_dry_run" if not apply else "related_notes_apply",
        blocked_reason="",
        next_action=(
            "Revisar updates e repetir com --apply --receipt para gravar."
            if not apply and plan.updates
            else ""
        ),
        extra={
            "source_export_schema": payload["schema"],
            "source_export_generated_at": payload["generated_at"],
            "plugin": payload["plugin"],
            "model": payload["model"],
            "min_score": min_score,
            "max_links": max_links,
            **_plan_summary(plan),
            "updates": plan.public_updates_payload(),
            "skipped_edges": plan.skipped_edges_payload(),
            "hash_warnings": blocked.hash_warnings,
            "export_relocation": blocked.export_relocation,
        },
    )
    if not apply:
        return result

    applied_updates = _apply_updates(
        [update for update in plan.private_updates if update.changed],
        backup=backup,
    )
    receipt = _write_receipt(
        receipt_path or _default_receipt_path(),
        export_path=export,
        wiki_dir=config.wiki_dir,
        export_payload=payload,
        plan=result,
        applied_updates=applied_updates,
    )
    result.update(
        {
            "applied_note_count": len(applied_updates),
            "receipt_path": str(receipt),
            "updates": applied_updates,
        }
    )
    return result


def cleanup_invalid_related_notes_links(
    config: MedConfig,
    *,
    backup: bool = False,
    cleanup_reason: str = "",
) -> JsonObject:
    """Remove broken links from generated Related Notes sections.

    This is a degraded safety path for apply workflows when the plugin export
    cannot be refreshed. It does not invent new recommendations; it only keeps
    links that already point to one unique existing note.
    """
    notes_by_target = _notes_by_target(config.wiki_dir)
    reports: list[JsonObject] = []
    changed_files: list[str] = []
    backup_paths: list[str] = []
    removed_link_count = 0
    kept_link_count = 0
    for path in iter_notes(config.wiki_dir):
        relative = path.relative_to(config.wiki_dir).as_posix()
        text = path.read_text(encoding="utf-8")
        if _is_index_note(path, text):
            continue
        span = _related_section_span(text)
        if span is None:
            continue
        updated, report = _clean_related_section_text(
            text,
            span,
            source_relative_path=relative,
            notes_by_target=notes_by_target,
        )
        if not report.removed_links:
            continue
        removed_link_count += len(report.removed_links)
        kept_link_count += report.kept_link_count
        if updated == text:
            reports.append({"path": relative, "changed": False, **report.to_payload()})
            continue
        atomic_write_text(path, updated)
        changed_files.append(str(path))
        reports.append({"path": relative, "changed": True, "backup_path": "", **report.to_payload()})
    return {
        "schema": RELATED_NOTES_SAFETY_CLEANUP_SCHEMA,
        "phase": "related_notes_safety_cleanup",
        "status": "completed" if changed_files else "skipped",
        "cleanup_reason": cleanup_reason,
        "backup": backup,
        "changed_file_count": len(changed_files),
        "changed_files": changed_files,
        "backup_paths": backup_paths,
        "removed_link_count": removed_link_count,
        "kept_link_count": kept_link_count,
        "reports": reports,
    }


def _load_and_validate_export(
    export_path: Path,
    wiki_dir: Path,
    *,
    max_age_hours: float,
    allow_stale_note_hashes: bool = False,
) -> _RelatedNotesExportValidation:
    if not export_path.is_file():
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_missing",
            next_action=(
                "Exportar .obsidian/plugins/related-notes-obsidian/medical-notes-export.json "
                "ou passar --export para um arquivo related-notes-export.v1."
            ),
        )
    cache_key = _related_notes_export_cache_key(
        export_path,
        wiki_dir,
        max_age_hours=max_age_hours,
        allow_stale_note_hashes=allow_stale_note_hashes,
    )
    if cache_key is not None and cache_key in _RELATED_NOTES_EXPORT_CACHE:
        return _clone_export_validation_result(_RELATED_NOTES_EXPORT_CACHE[cache_key])
    try:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_invalid_json",
            next_action=f"Corrigir JSON do export antes de repetir. Detalhe: {exc}",
        )
    if not isinstance(payload, dict):
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_schema_invalid",
            next_action=f"Gerar export no schema {RELATED_NOTES_EXPORT_SCHEMA}.",
        )
    raw_payload = JsonObjectAdapter.validate_python(payload)
    if _json_str_field(raw_payload, "schema") != RELATED_NOTES_EXPORT_SCHEMA:
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_schema_invalid",
            next_action=f"Gerar export no schema {RELATED_NOTES_EXPORT_SCHEMA}.",
        )
    forbidden = _find_forbidden_export_keys(payload)
    if forbidden:
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_contains_private_payload",
            next_action="Gerar export sem API keys, tokens, conteúdo bruto, markdown ou embeddings.",
            extra={"forbidden_keys": forbidden[:12]},
        )
    try:
        contract = RelatedNotesExport.model_validate(payload)
    except PydanticValidationError as exc:
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_contract_invalid",
            next_action=f"Gerar export no schema tipado {RELATED_NOTES_EXPORT_SCHEMA} pela rota oficial do plugin.",
            extra={"contract_errors": _contract_errors(exc)},
        )
    payload = contract.to_payload()
    vault_root = contract.vault_root.strip()
    vault_root_mismatch = not _same_root(vault_root, wiki_dir)
    age_error = _staleness_error(contract.generated_at.isoformat(), max_age_hours=max_age_hours)
    if age_error:
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_export_stale",
            next_action=wiki_cli_command("related-notes-sync", "--recover-export", "--mode", "auto", "--json"),
            extra={"generated_at": payload["generated_at"], "max_age_hours": max_age_hours, "detail": age_error},
        )

    notes_result = _parse_export_notes(contract, wiki_dir)
    if isinstance(notes_result, _RelatedNotesParseBlocked):
        return _blocked_parse_payload(export_path, wiki_dir, notes_result)
    notes = notes_result.notes
    profile_id = _export_profile_id(contract)
    hash_errors = _hash_errors(notes, profile_id=profile_id)
    if hash_errors and not allow_stale_note_hashes:
        export_relocation: JsonObject = {}
        if vault_root_mismatch:
            export_relocation = _export_relocation_payload(
                status="rejected",
                proof="relative_paths_and_representation_hashes",
                reason="hash_mismatch",
                note_count=len(notes),
                errors=hash_errors[:12],
            )
        return _blocked_export_validation(
            export_path,
            wiki_dir,
            blocked_reason="related_notes_hash_mismatch",
            next_action=wiki_cli_command("related-notes-sync", "--recover-export", "--mode", "auto", "--json"),
            hash_errors=_json_object_list(hash_errors[:12]),
            export_relocation=export_relocation,
        )
    if vault_root_mismatch:
        relocation_errors = _relocated_export_verification_errors(
            export_path=export_path,
            wiki_dir=wiki_dir,
            notes=notes,
        )
        if relocation_errors:
            return _blocked_export_validation(
                export_path,
                wiki_dir,
                blocked_reason="related_notes_vault_mismatch",
                next_action=(
                    "Conferir o export do Related Notes: o caminho do vault mudou e a validação local "
                    "não conseguiu provar que o export cobre esta Wiki."
                ),
                export_relocation=_export_relocation_payload(
                    status="rejected",
                    proof="relative_paths_and_representation_hashes",
                    reason="coverage_or_location_mismatch",
                    note_count=len(notes),
                    errors=relocation_errors,
                ),
            )

    edges_result = _parse_export_edges(contract, notes)
    if isinstance(edges_result, _RelatedNotesParseBlocked):
        return _blocked_parse_payload(export_path, wiki_dir, edges_result)
    export_relocation: JsonObject = {}
    if vault_root_mismatch:
        export_relocation = _export_relocation_payload(
            status="accepted",
            proof="relative_paths_and_representation_hashes",
            reason="vault_root_changed_but_relative_export_matches_current_wiki",
            note_count=len(notes),
            errors=[],
        )
    result = _ready_export_validation(
        export_path,
        wiki_dir,
        payload=payload,
        notes=notes,
        edges=edges_result.edges,
        hash_warnings=_json_object_list(hash_errors[:12]) if hash_errors else [],
        export_relocation=export_relocation,
    )
    if cache_key is not None:
        _store_export_validation_result(cache_key, result)
    return _clone_export_validation_result(result)


def _related_notes_export_cache_key(
    export_path: Path,
    wiki_dir: Path,
    *,
    max_age_hours: float,
    allow_stale_note_hashes: bool,
) -> _RelatedNotesExportCacheKey | None:
    try:
        stat = export_path.stat()
    except OSError:
        return None
    return _RelatedNotesExportCacheKey(
        export_path=str(export_path.resolve(strict=False)),
        wiki_dir=str(wiki_dir.resolve(strict=False)),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        max_age_hours=float(max_age_hours),
        allow_stale_note_hashes=allow_stale_note_hashes,
    )


def _store_export_validation_result(key: _RelatedNotesExportCacheKey, result: _RelatedNotesExportValidation) -> None:
    if len(_RELATED_NOTES_EXPORT_CACHE) >= _RELATED_NOTES_EXPORT_CACHE_MAX_ENTRIES:
        _RELATED_NOTES_EXPORT_CACHE.clear()
    _RELATED_NOTES_EXPORT_CACHE[key] = _clone_export_validation_result(result)


def _clone_export_validation_result(result: _RelatedNotesExportValidation) -> _RelatedNotesExportValidation:
    return result.model_copy(
        update={
            "payload": dict(result.payload),
            "notes": dict(result.notes),
            "edges": list(result.edges),
            "hash_errors": list(result.hash_errors),
            "stale_notes": list(result.stale_notes),
            "validation_errors": list(result.validation_errors),
            "hash_warnings": list(result.hash_warnings),
            "export_relocation": dict(result.export_relocation),
            "extra_payload": dict(result.extra_payload),
        }
    )


def _parse_export_notes(contract: RelatedNotesExport, wiki_dir: Path) -> _RelatedNotesNotesParseResult:
    """Validate exported note paths against the current vault."""

    notes: dict[str, RelatedNote] = {}
    errors: list[JsonObject] = []
    stale_notes: list[JsonObject] = []
    for item in contract.notes:
        rel = _safe_relative_path(item.path)
        if rel is None:
            errors.append({"path": item.path, "error": "path must be relative inside wiki"})
            continue
        abs_path = (wiki_dir / rel).resolve(strict=False)
        if not _is_inside(abs_path, wiki_dir):
            errors.append({"path": item.path, "error": "path escapes wiki_dir"})
            continue
        if not abs_path.is_file():
            stale_notes.append(
                {
                    "path": rel.as_posix(),
                    "expected_hash": _normalize_hash(item.content_hash),
                    "actual_hash": "missing",
                }
            )
            errors.append({"path": rel.as_posix(), "error": "note file missing"})
            continue
        notes[rel.as_posix()] = RelatedNote(
            rel_path=rel.as_posix(),
            abs_path=abs_path,
            title=item.title or abs_path.stem,
            content_hash=_normalize_hash(item.content_hash),
        )
    if errors:
        if stale_notes and len(stale_notes) == len(errors):
            return _RelatedNotesParseBlocked(
                blocked_reason="related_notes_export_stale",
                next_action=wiki_cli_command("related-notes-sync", "--recover-export", "--mode", "auto", "--json"),
                validation_errors=errors[:20],
                stale_notes=stale_notes[:20],
            )
        return _RelatedNotesParseBlocked(
            blocked_reason="related_notes_note_path_invalid",
            next_action="Corrigir paths relativos e hashes no export do plugin.",
            validation_errors=errors[:20],
            stale_notes=[],
        )
    return _RelatedNotesParsedNotes(notes=notes)


def _parse_export_edges(
    contract: RelatedNotesExport,
    notes: dict[str, RelatedNote],
) -> _RelatedNotesEdgesParseResult:
    """Validate exported graph edges against the typed note map."""

    edges: list[RelatedEdge] = []
    errors: list[JsonObject] = []
    for item in contract.edges:
        source_path = _safe_relative_path_string(item.source_path)
        target_path = _safe_relative_path_string(item.target_path)
        if not source_path or not target_path:
            errors.append({"edge": f"{item.source_path}->{item.target_path}", "error": "source_path and target_path must be relative"})
            continue
        if source_path not in notes or target_path not in notes:
            errors.append({"edge": f"{source_path}->{target_path}", "error": "edge references note missing from notes[]"})
            continue
        edges.append(
            RelatedEdge(
                source_path=source_path,
                target_path=target_path,
                score=item.score,
                rank=item.rank,
                source=item.source,
            )
        )
    if errors:
        return _RelatedNotesParseBlocked(
            blocked_reason="related_notes_edge_invalid",
            next_action="Corrigir edges para apontar apenas para notes[] válidas.",
            validation_errors=errors[:20],
            stale_notes=[],
        )
    return _RelatedNotesParsedEdges(edges=edges)


def _relocated_export_verification_errors(
    *,
    export_path: Path,
    wiki_dir: Path,
    notes: dict[str, RelatedNote],
) -> list[JsonObject]:
    errors: list[JsonObject] = []
    if not _is_inside(export_path.resolve(strict=False), wiki_dir):
        errors.append({"code": "export_not_inside_current_wiki"})

    exported_paths = set(notes)
    current_paths: set[str] = set()
    for path in iter_notes(wiki_dir):
        text = path.read_text(encoding="utf-8")
        if _is_index_note(path, text):
            continue
        current_paths.add(path.relative_to(wiki_dir).as_posix())

    missing_paths = sorted(current_paths - exported_paths)
    extra_paths = sorted(exported_paths - current_paths)
    if not exported_paths and current_paths:
        errors.append({"code": "export_has_no_notes", "current_note_count": len(current_paths)})
    if missing_paths:
        errors.append({"code": "export_missing_current_notes", "paths": missing_paths[:20]})
    if extra_paths:
        errors.append({"code": "export_contains_non_current_notes", "paths": extra_paths[:20]})
    return errors


def _export_relocation_payload(
    *,
    status: str,
    proof: str,
    reason: str,
    note_count: int,
    errors: list[JsonObject],
) -> JsonObject:
    return JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.related-notes-export-relocation.v1",
            "status": status,
            "proof": proof,
            "reason": reason,
            "note_count": note_count,
            "uses_absolute_path_for_hash": False,
            "api_calls": 0,
            "embedding_calls": 0,
            "errors": errors,
        }
    )


def _blocked_parse_payload(
    export_path: Path,
    wiki_dir: Path,
    parse_result: _RelatedNotesParseBlocked | object,
) -> _RelatedNotesExportValidation:
    typed_result = (
        parse_result
        if isinstance(parse_result, _RelatedNotesParseBlocked)
        else _RelatedNotesBlockedParseInput.model_validate(parse_result).to_result()
    )
    return _blocked_export_validation(
        export_path,
        wiki_dir,
        blocked_reason=typed_result.blocked_reason or "related_notes_export_invalid",
        next_action=typed_result.next_action or "Corrigir o export do plugin Related Notes.",
        validation_errors=typed_result.validation_errors,
        stale_notes=typed_result.stale_notes,
    )


def _plan_related_note_updates(
    wiki_dir: Path,
    notes: dict[str, RelatedNote],
    edges: list[RelatedEdge],
    *,
    min_score: float,
    max_links: int,
) -> _RelatedNotesUpdatePlan:
    updates: list[_RelatedNotesPlannedUpdate] = []
    skipped_edges: list[_RelatedNotesSkippedEdge] = []
    by_source: dict[str, list[RelatedEdge]] = {}
    for edge in edges:
        by_source.setdefault(edge.source_path, []).append(edge)

    title_counts: dict[str, int] = {}
    for note in notes.values():
        title_key = _link_key(note.title or note.abs_path.stem)
        title_counts[title_key] = (title_counts[title_key] if title_key in title_counts else 0) + 1

    for index, (source_path, note) in enumerate(sorted(notes.items()), start=1):
        cooperative_cpu_yield(index)
        source_edges = by_source[source_path] if source_path in by_source else []
        text = note.abs_path.read_text(encoding="utf-8")
        span = _related_section_span(text)
        if span is None:
            skipped_edges.extend(
                _RelatedNotesSkippedEdge(
                    source_path=edge.source_path,
                    target_path=edge.target_path,
                    reason="missing_related_section",
                )
                for edge in source_edges
            )
            if not source_edges:
                skipped_edges.append(_RelatedNotesSkippedEdge(source_path=source_path, reason="missing_related_section"))
            continue
        existing_targets = _existing_related_targets(text[span[1] : span[2]])
        proposed: list[_RelatedNotesProposedLink] = []
        for edge in sorted(
            source_edges,
            key=lambda item: (-item.score, item.rank, _link_key(notes[item.target_path].title), item.target_path),
        ):
            target = notes[edge.target_path]
            target_key = _link_key(target.title or target.abs_path.stem)
            if _link_key(note.title) == target_key:
                skipped_edges.append(
                    _RelatedNotesSkippedEdge(
                        source_path=edge.source_path,
                        target_path=edge.target_path,
                        reason="self_link",
                    )
                )
                continue
            if edge.score < min_score:
                skipped_edges.append(
                    _RelatedNotesSkippedEdge(
                        source_path=edge.source_path,
                        target_path=edge.target_path,
                        reason="below_min_score",
                        score=f"{edge.score:.4f}",
                    )
                )
                continue
            if len(proposed) >= max_links:
                skipped_edges.append(
                    _RelatedNotesSkippedEdge(
                        source_path=edge.source_path,
                        target_path=edge.target_path,
                        reason="max_links_reached",
                    )
                )
                continue
            proposed.append(
                _RelatedNotesProposedLink(
                    target_path=target.rel_path,
                    target_title=target.title,
                    score=edge.score,
                    rank=edge.rank,
                    source=edge.source,
                    content_hash=target.content_hash,
                    line=_render_link_line(target, title_counts),
                )
            )
        new_text = _render_related_section_update(text, span, proposed)
        updates.append(
            _RelatedNotesPlannedUpdate(
                file=str(note.abs_path),
                relative_path=note.rel_path,
                source_title=note.title,
                content_hash=note.content_hash,
                cleared_links=sorted(existing_targets),
                cleared_link_count=len(existing_targets),
                proposed_links=proposed,
                new_content=new_text,
                changed=new_text != text,
                min_score=min_score,
            )
        )
    public_updates = [item.public_update() for item in updates if item.changed]
    return _RelatedNotesUpdatePlan(
        wiki_dir=str(wiki_dir),
        updates=public_updates,
        private_updates=updates,
        skipped_edges=skipped_edges,
    )


def _related_section_span(text: str) -> tuple[int, int, int, int] | None:
    match = _RELATED_HEADING_RE.search(text)
    if not match:
        return None
    next_h2 = _NEXT_H2_RE.search(text, match.end())
    footer = _FOOTER_RE.search(text, match.end())
    candidates = [item.start() for item in (next_h2, footer) if item is not None]
    end = min(candidates) if candidates else len(text)
    return match.start(), match.end(), end, match.end()


def _existing_related_targets(section_body: str) -> set[str]:
    targets: set[str] = set()
    for match in _WIKILINK_RE.finditer(section_body):
        raw = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if raw:
            targets.add(_link_key(obsidian_target_name(raw)))
    return targets


def _notes_by_target(wiki_dir: Path) -> dict[str, list[str]]:
    notes: dict[str, list[str]] = {}
    for path in iter_notes(wiki_dir):
        text = path.read_text(encoding="utf-8")
        if _is_index_note(path, text):
            continue
        relative = path.relative_to(wiki_dir).as_posix()
        notes.setdefault(normalize_key(path.stem), []).append(relative)
    return notes


class _RelatedSectionCleanupReport(ContractModel):
    """Report for deterministic cleanup of invalid Related Notes links."""

    removed_links: list[JsonObject] = Field(default_factory=list)
    kept_link_count: int = Field(default=0, ge=0)
    cleared_to_marker: bool = Field(default=False, strict=True)


def _clean_related_section_text(
    text: str,
    span: tuple[int, int, int, int],
    *,
    source_relative_path: str,
    notes_by_target: dict[str, list[str]],
) -> tuple[str, _RelatedSectionCleanupReport]:
    heading_start, _heading_end, section_end, content_start = span
    section_body = text[content_start:section_end]
    kept_lines: list[str] = []
    removed_links: list[JsonObject] = []
    seen: set[str] = set()
    for match in _WIKILINK_RE.finditer(section_body):
        raw = match.group(1).strip()
        target = obsidian_target_name(raw.split("|", 1)[0].split("#", 1)[0].strip())
        target_key = normalize_key(target)
        target_paths = notes_by_target[target_key] if target_key in notes_by_target else []
        reason = ""
        if not target or is_index_target(target):
            reason = "not_note_target"
        elif not target_paths:
            reason = "dangling_link"
        elif len(target_paths) > 1:
            reason = "ambiguous_link"
        elif target_paths[0] == source_relative_path:
            reason = "self_link"
        elif target_key in seen:
            reason = "duplicate_related_link"
        if reason:
            removed_links.append({"target": target, "raw": raw, "reason": reason})
            continue
        seen.add(target_key)
        kept_lines.append(f"- [[{raw}]]")
    rendered_lines = kept_lines or [f"- {NO_STRONG_LINKS_MARKER}"]
    section = "## 🔗 Notas Relacionadas\n" + "\n".join(rendered_lines).rstrip() + "\n\n"
    report = _RelatedSectionCleanupReport(
        removed_links=removed_links,
        kept_link_count=len(kept_lines),
        cleared_to_marker=not kept_lines,
    )
    return text[:heading_start] + section + text[section_end:], report


def _render_link_line(target: RelatedNote, title_counts: dict[str, int]) -> str:
    title = target.title or target.abs_path.stem
    title_key = _link_key(title)
    if (title_counts[title_key] if title_key in title_counts else 0) > 1:
        target_without_suffix = target.rel_path[:-3] if target.rel_path.endswith(".md") else target.rel_path
        return f"- [[{target_without_suffix}|{title}]]"
    return f"- [[{title}]]"


def _render_related_section_update(
    text: str,
    span: tuple[int, int, int, int],
    proposed: list[_RelatedNotesProposedLink],
) -> str:
    heading_start, _heading_end, section_end, content_start = span
    lines = [item.line for item in proposed] or [f"- {NO_STRONG_LINKS_MARKER}"]
    section = "## 🔗 Notas Relacionadas\n" + "\n".join(lines).rstrip() + "\n\n"
    return text[:heading_start] + section + text[section_end:]


def _apply_updates(public_updates: list[_RelatedNotesPlannedUpdate], *, backup: bool) -> list[JsonObject]:
    applied: list[JsonObject] = []
    for update in public_updates:
        path = Path(update.file)
        atomic_write_text(path, update.new_content)
        public_update = update.public_update().to_payload()
        applied.append(
            {
                **public_update,
                "backup_path": "",
                "applied": True,
            }
        )
    return applied


def _write_receipt(
    path: Path,
    *,
    export_path: Path,
    wiki_dir: Path,
    export_payload: JsonObject,
    plan: JsonObject,
    applied_updates: list[JsonObject],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    typed_plan = JsonObjectAdapter.validate_python(plan)
    typed_updates = _RelatedNotesAppliedUpdatesPayload.model_validate({"updates": applied_updates}).updates
    typed_export = RelatedNotesExport.model_validate(export_payload)
    sync_result = LinkRelatedSyncResult.from_payload(typed_plan)
    receipt = _RelatedNotesSyncReceiptPayload(
        schema=RELATED_NOTES_SYNC_RECEIPT_SCHEMA,
        generated_at=_now_iso(),
        status=sync_result.status or "completed",
        phase="related_notes_apply",
        dry_run=False,
        no_resource_mutation=len(typed_updates) == 0,
        wiki_dir=str(wiki_dir),
        export_path=str(export_path),
        export_hash="sha256:" + file_sha256(export_path),
        export_generated_at=typed_export.generated_at.isoformat(),
        plugin=typed_export.plugin.to_payload(),
        model=typed_export.model_info.to_payload(),
        api_calls=0,
        api_failures=0,
        plan_hash="sha256:" + canonical_json_hash({key: value for key, value in typed_plan.items() if key != "updates"}),
        applied_note_count=len(typed_updates),
        update_count=len(typed_updates),
        updates=typed_updates,
    ).to_payload()
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return path


def _base_payload(
    export_path: Path,
    wiki_dir: Path,
    *,
    status: str,
    phase: str,
    blocked_reason: str,
    next_action: str,
    extra: JsonObject | None = None,
) -> JsonObject:
    selected_recovery_mode = (
        "reindex-vault"
        if blocked_reason in {"related_notes_hash_mismatch", "related_notes_export_stale"}
        else "manual"
    )
    extra_payload = JsonObjectAdapter.validate_python(extra or {})
    return JsonObjectAdapter.validate_python({
        "schema": RELATED_NOTES_SYNC_SCHEMA,
        "phase": phase,
        "status": status,
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "required_inputs": RELATED_NOTES_REQUIRED_INPUTS,
        "human_decision_required": False,
        "manual_instruction_allowed": _manual_instruction_allowed(blocked_reason),
        "selected_recovery_mode": selected_recovery_mode,
        "retry_command": wiki_cli_command("run-linker", "--diagnose", "--json"),
        "wiki_dir": str(wiki_dir),
        "export_path": str(export_path),
        **extra_payload,
    })


def _plan_summary(plan: _RelatedNotesUpdatePlan) -> dict[str, int]:
    return plan.summary()


def _contract_errors(exc: PydanticValidationError) -> list[JsonObject]:
    errors: list[JsonObject] = []
    for item in exc.errors()[:20]:
        loc_value = item["loc"] if "loc" in item else ()
        loc = ".".join(str(part) for part in loc_value) or "$"
        errors.append(
            {
                "loc": loc,
                "message": str(item["msg"] if "msg" in item else ""),
                "type": str(item["type"] if "type" in item else ""),
            }
        )
    return errors


def _export_profile_id(contract: RelatedNotesExport) -> str:
    """Read the embedding profile from the validated export contract."""

    return normalize_related_notes_profile_id(contract.model_info.embedding_profile_id)


def _hash_errors(notes: dict[str, RelatedNote], *, profile_id: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for note in notes.values():
        markdown = note.abs_path.read_text(encoding="utf-8")
        actual = related_notes_content_hash(
            path=note.rel_path,
            title=note.title or note.abs_path.stem,
            markdown=markdown,
            profile_id=profile_id,
        )
        if actual.lower() != note.content_hash.lower():
            legacy = (
                related_notes_legacy_clean_v1_content_hash(
                    path=note.rel_path,
                    title=note.title or note.abs_path.stem,
                    markdown=markdown,
                )
                if profile_id == "clean_v1"
                else ""
            )
            if legacy and legacy.lower() == note.content_hash.lower():
                continue
            errors.append(
                {
                    "path": note.rel_path,
                    "expected": note.content_hash,
                    "actual": actual,
                    "hash_basis": "representation_hash",
                    "embedding_profile_id": profile_id,
                }
            )
    return errors


def _safe_relative_path(value: str) -> PurePosixPath | None:
    text = value.strip().replace("\\", "/")
    if not text or text.startswith(("/", "../")) or _WINDOWS_ABSOLUTE_RE.match(text):
        return None
    rel = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in rel.parts):
        return None
    return rel


def _safe_relative_path_string(value: str) -> str:
    rel = _safe_relative_path(value)
    return rel.as_posix() if rel is not None else ""


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _same_root(value: str, wiki_dir: Path) -> bool:
    if not value:
        return False
    if value in {".", "./"}:
        return True
    if _WINDOWS_ABSOLUTE_RE.match(value) and not _WINDOWS_ABSOLUTE_RE.match(str(wiki_dir)):
        return False
    try:
        return Path(value).expanduser().resolve(strict=False) == wiki_dir.resolve(strict=False)
    except OSError:
        return False


def _normalize_hash(value: str) -> str:
    text = value.strip().lower()
    if not text:
        return ""
    return text if text.startswith("sha256:") else "sha256:" + text


def _find_forbidden_export_keys(value: object, *, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", key_text.lower())
            path = f"{prefix}.{key_text}" if prefix else key_text
            if normalized in _FORBIDDEN_EXPORT_KEYS:
                found.append(path)
            found.extend(_find_forbidden_export_keys(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value[:100]):
            found.extend(_find_forbidden_export_keys(item, prefix=f"{prefix}[{index}]"))
    return found


def _staleness_error(value: str, *, max_age_hours: float) -> str:
    if max_age_hours <= 0:
        return ""
    try:
        generated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "generated_at is not valid ISO-8601"
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - generated).total_seconds()
    if age_seconds < 0:
        return ""
    if age_seconds > max_age_hours * 3600:
        return f"export age is {age_seconds / 3600:.1f}h"
    return ""


def _link_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _default_receipt_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return user_state_dir() / "runs" / stamp / "related-notes-receipt.json"
