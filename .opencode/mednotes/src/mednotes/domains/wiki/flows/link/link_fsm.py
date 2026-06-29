from __future__ import annotations

from typing import Literal, cast

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.wiki.contracts.workflow_outcomes import WorkflowDecision
from mednotes.domains.wiki.flows.link.link_machine import (
    LINK_BODY_WORKFLOW,
    LINK_PUBLIC_WORKFLOWS,
    LinkBoundaryEvent,
    LinkMachine,
    LinkMode,
    category_for_link_state,
)
from mednotes.domains.wiki.flows.link.link_machine import (
    LinkState as MachineLinkState,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffectKind
from mednotes.kernel.errors import EXIT_IO, EXIT_MISSING, EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
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

LINK_WORKFLOW = "/mednotes:link"
LINK_BODY_PUBLIC_WORKFLOW = LINK_BODY_WORKFLOW


class _MachineAuditEvidence(ContractModel):
    """Typed event evidence consumed by parent workflow FSMs."""

    model_config = ConfigDict(extra="ignore")

    adapter_schema: str = ""
    adapter_phase: str = ""
    adapter_status: str = ""
    adapter_reason: str = ""
    operation: JsonObject = Field(default_factory=dict)
    mode: str = ""
    include_related_notes: bool = False
    counts: JsonObject = Field(default_factory=dict)
    required_inputs: list[str] = Field(default_factory=list)
    related_notes_recovery_state: JsonObject = Field(default_factory=dict)
    stale_reason: str = ""
    expected_git_status_hash: str = ""
    actual_git_status_hash: str = ""
    expected_git_head: str = ""
    actual_git_head: str = ""

    @field_validator("operation", mode="before")
    @classmethod
    def _coerce_operation(cls, value: object) -> JsonObject:
        if not isinstance(value, dict):
            return {}
        return JsonObjectAdapter.validate_python(value)

    @field_validator("counts", "related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_json_object(cls, value: object) -> JsonObject:
        if not isinstance(value, dict):
            return {}
        return JsonObjectAdapter.validate_python(value)

    def to_payload(self) -> JsonObject:
        return JsonObjectAdapter.validate_python(self.model_dump(mode="json"))


class _MachineEventEvidence(ContractModel):
    """Typed lens over persisted machine event evidence."""

    model_config = ConfigDict(extra="ignore")

    audit_evidence: _MachineAuditEvidence = Field(default_factory=_MachineAuditEvidence)
LINK_SCHEMA = "medical-notes-workbench.link-fsm-result.v1"
LINK_RECEIPT_SCHEMA = "medical-notes-workbench.link-receipt.v1"
LINK_AGENT_DIRECTIVE_FIELD = "agent_directive"

LINK_ALLOWED_ROOT_KEYS = frozenset(
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
LINK_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "returncode",
        "workflow_exit_code",
        "body_term_linker",
        "related_notes_sync",
        "reference_repair",
        "graph_audit_after",
        "vocabulary_bootstrap",
        "vocabulary_curator_batch_plan",
    }
)

def category_for_state(state: str) -> WorkflowStateCategory:
    """Map link leaf states through the canonical LinkMachine enum only."""

    return category_for_link_state(MachineLinkState(state))


class LinkFsmFacts(ContractModel):
    workflow: Literal["/mednotes:link", "/mednotes:link-body"] = LINK_WORKFLOW
    run_id: str = Field(min_length=1)
    mode: LinkMode = LinkMode.FULL
    initial_state: MachineLinkState
    event: LinkBoundaryEvent
    changed_files: list[str] = Field(default_factory=list)
    mutated: bool = False
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _event_must_match_fsm_entry(self) -> LinkFsmFacts:
        if self.event.workflow != self.workflow:
            raise ValueError("link event workflow must match LinkFsmFacts.workflow")
        if self.event.run_id != self.run_id:
            raise ValueError("link event run_id must match LinkFsmFacts.run_id")
        if self.event.current_state != self.initial_state.value:
            raise ValueError("link event current_state must match initial_state")
        if self.workflow == LINK_BODY_PUBLIC_WORKFLOW and self.mode != LinkMode.BODY_ONLY:
            raise ValueError("link-body facts require body_only mode")
        if self.workflow == LINK_WORKFLOW and self.mode != LinkMode.FULL:
            raise ValueError("full link facts require full mode")
        return self


class _LinkPayloadProgressViewFields(ContractModel):
    status: StrictStr


class _LinkPayloadSnapshotFields(ContractModel):
    current_category: StrictStr


class _LinkPayloadReceiptFields(ContractModel):
    status: StrictStr


class _LinkPayloadFields(ContractModel):
    workflow: StrictStr
    progress_view_model: _LinkPayloadProgressViewFields
    state_machine_snapshot: _LinkPayloadSnapshotFields
    receipt: _LinkPayloadReceiptFields


class _LinkCliExitCodeFields(ContractModel):
    progress_view_model: _LinkPayloadProgressViewFields


class _LinkErrorContextFields(ContractModel):
    missing_inputs: list[StrictStr] = Field(default_factory=list)


class LinkFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.link-fsm-result.v1"] = Field(default=LINK_SCHEMA, alias="schema")
    workflow: Literal["/mednotes:link", "/mednotes:link-body"] = LINK_WORKFLOW
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
    def _progress_view_model_matches_state(self) -> LinkFsmResult:
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
        assert_link_fsm_payload(payload)
        return payload


def build_link_fsm_result(facts: LinkFsmFacts) -> LinkFsmResult:
    """Project one typed LinkMachine event into the public link FSM payload."""

    return build_link_fsm_result_from_model(
        _link_model_after_event(facts.initial_state, facts.event),
        version_control_safety=facts.version_control_safety,
        error_context=facts.error_context,
        artifacts=facts.artifacts,
        changed_files=facts.changed_files,
        mutated=facts.mutated,
    )


def _link_model_after_event(initial_state: MachineLinkState, event: WorkflowEventLike) -> WorkflowModel:
    model = WorkflowModel.start(
        workflow=event.workflow,
        run_id=event.run_id,
        initial_state=initial_state.value,
    )
    send_workflow_event(
        LinkMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        event,
    )
    return model


def build_link_fsm_result_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
    error_context: JsonObject | None = None,
    artifacts: JsonObject | None = None,
    changed_files: list[str] | None = None,
    mutated: bool | None = None,
) -> LinkFsmResult:
    """Project a real LinkMachine model without reading adapter reports."""

    _validate_link_machine_model(model)
    state = MachineLinkState(model.state)
    category = category_for_link_state(state)
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
        report_requires=["graph", "body_links", "related_notes"],
        summary=public_report.summary_text(),
        instructions=_machine_agent_instructions(category),
    ).to_payload()
    machine_error_context = error_context or _error_context_from_model(model, state, category)
    return LinkFsmResult(
        workflow=cast(Literal["/mednotes:link", "/mednotes:link-body"], model.workflow),
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


def link_fsm_payload_from_model(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> JsonObject:
    """JSON boundary for the machine-driven link FSM projection."""

    return build_link_fsm_result_from_model(model, version_control_safety=version_control_safety).to_payload()


def _validate_link_machine_model(model: WorkflowModel) -> None:
    if model.workflow not in LINK_PUBLIC_WORKFLOWS:
        raise ValueError(f"link FSM projector requires workflow in {sorted(LINK_PUBLIC_WORKFLOWS)}")
    MachineLinkState(model.state)


def _progress_state_from_model(
    model: WorkflowModel,
    state: MachineLinkState,
    category: WorkflowStateCategory,
) -> WorkflowProgressState:
    status = _machine_progress_status(category)
    current, total, counts = _machine_progress_numbers(model, state, status)
    return WorkflowProgressState(
        workflow=model.workflow,
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
        technical_context={
            "reason": _machine_reason_code(model, state),
            "category": category.value,
            "source": "LinkMachine",
        },
    )


def _machine_progress_numbers(
    model: WorkflowModel,
    state: MachineLinkState,
    status: WorkflowProgressStatus,
) -> tuple[int, int, WorkflowProgressCounts]:
    changed = _machine_event_int(model, "changed_file_count") or _machine_audit_count(model, "files_changed")
    planned = max(_machine_event_int(model, "planned_link_count"), _machine_audit_count(model, "links_planned"))
    rewritten = _machine_audit_count(model, "links_rewritten")
    blocker_count = max(_machine_event_int(model, "blocker_count"), _machine_audit_count(model, "blocker_count"))
    fresh = _machine_audit_count(model, "fresh_record_count")
    remaining = _machine_audit_count(model, "remaining_count")
    total_notes = _machine_audit_count(model, "total_note_count")
    cache_hits = _machine_audit_count(model, "reused_count")
    api_calls = _machine_audit_count(model, "embedded_count")

    if state == MachineLinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
        total = total_notes or fresh + remaining
        return (
            fresh,
            total,
            WorkflowProgressCounts(
                planned_items=total,
                processed_items=fresh,
                cache_hits=cache_hits,
                api_calls=api_calls,
                remaining_items=remaining,
                blocked_items=remaining,
                deferred_items=remaining,
                mutated_files=changed,
                written_files=changed,
            ),
        )
    if state == MachineLinkState.COMPLETED:
        total = max(changed, rewritten, planned)
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
    if state == MachineLinkState.COMPLETED_WITH_LINK_BLOCKERS:
        blocked = max(blocker_count, 1)
        return (
            0,
            blocked,
            WorkflowProgressCounts(
                planned_items=max(planned, blocked),
                warnings=blocked,
                remaining_items=blocked,
                blocked_items=blocked,
                mutated_files=changed,
                written_files=changed,
            ),
        )
    if state == MachineLinkState.WAITING_HUMAN_CONFIRMATION:
        total = max(planned, rewritten)
        return (
            0,
            total,
            WorkflowProgressCounts(
                planned_items=total,
                remaining_items=total,
                blocked_items=total,
            ),
        )
    if status in {WorkflowProgressStatus.WAITING_AGENT, WorkflowProgressStatus.BLOCKED, WorkflowProgressStatus.FAILED}:
        blocked = max(blocker_count, remaining, 1)
        return (
            0,
            blocked,
            WorkflowProgressCounts(
                planned_items=max(planned, blocked),
                remaining_items=blocked,
                blocked_items=blocked,
                mutated_files=changed,
                written_files=changed,
            ),
        )
    total = max(planned, rewritten)
    return 0, total, WorkflowProgressCounts(planned_items=total)


def _machine_event_int(model: WorkflowModel, field_name: str) -> int:
    if not model.event_log:
        return 0
    event = model.event_log[-1]
    value = event[field_name] if field_name in event else 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _machine_audit_count(model: WorkflowModel, field_name: str) -> int:
    evidence = _machine_audit_evidence(model)
    try:
        raw_counts = evidence["counts"]
    except KeyError:
        return 0
    if not isinstance(raw_counts, dict):
        return 0
    try:
        value = raw_counts[field_name]
    except KeyError:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _snapshot_from_model(
    model: WorkflowModel,
    state: MachineLinkState,
    category: WorkflowStateCategory,
) -> WorkflowStateMachineSnapshot:
    return WorkflowStateMachineSnapshot(
        workflow=model.workflow,
        run_id=model.run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_machine_snapshot_transition(transition) for transition in model.transition_log],
        metadata={
            "reason": _machine_reason_code(model, state),
            "source": "LinkMachine",
            "link_mode": _link_mode_for_model(model).value,
        },
    )


def _link_mode_for_model(model: WorkflowModel) -> LinkMode:
    """Recover the invariant execution mode from the observed event or workflow."""

    if model.workflow == LINK_BODY_PUBLIC_WORKFLOW:
        return LinkMode.BODY_ONLY
    for raw_event in reversed(model.event_log):
        try:
            observation = raw_event["observation"]
        except KeyError:
            continue
        if not isinstance(observation, dict):
            continue
        try:
            raw_mode = observation["mode"]
        except KeyError:
            continue
        if isinstance(raw_mode, str) and raw_mode:
            return LinkMode(raw_mode)
    return LinkMode.FULL


def _machine_snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=category_for_link_state(MachineLinkState(transition.to_state)),
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
        schema=LINK_RECEIPT_SCHEMA,
        workflow=model.workflow,
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
    state: MachineLinkState,
    progress_state: WorkflowProgressState,
) -> WorkflowReports:
    summary = _machine_message_for_state(state)
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=progress_state.workflow,
        run_id=model.run_id,
        headline=summary,
        lines=public_lines,
    )
    details: JsonObject = {
        "primary_objective_summary": _primary_objective_summary(
            run_id=model.run_id,
            workflow=progress_state.workflow,
            state=state,
            progress_state=progress_state,
        ).to_payload()
    }
    operation_details = _operation_details_from_model(model)
    if operation_details:
        details.update(operation_details)
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details=details,
    )


