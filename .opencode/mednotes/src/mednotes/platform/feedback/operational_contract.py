"""Shared operational API contract for workflow outputs and feedback records."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pydantic import ConfigDict, Field, StrictStr

from mednotes.kernel.agent_directive import AgentDirective, AgentDirectiveControl
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

FSM_FIRST_SCHEMAS = {
    "medical-notes-workbench.fix-wiki-fsm-result.v1",
    "medical-notes-workbench.flashcards-fsm-result.v1",
    "medical-notes-workbench.link-fsm-result.v1",
    "medical-notes-workbench.link-related-fsm-result.v1",
    "medical-notes-workbench.process-chats-fsm-result.v1",
    "medical-notes-workbench.setup-fsm-result.v1",
    "medical-notes-workbench.history-fsm-result.v1",
}

_AGENT_DIRECTIVE_SCHEMA = "medical-notes-workbench.agent-directive.v1"

_AGENT_DIRECTIVE_STATUSES = {
    "running",
    "waiting_agent",
    "waiting_external",
    "waiting_human",
    "blocked",
    "failed",
    "completed",
    "completed_with_warnings",
}

_AGENT_PREAMBLE_FIELD_PREFIXES = (
    "Status:",
    "phase:",
    "workflow_exit_code:",
    "workflow_result_label:",
    "blocked_reason:",
    "continuation_reason:",
    "blocking_reasons:",
    "next_action:",
    "next_command:",
    "execution_gate:",
    "resume_after_resolution:",
    "progress_view_model.status:",
    "progress_view_model.phase:",
    "progress_view_model.state:",
    "progress_view_model.can_continue_now:",
    "progress_view_model.resume_action:",
    "state_machine_snapshot.current_category:",
    "state_machine_snapshot.current_state:",
    "receipt.status:",
    "receipt.next_action:",
    "required_inputs:",
    "human_decision_required:",
    "decision.kind:",
    "decision.reason_code:",
    "decision.next_action:",
    "human_decision_packet:",
)

TOOL_PARAMETER_CONTRACT_VIOLATION = "agent.tool_param_contract_violation"
TOOL_CALL_ERROR = "agent.tool_call_error"
PUBLIC_TOOL_TEXT_CONTRACT_VIOLATION = "agent.public_tool_text_contract_violation"
PUBLIC_DEV_ESCAPE_CONTRACT_VIOLATION = "agent.public_dev_escape_contract_violation"
SUBAGENT_BATCH_CONTRACT_VIOLATION = "agent.subagent_batch_contract_violation"
SUBAGENT_RAW_CONTENT_CONTRACT_VIOLATION = "agent.subagent_raw_content_contract_violation"
SUBAGENT_INVOCATION_PACKET_CONTRACT_VIOLATION = "agent.subagent_invocation_packet_contract_violation"
SPECIALIST_PARALLEL_INVOCATION_CONTRACT_VIOLATION = "agent.specialist_parallel_invocation_contract_violation"
SPECIALIST_DUPLICATE_INVOCATION_CONTRACT_VIOLATION = "agent.specialist_duplicate_invocation_contract_violation"
WORKFLOW_CONTINUED_AFTER_BLOCKED_PAYLOAD = "agent.workflow_continued_after_blocked_payload"
MANUAL_SUBAGENT_CONTRACT_VIOLATION = "agent.manual_subagent_contract_violation"
WORKSPACE_ADD_DIR_HIDDEN_IGNORED = "agent.workspace_add_dir_hidden_ignored"
STYLE_REWRITE_WORKSPACE_PERMISSION_TIMEOUT = "agent.style_rewrite_workspace_permission_timeout"
PARALLEL_STYLE_REWRITE_CONTRACT_VIOLATION = "agent.parallel_style_rewrite_contract_violation"
DEPENDENT_STYLE_REWRITE_BATCH_CONTRACT_VIOLATION = "agent.dependent_style_rewrite_batch_contract_violation"
INVALID_EXTENSION_COMMAND_PATH = "agent.invalid_extension_command_path"
SHELL_CHAIN_CONTRACT_VIOLATION = "agent.shell_chain_contract_violation"
STYLE_REWRITE_DIRECT_CONTENT_APPLY = "agent.style_rewrite_direct_content_apply_contract_violation"
STYLE_REWRITE_UNVERIFIED_MODEL_CLAIM = "agent.style_rewrite_unverified_model_claim_contract_violation"
STYLE_REWRITE_PARENT_OUTPUT_WRITE = "agent.style_rewrite_parent_output_write_contract_violation"
SPECIALIST_UNVERIFIED_MODEL_ESCAPE = "agent.specialist_unverified_model_escape_contract_violation"
PROCESS_CHATS_RAW_WRITE = "agent.process_chats_raw_write_contract_violation"
PROCESS_CHATS_PARENT_ARTIFACT_WRITE_WITHOUT_SUBAGENT = (
    "agent.process_chats_parent_artifact_write_without_subagent"
)
WORKFLOW_SOURCE_DISCOVERY_AFTER_BLOCK = "agent.workflow_source_discovery_after_block"
STALE_EXTENSION_SCRIPT_PATH = "agent.stale_extension_script_path"
STALE_EXTENSION_SKILL_PATH = "agent.stale_extension_skill_path"
STALE_SUPERPOWERS_SKILL_PATH = "agent.stale_superpowers_skill_path"
WORKFLOW_ARTIFACT_DIRECT_WRITE = "agent.workflow_artifact_direct_write"
WORKFLOW_ARTIFACT_SHELL_COPY = "agent.workflow_artifact_shell_copy"
WORKFLOW_ARTIFACT_SHELL_REDIRECT = "agent.workflow_artifact_shell_redirect"
DUPLICATE_WORKFLOW_COMMAND = "agent.duplicate_workflow_command"
PREPARATORY_PERMISSION_PROBE = "agent.preparatory_permission_probe"
NONCANONICAL_PYTHON_ENVIRONMENT_PROBE = "agent.noncanonical_python_environment_probe"
FINAL_ARTIFACT_PATH_INVALID = "agent.final_artifact_path_invalid"
PACKAGED_AGENT_TEMPLATE_CONTRACT = "medical-notes-workbench.packaged-agent-template.v1"

_TRANSCRIPT_CHILD_CONTAINER_KEYS = (
    "$set",
    "content",
    "events",
    "items",
    "messages",
    "records",
    "response",
    "responses",
    "result",
    "tool_calls",
    "toolCalls",
    "transcript",
)

_RETRYABLE_SPECIALIST_BLOCKED_REASONS = {
    "specialist_model_metadata_missing",
    "style_rewrite_agent_contract_violation",
    "style_rewrite_output_missing",
    "style_rewrite_still_requires_rewrite",
}

_RUN_SHELL_TOOL_ALIASES = {
    "bash",
    "powershell",
    "pwsh",
    "run_shell",
    "run_shell_command",
    "run_command",
    "shell",
    "shelltool",
}

_RUN_SHELL_ALLOWED_PARAMETERS = {
    "CommandLine",
    "Cwd",
    "TimeoutMs",
    "WaitMsBeforeAsync",
    "cmd",
    "command",
    "cwd",
    "description",
    "delay_ms",
    "dirPath",
    "dir_path",
    "directory",
    "max_output_chars",
    "max_output_tokens",
    "script",
    "timeout_ms",
    "toolAction",
    "toolSummary",
    "workingDirectory",
    "working_directory",
    "yield_time_ms",
}

_SHELL_COMMAND_PARAMETER_FIELDS = ("command", "cmd", "script", "CommandLine", "commandLine")

_UPDATE_TOPIC_ALLOWED_PARAMETERS = {
    "strategic_intent",
    "summary",
    "title",
}

_INVOKE_AGENT_ALLOWED_PARAMETERS = {
    "agent_name",
    "prompt",
}

_TOOL_ALLOWED_PARAMETERS = {
    "run_shell_command": _RUN_SHELL_ALLOWED_PARAMETERS,
    "update_topic": _UPDATE_TOPIC_ALLOWED_PARAMETERS,
    "invoke_agent": _INVOKE_AGENT_ALLOWED_PARAMETERS,
}

_UPDATE_TOPIC_PUBLIC_TEXT_FIELDS = ("title", "summary", "strategic_intent")
_PUBLIC_TOOL_TEXT_FORBIDDEN_TERMS = (
    "--apply",
    "--dry-run",
    "--json",
    "apply-note-merge",
    "apply-style-rewrite",
    "apply-specialist-style-rewrite",
    "fix-wiki --",
    "guard_lease",
    "plan-subagents",
    "receipt",
    "run-finish",
    "run-linker",
    "run-start",
    "run_id",
    "schema",
    "scripts/",
    "uv run",
)
_PUBLIC_FINAL_RESPONSE_AGENT_INSTRUCTIONS = (
    "agent_instruction: nao anexe bloco diagnostico, JSON, XML, YAML ou campos tecnicos na resposta publica final; use logs/JSON para detalhes tecnicos.",
    "agent_instruction: em mensagens publicas de progresso e resposta final, nao cite subcomandos internos; diga correcao da Wiki e modelo medico especialista.",
)
_WORKFLOW_ARTIFACT_WRITE_TOOLS = {"write_file", "write_to_file", "write", "replace", "edit", "multiedit"}
_WORKFLOW_ARTIFACT_PATH_FIELDS = (
    "AbsolutePath",
    "TargetFile",
    "absolutePath",
    "absolute_path",
    "filePath",
    "file_path",
    "path",
    "targetFile",
    "target_file",
)
_WORKFLOW_ARTIFACT_NAME_RE = re.compile(
    r"(^|[-_])("
    r"plan|manifest|receipt|report|diagnosis|trigger-context|trigger_context|run_state"
    r")([-_.]|$)"
)
_WORKFLOW_ARTIFACT_SCRATCH_NAMES = (
    "compact-report.json",
    "dry_run_output.json",
    "fix-wiki-plan.json",
    "fix-wiki-user-report.md",
    "full-report.json",
    "link-diagnosis.json",
    "run_state.json",
)
_UNVERIFIED_SPECIALIST_MODEL_ENV = "MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL"
_PROCESS_CHATS_CONTEXT_MARKERS = (
    "/mednotes:process-chats",
    "process-medical-chats",
    "mednotes-process-chats",
)
_PROCESS_CHATS_ARTIFACT_SUFFIXES = (
    ".md",
    "coverage.json",
    "manifest.json",
    "raw-coverage.v1.json",
    "medical-notes-workbench.raw-coverage.v1.json",
    "note-plan.json",
    "triager-output.json",
)
_PACKAGED_SPECIALIST_AGENTS = frozenset({"med-knowledge-architect"})
_PACKAGED_SPECIALIST_AGENT_TEMPLATE_MARKERS = {
    "med-knowledge-architect": (
        "packaged_agent_template_contract: medical-notes-workbench.packaged-agent-template.v1",
        'You = "A Mente"',
        "Parent packet contract:",
        "parent_raw_content_bypass",
    )
}
_INTER_AGENT_MESSAGE_TOOLS = frozenset({"send_message", "invoke_subagent"})
_SUBAGENT_DEFINITION_TOOLS = frozenset({"define_subagent", "define_agent", "create_subagent"})
_MESSAGE_PARAMETER_FIELDS = ("Message", "message", "prompt", "Prompt", "content", "Content")
_AGENT_NAME_PARAMETER_FIELDS = ("agent_name", "agentName", "name", "Name", "agent", "Agent", "TypeName", "typeName")
_AGY_SUBAGENT_LIST_FIELDS = ("Subagents", "subagents")
_SUBAGENT_SYSTEM_PROMPT_PARAMETER_FIELDS = (
    "system_prompt",
    "SystemPrompt",
    "instructions",
    "Instructions",
    "prompt",
    "Prompt",
)
_STYLE_REWRITE_SUBAGENT_PROMPT_MARKERS = (
    "style-rewrite-",
    "wiki_note_style_rewrite",
    "rewrite prompt",
    "style-rewrite job",
)
_STYLE_REWRITE_TYPED_WORK_ITEM_TOKENS = (
    '"work_id"',
    '"item_type"',
    '"target_path"',
    '"target_hash_before"',
    '"temp_output"',
    '"subagent_output_contract"',
)
_HANDWRITTEN_SUBAGENT_PROMPT_MARKERS = (
    "CRITICAL MANDATORY INSTRUCTIONS",
    "You are assigned the style-rewrite job for:",
    "- Work ID:",
    "- Target Path:",
    "- Temp Output:",
)
_AGY_HIDDEN_WORKSPACE_RE = re.compile(
    r"failed\s+to\s+add\s+workspace\s+folder\b[\s\S]{0,500}\bis\s+hidden\s*:\s*ignore\s+uri",
    re.IGNORECASE,
)


class AgentPreambleProgressView(ContractModel):
    """Typed fallback lens used only to fail closed on malformed FSM payloads."""

    model_config = ConfigDict(extra="ignore")

    status: StrictStr = ""


class AgentPreambleSnapshot(ContractModel):
    """Typed fallback lens for the current FSM category in invalid payloads."""

    model_config = ConfigDict(extra="ignore")

    current_category: StrictStr = ""


class AgentPreamblePayload(ContractModel):
    """Typed preamble input; valid directives remain the only executable route."""

    model_config = ConfigDict(extra="ignore")

    schema_id: StrictStr = Field(default="", alias="schema")
    agent_directive: JsonObject | None = None
    progress_view_model: AgentPreambleProgressView = Field(default_factory=AgentPreambleProgressView)
    state_machine_snapshot: AgentPreambleSnapshot = Field(default_factory=AgentPreambleSnapshot)
    human_decision_required: bool = False


AgentPreambleProgressView.model_rebuild(_types_namespace=globals())
AgentPreambleSnapshot.model_rebuild(_types_namespace=globals())
AgentPreamblePayload.model_rebuild(_types_namespace=globals())


def agent_preamble_lines(payload: object) -> list[str]:
    """Return an agent-facing preamble projected from the operational contract."""
    preamble = AgentPreamblePayload.model_validate(payload)
    directive = _agent_directive(preamble)
    if _is_fsm_first_payload(preamble):
        if directive is None:
            return _invalid_agent_directive_preamble_lines(preamble)
        directive_lines = _agent_directive_preamble_lines(directive)
        if directive_lines:
            return directive_lines
        return []
    if directive is None:
        return []
    return _agent_directive_preamble_lines(directive)


def _is_fsm_first_payload(payload: AgentPreamblePayload) -> bool:
    return payload.schema_id in FSM_FIRST_SCHEMAS


def _agent_directive(payload: AgentPreamblePayload) -> AgentDirective | JsonObject | None:
    if payload.agent_directive is None:
        return None
    return _canonical_agent_directive(payload.agent_directive)


def _agent_directive_preamble_lines(directive: AgentDirective | JsonObject) -> list[str]:
    if isinstance(directive, dict):
        directive = JsonObjectAdapter.validate_python(directive)
        control = directive.get("control")
        if not isinstance(control, dict):
            return []
        control = JsonObjectAdapter.validate_python(control)
        banner = _agent_directive_banner(str(control.get("status") or "").strip())
        if not banner:
            return []
        lines = [banner]
        lines.extend(_string_list(directive.get("instructions")))
        summary = str(directive.get("summary") or "").strip()
        if summary:
            lines.append(f"agent_directive.summary: {summary}")
        lines.extend(_fallback_agent_directive_control_lines(control))
        if len(lines) == 1:
            return []
        lines.append("---")
        return lines
    control = directive.control
    banner = _agent_directive_banner(control.status)
    if not banner:
        return []
    lines = [banner]
    lines.extend(directive.instructions)
    summary = directive.summary.strip()
    if summary:
        lines.append(f"agent_directive.summary: {summary}")
    lines.extend(_agent_directive_control_lines(control))
    if len(lines) == 1:
        return []
    lines.append("---")
    return lines


def _canonical_agent_directive(directive: JsonObject) -> AgentDirective | JsonObject | None:
    canonical_error = _canonical_agent_directive_error(directive)
    if canonical_error is None:
        return _fallback_agent_directive(directive)
    if canonical_error:
        return None
    try:
        return AgentDirective.model_validate(directive)
    except ValueError as exc:
        del exc
        return None


def _canonical_agent_directive_error(directive: JsonObject) -> str | None:
    try:
        AgentDirective.model_validate(directive)
    except ValueError as exc:
        return str(exc)
    return ""


def _fallback_agent_directive(directive: JsonObject) -> JsonObject | None:
    error = _agent_directive_fallback_error(directive)
    if error:
        return None
    return directive


def _agent_directive_fallback_error(directive: JsonObject) -> str:
    if directive.get("schema") != _AGENT_DIRECTIVE_SCHEMA:
        return "agent_directive.schema invalid"
    if not _non_empty_text(directive.get("workflow")):
        return "agent_directive.workflow must be non-empty"
    if not _non_empty_text(directive.get("run_id")):
        return "agent_directive.run_id must be non-empty"
    instructions = directive.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, list):
            return "agent_directive.instructions must be a list"
        for line in instructions:
            if not isinstance(line, str):
                return "agent_directive.instructions must be text"
            if line.strip().casefold().startswith("agent_instruction:"):
                return "agent_directive.instructions must not include agent_instruction prefix"
    control = directive.get("control")
    if not isinstance(control, dict):
        return "agent_directive.control must be an object"
    if not _non_empty_text(control.get("state")):
        return "agent_directive.control.state must be non-empty"
    status = str(control.get("status") or "").strip()
    capabilities = control.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    continue_allowed = capabilities.get("continue")
    final_report_allowed = capabilities.get("final_report")
    effects = control.get("effects")
    effects = effects if isinstance(effects, list) else []
    resume = str(control.get("resume") or "").strip()
    blockers = _string_list(control.get("blockers"))
    if status == "waiting_agent":
        if continue_allowed is not True:
            return "waiting_agent requires control.capabilities.continue=true"
        if final_report_allowed is True:
            return "waiting_agent requires control.capabilities.final_report=false"
        if not effects and not resume:
            return "waiting_agent requires effects or resume"
    if status in {"completed", "completed_with_warnings"} and final_report_allowed is not True:
        return "completed directive requires control.capabilities.final_report=true"
    if status in {"waiting_human", "waiting_external", "blocked", "failed"} and not blockers and not resume:
        return f"{status} directive requires blockers or resume"
    return ""


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _invalid_agent_directive_preamble_lines(payload: AgentPreamblePayload) -> list[str]:
    banner = _agent_preamble_banner(payload)
    if not banner:
        return []
    return [
        banner,
        "agent_directive: missing_or_invalid",
        (
            "agent_instruction: pare e reporte bug de contrato em "
            "agent_directive root; nao use diagnostic_context nem campos agent-facing legados."
        ),
        "---",
    ]


def _agent_directive_banner(status: str) -> str:
    match status:
        case "running":
            return ">>> WORKFLOW EM EXECUCAO"
        case "waiting_agent":
            return ">>> CONTINUACAO AUTOMATICA OBRIGATORIA"
        case "waiting_human":
            return "??? DECISAO HUMANA NECESSARIA"
        case "failed":
            return "!!! WORKFLOW FALHOU"
        case "blocked":
            return "!!! ACAO OBRIGATORIA DO WORKFLOW"
        case "waiting_external":
            return "... AGUARDANDO CONDICAO EXTERNA"
        case _:
            return ""


def _agent_directive_control_lines(control: AgentDirectiveControl | JsonObject) -> list[str]:
    if isinstance(control, AgentDirectiveControl):
        return _canonical_agent_directive_control_lines(control)
    if isinstance(control, dict):
        return _fallback_agent_directive_control_lines(JsonObjectAdapter.validate_python(control))
    return []


def _canonical_agent_directive_control_lines(control: AgentDirectiveControl) -> list[str]:
    lines: list[str] = []
    fields = ("status", "state", "phase", "reason", "resume") if control.status == "running" else (
        "status",
        "state",
        "reason",
        "resume",
    )
    for field in fields:
        value = str(getattr(control, field)).strip()
        if value:
            lines.append(f"agent_directive.control.{field}: {value}")
    lines.append(f"agent_directive.control.capabilities.continue: {_json_bool(control.capabilities.continue_)}")
    lines.append(f"agent_directive.control.capabilities.final_report: {_json_bool(control.capabilities.final_report)}")
    effect_kinds = [effect.kind.value for effect in control.effects]
    if effect_kinds:
        lines.append(f"agent_directive.control.effects: {json.dumps(effect_kinds, ensure_ascii=False)}")
    if control.blockers:
        lines.append(f"agent_directive.control.blockers: {json.dumps(control.blockers, ensure_ascii=False)}")
    return lines


def _fallback_agent_directive_control_lines(control: JsonObject) -> list[str]:
    lines: list[str] = []
    status = str(control.get("status") or "").strip()
    fields = ("status", "state", "phase", "reason", "resume") if status == "running" else (
        "status",
        "state",
        "reason",
        "resume",
    )
    for field in fields:
        value = str(control.get(field) or "").strip()
        if value:
            lines.append(f"agent_directive.control.{field}: {value}")
    capabilities = control.get("capabilities")
    if isinstance(capabilities, dict):
        capabilities = JsonObjectAdapter.validate_python(capabilities)
        if "continue" in capabilities:
            lines.append(f"agent_directive.control.capabilities.continue: {_json_bool(capabilities.get('continue'))}")
        if "final_report" in capabilities:
            lines.append(
                f"agent_directive.control.capabilities.final_report: {_json_bool(capabilities.get('final_report'))}"
            )
    effects = control.get("effects")
    if isinstance(effects, list):
        effect_kinds = [
            str(item.get("kind")).strip()
            for item in effects
            if isinstance(item, dict) and str(item.get("kind") or "").strip()
        ]
        if effect_kinds:
            lines.append(f"agent_directive.control.effects: {json.dumps(effect_kinds, ensure_ascii=False)}")
    blockers = _string_list(control.get("blockers"))
    if blockers:
        lines.append(f"agent_directive.control.blockers: {json.dumps(blockers, ensure_ascii=False)}")
    return lines


def _json_bool(value: object) -> str:
    return "true" if value is True else "false"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _agent_preamble_banner(payload: AgentPreamblePayload) -> str:
    status = payload.progress_view_model.status.strip()
    category = payload.state_machine_snapshot.current_category.strip()
    if status == "running" or category == "running":
        return ">>> WORKFLOW EM EXECUCAO"
    if status == "waiting_agent" or category == "waiting_agent":
        return ">>> CONTINUACAO AUTOMATICA OBRIGATORIA"
    if status == "waiting_human" or category == "waiting_human" or payload.human_decision_required is True:
        return "??? DECISAO HUMANA NECESSARIA"
    if status == "failed" or category == "failed":
        return "!!! WORKFLOW FALHOU"
    if status in {"blocked", "error", "needs_review", "completed_with_link_blockers"} or category == "blocked":
        return "!!! ACAO OBRIGATORIA DO WORKFLOW"
    if status == "waiting_external" or category == "waiting_external":
        return "... AGUARDANDO CONDICAO EXTERNA"
    return ""

def validate_agent_tool_calls(transcript: Any) -> list[dict[str, Any]]:
    """Detect tool calls that include unsupported parameters.

    This is intentionally a transcript validator, not a prompt rule. It gives
    the lab and hooks a deterministic way to flag tool-contract drift such as
    `wait_for_previous` on shell calls.
    """
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    agy_plugin_context = _transcript_contains(
        transcript,
        ".gemini/config/plugins/medical-notes-workbench/skills/",
    )
    process_chats_context = any(_transcript_contains(transcript, marker) for marker in _PROCESS_CHATS_CONTEXT_MARKERS)
    process_chats_specialist_seen = False
    for tool_name, parameters in _iter_agent_tool_calls(transcript):
        canonical_tool = _canonical_tool_name(tool_name)
        allowed = _TOOL_ALLOWED_PARAMETERS.get(canonical_tool)
        if allowed:
            for key in parameters:
                if key in allowed:
                    continue
                finding_key = (canonical_tool, key)
                if finding_key in seen:
                    continue
                seen.add(finding_key)
                findings.append(
                    {
                        "code": TOOL_PARAMETER_CONTRACT_VIOLATION,
                        "severity": "medium",
                        "tool_name": canonical_tool,
                        "bad_param": key,
                        "message": f"Tool call {canonical_tool} included unsupported parameter {key}.",
                        "next_action": (
                            "Reportar como bug de contrato de tool; sequencie comandos esperando "
                            "o resultado da chamada anterior."
                        ),
                    }
                )
        permission_probe_finding = _permission_probe_finding(canonical_tool)
        if permission_probe_finding and (canonical_tool, "permission_probe") not in seen:
            seen.add((canonical_tool, "permission_probe"))
            findings.append(permission_probe_finding)
        batch_finding = _subagent_batch_finding(canonical_tool, parameters)
        if batch_finding and (canonical_tool, "subagent_batch") not in seen:
            seen.add((canonical_tool, "subagent_batch"))
            findings.append(batch_finding)
        raw_content_finding = _subagent_raw_content_finding(canonical_tool, parameters)
        if raw_content_finding and (canonical_tool, "subagent_raw_content") not in seen:
            seen.add((canonical_tool, "subagent_raw_content"))
            findings.append(raw_content_finding)
        for invocation_finding in _subagent_invocation_packet_findings(canonical_tool, parameters):
            finding_key = (
                canonical_tool,
                "subagent_invocation_packet",
                str(invocation_finding.get("agent_name") or ""),
                str(invocation_finding.get("bad_param") or ""),
            )
            if finding_key in seen:
                continue
            seen.add(finding_key)
            findings.append(invocation_finding)
        invalid_extension_path_finding = _invalid_extension_command_path_finding(canonical_tool, parameters)
        if invalid_extension_path_finding and (canonical_tool, "invalid_extension_command_path") not in seen:
            seen.add((canonical_tool, "invalid_extension_command_path"))
            findings.append(invalid_extension_path_finding)
        stale_script_path_finding = _stale_extension_script_path_finding(
            canonical_tool,
            parameters,
            agy_plugin_context=agy_plugin_context,
        )
        if stale_script_path_finding and (canonical_tool, "stale_extension_script_path") not in seen:
            seen.add((canonical_tool, "stale_extension_script_path"))
            findings.append(stale_script_path_finding)
        shell_chain_finding = _shell_chain_finding(canonical_tool, parameters)
        if shell_chain_finding and (canonical_tool, "shell_chain") not in seen:
            seen.add((canonical_tool, "shell_chain"))
            findings.append(shell_chain_finding)
        public_dev_escape_finding = _public_dev_escape_finding(canonical_tool, parameters)
        if public_dev_escape_finding and (canonical_tool, "public_dev_escape") not in seen:
            seen.add((canonical_tool, "public_dev_escape"))
            findings.append(public_dev_escape_finding)
        direct_style_apply_finding = _style_rewrite_direct_content_apply_finding(canonical_tool, parameters)
        if direct_style_apply_finding and (canonical_tool, "style_rewrite_direct_content_apply") not in seen:
            seen.add((canonical_tool, "style_rewrite_direct_content_apply"))
            findings.append(direct_style_apply_finding)
        unverified_model_finding = _style_rewrite_unverified_model_claim_finding(canonical_tool, parameters)
        if unverified_model_finding and (canonical_tool, "style_rewrite_unverified_model_claim") not in seen:
            seen.add((canonical_tool, "style_rewrite_unverified_model_claim"))
            findings.append(unverified_model_finding)
        unverified_escape_finding = _specialist_unverified_model_escape_finding(canonical_tool, parameters)
        if unverified_escape_finding and (canonical_tool, "specialist_unverified_model_escape") not in seen:
            seen.add((canonical_tool, "specialist_unverified_model_escape"))
            findings.append(unverified_escape_finding)
        style_output_write_finding = _style_rewrite_parent_output_write_finding(canonical_tool, parameters)
        if style_output_write_finding and (
            canonical_tool,
            "style_rewrite_parent_output_write",
            str(style_output_write_finding.get("path") or ""),
        ) not in seen:
            seen.add(
                (
                    canonical_tool,
                    "style_rewrite_parent_output_write",
                    str(style_output_write_finding.get("path") or ""),
                )
            )
            findings.append(style_output_write_finding)
        if process_chats_context:
            raw_write_finding = _process_chats_raw_write_finding(canonical_tool, parameters)
            if raw_write_finding and (
                canonical_tool,
                "process_chats_raw_write",
                str(raw_write_finding.get("path") or ""),
            ) not in seen:
                seen.add(
                    (
                        canonical_tool,
                        "process_chats_raw_write",
                        str(raw_write_finding.get("path") or ""),
                    )
                )
                findings.append(raw_write_finding)
            artifact_write_finding = _process_chats_parent_artifact_write_without_subagent_finding(
                canonical_tool,
                parameters,
                process_chats_specialist_seen=process_chats_specialist_seen,
            )
            if artifact_write_finding and (
                canonical_tool,
                "process_chats_parent_artifact_write_without_subagent",
                str(artifact_write_finding.get("path") or ""),
            ) not in seen:
                seen.add(
                    (
                        canonical_tool,
                        "process_chats_parent_artifact_write_without_subagent",
                        str(artifact_write_finding.get("path") or ""),
                    )
                )
                findings.append(artifact_write_finding)
        source_discovery_finding = _workflow_source_discovery_after_block_finding(canonical_tool, parameters)
        if source_discovery_finding and (canonical_tool, "workflow_source_discovery_after_block") not in seen:
            seen.add((canonical_tool, "workflow_source_discovery_after_block"))
            findings.append(source_discovery_finding)
        python_environment_probe_finding = _python_environment_probe_finding(canonical_tool, parameters)
        if python_environment_probe_finding and (canonical_tool, "python_environment_probe") not in seen:
            seen.add((canonical_tool, "python_environment_probe"))
            findings.append(python_environment_probe_finding)
        workflow_artifact_write_finding = _workflow_artifact_direct_write_finding(canonical_tool, parameters)
        if workflow_artifact_write_finding and (
            canonical_tool,
            "workflow_artifact_direct_write",
            str(workflow_artifact_write_finding.get("path") or ""),
        ) not in seen:
            seen.add(
                (
                    canonical_tool,
                    "workflow_artifact_direct_write",
                    str(workflow_artifact_write_finding.get("path") or ""),
                )
            )
            findings.append(workflow_artifact_write_finding)
        workflow_artifact_shell_copy_finding = _workflow_artifact_shell_copy_finding(canonical_tool, parameters)
        if workflow_artifact_shell_copy_finding and (canonical_tool, "workflow_artifact_shell_copy") not in seen:
            seen.add((canonical_tool, "workflow_artifact_shell_copy"))
            findings.append(workflow_artifact_shell_copy_finding)
        workflow_artifact_shell_redirect_finding = _workflow_artifact_shell_redirect_finding(canonical_tool, parameters)
        if workflow_artifact_shell_redirect_finding and (
            canonical_tool,
            "workflow_artifact_shell_redirect",
        ) not in seen:
            seen.add((canonical_tool, "workflow_artifact_shell_redirect"))
            findings.append(workflow_artifact_shell_redirect_finding)
        if process_chats_context and _is_process_chats_specialist_invocation(canonical_tool):
            process_chats_specialist_seen = True
    for batch_finding in _parallel_style_rewrite_findings(transcript):
        finding_key = ("run_shell_command", "parallel_style_rewrite", str(batch_finding.get("mode") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(batch_finding)
    for specialist_finding in _parallel_specialist_invocation_findings(transcript):
        finding_key = ("invoke_agent", "parallel_specialist_invocation", str(specialist_finding.get("call_count") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(specialist_finding)
    for specialist_finding in _duplicate_specialist_invocation_findings(transcript):
        finding_key = (
            "invoke_agent",
            "duplicate_specialist_invocation",
            str(specialist_finding.get("work_id") or ""),
        )
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(specialist_finding)
    for blocked_finding in _continued_after_blocked_payload_findings(transcript):
        finding_key = (
            "workflow_continued_after_blocked_payload",
            str(blocked_finding.get("blocked_reason") or ""),
            str(blocked_finding.get("tool_name") or ""),
        )
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(blocked_finding)
    for duplicate_finding in _duplicate_workflow_command_findings(transcript):
        finding_key = (
            "run_shell_command",
            "duplicate_workflow_command",
            str(duplicate_finding.get("workflow") or ""),
            str(duplicate_finding.get("mode") or ""),
        )
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(duplicate_finding)
    for error_finding in _agent_tool_error_findings(transcript):
        finding_key = ("tool_error", str(error_finding.get("message") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(error_finding)
    for skill_finding in _stale_extension_skill_findings(transcript, agy_plugin_context=agy_plugin_context):
        finding_key = ("stale_skill", str(skill_finding.get("path") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(skill_finding)
    for artifact_finding in _final_artifact_path_findings(transcript):
        finding_key = ("final_artifact_path", str(artifact_finding.get("path") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(artifact_finding)
    for manual_subagent_finding in _manual_packaged_subagent_findings(transcript):
        finding_key = (
            "manual_subagent_definition",
            str(manual_subagent_finding.get("agent_name") or ""),
        )
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(manual_subagent_finding)
    for hidden_workspace_finding in _agy_hidden_workspace_findings(transcript):
        finding_key = ("agy_hidden_workspace", str(hidden_workspace_finding.get("path") or ""))
        if finding_key in seen:
            continue
        seen.add(finding_key)
        findings.append(hidden_workspace_finding)
    return findings


def _agent_tool_error_findings(transcript: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        workspace_timeout_finding = _style_rewrite_workspace_permission_timeout_finding(value)
        if workspace_timeout_finding:
            findings.append(workspace_timeout_finding)
            return
        stale_superpowers_finding = _stale_superpowers_tool_error_finding(value)
        if stale_superpowers_finding:
            findings.append(stale_superpowers_finding)
            return
        error_payload = _tool_error_payload(value)
        if error_payload:
            error_type, severity, text = error_payload
            findings.append(
                {
                    "code": TOOL_CALL_ERROR,
                    "severity": severity,
                    "error_type": error_type,
                    "tool_type": str(value.get("type") or ""),
                    "message": f"Tool call failed before execution: {_normalize_tool_error_text(text)}",
                    "next_action": "Reportar a tool call falha no relatório final mesmo se um retry posterior recuperar.",
                }
            )
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(transcript)
    return findings


def _style_rewrite_workspace_permission_timeout_finding(value: dict[str, Any]) -> dict[str, Any] | None:
    raw = value.get("error") or value.get("content") or value.get("message") or ""
    text = str(raw)
    lowered = text.lower()
    status = str(value.get("status") or "").lower()
    if status not in {"error", "failed"}:
        return None
    if "permission prompt" not in lowered or "write_file" not in lowered or "timed out" not in lowered:
        return None
    match = re.search(r"target ['\"](?P<path>[^'\"]*tmp/agent-work/fix-wiki/[^'\"]+\.rewrite\.md)['\"]", text)
    if not match:
        return None
    path = match.group("path")
    return {
        "code": STYLE_REWRITE_WORKSPACE_PERMISSION_TIMEOUT,
        "severity": "high",
        "tool_name": "write_file",
        "bad_param": "target",
        "path": path,
        "message": "Style rewrite temp_output was outside the writable AGY/subagent workspace.",
        "next_action": (
            "Repetir a rodada com o temp_dir do work_item adicionado ao workspace antes de invocar "
            "o subagente; não tente contornar por scratch, run_command ou conteúdo colado."
        ),
    }


def _stale_superpowers_tool_error_finding(value: dict[str, Any]) -> dict[str, Any] | None:
    raw = value.get("error") or value.get("content") or value.get("message") or ""
    text = str(raw).replace("\\", "/").casefold()
    status = str(value.get("status") or "").lower()
    event_type = str(value.get("type") or "").upper()
    if ".gemini/extensions/superpowers/skills" not in text:
        return None
    if event_type != "ERROR_MESSAGE" and status not in {"error", "failed"}:
        return None
    return {
        "code": STALE_SUPERPOWERS_SKILL_PATH,
        "severity": "high",
        "tool_name": "read_file",
        "bad_param": "path",
        "path": "~/.gemini/extensions/superpowers/skills/*",
        "message": "Agent tried to load stale Superpowers skill files outside the AGY plugin surface.",
        "next_action": (
            "Reportar como bug de roteamento AGY; use somente skills/docs empacotados no plugin "
            "ou caminhos explícitos do payload oficial."
        ),
    }


def _tool_error_payload(value: dict[str, Any]) -> tuple[str, str, str] | None:
    raw = value.get("error") or value.get("content") or value.get("message") or ""
    text = str(raw)
    lowered = text.lower()
    status = str(value.get("status") or "").lower()
    event_type = str(value.get("type") or "").upper()
    if (
        ("invalid tool call" in lowered or "invalid_tool_params" in lowered)
        and (event_type == "ERROR_MESSAGE" or status in {"error", "failed"})
    ):
        return ("invalid_tool_params", "medium", text)
    if status in {"error", "failed"} and _looks_like_transcript_tool_event(value):
        severity = "low" if _is_low_severity_tool_error(text) else "medium"
        return ("tool_status_error", severity, text)
    return None


def _looks_like_transcript_tool_event(value: dict[str, Any]) -> bool:
    event_type = str(value.get("type") or "").upper()
    if not event_type or event_type in {"ERROR_MESSAGE", "PLANNER_RESPONSE", "SYSTEM_MESSAGE"}:
        return False
    return any(key in value for key in ("step_index", "source", "created_at", "tool_name", "content"))


def _is_low_severity_tool_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "permission denied" in lowered
        and ("read_file" in lowered or "list" in lowered)
        and "system protection boundary" in lowered
    )


def _normalize_tool_error_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    marker = "Error Message:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    if len(text) > 300:
        text = text[:297].rstrip() + "..."
    return text


def _public_tool_text_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "update_topic":
        return None
    field_hits: list[str] = []
    term_hits: list[str] = []
    for field in _UPDATE_TOPIC_PUBLIC_TEXT_FIELDS:
        value = parameters.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        matches = [term for term in _PUBLIC_TOOL_TEXT_FORBIDDEN_TERMS if term in lowered]
        if not matches:
            continue
        field_hits.append(field)
        for term in matches:
            if term not in term_hits:
                term_hits.append(term)
    if not field_hits:
        return None
    return {
        "code": PUBLIC_TOOL_TEXT_CONTRACT_VIOLATION,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "public_text",
        "message": "Tool call update_topic exposed internal workflow terms in public text.",
        "fields": field_hits,
        "forbidden_terms": term_hits,
        "next_action": (
            "Reportar como bug de UX/tool-contract; traduza update_topic para linguagem pública "
            "sem flags, comandos, paths ou IDs técnicos."
        ),
    }


def _permission_probe_finding(tool_name: str) -> dict[str, Any] | None:
    if tool_name != "list_permissions":
        return None
    return {
        "code": PREPARATORY_PERMISSION_PROBE,
        "severity": "low",
        "tool_name": tool_name,
        "bad_param": "tool_call",
        "message": "Agent listed AGY permissions as a preparatory probe before the workflow.",
        "next_action": (
            "Reportar como desvio operacional leve; em ambiente já preparado, execute o workflow público "
            "e reporte bloqueios do payload em vez de sondar permissões."
        ),
    }


def _subagent_batch_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "invoke_agent":
        return None
    agent_name = str(parameters.get("agent_name") or "")
    if agent_name != "med-knowledge-architect":
        return None
    prompt = parameters.get("prompt")
    if not isinstance(prompt, str):
        return None
    work_ids = sorted(set(re.findall(r"style-rewrite-\d{3}-[a-z0-9-]+", prompt)))
    if len(work_ids) <= 1:
        return None
    return {
        "code": SUBAGENT_BATCH_CONTRACT_VIOLATION,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "prompt",
        "message": "Tool call invoke_agent batched multiple style rewrite work items into one med-knowledge-architect.",
        "work_item_count": len(work_ids),
        "work_ids": work_ids,
        "next_action": (
            "Reportar como bug de orquestração; lance um med-knowledge-architect por work_item.target_path."
        ),
    }


def _subagent_raw_content_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name == "invoke_agent":
        agent_name = str(parameters.get("agent_name") or "")
        if agent_name != "med-knowledge-architect":
            return None
        prompt = parameters.get("prompt")
        if not isinstance(prompt, str) or not _looks_like_raw_markdown_note(prompt):
            return None
        return {
            "code": SUBAGENT_RAW_CONTENT_CONTRACT_VIOLATION,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": "prompt",
            "message": "Tool call invoke_agent embedded raw Markdown note content in a med-knowledge-architect prompt.",
            "next_action": (
                "Reportar como bug de privacidade/orquestração; passe apenas work_item, target_path, "
                "rewrite_prompt e temp_output oficiais, sem colar conteúdo clínico no prompt pai."
            ),
        }
    if tool_name not in _INTER_AGENT_MESSAGE_TOOLS:
        return None
    field_name, message = _first_string_parameter(parameters, _MESSAGE_PARAMETER_FIELDS)
    if not message or not _looks_like_raw_markdown_note(message):
        return None
    return {
        "code": SUBAGENT_RAW_CONTENT_CONTRACT_VIOLATION,
        "severity": "high",
        "tool_name": tool_name,
        "bad_param": field_name,
        "message": f"Tool call {tool_name} embedded raw Markdown note content in an inter-agent message.",
        "next_action": (
            "Reportar como bug de privacidade/orquestração; não cole conteúdo clínico no parent "
            "ou em mensagens entre agentes. Recomece pela rota oficial com o work_item tipado."
        ),
    }


def _subagent_invocation_packet_findings(tool_name: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    if tool_name not in {"invoke_agent", "invoke_subagent"}:
        return []
    findings: list[dict[str, Any]] = []
    for agent_name, field_name, prompt in _packaged_subagent_invocation_prompts(parameters):
        if agent_name != "med-knowledge-architect":
            continue
        if not _looks_like_style_rewrite_invocation_prompt(prompt):
            continue
        if _is_official_typed_style_rewrite_invocation_prompt(prompt):
            continue
        findings.append(
            {
                "code": SUBAGENT_INVOCATION_PACKET_CONTRACT_VIOLATION,
                "severity": "high",
                "tool_name": tool_name,
                "bad_param": field_name,
                "agent_name": agent_name,
                "message": (
                    f"Tool call {tool_name} sent a handwritten med-knowledge-architect prompt instead "
                    "of the official typed work item packet."
                ),
                "next_action": (
                    "Reportar como bug de orquestração; invoque o subagente com o work_item tipado do plano oficial, "
                    "incluindo target_hash_before, temp_output e subagent_output_contract, sem instruções manuais extras."
                ),
            }
        )
    return findings


def _packaged_subagent_invocation_prompts(parameters: dict[str, Any]) -> list[tuple[str, str, str]]:
    prompts: list[tuple[str, str, str]] = []
    field_name, prompt = _first_string_parameter(parameters, _MESSAGE_PARAMETER_FIELDS)
    _, agent_name = _first_string_parameter(parameters, _AGENT_NAME_PARAMETER_FIELDS)
    if agent_name and prompt:
        prompts.append((agent_name, field_name, prompt))
    for list_field in _AGY_SUBAGENT_LIST_FIELDS:
        value = parameters.get(list_field)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            nested_field_name, nested_prompt = _first_string_parameter(item, _MESSAGE_PARAMETER_FIELDS)
            _, nested_agent_name = _first_string_parameter(item, _AGENT_NAME_PARAMETER_FIELDS)
            if nested_agent_name and nested_prompt:
                prompts.append((nested_agent_name, f"{list_field}[].{nested_field_name}", nested_prompt))
    return prompts


def _looks_like_style_rewrite_invocation_prompt(prompt: str) -> bool:
    lowered = prompt.casefold()
    return any(marker in lowered for marker in _STYLE_REWRITE_SUBAGENT_PROMPT_MARKERS)


def _is_official_typed_style_rewrite_invocation_prompt(prompt: str) -> bool:
    if any(marker in prompt for marker in _HANDWRITTEN_SUBAGENT_PROMPT_MARKERS):
        return False
    return all(token in prompt for token in _STYLE_REWRITE_TYPED_WORK_ITEM_TOKENS)


def _looks_like_raw_markdown_note(text: str) -> bool:
    lowered = text.lower()
    has_raw_note_marker = any(
        marker in lowered for marker in ("material-fonte", "nota atual", "nota médica abaixo", "nota medica abaixo")
    )
    has_markdown_note = bool(re.search(r"(?m)^#\s+\S+", text) or "\n---\n" in text or "```markdown" in lowered)
    return has_raw_note_marker and has_markdown_note


def _first_string_parameter(parameters: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, str]:
    for field in fields:
        value = parameters.get(field)
        if isinstance(value, str) and value.strip():
            return field, value
    return "", ""


def _parallel_style_rewrite_findings(transcript: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for batch in _iter_tool_call_batches(transcript):
        style_calls: list[dict[str, str]] = []
        dependent_families: list[str] = []
        for tool_name, parameters in batch:
            if _canonical_tool_name(tool_name) != "run_shell_command":
                continue
            command = _shell_command_text(parameters)
            for command_family in (
                "finalize-style-rewrite-output",
                "collect-style-rewrite-outputs",
                "apply-style-rewrite",
            ):
                if command_family in command:
                    dependent_families.append(command_family)
            if "apply-style-rewrite" in command or "apply-specialist-style-rewrite" in command:
                style_calls.append(
                    {
                        "mode": "dry_run" if "--dry-run" in command else "apply",
                        "command": command,
                    }
                )
        unique_dependent_families = sorted(set(dependent_families))
        if len(unique_dependent_families) > 1:
            findings.append(
                {
                    "code": DEPENDENT_STYLE_REWRITE_BATCH_CONTRACT_VIOLATION,
                    "severity": "high",
                    "tool_name": "run_shell_command",
                    "bad_param": "tool_batch",
                    "command_families": unique_dependent_families,
                    "message": "Dependent style-rewrite commands were emitted in the same tool batch.",
                    "next_action": (
                        "Reportar como bug de orquestração; use apply-specialist-style-rewrite para finalizar, "
                        "coletar e aplicar um item em uma única chamada oficial."
                    ),
                }
            )
        if len(style_calls) <= 1:
            continue
        modes = sorted({item["mode"] for item in style_calls})
        findings.append(
            {
                "code": PARALLEL_STYLE_REWRITE_CONTRACT_VIOLATION,
                "severity": "medium",
                "tool_name": "run_shell_command",
                "bad_param": "tool_batch",
                "mode": "+".join(modes),
                "call_count": len(style_calls),
                "message": "Multiple apply-style-rewrite commands were emitted in the same tool batch.",
                "next_action": (
                    "Reportar como bug de orquestração; valide e aplique cada rewrite em série, "
                    "aguardando o resultado JSON antes do próximo comando."
                ),
            }
        )
    return findings


def _parallel_specialist_invocation_findings(transcript: Any) -> list[dict[str, Any]]:
    for batch in _iter_tool_call_batches(transcript):
        specialist_calls = [
            parameters
            for tool_name, parameters in batch
            if _is_style_rewrite_specialist_invocation(_canonical_tool_name(tool_name), parameters)
        ]
        if len(specialist_calls) > 1:
            return [_parallel_specialist_invocation_finding(len(specialist_calls))]

    active_tool_ids: list[str] = []
    active_count = 0
    for record in _iter_agent_tool_event_records(transcript):
        tool_name = _canonical_tool_name(_tool_name_from_record(record))
        event_type = str(record.get("type") or record.get("event_type") or "").casefold()
        tool_id = str(record.get("tool_id") or record.get("id") or "").strip()
        parameters = _tool_parameters_from_record(record) or {}
        if event_type == "tool_result":
            if tool_id and tool_id in active_tool_ids:
                active_tool_ids.remove(tool_id)
                active_count = max(0, active_count - 1)
            continue
        if event_type != "tool_use":
            continue
        if not _is_style_rewrite_specialist_invocation(tool_name, parameters):
            continue
        if active_count > 0:
            return [_parallel_specialist_invocation_finding(active_count + 1)]
        active_count += 1
        if tool_id:
            active_tool_ids.append(tool_id)
    return []


def _parallel_specialist_invocation_finding(call_count: int) -> dict[str, Any]:
    return {
        "code": SPECIALIST_PARALLEL_INVOCATION_CONTRACT_VIOLATION,
        "severity": "high",
        "tool_name": "invoke_agent",
        "bad_param": "tool_sequence",
        "call_count": call_count,
        "message": "Multiple med-knowledge-architect invoke_agent calls were started before a prior specialist receipt/result.",
        "next_action": (
            "Reportar como bug de orquestração; no Gemini CLI, execute o lote de reescrita em série, "
            "aguardando resultado e specialist_task_run_receipt_path antes do próximo invoke_agent."
        ),
    }


def _duplicate_specialist_invocation_findings(transcript: Any) -> list[dict[str, Any]]:
    seen_work_ids: set[str] = set()
    for record in _iter_agent_tool_event_records(transcript):
        event_type = str(record.get("type") or record.get("event_type") or "").casefold()
        if event_type != "tool_use":
            continue
        tool_name = _canonical_tool_name(_tool_name_from_record(record))
        parameters = _tool_parameters_from_record(record) or {}
        if not _is_style_rewrite_specialist_invocation(tool_name, parameters):
            continue
        work_id = _style_rewrite_work_id_from_parameters(parameters)
        if not work_id:
            continue
        if work_id in seen_work_ids:
            return [
                {
                    "code": SPECIALIST_DUPLICATE_INVOCATION_CONTRACT_VIOLATION,
                    "severity": "high",
                    "tool_name": "invoke_agent",
                    "bad_param": "tool_sequence",
                    "work_id": work_id,
                    "message": "The same style rewrite work item was sent to med-knowledge-architect more than once.",
                    "next_action": (
                        "Reportar como bug de orquestração; depois do primeiro invoke_agent, finalize com "
                        "recibo oficial se existir, ou pare e reporte o bloqueio de recibo/modelo sem repetir "
                        "o subagente para o mesmo work_id."
                    ),
                }
            ]
        seen_work_ids.add(work_id)
    return []


def _continued_after_blocked_payload_findings(transcript: object) -> list[JsonObject]:
    blocked_reason = ""
    for record in _iter_agent_tool_event_records(transcript):
        event_type = str(record.get("type") or record.get("event_type") or "").casefold()
        tool_name = _canonical_tool_name(_tool_name_from_record(record))
        if event_type == "tool_result":
            output = str(record.get("output") or record.get("content") or "")
            found_reason = _blocked_payload_reason_from_output(output)
            if found_reason:
                blocked_reason = found_reason
            continue
        if event_type != "tool_use" or not blocked_reason:
            continue
        parameters = _tool_parameters_from_record(record) or {}
        if _is_allowed_after_blocked_payload_tool_use(tool_name, parameters, blocked_reason=blocked_reason):
            continue
        return [
            {
                "code": WORKFLOW_CONTINUED_AFTER_BLOCKED_PAYLOAD,
                "severity": "high",
                "tool_name": tool_name,
                "bad_param": "tool_sequence",
                "blocked_reason": blocked_reason,
                "message": "Agent continued executing tools after a workflow payload explicitly blocked continuation.",
                "next_action": (
                    "Reportar como bug de orquestração; quando o payload disser WORKFLOW BLOQUEADO e "
                    "next_command=null, pare a continuação pública e só feche a proteção do vault quando aplicável."
                ),
            }
        ]
    return []


def _blocked_payload_reason_from_output(output: str) -> str:
    if not output or "blocked" not in output or "blocked_reason" not in output:
        return ""
    patterns = (
        r"blocked_reason:\s*([A-Za-z0-9_.-]+)",
        r'"blocked_reason"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return ""


def _is_allowed_after_blocked_payload_tool_use(
    tool_name: str,
    parameters: JsonObject,
    *,
    blocked_reason: str,
) -> bool:
    if tool_name in {"tracker_create_task", "tracker_update_task", "tracker_visualize", "update_topic"}:
        return True
    if blocked_reason in _RETRYABLE_SPECIALIST_BLOCKED_REASONS:
        return _is_style_rewrite_specialist_invocation(tool_name, parameters)
    if tool_name != "run_shell_command":
        return False
    command = _shell_command_text(parameters)
    if "vault_git.py" in command and "run-finish" in command:
        return True
    return False


def _is_style_rewrite_specialist_invocation(tool_name: str, parameters: dict[str, Any]) -> bool:
    if tool_name != "invoke_agent":
        return False
    agent_name = str(parameters.get("agent_name") or parameters.get("name") or "").strip()
    if agent_name == "med-knowledge-architect":
        return True
    _field_name, prompt = _first_string_parameter(parameters, _MESSAGE_PARAMETER_FIELDS)
    return "med-knowledge-architect" in prompt or "style-rewrite-" in prompt


def _style_rewrite_work_id_from_parameters(parameters: dict[str, Any]) -> str:
    _field_name, prompt = _first_string_parameter(parameters, _MESSAGE_PARAMETER_FIELDS)
    match = re.search(r"style-rewrite-\d{3}-[a-z0-9-]+", prompt)
    return match.group(0) if match else ""


def _iter_agent_tool_event_records(node: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        event_type = str(value.get("type") or value.get("event_type") or "").casefold()
        if event_type in {"tool_use", "tool_result"}:
            records.append(value)
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(node)
    return records


def _invalid_extension_command_path_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if "dist/gemini-cli-" not in command:
        return None
    path_hits = sorted(
        path for path in set(re.findall(r"""[^\s"']*dist/gemini-cli-[^\s"']+""", command))
        if "dist/gemini-cli-extension" not in path
    )
    if not path_hits:
        return None
    return {
        "code": INVALID_EXTENSION_COMMAND_PATH,
        "severity": "high",
        "tool_name": tool_name,
        "bad_param": "command",
        "message": "Tool call run_shell_command referenced a non-canonical Gemini extension dist path.",
        "paths": path_hits[:5],
        "next_action": (
            "Reportar como bug de descoberta de caminho; use somente o extensionPath carregado "
            "pelo bundle ativo e não invente variantes de dist/gemini-cli-extension."
        ),
    }


