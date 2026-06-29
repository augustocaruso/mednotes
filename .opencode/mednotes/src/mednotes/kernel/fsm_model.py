from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effects import WorkflowEffect
from mednotes.kernel.fsm_event import WorkflowEventLike
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult


class WorkflowModel(ContractModel):
    """Persisted workflow state used directly as python-statemachine carrier."""

    STATECHART_STATE_FIELD: ClassVar[str] = "state"

    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    # Public persisted state is the single mutable StateChart carrier.
    state: str = Field(min_length=1)
    # Events are typed before sending; the persisted log stores JSON evidence
    # so WorkflowModel can rehydrate without importing every domain union.
    event_log: list[JsonObject] = Field(default_factory=list)
    transition_log: list[WorkflowTransitionResult] = Field(default_factory=list)
    last_transition: WorkflowTransitionResult | None = None
    pending_effects: list[WorkflowEffect] = Field(default_factory=list)

    @classmethod
    def start(cls, *, workflow: str, run_id: str, initial_state: str) -> WorkflowModel:
        return cls(workflow=workflow, run_id=run_id, state=initial_state)

    def __setattr__(self, name: str, value: object) -> None:
        """Allow python-statemachine's transient empty carrier during microsteps."""

        if name == self.STATECHART_STATE_FIELD and value is None:
            object.__setattr__(self, name, value)
            return
        super().__setattr__(name, value)

    def record_event(self, event: WorkflowEventLike) -> None:
        if not isinstance(event, ContractModel):
            raise TypeError("workflow events must be Pydantic contract models")
        if event.workflow != self.workflow or event.run_id != self.run_id:
            raise ValueError("event belongs to a different workflow run")
        self.event_log.append(event.to_payload())

    def record_transition(self, transition: WorkflowTransitionResult) -> None:
        if transition.workflow != self.workflow or transition.run_id != self.run_id:
            raise ValueError("transition belongs to a different workflow run")
        if self.state != transition.to_state:
            raise ValueError("machine state does not match transition target")
        object.__setattr__(self, "last_transition", transition)
        object.__setattr__(self, "pending_effects", list(transition.effects))
        self.transition_log.append(transition)
