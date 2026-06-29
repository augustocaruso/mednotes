"""Stable programmatic API for deterministic Wiki workflows.

Use this module as the public Python import surface for scripts outside the
``wiki`` package. Use ``wiki/cli.py`` only as the public terminal entrypoint.
Modules inside ``wiki`` may import each other as package internals.
"""
from __future__ import annotations

from mednotes.domains.wiki.batch_state import batch_state_from, canonical_json_hash, file_sha256
from mednotes.domains.wiki.capabilities.atomicity.atomicity import (
    ATOMICITY_SPLIT_BUNDLE_SCHEMA,
    ATOMICITY_SPLIT_PLAN_SCHEMA,
    ATOMICITY_SPLIT_RECEIPT_SCHEMA,
    apply_atomicity_split_bundle,
    build_atomicity_split_plan,
)
from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
    LinkWorkflowEffectAdapter as LinkWorkflowEffectAdapter,
)
from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
    RelatedNotesEffectAdapter as RelatedNotesEffectAdapter,
)
from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
    SpecialistModelEffectAdapter as SpecialistModelEffectAdapter,
)
from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
    WaitExternalEffectAdapter as WaitExternalEffectAdapter,
)
from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
    WikiSubworkflowEffectAdapter as WikiSubworkflowEffectAdapter,
)
from mednotes.domains.wiki.capabilities.graph.coverage import (
    RAW_COVERAGE_SCHEMA,
    validate_raw_coverage,
    validate_raw_coverage_structure,
)
from mednotes.domains.wiki.capabilities.graph.graph import main as graph_main
from mednotes.domains.wiki.capabilities.graph.graph_fixes import GRAPH_FIX_SCHEMA, fix_wiki_graph
from mednotes.domains.wiki.capabilities.hygiene.hygiene import (
    ROOT_HYGIENE_AUDIT_SCHEMA,
    WIKI_HYGIENE_CLEANUP_SCHEMA,
    WIKI_HYGIENE_SCHEMA,
    audit_user_root_hygiene,
    cleanup_wiki_hygiene,
    collect_wiki_hygiene,
)
from mednotes.domains.wiki.capabilities.markdown.markdown_node_runtime import (
    MarkdownNodeRuntimeUnavailable,
    ensure_markdown_node_runtime,
    markdown_node_runtime_status,
)
from mednotes.domains.wiki.capabilities.markdown.markdown_query import (
    MARKDOWN_QUERY_BLOCKED_REASON,
    MARKDOWN_QUERY_NEXT_ACTION,
    MarkdownDbChatMetadataProvider,
    MarkdownQueryUnavailable,
)
from mednotes.domains.wiki.capabilities.notes.artifacts import (
    ARTIFACT_HTML_MANIFEST_SCHEMA,
    ARTIFACT_HTML_VALIDATION_SCHEMA,
    chat_id_from_raw,
    discover_artifact_manifests,
    required_artifacts_for_raw,
    validate_artifact_batch,
    validate_note_artifacts,
)
from mednotes.domains.wiki.capabilities.notes.canonical_merge import CANONICAL_MERGE_APPLY_SCHEMA, apply_canonical_merge
from mednotes.domains.wiki.capabilities.notes.note_merge import apply_note_merge
from mednotes.domains.wiki.capabilities.notes.note_plan import (
    ATTACH_TO_PLANNED_MEANING_ACTION,
    NEEDS_CONTEXT_ACTION,
    PLANNED_MEANING_ACTION,
    TRIAGE_NOTE_PLAN_SCHEMA,
    TRIAGE_NOTE_PLAN_V2_SCHEMA,
    load_triage_note_plan,
    normalize_triage_note_plan_v2,
    note_plan_hash,
    parse_triage_note_plan,
    serialize_triage_note_plan,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import (
    atomic_write_text,
    covered_raw_chat_index,
    create_backup,
    list_by_status,
    list_raw_files,
    mutate_raw_frontmatter,
    parse_frontmatter,
    prune_backup_files,
    raw_summary,
    read_note_meta,
    split_frontmatter,
    update_frontmatter,
)
from mednotes.domains.wiki.capabilities.publish.publish import (
    plan_publish_batch,
    publish_batch,
    resolve_collision,
    stage_note,
    taxonomy_new_leaf_authorization_for_manifest,
    taxonomy_new_leaf_authorization_from_plan,
    write_new_note,
)
from mednotes.domains.wiki.capabilities.publish.publish import (
    publish_batch_operation_result as publish_batch_operation_result,
)
from mednotes.domains.wiki.capabilities.publish.publish_receipts import (
    PUBLISH_DRY_RUN_RECEIPTS_SCHEMA,
    clear_publish_dry_run,
    publish_receipts_path,
    record_publish_dry_run,
    require_publish_dry_run,
)
from mednotes.domains.wiki.capabilities.publish.publish_recovery import (
    PUBLISH_STATE_DIAGNOSIS_SCHEMA,
    diagnose_publish_state,
)
from mednotes.domains.wiki.capabilities.quality.agent_behavior_corpus import (
    AGENT_BEHAVIOR_CASE_DRAFT_REPORT_SCHEMA,
    AGENT_BEHAVIOR_CASE_DRAFT_SCHEMA,
    AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA,
    AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA,
    AGENT_BEHAVIOR_CORPUS_SCHEMA,
    agent_behavior_baseline_paths,
    evaluate_agent_behavior_corpus,
    suggest_agent_behavior_cases_from_evidence,
    suggest_agent_behavior_cases_from_telemetry,
    validate_agent_behavior_report_path,
)
from mednotes.domains.wiki.capabilities.quality.agent_report_validation import validate_agent_run_report
from mednotes.domains.wiki.capabilities.quality.agent_run_audit import audit_agent_transcript
from mednotes.domains.wiki.capabilities.quality.architect_prompt_eval import (
    ARCHITECT_PROMPT_EVAL_SCHEMA,
    evaluate_architect_prompt_outputs,
)
from mednotes.domains.wiki.capabilities.quality.body_linker_eval import (
    BODY_LINKER_EVAL_REPORT_SCHEMA,
    BODY_LINKER_EVAL_SUITE_SCHEMA,
    evaluate_body_linker_cases,
)
from mednotes.domains.wiki.capabilities.quality.curator_output_validation import validate_curator_batch_outputs
from mednotes.domains.wiki.capabilities.quality.curator_prompt_eval import (
    CURATOR_PROMPT_EVAL_SCHEMA,
    CURATOR_PROMPT_EXPECTATIONS_SCHEMA,
    CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA,
    build_curator_prompt_expectations_template,
    canonical_payload_hash,
    evaluate_curator_prompt_outputs,
    load_curator_prompt_expectations,
    promote_curator_prompt_baseline,
)
from mednotes.domains.wiki.capabilities.quality.triager_prompt_eval import (
    TRIAGER_EVAL_RETRY_NEXT_ACTION,
    TRIAGER_PROMPT_EVAL_SCHEMA,
    TRIAGER_PROMPT_EXPECTATIONS_SCHEMA,
    evaluate_triager_prompt_output,
    load_triager_prompt_expectations,
    validate_triager_prompt_eval_for_note_plan,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    DEFAULT_MAX_LINKS as DEFAULT_RELATED_NOTES_MAX_LINKS,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    DEFAULT_MIN_SCORE as DEFAULT_RELATED_NOTES_MIN_SCORE,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    RELATED_NOTES_EXPORT_SCHEMA,
    RELATED_NOTES_SYNC_RECEIPT_SCHEMA,
    RELATED_NOTES_SYNC_SCHEMA,
    recover_related_notes_export,
    sync_related_notes,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    recover_related_notes_export_operation_result as recover_related_notes_export_operation_result,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    sync_related_notes_operation_result as sync_related_notes_operation_result,
)
from mednotes.domains.wiki.capabilities.specialist.specialist_task_runner import (
    finalize_agy_specialist_task,
    finalize_opencode_architect_task,
    finalize_opencode_specialist_task,
)
from mednotes.domains.wiki.capabilities.style.style import (
    apply_style_rewrite,
    apply_style_rewrite_from_manifest,
    collect_style_rewrite_outputs,
    finalize_collect_apply_style_rewrite,
    finalize_style_rewrite_apply_receipt,
    finalize_style_rewrite_atomic_apply_result,
    finalize_style_rewrite_output,
    fix_note_style_file,
    fix_wiki_style,
    style_rewrite_manifest_required_receipt,
    validate_note_style_file,
    validate_wiki_note_contract,
    validate_wiki_style,
)
from mednotes.domains.wiki.capabilities.subagents.agents import CANONICAL_MERGE_PLAN_SCHEMA, plan_subagents
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy import (
    CANONICAL_AREA_ALIASES,
    CANONICAL_TAXONOMY,
    CANONICAL_TAXONOMY_ALIASES,
    CANONICAL_TAXONOMY_POLICY,
    TAXONOMY_STATUS_SCHEMA,
    TaxonomyAreaPolicy,
    TaxonomyDecisionRequired,
    TaxonomyResolution,
    TaxonomySpecialtyPolicy,
    _write_json_atomic,
    apply_taxonomy_migration,
    canonical_taxonomy_invariants,
    canonical_taxonomy_tree,
    normalize_taxonomy,
    render_taxonomy_status_markdown,
    resolve_target_for_note,
    resolve_taxonomy,
    rollback_taxonomy_migration,
    safe_title,
    target_for_note,
    taxonomy_audit,
    taxonomy_migration_plan,
    taxonomy_status,
    taxonomy_tree,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import (
    CURATOR_PROMPT_IDENTITY_SCHEMA,
    VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA,
    VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA,
    VOCABULARY_CURATOR_BATCH_RECEIPT_SCHEMA,
    agent_output_ignored_notice,
    apply_curator_batch_outputs,
    build_curator_prompt_identity,
    build_vocabulary_curator_batch_plan,
    collect_curator_outputs,
    curator_plan_hash,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_ingestion import (
    INGESTION_RECEIPT_SCHEMA,
    INGESTION_SCHEMA,
    apply_semantic_ingestion,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_recovery import (
    VOCABULARY_RECOVERY_PLAN_SCHEMA,
    VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
    VOCABULARY_STATUS_SCHEMA,
    apply_vocabulary_recovery_plan,
    build_vocabulary_recovery_plan,
    diagnose_vocabulary_status,
    vocabulary_status,
)
from mednotes.domains.wiki.common import (
    BLOCKER_RESOLUTION_SCHEMA,
    EXIT_IO,
    EXIT_LINKER,
    EXIT_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    MIGRATION_PLAN_SCHEMA,
    MIGRATION_RECEIPT_SCHEMA,
    NOTE_MERGE_APPLY_SCHEMA,
    NOTE_MERGE_PLAN_SCHEMA,
    SUBAGENT_PLAN_SCHEMA,
    WIKI_CLI_RELPATH,
    WIKI_HEALTH_FIX_SCHEMA,
    CollisionError,
    FileWriteError,
    MedOpsError,
    MissingPathError,
    ValidationError,
    _json,
    _now_iso,
    wiki_cli_relative_command,
)
from mednotes.domains.wiki.config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_RAW_DIR,
    DEFAULT_VOCABULARY_DB_PATH,
    DEFAULT_WIKI_DIR,
    MedConfig,
    WikiPathResolutionError,
    _path,
    resolve_config,
    validate_config,
)
from mednotes.domains.wiki.contracts.agent_run_audit import AuditWorkflow as AuditWorkflow
from mednotes.domains.wiki.contracts.agents import NextSpecialistTask as NextSpecialistTask
from mednotes.domains.wiki.contracts.agents import PlanOutputReceipt as PlanOutputReceipt
from mednotes.domains.wiki.contracts.agents import SpecialistContinuationWorkItem as SpecialistContinuationWorkItem
from mednotes.domains.wiki.contracts.agents import SubagentBatchPlan as SubagentBatchPlan
from mednotes.domains.wiki.contracts.effect_payloads import LinkWorkflowRunEffectPayload as LinkWorkflowRunEffectPayload
from mednotes.domains.wiki.contracts.effect_payloads import (
    RelatedNotesExportEffectPayload as RelatedNotesExportEffectPayload,
)
from mednotes.domains.wiki.contracts.happy_path import HappyPathRunMetrics as HappyPathRunMetrics
from mednotes.domains.wiki.contracts.happy_path import happy_path_round_metrics as happy_path_round_metrics
from mednotes.domains.wiki.contracts.paths import WikiPathResolutionPayload as WikiPathResolutionPayload
from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedSyncResult
from mednotes.domains.wiki.contracts.specialist import SpecialistNextApplyStep as SpecialistNextApplyStep
from mednotes.domains.wiki.contracts.status import StatusSnapshot as StatusSnapshot
from mednotes.domains.wiki.contracts.style_rewrite import (
    StyleRewriteAtomicApplyAgentStdout as StyleRewriteAtomicApplyAgentStdout,
)
from mednotes.domains.wiki.contracts.style_rewrite import (
    StyleRewriteAtomicApplyResult as StyleRewriteAtomicApplyResult,
)
from mednotes.domains.wiki.contracts.workflow_guardrails import (
    LINK_REQUIRED_INPUTS,
    PUBLISH_REQUIRED_INPUTS,
    SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON,
    annotate_payload,
    error_context,
    harden_operational_payload,
    require_subagent_output_contract,
    subagent_output_contract_errors,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import attach_human_decision_packet
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_fsm import (
    build_fix_wiki_fsm_result,
    fix_wiki_cli_exit_code,
    fix_wiki_fsm_facts_from_runtime,
)
from mednotes.domains.wiki.flows.fix_wiki.health import fix_wiki_agent_stdout_report as fix_wiki_agent_stdout_report
from mednotes.domains.wiki.flows.fix_wiki.health import fix_wiki_health
from mednotes.domains.wiki.flows.link.link_fsm import (
    build_link_fsm_result,
    link_cli_exit_code,
)
from mednotes.domains.wiki.flows.link.link_runtime_result import (
    LinkerRunResult,
    link_fsm_facts_from_linker_result,
)
from mednotes.domains.wiki.flows.link.link_triggers import LINK_TRIGGER_CONTEXT_SCHEMA, write_trigger_context
from mednotes.domains.wiki.flows.link.linking import (
    LINK_DIAGNOSIS_SCHEMA,
    LINK_RUN_RECEIPT_SCHEMA,
    LINK_RUN_SCHEMA,
    apply_link_diagnosis,
    diagnose_links,
    graph_audit,
    run_linker,
)
from mednotes.domains.wiki.flows.link.related_notes_fsm import (
    LINK_RELATED_SCHEMA,
    link_related_cli_exit_code,
    link_related_fsm_payload_from_sync_result,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_fsm import (
    PROCESS_CHATS_SCHEMA,
    ProcessChatsFsmFacts,
    ProcessChatsLinkerRun,
    ProcessChatsOperationalSummary,
    ProcessChatsPublishOperationResult,
    build_process_chats_fsm_result,
    process_chats_cli_exit_code,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_machine import (
    NoPendingRawChatsEvent,
    NoTriagedRawChatsEvent,
    ProcessChatsErrorContext,
    ProcessChatsPublishRuntimeObservation,
    ProcessChatsState,
    RollbackFailureRecordedEvent,
    TriagedRawChatsAvailableEvent,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_runtime_result import (
    process_chats_fsm_payload_from_publish_result,
)
from mednotes.domains.wiki.performance import (
    cooperative_cpu_yield_scope,
    cooperative_cpu_yield_settings_from_env,
)
from mednotes.kernel.agent_directive import AgentDirective as AgentDirective
from mednotes.kernel.base import JsonArrayAdapter, JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.effect_executor import WorkflowEffectExecutor as WorkflowEffectExecutor
from mednotes.kernel.effects import WorkflowEffect as WorkflowEffect
from mednotes.kernel.effects import WorkflowEffectKind as WorkflowEffectKind
from mednotes.kernel.effects import WorkflowEffectResult as WorkflowEffectResult
from mednotes.kernel.effects import WorkflowEffectStatus as WorkflowEffectStatus
from mednotes.kernel.progress import WorkflowProgressEvent as WorkflowProgressEvent
from mednotes.kernel.progress import WorkflowProgressEventType as WorkflowProgressEventType
from mednotes.kernel.progress import WorkflowProgressStatus as WorkflowProgressStatus
from mednotes.kernel.progress import build_progress_view_model as build_progress_view_model
from mednotes.kernel.progress import fold_progress_events as fold_progress_events
from mednotes.kernel.workflow import VersionControlSafety as VersionControlSafety
from mednotes.platform.backup_policy import (
    BACKUP_CLEANUP_SCHEMA,
    DEFAULT_BACKUP_POLICY,
    BackupPolicy,
    archive_legacy_backups,
    collect_legacy_backup_candidates,
)

__all__ = [
    "CANONICAL_TAXONOMY",
    "CANONICAL_AREA_ALIASES",
    "CANONICAL_TAXONOMY_ALIASES",
    "CANONICAL_TAXONOMY_POLICY",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_RAW_DIR",
    "DEFAULT_VOCABULARY_DB_PATH",
    "DEFAULT_WIKI_DIR",
    "EXIT_IO",
    "EXIT_LINKER",
    "EXIT_MISSING",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
    "LINK_REQUIRED_INPUTS",
    "PUBLISH_REQUIRED_INPUTS",
    "SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON",
    "harden_operational_payload",
    "NOTE_MERGE_APPLY_SCHEMA",
    "NOTE_MERGE_PLAN_SCHEMA",
    "MIGRATION_PLAN_SCHEMA",
    "MIGRATION_RECEIPT_SCHEMA",
    "PUBLISH_DRY_RUN_RECEIPTS_SCHEMA",
    "PUBLISH_STATE_DIAGNOSIS_SCHEMA",
    "PROCESS_CHATS_SCHEMA",
    "ProcessChatsLinkerRun",
    "ProcessChatsPublishRuntimeObservation",
    "ProcessChatsPublishOperationResult",
    "NextSpecialistTask",
    "PlanOutputReceipt",
    "SpecialistNextApplyStep",
    "StatusSnapshot",
    "StyleRewriteAtomicApplyResult",
    "StyleRewriteAtomicApplyAgentStdout",
    "SubagentBatchPlan",
    "WikiPathResolutionPayload",
    "RAW_COVERAGE_SCHEMA",
    "RELATED_NOTES_EXPORT_SCHEMA",
    "LINK_DIAGNOSIS_SCHEMA",
    "LINK_RUN_SCHEMA",
    "LINK_RUN_RECEIPT_SCHEMA",
    "LINK_TRIGGER_CONTEXT_SCHEMA",
    "LinkerRunResult",
    "build_link_fsm_result",
    "link_cli_exit_code",
    "link_fsm_facts_from_linker_result",
    "process_chats_cli_exit_code",
    "process_chats_fsm_payload_from_publish_result",
    "link_related_cli_exit_code",
    "link_related_fsm_payload_from_sync_result",
    "MARKDOWN_QUERY_BLOCKED_REASON",
    "MARKDOWN_QUERY_NEXT_ACTION",
    "RELATED_NOTES_SYNC_RECEIPT_SCHEMA",
    "RELATED_NOTES_SYNC_SCHEMA",
    "LINK_RELATED_SCHEMA",
    "LinkRelatedSyncResult",
    "ARTIFACT_HTML_MANIFEST_SCHEMA",
    "ARTIFACT_HTML_VALIDATION_SCHEMA",
    "ATOMICITY_SPLIT_BUNDLE_SCHEMA",
    "ATOMICITY_SPLIT_PLAN_SCHEMA",
    "ATOMICITY_SPLIT_RECEIPT_SCHEMA",
    "BODY_LINKER_EVAL_REPORT_SCHEMA",
    "BODY_LINKER_EVAL_SUITE_SCHEMA",
    "ATTACH_TO_PLANNED_MEANING_ACTION",
    "NEEDS_CONTEXT_ACTION",
    "PLANNED_MEANING_ACTION",
    "TRIAGE_NOTE_PLAN_SCHEMA",
    "TRIAGE_NOTE_PLAN_V2_SCHEMA",
    "TRIAGER_PROMPT_EVAL_SCHEMA",
    "TRIAGER_PROMPT_EXPECTATIONS_SCHEMA",
    "SUBAGENT_PLAN_SCHEMA",
    "TAXONOMY_STATUS_SCHEMA",
    "BLOCKER_RESOLUTION_SCHEMA",
    "CANONICAL_MERGE_PLAN_SCHEMA",
    "CANONICAL_MERGE_APPLY_SCHEMA",
    "WIKI_CLI_RELPATH",
    "WIKI_HEALTH_FIX_SCHEMA",
    "WIKI_HYGIENE_CLEANUP_SCHEMA",
    "WIKI_HYGIENE_SCHEMA",
    "ROOT_HYGIENE_AUDIT_SCHEMA",
    "GRAPH_FIX_SCHEMA",
    "INGESTION_RECEIPT_SCHEMA",
    "INGESTION_SCHEMA",
    "VOCABULARY_RECOVERY_PLAN_SCHEMA",
    "VOCABULARY_RECOVERY_RECEIPT_SCHEMA",
    "VOCABULARY_STATUS_SCHEMA",
    "VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA",
    "VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA",
    "VOCABULARY_CURATOR_BATCH_RECEIPT_SCHEMA",
    "ARCHITECT_PROMPT_EVAL_SCHEMA",
    "CURATOR_PROMPT_IDENTITY_SCHEMA",
    "CURATOR_PROMPT_EVAL_SCHEMA",
    "CURATOR_PROMPT_EXPECTATIONS_SCHEMA",
    "CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA",
    "AGENT_BEHAVIOR_CORPUS_SCHEMA",
    "AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA",
    "AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA",
    "AGENT_BEHAVIOR_CASE_DRAFT_SCHEMA",
    "AGENT_BEHAVIOR_CASE_DRAFT_REPORT_SCHEMA",
    "BACKUP_CLEANUP_SCHEMA",
    "BackupPolicy",
    "DEFAULT_BACKUP_POLICY",
    "DEFAULT_RELATED_NOTES_MAX_LINKS",
    "DEFAULT_RELATED_NOTES_MIN_SCORE",
    "archive_legacy_backups",
    "annotate_payload",
    "attach_human_decision_packet",
    "apply_link_diagnosis",
    "audit_agent_transcript",
    "audit_user_root_hygiene",
    "collect_legacy_backup_candidates",
    "CollisionError",
    "FileWriteError",
    "MedConfig",
    "MedOpsError",
    "MissingPathError",
    "MarkdownDbChatMetadataProvider",
    "MarkdownNodeRuntimeUnavailable",
    "MarkdownQueryUnavailable",
    "TaxonomyAreaPolicy",
    "TaxonomyResolution",
    "TaxonomyDecisionRequired",
    "TaxonomySpecialtyPolicy",
    "ValidationError",
    "VersionControlSafety",
    "JsonArrayAdapter",
    "JsonObject",
    "JsonObjectAdapter",
    "JsonValue",
    "WikiPathResolutionError",
    "_json",
    "_now_iso",
    "wiki_cli_relative_command",
    "_path",
    "_write_json_atomic",
    "apply_curator_batch_outputs",
    "agent_output_ignored_notice",
    "apply_vocabulary_recovery_plan",
    "apply_atomicity_split_bundle",
    "apply_canonical_merge",
    "apply_note_merge",
    "apply_style_rewrite_from_manifest",
    "build_atomicity_split_plan",
    "apply_semantic_ingestion",
    "apply_style_rewrite",
    "apply_taxonomy_migration",
    "apply_vocabulary_recovery_plan",
    "atomic_write_text",
    "batch_state_from",
    "build_vocabulary_curator_batch_plan",
    "build_curator_prompt_identity",
    "collect_curator_outputs",
    "collect_style_rewrite_outputs",
    "cooperative_cpu_yield_scope",
    "cooperative_cpu_yield_settings_from_env",
    "finalize_collect_apply_style_rewrite",
    "finalize_style_rewrite_atomic_apply_result",
    "finalize_style_rewrite_apply_receipt",
    "finalize_style_rewrite_output",
    "finalize_agy_specialist_task",
    "finalize_opencode_architect_task",
    "finalize_opencode_specialist_task",
    "curator_plan_hash",
    "build_curator_prompt_expectations_template",
    "build_vocabulary_recovery_plan",
    "canonical_taxonomy_invariants",
    "canonical_taxonomy_tree",
    "canonical_json_hash",
    "canonical_payload_hash",
    "chat_id_from_raw",
    "clear_publish_dry_run",
    "covered_raw_chat_index",
    "cleanup_wiki_hygiene",
    "collect_wiki_hygiene",
    "create_backup",
    "discover_artifact_manifests",
    "diagnose_publish_state",
    "diagnose_vocabulary_status",
    "diagnose_links",
    "error_context",
    "evaluate_body_linker_cases",
    "evaluate_architect_prompt_outputs",
    "evaluate_curator_prompt_outputs",
    "evaluate_triager_prompt_output",
    "evaluate_agent_behavior_corpus",
    "agent_behavior_baseline_paths",
    "validate_agent_behavior_report_path",
    "validate_agent_run_report",
    "ensure_markdown_node_runtime",
    "suggest_agent_behavior_cases_from_evidence",
    "suggest_agent_behavior_cases_from_telemetry",
    "load_curator_prompt_expectations",
    "promote_curator_prompt_baseline",
    "fix_note_style_file",
    "fix_wiki_health",
    "fix_wiki_graph",
    "fix_wiki_cli_exit_code",
    "file_sha256",
    "fix_wiki_style",
    "graph_audit",
    "graph_main",
    "list_by_status",
    "list_raw_files",
    "load_triage_note_plan",
    "normalize_triage_note_plan_v2",
    "load_triager_prompt_expectations",
    "load_curator_prompt_expectations",
    "markdown_node_runtime_status",
    "require_subagent_output_contract",
    "subagent_output_contract_errors",
    "mutate_raw_frontmatter",
    "normalize_taxonomy",
    "note_plan_hash",
    "parse_frontmatter",
    "parse_triage_note_plan",
    "ProcessChatsFsmFacts",
    "ProcessChatsOperationalSummary",
    "ProcessChatsState",
    "plan_publish_batch",
    "plan_subagents",
    "publish_batch",
    "publish_receipts_path",
    "taxonomy_new_leaf_authorization_for_manifest",
    "taxonomy_new_leaf_authorization_from_plan",
    "prune_backup_files",
    "raw_summary",
    "read_note_meta",
    "record_publish_dry_run",
    "recover_related_notes_export",
    "required_artifacts_for_raw",
    "resolve_collision",
    "resolve_config",
    "resolve_target_for_note",
    "resolve_taxonomy",
    "render_taxonomy_status_markdown",
    "require_publish_dry_run",
    "rollback_taxonomy_migration",
    "run_linker",
    "safe_title",
    "serialize_triage_note_plan",
    "split_frontmatter",
    "stage_note",
    "style_rewrite_manifest_required_receipt",
    "sync_related_notes",
    "target_for_note",
    "taxonomy_audit",
    "taxonomy_migration_plan",
    "taxonomy_status",
    "taxonomy_tree",
    "TRIAGER_EVAL_RETRY_NEXT_ACTION",
    "NoPendingRawChatsEvent",
    "NoTriagedRawChatsEvent",
    "ProcessChatsErrorContext",
    "RollbackFailureRecordedEvent",
    "TriagedRawChatsAvailableEvent",
    "update_frontmatter",
    "validate_config",
    "validate_curator_batch_outputs",
    "validate_artifact_batch",
    "validate_note_style_file",
    "validate_note_artifacts",
    "validate_raw_coverage",
    "validate_raw_coverage_structure",
    "validate_triager_prompt_eval_for_note_plan",
    "validate_wiki_note_contract",
    "validate_wiki_style",
    "vocabulary_status",
    "write_new_note",
    "write_trigger_context",
    "build_process_chats_fsm_result",
    "build_fix_wiki_fsm_result",
    "fix_wiki_fsm_facts_from_runtime",
]