def _stale_extension_script_path_finding(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    agy_plugin_context: bool = False,
) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if agy_plugin_context and re.search(
        r"(?:~|/Users/[^/\s\"']+)/\.gemini/extensions/medical-notes-workbench/",
        command,
    ):
        return {
            "code": STALE_EXTENSION_SCRIPT_PATH,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": "command",
            "message": "Tool call run_shell_command used the global Gemini extension path while the session was running from an AGY plugin root.",
            "next_action": (
                "Reportar como bug de descoberta de caminho; carregue a skill escopada do plugin "
                "e use o extensionPath ativo em vez de ~/.gemini/extensions/medical-notes-workbench."
            ),
        }
    if re.search(r"scripts[/\\]mednotes[/\\]vault[/\\]vault_git\.py", command):
        return {
            "code": STALE_EXTENSION_SCRIPT_PATH,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": "command",
            "message": "Tool call run_shell_command referenced stale vault_git.py path under scripts/mednotes/vault.",
            "next_action": (
                "Reportar como bug de descoberta de caminho; use scripts/vault/vault_git.py do bundle ativo."
            ),
        }
    return None


def _stale_extension_skill_findings(transcript: Any, *, agy_plugin_context: bool = False) -> list[dict[str, Any]]:
    if not agy_plugin_context:
        return []
    if not _transcript_contains_stale_skill_view(transcript):
        return []
    return [
        {
            "code": STALE_EXTENSION_SKILL_PATH,
            "severity": "high",
            "tool_name": "view_file",
            "bad_param": "path",
            "path": "~/.gemini/config/skills/fix-medical-wiki/SKILL.md",
            "message": "Agent loaded the unscoped global fix-medical-wiki skill after loading the AGY plugin launcher.",
            "next_action": (
                "Reportar como bug de skill routing; o launcher deve carregar "
                "${extensionPath}/skills/fix-medical-wiki/SKILL.md por path escopado."
            ),
        }
    ]


