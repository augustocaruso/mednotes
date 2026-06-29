"""Typed linker runtime boundary for `/mednotes:link` and `/mednotes:link-body`.

The linker adapter still returns operational JSON. This boundary validates that
JSON into facts, but it does not name the final workflow outcome. The only
machine event emitted here is `LinkRuntimeObservedEvent`; `LinkMachine` guards
own the operational leaf-state priority.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictStr, model_validator

from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.contracts.curator import CuratorBatchPlan
from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedNotesSync, RelatedNotesRecoveryState
from mednotes.domains.wiki.contracts.vocabulary_ingestion import VocabularyBootstrapPlan
from mednotes.domains.wiki.flows.link.link_fsm import LINK_BODY_PUBLIC_WORKFLOW, LINK_WORKFLOW, LinkFsmFacts
from mednotes.domains.wiki.flows.link.link_machine import (
    LinkMode,
    LinkRuntimeObservation,
    LinkRuntimeObservedEvent,
    LinkState,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.workflow import VersionControlSafety


class LinkerRunResult(ContractModel):
    """Typed lens for linker operation JSON before it reaches LinkMachine.

    The lower-level linker emits one contract for diagnosis and another for
    apply. Missing or unknown schemas are contract errors; defaults here may
    preserve optional counters, never fabricate a valid operation from
    non-contract JSON.
    """

    schema_id: Literal[
        "medical-notes-workbench.link-diagnosis.v1",
        "medical-notes-workbench.link-run.v1",
        "medical-notes-workbench.link-run-receipt.v1",
    ] = Field(alias="schema")
    phase: StrictStr = ""
    status: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    returncode: int = Field(default=0, ge=0, strict=True)
    files_changed: int = Field(default=0, ge=0, strict=True)
    links_planned: int = Field(default=0, ge=0, strict=True)
    links_rewritten: int = Field(default=0, ge=0, strict=True)
    blocker_count: int = Field(default=0, ge=0, strict=True)
    related_notes_applied: bool = Field(default=False, strict=True)
    changed_files: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    diagnosis_path: StrictStr = ""
    receipt_path: StrictStr = ""
    error: StrictStr = ""
    parse_error: StrictStr = ""
    stale_reason: StrictStr = ""
    related_notes_sync: LinkRelatedNotesSync | None = None
    related_notes_recovery_state: RelatedNotesRecoveryState = Field(default_factory=RelatedNotesRecoveryState)
    body_term_linker: JsonObject | None = None
    vocabulary_db_path: StrictStr = ""
    vocabulary_bootstrap: VocabularyBootstrapPlan | None = None
    vocabulary_curator_batch_plan: CuratorBatchPlan | None = None
    vocabulary_curator_batch_plan_path: StrictStr = ""
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_operation_contract(self) -> LinkerRunResult:
        """Require the minimum public fields that keep success from being invented."""

        if not self.status.strip():
            raise ValueError("status is required for linker runtime contract")
        return self

    @classmethod
    def from_payload(cls, value: object) -> LinkerRunResult:
        if isinstance(value, cls):
            return value
        raw = _raw_payload(value)
        related = _raw_field(raw, "related_notes_sync")
        bootstrap = _raw_field(raw, "vocabulary_bootstrap")
        curator_batch_plan = _raw_field(raw, "vocabulary_curator_batch_plan")
        return cls.model_validate(
            {
                "schema": _raw_text_field(raw, "schema"),
                "phase": _raw_text_field(raw, "phase"),
                "status": _raw_text_field(raw, "status"),
                "blocked_reason": _raw_text_field(raw, "blocked_reason"),
                "next_action": _raw_text_field(raw, "next_action"),
                "returncode": _raw_field(raw, "returncode", 0),
                "files_changed": _raw_field(raw, "files_changed", 0),
                "links_planned": _raw_field(raw, "links_planned", 0),
                "links_rewritten": _raw_field(raw, "links_rewritten", 0),
                "blocker_count": _raw_field(raw, "blocker_count", 0),
                "related_notes_applied": _raw_field(raw, "related_notes_applied", False),
                "changed_files": [str(item) for item in _raw_list(_raw_field(raw, "changed_files")) if item],
                "required_inputs": [str(item) for item in _raw_list(_raw_field(raw, "required_inputs")) if item],
                "diagnosis_path": _raw_text_field(raw, "diagnosis_path"),
                "receipt_path": _raw_text_field(raw, "receipt_path"),
                "error": _raw_text_field(raw, "error"),
                "parse_error": _raw_text_field(raw, "parse_error"),
                "stale_reason": _raw_text_field(raw, "stale_reason"),
                "related_notes_sync": LinkRelatedNotesSync.from_payload(related) if isinstance(related, dict) else None,
                "related_notes_recovery_state": RelatedNotesRecoveryState.from_payload(
                    _raw_field(raw, "related_notes_recovery_state")
                ),
                "body_term_linker": _raw_payload(_raw_field(raw, "body_term_linker")) or None,
                "vocabulary_db_path": _raw_text_field(raw, "vocabulary_db_path"),
                "vocabulary_bootstrap": (
                    VocabularyBootstrapPlan.model_validate(bootstrap) if bootstrap is not None else None
                ),
                "vocabulary_curator_batch_plan": (
                    CuratorBatchPlan.model_validate(curator_batch_plan) if _has_payload(curator_batch_plan) else None
                ),
                "vocabulary_curator_batch_plan_path": _raw_text_field(raw, "vocabulary_curator_batch_plan_path"),
                "operation_payload": raw,
            }
        )


def link_fsm_facts_from_linker_result(
    result: JsonObject,
    *,
    run_id: str,
    mode: Literal["diagnose", "apply"],
    include_related_notes: bool,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> LinkFsmFacts:
    """Translate validated linker facts into the single LinkMachine observation event."""

    linker_result = LinkerRunResult.from_payload(result)
    link_mode = LinkMode.FULL if include_related_notes else LinkMode.BODY_ONLY
    workflow = LINK_WORKFLOW if link_mode == LinkMode.FULL else LINK_BODY_PUBLIC_WORKFLOW
    observation = _observation_for_linker_result(
        linker_result,
        operation=mode,
        link_mode=link_mode,
    )
    event = LinkRuntimeObservedEvent(
        workflow=workflow,
        run_id=run_id,
        current_state=LinkState.DIAGNOSING_GRAPH.value,
        observation=observation,
        audit_evidence=_audit_evidence(linker_result, mode=mode, include_related_notes=include_related_notes),
    )
    changed_files = list(linker_result.changed_files)
    changed_count = max(linker_result.files_changed, len(changed_files))
    return LinkFsmFacts(
        workflow=workflow,
        mode=link_mode,
        run_id=run_id,
        initial_state=LinkState.DIAGNOSING_GRAPH,
        event=event,
        changed_files=changed_files,
        mutated=mode == "apply" and changed_count > 0,
        artifacts=_artifacts(linker_result),
        version_control_safety=_version_control_safety_with_file_count(
            version_control_safety,
            changed_file_count=changed_count,
        ),
        error_context=_error_context(linker_result),
    )


def _observation_for_linker_result(
    result: LinkerRunResult,
    *,
    operation: Literal["diagnose", "apply"],
    link_mode: LinkMode,
) -> LinkRuntimeObservation:
    body_reason = _body_linker_blocked_reason(result)
    related_blocked = link_mode == LinkMode.FULL and _related_notes_blocked(result)
    vocabulary_bootstrap_required = link_mode == LinkMode.FULL and body_reason == "vocabulary_bootstrap_required"
    vocabulary_curator_required = link_mode == LinkMode.FULL and body_reason == "vocabulary_semantic_ingestion_pending"
    vocabulary_db_path = _vocabulary_db_path(result)
    if vocabulary_bootstrap_required and not vocabulary_db_path:
        raise ValueError("vocabulary_db_path is required when vocabulary bootstrap is required")
    return LinkRuntimeObservation(
        mode=link_mode,
        operation=operation,
        failed=_failed(result),
        stale_diagnosis=_stale_diagnosis(result),
        changed_file_count=max(result.files_changed, len(result.changed_files)),
        planned_link_count=result.links_planned,
        rewritten_link_count=result.links_rewritten,
        blocker_count=max(result.blocker_count, 1 if result.status == "blocked" else 0),
        body_linker_blocked=bool(body_reason),
        body_linker_blocked_reason=body_reason,
        related_notes_present=result.related_notes_sync is not None,
        related_notes_blocked=related_blocked,
        related_notes_waiting_external=link_mode == LinkMode.FULL and _related_notes_waiting_external(result),
        related_notes_applied=result.related_notes_applied,
        vocabulary_bootstrap_required=vocabulary_bootstrap_required,
        vocabulary_curator_required=vocabulary_curator_required,
        vocabulary_db_path=vocabulary_db_path,
        vocabulary_curator_batch_plan_path=_vocabulary_curator_batch_plan_path(result),
        vocabulary_curator_work_item_count=_vocabulary_curator_work_item_count(result),
        next_action=_next_action_from_facts(result),
        reason_code=_reason_code_from_facts(result),
        related_notes_export_recovery_required=bool(_related_notes_export_recovery_reason(result)),
        related_notes_export_recovery_reason=_related_notes_export_recovery_reason(result),
        related_notes_recovery_state=result.related_notes_recovery_state.to_payload(),
    )


def _failed(result: LinkerRunResult) -> bool:
    if result.error.strip() or result.parse_error.strip():
        return True
    return result.returncode not in {0, 3}


def _stale_diagnosis(result: LinkerRunResult) -> bool:
    if result.blocked_reason == "stale_diagnosis":
        return True
    return bool(result.stale_reason.strip())


def _related_notes_blocked(result: LinkerRunResult) -> bool:
    related = result.related_notes_sync
    return bool(
        result.blocked_reason == "related_notes_blocked"
        or (related is not None and (related.status == "blocked" or bool(related.blocked_reason)))
    )


RELATED_NOTES_EXPORT_RECOVERY_REASONS = frozenset(
    {
        "related_notes_hash_mismatch",
        "related_notes_export_stale",
        "related_notes_vault_mismatch",
    }
)


def _related_notes_export_recovery_reason(result: LinkerRunResult) -> str:
    """Expose a recoverable Related Notes export fact without executing recovery."""

    related = result.related_notes_sync
    reason = related.blocked_reason if related is not None else ""
    return reason if reason in RELATED_NOTES_EXPORT_RECOVERY_REASONS else ""


def _related_notes_waiting_external(result: LinkerRunResult) -> bool:
    recovery = result.related_notes_recovery_state
    return (
        _related_notes_blocked(result)
        and recovery.status == "waiting_for_retry"
        and recovery.blocked_reason
        in {
            "related_notes_headless_quota_exhausted",
            "related_notes_headless_time_budget_exhausted",
        }
    )


class _BodyTermLinkerFields(ContractModel):
    """Typed body-linker blocker fields used to derive link observation facts."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: StrictStr = ""


