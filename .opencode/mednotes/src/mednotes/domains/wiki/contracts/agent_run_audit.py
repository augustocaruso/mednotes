from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from mednotes.kernel.base import ContractModel, JsonObject

AuditWorkflow = Literal["process-chats", "fix-wiki", "link", "unknown"]
AuditStatus = Literal["clean", "findings", "blocked"]
AuditSeverity = Literal["info", "warning", "contract_violation", "blocking_candidate"]
AuditConfidence = Literal["low", "medium", "high"]
RecommendedAction = Literal["ignore", "document", "prompt_hardening", "test_fixture", "runtime_guardrail"]
CanonicalArtifactKind = Literal[
    "triage_note_plan",
    "raw_coverage",
    "manifest",
    "receipt",
    "style_rewrite_output",
    "human_decision_backlog",
    "unknown",
]


class AgentTranscriptSource(ContractModel):
    schema_: Literal["medical-notes-workbench.agent-transcript-source.v1"] = Field(
        "medical-notes-workbench.agent-transcript-source.v1",
        alias="schema",
    )
    path_label: StrictStr
    source_kind: Literal["transcript", "workflow_payload", "final_report", "runtime_log"]
    present: StrictBool
    format: Literal["json", "jsonl", "text", "missing", "unknown"]


class ToolCallObservation(ContractModel):
    """Normalized tool-call fact extracted from a harness transcript.

    The audit layer stores facts only: runtime shape, tool name, status, target
    and whether the tool output exposed an executable FSM directive. Policy is
    decided later by the audit rules, not while parsing raw JSON.
    """

    schema_: Literal["medical-notes-workbench.tool-call-observation.v1"] = Field(
        "medical-notes-workbench.tool-call-observation.v1",
        alias="schema",
    )
    index: StrictInt = Field(ge=0)
    tool_name: StrictStr = Field(min_length=1)
    status: StrictStr = ""
    command_text: StrictStr = ""
    target_path: StrictStr = ""
    output_excerpt: StrictStr = ""
    agent_effect_pending_signal: StrictBool = False
    target_is_agy_config_skill: StrictBool = False
    stale_materialized_skill_signal: StrictBool = False
    parameter_keys: list[StrictStr] = Field(default_factory=list)


class SubagentInvocationObservation(ContractModel):
    schema_: Literal["medical-notes-workbench.subagent-invocation-observation.v1"] = Field(
        "medical-notes-workbench.subagent-invocation-observation.v1",
        alias="schema",
    )
    index: StrictInt = Field(ge=0)
    tool_name: StrictStr = Field(min_length=1)
    agent_name: StrictStr = ""
    prompt_length: StrictInt = Field(ge=0)
    has_work_item: StrictBool = False
    has_raw_content_markers: StrictBool = False


class CanonicalArtifactObservation(ContractModel):
    schema_: Literal["medical-notes-workbench.canonical-artifact-observation.v1"] = Field(
        "medical-notes-workbench.canonical-artifact-observation.v1",
        alias="schema",
    )
    index: StrictInt = Field(ge=0)
    tool_name: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)
    artifact_kind: CanonicalArtifactKind
    after_subagent: StrictBool = False


class WorkflowDeviationFinding(ContractModel):
    schema_: Literal["medical-notes-workbench.workflow-deviation-finding.v1"] = Field(
        "medical-notes-workbench.workflow-deviation-finding.v1",
        alias="schema",
    )
    code: StrictStr = Field(min_length=1)
    workflow: AuditWorkflow
    severity: AuditSeverity
    confidence: AuditConfidence
    evidence_ref: StrictStr = Field(min_length=1)
    expected_contract: StrictStr = Field(min_length=1)
    observed_behavior: StrictStr = Field(min_length=1)
    recommended_action: RecommendedAction
    promotion_gate: StrictStr = Field(min_length=1)


