"""Operational StateChart for `/mednotes:setup`.

Setup is a public workflow because other workflows recover through it. The
machine therefore owns the recovery state itself: paths, Python/uv, Markdown
runtime, Obsidian readiness, vault guard and remote-policy choices are leaf
states, not `blocked + reason` projections.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from statemachine import StateChart
from statemachine.states import States

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effect_intent import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.state_machine import WorkflowStateCategory
from mednotes.kernel.workflow import (
    DecisionEvidence,
    HumanDecisionOption,
    HumanDecisionPacket,
    RejectedAutomation,
    WorkflowDecision,
)

SETUP_WORKFLOW: Literal["/mednotes:setup"] = "/mednotes:setup"


class SetupState(StrEnum):
    CHECKING_ENVIRONMENT = "checking_environment"
    PATHS_REQUIRED = "paths_required"
    PATHS_CONFIGURED = "paths_configured"
    CONFIG_VALIDATION_RUNNING = "config_validation_running"
    CONFIG_ENCODING_REQUIRED = "config_encoding_required"
    PYTHON_ENV_REQUIRED = "python_env_required"
    PYTHON_ENV_READY = "python_env_ready"
    OBSIDIAN_NOT_READY = "obsidian_not_ready"
    MARKDOWN_RUNTIME_REQUIRED = "markdown_runtime_required"
    MARKDOWN_INDEX_REQUIRED = "markdown_index_required"
    MARKDOWN_RUNTIME_READY = "markdown_runtime_ready"
    VAULT_GUARD_REQUIRED = "vault_guard_required"
    VAULT_LOCAL_READY = "vault_local_ready"
    LOCAL_READY_GITHUB_PENDING = "local_ready_github_pending"
    GITHUB_LOGIN_REQUIRED = "github_login_required"
    GITHUB_REMOTE_CONFIRMATION_REQUIRED = "github_remote_confirmation_required"
    GITHUB_REMOTE_AMBIGUOUS = "github_remote_ambiguous"
    BRANCH_CONFIRMATION_REQUIRED = "branch_confirmation_required"
    POLICY_DECISION_REQUIRED = "policy_decision_required"
    READY = "ready"
    FAILED = "failed"


class SetupVaultOutcome(StrEnum):
    """Canonical outcome for the private vault setup adapter boundary."""

    READY = "ready"
    LOCAL_READY_GITHUB_PENDING = "local_ready_github_pending"
    GITHUB_LOGIN_REQUIRED = "github_login_required"
    GITHUB_REMOTE_CONFIRMATION_REQUIRED = "github_remote_confirmation_required"
    GITHUB_REMOTE_AMBIGUOUS = "github_remote_ambiguous"
    BRANCH_CONFIRMATION_REQUIRED = "branch_confirmation_required"
    PYTHON_ENV_BLOCKED = "python_env_blocked"
    UNSUPPORTED_OR_POLICY_GAP = "unsupported_or_policy_gap"


class SetupVaultDecisionPacket(BaseModel):
    """Typed lens over the vault adapter's private human-decision payload."""

    model_config = ConfigDict(extra="ignore", strict=True)

    kind: str = ""
    resume_action: str = ""
    current_branch: str = ""


