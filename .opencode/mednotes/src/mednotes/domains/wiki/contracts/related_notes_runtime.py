from __future__ import annotations

from typing import Self

from pydantic import Field, StrictStr, model_validator

from mednotes.kernel.base import ContractModel, JsonObjectAdapter, JsonValue

JsonPayload = dict[str, JsonValue]


def _raw_payload(value: object) -> JsonPayload:
    if isinstance(value, ContractModel):
        return JsonObjectAdapter.validate_python(value.to_payload())
    if isinstance(value, dict):
        return JsonObjectAdapter.validate_python(value)
    return {}


def _raw_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return []


def _raw_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"invalid boolean related-notes runtime value: {value}")


def _raw_value_field(raw: JsonPayload, key: str, default: object = "") -> object:
    """Preserve raw field types so Pydantic owns operational validation."""

    if key not in raw:
        return default
    value = raw[key]
    if value is None:
        return default
    return value


def _raw_fallback_value(value: object, default: object = "") -> object:
    """Preserve fallback types so Pydantic owns operational validation."""

    if value is None:
        return default
    return value


class RelatedNotesRecoveryState(ContractModel):
    schema_id: StrictStr = Field(default="", alias="schema")
    status: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    fresh_record_count: int = Field(default=0, ge=0, strict=True)
    partial_record_count: int = Field(default=0, ge=0, strict=True)
    stale_record_count: int = Field(default=0, ge=0, strict=True)
    record_count: int = Field(default=0, ge=0, strict=True)
    total_note_count: int = Field(default=0, ge=0, strict=True)
    remaining_count: int = Field(default=0, ge=0, strict=True)
    embedded_count: int = Field(default=0, ge=0, strict=True)
    reused_count: int = Field(default=0, ge=0, strict=True)
    attempt_count: int = Field(default=0, ge=0, strict=True)
    next_retry_after_seconds: int = Field(default=0, ge=0, strict=True)
    resume_supported: bool = Field(default=False, strict=True)
    operation_payload: JsonPayload = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        raw = _raw_payload(value)
        return cls.model_validate(
            {
                "schema": _raw_value_field(raw, "schema"),
                "status": _raw_value_field(raw, "status"),
                "blocked_reason": _raw_value_field(raw, "blocked_reason"),
                "next_action": _raw_value_field(raw, "next_action"),
                "fresh_record_count": _raw_value_field(raw, "fresh_record_count", 0),
                "partial_record_count": _raw_value_field(raw, "partial_record_count", 0),
                "stale_record_count": _raw_value_field(raw, "stale_record_count", 0),
                "record_count": _raw_value_field(raw, "record_count", 0),
                "total_note_count": _raw_value_field(raw, "total_note_count", 0),
                "remaining_count": _raw_value_field(raw, "remaining_count", 0),
                "embedded_count": _raw_value_field(raw, "embedded_count", 0),
                "reused_count": _raw_value_field(raw, "reused_count", 0),
                "attempt_count": _raw_value_field(raw, "attempt_count", 0),
                "next_retry_after_seconds": _raw_value_field(raw, "next_retry_after_seconds", 0),
                "resume_supported": _raw_bool(_raw_value_field(raw, "resume_supported", False)),
                "operation_payload": raw,
            }
        )

    @classmethod
    def from_headless_projection(cls, value: object, *, blocked_reason: object = "") -> Self:
        raw = _raw_payload(value)
        partial_record_count = _raw_value_field(raw, "partial_record_count", 0)
        blocked_reason_value = _raw_value_field(raw, "blocked_reason") or _raw_fallback_value(blocked_reason)
        payload = {
            "schema": "medical-notes-workbench.related-notes-recovery-state.v1",
            "status": "waiting_for_retry",
            "blocked_reason": blocked_reason_value,
            "next_action": _raw_value_field(raw, "next_action"),
            "fresh_record_count": _raw_value_field(raw, "fresh_record_count", partial_record_count),
            "partial_record_count": partial_record_count,
            "stale_record_count": _raw_value_field(raw, "stale_record_count", 0),
            "record_count": _raw_value_field(raw, "record_count", 0),
            "total_note_count": _raw_value_field(raw, "total_note_count", 0),
            "remaining_count": _raw_value_field(raw, "remaining_count", 0),
            "embedded_count": _raw_value_field(raw, "embedded_count", 0),
            "reused_count": _raw_value_field(raw, "reused_count", 0),
            "attempt_count": _raw_value_field(raw, "attempt_count", 1),
            "next_retry_after_seconds": _raw_value_field(raw, "next_retry_after_seconds", 0),
            "resume_supported": _raw_bool(_raw_value_field(raw, "resume_supported", False)),
        }
        return cls.model_validate({**payload, "operation_payload": payload})

    def __bool__(self) -> bool:
        return bool(self.operation_payload)


