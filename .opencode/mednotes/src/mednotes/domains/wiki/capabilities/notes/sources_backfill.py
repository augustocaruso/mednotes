"""Backfill canonical chat provenance for existing Wiki notes."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import ConfigDict, Field

from mednotes.domains.wiki.capabilities.markdown.markdown_query import MarkdownDbChatMetadataProvider
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import FrontmatterYamlUnavailable
from mednotes.domains.wiki.capabilities.notes.provenance import (
    ChatProvenance,
    apply_note_provenance,
    audit_note_provenance,
    classify_note_provenance,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.config import MedConfig
from mednotes.kernel.base import ContractModel, JsonObject

SOURCES_BACKFILL_AUDIT_SCHEMA = "medical-notes-workbench.chats-backfill-audit.v1"
SOURCES_BACKFILL_RECEIPT_SCHEMA = "medical-notes-workbench.chats-backfill-receipt.v1"


class _FallbackChatMetadataProvider:
    def lookup_chat(self, chat_id: str) -> None:
        return None


class _ChatMetadataProvider(Protocol):
    """Small provider contract needed by provenance backfill."""

    def lookup_chat(self, chat_id: str) -> object | None: ...


class _ProvenanceAuditFields(ContractModel):
    """Typed view over provenance audit output used by this backfill flow."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    errors: list[JsonObject] = Field(default_factory=list)
    warnings: list[JsonObject] = Field(default_factory=list)
    blocked_reason: str = ""


class _BackfillReportFields(ContractModel):
    """Closed operational fields for one note provenance backfill report."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    status: str = ""
    chat_ids: list[str] = Field(default_factory=list)
    legacy_urls: list[str] = Field(default_factory=list)
    warnings: list[JsonObject] = Field(default_factory=list)
    wrote: bool = Field(default=False, strict=True)


class _MetadataLookupFields(ContractModel):
    """Typed metadata lookup mode used to summarize backfill execution."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    mode: str = ""


def audit_sources_backfill(config: MedConfig, *, node_modules_path: Path | None = None) -> dict[str, object]:
    provider = MarkdownDbChatMetadataProvider(
        wiki_dir=config.wiki_dir,
        raw_dir=config.raw_dir,
        node_modules_path=node_modules_path,
    )
    reports = [_note_backfill_report(path, provider) for path in _backfillable_notes(config.wiki_dir)]
    return _summary_payload(
        schema=SOURCES_BACKFILL_AUDIT_SCHEMA,
        config=config,
        reports=reports,
        dry_run=True,
        backup=False,
        backup_paths=[],
        write_errors=[],
        metadata_lookup={"mode": "not_required_for_audit", "skipped_reason": "", "warning": None},
    )


def apply_sources_backfill(
    config: MedConfig,
    *,
    backup: bool,
    node_modules_path: Path | None = None,
    metadata_fallback_reason: str = "",
    metadata_warning: JsonObject | None = None,
) -> dict[str, object]:
    backup = False
    if metadata_fallback_reason:
        provider: _ChatMetadataProvider = _FallbackChatMetadataProvider()
        metadata_lookup = {
            "mode": "fallback",
            "skipped_reason": metadata_fallback_reason,
            "warning": metadata_warning,
        }
    else:
        provider = MarkdownDbChatMetadataProvider(
            wiki_dir=config.wiki_dir,
            raw_dir=config.raw_dir,
            node_modules_path=node_modules_path,
        )
        metadata_lookup = {"mode": "markdown_query", "skipped_reason": "", "warning": None}
    reports: list[JsonObject] = []
    backup_paths: list[str] = []
    write_errors: list[dict[str, str]] = []
    for path in _backfillable_notes(config.wiki_dir):
        report = _note_backfill_report(path, provider)
        report_fields = _BackfillReportFields.model_validate(report)
        if report_fields.status != "planned":
            reports.append(report)
            continue
        original = path.read_text(encoding="utf-8")
        try:
            updated = _apply_text(original, report, provider)
            if updated != original:
                atomic_write_text(path, updated)
                report = {**report, "status": "written", "wrote": True, "changed": True}
            else:
                report = {**report, "status": "already_canonical", "wrote": False, "changed": False}
        except (FrontmatterYamlUnavailable, OSError) as exc:
            write_errors.append({"path": str(path), "error": str(exc)})
            report = {**report, "status": "write_error", "wrote": False, "changed": False, "error": str(exc)}
        reports.append(report)
    return _summary_payload(
        schema=SOURCES_BACKFILL_RECEIPT_SCHEMA,
        config=config,
        reports=reports,
        dry_run=False,
        backup=backup,
        backup_paths=backup_paths,
        write_errors=write_errors,
        metadata_lookup=metadata_lookup,
    )