class SetupVaultAdapterPayload(BaseModel):
    """Normalize `vault_git.py setup` output into a closed setup outcome.

    The vault script owns its private JSON shape; the setup StateChart owns the
    canonical states. This boundary is the only place where adapter strings are
    translated into a `SetupVaultOutcome`, and ignored extras cannot influence
    workflow policy.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    status_text: str = Field(default="", alias="status")
    blocker_text: str = Field(default="", alias="blocked_reason")
    outcome: SetupVaultOutcome = SetupVaultOutcome.UNSUPPORTED_OR_POLICY_GAP
    summary_text: str = ""
    human_message: str = ""
    local_ready: bool = False
    github_ready: bool = False
    human_decision_required: bool = False
    human_decision_packet: SetupVaultDecisionPacket | None = None
    current_branch: str = ""

    @model_validator(mode="after")
    def _derive_outcome(self) -> SetupVaultAdapterPayload:
        status_text = self.status_text
        blocker_text = self.blocker_text
        decision_kind = self.human_decision_packet.kind if self.human_decision_packet is not None else ""
        resume_action = self.human_decision_packet.resume_action if self.human_decision_packet is not None else ""
        object.__setattr__(self, "summary_text", self.human_message or status_text or "Setup do vault avaliado.")
        if status_text == "ready" or (self.local_ready and self.github_ready):
            object.__setattr__(self, "outcome", SetupVaultOutcome.READY)
        elif (
            status_text == "blocked_branch_confirmation_required"
            or blocker_text == "non_main_branch"
            or decision_kind == "confirm_main_branch"
        ):
            object.__setattr__(self, "outcome", SetupVaultOutcome.BRANCH_CONFIRMATION_REQUIRED)
        elif blocker_text == "github_login_required" or resume_action == "--start-github-login":
            object.__setattr__(self, "outcome", SetupVaultOutcome.GITHUB_LOGIN_REQUIRED)
        elif status_text == "awaiting_remote_confirmation":
            object.__setattr__(self, "outcome", SetupVaultOutcome.GITHUB_REMOTE_CONFIRMATION_REQUIRED)
        elif self.human_decision_required and self.local_ready:
            object.__setattr__(self, "outcome", SetupVaultOutcome.GITHUB_REMOTE_AMBIGUOUS)
        elif status_text == "local_ready_github_pending" or self.local_ready:
            object.__setattr__(self, "outcome", SetupVaultOutcome.LOCAL_READY_GITHUB_PENDING)
        elif status_text == "blocked_missing_git" or blocker_text in {
            "missing_git",
            "git_missing",
            "environment_blocker.windows_path_or_venv",
        }:
            object.__setattr__(self, "outcome", SetupVaultOutcome.PYTHON_ENV_BLOCKED)
        else:
            object.__setattr__(self, "outcome", SetupVaultOutcome.UNSUPPORTED_OR_POLICY_GAP)
        return self


class SetupEvent(ContractModel):
    """Base event accepted by the setup StateChart."""

    workflow: str = SETUP_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_setup(cls, value: str) -> str:
        if value != SETUP_WORKFLOW:
            raise ValueError(f"setup event workflow must be {SETUP_WORKFLOW}")
        return value


def _event_name(event: SetupEvent) -> str:
    """Return the concrete Literal discriminator declared by each event class."""

    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("setup events must declare a name discriminator")
    return name


class PathsMissingEvent(SetupEvent):
    name: Literal["paths_missing"] = "paths_missing"
    reason_code: Literal["paths_missing", "wiki_dir_missing"]
    missing_path_kind: Literal["wiki_dir", "raw_dir", "both"]


class PathsOkEvent(SetupEvent):
    name: Literal["paths_ok"] = "paths_ok"
    config_path: str = Field(min_length=1)


class PathsConfiguredEvent(SetupEvent):
    name: Literal["paths_configured"] = "paths_configured"
    config_path: str = Field(min_length=1)


class PythonOrUvInvalidEvent(SetupEvent):
    name: Literal["python_or_uv_invalid"] = "python_or_uv_invalid"
    reason_code: Literal["environment_blocker.windows_path_or_venv"]


class PythonEnvBootstrappedEvent(SetupEvent):
    name: Literal["python_env_bootstrapped"] = "python_env_bootstrapped"
    summary: str = Field(min_length=1)


class ConfigEncodingInvalidEvent(SetupEvent):
    name: Literal["config_encoding_invalid"] = "config_encoding_invalid"
    reason_code: Literal["config_encoding_invalid"]


class ConfigValidationCompletedEvent(SetupEvent):
    name: Literal["config_validation_completed"] = "config_validation_completed"
    config_path: str = Field(min_length=1)


class ConfigValidationBlockedEvent(SetupEvent):
    name: Literal["config_validation_blocked"] = "config_validation_blocked"
    reason_code: Literal["config_encoding_invalid"]
    config_path: str = Field(min_length=1)


class ConfigRepairedEvent(SetupEvent):
    name: Literal["config_repaired"] = "config_repaired"
    config_path: str = Field(min_length=1)


class ObsidianNotReadyEvent(SetupEvent):
    name: Literal["obsidian_not_ready"] = "obsidian_not_ready"
    reason_code: Literal["obsidian_not_ready"]


class ObsidianReadyEvent(SetupEvent):
    name: Literal["obsidian_ready"] = "obsidian_ready"
    summary: str = Field(min_length=1)


class MarkdownRuntimeMissingEvent(SetupEvent):
    name: Literal["markdown_runtime_missing"] = "markdown_runtime_missing"
    reason_code: Literal[
        "markdown_runtime_missing",
        "markdown_runtime_stale",
        "node_runtime_missing",
        "node_runtime_stale",
    ]


class MarkdownRuntimeOkEvent(SetupEvent):
    name: Literal["markdown_runtime_ok"] = "markdown_runtime_ok"
    summary: str = Field(min_length=1)


class MarkdownRuntimeRebuiltEvent(SetupEvent):
    name: Literal["markdown_runtime_rebuilt"] = "markdown_runtime_rebuilt"
    summary: str = Field(min_length=1)


class MarkdownIndexMissingEvent(SetupEvent):
    name: Literal["markdown_index_missing"] = "markdown_index_missing"
    reason_code: Literal["markdown_index_missing", "markdown_index_stale"]


class MarkdownIndexRebuiltEvent(SetupEvent):
    name: Literal["markdown_index_rebuilt"] = "markdown_index_rebuilt"
    summary: str = Field(min_length=1)


class VaultGuardMissingEvent(SetupEvent):
    name: Literal["vault_guard_missing"] = "vault_guard_missing"
    reason_code: Literal["vault_guard_required"]


class VaultGuardOkEvent(SetupEvent):
    name: Literal["vault_guard_ok"] = "vault_guard_ok"
    summary: str = Field(min_length=1)


class VaultGuardConfiguredEvent(SetupEvent):
    name: Literal["vault_guard_configured"] = "vault_guard_configured"
    summary: str = Field(min_length=1)


class GithubRemoteDecisionRequiredEvent(SetupEvent):
    name: Literal["github_remote_decision_required"] = "github_remote_decision_required"
    reason_code: Literal["github_remote_missing", "github_remote_ambiguous"]


class GithubLoginRequiredEvent(SetupEvent):
    name: Literal["github_login_required"] = "github_login_required"
    reason_code: Literal["github_login_required"]


class GithubRemotePendingEvent(SetupEvent):
    name: Literal["github_remote_pending"] = "github_remote_pending"
    reason_code: Literal["github_remote_missing", "github_cli_missing"]


class BranchConfirmationRequiredEvent(SetupEvent):
    name: Literal["branch_confirmation_required"] = "branch_confirmation_required"
    reason_code: Literal["blocked_branch_confirmation_required", "non_main_branch"]


class BranchConfirmedEvent(SetupEvent):
    name: Literal["branch_confirmed"] = "branch_confirmed"
    confirmed_by: str = Field(min_length=1)


class GithubRemoteConfirmedEvent(SetupEvent):
    name: Literal["github_remote_confirmed"] = "github_remote_confirmed"
    confirmed_by: str = Field(min_length=1)


class LocalOnlyAcceptedEvent(SetupEvent):
    name: Literal["local_only_accepted"] = "local_only_accepted"
    accepted_by: str = Field(min_length=1)


class LocalReadyEvent(SetupEvent):
    name: Literal["local_ready"] = "local_ready"
    summary: str = Field(min_length=1)


class UnsupportedHostOrPolicyGapEvent(SetupEvent):
    name: Literal["unsupported_host_or_policy_gap"] = "unsupported_host_or_policy_gap"
    reason_code: Literal["unsupported_host_or_policy_gap"]


class PolicyExceptionConfiguredEvent(SetupEvent):
    name: Literal["policy_exception_configured"] = "policy_exception_configured"
    summary: str = Field(min_length=1)


class UnsupportedByPolicyEvent(SetupEvent):
    name: Literal["unsupported_by_policy"] = "unsupported_by_policy"
    reason_code: Literal["unsupported_by_policy"]


SetupBoundaryEvent = Annotated[
    PathsMissingEvent
    | PathsOkEvent
    | PathsConfiguredEvent
    | PythonOrUvInvalidEvent
    | PythonEnvBootstrappedEvent
    | ConfigEncodingInvalidEvent
    | ConfigValidationCompletedEvent
    | ConfigValidationBlockedEvent
    | ConfigRepairedEvent
    | ObsidianNotReadyEvent
    | ObsidianReadyEvent
    | MarkdownRuntimeMissingEvent
    | MarkdownRuntimeOkEvent
    | MarkdownRuntimeRebuiltEvent
    | MarkdownIndexMissingEvent
    | MarkdownIndexRebuiltEvent
    | VaultGuardMissingEvent
    | VaultGuardOkEvent
    | VaultGuardConfiguredEvent
    | GithubRemoteDecisionRequiredEvent
    | GithubLoginRequiredEvent
    | GithubRemotePendingEvent
    | BranchConfirmationRequiredEvent
    | BranchConfirmedEvent
    | GithubRemoteConfirmedEvent
    | LocalOnlyAcceptedEvent
    | LocalReadyEvent
    | UnsupportedHostOrPolicyGapEvent
    | PolicyExceptionConfiguredEvent
    | UnsupportedByPolicyEvent,
    Field(discriminator="name"),
]
SetupBoundaryEventAdapter = TypeAdapter(SetupBoundaryEvent)


def setup_event_from_vault_adapter_payload(
    payload: SetupVaultAdapterPayload,
    *,
    run_id: str,
) -> tuple[SetupState, SetupBoundaryEvent]:
    """Convert the typed vault boundary outcome into a setup StateChart event."""

    summary = payload.summary_text
    match payload.outcome:
        case SetupVaultOutcome.READY:
            state = SetupState.VAULT_LOCAL_READY
            return (
                state,
                LocalReadyEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    summary=summary,
                ),
            )
        case SetupVaultOutcome.LOCAL_READY_GITHUB_PENDING:
            state = SetupState.VAULT_LOCAL_READY
            return (
                state,
                GithubRemotePendingEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="github_remote_missing",
                ),
            )
        case SetupVaultOutcome.GITHUB_LOGIN_REQUIRED:
            state = SetupState.VAULT_LOCAL_READY
            return (
                state,
                GithubLoginRequiredEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="github_login_required",
                ),
            )
        case SetupVaultOutcome.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            state = SetupState.VAULT_LOCAL_READY
            return (
                state,
                GithubRemoteDecisionRequiredEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="github_remote_missing",
                ),
            )
        case SetupVaultOutcome.GITHUB_REMOTE_AMBIGUOUS:
            state = SetupState.VAULT_LOCAL_READY
            return (
                state,
                GithubRemoteDecisionRequiredEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="github_remote_ambiguous",
                ),
            )
        case SetupVaultOutcome.BRANCH_CONFIRMATION_REQUIRED:
            state = SetupState.CHECKING_ENVIRONMENT
            return (
                state,
                BranchConfirmationRequiredEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="blocked_branch_confirmation_required",
                ),
            )
        case SetupVaultOutcome.PYTHON_ENV_BLOCKED:
            state = SetupState.CHECKING_ENVIRONMENT
            return (
                state,
                PythonOrUvInvalidEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="environment_blocker.windows_path_or_venv",
                    audit_evidence={
                        "vault_status": payload.status_text,
                        "vault_blocker": payload.blocker_text,
                    },
                ),
            )
        case SetupVaultOutcome.UNSUPPORTED_OR_POLICY_GAP:
            state = SetupState.CHECKING_ENVIRONMENT
            return (
                state,
                UnsupportedHostOrPolicyGapEvent(
                    workflow=SETUP_WORKFLOW,
                    run_id=run_id,
                    current_state=state.value,
                    reason_code="unsupported_host_or_policy_gap",
                    audit_evidence={
                        "vault_status": payload.status_text,
                        "vault_blocker": payload.blocker_text,
                    },
                ),
            )


class SetupMachine(StateChart[WorkflowModel]):
    """Pure domain setup StateChart; effects describe work, never execute it."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        SetupState,
        initial=SetupState.CHECKING_ENVIRONMENT,
        final={SetupState.LOCAL_READY_GITHUB_PENDING, SetupState.READY, SetupState.FAILED},
        use_enum_instance=False,
    )

    paths_missing = (
        states.CHECKING_ENVIRONMENT.to(states.PATHS_REQUIRED, on="_on_human_required")
        | states.PYTHON_ENV_READY.to(states.PATHS_REQUIRED, on="_on_human_required")
    )
    paths_ok = (
        states.CHECKING_ENVIRONMENT.to(states.CONFIG_VALIDATION_RUNNING, on="_on_agent_required")
        | states.PYTHON_ENV_READY.to(states.CONFIG_VALIDATION_RUNNING, on="_on_agent_required")
    )
    paths_configured = states.PATHS_REQUIRED.to(states.CONFIG_VALIDATION_RUNNING, on="_on_agent_required")

    python_or_uv_invalid = states.CHECKING_ENVIRONMENT.to(states.PYTHON_ENV_REQUIRED, on="_on_agent_required")
    python_env_bootstrapped = states.PYTHON_ENV_REQUIRED.to(states.PYTHON_ENV_READY, on="_on_transition")

    config_encoding_invalid = states.CHECKING_ENVIRONMENT.to(
        states.CONFIG_ENCODING_REQUIRED,
        on="_on_agent_required",
    )
    config_validation_completed = states.CONFIG_VALIDATION_RUNNING.to(states.PATHS_CONFIGURED, on="_on_transition")
    config_validation_blocked = states.CONFIG_VALIDATION_RUNNING.to(
        states.CONFIG_ENCODING_REQUIRED,
        on="_on_agent_required",
    )
    config_repaired = states.CONFIG_ENCODING_REQUIRED.to(states.CONFIG_VALIDATION_RUNNING, on="_on_agent_required")

    obsidian_not_ready = states.PATHS_CONFIGURED.to(states.OBSIDIAN_NOT_READY, on="_on_wait_external")
    obsidian_ready = states.OBSIDIAN_NOT_READY.to(states.PATHS_CONFIGURED, on="_on_transition")

    markdown_runtime_missing = states.PATHS_CONFIGURED.to(
        states.MARKDOWN_RUNTIME_REQUIRED,
        on="_on_agent_required",
    )
    markdown_runtime_ok = states.PATHS_CONFIGURED.to(states.MARKDOWN_RUNTIME_READY, on="_on_transition")
    markdown_runtime_rebuilt = states.MARKDOWN_RUNTIME_REQUIRED.to(
        states.MARKDOWN_RUNTIME_READY,
        on="_on_transition",
    )
    markdown_index_missing = states.PATHS_CONFIGURED.to(states.MARKDOWN_INDEX_REQUIRED, on="_on_agent_required")
    markdown_index_rebuilt = states.MARKDOWN_INDEX_REQUIRED.to(
        states.MARKDOWN_RUNTIME_READY,
        on="_on_transition",
    )

    vault_guard_missing = states.MARKDOWN_RUNTIME_READY.to(states.VAULT_GUARD_REQUIRED, on="_on_agent_required")
    vault_guard_ok = states.MARKDOWN_RUNTIME_READY.to(states.VAULT_LOCAL_READY, on="_on_transition")
    vault_guard_configured = states.VAULT_GUARD_REQUIRED.to(states.VAULT_LOCAL_READY, on="_on_transition")

    github_remote_decision_required = (
        states.VAULT_LOCAL_READY.to(
            states.GITHUB_REMOTE_CONFIRMATION_REQUIRED,
            cond="_is_github_remote_missing",
            on="_on_human_required",
        )
        | states.VAULT_LOCAL_READY.to(
            states.GITHUB_REMOTE_AMBIGUOUS,
            cond="_is_github_remote_ambiguous",
            on="_on_human_required",
        )
    )
    github_login_required = states.VAULT_LOCAL_READY.to(states.GITHUB_LOGIN_REQUIRED, on="_on_human_required")
    github_remote_pending = states.VAULT_LOCAL_READY.to(
        states.LOCAL_READY_GITHUB_PENDING,
        on="_on_transition",
    )
    branch_confirmation_required = states.CHECKING_ENVIRONMENT.to(
        states.BRANCH_CONFIRMATION_REQUIRED,
        on="_on_human_required",
    )
    branch_confirmed = states.BRANCH_CONFIRMATION_REQUIRED.to(states.CHECKING_ENVIRONMENT, on="_on_transition")
    github_remote_confirmed = (
        states.GITHUB_REMOTE_CONFIRMATION_REQUIRED.to(states.READY, on="_on_transition")
        | states.GITHUB_REMOTE_AMBIGUOUS.to(states.READY, on="_on_transition")
    )
    local_only_accepted = (
        states.GITHUB_REMOTE_CONFIRMATION_REQUIRED.to(states.READY, on="_on_transition")
        | states.GITHUB_REMOTE_AMBIGUOUS.to(states.READY, on="_on_transition")
        | states.GITHUB_LOGIN_REQUIRED.to(states.LOCAL_READY_GITHUB_PENDING, on="_on_transition")
    )
    local_ready = states.VAULT_LOCAL_READY.to(states.READY, on="_on_transition")

    unsupported_host_or_policy_gap = states.CHECKING_ENVIRONMENT.to(
        states.POLICY_DECISION_REQUIRED,
        on="_on_human_required",
    )
    policy_exception_configured = states.POLICY_DECISION_REQUIRED.to(
        states.CHECKING_ENVIRONMENT,
        on="_on_transition",
    )
    unsupported_by_policy = states.POLICY_DECISION_REQUIRED.to(states.FAILED, on="_on_failed")

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return category_for_setup_state(SetupState(state))

    def _on_transition(self, workflow_event: SetupEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(workflow_event, to_state)

    def _on_agent_required(self, workflow_event: SetupEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=str(getattr(workflow_event, "reason_code", to_state.value)),
            effects=[_setup_effect(workflow_event, to_state)],
        )

    def _on_wait_external(self, workflow_event: SetupEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        resume_action = resume_action_for_setup_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=str(getattr(workflow_event, "reason_code", to_state.value)),
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action=resume_action,
        )

    def _on_human_required(self, workflow_event: SetupEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", to_state.value))
        return _human_transition(workflow_event, to_state, reason_code=reason_code)

    def _on_failed(self, workflow_event: SetupEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", to_state.value))
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="failed", phase=to_state.value, reason_code=reason_code),
        )

    def _is_github_remote_missing(self, workflow_event: GithubRemoteDecisionRequiredEvent) -> bool:
        return workflow_event.reason_code == "github_remote_missing"

    def _is_github_remote_ambiguous(self, workflow_event: GithubRemoteDecisionRequiredEvent) -> bool:
        return workflow_event.reason_code == "github_remote_ambiguous"


def _transition(
    workflow_event: SetupEvent,
    to_state: SetupState,
    *,
    reason_code: str | None = None,
    effects: list[WorkflowEffect] | None = None,
    decision: WorkflowDecision | None = None,
    human_decision_packet: HumanDecisionPacket | None = None,
    resume_action: str = "",
) -> WorkflowTransitionResult:
    return WorkflowTransitionResult(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        from_state=workflow_event.current_state,
        to_state=to_state.value,
        trigger=_event_name(workflow_event),
        reason_code=reason_code or str(getattr(workflow_event, "reason_code", _event_name(workflow_event))),
        effects=list(effects or []),
        decision=decision,
        human_decision_packet=human_decision_packet,
        resume_action=resume_action,
    )


def _target_state(target: object) -> SetupState:
    """Read the python-statemachine transition target without touching IO."""

    value = getattr(target, "value", target)
    return SetupState(str(value))


def _setup_effect(workflow_event: SetupEvent, origin_state: SetupState) -> WorkflowEffect:
    target, payload_kind = _effect_contract_for_state(origin_state)
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"setup-{origin_state.value.replace('_', '-')}",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=target,
        payload={"kind": payload_kind, "resume_action": resume_action_for_setup_state(origin_state)},
        requires_receipt=False,
        no_resource_mutation=True,
    )


