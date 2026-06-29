from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.wiki.contracts.agent_report import (
    ProcessChatsCoverageStatus,
    ProcessChatsLinkerStatus,
    ProcessChatsNotesStatus,
    ProcessChatsObjectiveStatus,
    ProcessChatsPrimaryObjectiveSummary,
    ProcessChatsRawStatus,
)
from mednotes.domains.wiki.contracts.publish import PublishReceipt
from mednotes.domains.wiki.contracts.workflow_blockers import BlockerRegistryError, blocker_entry
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    RejectedAutomation,
    WorkflowDecision,
    WorkflowDecisionKind,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_machine import (
    ProcessChatsBoundaryEvent,
    ProcessChatsMachine,
    ProcessChatsPublishRuntimeObservation,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_machine import (
    ProcessChatsState as MachineProcessChatsState,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind, WorkflowEffectResult
from mednotes.kernel.fsm_event import WorkflowEventLike
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.progress import (
    WorkflowProgressCounts,
    WorkflowProgressEvent,
    WorkflowProgressEventType,
    WorkflowProgressState,
    WorkflowProgressStatus,
    WorkflowProgressViewModel,
    build_progress_view_model,
    progress_state_from_view_model,
)
from mednotes.kernel.public_report import (
    WorkflowPublicReport,
    WorkflowReports,
    assert_public_report_matches_progress,
    public_progress_followup_line,
)
from mednotes.kernel.state_machine import (
    WorkflowStateCategory,
    WorkflowStateMachineSnapshot,
    WorkflowTransition,
    send_workflow_event,
)
from mednotes.kernel.workflow import (
    HumanDecisionPacket,
    ReceiptStatus,
    VersionControlSafety,
    WorkflowReceiptPayload,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

PROCESS_CHATS_WORKFLOW: Literal["/mednotes:process-chats"] = "/mednotes:process-chats"
PROCESS_CHATS_SCHEMA = "medical-notes-workbench.process-chats-fsm-result.v1"
PROCESS_CHATS_RECEIPT_SCHEMA = "medical-notes-workbench.process-chats-receipt.v1"
MEDNOTES_AGENT_DIRECTIVE_SCHEMA = "medical-notes-workbench.agent-directive.v1"
PROCESS_CHATS_AGENT_DIRECTIVE_FIELD = "agent_directive"


PROCESS_CHATS_ALLOWED_ROOT_KEYS = frozenset(
    {
        "schema",
        "workflow",
        "run_id",
        "state_machine_snapshot",
        "progress_view_model",
        "decision",
        "human_decision_packet",
        "receipt",
        "reports",
        "agent_directive",
        "artifacts",
        "version_control_safety",
        "diagnostic_context",
        "error_context",
    }
)
PROCESS_CHATS_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "dry_run",
        "dry_run_receipt",
        "created",
        "created_count",
        "raw_updates",
        "processed_raw_count",
        "publish_receipt",
        "planned_batches",
        "linker",
        "linker_applied",
        "linker_skipped_reason",
        "link_trigger_context_path",
        "linker_trigger_context_path",
        "linker_diagnosis_path",
        "linker_receipt_path",
    }
)

_PHASE_BY_STATE = {
    MachineProcessChatsState.ENVIRONMENT_CHECKING.value: "environment",
    MachineProcessChatsState.ENVIRONMENT_PATHS_MISSING.value: "environment",
    MachineProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.value: "environment",
    MachineProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS.value: "backlog",
    MachineProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS.value: "backlog",
    MachineProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY.value: "backlog",
    MachineProcessChatsState.VAULT_GUARD_DECISION_REQUIRED.value: "vault_guard",
    MachineProcessChatsState.VAULT_GUARD_REJECTED.value: "vault_guard",
    MachineProcessChatsState.TRIAGE_PLANNING.value: "triage",
    MachineProcessChatsState.ARCHITECT_WORK_REQUESTED.value: "architect",
    MachineProcessChatsState.ARCHITECT_AWAITING_SPECIALIST_CAPACITY.value: "architect",
    MachineProcessChatsState.ARCHITECT_REVIEWING_OUTPUT.value: "architect",
    MachineProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED.value: "subagent_plan_attestation",
    MachineProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID.value: "subagent_plan_attestation",
    MachineProcessChatsState.NOTE_VALIDATION_RUNNING.value: "note_validation",
    MachineProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP.value: "note_validation",
    MachineProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH.value: "note_validation",
    MachineProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID.value: "note_validation",
    MachineProcessChatsState.STAGING_MANIFEST_READY.value: "staging",
    MachineProcessChatsState.PUBLISH_AWAITING_CONFIRMATION.value: "publish_preview",
    MachineProcessChatsState.PUBLISH_CANCELLED_BY_HUMAN.value: "publish_preview",
    MachineProcessChatsState.PUBLISH_APPLY_REQUESTED.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_PAUSED_FOR_QUOTA.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_STALE_RECEIPT.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_DUPLICATE_TARGET.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_PROVENANCE_GAP.value: "publish_apply",
    MachineProcessChatsState.PUBLISH_RECEIPT_INVALID.value: "publish_apply",
    MachineProcessChatsState.LINK_RUN_REQUESTED.value: "link_package",
    MachineProcessChatsState.CONTRACT_GAP_MISSING_NEXT_ACTION.value: "contract_gap",
    MachineProcessChatsState.CONTRACT_GAP_MISSING_ERROR_CONTEXT.value: "contract_gap",
    MachineProcessChatsState.AGENT_TOOL_CONTRACT_VIOLATION.value: "agent_tool_contract",
    MachineProcessChatsState.ROLLBACK_RECORDED.value: "rollback",
    MachineProcessChatsState.PUBLISHED.value: "publish_apply",
    MachineProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS.value: "link_package",
    MachineProcessChatsState.TERMINAL_FAILURE_RECORDED.value: "publish_failed",
}


class ProcessChatsOutcomeReason(StrEnum):
    NO_PENDING = "no_pending"
    TRIAGED_READY = "triaged_raw_chats_ready"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHED = "published"
    LINKER_BLOCKED = "process_chats_linker_blocked"
    RECOVERABLE_BLOCKED = "recoverable_blocked"
    WAITING_HUMAN = "waiting_human"
    BLOCKED = "blocked"
    FAILED = "failed"


class ProcessChatsBatchState(ContractModel):
    batch_id: str = ""
    run_id: str = ""
    note_plan_hash: str = ""
    coverage_hash: str = ""
    source_artifact_hash: str = ""
    raw_file: str = ""
    raw_files: str = ""
    coverage_path: str = ""


class ProcessChatsDryRunReceipt(ContractModel):
    path: str = ""
    expires_at: int = Field(default=0, ge=0, strict=True)
    manifest_hash: str = ""
    dry_run_options_hash: str = ""
    batch_state: list[ProcessChatsBatchState] = Field(default_factory=list)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _normalize_expires_at(cls, value: object) -> int:
        if value in (None, ""):
            return 0
        if isinstance(value, bool):
            raise ValueError("expires_at must be an epoch timestamp")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("expires_at must be an epoch timestamp or ISO datetime") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp())
        raise ValueError("expires_at must be an epoch timestamp")


class TaxonomyCanonicalizationStep(ContractModel):
    from_taxonomy: str = Field(default="", alias="from")
    to: str = ""
    under: str = ""


class ProcessChatsDecisionSummary(ContractModel):
    kind: WorkflowDecisionKind
    phase: str = ""
    reason_code: str = ""
    public_summary: str = ""
    developer_summary: str = ""
    rejected_automations: list[RejectedAutomation] = Field(default_factory=list)
    evidence: list[DecisionEvidence] = Field(default_factory=list)


