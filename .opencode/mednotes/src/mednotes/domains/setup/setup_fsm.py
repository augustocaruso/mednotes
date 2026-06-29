"""Public projection for `/mednotes:setup` StateChart results.

This module is a projector, not a second state engine. It formats the persisted
`WorkflowModel` produced by `setup_machine.py` into the public FSM-first shape
consumed by hooks, agents and user-facing reports.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.setup.setup_machine import (
    SETUP_WORKFLOW,
    ConfigValidationBlockedEvent,
    ConfigValidationCompletedEvent,
    SetupMachine,
    SetupState,
    SetupVaultAdapterPayload,
    category_for_setup_state,
    resume_action_for_setup_state,
    setup_event_from_vault_adapter_payload,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffectKind
from mednotes.kernel.fsm_event import WorkflowEventLike
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.progress import (
    WorkflowProgressEventType,
    WorkflowProgressState,
    WorkflowProgressStatus,
    WorkflowProgressViewModel,
    build_progress_view_model,
    progress_state_from_view_model,
)
from mednotes.kernel.public_report import (
    WorkflowPrimaryObjectiveSummary,
    WorkflowPublicReport,
    WorkflowReports,
    assert_public_report_matches_progress,
    public_progress_followup_line,
)
from mednotes.kernel.state_machine import (
    WorkflowStateCategory,
    WorkflowStateMachineSnapshot,
    WorkflowTransition,
    send_workflow_event,
)
from mednotes.kernel.workflow import (
    HumanDecisionPacket,
    ReceiptStatus,
    VersionControlSafety,
    WorkflowDecision,
    WorkflowReceiptPayload,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

SETUP_SCHEMA = "medical-notes-workbench.setup-fsm-result.v1"
SETUP_RECEIPT_SCHEMA = "medical-notes-workbench.setup-receipt.v1"
SETUP_AGENT_DIRECTIVE_FIELD = "agent_directive"
SETUP_FAILED_CATEGORIES = frozenset({WorkflowStateCategory.FAILED.value})
SETUP_RECEIPT_TERMINAL_EMPTY_ACTION_STATUSES = frozenset({WorkflowProgressStatus.COMPLETED})
SETUP_RECEIPT_HUMAN_DECISION_STATUSES = frozenset({WorkflowProgressStatus.WAITING_HUMAN})

SETUP_ALLOWED_ROOT_KEYS = frozenset(
    {
        "schema",
        "workflow",
        "run_id",
        "state_machine_snapshot",
        "progress_view_model",
        "decision",
        "human_decision_packet",
        "receipt",
        "reports",
        "agent_directive",
        "artifacts",
        "version_control_safety",
        "diagnostic_context",
        "error_context",
    }
)
SETUP_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "workflow_exit_code",
        "public_report",
        "orchestration_plan",
    }
)


class _SetupPayloadProgressView(ContractModel):
    status: StrictStr


class _SetupPayloadSnapshot(ContractModel):
    current_category: StrictStr


class _SetupPayloadReceipt(ContractModel):
    status: StrictStr


class _SetupPayloadFields(ContractModel):
    progress_view_model: _SetupPayloadProgressView
    state_machine_snapshot: _SetupPayloadSnapshot
    receipt: _SetupPayloadReceipt


class SetupFsmResult(ContractModel):
    """Canonical public setup result projected from the StateChart model."""

    schema_id: Literal["medical-notes-workbench.setup-fsm-result.v1"] = Field(default=SETUP_SCHEMA, alias="schema")
    workflow: Literal["/mednotes:setup"] = SETUP_WORKFLOW
    run_id: str = Field(min_length=1)
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    state_machine_snapshot: WorkflowStateMachineSnapshot
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: WorkflowReceiptPayload
    reports: WorkflowReports
    agent_directive: JsonObject
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _hydrate_progress_state_from_public_payload(cls, value: object) -> object:
        """Accept public payloads where progress_state is intentionally hidden."""

        if not isinstance(value, dict) or "progress_state" in value or "progress_view_model" not in value:
            return value
        hydrated = dict(value)
        progress_view = WorkflowProgressViewModel.model_validate(value["progress_view_model"])
        hydrated["progress_state"] = progress_state_from_view_model(progress_view).to_payload()
        return hydrated

    @model_validator(mode="after")
    def _progress_view_model_matches_state(self) -> SetupFsmResult:
        expected = build_progress_view_model(self.progress_state).to_payload()
        if self.progress_view_model.to_payload() != expected:
            raise ValueError("progress_view_model must match progress_state")
        return self

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "schema": self.schema_id,
            "workflow": self.workflow,
            "run_id": self.run_id,
            "state_machine_snapshot": self.state_machine_snapshot.to_payload(),
            "progress_view_model": self.progress_view_model.to_payload(),
            "decision": self.decision.to_payload() if self.decision is not None else None,
            "human_decision_packet": self.human_decision_packet.to_payload()
            if self.human_decision_packet is not None
            else None,
            "receipt": self.receipt.to_payload(),
            "reports": self.reports.to_payload(),
            "agent_directive": dict(self.agent_directive),
            "artifacts": dict(self.artifacts),
            "version_control_safety": self.version_control_safety.to_payload(),
            "error_context": dict(self.error_context),
        }
        if self.diagnostic_context:
            payload["diagnostic_context"] = dict(self.diagnostic_context)
        payload = JsonObjectAdapter.validate_python(payload)
        assert_setup_fsm_payload(payload)
        return payload


def build_setup_fsm_result(
    model: WorkflowModel,
    *,
    version_control_safety: VersionControlSafety | None = None,
    error_context: JsonObject | None = None,
) -> SetupFsmResult:
    """Project a persisted setup machine model into the public FSM contract."""

    _validate_setup_model(model)
    state = SetupState(model.state)
    category = category_for_setup_state(state)
    progress_state = _progress_state(model, state, category)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _snapshot(model, state, category)
    receipt = _receipt(
        model,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        version_control_safety=version_control_safety or _default_version_control_safety(),
    )
    reports_model = _reports(state, progress_state)
    public_report = reports_model.public_report
    diagnostic_context = _diagnostic_context(model, state, category)
    directive = agent_directive_from_progress_view_model(
        progress_view_model,
        schema="medical-notes-workbench.agent-directive.v1",
        reason=_reason_code(model, state),
        effects=model.pending_effects,
        blockers=_blockers_for(category, state),
        resume=progress_state.resume_action,
        report_requires=["setup_state", "recovery_route"],
        summary=public_report.summary_text(),
        instructions=_agent_instructions(category),
    ).to_payload()
    return SetupFsmResult(
        run_id=model.run_id,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
        decision=model.last_transition.decision if model.last_transition is not None else None,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        receipt=receipt,
        reports=reports_model,
        agent_directive=directive,
        version_control_safety=receipt.version_control_safety,
        diagnostic_context=diagnostic_context,
        error_context=error_context or _setup_error_context(model, state, category, progress_state),
    )


def _setup_error_context(
    model: WorkflowModel,
    state: SetupState,
    category: WorkflowStateCategory,
    progress_state: WorkflowProgressState,
) -> JsonObject:
    """Produce a recoverable failure context for terminal setup failures."""

    if category != WorkflowStateCategory.FAILED:
        return {}
    reason_code = _reason_code(model, state)
    next_action = (
        progress_state.resume_action
        or "Revisar a política/ambiente informado e retomar /mednotes:setup pela rota oficial."
    )
    return {
        "blocked_reason": reason_code,
        "root_cause": reason_code,
        "affected_artifact": "mednotes_setup_environment",
        "error_summary": "O setup terminou em falha antes de preparar o Workbench.",
        "suggested_fix": next_action,
        "next_action": next_action,
        "retry_scope": "setup_environment",
        "human_decision_required": False,
        "missing_inputs": [],
    }


def setup_fsm_payload_from_model(model: WorkflowModel) -> JsonObject:
    """Convenience boundary for callers that need the JSON payload directly."""

    return build_setup_fsm_result(model).to_payload()


class _SetupConfigValidationPayload(BaseModel):
    """Typed adapter lens for the private setup config validation receipt."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_id: Literal["medical-notes-workbench.setup-config-validation.v1"] = Field(alias="schema")
    status: Literal["valid", "blocked"]
    config_path: str = Field(min_length=1)
    reason_code: Literal["config_encoding_invalid"] | None = None

    @model_validator(mode="after")
    def _blocked_status_requires_reason(self) -> _SetupConfigValidationPayload:
        match self.status:
            case "blocked":
                if self.reason_code != "config_encoding_invalid":
                    raise ValueError("blocked setup config validation requires reason_code=config_encoding_invalid")
            case "valid":
                if self.reason_code is not None:
                    raise ValueError("valid setup config validation must not carry a blocker reason")
        return self


