"""Canonical fix-wiki StateChart states and state-category mapping.

This module is pure domain data shared by the machine and public projector.
`fix_wiki_machine.py` owns transitions; `fix_wiki_fsm.py` only projects the
machine result and must not be imported by the machine for state truth.
"""

from __future__ import annotations

from enum import StrEnum

from mednotes.kernel.state_machine import WorkflowStateCategory

FIX_WIKI_WORKFLOW = "/mednotes:fix-wiki"


class FixWikiState(StrEnum):
    """Canonical leaf states for the fix-wiki StateChart."""

    DIAGNOSIS_RUNNING = "diagnosis.running"
    ENVIRONMENT_PATHS_MISSING = "environment.paths_missing"
    ENVIRONMENT_WIKI_DIR_MISSING = "environment.wiki_dir_missing"
    ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED = "environment.windows_path_or_venv_blocked"
    VAULT_GUARD_RUNNING = "vault_guard.running"
    VAULT_GUARD_DECISION_REQUIRED = "vault_guard.decision_required"
    SUBAGENT_PLAN_ATTESTATION_REQUIRED = "subagent_plan_attestation.required"
    SUBAGENT_PLAN_ATTESTATION_INVALID = "subagent_plan_attestation.invalid"
    AGENT_TOOL_CONTRACT_VIOLATION = "agent_tool_contract_violation"
    DETERMINISTIC_REPAIRS_RUNNING = "deterministic_repairs.running"
    DETERMINISTIC_REPAIRS_FAILED = "deterministic_repairs.failed"
    STYLE_REWRITE_SPECIALIST_REQUESTED = "style_rewrite.specialist_requested"
    STYLE_REWRITE_CAPACITY_WAIT = "style_rewrite.capacity_wait"
    STYLE_REWRITE_REVIEW_REQUIRED = "style_rewrite.review_required"
    STYLE_REWRITE_APPLY_RUNNING = "style_rewrite.apply_running"
    TAXONOMY_DECISION_REQUIRED = "taxonomy.decision_required"
    TAXONOMY_APPLY_RUNNING = "taxonomy.apply_running"
    VOCABULARY_CURATOR_RUNNING = "vocabulary.curator_running"
    VOCABULARY_SEMANTIC_INGESTION_PENDING = "vocabulary.semantic_ingestion_pending"
    VOCABULARY_EVAL_RUNNING = "vocabulary.eval_running"
    VOCABULARY_EVAL_NEEDS_REVIEW = "vocabulary.eval_needs_review"
    VOCABULARY_APPLY_RUNNING = "vocabulary.apply_running"
    VOCABULARY_SQLITE_INTEGRITY_FAILED = "vocabulary.sqlite_integrity_failed"
    ATOMICITY_SPLIT_RUNNING = "atomicity_split.running"
    ATOMICITY_SPLIT_REVIEW_REQUIRED = "atomicity_split.review_required"
    RELATED_NOTES_EXPORT_RUNNING = "related_notes.export_running"
    RELATED_NOTES_QUOTA_WAIT = "related_notes.quota_wait"
    RELATED_NOTES_OBSIDIAN_NOT_READY = "related_notes.obsidian_not_ready"
    RELATED_NOTES_BLOCKED = "related_notes.blocked"
    LINK_RUN_REQUESTED = "link.run_requested"
    LINK_GRAPH_BLOCKED = "link.graph_blocked"
    LINK_GRAPH_REVIEW_REQUIRED = "link.graph_review_required"
    LINKER_BLOCKED = "link.linker_blocked"
    MERGE_RUNNING = "merge.running"
    MERGE_REVIEW_REQUIRED = "merge.review_required"
    CONTRACT_GAP_MISSING_NEXT_ACTION = "contract_gap.missing_next_action"
    CONTRACT_GAP_MISSING_ERROR_CONTEXT = "contract_gap.missing_error_context"
    ROLLBACK_RUNNING = "rollback.running"
    ROLLBACK_PERFORMED = "rollback.performed"
    ROLLBACK_FAILED = "rollback.failed"
    FINAL_VALIDATION_RUNNING = "final_validation.running"
    FINAL_VALIDATION_FAILED = "final_validation.failed"
    PREVIEW_READY = "preview.ready"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class FixWikiDiagnosisLane(StrEnum):
    """Typed diagnosis lanes; guards are allowed to inspect only this value."""

    ENVIRONMENT_PATHS_MISSING = "environment.paths_missing"
    ENVIRONMENT_WIKI_DIR_MISSING = "environment.wiki_dir_missing"
    ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED = "environment.windows_path_or_venv_blocked"
    VAULT_GUARD_DECISION_REQUIRED = "vault_guard.decision_required"
    SUBAGENT_PLAN_ATTESTATION_REQUIRED = "subagent_plan_attestation.required"
    SUBAGENT_PLAN_ATTESTATION_INVALID = "subagent_plan_attestation.invalid"
    AGENT_TOOL_CONTRACT_VIOLATION = "agent_tool_contract_violation"
    DETERMINISTIC_REPAIRS = "deterministic_repairs"
    STYLE_REWRITE = "style_rewrite"
    TAXONOMY = "taxonomy"
    VOCABULARY_SEMANTIC_INGESTION_PENDING = "vocabulary.semantic_ingestion_pending"
    VOCABULARY = "vocabulary"
    ATOMICITY_SPLIT = "atomicity_split"
    MERGE = "merge"
    RELATED_NOTES = "related_notes"
    LINK = "link"
    CONTRACT_GAP_MISSING_NEXT_ACTION = "contract_gap.missing_next_action"
    CONTRACT_GAP_MISSING_ERROR_CONTEXT = "contract_gap.missing_error_context"
    ROLLBACK = "rollback"
    FINAL_VALIDATION = "final_validation"