def _operation_details_from_model(model: WorkflowModel) -> JsonObject:
    """Expose typed child-operation evidence for parent FSMs without root state."""

    for event in reversed(model.event_log):
        event_evidence = _MachineEventEvidence.model_validate(event)
        if event_evidence.audit_evidence.operation:
            return event_evidence.audit_evidence.operation
    return {}


def _primary_objective_summary(
    *,
    run_id: str,
    workflow: str,
    state: MachineLinkState,
    progress_state: WorkflowProgressState,
) -> WorkflowPrimaryObjectiveSummary:
    """State-owned answer to whether `/mednotes:link` completed its job."""

    completed = state == MachineLinkState.COMPLETED
    changed_count = max(progress_state.counts.mutated_files, progress_state.counts.written_files)
    return WorkflowPrimaryObjectiveSummary(
        workflow=workflow,
        run_id=run_id,
        objective=_link_objective_for_workflow(workflow),
        completed=completed,
        status=state.value,
        mutation_state="changed" if changed_count > 0 else "unchanged",
        mutation_summary=_link_mutation_summary(changed_count),
        remaining_work_summary=_link_remaining_work_summary(state, completed),
        next_step_summary=_link_next_step_summary(progress_state, completed),
        blocked_reason="" if completed else state.value,
    )


