from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictStr, ValidationInfo, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue

# Framework schema id. Product-specific registries may export this contract
# under their own target ids, but the kernel must stay domain-neutral.
HUMAN_DECISION_PACKET_SCHEMA = "workflow.human-decision-packet.v1"


class WorkflowDecisionKind(StrEnum):
    AUTO_FIX = "auto_fix"
    AUTO_DEFER = "auto_defer"
    AUTO_PLAN = "auto_plan"
    ASK_HUMAN = "ask_human"
    HARD_BLOCK = "hard_block"
    FAILED = "failed"

    @classmethod
    def coerce(cls, value: str | WorkflowDecisionKind) -> WorkflowDecisionKind:
        try:
            return cls(str(value))
        except ValueError as exc:
            raise WorkflowOutcomeError(f"unknown workflow decision kind: {value}") from exc


class WorkflowAutomationKind(StrEnum):
    AUTO_FIX = "auto_fix"
    AUTO_DEFER = "auto_defer"
    AUTO_PLAN = "auto_plan"

    @classmethod
    def coerce(cls, value: str | WorkflowAutomationKind) -> WorkflowAutomationKind:
        try:
            return cls(str(value))
        except ValueError as exc:
            raise WorkflowOutcomeError(f"unknown automation kind: {value}") from exc


DecisionKind = (
    WorkflowDecisionKind | Literal["auto_fix", "auto_defer", "auto_plan", "ask_human", "hard_block", "failed"]
)
AutomationKind = WorkflowAutomationKind | Literal["auto_fix", "auto_defer", "auto_plan"]
ReceiptStatus = Literal[
    "running",
    "completed",
    "completed_with_warnings",
    "waiting_agent",
    "waiting_external",
    "waiting_human",
    "blocked",
    "failed",
]


class WorkflowOutcomeError(ValueError):
    """Raised when a workflow decision violates the outcome contract."""


EXECUTABLE_DIAGNOSTIC_CONTEXT_KEYS = frozenset(
    {
        "action_directives",
        "agent_directive",
        "continuation_plan",
        "human_decision_packet",
        "human_decision_required",
        "next_action",
        "next_command",
        "pending_effects",
        "required_inputs",
        "resume_action",
        "resume_after_resolution",
        "resume_command",
    }
)


EXECUTABLE_DIAGNOSTIC_CONTEXT_KEY_SUFFIXES = ("_next_action", "_command")
EXECUTABLE_DIAGNOSTIC_CONTEXT_KEY_PREFIXES = ("resume_", "continuation_")


def assert_diagnostic_context_evidence_only(diagnostic_context: object) -> None:
    """Keep diagnostic_context as evidence, never as a second workflow API."""

    offending_path = _executable_diagnostic_path(diagnostic_context, "diagnostic_context")
    if offending_path:
        raise ValueError(f"diagnostic_context must not carry executable routes: {offending_path}")


def diagnostic_context_evidence_only(diagnostic_context: object) -> JsonObject:
    """Return diagnostic evidence with executable route fields recursively removed."""

    cleaned = _remove_executable_diagnostic_routes(diagnostic_context)
    if isinstance(cleaned, dict):
        return JsonObjectAdapter.validate_python(cleaned)
    return {}


def _executable_diagnostic_path(value: object, path: str) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _is_executable_diagnostic_context_key(key_text):
                return child_path
            nested_path = _executable_diagnostic_path(child, child_path)
            if nested_path:
                return nested_path
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested_path = _executable_diagnostic_path(item, f"{path}[{index}]")
            if nested_path:
                return nested_path
    return ""


def _remove_executable_diagnostic_routes(value: object) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, child in value.items():
            key_text = str(key)
            if _is_executable_diagnostic_context_key(key_text):
                continue
            cleaned[key_text] = _remove_executable_diagnostic_routes(child)
        return cleaned
    if isinstance(value, list):
        return [_remove_executable_diagnostic_routes(item) for item in value]
    return value


