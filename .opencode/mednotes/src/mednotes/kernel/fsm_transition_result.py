from __future__ import annotations

from collections.abc import Callable

from pydantic import Field, StrictStr, ValidationInfo, field_validator

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.workflow import HumanDecisionPacket, WorkflowDecision


class WorkflowTransitionResult(ContractModel):
    """Typed result returned by a real StateChart transition callback."""

    workflow: StrictStr = Field(min_length=1)
    run_id: StrictStr = Field(min_length=1)
    from_state: StrictStr = Field(min_length=1)
    to_state: StrictStr = Field(min_length=1)
    trigger: StrictStr = Field(min_length=1)
    reason_code: StrictStr = Field(min_length=1)
    effects: list[WorkflowEffect] = Field(default_factory=list)
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    resume_action: str = ""
    # Redacted/debug-only evidence; categories and effects are validated from typed fields.
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow", "run_id", "from_state", "to_state", "trigger", "reason_code")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


def validate_transition_result(
    transition: WorkflowTransitionResult,
    *,
    category_for_state: Callable[[str], object],
) -> WorkflowTransitionResult:
    """Validate category-dependent contracts without importing domain state maps."""

    for effect in transition.effects:
        if effect.origin_state != transition.to_state:
            raise ValueError("effect origin_state must match transition target")

    category = _category_value(category_for_state(transition.to_state))
    match category:
        case "waiting_agent":
            if not transition.effects:
                raise ValueError("waiting_agent transition requires at least one workflow effect")
        case "waiting_human":
            if transition.human_decision_packet is None:
                raise ValueError("waiting_human transition requires human_decision_packet")
            if not _has_effect_kind(transition, WorkflowEffectKind.ASK_HUMAN):
                raise ValueError("waiting_human transition requires ask_human effect")
        case "waiting_external":
            if not transition.resume_action.strip():
                raise ValueError("waiting_external transition requires resume_action")
            if not _has_effect_kind(transition, WorkflowEffectKind.WAIT_EXTERNAL):
                raise ValueError("waiting_external transition requires wait_external effect")
        case "blocked" | "failed":
            if transition.decision is None:
                raise ValueError(f"{category} transition requires decision")
    return transition


def _category_value(category: object) -> str:
    value = getattr(category, "value", category)
    return str(value)


def _has_effect_kind(transition: WorkflowTransitionResult, kind: WorkflowEffectKind) -> bool:
    return any(effect.kind == kind for effect in transition.effects)
