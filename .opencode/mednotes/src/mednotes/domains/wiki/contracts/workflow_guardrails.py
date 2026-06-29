"""Shared operational guardrails for deterministic Wiki workflows."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, StrictBool, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import ValidationError
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.guardrails import (
    BLOCKING_STATUSES_REQUIRING_NEXT_ACTION,
    CONTRACT_GAP_MISSING_NEXT_ACTION,
    OperationalErrorContext,
    WorkflowGuardrailPayloadFields,
    blocked_payload_requires_next_action,
    default_contract_next_action,
    workflow_guardrail_payload_fields,
)
from mednotes.kernel.workflow import HUMAN_DECISION_PACKET_SCHEMA

SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON = "subagent_output_contract.invalid"
CONTRACT_GAP_MISSING_ERROR_CONTEXT = "contract_gap.missing_error_context"
PROCESS_CHATS_REQUIRED_INPUTS = ["raw_file", "note_plan", "coverage_path"]
PUBLISH_REQUIRED_INPUTS = ["manifest", "coverage_path", "dry_run_receipt"]
LINK_REQUIRED_INPUTS = ["wiki_dir", "vocabulary_db_path"]
FIX_WIKI_REQUIRED_INPUTS = ["wiki_dir", "vocabulary_db_path"]
STYLE_REWRITE_REQUIRED_INPUTS = ["target", "content"]
NOTE_MERGE_REQUIRED_INPUTS = ["plan", "content"]
BLOCKING_STATUSES = BLOCKING_STATUSES_REQUIRING_NEXT_ACTION


class _SubagentOutputContractFields(ContractModel):
    schema_id: StrictStr | None = Field(default=None, alias="schema")
    workflow: StrictStr | None = None
    phase: StrictStr | None = None
    source_workflow: StrictStr | None = None
    agent: StrictStr | None = None
    status: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    human_decision_required: StrictBool = False
    error_context: JsonObject | None = None


def annotate_payload(
    payload: JsonObject,
    *,
    phase: str,
    status: str,
    blocked_reason: str | None = None,
    next_action: str | None = None,
    required_inputs: list[str] | None = None,
    human_decision_required: bool = False,
) -> JsonObject:
    """Attach stable operational fields expected by workflows/tests/agents."""
    annotated = dict(JsonObjectAdapter.validate_python(payload))
    boundary_fields = JsonObjectAdapter.validate_python(
        {
            "phase": phase,
            "status": status,
            "blocked_reason": blocked_reason or "",
            "next_action": next_action or "",
            "required_inputs": list(required_inputs or []),
            "human_decision_required": bool(human_decision_required),
        }
    )
    annotated.update(boundary_fields)
    return _ensure_workflow_decision_boundary(annotated)


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value)


def _object_field(value: object, key: str) -> object:
    """Read optional external fields without letting loose dict access drive flow."""

    if not isinstance(value, dict) or key not in value:
        return None
    return value[key]


def _json_field(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return value if isinstance(value, str) else ""


def _json_object_field(payload: JsonObject, key: str) -> JsonObject | None:
    value = _json_field(payload, key)
    return value if isinstance(value, dict) else None


def _json_stringified_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return str(value) if value else ""


def _guardrail_fields(payload: JsonObject) -> WorkflowGuardrailPayloadFields:
    return workflow_guardrail_payload_fields(payload)


def _strict_guardrail_fields(payload: JsonObject) -> WorkflowGuardrailPayloadFields:
    raw_fields: JsonObject = {}
    for key in (
        "phase",
        "status",
        "blocked_reason",
        "next_action",
        "next_command",
        "required_inputs",
        "human_decision_required",
        "human_decision_packet",
        "human_decision_packets",
        "error_context",
        "diagnostic_context",
        "affected_artifact",
        "error",
        "message",
    ):
        if key in payload:
            raw_fields[key] = payload[key]
    return WorkflowGuardrailPayloadFields.model_validate(raw_fields)


def _subagent_contract_fields(payload: JsonObject) -> tuple[_SubagentOutputContractFields, list[JsonObject]]:
    raw_fields: JsonObject = {}
    for key in (
        "schema",
        "workflow",
        "phase",
        "source_workflow",
        "agent",
        "status",
        "blocked_reason",
        "next_action",
        "error_context",
    ):
        if key in payload:
            raw_fields[key] = payload[key]
    try:
        return _SubagentOutputContractFields.model_validate(raw_fields), []
    except PydanticValidationError as exc:
        errors: list[JsonObject] = []
        for issue in exc.errors():
            loc = _object_field(issue, "loc")
            field = "$"
            if isinstance(loc, (list, tuple)) and loc:
                field = str(loc[0])
            elif isinstance(loc, str) and loc:
                field = loc
            message = _object_field(issue, "msg")
            issue_type = _object_field(issue, "type")
            errors.append(
                {
                    "code": f"{field}_invalid_type",
                    "field": field,
                    "expected": "valid typed contract field",
                    "actual": str(message or issue_type or "invalid"),
                }
            )
        return _SubagentOutputContractFields(), errors


def _diagnostic_context(fields: WorkflowGuardrailPayloadFields) -> JsonObject:
    return dict(fields.diagnostic_context)


def _is_blocked_payload(payload: JsonObject) -> bool:
    return blocked_payload_requires_next_action(payload)


def _default_contract_next_action(*, workflow: str, command: str) -> str:
    command_route = f"wiki-cli:{command}" if command and not workflow else command
    return default_contract_next_action(workflow=workflow, command=command_route)


def _invalid_contract_gap_payload(
    *,
    payload: JsonObject,
    phase: str,
    root_cause: str,
    next_action: str,
) -> JsonObject:
    hardened = dict(_json_object(payload))
    fields = _guardrail_fields(hardened)
    hardened.update(
        {
            "status": "blocked",
            "blocked_reason": root_cause,
            "next_action": next_action,
            "human_decision_required": False,
            "required_inputs": list(fields.required_inputs),
            "error_context": error_context(
                phase=phase,
                blocked_reason=root_cause,
                root_cause=root_cause,
                affected_artifact=fields.affected_artifact or phase,
                error_summary=root_cause,
                suggested_fix=next_action,
                next_action=next_action,
                retry_scope="restore_official_workflow_route",
            ),
        }
    )
    diagnostic = _diagnostic_context(fields)
    diagnostic["root_cause_code"] = root_cause
    if root_cause == "workflow.invalid_human_decision_packet":
        diagnostic["decision_boundary_error"] = "WorkflowOutcomeError"
    hardened["diagnostic_context"] = diagnostic
    return hardened


def _is_legacy_schema_human_packet(packet: JsonObject) -> bool:
    return _json_str_field(packet, "schema") == HUMAN_DECISION_PACKET_SCHEMA and "decision_summary" not in packet


def harden_operational_payload(
    payload: JsonObject,
    *,
    workflow: str = "",
    command: str = "",
    require_error_context: bool = False,
) -> JsonObject:
    """Fail closed on blocked payloads that lack an actionable recovery contract."""
    hardened = dict(_json_object(payload))
    fields = _strict_guardrail_fields(hardened)
    if not _is_blocked_payload(hardened):
        return hardened

    phase = fields.phase or command or "unknown"
    original_blocked_reason = fields.blocked_reason
    current_status = fields.status
    current_blocked_reason = fields.blocked_reason
    current_next_action = fields.next_action
    current_required_inputs = list(fields.required_inputs)
    current_human_decision_required = fields.human_decision_required
    current_error_context = fields.error_context

    if fields.human_decision_required:
        from mednotes.domains.wiki.contracts.workflow_outcomes import WorkflowOutcomeError, attach_human_decision_packet

        candidate_packets: list[JsonObject] = []
        packet = fields.human_decision_packet
        if packet is not None:
            candidate_packets.append(packet)
        candidate_packets.extend(fields.human_decision_packets)
        packet_valid = False
        for candidate in candidate_packets:
            try:
                attach_human_decision_packet(dict(hardened), packet=candidate)
            except WorkflowOutcomeError:
                continue
            packet_valid = True
            break
        if not candidate_packets or not packet_valid or any(_is_legacy_schema_human_packet(item) for item in candidate_packets):
            return _invalid_contract_gap_payload(
                payload=hardened,
                phase=phase,
                root_cause="workflow.invalid_human_decision_packet",
                next_action=(
                    "Gerar human_decision_packet pela API WorkflowDecision(kind='ask_human') "
                    "com evidencias e automacoes rejeitadas."
                ),
            )

    if require_error_context and current_error_context is not None:
        from mednotes.kernel.guardrails import validate_error_context

        normalized_error_context = dict(current_error_context)
        normalized_error_context.setdefault("phase", phase)
        hardened["error_context"] = normalized_error_context
        current_error_context = normalized_error_context
        try:
            validate_error_context(normalized_error_context)
        except PydanticValidationError:
            return _invalid_contract_gap_payload(
                payload=hardened,
                phase=phase,
                root_cause="contract_gap.invalid_error_context",
                next_action=(
                    "Emitir error_context completo com root_cause, affected_artifact, error_summary, "
                    "suggested_fix, next_action e retry_scope."
                ),
            )

    missing_fields: list[str] = []
    if not current_next_action.strip():
        missing_fields.append("next_action")
        current_blocked_reason = CONTRACT_GAP_MISSING_NEXT_ACTION
        current_next_action = _default_contract_next_action(workflow=workflow, command=command)
        current_status = "blocked"
        hardened.update(
            {
                "blocked_reason": current_blocked_reason,
                "next_action": current_next_action,
                "status": current_status,
            }
        )
    elif not current_status:
        current_status = "blocked"
        hardened.update({"status": current_status})

    raw_required_inputs = _json_field(hardened, "required_inputs")
    if "required_inputs" not in hardened or not isinstance(raw_required_inputs, list):
        if missing_fields or require_error_context:
            current_required_inputs = []
            hardened["required_inputs"] = []
    if "human_decision_required" not in hardened:
        if missing_fields or require_error_context:
            current_human_decision_required = False
            hardened["human_decision_required"] = False

    diagnostic = _diagnostic_context(fields)
    if missing_fields:
        diagnostic["root_cause_code"] = current_blocked_reason
        diagnostic["contract_gap"] = {
            "missing_fields": missing_fields,
            "original_blocked_reason": original_blocked_reason,
            "workflow": workflow,
            "command": command,
        }

    needs_error_context = require_error_context or bool(missing_fields)
    if needs_error_context and current_error_context is None:
        root_cause = current_blocked_reason or CONTRACT_GAP_MISSING_ERROR_CONTEXT
        if "error_context" not in missing_fields and require_error_context:
            missing_fields.append("error_context")
        missing_inputs = _missing_inputs_for_synthesized_error_context(
            required_inputs=current_required_inputs,
            missing_fields=missing_fields,
        )
        current_error_context = error_context(
            phase=phase,
            blocked_reason=root_cause,
            root_cause=root_cause,
            affected_artifact=fields.affected_artifact or phase,
            error_summary=fields.error or fields.message or root_cause,
            suggested_fix=current_next_action or _default_contract_next_action(workflow=workflow, command=command),
            next_action=current_next_action or _default_contract_next_action(workflow=workflow, command=command),
            retry_scope="restore_official_workflow_route",
            missing_inputs=missing_inputs,
            human_decision_required=current_human_decision_required,
        )
        hardened["error_context"] = current_error_context
    if current_error_context is not None:
        diagnostic["error_context"] = current_error_context
        diagnostic.setdefault("root_cause_code", current_blocked_reason)
    if diagnostic:
        hardened["diagnostic_context"] = diagnostic
    return _ensure_workflow_decision_boundary(hardened)


def _missing_inputs_for_synthesized_error_context(
    *,
    required_inputs: object,
    missing_fields: list[str],
) -> list[str]:
    """Prefer workflow inputs over the diagnostic field when synthesizing recovery context."""

    if missing_fields == ["error_context"] and isinstance(required_inputs, list):
        return [str(item) for item in required_inputs]
    return [str(item) for item in missing_fields]


def _ensure_workflow_decision_boundary(payload: JsonObject) -> JsonObject:
    operational_payload = _json_object(payload)
    fields = _strict_guardrail_fields(operational_payload)
    if fields.status not in {"blocked", "failed"} and not fields.blocked_reason:
        return operational_payload
    if _json_object_field(operational_payload, "decision_summary") is not None:
        return operational_payload
    try:
        packet = fields.human_decision_packet
        if packet is not None and _json_object_field(packet, "decision_summary") is not None:
            from mednotes.domains.wiki.contracts.workflow_outcomes import attach_human_decision_packet

            return attach_human_decision_packet(dict(operational_payload), packet=packet)
        for item in fields.human_decision_packets:
            if _json_object_field(item, "decision_summary") is not None:
                from mednotes.domains.wiki.contracts.workflow_outcomes import attach_human_decision_packet

                return attach_human_decision_packet(dict(operational_payload), packet=item)
    except Exception as exc:
        hardened = dict(operational_payload)
        diagnostic = _diagnostic_context(fields)
        diagnostic["decision_boundary_error"] = exc.__class__.__name__
        diagnostic.setdefault("root_cause_code", fields.blocked_reason or "workflow_decision_boundary_failed")
        hardened["diagnostic_context"] = diagnostic
        return hardened
    return operational_payload


def _generalist_signal_path(value: JsonValue, *, path: str = "$") -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}"
            if str(key).lower() == "used_generalist" and item is True:
                return key_path
            found = _generalist_signal_path(item, path=key_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _generalist_signal_path(item, path=f"{path}.{index}")
            if found:
                return found
    elif isinstance(value, str) and "generalist" in value.casefold():
        return path
    return ""


def subagent_output_contract_errors(
    payload: JsonObject,
    *,
    expected_schema: str,
    expected_workflow: str,
    expected_phase: str,
    allowed_agents: set[str] | list[str] | tuple[str, ...],
    source_workflow: str,
) -> list[JsonObject]:
    """Return structural issues that make a subagent output unsafe to apply."""
    allowed = {str(agent) for agent in allowed_agents}
    fields, field_errors = _subagent_contract_fields(payload)
    checks = [
        ("schema", expected_schema),
        ("workflow", expected_workflow),
        ("phase", expected_phase),
        ("source_workflow", source_workflow),
    ]
    errors: list[JsonObject] = list(field_errors)
    actual_values = {
        "schema": fields.schema_id,
        "workflow": fields.workflow,
        "phase": fields.phase,
        "source_workflow": fields.source_workflow,
    }
    for field, expected in checks:
        if any(_json_str_field(error, "field") == field for error in field_errors):
            continue
        actual = actual_values[field] or ""
        if actual != expected:
            errors.append(
                {
                    "code": f"{field}_mismatch" if actual else f"{field}_missing",
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                }
            )
    agent = fields.agent or ""
    if agent not in allowed:
        if not any(_json_str_field(error, "field") == "agent" for error in field_errors):
            errors.append(
                {
                    "code": "agent_not_allowed" if agent else "agent_missing",
                    "field": "agent",
                    "expected": ",".join(sorted(allowed)),
                    "actual": agent,
                }
            )
    generalist_path = _generalist_signal_path(payload)
    if generalist_path:
        errors.append(
            {
                "code": "generalist_forbidden",
                "field": generalist_path,
                "expected": "no generalist signal",
                "actual": "generalist",
            }
        )
    if _is_blocked_payload(payload) and fields.error_context is None:
        errors.append(
            {
                "code": "error_context_missing",
                "field": "error_context",
                "expected": "object",
                "actual": "missing",
            }
        )
    return errors


def require_subagent_output_contract(
    payload: JsonObject,
    *,
    expected_schema: str,
    expected_workflow: str,
    expected_phase: str,
    allowed_agents: set[str] | list[str] | tuple[str, ...],
    source_workflow: str,
) -> None:
    errors = subagent_output_contract_errors(
        payload,
        expected_schema=expected_schema,
        expected_workflow=expected_workflow,
        expected_phase=expected_phase,
        allowed_agents=allowed_agents,
        source_workflow=source_workflow,
    )
    if errors:
        summary = "; ".join(f"{error['code']}({error['field']})" for error in errors)
        raise ValidationError(f"{SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON}: {summary}")


def error_context(
    *,
    phase: str,
    blocked_reason: str,
    root_cause: str,
    affected_artifact: str,
    error_summary: str,
    suggested_fix: str,
    next_action: str,
    retry_scope: str,
    affected_items: list[str] | None = None,
    missing_inputs: list[str] | None = None,
    max_attempts: int | None = None,
    human_decision_required: bool = False,
) -> JsonObject:
    """Build the minimal retry context agents/subagents need to fix safely."""
    return OperationalErrorContext(
        phase=phase,
        blocked_reason=blocked_reason,
        root_cause=root_cause,
        affected_artifact=affected_artifact,
        error_summary=error_summary,
        suggested_fix=suggested_fix,
        next_action=next_action,
        retry_scope=retry_scope,
        human_decision_required=human_decision_required,
        affected_items=list(affected_items or []),
        missing_inputs=list(missing_inputs or []),
        max_attempts=max_attempts,
    ).to_payload()


def human_decision_packet(
    *,
    kind: str,
    question: str,
    options: list[JsonObject | str],
    resume_action: str,
    phase: str,
    blocked_reason: str = "human_decision_required",
    target_kind: str = "",
    target_key: str = "",
    context: JsonObject | None = None,
) -> JsonObject:
    """Build a closed, resumable decision packet for agents and telemetry."""
    clean_kind = normalize_key(kind).replace(" ", "_") or "manual_review"
    clean_options: list[JsonObject] = []
    for index, option in enumerate(options, start=1):
        if isinstance(option, dict):
            label = (
                _json_stringified_field(option, "label")
                or _json_stringified_field(option, "value")
                or _json_stringified_field(option, "id")
                or f"Opção {index}"
            )
            option_id = _json_stringified_field(option, "id") or normalize_key(label).replace(" ", "_") or f"option_{index}"
            clean: JsonObject = {"id": option_id, "label": label}
            for key in ("description", "consequence", "value", "resume_action"):
                value = _json_field(option, key)
                if value:
                    clean[key] = str(value)
        else:
            label = str(option)
            clean = {
                "id": normalize_key(label).replace(" ", "_") or f"option_{index}",
                "label": label,
                "value": label,
            }
        clean_options.append(clean)
    packet: JsonObject = {
        "schema": HUMAN_DECISION_PACKET_SCHEMA,
        "kind": clean_kind,
        "type": clean_kind,
        "status": "pending",
        "phase": phase,
        "blocked_reason": blocked_reason,
        "question": question,
        "options": clean_options,
        "resume_action": resume_action,
    }
    if target_kind:
        packet["target_kind"] = target_kind
    if target_key:
        packet["target_key"] = target_key
    if context:
        packet["context"] = context
    return packet


def note_target_index(wiki_dir: Path, *, as_relative: bool = False) -> dict[str, list[Path | str]]:
    """Index existing note stems by Obsidian-style normalized target key."""
    targets: dict[str, list[Path | str]] = {}
    if not wiki_dir.exists():
        return targets
    for path in iter_notes(wiki_dir):
        display: Path | str
        if as_relative:
            try:
                display = path.relative_to(wiki_dir).as_posix()
            except ValueError:
                display = str(path)
        else:
            display = path
        targets.setdefault(normalize_key(path.stem), []).append(display)
    return targets


def plan_status(*, item_count: int, blocked_item_count: int) -> tuple[str, str, bool]:
    """Return status, next action and whether a human decision is needed."""
    if item_count == 0 and blocked_item_count:
        return (
            "blocked",
            "Revisar os blocked_items, corrigir as precondições e planejar novamente.",
            True,
        )
    if item_count and blocked_item_count:
        return (
            "ready_with_blockers",
            "Executar apenas os work_items liberados e tratar os blocked_items antes do próximo lote.",
            True,
        )
    if item_count:
        return ("ready", "Executar somente os work_items deste plano e consolidar serialmente depois.", False)
    return ("completed", "Nenhum item pendente para esta fase.", False)
