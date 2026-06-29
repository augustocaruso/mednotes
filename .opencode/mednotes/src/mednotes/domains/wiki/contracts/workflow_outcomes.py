"""Typed workflow outcome facade for Wiki domain callers.

This module deliberately does not reconstruct decisions from legacy
`status`/`blocked_reason` fields. A caller that wants a `WorkflowDecision` must
provide the typed `decision_summary` or a typed `human_decision_packet`; otherwise
the boundary fails closed and the emitting workflow must be fixed at the source.
"""
from __future__ import annotations

from pydantic import Field, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.workflow import (
    AutomationKind,
    DecisionEvidence,
    DecisionKind,
    HumanDecisionOption,
    HumanDecisionPacket,
    RejectedAutomation,
    WorkflowAutomationKind,
    WorkflowDecision,
    WorkflowDecisionKind,
    WorkflowDecisionSummary,
    WorkflowOutcomeError,
    attach_human_decision_packet,
)

__all__ = [
    "AutomationKind",
    "DecisionEvidence",
    "DecisionKind",
    "HumanDecisionOption",
    "HumanDecisionPacket",
    "RejectedAutomation",
    "WorkflowAutomationKind",
    "WorkflowDecision",
    "WorkflowDecisionKind",
    "WorkflowOutcomeError",
    "attach_human_decision_packet",
    "decision_payload_from_decision",
    "decision_from_payload",
]


class _WorkflowDecisionSourcePayload(ContractModel):
    """Typed subset used to recover an already-declared decision from JSON."""

    decision_summary: WorkflowDecisionSummary | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    next_action: StrictStr = ""
    required_inputs: list[StrictStr] = Field(default_factory=list)


def decision_from_payload(payload: JsonObject) -> WorkflowDecision:
    """Validate a typed decision already present in an operational payload."""
    try:
        return _decision_from_payload(_decision_source_payload(payload))
    except PydanticValidationError as exc:
        raise WorkflowOutcomeError(f"invalid workflow decision payload: {exc}") from exc


def _decision_source_payload(payload: JsonObject) -> _WorkflowDecisionSourcePayload:
    raw: dict[str, object] = {}
    for key in ("decision_summary", "human_decision_packet", "next_action", "required_inputs"):
        if key in payload:
            raw[key] = payload[key]
    return _WorkflowDecisionSourcePayload.model_validate(raw)


def _decision_from_payload(source: _WorkflowDecisionSourcePayload) -> WorkflowDecision:
    packet = source.human_decision_packet
    summary = source.decision_summary or (packet.decision_summary if packet is not None else None)
    if summary is None:
        raise WorkflowOutcomeError("decision_summary is required; legacy status fields cannot create decisions")

    next_action = source.next_action or (packet.resume_action if packet is not None else "")
    # Ask-human packets carry the closed option set and recommended option.
    # Reconstructing an intermediate WorkflowDecision without those fields would
    # trip the contract before the packet data is folded back in.
    if packet is not None:
        return WorkflowDecision(
            kind=summary.kind,
            phase=summary.phase,
            reason_code=summary.reason_code,
            public_summary=summary.public_summary,
            developer_summary=summary.developer_summary,
            evidence=summary.evidence,
            rejected_automations=packet.rejected_automations,
            next_action=next_action,
            required_inputs=list(source.required_inputs),
            human_decision_kind=packet.kind,
            resume_action=packet.resume_action,
            recommended_option_id=packet.recommended_option_id,
            options=packet.options,
        )
    return WorkflowDecision(
        kind=summary.kind,
        phase=summary.phase,
        reason_code=summary.reason_code,
        public_summary=summary.public_summary,
        developer_summary=summary.developer_summary,
        evidence=summary.evidence,
        rejected_automations=summary.rejected_automations,
        next_action=next_action,
        required_inputs=list(source.required_inputs),
    )


def decision_payload_from_decision(decision: WorkflowDecision) -> JsonObject:
    """Project a typed decision summary without manufacturing workflow state."""

    payload: JsonObject = {
        "decision_summary": decision.decision_summary(),
        "next_action": decision.next_action,
        "required_inputs": list(decision.required_inputs),
        "human_decision_required": decision.kind == WorkflowDecisionKind.ASK_HUMAN,
    }
    if decision.kind == WorkflowDecisionKind.ASK_HUMAN:
        payload["human_decision_packet"] = decision.to_human_decision_packet()
    return payload