def _link_mutation_summary(changed_count: int) -> str:
    if changed_count > 0:
        return f"{changed_count} arquivo(s) de links foram alterados."
    return "Nenhum arquivo de links foi alterado nesta etapa."


def _link_objective_for_workflow(workflow: str) -> str:
    if workflow == LINK_BODY_PUBLIC_WORKFLOW:
        return "Atualizar somente WikiLinks no corpo das notas, sem Notas Relacionadas."
    return "Atualizar grafo, links de corpo e Notas Relacionadas quando aplicável."


def _link_remaining_work_summary(state: MachineLinkState, completed: bool) -> str:
    if completed:
        return "Grafo, links de corpo e Notas Relacionadas ficaram concluídos."
    if state == MachineLinkState.COMPLETED_WITH_LINK_BLOCKERS:
        return "O link terminou com pendências explícitas de grafo ou Notas Relacionadas."
    return _machine_message_for_state(state)


def _link_next_step_summary(progress_state: WorkflowProgressState, completed: bool) -> str:
    if completed:
        return "Nenhuma ação pendente para o pacote de links."
    return progress_state.resume_action or "Retomar /mednotes:link pela rota oficial."


def _diagnostic_context_from_model(
    model: WorkflowModel,
    state: MachineLinkState,
    category: WorkflowStateCategory,
) -> JsonObject:
    if category in {WorkflowStateCategory.COMPLETED, WorkflowStateCategory.COMPLETED_WITH_WARNINGS}:
        return {}
    context: JsonObject = {
        "schema": "medical-notes-workbench.link-fsm-diagnostic-context.v2",
        "state": state.value,
        "category": category.value,
        "reason": _machine_reason_code(model, state),
        "source": "LinkMachine",
    }
    evidence = _machine_audit_evidence(model)
    for key, value in evidence.items():
        if key not in context:
            context[key] = value
    return diagnostic_context_evidence_only(context)