class ProcessChatsArtifactReport(ContractModel):
    schema_id: str = Field(default="", alias="schema")
    scope: str = ""
    required: bool = Field(default=False, strict=True)
    manifest_count: int = Field(default=0, ge=0, strict=True)
    artifact_count: int = Field(default=0, ge=0, strict=True)
    included_artifact_count: int = Field(default=0, ge=0, strict=True)
    covered_artifact_count: int = Field(default=0, ge=0, strict=True)
    missing_artifact_count: int = Field(default=0, ge=0, strict=True)
    errors: list[str] = Field(default_factory=list)
    note: str = ""
    manifests: list[JsonObject] = Field(default_factory=list)
    artifacts: list[JsonObject] = Field(default_factory=list)
    included_artifacts: list[JsonObject] = Field(default_factory=list)
    missing_artifacts: list[JsonObject] = Field(default_factory=list)
    partial_artifacts: list[JsonObject] = Field(default_factory=list)


class ProcessChatsArtifactValidation(ProcessChatsArtifactReport):
    notes: list[ProcessChatsArtifactReport] = Field(default_factory=list)


class ProcessChatsCoverageSource(ContractModel):
    raw_file: str = Field(min_length=1)
    status: Literal["covered", "already_covered", "not_relevant"]
    target_title: str = ""
    target_section: str = ""
    new_information_summary: str = ""
    reference_added: str = ""
    reason: str = ""
    existing_title: str = ""


class ProcessChatsCoverageSummary(ContractModel):
    """Coverage summary accepted at the typed publish-to-FSM boundary."""

    schema_id: str = Field(default="", alias="schema")
    coverage_path: str = ""
    coverage_hash: str = ""
    coverage_hashes: list[str] = Field(default_factory=list)
    raw_file: str = ""
    raw_files: list[str] = Field(default_factory=list)
    multi_source: bool = Field(default=False, strict=True)
    source_count: int = Field(default=0, ge=0, strict=True)
    exhaustive: bool = Field(default=False, strict=True)
    status: str = ""
    item_count: int = Field(default=0, ge=0, strict=True)
    planned_meaning_count: int = Field(default=0, ge=0, strict=True)
    not_a_note_count: int = Field(default=0, ge=0, strict=True)
    raw_file_count: int = Field(default=0, ge=0, strict=True)
    covered_count: int = Field(default=0, ge=0, strict=True)
    sources: list[ProcessChatsCoverageSource] = Field(default_factory=list)
    source_status_counts: dict[str, int] = Field(default_factory=dict)
    staged_note_count: int = Field(default=0, ge=0, strict=True)
    note_plan_hash: str = ""
    batch_id: str = ""
    run_id: str = ""
    source_artifact_hash: str = ""
    note_plan_source_count: int = Field(default=0, ge=0, strict=True)
    note_plan_hashes: dict[str, str] = Field(default_factory=dict)
    note_plan_item_count: int = Field(default=0, ge=0, strict=True)
    note_plan_planned_meaning_count: int = Field(default=0, ge=0, strict=True)
    note_plan_attach_count: int = Field(default=0, ge=0, strict=True)
    note_plan_not_a_note_count: int = Field(default=0, ge=0, strict=True)
    note_plan_needs_context_count: int = Field(default=0, ge=0, strict=True)


class ProcessChatsRawUpdate(ContractModel):
    raw_file: str = Field(min_length=1)
    backup: str | None = None
    updated: bool = Field(strict=True)
    updates: dict[str, str] = Field(default_factory=dict)


class ProcessChatsNewTaxonomyLeafAuthorizationNote(ContractModel):
    target_path: str = Field(min_length=1)
    taxonomy: str = ""
    taxonomy_requested: str = ""
    taxonomy_new_dirs: list[str] = Field(default_factory=list)


class ProcessChatsNewTaxonomyLeafAuthorization(ContractModel):
    required: bool = Field(default=False, strict=True)
    authorized_by_dry_run_receipt: bool = Field(default=False, strict=True)
    note_count: int = Field(default=0, ge=0, strict=True)
    notes: list[ProcessChatsNewTaxonomyLeafAuthorizationNote] = Field(default_factory=list)


class ProcessChatsPlannedNoteSummary(ContractModel):
    title: str = ""
    taxonomy: str = ""
    taxonomy_requested: str = ""
    taxonomy_canonicalized: list[TaxonomyCanonicalizationStep] = Field(default_factory=list)
    taxonomy_new_dirs: list[str] = Field(default_factory=list)
    content_path: str = ""
    target_path: str = ""
    artifact_validation: ProcessChatsArtifactValidation | None = None


class ProcessChatsPlannedBatchSummary(ContractModel):
    raw_file: str = ""
    raw_files: list[str] = Field(default_factory=list)
    notes: list[ProcessChatsPlannedNoteSummary] = Field(default_factory=list)
    coverage_path: str = ""
    coverage: ProcessChatsCoverageSummary | None = None
    artifact_validation: ProcessChatsArtifactValidation | None = None
    batch_state: ProcessChatsBatchState | None = None


class ProcessChatsLinkerRun(ContractModel):
    """Typed evidence packet for a parent workflow delegating to `/mednotes:link`."""

    schema_id: str | None = Field(default=None, alias="schema")
    phase: str = ""
    status: str = ""
    next_action: str = ""
    trigger_context_path: str = ""
    diagnosis_path: str = ""
    receipt_path: str = ""
    diagnosis_status: str = ""
    diagnosis_blocked_reason: str = ""
    blocker_count: int = Field(default=0, ge=0, strict=True)
    linker_applied: bool = Field(default=False, strict=True)
    linker_skipped_reason: str = ""
    apply_status: str = ""
    apply_blocked_reason: str = ""
    changed_files: list[str] = Field(default_factory=list)
    files_changed: int = Field(default=0, ge=0, strict=True)
    workflow_effect_results: list[WorkflowEffectResult] = Field(default_factory=list)


class ProcessChatsContinuationEffect(ContractModel):
    kind: str = Field(min_length=1)
    workflow: Literal["/mednotes:process-chats"] = PROCESS_CHATS_WORKFLOW
    blocked_reason: str = Field(min_length=1)


class ProcessChatsContinuationPlan(ContractModel):
    schema_id: Literal["medical-notes-workbench.process-chats-continuation-plan.v1"] = Field(
        default="medical-notes-workbench.process-chats-continuation-plan.v1",
        alias="schema",
    )
    status: Literal["ready"] = "ready"
    workflow: Literal["/mednotes:process-chats"] = PROCESS_CHATS_WORKFLOW
    lane: str = Field(min_length=1)
    blocked_reason: str = Field(min_length=1)
    next_effect: ProcessChatsContinuationEffect
    retry_budget: int = Field(default=1, ge=1, strict=True)
    summary: str = Field(min_length=1)
    directive_instructions: list[str] = Field(default_factory=list, exclude=True)

    def to_payload(self) -> JsonObject:
        return JsonObjectAdapter.validate_python(self.model_dump(by_alias=True))