class FixWikiReason(StrEnum):
    """Closed public reason labels derived from fix-wiki leaf states."""

    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PREVIEW_READY = "preview_ready"
    ENVIRONMENT_PATHS_MISSING = "environment.paths_missing"
    ENVIRONMENT_WIKI_DIR_MISSING = "environment.wiki_dir_missing"
    ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED = "environment.windows_path_or_venv_blocked"
    WAITING_EXTERNAL_RELATED_NOTES = "waiting_external_related_notes"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_HUMAN = "waiting_human"
    SUBAGENT_PLAN_ATTESTATION_REQUIRED = "subagent_plan_attestation_required"
    SUBAGENT_PLAN_ATTESTATION_INVALID = "subagent_plan_attestation_invalid"
    STYLE_REWRITE_REVIEW_REQUIRED = "style_rewrite_review_required"
    TAXONOMY_DECISION_REQUIRED = "taxonomy_decision_required"
    VOCABULARY_EVAL_NEEDS_REVIEW = "vocabulary_eval_needs_review"
    ATOMICITY_SPLIT_REVIEW_REQUIRED = "atomicity_split_review_required"
    MERGE_REVIEW_REQUIRED = "merge_review_required"
    STYLE_REWRITE_READY = "style_rewrite_ready"
    VOCABULARY_SEMANTIC_INGESTION_PENDING = "vocabulary_semantic_ingestion_pending"
    GRAPH_BLOCKED = "graph_blocked"
    ATOMICITY_SPLIT_REQUIRED = "atomicity_split_required"
    RELATED_NOTES_BLOCKED = "related_notes_blocked"
    LINK_RUN_REQUESTED = "link_run_requested"
    LINKER_BLOCKED = "linker_blocked"
    GRAPH_REVIEW_REQUIRED = "graph_review_required"
    TAXONOMY_BLOCKED = "taxonomy_blocked"
    VAULT_GUARD_REQUIRED = "vault_guard_required"
    STYLE_REWRITE_REQUIRED = "style_rewrite_required"
    FAILED = "failed"