def setup_fsm_payload_from_vault_payload(payload: object, *, run_id: str = "setup-vault") -> JsonObject:
    """Convert the vault setup adapter result into the public setup FSM payload."""

    raw = SetupVaultAdapterPayload.model_validate(payload)
    initial_state, event = setup_event_from_vault_adapter_payload(raw, run_id=run_id)
    model = WorkflowModel.start(workflow=SETUP_WORKFLOW, run_id=run_id, initial_state=initial_state.value)
    # `setup_event_from_vault_adapter_payload` returns the closed discriminated
    # setup event union; every member has the StateChart `name` field required by
    # the kernel protocol even though the abstract base intentionally does not.
    send_workflow_event(
        SetupMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        cast(WorkflowEventLike, event),
    )
    return build_setup_fsm_result(model).to_payload()


def setup_fsm_payload_from_config_validation_payload(
    payload: object,
    *,
    run_id: str = "setup-config-validation",
) -> JsonObject:
    """Convert the config validation adapter receipt into a setup FSM payload."""

    raw = _SetupConfigValidationPayload.model_validate(payload)
    model = WorkflowModel.start(
        workflow=SETUP_WORKFLOW,
        run_id=run_id,
        initial_state=SetupState.CONFIG_VALIDATION_RUNNING.value,
    )
    machine = SetupMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    match raw.status:
        case "valid":
            send_workflow_event(
                machine,
                ConfigValidationCompletedEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=SetupState.CONFIG_VALIDATION_RUNNING.value,
                    config_path=raw.config_path,
                ),
            )
        case "blocked":
            send_workflow_event(
                machine,
                ConfigValidationBlockedEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=SetupState.CONFIG_VALIDATION_RUNNING.value,
                    reason_code="config_encoding_invalid",
                    config_path=raw.config_path,
                    audit_evidence={"config_path": raw.config_path},
                ),
            )
    return build_setup_fsm_result(model).to_payload()