def _body_linker_blocked_reason(result: LinkerRunResult) -> str:
    return _BodyTermLinkerFields.model_validate(result.body_term_linker or {}).blocked_reason


def _reason_code_from_facts(result: LinkerRunResult) -> str:
    if _stale_diagnosis(result):
        return "stale_diagnosis"
    if _body_linker_blocked_reason(result):
        body_reason = _body_linker_blocked_reason(result)
        if body_reason == "vocabulary_semantic_ingestion_pending":
            return "vocabulary_curator_required"
        return body_reason
    recovery_reason = _related_notes_export_recovery_reason(result)
    if recovery_reason:
        return recovery_reason
    if _related_notes_blocked(result):
        return "related_notes_blocked"
    if result.blocked_reason.strip():
        return result.blocked_reason.strip()
    if result.error.strip():
        return result.error.strip()
    if result.parse_error.strip():
        return result.parse_error.strip()
    return ""


def _next_action_from_facts(result: LinkerRunResult) -> str:
    if result.next_action.strip():
        return result.next_action.strip()
    reason = _reason_code_from_facts(result)
    match reason:
        case "stale_diagnosis":
            return "Repetir a conferencia de links e aplicar o novo diagnostico."
        case "vocabulary_bootstrap_required":
            return "Preparar o vocabulario pela rota oficial e repetir /mednotes:link."
        case "vocabulary_semantic_ingestion_pending" | "vocabulary_curator_required":
            return "Aplicar a curadoria semantica oficial e repetir /mednotes:link."
        case "related_notes_blocked":
            return "Atualizar o export do Related Notes e repetir /mednotes:link."
        case "graph_blockers":
            return "Resolver os bloqueios de grafo pela rota oficial e repetir /mednotes:link."
        case "body_linker_blocked":
            return "Resolver o bloqueio informado pelo linker e repetir /mednotes:link."
        case _:
            return ""


