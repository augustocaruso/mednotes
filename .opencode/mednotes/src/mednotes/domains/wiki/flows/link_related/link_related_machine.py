"""Operational StateChart for `/mednotes:link-related`.

The machine owns the real link-related workflow states: export availability,
stale export recovery, external quota waits, human apply confirmation, and the
vault-mutating section sync. Public JSON is a projection of this model, not a
second place to classify `status + blocked_reason`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator
from statemachine import StateChart
from statemachine.states import States

from mednotes.domains.wiki.contracts.effect_payloads import (
    RelatedNotesExportEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
    RelatedNotesSyncSectionEffectPayload,
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

LINK_RELATED_WORKFLOW: Literal["/mednotes:link-related"] = "/mednotes:link-related"


class LinkRelatedState(StrEnum):
    CHECKING_EXPORT = "checking_export"
    EXPORT_REQUIRED = "export_required"
    PREVIEW_READY = "preview_ready"
    WAITING_HUMAN_CONFIRMATION = "waiting_human_confirmation"
    APPLYING_RELATED_NOTES = "applying_related_notes"
    WAITING_EXTERNAL_QUOTA = "waiting_for_external_quota"
    STALE_EXPORT = "stale_export"
    APPLY_CANCELLED = "apply_cancelled"
    RELATED_NOTES_EXPORT_BLOCKED = "related_notes_export_blocked"
    RELATED_NOTES_PREVIEW_BLOCKED = "related_notes_preview_blocked"
    RELATED_NOTES_APPLY_BLOCKED = "related_notes_apply_blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class LinkRelatedEvent(ContractModel):
    """Base event accepted by the link-related StateChart."""

    workflow: str = LINK_RELATED_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_link_related(cls, value: str) -> str:
        if value != LINK_RELATED_WORKFLOW:
            raise ValueError(f"link-related event workflow must be {LINK_RELATED_WORKFLOW}")
        return value


def _event_name(event: LinkRelatedEvent) -> str:
    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("link-related events must declare a name discriminator")
    return name


class ExportReadyEvent(LinkRelatedEvent):
    name: Literal["export_ready"] = "export_ready"
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    proposed_link_count: int = Field(default=0, ge=0, strict=True)
    cleared_link_count: int = Field(default=0, ge=0, strict=True)


class ExportMissingEvent(LinkRelatedEvent):
    name: Literal["export_missing"] = "export_missing"
    reason_code: str = "related_notes_export_missing"
    export_path: str = ""


class ExportStaleEvent(LinkRelatedEvent):
    name: Literal["export_stale"] = "export_stale"
    reason_code: str = "related_notes_export_stale"
    stale_record_count: int = Field(default=0, ge=0, strict=True)


class LinkRelatedRuntimeObservation(ContractModel):
    """Typed adapter facts; LinkRelatedMachine decides the public leaf state."""

    mode: Literal["dry_run", "apply", "recover_export"]
    failed: bool = False
    export_missing: bool = False
    export_stale: bool = False
    preview_ready: bool = False
    applied: bool = False
    blocked: bool = False
    waiting_external: bool = False
    planned_note_count: int = Field(default=0, ge=0, strict=True)
    proposed_link_count: int = Field(default=0, ge=0, strict=True)
    cleared_link_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    fresh_record_count: int = Field(default=0, ge=0, strict=True)
    stale_record_count: int = Field(default=0, ge=0, strict=True)
    remaining_count: int = Field(default=0, ge=0, strict=True)
    reason_code: str = ""
    next_action: str = ""
    export_path: str = ""
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_related_notes_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload:
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class LinkRelatedRuntimeObservedEvent(LinkRelatedEvent):
    """Single runtime observation event; guards own outcome priority."""

    name: Literal["runtime_observed"] = "runtime_observed"
    observation: LinkRelatedRuntimeObservation


class PreviewRequiresConfirmationEvent(LinkRelatedEvent):
    name: Literal["preview_requires_confirmation"] = "preview_requires_confirmation"
    planned_note_count: int = Field(ge=1, strict=True)


class HumanApplyApprovedEvent(LinkRelatedEvent):
    name: Literal["human_apply_approved"] = "human_apply_approved"
    approved_by: str = Field(min_length=1)


class HumanApplyCancelledEvent(LinkRelatedEvent):
    name: Literal["human_apply_cancelled"] = "human_apply_cancelled"
    cancelled_by: str = Field(min_length=1)


class RelatedNotesAppliedEvent(LinkRelatedEvent):
    name: Literal["related_notes_applied"] = "related_notes_applied"
    changed_file_count: int = Field(ge=0, strict=True)


class RelatedNotesQuotaWaitEvent(LinkRelatedEvent):
    name: Literal["related_notes_quota_wait"] = "related_notes_quota_wait"
    resume_action: str = Field(min_length=1)
    fresh_record_count: int = Field(default=0, ge=0, strict=True)
    stale_record_count: int = Field(default=0, ge=0, strict=True)
    remaining_count: int = Field(default=0, ge=0, strict=True)
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_related_notes_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload:
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class RelatedNotesQuotaReadyEvent(LinkRelatedEvent):
    name: Literal["related_notes_quota_ready"] = "related_notes_quota_ready"
    restored_by: str = Field(min_length=1)


class LinkRelatedBlockedEvent(LinkRelatedEvent):
    name: Literal["link_related_blocked"] = "link_related_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class LinkRelatedFailedEvent(LinkRelatedEvent):
    name: Literal["link_related_failed"] = "link_related_failed"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


LinkRelatedBoundaryEvent = Annotated[
    ExportReadyEvent
    | ExportMissingEvent
    | ExportStaleEvent
    | LinkRelatedRuntimeObservedEvent
    | PreviewRequiresConfirmationEvent
    | HumanApplyApprovedEvent
    | HumanApplyCancelledEvent
    | RelatedNotesAppliedEvent
    | RelatedNotesQuotaWaitEvent
    | RelatedNotesQuotaReadyEvent
    | LinkRelatedBlockedEvent
    | LinkRelatedFailedEvent,
    Field(discriminator="name"),
]
LinkRelatedBoundaryEventAdapter = TypeAdapter(LinkRelatedBoundaryEvent)


class LinkRelatedMachine(StateChart[WorkflowModel]):
    """Pure domain StateChart for Related Notes sync and recovery."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        LinkRelatedState,
        initial=LinkRelatedState.CHECKING_EXPORT,
        final={
            LinkRelatedState.COMPLETED,
            LinkRelatedState.APPLY_CANCELLED,
            LinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED,
            LinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED,
            LinkRelatedState.RELATED_NOTES_APPLY_BLOCKED,
            LinkRelatedState.FAILED,
        },
        use_enum_instance=False,
    )

    export_ready = (
        states.CHECKING_EXPORT.to(states.PREVIEW_READY, on="_on_preview_ready")
        | states.EXPORT_REQUIRED.to(states.PREVIEW_READY, on="_on_preview_ready")
        | states.STALE_EXPORT.to(states.PREVIEW_READY, on="_on_preview_ready")
    )
    export_missing = states.CHECKING_EXPORT.to(states.EXPORT_REQUIRED, on="_on_export_required")
    export_stale = (
        states.CHECKING_EXPORT.to(states.STALE_EXPORT, on="_on_stale_export")
        | states.PREVIEW_READY.to(states.STALE_EXPORT, on="_on_stale_export")
    )
    runtime_observed = (
        states.CHECKING_EXPORT.to(states.FAILED, cond="_observed_failed", on="_on_runtime_failed")
        | states.STALE_EXPORT.to(states.FAILED, cond="_observed_failed", on="_on_runtime_failed")
        | states.APPLYING_RELATED_NOTES.to(states.FAILED, cond="_observed_failed", on="_on_runtime_failed")
        | states.CHECKING_EXPORT.to(
            states.WAITING_EXTERNAL_QUOTA,
            cond="_observed_waiting_external",
            on="_on_runtime_wait_external",
        )
        | states.STALE_EXPORT.to(
            states.WAITING_EXTERNAL_QUOTA,
            cond="_observed_waiting_external",
            on="_on_runtime_wait_external",
        )
        | states.APPLYING_RELATED_NOTES.to(
            states.WAITING_EXTERNAL_QUOTA,
            cond="_observed_waiting_external",
            on="_on_runtime_wait_external",
        )
        | states.CHECKING_EXPORT.to(
            states.EXPORT_REQUIRED,
            cond="_observed_export_missing",
            on="_on_runtime_export_required",
        )
        | states.STALE_EXPORT.to(
            states.EXPORT_REQUIRED,
            cond="_observed_export_missing",
            on="_on_runtime_export_required",
        )
        | states.CHECKING_EXPORT.to(
            states.STALE_EXPORT,
            cond="_observed_export_stale",
            on="_on_runtime_stale_export",
        )
        | states.STALE_EXPORT.to(
            states.RELATED_NOTES_EXPORT_BLOCKED,
            cond="_observed_export_stale",
            on="_on_runtime_blocked",
        )
        | states.CHECKING_EXPORT.to(
            states.WAITING_HUMAN_CONFIRMATION,
            cond="_observed_preview_requires_confirmation",
            on="_on_runtime_human_confirmation",
        )
        | states.STALE_EXPORT.to(
            states.WAITING_HUMAN_CONFIRMATION,
            cond="_observed_preview_requires_confirmation",
            on="_on_runtime_human_confirmation",
        )
        | states.CHECKING_EXPORT.to(states.PREVIEW_READY, cond="_observed_preview_ready", on="_on_runtime_completed")
        | states.STALE_EXPORT.to(states.PREVIEW_READY, cond="_observed_preview_ready", on="_on_runtime_completed")
        | states.APPLYING_RELATED_NOTES.to(states.COMPLETED, cond="_observed_applied", on="_on_runtime_completed")
        | states.CHECKING_EXPORT.to(
            states.RELATED_NOTES_EXPORT_BLOCKED,
            cond="_observed_blocked",
            on="_on_runtime_blocked",
        )
        | states.STALE_EXPORT.to(
            states.RELATED_NOTES_EXPORT_BLOCKED,
            cond="_observed_blocked",
            on="_on_runtime_blocked",
        )
        | states.PREVIEW_READY.to(
            states.RELATED_NOTES_PREVIEW_BLOCKED,
            cond="_observed_blocked",
            on="_on_runtime_blocked",
        )
        | states.APPLYING_RELATED_NOTES.to(
            states.RELATED_NOTES_APPLY_BLOCKED,
            cond="_observed_blocked",
            on="_on_runtime_blocked",
        )
    )
    preview_requires_confirmation = states.PREVIEW_READY.to(
        states.WAITING_HUMAN_CONFIRMATION,
        on="_on_human_confirmation",
    )
    human_apply_approved = states.WAITING_HUMAN_CONFIRMATION.to(
        states.APPLYING_RELATED_NOTES,
        on="_on_apply_related_notes",
    )
    human_apply_cancelled = states.WAITING_HUMAN_CONFIRMATION.to(states.APPLY_CANCELLED, on="_on_blocked")
    related_notes_applied = states.APPLYING_RELATED_NOTES.to(states.COMPLETED, on="_on_completed")
    related_notes_quota_wait = (
        states.CHECKING_EXPORT.to(states.WAITING_EXTERNAL_QUOTA, on="_on_wait_external")
        | states.STALE_EXPORT.to(states.WAITING_EXTERNAL_QUOTA, on="_on_wait_external")
        | states.APPLYING_RELATED_NOTES.to(states.WAITING_EXTERNAL_QUOTA, on="_on_wait_external")
    )
    related_notes_quota_ready = states.WAITING_EXTERNAL_QUOTA.to(states.CHECKING_EXPORT, on="_on_transition")
    link_related_blocked = (
        states.CHECKING_EXPORT.to(states.RELATED_NOTES_EXPORT_BLOCKED, on="_on_blocked")
        | states.EXPORT_REQUIRED.to(states.RELATED_NOTES_EXPORT_BLOCKED, on="_on_blocked")
        | states.STALE_EXPORT.to(states.RELATED_NOTES_EXPORT_BLOCKED, on="_on_blocked")
        | states.PREVIEW_READY.to(states.RELATED_NOTES_PREVIEW_BLOCKED, on="_on_blocked")
        | states.APPLYING_RELATED_NOTES.to(states.RELATED_NOTES_APPLY_BLOCKED, on="_on_blocked")
    )
    link_related_failed = (
        states.CHECKING_EXPORT.to(states.FAILED, on="_on_failed")
        | states.STALE_EXPORT.to(states.FAILED, on="_on_failed")
        | states.APPLYING_RELATED_NOTES.to(states.FAILED, on="_on_failed")
    )

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return category_for_link_related_state(LinkRelatedState(state))

    def _on_transition(self, workflow_event: LinkRelatedEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_preview_ready(self, workflow_event: ExportReadyEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_export_required(self, workflow_event: ExportMissingEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=workflow_event.reason_code,
            effects=[_export_effect(workflow_event, to_state, reason_code=workflow_event.reason_code)],
            resume_action=_resume_action_for_link_related_state(to_state),
        )

    def _on_stale_export(self, workflow_event: ExportStaleEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=workflow_event.reason_code,
            effects=[_export_effect(workflow_event, to_state, reason_code=workflow_event.reason_code)],
            resume_action=_resume_action_for_link_related_state(to_state),
        )

    def _on_runtime_export_required(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, fallback="related_notes_export_missing")
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            effects=[_export_effect(workflow_event, to_state, reason_code=reason_code)],
            resume_action=_runtime_next_action(workflow_event, to_state),
        )

    def _on_runtime_stale_export(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, fallback="related_notes_export_stale")
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            effects=[_export_effect(workflow_event, to_state, reason_code=reason_code)],
            resume_action=_runtime_next_action(workflow_event, to_state),
        )

    def _on_runtime_human_confirmation(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = "link_related_apply_confirmation_required"
        decision = _decision(
            kind="ask_human",
            phase=to_state.value,
            reason_code=reason_code,
            next_action="link-related:confirm-apply",
        )
        packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="link-related-human-confirmation",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.ASK_HUMAN,
            target="human.link_related_confirmation",
            payload={
                "kind": "human_decision",
                "planned_note_count": workflow_event.observation.planned_note_count,
            },
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

    def _on_human_confirmation(
        self,
        workflow_event: PreviewRequiresConfirmationEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = "link_related_apply_confirmation_required"
        decision = _decision(
            kind="ask_human",
            phase=to_state.value,
            reason_code=reason_code,
            next_action="link-related:confirm-apply",
        )
        packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="link-related-human-confirmation",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.ASK_HUMAN,
            target="human.link_related_confirmation",
            payload={"kind": "human_decision", "planned_note_count": workflow_event.planned_note_count},
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

    def _on_apply_related_notes(self, workflow_event: HumanApplyApprovedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_sync_effect(workflow_event, to_state)],
            resume_action=_resume_action_for_link_related_state(to_state),
        )

    def _on_wait_external(
        self,
        workflow_event: RelatedNotesQuotaWaitEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code="related_notes_quota_wait",
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action=workflow_event.resume_action,
        )

    def _on_runtime_wait_external(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=_runtime_reason_code(workflow_event, fallback="related_notes_quota_wait"),
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action=_runtime_next_action(workflow_event, to_state),
        )

    def _on_runtime_blocked(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, fallback="related_notes_blocked")
        next_action = _runtime_next_action(workflow_event, to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_runtime_failed(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = _runtime_reason_code(workflow_event, fallback="related_notes_failed")
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(
                kind="failed",
                phase=to_state.value,
                reason_code=reason_code,
                next_action=_runtime_next_action(workflow_event, to_state),
            ),
        )

    def _on_runtime_completed(
        self,
        workflow_event: LinkRelatedRuntimeObservedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_blocked(self, workflow_event: LinkRelatedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", _event_name(workflow_event)))
        next_action = str(getattr(workflow_event, "next_action", "")) or _resume_action_for_link_related_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_failed(self, workflow_event: LinkRelatedFailedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            reason_code=workflow_event.reason_code,
            decision=_decision(
                kind="failed",
                phase=to_state.value,
                reason_code=workflow_event.reason_code,
                next_action=workflow_event.next_action,
            ),
        )

    def _on_completed(self, workflow_event: LinkRelatedEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _observed_failed(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.failed

    def _observed_waiting_external(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.waiting_external

    def _observed_export_missing(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.export_missing

    def _observed_export_stale(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.export_stale

    def _observed_preview_requires_confirmation(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.preview_ready and observation.planned_note_count > 0

    def _observed_preview_ready(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.preview_ready and observation.planned_note_count <= 0

    def _observed_applied(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.applied

    def _observed_blocked(self, workflow_event: LinkRelatedRuntimeObservedEvent) -> bool:
        return workflow_event.observation.blocked


def category_for_link_related_state(state: LinkRelatedState) -> WorkflowStateCategory:
    """Map each link-related leaf state to the public workflow category."""

    match state:
        case LinkRelatedState.CHECKING_EXPORT | LinkRelatedState.APPLYING_RELATED_NOTES:
            return WorkflowStateCategory.RUNNING
        case LinkRelatedState.EXPORT_REQUIRED | LinkRelatedState.STALE_EXPORT:
            return WorkflowStateCategory.WAITING_AGENT
        case LinkRelatedState.PREVIEW_READY | LinkRelatedState.COMPLETED:
            return WorkflowStateCategory.COMPLETED
        case LinkRelatedState.WAITING_HUMAN_CONFIRMATION:
            return WorkflowStateCategory.WAITING_HUMAN
        case LinkRelatedState.WAITING_EXTERNAL_QUOTA:
            return WorkflowStateCategory.WAITING_EXTERNAL
        case (
            LinkRelatedState.APPLY_CANCELLED
            | LinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED
            | LinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED
            | LinkRelatedState.RELATED_NOTES_APPLY_BLOCKED
        ):
            return WorkflowStateCategory.BLOCKED
        case LinkRelatedState.FAILED:
            return WorkflowStateCategory.FAILED


def _transition(
    workflow_event: LinkRelatedEvent,
    to_state: LinkRelatedState,
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
        reason_code=reason_code or _event_name(workflow_event),
        effects=list(effects or []),
        decision=decision,
        human_decision_packet=human_decision_packet,
        resume_action=resume_action,
    )


def _target_state(target: object) -> LinkRelatedState:
    value = getattr(target, "value", target)
    return LinkRelatedState(str(value))


def _export_effect(
    workflow_event: LinkRelatedEvent,
    origin_state: LinkRelatedState,
    *,
    reason_code: str,
) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"link-related-{origin_state.value.replace('_', '-')}",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="related_notes.export",
        payload=RelatedNotesExportEffectPayload(reason_code=reason_code).to_payload(),
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=_resume_action_for_link_related_state(origin_state),
    )


def _sync_effect(workflow_event: LinkRelatedEvent, origin_state: LinkRelatedState) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="link-related-sync-section",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="related_notes.section",
        payload=RelatedNotesSyncSectionEffectPayload(apply=True).to_payload(),
        mutates_resources=True,
        rollback_declared=True,
        requires_receipt=True,
    )


def _wait_external_effect(
    workflow_event: RelatedNotesQuotaWaitEvent | LinkRelatedRuntimeObservedEvent,
    origin_state: LinkRelatedState,
) -> WorkflowEffect:
    if isinstance(workflow_event, LinkRelatedRuntimeObservedEvent):
        recovery_state = workflow_event.observation.related_notes_recovery_state
        resume_action = _runtime_next_action(workflow_event, origin_state)
        fresh_record_count = workflow_event.observation.fresh_record_count
        stale_record_count = workflow_event.observation.stale_record_count
        remaining_count = workflow_event.observation.remaining_count
    else:
        recovery_state = workflow_event.related_notes_recovery_state
        resume_action = workflow_event.resume_action
        fresh_record_count = workflow_event.fresh_record_count
        stale_record_count = workflow_event.stale_record_count
        remaining_count = workflow_event.remaining_count
    if not recovery_state.status:
        recovery_state = RelatedNotesRecoveryStateEffectPayload.from_payload(
            {
                "schema": "medical-notes-workbench.related-notes-recovery-state.v1",
                "status": "waiting_for_retry",
                "blocked_reason": "related_notes_headless_quota_exhausted",
                "next_action": resume_action,
                "fresh_record_count": fresh_record_count,
                "stale_record_count": stale_record_count,
                "remaining_count": remaining_count,
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
        effect_id="link-related-quota-wait",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.WAIT_EXTERNAL,
        target="related_notes.quota",
        payload=payload,
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=resume_action,
    )


def _runtime_reason_code(workflow_event: LinkRelatedRuntimeObservedEvent, *, fallback: str) -> str:
    value = workflow_event.observation.reason_code.strip()
    return value or fallback


def _runtime_next_action(workflow_event: LinkRelatedRuntimeObservedEvent, state: LinkRelatedState) -> str:
    value = workflow_event.observation.next_action.strip()
    return value or _resume_action_for_link_related_state(state)


def _decision(*, kind: str, phase: str, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind=kind,
        phase=phase,
        reason_code=reason_code,
        public_summary="Notas Relacionadas precisam de condução antes de continuar.",
        developer_summary=f"LinkRelatedMachine reached {phase}:{reason_code}.",
        evidence=[
            DecisionEvidence(
                summary="A StateChart de link-related decidiu a proxima etapa.",
                technical_code=reason_code,
                source="LinkRelatedMachine",
            )
        ],
        rejected_automations=_rejected_automations() if kind == "ask_human" else [],
        next_action=next_action,
        resume_action=next_action,
        options=_confirmation_options() if kind == "ask_human" else [],
        recommended_option_id="confirm_apply" if kind == "ask_human" else "",
        human_decision_kind="link_related_apply_confirmation" if kind == "ask_human" else "",
    )


def _rejected_automations() -> list[RejectedAutomation]:
    return [
        RejectedAutomation(
            kind="auto_fix",
            reason_code="vault_mutation_requires_confirmation",
            reason="A seção Notas Relacionadas altera Markdown do vault.",
        ),
        RejectedAutomation(
            kind="auto_defer",
            reason_code="preview_ready_for_human_choice",
            reason="Adiar sem pergunta esconderia uma confirmação necessária.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code="apply_plan_already_exists",
            reason="A prévia já definiu o plano; falta autorização humana.",
        ),
    ]


def _confirmation_options() -> list[HumanDecisionOption]:
    return [
        HumanDecisionOption(
            id="confirm_apply",
            label="Aplicar Notas Relacionadas",
            description="Atualiza a seção gerenciada usando a prévia validada.",
            consequence="Mutará a seção gerenciada no vault.",
            safety="vault_guard_required",
        ),
        HumanDecisionOption(
            id="cancel",
            label="Cancelar",
            description="Mantém o vault como está e encerra a aplicação.",
            consequence="Nenhum arquivo será alterado.",
            safety="no_resource_mutation",
        ),
    ]


def _resume_action_for_link_related_state(state: LinkRelatedState) -> str:
    match state:
        case LinkRelatedState.EXPORT_REQUIRED | LinkRelatedState.STALE_EXPORT:
            return "link-related:recover-export"
        case LinkRelatedState.WAITING_HUMAN_CONFIRMATION:
            return "link-related:confirm-apply"
        case LinkRelatedState.APPLYING_RELATED_NOTES:
            return "link-related:apply"
        case LinkRelatedState.WAITING_EXTERNAL_QUOTA:
            return "link-related:retry-export"
        case (
            LinkRelatedState.APPLY_CANCELLED
            | LinkRelatedState.RELATED_NOTES_EXPORT_BLOCKED
            | LinkRelatedState.RELATED_NOTES_PREVIEW_BLOCKED
            | LinkRelatedState.RELATED_NOTES_APPLY_BLOCKED
            | LinkRelatedState.FAILED
        ):
            return "link-related:diagnose"
        case _:
            return ""
