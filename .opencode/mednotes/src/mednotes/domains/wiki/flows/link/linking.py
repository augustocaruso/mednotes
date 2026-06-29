"""Semantic linker and graph-audit orchestration for the Wiki CLI."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import canonical_json_hash, file_sha256
from mednotes.domains.wiki.capabilities.body_link.body_linker import (
    DEFAULT_LLM_DISAMBIGUATION_MODEL,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    apply_body_linker_plan,
)
from mednotes.domains.wiki.capabilities.body_link.body_linker import (
    run_body_linker as run_db_body_linker,
)
from mednotes.domains.wiki.capabilities.graph import graph as wiki_graph
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import infer_title
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    RELATED_NOTES_SYNC_SCHEMA,
    default_export_path,
    recover_related_notes_export_operation_result,
    sync_related_notes_operation_result,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import extract_aliases, normalize_key
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_bootstrap import planned_vocabulary_bootstrap
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import build_vocabulary_curator_batch_plan
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_ingestion import apply_semantic_ingestion
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import (
    initialize_vocabulary_db,
    load_vocabulary_map_diagnosis,
    note_content_hash,
    upsert_note,
)
from mednotes.domains.wiki.common import ValidationError, _now_iso, wiki_cli_command
from mednotes.domains.wiki.config import MedConfig, _path
from mednotes.domains.wiki.contracts.link_git import LinkGitContext, LinkState
from mednotes.domains.wiki.contracts.related_notes_runtime import (
    LinkRelatedSyncResult,
    RelatedNotesPassSummary,
    RelatedNotesRecoveryState,
)
from mednotes.domains.wiki.contracts.workflow_blockers import blocker_entry, decision_for_code
from mednotes.domains.wiki.contracts.workflow_guardrails import LINK_REQUIRED_INPUTS
from mednotes.domains.wiki.flows.link.link_git import (
    collect_git_context,
    load_link_state,
    trigger_context_from_git,
    write_link_state,
)
from mednotes.domains.wiki.flows.link.link_retry_governance import (
    build_diagnosis_identity,
    force_diagnose_event,
    record_diagnosis_attempt,
    redundant_diagnosis_payload,
)
from mednotes.domains.wiki.flows.link.link_triggers import (
    affected_notes_from_context,
    derive_triggers,
    is_image_only_context,
    load_trigger_context,
    structural_events_from_context,
)
from mednotes.domains.wiki.flows.link.reference_repair import apply_reference_repair_plan, plan_reference_repair
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

LINK_DIAGNOSIS_SCHEMA = "medical-notes-workbench.link-diagnosis.v1"
LINK_RUN_SCHEMA = "medical-notes-workbench.link-run.v1"
LINK_RUN_RECEIPT_SCHEMA = "medical-notes-workbench.link-run-receipt.v1"
RELATED_NOTES_CONVERGENCE_MAX_PASSES = 3
LINK_PHASE_ORDER = (
    "reference_repair",
    "contextual_alias_disambiguation",
    "body_term_linker",
    "related_notes_sync",
    "graph_validation",
)


def _json_object(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return cast(JsonObject, payload)
    return JsonObjectAdapter.validate_python(payload)


def _json_object_or_empty(payload: object | None) -> JsonObject:
    return _json_object(payload) if isinstance(payload, dict) else {}


def _json_field(payload: JsonObject, key: str, default: object = "") -> object:
    return payload[key] if key in payload else default


def _json_text(payload: JsonObject, key: str, default: str = "") -> str:
    """Read a text field from already-validated JSON without loose `str(x or "")` fallback."""

    value = _json_field(payload, key, default)
    return value if isinstance(value, str) else default


def _json_bool(payload: JsonObject, key: str, default: bool = False) -> bool:
    """Read boolean flags used for linker flow decisions from validated JSON."""

    value = _json_field(payload, key, default)
    return value if isinstance(value, bool) else default


def _json_int(payload: JsonObject, key: str, default: int = 0) -> int:
    value = _json_field(payload, key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


_VocabularyMapStatus = Literal[
    "",
    "blocked",
    "blocked_human",
    "blocked_pending",
    "failed",
    "planned",
    "ready",
    "skipped",
]
_VocabularyBootstrapStatus = Literal[
    "",
    "completed",
    "existing",
    "failed",
    "planned",
    "queued_semantic_ingestion",
    "ready",
    "skipped",
]
_VocabularySemanticRepairStatus = Literal["completed", "completed_with_blockers", "skipped"]


class _ContextualAliasDiagnosis(ContractModel):
    """Typed view for contextual alias evidence emitted by the body linker."""

    model_config = ConfigDict(extra="ignore")

    status: str = "skipped"
    mode: str = ""
    candidate_count: int = Field(default=0, ge=0, strict=True)
    decision_count: int = Field(default=0, ge=0, strict=True)
    linked_count: int = Field(default=0, ge=0, strict=True)
    deferred_count: int = Field(default=0, ge=0, strict=True)
    no_link_count: int = Field(default=0, ge=0, strict=True)
    rejected_count: int = Field(default=0, ge=0, strict=True)
    skipped_reason: str = ""
    blocked_reason: str = ""

    @classmethod
    def from_payload(cls, value: object) -> _ContextualAliasDiagnosis:
        return cls.model_validate(_json_object_or_empty(value))


class _GitContextView(ContractModel):
    """Typed projection of git context fields consumed by link apply safety."""

    model_config = ConfigDict(extra="ignore")

    available: bool = False
    repo_root: str = ""
    branch: str = ""
    head: str = ""
    status_hash: str = ""
    changed_paths: list[JsonObject] = Field(default_factory=list)
    unavailable_reason: str = ""

    @field_validator("changed_paths", mode="before")
    @classmethod
    def _changed_paths_or_empty(cls, value: object) -> list[JsonObject]:
        if not isinstance(value, list):
            return []
        return [_json_object(item) for item in value if isinstance(item, dict)]

    @classmethod
    def from_payload(cls, value: object) -> _GitContextView:
        return cls.model_validate(_json_object_or_empty(value))


class _GraphAuditView(ContractModel):
    """Typed graph audit counts that determine link apply completion."""

    model_config = ConfigDict(extra="ignore")

    error_count: int = Field(default=0, ge=0, strict=True)
    warning_count: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def from_payload(cls, value: object) -> _GraphAuditView:
        return cls.model_validate(_json_object_or_empty(value))


class _VocabularyMapIssueView(ContractModel):
    """Typed vocabulary-map issue used to construct link blockers."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""
    message: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    decision_summary: JsonObject | None = None
    severity: str = ""


class _VocabularyMapDiagnosisView(ContractModel):
    """Typed view of vocabulary diagnosis fields that can block link diagnosis."""

    model_config = ConfigDict(extra="ignore")

    status: _VocabularyMapStatus = ""
    pending_semantic_ingestion_count: int = Field(default=0, ge=0, strict=True)
    note_count: int = Field(default=0, ge=0, strict=True)
    meaning_count: int = Field(default=0, ge=0, strict=True)
    issues: list[_VocabularyMapIssueView] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: object) -> _VocabularyMapDiagnosisView:
        return cls.model_validate(_json_object_or_empty(value))


class _VocabularyBootstrapView(ContractModel):
    """Typed vocabulary bootstrap diagnosis before it controls link phases."""

    model_config = ConfigDict(extra="ignore")

    status: _VocabularyBootstrapStatus = ""
    note_count: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def from_payload(cls, value: object) -> _VocabularyBootstrapView:
        return cls.model_validate(_json_object_or_empty(value))


class _VocabularySemanticRepairView(ContractModel):
    """Typed vocabulary repair receipt before link apply can branch on it."""

    model_config = ConfigDict(extra="ignore")

    schema_id: Literal["medical-notes-workbench.vocabulary-semantic-repair.v1"] = Field(alias="schema")
    status: _VocabularySemanticRepairStatus
    blocked_reason: str = ""
    next_action: str = ""
    human_decision_required: bool = Field(default=False, strict=True)
    applied_count: int = Field(default=0, ge=0, strict=True)
    blocked_count: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def from_payload(cls, value: object) -> _VocabularySemanticRepairView:
        return cls.model_validate(_json_object(value))


class _ReferenceRepairView(ContractModel):
    """Typed view of the reference-repair plan used by linker phases and receipts."""

    model_config = ConfigDict(extra="ignore")

    status: str = "skipped"
    affected_note_count: int = Field(default=0, ge=0, strict=True)
    action_count: int = Field(default=0, ge=0, strict=True)
    blocking_action_count: int = Field(default=0, ge=0, strict=True)
    human_decision_count: int = Field(default=0, ge=0, strict=True)
    triage_count: int = Field(default=0, ge=0, strict=True)
    human_decision_packets: list[JsonObject] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: object) -> _ReferenceRepairView:
        return cls.model_validate(_json_object_or_empty(value))


class _ReferenceApplyView(ContractModel):
    """Typed reference-repair apply receipt before link receipts consume it."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    changed_file_count: int = Field(default=0, ge=0, strict=True)
    reports: list[JsonObject] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: object | None) -> _ReferenceApplyView:
        return cls.model_validate(_json_object_or_empty(value))


class _LinkBodyPlanView(ContractModel):
    """Typed body-linker note plan used for changed-file and skip accounting."""

    model_config = ConfigDict(extra="ignore")

    file: str = ""
    changed: bool = False
    insertions: list[JsonObject] = Field(default_factory=list)
    rewrites: list[JsonObject] = Field(default_factory=list)
    skipped: list[JsonObject] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: object) -> _LinkBodyPlanView:
        return cls.model_validate(_json_object_or_empty(value))


class _LinkPlanSkipView(ContractModel):
    """Typed skipped occurrence emitted by contextual/body linker planning."""

    model_config = ConfigDict(extra="ignore")

    occurrence_id: str = ""
    reason_code: str = ""
    action: str = ""

    @classmethod
    def from_payload(cls, value: object) -> _LinkPlanSkipView:
        return cls.model_validate(_json_object_or_empty(value))


class _SnapshotNoteView(ContractModel):
    """Typed note entry from a link snapshot."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    content_hash: str = ""

    @classmethod
    def from_payload(cls, value: object) -> _SnapshotNoteView:
        return cls.model_validate(_json_object_or_empty(value))


class _SemanticIngestionItemView(ContractModel):
    """Typed subset of semantic-ingestion items used for repair error receipts."""

    model_config = ConfigDict(extra="ignore")

    note_path: str = ""
    content_hash: str = ""

    @classmethod
    def from_payload(cls, value: object) -> _SemanticIngestionItemView:
        return cls.model_validate(_json_object_or_empty(value))


class _BodyLinkerView(ContractModel):
    """Typed view over body-linker output before it is folded into the link FSM."""

    model_config = ConfigDict(extra="ignore")

    status: str = "skipped"
    blocked_reason: str = ""
    next_action: str = ""
    returncode: int = Field(default=0, ge=0, strict=True)
    files_changed: int = Field(default=0, ge=0, strict=True)
    links_planned: int = Field(default=0, ge=0, strict=True)
    links_rewritten: int = Field(default=0, ge=0, strict=True)
    blocker_count: int = Field(default=0, ge=0, strict=True)
    error: str = ""
    parse_error: str = ""
    body_linker_mode: str = ""
    contextual_alias_disambiguation: _ContextualAliasDiagnosis = Field(default_factory=_ContextualAliasDiagnosis)
    graph_audit_before: JsonObject = Field(default_factory=dict)
    vocabulary_map_diagnosis: _VocabularyMapDiagnosisView = Field(default_factory=_VocabularyMapDiagnosisView)
    plans: list[_LinkBodyPlanView] = Field(default_factory=list)
    blockers: list[JsonObject] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, value: object) -> _BodyLinkerView:
        return cls.model_validate(_json_object_or_empty(value))


