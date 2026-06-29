from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from mednotes.domains.wiki.contracts.agent_run_audit import WorkflowTranscriptAuditResult
from mednotes.domains.wiki.contracts.happy_path import HappyPathRunMetrics
from mednotes.domains.wiki.contracts.public_report import WorkflowPublicReportViewModel
from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.public_report import WorkflowPrimaryObjectiveSummary


class AgentRunReportFindingCode(StrEnum):
    RECEIPT_STATUS_MISMATCH = "agent.final_report_receipt_status_mismatch"
    PROGRESS_STATUS_MISMATCH = "agent.final_report_progress_status_mismatch"
    WORKFLOW_STATUS_OMITTED = "agent.final_report_workflow_status_omitted"
    OMITTED_TOOL_ERROR = "agent.final_report_omitted_tool_error"
    REPORTED_ARTIFACT_PATH_INVALID = "agent.final_report_artifact_path_invalid"
    FINAL_REPORT_LOCAL_PATH_LEAK = "agent.final_report_local_path_leak"
    SUCCESS_CLAIM_MISMATCH = "agent.final_report_success_claim_mismatch"
    PUBLIC_OUTPUT_INTERNAL_TERM_LEAK = "agent.public_output_internal_term_leak"
    STALE_NEXT_ACTION = "agent.workflow_stale_next_action"
    PRIMARY_OBJECTIVE_OMITTED = "agent.final_report_primary_objective_omitted"
    TOOL_DEVIATION_OMITTED = "agent.final_report_tool_deviation_omitted"
    WORKFLOW_CONTRACT_CONTRADICTION = "agent.workflow_contract_contradiction"
    AGENT_EVENT_OMITTED = "agent.final_report_agent_event_omitted"
    VERSION_CONTROL_SAFETY_OMITTED = "agent.final_report_version_control_safety_omitted"
    API_ACCOUNTING_MISMATCH = "agent.final_report_api_accounting_mismatch"
    OPERATIONAL_WARNING_OMITTED = "agent.final_report_operational_warning_omitted"
    WAITING_AGENT_CONTINUATION_OMITTED = "agent.final_report_waiting_agent_continuation_omitted"
    FINAL_REPORT_NOT_ALLOWED = "agent.final_report_not_allowed"
    WORKFLOW_AGENT_DIRECTIVE_INVALID = "agent.workflow_agent_directive_invalid"
    MISSING_ERROR_CONTEXT_ROOT_CAUSE = "agent.final_report_missing_error_context_root_cause"
    CONTENT_QUALITY_AUDIT_OMITTED = "agent.final_report_content_quality_audit_omitted"
    RUNTIME_ERROR_OMITTED = "agent.final_report_runtime_error_omitted"
    RUNTIME_PERFORMANCE_BUG = "agent.runtime_performance_bug"
    RUNTIME_ROUTE_PROBE_OBSERVED = "agent.runtime_route_probe_observed"
    SPECIALIST_MODEL_POLICY_VIOLATION = "agent.specialist_model_policy_violation"
    BLOCKED_TOOL_RESULT_OMITTED = "agent.final_report_blocked_tool_result_omitted"
    RUN_FINISH_OMITTED = "agent.final_report_run_finish_omitted"
    FINAL_REPORT_INCOMPLETE = "agent.final_report_incomplete"
    WAITING_EXTERNAL_CONTINUATION_ATTEMPTED = "agent.waiting_external_continuation_attempted"
    BATCH_PROGRESS_REPORT_OMITTED = "agent.batch_progress_report_omitted"
    SPECIALIST_APPLY_STEP_OMITTED = "agent.specialist_apply_step_omitted"
    SPECIALIST_REWRITE_COUNT_MISMATCH = "agent.final_report_specialist_rewrite_count_mismatch"
    READY_CONTINUATION_STOPPED = "agent.ready_continuation_stopped"
    PROCESS_CHATS_PRIMARY_OBJECTIVE_UNRESOLVED = "agent.process_chats_primary_objective_unresolved"
    PROCESS_CHATS_VAULT_DELETION_WITHOUT_RECEIPT = "agent.process_chats_vault_deletion_without_receipt"
    TRANSCRIPT_UNREADABLE = "agent.transcript_unreadable"
    SUBAGENT_RAW_CONTENT_CONTRACT_VIOLATION = "agent.subagent_raw_content_contract_violation"
    PARENT_CANONICAL_ARTIFACT_WRITE_BEFORE_SUBAGENT = "agent.parent_canonical_artifact_write_before_subagent"
    PARENT_CANONICAL_ARTIFACT_WRITE_AFTER_SUBAGENT = "agent.parent_canonical_artifact_write_after_subagent"
    PARALLEL_HUMAN_DECISION_BACKLOG = "agent.parallel_human_decision_backlog"
    AGY_MATERIALIZED_SKILL_MISCLASSIFIED_AS_STALE = "agent.agy_materialized_skill_misclassified_as_stale"
    RECOVERABLE_TOOL_ERROR_OBSERVED = "agent.recoverable_tool_error_observed"
    EFFECT_PAYLOAD_CONTRACT_INVALID = "effect_payload_contract_invalid"


