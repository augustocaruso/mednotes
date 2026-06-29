"""Operational StateChart for `/mednotes:history`.

History/restore is a public preview/apply workflow. This machine keeps restore
preview, human confirmation, stale restore points, conflicts, and the mutating
restore apply as explicit states; Git execution stays in the vault adapter.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator
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

HISTORY_WORKFLOW: Literal["/mednotes:history"] = "/mednotes:history"


class HistoryState(StrEnum):
    LISTING_RESTORE_POINTS = "listing_restore_points"
    RESTORE_POINTS_LISTED = "restore_points_listed"
    PREVIEW_READY = "preview_ready"
    WAITING_HUMAN_CONFIRMATION = "waiting_human_confirmation"
    APPLYING_RESTORE = "applying_restore"
    STALE_RESTORE_POINT = "stale_restore_point"
    RESTORE_CONFLICT = "restore_conflict"
    RESTORE_CANCELLED = "restore_cancelled"
    RESTORE_POINT_LIST_BLOCKED = "restore_point_list_blocked"
    RESTORE_PREVIEW_BLOCKED = "restore_preview_blocked"
    RESTORE_CONFIRMATION_BLOCKED = "restore_confirmation_blocked"
    RESTORE_APPLY_BLOCKED = "restore_apply_blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class HistoryEvent(ContractModel):
    """Base event accepted by the history StateChart."""

    workflow: str = HISTORY_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_history(cls, value: str) -> str:
        if value != HISTORY_WORKFLOW:
            raise ValueError(f"history event workflow must be {HISTORY_WORKFLOW}")
        return value


def _event_name(event: HistoryEvent) -> str:
    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("history events must declare a name discriminator")
    return name


class RestorePointSelectedEvent(HistoryEvent):
    name: Literal["restore_point_selected"] = "restore_point_selected"
    restore_point_id: str = Field(min_length=1)


class RestorePointsListedEvent(HistoryEvent):
    name: Literal["restore_points_listed"] = "restore_points_listed"
    restore_point_count: int = Field(default=0, ge=0, strict=True)


class PreviewRequiresConfirmationEvent(HistoryEvent):
    name: Literal["preview_requires_confirmation"] = "preview_requires_confirmation"
    restore_preview_path: str = Field(min_length=1)
    affected_file_count: int = Field(ge=1, strict=True)


class HumanRestoreApprovedEvent(HistoryEvent):
    name: Literal["human_restore_approved"] = "human_restore_approved"
    approved_by: str = Field(min_length=1)


class HumanRestoreCancelledEvent(HistoryEvent):
    name: Literal["human_restore_cancelled"] = "human_restore_cancelled"
    cancelled_by: str = Field(min_length=1)


class RestoreAppliedEvent(HistoryEvent):
    name: Literal["restore_applied"] = "restore_applied"
    restored_file_count: int = Field(ge=0, strict=True)


class StaleRestorePointDetectedEvent(HistoryEvent):
    name: Literal["stale_restore_point_detected"] = "stale_restore_point_detected"
    reason_code: Literal["stale_restore_point"] = "stale_restore_point"
    next_action: str = Field(min_length=1)


class RestorePointRefreshedEvent(HistoryEvent):
    name: Literal["restore_point_refreshed"] = "restore_point_refreshed"
    refreshed_by: str = Field(min_length=1)


class RestoreConflictDetectedEvent(HistoryEvent):
    name: Literal["restore_conflict_detected"] = "restore_conflict_detected"
    reason_code: Literal["restore_conflict"] = "restore_conflict"
    next_action: str = Field(min_length=1)


class RestoreConflictResolvedEvent(HistoryEvent):
    name: Literal["restore_conflict_resolved"] = "restore_conflict_resolved"
    resolved_by: str = Field(min_length=1)


class HistoryBlockedEvent(HistoryEvent):
    name: Literal["history_blocked"] = "history_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class HistoryFailedEvent(HistoryEvent):
    name: Literal["history_failed"] = "history_failed"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


HistoryBoundaryEvent = Annotated[
    RestorePointSelectedEvent
    | RestorePointsListedEvent
    | PreviewRequiresConfirmationEvent
    | HumanRestoreApprovedEvent
    | HumanRestoreCancelledEvent
    | RestoreAppliedEvent
    | StaleRestorePointDetectedEvent
    | RestorePointRefreshedEvent
    | RestoreConflictDetectedEvent
    | RestoreConflictResolvedEvent
    | HistoryBlockedEvent
    | HistoryFailedEvent,
    Field(discriminator="name"),
]
HistoryBoundaryEventAdapter = TypeAdapter(HistoryBoundaryEvent)


class HistoryMachine(StateChart[WorkflowModel]):
    """Pure domain StateChart for restore preview/apply."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        HistoryState,
        initial=HistoryState.LISTING_RESTORE_POINTS,
        final={
            HistoryState.COMPLETED,
            HistoryState.RESTORE_POINTS_LISTED,
            HistoryState.RESTORE_CANCELLED,
            HistoryState.RESTORE_POINT_LIST_BLOCKED,
            HistoryState.RESTORE_PREVIEW_BLOCKED,
            HistoryState.RESTORE_CONFIRMATION_BLOCKED,
            HistoryState.RESTORE_APPLY_BLOCKED,
            HistoryState.FAILED,
        },
        use_enum_instance=False,
    )

    restore_point_selected = states.LISTING_RESTORE_POINTS.to(states.PREVIEW_READY, on="_on_preview_requested")
    restore_points_listed = states.LISTING_RESTORE_POINTS.to(states.RESTORE_POINTS_LISTED, on="_on_completed")
    preview_requires_confirmation = states.PREVIEW_READY.to(
        states.WAITING_HUMAN_CONFIRMATION,
        on="_on_human_confirmation",
    )
    human_restore_approved = states.WAITING_HUMAN_CONFIRMATION.to(states.APPLYING_RESTORE, on="_on_apply_restore")
    human_restore_cancelled = states.WAITING_HUMAN_CONFIRMATION.to(states.RESTORE_CANCELLED, on="_on_blocked")
    restore_applied = states.APPLYING_RESTORE.to(states.COMPLETED, on="_on_completed")
    stale_restore_point_detected = (
        states.PREVIEW_READY.to(states.STALE_RESTORE_POINT, on="_on_blocked")
        | states.WAITING_HUMAN_CONFIRMATION.to(states.STALE_RESTORE_POINT, on="_on_blocked")
        | states.APPLYING_RESTORE.to(states.STALE_RESTORE_POINT, on="_on_blocked")
    )
    restore_point_refreshed = states.STALE_RESTORE_POINT.to(states.LISTING_RESTORE_POINTS, on="_on_transition")
    restore_conflict_detected = (
        states.PREVIEW_READY.to(states.RESTORE_CONFLICT, on="_on_blocked")
        | states.WAITING_HUMAN_CONFIRMATION.to(states.RESTORE_CONFLICT, on="_on_blocked")
        | states.APPLYING_RESTORE.to(states.RESTORE_CONFLICT, on="_on_blocked")
    )
    restore_conflict_resolved = states.RESTORE_CONFLICT.to(states.LISTING_RESTORE_POINTS, on="_on_transition")
    history_blocked = (
        states.LISTING_RESTORE_POINTS.to(states.RESTORE_POINT_LIST_BLOCKED, on="_on_blocked")
        | states.PREVIEW_READY.to(states.RESTORE_PREVIEW_BLOCKED, on="_on_blocked")
        | states.WAITING_HUMAN_CONFIRMATION.to(states.RESTORE_CONFIRMATION_BLOCKED, on="_on_blocked")
        | states.APPLYING_RESTORE.to(states.RESTORE_APPLY_BLOCKED, on="_on_blocked")
    )
    history_failed = (
        states.LISTING_RESTORE_POINTS.to(states.FAILED, on="_on_failed")
        | states.PREVIEW_READY.to(states.FAILED, on="_on_failed")
        | states.APPLYING_RESTORE.to(states.FAILED, on="_on_failed")
    )

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return category_for_history_state(HistoryState(state))

    def _on_transition(self, workflow_event: HistoryEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_preview_requested(self, workflow_event: RestorePointSelectedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="history-restore-preview",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="history.restore_preview",
            payload={"kind": "restore_preview", "restore_point_id": workflow_event.restore_point_id},
            requires_receipt=False,
            no_resource_mutation=True,
        )
        return _transition(workflow_event, to_state, effects=[effect], resume_action="history:preview")

    def _on_human_confirmation(
        self,
        workflow_event: PreviewRequiresConfirmationEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = "history_restore_confirmation_required"
        decision = _decision(
            kind="ask_human",
            phase=to_state.value,
            reason_code=reason_code,
            next_action="history:confirm-restore",
        )
        packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="history-human-confirmation",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.ASK_HUMAN,
            target="human.history_restore_confirmation",
            payload={
                "kind": "human_decision",
                "restore_preview_path": workflow_event.restore_preview_path,
                "affected_file_count": workflow_event.affected_file_count,
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

    def _on_apply_restore(self, workflow_event: HumanRestoreApprovedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="history-restore-apply",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="history.restore_apply",
            payload={"kind": "restore_apply"},
            mutates_resources=True,
            rollback_declared=True,
            requires_receipt=True,
        )
        return _transition(workflow_event, to_state, effects=[effect], resume_action="history:apply")

    def _on_blocked(self, workflow_event: HistoryEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", _event_name(workflow_event)))
        next_action = str(getattr(workflow_event, "next_action", "")) or _resume_action_for_history_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_failed(self, workflow_event: HistoryFailedEvent, target: object) -> WorkflowTransitionResult:
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

    def _on_completed(self, workflow_event: HistoryEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))


def category_for_history_state(state: HistoryState) -> WorkflowStateCategory:
    """Map each history leaf state to the public workflow category."""

    match state:
        case HistoryState.LISTING_RESTORE_POINTS | HistoryState.PREVIEW_READY | HistoryState.APPLYING_RESTORE:
            return WorkflowStateCategory.RUNNING
        case HistoryState.WAITING_HUMAN_CONFIRMATION:
            return WorkflowStateCategory.WAITING_HUMAN
        case (
            HistoryState.STALE_RESTORE_POINT
            | HistoryState.RESTORE_CONFLICT
            | HistoryState.RESTORE_CANCELLED
            | HistoryState.RESTORE_POINT_LIST_BLOCKED
            | HistoryState.RESTORE_PREVIEW_BLOCKED
            | HistoryState.RESTORE_CONFIRMATION_BLOCKED
            | HistoryState.RESTORE_APPLY_BLOCKED
        ):
            return WorkflowStateCategory.BLOCKED
        case HistoryState.FAILED:
            return WorkflowStateCategory.FAILED
        case HistoryState.COMPLETED | HistoryState.RESTORE_POINTS_LISTED:
            return WorkflowStateCategory.COMPLETED


def _transition(
    workflow_event: HistoryEvent,
    to_state: HistoryState,
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


def _target_state(target: object) -> HistoryState:
    value = getattr(target, "value", target)
    return HistoryState(str(value))


def _decision(*, kind: str, phase: str, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind=kind,
        phase=phase,
        reason_code=reason_code,
        public_summary="Historico do vault precisa de condução antes de continuar.",
        developer_summary=f"HistoryMachine reached {phase}:{reason_code}.",
        evidence=[
            DecisionEvidence(
                summary="A StateChart de history decidiu a proxima etapa.",
                technical_code=reason_code,
                source="HistoryMachine",
            )
        ],
        rejected_automations=_rejected_automations() if kind == "ask_human" else [],
        next_action=next_action,
        resume_action=next_action,
        options=_confirmation_options() if kind == "ask_human" else [],
        recommended_option_id="confirm_restore" if kind == "ask_human" else "",
        human_decision_kind="history_restore_confirmation" if kind == "ask_human" else "",
    )


def _rejected_automations() -> list[RejectedAutomation]:
    return [
        RejectedAutomation(
            kind="auto_fix",
            reason_code="restore_mutates_resources",
            reason="Aplicar restauração muda arquivos do vault.",
        ),
        RejectedAutomation(
            kind="auto_defer",
            reason_code="restore_preview_requires_choice",
            reason="Adiar sem pergunta esconderia uma decisão de restauração.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code="restore_preview_already_exists",
            reason="A prévia já existe; falta autorização para aplicar.",
        ),
    ]


def _confirmation_options() -> list[HumanDecisionOption]:
    return [
        HumanDecisionOption(
            id="confirm_restore",
            label="Aplicar restauração",
            description="Restaura o vault conforme a prévia validada.",
            consequence="Pode alterar ou remover arquivos do vault.",
            safety="restore_preview_required",
        ),
        HumanDecisionOption(
            id="cancel",
            label="Cancelar",
            description="Mantém o vault como está.",
            consequence="Nenhum arquivo será restaurado.",
            safety="no_mutation",
        ),
    ]


def _resume_action_for_history_state(state: HistoryState) -> str:
    match state:
        case HistoryState.PREVIEW_READY:
            return "history:preview"
        case HistoryState.WAITING_HUMAN_CONFIRMATION:
            return "history:confirm-restore"
        case HistoryState.APPLYING_RESTORE:
            return "history:apply"
        case HistoryState.STALE_RESTORE_POINT:
            return "history:refresh-restore-point"
        case HistoryState.RESTORE_CONFLICT:
            return "history:resolve-conflict"
        case (
            HistoryState.RESTORE_CANCELLED
            | HistoryState.RESTORE_POINT_LIST_BLOCKED
            | HistoryState.RESTORE_PREVIEW_BLOCKED
            | HistoryState.RESTORE_CONFIRMATION_BLOCKED
            | HistoryState.RESTORE_APPLY_BLOCKED
            | HistoryState.FAILED
        ):
            return "history:timeline"
        case _:
            return ""
