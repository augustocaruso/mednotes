"""Command-line interface for deterministic Wiki workflow operations."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Literal, cast

from pydantic import ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.api import (
    DEFAULT_RELATED_NOTES_MAX_LINKS,
    DEFAULT_RELATED_NOTES_MIN_SCORE,
    EXIT_IO,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    LINK_REQUIRED_INPUTS,
    LINK_TRIGGER_CONTEXT_SCHEMA,
    MARKDOWN_QUERY_BLOCKED_REASON,
    MARKDOWN_QUERY_NEXT_ACTION,
    PUBLISH_REQUIRED_INPUTS,
    TRIAGER_EVAL_RETRY_NEXT_ACTION,
    WIKI_CLI_RELPATH,
    AgentDirective,
    AuditWorkflow,
    HappyPathRunMetrics,
    JsonArrayAdapter,
    JsonObject,
    JsonObjectAdapter,
    JsonValue,
    LinkerRunResult,
    LinkRelatedSyncResult,
    LinkWorkflowEffectAdapter,
    LinkWorkflowRunEffectPayload,
    MarkdownDbChatMetadataProvider,
    MarkdownNodeRuntimeUnavailable,
    MarkdownQueryUnavailable,
    MedConfig,
    MedOpsError,
    MissingPathError,
    NextSpecialistTask,
    NoPendingRawChatsEvent,
    NoTriagedRawChatsEvent,
    PlanOutputReceipt,
    ProcessChatsErrorContext,
    ProcessChatsFsmFacts,
    ProcessChatsLinkerRun,
    ProcessChatsOperationalSummary,
    ProcessChatsPublishOperationResult,
    ProcessChatsPublishRuntimeObservation,
    ProcessChatsState,
    RelatedNotesEffectAdapter,
    RelatedNotesExportEffectPayload,
    RollbackFailureRecordedEvent,
    SpecialistContinuationWorkItem,
    StatusSnapshot,
    StyleRewriteAtomicApplyAgentStdout,
    StyleRewriteAtomicApplyResult,
    SubagentBatchPlan,
    TriagedRawChatsAvailableEvent,
    ValidationError,
    VersionControlSafety,
    WaitExternalEffectAdapter,
    WikiPathResolutionError,
    WikiPathResolutionPayload,
    WikiSubworkflowEffectAdapter,
    WorkflowEffect,
    WorkflowEffectExecutor,
    WorkflowEffectKind,
    WorkflowEffectResult,
    WorkflowEffectStatus,
    WorkflowProgressEvent,
    WorkflowProgressEventType,
    WorkflowProgressStatus,
    _now_iso,
    _path,
    _write_json_atomic,
    agent_output_ignored_notice,
    annotate_payload,
    apply_atomicity_split_bundle,
    apply_canonical_merge,
    apply_curator_batch_outputs,
    apply_note_merge,
    apply_semantic_ingestion,
    apply_style_rewrite,
    apply_style_rewrite_from_manifest,
    apply_taxonomy_migration,
    apply_vocabulary_recovery_plan,
    attach_human_decision_packet,
    audit_agent_transcript,
    audit_user_root_hygiene,
    build_curator_prompt_expectations_template,
    build_fix_wiki_fsm_result,
    build_link_fsm_result,
    build_process_chats_fsm_result,
    build_progress_view_model,
    build_vocabulary_recovery_plan,
    canonical_taxonomy_tree,
    clear_publish_dry_run,
    collect_curator_outputs,
    collect_style_rewrite_outputs,
    cooperative_cpu_yield_scope,
    cooperative_cpu_yield_settings_from_env,
    covered_raw_chat_index,
    curator_plan_hash,
    diagnose_publish_state,
    ensure_markdown_node_runtime,
    error_context,
    evaluate_agent_behavior_corpus,
    evaluate_body_linker_cases,
    evaluate_curator_prompt_outputs,
    evaluate_triager_prompt_output,
    file_sha256,
    finalize_agy_specialist_task,
    finalize_collect_apply_style_rewrite,
    finalize_opencode_architect_task,
    finalize_opencode_specialist_task,
    finalize_style_rewrite_apply_receipt,
    finalize_style_rewrite_atomic_apply_result,
    finalize_style_rewrite_output,
    fix_note_style_file,
    fix_wiki_agent_stdout_report,
    fix_wiki_cli_exit_code,
    fix_wiki_fsm_facts_from_runtime,
    fix_wiki_health,
    fold_progress_events,
    graph_audit,
    happy_path_round_metrics,
    harden_operational_payload,
    link_cli_exit_code,
    link_fsm_facts_from_linker_result,
    link_related_cli_exit_code,
    link_related_fsm_payload_from_sync_result,
    list_by_status,
    load_curator_prompt_expectations,
    load_triager_prompt_expectations,
    markdown_node_runtime_status,
    mutate_raw_frontmatter,
    plan_subagents,
    process_chats_cli_exit_code,
    process_chats_fsm_payload_from_publish_result,
    promote_curator_prompt_baseline,
    publish_batch_operation_result,
    read_note_meta,
    record_publish_dry_run,
    recover_related_notes_export_operation_result,
    require_publish_dry_run,
    resolve_config,
    resolve_taxonomy,
    rollback_taxonomy_migration,
    run_linker,
    serialize_triage_note_plan,
    stage_note,
    style_rewrite_manifest_required_receipt,
    suggest_agent_behavior_cases_from_evidence,
    suggest_agent_behavior_cases_from_telemetry,
    sync_related_notes_operation_result,
    taxonomy_audit,
    taxonomy_migration_plan,
    taxonomy_new_leaf_authorization_for_manifest,
    taxonomy_status,
    taxonomy_tree,
    validate_agent_behavior_report_path,
    validate_agent_run_report,
    validate_config,
    validate_curator_batch_outputs,
    validate_note_artifacts,
    validate_note_style_file,
    validate_triager_prompt_eval_for_note_plan,
    validate_wiki_style,
    vocabulary_status,
    wiki_cli_relative_command,
    write_trigger_context,
)
from mednotes.domains.wiki.api import _json as _emit_json
from mednotes.kernel.base import ContractModel
from mednotes.kernel.public_report import WorkflowPublicReport
from mednotes.kernel.state_machine import WorkflowStateCategory, WorkflowStateMachineSnapshot
from mednotes.platform.feedback import (
    agent_preamble_lines,
    command_string,
    safe_record_workflow_run,
    validate_agent_tool_calls,
)
from mednotes.platform.paths import (
    environment_preflight,
    plan_set_paths,
    repair_config_template,
    resolve_raw_dir,
    user_state_dir,
)
from mednotes.platform.vault_guard import VaultGuardError, active_guard_exists, require_vault_guard

STAGE_NOTE_REQUIRED_INPUTS = ["raw_file", "taxonomy", "title", "content_path", "coverage_path"]
LiteralRelatedMode = Literal["dry_run", "apply", "recover_export"]


class _EnvironmentPreflightCliFields(ContractModel):
    """Typed lens for platform preflight output before the CLI renders status JSON."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    persistent_venv: str = ""
    blockers: list[object] = Field(default_factory=list)


class _StatusValidationCliFields(ContractModel):
    """Typed view of validate_config output consumed by `/mednotes:status`."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    raw_dir: str = ""
    raw_dir_exists: bool = False
    wiki_dir: str = ""
    wiki_dir_exists: bool = False
    wiki_source: str = ""
    wiki_memory_path: str = ""
    config_path: str = ""
    catalog_path: str = ""
    catalog_path_exists: bool = False
    vocabulary_db_path: str = ""
    vocabulary_db_exists: bool = False
    path_resolution: JsonObject = Field(default_factory=dict)
    environment_preflight: JsonObject = Field(default_factory=dict)
    human_decision_required: bool = False


class _MarkdownQueryCliFields(ContractModel):
    """Shared status slice for markdown-query runtime and adapter reports."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    node_modules_path: str = ""


class _StyleRewriteValidationIssueFields(ContractModel):
    """Style validation issue code used in compact agent-facing summaries."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""


class _StyleRewriteValidationFields(ContractModel):
    """Validation slice carried by direct style-rewrite apply payloads."""

    model_config = ConfigDict(extra="ignore")

    errors: list[_StyleRewriteValidationIssueFields] = Field(default_factory=list)
    warnings: list[_StyleRewriteValidationIssueFields] = Field(default_factory=list)
    requires_llm_rewrite: bool = False


class _StyleRewriteDirectApplyFields(ContractModel):
    """Typed lens for the direct apply_style_rewrite result before receipt wrapping."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    phase: str = ""
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    linker_skipped_reason: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    target_path: str = ""
    content_path: str = ""
    dry_run: bool = False
    changed: bool = False
    written: bool = False
    backup_path: str | None = None
    deterministic_fixes_applied: list[object] = Field(default_factory=list)
    validation: _StyleRewriteValidationFields = Field(default_factory=_StyleRewriteValidationFields)


class _StyleRewriteSummaryItemFields(ContractModel):
    """Compact item shape used only for agent stdout summaries."""

    model_config = ConfigDict(extra="ignore")

    work_id: str = ""
    target_path: str = ""
    output_path: str = ""
    content_path: str = ""
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    changed: bool = False
    written: bool = False
    backup_path: str | None = None
    deterministic_fixes_applied: list[object] = Field(default_factory=list)
    validation: JsonObject = Field(default_factory=dict)


class _StyleRewritePlanStepCliFields(ContractModel):
    """Typed next-step effect payload for style-rewrite planning."""

    model_config = ConfigDict(extra="ignore")

    command_family: str = ""
    arguments: list[object] = Field(default_factory=list)
    agent_instruction: str = ""


class _SpecialistContinuationSourceFields(ContractModel):
    """Plan work item before it is validated as SpecialistContinuationWorkItem."""

    model_config = ConfigDict(extra="ignore")

    work_id: str = ""
    phase: str = "style-rewrite"
    agent: str = "med-knowledge-architect"
    item_type: str | None = None
    target_path: str = ""
    target_hash_before: str = ""
    title: str | None = None
    rewrite_prompt: str = ""
    model_policy: str | None = None
    required_model_tier: str = ""
    preferred_model_tier: str | None = None
    temp_output: str = ""
    specialist_task_run_receipt_path: str | None = None
    subagent_output_contract: JsonObject = Field(default_factory=dict)


class _StyleRewriteLinkerCheckpointFields(ContractModel):
    """Linker status slice included in style-rewrite checkpoint messaging."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    diagnosis_blocked_reason: str = ""
    apply_blocked_reason: str = ""
    linker_skipped_reason: str = ""


class _PlanAttestationCliFields(ContractModel):
    """Plan attestation slice copied into plan-output receipts."""

    model_config = ConfigDict(extra="ignore")

    plan_hash: str = ""


class _WorkflowStatusCliFields(ContractModel):
    """Generic status/blocker slice for CLI decisions that are not workflow state."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    skipped_reason: str = ""
    next_action: str = ""
    run_id: str = ""
    mode: str = ""
    plan_hash: str = ""
    snapshot_hash: str = ""
    phase: str = ""
    schema_id: str = Field(default="", alias="schema")
    error_context: JsonObject = Field(default_factory=dict)


class _WorkflowEffectResultsCliFields(ContractModel):
    """Optional effect-result collection carried by compact linker summaries."""

    model_config = ConfigDict(extra="ignore")

    workflow_effect_results: list[object] = Field(default_factory=list)


class _LinkTriggerContextCliFields(ContractModel):
    """Trigger-context identity used before launching the link subworkflow."""

    model_config = ConfigDict(extra="ignore")

    source_workflow: str = "/mednotes:link"


class _TaxonomyOperationCliFields(ContractModel):
    """Taxonomy operation slice used to create link trigger context."""

    model_config = ConfigDict(extra="ignore")

    action: str = ""
    source: str = ""
    destination: str = ""


class _ReferenceRepairSummaryFields(ContractModel):
    """Reference-repair summary projected into compact linker payloads."""

    model_config = ConfigDict(extra="ignore")

    status: str = "skipped"
    affected_note_count: int = Field(default=0, ge=0)
    action_count: int = Field(default=0, ge=0)
    blocking_action_count: int = Field(default=0, ge=0)
    human_decision_count: int = Field(default=0, ge=0)
    triage_count: int = Field(default=0, ge=0)
    package_mode: str = "diagnosis_bound"
    manual_script_allowed: bool = False
    requires_backup: bool = False
    requires_receipt: bool = True
    note_actions: list[JsonObject] = Field(default_factory=list)
    structural_actions: list[JsonObject] = Field(default_factory=list)
    catalog_actions: list[JsonObject] = Field(default_factory=list)


class _CompactRelatedNotesCliFields(ContractModel):
    """Related Notes slice allowed to influence compact linker UX/state."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""


class _LinkerDecisionSummaryCarrierFields(ContractModel):
    """Blocker item slice that may carry a typed decision summary."""

    model_config = ConfigDict(extra="ignore")

    decision_summary: JsonObject = Field(default_factory=dict)


class _LinkerBodyTermCliFields(ContractModel):
    """Body-linker counters used when compacting linker output."""

    model_config = ConfigDict(extra="ignore")

    contextual_alias_disambiguation: object | None = None
    links_planned: object | None = None
    links_rewritten: object | None = None


class _IssueSummaryFields(ContractModel):
    """Issue code/count slice used for compact grouped summaries."""

    model_config = ConfigDict(extra="ignore")

    code: str = "unknown"
    count: int = Field(default=0, ge=0)


class _RuntimeErrorContextCliFields(ContractModel):
    """Operational error context fields required by runtime-specific projections."""

    model_config = ConfigDict(extra="ignore")

    phase: str = ""
    blocked_reason: str = ""
    root_cause: str = ""
    affected_artifact: str = ""
    error_summary: str = ""
    suggested_fix: str = ""
    next_action: str = ""
    retry_scope: str = ""
    traceback_summary: str = ""
    missing_inputs: list[str] = Field(default_factory=list)


class _UnexpectedErrorPayloadCliFields(ContractModel):
    """Exception payload slice used only for stderr rendering."""

    model_config = ConfigDict(extra="ignore")

    diagnostic_context: _RuntimeErrorContextCliFields = Field(default_factory=_RuntimeErrorContextCliFields)


class _ProcessChatsRunCliFields(ContractModel):
    """Publish-result identity fields used to derive process-chats run ids."""

    model_config = ConfigDict(extra="ignore")

    manifest_hash: str = ""


class _ProcessChatsRawUpdateSafetyFields(ContractModel):
    """Raw update slice used only to count vault mutations."""

    model_config = ConfigDict(extra="ignore")

    raw_file: str = ""
    updated: bool = Field(default=False, strict=True)


class _ProcessChatsMutationSafetyFields(ContractModel):
    """Minimal process-chats mutation evidence independent from FSM observation."""

    model_config = ConfigDict(extra="ignore")

    created: list[str] = Field(default_factory=list)
    processed_raw_count: int = Field(default=0, ge=0, strict=True)
    raw_updates: list[_ProcessChatsRawUpdateSafetyFields] = Field(default_factory=list)


class _TriageMetaCliFields(ContractModel):
    """Triage metadata carried by raw chat rows during stage-note preservation."""

    model_config = ConfigDict(extra="ignore")

    data_importacao: str = ""
    titulo_triagem: str = ""


class _HappyPathValidationEnvelopeFields(ContractModel):
    """Validation artifact envelope used by happy-path round summaries."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    happy_path_metrics: JsonObject | None = None


class _VaultGuardCliBlockFields(ContractModel):
    """Vault guard failure slice projected into public FSM error context."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    human_message: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)


class _PathResolutionCliBlockFields(ContractModel):
    """Path-resolution exception slice after compatibility blocker enrichment."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)


class _KnownErrorFeedbackPayloadFields(ContractModel):
    """Known-error payload slice allowed to cross from legacy exception code."""

    model_config = ConfigDict(extra="ignore")

    error_context: JsonObject = Field(default_factory=dict)