class _LinkDiagnosisView(ContractModel):
    """Typed view of saved link diagnosis fields consumed by apply preflight."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    human_decision_required: bool = False
    diagnosis_path: str = ""
    wiki_dir: str = ""
    vocabulary_db_path: str = ""
    blocker_count: int = Field(default=0, ge=0)
    blockers: list[JsonObject] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    files_changed: int = Field(default=0, ge=0)
    snapshot_hash: str = ""
    plan_hash: str = ""
    trigger_context: JsonObject = Field(default_factory=dict)
    triggers_detected: list[str] = Field(default_factory=list)
    affected_notes: list[JsonObject] = Field(default_factory=list)
    git: _GitContextView = Field(default_factory=_GitContextView)
    reference_repair: _ReferenceRepairView = Field(default_factory=_ReferenceRepairView)
    body_term_linker: _BodyLinkerView = Field(default_factory=_BodyLinkerView)
    related_notes_sync: JsonObject = Field(default_factory=dict)
    version_control_safety: JsonObject = Field(default_factory=dict)
    receipt: JsonObject = Field(default_factory=dict)
    guard_receipt: JsonObject = Field(default_factory=dict)
    vocabulary_bootstrap: JsonObject = Field(default_factory=dict)

    @field_validator(
        "trigger_context",
        "related_notes_sync",
        "version_control_safety",
        "receipt",
        "guard_receipt",
        "vocabulary_bootstrap",
        mode="before",
    )
    @classmethod
    def _object_or_empty(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)

    @field_validator("affected_notes", mode="before")
    @classmethod
    def _affected_notes_or_empty(cls, value: object) -> list[JsonObject]:
        if not isinstance(value, list):
            return []
        return [_json_object(item) for item in value if isinstance(item, dict)]

    @field_validator("changed_files", "triggers_detected", mode="before")
    @classmethod
    def _string_list_or_empty(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @classmethod
    def from_payload(cls, value: object) -> _LinkDiagnosisView:
        return cls.model_validate(_json_object_or_empty(value))

    def version_control_safety_payload(self) -> JsonObject:
        if self.version_control_safety:
            return self.version_control_safety
        receipt = _json_object_or_empty(self.receipt)
        nested = _json_object_or_empty(_json_field(receipt, "version_control_safety"))
        if nested:
            return nested
        guard_receipt = _json_object_or_empty(self.guard_receipt)
        return _json_object_or_empty(_json_field(guard_receipt, "version_control_safety"))


def related_notes_sync_blocked(result: object) -> bool:
    typed = LinkRelatedSyncResult.from_payload(result)
    return typed.status == "blocked" or bool(typed.blocked_reason)


def _related_notes_apply_blocked(result: LinkRelatedSyncResult | None, *, required: bool) -> bool:
    """Treat skipped Related Notes as blocking only when the parent made it required."""

    if result is None:
        return False
    return related_notes_sync_blocked(result) or (
        required and result.status == "skipped" and bool(result.skipped_reason)
    )


def _related_notes_required_for_apply(diagnosis: JsonObject) -> bool:
    """Process-chats publishes new notes, so its link package must close Related Notes too."""

    context = _json_object_or_empty(_json_field(diagnosis, "trigger_context"))
    return _json_field(context, "source_workflow") == "/mednotes:process-chats"


def _related_notes_waiting_external(result: LinkRelatedSyncResult | None) -> bool:
    if result is None:
        return False
    recovery = result.related_notes_recovery_state
    return (
        recovery.status == "waiting_for_retry"
        and recovery.blocked_reason
        in {
            "related_notes_headless_quota_exhausted",
            "related_notes_headless_time_budget_exhausted",
        }
    )


def _related_notes_recovery_payload(result: LinkRelatedSyncResult | None) -> JsonObject:
    if result is None:
        return {}
    recovery = result.related_notes_recovery_state
    return recovery.to_payload() if recovery else {}


def _link_diagnosis_contract_invalid_payload(
    *,
    diagnosis_path: Path,
    detail: str,
    source_payload: JsonObject,
    extra: JsonObject | None = None,
) -> JsonObject:
    """Block apply when a saved/refreshed link artifact is not FSM-complete."""

    return _json_object(
        {
            "schema": LINK_RUN_SCHEMA,
            "phase": "link_apply_preflight",
            "status": "blocked",
            "blocked_reason": "link_diagnosis_contract_invalid",
            "next_action": "Reexecutar /mednotes:link --diagnose para gerar diagnóstico FSM válido.",
            "required_inputs": ["diagnosis"],
            "human_decision_required": False,
            "diagnosis_path": str(diagnosis_path),
            "error_context": {
                "root_cause": "effect_payload_contract_invalid",
                "detail": detail,
            },
            "invalid_diagnosis": source_payload,
            "returncode": 3,
            **(extra or {}),
        }
    )


def _link_apply_blocked_reason(
    *,
    body_or_graph_blocked: bool,
    related_notes_blocked: bool,
) -> str:
    if related_notes_blocked:
        return "related_notes_blocked"
    if body_or_graph_blocked:
        return "graph_blockers"
    return ""


def _link_apply_next_action(
    *,
    blocked_reason: str,
    related_notes: LinkRelatedSyncResult | None,
) -> str:
    if blocked_reason == "related_notes_blocked":
        return related_notes.next_action if related_notes is not None and related_notes.next_action else (
            "Atualizar o export do Related Notes ou aguardar a cota externa e repetir /mednotes:link."
        )
    if blocked_reason == "graph_blockers":
        return "Rodar /mednotes:fix-wiki --dry-run para resolver blockers semânticos."
    return ""


def run_related_notes_sync(
    config: MedConfig,
    *,
    apply: bool = False,
    backup: bool = False,
    receipt_path: Path | None = None,
    max_age_hours: float = 168.0,
    allow_stale_note_hashes: bool = False,
) -> LinkRelatedSyncResult:
    export_path = default_export_path(config.wiki_dir)
    if not export_path.is_file():
        return LinkRelatedSyncResult.from_payload(
            {
                "schema": RELATED_NOTES_SYNC_SCHEMA,
                "phase": "related_notes_skipped",
                "status": "skipped",
                "blocked_reason": "",
                "skipped_reason": "related_notes_export_missing",
                "next_action": (
                    "Exportar medical-notes-export.json pelo plugin Related Notes para sincronizar "
                    "a seção gerenciada Notas Relacionadas."
                ),
                "required_inputs": ["wiki_dir", "related_notes_export"],
                "human_decision_required": False,
                "wiki_dir": str(config.wiki_dir),
                "export_path": "",
                "default_export_name": "medical-notes-export.json",
                "applied_note_count": 0,
                "planned_note_count": 0,
                "proposed_link_count": 0,
                "cleared_link_count": 0,
            }
        )
    return LinkRelatedSyncResult.from_payload(
        sync_related_notes_operation_result(
            config,
            export_path=export_path,
            apply=apply,
            backup=backup,
            receipt_path=receipt_path,
            max_age_hours=max_age_hours,
            allow_stale_note_hashes=allow_stale_note_hashes,
        )
    )


def _related_notes_payload(result: LinkRelatedSyncResult | None) -> JsonObject | None:
    return _json_object(result.operation_payload) if result is not None else None


def _related_notes_required_inputs(result: LinkRelatedSyncResult) -> list[str]:
    return result.required_inputs or ["wiki_dir", "related_notes_export"]


def _related_notes_pass_summary(kind: str, result: LinkRelatedSyncResult) -> RelatedNotesPassSummary:
    return RelatedNotesPassSummary.from_sync_result(kind, result)


def _combined_related_notes_updates(results: list[LinkRelatedSyncResult]) -> list[JsonObject]:
    combined: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        for update in result.updates:
            payload = _json_object(update.operation_payload)
            path = update.path or _json_text(payload, "file")
            key = (path, json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            combined.append(payload)
    return combined


def _convergence_state_from_blocked(result: LinkRelatedSyncResult) -> str:
    if result.related_notes_recovery_state.status == "waiting_for_retry":
        return "waiting_external"
    if result.blocked_reason in {
        "related_notes_headless_quota_exhausted",
        "related_notes_headless_time_budget_exhausted",
    }:
        return "waiting_external"
    return "blocked"


def _related_notes_convergence_result(
    *,
    base: LinkRelatedSyncResult,
    status: str,
    blocked_reason: str = "",
    next_action: str = "",
    applied_results: list[LinkRelatedSyncResult],
    passes: list[RelatedNotesPassSummary],
    final_planned_note_count: int,
    max_passes: int,
    extra: JsonObject | None = None,
) -> LinkRelatedSyncResult:
    result = _json_object(base.operation_payload)
    if extra:
        result.update(extra)
    total_applied = sum(payload.applied_note_count for payload in applied_results)
    operation_count = len(passes)
    cycle_count = sum(1 for item in passes if item.kind == "apply")
    blocked_probe = LinkRelatedSyncResult.from_payload({**result, "status": status, "blocked_reason": blocked_reason})
    result.update(
        {
            "schema": RELATED_NOTES_SYNC_SCHEMA,
            "phase": "related_notes_apply_convergence",
            "status": status,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "planned_note_count": final_planned_note_count,
            "applied_note_count": total_applied,
            "updates": _combined_related_notes_updates(applied_results),
            "convergence": {
                "schema": "medical-notes-workbench.related-notes-convergence.v1",
                "status": "stable" if status == "completed" else _convergence_state_from_blocked(blocked_probe),
                "pass_count": cycle_count,
                "max_passes": max_passes,
                "cycle_count": cycle_count,
                "max_cycles": max_passes,
                "operation_count": operation_count,
                "final_planned_note_count": final_planned_note_count,
                "applied_note_count": total_applied,
                "passes": [item.to_payload() for item in passes],
            },
        }
    )
    return LinkRelatedSyncResult.from_payload(result)


def _related_notes_convergence_base(config: MedConfig) -> LinkRelatedSyncResult:
    export_path = default_export_path(config.wiki_dir)
    return LinkRelatedSyncResult.from_payload(
        {
            "schema": RELATED_NOTES_SYNC_SCHEMA,
            "phase": "related_notes_apply_convergence",
            "status": "blocked",
            "blocked_reason": "",
            "next_action": "",
            "required_inputs": ["wiki_dir", "related_notes_export"],
            "human_decision_required": False,
            "manual_instruction_allowed": False,
            "wiki_dir": str(config.wiki_dir),
            "export_path": str(export_path),
            "planned_note_count": 0,
            "proposed_link_count": 0,
            "cleared_link_count": 0,
            "skipped_edge_count": 0,
            "applied_note_count": 0,
            "updates": [],
        }
    )


def _related_notes_recovery_sync_result(value: object) -> LinkRelatedSyncResult:
    """Project export recovery into the sync-result stream without erasing its type."""

    raw = _json_object_or_empty(value)
    nested_recovery = raw["related_notes_recovery_state"] if "related_notes_recovery_state" in raw else value
    recovery = RelatedNotesRecoveryState.from_payload(nested_recovery)
    # Keep the adapter's recovery evidence at the operation boundary. The typed
    # recovery state drives resumability; stale-note/API evidence remains
    # audit-only, but fix-wiki must still be able to prove what was recovered.
    payload: JsonObject = {
        **raw,
        "schema": RELATED_NOTES_SYNC_SCHEMA,
        "phase": "related_notes_export_recovery",
        "status": recovery.status or _json_text(raw, "status"),
        "blocked_reason": recovery.blocked_reason or _json_text(raw, "blocked_reason"),
        "next_action": recovery.next_action or _json_text(raw, "next_action"),
        "related_notes_recovery_state": recovery.to_payload(),
    }
    return LinkRelatedSyncResult.from_payload(
        payload
    )


def _converge_related_notes_sync(
    config: MedConfig,
    *,
    backup: bool,
    max_passes: int = RELATED_NOTES_CONVERGENCE_MAX_PASSES,
) -> LinkRelatedSyncResult:
    if not default_export_path(config.wiki_dir).is_file():
        return run_related_notes_sync(config, apply=True, backup=backup)
    applied_results: list[LinkRelatedSyncResult] = []
    passes: list[RelatedNotesPassSummary] = []
    last_payload = _related_notes_convergence_base(config)

    for pass_index in range(1, max_passes + 1):
        recovery = _related_notes_recovery_sync_result(
            recover_related_notes_export_operation_result(
                config,
                mode="auto",
                workflow="/mednotes:link",
                run_id=f"related-notes-convergence-{pass_index}",
            )
        )
        passes.append(_related_notes_pass_summary("recover_export", recovery))
        if related_notes_sync_blocked(recovery):
            return _related_notes_convergence_result(
                base=last_payload,
                status="blocked",
                blocked_reason=recovery.blocked_reason or "related_notes_export_recovery_blocked",
                next_action=recovery.next_action,
                applied_results=applied_results,
                passes=passes,
                final_planned_note_count=last_payload.planned_note_count,
                max_passes=max_passes,
                extra={
                    "related_notes_export_recovery": recovery.operation_payload,
                    "related_notes_recovery_state": recovery.related_notes_recovery_state.operation_payload,
                },
            )

        preview = run_related_notes_sync(config, apply=False, backup=False)
        passes.append(_related_notes_pass_summary("dry_run", preview))
        if preview.status == "skipped":
            return preview
        planned_note_count = preview.planned_note_count
        if related_notes_sync_blocked(preview):
            return _related_notes_convergence_result(
                base=preview,
                status="blocked",
                blocked_reason=preview.blocked_reason or "related_notes_preview_blocked",
                next_action=preview.next_action,
                applied_results=applied_results,
                passes=passes,
                final_planned_note_count=planned_note_count,
                max_passes=max_passes,
                extra={"related_notes_export_recovery": recovery.operation_payload},
            )
        if planned_note_count == 0:
            return _related_notes_convergence_result(
                base=preview,
                status="completed",
                applied_results=applied_results,
                passes=passes,
                final_planned_note_count=0,
                max_passes=max_passes,
                extra={"related_notes_export_recovery": recovery.operation_payload},
            )

        last_payload = run_related_notes_sync(config, apply=True, backup=backup)
        passes.append(_related_notes_pass_summary("apply", last_payload))
        if related_notes_sync_blocked(last_payload):
            return _related_notes_convergence_result(
                base=last_payload,
                status="blocked",
                blocked_reason=last_payload.blocked_reason or "related_notes_apply_blocked",
                next_action=last_payload.next_action,
                applied_results=applied_results,
                passes=passes,
                final_planned_note_count=planned_note_count,
                max_passes=max_passes,
                extra={"related_notes_export_recovery": recovery.operation_payload},
            )
        applied_results.append(last_payload)
        if last_payload.applied_note_count >= planned_note_count:
            return _related_notes_convergence_result(
                base=last_payload,
                status="completed",
                applied_results=applied_results,
                passes=passes,
                final_planned_note_count=0,
                max_passes=max_passes,
                extra={"related_notes_export_recovery": recovery.operation_payload},
            )

    return _related_notes_convergence_result(
        base=last_payload,
        status="blocked",
        blocked_reason="related_notes_convergence_not_reached",
        next_action="A sincronização de Notas Relacionadas ainda mudou após várias passadas; repetir pela rota oficial depois de revisar o relatório.",
        applied_results=applied_results,
        passes=passes,
        final_planned_note_count=last_payload.planned_note_count,
        max_passes=max_passes,
    )


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def default_link_diagnosis_path() -> Path:
    return _path(f"~/.mednotes/runs/{_run_id()}/link-diagnosis.json")


def default_link_receipt_path() -> Path:
    return _path(f"~/.mednotes/runs/{_run_id()}/link-run-receipt.json")


def _first_heading_or_stem(text: str, path: Path) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem


def _file_hash(path: Path | None) -> str:
    if not path or not path.is_file():
        return ""
    return "sha256:" + file_sha256(path)


def _collect_snapshot(config: MedConfig) -> JsonObject:
    notes: list[JsonObject] = []
    if config.wiki_dir.is_dir():
        for path in iter_notes(config.wiki_dir):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                notes.append(
                    _json_object({
                        "path": path.relative_to(config.wiki_dir).as_posix(),
                        "read_error": str(exc),
                    })
                )
                continue
            if _is_index_note(path, text):
                continue
            notes.append(
                _json_object({
                    "path": path.relative_to(config.wiki_dir).as_posix(),
                    "stem": path.stem,
                    "title": _first_heading_or_stem(text, path),
                    "aliases": extract_aliases(text),
                    "content_hash": "sha256:" + file_sha256(path),
                })
            )
    export_path = default_export_path(config.wiki_dir)
    snapshot = JsonObjectAdapter.validate_python({
        "wiki_dir": str(config.wiki_dir),
        "wiki_dir_exists": config.wiki_dir.is_dir(),
        "catalog_path": str(config.catalog_path) if config.catalog_path else "",
        "catalog_hash": _file_hash(config.catalog_path),
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "vocabulary_db_hash": _file_hash(config.vocabulary_db_path),
        "related_notes_export_path": str(export_path) if export_path.is_file() else "",
        "related_notes_export_hash": _file_hash(export_path),
        "note_count": len(notes),
        "notes": notes,
    })
    snapshot["snapshot_hash"] = "sha256:" + canonical_json_hash(snapshot)
    return snapshot


def _git_context_for(config: MedConfig, link_state: LinkState | None = None) -> LinkGitContext:
    """Collect typed Git context before any linker branch reads its fields."""

    return collect_git_context(
        config.wiki_dir,
        previous_state=link_state if link_state is not None else load_link_state(),
    )


def _git_trigger_context_for(
    git_context: LinkGitContext,
    *,
    snapshot: JsonObject,
    link_state: LinkState | None,
) -> JsonObject | None:
    if link_state is not None and link_state.snapshot_hash == _json_field(snapshot, "snapshot_hash"):
        return None
    trigger_context = trigger_context_from_git(git_context)
    return trigger_context.to_payload() if trigger_context is not None else None


def _phase_status_from_linker(payload: JsonObject, *, phase: str) -> str:
    if _json_field(payload, "error") or _json_field(payload, "parse_error"):
        return "failed"
    if _json_int(payload, "blocker_count"):
        return "blocked"
    if phase == "body_term_linker":
        return (
            "planned"
            if _json_int(payload, "links_planned")
            or _json_int(payload, "links_rewritten")
            else "skipped"
        )
    return "planned"


def _graph_issues_from(body_linker: JsonObject) -> list[JsonObject]:
    body_view = _BodyLinkerView.from_payload(body_linker)
    graph = body_view.graph_audit_before
    issues: list[JsonObject] = []
    for key in ("errors", "warnings"):
        values = graph.get(key)
        if isinstance(values, list):
            issues.extend(_json_object(item) for item in values if isinstance(item, dict))
    return issues


def _diagnosis_phases(
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
    reference_repair: JsonObject,
) -> JsonObject:
    body_view = _BodyLinkerView.from_payload(body_linker)
    repair_view = _ReferenceRepairView.from_payload(reference_repair)
    related_status = "skipped"
    if related_notes is not None:
        if related_notes_sync_blocked(related_notes):
            related_status = "blocked"
        elif related_notes.planned_note_count:
            related_status = "planned"
        else:
            related_status = related_notes.status or "skipped"
    contextual = body_view.contextual_alias_disambiguation
    return _json_object({
        "reference_repair": {
            "status": repair_view.status,
            "affected_note_count": repair_view.affected_note_count,
            "action_count": repair_view.action_count,
            "blocking_action_count": repair_view.blocking_action_count,
            "human_decision_count": repair_view.human_decision_count,
            "triage_count": repair_view.triage_count,
        },
        "contextual_alias_disambiguation": {
            "status": contextual.status,
            "mode": contextual.mode,
            "candidate_count": contextual.candidate_count,
            "linked_count": contextual.linked_count,
            "deferred_count": contextual.deferred_count,
            "no_link_count": contextual.no_link_count,
            "rejected_count": contextual.rejected_count,
            "skipped_reason": contextual.skipped_reason,
            "blocked_reason": contextual.blocked_reason,
        },
        "body_term_linker": {
            "status": _phase_status_from_linker(body_linker, phase="body_term_linker"),
            "blocked_reason": body_view.blocked_reason,
            "links_planned": body_view.links_planned,
            "links_rewritten": body_view.links_rewritten,
        },
        "related_notes_sync": {
            "status": related_status,
            "planned_note_count": related_notes.planned_note_count if related_notes is not None else 0,
            "skipped_reason": related_notes.skipped_reason if related_notes is not None else "",
            "blocked_reason": related_notes.blocked_reason if related_notes is not None else "",
        },
        "graph_validation": {
            "status": "blocked" if body_view.blocker_count else "planned",
            "blocker_count": body_view.blocker_count,
        },
    })


def _collect_blockers(
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
    reference_repair: JsonObject | None = None,
) -> list[JsonObject]:
    repair_view = _ReferenceRepairView.from_payload(reference_repair) if reference_repair is not None else None
    body_view = _BodyLinkerView.from_payload(body_linker)
    blocker_list = [_json_object(item) for item in body_view.blockers if isinstance(item, dict)]
    if repair_view is not None and repair_view.blocking_action_count:
        blocker_list.append(
            _json_object({
                "code": "reference_repair_blocked",
                "message": "Há WikiLinks ausentes/ambíguos ou alvos estruturais que exigem reparo antes do apply.",
                "blocking_action_count": repair_view.blocking_action_count,
                "human_decision_count": repair_view.human_decision_count,
            })
        )
    if related_notes is not None and related_notes_sync_blocked(related_notes):
        blocker_list.append(
            _json_object({
                "code": related_notes.blocked_reason or "related_notes_blocked",
                "message": related_notes.next_action,
            })
        )
    return blocker_list


def _diagnosis_next_action(
    *,
    blockers: list[JsonObject],
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
) -> str:
    if related_notes is not None and related_notes_sync_blocked(related_notes):
        return related_notes.next_action or "Corrigir o export Related Notes antes de aplicar."
    if blockers:
        body_view = _BodyLinkerView.from_payload(body_linker)
        return body_view.next_action or "Rodar /mednotes:fix-wiki --dry-run para resolver blockers semânticos antes de aplicar."
    return "Aplicar com run-linker --apply --diagnosis <link-diagnosis.json>."


def _diagnosis_required_inputs(*, related_notes: LinkRelatedSyncResult | None) -> list[str]:
    if related_notes is not None and related_notes_sync_blocked(related_notes):
        return _related_notes_required_inputs(related_notes)
    return LINK_REQUIRED_INPUTS


def _curator_batch_next_action(plan_path: Path) -> str:
    return (
        "/mednotes:link deve continuar a curadoria do grafo: lançar um "
        f"med-link-graph-curator por work_items[] em {plan_path}, escrever um "
        "vocabulary-curator-batch-output-manifest.v1, rodar "
        f"eval-curator-batch --plan {plan_path} --outputs <manifest.json> "
        "--report <curator-prompt-eval.json> --json, validar com "
        f"apply-curator-batch --plan {plan_path} --outputs <manifest.json> "
        "--validate-only --json e aplicar com --prompt-eval antes de repetir "
        "run-linker --diagnose."
    )


def _link_vocabulary_curator_batch(
    config: MedConfig,
    *,
    diagnosis_path: Path,
    body_linker: JsonObject,
) -> tuple[JsonObject, str, str]:
    body_view = _BodyLinkerView.from_payload(body_linker)
    if body_view.blocked_reason != "vocabulary_semantic_ingestion_pending":
        return {}, "", ""
    if config.vocabulary_db_path is None:
        return {}, "", ""
    run_dir = diagnosis_path.parent
    plan_path = run_dir / "vocabulary-curator-batch-plan.json"
    plan = build_vocabulary_curator_batch_plan(
        db_path=config.vocabulary_db_path,
        batch_id=f"link:{diagnosis_path.stem}",
        output_dir=run_dir / "vocabulary-curator-outputs",
    )
    atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return _json_object(plan), str(plan_path), _curator_batch_next_action(plan_path)


def _dedupe_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        normalized = normalize_key(text)
        if not text or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
    return result


def _sync_vocabulary_notes_from_wiki(config: MedConfig) -> None:
    if config.vocabulary_db_path is None:
        return
    initialize_vocabulary_db(config.vocabulary_db_path)
    with sqlite3.connect(config.vocabulary_db_path) as conn:
        for path in iter_notes(config.wiki_dir) if config.wiki_dir.exists() else []:
            text = path.read_text(encoding="utf-8")
            if _is_index_note(path, text):
                continue
            upsert_note(conn, path=path, title=infer_title(text, path), content_hash=note_content_hash(path))


def _yaml_aliases_by_note_id(conn: sqlite3.Connection) -> dict[int, list[str]]:
    aliases: dict[int, list[str]] = {}
    rows = conn.execute(
        """
        SELECT note_id, alias_text
        FROM yaml_alias_claims
        WHERE visible_in_yaml = 1
          AND claim_status != 'conflicting_alias'
        ORDER BY note_id, normalized_surface
        """
    ).fetchall()
    for note_id, alias_text in rows:
        aliases.setdefault(int(note_id), []).append(str(alias_text))
    return aliases


def _baseline_semantic_ingestion_items(config: MedConfig) -> list[JsonObject]:
    if config.vocabulary_db_path is None or not config.vocabulary_db_path.exists():
        return []
    _sync_vocabulary_notes_from_wiki(config)
    with sqlite3.connect(config.vocabulary_db_path) as conn:
        conn.row_factory = sqlite3.Row
        yaml_aliases = _yaml_aliases_by_note_id(conn)
        rows = conn.execute(
            """
            SELECT id, path, title, content_hash
            FROM notes
            WHERE status = 'active'
            ORDER BY path
            """
        ).fetchall()
    items: list[JsonObject] = []
    for row in rows:
        note_path = Path(str(row["path"]))
        if not note_path.is_file():
            continue
        text = note_path.read_text(encoding="utf-8")
        title = infer_title(text, note_path)
        title_norm = normalize_key(title)
        aliases = _dedupe_texts([title, note_path.stem, *extract_aliases(text), *yaml_aliases.get(int(row["id"]), [])])
        item_aliases: list[JsonObject] = []
        for alias in aliases:
            alias_norm = normalize_key(alias)
            item_aliases.append(
                _json_object({
                    "text": alias,
                    "kind": "canonical_title" if alias_norm == title_norm else "alias",
                    "link_policy": "direct" if alias_norm == title_norm else "requires_context",
                    "visible_in_yaml": True,
                    "intrinsically_ambiguous": alias_norm != title_norm,
                    "source": "system",
                })
            )
        items.append(
            _json_object({
                "schema": "medical-notes-workbench.note-semantic-ingestion.v1",
                "workflow": "/mednotes:link",
                "phase": "vocabulary_curation",
                "agent": "med-link-graph-curator",
                "source_workflow": "/mednotes:link",
                "note_path": str(note_path),
                "content_hash": note_content_hash(note_path),
                "primary_meaning": {
                    "label": title,
                    "semantic_type": "medical_concept",
                    "atomic_status": "unknown",
                },
                "aliases": item_aliases,
                "deferred_work_items": [],
                "confidence": 0.72,
                "source": "system",
            })
        )
    return items


def _drop_unresolved_surfaces(db_path: Path) -> int:
    initialize_vocabulary_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id
            FROM surfaces s
            LEFT JOIN surface_meaning_policy p ON p.surface_id = s.id
            WHERE p.id IS NULL
            """
        ).fetchall()
        conn.executemany("DELETE FROM surfaces WHERE id = ?", [(int(row[0]),) for row in rows])
    return len(rows)