def _machine_audit_evidence(model: WorkflowModel) -> JsonObject:
    if not model.event_log:
        return {}
    event = _MachineEventEvidence.model_validate(model.event_log[-1])
    return event.audit_evidence.to_payload()


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
        case WorkflowProgressStatus.WAITING_HUMAN:
            return WorkflowProgressEventType.DECISION_EMITTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _machine_phase_for_state(state: MachineLinkState) -> str:
    match state:
        case MachineLinkState.CHECKING_TRIGGER_CONTEXT:
            return "trigger_context"
        case MachineLinkState.DIAGNOSING_GRAPH | MachineLinkState.STALE_DIAGNOSIS:
            return "diagnosis"
        case MachineLinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
            return "vocabulary_bootstrap"
        case MachineLinkState.PLANNING_BODY_LINKS | MachineLinkState.APPLYING_BODY_LINKS:
            return "body_links"
        case MachineLinkState.PLANNING_RELATED_NOTES | MachineLinkState.APPLYING_RELATED_NOTES:
            return "related_notes"
        case (
            MachineLinkState.PLANNING_VOCABULARY_SEMANTIC_REPAIR
            | MachineLinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR
        ):
            return "vocabulary_semantic_repair"
        case MachineLinkState.WAITING_AGENT_DISAMBIGUATION:
            return "agent_disambiguation"
        case MachineLinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
            return "related_notes_export_recovery"
        case MachineLinkState.WAITING_AGENT_VOCABULARY_CURATOR:
            return "vocabulary_curator"
        case MachineLinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
            return "related_notes_recovery"
        case MachineLinkState.WAITING_HUMAN_CONFIRMATION:
            return "human_confirmation"
        case MachineLinkState.COMPLETED | MachineLinkState.COMPLETED_WITH_LINK_BLOCKERS:
            return "completed"
        case MachineLinkState.APPLY_CANCELLED:
            return "apply_cancelled"
        case MachineLinkState.GRAPH_DIAGNOSIS_BLOCKED:
            return "graph_diagnosis_blocked"
        case MachineLinkState.BODY_LINKS_BLOCKED:
            return "body_links_blocked"
        case MachineLinkState.RELATED_NOTES_BLOCKED:
            return "related_notes_blocked"
        case MachineLinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED:
            return "vocabulary_semantic_repair_blocked"
        case MachineLinkState.FAILED:
            return "failed"