class _HumanDecisionExceptionPacketFields(ContractModel):
    """Minimal typed view of decision packets attached to domain exceptions."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    status: str = ""
    resume_action: str = ""


PROCESS_CHATS_COMMANDS = {
    "list-pending",
    "list-triados",
    "plan-subagents",
    "eval-triager-output",
    "triage",
    "discard",
    "stage-note",
    "publish-batch",
    "publish-status",
    "validate-note",
    "fix-note",
    "finalize-opencode-architect-task",
    "apply-canonical-merge",
}
FIX_WIKI_COMMANDS = {
    "taxonomy-canonical",
    "taxonomy-tree",
    "taxonomy-audit",
    "taxonomy-status",
    "taxonomy-plan",
    "taxonomy-apply",
    "taxonomy-rollback",
    "taxonomy-migrate",
    "taxonomy-resolve",
    "graph-audit",
    "validate-wiki",
    "fix-wiki",
    "apply-note-merge",
    "apply-atomicity-split",
    "apply-style-rewrite",
    "apply-specialist-style-rewrite",
    "collect-style-rewrite-outputs",
    "finalize-agy-specialist-task",
    "finalize-opencode-specialist-task",
    "finalize-style-rewrite-output",
}
ERROR_CONTEXT_CONTRACT_COMMANDS = (
    PROCESS_CHATS_COMMANDS
    | FIX_WIKI_COMMANDS
    | {
        "apply-curator-batch",
        "apply-semantic-ingestion",
        "collect-curator-outputs",
        "related-notes-sync",
        "run-linker",
        "vocabulary-recover",
        "vocabulary-status",
    }
)
FSM_FIRST_EXCEPTION_COMMANDS = {
    "fix-wiki",
    "run-linker",
    "related-notes-sync",
    "list-pending",
    "list-triados",
    "publish-batch",
}
PROCESS_CHATS_RECOVERABLE_EXCEPTION_REASONS = frozenset(
    {
        "coverage_path_missing",
        "coverage_invalid",
        "manifest_invalid",
        "manifest_mismatch",
        "validation_errors",
        "validation_failed",
        "requires_llm_rewrite",
        "dry_run_receipt_required",
        "dry_run_receipt_invalid",
        "new_taxonomy_leaf_requires_dry_run_authorization",
        "stale_receipt",
        "duplicate_target",
        "duplicate_obsidian_target",
        "provenance_gap",
        "publish_receipt_invalid",
    }
)


def _add_common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--config", default=default, help="Optional config.toml. Reads [chat_processor].")
    parser.add_argument("--raw-dir", default=default, help="Override Chats_Raw directory.")
    parser.add_argument("--wiki-dir", default=default, help="Override Wiki_Medicina directory.")
    parser.add_argument("--catalog-path", default=default, help="Override CATALOGO_WIKI.json path.")
    parser.add_argument("--vocabulary-db", default=default, help="Override vocabulary SQLite DB path.")
    parser.add_argument("--artifact-dir", default=default, help="Directory containing gemini-md-export HTML artifact manifests.")


def _workflow_for_command(command: str) -> str:
    if command in PROCESS_CHATS_COMMANDS:
        return "/mednotes:process-chats"
    if command in FIX_WIKI_COMMANDS:
        return "/mednotes:fix-wiki"
    if command == "run-linker":
        return "/mednotes:link"
    if command == "apply-semantic-ingestion":
        return "/mednotes:link"
    if command in {"vocabulary-status", "vocabulary-recover"}:
        return "/mednotes:link"
    if command in {"eval-curator-batch", "apply-curator-batch", "collect-curator-outputs"}:
        return "/mednotes:link"
    if command == "eval-curator-batch":
        return "/mednotes:link"
    if command == "evaluate-body-linker":
        return "/mednotes:link"
    if command == "apply-atomicity-split":
        return "/mednotes:fix-wiki"
    if command == "apply-note-merge":
        return "/mednotes:fix-wiki"
    if command in {"status", "validate"}:
        return "/mednotes:status"
    if command == "root-hygiene-audit":
        return "/mednotes:status"
    if command in {"markdown-query-status", "markdown-query-probe"}:
        return "/mednotes:status"
    if command == "markdown-query-rebuild":
        return "/mednotes:setup"
    if command in {"set-paths", "repair-config-template"}:
        return "/mednotes:setup"
    return f"wiki-cli:{command}"


def _workflow_for_args(args: argparse.Namespace) -> str:
    command = str(getattr(args, "command", "unknown"))
    phase = str(getattr(args, "phase", ""))
    if command == "plan-subagents" and phase == "vocabulary-curation":
        return "/mednotes:link"
    if command == "plan-subagents" and phase in {"style-rewrite", "note-merge", "atomicity-split"}:
        return "/mednotes:fix-wiki"
    if command == "plan-subagents":
        return "/mednotes:process-chats"
    if command == "eval-triager-output":
        return "/mednotes:process-chats"
    return _workflow_for_command(command)


def _path_is_inside(candidate: Path, root: Path) -> bool:
    try:
        left = candidate.expanduser().resolve(strict=False)
        right = root.expanduser().resolve(strict=False)
    except OSError:
        left = candidate.expanduser().absolute()
        right = root.expanduser().absolute()
    if sys.platform == "win32":
        left_text = str(left).lower()
        right_text = str(right).lower()
    else:
        left_text = str(left)
        right_text = str(right)
    separator = "\\" if sys.platform == "win32" else "/"
    return left_text == right_text or left_text.startswith(right_text.rstrip("/\\") + separator)


def command_may_require_guard(args: argparse.Namespace) -> bool:
    command = str(getattr(args, "command", ""))
    if command in {"triage", "discard", "publish-batch"}:
        return not bool(getattr(args, "dry_run", False))
    if command == "run-linker":
        return bool(getattr(args, "apply", False))
    if command == "related-notes-sync":
        return bool(getattr(args, "apply", False))
    if command == "fix-wiki":
        return bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False))
    if command == "taxonomy-migrate":
        return bool(getattr(args, "apply", False) or getattr(args, "rollback", False))
    if command in {"taxonomy-apply", "taxonomy-rollback", "apply-atomicity-split"}:
        return True
    if command in {"apply-style-rewrite", "apply-specialist-style-rewrite"}:
        return not bool(getattr(args, "dry_run", False))
    if command == "apply-note-merge":
        return not bool(getattr(args, "dry_run", False))
    return False


def direct_guard_target(args: argparse.Namespace) -> Path | None:
    command = str(getattr(args, "command", ""))
    if command == "fix-note":
        output = getattr(args, "output", "")
        return _path(output) if output else None
    if command == "apply-style-rewrite" and not bool(getattr(args, "dry_run", False)):
        target = getattr(args, "target", "")
        return _path(target) if target else None
    return None


def command_requires_guard(args: argparse.Namespace, config: MedConfig) -> bool:
    command = str(getattr(args, "command", ""))
    if command in {"triage", "discard"}:
        return not bool(getattr(args, "dry_run", False))
    if command == "publish-batch":
        return not bool(getattr(args, "dry_run", False))
    if command == "run-linker":
        return bool(getattr(args, "apply", False))
    if command == "related-notes-sync":
        return bool(getattr(args, "apply", False))
    if command == "fix-note":
        output = getattr(args, "output", "")
        return bool(output) and _path_is_inside(_path(output), config.wiki_dir)
    if command == "fix-wiki":
        return bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False))
    if command == "taxonomy-migrate":
        return bool(getattr(args, "apply", False) or getattr(args, "rollback", False))
    if command in {"taxonomy-apply", "taxonomy-rollback", "apply-atomicity-split"}:
        return True
    if command in {"apply-style-rewrite", "apply-specialist-style-rewrite"}:
        return not bool(getattr(args, "dry_run", False))
    if command == "apply-note-merge":
        return not bool(getattr(args, "dry_run", False))
    return False


def _hydrate_run_linker_apply_args_from_diagnosis(args: argparse.Namespace) -> None:
    if str(getattr(args, "command", "")) != "run-linker":
        return
    if not bool(getattr(args, "apply", False)):
        return
    diagnosis = getattr(args, "diagnosis", None)
    if not diagnosis:
        return
    try:
        payload = json.loads(_path(diagnosis).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    field_map = {
        "wiki_dir": "wiki_dir",
        "catalog_path": "catalog_path",
        "vocabulary_db": "vocabulary_db_path",
    }
    snapshot = _dict_value(_json_field(payload, "snapshot"))
    for arg_name, payload_key in field_map.items():
        if getattr(args, arg_name, None):
            continue
        value = _json_field(payload, payload_key) or _json_field(snapshot, payload_key)
        if isinstance(value, str) and value.strip():
            setattr(args, arg_name, value)


def _record_feedback(
    args: argparse.Namespace,
    payload: object,
    exit_code: int,
    started_at: float,
    *,
    snippets: list[object] | None = None,
) -> None:
    safe_record_workflow_run(
        workflow=_workflow_for_args(args),
        command=command_string(),
        payload=payload if isinstance(payload, dict) else {"status": "completed" if exit_code == 0 else "failed"},
        exit_code=exit_code,
        started_at=started_at,
        snippets=snippets,
    )


def _requires_operational_error_context(args: argparse.Namespace, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    fields = _WorkflowStatusCliFields.model_validate(payload)
    status = fields.status
    blocked_reason = fields.blocked_reason
    if status not in {"blocked", "failed"} and not blocked_reason:
        return False
    command = str(getattr(args, "command", ""))
    return command in ERROR_CONTEXT_CONTRACT_COMMANDS


def _add_taxonomy_creation_mode(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(allow_new_taxonomy_leaf=True)
    parser.add_argument(
        "--strict-existing-taxonomy",
        action="store_false",
        dest="allow_new_taxonomy_leaf",
        help="Require the final non-canonical taxonomy leaf to already exist.",
    )
    parser.add_argument(
        "--allow-new-taxonomy-leaf",
        action="store_true",
        dest="allow_new_taxonomy_leaf",
        help=argparse.SUPPRESS,
    )


def _read_json(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"JSON file not found: {path}") from exc
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            f"artifact_encoding.unsupported_utf16: JSON file {path} is UTF-16; "
            "regenerate it with the CLI --output/--plan-output/--report flags or write UTF-8 without BOM."
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            f"artifact_encoding.invalid_utf8: JSON file {path} must be UTF-8 or UTF-8 with BOM: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON file {path}: {exc}") from exc


def _read_json_argument(path_value: str) -> object:
    """Read a UTF-8 JSON file or stdin for projection-only adapter boundaries."""

    if path_value == "-":
        try:
            return json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON on stdin: {exc}") from exc
    return _read_json(_path(path_value))


def _dict_value(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _json_field(payload: Mapping[str, object], key: str, default: object | None = None) -> object | None:
    return payload[key] if key in payload else default


def _agent_preamble_enabled() -> bool:
    return os.environ.get("MEDNOTES_AGENT_PREAMBLE", "").strip().lower() == "stderr"


def _agent_stdout_compact_enabled() -> bool:
    mode = os.environ.get("MEDNOTES_AGENT_STDOUT", "").strip().lower()
    if mode in {"compact", "1", "true", "yes"}:
        return True
    if mode in {"full", "0", "false", "no"}:
        return False
    return _agent_preamble_enabled()


def _agent_directive_payload(
    *,
    workflow: str,
    run_id: str,
    status: str,
    state: str,
    reason: str,
    summary: str,
    instructions: list[str],
    phase: str = "",
    continue_now: bool = False,
    final_report: bool = False,
    effects: list[dict[str, object]] | None = None,
    blockers: list[str] | None = None,
    resume: str = "",
    report_requires: list[str] | None = None,
) -> dict[str, object]:
    return AgentDirective.model_validate(
        {
            "workflow": workflow,
            "run_id": run_id,
            "control": {
                "status": status,
                "state": state,
                "phase": phase,
                "reason": reason,
                "capabilities": {"continue": continue_now, "final_report": final_report},
                "effects": effects or [],
                "blockers": blockers or [],
                "resume": resume,
                "report": {"requires": report_requires or ["public_report"]},
                "limits": {"raw_content": False, "absolute_paths": False, "ad_hoc_scripts": False},
            },
            "summary": summary,
            "instructions": instructions,
        }
    ).to_payload()


def _running_agent_preamble_lines(*, label: str, workflow: str, phase: str) -> list[str]:
    event = WorkflowProgressEvent(
        workflow=workflow or "/mednotes",
        run_id="pending-final-json",
        state="running",
        phase=phase or "workflow_running",
        event_type=WorkflowProgressEventType.WORKFLOW_STARTED,
        status=WorkflowProgressStatus.RUNNING,
        message=f"{label} em andamento; aguardando resultado final.",
    )
    view_model = build_progress_view_model(fold_progress_events([event]))
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.workflow-running-preamble.v1",
        "workflow": event.workflow,
        "run_id": event.run_id,
        "progress_view_model": view_model.to_payload(),
        "state_machine_snapshot": {
            "workflow": event.workflow,
            "run_id": event.run_id,
            "current_category": "running",
            "current_state": event.state,
        },
        "agent_directive": _agent_directive_payload(
            workflow=event.workflow,
            run_id=event.run_id,
            status="running",
            state=event.state,
            phase=view_model.phase,
            reason="workflow_running",
            continue_now=True,
            final_report=False,
            summary=f"{label} em andamento; aguardando resultado final.",
            instructions=[
                "aguarde o JSON final antes de concluir.",
                "tool status=running nao e sucesso do workflow.",
                "nao execute probes, git status ou comandos alternativos enquanto esta task estiver rodando.",
            ],
        ),
    }
    return agent_preamble_lines(payload)


def _emit_running_agent_preamble(*, label: str, workflow: str, phase: str) -> None:
    if not _agent_preamble_enabled():
        return
    try:
        lines = _running_agent_preamble_lines(label=label, workflow=workflow, phase=phase)
        if lines:
            print("\n".join(lines), file=sys.stderr, flush=True)
    except Exception:
        # Optional agent-facing output must not affect stdout JSON or exit semantics.
        return


@contextmanager
def _workflow_heartbeat(
    label: str,
    *,
    workflow: str = "/mednotes",
    phase: str = "workflow_running",
    interval_seconds: float = 60.0,
):
    if os.getenv("MEDNOTES_WORKFLOW_HEARTBEAT", "1").strip().lower() in {"0", "false", "no"}:
        yield
        return
    print(f"[mednotes] {label} em andamento; aguardando resultado final.", file=sys.stderr, flush=True)
    _emit_running_agent_preamble(label=label, workflow=workflow, phase=phase)
    stop = threading.Event()

    def emit() -> None:
        while not stop.wait(interval_seconds):
            print(f"[mednotes] {label} ainda em andamento.", file=sys.stderr, flush=True)
            _emit_running_agent_preamble(label=label, workflow=workflow, phase=phase)

    thread = threading.Thread(target=emit, name="mednotes-workflow-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=0.2)


def _list_value(value: object) -> list[JsonValue]:
    return JsonArrayAdapter.validate_python(value) if isinstance(value, list) else []


def _str_list_value(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _namespace_string(args: argparse.Namespace, name: str, default: str = "") -> str:
    """Normalize optional argparse fields before they drive operational payloads."""

    value = getattr(args, name, default)
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return f"{value}".strip()


def _int_value(value: object, *, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _link_fsm_payload_from_result(result: Mapping[str, object], args: argparse.Namespace) -> dict[str, object]:
    applying = bool(getattr(args, "apply", False))
    include_related_notes = not bool(getattr(args, "no_related_notes", False))
    # The linker FSM consumes the canonical operation payload directly. A
    # compact report view must never reclassify runtime state before event
    # selection.
    operation_result = LinkerRunResult.from_payload(result).operation_payload
    return build_link_fsm_result(
        link_fsm_facts_from_linker_result(
            operation_result,
            run_id=_link_run_id(operation_result),
            mode="apply" if applying else "diagnose",
            include_related_notes=include_related_notes,
            version_control_safety=_link_version_control_safety(operation_result, applying=applying),
        )
    ).to_payload()


def _workflow_effect_executor(config: MedConfig) -> WorkflowEffectExecutor:
    link_adapter = LinkWorkflowEffectAdapter(config=config, run_linker_fn=run_linker)
    related_adapter = RelatedNotesEffectAdapter(config=config)
    wait_external_adapter = WaitExternalEffectAdapter()
    subworkflow_adapter = WikiSubworkflowEffectAdapter(
        link_adapter=link_adapter,
        related_notes_adapter=related_adapter,
    )
    return WorkflowEffectExecutor(
        adapters={
            WorkflowEffectKind.RUN_SUBWORKFLOW: subworkflow_adapter,
            WorkflowEffectKind.WAIT_EXTERNAL: wait_external_adapter,
        }
    )


def _link_run_id(result: dict[str, object]) -> str:
    fields = _WorkflowStatusCliFields.model_validate(result)
    for key in ("run_id", "plan_hash", "snapshot_hash"):
        value = getattr(fields, key).strip()
        if value:
            safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value)[:48].strip("-")
            return f"link-{safe}" if safe else "link-run"
    return f"link-{int(time.time() * 1000)}"


def _link_result_with_guard_safety(
    config: MedConfig,
    result: JsonObject,
    *,
    applying: bool,
) -> JsonObject:
    """Attach active guard evidence for a mutating link result before FSM projection."""

    if not applying or _version_control_safety_payload(result):
        return result
    report = LinkerRunResult.from_payload(result)
    changed_file_count = max(report.files_changed, len(report.changed_files))
    if changed_file_count <= 0 or not active_guard_exists(config.wiki_dir):
        return result
    enriched = dict(result)
    enriched["version_control_safety"] = _active_guard_version_control_safety_payload(
        changed_file_count=changed_file_count,
    )
    return JsonObjectAdapter.validate_python(enriched)


def _active_guard_version_control_safety_payload(*, changed_file_count: int) -> JsonObject:
    """Represent the currently active vault guard without claiming run-finish."""

    return JsonObjectAdapter.validate_python({
        "resource_guard_active": True,
        "run_start_seen": True,
        "run_finish_seen": False,
        "restore_point_before": "vault-guard",
        "restore_point_after": "",
        "sync_status": "pending_run_finish",
        "backup_online": "pending_run_finish",
        "direct_mutation_forbidden": True,
        "mutation_without_guard": False,
        "rollback_declared": True,
        "no_resource_mutation": False,
        "changed_file_count": changed_file_count,
    })


def _link_version_control_safety(result: object, *, applying: bool) -> VersionControlSafety:
    report = LinkerRunResult.from_payload(result)
    files_changed = report.files_changed or len(report.changed_files)
    return _version_control_safety_from_evidence(
        report.operation_payload,
        applying=applying,
        changed_file_count=files_changed,
    )


def _process_chats_publish_operation_payload(result: Mapping[str, object]) -> dict[str, object]:
    """Return the closed process-chats operation payload consumed by the FSM."""

    return {
        key: value
        for key, value in result.items()
        if key not in {"version_control_safety", "guard_receipt", "receipt"}
    }


def _process_chats_fsm_payload_from_result(result: dict[str, object], manifest: Path, *, applying: bool) -> dict[str, object]:
    operation_result = _process_chats_publish_operation_payload(result)
    return process_chats_fsm_payload_from_publish_result(
        operation_result,
        run_id=_process_chats_run_id(result, manifest),
        version_control_safety=_process_chats_version_control_safety(result, applying=applying),
    )


def _process_chats_publish_operation_paused(result: Mapping[str, object]) -> bool:
    """Decide CLI early-exit from the canonical runtime observation, not root status."""

    operation_result = ProcessChatsPublishOperationResult.model_validate(
        _process_chats_publish_operation_payload(result)
    )
    observation = operation_result.runtime_observation
    return bool(
        observation.blocked
        or observation.quota_wait
        or observation.rollback_recorded
        or observation.link_blocked
        or observation.validation_coverage_gap
        or observation.validation_manifest_mismatch
        or observation.validation_content_invalid
        or observation.publish_dry_run_receipt_required
        or observation.publish_stale_receipt
        or observation.publish_duplicate_target
        or observation.publish_provenance_gap
    )


def _process_chats_run_id(result: dict[str, object], manifest: Path) -> str:
    fields = _ProcessChatsRunCliFields.model_validate(result)
    basis = fields.manifest_hash
    if not basis:
        try:
            basis = file_sha256(manifest)
        except OSError:
            basis = manifest.name
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", basis)[:48].strip("-")
    return f"process-chats-{safe or 'run'}"


def _process_chats_result_with_guard_safety(
    config: MedConfig,
    result: JsonObject,
    *,
    applying: bool,
) -> JsonObject:
    """Attach vault-guard evidence to mutating process-chats publish results."""

    if not applying or _version_control_safety_payload(result):
        return result
    publish_result = ProcessChatsPublishOperationResult.model_validate(result)
    changed_files = set(publish_result.created)
    changed_files.update(update.raw_file for update in publish_result.raw_updates if update.updated)
    changed_file_count = len(changed_files)
    if changed_file_count <= 0 or not active_guard_exists(config.wiki_dir):
        return result
    enriched = dict(result)
    enriched["version_control_safety"] = _active_guard_version_control_safety_payload(
        changed_file_count=changed_file_count,
    )
    return JsonObjectAdapter.validate_python(enriched)


def _process_chats_version_control_safety(result: object, *, applying: bool) -> VersionControlSafety:
    raw_payload = _dict_value(result)
    mutation_fields = _ProcessChatsMutationSafetyFields.model_validate(raw_payload)
    changed_files = set(mutation_fields.created)
    changed_files.update(update.raw_file for update in mutation_fields.raw_updates if update.updated)
    changed_file_count = len(changed_files)
    return _version_control_safety_from_evidence(
        raw_payload,
        applying=applying,
        changed_file_count=changed_file_count,
    )


def _link_related_fsm_payload_from_result(
    result: dict[str, object],
    *,
    mode: LiteralRelatedMode,
    applying: bool,
) -> dict[str, object]:
    sync_result = LinkRelatedSyncResult.from_payload(result)
    return link_related_fsm_payload_from_sync_result(
        JsonObjectAdapter.validate_python(result),
        run_id=_link_related_run_id(sync_result),
        mode=mode,
        version_control_safety=_link_related_version_control_safety(sync_result, applying=applying),
    )


def _link_related_result_with_guard_safety(
    config: MedConfig,
    result: JsonObject,
    *,
    applying: bool,
) -> JsonObject:
    """Attach active vault-guard evidence before projecting Related Notes apply."""

    if not applying or _version_control_safety_payload(result):
        return result
    sync_result = LinkRelatedSyncResult.from_payload(result)
    changed_update_count = sum(1 for update in sync_result.updates if update.changed)
    changed_file_count = max(sync_result.applied_note_count, changed_update_count)
    if changed_file_count <= 0 or not active_guard_exists(config.wiki_dir):
        return result
    enriched = dict(result)
    enriched["version_control_safety"] = _active_guard_version_control_safety_payload(
        changed_file_count=changed_file_count,
    )
    return JsonObjectAdapter.validate_python(enriched)


def _link_related_run_id(result: LinkRelatedSyncResult) -> str:
    basis = result.export_path or result.receipt_path or result.blocked_reason or "run"
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", basis)[:48].strip("-")
    return f"link-related-{safe or 'run'}"


def _link_related_version_control_safety(result: LinkRelatedSyncResult, *, applying: bool) -> dict[str, object]:
    changed_update_count = sum(1 for update in result.updates if update.changed)
    changed_file_count = max(result.applied_note_count, changed_update_count)
    return _version_control_safety_from_evidence(
        result.operation_payload,
        applying=applying,
        changed_file_count=changed_file_count,
    ).to_payload()


def _version_control_safety_from_evidence(
    payload: JsonObject,
    *,
    applying: bool,
    changed_file_count: int,
) -> VersionControlSafety:
    """Normalize guard evidence without fabricating it from apply/count fields."""

    safety_payload = _version_control_safety_payload(payload)
    if safety_payload:
        safety = VersionControlSafety.model_validate(safety_payload)
        if safety.changed_file_count == changed_file_count:
            return safety
        raise ValidationError(
            "version_control_safety_evidence_mismatch: guard evidence changed_file_count "
            "does not match operation result"
        )
    if applying and changed_file_count > 0:
        raise ValidationError("version_control_safety_evidence_missing: mutating workflow result lacks guard evidence")
    return VersionControlSafety(
        resource_guard_active=False,
        run_start_seen=False,
        run_finish_seen=False,
        restore_point_before="",
        restore_point_after="",
        sync_status="not_checked",
        backup_online="not_checked",
        direct_mutation_forbidden=True,
        mutation_without_guard=False,
        rollback_declared=False,
        no_resource_mutation=True,
        changed_file_count=0,
    )


def _version_control_safety_payload(payload: JsonObject) -> JsonObject:
    """Find safety evidence only where guarded runtimes are allowed to emit it."""

    direct = _dict_value(_json_field(payload, "version_control_safety"))
    if direct:
        return direct
    receipt = _dict_value(_json_field(payload, "receipt"))
    nested = _dict_value(_json_field(receipt, "version_control_safety"))
    if nested:
        return nested
    guard_receipt = _dict_value(_json_field(payload, "guard_receipt"))
    return _dict_value(_json_field(guard_receipt, "version_control_safety"))


def _process_chats_public_report_payload(
    *,
    run_id: str,
    headline: str,
    lines: list[str],
) -> JsonObject:
    """Build the only public wording channel for process-chats helper payloads."""

    return WorkflowPublicReport(
        workflow="/mednotes:process-chats",
        run_id=run_id,
        headline=headline,
        lines=lines,
    ).to_payload()


def _process_chats_empty_backlog_contract(
    *,
    mode: str,
    pending_count: int | None = None,
    triaged_count: int | None = None,
) -> dict[str, object]:
    run_id = f"process-chats-{mode}-empty-backlog"
    pending = int(pending_count or 0)
    triaged = int(triaged_count or 0)
    if mode == "pending" and triaged > 0:
        initial_state = ProcessChatsState.ENVIRONMENT_CHECKING
        event = TriagedRawChatsAvailableEvent(
            run_id=run_id,
            current_state=initial_state.value,
            triaged_count=triaged,
        )
        summary = ProcessChatsOperationalSummary(planned_note_count=triaged)
    elif mode == "triados":
        initial_state = ProcessChatsState.TRIAGE_PLANNING
        event = NoTriagedRawChatsEvent(
            run_id=run_id,
            current_state=initial_state.value,
            pending_count=pending,
        )
        summary = ProcessChatsOperationalSummary()
    else:
        initial_state = ProcessChatsState.ENVIRONMENT_CHECKING
        event = NoPendingRawChatsEvent(
            run_id=run_id,
            current_state=initial_state.value,
            triaged_count=triaged,
        )
        summary = ProcessChatsOperationalSummary()
    result = build_process_chats_fsm_result(
        ProcessChatsFsmFacts(
            run_id=run_id,
            initial_state=initial_state,
            event=event,
            operational_summary=summary,
            version_control_safety=VersionControlSafety(
                resource_guard_active=False,
                run_start_seen=False,
                run_finish_seen=False,
                restore_point_before="",
                restore_point_after="",
                sync_status="not_checked",
                backup_online="not_checked",
                direct_mutation_forbidden=True,
                mutation_without_guard=False,
                rollback_declared=False,
                no_resource_mutation=True,
                changed_file_count=0,
            ),
        )
    )
    return result.to_payload()


def _status_payload(
    rows: list[dict[str, str]],
    mode: str,
    args: argparse.Namespace,
    *,
    backlog_counts: dict[str, int] | None = None,
) -> list[dict[str, str]] | dict[str, object]:
    limit = max(0, int(getattr(args, "limit", 0) or 0))
    if getattr(args, "summary", False):
        sample_limit = limit or 20
        if mode in {"pending", "triados"} and not rows:
            return _process_chats_empty_backlog_contract(
                mode=mode,
                pending_count=backlog_counts.get("pending") if backlog_counts else None,
                triaged_count=backlog_counts.get("triados") if backlog_counts else None,
            )
        payload: dict[str, object] = {"mode": mode, "count": len(rows), "sample": rows[:sample_limit]}
        if backlog_counts is not None:
            payload["backlog_counts"] = backlog_counts
        return payload
    if limit:
        return rows[:limit]
    return rows


def _preserve_triage_import_date(existing_meta: Mapping[str, str]) -> str:
    fields = _TriageMetaCliFields.model_validate(dict(existing_meta))
    existing = fields.data_importacao.strip()
    return existing or date.today().isoformat()


def _preserve_richer_triage_title(existing_meta: Mapping[str, str], requested_title: str) -> str:
    fields = _TriageMetaCliFields.model_validate(dict(existing_meta))
    existing = fields.titulo_triagem.strip()
    requested = requested_title.strip()
    if not existing:
        return requested
    if not requested:
        return existing
    if existing.casefold().startswith(requested.casefold()) and len(existing) > len(requested):
        return existing
    return requested


def _auto_link_run_dir(label: str) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in label).strip("-") or "workflow"
    run_dir = user_state_dir() / "runs" / f"{int(time.time() * 1000)}-{safe_label}-link"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _link_subworkflow_effect(
    *,
    source_workflow: str,
    run_id: str,
    effect_id: str,
    diagnose: bool,
    apply: bool,
    diagnosis_path: Path,
    receipt_path: Path | None,
    trigger_context_path: Path | None,
    include_related_notes: bool,
    label: str,
    version_control_safety: VersionControlSafety | None = None,
) -> WorkflowEffect:
    payload = LinkWorkflowRunEffectPayload(
        kind="link_run" if apply else "diagnose",
        diagnose=diagnose,
        apply=apply,
        diagnosis_path=str(diagnosis_path),
        receipt_path=str(receipt_path) if receipt_path is not None else "",
        trigger_context_path=str(trigger_context_path) if trigger_context_path is not None else "",
        no_related_notes=not include_related_notes,
        llm_disambiguation="auto",
        version_control_safety=version_control_safety,
    ).to_payload()
    return WorkflowEffect(
        workflow=source_workflow or "/mednotes:link",
        run_id=run_id,
        effect_id=effect_id,
        origin_state="run_linker_package",
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:link",
        payload=payload,
        mutates_resources=apply,
        rollback_declared=apply,
        no_resource_mutation=not apply,
        requires_receipt=False,
        metadata={"label": label},
    )


def _link_runtime_result_from_effect_result(result: WorkflowEffectResult) -> LinkerRunResult:
    """Normalize a linker subworkflow effect into the operational linker contract.

    The effect adapter returns the canonical public `/mednotes:link` FSM payload.
    Process-chats still needs the private operational diagnosis/apply artifact to
    decide whether the linker can safely move from preview to apply. This
    boundary reads those declared artifacts instead of inferring state from
    human text or legacy root fields.
    """

    canonical = _link_runtime_payload_from_effect_payload(result)
    if canonical is not None:
        return canonical

    return LinkerRunResult.from_payload(
        _link_runtime_contract_invalid_payload(
            phase=result.effect.origin_state,
            next_action=result.next_action,
            error_context_payload=result.error_context,
            failed=result.status == WorkflowEffectStatus.FAILED,
        )
    )


def _link_runtime_contract_invalid_payload(
    *,
    phase: str,
    next_action: str = "",
    reason: str = "link_runtime_artifact_contract_invalid",
    path: Path | None = None,
    error_context_payload: Mapping[str, object] | None = None,
    failed: bool = False,
) -> JsonObject:
    """Return a typed blocked linker payload when the runtime artifact is not canonical.

    The CLI adapter is allowed to report that the effect/artifact contract is
    invalid. It must not infer a successful linker state from legacy receipts or
    from progress text; the linker FSM consumes typed observation facts.
    """

    public_next_action = (
        next_action.strip()
        or "Reexecutar /mednotes:link pela rota oficial; o artefato operacional não satisfez o contrato canônico."
    )
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.link-run.v1",
        "phase": phase or "link_runtime_contract",
        "status": "failed" if failed else "blocked",
        "blocked_reason": reason,
        "next_action": public_next_action,
        "required_inputs": ["link_runtime_artifact"],
        "human_decision_required": False,
        "blocker_count": 1,
        "files_changed": 0,
        "changed_files": [],
    }
    if path is not None:
        payload["artifact_path"] = str(path)
    context_source = dict(error_context_payload or {})
    payload.update(
        {
            "error_context": error_context(
                phase=phase,
                blocked_reason=reason,
                root_cause=reason,
                affected_artifact=str(path) if path is not None else "workflow_effect_result",
                error_summary="O payload operacional do linker não satisfaz o contrato FSM-first.",
                suggested_fix=public_next_action,
                next_action=public_next_action,
                retry_scope="rerun_mednotes_link_official_route",
                missing_inputs=["link_runtime_artifact"],
                human_decision_required=False,
            )
        }
    )
    if context_source:
        payload["runtime_error_context"] = context_source
    return JsonObjectAdapter.validate_python(payload)


def _link_runtime_payload_from_effect_payload(result: WorkflowEffectResult) -> LinkerRunResult | None:
    payload = result.payload
    schema = _json_field(payload, "schema")
    if schema in {
        "medical-notes-workbench.link-diagnosis.v1",
        "medical-notes-workbench.link-run.v1",
        "medical-notes-workbench.link-run-receipt.v1",
    }:
        try:
            return LinkerRunResult.from_payload(payload)
        except PydanticValidationError:
            return LinkerRunResult.from_payload(
                _link_runtime_contract_invalid_payload(
                    phase=result.effect.origin_state,
                    next_action=result.next_action,
                    error_context_payload=result.error_context,
                    failed=result.status == WorkflowEffectStatus.FAILED,
                )
            )
    if schema != "medical-notes-workbench.link-fsm-result.v1":
        return None

    artifacts = _dict_value(_json_field(payload, "artifacts"))
    try:
        intent = LinkWorkflowRunEffectPayload.from_effect_payload(result.effect.payload)
    except PydanticValidationError:
        return None
    preferred_key = "receipt_path" if intent.apply else "diagnosis_path"
    artifact_payload = _link_runtime_payload_from_artifact(artifacts, preferred_key)
    if artifact_payload is not None:
        return artifact_payload
    return LinkerRunResult.from_payload(
        _link_runtime_contract_invalid_payload(
            phase=result.effect.origin_state,
            next_action=result.next_action,
            error_context_payload=result.error_context,
            failed=result.status == WorkflowEffectStatus.FAILED,
        )
    )


def _link_runtime_payload_from_artifact(artifacts: Mapping[str, object], artifact_key: str) -> LinkerRunResult | None:
    path_value = artifacts.get(artifact_key)
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    normalized = _normalized_linker_artifact_payload(raw, path=path)
    try:
        return LinkerRunResult.from_payload(normalized)
    except PydanticValidationError:
        return LinkerRunResult.from_payload(
            _link_runtime_contract_invalid_payload(
                phase="link_runtime_artifact",
                path=path,
            )
        )


def _related_notes_export_effect_from_link_fsm_payload(result: WorkflowEffectResult) -> WorkflowEffect | None:
    """Return the Related Notes recovery effect only when the Link FSM emitted it.

    The adapter must not interpret `blocked_reason`, `next_action`, or human
    report text as permission to recover. This function only rehydrates the
    executable effect that already crossed the FSM -> agent boundary through
    `agent_directive.control.effects`.
    """

    payload = result.payload
    if _json_field(payload, "schema") != "medical-notes-workbench.link-fsm-result.v1":
        return None
    directive_payload = _dict_value(_json_field(payload, "agent_directive"))
    if not directive_payload:
        return None
    try:
        directive = AgentDirective.model_validate(directive_payload)
    except PydanticValidationError:
        return None
    for projected in directive.control.effects:
        if projected.kind != WorkflowEffectKind.RUN_SUBWORKFLOW or projected.target != "related_notes.export":
            continue
        try:
            intent = RelatedNotesExportEffectPayload.from_effect_payload(projected.payload)
        except PydanticValidationError:
            return None
        return WorkflowEffect(
            workflow=directive.workflow,
            run_id=directive.run_id,
            effect_id=projected.effect_id or "link-related-notes-export-recovery",
            origin_state=projected.origin_state,
            kind=projected.kind,
            target=projected.target,
            payload=intent.to_payload(),
            mutates_resources=projected.mutates_resources,
            no_resource_mutation=projected.no_resource_mutation,
            rollback_declared=projected.rollback_declared,
            requires_receipt=projected.requires_receipt,
            requires_attestation=projected.requires_attestation,
            model_policy=projected.model_policy,
            resume_action=projected.resume_action,
            metadata=projected.metadata,
        )
    return None


def _link_fsm_snapshot_category(result: WorkflowEffectResult) -> WorkflowStateCategory | None:
    """Read the public FSM category from a canonical `/mednotes:link` result.

    This adapter may chain another official effect only from FSM state, never
    from private linker status/blocker fields that merely describe runtime
    evidence.
    """

    payload = result.payload
    if _json_field(payload, "schema") != "medical-notes-workbench.link-fsm-result.v1":
        return None
    try:
        snapshot = WorkflowStateMachineSnapshot.model_validate(_json_field(payload, "state_machine_snapshot"))
    except PydanticValidationError:
        return None
    return snapshot.current_category


def _linker_phase_blocked_reason(phase: Mapping[str, object]) -> str:
    fields = _WorkflowStatusCliFields.model_validate(dict(phase))
    status = fields.status.strip()
    blocked_reason = fields.blocked_reason.strip()
    if blocked_reason:
        return blocked_reason
    skipped_reason = fields.skipped_reason.strip()
    return skipped_reason if status in {"blocked", "failed"} else ""


def _specific_linker_blocked_reason(linker_payload: Mapping[str, object], *, fallback: str) -> str:
    """Expose the linker's actionable phase blocker without making CLI state."""

    for key in ("body_term_linker", "related_notes_sync"):
        phase = _dict_value(_json_field(linker_payload, key))
        reason = _linker_phase_blocked_reason(phase)
        if reason:
            return reason
    plan = _dict_value(_json_field(linker_payload, "plan"))
    phases = _dict_value(_json_field(plan, "phases"))
    for phase_key in (
        "body_term_linker",
        "contextual_alias_disambiguation",
        "related_notes_sync",
        "graph_validation",
    ):
        phase = _dict_value(_json_field(phases, phase_key))
        reason = _linker_phase_blocked_reason(phase)
        if reason:
            return reason
    vocabulary_bootstrap = _dict_value(_json_field(linker_payload, "vocabulary_bootstrap"))
    if _json_field(vocabulary_bootstrap, "status") == "planned":
        return "vocabulary_bootstrap_required"
    return str(_json_field(linker_payload, "blocked_reason") or fallback).strip() or fallback


def _normalized_linker_artifact_payload(raw: Mapping[str, object], *, path: Path | None = None) -> dict[str, object]:
    """Validate linker artifacts without synthesizing legacy success fields."""

    payload = {str(key): value for key, value in raw.items()}
    fields = _WorkflowStatusCliFields.model_validate(payload)
    if fields.schema_id != "medical-notes-workbench.link-run-receipt.v1":
        return payload
    return payload