def assert_setup_fsm_payload(payload: JsonObject) -> None:
    """Reject stale setup payload shapes before hooks or agents consume them."""

    required = SETUP_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing = required - set(payload)
    if missing:
        raise ValueError("setup FSM payload missing canonical root keys: " + ", ".join(sorted(missing)))
    forbidden = SETUP_FORBIDDEN_ROOT_KEYS & set(payload)
    if forbidden:
        raise ValueError("setup FSM payload contains forbidden root keys: " + ", ".join(sorted(forbidden)))
    extra = set(payload) - SETUP_ALLOWED_ROOT_KEYS
    if extra:
        raise ValueError("setup FSM payload contains unknown root keys: " + ", ".join(sorted(extra)))
    diagnostic_context = payload["diagnostic_context"] if "diagnostic_context" in payload else {}
    assert_diagnostic_context_evidence_only(diagnostic_context)
    if isinstance(diagnostic_context, dict) and "agent_directive" in diagnostic_context:
        raise ValueError("setup FSM diagnostic_context must not contain agent_directive")
    fields = _setup_payload_fields(payload)
    if fields.progress_view_model.status != fields.state_machine_snapshot.current_category:
        raise ValueError("setup FSM status must match state_machine_snapshot category")
    if fields.receipt.status != fields.progress_view_model.status:
        raise ValueError("setup FSM receipt status must match progress_view_model")
    if fields.progress_view_model.status in SETUP_FAILED_CATEGORIES and not payload["error_context"]:
        raise ValueError("setup FSM failed payload requires error_context")
    reports_model = WorkflowReports.model_validate(payload["reports"])
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    assert_public_report_matches_progress(
        reports_model.public_report,
        workflow=SETUP_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="setup FSM",
    )
    assert_agent_directive_matches_progress(
        AgentDirective.model_validate(payload[SETUP_AGENT_DIRECTIVE_FIELD]),
        workflow=SETUP_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=_allowed_agent_effect_kinds_for_category(snapshot.current_category),
        label="setup FSM",
    )