def _machine_message_for_state(state: MachineLinkState) -> str:
    match state:
        case MachineLinkState.STALE_DIAGNOSIS:
            return "O diagnostico de links ficou desatualizado."
        case MachineLinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
            return "O vocabulario precisa ser preparado antes dos links."
        case MachineLinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
            return "Notas Relacionadas aguardam cota externa para continuar."
        case MachineLinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
            return "O export do Related Notes precisa ser recuperado pela rota oficial."
        case MachineLinkState.WAITING_HUMAN_CONFIRMATION:
            return "Preciso de confirmacao antes de aplicar links."
        case MachineLinkState.GRAPH_DIAGNOSIS_BLOCKED:
            return "Diagnóstico do grafo de links bloqueado."
        case MachineLinkState.BODY_LINKS_BLOCKED:
            return "Planejamento ou aplicação dos links de corpo bloqueada."
        case MachineLinkState.RELATED_NOTES_BLOCKED:
            return "Atualização de Notas Relacionadas bloqueada."
        case MachineLinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED:
            return "Reparo semântico do vocabulário bloqueado."
        case MachineLinkState.COMPLETED:
            return "Links atualizados e conferidos."
        case MachineLinkState.COMPLETED_WITH_LINK_BLOCKERS:
            return "Links concluidos com bloqueios pendentes."
        case MachineLinkState.FAILED:
            return "O workflow de links falhou antes de concluir."
        case _:
            return "Workflow de links em andamento."