def _wiki_relative_path(path: Path, config: MedConfig) -> str:
    try:
        return path.resolve().relative_to(config.wiki_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _title_from_note_path(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or path.stem
    except OSError:
        pass
    return path.stem


def _hash_if_present(path: Path) -> str:
    return "sha256:" + file_sha256(path) if path.is_file() else ""


def _link_runtime_result_has_blockers(result: LinkerRunResult | None) -> bool:
    if result is None:
        return True
    return bool(
        result.blocker_count
        or result.blocked_reason.strip()
        or result.error.strip()
        or result.parse_error.strip()
        or result.returncode not in {0, 3}
    )


def _link_runtime_result_applied(result: LinkerRunResult | None) -> bool:
    if result is None:
        return False
    return result.status in {"completed", "completed_with_link_blockers"} and result.files_changed >= 0


def _extension_root() -> Path:
    from mednotes.platform.paths import extension_root

    return extension_root()


def _delegate_vault_git_run_finish(args: argparse.Namespace) -> int:
    vault_git = _extension_root() / "scripts" / "vault" / "vault_git.py"
    command = [sys.executable, str(vault_git), "run-finish"]
    for option, value in (
        ("--agent", getattr(args, "agent", None)),
        ("--workflow", getattr(args, "workflow", None)),
        ("--title", getattr(args, "title", None)),
        ("--body-file", getattr(args, "body_file", None)),
        ("--tool", getattr(args, "tool", None)),
        ("--subagent", getattr(args, "subagent", None)),
        ("--run-id", getattr(args, "run_id", None)),
        ("--trigger-context", getattr(args, "trigger_context", None)),
        ("--receipt", getattr(args, "receipt", None)),
        ("--notes-touched", getattr(args, "notes_touched", None)),
        ("--branch", getattr(args, "branch", None)),
        ("--vault-dir", getattr(args, "vault_dir", None)),
    ):
        if value is not None:
            command.extend([option, str(value)])
    if bool(getattr(args, "public_json", False)):
        command.append("--public-json")
    if bool(getattr(args, "json", False)):
        command.append("--json")
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return int(completed.returncode)


def _auto_run_linker_from_trigger_context(
    config: MedConfig,
    trigger_context: dict[str, object],
    *,
    label: str,
    include_related_notes: bool = True,
) -> ProcessChatsLinkerRun:
    run_dir = _auto_link_run_dir(label)
    run_id = run_dir.name
    trigger_fields = _LinkTriggerContextCliFields.model_validate(trigger_context)
    source_workflow = trigger_fields.source_workflow or "/mednotes:link"
    trigger_context_path = run_dir / "link-trigger-context.json"
    diagnosis_path = run_dir / "link-diagnosis.json"
    receipt_path = run_dir / "link-run-receipt.json"
    write_trigger_context(trigger_context_path, trigger_context)
    executor = _workflow_effect_executor(config)
    diagnosis_effect_result = executor.execute(
        _link_subworkflow_effect(
            source_workflow=source_workflow,
            run_id=run_id,
            effect_id=f"{label}-link-diagnosis",
            diagnose=True,
            apply=False,
            diagnosis_path=diagnosis_path,
            receipt_path=None,
            trigger_context_path=trigger_context_path,
            include_related_notes=include_related_notes,
            label=label,
        )
    )
    diagnosis_result = _link_runtime_result_from_effect_result(diagnosis_effect_result)
    diagnosis_payload = diagnosis_result.operation_payload
    effect_results = [diagnosis_effect_result.to_payload()]
    recovery_effect = _related_notes_export_effect_from_link_fsm_payload(diagnosis_effect_result)
    if recovery_effect is not None:
        recovery_effect_result = executor.execute(recovery_effect)
        effect_results.append(recovery_effect_result.to_payload())
        diagnosis_effect_result = executor.execute(
            _link_subworkflow_effect(
                source_workflow=source_workflow,
                run_id=run_id,
                effect_id=f"{label}-link-diagnosis-after-related-notes-recovery",
                diagnose=True,
                apply=False,
                diagnosis_path=diagnosis_path,
                receipt_path=None,
                trigger_context_path=trigger_context_path,
                include_related_notes=include_related_notes,
                label=label,
            )
        )
        effect_results.append(diagnosis_effect_result.to_payload())
        diagnosis_result = _link_runtime_result_from_effect_result(diagnosis_effect_result)
        diagnosis_payload = diagnosis_result.operation_payload
    apply_effect_result: WorkflowEffectResult | None = None
    apply_result: LinkerRunResult | None = None
    # The mutating half remains an official `/mednotes:link` effect, but the
    # parent may only chain it after the LinkMachine reports a completed
    # diagnosis. Any other FSM category must wait for its own emitted effects or
    # human/external continuation route.
    if diagnosis_path.is_file() and _link_fsm_snapshot_category(diagnosis_effect_result) == WorkflowStateCategory.COMPLETED:
        version_control_safety = (
            VersionControlSafety.model_validate(
                _active_guard_version_control_safety_payload(
                    changed_file_count=len(_list_value(_json_field(trigger_context, "changed_notes"))),
                )
            )
            if active_guard_exists(config.wiki_dir)
            else None
        )
        apply_effect_result = executor.execute(
            _link_subworkflow_effect(
                source_workflow=source_workflow,
                run_id=run_id,
                effect_id=f"{label}-link-apply",
                diagnose=False,
                apply=True,
                diagnosis_path=diagnosis_path,
                receipt_path=receipt_path,
                trigger_context_path=None,
                include_related_notes=include_related_notes,
                label=label,
                version_control_safety=version_control_safety,
            )
        )
        apply_result = _link_runtime_result_from_effect_result(apply_effect_result)
    linker_applied = _link_runtime_result_applied(apply_result)
    completed_without_blockers = bool(linker_applied and not _link_runtime_result_has_blockers(apply_result))
    # The diagnosis report carries an apply-oriented next action by design. Once
    # the adapter actually applies that diagnosis successfully, the process-chats
    # FSM must receive a clean completed linker payload instead of stale CLI
    # guidance that would leak into the public/agent-facing report.
    effective_result = apply_result or diagnosis_result
    next_action = "" if completed_without_blockers else effective_result.next_action
    skipped_reason = ""
    if apply_result is None:
        skipped_reason = _specific_linker_blocked_reason(diagnosis_payload, fallback="not_safe_to_apply")
    elif _link_runtime_result_has_blockers(apply_result):
        skipped_reason = _specific_linker_blocked_reason(
            apply_result.operation_payload,
            fallback=apply_result.blocked_reason or "linker_blocked",
        )
    blocker_count = max(
        diagnosis_result.blocker_count,
        apply_result.blocker_count if apply_result is not None else 0,
    )
    if apply_effect_result is not None:
        effect_results.append(apply_effect_result.to_payload())
    return ProcessChatsLinkerRun.model_validate(
        {
            "schema": "medical-notes-workbench.workflow-linker-run.v1",
            "phase": "workflow_linker",
            "status": effective_result.status or "blocked",
            "next_action": next_action,
            "trigger_context_path": str(trigger_context_path),
            "diagnosis_path": str(diagnosis_path),
            "receipt_path": str(receipt_path) if apply_result else "",
            "diagnosis_status": diagnosis_result.status,
            "diagnosis_blocked_reason": _specific_linker_blocked_reason(diagnosis_payload, fallback=""),
            "blocker_count": blocker_count,
            "linker_applied": linker_applied,
            "linker_skipped_reason": skipped_reason,
            "apply_status": apply_result.status if apply_result is not None else "",
            "apply_blocked_reason": apply_result.blocked_reason if apply_result is not None else "",
            "changed_files": apply_result.changed_files if apply_result is not None else [],
            "files_changed": apply_result.files_changed if apply_result is not None else 0,
            "workflow_effect_results": effect_results,
        }
    )


def _publish_trigger_context(result: dict[str, object], config: MedConfig, manifest: Path) -> dict[str, object] | None:
    created = _json_field(result, "created")
    if not isinstance(created, list) or not created:
        return None
    changed_notes: list[dict[str, object]] = []
    for value in created:
        path = _path(str(value))
        changed_notes.append(
            {
                "change_type": "created",
                "content_change": "text",
                "path": _wiki_relative_path(path, config),
                "title": _title_from_note_path(path),
                "after_hash": _hash_if_present(path),
            }
        )
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": "/mednotes:process-chats",
        "batch_id": str(manifest),
        "changed_notes": changed_notes,
    }


def _record_linker_run_evidence(result: dict[str, object], linker_run: ProcessChatsLinkerRun | JsonObject) -> None:
    """Attach child linker evidence without deciding the parent workflow state.

    The parent FSM consumes `runtime_observation` as its canonical event packet.
    Linker run details remain diagnostics; typed booleans from
    `ProcessChatsLinkerRun` update the observation without parsing public
    reports or legacy status strings.
    """

    linker_run = ProcessChatsLinkerRun.model_validate(linker_run)
    linker_payload = linker_run.to_payload()
    update: dict[str, object] = {
        "linker": linker_payload,
        "link_trigger_context_path": linker_run.trigger_context_path,
        "linker_trigger_context_path": linker_run.trigger_context_path,
        "linker_diagnosis_path": linker_run.diagnosis_path,
        "linker_receipt_path": linker_run.receipt_path,
    }
    if "runtime_observation" in result:
        previous_observation = ProcessChatsPublishRuntimeObservation.model_validate(result["runtime_observation"])
        link_blocked = _process_chats_linker_run_has_blockers(linker_run)
        link_completed = bool(linker_run.linker_applied and not link_blocked)
        reason_code = "" if link_completed else "process_chats_linker_blocked"
        next_action = (
            ""
            if link_completed
            else _process_chats_link_next_action(linker_run.next_action)
        )
        affected_artifact = (
            linker_run.receipt_path
            or linker_run.diagnosis_path
            or linker_run.trigger_context_path
            or previous_observation.manifest_path
            or "linker"
        )
        error_context = (
            None
            if link_completed
            else ProcessChatsErrorContext(
                root_cause=reason_code,
                affected_artifact=affected_artifact,
                retry_scope="process-chats-link",
                next_action=next_action,
            )
        )
        update["runtime_observation"] = ProcessChatsPublishRuntimeObservation(
            source_state=ProcessChatsState.LINK_RUN_REQUESTED,
            link_completed=link_completed,
            link_blocked=not link_completed,
            reason_code=reason_code,
            next_action=next_action,
            manifest_path=previous_observation.manifest_path,
            receipt_id=previous_observation.receipt_id,
            published_count=previous_observation.published_count,
            link_trigger_context_path=linker_run.trigger_context_path,
            link_receipt_id=linker_run.receipt_path or previous_observation.link_receipt_id,
            link_changed_files=linker_run.changed_files or previous_observation.link_changed_files,
            error_context=error_context,
        ).to_payload()
    result.update(update)


def _process_chats_linker_run_has_blockers(linker_run: ProcessChatsLinkerRun) -> bool:
    """Return linker blockage from typed counters/reasons, not public status."""

    return bool(
        linker_run.blocker_count
        or linker_run.linker_skipped_reason.strip()
        or linker_run.diagnosis_blocked_reason.strip()
        or linker_run.apply_blocked_reason.strip()
    )


def _process_chats_link_next_action(value: str) -> str:
    """Render process-chats linker recovery in public workflow language."""

    text = value.strip()
    if not text:
        return "Resolver pendências de conexões/grafo pela rota oficial."
    legacy_prefix = "Resolver pendências do linker/grafo:"
    if text.startswith(legacy_prefix):
        detail = text[len(legacy_prefix):].strip()
        return f"Resolver pendências de conexões/grafo: {detail}" if detail else "Resolver pendências de conexões/grafo pela rota oficial."
    if text.startswith("Resolver pendências de conexões/grafo"):
        return text
    return f"Resolver pendências de conexões/grafo: {text}"


def _refresh_receipt_if_present(result: dict[str, object]) -> None:
    receipt_path = _json_field(result, "receipt_path")
    if isinstance(receipt_path, str) and receipt_path:
        _write_json_atomic(_path(receipt_path), result)


def _inferred_wiki_root_from_note(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if parent.name == "Wiki_Medicina":
            return parent
    return path.parent


def _resolve_config_for_target(args: argparse.Namespace, target: Path) -> MedConfig:
    try:
        return resolve_config(args)
    except WikiPathResolutionError:
        inferred_args = argparse.Namespace(**vars(args))
        inferred_args.wiki_dir = str(_inferred_wiki_root_from_note(target))
        return resolve_config(inferred_args)


def _single_modified_note_context(
    *,
    source_workflow: str,
    path: Path,
    config: MedConfig,
    batch_id: str,
) -> dict[str, object]:
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": source_workflow,
        "batch_id": batch_id,
        "changed_notes": [
            {
                "change_type": "modified",
                "content_change": "text",
                "path": _wiki_relative_path(path, config),
                "title": _title_from_note_path(path),
                "after_hash": _hash_if_present(path),
            }
        ],
    }


def _style_rewrite_arg_path(args: argparse.Namespace, name: str) -> Path | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return _path(text) if text else None


def _style_rewrite_apply_payload(args: argparse.Namespace) -> dict[str, object]:
    plan = _style_rewrite_arg_path(args, "plan")
    outputs = _style_rewrite_arg_path(args, "outputs")
    target = _style_rewrite_arg_path(args, "target")
    content = _style_rewrite_arg_path(args, "content")
    raw_work_id = getattr(args, "work_id", "")
    work_id = raw_work_id.strip() if isinstance(raw_work_id, str) else str(raw_work_id).strip()
    if not bool(getattr(args, "dry_run", False)) and plan and outputs and work_id:
        return apply_style_rewrite_from_manifest(
            plan_path=plan,
            outputs_path=outputs,
            work_id=work_id,
            dry_run=False,
            backup=False,
        )
    if not bool(getattr(args, "dry_run", False)):
        return style_rewrite_manifest_required_receipt(target_path=target, content_path=content)
    if target is None or content is None:
        return style_rewrite_manifest_required_receipt(target_path=target, content_path=content)
    return apply_style_rewrite(
        target,
        content,
        dry_run=True,
        backup=False,
    )


def _style_rewrite_atomic_apply_payload(args: argparse.Namespace) -> dict[str, object]:
    return finalize_collect_apply_style_rewrite(
        plan_path=_path(args.plan),
        manifest_path=_path(args.manifest),
        work_id=str(args.work_id),
        specialist_run_receipt_path=_style_rewrite_arg_path(args, "specialist_run_receipt"),
        backup=False,
    )


def _style_rewrite_written_target(result: dict[str, object], args: argparse.Namespace) -> Path | None:
    if _json_field(result, "written"):
        return _style_rewrite_arg_path(args, "target")
    nested_apply = _json_field(result, "apply")
    if isinstance(nested_apply, dict):
        nested_target = _style_rewrite_written_target(nested_apply, args)
        if nested_target is not None:
            return nested_target
    items = _json_field(result, "items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_fields = _StyleRewriteSummaryItemFields.model_validate(item)
            if item_fields.written and item_fields.target_path:
                return _path(item_fields.target_path)
    return None


def _compact_workflow_linker_run(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, object] = {}
    for key in (
        "schema",
        "phase",
        "status",
        "trigger_context_path",
        "diagnosis_path",
        "receipt_path",
        "diagnosis_status",
        "diagnosis_blocked_reason",
        "blocker_count",
        "linker_applied",
        "linker_skipped_reason",
        "apply_status",
        "apply_blocked_reason",
        "files_changed",
        "changed_files",
    ):
        if key in value:
            summary[key] = value[key]
    fields = _WorkflowEffectResultsCliFields.model_validate(value)
    if fields.workflow_effect_results:
        summary["workflow_effect_result_count"] = len(fields.workflow_effect_results)
    return summary


def _compact_style_rewrite_apply_payload(result: dict[str, object]) -> dict[str, object]:
    fields = _StyleRewriteDirectApplyFields.model_validate(result)
    items = _json_field(result, "items")
    item_list = items if isinstance(items, list) else []
    if not item_list and fields.target_path and fields.content_path:
        validation = fields.validation
        error_list = validation.errors
        warning_list = validation.warnings
        requires_llm_rewrite = validation.requires_llm_rewrite
        blocked_reason = ""
        status = "validated" if fields.dry_run else "idempotent"
        next_action = ""
        if error_list:
            status = "blocked"
            blocked_reason = "validation_errors"
            next_action = "Regenerar o rewrite pela rota de autoria médica especializada."
        elif requires_llm_rewrite:
            status = "blocked"
            blocked_reason = "style_rewrite_still_requires_rewrite"
            next_action = (
                "Regenerar o rewrite pela rota de autoria médica especializada até "
                "validation.requires_llm_rewrite=false."
            )
        elif fields.written is True:
            status = "applied"
        item_list = [
            {
                "target_path": fields.target_path,
                "content_path": fields.content_path,
                "output_path": fields.content_path,
                "status": status,
                "blocked_reason": blocked_reason,
                "next_action": next_action,
                "changed": fields.changed,
                "written": fields.written,
                "backup_path": fields.backup_path,
                "deterministic_fixes_applied": fields.deterministic_fixes_applied,
                "validation": {
                    "ok": not error_list and not requires_llm_rewrite,
                    "error_count": len(error_list),
                    "warning_count": len(warning_list),
                    "requires_llm_rewrite": requires_llm_rewrite,
                    "error_codes": [item.code for item in error_list if item.code.strip()],
                    "warning_codes": [item.code for item in warning_list if item.code.strip()],
                },
            }
        ]
    compact_items: list[dict[str, object]] = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        compact_items.append(
            {
                key: item[key]
                for key in (
                    "work_id",
                    "target_path",
                    "output_path",
                    "status",
                    "blocked_reason",
                    "next_action",
                    "changed",
                    "written",
                    "backup_path",
                    "content_path",
                    "deterministic_fixes_applied",
                    "validation",
                )
                if key in item
            }
        )
    linker = _compact_workflow_linker_run(_json_field(result, "linker"))
    compact: dict[str, object] = {
        "schema": "medical-notes-workbench.style-rewrite-apply-agent-stdout.v1",
        "source_schema": _json_field(result, "schema"),
        "phase": _json_field(result, "phase"),
        "status": _json_field(result, "status"),
        "blocked_reason": _json_field(result, "blocked_reason", ""),
        "next_action": _json_field(result, "next_action", ""),
        "required_inputs": _json_field(result, "required_inputs", []),
        "human_decision_required": _json_field(result, "human_decision_required", False),
        "plan_path": _json_field(result, "plan_path"),
        "output_manifest_path": _json_field(result, "output_manifest_path"),
        "item_count": len(compact_items),
        "written_count": sum(1 for item in compact_items if _StyleRewriteSummaryItemFields.model_validate(item).written),
        "changed_count": sum(1 for item in compact_items if _StyleRewriteSummaryItemFields.model_validate(item).changed),
        "items": compact_items,
        "link_trigger_context_path": _json_field(result, "link_trigger_context_path"),
        "linker_trigger_context_path": _json_field(result, "linker_trigger_context_path"),
        "linker_diagnosis_path": _json_field(result, "linker_diagnosis_path"),
        "linker_receipt_path": _json_field(result, "linker_receipt_path"),
        "linker_applied": _json_field(result, "linker_applied"),
        "linker_skipped_reason": _json_field(result, "linker_skipped_reason"),
        "linker": linker,
        "error_context": _json_field(result, "error_context"),
        "diagnostic_context": _json_field(result, "diagnostic_context"),
        "decision_summary": _json_field(result, "decision_summary"),
        "agent_events": _json_field(result, "agent_events"),
    }
    return {key: value for key, value in compact.items() if value not in (None, {})}


def _style_rewrite_apply_summary_item(item: Mapping[str, object]) -> dict[str, object]:
    fields = _StyleRewriteSummaryItemFields.model_validate(dict(item))
    payload = {
        key: item[key]
        for key in (
            "work_id",
            "target_path",
            "output_path",
            "status",
            "blocked_reason",
            "next_action",
            "changed",
            "written",
        )
        if key in item
    }
    if fields.work_id and "work_id" not in payload:
        payload["work_id"] = fields.work_id
    return payload


def _agent_stdout_bounded_text(value: object, *, max_chars: int = 1200) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _specialist_continuation_work_item_payload(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    fields = _SpecialistContinuationSourceFields.model_validate(item)
    receipt_path = fields.specialist_task_run_receipt_path or _derived_specialist_task_run_receipt_path(fields)
    payload = {
        "work_id": fields.work_id,
        "phase": fields.phase,
        "agent": fields.agent,
        "item_type": fields.item_type or "",
        "target_path": fields.target_path,
        "target_hash_before": fields.target_hash_before,
        "title": fields.title or "",
        "model_policy": fields.model_policy or "medical_specialist_authoring.v1",
        "required_model_tier": fields.required_model_tier,
        "preferred_model_tier": fields.preferred_model_tier or "",
        "temp_output": fields.temp_output,
        "specialist_task_run_receipt_path": receipt_path,
        "subagent_output_contract": fields.subagent_output_contract,
    }
    rewrite_prompt = _agent_stdout_bounded_text(fields.rewrite_prompt)
    if rewrite_prompt is not None:
        payload["rewrite_prompt"] = rewrite_prompt
    try:
        return SpecialistContinuationWorkItem.model_validate(payload).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude_defaults=True,
        )
    except PydanticValidationError:
        return None


def _derived_specialist_task_run_receipt_path(item: _SpecialistContinuationSourceFields) -> str:
    if item.specialist_task_run_receipt_path:
        return item.specialist_task_run_receipt_path
    if not item.temp_output:
        return ""
    return str(Path(item.temp_output).with_suffix(".specialist-task-run-receipt.json"))


def _style_rewrite_linker_checkpoint_summary(*, linker: Mapping[str, object], apply_payload: Mapping[str, object]) -> str:
    if not linker:
        return ""
    linker_fields = _StyleRewriteLinkerCheckpointFields.model_validate(dict(linker))
    apply_fields = _StyleRewriteDirectApplyFields.model_validate(dict(apply_payload))
    blocker = (
        apply_fields.linker_skipped_reason
        or apply_fields.blocked_reason
        or linker_fields.diagnosis_blocked_reason
        or linker_fields.apply_blocked_reason
        or linker_fields.linker_skipped_reason
    ).strip()
    status = linker_fields.status.strip()
    if blocker or status == "blocked":
        reason = blocker or "linker_blocked"
        return (
            f"grafo/linker pendente neste apply ({reason}); continue pela rota indicada antes de declarar "
            "a Wiki concluida."
        )
    return "grafo/linker sem blockers neste apply"


def _style_rewrite_atomic_apply_agent_directive(
    *,
    work_id: str,
    plan_path: str,
    next_specialist_task: NextSpecialistTask | None,
    next_plan_step: Mapping[str, object],
    linker_summary: str,
) -> dict[str, object]:
    if next_specialist_task is not None:
        return _agent_directive_payload(
            workflow="/mednotes:fix-wiki",
            run_id=f"style-rewrite-apply-{work_id}",
            status="waiting_agent",
            state="style_rewrite_batch_continue",
            phase="style_rewrite",
            reason="style_rewrite_next_specialist_ready",
            continue_now=True,
            final_report=False,
            effects=[
                _style_rewrite_specialist_agent_effect(
                    next_specialist_task,
                    plan_path=plan_path,
                )
            ],
            resume=(
                "Continue pelo agent_directive.control.effects[].payload.current_batch_items[0] no harness atual; "
                "no OpenCode, use task com JSON raiz contendo somente current_batch_items."
            ),
            report_requires=["specialist_checkpoint", "linker_state", "agent_directive"],
            summary="A reescrita atual foi aplicada; ainda ha item especialista no lote atual.",
            instructions=[
                "Nao rode validate-agent-run-report, fix-wiki --apply ou plan-subagents enquanto ha efeito call_specialist_model executavel.",
                "No OpenCode, passe exatamente agent_directive.control.effects[].payload.current_batch_items como prompt JSON da task.",
                "Depois da task, finalize pelo comando oficial e aplique o recibo antes de qualquer outro especialista.",
                f"Estado do linker neste apply: {linker_summary or 'nao reportado'}.",
            ],
        )
    return _agent_directive_payload(
        workflow="/mednotes:fix-wiki",
        run_id=f"style-rewrite-apply-{work_id}",
        status="waiting_agent",
        state="style_rewrite_batch_checkpoint",
        phase="style_rewrite",
        reason="style_rewrite_batch_checkpoint_required",
        continue_now=True,
        final_report=False,
        effects=[_style_rewrite_plan_subagents_effect(next_plan_step)],
        resume=(
            "Reporte um checkpoint humano curto deste lote e depois execute o efeito run_subworkflow em agent_directive.control.effects; "
            "nao rode validate-agent-run-report nem fix-wiki --apply antes desse checkpoint."
        ),
        report_requires=["batch_checkpoint", "content_quality", "linker_state", "remaining_rewrites"],
        summary="O lote atual foi aplicado; reporte checkpoint e siga para o proximo planejamento.",
        instructions=[
            "O checkpoint nao e relatorio final da Wiki.",
            "Depois do checkpoint, use agent_directive.control.effects[].payload.arguments para renovar o lote.",
            "Nao valide relatorio final contra o compact-report antigo enquanto a fila de estilo nao estiver vazia.",
            f"Estado do linker neste apply: {linker_summary or 'nao reportado'}.",
        ],
    )


def _style_rewrite_plan_output_agent_directive(
    *,
    plan_path: Path,
    next_specialist_task: NextSpecialistTask,
) -> dict[str, object]:
    work_id = next_specialist_task.work_id
    return _agent_directive_payload(
        workflow="/mednotes:fix-wiki",
        run_id=f"style-rewrite-plan-{work_id or plan_path.stem}",
        status="waiting_agent",
        state="style_rewrite_batch_continue",
        phase="style_rewrite",
        reason="style_rewrite_next_specialist_ready",
        continue_now=True,
        final_report=False,
        effects=[_style_rewrite_specialist_agent_effect(next_specialist_task, plan_path=plan_path)],
        resume=(
            "Continue pelo agent_directive.control.effects[].payload.current_batch_items[0] deste recibo; "
            "no OpenCode, use task com JSON raiz contendo somente current_batch_items."
        ),
        report_requires=["specialist_checkpoint", "agent_directive"],
        summary="Novo lote de reescrita planejado; ha item especialista executavel agora.",
        instructions=[
            "Nao rode validate-agent-run-report, fix-wiki --apply ou plan-subagents enquanto ha efeito call_specialist_model executavel.",
            "No OpenCode, passe exatamente agent_directive.control.effects[].payload.current_batch_items como prompt JSON da task.",
            (
                f"Depois da task, rode {wiki_cli_relative_command('finalize-opencode-specialist-task')} "
                "com o mesmo --plan e --work-id deste recibo, depois aplique o recibo."
            ),
            "Nao reconstruir current_batch_items, target_path, target_hash_before, rewrite_prompt, temp_output ou specialist_task_run_receipt_path manualmente.",
        ],
    )


def _style_rewrite_specialist_agent_effect(
    next_specialist_task: NextSpecialistTask,
    *,
    plan_path: str | Path = "",
) -> dict[str, object]:
    """Return the executable specialist effect carried by AgentDirective.

    Plan receipts are a public automation boundary, not just a human summary.
    The hook must be able to continue from this effect without reopening the
    private plan file or reconstructing `current_batch_items` from text.
    """

    normalized_plan_path = str(plan_path) if plan_path else ""
    work_id = next_specialist_task.work_id
    current_batch_items = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in next_specialist_task.current_batch_items
    ]
    payload: dict[str, object] = {
        "kind": "style_rewrite",
        "work_id": work_id,
        "agent": next_specialist_task.agent,
        "title": next_specialist_task.title,
        "execution_mode": next_specialist_task.execution_mode,
        "authoring_mode": next_specialist_task.authoring_mode,
        "authoring_max_concurrency": next_specialist_task.authoring_max_concurrency,
        "apply_mode": next_specialist_task.apply_mode,
        "serial_apply_required": next_specialist_task.serial_apply_required,
        "wait_for_all_authoring_outputs_before_apply": (
            next_specialist_task.wait_for_all_authoring_outputs_before_apply
        ),
        "current_batch_items": current_batch_items,
    }
    if normalized_plan_path:
        payload["plan_path"] = normalized_plan_path
        payload["receipt_finalizers"] = [
            {
                "harness": "opencode",
                "command_family": "finalize-opencode-specialist-task",
                "arguments": ["--plan", normalized_plan_path, "--work-id", work_id, "--json"],
            },
            {
                "harness": "antigravity",
                "command_family": "finalize-agy-specialist-task",
                "arguments": ["--plan", normalized_plan_path, "--work-id", work_id, "--json"],
                "requires_transcript_or_runtime_log": True,
            },
        ]
        apply_command: dict[str, object] = {
            "command_family": "apply-specialist-style-rewrite",
            "arguments": [
                "--plan",
                normalized_plan_path,
                "--work-id",
                work_id,
                "--json",
            ],
        }
        payload["apply_command"] = apply_command
        payload["harness_routes"] = {
            "antigravity_cli": {
                "route_kind": "packaged_template_subagent",
                "agent_name": next_specialist_task.agent,
                "define_subagent_source": "packaged_agent_template_only",
                "template_path_candidates": [
                    str(
                        Path.home()
                        / ".gemini"
                        / "config"
                        / "plugins"
                        / "medical-notes-workbench"
                        / "agents"
                        / f"{next_specialist_task.agent}.md"
                    ),
                    str(
                        Path.home()
                        / ".gemini"
                        / "antigravity-cli"
                        / "plugins"
                        / "medical-notes-workbench"
                        / "agents"
                        / f"{next_specialist_task.agent}.md"
                    ),
                ],
            }
        }
    return {
        "kind": "call_specialist_model",
        "target": next_specialist_task.agent,
        "payload": payload,
    }


def _style_rewrite_plan_subagents_effect(next_plan_step: Mapping[str, object]) -> dict[str, object]:
    """Project the next planning step as an AgentDirective effect.

    The compact stdout intentionally has no root continuation-step field. Hooks and
    agents consume this effect so the FSM directive remains the single
    executable contract.
    """

    step = _StyleRewritePlanStepCliFields.model_validate(next_plan_step)
    return {
        "kind": "run_subworkflow",
        "target": "plan-subagents",
        "payload": {
            "kind": "style_rewrite_plan_next_batch",
            "command_family": step.command_family,
            "arguments": step.arguments,
            "agent_instruction": step.agent_instruction,
        },
    }


def _compact_style_rewrite_atomic_apply_payload(result: dict[str, object]) -> dict[str, object]:
    atomic = StyleRewriteAtomicApplyResult.model_validate(result)
    apply_model = atomic.apply
    apply_payload = apply_model.to_payload() if apply_model is not None else {}
    item_list = apply_model.items if apply_model is not None else []
    compact_items = [
        _style_rewrite_apply_summary_item(item.to_payload())
        for item in item_list
    ]
    linker_evidence = (
        apply_model.linker.to_payload()
        if apply_model is not None and apply_model.linker is not None
        else {}
    )
    linker = _compact_workflow_linker_run(linker_evidence)
    plan_path = atomic.plan_path
    work_id = atomic.work_id
    next_specialist_task = _next_specialist_task_after_work_id(
        plan_path=plan_path,
        current_work_id=work_id,
    )
    batch_report_required = next_specialist_task is None
    next_plan_step = {
        "command_family": "plan-subagents",
        "arguments": [
            "--phase",
            "style-rewrite",
            "--max-concurrency",
            "1",
            "--limit",
            "3",
            "--output",
            plan_path,
            "--json",
        ]
        if plan_path
        else [],
        "agent_instruction": (
            "Use este plan-subagents com --output para renovar o plano da proxima leva; "
            "nao use o JSON impresso como plan_path e nao rode fix-wiki --apply antes de esvaziar a fila."
        ),
    }
    linker_summary = _style_rewrite_linker_checkpoint_summary(linker=linker, apply_payload=apply_payload)
    payload = {
        "schema": "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1",
        "source_schema": atomic.schema_,
        "phase": atomic.phase,
        "status": atomic.status,
        "blocked_reason": atomic.blocked_reason,
        "next_action": atomic.next_action,
        "required_inputs": atomic.required_inputs,
        "human_decision_required": atomic.human_decision_required,
        "plan_path": plan_path,
        "manifest_path": atomic.manifest_path,
        "work_id": work_id,
        "specialist_run_receipt_path": atomic.specialist_run_receipt_path,
        "item_count": len(compact_items),
        "written_count": sum(1 for item in compact_items if _StyleRewriteSummaryItemFields.model_validate(item).written),
        "changed_count": sum(1 for item in compact_items if _StyleRewriteSummaryItemFields.model_validate(item).changed),
        "items": compact_items,
        "linker_trigger_context_path": apply_model.linker_trigger_context_path if apply_model is not None else "",
        "linker_diagnosis_path": apply_model.linker_diagnosis_path if apply_model is not None else "",
        "linker_receipt_path": apply_model.linker_receipt_path if apply_model is not None else "",
        "linker_applied": apply_model.linker_applied if apply_model is not None else False,
        "linker_skipped_reason": apply_model.linker_skipped_reason if apply_model is not None else "",
        "linker": linker,
        "human_progress_checkpoint": {
            "schema": "medical-notes-workbench.style-rewrite-human-progress-checkpoint.v1",
            "summary": (
                f"Lote aplicado: {len(compact_items)} reescrita(s). "
                "Qualidade clínica: precisa aparecer no relatório do agente; "
                "YAML, proveniência e links foram preservados pelo contrato técnico quando a validação não trouxe blockers. "
                "Restam itens de reescrita até o próximo plano indicar fila vazia."
            ),
            "content_quality": "requires_agent_audit",
            "preserved": ["YAML", "proveniência", "links"],
            "linker_summary": linker_summary,
            "remaining_summary": "restam reescritas até o próximo plano retornar fila vazia",
        },
        "batch_progress_report": {
            "schema": "medical-notes-workbench.style-rewrite-batch-progress-agent-handoff.v1",
            "required": batch_report_required,
            "agent_instruction": (
                (
                    "Ainda ha item no lote atual. Siga agent_directive.control.effects sem replanejar "
                    "e reporte o resumo humano somente quando o lote acabar ou quando houver bloqueio."
                )
                if next_specialist_task is not None
                else (
                    "Antes de chamar plan-subagents, fix-wiki --apply ou outra invocação especialista, "
                    "reporte ao usuario um resumo curto deste lote: notas aplicadas, qualidade do conteudo, "
                    "YAML/proveniencia/links preservados, estado do grafo/linker e quantas reescritas restam. "
                    "Depois desse checkpoint, use agent_directive.control.effects; nao rerode fix-wiki --apply no meio da fila."
                )
            ),
        },
        "agent_directive": _style_rewrite_atomic_apply_agent_directive(
            work_id=work_id,
            plan_path=plan_path,
            next_specialist_task=next_specialist_task,
            next_plan_step=next_plan_step,
            linker_summary=linker_summary,
        ),
        "error_context": atomic.error_context.to_payload() if atomic.error_context is not None else None,
    }
    compact = StyleRewriteAtomicApplyAgentStdout.model_validate(payload)
    return compact.model_dump(mode="json", by_alias=True, exclude_none=True)


def _style_rewrite_stdout_payload(result: dict[str, object]) -> dict[str, object]:
    if not _agent_stdout_compact_enabled():
        return result
    if _json_field(result, "schema") == "medical-notes-workbench.style-rewrite-atomic-apply-result.v1":
        return _compact_style_rewrite_atomic_apply_payload(result)
    return _compact_style_rewrite_apply_payload(result)


def _plan_output_receipt_work_item_summary(item: Mapping[str, object]) -> dict[str, object]:
    return {
        key: item[key]
        for key in (
            "work_id",
            "phase",
            "agent",
            "title",
            "item_type",
        )
        if key in item and item[key] not in (None, "")
    }


def _next_specialist_task_from_plan_output(
    *,
    phase: str,
    plan_path: Path,
    work_items: Sequence[object],
) -> NextSpecialistTask | None:
    if phase != "style-rewrite":
        return None
    first_item = next((item for item in work_items if isinstance(item, dict)), None)
    if first_item is None:
        return None
    fields = _SpecialistContinuationSourceFields.model_validate(first_item)
    work_id = fields.work_id
    if not work_id:
        return None
    continuation_item = _specialist_continuation_work_item_payload(first_item)
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.next-specialist-task.v1",
        "kind": "call_specialist_model",
        "work_id": work_id,
        "agent": fields.agent or "med-knowledge-architect",
        "title": fields.title or "",
        "execution_mode": "parallel_authoring_serial_apply",
        "authoring_mode": "parallel",
        "authoring_max_concurrency": 1,
        "apply_mode": "serial",
        "serial_apply_required": True,
        "wait_for_all_authoring_outputs_before_apply": True,
        "agent_instruction": (
            "Use agent_directive.control.effects[].payload.current_batch_items; nao leia o plan_path para descobrir o proximo work_id."
        ),
    }
    if continuation_item is not None:
        payload["current_batch_items"] = [continuation_item]
    return NextSpecialistTask.model_validate(payload)