def _wait_external_effect(workflow_event: SetupEvent, origin_state: SetupState) -> WorkflowEffect:
    resume_action = resume_action_for_setup_state(origin_state)
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"setup-{origin_state.value.replace('_', '-')}-wait",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.WAIT_EXTERNAL,
        target="obsidian.plugin",
        payload={
            "schema": "medical-notes-workbench.wait-external-effect-payload.v1",
            "kind": "wait_external",
            "wait_target": "obsidian.plugin",
            "blocked_reason": "obsidian_not_ready",
            "next_action": resume_action,
            "resume_supported": True,
        },
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=resume_action,
    )


def _human_transition(
    workflow_event: SetupEvent,
    to_state: SetupState,
    *,
    reason_code: str,
) -> WorkflowTransitionResult:
    decision = _decision(kind="ask_human", phase=to_state.value, reason_code=reason_code)
    packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
    effect = WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"setup-{to_state.value.replace('_', '-')}-human-decision",
        origin_state=to_state.value,
        kind=WorkflowEffectKind.ASK_HUMAN,
        target="human.setup_decision",
        payload={"kind": "setup_human_decision", "reason_code": reason_code},
        requires_receipt=False,
        no_resource_mutation=True,
    )
    return _transition(
        workflow_event,
        to_state,
        reason_code=reason_code,
        effects=[effect],
        decision=decision,
        human_decision_packet=packet,
        resume_action=decision.resume_action,
    )