def _direct_ambiguous_surface_repair_needed(diagnosis: JsonObject) -> bool:
    issue_payload = _json_field(diagnosis, "issues")
    issues = issue_payload if isinstance(issue_payload, list) else []
    return any(
        isinstance(issue, dict) and issue.get("code") == "vocabulary_map.direct_policy_on_ambiguous_surface"
        for issue in issues
    )


def _human_vocabulary_issues_are_auto_contextualizable(diagnosis: JsonObject) -> bool:
    issue_payload = _json_field(diagnosis, "issues", [])
    issues = [_json_object(issue) for issue in issue_payload if isinstance(issue, dict)] if isinstance(issue_payload, list) else []
    human_issues = [
        issue
        for issue in issues
        if issue.get("severity") == "human_decision"
        or issue.get("code")
        in {
            "vocabulary_map.duplicate_meaning",
            "vocabulary_map.non_atomic_note",
            "vocabulary_map.conflicting_alias",
            "vocabulary_map.direct_policy_on_ambiguous_surface",
        }
    ]
    return bool(human_issues) and all(
        issue.get("code") == "vocabulary_map.direct_policy_on_ambiguous_surface"
        for issue in human_issues
    )


def _contextualize_direct_policies_for_ambiguous_surfaces(db_path: Path) -> int:
    initialize_vocabulary_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT p.id
            FROM surface_meaning_policy p
            JOIN surfaces s ON s.id = p.surface_id
            WHERE p.link_policy = 'direct'
              AND p.surface_id IN (
                SELECT s2.id
                FROM surfaces s2
                JOIN surface_meaning_policy p2 ON p2.surface_id = s2.id
                GROUP BY s2.id
                HAVING COUNT(DISTINCT p2.meaning_id) > 1
                   OR MAX(s2.intrinsically_ambiguous) = 1
              )
            ORDER BY p.id
            """
        ).fetchall()
        policy_ids = [(int(row[0]),) for row in rows]
        conn.executemany(
            """
            UPDATE surface_meaning_policy
            SET link_policy='requires_context', updated_at=CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            policy_ids,
        )
    return len(policy_ids)


