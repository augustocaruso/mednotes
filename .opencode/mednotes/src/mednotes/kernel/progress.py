from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from mednotes.kernel.base import ContractModel, JsonObject


class WorkflowProgressStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_AGENT = "waiting_agent"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"


class ProgressMode(StrEnum):
    DETERMINATE = "determinate"
    INDETERMINATE = "indeterminate"


class WorkflowProgressEventType(StrEnum):
    WORKFLOW_STARTED = "workflow_started"
    STATE_ENTERED = "state_entered"
    STEP_STARTED = "step_started"
    ITEM_PROCESSED = "item_processed"
    CACHE_HIT = "cache_hit"
    API_CALL_STARTED = "api_call_started"
    API_CALL_THROTTLED = "api_call_throttled"
    EXTERNAL_WAIT_STARTED = "external_wait_started"
    RESOURCE_MUTATED = "resource_mutated"
    DECISION_EMITTED = "decision_emitted"
    VALIDATION_COMPLETED = "validation_completed"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"


class WorkflowProgressCounts(ContractModel):
    planned_items: int = Field(default=0, ge=0)
    processed_items: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    api_failures: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    mutated_files: int = Field(default=0, ge=0)
    written_files: int = Field(default=0, ge=0)
    remaining_items: int = Field(default=0, ge=0)
    blocked_items: int = Field(default=0, ge=0)
    deferred_items: int = Field(default=0, ge=0)

    def plus(self, other: WorkflowProgressCounts) -> WorkflowProgressCounts:
        return WorkflowProgressCounts(
            planned_items=self.planned_items + other.planned_items,
            processed_items=self.processed_items + other.processed_items,
            cache_hits=self.cache_hits + other.cache_hits,
            api_calls=self.api_calls + other.api_calls,
            api_failures=self.api_failures + other.api_failures,
            warnings=self.warnings + other.warnings,
            mutated_files=self.mutated_files + other.mutated_files,
            written_files=self.written_files + other.written_files,
            remaining_items=self.remaining_items + other.remaining_items,
            blocked_items=self.blocked_items + other.blocked_items,
            deferred_items=self.deferred_items + other.deferred_items,
        )


class WorkflowProgressEvent(ContractModel):
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    event_type: WorkflowProgressEventType
    message: str = ""
    status: WorkflowProgressStatus = WorkflowProgressStatus.RUNNING
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    counts: WorkflowProgressCounts = Field(default_factory=WorkflowProgressCounts)
    resume_action: str = ""
    resume_supported: bool = False
    can_continue_now: bool = True
    user_action: str = ""
    decision: JsonObject | None = None
    technical_context: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "state", "phase")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class WorkflowProgressState(ContractModel):
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    event_type: WorkflowProgressEventType
    message: str = ""
    status: WorkflowProgressStatus = WorkflowProgressStatus.RUNNING
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    counts: WorkflowProgressCounts = Field(default_factory=WorkflowProgressCounts)
    resume_action: str = ""
    resume_supported: bool = False
    can_continue_now: bool = True
    user_action: str = ""
    decision: JsonObject | None = None
    technical_context: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "state", "phase")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class WorkflowProgressViewModel(ContractModel):
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    status: WorkflowProgressStatus
    mode: ProgressMode
    percent: int = Field(ge=0, le=100)
    terminal: bool
    successful: bool
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    count_label: str = ""
    message: str = ""
    user_action: str = ""
    resume_action: str = ""
    resume_supported: bool = False
    can_continue_now: bool = True
    counts: WorkflowProgressCounts = Field(default_factory=WorkflowProgressCounts)
    decision: JsonObject | None = None
    technical_context: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "state", "phase")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def _successful_requires_completed_status(self) -> WorkflowProgressViewModel:
        if self.successful and self.status not in _SUCCESS_STATUSES:
            raise ValueError("successful requires completed status")
        if self.status == WorkflowProgressStatus.WAITING_EXTERNAL:
            object.__setattr__(self, "can_continue_now", False)
        return self


_TERMINAL_STATUSES = {
    WorkflowProgressStatus.BLOCKED,
    WorkflowProgressStatus.FAILED,
    WorkflowProgressStatus.COMPLETED,
    WorkflowProgressStatus.COMPLETED_WITH_WARNINGS,
}
_SUCCESS_STATUSES = {
    WorkflowProgressStatus.COMPLETED,
    WorkflowProgressStatus.COMPLETED_WITH_WARNINGS,
}


