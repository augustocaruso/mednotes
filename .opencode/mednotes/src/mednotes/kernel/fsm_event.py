from __future__ import annotations

from typing import Protocol

from pydantic import Field

from mednotes.kernel.base import ContractModel, JsonObject


class WorkflowEvent(ContractModel):
    """Base fact consumed by workflow statecharts; domains add typed fields."""

    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    # Redacted replay/debug material only; FSM decisions must use typed fields.
    audit_evidence: JsonObject = Field(default_factory=dict)


class WorkflowEventLike(Protocol):
    """Structural event surface required by the StateChart kernel wrapper."""

    @property
    def workflow(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def name(self) -> str:
        ...

    @property
    def current_state(self) -> str: ...