def _machine_resume_action(model: WorkflowModel, state: MachineLinkState) -> str:
    if state in {MachineLinkState.COMPLETED, MachineLinkState.COMPLETED_WITH_LINK_BLOCKERS}:
        return ""
    if model.last_transition is not None and model.last_transition.resume_action:
        return model.last_transition.resume_action
    match state:
        case MachineLinkState.STALE_DIAGNOSIS:
            return "link:diagnose"
        case MachineLinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
            return "link:bootstrap-vocabulary"
        case MachineLinkState.WAITING_AGENT_DISAMBIGUATION:
            return "link:run-agent-disambiguation"
        case MachineLinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
            return "link:recover-related-notes-export"
        case MachineLinkState.WAITING_AGENT_VOCABULARY_CURATOR:
            return "link:run-vocabulary-curator"
        case MachineLinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
            return "link:retry-related-notes-export"
        case MachineLinkState.WAITING_HUMAN_CONFIRMATION:
            return "link:confirm-apply"
        case MachineLinkState.APPLYING_BODY_LINKS:
            return "link:apply-body-links"
        case MachineLinkState.APPLYING_RELATED_NOTES:
            return "link:apply-related-notes"
        case MachineLinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR:
            return "link:apply-vocabulary-semantic-repair"
        case MachineLinkState.GRAPH_DIAGNOSIS_BLOCKED:
            return "link:diagnose"
        case MachineLinkState.BODY_LINKS_BLOCKED:
            return "link:repair-body-links"
        case MachineLinkState.RELATED_NOTES_BLOCKED:
            return "link:repair-related-notes"
        case MachineLinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED:
            return "link:repair-vocabulary-semantics"
        case _:
            return "link:diagnose"


def _machine_reason_code(model: WorkflowModel, state: MachineLinkState) -> str:
    if model.last_transition is not None:
        return model.last_transition.reason_code
    return state.value


def _machine_blockers(
    category: WorkflowStateCategory,
    model: WorkflowModel,
    state: MachineLinkState,
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
    state: MachineLinkState,
    category: WorkflowStateCategory,
) -> JsonObject:
    """Synthesize the minimal recovery context owned by the LinkMachine state."""

    if category not in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return {}
    reason = _machine_reason_code(model, state) or state.value
    return JsonObjectAdapter.validate_python(
        {
            "blocked_reason": reason,
            "root_cause": reason,
            "affected_artifact": state.value,
            "next_action": _machine_resume_action(model, state) or "link:diagnose",
            "retry_scope": "link",
        }
    )


def _machine_agent_instructions(category: WorkflowStateCategory) -> list[str]:
    if category == WorkflowStateCategory.WAITING_AGENT:
        return ["Execute somente os efeitos em agent_directive.control.effects e retome /mednotes:link pelo resultado tipado."]
    if category == WorkflowStateCategory.WAITING_EXTERNAL:
        return ["Aguarde a condicao externa indicada antes de retomar /mednotes:link."]
    if category == WorkflowStateCategory.WAITING_HUMAN:
        return ["Peca a decisao humana fechada antes de aplicar links."]
    if category in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return ["Use a decisao e o resume_action da FSM para recuperar o workflow de links."]
    return ["Use a LinkMachine como fonte de verdade do estado de links."]


def _version_control_safety(value: VersionControlSafety | dict[str, object]) -> VersionControlSafety:
    if isinstance(value, VersionControlSafety):
        return value
    return VersionControlSafety.model_validate(value)


