from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.progress import WorkflowProgressStatus, WorkflowProgressViewModel
from mednotes.kernel.state_machine import WorkflowStateMachineSnapshot

AgentDirectiveStatus = Literal[
    "running",
    "waiting_agent",
    "waiting_external",
    "waiting_human",
    "blocked",
    "failed",
    "completed",
    "completed_with_warnings",
]

AGENT_DIRECTIVE_SCHEMA = "agent-directive.v1"


class AgentCapabilities(ContractModel):
    continue_: bool = Field(False, alias="continue")
    final_report: bool = False


class AgentLimits(ContractModel):
    raw_content: bool = False
    absolute_paths: bool = False
    ad_hoc_scripts: bool = False


class AgentEffect(ContractModel):
    """Redacted executable effect projection for hooks and agent automation.

    The directive is the public FSM -> agent contract. It exposes enough typed
    effect identity to continue safely, without making hooks reconstruct work
    from diagnostic evidence or opaque adapter payload fields.
    """

    effect_id: str = ""
    kind: WorkflowEffectKind
    target: str = ""
    origin_state: str = ""
    resume_action: str = ""
    mutates_resources: bool = False
    no_resource_mutation: bool = False
    rollback_declared: bool = False
    requires_receipt: bool = True
    requires_attestation: bool = False
    model_policy: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    payload_schema: str = ""
    payload: JsonObject = Field(default_factory=dict)


class AgentReportDirective(ContractModel):
    requires: list[str] = Field(default_factory=list)


