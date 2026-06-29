"""Canonical StateChart for `/flashcards`.

The machine keeps Anki writes, source staleness, human confirmation, and
Obsidian tag mutation as explicit states. `fsm.py` is the public projector for
this canonical StateChart; compatibility with older payloads belongs at adapter
boundaries, not inside the machine identity.
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

FLASHCARDS_WORKFLOW: Literal["/flashcards"] = "/flashcards"


class FlashcardsState(StrEnum):
    CHECKING_SOURCES = "checking_sources"
    WAITING_AGENT_CANDIDATES = "waiting_agent_candidates"
    WAITING_HUMAN_CONFIRMATION = "waiting_human_confirmation"
    ANKI_UNAVAILABLE = "anki_unavailable"
    WRITING_ANKI = "writing_anki"
    TAGGING_OBSIDIAN = "tagging_obsidian"
    STALE_SOURCE = "stale_source"
    CREATE_CANCELLED = "create_cancelled"
    SOURCE_SELECTION_BLOCKED = "flashcards_source_selection_blocked"
    CANDIDATE_GENERATION_BLOCKED = "flashcards_candidate_generation_blocked"
    PREVIEW_DECISION_BLOCKED = "flashcards_preview_decision_blocked"
    ANKI_WRITE_BLOCKED = "flashcards_anki_write_blocked"
    OBSIDIAN_TAGGING_BLOCKED = "flashcards_obsidian_tagging_blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class FlashcardsEvent(ContractModel):
    """Base event accepted by the flashcards StateChart."""

    workflow: str = FLASHCARDS_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_flashcards(cls, value: str) -> str:
        if value != FLASHCARDS_WORKFLOW:
            raise ValueError(f"flashcards event workflow must be {FLASHCARDS_WORKFLOW}")
        return value


def _event_name(event: FlashcardsEvent) -> str:
    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("flashcards events must declare a name discriminator")
    return name


class SourcesResolvedEvent(FlashcardsEvent):
    name: Literal["sources_resolved"] = "sources_resolved"
    source_count: int = Field(ge=0, strict=True)


class CandidateGenerationCompletedEvent(FlashcardsEvent):
    name: Literal["candidate_generation_completed"] = "candidate_generation_completed"
    candidate_count: int = Field(ge=0, strict=True)
    new_card_count: int = Field(ge=0, strict=True)


class NoCardsToCreateEvent(FlashcardsEvent):
    name: Literal["no_cards_to_create"] = "no_cards_to_create"
    source_count: int = Field(ge=0, strict=True)
    candidate_count: int = Field(ge=0, strict=True)
    duplicate_card_count: int = Field(ge=0, strict=True)


class HumanCreateApprovedEvent(FlashcardsEvent):
    name: Literal["human_create_approved"] = "human_create_approved"
    approved_by: str = Field(min_length=1)


class HumanCreateCancelledEvent(FlashcardsEvent):
    name: Literal["human_create_cancelled"] = "human_create_cancelled"
    cancelled_by: str = Field(min_length=1)


class AnkiUnavailableEvent(FlashcardsEvent):
    name: Literal["anki_unavailable"] = "anki_unavailable"
    resume_action: str = Field(min_length=1)


class AnkiAvailableEvent(FlashcardsEvent):
    name: Literal["anki_available"] = "anki_available"
    restored_by: str = Field(min_length=1)


class AnkiWriteCompletedEvent(FlashcardsEvent):
    name: Literal["anki_write_completed"] = "anki_write_completed"
    created_card_count: int = Field(ge=0, strict=True)


class ObsidianTaggingCompletedEvent(FlashcardsEvent):
    name: Literal["obsidian_tagging_completed"] = "obsidian_tagging_completed"
    tagged_source_count: int = Field(ge=0, strict=True)
    changed_files: list[str] = Field(default_factory=list)


class StaleSourceDetectedEvent(FlashcardsEvent):
    name: Literal["stale_source_detected"] = "stale_source_detected"
    reason_code: Literal["flashcards_source_stale"] = "flashcards_source_stale"
    next_action: str = Field(min_length=1)


class SourceRefreshedEvent(FlashcardsEvent):
    name: Literal["source_refreshed"] = "source_refreshed"
    refreshed_by: str = Field(min_length=1)


class FlashcardsBlockedEvent(FlashcardsEvent):
    name: Literal["flashcards_blocked"] = "flashcards_blocked"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class FlashcardsFailedEvent(FlashcardsEvent):
    name: Literal["flashcards_failed"] = "flashcards_failed"
    reason_code: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


FlashcardsBoundaryEvent = Annotated[
    SourcesResolvedEvent
    | CandidateGenerationCompletedEvent
    | NoCardsToCreateEvent
    | HumanCreateApprovedEvent
    | HumanCreateCancelledEvent
    | AnkiUnavailableEvent
    | AnkiAvailableEvent
    | AnkiWriteCompletedEvent
    | ObsidianTaggingCompletedEvent
    | StaleSourceDetectedEvent
    | SourceRefreshedEvent
    | FlashcardsBlockedEvent
    | FlashcardsFailedEvent,
    Field(discriminator="name"),
]
FlashcardsBoundaryEventAdapter = TypeAdapter(FlashcardsBoundaryEvent)


class FlashcardsMachine(StateChart[WorkflowModel]):
    """Pure domain canonical StateChart for preview-first flashcard creation."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        FlashcardsState,
        initial=FlashcardsState.CHECKING_SOURCES,
        final={
            FlashcardsState.COMPLETED,
            FlashcardsState.CREATE_CANCELLED,
            FlashcardsState.SOURCE_SELECTION_BLOCKED,
            FlashcardsState.CANDIDATE_GENERATION_BLOCKED,
            FlashcardsState.PREVIEW_DECISION_BLOCKED,
            FlashcardsState.ANKI_WRITE_BLOCKED,
            FlashcardsState.OBSIDIAN_TAGGING_BLOCKED,
            FlashcardsState.FAILED,
        },
        use_enum_instance=False,
    )

    sources_resolved = states.CHECKING_SOURCES.to(states.WAITING_AGENT_CANDIDATES, on="_on_agent_candidates")
    candidate_generation_completed = states.WAITING_AGENT_CANDIDATES.to(
        states.WAITING_HUMAN_CONFIRMATION,
        on="_on_human_confirmation",
    )
    no_cards_to_create = states.WAITING_AGENT_CANDIDATES.to(states.COMPLETED, on="_on_completed")
    human_create_approved = states.WAITING_HUMAN_CONFIRMATION.to(states.WRITING_ANKI, on="_on_write_anki")
    human_create_cancelled = states.WAITING_HUMAN_CONFIRMATION.to(states.CREATE_CANCELLED, on="_on_blocked")
    anki_unavailable = (
        states.WAITING_HUMAN_CONFIRMATION.to(states.ANKI_UNAVAILABLE, on="_on_wait_external")
        | states.WRITING_ANKI.to(states.ANKI_UNAVAILABLE, on="_on_wait_external")
    )
    anki_available = states.ANKI_UNAVAILABLE.to(states.CHECKING_SOURCES, on="_on_transition")
    anki_write_completed = states.WRITING_ANKI.to(states.TAGGING_OBSIDIAN, on="_on_tag_obsidian")
    obsidian_tagging_completed = states.TAGGING_OBSIDIAN.to(states.COMPLETED, on="_on_completed")
    stale_source_detected = (
        states.WAITING_HUMAN_CONFIRMATION.to(states.STALE_SOURCE, on="_on_blocked")
        | states.WRITING_ANKI.to(states.STALE_SOURCE, on="_on_blocked")
        | states.TAGGING_OBSIDIAN.to(states.STALE_SOURCE, on="_on_blocked")
    )
    source_refreshed = states.STALE_SOURCE.to(states.CHECKING_SOURCES, on="_on_transition")
    flashcards_blocked = (
        states.CHECKING_SOURCES.to(states.SOURCE_SELECTION_BLOCKED, on="_on_blocked")
        | states.WAITING_AGENT_CANDIDATES.to(states.CANDIDATE_GENERATION_BLOCKED, on="_on_blocked")
        | states.WAITING_HUMAN_CONFIRMATION.to(states.PREVIEW_DECISION_BLOCKED, on="_on_blocked")
        | states.WRITING_ANKI.to(states.ANKI_WRITE_BLOCKED, on="_on_blocked")
        | states.TAGGING_OBSIDIAN.to(states.OBSIDIAN_TAGGING_BLOCKED, on="_on_blocked")
    )
    flashcards_failed = (
        states.CHECKING_SOURCES.to(states.FAILED, on="_on_failed")
        | states.WAITING_AGENT_CANDIDATES.to(states.FAILED, on="_on_failed")
        | states.WRITING_ANKI.to(states.FAILED, on="_on_failed")
        | states.TAGGING_OBSIDIAN.to(states.FAILED, on="_on_failed")
    )

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return category_for_flashcards_state(FlashcardsState(state))

    def _on_transition(self, workflow_event: FlashcardsEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))

    def _on_agent_candidates(self, workflow_event: SourcesResolvedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_subworkflow_effect(workflow_event, to_state, target="flashcards.generate_candidates")],
            resume_action=_resume_action_for_flashcards_state(to_state),
        )

    def _on_human_confirmation(
        self,
        workflow_event: CandidateGenerationCompletedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = "flashcards_preview_confirmation_required"
        decision = _decision(
            kind="ask_human",
            phase=to_state.value,
            reason_code=reason_code,
            next_action="flashcards:confirm-create",
        )
        packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="flashcards-human-confirmation",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.ASK_HUMAN,
            target="human.flashcards_create_confirmation",
            payload={
                "kind": "human_decision",
                "candidate_count": workflow_event.candidate_count,
                "new_card_count": workflow_event.new_card_count,
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

    def _on_write_anki(self, workflow_event: HumanCreateApprovedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_subworkflow_effect(workflow_event, to_state, target="flashcards.write_anki")],
            resume_action=_resume_action_for_flashcards_state(to_state),
        )

    def _on_wait_external(self, workflow_event: AnkiUnavailableEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="flashcards-anki-wait",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.WAIT_EXTERNAL,
            target="anki.desktop",
            payload={
                "schema": "medical-notes-workbench.wait-external-effect-payload.v1",
                "kind": "wait_external",
                "wait_target": "anki.desktop",
                "blocked_reason": "anki_unavailable",
                "next_action": workflow_event.resume_action,
                "resume_supported": True,
            },
            requires_receipt=False,
            no_resource_mutation=True,
            resume_action=workflow_event.resume_action,
        )
        return _transition(
            workflow_event,
            to_state,
            reason_code="anki_unavailable",
            effects=[effect],
            resume_action=workflow_event.resume_action,
        )

    def _on_tag_obsidian(self, workflow_event: AnkiWriteCompletedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        effect = WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="flashcards-tag-obsidian",
            origin_state=to_state.value,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="flashcards.tag_obsidian",
            payload={"kind": "tag_obsidian", "created_card_count": workflow_event.created_card_count},
            mutates_resources=True,
            rollback_declared=True,
            requires_receipt=True,
        )
        return _transition(
            workflow_event,
            to_state,
            effects=[effect],
            resume_action=_resume_action_for_flashcards_state(to_state),
        )

    def _on_blocked(self, workflow_event: FlashcardsEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        reason_code = str(getattr(workflow_event, "reason_code", _event_name(workflow_event)))
        next_action = str(getattr(workflow_event, "next_action", "")) or _resume_action_for_flashcards_state(to_state)
        return _transition(
            workflow_event,
            to_state,
            reason_code=reason_code,
            decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code, next_action=next_action),
            resume_action=next_action,
        )

    def _on_failed(self, workflow_event: FlashcardsFailedEvent, target: object) -> WorkflowTransitionResult:
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

    def _on_completed(self, workflow_event: FlashcardsEvent, target: object) -> WorkflowTransitionResult:
        return _transition(workflow_event, _target_state(target))


def category_for_flashcards_state(state: FlashcardsState) -> WorkflowStateCategory:
    """Map each flashcards leaf state to the public workflow category."""

    match state:
        case FlashcardsState.CHECKING_SOURCES:
            return WorkflowStateCategory.RUNNING
        case FlashcardsState.WAITING_AGENT_CANDIDATES | FlashcardsState.WRITING_ANKI | FlashcardsState.TAGGING_OBSIDIAN:
            return WorkflowStateCategory.WAITING_AGENT
        case FlashcardsState.WAITING_HUMAN_CONFIRMATION:
            return WorkflowStateCategory.WAITING_HUMAN
        case FlashcardsState.ANKI_UNAVAILABLE:
            return WorkflowStateCategory.WAITING_EXTERNAL
        case (
            FlashcardsState.STALE_SOURCE
            | FlashcardsState.CREATE_CANCELLED
            | FlashcardsState.SOURCE_SELECTION_BLOCKED
            | FlashcardsState.CANDIDATE_GENERATION_BLOCKED
            | FlashcardsState.PREVIEW_DECISION_BLOCKED
            | FlashcardsState.ANKI_WRITE_BLOCKED
            | FlashcardsState.OBSIDIAN_TAGGING_BLOCKED
        ):
            return WorkflowStateCategory.BLOCKED
        case FlashcardsState.FAILED:
            return WorkflowStateCategory.FAILED
        case FlashcardsState.COMPLETED:
            return WorkflowStateCategory.COMPLETED


def _transition(
    workflow_event: FlashcardsEvent,
    to_state: FlashcardsState,
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


def _target_state(target: object) -> FlashcardsState:
    value = getattr(target, "value", target)
    return FlashcardsState(str(value))


def _subworkflow_effect(workflow_event: FlashcardsEvent, origin_state: FlashcardsState, *, target: str) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"flashcards-{origin_state.value.replace('_', '-')}",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=target,
        payload={"kind": target.replace(".", "_")},
        requires_receipt=True,
        no_resource_mutation=True,
    )


def _decision(*, kind: str, phase: str, reason_code: str, next_action: str) -> WorkflowDecision:
    return WorkflowDecision(
        kind=kind,
        phase=phase,
        reason_code=reason_code,
        public_summary="Flashcards precisam de condução antes de continuar.",
        developer_summary=f"FlashcardsMachine reached {phase}:{reason_code}.",
        evidence=[
            DecisionEvidence(
                summary="A StateChart de flashcards decidiu a proxima etapa.",
                technical_code=reason_code,
                source="FlashcardsMachine",
            )
        ],
        rejected_automations=_rejected_automations() if kind == "ask_human" else [],
        next_action=next_action,
        resume_action=next_action,
        options=_confirmation_options() if kind == "ask_human" else [],
        recommended_option_id="create_cards" if kind == "ask_human" else "",
        human_decision_kind="flashcards_preview_confirmation" if kind == "ask_human" else "",
    )


def _rejected_automations() -> list[RejectedAutomation]:
    return [
        RejectedAutomation(
            kind="auto_fix",
            reason_code="writes_anki_and_vault_tag",
            reason="Criar cards e marcar notas exige confirmação humana.",
        ),
        RejectedAutomation(
            kind="auto_defer",
            reason_code="preview_ready_for_human_choice",
            reason="Adiar sem perguntar esconderia uma decisão necessária.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code="plan_already_exists",
            reason="A prévia já existe; falta autorização para escrever.",
        ),
    ]


def _confirmation_options() -> list[HumanDecisionOption]:
    return [
        HumanDecisionOption(
            id="create_cards",
            label="Criar cards",
            description="Grava os cards novos no Anki.",
            consequence="Pode criar notas no Anki e depois marcar fontes no Obsidian.",
            safety="preview_required",
        ),
        HumanDecisionOption(
            id="review_candidates",
            label="Revisar candidatos",
            description="Volta para a prévia sem gravar.",
            consequence="Nenhum card é criado.",
            safety="no_mutation",
        ),
    ]


def _resume_action_for_flashcards_state(state: FlashcardsState) -> str:
    match state:
        case FlashcardsState.WAITING_AGENT_CANDIDATES:
            return "flashcards:generate-candidates"
        case FlashcardsState.WAITING_HUMAN_CONFIRMATION:
            return "flashcards:confirm-create"
        case FlashcardsState.ANKI_UNAVAILABLE:
            return "flashcards:retry-anki"
        case FlashcardsState.WRITING_ANKI:
            return "flashcards:write-anki"
        case FlashcardsState.TAGGING_OBSIDIAN:
            return "flashcards:tag-obsidian"
        case (
            FlashcardsState.STALE_SOURCE
            | FlashcardsState.CREATE_CANCELLED
            | FlashcardsState.SOURCE_SELECTION_BLOCKED
            | FlashcardsState.CANDIDATE_GENERATION_BLOCKED
            | FlashcardsState.PREVIEW_DECISION_BLOCKED
            | FlashcardsState.ANKI_WRITE_BLOCKED
            | FlashcardsState.OBSIDIAN_TAGGING_BLOCKED
            | FlashcardsState.FAILED
        ):
            return "flashcards:prepare"
        case _:
            return ""