class ProcessChatsPublishOperationResult(ContractModel):
    """Closed operation payload consumed by the FSM boundary.

    `runtime_observation` is the sole state-driving publish/link fact packet.
    Historical status fields remain diagnostic UX fields and must not select
    FSM entry states or leaf states at this boundary.
    """

    schema_id: str | None = Field(default=None, alias="schema")
    workflow: str | None = None
    phase: str = ""
    status: Literal[
        "ready_to_publish",
        "published",
        "completed_with_link_blockers",
        "completed",
        "blocked",
        "failed",
    ]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = Field(default=False, strict=True)
    human_decision_packet: HumanDecisionPacket | None = None
    decision_summary: ProcessChatsDecisionSummary | None = None
    error_context: JsonObject = Field(default_factory=dict)
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error: str | None = None
    parse_error: str | None = None
    dry_run: bool = Field(default=False, strict=True)
    backup: bool = Field(default=False, strict=True)
    manifest: str = ""
    manifest_hash: str = ""
    allow_new_taxonomy_leaf: bool = Field(default=True, strict=True)
    require_coverage: bool = Field(default=True, strict=True)
    batch_state: list[ProcessChatsBatchState] = Field(default_factory=list)
    new_taxonomy_leaf_authorization: ProcessChatsNewTaxonomyLeafAuthorization | None = None
    planned_batches: list[ProcessChatsPlannedBatchSummary] = Field(default_factory=list)
    coverage_summary: ProcessChatsCoverageSummary | None = None
    coverage: ProcessChatsCoverageSummary | None = None
    created: list[str] = Field(default_factory=list)
    raw_updates: list[ProcessChatsRawUpdate] = Field(default_factory=list)
    created_count: int = Field(default=0, ge=0, strict=True)
    processed_raw_count: int = Field(default=0, ge=0, strict=True)
    publish_receipt: PublishReceipt | None = None
    dry_run_receipt: ProcessChatsDryRunReceipt | None = None
    linker: ProcessChatsLinkerRun | None = None
    linker_applied: bool = Field(default=False, strict=True)
    linker_skipped_reason: str = ""
    link_trigger_context_path: str = ""
    linker_trigger_context_path: str = ""
    linker_diagnosis_path: str = ""
    linker_receipt_path: str = ""
    runtime_observation: ProcessChatsPublishRuntimeObservation

    @model_validator(mode="after")
    def _workflow_must_match_process_chats(self) -> ProcessChatsPublishOperationResult:
        if self.workflow is not None and self.workflow != PROCESS_CHATS_WORKFLOW:
            raise ValueError("process-chats publish operation result has invalid workflow")
        return self


class ProcessChatsPublishDiagnostic(ContractModel):
    """Non-authoritative publish details rendered after the StateChart decides state."""

    status: str = ""
    receipt_status: str = ""
    dry_run: bool = Field(default=False, strict=True)
    manifest: str = ""
    dry_run_receipt: ProcessChatsDryRunReceipt | None = None
    new_taxonomy_leaf_authorization: ProcessChatsNewTaxonomyLeafAuthorization | None = None


class ProcessChatsLinkerDiagnostic(ContractModel):
    """Non-authoritative linker details rendered after the StateChart decides state."""

    status: str = ""
    next_action: str = ""
    diagnosis_status: str = ""
    applied: bool = Field(default=False, strict=True)
    skipped_reason: str = ""
    blocker_count: int = Field(default=0, ge=0, strict=True)


class ProcessChatsOperationalSummary(ContractModel):
    """Operational counts and artifacts that cannot choose the FSM transition."""

    note_count: int = Field(default=0, ge=0, strict=True)
    raw_count: int = Field(default=0, ge=0, strict=True)
    coverage_raw_count: int = Field(default=0, ge=0, strict=True)
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    mutated: bool = Field(default=False, strict=True)
    changed_files: list[str] = Field(default_factory=list)
    blocked_item_count: int = Field(default=0, ge=0, strict=True)
    next_action: str = ""
    publish: ProcessChatsPublishDiagnostic = Field(default_factory=ProcessChatsPublishDiagnostic)
    linker: ProcessChatsLinkerDiagnostic = Field(default_factory=ProcessChatsLinkerDiagnostic)
    artifacts: JsonObject = Field(default_factory=dict)


class ProcessChatsFsmFacts(ContractModel):
    run_id: str = Field(min_length=1)
    initial_state: MachineProcessChatsState
    event: ProcessChatsBoundaryEvent
    operational_summary: ProcessChatsOperationalSummary = Field(default_factory=ProcessChatsOperationalSummary)
    version_control_safety: VersionControlSafety
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _event_must_match_process_chats_entry(self) -> ProcessChatsFsmFacts:
        if self.event.workflow != PROCESS_CHATS_WORKFLOW:
            raise ValueError(f"process-chats event workflow must be {PROCESS_CHATS_WORKFLOW}")
        if self.event.run_id != self.run_id:
            raise ValueError("process-chats event run_id must match ProcessChatsFsmFacts.run_id")
        if self.event.current_state != self.initial_state.value:
            raise ValueError("process-chats event current_state must match initial_state")
        return self


class _ProcessChatsMachineProjection(ContractModel):
    """Public payload lens derived only from `ProcessChatsMachine`.

    This is not a second workflow state. Effects are emitted by the StateChart
    transition and then projected outward; agent-facing control must not infer
    or rebuild them from status strings or adapter payloads.
    """

    reason: ProcessChatsOutcomeReason
    reason_code: str = ""
    state: MachineProcessChatsState
    category: WorkflowStateCategory
    status: WorkflowProgressStatus
    event_type: WorkflowProgressEventType
    message: str
    trigger: str
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    next_action: str = ""
    resume_action: str = ""
    resume_supported: bool = False
    can_continue_now: bool = False
    effects: list[WorkflowEffect] = Field(default_factory=list)


class _ProcessChatsPayloadProgressView(ContractModel):
    status: StrictStr
    state: StrictStr = ""


class _ProcessChatsPayloadSnapshot(ContractModel):
    current_category: StrictStr


class _ProcessChatsPayloadReceipt(ContractModel):
    status: StrictStr


class _ProcessChatsPayloadFields(ContractModel):
    workflow: Literal["/mednotes:process-chats"]
    progress_view_model: _ProcessChatsPayloadProgressView
    state_machine_snapshot: _ProcessChatsPayloadSnapshot
    receipt: _ProcessChatsPayloadReceipt


class ProcessChatsFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.process-chats-fsm-result.v1"] = Field(
        default=PROCESS_CHATS_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/mednotes:process-chats"] = PROCESS_CHATS_WORKFLOW
    run_id: str = Field(min_length=1)
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    state_machine_snapshot: WorkflowStateMachineSnapshot
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: WorkflowReceiptPayload
    reports: WorkflowReports
    agent_directive: JsonObject
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _hydrate_progress_state_from_public_payload(cls, value: object) -> object:
        """Accept public payloads where progress_state is intentionally hidden."""

        if not isinstance(value, dict) or "progress_state" in value or "progress_view_model" not in value:
            return value
        hydrated = dict(value)
        progress_view = WorkflowProgressViewModel.model_validate(value["progress_view_model"])
        hydrated["progress_state"] = progress_state_from_view_model(progress_view).to_payload()
        return hydrated

    @model_validator(mode="after")
    def _progress_view_model_matches_state(self) -> ProcessChatsFsmResult:
        expected = build_progress_view_model(self.progress_state).to_payload()
        if self.progress_view_model.to_payload() != expected:
            raise ValueError("progress_view_model must match progress_state")
        return self

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "schema": self.schema_id,
            "workflow": self.workflow,
            "run_id": self.run_id,
            "state_machine_snapshot": self.state_machine_snapshot.to_payload(),
            "progress_view_model": self.progress_view_model.to_payload(),
            "decision": self.decision.to_payload() if self.decision is not None else None,
            "human_decision_packet": self.human_decision_packet.to_payload()
            if self.human_decision_packet is not None
            else None,
            "receipt": self.receipt.to_payload(),
            "reports": self.reports.to_payload(),
            "agent_directive": dict(self.agent_directive),
            "artifacts": dict(self.artifacts),
            "version_control_safety": self.version_control_safety.to_payload(),
            "error_context": dict(self.error_context),
        }
        if self.diagnostic_context:
            payload["diagnostic_context"] = dict(self.diagnostic_context)
        payload = JsonObjectAdapter.validate_python(payload)
        assert_process_chats_fsm_payload(payload)
        return payload


