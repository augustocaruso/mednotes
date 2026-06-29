"""Operational StateChart for `/mednotes:process-chats`.

This module is pure domain code: it defines workflow states, typed boundary
events and executable effect intents. Actions never execute IO; adapters consume
the emitted `WorkflowEffect` objects outside the statechart.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, TypedDict

from pydantic import Field, TypeAdapter, field_validator
from statemachine import StateChart
from statemachine.states import States

from mednotes.domains.wiki.contracts.effect_payloads import LinkWorkflowRunEffectPayload, WaitExternalEffectPayload
from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind, WorkflowEffectResult
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.state_machine import WorkflowStateCategory
from mednotes.kernel.workflow import (
    DecisionEvidence,
    HumanDecisionOption,
    HumanDecisionPacket,
    RejectedAutomation,
    WorkflowAutomationKind,
    WorkflowDecision,
    WorkflowDecisionKind,
)

PROCESS_CHATS_WORKFLOW: Literal["/mednotes:process-chats"] = "/mednotes:process-chats"


class _ProcessChatsEventCommonKwargs(TypedDict):
    workflow: str
    run_id: str
    current_state: str


class ProcessChatsState(StrEnum):
    ENVIRONMENT_CHECKING = "environment.checking"
    ENVIRONMENT_PATHS_MISSING = "environment.paths_missing"
    ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED = "environment.windows_path_or_venv_blocked"
    BACKLOG_NO_PENDING_RAW_CHATS = "backlog.no_pending_raw_chats"
    BACKLOG_NO_TRIAGED_RAW_CHATS = "backlog.no_triaged_raw_chats"
    BACKLOG_TRIAGED_RAW_CHATS_READY = "backlog.triaged_raw_chats_ready"
    VAULT_GUARD_DECISION_REQUIRED = "vault_guard.decision_required"
    VAULT_GUARD_REJECTED = "vault_guard.rejected"
    TRIAGE_PLANNING = "triage.planning"
    ARCHITECT_WORK_REQUESTED = "architect.work_requested"
    ARCHITECT_AWAITING_SPECIALIST_CAPACITY = "architect.awaiting_specialist_capacity"
    ARCHITECT_REVIEWING_OUTPUT = "architect.reviewing_output"
    SUBAGENT_PLAN_ATTESTATION_REQUIRED = "subagent_plan_attestation.required"
    SUBAGENT_PLAN_ATTESTATION_INVALID = "subagent_plan_attestation.invalid"
    NOTE_VALIDATION_RUNNING = "note_validation.running"
    NOTE_VALIDATION_COVERAGE_GAP = "note_validation.coverage_gap"
    NOTE_VALIDATION_MANIFEST_MISMATCH = "note_validation.manifest_mismatch"
    NOTE_VALIDATION_CONTENT_INVALID = "note_validation.content_invalid"
    STAGING_MANIFEST_READY = "staging.manifest_ready"
    PUBLISH_AWAITING_CONFIRMATION = "publish.awaiting_confirmation"
    PUBLISH_CANCELLED_BY_HUMAN = "publish.cancelled_by_human"
    PUBLISH_APPLY_REQUESTED = "publish.apply_requested"
    PUBLISH_PAUSED_FOR_QUOTA = "publish.paused_for_quota"
    PUBLISH_DRY_RUN_RECEIPT_REQUIRED = "publish.dry_run_receipt_required"
    PUBLISH_STALE_RECEIPT = "publish.stale_receipt"
    PUBLISH_DUPLICATE_TARGET = "publish.duplicate_target"
    PUBLISH_PROVENANCE_GAP = "publish.provenance_gap"
    PUBLISH_RECEIPT_INVALID = "publish.receipt_invalid"
    LINK_RUN_REQUESTED = "link.run_requested"
    CONTRACT_GAP_MISSING_NEXT_ACTION = "contract_gap.missing_next_action"
    CONTRACT_GAP_MISSING_ERROR_CONTEXT = "contract_gap.missing_error_context"
    AGENT_TOOL_CONTRACT_VIOLATION = "agent_tool_contract_violation"
    ROLLBACK_RECORDED = "rollback.recorded"
    PUBLISHED = "published"
    COMPLETED_WITH_LINK_BLOCKERS = "completed_with_link_blockers"
    TERMINAL_FAILURE_RECORDED = "terminal.failure_recorded"


class ProcessChatsErrorContext(ContractModel):
    """Typed problem context passed across retries without reading raw payloads."""

    root_cause: str = Field(min_length=1)
    affected_artifact: str = Field(min_length=1)
    retry_scope: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class ProcessChatsEvent(ContractModel):
    """Base event for process-chats facts accepted by the StateChart."""

    workflow: str = PROCESS_CHATS_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_process_chats(cls, value: str) -> str:
        if value != PROCESS_CHATS_WORKFLOW:
            raise ValueError(f"process-chats event workflow must be {PROCESS_CHATS_WORKFLOW}")
        return value


def _event_name(event: ProcessChatsEvent) -> str:
    """Return the concrete Literal discriminator declared by each event class."""

    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("process-chats events must declare a name discriminator")
    return name


class EnvironmentCheckedEvent(ProcessChatsEvent):
    name: Literal["environment_checked"] = "environment_checked"
    wiki_dir_configured: bool
    raw_dir_configured: bool


class PathsMissingEvent(ProcessChatsEvent):
    name: Literal["paths_missing"] = "paths_missing"
    reason_code: Literal["paths_missing", "wiki_dir_missing"]
    missing_path_kind: Literal["wiki_dir", "raw_dir", "both"]
    setup_target: Literal["/mednotes:setup paths"]


class PathsConfiguredEvent(ProcessChatsEvent):
    name: Literal["paths_configured"] = "paths_configured"
    config_path: str = Field(min_length=1)


class WindowsPathOrVenvBlockedEvent(ProcessChatsEvent):
    name: Literal["windows_path_or_venv_blocked"] = "windows_path_or_venv_blocked"
    reason_code: Literal["environment_blocker.windows_path_or_venv"]
    setup_target: Literal["/mednotes:setup bootstrap"]
    error_context: ProcessChatsErrorContext


class EnvironmentBootstrapCompletedEvent(ProcessChatsEvent):
    name: Literal["environment_bootstrap_completed"] = "environment_bootstrap_completed"
    bootstrap_summary: str = Field(min_length=1)


class NoPendingRawChatsEvent(ProcessChatsEvent):
    name: Literal["no_pending_raw_chats"] = "no_pending_raw_chats"
    triaged_count: int = Field(default=0, ge=0, strict=True)


class NoTriagedRawChatsEvent(ProcessChatsEvent):
    name: Literal["no_triaged_raw_chats"] = "no_triaged_raw_chats"
    pending_count: int = Field(default=0, ge=0, strict=True)


class TriagedRawChatsAvailableEvent(ProcessChatsEvent):
    name: Literal["triaged_raw_chats_available"] = "triaged_raw_chats_available"
    triaged_count: int = Field(ge=1, strict=True)


class TriagePlanCreatedEvent(ProcessChatsEvent):
    name: Literal["triage_plan_created"] = "triage_plan_created"
    note_plan_hash: str = Field(min_length=1)
    raw_file_count: int = Field(ge=0, strict=True)
    exhaustive: bool


class SubagentPlanAttestationMissingEvent(ProcessChatsEvent):
    name: Literal["subagent_plan_attestation_missing"] = "subagent_plan_attestation_missing"
    reason_code: Literal["subagent_plan_attestation_required"]
    error_context: ProcessChatsErrorContext


class SubagentPlanAttestationInvalidEvent(ProcessChatsEvent):
    name: Literal["subagent_plan_attestation_invalid"] = "subagent_plan_attestation_invalid"
    reason_code: Literal["subagent_plan_attestation_invalid"]
    error_context: ProcessChatsErrorContext


class SubagentPlanAttestationSuppliedEvent(ProcessChatsEvent):
    name: Literal["subagent_plan_attestation_supplied"] = "subagent_plan_attestation_supplied"
    attestation_hash: str = Field(min_length=1)


class ArchitectSpecialistCapacityBlockedEvent(ProcessChatsEvent):
    name: Literal["architect_specialist_capacity_blocked"] = "architect_specialist_capacity_blocked"
    reason_code: Literal["specialist_model_unavailable", "specialist_model_quota_exhausted"]
    resume_action: str = Field(min_length=1)


class ArchitectSpecialistCapacityRestoredEvent(ProcessChatsEvent):
    name: Literal["architect_specialist_capacity_restored"] = "architect_specialist_capacity_restored"
    restored_by: str = Field(min_length=1)


class ArchitectWorkCompletedEvent(ProcessChatsEvent):
    name: Literal["architect_work_completed"] = "architect_work_completed"
    receipt_id: str = Field(min_length=1)
    attestation_hash: str = Field(min_length=1)
    coverage_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)


class ArchitectOutputAcceptedEvent(ProcessChatsEvent):
    name: Literal["architect_output_accepted"] = "architect_output_accepted"
    coverage_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    note_plan_hash: str = Field(min_length=1)


class ArchitectOutputInvalidEvent(ProcessChatsEvent):
    name: Literal["architect_output_invalid"] = "architect_output_invalid"
    reason_code: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class NoteValidationCoverageGapEvent(ProcessChatsEvent):
    name: Literal["note_validation_coverage_gap"] = "note_validation_coverage_gap"
    reason_code: Literal["coverage_path_missing", "coverage_invalid"]
    error_context: ProcessChatsErrorContext


class NoteValidationManifestMismatchEvent(ProcessChatsEvent):
    name: Literal["note_validation_manifest_mismatch"] = "note_validation_manifest_mismatch"
    reason_code: Literal["manifest_invalid", "manifest_mismatch"]
    error_context: ProcessChatsErrorContext


class NoteValidationContentInvalidEvent(ProcessChatsEvent):
    name: Literal["note_validation_content_invalid"] = "note_validation_content_invalid"
    reason_code: Literal["ValidationError", "validation_errors", "validation_failed", "requires_llm_rewrite"]
    error_context: ProcessChatsErrorContext


class NoteValidationRetryRequestedEvent(ProcessChatsEvent):
    name: Literal["note_validation_retry_requested"] = "note_validation_retry_requested"
    resolved_by: str = Field(min_length=1)


class NotesValidatedEvent(ProcessChatsEvent):
    name: Literal["notes_validated"] = "notes_validated"
    manifest_path: str = Field(min_length=1)
    coverage_path: str = Field(min_length=1)
    staged_note_count: int = Field(ge=0, strict=True)


class PublishPreviewProducedEvent(ProcessChatsEvent):
    name: Literal["publish_preview_produced"] = "publish_preview_produced"
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)


class HumanPublishApprovalEvent(ProcessChatsEvent):
    name: Literal["publish_approved_by_human"] = "publish_approved_by_human"
    approved_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class HumanPublishCancellationEvent(ProcessChatsEvent):
    name: Literal["publish_cancelled_by_human"] = "publish_cancelled_by_human"
    cancelled_by: str = Field(min_length=1)


class PublishBatchCompletedEvent(ProcessChatsEvent):
    name: Literal["publish_batch_completed"] = "publish_batch_completed"
    receipt_id: str = Field(min_length=1)
    published_count: int = Field(ge=0, strict=True)
    link_trigger_context_path: str = Field(min_length=1)


class PublishDryRunReceiptRequiredEvent(ProcessChatsEvent):
    name: Literal["publish_dry_run_receipt_required"] = "publish_dry_run_receipt_required"
    reason_code: Literal["dry_run_receipt_required"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishStaleReceiptEvent(ProcessChatsEvent):
    name: Literal["publish_stale_receipt"] = "publish_stale_receipt"
    reason_code: Literal["dry_run_receipt_invalid", "new_taxonomy_leaf_requires_dry_run_authorization", "stale_receipt"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishDuplicateTargetEvent(ProcessChatsEvent):
    name: Literal["publish_duplicate_target"] = "publish_duplicate_target"
    reason_code: Literal["duplicate_target", "duplicate_obsidian_target"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishProvenanceGapEvent(ProcessChatsEvent):
    name: Literal["publish_provenance_gap"] = "publish_provenance_gap"
    reason_code: Literal["provenance_gap"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishReceiptInvalidEvent(ProcessChatsEvent):
    name: Literal["publish_receipt_invalid"] = "publish_receipt_invalid"
    reason_code: Literal["publish_receipt_invalid"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishBlockerResolvedEvent(ProcessChatsEvent):
    name: Literal["publish_blocker_resolved"] = "publish_blocker_resolved"
    resolved_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class ExternalQuotaReportedEvent(ProcessChatsEvent):
    name: Literal["external_quota_reported"] = "external_quota_reported"
    quota_kind: Literal["publish_batch"]
    resume_action: str = Field(min_length=1)


class ExternalReadyEvent(ProcessChatsEvent):
    name: Literal["external_ready"] = "external_ready"
    restored_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class LinkRunCompletedEvent(ProcessChatsEvent):
    name: Literal["link_run_completed"] = "link_run_completed"
    receipt_id: str = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)


class LinkRunBlockedEvent(ProcessChatsEvent):
    name: Literal["link_run_blocked"] = "link_run_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class ProcessChatsPublishRuntimeObservation(ContractModel):
    """Typed publish/link facts; ProcessChatsMachine owns leaf-state priority."""

    source_state: ProcessChatsState
    preview_ready: bool = False
    publish_completed: bool = False
    link_completed: bool = False
    link_blocked: bool = False
    rollback_recorded: bool = False
    blocked: bool = False
    quota_wait: bool = False
    validation_coverage_gap: bool = False
    validation_manifest_mismatch: bool = False
    validation_content_invalid: bool = False
    publish_dry_run_receipt_required: bool = False
    publish_stale_receipt: bool = False
    publish_duplicate_target: bool = False
    publish_provenance_gap: bool = False
    reason_code: str = ""
    next_action: str = ""
    manifest_path: str = ""
    dry_run_receipt_path: str = ""
    receipt_id: str = ""
    published_count: int = Field(default=0, ge=0, strict=True)
    link_trigger_context_path: str = ""
    link_receipt_id: str = ""
    link_changed_files: list[str] = Field(default_factory=list)
    error_context: ProcessChatsErrorContext | None = None


class ProcessChatsPublishRuntimeObservedEvent(ProcessChatsEvent):
    """Single publish-runtime observation event consumed by ProcessChatsMachine."""

    name: Literal["publish_runtime_observed"] = "publish_runtime_observed"
    observation: ProcessChatsPublishRuntimeObservation


class MissingNextActionEvent(ProcessChatsEvent):
    name: Literal["missing_next_action"] = "missing_next_action"
    contract_source: str = Field(min_length=1)
    next_action_hint: str = Field(min_length=1)


class MissingErrorContextEvent(ProcessChatsEvent):
    name: Literal["missing_error_context"] = "missing_error_context"
    contract_source: str = Field(min_length=1)
    error_context_hint: str = Field(min_length=1)


class AgentToolContractViolationEvent(ProcessChatsEvent):
    name: Literal["agent_tool_contract_violation"] = "agent_tool_contract_violation"
    origin_event: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class VaultGuardRequiredEvent(ProcessChatsEvent):
    name: Literal["vault_guard_required"] = "vault_guard_required"
    reason_code: Literal["vault_guard_required"]
    changed_file_count: int = Field(ge=0, strict=True)


class VaultGuardConfirmedEvent(ProcessChatsEvent):
    name: Literal["vault_guard_confirmed"] = "vault_guard_confirmed"
    confirmed_by: str = Field(min_length=1)


class VaultGuardRejectedEvent(ProcessChatsEvent):
    name: Literal["vault_guard_rejected"] = "vault_guard_rejected"
    rejected_by: str = Field(min_length=1)


class RollbackCompletedEvent(ProcessChatsEvent):
    name: Literal["rollback_completed"] = "rollback_completed"
    rollback_receipt_id: str = Field(min_length=1)


class RollbackFailureRecordedEvent(ProcessChatsEvent):
    name: Literal["rollback_failure_recorded"] = "rollback_failure_recorded"
    error_context: ProcessChatsErrorContext


ProcessChatsBoundaryEvent = Annotated[
    EnvironmentCheckedEvent
    | PathsMissingEvent
    | PathsConfiguredEvent
    | WindowsPathOrVenvBlockedEvent
    | EnvironmentBootstrapCompletedEvent
    | NoPendingRawChatsEvent
    | NoTriagedRawChatsEvent
    | TriagedRawChatsAvailableEvent
    | TriagePlanCreatedEvent
    | SubagentPlanAttestationMissingEvent
    | SubagentPlanAttestationInvalidEvent
    | SubagentPlanAttestationSuppliedEvent
    | ArchitectSpecialistCapacityBlockedEvent
    | ArchitectSpecialistCapacityRestoredEvent
    | ArchitectWorkCompletedEvent
    | ArchitectOutputAcceptedEvent
    | ArchitectOutputInvalidEvent
    | NoteValidationCoverageGapEvent
    | NoteValidationManifestMismatchEvent
    | NoteValidationContentInvalidEvent
    | NoteValidationRetryRequestedEvent
    | NotesValidatedEvent
    | PublishPreviewProducedEvent
    | HumanPublishApprovalEvent
    | HumanPublishCancellationEvent
    | PublishBatchCompletedEvent
    | PublishDryRunReceiptRequiredEvent
    | PublishStaleReceiptEvent
    | PublishDuplicateTargetEvent
    | PublishProvenanceGapEvent
    | PublishReceiptInvalidEvent
    | PublishBlockerResolvedEvent
    | ExternalQuotaReportedEvent
    | ExternalReadyEvent
    | LinkRunCompletedEvent
    | LinkRunBlockedEvent
    | ProcessChatsPublishRuntimeObservedEvent
    | MissingNextActionEvent
    | MissingErrorContextEvent
    | AgentToolContractViolationEvent
    | VaultGuardRequiredEvent
    | VaultGuardConfirmedEvent
    | VaultGuardRejectedEvent
    | RollbackCompletedEvent
    | RollbackFailureRecordedEvent,
    Field(discriminator="name"),
]
ProcessChatsBoundaryEventAdapter = TypeAdapter(ProcessChatsBoundaryEvent)


class SetupPathsEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.setup-paths-effect.v1"] = (
        "medical-notes-workbench.process-chats.setup-paths-effect.v1"
    )
    kind: Literal["setup_paths"] = "setup_paths"
    missing_path_kind: Literal["wiki_dir", "raw_dir", "both"]


class SetupBootstrapEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.setup-bootstrap-effect.v1"] = (
        "medical-notes-workbench.process-chats.setup-bootstrap-effect.v1"
    )
    kind: Literal["setup_bootstrap"] = "setup_bootstrap"
    reason_code: Literal["environment_blocker.windows_path_or_venv"]


class ArchitectSpecialistEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.architect-effect.v1"] = (
        "medical-notes-workbench.process-chats.architect-effect.v1"
    )
    kind: Literal["architect_specialist"] = "architect_specialist"
    note_plan_hash: str
    raw_file_count: int = Field(ge=0, strict=True)


class ArchitectPlanningEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.architect-planning-effect.v1"] = (
        "medical-notes-workbench.process-chats.architect-planning-effect.v1"
    )
    kind: Literal["architect_planning"] = "architect_planning"
    triaged_count: int = Field(ge=1, strict=True)
    command: Literal["plan-subagents --phase architect"] = "plan-subagents --phase architect"


class PlanAttestationEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.plan-attestation-effect.v1"] = (
        "medical-notes-workbench.process-chats.plan-attestation-effect.v1"
    )
    kind: Literal["plan_attestation"] = "plan_attestation"
    reason_code: str = Field(min_length=1)


class ResumeArchitectWorkEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.resume-architect-effect.v1"] = (
        "medical-notes-workbench.process-chats.resume-architect-effect.v1"
    )
    kind: Literal["resume_architect_work"] = "resume_architect_work"
    resolved_by: str = Field(min_length=1)


class ResumePublishBlockerEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.resume-publish-blocker-effect.v1"] = (
        "medical-notes-workbench.process-chats.resume-publish-blocker-effect.v1"
    )
    kind: Literal["resume_publish_blocker"] = "resume_publish_blocker"
    reason_code: str = Field(min_length=1)


class VaultGuardDecisionEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.vault-guard-decision-effect.v1"] = (
        "medical-notes-workbench.process-chats.vault-guard-decision-effect.v1"
    )
    kind: Literal["vault_guard_decision"] = "vault_guard_decision"
    changed_file_count: int = Field(ge=0, strict=True)


class HumanPublishDecisionEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.publish-decision-effect.v1"] = (
        "medical-notes-workbench.process-chats.publish-decision-effect.v1"
    )
    kind: Literal["publish_decision"] = "publish_decision"
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class PublishBatchEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.publish-batch-effect.v1"]
    kind: Literal["publish_batch"]
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class RollbackEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.rollback-effect.v1"] = (
        "medical-notes-workbench.process-chats.rollback-effect.v1"
    )
    kind: Literal["rollback"] = "rollback"
    failed_origin_state: str = Field(min_length=1)


class FailureFinalizationEffectPayload(ContractModel):
    schema_version: Literal["medical-notes-workbench.process-chats.failure-finalization-effect.v1"] = (
        "medical-notes-workbench.process-chats.failure-finalization-effect.v1"
    )
    kind: Literal["failure_finalization"] = "failure_finalization"
    rollback_state: Literal["rollback.recorded"] = ProcessChatsState.ROLLBACK_RECORDED.value


ProcessChatsEffectPayload = Annotated[
    SetupPathsEffectPayload
    | SetupBootstrapEffectPayload
    | ArchitectSpecialistEffectPayload
    | ArchitectPlanningEffectPayload
    | WaitExternalEffectPayload
    | PlanAttestationEffectPayload
    | ResumeArchitectWorkEffectPayload
    | ResumePublishBlockerEffectPayload
    | VaultGuardDecisionEffectPayload
    | HumanPublishDecisionEffectPayload
    | PublishBatchEffectPayload
    | LinkWorkflowRunEffectPayload
    | RollbackEffectPayload
    | FailureFinalizationEffectPayload,
    Field(discriminator="kind"),
]
ProcessChatsEffectPayloadAdapter = TypeAdapter(ProcessChatsEffectPayload)


class SetupPathsConfiguredOutcome(ContractModel):
    code: Literal["setup.paths_configured"] = "setup.paths_configured"
    config_path: str = Field(min_length=1)


class SetupBootstrapCompletedOutcome(ContractModel):
    code: Literal["setup.bootstrap_completed"] = "setup.bootstrap_completed"
    bootstrap_summary: str = Field(min_length=1)


class ArchitectCompletedOutcome(ContractModel):
    code: Literal["architect.completed"] = "architect.completed"
    receipt_id: str = Field(min_length=1)
    attestation_hash: str = Field(min_length=1)
    coverage_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)


class ArchitectCapacityBlockedOutcome(ContractModel):
    code: Literal["architect.capacity_blocked"] = "architect.capacity_blocked"
    reason_code: Literal["specialist_model_unavailable", "specialist_model_quota_exhausted"]
    resume_action: str = Field(min_length=1)


class AgentToolContractViolationOutcome(ContractModel):
    code: Literal["agent_tool_contract_violation"] = "agent_tool_contract_violation"
    origin_event: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class ExternalReadyOutcome(ContractModel):
    code: Literal["external.ready"] = "external.ready"
    restored_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class AttestationSuppliedOutcome(ContractModel):
    code: Literal["attestation.supplied"] = "attestation.supplied"
    attestation_hash: str = Field(min_length=1)


class ValidationRetryRequestedOutcome(ContractModel):
    code: Literal["validation.retry_requested"] = "validation.retry_requested"
    resolved_by: str = Field(min_length=1)


class HumanConfirmedOutcome(ContractModel):
    code: Literal["human.confirmed"] = "human.confirmed"
    confirmed_by: str = Field(min_length=1)


class HumanRejectedOutcome(ContractModel):
    code: Literal["human.rejected"] = "human.rejected"
    rejected_by: str = Field(min_length=1)


class HumanApprovedOutcome(ContractModel):
    code: Literal["human.approved"] = "human.approved"
    approved_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class HumanCancelledOutcome(ContractModel):
    code: Literal["human.cancelled"] = "human.cancelled"
    cancelled_by: str = Field(min_length=1)


class PublishBatchCompletedOutcome(ContractModel):
    code: Literal["publish.completed"] = "publish.completed"
    receipt_id: str = Field(min_length=1)
    published_count: int = Field(ge=0, strict=True)
    link_trigger_context_path: str = Field(min_length=1)


class PublishDryRunReceiptRequiredOutcome(ContractModel):
    code: Literal["publish.dry_run_receipt_required"] = "publish.dry_run_receipt_required"
    reason_code: Literal["dry_run_receipt_required"] = "dry_run_receipt_required"
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishStaleReceiptOutcome(ContractModel):
    code: Literal["publish.stale_receipt"] = "publish.stale_receipt"
    reason_code: Literal["dry_run_receipt_invalid", "new_taxonomy_leaf_requires_dry_run_authorization", "stale_receipt"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishDuplicateTargetOutcome(ContractModel):
    code: Literal["publish.duplicate_target"] = "publish.duplicate_target"
    reason_code: Literal["duplicate_target", "duplicate_obsidian_target"]
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishProvenanceGapOutcome(ContractModel):
    code: Literal["publish.provenance_gap"] = "publish.provenance_gap"
    reason_code: Literal["provenance_gap"] = "provenance_gap"
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class PublishReceiptInvalidOutcome(ContractModel):
    code: Literal["publish.receipt_invalid"] = "publish.receipt_invalid"
    reason_code: Literal["publish_receipt_invalid"] = "publish_receipt_invalid"
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class ExternalQuotaReportedOutcome(ContractModel):
    code: Literal["external.quota_reported"] = "external.quota_reported"
    quota_kind: Literal["publish_batch"]
    resume_action: str = Field(min_length=1)


class PublishBlockerResolvedOutcome(ContractModel):
    code: Literal["publish.blocker_resolved"] = "publish.blocker_resolved"
    resolved_by: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    dry_run_receipt_path: str = Field(min_length=1)


class LinkCompletedOutcome(ContractModel):
    code: Literal["link.completed"] = "link.completed"
    receipt_id: str = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)


class LinkBlockedOutcome(ContractModel):
    code: Literal["link.blocked"] = "link.blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    error_context: ProcessChatsErrorContext


class RollbackCompletedOutcome(ContractModel):
    code: Literal["rollback.completed"] = "rollback.completed"
    rollback_receipt_id: str = Field(min_length=1)


class RollbackFailureRecordedOutcome(ContractModel):
    code: Literal["rollback.failure_recorded"] = "rollback.failure_recorded"
    error_context: ProcessChatsErrorContext


class ContractMissingNextActionOutcome(ContractModel):
    code: Literal["contract_gap.missing_next_action"] = "contract_gap.missing_next_action"
    next_action_hint: str = Field(min_length=1)


class ContractMissingErrorContextOutcome(ContractModel):
    code: Literal["contract_gap.missing_error_context"] = "contract_gap.missing_error_context"
    error_context_hint: str = Field(min_length=1)


ProcessChatsEffectOutcome = Annotated[
    SetupPathsConfiguredOutcome
    | SetupBootstrapCompletedOutcome
    | ArchitectCompletedOutcome
    | ArchitectCapacityBlockedOutcome
    | AgentToolContractViolationOutcome
    | ExternalReadyOutcome
    | AttestationSuppliedOutcome
    | ValidationRetryRequestedOutcome
    | HumanConfirmedOutcome
    | HumanRejectedOutcome
    | HumanApprovedOutcome
    | HumanCancelledOutcome
    | PublishBatchCompletedOutcome
    | PublishDryRunReceiptRequiredOutcome
    | PublishStaleReceiptOutcome
    | PublishDuplicateTargetOutcome
    | PublishProvenanceGapOutcome
    | PublishReceiptInvalidOutcome
    | ExternalQuotaReportedOutcome
    | PublishBlockerResolvedOutcome
    | LinkCompletedOutcome
    | LinkBlockedOutcome
    | RollbackCompletedOutcome
    | RollbackFailureRecordedOutcome
    | ContractMissingNextActionOutcome
    | ContractMissingErrorContextOutcome,
    Field(discriminator="code"),
]
ProcessChatsEffectOutcomeAdapter = TypeAdapter(ProcessChatsEffectOutcome)


@dataclass(frozen=True)
class ProcessChatsEffectReturnEventRow:
    """One authorized adapter outcome -> boundary event edge."""

    kind: WorkflowEffectKind
    target: str
    origin_state: str
    outcome_code: str
    event_name: str
    event_model: type[ProcessChatsEvent]


@dataclass(frozen=True)
class ProcessChatsEffectReturnEventMatrix:
    """Exact lookup table for effect-result conversion; no status guessing."""

    rows: tuple[ProcessChatsEffectReturnEventRow, ...]

    def lookup(
        self,
        *,
        kind: WorkflowEffectKind,
        target: str,
        origin_state: str,
        outcome_code: str,
    ) -> ProcessChatsEffectReturnEventRow:
        for row in self.rows:
            if (
                row.kind == kind
                and row.target == target
                and row.origin_state == origin_state
                and row.outcome_code == outcome_code
            ):
                return row
        raise ValueError(
            "no process-chats effect return row for "
            f"{kind.value}:{target}:{origin_state}:{outcome_code}"
        )

    def outcome_codes(self) -> set[str]:
        return {row.outcome_code for row in self.rows}


def _row(
    kind: WorkflowEffectKind,
    target: str,
    origin: ProcessChatsState,
    outcome_code: str,
    event_name: str,
    event_model: type[ProcessChatsEvent],
) -> ProcessChatsEffectReturnEventRow:
    return ProcessChatsEffectReturnEventRow(
        kind=kind,
        target=target,
        origin_state=origin.value,
        outcome_code=outcome_code,
        event_name=event_name,
        event_model=event_model,
    )


PROCESS_CHATS_EFFECT_RETURN_EVENT_MATRIX = ProcessChatsEffectReturnEventMatrix(
    rows=(
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:setup paths",
            ProcessChatsState.ENVIRONMENT_PATHS_MISSING,
            "setup.paths_configured",
            "paths_configured",
            PathsConfiguredEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:setup paths",
            ProcessChatsState.ENVIRONMENT_PATHS_MISSING,
            "contract_gap.missing_next_action",
            "missing_next_action",
            MissingNextActionEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:setup bootstrap",
            ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
            "setup.bootstrap_completed",
            "environment_bootstrap_completed",
            EnvironmentBootstrapCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:setup bootstrap",
            ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
        _row(
            WorkflowEffectKind.CALL_SPECIALIST_MODEL,
            "med-knowledge-architect",
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "architect.completed",
            "architect_work_completed",
            ArchitectWorkCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.CALL_SPECIALIST_MODEL,
            "med-knowledge-architect",
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "architect.capacity_blocked",
            "architect_specialist_capacity_blocked",
            ArchitectSpecialistCapacityBlockedEvent,
        ),
        _row(
            WorkflowEffectKind.CALL_SPECIALIST_MODEL,
            "med-knowledge-architect",
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "agent_tool_contract_violation",
            "agent_tool_contract_violation",
            AgentToolContractViolationEvent,
        ),
        _row(
            WorkflowEffectKind.WAIT_EXTERNAL,
            "wait_external.specialist_capacity",
            ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY,
            "external.ready",
            "architect_specialist_capacity_restored",
            ArchitectSpecialistCapacityRestoredEvent,
        ),
        _row(
            WorkflowEffectKind.WAIT_EXTERNAL,
            "wait_external.specialist_capacity",
            ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY,
            "contract_gap.missing_next_action",
            "missing_next_action",
            MissingNextActionEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "agent.plan_attestation",
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
            "attestation.supplied",
            "subagent_plan_attestation_supplied",
            SubagentPlanAttestationSuppliedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "agent.plan_attestation",
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "agent.plan_attestation",
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID,
            "attestation.supplied",
            "subagent_plan_attestation_supplied",
            SubagentPlanAttestationSuppliedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "agent.plan_attestation",
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
        *(
            _row(
                WorkflowEffectKind.RUN_SUBWORKFLOW,
                "workflow.resume_architect_work",
                origin,
                "validation.retry_requested",
                "note_validation_retry_requested",
                NoteValidationRetryRequestedEvent,
            )
            for origin in (
                ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP,
                ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH,
                ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID,
            )
        ),
        *(
            _row(
                WorkflowEffectKind.RUN_SUBWORKFLOW,
                "workflow.resume_architect_work",
                origin,
                "contract_gap.missing_error_context",
                "missing_error_context",
                MissingErrorContextEvent,
            )
            for origin in (
                ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP,
                ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH,
                ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID,
            )
        ),
        _row(
            WorkflowEffectKind.ASK_HUMAN,
            "human.vault_guard_decision",
            ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED,
            "human.confirmed",
            "vault_guard_confirmed",
            VaultGuardConfirmedEvent,
        ),
        _row(
            WorkflowEffectKind.ASK_HUMAN,
            "human.vault_guard_decision",
            ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED,
            "human.rejected",
            "vault_guard_rejected",
            VaultGuardRejectedEvent,
        ),
        _row(
            WorkflowEffectKind.ASK_HUMAN,
            "human.publish_decision",
            ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION,
            "human.approved",
            "publish_approved_by_human",
            HumanPublishApprovalEvent,
        ),
        _row(
            WorkflowEffectKind.ASK_HUMAN,
            "human.publish_decision",
            ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION,
            "human.cancelled",
            "publish_cancelled_by_human",
            HumanPublishCancellationEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.completed",
            "publish_batch_completed",
            PublishBatchCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.dry_run_receipt_required",
            "publish_dry_run_receipt_required",
            PublishDryRunReceiptRequiredEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.stale_receipt",
            "publish_stale_receipt",
            PublishStaleReceiptEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.duplicate_target",
            "publish_duplicate_target",
            PublishDuplicateTargetEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.provenance_gap",
            "publish_provenance_gap",
            PublishProvenanceGapEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.receipt_invalid",
            "publish_receipt_invalid",
            PublishReceiptInvalidEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "publish-batch",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "external.quota_reported",
            "external_quota_reported",
            ExternalQuotaReportedEvent,
        ),
        _row(
            WorkflowEffectKind.WAIT_EXTERNAL,
            "wait_external.publish_quota",
            ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA,
            "external.ready",
            "external_ready",
            ExternalReadyEvent,
        ),
        _row(
            WorkflowEffectKind.WAIT_EXTERNAL,
            "wait_external.publish_quota",
            ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA,
            "contract_gap.missing_next_action",
            "missing_next_action",
            MissingNextActionEvent,
        ),
        *(
            _row(
                WorkflowEffectKind.RUN_SUBWORKFLOW,
                "workflow.resume_publish_blocker",
                origin,
                "publish.blocker_resolved",
                "publish_blocker_resolved",
                PublishBlockerResolvedEvent,
            )
            for origin in (
                ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED,
                ProcessChatsState.PUBLISH_STALE_RECEIPT,
                ProcessChatsState.PUBLISH_DUPLICATE_TARGET,
                ProcessChatsState.PUBLISH_PROVENANCE_GAP,
                ProcessChatsState.PUBLISH_RECEIPT_INVALID,
            )
        ),
        *(
            _row(
                WorkflowEffectKind.RUN_SUBWORKFLOW,
                "workflow.resume_publish_blocker",
                origin,
                "contract_gap.missing_error_context",
                "missing_error_context",
                MissingErrorContextEvent,
            )
            for origin in (
                ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED,
                ProcessChatsState.PUBLISH_STALE_RECEIPT,
                ProcessChatsState.PUBLISH_DUPLICATE_TARGET,
                ProcessChatsState.PUBLISH_PROVENANCE_GAP,
                ProcessChatsState.PUBLISH_RECEIPT_INVALID,
            )
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:link",
            ProcessChatsState.LINK_RUN_REQUESTED,
            "link.completed",
            "link_run_completed",
            LinkRunCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "/mednotes:link",
            ProcessChatsState.LINK_RUN_REQUESTED,
            "link.blocked",
            "link_run_blocked",
            LinkRunBlockedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "vault.rollback",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "rollback.completed",
            "rollback_completed",
            RollbackCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "vault.rollback",
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "vault.rollback",
            ProcessChatsState.LINK_RUN_REQUESTED,
            "rollback.completed",
            "rollback_completed",
            RollbackCompletedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "vault.rollback",
            ProcessChatsState.LINK_RUN_REQUESTED,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "workflow.failure_finalization",
            ProcessChatsState.ROLLBACK_RECORDED,
            "rollback.failure_recorded",
            "rollback_failure_recorded",
            RollbackFailureRecordedEvent,
        ),
        _row(
            WorkflowEffectKind.RUN_SUBWORKFLOW,
            "workflow.failure_finalization",
            ProcessChatsState.ROLLBACK_RECORDED,
            "contract_gap.missing_error_context",
            "missing_error_context",
            MissingErrorContextEvent,
        ),
    )
)


class ProcessChatsMachine(StateChart[WorkflowModel]):
    """Operational statechart for process-chats; actions emit intents, never IO."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        ProcessChatsState,
        initial=ProcessChatsState.ENVIRONMENT_CHECKING,
        final={
            ProcessChatsState.VAULT_GUARD_REJECTED,
            ProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS,
            ProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS,
            ProcessChatsState.PUBLISH_CANCELLED_BY_HUMAN,
            ProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION,
            ProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            ProcessChatsState.AGENT_TOOL_CONTRACT_VIOLATION,
            ProcessChatsState.PUBLISHED,
            ProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS,
            ProcessChatsState.TERMINAL_FAILURE_RECORDED,
        },
        use_enum_instance=False,
    )

    environment_checked = states.ENVIRONMENT_CHECKING.to(states.TRIAGE_PLANNING)
    paths_missing = states.ENVIRONMENT_CHECKING.to(states.ENVIRONMENT_PATHS_MISSING)
    windows_path_or_venv_blocked = states.ENVIRONMENT_CHECKING.to(
        states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
    )
    paths_configured = states.ENVIRONMENT_PATHS_MISSING.to(states.ENVIRONMENT_CHECKING)
    environment_bootstrap_completed = states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.to(
        states.ENVIRONMENT_CHECKING
    )
    no_pending_raw_chats = states.ENVIRONMENT_CHECKING.to(states.BACKLOG_NO_PENDING_RAW_CHATS)
    no_triaged_raw_chats = states.TRIAGE_PLANNING.to(states.BACKLOG_NO_TRIAGED_RAW_CHATS)
    triaged_raw_chats_available = states.ENVIRONMENT_CHECKING.to(states.BACKLOG_TRIAGED_RAW_CHATS_READY)
    triage_plan_created = (
        states.TRIAGE_PLANNING.to(states.ARCHITECT_WORK_REQUESTED)
        | states.BACKLOG_TRIAGED_RAW_CHATS_READY.to(states.ARCHITECT_WORK_REQUESTED)
    )
    subagent_plan_attestation_missing = states.ARCHITECT_WORK_REQUESTED.to(
        states.SUBAGENT_PLAN_ATTESTATION_REQUIRED
    )
    subagent_plan_attestation_invalid = states.ARCHITECT_WORK_REQUESTED.to(
        states.SUBAGENT_PLAN_ATTESTATION_INVALID
    )
    subagent_plan_attestation_supplied = (
        states.SUBAGENT_PLAN_ATTESTATION_REQUIRED.to(states.ARCHITECT_WORK_REQUESTED)
        | states.SUBAGENT_PLAN_ATTESTATION_INVALID.to(states.ARCHITECT_WORK_REQUESTED)
    )
    architect_specialist_capacity_blocked = states.ARCHITECT_WORK_REQUESTED.to(
        states.ARCHITECT_AWAITING_SPECIALIST_CAPACITY
    )
    architect_specialist_capacity_restored = states.ARCHITECT_AWAITING_SPECIALIST_CAPACITY.to(
        states.ARCHITECT_WORK_REQUESTED
    )
    architect_work_completed = states.ARCHITECT_WORK_REQUESTED.to(states.ARCHITECT_REVIEWING_OUTPUT)
    architect_output_accepted = states.ARCHITECT_REVIEWING_OUTPUT.to(states.NOTE_VALIDATION_RUNNING)
    architect_output_invalid = states.ARCHITECT_REVIEWING_OUTPUT.to(states.NOTE_VALIDATION_CONTENT_INVALID)
    note_validation_coverage_gap = states.NOTE_VALIDATION_RUNNING.to(states.NOTE_VALIDATION_COVERAGE_GAP)
    note_validation_manifest_mismatch = states.NOTE_VALIDATION_RUNNING.to(
        states.NOTE_VALIDATION_MANIFEST_MISMATCH
    )
    note_validation_content_invalid = states.NOTE_VALIDATION_RUNNING.to(states.NOTE_VALIDATION_CONTENT_INVALID)
    note_validation_retry_requested = (
        states.NOTE_VALIDATION_COVERAGE_GAP.to(states.ARCHITECT_WORK_REQUESTED)
        | states.NOTE_VALIDATION_MANIFEST_MISMATCH.to(states.ARCHITECT_WORK_REQUESTED)
        | states.NOTE_VALIDATION_CONTENT_INVALID.to(states.ARCHITECT_WORK_REQUESTED)
    )
    notes_validated = states.NOTE_VALIDATION_RUNNING.to(states.STAGING_MANIFEST_READY)
    publish_preview_produced = states.STAGING_MANIFEST_READY.to(states.PUBLISH_AWAITING_CONFIRMATION)
    vault_guard_required = states.STAGING_MANIFEST_READY.to(states.VAULT_GUARD_DECISION_REQUIRED)
    vault_guard_confirmed = states.VAULT_GUARD_DECISION_REQUIRED.to(states.STAGING_MANIFEST_READY)
    vault_guard_rejected = states.VAULT_GUARD_DECISION_REQUIRED.to(states.VAULT_GUARD_REJECTED)
    publish_approved_by_human = states.PUBLISH_AWAITING_CONFIRMATION.to(states.PUBLISH_APPLY_REQUESTED)
    publish_cancelled_by_human = states.PUBLISH_AWAITING_CONFIRMATION.to(states.PUBLISH_CANCELLED_BY_HUMAN)
    publish_batch_completed = states.PUBLISH_APPLY_REQUESTED.to(states.LINK_RUN_REQUESTED)
    publish_runtime_observed = (
        states.ROLLBACK_RECORDED.to(
            states.TERMINAL_FAILURE_RECORDED,
            cond="_observed_rollback_recorded",
            on="_on_observed_rollback_recorded",
        )
        | states.STAGING_MANIFEST_READY.to(
            states.PUBLISH_AWAITING_CONFIRMATION,
            cond="_observed_preview_ready",
            on="_on_observed_publish_preview",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.LINK_RUN_REQUESTED,
            cond="_observed_publish_completed",
            on="_on_observed_publish_completed",
        )
        | states.LINK_RUN_REQUESTED.to(
            states.PUBLISHED,
            cond="_observed_link_completed",
            on="_on_observed_link_completed",
        )
        | states.LINK_RUN_REQUESTED.to(
            states.COMPLETED_WITH_LINK_BLOCKERS,
            cond="_observed_link_blocked",
            on="_on_observed_link_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_PAUSED_FOR_QUOTA,
            cond="_observed_quota_wait",
            on="_on_observed_quota_wait",
        )
        | states.NOTE_VALIDATION_RUNNING.to(
            states.NOTE_VALIDATION_COVERAGE_GAP,
            cond="_observed_coverage_gap",
            on="_on_observed_blocked",
        )
        | states.NOTE_VALIDATION_RUNNING.to(
            states.NOTE_VALIDATION_MANIFEST_MISMATCH,
            cond="_observed_manifest_mismatch",
            on="_on_observed_blocked",
        )
        | states.NOTE_VALIDATION_RUNNING.to(
            states.NOTE_VALIDATION_CONTENT_INVALID,
            cond="_observed_content_invalid",
            on="_on_observed_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_DRY_RUN_RECEIPT_REQUIRED,
            cond="_observed_dry_run_receipt_required",
            on="_on_observed_publish_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_STALE_RECEIPT,
            cond="_observed_stale_receipt",
            on="_on_observed_publish_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_DUPLICATE_TARGET,
            cond="_observed_duplicate_target",
            on="_on_observed_publish_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_PROVENANCE_GAP,
            cond="_observed_provenance_gap",
            on="_on_observed_publish_blocked",
        )
        | states.PUBLISH_APPLY_REQUESTED.to(
            states.PUBLISH_RECEIPT_INVALID,
            cond="_observed_blocked",
            on="_on_observed_publish_blocked",
        )
    )
    publish_dry_run_receipt_required = states.PUBLISH_APPLY_REQUESTED.to(
        states.PUBLISH_DRY_RUN_RECEIPT_REQUIRED
    )
    publish_stale_receipt = states.PUBLISH_APPLY_REQUESTED.to(states.PUBLISH_STALE_RECEIPT)
    publish_duplicate_target = states.PUBLISH_APPLY_REQUESTED.to(states.PUBLISH_DUPLICATE_TARGET)
    publish_provenance_gap = states.PUBLISH_APPLY_REQUESTED.to(states.PUBLISH_PROVENANCE_GAP)
    publish_receipt_invalid = states.PUBLISH_APPLY_REQUESTED.to(states.PUBLISH_RECEIPT_INVALID)
    publish_blocker_resolved = (
        states.PUBLISH_DRY_RUN_RECEIPT_REQUIRED.to(states.PUBLISH_APPLY_REQUESTED)
        | states.PUBLISH_STALE_RECEIPT.to(states.PUBLISH_APPLY_REQUESTED)
        | states.PUBLISH_DUPLICATE_TARGET.to(states.PUBLISH_APPLY_REQUESTED)
        | states.PUBLISH_PROVENANCE_GAP.to(states.PUBLISH_APPLY_REQUESTED)
        | states.PUBLISH_RECEIPT_INVALID.to(states.PUBLISH_APPLY_REQUESTED)
    )
    link_run_completed = states.LINK_RUN_REQUESTED.to(states.PUBLISHED)
    link_run_blocked = states.LINK_RUN_REQUESTED.to(states.COMPLETED_WITH_LINK_BLOCKERS)
    external_quota_reported = states.PUBLISH_APPLY_REQUESTED.to(states.PUBLISH_PAUSED_FOR_QUOTA)
    external_ready = states.PUBLISH_PAUSED_FOR_QUOTA.to(states.PUBLISH_APPLY_REQUESTED)
    missing_next_action = (
        states.ENVIRONMENT_PATHS_MISSING.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.VAULT_GUARD_DECISION_REQUIRED.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.ARCHITECT_AWAITING_SPECIALIST_CAPACITY.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.ARCHITECT_REVIEWING_OUTPUT.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.NOTE_VALIDATION_RUNNING.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.NOTE_VALIDATION_COVERAGE_GAP.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.NOTE_VALIDATION_MANIFEST_MISMATCH.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.NOTE_VALIDATION_CONTENT_INVALID.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_APPLY_REQUESTED.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_PAUSED_FOR_QUOTA.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_DRY_RUN_RECEIPT_REQUIRED.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_STALE_RECEIPT.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_DUPLICATE_TARGET.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_PROVENANCE_GAP.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.PUBLISH_RECEIPT_INVALID.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
        | states.LINK_RUN_REQUESTED.to(states.CONTRACT_GAP_MISSING_NEXT_ACTION)
    )
    missing_error_context = (
        states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.SUBAGENT_PLAN_ATTESTATION_REQUIRED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.SUBAGENT_PLAN_ATTESTATION_INVALID.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.ARCHITECT_REVIEWING_OUTPUT.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.NOTE_VALIDATION_RUNNING.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.NOTE_VALIDATION_COVERAGE_GAP.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.NOTE_VALIDATION_MANIFEST_MISMATCH.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.NOTE_VALIDATION_CONTENT_INVALID.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_APPLY_REQUESTED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_DRY_RUN_RECEIPT_REQUIRED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_STALE_RECEIPT.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_DUPLICATE_TARGET.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_PROVENANCE_GAP.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.PUBLISH_RECEIPT_INVALID.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.LINK_RUN_REQUESTED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
        | states.ROLLBACK_RECORDED.to(states.CONTRACT_GAP_MISSING_ERROR_CONTEXT)
    )
    agent_tool_contract_violation = (
        states.ARCHITECT_WORK_REQUESTED.to(states.AGENT_TOOL_CONTRACT_VIOLATION)
        | states.PUBLISH_APPLY_REQUESTED.to(states.AGENT_TOOL_CONTRACT_VIOLATION)
        | states.LINK_RUN_REQUESTED.to(states.AGENT_TOOL_CONTRACT_VIOLATION)
    )
    rollback_completed = (
        states.PUBLISH_APPLY_REQUESTED.to(states.ROLLBACK_RECORDED)
        | states.LINK_RUN_REQUESTED.to(states.ROLLBACK_RECORDED)
    )
    rollback_failure_recorded = states.ROLLBACK_RECORDED.to(states.TERMINAL_FAILURE_RECORDED)

    _PHASE_BY_STATE: ClassVar[dict[ProcessChatsState, str]] = {
        ProcessChatsState.ENVIRONMENT_CHECKING: "environment",
        ProcessChatsState.ENVIRONMENT_PATHS_MISSING: "environment",
        ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED: "environment",
        ProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS: "backlog",
        ProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS: "backlog",
        ProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY: "backlog",
        ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED: "vault_guard",
        ProcessChatsState.VAULT_GUARD_REJECTED: "vault_guard",
        ProcessChatsState.TRIAGE_PLANNING: "triage",
        ProcessChatsState.ARCHITECT_WORK_REQUESTED: "architect",
        ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY: "architect",
        ProcessChatsState.ARCHITECT_REVIEWING_OUTPUT: "architect",
        ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED: "subagent_plan_attestation",
        ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID: "subagent_plan_attestation",
        ProcessChatsState.NOTE_VALIDATION_RUNNING: "note_validation",
        ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP: "note_validation",
        ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH: "note_validation",
        ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID: "note_validation",
        ProcessChatsState.STAGING_MANIFEST_READY: "staging",
        ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION: "publish",
        ProcessChatsState.PUBLISH_CANCELLED_BY_HUMAN: "publish",
        ProcessChatsState.PUBLISH_APPLY_REQUESTED: "publish",
        ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA: "publish",
        ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED: "publish",
        ProcessChatsState.PUBLISH_STALE_RECEIPT: "publish",
        ProcessChatsState.PUBLISH_DUPLICATE_TARGET: "publish",
        ProcessChatsState.PUBLISH_PROVENANCE_GAP: "publish",
        ProcessChatsState.PUBLISH_RECEIPT_INVALID: "publish",
        ProcessChatsState.LINK_RUN_REQUESTED: "link",
        ProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION: "contract_gap",
        ProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT: "contract_gap",
        ProcessChatsState.AGENT_TOOL_CONTRACT_VIOLATION: "agent_tool_contract",
        ProcessChatsState.ROLLBACK_RECORDED: "rollback",
        ProcessChatsState.PUBLISHED: "published",
        ProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS: "link",
        ProcessChatsState.TERMINAL_FAILURE_RECORDED: "failed",
    }

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        match ProcessChatsState(state):
            case (
                ProcessChatsState.ENVIRONMENT_CHECKING
                | ProcessChatsState.TRIAGE_PLANNING
                | ProcessChatsState.ARCHITECT_REVIEWING_OUTPUT
                | ProcessChatsState.NOTE_VALIDATION_RUNNING
                | ProcessChatsState.STAGING_MANIFEST_READY
            ):
                return WorkflowStateCategory.RUNNING
            case (
                ProcessChatsState.ENVIRONMENT_PATHS_MISSING
                | ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
                | ProcessChatsState.ARCHITECT_WORK_REQUESTED
                | ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED
                | ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID
                | ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP
                | ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH
                | ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID
                | ProcessChatsState.PUBLISH_APPLY_REQUESTED
                | ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED
                | ProcessChatsState.PUBLISH_STALE_RECEIPT
                | ProcessChatsState.PUBLISH_DUPLICATE_TARGET
                | ProcessChatsState.PUBLISH_PROVENANCE_GAP
                | ProcessChatsState.PUBLISH_RECEIPT_INVALID
                | ProcessChatsState.LINK_RUN_REQUESTED
                | ProcessChatsState.ROLLBACK_RECORDED
                | ProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY
            ):
                return WorkflowStateCategory.WAITING_AGENT
            case ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA | ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY:
                return WorkflowStateCategory.WAITING_EXTERNAL
            case ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED | ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION:
                return WorkflowStateCategory.WAITING_HUMAN
            case (
                ProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION
                | ProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT
                | ProcessChatsState.AGENT_TOOL_CONTRACT_VIOLATION
                | ProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS
            ):
                return WorkflowStateCategory.BLOCKED
            case (
                ProcessChatsState.PUBLISHED
                | ProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS
                | ProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS
            ):
                return WorkflowStateCategory.COMPLETED
            case ProcessChatsState.PUBLISH_CANCELLED_BY_HUMAN | ProcessChatsState.VAULT_GUARD_REJECTED:
                return WorkflowStateCategory.COMPLETED_WITH_WARNINGS
            case ProcessChatsState.TERMINAL_FAILURE_RECORDED:
                return WorkflowStateCategory.FAILED
        raise ValueError(f"unknown process-chats state: {state}")

    def on_environment_checked(self, workflow_event: EnvironmentCheckedEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.TRIAGE_PLANNING, "environment_checked")

    def on_paths_missing(self, workflow_event: PathsMissingEvent) -> WorkflowTransitionResult:
        effect = _setup_paths_effect(workflow_event, ProcessChatsState.ENVIRONMENT_PATHS_MISSING)
        return self._transition(
            workflow_event,
            ProcessChatsState.ENVIRONMENT_PATHS_MISSING,
            workflow_event.reason_code,
            effects=[effect],
            resume_action="/mednotes:setup",
        )

    def on_windows_path_or_venv_blocked(
        self, workflow_event: WindowsPathOrVenvBlockedEvent
    ) -> WorkflowTransitionResult:
        effect = _setup_bootstrap_effect(workflow_event, ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
            workflow_event.reason_code,
            effects=[effect],
            resume_action="/mednotes:setup",
        )

    def on_paths_configured(self, workflow_event: PathsConfiguredEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.ENVIRONMENT_CHECKING, "setup.paths_configured")

    def on_environment_bootstrap_completed(
        self, workflow_event: EnvironmentBootstrapCompletedEvent
    ) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.ENVIRONMENT_CHECKING,
            "setup.bootstrap_completed",
        )

    def on_no_pending_raw_chats(self, workflow_event: NoPendingRawChatsEvent) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS,
            "no_pending_raw_chats",
        )

    def on_no_triaged_raw_chats(self, workflow_event: NoTriagedRawChatsEvent) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS,
            "no_triaged_raw_chats",
        )

    def on_triaged_raw_chats_available(
        self,
        workflow_event: TriagedRawChatsAvailableEvent,
    ) -> WorkflowTransitionResult:
        effect = _architect_planning_effect(workflow_event, ProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY)
        return self._transition(
            workflow_event,
            ProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY,
            "triaged_raw_chats_available",
            effects=[effect],
            resume_action="Continuar com list-triados e plan-subagents --phase architect.",
        )

    def on_triage_plan_created(self, workflow_event: TriagePlanCreatedEvent) -> WorkflowTransitionResult:
        effect = _architect_effect(workflow_event, ProcessChatsState.ARCHITECT_WORK_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "triage_plan_created",
            effects=[effect],
        )

    def on_subagent_plan_attestation_missing(
        self, workflow_event: SubagentPlanAttestationMissingEvent
    ) -> WorkflowTransitionResult:
        effect = _plan_attestation_effect(workflow_event, ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED)
        return self._transition(
            workflow_event,
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
            workflow_event.reason_code,
            effects=[effect],
        )

    def on_subagent_plan_attestation_invalid(
        self, workflow_event: SubagentPlanAttestationInvalidEvent
    ) -> WorkflowTransitionResult:
        effect = _plan_attestation_effect(workflow_event, ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID)
        return self._transition(
            workflow_event,
            ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID,
            workflow_event.reason_code,
            effects=[effect],
        )

    def on_subagent_plan_attestation_supplied(
        self, workflow_event: SubagentPlanAttestationSuppliedEvent
    ) -> WorkflowTransitionResult:
        effect = _architect_retry_effect(workflow_event, ProcessChatsState.ARCHITECT_WORK_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "attestation.supplied",
            effects=[effect],
        )

    def on_architect_specialist_capacity_blocked(
        self, workflow_event: ArchitectSpecialistCapacityBlockedEvent
    ) -> WorkflowTransitionResult:
        effect = _wait_external_effect(
            workflow_event,
            ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY,
            target="wait_external.specialist_capacity",
            wait_target="specialist_capacity",
            resume_action=workflow_event.resume_action,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY,
            workflow_event.reason_code,
            effects=[effect],
            resume_action=workflow_event.resume_action,
        )

    def on_architect_specialist_capacity_restored(
        self, workflow_event: ArchitectSpecialistCapacityRestoredEvent
    ) -> WorkflowTransitionResult:
        effect = _architect_retry_effect(workflow_event, ProcessChatsState.ARCHITECT_WORK_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "external.ready",
            effects=[effect],
        )

    def on_architect_work_completed(self, workflow_event: ArchitectWorkCompletedEvent) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_REVIEWING_OUTPUT,
            "architect.completed",
        )

    def on_architect_output_accepted(self, workflow_event: ArchitectOutputAcceptedEvent) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.NOTE_VALIDATION_RUNNING,
            "architect_output_accepted",
        )

    def on_architect_output_invalid(self, workflow_event: ArchitectOutputInvalidEvent) -> WorkflowTransitionResult:
        effect = _resume_architect_work_effect(workflow_event, ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID)
        return self._transition(
            workflow_event,
            ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID,
            workflow_event.reason_code,
            effects=[effect],
            resume_action=workflow_event.error_context.next_action,
        )

    def on_note_validation_coverage_gap(
        self, workflow_event: NoteValidationCoverageGapEvent
    ) -> WorkflowTransitionResult:
        return self._recoverable_validation_block(
            workflow_event,
            ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP,
        )

    def on_note_validation_manifest_mismatch(
        self, workflow_event: NoteValidationManifestMismatchEvent
    ) -> WorkflowTransitionResult:
        return self._recoverable_validation_block(
            workflow_event,
            ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH,
        )

    def on_note_validation_content_invalid(
        self, workflow_event: NoteValidationContentInvalidEvent
    ) -> WorkflowTransitionResult:
        return self._recoverable_validation_block(
            workflow_event,
            ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID,
        )

    def _recoverable_validation_block(
        self,
        workflow_event: (
            NoteValidationCoverageGapEvent
            | NoteValidationManifestMismatchEvent
            | NoteValidationContentInvalidEvent
        ),
        to_state: ProcessChatsState,
    ) -> WorkflowTransitionResult:
        effect = _resume_architect_work_effect(workflow_event, to_state)
        return self._transition(
            workflow_event,
            to_state,
            workflow_event.reason_code,
            effects=[effect],
            resume_action=workflow_event.error_context.next_action,
        )

    def on_note_validation_retry_requested(
        self, workflow_event: NoteValidationRetryRequestedEvent
    ) -> WorkflowTransitionResult:
        effect = _architect_retry_effect(workflow_event, ProcessChatsState.ARCHITECT_WORK_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ARCHITECT_WORK_REQUESTED,
            "validation.retry_requested",
            effects=[effect],
        )

    def on_notes_validated(self, workflow_event: NotesValidatedEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.STAGING_MANIFEST_READY, "notes_validated")

    def on_publish_preview_produced(self, workflow_event: PublishPreviewProducedEvent) -> WorkflowTransitionResult:
        to_state = ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION
        decision = _ask_human_decision(
            state=to_state,
            reason_code="publish_confirmation_required",
            public_summary="Confirmar publicação das notas preparadas?",
            next_action="Confirmar ou cancelar a publicação.",
            recommended_option_id="approve",
            options=(
                HumanDecisionOption(id="approve", label="Publicar", description="Aplicar o lote preparado."),
                HumanDecisionOption(id="cancel", label="Cancelar", description="Encerrar sem mutar a Wiki."),
            ),
        )
        effect = _human_publish_decision_effect(workflow_event, to_state)
        return self._transition(
            workflow_event,
            to_state,
            "publish_preview_produced",
            effects=[effect],
            decision=decision,
            human_decision_packet=HumanDecisionPacket.model_validate(decision.to_human_decision_packet()),
            resume_action=decision.resume_action,
        )

    def on_vault_guard_required(self, workflow_event: VaultGuardRequiredEvent) -> WorkflowTransitionResult:
        to_state = ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED
        decision = _ask_human_decision(
            state=to_state,
            reason_code=workflow_event.reason_code,
            public_summary="Confirmar ponto de restauração antes de alterar a Wiki?",
            next_action="Confirmar a proteção do vault ou cancelar a operação.",
            recommended_option_id="confirm",
            options=(
                HumanDecisionOption(id="confirm", label="Confirmar", description="Continuar com proteção do vault."),
                HumanDecisionOption(id="reject", label="Cancelar", description="Encerrar sem mutar a Wiki."),
            ),
        )
        effect = _vault_guard_decision_effect(workflow_event, to_state)
        return self._transition(
            workflow_event,
            to_state,
            workflow_event.reason_code,
            effects=[effect],
            decision=decision,
            human_decision_packet=HumanDecisionPacket.model_validate(decision.to_human_decision_packet()),
            resume_action=decision.resume_action,
        )

    def on_vault_guard_confirmed(self, workflow_event: VaultGuardConfirmedEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.STAGING_MANIFEST_READY, "human.confirmed")

    def on_vault_guard_rejected(self, workflow_event: VaultGuardRejectedEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.VAULT_GUARD_REJECTED, "human.rejected")

    def on_publish_approved_by_human(self, workflow_event: HumanPublishApprovalEvent) -> WorkflowTransitionResult:
        effect = _publish_batch_effect(workflow_event, ProcessChatsState.PUBLISH_APPLY_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "human.approved",
            effects=[effect],
        )

    def on_publish_cancelled_by_human(self, workflow_event: HumanPublishCancellationEvent) -> WorkflowTransitionResult:
        return self._transition(
            workflow_event,
            ProcessChatsState.PUBLISH_CANCELLED_BY_HUMAN,
            "human.cancelled",
        )

    def on_publish_batch_completed(self, workflow_event: PublishBatchCompletedEvent) -> WorkflowTransitionResult:
        effect = _link_effect(workflow_event, ProcessChatsState.LINK_RUN_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.LINK_RUN_REQUESTED,
            "publish.completed",
            effects=[effect],
        )

    def _on_observed_publish_preview(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        decision = _ask_human_decision(
            state=to_state,
            reason_code="publish_confirmation_required",
            public_summary="Confirmar publicação das notas preparadas?",
            next_action="Confirmar ou cancelar a publicação.",
            recommended_option_id="approve",
            options=(
                HumanDecisionOption(id="approve", label="Publicar", description="Aplicar o lote preparado."),
                HumanDecisionOption(id="cancel", label="Cancelar", description="Encerrar sem mutar a Wiki."),
            ),
        )
        observation = workflow_event.observation
        effect = _workflow_effect(
            workflow_event,
            to_state,
            kind=WorkflowEffectKind.ASK_HUMAN,
            target="human.publish_decision",
            payload=HumanPublishDecisionEffectPayload(
                manifest_path=observation.manifest_path,
                dry_run_receipt_path=observation.dry_run_receipt_path,
            ),
            requires_receipt=False,
        )
        return self._transition(
            workflow_event,
            to_state,
            "publish_preview_produced",
            effects=[effect],
            decision=decision,
            human_decision_packet=HumanDecisionPacket.model_validate(decision.to_human_decision_packet()),
            resume_action=decision.resume_action,
        )

    def _on_observed_publish_completed(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        observation = workflow_event.observation
        effect = _workflow_effect(
            workflow_event,
            to_state,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="/mednotes:link",
            payload=LinkWorkflowRunEffectPayload(
                kind="link_run",
                diagnose=False,
                apply=True,
                trigger_context_path=observation.link_trigger_context_path,
                no_related_notes=False,
            ),
            mutates_resources=True,
            rollback_declared=True,
        )
        return self._transition(workflow_event, to_state, "publish.completed", effects=[effect])

    def _on_observed_link_completed(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState(str(getattr(target, "value", target))), "link.completed")

    def _on_observed_link_blocked(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        reason_code = workflow_event.observation.reason_code or "process_chats_linker_blocked"
        next_action = workflow_event.observation.next_action or "Resolver pendências de conexões/grafo pela rota oficial."
        decision = _hard_block_decision(state=to_state, reason_code=reason_code, next_action=next_action)
        return self._transition(workflow_event, to_state, reason_code, decision=decision)

    def _on_observed_quota_wait(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        resume_action = workflow_event.observation.next_action or "Aguardar cota externa e retomar publicação."
        effect = _wait_external_effect(
            workflow_event,
            to_state,
            target="wait_external.publish_quota",
            wait_target="publish_quota",
            resume_action=resume_action,
        )
        return self._transition(
            workflow_event,
            to_state,
            workflow_event.observation.reason_code or "external.quota_reported",
            effects=[effect],
            resume_action=resume_action,
        )

    def _on_observed_blocked(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        observation = workflow_event.observation
        effect = _resume_architect_work_effect(workflow_event, to_state)
        return self._transition(
            workflow_event,
            to_state,
            observation.reason_code or to_state.value,
            effects=[effect],
            resume_action=observation.next_action,
        )

    def _on_observed_publish_blocked(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        observation = workflow_event.observation
        effect = _workflow_effect(
            workflow_event,
            to_state,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="workflow.resume_publish_blocker",
            payload=ResumePublishBlockerEffectPayload(reason_code=observation.reason_code or "publish_receipt_invalid"),
            resume_action=observation.next_action,
        )
        return self._transition(
            workflow_event,
            to_state,
            observation.reason_code or "publish_receipt_invalid",
            effects=[effect],
            resume_action=observation.next_action,
        )

    def _on_observed_rollback_recorded(
        self,
        workflow_event: ProcessChatsPublishRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState(str(getattr(target, "value", target)))
        context = _publish_observation_error_context(workflow_event)
        decision = _failed_decision(
            state=to_state,
            reason_code=context.root_cause,
            next_action=context.next_action,
        )
        return self._transition(workflow_event, to_state, "rollback.failure_recorded", decision=decision)

    def _observed_preview_ready(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.preview_ready

    def _observed_publish_completed(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.publish_completed

    def _observed_link_completed(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.link_completed

    def _observed_link_blocked(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.link_blocked

    def _observed_rollback_recorded(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.rollback_recorded

    def _observed_quota_wait(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.quota_wait

    def _observed_coverage_gap(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.validation_coverage_gap

    def _observed_manifest_mismatch(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.validation_manifest_mismatch

    def _observed_content_invalid(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.validation_content_invalid

    def _observed_dry_run_receipt_required(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.publish_dry_run_receipt_required

    def _observed_stale_receipt(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.publish_stale_receipt

    def _observed_duplicate_target(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.publish_duplicate_target

    def _observed_provenance_gap(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.publish_provenance_gap

    def _observed_blocked(self, workflow_event: ProcessChatsPublishRuntimeObservedEvent) -> bool:
        return workflow_event.observation.blocked

    def on_publish_dry_run_receipt_required(
        self, workflow_event: PublishDryRunReceiptRequiredEvent
    ) -> WorkflowTransitionResult:
        return self._recoverable_publish_block(
            workflow_event,
            ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED,
        )

    def on_publish_stale_receipt(self, workflow_event: PublishStaleReceiptEvent) -> WorkflowTransitionResult:
        return self._recoverable_publish_block(workflow_event, ProcessChatsState.PUBLISH_STALE_RECEIPT)

    def on_publish_duplicate_target(self, workflow_event: PublishDuplicateTargetEvent) -> WorkflowTransitionResult:
        return self._recoverable_publish_block(workflow_event, ProcessChatsState.PUBLISH_DUPLICATE_TARGET)

    def on_publish_provenance_gap(self, workflow_event: PublishProvenanceGapEvent) -> WorkflowTransitionResult:
        return self._recoverable_publish_block(workflow_event, ProcessChatsState.PUBLISH_PROVENANCE_GAP)

    def on_publish_receipt_invalid(self, workflow_event: PublishReceiptInvalidEvent) -> WorkflowTransitionResult:
        return self._recoverable_publish_block(workflow_event, ProcessChatsState.PUBLISH_RECEIPT_INVALID)

    def _recoverable_publish_block(
        self,
        workflow_event: (
            PublishDryRunReceiptRequiredEvent
            | PublishStaleReceiptEvent
            | PublishDuplicateTargetEvent
            | PublishProvenanceGapEvent
            | PublishReceiptInvalidEvent
        ),
        to_state: ProcessChatsState,
    ) -> WorkflowTransitionResult:
        effect = _resume_publish_blocker_effect(workflow_event, to_state)
        return self._transition(
            workflow_event,
            to_state,
            workflow_event.reason_code,
            effects=[effect],
            resume_action=workflow_event.next_action,
        )

    def on_publish_blocker_resolved(self, workflow_event: PublishBlockerResolvedEvent) -> WorkflowTransitionResult:
        effect = _publish_batch_effect(workflow_event, ProcessChatsState.PUBLISH_APPLY_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "publish.blocker_resolved",
            effects=[effect],
        )

    def on_external_quota_reported(self, workflow_event: ExternalQuotaReportedEvent) -> WorkflowTransitionResult:
        effect = _wait_external_effect(
            workflow_event,
            ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA,
            target="wait_external.publish_quota",
            wait_target="publish_quota",
            resume_action=workflow_event.resume_action,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA,
            "external.quota_reported",
            effects=[effect],
            resume_action=workflow_event.resume_action,
        )

    def on_external_ready(self, workflow_event: ExternalReadyEvent) -> WorkflowTransitionResult:
        effect = _publish_batch_effect(workflow_event, ProcessChatsState.PUBLISH_APPLY_REQUESTED)
        return self._transition(
            workflow_event,
            ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            "external.ready",
            effects=[effect],
        )

    def on_link_run_completed(self, workflow_event: LinkRunCompletedEvent) -> WorkflowTransitionResult:
        return self._transition(workflow_event, ProcessChatsState.PUBLISHED, "link.completed")

    def on_link_run_blocked(self, workflow_event: LinkRunBlockedEvent) -> WorkflowTransitionResult:
        decision = _hard_block_decision(
            state=ProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS,
            reason_code=workflow_event.reason_code,
            next_action=workflow_event.next_action,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS,
            workflow_event.reason_code,
            decision=decision,
        )

    def on_missing_next_action(self, workflow_event: MissingNextActionEvent) -> WorkflowTransitionResult:
        decision = _hard_block_decision(
            state=ProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION,
            reason_code="contract_gap.missing_next_action",
            next_action=workflow_event.next_action_hint,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION,
            "contract_gap.missing_next_action",
            decision=decision,
        )

    def on_missing_error_context(self, workflow_event: MissingErrorContextEvent) -> WorkflowTransitionResult:
        decision = _hard_block_decision(
            state=ProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            reason_code="contract_gap.missing_error_context",
            next_action=workflow_event.error_context_hint,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            "contract_gap.missing_error_context",
            decision=decision,
        )

    def on_agent_tool_contract_violation(
        self, workflow_event: AgentToolContractViolationEvent
    ) -> WorkflowTransitionResult:
        to_state = ProcessChatsState.AGENT_TOOL_CONTRACT_VIOLATION
        decision = _hard_block_decision(
            state=to_state,
            reason_code="agent_tool_contract_violation",
            next_action=workflow_event.error_context.next_action,
        )
        return self._transition(workflow_event, to_state, "agent_tool_contract_violation", decision=decision)

    def on_rollback_completed(self, workflow_event: RollbackCompletedEvent) -> WorkflowTransitionResult:
        effect = _failure_finalization_effect(workflow_event, ProcessChatsState.ROLLBACK_RECORDED)
        return self._transition(
            workflow_event,
            ProcessChatsState.ROLLBACK_RECORDED,
            "rollback.completed",
            effects=[effect],
        )

    def on_rollback_failure_recorded(self, workflow_event: RollbackFailureRecordedEvent) -> WorkflowTransitionResult:
        decision = _failed_decision(
            state=ProcessChatsState.TERMINAL_FAILURE_RECORDED,
            reason_code=workflow_event.error_context.root_cause,
            next_action=workflow_event.error_context.next_action,
        )
        return self._transition(
            workflow_event,
            ProcessChatsState.TERMINAL_FAILURE_RECORDED,
            "rollback.failure_recorded",
            decision=decision,
        )

    def _transition(
        self,
        workflow_event: ProcessChatsEvent,
        to_state: ProcessChatsState,
        reason_code: str,
        *,
        effects: list[WorkflowEffect] | None = None,
        decision: WorkflowDecision | None = None,
        human_decision_packet: HumanDecisionPacket | None = None,
        resume_action: str = "",
    ) -> WorkflowTransitionResult:
        return WorkflowTransitionResult(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            from_state=workflow_event.current_state,
            to_state=to_state.value,
            trigger=_event_name(workflow_event),
            reason_code=reason_code,
            effects=effects or [],
            decision=decision,
            human_decision_packet=human_decision_packet,
            resume_action=resume_action,
        )


def _workflow_effect(
    workflow_event: ProcessChatsEvent,
    to_state: ProcessChatsState,
    *,
    kind: WorkflowEffectKind,
    target: str,
    payload: ProcessChatsEffectPayload,
    mutates_resources: bool = False,
    rollback_declared: bool = False,
    requires_receipt: bool = True,
    requires_attestation: bool = False,
    model_policy: dict[str, str] | None = None,
    resume_action: str = "",
) -> WorkflowEffect:
    payload_model = ProcessChatsEffectPayloadAdapter.validate_python(payload)
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"{workflow_event.run_id}:{_event_name(workflow_event)}:{target}",
        origin_state=to_state.value,
        kind=kind,
        target=target,
        payload=payload_model.to_payload(),
        mutates_resources=mutates_resources,
        rollback_declared=rollback_declared,
        no_resource_mutation=not mutates_resources,
        requires_receipt=requires_receipt,
        requires_attestation=requires_attestation,
        model_policy=model_policy or {},
        resume_action=resume_action,
    )


def _setup_paths_effect(event: PathsMissingEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=event.setup_target,
        payload=SetupPathsEffectPayload(missing_path_kind=event.missing_path_kind),
    )


def _setup_bootstrap_effect(event: WindowsPathOrVenvBlockedEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=event.setup_target,
        payload=SetupBootstrapEffectPayload(reason_code=event.reason_code),
    )


def _architect_effect(event: TriagePlanCreatedEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        target="med-knowledge-architect",
        payload=ArchitectSpecialistEffectPayload(
            note_plan_hash=event.note_plan_hash,
            raw_file_count=event.raw_file_count,
        ),
        requires_attestation=True,
        model_policy={"specialist": "med-knowledge-architect"},
    )


def _architect_planning_effect(event: TriagedRawChatsAvailableEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:process-chats plan-subagents architect",
        payload=ArchitectPlanningEffectPayload(triaged_count=event.triaged_count),
        requires_receipt=False,
        resume_action="Continuar com list-triados e plan-subagents --phase architect.",
    )


def _architect_retry_effect(event: ProcessChatsEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        target="med-knowledge-architect",
        payload=ArchitectSpecialistEffectPayload(note_plan_hash="resume", raw_file_count=0),
        requires_attestation=True,
        model_policy={"specialist": "med-knowledge-architect"},
    )


def _resume_architect_work_effect(event: ProcessChatsEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="workflow.resume_architect_work",
        payload=ResumeArchitectWorkEffectPayload(resolved_by="architect_retry"),
    )


def _resume_publish_blocker_effect(
    event: (
        PublishDryRunReceiptRequiredEvent
        | PublishStaleReceiptEvent
        | PublishDuplicateTargetEvent
        | PublishProvenanceGapEvent
        | PublishReceiptInvalidEvent
    ),
    to_state: ProcessChatsState,
) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="workflow.resume_publish_blocker",
        payload=ResumePublishBlockerEffectPayload(reason_code=event.reason_code),
        resume_action=event.next_action,
    )


def _plan_attestation_effect(
    event: SubagentPlanAttestationMissingEvent | SubagentPlanAttestationInvalidEvent,
    to_state: ProcessChatsState,
) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="agent.plan_attestation",
        payload=PlanAttestationEffectPayload(reason_code=event.reason_code),
    )


def _wait_external_effect(
    event: ProcessChatsEvent,
    to_state: ProcessChatsState,
    *,
    target: str,
    wait_target: Literal["specialist_capacity", "publish_quota"],
    resume_action: str,
) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.WAIT_EXTERNAL,
        target=target,
        payload=WaitExternalEffectPayload(
            wait_target=wait_target,
            blocked_reason=target,
            next_action=resume_action,
            resume_supported=True,
        ),
        requires_receipt=False,
        resume_action=resume_action,
    )


def _publish_observation_error_context(
    event: ProcessChatsPublishRuntimeObservedEvent,
) -> ProcessChatsErrorContext:
    observation = event.observation
    if observation.error_context is not None:
        return observation.error_context
    reason = observation.reason_code or "process_chats_publish_runtime_blocked"
    return ProcessChatsErrorContext(
        root_cause=reason,
        affected_artifact=observation.manifest_path or "process-chats-manifest",
        retry_scope="process-chats",
        next_action=observation.next_action or "Corrigir o bloqueio e retomar /mednotes:process-chats.",
    )


def _human_publish_decision_effect(event: PublishPreviewProducedEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.ASK_HUMAN,
        target="human.publish_decision",
        payload=HumanPublishDecisionEffectPayload(
            manifest_path=event.manifest_path,
            dry_run_receipt_path=event.dry_run_receipt_path,
        ),
        requires_receipt=False,
    )


def _vault_guard_decision_effect(event: VaultGuardRequiredEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.ASK_HUMAN,
        target="human.vault_guard_decision",
        payload=VaultGuardDecisionEffectPayload(changed_file_count=event.changed_file_count),
        requires_receipt=False,
    )


def _publish_batch_effect(event: ProcessChatsEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    if not isinstance(event, HumanPublishApprovalEvent | ExternalReadyEvent | PublishBlockerResolvedEvent):
        raise TypeError("publish-batch effect requires an event with validated receipt paths")
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="publish-batch",
        payload=PublishBatchEffectPayload(
            schema_version="medical-notes-workbench.process-chats.publish-batch-effect.v1",
            kind="publish_batch",
            manifest_path=event.manifest_path,
            dry_run_receipt_path=event.dry_run_receipt_path,
        ),
        mutates_resources=True,
        rollback_declared=True,
    )


def _link_effect(event: PublishBatchCompletedEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:link",
        payload=LinkWorkflowRunEffectPayload(
            kind="link_run",
            diagnose=False,
            apply=True,
            trigger_context_path=event.link_trigger_context_path,
            no_related_notes=False,
        ),
        mutates_resources=True,
        rollback_declared=True,
    )


def _rollback_effect(event: ProcessChatsEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="vault.rollback",
        payload=RollbackEffectPayload(failed_origin_state=event.current_state),
        mutates_resources=True,
        rollback_declared=True,
    )


def _failure_finalization_effect(event: ProcessChatsEvent, to_state: ProcessChatsState) -> WorkflowEffect:
    return _workflow_effect(
        event,
        to_state,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="workflow.failure_finalization",
        payload=FailureFinalizationEffectPayload(),
    )


def _hard_block_decision(*, state: ProcessChatsState, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind=WorkflowDecisionKind.HARD_BLOCK,
        phase=ProcessChatsMachine._PHASE_BY_STATE[state],
        reason_code=reason_code,
        public_summary=f"Process-chats bloqueado em {state.value}.",
        developer_summary=f"Process-chats reached blocked leaf {state.value}.",
        evidence=[
            DecisionEvidence(
                summary=f"Estado operacional bloqueado: {state.value}",
                technical_code=reason_code,
                source="process_chats_machine",
            )
        ],
        next_action=next_action,
    )


def _failed_decision(*, state: ProcessChatsState, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind=WorkflowDecisionKind.FAILED,
        phase=ProcessChatsMachine._PHASE_BY_STATE[state],
        reason_code=reason_code,
        public_summary="Process-chats falhou após registrar rollback/recuperação.",
        developer_summary=f"Process-chats reached failed leaf {state.value}.",
        evidence=[
            DecisionEvidence(
                summary=f"Estado terminal de falha: {state.value}",
                technical_code=reason_code,
                source="process_chats_machine",
            )
        ],
        next_action=next_action,
    )


def _ask_human_decision(
    *,
    state: ProcessChatsState,
    reason_code: str,
    public_summary: str,
    next_action: str,
    recommended_option_id: str,
    options: tuple[HumanDecisionOption, ...],
) -> WorkflowDecision:
    return WorkflowDecision(
        kind=WorkflowDecisionKind.ASK_HUMAN,
        phase=ProcessChatsMachine._PHASE_BY_STATE[state],
        reason_code=reason_code,
        public_summary=public_summary,
        developer_summary="A decisão humana protege uma mutação ou parada limpa do workflow.",
        evidence=[
            DecisionEvidence(
                summary=f"Estado aguardando decisão humana: {state.value}",
                technical_code=reason_code,
                source="process_chats_machine",
            )
        ],
        next_action=next_action,
        resume_action=next_action,
        rejected_automations=_rejected_automations(reason_code),
        recommended_option_id=recommended_option_id,
        options=list(options),
        human_decision_kind=reason_code,
    )


def _rejected_automations(reason_code: str) -> list[RejectedAutomation]:
    return [
        RejectedAutomation(
            kind=kind,
            reason_code=reason_code,
            reason="A escolha altera segurança, mutação ou publicação e exige confirmação explícita.",
        )
        for kind in WorkflowAutomationKind
    ]


def process_chats_event_from_effect_result(
    model: WorkflowModel,
    result: WorkflowEffectResult,
) -> ProcessChatsBoundaryEvent:
    """Convert an adapter result to one typed event using only the outcome matrix."""

    if result.effect.workflow != model.workflow or result.effect.run_id != model.run_id:
        raise ValueError("effect result belongs to a different process-chats run")
    if result.effect.origin_state != model.state:
        raise ValueError("effect origin_state does not match current workflow state")
    outcome = ProcessChatsEffectOutcomeAdapter.validate_python(result.outcome.model_dump(mode="json"))
    row = PROCESS_CHATS_EFFECT_RETURN_EVENT_MATRIX.lookup(
        kind=result.effect.kind,
        target=result.effect.target,
        origin_state=result.effect.origin_state,
        outcome_code=outcome.code,
    )
    event = _event_from_outcome(row, model=model, outcome=outcome)
    return ProcessChatsBoundaryEventAdapter.validate_python(event.to_payload())


def _event_from_outcome(
    row: ProcessChatsEffectReturnEventRow,
    *,
    model: WorkflowModel,
    outcome: ProcessChatsEffectOutcome,
) -> ProcessChatsEvent:
    common: _ProcessChatsEventCommonKwargs = {
        "workflow": PROCESS_CHATS_WORKFLOW,
        "run_id": model.run_id,
        "current_state": model.state,
    }
    match outcome:
        case SetupPathsConfiguredOutcome():
            return PathsConfiguredEvent(**common, config_path=outcome.config_path)
        case SetupBootstrapCompletedOutcome():
            return EnvironmentBootstrapCompletedEvent(**common, bootstrap_summary=outcome.bootstrap_summary)
        case ArchitectCompletedOutcome():
            return ArchitectWorkCompletedEvent(
                **common,
                receipt_id=outcome.receipt_id,
                attestation_hash=outcome.attestation_hash,
                coverage_path=outcome.coverage_path,
                manifest_path=outcome.manifest_path,
            )
        case ArchitectCapacityBlockedOutcome():
            return ArchitectSpecialistCapacityBlockedEvent(
                **common,
                reason_code=outcome.reason_code,
                resume_action=outcome.resume_action,
            )
        case AgentToolContractViolationOutcome():
            return AgentToolContractViolationEvent(
                **common,
                origin_event=outcome.origin_event,
                error_context=outcome.error_context,
            )
        case ExternalReadyOutcome():
            if row.event_model is ArchitectSpecialistCapacityRestoredEvent:
                return ArchitectSpecialistCapacityRestoredEvent(**common, restored_by=outcome.restored_by)
            return ExternalReadyEvent(
                **common,
                restored_by=outcome.restored_by,
                manifest_path=outcome.manifest_path,
                dry_run_receipt_path=outcome.dry_run_receipt_path,
            )
        case AttestationSuppliedOutcome():
            return SubagentPlanAttestationSuppliedEvent(**common, attestation_hash=outcome.attestation_hash)
        case ValidationRetryRequestedOutcome():
            return NoteValidationRetryRequestedEvent(**common, resolved_by=outcome.resolved_by)
        case HumanConfirmedOutcome():
            return VaultGuardConfirmedEvent(**common, confirmed_by=outcome.confirmed_by)
        case HumanRejectedOutcome():
            return VaultGuardRejectedEvent(**common, rejected_by=outcome.rejected_by)
        case HumanApprovedOutcome():
            return HumanPublishApprovalEvent(
                **common,
                approved_by=outcome.approved_by,
                manifest_path=outcome.manifest_path,
                dry_run_receipt_path=outcome.dry_run_receipt_path,
            )
        case HumanCancelledOutcome():
            return HumanPublishCancellationEvent(**common, cancelled_by=outcome.cancelled_by)
        case PublishBatchCompletedOutcome():
            return PublishBatchCompletedEvent(
                **common,
                receipt_id=outcome.receipt_id,
                published_count=outcome.published_count,
                link_trigger_context_path=outcome.link_trigger_context_path,
            )
        case PublishDryRunReceiptRequiredOutcome():
            return PublishDryRunReceiptRequiredEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case PublishStaleReceiptOutcome():
            return PublishStaleReceiptEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case PublishDuplicateTargetOutcome():
            return PublishDuplicateTargetEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case PublishProvenanceGapOutcome():
            return PublishProvenanceGapEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case PublishReceiptInvalidOutcome():
            return PublishReceiptInvalidEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case ExternalQuotaReportedOutcome():
            return ExternalQuotaReportedEvent(
                **common,
                quota_kind=outcome.quota_kind,
                resume_action=outcome.resume_action,
            )
        case PublishBlockerResolvedOutcome():
            return PublishBlockerResolvedEvent(
                **common,
                resolved_by=outcome.resolved_by,
                manifest_path=outcome.manifest_path,
                dry_run_receipt_path=outcome.dry_run_receipt_path,
            )
        case LinkCompletedOutcome():
            return LinkRunCompletedEvent(
                **common,
                receipt_id=outcome.receipt_id,
                changed_files=list(outcome.changed_files),
            )
        case LinkBlockedOutcome():
            return LinkRunBlockedEvent(
                **common,
                reason_code=outcome.reason_code,
                next_action=outcome.next_action,
                error_context=outcome.error_context,
            )
        case RollbackCompletedOutcome():
            return RollbackCompletedEvent(**common, rollback_receipt_id=outcome.rollback_receipt_id)
        case RollbackFailureRecordedOutcome():
            return RollbackFailureRecordedEvent(**common, error_context=outcome.error_context)
        case ContractMissingNextActionOutcome():
            return MissingNextActionEvent(
                **common,
                contract_source=row.origin_state,
                next_action_hint=outcome.next_action_hint,
            )
        case ContractMissingErrorContextOutcome():
            return MissingErrorContextEvent(
                **common,
                contract_source=row.origin_state,
                error_context_hint=outcome.error_context_hint,
            )


@dataclass(frozen=True)
class ProcessChatsEffectBuilderCase:
    origin_state: str
    outcomes: tuple[ProcessChatsEffectOutcome, ...]
    build: Callable[[], WorkflowEffect]


def process_chats_effect_builders(
    *,
    workflow: str,
    run_id: str,
) -> tuple[ProcessChatsEffectBuilderCase, ...]:
    """Return representative effects so matrix coverage tests track all builders."""

    if workflow != PROCESS_CHATS_WORKFLOW:
        raise ValueError(f"process-chats effect builders require workflow {PROCESS_CHATS_WORKFLOW}")
    event = _BuilderEvent(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=run_id,
        current_state=ProcessChatsState.ENVIRONMENT_CHECKING.value,
    )

    def effect_for(
        origin: ProcessChatsState,
        *,
        kind: WorkflowEffectKind,
        target: str,
        payload: ProcessChatsEffectPayload,
        mutates_resources: bool = False,
        rollback_declared: bool = False,
        requires_attestation: bool = False,
        model_policy: dict[str, str] | None = None,
    ) -> WorkflowEffect:
        return _workflow_effect(
            event,
            origin,
            kind=kind,
            target=target,
            payload=payload,
            mutates_resources=mutates_resources,
            rollback_declared=rollback_declared,
            requires_attestation=requires_attestation,
            model_policy=model_policy,
        )

    err = ProcessChatsErrorContext(
        root_cause="contract_gap",
        affected_artifact="builder",
        retry_scope="single-run",
        next_action="Corrigir contrato e retomar.",
    )
    return (
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.ENVIRONMENT_PATHS_MISSING.value,
            outcomes=(
                SetupPathsConfiguredOutcome(config_path="config.toml"),
                ContractMissingNextActionOutcome(next_action_hint="Informar próxima ação de setup."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.ENVIRONMENT_PATHS_MISSING,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="/mednotes:setup paths",
                payload=SetupPathsEffectPayload(missing_path_kind="wiki_dir"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.value,
            outcomes=(
                SetupBootstrapCompletedOutcome(bootstrap_summary="bootstrap ok"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar error_context."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="/mednotes:setup bootstrap",
                payload=SetupBootstrapEffectPayload(reason_code="environment_blocker.windows_path_or_venv"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.ARCHITECT_WORK_REQUESTED.value,
            outcomes=(
                ArchitectCompletedOutcome(
                    receipt_id="receipt",
                    attestation_hash="attestation",
                    coverage_path="/tmp/coverage.json",
                    manifest_path="/tmp/manifest.json",
                ),
                ArchitectCapacityBlockedOutcome(
                    reason_code="specialist_model_quota_exhausted",
                    resume_action="Aguardar capacidade.",
                ),
                AgentToolContractViolationOutcome(origin_event="architect_work_completed", error_context=err),
            ),
            build=lambda: effect_for(
                ProcessChatsState.ARCHITECT_WORK_REQUESTED,
                kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
                target="med-knowledge-architect",
                payload=ArchitectSpecialistEffectPayload(note_plan_hash="note-plan", raw_file_count=1),
                requires_attestation=True,
                model_policy={"specialist": "med-knowledge-architect"},
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY.value,
            outcomes=(
                ExternalReadyOutcome(
                    restored_by="quota_window",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingNextActionOutcome(next_action_hint="Informar retomada."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY,
                kind=WorkflowEffectKind.WAIT_EXTERNAL,
                target="wait_external.specialist_capacity",
                payload=WaitExternalEffectPayload(
                    wait_target="specialist_capacity",
                    blocked_reason="wait_external.specialist_capacity",
                    next_action="Aguardar especialista.",
                    resume_supported=True,
                ),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED.value,
            outcomes=(
                AttestationSuppliedOutcome(attestation_hash="attestation"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de attestation."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="agent.plan_attestation",
                payload=PlanAttestationEffectPayload(reason_code="subagent_plan_attestation_required"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID.value,
            outcomes=(
                AttestationSuppliedOutcome(attestation_hash="attestation"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de attestation."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="agent.plan_attestation",
                payload=PlanAttestationEffectPayload(reason_code="subagent_plan_attestation_invalid"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP.value,
            outcomes=(
                ValidationRetryRequestedOutcome(resolved_by="architect_retry"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de validação."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_architect_work",
                payload=ResumeArchitectWorkEffectPayload(resolved_by="architect_retry"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH.value,
            outcomes=(
                ValidationRetryRequestedOutcome(resolved_by="architect_retry"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de validação."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_architect_work",
                payload=ResumeArchitectWorkEffectPayload(resolved_by="architect_retry"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID.value,
            outcomes=(
                ValidationRetryRequestedOutcome(resolved_by="architect_retry"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de validação."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_architect_work",
                payload=ResumeArchitectWorkEffectPayload(resolved_by="architect_retry"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED.value,
            outcomes=(HumanConfirmedOutcome(confirmed_by="human"), HumanRejectedOutcome(rejected_by="human")),
            build=lambda: effect_for(
                ProcessChatsState.VAULT_GUARD_DECISION_REQUIRED,
                kind=WorkflowEffectKind.ASK_HUMAN,
                target="human.vault_guard_decision",
                payload=VaultGuardDecisionEffectPayload(changed_file_count=1),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION.value,
            outcomes=(
                HumanApprovedOutcome(
                    approved_by="human",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                HumanCancelledOutcome(cancelled_by="human"),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_AWAITING_CONFIRMATION,
                kind=WorkflowEffectKind.ASK_HUMAN,
                target="human.publish_decision",
                payload=HumanPublishDecisionEffectPayload(
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED.value,
            outcomes=(
                PublishBatchCompletedOutcome(
                    receipt_id="receipt",
                    published_count=1,
                    link_trigger_context_path="/tmp/link-trigger.json",
                ),
                PublishDryRunReceiptRequiredOutcome(next_action="Recriar recibo.", error_context=err),
                PublishStaleReceiptOutcome(
                    reason_code="stale_receipt",
                    next_action="Atualizar recibo.",
                    error_context=err,
                ),
                PublishDuplicateTargetOutcome(
                    reason_code="duplicate_target",
                    next_action="Resolver alvo duplicado.",
                    error_context=err,
                ),
                PublishProvenanceGapOutcome(next_action="Corrigir proveniência.", error_context=err),
                PublishReceiptInvalidOutcome(next_action="Recriar recibo.", error_context=err),
                ExternalQuotaReportedOutcome(
                    quota_kind="publish_batch",
                    resume_action="Aguardar quota.",
                ),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="publish-batch",
                payload=PublishBatchEffectPayload(
                    schema_version="medical-notes-workbench.process-chats.publish-batch-effect.v1",
                    kind="publish_batch",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                mutates_resources=True,
                rollback_declared=True,
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA.value,
            outcomes=(
                ExternalReadyOutcome(
                    restored_by="quota_window",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingNextActionOutcome(next_action_hint="Informar retomada de quota."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA,
                kind=WorkflowEffectKind.WAIT_EXTERNAL,
                target="wait_external.publish_quota",
                payload=WaitExternalEffectPayload(
                    wait_target="publish_quota",
                    blocked_reason="wait_external.publish_quota",
                    next_action="Aguardar quota.",
                    resume_supported=True,
                ),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED.value,
            outcomes=(
                PublishBlockerResolvedOutcome(
                    resolved_by="receipt_recreated",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de publish."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_publish_blocker",
                payload=ResumePublishBlockerEffectPayload(reason_code="dry_run_receipt_required"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_STALE_RECEIPT.value,
            outcomes=(
                PublishBlockerResolvedOutcome(
                    resolved_by="receipt_recreated",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de publish."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_STALE_RECEIPT,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_publish_blocker",
                payload=ResumePublishBlockerEffectPayload(reason_code="stale_receipt"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_DUPLICATE_TARGET.value,
            outcomes=(
                PublishBlockerResolvedOutcome(
                    resolved_by="target_deduped",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de publish."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_DUPLICATE_TARGET,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_publish_blocker",
                payload=ResumePublishBlockerEffectPayload(reason_code="duplicate_target"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_PROVENANCE_GAP.value,
            outcomes=(
                PublishBlockerResolvedOutcome(
                    resolved_by="provenance_completed",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de publish."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_PROVENANCE_GAP,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_publish_blocker",
                payload=ResumePublishBlockerEffectPayload(reason_code="provenance_gap"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_RECEIPT_INVALID.value,
            outcomes=(
                PublishBlockerResolvedOutcome(
                    resolved_by="receipt_recreated",
                    manifest_path="/tmp/manifest.json",
                    dry_run_receipt_path="/tmp/dry-run.json",
                ),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de publish."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_RECEIPT_INVALID,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.resume_publish_blocker",
                payload=ResumePublishBlockerEffectPayload(reason_code="publish_receipt_invalid"),
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.LINK_RUN_REQUESTED.value,
            outcomes=(
                LinkCompletedOutcome(receipt_id="link", changed_files=[]),
                LinkBlockedOutcome(
                    reason_code="graph_blockers",
                    next_action="Resolver linker.",
                    error_context=err,
                ),
            ),
            build=lambda: effect_for(
                ProcessChatsState.LINK_RUN_REQUESTED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="/mednotes:link",
                payload=LinkWorkflowRunEffectPayload(
                    kind="link_run",
                    diagnose=False,
                    apply=True,
                    trigger_context_path="/tmp/link-trigger.json",
                    no_related_notes=False,
                ),
                mutates_resources=True,
                rollback_declared=True,
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED.value,
            outcomes=(
                RollbackCompletedOutcome(rollback_receipt_id="rollback"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de rollback."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="vault.rollback",
                payload=RollbackEffectPayload(failed_origin_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED.value),
                mutates_resources=True,
                rollback_declared=True,
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.LINK_RUN_REQUESTED.value,
            outcomes=(
                RollbackCompletedOutcome(rollback_receipt_id="rollback"),
                ContractMissingErrorContextOutcome(error_context_hint="Informar erro de rollback."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.LINK_RUN_REQUESTED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="vault.rollback",
                payload=RollbackEffectPayload(failed_origin_state=ProcessChatsState.LINK_RUN_REQUESTED.value),
                mutates_resources=True,
                rollback_declared=True,
            ),
        ),
        ProcessChatsEffectBuilderCase(
            origin_state=ProcessChatsState.ROLLBACK_RECORDED.value,
            outcomes=(
                RollbackFailureRecordedOutcome(error_context=err),
                ContractMissingErrorContextOutcome(error_context_hint="Informar finalização de falha."),
            ),
            build=lambda: effect_for(
                ProcessChatsState.ROLLBACK_RECORDED,
                kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
                target="workflow.failure_finalization",
                payload=FailureFinalizationEffectPayload(),
            ),
        ),
    )


class _BuilderEvent(ProcessChatsEvent):
    name: Literal["builder_effect_case"] = "builder_effect_case"