def _backfillable_notes(wiki_dir: Path) -> list[Path]:
    notes: list[Path] = []
    for path in iter_notes(wiki_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            notes.append(path)
            continue
        if _is_index_note(path, text):
            continue
        notes.append(path)
    return notes


def _note_backfill_report(path: Path, provider: _ChatMetadataProvider) -> JsonObject:
    text = path.read_text(encoding="utf-8")
    audit = audit_note_provenance(text, chat_lookup=provider)
    audit_fields = _ProvenanceAuditFields.model_validate(audit)
    state = classify_note_provenance(text)
    base = {
        "path": str(path),
        "chat_ids": list(state.chat_ids),
        "legacy_urls": list(state.legacy_urls),
        "errors": audit_fields.errors,
        "warnings": audit_fields.warnings,
        "wrote": False,
        "changed": False,
    }
    if audit_fields.errors and audit_fields.blocked_reason == "chats.shape_invalid":
        return {**base, "status": "blocked", "blocked_reason": "chats.shape_invalid"}
    if state.status == "already_canonical":
        return {**base, "status": "already_canonical", "blocked_reason": ""}
    if state.status == "migratable" or state.chat_ids:
        return {**base, "status": "planned", "blocked_reason": "", "would_write": True}
    return {**base, "status": "warning", "blocked_reason": "", "would_write": False}


def _apply_text(text: str, report: JsonObject, provider: _ChatMetadataProvider) -> str:
    report_fields = _BackfillReportFields.model_validate(report)
    chats = [ChatProvenance(chat_id) for chat_id in report_fields.chat_ids]
    if not chats:
        chats = [ChatProvenance(url) for url in report_fields.legacy_urls]
    result = apply_note_provenance(text, chats=chats, chat_lookup=provider)
    return str(result["text"])


def _summary_payload(
    *,
    schema: str,
    config: MedConfig,
    reports: list[JsonObject],
    dry_run: bool,
    backup: bool,
    backup_paths: list[str],
    write_errors: list[dict[str, str]],
    metadata_lookup: JsonObject,
) -> dict[str, object]:
    report_fields = [_BackfillReportFields.model_validate(report) for report in reports]
    metadata_fields = _MetadataLookupFields.model_validate(metadata_lookup)
    written_count = sum(1 for report in report_fields if report.wrote)
    planned_count = sum(1 for report in report_fields if report.status == "planned")
    if not dry_run and written_count == 0 and planned_count == 0 and metadata_fields.mode == "markdown_query":
        metadata_lookup = {"mode": "not_required", "skipped_reason": "no_recoverable_sources", "warning": None}
    warning_items = [
        warning
        for report in report_fields
        for warning in report.warnings
    ]
    blocked_count = sum(1 for report in report_fields if report.status in {"blocked", "write_error"})
    status = "blocked" if blocked_count else "planned" if dry_run and planned_count else "completed"
    return {
        "schema": schema,
        "phase": "provenance_backfill",
        "status": status,
        "dry_run": dry_run,
        "backup": backup,
        "wiki_dir": str(config.wiki_dir),
        "raw_dir": str(config.raw_dir),
        "scanned_count": len(reports),
        "recoverable_count": sum(1 for report in report_fields if report.status == "planned"),
        "would_write_count": planned_count if dry_run else 0,
        "written_count": written_count,
        "already_canonical_count": sum(1 for report in report_fields if report.status == "already_canonical"),
        "unrecoverable_count": sum(1 for report in report_fields if report.status == "warning"),
        "warning_count": len(warning_items),
        "warnings": warning_items,
        "blocked_count": blocked_count,
        "write_error_count": len(write_errors),
        "write_errors": write_errors,
        "backup_paths": backup_paths,
        "reports": reports,
        "metadata_lookup": metadata_lookup,
    }