def build_process_chats_fsm_result(facts: ProcessChatsFsmFacts) -> ProcessChatsFsmResult:
    machine_model = _process_chats_model_after_event(facts.initial_state, facts.event)
    projection = _project_machine_outcome(facts, machine_model)
    progress_state = _progress_state(facts, projection)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _transition_from_machine_model(machine_model, projection, progress_state)
    receipt = _receipt(facts, projection, progress_state, snapshot)
    reports = _reports(facts, projection, progress_state)
    public_report = reports.public_report
    diagnostic_context = _diagnostic_context(
        facts,
        projection,
    )
    agent_directive = _agent_directive(
        projection,
        progress_view_model=progress_view_model,
        user_visible_summary=public_report.summary_text(),
    )
    diagnostic_context = _problem_diagnostic_context(diagnostic_context, projection, facts.operational_summary)
    if facts.error_context:
        diagnostic_context = diagnostic_context_evidence_only(
            {**diagnostic_context, "error_context": dict(facts.error_context)}
        )
    return ProcessChatsFsmResult(
        run_id=facts.run_id,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
        decision=projection.decision,
        human_decision_packet=projection.human_decision_packet,
        receipt=receipt,
        reports=reports,
        agent_directive=agent_directive,
        artifacts=dict(facts.operational_summary.artifacts),
        version_control_safety=facts.version_control_safety,
        diagnostic_context=diagnostic_context,
        error_context=facts.error_context,
    )


def _process_chats_model_after_event(
    initial_state: MachineProcessChatsState,
    event: ProcessChatsBoundaryEvent,
) -> WorkflowModel:
    model = WorkflowModel.start(workflow=PROCESS_CHATS_WORKFLOW, run_id=event.run_id, initial_state=initial_state.value)
    _send_machine_event(model, event)
    return model


def _send_machine_event(model: WorkflowModel, event: WorkflowEventLike) -> WorkflowTransitionResult:
    machine = ProcessChatsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    return send_workflow_event(machine, event)


def _project_machine_outcome(facts: ProcessChatsFsmFacts, model: WorkflowModel) -> _ProcessChatsMachineProjection:
    transition = model.last_transition
    if transition is None:
        raise ValueError("process-chats machine model has no transition to project")
    state = MachineProcessChatsState(model.state)
    category = _machine_category_for_state(state.value)
    status = _progress_status_for_category(category)
    reason = _machine_reason_for_state(state)
    decision = transition.decision
    next_action = _machine_next_action(facts, transition)
    resume_action = transition.resume_action or (decision.resume_action if decision is not None else "")
    return _projection(
        reason=reason,
        reason_code=transition.reason_code,
        state=state,
        category=category,
        status=status,
        event_type=_event_type_for_status(status),
        decision=decision,
        human_decision_packet=transition.human_decision_packet,
        next_action=next_action,
        resume_action=resume_action,
        resume_supported=bool(resume_action),
        can_continue_now=status == WorkflowProgressStatus.WAITING_AGENT,
        effects=list(transition.effects),
        message=_machine_message_for_state(state, reason),
        trigger=transition.trigger,
    )


def _machine_category_for_state(state: str) -> WorkflowStateCategory:
    model = WorkflowModel.start(workflow=PROCESS_CHATS_WORKFLOW, run_id="category", initial_state=state)
    machine = ProcessChatsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    return machine.category_for_state(state)


def _progress_status_for_category(category: WorkflowStateCategory) -> WorkflowProgressStatus:
    match category:
        case WorkflowStateCategory.PREPARING | WorkflowStateCategory.RUNNING:
            return WorkflowProgressStatus.RUNNING
        case WorkflowStateCategory.WAITING_AGENT:
            return WorkflowProgressStatus.WAITING_AGENT
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return WorkflowProgressStatus.WAITING_EXTERNAL
        case WorkflowStateCategory.WAITING_HUMAN:
            return WorkflowProgressStatus.WAITING_HUMAN
        case WorkflowStateCategory.BLOCKED:
            return WorkflowProgressStatus.BLOCKED
        case WorkflowStateCategory.FAILED:
            return WorkflowProgressStatus.FAILED
        case WorkflowStateCategory.COMPLETED:
            return WorkflowProgressStatus.COMPLETED
        case WorkflowStateCategory.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressStatus.COMPLETED_WITH_WARNINGS


def _event_type_for_status(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    match status:
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.BLOCKED | WorkflowProgressStatus.WAITING_HUMAN | WorkflowProgressStatus.WAITING_AGENT:
            return WorkflowProgressEventType.DECISION_EMITTED
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _machine_reason_for_state(state: MachineProcessChatsState) -> ProcessChatsOutcomeReason:
    match state:
        case MachineProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS | MachineProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS:
            return ProcessChatsOutcomeReason.NO_PENDING
        case MachineProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY:
            return ProcessChatsOutcomeReason.TRIAGED_READY
        case MachineProcessChatsState.PUBLISH_AWAITING_CONFIRMATION:
            return ProcessChatsOutcomeReason.READY_TO_PUBLISH
        case MachineProcessChatsState.PUBLISHED:
            return ProcessChatsOutcomeReason.PUBLISHED
        case MachineProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS:
            return ProcessChatsOutcomeReason.LINKER_BLOCKED
        case MachineProcessChatsState.TERMINAL_FAILURE_RECORDED:
            return ProcessChatsOutcomeReason.FAILED
        case (
            MachineProcessChatsState.ARCHITECT_WORK_REQUESTED
            | MachineProcessChatsState.ENVIRONMENT_PATHS_MISSING
            | MachineProcessChatsState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
            | MachineProcessChatsState.SUBAGENT_PLAN_ATTESTATION_REQUIRED
            | MachineProcessChatsState.SUBAGENT_PLAN_ATTESTATION_INVALID
            | MachineProcessChatsState.NOTE_VALIDATION_COVERAGE_GAP
            | MachineProcessChatsState.NOTE_VALIDATION_MANIFEST_MISMATCH
            | MachineProcessChatsState.NOTE_VALIDATION_CONTENT_INVALID
            | MachineProcessChatsState.PUBLISH_DRY_RUN_RECEIPT_REQUIRED
            | MachineProcessChatsState.PUBLISH_STALE_RECEIPT
            | MachineProcessChatsState.PUBLISH_DUPLICATE_TARGET
            | MachineProcessChatsState.PUBLISH_PROVENANCE_GAP
            | MachineProcessChatsState.PUBLISH_RECEIPT_INVALID
        ):
            return ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED
        case _:
            return ProcessChatsOutcomeReason.BLOCKED


def _machine_next_action(facts: ProcessChatsFsmFacts, transition: WorkflowTransitionResult) -> str:
    if transition.decision is not None and transition.decision.next_action.strip():
        return transition.decision.next_action
    if transition.resume_action.strip():
        return transition.resume_action
    return _default_next_action(facts, _machine_reason_for_state(MachineProcessChatsState(transition.to_state)))


def _machine_message_for_state(state: MachineProcessChatsState, reason: ProcessChatsOutcomeReason) -> str:
    match state:
        case MachineProcessChatsState.BACKLOG_NO_PENDING_RAW_CHATS:
            return "Nenhum chat novo para processar."
        case MachineProcessChatsState.BACKLOG_NO_TRIAGED_RAW_CHATS:
            return "Nenhum chat triado para publicar."
        case MachineProcessChatsState.BACKLOG_TRIAGED_RAW_CHATS_READY:
            return "Nenhum chat bruto novo está pendente; há chats triados aguardando arquitetura."
        case MachineProcessChatsState.PUBLISH_AWAITING_CONFIRMATION:
            return "Previa pronta; aguardando confirmacao humana para publicar."
        case MachineProcessChatsState.PUBLISHED:
            return "Notas publicadas, raws atualizados e conexões executadas."
        case MachineProcessChatsState.COMPLETED_WITH_LINK_BLOCKERS:
            return "Publicacao concluida com pendencias de conexoes/grafo."
        case MachineProcessChatsState.TERMINAL_FAILURE_RECORDED:
            return "Process-chats falhou antes de concluir."
        case _:
            return f"Process-chats em {state.value} por {reason.value}."


def _transition_from_machine_model(
    model: WorkflowModel,
    projection: _ProcessChatsMachineProjection,
    progress_state: WorkflowProgressState,
) -> WorkflowStateMachineSnapshot:
    transition = model.last_transition
    if transition is None:
        raise ValueError("process-chats machine model has no last transition")
    event = _progress_event_from_transition(transition, projection, progress_state)
    projected_transition = WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=projection.category,
        trigger=transition.trigger,
        effects=list(transition.effects),
        progress_events=[event],
        decision=projection.decision,
        resume_action=projection.resume_action,
    )
    return WorkflowStateMachineSnapshot(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=model.run_id,
        current_state=model.state,
        current_category=projection.category,
        transitions=[projected_transition],
        metadata={"reason": projection.reason.value, "statechart": "process_chats_machine"},
    )