def _is_executable_diagnostic_context_key(key: str) -> bool:
    """Detect continuation aliases so diagnostics cannot grow a shadow API."""

    normalized = key.strip().casefold()
    return (
        normalized in EXECUTABLE_DIAGNOSTIC_CONTEXT_KEYS
        or "next_action" in normalized
        or any(normalized.endswith(suffix) for suffix in EXECUTABLE_DIAGNOSTIC_CONTEXT_KEY_SUFFIXES)
        or any(normalized.startswith(prefix) for prefix in EXECUTABLE_DIAGNOSTIC_CONTEXT_KEY_PREFIXES)
    )


def _non_empty(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    return cleaned


class DecisionEvidence(ContractModel):
    summary: str
    technical_code: str
    source: str
    affected_items: list[str] = Field(default_factory=list)
    candidates: list[JsonObject] = Field(default_factory=list)
    confidence: str | float = ""
    risk: str = ""

    @field_validator("summary", "technical_code", "source")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty(value, str(info.field_name))

    def as_dict(self) -> JsonObject:
        payload: JsonObject = {
            "summary": self.summary,
            "technical_code": self.technical_code,
            "source": self.source,
        }
        if self.affected_items:
            payload["affected_items"] = list(self.affected_items)
        if self.candidates:
            payload["candidates"] = [dict(item) for item in self.candidates]
        if self.confidence != "":
            payload["confidence"] = self.confidence
        if self.risk:
            payload["risk"] = self.risk
        return _json_object(payload)


class RejectedAutomation(ContractModel):
    kind: WorkflowAutomationKind
    reason_code: str
    reason: str
    safe: bool = False
    evidence_refs: list[str] = Field(default_factory=list)

    def __init__(self, **data: object) -> None:
        if "kind" in data:
            data["kind"] = WorkflowAutomationKind.coerce(str(data["kind"]))
        super().__init__(**data)

    @field_validator("reason_code", "reason")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty(value, str(info.field_name))

    @model_validator(mode="after")
    def _rejection_must_be_unsafe(self) -> RejectedAutomation:
        if self.safe:
            raise ValueError("rejected automation safe must be false")
        return self

    def as_dict(self) -> JsonObject:
        payload: JsonObject = {
            "kind": str(self.kind),
            "safe": False,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }
        if self.evidence_refs:
            payload["evidence_refs"] = list(self.evidence_refs)
        return _json_object(payload)


class HumanDecisionOption(ContractModel):
    id: str
    label: str = ""
    value: str = ""
    description: str = ""
    consequence: str = ""
    safety: str = ""

    @model_validator(mode="after")
    def _requires_closed_label(self) -> HumanDecisionOption:
        object.__setattr__(self, "id", _non_empty(self.id, "id"))
        label = str(self.label or self.value or "").strip()
        if not label:
            raise ValueError("label must be non-empty")
        object.__setattr__(self, "label", label)
        return self


class WorkflowDecisionSummary(ContractModel):
    kind: WorkflowDecisionKind
    phase: str
    reason_code: str
    public_summary: str
    developer_summary: str
    rejected_automations: list[RejectedAutomation] = Field(default_factory=list)
    evidence: list[DecisionEvidence] = Field(default_factory=list)

    def __init__(self, **data: object) -> None:
        if "kind" in data:
            data["kind"] = WorkflowDecisionKind.coerce(str(data["kind"]))
        super().__init__(**data)


class HumanDecisionPacket(ContractModel):
    schema_id: Literal["workflow.human-decision-packet.v1"] = Field(
        default=HUMAN_DECISION_PACKET_SCHEMA,
        alias="schema",
    )
    kind: str
    type: str = ""
    status: str
    phase: str = ""
    blocked_reason: str = ""
    question: str
    why_this_needs_you: str = ""
    recommended_option_id: str
    options: list[HumanDecisionOption]
    context: JsonObject = Field(default_factory=dict)
    evidence_summary: str = ""
    rejected_automations: list[RejectedAutomation]
    decision_summary: WorkflowDecisionSummary
    resume_action: str
    id: str = ""
    target_kind: str = ""
    target_key: str = ""
    path: str = ""
    target: str = ""
    line: int | str | None = None
    public_summary: str = ""
    continue_after_choice: str = ""

    @model_validator(mode="after")
    def _validate_packet_contract(self) -> HumanDecisionPacket:
        object.__setattr__(self, "kind", _non_empty(self.kind or self.type, "kind"))
        if not self.type:
            object.__setattr__(self, "type", self.kind)
        object.__setattr__(self, "status", _non_empty(self.status, "status"))
        object.__setattr__(self, "question", _non_empty(self.question, "question"))
        object.__setattr__(self, "resume_action", _non_empty(self.resume_action, "resume_action"))
        if self.decision_summary.kind != WorkflowDecisionKind.ASK_HUMAN:
            raise ValueError("decision_summary must come from ask_human decision")
        option_ids = {option.id for option in self.options}
        if not option_ids:
            raise ValueError("options must contain at least one item")
        recommended = _non_empty(self.recommended_option_id, "recommended_option_id")
        if recommended not in option_ids:
            raise ValueError("recommended_option_id must match an option id")
        rejected = {item.kind for item in self.rejected_automations}
        missing = [kind for kind in WorkflowAutomationKind if kind not in rejected]
        if missing:
            raise ValueError("missing rejected automation evidence for: " + ", ".join(str(kind) for kind in missing))
        return self


class WorkflowDecision(ContractModel):
    kind: WorkflowDecisionKind
    phase: str
    reason_code: str
    public_summary: str
    developer_summary: str
    evidence: list[DecisionEvidence]
    next_action: str
    rejected_automations: list[RejectedAutomation] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    resume_action: str = ""
    mutates: bool = False
    artifacts: list[dict[str, str]] = Field(default_factory=list)
    recommended_option_id: str = ""
    options: list[HumanDecisionOption] = Field(default_factory=list)
    human_decision_kind: str = ""

    def __init__(self, **data: object) -> None:
        if "kind" in data:
            data["kind"] = WorkflowDecisionKind.coerce(str(data["kind"]))
        kind = data.get("kind")
        options = data.get("options")
        if kind == WorkflowDecisionKind.ASK_HUMAN and options is not None:
            _closed_option_ids(options, error_prefix="ask_human closed options")
        super().__init__(**data)
        self._validate_decision_contract()

    @classmethod
    def model_validate(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "forbid", "ignore"] | None = None,
        from_attributes: bool | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        decision = super().model_validate(
            obj,
            strict=strict,
            extra=extra,
            from_attributes=from_attributes,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        decision._validate_decision_contract()
        return decision

    @field_validator("phase", "reason_code", "public_summary", "developer_summary")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty(value, str(info.field_name))

    @field_validator("evidence")
    @classmethod
    def _requires_evidence(cls, value: list[DecisionEvidence]) -> list[DecisionEvidence]:
        if not value:
            raise ValueError("evidence must contain at least one item")
        return value

    def _validate_decision_contract(self) -> None:
        if self.kind == WorkflowDecisionKind.ASK_HUMAN:
            rejected = {item.kind for item in self.rejected_automations}
            missing = [kind for kind in WorkflowAutomationKind if kind not in rejected]
            if missing:
                raise WorkflowOutcomeError(
                    "ask_human requires rejected automation evidence for: " + ", ".join(str(kind) for kind in missing)
                )
            option_ids = {option.id for option in self.options}
            if not option_ids:
                raise WorkflowOutcomeError("ask_human closed options requires options")
            if not self.recommended_option_id:
                raise WorkflowOutcomeError("ask_human requires recommended_option_id")
            if self.recommended_option_id not in option_ids:
                raise WorkflowOutcomeError("recommended_option_id must match an option id")
        if (
            self.kind
            in {
                WorkflowDecisionKind.HARD_BLOCK,
                WorkflowDecisionKind.FAILED,
                WorkflowDecisionKind.ASK_HUMAN,
            }
            and not self.next_action
        ):
            raise WorkflowOutcomeError(f"{self.kind} requires next_action")

    def decision_summary(self) -> JsonObject:
        return _json_object(
            {
                "kind": str(self.kind),
                "phase": self.phase,
                "reason_code": self.reason_code,
                "public_summary": self.public_summary,
                "developer_summary": self.developer_summary,
                "rejected_automations": [item.as_dict() for item in self.rejected_automations],
                "evidence": [item.as_dict() for item in self.evidence],
            }
        )

    def to_human_decision_packet(self) -> JsonObject:
        if self.kind != WorkflowDecisionKind.ASK_HUMAN:
            raise WorkflowOutcomeError("only ask_human decisions produce human_decision_packet")
        packet_kind = self.human_decision_kind or self.reason_code
        return HumanDecisionPacket(
            schema=HUMAN_DECISION_PACKET_SCHEMA,
            kind=packet_kind,
            type=packet_kind,
            status="pending",
            phase=self.phase,
            blocked_reason=self.reason_code,
            question=self.public_summary,
            why_this_needs_you=self.developer_summary,
            recommended_option_id=self.recommended_option_id,
            options=self.options,
            context={"evidence": [item.as_dict() for item in self.evidence]},
            evidence_summary=self.public_summary,
            rejected_automations=self.rejected_automations,
            decision_summary=self.decision_summary(),
            resume_action=self.resume_action or self.next_action,
        ).to_payload()


class WorkflowPhaseOutcome(ContractModel):
    phase: StrictStr
    decision_summary: JsonObject
    human_decision_packet: HumanDecisionPacket | None = None


class WorkflowPhaseReceipt(ContractModel):
    receipt_path: StrictStr
    status: StrictStr = ""


class WorkflowArtifact(ContractModel):
    kind: StrictStr
    path: StrictStr


class WorkflowRollback(ContractModel):
    strategy: StrictStr
    value: StrictStr


class VersionControlSafety(ContractModel):
    no_resource_mutation: bool = Field(strict=True)
    rollback_declared: bool = Field(strict=True)
    resource_guard_active: bool = Field(default=False, strict=True)
    run_start_seen: bool = Field(default=False, strict=True)
    run_finish_seen: bool = Field(default=False, strict=True)
    restore_point_before: bool | str = False
    restore_point_after: bool | str = False
    sync_status: str = ""
    backup_online: str = ""
    direct_mutation_forbidden: bool = Field(default=True, strict=True)
    mutation_without_guard: bool = Field(default=False, strict=True)
    changed_file_count: int = Field(default=0, ge=0, strict=True)
    agent_instruction: str = ""


from mednotes.kernel.progress import (  # noqa: E402
    WorkflowProgressState,
    WorkflowProgressStatus,
    WorkflowProgressViewModel,
    build_progress_view_model,
)
from mednotes.kernel.state_machine import WorkflowStateMachineSnapshot  # noqa: E402


class WorkflowReceiptPayload(ContractModel):
    schema_id: str = Field(alias="schema")
    workflow: str
    run_id: str
    status: ReceiptStatus
    mutated: bool
    next_action: str = ""
    human_decision_required: bool = False
    human_decision_packet: HumanDecisionPacket | None = None
    phase_outcomes: list[WorkflowPhaseOutcome] = Field(default_factory=list)
    phase_receipts: dict[str, WorkflowPhaseReceipt] = Field(default_factory=dict)
    artifacts: list[WorkflowArtifact] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    rollback: WorkflowRollback | None = None
    version_control_safety: VersionControlSafety
    progress_state: WorkflowProgressState | None = None
    progress_view_model: WorkflowProgressViewModel | None = None
    state_machine_snapshot: WorkflowStateMachineSnapshot | None = None

    @model_validator(mode="after")
    def _validate_receipt_contract(self) -> WorkflowReceiptPayload:
        needs_next_action = {
            "blocked",
            "failed",
            "completed_with_warnings",
            "waiting_agent",
            "waiting_external",
            "waiting_human",
        }
        if self.status in needs_next_action and not self.next_action.strip():
            raise ValueError(f"{self.status} receipt requires next_action")
        if self.status == "waiting_human":
            object.__setattr__(self, "human_decision_required", True)
        if self.human_decision_required and self.human_decision_packet is None:
            raise ValueError("human_decision_required receipt requires human_decision_packet")
        if self.human_decision_packet is not None:
            _validate_ask_human_packet(self.human_decision_packet)
        if self.status == "waiting_external":
            self._validate_waiting_external_progress()
        self._validate_progress_ownership()
        return self

    def _validate_waiting_external_progress(self) -> None:
        if self.progress_state is None:
            raise ValueError("waiting_external receipt requires progress_state")
        if self.progress_state.status != WorkflowProgressStatus.WAITING_EXTERNAL:
            raise ValueError("waiting_external receipt requires waiting_external progress_state")
        if not self.progress_state.resume_action.strip():
            raise ValueError("waiting_external receipt requires progress_state.resume_action")
        if self.progress_state.can_continue_now:
            raise ValueError("waiting_external receipt requires can_continue_now=false")

    def _validate_progress_ownership(self) -> None:
        for field_name in ("progress_state", "progress_view_model", "state_machine_snapshot"):
            embedded = getattr(self, field_name)
            if embedded is None:
                continue
            _require_same_receipt_identity(
                field_name=field_name,
                receipt_workflow=self.workflow,
                receipt_run_id=self.run_id,
                embedded_workflow=embedded.workflow,
                embedded_run_id=embedded.run_id,
            )
        if self.progress_state is not None and self.progress_view_model is not None:
            _require_progress_view_model_matches_state(self.progress_view_model, self.progress_state)
        if self.progress_state is not None and self.state_machine_snapshot is not None:
            if self.state_machine_snapshot.current_state != self.progress_state.state:
                raise ValueError("state_machine_snapshot current_state must match progress_state state")


def _require_same_receipt_identity(
    *,
    field_name: str,
    receipt_workflow: str,
    receipt_run_id: str,
    embedded_workflow: str,
    embedded_run_id: str,
) -> None:
    if embedded_workflow != receipt_workflow:
        raise ValueError(f"{field_name}.workflow must match receipt workflow")
    if embedded_run_id != receipt_run_id:
        raise ValueError(f"{field_name}.run_id must match receipt run_id")


def _require_progress_view_model_matches_state(
    view_model: WorkflowProgressViewModel,
    state: WorkflowProgressState,
) -> None:
    expected_payload = build_progress_view_model(state).to_payload()
    actual_payload = view_model.to_payload()
    for field_name, expected_value in expected_payload.items():
        if actual_payload.get(field_name) != expected_value:
            raise ValueError(f"progress_view_model.{field_name} must match canonical progress_state projection")


def attach_human_decision_packet(
    payload: JsonObject,
    *,
    packet: HumanDecisionPacket | JsonObject,
) -> JsonObject:
    """Attach an ask_human packet only when it carries the decision contract."""
    typed = _validate_ask_human_packet(packet)
    packet_payload = typed.to_payload()
    enriched: JsonObject = dict(payload)
    enriched["human_decision_required"] = True
    enriched["human_decision_packet"] = packet_payload
    enriched["human_decision_packets"] = [packet_payload]
    enriched["decision_summary"] = typed.decision_summary.to_payload()
    return _json_object(enriched)


def _validate_ask_human_packet(packet: HumanDecisionPacket | JsonObject) -> HumanDecisionPacket:
    if isinstance(packet, HumanDecisionPacket):
        return packet
    try:
        return HumanDecisionPacket.model_validate(packet)
    except PydanticValidationError as exc:
        raise WorkflowOutcomeError(f"human_decision_packet invalid: {exc}") from exc


def _closed_option_ids(options: object, *, error_prefix: str) -> set[str]:
    if not isinstance(options, list) or not options:
        raise WorkflowOutcomeError(f"{error_prefix} requires options")
    option_ids: set[str] = set()
    for index, option in enumerate(options):
        if isinstance(option, HumanDecisionOption):
            option_ids.add(option.id)
            continue
        if not isinstance(option, dict):
            raise WorkflowOutcomeError(f"{error_prefix} options[{index}] must be an object")
        try:
            option_ids.add(HumanDecisionOption.model_validate(option).id)
        except PydanticValidationError as exc:
            raise WorkflowOutcomeError(f"{error_prefix} options[{index}] invalid: {exc}") from exc
    return option_ids


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source.get(key, default)