def _next_specialist_task_after_work_id(*, plan_path: str, current_work_id: str) -> NextSpecialistTask | None:
    if not plan_path or not current_work_id:
        return None
    path = Path(plan_path)
    if not path.is_file():
        return None
    plan = _read_json(path)
    if not isinstance(plan, dict):
        return None
    plan_fields = SubagentBatchPlan.model_validate(plan)
    if plan_fields.phase != "style-rewrite":
        return None
    current_index = next(
        (
            index
            for index, item in enumerate(plan_fields.work_items)
            if item.work_id == current_work_id
        ),
        None,
    )
    if current_index is None:
        return None
    for item in plan_fields.work_items[current_index + 1 :]:
        if item.work_id.strip():
            return _next_specialist_task_from_plan_output(
                phase="style-rewrite",
                plan_path=path,
                work_items=[item.to_payload()],
            )
    return None


def _plan_output_receipt_payload(
    *,
    result: dict[str, object],
    phase: str,
    output: Path,
) -> dict[str, object]:
    plan = SubagentBatchPlan.model_validate(result)
    plan_status = plan.status
    blocked_status = plan_status in {"blocked", "needs_review"}
    plan_attestation = plan.plan_attestation.to_payload() if plan.plan_attestation is not None else {}
    attestation_fields = _PlanAttestationCliFields.model_validate(plan_attestation)
    work_items = [item.to_payload() for item in plan.work_items]
    current_batch_items = [
        _plan_output_receipt_work_item_summary(item)
        for item in work_items
        if isinstance(item, dict)
    ]
    next_specialist_task = _next_specialist_task_from_plan_output(
        phase=phase,
        plan_path=output,
        work_items=work_items,
    )
    error_context_payload = _dict_value(_json_field(result, "error_context"))
    agent_directive = (
        _style_rewrite_plan_output_agent_directive(
            plan_path=output,
            next_specialist_task=next_specialist_task,
        )
        if phase == "style-rewrite" and plan_status == "ready" and next_specialist_task is not None
        else None
    )
    payload = {
        "schema": "medical-notes-workbench.plan-output-receipt.v1",
        "phase": "plan-subagents",
        "status": "blocked" if blocked_status else "written",
        "blocked_reason": plan.blocked_reason if blocked_status else "",
        "next_action": plan.next_action,
        "required_inputs": plan.required_inputs,
        "human_decision_required": plan.human_decision_required,
        "plan_path": str(output),
        "plan_schema": plan.schema_ or "",
        "plan_phase": phase,
        "plan_attestation": plan_attestation or None,
        "plan_hash": attestation_fields.plan_hash,
        "plan_status": plan_status,
        "item_count": plan.item_count,
        "blocked_item_count": plan.blocked_item_count,
        "batch_id": plan.batch_id or "",
        "current_batch_items": current_batch_items,
        "agent_directive": agent_directive,
        "error_context": error_context_payload or None,
    }
    receipt = PlanOutputReceipt.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_defaults=True,
    )
    if agent_directive is not None:
        receipt.update({"agent_directive": _dict_value(agent_directive)})
    return receipt


def _taxonomy_trigger_context(
    result: dict[str, object],
    config: MedConfig,
    *,
    source_workflow: str,
    batch_id: str,
) -> dict[str, object] | None:
    raw_operations = _json_field(result, "applied_operations") or _json_field(result, "rollback_operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        return None
    changed_notes: list[dict[str, object]] = []
    for op in raw_operations:
        if not isinstance(op, dict):
            continue
        operation = _TaxonomyOperationCliFields.model_validate(op)
        if operation.action != "move_dir":
            continue
        source_rel = operation.source
        destination_rel = operation.destination
        destination_dir = config.wiki_dir.joinpath(*Path(destination_rel).parts)
        if not source_rel or not destination_rel or not destination_dir.is_dir():
            continue
        for note in sorted(destination_dir.rglob("*.md")):
            try:
                suffix = note.relative_to(destination_dir).as_posix()
            except ValueError:
                suffix = note.name
            changed_notes.append(
                {
                    "change_type": "moved",
                    "content_change": "structural",
                    "old_path": (Path(source_rel) / suffix).as_posix(),
                    "old_title": note.stem,
                    "path": _wiki_relative_path(note, config),
                    "title": _title_from_note_path(note),
                    "after_hash": _hash_if_present(note),
                }
            )
    if not changed_notes:
        return None
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": source_workflow,
        "batch_id": batch_id,
        "changed_notes": changed_notes,
    }


def _write_optional_taxonomy_report(result: dict[str, object], report_output: str | None) -> None:
    if not report_output:
        return
    output = _path(report_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(str(_json_field(result, "human_report_markdown", "")), encoding="utf-8")
    result["report_path"] = str(output)


def _apply_taxonomy_plan_and_link(
    *,
    plan_path: Path,
    config: MedConfig,
    receipt_path: Path | None,
) -> dict[str, object]:
    result = apply_taxonomy_migration(plan_path, config, receipt_path=receipt_path)
    trigger_context = _taxonomy_trigger_context(
        result,
        config,
        source_workflow="/mednotes:fix-wiki",
        batch_id=str(plan_path),
    )
    if trigger_context is not None:
        _record_linker_run_evidence(
            result,
            _auto_run_linker_from_trigger_context(
                config,
                trigger_context,
                label="taxonomy-migrate",
                include_related_notes=False,
            ),
        )
    return result


def _rollback_taxonomy_receipt_and_link(*, receipt_path: Path, config: MedConfig) -> dict[str, object]:
    result = rollback_taxonomy_migration(receipt_path, config)
    trigger_context = _taxonomy_trigger_context(
        result,
        config,
        source_workflow="/mednotes:fix-wiki",
        batch_id=str(receipt_path),
    )
    if trigger_context is not None:
        _record_linker_run_evidence(
            result,
            _auto_run_linker_from_trigger_context(
                config,
                trigger_context,
                label="taxonomy-rollback",
                include_related_notes=False,
            ),
        )
    return result


def _reference_repair_summary(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    fields = _ReferenceRepairSummaryFields.model_validate(value)
    return {
        "status": fields.status,
        "affected_note_count": fields.affected_note_count,
        "action_count": fields.action_count,
        "blocking_action_count": fields.blocking_action_count,
        "human_decision_count": fields.human_decision_count,
        "triage_count": fields.triage_count,
        "package_mode": fields.package_mode,
        "manual_script_allowed": fields.manual_script_allowed,
        "requires_backup": fields.requires_backup,
        "requires_receipt": fields.requires_receipt,
        "notes_sample": fields.note_actions[:10],
        "structural_actions_sample": fields.structural_actions[:10],
        "catalog_actions_sample": fields.catalog_actions[:10],
    }


def _compact_linker_payload(result: Mapping[str, object]) -> dict[str, object]:
    if _json_field(result, "schema") in {
        "medical-notes-workbench.link-diagnosis.v1",
        "medical-notes-workbench.link-run.v1",
    }:
        blockers = _json_field(result, "blockers")
        blocker_list = blockers if isinstance(blockers, list) else []
        summary_items = list(blocker_list)
        reference = _json_field(result, "reference_repair")
        if isinstance(reference, dict):
            note_actions = reference.get("note_actions", [])
            for note in note_actions if isinstance(note_actions, list) else []:
                if isinstance(note, dict) and isinstance(note.get("actions"), list):
                    summary_items.extend(
                        action
                        for action in note["actions"]
                        if isinstance(action, dict) and action.get("code") != "duplicate_stem"
                    )
            structural_actions = reference.get("structural_actions", [])
            for action in structural_actions if isinstance(structural_actions, list) else []:
                if isinstance(action, dict):
                    summary_items.append(action)
        decision_summary = _json_field(result, "decision_summary")
        if not isinstance(decision_summary, dict):
            for item in blocker_list:
                if not isinstance(item, dict):
                    continue
                item_fields = _LinkerDecisionSummaryCarrierFields.model_validate(item)
                if item_fields.decision_summary:
                    decision_summary = item_fields.decision_summary
                    break
        if not isinstance(decision_summary, dict) and _json_field(result, "blocked_reason") == "link_plan_blocked":
            decision_summary = {
                "kind": "auto_plan",
                "phase": "link_diagnosis",
                "reason_code": "link_plan_blocked",
                "public_summary": "O plano de links tem bloqueios antes da aplicação.",
                "developer_summary": "Link diagnosis produced blockers; follow next_action before apply.",
            }
        body = _json_field(result, "body_term_linker")
        body_payload = body if isinstance(body, dict) else {}
        body_fields = _LinkerBodyTermCliFields.model_validate(body_payload)
        compact: dict[str, object] = {
            "schema": _json_field(result, "schema"),
            "phase": _json_field(result, "phase"),
            "status": _json_field(result, "status"),
            "blocked_reason": _json_field(result, "blocked_reason", ""),
            "stale_reason": _json_field(result, "stale_reason", ""),
            "next_action": _json_field(result, "next_action", ""),
            "required_inputs": _json_field(result, "required_inputs", []),
            "human_decision_required": _json_field(result, "human_decision_required", False),
            "decision_summary": decision_summary,
            "wiki_dir": _json_field(result, "wiki_dir"),
            "catalog_path": _json_field(result, "catalog_path"),
            "vocabulary_db_path": _json_field(result, "vocabulary_db_path"),
            "vocabulary_bootstrap": _json_field(result, "vocabulary_bootstrap"),
            "vocabulary_map_diagnosis": _json_field(result, "vocabulary_map_diagnosis"),
            "vocabulary_curator_batch_plan": _json_field(result, "vocabulary_curator_batch_plan"),
            "vocabulary_curator_batch_plan_path": _json_field(result, "vocabulary_curator_batch_plan_path"),
            "vocabulary_curator_next_action": _json_field(result, "vocabulary_curator_next_action"),
            "vocabulary_semantic_repair": _json_field(result, "vocabulary_semantic_repair"),
            "diagnosis_path": _json_field(result, "diagnosis_path"),
            "receipt_path": _json_field(result, "receipt_path"),
            "plan_hash": _json_field(result, "plan_hash"),
            "snapshot_hash": _json_field(result, "snapshot_hash"),
            "git_status_hash": _json_field(result, "git_status_hash"),
            "expected_git_status_hash": _json_field(result, "expected_git_status_hash"),
            "actual_git_status_hash": _json_field(result, "actual_git_status_hash"),
            "expected_git_head": _json_field(result, "expected_git_head"),
            "actual_git_head": _json_field(result, "actual_git_head"),
            "trigger_context": _json_field(result, "trigger_context"),
            "triggers_detected": _json_field(result, "triggers_detected", []),
            "affected_notes": _json_field(result, "affected_notes", []),
            "skipped_reason": _json_field(result, "skipped_reason", ""),
            "phases": _json_field(result, "phases", {}),
            "contextual_alias_disambiguation": _json_field(result, "contextual_alias_disambiguation")
            or body_fields.contextual_alias_disambiguation,
            "body_term_linker": _json_field(result, "body_term_linker"),
            "reference_repair": _reference_repair_summary(_json_field(result, "reference_repair")),
            "links_planned": _json_field(result, "links_planned", body_fields.links_planned),
            "links_rewritten": _json_field(result, "links_rewritten", body_fields.links_rewritten),
            "files_changed": _json_field(result, "files_changed"),
            "changed_files": _json_field(result, "changed_files", []),
            "blocker_count": _json_field(result, "blocker_count", 0),
            "blocker_summary": _issue_summary(summary_items),
            "blockers_sample": blocker_list[:10],
            "related_notes_sync": _json_field(result, "related_notes_sync"),
            "related_notes_applied": _json_field(result, "related_notes_applied"),
            "related_notes_skipped_reason": _json_field(result, "related_notes_skipped_reason"),
            "body_only_fallback": _json_field(result, "body_only_fallback"),
            "retry_governance": _json_field(result, "retry_governance"),
            "agent_events": _json_field(result, "agent_events"),
            "returncode": _json_field(result, "returncode"),
            "error": _json_field(result, "error"),
            "parse_error": _json_field(result, "parse_error"),
        }
        return {key: value for key, value in compact.items() if value is not None}

    blockers = _json_field(result, "blockers")
    blocker_list = blockers if isinstance(blockers, list) else []
    graph = _json_field(result, "graph_audit_before")
    graph_summary = {}
    if isinstance(graph, dict):
        graph_summary = {
            "ok": graph.get("ok"),
            "error_count": graph.get("error_count"),
            "warning_count": graph.get("warning_count"),
        }
    blocker_count = _int_value(_json_field(result, "blocker_count"))
    parse_error = bool(_json_field(result, "parse_error"))
    error = bool(_json_field(result, "error"))
    related_notes = _json_field(result, "related_notes_sync")
    related_notes_fields = _CompactRelatedNotesCliFields.model_validate(
        related_notes if isinstance(related_notes, dict) else {}
    )
    related_notes_blocked = (
        related_notes_fields.status == "blocked" or bool(related_notes_fields.blocked_reason)
    )
    blocked = bool(_json_field(result, "blocked")) or bool(blocker_count and not _json_field(result, "dry_run"))
    status = (
        "failed"
        if parse_error or error
        else "blocked"
        if related_notes_blocked
        else "completed_with_link_blockers"
        if blocked
        else "preview_ready"
        if _json_field(result, "dry_run")
        else "completed"
    )
    blocked_reason = (
        "related_notes_blocked"
        if related_notes_blocked
        else "graph_blockers"
        if blocked
        else "linker_error"
        if parse_error or error
        else ""
    )
    next_action = (
        related_notes_fields.next_action
        if related_notes_blocked
        else (
        "Rodar /mednotes:fix-wiki --dry-run para resolver blockers semânticos antes do linker real."
        if blocked
        else "Inspecionar stderr/stdout do linker antes de tentar novamente."
        if parse_error or error
        else ""
        )
    )
    catalog_non_blocking_issues = _list_value(_json_field(result, "catalog_non_blocking_issues"))
    compact = {
        "ok": _json_field(result, "ok"),
        "error": _json_field(result, "error"),
        "parse_error": _json_field(result, "parse_error"),
        "blocked": _json_field(result, "blocked"),
        "dry_run": _json_field(result, "dry_run"),
        "returncode": _json_field(result, "returncode"),
        "wiki_dir": _json_field(result, "wiki_dir"),
        "catalog_path": _json_field(result, "catalog_path"),
        "catalog_exists": _json_field(result, "catalog_exists"),
        "catalog_issue_count": _json_field(result, "catalog_issue_count"),
        "catalog_blocker_count": _json_field(result, "catalog_blocker_count"),
        "catalog_non_blocking_issues": catalog_non_blocking_issues[:10],
        "vocabulary_count": _json_field(result, "vocabulary_count"),
        "files_scanned": _json_field(result, "files_scanned"),
        "files_changed": _json_field(result, "files_changed"),
        "links_planned": _json_field(result, "links_planned"),
        "links_rewritten": _json_field(result, "links_rewritten"),
        "blocker_count": _json_field(result, "blocker_count"),
        "blocker_summary": _issue_summary(blocker_list),
        "blockers_sample": blocker_list[:10],
        "graph_audit_before": graph_summary,
        "related_notes_sync": related_notes,
        "related_notes_applied": _json_field(result, "related_notes_applied"),
        "related_notes_skipped_reason": _json_field(result, "related_notes_skipped_reason"),
        "body_linker_skipped_reason": _json_field(result, "body_linker_skipped_reason"),
    }
    stderr = _json_field(result, "stderr")
    if stderr:
        compact["stderr"] = stderr
    return annotate_payload(
        compact,
        phase="run_linker_dry_run" if _json_field(result, "dry_run") else "run_linker_apply",
        status=status,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=LINK_REQUIRED_INPUTS,
        human_decision_required=bool(_json_field(result, "human_decision_required")),
    )


def _validate_without_wiki_payload(args: argparse.Namespace, exc: WikiPathResolutionError) -> JsonObject:
    raw_dir = resolve_raw_dir(
        explicit=getattr(args, "raw_dir", None),
        config=getattr(args, "config", None),
        start=Path.cwd(),
    )
    catalog_path = user_state_dir() / "CATALOGO_WIKI.json"
    path_payload = exc.payload(phase="resolve_wiki_dir")
    path_resolution = WikiPathResolutionPayload.model_validate(path_payload)
    preflight = environment_preflight(
        extension_root=_extension_root(),
        state_dir=user_state_dir(),
        sample_paths=[raw_dir],
    )
    preflight_fields = _EnvironmentPreflightCliFields.model_validate(preflight)
    preflight_required = preflight_fields.required_inputs if preflight_fields.status == "blocked" else []
    required_inputs = sorted(
        {*preflight_required, *path_resolution.required_inputs}
    )
    return JsonObjectAdapter.validate_python({
        "phase": "validate_environment",
        "status": "completed_with_warnings",
        "blocked_reason": "",
        "next_action": path_resolution.next_action or preflight_fields.next_action,
        "required_inputs": required_inputs,
        "human_decision_required": False,
        "raw_dir": str(raw_dir),
        "raw_dir_exists": raw_dir.exists(),
        "wiki_dir": "",
        "wiki_dir_exists": False,
        "wiki_source": path_resolution.wiki_source,
        "wiki_memory_path": path_resolution.memory_path,
        "wiki_compat_warnings": [
            "wiki_dir ainda não configurado; comandos que mutam ou leem Wiki vão bloquear até /mednotes:setup."
        ],
        "catalog_path": str(catalog_path),
        "catalog_path_exists": catalog_path.exists(),
        "artifact_dir": "",
        "artifact_dir_exists": False,
        "path_resolution": path_payload,
        "environment_preflight": preflight,
    })


def _validate_agent_session_payload(transcript_path: Path) -> dict[str, object]:
    try:
        transcript = _load_agent_session_transcript(transcript_path)
    except (OSError, json.JSONDecodeError) as exc:
        return annotate_payload(
            {
                "schema": "medical-notes-workbench.agent-session-validation.v1",
                "phase": "validate_agent_session",
                "status": "blocked",
                "blocked_reason": "agent_session_transcript_unreadable",
                "next_action": "Regerar ou informar um transcript JSON/NDJSON legível e repetir validate-agent-session.",
                "required_inputs": ["transcript"],
                "human_decision_required": False,
                "transcript_path": str(transcript_path),
                "error": str(exc),
            },
            phase="validate_agent_session",
            status="blocked",
            blocked_reason="agent_session_transcript_unreadable",
            next_action="Regerar ou informar um transcript JSON/NDJSON legível e repetir validate-agent-session.",
            required_inputs=["transcript"],
            human_decision_required=False,
        )

    findings = validate_agent_tool_calls(transcript)
    status = "blocked" if findings else "completed"
    blocked_reason = "agent_tool_contract_violation" if findings else ""
    next_action = (
        "Reportar os desvios de tool/subagent/comando encontrados e repetir a rodada com a rota oficial compacta."
        if findings
        else ""
    )
    return annotate_payload(
        {
            "schema": "medical-notes-workbench.agent-session-validation.v1",
            "phase": "validate_agent_session",
            "status": status,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "required_inputs": [],
            "human_decision_required": False,
            "transcript_path": str(transcript_path),
            "finding_count": len(findings),
            "findings": findings,
        },
        phase="validate_agent_session",
        status=status,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=[],
        human_decision_required=False,
    )


def _validate_agent_run_report_payload(
    *,
    workflow_payload_path: Path,
    transcript_path: Path | None,
    final_report_path: Path | None,
    runtime_log_paths: list[Path] | None = None,
) -> dict[str, object]:
    workflow_payload = _read_json(workflow_payload_path)
    if not isinstance(workflow_payload, dict):
        raise ValidationError("workflow payload must be a JSON object")
    runtime_log_paths = runtime_log_paths or []
    discovered_transcript_path = None
    if transcript_path is None:
        discovered_transcript_path = _discover_agy_transcript_path(runtime_log_paths)
        transcript_path = discovered_transcript_path
    transcript = _load_agent_session_transcript(transcript_path) if transcript_path is not None else None
    final_report_text = final_report_path.read_text(encoding="utf-8") if final_report_path is not None else None
    runtime_log_text = _load_runtime_logs(runtime_log_paths)
    result = validate_agent_run_report(
        workflow_payload=workflow_payload,
        transcript=transcript,
        final_report_text=final_report_text,
        runtime_log_text=runtime_log_text or None,
        workflow_payload_path=workflow_payload_path,
        transcript_path=transcript_path,
        final_report_path=final_report_path,
        runtime_log_paths=runtime_log_paths,
    )
    payload = result.to_payload()
    payload["workflow_payload_path"] = str(workflow_payload_path)
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    if discovered_transcript_path is not None:
        payload["transcript_auto_discovered"] = True
    if final_report_path is not None:
        payload["final_report_path"] = str(final_report_path)
    if runtime_log_paths:
        payload["runtime_log_paths"] = [str(path) for path in runtime_log_paths]
    return payload


def _summarize_happy_path_round_payload(validation_paths: Sequence[Path]) -> JsonObject:
    if not validation_paths:
        raise ValidationError("summarize-happy-path-round requires at least one --validation file")
    runs = [_happy_path_run_metrics_from_validation(path) for path in validation_paths]
    workflows = {run.workflow for run in runs}
    if len(workflows) != 1:
        raise ValidationError(
            "happy_path_round.workflow_mismatch: all validation files in one round must use the same workflow"
        )
    return happy_path_round_metrics(workflow=runs[0].workflow, runs=runs).to_payload()


def _happy_path_run_metrics_from_validation(path: Path) -> HappyPathRunMetrics:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValidationError(f"happy_path_round.invalid_validation: {path} must contain a JSON object")
    envelope = _HappyPathValidationEnvelopeFields.model_validate(payload)
    metrics_payload = envelope.happy_path_metrics
    if metrics_payload is None and envelope.schema_id == "medical-notes-workbench.happy-path-run-metrics.v1":
        metrics_payload = JsonObjectAdapter.validate_python(payload)
    if not isinstance(metrics_payload, dict):
        raise ValidationError(f"happy_path_round.metrics_missing: {path} does not contain happy_path_metrics")
    try:
        return HappyPathRunMetrics.model_validate(metrics_payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"happy_path_round.metrics_invalid: {path}: {exc}") from exc


def _discover_agy_transcript_path(runtime_log_paths: list[Path]) -> Path | None:
    for path in runtime_log_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(
            r"(?:Print mode: conversation=|Created conversation )"
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            text,
        )
        if not match:
            continue
        candidate = (
            Path.home()
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / match.group(1)
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
        if candidate.exists():
            return candidate
    return None


def _load_runtime_logs(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        if not path.exists():
            raise MissingPathError(f"Runtime log not found: {path}")
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def _load_agent_session_transcript(transcript_path: Path) -> object:
    text = transcript_path.read_text(encoding="utf-8", errors="replace")
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        rows: list[object] = []
        strict_ndjson_failed = False
        for line in stripped.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if not candidate.startswith(("{", "[")):
                strict_ndjson_failed = True
                continue
            try:
                rows.append(json.loads(candidate))
            except json.JSONDecodeError:
                strict_ndjson_failed = True
        if rows:
            return rows
        if strict_ndjson_failed:
            raise exc
        return rows


def _environment_preflight_cli_payload() -> JsonObject:
    # O agent invoca o SHIM (bundle/scripts/mednotes/wiki/cli.py), não este lib;
    # o caminho canônico exibido é sempre o relpath agent-facing.
    canonical_cli = WIKI_CLI_RELPATH
    raw = environment_preflight(
        extension_root=_extension_root(),
        state_dir=user_state_dir(),
    )
    fields = _EnvironmentPreflightCliFields.model_validate(raw)
    blocked = fields.status == "blocked" or bool(fields.blockers)
    uv_environment = os.environ.get("UV_PROJECT_ENVIRONMENT") or fields.persistent_venv
    return JsonObjectAdapter.validate_python({
        "schema": "medical-notes-workbench.environment-preflight.v1",
        "status": "blocked" if blocked else "ready",
        "blocked_reason": fields.blocked_reason or "environment_blocker.windows_path_or_venv" if blocked else "",
        "canonical_command": f"uv run python {canonical_cli} <command>",
        "python_executable": sys.executable,
        "uv_project_environment": uv_environment,
        "next_action": fields.next_action if blocked else "",
        "required_inputs": fields.required_inputs,
        "human_decision_required": False,
        "environment_preflight": raw,
    })


def _status_cli_payload(config: MedConfig) -> JsonObject:
    validation = validate_config(config)
    fields = _StatusValidationCliFields.model_validate(validation)
    missing_paths = [
        name
        for name, exists in (
            ("raw_dir", fields.raw_dir_exists),
            ("wiki_dir", fields.wiki_dir_exists),
        )
        if not exists
    ]
    warnings: list[str] = []
    if not fields.vocabulary_db_exists:
        warnings.append("vocabulary_db_missing")
    if not fields.catalog_path_exists:
        warnings.append("catalog_missing")

    blocked = fields.status == "blocked" or bool(missing_paths)
    status = "blocked" if blocked else "completed_with_warnings" if warnings else "ready"
    blocked_reason = fields.blocked_reason
    if blocked and missing_paths and not blocked_reason:
        blocked_reason = "paths_missing"
    next_action = fields.next_action
    if blocked and not next_action:
        next_action = "Rodar /mednotes:setup para configurar paths locais antes de executar workflows da Wiki."
    required_inputs = sorted({*fields.required_inputs, *missing_paths})
    return StatusSnapshot(
        status=status,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=required_inputs,
        human_decision_required=False,
        raw_dir=fields.raw_dir,
        raw_dir_exists=fields.raw_dir_exists,
        wiki_dir=fields.wiki_dir,
        wiki_dir_exists=fields.wiki_dir_exists,
        wiki_source=fields.wiki_source,
        wiki_memory_path=fields.wiki_memory_path,
        config_path=fields.config_path,
        catalog_path=fields.catalog_path,
        catalog_path_exists=fields.catalog_path_exists,
        vocabulary_db_path=fields.vocabulary_db_path,
        vocabulary_db_exists=fields.vocabulary_db_exists,
        warnings=warnings,
        environment_preflight=fields.environment_preflight,
        validate_environment=JsonObjectAdapter.validate_python(validation),
    ).to_payload()


def _status_without_wiki_payload(args: argparse.Namespace, exc: WikiPathResolutionError) -> JsonObject:
    validation = _validate_without_wiki_payload(args, exc)
    fields = _StatusValidationCliFields.model_validate(validation)
    path_resolution = WikiPathResolutionPayload.model_validate(fields.path_resolution)
    return StatusSnapshot(
        status="blocked",
        blocked_reason=path_resolution.blocked_reason or "wiki_path_missing",
        next_action=fields.next_action,
        required_inputs=fields.required_inputs,
        human_decision_required=fields.human_decision_required,
        raw_dir=fields.raw_dir,
        raw_dir_exists=fields.raw_dir_exists,
        wiki_dir="",
        wiki_dir_exists=False,
        wiki_source=fields.wiki_source,
        wiki_memory_path=fields.wiki_memory_path,
        catalog_path=fields.catalog_path,
        catalog_path_exists=fields.catalog_path_exists,
        vocabulary_db_path="",
        vocabulary_db_exists=False,
        warnings=["wiki_dir_missing"],
        path_resolution=fields.path_resolution,
        environment_preflight=fields.environment_preflight,
        validate_environment=JsonObjectAdapter.validate_python(validation),
    ).to_payload()


def _markdown_query_schema(command: str) -> str:
    suffix = command.removeprefix("markdown-query-").replace("-", "_")
    return f"medical-notes-workbench.markdown-query-{suffix.replace('_', '-')}.v1"


def _markdown_query_phase(command: str) -> str:
    return command.replace("-", "_")


def _markdown_query_required_inputs() -> list[str]:
    return ["wiki_dir", "raw_dir", "markdown_query_index"]


def _markdown_query_blocked_cli_payload(
    *,
    command: str,
    runtime: JsonObject | None = None,
    adapter: JsonObject | None = None,
    error: str = "",
) -> JsonObject:
    payload = JsonObjectAdapter.validate_python({
        "schema": _markdown_query_schema(command),
        "runtime": JsonObjectAdapter.validate_python(runtime or {}),
        "adapter": JsonObjectAdapter.validate_python(adapter or {}),
    })
    if error:
        payload["error"] = error
    return JsonObjectAdapter.validate_python(
        annotate_payload(
            payload,
            phase=_markdown_query_phase(command),
            status="blocked",
            blocked_reason=MARKDOWN_QUERY_BLOCKED_REASON,
            next_action=MARKDOWN_QUERY_NEXT_ACTION,
            required_inputs=_markdown_query_required_inputs(),
            human_decision_required=False,
        )
    )


def _markdown_query_cli_payload(config: MedConfig, command: str) -> JsonObject:
    extension_root = _extension_root()
    runtime: JsonObject
    node_modules_path: Path | None = None
    if command == "markdown-query-status":
        runtime = markdown_node_runtime_status(
            extension_root=extension_root,
            state_dir=user_state_dir(),
        )
        runtime_fields = _MarkdownQueryCliFields.model_validate(runtime)
        if runtime_fields.status == "ready":
            node_modules_path = Path(runtime_fields.node_modules_path)
    else:
        try:
            runtime = ensure_markdown_node_runtime(
                extension_root=extension_root,
                state_dir=user_state_dir(),
            )
            runtime_fields = _MarkdownQueryCliFields.model_validate(runtime)
            node_modules_path = Path(runtime_fields.node_modules_path)
        except MarkdownNodeRuntimeUnavailable as exc:
            return _markdown_query_blocked_cli_payload(
                command=command,
                runtime=exc.payload,
                error=str(exc),
            )

    provider = MarkdownDbChatMetadataProvider(
        wiki_dir=config.wiki_dir,
        raw_dir=config.raw_dir,
        node_modules_path=node_modules_path,
    )
    try:
        if command == "markdown-query-status":
            adapter = provider.status()
        elif command == "markdown-query-rebuild":
            adapter = provider.rebuild()
        elif command == "markdown-query-probe":
            adapter = provider.probe()
        else:  # pragma: no cover - argparse prevents this
            raise MarkdownQueryUnavailable(f"Unknown Markdown query command: {command}")
    except MarkdownQueryUnavailable as exc:
        return _markdown_query_blocked_cli_payload(
            command=command,
            runtime=runtime,
            adapter=exc.payload,
            error=str(exc),
        )

    adapter_fields = _MarkdownQueryCliFields.model_validate(adapter)
    runtime_fields = _MarkdownQueryCliFields.model_validate(runtime)
    ready = adapter_fields.status == "ready"
    if command == "markdown-query-status":
        ready = ready and runtime_fields.status == "ready"
    payload = {
        "schema": _markdown_query_schema(command),
        "runtime": runtime,
        "adapter": adapter,
    }
    return annotate_payload(
        payload,
        phase=_markdown_query_phase(command),
        status="ready" if ready else "blocked",
        blocked_reason="" if ready else MARKDOWN_QUERY_BLOCKED_REASON,
        next_action="" if ready else MARKDOWN_QUERY_NEXT_ACTION,
        required_inputs=[] if ready else _markdown_query_required_inputs(),
        human_decision_required=False,
    )


def _no_resource_mutation_safety() -> VersionControlSafety:
    """Default safety proof for exception projections that perform no mutation."""

    return VersionControlSafety(
        no_resource_mutation=True,
        rollback_declared=False,
        resource_guard_active=False,
        run_start_seen=False,
        run_finish_seen=False,
        restore_point_before=False,
        restore_point_after=False,
        sync_status="not_started",
        backup_online="not_started",
        direct_mutation_forbidden=True,
        mutation_without_guard=False,
        changed_file_count=0,
    )


def _fsm_first_exception_payload(
    args: argparse.Namespace,
    exc: BaseException,
    exit_code: int,
) -> JsonObject | None:
    """Project global exceptions for FSM-first commands through their public contracts."""

    command = _namespace_string(args, "command")
    if command not in FSM_FIRST_EXCEPTION_COMMANDS:
        return None
    run_id = f"{command}-exception-{int(time.time() * 1000)}"
    context = _fsm_first_exception_error_context(args, exc)
    context_fields = _RuntimeErrorContextCliFields.model_validate(context)
    if command == "fix-wiki":
        return build_fix_wiki_fsm_result(
            fix_wiki_fsm_facts_from_runtime(
                run_id=run_id,
                requested_apply=bool(getattr(args, "apply", False)),
                effective_apply=bool(getattr(args, "apply", False)) and not bool(getattr(args, "dry_run", False)),
                failed=True,
                failed_reason_code=context_fields.root_cause,
                vault_guard_required=context_fields.root_cause == "vault_guard_required",
                environment_windows_path_or_venv_blocked=context_fields.root_cause
                == "environment_blocker.windows_path_or_venv",
                next_action=context_fields.next_action,
                required_inputs=[],
                version_control_safety=_no_resource_mutation_safety(),
                diagnostic_context={
                    "schema": "medical-notes-workbench.fsm-first-cli-exception.v1",
                    "command": command,
                    "exit_code": exit_code,
                },
                error_context=context,
            )
        ).to_payload()
    if command == "run-linker":
        return _link_fsm_payload_from_result(
            {
                "schema": "medical-notes-workbench.link-run.v1",
                "phase": command,
                "status": "failed",
                "blocked_reason": context_fields.root_cause,
                "next_action": context_fields.next_action,
                "returncode": max(exit_code, 0),
                "error": context_fields.error_summary,
                "required_inputs": context_fields.missing_inputs,
            },
            args,
        )
    if command == "related-notes-sync":
        return _link_related_fsm_payload_from_result(
            {
                "schema": "medical-notes-workbench.related-notes-sync.v1",
                "phase": command,
                "status": "failed",
                "blocked_reason": context_fields.root_cause,
                "next_action": context_fields.next_action,
                "error": context_fields.error_summary,
                "error_context": _link_related_exception_runtime_error_context(context),
            },
            mode="recover_export"
            if bool(getattr(args, "recover_export", False))
            else "apply"
            if bool(getattr(args, "apply", False))
            else "dry_run",
            applying=bool(getattr(args, "apply", False)),
        )
    return _process_chats_exception_payload(command=command, run_id=run_id, context=context)


def _process_chats_exception_payload(*, command: str, run_id: str, context: JsonObject) -> JsonObject:
    """Build process-chats failure output from a canonical machine event."""

    context_fields = _RuntimeErrorContextCliFields.model_validate(context)
    root_cause = context_fields.root_cause
    if root_cause in PROCESS_CHATS_RECOVERABLE_EXCEPTION_REASONS:
        return process_chats_fsm_payload_from_publish_result(
            {
                "schema": "medical-notes-workbench.process-chats-publish-operation-result.v1",
                "workflow": "/mednotes:process-chats",
                "phase": command,
                "status": "blocked",
                "blocked_reason": root_cause,
                "next_action": context_fields.next_action,
                "required_inputs": context_fields.missing_inputs,
                "error_context": context,
                "diagnostic_context": {
                    "schema": "medical-notes-workbench.fsm-first-cli-exception.v1",
                    "command": command,
                    "recoverable": True,
                },
                "manifest": context_fields.affected_artifact,
                "runtime_observation": _process_chats_exception_runtime_observation(context_fields),
            },
            run_id=run_id,
            version_control_safety=_no_resource_mutation_safety(),
        )
    process_error = ProcessChatsErrorContext.model_validate(
        {
            "root_cause": context_fields.root_cause,
            "affected_artifact": context_fields.affected_artifact,
            "retry_scope": context_fields.retry_scope,
            "next_action": context_fields.next_action,
        }
    )
    event = RollbackFailureRecordedEvent(
        workflow="/mednotes:process-chats",
        run_id=run_id,
        current_state=ProcessChatsState.ROLLBACK_RECORDED.value,
        error_context=process_error,
        audit_evidence={
            "schema": "medical-notes-workbench.fsm-first-cli-exception.v1",
            "command": command,
        },
    )
    return build_process_chats_fsm_result(
        ProcessChatsFsmFacts(
            run_id=run_id,
            initial_state=ProcessChatsState.ROLLBACK_RECORDED,
            event=event,
            operational_summary=ProcessChatsOperationalSummary(next_action=context_fields.next_action),
            version_control_safety=_no_resource_mutation_safety(),
            error_context=context,
        )
    ).to_payload()


def _process_chats_exception_runtime_observation(context_fields: _RuntimeErrorContextCliFields) -> JsonObject:
    """Translate a typed CLI exception into the process-chats publish observation."""

    root_cause = context_fields.root_cause
    process_error = ProcessChatsErrorContext(
        root_cause=root_cause,
        affected_artifact=context_fields.affected_artifact,
        retry_scope=context_fields.retry_scope,
        next_action=context_fields.next_action,
    )
    base = {
        "reason_code": root_cause,
        "next_action": context_fields.next_action,
        "manifest_path": context_fields.affected_artifact,
        "receipt_id": context_fields.affected_artifact or root_cause,
        "error_context": process_error,
    }
    match root_cause:
        case "coverage_path_missing" | "coverage_invalid":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.NOTE_VALIDATION_RUNNING,
                validation_coverage_gap=True,
                **base,
            ).to_payload()
        case "manifest_invalid" | "manifest_mismatch":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.NOTE_VALIDATION_RUNNING,
                validation_manifest_mismatch=True,
                **base,
            ).to_payload()
        case "validation_errors" | "validation_failed" | "requires_llm_rewrite":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.NOTE_VALIDATION_RUNNING,
                validation_content_invalid=True,
                **base,
            ).to_payload()
        case "dry_run_receipt_required":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                publish_dry_run_receipt_required=True,
                **base,
            ).to_payload()
        case "dry_run_receipt_invalid" | "new_taxonomy_leaf_requires_dry_run_authorization" | "stale_receipt":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                publish_stale_receipt=True,
                **base,
            ).to_payload()
        case "duplicate_target" | "duplicate_obsidian_target":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                publish_duplicate_target=True,
                **base,
            ).to_payload()
        case "provenance_gap":
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                publish_provenance_gap=True,
                **base,
            ).to_payload()
        case _:
            return ProcessChatsPublishRuntimeObservation(
                source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                blocked=True,
                **base,
            ).to_payload()


