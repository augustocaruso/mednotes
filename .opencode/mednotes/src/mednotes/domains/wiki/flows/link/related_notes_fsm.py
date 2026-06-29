from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import SkipJsonSchema
from statemachine import StateChart
from statemachine.states import States

from mednotes.domains.wiki.contracts.effect_payloads import (
    RelatedNotesRecoveryStateEffectPayload,
    WaitExternalEffectPayload,
)
from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedSyncResult, RelatedNotesRecoveryState
from mednotes.domains.wiki.contracts.workflow_outcomes import DecisionEvidence, WorkflowDecision
from mednotes.domains.wiki.flows.link_related.link_related_machine import (
    LinkRelatedBoundaryEvent,
    LinkRelatedMachine,
    LinkRelatedRuntimeObservation,
    LinkRelatedRuntimeObservedEvent,
    category_for_link_related_state,
)
from mednotes.domains.wiki.flows.link_related.link_related_machine import (
    LinkRelatedState as MachineLinkRelatedState,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effect_intent import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.fsm_event import WorkflowEventLike
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.progress import (
    WorkflowProgressCounts,
    WorkflowProgressEventType,
    WorkflowProgressState,
    WorkflowProgressStatus,
    WorkflowProgressViewModel,
    build_progress_view_model,
    progress_state_from_view_model,
)
from mednotes.kernel.public_report import (
    WorkflowPrimaryObjectiveSummary,
    WorkflowPublicReport,
    WorkflowReports,
    assert_public_report_matches_progress,
    public_progress_followup_line,
)
from mednotes.kernel.state_machine import (
    WorkflowStateCategory,
    WorkflowStateMachineSnapshot,
    WorkflowTransition,
    send_workflow_event,
)
from mednotes.kernel.workflow import (
    HumanDecisionPacket,
    ReceiptStatus,
    VersionControlSafety,
    WorkflowReceiptPayload,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

LINK_RELATED_WORKFLOW = "/mednotes:link-related"
LINK_RELATED_SCHEMA = "medical-notes-workbench.link-related-fsm-result.v1"
LINK_RELATED_RECEIPT_SCHEMA = "medical-notes-workbench.link-related-receipt.v1"
LINK_RELATED_AGENT_DIRECTIVE_FIELD = "agent_directive"

_PHASE = "related_notes_recovery"
_WAITING_QUOTA_STATE = "waiting_for_external_quota"
_RECOVERY_BLOCKED_STATE = "related_notes_recovery_blocked"
_RECOVERING_STATE = "recovering_related_notes"

LINK_RELATED_ALLOWED_ROOT_KEYS = frozenset(
    {
        "schema",
        "workflow",
        "run_id",
        "state_machine_snapshot",
        "progress_view_model",
        "decision",
        "human_decision_packet",
        "receipt",
        "reports",
        "agent_directive",
        "artifacts",
        "version_control_safety",
        "diagnostic_context",
        "error_context",
    }
)
LINK_RELATED_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "manual_instruction_allowed",
        "selected_recovery_mode",
        "retry_command",
        "wiki_dir",
        "export_path",
        "planned_note_count",
        "proposed_link_count",
        "cleared_link_count",
        "skipped_edge_count",
        "applied_note_count",
        "updates",
        "skipped_edges",
        "related_notes_recovery_state",
    }
)


def category_for_state(state: str) -> WorkflowStateCategory:
    """Map link-related leaf states to the public FSM category."""

    return category_for_link_related_state(MachineLinkRelatedState(state))


class LinkRelatedFsmFacts(ContractModel):
    run_id: str = Field(min_length=1)
    initial_state: MachineLinkRelatedState
    event: LinkRelatedBoundaryEvent
    changed_files: list[str] = Field(default_factory=list)
    mutated: bool = False
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _event_must_match_fsm_entry(self) -> LinkRelatedFsmFacts:
        if self.event.workflow != LINK_RELATED_WORKFLOW:
            raise ValueError(f"link-related event workflow must be {LINK_RELATED_WORKFLOW}")
        if self.event.run_id != self.run_id:
            raise ValueError("link-related event run_id must match LinkRelatedFsmFacts.run_id")
        if self.event.current_state != self.initial_state.value:
            raise ValueError("link-related event current_state must match initial_state")
        return self


class _LinkRelatedRuntimeFacts(ContractModel):
    """Typed runtime bridge from Related Notes sync output to a machine event."""

    run_id: str = Field(min_length=1)
    mode: Literal["dry_run", "apply", "recover_export"]
    sync_result: LinkRelatedSyncResult = Field(default_factory=LinkRelatedSyncResult)
    version_control_safety: VersionControlSafety
    next_action: str = ""
    human_decision_packet: HumanDecisionPacket | None = None
    error_context: JsonObject = Field(default_factory=dict)

    @field_validator("sync_result", mode="before")
    @classmethod
    def _coerce_sync_result(cls, value: object) -> LinkRelatedSyncResult:
        return LinkRelatedSyncResult.from_payload(value)

    @model_validator(mode="after")
    def _observation_must_be_modeled(self) -> _LinkRelatedRuntimeFacts:
        observation = _link_related_runtime_observation(self)
        if not (
            observation.failed
            or observation.export_missing
            or observation.export_stale
            or observation.preview_ready
            or observation.applied
            or observation.blocked
            or observation.waiting_external
        ):
            raise ValueError("effect_payload_contract_invalid: unmodeled related-notes runtime status")
        return self


class _LinkRelatedOperationDecisionFields(ContractModel):
    """Typed lens for the optional ask-human packet embedded in adapter output."""

    model_config = ConfigDict(extra="ignore")

    human_decision_packet: HumanDecisionPacket | None = None


class _LinkRelatedReportOperationFields(ContractModel):
    """Small projection lens for audit-only Related Notes operation payloads."""

    model_config = ConfigDict(extra="ignore")

    status: StrictStr = ""
    phase: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    proposed_link_count: int = Field(default=0, ge=0, strict=True)
    cleared_link_count: int = Field(default=0, ge=0, strict=True)
    skipped_edge_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    updates: list[JsonObject] = Field(default_factory=list)
    skipped_edges: list[JsonObject] = Field(default_factory=list)
    export_relocation: JsonObject = Field(default_factory=dict)
    related_notes_recovery_state: JsonObject = Field(default_factory=dict)


class _LinkRelatedErrorPayloadFields(ContractModel):
    """Typed lens for error evidence stored in the adapter operation payload."""

    model_config = ConfigDict(extra="ignore")

    validation_errors: list[object] = Field(default_factory=list)
    contract_errors: list[object] = Field(default_factory=list)
    hash_errors: list[object] = Field(default_factory=list)
    stale_notes: list[object] = Field(default_factory=list)
    forbidden_keys: list[object] = Field(default_factory=list)
    detail: object = ""
    selected_recovery_mode: object = ""
    command_returncode: object = ""
    error: object = ""
    parse_error: object = ""
    skipped_reason: object = ""

    @property
    def has_error_detail(self) -> bool:
        return any(
            (
                self.validation_errors,
                self.contract_errors,
                self.hash_errors,
                self.stale_notes,
                self.forbidden_keys,
                self.detail,
                self.error,
                self.parse_error,
                self.skipped_reason,
            )
        )


class _LinkRelatedPayloadProgressViewFields(ContractModel):
    status: StrictStr
    state: StrictStr = ""


class _LinkRelatedPayloadSnapshotFields(ContractModel):
    current_category: StrictStr


class _LinkRelatedPayloadReceiptFields(ContractModel):
    status: StrictStr