def _transcript_contains_stale_skill_view(transcript: Any) -> bool:
    stale_path = ".gemini/config/skills/fix-medical-wiki/skill.md"

    def visit(value: Any) -> bool:
        if isinstance(value, list):
            return any(visit(item) for item in value)
        if not isinstance(value, dict):
            return False
        event_type = str(value.get("type") or "").lower()
        content = str(value.get("content") or "").lower()
        if event_type == "view_file" and stale_path in content:
            return True
        tool_name = _canonical_tool_name(_tool_name_from_record(value))
        parameters = _tool_parameters_from_record(value)
        if tool_name in {"view_file", "read_file"} and isinstance(parameters, dict):
            for raw in parameters.values():
                if isinstance(raw, str) and stale_path in raw.lower():
                    return True
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)) and visit(child):
                return True
        return False

    return visit(transcript)


def _shell_chain_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    operator = _first_unquoted_shell_chain_operator(command)
    if not operator:
        return None
    return {
        "code": SHELL_CHAIN_CONTRACT_VIOLATION,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "command",
        "operator": operator,
        "message": "Tool call run_shell_command chained multiple shell operations in one command.",
        "next_action": (
            "Reportar como bug de orquestração; emita uma tool call por comando e aguarde cada JSON/exit code."
        ),
    }