AgentRunReportSeverity = Literal["low", "medium", "high", "critical"]
AgentRunReportValidationStatus = Literal["completed", "blocked"]
FixWikiObjectiveStatus = Literal["yes", "partial", "no", "waiting_agent", "waiting_external", "failed", "unknown"]
FixWikiGraphStatus = Literal["improved", "clean", "blocked", "unchanged", "worse", "unknown"]
FixWikiRelatedNotesStatus = Literal["updated", "pending", "blocked", "skipped", "unknown"]
ProcessChatsObjectiveStatus = Literal[
    "no_pending",
    "preview_ready",
    "ready_to_publish",
    "published",
    "completed_with_link_blockers",
    "completed",
    "blocked",
    "failed",
    "unknown",
]
ProcessChatsNotesStatus = Literal["ready_to_publish", "published", "not_written", "blocked", "unknown"]
ProcessChatsRawStatus = Literal["covered", "processed", "partial", "not_processed", "unknown"]
ProcessChatsCoverageStatus = Literal["valid", "missing", "invalid", "not_applicable", "unknown"]
ProcessChatsLinkerStatus = Literal["applied", "blocked", "skipped", "not_run", "not_applicable", "unknown"]
StyleRewriteApplyStatus = Literal["applied", "completed", "blocked", "failed", "waiting_external"]


class StyleRewriteAtomicApplyItem(ContractModel):
    """Typed item evidence emitted by style-rewrite apply tools."""

    work_id: str = ""
    written: bool


class StyleRewriteAtomicApplyResult(ContractModel):
    """Tool-output contract consumed by final report validation."""

    schema_: Literal[
        "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1",
        "medical-notes-workbench.style-rewrite-atomic-apply-result.v1",
    ] = Field(alias="schema")
    status: StyleRewriteApplyStatus
    work_id: str = ""
    written_count: int = Field(default=0, ge=0)
    items: tuple[StyleRewriteAtomicApplyItem, ...] = ()


class FixWikiPrimaryObjectiveSummary(ContractModel):
    schema_: Literal["medical-notes-workbench.fix-wiki-primary-objective-summary.v1"] = Field(
        "medical-notes-workbench.fix-wiki-primary-objective-summary.v1",
        alias="schema",
    )
    wiki_fixed: FixWikiObjectiveStatus
    wiki_summary: str
    mutation_count: int = Field(ge=0)
    written_count: int = Field(ge=0)
    mutation_summary: str
    graph_status: FixWikiGraphStatus
    graph_summary: str
    related_notes_status: FixWikiRelatedNotesStatus
    related_notes_summary: str
    required_report_items: list[str] = Field(
        default_factory=lambda: [
            "wiki_fixed",
            "mutation_summary",
            "graph_summary",
            "related_notes_summary",
        ]
    )

    @field_validator("wiki_summary", "mutation_summary", "graph_summary", "related_notes_summary")
    @classmethod
    def _required_summary_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class ProcessChatsPrimaryObjectiveSummary(ContractModel):
    schema_: Literal["medical-notes-workbench.process-chats-primary-objective-summary.v1"] = Field(
        "medical-notes-workbench.process-chats-primary-objective-summary.v1",
        alias="schema",
    )
    process_status: ProcessChatsObjectiveStatus
    process_summary: str
    notes_status: ProcessChatsNotesStatus
    note_count: int = Field(ge=0, strict=True)
    wiki_write_summary: str
    raw_status: ProcessChatsRawStatus
    raw_count: int = Field(ge=0, strict=True)
    raw_summary: str
    coverage_status: ProcessChatsCoverageStatus
    coverage_summary: str
    linker_status: ProcessChatsLinkerStatus
    linker_summary: str
    required_report_items: list[str] = Field(
        default_factory=lambda: [
            "process_status",
            "raw_summary",
            "wiki_write_summary",
            "coverage_summary",
            "linker_summary",
        ]
    )

    @field_validator(
        "process_summary",
        "wiki_write_summary",
        "raw_summary",
        "coverage_summary",
        "linker_summary",
    )
    @classmethod
    def _required_summary_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class AgentRunReportFinding(ContractModel):
    schema_: Literal["medical-notes-workbench.agent-run-report-finding.v1"] = Field(
        "medical-notes-workbench.agent-run-report-finding.v1",
        alias="schema",
    )
    code: AgentRunReportFindingCode
    severity: AgentRunReportSeverity
    source: str
    message: str
    next_action: str
    source_field: str = ""
    expected: str = ""
    actual: str = ""
    path: str = ""
    artifact_name: str = ""
    tool_error_type: str = ""
    evidence: JsonObject = Field(default_factory=dict)

    @field_validator("source", "message", "next_action")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class AgentRunReportValidation(ContractModel):
    schema_: Literal["medical-notes-workbench.agent-run-report-validation.v1"] = Field(
        "medical-notes-workbench.agent-run-report-validation.v1",
        alias="schema",
    )
    phase: Literal["validate_agent_run_report"] = "validate_agent_run_report"
    status: AgentRunReportValidationStatus
    workflow: str = ""
    run_id: str = ""
    workflow_status: str = ""
    workflow_phase: str = ""
    receipt_status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    final_report_present: bool = False
    transcript_present: bool = False
    workflow_payload_path: str = ""
    transcript_path: str = ""
    final_report_path: str = ""
    primary_objective: (
        FixWikiPrimaryObjectiveSummary | ProcessChatsPrimaryObjectiveSummary | WorkflowPrimaryObjectiveSummary | None
    ) = None
    happy_path_metrics: HappyPathRunMetrics | None = None
    public_report_view_model: WorkflowPublicReportViewModel | None = None
    transcript_audit: WorkflowTranscriptAuditResult | None = None
    finding_count: int = Field(ge=0)
    findings: list[AgentRunReportFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_report_contract(self) -> AgentRunReportValidation:
        if self.finding_count != len(self.findings):
            raise ValueError("finding_count must match findings length")
        if self.findings and self.status != "blocked":
            raise ValueError("findings require blocked status")
        if self.status == "blocked" and not self.next_action.strip():
            raise ValueError("blocked validation requires next_action")
        return self
