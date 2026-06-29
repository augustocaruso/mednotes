"""Domain-agnostic public report contract for FSM-first workflows.

The FSM owns the state and emits one human-visible wording channel under
``reports.public_report``. Adapters, hooks and agents may display this model,
but they must not infer workflow state from legacy text fields.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, field_validator

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.progress import WorkflowProgressState, WorkflowProgressStatus, WorkflowProgressViewModel

_PUBLIC_REPORT_INTERNAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"--json\b",
        r"--dry-run\b",
        r"\bdry-run\b",
        r"\bschema\b",
        r"\breceipt\b",
        r"\brecibo\b",
        r"sha256:",
        r"(?<!\w)hash(?!\w)",
        r"/Users/",
        r"~/\.[\w-]+",
        r"\b[\w./-]+/cli\.py\b",
        r"\b[\w:/.-]+\s+--[\w-]+",
        r"decision\.reason_code",
        r"human_decision_packet",
        r"next_action",
        r"run_id",
        r"guard_lease",
    )
)


def _public_report_text_without_internal_terms(value: str, *, field_name: str) -> str:
    """Keep the human-visible channel free of automation/debug vocabulary."""

    for pattern in _PUBLIC_REPORT_INTERNAL_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"public report {field_name} contains internal term")
    return value


class WorkflowPublicReport(ContractModel):
    """Canonical human-visible text embedded under ``reports.public_report``.

    This object intentionally carries no status, phase, mutation count or
    decision flags. Those facts belong to the FSM snapshot, progress model,
    receipt and agent directive; duplicating them here creates a second source
    of truth for human and agent consumers.
    """

    schema_: Literal["workflow.public-report.v1"] = Field(
        "workflow.public-report.v1",
        alias="schema",
    )
    audience: Literal["user"] = "user"
    workflow: StrictStr = Field(min_length=1)
    run_id: StrictStr = Field(min_length=1)
    headline: StrictStr = Field(min_length=1)
    lines: list[StrictStr] = Field(min_length=1)

    @field_validator("headline", mode="after")
    @classmethod
    def _headline_must_be_clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("public report headline must be non-empty")
        return cleaned

    @field_validator("lines", mode="after")
    @classmethod
    def _lines_must_be_clean(cls, value: list[str]) -> list[str]:
        cleaned = [line.strip() for line in value if line.strip()]
        if not cleaned:
            raise ValueError("public report lines must contain visible text")
        return cleaned

    def summary_text(self) -> str:
        """Return the exact text agents may reuse without rephrasing."""

        return "\n".join(self.lines)


def assert_public_report_has_no_internal_terms(public_report: WorkflowPublicReport) -> None:
    """Reject default public text that leaks automation/debug vocabulary."""

    _public_report_text_without_internal_terms(public_report.headline, field_name="headline")
    for line in public_report.lines:
        _public_report_text_without_internal_terms(line, field_name="line")


class WorkflowReports(ContractModel):
    """Shared reports envelope for FSM-first workflows.

    ``summary`` and ``public_report`` are the common reporting surface. Domain
    projections that need structured evidence for validators can place it in
    ``details`` without teaching renderers to infer workflow state from it.
    """

    summary: StrictStr = Field(min_length=1)
    public_report: WorkflowPublicReport
    details: JsonObject = Field(default_factory=dict)


class WorkflowPrimaryObjectiveSummary(ContractModel):
    """Minimum structured answer to a workflow's user-visible objective.

    FSM projections place this under
    ``reports.details.primary_objective_summary`` so validators can tell the
    difference between "work completed", "preview prepared", and "blocked"
    without parsing human prose or legacy root fields.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_: StrictStr = Field(
        "workflow.primary-objective-summary.v1",
        alias="schema",
    )
    workflow: StrictStr = Field(min_length=1)
    run_id: StrictStr = Field(min_length=1)
    objective: StrictStr = Field(min_length=1)
    completed: StrictBool
    status: StrictStr = Field(min_length=1)
    mutation_state: Literal["changed", "unchanged", "not_applicable"]
    mutation_summary: StrictStr = Field(min_length=1)
    remaining_work_summary: StrictStr = Field(min_length=1)
    next_step_summary: StrictStr = Field(min_length=1)
    blocked_reason: StrictStr = ""
    required_report_items: list[StrictStr] = Field(
        default_factory=lambda: [
            "objective_status",
            "mutation_summary",
            "remaining_work_summary",
            "next_step_summary",
        ]
    )


class FsmFirstPayloadSummary(ContractModel):
    """Typed feedback/telemetry lens over the canonical FSM payload roots.

    Feedback records are observability artifacts, not workflow controllers. This
    model lets adapters summarize FSM-first payloads without letting stale
    legacy root fields such as ``status`` or ``next_action`` become a second
    source of truth.
    """

    status: StrictStr = ""
    phase: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    required_inputs: list[StrictStr] = Field(default_factory=list)
    human_decision_required: StrictBool = False

    @classmethod
    def from_payload(cls, payload: JsonObject) -> FsmFirstPayloadSummary:
        progress = _object_field(payload, "progress_view_model")
        snapshot = _object_field(payload, "state_machine_snapshot")
        decision = _object_field(payload, "decision")
        receipt = _object_field(payload, "receipt")
        error_context = _object_field(payload, "error_context")
        human_packet = _object_field(payload, "human_decision_packet")
        human_packets = _array_field(payload, "human_decision_packets")
        required_inputs = (
            _string_list_field(decision, "required_inputs")
            or _string_list_field(receipt, "required_inputs")
            or _string_list_field(error_context, "required_inputs")
        )
        return cls(
            status=_text_field(progress, "status")
            or _text_field(receipt, "status")
            or _text_field(snapshot, "current_category"),
            phase=_text_field(progress, "phase")
            or _text_field(progress, "state")
            or _text_field(snapshot, "current_state")
            or _text_field(snapshot, "current_category"),
            blocked_reason=_text_field(error_context, "blocked_reason")
            or _text_field(error_context, "root_cause")
            or _text_field(decision, "reason_code"),
            next_action=_text_field(decision, "next_action")
            or _text_field(receipt, "next_action")
            or _text_field(progress, "user_action")
            or _text_field(progress, "resume_action"),
            required_inputs=required_inputs,
            human_decision_required=(
                _text_field(decision, "kind") == "ask_human"
                or bool(human_packet)
                or bool(human_packets)
            ),
        )