def _effect_contract_for_state(state: SetupState) -> tuple[str, str]:
    """Map recoverable setup states to the adapter command they require."""

    match state:
        case SetupState.PYTHON_ENV_REQUIRED:
            return "setup:bootstrap-python", "bootstrap_python"
        case SetupState.CONFIG_VALIDATION_RUNNING:
            return "setup:validate-config", "validate_config"
        case SetupState.CONFIG_ENCODING_REQUIRED:
            return "setup:repair-config", "repair_config"
        case SetupState.MARKDOWN_RUNTIME_REQUIRED:
            return "setup:rebuild-markdown-runtime", "rebuild_markdown_runtime"
        case SetupState.MARKDOWN_INDEX_REQUIRED:
            return "setup:rebuild-markdown-index", "rebuild_markdown_index"
        case SetupState.VAULT_GUARD_REQUIRED:
            return "setup:vault-guard", "vault_guard"
        case _:
            raise AssertionError(f"state does not emit setup effect: {state.value}")


def category_for_setup_state(state: SetupState) -> WorkflowStateCategory:
    """Map setup leaf states to public workflow categories."""

    match state:
        case (
            SetupState.CHECKING_ENVIRONMENT
            | SetupState.PYTHON_ENV_READY
            | SetupState.PATHS_CONFIGURED
            | SetupState.MARKDOWN_RUNTIME_READY
            | SetupState.VAULT_LOCAL_READY
        ):
            return WorkflowStateCategory.RUNNING
        case (
            SetupState.PATHS_REQUIRED
            | SetupState.GITHUB_LOGIN_REQUIRED
            | SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED
            | SetupState.GITHUB_REMOTE_AMBIGUOUS
            | SetupState.BRANCH_CONFIRMATION_REQUIRED
            | SetupState.POLICY_DECISION_REQUIRED
        ):
            return WorkflowStateCategory.WAITING_HUMAN
        case (
            SetupState.CONFIG_VALIDATION_RUNNING
            | SetupState.CONFIG_ENCODING_REQUIRED
            | SetupState.PYTHON_ENV_REQUIRED
            | SetupState.MARKDOWN_RUNTIME_REQUIRED
            | SetupState.MARKDOWN_INDEX_REQUIRED
            | SetupState.VAULT_GUARD_REQUIRED
        ):
            return WorkflowStateCategory.WAITING_AGENT
        case SetupState.OBSIDIAN_NOT_READY:
            return WorkflowStateCategory.WAITING_EXTERNAL
        case SetupState.LOCAL_READY_GITHUB_PENDING:
            return WorkflowStateCategory.COMPLETED_WITH_WARNINGS
        case SetupState.READY:
            return WorkflowStateCategory.COMPLETED
        case SetupState.FAILED:
            return WorkflowStateCategory.FAILED


