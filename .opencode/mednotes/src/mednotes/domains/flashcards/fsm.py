from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.flashcards.contracts import FlashcardAcceptedCard, FlashcardsTaggingReceipt, FlashcardWritePlan
from mednotes.domains.flashcards.flashcards_machine import (
    FlashcardsMachine,
    FlashcardsState,
    ObsidianTaggingCompletedEvent,
    category_for_flashcards_state,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffectKind
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
    WorkflowDecision,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

FLASHCARDS_WORKFLOW = "/flashcards"
FLASHCARDS_FSM_SCHEMA = "medical-notes-workbench.flashcards-fsm-result.v1"


class _FlashcardsMachineEventEvidence(ContractModel):
    """Typed lens over persisted flashcards machine event evidence."""

    model_config = ConfigDict(extra="ignore")

    audit_evidence: JsonObject = Field(default_factory=dict)


FLASHCARDS_ALLOWED_ROOT_KEYS = frozenset(
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
        "diagnostic_context",
        "error_context",
    }
)
FLASHCARDS_FORBIDDEN_ROOT_KEYS = frozenset(
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


def _json_object_field(payload: JsonObject, key: str) -> JsonObject:
    value = payload[key] if key in payload else {}
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


class FlashcardsPrimaryObjectiveSummary(ContractModel):
    schema_id: Literal["workflow.primary-objective-summary.v1"] = Field(
        default="workflow.primary-objective-summary.v1",
        alias="schema",
    )
    workflow: Literal["/flashcards"] = FLASHCARDS_WORKFLOW
    run_id: StrictStr = Field(min_length=1)
    objective: StrictStr = "Criar flashcards no Anki e marcar as fontes no Obsidian após sucesso real."
    completed: StrictBool
    status: StrictStr = Field(min_length=1)
    mutation_state: Literal["changed", "unchanged", "not_applicable"]
    mutation_summary: StrictStr = Field(min_length=1)
    remaining_work_summary: StrictStr = Field(min_length=1)
    next_step_summary: StrictStr = Field(min_length=1)
    blocked_reason: StrictStr = ""
    required_report_items: list[StrictStr] = Field(
        default_factory=lambda: [
            "objective_status",
            "mutation_summary",
            "remaining_work_summary",
            "next_step_summary",
        ]
    )
    preview_only: bool
    created_cards: bool
    created_card_count: int = Field(ge=0)
    processed_source_count: int = Field(ge=0)
    tagged_source_count: int = Field(ge=0)
    obsidian_links_valid: bool


class FlashcardsReceipt(ContractModel):
    status: WorkflowProgressStatus
    changed_files: list[str] = Field(default_factory=list)
    created_card_count: int = Field(default=0, ge=0)


class FlashcardReportInput(ContractModel):
    accepted_cards: list[FlashcardAcceptedCard] = Field(default_factory=list)
    reports: WorkflowReports | None = None


class FlashcardsArtifacts(ContractModel):
    source_manifest_path: str = ""
    write_plan_path: str = ""
    final_report_path: str = ""
    index_path: str = ""
    dry_run: bool = False
    write_plan: FlashcardWritePlan | None = None
    apply_result: JsonObject = Field(default_factory=dict)


class FlashcardsFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.flashcards-fsm-result.v1"] = Field(
        default=FLASHCARDS_FSM_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/flashcards"] = FLASHCARDS_WORKFLOW
    run_id: str
    state_machine_snapshot: WorkflowStateMachineSnapshot
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: FlashcardsReceipt
    reports: WorkflowReports
    agent_directive: JsonObject
    artifacts: FlashcardsArtifacts = Field(default_factory=FlashcardsArtifacts)
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject | None = None

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
    def _progress_view_model_matches_state(self) -> FlashcardsFsmResult:
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
            "artifacts": self.artifacts.to_payload(),
        }
        if self.diagnostic_context:
            payload["diagnostic_context"] = dict(self.diagnostic_context)
        if self.error_context is not None:
            payload["error_context"] = dict(self.error_context)
        payload = JsonObjectAdapter.validate_python(payload)
        assert_flashcards_fsm_payload(payload)
        return payload


