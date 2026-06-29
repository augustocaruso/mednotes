"""Typed receipt-evidence projection for `/mednotes:fix-wiki`.

Adapters can return raw JSON, but receipt status and counts are workflow
evidence. This module validates those edge payloads before projecting the
`fix-wiki-receipt` JSON consumed by audit/reporting code.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedSyncResult, RelatedNotesRecoveryState
from mednotes.domains.wiki.flows.link.linking import related_notes_sync_blocked
from mednotes.kernel.base import JsonObject, JsonObjectAdapter


class _OperationEvidence(BaseModel):
    """Common typed lens for small phase receipts."""

    model_config = ConfigDict(extra="ignore", strict=True)

    status: StrictStr = "skipped"
    trigger: StrictStr = ""
    receipt_path: StrictStr = ""
    diagnosis_path: StrictStr = ""
    written_count: StrictInt = Field(default=0, ge=0)
    write_error_count: StrictInt = Field(default=0, ge=0)
    warning_count: StrictInt = Field(default=0, ge=0)
    applied_count: StrictInt = Field(default=0, ge=0)
    blocked_count: StrictInt = Field(default=0, ge=0)
    synced_count: StrictInt = Field(default=0, ge=0)
    missing_count: StrictInt = Field(default=0, ge=0)
    removed_empty_dir_count: StrictInt = Field(default=0, ge=0)
    error_count: StrictInt = Field(default=0, ge=0)
    changed_file_count: StrictInt = Field(default=0, ge=0)
    removed_link_count: StrictInt = Field(default=0, ge=0)
    files_changed: StrictInt = Field(default=0, ge=0)
    links_planned: StrictInt = Field(default=0, ge=0)
    changed_count: StrictInt = Field(default=0, ge=0)
    applied_note_count: StrictInt = Field(default=0, ge=0)
    backup_paths: list[StrictStr] = Field(default_factory=list)


class _PlanEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    plan_hash: StrictStr = ""
    snapshot_hash: StrictStr = ""
    status: StrictStr = ""


class _LinkerApplyEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    returncode: StrictInt | None = None
    related_notes_sync: JsonObject = Field(default_factory=dict)
    body_term_linker: _OperationEvidence = Field(default_factory=_OperationEvidence)
    receipt_path: StrictStr = ""
    files_changed: StrictInt = Field(default=0, ge=0)
    changed_file_count: StrictInt = Field(default=0, ge=0)

    @property
    def applied(self) -> bool:
        if self.files_changed or self.changed_file_count:
            return True
        if self.body_term_linker.files_changed or self.body_term_linker.changed_file_count:
            return True
        if self.body_term_linker.applied_note_count:
            return True
        related = _OperationEvidence.model_validate(self.related_notes_sync)
        if related.files_changed or related.changed_file_count or related.applied_note_count:
            return True
        return self.returncode == 0

    @property
    def status_for_receipt(self) -> str:
        if self.returncode is None:
            return "skipped"
        if self.returncode == 0:
            return "completed"
        if self.applied:
            return "partial_blocked"
        return "blocked"


class _RelatedNotesHeadlessEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    partial_record_count: StrictInt = Field(default=0, ge=0)


class _RelatedNotesRecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    related_notes_recovery_state: JsonObject = Field(default_factory=dict)
    headless_export: _RelatedNotesHeadlessEvidence = Field(default_factory=_RelatedNotesHeadlessEvidence)
    blocked_reason: StrictStr = ""

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_missing_recovery_state(cls, value: object) -> JsonObject:
        """Treat absent progress state as empty evidence, not workflow failure."""

        if value is None:
            return {}
        return JsonObjectAdapter.validate_python(value)


def build_fix_wiki_receipt_evidence(
    path: Path,
    *,
    run_id: str,
    status: str,
    fix_wiki_plan: object,
    plan_validation: JsonObject,
    snapshot_hash_before: str,
    snapshot_hash_after: str,
    vocabulary_bootstrap: object,
    hygiene_pre_cleanup: object | None,
    style_fix: object,
    sources_backfill: object,
    alias_projection_apply: object | None,
    vocabulary_hash_sync: object,
    taxonomy_apply: object | None,
    linker_diagnosis: object,
    linker_apply: object | None,
    related_notes_export_recovery: object | None,
    related_notes_safety_cleanup: object,
    hygiene_cleanup: object | None,
    blockers: list[JsonObject],
    skips: list[JsonObject],
    file_changes: list[JsonObject],
) -> JsonObject:
    """Build the fix-wiki receipt after validating adapter evidence."""

    plan = _PlanEvidence.model_validate(fix_wiki_plan)
    vocabulary = _operation(vocabulary_bootstrap)
    hygiene_pre = _operation(hygiene_pre_cleanup)
    hygiene_final = _operation(hygiene_cleanup)
    style = _operation(style_fix)
    provenance = _operation(sources_backfill)
    alias_projection = _operation(alias_projection_apply)
    vocabulary_hash = _operation(vocabulary_hash_sync)
    taxonomy = _operation(taxonomy_apply)
    diagnosis = _operation(linker_diagnosis)
    linker = _linker(linker_apply)
    related_notes_recovery_state = _related_notes_recovery_state(related_notes_export_recovery)
    related_notes_recovery_payload = _related_notes_recovery_state_payload(related_notes_recovery_state)
    related_notes_recovery_blocked = bool(
        related_notes_recovery_state is not None
        and (
            related_notes_recovery_state.blocked_reason
            or related_notes_recovery_state.status in {"waiting_for_retry", "blocked", "failed"}
        )
    )
    related_notes_safety = _operation(related_notes_safety_cleanup)
    body_linker_applied = bool(linker.body_term_linker.files_changed or linker.body_term_linker.links_planned)

    return JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.fix-wiki-receipt.v1",
            "run_id": run_id,
            "status": status,
            "receipt_path": str(path),
            "plan": {
                "plan_hash": plan.plan_hash,
                "snapshot_hash": plan.snapshot_hash,
                "status": plan.status,
            },
            "plan_validation": plan_validation,
            "snapshots": {
                "before_hash": snapshot_hash_before,
                "after_hash": snapshot_hash_after,
            },
            "phase_receipts": {
                "vocabulary_bootstrap": {
                    "status": vocabulary.status,
                    "trigger": vocabulary.trigger,
                    "receipt_path": vocabulary.receipt_path,
                },
                "hygiene": {
                    "preflight_removed_empty_dir_count": hygiene_pre.removed_empty_dir_count,
                    "final_removed_empty_dir_count": hygiene_final.removed_empty_dir_count,
                    "error_count": hygiene_final.error_count + hygiene_pre.error_count,
                },
                "style_yaml": {
                    "written_count": style.written_count,
                    "write_error_count": style.write_error_count,
                },
                "provenance_backfill": {
                    "status": provenance.status,
                    "written_count": provenance.written_count,
                    "warning_count": provenance.warning_count,
                    "receipt_path": provenance.receipt_path,
                },
                "alias_projection": {
                    "status": alias_projection.status,
                    "applied_count": alias_projection.applied_count,
                    "blocked_count": alias_projection.blocked_count,
                    "receipt_path": alias_projection.receipt_path,
                },
                "vocabulary_hash_sync": {
                    "status": vocabulary_hash.status,
                    "synced_count": vocabulary_hash.synced_count,
                    "missing_count": vocabulary_hash.missing_count,
                },
                "taxonomy": {
                    "status": taxonomy.status,
                    "applied_count": taxonomy.applied_count,
                    "receipt_path": taxonomy.receipt_path,
                },
                "linker": {
                    "status": linker.status_for_receipt,
                    "diagnosis_status": diagnosis.status,
                    "diagnosis_path": diagnosis.diagnosis_path,
                    "applied": linker.applied,
                    "body_term_linker_applied": body_linker_applied,
                    "related_notes_blocked": related_notes_sync_blocked(linker.related_notes_sync)
                    or related_notes_recovery_blocked,
                    "receipt_path": linker.receipt_path,
                    "files_changed": linker.files_changed,
                },
                "related_notes_recovery": _related_notes_recovery_receipt(related_notes_recovery_state),
                "related_notes_safety_cleanup": {
                    "status": related_notes_safety.status,
                    "changed_file_count": related_notes_safety.changed_file_count,
                    "removed_link_count": related_notes_safety.removed_link_count,
                    "backup_paths": related_notes_safety.backup_paths,
                },
                "final_validation": {
                    "snapshot_hash": snapshot_hash_after,
                },
            },
            "file_changes": file_changes,
            "related_notes_recovery_state": related_notes_recovery_payload,
            "blockers": blockers,
            "skips": skips,
            "rollback": {
                "strategy": "git_or_phase_receipts",
                "taxonomy_receipt_path": taxonomy.receipt_path,
                "linker_receipt_path": linker.receipt_path,
            },
        }
    )


def _operation(payload: object | None) -> _OperationEvidence:
    return _OperationEvidence.model_validate(payload or {})


def _linker(payload: object | None) -> _LinkerApplyEvidence:
    return _LinkerApplyEvidence.model_validate(payload or {})


def _related_notes_recovery_state(payload: object | None) -> RelatedNotesRecoveryState | None:
    if isinstance(payload, RelatedNotesRecoveryState):
        return payload if payload else None
    if isinstance(payload, LinkRelatedSyncResult):
        state = payload.related_notes_recovery_state
        return state if state else None
    if payload is None:
        return None
    evidence = _RelatedNotesRecoveryEvidence.model_validate(payload)
    if evidence.related_notes_recovery_state:
        recovery_state = RelatedNotesRecoveryState.from_payload(evidence.related_notes_recovery_state)
        return recovery_state if recovery_state else None
    if not evidence.headless_export.partial_record_count:
        return None
    return RelatedNotesRecoveryState.from_headless_projection(
        evidence.headless_export.model_dump(mode="json"),
        blocked_reason=evidence.blocked_reason,
    )


def _related_notes_recovery_state_payload(state: RelatedNotesRecoveryState | None) -> JsonObject:
    if state is None:
        return {}
    payload = state.to_payload()
    payload.pop("operation_payload", None)
    return JsonObjectAdapter.validate_python(payload)


def _related_notes_recovery_receipt(state: RelatedNotesRecoveryState | None) -> JsonObject:
    if state is None:
        return {
            "status": "skipped",
            "blocked_reason": "",
            "fresh_record_count": 0,
            "stale_record_count": 0,
            "record_count": 0,
            "total_note_count": 0,
            "remaining_count": 0,
            "next_retry_after_seconds": 0,
        }
    return {
        "status": state.status,
        "blocked_reason": state.blocked_reason,
        "fresh_record_count": state.fresh_record_count,
        "stale_record_count": state.stale_record_count,
        "record_count": state.record_count,
        "total_note_count": state.total_note_count,
        "remaining_count": state.remaining_count,
        "next_retry_after_seconds": state.next_retry_after_seconds,
    }
