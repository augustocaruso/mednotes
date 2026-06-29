"""Executable workflow effect intents without workflow-result dependencies.

`WorkflowEffect` is the FSM-owned intent that adapters may execute later. It
must stay independent from `workflow.py` so `state_machine.py` can type
transition effects without creating a kernel import cycle.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field, ValidationInfo, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject


class WorkflowEffectKind(StrEnum):
    RUN_SUBWORKFLOW = "run_subworkflow"
    CALL_SPECIALIST_MODEL = "call_specialist_model"
    ASK_HUMAN = "ask_human"
    WAIT_EXTERNAL = "wait_external"


class WorkflowEffect(ContractModel):
    """Executable work intent emitted from one stable workflow state."""

    schema_id: str = Field(default="workflow-effect.v1", alias="schema")
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    effect_id: str = Field(min_length=1)
    origin_state: str = Field(min_length=1)
    kind: WorkflowEffectKind
    target: str = ""
    payload: JsonObject = Field(default_factory=dict)
    mutates_resources: bool = Field(default=False, strict=True)
    no_resource_mutation: bool = Field(default=False, strict=True)
    rollback_declared: bool = Field(default=False, strict=True)
    requires_receipt: bool = Field(default=True, strict=True)
    requires_attestation: bool = Field(default=False, strict=True)
    model_policy: JsonObject = Field(default_factory=dict)
    resume_action: str = ""
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "effect_id", "origin_state")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def _validate_effect_contract(self) -> WorkflowEffect:
        if (
            self.kind
            in {
                WorkflowEffectKind.RUN_SUBWORKFLOW,
                WorkflowEffectKind.CALL_SPECIALIST_MODEL,
                WorkflowEffectKind.WAIT_EXTERNAL,
            }
            and not self.target.strip()
        ):
            raise ValueError(f"{self.kind} requires target")
        if self.mutates_resources and self.no_resource_mutation:
            raise ValueError("mutates_resources cannot be combined with no_resource_mutation")
        if self.mutates_resources and not self.rollback_declared:
            raise ValueError("mutates_resources requires rollback_declared")
        if self.kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL and not self.model_policy:
            raise ValueError("call_specialist_model requires model_policy")
        return self