def _progress_event_from_transition(
    transition: WorkflowTransitionResult,
    projection: _ProcessChatsMachineProjection,
    progress_state: WorkflowProgressState,
) -> WorkflowProgressEvent:
    return WorkflowProgressEvent(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=transition.run_id,
        state=transition.to_state,
        phase=progress_state.phase,
        event_type=projection.event_type,
        message=projection.message,
        status=projection.status,
        current=progress_state.current,
        total=progress_state.total,
        counts=progress_state.counts,
        resume_action=projection.resume_action,
        resume_supported=projection.resume_supported,
        can_continue_now=projection.can_continue_now,
        decision=progress_state.decision,
        technical_context=progress_state.technical_context,
    )


def _projection(
    *,
    reason: ProcessChatsOutcomeReason,
    reason_code: str = "",
    state: MachineProcessChatsState,
    category: WorkflowStateCategory,
    status: WorkflowProgressStatus,
    event_type: WorkflowProgressEventType,
    message: str,
    trigger: str,
    decision: WorkflowDecision | None = None,
    human_decision_packet: HumanDecisionPacket | None = None,
    next_action: str = "",
    resume_action: str = "",
    resume_supported: bool = False,
    can_continue_now: bool = False,
    effects: list[WorkflowEffect] | None = None,
) -> _ProcessChatsMachineProjection:
    return _ProcessChatsMachineProjection(
        reason=reason,
        reason_code=reason_code,
        state=state,
        category=category,
        status=status,
        event_type=event_type,
        decision=decision,
        human_decision_packet=human_decision_packet,
        next_action=next_action,
        resume_action=resume_action,
        resume_supported=resume_supported,
        can_continue_now=can_continue_now,
        effects=list(effects or []),
        message=message,
        trigger=trigger,
    )


def _default_next_action(facts: ProcessChatsFsmFacts, reason: ProcessChatsOutcomeReason) -> str:
    linker_next_action = _linker_next_action_after_link_attempt(
        facts.operational_summary.linker.next_action.strip()
        if reason == ProcessChatsOutcomeReason.LINKER_BLOCKED
        else ""
    )
    if facts.operational_summary.next_action.strip():
        next_action = _linker_next_action_after_link_attempt(facts.operational_summary.next_action.strip())
        if linker_next_action and linker_next_action not in next_action:
            return f"{next_action} Detalhe das conexões/grafo: {linker_next_action}"
        return next_action
    if linker_next_action:
        return f"Resolver pendências de conexões/grafo: {linker_next_action}"
    match reason:
        case ProcessChatsOutcomeReason.NO_PENDING:
            return ""
        case ProcessChatsOutcomeReason.TRIAGED_READY:
            return "Continuar para os chats triados com list-triados e plan-subagents --phase architect."
        case ProcessChatsOutcomeReason.READY_TO_PUBLISH:
            return "Revisar a prévia e confirmar publicação pela rota oficial."
        case ProcessChatsOutcomeReason.LINKER_BLOCKED:
            return "Resolver pendências de conexões/grafo pela rota oficial antes de considerar o lote concluído."
        case ProcessChatsOutcomeReason.WAITING_HUMAN:
            return "Responder a decisão solicitada para continuar."
        case ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED:
            return "Continuar automaticamente pela etapa de recuperacao oficial antes de concluir."
        case ProcessChatsOutcomeReason.BLOCKED:
            return "Corrigir o bloqueio informado e repetir /mednotes:process-chats pela rota oficial."
        case ProcessChatsOutcomeReason.FAILED:
            return "Revisar o erro do workflow e retomar /mednotes:process-chats pela rota oficial."
        case ProcessChatsOutcomeReason.PUBLISHED:
            return ""


def _linker_next_action_after_link_attempt(value: str) -> str:
    """Drop stale pre-link diagnosis commands after the linker child already ran."""

    if "run-linker --diagnose" not in value:
        return value
    detail_marker = "Detalhe das conexões/grafo:"
    if detail_marker in value:
        detail = value.split(detail_marker, maxsplit=1)[1].strip()
        if detail:
            return detail
    return "Resolver pendências de conexões/grafo pela rota oficial antes de considerar o lote concluído."


def _progress_state(facts: ProcessChatsFsmFacts, projection: _ProcessChatsMachineProjection) -> WorkflowProgressState:
    summary = facts.operational_summary
    note_count = summary.note_count
    raw_count = summary.raw_count
    coverage_count = summary.coverage_raw_count
    planned = max(note_count, summary.planned_note_count, coverage_count, raw_count)
    current = planned if projection.status == WorkflowProgressStatus.COMPLETED else note_count
    if projection.reason == ProcessChatsOutcomeReason.READY_TO_PUBLISH:
        current = planned
    counts = WorkflowProgressCounts(
        planned_items=planned,
        processed_items=current,
        mutated_files=note_count if summary.mutated else 0,
        written_files=note_count if summary.mutated else 0,
        blocked_items=_blocked_item_count(facts, projection),
    )
    return WorkflowProgressState(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=facts.run_id,
        state=projection.state.value,
        phase=_PHASE_BY_STATE[projection.state.value],
        event_type=projection.event_type,
        message=projection.message,
        status=projection.status,
        current=current,
        total=planned,
        counts=counts,
        resume_action=projection.resume_action,
        resume_supported=projection.resume_supported,
        can_continue_now=projection.can_continue_now,
        decision=projection.decision.decision_summary() if projection.decision is not None else None,
        technical_context={
            "reason": projection.reason.value,
            "trigger": projection.trigger,
            "process_status": projection.state.value,
            "note_count": note_count,
            "raw_count": raw_count,
        },
    )


def _receipt(
    facts: ProcessChatsFsmFacts,
    projection: _ProcessChatsMachineProjection,
    progress_state: WorkflowProgressState,
    snapshot: WorkflowStateMachineSnapshot,
) -> WorkflowReceiptPayload:
    view_model = build_progress_view_model(progress_state)
    return WorkflowReceiptPayload(
        schema=PROCESS_CHATS_RECEIPT_SCHEMA,
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=facts.run_id,
        status=_receipt_status(projection),
        mutated=facts.operational_summary.mutated,
        next_action=_receipt_next_action(projection),
        human_decision_required=projection.status == WorkflowProgressStatus.WAITING_HUMAN,
        human_decision_packet=projection.human_decision_packet,
        changed_files=list(facts.operational_summary.changed_files),
        version_control_safety=facts.version_control_safety,
        progress_state=progress_state,
        progress_view_model=view_model,
        state_machine_snapshot=snapshot,
    )