class LinkRelatedUpdate(ContractModel):
    path: str = ""
    relative_path: StrictStr = ""
    file: StrictStr = ""
    backup_path: StrictStr = ""
    changed: bool = Field(default=False, strict=True)
    cleared_link_count: int = Field(default=0, ge=0, strict=True)
    proposed_links: list[LinkRelatedProposedLink] = Field(default_factory=list)
    operation_payload: JsonPayload = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        raw = _raw_payload(value)
        return cls.model_validate(
            {
                "path": str(_raw_value_field(raw, "path")),
                "relative_path": _raw_value_field(raw, "relative_path"),
                "file": _raw_value_field(raw, "file"),
                "backup_path": _raw_value_field(raw, "backup_path"),
                "changed": _raw_bool(_raw_value_field(raw, "changed", False)),
                "cleared_link_count": _raw_value_field(raw, "cleared_link_count", 0),
                "proposed_links": _raw_list(_raw_value_field(raw, "proposed_links", [])),
                "operation_payload": raw,
            }
        )


class LinkRelatedProposedLink(ContractModel):
    target_path: StrictStr
    target_title: StrictStr = ""
    score: float = Field(default=0, ge=0)
    rank: int = Field(default=0, ge=0)
    source: StrictStr = ""
    content_hash: StrictStr = ""
    line: StrictStr = ""


class LinkRelatedSkippedEdge(ContractModel):
    source_path: StrictStr = ""
    target_path: StrictStr = ""
    reason: StrictStr
    score: StrictStr = ""


class LinkRelatedRuntimeErrorContext(ContractModel):
    phase: StrictStr = ""
    blocked_reason: StrictStr = ""
    root_cause: StrictStr = ""
    affected_artifact: StrictStr = ""
    error_summary: StrictStr = ""
    suggested_fix: StrictStr = ""
    next_action: StrictStr = ""
    retry_scope: StrictStr = ""
    payload_schema: StrictStr = ""
    validation_errors: list[JsonPayload] = Field(default_factory=list)
    stale_notes: list[JsonPayload] = Field(default_factory=list)
    contract_errors: list[JsonPayload] = Field(default_factory=list)
    forbidden_keys: list[StrictStr] = Field(default_factory=list)
    details: JsonPayload = Field(default_factory=dict)


class RelatedNotesPassSummary(ContractModel):
    kind: str
    status: str = ""
    blocked_reason: str = ""
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    fresh_record_count: int = Field(default=0, ge=0, strict=True)
    stale_record_count: int = Field(default=0, ge=0, strict=True)
    remaining_count: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        raw = _raw_payload(value)
        return cls.model_validate(
            {
                "kind": _raw_value_field(raw, "kind"),
                "status": _raw_value_field(raw, "status"),
                "blocked_reason": _raw_value_field(raw, "blocked_reason"),
                "planned_note_count": _raw_value_field(raw, "planned_note_count", 0),
                "applied_note_count": _raw_value_field(raw, "applied_note_count", 0),
                "fresh_record_count": _raw_value_field(raw, "fresh_record_count", 0),
                "stale_record_count": _raw_value_field(raw, "stale_record_count", 0),
                "remaining_count": _raw_value_field(raw, "remaining_count", 0),
            }
        )

    @classmethod
    def from_sync_result(cls, kind: str, result: LinkRelatedSyncResult) -> Self:
        recovery = result.related_notes_recovery_state
        return cls.model_validate(
            {
                "kind": kind,
                "status": result.status,
                "blocked_reason": result.blocked_reason,
                "planned_note_count": result.planned_note_count,
                "applied_note_count": result.applied_note_count,
                "fresh_record_count": recovery.fresh_record_count,
                "stale_record_count": recovery.stale_record_count,
                "remaining_count": recovery.remaining_count,
            }
        )


