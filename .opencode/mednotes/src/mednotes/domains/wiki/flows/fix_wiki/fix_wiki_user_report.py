"""Human-readable redacted report for the `fix-wiki` workflow."""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_fsm import assert_fix_wiki_fsm_payload
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_primary_objective import fix_wiki_primary_objective_summary
from mednotes.kernel.base import JsonArrayAdapter, JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.public_report import WorkflowPublicReport

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "": 5}
_TRANSIENT_MUTATION_LINE_RE = re.compile(r"\balterei\s+\d+\s+arquivo", re.IGNORECASE)
_PUBLIC_GRAPH_CURATION_ACTION = "Retomar a curadoria do grafo pela rota oficial antes de concluir a atualização de links."
_PUBLIC_HUMAN_DECISION_RESUME = "Após escolher uma opção, retome pelo fluxo oficial do /mednotes:fix-wiki."
_TECHNICAL_GRAPH_CURATION_MARKERS = (
    "med-link-graph-curator",
    "collect-curator-outputs",
    "eval-curator-batch",
    "apply-curator-batch",
    "vocabulary-curator-batch-plan",
)


class _FixWikiUserReportFieldModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class _FixWikiUserReportProjectionModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)


class _FsmProgressDisplayFields(_FixWikiUserReportProjectionModel):
    status: StrictStr = ""
    headline: StrictStr = ""
    message: StrictStr = ""
    detail: StrictStr = ""
    count_label: StrictStr = ""
    user_action: StrictStr = ""
    resume_action: StrictStr = ""
    can_continue_now: StrictBool | None = None


class _FsmSnapshotDisplayFields(_FixWikiUserReportProjectionModel):
    current_state: StrictStr = ""


class _AgentCapabilitiesFields(_FixWikiUserReportProjectionModel):
    continue_: StrictBool = Field(default=False, alias="continue")


class _AgentControlFields(_FixWikiUserReportProjectionModel):
    status: StrictStr = ""
    capabilities: _AgentCapabilitiesFields = Field(default_factory=_AgentCapabilitiesFields)
    effects: list[JsonValue] = Field(default_factory=list)


class _AgentDirectiveFields(_FixWikiUserReportProjectionModel):
    control: _AgentControlFields = Field(default_factory=_AgentControlFields)


class _FsmReportsDisplayFields(_FixWikiUserReportProjectionModel):
    summary: StrictStr = ""
    public_report: WorkflowPublicReport


class _FixWikiFsmReportRootFields(_FixWikiUserReportProjectionModel):
    """Typed projection of the canonical FSM payload used by the user renderer."""

    progress_view_model: JsonObject
    state_machine_snapshot: JsonObject
    reports: _FsmReportsDisplayFields
    receipt: JsonObject
    agent_directive: JsonObject
    human_decision_packet: JsonObject | None = None


class _DecisionOptionFields(_FixWikiUserReportFieldModel):
    id: StrictStr = ""
    label: StrictStr = ""


class _RejectedAutomationFields(_FixWikiUserReportFieldModel):
    kind: StrictStr = ""
    reason: StrictStr = ""
    reason_code: StrictStr = ""


class _HumanDecisionPacketFields(_FixWikiUserReportFieldModel):
    question: StrictStr = ""
    public_summary: StrictStr = ""
    options: list[_DecisionOptionFields] = Field(default_factory=list)
    rejected_automations: list[_RejectedAutomationFields] = Field(default_factory=list)
    resume_action: StrictStr = ""


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value)


def _json_array(value: object) -> list[JsonValue]:
    return JsonArrayAdapter.validate_python(value)


def _field_payload(source: JsonObject, field_names: tuple[str, ...]) -> JsonObject:
    payload: JsonObject = {}
    for field_name in field_names:
        if field_name in source:
            payload[field_name] = source[field_name]
    return payload


def _json_array_field(source: JsonObject, field_name: str) -> list[JsonValue]:
    if field_name not in source:
        return []
    return _json_array(source[field_name])


def _object_field_payload(source: JsonObject, field_name: str, field_names: tuple[str, ...]) -> JsonObject:
    if field_name not in source:
        return {}
    return _field_payload(_json_object(source[field_name]), field_names)


