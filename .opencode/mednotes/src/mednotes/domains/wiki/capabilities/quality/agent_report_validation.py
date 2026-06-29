from __future__ import annotations

import json
import re
import shlex
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import cast
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from mednotes.domains.wiki.capabilities.quality.agent_run_audit import audit_agent_transcript
from mednotes.domains.wiki.common import SKILLS_RELPATH
from mednotes.domains.wiki.contracts.agent_report import (
    AgentRunReportFinding,
    AgentRunReportFindingCode,
    AgentRunReportSeverity,
    AgentRunReportValidation,
    FixWikiPrimaryObjectiveSummary,
    ProcessChatsPrimaryObjectiveSummary,
    StyleRewriteAtomicApplyResult,
)
from mednotes.domains.wiki.contracts.agent_run_audit import (
    AuditWorkflow,
    WorkflowDeviationFinding,
    WorkflowTranscriptAuditResult,
)
from mednotes.domains.wiki.contracts.happy_path import happy_path_metrics_from_findings
from mednotes.domains.wiki.contracts.public_report import WorkflowPublicObjectiveAnswer, WorkflowPublicReportViewModel
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_primary_objective import fix_wiki_primary_objective_summary
from mednotes.domains.wiki.flows.process_chats.process_chats_primary_objective import (
    process_chats_primary_objective_summary,
)
from mednotes.kernel.agent_directive import AgentDirective, AgentEffect
from mednotes.kernel.base import JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffectKind
from mednotes.kernel.public_report import WorkflowPrimaryObjectiveSummary, WorkflowPublicReport
from mednotes.platform.feedback.operational_contract import (
    PUBLIC_TOOL_TEXT_CONTRACT_VIOLATION,
    TOOL_CALL_ERROR,
    validate_agent_tool_calls,
)

NON_SUCCESS_STATUSES = {
    "blocked",
    "failed",
    "waiting_agent",
    "waiting_external",
    "waiting_human",
    "completed_with_link_blockers",
}
FSM_FIRST_SCHEMAS = {
    "medical-notes-workbench.fix-wiki-fsm-result.v1",
    "medical-notes-workbench.flashcards-fsm-result.v1",
    "medical-notes-workbench.link-fsm-result.v1",
    "medical-notes-workbench.link-related-fsm-result.v1",
    "medical-notes-workbench.process-chats-fsm-result.v1",
    "medical-notes-workbench.setup-fsm-result.v1",
    "medical-notes-workbench.history-fsm-result.v1",
}
PrimaryObjectiveSummary = (
    FixWikiPrimaryObjectiveSummary | ProcessChatsPrimaryObjectiveSummary | WorkflowPrimaryObjectiveSummary
)
STYLE_REWRITE_APPLY_RESULT_SCHEMAS = {
    "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1",
    "medical-notes-workbench.style-rewrite-atomic-apply-result.v1",
}
KNOWN_WORKFLOW_STATUSES = NON_SUCCESS_STATUSES | {
    "no_pending",
    "completed",
    "completed_with_warnings",
    "ready_to_publish",
    "published",
    "preview_ready",
    "ready",
    "running",
}
TRANSCRIPT_AUDIT_FINDING_CODE_MAP: dict[str, AgentRunReportFindingCode] = {
    "agent.transcript_unreadable": AgentRunReportFindingCode.TRANSCRIPT_UNREADABLE,
    "agent.subagent_raw_content_contract_violation": (
        AgentRunReportFindingCode.SUBAGENT_RAW_CONTENT_CONTRACT_VIOLATION
    ),
    "agent.parent_canonical_artifact_write_before_subagent": (
        AgentRunReportFindingCode.PARENT_CANONICAL_ARTIFACT_WRITE_BEFORE_SUBAGENT
    ),
    "agent.parent_canonical_artifact_write_after_subagent": (
        AgentRunReportFindingCode.PARENT_CANONICAL_ARTIFACT_WRITE_AFTER_SUBAGENT
    ),
    "agent.parallel_human_decision_backlog": AgentRunReportFindingCode.PARALLEL_HUMAN_DECISION_BACKLOG,
    "agent.agy_materialized_skill_misclassified_as_stale": (
        AgentRunReportFindingCode.AGY_MATERIALIZED_SKILL_MISCLASSIFIED_AS_STALE
    ),
    "agent.recoverable_tool_error_observed": AgentRunReportFindingCode.RECOVERABLE_TOOL_ERROR_OBSERVED,
}
GLOBAL_SUCCESS_CONTEXT_MARKERS = {
    "workflow",
    "fluxo",
    "wiki",
    "publicacao",
    "publicou",
    "publicad",
    "conclu",
    "pronto",
    "completo",
    "final",
}
SCOPED_SUCCESS_CONTEXT_MARKERS = {
    "reparos deterministic",
    "reparo deterministic",
    "reparos iniciais",
    "reparo inicial",
    "reparos automatic",
    "reparo automatic",
    "etapa deterministic",
    "related notes",
    "notas relacionadas",
    "grafo",
    "body links",
    "links corporais",
}
SUCCESS_CLAIM_RE = re.compile(
    r"\b("
    r"sucesso|conclu[ií]do|concluiu|completed|success|sem\s+desvios|sem\s+problemas|pronto"
    r")\b",
    re.IGNORECASE,
)
UNSUPPORTED_BLOCKER_CLAIM_RE = re.compile(
    r"\b("
    r"bloquead\w*|blocked|pausad\w*|interrompid\w*|bloqueio\s+preventivo|"
    r"duplicidade|duplicate|collision|colis[aã]o"
    r")\b",
    re.IGNORECASE,
)
NO_TOOL_DEVIATION_CLAIM_RE = re.compile(
    r"(desvios?\s+do\s+happy\s+path\s*:\s*nenhum|nenhum\s+desvio|sem\s+desvios?|"
    r"n[aã]o\s+houve\s+desvios?|houve\s+desvios?[^?]{0,100}\?\s*n[aã]o\s+houve|"
    r"n[aã]o\s+foram\s+executados\s+probes?|no\s+deviations?|no\s+probes?)",
    re.IGNORECASE,
)
SPECIALIST_REWRITE_COUNT_CLAIM_RE = re.compile(
    r"\b(?P<count>\d+)\s+"
    r"(?:nota(?:\(s\))?s?|arquivo(?:\(s\))?s?)"
    r"[^.!?\n]{0,80}\b(?:reescrit|rewrite)",
    re.IGNORECASE,
)
RUNTIME_CONTINUATION_UNAVAILABLE_RE = re.compile(
    r"(runtime\s+headless|headless|cli)[^.!?\n]{0,160}"
    r"(n[aã]o\s+possui|sem|lacks?|unavailable|indispon[ií]vel)[^.!?\n]{0,160}"
    r"(invoke_agent|ferramenta|tool|subagente|subagent|med-knowledge-architect)",
    re.IGNORECASE,
)
STATUS_VALUE_RE = re.compile(r"\b[a-z][a-z0-9_]*\b")
NEGATED_SUCCESS_PREFIX_RE = re.compile(r"\b(n[aã]o|not|never|sem)\b[\w\s]{0,24}$", re.IGNORECASE)
NEGATED_SUCCESS_SENTENCE_RE = re.compile(
    r"\b(n[aã]o|not|never|sem)\b[^.!?\n]{0,160}"
    r"\b(sucesso|success|conclu[ií]do|concluiu|completed|pronto|completo)\b",
    re.IGNORECASE,
)
SCOPED_SUCCESS_WITH_GLOBAL_BLOCKER_RE = re.compile(
    r"\b(mas|por[eé]m|contudo)\b[^.!?\n]{0,180}"
    r"\b(workflow|fluxo|wiki)\b[^.!?\n]{0,180}"
    r"\b(terminou|ficou|permanece|aguarda|bloque\w*|interromp\w*|waiting_agent|waiting_external|pendente)\b",
    re.IGNORECASE,
)
BACKTICK_ABSOLUTE_PATH_RE = re.compile(r"`(?P<path>/[^`]+)`")
FILE_URI_RE = re.compile(r"file://(?P<path>/[^\s\]`>\"']+)")
PLAIN_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])(?P<path>/(?:Users|tmp|private/tmp)/[^\s)\]`>\"']+)")
TOOL_CONTENT_FILE_PATH_RE = re.compile(r"File Path:\s*`file://(?P<path>[^`]+)`")
PUBLIC_OUTPUT_FORBIDDEN_TERMS = (
    "uv run",
    "--apply",
    "wiki/cli.py",
    "--json",
    "--dry-run",
    "blocked_reason",
    "receipt",
    "recibo",
    "schema",
    "hash",
    "fix-wiki --apply",
    "finalize-agy-specialist-task",
    "run-linker",
    "resource_guard_active",
    "compact-report",
    "full-report",
    "workflow_exit_code",
    "código de saída",
    "codigo de saida",
    "código de retorno",
    "codigo de retorno",
    "exit code",
    "returncode",
    "background task",
    "agy background fallback",
    "harness externo",
    "versionamento",
    "workflow",
    "linker",
    "atestação",
    "atestacao",
    "homologado",
    "logs",
    "progress_view_model",
    "process_chats_terminal_state",
    "specialist_model_quota_exhausted",
    "specialist_model_capacity_unavailable",
    "guard_lease_mismatch",
    "run_id",
    "i am waiting",
    "you will be notified",
    "waiting for completion",
    "no_pending",
)
TRANSCRIPT_CHILD_CONTAINER_KEYS = (
    "$set",
    "content",
    "events",
    "items",
    "messages",
    "records",
    "response",
    "responses",
    "result",
    "toolCalls",
    "tool_calls",
    "transcript",
)
CPU_SAMPLE_SCHEMA = "medical-notes-workbench.controlled-experiment-cpu-sample.v1"
HIGH_CPU_PERCENT_THRESHOLD = 85.0
HIGH_CPU_MIN_SAMPLE_COUNT = 2
HIGH_CPU_MIN_SPAN_SECONDS = 10.0
AGY_SELECTED_MODEL_RE = re.compile(r'Propagating selected model override to backend:\s+label="(?P<label>[^"]+)"')
FLASH_MODEL_RE = re.compile(r"\bflash\b|gemini[-\s\d.]*flash", re.IGNORECASE)
PROCESS_CHATS_WIKI_DELETION_RE = re.compile(
    r"(?m)^\s*(?:D|deleted:)\s+(?P<path>.*(?:Wiki_Medicina|wiki)[^\n]*\.md)\s*$",
    re.IGNORECASE,
)
ROOT_CAUSE_PUBLIC_LABELS: dict[str, tuple[str, ...]] = {
    "environment_blocker.windows_path_or_venv": (
        "ambiente Python",
        "Acesso negado",
        "venv",
    ),
    "specialist_model_capacity_unavailable": (
        "cota",
        "quota",
        "capacidade",
        "modelo especialista",
    ),
    "specialist_model_quota_exhausted": (
        "cota",
        "quota",
        "capacidade",
        "modelo especialista",
    ),
    "vocabulary_curation_required": (
        "curadoria de vocabulário",
        "vocabulary curation",
        "vocabulário",
    ),
}
LEGITIMATE_SPECIALIST_STOP_REASONS = {
    "rewrite_output_validation_errors",
    "specialist_model_capacity_unavailable",
    "specialist_model_quota_exhausted",
    "style_rewrite_agent_contract_violation",
    "style_rewrite_output_missing",
    "style_rewrite_still_requires_rewrite",
    "target_hash_changed",
}
WAITING_AGENT_CONTINUATION_MARKERS = (
    "med-knowledge-architect",
    "finalize-agy-specialist-task",
    "finalize-opencode-specialist-task",
    "invoke_agent",
    "define_subagent",
    "invoke_subagent",
    "finalize-style-rewrite-output",
    "collect-style-rewrite-outputs",
    "apply-specialist-style-rewrite",
    "apply-style-rewrite",
)
NON_ERROR_DECISION_REASON_CODES = {
    "style_rewrite_ready",
}
NON_SUCCESS_HUMAN_STATUS_MARKERS: dict[str, tuple[str, ...]] = {
    "blocked": (
        "bloquead",
        "nao concluiu",
        "nao foi conclu",
        "nao fixou",
        "pendente",
    ),
    "failed": (
        "falhou",
        "erro",
        "nao concluiu",
        "nao foi conclu",
    ),
    "waiting_agent": (
        "aguard",
        "bloquead",
        "cota",
        "quota",
        "modelo especialista",
        "nao fixou",
        "nao foi fixada por completo",
        "parcial",
        "pendente",
        "reescrita especializada",
    ),
    "waiting_external": (
        "aguard",
        "bloquead",
        "cota",
        "quota",
        "capacidade",
        "modelo especialista",
        "nao fixou",
        "pendente",
        "sem capacidade",
    ),
    "waiting_human": (
        "decisao humana",
        "escolha humana",
        "confirmacao",
        "confirmar",
        "aguard",
        "pendente",
    ),
    "completed_with_link_blockers": (
        "link",
        "grafo",
        "bloquead",
        "pendente",
    ),
}


def validate_agent_run_report(
    *,
    workflow_payload: JsonObject,
    transcript: object | None = None,
    final_report_text: str | None = None,
    runtime_log_text: str | None = None,
    workflow_payload_path: Path | None = None,
    transcript_path: Path | None = None,
    final_report_path: Path | None = None,
    runtime_log_paths: list[Path] | None = None,
) -> AgentRunReportValidation:
    """Validate the agent's final report against the workflow's typed truth."""

    raw_payload = _json_object(workflow_payload)
    agent_directive_findings = _agent_directive_contract_findings(raw_payload)
    payload = _payload_with_safe_diagnostic_context(raw_payload)
    truth = _workflow_truth(payload)
    primary_objective = _workflow_primary_objective_summary(payload)
    final_text = _final_report_text(final_report_text=final_report_text, transcript=transcript)
    findings: list[AgentRunReportFinding] = list(agent_directive_findings)
    findings.extend(_legacy_specialist_route_findings(payload))
    final_report_present = bool(final_text)

    findings.extend(_public_output_findings(payload))
    findings.extend(_public_report_pending_effect_success_findings(payload))
    findings.extend(_stale_next_action_findings(payload))
    if primary_objective is None:
        findings.extend(_missing_fsm_primary_objective_findings(payload))
    if final_text:
        findings.extend(_final_report_permission_findings(payload, final_text))
        incomplete_findings = _final_report_incomplete_findings(final_text, truth)
        findings.extend(incomplete_findings)
        if incomplete_findings:
            final_report_present = False
        findings.extend(_final_report_internal_term_findings(final_text))
        findings.extend(_status_mismatch_findings(final_text, truth, primary_objective))
        findings.extend(_unsupported_blocker_claim_findings(final_text, truth))
        findings.extend(_success_claim_findings(final_text, truth))
        findings.extend(_omitted_status_findings(final_text, truth))
        findings.extend(_error_context_root_cause_findings(payload, final_text))
        findings.extend(_final_report_local_path_leak_findings(final_text))
        findings.extend(_invalid_reported_artifact_path_findings(final_text))
        findings.extend(_workflow_payload_omission_findings(payload, final_text, transcript))
        if primary_objective is not None:
            findings.extend(_primary_objective_payload_findings(payload, primary_objective))
            findings.extend(_primary_objective_success_claim_findings(final_text, primary_objective))
            findings.extend(_primary_objective_omission_findings(final_text, primary_objective))
    elif primary_objective is not None:
        findings.extend(_primary_objective_payload_findings(payload, primary_objective))
    findings.extend(_workflow_payload_consistency_findings(payload))
    findings.extend(_runtime_log_findings(payload, runtime_log_text or "", final_text, transcript))
    if transcript is not None:
        findings.extend(_tool_payload_contract_findings(transcript))
        findings.extend(_omitted_tool_error_findings(transcript, final_text))
        findings.extend(_omitted_tool_deviation_findings(transcript, final_text))
        findings.extend(_blocked_workflow_tool_result_findings(transcript, final_text))
        findings.extend(_update_topic_success_claim_findings(transcript, truth))
        findings.extend(_transcript_specialist_model_policy_findings(payload, transcript))
        findings.extend(_specialist_completed_apply_step_findings(transcript))
        findings.extend(_opencode_specialist_receipt_step_findings(payload, transcript))
        findings.extend(_style_rewrite_batch_progress_checkpoint_findings(payload, transcript))
        findings.extend(_specialist_rewrite_count_findings(transcript, final_text))
        findings.extend(
            _waiting_agent_continuation_findings(
                payload,
                transcript,
                final_text,
                runtime_log_text or "",
            )
        )
        findings.extend(
            _ready_continuation_stopped_findings(
                payload,
                transcript,
                final_text,
                runtime_log_text or "",
            )
        )
        findings.extend(_waiting_external_continuation_attempt_findings(payload, transcript))
    transcript_audit = _audit_agent_transcript_from_paths(
        truth=truth,
        workflow_payload_path=workflow_payload_path,
        transcript_path=transcript_path,
        final_report_path=final_report_path,
        runtime_log_paths=runtime_log_paths or [],
    )
    if transcript_audit is not None:
        findings.extend(_transcript_audit_findings(transcript_audit))

    status = "blocked" if findings else "completed"
    happy_path_metrics = happy_path_metrics_from_findings(
        workflow=truth.workflow or _optional_text(payload, "workflow"),
        run_id=truth.run_id or str(payload.get("run_id") or "unknown"),
        findings=findings,
        primary_objective_completed=_primary_objective_completed(primary_objective),
        legitimate_stop_reason=_legitimate_stop_reason(payload, primary_objective),
    )
    public_report_view_model = _public_report_view_model(payload, primary_objective)
    return AgentRunReportValidation(
        status=status,
        workflow=truth.workflow,
        run_id=truth.run_id,
        workflow_status=truth.workflow_status,
        workflow_phase=truth.workflow_phase,
        receipt_status=truth.receipt_status,
        blocked_reason="agent_final_report_contract_violation" if findings else "",
        next_action=(
            "Corrigir o relatório final do agente para refletir o payload oficial, reportar erros de tool "
            "e remover caminhos de artefatos inexistentes antes de concluir a rodada."
            if findings
            else ""
        ),
        final_report_present=final_report_present,
        transcript_present=transcript is not None or transcript_path is not None,
        workflow_payload_path=str(workflow_payload_path) if workflow_payload_path is not None else "",
        transcript_path=str(transcript_path) if transcript_path is not None else "",
        final_report_path=str(final_report_path) if final_report_path is not None else "",
        primary_objective=primary_objective,
        happy_path_metrics=happy_path_metrics,
        public_report_view_model=public_report_view_model,
        transcript_audit=transcript_audit,
        finding_count=len(findings),
        findings=findings,
    )