def _object_field(source: JsonObject, key: str) -> JsonObject:
    value = source[key] if key in source else {}
    return value if isinstance(value, dict) else {}


def _array_field(source: JsonObject, key: str) -> list[object]:
    value = source[key] if key in source else []
    return value if isinstance(value, list) else []


def _text_field(source: JsonObject, key: str) -> str:
    value = source[key] if key in source else ""
    return str(value).strip() if value is not None else ""


def _string_list_field(source: JsonObject, key: str) -> list[str]:
    value = source[key] if key in source else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class WorkflowPublicDecisionOption(ContractModel):
    """Human-facing closed option rendered from a technical decision packet."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    description: StrictStr = ""


class _HumanDecisionPacketForPublicReport(ContractModel):
    """Narrow lens over ``human_decision_packet`` for public rendering only."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    question: StrictStr = Field(min_length=1)
    options: list[WorkflowPublicDecisionOption] = Field(min_length=1)
    items: list[JsonObject] = Field(default_factory=list)
    resume_action: StrictStr = Field(min_length=1)


class WorkflowPublicDecisionPrompt(ContractModel):
    """Question/options view shown to the user instead of raw decision fields."""

    question: StrictStr = Field(min_length=1)
    options: list[WorkflowPublicDecisionOption] = Field(min_length=1)
    affected_items: list[StrictStr] = Field(default_factory=list)
    resume_summary: StrictStr = Field(min_length=1)

    def summary_lines(self) -> list[str]:
        """Return readable lines without leaking packet field names."""

        lines = [self.question]
        lines.extend(f"- {option.label}" for option in self.options)
        if self.affected_items:
            lines.append("Itens afetados: " + ", ".join(self.affected_items))
        lines.append(self.resume_summary)
        return lines


def public_decision_prompt_from_packet(packet: JsonObject) -> WorkflowPublicDecisionPrompt:
    """Render a typed human-decision packet as public question/options text."""

    fields = _HumanDecisionPacketForPublicReport.model_validate(packet)
    affected_items: list[str] = []
    for item in fields.items:
        source = item.get("source")
        if isinstance(source, str) and source.strip():
            affected_items.append(source.strip())
    return WorkflowPublicDecisionPrompt(
        question=fields.question.strip(),
        options=fields.options,
        affected_items=affected_items,
        resume_summary=fields.resume_action.strip(),
    )


_INCOMPLETE_STATUSES = {
    WorkflowProgressStatus.RUNNING,
    WorkflowProgressStatus.WAITING_AGENT,
    WorkflowProgressStatus.WAITING_EXTERNAL,
    WorkflowProgressStatus.WAITING_HUMAN,
    WorkflowProgressStatus.BLOCKED,
    WorkflowProgressStatus.FAILED,
}
_SUCCESS_ONLY_PUBLIC_MARKERS = (
    "workflow concluido",
    "workflow concluído",
    "concluido com sucesso",
    "concluído com sucesso",
    "conclui com sucesso",
    "concluí com sucesso",
    "nao encontrei bloqueios",
    "não encontrei bloqueios",
    "nenhum bloqueio tecnico restante",
    "nenhum bloqueio técnico restante",
    "sem bloqueios",
    "nada pendente",
)


def assert_public_report_matches_progress(
    public_report: WorkflowPublicReport,
    *,
    workflow: str,
    run_id: str,
    progress_view_model: WorkflowProgressViewModel,
    label: str,
) -> None:
    """Reject human-facing text that contradicts the FSM progress contract."""

    if public_report.workflow != workflow or public_report.run_id != run_id:
        raise ValueError(f"{label} reports.public_report must match workflow and run_id")
    if progress_view_model.workflow != workflow or progress_view_model.run_id != run_id:
        raise ValueError(f"{label} progress_view_model must match workflow and run_id")
    assert_public_report_has_no_internal_terms(public_report)
    if progress_view_model.status not in _INCOMPLETE_STATUSES:
        return
    public_text = f"{public_report.headline}\n{public_report.summary_text()}".casefold()
    for marker in _SUCCESS_ONLY_PUBLIC_MARKERS:
        if marker in public_text:
            raise ValueError(f"{label} reports.public_report contradicts incomplete FSM status")


def public_progress_followup_line(progress: WorkflowProgressState | WorkflowProgressViewModel) -> str:
    """Render a safe user-facing next-step line from canonical progress state."""

    match progress.status:
        case WorkflowProgressStatus.RUNNING:
            return "O workflow ainda está em andamento."
        case WorkflowProgressStatus.WAITING_AGENT:
            if progress.can_continue_now:
                return "Vou continuar pela rota oficial antes do relatório final."
            return "Há uma etapa do agente pendente antes de concluir."
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "Aguardando uma condição externa antes de retomar com segurança."
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "Preciso da sua escolha antes de continuar com segurança."
        case WorkflowProgressStatus.BLOCKED:
            return "Há um bloqueio que precisa ser resolvido antes de continuar."
        case WorkflowProgressStatus.FAILED:
            return "A execução falhou; vou usar o contexto técnico para orientar a recuperação."
        case _:
            return ""