def _audit_evidence(
    result: LinkerRunResult,
    *,
    mode: Literal["diagnose", "apply"],
    include_related_notes: bool,
) -> JsonObject:
    recovery = result.related_notes_recovery_state
    operation: JsonObject = {
        "schema": result.schema_id,
        "phase": result.phase,
        "status": result.status,
        "blocked_reason": _reason_code_from_facts(result),
        "next_action": _next_action_from_facts(result),
        "returncode": result.returncode,
        "files_changed": result.files_changed,
        "links_planned": result.links_planned,
        "links_rewritten": result.links_rewritten,
        "blocker_count": result.blocker_count,
        "related_notes_applied": result.related_notes_applied,
        "changed_files": list(result.changed_files),
        "required_inputs": list(result.required_inputs),
        "diagnosis_path": result.diagnosis_path,
        "receipt_path": result.receipt_path,
    }
    if result.related_notes_sync is not None:
        operation["related_notes_sync"] = result.related_notes_sync.to_payload()
    if result.body_term_linker is not None:
        operation["body_term_linker"] = result.body_term_linker
    if result.vocabulary_bootstrap is not None:
        operation["vocabulary_bootstrap"] = result.vocabulary_bootstrap.to_payload()
    if result.vocabulary_curator_batch_plan is not None:
        operation["vocabulary_curator_batch_plan"] = result.vocabulary_curator_batch_plan.to_payload()
    if recovery:
        operation["related_notes_recovery_state"] = recovery.to_payload()
    evidence: JsonObject = {
        "adapter_schema": result.schema_id,
        "adapter_phase": result.phase,
        "adapter_status": result.status,
        "adapter_reason": _reason_code_from_facts(result),
        "operation": operation,
        "mode": mode,
        "include_related_notes": include_related_notes,
        "counts": {
            "files_changed": max(result.files_changed, len(result.changed_files)),
            "links_planned": result.links_planned,
            "links_rewritten": result.links_rewritten,
            "blocker_count": result.blocker_count,
            "related_notes_applied": result.related_notes_applied,
            "fresh_record_count": recovery.fresh_record_count,
            "remaining_count": recovery.remaining_count,
            "total_note_count": recovery.total_note_count,
            "reused_count": recovery.reused_count,
            "embedded_count": recovery.embedded_count,
        },
    }
    if result.required_inputs:
        evidence["required_inputs"] = list(result.required_inputs)
    if recovery:
        evidence["related_notes_recovery_state"] = recovery.to_payload()
    if result.stale_reason:
        evidence["stale_reason"] = result.stale_reason
    for key in (
        "expected_git_status_hash",
        "actual_git_status_hash",
        "expected_git_head",
        "actual_git_head",
    ):
        try:
            value = result.operation_payload[key]
        except KeyError:
            continue
        if isinstance(value, str) and value.strip():
            evidence[key] = value
    return JsonObjectAdapter.validate_python(evidence)