def _public_dev_escape_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not command:
        return None
    if not re.search(r"\bMEDNOTES_ALLOW_DEV_ESCAPE\s*=\s*(?:1|true|yes)\b", command, re.IGNORECASE):
        if not re.search(r"\b--skip-prompt-eval\b", command):
            return None
    return {
        "code": PUBLIC_DEV_ESCAPE_CONTRACT_VIOLATION,
        "severity": "high",
        "tool_name": "run_shell_command",
        "bad_param": "command",
        "message": "Tool call run_shell_command attempted to use a developer escape in a public workflow.",
        "next_action": (
            "Reportar como bug de orquestração; pare a execução pública e retome pela rota oficial "
            "com recibo/proveniência tipados, sem MEDNOTES_ALLOW_DEV_ESCAPE ou --skip-prompt-eval."
        ),
    }


def _style_rewrite_direct_content_apply_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not command or "apply-style-rewrite" not in command:
        return None
    if "--target" not in command or "--content" not in command or "--dry-run" in command:
        return None
    return {
        "code": STYLE_REWRITE_DIRECT_CONTENT_APPLY,
        "severity": "high",
        "tool_name": "run_shell_command",
        "bad_param": "command",
        "message": "Agent attempted to apply a style rewrite from loose --target/--content paths.",
        "next_action": (
            "Reportar como bug de orquestração; aplique reescrita médica somente por "
            "apply-specialist-style-rewrite com plan, manifest, work_id e recibo especialista oficial."
        ),
    }