class AgentDirectiveControl(ContractModel):
    status: AgentDirectiveStatus
    state: str = Field(min_length=1, pattern=r"\S")
    reason: str = ""
    phase: str = ""
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    effects: list[AgentEffect] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    resume: str = ""
    report: AgentReportDirective = Field(default_factory=AgentReportDirective)
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @field_validator("state")
    @classmethod
    def _state_must_be_identity_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @field_validator("blockers")
    @classmethod
    def _blockers_must_be_text(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @model_validator(mode="after")
    def _status_shape(self) -> AgentDirectiveControl:
        if self.status == "waiting_agent":
            if not self.capabilities.continue_:
                raise ValueError("waiting_agent requires control.capabilities.continue=true")
            if self.capabilities.final_report:
                raise ValueError("waiting_agent requires control.capabilities.final_report=false")
            if not self.effects:
                raise ValueError("waiting_agent requires agent_directive.control.effects")
        if self.status in {"completed", "completed_with_warnings"}:
            if not self.capabilities.final_report:
                raise ValueError("completed directive requires control.capabilities.final_report=true")
        if self.status in {"waiting_human", "waiting_external", "blocked", "failed"}:
            if not self.blockers and not self.resume.strip():
                raise ValueError(f"{self.status} directive requires blockers or resume")
        return self


class AgentDirective(ContractModel):
    # The kernel is framework-only, so its default schema is neutral. Product
    # workflows must pass their public schema explicitly at the projection edge.
    schema_: str = Field(AGENT_DIRECTIVE_SCHEMA, alias="schema")
    workflow: str = Field(min_length=1, pattern=r"\S")
    run_id: str = Field(min_length=1, pattern=r"\S")
    control: AgentDirectiveControl
    summary: str = ""
    instructions: list[str] = Field(default_factory=list)

    @field_validator("workflow", "run_id")
    @classmethod
    def _identity_fields_must_be_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @field_validator("instructions")
    @classmethod
    def _instructions_are_plain_text(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for line in value:
            text = line.strip()
            if not text:
                continue
            if text.casefold().startswith("agent_instruction:"):
                raise ValueError("AgentDirective instructions must not include agent_instruction prefix")
            cleaned.append(text)
        return cleaned


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = payload[key] if key in payload else ""
    return value if isinstance(value, str) else ""


def assert_agent_directive_matches_progress(
    directive: AgentDirective,
    *,
    workflow: str,
    run_id: str,
    progress_view_model: WorkflowProgressViewModel,
    snapshot: WorkflowStateMachineSnapshot,
    allowed_effect_kinds: set[WorkflowEffectKind],
    label: str,
) -> None:
    """Assert the agent-facing route is a projection of the current FSM state.

    The directive is executable by hooks/subagents, so it cannot be validated as
    a standalone object. It must agree with the current StateChart leaf, progress
    model and effect policy; otherwise it becomes a parallel state channel.
    """

    if directive.workflow != workflow:
        raise ValueError(f"{label} agent_directive workflow must match workflow")
    if directive.run_id != run_id or directive.run_id != snapshot.run_id or directive.run_id != progress_view_model.run_id:
        raise ValueError(f"{label} agent_directive run_id must match progress and snapshot")
    if progress_view_model.workflow != workflow or snapshot.workflow != workflow:
        raise ValueError(f"{label} progress and snapshot workflow must match workflow")
    if directive.control.status != progress_view_model.status.value:
        raise ValueError(f"{label} agent_directive status must match progress view status")
    if progress_view_model.status.value != snapshot.current_category.value:
        raise ValueError(f"{label} progress status must match state_machine_snapshot category")
    if directive.control.state != snapshot.current_state:
        raise ValueError(f"{label} agent_directive state must match current StateChart state")
    if progress_view_model.state != snapshot.current_state:
        raise ValueError(f"{label} progress state must match current StateChart state")
    if directive.control.capabilities.continue_ != progress_view_model.can_continue_now:
        raise ValueError(f"{label} agent_directive continue capability must match progress view")
    final_report = progress_view_model.status in {
        WorkflowProgressStatus.COMPLETED,
        WorkflowProgressStatus.COMPLETED_WITH_WARNINGS,
    }
    if directive.control.capabilities.final_report != final_report:
        raise ValueError(f"{label} agent_directive final_report capability must match progress status")
    for effect in directive.control.effects:
        if effect.origin_state != snapshot.current_state:
            raise ValueError(f"{label} agent_directive effect origin_state must match current state")
        if effect.kind not in allowed_effect_kinds:
            raise ValueError(f"{label} agent_directive effect kind is not allowed for current state")


def agent_directive_from_progress_view_model(
    view_model: WorkflowProgressViewModel,
    *,
    schema: str = AGENT_DIRECTIVE_SCHEMA,
    reason: str,
    report_requires: list[str],
    summary: str,
    effects: Sequence[WorkflowEffect | AgentEffect | JsonObject] | None = None,
    blockers: list[str] | None = None,
    resume: str = "",
    instructions: list[str] | None = None,
    effect_payload_projector: Callable[[object], JsonObject] | None = None,
) -> AgentDirective:
    status = _directive_status(view_model.status)
    final_report = status in {"completed", "completed_with_warnings"}
    required_reason = _required_directive_text(reason, field_name="reason")
    required_summary = _required_directive_text(summary, field_name="summary")
    required_report_requires = _required_directive_report_requires(report_requires)
    control = {
        "status": status,
        "state": view_model.state,
        "phase": view_model.phase,
        "reason": required_reason,
        "capabilities": {
            "continue": view_model.can_continue_now,
            "final_report": final_report,
        },
        "effects": [
            _project_agent_effect(
                effect,
                effect_payload_projector=effect_payload_projector or _default_effect_payload_projector,
            ).to_payload()
            for effect in effects or []
        ],
        "blockers": blockers or [],
        "resume": resume or view_model.resume_action or "",
        "report": {"requires": required_report_requires},
        "limits": {
            "raw_content": False,
            "absolute_paths": False,
            "ad_hoc_scripts": False,
        },
    }
    return AgentDirective.model_validate(
        {
            "schema": schema,
            "workflow": view_model.workflow,
            "run_id": view_model.run_id,
            "control": control,
            "summary": required_summary,
            "instructions": instructions or _directive_instructions(status),
        }
    )


def _project_agent_effect(
    effect: WorkflowEffect | AgentEffect | JsonObject,
    *,
    effect_payload_projector: Callable[[object], JsonObject],
) -> AgentEffect:
    if isinstance(effect, AgentEffect):
        return effect
    if isinstance(effect, WorkflowEffect):
        payload = effect_payload_projector(effect.payload)
        return AgentEffect.model_validate(
            {
                "effect_id": effect.effect_id,
                "kind": effect.kind,
                "target": effect.target,
                "origin_state": effect.origin_state,
                "resume_action": effect.resume_action,
                "mutates_resources": effect.mutates_resources,
                "no_resource_mutation": effect.no_resource_mutation,
                "rollback_declared": effect.rollback_declared,
                "requires_receipt": effect.requires_receipt,
                "requires_attestation": effect.requires_attestation,
                "model_policy": effect.model_policy,
                "metadata": effect.metadata,
                "payload_schema": _json_str_field(payload, "schema"),
                "payload": payload,
            }
        )
    return AgentEffect.model_validate(effect)


def _default_effect_payload_projector(value: object) -> JsonObject:
    """Validate effect payloads without applying domain-specific redaction."""

    return JsonObjectAdapter.validate_python(value)


def _directive_status(status: WorkflowProgressStatus) -> str:
    match status:
        case WorkflowProgressStatus.RUNNING:
            return "running"
        case WorkflowProgressStatus.WAITING_AGENT:
            return "waiting_agent"
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "waiting_external"
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "waiting_human"
        case WorkflowProgressStatus.BLOCKED:
            return "blocked"
        case WorkflowProgressStatus.FAILED:
            return "failed"
        case WorkflowProgressStatus.COMPLETED:
            return "completed"
        case WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return "completed_with_warnings"
        case WorkflowProgressStatus.IDLE:
            raise ValueError("idle progress cannot produce agent directive")


def _directive_instructions(status: str) -> list[str]:
    match status:
        case "running":
            return ["aguarde o resultado final do workflow antes de concluir."]
        case "waiting_agent":
            return ["continue pela rota oficial antes do relatorio final."]
        case "waiting_external":
            return ["reporte o progresso parcial e aguarde a condicao externa."]
        case "waiting_human":
            return ["peca a decisao humana pelo pacote oficial antes de continuar."]
        case "blocked":
            return ["reporte o bloqueio literal; nao declare sucesso do workflow."]
        case "failed":
            return ["reporte a falha literal e o contexto de erro."]
        case "completed" | "completed_with_warnings":
            return ["escreva o relatorio final usando os relatorios oficiais."]
        case _:
            raise ValueError(f"unsupported directive status: {status}")


def _required_directive_text(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    return cleaned


def _required_directive_report_requires(value: list[str]) -> list[str]:
    cleaned = [item.strip() for item in value if item.strip()]
    if not cleaned:
        raise ValueError("report_requires must be non-empty")
    return cleaned