FIX_WIKI_DIAGNOSIS_PRIORITY: tuple[FixWikiDiagnosisLane, ...] = (
    FixWikiDiagnosisLane.ENVIRONMENT_PATHS_MISSING,
    FixWikiDiagnosisLane.ENVIRONMENT_WIKI_DIR_MISSING,
    FixWikiDiagnosisLane.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED,
    FixWikiDiagnosisLane.VAULT_GUARD_DECISION_REQUIRED,
    FixWikiDiagnosisLane.SUBAGENT_PLAN_ATTESTATION_REQUIRED,
    FixWikiDiagnosisLane.SUBAGENT_PLAN_ATTESTATION_INVALID,
    FixWikiDiagnosisLane.AGENT_TOOL_CONTRACT_VIOLATION,
    FixWikiDiagnosisLane.DETERMINISTIC_REPAIRS,
    FixWikiDiagnosisLane.STYLE_REWRITE,
    FixWikiDiagnosisLane.TAXONOMY,
    FixWikiDiagnosisLane.VOCABULARY_SEMANTIC_INGESTION_PENDING,
    FixWikiDiagnosisLane.VOCABULARY,
    FixWikiDiagnosisLane.ATOMICITY_SPLIT,
    FixWikiDiagnosisLane.MERGE,
    FixWikiDiagnosisLane.RELATED_NOTES,
    FixWikiDiagnosisLane.LINK,
    FixWikiDiagnosisLane.CONTRACT_GAP_MISSING_NEXT_ACTION,
    FixWikiDiagnosisLane.CONTRACT_GAP_MISSING_ERROR_CONTEXT,
    FixWikiDiagnosisLane.ROLLBACK,
    FixWikiDiagnosisLane.FINAL_VALIDATION,
)


def category_for_state(state: str | FixWikiState) -> WorkflowStateCategory:
    """Map every fix-wiki leaf state to the public FSM category."""

    try:
        state_value = state if isinstance(state, FixWikiState) else FixWikiState(str(state))
    except ValueError as exc:
        raise ValueError(f"unknown fix-wiki state: {state}") from exc

    match state_value:
        case (
            FixWikiState.DIAGNOSIS_RUNNING
            | FixWikiState.VAULT_GUARD_RUNNING
            | FixWikiState.DETERMINISTIC_REPAIRS_RUNNING
            | FixWikiState.STYLE_REWRITE_APPLY_RUNNING
            | FixWikiState.TAXONOMY_APPLY_RUNNING
            | FixWikiState.VOCABULARY_CURATOR_RUNNING
            | FixWikiState.VOCABULARY_EVAL_RUNNING
            | FixWikiState.VOCABULARY_APPLY_RUNNING
            | FixWikiState.ATOMICITY_SPLIT_RUNNING
            | FixWikiState.RELATED_NOTES_EXPORT_RUNNING
            | FixWikiState.LINK_RUN_REQUESTED
            | FixWikiState.MERGE_RUNNING
            | FixWikiState.ROLLBACK_RUNNING
            | FixWikiState.FINAL_VALIDATION_RUNNING
        ):
            return WorkflowStateCategory.RUNNING
        case (
            FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED
            | FixWikiState.VOCABULARY_SEMANTIC_INGESTION_PENDING
        ):
            return WorkflowStateCategory.WAITING_AGENT
        case (
            FixWikiState.STYLE_REWRITE_CAPACITY_WAIT
            | FixWikiState.RELATED_NOTES_QUOTA_WAIT
        ):
            return WorkflowStateCategory.WAITING_EXTERNAL
        case (
            FixWikiState.STYLE_REWRITE_REVIEW_REQUIRED
            | FixWikiState.TAXONOMY_DECISION_REQUIRED
            | FixWikiState.SUBAGENT_PLAN_ATTESTATION_REQUIRED
            | FixWikiState.SUBAGENT_PLAN_ATTESTATION_INVALID
            | FixWikiState.VOCABULARY_EVAL_NEEDS_REVIEW
            | FixWikiState.ATOMICITY_SPLIT_REVIEW_REQUIRED
            | FixWikiState.MERGE_REVIEW_REQUIRED
            | FixWikiState.LINK_GRAPH_REVIEW_REQUIRED
        ):
            return WorkflowStateCategory.WAITING_HUMAN
        case (
            FixWikiState.ENVIRONMENT_PATHS_MISSING
            | FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING
            | FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
            | FixWikiState.RELATED_NOTES_OBSIDIAN_NOT_READY
            | FixWikiState.RELATED_NOTES_BLOCKED
            | FixWikiState.LINK_GRAPH_BLOCKED
            | FixWikiState.LINKER_BLOCKED
            | FixWikiState.VAULT_GUARD_DECISION_REQUIRED
            | FixWikiState.ROLLBACK_FAILED
        ):
            return WorkflowStateCategory.BLOCKED
        case (
            FixWikiState.FAILED
            | FixWikiState.AGENT_TOOL_CONTRACT_VIOLATION
            | FixWikiState.CONTRACT_GAP_MISSING_NEXT_ACTION
            | FixWikiState.CONTRACT_GAP_MISSING_ERROR_CONTEXT
            | FixWikiState.DETERMINISTIC_REPAIRS_FAILED
            | FixWikiState.VOCABULARY_SQLITE_INTEGRITY_FAILED
            | FixWikiState.ROLLBACK_PERFORMED
            | FixWikiState.FINAL_VALIDATION_FAILED
        ):
            return WorkflowStateCategory.FAILED
        case FixWikiState.COMPLETED:
            return WorkflowStateCategory.COMPLETED
        case FixWikiState.PREVIEW_READY:
            return WorkflowStateCategory.COMPLETED
        case FixWikiState.COMPLETED_WITH_WARNINGS:
            return WorkflowStateCategory.COMPLETED_WITH_WARNINGS
        case _:
            raise AssertionError(f"unclassified fix-wiki state: {state_value}")