def _decision_options(source: JsonObject) -> list[_DecisionOptionFields]:
    return [
        _DecisionOptionFields.model_validate(_field_payload(_json_object(item), ("id", "label")))
        for item in _json_array_field(source, "options")
    ]


def _rejected_automations(source: JsonObject) -> list[_RejectedAutomationFields]:
    return [
        _RejectedAutomationFields.model_validate(_field_payload(_json_object(item), ("kind", "reason", "reason_code")))
        for item in _json_array_field(source, "rejected_automations")
    ]


def _human_decision_packet_fields(source: JsonObject) -> _HumanDecisionPacketFields:
    payload = _field_payload(source, ("question", "public_summary", "resume_action"))
    payload["options"] = _decision_options(source)
    payload["rejected_automations"] = _rejected_automations(source)
    return _HumanDecisionPacketFields.model_validate(payload)


def _human_decision_packet_has_data(packet: _HumanDecisionPacketFields) -> bool:
    return bool(
        packet.question
        or packet.public_summary
        or packet.options
        or packet.rejected_automations
        or packet.resume_action
    )


def render_fix_wiki_user_report(report: object) -> str:
    """Render only the canonical FSM payload; legacy root reports are rejected."""

    payload = _json_object(report)
    schema = payload["schema"] if "schema" in payload else ""
    if schema != "medical-notes-workbench.fix-wiki-fsm-result.v1":
        raise ValueError("fix-wiki user report requires the canonical FSM result payload")
    assert_fix_wiki_fsm_payload(payload)
    return _render_fix_wiki_fsm_report(payload)


def write_fix_wiki_user_report_v2(path: Path, payload: object) -> None:
    """Write a compact human report without raw note bodies or textual diffs."""

    atomic_write_text(path, render_fix_wiki_user_report(payload))