def assert_flashcards_fsm_payload(payload: JsonObject) -> None:
    """Gate the public `/flashcards` FSM payload against legacy root truth."""

    payload = JsonObjectAdapter.validate_python(payload)
    legacy_keys = set(payload) & FLASHCARDS_FORBIDDEN_ROOT_KEYS
    if legacy_keys:
        raise ValueError(f"flashcards FSM payload contains legacy root keys: {sorted(legacy_keys)}")
    required_keys = FLASHCARDS_ALLOWED_ROOT_KEYS - {"diagnostic_context", "error_context"}
    missing_keys = required_keys - set(payload)
    if missing_keys:
        raise ValueError(f"flashcards FSM payload missing canonical root keys: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - FLASHCARDS_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"flashcards FSM payload contains unexpected root keys: {sorted(unexpected_keys)}")
    diagnostic_context = _json_object_field(payload, "diagnostic_context")
    assert_diagnostic_context_evidence_only(diagnostic_context)
    if "agent_directive" in diagnostic_context:
        raise ValueError("flashcards FSM diagnostic_context must not contain agent_directive")
    reports = WorkflowReports.model_validate(payload["reports"])
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    receipt = FlashcardsReceipt.model_validate(payload["receipt"])
    if progress_view_model.status != snapshot.current_category.value:
        raise ValueError("flashcards FSM status must match state_machine_snapshot category")
    if receipt.status != progress_view_model.status:
        raise ValueError("flashcards FSM receipt status must match progress view status")
    assert_public_report_matches_progress(
        reports.public_report,
        workflow=FLASHCARDS_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="flashcards FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(_json_object_field(payload, "agent_directive")),
        workflow=FLASHCARDS_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="flashcards FSM",
    )


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """Flashcards delegates no hidden execution outside its FSM contract."""

    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def build_flashcards_fsm_result_from_model(model: WorkflowModel) -> FlashcardsFsmResult:
    """Project the real FlashcardsMachine model without reclassifying aggregate facts."""

    _validate_flashcards_machine_model(model)
    state = FlashcardsState(model.state)
    category = category_for_flashcards_state(state)
    progress_state = _progress_state_from_model(model, state, category)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _snapshot_from_model(model, state, category)
    reports = _reports_from_model(model, state, progress_state)
    agent_directive = agent_directive_from_progress_view_model(
        progress_view_model,
        schema="medical-notes-workbench.agent-directive.v1",
        reason=_machine_reason_code(model, state),
        effects=model.pending_effects,
        blockers=_machine_blockers(category, model, state),
        resume=progress_state.resume_action,
        report_requires=["primary_objective", "anki_write", "obsidian_links"],
        summary=_machine_agent_summary(state, progress_state),
        instructions=_machine_agent_instructions(category),
    ).to_payload()
    # Keep the public flashcards payload on the repository-wide directive schema
    # while the generic kernel model remains intentionally domain-neutral.
    return FlashcardsFsmResult(
        run_id=model.run_id,
        state_machine_snapshot=snapshot,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        decision=model.last_transition.decision if model.last_transition is not None else None,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        receipt=FlashcardsReceipt(
            status=progress_view_model.status,
            changed_files=_event_string_list(model, "changed_files"),
            created_card_count=_event_int_max(model, "created_card_count"),
        ),
        reports=reports,
        agent_directive=JsonObjectAdapter.validate_python(agent_directive),
        artifacts=FlashcardsArtifacts(),
        diagnostic_context=_diagnostic_context_from_model(model, state, category),
        error_context=_error_context_for(model.last_transition.decision if model.last_transition is not None else None),
    )

def flashcards_fsm_payload_from_model(model: WorkflowModel) -> JsonObject:
    """JSON boundary for the machine-driven `/flashcards` FSM projection."""

    return build_flashcards_fsm_result_from_model(model).to_payload()


def flashcards_fsm_payload_from_tagging_receipt(receipt: JsonObject, *, run_id: str) -> JsonObject:
    """Project the official Obsidian tag receipt into the terminal FSM state."""

    tagging = FlashcardsTaggingReceipt.model_validate(receipt)
    model = WorkflowModel.start(
        workflow=FLASHCARDS_WORKFLOW,
        run_id=run_id,
        initial_state=FlashcardsState.TAGGING_OBSIDIAN.value,
    )
    send_workflow_event(
        FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        ObsidianTaggingCompletedEvent(
            workflow=FLASHCARDS_WORKFLOW,
            run_id=run_id,
            current_state=FlashcardsState.TAGGING_OBSIDIAN.value,
            tagged_source_count=len(tagging.changed_files),
            changed_files=list(tagging.changed_files),
            audit_evidence={
                "effect_target": tagging.effect_target,
                "status": tagging.status,
                "tag": tagging.tag,
            },
        ),
    )
    return flashcards_fsm_payload_from_model(model)


def _validate_flashcards_machine_model(model: WorkflowModel) -> None:
    if model.workflow != FLASHCARDS_WORKFLOW:
        raise ValueError(f"flashcards FSM projector requires workflow={FLASHCARDS_WORKFLOW}")
    FlashcardsState(model.state)


def _progress_state_from_model(
    model: WorkflowModel,
    state: FlashcardsState,
    category: WorkflowStateCategory,
) -> WorkflowProgressState:
    status = _machine_progress_status(category)
    current, total, counts = _machine_counts(model, state)
    return WorkflowProgressState(
        workflow=FLASHCARDS_WORKFLOW,
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
            "source": "FlashcardsMachine",
            "source_count": _event_int_max(model, "source_count"),
            "candidate_count": _event_int_max(model, "candidate_count"),
            "new_card_count": _event_int_max(model, "new_card_count"),
            "created_card_count": _event_int_max(model, "created_card_count"),
            "tagged_source_count": _event_int_max(model, "tagged_source_count"),
        },
    )