class _LinkRelatedPayloadFields(ContractModel):
    workflow: StrictStr
    progress_view_model: _LinkRelatedPayloadProgressViewFields
    state_machine_snapshot: _LinkRelatedPayloadSnapshotFields
    receipt: _LinkRelatedPayloadReceiptFields
    diagnostic_context: JsonObject = Field(default_factory=dict)


class LinkRelatedFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.link-related-fsm-result.v1"] = Field(
        default=LINK_RELATED_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/mednotes:link-related"] = LINK_RELATED_WORKFLOW
    run_id: str = Field(min_length=1)
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    state_machine_snapshot: WorkflowStateMachineSnapshot
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: WorkflowReceiptPayload
    reports: WorkflowReports
    agent_directive: JsonObject
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _hydrate_progress_state_from_public_payload(cls, value: object) -> object:
        """Accept public payloads where progress_state is intentionally hidden."""

        if not isinstance(value, dict) or "progress_state" in value or "progress_view_model" not in value:
            return value
        hydrated = dict(value)
        progress_view = WorkflowProgressViewModel.model_validate(value["progress_view_model"])
        hydrated["progress_state"] = progress_state_from_view_model(progress_view).to_payload()
        return hydrated

    @model_validator(mode="after")
    def _progress_view_model_matches_state(self) -> LinkRelatedFsmResult:
        expected = build_progress_view_model(self.progress_state).to_payload()
        if self.progress_view_model.to_payload() != expected:
            raise ValueError("progress_view_model must match progress_state")
        return self

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "schema": self.schema_id,
            "workflow": self.workflow,
            "run_id": self.run_id,
            "state_machine_snapshot": self.state_machine_snapshot.to_payload(),
            "progress_view_model": self.progress_view_model.to_payload(),
            "decision": self.decision.to_payload() if self.decision is not None else None,
            "human_decision_packet": self.human_decision_packet.to_payload()
            if self.human_decision_packet is not None
            else None,
            "receipt": self.receipt.to_payload(),
            "reports": self.reports.to_payload(),
            "agent_directive": dict(self.agent_directive),
            "artifacts": dict(self.artifacts),
            "version_control_safety": self.version_control_safety.to_payload(),
            "error_context": dict(self.error_context),
        }
        if self.diagnostic_context:
            payload["diagnostic_context"] = dict(self.diagnostic_context)
        payload = JsonObjectAdapter.validate_python(payload)
        assert_link_related_fsm_payload(payload)
        return payload


def build_link_related_fsm_result(facts: LinkRelatedFsmFacts) -> LinkRelatedFsmResult:
    """Project one typed LinkRelatedMachine event into the public payload."""

    return build_link_related_fsm_result_from_model(
        _link_related_model_after_event(facts.initial_state, facts.event),
        version_control_safety=facts.version_control_safety,
        error_context=facts.error_context,
        artifacts=facts.artifacts,
        changed_files=facts.changed_files,
        mutated=facts.mutated,
    )


def link_related_fsm_payload_from_sync_result(
    result: JsonObject,
    *,
    run_id: str,
    mode: Literal["dry_run", "apply", "recover_export"],
    version_control_safety: VersionControlSafety | dict[str, object],
) -> JsonObject:
    return build_link_related_fsm_result(
        link_related_fsm_facts_from_sync_result(
            result,
            run_id=run_id,
            mode=mode,
            version_control_safety=version_control_safety,
        )
    ).to_payload()


def build_link_related_fsm_result_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
    error_context: JsonObject | None = None,
    artifacts: JsonObject | None = None,
    changed_files: list[str] | None = None,
    mutated: bool | None = None,
) -> LinkRelatedFsmResult:
    """Project the real LinkRelatedMachine model without reading operation payloads."""

    _validate_link_related_machine_model(model)
    state = MachineLinkRelatedState(model.state)
    category = category_for_link_related_state(state)
    progress_state = _progress_state_from_model(model, state, category)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _snapshot_from_model(model, state, category)
    safety = _version_control_safety(version_control_safety)
    receipt = _receipt_from_model(
        model,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        version_control_safety=safety,
        changed_files=changed_files or [],
        mutated=mutated,
    )
    reports_model = _reports_from_model(model, state, progress_state)
    public_report = reports_model.public_report
    diagnostic_context = _diagnostic_context_from_model(model, state, category)
    agent_directive = agent_directive_from_progress_view_model(
        progress_view_model,
        schema="medical-notes-workbench.agent-directive.v1",
        reason=_machine_reason_code(model, state),
        effects=model.pending_effects,
        blockers=_machine_blockers(category, model, state),
        resume=progress_state.resume_action,
        report_requires=["related_notes"],
        summary=public_report.summary_text(),
        instructions=_machine_agent_instructions(category),
    ).to_payload()
    machine_error_context = error_context or _error_context_from_model(model, state, category)
    return LinkRelatedFsmResult(
        run_id=model.run_id,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
        decision=model.last_transition.decision if model.last_transition is not None else None,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        receipt=receipt,
        reports=reports_model,
        agent_directive=JsonObjectAdapter.validate_python(agent_directive),
        artifacts=artifacts or {},
        version_control_safety=safety,
        diagnostic_context=diagnostic_context,
        error_context=machine_error_context,
    )


def link_related_fsm_payload_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> JsonObject:
    """JSON boundary for the machine-driven link-related FSM projection."""

    return build_link_related_fsm_result_from_model(
        model,
        version_control_safety=version_control_safety,
    ).to_payload()


def link_related_fsm_facts_from_sync_result(
    result: JsonObject,
    *,
    run_id: str,
    mode: Literal["dry_run", "apply", "recover_export"],
    version_control_safety: VersionControlSafety | dict[str, object],
) -> LinkRelatedFsmFacts:
    sync_result = LinkRelatedSyncResult.from_payload(result)
    explicit_error_context = sync_result.error_context
    operation_decision = _LinkRelatedOperationDecisionFields.model_validate(sync_result.operation_payload)
    runtime_facts = _LinkRelatedRuntimeFacts(
        run_id=run_id,
        mode=mode,
        sync_result=sync_result,
        version_control_safety=version_control_safety,
        next_action=sync_result.next_action,
        human_decision_packet=operation_decision.human_decision_packet,
        error_context=(
            explicit_error_context.to_payload()
            if explicit_error_context is not None
            else _link_related_error_context_from_result(sync_result)
        ),
    )
    observation = _link_related_runtime_observation(runtime_facts)
    initial_state = _link_related_runtime_source_state(runtime_facts, observation)
    reason = _link_related_runtime_reason_code(runtime_facts, fallback=_link_related_observation_fallback_reason(observation))
    event = LinkRelatedRuntimeObservedEvent(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=run_id,
        current_state=initial_state.value,
        observation=observation,
        audit_evidence=_link_related_runtime_audit_evidence(runtime_facts, reason),
    )
    changed_files = _link_related_changed_files(sync_result)
    return LinkRelatedFsmFacts(
        run_id=run_id,
        initial_state=initial_state,
        event=event,
        changed_files=changed_files,
        mutated=mode == "apply" and bool(changed_files or sync_result.applied_note_count),
        artifacts=_link_related_artifacts(sync_result),
        version_control_safety=version_control_safety,
        error_context=runtime_facts.error_context,
    )


def _link_related_model_after_event(initial_state: MachineLinkRelatedState, event: WorkflowEventLike) -> WorkflowModel:
    model = WorkflowModel.start(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=event.run_id,
        initial_state=initial_state.value,
    )
    send_workflow_event(
        LinkRelatedMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        event,
    )
    return model


