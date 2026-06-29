"""Receipt builder for workflow outcomes."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.contracts.workflow_outcomes import WorkflowDecision
from mednotes.kernel.base import JsonObject
from mednotes.kernel.workflow import (
    HumanDecisionPacket,
    VersionControlSafety,
    WorkflowArtifact,
    WorkflowPhaseOutcome,
    WorkflowPhaseReceipt,
    WorkflowReceiptPayload,
    WorkflowRollback,
)


class WorkflowReceiptError(ValueError):
    """Raised when a workflow receipt violates the common contract."""


@dataclass
class WorkflowReceiptBuilder:
    schema: str
    workflow: str
    run_id: str
    phase_outcomes: list[WorkflowPhaseOutcome] = field(default_factory=list)
    phase_receipts: dict[str, WorkflowPhaseReceipt] = field(default_factory=dict)
    artifacts: list[WorkflowArtifact] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    rollback: WorkflowRollback | None = None
    no_resource_mutation: bool = False

    def add_phase(self, phase: str, *, decision: WorkflowDecision) -> None:
        human_packet = (
            HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
            if decision.kind == "ask_human"
            else None
        )
        self.phase_outcomes.append(
            WorkflowPhaseOutcome(
                phase=phase,
                decision_summary=decision.decision_summary(),
                human_decision_packet=human_packet,
            )
        )

    def add_phase_receipt(self, phase: str, receipt_path: str, *, status: str = "") -> None:
        self.phase_receipts[phase] = WorkflowPhaseReceipt(receipt_path=receipt_path, status=status)

    def add_artifact(self, kind: str, path: str) -> None:
        self.artifacts.append(WorkflowArtifact(kind=kind, path=path))

    def add_mutation_summary(self, *, changed_paths: list[str]) -> None:
        self.changed_files.extend(changed_paths)

    def add_rollback(self, strategy: str, value: str) -> None:
        self.rollback = WorkflowRollback(strategy=strategy, value=value)

    def finalize(self, *, status: str, next_action: str = "") -> JsonObject:
        if status in {"blocked", "failed", "completed_with_warnings", "completed_with_link_blockers"} and not next_action:
            raise WorkflowReceiptError(f"{status} receipt requires next_action")
        mutated = bool(self.changed_files)
        if mutated and not self.rollback and not self.no_resource_mutation:
            raise WorkflowReceiptError("mutated receipt requires rollback or no_resource_mutation")
        try:
            phase_outcomes = [WorkflowPhaseOutcome.model_validate(item) for item in self.phase_outcomes]
            phase_receipts = {
                phase: WorkflowPhaseReceipt.model_validate(receipt) for phase, receipt in self.phase_receipts.items()
            }
            artifacts = [WorkflowArtifact.model_validate(item) for item in self.artifacts]
            rollback = WorkflowRollback.model_validate(self.rollback) if self.rollback else None
            human_packets = [outcome.human_decision_packet for outcome in phase_outcomes if outcome.human_decision_packet]
            receipt = WorkflowReceiptPayload(
                schema=self.schema,
                workflow=self.workflow,
                run_id=self.run_id,
                status=status,  # type: ignore[arg-type]
                mutated=mutated,
                next_action=next_action,
                human_decision_required=bool(human_packets),
                human_decision_packet=human_packets[0] if human_packets else None,
                phase_outcomes=phase_outcomes,
                phase_receipts=phase_receipts,
                artifacts=artifacts,
                changed_files=list(self.changed_files),
                rollback=rollback,
                version_control_safety=VersionControlSafety(
                    no_resource_mutation=self.no_resource_mutation,
                    rollback_declared=bool(self.rollback),
                ),
            )
        except PydanticValidationError as exc:
            raise WorkflowReceiptError(f"invalid receipt payload: {exc}") from exc
        payload = receipt.to_payload()
        payload["rollback"] = payload["rollback"] or {}
        return payload