def _machine_counts(
    model: WorkflowModel,
    state: FlashcardsState,
) -> tuple[int, int, WorkflowProgressCounts]:
    source_count = _event_int_max(model, "source_count")
    candidate_count = _event_int_max(model, "candidate_count")
    new_card_count = _event_int_max(model, "new_card_count")
    created_count = _event_int_max(model, "created_card_count")
    tagged_count = _event_int_max(model, "tagged_source_count")
    planned = max(candidate_count, new_card_count, created_count, source_count)
    if state == FlashcardsState.COMPLETED:
        total = max(created_count, tagged_count, candidate_count, source_count)
        return (
            total,
            total,
            WorkflowProgressCounts(
                planned_items=total,
                processed_items=total,
                mutated_files=tagged_count,
                written_files=tagged_count,
            ),
        )
    if state == FlashcardsState.WAITING_HUMAN_CONFIRMATION:
        return (
            0,
            max(candidate_count, new_card_count),
            WorkflowProgressCounts(
                planned_items=max(candidate_count, new_card_count),
                remaining_items=new_card_count,
                blocked_items=new_card_count,
            ),
        )
    if state in {
        FlashcardsState.STALE_SOURCE,
        FlashcardsState.CREATE_CANCELLED,
        FlashcardsState.SOURCE_SELECTION_BLOCKED,
        FlashcardsState.CANDIDATE_GENERATION_BLOCKED,
        FlashcardsState.PREVIEW_DECISION_BLOCKED,
        FlashcardsState.ANKI_WRITE_BLOCKED,
        FlashcardsState.OBSIDIAN_TAGGING_BLOCKED,
        FlashcardsState.FAILED,
    }:
        blocked = max(planned, 1)
        return (
            0,
            blocked,
            WorkflowProgressCounts(
                planned_items=planned,
                remaining_items=blocked,
                blocked_items=blocked,
            ),
        )
    return (
        0,
        planned,
        WorkflowProgressCounts(
            planned_items=planned,
            remaining_items=planned,
        ),
    )


