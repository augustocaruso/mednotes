"""Audit real harness transcripts against the FSM-first workflow contract.

This module intentionally normalizes raw Gemini/AGY/OpenCode transcript shapes
into typed observations before applying policy. Adapters and transcript parsers
detect facts; audit rules decide whether those facts are deviations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.contracts.agent_run_audit import (
    AgentTranscriptSource,
    AuditConfidence,
    AuditSeverity,
    AuditStatus,
    AuditWorkflow,
    CanonicalArtifactKind,
    CanonicalArtifactObservation,
    HardeningRecommendation,
    RecommendedAction,
    SubagentInvocationObservation,
    ToolCallObservation,
    WorkflowDeviationFinding,
    WorkflowTranscriptAuditResult,
    WorkflowTranscriptAuditSummary,
)
from mednotes.kernel.base import JsonValue

RawJsonObject = dict[str, JsonValue]

_TRANSCRIPT_CHILD_KEYS = (
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
_RAW_CONTENT_MARKERS = ("RAW_CHAT_BODY_START", "Chats_Raw content:", "```markdown", "<html", "<!doctype html")
_ARTIFACT_TOKEN = r"[^ \t\r\n\"'<>|;]*"
_LOCAL_PATH_PLACEHOLDER = "<local-path>"
_SENSITIVE_OUTPUT_PLACEHOLDER = "<redacted-sensitive-output>"
_AGY_CONFIG_SKILLS_PATH_RE = re.compile(
    (
        r"(?:/Users/[^\s\"']*?|/private/tmp/[^\s\"']*?|/tmp/[^\s\"']*?|"
        r"\b[A-Za-z]:(?:\\{1,2})Users(?:\\{1,2})[^\s\"']*?)"
        r"(?P<suffix>(?:[/\\]{1,2})\.gemini(?:[/\\]{1,2})config(?:[/\\]{1,2})skills"
        r"(?:[/\\]{1,2})[^\s\"']+)"
    ),
    re.IGNORECASE,
)
_POSIX_LOCAL_PATH_RE = re.compile(r"/Users/[^\s\"']+|/private/tmp/[^\s\"']+|/tmp/[^\s\"']+")
_WINDOWS_USER_PATH_RE = re.compile(r"\b[A-Za-z]:(?:\\{1,2})Users(?:\\{1,2})[^\s\"']+")
_CANONICAL_ARTIFACT_PATTERNS: tuple[tuple[re.Pattern[str], CanonicalArtifactKind], ...] = (
    (
        re.compile(rf"(?P<path>{_ARTIFACT_TOKEN}triage-note-plan{_ARTIFACT_TOKEN}\.json)", re.IGNORECASE),
        "triage_note_plan",
    ),
    (
        re.compile(rf"(?P<path>{_ARTIFACT_TOKEN}raw-coverage{_ARTIFACT_TOKEN}\.json)", re.IGNORECASE),
        "raw_coverage",
    ),
    (
        re.compile(rf"(?P<path>{_ARTIFACT_TOKEN}manifest{_ARTIFACT_TOKEN}\.json)", re.IGNORECASE),
        "manifest",
    ),
    (
        re.compile(rf"(?P<path>{_ARTIFACT_TOKEN}receipt{_ARTIFACT_TOKEN}\.json)", re.IGNORECASE),
        "receipt",
    ),
    (
        re.compile(rf"(?P<path>{_ARTIFACT_TOKEN}\.rewrite\.md)", re.IGNORECASE),
        "style_rewrite_output",
    ),
    (
        re.compile(
            rf"(?P<path>{_ARTIFACT_TOKEN}(?:pendencias|pendencias_processor|human-decision){_ARTIFACT_TOKEN}\.md)",
            re.IGNORECASE,
        ),
        "human_decision_backlog",
    ),
)
_SHELL_TOOL_NAMES = {"run_shell_command", "bash", "shell", "run_command", "run_shell"}
_DISCOVERY_TOOL_NAMES = {
    "glob",
    "grep",
    "grep_search",
    "list_dir",
    "list_directory",
    "read",
    "read_file",
    "search_file_content",
    "view_file",
}
_DISCOVERY_SHELL_COMMAND_RE = re.compile(r"^\s*(?:rg|grep|find|ls|cat|sed)\b")


def audit_agent_transcript(
    *,
    transcript_path: Path | None,
    workflow: AuditWorkflow = "unknown",
    workflow_payload_path: Path | None = None,
    final_report_path: Path | None = None,
    runtime_log_paths: list[Path] | None = None,
) -> WorkflowTranscriptAuditResult:
    runtime_log_paths = runtime_log_paths or []
    findings: list[WorkflowDeviationFinding] = []
    sources = _sources(transcript_path, workflow_payload_path, final_report_path, runtime_log_paths)
    transcript: JsonValue | None = None
    if transcript_path is not None:
        try:
            transcript = _read_transcript(transcript_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PydanticValidationError) as exc:
            findings.append(
                _finding(
                    code="agent.transcript_unreadable",
                    workflow=workflow,
                    severity="blocking_candidate",
                    confidence="high",
                    evidence_ref="transcript:load",
                    expected_contract="transcript must be readable JSON, JSONL, or text for post-run audit",
                    observed_behavior=f"transcript could not be parsed: {type(exc).__name__}",
                    recommended_action="test_fixture",
                    promotion_gate="keep as validation blocker for unreadable transcript inputs",
                )
            )
    if transcript is not None:
        observations = _collect_observations(transcript)
        findings.extend(_detect_deviations(observations, workflow=workflow))
        if workflow_payload_path is None:
            findings.extend(_workflow_payload_missing_findings(transcript, workflow=workflow))
    return _result(
        workflow=workflow,
        transcript_present=transcript_path is not None,
        workflow_payload_present=workflow_payload_path is not None,
        final_report_present=final_report_path is not None,
        findings=findings,
        sources=sources,
    )


def _sources(
    transcript_path: Path | None,
    workflow_payload_path: Path | None,
    final_report_path: Path | None,
    runtime_log_paths: list[Path],
) -> list[AgentTranscriptSource]:
    items: list[AgentTranscriptSource] = [
        _source(transcript_path, "transcript"),
        _source(workflow_payload_path, "workflow_payload"),
        _source(final_report_path, "final_report"),
    ]
    items.extend(_source(path, "runtime_log") for path in runtime_log_paths)
    return items


def _source(
    path: Path | None,
    source_kind: Literal["transcript", "workflow_payload", "final_report", "runtime_log"],
) -> AgentTranscriptSource:
    present = path is not None and path.exists()
    suffix = path.suffix.lower() if path is not None else ""
    fmt: Literal["json", "jsonl", "text", "missing", "unknown"]
    if not present:
        fmt = "missing"
    elif suffix == ".json":
        fmt = "json"
    elif suffix in {".jsonl", ".ndjson"}:
        fmt = "jsonl"
    elif suffix in {".txt", ".log", ".md"}:
        fmt = "text"
    else:
        fmt = "unknown"
    return AgentTranscriptSource(
        path_label=path.name if path is not None else "",
        source_kind=source_kind,
        present=present,
        format=fmt,
    )


def _read_transcript(path: Path) -> JsonValue:
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return cast(JsonValue, json.loads(text))
    if suffix in {".jsonl", ".ndjson"}:
        records: list[JsonValue] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(cast(JsonValue, json.loads(stripped)))
            except json.JSONDecodeError as exc:
                raise json.JSONDecodeError(f"{exc.msg} at JSONL line {line_number}", exc.doc, exc.pos) from exc
        return {"records": records}
    records = _json_records_from_mixed_text(text)
    if records:
        return {"records": records}
    return {"records": [{"type": "text", "text": text}]}


def _json_records_from_mixed_text(text: str) -> list[JsonValue]:
    records: list[JsonValue] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith(("{", "[")):
            continue
        try:
            records.append(cast(JsonValue, json.loads(stripped)))
        except json.JSONDecodeError:
            continue
    return records


def _collect_observations(
    transcript: JsonValue,
) -> list[ToolCallObservation | SubagentInvocationObservation | CanonicalArtifactObservation]:
    observations: list[ToolCallObservation | SubagentInvocationObservation | CanonicalArtifactObservation] = []
    subagent_seen = False
    for index, record in enumerate(_iter_records(transcript)):
        tool_name = _tool_name(record)
        if not tool_name:
            continue
        params = _tool_parameters(record)
        status = _tool_status(record)
        raw_command_text = _command_text(tool_name, params)
        raw_target_path = _target_path(params)
        raw_output_text = _output_text(record)
        command_text = _redacted_text(raw_command_text)
        target_path = _redacted_text(raw_target_path)
        output_excerpt = _redacted_excerpt(raw_output_text)
        target_is_agy_config_skill = _is_agy_config_skills_path(raw_target_path)
        observations.append(
            ToolCallObservation(
                index=index,
                tool_name=tool_name,
                status=status,
                command_text=command_text,
                target_path=target_path,
                output_excerpt=output_excerpt,
                agent_effect_pending_signal=_has_executable_agent_effect_signal(raw_output_text),
                target_is_agy_config_skill=target_is_agy_config_skill,
                stale_materialized_skill_signal=_has_stale_materialized_skill_signal(raw_output_text),
                parameter_keys=sorted(params),
            )
        )
        if tool_name in {"invoke_agent", "invoke_subagent", "send_message"}:
            prompt = str(params.get("prompt") or params.get("message") or params.get("content") or "")
            observations.append(
                SubagentInvocationObservation(
                    index=index,
                    tool_name=tool_name,
                    agent_name=str(params.get("agent") or params.get("agent_name") or ""),
                    prompt_length=len(prompt),
                    has_work_item='"work_item"' in prompt or "work_item" in params,
                    has_raw_content_markers=_has_raw_content_markers(prompt),
                )
            )
            subagent_seen = True
        artifact = _artifact_match(raw_target_path or raw_command_text)
        if artifact:
            artifact_kind, artifact_path = artifact
            observations.append(
                CanonicalArtifactObservation(
                    index=index,
                    tool_name=tool_name,
                    path=artifact_path,
                    artifact_kind=artifact_kind,
                    after_subagent=subagent_seen,
                )
            )
    return observations


def _iter_records(node: JsonValue) -> list[RawJsonObject]:
    records: list[RawJsonObject] = []
    if isinstance(node, dict):
        if _tool_name(node):
            records.append(node)
        for key in _TRANSCRIPT_CHILD_KEYS:
            child = node.get(key)
            if child is not None:
                records.extend(_iter_records(child))
    elif isinstance(node, list):
        for item in node:
            records.extend(_iter_records(item))
    return records


def _tool_name(record: RawJsonObject) -> str:
    for key in ("tool_name", "name", "function_name", "recipient_name", "tool"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value.rsplit(".", 1)[-1]
    call = record.get("tool_call")
    if isinstance(call, dict):
        return _tool_name(call)
    part = record.get("part")
    if isinstance(part, dict):
        return _tool_name(cast(RawJsonObject, part))
    return ""


def _tool_parameters(record: RawJsonObject) -> RawJsonObject:
    for key in ("arguments", "args", "parameters", "input"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = cast(JsonValue, json.loads(value))
            except json.JSONDecodeError:
                return {"text": value}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
    for state in _state_objects(record):
        value = state.get("input")
        if isinstance(value, dict):
            return cast(RawJsonObject, value)
    call = record.get("tool_call")
    if isinstance(call, dict):
        return _tool_parameters(call)
    part = record.get("part")
    if isinstance(part, dict):
        return _tool_parameters(cast(RawJsonObject, part))
    return {}


def _tool_status(record: RawJsonObject) -> str:
    for key in ("status", "state"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    for state in _state_objects(record):
        value = state.get("status")
        if isinstance(value, str):
            return value
    return ""


def _command_text(tool_name: str, params: RawJsonObject) -> str:
    if tool_name not in _SHELL_TOOL_NAMES:
        return ""
    for key in ("command", "cmd", "CommandLine", "commandLine"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return ""


def _target_path(params: RawJsonObject) -> str:
    for key in (
        "path",
        "file_path",
        "filePath",
        "absolute_path",
        "absolutePath",
        "target_path",
        "targetPath",
        "output_path",
        "outputPath",
        "SearchDirectory",
        "searchDirectory",
        "DirectoryPath",
        "directoryPath",
    ):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return ""


def _output_text(record: RawJsonObject) -> str:
    result = record.get("result")
    if isinstance(result, dict):
        parts = [str(result.get(key) or "") for key in ("stdout", "stderr", "output", "text")]
        return "\n".join(part for part in parts if part)
    if isinstance(result, str):
        return result
    for state in _state_objects(record):
        parts = [str(state.get(key) or "") for key in ("stdout", "stderr", "output", "text")]
        text = "\n".join(part for part in parts if part)
        if text:
            return text
    return str(record.get("output") or record.get("text") or "")


def _state_objects(record: RawJsonObject) -> list[RawJsonObject]:
    """Return runtime state objects without treating nested args as tool calls."""

    states: list[RawJsonObject] = []
    value = record.get("state")
    if isinstance(value, dict):
        states.append(cast(RawJsonObject, value))
    part = record.get("part")
    if isinstance(part, dict):
        part_state = part.get("state")
        if isinstance(part_state, dict):
            states.append(cast(RawJsonObject, part_state))
    return states


def _has_executable_agent_effect_signal(text: str) -> bool:
    """Detect FSM-to-agent continuation evidence inside raw tool output."""

    if not text:
        return False
    try:
        payload = cast(JsonValue, json.loads(text))
    except json.JSONDecodeError:
        lowered = text.lower()
        return "agent_directive" in lowered and "waiting_agent" in lowered and "effects" in lowered
    return _json_contains_executable_agent_effect(payload)


def _json_contains_executable_agent_effect(node: JsonValue) -> bool:
    if isinstance(node, dict):
        directive = node.get("agent_directive")
        if isinstance(directive, dict) and _directive_has_executable_effect(cast(RawJsonObject, directive)):
            return True
        return any(_json_contains_executable_agent_effect(value) for value in node.values())
    if isinstance(node, list):
        return any(_json_contains_executable_agent_effect(item) for item in node)
    return False


def _directive_has_executable_effect(directive: RawJsonObject) -> bool:
    control = directive.get("control")
    if not isinstance(control, dict):
        return False
    status = control.get("status")
    can_continue = control.get("can_continue_now")
    effects = control.get("effects")
    if status != "waiting_agent" or can_continue is not True or not isinstance(effects, list):
        return False
    return any(isinstance(effect, dict) and effect.get("kind") for effect in effects)


def _redacted_excerpt(text: str, *, limit: int = 240) -> str:
    if _has_raw_content_markers(text):
        return _SENSITIVE_OUTPUT_PLACEHOLDER
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    cleaned = _redacted_text(cleaned)
    return cleaned[:limit]


def _redacted_text(text: str) -> str:
    redacted = _AGY_CONFIG_SKILLS_PATH_RE.sub(_redacted_agy_config_skills_path, text)
    redacted = _POSIX_LOCAL_PATH_RE.sub(_LOCAL_PATH_PLACEHOLDER, redacted)
    return _WINDOWS_USER_PATH_RE.sub(_LOCAL_PATH_PLACEHOLDER, redacted)


def _redacted_agy_config_skills_path(match: re.Match[str]) -> str:
    suffix = match.group("suffix").replace("\\", "/")
    suffix = re.sub(r"/+", "/", suffix)
    return f"{_LOCAL_PATH_PLACEHOLDER}{suffix}"


def _is_agy_config_skills_path(text: str) -> bool:
    normalized = re.sub(r"/+", "/", text.replace("\\", "/")).lower()
    return "/.gemini/config/skills/" in normalized


def _has_raw_content_markers(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _RAW_CONTENT_MARKERS) or len(text) > 8000


def _has_stale_materialized_skill_signal(text: str) -> bool:
    lowered = text.lower()
    return "stale" in lowered and ("materialized skill" in lowered or "config/skills" in lowered)


def _artifact_match(text: str) -> tuple[CanonicalArtifactKind, str] | None:
    for pattern, kind in _CANONICAL_ARTIFACT_PATTERNS:
        match = pattern.search(text)
        if match:
            return kind, _redacted_text(match.group("path"))
    return None


def _detect_deviations(
    observations: list[ToolCallObservation | SubagentInvocationObservation | CanonicalArtifactObservation],
    *,
    workflow: AuditWorkflow,
) -> list[WorkflowDeviationFinding]:
    findings: list[WorkflowDeviationFinding] = []
    seen: set[tuple[str, str]] = set()
    fsm_effect_pending = False
    for observation in observations:
        if isinstance(observation, ToolCallObservation) and fsm_effect_pending:
            finding = _discovery_tool_while_effect_pending_finding(observation, workflow=workflow)
            if finding is not None:
                key = (finding.code, finding.evidence_ref)
                if key not in seen:
                    seen.add(key)
                    findings.append(finding)
        for finding in _findings_for_observation(observation, workflow=workflow):
            key = (finding.code, finding.evidence_ref)
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)
        if isinstance(observation, ToolCallObservation) and observation.agent_effect_pending_signal:
            fsm_effect_pending = True
    return findings


def _workflow_payload_missing_findings(transcript: JsonValue, *, workflow: AuditWorkflow) -> list[WorkflowDeviationFinding]:
    if not _transcript_invokes_workflow(transcript, workflow=workflow):
        return []
    return [
        _finding(
            code="agent.workflow_payload_missing",
            workflow=workflow,
            severity="blocking_candidate",
            confidence="high",
            evidence_ref="transcript:workflow-invocation",
            expected_contract="public workflow invocation must produce an official workflow payload before final audit can pass",
            observed_behavior="transcript invoked a public workflow but no workflow payload path was provided or discovered",
            recommended_action="runtime_guardrail",
            promotion_gate="block the run and repeat only after the agent reaches the official workflow JSON payload",
        )
    ]


def _transcript_invokes_workflow(transcript: JsonValue, *, workflow: AuditWorkflow) -> bool:
    text = "\n".join(_text_fragments(transcript)).lower()
    if not text:
        return False
    if workflow == "fix-wiki":
        return "/mednotes:fix-wiki" in text or "fix-wiki --apply" in text or "fix-wiki --dry-run" in text
    if workflow == "process-chats":
        return "/mednotes:process-chats" in text or "process-chats" in text or "process-medical-chats" in text
    if workflow == "link":
        return "/mednotes:link" in text
    return any(marker in text for marker in ("/mednotes:", "/flashcards"))


def _text_fragments(node: JsonValue) -> list[str]:
    fragments: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key in {"content", "text", "message", "prompt", "command", "CommandLine"}:
                fragments.append(value)
            elif isinstance(value, (dict, list)):
                fragments.extend(_text_fragments(value))
    elif isinstance(node, list):
        for item in node:
            fragments.extend(_text_fragments(item))
    return fragments


def _findings_for_observation(
    observation: ToolCallObservation | SubagentInvocationObservation | CanonicalArtifactObservation,
    *,
    workflow: AuditWorkflow,
) -> list[WorkflowDeviationFinding]:
    if isinstance(observation, SubagentInvocationObservation):
        return _subagent_findings(observation, workflow=workflow)
    if isinstance(observation, CanonicalArtifactObservation):
        return _canonical_artifact_findings(observation, workflow=workflow)
    return _tool_observation_findings(observation, workflow=workflow)


def _subagent_findings(
    observation: SubagentInvocationObservation,
    *,
    workflow: AuditWorkflow,
) -> list[WorkflowDeviationFinding]:
    if not observation.has_raw_content_markers:
        return []
    return [
        _finding(
            code="agent.subagent_raw_content_contract_violation",
            workflow=workflow,
            severity="blocking_candidate",
            confidence="high",
            evidence_ref=f"tool:{observation.index}",
            expected_contract=(
                "subagents receive work_item, official paths, hashes, and output paths instead of raw clinical content"
            ),
            observed_behavior=(
                f"{observation.tool_name} prompt length was {observation.prompt_length} and included raw-content markers"
            ),
            recommended_action="runtime_guardrail",
            promotion_gate="block immediately once a synthetic fixture covers the marker and long-prompt path",
        )
    ]


def _canonical_artifact_findings(
    observation: CanonicalArtifactObservation,
    *,
    workflow: AuditWorkflow,
) -> list[WorkflowDeviationFinding]:
    if observation.tool_name not in {"write_file", "write_to_file", "replace"}:
        return []
    if observation.artifact_kind == "human_decision_backlog":
        return []
    code = (
        "agent.parent_canonical_artifact_write_after_subagent"
        if observation.after_subagent
        else "agent.parent_canonical_artifact_write_before_subagent"
    )
    return [
        _finding(
            code=code,
            workflow=workflow,
            severity="contract_violation",
            confidence="high",
            evidence_ref=f"tool:{observation.index}",
            expected_contract=(
                "canonical workflow artifacts are produced by official CLI, typed adapters, or signed subagent outputs"
            ),
            observed_behavior=f"parent used {observation.tool_name} for {observation.artifact_kind}",
            recommended_action="test_fixture",
            promotion_gate=(
                "promote to runtime guardrail when the artifact can drive publish, apply, coverage, linker, "
                "or report status"
            ),
        )
    ]


def _tool_observation_findings(
    observation: ToolCallObservation,
    *,
    workflow: AuditWorkflow,
) -> list[WorkflowDeviationFinding]:
    findings: list[WorkflowDeviationFinding] = []
    if _is_recoverable_tool_error(observation):
        findings.append(
            _finding(
                code="agent.recoverable_tool_error_observed",
                workflow=workflow,
                severity="warning",
                confidence="medium",
                evidence_ref=f"tool:{observation.index}",
                expected_contract=(
                    "recoverable tool errors are reported as execution friction without becoming primary workflow failure"
                ),
                observed_behavior=f"{observation.tool_name} returned status={observation.status}",
                recommended_action="document",
                promotion_gate=(
                    "promote only if retry hides the error from final report or mutates state through an unofficial path"
                ),
            )
        )
    if _is_agy_materialized_skill_stale_instruction(observation):
        findings.append(
            _finding(
                code="agent.agy_materialized_skill_misclassified_as_stale",
                workflow=workflow,
                severity="warning",
                confidence="high",
                evidence_ref=f"tool:{observation.index}",
                expected_contract="AGY config/skills materialization is acceptable when it is the native runtime surface",
                observed_behavior="skill text instructs the agent to treat config/skills as stale context",
                recommended_action="prompt_hardening",
                promotion_gate="promote when the instruction causes bypass of activate_skill or native AGY orchestration",
            )
        )
    return findings


def _discovery_tool_while_effect_pending_finding(
    observation: ToolCallObservation,
    *,
    workflow: AuditWorkflow,
) -> WorkflowDeviationFinding | None:
    if not _is_discovery_tool_while_fsm_effect_pending(observation):
        return None
    return _finding(
        code="agent.discovery_tool_while_fsm_effect_pending",
        workflow=workflow,
        severity="blocking_candidate",
        confidence="high",
        evidence_ref=f"tool:{observation.index}",
        expected_contract=(
            "once agent_directive.control.effects exposes an executable FSM continuation, "
            "the agent follows that official effect instead of probing files or manifests"
        ),
        observed_behavior=(
            f"agent used {observation.tool_name} while an executable FSM effect was already pending"
        ),
        recommended_action="runtime_guardrail",
        promotion_gate="block controlled run promotion and harden hooks/audit before repeating the experiment",
    )


def _is_discovery_tool_while_fsm_effect_pending(observation: ToolCallObservation) -> bool:
    tool_name = observation.tool_name.lower()
    if tool_name in _DISCOVERY_TOOL_NAMES:
        return True
    if tool_name in _SHELL_TOOL_NAMES:
        return bool(_DISCOVERY_SHELL_COMMAND_RE.search(observation.command_text))
    return False


def _is_recoverable_tool_error(observation: ToolCallObservation) -> bool:
    if observation.tool_name not in _SHELL_TOOL_NAMES:
        return False
    if observation.status.lower() not in {"error", "failed"}:
        return False
    command = observation.command_text.lower()
    if any(mutating in command for mutating in (" --apply", " publish-batch", " apply-", " taxonomy-apply")):
        return False
    return True


def _is_agy_materialized_skill_stale_instruction(observation: ToolCallObservation) -> bool:
    return observation.target_is_agy_config_skill and observation.stale_materialized_skill_signal


def _finding(
    *,
    code: str,
    workflow: AuditWorkflow,
    severity: str,
    confidence: str,
    evidence_ref: str,
    expected_contract: str,
    observed_behavior: str,
    recommended_action: str,
    promotion_gate: str,
) -> WorkflowDeviationFinding:
    return WorkflowDeviationFinding(
        code=code,
        workflow=workflow,
        severity=cast(AuditSeverity, severity),
        confidence=cast(AuditConfidence, confidence),
        evidence_ref=evidence_ref,
        expected_contract=expected_contract,
        observed_behavior=observed_behavior,
        recommended_action=cast(RecommendedAction, recommended_action),
        promotion_gate=promotion_gate,
    )


def _result(
    *,
    workflow: AuditWorkflow,
    transcript_present: bool,
    workflow_payload_present: bool,
    final_report_present: bool,
    findings: list[WorkflowDeviationFinding],
    sources: list[AgentTranscriptSource],
) -> WorkflowTranscriptAuditResult:
    summary = _summary(findings)
    status = "clean" if not findings else "blocked" if summary.blocking_candidate_count else "findings"
    return WorkflowTranscriptAuditResult(
        status=cast(AuditStatus, status),
        workflow=workflow,
        transcript_present=transcript_present,
        workflow_payload_present=workflow_payload_present,
        final_report_present=final_report_present,
        blocked_reason=_blocked_reason(findings),
        next_action="" if not findings else _next_action(findings, summary),
        finding_count=len(findings),
        summary=summary,
        findings=findings,
        hardening_recommendations=_recommendations(findings),
        behavior_case_candidates=_behavior_case_candidates(findings),
        sources=sources,
    )


def _blocked_reason(findings: list[WorkflowDeviationFinding]) -> str:
    for finding in findings:
        if finding.severity == "blocking_candidate":
            return finding.code
    return ""


def _next_action(findings: list[WorkflowDeviationFinding], summary: WorkflowTranscriptAuditSummary) -> str:
    for finding in findings:
        if finding.severity == "blocking_candidate":
            return finding.promotion_gate
    return summary.recommended_next_action


def _summary(findings: list[WorkflowDeviationFinding]) -> WorkflowTranscriptAuditSummary:
    blocking = sum(1 for item in findings if item.severity == "blocking_candidate")
    contracts = sum(1 for item in findings if item.severity == "contract_violation")
    warnings = sum(1 for item in findings if item.severity == "warning")
    infos = sum(1 for item in findings if item.severity == "info")
    highest = "blocking_candidate" if blocking else "contract_violation" if contracts else "warning" if warnings else "info"
    next_action = "no transcript audit findings" if not findings else "review workflow transcript audit findings"
    return WorkflowTranscriptAuditSummary(
        finding_count=len(findings),
        blocking_candidate_count=blocking,
        contract_violation_count=contracts,
        warning_count=warnings,
        info_count=infos,
        highest_severity=cast(AuditSeverity, highest),
        recommended_next_action=next_action,
    )


def _recommendations(findings: list[WorkflowDeviationFinding]) -> list[HardeningRecommendation]:
    recommendations: list[HardeningRecommendation] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.code, finding.recommended_action)
        if key in seen:
            continue
        seen.add(key)
        recommendations.append(
            HardeningRecommendation(
                action=finding.recommended_action,
                code=finding.code,
                rationale=finding.observed_behavior,
                target=finding.expected_contract,
            )
        )
    return recommendations


def _behavior_case_candidates(findings: list[WorkflowDeviationFinding]) -> list[RawJsonObject]:
    candidates: list[RawJsonObject] = []
    for finding in findings:
        if finding.severity not in {"contract_violation", "blocking_candidate"}:
            continue
        candidates.append(
            {
                "source_kind": "agent_report",
                "workflow": f"/mednotes:{finding.workflow}" if finding.workflow != "unknown" else "",
                "phase": "post_run_audit",
                "signal": finding.code,
                "severity": "critical" if finding.severity == "blocking_candidate" else "high",
                "redacted_evidence": {
                    "evidence_ref": finding.evidence_ref,
                    "summary": finding.observed_behavior,
                    "expected_contract": finding.expected_contract,
                },
            }
        )
    return candidates
