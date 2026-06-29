from __future__ import annotations

from typing import Annotated, Literal, NamedTuple, cast

from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator

from mednotes.domains.wiki.contracts.effect_payloads import (
    RelatedNotesRecoveryEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
    WaitExternalEffectPayload,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import WorkflowDecision, decision_from_payload
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_fsm import (
    FIX_WIKI_WORKFLOW,
    FixWikiDiagnosisLane,
    FixWikiFsmFacts,
    FixWikiState,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_machine import (
    AtomicitySplitAppliedEvent,
    AtomicitySplitBlockedEvent,
    DeterministicRepairsAppliedEvent,
    DeterministicRepairsBlockedEvent,
    FinalValidationFailedEvent,
    FinalValidationFoundMoreWorkEvent,
    FinalValidationPassedEvent,
    FinalValidationWarningsEvent,
    FixWikiBoundaryEventAdapter,
    FixWikiEvent,
    FixWikiMachine,
    LinkCompletedEvent,
    LinkerBlockedEvent,
    LinkGraphBlockedEvent,
    MergeAppliedEvent,
    MergeBlockedEvent,
    RelatedNotesBlockedEvent,
    RelatedNotesExportCompletedEvent,
    RelatedNotesObsidianNotReadyEvent,
    RelatedNotesQuotaWaitEvent,
    RollbackCompletedEvent,
    RollbackFailedEvent,
    SetupBootstrapBlockedEvent,
    SetupBootstrapReadyEvent,
    StyleRewriteAppliedEvent,
    StyleRewriteBlockedEvent,
    StyleRewriteCapacityWaitEvent,
    StyleRewriteReviewRequiredEvent,
    StyleRewriteSpecialistCompletedEvent,
    TaxonomyAppliedEvent,
    TaxonomyBlockedEvent,
    TaxonomyDecisionRequiredEvent,
    VaultGuardBlockedEvent,
    VaultGuardReadyEvent,
    VocabularyAppliedEvent,
    VocabularyCuratorCompletedEvent,
    VocabularyEvalNeedsReviewEvent,
    VocabularyIntegrityFailedEvent,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_states import FIX_WIKI_DIAGNOSIS_PRIORITY
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind, WorkflowEffectResult, WorkflowEffectStatus
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.state_machine import send_workflow_event
from mednotes.kernel.workflow import HumanDecisionPacket

RELATED_NOTES_EXTERNAL_RETRY_REASONS = frozenset(
    {
        "related_notes_headless_quota_exhausted",
        "related_notes_headless_time_budget_exhausted",
    }
)


class _FixWikiEffectErrorContextFields(ContractModel):
    """Typed lens for effect error fields that influence recovery state."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    root_cause: str = ""


class _FixWikiLinkChildDiagnosticFields(ContractModel):
    """Typed diagnostic fields exposed by the child `/mednotes:link` FSM result."""

    model_config = ConfigDict(extra="ignore")

    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload | None = None

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_recovery_state(cls, value: object) -> RelatedNotesRecoveryStateEffectPayload | None:
        if value in (None, {}):
            return None
        if isinstance(value, RelatedNotesRecoveryStateEffectPayload):
            return value
        return RelatedNotesRecoveryStateEffectPayload.from_payload(value)


class _FixWikiLinkChildPayloadFields(ContractModel):
    """Typed boundary for the child link FSM payload consumed by fix-wiki."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    diagnostic_context: _FixWikiLinkChildDiagnosticFields = Field(default_factory=_FixWikiLinkChildDiagnosticFields)


class FixWikiEffectRuntimeUpdate(ContractModel):
    """Typed effect-result update before rebuilding canonical fix-wiki facts."""

    model_config = ConfigDict(extra="forbid")

    next_action: str = ""
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)
    external_wait_reason_code: str = ""
    external_wait_resume_action: str = ""
    external_wait_payload: JsonObject = Field(default_factory=dict)
    related_notes_blocked: bool = Field(default=False, strict=True)
    related_notes_recovery_state: JsonObject = Field(default_factory=dict)
    human_decision_required: bool = Field(default=False, strict=True)
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    pending_effects: list[WorkflowEffect] | None = None
    linker_blocked: bool = Field(default=False, strict=True)
    failed: bool = Field(default=False, strict=True)
    failed_reason_code: str = ""

    def to_runtime_update(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.model_dump(mode="python", exclude_defaults=True, exclude_none=True),
        )


def _style_rewrite_payload_text(payload: JsonObject, key: str) -> str:
    """Read required work-item identity without letting raw JSON drive policy."""

    if key not in payload:
        return ""
    value = payload[key]
    if value is None:
        return ""
    return str(value).strip()


class FixWikiStyleRewriteSpecialistEffectRequest(ContractModel):
    """Typed request for the executable specialist effect emitted by fix-wiki.

    The effect authorizes parallel specialist authoring, but it never authorizes
    parallel vault mutation. Each item is an independent temp-output proposal;
    the CLI remains the only serial apply boundary.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    work_id: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    agent_name: str = Field(default="med-knowledge-architect", min_length=1)
    title: str = ""
    plan_path: str = ""
    manifest_path: str = ""
    current_batch_items: list[JsonObject] = Field(min_length=1)
    authoring_max_concurrency: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def batch_items_have_unique_operational_owners(self) -> FixWikiStyleRewriteSpecialistEffectRequest:
        work_ids = [_style_rewrite_payload_text(item, "work_id") for item in self.current_batch_items]
        target_paths = [_style_rewrite_payload_text(item, "target_path") for item in self.current_batch_items]
        temp_outputs = [_style_rewrite_payload_text(item, "temp_output") for item in self.current_batch_items]
        if any(not value for value in work_ids):
            raise ValueError("style rewrite batch items require work_id")
        if any(not value for value in target_paths):
            raise ValueError("style rewrite batch items require target_path")
        if any(not value for value in temp_outputs):
            raise ValueError("style rewrite batch items require temp_output")
        if len(work_ids) != len(set(work_ids)):
            raise ValueError("style rewrite batch work_id values must be unique")
        if len(target_paths) != len(set(target_paths)):
            raise ValueError("style rewrite batch target_path values must be unique")
        if len(temp_outputs) != len(set(temp_outputs)):
            raise ValueError("style rewrite batch temp_output values must be unique")
        if work_ids[0] != self.work_id:
            raise ValueError("style rewrite effect work_id must match the first batch item")
        if target_paths[0] != self.target_path:
            raise ValueError("style rewrite effect target_path must match the first batch item")
        return self


class _StyleRewriteWorkItemForEffect(ContractModel):
    """Minimal style-rewrite work item shape needed to launch a specialist."""

    model_config = ConfigDict(extra="ignore")

    work_id: str = ""
    target_path: str = ""
    agent: str = ""
    title: str = ""


class _StyleRewritePlanForEffect(ContractModel):
    """Typed plan slice for deriving specialist effects inside the effects domain."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    agent: str = ""
    max_concurrency: int = Field(default=1, ge=0)
    work_items: list[_StyleRewriteWorkItemForEffect] = Field(default_factory=list)


class _StyleRewritePlanPayloadForEffect(ContractModel):
    """Raw JSON work-item payloads preserved after the typed plan slice passes."""

    model_config = ConfigDict(extra="ignore")

    work_items: list[JsonObject] = Field(default_factory=list)


class _StyleRewriteBlockerGroupForEffect(ContractModel):
    """Authorization group proving the style rewrite lane can run automatically."""

    model_config = ConfigDict(extra="ignore")

    route: str = ""
    automatic: bool = Field(default=False, strict=True)


class _StyleRewriteBlockerResolutionForEffect(ContractModel):
    """Typed blocker-resolution slice used before emitting a specialist effect."""

    model_config = ConfigDict(extra="ignore")

    groups: list[_StyleRewriteBlockerGroupForEffect] = Field(default_factory=list)


class _StyleRewritePendingEffectSource(ContractModel):
    """Runtime facts accepted by the effect module before it emits executable work."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(min_length=1)
    requested_apply: bool = Field(default=False, strict=True)
    effective_apply: bool = Field(default=False, strict=True)
    requires_llm_rewrite_count: int = Field(default=0, ge=0, strict=True)
    style_rewrite_plan: JsonObject | None = None
    blocker_resolution: _StyleRewriteBlockerResolutionForEffect | None = None
    style_rewrite_plan_path: str = ""
    style_rewrite_manifest_path: str = ""


class SetupBootstrapEffectPayload(ContractModel):
    kind: Literal["setup_bootstrap"] = "setup_bootstrap"


class VaultGuardEffectPayload(ContractModel):
    kind: Literal["vault_guard"] = "vault_guard"


class DeterministicRepairsEffectPayload(ContractModel):
    kind: Literal["deterministic_repairs"] = "deterministic_repairs"


class StyleRewriteEffectPayload(ContractModel):
    kind: Literal["style_rewrite"] = "style_rewrite"
    work_id: str = ""
    target_path: str = ""
    operation_payload: JsonObject = Field(default_factory=dict)


class StyleRewriteApplyEffectPayload(ContractModel):
    kind: Literal["style_rewrite_apply"] = "style_rewrite_apply"
    work_id: str = ""
    target_path: str = ""


class TaxonomyEffectPayload(ContractModel):
    kind: Literal["taxonomy"] = "taxonomy"


class VocabularyCuratorEffectPayload(ContractModel):
    kind: Literal["vocabulary_curator"] = "vocabulary_curator"


class VocabularyEvalEffectPayload(ContractModel):
    kind: Literal["vocabulary_eval"] = "vocabulary_eval"


class VocabularyApplyEffectPayload(ContractModel):
    kind: Literal["vocabulary_apply"] = "vocabulary_apply"


class AtomicitySplitEffectPayload(ContractModel):
    kind: Literal["atomicity_split"] = "atomicity_split"


class MergeEffectPayload(ContractModel):
    kind: Literal["merge"] = "merge"


class RelatedNotesEffectPayload(ContractModel):
    kind: Literal["related_notes"] = "related_notes"


class LinkEffectPayload(ContractModel):
    kind: Literal["link"] = "link"
    diagnose: bool = False
    apply: bool = False
    no_related_notes: bool = False


class FinalValidationEffectPayload(ContractModel):
    kind: Literal["final_validation"] = "final_validation"


class RollbackEffectPayload(ContractModel):
    kind: Literal["rollback"] = "rollback"


class WaitExternalIntentPayload(ContractModel):
    """Internal effect intent keyed by `kind`; the canonical WAIT_EXTERNAL contract lives in WorkflowEffect.payload."""

    kind: Literal["wait_external"] = "wait_external"
    reason_code: str = ""


class HumanDecisionEffectPayload(ContractModel):
    kind: Literal["human_decision"] = "human_decision"
    reason_code: str = ""


FixWikiEffectPayload = Annotated[
    SetupBootstrapEffectPayload
    | VaultGuardEffectPayload
    | DeterministicRepairsEffectPayload
    | StyleRewriteEffectPayload
    | StyleRewriteApplyEffectPayload
    | TaxonomyEffectPayload
    | VocabularyCuratorEffectPayload
    | VocabularyEvalEffectPayload
    | VocabularyApplyEffectPayload
    | AtomicitySplitEffectPayload
    | MergeEffectPayload
    | RelatedNotesEffectPayload
    | LinkEffectPayload
    | FinalValidationEffectPayload
    | RollbackEffectPayload
    | WaitExternalIntentPayload
    | HumanDecisionEffectPayload,
    Field(discriminator="kind"),
]
FixWikiEffectPayloadAdapter = TypeAdapter(FixWikiEffectPayload)


class FixWikiSetupBootstrapReadyOutcome(ContractModel):
    code: Literal["setup.bootstrap.ready"] = "setup.bootstrap.ready"


class FixWikiSetupBootstrapBlockedOutcome(ContractModel):
    code: Literal["setup.bootstrap.blocked"] = "setup.bootstrap.blocked"


class FixWikiVaultGuardReadyOutcome(ContractModel):
    code: Literal["vault_guard.ready"] = "vault_guard.ready"


class FixWikiVaultGuardBlockedOutcome(ContractModel):
    code: Literal["vault_guard.blocked"] = "vault_guard.blocked"


class FixWikiDeterministicAppliedOutcome(ContractModel):
    code: Literal["deterministic.applied"] = "deterministic.applied"


class FixWikiDeterministicBlockedOutcome(ContractModel):
    code: Literal["deterministic.blocked"] = "deterministic.blocked"


class FixWikiStyleSpecialistCompletedOutcome(ContractModel):
    code: Literal["style.specialist_completed"] = "style.specialist_completed"


class FixWikiStyleCapacityWaitOutcome(ContractModel):
    code: Literal["style.capacity_wait"] = "style.capacity_wait"


class FixWikiStyleReviewRequiredOutcome(ContractModel):
    code: Literal["style.review_required"] = "style.review_required"


class FixWikiStyleApplyCompletedOutcome(ContractModel):
    code: Literal["style.apply_completed"] = "style.apply_completed"


class FixWikiStyleBlockedOutcome(ContractModel):
    code: Literal["style.blocked"] = "style.blocked"


class FixWikiTaxonomyDecisionRequiredOutcome(ContractModel):
    code: Literal["taxonomy.decision_required"] = "taxonomy.decision_required"


class FixWikiTaxonomyAppliedOutcome(ContractModel):
    code: Literal["taxonomy.applied"] = "taxonomy.applied"


class FixWikiTaxonomyBlockedOutcome(ContractModel):
    code: Literal["taxonomy.blocked"] = "taxonomy.blocked"


class FixWikiVocabularyCuratorCompletedOutcome(ContractModel):
    code: Literal["vocabulary.curator_completed"] = "vocabulary.curator_completed"


class FixWikiVocabularyEvalNeedsReviewOutcome(ContractModel):
    code: Literal["vocabulary.eval_needs_review"] = "vocabulary.eval_needs_review"


class FixWikiVocabularyAppliedOutcome(ContractModel):
    code: Literal["vocabulary.applied"] = "vocabulary.applied"


class FixWikiVocabularyIntegrityFailedOutcome(ContractModel):
    code: Literal["vocabulary.integrity_failed"] = "vocabulary.integrity_failed"


class FixWikiAtomicitySplitAppliedOutcome(ContractModel):
    code: Literal["atomicity.split_applied"] = "atomicity.split_applied"


class FixWikiAtomicityBlockedOutcome(ContractModel):
    code: Literal["atomicity.blocked"] = "atomicity.blocked"


class FixWikiMergeAppliedOutcome(ContractModel):
    code: Literal["merge.applied"] = "merge.applied"


class FixWikiMergeBlockedOutcome(ContractModel):
    code: Literal["merge.blocked"] = "merge.blocked"


class FixWikiRelatedNotesExportCompletedOutcome(ContractModel):
    code: Literal["related_notes.export_completed"] = "related_notes.export_completed"


class FixWikiRelatedNotesQuotaWaitOutcome(ContractModel):
    code: Literal["related_notes.quota_wait"] = "related_notes.quota_wait"


class FixWikiRelatedNotesObsidianNotReadyOutcome(ContractModel):
    code: Literal["related_notes.obsidian_not_ready"] = "related_notes.obsidian_not_ready"


class FixWikiRelatedNotesBlockedOutcome(ContractModel):
    code: Literal["related_notes.blocked"] = "related_notes.blocked"


class FixWikiLinkCompletedOutcome(ContractModel):
    code: Literal["link.completed"] = "link.completed"


class FixWikiLinkBlockedOutcome(ContractModel):
    code: Literal["link.blocked"] = "link.blocked"


class FixWikiLinkGraphBlockedOutcome(ContractModel):
    code: Literal["graph_blocked"] = "graph_blocked"


class FixWikiLinkerBlockedOutcome(ContractModel):
    code: Literal["linker_blocked"] = "linker_blocked"


class FixWikiFinalValidationPassedOutcome(ContractModel):
    code: Literal["final_validation.passed"] = "final_validation.passed"


class FixWikiFinalValidationWarningsOutcome(ContractModel):
    code: Literal["final_validation.warnings"] = "final_validation.warnings"


class FixWikiFinalValidationFoundMoreWorkOutcome(ContractModel):
    code: Literal["final_validation.found_more_work"] = "final_validation.found_more_work"
    pending_lanes: list[FixWikiDiagnosisLane] = Field(min_length=1)
    selected_lane: FixWikiDiagnosisLane

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
    def _selected_lane_must_be_priority_head(self) -> FixWikiFinalValidationFoundMoreWorkOutcome:
        if self.selected_lane != self.pending_lanes[0]:
            raise ValueError("selected_lane must match the highest-priority pending lane")
        return self


class FixWikiFinalValidationFailedOutcome(ContractModel):
    code: Literal["final_validation.failed"] = "final_validation.failed"


class FixWikiRollbackCompletedOutcome(ContractModel):
    code: Literal["rollback.completed"] = "rollback.completed"


class FixWikiRollbackFailedOutcome(ContractModel):
    code: Literal["rollback.failed"] = "rollback.failed"


FixWikiEffectOutcome = Annotated[
    FixWikiSetupBootstrapReadyOutcome
    | FixWikiSetupBootstrapBlockedOutcome
    | FixWikiVaultGuardReadyOutcome
    | FixWikiVaultGuardBlockedOutcome
    | FixWikiDeterministicAppliedOutcome
    | FixWikiDeterministicBlockedOutcome
    | FixWikiStyleSpecialistCompletedOutcome
    | FixWikiStyleCapacityWaitOutcome
    | FixWikiStyleReviewRequiredOutcome
    | FixWikiStyleApplyCompletedOutcome
    | FixWikiStyleBlockedOutcome
    | FixWikiTaxonomyDecisionRequiredOutcome
    | FixWikiTaxonomyAppliedOutcome
    | FixWikiTaxonomyBlockedOutcome
    | FixWikiVocabularyCuratorCompletedOutcome
    | FixWikiVocabularyEvalNeedsReviewOutcome
    | FixWikiVocabularyAppliedOutcome
    | FixWikiVocabularyIntegrityFailedOutcome
    | FixWikiAtomicitySplitAppliedOutcome
    | FixWikiAtomicityBlockedOutcome
    | FixWikiMergeAppliedOutcome
    | FixWikiMergeBlockedOutcome
    | FixWikiRelatedNotesExportCompletedOutcome
    | FixWikiRelatedNotesQuotaWaitOutcome
    | FixWikiRelatedNotesObsidianNotReadyOutcome
    | FixWikiRelatedNotesBlockedOutcome
    | FixWikiLinkCompletedOutcome
    | FixWikiLinkBlockedOutcome
    | FixWikiLinkGraphBlockedOutcome
    | FixWikiLinkerBlockedOutcome
    | FixWikiFinalValidationPassedOutcome
    | FixWikiFinalValidationWarningsOutcome
    | FixWikiFinalValidationFoundMoreWorkOutcome
    | FixWikiFinalValidationFailedOutcome
    | FixWikiRollbackCompletedOutcome
    | FixWikiRollbackFailedOutcome,
    Field(discriminator="code"),
]
FixWikiEffectOutcomeAdapter = TypeAdapter(FixWikiEffectOutcome)


class EffectReturnEventKey(NamedTuple):
    kind: WorkflowEffectKind
    target: str
    origin_state: str
    effect_code: str


# Exact effect-result matrix: no status/payload fallback is allowed to decide the
# next event. Adapter payloads may remain as opaque audit evidence only; this
# table is the domain-owned routing contract.
EFFECT_RETURN_EVENT_MATRIX: dict[EffectReturnEventKey, type[FixWikiEvent]] = {
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_PATHS_MISSING.value,
        "setup.bootstrap.ready",
    ): SetupBootstrapReadyEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_PATHS_MISSING.value,
        "setup.bootstrap.blocked",
    ): SetupBootstrapBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING.value,
        "setup.bootstrap.ready",
    ): SetupBootstrapReadyEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING.value,
        "setup.bootstrap.blocked",
    ): SetupBootstrapBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.value,
        "setup.bootstrap.ready",
    ): SetupBootstrapReadyEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:setup",
        FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.value,
        "setup.bootstrap.blocked",
    ): SetupBootstrapBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vault_guard",
        FixWikiState.VAULT_GUARD_RUNNING.value,
        "vault_guard.ready",
    ): VaultGuardReadyEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vault_guard",
        FixWikiState.VAULT_GUARD_RUNNING.value,
        "vault_guard.blocked",
    ): VaultGuardBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.deterministic_repairs",
        FixWikiState.DETERMINISTIC_REPAIRS_RUNNING.value,
        "deterministic.applied",
    ): DeterministicRepairsAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.deterministic_repairs",
        FixWikiState.DETERMINISTIC_REPAIRS_RUNNING.value,
        "deterministic.blocked",
    ): DeterministicRepairsBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        "med-knowledge-architect",
        FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value,
        "style.specialist_completed",
    ): StyleRewriteSpecialistCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        "med-knowledge-architect",
        FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value,
        "style.capacity_wait",
    ): StyleRewriteCapacityWaitEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        "med-knowledge-architect",
        FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value,
        "style.review_required",
    ): StyleRewriteReviewRequiredEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.style_rewrite_apply",
        FixWikiState.STYLE_REWRITE_APPLY_RUNNING.value,
        "style.apply_completed",
    ): StyleRewriteAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.style_rewrite_apply",
        FixWikiState.STYLE_REWRITE_APPLY_RUNNING.value,
        "style.blocked",
    ): StyleRewriteBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.taxonomy",
        FixWikiState.TAXONOMY_APPLY_RUNNING.value,
        "taxonomy.decision_required",
    ): TaxonomyDecisionRequiredEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.taxonomy",
        FixWikiState.TAXONOMY_APPLY_RUNNING.value,
        "taxonomy.applied",
    ): TaxonomyAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.taxonomy",
        FixWikiState.TAXONOMY_APPLY_RUNNING.value,
        "taxonomy.blocked",
    ): TaxonomyBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vocabulary_curator",
        FixWikiState.VOCABULARY_CURATOR_RUNNING.value,
        "vocabulary.curator_completed",
    ): VocabularyCuratorCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vocabulary_curator",
        FixWikiState.VOCABULARY_SEMANTIC_INGESTION_PENDING.value,
        "vocabulary.curator_completed",
    ): VocabularyCuratorCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vocabulary_eval",
        FixWikiState.VOCABULARY_EVAL_RUNNING.value,
        "vocabulary.eval_needs_review",
    ): VocabularyEvalNeedsReviewEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vocabulary_apply",
        FixWikiState.VOCABULARY_APPLY_RUNNING.value,
        "vocabulary.applied",
    ): VocabularyAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.vocabulary_apply",
        FixWikiState.VOCABULARY_APPLY_RUNNING.value,
        "vocabulary.integrity_failed",
    ): VocabularyIntegrityFailedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.atomicity_split",
        FixWikiState.ATOMICITY_SPLIT_RUNNING.value,
        "atomicity.split_applied",
    ): AtomicitySplitAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.atomicity_split",
        FixWikiState.ATOMICITY_SPLIT_RUNNING.value,
        "atomicity.blocked",
    ): AtomicitySplitBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.note_merge",
        FixWikiState.MERGE_RUNNING.value,
        "merge.applied",
    ): MergeAppliedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.note_merge",
        FixWikiState.MERGE_RUNNING.value,
        "merge.blocked",
    ): MergeBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "related_notes.export",
        FixWikiState.RELATED_NOTES_EXPORT_RUNNING.value,
        "related_notes.export_completed",
    ): RelatedNotesExportCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "related_notes.export",
        FixWikiState.RELATED_NOTES_EXPORT_RUNNING.value,
        "related_notes.quota_wait",
    ): RelatedNotesQuotaWaitEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "related_notes.export",
        FixWikiState.RELATED_NOTES_EXPORT_RUNNING.value,
        "related_notes.obsidian_not_ready",
    ): RelatedNotesObsidianNotReadyEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "related_notes.export",
        FixWikiState.RELATED_NOTES_EXPORT_RUNNING.value,
        "related_notes.blocked",
    ): RelatedNotesBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:link",
        FixWikiState.LINK_RUN_REQUESTED.value,
        "link.completed",
    ): LinkCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:link",
        FixWikiState.LINK_RUN_REQUESTED.value,
        "link.blocked",
    ): LinkerBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:link",
        FixWikiState.LINK_RUN_REQUESTED.value,
        "graph_blocked",
    ): LinkGraphBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:link",
        FixWikiState.LINK_RUN_REQUESTED.value,
        "linker_blocked",
    ): LinkerBlockedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "/mednotes:link",
        FixWikiState.LINK_RUN_REQUESTED.value,
        "related_notes.quota_wait",
    ): RelatedNotesQuotaWaitEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.final_validation",
        FixWikiState.FINAL_VALIDATION_RUNNING.value,
        "final_validation.passed",
    ): FinalValidationPassedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.final_validation",
        FixWikiState.FINAL_VALIDATION_RUNNING.value,
        "final_validation.warnings",
    ): FinalValidationWarningsEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.final_validation",
        FixWikiState.FINAL_VALIDATION_RUNNING.value,
        "final_validation.found_more_work",
    ): FinalValidationFoundMoreWorkEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.final_validation",
        FixWikiState.FINAL_VALIDATION_RUNNING.value,
        "final_validation.failed",
    ): FinalValidationFailedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.rollback",
        FixWikiState.ROLLBACK_RUNNING.value,
        "rollback.completed",
    ): RollbackCompletedEvent,
    EffectReturnEventKey(
        WorkflowEffectKind.RUN_SUBWORKFLOW,
        "fix_wiki.rollback",
        FixWikiState.ROLLBACK_RUNNING.value,
        "rollback.failed",
    ): RollbackFailedEvent,
}


def fix_wiki_event_from_effect_result(model: object, result: WorkflowEffectResult) -> FixWikiEvent:
    """Convert one typed effect result into the next StateChart event.

    `model` is accepted for the future persisted-run integration; the current
    routing contract is intentionally derived from the effect identity and the
    domain outcome code only.
    """

    del model
    outcome = FixWikiEffectOutcomeAdapter.validate_python(result.outcome.to_payload())
    key = EffectReturnEventKey(
        result.effect.kind,
        result.effect.target,
        result.effect.origin_state,
        outcome.code,
    )
    event_cls = EFFECT_RETURN_EVENT_MATRIX.get(key)
    if event_cls is None:
        raise ValueError(f"missing fix-wiki effect return matrix row: {key}")
    payload: JsonObject = {
        "workflow": result.effect.workflow or FIX_WIKI_WORKFLOW,
        "run_id": result.effect.run_id,
        "current_state": result.effect.origin_state,
    }
    if isinstance(outcome, FixWikiFinalValidationFoundMoreWorkOutcome):
        payload["pending_lanes"] = [lane.value for lane in outcome.pending_lanes]
        payload["selected_lane"] = outcome.selected_lane.value
    if event_cls is RelatedNotesQuotaWaitEvent:
        recovery = _effect_related_notes_recovery_state(result)
        if recovery is not None:
            payload["related_notes_recovery_state"] = recovery
    return event_cls.model_validate(payload)


def facts_after_effect_results(
    facts: FixWikiFsmFacts,
    effect_results: list[WorkflowEffectResult],
) -> FixWikiFsmFacts:
    """Fold typed effect results back into canonical fix-wiki facts.

    The public workflow facade may execute the effects, but effect status
    interpretation lives beside the effect contract so `health.py` does not
    become a second recovery-state machine.
    """

    if not effect_results:
        return facts
    statechart_facts = _facts_after_statechart_effect_results(facts, effect_results)
    if statechart_facts is not None:
        return statechart_facts
    last = effect_results[-1]
    diagnostics = _scrub_fsm_diagnostic_context(facts.diagnostic_context)
    diagnostics["effect_results"] = [result.to_payload() for result in effect_results]
    match last.status:
        case WorkflowEffectStatus.WAITING_EXTERNAL:
            update = FixWikiEffectRuntimeUpdate(
                external_wait_reason_code=_effect_external_wait_reason(last),
                external_wait_resume_action=last.resume_action or last.next_action,
                external_wait_payload=last.to_payload(),
                next_action=last.next_action or last.resume_action,
                diagnostic_context=diagnostics,
            )
            recovery_state = _effect_related_notes_recovery_state(last)
            if _is_related_notes_effect(last.effect) or recovery_state is not None:
                update_payload = {
                    **update.model_dump(mode="python"),
                    "related_notes_blocked": True,
                }
                if recovery_state is not None:
                    update_payload["related_notes_recovery_state"] = recovery_state.to_payload()
                update = FixWikiEffectRuntimeUpdate.model_validate(update_payload)
            return facts.with_runtime_updates(update.to_runtime_update())
        case WorkflowEffectStatus.WAITING_HUMAN:
            decision = _workflow_decision_from_waiting_human_effect(last)
            human_decision_required = decision.kind == "ask_human"
            update = FixWikiEffectRuntimeUpdate(
                human_decision_required=human_decision_required,
                decision=decision,
                human_decision_packet=last.human_decision_packet,
                pending_effects=[],
                next_action=last.next_action or last.resume_action or decision.next_action,
                diagnostic_context=diagnostics,
                error_context=last.error_context,
            )
            return facts.with_runtime_updates(update.to_runtime_update())
        case WorkflowEffectStatus.BLOCKED:
            update = _blocked_effect_runtime_update(last)
            update = FixWikiEffectRuntimeUpdate.model_validate(
                {**update.model_dump(mode="python"), "diagnostic_context": diagnostics}
            )
            return facts.with_runtime_updates(update.to_runtime_update())
        case WorkflowEffectStatus.FAILED:
            update = FixWikiEffectRuntimeUpdate(
                failed=True,
                failed_reason_code=_effect_result_reason(last, fallback="workflow_effect_failed"),
                next_action=last.next_action,
                diagnostic_context=diagnostics,
                error_context=last.error_context,
            )
            return facts.with_runtime_updates(update.to_runtime_update())
        case _:
            update = FixWikiEffectRuntimeUpdate(diagnostic_context=diagnostics)
            return facts.with_runtime_updates(update.to_runtime_update())


def _facts_after_statechart_effect_results(
    facts: FixWikiFsmFacts,
    effect_results: list[WorkflowEffectResult],
) -> FixWikiFsmFacts | None:
    """Apply effect outcomes as first-class FixWikiMachine events.

    Adapters may execute effects, but they do not decide the post-effect state.
    A typed domain outcome must map through `EFFECT_RETURN_EVENT_MATRIX` and then
    through python-statemachine before it can become public workflow state.
    """

    driving_results = [result for result in effect_results if _effect_result_drives_statechart(result)]
    if not driving_results:
        return None

    diagnostics = _scrub_fsm_diagnostic_context(facts.diagnostic_context)
    diagnostics["effect_results"] = [result.to_payload() for result in effect_results]
    model = WorkflowModel.start(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=facts.run_id,
        initial_state=facts.initial_state.value,
    )
    machine = FixWikiMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    send_workflow_event(machine, facts.event)
    last_event: FixWikiEvent = facts.event
    last_result: WorkflowEffectResult | None = None
    for result in driving_results:
        last_event = fix_wiki_event_from_effect_result(model, result)
        send_workflow_event(machine, FixWikiBoundaryEventAdapter.validate_python(last_event.to_payload()))
        last_result = result

    runtime_update: dict[str, object] = {
        "diagnostic_context": diagnostics,
        "pending_effects": list(model.pending_effects),
    }
    if isinstance(last_event, RelatedNotesQuotaWaitEvent) and last_event.related_notes_recovery_state.status:
        runtime_update.update(
            {
                "related_notes_blocked": True,
                "related_notes_recovery_state": last_event.related_notes_recovery_state.to_payload(),
                "external_wait_reason_code": last_event.related_notes_recovery_state.blocked_reason,
                "external_wait_resume_action": last_event.related_notes_recovery_state.next_action,
            }
        )
    elif isinstance(last_event, StyleRewriteCapacityWaitEvent) and last_result is not None:
        resume_action = last_result.resume_action or last_result.next_action
        runtime_update.update(
            {
                "external_wait_reason_code": _effect_result_reason(
                    last_result,
                    fallback="specialist_model_capacity_unavailable",
                ),
                "external_wait_resume_action": resume_action,
                "external_wait_payload": last_result.to_payload(),
                "next_action": last_result.next_action or resume_action,
                "error_context": last_result.error_context,
            }
        )
    runtime = facts.runtime.__class__.model_validate(
        {**facts.runtime.model_dump(mode="python"), **runtime_update}
    )
    event = FixWikiBoundaryEventAdapter.validate_python(last_event.to_payload())
    return FixWikiFsmFacts(
        run_id=facts.run_id,
        initial_state=FixWikiState(event.current_state),
        event=event,
        runtime=runtime,
        machine_effects=list(model.pending_effects),
    )


def _effect_result_drives_statechart(result: WorkflowEffectResult) -> bool:
    """Return true for effect kinds whose outcomes represent a new FSM event."""

    if result.effect.kind in {WorkflowEffectKind.WAIT_EXTERNAL, WorkflowEffectKind.ASK_HUMAN}:
        return False
    return result.status in {
        WorkflowEffectStatus.COMPLETED,
        WorkflowEffectStatus.COMPLETED_WITH_WARNINGS,
        WorkflowEffectStatus.WAITING_AGENT,
        WorkflowEffectStatus.WAITING_EXTERNAL,
        WorkflowEffectStatus.WAITING_HUMAN,
        WorkflowEffectStatus.BLOCKED,
        WorkflowEffectStatus.FAILED,
    }


def _scrub_fsm_diagnostic_context(context: JsonObject) -> JsonObject:
    """Keep diagnostics explanatory; executable routes live in FSM root contracts."""

    diagnostics = dict(context)
    for key in (
        "orchestration_plan",
        "continuation_plan",
        "pending_effects",
        "action_directives",
    ):
        diagnostics.pop(key, None)
    return diagnostics


def _effect_result_reason(result: WorkflowEffectResult, *, fallback: str) -> str:
    """Return the typed reason from an effect result without reading diagnostics."""

    error_context = _FixWikiEffectErrorContextFields.model_validate(result.error_context)
    for value in (
        error_context.blocked_reason,
        error_context.root_cause,
        getattr(result.outcome, "reason_code", ""),
    ):
        text = value.strip() if isinstance(value, str) else ""
        if text:
            return text
    return fallback


def _blocked_effect_runtime_update(result: WorkflowEffectResult) -> FixWikiEffectRuntimeUpdate:
    """Classify blocked effect results by the effect that actually blocked."""

    reason = _effect_result_reason(result, fallback="workflow_effect_blocked")
    if result.effect.kind == WorkflowEffectKind.RUN_SUBWORKFLOW and result.effect.target == "/mednotes:link":
        return FixWikiEffectRuntimeUpdate(
            next_action=result.next_action,
            error_context=result.error_context,
            linker_blocked=True,
        )
    if _is_related_notes_effect(result.effect):
        recovery_state = _effect_related_notes_recovery_state(result)
        return FixWikiEffectRuntimeUpdate(
            next_action=result.next_action,
            error_context=result.error_context,
            related_notes_blocked=True,
            related_notes_recovery_state=recovery_state.to_payload() if recovery_state is not None else {},
        )
    return FixWikiEffectRuntimeUpdate(
        next_action=result.next_action,
        error_context=result.error_context,
        failed=True,
        failed_reason_code=reason,
    )


def _workflow_decision_from_waiting_human_effect(result: WorkflowEffectResult) -> WorkflowDecision:
    """Convert a typed ask-human effect result into the fix-wiki decision root."""

    packet = result.human_decision_packet
    if packet is None:
        raise ValueError("waiting_human effect result requires human_decision_packet")
    next_action = result.next_action or result.resume_action or packet.resume_action
    return decision_from_payload(
        {
            "human_decision_packet": packet.to_payload(),
            "next_action": next_action,
        }
    )


def _effect_related_notes_recovery_state(result: WorkflowEffectResult) -> RelatedNotesRecoveryStateEffectPayload | None:
    """Return typed Related Notes recovery state from official effect payloads only."""

    try:
        wait_payload = WaitExternalEffectPayload.from_effect_payload(result.payload)
    except ValueError:
        wait_payload = None
    if wait_payload is not None and wait_payload.related_notes_recovery_state is not None:
        return wait_payload.related_notes_recovery_state
    try:
        recovery_payload = RelatedNotesRecoveryEffectPayload.from_operation_payload(result.payload)
    except ValueError:
        return _link_child_related_notes_recovery_state(result)
    return recovery_payload.related_notes_recovery_state


def _link_child_related_notes_recovery_state(
    result: WorkflowEffectResult,
) -> RelatedNotesRecoveryStateEffectPayload | None:
    """Read Related Notes progress from a typed child `/mednotes:link` FSM result."""

    if result.effect.kind != WorkflowEffectKind.RUN_SUBWORKFLOW or result.effect.target != "/mednotes:link":
        return None
    try:
        child_payload = _FixWikiLinkChildPayloadFields.model_validate(result.payload)
    except ValueError:
        return None
    return child_payload.diagnostic_context.related_notes_recovery_state


def _effect_external_wait_reason(result: WorkflowEffectResult) -> str:
    error_context = _FixWikiEffectErrorContextFields.model_validate(result.error_context)
    if error_context.blocked_reason:
        return error_context.blocked_reason
    recovery_state = _effect_related_notes_recovery_state(result)
    if recovery_state is not None and recovery_state.blocked_reason:
        return recovery_state.blocked_reason
    if result.effect.kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL:
        return "specialist_model_capacity_unavailable"
    if _is_related_notes_effect(result.effect):
        return "related_notes_headless_quota_exhausted"
    return "workflow_external_wait"


def _is_related_notes_effect(effect: WorkflowEffect) -> bool:
    """Recognize Wiki-domain Related Notes work through target, not kernel kind."""

    return effect.kind == WorkflowEffectKind.RUN_SUBWORKFLOW and effect.target in {
        "related_notes.export",
        "related_notes_export",
        "related_notes.section",
    }


def style_rewrite_specialist_effect_from_request(
    request: FixWikiStyleRewriteSpecialistEffectRequest,
) -> WorkflowEffect:
    """Build the rich specialist-call effect from a typed fix-wiki request."""

    batch_items = request.current_batch_items
    authoring_max_concurrency = min(request.authoring_max_concurrency, len(batch_items))
    return WorkflowEffect(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=request.run_id,
        effect_id=f"style-rewrite-{request.work_id}",
        origin_state=FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value,
        kind=WorkflowEffectKind.CALL_SPECIALIST_MODEL,
        target=request.agent_name,
        payload={
            "kind": "style_rewrite",
            "work_id": request.work_id,
            "agent": request.agent_name,
            "title": request.title,
            "execution_mode": "parallel_authoring_serial_apply",
            "authoring_mode": "parallel",
            "authoring_max_concurrency": authoring_max_concurrency,
            "apply_mode": "serial",
            "serial_apply_required": True,
            "wait_for_all_authoring_outputs_before_apply": True,
            "current_batch_items": batch_items,
            "current_batch_item_count": len(batch_items),
            "plan_path": request.plan_path,
            "manifest_path": request.manifest_path,
            "style_rewrite_plan_path": request.plan_path,
            "style_rewrite_manifest_path": request.manifest_path,
        },
        requires_receipt=True,
        requires_attestation=True,
        model_policy={
            "policy": "medical_specialist_authoring.v1",
            "required_model_tier": "specialist",
            "preferred_model_tier": "pro",
            "forbid_flash_fallback": True,
        },
    )


def pending_effect_payloads_from_fix_wiki_runtime_source(source: object) -> list[JsonObject]:
    """Derive executable pending effects from typed runtime facts in the effects layer."""

    fields = _StyleRewritePendingEffectSource.model_validate(source)
    if not fields.requested_apply or not fields.effective_apply:
        return []
    rewrite_plan = fields.style_rewrite_plan
    if rewrite_plan is None:
        return []
    typed_plan = _StyleRewritePlanForEffect.model_validate(rewrite_plan)
    plan_payload = _StyleRewritePlanPayloadForEffect.model_validate(rewrite_plan)
    if typed_plan.status != "ready":
        return []
    if fields.requires_llm_rewrite_count <= 0:
        return []
    blocker_resolution = fields.blocker_resolution
    if blocker_resolution is None or not _has_automatic_style_rewrite_group_for_effect(blocker_resolution):
        return []
    work_items = list(typed_plan.work_items)
    if not work_items:
        return []
    first_item = work_items[0]
    work_id = first_item.work_id.strip()
    target_path = first_item.target_path.strip()
    if not work_id or not target_path:
        return []
    agent_name = first_item.agent or typed_plan.agent or "med-knowledge-architect"
    current_batch_items: list[JsonObject] = []
    for index, work_item in enumerate(work_items):
        item_payload = (
            plan_payload.work_items[index]
            if index < len(plan_payload.work_items)
            else JsonObjectAdapter.validate_python(work_item.model_dump(exclude_defaults=True, exclude_none=True))
        )
        current_batch_items.append(item_payload)
    effect = style_rewrite_specialist_effect_from_request(
        FixWikiStyleRewriteSpecialistEffectRequest(
            run_id=fields.run_id,
            work_id=work_id,
            target_path=target_path,
            agent_name=agent_name,
            title=first_item.title,
            plan_path=fields.style_rewrite_plan_path,
            manifest_path=fields.style_rewrite_manifest_path,
            current_batch_items=current_batch_items,
            authoring_max_concurrency=typed_plan.max_concurrency or len(current_batch_items),
        )
    )
    return [effect.to_payload()]


def _has_automatic_style_rewrite_group_for_effect(
    blocker_resolution: _StyleRewriteBlockerResolutionForEffect,
) -> bool:
    return any(group.route == "style_rewrite" and group.automatic for group in blocker_resolution.groups)


def missing_fix_wiki_effect_adapter_is_optional(effect: WorkflowEffect) -> bool:
    """Return true only for agent-mediated effects that may wait for the agent."""

    return effect.kind in {
        WorkflowEffectKind.ASK_HUMAN,
        WorkflowEffectKind.CALL_SPECIALIST_MODEL,
    }


def effect_result_stops_fix_wiki_execution(result: WorkflowEffectResult) -> bool:
    """Return whether an effect result should pause the current fix-wiki pass."""

    return result.status in {
        WorkflowEffectStatus.WAITING_EXTERNAL,
        WorkflowEffectStatus.WAITING_HUMAN,
        WorkflowEffectStatus.BLOCKED,
        WorkflowEffectStatus.FAILED,
    }