def _snapshot_from_model(
    model: WorkflowModel,
    state: FlashcardsState,
    category: WorkflowStateCategory,
) -> WorkflowStateMachineSnapshot:
    return WorkflowStateMachineSnapshot(
        workflow=FLASHCARDS_WORKFLOW,
        run_id=model.run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_machine_snapshot_transition(transition) for transition in model.transition_log],
        metadata={"reason": _machine_reason_code(model, state), "source": "FlashcardsMachine"},
    )


def _machine_snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=category_for_flashcards_state(FlashcardsState(transition.to_state)),
        trigger=transition.trigger,
        effects=list(transition.effects),
        decision=transition.decision,
        resume_action=transition.resume_action,
    )


def _reports_from_model(
    model: WorkflowModel,
    state: FlashcardsState,
    progress_state: WorkflowProgressState,
) -> WorkflowReports:
    summary = _machine_message_for_state(state)
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=FLASHCARDS_WORKFLOW,
        run_id=progress_state.run_id,
        headline=summary,
        lines=public_lines,
    )
    created_count = _event_int_max(model, "created_card_count")
    tagged_count = _event_int_max(model, "tagged_source_count")
    completed = state == FlashcardsState.COMPLETED
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details={
            "primary_objective_summary": FlashcardsPrimaryObjectiveSummary(
                run_id=model.run_id,
                completed=completed,
                status=state.value,
                mutation_state="changed" if created_count > 0 or tagged_count > 0 else "unchanged",
                mutation_summary=_flashcards_mutation_summary(created_count, tagged_count),
                remaining_work_summary=_flashcards_remaining_work_summary(state, completed),
                next_step_summary=_flashcards_next_step_summary(progress_state, completed),
                blocked_reason="" if completed else state.value,
                preview_only=state
                in {
                    FlashcardsState.WAITING_AGENT_CANDIDATES,
                    FlashcardsState.WAITING_HUMAN_CONFIRMATION,
                },
                created_cards=created_count > 0,
                created_card_count=created_count,
                processed_source_count=max(
                    _event_int_max(model, "source_count"),
                    progress_state.counts.processed_items,
                ),
                tagged_source_count=tagged_count,
                obsidian_links_valid=state != FlashcardsState.STALE_SOURCE,
            ).to_payload()
        },
    )


def _flashcards_mutation_summary(created_count: int, tagged_count: int) -> str:
    if created_count > 0:
        return f"{created_count} card(s) foram criados no Anki; {tagged_count} fonte(s) foram marcadas."
    if tagged_count > 0:
        return f"{tagged_count} fonte(s) foram marcadas no Obsidian."
    return "Nenhum card foi criado e nenhuma fonte foi marcada nesta etapa."


def _flashcards_remaining_work_summary(state: FlashcardsState, completed: bool) -> str:
    if completed:
        return "Cards aceitos foram criados e as fontes foram marcadas quando aplicável."
    return _machine_message_for_state(state)


def _flashcards_next_step_summary(progress_state: WorkflowProgressState, completed: bool) -> str:
    if completed:
        return "Nenhuma ação pendente para flashcards."
    return progress_state.resume_action or "Retomar /flashcards pela rota oficial."


def _diagnostic_context_from_model(
    model: WorkflowModel,
    state: FlashcardsState,
    category: WorkflowStateCategory,
) -> JsonObject:
    if category == WorkflowStateCategory.COMPLETED:
        return {}
    context: JsonObject = {
        "schema": "medical-notes-workbench.flashcards-fsm-diagnostic-context.v2",
        "state": state.value,
        "category": category.value,
        "reason": _machine_reason_code(model, state),
        "source": "FlashcardsMachine",
    }
    evidence = _machine_audit_evidence(model)
    for key, value in evidence.items():
        if key not in context:
            context[key] = value
    return diagnostic_context_evidence_only(context)