def _allowed_agent_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    """Setup currently recovers by human/runtime action, not hidden effects."""

    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def _setup_payload_fields(payload: JsonObject) -> _SetupPayloadFields:
    raw_fields: JsonObject = {
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status",)),
        "state_machine_snapshot": _json_object_subset(payload, "state_machine_snapshot", ("current_category",)),
        "receipt": _json_object_subset(payload, "receipt", ("status",)),
    }
    try:
        return _SetupPayloadFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"setup FSM payload invalid: {loc}: {msg}") from exc


def _json_object_subset(payload: JsonObject, key: str, fields: tuple[str, ...]) -> JsonObject:
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"setup FSM payload {key} must be an object")
    subset = {field: value[field] for field in fields if field in value}
    return JsonObjectAdapter.validate_python(subset)


def _validate_setup_model(model: WorkflowModel) -> None:
    if model.workflow != SETUP_WORKFLOW:
        raise ValueError(f"setup FSM projector requires workflow={SETUP_WORKFLOW}")
    SetupState(model.state)


def _progress_state(
    model: WorkflowModel,
    state: SetupState,
    category: WorkflowStateCategory,
) -> WorkflowProgressState:
    status = _progress_status(category)
    return WorkflowProgressState(
        workflow=SETUP_WORKFLOW,
        run_id=model.run_id,
        state=state.value,
        phase=_phase_for_state(state),
        event_type=_event_type_for_status(status),
        message=_message_for_state(state),
        status=status,
        resume_action=_resume_action(model, state),
        resume_supported=status in {
            WorkflowProgressStatus.WAITING_AGENT,
            WorkflowProgressStatus.WAITING_EXTERNAL,
            WorkflowProgressStatus.WAITING_HUMAN,
        },
        can_continue_now=status
        in {
            WorkflowProgressStatus.RUNNING,
            WorkflowProgressStatus.WAITING_AGENT,
        },
        decision=model.last_transition.decision.to_payload()
        if model.last_transition is not None and model.last_transition.decision is not None
        else None,
        technical_context={"reason": _reason_code(model, state), "category": category.value},
    )


def _snapshot(
    model: WorkflowModel,
    state: SetupState,
    category: WorkflowStateCategory,
) -> WorkflowStateMachineSnapshot:
    return WorkflowStateMachineSnapshot(
        workflow=SETUP_WORKFLOW,
        run_id=model.run_id,
        current_state=state.value,
        current_category=category,
        transitions=[_snapshot_transition(transition) for transition in model.transition_log],
        metadata={"reason": _reason_code(model, state)},
    )


def _snapshot_transition(transition: WorkflowTransitionResult) -> WorkflowTransition:
    return WorkflowTransition(
        workflow=transition.workflow,
        from_state=transition.from_state,
        to_state=transition.to_state,
        to_category=category_for_setup_state(SetupState(transition.to_state)),
        trigger=transition.trigger,
        effects=list(transition.effects),
        decision=transition.decision,
        resume_action=transition.resume_action,
    )


