from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mednotes.kernel.base import ContractModel
from mednotes.kernel.workflow import HumanDecisionPacket


class PublishManifestNote(ContractModel):
    taxonomy: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_path: str = Field(min_length=1)
    safe_filename: str | None = None


class PublishManifestBatch(ContractModel):
    raw_file: str = Field(min_length=1)
    coverage_path: str | None = None
    note_plan_path: str | None = None
    raw_files: list[str] = Field(default_factory=list)
    notes: list[PublishManifestNote] = Field(min_length=1)
    batch_id: str | None = None
    run_id: str | None = None
    note_plan_hash: str | None = None
    coverage_hash: str | None = None
    source_artifact_hash: str | None = None
    human_decision_required: bool | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)

    @model_validator(mode="after")
    def pending_human_decision_is_explicit(self) -> PublishManifestBatch:
        if self.human_decision_required is True:
            return self
        if self.human_decision_packet is not None and self.human_decision_packet.status == "pending":
            object.__setattr__(self, "human_decision_required", True)
        if any(packet.status == "pending" for packet in self.human_decision_packets):
            object.__setattr__(self, "human_decision_required", True)
        return self


class PublishManifest(ContractModel):
    schema_id: str | None = Field(default=None, alias="schema")
    human_decision_required: bool | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)
    batches: list[PublishManifestBatch] = Field(min_length=1)

    @model_validator(mode="after")
    def manifest_must_have_notes(self) -> PublishManifest:
        if not self.batches:
            raise ValueError("publish manifest requires canonical batches[]")
        if self.human_decision_packet is not None and self.human_decision_packet.status == "pending":
            object.__setattr__(self, "human_decision_required", True)
        if any(packet.status == "pending" for packet in self.human_decision_packets):
            object.__setattr__(self, "human_decision_required", True)
        return self

    def require_coverage(self) -> None:
        missing = [batch.raw_file for batch in self.batches if not batch.coverage_path]
        if missing:
            raise ValueError(
                "publish manifest batches require coverage_path when require_coverage=True: "
                + ", ".join(missing)
            )

    def pending_human_decision(self) -> bool:
        if self.human_decision_required is True:
            return True
        if self.human_decision_packet is not None and self.human_decision_packet.status == "pending":
            return True
        if any(packet.status == "pending" for packet in self.human_decision_packets):
            return True
        return any(batch.human_decision_required is True for batch in self.batches)


class PublishReceiptItem(ContractModel):
    path: str = Field(min_length=1)
    status: Literal["published", "skipped", "blocked"]
    reason: str | None = None


class PublishErrorContext(ContractModel):
    root_cause: str = Field(min_length=1)
    affected_artifact: str = Field(min_length=1)
    error_summary: str = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    retry_scope: str = Field(min_length=1)
    phase: str | None = None
    blocked_reason: str | None = None
    details: dict[str, object] | None = None


class PublishReceipt(ContractModel):
    schema_id: Literal["medical-notes-workbench.publish-receipt.v1"] = Field(alias="schema")
    status: Literal["ready_to_publish", "published", "completed_with_link_blockers", "blocked"]
    batch_id: str = Field(min_length=1)
    published_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    items: list[PublishReceiptItem]
    next_action: str = ""
    error_context: PublishErrorContext | None = None

    @model_validator(mode="after")
    def blocked_requires_error_context(self) -> PublishReceipt:
        if self.status == "blocked" and self.error_context is None:
            raise ValueError("blocked publish receipts require error_context")
        return self