def _receipt_status(projection: _ProcessChatsMachineProjection) -> ReceiptStatus:
    match projection.status:
        case WorkflowProgressStatus.COMPLETED:
            return "completed"
        case WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return "completed_with_warnings"
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "waiting_human"
        case WorkflowProgressStatus.WAITING_AGENT:
            return "waiting_agent"
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "waiting_external"
        case WorkflowProgressStatus.BLOCKED:
            return "blocked"
        case WorkflowProgressStatus.FAILED:
            return "failed"
        case _:
            return "blocked"


def _receipt_next_action(projection: _ProcessChatsMachineProjection) -> str:
    if projection.reason == ProcessChatsOutcomeReason.PUBLISHED:
        return ""
    return projection.next_action


def _reports(
    facts: ProcessChatsFsmFacts,
    projection: _ProcessChatsMachineProjection,
    progress_state: WorkflowProgressState,
) -> WorkflowReports:
    note_count = facts.operational_summary.note_count
    raw_count = facts.operational_summary.raw_count
    match projection.reason:
        case ProcessChatsOutcomeReason.NO_PENDING:
            summary = "Nenhum chat novo para processar."
        case ProcessChatsOutcomeReason.TRIAGED_READY:
            summary = "Nenhum chat bruto novo está pendente; ainda há chats triados para preparar."
        case ProcessChatsOutcomeReason.READY_TO_PUBLISH:
            summary = "Prévia pronta; nenhuma nota foi publicada."
        case ProcessChatsOutcomeReason.PUBLISHED:
            summary = f"Publiquei {note_count} nota(s), atualizei {raw_count} raw chat(s) e rodei o pacote de links."
        case ProcessChatsOutcomeReason.LINKER_BLOCKED:
            summary = f"Publiquei {note_count} nota(s), mas o pacote de links/grafo ficou pendente."
        case ProcessChatsOutcomeReason.WAITING_HUMAN:
            summary = "Preciso de uma escolha sua antes de continuar o process-chats."
        case ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED:
            summary = "Encontrei uma pendencia recuperavel e vou continuar pela rota oficial."
        case ProcessChatsOutcomeReason.BLOCKED:
            summary = "Process-chats bloqueou antes de publicar o lote."
        case ProcessChatsOutcomeReason.FAILED:
            summary = "Process-chats falhou antes de concluir."
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=facts.run_id,
        headline=summary,
        lines=public_lines,
    )
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details={"primary_objective_summary": _primary_objective_summary(facts, projection).to_payload()},
    )


def _primary_objective_summary(
    facts: ProcessChatsFsmFacts,
    projection: _ProcessChatsMachineProjection,
) -> ProcessChatsPrimaryObjectiveSummary:
    """Derive the public objective answer from FSM facts, not legacy fields."""
    note_count = facts.operational_summary.note_count
    raw_count = facts.operational_summary.raw_count
    coverage_count = facts.operational_summary.coverage_raw_count
    status: ProcessChatsObjectiveStatus = _primary_process_status(projection)
    linker_status = _primary_linker_status(facts, projection)
    return ProcessChatsPrimaryObjectiveSummary(
        process_status=status,
        process_summary=_primary_process_summary(status=status, note_count=note_count, raw_count=raw_count),
        notes_status=_primary_notes_status(status),
        note_count=note_count,
        wiki_write_summary=_primary_wiki_write_summary(status=status, note_count=note_count),
        raw_status=_primary_raw_status(status=status, raw_count=raw_count, coverage_count=coverage_count),
        raw_count=raw_count,
        raw_summary=_primary_raw_summary(status=status, raw_count=raw_count, coverage_count=coverage_count),
        coverage_status=_primary_coverage_status(status=status, coverage_count=coverage_count),
        coverage_summary=_primary_coverage_summary(status=status, coverage_count=coverage_count),
        linker_status=linker_status,
        linker_summary=_primary_linker_summary(linker_status=linker_status, facts=facts, status=status),
    )


def _primary_process_status(projection: _ProcessChatsMachineProjection) -> ProcessChatsObjectiveStatus:
    match projection.reason:
        case ProcessChatsOutcomeReason.NO_PENDING:
            return "no_pending"
        case ProcessChatsOutcomeReason.TRIAGED_READY:
            return "ready_to_publish"
        case ProcessChatsOutcomeReason.READY_TO_PUBLISH | ProcessChatsOutcomeReason.WAITING_HUMAN:
            return "ready_to_publish"
        case ProcessChatsOutcomeReason.PUBLISHED:
            return "published"
        case ProcessChatsOutcomeReason.LINKER_BLOCKED:
            return "completed_with_link_blockers"
        case ProcessChatsOutcomeReason.FAILED:
            return "failed"
        case ProcessChatsOutcomeReason.BLOCKED | ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED:
            return "blocked"


def _primary_process_summary(*, status: ProcessChatsObjectiveStatus, note_count: int, raw_count: int) -> str:
    match status:
        case "ready_to_publish":
            return "process-chats preparou a prévia; nenhuma nota foi publicada."
        case "no_pending":
            return "Não havia raw chat novo ou triado para processar; nada foi publicado."
        case "published":
            return f"process-chats publicou {note_count} nota(s) e processou {raw_count} raw chat(s)."
        case "completed_with_link_blockers":
            return (
                f"process-chats publicou {note_count} nota(s) e processou {raw_count} raw chat(s), "
                "mas as conexões/grafo ficaram pendentes."
            )
        case "failed":
            return "process-chats falhou antes de concluir a publicação."
        case _:
            return "process-chats bloqueou antes de concluir a publicação."


def _primary_notes_status(status: ProcessChatsObjectiveStatus) -> ProcessChatsNotesStatus:
    match status:
        case "no_pending":
            return "not_written"
        case "ready_to_publish":
            return "ready_to_publish"
        case "published" | "completed_with_link_blockers":
            return "published"
        case "blocked" | "failed":
            return "blocked"
        case _:
            return "unknown"


def _primary_wiki_write_summary(*, status: ProcessChatsObjectiveStatus, note_count: int) -> str:
    match status:
        case "no_pending":
            return "Nenhuma nota foi escrita porque não havia chat novo para processar."
        case "ready_to_publish":
            return "Nenhum arquivo da Wiki foi escrito; a publicação ainda está em prévia."
        case "published" | "completed_with_link_blockers":
            return f"{note_count} arquivo(s) da Wiki foram escritos."
        case "blocked" | "failed":
            return "Nenhum arquivo da Wiki deve ser considerado publicado neste estado."
        case _:
            return "O payload FSM não confirmou escrita real na Wiki."


def _primary_raw_status(
    *,
    status: ProcessChatsObjectiveStatus,
    raw_count: int,
    coverage_count: int,
) -> ProcessChatsRawStatus:
    if status == "no_pending":
        return "not_processed"
    if status == "ready_to_publish":
        return "covered" if coverage_count else "not_processed"
    if status in {"published", "completed_with_link_blockers"}:
        return "processed" if raw_count else "unknown"
    if status in {"blocked", "failed"}:
        return "not_processed"
    return "unknown"


def _primary_raw_summary(*, status: ProcessChatsObjectiveStatus, raw_count: int, coverage_count: int) -> str:
    if status == "no_pending":
        return "Nenhum raw chat foi processado porque não havia item novo nesta fase."
    if status == "ready_to_publish":
        if coverage_count:
            return f"{coverage_count} raw chat(s) estão cobertos, mas ainda não foram marcados como processados."
        return "Nenhum raw chat foi marcado como processado nesta prévia."
    if status in {"published", "completed_with_link_blockers"}:
        if raw_count:
            return f"{raw_count} raw chat(s) foram marcados como processados."
        return "O payload FSM publicou notas, mas não confirmou raws processados."
    if status in {"blocked", "failed"}:
        return "Nenhum raw chat deve ser considerado processado neste estado."
    return "O payload FSM não confirmou cobertura ou processamento dos raw chats."