def _machine_audit_evidence(model: WorkflowModel) -> JsonObject:
    if not model.event_log:
        return {}
    event = _FlashcardsMachineEventEvidence.model_validate(model.event_log[-1])
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
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case WorkflowProgressStatus.WAITING_HUMAN | WorkflowProgressStatus.BLOCKED:
            return WorkflowProgressEventType.DECISION_EMITTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _machine_phase_for_state(state: FlashcardsState) -> str:
    match state:
        case FlashcardsState.CHECKING_SOURCES | FlashcardsState.STALE_SOURCE:
            return "flashcards_sources"
        case FlashcardsState.WAITING_AGENT_CANDIDATES | FlashcardsState.WAITING_HUMAN_CONFIRMATION:
            return "flashcards_preview"
        case FlashcardsState.ANKI_UNAVAILABLE | FlashcardsState.WRITING_ANKI:
            return "flashcards_anki"
        case FlashcardsState.TAGGING_OBSIDIAN:
            return "flashcards_obsidian_tagging"
        case FlashcardsState.COMPLETED:
            return "flashcards_completed"
        case FlashcardsState.CREATE_CANCELLED:
            return "flashcards_create_cancelled"
        case FlashcardsState.SOURCE_SELECTION_BLOCKED:
            return "flashcards_source_selection_blocked"
        case FlashcardsState.CANDIDATE_GENERATION_BLOCKED:
            return "flashcards_candidate_generation_blocked"
        case FlashcardsState.PREVIEW_DECISION_BLOCKED:
            return "flashcards_preview_decision_blocked"
        case FlashcardsState.ANKI_WRITE_BLOCKED:
            return "flashcards_anki_write_blocked"
        case FlashcardsState.OBSIDIAN_TAGGING_BLOCKED:
            return "flashcards_obsidian_tagging_blocked"
        case FlashcardsState.FAILED:
            return "flashcards_failed"


def _machine_message_for_state(state: FlashcardsState) -> str:
    match state:
        case FlashcardsState.WAITING_AGENT_CANDIDATES:
            return "Flashcards aguardam geração de candidatos pelo agente."
        case FlashcardsState.WAITING_HUMAN_CONFIRMATION:
            return "Revise a prévia antes de criar cards no Anki."
        case FlashcardsState.ANKI_UNAVAILABLE:
            return "Flashcards aguardam o Anki ficar disponível."
        case FlashcardsState.WRITING_ANKI:
            return "Flashcards estão sendo gravados no Anki."
        case FlashcardsState.TAGGING_OBSIDIAN:
            return "Flashcards aguardam marcação das fontes no Obsidian."
        case FlashcardsState.STALE_SOURCE:
            return "A fonte dos flashcards ficou desatualizada."
        case FlashcardsState.COMPLETED:
            return "Flashcards criados e fontes conferidas."
        case FlashcardsState.CREATE_CANCELLED:
            return "Criação de flashcards cancelada antes de gravar no Anki."
        case FlashcardsState.SOURCE_SELECTION_BLOCKED:
            return "Seleção das fontes de flashcards bloqueada."
        case FlashcardsState.CANDIDATE_GENERATION_BLOCKED:
            return "Geração de candidatos de flashcards bloqueada."
        case FlashcardsState.PREVIEW_DECISION_BLOCKED:
            return "Decisão da prévia de flashcards bloqueada."
        case FlashcardsState.ANKI_WRITE_BLOCKED:
            return "Escrita dos flashcards no Anki bloqueada."
        case FlashcardsState.OBSIDIAN_TAGGING_BLOCKED:
            return "Marcação das fontes no Obsidian bloqueada."
        case FlashcardsState.FAILED:
            return "Flashcards falharam antes de concluir."
        case _:
            return "Workflow de flashcards em andamento."