def _style_rewrite_unverified_model_claim_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not command or "finalize-style-rewrite-output" not in command:
        return None
    if "--specialist-run-receipt" in command:
        return None
    if "--actual-model" not in command and "--provider" not in command:
        return None
    return {
        "code": STYLE_REWRITE_UNVERIFIED_MODEL_CLAIM,
        "severity": "high",
        "tool_name": "run_shell_command",
        "bad_param": "command",
        "message": "Agent attempted to finalize a specialist rewrite using parent-declared model provenance.",
        "next_action": (
            "Reportar como bug de orquestração; o parent não pode declarar Pro/Flash manualmente. "
            "Use somente specialist-task-run-receipt.v1 validado pelo Workbench."
        ),
    }


def _specialist_unverified_model_escape_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    field_name, text = _first_parameter_containing(parameters, _UNVERIFIED_SPECIALIST_MODEL_ENV)
    if not text:
        return None
    return {
        "code": SPECIALIST_UNVERIFIED_MODEL_ESCAPE,
        "severity": "high",
        "tool_name": tool_name,
        "bad_param": field_name,
        "message": "Agent attempted to enable the unverified specialist model escape during a public workflow.",
        "next_action": (
            "Reportar como bug de orquestração; reescrita médica pública só pode avançar com "
            "specialist-task-run-receipt.v1 validado, sem MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL."
        ),
    }