class WorkflowTranscriptAuditSummary(ContractModel):
    schema_: Literal["medical-notes-workbench.workflow-transcript-audit-summary.v1"] = Field(
        "medical-notes-workbench.workflow-transcript-audit-summary.v1",
        alias="schema",
    )
    finding_count: StrictInt = Field(ge=0)
    blocking_candidate_count: StrictInt = Field(ge=0)
    contract_violation_count: StrictInt = Field(ge=0)
    warning_count: StrictInt = Field(ge=0)
    info_count: StrictInt = Field(ge=0)
    highest_severity: AuditSeverity
    recommended_next_action: StrictStr = Field(min_length=1)


class HardeningRecommendation(ContractModel):
    schema_: Literal["medical-notes-workbench.hardening-recommendation.v1"] = Field(
        "medical-notes-workbench.hardening-recommendation.v1",
        alias="schema",
    )
    action: RecommendedAction
    code: StrictStr = Field(min_length=1)
    rationale: StrictStr = Field(min_length=1)
    target: StrictStr = Field(min_length=1)


class AgentTranscriptAuditInput(ContractModel):
    schema_: Literal["medical-notes-workbench.agent-transcript-audit-input.v1"] = Field(
        "medical-notes-workbench.agent-transcript-audit-input.v1",
        alias="schema",
    )
    workflow: AuditWorkflow = "unknown"
    transcript_path: StrictStr = ""
    workflow_payload_path: StrictStr = ""
    final_report_path: StrictStr = ""
    runtime_log_paths: list[StrictStr] = Field(default_factory=list)


class WorkflowTranscriptAuditResult(ContractModel):
    schema_: Literal["medical-notes-workbench.workflow-transcript-audit.v1"] = Field(
        "medical-notes-workbench.workflow-transcript-audit.v1",
        alias="schema",
    )
    status: AuditStatus
    workflow: AuditWorkflow
    transcript_present: StrictBool
    workflow_payload_present: StrictBool
    final_report_present: StrictBool
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    finding_count: StrictInt = Field(ge=0)
    summary: WorkflowTranscriptAuditSummary
    findings: list[WorkflowDeviationFinding] = Field(default_factory=list)
    hardening_recommendations: list[HardeningRecommendation] = Field(default_factory=list)
    behavior_case_candidates: list[JsonObject] = Field(default_factory=list)
    sources: list[AgentTranscriptSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_counts(self) -> WorkflowTranscriptAuditResult:
        finding_count = len(self.findings)
        if self.finding_count != finding_count or self.summary.finding_count != finding_count:
            raise ValueError("finding_count must match findings length")
        blocking_candidate_count = sum(1 for item in self.findings if item.severity == "blocking_candidate")
        contract_violation_count = sum(1 for item in self.findings if item.severity == "contract_violation")
        warning_count = sum(1 for item in self.findings if item.severity == "warning")
        info_count = sum(1 for item in self.findings if item.severity == "info")
        if (
            self.summary.blocking_candidate_count != blocking_candidate_count
            or self.summary.contract_violation_count != contract_violation_count
            or self.summary.warning_count != warning_count
            or self.summary.info_count != info_count
        ):
            raise ValueError("summary severity counts must match findings")
        if self.summary.highest_severity != _highest_severity(self.findings):
            raise ValueError("summary highest_severity must match findings")
        if self.workflow != "unknown" and any(item.workflow != self.workflow for item in self.findings):
            raise ValueError("finding workflow must match audit workflow")
        if self.status == "clean" and self.findings:
            raise ValueError("clean audit cannot include findings")
        has_blocking_candidate = any(item.severity == "blocking_candidate" for item in self.findings)
        if self.status == "findings" and not self.findings:
            raise ValueError("findings audit requires at least one finding")
        if self.status == "findings" and has_blocking_candidate:
            raise ValueError("blocking_candidate finding requires blocked status")
        if self.status == "blocked" and not has_blocking_candidate:
            raise ValueError("blocked audit requires a blocking_candidate finding")
        return self


def _highest_severity(findings: list[WorkflowDeviationFinding]) -> AuditSeverity:
    if any(item.severity == "blocking_candidate" for item in findings):
        return "blocking_candidate"
    if any(item.severity == "contract_violation" for item in findings):
        return "contract_violation"
    if any(item.severity == "warning" for item in findings):
        return "warning"
    return "info"