def _vocabulary_repair_needed(diagnosis: JsonObject) -> bool:
    diagnosis_view = _VocabularyMapDiagnosisView.from_payload(diagnosis)
    if diagnosis_view.status == "blocked_human" and not _human_vocabulary_issues_are_auto_contextualizable(diagnosis):
        return False
    if diagnosis_view.pending_semantic_ingestion_count > 0:
        return True
    if _direct_ambiguous_surface_repair_needed(diagnosis):
        return True
    if any(
        issue.code == "vocabulary_map.unresolved_surfaces_without_meanings"
        for issue in diagnosis_view.issues
    ):
        return True
    return diagnosis_view.note_count > 0 and diagnosis_view.meaning_count == 0


def _registered_blocker_requires_human(code: str, *, fallback: bool) -> bool:
    try:
        return blocker_entry(code).requires_human_packet
    except Exception:
        return fallback


def _attach_registered_decision_summary(
    item: JsonObject,
    *,
    phase: str,
    fallback_code: str,
) -> JsonObject:
    issue = _VocabularyMapIssueView.model_validate(item)
    if issue.decision_summary is not None:
        return item
    code = issue.code or fallback_code
    try:
        decision = decision_for_code(
            code,
            phase=phase,
            public_summary=issue.message or "Bloqueio recuperavel no vocabulario.",
            developer_summary=issue.message or code,
            next_action=issue.next_action or "Continuar pelo fluxo oficial do workflow.",
        )
    except Exception:
        return item
    enriched = _json_object(item)
    enriched["decision_summary"] = decision.decision_summary()
    return _json_object(enriched)


def _diagnosis_requests_vocabulary_repair(diagnosis: JsonObject) -> bool:
    body = _json_field(diagnosis, "body_term_linker")
    body_reason = _BodyLinkerView.from_payload(body).blocked_reason
    if body_reason in {"vocabulary_semantic_ingestion_pending", "vocabulary_map_blocked"}:
        return True
    vocabulary_map = _json_field(diagnosis, "vocabulary_map_diagnosis")
    if isinstance(vocabulary_map, dict) and _vocabulary_repair_needed(_json_object(vocabulary_map)):
        return True
    blocker_payload = _json_field(diagnosis, "blockers")
    blockers = blocker_payload if isinstance(blocker_payload, list) else []
    return any(
        _json_text(_json_object(blocker), "code")
        in {"vocabulary_semantic_ingestion_pending", "vocabulary_map.unresolved_surfaces_without_meanings"}
        if isinstance(blocker, dict)
        else False
        for blocker in blockers
    )