def _link_related_runtime_source_state(
    facts: _LinkRelatedRuntimeFacts,
    observation: LinkRelatedRuntimeObservation,
) -> MachineLinkRelatedState:
    """Choose only the legal source state; LinkRelatedMachine still chooses the leaf."""

    if facts.mode == "apply":
        return MachineLinkRelatedState.APPLYING_RELATED_NOTES
    if facts.mode == "recover_export":
        return MachineLinkRelatedState.STALE_EXPORT
    if observation.blocked and observation.preview_ready:
        return MachineLinkRelatedState.PREVIEW_READY
    return MachineLinkRelatedState.CHECKING_EXPORT


def _link_related_runtime_reason_code(facts: _LinkRelatedRuntimeFacts, *, fallback: str) -> str:
    result = facts.sync_result
    for value in (result.blocked_reason, result.skipped_reason, result.error, result.parse_error):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return fallback


def _link_related_export_missing(facts: _LinkRelatedRuntimeFacts) -> bool:
    return _link_related_runtime_reason_code(facts, fallback="") == "related_notes_export_missing"


def _link_related_export_stale(facts: _LinkRelatedRuntimeFacts) -> bool:
    return _link_related_runtime_reason_code(facts, fallback="") in {
        "related_notes_export_stale",
        "related_notes_export_still_stale",
        "related_notes_hash_mismatch",
        "related_notes_vault_mismatch",
    }


def _link_related_runtime_audit_evidence(
    facts: _LinkRelatedRuntimeFacts,
    reason: str,
) -> JsonObject:
    result = facts.sync_result
    recovery = result.related_notes_recovery_state
    report_operation = _link_related_report_operation(result)
    related_notes_evidence: JsonObject = {
        "status": result.status,
        "blocked_reason": _link_related_runtime_reason_code(facts, fallback=""),
        "selected_recovery_mode": result.selected_recovery_mode,
        "manual_instruction_allowed": result.manual_instruction_allowed,
    }
    for key in ("automatic_recovery_unavailable_reason", "export_relocation"):
        if key in result.operation_payload:
            related_notes_evidence[key] = result.operation_payload[key]
    return JsonObjectAdapter.validate_python(
        {
            "mode": facts.mode,
            "runtime_status": result.status,
            "runtime_phase": result.phase,
            "runtime_reason": reason,
            "related_notes": related_notes_evidence,
            "report_operation": report_operation.model_dump(),
            "counts": {
                "planned_note_count": result.planned_note_count,
                "proposed_link_count": result.proposed_link_count,
                "cleared_link_count": result.cleared_link_count,
                "skipped_edge_count": result.skipped_edge_count,
                "applied_note_count": result.applied_note_count,
                "fresh_record_count": recovery.fresh_record_count,
                "stale_record_count": recovery.stale_record_count,
                "remaining_count": recovery.remaining_count,
            },
            "settings": {
                "min_score": result.min_score,
                "max_links": result.max_links,
            },
        }
    )


def _link_related_report_operation(result: LinkRelatedSyncResult) -> _LinkRelatedReportOperationFields:
    """Project adapter details into report-only facts without making them FSM state."""

    return _LinkRelatedReportOperationFields.model_validate(
        {
            **result.operation_payload,
            "status": result.status,
            "phase": result.phase,
            "blocked_reason": result.blocked_reason,
            "next_action": result.next_action,
            "planned_note_count": result.planned_note_count,
            "proposed_link_count": result.proposed_link_count,
            "cleared_link_count": result.cleared_link_count,
            "skipped_edge_count": result.skipped_edge_count,
            "applied_note_count": result.applied_note_count,
        }
    )


def _validate_link_related_machine_model(model: WorkflowModel) -> None:
    if model.workflow != LINK_RELATED_WORKFLOW:
        raise ValueError(f"link-related FSM projector requires workflow={LINK_RELATED_WORKFLOW}")
    MachineLinkRelatedState(model.state)


def _progress_state_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    category: WorkflowStateCategory,
) -> WorkflowProgressState:
    status = _machine_progress_status(category)
    current, total, counts = _machine_counts(model, state, status)
    return WorkflowProgressState(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=model.run_id,
        state=state.value,
        phase=_machine_phase_for_state(state),
        event_type=_machine_event_type(status),
        message=_machine_message_for_state(state),
        status=status,
        current=current,
        total=total,
        counts=counts,
        resume_action=_machine_resume_action(model, state),
        resume_supported=status
        in {
            WorkflowProgressStatus.WAITING_AGENT,
            WorkflowProgressStatus.WAITING_EXTERNAL,
            WorkflowProgressStatus.WAITING_HUMAN,
            WorkflowProgressStatus.BLOCKED,
        },
        can_continue_now=status
        in {
            WorkflowProgressStatus.RUNNING,
            WorkflowProgressStatus.WAITING_AGENT,
        },
        decision=model.last_transition.decision.decision_summary()
        if model.last_transition is not None and model.last_transition.decision is not None
        else None,
        technical_context=_machine_technical_context(model, state, category),
    )


def _machine_counts(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    status: WorkflowProgressStatus,
) -> tuple[int, int, WorkflowProgressCounts]:
    event = _last_machine_event(model)
    planned = _event_int(event, "planned_note_count")
    fresh = _event_int(event, "fresh_record_count")
    stale = _event_int(event, "stale_record_count")
    remaining = _event_int(event, "remaining_count")
    changed = _event_int(event, "changed_file_count")

    if state == MachineLinkRelatedState.WAITING_EXTERNAL_QUOTA:
        total = fresh + remaining
        return (
            fresh,
            total,
            WorkflowProgressCounts(
                planned_items=total,
                processed_items=fresh,
                remaining_items=remaining,
                blocked_items=remaining,
                deferred_items=remaining,
            ),
        )
    if state == MachineLinkRelatedState.COMPLETED:
        total = max(changed, planned)
        return (
            total,
            total,
            WorkflowProgressCounts(
                planned_items=total,
                processed_items=total,
                mutated_files=changed,
                written_files=changed,
            ),
        )
    if state == MachineLinkRelatedState.PREVIEW_READY:
        return (
            planned,
            planned,
            WorkflowProgressCounts(
                planned_items=planned,
                processed_items=planned,
            ),
        )
    if state == MachineLinkRelatedState.WAITING_HUMAN_CONFIRMATION:
        return (
            0,
            planned,
            WorkflowProgressCounts(
                planned_items=planned,
                remaining_items=planned,
                blocked_items=planned,
            ),
        )
    if state in {MachineLinkRelatedState.EXPORT_REQUIRED, MachineLinkRelatedState.STALE_EXPORT}:
        blocked = max(planned, stale, 1)
        return (
            0,
            blocked,
            WorkflowProgressCounts(
                planned_items=planned,
                remaining_items=blocked,
                blocked_items=blocked,
            ),
        )
    if status in {WorkflowProgressStatus.BLOCKED, WorkflowProgressStatus.FAILED}:
        blocked = max(planned, remaining, stale, 1)
        return (
            0,
            blocked,
            WorkflowProgressCounts(
                planned_items=planned,
                remaining_items=blocked,
                blocked_items=blocked,
            ),
        )
    return 0, planned, WorkflowProgressCounts(planned_items=planned)


def _snapshot_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    category: WorkflowStateCategory,
) -> WorkflowStateMachineSnapshot:
    return WorkflowStateMachineSnapshot(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=model.run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_machine_snapshot_transition(transition) for transition in model.transition_log],
        metadata={"reason": _machine_reason_code(model, state), "source": "LinkRelatedMachine"},
    )


def _machine_snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=category_for_link_related_state(MachineLinkRelatedState(transition.to_state)),
        trigger=transition.trigger,
        effects=list(transition.effects),
        decision=transition.decision,
        resume_action=transition.resume_action,
    )