def _link_related_exception_runtime_error_context(context: JsonObject) -> JsonObject:
    """Conform generic workflow error context to the Related Notes runtime boundary."""

    fields = _RuntimeErrorContextCliFields.model_validate(context)
    return JsonObjectAdapter.validate_python(
        {
            "phase": fields.phase,
            "blocked_reason": fields.blocked_reason,
            "root_cause": fields.root_cause,
            "affected_artifact": fields.affected_artifact,
            "error_summary": fields.error_summary,
            "suggested_fix": fields.suggested_fix,
            "next_action": fields.next_action,
            "retry_scope": fields.retry_scope,
            "details": {},
        }
    )


def _fsm_first_exception_error_context(args: argparse.Namespace, exc: BaseException) -> JsonObject:
    """Typed actionable context shared by all FSM-first exception projectors."""

    command = _namespace_string(args, "command", "workflow") or "workflow"
    specialized = _specialized_fsm_first_exception_error_context(args, exc)
    if specialized is not None:
        return specialized
    root_cause = exc.__class__.__name__
    next_action = _unexpected_error_next_action(f"blocked.{root_cause.lower()}")
    return error_context(
        phase=command,
        blocked_reason=root_cause,
        root_cause=root_cause,
        affected_artifact=command,
        error_summary=_redacted_exception_summary(exc),
        suggested_fix=next_action,
        next_action=next_action,
        retry_scope=f"{command.replace('-', '_')}_official_retry",
        missing_inputs=[],
        human_decision_required=False,
    )


def _specialized_fsm_first_exception_error_context(
    args: argparse.Namespace,
    exc: BaseException,
) -> JsonObject | None:
    """Preserve typed domain recovery codes when exceptions cross the CLI boundary."""

    command = _namespace_string(args, "command", "workflow") or "workflow"
    if isinstance(exc, VaultGuardError):
        block = _VaultGuardCliBlockFields.model_validate(exc.to_payload())
        return error_context(
            phase=command,
            blocked_reason=block.blocked_reason,
            root_cause=block.blocked_reason,
            affected_artifact=str(exc.vault_dir),
            error_summary=block.human_message,
            suggested_fix=block.next_action,
            next_action=block.next_action,
            retry_scope="vault_guard_run_start_then_retry",
            missing_inputs=block.required_inputs,
            human_decision_required=False,
        )
    if isinstance(exc, WikiPathResolutionError):
        block = _PathResolutionCliBlockFields.model_validate(exc.payload(phase=f"{command}_path_resolution"))
        reason = block.blocked_reason or "paths.wiki_dir_missing"
        next_action = block.next_action or str(exc)
        missing_inputs = list(block.required_inputs)
        if not missing_inputs and reason in {"paths.wiki_dir_missing", "missing_wiki_dir", "wiki_dir_missing"}:
            missing_inputs = ["wiki_dir"]
        return error_context(
            phase=command,
            blocked_reason=reason,
            root_cause=reason,
            affected_artifact="wiki_dir",
            error_summary=str(exc),
            suggested_fix=next_action,
            next_action=next_action,
            retry_scope="configure_paths_then_retry",
            missing_inputs=missing_inputs,
            human_decision_required=False,
        )
    if isinstance(exc, MedOpsError):
        payload = _known_error_feedback_payload(args, exc, exc.exit_code)
        known_error = _KnownErrorFeedbackPayloadFields.model_validate(payload)
        if known_error.error_context:
            return known_error.error_context
    return None


def _known_error_feedback_payload(args: argparse.Namespace, exc: MedOpsError, exit_code: int) -> dict[str, object]:
    phase = getattr(args, "command", "unknown")
    status = "blocked" if exit_code == EXIT_VALIDATION else "failed"
    message = str(exc)
    human_decision_reason = _human_decision_blocked_reason_from_exception(exc)
    payload: dict[str, object] = {
        "phase": phase,
        "status": status,
        "blocked_reason": exc.__class__.__name__,
        "next_action": "",
        "required_inputs": [],
        "error": message,
        "error_type": exc.__class__.__name__,
    }
    payload.update(_known_error_artifact_paths(args, phase))
    if isinstance(exc, ValidationError) and phase == "publish-batch":
        payload.update(_publish_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase == "triage":
        payload.update(_triage_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase == "stage-note":
        if human_decision_reason == "taxonomy_resolution_required":
            payload.update(_stage_note_validation_feedback(f"{human_decision_reason}: {message}"))
        else:
            payload.update(_stage_note_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase == "taxonomy-resolve":
        payload.update(_taxonomy_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase == "apply-curator-batch":
        payload.update(_curator_batch_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase == "eval-agent-behavior-corpus":
        payload.update(_agent_behavior_corpus_validation_feedback(message))
    elif isinstance(exc, ValidationError) and phase in {
        "finalize-style-rewrite-output",
        "collect-style-rewrite-outputs",
        "apply-style-rewrite",
        "apply-specialist-style-rewrite",
    }:
        payload.update(_style_rewrite_validation_feedback(message, phase=phase))
    elif (
        isinstance(exc, ValidationError)
        and phase == "vocabulary-recover"
        and not message.startswith("artifact_encoding.")
    ):
        payload.update(_vocabulary_recover_validation_feedback(message))
    elif isinstance(exc, ValidationError) and message.startswith("artifact_encoding."):
        code = message.split(":", 1)[0]
        payload.update(
            _attach_error_context(
                {
                    "blocked_reason": code,
                    "next_action": (
                        "Regenerar o artefato pelo comando oficial com --output/--plan-output/--report "
                        "ou gravar JSON como UTF-8 sem BOM; nao usar redirecionamento PowerShell legado."
                    ),
                    "required_inputs": ["utf8_json_artifact"],
                    "human_decision_required": False,
                },
                phase=phase,
                message=message,
                affected_artifact="json_artifact",
                suggested_fix="Regenerar o JSON como UTF-8 pelo proprio CLI.",
                retry_scope="regenerate_artifact_encoding",
                root_cause=code,
            )
        )
    _attach_human_decision_from_exception(payload, exc)
    return payload


def _unexpected_error_feedback_payload(args: argparse.Namespace, exc: Exception, exit_code: int) -> dict[str, object]:
    phase = str(getattr(args, "command", "unknown"))
    root_cause = f"blocked.{exc.__class__.__name__.lower()}"
    summary = _redacted_exception_summary(exc)
    next_action = _unexpected_error_next_action(root_cause)
    payload: dict[str, object] = {
        "phase": phase,
        "status": "failed",
        "blocked_reason": root_cause,
        "next_action": next_action,
        "required_inputs": [],
        "diagnostic_context": {
            "root_cause_code": root_cause,
            "traceback_summary": summary,
        },
    }
    payload["error_context"] = error_context(
        phase=phase,
        blocked_reason=root_cause,
        root_cause=root_cause,
        affected_artifact=phase,
        error_summary=summary,
        suggested_fix=next_action,
        next_action=next_action,
        retry_scope="fix_runtime_error_before_retry",
    )
    return payload


def _unexpected_error_next_action(root_cause: str) -> str:
    if root_cause == "blocked.integrityerror":
        return (
            "Parar o retry e corrigir a inconsistência do banco/artefato pelo workflow oficial "
            "antes de repetir o comando."
        )
    return "Inspecionar o erro resumido, corrigir a causa runtime e repetir o comando oficial."


def _redacted_exception_summary(exc: BaseException) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    text = re.sub(r"(?i)(bearer|api[_-]?key|token|secret|password)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"(?<![\w.-])/Users/[^ \n\t:;]+", "[redacted-path]", text)
    text = text.replace(".env", "[redacted-env]")
    return text[:1000]


def _attach_human_decision_from_exception(payload: dict[str, object], exc: BaseException) -> None:
    packet = _human_decision_packet_payload_from_exception(exc)
    if not packet:
        return
    payload.update(attach_human_decision_packet(dict(payload), packet=packet))
    error_ctx = _json_field(payload, "error_context")
    if isinstance(error_ctx, dict):
        error_ctx["human_decision_required"] = bool(_json_field(payload, "human_decision_required"))


def _human_decision_blocked_reason_from_exception(exc: BaseException) -> str:
    packet = _human_decision_packet_payload_from_exception(exc)
    if not packet:
        return ""
    return _HumanDecisionExceptionPacketFields.model_validate(packet).blocked_reason


def _human_decision_packet_payload_from_exception(exc: BaseException) -> JsonObject:
    """Normalize an exception-owned decision packet at the CLI boundary only."""

    packet = getattr(exc, "human_decision_packet", None)
    if isinstance(packet, ContractModel):
        return JsonObjectAdapter.validate_python(packet.to_payload())
    if isinstance(packet, dict):
        return JsonObjectAdapter.validate_python(packet)
    return {}


def _known_error_artifact_paths(args: argparse.Namespace, phase: str) -> dict[str, str]:
    fields_by_phase = {
        "stage-note": {
            "manifest": "manifest",
            "raw_file": "raw_file",
            "content_path": "content",
            "coverage_path": "coverage",
        },
        "publish-batch": {"manifest": "manifest"},
        "publish-status": {"manifest": "manifest"},
        "triage": {"raw_file": "raw_file", "note_plan_path": "note_plan"},
    }
    fields = fields_by_phase.get(phase, {})
    paths: dict[str, str] = {}
    for output_key, arg_key in fields.items():
        value = getattr(args, arg_key, None)
        if value:
            paths[output_key] = str(value)
    return paths


def _attach_error_context(
    payload: dict[str, object],
    *,
    phase: str,
    message: str,
    affected_artifact: str,
    suggested_fix: str,
    retry_scope: str,
    root_cause: str | None = None,
    affected_items: list[str] | None = None,
    max_attempts: int | None = None,
) -> dict[str, object]:
    blocked_reason = str(_json_field(payload, "blocked_reason") or "ValidationError")
    next_action = str(_json_field(payload, "next_action") or suggested_fix)
    missing_inputs = _str_list_value(_json_field(payload, "missing_inputs"))
    payload["error_context"] = error_context(
        phase=phase,
        blocked_reason=blocked_reason,
        root_cause=root_cause or blocked_reason,
        affected_artifact=affected_artifact,
        error_summary=message,
        suggested_fix=suggested_fix,
        next_action=next_action,
        retry_scope=retry_scope,
        affected_items=affected_items,
        missing_inputs=missing_inputs,
        max_attempts=max_attempts,
        human_decision_required=bool(_json_field(payload, "human_decision_required")),
    )
    return payload


class _ValidationFeedbackSpec(ContractModel):
    """Closed CLI feedback route selected only by explicit validation codes."""

    model_config = ConfigDict(extra="forbid")

    blocked_reason: str = ""
    next_action: str
    required_inputs: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool | None = None
    affected_artifact: str
    suggested_fix: str
    retry_scope: str
    root_cause: str = ""
    max_attempts: int | None = None
    agent_notice: bool = False


class _ValidationFeedbackPayload(ContractModel):
    """Typed CLI feedback payload before attaching the structured error context."""

    model_config = ConfigDict(extra="forbid")

    next_action: str
    required_inputs: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    missing_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool | None = None
    agent_notice: str = ""

    def to_payload(self) -> JsonObject:
        payload = {
            "next_action": self.next_action,
            "required_inputs": self.required_inputs,
            **({"blocked_reason": self.blocked_reason} if self.blocked_reason else {}),
            **({"missing_inputs": self.missing_inputs} if self.missing_inputs else {}),
            **(
                {"human_decision_required": self.human_decision_required}
                if self.human_decision_required is not None
                else {}
            ),
            **({"agent_notice": self.agent_notice} if self.agent_notice else {}),
        }
        return JsonObjectAdapter.validate_python(payload)


def _validation_message_code(
    message: str,
    *,
    registered_codes: set[str],
    exact_messages: Mapping[str, str] | None = None,
) -> str:
    """Extract an explicit producer code without classifying by prose text."""

    stripped = message.strip()
    exact = exact_messages or {}
    if stripped in exact:
        return exact[stripped]
    prefix = stripped.split(":", 1)[0].strip()
    if prefix in registered_codes:
        return prefix
    first_token = stripped.split(maxsplit=1)[0].strip()
    if first_token in registered_codes:
        return first_token
    return ""


def _feedback_payload_from_spec(spec: _ValidationFeedbackSpec, *, phase: str, message: str) -> dict[str, object]:
    """Render a coded validation route into the stable CLI error payload."""

    payload = _ValidationFeedbackPayload(
        next_action=spec.next_action,
        required_inputs=spec.required_inputs,
        blocked_reason=spec.blocked_reason,
        missing_inputs=spec.missing_inputs,
        human_decision_required=spec.human_decision_required,
        agent_notice=agent_output_ignored_notice(spec.next_action) if spec.agent_notice else "",
    ).to_payload()
    return _attach_error_context(
        payload,
        phase=phase,
        message=message,
        affected_artifact=spec.affected_artifact,
        suggested_fix=spec.suggested_fix,
        retry_scope=spec.retry_scope,
        root_cause=spec.root_cause or spec.blocked_reason or "ValidationError",
        max_attempts=spec.max_attempts,
    )


def _publish_validation_feedback(message: str) -> dict[str, object]:
    next_actions = {
        "requires_llm_rewrite": "Chamar med-knowledge-architect com rewrite_prompt/error_context para reescrever a nota temporária, validar novamente e repetir publish-batch --dry-run.",
        "batch_state_mismatch": "Regenerar os artefatos downstream a partir do note_plan atual: coverage, manifest via stage-note --coverage e então publish-batch --dry-run.",
        "provenance_gap": "Completar a proveniência multi-fonte antes de publicar.",
        "human_decision_required": "Resolver a decisão humana pendente e repetir publish-batch --dry-run.",
        "coverage_path_missing": "Gerar coverage_path a partir do note_plan, repetir stage-note --coverage <coverage.json> e depois publish-batch --dry-run.",
        "coverage_invalid": "Corrigir ou regenerar coverage a partir do note_plan, repetir stage-note --coverage <coverage.json> e depois publish-batch --dry-run.",
        "new_taxonomy_leaf_requires_dry_run_authorization": "Rodar publish-batch --dry-run com o mesmo manifest e autorização de novo leaf de taxonomia; depois repetir o publish real usando o receipt atualizado.",
        "dry_run_receipt_invalid": "Rodar publish-batch --dry-run com o mesmo manifest/opções antes do publish real.",
        "manifest_invalid": "Corrigir ou regenerar manifest via stage-note --coverage antes de repetir publish-batch.",
    }
    code = _validation_message_code(message, registered_codes=set(next_actions))
    if not code:
        return {}
    artifact_by_code = {
        "requires_llm_rewrite": "content_path",
        "batch_state_mismatch": "batch_state",
        "provenance_gap": "coverage_path",
        "human_decision_required": "manifest",
        "coverage_path_missing": "coverage_path",
        "coverage_invalid": "coverage_path",
        "new_taxonomy_leaf_requires_dry_run_authorization": "dry_run_receipt",
        "dry_run_receipt_invalid": "dry_run_receipt",
        "manifest_invalid": "manifest",
    }
    retry_by_code = {
        "requires_llm_rewrite": "style_rewrite_then_publish_dry_run",
        "batch_state_mismatch": "regenerate_artifact_chain",
        "provenance_gap": "coverage_and_note_provenance",
        "human_decision_required": "resolve_human_decision_then_publish",
        "coverage_path_missing": "coverage_then_publish_dry_run",
        "coverage_invalid": "coverage_then_publish_dry_run",
        "new_taxonomy_leaf_requires_dry_run_authorization": "publish_dry_run_then_apply",
        "dry_run_receipt_invalid": "publish_dry_run_then_apply",
        "manifest_invalid": "stage_then_publish_dry_run",
    }
    missing_inputs = ["coverage_path"] if code == "coverage_path_missing" else []
    if code in {"new_taxonomy_leaf_requires_dry_run_authorization", "dry_run_receipt_invalid"}:
        missing_inputs = ["dry_run_receipt"]
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason=code,
            next_action=next_actions[code],
            required_inputs=PUBLISH_REQUIRED_INPUTS,
            missing_inputs=missing_inputs,
            affected_artifact=artifact_by_code[code],
            suggested_fix=next_actions[code],
            retry_scope=retry_by_code[code],
            root_cause=code,
            max_attempts=2 if code == "requires_llm_rewrite" else None,
        ),
        phase="publish-batch",
        message=message,
    )


def _agent_behavior_corpus_validation_feedback(message: str) -> dict[str, object]:
    if "report_would_overwrite_baseline" not in message:
        return {}
    next_action = (
        "Gravar o relatório de eval-agent-behavior-corpus em um arquivo separado e promover o eval interno "
        "para o baseline pela rota oficial; não sobrescrever baseline_eval_path com relatório wrapper."
    )
    payload: dict[str, object] = {
        "blocked_reason": "agent_behavior_report_would_overwrite_baseline",
        "next_action": next_action,
        "required_inputs": ["corpus_path", "report_path", "baseline_eval_path"],
        "human_decision_required": False,
    }
    return _attach_error_context(
        payload,
        phase="eval-agent-behavior-corpus",
        message=message,
        affected_artifact="baseline_eval_path",
        suggested_fix="Use outro --report e promova somente o eval de suite como baseline.",
        retry_scope="write_eval_report_then_promote_baseline",
        root_cause="agent_behavior_report_would_overwrite_baseline",
    )


def _curator_batch_validation_feedback(message: str) -> dict[str, object]:
    specs = {
        "curator_prompt_eval_required": _ValidationFeedbackSpec(
            blocked_reason="curator_prompt_eval_required",
            next_action="Gerar o manifest com collect-curator-outputs, rodar eval-curator-batch --report <curator-prompt-eval.json> e repetir apply-curator-batch --prompt-eval <curator-prompt-eval.json>.",
            required_inputs=["prompt_eval"],
            human_decision_required=False,
            affected_artifact="prompt_eval",
            suggested_fix="Validar outputs do curador antes de aplicar o batch.",
            retry_scope="collect_eval_then_apply_curator_batch",
            root_cause="curator_prompt_eval_required",
            agent_notice=True,
        ),
        "curator_prompt_eval_skip_reason_required": _ValidationFeedbackSpec(
            blocked_reason="curator_prompt_eval_skip_not_allowed",
            next_action="Remova --skip-prompt-eval, gere o relatório com eval-curator-batch e repita apply-curator-batch com --prompt-eval.",
            required_inputs=["prompt_eval"],
            human_decision_required=False,
            affected_artifact="prompt_eval",
            suggested_fix="Usar prompt eval real para apply de curadoria.",
            retry_scope="restore_prompt_eval_gate",
            root_cause="curator_prompt_eval_skip_not_allowed",
            agent_notice=True,
        ),
        "curator_prompt_eval_options_conflict": _ValidationFeedbackSpec(
            blocked_reason="curator_prompt_eval_options_conflict",
            next_action="Escolha apenas --prompt-eval com relatório válido; não combine com --skip-prompt-eval.",
            required_inputs=["prompt_eval"],
            human_decision_required=False,
            affected_artifact="prompt_eval",
            suggested_fix="Remover a opção conflitante e repetir apply-curator-batch.",
            retry_scope="restore_prompt_eval_gate",
            root_cause="curator_prompt_eval_options_conflict",
            agent_notice=True,
        ),
    }
    code = _validation_message_code(message, registered_codes=set(specs))
    if code:
        return _feedback_payload_from_spec(specs[code], phase="apply-curator-batch", message=message)
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason="apply_curator_batch_validation_error",
            next_action="Gerar o manifest com collect-curator-outputs, rodar eval-curator-batch e repetir apply-curator-batch com --prompt-eval; não edite artefatos manualmente.",
            required_inputs=["official_curator_batch_artifacts"],
            human_decision_required=False,
            affected_artifact="curator_batch_artifacts",
            suggested_fix="Recriar manifest/eval pelo fluxo oficial antes de aplicar.",
            retry_scope="collect_eval_then_apply_curator_batch",
            root_cause="apply_curator_batch_validation_error",
            agent_notice=True,
        ),
        phase="apply-curator-batch",
        message=message,
    )


def _vocabulary_recover_validation_feedback(message: str) -> dict[str, object]:
    code = _validation_message_code(
        message,
        registered_codes={"vocabulary_recovery_plan_required"},
        exact_messages={"--plan is required with --apply": "vocabulary_recovery_plan_required"},
    )
    if not code:
        return {}
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason="vocabulary_recovery_plan_required",
            next_action="Rodar vocabulary-recover --dry-run --plan-output <plan.json> para gerar o plano, revisar blockers e repetir com --apply --plan <plan.json> --receipt <receipt.json>.",
            required_inputs=["plan"],
            human_decision_required=False,
            affected_artifact="vocabulary_recovery_plan",
            suggested_fix="Gerar e revisar um plano de recovery antes do apply.",
            retry_scope="vocabulary_recover_dry_run_then_apply",
            root_cause="vocabulary_recovery_plan_required",
        ),
        phase="vocabulary-recover",
        message=message,
    )