def _audit_agent_transcript_from_paths(
    *,
    truth: _WorkflowTruth,
    workflow_payload_path: Path | None,
    transcript_path: Path | None,
    final_report_path: Path | None,
    runtime_log_paths: list[Path],
) -> WorkflowTranscriptAuditResult | None:
    if transcript_path is None:
        return None
    return audit_agent_transcript(
        transcript_path=transcript_path,
        workflow=_audit_workflow(truth.workflow),
        workflow_payload_path=workflow_payload_path,
        final_report_path=final_report_path,
        runtime_log_paths=runtime_log_paths,
    )


def _audit_workflow(workflow: str) -> AuditWorkflow:
    normalized = workflow.strip().lower()
    if normalized.startswith("/"):
        normalized = normalized[1:]
    if normalized.startswith("mednotes:"):
        normalized = normalized.split(":", 1)[1]
    normalized = normalized.replace("_", "-")
    if normalized in {"process-chats", "fix-wiki", "link"}:
        return cast(AuditWorkflow, normalized)
    return "unknown"


def _transcript_audit_findings(
    transcript_audit: WorkflowTranscriptAuditResult,
) -> list[AgentRunReportFinding]:
    return [_transcript_audit_finding(audit_finding) for audit_finding in transcript_audit.findings]


def _transcript_audit_finding(audit_finding: WorkflowDeviationFinding) -> AgentRunReportFinding:
    next_action = audit_finding.promotion_gate or str(audit_finding.recommended_action)
    return AgentRunReportFinding(
        code=_agent_report_code_for_audit(audit_finding),
        severity=_agent_report_severity_for_audit(audit_finding),
        source="transcript_audit",
        source_field="transcript_audit.findings",
        expected=audit_finding.expected_contract,
        actual=audit_finding.observed_behavior,
        message=audit_finding.observed_behavior,
        next_action=next_action,
        evidence={
            "evidence_ref": audit_finding.evidence_ref,
            "recommended_action": audit_finding.recommended_action,
        },
    )


def _agent_report_code_for_audit(audit_finding: WorkflowDeviationFinding) -> AgentRunReportFindingCode:
    return TRANSCRIPT_AUDIT_FINDING_CODE_MAP.get(
        audit_finding.code,
        AgentRunReportFindingCode.WORKFLOW_CONTRACT_CONTRADICTION,
    )


def _agent_report_severity_for_audit(audit_finding: WorkflowDeviationFinding) -> AgentRunReportSeverity:
    if audit_finding.severity == "blocking_candidate":
        return "critical"
    return "high"


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value)