def repair_vocabulary_semantics_for_link(
    config: MedConfig,
    *,
    run_dir: Path | None = None,
    trigger: str = "link_apply",
) -> JsonObject:
    if config.vocabulary_db_path is None:
        return _json_object({
            "schema": "medical-notes-workbench.vocabulary-semantic-repair.v1",
            "status": "skipped",
            "skipped_reason": "vocabulary_db_unconfigured",
            "applied_count": 0,
            "blocked_count": 0,
        })
    db_path = config.vocabulary_db_path
    if not db_path.exists():
        return _json_object({
            "schema": "medical-notes-workbench.vocabulary-semantic-repair.v1",
            "status": "skipped",
            "skipped_reason": "vocabulary_db_missing",
            "db_path": str(db_path),
            "applied_count": 0,
            "blocked_count": 0,
        })
    before = _json_object(load_vocabulary_map_diagnosis(db_path).as_diagnosis_dict())
    before_view = _VocabularyMapDiagnosisView.from_payload(before)
    if not _vocabulary_repair_needed(before):
        return _json_object({
            "schema": "medical-notes-workbench.vocabulary-semantic-repair.v1",
            "status": "skipped",
            "skipped_reason": "vocabulary_already_ready"
            if before_view.status == "ready"
            else "human_decision_required",
            "trigger": trigger,
            "db_path": str(db_path),
            "diagnosis_before": before,
            "applied_count": 0,
            "blocked_count": 0,
        })
    items = _baseline_semantic_ingestion_items(config)
    receipts: list[JsonObject] = []
    applied_count = 0
    blocked_count = 0
    with sqlite3.connect(db_path) as conn:
        for item in items:
            try:
                receipt = apply_semantic_ingestion(db_path=db_path, item=item, conn=conn)
            except ValidationError as exc:
                item_view = _SemanticIngestionItemView.from_payload(item)
                receipt = _json_object({
                    "schema": "medical-notes-workbench.note-semantic-ingestion-apply-receipt.v1",
                    "status": "blocked",
                    "blocked_reason": "semantic_ingestion.validation_error",
                    "error": str(exc),
                    "note_path": item_view.note_path,
                    "content_hash": item_view.content_hash,
                })
            receipt = _json_object(receipt)
            if _json_field(receipt, "status") == "applied":
                applied_count += 1
            else:
                blocked_count += 1
            receipts.append(receipt)
    dropped_orphan_surface_count = _drop_unresolved_surfaces(db_path)
    contextualized_direct_policy_count = _contextualize_direct_policies_for_ambiguous_surfaces(db_path)
    after = _json_object(load_vocabulary_map_diagnosis(db_path).as_diagnosis_dict())
    after_view = _VocabularyMapDiagnosisView.from_payload(after)
    status = "completed" if after_view.status == "ready" else "completed_with_blockers"
    payload = _json_object({
        "schema": "medical-notes-workbench.vocabulary-semantic-repair.v1",
        "phase": "vocabulary_semantic_repair",
        "status": status,
        "trigger": trigger,
        "db_path": str(db_path),
        "diagnosis_before": before,
        "diagnosis_after": after,
        "item_count": len(items),
        "applied_count": applied_count,
        "blocked_count": blocked_count,
        "dropped_orphan_surface_count": dropped_orphan_surface_count,
        "contextualized_direct_policy_count": contextualized_direct_policy_count,
        "receipts": receipts,
    })
    if status != "completed":
        payload = _json_object(
            {
                **payload,
                "blocked_reason": _json_text(after, "status", "vocabulary_semantic_repair_blocked"),
                "next_action": "Resolver decisões humanas ou erros de ingestão restantes pelo workflow /mednotes:link.",
            }
        )
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = run_dir / "vocabulary-semantic-repair-receipt.json"
        atomic_write_text(receipt_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        payload["receipt_path"] = str(receipt_path)
    return _json_object(payload)


def _body_only_fallback(
    *,
    path: Path,
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
    reference_repair: JsonObject,
) -> JsonObject | None:
    if related_notes is None or not related_notes_sync_blocked(related_notes):
        return None
    body_view = _BodyLinkerView.from_payload(body_linker)
    repair_view = _ReferenceRepairView.from_payload(reference_repair)
    body_blocked = bool(body_view.error or body_view.parse_error or body_view.blocker_count)
    reference_blocked = bool(repair_view.blocking_action_count)
    blocked_phases: list[str] = []
    if body_blocked:
        blocked_phases.append("body_term_linker")
    if reference_blocked:
        blocked_phases.append("reference_repair")
    recovery_command = wiki_cli_command("related-notes-sync", "--recover-export", "--mode", "auto", "--json")
    if blocked_phases:
        return _json_object({
            "safe": False,
            "command": "",
            "diagnosis_path": str(path),
            "allowed_scope": [],
            "excluded_scope": ["related_notes_sync"],
            "blocked_phases": blocked_phases,
            "expected_changed_counts": {
                "modified": 0,
                "deleted": 0,
                "created": 0,
                "links_planned": body_view.links_planned,
                "reference_actions": repair_view.action_count,
            },
            "reason": "Related Notes está bloqueado e pelo menos uma fase body/reference também não está segura.",
            "next_action": recovery_command,
        })
    return _json_object({
        "safe": True,
        "command": "/mednotes:link-body",
        "cli_command": wiki_cli_command("run-linker", "--apply", "--no-related-notes", "--diagnosis", str(path), "--json"),
        "diagnosis_path": str(path),
        "allowed_scope": ["reference_repair", "body_term_linker"],
        "excluded_scope": ["related_notes_sync"],
        "blocked_phases": [],
        "expected_changed_counts": {
            "modified": max(
                body_view.files_changed,
                repair_view.affected_note_count,
            ),
            "deleted": 0,
            "created": 0,
            "links_planned": body_view.links_planned,
            "reference_actions": repair_view.action_count,
        },
        "reason": "Related Notes export está stale, mas as fases body/reference não dependem do export.",
        "next_action": "/mednotes:link-body",
    })


def _skipped_image_only_diagnosis(
    config: MedConfig,
    *,
    path: Path,
    trigger_context: JsonObject,
    git_context: LinkGitContext | None = None,
) -> JsonObject:
    snapshot = _collect_snapshot(config)
    git_context = git_context or _git_context_for(config)
    git_payload = git_context.to_payload()
    git_view = _GitContextView.from_payload(git_payload)
    phases = JsonObjectAdapter.validate_python({
        phase: {"status": "skipped"}
        for phase in LINK_PHASE_ORDER
    })
    plan_payload = JsonObjectAdapter.validate_python({"phase_order": list(LINK_PHASE_ORDER), "phases": phases})
    payload = _json_object({
        "schema": LINK_DIAGNOSIS_SCHEMA,
        "generated_at": _now_iso(),
        "phase": "link_diagnosis",
        "status": "skipped",
        "blocked_reason": "",
        "skipped_reason": "image_only_changes",
        "next_action": "",
        "required_inputs": LINK_REQUIRED_INPUTS,
        "human_decision_required": False,
        "diagnosis_path": str(path),
        "wiki_dir": str(config.wiki_dir),
        "catalog_path": str(config.catalog_path) if config.catalog_path else None,
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "trigger_context": trigger_context,
        "triggers_detected": derive_triggers(trigger_context),
        "affected_notes": affected_notes_from_context(trigger_context),
        "git": git_payload,
        "git_status_hash": git_view.status_hash,
        "snapshot": snapshot,
        "snapshot_hash": snapshot["snapshot_hash"],
        "plan": plan_payload,
        "plan_hash": "sha256:" + canonical_json_hash(plan_payload),
        "phases": phases,
        "reference_repair": {
            "schema": "medical-notes-workbench.reference-repair-plan.v1",
            "phase": "reference_repair",
            "status": "skipped",
            "package_mode": "diagnosis_bound",
            "manual_script_allowed": False,
            "requires_backup": False,
            "requires_receipt": True,
            "action_count": 0,
            "affected_note_count": 0,
            "blocking_action_count": 0,
            "human_decision_count": 0,
            "triage_count": 0,
            "human_decision_required": False,
            "triage_required": False,
            "note_actions": [],
            "structural_actions": [],
            "catalog_actions": [],
            "human_decision_packets": [],
        },
        "human_decision_packets": [],
        "links_planned": 0,
        "links_rewritten": 0,
        "blocker_count": 0,
        "blockers": [],
        "body_term_linker": None,
        "related_notes_sync": None,
        "related_notes_skipped_reason": "",
        "returncode": 0,
    })
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def _run_body_linker(
    config: MedConfig,
    *,
    dry_run: bool,
    llm_disambiguation: str = "off",
    llm_model: str | None = None,
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    llm_disambiguator: Callable[..., object] | None = None,
) -> JsonObject:
    if config.vocabulary_db_path is None:
        graph_before = graph_audit(config) if config.wiki_dir.exists() else {}
        return _json_object({
            "ok": False,
            "blocked": True,
            "dry_run": dry_run,
            "phase": "run_linker_dry_run" if dry_run else "run_linker_apply",
            "status": "blocked",
            "blocked_reason": "vocabulary_db_unconfigured",
            "next_action": "Configure vocabulary_db_path e rode /mednotes:fix-wiki --apply para instanciar o DB.",
            "required_inputs": ["vocabulary_db_path"],
            "human_decision_required": False,
            "body_linker_mode": "vocabulary_db",
            "body_linker_skipped_reason": "vocabulary_db_unconfigured",
            "vocabulary_db_path": "",
            "returncode": 3,
            "files_scanned": 0,
            "files_changed": 0,
            "links_planned": 0,
            "links_rewritten": 0,
            "blocker_count": 1,
            "blockers": [
                {
                    "code": "vocabulary_db_unconfigured",
                    "message": "O linker atual exige vocabulary DB configurado antes de aplicar body links.",
                }
            ],
            "graph_audit_before": graph_before,
            "plans": [],
        })
    if config.vocabulary_db_path and config.vocabulary_db_path.exists():
        graph_before = graph_audit(config)
        vocabulary_map_diagnosis = load_vocabulary_map_diagnosis(config.vocabulary_db_path).as_diagnosis_dict()
        vocabulary_view = _VocabularyMapDiagnosisView.from_payload(vocabulary_map_diagnosis)
        vocabulary_status = vocabulary_view.status
        if vocabulary_status in {"blocked_pending", "blocked_human"}:
            pending_count = vocabulary_view.pending_semantic_ingestion_count
            blocked_reason = "vocabulary_semantic_ingestion_pending" if pending_count else "vocabulary_map_blocked"
            blockers: list[JsonObject] = []
            for issue in vocabulary_view.issues:
                item = _json_object({
                    "code": issue.code or blocked_reason,
                    "message": issue.message,
                    "next_action": issue.next_action,
                    "required_inputs": issue.required_inputs,
                })
                if issue.decision_summary is not None:
                    item = _json_object({**item, "decision_summary": issue.decision_summary})
                blockers.append(
                    _attach_registered_decision_summary(
                        item,
                        phase="link_diagnosis",
                        fallback_code=blocked_reason,
                    )
                )
            if not blockers:
                blockers = [
                    _attach_registered_decision_summary(
                        _json_object({
                            "code": blocked_reason,
                            "message": "Vocabulary DB is not ready for body linker.",
                        }),
                        phase="link_diagnosis",
                        fallback_code=blocked_reason,
                    )
                ]
            human_decision_required = any(
                _registered_blocker_requires_human(
                    _json_text(item, "code", blocked_reason),
                    fallback=vocabulary_status == "blocked_human",
                )
                for item in blockers
            )
            return _json_object({
                "ok": False,
                "blocked": True,
                "dry_run": dry_run,
                "phase": "run_linker_dry_run" if dry_run else "run_linker_apply",
                "status": "blocked",
                "blocked_reason": blocked_reason,
                "next_action": (
                    "Continuar a curadoria semântica dentro de /mednotes:link antes de linkar o corpo."
                    if blocked_reason == "vocabulary_semantic_ingestion_pending"
                    else "Reconciliar o vocabulary DB pelo fluxo oficial de /mednotes:link antes de projetar aliases ou linkar o corpo."
                ),
                "required_inputs": ["vocabulary_semantic_ingestion"] if pending_count else ["vocabulary_recovery"],
                "human_decision_required": human_decision_required,
                "body_linker_mode": "vocabulary_db",
                "body_linker_skipped_reason": blocked_reason,
                "vocabulary_db_path": str(config.vocabulary_db_path),
                "vocabulary_map_diagnosis": vocabulary_map_diagnosis,
                "pending_semantic_ingestion_count": pending_count,
                "returncode": 3,
                "files_scanned": 0,
                "files_changed": 0,
                "links_planned": 0,
                "links_rewritten": 0,
                "blocker_count": len(blockers),
                "blockers": blockers,
                "graph_audit_before": graph_before,
                "plans": [],
            })
        payload = _json_object(run_db_body_linker(
            wiki_dir=config.wiki_dir,
            db_path=config.vocabulary_db_path,
            dry_run=dry_run,
            llm_mode=llm_disambiguation if dry_run else "off",
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            llm_disambiguator=llm_disambiguator,
        ))
        payload["returncode"] = 3 if _json_field(payload, "blocked") else 0
        payload["graph_audit_before"] = graph_before
        payload["vocabulary_map_diagnosis"] = vocabulary_map_diagnosis
        return _json_object(payload)

    return _body_linker_blocked_for_vocabulary_bootstrap(
        config,
        dry_run=dry_run,
            vocabulary_bootstrap=_json_object(planned_vocabulary_bootstrap(config)),
        )


def _body_linker_blocked_for_vocabulary_bootstrap(
    config: MedConfig,
    *,
    dry_run: bool,
    vocabulary_bootstrap: JsonObject,
) -> JsonObject:
    graph_before = graph_audit(config) if config.wiki_dir.exists() else {}
    bootstrap_view = _VocabularyBootstrapView.from_payload(vocabulary_bootstrap)
    if bootstrap_view.note_count == 0:
        return _json_object({
            "ok": True,
            "blocked": False,
            "dry_run": dry_run,
            "phase": "run_linker_dry_run" if dry_run else "run_linker_apply",
            "status": "skipped",
            "blocked_reason": "",
            "skipped_reason": "vocabulary_bootstrap_empty_wiki",
            "next_action": "",
            "required_inputs": [],
            "human_decision_required": False,
            "body_linker_mode": "vocabulary_db",
            "body_linker_skipped_reason": "vocabulary_bootstrap_empty_wiki",
            "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
            "vocabulary_bootstrap": vocabulary_bootstrap,
            "returncode": 0,
            "files_scanned": 0,
            "files_changed": 0,
            "links_planned": 0,
            "links_rewritten": 0,
            "blocker_count": 0,
            "blockers": [],
            "graph_audit_before": graph_before,
            "plans": [],
        })
    return _json_object({
        "ok": False,
        "blocked": True,
        "dry_run": dry_run,
        "phase": "run_linker_dry_run" if dry_run else "run_linker_apply",
        "status": "blocked",
        "blocked_reason": "vocabulary_bootstrap_required",
        "next_action": (
            "Resolver pendências do linker/grafo: instanciar o vocabulary DB via workflow apply, "
            "processar a fila com med-link-graph-curator e repetir o diagnóstico de links."
        ),
        "required_inputs": ["vocabulary_bootstrap", "vocabulary_semantic_ingestion"],
        "human_decision_required": False,
        "body_linker_mode": "vocabulary_db",
        "body_linker_skipped_reason": "vocabulary_bootstrap_required",
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "vocabulary_bootstrap": vocabulary_bootstrap,
        "returncode": 3,
        "files_scanned": 0,
        "files_changed": 0,
        "links_planned": 0,
        "links_rewritten": 0,
        "blocker_count": 1,
        "blockers": [
            {
                "code": "vocabulary_bootstrap_required",
                "message": "O DB de vocabulário ainda não existe; diagnóstico não instancia nem limpa notas.",
            }
        ],
        "graph_audit_before": graph_before,
        "plans": [],
    })


def diagnose_links(
    config: MedConfig,
    *,
    diagnosis_path: Path | None = None,
    include_related_notes: bool = True,
    force_diagnose: bool = False,
    trigger_context: JsonObject | None = None,
    llm_disambiguation: str = "auto",
    llm_model: str | None = None,
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    llm_disambiguator: Callable[..., object] | None = None,
) -> JsonObject:
    path = diagnosis_path or default_link_diagnosis_path()
    link_state = load_link_state()
    git_context = _git_context_for(config, link_state)
    git_payload = git_context.to_payload()
    if not config.wiki_dir.exists():
        message = f"Wiki dir não encontrado: {config.wiki_dir}"
        git_view = _GitContextView.from_payload(git_payload)
        payload = _json_object({
            "schema": LINK_DIAGNOSIS_SCHEMA,
            "generated_at": _now_iso(),
            "phase": "link_diagnosis",
            "status": "failed",
            "blocked_reason": "linker_error",
            "next_action": "Corrigir --wiki-dir ou [paths].wiki_dir e rodar o diagnóstico novamente.",
            "required_inputs": LINK_REQUIRED_INPUTS,
            "human_decision_required": False,
            "diagnosis_path": str(path),
            "wiki_dir": str(config.wiki_dir),
            "catalog_path": str(config.catalog_path) if config.catalog_path else None,
            "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
            "git": git_payload,
            "git_status_hash": git_view.status_hash,
            "error": message,
            "returncode": 4,
        })
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload
    if trigger_context is not None and is_image_only_context(trigger_context):
        return _skipped_image_only_diagnosis(config, path=path, trigger_context=trigger_context, git_context=git_context)
    vocabulary_bootstrap = _json_object(planned_vocabulary_bootstrap(config))
    trigger_snapshot = _collect_snapshot(config)
    effective_trigger_context = trigger_context or _git_trigger_context_for(git_context, snapshot=trigger_snapshot, link_state=link_state)
    identity = build_diagnosis_identity(
        snapshot=trigger_snapshot,
        git_context=git_payload,
        trigger_context=effective_trigger_context,
        include_related_notes=include_related_notes,
        llm_disambiguation=llm_disambiguation,
        llm_model=llm_model or DEFAULT_LLM_DISAMBIGUATION_MODEL,
        llm_timeout=llm_timeout,
    )
    if not force_diagnose:
        redundant = redundant_diagnosis_payload(link_state.to_payload() if link_state is not None else {}, identity)
        if redundant is not None:
            return redundant
    bootstrap_view = _VocabularyBootstrapView.from_payload(vocabulary_bootstrap)
    body_linker = (
        _body_linker_blocked_for_vocabulary_bootstrap(
            config,
            dry_run=True,
            vocabulary_bootstrap=vocabulary_bootstrap,
        )
        if bootstrap_view.status == "planned"
        else _run_body_linker(
            config,
            dry_run=True,
            llm_disambiguation=llm_disambiguation,
            llm_model=llm_model or DEFAULT_LLM_DISAMBIGUATION_MODEL,
            llm_timeout=llm_timeout,
            llm_disambiguator=llm_disambiguator,
        )
    )
    vocabulary_curator_batch_plan, vocabulary_curator_batch_plan_path, vocabulary_curator_next_action = (
        _link_vocabulary_curator_batch(
            config,
            diagnosis_path=path,
            body_linker=body_linker,
        )
    )
    if vocabulary_curator_next_action:
        body_linker = _json_object({**body_linker, "next_action": vocabulary_curator_next_action})
    related_notes = run_related_notes_sync(config, apply=False, backup=False) if include_related_notes else None
    related_notes_payload = _related_notes_payload(related_notes)
    body_view = _BodyLinkerView.from_payload(body_linker)
    contextual_diagnosis = body_view.contextual_alias_disambiguation
    # The body linker can persist contextual LLM decisions into the vocabulary
    # DB during diagnosis. Persist the post-diagnosis snapshot only when that
    # happened so ordinary diagnosis still reuses the trigger snapshot.
    snapshot = _collect_snapshot(config) if contextual_diagnosis.decision_count else trigger_snapshot
    reference_repair = _json_object(plan_reference_repair(
        _graph_issues_from(body_linker),
        structural_events=structural_events_from_context(effective_trigger_context),
    ))
    repair_view = _ReferenceRepairView.from_payload(reference_repair)
    blockers = _collect_blockers(body_linker, related_notes, reference_repair)
    phases = _diagnosis_phases(body_linker, related_notes, reference_repair)
    human_decision_packets = list(repair_view.human_decision_packets)
    failed = bool(body_view.error or body_view.parse_error)
    status = "failed" if failed else "blocked" if blockers else "diagnosis_ready"
    blocked_reason = (
        "linker_error"
        if failed
        else "link_plan_blocked"
        if blockers
        else ""
    )
    plan = _json_object({
        "phase_order": list(LINK_PHASE_ORDER),
        "phases": phases,
        "reference_repair": reference_repair,
        "body_term_linker": body_linker,
        "related_notes_sync": related_notes_payload,
        "llm_disambiguation": {
            "mode": llm_disambiguation,
            "model": llm_model or DEFAULT_LLM_DISAMBIGUATION_MODEL,
            "timeout_seconds": llm_timeout,
        },
    })
    if vocabulary_curator_batch_plan:
        plan["vocabulary_curation"] = {
            "status": _json_text(vocabulary_curator_batch_plan, "status"),
            "item_count": _json_int(vocabulary_curator_batch_plan, "item_count"),
            "plan_path": vocabulary_curator_batch_plan_path,
        }
    plan_hash = "sha256:" + canonical_json_hash(plan)
    payload = _json_object({
        "schema": LINK_DIAGNOSIS_SCHEMA,
        "generated_at": _now_iso(),
        "phase": "link_diagnosis",
        "status": status,
        "blocked_reason": blocked_reason,
        "next_action": _diagnosis_next_action(blockers=blockers, body_linker=body_linker, related_notes=related_notes),
        "required_inputs": _diagnosis_required_inputs(related_notes=related_notes),
        "human_decision_required": bool(human_decision_packets),
        "diagnosis_path": str(path),
        "wiki_dir": str(config.wiki_dir),
        "catalog_path": str(config.catalog_path) if config.catalog_path else None,
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "vocabulary_bootstrap": vocabulary_bootstrap,
        "vocabulary_map_diagnosis": body_view.vocabulary_map_diagnosis.to_payload(),
        "vocabulary_curator_batch_plan": vocabulary_curator_batch_plan,
        "vocabulary_curator_batch_plan_path": vocabulary_curator_batch_plan_path,
        "vocabulary_curator_next_action": vocabulary_curator_next_action,
        "trigger_context": effective_trigger_context,
        "triggers_detected": derive_triggers(effective_trigger_context),
        "affected_notes": affected_notes_from_context(effective_trigger_context),
        "git": git_payload,
        "git_status_hash": _GitContextView.from_payload(git_payload).status_hash,
        "snapshot": snapshot,
        "snapshot_hash": snapshot["snapshot_hash"],
        "plan": plan,
        "plan_hash": plan_hash,
        "phases": phases,
        "reference_repair": reference_repair,
        "human_decision_packets": human_decision_packets,
        "links_planned": body_view.links_planned,
        "links_rewritten": body_view.links_rewritten,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "body_term_linker": body_linker,
        "contextual_alias_disambiguation": body_view.contextual_alias_disambiguation.to_payload(),
        "related_notes_sync": related_notes_payload,
        "related_notes_skipped_reason": related_notes.skipped_reason if related_notes is not None else "",
        "body_only_fallback": _body_only_fallback(
            path=path,
            body_linker=body_linker,
            related_notes=related_notes,
            reference_repair=reference_repair,
        ),
        "agent_events": [force_diagnose_event(diagnosis_path=path)] if force_diagnose else [],
        "returncode": body_view.returncode,
    })
    if body_view.error:
        payload["error"] = body_view.error
    if body_view.parse_error:
        payload["parse_error"] = body_view.parse_error
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    record_diagnosis_attempt(payload, identity=identity)
    return _json_object(payload)


def _load_diagnosis(path: Path) -> JsonObject:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Diagnóstico de links não encontrado: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Diagnóstico de links inválido: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != LINK_DIAGNOSIS_SCHEMA:
        raise ValidationError(f"Diagnóstico de links precisa usar schema {LINK_DIAGNOSIS_SCHEMA}.")
    return _json_object(data)


def _changed_files_from(*payloads: JsonObject | None) -> list[str]:
    changed: set[str] = set()
    for payload in payloads:
        if not payload:
            continue
        plans = _json_field(payload, "plans")
        if isinstance(plans, list):
            for plan in plans:
                plan_view = _LinkBodyPlanView.from_payload(plan)
                if plan_view.changed and plan_view.file:
                    changed.add(plan_view.file)
        updates = _json_field(payload, "updates")
        if isinstance(updates, list):
            for update in updates:
                if not isinstance(update, dict) or update.get("changed") is False:
                    continue
                file_path = update.get("file") or update.get("path")
                if isinstance(file_path, str):
                    changed.add(file_path)
        changed_files = _json_field(payload, "changed_files")
        if isinstance(changed_files, list):
            for item in changed_files:
                if isinstance(item, str):
                    changed.add(item)
    return sorted(changed)


def _snapshot_note_hashes(snapshot: JsonObject) -> dict[str, str]:
    notes = snapshot.get("notes")
    if not isinstance(notes, list):
        return {}
    hashes: dict[str, str] = {}
    for note in notes:
        note_view = _SnapshotNoteView.from_payload(note)
        if note_view.path:
            hashes[note_view.path] = note_view.content_hash
    return hashes


def _relative_receipt_path(config: MedConfig, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(config.wiki_dir.resolve()).as_posix()
        except (OSError, ValueError):
            return path.as_posix()
    return path.as_posix()


def _safe_link_action(action: JsonObject, *, phase: str) -> JsonObject:
    allowed = (
        "action",
        "target",
        "old_target",
        "new_target",
        "replacement",
        "receipt_code",
        "line",
        "term",
        "matched_text",
        "display_text",
        "start",
        "end",
        "source",
        "occurrence_id",
        "context_hash",
        "confidence",
        "reason_code",
    )
    clean: JsonObject = {"phase": phase}
    for key in allowed:
        value = action.get(key)
        if value not in (None, ""):
            clean[key] = value
    return _json_object(clean)


def _phase_file_actions(
    config: MedConfig,
    *,
    reference_apply: JsonObject | None,
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
) -> dict[str, JsonObject]:
    by_path: dict[str, JsonObject] = {}

    def entry(path_value: str) -> JsonObject:
        rel = _relative_receipt_path(config, path_value)
        return by_path.setdefault(rel, {"path": rel, "phases": [], "actions": [], "backup_paths": []})

    reference_apply_view = _ReferenceApplyView.from_payload(reference_apply)
    for report in reference_apply_view.reports:
        report_payload = _json_object(report)
        report_path = _json_field(report_payload, "path")
        if not _json_field(report_payload, "changed") or not isinstance(report_path, str):
            continue
        item = entry(report_path)
        item["phases"].append("reference_repair")
        backup_path = _json_text(report_payload, "backup_path")
        if backup_path:
            item["backup_paths"].append(backup_path)
        actions = _json_field(report_payload, "actions")
        if isinstance(actions, list):
            item["actions"].extend(
                _safe_link_action(action, phase="reference_repair")
                for action in actions
                if isinstance(action, dict)
            )

    body_view = _BodyLinkerView.from_payload(body_linker)
    for plan_view in body_view.plans:
        if not plan_view.changed or not plan_view.file:
            continue
        item = entry(plan_view.file)
        item["phases"].append("body_term_linker")
        for insertion in plan_view.insertions:
            if isinstance(insertion, dict):
                item["actions"].append(
                    _json_object({
                        "phase": "body_term_linker",
                        "action": "insert_body_wikilink",
                        **_safe_link_action(insertion, phase="body_term_linker"),
                    })
                )
        for rewrite in plan_view.rewrites:
            if isinstance(rewrite, dict):
                item["actions"].append(
                    _json_object({
                        "phase": "body_term_linker",
                        "action": "rewrite_body_wikilink",
                        **_safe_link_action(rewrite, phase="body_term_linker"),
                    })
                )

    if related_notes is not None:
        for update_model in related_notes.updates:
            path_value = update_model.relative_path or update_model.path or update_model.file
            if not path_value:
                continue
            item = entry(path_value)
            item["phases"].append("related_notes_sync")
            if update_model.backup_path:
                item["backup_paths"].append(update_model.backup_path)
            item["actions"].append(
                _json_object({
                    "phase": "related_notes_sync",
                    "action": "rewrite_related_notes_section",
                    "cleared_link_count": update_model.cleared_link_count,
                    "proposed_link_count": len(update_model.proposed_links),
                })
            )
    for value in by_path.values():
        value["phases"] = sorted(set(value["phases"]))
        value["backup_paths"] = sorted(set(value["backup_paths"]))
    return by_path


def _file_changes(
    *,
    config: MedConfig,
    before_snapshot: JsonObject,
    after_snapshot: JsonObject,
    reference_apply: JsonObject | None,
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
) -> list[JsonObject]:
    before_hashes = _snapshot_note_hashes(before_snapshot)
    after_hashes = _snapshot_note_hashes(after_snapshot)
    action_map = _phase_file_actions(config, reference_apply=reference_apply, body_linker=body_linker, related_notes=related_notes)
    changed_paths = set(action_map)
    for raw_path in _changed_files_from(reference_apply, body_linker, _related_notes_payload(related_notes)):
        changed_paths.add(_relative_receipt_path(config, raw_path))
    changes: list[JsonObject] = []
    for rel in sorted(path for path in changed_paths if path):
        detail = action_map.get(rel, {"path": rel, "phases": [], "actions": [], "backup_paths": []})
        changes.append(
                _json_object({
                    "path": rel,
                "phases": detail.get("phases", []),
                "before_hash": before_hashes.get(rel, ""),
                "after_hash": after_hashes.get(rel, ""),
                "actions": detail.get("actions", []),
                    "backup_paths": detail.get("backup_paths", []),
                })
            )
    return changes


def _trigger_context_summary(diagnosis: JsonObject) -> JsonObject:
    context = _json_object_or_empty(_json_field(diagnosis, "trigger_context"))
    source = _json_field(context, "source_workflow", "manual")
    return _json_object({
        "source_workflow": source or "manual",
        "triggers_detected": _json_field(diagnosis, "triggers_detected", []),
        "affected_notes": _json_field(diagnosis, "affected_notes", []),
    })


def _receipt_skips(diagnosis: JsonObject, related_notes: LinkRelatedSyncResult | None) -> list[JsonObject]:
    skips: list[JsonObject] = []
    phases = _json_field(diagnosis, "phases")
    for phase, details in phases.items() if isinstance(phases, dict) else []:
        if isinstance(details, dict) and details.get("status") == "skipped":
            skips.append(_json_object({"phase": phase, "reason": details.get("skipped_reason") or "skipped"}))
    if related_notes is not None and related_notes.skipped_reason:
        skips.append(_json_object({"phase": "related_notes_sync", "reason": related_notes.skipped_reason}))
    body_view = _BodyLinkerView.from_payload(_json_field(diagnosis, "body_term_linker"))
    for plan_view in body_view.plans:
        for item in plan_view.skipped:
            skip_view = _LinkPlanSkipView.from_payload(item)
            skips.append(
                _json_object({
                    "phase": "contextual_alias_disambiguation",
                    "path": _relative_receipt_path_from_diagnosis(diagnosis, plan_view.file),
                    "occurrence_id": skip_view.occurrence_id,
                    "reason": skip_view.reason_code or skip_view.action or "contextual_alias_skipped",
                    "action": skip_view.action,
                })
            )
    return skips


def _link_apply_safety_preflight_block(
    *,
    diagnosis_path: Path,
    diagnosis: JsonObject,
    version_control_guard_active: bool,
    vocabulary_repair_requested: bool,
) -> JsonObject | None:
    """Stop mutating apply routes after stale checks and before resource writes."""

    if _diagnosis_has_mutating_guard_safety(diagnosis):
        return None
    planned_change_count = _diagnosis_planned_change_count(diagnosis)
    if planned_change_count <= 0 and not vocabulary_repair_requested:
        return None
    if version_control_guard_active:
        return None
    return _json_object({
        "schema": LINK_RUN_SCHEMA,
        "phase": "link_apply_preflight",
        "status": "blocked",
        "blocked_reason": "version_control_safety_evidence_missing",
        "next_action": (
            "Abrir o ponto de restauração do vault pela rota oficial, repetir a conferência se o diagnóstico "
            "ficar obsoleto e só então aplicar."
        ),
        "required_inputs": ["version_control_safety"],
        "human_decision_required": False,
        "diagnosis_path": str(diagnosis_path),
        "planned_changed_file_count": planned_change_count,
        "vocabulary_repair_requested": vocabulary_repair_requested,
        "changed_files": [],
        "files_changed": 0,
        "returncode": 3,
    })


def _diagnosis_has_mutating_guard_safety(diagnosis: JsonObject) -> bool:
    """Only real guard evidence can authorize a mutating apply attempt."""

    payload = _diagnosis_version_control_safety_payload(diagnosis)
    if not payload:
        return False
    return bool(
        _json_field(payload, "resource_guard_active")
        and _json_field(payload, "run_start_seen")
        and _json_field(payload, "run_finish_seen")
        and not _json_field(payload, "no_resource_mutation")
    )


def _diagnosis_version_control_safety_payload(diagnosis: JsonObject) -> JsonObject:
    direct = _json_object_or_empty(_json_field(diagnosis, "version_control_safety"))
    if direct:
        return direct
    receipt = _json_object_or_empty(_json_field(diagnosis, "receipt"))
    nested = _json_object_or_empty(_json_field(receipt, "version_control_safety"))
    if nested:
        return nested
    guard_receipt = _json_object_or_empty(_json_field(diagnosis, "guard_receipt"))
    return _json_object_or_empty(_json_field(guard_receipt, "version_control_safety"))


def _diagnosis_planned_change_count(diagnosis: JsonObject) -> int:
    changed_files = _json_field(diagnosis, "changed_files")
    return max(
        _strict_non_negative_int(_json_field(diagnosis, "files_changed")),
        len(changed_files) if isinstance(changed_files, list) else 0,
        _body_linker_planned_change_count(_json_object_or_empty(_json_field(diagnosis, "body_term_linker"))),
        _reference_repair_planned_change_count(_json_object_or_empty(_json_field(diagnosis, "reference_repair"))),
        _related_notes_planned_change_count(_json_object_or_empty(_json_field(diagnosis, "related_notes_sync"))),
    )


def _body_linker_planned_change_count(body_linker: JsonObject) -> int:
    plans = _json_field(body_linker, "plans")
    if isinstance(plans, list):
        return sum(1 for plan in plans if _json_field(_json_object_or_empty(plan), "changed") is True)
    return max(
        _strict_non_negative_int(_json_field(body_linker, "files_changed")),
        _strict_non_negative_int(_json_field(body_linker, "links_planned")),
        _strict_non_negative_int(_json_field(body_linker, "links_rewritten")),
    )


def _reference_repair_planned_change_count(reference_repair: JsonObject) -> int:
    return max(
        _strict_non_negative_int(_json_field(reference_repair, "changed_file_count")),
        _strict_non_negative_int(_json_field(reference_repair, "affected_note_count")),
        _strict_non_negative_int(_json_field(reference_repair, "action_count")),
    )


def _related_notes_planned_change_count(related_notes: JsonObject) -> int:
    updates = _json_field(related_notes, "updates")
    if isinstance(updates, list):
        return sum(1 for update in updates if _json_field(_json_object_or_empty(update), "changed") is True)
    return max(
        _strict_non_negative_int(_json_field(related_notes, "applied_note_count")),
        _strict_non_negative_int(_json_field(related_notes, "update_count")),
    )


def _strict_non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _relative_receipt_path_from_diagnosis(diagnosis: JsonObject, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    wiki_dir = Path(_LinkDiagnosisView.from_payload(diagnosis).wiki_dir)
    if path.is_absolute() and str(wiki_dir):
        try:
            return path.resolve().relative_to(wiki_dir.resolve()).as_posix()
        except (OSError, ValueError):
            return path.as_posix()
    return path.as_posix()


def _write_link_receipt(
    path: Path,
    *,
    config: MedConfig,
    diagnosis: JsonObject,
    snapshot_before: JsonObject,
    snapshot_after: JsonObject,
    git_before: JsonObject,
    git_after: JsonObject,
    body_linker: JsonObject,
    related_notes: LinkRelatedSyncResult | None,
    reference_apply: JsonObject | None,
    graph_after: JsonObject,
) -> JsonObject:
    diagnosis_view = _LinkDiagnosisView.from_payload(diagnosis)
    body_view = _BodyLinkerView.from_payload(body_linker)
    graph_view = _GraphAuditView.from_payload(graph_after)
    git_before_view = _GitContextView.from_payload(git_before)
    git_after_view = _GitContextView.from_payload(git_after)
    related_notes_payload = _related_notes_payload(related_notes)
    changed_files = _changed_files_from(reference_apply, body_linker, related_notes_payload)
    file_changes = _file_changes(
        config=config,
        before_snapshot=snapshot_before,
        after_snapshot=snapshot_after,
        reference_apply=reference_apply,
        body_linker=body_linker,
        related_notes=related_notes,
    )
    reference_apply_view = _ReferenceApplyView.from_payload(reference_apply)
    reference_repair_payload = diagnosis_view.reference_repair.to_payload()
    contextual = body_view.contextual_alias_disambiguation
    phase_receipts = _json_object({
        "reference_repair": {
            "status": reference_apply_view.status or diagnosis_view.reference_repair.status or "skipped",
            "affected_note_count": diagnosis_view.reference_repair.affected_note_count,
            "action_count": diagnosis_view.reference_repair.action_count,
            "blocking_action_count": diagnosis_view.reference_repair.blocking_action_count,
            "human_decision_count": diagnosis_view.reference_repair.human_decision_count,
            "triage_count": diagnosis_view.reference_repair.triage_count,
            "changed_file_count": reference_apply_view.changed_file_count,
        },
        "contextual_alias_disambiguation": {
            "status": contextual.status,
            "mode": contextual.mode,
            "candidate_count": contextual.candidate_count,
            "decision_count": contextual.decision_count,
            "linked_count": contextual.linked_count,
            "deferred_count": contextual.deferred_count,
            "no_link_count": contextual.no_link_count,
            "rejected_count": contextual.rejected_count,
            "skipped_reason": contextual.skipped_reason,
            "blocked_reason": contextual.blocked_reason,
        },
        "body_term_linker": {
            "status": "completed" if body_view.returncode == 0 else "blocked",
            "links_planned": body_view.links_planned,
            "links_rewritten": body_view.links_rewritten,
        },
        "related_notes_sync": {
            "status": related_notes.status if related_notes is not None else "skipped",
            "applied_note_count": related_notes.applied_note_count if related_notes is not None else 0,
            "skipped_reason": related_notes.skipped_reason if related_notes is not None else "",
        },
        "graph_validation": {
            "status": "completed" if not graph_view.error_count else "blocked",
            "error_count": graph_view.error_count,
            "warning_count": graph_view.warning_count,
        },
    })
    body_or_graph_blocked = bool(
        body_view.blocker_count or graph_view.error_count
    )
    related_notes_required = _related_notes_required_for_apply(diagnosis)
    related_notes_blocked = _related_notes_apply_blocked(related_notes, required=related_notes_required)
    blocked = bool(body_or_graph_blocked or related_notes_blocked)
    blocked_reason = _link_apply_blocked_reason(
        body_or_graph_blocked=body_or_graph_blocked,
        related_notes_blocked=related_notes_blocked,
    )
    # The apply receipt carries the same guard evidence that authorized the
    # diagnosis, preserving a single audit trail for mutating linker work.
    version_control_safety = _diagnosis_version_control_safety_payload(diagnosis)
    receipt = _json_object({
        "schema": LINK_RUN_RECEIPT_SCHEMA,
        "generated_at": _now_iso(),
        "phase": "link_apply",
        "status": "completed_with_link_blockers" if blocked else "completed",
        "blocked_reason": blocked_reason,
        "next_action": _link_apply_next_action(blocked_reason=blocked_reason, related_notes=related_notes),
        "required_inputs": [*LINK_REQUIRED_INPUTS, "diagnosis"],
        "human_decision_required": False,
        "receipt_path": str(path),
        "wiki_dir": str(config.wiki_dir),
        "catalog_path": str(config.catalog_path) if config.catalog_path else None,
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "vocabulary_bootstrap": _json_field(diagnosis, "vocabulary_bootstrap", {}),
        "diagnosis_path": diagnosis_view.diagnosis_path,
        "diagnosis_hash": "sha256:" + canonical_json_hash(diagnosis),
        "plan_hash": _json_field(diagnosis, "plan_hash", ""),
        "snapshot_hash": _json_field(diagnosis, "snapshot_hash", ""),
        "trigger_context_summary": _trigger_context_summary(diagnosis),
        "git": {
            "available": git_before_view.available,
            "repo_root": git_before_view.repo_root,
            "branch": git_before_view.branch,
            "head_before": git_before_view.head,
            "head_after": git_after_view.head,
            "status_hash_before": git_before_view.status_hash,
            "status_hash_after": git_after_view.status_hash,
            "changed_paths_before": git_before_view.changed_paths,
            "changed_paths_after": git_after_view.changed_paths,
            "unavailable_reason": git_before_view.unavailable_reason,
        },
        "snapshots": {
            "diagnosis_snapshot_hash": _json_field(diagnosis, "snapshot_hash", ""),
            "before_hash": snapshot_before.get("snapshot_hash", ""),
            "after_hash": snapshot_after.get("snapshot_hash", ""),
            "note_count_before": int(snapshot_before.get("note_count", 0) or 0),
            "note_count_after": int(snapshot_after.get("note_count", 0) or 0),
        },
        "phases": phase_receipts,
        "phase_receipts": phase_receipts,
        "changed_files": changed_files,
        "files_changed": len(changed_files),
        "file_changes": file_changes,
        "version_control_safety": version_control_safety,
        "protected_zone_checks": {
            "status": "completed",
            "strategy": "Fases de apply usam spans protegidos para YAML, headings, code, tabelas, footer e seção Notas Relacionadas quando aplicável.",
        },
        "blockers": _json_field(diagnosis, "blockers", []),
        "skips": _receipt_skips(diagnosis, related_notes),
        "rollback": {
            "type": "git" if git_before.get("available") else "backup",
            "details": "Use os pontos de restauração/version control do vault para rollback.",
        },
        "reference_repair": reference_repair_payload,
        "reference_repair_apply": reference_apply,
        "body_term_linker": body_linker,
        "related_notes_sync": related_notes_payload,
        "graph_audit_after": graph_after,
    })
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return _json_object(receipt)


def apply_link_diagnosis(
    config: MedConfig,
    *,
    diagnosis_path: Path,
    receipt_path: Path | None = None,
    include_related_notes: bool = True,
    backup: bool = False,
    version_control_guard_active: bool = False,
) -> JsonObject:
    diagnosis = _load_diagnosis(diagnosis_path)
    diagnosis_view = _LinkDiagnosisView.from_payload(diagnosis)
    if receipt_path is not None and receipt_path.exists():
        return _json_object({
            "schema": LINK_RUN_SCHEMA,
            "phase": "link_apply_preflight",
            "status": "blocked",
            "blocked_reason": "receipt_path_exists",
            "next_action": "Escolha um novo --receipt para preservar a evidência da tentativa anterior.",
            "required_inputs": ["receipt"],
            "human_decision_required": False,
            "diagnosis_path": str(diagnosis_path),
            "receipt_path": str(receipt_path),
            "returncode": 3,
        })
    current_snapshot = _collect_snapshot(config)
    current_git = _git_context_for(config)
    current_git_payload = current_git.to_payload()
    current_git_view = _GitContextView.from_payload(current_git_payload)
    expected_db = diagnosis_view.vocabulary_db_path
    actual_db = str(config.vocabulary_db_path) if config.vocabulary_db_path else ""
    if expected_db != actual_db:
        return _json_object({
            "schema": LINK_RUN_SCHEMA,
            "phase": "link_apply_preflight",
            "status": "blocked",
            "blocked_reason": "vocabulary_db_mismatch",
            "next_action": "Rodar run-linker --diagnose novamente usando o mesmo vocabulary DB do apply.",
            "required_inputs": ["diagnosis", "vocabulary_db"],
            "human_decision_required": False,
            "diagnosis_path": str(diagnosis_path),
            "expected_vocabulary_db_path": expected_db,
            "actual_vocabulary_db_path": actual_db,
            "returncode": 3,
        })
    if current_snapshot["snapshot_hash"] != _json_field(diagnosis, "snapshot_hash"):
        return _json_object({
            "schema": LINK_RUN_SCHEMA,
            "phase": "link_apply_preflight",
            "status": "blocked",
            "blocked_reason": "stale_diagnosis",
            "next_action": "Rodar run-linker --diagnose novamente; a Wiki mudou desde o diagnóstico.",
            "required_inputs": ["diagnosis"],
            "human_decision_required": False,
            "diagnosis_path": str(diagnosis_path),
            "expected_snapshot_hash": _json_field(diagnosis, "snapshot_hash", ""),
            "actual_snapshot_hash": current_snapshot["snapshot_hash"],
            "returncode": 3,
        })
    expected_git = diagnosis_view.git
    if expected_git.available and expected_git.status_hash:
        if current_git_view.status_hash != expected_git.status_hash:
            return _json_object({
                "schema": LINK_RUN_SCHEMA,
                "phase": "link_apply_preflight",
                "status": "blocked",
                "blocked_reason": "stale_diagnosis",
                "stale_reason": "git_status_changed",
                "next_action": "Rodar run-linker --diagnose novamente; o estado Git da Wiki mudou desde o diagnóstico.",
                "required_inputs": ["diagnosis"],
                "human_decision_required": False,
                "diagnosis_path": str(diagnosis_path),
                "expected_git_status_hash": expected_git.status_hash,
                "actual_git_status_hash": current_git_view.status_hash,
                "expected_git_head": expected_git.head,
                "actual_git_head": current_git_view.head,
                "returncode": 3,
            })
    if diagnosis_view.status != "diagnosis_ready" or diagnosis_view.blocker_count:
        if _diagnosis_requests_vocabulary_repair(diagnosis):
            safety_block = _link_apply_safety_preflight_block(
                diagnosis_path=diagnosis_path,
                diagnosis=diagnosis,
                version_control_guard_active=version_control_guard_active,
                vocabulary_repair_requested=True,
            )
            if safety_block is not None:
                return safety_block
            vocabulary_repair = repair_vocabulary_semantics_for_link(
                config,
                run_dir=diagnosis_path.parent,
                trigger="run_linker_apply",
            )
            try:
                vocabulary_repair_view = _VocabularySemanticRepairView.from_payload(vocabulary_repair)
            except (PydanticValidationError, ValueError) as exc:
                return _link_diagnosis_contract_invalid_payload(
                    diagnosis_path=diagnosis_path,
                    detail=str(exc),
                    source_payload=vocabulary_repair,
                    extra=_json_object({"contract": "vocabulary_semantic_repair"}),
                )
            if vocabulary_repair_view.status == "completed":
                refreshed = diagnose_links(
                    config,
                    diagnosis_path=diagnosis_path,
                    include_related_notes=include_related_notes,
                    force_diagnose=True,
                    trigger_context=_json_object(_json_field(diagnosis, "trigger_context"))
                    if isinstance(_json_field(diagnosis, "trigger_context"), dict)
                    else None,
                )
                refreshed_view = _LinkDiagnosisView.from_payload(refreshed)
                if refreshed_view.status == "diagnosis_ready" and not refreshed_view.blocker_count:
                    applied = apply_link_diagnosis(
                        config,
                        diagnosis_path=diagnosis_path,
                        receipt_path=receipt_path,
                        include_related_notes=include_related_notes,
                        backup=backup,
                        version_control_guard_active=version_control_guard_active,
                    )
                    return _json_object({
                        **applied,
                        "vocabulary_semantic_repair": vocabulary_repair,
                        "vocabulary_repaired_diagnosis": refreshed,
                    })
                return _json_object({
                    "schema": LINK_RUN_SCHEMA,
                    "phase": "link_apply_preflight",
                    "status": "blocked",
                    "blocked_reason": refreshed_view.blocked_reason or "diagnosis_blocked_after_vocabulary_repair",
                    "next_action": refreshed_view.next_action or "Resolver blockers restantes do diagnóstico.",
                    "required_inputs": ["diagnosis"],
                    "human_decision_required": refreshed_view.human_decision_required,
                    "diagnosis_path": str(diagnosis_path),
                    "vocabulary_semantic_repair": vocabulary_repair,
                    "refreshed_diagnosis": refreshed,
                    "returncode": 3,
                })
            return _json_object({
                "schema": LINK_RUN_SCHEMA,
                "phase": "link_apply_preflight",
                "status": "blocked",
                "blocked_reason": vocabulary_repair_view.blocked_reason or "vocabulary_semantic_repair_blocked",
                "next_action": vocabulary_repair_view.next_action or "Resolver vocabulary DB pelo workflow /mednotes:link.",
                "required_inputs": ["vocabulary_semantic_repair"],
                "human_decision_required": vocabulary_repair_view.human_decision_required,
                "diagnosis_path": str(diagnosis_path),
                "vocabulary_semantic_repair": vocabulary_repair,
                "returncode": 3,
            })
        return _json_object({
            "schema": LINK_RUN_SCHEMA,
            "phase": "link_apply_preflight",
            "status": "blocked",
            "blocked_reason": _json_field(diagnosis, "blocked_reason") or "diagnosis_blocked",
            "next_action": _json_field(diagnosis, "next_action") or "Resolver blockers do diagnóstico antes de aplicar.",
            "required_inputs": ["diagnosis"],
            "human_decision_required": bool(_json_field(diagnosis, "human_decision_required")),
            "diagnosis_path": str(diagnosis_path),
            "blocker_count": _json_int(diagnosis, "blocker_count"),
            "blockers": _json_field(diagnosis, "blockers", []),
            "reference_repair": _json_field(diagnosis, "reference_repair", {}),
            "human_decision_packets": _json_field(diagnosis, "human_decision_packets", []),
            "returncode": 3,
        })

    safety_block = _link_apply_safety_preflight_block(
        diagnosis_path=diagnosis_path,
        diagnosis=diagnosis,
        version_control_guard_active=version_control_guard_active,
        vocabulary_repair_requested=False,
    )
    if safety_block is not None:
        return safety_block

    reference_repair_plan = _json_object_or_empty(_json_field(diagnosis, "reference_repair"))
    reference_apply = _json_object(apply_reference_repair_plan(config.wiki_dir, reference_repair_plan))
    diagnosis_body_linker = _json_object_or_empty(_json_field(diagnosis, "body_term_linker"))
    diagnosis_body_view = _BodyLinkerView.from_payload(diagnosis_body_linker)
    if diagnosis_body_view.body_linker_mode == "vocabulary_db":
        body_linker = _json_object(apply_body_linker_plan(
            wiki_dir=config.wiki_dir,
            body_linker_payload=diagnosis_body_linker,
        ))
    else:
        body_linker = _run_body_linker(config, dry_run=False)
    related_notes = (
        _converge_related_notes_sync(config, backup=backup)
        if include_related_notes
        else None
    )
    related_notes_payload = _related_notes_payload(related_notes)
    graph_after = graph_audit(config)
    snapshot_after = _collect_snapshot(config)
    git_after = _git_context_for(config)
    git_after_payload = git_after.to_payload()
    actual_receipt_path = receipt_path or default_link_receipt_path()
    receipt = _write_link_receipt(
        actual_receipt_path,
        config=config,
        diagnosis=diagnosis,
        snapshot_before=current_snapshot,
        snapshot_after=snapshot_after,
        git_before=current_git_payload,
        git_after=git_after_payload,
        body_linker=body_linker,
        related_notes=related_notes,
        reference_apply=reference_apply,
        graph_after=graph_after,
    )
    write_link_state(
        snapshot_hash=_json_text(snapshot_after, "snapshot_hash"),
        git_context=git_after,
        receipt_path=actual_receipt_path,
    )
    body_view = _BodyLinkerView.from_payload(body_linker)
    graph_view = _GraphAuditView.from_payload(graph_after)
    body_or_graph_blocked = bool(
        body_view.blocker_count or graph_view.error_count
    )
    related_notes_required = _related_notes_required_for_apply(diagnosis)
    related_notes_blocked = _related_notes_apply_blocked(related_notes, required=related_notes_required)
    blocked = bool(body_or_graph_blocked or related_notes_blocked)
    blocked_reason = _link_apply_blocked_reason(
        body_or_graph_blocked=body_or_graph_blocked,
        related_notes_blocked=related_notes_blocked,
    )
    return _json_object({
        "schema": LINK_RUN_SCHEMA,
        "phase": "link_apply",
        "status": "completed_with_link_blockers" if blocked else "completed",
        "blocked_reason": blocked_reason,
        "next_action": _link_apply_next_action(blocked_reason=blocked_reason, related_notes=related_notes),
        "required_inputs": [*LINK_REQUIRED_INPUTS, "diagnosis"],
        "human_decision_required": False,
        "wiki_dir": str(config.wiki_dir),
        "catalog_path": str(config.catalog_path) if config.catalog_path else None,
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "diagnosis_path": str(diagnosis_path),
        "receipt_path": str(actual_receipt_path),
        "plan_hash": _json_field(diagnosis, "plan_hash", ""),
        "snapshot_hash": _json_field(diagnosis, "snapshot_hash", ""),
        "phases": receipt["phases"],
        "changed_files": receipt["changed_files"],
        "files_changed": len(receipt["changed_files"]),
        "version_control_safety": _json_object_or_empty(_json_field(receipt, "version_control_safety")),
        "body_term_linker": body_linker,
        "reference_repair_apply": reference_apply,
        "related_notes_sync": related_notes_payload,
        "related_notes_recovery_state": _related_notes_recovery_payload(related_notes),
        "related_notes_applied": bool(related_notes.applied_note_count) if related_notes is not None else False,
        "related_notes_skipped_reason": related_notes.skipped_reason if related_notes is not None else "",
        "graph_audit_after": graph_after,
        "blocker_count": body_view.blocker_count,
        "returncode": 3 if blocked else body_view.returncode,
    })


def run_linker(
    config: MedConfig,
    *,
    diagnose: bool = False,
    apply: bool = False,
    diagnosis_path: Path | None = None,
    receipt_path: Path | None = None,
    include_related_notes: bool = True,
    backup: bool = False,
    force_diagnose: bool = False,
    trigger_context_path: Path | None = None,
    llm_disambiguation: str = "auto",
    llm_model: str | None = None,
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    llm_disambiguator: Callable[..., object] | None = None,
    version_control_guard_active: bool = False,
) -> JsonObject:
    if diagnose == apply:
        raise ValidationError("run-linker requires exactly one of diagnose=True or apply=True.")
    if apply and trigger_context_path is not None:
        raise ValidationError("run-linker --apply does not accept --trigger-context; pass the saved --diagnosis only.")
    if diagnose:
        trigger_context = load_trigger_context(trigger_context_path)
        return diagnose_links(
            config,
            diagnosis_path=diagnosis_path,
            include_related_notes=include_related_notes,
            force_diagnose=force_diagnose,
            trigger_context=trigger_context,
            llm_disambiguation=llm_disambiguation,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            llm_disambiguator=llm_disambiguator,
        )
    if diagnosis_path is None:
        raise ValidationError("run-linker --apply requires --diagnosis <link-diagnosis.json>.")
    return apply_link_diagnosis(
        config,
        diagnosis_path=diagnosis_path,
        receipt_path=receipt_path,
        include_related_notes=include_related_notes,
        backup=backup,
        version_control_guard_active=version_control_guard_active,
    )


def graph_audit(config: MedConfig) -> JsonObject:
    return _json_object(wiki_graph.audit_wiki_graph(config.wiki_dir, catalog_path=config.catalog_path))