def resume_action_for_setup_state(state: SetupState) -> str:
    """Return the user/agent-visible recovery action for a setup leaf state."""

    match state:
        case SetupState.PATHS_REQUIRED:
            return "setup:set-paths"
        case SetupState.PYTHON_ENV_REQUIRED:
            return "setup:bootstrap-python"
        case SetupState.CONFIG_VALIDATION_RUNNING:
            return "setup:validate-config"
        case SetupState.CONFIG_ENCODING_REQUIRED:
            return "setup:repair-config"
        case SetupState.MARKDOWN_RUNTIME_REQUIRED:
            return "setup:rebuild-markdown-runtime"
        case SetupState.MARKDOWN_INDEX_REQUIRED:
            return "setup:rebuild-markdown-index"
        case SetupState.VAULT_GUARD_REQUIRED:
            return "setup:vault-guard"
        case SetupState.OBSIDIAN_NOT_READY:
            return "setup:wait-obsidian"
        case SetupState.GITHUB_LOGIN_REQUIRED:
            return "setup:start-github-login"
        case SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            return "setup:confirm-github-remote"
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return "setup:resolve-ambiguous-remote"
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return "setup:confirm-main-branch"
        case SetupState.LOCAL_READY_GITHUB_PENDING:
            return "setup:choose-local-only"
        case SetupState.POLICY_DECISION_REQUIRED:
            return "setup:resolve-policy"
        case _:
            return "/mednotes:setup"