def _receipt(
    model: WorkflowModel,
    *,
    progress_state: WorkflowProgressState,
    progress_view_model: WorkflowProgressViewModel,
    snapshot: WorkflowStateMachineSnapshot,
    version_control_safety: VersionControlSafety,
) -> WorkflowReceiptPayload:
    return WorkflowReceiptPayload(
        schema=SETUP_RECEIPT_SCHEMA,
        workflow=SETUP_WORKFLOW,
        run_id=model.run_id,
        status=_receipt_status(progress_state.status),
        mutated=False,
        next_action="" if progress_state.status in SETUP_RECEIPT_TERMINAL_EMPTY_ACTION_STATUSES else progress_state.resume_action,
        human_decision_required=progress_state.status in SETUP_RECEIPT_HUMAN_DECISION_STATUSES,
        human_decision_packet=model.last_transition.human_decision_packet if model.last_transition is not None else None,
        version_control_safety=version_control_safety,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
    )


def _receipt_status(status: WorkflowProgressStatus) -> ReceiptStatus:
    match status:
        case WorkflowProgressStatus.RUNNING:
            return "running"
        case WorkflowProgressStatus.COMPLETED:
            return "completed"
        case WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return "completed_with_warnings"
        case WorkflowProgressStatus.WAITING_AGENT:
            return "waiting_agent"
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "waiting_external"
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "waiting_human"
        case WorkflowProgressStatus.FAILED:
            return "failed"
        case WorkflowProgressStatus.BLOCKED:
            return "blocked"
        case _:
            return "blocked"


def _progress_status(category: WorkflowStateCategory) -> WorkflowProgressStatus:
    match category:
        case WorkflowStateCategory.PREPARING | WorkflowStateCategory.RUNNING:
            return WorkflowProgressStatus.RUNNING
        case WorkflowStateCategory.WAITING_AGENT:
            return WorkflowProgressStatus.WAITING_AGENT
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return WorkflowProgressStatus.WAITING_EXTERNAL
        case WorkflowStateCategory.WAITING_HUMAN:
            return WorkflowProgressStatus.WAITING_HUMAN
        case WorkflowStateCategory.BLOCKED:
            return WorkflowProgressStatus.BLOCKED
        case WorkflowStateCategory.FAILED:
            return WorkflowProgressStatus.FAILED
        case WorkflowStateCategory.COMPLETED:
            return WorkflowProgressStatus.COMPLETED
        case WorkflowStateCategory.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressStatus.COMPLETED_WITH_WARNINGS


