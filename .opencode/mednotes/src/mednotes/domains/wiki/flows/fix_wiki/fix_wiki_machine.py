"""Flat StateChart for `/mednotes:fix-wiki`.

The first fix-wiki StateChart is intentionally flat: every operational lane is a
leaf state, and `category_for_state()` derives the public category. That keeps
diagnosis, blockers, agent handoff, link routing and final validation observable
without introducing generic container states as a second source of truth.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator
from statemachine import StateChart
from statemachine.states import States

from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkWorkflowRunEffectPayload,
    RelatedNotesExportEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
    WaitExternalEffectPayload,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    HumanDecisionOption,
    RejectedAutomation,
    WorkflowDecision,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_states import (
    FIX_WIKI_DIAGNOSIS_PRIORITY,
    FIX_WIKI_WORKFLOW,
    FixWikiDiagnosisLane,
    FixWikiState,
    reason_for_state,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_states import (
    category_for_state as fix_wiki_category_for_state,
)
from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.state_machine import WorkflowStateCategory
from mednotes.kernel.workflow import HumanDecisionPacket


class FixWikiEvent(ContractModel):
    """Base event for fix-wiki facts accepted by the StateChart."""

    workflow: str = FIX_WIKI_WORKFLOW
    run_id: str = Field(min_length=1)
    current_state: str = Field(min_length=1)
    audit_evidence: JsonObject = Field(default_factory=dict)

    @field_validator("workflow")
    @classmethod
    def _workflow_must_be_fix_wiki(cls, value: str) -> str:
        if value != FIX_WIKI_WORKFLOW:
            raise ValueError(f"fix-wiki event workflow must be {FIX_WIKI_WORKFLOW}")
        return value


def _event_name(event: FixWikiEvent) -> str:
    """Return the concrete Literal discriminator declared by each event class."""

    name = getattr(event, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("fix-wiki events must declare a name discriminator")
    return name


class DiagnosisQueueEvent(FixWikiEvent):
    """Canonical diagnosis queue; concrete events supply the trigger name."""

    pending_lanes: list[FixWikiDiagnosisLane] = Field(min_length=1)
    selected_lane: FixWikiDiagnosisLane
    style_rewrite_effect: WorkflowEffect | None = None

    @field_validator("pending_lanes")
    @classmethod
    def _reject_duplicate_lanes(cls, value: list[FixWikiDiagnosisLane]) -> list[FixWikiDiagnosisLane]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate diagnosis lanes are not allowed")
        expected_order = sorted(value, key=FIX_WIKI_DIAGNOSIS_PRIORITY.index)
        if list(value) != expected_order:
            raise ValueError("diagnosis lanes must follow FIX_WIKI_DIAGNOSIS_PRIORITY")
        return value

    @model_validator(mode="after")
    def _selected_lane_must_be_priority_head(self) -> DiagnosisQueueEvent:
        if self.selected_lane not in self.pending_lanes:
            raise ValueError("selected_lane must be present in pending_lanes")
        expected = self.pending_lanes[0]
        if self.selected_lane != expected:
            raise ValueError("selected_lane must match the highest-priority pending lane")
        if self.style_rewrite_effect is not None:
            if self.selected_lane != FixWikiDiagnosisLane.STYLE_REWRITE:
                raise ValueError("style_rewrite_effect is only valid for the style rewrite lane")
            if self.style_rewrite_effect.kind != WorkflowEffectKind.CALL_SPECIALIST_MODEL:
                raise ValueError("style_rewrite_effect must be a specialist model effect")
            if self.style_rewrite_effect.origin_state != FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value:
                raise ValueError("style_rewrite_effect origin_state must match style_rewrite.specialist_requested")
        return self


class DiagnosisProducedEvent(DiagnosisQueueEvent):
    """Typed diagnosis fan-out; guards must inspect only `selected_lane`."""

    name: Literal["diagnosis_produced"] = "diagnosis_produced"


class FixWikiRuntimeObservation(ContractModel):
    """Runtime facts normalized at the boundary; StateChart guards choose state."""

    failed: bool = False
    failed_reason_code: str = ""
    vault_guard_required: bool = False
    environment_windows_path_or_venv_blocked: bool = False
    next_action: str = ""
    human_decision_required: bool = False
    external_wait_reason_code: str = ""
    related_notes_waiting_external: bool = False
    vocabulary_semantic_ingestion_pending: bool = False
    vocabulary_eval_needs_review: bool = False
    atomicity_split_required: bool = False
    merge_review_required: bool = False
    graph_review_required: bool = False
    graph_blocker_count: int = Field(default=0, ge=0)
    graph_error_count: int = Field(default=0, ge=0)
    related_notes_blocked: bool = False
    linker_blocked: bool = False
    taxonomy_action_required: bool = False
    specialist_model_waiting_agent: bool = False
    requires_llm_rewrite_count: int = Field(default=0, ge=0)
    effective_apply: bool = False
    warning_count: int = Field(default=0, ge=0)
    style_rewrite_effect: WorkflowEffect | None = None
    link_subworkflow_required: bool = False
    link_effect: WorkflowEffect | None = None
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @model_validator(mode="after")
    def _style_effect_matches_observed_lane(self) -> FixWikiRuntimeObservation:
        if self.style_rewrite_effect is not None:
            if not self.specialist_model_waiting_agent:
                raise ValueError("style_rewrite_effect requires specialist_model_waiting_agent")
            if self.style_rewrite_effect.kind != WorkflowEffectKind.CALL_SPECIALIST_MODEL:
                raise ValueError("style_rewrite_effect must be a specialist model effect")
            if self.style_rewrite_effect.origin_state != FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value:
                raise ValueError("style_rewrite_effect origin_state must match style_rewrite.specialist_requested")
        if self.link_effect is not None and not self.link_subworkflow_required:
            raise ValueError("link_effect requires link_subworkflow_required")
        if self.link_subworkflow_required and self.link_effect is None:
            raise ValueError("link_subworkflow_required requires link_effect")
        if self.link_effect is not None:
            if self.link_effect.kind != WorkflowEffectKind.RUN_SUBWORKFLOW:
                raise ValueError("link_effect must be a run_subworkflow effect")
            if self.link_effect.target != "/mednotes:link":
                raise ValueError("link_effect target must be /mednotes:link")
            if self.link_effect.origin_state != FixWikiState.LINK_RUN_REQUESTED.value:
                raise ValueError("link_effect origin_state must match link.run_requested")
        return self


class RuntimeObservedEvent(FixWikiEvent):
    """Single adapter event; StateChart guards own priority and leaf selection."""

    name: Literal["runtime_observed"] = "runtime_observed"
    observation: FixWikiRuntimeObservation


class DiagnosisCleanEvent(FixWikiEvent):
    """Clean diagnosis path; no pending lane is fabricated for final validation."""
    name: Literal["diagnosis_clean"] = "diagnosis_clean"


class BlockerRetryRequestedEvent(FixWikiEvent):
    """Recoverable blocker retry; the next step is a fresh diagnosis."""
    name: Literal["blocker_retry_requested"] = "blocker_retry_requested"
    reason_code: str = "retry_requested"


class SetupBootstrapReadyEvent(FixWikiEvent):
    name: Literal["setup_bootstrap_ready"] = "setup_bootstrap_ready"


class SetupBootstrapBlockedEvent(FixWikiEvent):
    name: Literal["setup_bootstrap_blocked"] = "setup_bootstrap_blocked"


class VaultGuardReadyEvent(FixWikiEvent):
    name: Literal["vault_guard_ready"] = "vault_guard_ready"


class VaultGuardDecisionApprovedEvent(FixWikiEvent):
    name: Literal["vault_guard_decision_approved"] = "vault_guard_decision_approved"


class VaultGuardBlockedEvent(FixWikiEvent):
    name: Literal["vault_guard_blocked"] = "vault_guard_blocked"


class DeterministicRepairsAppliedEvent(FixWikiEvent):
    name: Literal["deterministic_repairs_applied"] = "deterministic_repairs_applied"


class DeterministicRepairsBlockedEvent(FixWikiEvent):
    name: Literal["deterministic_repairs_blocked"] = "deterministic_repairs_blocked"


class StyleRewriteSpecialistCompletedEvent(FixWikiEvent):
    name: Literal["style_rewrite_specialist_completed"] = "style_rewrite_specialist_completed"


class StyleRewriteCapacityWaitEvent(FixWikiEvent):
    name: Literal["style_rewrite_capacity_wait"] = "style_rewrite_capacity_wait"


class StyleRewriteReviewRequiredEvent(FixWikiEvent):
    name: Literal["style_rewrite_review_required"] = "style_rewrite_review_required"


class StyleRewriteReviewApprovedEvent(FixWikiEvent):
    name: Literal["style_rewrite_review_approved"] = "style_rewrite_review_approved"


class StyleRewriteAppliedEvent(FixWikiEvent):
    name: Literal["style_rewrite_applied"] = "style_rewrite_applied"


class StyleRewriteBlockedEvent(FixWikiEvent):
    name: Literal["style_rewrite_blocked"] = "style_rewrite_blocked"


class TaxonomyDecisionRequiredEvent(FixWikiEvent):
    name: Literal["taxonomy_decision_required"] = "taxonomy_decision_required"


class TaxonomyDecisionApprovedEvent(FixWikiEvent):
    name: Literal["taxonomy_decision_approved"] = "taxonomy_decision_approved"


class TaxonomyAppliedEvent(FixWikiEvent):
    name: Literal["taxonomy_applied"] = "taxonomy_applied"


class TaxonomyBlockedEvent(FixWikiEvent):
    name: Literal["taxonomy_blocked"] = "taxonomy_blocked"


class VocabularyCuratorCompletedEvent(FixWikiEvent):
    name: Literal["vocabulary_curator_completed"] = "vocabulary_curator_completed"


class VocabularyEvalNeedsReviewEvent(FixWikiEvent):
    name: Literal["vocabulary_eval_needs_review"] = "vocabulary_eval_needs_review"


class VocabularyEvalPassedEvent(FixWikiEvent):
    name: Literal["vocabulary_eval_passed"] = "vocabulary_eval_passed"


class VocabularyAppliedEvent(FixWikiEvent):
    name: Literal["vocabulary_applied"] = "vocabulary_applied"


class VocabularyIntegrityFailedEvent(FixWikiEvent):
    name: Literal["vocabulary_integrity_failed"] = "vocabulary_integrity_failed"


class AtomicitySplitAppliedEvent(FixWikiEvent):
    name: Literal["atomicity_split_applied"] = "atomicity_split_applied"


class AtomicitySplitBlockedEvent(FixWikiEvent):
    name: Literal["atomicity_split_blocked"] = "atomicity_split_blocked"


class MergeAppliedEvent(FixWikiEvent):
    name: Literal["merge_applied"] = "merge_applied"


class MergeBlockedEvent(FixWikiEvent):
    name: Literal["merge_blocked"] = "merge_blocked"


class RelatedNotesExportCompletedEvent(FixWikiEvent):
    name: Literal["related_notes_export_completed"] = "related_notes_export_completed"


class RelatedNotesQuotaWaitEvent(FixWikiEvent):
    name: Literal["related_notes_quota_wait"] = "related_notes_quota_wait"
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload = Field(
        default_factory=RelatedNotesRecoveryStateEffectPayload
    )

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_related_notes_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload:
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class RelatedNotesObsidianNotReadyEvent(FixWikiEvent):
    name: Literal["related_notes_obsidian_not_ready"] = "related_notes_obsidian_not_ready"


class RelatedNotesBlockedEvent(FixWikiEvent):
    name: Literal["related_notes_blocked"] = "related_notes_blocked"


class LinkCompletedEvent(FixWikiEvent):
    name: Literal["link_completed"] = "link_completed"


class LinkGraphBlockedEvent(FixWikiEvent):
    name: Literal["link_graph_blocked"] = "link_graph_blocked"


class LinkerBlockedEvent(FixWikiEvent):
    name: Literal["linker_blocked"] = "linker_blocked"


class FinalValidationPassedEvent(FixWikiEvent):
    name: Literal["final_validation_passed"] = "final_validation_passed"


class PreviewReadyEvent(FixWikiEvent):
    name: Literal["preview_ready"] = "preview_ready"


class FinalValidationWarningsEvent(FixWikiEvent):
    name: Literal["final_validation_warnings"] = "final_validation_warnings"


class FinalValidationFoundMoreWorkEvent(DiagnosisQueueEvent):
    name: Literal["final_validation_found_more_work"] = "final_validation_found_more_work"


class FinalValidationFailedEvent(FixWikiEvent):
    name: Literal["final_validation_failed"] = "final_validation_failed"


class RollbackCompletedEvent(FixWikiEvent):
    name: Literal["rollback_completed"] = "rollback_completed"


class RollbackFailedEvent(FixWikiEvent):
    name: Literal["rollback_failed"] = "rollback_failed"


FixWikiBoundaryEvent = Annotated[
    DiagnosisProducedEvent
    | RuntimeObservedEvent
    | DiagnosisCleanEvent
    | BlockerRetryRequestedEvent
    | SetupBootstrapReadyEvent
    | SetupBootstrapBlockedEvent
    | VaultGuardReadyEvent
    | VaultGuardDecisionApprovedEvent
    | VaultGuardBlockedEvent
    | DeterministicRepairsAppliedEvent
    | DeterministicRepairsBlockedEvent
    | StyleRewriteSpecialistCompletedEvent
    | StyleRewriteCapacityWaitEvent
    | StyleRewriteReviewRequiredEvent
    | StyleRewriteReviewApprovedEvent
    | StyleRewriteAppliedEvent
    | StyleRewriteBlockedEvent
    | TaxonomyDecisionRequiredEvent
    | TaxonomyDecisionApprovedEvent
    | TaxonomyAppliedEvent
    | TaxonomyBlockedEvent
    | VocabularyCuratorCompletedEvent
    | VocabularyEvalNeedsReviewEvent
    | VocabularyEvalPassedEvent
    | VocabularyAppliedEvent
    | VocabularyIntegrityFailedEvent
    | AtomicitySplitAppliedEvent
    | AtomicitySplitBlockedEvent
    | MergeAppliedEvent
    | MergeBlockedEvent
    | RelatedNotesExportCompletedEvent
    | RelatedNotesQuotaWaitEvent
    | RelatedNotesObsidianNotReadyEvent
    | RelatedNotesBlockedEvent
    | LinkCompletedEvent
    | LinkGraphBlockedEvent
    | LinkerBlockedEvent
    | FinalValidationPassedEvent
    | PreviewReadyEvent
    | FinalValidationWarningsEvent
    | FinalValidationFoundMoreWorkEvent
    | FinalValidationFailedEvent
    | RollbackCompletedEvent
    | RollbackFailedEvent,
    Field(discriminator="name"),
]
FixWikiBoundaryEventAdapter = TypeAdapter(FixWikiBoundaryEvent)

FIX_WIKI_BOUNDARY_EVENT_NAMES = frozenset(
    FixWikiBoundaryEventAdapter.json_schema()["discriminator"]["mapping"]
)


class FixWikiMachine(StateChart[WorkflowModel]):
    """Pure domain StateChart; callbacks only return typed transition results."""

    allow_event_without_transition = False
    catch_errors_as_events = False
    states = States.from_enum(
        FixWikiState,
        initial=FixWikiState.DIAGNOSIS_RUNNING,
        final={
            FixWikiState.AGENT_TOOL_CONTRACT_VIOLATION,
            FixWikiState.CONTRACT_GAP_MISSING_NEXT_ACTION,
            FixWikiState.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            FixWikiState.DETERMINISTIC_REPAIRS_FAILED,
            FixWikiState.VOCABULARY_SQLITE_INTEGRITY_FAILED,
            FixWikiState.ROLLBACK_PERFORMED,
            FixWikiState.FINAL_VALIDATION_FAILED,
            FixWikiState.FAILED,
            FixWikiState.PREVIEW_READY,
            FixWikiState.COMPLETED,
            FixWikiState.COMPLETED_WITH_WARNINGS,
        },
        use_enum_instance=False,
    )

    diagnosis_clean = states.DIAGNOSIS_RUNNING.to(
        states.FINAL_VALIDATION_RUNNING,
        on="_on_enter_final_validation",
    )

    diagnosis_produced = (
        states.DIAGNOSIS_RUNNING.to(
            states.ENVIRONMENT_PATHS_MISSING,
            cond="_selected_environment_paths_missing",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ENVIRONMENT_WIKI_DIR_MISSING,
            cond="_selected_environment_wiki_dir_missing",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
            cond="_selected_environment_windows_path_or_venv_blocked",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.VAULT_GUARD_DECISION_REQUIRED,
            cond="_selected_vault_guard_decision_required",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
            cond="_selected_subagent_plan_attestation_required",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.SUBAGENT_PLAN_ATTESTATION_INVALID,
            cond="_selected_subagent_plan_attestation_invalid",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.AGENT_TOOL_CONTRACT_VIOLATION,
            cond="_selected_agent_tool_contract_violation",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.DETERMINISTIC_REPAIRS_RUNNING,
            cond="_selected_deterministic_repairs",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.STYLE_REWRITE_SPECIALIST_REQUESTED,
            cond="_selected_style_rewrite",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.TAXONOMY_DECISION_REQUIRED,
            cond="_selected_taxonomy",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.VOCABULARY_SEMANTIC_INGESTION_PENDING,
            cond="_selected_vocabulary_semantic_ingestion_pending",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.VOCABULARY_CURATOR_RUNNING,
            cond="_selected_vocabulary",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ATOMICITY_SPLIT_RUNNING,
            cond="_selected_atomicity_split",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.MERGE_RUNNING,
            cond="_selected_merge",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.RELATED_NOTES_EXPORT_RUNNING,
            cond="_selected_related_notes",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.LINK_RUN_REQUESTED,
            cond="_selected_link",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.CONTRACT_GAP_MISSING_NEXT_ACTION,
            cond="_selected_contract_gap_missing_next_action",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            cond="_selected_contract_gap_missing_error_context",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ROLLBACK_RUNNING,
            cond="_selected_rollback",
            on="_on_diagnosis_produced",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.FINAL_VALIDATION_RUNNING,
            cond="_selected_final_validation",
            on="_on_diagnosis_produced",
        )
    )

    runtime_observed = (
        states.DIAGNOSIS_RUNNING.to(
            states.VAULT_GUARD_DECISION_REQUIRED,
            cond="_observed_vault_guard_required",
            on="_on_blocked",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
            cond="_observed_environment_blocked",
            on="_on_blocked",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.FAILED,
            cond="_observed_failed",
            on="_on_failed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.RELATED_NOTES_QUOTA_WAIT,
            cond="_observed_related_notes_quota_wait",
            on="_on_wait_external",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.STYLE_REWRITE_CAPACITY_WAIT,
            cond="_observed_style_rewrite_capacity_wait",
            on="_on_wait_external",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.VOCABULARY_SEMANTIC_INGESTION_PENDING,
            cond="_observed_vocabulary_semantic_ingestion_pending",
            on="_on_runtime_observed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.VOCABULARY_EVAL_NEEDS_REVIEW,
            cond="_observed_vocabulary_eval_needs_review",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.ATOMICITY_SPLIT_REVIEW_REQUIRED,
            cond="_observed_atomicity_split_required",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.MERGE_REVIEW_REQUIRED,
            cond="_observed_merge_review_required",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.LINK_GRAPH_REVIEW_REQUIRED,
            cond="_observed_graph_review_required",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.LINK_GRAPH_BLOCKED,
            cond="_observed_graph_blocked",
            on="_on_blocked",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.TAXONOMY_DECISION_REQUIRED,
            cond="_observed_taxonomy_action_required",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.RELATED_NOTES_BLOCKED,
            cond="_observed_related_notes_blocked",
            on="_on_blocked",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.LINKER_BLOCKED,
            cond="_observed_linker_blocked",
            on="_on_blocked",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.STYLE_REWRITE_SPECIALIST_REQUESTED,
            cond="_observed_specialist_model_waiting_agent",
            on="_on_runtime_observed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.STYLE_REWRITE_REVIEW_REQUIRED,
            cond="_observed_style_rewrite_review_required",
            on="_on_human_review",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
            cond="_observed_human_decision_contract_gap",
            on="_on_failed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.LINK_RUN_REQUESTED,
            cond="_observed_link_subworkflow_required",
            on="_on_runtime_observed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.PREVIEW_READY,
            cond="_observed_preview_ready",
            on="_on_completed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.COMPLETED_WITH_WARNINGS,
            cond="_observed_completed_with_warnings",
            on="_on_completed",
        )
        | states.DIAGNOSIS_RUNNING.to(
            states.COMPLETED,
            cond="_observed_completed",
            on="_on_completed",
        )
    )

    blocker_retry_requested = (
        states.ENVIRONMENT_PATHS_MISSING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ENVIRONMENT_WIKI_DIR_MISSING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.to(
            states.DIAGNOSIS_RUNNING,
            on="_on_resume_diagnosis",
        )
        | states.VAULT_GUARD_DECISION_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.SUBAGENT_PLAN_ATTESTATION_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.SUBAGENT_PLAN_ATTESTATION_INVALID.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.STYLE_REWRITE_CAPACITY_WAIT.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.STYLE_REWRITE_REVIEW_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.TAXONOMY_DECISION_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.VOCABULARY_SEMANTIC_INGESTION_PENDING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.VOCABULARY_EVAL_NEEDS_REVIEW.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ATOMICITY_SPLIT_REVIEW_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.RELATED_NOTES_QUOTA_WAIT.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.RELATED_NOTES_OBSIDIAN_NOT_READY.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.RELATED_NOTES_BLOCKED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.LINK_GRAPH_REVIEW_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.LINK_GRAPH_BLOCKED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.LINKER_BLOCKED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.MERGE_REVIEW_REQUIRED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ROLLBACK_FAILED.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
    )

    setup_bootstrap_ready = (
        states.ENVIRONMENT_PATHS_MISSING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ENVIRONMENT_WIKI_DIR_MISSING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
        | states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.to(
            states.DIAGNOSIS_RUNNING,
            on="_on_resume_diagnosis",
        )
    )
    setup_bootstrap_blocked = (
        states.ENVIRONMENT_PATHS_MISSING.to(states.FAILED, on="_on_failed")
        | states.ENVIRONMENT_WIKI_DIR_MISSING.to(states.FAILED, on="_on_failed")
        | states.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.to(states.FAILED, on="_on_failed")
    )
    vault_guard_decision_approved = states.VAULT_GUARD_DECISION_REQUIRED.to(
        states.VAULT_GUARD_RUNNING,
        on="_on_vault_guard_run",
    )
    vault_guard_ready = states.VAULT_GUARD_RUNNING.to(states.DIAGNOSIS_RUNNING, on="_on_resume_diagnosis")
    vault_guard_blocked = states.VAULT_GUARD_RUNNING.to(
        states.VAULT_GUARD_DECISION_REQUIRED,
        on="_on_blocked",
    )
    deterministic_repairs_applied = states.DETERMINISTIC_REPAIRS_RUNNING.to(
        states.LINK_RUN_REQUESTED,
        on="_on_route_link",
    )
    deterministic_repairs_blocked = states.DETERMINISTIC_REPAIRS_RUNNING.to(
        states.DETERMINISTIC_REPAIRS_FAILED,
        on="_on_failed",
    )
    style_rewrite_specialist_completed = states.STYLE_REWRITE_SPECIALIST_REQUESTED.to(
        states.STYLE_REWRITE_REVIEW_REQUIRED,
        on="_on_human_review",
    )
    style_rewrite_capacity_wait = states.STYLE_REWRITE_SPECIALIST_REQUESTED.to(
        states.STYLE_REWRITE_CAPACITY_WAIT,
        on="_on_wait_external",
    )
    style_rewrite_review_required = states.STYLE_REWRITE_SPECIALIST_REQUESTED.to(
        states.STYLE_REWRITE_REVIEW_REQUIRED,
        on="_on_human_review",
    )
    style_rewrite_review_approved = states.STYLE_REWRITE_REVIEW_REQUIRED.to(
        states.STYLE_REWRITE_APPLY_RUNNING,
        on="_on_style_rewrite_apply",
    )
    style_rewrite_applied = states.STYLE_REWRITE_APPLY_RUNNING.to(states.LINK_RUN_REQUESTED, on="_on_route_link")
    style_rewrite_blocked = states.STYLE_REWRITE_APPLY_RUNNING.to(states.FAILED, on="_on_failed")
    taxonomy_decision_required = states.TAXONOMY_APPLY_RUNNING.to(
        states.TAXONOMY_DECISION_REQUIRED,
        on="_on_human_review",
    )
    taxonomy_decision_approved = states.TAXONOMY_DECISION_REQUIRED.to(
        states.TAXONOMY_APPLY_RUNNING,
        on="_on_taxonomy_apply",
    )
    taxonomy_applied = states.TAXONOMY_APPLY_RUNNING.to(states.LINK_RUN_REQUESTED, on="_on_route_link")
    taxonomy_blocked = states.TAXONOMY_APPLY_RUNNING.to(
        states.TAXONOMY_DECISION_REQUIRED,
        on="_on_human_review",
    )
    vocabulary_curator_completed = (
        states.VOCABULARY_CURATOR_RUNNING.to(
            states.VOCABULARY_EVAL_RUNNING,
            on="_on_vocabulary_eval",
        )
        | states.VOCABULARY_SEMANTIC_INGESTION_PENDING.to(
            states.VOCABULARY_EVAL_RUNNING,
            on="_on_vocabulary_eval",
        )
    )
    vocabulary_eval_needs_review = states.VOCABULARY_EVAL_RUNNING.to(
        states.VOCABULARY_EVAL_NEEDS_REVIEW,
        on="_on_human_review",
    )
    vocabulary_eval_passed = states.VOCABULARY_EVAL_RUNNING.to(
        states.VOCABULARY_APPLY_RUNNING,
        on="_on_vocabulary_apply",
    )
    vocabulary_applied = states.VOCABULARY_APPLY_RUNNING.to(states.LINK_RUN_REQUESTED, on="_on_route_link")
    vocabulary_integrity_failed = states.VOCABULARY_APPLY_RUNNING.to(
        states.VOCABULARY_SQLITE_INTEGRITY_FAILED,
        on="_on_failed",
    )
    atomicity_split_applied = states.ATOMICITY_SPLIT_RUNNING.to(states.LINK_RUN_REQUESTED, on="_on_route_link")
    atomicity_split_blocked = states.ATOMICITY_SPLIT_RUNNING.to(
        states.ATOMICITY_SPLIT_REVIEW_REQUIRED,
        on="_on_human_review",
    )
    merge_applied = states.MERGE_RUNNING.to(states.LINK_RUN_REQUESTED, on="_on_route_link")
    merge_blocked = states.MERGE_RUNNING.to(states.MERGE_REVIEW_REQUIRED, on="_on_human_review")
    related_notes_export_completed = states.RELATED_NOTES_EXPORT_RUNNING.to(
        states.LINK_RUN_REQUESTED,
        on="_on_route_link",
    )
    related_notes_quota_wait = (
        states.RELATED_NOTES_EXPORT_RUNNING.to(
            states.RELATED_NOTES_QUOTA_WAIT,
            on="_on_wait_external",
        )
        | states.LINK_RUN_REQUESTED.to(
            states.RELATED_NOTES_QUOTA_WAIT,
            on="_on_wait_external",
        )
    )
    related_notes_obsidian_not_ready = states.RELATED_NOTES_EXPORT_RUNNING.to(
        states.RELATED_NOTES_OBSIDIAN_NOT_READY,
        on="_on_blocked",
    )
    related_notes_blocked = states.RELATED_NOTES_EXPORT_RUNNING.to(
        states.RELATED_NOTES_BLOCKED,
        on="_on_blocked",
    )
    link_completed = states.LINK_RUN_REQUESTED.to(states.FINAL_VALIDATION_RUNNING, on="_on_enter_final_validation")
    link_graph_blocked = states.LINK_RUN_REQUESTED.to(states.LINK_GRAPH_BLOCKED, on="_on_blocked")
    linker_blocked = states.LINK_RUN_REQUESTED.to(states.LINKER_BLOCKED, on="_on_blocked")
    preview_ready = states.FINAL_VALIDATION_RUNNING.to(states.PREVIEW_READY, on="_on_completed")
    final_validation_passed = states.FINAL_VALIDATION_RUNNING.to(states.COMPLETED, on="_on_completed")
    final_validation_warnings = states.FINAL_VALIDATION_RUNNING.to(
        states.COMPLETED_WITH_WARNINGS,
        on="_on_completed",
    )
    final_validation_found_more_work = states.FINAL_VALIDATION_RUNNING.to(
        states.DIAGNOSIS_RUNNING,
        on="_on_resume_diagnosis",
    )
    final_validation_failed = states.FINAL_VALIDATION_RUNNING.to(states.FINAL_VALIDATION_FAILED, on="_on_failed")
    rollback_completed = states.ROLLBACK_RUNNING.to(states.ROLLBACK_PERFORMED, on="_on_failed")
    rollback_failed = states.ROLLBACK_RUNNING.to(states.ROLLBACK_FAILED, on="_on_blocked")

    def category_for_state(self, state: str) -> WorkflowStateCategory:
        return fix_wiki_category_for_state(state)

    def _selected_environment_paths_missing(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.ENVIRONMENT_PATHS_MISSING

    def _selected_environment_wiki_dir_missing(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.ENVIRONMENT_WIKI_DIR_MISSING

    def _selected_environment_windows_path_or_venv_blocked(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED

    def _selected_vault_guard_decision_required(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.VAULT_GUARD_DECISION_REQUIRED

    def _selected_subagent_plan_attestation_required(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.SUBAGENT_PLAN_ATTESTATION_REQUIRED

    def _selected_subagent_plan_attestation_invalid(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.SUBAGENT_PLAN_ATTESTATION_INVALID

    def _selected_agent_tool_contract_violation(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.AGENT_TOOL_CONTRACT_VIOLATION

    def _selected_deterministic_repairs(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.DETERMINISTIC_REPAIRS

    def _selected_style_rewrite(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.STYLE_REWRITE

    def _selected_taxonomy(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.TAXONOMY

    def _selected_vocabulary_semantic_ingestion_pending(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.VOCABULARY_SEMANTIC_INGESTION_PENDING

    def _selected_vocabulary(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.VOCABULARY

    def _selected_atomicity_split(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.ATOMICITY_SPLIT

    def _selected_merge(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.MERGE

    def _selected_related_notes(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.RELATED_NOTES

    def _selected_link(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.LINK

    def _selected_contract_gap_missing_next_action(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.CONTRACT_GAP_MISSING_NEXT_ACTION

    def _selected_contract_gap_missing_error_context(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.CONTRACT_GAP_MISSING_ERROR_CONTEXT

    def _selected_rollback(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.ROLLBACK

    def _selected_final_validation(self, workflow_event: DiagnosisProducedEvent) -> bool:
        return workflow_event.selected_lane == FixWikiDiagnosisLane.FINAL_VALIDATION

    def _observed_vault_guard_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.vault_guard_required

    def _observed_environment_blocked(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.environment_windows_path_or_venv_blocked

    def _observed_related_notes_quota_wait(self, workflow_event: RuntimeObservedEvent) -> bool:
        return bool(
            workflow_event.observation.external_wait_reason_code
            and workflow_event.observation.related_notes_recovery_state.status
        ) or workflow_event.observation.related_notes_waiting_external

    def _observed_style_rewrite_capacity_wait(self, workflow_event: RuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return bool(observation.external_wait_reason_code) and not self._observed_related_notes_quota_wait(
            workflow_event
        )

    def _observed_vocabulary_semantic_ingestion_pending(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.vocabulary_semantic_ingestion_pending

    def _observed_vocabulary_eval_needs_review(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.vocabulary_eval_needs_review

    def _observed_atomicity_split_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.atomicity_split_required

    def _observed_merge_review_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.merge_review_required

    def _observed_graph_review_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.graph_review_required and (observation.graph_blocker_count > 0 or observation.graph_error_count > 0)

    def _observed_graph_blocked(self, workflow_event: RuntimeObservedEvent) -> bool:
        observation = workflow_event.observation
        return observation.graph_blocker_count > 0 or observation.graph_error_count > 0

    def _observed_related_notes_blocked(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.related_notes_blocked

    def _observed_linker_blocked(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.linker_blocked

    def _observed_taxonomy_action_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.taxonomy_action_required

    def _observed_specialist_model_waiting_agent(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.specialist_model_waiting_agent

    def _observed_style_rewrite_review_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.requires_llm_rewrite_count > 0

    def _observed_human_decision_contract_gap(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.human_decision_required

    def _observed_link_subworkflow_required(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.link_subworkflow_required

    def _observed_failed(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.failed

    def _observed_preview_ready(self, workflow_event: RuntimeObservedEvent) -> bool:
        return not workflow_event.observation.effective_apply

    def _observed_completed_with_warnings(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.warning_count > 0

    def _observed_completed(self, workflow_event: RuntimeObservedEvent) -> bool:
        return workflow_event.observation.effective_apply

    def _on_diagnosis_produced(
        self,
        workflow_event: DiagnosisProducedEvent,
        target: object,
    ) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        match to_state:
            case (
                FixWikiState.ENVIRONMENT_PATHS_MISSING
                | FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING
                | FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
            ):
                return _setup_recovery_transition(workflow_event, to_state)
            case FixWikiState.VAULT_GUARD_DECISION_REQUIRED:
                return _blocked_transition(workflow_event, to_state)
            case (
                FixWikiState.SUBAGENT_PLAN_ATTESTATION_REQUIRED
                | FixWikiState.SUBAGENT_PLAN_ATTESTATION_INVALID
            ):
                return _human_transition(workflow_event, to_state)
            case FixWikiState.AGENT_TOOL_CONTRACT_VIOLATION:
                return _failed_transition(workflow_event, to_state)
            case FixWikiState.DETERMINISTIC_REPAIRS_RUNNING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.deterministic_repairs")],
                )
            case FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED:
                return _transition(workflow_event, to_state, effects=[_style_rewrite_effect(workflow_event, to_state)])
            case FixWikiState.TAXONOMY_DECISION_REQUIRED:
                return _human_transition(workflow_event, to_state)
            case (
                FixWikiState.VOCABULARY_CURATOR_RUNNING
                | FixWikiState.VOCABULARY_SEMANTIC_INGESTION_PENDING
            ):
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.vocabulary_curator")],
                )
            case FixWikiState.ATOMICITY_SPLIT_RUNNING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.atomicity_split")],
                )
            case FixWikiState.MERGE_RUNNING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.note_merge")],
                )
            case FixWikiState.RELATED_NOTES_EXPORT_RUNNING:
                return _transition(workflow_event, to_state, effects=[_related_notes_effect(workflow_event, to_state)])
            case FixWikiState.LINK_RUN_REQUESTED:
                return _transition(workflow_event, to_state, effects=[_link_effect(workflow_event, to_state)])
            case (
                FixWikiState.CONTRACT_GAP_MISSING_NEXT_ACTION
                | FixWikiState.CONTRACT_GAP_MISSING_ERROR_CONTEXT
            ):
                return _failed_transition(workflow_event, to_state)
            case FixWikiState.ROLLBACK_RUNNING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.rollback")],
                )
            case FixWikiState.FINAL_VALIDATION_RUNNING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_final_validation_effect(workflow_event, to_state)],
                )
            case _:
                raise AssertionError(f"unexpected diagnosis target: {to_state}")

    def _on_runtime_observed(self, workflow_event: RuntimeObservedEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        match to_state:
            case FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED:
                return _transition(workflow_event, to_state, effects=[_style_rewrite_effect(workflow_event, to_state)])
            case FixWikiState.VOCABULARY_SEMANTIC_INGESTION_PENDING:
                return _transition(
                    workflow_event,
                    to_state,
                    effects=[_domain_effect(workflow_event, to_state, target="fix_wiki.vocabulary_curator")],
                )
            case FixWikiState.LINK_RUN_REQUESTED:
                return _transition(workflow_event, to_state, effects=[_link_effect(workflow_event, to_state)])
            case _:
                return _transition(workflow_event, to_state)

    def _on_resume_diagnosis(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(workflow_event, FixWikiState.DIAGNOSIS_RUNNING)

    def _on_route_link(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.LINK_RUN_REQUESTED,
            effects=[_link_effect(workflow_event, FixWikiState.LINK_RUN_REQUESTED)],
        )

    def _on_vault_guard_run(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.VAULT_GUARD_RUNNING,
            effects=[_vault_guard_effect(workflow_event, FixWikiState.VAULT_GUARD_RUNNING)],
        )

    def _on_enter_final_validation(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.FINAL_VALIDATION_RUNNING,
            effects=[_final_validation_effect(workflow_event, FixWikiState.FINAL_VALIDATION_RUNNING)],
        )

    def _on_vocabulary_eval(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.VOCABULARY_EVAL_RUNNING,
            effects=[
                _domain_effect(
                    workflow_event,
                    FixWikiState.VOCABULARY_EVAL_RUNNING,
                    target="fix_wiki.vocabulary_eval",
                )
            ],
        )

    def _on_style_rewrite_apply(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.STYLE_REWRITE_APPLY_RUNNING,
            effects=[
                _domain_effect(
                    workflow_event,
                    FixWikiState.STYLE_REWRITE_APPLY_RUNNING,
                    target="fix_wiki.style_rewrite_apply",
                )
            ],
        )

    def _on_taxonomy_apply(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.TAXONOMY_APPLY_RUNNING,
            effects=[_domain_effect(workflow_event, FixWikiState.TAXONOMY_APPLY_RUNNING, target="fix_wiki.taxonomy")],
        )

    def _on_vocabulary_apply(self, workflow_event: FixWikiEvent) -> WorkflowTransitionResult:
        return _transition(
            workflow_event,
            FixWikiState.VOCABULARY_APPLY_RUNNING,
            effects=[
                _domain_effect(
                    workflow_event,
                    FixWikiState.VOCABULARY_APPLY_RUNNING,
                    target="fix_wiki.vocabulary_apply",
                )
            ],
        )

    def _on_wait_external(self, workflow_event: FixWikiEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(
            workflow_event,
            to_state,
            effects=[_wait_external_effect(workflow_event, to_state)],
            resume_action="Retomar pela rota oficial depois que a condição externa estiver resolvida.",
        )

    def _on_human_review(self, workflow_event: FixWikiEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _human_transition(workflow_event, to_state)

    def _on_blocked(self, workflow_event: FixWikiEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _blocked_transition(workflow_event, to_state)

    def _on_failed(self, workflow_event: FixWikiEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _failed_transition(workflow_event, to_state)

    def _on_completed(self, workflow_event: FixWikiEvent, target: object) -> WorkflowTransitionResult:
        to_state = _target_state(target)
        return _transition(workflow_event, to_state)


def _transition(
    workflow_event: FixWikiEvent,
    to_state: FixWikiState,
    *,
    reason_code: str | None = None,
    effects: list[WorkflowEffect] | None = None,
    decision: WorkflowDecision | None = None,
    human_decision_packet: HumanDecisionPacket | None = None,
    resume_action: str = "",
) -> WorkflowTransitionResult:
    trigger = _event_name(workflow_event)
    state_reason = _transition_reason_for_state(to_state)
    return WorkflowTransitionResult(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        from_state=workflow_event.current_state,
        to_state=to_state.value,
        trigger=trigger,
        reason_code=reason_code or state_reason,
        effects=list(effects or []),
        decision=decision,
        human_decision_packet=human_decision_packet,
        resume_action=resume_action,
    )


def _transition_reason_for_state(state: FixWikiState) -> str:
    """Record the reached leaf for running states and public state reason otherwise."""

    if fix_wiki_category_for_state(state) == WorkflowStateCategory.RUNNING:
        return state.value
    return reason_for_state(state).value


def _target_state(target: object) -> FixWikiState:
    """Read the python-statemachine transition target without touching IO."""

    value = getattr(target, "value", target)
    return FixWikiState(str(value))


def _blocked_transition(
    workflow_event: FixWikiEvent,
    to_state: FixWikiState,
) -> WorkflowTransitionResult:
    reason_code = reason_for_state(to_state).value
    return _transition(
        workflow_event,
        to_state,
        reason_code=reason_code,
        decision=_decision(kind="hard_block", phase=to_state.value, reason_code=reason_code),
    )


def _setup_recovery_transition(
    workflow_event: FixWikiEvent,
    to_state: FixWikiState,
) -> WorkflowTransitionResult:
    reason_code = reason_for_state(to_state).value
    resume_action = _setup_resume_action(to_state)
    return _transition(
        workflow_event,
        to_state,
        reason_code=reason_code,
        effects=[_setup_recovery_effect(workflow_event, to_state, reason_code=reason_code)],
        decision=_decision(
            kind="hard_block",
            phase=to_state.value,
            reason_code=reason_code,
            next_action=resume_action,
        ),
        resume_action=resume_action,
    )


def _failed_transition(
    workflow_event: FixWikiEvent,
    to_state: FixWikiState,
) -> WorkflowTransitionResult:
    reason_code = reason_for_state(to_state).value
    return _transition(
        workflow_event,
        to_state,
        reason_code=reason_code,
        decision=_decision(kind="failed", phase=to_state.value, reason_code=reason_code),
    )


def _human_transition(
    workflow_event: FixWikiEvent,
    to_state: FixWikiState,
) -> WorkflowTransitionResult:
    reason_code = reason_for_state(to_state).value
    decision = _decision(
        kind="ask_human",
        phase=to_state.value,
        reason_code=reason_code,
        next_action=_human_next_action(to_state),
    )
    packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
    effect = WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"fix-wiki-{to_state.value.replace('.', '-')}-human-decision",
        origin_state=to_state.value,
        kind=WorkflowEffectKind.ASK_HUMAN,
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


def _human_next_action(to_state: FixWikiState) -> str:
    """Keep human-decision actions tied to the reached operational leaf."""

    match to_state:
        case FixWikiState.STYLE_REWRITE_REVIEW_REQUIRED:
            return "Executar a reescrita semantica oficial antes de concluir."
        case FixWikiState.TAXONOMY_DECISION_REQUIRED:
            return "Resolver a acao de taxonomia pela rota oficial antes de concluir."
        case FixWikiState.ATOMICITY_SPLIT_REVIEW_REQUIRED:
            return "Resolver o split de atomicidade pela rota oficial."
        case FixWikiState.VOCABULARY_EVAL_NEEDS_REVIEW:
            return "Revisar a avaliacao do vocabulario e retomar pela rota oficial."
        case FixWikiState.LINK_GRAPH_REVIEW_REQUIRED:
            return "Revisar os bloqueios de grafo e retomar pela rota oficial."
        case FixWikiState.MERGE_REVIEW_REQUIRED:
            return "Revisar o merge de notas e retomar pela rota oficial."
        case (
            FixWikiState.SUBAGENT_PLAN_ATTESTATION_REQUIRED
            | FixWikiState.SUBAGENT_PLAN_ATTESTATION_INVALID
        ):
            return "Reemitir o plano de subagente pela rota oficial com atestacao valida."
        case _:
            return "Responder a decisao solicitada para continuar."


def _decision(
    *,
    kind: Literal["hard_block", "failed", "ask_human"],
    phase: str,
    reason_code: str,
    next_action: str = "Retomar pela rota oficial indicada pelo workflow.",
) -> WorkflowDecision:
    evidence = [
        DecisionEvidence(
            summary=f"fix-wiki StateChart reached {phase}.",
            technical_code=reason_code,
            source="fix_wiki_machine",
        )
    ]
    base: JsonObject = {
        "kind": kind,
        "phase": phase,
        "reason_code": reason_code,
        "public_summary": "O fix-wiki precisa parar nesta etapa.",
        "developer_summary": f"StateChart transition stopped at {phase}:{reason_code}.",
        "evidence": evidence,
        "next_action": next_action,
        "resume_action": next_action,
    }
    if kind == "ask_human":
        base.update(
            {
                "public_summary": "Preciso de uma decisão sua antes de continuar.",
                "human_decision_kind": reason_code,
                "recommended_option_id": "continue",
                "options": [
                    HumanDecisionOption(
                        id="continue",
                        label="Continuar",
                        description="Retoma pela rota oficial depois da confirmação.",
                    )
                ],
                "rejected_automations": _rejected_automations(reason_code),
            }
        )
    return WorkflowDecision(**base)


def _rejected_automations(reason_code: str) -> list[RejectedAutomation]:
    return [
        RejectedAutomation(kind="auto_fix", reason_code=reason_code, reason="Requer confirmação humana."),
        RejectedAutomation(
            kind="auto_defer",
            reason_code=reason_code,
            reason="Adiar sem decisão deixaria o fluxo ambíguo.",
        ),
        RejectedAutomation(
            kind="auto_plan",
            reason_code=reason_code,
            reason="Planejar sem escolha humana não resolve o bloqueio.",
        ),
    ]


def _payload(kind: str) -> JsonObject:
    return {"kind": kind}


def _domain_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState, *, target: str) -> WorkflowEffect:
    payload_kind = target.removeprefix("fix_wiki.")
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"fix-wiki-{origin_state.value.replace('.', '-')}",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target=target,
        payload=_payload(payload_kind),
        mutates_resources=True,
        rollback_declared=True,
        requires_receipt=False,
    )


def _setup_recovery_effect(
    workflow_event: FixWikiEvent,
    origin_state: FixWikiState,
    *,
    reason_code: str,
) -> WorkflowEffect:
    resume_action = _setup_resume_action(origin_state)
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"fix-wiki-{origin_state.value.replace('.', '-')}-setup",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:setup",
        payload={
            "kind": "setup_recovery",
            "reason_code": reason_code,
            "setup_state": _setup_state_for_fix_wiki_environment(origin_state),
            "resume_action": resume_action,
        },
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=resume_action,
    )


def _setup_resume_action(state: FixWikiState) -> str:
    match state:
        case FixWikiState.ENVIRONMENT_PATHS_MISSING | FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING:
            return "setup:set-paths"
        case FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED:
            return "setup:bootstrap-python"
        case _:
            return "/mednotes:setup"


def _setup_state_for_fix_wiki_environment(state: FixWikiState) -> str:
    match state:
        case FixWikiState.ENVIRONMENT_PATHS_MISSING | FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING:
            return "paths_required"
        case FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED:
            return "python_env_required"
        case _:
            return "checking_environment"


def _vault_guard_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="fix-wiki-vault-guard",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="fix_wiki.vault_guard",
        payload=_payload("vault_guard"),
        requires_receipt=False,
        no_resource_mutation=True,
    )


def _style_rewrite_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    """Emit the executable specialist effect from the StateChart transition.

    Runtime evidence may carry the batch payload, but the transition owns the
    public effect identity, target, origin state and model policy.
    """

    runtime_effect: WorkflowEffect | None = None
    if isinstance(workflow_event, DiagnosisQueueEvent):
        runtime_effect = workflow_event.style_rewrite_effect
    elif isinstance(workflow_event, RuntimeObservedEvent):
        runtime_effect = workflow_event.observation.style_rewrite_effect

    if runtime_effect is not None:
        payload = dict(runtime_effect.payload)
        if "kind" not in payload:
            payload["kind"] = "style_rewrite"
        return WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="fix-wiki-style-rewrite-specialist",
            origin_state=origin_state.value,
            kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
            target="med-knowledge-architect",
            payload=payload,
            requires_receipt=True,
            requires_attestation=True,
            model_policy=_style_rewrite_model_policy(runtime_effect.model_policy),
        )
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="fix-wiki-style-rewrite-specialist",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        target="med-knowledge-architect",
        payload=_payload("style_rewrite"),
        requires_receipt=True,
        requires_attestation=True,
        model_policy=_style_rewrite_model_policy(),
    )


def _style_rewrite_model_policy(override: JsonObject | None = None) -> JsonObject:
    """Keep specialist policy canonical even when runtime supplied batch data."""

    policy = {
        "policy": "medical_specialist_authoring.v1",
        "required_model_tier": "specialist",
        "preferred_model_tier": "pro",
        "forbid_flash_fallback": True,
    }
    if override:
        policy.update(override)
    policy["forbid_flash_fallback"] = True
    policy["required_model_tier"] = "specialist"
    return policy


def _related_notes_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="fix-wiki-related-notes-export",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="related_notes.export",
        payload=RelatedNotesExportEffectPayload(reason_code="related_notes").to_payload(),
        requires_receipt=False,
        no_resource_mutation=True,
    )


def _link_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    runtime_effect: WorkflowEffect | None = None
    if isinstance(workflow_event, RuntimeObservedEvent):
        runtime_effect = workflow_event.observation.link_effect
    if runtime_effect is not None:
        payload = dict(runtime_effect.payload)
        return WorkflowEffect(
            workflow=workflow_event.workflow,
            run_id=workflow_event.run_id,
            effect_id="fix-wiki-link-run",
            origin_state=origin_state.value,
            kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
            target="/mednotes:link",
            payload=payload,
            mutates_resources=True,
            rollback_declared=True,
            requires_receipt=False,
        )
    payload = LinkWorkflowRunEffectPayload(
        kind="link_run",
        diagnose=False,
        apply=True,
        no_related_notes=False,
    ).to_payload()
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="fix-wiki-link-run",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:link",
        payload=payload,
        mutates_resources=True,
        rollback_declared=True,
        requires_receipt=False,
    )


def _wait_external_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    target = (
        "related_notes.quota"
        if origin_state == FixWikiState.RELATED_NOTES_QUOTA_WAIT
        else "specialist_model.capacity"
    )
    resume_action = "Retomar pela rota oficial depois que a condição externa estiver resolvida."
    payload = WaitExternalEffectPayload(
        wait_target=target,
        blocked_reason=reason_for_state(origin_state).value,
        next_action=resume_action,
        resume_supported=True,
    ).to_payload()
    if isinstance(workflow_event, RelatedNotesQuotaWaitEvent):
        recovery_state = workflow_event.related_notes_recovery_state
    elif isinstance(workflow_event, RuntimeObservedEvent) and origin_state == FixWikiState.RELATED_NOTES_QUOTA_WAIT:
        recovery_state = workflow_event.observation.related_notes_recovery_state
    else:
        recovery_state = None
    if recovery_state is not None:
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
        if isinstance(workflow_event, RuntimeObservedEvent):
            resume_action = recovery_state.next_action or workflow_event.observation.next_action or resume_action
        else:
            resume_action = recovery_state.next_action or resume_action
        payload = WaitExternalEffectPayload.model_validate(
            {
                "wait_target": target,
                "related_notes_recovery_state": recovery_state,
                "next_action": resume_action,
            }
        ).to_payload()
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id=f"fix-wiki-{origin_state.value.replace('.', '-')}-wait",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.WAIT_EXTERNAL,
        target=target,
        payload=payload,
        requires_receipt=False,
        no_resource_mutation=True,
        resume_action=resume_action,
    )


def _final_validation_effect(workflow_event: FixWikiEvent, origin_state: FixWikiState) -> WorkflowEffect:
    return WorkflowEffect(
        workflow=workflow_event.workflow,
        run_id=workflow_event.run_id,
        effect_id="fix-wiki-final-validation",
        origin_state=origin_state.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="fix_wiki.final_validation",
        payload=_payload("final_validation"),
        requires_receipt=False,
        no_resource_mutation=True,
    )