def _decision(
    *,
    kind: Literal["ask_human", "failed"],
    phase: str,
    reason_code: str,
) -> WorkflowDecision:
    resume_action = resume_action_for_setup_state(SetupState(phase)) if phase in {state.value for state in SetupState} else ""
    evidence = [
        DecisionEvidence(
            summary=f"setup StateChart reached {phase}.",
            technical_code=reason_code,
            source="setup_machine",
        )
    ]
    base: JsonObject = {
        "kind": kind,
        "phase": phase,
        "reason_code": reason_code,
        "public_summary": "O setup precisa parar nesta etapa.",
        "developer_summary": f"StateChart transition stopped at {phase}:{reason_code}.",
        "evidence": evidence,
        "next_action": resume_action or "/mednotes:setup",
        "resume_action": resume_action or "/mednotes:setup",
    }
    if kind == "ask_human":
        base.update(
            {
                "public_summary": _human_question(SetupState(phase)),
                "human_decision_kind": reason_code,
                "recommended_option_id": _recommended_option_id_for_state(SetupState(phase)),
                "options": _human_options_for_state(SetupState(phase)),
                "rejected_automations": _rejected_automations(reason_code),
            }
        )
    return WorkflowDecision(**base)


def _recommended_option_id_for_state(state: SetupState) -> str:
    match state:
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return "configure_remote"
        case _:
            return "continue"


