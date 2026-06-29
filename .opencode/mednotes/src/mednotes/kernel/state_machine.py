from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import Field, StrictStr, ValidationInfo, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effect_intent import WorkflowEffect
from mednotes.kernel.progress import WorkflowProgressEvent
from mednotes.kernel.workflow import WorkflowDecision

if TYPE_CHECKING:
    from mednotes.kernel.fsm_event import WorkflowEventLike
    from mednotes.kernel.fsm_model import WorkflowModel
    from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult


class WorkflowStateCategory(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_AGENT = "waiting_agent"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"


class WorkflowTransition(ContractModel):
    workflow: StrictStr
    from_state: StrictStr
    to_state: StrictStr
    to_category: WorkflowStateCategory
    trigger: StrictStr
    # Executable effects are canonical WorkflowEffect intents emitted by the FSM.
    # Resource-mutation safety facts belong in receipts, not in transitions.
    effects: list[WorkflowEffect] = Field(default_factory=list)
    progress_events: list[WorkflowProgressEvent] = Field(default_factory=list)
    decision: WorkflowDecision | None = None
    resume_action: str = ""
    allowed_next_triggers: list[str] = Field(default_factory=list)

    @field_validator("workflow", "from_state", "to_state", "trigger")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def _terminal_or_human_wait_requires_decision(self) -> WorkflowTransition:
        if self.to_category in {
            WorkflowStateCategory.WAITING_HUMAN,
            WorkflowStateCategory.BLOCKED,
            WorkflowStateCategory.FAILED,
        } and self.decision is None:
            raise ValueError(f"{self.to_category} transition requires decision")
        if self.to_category == WorkflowStateCategory.WAITING_EXTERNAL and not self.resume_action.strip():
            raise ValueError("waiting_external transition requires resume_action")
        return self


class WorkflowStateMachineSnapshot(ContractModel):
    workflow: StrictStr
    run_id: StrictStr
    current_state: StrictStr
    current_category: WorkflowStateCategory
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "current_state")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


WorkflowEventT = TypeVar("WorkflowEventT", bound="WorkflowEventLike")


class WorkflowStateChart(Protocol):
    """Minimum StateChart surface the kernel can wrap without owning the FSM."""

    model: WorkflowModel
    state_field: str

    def send(self, event_name: str, **kwargs: object) -> object: ...

    def category_for_state(self, state: str) -> WorkflowStateCategory: ...


def send_workflow_event(
    machine: WorkflowStateChart,
    event: WorkflowEventT,
) -> WorkflowTransitionResult:
    """Send one typed event through python-statemachine and persist its result."""

    from mednotes.kernel.fsm_model import WorkflowModel
    from mednotes.kernel.fsm_transition_result import validate_transition_result

    model = machine.model
    if machine.state_field != WorkflowModel.STATECHART_STATE_FIELD:
        raise ValueError(f"workflow machines must use state_field={WorkflowModel.STATECHART_STATE_FIELD}")
    if not isinstance(event, ContractModel):
        raise TypeError("workflow events must be Pydantic contract models before machine.send")
    if event.workflow != model.workflow or event.run_id != model.run_id:
        raise ValueError("event belongs to a different workflow run")
    if event.current_state != model.state:
        raise ValueError("event.current_state does not match the workflow model state")

    previous_state = model.state
    try:
        raw_result = machine.send(event.name, workflow_event=event)
        result = extract_single_transition_result(raw_result)
        validate_transition_result_matches_event(result, event)
        validate_transition_result(result, category_for_state=machine.category_for_state)

        carrier_state = model.state
        if not carrier_state:
            raise ValueError("statechart state is not stable after transition")
        if model.state != result.to_state:
            raise ValueError("public workflow state does not match transition target")

        model.record_event(event)
        model.record_transition(result)
    except Exception:
        # python-statemachine mutates the configured carrier during send; failed
        # contract validation must not leak a candidate state as public truth.
        object.__setattr__(model, "state", previous_state)
        raise
    return result


def extract_single_transition_result(raw_result: object) -> WorkflowTransitionResult:
    """Normalize the callback result shape produced by python-statemachine."""

    from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult

    if isinstance(raw_result, WorkflowTransitionResult):
        return raw_result
    if isinstance(raw_result, list) and len(raw_result) == 1 and isinstance(raw_result[0], WorkflowTransitionResult):
        return raw_result[0]
    raise TypeError("workflow callback must return exactly one WorkflowTransitionResult")


def validate_transition_result_matches_event(
    result: WorkflowTransitionResult,
    event: WorkflowEventT,
) -> None:
    """Reject callback results that do not describe the event just sent."""

    if result.workflow != event.workflow:
        raise ValueError("transition workflow does not match event workflow")
    if result.run_id != event.run_id:
        raise ValueError("transition run_id does not match event run_id")
    if result.from_state != event.current_state:
        raise ValueError("transition from_state does not match event.current_state")
    if result.trigger != event.name:
        raise ValueError("transition trigger does not match event.name")
