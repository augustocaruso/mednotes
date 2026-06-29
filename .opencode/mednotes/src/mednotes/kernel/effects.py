"""Framework workflow-effect contracts (pure FSM kernel, domain-agnostic).

WorkflowEffect / WorkflowEffectResult are the generic effect intent + result of
the FSM kernel. Concrete product payloads live in domain modules; adapters are
the only layer allowed to materialize those effects in the outside world.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SerializeAsAny, model_validator

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effect_intent import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.progress import WorkflowProgressEvent
from mednotes.kernel.workflow import HumanDecisionPacket

__all__ = [
    "WorkflowEffect",
    "WorkflowEffectKind",
    "WorkflowEffectOutcome",
    "WorkflowEffectResult",
    "WorkflowEffectStatus",
    "workflow_effect_blocked_outcome",
    "workflow_effect_completed_outcome",
    "workflow_effect_failed_outcome",
    "workflow_effect_skipped_outcome",
    "workflow_effect_waiting_agent_outcome",
    "workflow_effect_waiting_external_outcome",
    "workflow_effect_waiting_human_outcome",
    "workflow_effect_warning_outcome",
]


class WorkflowEffectStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    WAITING_AGENT = "waiting_agent"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowEffectOutcome(ContractModel):
    """Generic outcome discriminator for framework-level roundtrips.

    Domain adapters should return stricter outcome models at their own
    boundary. The kernel only requires a stable `code` field so result envelopes
    can be serialized without knowing product-specific outcome matrices.
    """

    code: str = Field(min_length=1)
    reason_code: str = ""


def workflow_effect_completed_outcome() -> WorkflowEffectOutcome:
    """Factory for framework-only tests and adapters without domain policy."""

    return WorkflowEffectOutcome(code="workflow_effect.completed")


def workflow_effect_warning_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level warning outcomes."""

    return WorkflowEffectOutcome(code="workflow_effect.completed_with_warnings", reason_code=reason_code)


def workflow_effect_waiting_external_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level resumable external waits."""

    return WorkflowEffectOutcome(code="workflow_effect.waiting_external", reason_code=reason_code)


def workflow_effect_waiting_agent_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level executable agent work."""

    return WorkflowEffectOutcome(code="workflow_effect.waiting_agent", reason_code=reason_code)


def workflow_effect_waiting_human_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level human-decision waits."""

    return WorkflowEffectOutcome(code="workflow_effect.waiting_human", reason_code=reason_code)


def workflow_effect_blocked_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level blocked outcomes."""

    return WorkflowEffectOutcome(code="workflow_effect.blocked", reason_code=reason_code)


def workflow_effect_failed_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for framework-level failed outcomes."""

    return WorkflowEffectOutcome(code="workflow_effect.failed", reason_code=reason_code)


def workflow_effect_skipped_outcome(*, reason_code: str = "") -> WorkflowEffectOutcome:
    """Factory for intentionally skipped effects."""

    return WorkflowEffectOutcome(code="workflow_effect.skipped", reason_code=reason_code)


class WorkflowEffectResult(ContractModel):
    """Typed result returned by the adapter that materialized one effect."""

    schema_id: str = Field(default="workflow-effect-result.v1", alias="schema")
    effect: WorkflowEffect
    status: WorkflowEffectStatus
    # Effect outcomes are polymorphic by design: adapters may return a stricter
    # domain model while the kernel only requires the generic code contract.
    outcome: SerializeAsAny[WorkflowEffectOutcome | ContractModel]
    public_summary: str = Field(min_length=1)
    developer_summary: str = Field(min_length=1)
    payload: JsonObject = Field(default_factory=dict)
    receipt: JsonObject | None = None
    attestation: JsonObject | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    error_context: JsonObject = Field(default_factory=dict)
    progress_events: list[WorkflowProgressEvent] = Field(default_factory=list)
    next_action: str = ""
    resume_action: str = ""

    @model_validator(mode="after")
    def _validate_result_contract(self) -> WorkflowEffectResult:
        if (
            self.status
            in {
                WorkflowEffectStatus.BLOCKED,
                WorkflowEffectStatus.FAILED,
                WorkflowEffectStatus.COMPLETED_WITH_WARNINGS,
            }
            and not self.next_action.strip()
        ):
            raise ValueError(f"{self.status} requires next_action")
        if self.status == WorkflowEffectStatus.WAITING_EXTERNAL:
            if not self.resume_action.strip():
                raise ValueError("waiting_external effect result requires resume_action")
            if not self.next_action.strip():
                object.__setattr__(self, "next_action", self.resume_action)
        if self.status == WorkflowEffectStatus.WAITING_HUMAN and self.human_decision_packet is None:
            raise ValueError("waiting_human effect result requires human_decision_packet")
        outcome_code = getattr(self.outcome, "code", "")
        if not isinstance(outcome_code, str) or not outcome_code.strip():
            raise ValueError("effect result outcome requires code")
        if (
            self.status in {WorkflowEffectStatus.COMPLETED, WorkflowEffectStatus.COMPLETED_WITH_WARNINGS}
            and self.effect.requires_receipt
            and self.receipt is None
        ):
            raise ValueError("completed effect result requires receipt")
        if (
            self.status in {WorkflowEffectStatus.COMPLETED, WorkflowEffectStatus.COMPLETED_WITH_WARNINGS}
            and self.effect.requires_attestation
            and self.attestation is None
        ):
            raise ValueError("effect result requires attestation")
        return self