def _triage_validation_feedback(message: str) -> dict[str, object]:
    triager_specs = {
        "triager_eval_required": _ValidationFeedbackSpec(
            blocked_reason="triager_eval_required",
            next_action="Rodar eval-triager-output para o output bruto do med-chat-triager, gravar triager-prompt-eval.v1 e repetir triage com --triager-eval; não editar note_plan manualmente.",
            required_inputs=["raw_file", "note_plan", "triager_eval"],
            missing_inputs=["triager_eval"],
            affected_artifact="triager_eval",
            suggested_fix="Gerar triager-prompt-eval.v1 antes de mutar YAML/status do raw chat.",
            retry_scope="triager_eval_then_triage",
            root_cause="triager_eval_required",
        ),
        "triager_eval_failed": _ValidationFeedbackSpec(
            blocked_reason="triager_eval_failed",
            next_action=TRIAGER_EVAL_RETRY_NEXT_ACTION,
            required_inputs=["raw_file", "note_plan", "triager_eval"],
            affected_artifact="triager_eval",
            suggested_fix="Regenerar o output do triager pela rota oficial de avaliação, sem patch manual.",
            retry_scope="triager_eval_then_triage",
            root_cause="triager_eval_failed",
        ),
        "triager_eval_stale": _ValidationFeedbackSpec(
            blocked_reason="triager_eval_stale",
            next_action="Regenerar eval-triager-output para o mesmo raw_file e o mesmo note_plan atual; não reaproveitar eval de um JSON editado.",
            required_inputs=["raw_file", "note_plan", "triager_eval"],
            affected_artifact="triager_eval",
            suggested_fix="Regenerar triager-prompt-eval.v1 amarrado ao note_plan atual.",
            retry_scope="triager_eval_then_triage",
            root_cause="triager_eval_stale",
        ),
        "triager_eval_invalid": _ValidationFeedbackSpec(
            blocked_reason="triager_eval_invalid",
            next_action=(
                "Regenerar eval-triager-output pela rota oficial com subagent_run_receipt assinado; "
                "não editar triager-prompt-eval.v1 manualmente."
            ),
            required_inputs=["raw_file", "note_plan", "triager_eval"],
            affected_artifact="triager_eval",
            suggested_fix="Gerar triager-prompt-eval.v1 com cadeia assinada de subagente.",
            retry_scope="triager_eval_then_triage",
            root_cause="triager_eval_invalid",
        ),
        "triager_eval_missing_subagent_run_receipt": _ValidationFeedbackSpec(
            blocked_reason="triager_eval_invalid",
            next_action=(
                "Regenerar eval-triager-output com --subagent-run-receipt e "
                "--require-subagent-run-receipt antes de aplicar triage."
            ),
            required_inputs=["raw_file", "note_plan", "triager_eval"],
            missing_inputs=["subagent_run_receipt"],
            affected_artifact="triager_eval",
            suggested_fix="Amarrar o eval ao receipt assinado do subagente.",
            retry_scope="triager_eval_then_triage",
            root_cause="triager_eval_invalid",
        ),
    }
    code = _validation_message_code(message, registered_codes=set(triager_specs))
    if code:
        return _feedback_payload_from_spec(triager_specs[code], phase="triage", message=message)
    note_plan_code = _triage_note_plan_validation_code(message)
    missing_inputs = _triage_note_plan_missing_inputs(note_plan_code)
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason="note_plan_invalid",
            next_action=_triage_validation_next_action(message),
            required_inputs=["raw_file", "note_plan"],
            missing_inputs=missing_inputs,
            affected_artifact="note_plan",
            suggested_fix="Corrigir o JSON do note_plan conforme triage-note-plan.v2.",
            retry_scope="triage_note_plan_only",
            root_cause="note_plan_invalid",
        ),
        phase="triage",
        message=message,
    )


def _triage_validation_next_action(message: str) -> str:
    code = _triage_note_plan_validation_code(message)
    if code == "note_plan_meaning_claim_missing":
        return "Corrigir o note_plan: cada item planned_meaning precisa de meaning_claim conforme triage-policy.md; depois repetir somente triage --note-plan."
    if code == "note_plan_target_item_id_missing":
        return "Corrigir o note_plan: cada item attach_to_planned_meaning precisa de target_item_id apontando para um planned_meaning irmão; depois repetir somente triage --note-plan."
    if code == "note_plan_reason_code_missing":
        return "Corrigir o note_plan: items attach_to_planned_meaning, not_a_note e needs_context exigem reason_code do conjunto fechado em triage-policy.md; depois repetir somente triage --note-plan."
    if code == "note_plan_duplicate_meaning":
        return "Revisar o note_plan e consolidar staged_title de planned_meaning duplicados por acento/caixa; depois repetir somente triage --note-plan."
    if code == "note_plan_raw_file_mismatch":
        return "Corrigir o raw_file dentro do note_plan para apontar exatamente para o raw chat processado e repetir somente triage --note-plan."
    return (
        "Corrigir o JSON do note_plan conforme triage-note-plan.v2 e repetir somente triage --note-plan; "
        "não avançar para architect, stage-note ou publish-batch enquanto o plano estiver inválido."
    )


def _triage_note_plan_validation_code(message: str) -> str:
    """Read producer-owned note_plan error codes without classifying prose."""

    return _validation_message_code(
        message,
        registered_codes={
            "note_plan_meaning_claim_missing",
            "note_plan_target_item_id_missing",
            "note_plan_reason_code_missing",
            "note_plan_duplicate_meaning",
            "note_plan_raw_file_mismatch",
        },
    )


def _triage_note_plan_missing_inputs(code: str) -> list[str]:
    """Expose the missing field that makes the note_plan unrecoverable as-is."""

    if code == "note_plan_meaning_claim_missing":
        return ["meaning_claim"]
    if code == "note_plan_target_item_id_missing":
        return ["target_item_id"]
    if code == "note_plan_reason_code_missing":
        return ["reason_code"]
    if code == "note_plan_raw_file_mismatch":
        return ["raw_file"]
    return []


def _stage_note_validation_next_action(message: str) -> str:
    actions = {
        "requires_llm_rewrite": "Chamar med-knowledge-architect com rewrite_prompt/error_context para reescrever a nota temporária, validar novamente e repetir stage-note.",
        "batch_state_mismatch": "Regenerar coverage a partir do note_plan atual e repetir stage-note --coverage; não reaproveitar manifest/coverage de outro lote.",
        "provenance_gap": "Completar coverage.sources para todos os raw_files do merge canônico e repetir stage-note --coverage; não avançar para publish-batch enquanto a proveniência estiver incompleta.",
        "coverage_invalid": "Corrigir ou regenerar o coverage_path a partir do note_plan, repetir stage-note --coverage <coverage.json> e só então seguir para publish-batch --dry-run.",
        "taxonomy_resolution_required": "Revisar taxonomy/title ou permitir novo leaf de taxonomia quando seguro, depois repetir stage-note antes do publish-batch.",
        "manifest_invalid": "Regenerar manifest via stage-note antes de seguir para publish-batch.",
    }
    code = _validation_message_code(message, registered_codes=set(actions))
    return actions.get(
        code,
        "Rodar validate-note --content <temp.md> --title <título> --raw-file <raw.md> --json, corrigir a nota temporária e repetir stage-note antes do publish-batch --dry-run.",
    )


def _stage_note_validation_feedback(message: str) -> dict[str, object]:
    code = _validation_message_code(
        message,
        registered_codes={
            "requires_llm_rewrite",
            "batch_state_mismatch",
            "provenance_gap",
            "coverage_invalid",
            "taxonomy_resolution_required",
            "manifest_invalid",
        },
    )
    artifact_by_code = {
        "requires_llm_rewrite": "content_path",
        "batch_state_mismatch": "batch_state",
        "provenance_gap": "coverage_path",
        "coverage_invalid": "coverage_path",
        "taxonomy_resolution_required": "taxonomy",
        "manifest_invalid": "manifest",
    }
    retry_by_code = {
        "requires_llm_rewrite": "style_rewrite_then_stage_note",
        "batch_state_mismatch": "regenerate_artifact_chain",
        "provenance_gap": "coverage_and_note_provenance",
        "coverage_invalid": "coverage_then_stage",
        "taxonomy_resolution_required": "taxonomy_then_stage",
        "manifest_invalid": "stage_note_manifest_only",
    }
    affected_artifact = artifact_by_code.get(code, "content_path")
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason=code,
            next_action=_stage_note_validation_next_action(message),
            required_inputs=STAGE_NOTE_REQUIRED_INPUTS,
            affected_artifact=affected_artifact,
            suggested_fix=_stage_note_validation_next_action(message),
            retry_scope=retry_by_code.get(code, "stage_note_only"),
            root_cause=code or "ValidationError",
            max_attempts=2 if affected_artifact == "content_path" else None,
        ),
        phase="stage-note",
        message=message,
    )


def _style_rewrite_validation_feedback(message: str, *, phase: str) -> dict[str, object]:
    specs = {
        "subagent_output_contract.invalid": _ValidationFeedbackSpec(
            blocked_reason="subagent_output_contract.invalid",
            next_action="Regerar o plano de style-rewrite pela rota oficial de /mednotes:fix-wiki; o work_item precisa declarar subagent_output_contract e a atestação fica parent-only.",
            required_inputs=["plan", "work_id"],
            human_decision_required=False,
            affected_artifact="style_rewrite_plan",
            suggested_fix="Regerar o plano de style-rewrite pela rota oficial de /mednotes:fix-wiki.",
            retry_scope="regenerate_style_rewrite_plan",
            root_cause="subagent_output_contract.invalid",
        ),
        "style_rewrite_plan_contract_invalid": _ValidationFeedbackSpec(
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regerar o plano de style-rewrite pela rota oficial de /mednotes:fix-wiki antes de relançar collect/finalize/apply.",
            required_inputs=["plan", "work_id"],
            human_decision_required=False,
            affected_artifact="style_rewrite_plan",
            suggested_fix="Regerar o plano de style-rewrite pela rota oficial.",
            retry_scope="regenerate_style_rewrite_plan",
            root_cause="style_rewrite_plan_contract_invalid",
        ),
        "style_rewrite_manifest_invalid": _ValidationFeedbackSpec(
            blocked_reason="style_rewrite_manifest_invalid",
            next_action="Regenerar o manifest com collect-style-rewrite-outputs usando o plano atual.",
            human_decision_required=False,
            affected_artifact="style_rewrite_manifest",
            suggested_fix="Regenerar o manifest com collect-style-rewrite-outputs usando o plano atual.",
            retry_scope="collect_style_rewrite_outputs_then_apply",
            root_cause="style_rewrite_manifest_invalid",
        ),
        "style_rewrite_output_attestation_invalid": _ValidationFeedbackSpec(
            blocked_reason="style_rewrite_output_attestation_invalid",
            next_action="Refinalizar o output com finalize-style-rewrite-output e coletar novo manifest.",
            human_decision_required=False,
            affected_artifact="style_rewrite_output_attestation",
            suggested_fix="Refinalizar o output com finalize-style-rewrite-output.",
            retry_scope="finalize_style_rewrite_output_then_collect",
            root_cause="style_rewrite_output_attestation_invalid",
        ),
        "style_rewrite_output_receipt_invalid": _ValidationFeedbackSpec(
            blocked_reason="style_rewrite_output_receipt_invalid",
            next_action="Descartar receipt legado e usar finalize-style-rewrite-output para gerar atestação Workbench.",
            human_decision_required=False,
            affected_artifact="style_rewrite_output_receipt",
            suggested_fix="Gerar atestação Workbench com finalize-style-rewrite-output.",
            retry_scope="finalize_style_rewrite_output_then_collect",
            root_cause="style_rewrite_output_receipt_invalid",
        ),
    }
    code = _validation_message_code(message, registered_codes=set(specs))
    if code:
        return _feedback_payload_from_spec(specs[code], phase=phase, message=message)
    return _feedback_payload_from_spec(
        _ValidationFeedbackSpec(
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Corrigir o artefato style-rewrite apontado pelo erro e repetir a etapa oficial.",
            human_decision_required=False,
            affected_artifact="style_rewrite",
            suggested_fix="Corrigir o artefato style-rewrite apontado pelo erro.",
            retry_scope="style_rewrite_artifact_only",
            root_cause="style_rewrite_plan_contract_invalid",
        ),
        phase=phase,
        message=message,
    )


def _taxonomy_validation_feedback(message: str) -> dict[str, object]:
    next_action = "Revisar a taxonomia sugerida, escolher categoria existente ou autorizar nova leaf quando seguro."
    payload: dict[str, object] = {
        "blocked_reason": "taxonomy_resolution_required",
        "next_action": next_action,
        "required_inputs": ["taxonomy", "title", "wiki_dir"],
    }
    return _attach_error_context(
        payload,
        phase="taxonomy-resolve",
        message=message,
        affected_artifact="taxonomy",
        suggested_fix="Resolver a taxonomia antes de retomar o workflow.",
        retry_scope="taxonomy_only",
        root_cause="taxonomy_resolution_required",
    )


def _note_repair_route(report: dict[str, object]) -> str:
    errors = _json_field(report, "errors") if isinstance(_json_field(report, "errors"), list) else []
    fixes = _json_field(report, "fixes_applied") if isinstance(_json_field(report, "fixes_applied"), list) else []
    if _json_field(report, "requires_llm_rewrite"):
        return "llm_rewrite"
    if errors:
        return "deterministic_fix_note"
    if fixes:
        return "deterministic_fix_applied"
    return "none"


def _validate_note_next_action(report: dict[str, object]) -> str:
    route = _note_repair_route(report)
    if route == "llm_rewrite":
        return (
            "Escalar para rewrite clínico: passar rewrite_prompt e error_context ao architect; "
            "não repetir fix-note indefinidamente."
        )
    if route == "deterministic_fix_note":
        return "Rodar fix-note para correções determinísticas de forma/YAML/footer e validar novamente."
    return ""


def _fix_note_next_action(report: dict[str, object]) -> str:
    route = _note_repair_route(report)
    if route == "llm_rewrite":
        return (
            "Escalar para rewrite clínico com rewrite_prompt; não editar scripts, prompts ou runbooks "
            "como workaround."
        )
    if route == "deterministic_fix_applied":
        return "Aplicar o output normalizado e seguir para stage-note/dry-run."
    if route == "deterministic_fix_note":
        return "Revisar o conteúdo e rodar fix-note novamente somente se o input mudou."
    return "Aplicar o output normalizado ou seguir para stage-note/dry-run."


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _emit_agent_preamble_if_enabled(payload: object) -> None:
    if os.environ.get("MEDNOTES_AGENT_PREAMBLE", "").strip().lower() != "stderr":
        return
    if not isinstance(payload, dict):
        return
    try:
        lines = agent_preamble_lines(payload)
        if lines:
            print("\n".join(lines), file=sys.stderr)
    except Exception:
        # Optional agent-facing output must not affect stdout JSON or exit semantics.
        return


def _issue_summary(issues: list[object], *, sample_size: int = 3) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for raw_issue in issues:
        if not isinstance(raw_issue, dict):
            continue
        issue_fields = _IssueSummaryFields.model_validate(raw_issue)
        code = issue_fields.code or "unknown"
        group = grouped.setdefault(code, {"code": code, "count": 0, "examples": []})
        group_fields = _IssueSummaryFields.model_validate(group)
        group["count"] = group_fields.count + 1
        examples = group["examples"]
        if isinstance(examples, list) and len(examples) < sample_size:
            examples.append(_issue_example(raw_issue))
    return sorted(
        grouped.values(),
        key=lambda item: (
            -_IssueSummaryFields.model_validate(item).count,
            _IssueSummaryFields.model_validate(item).code,
        ),
    )