def _receipt_from_model(
    model: WorkflowModel,
    *,
    progress_state: WorkflowProgressState,
    progress_view_model: WorkflowProgressViewModel,
    snapshot: WorkflowStateMachineSnapshot,
    version_control_safety: VersionControlSafety,
    changed_files: list[str],
    mutated: bool | None,
) -> WorkflowReceiptPayload:
    return WorkflowReceiptPayload(
        schema=LINK_RELATED_RECEIPT_SCHEMA,
        workflow=LINK_RELATED_WORKFLOW,
        run_id=model.run_id,
        status=_machine_receipt_status(progress_state.status),
        mutated=mutated if mutated is not None else version_control_safety.changed_file_count > 0,
        next_action="" if progress_state.status == WorkflowProgressStatus.COMPLETED else progress_state.resume_action,
        human_decision_required=progress_state.status == WorkflowProgressStatus.WAITING_HUMAN,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        changed_files=changed_files,
        version_control_safety=version_control_safety,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
    )


def _reports_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    progress_state: WorkflowProgressState,
) -> WorkflowReports:
    summary = _machine_message_for_state(state)
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=model.run_id,
        headline=summary,
        lines=public_lines,
    )
    related_notes_report = _related_notes_report_from_model(model, state, progress_state)
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details={
            "primary_objective_summary": _primary_objective_summary(model, state, progress_state).to_payload(),
            "related_notes": related_notes_report,
        },
    )


def _primary_objective_summary(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    progress_state: WorkflowProgressState,
) -> WorkflowPrimaryObjectiveSummary:
    """State-owned answer to whether Related Notes were actually updated."""

    completed = state == MachineLinkRelatedState.COMPLETED or (
        state == MachineLinkRelatedState.PREVIEW_READY and progress_state.counts.planned_items == 0
    )
    changed_count = max(progress_state.counts.mutated_files, progress_state.counts.written_files)
    if state == MachineLinkRelatedState.PREVIEW_READY and completed:
        mutation_summary = "Notas Relacionadas conferidas; nenhuma alteração era necessária."
    elif state == MachineLinkRelatedState.PREVIEW_READY:
        mutation_summary = "Prévia de Notas Relacionadas pronta; nada foi alterado ainda."
    elif changed_count > 0:
        mutation_summary = f"{changed_count} nota(s) tiveram Notas Relacionadas atualizadas."
    else:
        mutation_summary = "Nenhuma seção de Notas Relacionadas foi alterada nesta etapa."
    return WorkflowPrimaryObjectiveSummary(
        workflow=LINK_RELATED_WORKFLOW,
        run_id=model.run_id,
        objective="Atualizar a seção Notas Relacionadas a partir do export oficial.",
        completed=completed,
        status=state.value,
        mutation_state="changed" if changed_count > 0 else "unchanged",
        mutation_summary=mutation_summary,
        remaining_work_summary=_link_related_remaining_work_summary(state, completed),
        next_step_summary=_link_related_next_step_summary(progress_state, completed),
        blocked_reason="" if completed else state.value,
    )


def _link_related_remaining_work_summary(state: MachineLinkRelatedState, completed: bool) -> str:
    if completed:
        if state == MachineLinkRelatedState.PREVIEW_READY:
            return "Export conferido; não havia alterações de Notas Relacionadas para aplicar."
        return "Notas Relacionadas foram atualizadas e conferidas."
    if state == MachineLinkRelatedState.PREVIEW_READY:
        return "Ainda falta confirmar/aplicar a prévia para alterar a Wiki."
    return _machine_message_for_state(state)


def _link_related_next_step_summary(progress_state: WorkflowProgressState, completed: bool) -> str:
    if completed:
        return "Nenhuma ação pendente para Notas Relacionadas."
    return progress_state.resume_action or "Retomar /mednotes:link-related pela rota oficial."


def _related_notes_report_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    progress_state: WorkflowProgressState,
) -> JsonObject:
    """Build the human/report projection without making it state truth.

    The state remains owned by LinkRelatedMachine; this report carries the
    adapter's typed operational details so users/tests can audit planned or
    applied Related Notes changes without reintroducing non-FSM root fields.
    """

    evidence = _machine_audit_evidence(model)
    raw_operation = evidence.get("report_operation")
    operation = (
        _LinkRelatedReportOperationFields.model_validate(raw_operation)
        if isinstance(raw_operation, dict)
        else None
    )
    counts = _dict_from(evidence.get("counts")) or progress_state.counts.to_payload()
    settings = _dict_from(evidence.get("settings"))
    report: JsonObject = {
        "schema": "medical-notes-workbench.link-related-machine-report.v1",
        "source": "LinkRelatedMachine",
        "counts": counts,
    }
    if settings:
        report["settings"] = settings
    if operation is not None:
        operation_counts = {
            "planned_note_count": operation.planned_note_count,
            "proposed_link_count": operation.proposed_link_count,
            "cleared_link_count": operation.cleared_link_count,
            "skipped_edge_count": operation.skipped_edge_count,
            "applied_note_count": operation.applied_note_count,
        }
        report["counts"] = {**counts, **operation_counts}
        report["planned_changes"] = {
            "updates": operation.updates,
            "skipped_edges": operation.skipped_edges,
        }
        recovery_state = RelatedNotesRecoveryState.from_payload(operation.related_notes_recovery_state)
        report["related_notes"] = {
            "export_relocation": operation.export_relocation,
            "recovery_progress": {
                "fresh_record_count": recovery_state.fresh_record_count,
                "partial_record_count": recovery_state.partial_record_count,
                "stale_record_count": recovery_state.stale_record_count,
                "record_count": recovery_state.record_count,
                "total_note_count": recovery_state.total_note_count,
                "remaining_count": recovery_state.remaining_count,
                "embedded_count": recovery_state.embedded_count,
                "reused_count": recovery_state.reused_count,
                "attempt_count": recovery_state.attempt_count,
            },
        }
    return JsonObjectAdapter.validate_python(report)


def _dict_from(value: object) -> JsonObject:
    if isinstance(value, dict):
        return JsonObjectAdapter.validate_python(value)
    return {}