def fold_progress_events(events: list[WorkflowProgressEvent]) -> WorkflowProgressState:
    if not events:
        raise ValueError("progress events must be non-empty")

    expected_workflow = events[0].workflow
    expected_run_id = events[0].run_id
    counts = WorkflowProgressCounts()
    current = 0
    total = 0
    for event in events:
        if event.workflow != expected_workflow:
            raise ValueError("progress events must share the same workflow")
        if event.run_id != expected_run_id:
            raise ValueError("progress events must share the same run_id")
        counts = counts.plus(event.counts)
        current = max(current, event.current)
        total = max(total, event.total)

    last = events[-1]
    return WorkflowProgressState(
        workflow=last.workflow,
        run_id=last.run_id,
        state=last.state,
        phase=last.phase,
        event_type=last.event_type,
        message=last.message,
        status=last.status,
        current=current,
        total=total,
        counts=counts,
        resume_action=last.resume_action,
        resume_supported=last.resume_supported,
        can_continue_now=last.can_continue_now,
        user_action=last.user_action,
        decision=last.decision,
        technical_context=last.technical_context,
    )


def build_progress_view_model(state: WorkflowProgressState) -> WorkflowProgressViewModel:
    mode = ProgressMode.DETERMINATE if state.total > 0 else ProgressMode.INDETERMINATE
    successful = state.status in _SUCCESS_STATUSES
    terminal = state.status in _TERMINAL_STATUSES
    percent = _progress_percent(state.current, state.total, successful=successful)
    count_label = f"{state.current} de {state.total}" if state.total > 0 else ""
    user_action = _user_action_for(state)

    return WorkflowProgressViewModel(
        workflow=state.workflow,
        run_id=state.run_id,
        state=state.state,
        phase=state.phase,
        status=state.status,
        mode=mode,
        percent=percent,
        terminal=terminal,
        successful=successful,
        current=state.current,
        total=state.total,
        count_label=count_label,
        message=state.message,
        user_action=user_action,
        resume_action=state.resume_action,
        resume_supported=state.resume_supported,
        can_continue_now=state.can_continue_now,
        counts=state.counts,
        decision=state.decision,
        technical_context=state.technical_context,
    )


def progress_state_from_view_model(view_model: WorkflowProgressViewModel) -> WorkflowProgressState:
    """Rehydrate the internal progress state from the public view model.

    FSM result models hide ``progress_state`` from public schemas/payloads. When
    a public payload is revalidated, this helper rebuilds the private state used
    by internal drift validators without making it a second public source of
    truth.
    """

    return WorkflowProgressState(
        workflow=view_model.workflow,
        run_id=view_model.run_id,
        state=view_model.state,
        phase=view_model.phase,
        event_type=_event_type_for_view_status(view_model.status),
        message=view_model.message,
        status=view_model.status,
        current=view_model.current,
        total=view_model.total,
        counts=view_model.counts,
        resume_action=view_model.resume_action,
        resume_supported=view_model.resume_supported,
        can_continue_now=view_model.can_continue_now,
        user_action=view_model.user_action,
        decision=view_model.decision,
        technical_context=view_model.technical_context,
    )


def _event_type_for_view_status(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    """Choose a coherent private event type for revalidated public payloads."""

    match status:
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case WorkflowProgressStatus.WAITING_HUMAN:
            return WorkflowProgressEventType.DECISION_EMITTED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
        case WorkflowProgressStatus.BLOCKED:
            return WorkflowProgressEventType.VALIDATION_COMPLETED
        case WorkflowProgressStatus.RUNNING:
            return WorkflowProgressEventType.STATE_ENTERED
        case WorkflowProgressStatus.WAITING_AGENT:
            return WorkflowProgressEventType.STEP_STARTED
        case WorkflowProgressStatus.IDLE:
            return WorkflowProgressEventType.WORKFLOW_STARTED


def _progress_percent(current: int, total: int, *, successful: bool) -> int:
    if successful:
        return 100
    if total <= 0:
        return 0
    return min(99, max(0, int((max(0, current) / total) * 100)))


def _user_action_for(state: WorkflowProgressState) -> str:
    if state.user_action.strip():
        return state.user_action.strip()
    if state.status == WorkflowProgressStatus.WAITING_AGENT:
        if state.resume_action:
            return state.resume_action
        return "Continue pela etapa assistida indicada pelo workflow antes de concluir."
    if state.status == WorkflowProgressStatus.WAITING_EXTERNAL:
        if state.resume_action:
            return "Aguarde a condicao externa e retome pela acao oficial quando ela estiver disponivel."
        return "Aguarde a condicao externa antes de retomar pela rota oficial."
    return ""