def assert_link_fsm_payload(payload: JsonObject) -> None:
    payload = JsonObjectAdapter.validate_python(payload)
    legacy_keys = set(payload) & LINK_FORBIDDEN_ROOT_KEYS
    if legacy_keys:
        raise ValueError(f"link FSM payload contains adapter root fields: {sorted(legacy_keys)}")
    required_root_keys = LINK_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing_keys = required_root_keys - set(payload)
    if missing_keys:
        raise ValueError(f"link FSM payload missing canonical root fields: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - LINK_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"link FSM payload contains unexpected root fields: {sorted(unexpected_keys)}")
    try:
        diagnostic_context = payload["diagnostic_context"]
    except KeyError:
        diagnostic_context = {}
    assert_diagnostic_context_evidence_only(diagnostic_context)
    if isinstance(diagnostic_context, dict) and "agent_directive" in diagnostic_context:
        raise ValueError("link FSM diagnostic_context must not contain agent_directive")
    fields = _link_payload_fields(payload)
    if fields.workflow not in LINK_PUBLIC_WORKFLOWS:
        raise ValueError("link FSM payload has invalid workflow")
    if fields.progress_view_model.status != fields.state_machine_snapshot.current_category:
        raise ValueError("link FSM status must match state_machine_snapshot category")
    if fields.receipt.status != fields.progress_view_model.status:
        raise ValueError("link FSM receipt status must match progress view status")
    if fields.progress_view_model.status in {
        WorkflowStateCategory.BLOCKED.value,
        WorkflowStateCategory.FAILED.value,
    } and not payload["error_context"]:
        raise ValueError("link FSM blocked/failed payload requires error_context")
    reports_model = WorkflowReports.model_validate(payload["reports"])
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    assert_public_report_matches_progress(
        reports_model.public_report,
        workflow=fields.workflow,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="link FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(payload[LINK_AGENT_DIRECTIVE_FIELD]),
        workflow=fields.workflow,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="link FSM",
    )
    _assert_link_snapshot(snapshot)


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """Keep executable linker effects tied to the current FSM lane."""

    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW, WorkflowEffectKind.CALL_SPECIALIST_MODEL}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def _assert_link_snapshot(snapshot: WorkflowStateMachineSnapshot) -> None:
    if snapshot.workflow not in LINK_PUBLIC_WORKFLOWS:
        raise ValueError("link FSM snapshot has invalid workflow")
    if snapshot.current_category != category_for_state(snapshot.current_state):
        raise ValueError("link FSM snapshot category does not match state")
    edges = _link_machine_edges()
    for transition in snapshot.transitions:
        if transition.to_category != category_for_state(transition.to_state):
            raise ValueError("link FSM transition category does not match state")
        edge = (transition.trigger, transition.from_state, transition.to_state)
        if edge not in edges:
            raise ValueError(f"unauthorized FSM transition: {edge}")


def _link_machine_edges() -> set[tuple[str, str, str]]:
    """Return every transition edge declared by the canonical LinkMachine."""

    edges: set[tuple[str, str, str]] = set()
    for event in LinkMachine.events:
        for transition in event._transitions:
            for target in transition._targets:
                edges.add((event.id, str(transition.source.value), str(target.value)))
    return edges


def _link_payload_fields(payload: JsonObject) -> _LinkPayloadFields:
    raw_fields: JsonObject = {
        "workflow": payload["workflow"],
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status",)),
        "state_machine_snapshot": _json_object_subset(payload, "state_machine_snapshot", ("current_category",)),
        "receipt": _json_object_subset(payload, "receipt", ("status",)),
    }
    try:
        return _LinkPayloadFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"link FSM payload invalid: {loc}: {msg}") from exc


def _json_object_subset(payload: JsonObject, field_name: str, keys: tuple[str, ...]) -> JsonObject:
    try:
        source = JsonObjectAdapter.validate_python(payload[field_name])
    except PydanticValidationError as exc:
        raise ValueError(f"link FSM payload invalid: {field_name} must be an object") from exc
    return {key: source[key] for key in keys if key in source}


def link_cli_exit_code(payload: JsonObject) -> int:
    fields = _link_cli_exit_code_fields(payload)
    status = fields.progress_view_model.status
    match status:
        case "completed" | "completed_with_warnings":
            return EXIT_OK
        case "waiting_agent" | "waiting_external" | "waiting_human" | "blocked":
            return EXIT_VALIDATION
        case "failed":
            if _link_error_context_missing_path(payload):
                return EXIT_MISSING
            return EXIT_IO
        case _:
            return EXIT_USAGE


def _link_error_context_missing_path(payload: JsonObject) -> bool:
    try:
        fields = _LinkErrorContextFields.model_validate(
            _json_object_subset(payload, "error_context", ("missing_inputs",))
        )
    except (KeyError, ValueError, PydanticValidationError):
        return False
    return "wiki_dir" in fields.missing_inputs


def _link_cli_exit_code_fields(payload: JsonObject) -> _LinkCliExitCodeFields:
    payload = JsonObjectAdapter.validate_python(payload)
    raw_fields: JsonObject = {
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status",)),
    }
    try:
        return _LinkCliExitCodeFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"link FSM exit-code payload invalid: {loc}: {msg}") from exc