def _style_rewrite_parent_output_write_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name not in _WORKFLOW_ARTIFACT_WRITE_TOOLS:
        return None
    for field, path in _workflow_artifact_write_paths(parameters):
        if not _looks_like_style_rewrite_parent_output_path(path):
            continue
        return {
            "code": STYLE_REWRITE_PARENT_OUTPUT_WRITE,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": field,
            "path": path,
            "message": (
                "Agent directly wrote a style-rewrite output artifact that must be produced by "
                "the specialist runner and Workbench finalization commands."
            ),
            "next_action": (
                "Reportar como bug de autoria/recibo; o parent deve chamar o especialista oficial "
                "e depois apply-specialist-style-rewrite, nunca escrever .rewrite.md, attestation "
                "ou receipt por write_file/write_to_file."
            ),
        }
    return None


def _process_chats_raw_write_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name not in _WORKFLOW_ARTIFACT_WRITE_TOOLS:
        return None
    for field, path in _workflow_artifact_write_paths(parameters):
        if not _looks_like_chats_raw_path(path):
            continue
        return {
            "code": PROCESS_CHATS_RAW_WRITE,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": field,
            "path": path,
            "message": "Agent attempted to write a raw chat file during process-chats instead of using wiki/cli.py.",
            "next_action": (
                "Reportar como bug de integridade; raw chat body é imutável e YAML/status "
                "só pode ser mutado por wiki/cli.py triage/discard/publish-batch."
            ),
        }
    return None