def _primary_coverage_status(*, status: ProcessChatsObjectiveStatus, coverage_count: int) -> ProcessChatsCoverageStatus:
    if status == "no_pending":
        return "not_applicable"
    if status == "ready_to_publish" and coverage_count:
        return "valid"
    if status in {"published", "completed_with_link_blockers"} and coverage_count:
        return "valid"
    if status in {"blocked", "failed"}:
        return "unknown"
    return "unknown"


def _primary_coverage_summary(*, status: ProcessChatsObjectiveStatus, coverage_count: int) -> str:
    if status == "no_pending":
        return "Coverage/manifest não se aplicam porque nenhuma publicação foi preparada."
    if coverage_count:
        return f"Coverage/manifest coerentes para {coverage_count} raw chat(s)."
    if status == "ready_to_publish":
        return "O payload FSM não trouxe confirmação suficiente de coverage/manifest."
    return "Coverage/manifest não foram confirmados neste estado."


def _primary_linker_status(
    facts: ProcessChatsFsmFacts,
    projection: _ProcessChatsMachineProjection,
) -> ProcessChatsLinkerStatus:
    if projection.reason == ProcessChatsOutcomeReason.NO_PENDING:
        return "not_applicable"
    if projection.reason == ProcessChatsOutcomeReason.TRIAGED_READY:
        return "not_run"
    if projection.reason == ProcessChatsOutcomeReason.READY_TO_PUBLISH:
        return "not_run"
    if projection.reason == ProcessChatsOutcomeReason.LINKER_BLOCKED:
        return "blocked"
    if projection.reason == ProcessChatsOutcomeReason.PUBLISHED:
        linker = facts.operational_summary.linker
        if linker.applied or linker.status == "completed" or linker.blocker_count == 0:
            return "applied"
        return "unknown"
    if projection.reason in {ProcessChatsOutcomeReason.BLOCKED, ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED, ProcessChatsOutcomeReason.FAILED}:
        return "not_run"
    return "unknown"


def _primary_linker_summary(
    *,
    linker_status: ProcessChatsLinkerStatus,
    facts: ProcessChatsFsmFacts,
    status: ProcessChatsObjectiveStatus,
) -> str:
    linker = facts.operational_summary.linker
    match linker_status:
        case "not_applicable":
            return "Conexões/grafo não se aplicam porque nenhuma nota foi publicada."
        case "not_run":
            if status == "ready_to_publish":
                return "Conexões/grafo ainda não rodaram porque a publicação não foi confirmada."
            return "Conexões/grafo não rodaram porque a publicação não foi concluída."
        case "applied":
            return "Conexões/grafo aplicadas sem bloqueios."
        case "blocked":
            reason = linker.skipped_reason or f"{linker.blocker_count} blocker(s)"
            return f"Conexões/grafo ficaram pendentes: {reason}."
        case _:
            return "O payload FSM não confirmou o estado de conexões/grafo."


def _diagnostic_context(
    facts: ProcessChatsFsmFacts,
    projection: _ProcessChatsMachineProjection,
) -> JsonObject:
    """Build explanatory diagnostics without carrying executable control."""

    summary = facts.operational_summary
    publish = summary.publish
    linker = summary.linker
    publish_context: JsonObject = {
        "status": publish.status,
        "receipt_status": publish.receipt_status,
        "dry_run": publish.dry_run,
        "manifest": publish.manifest,
    }
    if publish.dry_run_receipt is not None:
        publish_context["dry_run_receipt"] = {
            "path": publish.dry_run_receipt.path,
            "expires_at": publish.dry_run_receipt.expires_at,
            "manifest_hash": publish.dry_run_receipt.manifest_hash,
            "dry_run_options_hash": publish.dry_run_receipt.dry_run_options_hash,
            "batch_state": [item.to_payload() for item in publish.dry_run_receipt.batch_state],
        }
    if publish.new_taxonomy_leaf_authorization:
        publish_context["new_taxonomy_leaf_authorization"] = publish.new_taxonomy_leaf_authorization.to_payload()
    context: JsonObject = {
        "schema": "medical-notes-workbench.process-chats-fsm-diagnostic-context.v1",
        "reason": projection.reason.value,
        "outcome_reason": projection.reason.value,
        "state": projection.state.value,
        "publish": publish_context,
        "counts": {
            "note_count": summary.note_count,
            "raw_count": summary.raw_count,
            "coverage_raw_count": summary.coverage_raw_count,
            "linker_applied": linker.applied,
        },
        "linker": {
            "status": linker.status,
            "diagnosis_status": linker.diagnosis_status,
            "applied": linker.applied,
            "skipped_reason": linker.skipped_reason,
            "blocker_count": linker.blocker_count,
        },
    }
    return diagnostic_context_evidence_only(context)


def _agent_directive(
    projection: _ProcessChatsMachineProjection,
    *,
    progress_view_model: WorkflowProgressViewModel,
    user_visible_summary: str,
) -> JsonObject:
    """Build the root executable agent contract directly from FSM state."""

    directive_instructions: list[str] = []
    if projection.reason == ProcessChatsOutcomeReason.RECOVERABLE_BLOCKED:
        plan = _recoverable_blocker_plan(projection.reason_code or projection.trigger)
        if plan is None:
            raise ValueError("recoverable process-chats diagnostic context requires a recovery plan")
        directive_instructions = list(plan.directive_instructions)
    typed = agent_directive_from_progress_view_model(
        progress_view_model,
        schema=MEDNOTES_AGENT_DIRECTIVE_SCHEMA,
        reason=projection.reason.value,
        effects=projection.effects,
        blockers=_blocked_by_for_directive(projection),
        resume=projection.resume_action,
        report_requires=["primary_objective", "raw_coverage", "manifest", "linker"],
        summary=user_visible_summary,
        instructions=_plain_agent_directive_instructions(directive_instructions),
    )
    return JsonObjectAdapter.validate_python(typed.to_payload())


def _problem_diagnostic_context(
    context: JsonObject,
    projection: _ProcessChatsMachineProjection,
    summary: ProcessChatsOperationalSummary,
) -> JsonObject:
    publish = summary.publish
    linker = summary.linker
    if projection.status == WorkflowProgressStatus.COMPLETED and publish.dry_run is True:
        return diagnostic_context_evidence_only(context)
    if projection.status == WorkflowProgressStatus.COMPLETED:
        linker_deviation = linker.applied is not True or bool(linker.skipped_reason.strip())
        if linker_deviation:
            return diagnostic_context_evidence_only(context)
        return {}
    return diagnostic_context_evidence_only(context)


def _blocked_by_for_directive(projection: _ProcessChatsMachineProjection) -> list[str]:
    if projection.status not in {
        WorkflowProgressStatus.BLOCKED,
        WorkflowProgressStatus.FAILED,
        WorkflowProgressStatus.WAITING_EXTERNAL,
        WorkflowProgressStatus.WAITING_HUMAN,
    }:
        return []
    if projection.decision is not None:
        return [projection.decision.reason_code]
    return [projection.trigger or projection.reason.value]