def _human_question(state: SetupState) -> str:
    match state:
        case SetupState.PATHS_REQUIRED:
            return "Preciso que voce escolha ou confirme os caminhos da Wiki antes de continuar."
        case SetupState.GITHUB_LOGIN_REQUIRED:
            return "Preciso que voce confirme se devo abrir o login do GitHub para ativar backup online."
        case SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            return "Preciso que voce confirme se devo criar o repositorio privado de backup."
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return "Preciso que voce escolha qual remote GitHub deve ser usado."
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return "Preciso que voce confirme se posso ajustar a branch principal do vault."
        case SetupState.POLICY_DECISION_REQUIRED:
            return "Preciso que voce escolha uma rota suportada para este ambiente."
        case _:
            return "Preciso de uma decisao sua antes de continuar."


def _human_options_for_state(state: SetupState) -> list[HumanDecisionOption]:
    match state:
        case SetupState.PATHS_REQUIRED:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Configurar caminhos",
                    description="Abre a rota oficial para definir Wiki e raw chats.",
                ),
                HumanDecisionOption(
                    id="cancel",
                    label="Parar",
                    description="Mantem o workflow bloqueado ate os caminhos serem definidos.",
                ),
            ]
        case SetupState.GITHUB_LOGIN_REQUIRED:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Entrar no GitHub",
                    description="Abre a rota oficial de login antes de tentar backup online.",
                ),
                HumanDecisionOption(
                    id="local_only",
                    label="Seguir local-only",
                    description="Mantem a protecao local pronta e deixa backup online pendente.",
                ),
            ]
        case SetupState.GITHUB_REMOTE_CONFIRMATION_REQUIRED:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Criar backup privado",
                    description="Cria o remote privado proposto e finaliza o setup online.",
                ),
                HumanDecisionOption(
                    id="local_only",
                    label="Seguir local-only",
                    description="Libera workflows locais e deixa backup online pendente.",
                ),
            ]
        case SetupState.GITHUB_REMOTE_AMBIGUOUS:
            return [
                HumanDecisionOption(
                    id="configure_remote",
                    label="Escolher remote",
                    description="Resolve qual remote GitHub deve ser usado pelo backup.",
                ),
                HumanDecisionOption(
                    id="local_only",
                    label="Seguir local-only",
                    description="Libera workflows locais e deixa backup online pendente.",
                ),
            ]
        case SetupState.BRANCH_CONFIRMATION_REQUIRED:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Ajustar branch",
                    description="Renomeia a branch atual para main pela rota oficial.",
                ),
                HumanDecisionOption(
                    id="cancel",
                    label="Parar",
                    description="Mantem a branch atual e deixa o setup bloqueado.",
                ),
            ]
        case SetupState.POLICY_DECISION_REQUIRED:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Corrigir ambiente",
                    description="Retoma o setup depois de mover para uma rota suportada.",
                ),
                HumanDecisionOption(
                    id="cancel",
                    label="Abortar",
                    description="Encerra o setup sem liberar workflows dependentes.",
                ),
            ]
        case _:
            return [
                HumanDecisionOption(
                    id="continue",
                    label="Continuar",
                    description="Retoma pela rota oficial depois da confirmacao.",
                )
            ]


def _rejected_automations(reason_code: str) -> list[RejectedAutomation]:
    return [
        RejectedAutomation(kind="auto_fix", reason_code=reason_code, reason="Requer confirmacao humana."),
        RejectedAutomation(
            kind="auto_defer",
            reason_code=reason_code,
            reason="Adiar deixaria o setup sem rota de retomada clara.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code=reason_code,
            reason="Planejar sem escolha humana nao resolve o bloqueio.",
        ),
    ]