def _process_chats_parent_artifact_write_without_subagent_finding(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    process_chats_specialist_seen: bool,
) -> dict[str, Any] | None:
    if process_chats_specialist_seen:
        return None
    if tool_name not in _WORKFLOW_ARTIFACT_WRITE_TOOLS:
        return None
    for field, path in _workflow_artifact_write_paths(parameters):
        if not _looks_like_process_chats_generated_artifact_path(path):
            continue
        return {
            "code": PROCESS_CHATS_PARENT_ARTIFACT_WRITE_WITHOUT_SUBAGENT,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": field,
            "path": path,
            "message": "Agent wrote a process-chats artifact before any specialist/subagent invocation.",
            "next_action": (
                "Reportar como bug de autoria; use plan-subagents e um subagent/runner oficial "
                "antes de salvar note_plan, coverage, manifest ou Markdown temporário."
            ),
        }
    return None


def _workflow_source_discovery_after_block_finding(tool_name: str, parameters: JsonObject) -> JsonObject | None:
    command = _shell_command_text(parameters) if tool_name == "run_shell_command" else ""
    file_path = ""
    if tool_name in {"read_file", "view_file"}:
        _field, file_path = _first_string_parameter(parameters, ("file_path", "path", "absolute_path"))
    if command:
        lowered_command = command.lower()
        source_probe = bool(
            "bundle/scripts/mednotes" in lowered_command
            and (
                (
                    re.search(r"\b(?:grep|rg)\b", lowered_command)
                    and (
                        "mednotes_allow_dev_escape" in lowered_command
                        or "specialist-task-run-receipt" in lowered_command
                        or "apply-style-rewrite" in lowered_command
                        or "finalize-style-rewrite-output" in lowered_command
                    )
                )
                or (
                    re.search(r"\b(?:cat|sed|nl|head|tail|less)\b", lowered_command)
                    and re.search(r"bundle/scripts/mednotes/[^\"' ]+\.py\b", lowered_command)
                )
            )
        )
    else:
        lowered_path = file_path.lower()
        source_probe = "bundle/scripts/mednotes/" in lowered_path and lowered_path.endswith(".py")
    if not source_probe:
        return None
    return {
        "code": WORKFLOW_SOURCE_DISCOVERY_AFTER_BLOCK,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "command" if command else "file_path",
        "message": "Agent inspected Workbench source code while executing a public workflow instead of following the typed payload.",
        "next_action": (
            "Reportar como atrito de UX; o workflow deve oferecer continuação oficial ou bloqueio terminal, "
            "sem induzir o agente a procurar bypass em código-fonte."
        ),
    }