def _machine_resume_action(model: WorkflowModel, state: FlashcardsState) -> str:
    if state == FlashcardsState.COMPLETED:
        return ""
    if model.last_transition is not None and model.last_transition.resume_action:
        return model.last_transition.resume_action
    match state:
        case FlashcardsState.WAITING_AGENT_CANDIDATES:
            return "flashcards:generate-candidates"
        case FlashcardsState.WAITING_HUMAN_CONFIRMATION:
            return "flashcards:confirm-create"
        case FlashcardsState.ANKI_UNAVAILABLE:
            return "flashcards:retry-anki"
        case FlashcardsState.WRITING_ANKI:
            return "flashcards:write-anki"
        case FlashcardsState.TAGGING_OBSIDIAN:
            return "flashcards:tag-obsidian"
        case (
            FlashcardsState.STALE_SOURCE
            | FlashcardsState.CREATE_CANCELLED
            | FlashcardsState.SOURCE_SELECTION_BLOCKED
            | FlashcardsState.CANDIDATE_GENERATION_BLOCKED
            | FlashcardsState.PREVIEW_DECISION_BLOCKED
            | FlashcardsState.ANKI_WRITE_BLOCKED
            | FlashcardsState.OBSIDIAN_TAGGING_BLOCKED
            | FlashcardsState.FAILED
        ):
            return "flashcards:prepare"
        case _:
            return ""


def _machine_reason_code(model: WorkflowModel, state: FlashcardsState) -> str:
    if model.last_transition is not None:
        return model.last_transition.reason_code
    return state.value


def _machine_blockers(
    category: WorkflowStateCategory,
    model: WorkflowModel,
    state: FlashcardsState,
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


def _machine_agent_summary(state: FlashcardsState, progress_state: WorkflowProgressState) -> str:
    return progress_state.message or _machine_message_for_state(state)


def _machine_agent_instructions(category: WorkflowStateCategory) -> list[str]:
    if category == WorkflowStateCategory.WAITING_AGENT:
        return ["Execute somente os efeitos em agent_directive.control.effects e retome /flashcards pelo resultado tipado."]
    if category == WorkflowStateCategory.WAITING_EXTERNAL:
        return ["Aguarde o Anki ficar disponivel antes de retomar /flashcards."]
    if category == WorkflowStateCategory.WAITING_HUMAN:
        return ["Peça a decisão humana fechada antes de criar cards no Anki."]
    if category in {WorkflowStateCategory.BLOCKED, WorkflowStateCategory.FAILED}:
        return ["Use a decisão e o resume_action da FSM para recuperar /flashcards."]
    return ["Use a FlashcardsMachine como fonte de verdade do estado de flashcards."]


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


def _event_int_max(model: WorkflowModel, field_name: str) -> int:
    values: list[int] = []
    for event in model.event_log:
        value = event[field_name] if field_name in event else 0
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            values.append(value)
    return max(values, default=0)


def _event_string_list(model: WorkflowModel, field_name: str) -> list[str]:
    values: list[str] = []
    for event in model.event_log:
        value = event[field_name] if field_name in event else []
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item:
                values.append(item)
    return list(dict.fromkeys(values))


def _error_context_for(decision: WorkflowDecision | None) -> JsonObject | None:
    if decision is None or decision.kind == "ask_human":
        return None
    if decision.reason_code == "invalid_obsidian_deeplink":
        return JsonObjectAdapter.validate_python(
            {
                "phase": decision.phase,
                "blocked_reason": decision.reason_code,
                "root_cause": "invalid_obsidian_deeplink",
                "affected_artifact": "flashcards_source_manifest",
                "error_summary": decision.developer_summary,
                "suggested_fix": "Regenerar o manifest de fontes e preparar novamente antes de criar cards.",
                "next_action": decision.next_action,
                "retry_scope": "regenerate_flashcard_sources_then_prepare",
                "human_decision_required": False,
            }
        )
    return JsonObjectAdapter.validate_python(
        {
            "phase": decision.phase,
            "blocked_reason": decision.reason_code,
            "root_cause": decision.reason_code,
            "affected_artifact": "flashcards_prepare_payload",
            "error_summary": decision.developer_summary,
            "suggested_fix": decision.next_action,
            "next_action": decision.next_action,
            "retry_scope": "resolve_flashcards_prepare_blocker_then_prepare",
            "human_decision_required": False,
        }
    )
