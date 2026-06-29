"""Public FSM projection for `/mednotes:history`.

`HistoryMachine` owns the operational state. This module only projects that
state into the public workflow contract used by hooks, agents, and human-facing
reports; it does not execute restore IO or infer policy from adapter text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.history.history_machine import (
    HistoryBlockedEvent,
    HistoryFailedEvent,
    HistoryMachine,
    HistoryState,
    PreviewRequiresConfirmationEvent,
    RestoreAppliedEvent,
    RestorePointsListedEvent,
    StaleRestorePointDetectedEvent,
    category_for_history_state,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffectKind
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
    VersionControlSafety,
    WorkflowDecision,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

HISTORY_WORKFLOW = "/mednotes:history"
HISTORY_FSM_SCHEMA = "medical-notes-workbench.history-fsm-result.v1"
HISTORY_AGENT_DIRECTIVE_FIELD = "agent_directive"


class _HistoryMachineEventEvidence(ContractModel):
    """Typed lens over persisted history machine event evidence."""

    model_config = ConfigDict(extra="ignore")

    audit_evidence: JsonObject = Field(default_factory=dict)


HISTORY_ALLOWED_ROOT_KEYS = frozenset(
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
HISTORY_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "workflow_exit_code",
    }
)


class HistoryVaultOutcome(StrEnum):
    """Canonical outcome for the vault history/restore adapter boundary."""

    RESTORE_POINTS_LISTED = "restore_points_listed"
    PREVIEW_READY = "preview_ready"
    RESTORE_APPLIED = "restore_applied"
    STALE_RESTORE_POINT = "stale_restore_point"
    BLOCKED = "blocked"
    FAILED = "failed"


class HistoryReceipt(ContractModel):
    """Receipt projection for history states, including non-terminal running states."""

    schema_id: Literal["medical-notes-workbench.history-receipt.v1"] = Field(
        default="medical-notes-workbench.history-receipt.v1",
        alias="schema",
    )
    workflow: Literal["/mednotes:history"] = HISTORY_WORKFLOW
    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    mutated: bool = False
    next_action: str = ""
    human_decision_required: bool = False
    restored_file_count: int = Field(default=0, ge=0)
    version_control_safety: VersionControlSafety


class HistoryFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.history-fsm-result.v1"] = Field(
        default=HISTORY_FSM_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/mednotes:history"] = HISTORY_WORKFLOW
    run_id: str = Field(min_length=1)
    state_machine_snapshot: WorkflowStateMachineSnapshot
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: HistoryReceipt
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
    def _progress_view_model_matches_state(self) -> HistoryFsmResult:
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
        assert_history_fsm_payload(payload)
        return payload


def assert_history_fsm_payload(payload: JsonObject) -> None:
    """Gate the public history FSM payload against legacy root truth."""

    payload = JsonObjectAdapter.validate_python(payload)
    legacy_keys = set(payload) & HISTORY_FORBIDDEN_ROOT_KEYS
    if legacy_keys:
        raise ValueError(f"history FSM payload contains legacy root keys: {sorted(legacy_keys)}")
    required_keys = HISTORY_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing_keys = required_keys - set(payload)
    if missing_keys:
        raise ValueError(f"history FSM payload missing canonical root keys: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - HISTORY_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"history FSM payload contains unexpected root keys: {sorted(unexpected_keys)}")
    diagnostic_context = payload["diagnostic_context"] if "diagnostic_context" in payload else {}
    assert_diagnostic_context_evidence_only(diagnostic_context)
    if isinstance(diagnostic_context, dict) and "agent_directive" in diagnostic_context:
        raise ValueError("history FSM diagnostic_context must not contain agent_directive")
    reports_payload = JsonObjectAdapter.validate_python(payload["reports"])
    if "human" in reports_payload:
        raise ValueError("history FSM reports must not expose legacy human report text")
    reports = WorkflowReports.model_validate(reports_payload)
    public_report = reports.public_report
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    receipt = HistoryReceipt.model_validate(payload["receipt"])
    if progress_view_model.status != snapshot.current_category.value:
        raise ValueError("history FSM status must match state_machine_snapshot category")
    if receipt.status != progress_view_model.status:
        raise ValueError("history FSM receipt status must match progress view status")
    assert_public_report_matches_progress(
        public_report,
        workflow=HISTORY_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="history FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(payload[HISTORY_AGENT_DIRECTIVE_FIELD]),
        workflow=HISTORY_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="history FSM",
    )


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """History has no hidden executable adapter effect in its public directive."""

    match category:
        case WorkflowStateCategory.RUNNING:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def history_fsm_payload_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> JsonObject:
    """JSON boundary for the machine-driven history FSM projection."""

    return build_history_fsm_result_from_model(
        model,
        version_control_safety=version_control_safety,
    ).to_payload()


class _VaultHistoryPayload(BaseModel):
    """Typed adapter lens for vault history/restore payloads."""

    model_config = ConfigDict(extra="ignore", strict=True)

    schema_id: str = Field(default="", alias="schema")
    adapter_status: str = Field(default="", alias="status")
    adapter_outcome: HistoryVaultOutcome = HistoryVaultOutcome.FAILED
    reason_code: str = ""
    restore_mutated: bool = False
    count: int = Field(default=0, ge=0)
    plan_path: str = ""
    affected_files: list[str] = Field(default_factory=list)
    restored_file_count: int = Field(default=0, ge=0)
    next_action: str = ""
    human_message: str = ""

    @model_validator(mode="after")
    def _derive_adapter_outcome(self) -> _VaultHistoryPayload:
        status = self.adapter_status
        object.__setattr__(self, "reason_code", status or "history_failed")
        object.__setattr__(
            self,
            "restore_mutated",
            self.schema_id == "medical-notes-workbench.vault-restore-apply.v1" and status == "restored",
        )
        if self.schema_id == "medical-notes-workbench.vault-timeline.v1" and status == "completed":
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.RESTORE_POINTS_LISTED)
        elif self.schema_id == "medical-notes-workbench.vault-restore-plan.v1" and status == "preview_ready":
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.PREVIEW_READY)
        elif self.schema_id == "medical-notes-workbench.vault-restore-apply.v1" and status in {
            "restored",
            "no_changes",
        }:
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.RESTORE_APPLIED)
        elif status == "blocked_stale_preview":
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.STALE_RESTORE_POINT)
            object.__setattr__(self, "reason_code", "stale_restore_point")
        elif status.startswith("blocked"):
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.BLOCKED)
        else:
            object.__setattr__(self, "adapter_outcome", HistoryVaultOutcome.FAILED)
        return self


def history_fsm_payload_from_vault_payload(
    payload: object,
    *,
    run_id: str = "history-vault",
    version_control_safety: VersionControlSafety | dict[str, object] | None = None,
) -> JsonObject:
    """Convert a vault history adapter result into the public history FSM payload."""

    raw = _VaultHistoryPayload.model_validate(payload)
    initial_state, event = _history_event_from_vault_payload(raw, run_id=run_id)
    model = WorkflowModel.start(workflow=HISTORY_WORKFLOW, run_id=run_id, initial_state=initial_state.value)
    send_workflow_event(HistoryMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD), event)
    return build_history_fsm_result_from_model(
        model,
        version_control_safety=version_control_safety or {"no_resource_mutation": not raw.restore_mutated},
    ).to_payload()


def _history_event_from_vault_payload(
    payload: _VaultHistoryPayload,
    *,
    run_id: str,
) -> tuple[HistoryState, WorkflowEventLike]:
    if payload.adapter_outcome == HistoryVaultOutcome.RESTORE_POINTS_LISTED:
        state = HistoryState.LISTING_RESTORE_POINTS
        return (
            state,
            RestorePointsListedEvent(
                workflow=HISTORY_WORKFLOW,
                run_id=run_id,
                current_state=state.value,
                restore_point_count=payload.count,
            ),
        )
    if payload.adapter_outcome == HistoryVaultOutcome.PREVIEW_READY:
        state = HistoryState.PREVIEW_READY
        return (
            state,
            PreviewRequiresConfirmationEvent(
                workflow=HISTORY_WORKFLOW,
                run_id=run_id,
                current_state=state.value,
                restore_preview_path=payload.plan_path or "vault-restore-plan",
                affected_file_count=max(1, len(payload.affected_files)),
            ),
        )
    if payload.adapter_outcome == HistoryVaultOutcome.RESTORE_APPLIED:
        state = HistoryState.APPLYING_RESTORE
        return (
            state,
            RestoreAppliedEvent(
                workflow=HISTORY_WORKFLOW,
                run_id=run_id,
                current_state=state.value,
                restored_file_count=payload.restored_file_count or len(payload.affected_files),
            ),
        )
    if payload.adapter_outcome == HistoryVaultOutcome.STALE_RESTORE_POINT:
        state = HistoryState.APPLYING_RESTORE
        return (
            state,
            StaleRestorePointDetectedEvent(
                workflow=HISTORY_WORKFLOW,
                run_id=run_id,
                current_state=state.value,
                next_action="history:refresh-restore-point",
            ),
        )
    state = HistoryState.LISTING_RESTORE_POINTS
    if payload.adapter_outcome == HistoryVaultOutcome.BLOCKED:
        return (
            state,
            HistoryBlockedEvent(
                workflow=HISTORY_WORKFLOW,
                run_id=run_id,
                current_state=state.value,
                reason_code=payload.reason_code or "history_blocked",
                next_action=payload.next_action or payload.human_message or "Gerar nova prévia pela rota oficial.",
            ),
        )
    return (
        state,
        HistoryFailedEvent(
            workflow=HISTORY_WORKFLOW,
            run_id=run_id,
            current_state=state.value,
            reason_code=payload.reason_code or "history_failed",
            next_action=payload.next_action or payload.human_message or "Repetir /mednotes:history pela rota oficial.",
        ),
    )


def build_history_fsm_result_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> HistoryFsmResult:
    """Project a real HistoryMachine model into the public workflow contract."""

    _validate_history_machine_model(model)
    state = HistoryState(model.state)
    category = category_for_history_state(state)
    progress_state = _progress_state_from_model(model, state, category)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _snapshot_from_model(model, state, category)
    safety = _version_control_safety(version_control_safety)
    reports = _reports_from_model(state, progress_state)
    public_report = reports.public_report
    agent_directive = agent_directive_from_progress_view_model(
        progress_view_model,
        schema="medical-notes-workbench.agent-directive.v1",
        reason=_machine_reason_code(model, state),
        effects=model.pending_effects,
        blockers=_machine_blockers(category, model, state),
        resume=progress_state.resume_action,
        report_requires=["primary_objective", "restore_preview", "restore_apply"],
        summary=public_report.summary_text(),
        instructions=_machine_agent_instructions(category),
    ).to_payload()
    return HistoryFsmResult(
        run_id=model.run_id,
        state_machine_snapshot=snapshot,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        decision=model.last_transition.decision if model.last_transition is not None else None,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        receipt=_receipt_from_model(
            model,
            progress_state=progress_state,
            version_control_safety=safety,
        ),
        reports=reports,
        agent_directive=JsonObjectAdapter.validate_python(agent_directive),
        version_control_safety=safety,
        diagnostic_context=_diagnostic_context_from_model(model, state, category),
        error_context=_error_context_from_model(model, state, category),
    )


def _validate_history_machine_model(model: WorkflowModel) -> None:
    if model.workflow != HISTORY_WORKFLOW:
        raise ValueError(f"history FSM projector requires workflow={HISTORY_WORKFLOW}")
    HistoryState(model.state)


def _progress_state_from_model(
    model: WorkflowModel,
    state: HistoryState,
    category: WorkflowStateCategory,
) -> WorkflowProgressState:
    status = _machine_progress_status(category)
    affected = _last_event_int(model, "affected_file_count")
    restored = _last_event_int(model, "restored_file_count")
    total = max(affected, restored)
    current = restored if state == HistoryState.COMPLETED else 0
    return WorkflowProgressState(
        workflow=HISTORY_WORKFLOW,
        run_id=model.run_id,
        state=state.value,
        phase=_machine_phase_for_state(state),
        event_type=_machine_event_type(status),
        message=_machine_message_for_state(state),
        status=status,
        current=current,
        total=total,
        counts=WorkflowProgressCounts(
            planned_items=total,
            processed_items=current,
            mutated_files=restored,
            written_files=restored,
            remaining_items=max(total - current, 0),
            blocked_items=total if status in {WorkflowProgressStatus.BLOCKED, WorkflowProgressStatus.FAILED} else 0,
        ),
        resume_action=_machine_resume_action(model, state),
        resume_supported=status
        in {
            WorkflowProgressStatus.RUNNING,
            WorkflowProgressStatus.WAITING_HUMAN,
            WorkflowProgressStatus.BLOCKED,
        },
        can_continue_now=status == WorkflowProgressStatus.RUNNING,
        decision=model.last_transition.decision.decision_summary()
        if model.last_transition is not None and model.last_transition.decision is not None
        else None,
        technical_context={
            "reason": _machine_reason_code(model, state),
            "category": category.value,
            "source": "HistoryMachine",
            "affected_file_count": affected,
            "restored_file_count": restored,
        },
    )


def _snapshot_from_model(
    model: WorkflowModel,
    state: HistoryState,
    category: WorkflowStateCategory,
) -> WorkflowStateMachineSnapshot:
    return WorkflowStateMachineSnapshot(
        workflow=HISTORY_WORKFLOW,
        run_id=model.run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_machine_snapshot_transition(transition) for transition in model.transition_log],
        metadata={"reason": _machine_reason_code(model, state), "source": "HistoryMachine"},
    )


def _machine_snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=category_for_history_state(HistoryState(transition.to_state)),
        trigger=transition.trigger,
        effects=list(transition.effects),
        decision=transition.decision,
        resume_action=transition.resume_action,
    )


def _receipt_from_model(
    model: WorkflowModel,
    *,
    progress_state: WorkflowProgressState,
    version_control_safety: VersionControlSafety,
) -> HistoryReceipt:
    return HistoryReceipt(
        run_id=model.run_id,
        status=progress_state.status.value,
        mutated=version_control_safety.changed_file_count > 0,
        next_action="" if progress_state.status == WorkflowProgressStatus.COMPLETED else progress_state.resume_action,
        human_decision_required=progress_state.status == WorkflowProgressStatus.WAITING_HUMAN,
        restored_file_count=_last_event_int(model, "restored_file_count"),
        version_control_safety=version_control_safety,
    )


def _reports_from_model(state: HistoryState, progress_state: WorkflowProgressState) -> WorkflowReports:
    summary = _machine_message_for_state(state)
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=HISTORY_WORKFLOW,
        run_id=progress_state.run_id,
        headline=summary,
        lines=public_lines,
    )
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details={
            "primary_objective_summary": _history_primary_objective_summary(
                state=state,
                progress_state=progress_state,
            ).to_payload()
        },
    )


def _history_primary_objective_summary(
    *,
    state: HistoryState,
    progress_state: WorkflowProgressState,
) -> WorkflowPrimaryObjectiveSummary:
    """State-owned answer to whether history listed/restored as requested."""

    completed = state in {HistoryState.RESTORE_POINTS_LISTED, HistoryState.COMPLETED}
    restored_count = progress_state.counts.written_files
    return WorkflowPrimaryObjectiveSummary(
        workflow=HISTORY_WORKFLOW,
        run_id=progress_state.run_id,
        objective="Listar pontos de restauração e aplicar restauração somente após prévia/decisão.",
        completed=completed,
        status=state.value,
        mutation_state="changed" if restored_count > 0 else "unchanged",
        mutation_summary=_history_mutation_summary(restored_count),
        remaining_work_summary=_history_remaining_work_summary(state, completed),
        next_step_summary=_history_next_step_summary(progress_state, completed),
        blocked_reason="" if completed else state.value,
    )


def _history_mutation_summary(restored_count: int) -> str:
    if restored_count > 0:
        return f"{restored_count} arquivo(s) foram restaurados."
    return "Nenhuma restauração foi aplicada nesta etapa."


def _history_remaining_work_summary(state: HistoryState, completed: bool) -> str:
    if completed and state == HistoryState.RESTORE_POINTS_LISTED:
        return "Pontos de restauração listados; nenhuma restauração foi solicitada."
    if completed:
        return "Restauração aplicada e conferida."
    return _machine_message_for_state(state)


def _history_next_step_summary(progress_state: WorkflowProgressState, completed: bool) -> str:
    if completed:
        return "Nenhuma ação pendente para o histórico nesta rota."
    return progress_state.resume_action or "Retomar /mednotes:history pela rota oficial."


def _diagnostic_context_from_model(
    model: WorkflowModel,
    state: HistoryState,
    category: WorkflowStateCategory,
) -> JsonObject:
    if category == WorkflowStateCategory.COMPLETED:
        return {}
    context: JsonObject = {
        "schema": "medical-notes-workbench.history-fsm-diagnostic-context.v1",
        "state": state.value,
        "category": category.value,
        "reason": _machine_reason_code(model, state),
        "source": "HistoryMachine",
    }
    evidence = _machine_audit_evidence(model)
    for key, value in evidence.items():
        if key not in context:
            context[key] = value
    return diagnostic_context_evidence_only(context)


def _error_context_from_model(
    model: WorkflowModel,
    state: HistoryState,
    category: WorkflowStateCategory,
) -> JsonObject:
    """Expose a typed recovery route for blocked/failed history states."""

    if category not in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return {}
    reason = _machine_reason_code(model, state)
    next_action = _machine_resume_action(model, state) or "history:timeline"
    return JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.error-context.v1",
            "phase": _machine_phase_for_state(state),
            "blocked_reason": reason,
            "root_cause": reason,
            "affected_artifact": "vault_restore",
            "error_summary": _machine_message_for_state(state),
            "suggested_fix": next_action,
            "next_action": next_action,
            "retry_scope": "history_restore_workflow",
            "missing_inputs": [],
            "human_decision_required": category == WorkflowStateCategory.WAITING_HUMAN,
            "version_control_safety": "preserve_vault_restore_point_before_retry",
        }
    )


def _machine_audit_evidence(model: WorkflowModel) -> JsonObject:
    if not model.event_log:
        return {}
    event = _HistoryMachineEventEvidence.model_validate(model.event_log[-1])
    return JsonObjectAdapter.validate_python(event.audit_evidence)


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


def _machine_event_type(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    match status:
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.WAITING_HUMAN | WorkflowProgressStatus.BLOCKED:
            return WorkflowProgressEventType.DECISION_EMITTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _machine_phase_for_state(state: HistoryState) -> str:
    match state:
        case HistoryState.LISTING_RESTORE_POINTS:
            return "history_list"
        case HistoryState.RESTORE_POINTS_LISTED:
            return "history_list"
        case HistoryState.PREVIEW_READY | HistoryState.WAITING_HUMAN_CONFIRMATION:
            return "history_preview"
        case HistoryState.APPLYING_RESTORE | HistoryState.COMPLETED:
            return "history_restore"
        case HistoryState.STALE_RESTORE_POINT:
            return "history_stale_restore_point"
        case HistoryState.RESTORE_CONFLICT:
            return "history_restore_conflict"
        case HistoryState.RESTORE_CANCELLED:
            return "history_restore_cancelled"
        case HistoryState.RESTORE_POINT_LIST_BLOCKED:
            return "restore_point_list_blocked"
        case HistoryState.RESTORE_PREVIEW_BLOCKED:
            return "restore_preview_blocked"
        case HistoryState.RESTORE_CONFIRMATION_BLOCKED:
            return "restore_confirmation_blocked"
        case HistoryState.RESTORE_APPLY_BLOCKED:
            return "restore_apply_blocked"
        case HistoryState.FAILED:
            return "history_failed"


def _machine_message_for_state(state: HistoryState) -> str:
    match state:
        case HistoryState.PREVIEW_READY:
            return "Restauração aguardando geração de prévia."
        case HistoryState.WAITING_HUMAN_CONFIRMATION:
            return "Prévia de restauração aguardando confirmação humana."
        case HistoryState.APPLYING_RESTORE:
            return "Restauração aguardando aplicação pelo adapter oficial."
        case HistoryState.STALE_RESTORE_POINT:
            return "Ponto de restauração ficou desatualizado."
        case HistoryState.RESTORE_CONFLICT:
            return "Restauração encontrou conflito antes de aplicar."
        case HistoryState.COMPLETED:
            return "Restauração aplicada e conferida."
        case HistoryState.RESTORE_POINTS_LISTED:
            return "Histórico de pontos de restauração listado."
        case HistoryState.RESTORE_CANCELLED:
            return "Restauração cancelada antes de alterar o vault."
        case HistoryState.RESTORE_POINT_LIST_BLOCKED:
            return "Listagem de pontos de restauração bloqueada."
        case HistoryState.RESTORE_PREVIEW_BLOCKED:
            return "Prévia de restauração bloqueada."
        case HistoryState.RESTORE_CONFIRMATION_BLOCKED:
            return "Confirmação da restauração bloqueada."
        case HistoryState.RESTORE_APPLY_BLOCKED:
            return "Aplicação da restauração bloqueada."
        case HistoryState.FAILED:
            return "Histórico falhou antes de concluir."
        case _:
            return "Histórico em andamento."


def _machine_resume_action(model: WorkflowModel, state: HistoryState) -> str:
    if state in {HistoryState.COMPLETED, HistoryState.RESTORE_POINTS_LISTED}:
        return ""
    if model.last_transition is not None and model.last_transition.resume_action:
        return model.last_transition.resume_action
    match state:
        case HistoryState.PREVIEW_READY:
            return "history:preview"
        case HistoryState.WAITING_HUMAN_CONFIRMATION:
            return "history:confirm-restore"
        case HistoryState.APPLYING_RESTORE:
            return "history:apply"
        case HistoryState.STALE_RESTORE_POINT:
            return "history:refresh-restore-point"
        case HistoryState.RESTORE_CONFLICT:
            return "history:resolve-conflict"
        case (
            HistoryState.RESTORE_CANCELLED
            | HistoryState.RESTORE_POINT_LIST_BLOCKED
            | HistoryState.RESTORE_PREVIEW_BLOCKED
            | HistoryState.RESTORE_CONFIRMATION_BLOCKED
            | HistoryState.RESTORE_APPLY_BLOCKED
            | HistoryState.FAILED
        ):
            return "history:timeline"
        case _:
            return ""


def _machine_reason_code(model: WorkflowModel, state: HistoryState) -> str:
    if model.last_transition is not None:
        return model.last_transition.reason_code
    return state.value


def _machine_blockers(
    category: WorkflowStateCategory,
    model: WorkflowModel,
    state: HistoryState,
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


def _machine_agent_instructions(category: WorkflowStateCategory) -> list[str]:
    if category == WorkflowStateCategory.WAITING_HUMAN:
        return ["Peça confirmação humana fechada antes de aplicar restauração."]
    if category in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return ["Use a decisão e o resume_action da FSM para recuperar /mednotes:history."]
    return ["Execute somente os efeitos em agent_directive.control.effects e retome /mednotes:history pelo resultado tipado."]


def _last_event_int(model: WorkflowModel, field_name: str) -> int:
    if not model.event_log:
        return 0
    event = model.event_log[-1]
    value = event[field_name] if field_name in event else 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _version_control_safety(value: VersionControlSafety | dict[str, object]) -> VersionControlSafety:
    if isinstance(value, VersionControlSafety):
        return value
    return VersionControlSafety.model_validate(value)