def _python_environment_probe_finding(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not re.search(r"\bpython(?:3(?:\.\d+)?)?\s+-c\b", command):
        return None
    if not any(
        marker in command for marker in ("MEDNOTES", "MEDICAL_NOTES_WORKBENCH", "GEMINI", "medical-notes-workbench")
    ):
        return None
    if "uv run" in command or "scripts/run_python.mjs" in command or "wiki/cli.py" in command:
        return None
    return {
        "code": NONCANONICAL_PYTHON_ENVIRONMENT_PROBE,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "command",
        "message": "Tool call run_shell_command used ad hoc python -c to inspect Workbench environment state.",
        "next_action": (
            "Reportar como desvio operacional; use a rota oficial do Workbench ou um recibo tipado, "
            "e inclua o probe no relatório final se ele já ocorreu."
        ),
    }


def _workflow_artifact_direct_write_finding(tool_name: str, parameters: JsonObject) -> JsonObject | None:
    if tool_name not in _WORKFLOW_ARTIFACT_WRITE_TOOLS:
        return None
    for field, path in _workflow_artifact_write_paths(parameters):
        if not _looks_like_workflow_artifact_path(path):
            continue
        return {
            "code": WORKFLOW_ARTIFACT_DIRECT_WRITE,
            "severity": "high",
            "tool_name": tool_name,
            "bad_param": field,
            "path": path,
            "message": f"Tool call {tool_name} directly modified a workflow artifact that must be produced by wiki/cli.py.",
            "next_action": (
                "Reportar como bug de orquestração; regenere o artefato pela rota oficial do "
                "wiki/cli.py e não use write_file/replace/edit para plans, manifests, receipts ou reports."
            ),
        }
    return None


def _workflow_artifact_shell_copy_finding(tool_name: str, parameters: JsonObject) -> JsonObject | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not command or not re.search(r"\b(cp|copy|Copy-Item)\b", command):
        return None
    normalized = command.replace("\\", "/")
    if "/.gemini/antigravity-cli/scratch/" not in normalized:
        return None
    artifact_names = _workflow_artifact_names_in_command(normalized)
    if not artifact_names:
        return None
    return {
        "code": WORKFLOW_ARTIFACT_SHELL_COPY,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "command",
        "artifact_names": artifact_names,
        "message": "Tool call run_shell_command copied workflow artifacts into AGY scratch outside the official run directory.",
        "next_action": (
            "Reportar como contaminação do experimento; preserve artefatos na pasta oficial da rodada "
            "ou no diretório lab artifact, não em scratch global."
        ),
    }


def _workflow_artifact_shell_redirect_finding(tool_name: str, parameters: JsonObject) -> JsonObject | None:
    if tool_name != "run_shell_command":
        return None
    command = _shell_command_text(parameters)
    if not command or not _has_shell_stdout_redirect(command):
        return None
    normalized = command.replace("\\", "/")
    if "/.gemini/antigravity-cli/scratch/" not in normalized:
        return None
    artifact_names = _workflow_artifact_names_in_command(normalized)
    if not artifact_names:
        return None
    return {
        "code": WORKFLOW_ARTIFACT_SHELL_REDIRECT,
        "severity": "medium",
        "tool_name": tool_name,
        "bad_param": "command",
        "artifact_names": artifact_names,
        "message": "Tool call run_shell_command redirected workflow output into AGY scratch outside the official run directory.",
        "next_action": (
            "Reportar como contaminação do experimento; use os artefatos oficiais emitidos pelo workflow "
            "ou o diretório lab artifact, não scratch global."
        ),
    }


def _duplicate_workflow_command_findings(transcript: object) -> list[JsonObject]:
    counts: dict[tuple[str, str], int] = {}
    for tool_name, parameters in _iter_agent_tool_calls(transcript):
        if _canonical_tool_name(tool_name) != "run_shell_command":
            continue
        command = _shell_command_text(parameters)
        workflow_key = _workflow_command_key(command)
        if not workflow_key:
            continue
        counts[workflow_key] = counts.get(workflow_key, 0) + 1

    findings: list[JsonObject] = []
    for (workflow, mode), count in sorted(counts.items()):
        if count <= 1:
            continue
        findings.append(
            {
                "code": DUPLICATE_WORKFLOW_COMMAND,
                "severity": "medium",
                "tool_name": "run_shell_command",
                "workflow": workflow,
                "mode": mode,
                "count": count,
                "message": f"Agent invoked the same {workflow} {mode} workflow more than once in one session.",
                "next_action": (
                    "Reportar como desvio operacional; leia compact_report/full_report ou artefatos oficiais em vez de repetir "
                    "o workflow sem mudança de entrada."
                ),
            }
        )
    return findings


def _workflow_command_key(command: str) -> tuple[str, str] | None:
    if not command:
        return None
    if re.search(r"(?:^|[\s\"'])fix-wiki(?:\s|$)", command) and "--dry-run" in command and "--apply" not in command:
        return ("/mednotes:fix-wiki", "preview")
    return None


def _final_artifact_path_findings(transcript: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in _final_response_file_uri_paths(transcript):
        if not _looks_like_reported_workflow_artifact_path(path):
            continue
        if Path(path).exists():
            continue
        findings.append(
            {
                "code": FINAL_ARTIFACT_PATH_INVALID,
                "severity": "medium",
                "tool_name": "planner_response",
                "bad_param": "content",
                "path": path,
                "artifact_name": path.replace("\\", "/").rsplit("/", 1)[-1],
                "message": "Agent final response linked a workflow artifact path that does not exist.",
                "next_action": (
                    "Reportar como bug de relatório final; use somente os caminhos oficiais emitidos pelo workflow "
                    "e confira existência antes de publicar links de artefato."
                ),
            }
        )
    return findings


def _manual_packaged_subagent_findings(transcript: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    valid_packaged_definitions: set[str] = set()

    def append(agent_name: str) -> None:
        if agent_name not in _PACKAGED_SPECIALIST_AGENTS:
            return
        if agent_name in valid_packaged_definitions:
            return
        if agent_name in seen:
            return
        seen.add(agent_name)
        findings.append(
            {
                "code": MANUAL_SUBAGENT_CONTRACT_VIOLATION,
                "severity": "high",
                "tool_name": "define_subagent",
                "bad_param": "agent_name",
                "agent_name": agent_name,
                "message": f"Agent manually defined the packaged {agent_name} instead of using the bundled agent.",
                "next_action": (
                    "Reportar como bug de orquestração/modelo; para style_rewrite no AGY, leia o template "
                    "empacotado completo, use define_subagent autorizado e finalize com finalize-agy-specialist-task."
                ),
            }
        )

    def is_authorized_template_definition(agent_name: str, parameters: dict[str, Any]) -> bool:
        markers = _PACKAGED_SPECIALIST_AGENT_TEMPLATE_MARKERS.get(agent_name)
        if not markers:
            return False
        prompt_parts = [
            str(parameters.get(field) or "")
            for field in _SUBAGENT_SYSTEM_PROMPT_PARAMETER_FIELDS
            if parameters.get(field)
        ]
        prompt = "\n".join(prompt_parts)
        return bool(prompt) and all(marker in prompt for marker in markers)

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        tool_name = _canonical_tool_name(_tool_name_from_record(value))
        parameters = _tool_parameters_from_record(value)
        if tool_name in _SUBAGENT_DEFINITION_TOOLS and parameters is not None:
            _, agent_name = _first_string_parameter(parameters, _AGENT_NAME_PARAMETER_FIELDS)
            if is_authorized_template_definition(agent_name, parameters):
                valid_packaged_definitions.add(agent_name)
                return
            append(agent_name)
        raw_text = str(value.get("content") or value.get("message") or value.get("text") or "")
        folded = raw_text.casefold()
        for agent_name in _PACKAGED_SPECIALIST_AGENTS:
            if f'subagent "{agent_name}" defined successfully' in folded:
                append(agent_name)
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(transcript)
    return findings


def _agy_hidden_workspace_findings(transcript: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if _AGY_HIDDEN_WORKSPACE_RE.search(value):
                findings.append(
                    {
                        "code": WORKSPACE_ADD_DIR_HIDDEN_IGNORED,
                        "severity": "high",
                        "tool_name": "add_workspace_folder",
                        "bad_param": "path",
                        "message": "AGY ignored an --add-dir/workspace folder because the path is hidden.",
                        "next_action": (
                            "Preparar o vault de experimento em diretório visível ao AGY e repetir a rodada; "
                            "não contorne a falha lendo e colando conteúdo bruto no subagente."
                        ),
                    }
                )
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(transcript)
    return findings


def _final_response_file_uri_paths(transcript: Any) -> list[str]:
    paths: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        event_type = str(value.get("type") or "").upper()
        if event_type == "PLANNER_RESPONSE":
            for field in ("content", "text", "message", "response"):
                raw = value.get(field)
                if isinstance(raw, str):
                    paths.extend(_file_uri_paths_in_text(raw))
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(transcript)
    return paths


def _file_uri_paths_in_text(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"file://(?P<path>/[^\s)\]`>\"']+)", text):
        raw_path = unquote(match.group("path")).rstrip(".,;")
        if raw_path and raw_path not in paths:
            paths.append(raw_path)
    return paths


def _workflow_artifact_names_in_command(command: str) -> list[str]:
    return sorted(name for name in _WORKFLOW_ARTIFACT_SCRATCH_NAMES if name in command)


def _has_shell_stdout_redirect(command: str) -> bool:
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == ">":
            return True
        index += 1
    return bool(re.search(r"\b(?:Out-File|Set-Content)\b", command))


def _workflow_artifact_write_paths(parameters: JsonObject) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for field in _WORKFLOW_ARTIFACT_PATH_FIELDS:
        value = parameters.get(field)
        if isinstance(value, str) and value.strip():
            paths.append((field, value.strip()))
    return paths


def _shell_command_text(parameters: dict[str, Any]) -> str:
    for field in _SHELL_COMMAND_PARAMETER_FIELDS:
        value = parameters.get(field)
        if isinstance(value, str) and value.strip():
            return _unwrap_tool_command_line(value.strip())
    return ""


def _unwrap_tool_command_line(command: str) -> str:
    if len(command) < 2 or command[0] != command[-1] or command[0] not in {"'", '"'}:
        return command
    quote = command[0]
    unwrapped = command[1:-1]
    if quote == '"':
        return unwrapped.replace('\\"', '"')
    return unwrapped.replace("\\'", "'")


def _transcript_contains(transcript: Any, needle: str) -> bool:
    needle = needle.lower()

    def visit(value: Any) -> bool:
        if isinstance(value, str):
            return needle in value.lower()
        if isinstance(value, list):
            return any(visit(item) for item in value)
        if isinstance(value, dict):
            return any(visit(str(key)) or visit(item) for key, item in value.items())
        return False

    return visit(transcript)


def _first_parameter_containing(parameters: dict[str, Any], needle: str) -> tuple[str, str]:
    folded_needle = needle.casefold()
    for field, value in parameters.items():
        if not isinstance(value, str):
            continue
        if folded_needle in value.casefold():
            return field, value
    return "", ""


def _normalized_operational_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _looks_like_workflow_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return False
    return "/runs/" in normalized and bool(_WORKFLOW_ARTIFACT_NAME_RE.search(name))


def _looks_like_style_rewrite_parent_output_path(path: str) -> bool:
    normalized = _normalized_operational_path(path)
    if "/tmp/agent-work/fix-wiki/style-rewrite-" not in normalized:
        return False
    return normalized.endswith(
        (
            ".rewrite.md",
            ".rewrite.md.attestation.json",
            ".rewrite.md.receipt.json",
            ".style-rewrite-output.json",
        )
    )


def _looks_like_chats_raw_path(path: str) -> bool:
    return "/chats_raw/" in _normalized_operational_path(path)


def _looks_like_process_chats_generated_artifact_path(path: str) -> bool:
    normalized = _normalized_operational_path(path)
    if "/process-chats/" not in normalized:
        return False
    return normalized.endswith(_PROCESS_CHATS_ARTIFACT_SUFFIXES)


def _is_process_chats_specialist_invocation(tool_name: str) -> bool:
    return tool_name in {"invoke_agent", "invoke_subagent", "send_message"}


def _looks_like_reported_workflow_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return name in _WORKFLOW_ARTIFACT_SCRATCH_NAMES or _looks_like_workflow_artifact_path(path)


def _first_unquoted_shell_chain_operator(command: str) -> str:
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if command.startswith("&&", index):
            return "&&"
        if command.startswith("||", index):
            return "||"
        if char == ";":
            return ";"
        if char == "\n" and command[:index].strip() and command[index + 1 :].strip():
            return "newline"
        index += 1
    return ""


def _iter_agent_tool_calls(node: Any) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        tool_name = _tool_name_from_record(value)
        parameters = _tool_parameters_from_record(value)
        if tool_name and parameters is not None:
            calls.append((tool_name, parameters))
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(node)
    return calls


def _iter_tool_call_batches(node: Any) -> list[list[tuple[str, dict[str, Any]]]]:
    batches: list[list[tuple[str, dict[str, Any]]]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key in ("tool_calls", "toolCalls", "calls"):
            raw_batch = value.get(key)
            if not isinstance(raw_batch, list):
                continue
            batch: list[tuple[str, dict[str, Any]]] = []
            for item in raw_batch:
                if not isinstance(item, dict):
                    continue
                tool_name = _tool_name_from_record(item)
                parameters = _tool_parameters_from_record(item)
                if tool_name and parameters is not None:
                    batch.append((tool_name, parameters))
            if batch:
                batches.append(batch)
        for key in _TRANSCRIPT_CHILD_CONTAINER_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    visit(node)
    return batches


def _tool_name_from_record(record: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name", "tool", "recipient_name", "recipient"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_parameters_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("parameters", "args", "arguments", "tool_input", "toolInput", "input"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return None


def _canonical_tool_name(tool_name: str) -> str:
    normalized = str(tool_name or "").replace("functions.", "").replace("tools.", "")
    normalized = normalized.replace(" ", "_").lower()
    if normalized in _RUN_SHELL_TOOL_ALIASES:
        return "run_shell_command"
    return normalized