def reason_for_state(state: str | FixWikiState) -> FixWikiReason:
    """Derive the public reason from the canonical leaf state only.

    Runtime outcomes and transition metadata may be useful audit evidence, but
    they are not allowed to override this map. If a reason needs to become more
    specific, the fix is to add a precise leaf state rather than a second
    status/reason channel.
    """

    state_value = state if isinstance(state, FixWikiState) else FixWikiState(str(state))
    if state_value == FixWikiState.PREVIEW_READY:
        return FixWikiReason.PREVIEW_READY
    if state_value == FixWikiState.ENVIRONMENT_PATHS_MISSING:
        return FixWikiReason.ENVIRONMENT_PATHS_MISSING
    if state_value == FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING:
        return FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING
    if state_value == FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED:
        return FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
    if state_value == FixWikiState.COMPLETED:
        return FixWikiReason.COMPLETED
    if state_value == FixWikiState.COMPLETED_WITH_WARNINGS:
        return FixWikiReason.COMPLETED_WITH_WARNINGS
    if state_value == FixWikiState.RELATED_NOTES_QUOTA_WAIT:
        return FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES
    if state_value == FixWikiState.STYLE_REWRITE_CAPACITY_WAIT:
        return FixWikiReason.WAITING_EXTERNAL
    if state_value == FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED:
        return FixWikiReason.STYLE_REWRITE_READY
    if state_value == FixWikiState.VOCABULARY_SEMANTIC_INGESTION_PENDING:
        return FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING
    if state_value == FixWikiState.LINK_GRAPH_BLOCKED:
        return FixWikiReason.GRAPH_BLOCKED
    if state_value == FixWikiState.LINK_GRAPH_REVIEW_REQUIRED:
        return FixWikiReason.GRAPH_REVIEW_REQUIRED
    if state_value == FixWikiState.LINK_RUN_REQUESTED:
        return FixWikiReason.LINK_RUN_REQUESTED
    if state_value == FixWikiState.LINKER_BLOCKED:
        return FixWikiReason.LINKER_BLOCKED
    if state_value in {FixWikiState.RELATED_NOTES_BLOCKED, FixWikiState.RELATED_NOTES_OBSIDIAN_NOT_READY}:
        return FixWikiReason.RELATED_NOTES_BLOCKED
    if state_value == FixWikiState.ATOMICITY_SPLIT_REVIEW_REQUIRED:
        return FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED
    if state_value == FixWikiState.TAXONOMY_DECISION_REQUIRED:
        return FixWikiReason.TAXONOMY_DECISION_REQUIRED
    if state_value == FixWikiState.VAULT_GUARD_DECISION_REQUIRED:
        return FixWikiReason.VAULT_GUARD_REQUIRED
    if state_value == FixWikiState.STYLE_REWRITE_REVIEW_REQUIRED:
        return FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
    if state_value == FixWikiState.SUBAGENT_PLAN_ATTESTATION_REQUIRED:
        return FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED
    if state_value == FixWikiState.SUBAGENT_PLAN_ATTESTATION_INVALID:
        return FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID
    if state_value == FixWikiState.VOCABULARY_EVAL_NEEDS_REVIEW:
        return FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW
    if state_value == FixWikiState.MERGE_REVIEW_REQUIRED:
        return FixWikiReason.MERGE_REVIEW_REQUIRED
    if category_for_state(state_value) == WorkflowStateCategory.WAITING_HUMAN:
        return FixWikiReason.WAITING_HUMAN
    if category_for_state(state_value) == WorkflowStateCategory.FAILED:
        return FixWikiReason.FAILED
    return FixWikiReason.FAILED
