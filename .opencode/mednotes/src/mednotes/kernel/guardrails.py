from __future__ import annotations

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue

CONTRACT_GAP_MISSING_NEXT_ACTION = "contract_gap.missing_next_action"

BLOCKING_STATUSES_REQUIRING_NEXT_ACTION = {
    "blocked",
    "failed",
    "error",
    "needs_review",
}

NONBLOCKING_TERMINAL_STATUSES = {
    "applied",
    "completed",
    "completed_with_warnings",
    "diagnosis_ready",
    "published",
    "ready",
}


class OperationalErrorContext(ContractModel):
    phase: StrictStr = Field(min_length=1)
    blocked_reason: StrictStr = Field(min_length=1)
    root_cause: StrictStr = Field(min_length=1)
    affected_artifact: StrictStr = Field(min_length=1)
    error_summary: StrictStr = Field(min_length=1)
    suggested_fix: StrictStr = Field(min_length=1)
    next_action: StrictStr = Field(min_length=1)
    retry_scope: StrictStr = Field(min_length=1)
    human_decision_required: StrictBool = False
    affected_items: list[StrictStr] = Field(default_factory=list)
    missing_inputs: list[StrictStr] = Field(default_factory=list)
    max_attempts: int | None = None
    details: JsonValue = None


class WorkflowGuardrailPayloadFields(ContractModel):
    # This model is a typed decision view over larger workflow payloads. It must
    # ignore unrelated keys so feedback, FSM results, and legacy evidence
    # payloads can be inspected without becoming this helper's full schema.
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    phase: StrictStr = ""
    status: StrictStr = ""
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    next_command: StrictStr = ""
    required_inputs: list[StrictStr] = Field(default_factory=list)
    human_decision_required: StrictBool = False
    human_decision_packet: JsonObject | None = None
    human_decision_packets: list[JsonObject] = Field(default_factory=list)
    error_context: JsonObject | None = None
    diagnostic_context: JsonObject = Field(default_factory=dict)
    affected_artifact: StrictStr = ""
    error: StrictStr = ""
    message: StrictStr = ""
    blocked: StrictBool | None = None
    ok: StrictBool | None = None
    parse_error: JsonValue = None
    blocker_count: StrictInt | None = None
    error_count: StrictInt | None = None


def _payload_value(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _optional_string(payload: JsonObject, key: str) -> object:
    value = _payload_value(payload, key)
    if value is None:
        return ""
    return value if isinstance(value, str) else value


def _optional_display_string(payload: JsonObject, key: str) -> str:
    value = _payload_value(payload, key)
    return value if isinstance(value, str) else ""


def _optional_bool(payload: JsonObject, key: str) -> object:
    value = _payload_value(payload, key)
    if value is None:
        return None
    return value if isinstance(value, bool) else value


def _optional_int(payload: JsonObject, key: str) -> object:
    value = _payload_value(payload, key)
    if value is None:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else value


def _string_list(payload: JsonObject, key: str) -> list[str]:
    value = _payload_value(payload, key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _json_object_or_none(payload: JsonObject, key: str) -> JsonObject | None:
    value = _payload_value(payload, key)
    return value if isinstance(value, dict) else None


def _json_object_list(payload: JsonObject, key: str) -> list[JsonObject]:
    value = _payload_value(payload, key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _optional_json_value(payload: JsonObject, key: str) -> JsonValue:
    if key not in payload:
        return None
    return JsonObjectAdapter.validate_python({"value": payload[key]})["value"]


def workflow_guardrail_payload_fields(payload: JsonObject) -> WorkflowGuardrailPayloadFields:
    """Extract only the fields that can drive generic recovery guardrails."""

    raw_fields: JsonObject = {
        "phase": _optional_display_string(payload, "phase"),
        "status": _optional_string(payload, "status"),
        "blocked_reason": _optional_string(payload, "blocked_reason"),
        "next_action": _optional_string(payload, "next_action"),
        "next_command": _optional_string(payload, "next_command"),
        "required_inputs": _string_list(payload, "required_inputs"),
        "human_decision_required": _optional_bool(payload, "human_decision_required") or False,
        "human_decision_packet": _json_object_or_none(payload, "human_decision_packet"),
        "human_decision_packets": _json_object_list(payload, "human_decision_packets"),
        "error_context": _json_object_or_none(payload, "error_context"),
        "diagnostic_context": _json_object_or_none(payload, "diagnostic_context") or {},
        "affected_artifact": _optional_string(payload, "affected_artifact"),
        "error": _optional_string(payload, "error"),
        "message": _optional_string(payload, "message"),
        "blocked": _optional_bool(payload, "blocked"),
        "ok": _optional_bool(payload, "ok"),
        "parse_error": _optional_json_value(payload, "parse_error"),
        "blocker_count": _optional_int(payload, "blocker_count"),
        "error_count": _optional_int(payload, "error_count"),
    }
    return WorkflowGuardrailPayloadFields.model_validate(raw_fields)


def validate_error_context(payload: JsonObject) -> OperationalErrorContext:
    return OperationalErrorContext.model_validate(payload)


def default_contract_next_action(*, workflow: str = "", command: str = "") -> str:
    """Public recovery text for blocked payloads missing an actionable route."""

    route = workflow or command or "workflow oficial"
    return (
        f"Pare antes de mutar. Reexecute a rota oficial de {route} e reporte este "
        "contract_gap ao mantenedor se o payload continuar sem next_action."
    )


def blocked_payload_requires_next_action(payload: JsonObject) -> bool:
    """Return whether an operational payload must expose a recovery route."""

    fields = workflow_guardrail_payload_fields(payload)
    if fields.status in BLOCKING_STATUSES_REQUIRING_NEXT_ACTION or fields.blocked_reason:
        return True
    if fields.status in NONBLOCKING_TERMINAL_STATUSES:
        return False
    if fields.blocked is True or fields.ok is False:
        return True
    if fields.error or fields.parse_error:
        return True
    return (fields.blocker_count or 0) > 0 or (fields.error_count or 0) > 0


def needs_next_action_hardening(payload: JsonObject) -> bool:
    """Return whether a blocked payload needs contract-gap hardening."""

    if not blocked_payload_requires_next_action(payload):
        return False
    fields = workflow_guardrail_payload_fields(payload)
    return not (fields.next_action or fields.next_command).strip()