class RelatedNotesConvergence(ContractModel):
    """Typed convergence summary; operation_payload remains audit-only."""

    schema_id: StrictStr = Field(default="", alias="schema")
    status: StrictStr = ""
    pass_count: int = Field(default=0, ge=0, strict=True)
    max_passes: int = Field(default=0, ge=0, strict=True)
    cycle_count: int = Field(default=0, ge=0, strict=True)
    max_cycles: int = Field(default=0, ge=0, strict=True)
    operation_count: int = Field(default=0, ge=0, strict=True)
    final_planned_note_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    passes: list[RelatedNotesPassSummary] = Field(default_factory=list)
    operation_payload: JsonPayload = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        raw = _raw_payload(value)
        return cls.model_validate(
            {
                "schema": _raw_value_field(raw, "schema"),
                "status": _raw_value_field(raw, "status"),
                "pass_count": _raw_value_field(raw, "pass_count", 0),
                "max_passes": _raw_value_field(raw, "max_passes", 0),
                "cycle_count": _raw_value_field(raw, "cycle_count", 0),
                "max_cycles": _raw_value_field(raw, "max_cycles", 0),
                "operation_count": _raw_value_field(raw, "operation_count", 0),
                "final_planned_note_count": _raw_value_field(raw, "final_planned_note_count", 0),
                "applied_note_count": _raw_value_field(raw, "applied_note_count", 0),
                "passes": [
                    RelatedNotesPassSummary.from_payload(item)
                    for item in _raw_list(_raw_value_field(raw, "passes", []))
                ],
                "operation_payload": raw,
            }
        )

    def __bool__(self) -> bool:
        return bool(self.operation_payload)


