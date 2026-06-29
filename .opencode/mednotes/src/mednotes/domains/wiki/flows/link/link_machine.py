"""Operational StateChart for `/mednotes:link`.

This is the target FSM for the link package. It intentionally uses operational
leaf states for stale diagnosis, vocabulary bootstrap, agent work, external
quota and human apply confirmation so the public state does not collapse into
`status + blocked_reason` from the historical linker report.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator
from statemachine import StateChart
from statemachine.states import States

from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkWorkflowRunEffectPayload,
    LinkWorkflowRunKind,
    RelatedNotesExportEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
    WaitExternalEffectPayload,
)
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

LINK_WORKFLOW: Literal["/mednotes:link"] = "/mednotes:link"
LINK_BODY_WORKFLOW: Literal["/mednotes:link-body"] = "/mednotes:link-body"
LINK_PUBLIC_WORKFLOWS = frozenset({LINK_WORKFLOW, LINK_BODY_WORKFLOW})
LINK_BODY_FORBIDDEN_EVENT_NAMES = frozenset(
    {
        "vocabulary_bootstrap_required",
        "vocabulary_bootstrap_completed",
        "related_notes_planned",
        "related_notes_apply_requested",
        "vocabulary_semantic_repair_planned",
        "vocabulary_semantic_repair_apply_requested",
        "vocabulary_curator_required",
        "vocabulary_curator_completed",
        "related_notes_export_recovered",
        "related_notes_quota_wait",
        "related_notes_quota_ready",
        "related_notes_applied",
        "vocabulary_semantic_repair_applied",
    }
)


class LinkMode(StrEnum):
    FULL = "full"
    BODY_ONLY = "body_only"


class LinkState(StrEnum):
    CHECKING_TRIGGER_CONTEXT = "checking_trigger_context"
    DIAGNOSING_GRAPH = "diagnosing_graph"
    VOCABULARY_BOOTSTRAP_REQUIRED = "vocabulary_bootstrap_required"
    PLANNING_BODY_LINKS = "planning_body_links"
    PLANNING_RELATED_NOTES = "planning_related_notes"
    PLANNING_VOCABULARY_SEMANTIC_REPAIR = "planning_vocabulary_semantic_repair"
    WAITING_AGENT_DISAMBIGUATION = "waiting_agent_disambiguation"
    WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY = "waiting_agent_related_notes_export_recovery"
    WAITING_AGENT_VOCABULARY_CURATOR = "waiting_agent_vocabulary_curator"
    WAITING_EXTERNAL_RELATED_NOTES_QUOTA = "waiting_external_related_notes_quota"
    WAITING_HUMAN_CONFIRMATION = "waiting_human_confirmation"
    APPLYING_BODY_LINKS = "applying_body_links"
    APPLYING_RELATED_NOTES = "applying_related_notes"
    APPLYING_VOCABULARY_SEMANTIC_REPAIR = "applying_vocabulary_semantic_repair"
    STALE_DIAGNOSIS = "stale_diagnosis"
    APPLY_CANCELLED = "apply_cancelled"
    GRAPH_DIAGNOSIS_BLOCKED = "graph_diagnosis_blocked"
    BODY_LINKS_BLOCKED = "body_links_blocked"
    RELATED_NOTES_BLOCKED = "related_notes_blocked"
    VOCABULARY_SEMANTIC_REPAIR_BLOCKED = "vocabulary_semantic_repair_blocked"
    COMPLETED = "completed"
    COMPLETED_WITH_LINK_BLOCKERS = "completed_with_link_blockers"
    FAILED = "failed"


class LinkEvent(ContractModel):
    """Base event accepted by the link StateChart."""

    workflow: str = LINK_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_link(cls, value: str) -> str:
        if value not in LINK_PUBLIC_WORKFLOWS:
            raise ValueError(f"link event workflow must be one of {sorted(LINK_PUBLIC_WORKFLOWS)}")
        return value

    @model_validator(mode="after")
    def _body_only_rejects_full_package_events(self) -> LinkEvent:
        event_name = _event_name(self)
        if self.workflow == LINK_BODY_WORKFLOW and event_name in LINK_BODY_FORBIDDEN_EVENT_NAMES:
            raise ValueError(f"{event_name} is not valid for {LINK_BODY_WORKFLOW}")
        return self


def _event_name(event: LinkEvent) -> str:
    """Return the concrete Literal discriminator declared by each event class."""

    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("link events must declare a name discriminator")
    return name


class TriggerContextReadyEvent(LinkEvent):
    name: Literal["trigger_context_ready"] = "trigger_context_ready"
    trigger_context_path: str = Field(min_length=1)


class StaleDiagnosisEvent(LinkEvent):
    name: Literal["stale_diagnosis"] = "stale_diagnosis"
    reason_code: Literal["stale_diagnosis"]


class DiagnosisRefreshRequestedEvent(LinkEvent):
    name: Literal["diagnosis_refresh_requested"] = "diagnosis_refresh_requested"
    reason_code: Literal["stale_diagnosis"] = "stale_diagnosis"


class DiagnosisCleanEvent(LinkEvent):
    name: Literal["diagnosis_clean"] = "diagnosis_clean"
    changed_file_count: int = Field(default=0, ge=0, strict=True)


class DiagnosisCompletedWithLinkBlockersEvent(LinkEvent):
    name: Literal["diagnosis_completed_with_link_blockers"] = "diagnosis_completed_with_link_blockers"
    blocker_count: int = Field(ge=1, strict=True)


class LinkRuntimeObservation(ContractModel):
    """Adapter facts observed at the boundary; guards decide the leaf state."""

    mode: LinkMode = LinkMode.FULL
    operation: Literal["diagnose", "apply"]
    failed: bool = False
    stale_diagnosis: bool = False
    changed_file_count: int = Field(default=0, ge=0, strict=True)
    planned_link_count: int = Field(default=0, ge=0, strict=True)
    rewritten_link_count: int = Field(default=0, ge=0, strict=True)
    blocker_count: int = Field(default=0, ge=0, strict=True)
    body_linker_blocked: bool = False
    body_linker_blocked_reason: str = ""
    related_notes_present: bool = False
    related_notes_blocked: bool = False
    related_notes_export_recovery_required: bool = False
    related_notes_export_recovery_reason: str = ""
    related_notes_waiting_external: bool = False
    related_notes_applied: bool = False
    vocabulary_bootstrap_required: bool = False
    vocabulary_curator_required: bool = False
    vocabulary_db_path: str = ""
    vocabulary_curator_batch_plan_path: str = ""
    vocabulary_curator_work_item_count: int = Field(default=1, ge=1, strict=True)
    next_action: str = ""
    reason_code: str = ""
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )


class LinkRuntimeObservedEvent(LinkEvent):
    """Single runtime observation event; LinkMachine owns outcome priority."""

    name: Literal["runtime_observed"] = "runtime_observed"
    observation: LinkRuntimeObservation


class VocabularyBootstrapRequiredEvent(LinkEvent):
    name: Literal["vocabulary_bootstrap_required"] = "vocabulary_bootstrap_required"
    db_path: str = Field(min_length=1)


class VocabularyBootstrapCompletedEvent(LinkEvent):
    name: Literal["vocabulary_bootstrap_completed"] = "vocabulary_bootstrap_completed"
    db_path: str = Field(min_length=1)


class BodyLinksPlannedEvent(LinkEvent):
    name: Literal["body_links_planned"] = "body_links_planned"
    planned_link_count: int = Field(ge=0, strict=True)


class BodyLinksReadyForConfirmationEvent(LinkEvent):
    name: Literal["body_links_ready_for_confirmation"] = "body_links_ready_for_confirmation"
    planned_link_count: int = Field(ge=0, strict=True)


class RelatedNotesPlannedEvent(LinkEvent):
    name: Literal["related_notes_planned"] = "related_notes_planned"
    planned_note_count: int = Field(ge=0, strict=True)


class RelatedNotesApplyRequestedEvent(LinkEvent):
    name: Literal["related_notes_apply_requested"] = "related_notes_apply_requested"
    planned_note_count: int = Field(ge=0, strict=True)


class VocabularySemanticRepairPlannedEvent(LinkEvent):
    name: Literal["vocabulary_semantic_repair_planned"] = "vocabulary_semantic_repair_planned"
    planned_repair_count: int = Field(ge=0, strict=True)


class VocabularySemanticRepairApplyRequestedEvent(LinkEvent):
    name: Literal["vocabulary_semantic_repair_apply_requested"] = "vocabulary_semantic_repair_apply_requested"
    planned_repair_count: int = Field(ge=0, strict=True)


class AgentDisambiguationRequiredEvent(LinkEvent):
    name: Literal["agent_disambiguation_required"] = "agent_disambiguation_required"
    ambiguous_alias_count: int = Field(ge=1, strict=True)


class AgentDisambiguationCompletedEvent(LinkEvent):
    name: Literal["agent_disambiguation_completed"] = "agent_disambiguation_completed"
    summary: str = Field(min_length=1)


class VocabularyCuratorRequiredEvent(LinkEvent):
    name: Literal["vocabulary_curator_required"] = "vocabulary_curator_required"
    work_item_count: int = Field(ge=1, strict=True)
    batch_plan_path: str = ""


class VocabularyCuratorCompletedEvent(LinkEvent):
    name: Literal["vocabulary_curator_completed"] = "vocabulary_curator_completed"
    summary: str = Field(min_length=1)


class RelatedNotesQuotaWaitEvent(LinkEvent):
    name: Literal["related_notes_quota_wait"] = "related_notes_quota_wait"
    resume_action: str = Field(min_length=1)
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_related_notes_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload:
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class RelatedNotesExportRecoveredEvent(LinkEvent):
    name: Literal["related_notes_export_recovered"] = "related_notes_export_recovered"
    summary: str = Field(min_length=1)


class RelatedNotesQuotaReadyEvent(LinkEvent):
    name: Literal["related_notes_quota_ready"] = "related_notes_quota_ready"
    restored_by: str = Field(min_length=1)


class HumanApplyApprovedEvent(LinkEvent):
    name: Literal["human_apply_approved"] = "human_apply_approved"
    approved_by: str = Field(min_length=1)


class HumanApplyCancelledEvent(LinkEvent):
    name: Literal["human_apply_cancelled"] = "human_apply_cancelled"
    cancelled_by: str = Field(min_length=1)


class BodyLinksAppliedEvent(LinkEvent):
    name: Literal["body_links_applied"] = "body_links_applied"
    changed_file_count: int = Field(ge=0, strict=True)


class RelatedNotesAppliedEvent(LinkEvent):
    name: Literal["related_notes_applied"] = "related_notes_applied"
    changed_file_count: int = Field(ge=0, strict=True)


class VocabularySemanticRepairAppliedEvent(LinkEvent):
    name: Literal["vocabulary_semantic_repair_applied"] = "vocabulary_semantic_repair_applied"
    changed_file_count: int = Field(ge=0, strict=True)


class LinkBlockedEvent(LinkEvent):
    name: Literal["link_blocked"] = "link_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class LinkFailedEvent(LinkEvent):
    name: Literal["link_failed"] = "link_failed"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


LinkBoundaryEvent = Annotated[
    TriggerContextReadyEvent
    | StaleDiagnosisEvent
    | DiagnosisRefreshRequestedEvent
    | DiagnosisCleanEvent
    | DiagnosisCompletedWithLinkBlockersEvent
    | LinkRuntimeObservedEvent
    | VocabularyBootstrapRequiredEvent
    | VocabularyBootstrapCompletedEvent
    | BodyLinksPlannedEvent
    | BodyLinksReadyForConfirmationEvent
    | RelatedNotesPlannedEvent
    | RelatedNotesApplyRequestedEvent
    | VocabularySemanticRepairPlannedEvent
    | VocabularySemanticRepairApplyRequestedEvent
    | AgentDisambiguationRequiredEvent
    | AgentDisambiguationCompletedEvent
    | VocabularyCuratorRequiredEvent
    | VocabularyCuratorCompletedEvent
    | RelatedNotesExportRecoveredEvent
    | RelatedNotesQuotaWaitEvent
    | RelatedNotesQuotaReadyEvent
    | HumanApplyApprovedEvent
    | HumanApplyCancelledEvent
    | BodyLinksAppliedEvent
    | RelatedNotesAppliedEvent
    | VocabularySemanticRepairAppliedEvent
    | LinkBlockedEvent
    | LinkFailedEvent,
    Field(discriminator="name"),
]
LinkBoundaryEventAdapter = TypeAdapter(LinkBoundaryEvent)


class LinkMachine(StateChart[WorkflowModel]):
    """Pure domain StateChart for link state and effect intents."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        LinkState,
        initial=LinkState.CHECKING_TRIGGER_CONTEXT,
        final={
            LinkState.COMPLETED,
            LinkState.COMPLETED_WITH_LINK_BLOCKERS,
            LinkState.APPLY_CANCELLED,
            LinkState.GRAPH_DIAGNOSIS_BLOCKED,
            LinkState.BODY_LINKS_BLOCKED,
            LinkState.RELATED_NOTES_BLOCKED,
            LinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED,
            LinkState.FAILED,
        },
        use_enum_instance=False,
    )

    trigger_context_ready = states.CHECKING_TRIGGER_CONTEXT.to(states.DIAGNOSING_GRAPH, on="_on_diagnose")
    stale_diagnosis = (
        states.CHECKING_TRIGGER_CONTEXT.to(states.STALE_DIAGNOSIS, on="_on_blocked")
        | states.DIAGNOSING_GRAPH.to(states.STALE_DIAGNOSIS, on="_on_blocked")
    )
    diagnosis_refresh_requested = states.STALE_DIAGNOSIS.to(states.DIAGNOSING_GRAPH, on="_on_diagnose")
    diagnosis_clean = states.DIAGNOSING_GRAPH.to(states.COMPLETED, on="_on_completed")
    diagnosis_completed_with_link_blockers = states.DIAGNOSING_GRAPH.to(
        states.COMPLETED_WITH_LINK_BLOCKERS,
        on="_on_completed",
    )
    runtime_observed = (
        states.DIAGNOSING_GRAPH.to(states.FAILED, cond="_observed_failed", on="_on_runtime_failed")
        | states.DIAGNOSING_GRAPH.to(
            states.STALE_DIAGNOSIS,
            cond="_observed_stale_diagnosis",
            on="_on_runtime_blocked",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.WAITING_EXTERNAL_RELATED_NOTES_QUOTA,
            cond="_observed_related_notes_quota",
            on="_on_runtime_wait_external",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.VOCABULARY_BOOTSTRAP_REQUIRED,
            cond="_observed_vocabulary_bootstrap_required",
            on="_on_runtime_agent_required",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.WAITING_AGENT_VOCABULARY_CURATOR,
            cond="_observed_vocabulary_curator_required",
            on="_on_runtime_agent_required",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY,
            cond="_observed_related_notes_export_recovery_required",
            on="_on_runtime_agent_required",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.RELATED_NOTES_BLOCKED,
            cond="_observed_related_notes_blocked",
            on="_on_runtime_blocked",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.BODY_LINKS_BLOCKED,
            cond="_observed_body_linker_blocked",
            on="_on_runtime_blocked",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.COMPLETED_WITH_LINK_BLOCKERS,
            cond="_observed_completed_with_warnings",
            on="_on_completed",
        )
        | states.DIAGNOSING_GRAPH.to(
            states.COMPLETED,
            cond="_observed_completed",
            on="_on_completed",
        )
    )

    vocabulary_bootstrap_required = states.DIAGNOSING_GRAPH.to(
        states.VOCABULARY_BOOTSTRAP_REQUIRED,
        on="_on_agent_required",
    )
    vocabulary_bootstrap_completed = states.VOCABULARY_BOOTSTRAP_REQUIRED.to(
        states.DIAGNOSING_GRAPH,
        on="_on_transition",
    )
    body_links_planned = states.DIAGNOSING_GRAPH.to(states.PLANNING_BODY_LINKS, on="_on_transition")
    body_links_ready_for_confirmation = states.PLANNING_BODY_LINKS.to(
        states.WAITING_HUMAN_CONFIRMATION,
        on="_on_human_confirmation",
    )
    related_notes_planned = states.DIAGNOSING_GRAPH.to(states.PLANNING_RELATED_NOTES, on="_on_transition")
    related_notes_apply_requested = states.PLANNING_RELATED_NOTES.to(
        states.APPLYING_RELATED_NOTES,
        on="_on_apply_related_notes",
    )
    vocabulary_semantic_repair_planned = states.DIAGNOSING_GRAPH.to(
        states.PLANNING_VOCABULARY_SEMANTIC_REPAIR,
        on="_on_transition",
    )
    vocabulary_semantic_repair_apply_requested = states.PLANNING_VOCABULARY_SEMANTIC_REPAIR.to(
        states.APPLYING_VOCABULARY_SEMANTIC_REPAIR,
        on="_on_apply_vocabulary_semantic_repair",
    )
    agent_disambiguation_required = states.DIAGNOSING_GRAPH.to(
        states.WAITING_AGENT_DISAMBIGUATION,
        on="_on_agent_required",
    )
    agent_disambiguation_completed = states.WAITING_AGENT_DISAMBIGUATION.to(
        states.DIAGNOSING_GRAPH,
        on="_on_transition",
    )
    vocabulary_curator_required = states.DIAGNOSING_GRAPH.to(
        states.WAITING_AGENT_VOCABULARY_CURATOR,
        on="_on_agent_required",
    )
    vocabulary_curator_completed = states.WAITING_AGENT_VOCABULARY_CURATOR.to(
        states.DIAGNOSING_GRAPH,
        on="_on_transition",
    )
    related_notes_export_recovered = states.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY.to(
        states.DIAGNOSING_GRAPH,
        on="_on_transition",
    )

    related_notes_quota_wait = (
        states.DIAGNOSING_GRAPH.to(states.WAITING_EXTERNAL_RELATED_NOTES_QUOTA, on="_on_wait_external")
        | states.PLANNING_RELATED_NOTES.to(states.WAITING_EXTERNAL_RELATED_NOTES_QUOTA, on="_on_wait_external")
    )
    related_notes_quota_ready = states.WAITING_EXTERNAL_RELATED_NOTES_QUOTA.to(
        states.DIAGNOSING_GRAPH,
        on="_on_transition",
    )

    human_apply_approved = states.WAITING_HUMAN_CONFIRMATION.to(states.APPLYING_BODY_LINKS, on="_on_apply_body_links")
    human_apply_cancelled = states.WAITING_HUMAN_CONFIRMATION.to(states.APPLY_CANCELLED, on="_on_blocked")
    body_links_applied = states.APPLYING_BODY_LINKS.to(states.COMPLETED, on="_on_completed")
    related_notes_applied = states.APPLYING_RELATED_NOTES.to(states.COMPLETED, on="_on_completed")
    vocabulary_semantic_repair_applied = states.APPLYING_VOCABULARY_SEMANTIC_REPAIR.to(
        states.COMPLETED,
        on="_on_completed",
    )
    link_blocked = (
        states.DIAGNOSING_GRAPH.to(states.GRAPH_DIAGNOSIS_BLOCKED, on="_on_blocked")
        | states.PLANNING_BODY_LINKS.to(states.BODY_LINKS_BLOCKED, on="_on_blocked")
        | states.APPLYING_BODY_LINKS.to(states.BODY_LINKS_BLOCKED, on="_on_blocked")
        | states.PLANNING_RELATED_NOTES.to(states.RELATED_NOTES_BLOCKED, on="_on_blocked")
        | states.APPLYING_RELATED_NOTES.to(states.RELATED_NOTES_BLOCKED, on="_on_blocked")
        | states.PLANNING_VOCABULARY_SEMANTIC_REPAIR.to(
            states.VOCABULARY_SEMANTIC_REPAIR_BLOCKED,
            on="_on_blocked",
        )
        | states.APPLYING_VOCABULARY_SEMANTIC_REPAIR.to(
            states.VOCABULARY_SEMANTIC_REPAIR_BLOCKED,
            on="_on_blocked",
        )
    )
    link_failed = (
        states.DIAGNOSING_GRAPH.to(states.FAILED, on="_on_failed")
        | states.APPLYING_BODY_LINKS.to(states.FAILED, on="_on_failed")
        | states.APPLYING_RELATED_NOTES.to(states.FAILED, on="_on_failed")
        | states.APPLYING_VOCABULARY_SEMANTIC_REPAIR.to(states.FAILED, on="_on_failed")
    )

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return category_for_link_state(LinkState(state))

    def _on_transition(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_diagnose(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(workflow_event, to_state, effects=[_link_effect(workflow_event, to_state)])

    def _on_agent_required(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=str(getattr(workflow_event, "reason_code", _event_name(workflow_event))),
            effects=[_agent_required_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_state(to_state),
        )

    def _on_wait_external(self, workflow_event: RelatedNotesQuotaWaitEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code="related_notes_quota_wait",
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action=workflow_event.resume_action,
        )

    def _on_human_confirmation(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _human_transition(workflow_event, to_state, reason_code="link_apply_confirmation_required")

    def _on_apply_body_links(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_link_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_state(to_state),
        )

    def _on_apply_related_notes(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_link_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_state(to_state),
        )

    def _on_apply_vocabulary_semantic_repair(
        self,
        workflow_event: LinkEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_link_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_state(to_state),
        )

    def _on_blocked(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", to_state.value))
        next_action = str(getattr(workflow_event, "next_action", "")) or _resume_action_for_link_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_failed(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", to_state.value))
        next_action = str(getattr(workflow_event, "next_action", "")) or "Retomar pelo diagnostico oficial de links."
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="failed", phase=to_state.value, reason_code=reason_code, next_action=next_action),
        )

    def _on_completed(self, workflow_event: LinkEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_runtime_agent_required(
        self,
        workflow_event: LinkRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=_runtime_reason_code(workflow_event, to_state),
            effects=[_agent_required_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_state(to_state),
        )

    def _on_runtime_wait_external(
        self,
        workflow_event: LinkRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        next_action = workflow_event.observation.next_action or _resume_action_for_link_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code="related_notes_quota_wait",
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action=next_action,
        )

    def _on_runtime_blocked(
        self,
        workflow_event: LinkRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, to_state)
        next_action = workflow_event.observation.next_action or _resume_action_for_link_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_runtime_failed(
        self,
        workflow_event: LinkRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, to_state)
        next_action = workflow_event.observation.next_action or "Retomar pelo diagnostico oficial de links."
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="failed", phase=to_state.value, reason_code=reason_code, next_action=next_action),
        )

    def _observed_failed(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.failed or (
            observation.mode == LinkMode.BODY_ONLY
            and (
                observation.related_notes_waiting_external
                or observation.related_notes_blocked
                or observation.related_notes_export_recovery_required
                or observation.related_notes_applied
            )
        )

    def _observed_stale_diagnosis(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        return workflow_event.observation.stale_diagnosis

    def _observed_related_notes_quota(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.mode == LinkMode.FULL and observation.related_notes_waiting_external

    def _observed_related_notes_export_recovery_required(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.mode == LinkMode.FULL and observation.related_notes_export_recovery_required

    def _observed_vocabulary_bootstrap_required(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        return workflow_event.observation.vocabulary_bootstrap_required

    def _observed_vocabulary_curator_required(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        return workflow_event.observation.vocabulary_curator_required

    def _observed_related_notes_blocked(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.mode == LinkMode.FULL and observation.related_notes_blocked

    def _observed_body_linker_blocked(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.body_linker_blocked or observation.blocker_count > 0

    def _observed_completed_with_warnings(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.blocker_count > 0 and not observation.rewritten_link_count

    def _observed_completed(self, workflow_event: LinkRuntimeObservedEvent) -> bool:
        return not self._observed_failed(workflow_event)


def category_for_link_state(state: LinkState) -> WorkflowStateCategory:
    """Map each link leaf state to the public workflow category."""

    match state:
        case (
            LinkState.CHECKING_TRIGGER_CONTEXT
            | LinkState.DIAGNOSING_GRAPH
            | LinkState.PLANNING_BODY_LINKS
            | LinkState.PLANNING_RELATED_NOTES
            | LinkState.PLANNING_VOCABULARY_SEMANTIC_REPAIR
            | LinkState.APPLYING_BODY_LINKS
            | LinkState.APPLYING_RELATED_NOTES
            | LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR
        ):
            return WorkflowStateCategory.RUNNING
        case (
            LinkState.VOCABULARY_BOOTSTRAP_REQUIRED
            | LinkState.WAITING_AGENT_DISAMBIGUATION
            | LinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY
            | LinkState.WAITING_AGENT_VOCABULARY_CURATOR
        ):
            return WorkflowStateCategory.WAITING_AGENT
        case LinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
            return WorkflowStateCategory.WAITING_EXTERNAL
        case LinkState.WAITING_HUMAN_CONFIRMATION:
            return WorkflowStateCategory.WAITING_HUMAN
        case (
            LinkState.STALE_DIAGNOSIS
            | LinkState.APPLY_CANCELLED
            | LinkState.GRAPH_DIAGNOSIS_BLOCKED
            | LinkState.BODY_LINKS_BLOCKED
            | LinkState.RELATED_NOTES_BLOCKED
            | LinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED
        ):
            return WorkflowStateCategory.BLOCKED
        case LinkState.FAILED:
            return WorkflowStateCategory.FAILED
        case LinkState.COMPLETED:
            return WorkflowStateCategory.COMPLETED
        case LinkState.COMPLETED_WITH_LINK_BLOCKERS:
            return WorkflowStateCategory.COMPLETED_WITH_WARNINGS


def _transition(
    workflow_event: LinkEvent,
    to_state: LinkState,
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


def _target_state(target: object) -> LinkState:
    """Read the python-statemachine transition target without touching IO."""

    value = getattr(target, "value", target)
    return LinkState(str(value))


def _agent_required_effect(workflow_event: LinkEvent, origin_state: LinkState) -> WorkflowEffect:
    """Emit the effect selected by the LinkMachine waiting-agent state."""

    if origin_state == LinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
        return _related_notes_export_recovery_effect(workflow_event, origin_state)
    return _link_effect(workflow_event, origin_state)


def _related_notes_export_recovery_effect(workflow_event: LinkEvent, origin_state: LinkState) -> WorkflowEffect:
    reason_code = str(getattr(workflow_event, "reason_code", "")) or origin_state.value
    if isinstance(workflow_event, LinkRuntimeObservedEvent):
        reason_code = workflow_event.observation.related_notes_export_recovery_reason or _runtime_reason_code(
            workflow_event,
            origin_state,
        )
    payload = RelatedNotesExportEffectPayload(mode="auto", reason_code=reason_code).to_payload()
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="link-related-notes-export-recovery",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="related_notes.export",
        payload=payload,
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=_resume_action_for_link_state(origin_state),
    )


def _link_effect(workflow_event: LinkEvent, origin_state: LinkState) -> WorkflowEffect:
    target, payload_kind = _effect_contract_for_state(origin_state)
    if workflow_event.workflow == LINK_BODY_WORKFLOW:
        if origin_state not in {LinkState.DIAGNOSING_GRAPH, LinkState.APPLYING_BODY_LINKS}:
            raise ValueError(f"{origin_state.value} is not executable for {LINK_BODY_WORKFLOW}")
        target = LINK_BODY_WORKFLOW
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"link-{origin_state.value.replace('_', '-')}",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=target,
        payload=_link_effect_payload(workflow_event, payload_kind=payload_kind),
        mutates_resources=origin_state
        in {
            LinkState.APPLYING_BODY_LINKS,
            LinkState.APPLYING_RELATED_NOTES,
            LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR,
        },
        rollback_declared=origin_state
        in {
            LinkState.APPLYING_BODY_LINKS,
            LinkState.APPLYING_RELATED_NOTES,
            LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR,
        },
        requires_receipt=False,
        no_resource_mutation=origin_state
        not in {
            LinkState.APPLYING_BODY_LINKS,
            LinkState.APPLYING_RELATED_NOTES,
            LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR,
        },
    )


def _link_effect_payload(workflow_event: LinkEvent, *, payload_kind: LinkWorkflowRunKind) -> JsonObject:
    """Project typed event fields that the adapter/agent needs to execute the effect."""

    apply_requested = payload_kind.startswith("apply_")
    payload = LinkWorkflowRunEffectPayload(
        kind=payload_kind,
        diagnose=not apply_requested,
        apply=apply_requested,
        diagnosis_path=str(getattr(workflow_event, "diagnosis_path", "")),
        receipt_path=str(getattr(workflow_event, "receipt_path", "")),
        no_related_notes=workflow_event.workflow == LINK_BODY_WORKFLOW,
    ).to_payload()
    if isinstance(workflow_event, VocabularyBootstrapRequiredEvent):
        payload["db_path"] = workflow_event.db_path
    if isinstance(workflow_event, VocabularyCuratorRequiredEvent):
        payload["work_item_count"] = workflow_event.work_item_count
        if workflow_event.batch_plan_path:
            payload["batch_plan_path"] = workflow_event.batch_plan_path
    if isinstance(workflow_event, LinkRuntimeObservedEvent):
        observation = workflow_event.observation
        if payload_kind == "vocabulary_bootstrap":
            if not observation.vocabulary_db_path.strip():
                raise ValueError("vocabulary_bootstrap effect requires vocabulary_db_path")
            payload["db_path"] = observation.vocabulary_db_path
        if payload_kind == "vocabulary_curator":
            payload["work_item_count"] = observation.vocabulary_curator_work_item_count
            if observation.vocabulary_curator_batch_plan_path:
                payload["batch_plan_path"] = observation.vocabulary_curator_batch_plan_path
    return payload


def _wait_external_effect(workflow_event: RelatedNotesQuotaWaitEvent | LinkRuntimeObservedEvent, origin_state: LinkState) -> WorkflowEffect:
    recovery_state = (
        workflow_event.observation.related_notes_recovery_state
        if isinstance(workflow_event, LinkRuntimeObservedEvent)
        else workflow_event.related_notes_recovery_state
    )
    resume_action = (
        workflow_event.observation.next_action
        if isinstance(workflow_event, LinkRuntimeObservedEvent)
        else workflow_event.resume_action
    )
    if not recovery_state.status:
        recovery_state = RelatedNotesRecoveryStateEffectPayload.from_payload(
            {
                "schema": "medical-notes-workbench.related-notes-recovery-state.v1",
                "status": "waiting_for_retry",
                "blocked_reason": "related_notes_headless_quota_exhausted",
                "next_action": resume_action,
                "resume_supported": True,
            }
        )
    payload = WaitExternalEffectPayload.model_validate(
        {
            "related_notes_recovery_state": recovery_state,
            "next_action": resume_action,
        }
    ).to_payload()
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="link-related-notes-quota-wait",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.WAIT_EXTERNAL,
        target="related_notes.quota",
        payload=payload,
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=resume_action,
    )


def _human_transition(
    workflow_event: LinkEvent,
    to_state: LinkState,
    *,
    reason_code: str,
) -> WorkflowTransitionResult:
    decision = _decision(
        kind="ask_human",
        phase=to_state.value,
        reason_code=reason_code,
        next_action="link:confirm-apply",
    )
    packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
    effect = WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="link-human-apply-confirmation",
        origin_state=to_state.value,
        kind=WorkflowEffectKind.ASK_HUMAN,
        target="human.link_apply_confirmation",
        payload={"kind": "human_decision", "reason_code": reason_code},
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


def _effect_contract_for_state(state: LinkState) -> tuple[str, LinkWorkflowRunKind]:
    match state:
        case LinkState.DIAGNOSING_GRAPH:
            return LINK_WORKFLOW, "diagnose"
        case LinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
            return LINK_WORKFLOW, "vocabulary_bootstrap"
        case LinkState.WAITING_AGENT_DISAMBIGUATION:
            return LINK_WORKFLOW, "agent_disambiguation"
        case LinkState.WAITING_AGENT_VOCABULARY_CURATOR:
            return LINK_WORKFLOW, "vocabulary_curator"
        case LinkState.APPLYING_BODY_LINKS:
            return LINK_WORKFLOW, "apply_body_links"
        case LinkState.APPLYING_RELATED_NOTES:
            return LINK_WORKFLOW, "apply_related_notes"
        case LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR:
            return LINK_WORKFLOW, "apply_vocabulary_semantic_repair"
        case _:
            raise AssertionError(f"state does not emit link effect: {state.value}")


def _runtime_reason_code(workflow_event: LinkRuntimeObservedEvent, state: LinkState) -> str:
    observation = workflow_event.observation
    if observation.mode == LinkMode.BODY_ONLY and (
        observation.related_notes_waiting_external
        or observation.related_notes_blocked
        or observation.related_notes_applied
    ):
        return "link_body_mode_contract_violation"
    if observation.reason_code.strip():
        return observation.reason_code.strip()
    if state == LinkState.STALE_DIAGNOSIS:
        return "stale_diagnosis"
    if state == LinkState.WAITING_EXTERNAL_RELATED_NOTES_QUOTA:
        return "related_notes_quota_wait"
    if state == LinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
        return observation.related_notes_export_recovery_reason or "related_notes_export_recovery_required"
    if state == LinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
        return "vocabulary_bootstrap_required"
    if state == LinkState.WAITING_AGENT_VOCABULARY_CURATOR:
        return "vocabulary_semantic_ingestion_pending"
    if state == LinkState.RELATED_NOTES_BLOCKED:
        return "related_notes_blocked"
    if state == LinkState.BODY_LINKS_BLOCKED:
        return observation.body_linker_blocked_reason or "body_linker_blocked"
    if state == LinkState.FAILED:
        return "link_failed"
    return state.value


def _resume_action_for_link_state(state: LinkState) -> str:
    match state:
        case LinkState.STALE_DIAGNOSIS:
            return "link:diagnose"
        case LinkState.GRAPH_DIAGNOSIS_BLOCKED:
            return "link:diagnose"
        case LinkState.BODY_LINKS_BLOCKED:
            return "link:repair-body-links"
        case LinkState.RELATED_NOTES_BLOCKED:
            return "link:repair-related-notes"
        case LinkState.WAITING_AGENT_RELATED_NOTES_EXPORT_RECOVERY:
            return "link:recover-related-notes-export"
        case LinkState.VOCABULARY_SEMANTIC_REPAIR_BLOCKED:
            return "link:repair-vocabulary-semantics"
        case LinkState.VOCABULARY_BOOTSTRAP_REQUIRED:
            return "link:bootstrap-vocabulary"
        case LinkState.WAITING_AGENT_DISAMBIGUATION:
            return "link:run-agent-disambiguation"
        case LinkState.WAITING_AGENT_VOCABULARY_CURATOR:
            return "link:run-vocabulary-curator"
        case LinkState.WAITING_HUMAN_CONFIRMATION:
            return "link:confirm-apply"
        case LinkState.APPLYING_BODY_LINKS:
            return "link:apply-body-links"
        case LinkState.APPLYING_RELATED_NOTES:
            return "link:apply-related-notes"
        case LinkState.APPLYING_VOCABULARY_SEMANTIC_REPAIR:
            return "link:apply-vocabulary-semantic-repair"
        case _:
            return "link:diagnose"


def _decision(
    *,
    kind: Literal["hard_block", "failed", "ask_human"],
    phase: str,
    reason_code: str,
    next_action: str,
) -> WorkflowDecision:
    evidence = [
        DecisionEvidence(
            summary=f"link StateChart reached {phase}.",
            technical_code=reason_code,
            source="link_machine",
        )
    ]
    base: JsonObject = {
        "kind": kind,
        "phase": phase,
        "reason_code": reason_code,
        "public_summary": "O workflow de links precisa parar nesta etapa.",
        "developer_summary": f"StateChart transition stopped at {phase}:{reason_code}.",
        "evidence": evidence,
        "next_action": next_action,
        "required_inputs": _required_inputs_for_reason(reason_code),
        "resume_action": next_action,
    }
    if kind == "ask_human":
        base.update(
            {
                "public_summary": "Preciso da sua confirmacao antes de aplicar mudancas de links.",
                "human_decision_kind": reason_code,
                "recommended_option_id": "apply",
                "options": [
                    HumanDecisionOption(
                        id="apply",
                        label="Aplicar",
                        description="Aplica o plano de links com protecao do vault.",
                    ),
                    HumanDecisionOption(
                        id="cancel",
                        label="Cancelar",
                        description="Mantem a Wiki sem alteracoes deste plano.",
                    ),
                ],
                "rejected_automations": _rejected_automations(reason_code),
            }
        )
    return WorkflowDecision(**base)


def _required_inputs_for_reason(reason_code: str) -> list[str]:
    """Return missing operator inputs owned by LinkMachine blocker decisions."""

    match reason_code:
        case "linker_mode_required":
            return ["diagnose_or_apply"]
        case "trigger_context_apply_not_allowed":
            return ["diagnosis"]
        case _:
            return []


def _rejected_automations(reason_code: str) -> list[RejectedAutomation]:
    return [
        RejectedAutomation(kind="auto_fix", reason_code=reason_code, reason="Aplicar links muta a Wiki."),
        RejectedAutomation(
            kind="auto_defer",
            reason_code=reason_code,
            reason="Adiar sem decisao deixa o plano de links pendente.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code=reason_code,
            reason="Planejar novamente nao substitui a confirmacao de apply.",
        ),
    ]
