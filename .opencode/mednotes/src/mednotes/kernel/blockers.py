from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from mednotes.kernel.base import ContractModel
from mednotes.kernel.workflow import WorkflowDecisionKind


def _non_empty(value: str, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    return cleaned


class BlockerEntryModel(ContractModel):
    code: str
    owner_phase: str
    default_decision: WorkflowDecisionKind
    safe_to_continue_batch: bool
    requires_human_packet: bool
    public_label: str
    public_explanation: str
    developer_explanation: str
    test_fixture: str

    @field_validator("code", "owner_phase", "public_label", "public_explanation", "developer_explanation", "test_fixture")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty(value, str(info.field_name))

    @model_validator(mode="after")
    def _validate_human_packet_policy(self) -> BlockerEntryModel:
        if self.default_decision == WorkflowDecisionKind.ASK_HUMAN and not self.requires_human_packet:
            raise ValueError("requires_human_packet must be true when default_decision is ask_human")
        if self.default_decision != WorkflowDecisionKind.ASK_HUMAN and self.requires_human_packet:
            raise ValueError("requires_human_packet is only valid for ask_human blockers")
        return self