def _issue_example(issue: dict[str, object]) -> dict[str, object]:
    keys = ("file", "target", "line", "raw", "files", "message")
    return {key: issue[key] for key in keys if key in issue}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Medical Notes Workbench deterministic chat-processing operations.")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    env_preflight = sub.add_parser("environment-preflight", help="Check Python/uv/path setup without resolving the Wiki.")
    env_preflight.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    set_paths = sub.add_parser("set-paths", help="Validate and persist app-owned Wiki/Raw paths.")
    set_paths.add_argument("--config", help="Optional config.toml. Defaults to the app-owned state config.")
    set_paths.add_argument("--wiki-dir", required=True, help="Existing Wiki_Medicina directory.")
    set_paths.add_argument("--raw-dir", required=True, help="Existing Chats_Raw directory.")
    set_paths.add_argument(
        "--agent-repair",
        action="store_true",
        help="Block instead of overwriting when valid configured paths conflict with the proposed repair.",
    )
    set_paths.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    repair_config = sub.add_parser("repair-config-template", help="Rewrite config.toml from the UTF-8 template while preserving [paths].")
    repair_config.add_argument("--config", help="Optional config.toml. Defaults to the app-owned state config.")
    repair_config.add_argument("--template", help="Optional UTF-8 config.example.toml template.")
    repair_config.add_argument("--dry-run", action="store_true", help="Report the planned rewrite without mutating config.toml.")
    repair_config.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    markdown_query_status = sub.add_parser("markdown-query-status", help="Check the persistent Markdown query index.")
    _add_common(markdown_query_status, suppress_defaults=True)
    markdown_query_status.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    markdown_query_rebuild = sub.add_parser("markdown-query-rebuild", help="Prepare the persistent Markdown query index.")
    _add_common(markdown_query_rebuild, suppress_defaults=True)
    markdown_query_rebuild.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    markdown_query_probe = sub.add_parser("markdown-query-probe", help="Probe the persistent Markdown query index.")
    _add_common(markdown_query_probe, suppress_defaults=True)
    markdown_query_probe.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    pending = sub.add_parser("list-pending", help="List raw chats with no status or status=pendente.")
    _add_common(pending, suppress_defaults=True)
    pending.add_argument("--summary", action="store_true", help="Emit counts plus a small sample instead of the full list.")
    pending.add_argument("--limit", type=int, default=0, help="Limit returned rows, or sample size with --summary.")
    pending.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    triados = sub.add_parser("list-triados", help="List raw chats with status=triado and tipo=medicina.")
    _add_common(triados, suppress_defaults=True)
    triados.add_argument("--summary", action="store_true", help="Emit counts plus a small sample instead of the full list.")
    triados.add_argument("--limit", type=int, default=0, help="Limit returned rows, or sample size with --summary.")
    triados.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    plan_agents = sub.add_parser("plan-subagents", help="Build a safe subagent work plan for process-chats/fix-wiki/link.")
    _add_common(plan_agents, suppress_defaults=True)
    plan_agents.add_argument(
        "--phase",
        choices=("triage", "architect", "style-rewrite", "note-merge", "vocabulary-curation", "atomicity-split"),
        required=True,
    )
    plan_agents.add_argument(
        "--max-concurrency",
        type=int,
        default=0,
        help="Override the conservative default fan-out; omit for process-chats default 5.",
    )
    plan_agents.add_argument("--temp-root", help="Base temporary directory for isolated triage/architect/workflow artifacts.")
    plan_agents.add_argument("--limit", type=int, help="Limit returned work items for the next explicit batch.")
    plan_agents.add_argument("--fix-wiki-plan", help="Saved fix-wiki-plan.json required for atomicity-split.")
    plan_agents.add_argument("--output", help="Write the full generated plan JSON to this path and print a compact receipt.")
    plan_agents.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    taxonomy_canonical = sub.add_parser("taxonomy-canonical", help="Print the canonical Wiki_Medicina taxonomy.")
    _add_common(taxonomy_canonical, suppress_defaults=True)

    taxonomy = sub.add_parser("taxonomy-tree", help="List existing Wiki_Medicina taxonomy folders.")
    _add_common(taxonomy, suppress_defaults=True)
    taxonomy.add_argument("--max-depth", type=int, default=0, help="Limit folder depth; 0 means all depths.")

    taxonomy_audit_parser = sub.add_parser("taxonomy-audit", help="Dry-run audit of the vault against the canonical taxonomy.")
    _add_common(taxonomy_audit_parser, suppress_defaults=True)
    taxonomy_audit_parser.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    taxonomy_status_parser = sub.add_parser("taxonomy-status", help="Summarize taxonomy health for humans and agents.")
    _add_common(taxonomy_status_parser, suppress_defaults=True)
    taxonomy_status_parser.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    taxonomy_status_parser.add_argument("--report-output", help="Write human Markdown taxonomy status report to this path.")

    taxonomy_migrate = sub.add_parser("taxonomy-migrate", help="Plan, apply, or roll back conservative taxonomy directory moves.")
    _add_common(taxonomy_migrate, suppress_defaults=True)
    migrate_mode = taxonomy_migrate.add_mutually_exclusive_group()
    migrate_mode.add_argument("--dry-run", action="store_true", help="Generate a migration plan without moving files. Default mode.")
    migrate_mode.add_argument("--apply", action="store_true", help="Apply a previously generated migration plan.")
    migrate_mode.add_argument("--rollback", action="store_true", help="Rollback a migration receipt.")
    taxonomy_migrate.add_argument("--plan", help="Plan JSON path. Required with --apply.")
    taxonomy_migrate.add_argument("--plan-output", help="Write generated dry-run plan to this path.")
    taxonomy_migrate.add_argument("--receipt", help="Receipt path for --apply output or --rollback input.")

    taxonomy_plan = sub.add_parser("taxonomy-plan", help="Write a taxonomy migration plan without moving files.")
    _add_common(taxonomy_plan, suppress_defaults=True)
    taxonomy_plan.add_argument("--plan-output", help="Write generated dry-run plan to this path.")
    taxonomy_plan.add_argument("--report-output", help="Write human Markdown taxonomy migration report to this path.")

    taxonomy_apply = sub.add_parser("taxonomy-apply", help="Apply a reviewed taxonomy migration plan.")
    _add_common(taxonomy_apply, suppress_defaults=True)
    taxonomy_apply.add_argument("--plan", required=True, help="Plan JSON path.")
    taxonomy_apply.add_argument("--receipt", help="Receipt path for apply output.")

    taxonomy_rollback = sub.add_parser("taxonomy-rollback", help="Rollback a taxonomy migration receipt.")
    _add_common(taxonomy_rollback, suppress_defaults=True)
    taxonomy_rollback.add_argument("--receipt", required=True, help="Receipt path to roll back.")

    taxonomy_resolve = sub.add_parser("taxonomy-resolve", help="Validate and canonicalize one taxonomy against the existing wiki tree.")
    _add_common(taxonomy_resolve, suppress_defaults=True)
    taxonomy_resolve.add_argument("--taxonomy", required=True)
    taxonomy_resolve.add_argument("--title", help="Optional note title; rejects taxonomy/title duplication when provided.")
    _add_taxonomy_creation_mode(taxonomy_resolve)

    triage = sub.add_parser("triage", help="Mark one raw chat as triaged.")
    _add_common(triage, suppress_defaults=True)
    triage.add_argument("--raw-file", required=True)
    triage.add_argument("--tipo", default="medicina")
    triage.add_argument("--titulo", required=True)
    triage.add_argument("--fonte-id", default="")
    triage.add_argument("--note-plan", help="JSON file produced by med-chat-triager with the exhaustive note plan.")
    triage.add_argument(
        "--triager-eval",
        help="Passing triager-prompt-eval.v1 report for this exact raw_file and note_plan.",
    )
    triage.add_argument("--dry-run", action="store_true")
    triage.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    eval_triager = sub.add_parser(
        "eval-triager-output",
        help="Evaluate med-chat-triager output before triage/discard.",
    )
    _add_common(eval_triager, suppress_defaults=True)
    eval_triager.add_argument("--raw-file", required=True)
    eval_triager.add_argument("--output", required=True, help="JSON output produced by med-chat-triager.")
    eval_triager.add_argument(
        "--expectations",
        help="Optional triager-prompt-expectations.v1 golden corpus kept outside the agent output.",
    )
    eval_triager.add_argument("--report", help="Write triager-prompt-eval.v1 JSON to this path.")
    eval_triager.add_argument("--baseline-eval", help="Optional previous triager-prompt-eval.v1 JSON for regression checks.")
    eval_triager.add_argument(
        "--require-agent-metrics",
        action="store_true",
        help="Treat missing/malformed agent_metrics as blocking. Intended for lab prompt-quality runs, not default UX.",
    )
    eval_triager.add_argument(
        "--subagent-run-receipt",
        help=(
            "Runner-issued signed subagent-run-receipt.v1 for this output. "
            "Required for mutating process-chats triage."
        ),
    )
    eval_triager.add_argument(
        "--require-subagent-run-receipt",
        action="store_true",
        help="Treat missing/stale subagent run receipt as blocking.",
    )
    eval_triager.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    discard = sub.add_parser("discard", help="Mark one raw chat as discarded.")
    _add_common(discard, suppress_defaults=True)
    discard.add_argument("--raw-file", required=True)
    discard.add_argument("--reason", required=True)
    discard.add_argument("--dry-run", action="store_true")
    discard.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    stage = sub.add_parser("stage-note", help="Append a generated note to a manifest.")
    _add_common(stage, suppress_defaults=True)
    stage.add_argument("--manifest", required=True)
    stage.add_argument("--raw-file", required=True)
    stage.add_argument("--taxonomy", required=True)
    stage.add_argument("--title", required=True)
    stage.add_argument("--content", required=True)
    stage.add_argument("--coverage", help="Exhaustive raw coverage inventory JSON for this raw chat.")
    stage.add_argument("--dry-run", action="store_true")
    _add_taxonomy_creation_mode(stage)

    publish = sub.add_parser("publish-batch", help="Publish all notes from a manifest, then mark raw files processed.")
    _add_common(publish, suppress_defaults=True)
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--collision", choices=("abort", "suffix"), default="abort")
    publish.add_argument(
        "--skip-coverage",
        action="store_false",
        dest="require_coverage",
        default=True,
        help="Developer/emergency override: publish without an exhaustive raw coverage inventory.",
    )
    publish.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    _add_taxonomy_creation_mode(publish)

    publish_status = sub.add_parser("publish-status", help="Diagnose whether a manifest is ready for publish without mutating.")
    _add_common(publish_status, suppress_defaults=True)
    publish_status.add_argument("--manifest", required=True)
    publish_status.add_argument("--collision", choices=("abort", "suffix"), default="abort")
    publish_status.add_argument(
        "--skip-coverage",
        action="store_false",
        dest="require_coverage",
        default=True,
        help="Developer/emergency override: diagnose publish without exhaustive raw coverage.",
    )
    publish_status.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    _add_taxonomy_creation_mode(publish_status)

    linker = sub.add_parser("run-linker", help="Diagnose or apply the configured Wiki linker.")
    _add_common(linker, suppress_defaults=True)
    linker_mode = linker.add_mutually_exclusive_group()
    linker_mode.add_argument("--diagnose", action="store_true", help="Build and save a link diagnosis without mutating notes.")
    linker_mode.add_argument("--apply", action="store_true", help="Apply a previously saved link diagnosis.")
    linker.add_argument("--diagnosis", help="Diagnosis artifact path. Output with --diagnose; required input with --apply.")
    linker.add_argument("--receipt", help="Receipt path for --apply output.")
    linker.add_argument("--trigger-context", help="Trigger context JSON path. Accepted only with --diagnose.")
    linker.add_argument(
        "--force-diagnose",
        action="store_true",
        help="Developer override: recompute diagnosis even if retry governance detects unchanged blocker state.",
    )
    linker.add_argument(
        "--llm-disambiguation",
        choices=("auto", "off", "required"),
        default="auto",
        help="Desambiguate requires_context body aliases during --diagnose. Apply never calls LLM.",
    )
    linker.add_argument("--llm-model", default=None, help="Gemini model for contextual alias disambiguation.")
    linker.add_argument("--llm-timeout", type=int, default=60, help="Timeout in seconds for Gemini contextual disambiguation.")
    linker.add_argument(
        "--no-related-notes",
        action="store_true",
        help="Skip related_notes_sync; use for body WikiLinks without rewriting Notas Relacionadas.",
    )
    linker.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    linker.add_argument("--full", action="store_true", help="Include per-file linker plans in the JSON output.")

    semantic_ingestion = sub.add_parser(
        "apply-semantic-ingestion",
        help="Apply one med-link-graph-curator semantic ingestion payload to the vocabulary DB.",
    )
    _add_common(semantic_ingestion, suppress_defaults=True)
    semantic_ingestion.add_argument("--input", required=True, help="note-semantic-ingestion.v1 JSON produced by med-link-graph-curator.")
    semantic_ingestion.add_argument("--receipt", help="Write note-semantic-ingestion-apply-receipt.v1 to this path.")
    semantic_ingestion.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    status = sub.add_parser("status", help="Print deterministic Workbench status without mutating or auditing user root.")
    _add_common(status, suppress_defaults=True)
    status.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    vocabulary_status_parser = sub.add_parser("vocabulary-status", help="Inspect vocabulary DB schema and queue health.")
    _add_common(vocabulary_status_parser, suppress_defaults=True)
    vocabulary_status_parser.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    vocabulary_recover = sub.add_parser("vocabulary-recover", help="Plan or apply official vocabulary DB recovery.")
    _add_common(vocabulary_recover, suppress_defaults=True)
    vocabulary_recover.add_argument("--mode", choices=("reconcile-queue", "rebuild-db", "catalog-assisted"), required=True)
    recover_mode = vocabulary_recover.add_mutually_exclusive_group(required=True)
    recover_mode.add_argument("--dry-run", action="store_true")
    recover_mode.add_argument("--apply", action="store_true")
    vocabulary_recover.add_argument("--plan", help="Plan JSON input for --apply; backward-compatible dry-run output path.")
    vocabulary_recover.add_argument("--plan-output", help="Write dry-run plan JSON to this path and print a compact receipt.")
    vocabulary_recover.add_argument("--receipt", help="Receipt path for --apply output.")
    vocabulary_recover.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    curator_collect = sub.add_parser(
        "collect-curator-outputs",
        help="Build a curator output manifest from output_path values in a batch plan.",
    )
    _add_common(curator_collect, suppress_defaults=True)
    curator_collect.add_argument("--plan", required=True, help="vocabulary-curator-batch-plan.v1 JSON.")
    curator_collect.add_argument("--manifest", required=True, help="Write vocabulary-curator-batch-output-manifest.v1 JSON here.")
    curator_collect.add_argument(
        "--include-missing",
        action="store_true",
        help="Include planned output paths even when files are not present; default records missing paths but excludes them.",
    )
    curator_collect.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    curator_batch = sub.add_parser(
        "apply-curator-batch",
        help="Apply a manifest of med-link-graph-curator semantic ingestion outputs.",
    )
    _add_common(curator_batch, suppress_defaults=True)
    curator_batch.add_argument("--plan", required=True, help="vocabulary-curator-batch-plan.v1 JSON.")
    curator_batch.add_argument("--outputs", required=True, help="vocabulary-curator-batch-output-manifest.v1 JSON.")
    curator_batch.add_argument("--receipt", help="Write vocabulary-curator-batch-receipt.v1 to this path.")
    curator_batch.add_argument("--validate-only", action="store_true", help="Validate all curator outputs before opening a DB write path.")
    curator_prompt_eval = curator_batch.add_mutually_exclusive_group()
    curator_prompt_eval.add_argument("--prompt-eval", help="Required prompt-quality eval report for DB-writing apply mode.")
    curator_prompt_eval.add_argument(
        "--skip-prompt-eval",
        action="store_true",
        help="Deprecated escape hatch; apply mode ignores outputs and requires --prompt-eval.",
    )
    curator_batch.add_argument("--skip-prompt-eval-reason", help="Required explicit reason when using --skip-prompt-eval.")
    curator_batch.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    curator_eval = sub.add_parser(
        "eval-curator-batch",
        help="Evaluate med-link-graph-curator outputs against prompt-quality and efficiency rubrics.",
    )
    _add_common(curator_eval, suppress_defaults=True)
    curator_eval.add_argument("--plan", required=True, help="vocabulary-curator-batch-plan.v1 JSON.")
    curator_eval.add_argument("--outputs", required=True, help="vocabulary-curator-batch-output-manifest.v1 JSON.")
    curator_eval.add_argument("--expectations", help="curator-prompt-golden-expectations.v1 JSON loaded only for eval.")
    curator_eval.add_argument("--baseline-eval", help="Previous curator-prompt-eval.v1 JSON for before/after deltas.")
    curator_eval.add_argument("--report", help="Write curator-prompt-eval.v1 JSON to this path.")
    curator_eval.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    curator_expectations = sub.add_parser(
        "init-curator-expectations",
        help="Write a redacted curator-prompt-golden-expectations.v1 template from a curator batch plan.",
    )
    _add_common(curator_expectations, suppress_defaults=True)
    curator_expectations.add_argument("--plan", required=True, help="vocabulary-curator-batch-plan.v1 JSON.")
    curator_expectations.add_argument("--output", required=True, help="Write curator-prompt-golden-expectations.v1 template here.")
    curator_expectations.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    curator_baseline = sub.add_parser(
        "promote-curator-baseline",
        help="Promote a passing curator-prompt-eval.v1 report to a reusable baseline.",
    )
    _add_common(curator_baseline, suppress_defaults=True)
    curator_baseline.add_argument("--eval", required=True, help="Passing curator-prompt-eval.v1 JSON.")
    curator_baseline.add_argument("--output", required=True, help="Write the baseline JSON here.")
    curator_baseline.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    agent_behavior = sub.add_parser(
        "eval-agent-behavior-corpus",
        help="Evaluate versioned agent behavior corpus fixtures before accepting prompt changes.",
    )
    _add_common(agent_behavior, suppress_defaults=True)
    agent_behavior.add_argument("--corpus", required=True, help="agent-behavior-corpus.v1 JSON file or directory containing corpus.json.")
    agent_behavior.add_argument("--report", help="Write agent-behavior-corpus-report.v1 JSON to this path.")
    agent_behavior.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    agent_session = sub.add_parser(
        "validate-agent-session",
        help="Validate captured agent transcript/tool-call arguments against the Workbench contract.",
    )
    agent_session.add_argument("--transcript", required=True, help="JSON/NDJSON transcript or tool-call capture to validate.")
    agent_session.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    agent_run_report = sub.add_parser(
        "validate-agent-run-report",
        help="Compare an agent final report against the official workflow payload and transcript.",
    )
    agent_run_report.add_argument("--workflow-payload", required=True, help="Official workflow JSON payload or compact report.")
    agent_run_report.add_argument("--transcript", help="Optional JSON/NDJSON transcript or tool-call capture.")
    agent_run_report.add_argument("--final-report", help="Optional final report Markdown/text when not embedded in transcript.")
    agent_run_report.add_argument("--runtime-log", action="append", default=[], help="Optional stdout/stderr/runtime log captured outside the transcript.")
    agent_run_report.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    transcript_audit = sub.add_parser(
        "audit-agent-transcript",
        help="Audit AGY/Gemini transcript behavior against Workbench workflow contracts.",
    )
    transcript_audit.add_argument(
        "--workflow",
        choices=("process-chats", "fix-wiki", "link", "unknown"),
        default="unknown",
        help="Workflow being audited.",
    )
    transcript_audit.add_argument("--transcript", required=True, help="JSON/JSONL/text transcript or tool-call capture.")
    transcript_audit.add_argument("--workflow-payload", help="Optional official workflow JSON payload.")
    transcript_audit.add_argument("--final-report", help="Optional final report Markdown/text.")
    transcript_audit.add_argument("--runtime-log", action="append", default=[], help="Optional runtime log captured outside transcript.")
    transcript_audit.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    happy_path_round = sub.add_parser(
        "summarize-happy-path-round",
        help="Summarize happy-path prevalence from validated agent-run reports.",
    )
    happy_path_round.add_argument(
        "--validation",
        action="append",
        required=True,
        help="Agent-run validation JSON file containing happy_path_metrics. Repeat for each run.",
    )
    happy_path_round.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    telemetry_case_drafts = sub.add_parser(
        "suggest-agent-behavior-cases-from-telemetry",
        help="Create reviewable behavior-corpus draft cases from redacted telemetry JSON.",
    )
    _add_common(telemetry_case_drafts, suppress_defaults=True)
    telemetry_case_drafts.add_argument("--input", required=True, help="Telemetry envelope/record JSON file or directory.")
    telemetry_case_drafts.add_argument("--output-dir", required=True, help="Directory for agent-behavior-case-draft.v1 JSON files.")
    telemetry_case_drafts.add_argument("--app", default="medical-notes-workbench", help="App scope filter.")
    telemetry_case_drafts.add_argument("--app-version", help="Optional app_version filter.")
    telemetry_case_drafts.add_argument(
        "--min-severity",
        choices=("low", "medium", "high", "critical"),
        default="medium",
        help="Minimum signal severity for draft generation.",
    )
    telemetry_case_drafts.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    evidence_case_drafts = sub.add_parser(
        "suggest-agent-behavior-cases-from-evidence",
        help="Create reviewable behavior-corpus draft cases from telemetry, reports, manifests, or freeform evidence.",
    )
    _add_common(evidence_case_drafts, suppress_defaults=True)
    evidence_case_drafts.add_argument("--input", required=True, help="Evidence JSON/Markdown file or directory.")
    evidence_case_drafts.add_argument("--output-dir", required=True, help="Directory for agent-behavior-case-draft.v1 JSON files.")
    evidence_case_drafts.add_argument("--app", default="medical-notes-workbench", help="App scope filter.")
    evidence_case_drafts.add_argument("--app-version", help="Optional app_version filter.")
    evidence_case_drafts.add_argument(
        "--min-severity",
        choices=("low", "medium", "high", "critical"),
        default="medium",
        help="Minimum signal severity for draft generation.",
    )
    evidence_case_drafts.add_argument(
        "--source-kind",
        choices=("auto", "telemetry", "inbox_report", "human_report", "agent_report", "user_report"),
        default="auto",
        help="Evidence source label for non-telemetry inputs.",
    )
    evidence_case_drafts.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    related = sub.add_parser("related-notes-sync", help="Sync managed Notas Relacionadas from the Related Notes plugin export.")
    _add_common(related, suppress_defaults=True)
    related_mode = related.add_mutually_exclusive_group()
    related_mode.add_argument("--dry-run", action="store_true", help="Generate a plan without mutating notes. Default mode.")
    related_mode.add_argument("--apply", action="store_true", help="Rewrite managed Notas Relacionadas sections.")
    related_mode.add_argument("--recover-export", action="store_true", help="Recover stale Related Notes export through Obsidian CLI and revalidate.")
    related.add_argument(
        "--mode",
        choices=("auto", "reindex-vault", "index-missing", "export-only-diagnostic"),
        default="auto",
        help="Recovery mode for --recover-export.",
    )
    related.add_argument("--export", help="Related Notes export JSON. Defaults to .obsidian/plugins/related-notes-obsidian/medical-notes-export.json.")
    related.add_argument("--receipt", help="Receipt path for --apply output.")
    related.add_argument("--min-score", type=float, default=DEFAULT_RELATED_NOTES_MIN_SCORE)
    related.add_argument("--max-links", type=int, default=DEFAULT_RELATED_NOTES_MAX_LINKS)
    related.add_argument("--max-age-hours", type=float, default=168.0)
    related.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    graph = sub.add_parser("graph-audit", help="Audit Wiki_Medicina link graph health without writing files.")
    _add_common(graph, suppress_defaults=True)
    graph.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    validate_note = sub.add_parser("validate-note", help="Validate one generated Wiki_Medicina note style.")
    _add_common(validate_note, suppress_defaults=True)
    validate_note.add_argument("--content", required=True, help="Generated Markdown note to validate.")
    validate_note.add_argument("--title", required=True, help="Expected note title / level-1 heading.")
    validate_note.add_argument("--raw-file", help="Optional raw chat file for provenance validation.")
    validate_note.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    fix_note = sub.add_parser("fix-note", help="Apply deterministic style fixes to one generated Wiki_Medicina note.")
    _add_common(fix_note, suppress_defaults=True)
    fix_note.add_argument("--content", required=True, help="Generated Markdown note to fix.")
    fix_note.add_argument("--title", required=True, help="Expected note title / level-1 heading.")
    fix_note.add_argument("--raw-file", help="Optional raw chat file for provenance validation.")
    fix_note.add_argument("--output", required=True, help="Write fixed Markdown to this path.")
    fix_note.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    apply_canonical = sub.add_parser(
        "apply-canonical-merge",
        help="Validate and apply an architect-authored merge into an existing canonical Wiki note.",
    )
    _add_common(apply_canonical, suppress_defaults=True)
    apply_canonical.add_argument("--target", required=True, help="Existing Wiki_Medicina note to replace.")
    apply_canonical.add_argument("--content", required=True, help="Temporary full replacement Markdown written by med-knowledge-architect.")
    apply_canonical.add_argument("--coverage", required=True, help="raw-coverage.v1 proving the merged raw chat coverage.")
    apply_canonical.add_argument("--dry-run", action="store_true", help="Validate and report without writing.")
    apply_canonical.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    validate_wiki = sub.add_parser("validate-wiki", help="Audit all Markdown notes under Wiki_Medicina without writing files.")
    _add_common(validate_wiki, suppress_defaults=True)
    validate_wiki.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    fix_wiki = sub.add_parser("fix-wiki", help="Audit/fix Wiki_Medicina style and graph health.")
    _add_common(fix_wiki, suppress_defaults=True)
    fix_wiki.add_argument("--apply", action="store_true", help="Write changes in-place. Without this, only reports what would change.")
    fix_wiki.add_argument("--dry-run", action="store_true", help="Explicit preview mode; write nothing.")
    fix_wiki.add_argument(
        "--apply-taxonomy",
        action="store_true",
        help="Allow fix-wiki --apply to execute reviewed deterministic taxonomy directory moves.",
    )
    fix_wiki.add_argument(
        "--vocabulary-reset",
        action="store_true",
        help="Force a fresh vocabulary DB bootstrap/reset even when the DB already exists.",
    )
    fix_wiki.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    collect_rewrite_outputs = sub.add_parser(
        "collect-style-rewrite-outputs",
        help="Collect style rewrite outputs into a hash manifest before apply.",
    )
    _add_common(collect_rewrite_outputs, suppress_defaults=True)
    collect_rewrite_outputs.add_argument("--plan", required=True, help="Official style-rewrite subagent plan.")
    collect_rewrite_outputs.add_argument("--manifest", required=True, help="Manifest JSON to write.")
    collect_rewrite_outputs.add_argument("--work-id", default="", help="Collect only one finalized work item.")
    collect_rewrite_outputs.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    finalize_rewrite_output = sub.add_parser(
        "finalize-style-rewrite-output",
        help="Validate one style rewrite output and write the Workbench attestation.",
    )
    _add_common(finalize_rewrite_output, suppress_defaults=True)
    finalize_rewrite_output.add_argument("--plan", required=True, help="Official style-rewrite subagent plan.")
    finalize_rewrite_output.add_argument("--work-id", required=True, help="Work item id to finalize.")
    finalize_rewrite_output.add_argument("--actual-model", default="", help=argparse.SUPPRESS)
    finalize_rewrite_output.add_argument("--provider", default="", help=argparse.SUPPRESS)
    finalize_rewrite_output.add_argument(
        "--specialist-run-receipt",
        default="",
        help="Workbench-validated specialist task receipt for model provenance.",
    )
    finalize_rewrite_output.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    finalize_agy_specialist = sub.add_parser(
        "finalize-agy-specialist-task",
        help="Validate one AGY specialist output and write a Workbench specialist receipt.",
    )
    _add_common(finalize_agy_specialist, suppress_defaults=True)
    finalize_agy_specialist.add_argument("--plan", required=True, help="Official style-rewrite subagent plan.")
    finalize_agy_specialist.add_argument("--work-id", required=True, help="Work item id to finalize.")
    finalize_agy_specialist.add_argument(
        "--transcript",
        required=True,
        help="AGY transcript or task log containing model/runtime evidence.",
    )
    finalize_agy_specialist.add_argument(
        "--runtime-log",
        default="",
        help="Optional AGY runtime log proving a settings-switch model override for this specialist session.",
    )
    finalize_agy_specialist.add_argument(
        "--requested-model",
        default="Gemini 3.1 Pro (High)",
        help=argparse.SUPPRESS,
    )
    finalize_agy_specialist.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    finalize_opencode_specialist = sub.add_parser(
        "finalize-opencode-specialist-task",
        help="Validate one OpenCode task specialist output and write a Workbench specialist receipt.",
    )
    _add_common(finalize_opencode_specialist, suppress_defaults=True)
    finalize_opencode_specialist.add_argument("--plan", required=True, help="Official style-rewrite subagent plan.")
    finalize_opencode_specialist.add_argument("--work-id", required=True, help="Work item id to finalize.")
    finalize_opencode_specialist.add_argument(
        "--task-metadata",
        default="",
        help="Optional OpenCode task metadata path. When omitted, the CLI uses the native hook-captured metadata for --work-id.",
    )
    finalize_opencode_specialist.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    finalize_opencode_architect = sub.add_parser(
        "finalize-opencode-architect-task",
        help="Validate one OpenCode architect output for process-chats serial consolidation.",
    )
    _add_common(finalize_opencode_architect, suppress_defaults=True)
    finalize_opencode_architect.add_argument("--plan", required=True, help="Official architect subagent plan.")
    finalize_opencode_architect.add_argument("--work-id", required=True, help="Architect work item id to finalize.")
    finalize_opencode_architect.add_argument(
        "--task-metadata",
        default="",
        help="Optional OpenCode task metadata path. When omitted, the CLI uses native hook-captured metadata for --work-id.",
    )
    finalize_opencode_architect.add_argument(
        "--architect-output",
        default="",
        help="Optional architect-output.v1 JSON path. When omitted, the CLI uses native hook-captured output for --work-id.",
    )
    finalize_opencode_architect.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    apply_rewrite = sub.add_parser(
        "apply-style-rewrite",
        help="Validate/apply a finalized style rewrite; real apply requires plan, manifest and work-id.",
    )
    _add_common(apply_rewrite, suppress_defaults=True)
    apply_rewrite.add_argument("--target", help="Preview-only target note path; use only with --dry-run.")
    apply_rewrite.add_argument("--content", help="Preview-only rewritten Markdown path; use only with --dry-run.")
    apply_rewrite.add_argument("--plan", help="Official style-rewrite subagent plan; required for real apply.")
    apply_rewrite.add_argument("--outputs", help="Hash manifest from collect-style-rewrite-outputs; required for real apply.")
    apply_rewrite.add_argument("--work-id", help="Work item id to apply from the style-rewrite manifest; required for real apply.")
    apply_rewrite.add_argument("--dry-run", action="store_true", help="Validate and report without writing.")
    apply_rewrite.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    atomic_apply_rewrite = sub.add_parser(
        "apply-specialist-style-rewrite",
        help="Finalize, collect and apply one official specialist style rewrite atomically.",
    )
    _add_common(atomic_apply_rewrite, suppress_defaults=True)
    atomic_apply_rewrite.add_argument("--plan", required=True, help="Official style-rewrite subagent plan.")
    atomic_apply_rewrite.add_argument("--manifest", required=True, help="Single-item manifest JSON to write and consume.")
    atomic_apply_rewrite.add_argument("--work-id", required=True, help="Work item id to finalize and apply.")
    atomic_apply_rewrite.add_argument(
        "--specialist-run-receipt",
        required=True,
        help="Workbench-validated specialist task receipt for this work item.",
    )
    atomic_apply_rewrite.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    run_finish = sub.add_parser(
        "run-finish",
        help=argparse.SUPPRESS,
    )
    run_finish.add_argument("--agent", required=True)
    run_finish.add_argument("--workflow")
    run_finish.add_argument("--title")
    run_finish.add_argument("--body-file")
    run_finish.add_argument("--tool")
    run_finish.add_argument("--subagent")
    run_finish.add_argument("--run-id")
    run_finish.add_argument("--trigger-context")
    run_finish.add_argument("--receipt")
    run_finish.add_argument("--notes-touched")
    run_finish.add_argument("--branch")
    run_finish.add_argument("--vault-dir")
    run_finish.add_argument("--public-json", action="store_true")
    run_finish.add_argument("--json", action="store_true")

    apply_note_merge_parser = sub.add_parser("apply-note-merge", help="Validate and apply one semantic note merge.")
    _add_common(apply_note_merge_parser, suppress_defaults=True)
    apply_note_merge_parser.add_argument("--plan", required=True, help="Official note-merge-plan.v1 JSON.")
    apply_note_merge_parser.add_argument("--content", required=True, help="Temporary merged Markdown note.")
    apply_note_merge_parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing/removing files.")
    apply_note_merge_parser.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    apply_atomicity = sub.add_parser("apply-atomicity-split", help="Validate and apply one atomicity split bundle.")
    _add_common(apply_atomicity, suppress_defaults=True)
    apply_atomicity.add_argument("--bundle", required=True, help="atomicity-split-bundle.v1 produced by med-knowledge-architect.")
    apply_atomicity.add_argument("--defer-linker", action="store_true", help="Internal parent-batch mode: emit trigger context but do not run linker.")
    apply_atomicity.add_argument("--parent-batch-id", default="", help="Required with --defer-linker.")
    apply_atomicity.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    eval_body_linker = sub.add_parser("evaluate-body-linker", help="Run redacted body-linker quality fixtures.")
    eval_body_linker.add_argument("--cases", required=True, help="body-linker-eval-suite.v1 JSON.")
    eval_body_linker.add_argument("--vocabulary-db", required=True, help="Vocabulary SQLite DB used by the cases.")
    eval_body_linker.add_argument("--max-false-positive-rate", type=float, default=0.0, help="Quality gate budget.")
    eval_body_linker.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    validate = sub.add_parser("validate", help="Print resolved paths and existence checks.")
    _add_common(validate, suppress_defaults=True)
    validate.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")

    root_hygiene = sub.add_parser("root-hygiene-audit", help="Audit loose files in the user root without deleting anything.")
    _add_common(root_hygiene, suppress_defaults=True)
    root_hygiene.add_argument("--user-root", help="Override user root to audit; defaults to HOME.")
    root_hygiene.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = time.time()
    exit_code = EXIT_OK
    emitted_payload: object = None

    def _emit_compact_json(data: object) -> None:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    json_emit = _emit_compact_json if _agent_stdout_compact_enabled() else _emit_json

    def _json(data: object) -> None:
        nonlocal emitted_payload
        if isinstance(data, dict):
            data = harden_operational_payload(
                data,
                workflow=_workflow_for_args(args),
                command=str(getattr(args, "command", "")),
                require_error_context=_requires_operational_error_context(args, data),
            )
        emitted_payload = data
        _emit_agent_preamble_if_enabled(data)
        json_emit(data)

    try:
        config = None

        def get_config():
            nonlocal config
            if config is None:
                config = resolve_config(args)
            return config

        if args.command == "run-finish":
            return _delegate_vault_git_run_finish(args)

        if args.command == "environment-preflight":
            payload = _environment_preflight_cli_payload()
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "set-paths":
            payload = plan_set_paths(
                config=getattr(args, "config", None),
                wiki_dir=args.wiki_dir,
                raw_dir=args.raw_dir,
                agent_repair=bool(getattr(args, "agent_repair", False)),
            )
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "repair-config-template":
            payload = repair_config_template(
                config=getattr(args, "config", None),
                template=getattr(args, "template", None),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "validate-agent-session":
            payload = _validate_agent_session_payload(_path(args.transcript))
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "audit-agent-transcript":
            payload = audit_agent_transcript(
                transcript_path=_path(args.transcript),
                workflow=cast(AuditWorkflow, args.workflow),
                workflow_payload_path=_path(args.workflow_payload) if getattr(args, "workflow_payload", None) else None,
                final_report_path=_path(args.final_report) if getattr(args, "final_report", None) else None,
                runtime_log_paths=[_path(path) for path in getattr(args, "runtime_log", [])],
            ).to_payload()
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "validate-agent-run-report":
            payload = _validate_agent_run_report_payload(
                workflow_payload_path=_path(args.workflow_payload),
                transcript_path=_path(args.transcript) if getattr(args, "transcript", None) else None,
                final_report_path=_path(args.final_report) if getattr(args, "final_report", None) else None,
                runtime_log_paths=[_path(path) for path in getattr(args, "runtime_log", [])],
            )
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "summarize-happy-path-round":
            payload = _summarize_happy_path_round_payload(
                [_path(path) for path in getattr(args, "validation", [])]
            )
            _json(payload)
            return EXIT_OK

        if args.command in {"markdown-query-status", "markdown-query-rebuild", "markdown-query-probe"}:
            payload = _markdown_query_cli_payload(get_config(), args.command)
            _json(payload)
            if _json_field(payload, "status") == "blocked" and args.command != "markdown-query-status":
                return EXIT_VALIDATION
            return EXIT_OK

        _hydrate_run_linker_apply_args_from_diagnosis(args)

        direct_target = direct_guard_target(args)
        if direct_target is not None:
            require_vault_guard(
                direct_target,
                workflow=_workflow_for_args(args),
                command=str(getattr(args, "command", "")),
            )
        elif command_may_require_guard(args):
            guard_config = get_config()
            if command_requires_guard(args, guard_config):
                require_vault_guard(
                    guard_config.wiki_dir,
                    workflow=_workflow_for_args(args),
                    command=str(getattr(args, "command", "")),
                )

        if args.command == "taxonomy-canonical":
            _json(canonical_taxonomy_tree())
            return EXIT_OK
        if args.command == "root-hygiene-audit":
            _json(audit_user_root_hygiene(_path(args.user_root) if args.user_root else None))
            return EXIT_OK
        if args.command == "evaluate-body-linker":
            result = evaluate_body_linker_cases(
                fixture_path=_path(args.cases),
                vocabulary_db_path=_path(args.vocabulary_db),
                max_false_positive_rate=float(args.max_false_positive_rate),
            )
            _json(result)
            quality_gate = _dict_value(_json_field(result, "quality_gate"))
            if _json_field(quality_gate, "status") == "failed":
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "eval-triager-output":
            expectations = (
                load_triager_prompt_expectations(_path(args.expectations))
                if getattr(args, "expectations", None)
                else None
            )
            signing_key_value = os.environ.get("MEDNOTES_SUBAGENT_RUNNER_SIGNING_KEY")
            subagent_runner_signing_key = signing_key_value if isinstance(signing_key_value, str) else ""
            result = evaluate_triager_prompt_output(
                raw_file=_path(args.raw_file),
                output_path=_path(args.output),
                expectations=expectations,
                baseline_eval_path=_path(args.baseline_eval) if args.baseline_eval else None,
                require_agent_metrics=bool(getattr(args, "require_agent_metrics", False)),
                subagent_run_receipt_path=_path(args.subagent_run_receipt)
                if getattr(args, "subagent_run_receipt", None)
                else None,
                require_subagent_run_receipt=bool(getattr(args, "require_subagent_run_receipt", False)),
                subagent_runner_signing_key=subagent_runner_signing_key,
            )
            if args.report:
                _write_json_atomic(_path(args.report), result)
            _json(result)
            if _json_field(result, "status") != "pass":
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "eval-agent-behavior-corpus":
            if args.report:
                validate_agent_behavior_report_path(corpus_path=_path(args.corpus), report_path=_path(args.report))
            result = evaluate_agent_behavior_corpus(_path(args.corpus))
            if args.report:
                _write_json_atomic(_path(args.report), result)
            _json(result)
            if _json_field(result, "status") == "needs_review":
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "suggest-agent-behavior-cases-from-telemetry":
            result = suggest_agent_behavior_cases_from_telemetry(
                _path(args.input),
                output_dir=_path(args.output_dir),
                app=str(args.app),
                app_version=str(args.app_version) if args.app_version else None,
                min_severity=str(args.min_severity),
            )
            _json(result)
            return EXIT_OK
        if args.command == "suggest-agent-behavior-cases-from-evidence":
            result = suggest_agent_behavior_cases_from_evidence(
                _path(args.input),
                output_dir=_path(args.output_dir),
                app=str(args.app),
                app_version=str(args.app_version) if args.app_version else None,
                min_severity=str(args.min_severity),
                source_kind=str(args.source_kind),
            )
            _json(result)
            return EXIT_OK
        if args.command == "status":
            try:
                payload = _status_cli_payload(get_config())
            except WikiPathResolutionError as exc:
                payload = _status_without_wiki_payload(args, exc)
            _json(payload)
            if _json_field(payload, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK

        if args.command == "validate":
            try:
                _json(validate_config(get_config()))
            except WikiPathResolutionError as exc:
                _json(_validate_without_wiki_payload(args, exc))
            return EXIT_OK
        if args.command == "validate-note":
            report = validate_note_style_file(
                _path(args.content),
                args.title,
                raw_file=_path(args.raw_file) if args.raw_file else None,
            )
            if args.raw_file:
                artifact_dir = _path(args.artifact_dir) if getattr(args, "artifact_dir", None) else None
                artifact_report = validate_note_artifacts(
                    _path(args.content).read_text(encoding="utf-8"),
                    raw_file=_path(args.raw_file),
                    artifact_dir=artifact_dir,
                )
                report["artifact_validation"] = artifact_report
            report["repair_route"] = _note_repair_route(report)
            _json(
                annotate_payload(
                    report,
                    phase="validate_note",
                    status="blocked" if report["errors"] else "completed",
                    blocked_reason="validation_errors" if report["errors"] else "",
                    next_action=_validate_note_next_action(report),
                    required_inputs=["content", "title", "raw_file"],
                )
            )
            if report["errors"]:
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "fix-note":
            report = fix_note_style_file(
                _path(args.content),
                args.title,
                _path(args.output),
                raw_file=_path(args.raw_file) if args.raw_file else None,
            )
            report["repair_route"] = _note_repair_route(report)
            _json(
                annotate_payload(
                    report,
                    phase="fix_note",
                    status="blocked" if report["errors"] else "completed",
                    blocked_reason="validation_errors" if report["errors"] else "",
                    next_action=_fix_note_next_action(report),
                    required_inputs=["content", "title", "output", "raw_file"],
                )
            )
            if report["errors"]:
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "collect-style-rewrite-outputs":
            result = collect_style_rewrite_outputs(
                _path(args.plan),
                _path(args.manifest),
                work_id=_namespace_string(args, "work_id"),
            )
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "finalize-style-rewrite-output":
            result = finalize_style_rewrite_output(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                actual_model=_namespace_string(args, "actual_model"),
                provider=_namespace_string(args, "provider"),
                specialist_run_receipt_path=_path(args.specialist_run_receipt)
                if args.specialist_run_receipt
                else None,
            )
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "finalize-agy-specialist-task":
            result = finalize_agy_specialist_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                transcript_path=_path(args.transcript),
                runtime_log_path=_path(args.runtime_log) if args.runtime_log else None,
                requested_model=str(args.requested_model or "Gemini 3.1 Pro (High)"),
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "finalize-opencode-specialist-task":
            result = finalize_opencode_specialist_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                task_metadata_path=_path(args.task_metadata) if args.task_metadata else None,
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "finalize-opencode-architect-task":
            result = finalize_opencode_architect_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                task_metadata_path=_path(args.task_metadata) if args.task_metadata else None,
                architect_output_path=_path(args.architect_output) if args.architect_output else None,
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
            return EXIT_OK
        if args.command == "apply-style-rewrite":
            result = _style_rewrite_apply_payload(args)
            written_target = _style_rewrite_written_target(result, args)
            if not args.dry_run and written_target is not None and _json_field(result, "status") != "blocked":
                link_config = _resolve_config_for_target(args, written_target)
                trigger_context = _single_modified_note_context(
                    source_workflow="/mednotes:fix-wiki",
                    path=written_target,
                    config=link_config,
                    batch_id=str(written_target),
                )
                _record_linker_run_evidence(
                    result,
                    _auto_run_linker_from_trigger_context(
                        link_config,
                        trigger_context,
                        label="style-rewrite",
                        include_related_notes=True,
                    ),
                )
                result = finalize_style_rewrite_apply_receipt(result)
            _json(_style_rewrite_stdout_payload(result))
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
            if _json_field(result, "status") == "completed_with_link_blockers":
                return EXIT_VALIDATION
            validation = _dict_value(_json_field(result, "validation"))
            if _json_field(validation, "errors"):
                return EXIT_VALIDATION
            return EXIT_OK

        config = get_config()
        if args.command == "list-pending":
            covered_ids = set(covered_raw_chat_index(config.wiki_dir))
            rows = list_by_status(
                config.raw_dir,
                "pending",
                covered_raw_chat_ids=covered_ids,
            )
            triaged_rows = list_by_status(config.raw_dir, "triados")
            _json(
                _status_payload(
                    rows,
                    "pending",
                    args,
                    backlog_counts={"pending": len(rows), "triados": len(triaged_rows)},
                )
            )
        elif args.command == "list-triados":
            covered_ids = set(covered_raw_chat_index(config.wiki_dir))
            pending_rows = list_by_status(config.raw_dir, "pending", covered_raw_chat_ids=covered_ids)
            rows = list_by_status(config.raw_dir, "triados")
            _json(
                _status_payload(
                    rows,
                    "triados",
                    args,
                    backlog_counts={"pending": len(pending_rows), "triados": len(rows)},
                )
            )
        elif args.command == "plan-subagents":
            result = plan_subagents(
                config,
                args.phase,
                max_concurrency=args.max_concurrency or None,
                temp_root=_path(args.temp_root) if args.temp_root else None,
                limit=args.limit,
                fix_wiki_plan_path=_path(args.fix_wiki_plan) if getattr(args, "fix_wiki_plan", None) else None,
            )
            if args.output:
                output = _path(args.output)
                _write_json_atomic(output, result)
                result = _plan_output_receipt_payload(result=result, phase=args.phase, output=output)
            _json(result)
            if _json_field(result, "status") in {"blocked", "needs_review"} or _json_field(result, "plan_status") in {"blocked", "needs_review"}:
                return EXIT_VALIDATION
        elif args.command == "taxonomy-canonical":
            _json(canonical_taxonomy_tree())
        elif args.command == "taxonomy-tree":
            _json(taxonomy_tree(config.wiki_dir, max_depth=args.max_depth))
        elif args.command == "taxonomy-audit":
            _json(taxonomy_audit(config.wiki_dir))
        elif args.command == "taxonomy-status":
            result = taxonomy_status(config.wiki_dir)
            if args.report_output:
                output = _path(args.report_output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(result["human_report_markdown"], encoding="utf-8")
                result["report_path"] = str(output)
            _json(result)
        elif args.command == "taxonomy-plan":
            plan = taxonomy_migration_plan(config.wiki_dir)
            if args.plan_output:
                output = _path(args.plan_output)
                output.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(output, plan)
                plan["plan_path"] = str(output)
            _write_optional_taxonomy_report(plan, args.report_output)
            _json(plan)
        elif args.command == "taxonomy-apply":
            result = _apply_taxonomy_plan_and_link(
                plan_path=_path(args.plan),
                config=config,
                receipt_path=_path(args.receipt) if args.receipt else None,
            )
            _json(result)
        elif args.command == "taxonomy-rollback":
            result = _rollback_taxonomy_receipt_and_link(receipt_path=_path(args.receipt), config=config)
            _json(result)
        elif args.command == "taxonomy-migrate":
            if args.rollback:
                if not args.receipt:
                    raise ValidationError("--receipt is required with --rollback")
                _json(_rollback_taxonomy_receipt_and_link(receipt_path=_path(args.receipt), config=config))
            elif args.apply:
                if not args.plan:
                    raise ValidationError("vocabulary_recovery_plan_required: --plan is required with --apply")
                _json(
                    _apply_taxonomy_plan_and_link(
                        plan_path=_path(args.plan),
                        config=config,
                        receipt_path=_path(args.receipt) if args.receipt else None,
                    )
                )
            else:
                plan = taxonomy_migration_plan(config.wiki_dir)
                if args.plan_output:
                    output = _path(args.plan_output)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    _write_json_atomic(output, plan)
                    plan["plan_path"] = str(output)
                _json(plan)
        elif args.command == "taxonomy-resolve":
            resolved = resolve_taxonomy(
                config.wiki_dir,
                args.taxonomy,
                title=args.title,
                allow_new_leaf=args.allow_new_taxonomy_leaf,
            )
            _json(resolved.to_json(config.wiki_dir, title=args.title))
        elif args.command == "triage":
            existing_raw_meta = read_note_meta(_path(args.raw_file))
            updates = {
                "tipo": args.tipo,
                "status": "triado",
                "data_importacao": _preserve_triage_import_date(existing_raw_meta),
                "fonte_id": args.fonte_id,
                "titulo_triagem": _preserve_richer_triage_title(existing_raw_meta, args.titulo),
            }
            if args.tipo.lower() == "medicina":
                if not args.note_plan:
                    raise ValidationError("--note-plan is required when --tipo medicina")
                note_plan_data = _read_json(_path(args.note_plan))
                if not isinstance(note_plan_data, dict):
                    raise ValidationError("note_plan must be a JSON object")
                serialized_note_plan = serialize_triage_note_plan(
                    note_plan_data,
                    _path(args.raw_file),
                )
                if not args.dry_run:
                    if not getattr(args, "triager_eval", None):
                        raise ValidationError(
                            "triager_eval_required: run eval-triager-output --raw-file <raw.md> "
                            "--output <triager-output.json> --report <triager-eval.json> --json, "
                            "then repeat triage with --triager-eval <triager-eval.json>."
                        )
                    validate_triager_prompt_eval_for_note_plan(
                        eval_path=_path(args.triager_eval),
                        raw_file=_path(args.raw_file),
                        note_plan=note_plan_data,
                    )
                updates["note_plan"] = serialized_note_plan
            _json(
                mutate_raw_frontmatter(
                    _path(args.raw_file),
                    updates,
                    dry_run=args.dry_run,
                    backup=False,
                )
            )
        elif args.command == "discard":
            _json(
                mutate_raw_frontmatter(
                    _path(args.raw_file),
                    {"status": "descartado", "discard_reason": args.reason, "discarded_at": _now_iso()},
                    dry_run=args.dry_run,
                    backup=False,
                )
            )
        elif args.command == "stage-note":
            _json(
                stage_note(
                    _path(args.manifest),
                    _path(args.raw_file),
                    args.taxonomy,
                    args.title,
                    _path(args.content),
                    args.dry_run,
                    config=config,
                    allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                    coverage_path=_path(args.coverage) if args.coverage else None,
                )
            )
        elif args.command == "publish-status":
            result = diagnose_publish_state(
                _path(args.manifest),
                config,
                collision=args.collision,
                allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                require_coverage=args.require_coverage,
            )
            _json(result)
            if _json_field(result, "status") != "ready":
                return EXIT_VALIDATION
        elif args.command == "publish-batch":
            manifest = _path(args.manifest)
            if not args.dry_run:
                require_publish_dry_run(
                    manifest,
                    config,
                    collision=args.collision,
                    allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                    require_coverage=args.require_coverage,
                )
                new_leaf_authorization = taxonomy_new_leaf_authorization_for_manifest(
                    manifest,
                    config,
                    collision=args.collision,
                    allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                    require_coverage=args.require_coverage,
                )
                require_publish_dry_run(
                    manifest,
                    config,
                    collision=args.collision,
                    allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                    require_coverage=args.require_coverage,
                    new_taxonomy_leaf_authorization=new_leaf_authorization,
                )
            result = publish_batch_operation_result(
                manifest,
                config,
                collision=args.collision,
                dry_run=args.dry_run,
                backup=False,
                allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                require_coverage=args.require_coverage,
            )
            result = _process_chats_result_with_guard_safety(
                config,
                JsonObjectAdapter.validate_python(result),
                applying=not bool(args.dry_run),
            )
            if _process_chats_publish_operation_paused(result):
                payload = _process_chats_fsm_payload_from_result(result, manifest, applying=not bool(args.dry_run))
                _json(payload)
                return process_chats_cli_exit_code(payload)
            if args.dry_run:
                taxonomy_authorization = _dict_value(_json_field(result, "new_taxonomy_leaf_authorization"))
                receipt = record_publish_dry_run(
                    manifest,
                    config,
                    collision=args.collision,
                    allow_new_taxonomy_leaf=args.allow_new_taxonomy_leaf,
                    require_coverage=args.require_coverage,
                    new_taxonomy_leaf_authorization=taxonomy_authorization or None,
                )
                result["dry_run_receipt"] = {
                    "path": str(manifest),
                    "expires_at": receipt["expires_at"],
                    "manifest_hash": _json_field(receipt, "manifest_hash") or _json_field(receipt, "manifest_sha256"),
                    "dry_run_options_hash": _json_field(receipt, "dry_run_options_hash"),
                    "batch_state": _json_field(receipt, "batch_state", []),
                }
            else:
                clear_publish_dry_run(manifest)
                trigger_context = _publish_trigger_context(result, config, manifest)
                if trigger_context is not None:
                    _record_linker_run_evidence(
                        result,
                        _auto_run_linker_from_trigger_context(
                            config,
                            trigger_context,
                            label="process-chats",
                            include_related_notes=True,
                        ),
                    )
            payload = _process_chats_fsm_payload_from_result(result, manifest, applying=not bool(args.dry_run))
            _json(payload)
            exit_code = process_chats_cli_exit_code(payload)
            if exit_code:
                return exit_code
        elif args.command == "apply-canonical-merge":
            result = apply_canonical_merge(
                config,
                _path(args.target),
                _path(args.content),
                _path(args.coverage),
                dry_run=args.dry_run,
                backup=False,
            )
            trigger_context = _json_field(result, "link_trigger_context")
            if isinstance(trigger_context, dict) and _json_field(result, "status") == "completed":
                _record_linker_run_evidence(
                    result,
                    _auto_run_linker_from_trigger_context(
                        config,
                        trigger_context,
                        label="process-chats-canonical-merge",
                        include_related_notes=True,
                    ),
                )
            _json(result)
            if _json_field(result, "status") == "failed":
                return EXIT_IO
            if _json_field(result, "status") == "blocked" or result["validation"]["errors"]:
                return EXIT_VALIDATION
        elif args.command == "run-linker":
            if not args.diagnose and not args.apply:
                result = {
                    "schema": "medical-notes-workbench.link-run.v1",
                    "phase": "link_mode_selection",
                    "status": "blocked",
                    "blocked_reason": "linker_mode_required",
                    "next_action": "Rode run-linker --diagnose para gerar um plano ou run-linker --apply --diagnosis <json> para aplicar um diagnóstico validado.",
                    "required_inputs": ["diagnose_or_apply"],
                    "human_decision_required": False,
                    "blocker_count": 1,
                    "returncode": EXIT_VALIDATION,
                }
                payload = _link_fsm_payload_from_result(result, args)
                _json(payload)
                return link_cli_exit_code(payload)
            if args.apply and not args.diagnosis:
                result = {
                    "schema": "medical-notes-workbench.link-run.v1",
                    "phase": "link_mode_selection",
                    "status": "blocked",
                    "blocked_reason": "diagnosis_required",
                    "next_action": "Rode run-linker --diagnose primeiro e passe o arquivo com --diagnosis.",
                    "required_inputs": ["diagnosis"],
                    "human_decision_required": False,
                    "blocker_count": 1,
                    "returncode": EXIT_VALIDATION,
                }
                payload = _link_fsm_payload_from_result(result, args)
                _json(payload)
                return link_cli_exit_code(payload)
            if args.apply and args.trigger_context:
                result = {
                    "schema": "medical-notes-workbench.link-run.v1",
                    "phase": "link_mode_selection",
                    "status": "blocked",
                    "blocked_reason": "trigger_context_apply_not_allowed",
                    "next_action": "Rode run-linker --diagnose --trigger-context <json> e aplique depois com --apply --diagnosis <json>.",
                    "required_inputs": ["diagnosis"],
                    "human_decision_required": False,
                    "blocker_count": 1,
                    "returncode": EXIT_VALIDATION,
                }
                payload = _link_fsm_payload_from_result(result, args)
                _json(payload)
                return link_cli_exit_code(payload)
            if args.force_diagnose and not args.diagnose:
                result = {
                    "schema": "medical-notes-workbench.link-run.v1",
                    "phase": "link_mode_selection",
                    "status": "blocked",
                    "blocked_reason": "force_diagnose_requires_diagnose",
                    "next_action": "Use --force-diagnose apenas com run-linker --diagnose.",
                    "required_inputs": ["diagnose"],
                    "human_decision_required": False,
                }
                payload = _link_fsm_payload_from_result(result, args)
                _json(payload)
                return link_cli_exit_code(payload)
            result = run_linker(
                config,
                diagnose=bool(args.diagnose),
                apply=bool(args.apply),
                diagnosis_path=_path(args.diagnosis) if args.diagnosis else None,
                receipt_path=_path(args.receipt) if args.receipt else None,
                trigger_context_path=_path(args.trigger_context) if args.trigger_context else None,
                include_related_notes=not bool(args.no_related_notes),
                backup=False,
                force_diagnose=bool(args.force_diagnose),
                llm_disambiguation=str(args.llm_disambiguation),
                llm_model=str(args.llm_model) if args.llm_model else None,
                llm_timeout=int(args.llm_timeout),
                version_control_guard_active=active_guard_exists(config.wiki_dir),
            )
            result = _link_result_with_guard_safety(config, result, applying=bool(args.apply))
            payload = _link_fsm_payload_from_result(result, args)
            _json(payload)
            exit_code = link_cli_exit_code(payload)
            if exit_code:
                return exit_code
        elif args.command == "apply-semantic-ingestion":
            item = _read_json(_path(args.input))
            if not isinstance(item, dict):
                raise ValidationError("semantic ingestion input must be a JSON object")
            vocabulary_db_path = config.vocabulary_db_path
            if vocabulary_db_path is None:
                raise ValidationError("vocabulary_db_path is required")
            result = apply_semantic_ingestion(
                db_path=vocabulary_db_path,
                item=item,
                require_contract=True,
            )
            if args.receipt:
                _write_json_atomic(_path(args.receipt), result)
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
        elif args.command == "vocabulary-status":
            vocabulary_db_path = config.vocabulary_db_path
            if vocabulary_db_path is None:
                raise ValidationError("vocabulary_db_path is required")
            result = vocabulary_status(vocabulary_db_path)
            _json(result)
            if _json_field(result, "status") in {"blocked", "degraded"}:
                return EXIT_VALIDATION
        elif args.command == "vocabulary-recover":
            if args.apply:
                if args.plan_output:
                    raise ValidationError("--plan-output is only valid with --dry-run")
                if not args.plan:
                    raise ValidationError("--plan is required with --apply")
                if not args.receipt:
                    result = {
                        "schema": "medical-notes-workbench.vocabulary-recovery-receipt.v1",
                        "status": "blocked",
                        "blocked_reason": "vocabulary_recovery_receipt_required",
                        "next_action": "Repetir com --receipt <receipt.json> para preservar rollback/auditoria antes de mutar SQLite.",
                        "required_inputs": ["receipt"],
                        "human_decision_required": False,
                    }
                    _json(result)
                    return EXIT_VALIDATION
                plan = _read_json(_path(args.plan))
                if not isinstance(plan, dict):
                    raise ValidationError("vocabulary recovery plan must be a JSON object")
                result = apply_vocabulary_recovery_plan(
                    db_path=config.vocabulary_db_path,
                    plan=plan,
                    receipt_path=_path(args.receipt),
                )
            else:
                result = build_vocabulary_recovery_plan(
                    db_path=config.vocabulary_db_path,
                    mode=str(args.mode),
                    wiki_dir=config.wiki_dir,
                    catalog_path=config.catalog_path,
                )
                plan_output = getattr(args, "plan_output", None) or args.plan
                if plan_output:
                    output = _path(plan_output)
                    _write_json_atomic(output, result)
                    if getattr(args, "plan_output", None):
                        plan_fields = _WorkflowStatusCliFields.model_validate(result)
                        result = {
                            "schema": "medical-notes-workbench.vocabulary-recovery-plan-receipt.v1",
                            "phase": "vocabulary-recover",
                            "status": "written",
                            "plan_path": str(output),
                            "plan_schema": plan_fields.schema_id,
                            "plan_status": plan_fields.status,
                            "mode": plan_fields.mode or _namespace_string(args, "mode"),
                            "action_count": len(_list_value(_json_field(result, "actions"))),
                            "blocked_count": len(_list_value(_json_field(result, "blocked_items"))),
                            "db_path": str(config.vocabulary_db_path),
                        }
            _json(result)
            if _json_field(result, "status") not in {"planned", "skipped", "applied", "completed", "written"}:
                return EXIT_VALIDATION
        elif args.command == "collect-curator-outputs":
            plan = _read_json(_path(args.plan))
            if not isinstance(plan, dict):
                raise ValidationError("curator batch plan must be a JSON object")
            result = collect_curator_outputs(
                plan=plan,
                manifest_path=_path(args.manifest),
                include_missing=bool(getattr(args, "include_missing", False)),
            )
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
        elif args.command == "apply-curator-batch":
            plan = _read_json(_path(args.plan))
            if not isinstance(plan, dict):
                raise ValidationError("curator batch plan must be a JSON object")
            if args.validate_only:
                result = validate_curator_batch_outputs(plan=plan, manifest_path=_path(args.outputs))
                _json(result)
                if _json_field(result, "status") == "blocked":
                    return EXIT_VALIDATION
                return EXIT_OK
            result = apply_curator_batch_outputs(
                plan=plan,
                manifest_path=_path(args.outputs),
                prompt_eval_path=_path(args.prompt_eval) if args.prompt_eval else None,
                skip_prompt_eval=bool(args.skip_prompt_eval),
                skip_prompt_eval_reason=_namespace_string(args, "skip_prompt_eval_reason"),
            )
            if args.receipt:
                _write_json_atomic(_path(args.receipt), result)
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
        elif args.command == "eval-curator-batch":
            plan = _read_json(_path(args.plan))
            if not isinstance(plan, dict):
                raise ValidationError("curator batch plan must be a JSON object")
            if args.expectations:
                plan = dict(plan)
                plan["evaluation_expectations_by_work_id"] = load_curator_prompt_expectations(
                    _path(args.expectations),
                    expected_plan_hash=curator_plan_hash(plan),
                )
            result = evaluate_curator_prompt_outputs(
                plan=plan,
                manifest_path=_path(args.outputs),
                baseline_eval_path=_path(args.baseline_eval) if args.baseline_eval else None,
            )
            if args.report:
                _write_json_atomic(_path(args.report), result)
            _json(result)
            if _json_field(result, "status") == "needs_review":
                return EXIT_VALIDATION
        elif args.command == "init-curator-expectations":
            plan = _read_json(_path(args.plan))
            if not isinstance(plan, dict):
                raise ValidationError("curator batch plan must be a JSON object")
            result = build_curator_prompt_expectations_template(plan)
            _write_json_atomic(_path(args.output), result)
            _json(result)
        elif args.command == "promote-curator-baseline":
            result = promote_curator_prompt_baseline(_path(args.eval))
            _write_json_atomic(_path(args.output), result)
            _json(result)
        elif args.command == "eval-agent-behavior-corpus":
            if args.report:
                validate_agent_behavior_report_path(corpus_path=_path(args.corpus), report_path=_path(args.report))
            result = evaluate_agent_behavior_corpus(_path(args.corpus))
            if args.report:
                _write_json_atomic(_path(args.report), result)
            _json(result)
            if _json_field(result, "status") == "needs_review":
                return EXIT_VALIDATION
        elif args.command == "suggest-agent-behavior-cases-from-telemetry":
            result = suggest_agent_behavior_cases_from_telemetry(
                _path(args.input),
                output_dir=_path(args.output_dir),
                app=str(args.app),
                app_version=str(args.app_version) if args.app_version else None,
                min_severity=str(args.min_severity),
            )
            _json(result)
        elif args.command == "suggest-agent-behavior-cases-from-evidence":
            result = suggest_agent_behavior_cases_from_evidence(
                _path(args.input),
                output_dir=_path(args.output_dir),
                app=str(args.app),
                app_version=str(args.app_version) if args.app_version else None,
                min_severity=str(args.min_severity),
                source_kind=str(args.source_kind),
            )
            _json(result)
        elif args.command == "related-notes-sync":
            if args.recover_export:
                result = recover_related_notes_export_operation_result(
                    config,
                    export_path=_path(args.export) if args.export else None,
                    mode=str(args.mode),
                )
                payload = _link_related_fsm_payload_from_result(result, mode="recover_export", applying=False)
            else:
                result = sync_related_notes_operation_result(
                    config,
                    export_path=_path(args.export) if args.export else None,
                    apply=bool(args.apply),
                    backup=False,
                    receipt_path=_path(args.receipt) if args.receipt else None,
                    min_score=float(args.min_score),
                    max_links=int(args.max_links),
                    max_age_hours=float(args.max_age_hours),
                )
                result = _link_related_result_with_guard_safety(config, result, applying=bool(args.apply))
                payload = _link_related_fsm_payload_from_result(
                    result,
                    mode="apply" if bool(args.apply) else "dry_run",
                    applying=bool(args.apply),
                )
            _json(payload)
            exit_code = link_related_cli_exit_code(payload)
            if exit_code:
                return exit_code
        elif args.command == "graph-audit":
            report = graph_audit(config)
            _json(report)
            if _json_field(report, "error_count", 0):
                return EXIT_VALIDATION
        elif args.command == "validate-note":
            report = validate_note_style_file(
                _path(args.content),
                args.title,
                raw_file=_path(args.raw_file) if args.raw_file else None,
            )
            if args.raw_file:
                artifact_report = validate_note_artifacts(
                    _path(args.content).read_text(encoding="utf-8"),
                    raw_file=_path(args.raw_file),
                    artifact_dir=config.artifact_dir,
                )
                report["artifact_validation"] = artifact_report
            _json(
                annotate_payload(
                    report,
                    phase="validate_note",
                    status="blocked" if report["errors"] else "completed",
                    blocked_reason="validation_errors" if report["errors"] else "",
                    next_action="Corrigir a nota e validar novamente." if report["errors"] else "",
                    required_inputs=["content", "title", "raw_file"],
                )
            )
            if report["errors"]:
                return EXIT_VALIDATION
        elif args.command == "fix-note":
            report = fix_note_style_file(
                _path(args.content),
                args.title,
                _path(args.output),
                raw_file=_path(args.raw_file) if args.raw_file else None,
            )
            _json(
                annotate_payload(
                    report,
                    phase="fix_note",
                    status="blocked" if report["errors"] else "completed",
                    blocked_reason="validation_errors" if report["errors"] else "",
                    next_action="Aplicar o output normalizado ou escalar para rewrite clínico se persistir." if not report["errors"] else "Revisar o conteúdo e rodar fix-note novamente.",
                    required_inputs=["content", "title", "output", "raw_file"],
                )
            )
            if report["errors"]:
                return EXIT_VALIDATION
        elif args.command == "validate-wiki":
            audit = validate_wiki_style(config.wiki_dir)
            error_count = int(audit.get("error_count", 0) or 0)
            warning_count = int(audit.get("warning_count", 0) or 0)
            audit = annotate_payload(
                audit,
                phase="validate_wiki",
                status="failed" if error_count else "completed_with_warnings" if warning_count else "completed",
                blocked_reason="validation_errors" if error_count else "",
                next_action=(
                    "Rodar /mednotes:fix-wiki --dry-run para obter o plano seguro de correção antes de aplicar mudanças."
                    if error_count
                    else "Revisar warnings de estilo antes de aplicar mudanças amplas na Wiki."
                    if warning_count
                    else ""
                ),
                required_inputs=["wiki_dir"],
                human_decision_required=False,
            )
            _json(audit)
            if error_count:
                return EXIT_VALIDATION
        elif args.command == "fix-wiki":
            if args.apply and args.dry_run:
                raise ValidationError("Use either --apply or --dry-run, not both")
            if args.apply_taxonomy and not args.apply:
                raise ValidationError("--apply-taxonomy requires --apply")
            effective_apply = args.apply and not args.dry_run
            backup_enabled = False
            heartbeat_label = "Reparo da Wiki" if effective_apply else "Conferência da Wiki"
            heartbeat_phase = "fix_wiki_apply" if effective_apply else "fix_wiki_preview"
            cpu_yield_settings = cooperative_cpu_yield_settings_from_env(default_enabled=True)
            with _workflow_heartbeat(heartbeat_label, workflow="/mednotes:fix-wiki", phase=heartbeat_phase):
                with cooperative_cpu_yield_scope(
                    enabled=cpu_yield_settings.enabled,
                    every=cpu_yield_settings.every,
                    seconds=cpu_yield_settings.seconds,
                ):
                    report = fix_wiki_health(
                        config,
                        apply=effective_apply,
                        backup=backup_enabled,
                        apply_taxonomy=args.apply_taxonomy,
                        vocabulary_reset=args.vocabulary_reset,
                        workflow_effect_executor=_workflow_effect_executor(config) if effective_apply else None,
                    )
            _json(fix_wiki_agent_stdout_report(report) if _agent_stdout_compact_enabled() else report)
            exit_code = fix_wiki_cli_exit_code(report)
            if exit_code != EXIT_OK:
                return exit_code
        elif args.command == "collect-style-rewrite-outputs":
            result = collect_style_rewrite_outputs(
                _path(args.plan),
                _path(args.manifest),
                work_id=_namespace_string(args, "work_id"),
            )
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
        elif args.command == "finalize-style-rewrite-output":
            result = finalize_style_rewrite_output(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                actual_model=_namespace_string(args, "actual_model"),
                provider=_namespace_string(args, "provider"),
                specialist_run_receipt_path=_path(args.specialist_run_receipt)
                if args.specialist_run_receipt
                else None,
            )
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
        elif args.command == "finalize-agy-specialist-task":
            result = finalize_agy_specialist_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                transcript_path=_path(args.transcript),
                runtime_log_path=_path(args.runtime_log) if args.runtime_log else None,
                requested_model=str(args.requested_model or "Gemini 3.1 Pro (High)"),
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
        elif args.command == "finalize-opencode-specialist-task":
            result = finalize_opencode_specialist_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                task_metadata_path=_path(args.task_metadata) if args.task_metadata else None,
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
        elif args.command == "finalize-opencode-architect-task":
            result = finalize_opencode_architect_task(
                plan_path=_path(args.plan),
                work_id=str(args.work_id),
                task_metadata_path=_path(args.task_metadata) if args.task_metadata else None,
                architect_output_path=_path(args.architect_output) if args.architect_output else None,
            )
            _json(result)
            if _json_field(result, "status") in {"blocked", "failed", "waiting_external"}:
                return EXIT_VALIDATION
        elif args.command == "apply-style-rewrite":
            result = _style_rewrite_apply_payload(args)
            written_target = _style_rewrite_written_target(result, args)
            if not args.dry_run and written_target is not None and _json_field(result, "status") != "blocked":
                trigger_context = _single_modified_note_context(
                    source_workflow="/mednotes:fix-wiki",
                    path=written_target,
                    config=config,
                    batch_id=str(written_target),
                )
                _record_linker_run_evidence(
                    result,
                    _auto_run_linker_from_trigger_context(
                        config,
                        trigger_context,
                        label="style-rewrite",
                        include_related_notes=True,
                    ),
                )
                result = finalize_style_rewrite_apply_receipt(result)
            _json(result)
            if _json_field(result, "status") == "blocked":
                return EXIT_VALIDATION
            if _json_field(result, "status") == "completed_with_link_blockers":
                return EXIT_VALIDATION
            validation = _dict_value(_json_field(result, "validation"))
            if _json_field(validation, "errors"):
                return EXIT_VALIDATION
        elif args.command == "apply-specialist-style-rewrite":
            result = _style_rewrite_atomic_apply_payload(args)
            written_target = _style_rewrite_written_target(result, args)
            if written_target is not None and _json_field(result, "status") != "blocked":
                trigger_context = _single_modified_note_context(
                    source_workflow="/mednotes:fix-wiki",
                    path=written_target,
                    config=config,
                    batch_id=str(written_target),
                )
                nested_apply = _json_field(result, "apply")
                if isinstance(nested_apply, dict):
                    nested_apply_payload = cast(JsonObject, JsonObjectAdapter.validate_python(nested_apply))
                    _record_linker_run_evidence(
                        cast(dict[str, object], nested_apply_payload),
                        _auto_run_linker_from_trigger_context(
                            config,
                            trigger_context,
                            label="style-rewrite",
                            include_related_notes=True,
                        ),
                    )
                    apply_receipt = finalize_style_rewrite_apply_receipt(nested_apply_payload)
                    result.update({"apply": apply_receipt})
                    result = finalize_style_rewrite_atomic_apply_result(result)
            _json(_style_rewrite_stdout_payload(result))
            if _json_field(result, "status") in {"blocked", "completed_with_link_blockers"}:
                return EXIT_VALIDATION
        elif args.command == "apply-note-merge":
            result = apply_note_merge(
                config,
                _path(args.plan),
                _path(args.content),
                dry_run=args.dry_run,
                backup=False,
            )
            trigger_context = _dict_value(_json_field(result, "link_trigger_context"))
            if trigger_context and not args.dry_run:
                _record_linker_run_evidence(
                    result,
                    _auto_run_linker_from_trigger_context(
                        config,
                        trigger_context,
                        label="note-merge",
                        include_related_notes=True,
                    ),
                )
                _refresh_receipt_if_present(result)
            _json(result)
            if _json_field(result, "status") == "failed":
                return EXIT_IO
            validation = _dict_value(_json_field(result, "validation"))
            if _json_field(validation, "errors"):
                return EXIT_VALIDATION
        elif args.command == "apply-atomicity-split":
            result = apply_atomicity_split_bundle(
                bundle_path=_path(args.bundle),
                wiki_dir=config.wiki_dir,
                backup=False,
                defer_linker=args.defer_linker,
                parent_batch_id=args.parent_batch_id,
                vocabulary_db_path=config.vocabulary_db_path,
            )
            trigger_context = _json_field(result, "link_trigger_context")
            if (
                isinstance(trigger_context, dict)
                and _json_field(result, "status") == "completed"
                and _json_field(result, "linker_status") != "deferred"
            ):
                _record_linker_run_evidence(
                    result,
                    _auto_run_linker_from_trigger_context(
                        config,
                        trigger_context,
                        label="atomicity-split",
                        include_related_notes=True,
                    ),
                )
                _refresh_receipt_if_present(result)
            _json(result)
            if _json_field(result, "status") == "failed":
                return EXIT_IO
            if _json_field(result, "status") == "blocked" or _json_field(result, "validation_errors"):
                return EXIT_VALIDATION
        elif args.command == "validate":
            _json(validate_config(config))
        else:  # pragma: no cover - argparse prevents this
            parser.print_help()
            return EXIT_USAGE
        return EXIT_OK
    except WikiPathResolutionError as exc:
        exit_code = exc.exit_code
        fsm_payload = _fsm_first_exception_payload(args, exc, exit_code)
        if fsm_payload is not None:
            emitted_payload = fsm_payload
            _json(emitted_payload)
            return exit_code
        emitted_payload = exc.payload(phase=f"{getattr(args, 'command', 'resolve')}_path_resolution")
        _json(emitted_payload)
        return exit_code
    except VaultGuardError as exc:
        exit_code = exc.exit_code
        fsm_payload = _fsm_first_exception_payload(args, exc, exit_code)
        if fsm_payload is not None:
            emitted_payload = fsm_payload
            _json(emitted_payload)
            return exit_code
        emitted_payload = exc.to_payload()
        _json(emitted_payload)
        return exit_code
    except MedOpsError as exc:
        exit_code = exc.exit_code
        fsm_payload = _fsm_first_exception_payload(args, exc, exit_code)
        if fsm_payload is not None:
            emitted_payload = fsm_payload
            _json(emitted_payload)
            return exit_code
        print(str(exc), file=sys.stderr)
        emitted_payload = _known_error_feedback_payload(args, exc, exit_code)
        if bool(getattr(args, "json", False)):
            _json(emitted_payload)
        return exit_code
    except Exception as exc:
        exit_code = EXIT_VALIDATION if exc.__class__.__name__.lower() == "integrityerror" else EXIT_IO
        fsm_payload = _fsm_first_exception_payload(args, exc, exit_code)
        if fsm_payload is not None:
            emitted_payload = fsm_payload
            _json(emitted_payload)
            return exit_code
        emitted_payload = _unexpected_error_feedback_payload(args, exc, exit_code)
        if bool(getattr(args, "json", False)):
            _json(emitted_payload)
        else:
            error_fields = _UnexpectedErrorPayloadCliFields.model_validate(emitted_payload)
            print(error_fields.diagnostic_context.traceback_summary or str(exc), file=sys.stderr)
        return exit_code
    finally:
        _record_feedback(args, emitted_payload, exit_code, started_at)


if __name__ == "__main__":
    raise SystemExit(main())