def _list_from(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return []


def _int_from(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _diagnostic_context_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    category: WorkflowStateCategory,
) -> JsonObject:
    if category == WorkflowStateCategory.COMPLETED:
        return {}
    context: JsonObject = {
        "schema": "medical-notes-workbench.link-related-fsm-diagnostic-context.v2",
        "state": state.value,
        "category": category.value,
        "reason": _machine_reason_code(model, state),
        "source": "LinkRelatedMachine",
    }
    evidence = _machine_audit_evidence(model)
    for key, value in evidence.items():
        if key == "report_operation":
            continue
        if key not in context:
            context[key] = value
    return diagnostic_context_evidence_only(context)


def _machine_audit_evidence(model: WorkflowModel) -> JsonObject:
    if not model.event_log:
        return {}
    event = _LinkRelatedMachineEventEvidence.model_validate(model.event_log[-1])
    return JsonObjectAdapter.validate_python(event.audit_evidence)


def _machine_technical_context(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    category: WorkflowStateCategory,
) -> JsonObject:
    event = _last_machine_event(model)
    return JsonObjectAdapter.validate_python(
        {
            "reason": _machine_reason_code(model, state),
            "category": category.value,
            "source": "LinkRelatedMachine",
            "trigger": _machine_trigger(model),
            "fresh_record_count": _event_int(event, "fresh_record_count"),
            "stale_record_count": _event_int(event, "stale_record_count"),
            "remaining_count": _event_int(event, "remaining_count"),
            "planned_note_count": _event_int(event, "planned_note_count"),
            "changed_file_count": _event_int(event, "changed_file_count"),
        }
    )


def _machine_progress_status(category: WorkflowStateCategory) -> WorkflowProgressStatus:
    match category:
        case WorkflowStateCategory.PREPARING | WorkflowStateCategory.RUNNING:
            return WorkflowProgressStatus.RUNNING
        case WorkflowStateCategory.WAITING_AGENT:
            return WorkflowProgressStatus.WAITING_AGENT
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return WorkflowProgressStatus.WAITING_EXTERNAL
        case WorkflowStateCategory.WAITING_HUMAN:
            return WorkflowProgressStatus.WAITING_HUMAN
        case WorkflowStateCategory.BLOCKED:
            return WorkflowProgressStatus.BLOCKED
        case WorkflowStateCategory.FAILED:
            return WorkflowProgressStatus.FAILED
        case WorkflowStateCategory.COMPLETED:
            return WorkflowProgressStatus.COMPLETED
        case WorkflowStateCategory.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressStatus.COMPLETED_WITH_WARNINGS


def _machine_receipt_status(status: WorkflowProgressStatus) -> ReceiptStatus:
    match status:
        case WorkflowProgressStatus.RUNNING:
            return "running"
        case WorkflowProgressStatus.COMPLETED:
            return "completed"
        case WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return "completed_with_warnings"
        case WorkflowProgressStatus.WAITING_AGENT:
            return "waiting_agent"
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "waiting_external"
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "waiting_human"
        case WorkflowProgressStatus.FAILED:
            return "failed"
        case WorkflowProgressStatus.BLOCKED:
            return "blocked"
        case _:
            return "blocked"


def _machine_event_type(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    match status:
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case WorkflowProgressStatus.WAITING_HUMAN | WorkflowProgressStatus.BLOCKED:
            return WorkflowProgressEventType.DECISION_EMITTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _machine_phase_for_state(state: MachineLinkRelatedState) -> str:
    match state:
        case MachineLinkRelatedState.CHECKING_EXPORT | MachineLinkRelatedState.EXPORT_REQUIRED:
            return "related_notes_export"
        case MachineLinkRelatedState.STALE_EXPORT:
            return "related_notes_export_recovery"
        case MachineLinkRelatedState.PREVIEW_READY | MachineLinkRelatedState.WAITING_HUMAN_CONFIRMATION:
            return "related_notes_preview"
        case MachineLinkRelatedState.APPLYING_RELATED_NOTES | MachineLinkRelatedState.COMPLETED:
            return "related_notes_apply"
        case MachineLinkRelatedState.WAITING_EXTERNAL_QUOTA:
            return "related_notes_recovery"
        case MachineLinkRelatedState.APPLY_CANCELLED:
            return "related_notes_apply_cancelled"
        case MachineLinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED:
            return "related_notes_export_blocked"
        case MachineLinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED:
            return "related_notes_preview_blocked"
        case MachineLinkRelatedState.RELATED_NOTES_APPLY_BLOCKED:
            return "related_notes_apply_blocked"
        case MachineLinkRelatedState.FAILED:
            return "related_notes_failed"


def _machine_message_for_state(state: MachineLinkRelatedState) -> str:
    match state:
        case MachineLinkRelatedState.EXPORT_REQUIRED:
            return "Export do Related Notes precisa ser gerado antes da sincronização."
        case MachineLinkRelatedState.STALE_EXPORT:
            return "Export do Related Notes ficou desatualizado."
        case MachineLinkRelatedState.PREVIEW_READY:
            return "Prévia das Notas Relacionadas pronta; nada foi alterado."
        case MachineLinkRelatedState.WAITING_HUMAN_CONFIRMATION:
            return "Preciso de confirmação antes de atualizar Notas Relacionadas."
        case MachineLinkRelatedState.APPLYING_RELATED_NOTES:
            return "Atualização das Notas Relacionadas está em execução."
        case MachineLinkRelatedState.WAITING_EXTERNAL_QUOTA:
            return "Notas Relacionadas aguardam cota externa para continuar."
        case MachineLinkRelatedState.COMPLETED:
            return "Notas Relacionadas atualizadas e conferidas."
        case MachineLinkRelatedState.APPLY_CANCELLED:
            return "Atualização das Notas Relacionadas cancelada antes de alterar o vault."
        case MachineLinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED:
            return "Export das Notas Relacionadas bloqueado."
        case MachineLinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED:
            return "Prévia das Notas Relacionadas bloqueada."
        case MachineLinkRelatedState.RELATED_NOTES_APPLY_BLOCKED:
            return "Aplicação das Notas Relacionadas bloqueada."
        case MachineLinkRelatedState.FAILED:
            return "Notas Relacionadas falharam antes de concluir."
        case _:
            return "Workflow de Notas Relacionadas em andamento."


def _machine_resume_action(model: WorkflowModel, state: MachineLinkRelatedState) -> str:
    if state == MachineLinkRelatedState.COMPLETED:
        return ""
    if model.last_transition is not None and model.last_transition.resume_action:
        return model.last_transition.resume_action
    match state:
        case MachineLinkRelatedState.EXPORT_REQUIRED | MachineLinkRelatedState.STALE_EXPORT:
            return "link-related:recover-export"
        case MachineLinkRelatedState.WAITING_HUMAN_CONFIRMATION:
            return "link-related:confirm-apply"
        case MachineLinkRelatedState.APPLYING_RELATED_NOTES:
            return "link-related:apply"
        case MachineLinkRelatedState.WAITING_EXTERNAL_QUOTA:
            return "link-related:retry-export"
        case (
            MachineLinkRelatedState.APPLY_CANCELLED
            | MachineLinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED
            | MachineLinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED
            | MachineLinkRelatedState.RELATED_NOTES_APPLY_BLOCKED
            | MachineLinkRelatedState.FAILED
        ):
            return "link-related:diagnose"
        case _:
            return ""


def _machine_reason_code(model: WorkflowModel, state: MachineLinkRelatedState) -> str:
    if model.last_transition is not None:
        return model.last_transition.reason_code
    return state.value


def _machine_trigger(model: WorkflowModel) -> str:
    if model.last_transition is not None:
        return model.last_transition.trigger
    return ""


def _machine_blockers(
    category: WorkflowStateCategory,
    model: WorkflowModel,
    state: MachineLinkRelatedState,
) -> list[str]:
    if category in {
        WorkflowStateCategory.WAITING_AGENT,
        WorkflowStateCategory.WAITING_EXTERNAL,
        WorkflowStateCategory.WAITING_HUMAN,
        WorkflowStateCategory.BLOCKED,
        WorkflowStateCategory.FAILED,
    }:
        return [_machine_reason_code(model, state)]
    return []


def _error_context_from_model(
    model: WorkflowModel,
    state: MachineLinkRelatedState,
    category: WorkflowStateCategory,
) -> JsonObject:
    """Synthesize recovery context from the LinkRelatedMachine leaf state."""

    if category not in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return {}
    reason = _machine_reason_code(model, state) or state.value
    return JsonObjectAdapter.validate_python(
        {
            "blocked_reason": reason,
            "root_cause": reason,
            "affected_artifact": state.value,
            "next_action": _machine_resume_action(model, state) or "link-related:diagnose",
            "retry_scope": "link-related",
        }
    )


def _machine_agent_instructions(category: WorkflowStateCategory) -> list[str]:
    if category == WorkflowStateCategory.WAITING_AGENT:
        return ["Execute somente os efeitos em agent_directive.control.effects e retome /mednotes:link-related pelo resultado tipado."]
    if category == WorkflowStateCategory.WAITING_EXTERNAL:
        return ["Aguarde a condição externa indicada antes de retomar /mednotes:link-related."]
    if category == WorkflowStateCategory.WAITING_HUMAN:
        return ["Peça a decisão humana fechada antes de atualizar Notas Relacionadas."]
    if category in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return ["Use a decisão e o resume_action da FSM para recuperar /mednotes:link-related."]
    return ["Use a LinkRelatedMachine como fonte de verdade do estado de Notas Relacionadas."]


def _last_machine_event(model: WorkflowModel) -> JsonObject | None:
    if not model.event_log:
        return None
    return model.event_log[-1]


class _LinkRelatedMachineEventEvidence(ContractModel):
    """Typed lens over persisted link-related machine event evidence."""

    model_config = ConfigDict(extra="ignore")

    audit_evidence: JsonObject = Field(default_factory=dict)


def _event_int(event: object, field_name: str) -> int:
    if isinstance(event, dict):
        if field_name in event:
            value = event[field_name]
        elif "observation" in event and isinstance(event["observation"], dict):
            observation = LinkRelatedRuntimeObservation.model_validate(event["observation"])
            value = getattr(observation, field_name, 0)
        else:
            value = 0
    else:
        value = getattr(event, field_name, 0) if event is not None else 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _version_control_safety(value: VersionControlSafety | dict[str, object]) -> VersionControlSafety:
    if isinstance(value, VersionControlSafety):
        return value
    return VersionControlSafety.model_validate(value)


def _link_related_runtime_observation(facts: _LinkRelatedRuntimeFacts) -> LinkRelatedRuntimeObservation:
    """Convert adapter output to facts; the machine owns final state priority."""

    result = facts.sync_result
    recovery = result.related_notes_recovery_state
    reason = _link_related_runtime_reason_code(facts, fallback="")
    current = _fresh_current(recovery)
    total = recovery.total_note_count
    return LinkRelatedRuntimeObservation(
        mode=facts.mode,
        failed=_link_related_observed_failed(facts),
        export_missing=reason == "related_notes_export_missing",
        export_stale=reason
        in {
            "related_notes_export_stale",
            "related_notes_export_still_stale",
            "related_notes_hash_mismatch",
            "related_notes_vault_mismatch",
        },
        preview_ready=facts.mode != "apply" and result.status in {"preview_ready", "completed", "recovered"},
        applied=facts.mode == "apply" and result.status == "completed",
        blocked=result.status == "blocked" or bool(reason and not _link_related_waiting_external_from_recovery(result)),
        waiting_external=_link_related_waiting_external_from_recovery(result),
        planned_note_count=result.planned_note_count,
        proposed_link_count=result.proposed_link_count,
        cleared_link_count=result.cleared_link_count,
        applied_note_count=result.applied_note_count,
        fresh_record_count=current,
        stale_record_count=recovery.stale_record_count,
        remaining_count=_remaining_count(recovery, current=current, total=total),
        reason_code=reason,
        next_action=_link_related_default_next_action(facts, reason or result.status or "related_notes"),
        export_path=result.export_path or result.default_export_name,
        related_notes_recovery_state=recovery.to_payload(),
    )


def _link_related_observed_failed(facts: _LinkRelatedRuntimeFacts) -> bool:
    result = facts.sync_result
    return bool(result.error.strip() or result.parse_error.strip() or result.status == "failed")


def _link_related_waiting_external_from_recovery(result: LinkRelatedSyncResult) -> bool:
    recovery = result.related_notes_recovery_state
    reason = result.blocked_reason or recovery.blocked_reason
    return (
        result.status == "blocked"
        and recovery.status == "waiting_for_retry"
        and reason
        in {
            "related_notes_headless_quota_exhausted",
            "related_notes_headless_time_budget_exhausted",
        }
    )


def _link_related_observation_fallback_reason(observation: LinkRelatedRuntimeObservation) -> str:
    if observation.failed:
        return "related_notes_failed"
    if observation.export_missing:
        return "related_notes_export_missing"
    if observation.export_stale:
        return "related_notes_export_stale"
    if observation.waiting_external:
        return "related_notes_quota_wait"
    if observation.blocked:
        return "related_notes_blocked"
    if observation.applied:
        return "completed"
    if observation.preview_ready:
        return "preview_ready"
    return "related_notes"


def _link_related_default_next_action(facts: _LinkRelatedRuntimeFacts, reason: str) -> str:
    if facts.next_action.strip():
        return facts.next_action.strip()
    result_next = facts.sync_result.next_action.strip()
    if result_next:
        return result_next
    match reason:
        case "preview_ready":
            if facts.sync_result.planned_note_count == 0:
                return ""
            return "Revisar a prévia e confirmar a atualização das Notas Relacionadas."
        case "waiting_external_related_notes":
            return "Aguardar a condição externa e retomar /mednotes:link-related pela rota oficial."
        case "related_notes_blocked":
            return "Corrigir o bloqueio informado e repetir /mednotes:link-related pela rota oficial."
        case "failed":
            return "Revisar o erro e retomar /mednotes:link-related pela rota oficial."
        case "completed" | "recovered":
            return ""
    raise AssertionError(f"unsupported link-related reason: {reason}")


def _link_related_artifacts(sync_result: LinkRelatedSyncResult) -> JsonObject:
    artifacts: JsonObject = {}
    if sync_result.export_path:
        artifacts["export_path"] = sync_result.export_path
    if sync_result.receipt_path:
        artifacts["receipt_path"] = sync_result.receipt_path
    return artifacts


def _link_related_changed_files(sync_result: LinkRelatedSyncResult) -> list[str]:
    return [
        update.path
        for update in sync_result.updates
        if update.changed and update.path
    ]


def _link_related_error_context_from_result(result: LinkRelatedSyncResult) -> JsonObject:
    evidence = _LinkRelatedErrorPayloadFields.model_validate(result.operation_payload)
    blocked_reason = (
        result.blocked_reason.strip()
        or result.skipped_reason.strip()
        or result.error.strip()
        or result.parse_error.strip()
    )
    if not blocked_reason and not evidence.has_error_detail:
        return {}
    next_action = result.next_action or "Revisar o erro e retomar /mednotes:link-related pela rota oficial."
    context: JsonObject = {
        "blocked_reason": blocked_reason,
        "root_cause": blocked_reason or "related_notes_error",
        "affected_artifact": "related_notes_export",
        "next_action": next_action,
    }
    for key in (
        "validation_errors",
        "contract_errors",
        "hash_errors",
        "stale_notes",
        "forbidden_keys",
        "detail",
        "selected_recovery_mode",
        "command_returncode",
        "error",
        "parse_error",
        "skipped_reason",
    ):
        value = getattr(evidence, key)
        if value not in (None, "", [], {}):
            context[key] = value
    return JsonObjectAdapter.validate_python(context)


def assert_link_related_fsm_payload(payload: JsonObject) -> None:
    payload = JsonObjectAdapter.validate_python(payload)
    forbidden_keys = set(payload) & LINK_RELATED_FORBIDDEN_ROOT_KEYS
    if forbidden_keys:
        raise ValueError(f"link-related FSM payload contains non-FSM root fields: {sorted(forbidden_keys)}")
    required_root_keys = LINK_RELATED_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing_keys = required_root_keys - set(payload)
    if missing_keys:
        raise ValueError(f"link-related FSM payload missing canonical root fields: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - LINK_RELATED_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"link-related FSM payload contains unexpected root fields: {sorted(unexpected_keys)}")
    fields = _link_related_payload_fields(payload)
    assert_diagnostic_context_evidence_only(fields.diagnostic_context)
    if "agent_directive" in fields.diagnostic_context:
        raise ValueError("link-related FSM diagnostic_context must not contain agent_directive")
    if fields.workflow != LINK_RELATED_WORKFLOW:
        raise ValueError("link-related FSM payload has invalid workflow")
    if fields.progress_view_model.status != fields.state_machine_snapshot.current_category:
        raise ValueError("link-related FSM status must match state_machine_snapshot category")
    if fields.receipt.status != fields.progress_view_model.status:
        raise ValueError("link-related FSM receipt status must match progress view status")
    if fields.progress_view_model.status in {
        WorkflowStateCategory.BLOCKED.value,
        WorkflowStateCategory.FAILED.value,
    } and not payload["error_context"]:
        raise ValueError("link-related FSM blocked/failed payload requires error_context")
    reports_model = WorkflowReports.model_validate(payload["reports"])
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    assert_public_report_matches_progress(
        reports_model.public_report,
        workflow=LINK_RELATED_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="link-related FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(payload[LINK_RELATED_AGENT_DIRECTIVE_FIELD]),
        workflow=LINK_RELATED_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="link-related FSM",
    )
    _assert_link_related_snapshot(snapshot)


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """Keep Related Notes recovery effects tied to the current FSM lane."""

    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def _assert_link_related_snapshot(snapshot: WorkflowStateMachineSnapshot) -> None:
    if snapshot.workflow != LINK_RELATED_WORKFLOW:
        raise ValueError("link-related FSM snapshot has invalid workflow")
    if snapshot.current_category != category_for_state(snapshot.current_state):
        raise ValueError("link-related FSM snapshot category does not match state")
    edges = _link_related_machine_edges()
    for transition in snapshot.transitions:
        if transition.to_category != category_for_state(transition.to_state):
            raise ValueError("link-related FSM transition category does not match state")
        edge = (transition.trigger, transition.from_state, transition.to_state)
        if edge not in edges:
            raise ValueError(f"unauthorized FSM transition: {edge}")


def _link_related_machine_edges() -> set[tuple[str, str, str]]:
    """Return every transition edge declared by the canonical LinkRelatedMachine."""

    edges: set[tuple[str, str, str]] = set()
    for event in LinkRelatedMachine.events:
        for transition in event._transitions:
            for target in transition._targets:
                edges.add((event.id, str(transition.source.value), str(target.value)))
    return edges


def _link_related_payload_fields(payload: JsonObject) -> _LinkRelatedPayloadFields:
    raw_fields: JsonObject = {
        "workflow": payload["workflow"],
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status",)),
        "state_machine_snapshot": _json_object_subset(payload, "state_machine_snapshot", ("current_category",)),
        "receipt": _json_object_subset(payload, "receipt", ("status",)),
        "diagnostic_context": payload["diagnostic_context"] if "diagnostic_context" in payload else {},
    }
    try:
        return _LinkRelatedPayloadFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"link-related FSM payload invalid: {loc}: {msg}") from exc


def _json_object_subset(payload: JsonObject, field_name: str, keys: tuple[str, ...]) -> JsonObject:
    try:
        source = JsonObjectAdapter.validate_python(payload[field_name])
    except PydanticValidationError as exc:
        raise ValueError(f"link-related FSM payload invalid: {field_name} must be an object") from exc
    return {key: source[key] for key in keys if key in source}


def link_related_cli_exit_code(payload: JsonObject) -> int:
    progress = _LinkRelatedPayloadProgressViewFields.model_validate(
        _json_object_subset(payload, "progress_view_model", ("status", "state"))
    )
    status = progress.status
    match status:
        case "completed" | "completed_with_warnings":
            return 0
        case "waiting_human" if progress.state in {
            MachineLinkRelatedState.PREVIEW_READY.value,
            MachineLinkRelatedState.WAITING_HUMAN_CONFIRMATION.value,
        }:
            return 0
        case "waiting_agent" | "waiting_external" | "waiting_human" | "blocked":
            return 3
        case "failed":
            return 5
        case _:
            return 1


class RelatedNotesRecoveryMachineState(StrEnum):
    """Small StateChart used when Related Notes recovery is embedded by a parent workflow."""

    RECOVERING_RELATED_NOTES = _RECOVERING_STATE
    WAITING_EXTERNAL_QUOTA = _WAITING_QUOTA_STATE
    RELATED_NOTES_RECOVERY_BLOCKED = _RECOVERY_BLOCKED_STATE


class RelatedNotesRecoveryEvent(ContractModel):
    """Base event for the embedded Related Notes recovery StateChart."""

    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)


class RelatedNotesRecoveryQuotaWaitEvent(RelatedNotesRecoveryEvent):
    name: Literal["related_notes_quota_wait"] = "related_notes_quota_wait"
    reason_code: str = Field(default="related_notes_quota_wait", min_length=1)
    next_action: str = Field(min_length=1)
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload:
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class RelatedNotesRecoveryQuotaReadyEvent(RelatedNotesRecoveryEvent):
    name: Literal["related_notes_quota_ready"] = "related_notes_quota_ready"
    restored_by: str = Field(min_length=1)


class RelatedNotesRecoveryBlockedEvent(RelatedNotesRecoveryEvent):
    name: Literal["related_notes_recovery_blocked"] = "related_notes_recovery_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


RelatedNotesRecoveryBoundaryEvent = Annotated[
    RelatedNotesRecoveryQuotaWaitEvent | RelatedNotesRecoveryQuotaReadyEvent | RelatedNotesRecoveryBlockedEvent,
    Field(discriminator="name"),
]


class RelatedNotesRecoveryMachine(StateChart[WorkflowModel]):
    """StateChart for resumable Related Notes recovery embedded in parent workflows."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        RelatedNotesRecoveryMachineState,
        initial=RelatedNotesRecoveryMachineState.RECOVERING_RELATED_NOTES,
        final={RelatedNotesRecoveryMachineState.RELATED_NOTES_RECOVERY_BLOCKED},
        use_enum_instance=False,
    )

    related_notes_quota_wait = states.RECOVERING_RELATED_NOTES.to(
        states.WAITING_EXTERNAL_QUOTA,
        on="_on_quota_wait",
    )
    related_notes_quota_ready = states.WAITING_EXTERNAL_QUOTA.to(
        states.RECOVERING_RELATED_NOTES,
        on="_on_transition",
    )
    related_notes_recovery_blocked = states.RECOVERING_RELATED_NOTES.to(
        states.RELATED_NOTES_RECOVERY_BLOCKED,
        on="_on_blocked",
    )

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return _category_for_recovery_state(RelatedNotesRecoveryMachineState(state))

    def _on_transition(
        self,
        workflow_event: RelatedNotesRecoveryEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        return _recovery_transition(
            workflow_event,
            _recovery_target_state(target),
            reason_code=str(getattr(workflow_event, "name", "")),
        )

    def _on_quota_wait(
        self,
        workflow_event: RelatedNotesRecoveryQuotaWaitEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _recovery_target_state(target)
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="related-notes-recovery-quota-wait",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.WAIT_EXTERNAL,
            target="related_notes.quota",
            payload=WaitExternalEffectPayload(
                related_notes_recovery_state=workflow_event.related_notes_recovery_state,
                next_action=workflow_event.next_action,
            ).to_payload(),
            requires_receipt=False,
            no_resource_mutation=True,
            resume_action=workflow_event.next_action,
        )
        return _recovery_transition(
            workflow_event,
            to_state,
            reason_code=workflow_event.reason_code,
            effects=[effect],
            resume_action=workflow_event.next_action,
        )

    def _on_blocked(
        self,
        workflow_event: RelatedNotesRecoveryBlockedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _recovery_target_state(target)
        return _recovery_transition(
            workflow_event,
            to_state,
            reason_code=workflow_event.reason_code,
            decision=_hard_block_decision(
                reason_code=workflow_event.reason_code,
                next_action=workflow_event.next_action,
            ),
        )


def _category_for_recovery_state(state: RelatedNotesRecoveryMachineState) -> WorkflowStateCategory:
    match state:
        case RelatedNotesRecoveryMachineState.RECOVERING_RELATED_NOTES:
            return WorkflowStateCategory.RUNNING
        case RelatedNotesRecoveryMachineState.WAITING_EXTERNAL_QUOTA:
            return WorkflowStateCategory.WAITING_EXTERNAL
        case RelatedNotesRecoveryMachineState.RELATED_NOTES_RECOVERY_BLOCKED:
            return WorkflowStateCategory.BLOCKED


def _recovery_target_state(target: object) -> RelatedNotesRecoveryMachineState:
    value = getattr(target, "value", target)
    return RelatedNotesRecoveryMachineState(str(value))


def _recovery_transition(
    workflow_event: RelatedNotesRecoveryEvent,
    to_state: RelatedNotesRecoveryMachineState,
    *,
    reason_code: str,
    effects: list[WorkflowEffect] | None = None,
    decision: WorkflowDecision | None = None,
    resume_action: str = "",
) -> WorkflowTransitionResult:
    return WorkflowTransitionResult(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        from_state=workflow_event.current_state,
        to_state=to_state.value,
        trigger=str(getattr(workflow_event, "name", "")),
        reason_code=reason_code,
        effects=list(effects or []),
        decision=decision,
        resume_action=resume_action,
    )


def _related_notes_recovery_model_after_event(
    *,
    workflow: str,
    run_id: str,
    event: RelatedNotesRecoveryBoundaryEvent,
) -> WorkflowModel:
    model = WorkflowModel.start(
        workflow=workflow,
        run_id=run_id,
        initial_state=RelatedNotesRecoveryMachineState.RECOVERING_RELATED_NOTES.value,
    )
    send_workflow_event(
        RelatedNotesRecoveryMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        event,
    )
    return model


def _recovery_snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=_category_for_recovery_state(RelatedNotesRecoveryMachineState(transition.to_state)),
        trigger=transition.trigger,
        effects=list(transition.effects),
        decision=transition.decision,
        resume_action=transition.resume_action,
    )


@dataclass(frozen=True)
class RelatedNotesRecoveryMachineProjection:
    """Recovery progress lens derived only from `RelatedNotesRecoveryMachine`.

    The input is typed Related Notes recovery evidence, and the carrier state is
    the recovery StateChart. This object serializes that machine view; it does
    not define a parallel recovery status.
    """

    progress_state: WorkflowProgressState
    progress_view_model: WorkflowProgressViewModel
    snapshot: WorkflowStateMachineSnapshot

    def to_payload(self) -> JsonObject:
        return JsonObjectAdapter.validate_python(
            {
                "progress_view_model": self.progress_view_model.to_payload(),
                "state_machine_snapshot": self.snapshot.to_payload(),
            }
        )


def build_related_notes_recovery_projection(
    *,
    workflow: str,
    run_id: str,
    recovery_state: object,
    next_action: str,
) -> RelatedNotesRecoveryMachineProjection:
    typed_recovery = RelatedNotesRecoveryState.from_payload(recovery_state)
    reason_code = typed_recovery.blocked_reason or "related_notes_recovery_blocked"
    waiting_external = typed_recovery.status == "waiting_for_retry"
    current = _fresh_current(typed_recovery)
    total = typed_recovery.total_note_count
    remaining = _remaining_count(typed_recovery, current=current, total=total)
    if total and current > total:
        current = max(0, total - remaining) if remaining else total
    counts = WorkflowProgressCounts(
        planned_items=total,
        processed_items=current,
        cache_hits=typed_recovery.reused_count,
        api_calls=typed_recovery.embedded_count,
        remaining_items=remaining,
        blocked_items=0 if waiting_external else remaining or total,
    )

    if waiting_external:
        event: RelatedNotesRecoveryBoundaryEvent = RelatedNotesRecoveryQuotaWaitEvent(
            workflow=workflow,
            run_id=run_id,
            current_state=RelatedNotesRecoveryMachineState.RECOVERING_RELATED_NOTES.value,
            reason_code=reason_code,
            next_action=next_action,
            related_notes_recovery_state=typed_recovery,
        )
        resume_supported = typed_recovery.resume_supported
        can_continue_now = False
    else:
        event = RelatedNotesRecoveryBlockedEvent(
            workflow=workflow,
            run_id=run_id,
            current_state=RelatedNotesRecoveryMachineState.RECOVERING_RELATED_NOTES.value,
            reason_code=reason_code,
            next_action=next_action,
        )
        resume_supported = False
        can_continue_now = False

    model = _related_notes_recovery_model_after_event(workflow=workflow, run_id=run_id, event=event)
    state = RelatedNotesRecoveryMachineState(model.state)
    category = _category_for_recovery_state(state)
    status = _machine_progress_status(category)
    event_type = (
        WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        if status == WorkflowProgressStatus.WAITING_EXTERNAL
        else WorkflowProgressEventType.DECISION_EMITTED
    )
    transition = model.last_transition
    if transition is None:
        raise ValueError("related-notes recovery event did not produce a machine transition")
    decision = transition.decision
    resume_action = transition.resume_action

    progress_state = WorkflowProgressState(
        workflow=workflow,
        run_id=run_id,
        state=state.value,
        phase=_PHASE,
        event_type=event_type,
        message=_message_for(waiting_external=waiting_external, reason_code=reason_code),
        status=status,
        current=current,
        total=total,
        counts=counts,
        resume_action=resume_action,
        resume_supported=resume_supported,
        can_continue_now=can_continue_now,
        decision=decision.decision_summary() if decision is not None else None,
        technical_context={
            "recovery_state_status": typed_recovery.status,
            "blocked_reason": reason_code,
            "attempt_count": typed_recovery.attempt_count,
            "fresh_record_count": typed_recovery.fresh_record_count,
            "stale_record_count": typed_recovery.stale_record_count,
            "total_note_count": total,
            "remaining_count": remaining,
        },
    )
    snapshot = WorkflowStateMachineSnapshot(
        workflow=workflow,
        run_id=run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_recovery_snapshot_transition(item) for item in model.transition_log],
        metadata={
            "source": "RelatedNotesRecoveryMachine",
            "recovery_state_schema": typed_recovery.schema_id,
            "blocked_reason": reason_code,
        },
    )

    return RelatedNotesRecoveryMachineProjection(
        progress_state=progress_state,
        progress_view_model=build_progress_view_model(progress_state),
        snapshot=snapshot,
    )


def _hard_block_decision(*, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind="hard_block",
        phase=_PHASE,
        reason_code=reason_code,
        public_summary="A recuperacao do Related Notes esta bloqueada antes de alterar a Wiki.",
        developer_summary="Related Notes recovery emitted a hard block before vault mutation.",
        evidence=[
            DecisionEvidence(
                summary="A recuperacao informou bloqueio operacional.",
                technical_code=reason_code,
                source="related_notes_recovery_state",
            )
        ],
        next_action=next_action,
    )


def _fresh_current(payload: RelatedNotesRecoveryState | dict[str, object]) -> int:
    state = RelatedNotesRecoveryState.from_payload(payload)
    return state.fresh_record_count or state.partial_record_count


def _remaining_count(payload: RelatedNotesRecoveryState | dict[str, object], *, current: int, total: int) -> int:
    value = RelatedNotesRecoveryState.from_payload(payload).remaining_count
    if value:
        return min(value, total) if total else value
    return max(0, total - current)


def _message_for(*, waiting_external: bool, reason_code: str) -> str:
    if waiting_external:
        if reason_code == "related_notes_headless_quota_exhausted":
            return "Related Notes aguardando cota externa para retomar pela rota oficial."
        if reason_code == "related_notes_headless_time_budget_exhausted":
            return "Related Notes pausou a indexação para evitar uma execução longa; a próxima tentativa retoma do índice parcial."
        return "Related Notes aguardando condição externa para retomar pela rota oficial."
    return f"Related Notes bloqueado: {reason_code}."