def _error_context(result: LinkerRunResult) -> JsonObject:
    reason = _reason_code_from_facts(result)
    if not (
        _failed(result)
        or _stale_diagnosis(result)
        or result.blocker_count
        or _body_linker_blocked_reason(result)
        or _related_notes_blocked(result)
    ):
        return {}
    summary = result.error or result.parse_error or result.blocked_reason or reason
    return JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.error-context.v1",
            "phase": result.phase or "link",
            "blocked_reason": reason,
            "root_cause": reason,
            "error_summary": summary,
            "suggested_fix": _next_action_from_facts(result),
            "next_action": _next_action_from_facts(result),
            "retry_scope": "link_official_route",
            "human_decision_required": False,
            "missing_inputs": list(result.required_inputs),
        }
    )


def _vocabulary_db_path(result: LinkerRunResult) -> str:
    if result.vocabulary_bootstrap is not None:
        return result.vocabulary_bootstrap.db_path.strip()
    return result.vocabulary_db_path


def _vocabulary_curator_batch_plan_path(result: LinkerRunResult) -> str:
    return result.vocabulary_curator_batch_plan_path


def _vocabulary_curator_work_item_count(result: LinkerRunResult) -> int:
    if result.vocabulary_curator_batch_plan is None:
        return 1
    return max(1, result.vocabulary_curator_batch_plan.item_count)


def _artifacts(result: LinkerRunResult) -> JsonObject:
    artifacts: JsonObject = {}
    if result.diagnosis_path:
        artifacts["diagnosis_path"] = result.diagnosis_path
    if result.receipt_path:
        artifacts["receipt_path"] = result.receipt_path
    return artifacts


def _version_control_safety_with_file_count(
    value: VersionControlSafety | dict[str, object],
    *,
    changed_file_count: int,
) -> VersionControlSafety:
    safety = value if isinstance(value, VersionControlSafety) else VersionControlSafety.model_validate(value)
    if safety.changed_file_count == changed_file_count:
        return safety
    raise ValidationError(
        "version_control_safety_evidence_mismatch: guard evidence changed_file_count "
        "does not match linker result"
    )


def _raw_payload(value: object) -> JsonObject:
    if isinstance(value, ContractModel):
        return JsonObjectAdapter.validate_python(value.to_payload())
    if isinstance(value, dict):
        return JsonObjectAdapter.validate_python(dict(value))
    return {}


def _has_payload(value: object) -> bool:
    """Treat empty optional operation objects as absent, not as valid plans."""

    return isinstance(value, dict) and bool(value)


def _raw_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return []


def _raw_field(raw: JsonObject, key: str, default: object = None) -> object:
    try:
        return raw[key]
    except KeyError:
        return default


def _raw_text_field(raw: JsonObject, key: str) -> object:
    value = _raw_field(raw, key, "")
    return "" if value is None else value