def _render_fix_wiki_fsm_report(report: JsonObject) -> str:
    root = _FixWikiFsmReportRootFields.model_validate(report)
    progress = _FsmProgressDisplayFields.model_validate(root.progress_view_model)
    snapshot = _FsmSnapshotDisplayFields.model_validate(root.state_machine_snapshot)
    public_report = root.reports.public_report
    agent_directive = _AgentDirectiveFields.model_validate(root.agent_directive)
    control = agent_directive.control
    can_continue_without_human = (
        control.status == "waiting_agent"
        and control.capabilities.continue_ is True
        and bool(control.effects)
    )
    public_lines = [
        _public_safe_report_line(_clean(line))
        for line in public_report.lines
        if line.strip()
    ]
    if not can_continue_without_human:
        public_lines = [line for line in public_lines if not _mentions_automatic_continuation(line)]
    reports_summary = _clean(root.reports.summary)
    summary = _clean(public_report.headline or reports_summary or progress.headline or progress.message or "Conferência concluída.")
    if not can_continue_without_human and _mentions_automatic_continuation(summary):
        summary = public_lines[0] if public_lines else "Workflow aguardando condição externa para retomar pela rota oficial."
    next_action = _public_next_action_from_report_lines(public_lines)
    public_lines = [line for line in public_lines if not _is_public_next_action_line(line)]
    progress_status = progress.status
    count_label = _clean(progress.count_label)
    lines: list[str] = [
        "# Fix Wiki Report",
        "",
        "## Resumo",
        "",
        f"- {summary}",
    ]
    for line in public_lines:
        if _looks_like_transient_mutation_line(line):
            continue
        if line != summary:
            lines.append(f"- {line}")
    if count_label:
        lines.append(f"- progresso: {count_label}")
    headline = _clean(progress.headline or progress.message)
    detail = _clean(progress.detail)
    if headline and headline != summary:
        lines.append(f"- {headline}")
    if detail and detail not in {summary, headline}:
        lines.append(f"- detalhe: {detail}")
    can_continue_now = progress.can_continue_now
    current_state = _clean(snapshot.current_state)
    if can_continue_now is False and not can_continue_without_human:
        lines.append(f"- agora: {_waiting_line_for_state(progress_status=progress_status, current_state=current_state)}")
    if can_continue_without_human and not any("continuar automaticamente" in line.casefold() for line in lines):
        lines.append("- vou continuar automaticamente pela próxima etapa segura.")
    if current_state == "waiting_for_external_quota":
        lines.append("- estado: progresso preservado para retomada quando a cota voltar.")
    if next_action:
        lines.extend(["", "## Próxima Ação", "", f"- {next_action}"])
    decision_packet = (
        _human_decision_packet_fields(root.human_decision_packet)
        if root.human_decision_packet is not None
        else _HumanDecisionPacketFields()
    )
    if _human_decision_packet_has_data(decision_packet):
        lines.extend(["", "## Decisão Necessária", ""])
        lines.extend(_human_decision_packet_details(decision_packet))
    objective = fix_wiki_primary_objective_summary(report)
    if objective is not None:
        lines.extend(
            [
                "",
                "## Resultado Da Wiki",
                "",
                f"- Wiki: {objective.wiki_summary}",
                f"- Mudanças reais: {objective.mutation_summary}",
                f"- Grafo: {objective.graph_summary}",
                f"- Notas Relacionadas: {objective.related_notes_summary}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _clean(value: object) -> str:
    if value is None:
        raw = ""
    elif isinstance(value, str):
        raw = value
    else:
        raw = str(value)
    text = raw.replace("\r", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def _public_safe_report_line(value: str) -> str:
    prefix = "Próxima ação:"
    if value.casefold().startswith(prefix.casefold()):
        action = value[len(prefix):].strip()
        safe_action = _public_safe_next_action(action)
        return f"{prefix} {safe_action}" if safe_action else ""
    return _public_safe_next_action(value) if _is_technical_graph_curation_action(value) else value


def _is_public_next_action_line(value: str) -> bool:
    return value.casefold().startswith("próxima ação:")


def _public_next_action_from_report_lines(lines: list[str]) -> str:
    prefix = "Próxima ação:"
    for line in lines:
        if line.casefold().startswith(prefix.casefold()):
            return _public_safe_next_action(_clean(line[len(prefix):]))
    return ""


def _public_safe_next_action(value: str) -> str:
    if not value:
        return ""
    if _is_technical_graph_curation_action(value):
        return _PUBLIC_GRAPH_CURATION_ACTION
    return value


def _is_technical_graph_curation_action(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in _TECHNICAL_GRAPH_CURATION_MARKERS)


def _looks_like_transient_mutation_line(value: str) -> bool:
    return bool(_TRANSIENT_MUTATION_LINE_RE.search(value))


def _mentions_automatic_continuation(value: str) -> bool:
    return "continuar automaticamente" in value.casefold()


def _waiting_line_for_state(*, progress_status: str, current_state: str) -> str:
    if progress_status == "waiting_human":
        return "aguardando decisão segura antes de continuar."
    if progress_status == "waiting_external" or current_state == "waiting_for_external_quota":
        return "aguardando condição externa antes de continuar."
    return "aguardando a próxima etapa oficial antes de continuar."


def _human_decision_packet_details(packet: _HumanDecisionPacketFields) -> list[str]:
    if not _human_decision_packet_has_data(packet):
        return []
    out: list[str] = []
    question = _clean(packet.question or packet.public_summary)
    if question:
        out.append(f"- pergunta: {question}")
    option_labels = [
        _clean(option.label or option.id)
        for option in packet.options[:8]
        if option.label or option.id
    ]
    if option_labels:
        out.append(f"- opções fechadas: {', '.join(option_labels)}")
    if packet.rejected_automations:
        out.append("- automações rejeitadas:")
        for item in packet.rejected_automations[:8]:
            kind = _clean(item.kind)
            reason = _clean(item.reason or item.reason_code)
            out.append(f"  - {kind}: {reason}")
    else:
        out.append("- possível_bug_ux: decisão humana sem automações rejeitadas registradas.")
    if packet.resume_action:
        out.append(f"- retomada: {_PUBLIC_HUMAN_DECISION_RESUME}")
    return out