class LinkRelatedSyncResult(ContractModel):
    schema_id: StrictStr = Field(default="", alias="schema")
    phase: StrictStr = ""
    status: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    skipped_reason: StrictStr = ""
    required_inputs: list[StrictStr] = Field(default_factory=list)
    human_decision_required: bool = Field(default=False, strict=True)
    selected_recovery_mode: StrictStr = ""
    manual_instruction_allowed: bool = Field(default=False, strict=True)
    wiki_dir: StrictStr = ""
    default_export_name: StrictStr = ""
    min_score: float | None = None
    max_links: int = Field(default=0, ge=0, strict=True)
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    proposed_link_count: int = Field(default=0, ge=0, strict=True)
    cleared_link_count: int = Field(default=0, ge=0, strict=True)
    skipped_edge_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    error: StrictStr = ""
    parse_error: StrictStr = ""
    export_path: StrictStr = ""
    receipt_path: StrictStr = ""
    updates: list[LinkRelatedUpdate] = Field(default_factory=list)
    skipped_edges: list[LinkRelatedSkippedEdge] = Field(default_factory=list)
    related_notes_recovery_state: RelatedNotesRecoveryState = Field(default_factory=RelatedNotesRecoveryState)
    convergence: RelatedNotesConvergence = Field(default_factory=RelatedNotesConvergence)
    export_relocation: JsonPayload = Field(default_factory=dict)
    error_context: LinkRelatedRuntimeErrorContext | None = None
    operation_payload: JsonPayload = Field(default_factory=dict)

    @model_validator(mode="after")
    def _completed_status_requires_operation_evidence(self) -> Self:
        """Prevent legacy `status=completed` text from fabricating FSM success."""

        if self.status not in {"completed", "recovered"}:
            return self
        if self.phase not in {
            "related_notes_apply",
            "related_notes_dry_run",
            "related_notes_export_recovery",
            "related_notes_sync",
            "related_notes_apply_convergence",
        }:
            raise ValueError("completed related-notes result requires a known operation phase")
        if self._has_success_operation_evidence():
            return self
        raise ValueError("completed related-notes result requires operation evidence")

    def _has_success_operation_evidence(self) -> bool:
        """Check for explicit adapter evidence, not defaulted model values."""

        if self.receipt_path or self.export_path or self.updates or self.skipped_edges:
            return True
        if self.related_notes_recovery_state.operation_payload:
            return True
        for field_name in (
            "planned_note_count",
            "proposed_link_count",
            "cleared_link_count",
            "skipped_edge_count",
            "applied_note_count",
        ):
            if field_name in self.operation_payload:
                return True
        return False

    @classmethod
    def from_payload(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        raw = _raw_payload(value)
        recovery = _raw_value_field(raw, "related_notes_recovery_state", None)
        if recovery:
            recovery_state = RelatedNotesRecoveryState.from_payload(recovery)
        elif _raw_value_field(raw, "headless_export", None):
            recovery_state = RelatedNotesRecoveryState.from_headless_projection(
                _raw_value_field(raw, "headless_export", None),
                blocked_reason=_raw_value_field(raw, "blocked_reason"),
            )
        else:
            recovery_state = RelatedNotesRecoveryState()
        return cls.model_validate(
            {
                "schema": _raw_value_field(raw, "schema"),
                "phase": _raw_value_field(raw, "phase"),
                "status": _raw_value_field(raw, "status"),
                "blocked_reason": _raw_value_field(raw, "blocked_reason"),
                "next_action": _raw_value_field(raw, "next_action"),
                "skipped_reason": _raw_value_field(raw, "skipped_reason"),
                "required_inputs": _raw_value_field(raw, "required_inputs", []),
                "human_decision_required": _raw_value_field(raw, "human_decision_required", False),
                "selected_recovery_mode": _raw_value_field(raw, "selected_recovery_mode"),
                "manual_instruction_allowed": _raw_value_field(raw, "manual_instruction_allowed", False),
                "wiki_dir": _raw_value_field(raw, "wiki_dir"),
                "default_export_name": _raw_value_field(raw, "default_export_name"),
                "min_score": _raw_value_field(raw, "min_score", None),
                "max_links": _raw_value_field(raw, "max_links", 0),
                "planned_note_count": _raw_value_field(raw, "planned_note_count", 0),
                "proposed_link_count": _raw_value_field(raw, "proposed_link_count", 0),
                "cleared_link_count": _raw_value_field(raw, "cleared_link_count", 0),
                "skipped_edge_count": _raw_value_field(raw, "skipped_edge_count", 0),
                "applied_note_count": _raw_value_field(raw, "applied_note_count", 0),
                "error": _raw_value_field(raw, "error"),
                "parse_error": _raw_value_field(raw, "parse_error"),
                "export_path": _raw_value_field(raw, "export_path"),
                "receipt_path": _raw_value_field(raw, "receipt_path"),
                "updates": [
                    LinkRelatedUpdate.from_payload(item)
                    for item in _raw_list(_raw_value_field(raw, "updates", []))
                ],
                "skipped_edges": _raw_list(_raw_value_field(raw, "skipped_edges", [])),
                "related_notes_recovery_state": recovery_state,
                "convergence": RelatedNotesConvergence.from_payload(_raw_value_field(raw, "convergence", None)),
                "export_relocation": _raw_payload(_raw_value_field(raw, "export_relocation", None)),
                "error_context": _raw_payload(_raw_value_field(raw, "error_context", None)) or None,
                "operation_payload": raw,
            }
        )


class LinkRelatedNotesSync(LinkRelatedSyncResult):
    pass