def _plain_agent_directive_instructions(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        text = line.strip()
        prefix = "agent_instruction:"
        if text.casefold().startswith(prefix):
            text = text[len(prefix):].strip()
        if text:
            cleaned.append(text)
    return cleaned


def assert_process_chats_fsm_payload(payload: JsonObject) -> None:
    forbidden_keys = set(payload) & PROCESS_CHATS_FORBIDDEN_ROOT_KEYS
    if forbidden_keys:
        raise ValueError(f"process-chats FSM payload contains non-FSM root fields: {sorted(forbidden_keys)}")
    required_root_keys = PROCESS_CHATS_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing_keys = required_root_keys - set(payload)
    if missing_keys:
        raise ValueError(f"process-chats FSM payload missing canonical root fields: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - PROCESS_CHATS_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"process-chats FSM payload contains unexpected root fields: {sorted(unexpected_keys)}")
    diagnostic_context = payload["diagnostic_context"] if "diagnostic_context" in payload else {}
    assert_diagnostic_context_evidence_only(diagnostic_context)
    if isinstance(diagnostic_context, dict) and "agent_directive" in diagnostic_context:
        raise ValueError("process-chats FSM diagnostic_context must not contain agent_directive")
    fields = _process_chats_payload_fields(payload)
    if fields.progress_view_model.status != fields.state_machine_snapshot.current_category:
        raise ValueError("process-chats FSM status must match state_machine_snapshot category")
    if fields.receipt.status != fields.progress_view_model.status:
        raise ValueError("process-chats FSM receipt status must match progress view status")
    if fields.progress_view_model.status in {
        WorkflowStateCategory.BLOCKED.value,
        WorkflowStateCategory.FAILED.value,
    } and not payload["error_context"]:
        raise ValueError("process-chats FSM blocked/failed payload requires error_context")
    reports = WorkflowReports.model_validate(payload["reports"])
    if "human" in payload["reports"]:
        raise ValueError("process-chats FSM reports must not expose legacy human report text")
    public_report = reports.public_report
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    assert_public_report_matches_progress(
        public_report,
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="process-chats FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(payload[PROCESS_CHATS_AGENT_DIRECTIVE_FIELD]),
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="process-chats FSM",
    )
    _assert_process_chats_machine_snapshot(snapshot)


def _assert_process_chats_machine_snapshot(snapshot: WorkflowStateMachineSnapshot) -> None:
    if snapshot.workflow != PROCESS_CHATS_WORKFLOW:
        raise ValueError("process-chats FSM snapshot has invalid workflow")
    if snapshot.current_category != _machine_category_for_state(snapshot.current_state):
        raise ValueError("process-chats FSM snapshot category does not match StateChart state")
    edges = _process_chats_machine_edges()
    for transition in snapshot.transitions:
        if transition.to_category != _machine_category_for_state(transition.to_state):
            raise ValueError("process-chats FSM transition category does not match StateChart state")
        edge = (transition.trigger, transition.from_state, transition.to_state)
        if edge not in edges:
            raise ValueError(f"unauthorized FSM transition: {edge}")


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """Declare which executable effects may be projected to the agent per lane."""

    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.CALL_SPECIALIST_MODEL, WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case WorkflowStateCategory.BLOCKED | WorkflowStateCategory.FAILED:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW}
        case _:
            return set()


def _process_chats_machine_edges() -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for event in ProcessChatsMachine.events:
        for transition in event._transitions:
            for target in transition._targets:
                edges.add((event.id, str(transition.source.value), str(target.value)))
    return edges


def _process_chats_payload_fields(payload: JsonObject) -> _ProcessChatsPayloadFields:
    raw_fields: JsonObject = {
        "workflow": payload["workflow"],
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status", "state")),
        "state_machine_snapshot": _json_object_subset(payload, "state_machine_snapshot", ("current_category",)),
        "receipt": _json_object_subset(payload, "receipt", ("status",)),
    }
    try:
        return _ProcessChatsPayloadFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"process-chats FSM payload invalid: {loc}: {msg}") from exc


def _json_object_subset(payload: JsonObject, field_name: str, keys: tuple[str, ...]) -> JsonObject:
    try:
        source = JsonObjectAdapter.validate_python(payload[field_name])
    except PydanticValidationError as exc:
        raise ValueError(f"process-chats FSM payload invalid: {field_name} must be an object") from exc
    return {key: source[key] for key in keys if key in source}


def process_chats_cli_exit_code(payload: JsonObject) -> int:
    fields = _process_chats_payload_fields(payload)
    status = fields.progress_view_model.status
    match status:
        case "completed" | "completed_with_warnings":
            return 0
        case "waiting_human" if (
            fields.progress_view_model.state == MachineProcessChatsState.PUBLISH_AWAITING_CONFIRMATION.value
        ):
            return 0
        case "waiting_human" | "waiting_agent" | "blocked":
            return 3
        case "failed":
            return 5
        case _:
            return 1


def _blocked_item_count(facts: ProcessChatsFsmFacts, projection: _ProcessChatsMachineProjection) -> int:
    if projection.status not in {
        WorkflowProgressStatus.BLOCKED,
        WorkflowProgressStatus.FAILED,
        WorkflowProgressStatus.WAITING_HUMAN,
        WorkflowProgressStatus.WAITING_AGENT,
    }:
        return 0
    return max(1, facts.operational_summary.blocked_item_count)


def _recoverable_blocker_plan(blocked_reason: str) -> ProcessChatsContinuationPlan | None:
    code = blocked_reason.strip()
    if not code:
        return None
    if code in {"ValidationError", "validation_errors", "validation_failed"}:
        lane, effect = _recovery_lane_and_effect(code)
        return ProcessChatsContinuationPlan(
            lane=lane,
            blocked_reason=code,
            next_effect=ProcessChatsContinuationEffect(kind=effect, blocked_reason=code),
            summary=_recovery_summary(code),
            directive_instructions=[
                "agent_instruction: workflow is waiting_agent, not completed.",
                "agent_instruction: repair or quarantine the invalid item before writing a final success report.",
            ],
        )
    try:
        entry = blocker_entry(code)
    except BlockerRegistryError:
        return None
    if entry.default_decision not in {
        WorkflowDecisionKind.AUTO_FIX,
        WorkflowDecisionKind.AUTO_PLAN,
        WorkflowDecisionKind.AUTO_DEFER,
    }:
        return None
    lane, effect = _recovery_lane_and_effect(code)
    return ProcessChatsContinuationPlan(
        lane=lane,
        blocked_reason=code,
        next_effect=ProcessChatsContinuationEffect(kind=effect, blocked_reason=code),
        summary=_recovery_summary(code),
        directive_instructions=[
            "agent_instruction: workflow is waiting_agent, not completed.",
            "agent_instruction: run the official next_effect before writing a final success report.",
        ],
    )


def _recovery_lane_and_effect(blocked_reason: str) -> tuple[str, str]:
    match blocked_reason:
        case "coverage_invalid" | "coverage_path_missing":
            return ("coverage_recovery", "regenerate_raw_coverage")
        case "dry_run_receipt_invalid" | "new_taxonomy_leaf_requires_dry_run_authorization":
            return ("publish_preview_recovery", "rerun_publish_preview")
        case "ValidationError" | "validation_errors" | "validation_failed":
            return ("note_validation_repair", "repair_or_quarantine_note")
        case "taxonomy_action_required" | "taxonomy_plan_blocked":
            return ("taxonomy_resolution", "resolve_taxonomy_or_quarantine")
        case _:
            return ("workflow_recovery", "recover_process_chats_blocker")


def _recovery_summary(blocked_reason: str) -> str:
    match blocked_reason:
        case "coverage_invalid" | "coverage_path_missing":
            return "Reconstruir a cobertura dos raw chats pela rota oficial e repetir a etapa de publicacao."
        case "dry_run_receipt_invalid" | "new_taxonomy_leaf_requires_dry_run_authorization":
            return "Gerar uma nova previa oficial de publicacao antes de aplicar o lote."
        case "ValidationError" | "validation_errors" | "validation_failed":
            return "Reparar ou quarentenar a nota invalida e continuar os demais itens seguros."
        case "taxonomy_action_required" | "taxonomy_plan_blocked":
            return "Resolver a taxonomia pela politica oficial antes de publicar o item afetado."
        case _:
            return "Recuperar o blocker pela rota oficial do process-chats antes de concluir."