class _AgentReportFieldModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class _RuntimeCpuSample(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: StrictStr = Field(default="", alias="schema")
    elapsed_seconds: float = Field(default=0.0, ge=0)
    total_cpu_percent: float = Field(default=0.0, ge=0)
    max_cpu_percent: float = Field(default=0.0, ge=0)
    process_count: StrictInt = Field(default=0, ge=0)
    max_cpu_command: StrictStr = ""


class _SpecialistRuntimeBatchItem(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    work_id: StrictStr = ""
    agent: StrictStr = ""
    model_policy: StrictStr = ""
    required_model_tier: StrictStr = ""
    preferred_model_tier: StrictStr = ""


class _WorkflowTruthPayloadFields(_AgentReportFieldModel):
    workflow: StrictStr = ""
    run_id: StrictStr = ""
    status: StrictStr = ""
    phase: StrictStr = ""
    blocked_reason: StrictStr = ""


class _ProgressTruthFields(_AgentReportFieldModel):
    workflow: StrictStr = ""
    run_id: StrictStr = ""
    status: StrictStr = ""
    phase: StrictStr = ""
    can_continue_now: StrictBool | None = None


class _PublicProgressFields(_AgentReportFieldModel):
    user_action: StrictStr = ""


class _PublicReceiptFields(_AgentReportFieldModel):
    next_action: StrictStr = ""


class _HumanDecisionPacketFields(_AgentReportFieldModel):
    """Human-decision summary fields used only after payload shape validation."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    why_this_needs_you: StrictStr = ""
    question: StrictStr = ""
    evidence_summary: StrictStr = ""
    type: StrictStr = ""
    kind: StrictStr = ""


class _AgentDirectiveCapabilities(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    continue_: StrictBool = Field(False, alias="continue")
    final_report: StrictBool = False


class _AgentDirectiveEffect(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    kind: StrictStr = ""


class _AgentDirectiveControl(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    status: StrictStr = ""
    state: StrictStr = ""
    capabilities: _AgentDirectiveCapabilities = Field(default_factory=_AgentDirectiveCapabilities)
    effects: list[_AgentDirectiveEffect] = Field(default_factory=list)
    blockers: list[StrictStr] = Field(default_factory=list)
    resume: StrictStr = ""


class _ReceiptTruthFields(_AgentReportFieldModel):
    workflow: StrictStr = ""
    run_id: StrictStr = ""
    status: StrictStr = ""


class _StateMachineTruthFields(_AgentReportFieldModel):
    current_state: StrictStr = ""


class _AgentReportRelatedRecoveryFields(_AgentReportFieldModel):
    status: StrictStr = ""


class _AgentReportApplyFields(_AgentReportFieldModel):
    requested_apply: StrictBool | None = None


class _AgentReportOrchestrationPlanFields(_AgentReportFieldModel):
    status: StrictStr = ""
    automatic: StrictBool | None = None
    executable_now: StrictBool | None = None
    human_decision_required: StrictBool | None = None


class _AgentReportVersionControlSafetyFields(_AgentReportFieldModel):
    mutation_without_guard: StrictBool | None = None
    resource_guard_active: StrictBool | None = None
    run_finish_seen: StrictBool | None = None
    sync_status: StrictStr = ""
    agent_instruction: StrictStr = ""


class _ProcessChatsTerminalFields(_AgentReportFieldModel):
    workflow: StrictStr = ""
    status: StrictStr = ""
    phase: StrictStr = ""
    process_chats_terminal_state: StrictStr = ""
    process_chats_backlog_state: StrictStr = ""
    item_count: StrictInt | None = None
    total_available_count: StrictInt | None = None


class _AgentReportHeadlessExportFields(_AgentReportFieldModel):
    embedded_count: StrictInt | None = None


class _AgentReportReportContractFields(_AgentReportFieldModel):
    must_include: list[StrictStr] = Field(default_factory=list)
    after_each_batch: StrictBool = False


class _SpecialistRuntimeBatch(_AgentReportFieldModel):
    """Executable specialist batch projected from agent_directive effects."""

    phase: StrictStr = ""
    current_batch_items: list[_SpecialistRuntimeBatchItem] = Field(default_factory=list)
    report_contract: _AgentReportReportContractFields = Field(default_factory=_AgentReportReportContractFields)


class _TranscriptEventFields(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    event_type: StrictStr = Field(default="", alias="type")
    tool_name: StrictStr = ""
    role: StrictStr = ""
    status: StrictStr = ""
    output: StrictStr = ""
    parameters: JsonObject = Field(default_factory=dict)
    content: object = ""


class _TranscriptTextParameters(_AgentReportFieldModel):
    """Text parameters that can influence transcript-derived decisions."""

    command: StrictStr = ""
    role: StrictStr = ""


class _OpenCodeSpecialistTaskMetadataFields(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: StrictStr = Field(default="", alias="schema")
    work_id: StrictStr = ""
    task_id: StrictStr = ""
    provider_id: StrictStr = ""
    model_id: StrictStr = ""
    model_tier: StrictStr = ""
    tool_sequence: list[StrictStr] = Field(default_factory=list)
    prompt_contract: StrictStr = ""
    raw_content_embedded: StrictBool | None = None


class _SpecialistTaskRunnerResultFields(_AgentReportFieldModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: StrictStr = Field(default="", alias="schema")
    status: StrictStr = ""
    work_id: StrictStr = ""
    next_apply_step: JsonObject | None = None


class _BlockedWorkflowToolResult(_AgentReportFieldModel):
    tool_name: StrictStr = ""
    status: StrictStr = ""
    phase: StrictStr = ""
    blocked_reason: StrictStr
    work_id: StrictStr = ""


def _field_payload(source: JsonObject, field_names: tuple[str, ...]) -> JsonObject:
    payload: JsonObject = {}
    for field_name in field_names:
        if field_name in source:
            payload[field_name] = source[field_name]
    return payload


def _object_field(source: JsonObject, field_name: str) -> JsonObject:
    if field_name not in source or source[field_name] is None:
        return {}
    value = source[field_name]
    if not isinstance(value, dict):
        if field_name == "diagnostic_context":
            return {}
        raise ValueError(f"{field_name} must be an object")
    return _json_object(value)


def _list_field(source: JsonObject, field_name: str) -> list[object]:
    value = source.get(field_name)
    if not isinstance(value, list):
        return []
    return list(value)


def _is_fsm_first_payload(payload: JsonObject) -> bool:
    return _optional_text(payload, "schema") in FSM_FIRST_SCHEMAS


def _payload_with_safe_diagnostic_context(payload: JsonObject) -> JsonObject:
    if isinstance(payload.get("diagnostic_context"), dict):
        return payload
    return {**payload, "diagnostic_context": {}}


def _agent_directive_from_payload(payload: JsonObject) -> tuple[AgentDirective | None, str]:
    if "agent_directive" not in payload:
        return None, "missing"
    directive_payload = payload["agent_directive"]
    if not isinstance(directive_payload, dict):
        return None, "agent_directive_not_object"
    try:
        return AgentDirective.model_validate(directive_payload), ""
    except ValidationError as exc:
        first_error = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first_error.get("loc", ())) or "agent_directive"
        message = str(first_error.get("msg") or "invalid")
        return None, f"{location}: {message}"


def _agent_directive_contract_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    if not _is_fsm_first_payload(payload):
        return []
    directive, directive_error = _agent_directive_from_payload(payload)
    if directive is not None:
        return []
    return [_agent_directive_invalid_finding(payload, directive_error)]


def _agent_directive_control(payload: JsonObject) -> _AgentDirectiveControl:
    directive, _error = _agent_directive_from_payload(payload)
    if directive is None:
        return _AgentDirectiveControl()
    control = directive.control.to_payload()
    return _AgentDirectiveControl.model_validate(
        _field_payload(
            control,
            ("status", "state", "capabilities", "effects", "blockers", "resume"),
        )
    )


def _specialist_runtime_batch_from_agent_directive(payload: JsonObject) -> _SpecialistRuntimeBatch:
    """Read executable specialist work only from the root agent directive."""

    directive, _directive_error = _agent_directive_from_payload(payload)
    if directive is None:
        return _SpecialistRuntimeBatch()
    batch_items: list[_SpecialistRuntimeBatchItem] = []
    report_contract = _AgentReportReportContractFields()
    for effect in directive.control.effects:
        effect_payload = effect.payload
        if effect.kind != WorkflowEffectKind.CALL_SPECIALIST_MODEL:
            continue
        if not _is_style_rewrite_specialist_effect(effect, effect_payload):
            continue
        batch_items.extend(
            _SpecialistRuntimeBatchItem.model_validate(item)
            for item in _list_field(effect_payload, "current_batch_items")
            if isinstance(item, dict)
        )
        candidate_report_contract = _object_field(effect_payload, "report_contract")
        if candidate_report_contract:
            report_contract = _AgentReportReportContractFields.model_validate(
                _field_payload(candidate_report_contract, ("must_include", "after_each_batch"))
            )
    return _SpecialistRuntimeBatch(
        phase="style_rewrite" if batch_items else "",
        current_batch_items=batch_items,
        report_contract=report_contract,
    )


def _is_style_rewrite_specialist_effect(effect: AgentEffect, effect_payload: JsonObject) -> bool:
    """Identify fix-wiki style-rewrite work without consulting diagnostics."""

    return (
        str(effect_payload.get("kind") or "") == "style_rewrite"
        or effect.target == "med-knowledge-architect"
        or bool(_list_field(effect_payload, "current_batch_items"))
    )


def _legacy_specialist_route_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    """Reject old diagnostic-only specialist batches as a contract violation."""

    diagnostic = _object_field(payload, "diagnostic_context")
    legacy_plan = _object_field(diagnostic, "orchestration" + "_plan")
    if not _list_field(legacy_plan, "current_batch_items"):
        return []
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    if batch.current_batch_items:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.WORKFLOW_AGENT_DIRECTIVE_INVALID,
            severity="critical",
            source="workflow_payload",
            source_field="diagnostic_context legacy specialist batch",
            expected="agent_directive.control.effects[].payload.current_batch_items",
            actual="specialist batch exposed only as diagnostic evidence",
            message="O payload tentou expor trabalho especialista executavel fora do agent_directive root.",
            next_action=(
                "Reemitir o payload FSM com agent_directive.control.effects[] e manter diagnostic_context "
                "apenas como evidencia."
            ),
        )
    ]


class _WorkflowTruth:
    def __init__(
        self,
        *,
        workflow: str,
        run_id: str,
        workflow_status: str,
        workflow_phase: str,
        progress_status: str,
        receipt_status: str,
        blocked_reason: str,
    ) -> None:
        self.workflow = workflow
        self.run_id = run_id
        self.workflow_status = workflow_status
        self.workflow_phase = workflow_phase
        self.progress_status = progress_status
        self.receipt_status = receipt_status
        self.blocked_reason = blocked_reason


def _workflow_truth(payload: JsonObject) -> _WorkflowTruth:
    fsm_first = _is_fsm_first_payload(payload)
    root = _WorkflowTruthPayloadFields.model_validate(
        _field_payload(payload, ("workflow", "run_id", "status", "phase", "blocked_reason"))
    )
    progress = _ProgressTruthFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("workflow", "run_id", "status", "phase", "can_continue_now"))
    )
    receipt = _ReceiptTruthFields.model_validate(
        _field_payload(_object_field(payload, "receipt"), ("workflow", "run_id", "status"))
    )
    snapshot = _StateMachineTruthFields.model_validate(
        _field_payload(_object_field(payload, "state_machine_snapshot"), ("current_state",))
    )
    if fsm_first:
        return _WorkflowTruth(
            workflow=progress.workflow or receipt.workflow or root.workflow,
            run_id=progress.run_id or receipt.run_id or root.run_id,
            workflow_status=progress.status or receipt.status,
            workflow_phase=progress.phase or snapshot.current_state,
            progress_status=progress.status,
            receipt_status=receipt.status,
            blocked_reason="",
        )
    return _WorkflowTruth(
        workflow=root.workflow or progress.workflow or receipt.workflow,
        run_id=root.run_id or progress.run_id or receipt.run_id,
        workflow_status=root.status or progress.status or receipt.status,
        workflow_phase=root.phase or progress.phase or snapshot.current_state,
        progress_status=progress.status or root.status,
        receipt_status=receipt.status or root.status,
        blocked_reason=root.blocked_reason,
    )


def _final_report_text(*, final_report_text: str | None, transcript: object | None) -> str:
    if final_report_text is not None:
        return _strip_controlled_experiment_json_lines(final_report_text)
    if transcript is None:
        return ""
    responses: list[str] = []
    delta_parts: list[str] = []

    def flush_delta_parts() -> None:
        if not delta_parts:
            return
        responses.append("".join(delta_parts))
        delta_parts.clear()

    def append_response(text: str, *, delta: bool = False) -> None:
        if not text.strip():
            return
        if delta:
            delta_parts.append(text)
            return
        flush_delta_parts()
        responses.append(text)

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        event_type = str(value.get("type") or "").upper()
        if event_type in {"TOOL_USE", "TOOL_RESULT"}:
            flush_delta_parts()
        if event_type == "PLANNER_RESPONSE":
            for field in ("content", "text", "message", "response"):
                raw = value.get(field)
                if isinstance(raw, str) and raw.strip():
                    append_response(raw)
                    break
        if event_type in {"GEMINI", "MESSAGE"}:
            role = str(value.get("role") or "").lower()
            if event_type == "GEMINI" or role in {"assistant", "model"}:
                text = _transcript_message_text(value.get("content"))
                if text.strip():
                    append_response(text, delta=bool(value.get("delta")))
        for child in _transcript_child_containers(value):
            visit(child)

    visit(transcript)
    flush_delta_parts()
    return _strip_controlled_experiment_json_lines("\n\n".join(responses))


def _strip_controlled_experiment_json_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and (
            "medical-notes-workbench.controlled-experiment-cpu-summary.v1" in stripped
            or "medical-notes-workbench.controlled-experiment-output-truncated.v1" in stripped
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


def _transcript_message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_transcript_message_text(item) for item in value]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        for field in ("text", "content", "message"):
            text = _transcript_message_text(value.get(field))
            if text.strip():
                return text
        parts = value.get("parts")
        if isinstance(parts, list):
            return _transcript_message_text(parts)
    return ""


def _final_report_incomplete_findings(final_text: str, truth: _WorkflowTruth) -> list[AgentRunReportFinding]:
    if not _final_report_looks_like_progress_only(final_text):
        return []
    status = truth.workflow_status or truth.progress_status or truth.receipt_status or "unknown"
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.FINAL_REPORT_INCOMPLETE,
            severity="high",
            source="final_report",
            source_field="final_report_text",
            expected="relatorio final com status publico, resultado primario, mutacoes, pendencias e erros",
            actual="progress_only",
            message="A resposta capturada parece mensagem intermediaria, nao relatorio final do workflow.",
            next_action=(
                "Tratar a rodada como incompleta e exigir fechamento que diga se a Wiki foi corrigida, "
                "o que mudou, o estado do grafo/Related Notes e qualquer bloqueio ou erro de runtime."
            ),
            evidence={"workflow_status": status},
        )
    ]


def _final_report_permission_findings(payload: JsonObject, final_text: str) -> list[AgentRunReportFinding]:
    if not final_text.strip():
        return []
    directive, _directive_error = _agent_directive_from_payload(payload)
    if directive is None:
        return []
    control = directive.control
    if control.capabilities.final_report:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.FINAL_REPORT_NOT_ALLOWED,
            severity="high",
            source="workflow_payload",
            source_field="agent_directive.control.capabilities.final_report",
            expected=f"status={control.status} final_report=false",
            actual="final_report_present",
            message="A diretiva oficial ainda não autoriza relatório final para este estado do workflow.",
            next_action=(
                "Continuar pela rota oficial ou reportar o bloqueio real antes de emitir uma resposta final."
            ),
            evidence={"directive_status": control.status, "directive_state": control.state},
        )
    ]


def _agent_directive_invalid_finding(payload: JsonObject, directive_error: str) -> AgentRunReportFinding:
    progress = _ProgressTruthFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("status", "can_continue_now"))
    )
    return AgentRunReportFinding(
        code=AgentRunReportFindingCode.WORKFLOW_AGENT_DIRECTIVE_INVALID,
        severity="high",
        source="workflow_payload",
        source_field="agent_directive.control",
        expected="agent_directive valido com control tipado para payload FSM-first",
        actual=directive_error or "invalid",
        message="Payload FSM-first nao trouxe agent_directive.control valido no root.",
        next_action=(
            "Corrigir o produtor FSM para emitir agent_directive antes de validar ou aceitar relatorio final."
        ),
        evidence={
            "schema": _optional_text(payload, "schema"),
            "progress_status": progress.status,
            "can_continue_now": progress.can_continue_now,
        },
    )


def _final_report_looks_like_progress_only(final_text: str) -> bool:
    if len(final_text.strip()) > 600:
        return False
    folded = _fold_text(final_text)
    substance_markers = (
        "status:",
        "receipt status",
        "fixou a wiki",
        "wiki ficou",
        "nao fixou",
        "nao foi fixada",
        "mutacao",
        "arquivos",
        "grafo",
        "related notes",
        "notas relacionadas",
        "bloque",
        "pendente",
        "parcial",
        "cota",
        "quota",
        "erro",
        "falhou",
    )
    if _folded_contains_any(folded, substance_markers):
        return False
    lines = [line.strip() for line in final_text.splitlines() if line.strip()]
    if not lines:
        return False
    progress_markers = (
        "i have started",
        "i started",
        "started the",
        "waiting for",
        "waiting for completion",
        "waiting for the execution",
        "aguardando resultado",
        "aguardando o resultado",
        "aguardando conclusao",
        "aguardando a conclusao",
        "em andamento",
        "vou aguardar",
    )
    return all(_folded_contains_any(_fold_text(line), progress_markers) for line in lines)


def _status_mismatch_findings(
    final_text: str,
    truth: _WorkflowTruth,
    primary_objective: PrimaryObjectiveSummary | None,
) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    reported = _reported_status_fields(final_text)
    receipt_status = reported.get("receipt.status")
    if receipt_status and truth.receipt_status and receipt_status != truth.receipt_status:
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.RECEIPT_STATUS_MISMATCH,
                severity="high",
                source="final_report",
                source_field="receipt.status",
                expected=truth.receipt_status,
                actual=receipt_status,
                message=(
                    "O relatório final declarou um receipt.status diferente do recibo oficial do workflow."
                ),
                next_action="Reescrever o relatório usando receipt.status do payload oficial.",
            )
        )
    progress_status = reported.get("progress_view_model.status")
    if progress_status and truth.progress_status and progress_status != truth.progress_status:
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PROGRESS_STATUS_MISMATCH,
                severity="high",
                source="final_report",
                source_field="progress_view_model.status",
                expected=truth.progress_status,
                actual=progress_status,
                message=(
                    "O relatório final declarou um progress_view_model.status diferente do payload oficial."
                ),
                next_action="Reescrever o relatório usando progress_view_model.status como fonte canônica.",
            )
        )
    root_status = reported.get("status")
    expected_root_statuses = _acceptable_public_statuses(truth, primary_objective)
    if root_status and expected_root_statuses and root_status not in expected_root_statuses:
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PROGRESS_STATUS_MISMATCH,
                severity="medium",
                source="final_report",
                source_field="status",
                expected=", ".join(sorted(expected_root_statuses)),
                actual=root_status,
                message="O relatório final declarou um status diferente do status canônico do workflow.",
                next_action="Corrigir o status público do relatório final antes de concluir a rodada.",
            )
        )
    return findings


def _acceptable_public_statuses(
    truth: _WorkflowTruth,
    primary_objective: PrimaryObjectiveSummary | None,
) -> set[str]:
    """Statuses a public final report may name without contradicting the FSM."""
    statuses: set[str] = set()
    if truth.workflow_status:
        statuses.add(truth.workflow_status)
    if isinstance(primary_objective, ProcessChatsPrimaryObjectiveSummary):
        statuses.add(primary_objective.process_status)
    if isinstance(primary_objective, WorkflowPrimaryObjectiveSummary):
        statuses.add(primary_objective.status)
    return statuses


def _reported_status_fields(final_text: str) -> dict[str, str]:
    reported: dict[str, str] = {}
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "receipt.status",
            re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:receipt\s+status|receipt\.status)\s*[:=]\s*`?(?P<value>[a-z0-9_]+)`?"),
        ),
        (
            "progress_view_model.status",
            re.compile(
                r"(?im)^\s*(?:[-*]\s*)?(?:progress_view_model\.status|progress\s+status)\s*[:=]\s*`?(?P<value>[a-z0-9_]+)`?"
            ),
        ),
        (
            "status",
            re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:status)\s*[:=]\s*`?(?P<value>[a-z0-9_]+)`?"),
        ),
    )
    for field, pattern in patterns:
        match = pattern.search(final_text)
        if not match:
            continue
        value = _normalize_status(match.group("value"))
        if value and value in KNOWN_WORKFLOW_STATUSES:
            reported[field] = value
    return reported


def _normalize_status(value: str) -> str:
    match = STATUS_VALUE_RE.search(value.strip().lower())
    return match.group(0) if match else ""


def _success_claim_findings(final_text: str, truth: _WorkflowTruth) -> list[AgentRunReportFinding]:
    status = truth.workflow_status or truth.progress_status or truth.receipt_status
    if status not in NON_SUCCESS_STATUSES:
        return []
    if not _has_positive_success_claim(final_text):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.SUCCESS_CLAIM_MISMATCH,
            severity="medium",
            source="final_report",
            source_field="final_report_text",
            expected=status,
            actual="success_claim",
            message="O relatório final usou linguagem de sucesso para um workflow que não está concluído.",
            next_action="Trocar linguagem de sucesso por progresso parcial, bloqueio ou espera externa conforme o payload oficial.",
        )
    ]


def _public_report_pending_effect_success_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    """Ensure the human-facing report cannot outrank pending FSM effects."""

    control = _agent_directive_control(payload)
    if control.status != "waiting_agent" or control.capabilities.continue_ is not True:
        return []
    if not control.effects and not control.resume.strip():
        return []
    reports = _object_field(payload, "reports")
    findings: list[AgentRunReportFinding] = []
    public_sources = [("reports.summary", _optional_text(reports, "summary"))]
    if "public_report" in reports:
        public_report = WorkflowPublicReport.model_validate(reports["public_report"])
        public_sources.append(("reports.public_report.headline", public_report.headline))
        public_sources.extend(
            (f"reports.public_report.lines[{index}]", line) for index, line in enumerate(public_report.lines)
        )
    for source_field, text in public_sources:
        if not _has_positive_success_claim(text):
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.SUCCESS_CLAIM_MISMATCH,
                severity="medium",
                source="workflow_payload",
                source_field=source_field,
                expected="waiting_agent",
                actual="success_claim",
                message="O relatório público declarou sucesso enquanto a FSM ainda exige continuação por agente.",
                next_action=(
                    "Projetar reports.* a partir da transição FSM e manter linguagem de progresso parcial "
                    "até agent_directive.control.capabilities.final_report=true."
                ),
            )
        )
    return findings


def _unsupported_blocker_claim_findings(final_text: str, truth: _WorkflowTruth) -> list[AgentRunReportFinding]:
    status = truth.workflow_status or truth.progress_status or truth.receipt_status
    if status in NON_SUCCESS_STATUSES or truth.blocked_reason:
        return []
    for match in UNSUPPORTED_BLOCKER_CLAIM_RE.finditer(final_text):
        sentence = _fold_text(_sentence_containing_match(final_text, match.start(), match.end()))
        if "sem bloque" in sentence or "nao bloque" in sentence or "não bloque" in sentence:
            continue
        return [
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.WORKFLOW_CONTRACT_CONTRADICTION,
                severity="high",
                source="final_report",
                source_field="final_report_text",
                expected=status or "workflow sem blocked_reason",
                actual=sentence[:180],
                message="O relatório final declarou bloqueio/duplicidade que não existe no payload oficial.",
                next_action=(
                    "Usar somente status, blocked_reason e decision oficiais para declarar bloqueio; "
                    "se o agente suspeitar duplicidade, registrar como suspeita e seguir a próxima ação oficial."
                ),
            )
        ]
    return []


def _has_positive_success_claim(final_text: str) -> bool:
    for match in SUCCESS_CLAIM_RE.finditer(final_text):
        prefix = final_text[max(0, match.start() - 32) : match.start()]
        if NEGATED_SUCCESS_PREFIX_RE.search(prefix):
            continue
        sentence = _fold_text(_sentence_containing_match(final_text, match.start(), match.end()))
        if NEGATED_SUCCESS_SENTENCE_RE.search(sentence):
            continue
        if _is_partial_success_sentence(sentence):
            continue
        return True
    return False


def _is_partial_success_sentence(sentence: str) -> bool:
    if "sem pendenc" in sentence or "sem blocker" in sentence or "sem bloque" in sentence:
        return False
    if not any(marker in sentence for marker in ("publicacao", "publicou", "publicad")):
        return False
    return any(marker in sentence for marker in ("pendenc", "pendente", "blocker", "bloque", "parcial"))


def _is_component_success_sentence(sentence: str) -> bool:
    if any(marker in sentence for marker in ("wiki", "workflow", "fluxo")):
        return False
    return any(marker in sentence for marker in SCOPED_SUCCESS_CONTEXT_MARKERS)


def _is_scoped_success_with_global_blocker(sentence: str) -> bool:
    return (
        any(marker in sentence for marker in SCOPED_SUCCESS_CONTEXT_MARKERS)
        and SCOPED_SUCCESS_WITH_GLOBAL_BLOCKER_RE.search(sentence) is not None
    )


def _sentence_containing_match(text: str, start: int, end: int) -> str:
    boundaries = "\n.!?"
    sentence_start = max(text.rfind(boundary, 0, start) for boundary in boundaries) + 1
    sentence_end_candidates = [
        index
        for boundary in boundaries
        if (index := text.find(boundary, end)) != -1
    ]
    sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
    return text[sentence_start:sentence_end]


def _omitted_status_findings(final_text: str, truth: _WorkflowTruth) -> list[AgentRunReportFinding]:
    status = truth.workflow_status or truth.progress_status or truth.receipt_status
    if status not in NON_SUCCESS_STATUSES:
        return []
    if status in final_text.lower() or _mentions_non_success_status_publicly(final_text, status):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.WORKFLOW_STATUS_OMITTED,
            severity="medium",
            source="final_report",
            source_field="progress_view_model.status",
            expected=status,
            actual="omitted",
            message="O relatório final não deixou claro que o workflow ainda não está concluído.",
            next_action=(
                "Explicar em linguagem pública que o workflow ficou parcial, bloqueado ou aguardando "
                "continuação; o identificador técnico é opcional."
            ),
        )
    ]


def _mentions_non_success_status_publicly(final_text: str, status: str) -> bool:
    markers = NON_SUCCESS_HUMAN_STATUS_MARKERS.get(status, ())
    if not markers:
        return False
    folded = _fold_text(final_text)
    return _folded_contains_any(folded, markers)


def _workflow_primary_objective_summary(
    payload: JsonObject,
) -> PrimaryObjectiveSummary | None:
    return (
        fix_wiki_primary_objective_summary(payload)
        or process_chats_primary_objective_summary(payload)
        or _generic_primary_objective_summary(payload)
    )


def _generic_primary_objective_summary(payload: JsonObject) -> WorkflowPrimaryObjectiveSummary | None:
    reports = _object_field(payload, "reports")
    details = _object_field(reports, "details")
    if "primary_objective_summary" not in details:
        return None
    summary = details["primary_objective_summary"]
    if not isinstance(summary, dict):
        raise ValueError("reports.details.primary_objective_summary must be an object")
    return WorkflowPrimaryObjectiveSummary.model_validate(summary)


def _primary_objective_completed(
    objective: PrimaryObjectiveSummary | None,
) -> bool:
    if objective is None:
        return False
    if isinstance(objective, FixWikiPrimaryObjectiveSummary):
        return objective.wiki_fixed == "yes"
    if isinstance(objective, WorkflowPrimaryObjectiveSummary):
        return objective.completed
    return objective.process_status in {
        "no_pending",
        "preview_ready",
        "ready_to_publish",
        "published",
        "completed_with_link_blockers",
        "completed",
    }


def _legitimate_stop_reason(
    payload: JsonObject,
    objective: PrimaryObjectiveSummary | None,
) -> str:
    progress = _object_field(payload, "progress_view_model")
    status = _optional_text(progress, "status") or _optional_text(payload, "status")
    if status == "waiting_external" and _payload_has_external_wait_evidence(payload):
        return "waiting_external"
    if status == "waiting_human" and _human_decision_packet(payload) is not None:
        return "waiting_human"
    if isinstance(objective, FixWikiPrimaryObjectiveSummary) and objective.wiki_fixed == "waiting_external":
        return "waiting_external"
    if isinstance(objective, WorkflowPrimaryObjectiveSummary):
        if objective.status == "waiting_external" or "waiting_external" in objective.status:
            return "waiting_external"
        if objective.status == "waiting_human" or "waiting_human" in objective.status:
            return "waiting_human"
    return ""


def _payload_has_external_wait_evidence(payload: JsonObject) -> bool:
    folded = _fold_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return any(
        marker in folded
        for marker in (
            "quota",
            "cota",
            "capacity",
            "capacidade",
            "waiting_external",
            "external_wait",
        )
    )


def _public_report_view_model(
    payload: JsonObject,
    objective: PrimaryObjectiveSummary | None,
) -> WorkflowPublicReportViewModel | None:
    if objective is None:
        return None
    if isinstance(objective, FixWikiPrimaryObjectiveSummary):
        return _fix_wiki_public_report_view_model(payload, objective)
    if isinstance(objective, ProcessChatsPrimaryObjectiveSummary):
        return _process_chats_public_report_view_model(payload, objective)
    return _generic_public_report_view_model(payload, objective)


def _fix_wiki_public_report_view_model(
    payload: JsonObject,
    objective: FixWikiPrimaryObjectiveSummary,
) -> WorkflowPublicReportViewModel:
    mutation_state = "changed" if objective.mutation_count > 0 or objective.written_count > 0 else "unchanged"
    human_reason = _human_decision_reason(payload)
    return WorkflowPublicReportViewModel(
        workflow="/mednotes:fix-wiki",
        run_id=str(payload.get("run_id") or ""),
        objective_answer=_fix_wiki_public_objective_answer(objective.wiki_fixed),
        headline=objective.wiki_summary,
        mutation_state=mutation_state,
        mutation_summary=objective.mutation_summary,
        remaining_work_summary=_join_public_parts(objective.graph_summary, objective.related_notes_summary),
        next_step_summary=_public_next_step(payload, fallback=objective.related_notes_summary),
        user_attention_required=bool(human_reason),
        human_reason=human_reason,
        internal_terms_present=False,
    )


def _process_chats_public_report_view_model(
    payload: JsonObject,
    objective: ProcessChatsPrimaryObjectiveSummary,
) -> WorkflowPublicReportViewModel:
    mutation_state = "changed" if objective.notes_status == "published" and objective.note_count > 0 else "unchanged"
    human_reason = _human_decision_reason(payload)
    return WorkflowPublicReportViewModel(
        workflow="/mednotes:process-chats",
        run_id=str(payload.get("run_id") or ""),
        objective_answer=_process_chats_public_objective_answer(objective.process_status),
        headline=objective.process_summary,
        mutation_state=mutation_state,
        mutation_summary=objective.wiki_write_summary,
        remaining_work_summary=_join_public_parts(objective.raw_summary, objective.coverage_summary, objective.linker_summary),
        next_step_summary=_public_next_step(payload, fallback=objective.linker_summary),
        user_attention_required=bool(human_reason),
        human_reason=human_reason,
        internal_terms_present=False,
    )


def _generic_public_report_view_model(
    payload: JsonObject,
    objective: WorkflowPrimaryObjectiveSummary,
) -> WorkflowPublicReportViewModel:
    human_reason = _human_decision_reason(payload)
    return WorkflowPublicReportViewModel(
        workflow=objective.workflow,
        run_id=objective.run_id,
        objective_answer=_generic_public_objective_answer(objective),
        headline=objective.objective,
        mutation_state=objective.mutation_state,
        mutation_summary=objective.mutation_summary,
        remaining_work_summary=objective.remaining_work_summary,
        next_step_summary=_public_next_step(payload, fallback=objective.next_step_summary),
        user_attention_required=bool(human_reason),
        human_reason=human_reason,
        internal_terms_present=False,
    )


def _fix_wiki_public_objective_answer(value: str) -> WorkflowPublicObjectiveAnswer:
    match value:
        case "yes":
            return "yes"
        case "waiting_agent":
            return "waiting_agent"
        case "waiting_external":
            return "waiting_external"
        case "failed":
            return "failed"
        case "no":
            return "no"
        case _:
            return "partial"


def _process_chats_public_objective_answer(value: str) -> WorkflowPublicObjectiveAnswer:
    match value:
        case "published" | "completed" | "completed_with_link_blockers" | "no_pending":
            return "yes"
        case "blocked":
            return "no"
        case "failed":
            return "failed"
        case _:
            return "partial"


def _generic_public_objective_answer(
    objective: WorkflowPrimaryObjectiveSummary,
) -> WorkflowPublicObjectiveAnswer:
    if objective.completed:
        return "yes"
    if objective.status == "failed" or "failed" in objective.status:
        return "failed"
    if objective.status == "waiting_external" or "waiting_external" in objective.status:
        return "waiting_external"
    if objective.status == "waiting_human" or "waiting_human" in objective.status:
        return "waiting_human"
    if objective.status == "blocked" or "blocked" in objective.status:
        return "no"
    if objective.status == "waiting_agent" or "waiting_agent" in objective.status:
        return "waiting_agent"
    return "partial"


def _human_decision_reason(payload: JsonObject) -> str:
    packet = _human_decision_packet(payload)
    if packet is None:
        return ""
    for value in (packet.why_this_needs_you, packet.question, packet.evidence_summary, packet.type, packet.kind):
        if value.strip():
            return value.strip()
    return "Decisao humana pendente."


def _human_decision_packet(payload: JsonObject) -> _HumanDecisionPacketFields | None:
    packet = _object_field(payload, "human_decision_packet")
    if not packet:
        return None
    return _HumanDecisionPacketFields.model_validate(
        _field_payload(packet, ("why_this_needs_you", "question", "evidence_summary", "type", "kind"))
    )


def _public_next_step(payload: JsonObject, *, fallback: str) -> str:
    progress = _PublicProgressFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("user_action",))
    )
    user_action = progress.user_action.strip()
    if user_action:
        return user_action
    receipt = _PublicReceiptFields.model_validate(_field_payload(_object_field(payload, "receipt"), ("next_action",)))
    next_action = receipt.next_action.strip()
    if next_action:
        return next_action
    return fallback


def _join_public_parts(*parts: str) -> str:
    cleaned = [part.strip() for part in parts if part.strip()]
    if not cleaned:
        return "Sem pendencias descritas."
    return " ".join(cleaned)


def _primary_objective_payload_findings(
    payload: JsonObject,
    objective: PrimaryObjectiveSummary,
) -> list[AgentRunReportFinding]:
    if not isinstance(objective, ProcessChatsPrimaryObjectiveSummary):
        return []
    if objective.process_status != "unknown":
        return []
    terminal = _ProcessChatsTerminalFields.model_validate(
        _field_payload(payload, ("workflow", "phase", "status", "item_count"))
    )
    workflow = terminal.workflow
    phase = terminal.phase
    status = terminal.status
    item_count = terminal.item_count or 0
    if workflow != "/mednotes:process-chats":
        return []
    if phase not in {"triage", "architect", "publish_dry_run", "publish_apply"} and not item_count:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.PROCESS_CHATS_PRIMARY_OBJECTIVE_UNRESOLVED,
            severity="high",
            source="workflow_payload",
            source_field="workflow/phase/status",
            expected="process-chats deve terminar em preview/publicação/linker ou blocker explícito antes do relatório final",
            actual=f"phase={phase or 'missing'} status={status or 'missing'} item_count={item_count}",
            message=(
                "O payload oficial ainda não prova que process-chats cumpriu o objetivo primário."
            ),
            next_action=(
                "Continuar a rota oficial de process-chats até publicar/preparar preview com coverage, "
                "rodar linker ou emitir blocker real antes de concluir."
            ),
        )
    ]


def _missing_fsm_primary_objective_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    schema = _optional_text(payload, "schema")
    workflow = _optional_text(payload, "workflow")
    if schema not in FSM_FIRST_SCHEMAS:
        if workflow == "/mednotes:process-chats":
            return [
                AgentRunReportFinding(
                    code=AgentRunReportFindingCode.PROCESS_CHATS_PRIMARY_OBJECTIVE_UNRESOLVED,
                    severity="high",
                    source="workflow_payload",
                    source_field="reports.details.primary_objective_summary",
                    expected="process-chats-fsm-result.v1 com reports.details.primary_objective_summary tipado",
                    actual=schema or "schema ausente",
                    message="O payload não trouxe o resumo primário canônico emitido pela FSM de process-chats.",
                    next_action=(
                        "Reexecutar /mednotes:process-chats pela rota FSM-first antes de validar o relatório final."
                    ),
                )
            ]
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.PRIMARY_OBJECTIVE_OMITTED,
            severity="high",
            source="workflow_payload",
            source_field="reports.details.primary_objective_summary",
            expected="payload FSM-first com reports.details.primary_objective_summary tipado",
            actual=schema or "schema ausente",
            message=f"O payload de {workflow or 'workflow FSM-first'} não trouxe o resumo primário canônico emitido pela FSM.",
            next_action="Corrigir a projeção FSM para emitir primary_objective_summary antes de validar relatório final.",
        )
    ]


def _safe_positive_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _public_output_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    for source_field, text in _public_text_sources(payload):
        lowered = text.lower()
        hits = [term for term in PUBLIC_OUTPUT_FORBIDDEN_TERMS if term in lowered]
        if not hits:
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PUBLIC_OUTPUT_INTERNAL_TERM_LEAK,
                severity="medium",
                source="workflow_payload",
                source_field=source_field,
                expected="linguagem pública sem comandos internos",
                actual=", ".join(hits),
                message="O payload público do workflow expôs termos internos de automação/desenvolvimento.",
                next_action=(
                    "Trocar o texto público por linguagem de usuário; deixe comandos, schemas, recibos e hashes "
                    "apenas em JSON/logs técnicos."
                ),
                evidence={"forbidden_terms": hits},
            )
        )
    return findings


def _final_report_internal_term_findings(final_text: str) -> list[AgentRunReportFinding]:
    lowered = final_text.lower()
    hits = [term for term in PUBLIC_OUTPUT_FORBIDDEN_TERMS if term in lowered]
    if not hits:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.PUBLIC_OUTPUT_INTERNAL_TERM_LEAK,
            severity="medium",
            source="final_report",
            source_field="final_report_text",
            expected="resposta pública sem nomes de campos, recibos, hashes ou estado técnico do guard",
            actual=", ".join(hits),
            message="A resposta final do agente expôs termos internos de automação/desenvolvimento.",
            next_action=(
                "Reescrever a resposta final em linguagem de usuário; deixe nomes de campos, recibos, "
                "hashes e detalhes técnicos do guard apenas em logs/JSON."
            ),
            evidence={"forbidden_terms": hits},
        )
    ]


def _public_text_sources(payload: JsonObject) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    progress = _object_field(payload, "progress_view_model")
    receipt = _object_field(payload, "receipt")
    decision = _object_field(payload, "decision")
    reports = _object_field(payload, "reports")
    for field, value in (
        ("progress_view_model.message", _optional_text(progress, "message")),
        ("progress_view_model.user_action", _optional_text(progress, "user_action")),
        ("receipt.next_action", _optional_text(receipt, "next_action")),
        ("decision.public_summary", _optional_text(decision, "public_summary")),
        ("decision.next_action", _optional_text(decision, "next_action")),
        ("reports.summary", _optional_text(reports, "summary")),
    ):
        if value.strip():
            sources.append((field, value))
    if "public_report" in reports:
        public_report = WorkflowPublicReport.model_validate(reports["public_report"])
        if public_report.headline.strip():
            sources.append(("reports.public_report.headline", public_report.headline))
        for index, line in enumerate(public_report.lines):
            if line.strip():
                sources.append((f"reports.public_report.lines[{index}]", line))
    return sources


def _optional_text(source: JsonObject, field_name: str) -> str:
    if field_name not in source or source[field_name] is None:
        return ""
    value = source[field_name]
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    return value


def _stale_next_action_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    progress = _object_field(payload, "progress_view_model")
    receipt = _object_field(payload, "receipt")
    decision = _object_field(payload, "decision")
    diagnostic = _object_field(payload, "diagnostic_context")
    related_state = _AgentReportRelatedRecoveryFields.model_validate(
        _field_payload(_object_field(diagnostic, "related_notes_recovery_state"), ("status",))
    )
    apply_context = _AgentReportApplyFields.model_validate(
        _field_payload(_object_field(diagnostic, "apply"), ("requested_apply",))
    )
    status = _optional_text(progress, "status") or _optional_text(receipt, "status")
    requested_apply = apply_context.requested_apply is True
    texts = [
        ("receipt.next_action", _optional_text(receipt, "next_action")),
        ("progress_view_model.user_action", _optional_text(progress, "user_action")),
        ("progress_view_model.resume_action", _optional_text(progress, "resume_action")),
        ("decision.next_action", _optional_text(decision, "next_action")),
    ]
    findings: list[AgentRunReportFinding] = []
    for source_field, text in texts:
        folded = _fold_text(text)
        if not folded:
            continue
        reason = ""
        if status == "waiting_external" and re.search(r"\b(dry-run|preview|previa|diagnostico)\b", folded):
            reason = "waiting_external_next_action_repeats_preview"
        if (
            status == "waiting_external"
            and related_state.status == "waiting_for_retry"
            and "export" in folded
            and "retom" not in folded
        ):
            reason = "related_notes_wait_next_action_regenerates_export"
        if requested_apply and status in NON_SUCCESS_STATUSES and re.search(r"\b(dry-run|preview|previa)\b", folded):
            reason = "apply_block_next_action_loops_to_preview"
        if not reason:
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.STALE_NEXT_ACTION,
                severity="high",
                source="workflow_payload",
                source_field=source_field,
                expected="próxima ação coerente com status/estado FSM",
                actual=text,
                message="A próxima ação pública ficou stale ou circular em relação ao estado canônico do workflow.",
                next_action="Gerar next_action a partir de progress_view_model/decision/receipt canônicos e revalidar o payload.",
                evidence={"reason": reason},
            )
        )
    return findings


def _workflow_payload_consistency_findings(payload: JsonObject) -> list[AgentRunReportFinding]:
    progress = _ProgressTruthFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("status", "can_continue_now"))
    )
    if not _agent_directive_requires_waiting_agent_continuation(payload):
        return []
    if progress.status == "waiting_agent" and progress.can_continue_now is True:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.WORKFLOW_CONTRACT_CONTRADICTION,
            severity="high",
            source="workflow_payload",
            source_field="agent_directive.control",
            expected="agent_directive com effects executáveis deve projetar progress_view_model.status=waiting_agent e can_continue_now=true",
            actual=f"status={progress.status or 'missing'} can_continue_now={progress.can_continue_now}",
            message=(
                "O payload oficial mistura continuação assistida executável com estado que não autoriza continuar."
            ),
            next_action=(
                "Corrigir a projeção FSM antes de confiar no relatório do agente ou repetir o experimento."
            ),
        )
    ]


def _waiting_agent_continuation_findings(
    payload: JsonObject,
    transcript: object,
    final_text: str,
    runtime_log_text: str,
) -> list[AgentRunReportFinding]:
    status, can_continue = _agent_continuation_status(payload)
    if status != "waiting_agent" or can_continue is not True:
        return []
    if not _agent_directive_requires_waiting_agent_continuation(payload):
        return []
    if _transcript_attempted_waiting_agent_continuation(
        transcript
    ) or _runtime_log_attempted_waiting_agent_continuation(runtime_log_text):
        return []
    if _reported_runtime_continuation_unavailable(final_text):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.WAITING_AGENT_CONTINUATION_OMITTED,
            severity="high",
            source="transcript",
            source_field="progress_view_model.status",
            expected="agente deve continuar pelos effects do agent_directive antes do relatório final",
            actual="relatório final emitido sem subagente/aplicação de reescrita",
            message="O workflow ficou em waiting_agent com continuação automática pronta, mas o agente encerrou sem executar a continuação.",
            next_action="Continuar pelo agent_directive.control.effects ou reportar explicitamente a incapacidade da CLI de invocar o subagente.",
        )
    ]


def _agent_directive_requires_waiting_agent_continuation(payload: JsonObject) -> bool:
    control = _agent_directive_control(payload)
    if control.status != "waiting_agent" or control.capabilities.continue_ is not True:
        return False
    return bool(control.effects or control.resume.strip())


def _agent_continuation_status(payload: JsonObject) -> tuple[str, bool | None]:
    control = _agent_directive_control(payload)
    if control.status:
        return control.status, control.capabilities.continue_
    progress = _ProgressTruthFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("status", "can_continue_now"))
    )
    return progress.status, progress.can_continue_now


def _ready_continuation_stopped_findings(
    payload: JsonObject,
    transcript: object,
    final_text: str,
    runtime_log_text: str,
) -> list[AgentRunReportFinding]:
    status, can_continue = _agent_continuation_status(payload)
    if status != "waiting_agent" or can_continue is not True:
        return []
    if not _agent_directive_requires_waiting_agent_continuation(payload):
        return []
    transcript_attempted = _transcript_attempted_waiting_agent_continuation(transcript)
    runtime_attempted = _runtime_log_attempted_waiting_agent_continuation(runtime_log_text)
    if not (transcript_attempted or runtime_attempted):
        return []
    if _reported_runtime_continuation_unavailable(final_text):
        return []
    if _transcript_reports_legitimate_specialist_stop(
        transcript,
        final_text,
    ) or _runtime_log_reports_legitimate_specialist_stop(runtime_log_text, final_text):
        return []
    folded = _fold_text(final_text)
    if not any(marker in folded for marker in ("proxima acao", "próxima ação", "retomar", "restam", "restantes")):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.READY_CONTINUATION_STOPPED,
            severity="high",
            source="workflow_payload",
            source_field="progress_view_model.status",
            expected="waiting_agent/can_continue_now=true deve continuar pela rota oficial ate quota, capacidade, validacao ruim ou fila vazia",
            actual="relatório final encerrou a rodada com continuação executável ainda pronta",
            message=(
                "O agente começou a continuação automática, mas parou e pediu retomada mesmo com o workflow ainda executável."
            ),
            next_action=(
                "Continuar pelo agent_directive.control.effects em vez de encerrar; se parar, reporte quota/capacidade/validação real como blocker."
            ),
        )
    ]


def _reported_runtime_continuation_unavailable(final_text: str) -> bool:
    if not final_text:
        return False
    return bool(RUNTIME_CONTINUATION_UNAVAILABLE_RE.search(final_text))


def _transcript_reports_legitimate_specialist_stop(transcript: object, final_text: str) -> bool:
    folded = _fold_text(final_text)
    if not folded:
        return False
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() not in {"tool_result", "run_command"}:
            continue
        output_text = _transcript_tool_output_text(event)
        payload = _json_payload_from_tool_output(output_text)
        if not payload:
            if _raw_transcript_output_reports_specialist_stop(output_text, folded):
                return True
            continue
        schema = _optional_text(payload, "schema")
        if schema != "medical-notes-workbench.specialist-task-runner-result.v1":
            if _raw_transcript_output_reports_specialist_stop(output_text, folded):
                return True
            continue
        status = _optional_text(payload, "status")
        blocked_reason = _optional_text(payload, "blocked_reason")
        if status not in {"blocked", "failed", "waiting_external"}:
            continue
        if blocked_reason not in LEGITIMATE_SPECIALIST_STOP_REASONS:
            continue
        if _folded_contains_any(
            folded,
            (blocked_reason, *ROOT_CAUSE_PUBLIC_LABELS.get(blocked_reason, ())),
        ):
            return True
    return False


def _raw_transcript_output_reports_specialist_stop(output_text: str, folded_final_text: str) -> bool:
    folded_output = _fold_text(output_text)
    if not folded_output or not folded_final_text:
        return False
    for blocked_reason in LEGITIMATE_SPECIALIST_STOP_REASONS:
        if blocked_reason not in folded_output:
            continue
        if _folded_contains_any(
            folded_final_text,
            (blocked_reason, *ROOT_CAUSE_PUBLIC_LABELS.get(blocked_reason, ())),
        ):
            return True
    return False


def _waiting_external_continuation_attempt_findings(
    payload: JsonObject,
    transcript: object,
) -> list[AgentRunReportFinding]:
    progress = _ProgressTruthFields.model_validate(
        _field_payload(_object_field(payload, "progress_view_model"), ("status", "can_continue_now"))
    )
    if progress.status != "waiting_external" and progress.can_continue_now is not False:
        return []
    if not _transcript_attempted_waiting_agent_continuation(transcript):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.WAITING_EXTERNAL_CONTINUATION_ATTEMPTED,
            severity="critical",
            source="transcript",
            source_field="progress_view_model.status",
            expected="waiting_external/can_continue_now=false deve parar sem invocar especialista ou comandos internos",
            actual="transcript tentou continuação especializada após o hard stop do workflow",
            message=(
                "O agente ignorou um estado não executável do workflow e tentou continuar a reescrita especializada."
            ),
            next_action=(
                "Não aplicar outputs dessa tentativa; corrigir o relatório/agente e retomar somente quando "
                "um runner oficial produzir recibo tipado."
            ),
        )
    ]


def _specialist_completed_apply_step_findings(transcript: object) -> list[AgentRunReportFinding]:
    pending_work_id = ""
    pending_apply_command = ""
    for event in _iter_transcript_events(transcript):
        event_type = event.event_type.casefold()
        if event_type in {"tool_result", "run_command"}:
            payload = _json_payload_from_tool_output(_transcript_tool_output_text(event))
            result = _SpecialistTaskRunnerResultFields.model_validate(
                _field_payload(payload, ("schema", "status", "work_id", "next_apply_step"))
            )
            if result.schema_id == "medical-notes-workbench.specialist-task-runner-result.v1" and result.status == "completed":
                pending_work_id = result.work_id
                if result.next_apply_step:
                    pending_apply_command = _optional_text(result.next_apply_step, "command_family")
                if not pending_apply_command:
                    pending_apply_command = "apply-specialist-style-rewrite"
            continue
        if event_type != "tool_use" or not pending_work_id:
            continue
        command = _event_parameter_text(event, "command")
        if not command:
            tool_name = event.tool_name.casefold()
            if tool_name == "read_file":
                return [_specialist_apply_step_omitted_finding(pending_work_id, "read_file")]
            continue
        folded = _fold_text(command)
        if pending_apply_command and pending_apply_command in folded and pending_work_id in command:
            pending_work_id = ""
            pending_apply_command = ""
            continue
        if _is_command_before_required_specialist_apply(folded):
            return [_specialist_apply_step_omitted_finding(pending_work_id, command)]
    return []


def _opencode_specialist_receipt_step_findings(
    payload: JsonObject,
    transcript: object,
) -> list[AgentRunReportFinding]:
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    if batch.phase != "style_rewrite":
        return []
    pending_work_ids: set[str] = set()
    for event in _iter_transcript_events(transcript):
        metadata = _opencode_task_metadata_from_event(event)
        if metadata is not None and metadata.work_id:
            pending_work_ids.add(metadata.work_id)
            continue
        if event.event_type.casefold() != "tool_use":
            continue
        command = _event_parameter_text(event, "command")
        if not command:
            continue
        folded = _fold_text(command)
        finalized_work_id = _command_argument(command, "--work-id") if "finalize-opencode-specialist-task" in folded else ""
        if finalized_work_id and finalized_work_id in pending_work_ids:
            pending_work_ids.remove(finalized_work_id)
            continue
        if "apply-specialist-style-rewrite" not in folded:
            continue
        work_id = _command_argument(command, "--work-id")
        if pending_work_ids and (not work_id or work_id in pending_work_ids):
            return [_specialist_apply_step_omitted_finding(work_id or sorted(pending_work_ids)[0], command)]
    return []


def _is_command_before_required_specialist_apply(folded_command: str) -> bool:
    return any(
        marker in folded_command
        for marker in (
            "fix-wiki --apply",
            "plan-subagents",
            "finalize-agy-specialist-task",
            "finalize-opencode-specialist-task",
            "finalize-style-rewrite-output",
            "collect-style-rewrite-outputs",
            "apply-style-rewrite",
        )
    )


def _specialist_apply_step_omitted_finding(work_id: str, actual: str) -> AgentRunReportFinding:
    return AgentRunReportFinding(
        code=AgentRunReportFindingCode.SPECIALIST_APPLY_STEP_OMITTED,
        severity="high",
        source="transcript",
        source_field="tool_result.output.next_apply_step",
        expected=(
            "quando a etapa especialista retorna completed, o proximo comando relevante deve ser "
            "apply-specialist-style-rewrite para o mesmo work_id"
        ),
        actual=actual,
        message=(
            "O agente recebeu uma reescrita especialista validada, mas desviou antes de aplicar o recibo oficial."
        ),
        next_action=(
            "Usar next_apply_step.arguments imediatamente após a etapa especialista completed; "
            "não ler manifesto, rerodar fix-wiki, chamar plan-subagents ou lançar outro especialista antes do apply."
        ),
        evidence={"work_id": work_id},
    )


def _transcript_attempted_waiting_agent_continuation(transcript: object) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, list):
            return any(visit(item) for item in value)
        if not isinstance(value, dict):
            return False
        event = _transcript_event_fields(value)
        if event is not None and event.event_type.casefold() in {"tool_use", "tool_result"}:
            raw_event = repr(event.model_dump(mode="json")).casefold()
            if any(marker in raw_event for marker in WAITING_AGENT_CONTINUATION_MARKERS):
                return True
        event_type = str(value.get("type") or "").upper()
        if event_type == "RUN_COMMAND":
            raw = repr(value).casefold()
            if any(marker in raw for marker in WAITING_AGENT_CONTINUATION_MARKERS):
                return True
        if event_type == "PLANNER_RESPONSE":
            tool_calls = value.get("tool_calls")
            if isinstance(tool_calls, list):
                raw = repr(tool_calls).casefold()
                if any(marker in raw for marker in WAITING_AGENT_CONTINUATION_MARKERS):
                    return True
        if _looks_like_saved_gemini_tool_call(value):
            raw = repr(value).casefold()
            if any(marker in raw for marker in WAITING_AGENT_CONTINUATION_MARKERS):
                return True
        for child in _transcript_child_containers(value):
            if visit(child):
                return True
        return False

    return visit(transcript)


def _runtime_log_attempted_waiting_agent_continuation(runtime_log_text: str) -> bool:
    folded = _fold_text(runtime_log_text)
    if not folded.strip():
        return False
    return _folded_contains_any(folded, WAITING_AGENT_CONTINUATION_MARKERS)


def _runtime_log_reports_legitimate_specialist_stop(runtime_log_text: str, final_text: str) -> bool:
    folded_log = _fold_text(runtime_log_text)
    folded_final = _fold_text(final_text)
    if not folded_log.strip() or not folded_final.strip():
        return False
    if not _runtime_log_attempted_waiting_agent_continuation(runtime_log_text):
        return False
    for blocked_reason in LEGITIMATE_SPECIALIST_STOP_REASONS:
        if not _folded_contains_any(folded_log, (blocked_reason,)):
            continue
        if _folded_contains_any(
            folded_final,
            (blocked_reason, *ROOT_CAUSE_PUBLIC_LABELS.get(blocked_reason, ())),
        ):
            return True
    quota_markers = (
        "terminalquotaerror",
        "quota_exhausted",
        "exhausted your capacity",
        "capacity on this model",
    )
    if _folded_contains_any(folded_log, quota_markers) and _folded_contains_any(
        folded_final,
        (
            "specialist_model_quota_exhausted",
            *ROOT_CAUSE_PUBLIC_LABELS["specialist_model_quota_exhausted"],
        ),
    ):
        return True
    return False


def _transcript_used_native_specialist_invocation(transcript: object) -> bool:
    native_tool_names = {"invoke_agent", "invoke_subagent", "define_subagent", "send_message"}
    for event in _iter_transcript_events(transcript):
        tool_name = event.tool_name.casefold()
        raw_event = repr(event.model_dump(mode="json")).casefold()
        if tool_name in native_tool_names and (
            "med-knowledge-architect" in raw_event or "style_rewrite" in raw_event
        ):
            return True
        if tool_name in {"run_command", "run_shell_command"}:
            command = _event_parameter_text(event, "command").casefold()
            if (
                "med-knowledge-architect" in command
                or "finalize-style-rewrite-output" in command
                or "apply-style-rewrite" in command
            ):
                return True
    return False


def _looks_like_saved_gemini_tool_call(value: JsonObject) -> bool:
    return isinstance(value.get("name"), str) and (
        "args" in value
        or "functionResponse" in value
        or "result" in value
        or "resultDisplay" in value
    )


def _blocked_workflow_tool_result_findings(
    transcript: object,
    final_text: str,
) -> list[AgentRunReportFinding]:
    blocked_results = _blocked_workflow_tool_results(transcript)
    if not blocked_results:
        return []
    folded = _fold_text(final_text)
    findings: list[AgentRunReportFinding] = []
    seen: set[str] = set()
    for result in blocked_results:
        key = f"{result.tool_name}:{result.phase}:{result.blocked_reason}:{result.work_id}"
        if key in seen:
            continue
        seen.add(key)
        reason_folded = _fold_text(result.blocked_reason)
        if folded and reason_folded.strip() and reason_folded in folded:
            continue
        if _final_report_explains_blocked_tool_result(result, folded):
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.BLOCKED_TOOL_RESULT_OMITTED,
                severity="high",
                source="transcript",
                source_field="tool_result.output.blocked_reason",
                expected="relatório final deve reportar qualquer payload de workflow bloqueado dentro de tool_result",
                actual=result.blocked_reason,
                message=(
                    "O transcript contém um comando com tool status=success, mas o payload oficial dentro "
                    "do output ficou bloqueado."
                ),
                next_action=(
                    "Reportar o blocked_reason literal, explicar o impacto no workflow e não tratar a "
                    "tool call como sucesso do workflow."
                ),
                evidence={
                    "tool_name": result.tool_name,
                    "phase": result.phase,
                    "work_id": result.work_id,
                    "status": result.status,
                },
            )
        )
    return findings


def _final_report_explains_blocked_tool_result(result: _BlockedWorkflowToolResult, folded_text: str) -> bool:
    if not folded_text:
        return False
    if result.blocked_reason != "style_rewrite_still_requires_rewrite":
        return False
    has_rewrite_context = any(marker in folded_text for marker in ("reescrita", "rewrite"))
    has_not_applied = any(
        marker in folded_text
        for marker in (
            "parou antes",
            "nao foi aplicada",
            "nao foi aplicado",
            "não foi aplicada",
            "não foi aplicado",
            "nenhuma nota",
            "pendente",
        )
    )
    has_style_cause = any(
        marker in folded_text
        for marker in (
            "criterios de estilo",
            "critérios de estilo",
            "nao atendeu",
            "não atendeu",
            "excesso de callouts",
            "visual didatico pendente",
            "visual didático pendente",
            "nota validada",
        )
    )
    return has_rewrite_context and has_not_applied and has_style_cause


def _blocked_workflow_tool_results(transcript: object) -> list[_BlockedWorkflowToolResult]:
    results: list[_BlockedWorkflowToolResult] = []
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() not in {"tool_result", "run_command"}:
            continue
        payload = _workflow_payload_from_tool_output(_transcript_tool_output_text(event))
        if not payload:
            continue
        status = _optional_text(payload, "status")
        blocked_reason = _optional_text(payload, "blocked_reason")
        if status != "blocked" or not blocked_reason:
            continue
        results.append(
            _BlockedWorkflowToolResult(
                tool_name=event.tool_name,
                status=status,
                phase=_optional_text(payload, "phase"),
                blocked_reason=blocked_reason,
                work_id=_optional_text(payload, "work_id"),
            )
        )
    return results


def _json_payload_from_tool_output(output: str) -> JsonObject:
    candidate = output.split("---", 1)[1] if "---" in output else output
    start = candidate.find("{")
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    try:
        parsed, _end = decoder.raw_decode(candidate[start:])
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _json_object(parsed)


def _tool_payload_contract_findings(transcript: object) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() not in {"tool_result", "run_command"}:
            continue
        payload = _json_payload_from_tool_output(_transcript_tool_output_text(event))
        schema = _optional_text(payload, "schema") if payload else ""
        if schema not in STYLE_REWRITE_APPLY_RESULT_SCHEMAS:
            continue
        try:
            StyleRewriteAtomicApplyResult.model_validate(payload)
        except ValidationError as exc:
            findings.append(_effect_payload_contract_invalid_finding(schema, exc))
    return findings


def _effect_payload_contract_invalid_finding(schema: str, exc: ValidationError) -> AgentRunReportFinding:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first_error.get("loc", ())) or "$"
    message = str(first_error.get("msg") or str(exc))
    return AgentRunReportFinding(
        code=AgentRunReportFindingCode.EFFECT_PAYLOAD_CONTRACT_INVALID,
        severity="critical",
        source="transcript.tool_result.output",
        source_field=schema,
        expected="payload de efeito validado por modelo Pydantic fechado antes de dirigir relatório ou contagem",
        actual=f"{location}: {message}",
        message=f"Tool output {schema} violou o contrato tipado antes de poder dirigir o workflow.",
        next_action=(
            "Reexecutar ou corrigir o produtor do efeito para emitir payload completo; não usar esse output "
            "para declarar apply, contagem ou conclusão."
        ),
    )


def _transcript_tool_output_text(event: _TranscriptEventFields) -> str:
    if event.output:
        return event.output
    if isinstance(event.content, str):
        return event.content
    return ""


def _workflow_payload_from_tool_output(output: str) -> JsonObject:
    if "blocked_reason" not in output or "blocked" not in output:
        return {}
    return _json_payload_from_tool_output(output)


def _iter_transcript_events(transcript: object) -> list[_TranscriptEventFields]:
    events: list[_TranscriptEventFields] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        event = _transcript_event_fields(value)
        if event is not None:
            events.append(event)
        events.extend(_planner_response_tool_call_events(value))
        for child in _transcript_child_containers(value):
            visit(child)

    visit(transcript)
    return events


def _transcript_child_containers(value: JsonObject) -> list[object]:
    children: list[object] = []
    for key in TRANSCRIPT_CHILD_CONTAINER_KEYS:
        child = value.get(key)
        if isinstance(child, (dict, list)):
            children.append(child)
    return children


def _planner_response_tool_call_events(value: JsonObject) -> list[_TranscriptEventFields]:
    event_type = str(value.get("type") or "").upper()
    if event_type != "PLANNER_RESPONSE":
        return []
    tool_calls = value.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    events: list[_TranscriptEventFields] = []
    for raw_tool_call in tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        args = raw_tool_call.get("args")
        parameters: JsonObject = {}
        if isinstance(args, dict):
            command = args.get("command") or args.get("CommandLine")
            if isinstance(command, str) and command.strip():
                parameters["command"] = command
        tool_name = raw_tool_call.get("name")
        events.append(
            _TranscriptEventFields.model_validate(
                {
                    "type": "tool_use",
                    "tool_name": tool_name if isinstance(tool_name, str) else "",
                    "parameters": parameters,
                    "content": raw_tool_call,
                }
            )
        )
    return events


def _transcript_event_fields(value: JsonObject) -> _TranscriptEventFields | None:
    normalized = dict(value)
    if not normalized.get("tool_name"):
        tool = normalized.get("tool")
        if isinstance(tool, str):
            normalized["tool_name"] = tool
    parameters = normalized.get("parameters")
    normalized_parameters = dict(parameters) if isinstance(parameters, dict) else {}
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict) and "metadata" not in normalized_parameters:
        normalized_parameters["metadata"] = metadata
    if normalized_parameters:
        normalized["parameters"] = normalized_parameters
    try:
        return _TranscriptEventFields.model_validate(normalized)
    except ValueError:
        return None


def _event_parameter_text(event: _TranscriptEventFields, field_name: str) -> str:
    """Read transcript tool parameters only after the event was normalized."""

    try:
        parameters = _TranscriptTextParameters.model_validate(_field_payload(event.parameters, ("command", "role")))
    except ValidationError:
        return ""
    match field_name:
        case "command":
            return parameters.command
        case "role":
            return parameters.role
        case _:
            raise ValueError(f"unsupported transcript text parameter: {field_name}")


def _opencode_task_metadata_from_event(
    event: _TranscriptEventFields,
) -> _OpenCodeSpecialistTaskMetadataFields | None:
    if event.tool_name.casefold() != "task":
        return None
    candidates = [
        event.parameters.get("metadata"),
        event.parameters.get("task_metadata"),
        event.parameters.get("taskMetadata"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        parsed = _opencode_task_metadata_from_candidate(JsonObjectAdapter.validate_python(candidate))
        if parsed is not None:
            return parsed
    return None


def _opencode_task_metadata_from_candidate(candidate: JsonObject) -> _OpenCodeSpecialistTaskMetadataFields | None:
    if str(candidate.get("schema") or "") == "medical-notes-workbench.opencode-specialist-task-metadata.v1":
        try:
            return _OpenCodeSpecialistTaskMetadataFields.model_validate(candidate)
        except ValidationError:
            return None
    native_model = candidate.get("model")
    if not isinstance(native_model, dict):
        return None
    provider_id = str(native_model.get("providerID") or native_model.get("provider_id") or "").strip()
    native_model_id = str(native_model.get("modelID") or native_model.get("model_id") or "").strip()
    if not provider_id and not native_model_id:
        return None
    model_id = native_model_id
    if provider_id and native_model_id and "/" not in native_model_id:
        model_id = f"{provider_id}/{native_model_id}"
    payload = {
        "schema": "medical-notes-workbench.opencode-specialist-task-metadata.v1",
        "work_id": str(candidate.get("work_id") or candidate.get("workID") or ""),
        "task_id": str(candidate.get("task_id") or candidate.get("taskID") or ""),
        "provider_id": provider_id,
        "model_id": model_id,
        "model_tier": "specialist",
        "tool_sequence": ["task"],
        "prompt_contract": str(candidate.get("prompt_contract") or ""),
        "raw_content_embedded": None,
    }
    try:
        return _OpenCodeSpecialistTaskMetadataFields.model_validate(payload)
    except ValidationError:
        return None


def _workflow_payload_omission_findings(
    payload: JsonObject,
    final_text: str,
    transcript: object | None,
) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    folded = _fold_text(final_text)
    final_report_incomplete = _final_report_looks_like_progress_only(final_text)
    diagnostic = _object_field(payload, "diagnostic_context")
    findings.extend(_omitted_agent_event_findings(diagnostic, folded))
    findings.extend(_omitted_version_control_safety_findings(payload, folded, transcript))
    findings.extend(_api_accounting_findings(payload, folded))
    findings.extend(_omitted_operational_warning_findings(diagnostic, folded))
    findings.extend(
        _content_quality_audit_findings(
            payload,
            folded,
            final_report_incomplete=final_report_incomplete,
        )
    )
    return findings


def _error_context_root_cause_findings(payload: JsonObject, final_text: str) -> list[AgentRunReportFinding]:
    root_cause, source_field = _canonical_root_cause(payload)
    if not root_cause:
        return []
    folded = _fold_text(final_text)
    if _folded_contains_any(
        folded,
        (root_cause, *ROOT_CAUSE_PUBLIC_LABELS.get(root_cause, ())),
    ):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.MISSING_ERROR_CONTEXT_ROOT_CAUSE,
            severity="high",
            source="workflow_payload",
            source_field=source_field,
            expected=root_cause,
            actual="omitted",
            message="O relatório final omitiu a causa raiz oficial do workflow.",
            next_action=(
                "Reescrever o relatório final priorizando error_context.root_cause/decision.reason_code "
                "antes de resumir exit code ou saída ruidosa da ferramenta."
            ),
            evidence={"root_cause": root_cause},
        )
    ]


def _canonical_root_cause(payload: JsonObject) -> tuple[str, str]:
    error_context = _object_field(payload, "error_context")
    root_cause = _optional_text(error_context, "root_cause")
    if root_cause:
        return root_cause, "error_context.root_cause"

    decision = _object_field(payload, "decision")
    reason_code = _optional_text(decision, "reason_code")
    if reason_code and reason_code not in NON_ERROR_DECISION_REASON_CODES:
        return reason_code, "decision.reason_code"

    blocked_reason = _optional_text(payload, "blocked_reason")
    if blocked_reason:
        return blocked_reason, "blocked_reason"

    return "", ""


def _omitted_agent_event_findings(diagnostic: JsonObject, folded_final_text: str) -> list[AgentRunReportFinding]:
    events = _collect_agent_events(diagnostic)
    relevant = [
        event
        for event in events
        if str(event.get("severity") or "").lower() in {"medium", "high", "critical"}
    ]
    if not relevant:
        return []
    omitted = [
        event
        for event in relevant
        if not _folded_contains_any(
            folded_final_text,
            (str(event.get(key) or "") for key in ("code", "root_cause_code", "type")),
        )
    ]
    if not omitted:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.AGENT_EVENT_OMITTED,
            severity="high",
            source="workflow_payload",
            source_field="diagnostic_context.agent_events",
            expected="eventos de agente medium+ devem aparecer no relatório final",
            actual=", ".join(str(event.get("code") or event.get("type") or "agent_event") for event in omitted[:5]),
            message="O relatório final omitiu agent_events relevantes emitidos pelo workflow.",
            next_action="Listar os agent_events relevantes e explicar impacto/mitigação no relatório da rodada.",
        )
    ]


def _omitted_version_control_safety_findings(
    payload: JsonObject,
    folded_final_text: str,
    transcript: object | None,
) -> list[AgentRunReportFinding]:
    safety = _AgentReportVersionControlSafetyFields.model_validate(
        _field_payload(
            _object_field(payload, "version_control_safety"),
            (
                "mutation_without_guard",
                "resource_guard_active",
                "run_finish_seen",
                "sync_status",
                "agent_instruction",
            ),
        )
    )
    findings: list[AgentRunReportFinding] = []
    if safety.mutation_without_guard is not True:
        pass
    elif not _folded_contains_any(
        folded_final_text,
        ("mutation_without_guard", "vault_guard", "version control", "controle de versao", "controle de versão"),
    ):
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.VERSION_CONTROL_SAFETY_OMITTED,
                severity="high",
                source="workflow_payload",
                source_field="version_control_safety.mutation_without_guard",
                expected="mutation_without_guard=true deve ser reportado",
                actual="omitted",
                message="O relatório final omitiu sinal de mutação sem guard de version control.",
                next_action="Reportar o sinal de version_control_safety e classificar se é limitação do harness ou bug do workflow.",
            )
        )
    if (
        safety.resource_guard_active is True
        and safety.run_finish_seen is False
        and not _mentions_guard_finish_pending(folded_final_text)
        and not _accepts_guard_finish_closed_confirmation(safety, folded_final_text)
        and not _transcript_confirms_guard_finish_closed(transcript)
    ):
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.RUN_FINISH_OMITTED,
                severity="high",
                source="workflow_payload",
                source_field="version_control_safety.run_finish_seen",
                expected="run_finish_seen=false com resource_guard_active=true deve ser reportado",
                actual="omitted",
                message="O relatório final omitiu que a proteção do vault ainda estava aberta.",
                next_action=(
                    "Fechar a proteção pela rota oficial ou reportar explicitamente que o workflow terminou "
                    "com pendência de proteção/version control."
                ),
                evidence={"sync_status": safety.sync_status},
            )
        )
    return findings


def _transcript_confirms_guard_finish_closed(transcript: object | None) -> bool:
    if transcript is None:
        return False
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() not in {"tool_result", "run_command"}:
            continue
        output_text = _transcript_tool_output_text(event)
        payload = _json_payload_from_tool_output(output_text)
        if _payload_confirms_guard_finish_closed(payload):
            return True
        folded = _fold_text(output_text)
        if (
            "vault-run-finish-public" in folded
            and "resource_guard_active" in folded
            and "false" in folded
            and "run_finish_seen" in folded
            and "true" in folded
        ):
            return True
    return False


def _payload_confirms_guard_finish_closed(payload: JsonObject) -> bool:
    if _optional_text(payload, "schema") != "medical-notes-workbench.vault-run-finish-public.v1":
        return False
    safety = payload.get("version_control_safety")
    if not isinstance(safety, dict):
        return False
    return safety.get("resource_guard_active") is False and safety.get("run_finish_seen") is True


def _mentions_guard_finish_pending(folded_text: str) -> bool:
    if not folded_text:
        return False
    has_guard = any(
        marker in folded_text
        for marker in (
            "vault_guard",
            "run_finish",
            "run-finish",
            "protecao do vault",
            "proteção do vault",
            "version control",
            "controle de versao",
            "controle de versão",
            "alteracoes concorrentes",
            "alterações concorrentes",
            "bloqueio de escrita concorrente",
            "ponto de restauracao",
            "ponto de restauração",
        )
    )
    has_pending = any(
        marker in folded_text
        for marker in (
            "pendente",
            "abert",
            "ativa",
            "nao encerr",
            "não encerr",
            "nao fech",
            "não fech",
            "pending_run_finish",
        )
    )
    return has_guard and has_pending


def _accepts_guard_finish_closed_confirmation(
    safety: _AgentReportVersionControlSafetyFields,
    folded_text: str,
) -> bool:
    if not folded_text:
        return False
    folded_instruction = _fold_text(safety.agent_instruction)
    if "antes do run-finish" not in folded_instruction and "before run-finish" not in folded_instruction:
        return False
    has_guard = any(
        marker in folded_text
        for marker in (
            "protecao do vault",
            "proteção do vault",
            "protecao do repositorio",
            "proteção do repositório",
            "vault guard",
            "vault_guard",
            "version control",
            "controle de versao",
            "controle de versão",
        )
    )
    has_closed = any(
        marker in folded_text
        for marker in (
            "encerrad",
            "fechad",
            "finalizad",
            "repositorio limpo",
            "repositório limpo",
            "clean",
        )
    )
    return has_guard and has_closed


def _runtime_log_findings(
    payload: JsonObject,
    runtime_log_text: str,
    final_text: str,
    transcript: object | None,
) -> list[AgentRunReportFinding]:
    findings = _runtime_performance_findings(runtime_log_text)
    findings.extend(_runtime_route_probe_findings(payload, runtime_log_text))
    findings.extend(_runtime_process_chats_vault_deletion_findings(payload, runtime_log_text))
    findings.extend(_runtime_specialist_model_policy_findings(payload, runtime_log_text, transcript))
    folded_log = _fold_text(runtime_log_text)
    if not folded_log:
        return findings
    runtime_errors = _runtime_error_labels(folded_log)
    if not runtime_errors:
        return findings
    folded_final = _fold_text(final_text)
    omitted = [
        label
        for label in runtime_errors
        if not _folded_contains_any(folded_final, _runtime_error_report_markers(label))
    ]
    if not omitted:
        return findings
    findings.append(
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.RUNTIME_ERROR_OMITTED,
            severity="high",
            source="runtime_log",
            source_field="runtime_log",
            expected="erros de runtime/headless devem aparecer no relatório final da rodada",
            actual=", ".join(omitted),
            message="O log do runtime contém erro relevante que o relatório final do agente não reportou.",
            next_action=(
                "Reescrever o relatório final incorporando o erro do runtime e seu impacto no workflow, "
                "mesmo quando o processo headless retornou exit code 0."
            ),
            evidence={"runtime_errors": omitted},
        )
    )
    return findings


def _runtime_process_chats_vault_deletion_findings(
    payload: JsonObject,
    runtime_log_text: str,
) -> list[AgentRunReportFinding]:
    if _optional_text(payload, "workflow") != "/mednotes:process-chats":
        return []
    folded_log = runtime_log_text or ""
    if not folded_log:
        return []
    deleted_paths = [
        match.group("path").strip()
        for match in PROCESS_CHATS_WIKI_DELETION_RE.finditer(folded_log)
        if match.group("path").strip()
    ]
    if not deleted_paths:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.PROCESS_CHATS_VAULT_DELETION_WITHOUT_RECEIPT,
            severity="critical",
            source="runtime_log",
            source_field="git status",
            expected="process-chats não deve apagar notas Wiki sem recibo tipado de merge/delete",
            actual=", ".join(deleted_paths[:5]),
            message=(
                "O runtime observou deleção de nota Wiki durante process-chats sem recibo tipado que autorize essa mutação."
            ),
            next_action=(
                "Parar a rodada, restaurar pelo vault guard/version control e repetir somente pela rota oficial "
                "de canonical merge/delete com receipt validado."
            ),
            evidence={"deleted_paths": deleted_paths[:20]},
        )
    ]


def _runtime_specialist_model_policy_findings(
    payload: JsonObject,
    runtime_log_text: str,
    transcript: object | None,
) -> list[AgentRunReportFinding]:
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    if batch.phase != "style_rewrite" or not batch.current_batch_items:
        return []
    specialist_items = [
        item
        for item in batch.current_batch_items
        if item.required_model_tier in {"specialist", "pro"}
        or item.preferred_model_tier == "pro"
        or item.model_policy == "medical_specialist_authoring.v1"
        or item.agent == "med-knowledge-architect"
    ]
    if not specialist_items:
        return []
    observed_model = _observed_agy_selected_model(runtime_log_text)
    if not observed_model or FLASH_MODEL_RE.search(observed_model) is None:
        return []
    if transcript is None or not _transcript_used_native_specialist_invocation(transcript):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.SPECIALIST_MODEL_POLICY_VIOLATION,
            severity="critical",
            source="runtime_log",
            source_field="runtime_log.selected_model+transcript.specialist_invocation",
            expected="tarefas médicas especializadas exigem modelo especialista/Pro sem fallback para Flash",
            actual=observed_model,
            message="O runtime selecionou Flash durante uma tarefa de reescrita médica especializada.",
            next_action=(
                "Não aplicar outputs desse lote; relançar a tarefa por runner oficial capaz de garantir "
                "modelo especialista/Pro e recibo atestado."
            ),
            evidence={
                "observed_model": observed_model,
                "transcript_specialist_invocation": "native",
                "work_ids": [item.work_id for item in specialist_items if item.work_id],
                "required_model_tiers": sorted({item.required_model_tier for item in specialist_items}),
                "model_policies": sorted({item.model_policy for item in specialist_items if item.model_policy}),
            },
        )
    ]


def _transcript_specialist_model_policy_findings(
    payload: JsonObject,
    transcript: object,
) -> list[AgentRunReportFinding]:
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    if batch.phase != "style_rewrite":
        return []
    specialist_items = [
        item
        for item in batch.current_batch_items
        if item.required_model_tier in {"specialist", "pro"}
        or item.preferred_model_tier == "pro"
        or item.model_policy == "medical_specialist_authoring.v1"
        or item.agent == "med-knowledge-architect"
    ]
    if not specialist_items:
        return []
    findings: list[AgentRunReportFinding] = []
    seen: set[tuple[str, str]] = set()
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() != "tool_use":
            continue
        opencode_metadata = _opencode_task_metadata_from_event(event)
        if opencode_metadata is not None:
            observed_model = opencode_metadata.model_id
            if not observed_model or FLASH_MODEL_RE.search(observed_model):
                key = ("opencode-task-model", observed_model)
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        AgentRunReportFinding(
                            code=AgentRunReportFindingCode.SPECIALIST_MODEL_POLICY_VIOLATION,
                            severity="critical",
                            source="transcript",
                            source_field="transcript.tool_use.parameters.metadata.model_id",
                            expected=(
                                "OpenCode task especialista deve provar modelo especialista/Pro via "
                                "opencode_task_metadata, sem fallback para Flash/Lite/Nano"
                            ),
                            actual=observed_model or "<missing>",
                            message=(
                                "A task OpenCode de autoria médica especializada registrou modelo ausente "
                                "ou proibido pela política de modelo."
                            ),
                            next_action=(
                                "Descartar outputs sem recibo valido e repetir a task OpenCode com modelo "
                                "especialista aceito antes de aplicar."
                            ),
                            evidence={
                                "harness": "opencode",
                                "observed_model": observed_model,
                                "provider_id": opencode_metadata.provider_id,
                                "task_id": opencode_metadata.task_id,
                                "work_id": opencode_metadata.work_id,
                                "work_ids": [item.work_id for item in specialist_items if item.work_id],
                            },
                        )
                    )
        command = _event_parameter_text(event, "command")
        if _command_uses_unverified_specialist_model_escape(command):
            key = ("unverified-specialist-model-escape", "public-workflow")
            if key not in seen:
                seen.add(key)
                findings.append(
                    AgentRunReportFinding(
                        code=AgentRunReportFindingCode.SPECIALIST_MODEL_POLICY_VIOLATION,
                        severity="critical",
                        source="transcript",
                        source_field="transcript.tool_use.parameters.command.env",
                        expected=(
                            "fluxo publico não deve usar dev-escape para aceitar modelo especialista "
                            "não verificado pelo Workbench"
                        ),
                        actual="MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL",
                        message=(
                            "O agente tentou contornar a proveniência de modelo especialista com variável "
                            "de escape de desenvolvedor."
                        ),
                        next_action=(
                            "Descartar o output desse item, reportar a violação e retomar pela rota oficial "
                            "com recibo/proveniência validada pelo Workbench."
                        ),
                        evidence={
                            "work_ids": [item.work_id for item in specialist_items if item.work_id],
                            "tool_name": event.tool_name,
                        },
                    )
                )
    return findings


def _command_uses_unverified_specialist_model_escape(command: str) -> bool:
    if "MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL" not in command:
        return False
    return "finalize-style-rewrite-output" in command or "apply-specialist-style-rewrite" in command


def _style_rewrite_batch_progress_checkpoint_findings(
    payload: JsonObject,
    transcript: object,
) -> list[AgentRunReportFinding]:
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    if batch.phase != "style_rewrite":
        return []
    if not batch.report_contract.after_each_batch:
        return []
    saw_batch_apply = False
    assistant_message_buffer: list[str] = []
    for event in _iter_transcript_events(transcript):
        event_type = event.event_type.casefold()
        if event_type == "message":
            role = (event.role or _event_parameter_text(event, "role")).casefold()
            if saw_batch_apply and role in {"", "assistant", "model"}:
                text = _transcript_message_text(event.content)
                if text.strip():
                    assistant_message_buffer.append(text)
            continue
        if event_type == "tool_result":
            continue
        if event_type != "tool_use":
            continue
        command = _event_parameter_text(event, "command")
        if not command:
            continue
        if saw_batch_apply and _looks_like_style_rewrite_batch_report("\n".join(assistant_message_buffer)):
            saw_batch_apply = False
            assistant_message_buffer = []
        if _is_real_style_rewrite_apply_command(command):
            saw_batch_apply = True
            assistant_message_buffer = []
            continue
        if saw_batch_apply and _is_next_style_rewrite_batch_command(command):
            return [
                AgentRunReportFinding(
                    code=AgentRunReportFindingCode.BATCH_PROGRESS_REPORT_OMITTED,
                    severity="high",
                    source="transcript",
                    source_field="transcript.tool_use.parameters.command",
                    expected=(
                        "após aplicar um lote de style-rewrite, o agente deve emitir resumo humano "
                        "com qualidade, preservação e pendências antes de planejar/rodar a próxima leva"
                    ),
                    actual=command,
                    message=(
                        "O agente continuou a próxima etapa de reescrita sem cumprir o checkpoint de relatório do lote."
                    ),
                    next_action=(
                        "Interromper a conclusão da rodada, reportar o lote aplicado em termos humanos e só então "
                        "retomar a próxima leva pela rota oficial."
                    ),
                    evidence={
                        "command": command,
                        "batch_work_ids": [item.work_id for item in batch.current_batch_items if item.work_id],
                    },
                )
            ]
    return []


def _specialist_rewrite_count_findings(transcript: object, final_text: str) -> list[AgentRunReportFinding]:
    work_ids = _applied_specialist_rewrite_work_ids(transcript)
    if not work_ids:
        return []
    reported_count = _reported_specialist_rewrite_count(final_text)
    if reported_count is None or reported_count == len(work_ids):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.SPECIALIST_REWRITE_COUNT_MISMATCH,
            severity="high",
            source="transcript",
            source_field="tool_result.output.style_rewrite_applied_count",
            expected=str(len(work_ids)),
            actual=str(reported_count),
            message="O relatório final declarou uma contagem de notas reescritas diferente dos applies oficiais observados.",
            next_action=(
                "Reescrever o relatório final usando a contagem real de applies oficiais e listar qualquer item aplicado, "
                "bloqueado ou pendente sem arredondar a evidência."
            ),
            evidence={"work_ids": work_ids},
        )
    ]


def _applied_specialist_rewrite_work_ids(transcript: object) -> list[str]:
    work_ids: list[str] = []

    def append(value: object) -> None:
        work_id = str(value or "").strip()
        if work_id and work_id not in work_ids:
            work_ids.append(work_id)

    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() not in {"tool_result", "run_command"}:
            continue
        payload = _json_payload_from_tool_output(_transcript_tool_output_text(event))
        schema = _optional_text(payload, "schema")
        if schema not in {
            "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1",
            "medical-notes-workbench.style-rewrite-atomic-apply-result.v1",
        }:
            continue
        if _optional_text(payload, "status").casefold() in {"blocked", "failed", "waiting_external"}:
            continue
        candidates = [payload]
        nested_apply = _object_field(payload, "apply")
        if nested_apply:
            candidates.append(nested_apply)
        for candidate in candidates:
            try:
                apply_result = StyleRewriteAtomicApplyResult.model_validate(candidate)
            except ValidationError:
                continue
            fallback_work_id = (apply_result.work_id or _optional_text(payload, "work_id")).strip()
            for item in apply_result.items:
                if item.written:
                    append(item.work_id or fallback_work_id)
            if apply_result.written_count > 0:
                append(fallback_work_id)
    return work_ids


def _reported_specialist_rewrite_count(final_text: str) -> int | None:
    folded = _fold_text(final_text)
    for match in SPECIALIST_REWRITE_COUNT_CLAIM_RE.finditer(folded):
        return _as_int(match.group("count"))
    return None


def _tool_result_has_style_rewrite_progress_checkpoint(output: str) -> bool:
    payload = _json_payload_from_tool_output(output)
    if not payload:
        return False
    candidate: object = payload
    if _optional_text(payload, "schema") == "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1":
        candidate = payload["human_progress_checkpoint"] if "human_progress_checkpoint" in payload else None
    if not isinstance(candidate, dict):
        return False
    if candidate.get("schema") != "medical-notes-workbench.style-rewrite-human-progress-checkpoint.v1":
        return False
    text = "\n".join(
        str(candidate.get(key) or "")
        for key in (
            "summary",
            "content_quality",
            "linker_summary",
            "remaining_summary",
        )
    )
    preserved = candidate.get("preserved")
    if isinstance(preserved, list):
        text += "\n" + "\n".join(str(item) for item in preserved)
    return _looks_like_style_rewrite_batch_report(text)


def _is_real_style_rewrite_apply_command(command: str) -> bool:
    folded = _fold_text(command)
    if "apply-specialist-style-rewrite" in folded:
        return True
    return "apply-style-rewrite" in folded and "--dry-run" not in folded


def _is_next_style_rewrite_batch_command(command: str) -> bool:
    folded = _fold_text(command)
    if "plan-subagents" in folded and "style-rewrite" in folded:
        return True
    return "fix-wiki" in folded and "--apply" in folded


def _looks_like_style_rewrite_batch_report(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    has_batch = "lote" in folded or "batch" in folded
    has_quality = "qualidade" in folded or "quality" in folded
    has_preservation = any(
        marker in folded
        for marker in (
            "yaml",
            "proveniencia",
            "proveniência",
            "links preserv",
            "preservou links",
            "preserved links",
        )
    )
    has_remaining = any(
        marker in folded
        for marker in (
            "restam",
            "restante",
            "remaining",
            "pendente",
            "faltam",
            "continua",
        )
    )
    return has_batch and has_quality and has_preservation and has_remaining


def _command_argument(command: str, option: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        pattern = re.compile(rf"{re.escape(option)}\s+(?P<value>\S+)")
        match = pattern.search(command)
        return match.group("value") if match else ""
    for index, part in enumerate(parts[:-1]):
        if part == option:
            return parts[index + 1]
    return ""


def _observed_runtime_model(runtime_log_text: str) -> str:
    labels = [match.group("label").strip() for match in AGY_SELECTED_MODEL_RE.finditer(runtime_log_text)]
    if labels:
        return labels[-1]
    flash_match = FLASH_MODEL_RE.search(runtime_log_text)
    return flash_match.group(0) if flash_match else ""


def _observed_agy_selected_model(runtime_log_text: str) -> str:
    labels = [match.group("label").strip() for match in AGY_SELECTED_MODEL_RE.finditer(runtime_log_text)]
    return labels[-1] if labels else ""


def _runtime_performance_findings(runtime_log_text: str) -> list[AgentRunReportFinding]:
    samples = _runtime_cpu_samples(runtime_log_text)
    findings: list[AgentRunReportFinding] = []
    active_runs: dict[str, list[_RuntimeCpuSample]] = {}
    for sample in sorted(samples, key=lambda item: item.elapsed_seconds):
        command_family = _cpu_command_family(sample.max_cpu_command)
        for stale_family in tuple(active_runs):
            if stale_family == command_family:
                continue
            findings.extend(
                _runtime_performance_findings_for_family(
                    stale_family,
                    active_runs.pop(stale_family),
                    total_sample_count=len(samples),
                )
            )
        if max(sample.total_cpu_percent, sample.max_cpu_percent) >= HIGH_CPU_PERCENT_THRESHOLD:
            active_runs.setdefault(command_family, []).append(sample)
            continue
        if command_family in active_runs:
            findings.extend(
                _runtime_performance_findings_for_family(
                    command_family,
                    active_runs.pop(command_family),
                    total_sample_count=len(samples),
                )
            )
    for command_family, family_samples in active_runs.items():
        findings.extend(
            _runtime_performance_findings_for_family(
                command_family,
                family_samples,
                total_sample_count=len(samples),
            )
        )
    return findings


def _runtime_route_probe_findings(
    payload: JsonObject,
    runtime_log_text: str,
) -> list[AgentRunReportFinding]:
    if not _is_process_chats_terminal_no_pending(payload):
        return []
    commands = [
        sample.max_cpu_command
        for sample in _runtime_cpu_samples(runtime_log_text)
        if _is_route_probe_command(sample.max_cpu_command)
    ]
    if not commands:
        return []
    unique_commands = list(dict.fromkeys(commands))
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.RUNTIME_ROUTE_PROBE_OBSERVED,
            severity="medium",
            source="runtime_log",
            source_field="runtime_log.cpu_samples.max_cpu_command",
            expected=(
                "process-chats terminal sem chats novos deve executar a checagem oficial direta "
                "sem probes recursivos de descoberta"
            ),
            actual="; ".join(command[:160] for command in unique_commands),
            message=(
                "O runtime registrou busca/probe recursivo durante um fluxo terminal simples; "
                "isso é atrito de rota e deve aparecer no relatório da rodada."
            ),
            next_action=(
                "Endurecer launcher/runbook ou harness para iniciar pela porta pública `list-pending --summary` "
                "sem busca exploratória, e repetir a rodada validando transcript/runtime log."
            ),
            evidence={
                "schema": CPU_SAMPLE_SCHEMA,
                "commands": unique_commands[:5],
            },
        )
    ]


def _is_process_chats_terminal_no_pending(payload: JsonObject) -> bool:
    fields = _ProcessChatsTerminalFields.model_validate(
        _field_payload(
            payload,
            (
                "workflow",
                "status",
                "phase",
                "process_chats_terminal_state",
                "process_chats_backlog_state",
                "item_count",
                "total_available_count",
            ),
        )
    )
    if fields.workflow != "/mednotes:process-chats" or fields.status != "completed":
        return False
    if fields.process_chats_terminal_state == "no_pending":
        return True
    if fields.phase == "pending_backlog" and fields.process_chats_backlog_state == "no_pending_raws":
        return True
    if fields.item_count == 0 and fields.total_available_count == 0:
        return True
    return False


def _is_route_probe_command(command: str) -> bool:
    parts = shlex.split(command)
    if not parts:
        return False
    executable = Path(parts[0]).name
    if executable == "grep" and "-r" in parts:
        return True
    if executable in {"find", "mdfind"}:
        return True
    if executable in {"rg", "ripgrep"} and any(part in {"-g", "--glob", "--files"} for part in parts):
        return True
    return False


def _runtime_performance_findings_for_family(
    command_family: str,
    high_samples: list[_RuntimeCpuSample],
    *,
    total_sample_count: int,
) -> list[AgentRunReportFinding]:
    if len(high_samples) < HIGH_CPU_MIN_SAMPLE_COUNT:
        return []
    observed_span = _estimated_high_cpu_span_seconds(high_samples)
    if observed_span < HIGH_CPU_MIN_SPAN_SECONDS:
        return []
    max_total_cpu = max(sample.total_cpu_percent for sample in high_samples)
    max_process_cpu = max(sample.max_cpu_percent for sample in high_samples)
    max_observed_cpu = max(max_total_cpu, max_process_cpu)
    max_sample = max(high_samples, key=lambda sample: max(sample.total_cpu_percent, sample.max_cpu_percent))
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.RUNTIME_PERFORMANCE_BUG,
            severity="medium",
            source="runtime_log",
            source_field="runtime_log.cpu_samples",
            expected="workflow longo deve manter CPU sob controle ou reportar progresso claro antes de monopolizar a sessão",
            actual=(
                f"{len(high_samples)} amostras acima de {HIGH_CPU_PERCENT_THRESHOLD:.0f}% "
                f"por {observed_span:.1f}s; pico={max_observed_cpu:.1f}%"
            ),
            message="A execução registrou CPU alta sustentada; isso é bug de performance/UX do workflow.",
            next_action=(
                "Investigar a fase do workflow que monopolizou CPU, adicionar progresso/limites quando necessário "
                "e reportar o impacto na próxima rodada de experimento."
            ),
            evidence={
                "schema": CPU_SAMPLE_SCHEMA,
                "command_family": command_family,
                "sample_count": len(high_samples),
                "total_sample_count": total_sample_count,
                "threshold_percent": HIGH_CPU_PERCENT_THRESHOLD,
                "observed_span_seconds": round(observed_span, 2),
                "max_cpu_percent": round(max_observed_cpu, 2),
                "max_total_cpu_percent": round(max_total_cpu, 2),
                "max_process_cpu_percent": round(max_process_cpu, 2),
                "max_cpu_command": max_sample.max_cpu_command[:500],
            },
        )
    ]


def _estimated_high_cpu_span_seconds(high_samples: list[_RuntimeCpuSample]) -> float:
    elapsed_values = sorted(sample.elapsed_seconds for sample in high_samples)
    if len(elapsed_values) < 2:
        return 0.0
    gaps = [
        after - before
        for before, after in zip(elapsed_values, elapsed_values[1:], strict=False)
        if after > before
    ]
    sample_window = min(gaps) if gaps else 0.0
    return max(elapsed_values) - min(elapsed_values) + sample_window


def _cpu_command_family(command: str) -> str:
    folded = command.casefold()
    if "mednotes/wiki/cli.py" in folded or "fix-wiki --apply" in folded:
        return "workbench_cli"
    if "/gemini" in folded or " gemini " in folded or folded.startswith("gemini "):
        return "external_model_runtime"
    return "other"


def _runtime_cpu_samples(runtime_log_text: str) -> list[_RuntimeCpuSample]:
    samples: list[_RuntimeCpuSample] = []
    for line in runtime_log_text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            sample = _RuntimeCpuSample.model_validate_json(candidate)
        except ValueError:
            continue
        if sample.schema_id == CPU_SAMPLE_SCHEMA:
            samples.append(sample)
    return samples


def _runtime_error_labels(folded_log: str) -> list[str]:
    labels: list[str] = []
    if "resource_exhausted" in folded_log or "code 429" in folded_log or " 429 " in folded_log:
        labels.append("RESOURCE_EXHAUSTED/429 quota")
    if "etimedout" in folded_log or "read timed out" in folded_log:
        labels.append("specialist model runtime timeout")
    if "agent executor error" in folded_log:
        labels.append("agent executor error")
    recovered_antigravity_auth = (
        "you are not logged into antigravity" in folded_log
        and ("auth succeeded" in folded_log or "silent auth succeeded" in folded_log)
        and "authentication timed out" not in folded_log
    )
    if (
        "authentication timed out" in folded_log
        or ("you are not logged into antigravity" in folded_log and not recovered_antigravity_auth)
    ):
        labels.append("antigravity authentication transient")
    return labels


def _runtime_error_report_markers(label: str) -> tuple[str, ...]:
    folded = _fold_text(label)
    if "resource_exhausted" in folded or "429" in folded or "quota" in folded:
        return ("resource_exhausted", "429", "quota", "cota", "cota 429")
    if "timeout" in folded:
        return (
            "etimedout",
            "read etimedout",
            "read timed out",
            "timeout",
            "tempo esgotado",
            "modelo especialista",
        )
    if "executor" in folded:
        return ("agent executor error", "executor", "erro de executor")
    if "authentication" in folded or "antigravity" in folded:
        return (
            "not logged into antigravity",
            "authentication timed out",
            "auth timed out",
            "antigravity",
            "autenticacao",
            "autenticação",
        )
    return (label,)


def _content_quality_audit_findings(
    payload: JsonObject,
    folded_final_text: str,
    *,
    final_report_incomplete: bool = False,
) -> list[AgentRunReportFinding]:
    if final_report_incomplete:
        return []
    batch = _specialist_runtime_batch_from_agent_directive(payload)
    report_contract = batch.report_contract
    if "content_quality_audit" not in set(report_contract.must_include):
        return []
    if _mentions_content_quality_audit(folded_final_text):
        return []
    if _mentions_content_quality_audit_not_applicable(folded_final_text):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.CONTENT_QUALITY_AUDIT_OMITTED,
            severity="high",
            source="workflow_payload",
            source_field="agent_directive.control.effects[].payload.report_contract.must_include",
            expected="auditoria de conteúdo/qualidade antes-depois das notas reescritas",
            actual="omitted",
            message="O relatório final omitiu a auditoria de conteúdo exigida para notas reescritas.",
            next_action=(
                "Reescrever o relatório final com auditoria antes/depois por nota: preservação de YAML/proveniência/links, "
                "qualidade clínica/didática e classificação resolvida/parcial/não resolvida/piorou."
            ),
        )
    ]


def _mentions_content_quality_audit(folded_text: str) -> bool:
    has_audit = any(
        marker in folded_text
        for marker in ("auditoria de conteudo", "auditoria de qualidade", "content quality audit")
    )
    has_before_after = any(
        marker in folded_text
        for marker in ("antes/depois", "antes e depois", "before/after")
    )
    has_quality = any(
        marker in folded_text
        for marker in ("qualidade clinica", "qualidade de conteudo", "bug de conteudo", "bug de ux")
    )
    has_outcome_classification = any(
        marker in folded_text
        for marker in ("resolvid", "parcial", "nao resolvid", "não resolvid", "piorou")
    )
    return has_audit and has_before_after and has_quality and has_outcome_classification


def _mentions_content_quality_audit_not_applicable(folded_text: str) -> bool:
    has_specialist_block = any(
        marker in folded_text
        for marker in (
            "specialist_model_quota_exhausted",
            "cota do modelo",
            "cota de uso do modelo",
            "cota no modelo",
            "quota do modelo",
            "capacidade do modelo",
            "capacidade externa do modelo",
            "limitacoes temporarias de cota",
            "limitações temporárias de cota",
            "modelo especialista",
            "modelo medico",
            "modelo médico",
            "modelo especializado",
            "modelo medico especializado",
            "modelo médico especializado",
            "modelo de ia especializado",
            "bloqueio imediato do modelo",
            "reescrita medica especializada",
            "reescrita médica especializada",
            "conteudo gerado",
            "conteúdo gerado",
            "criterios de estilo",
            "critérios de estilo",
            "visual didatico pendente",
            "visual didático pendente",
        )
    )
    has_rewrite_context = any(marker in folded_text for marker in ("reescrita", "rewrite", "conteudo clinico"))
    has_no_applied_output = any(
        marker in folded_text
        for marker in (
            "bloquead",
            "bloqueio",
            "interrompid",
            "nao foi aplicad",
            "não foi aplicad",
            "nenhuma nota",
            "nao avaliad",
            "não avaliad",
            "pendente",
        )
    )
    return has_specialist_block and has_rewrite_context and has_no_applied_output


def _api_accounting_findings(payload: JsonObject, folded_final_text: str) -> list[AgentRunReportFinding]:
    headless = _AgentReportHeadlessExportFields.model_validate(
        _field_payload(
            _object_field(
                _object_field(_object_field(payload, "diagnostic_context"), "related_notes_export_recovery"),
                "headless_export",
            ),
            ("embedded_count",),
        )
    )
    embedded_count = _as_int(headless.embedded_count)
    if embedded_count <= 0:
        return []
    denies_api_work = bool(
        re.search(
            r"(api_calls\s*[:=]\s*0|0\s+chamadas?\s+(?:a|à|ao|de)?\s*api|"
            r"n[aã]o\s+houve\s+chamadas?|sem\s+chamadas?|no\s+api)",
            folded_final_text,
        )
    )
    if not denies_api_work:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.API_ACCOUNTING_MISMATCH,
            severity="medium",
            source="workflow_payload",
            source_field="diagnostic_context.related_notes_export_recovery.headless_export.embedded_count",
            expected="relatório deve reconciliar embedded_count antes de afirmar zero chamadas de API",
            actual=f"embedded_count={embedded_count}",
            message="O relatório final afirmou zero trabalho de API apesar de o payload indicar embeddings gerados.",
            next_action="Explicar a diferença entre api_calls do workflow e embedded_count do export, ou corrigir os contadores.",
        )
    ]


def _omitted_operational_warning_findings(
    diagnostic: JsonObject,
    folded_final_text: str,
) -> list[AgentRunReportFinding]:
    warnings = _collect_graph_warnings(diagnostic)
    codes = {str(warning.get("code") or "") for warning in warnings if isinstance(warning, dict)}
    if "catalog_missing" not in codes or "catalog" in folded_final_text:
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.OPERATIONAL_WARNING_OMITTED,
            severity="medium",
            source="workflow_payload",
            source_field="diagnostic_context.graph_audit_final.warnings",
            expected="warning catalog_missing deve aparecer no relatório de experimento",
            actual="omitted",
            message="O relatório final omitiu warning operacional catalog_missing.",
            next_action="Reportar o warning e decidir se CATALOGO_WIKI.json é legado ou artefato ainda obrigatório.",
        )
    ]


def _collect_agent_events(value: object) -> list[JsonObject]:
    events: list[JsonObject] = []
    seen: set[tuple[str, str, str]] = set()

    def visit(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        agent_events = item.get("agent_events")
        if isinstance(agent_events, list):
            for event in agent_events:
                if isinstance(event, dict):
                    event_payload = _json_object(event)
                    key = (
                        _optional_text(event_payload, "code"),
                        _optional_text(event_payload, "type"),
                        _optional_text(event_payload, "phase"),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append(event_payload)
        for child in item.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(value)
    return events


def _collect_graph_warnings(diagnostic: JsonObject) -> list[JsonObject]:
    graph = _object_field(diagnostic, "graph_audit_final")
    warnings = graph.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [_json_object(warning) for warning in warnings if isinstance(warning, dict)]


def _folded_contains_any(folded_text: str, candidates: Iterable[object]) -> bool:
    for candidate in candidates:
        text = _fold_text(str(candidate or ""))
        if text.strip() and text.strip() in folded_text:
            return True
    return False


def _as_int(value: object) -> int:
    return _safe_positive_int(value)


def _primary_objective_omission_findings(
    final_text: str,
    objective: PrimaryObjectiveSummary,
) -> list[AgentRunReportFinding]:
    if isinstance(objective, ProcessChatsPrimaryObjectiveSummary):
        return _process_chats_primary_objective_omission_findings(final_text, objective)
    if isinstance(objective, WorkflowPrimaryObjectiveSummary):
        return _generic_primary_objective_omission_findings(final_text, objective)

    checks = (
        ("primary_objective.wiki_fixed", _mentions_wiki_outcome(final_text), objective.wiki_summary),
        ("primary_objective.mutation_summary", _mentions_mutation_outcome(final_text, objective), objective.mutation_summary),
        ("primary_objective.graph_summary", _mentions_graph_outcome(final_text, objective), objective.graph_summary),
        (
            "primary_objective.related_notes_summary",
            _mentions_related_notes_outcome(final_text, objective),
            objective.related_notes_summary,
        ),
    )
    findings: list[AgentRunReportFinding] = []
    for source_field, present, expected_summary in checks:
        if present:
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PRIMARY_OBJECTIVE_OMITTED,
                severity="high",
                source="final_report",
                source_field=source_field,
                expected=expected_summary,
                actual="omitted",
                message="O relatório final não respondeu uma pergunta obrigatória do objetivo primário do fix-wiki.",
                next_action=(
                    "Reescrever o relatório final respondendo: fixou a Wiki, o que mutou, "
                    "se o grafo melhorou e se Notas Relacionadas foi atualizado ou ficou pendente."
                ),
            )
        )
    return findings


def _primary_objective_success_claim_findings(
    final_text: str,
    objective: PrimaryObjectiveSummary,
) -> list[AgentRunReportFinding]:
    if isinstance(objective, WorkflowPrimaryObjectiveSummary):
        if objective.completed or not _has_positive_success_claim(final_text):
            return []
        return [
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.SUCCESS_CLAIM_MISMATCH,
                severity="medium",
                source="final_report",
                source_field="primary_objective.completed",
                expected="completed=false",
                actual="success_claim",
                message="O relatório final declarou sucesso para um objetivo primário que a FSM ainda não concluiu.",
                next_action="Trocar sucesso simples por prévia, espera, bloqueio ou etapa pendente conforme primary_objective_summary.",
            )
        ]
    if not isinstance(objective, ProcessChatsPrimaryObjectiveSummary):
        return []
    if objective.process_status != "completed_with_link_blockers":
        return []
    if not _has_positive_success_claim(final_text):
        return []
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.SUCCESS_CLAIM_MISMATCH,
            severity="medium",
            source="final_report",
            source_field="primary_objective.process_status",
            expected=objective.process_status,
            actual="success_claim",
            message="O relatório final usou linguagem de sucesso para process-chats com linker/grafo pendente.",
            next_action="Trocar sucesso simples por publicação concluída com pendência explícita de linker/grafo.",
        )
    ]


def _generic_primary_objective_omission_findings(
    final_text: str,
    objective: WorkflowPrimaryObjectiveSummary,
) -> list[AgentRunReportFinding]:
    folded = _fold_text(final_text)
    checks = (
        ("primary_objective.objective_status", _mentions_generic_objective_status(folded, objective), objective.status),
        (
            "primary_objective.mutation_summary",
            _mentions_summary_fragment(folded, objective.mutation_summary),
            objective.mutation_summary,
        ),
        (
            "primary_objective.remaining_work_summary",
            _mentions_summary_fragment(folded, objective.remaining_work_summary),
            objective.remaining_work_summary,
        ),
        (
            "primary_objective.next_step_summary",
            _mentions_summary_fragment(folded, objective.next_step_summary),
            objective.next_step_summary,
        ),
    )
    findings: list[AgentRunReportFinding] = []
    for source_field, present, expected_summary in checks:
        if present:
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PRIMARY_OBJECTIVE_OMITTED,
                severity="high",
                source="final_report",
                source_field=source_field,
                expected=expected_summary,
                actual="omitted",
                message="O relatório final não respondeu uma pergunta obrigatória do objetivo primário do workflow.",
                next_action="Reescrever o relatório final usando reports.details.primary_objective_summary.",
            )
        )
    return findings


def _mentions_generic_objective_status(
    folded_text: str,
    objective: WorkflowPrimaryObjectiveSummary,
) -> bool:
    if objective.status in folded_text:
        return True
    answer = _generic_public_objective_answer(objective)
    markers = NON_SUCCESS_HUMAN_STATUS_MARKERS.get(answer, ())
    return _folded_contains_any(folded_text, markers)


def _mentions_summary_fragment(folded_text: str, summary: str) -> bool:
    words = [word for word in _fold_text(summary).split() if len(word) >= 5]
    if not words:
        return False
    return sum(1 for word in words[:8] if word in folded_text) >= min(2, len(words))


def _process_chats_primary_objective_omission_findings(
    final_text: str,
    objective: ProcessChatsPrimaryObjectiveSummary,
) -> list[AgentRunReportFinding]:
    checks = (
        (
            "primary_objective.process_status",
            _mentions_process_chats_status(final_text, objective),
            objective.process_summary,
        ),
        ("primary_objective.raw_summary", _mentions_process_chats_raw(final_text, objective), objective.raw_summary),
        (
            "primary_objective.wiki_write_summary",
            _mentions_process_chats_wiki_write(final_text, objective),
            objective.wiki_write_summary,
        ),
        (
            "primary_objective.coverage_summary",
            _mentions_process_chats_coverage(final_text),
            objective.coverage_summary,
        ),
        (
            "primary_objective.linker_summary",
            _mentions_process_chats_linker(final_text, objective),
            objective.linker_summary,
        ),
    )
    findings: list[AgentRunReportFinding] = []
    for source_field, present, expected_summary in checks:
        if present:
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.PRIMARY_OBJECTIVE_OMITTED,
                severity="high",
                source="final_report",
                source_field=source_field,
                expected=expected_summary,
                actual="omitted",
                message="O relatório final não respondeu uma pergunta obrigatória do objetivo primário do process-chats.",
                next_action=(
                    "Reescrever o relatório final respondendo: se publicou ou só preparou prévia, "
                    "quais raws foram cobertos/processados, o que foi escrito na Wiki, "
                    "se coverage/manifest bateram e qual foi o estado do linker/grafo."
                ),
            )
        )
    return findings


def _mentions_wiki_outcome(final_text: str) -> bool:
    folded = _fold_text(final_text)
    return "wiki" in folded and any(marker in folded for marker in ("fixou", "corrig", "parcial", "pendente", "nao"))


def _mentions_mutation_outcome(final_text: str, objective: FixWikiPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if not any(marker in folded for marker in ("mutacao", "alterad", "modificad", "grav", "mudanca", "mudancas")):
        return False
    if objective.mutation_count == 0:
        return any(marker in folded for marker in (" 0 ", ": 0", "0 arquivo", "nenhum", "nada"))
    if str(objective.mutation_count) not in folded:
        return False
    if objective.written_count and objective.written_count != objective.mutation_count:
        return str(objective.written_count) in folded and any(
            marker in folded for marker in ("grav", "salv", "escrit", "workflow")
        )
    return True


def _mentions_graph_outcome(final_text: str, objective: FixWikiPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if "grafo" not in folded and "graph" not in folded:
        return False
    match objective.graph_status:
        case "clean":
            return any(
                marker in folded
                for marker in (
                    "limpo",
                    "sem bloqueio",
                    "sem blockers",
                    "sem erro",
                    "sem comparacao",
                    "sem comparação",
                    "grafo limpo",
                    "graph clean",
                    "terminou sem bloqueios",
                    "terminou sem erros",
                )
            )
        case "improved":
            return any(marker in folded for marker in ("melhor", "reduz", "corrig"))
        case "blocked":
            return any(marker in folded for marker in ("bloque", "pendente", "erro"))
        case "unchanged":
            return any(marker in folded for marker in ("nao melhorou", "não melhorou", "permaneceu", "inalter"))
        case "worse":
            return any(marker in folded for marker in ("pior", "regred"))
        case "unknown":
            return any(marker in folded for marker in ("sem comparacao", "sem comparação", "nao confirmou", "não confirmou"))


def _mentions_related_notes_outcome(final_text: str, objective: FixWikiPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if "related notes" not in folded and "notas relacionadas" not in folded:
        return False
    if objective.related_notes_status == "pending" and "cota" in _fold_text(objective.related_notes_summary):
        return "cota" in folded or "quota" in folded
    if objective.related_notes_status == "updated" and any(
        marker in folded
        for marker in (
            "convergencia total esta pendente",
            "convergencia pendente",
            "pendente da aplicacao",
            "pendente de aplicacao",
            "ficou pendente",
            "estao pendentes",
            "está pendente",
        )
    ):
        return False
    return True


def _mentions_process_chats_status(final_text: str, objective: ProcessChatsPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if objective.process_status in {"preview_ready", "ready_to_publish"}:
        return any(marker in folded for marker in ("previa", "preview", "pronta", "ready_to_publish"))
    return any(marker in folded for marker in ("publicacao", "publicou", "publicad", "process-chats"))


def _mentions_process_chats_raw(final_text: str, objective: ProcessChatsPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if not any(marker in folded for marker in ("raw", "chat", "chats")):
        return False
    if objective.raw_count == 0:
        return any(marker in folded for marker in ("0", "nenhum", "nao processad", "ainda nao"))
    return str(objective.raw_count) in folded


def _mentions_process_chats_wiki_write(final_text: str, objective: ProcessChatsPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if "wiki" not in folded:
        return False
    if not any(marker in folded for marker in ("arquivo", "nota", "escrit", "grav", "publicad")):
        return False
    if objective.note_count == 0:
        return any(marker in folded for marker in ("0", "nenhum", "nada", "ainda nao"))
    return str(objective.note_count) in folded


def _mentions_process_chats_coverage(final_text: str) -> bool:
    folded = _fold_text(final_text)
    return ("coverage" in folded or "cobertura" in folded) and "manifest" in folded


def _mentions_process_chats_linker(final_text: str, objective: ProcessChatsPrimaryObjectiveSummary) -> bool:
    folded = _fold_text(final_text)
    if not any(marker in folded for marker in ("linker", "grafo", "related notes", "notas relacionadas")):
        return False
    if objective.linker_status == "blocked":
        return any(marker in folded for marker in ("pendente", "bloque", "blocker", "nao aplicado"))
    if objective.linker_status == "not_run":
        return any(marker in folded for marker in ("nao rodou", "ainda nao", "nao foi confirmad", "publicacao nao"))
    return True


def _fold_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    without_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return f" {without_marks.casefold()} "


def _omitted_tool_error_findings(transcript: object, final_text: str) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    tool_errors = [
        finding
        for finding in validate_agent_tool_calls(transcript)
        if str(finding.get("code") or "") == TOOL_CALL_ERROR
    ]
    if not tool_errors:
        return []
    for error in tool_errors:
        if _final_report_mentions_tool_error(final_text, error):
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.OMITTED_TOOL_ERROR,
                severity=_finding_severity(error.get("severity")),
                source="transcript",
                source_field="tool_error",
                tool_error_type=str(error.get("error_type") or ""),
                message="O transcript contém tool call falha que o relatório final não reportou.",
                next_action="Reportar explicitamente a tool call falha e seu impacto, mesmo quando um retry posterior recuperar.",
                evidence={
                    "tool_type": str(error.get("tool_type") or ""),
                    "tool_error_message": str(error.get("message") or ""),
                },
            )
        )
    return findings


def _finding_severity(value: object) -> AgentRunReportSeverity:
    text = str(value or "medium").strip().lower()
    if text in {"low", "medium", "high", "critical"}:
        return cast(AgentRunReportSeverity, text)
    return "medium"


def _final_report_mentions_tool_error(final_text: str, error: JsonObject) -> bool:
    lowered = final_text.lower()
    if "tool" not in lowered:
        return False
    error_type = str(error.get("error_type") or "").lower()
    if error_type and error_type in lowered:
        return True
    return any(marker in lowered for marker in ("erro", "falh", "failed", "invalid tool", "invalid_tool"))


def _omitted_tool_deviation_findings(transcript: object, final_text: str) -> list[AgentRunReportFinding]:
    deviations, finding_codes = _transcript_tool_deviation_context(transcript)
    if not deviations:
        return []
    if _final_report_mentions_tool_deviations(final_text, deviations):
        return []
    no_deviation_claim = bool(final_text and NO_TOOL_DEVIATION_CLAIM_RE.search(final_text))
    return [
        AgentRunReportFinding(
            code=AgentRunReportFindingCode.TOOL_DEVIATION_OMITTED,
            severity="high",
            source="transcript",
            source_field="final_report_text",
            expected="relatório final deve listar probes, permissões e comandos fora do roteiro quando ocorrerem",
            actual="no_deviations_claim" if no_deviation_claim else ",".join(deviations),
            message=(
                "O relatório final afirmou que não houve desvios, mas o transcript contém probes ou tool calls fora do roteiro."
                if no_deviation_claim
                else "O relatório final omitiu probes ou tool calls fora do roteiro presentes no transcript."
            ),
            next_action=(
                "Reescrever a seção de avisos de execução listando os probes/tool calls observados "
                "e o impacto deles no experimento."
            ),
            evidence=_tool_deviation_evidence(deviations=deviations, finding_codes=finding_codes),
        )
    ]


def _tool_deviation_evidence(*, deviations: list[str], finding_codes: list[str]) -> JsonObject:
    evidence: JsonObject = {"tool_types": deviations}
    if finding_codes:
        evidence["finding_codes"] = finding_codes
    return evidence


def _update_topic_success_claim_findings(
    transcript: object,
    truth: _WorkflowTruth,
) -> list[AgentRunReportFinding]:
    status = truth.workflow_status or truth.progress_status or truth.receipt_status
    if status not in NON_SUCCESS_STATUSES:
        return []
    findings: list[AgentRunReportFinding] = []
    for event in _iter_transcript_events(transcript):
        if event.event_type.casefold() != "tool_use" or event.tool_name.casefold() != "update_topic":
            continue
        text = "\n".join(
            str(event.parameters.get(field) or "")
            for field in ("title", "summary", "strategic_intent")
        )
        if not _has_positive_success_claim(text):
            continue
        if _update_topic_acknowledges_partial_workflow(text):
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.SUCCESS_CLAIM_MISMATCH,
                severity="medium",
                source="transcript",
                source_field="transcript.tool_use.update_topic",
                expected=f"update_topic deve comunicar estado parcial/pendente quando workflow_status={status}",
                actual="success_claim",
                message="O update_topic usou linguagem de sucesso apesar de o workflow ainda estar parcial ou bloqueado.",
                next_action=(
                    "Atualizar a comunicação pública para dizer o que foi aplicado e o que ainda falta, "
                    "sem chamar o workflow parcial de sucesso."
                ),
                evidence={"workflow_status": status, "text": text},
            )
        )
    return findings


def _update_topic_acknowledges_partial_workflow(text: str) -> bool:
    folded = _fold_text(text)
    return any(
        marker in folded
        for marker in (
            "parcial",
            "pendente",
            "aguard",
            "waiting",
            "bloque",
            "falta",
            "restam",
            "nao conclu",
            "não conclu",
            "nao fixou",
            "não fixou",
        )
    )


def _transcript_tool_deviation_context(transcript: object) -> tuple[list[str], list[str]]:
    probe_types: list[str] = []
    finding_codes: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        event_type = str(value.get("type") or "").upper()
        tool_name = str(value.get("name") or value.get("tool_name") or "").strip()
        if event_type in {"VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH"}:
            if event_type == "VIEW_FILE":
                if _is_expected_workflow_skill_read(value) or _is_expected_cpu_sample_read(value):
                    return
                if _is_agy_background_task_log_read(value):
                    _append_unique(probe_types, "AGY_BACKGROUND_TASK_LOG")
                    return
            _append_unique(probe_types, event_type)
        if event_type == "GENERIC" and tool_name == "list_permissions":
            _append_unique(probe_types, "GENERIC:list_permissions")
        for child in _transcript_child_containers(value):
            visit(child)

    visit(transcript)
    for finding in validate_agent_tool_calls(transcript):
        code = str(finding.get("code") or "")
        if not code or code == TOOL_CALL_ERROR:
            continue
        if code == PUBLIC_TOOL_TEXT_CONTRACT_VIOLATION:
            continue
        _append_unique(finding_codes, code)
        tool_name = str(finding.get("tool_name") or code)
        _append_unique(probe_types, tool_name)
    return probe_types, finding_codes


def _is_expected_workflow_skill_read(event: JsonObject) -> bool:
    normalized = _transcript_event_file_path(event).replace("\\", "/")
    if not normalized.endswith("/SKILL.md"):
        return False
    return any(
        marker in normalized
        for marker in (
            "/mednotes-fix-wiki/SKILL.md",
            "/fix-medical-wiki/SKILL.md",
            "/obsidian-ops/SKILL.md",
            f"/{SKILLS_RELPATH}/fix-medical-wiki/SKILL.md",
        )
    )


def _is_expected_cpu_sample_read(event: JsonObject) -> bool:
    normalized = _transcript_event_file_path(event).replace("\\", "/")
    return normalized.endswith("/cpu-samples.jsonl")


def _is_agy_background_task_log_read(event: JsonObject) -> bool:
    normalized = _transcript_event_file_path(event).replace("\\", "/")
    return "/.gemini/antigravity-cli/brain/" in normalized and "/.system_generated/tasks/task-" in normalized and normalized.endswith(".log")


def _transcript_event_file_path(event: JsonObject) -> str:
    path_from_parameters = ""
    parameters = event.get("parameters")
    if isinstance(parameters, dict):
        args = parameters.get("args")
        if isinstance(args, dict):
            path_from_parameters = str(args.get("path") or args.get("file_path") or "")
    path_from_content = _tool_content_file_path(str(event.get("content") or ""))
    return str(
        event.get("path")
        or event.get("file_path")
        or path_from_parameters
        or path_from_content
        or ""
    )


def _tool_content_file_path(content: str) -> str:
    match = TOOL_CONTENT_FILE_PATH_RE.search(content)
    if match is None:
        return ""
    return unquote(match.group("path"))


def _final_report_mentions_tool_deviations(final_text: str, deviations: list[str]) -> bool:
    folded = _fold_text(final_text)
    if not folded:
        return False
    for deviation in deviations:
        token = _fold_text(deviation)
        if token in folded:
            continue
        if deviation == "VIEW_FILE" and any(marker in folded for marker in ("view_file", "leu skill", "leitura de skill", "read file")):
            continue
        if deviation == "AGY_BACKGROUND_TASK_LOG" and any(
            marker in folded
            for marker in (
                "agy background fallback",
                "task log",
                "background task",
                "fallback de background",
                "log indicado pela ferramenta",
                "log indicado pela propria ferramenta",
                "log indicado pela própria ferramenta",
                "registro indicado pela ferramenta",
                "registro da ferramenta",
                "execucao em segundo plano",
                "execução em segundo plano",
                "segundo plano",
                "registro temporario de progresso",
                "registro temporário de progresso",
            )
        ):
            continue
        if deviation == "LIST_DIRECTORY" and any(marker in folded for marker in ("list_directory", "listou diretorio", "listagem de diretorio")):
            continue
        if deviation == "GREP_SEARCH" and any(marker in folded for marker in ("grep_search", "grep", "busca textual")):
            continue
        return False
    return True


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _final_report_local_path_leak_findings(final_text: str) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    for path in _reported_absolute_paths(final_text):
        if not _looks_like_local_path_leak(path):
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.FINAL_REPORT_LOCAL_PATH_LEAK,
                severity="medium",
                source="final_report",
                source_field="final_report_text",
                path=path,
                artifact_name=path.replace("\\", "/").rsplit("/", 1)[-1],
                expected="resposta pública sem links file:// nem caminhos locais absolutos",
                actual=path,
                message="O relatório final expôs um caminho local da máquina no texto público.",
                next_action=(
                    "Trocar o caminho local por uma descrição humana do item afetado ou por referência técnica "
                    "apenas no log/JSON do experimento."
                ),
                evidence={"path": path},
            )
        )
    return findings


def _looks_like_local_path_leak(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("/mednotes:", "/flashcards")):
        return False
    return normalized.startswith(("/Users/", "/tmp/", "/private/tmp/", "/private/var/"))


def _invalid_reported_artifact_path_findings(final_text: str) -> list[AgentRunReportFinding]:
    findings: list[AgentRunReportFinding] = []
    for path in _reported_absolute_paths(final_text):
        if not _looks_like_reported_artifact_path(path):
            continue
        if Path(path).exists():
            continue
        findings.append(
            AgentRunReportFinding(
                code=AgentRunReportFindingCode.REPORTED_ARTIFACT_PATH_INVALID,
                severity="medium",
                source="filesystem",
                source_field="final_report_text",
                path=path,
                artifact_name=path.replace("\\", "/").rsplit("/", 1)[-1],
                message="O relatório final citou caminho de artefato ou backup que não existe no filesystem.",
                next_action="Remover o caminho inventado ou substituir pelo caminho oficial existente antes de concluir a rodada.",
            )
        )
    return findings


def _reported_absolute_paths(final_text: str) -> list[str]:
    paths: list[str] = []
    for pattern in (BACKTICK_ABSOLUTE_PATH_RE, FILE_URI_RE, PLAIN_ABSOLUTE_PATH_RE):
        for match in pattern.finditer(final_text):
            raw_path = _normalize_reported_path_candidate(unquote(match.group("path")).rstrip(".,;"))
            if raw_path and raw_path not in paths:
                paths.append(raw_path)
    return paths


def _normalize_reported_path_candidate(raw_path: str) -> str:
    stripped = raw_path.strip()
    for separator in ("\n", "\r"):
        if separator in stripped:
            stripped = stripped.split(separator, 1)[0].strip()
    if stripped.startswith(("/mednotes:", "/flashcards")):
        return stripped.split(maxsplit=1)[0]
    if stripped.endswith(")") and not Path(stripped).exists():
        markdown_link_candidate = stripped[:-1]
        if Path(markdown_link_candidate).exists() or _looks_like_reported_artifact_path(markdown_link_candidate):
            stripped = markdown_link_candidate
    return stripped


def _looks_like_reported_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("/mednotes:", "/flashcards")):
        return False
    name = normalized.rsplit("/", 1)[-1]
    if name.endswith((".json", ".md", ".bak", ".log")):
        return True
    return any(
        marker in normalized
        for marker in (
            "/runs/",
            "/workflow-",
            "fix-wiki",
            "link-diagnosis",
            "run_state",
            "compact-report",
            "full-report",
        )
    )