def _event_type_for_status(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    match status:
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case WorkflowProgressStatus.WAITING_HUMAN:
            return WorkflowProgressEventType.DECISION_EMITTED
        case _:
            return WorkflowProgressEventType.STATE_ENTERED


def _phase_for_state(state: SetupState) -> str:
    match state:
        case SetupState.CHECKING_ENVIRONMENT | SetupState.PYTHON_ENV_REQUIRED | SetupState.PYTHON_ENV_READY:
            return "environment"
        case (
            SetupState.PATHS_REQUIRED
            | SetupState.PATHS_CONFIGURED
            | SetupState.CONFIG_VALIDATION_RUNNING
            | SetupState.CONFIG_ENCODING_REQUIRED
        ):
            return "paths"
        case (
            SetupState.OBSIDIAN_NOT_READY
            | SetupState.MARKDOWN_RUNTIME_REQUIRED
            | SetupState.MARKDOWN_INDEX_REQUIRED
            | SetupState.MARKDOWN_RUNTIME_READY
        ):
            return "markdown_runtime"
        case SetupState.VAULT_GUARD_REQUIRED | SetupState.VAULT_LOCAL_READY:
            return "vault_guard"
        case (
            SetupState.LOCAL_READY_GITHUB_PENDING
            | SetupState.GITHUB_LOGIN_REQUIRED
            | SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED
            | SetupState.GITHUB_REMOTE_AMBIGUOUS
        ):
            return "remote_backup"
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return "branch_policy"
        case SetupState.POLICY_DECISION_REQUIRED:
            return "policy"
        case SetupState.READY:
            return "ready"
        case SetupState.FAILED:
            return "failed"


def _message_for_state(state: SetupState) -> str:
    match state:
        case SetupState.PATHS_REQUIRED:
            return "Setup aguardando os caminhos oficiais da Wiki e dos raw chats."
        case SetupState.PYTHON_ENV_REQUIRED:
            return "Setup precisa preparar Python/uv pela rota oficial."
        case SetupState.CONFIG_VALIDATION_RUNNING:
            return "Setup precisa validar a configuracao antes de continuar."
        case SetupState.CONFIG_ENCODING_REQUIRED:
            return "Setup precisa reparar a configuracao em UTF-8."
        case SetupState.OBSIDIAN_NOT_READY:
            return "Setup aguardando Obsidian/plugin ficar pronto."
        case SetupState.MARKDOWN_RUNTIME_REQUIRED:
            return "Setup precisa reconstruir o runtime Markdown."
        case SetupState.MARKDOWN_INDEX_REQUIRED:
            return "Setup precisa reconstruir o indice Markdown."
        case SetupState.VAULT_GUARD_REQUIRED:
            return "Setup precisa configurar a protecao do vault."
        case SetupState.LOCAL_READY_GITHUB_PENDING:
            return "Protecao local pronta; backup online ainda pendente."
        case SetupState.GITHUB_LOGIN_REQUIRED:
            return "Setup aguardando decisao sobre login do GitHub."
        case SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            return "Setup aguardando confirmacao para criar backup privado."
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return "Setup aguardando escolha do remote GitHub."
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return "Setup aguardando confirmacao para ajustar a branch principal."
        case SetupState.POLICY_DECISION_REQUIRED:
            return "Setup aguardando decisao de politica do ambiente."
        case SetupState.READY:
            return "Setup concluido; workflows dependentes podem retomar."
        case SetupState.FAILED:
            return "Setup falhou antes de liberar workflows dependentes."
        case _:
            return "Setup em andamento."


def _reports(state: SetupState, progress_state: WorkflowProgressState) -> WorkflowReports:
    summary = _message_for_state(state)
    public_lines = [summary]
    followup_line = public_progress_followup_line(progress_state)
    if followup_line:
        public_lines.append(followup_line)
    public_report = WorkflowPublicReport(
        workflow=SETUP_WORKFLOW,
        run_id=progress_state.run_id,
        headline=summary,
        lines=public_lines,
    )
    return WorkflowReports(
        summary=summary,
        public_report=public_report,
        details={
            "primary_objective_summary": _setup_primary_objective_summary(
                state=state,
                progress_state=progress_state,
            ).to_payload()
        },
    )


def _setup_primary_objective_summary(
    *,
    state: SetupState,
    progress_state: WorkflowProgressState,
) -> WorkflowPrimaryObjectiveSummary:
    """State-owned answer to whether setup released dependent workflows."""

    completed = state == SetupState.READY
    return WorkflowPrimaryObjectiveSummary(
        workflow=SETUP_WORKFLOW,
        run_id=progress_state.run_id,
        objective="Preparar caminhos, runtime local e proteção do vault para os workflows públicos.",
        completed=completed,
        status=state.value,
        mutation_state="not_applicable",
        mutation_summary="Setup não é um fluxo de limpeza/mutação da Wiki.",
        remaining_work_summary=_setup_remaining_work_summary(state, completed),
        next_step_summary=_setup_next_step_summary(progress_state, completed),
        blocked_reason="" if completed else _reason_code_from_state(state),
    )


def _setup_remaining_work_summary(state: SetupState, completed: bool) -> str:
    if completed:
        return "Ambiente local pronto para retomar workflows dependentes."
    return _message_for_state(state)


def _setup_next_step_summary(progress_state: WorkflowProgressState, completed: bool) -> str:
    if completed:
        return "Nenhuma ação pendente para o setup local."
    return progress_state.resume_action or "Retomar /mednotes:setup pela rota oficial."


def _diagnostic_context(
    model: WorkflowModel,
    state: SetupState,
    category: WorkflowStateCategory,
) -> JsonObject:
    if category == WorkflowStateCategory.COMPLETED:
        return {}
    return diagnostic_context_evidence_only({
        "schema": "medical-notes-workbench.setup-fsm-diagnostic-context.v1",
        "state": state.value,
        "category": category.value,
        "reason": _reason_code(model, state),
    })


def _blockers_for(category: WorkflowStateCategory, state: SetupState) -> list[str]:
    if category in {
        WorkflowStateCategory.WAITING_AGENT,
        WorkflowStateCategory.WAITING_EXTERNAL,
        WorkflowStateCategory.WAITING_HUMAN,
        WorkflowStateCategory.BLOCKED,
        WorkflowStateCategory.FAILED,
    }:
        return [_reason_code_from_state(state)]
    return []


def _agent_instructions(category: WorkflowStateCategory) -> list[str]:
    if category == WorkflowStateCategory.WAITING_AGENT:
        return ["Execute somente os efeitos em agent_directive.control.effects e retome o setup depois do resultado."]
    if category == WorkflowStateCategory.WAITING_EXTERNAL:
        return ["Aguarde a condicao externa indicada; nao fabrique sucesso enquanto ela nao estiver pronta."]
    if category == WorkflowStateCategory.WAITING_HUMAN:
        return ["Peca a decisao humana fechada indicada no human_decision_packet antes de continuar."]
    if category == WorkflowStateCategory.COMPLETED:
        return ["Reporte que o setup esta pronto para os workflows dependentes."]
    return ["Use o estado da FSM como fonte de verdade do setup."]


def _resume_action(model: WorkflowModel, state: SetupState) -> str:
    if state == SetupState.READY:
        return ""
    if model.last_transition is not None and model.last_transition.resume_action:
        return model.last_transition.resume_action
    return resume_action_for_setup_state(state)


def _reason_code(model: WorkflowModel, state: SetupState) -> str:
    if model.last_transition is not None:
        return model.last_transition.reason_code
    return _reason_code_from_state(state)


def _reason_code_from_state(state: SetupState) -> str:
    match state:
        case SetupState.PATHS_REQUIRED:
            return "paths_missing"
        case SetupState.PYTHON_ENV_REQUIRED:
            return "environment_blocker.windows_path_or_venv"
        case SetupState.CONFIG_VALIDATION_RUNNING:
            return "config_validation_required"
        case SetupState.CONFIG_ENCODING_REQUIRED:
            return "config_encoding_invalid"
        case SetupState.OBSIDIAN_NOT_READY:
            return "obsidian_not_ready"
        case SetupState.MARKDOWN_RUNTIME_REQUIRED:
            return "markdown_runtime_missing"
        case SetupState.MARKDOWN_INDEX_REQUIRED:
            return "markdown_index_missing"
        case SetupState.VAULT_GUARD_REQUIRED:
            return "vault_guard_required"
        case SetupState.LOCAL_READY_GITHUB_PENDING:
            return "github_remote_missing"
        case SetupState.GITHUB_LOGIN_REQUIRED:
            return "github_login_required"
        case SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            return "github_remote_missing"
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return "github_remote_ambiguous"
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return "blocked_branch_confirmation_required"
        case SetupState.POLICY_DECISION_REQUIRED:
            return "unsupported_host_or_policy_gap"
        case SetupState.FAILED:
            return "setup_failed"
        case _:
            return state.value


def _default_version_control_safety() -> VersionControlSafety:
    return VersionControlSafety(
        no_resource_mutation=True,
        rollback_declared=False,
        direct_mutation_forbidden=True,
        agent_instruction="Setup nao deve mutar conteudo medico da Wiki.",
    )
