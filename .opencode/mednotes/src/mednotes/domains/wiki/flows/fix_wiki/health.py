"""High-level Wiki health runtime/composition facade (`fix-wiki`).

This module is intentionally the outer orchestration shell around the typed
FSM. Keep the boundaries explicit:

1. collect/adapt runtime evidence from filesystem, graph, linker and reports;
2. build typed FSM facts before any public status/report decision;
3. execute WorkflowEffect adapters through the official adapter layer;
4. write receipts/reports/feedback after the FSM projection is built.

It must not become a second source of workflow state; policy remains in the
FixWikiMachine/FSM projector and adapters only execute declared effects.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import ConfigDict, Field, field_validator

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.atomicity.atomicity import build_atomicity_split_plan
from mednotes.domains.wiki.capabilities.effects.fix_wiki_runtime_adapters import (
    execute_link_subworkflow,
    execute_related_notes_export_recovery,
    fix_wiki_version_control_safety,
    version_control_mutation_summary,
)
from mednotes.domains.wiki.capabilities.hygiene.hygiene import cleanup_wiki_hygiene, collect_wiki_hygiene
from mednotes.domains.wiki.capabilities.markdown.markdown_query import (
    MarkdownQueryUnavailable,
    ensure_markdown_query_available,
)
from mednotes.domains.wiki.capabilities.notes import note_style
from mednotes.domains.wiki.capabilities.notes.note_style.models import STYLE_AUDIT_SCHEMA
from mednotes.domains.wiki.capabilities.notes.provenance import ChatProvenance, classify_note_provenance
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.notes.sources_backfill import apply_sources_backfill, audit_sources_backfill
from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    RELATED_NOTES_REQUIRED_INPUTS,
    cleanup_invalid_related_notes_links,
)
from mednotes.domains.wiki.capabilities.related_notes.related_notes_headless import (
    migrate_related_notes_clean_v1_table_hashes,
)
from mednotes.domains.wiki.capabilities.style.style import (
    _downgrade_invalid_root_note_report,
    _requires_style_rewrite,
    fix_wiki_style,
    validate_wiki_style,
)
from mednotes.domains.wiki.capabilities.subagents.agents import (
    DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY,
    configured_subagent_max_concurrency,
    plan_subagents,
)
from mednotes.domains.wiki.capabilities.vocabulary.alias_projection import (
    apply_alias_projection_plan,
    build_alias_projection_plan,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy import (
    apply_taxonomy_migration,
    taxonomy_audit,
    taxonomy_migration_plan,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_bootstrap import resolve_vocabulary_bootstrap
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import build_vocabulary_curator_batch_plan
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import load_vocabulary_map_diagnosis
from mednotes.domains.wiki.common import (
    BLOCKER_RESOLUTION_SCHEMA,
    FileWriteError,
    wiki_cli_base_command,
    wiki_cli_relative_command,
)
from mednotes.domains.wiki.config import MedConfig, _path, _user_state_dir
from mednotes.domains.wiki.contracts.link_runtime_artifact import normalize_link_runtime_artifact
from mednotes.domains.wiki.contracts.related_notes_runtime import LinkRelatedSyncResult, RelatedNotesRecoveryState
from mednotes.domains.wiki.contracts.style_rewrite import FixWikiStyleResult
from mednotes.domains.wiki.contracts.workflow_blockers import blocker_entry
from mednotes.domains.wiki.contracts.workflow_guardrails import (
    FIX_WIKI_REQUIRED_INPUTS,
    error_context,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import WorkflowDecision
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_context_packets import write_context_packets
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_decision_projection import (
    project_fix_wiki_human_decision_packets,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_effects import (
    effect_result_stops_fix_wiki_execution,
    missing_fix_wiki_effect_adapter_is_optional,
    pending_effect_payloads_from_fix_wiki_runtime_source,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_effects import (
    facts_after_effect_results as _facts_after_effect_results,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_fsm import (
    FixWikiFsmFacts,
    assert_fix_wiki_fsm_payload,
    build_fix_wiki_fsm_result,
    fix_wiki_fsm_facts_from_runtime,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_plan import (
    build_fix_wiki_plan,
    collect_fix_wiki_snapshot_files,
    fix_wiki_snapshot_hash,
    validate_fix_wiki_plan_snapshot,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_primary_objective import fix_wiki_primary_objective_summary
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_problem import build_problem
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_receipt_evidence import build_fix_wiki_receipt_evidence
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_user_report import write_fix_wiki_user_report_v2
from mednotes.domains.wiki.flows.link.link_triggers import LINK_TRIGGER_CONTEXT_SCHEMA, write_trigger_context
from mednotes.domains.wiki.flows.link.linking import (
    _vocabulary_repair_needed,
    graph_audit,
    related_notes_sync_blocked,
    repair_vocabulary_semantics_for_link,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effect_executor import MissingWorkflowEffectAdapter, WorkflowEffectExecutor
from mednotes.kernel.workflow import HumanDecisionPacket
from mednotes.platform.feedback.operational_contract import PACKAGED_AGENT_TEMPLATE_CONTRACT


class FixWikiSummaryReason(StrEnum):
    DRY_RUN_CLEAN = "dry_run_clean"
    DRY_RUN_WARNINGS = "dry_run_warnings"
    DRY_RUN_BLOCKED = "dry_run_blocked"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    TAXONOMY_CONFIRMATION = "taxonomy_confirmation"
    HUMAN_DECISION = "human_decision"
    STYLE_REWRITE_REQUIRED = "style_rewrite_required"
    GRAPH_BLOCKED = "graph_blocked"
    RELATED_NOTES_BLOCKED = "related_notes_blocked"
    LINKER_BLOCKED = "linker_blocked"
    TAXONOMY_ACTION_REQUIRED = "taxonomy_action_required"
    CHANGED_WITH_UNSPECIFIED_BLOCKER = "changed_with_unspecified_blocker"
    OPERATIONAL_BLOCKER = "operational_blocker"


class _StyleRewriteWorkItemFields(ContractModel):
    """Typed lens for the style-rewrite item that becomes a specialist effect."""

    model_config = ConfigDict(extra="ignore")

    work_id: str = ""
    target_path: str = ""
    target_hash_before: str = ""
    agent: str = ""
    item_type: str = ""
    owner_key: str = ""
    title: str = ""
    rewrite_prompt: str = ""
    model_policy: str = ""
    required_model_tier: str = ""
    preferred_model_tier: str = ""
    phase: str = ""
    temp_dir: str = ""
    temp_output: str = ""
    output_attestation_path: str = ""
    specialist_task_run_receipt_path: str = ""
    subagent_output_contract: JsonObject = Field(default_factory=dict)
    context_docs: JsonObject = Field(default_factory=dict)


class _AgentWorkspaceContextDocsFields(ContractModel):
    """Workspace files the official specialist runner may read without discovery."""

    model_config = ConfigDict(extra="ignore")

    required_read_files: list[str] = Field(default_factory=list)
    forbidden_discovery_roots: list[str] = Field(default_factory=list)


class _HumanDecisionPacketFields(ContractModel):
    """Minimal human-decision lens used by report helpers before full packet projection."""

    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    resume_action: str = ""


class _LinkArtifactStatusFields(ContractModel):
    """Typed lens for link artifacts before they are merged into fix-wiki facts."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    blocked_reason: str = ""
    blockers: list[JsonObject] = Field(default_factory=list)


class _AgentStdoutStateMachineSummaryFields(ContractModel):
    """Public StateChart slice copied into the compact agent stdout report."""

    model_config = ConfigDict(extra="ignore")

    workflow: object | None = None
    run_id: object | None = None
    current_state: object | None = None
    current_category: object | None = None
    metadata: object | None = None


class _AgentStdoutProgressCountsFields(ContractModel):
    """Known progress counters that are safe to keep inline in stdout."""

    model_config = ConfigDict(extra="ignore")

    mutated_files: object | None = None
    written_files: object | None = None
    warnings: object | None = None
    blocked_items: object | None = None
    remaining_items: object | None = None
    planned_items: object | None = None
    processed_items: object | None = None


class _AgentStdoutProgressViewFields(ContractModel):
    """Compact progress view derived from the canonical FSM progress model."""

    model_config = ConfigDict(extra="ignore")

    workflow: object | None = None
    run_id: object | None = None
    state: object | None = None
    phase: object | None = None
    status: object | None = None
    mode: object | None = None
    percent: object | None = None
    terminal: object | None = None
    successful: object | None = None
    current: object | None = None
    total: object | None = None
    count_label: object | None = None
    message: object | None = None
    user_action: object | None = None
    resume_action: object | None = None
    resume_supported: object | None = None
    can_continue_now: object | None = None
    counts: _AgentStdoutProgressCountsFields = Field(default_factory=_AgentStdoutProgressCountsFields)

    @field_validator("counts", mode="before")
    @classmethod
    def _coerce_counts(cls, value: object) -> _AgentStdoutProgressCountsFields:
        return _AgentStdoutProgressCountsFields.model_validate(_json_object_or_empty(value))


class _AgentStdoutApplyFields(ContractModel):
    """Small apply/mode summary for diagnostic stdout."""

    model_config = ConfigDict(extra="ignore")

    requested_apply: object | None = None
    effective_apply: object | None = None
    apply_taxonomy: object | None = None


class _AgentStdoutReceiptFields(ContractModel):
    """Receipt subset rendered from the canonical FSM receipt."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    workflow: object | None = None
    run_id: object | None = None
    status: object | None = None
    mutated: object | None = None
    next_action: object | None = None
    human_decision_required: object | None = None
    artifact_count: object | None = None
    phase_outcome_count: object | None = None
    phase_receipt_count: object | None = None


class _AgentStdoutPublicReportFields(ContractModel):
    """Human-safe public report slice; long lines stay in report artifacts."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    audience: object | None = None
    workflow: object | None = None
    headline: object | None = None
    lines: list[object] = Field(default_factory=list)


class _AgentStdoutReportDetailsFields(ContractModel):
    """Known details that remain useful in a compact agent transcript."""

    model_config = ConfigDict(extra="ignore")

    primary_objective_summary: object | None = None


class _AgentStdoutReportsFields(ContractModel):
    """Typed reports lens for stdout compaction."""

    model_config = ConfigDict(extra="ignore")

    summary: object | None = None
    public_report: object | None = None
    details: _AgentStdoutReportDetailsFields = Field(default_factory=_AgentStdoutReportDetailsFields)

    @field_validator("details", mode="before")
    @classmethod
    def _coerce_details(cls, value: object) -> _AgentStdoutReportDetailsFields:
        return _AgentStdoutReportDetailsFields.model_validate(_json_object_or_empty(value))


class _AgentStdoutDirectiveControlFields(ContractModel):
    """Control subset of agent_directive; effects stay executable but compact."""

    model_config = ConfigDict(extra="ignore")

    status: object | None = None
    state: object | None = None
    phase: object | None = None
    reason: object | None = None
    capabilities: object | None = None
    effects: list[object] = Field(default_factory=list)
    blockers: object | None = None
    resume: object | None = None
    report: object | None = None
    limits: object | None = None


class _AgentStdoutDirectiveFields(ContractModel):
    """Typed lens for the canonical FSM -> agent directive."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    workflow: object | None = None
    run_id: object | None = None
    control: _AgentStdoutDirectiveControlFields = Field(default_factory=_AgentStdoutDirectiveControlFields)
    summary: object | None = None
    instructions: list[object] = Field(default_factory=list)

    @field_validator("control", mode="before")
    @classmethod
    def _coerce_control(cls, value: object) -> _AgentStdoutDirectiveControlFields:
        return _AgentStdoutDirectiveControlFields.model_validate(_json_object_or_empty(value))


class _AgentStdoutEffectFields(ContractModel):
    """Executable effect subset copied into stdout after typed validation."""

    model_config = ConfigDict(extra="ignore")

    effect_id: object | None = None
    kind: object | None = None
    target: object | None = None
    origin_state: object | None = None
    resume_action: object | None = None
    mutates_resources: object | None = None
    rollback_declared: object | None = None
    payload_schema: object | None = None
    payload: object | None = None


class _AgentStdoutEffectPayloadFields(ContractModel):
    """Known effect payload fields that are useful for agent continuation."""

    model_config = ConfigDict(extra="ignore")

    kind: object | None = None
    command_family: object | None = None
    work_id: object | None = None
    agent: object | None = None
    title: object | None = None
    arguments: object | None = None
    execution_mode: object | None = None
    authoring_mode: object | None = None
    authoring_max_concurrency: object | None = None
    apply_mode: object | None = None
    serial_apply_required: object | None = None
    wait_for_all_authoring_outputs_before_apply: object | None = None
    current_batch_item_count: object | None = None
    plan_path: object | None = None
    manifest_path: object | None = None
    style_rewrite_plan_path: object | None = None
    style_rewrite_manifest_path: object | None = None
    current_batch_items: list[object] = Field(default_factory=list)


class _AgentStdoutArtifactFields(ContractModel):
    """Artifact paths that are safe to expose in the short stdout payload."""

    model_config = ConfigDict(extra="ignore")

    compact_report_path: object | None = None
    full_report_path: object | None = None
    run_state_path: object | None = None
    human_report_path: object | None = None


class _AgentStdoutDiagnosticFields(ContractModel):
    """Diagnostic subset rendered in stdout after the FSM result is compacted."""

    model_config = ConfigDict(extra="ignore")

    outcome_reason: object | None = None
    apply: object | None = None
    final_validation: object | None = None
    blocking_reasons: list[object] = Field(default_factory=list)
    related_notes_recovery_state: object | None = None
    related_notes_sync: object | None = None
    version_control_mutation_summary: object | None = None


class _FixWikiAgentStdoutReportFields(ContractModel):
    """Typed compact report consumed by the short agent stdout projection."""

    model_config = ConfigDict(extra="ignore")

    workflow: object | None = None
    run_id: object | None = None
    state_machine_snapshot: object | None = None
    progress_view_model: object | None = None
    decision: object | None = None
    human_decision_packet: object | None = None
    receipt: object | None = None
    reports: JsonObject = Field(default_factory=dict)
    agent_directive: object | None = None
    artifacts: _AgentStdoutArtifactFields = Field(default_factory=_AgentStdoutArtifactFields)
    version_control_safety: object | None = None
    diagnostic_context: _AgentStdoutDiagnosticFields = Field(default_factory=_AgentStdoutDiagnosticFields)
    error_context: object | None = None

    @field_validator("reports", mode="before")
    @classmethod
    def _coerce_reports(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)

    @field_validator("artifacts", mode="before")
    @classmethod
    def _coerce_artifacts(cls, value: object) -> _AgentStdoutArtifactFields:
        return _AgentStdoutArtifactFields.model_validate(_json_object_or_empty(value))

    @field_validator("diagnostic_context", mode="before")
    @classmethod
    def _coerce_diagnostic_context(cls, value: object) -> _AgentStdoutDiagnosticFields:
        return _AgentStdoutDiagnosticFields.model_validate(_json_object_or_empty(value))


class _AgentStdoutVersionControlSummaryFields(ContractModel):
    """Small version-control mutation summary for agent-facing diagnostics."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    available: object | None = None
    source: object | None = None
    deleted_paths: object | None = None


class _AgentStdoutVersionControlSafetyFields(ContractModel):
    """Safety subset copied from the canonical receipt/report."""

    model_config = ConfigDict(extra="ignore")

    no_resource_mutation: object | None = None
    rollback_declared: object | None = None
    resource_guard_active: object | None = None
    run_start_seen: object | None = None
    run_finish_seen: object | None = None
    restore_point_before: object | None = None
    restore_point_after: object | None = None
    sync_status: object | None = None
    backup_online: object | None = None
    direct_mutation_forbidden: object | None = None
    mutation_without_guard: object | None = None
    changed_file_count: int = Field(default=0, ge=0, strict=True)


class _FixWikiCompactReceiptFields(ContractModel):
    """Standalone compact receipt projection derived from the FSM receipt."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    workflow: object | None = None
    run_id: object | None = None
    status: object | None = None
    mutated: object | None = None
    next_action: object | None = None
    human_decision_required: object | None = None
    human_decision_packet: object | None = None
    rollback: object | None = None
    version_control_safety: object | None = None
    progress_state: object | None = None
    progress_view_model: object | None = None
    state_machine_snapshot: object | None = None
    changed_files: list[object] = Field(default_factory=list)
    artifacts: list[object] = Field(default_factory=list)
    phase_outcomes: list[object] = Field(default_factory=list)
    phase_receipts: JsonObject = Field(default_factory=dict)


class _RelatedNotesRecoveryPayloadFields(ContractModel):
    """Typed recovery evidence boundary for Related Notes projections."""

    model_config = ConfigDict(extra="ignore")

    schema_id: object | None = Field(default=None, alias="schema")
    phase: object | None = None
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    note_count: object | None = None
    record_count: object | None = None
    fresh_record_count: object | None = None
    stale_record_count: object | None = None
    remaining_count: object | None = None
    embedded_count: object | None = None
    reused_count: object | None = None
    api_calls: object | None = None
    api_failures: object | None = None
    stale_notes: object | None = None
    stale_note_count: object | None = None
    automatic_recovery_unavailable_reason: object | None = None
    selected_recovery_mode: str = ""
    recovery_mode: str = ""
    manual_instruction_allowed: object | None = None
    export_relocation: object | None = None
    export_path: object | None = None
    workflow_effect: object | None = None
    related_notes_recovery_state: object | None = None
    headless_export: JsonObject = Field(default_factory=dict)

    @field_validator("headless_export", mode="before")
    @classmethod
    def _coerce_headless_export(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)


class _RelatedNotesCompactSyncFields(ContractModel):
    """Report-only Related Notes sync evidence used by compact reports."""

    model_config = ConfigDict(extra="ignore")

    plugin: object | None = None
    model: object | None = None
    planned_note_count: object | None = None
    proposed_link_count: object | None = None
    cleared_link_count: object | None = None
    skipped_edge_count: object | None = None
    applied_note_count: object | None = None
    updates: list[object] = Field(default_factory=list)
    skipped_edges: list[object] = Field(default_factory=list)
    hash_warnings: list[object] = Field(default_factory=list)
    related_notes_export_recovery: object | None = None


class _LinkArtifactBodyCountsFields(ContractModel):
    """Nested body-linker counters consumed from a linker artifact."""

    model_config = ConfigDict(extra="ignore")

    links_planned: int = Field(default=0, ge=0)
    links_rewritten: int = Field(default=0, ge=0)
    plans: list[object] = Field(default_factory=list)


class _LinkArtifactRelatedNotesCountsFields(ContractModel):
    """Nested Related Notes counters consumed from a linker artifact."""

    model_config = ConfigDict(extra="ignore")

    applied_note_count: int = Field(default=0, ge=0)
    planned_note_count: int = Field(default=0, ge=0)
    updates: list[object] = Field(default_factory=list)


class _LinkArtifactGraphCountsFields(ContractModel):
    """Nested graph-audit counters consumed from a linker artifact."""

    model_config = ConfigDict(extra="ignore")

    error_count: int = Field(default=0, ge=0)
    blocker_count: int = Field(default=0, ge=0)
    orphan_count: int = Field(default=0, ge=0)


class _ConsumedLinkArtifactFields(ContractModel):
    """Typed view of a consumed `/mednotes:link` artifact."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    phase: str = ""
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    diagnosis_path: str = ""
    receipt_path: str = ""
    plan_hash: str = ""
    snapshot_hash: str = ""
    links_planned: int = Field(default=0, ge=0)
    links_rewritten: int = Field(default=0, ge=0)
    blocker_count: int = Field(default=0, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    files_changed: int = Field(default=0, ge=0)
    body_term_linker: _LinkArtifactBodyCountsFields = Field(default_factory=_LinkArtifactBodyCountsFields)
    related_notes_sync: _LinkArtifactRelatedNotesCountsFields = Field(default_factory=_LinkArtifactRelatedNotesCountsFields)
    graph_audit_after: _LinkArtifactGraphCountsFields = Field(default_factory=_LinkArtifactGraphCountsFields)
    graph_audit_before: _LinkArtifactGraphCountsFields = Field(default_factory=_LinkArtifactGraphCountsFields)
    changed_files: list[object] = Field(default_factory=list)
    file_changes: list[object] = Field(default_factory=list)

    @field_validator("body_term_linker", mode="before")
    @classmethod
    def _coerce_body_term_linker(cls, value: object) -> _LinkArtifactBodyCountsFields:
        return _LinkArtifactBodyCountsFields.model_validate(_json_object_or_empty(value))

    @field_validator("related_notes_sync", mode="before")
    @classmethod
    def _coerce_related_notes_sync(cls, value: object) -> _LinkArtifactRelatedNotesCountsFields:
        return _LinkArtifactRelatedNotesCountsFields.model_validate(_json_object_or_empty(value))

    @field_validator("graph_audit_after", "graph_audit_before", mode="before")
    @classmethod
    def _coerce_graph_counts(cls, value: object) -> _LinkArtifactGraphCountsFields:
        return _LinkArtifactGraphCountsFields.model_validate(_json_object_or_empty(value))


class _FixWikiWrittenReportFields(ContractModel):
    """Small write report entry used to build linker trigger context."""

    model_config = ConfigDict(extra="ignore")

    wrote: bool = False
    path: str = ""


class _FixWikiReportCollectionFields(ContractModel):
    """Collection of write reports from a fix-wiki sub-step."""

    model_config = ConfigDict(extra="ignore")

    reports: list[_FixWikiWrittenReportFields] = Field(default_factory=list)


class _AliasProjectionReceiptFields(ContractModel):
    """Alias projection receipt slice that can produce a changed-file event."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    before_hash: str = ""
    after_hash: str = ""
    note_path: str = ""


class _AliasProjectionApplyFields(ContractModel):
    """Alias projection apply receipt collection."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    applied_count: int = Field(default=0, ge=0)
    receipts: list[_AliasProjectionReceiptFields] = Field(default_factory=list)


class _DuplicateMergeReportFields(ContractModel):
    """Duplicate merge report slice used only for link trigger context."""

    model_config = ConfigDict(extra="ignore")

    keep: str = ""
    removed: list[str] = Field(default_factory=list)


class _DuplicateMergeReportsFields(ContractModel):
    """Duplicate merge reports from graph fix output."""

    model_config = ConfigDict(extra="ignore")

    reports: list[_DuplicateMergeReportFields] = Field(default_factory=list)


class _GraphFixTriggerFields(ContractModel):
    """Graph fix output fields needed to build link trigger context."""

    model_config = ConfigDict(extra="ignore")

    written_count: int = Field(default=0, ge=0)
    reports: list[_FixWikiWrittenReportFields] = Field(default_factory=list)
    duplicates: _DuplicateMergeReportsFields = Field(default_factory=_DuplicateMergeReportsFields)

    @field_validator("duplicates", mode="before")
    @classmethod
    def _coerce_duplicates(cls, value: object) -> _DuplicateMergeReportsFields:
        return _DuplicateMergeReportsFields.model_validate(_json_object_or_empty(value))


class _TaxonomyOperationFields(ContractModel):
    """Taxonomy operation slice consumed by trigger context and blocker plans."""

    model_config = ConfigDict(extra="ignore")

    action: str = ""
    source: str = ""
    destination: str = ""
    reason: str = ""
    blocked_reason: str = ""


class _TaxonomyPlanFields(ContractModel):
    """Taxonomy plan slice used by blocker-resolution planning."""

    model_config = ConfigDict(extra="ignore")

    operations: list[_TaxonomyOperationFields] = Field(default_factory=list)
    blocked_items: list[_TaxonomyOperationFields] = Field(default_factory=list)


class _TaxonomyReportFields(ContractModel):
    """Taxonomy audit counters rendered into the final validation summary."""

    model_config = ConfigDict(extra="ignore")

    proposed_moves: list[object] = Field(default_factory=list)
    duplicate_directory_groups: list[object] = Field(default_factory=list)


class _GraphAuditErrorsFields(ContractModel):
    """Graph audit errors consumed by blocker-resolution planning."""

    model_config = ConfigDict(extra="ignore")

    errors: list[JsonObject] = Field(default_factory=list)


class _LinkerBlockerFields(ContractModel):
    """Single linker blocker code used for Related Notes classification."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""


class _GraphIssueFields(ContractModel):
    """Graph issue slice used for samples and grouping."""

    model_config = ConfigDict(extra="ignore")

    code: str = "unknown"
    file: object | None = None
    line: object | None = None
    target: object | None = None
    raw: object | None = None
    message: object | None = None
    files: object | None = None


class _ReferenceRepairActionFields(ContractModel):
    """Reference-repair action code embedded in delegated graph reports."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""


class _ReferenceRepairNoteActionFields(ContractModel):
    """Reference-repair note action rendered in delegated graph reports."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    actions: list[_ReferenceRepairActionFields] = Field(default_factory=list)


class _ReferenceRepairFields(ContractModel):
    """Reference-repair summary emitted by the delegated linker diagnosis."""

    model_config = ConfigDict(extra="ignore")

    affected_note_count: int = Field(default=0, ge=0)
    note_actions: list[_ReferenceRepairNoteActionFields] = Field(default_factory=list)


class _ModifiedNoteEventFields(ContractModel):
    """Existing link-trigger event entry updated when a phase touches the same note."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    change_type: str = ""
    reason: str = ""
    reasons: list[str] = Field(default_factory=list)


class _VocabularyIssueFields(ContractModel):
    """Vocabulary-map issue slice used by recovery and identity decisions."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""
    severity: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)


class _VocabularyMapDiagnosisFields(ContractModel):
    """Vocabulary map diagnosis fields that may affect fix-wiki decisions."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    map_hash: str = ""
    issues: list[_VocabularyIssueFields] = Field(default_factory=list)


class _LinkerRelatedNotesCarrierFields(ContractModel):
    """Linker payload slice that may carry a Related Notes sync result."""

    model_config = ConfigDict(extra="ignore")

    related_notes_sync: object | None = None


class _LinkFsmReportsFields(ContractModel):
    """Reports slice from canonical child `/mednotes:link` FSM payloads."""

    model_config = ConfigDict(extra="ignore")

    details: JsonObject = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def _coerce_details(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)


class _LinkFsmReceiptFields(ContractModel):
    """Receipt slice from canonical child `/mednotes:link` FSM payloads."""

    model_config = ConfigDict(extra="ignore")

    changed_files: list[object] = Field(default_factory=list)


class _LinkFsmProgressFields(ContractModel):
    """Progress slice from canonical child `/mednotes:link` FSM payloads."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""


class _LinkFsmPayloadFields(ContractModel):
    """Canonical child link FSM lens consumed by fix-wiki projections."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    progress_view_model: _LinkFsmProgressFields = Field(default_factory=_LinkFsmProgressFields)
    reports: _LinkFsmReportsFields = Field(default_factory=_LinkFsmReportsFields)
    receipt: _LinkFsmReceiptFields = Field(default_factory=_LinkFsmReceiptFields)

    @field_validator("progress_view_model", mode="before")
    @classmethod
    def _coerce_progress_view_model(cls, value: object) -> _LinkFsmProgressFields:
        return _LinkFsmProgressFields.model_validate(_json_object_or_empty(value))

    @field_validator("reports", mode="before")
    @classmethod
    def _coerce_reports(cls, value: object) -> _LinkFsmReportsFields:
        return _LinkFsmReportsFields.model_validate(_json_object_or_empty(value))

    @field_validator("receipt", mode="before")
    @classmethod
    def _coerce_receipt(cls, value: object) -> _LinkFsmReceiptFields:
        return _LinkFsmReceiptFields.model_validate(_json_object_or_empty(value))


class _TaxonomyApplyFields(ContractModel):
    """Applied taxonomy operation collection for link trigger context."""

    model_config = ConfigDict(extra="ignore")

    receipt_path: str = ""
    applied_count: int = Field(default=0, ge=0)
    applied_operations: list[_TaxonomyOperationFields] = Field(default_factory=list)


class _RewritePlanCountFields(ContractModel):
    """Style-rewrite plan counters used by blocker resolution."""

    model_config = ConfigDict(extra="ignore")

    item_count: int = Field(default=0, ge=0)
    total_available_count: int = Field(default=0, ge=0)
    truncated: bool = False


class _BlockerResolutionGroupPayload(ContractModel):
    """Typed blocker group emitted for fix-wiki reports."""

    model_config = ConfigDict(extra="forbid")

    route: str
    count: int = Field(ge=0)
    automatic: bool
    reason: str
    next_action: str
    codes: list[str] = Field(default_factory=list)
    sample: list[JsonObject] = Field(default_factory=list)
    planned_item_count: int | None = None
    total_available_count: int | None = None
    truncated: bool | None = None


class _StyleAuditErrorFields(ContractModel):
    """Style validation error code used in content blocker evidence."""

    model_config = ConfigDict(extra="ignore")

    code: str = ""


class _StyleAuditReportFields(ContractModel):
    """Style audit report slice used to decide content rewrite blockers."""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    requires_llm_rewrite: bool = False
    path: str = ""
    title: object | None = None
    rewrite_prompt: str | None = None
    errors: list[_StyleAuditErrorFields] = Field(default_factory=list)
    warnings: list[object] = Field(default_factory=list)


class _StyleAuditReportsFields(ContractModel):
    """Style audit report collection."""

    model_config = ConfigDict(extra="ignore")

    reports: list[_StyleAuditReportFields] = Field(default_factory=list)


class _ChangedMarkdownFilesFields(ContractModel):
    """Payload slice listing Markdown paths changed by a post-style step."""

    model_config = ConfigDict(extra="ignore")

    changed_files: list[str] = Field(default_factory=list)
    changed_file_count: int = Field(default=0, ge=0)
    files_changed: int = Field(default=0, ge=0)
    written_count: int = Field(default=0, ge=0)
    applied_count: int = Field(default=0, ge=0)


class _HygienePathEntryFields(ContractModel):
    """Path entry emitted by hygiene cleanup steps."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    source: str = ""
    phase: str = ""
    action: str = ""
    problem_code: str = ""


class _PostStyleHygieneCleanupFields(ContractModel):
    """Hygiene cleanup paths that can invalidate previous style reports."""

    model_config = ConfigDict(extra="ignore")

    removed_empty_dir_entries: list[_HygienePathEntryFields] = Field(default_factory=list)
    removed_empty_dirs: list[str] = Field(default_factory=list)
    removed_empty_root_note_entries: list[_HygienePathEntryFields] = Field(default_factory=list)
    archived: list[_HygienePathEntryFields] = Field(default_factory=list)


class _DuplicateFilenameGroupFields(ContractModel):
    """Duplicate filename sample used by identity problem classification."""

    model_config = ConfigDict(extra="ignore")

    files: list[str] = Field(default_factory=list)


class _HygieneReportFields(ContractModel):
    """Wiki hygiene report fields that influence fix-wiki problem routing."""

    model_config = ConfigDict(extra="ignore")

    bak_or_rewrite: int = Field(default=0, ge=0)
    backup_file_count: int = Field(default=0, ge=0)
    rewrite_file_count: int = Field(default=0, ge=0)
    legacy_backup_candidate_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    empty_dirs: int = Field(default=0, ge=0)
    empty_dir_paths: list[object] = Field(default_factory=list)
    empty_root_note_count: int = Field(default=0, ge=0)
    empty_root_note_samples: list[object] = Field(default_factory=list)
    duplicate_hash_groups: int = Field(default=0, ge=0)
    duplicate_hash_samples: list[object] = Field(default_factory=list)
    duplicate_filename_groups: int = Field(default=0, ge=0)
    duplicate_filename_samples: list[_DuplicateFilenameGroupFields] = Field(default_factory=list)


class _FixWikiCanonicalReceiptFields(ContractModel):
    """Canonical receipt fields projected to the standalone receipt file."""

    model_config = ConfigDict(extra="ignore")

    workflow: str = ""
    run_id: str = ""
    status: str = ""
    mutated: bool | None = None
    next_action: str = ""
    human_decision_required: bool = Field(default=False, strict=True)
    human_decision_packet: object | None = None
    version_control_safety: JsonObject = Field(default_factory=dict)
    changed_files: list[object] = Field(default_factory=list)
    artifacts: list[object] = Field(default_factory=list)
    phase_outcomes: list[object] = Field(default_factory=list)
    phase_receipts: JsonObject = Field(default_factory=dict)
    changed_file_count: int | None = None

    @field_validator("version_control_safety", "phase_receipts", mode="before")
    @classmethod
    def _coerce_objects(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)


class _FixWikiReceiptProgressFields(ContractModel):
    """Progress fields used only to fill standalone receipt metadata."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    phase: str = ""
    resume_action: str = ""


class _FixWikiFsmReceiptReportFields(ContractModel):
    """Typed projection of the canonical FSM report needed to write receipt JSON."""

    model_config = ConfigDict(extra="ignore")

    workflow: str = ""
    run_id: str = ""
    receipt: _FixWikiCanonicalReceiptFields = Field(default_factory=_FixWikiCanonicalReceiptFields)
    progress_view_model: _FixWikiReceiptProgressFields = Field(default_factory=_FixWikiReceiptProgressFields)
    state_machine_snapshot: JsonObject = Field(default_factory=dict)

    @field_validator("receipt", mode="before")
    @classmethod
    def _coerce_receipt(cls, value: object) -> _FixWikiCanonicalReceiptFields:
        return _FixWikiCanonicalReceiptFields.model_validate(_json_object_or_empty(value))

    @field_validator("progress_view_model", mode="before")
    @classmethod
    def _coerce_progress(cls, value: object) -> _FixWikiReceiptProgressFields:
        return _FixWikiReceiptProgressFields.model_validate(_json_object_or_empty(value))

    @field_validator("state_machine_snapshot", mode="before")
    @classmethod
    def _coerce_state_machine(cls, value: object) -> JsonObject:
        return _json_object_or_empty(value)


class _FixWikiExistingReceiptFields(ContractModel):
    """Existing receipt evidence kept only as fallback while canonical fields win."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = ""
    status: str = ""
    phase: str = ""


class _StyleRewritePlanFields(ContractModel):
    """Typed lens for the ready style-rewrite plan consumed by fix-wiki effects."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    agent: str = ""
    work_items: list[_StyleRewriteWorkItemFields] = Field(default_factory=list)


class _BlockerResolutionGroupFields(ContractModel):
    """Typed lens for blocker-resolution groups used to authorize automation."""

    model_config = ConfigDict(extra="ignore")

    route: str = ""
    automatic: bool = Field(default=False, strict=True)
    next_action: str = ""


class _BlockerResolutionDiagnosticGroupFields(ContractModel):
    """Evidence-only blocker group; executable recovery lives in FSM roots."""

    model_config = ConfigDict(extra="ignore")

    route: str = ""
    count: int = Field(default=0, ge=0)
    automatic: bool = False
    reason: str = ""
    codes: list[str] = Field(default_factory=list)
    sample: list[JsonObject] = Field(default_factory=list)
    planned_item_count: int | None = None
    total_available_count: int | None = None
    truncated: bool | None = None


class _BlockerResolutionDiagnosticFields(ContractModel):
    """Evidence-only projection of blocker-resolution facts for diagnostic_context."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default=BLOCKER_RESOLUTION_SCHEMA, alias="schema")
    remaining_graph_blocker_count: int = Field(default=0, ge=0)
    write_error_count: int = Field(default=0, ge=0)
    requires_llm_rewrite_count: int = Field(default=0, ge=0)
    taxonomy_issue_count: int = Field(default=0, ge=0)
    taxonomy_operation_count: int = Field(default=0, ge=0)
    taxonomy_blocked_count: int = Field(default=0, ge=0)
    group_count: int = Field(default=0, ge=0)
    groups: list[_BlockerResolutionDiagnosticGroupFields] = Field(default_factory=list)
    has_blockers: bool = False
    linker_blocking_group_count: int = Field(default=0, ge=0)
    linker_can_apply: bool = False


class _BlockerResolutionFields(ContractModel):
    """Typed lens for deciding whether style rewrite can be launched automatically."""

    model_config = ConfigDict(extra="ignore")

    next_action: str = ""
    linker_can_apply: bool = Field(default=False, strict=True)
    groups: list[_BlockerResolutionGroupFields] = Field(default_factory=list)


class _FixWikiLinkerReportFields(ContractModel):
    """Boundary model for linker facts that feed the fix-wiki FSM."""

    model_config = ConfigDict(extra="ignore")

    linker_blocked: bool = Field(default=False, strict=True)
    linker_skipped_reason: str = ""


class _FixWikiStatusActionFields(ContractModel):
    """Typed status/next-action lens for legacy JSON evidence that cannot steer state raw."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    next_action: str = ""
    blocked_reason: str = ""
    skipped_reason: str = ""
    trigger: str = ""
    written_count: int = Field(default=0, ge=0, strict=True)
    warning_count: int = Field(default=0, ge=0, strict=True)
    removed_link_count: int = Field(default=0, ge=0, strict=True)


class _FixWikiRepairFactsFields(ContractModel):
    """Typed deterministic repair counters before they can affect workflow flow."""

    model_config = ConfigDict(extra="ignore")

    changed_count: int = Field(default=0, ge=0)
    written_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    recoverable_count: int = Field(default=0, ge=0)
    would_write_count: int = Field(default=0, ge=0)
    write_error_count: int = Field(default=0, ge=0)
    write_errors: list[object] = Field(default_factory=list)
    reports: list[object] = Field(default_factory=list)


class _FixWikiGraphMetricsFields(ContractModel):
    """Typed graph metrics that are allowed to influence fix-wiki state."""

    model_config = ConfigDict(extra="ignore")

    orphan_count: int = Field(default=0, ge=0)


class _FixWikiGraphAuditDecisionFields(ContractModel):
    """Typed graph audit counters read by blocker and warning classification."""

    model_config = ConfigDict(extra="ignore")

    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    blocker_count: int = Field(default=0, ge=0)
    metrics: _FixWikiGraphMetricsFields = Field(default_factory=_FixWikiGraphMetricsFields)


class _FixWikiFinalValidationGraphFields(ContractModel):
    """Closed graph validation summary passed into the canonical FSM facts."""

    model_config = ConfigDict(extra="forbid")

    error_count: int = Field(default=0, ge=0)
    blocker_count: int = Field(default=0, ge=0)
    orphan_count: int = Field(default=0, ge=0)


class _FixWikiFinalValidationHygieneFields(ContractModel):
    """Closed hygiene validation summary passed into the canonical FSM facts."""

    model_config = ConfigDict(extra="forbid")

    bak_or_rewrite: int = Field(default=0, ge=0)
    empty_dirs: int = Field(default=0, ge=0)
    empty_root_notes: int = Field(default=0, ge=0)
    duplicate_hash_groups: int = Field(default=0, ge=0)
    duplicate_filename_groups: int = Field(default=0, ge=0)


class _FixWikiFinalValidationTaxonomyFields(ContractModel):
    """Closed taxonomy validation summary passed into the canonical FSM facts."""

    model_config = ConfigDict(extra="forbid")

    proposed_moves: int = Field(default=0, ge=0)
    blocked_items: int = Field(default=0, ge=0)
    duplicate_directory_groups: int = Field(default=0, ge=0)
    ignored_items: list[str] = Field(default_factory=list)


class _FixWikiFinalValidationFields(ContractModel):
    """Strict final validation boundary before runtime facts enter the FSM."""

    model_config = ConfigDict(extra="forbid")

    graph: _FixWikiFinalValidationGraphFields = Field(default_factory=_FixWikiFinalValidationGraphFields)
    hygiene: _FixWikiFinalValidationHygieneFields = Field(default_factory=_FixWikiFinalValidationHygieneFields)
    taxonomy: _FixWikiFinalValidationTaxonomyFields = Field(default_factory=_FixWikiFinalValidationTaxonomyFields)


class _TaxonomyActionIssueFields(ContractModel):
    """Typed taxonomy action lists that can block fix-wiki/link automation."""

    model_config = ConfigDict(extra="ignore")

    proposed_moves: list[object] = Field(default_factory=list)
    unmapped_top_level_dirs: list[object] = Field(default_factory=list)
    duplicate_destinations: list[object] = Field(default_factory=list)
    root_notes: list[object] = Field(default_factory=list)


class _VocabularyBootstrapReceiptFields(ContractModel):
    """Typed receipt consumed by fix-wiki before vocabulary bootstrap can affect state."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: str = Field(default="", alias="schema")
    generated_at: str = ""
    status: str = ""
    trigger: str = ""
    automatic: bool = Field(default=False, strict=True)
    db_path: str = ""
    wiki_dir: str = ""
    plan_path: str = ""
    queue_path: str = ""
    receipt_path: str = ""
    note_count: int = Field(default=0, ge=0, strict=True)
    queued_note_count: int = Field(default=0, ge=0, strict=True)
    changed_files: list[str] = Field(default_factory=list)
    backup_paths: list[str] = Field(default_factory=list)
    dry_run: bool = Field(default=False, strict=True)
    note_count_deferred: bool = Field(default=False, strict=True)
    deferred_reason: str = ""

    def to_payload(self) -> JsonObject:
        return _contract_payload(self)


class _FixWikiApplyGuidanceLinkerFields(ContractModel):
    """Report-only linker follow-up facts used by apply guidance."""

    model_config = ConfigDict(extra="ignore")

    local_next_action: str = ""
    next_action: str = ""


class _FixWikiChangeCountContextFields(ContractModel):
    """Typed mutation-count facts used before entering the fix-wiki FSM."""

    model_config = ConfigDict(extra="forbid")

    schema_id: str = Field(default="", alias="schema")
    requested_apply: bool = Field(default=False, strict=True)
    effective_apply: bool = Field(default=False, strict=True)
    changed_count: int = Field(default=0, ge=0, strict=True)
    changed_count_meaning: str = ""
    changed_count_applied: bool = Field(default=False, strict=True)
    planned_change_count: int = Field(default=0, ge=0, strict=True)
    written_count: int = Field(default=0, ge=0, strict=True)
    total_changed_count: int = Field(default=0, ge=0, strict=True)
    vault_changed_file_count: int = Field(default=0, ge=0, strict=True)
    raw_vault_changed_file_count: int = Field(default=0, ge=0, strict=True)
    transient_vault_changed_file_count: int = Field(default=0, ge=0, strict=True)
    agent_instruction: str = ""


class _FixWikiAtomicitySplitPlanFields(ContractModel):
    """Small typed slice of the atomicity plan that can affect FSM state."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    item_count: int = Field(default=0, ge=0, strict=True)

class _FixWikiPlanPhaseFields(ContractModel):
    """Plan phase entry used when rendering diagnostic phase summaries."""

    model_config = ConfigDict(extra="ignore")

    phase: str = ""
    status: str = ""


class _FixWikiPlanFields(ContractModel):
    """Fix-wiki plan slice consumed by the runtime diagnostic summary."""

    model_config = ConfigDict(extra="ignore")

    phases: list[_FixWikiPlanPhaseFields] = Field(default_factory=list)


class _FixWikiFsmRuntimeSource(ContractModel):
    """Strict internal boundary from fix-wiki runtime facts into the FSM.

    `health.py` still owns orchestration and artifact collection. This model is
    the line where those raw runtime facts become typed data before any public
    state, decision, effect, or user-facing route is derived.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    requested_apply: bool = Field(strict=True)
    effective_apply: bool = Field(strict=True)
    total_changed_count: int = Field(default=0, ge=0, strict=True)
    change_count_context: _FixWikiChangeCountContextFields = Field(default_factory=_FixWikiChangeCountContextFields)
    warning_count: int = Field(default=0, ge=0, strict=True)
    graph_warning_count: int = Field(default=0, ge=0, strict=True)
    completed_with_warnings: bool = Field(default=False, strict=True)
    requires_llm_rewrite_count: int = Field(default=0, ge=0, strict=True)
    final_validation: _FixWikiFinalValidationFields = Field(default_factory=_FixWikiFinalValidationFields)
    version_control_safety: JsonObject = Field(default_factory=dict)
    related_notes_blocked: bool = Field(default=False, strict=True)
    related_notes_recovery_state: JsonObject = Field(default_factory=dict)
    vocabulary_semantic_ingestion_pending: bool = Field(default=False, strict=True)
    vocabulary_eval_needs_review: bool = Field(default=False, strict=True)
    atomicity_split_plan: _FixWikiAtomicitySplitPlanFields = Field(default_factory=_FixWikiAtomicitySplitPlanFields)
    atomicity_split_required: bool = Field(default=False, strict=True)
    human_decision_required: bool = Field(default=False, strict=True)
    human_decision_packets: list[JsonObject] = Field(default_factory=list)
    human_decision_kinds: list[str] = Field(default_factory=list)
    primary_human_decision_kind: str = ""
    human_decision_reason_code: str = ""
    failed: bool = Field(default=False, strict=True)
    failed_reason_code: str = ""
    vault_guard_required: bool = Field(default=False, strict=True)
    environment_windows_path_or_venv_blocked: bool = Field(default=False, strict=True)
    required_inputs: list[str] = Field(default_factory=list)
    file_changes: list[JsonObject | str] = Field(default_factory=list)
    linker_blocked: bool = Field(default=False, strict=True)
    linker_skipped_reason: str = ""
    linker_apply: JsonObject | None = None
    graph_review_required: bool = Field(default=False, strict=True)
    taxonomy_action_required: bool = Field(default=False, strict=True)
    error_context: JsonObject = Field(default_factory=dict)
    workflow_result_label: str = ""
    user_summary: str = ""
    blocker_resolution: JsonObject = Field(default_factory=dict)
    apply_guidance: JsonObject = Field(default_factory=dict)
    fix_wiki_plan_validation: JsonObject = Field(default_factory=dict)
    diagnostic_payload: JsonObject = Field(default_factory=dict)
    fix_wiki_problems: list[JsonObject] = Field(default_factory=list)
    write_error_count: int = Field(default=0, ge=0, strict=True)
    write_errors: list[JsonObject] = Field(default_factory=list)
    vocabulary_bootstrap: _VocabularyBootstrapReceiptFields = Field(default_factory=_VocabularyBootstrapReceiptFields)
    vocabulary_map_diagnosis: JsonObject = Field(default_factory=dict)
    vocabulary_semantic_repair: JsonObject = Field(default_factory=dict)
    vocabulary_curator_batch_plan: JsonObject | None = None
    alias_projection_plan: JsonObject = Field(default_factory=dict)
    alias_projection_apply: JsonObject = Field(default_factory=dict)
    sources_backfill: JsonObject = Field(default_factory=dict)
    markdown_node_runtime: JsonObject = Field(default_factory=dict)
    related_notes_sync: JsonObject = Field(default_factory=dict)
    related_notes_export_recovery: JsonObject = Field(default_factory=dict)
    related_notes_safety_cleanup: JsonObject | None = None
    linker_diagnosis: JsonObject = Field(default_factory=dict)
    linker_artifact_compaction: JsonObject = Field(default_factory=dict)
    graph_audit_final: JsonObject = Field(default_factory=dict)
    taxonomy_apply: JsonObject | None = None
    taxonomy_apply_enabled: bool = Field(default=False, strict=True)
    taxonomy_apply_requires_confirmation: bool = Field(default=False, strict=True)
    taxonomy_apply_skipped_reason: str = ""
    version_control_mutation_summary: JsonObject = Field(default_factory=dict)
    fix_wiki_plan_path: str = ""
    fix_wiki_receipt_path: str = ""
    sources_backfill_receipt_path: str = ""
    taxonomy_plan_path: str = ""
    taxonomy_receipt_path: str = ""
    style_rewrite_plan: JsonObject | None = None
    style_rewrite_plan_path: str = ""
    style_rewrite_manifest_path: str = ""
    human_report_path: str = ""
    compact_report_path: str = ""
    full_report_path: str = ""
    run_state_path: str = ""
    linker_diagnosis_path: str = ""
    linker_receipt_path: str = ""
    link_trigger_context_path: str = ""
    related_notes_receipt_path: str = ""
    vocabulary_curator_batch_plan_path: str = ""
    atomicity_split_plan_path: str = ""
    context_packets: dict[str, str] = Field(default_factory=dict)

    @property
    def merge_review_required(self) -> bool:
        """Expose merge review as a domain fact instead of FSM decision metadata."""

        merge_kinds = {"title_driven_merge_review", "note_merge_required", "merge_review_required"}
        return any(kind in merge_kinds for kind in self.human_decision_kinds)

    @property
    def graph_validation(self) -> JsonObject:
        return _json_object(self.final_validation.graph.model_dump(mode="json"))

    @property
    def warning_count_for_fsm(self) -> int:
        warning_count = self.warning_count + self.graph_warning_count
        if self.completed_with_warnings:
            return max(1, warning_count)
        return warning_count

    def artifact_paths(self) -> JsonObject:
        artifacts: dict[str, object] = {}
        for key in type(self).model_fields:
            value = getattr(self, key)
            if key.endswith("_path") and value:
                artifacts[key] = value
        if self.context_packets:
            artifacts["context_packets"] = self.context_packets
        return _json_object(artifacts)

    def changed_files_for_fsm(self) -> list[str]:
        changed_files: list[str] = []
        for item in self.file_changes:
            raw_path: object
            if isinstance(item, dict):
                raw_path = _json_field(_json_object(item), "path")
            else:
                raw_path = item
            if not isinstance(raw_path, str):
                continue
            path = raw_path.strip()
            if path:
                changed_files.append(path)
        return changed_files

    def next_action_for_fsm(self) -> str:
        """Expose the concrete recovery route that belongs to the FSM leaf."""

        if self.atomicity_split_required:
            return ""
        return _FixWikiStatusActionFields.model_validate(self.error_context).next_action.strip()

    def diagnostic_context_for_fsm(self, *, apply_taxonomy: bool) -> JsonObject:
        diagnostic_payload = _json_object_or_empty(self.diagnostic_payload)
        related_notes_export_recovery = _compact_related_notes_recovery(self.related_notes_export_recovery)
        related_notes_sync = _json_object(self.related_notes_sync)
        nested_related_notes_recovery = related_notes_sync.get("related_notes_export_recovery")
        if isinstance(nested_related_notes_recovery, dict):
            related_notes_sync["related_notes_export_recovery"] = _compact_related_notes_recovery(
                _json_object(nested_related_notes_recovery)
            )
        apply_context: dict[str, object] = {
            "requested_apply": self.requested_apply,
            "effective_apply": self.effective_apply,
            "apply_taxonomy": apply_taxonomy,
        }
        if self.change_count_context.vault_changed_file_count:
            apply_context["vault_changed_file_count"] = self.change_count_context.vault_changed_file_count
        if (
            self.change_count_context.raw_vault_changed_file_count
            and self.change_count_context.raw_vault_changed_file_count != self.change_count_context.vault_changed_file_count
        ):
            apply_context["raw_vault_changed_file_count"] = self.change_count_context.raw_vault_changed_file_count
        if self.change_count_context.written_count:
            apply_context["written_count"] = self.change_count_context.written_count
        context_payload: dict[str, object] = {
            "workflow_result_label": self.workflow_result_label,
            "runtime_summary": _json_field(diagnostic_payload, "runtime_summary", ""),
            "user_summary": self.user_summary,
            "apply": apply_context,
            "change_count_context": self.change_count_context.to_payload(),
            "blocker_resolution": self.blocker_resolution,
            "blocking_reasons": _json_field(diagnostic_payload, "blocking_reasons", []),
            "apply_guidance": self.apply_guidance,
            "final_validation": self.final_validation,
            "artifact_paths": self.artifact_paths(),
            "fix_wiki_plan_validation": self.fix_wiki_plan_validation,
            "phases": _json_field(diagnostic_payload, "phases", []),
            "fix_wiki_problems": self.fix_wiki_problems,
            "problems": self.fix_wiki_problems,
            "write_error_count": self.write_error_count,
            "write_errors": self.write_errors,
            "vocabulary_bootstrap": self.vocabulary_bootstrap.to_payload(),
            "vocabulary_map_diagnosis": self.vocabulary_map_diagnosis,
            "vocabulary_semantic_repair": self.vocabulary_semantic_repair,
            "vocabulary_curator_batch_plan": self.vocabulary_curator_batch_plan or {},
            "atomicity_split_plan": self.atomicity_split_plan.to_payload(),
            "alias_projection_plan": self.alias_projection_plan,
            "alias_projection_apply": self.alias_projection_apply,
            "sources_backfill": self.sources_backfill,
            "markdown_node_runtime": self.markdown_node_runtime,
            "related_notes_sync": related_notes_sync,
            "related_notes_export_recovery": related_notes_export_recovery,
            "related_notes_recovery_state": self.related_notes_recovery_state,
            "related_notes_safety_cleanup": self.related_notes_safety_cleanup or {},
            "linker_diagnosis": _consumed_link_artifact_source(self.linker_diagnosis),
            "linker_artifact_compaction": self.linker_artifact_compaction,
            "linker_skipped_reason": self.linker_skipped_reason,
            "graph_audit_final": self.graph_audit_final,
            "taxonomy_action_required": self.taxonomy_action_required,
            "taxonomy_apply": self.taxonomy_apply or {},
            "taxonomy_apply_enabled": self.taxonomy_apply_enabled,
            "taxonomy_apply_requires_confirmation": self.taxonomy_apply_requires_confirmation,
            "taxonomy_apply_skipped_reason": self.taxonomy_apply_skipped_reason,
            "version_control_mutation_summary": self.version_control_mutation_summary,
        }
        if self.linker_apply is not None:
            linker_apply = _consumed_link_artifact_source(self.linker_apply)
            if "related_notes_sync" not in linker_apply and related_notes_sync:
                # Body-only apply intentionally omits Related Notes execution;
                # preserve the original blocker evidence in the final context.
                linker_apply = _json_object({**linker_apply, "related_notes_sync": related_notes_sync})
            context_payload["linker_apply"] = linker_apply
        return _json_object(context_payload)

    def to_lens_payload(self) -> JsonObject:
        return _json_object(self.model_dump(mode="python"))


RELATED_NOTES_EXTERNAL_RETRY_REASONS = frozenset(
    {
        "related_notes_headless_quota_exhausted",
        "related_notes_headless_time_budget_exhausted",
    }
)


class _RelatedNotesConvergenceProjection(ContractModel):
    """Typed lens for convergence evidence embedded in Related Notes payloads."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    pass_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)


class _RelatedNotesAgentReportFields(ContractModel):
    """Typed boundary for the compact agent-facing Related Notes projection."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    phase: str = ""
    update_count: int = Field(default=0, ge=0, strict=True)
    hash_warning_count: int = Field(default=0, ge=0, strict=True)
    convergence: _RelatedNotesConvergenceProjection = Field(default_factory=_RelatedNotesConvergenceProjection)

    @field_validator("convergence", mode="before")
    @classmethod
    def _coerce_convergence(cls, value: object) -> _RelatedNotesConvergenceProjection:
        return _RelatedNotesConvergenceProjection.model_validate(_json_object_or_empty(value))


class _RelatedNotesPublicConvergenceFields(ContractModel):
    """Public-only convergence summary already sanitized for compact reports."""

    model_config = ConfigDict(extra="ignore")

    public_state: str = ""
    pass_count: int = Field(default=0, ge=0, strict=True)
    applied_note_count: int = Field(default=0, ge=0, strict=True)


class _RelatedNotesPublicSummaryFields(ContractModel):
    """Public-only Related Notes summary; it intentionally carries no route."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    status: str = ""
    public_state: str = ""
    public_summary: str = ""
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    update_count: int = Field(default=0, ge=0, strict=True)
    hash_warning_count: int = Field(default=0, ge=0, strict=True)
    convergence: _RelatedNotesPublicConvergenceFields = Field(default_factory=_RelatedNotesPublicConvergenceFields)

    @field_validator("convergence", mode="before")
    @classmethod
    def _coerce_convergence(cls, value: object) -> _RelatedNotesPublicConvergenceFields:
        return _RelatedNotesPublicConvergenceFields.model_validate(_json_object_or_empty(value))


def _json_object(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return cast(JsonObject, payload)
    return JsonObjectAdapter.validate_python(payload)


def _json_object_or_empty(payload: object | None) -> JsonObject:
    return _json_object(payload) if isinstance(payload, dict) else {}


def _json_field(payload: JsonObject, key: str, default: object = "") -> object:
    return payload[key] if key in payload else default


def _contract_payload(model: ContractModel, *, exclude: frozenset[str] = frozenset()) -> JsonObject:
    """Dump only caller-provided typed fields from a compact projection model."""

    return _json_object(
        model.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude_none=True,
            exclude=set(exclude),
        )
    )


def _optional_path_string(path: Path | None) -> str:
    """Render optional artifact paths without truthiness-based type coercion."""

    return "" if path is None else str(path)


def _nested_graph_audit(payload: JsonObject | None, field_name: str) -> JsonObject | None:
    if not isinstance(payload, dict):
        return None
    direct = _json_field(payload, field_name, None)
    if isinstance(direct, dict):
        return _json_object(direct)
    body_linker = _json_field(payload, "body_term_linker", None)
    if isinstance(body_linker, dict):
        nested = _json_field(_json_object(body_linker), field_name, None)
        if isinstance(nested, dict):
            return _json_object(nested)
    return None


def _graph_audit_before_linker_or_run(
    linker_diagnosis: JsonObject,
    fallback_audit: Callable[[], JsonObject],
) -> JsonObject:
    return _nested_graph_audit(linker_diagnosis, "graph_audit_before") or fallback_audit()


def _graph_audit_after_linker_or_run(
    linker_apply: JsonObject | None,
    graph_before_linker: JsonObject,
    related_notes_safety_cleanup: JsonObject,
    fallback_audit: Callable[[], JsonObject],
) -> JsonObject:
    cleanup_fields = _ChangedMarkdownFilesFields.model_validate(related_notes_safety_cleanup)
    if cleanup_fields.changed_file_count > 0:
        return fallback_audit()
    graph_after = _nested_graph_audit(linker_apply, "graph_audit_after")
    if graph_after is not None:
        return graph_after
    if linker_apply is None:
        return graph_before_linker
    return fallback_audit()


def _style_audit_after_fix_or_validate(
    wiki_dir: Path,
    style_fix: Mapping[str, object],
    sources_backfill: Mapping[str, object],
    *,
    fallback: Callable[[], JsonObject],
) -> JsonObject:
    if not _style_fix_reports_are_current(style_fix, sources_backfill):
        return fallback()
    style_fields = _StyleAuditReportsFields.model_validate(dict(style_fix))
    reports = [report.to_payload() for report in style_fields.reports]
    wiki_dir_value = style_fix["wiki_dir"] if "wiki_dir" in style_fix else wiki_dir
    return _json_object({
        "schema": STYLE_AUDIT_SCHEMA,
        "wiki_dir": str(wiki_dir_value or wiki_dir),
        "file_count": _int_field(style_fix, "file_count", len(reports)),
        "ok_count": sum(1 for item in style_fields.reports if item.ok),
        "error_count": sum(1 for item in style_fields.reports if item.errors),
        "warning_count": sum(1 for item in style_fields.reports if item.warnings),
        "reports": reports,
    })


def _style_audit_after_post_style_mutations(
    wiki_dir: Path,
    current_audit: Mapping[str, object],
    *,
    linker_apply: Mapping[str, object] | None,
    related_notes_safety_cleanup: Mapping[str, object] | None,
    hygiene_cleanup: Mapping[str, object] | None,
    fallback: Callable[[], JsonObject],
) -> JsonObject:
    current_fields = _StyleAuditReportsFields.model_validate(dict(current_audit))
    reports = [report.to_payload() for report in current_fields.reports]
    if not reports:
        return fallback()

    changed_paths = _post_style_changed_markdown_paths(
        wiki_dir,
        linker_apply=linker_apply,
        related_notes_safety_cleanup=related_notes_safety_cleanup,
        hygiene_cleanup=hygiene_cleanup,
    )
    if changed_paths is None:
        return fallback()
    if not changed_paths:
        return _style_audit_from_reports(wiki_dir=wiki_dir, reports=reports)

    reports_by_path: dict[str, JsonObject] = {}
    for report in reports:
        key = _style_report_relative_path(wiki_dir, report)
        if key is None:
            return fallback()
        reports_by_path[key] = report

    for relative_path in changed_paths:
        note_path = wiki_dir / relative_path
        if not note_path.exists():
            reports_by_path.pop(relative_path, None)
            continue
        if not note_path.is_file():
            return fallback()
        content = note_path.read_text(encoding="utf-8")
        title = note_style.infer_title(content, note_path)
        report = note_style.validate_note_style(content, title=title, path=str(note_path))
        reports_by_path[relative_path] = _downgrade_invalid_root_note_report(report, wiki_dir=wiki_dir, path=note_path)

    return _style_audit_from_reports(wiki_dir=wiki_dir, reports=list(reports_by_path.values()))


def _post_style_changed_markdown_paths(
    wiki_dir: Path,
    *,
    linker_apply: Mapping[str, object] | None,
    related_notes_safety_cleanup: Mapping[str, object] | None,
    hygiene_cleanup: Mapping[str, object] | None,
) -> list[str] | None:
    changed: set[str] = set()
    for payload in (linker_apply, related_notes_safety_cleanup):
        if payload is None:
            continue
        changed_fields = _ChangedMarkdownFilesFields.model_validate(dict(payload))
        for value in changed_fields.changed_files:
            relative_path = _normalize_wiki_markdown_path(wiki_dir, value)
            if relative_path is None:
                return None
            changed.add(relative_path)
    if hygiene_cleanup is not None:
        hygiene_fields = _PostStyleHygieneCleanupFields.model_validate(dict(hygiene_cleanup))
        for entry in hygiene_fields.removed_empty_root_note_entries:
            if not entry.path:
                return None
            relative_path = _normalize_wiki_markdown_path(wiki_dir, entry.path)
            if relative_path is None:
                return None
            changed.add(relative_path)
        for entry in hygiene_fields.archived:
            if not entry.source.endswith(".md"):
                continue
            relative_path = _normalize_wiki_markdown_path(wiki_dir, entry.source)
            if relative_path is None:
                return None
            changed.add(relative_path)
    return sorted(changed)


def _style_report_relative_path(wiki_dir: Path, report: Mapping[str, object]) -> str | None:
    fields = _StyleAuditReportFields.model_validate(dict(report))
    if not fields.path:
        return None
    return _normalize_wiki_markdown_path(wiki_dir, fields.path)


def _normalize_wiki_markdown_path(wiki_dir: Path, value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    path = Path(cleaned)
    candidate = path if path.is_absolute() else wiki_dir / path
    try:
        relative = candidate.relative_to(wiki_dir)
    except ValueError:
        try:
            relative = candidate.resolve(strict=False).relative_to(wiki_dir.resolve(strict=False))
        except ValueError:
            return None
    if relative.suffix.lower() != ".md":
        return None
    return relative.as_posix()


def _style_audit_from_reports(*, wiki_dir: Path, reports: list[JsonObject]) -> JsonObject:
    style_fields = _StyleAuditReportsFields.model_validate({"reports": reports})
    return _json_object({
        "schema": STYLE_AUDIT_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "file_count": len(reports),
        "ok_count": sum(1 for item in style_fields.reports if item.ok),
        "error_count": sum(1 for item in style_fields.reports if item.errors),
        "warning_count": sum(1 for item in style_fields.reports if item.warnings),
        "reports": reports,
    })


def _style_fix_reports_are_current(
    style_fix: Mapping[str, object],
    sources_backfill: Mapping[str, object],
) -> bool:
    style_fields = _FixWikiRepairFactsFields.model_validate(dict(style_fix))
    sources_fields = _FixWikiRepairFactsFields.model_validate(dict(sources_backfill))
    return (
        bool(style_fields.reports)
        and style_fields.write_error_count == 0
        and sources_fields.written_count == 0
        and sources_fields.would_write_count == 0
        and sources_fields.changed_count == 0
    )


def _deferred_initial_style_audit(wiki_dir: Path) -> JsonObject:
    return _json_object({
        "schema": STYLE_AUDIT_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "file_count": 0,
        "ok_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "reports": [],
        "status": "deferred",
        "deferred_reason": "apply_uses_post_mutation_style_audit",
    })


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_field(payload: Mapping[str, object], key: str, default: int = 0) -> int:
    value = payload[key] if key in payload else None
    return value if isinstance(value, int) else default


def _style_rewrite_plan_if_needed(config: MedConfig, audit: JsonObject) -> JsonObject | None:
    if not _requires_style_rewrite(audit):
        return None
    batch_size = configured_subagent_max_concurrency(config, "style-rewrite")
    return _json_object(
        plan_subagents(
            config,
            "style-rewrite",
            max_concurrency=batch_size,
            limit=batch_size,
            style_audit=audit,
        )
    )


def _taxonomy_action_issue_count(audit: JsonObject) -> int:
    fields = _TaxonomyActionIssueFields.model_validate(audit)
    return (
        len(fields.proposed_moves)
        + len(fields.unmapped_top_level_dirs)
        + len(fields.duplicate_destinations)
        + len(fields.root_notes)
    )


def _raw_chat_files_available(raw_dir: Path) -> bool:
    if not raw_dir.exists():
        return False
    return any(path.is_file() and path.suffix.lower() in {".md", ".markdown"} for path in raw_dir.rglob("*"))


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _run_dir(run_id: str) -> Path:
    return _user_state_dir() / "runs" / run_id


def _archive_root(run_id: str) -> Path:
    day = run_id[:8]
    return _user_state_dir() / "backup_archive" / "fix-wiki" / day / run_id


def _write_json_file(path: Path, data: JsonObject) -> None:
    atomic_write_text(path, json.dumps(_json_object(data), ensure_ascii=False, indent=2) + "\n")


def _wiki_snapshot_hash(wiki_dir: Path) -> str:
    return fix_wiki_snapshot_hash(wiki_dir)


def _vocabulary_blocked_reason(status: str) -> str:
    if status == "blocked_pending":
        return "vocabulary_semantic_ingestion_pending"
    if status == "blocked_human":
        return "vocabulary_map_blocked"
    return ""


def _blocker_requires_human(code: str, *, fallback: bool = False) -> bool:
    if not code:
        return False
    try:
        return blocker_entry(code).requires_human_packet
    except Exception:
        return fallback or code == "human_decision_required"


def _vocabulary_diagnosis_requires_human(vocabulary_map_diagnosis: JsonObject) -> bool:
    fields = _VocabularyMapDiagnosisFields.model_validate(vocabulary_map_diagnosis)
    status = fields.status
    if status != "blocked_human":
        return False
    if not fields.issues:
        return _blocker_requires_human(_vocabulary_blocked_reason(status), fallback=True)
    return any(
        _blocker_requires_human(
            issue.code,
            fallback=issue.severity == "human_decision",
        )
        for issue in fields.issues
    )


def _skipped_vocabulary_map_diagnosis(config: MedConfig) -> JsonObject:
    return _json_object({
        "schema": "medical-notes-workbench.vocabulary-map.v1",
        "status": "skipped",
        "db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "map_hash": "",
        "note_count": 0,
        "meaning_count": 0,
        "surface_count": 0,
        "ambiguous_surface_count": 0,
        "pending_semantic_ingestion_count": 0,
        "issues": [],
    })


def _vocabulary_map_diagnosis_payload(config: MedConfig, *, allow_create: bool) -> JsonObject:
    if config.vocabulary_db_path is None:
        return _skipped_vocabulary_map_diagnosis(config)
    if not allow_create and not config.vocabulary_db_path.exists():
        return _skipped_vocabulary_map_diagnosis(config)
    return _json_object(load_vocabulary_map_diagnosis(config.vocabulary_db_path).as_diagnosis_dict())


def _alias_projection_plan_status(vocabulary_map_diagnosis: JsonObject) -> JsonObject:
    fields = _VocabularyMapDiagnosisFields.model_validate(vocabulary_map_diagnosis)
    status = fields.status or "skipped"
    blocked_reason = _vocabulary_blocked_reason(status)
    if status == "skipped":
        return _json_object({"status": "skipped", "blocked_reason": ""})
    if blocked_reason:
        return _json_object({
            "status": "blocked",
            "blocked_reason": blocked_reason,
            "issues": [issue.model_dump(mode="json") for issue in fields.issues],
        })
    return _json_object({"status": "planned", "blocked_reason": ""})


def _alias_projection_plan_payload(
    config: MedConfig,
    vocabulary_map_diagnosis: JsonObject,
    *,
    run_dir: Path,
    backup: bool,
) -> JsonObject:
    base = _alias_projection_plan_status(vocabulary_map_diagnosis)
    if _json_field(base, "status") != "planned":
        return base
    if config.vocabulary_db_path is None or not config.vocabulary_db_path.exists():
        return _json_object({"status": "skipped", "blocked_reason": "", "skipped_reason": "vocabulary_db_missing"})
    vocab = load_vocabulary_map_diagnosis(config.vocabulary_db_path)
    plan = build_alias_projection_plan(vocab, backup=backup)
    plan_path = run_dir / "alias-projection-plan.json"
    write_error = _try_write_json_file(plan_path, plan)
    return _json_object({
        **plan,
        "plan_path": str(plan_path),
        "write_error": write_error or "",
    })


def _alias_projection_apply_payload(*, apply: bool, plan: JsonObject, run_dir: Path) -> JsonObject:
    if not apply:
        return _json_object({
            "schema": "medical-notes-workbench.alias-projection-receipt.v1",
            "status": "skipped",
            "skipped_reason": "preview",
            "applied_count": 0,
            "blocked_count": 0,
            "receipts": [],
        })
    plan_fields = _FixWikiStatusActionFields.model_validate(plan)
    if plan_fields.status != "planned":
        return _json_object({
            "schema": "medical-notes-workbench.alias-projection-receipt.v1",
            "status": "skipped",
            "skipped_reason": plan_fields.blocked_reason or plan_fields.skipped_reason or "not_planned",
            "applied_count": 0,
            "blocked_count": 0,
            "receipts": [],
        })
    receipt = apply_alias_projection_plan(plan)
    receipt_path = run_dir / "alias-projection-receipt.json"
    write_error = _try_write_json_file(receipt_path, receipt)
    return _json_object({**receipt, "receipt_path": str(receipt_path), "write_error": write_error or ""})


def _vocabulary_curator_batch_plan_payload(
    config: MedConfig,
    vocabulary_map_diagnosis: JsonObject,
    *,
    run_id: str,
    run_dir: Path,
) -> tuple[JsonObject, str]:
    diagnosis = _VocabularyMapDiagnosisFields.model_validate(vocabulary_map_diagnosis)
    if diagnosis.status != "blocked_pending":
        return _json_object({"status": "skipped", "skipped_reason": "vocabulary_not_pending", "item_count": 0}), ""
    if config.vocabulary_db_path is None:
        return _json_object({"status": "skipped", "skipped_reason": "vocabulary_db_missing", "item_count": 0}), ""
    plan = build_vocabulary_curator_batch_plan(
        db_path=config.vocabulary_db_path,
        batch_id=f"{run_id}-vocabulary-curation",
        output_dir=run_dir / "vocabulary-curator-outputs",
        limit=50,
    )
    plan_path = run_dir / "vocabulary-curator-batch-plan.json"
    write_error = _try_write_json_file(plan_path, plan)
    return _json_object({**plan, "plan_path": str(plan_path), "write_error": write_error or ""}), str(plan_path)


def _pending_deferred_work_items(db_path: Path | None) -> list[JsonObject]:
    if db_path is None or not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT work_id, source_agent, assigned_agent, reason, note_path,
                   content_hash, payload_json, status
            FROM deferred_work_items
            WHERE status = 'pending'
            ORDER BY updated_at ASC, work_id ASC
            """
        ).fetchall()
    items: list[JsonObject] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        item = {key: row[key] for key in row.keys() if key != "payload_json"}
        item["payload"] = payload if isinstance(payload, dict) else {}
        items.append(_json_object(item))
    return items


def _atomicity_split_plan_payload(
    *,
    fix_wiki_plan_path: Path,
    run_id: str,
    run_dir: Path,
) -> tuple[JsonObject, str]:
    plan = build_atomicity_split_plan(
        fix_wiki_plan_path=fix_wiki_plan_path,
        batch_id=f"{run_id}-atomicity-split",
        temp_root=run_dir / "atomicity-split",
        limit=20,
    )
    plan_fields = _FixWikiAtomicitySplitPlanFields.model_validate(plan)
    if plan_fields.status != "ready":
        return _json_object({**plan, "plan_path": "", "write_error": ""}), ""
    plan_path = run_dir / "atomicity-split-plan.json"
    write_error = _try_write_json_file(plan_path, plan)
    return _json_object({**plan, "plan_path": str(plan_path), "write_error": write_error or ""}), str(plan_path)


def _link_diagnosis_skipped_for_vocabulary_preview(
    path: Path,
    vocabulary_bootstrap: _VocabularyBootstrapReceiptFields,
) -> JsonObject:
    return _json_object({
        "schema": "medical-notes-workbench.link-diagnosis.v1",
        "phase": "link_diagnosis",
        "status": "skipped",
        "skipped_reason": "vocabulary_bootstrap_preview",
        "blocked_reason": "",
        "next_action": "",
        "local_next_action": (
            "Quando o workflow principal estiver autorizado a aplicar mudanças, inicializar o vocabulary DB "
            "antes do diagnóstico completo de links."
        ),
        "next_action_status": "preview_only",
        "next_action_public": False,
        "required_inputs": ["wiki_dir", "vocabulary_db"],
        "human_decision_required": False,
        "diagnosis_path": str(path),
        "vocabulary_bootstrap": vocabulary_bootstrap.to_payload(),
        "phases": {
            "reference_repair": {"status": "skipped"},
            "contextual_alias_disambiguation": {"status": "skipped"},
            "body_term_linker": {"status": "skipped", "blocked_reason": "vocabulary_bootstrap_preview"},
            "related_notes_sync": {"status": "skipped"},
            "graph_validation": {"status": "skipped"},
        },
        "blocker_count": 0,
        "links_planned": 0,
        "links_rewritten": 0,
        "related_notes_blocked": False,
        "related_notes_sync": None,
    })


def _related_notes_from_linker(
    *,
    linker_diagnosis: JsonObject,
    linker_apply: JsonObject | None,
) -> JsonObject:
    for payload in (linker_apply, linker_diagnosis):
        if isinstance(payload, dict):
            carrier = _LinkerRelatedNotesCarrierFields.model_validate(payload)
            if isinstance(carrier.related_notes_sync, dict):
                return _json_object(carrier.related_notes_sync)
            fsm_details = _link_fsm_operation_details(payload)
            related_notes = _json_field(fsm_details, "related_notes_sync")
            if isinstance(related_notes, dict):
                return _json_object(related_notes)
    return _json_object({
        "status": "skipped",
        "blocked_reason": "",
        "skipped_reason": "linker_not_run",
        "applied_note_count": 0,
        "planned_note_count": 0,
        "receipt_path": "",
    })


def _link_fsm_operation_details(payload: JsonObject | None) -> JsonObject:
    """Return adapter-projected child-operation details from a link FSM payload."""

    if not isinstance(payload, dict):
        return _json_object({})
    fields = _LinkFsmPayloadFields.model_validate(payload)
    if fields.schema_id != "medical-notes-workbench.link-fsm-result.v1":
        return _json_object({})
    return _json_object(fields.reports.details)


def _link_fsm_progress_value(payload: JsonObject | None) -> str:
    """Read the child FSM progress status without accepting legacy root status."""

    if not isinstance(payload, dict):
        return ""
    return normalize_link_runtime_artifact(payload).operation_status.strip()


def _is_link_fsm_payload(payload: JsonObject | None) -> bool:
    """Check whether the child artifact is the canonical link FSM payload."""

    if not isinstance(payload, dict):
        return False
    normalize_link_runtime_artifact(payload)
    return True


def _consumed_link_artifact_source(payload: JsonObject | None) -> JsonObject:
    """Return the stable link artifact slice consumed by fix-wiki decisions."""

    if not isinstance(payload, dict):
        return _json_object({})
    details = _link_fsm_operation_details(payload)
    if details:
        return details
    return _json_object(payload)


def _linker_blocked_only_by_related_notes(payload: JsonObject) -> bool:
    source = _consumed_link_artifact_source(payload)
    blockers = _json_field(source, "blockers")
    blocker_list = [item for item in blockers if isinstance(item, dict)] if isinstance(blockers, list) else []
    if not blocker_list:
        return False
    for blocker in blocker_list:
        code = _LinkerBlockerFields.model_validate(blocker).code
        if code != "related_notes_blocked" and not code.startswith("related_notes_"):
            return False
    return True


def _payload_int(payload: JsonObject, key: str) -> int:
    value = _json_field(payload, key, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _public_vault_changed_file_count(version_control_mutation_summary: JsonObject) -> int:
    markdown_count = _payload_int(version_control_mutation_summary, "markdown_changed_file_count")
    if markdown_count:
        return markdown_count
    return _payload_int(version_control_mutation_summary, "changed_file_count")


def _linker_applied_vault_changes(payload: JsonObject | None) -> bool:
    if not isinstance(payload, dict):
        return False
    fsm_fields = _LinkFsmPayloadFields.model_validate(payload)
    if (
        fsm_fields.schema_id == "medical-notes-workbench.link-fsm-result.v1"
        and fsm_fields.receipt.changed_files
    ):
        return True
    details = _link_fsm_operation_details(payload)
    source = details if details else payload
    if _payload_int(source, "files_changed") or _payload_int(source, "changed_file_count"):
        return True
    for key in ("body_term_linker", "related_notes_sync"):
        nested = _json_field(source, key)
        if isinstance(nested, dict) and (
            _payload_int(_json_object(nested), "files_changed")
            or _payload_int(_json_object(nested), "changed_file_count")
            or _payload_int(_json_object(nested), "applied_note_count")
        ):
            return True
    return False


def _linker_status_blocks_after_final_graph(payload: JsonObject | None, *, graph_error_count: int) -> bool:
    if not isinstance(payload, dict):
        return False
    child_status = _link_fsm_progress_value(payload)
    if child_status not in {"blocked", "failed", "completed_with_link_blockers"}:
        return False
    source = _link_fsm_operation_details(payload)
    fields = _LinkArtifactStatusFields.model_validate(source)
    status = fields.status.strip()
    if not status or status in {"completed", "completed_with_warnings", "diagnosis_ready"}:
        return False
    blocked_reason = fields.blocked_reason.strip()
    if status == "completed_with_link_blockers" and blocked_reason == "graph_blockers" and graph_error_count == 0:
        return False
    return True


def _related_notes_recovery_candidate(payload: JsonObject) -> bool:
    details = _link_fsm_operation_details(payload)
    source = details if details else payload
    carrier = _LinkerRelatedNotesCarrierFields.model_validate(source)
    related_sync = _related_notes_sync_result(carrier.related_notes_sync)
    if not related_sync or not related_notes_sync_blocked(related_sync):
        return False
    return related_sync.blocked_reason in {
        "related_notes_hash_mismatch",
        "related_notes_export_stale",
        "related_notes_vault_mismatch",
    }


def _related_notes_sync_result(payload: object | None) -> LinkRelatedSyncResult:
    return LinkRelatedSyncResult.from_payload(payload or {})


def _fix_wiki_required_inputs(*, blocked_reason: str, related_notes_sync: object) -> list[str]:
    sync_result = _related_notes_sync_result(related_notes_sync)
    if blocked_reason == "related_notes_blocked" and related_notes_sync_blocked(sync_result):
        if sync_result.required_inputs:
            return list(sync_result.required_inputs)
        return list(RELATED_NOTES_REQUIRED_INPUTS)
    return list(FIX_WIKI_REQUIRED_INPUTS)


def _related_notes_public_next_action(
    *,
    related_notes_export_recovery: object,
    related_notes_sync: object,
) -> str:
    recovery_fields = _RelatedNotesRecoveryPayloadFields.model_validate(
        _json_object_or_empty(related_notes_export_recovery)
    )
    recovery_state = _related_notes_recovery_state(related_notes_export_recovery)
    sync_result = _related_notes_sync_result(related_notes_sync)
    recovery_action = (recovery_fields.next_action or (recovery_state.next_action if recovery_state is not None else "")).strip()
    if recovery_action and not _looks_like_internal_cli_action(recovery_action):
        return recovery_action
    sync_action = sync_result.next_action.strip()
    if sync_action and not _looks_like_internal_cli_action(sync_action):
        return sync_action
    blocked_reason = (
        recovery_fields.blocked_reason
        or (recovery_state.blocked_reason if recovery_state is not None else "")
        or sync_result.blocked_reason
        or "related_notes_blocked"
    )
    if blocked_reason in {
        "related_notes_hash_mismatch",
        "related_notes_export_stale",
        "related_notes_export_still_stale",
        "related_notes_headless_embedding_failed",
    }:
        return "Atualizar o export das Notas Relacionadas (Related Notes) pela rota oficial e repetir a correção da Wiki."
    if blocked_reason == "related_notes_headless_time_budget_exhausted":
        return "Aguardar a janela de atualização permitir a retomada e repetir a atualização das Notas Relacionadas pela rota oficial."
    if blocked_reason == "related_notes_headless_quota_exhausted":
        return "Aguardar a quota de embeddings do Gemini voltar e repetir a atualização das Notas Relacionadas pela rota oficial."
    if blocked_reason in {"obsidian_cli_unavailable", "obsidian_not_ready", "obsidian_cli_timeout"}:
        return "Abrir/configurar o Obsidian CLI para atualizar o export das Notas Relacionadas e repetir a correção da Wiki."
    return "Resolver o bloqueio das Notas Relacionadas e repetir a correção da Wiki pela rota oficial."


def _related_notes_recovery_state(payload: object | None) -> RelatedNotesRecoveryState | None:
    if isinstance(payload, RelatedNotesRecoveryState):
        return payload if payload else None
    if isinstance(payload, LinkRelatedSyncResult):
        state = payload.related_notes_recovery_state
        return state if state else None
    if not isinstance(payload, dict):
        return None
    fields = _RelatedNotesRecoveryPayloadFields.model_validate(payload)
    if fields.related_notes_recovery_state:
        recovery_state = RelatedNotesRecoveryState.from_payload(fields.related_notes_recovery_state)
        return recovery_state if recovery_state else None
    headless = fields.headless_export
    if not headless:
        return None
    if not _json_field(headless, "partial_record_count"):
        return None
    return RelatedNotesRecoveryState.from_headless_projection(headless, blocked_reason=fields.blocked_reason)


def _related_notes_recovery_state_payload(state: RelatedNotesRecoveryState | None) -> JsonObject:
    if state is None:
        return {}
    payload = state.to_payload()
    payload.pop("operation_payload", None)
    return payload


def _related_notes_recovery_payload(payload: object | None) -> JsonObject:
    return _related_notes_recovery_state_payload(_related_notes_recovery_state(payload))


def _related_notes_recovery_can_continue(payload: object | None) -> bool:
    state = _related_notes_recovery_state(payload)
    if not state:
        return False
    return state.blocked_reason in RELATED_NOTES_EXTERNAL_RETRY_REASONS and state.resume_supported


def _looks_like_internal_cli_action(value: str) -> bool:
    return any(token in value for token in ("uv run", ".py", "related-notes-sync", "run-linker", "--json", "--recover-export"))


def _try_write_json_file(path: Path, data: JsonObject) -> str | None:
    try:
        _write_json_file(path, data)
    except (FileWriteError, OSError) as exc:
        return str(exc)
    return None


def _compact_consumed_linker_artifacts(
    *,
    diagnosis_path: Path | None,
    receipt_path: Path | None,
    additional_diagnosis_paths: Sequence[Path] = (),
) -> JsonObject:
    artifacts: list[JsonObject] = []
    seen_paths: set[Path] = set()
    for role, candidate in (
        *[("diagnosis", path) for path in (diagnosis_path, *additional_diagnosis_paths)],
        ("receipt", receipt_path),
    ):
        if candidate is None:
            continue
        try:
            identity = candidate.resolve()
        except OSError:
            identity = candidate.absolute()
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        artifacts.append(_compact_consumed_linker_artifact(path=candidate, artifact_role=role))
    compacted_count = sum(1 for artifact in artifacts if _json_field(artifact, "status") == "compacted")
    failed_count = sum(1 for artifact in artifacts if _json_field(artifact, "status") == "write_failed")
    bytes_saved = sum(_payload_int(artifact, "bytes_saved") for artifact in artifacts)
    status = "completed" if compacted_count else "skipped"
    if failed_count:
        status = "completed_with_warnings" if compacted_count else "failed"
    return _json_object({
        "schema": "medical-notes-workbench.consumed-link-artifacts-compaction.v1",
        "status": status,
        "artifact_count": len(artifacts),
        "compacted_count": compacted_count,
        "failed_count": failed_count,
        "bytes_saved": bytes_saved,
        "artifacts": artifacts,
    })


def _compact_consumed_linker_artifact(*, path: Path, artifact_role: str) -> JsonObject:
    if not path.exists():
        return _json_object({
            "schema": "medical-notes-workbench.consumed-link-artifact-summary.v1",
            "artifact_role": artifact_role,
            "path": str(path),
            "status": "skipped",
            "skipped_reason": "artifact_missing",
            "bytes_saved": 0,
        })
    original_bytes = path.read_bytes()
    original_size = len(original_bytes)
    original_hash = "sha256:" + hashlib.sha256(original_bytes).hexdigest()
    try:
        payload = json.loads(original_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _json_object({
            "schema": "medical-notes-workbench.consumed-link-artifact-summary.v1",
            "artifact_role": artifact_role,
            "path": str(path),
            "status": "skipped",
            "skipped_reason": "artifact_json_invalid",
            "error": str(exc),
            "original_size_bytes": original_size,
            "original_sha256": original_hash,
            "bytes_saved": 0,
        })
    if not isinstance(payload, dict):
        return _json_object({
            "schema": "medical-notes-workbench.consumed-link-artifact-summary.v1",
            "artifact_role": artifact_role,
            "path": str(path),
            "status": "skipped",
            "skipped_reason": "artifact_not_object",
            "original_size_bytes": original_size,
            "original_sha256": original_hash,
            "bytes_saved": 0,
        })
    payload_obj = _json_object(payload)
    payload_fields = _ConsumedLinkArtifactFields.model_validate(payload_obj)
    if payload_fields.schema_id == "medical-notes-workbench.consumed-link-artifact-summary.v1":
        return _json_object({
            **payload_obj,
            "status": "already_compacted",
            "bytes_saved": 0,
        })
    summary = _consumed_link_artifact_summary(
        payload_obj,
        artifact_role=artifact_role,
        path=path,
        original_size=original_size,
        original_hash=original_hash,
    )
    try:
        _write_json_file(path, summary)
    except (FileWriteError, OSError) as exc:
        return _json_object({
            **summary,
            "status": "write_failed",
            "write_error": str(exc),
            "bytes_saved": 0,
        })
    compacted_size = path.stat().st_size
    summary["compacted_size_bytes"] = compacted_size
    summary["bytes_saved"] = max(0, original_size - compacted_size)
    _write_json_file(path, summary)
    return summary


def _consumed_link_artifact_summary(
    payload: JsonObject,
    *,
    artifact_role: str,
    path: Path,
    original_size: int,
    original_hash: str,
) -> JsonObject:
    fields = _ConsumedLinkArtifactFields.model_validate(_consumed_link_artifact_source(payload))
    return _json_object({
        "schema": "medical-notes-workbench.consumed-link-artifact-summary.v1",
        "artifact_role": artifact_role,
        "path": str(path),
        "status": "compacted",
        "consumed_by": "fix_wiki_apply",
        "reusable_for_apply": False,
        "reuse_blocked_reason": "artifact_payload_was_consumed_and_compacted_after_fix_wiki_apply",
        "original_schema": fields.schema_id,
        "original_phase": fields.phase,
        "original_status": fields.status,
        "original_blocked_reason": fields.blocked_reason,
        "original_next_action": fields.next_action,
        "diagnosis_path": fields.diagnosis_path,
        "receipt_path": fields.receipt_path,
        "plan_hash": fields.plan_hash,
        "snapshot_hash": fields.snapshot_hash,
        "original_size_bytes": original_size,
        "original_sha256": original_hash,
        "compacted_size_bytes": 0,
        "bytes_saved": 0,
        "counts": _consumed_link_artifact_counts(payload),
        "next_action": (
            "Use o relatório compacto do fix-wiki para auditoria; reexecute /mednotes:link se precisar de novo diagnóstico aplicável."
        ),
    })


def _consumed_link_artifact_counts(payload: JsonObject) -> JsonObject:
    fields = _ConsumedLinkArtifactFields.model_validate(_consumed_link_artifact_source(payload))
    return _json_object({
        "links_planned": fields.links_planned or fields.body_term_linker.links_planned,
        "links_rewritten": fields.links_rewritten or fields.body_term_linker.links_rewritten,
        "blocker_count": fields.blocker_count,
        "body_item_count": len(fields.body_term_linker.plans),
        "changed_file_count": max(
            fields.changed_file_count,
            fields.files_changed,
            len(fields.changed_files),
        ),
        "changed_operation_count": len(fields.file_changes),
        "related_notes_applied_note_count": fields.related_notes_sync.applied_note_count,
        "related_notes_planned_note_count": fields.related_notes_sync.planned_note_count,
        "related_notes_changed_note_count": len(fields.related_notes_sync.updates),
        "graph_error_count": fields.graph_audit_after.error_count or fields.graph_audit_before.error_count,
        "graph_blocker_count": fields.graph_audit_after.blocker_count or fields.graph_audit_before.blocker_count,
        "graph_orphan_count": fields.graph_audit_after.orphan_count or fields.graph_audit_before.orphan_count,
    })


def _path_from_payload(payload: object, key: str) -> Path | None:
    if not isinstance(payload, dict):
        return None
    value = _json_field(_json_object(payload), key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return Path(stripped) if stripped else None


def _ensure_declared_artifact_exists(payload: JsonObject, *, fallback_path: Path | None = None) -> str:
    declared_value = _json_field(payload, "diagnosis_path")
    declared_path = declared_value if isinstance(declared_value, str) else ""
    if not declared_path and fallback_path is not None:
        declared_path = str(fallback_path)
        payload["diagnosis_path"] = declared_path
    if not declared_path:
        return ""
    path = Path(declared_path)
    if path.exists():
        return ""
    error = _try_write_json_file(path, payload)
    if error:
        payload["diagnosis_write_error"] = error
        return error
    return ""


def _relative_note_path(path: Path, wiki_dir: Path) -> str:
    try:
        return path.resolve().relative_to(wiki_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _note_hash(path: Path) -> str:
    return "sha256:" + file_sha256(path) if path.is_file() else ""


def _note_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or path.stem
    except OSError:
        pass
    return path.stem


def _sync_vocabulary_note_hashes_after_style(config: MedConfig, style_fix: JsonObject, *, apply: bool) -> JsonObject:
    if not apply:
        return {"status": "skipped", "skipped_reason": "preview", "synced_count": 0, "missing_count": 0}
    db_path = config.vocabulary_db_path
    if db_path is None or not db_path.is_file():
        return {"status": "skipped", "skipped_reason": "vocabulary_db_missing", "synced_count": 0, "missing_count": 0}
    reports = _FixWikiReportCollectionFields.model_validate(style_fix)
    changed_paths = [_path(report.path) for report in reports.reports if report.wrote and report.path]
    if not changed_paths:
        return {"status": "completed", "synced_count": 0, "missing_count": 0}
    synced_count = 0
    missing_count = 0
    with sqlite3.connect(db_path) as conn:
        for path in changed_paths:
            content_hash = _note_hash(path)
            if not content_hash:
                continue
            cursor = conn.execute(
                """
                UPDATE notes
                SET content_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE path = ? AND status = 'active'
                """,
                (content_hash, str(path)),
            )
            if cursor.rowcount:
                synced_count += 1
            else:
                missing_count += 1
    return {
        "status": "completed",
        "synced_count": synced_count,
        "missing_count": missing_count,
    }


def _add_modified_note_event(events: list[JsonObject], path: Path, wiki_dir: Path, *, reason: str) -> None:
    if not path.is_file():
        return
    rel = _relative_note_path(path, wiki_dir)
    for event in events:
        fields = _ModifiedNoteEventFields.model_validate(event)
        if fields.path != rel or fields.change_type != "modified":
            continue
        reasons = [item for item in fields.reasons if item]
        if not reasons and fields.reason:
            reasons = [fields.reason]
        if reason not in reasons:
            reasons.append(reason)
        event["reason"] = ",".join(reasons)
        event["reasons"] = reasons
        event["after_hash"] = _note_hash(path)
        return
    events.append(
        {
            "change_type": "modified",
            "content_change": "text",
            "path": rel,
            "title": _note_title(path),
            "after_hash": _note_hash(path),
            "reason": reason,
            "reasons": [reason],
        }
    )


def _fix_wiki_link_trigger_context(
    *,
    config: MedConfig,
    run_id: str,
    style_fix: JsonObject,
    sources_backfill: JsonObject,
    alias_projection_apply: JsonObject | None,
    graph_fix: JsonObject,
    taxonomy_apply: JsonObject | None,
) -> JsonObject | None:
    events: list[JsonObject] = []
    style_reports = _FixWikiReportCollectionFields.model_validate(style_fix)
    for report in style_reports.reports:
        if report.wrote and report.path:
            _add_modified_note_event(events, _path(report.path), config.wiki_dir, reason="style_fix")
    provenance_reports = _FixWikiReportCollectionFields.model_validate(sources_backfill)
    for report in provenance_reports.reports:
        if report.wrote and report.path:
            _add_modified_note_event(events, _path(report.path), config.wiki_dir, reason="provenance_backfill")
    alias_apply = _AliasProjectionApplyFields.model_validate(_json_object_or_empty(alias_projection_apply))
    for receipt_payload in alias_apply.receipts:
        if (
            receipt_payload.status == "applied"
            and receipt_payload.before_hash != receipt_payload.after_hash
            and receipt_payload.note_path
        ):
            _add_modified_note_event(events, _path(receipt_payload.note_path), config.wiki_dir, reason="alias_projection")
    graph_fields = _GraphFixTriggerFields.model_validate(graph_fix)
    for report in graph_fields.reports:
        if report.wrote and report.path:
            _add_modified_note_event(events, _path(report.path), config.wiki_dir, reason="graph_fix")
    for report in graph_fields.duplicates.reports:
        keep = report.keep
        for removed in report.removed:
            old_path = removed
            keep_path = config.wiki_dir / keep if keep else None
            events.append(
                {
                    "change_type": "merged",
                    "content_change": "structural",
                    "old_path": old_path,
                    "old_title": Path(old_path).stem,
                    "replacement_path": keep,
                    "replacement_title": keep_path.stem if keep_path else Path(old_path).stem,
                    "reason": "exact_duplicate_removed",
                }
            )
    taxonomy_apply_fields = _TaxonomyApplyFields.model_validate(_json_object_or_empty(taxonomy_apply))
    for op in taxonomy_apply_fields.applied_operations:
        if op.action != "move_dir":
            continue
        source_rel = op.source
        destination_rel = op.destination
        destination_dir = config.wiki_dir.joinpath(*Path(destination_rel).parts)
        if not source_rel or not destination_rel or not destination_dir.is_dir():
            continue
        for note in sorted(destination_dir.rglob("*.md")):
            dest_rel = _relative_note_path(note, config.wiki_dir)
            try:
                suffix = note.relative_to(destination_dir).as_posix()
            except ValueError:
                suffix = note.name
            old_rel = (Path(source_rel) / suffix).as_posix()
            events.append(
                {
                    "change_type": "moved",
                    "content_change": "structural",
                    "old_path": old_rel,
                    "old_title": note.stem,
                    "path": dest_rel,
                    "title": _note_title(note),
                    "after_hash": _note_hash(note),
                    "reason": "taxonomy_migration",
                }
            )
    if not events:
        return None
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": "/mednotes:fix-wiki",
        "batch_id": run_id,
        "changed_notes": events,
    }


def _quote_arg(value: str | Path) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _fix_wiki_command(config: MedConfig, *, apply: bool, backup: bool, apply_taxonomy: bool = False) -> str:
    flags = ["--wiki-dir", _quote_arg(config.wiki_dir), "fix-wiki"]
    flags.append("--apply" if apply else "--dry-run")
    if apply_taxonomy:
        flags.append("--apply-taxonomy")
    flags.append("--json")
    return wiki_cli_base_command() + " " + " ".join(str(flag) for flag in flags)


def _rollback_command(config: MedConfig, receipt_path: str | None) -> str | None:
    if not receipt_path:
        return None
    return (
        wiki_cli_base_command() + " "
        f"--wiki-dir {_quote_arg(config.wiki_dir)} "
        f"taxonomy-migrate --rollback --receipt {_quote_arg(receipt_path)}"
    )


def _backup_linker_planned_changes(config: MedConfig, linker_diagnosis: JsonObject, backup: bool) -> list[str]:
    return []


def _issue_sample(issues: list[JsonObject], limit: int = 5) -> list[JsonObject]:
    sample: list[JsonObject] = []
    for issue in issues[:limit]:
        fields = _GraphIssueFields.model_validate(issue)
        sample.append(_contract_payload(fields))
    return sample


def _issues_by_code(issues: list[JsonObject]) -> dict[str, list[JsonObject]]:
    grouped: dict[str, list[JsonObject]] = {}
    for issue in issues:
        code = _GraphIssueFields.model_validate(issue).code or "unknown"
        grouped.setdefault(code, []).append(issue)
    return grouped


def _blocker_resolution_plan(
    *,
    apply: bool,
    graph_audit_report: JsonObject,
    write_errors: list[JsonObject],
    rewrite_plan: JsonObject | None,
    taxonomy_plan: JsonObject,
    taxonomy_issue_count: int,
    taxonomy_requires_explicit_apply: bool = False,
    suppress_graph_link_repair: bool = False,
) -> JsonObject:
    graph_report = _GraphAuditErrorsFields.model_validate(graph_audit_report)
    graph_errors = list(graph_report.errors)
    by_code = _issues_by_code(graph_errors)
    taxonomy_fields = _TaxonomyPlanFields.model_validate(taxonomy_plan)
    taxonomy_operations = list(taxonomy_fields.operations)
    taxonomy_blocked = list(taxonomy_fields.blocked_items)
    groups: list[_BlockerResolutionGroupPayload] = []

    def add_group(
        route: str,
        *,
        count: int,
        automatic: bool,
        reason: str,
        next_action: str,
        codes: list[str] | None = None,
        sample: list[JsonObject] | None = None,
        planned_item_count: int | None = None,
        total_available_count: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        if count <= 0:
            return
        groups.append(
            _BlockerResolutionGroupPayload(
                route=route,
                count=count,
                automatic=automatic,
                reason=reason,
                next_action=next_action,
                codes=codes or [],
                sample=sample or [],
                planned_item_count=planned_item_count,
                total_available_count=total_available_count,
                truncated=truncated,
            )
        )

    add_group(
        "io_retry",
        count=len(write_errors),
        automatic=False,
        reason="Arquivos bloqueados para escrita impedem confirmar reparos antes do linker.",
        next_action="Liberar o arquivo bloqueado e repetir o reparo da Wiki pela rota oficial.",
        sample=write_errors[:5],
    )

    deterministic_codes = ["dangling_link", "self_link", "ambiguous_link"]
    deterministic_issues = [issue for code in deterministic_codes for issue in by_code.get(code, [])]
    add_group(
        "graph_link_repair",
        count=0 if suppress_graph_link_repair else len(deterministic_issues),
        automatic=True,
        reason="O grafo ainda tem WikiLinks quebrados, ambíguos ou autorreferências; repetir o mesmo apply sem plano novo vira loop.",
        next_action="Corrigir os WikiLinks indicados pelo auditor de grafo e repetir a conferência da Wiki pela rota oficial.",
        codes=deterministic_codes,
        sample=_issue_sample(deterministic_issues),
    )

    duplicate_issues = by_code.get("duplicate_stem", [])
    add_group(
        "title_driven_merge_review",
        count=len(duplicate_issues),
        automatic=False,
        reason="Títulos/stems duplicados são evidência de higiene, mas não autorizam merge automático.",
        next_action="Confirmar identidade semântica via vocabulary DB/curator ou decisão humana; só então gerar note-merge-plan.v1 e aplicar com apply-note-merge.",
        codes=["duplicate_stem"],
        sample=_issue_sample(duplicate_issues),
    )

    catalog_codes = [
        "catalog_invalid_json",
        "catalog_entry_missing_target",
        "catalog_target_missing",
        "catalog_target_ambiguous",
        "alias_conflict",
    ]
    catalog_issues = [issue for code in catalog_codes for issue in by_code.get(code, [])]
    add_group(
        "catalog_repair",
        count=len(catalog_issues),
        automatic=True,
        reason="O blocker está na identidade de vocabulário/alias, não na nota; o linker precisa de alvo canônico sem ausência ou ambiguidade.",
        next_action="Preparar curadoria de vocabulário, validar o lote, aplicar somente saídas aprovadas e repetir a conferência da Wiki.",
        codes=catalog_codes,
        sample=_issue_sample(catalog_issues),
    )

    other_issues = [
        issue
        for issue in graph_errors
        if _json_field(issue, "code") not in {*deterministic_codes, "duplicate_stem", *catalog_codes}
    ]
    add_group(
        "unknown_graph_blocker",
        count=len(other_issues),
        automatic=False,
        reason="Tipo de blocker ainda não tem reparo determinístico conhecido.",
        next_action="Inspecionar o sample, corrigir a causa e adicionar reparo determinístico se for recorrente.",
        sample=_issue_sample(other_issues),
    )

    planned_rewrite_count = 0
    rewrite_count = 0
    rewrite_truncated = False
    if isinstance(rewrite_plan, dict):
        rewrite_fields = _RewritePlanCountFields.model_validate(rewrite_plan)
        planned_rewrite_count = rewrite_fields.item_count
        rewrite_count = rewrite_fields.total_available_count or planned_rewrite_count
        rewrite_truncated = rewrite_fields.truncated
    add_group(
        "style_rewrite",
        count=rewrite_count,
        automatic=True,
        reason="A nota precisa de reescrita estrutural; fix-note não deve inventar seções clínicas ausentes.",
        next_action=(
            "Preparar uma reescrita assistida das notas inválidas, aplicar somente as versões validadas "
            "e repetir a conferência da Wiki."
        ),
        planned_item_count=planned_rewrite_count,
        total_available_count=rewrite_count,
        truncated=rewrite_truncated,
    )

    if taxonomy_operations and (not apply or taxonomy_requires_explicit_apply):
        taxonomy_reason = (
            "Há uma reorganização de pastas pronta, mas ela exige confirmação explícita antes de alterar a Wiki."
            if taxonomy_requires_explicit_apply
            else "Há uma reorganização de pastas planejada; revise o plano antes de autorizar a mudança."
        )
        taxonomy_next_action = (
            "Revisar o plano de organização; se estiver correto, autorizar a reorganização de pastas pela rota oficial."
            if taxonomy_requires_explicit_apply
            else "Revisar o plano de organização; se estiver correto, repetir o reparo com reorganização autorizada."
        )
        add_group(
            "taxonomy_migrate",
            count=len(taxonomy_operations),
            automatic=not taxonomy_requires_explicit_apply,
            reason=taxonomy_reason,
            next_action=taxonomy_next_action,
            sample=[
                {
                    "source": item.source,
                    "destination": item.destination,
                    "reason": item.reason,
                }
                for item in taxonomy_operations[:5]
            ],
        )
    add_group(
        "taxonomy_review_required",
        count=len(taxonomy_blocked),
        automatic=False,
        reason="A taxonomia restante não tem destino único seguro.",
        next_action="Revisar os itens do sample, resolver a classificação e repetir a conferência da Wiki pela rota oficial.",
        sample=[
            {
                "source": item.source,
                "destination": item.destination,
                "reason": item.blocked_reason or item.reason,
            }
            for item in taxonomy_blocked[:5]
        ],
    )

    linker_blocking_groups = [group for group in groups if group.route != "style_rewrite"]
    preferred_next_action = linker_blocking_groups[0].next_action if linker_blocking_groups else ""
    if not preferred_next_action and rewrite_count:
        preferred_next_action = next(
            (group.next_action for group in groups if group.route == "style_rewrite"),
            "",
        )
    group_payloads = [group.to_payload() for group in groups]

    return _json_object({
        "schema": BLOCKER_RESOLUTION_SCHEMA,
        "remaining_graph_blocker_count": len(graph_errors),
        "write_error_count": len(write_errors),
        "requires_llm_rewrite_count": rewrite_count,
        "taxonomy_issue_count": taxonomy_issue_count,
        "taxonomy_operation_count": len(taxonomy_operations),
        "taxonomy_blocked_count": len(taxonomy_blocked),
        "group_count": len(groups),
        "groups": group_payloads,
        "has_blockers": bool(groups),
        "linker_blocking_group_count": len(linker_blocking_groups),
        "linker_can_apply": not linker_blocking_groups,
        "next_action": preferred_next_action or (groups[0].next_action if groups else ""),
    })


def _taxonomy_problems(taxonomy_plan: JsonObject, *, decision_required: bool) -> list[JsonObject]:
    problems: list[JsonObject] = []
    fields = _TaxonomyPlanFields.model_validate(taxonomy_plan)
    operations = list(fields.operations)
    blocked = list(fields.blocked_items)
    if operations and decision_required:
        problems.append(
            build_problem(
                domain="structure",
                code="structure.taxonomy.moves_require_approval",
                severity="high",
                problem="Taxonomy plan contains folder moves that change vault paths.",
                recommendation="Review the full folder hierarchy before approving taxonomy moves.",
                risk="Changes paths and requires linker reference repair after move.",
                status="needs_decision",
                decision_required=True,
                recommended_action="approve_taxonomy_moves",
                resolver="taxonomy",
                context_packet="structure-context-packet.md",
                evidence={"operation_count": len(operations)},
                linker_trigger_after_resolve=True,
            )
        )
    for item in blocked:
        source = item.source
        problems.append(
            build_problem(
                domain="structure",
                code=f"structure.taxonomy.{item.blocked_reason or 'blocked'}",
                severity="high",
                problem="Taxonomy plan contains a blocked folder operation.",
                recommendation="Resolve the blocked taxonomy item before moving any folder.",
                risk="Partial taxonomy moves can corrupt paths and graph references.",
                status="blocked",
                decision_required=True,
                recommended_action="resolve_taxonomy_blocker",
                resolver="taxonomy",
                context_packet="structure-context-packet.md",
                evidence={"path": source, "blocked_reason": item.blocked_reason},
                linker_trigger_after_resolve=True,
            )
        )
    return problems


def _structure_hygiene_problems(hygiene: JsonObject) -> list[JsonObject]:
    fields = _HygieneReportFields.model_validate(hygiene)
    empty_dirs = fields.empty_dirs
    empty_root_notes = fields.empty_root_note_count
    problems: list[JsonObject] = []
    if empty_dirs:
        problems.append(
            build_problem(
                domain="structure",
                code="structure.empty_dir.present",
                severity="low",
                problem="The vault contains empty folders that should be removed from the taxonomy tree.",
                recommendation="Remove empty folders with the structure cleanup phase and keep attachments/plugin folders ignored.",
                risk="Empty folders make the taxonomy tree harder to inspect and can mislead future routing.",
                status="diagnosed",
                can_autofix=True,
                resolver="structure_empty_dir_cleanup",
                context_packet="structure-context-packet.md",
                evidence={
                    "count": empty_dirs,
                    "paths": fields.empty_dir_paths,
                },
            )
        )
    if empty_root_notes:
        problems.append(
            build_problem(
                domain="structure",
                code="structure.empty_root_note.present",
                severity="low",
                problem="The vault contains root-level Markdown files with no content.",
                recommendation="Archive strictly empty root notes before style, taxonomy and graph phases inspect the vault.",
                risk="Treating an empty root file as a clinical note creates false rewrite and taxonomy blockers.",
                status="diagnosed",
                can_autofix=True,
                resolver="structure_empty_root_note_cleanup",
                context_packet="structure-context-packet.md",
                evidence={
                    "count": empty_root_notes,
                    "paths": fields.empty_root_note_samples,
                },
            )
        )
    return problems


def _hygiene_cleanup_needed(hygiene: JsonObject) -> bool:
    fields = _HygieneReportFields.model_validate(hygiene)
    # Retired Markdown backups are diagnosis-only in public workflows. They
    # must not trigger automatic cleanup because vault restore points replaced
    # adjacent .bak mutation as the rollback contract.
    return bool(fields.rewrite_file_count or fields.empty_dirs or fields.empty_root_note_count)


def _identity_problems(
    hygiene: JsonObject,
    *,
    wiki_dir: Path,
    vocabulary_map_diagnosis: JsonObject | None = None,
) -> list[JsonObject]:
    fields = _HygieneReportFields.model_validate(hygiene)
    problems: list[JsonObject] = []
    duplicate_hash_groups = fields.duplicate_hash_groups
    duplicate_filename_groups = fields.duplicate_filename_groups
    if duplicate_hash_groups:
        problems.append(
            build_problem(
                domain="identity",
                code="identity.duplication.identical_notes",
                severity="medium",
                problem="The vault contains identical note bodies in more than one Markdown file.",
                recommendation="Remove exact duplicates only when provenance and canonical target are preserved.",
                risk="Removing the wrong file can lose path/provenance references.",
                status="diagnosed",
                can_autofix=True,
                resolver="note_merge",
                context_packet="identity-context-packet.md",
                evidence={
                    "group_count": duplicate_hash_groups,
                    "samples": fields.duplicate_hash_samples,
                },
                linker_trigger_after_resolve=True,
            )
        )
    if duplicate_filename_groups:
        problems.extend(_duplicate_stem_identity_problems(hygiene, wiki_dir=wiki_dir))
    problems.extend(_vocabulary_identity_problems(vocabulary_map_diagnosis or {}))
    return problems


def _duplicate_stem_identity_problems(hygiene: JsonObject, *, wiki_dir: Path) -> list[JsonObject]:
    fields = _HygieneReportFields.model_validate(hygiene)
    buckets: dict[str, list[JsonObject]] = {
        "identity.duplication.same_chat_same_topic": [],
        "identity.duplication.same_meaning_different_sources": [],
        "identity.duplication.same_meaning_multiple_notes": [],
    }
    for group in fields.duplicate_filename_samples:
        files = list(group.files)
        urls = _chat_urls_for_relative_files(wiki_dir, files)
        enriched = _json_object({"files": files, "chat_original_urls": urls})
        if len(urls) == 1:
            buckets["identity.duplication.same_chat_same_topic"].append(enriched)
        elif len(urls) > 1:
            buckets["identity.duplication.same_meaning_different_sources"].append(enriched)
        else:
            buckets["identity.duplication.same_meaning_multiple_notes"].append(enriched)

    problem_specs = {
        "identity.duplication.same_chat_same_topic": (
            "Multiple notes share the same title/stem and came from the same source chat.",
            "Merge the repeated notes while preserving the original chat reference.",
            "Keeping both notes makes the canonical identity ambiguous; merging the wrong files can lose provenance.",
        ),
        "identity.duplication.same_meaning_different_sources": (
            "Multiple notes share the same title/stem but came from different source chats.",
            "Merge the repeated concept and preserve every source in consolidated provenance.",
            "A false merge can collapse distinct concepts; a missed merge leaves one meaning split across notes.",
        ),
        "identity.duplication.same_meaning_multiple_notes": (
            "Multiple notes share the same title/stem but provenance is incomplete.",
                "Confirm semantic identity before routing the group to note_merge.",
            "Missing provenance makes the merge harder to audit and requires conservative validation.",
        ),
    }
    problems: list[JsonObject] = []
    for code, samples in buckets.items():
        if not samples:
            continue
        problem, recommendation, risk = problem_specs[code]
        problems.append(
            build_problem(
                domain="identity",
                code=code,
                severity="high",
                problem=problem,
                recommendation=recommendation,
                risk=risk,
                status="needs_decision",
                decision_required=True,
                recommended_action="note_merge",
                resolver="note_merge",
                context_packet="identity-context-packet.md",
                evidence={"group_count": len(samples), "samples": samples[:10]},
                linker_trigger_after_resolve=True,
            )
        )
    return problems


def _vocabulary_identity_problems(vocabulary_map_diagnosis: JsonObject) -> list[JsonObject]:
    fields = _VocabularyMapDiagnosisFields.model_validate(vocabulary_map_diagnosis)
    problems: list[JsonObject] = []
    duplicate_issues = [issue.to_payload() for issue in fields.issues if issue.code == "vocabulary_map.duplicate_meaning"]
    non_atomic_issues = [issue.to_payload() for issue in fields.issues if issue.code == "vocabulary_map.non_atomic_note"]
    if duplicate_issues:
        problems.append(
            build_problem(
                domain="identity",
                code="identity.duplication.same_meaning_multiple_notes",
                severity="high",
                problem="The vocabulary DB marks one meaning as duplicated across the vault.",
                recommendation="Create a note-merge plan for the affected meaning and preserve provenance.",
                risk="The graph cannot choose a single canonical target while one meaning maps to multiple notes.",
                status="needs_decision",
                decision_required=True,
                recommended_action="note_merge",
                resolver="note_merge",
                context_packet="identity-context-packet.md",
                evidence={"source": "vocabulary_map", "issues": duplicate_issues[:10]},
                linker_trigger_after_resolve=True,
            )
        )
    if non_atomic_issues:
        problems.append(
            build_problem(
                domain="identity",
                code="identity.atomicity.one_note_multiple_meanings",
                severity="high",
                problem="The vocabulary DB marks a note or meaning as non-atomic.",
                recommendation="Split or rewrite so each canonical note represents exactly one meaning.",
                risk="A non-atomic note makes aliases, body links and Related Notes semantically unsafe.",
                status="needs_decision",
                decision_required=True,
                recommended_action="split_or_rewrite_note",
                resolver="note_merge",
                context_packet="identity-context-packet.md",
                evidence={"source": "vocabulary_map", "issues": non_atomic_issues[:10]},
                linker_trigger_after_resolve=True,
            )
        )
    return problems


def _chat_urls_for_relative_files(wiki_dir: Path, files: list[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for rel in files:
        path = wiki_dir / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        state = classify_note_provenance(text)
        for source in [*state.chat_ids, *state.legacy_urls]:
            chat_id = ChatProvenance(str(source)).id
            if not chat_id:
                continue
            url = f"https://gemini.google.com/app/{chat_id}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _content_problems(style_audit: JsonObject) -> list[JsonObject]:
    style_fields = _StyleAuditReportsFields.model_validate(style_audit)
    rewrite_reports = [
        report
        for report in style_fields.reports
        if report.requires_llm_rewrite
    ]
    if not rewrite_reports:
        return []
    return [
        build_problem(
            domain="content",
            code="content.lint.requires_rewrite",
            severity="high",
            problem="One or more notes fail deterministic style validation and require controlled rewrite.",
            recommendation="Send only the listed paths and rewrite instructions to the style-rewrite route.",
            risk="Rewrite must preserve YAML, footer, images, embeds, code blocks and provenance.",
            status="needs_decision",
            decision_required=True,
            recommended_action="style_rewrite",
            resolver="style_rewrite",
            context_packet="content-context-packet.md",
            evidence={
                "count": len(rewrite_reports),
                "paths": [report.path for report in rewrite_reports[:10]],
                "codes": sorted(
                    {
                        error.code
                        for report in rewrite_reports
                        for error in report.errors
                        if error.code
                    }
                ),
            },
        )
    ]


class _LinkerPhaseBlockerProjection(ContractModel):
    """Typed slice of a linker phase used to preserve the real blocker cause."""

    blocked_reason: str = ""
    skipped_reason: str = ""

    @property
    def reason(self) -> str:
        return self.blocked_reason.strip() or self.skipped_reason.strip()


class _LinkerPlanPhasesBlockerProjection(ContractModel):
    """Minimal phase map consumed by fix-wiki without owning linker state."""

    body_term_linker: _LinkerPhaseBlockerProjection | None = None
    contextual_alias_disambiguation: _LinkerPhaseBlockerProjection | None = None
    related_notes_sync: _LinkerPhaseBlockerProjection | None = None
    graph_validation: _LinkerPhaseBlockerProjection | None = None

    def ordered_reasons(self) -> list[str]:
        phases = (
            self.body_term_linker,
            self.contextual_alias_disambiguation,
            self.related_notes_sync,
            self.graph_validation,
        )
        return [phase.reason for phase in phases if phase is not None and phase.reason]


class _LinkerPlanBlockerProjection(ContractModel):
    phases: _LinkerPlanPhasesBlockerProjection | None = None


class _LinkerVocabularyBootstrapProjection(ContractModel):
    status: str = ""


class _LinkerDiagnosisBlockerProjection(ContractModel):
    """Narrow linker diagnosis contract allowed to drive fix-wiki UX."""

    blocked_reason: str = ""
    plan: _LinkerPlanBlockerProjection | None = None
    body_term_linker: _LinkerPhaseBlockerProjection | None = None
    contextual_alias_disambiguation: _LinkerPhaseBlockerProjection | None = None
    related_notes_sync: _LinkerPhaseBlockerProjection | None = None
    vocabulary_bootstrap: _LinkerVocabularyBootstrapProjection | None = None


def _string_payload_field(source: Mapping[str, object], key: str) -> str:
    value = source[key] if key in source else ""
    return value if isinstance(value, str) else ""


def _phase_blocker_projection_payload(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    source = _json_object(value)
    payload: JsonObject = {}
    for key in ("blocked_reason", "skipped_reason"):
        text = _string_payload_field(source, key)
        if text:
            payload[key] = text
    return payload


def _vocabulary_bootstrap_projection_payload(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    source = _json_object(value)
    status = _string_payload_field(source, "status")
    return {"status": status} if status else {}


def _linker_plan_blocker_projection_payload(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    source = _json_object(value)
    phases_value = source["phases"] if "phases" in source else {}
    if not isinstance(phases_value, dict):
        return {}
    phases_source = _json_object(phases_value)
    phases_payload: JsonObject = {}
    for phase_key in (
        "body_term_linker",
        "contextual_alias_disambiguation",
        "related_notes_sync",
        "graph_validation",
    ):
        if phase_key in phases_source:
            phase_payload = _phase_blocker_projection_payload(phases_source[phase_key])
            if phase_payload:
                phases_payload[phase_key] = phase_payload
    return {"phases": phases_payload} if phases_payload else {}


def _nested_phase_blocker_projection_payload(source: JsonObject, parent_key: str, child_key: str) -> JsonObject:
    parent = source[parent_key] if parent_key in source else {}
    if not isinstance(parent, dict):
        return {}
    child = parent[child_key] if child_key in parent else {}
    return _phase_blocker_projection_payload(child)


def _linker_diagnosis_blocker_projection(linker_diagnosis: Mapping[str, object]) -> _LinkerDiagnosisBlockerProjection:
    """Validate the small linker slice that fix-wiki is allowed to interpret."""

    source = _json_object(dict(linker_diagnosis))
    payload: JsonObject = {}
    blocked_reason = _string_payload_field(source, "blocked_reason")
    if blocked_reason:
        payload.update({"blocked_reason": blocked_reason})
    for key in ("body_term_linker", "related_notes_sync"):
        if key in source:
            phase_payload = _phase_blocker_projection_payload(source[key])
            if phase_payload:
                payload[key] = phase_payload
    contextual_payload = _nested_phase_blocker_projection_payload(
        source,
        "body_term_linker",
        "contextual_alias_disambiguation",
    )
    if contextual_payload:
        payload["contextual_alias_disambiguation"] = contextual_payload
    if "vocabulary_bootstrap" in source:
        vocabulary_payload = _vocabulary_bootstrap_projection_payload(source["vocabulary_bootstrap"])
        if vocabulary_payload:
            payload["vocabulary_bootstrap"] = vocabulary_payload
    if "plan" in source:
        plan_payload = _linker_plan_blocker_projection_payload(source["plan"])
        if plan_payload:
            payload["plan"] = plan_payload
    return _LinkerDiagnosisBlockerProjection.model_validate(payload)


def _linker_specific_blocked_reason(linker_diagnosis: Mapping[str, object], *, fallback: str) -> str:
    """Prefer the actionable linker phase blocker over a generic plan blocker."""

    projection = _linker_diagnosis_blocker_projection(_consumed_link_artifact_source(_json_object(dict(linker_diagnosis))))
    if projection.plan is not None and projection.plan.phases is not None:
        plan_reasons = projection.plan.phases.ordered_reasons()
        if plan_reasons:
            return plan_reasons[0]
    if projection.contextual_alias_disambiguation is not None and projection.contextual_alias_disambiguation.reason:
        return projection.contextual_alias_disambiguation.reason
    for phase in (projection.body_term_linker, projection.related_notes_sync):
        if phase is not None and phase.reason:
            return phase.reason
    if projection.vocabulary_bootstrap is not None and projection.vocabulary_bootstrap.status == "planned":
        return "vocabulary_bootstrap_required"
    return projection.blocked_reason.strip() or fallback


def _knowledge_graph_problems(linker_diagnosis: JsonObject, linker_skipped_reason: str = "") -> list[JsonObject]:
    fields = _ConsumedLinkArtifactFields.model_validate(_consumed_link_artifact_source(linker_diagnosis))
    status = fields.status or "skipped"
    blocker_count = fields.blocker_count
    if status in {"diagnosis_ready", "skipped"} and not blocker_count and not linker_skipped_reason:
        return []
    return [
        build_problem(
            domain="knowledge_graph",
            code="knowledge_graph.linker.blocked" if blocker_count or linker_skipped_reason else "knowledge_graph.linker.diagnosed",
            severity="high" if blocker_count or linker_skipped_reason else "medium",
            problem="The graph state is owned by /mednotes:link and was diagnosed through the linker package.",
            recommendation="Use the saved link diagnosis/receipt from this run; do not repair graph links inside fix-wiki.",
            risk="Skipping the linker leaves WikiLinks, body links, Related Notes or index state stale.",
            status="blocked" if blocker_count or linker_skipped_reason else "diagnosed",
            can_autofix=not bool(blocker_count or linker_skipped_reason),
            resolver="run_linker",
            context_packet="knowledge_graph-context-packet.md",
            evidence={
                "status": status,
                "blocked_reason": linker_skipped_reason or fields.blocked_reason,
                "blocker_count": blocker_count,
                "link_diagnosis_path": fields.diagnosis_path,
            },
        )
    ]


def _finalize_fix_wiki_problems(
    problems: list[JsonObject],
    *,
    graph_error_count: int,
    linker_blocked: bool,
) -> list[JsonObject]:
    if graph_error_count or linker_blocked:
        return problems
    return [problem for problem in problems if _json_field(problem, "code") != "knowledge_graph.linker.blocked"]


def _delegated_graph_fix_report(config: MedConfig, linker_diagnosis: JsonObject | None = None) -> JsonObject:
    diagnosis = linker_diagnosis or {}
    reference_repair = _ReferenceRepairFields.model_validate(_json_field(diagnosis, "reference_repair", {}))
    affected_note_count = reference_repair.affected_note_count
    return _json_object({
        "schema": "medical-notes-workbench.wiki-graph-fix.v1",
        "wiki_dir": str(config.wiki_dir),
        "delegated_to": "/mednotes:link",
        "dry_run": True,
        "apply": False,
        "backup": False,
        "changed_count": affected_note_count,
        "written_count": 0,
        "write_error_count": 0,
        "write_errors": [],
        "backup_paths": [],
        "reports": [
            {
                "path": item.path,
                "relative_path": item.path,
                "changed": True,
                "would_write": False,
                "wrote": False,
                "backup": None,
                "write_error": None,
                "fixes_applied": [],
                "issue_codes": sorted({action.code for action in item.actions if action.code}),
                "delegated_to": "/mednotes:link/reference_repair",
            }
            for item in reference_repair.note_actions
        ],
        "duplicates": {
            "reports": [],
            "backup_paths": [],
            "write_errors": [],
            "removed_count": 0,
            "merge_required_count": 0,
        },
        "unresolved_blocker_count": 0,
    })


def fix_wiki_health(
    config: MedConfig,
    apply: bool = False,
    backup: bool = False,
    apply_taxonomy: bool = False,
    vocabulary_reset: bool = False,
    workflow_effect_executor: WorkflowEffectExecutor | None = None,
) -> JsonObject:
    backup = False
    markdown_node_runtime: JsonObject | None = None
    markdown_node_modules_path: Path | None = None
    run_id = _run_id()
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    archive_root = _archive_root(run_id)
    vocabulary_bootstrap_receipt = _VocabularyBootstrapReceiptFields.model_validate(
        resolve_vocabulary_bootstrap(
            config,
            apply=False,
            run_dir=run_dir,
            backup=backup,
            force_reset=vocabulary_reset,
            scan_notes=not apply,
        )
    )
    vocabulary_map_diagnosis = _vocabulary_map_diagnosis_payload(config, allow_create=False)
    vocabulary_map_fields = _VocabularyMapDiagnosisFields.model_validate(vocabulary_map_diagnosis)
    vocabulary_status = vocabulary_map_fields.status or "skipped"
    vocabulary_blocked_reason = _vocabulary_blocked_reason(vocabulary_status)
    vocabulary_apply_blocked = vocabulary_status in {"blocked_pending", "blocked_human"}
    vocabulary_structural_apply_blocked = vocabulary_status == "blocked_human"
    alias_projection_plan = _alias_projection_plan_payload(
        config,
        vocabulary_map_diagnosis,
        run_dir=run_dir,
        backup=backup,
    )
    alias_projection_apply = _alias_projection_apply_payload(apply=False, plan=alias_projection_plan, run_dir=run_dir)
    vocabulary_curator_batch_plan, vocabulary_curator_batch_plan_path = _vocabulary_curator_batch_plan_payload(
        config,
        vocabulary_map_diagnosis,
        run_id=run_id,
        run_dir=run_dir,
    )
    vocabulary_semantic_repair: JsonObject = {
        "schema": "medical-notes-workbench.vocabulary-semantic-repair.v1",
        "status": "skipped",
        "skipped_reason": "preview",
        "applied_count": 0,
        "blocked_count": 0,
    }

    hygiene_before = collect_wiki_hygiene(config.wiki_dir)
    dry_run_style_preview = (
        None
        if apply
        else FixWikiStyleResult.model_validate(fix_wiki_style(config.wiki_dir, apply=False, backup=False))
    )
    if apply:
        style_audit_initial = _deferred_initial_style_audit(config.wiki_dir)
    elif dry_run_style_preview is not None:
        style_audit_initial = _style_audit_from_reports(
            wiki_dir=config.wiki_dir,
            reports=dry_run_style_preview.to_audit_reports(),
        )
    else:
        style_audit_initial = _style_audit_from_reports(wiki_dir=config.wiki_dir, reports=[])
    taxonomy_initial_plan = taxonomy_migration_plan(config.wiki_dir)
    taxonomy_initial_fields = _TaxonomyPlanFields.model_validate(taxonomy_initial_plan)
    taxonomy_initial_operations = [item.model_dump(mode="json") for item in taxonomy_initial_fields.operations]
    taxonomy_initial_blocked = [item.model_dump(mode="json") for item in taxonomy_initial_fields.blocked_items]
    taxonomy_apply: JsonObject | None = None
    taxonomy_plan_path: Path | None = None
    taxonomy_receipt_path: Path | None = None
    taxonomy_apply_requires_confirmation = bool(apply and taxonomy_initial_operations and not apply_taxonomy)
    taxonomy_apply_skipped_reason = ""
    if taxonomy_initial_operations and not apply:
        taxonomy_apply_skipped_reason = "dry_run"
    elif taxonomy_apply_requires_confirmation:
        taxonomy_apply_skipped_reason = "apply_taxonomy_required"

    effective_apply = apply
    snapshot_hash = _wiki_snapshot_hash(config.wiki_dir)
    snapshot_files = collect_fix_wiki_snapshot_files(config.wiki_dir)
    fix_wiki_problems = [
        *_taxonomy_problems(
            taxonomy_initial_plan,
            decision_required=bool(taxonomy_initial_operations and not apply_taxonomy),
        ),
        *_structure_hygiene_problems(hygiene_before),
        *_identity_problems(
            hygiene_before,
            wiki_dir=config.wiki_dir,
            vocabulary_map_diagnosis=vocabulary_map_diagnosis,
        ),
        *_content_problems(style_audit_initial),
    ]
    deferred_work_items = _pending_deferred_work_items(config.vocabulary_db_path)
    context_packet_write_errors: list[dict[str, str]] = []
    try:
        context_packet_paths = write_context_packets(
            run_dir=run_dir,
            wiki_dir=config.wiki_dir,
            problems=fix_wiki_problems,
            taxonomy_plan=taxonomy_initial_plan,
            vocabulary_map_diagnosis=vocabulary_map_diagnosis,
        )
    except (FileWriteError, OSError) as exc:
        context_packet_paths = {}
        context_packet_write_errors.append({"phase": "context_packets", "error": str(exc)})
    fix_wiki_plan = build_fix_wiki_plan(
        run_id=run_id,
        wiki_dir=config.wiki_dir,
        snapshot_hash=snapshot_hash,
        vocabulary_status=vocabulary_status,
        problems=fix_wiki_problems,
        taxonomy_operations=taxonomy_initial_operations,
        taxonomy_blocked=taxonomy_initial_blocked,
        taxonomy_decision_approved=apply_taxonomy,
        snapshot_files=snapshot_files,
        context_packets=context_packet_paths,
        vocabulary_map_hash=vocabulary_map_fields.map_hash,
        deferred_work_items=deferred_work_items,
    )
    fix_wiki_plan_path = run_dir / "fix-wiki-plan.json"
    fix_wiki_plan_write_error = _try_write_json_file(fix_wiki_plan_path, fix_wiki_plan)
    if fix_wiki_plan_write_error:
        atomicity_split_plan, atomicity_split_plan_path = (
            {"status": "skipped", "skipped_reason": "fix_wiki_plan_write_failed", "item_count": 0},
            "",
        )
    else:
        atomicity_split_plan, atomicity_split_plan_path = _atomicity_split_plan_payload(
            fix_wiki_plan_path=fix_wiki_plan_path,
            run_id=run_id,
            run_dir=run_dir,
        )
    plan_validation = validate_fix_wiki_plan_snapshot(fix_wiki_plan, config.wiki_dir)
    plan_validation_status = str(_json_field(plan_validation, "status", ""))
    if effective_apply and plan_validation_status == "blocked":
        effective_apply = False
    hygiene_pre_cleanup = (
        cleanup_wiki_hygiene(
            config.wiki_dir,
            archive_root=archive_root / "preflight",
            archive_backups=False,
            remove_rewrites=True,
            remove_empty_dirs=True,
            remove_empty_root_notes=True,
        )
        if effective_apply and _hygiene_cleanup_needed(hygiene_before)
        else None
    )
    hygiene_after_preflight = collect_wiki_hygiene(config.wiki_dir) if hygiene_pre_cleanup else hygiene_before
    if apply and effective_apply:
        vocabulary_bootstrap_receipt = _VocabularyBootstrapReceiptFields.model_validate(
            resolve_vocabulary_bootstrap(
                config,
                apply=True,
                run_dir=run_dir,
                backup=backup,
                force_reset=vocabulary_reset,
            )
        )
        vocabulary_map_diagnosis = _vocabulary_map_diagnosis_payload(config, allow_create=True)
        vocabulary_status = str(_json_field(vocabulary_map_diagnosis, "status", "skipped") or "skipped")
        if _vocabulary_repair_needed(vocabulary_map_diagnosis):
            vocabulary_semantic_repair = repair_vocabulary_semantics_for_link(
                config,
                run_dir=run_dir,
                trigger="/mednotes:fix-wiki",
            )
            vocabulary_map_diagnosis = _vocabulary_map_diagnosis_payload(config, allow_create=True)
            vocabulary_status = str(_json_field(vocabulary_map_diagnosis, "status", "skipped") or "skipped")
        vocabulary_blocked_reason = _vocabulary_blocked_reason(vocabulary_status)
        vocabulary_apply_blocked = vocabulary_status in {"blocked_pending", "blocked_human"}
        vocabulary_structural_apply_blocked = vocabulary_status == "blocked_human"
        alias_projection_plan = _alias_projection_plan_payload(
            config,
            vocabulary_map_diagnosis,
            run_dir=run_dir,
            backup=backup,
        )
        alias_projection_apply = _alias_projection_apply_payload(
            apply=not vocabulary_apply_blocked,
            plan=alias_projection_plan,
            run_dir=run_dir,
        )
        vocabulary_curator_batch_plan, vocabulary_curator_batch_plan_path = _vocabulary_curator_batch_plan_payload(
            config,
            vocabulary_map_diagnosis,
            run_id=run_id,
            run_dir=run_dir,
        )

    if taxonomy_initial_operations:
        taxonomy_plan_path = run_dir / "taxonomy-plan.json"
        _write_json_file(taxonomy_plan_path, taxonomy_initial_plan)
        if effective_apply and apply_taxonomy and not taxonomy_initial_blocked and not vocabulary_structural_apply_blocked:
            taxonomy_receipt_path = run_dir / "taxonomy-receipt.json"
            taxonomy_apply = apply_taxonomy_migration(taxonomy_plan_path, config, receipt_path=taxonomy_receipt_path)
        elif effective_apply and apply_taxonomy and vocabulary_structural_apply_blocked:
            taxonomy_apply_skipped_reason = "vocabulary_map_blocked"
        elif effective_apply and apply_taxonomy and taxonomy_initial_blocked:
            taxonomy_apply_skipped_reason = "taxonomy_plan_blocked"

    if effective_apply:
        migrate_related_notes_clean_v1_table_hashes(config.wiki_dir)

    taxonomy_report = taxonomy_audit(config.wiki_dir)
    taxonomy_plan_after = taxonomy_migration_plan(config.wiki_dir)
    taxonomy_issue_count = _taxonomy_action_issue_count(taxonomy_report)
    style_fix_result = (
        dry_run_style_preview
        if dry_run_style_preview is not None and not effective_apply
        else FixWikiStyleResult.model_validate(fix_wiki_style(config.wiki_dir, apply=effective_apply, backup=backup))
    )
    style_fix = style_fix_result.to_payload()
    style_fix_fields = _FixWikiRepairFactsFields.model_validate(style_fix)
    style_write_errors = [error for error in style_fix_fields.write_errors if isinstance(error, dict)]
    sources_backfill_preview = audit_sources_backfill(config, node_modules_path=None)
    sources_backfill_preview_fields = _FixWikiRepairFactsFields.model_validate(sources_backfill_preview)
    sources_metadata_fallback_reason = ""
    sources_metadata_warning: JsonObject | None = None
    if effective_apply and not style_write_errors:
        recoverable_count = sources_backfill_preview_fields.recoverable_count
        if recoverable_count:
            if _raw_chat_files_available(config.raw_dir):
                try:
                    markdown_node_runtime = ensure_markdown_query_available(
                        wiki_dir=config.wiki_dir,
                        raw_dir=config.raw_dir,
                    )
                    markdown_node_modules_path = Path(str(markdown_node_runtime["node_modules_path"]))
                except MarkdownQueryUnavailable as exc:
                    sources_metadata_fallback_reason = exc.blocked_reason
                    sources_metadata_warning = {
                        "blocked_reason": exc.blocked_reason,
                        "error_summary": str(exc),
                        "next_action": exc.next_action,
                        "details": exc.payload,
                    }
            else:
                sources_metadata_fallback_reason = "raw_dir_empty"
        sources_backfill = apply_sources_backfill(
            config,
            backup=backup,
            node_modules_path=markdown_node_modules_path,
            metadata_fallback_reason=sources_metadata_fallback_reason,
            metadata_warning=sources_metadata_warning,
        )
    else:
        sources_backfill = sources_backfill_preview
        if effective_apply and style_write_errors:
            sources_backfill = {
                **sources_backfill,
                "status": "skipped",
                "skipped_reason": "style_write_errors",
                "would_write_count": 0,
            }
    sources_backfill_receipt_path = run_dir / "chats-backfill-receipt.json"
    sources_backfill_receipt_write_error = _try_write_json_file(sources_backfill_receipt_path, sources_backfill)
    sources_backfill["receipt_path"] = str(sources_backfill_receipt_path)
    sources_backfill["write_error"] = sources_backfill_receipt_write_error or ""
    sources_backfill_fields = _FixWikiRepairFactsFields.model_validate(sources_backfill)
    vocabulary_hash_sync = _sync_vocabulary_note_hashes_after_style(config, style_fix, apply=effective_apply)
    graph_fix = _delegated_graph_fix_report(config)
    write_errors = [
        *style_write_errors,
        *[error for error in sources_backfill_fields.write_errors if isinstance(error, dict)],
    ]
    write_error_count = len(write_errors)
    style_audit = _style_audit_after_fix_or_validate(
        config.wiki_dir,
        style_fix,
        sources_backfill,
        fallback=lambda: validate_wiki_style(config.wiki_dir),
    )
    # In apply runs, deterministic fixes and linker mutations can stale target hashes.
    # Materialize the executable rewrite plan only after final validation below.
    rewrite_plan = None if effective_apply else _style_rewrite_plan_if_needed(config, style_audit)
    style_rewrite_plan_path = run_dir / "style-rewrite-plan.json" if isinstance(rewrite_plan, dict) else None
    if style_rewrite_plan_path is not None and rewrite_plan is not None:
        _ = _try_write_json_file(style_rewrite_plan_path, rewrite_plan)
    style_rewrite_manifest_path = run_dir / "style-rewrite-manifest.json" if isinstance(rewrite_plan, dict) else None
    linker_diagnosis_path = run_dir / "link-diagnosis.json"
    link_trigger_context = _fix_wiki_link_trigger_context(
        config=config,
        run_id=run_id,
        style_fix=style_fix,
        sources_backfill=sources_backfill,
        alias_projection_apply=alias_projection_apply,
        graph_fix=graph_fix,
        taxonomy_apply=taxonomy_apply,
    )
    link_trigger_context_path = run_dir / "link-trigger-context.json" if link_trigger_context else None
    if link_trigger_context_path is not None and link_trigger_context is not None:
        write_trigger_context(link_trigger_context_path, link_trigger_context)
    linker_diagnosis = (
        {
            "schema": "medical-notes-workbench.link-diagnosis.v1",
            "phase": "link_diagnosis",
            "status": "skipped",
            "skipped_reason": "write_errors",
            "blocked_reason": "write_errors",
            "next_action": "Resolver erros de escrita antes de diagnosticar/aplicar links.",
            "diagnosis_path": str(linker_diagnosis_path),
            "blocker_count": 0,
            "links_planned": 0,
            "links_rewritten": 0,
        }
        if write_error_count
        else _link_diagnosis_skipped_for_vocabulary_preview(linker_diagnosis_path, vocabulary_bootstrap_receipt)
        if vocabulary_bootstrap_receipt.status == "planned" and not effective_apply
        else execute_link_subworkflow(
            config,
            workflow_effect_executor=workflow_effect_executor,
            run_id=str(run_id),
            effect_id="fix-wiki-link-diagnosis",
            diagnose=True,
            apply=False,
            diagnosis_path=linker_diagnosis_path,
            include_related_notes=True,
            trigger_context_path=link_trigger_context_path,
        )
    )
    related_notes_export_recovery: JsonObject = {}
    if effective_apply and _related_notes_recovery_candidate(linker_diagnosis):
        related_notes_export_recovery = execute_related_notes_export_recovery(
            config,
            workflow_effect_executor=workflow_effect_executor,
            run_id=str(run_id),
            mode="auto",
        )
        related_notes_recovery_result = LinkRelatedSyncResult.from_payload(related_notes_export_recovery)
        if related_notes_recovery_result.status == "recovered":
            linker_diagnosis = execute_link_subworkflow(
                config,
                workflow_effect_executor=workflow_effect_executor,
                run_id=str(run_id),
                effect_id="fix-wiki-link-diagnosis-after-related-notes-recovery",
                diagnose=True,
                apply=False,
                diagnosis_path=linker_diagnosis_path,
                include_related_notes=False,
                force_diagnose=True,
                trigger_context_path=link_trigger_context_path,
            )
    _ = _ensure_declared_artifact_exists(linker_diagnosis, fallback_path=linker_diagnosis_path)
    graph_before_linker = _graph_audit_before_linker_or_run(linker_diagnosis, lambda: graph_audit(config))
    graph_fix = _delegated_graph_fix_report(config, linker_diagnosis)
    blocker_resolution = _blocker_resolution_plan(
        apply=effective_apply,
        graph_audit_report=graph_before_linker,
        write_errors=write_errors,
        rewrite_plan=rewrite_plan,
        taxonomy_plan=taxonomy_plan_after,
        taxonomy_issue_count=taxonomy_issue_count,
        taxonomy_requires_explicit_apply=taxonomy_apply_requires_confirmation,
        suppress_graph_link_repair=effective_apply,
    )
    blocker_resolution_fields = _BlockerResolutionFields.model_validate(blocker_resolution)
    linker_apply: JsonObject | None = None
    linker_skipped_reason = vocabulary_blocked_reason if apply and vocabulary_apply_blocked else ""
    alias_projection_blocked = _json_field(alias_projection_apply, "status") == "blocked"
    body_only_linker_diagnosis: JsonObject | None = None
    body_only_linker_diagnosis_path: Path | None = None
    body_only_linker_diagnosis_write_error = ""
    pre_link_total_changed_count = _total_changed_count(
        style_fix=style_fix,
        sources_backfill=sources_backfill,
        alias_projection_apply=alias_projection_apply,
        graph_fix=graph_fix,
        taxonomy_apply=taxonomy_apply,
        linker_apply=None,
        related_notes_safety_cleanup=None,
        hygiene_pre_cleanup=hygiene_pre_cleanup,
        hygiene_cleanup=None,
    )
    pre_link_version_control_safety = fix_wiki_version_control_safety(
        config.wiki_dir,
        effective_apply=effective_apply,
        total_changed_count=pre_link_total_changed_count,
        version_control_mutation_summary=version_control_mutation_summary(config.wiki_dir),
    )
    linker_diagnosis_fields = _ConsumedLinkArtifactFields.model_validate(_consumed_link_artifact_source(linker_diagnosis))
    alias_projection_apply_fields = _FixWikiStatusActionFields.model_validate(alias_projection_apply or {})

    if effective_apply:
        if write_error_count:
            linker_skipped_reason = "write_errors"
        elif vocabulary_apply_blocked:
            linker_skipped_reason = vocabulary_blocked_reason
        elif alias_projection_blocked:
            linker_skipped_reason = alias_projection_apply_fields.blocked_reason or "alias_projection_blocked"
        elif linker_diagnosis_fields.blocker_count:
            if _linker_blocked_only_by_related_notes(linker_diagnosis):
                body_only_linker_diagnosis_path = run_dir / "link-diagnosis-body-only.json"
                body_only_linker_diagnosis = execute_link_subworkflow(
                    config,
                    workflow_effect_executor=workflow_effect_executor,
                    run_id=str(run_id),
                    effect_id="fix-wiki-link-diagnosis-body-only",
                    diagnose=True,
                    apply=False,
                    diagnosis_path=body_only_linker_diagnosis_path,
                    include_related_notes=False,
                    backup=backup,
                    force_diagnose=True,
                    trigger_context_path=link_trigger_context_path,
                )
                body_only_linker_diagnosis_write_error = _ensure_declared_artifact_exists(
                    body_only_linker_diagnosis,
                    fallback_path=body_only_linker_diagnosis_path,
                )
                if body_only_linker_diagnosis_write_error:
                    linker_skipped_reason = "body_only_linker_diagnosis_write_error"
                else:
                    body_only_fields = _ConsumedLinkArtifactFields.model_validate(
                        _consumed_link_artifact_source(body_only_linker_diagnosis)
                    )
                    if body_only_fields.status != "diagnosis_ready" or body_only_fields.blocker_count:
                        linker_skipped_reason = body_only_fields.blocked_reason or "body_only_link_diagnosis_blocked"
                    elif not blocker_resolution_fields.linker_can_apply:
                        if taxonomy_issue_count:
                            linker_skipped_reason = "taxonomy_action_required"
                        else:
                            linker_skipped_reason = _linker_specific_blocked_reason(
                                body_only_linker_diagnosis,
                                fallback="link_plan_blocked",
                            )
                    else:
                        _ = _backup_linker_planned_changes(config, body_only_linker_diagnosis, backup)
                        linker_apply = execute_link_subworkflow(
                            config,
                            workflow_effect_executor=workflow_effect_executor,
                            run_id=str(run_id),
                            effect_id="fix-wiki-link-apply-body-only",
                            diagnose=False,
                            apply=True,
                            diagnosis_path=body_only_linker_diagnosis_path,
                            receipt_path=run_dir / "link-run-receipt.json",
                            include_related_notes=False,
                            backup=backup,
                            version_control_safety=pre_link_version_control_safety,
                        )
            else:
                linker_skipped_reason = _linker_specific_blocked_reason(
                    linker_diagnosis,
                    fallback=linker_diagnosis_fields.blocked_reason or "link_diagnosis_blocked",
                )
        elif not blocker_resolution_fields.linker_can_apply:
            if taxonomy_issue_count:
                linker_skipped_reason = "taxonomy_action_required"
            else:
                linker_skipped_reason = _linker_specific_blocked_reason(linker_diagnosis, fallback="link_plan_blocked")
        else:
            _ = _backup_linker_planned_changes(config, linker_diagnosis, backup)
            linker_apply = execute_link_subworkflow(
                config,
                workflow_effect_executor=workflow_effect_executor,
                run_id=str(run_id),
                effect_id="fix-wiki-link-apply",
                diagnose=False,
                apply=True,
                diagnosis_path=linker_diagnosis_path,
                receipt_path=run_dir / "link-run-receipt.json",
                include_related_notes=True,
                backup=backup,
                version_control_safety=pre_link_version_control_safety,
            )

    knowledge_graph_problems = _knowledge_graph_problems(linker_diagnosis, linker_skipped_reason)
    if knowledge_graph_problems or linker_diagnosis or linker_apply or link_trigger_context:
        fix_wiki_problems = [*fix_wiki_problems, *knowledge_graph_problems]
        try:
            context_packet_paths.update(
                write_context_packets(
                    run_dir=run_dir,
                    wiki_dir=config.wiki_dir,
                    problems=knowledge_graph_problems,
                    taxonomy_plan=taxonomy_initial_plan,
                    vocabulary_map_diagnosis=vocabulary_map_diagnosis,
                    linker_diagnosis=linker_diagnosis,
                    link_trigger_context=link_trigger_context,
                    linker_receipt=linker_apply,
                )
            )
        except (FileWriteError, OSError) as exc:
            context_packet_write_errors.append({"phase": "knowledge_graph_context_packet", "error": str(exc)})

    related_notes_sync = _related_notes_from_linker(linker_diagnosis=linker_diagnosis, linker_apply=linker_apply)
    nested_related_notes_recovery = related_notes_sync.get("related_notes_export_recovery")
    if not related_notes_export_recovery and isinstance(nested_related_notes_recovery, dict):
        related_notes_export_recovery = _json_object(nested_related_notes_recovery)
    if (
        isinstance(nested_related_notes_recovery, dict)
        and "export_relocation" not in related_notes_sync
        and isinstance(nested_related_notes_recovery.get("export_relocation"), dict)
    ):
        related_notes_sync = _json_object({
            **related_notes_sync,
            "export_relocation": nested_related_notes_recovery["export_relocation"],
        })
    related_notes_safety_cleanup: JsonObject = {
        "schema": "medical-notes-workbench.related-notes-safety-cleanup.v1",
        "phase": "related_notes_safety_cleanup",
        "status": "skipped",
        "skipped_reason": "related_notes_sync_not_blocked",
        "changed_file_count": 0,
        "removed_link_count": 0,
        "reports": [],
    }
    if effective_apply and related_notes_sync_blocked(related_notes_sync):
        related_notes_safety_cleanup = cleanup_invalid_related_notes_links(
            config,
            backup=backup,
            cleanup_reason=str(
                related_notes_export_recovery.get("blocked_reason")
                or related_notes_sync.get("blocked_reason")
                or "related_notes_blocked"
            ),
        )
    related_notes_blocker = related_notes_sync_blocked(related_notes_sync)
    graph_after = _graph_audit_after_linker_or_run(
        linker_apply,
        graph_before_linker,
        _json_object(related_notes_safety_cleanup),
        lambda: graph_audit(config),
    )
    graph_after_fields = _FixWikiGraphAuditDecisionFields.model_validate(graph_after)
    final_hygiene_cleanup_needed = bool(effective_apply and _hygiene_cleanup_needed(hygiene_after_preflight))
    hygiene_cleanup = (
        cleanup_wiki_hygiene(
            config.wiki_dir,
            archive_root=archive_root / "final",
            archive_backups=False,
            remove_rewrites=True,
            remove_empty_dirs=True,
            remove_empty_root_notes=True,
        )
        if final_hygiene_cleanup_needed
        else None
    )
    hygiene_after = collect_wiki_hygiene(config.wiki_dir) if hygiene_cleanup else hygiene_after_preflight
    final_style_audit = _style_audit_after_post_style_mutations(
        config.wiki_dir,
        style_audit,
        linker_apply=linker_apply,
        related_notes_safety_cleanup=related_notes_safety_cleanup,
        hygiene_cleanup=hygiene_cleanup,
        fallback=lambda: validate_wiki_style(config.wiki_dir),
    )
    final_rewrite_plan = _style_rewrite_plan_if_needed(config, final_style_audit)
    style_audit = final_style_audit
    rewrite_plan = final_rewrite_plan
    if isinstance(rewrite_plan, dict):
        style_rewrite_plan_path = run_dir / "style-rewrite-plan.json"
        _ = _try_write_json_file(style_rewrite_plan_path, rewrite_plan)
        style_rewrite_manifest_path = run_dir / "style-rewrite-manifest.json"
    else:
        style_rewrite_plan_path = None
        style_rewrite_manifest_path = None
    taxonomy_action_required = bool(taxonomy_issue_count)
    style_audit_fields = _StyleAuditReportsFields.model_validate(style_audit)
    requires_llm_rewrite_count = sum(1 for item in style_audit_fields.reports if item.requires_llm_rewrite)
    graph_error_count = graph_after_fields.error_count
    blocker_resolution = _blocker_resolution_plan(
        apply=effective_apply,
        graph_audit_report=graph_after,
        write_errors=write_errors,
        rewrite_plan=rewrite_plan,
        taxonomy_plan=taxonomy_plan_after,
        taxonomy_issue_count=taxonomy_issue_count,
        taxonomy_requires_explicit_apply=taxonomy_apply_requires_confirmation,
        suppress_graph_link_repair=False,
    )
    blocker_resolution_fields = _BlockerResolutionFields.model_validate(blocker_resolution)
    atomicity_split_fields = _FixWikiAtomicitySplitPlanFields.model_validate(atomicity_split_plan)
    atomicity_split_required = bool(
        atomicity_split_fields.status == "ready" and atomicity_split_fields.item_count > 0
    )
    linker_payload = linker_apply or linker_diagnosis
    observed_linker_blocker = bool(
        linker_skipped_reason
        or _payload_int(linker_payload, "blocker_count")
        or _linker_status_blocks_after_final_graph(linker_apply, graph_error_count=graph_error_count)
    )
    hygiene_cleanup_fields = _HygieneReportFields.model_validate(hygiene_cleanup or {})
    hygiene_pre_cleanup_fields = _HygieneReportFields.model_validate(hygiene_pre_cleanup or {})
    hygiene_error_count = hygiene_cleanup_fields.error_count + hygiene_pre_cleanup_fields.error_count
    human_decision_packets = project_fix_wiki_human_decision_packets(blocker_resolution)
    observed_human_decision_needed = (
        bool(human_decision_packets)
        or _vocabulary_diagnosis_requires_human(vocabulary_map_diagnosis)
        or _blocker_requires_human(linker_skipped_reason)
        or _blocker_requires_human(_FixWikiStatusActionFields.model_validate(alias_projection_apply).blocked_reason)
    )
    if observed_human_decision_needed and not human_decision_packets:
        human_decision_packets = _human_decision_packets_for_unpacketized_blocker(
            graph_error_count=graph_error_count,
            graph_audit_report=graph_after,
            linker_skipped_reason=linker_skipped_reason,
        )
    human_decision_kinds = _human_decision_kinds(human_decision_packets)
    primary_human_decision_kind = human_decision_kinds[0] if human_decision_kinds else ""
    total_changed_count = _total_changed_count(
        style_fix=style_fix,
        sources_backfill=sources_backfill,
        alias_projection_apply=alias_projection_apply,
        graph_fix=graph_fix,
        taxonomy_apply=taxonomy_apply,
        linker_apply=linker_apply,
        related_notes_safety_cleanup=related_notes_safety_cleanup,
        hygiene_pre_cleanup=hygiene_pre_cleanup,
        hygiene_cleanup=hygiene_cleanup,
    )
    version_control_mutation_summary_payload = version_control_mutation_summary(config.wiki_dir)
    version_control_safety = fix_wiki_version_control_safety(
        config.wiki_dir,
        effective_apply=effective_apply,
        total_changed_count=total_changed_count,
        version_control_mutation_summary=version_control_mutation_summary_payload,
    )
    change_count_context = _fix_wiki_change_count_context(
        requested_apply=apply,
        effective_apply=effective_apply,
        style_fix=style_fix,
        total_changed_count=total_changed_count,
        version_control_mutation_summary=version_control_mutation_summary_payload,
    )
    fsm_failed = bool(write_error_count or hygiene_error_count)
    fsm_failed_reason_code = ""
    if write_error_count:
        fsm_failed_reason_code = "write_errors"
    elif hygiene_error_count:
        fsm_failed_reason_code = "hygiene_errors"
    observed_warning_completion = bool(
        graph_after_fields.warning_count
        and not fsm_failed
        and not (
            observed_human_decision_needed
            or requires_llm_rewrite_count
            or graph_error_count
            or atomicity_split_required
            or observed_linker_blocker
            or related_notes_blocker
            or taxonomy_action_required
            or vocabulary_blocked_reason
            or alias_projection_blocked
            or (apply and plan_validation_status == "blocked")
        )
    )
    status_probe_source = _FixWikiFsmRuntimeSource.model_validate(
        {
            "run_id": run_id,
            "requested_apply": apply,
            "effective_apply": effective_apply,
            "total_changed_count": total_changed_count,
            "change_count_context": change_count_context,
            "warning_count": style_fix_fields.warning_count,
            "graph_warning_count": graph_after_fields.warning_count,
            "completed_with_warnings": observed_warning_completion,
            "requires_llm_rewrite_count": requires_llm_rewrite_count,
            "final_validation": {
                "graph": {
                    "error_count": graph_error_count,
                    "blocker_count": graph_after_fields.blocker_count or graph_error_count,
                    "orphan_count": graph_after_fields.metrics.orphan_count,
                }
            },
            "version_control_safety": version_control_safety,
            "related_notes_blocked": related_notes_blocker,
            "related_notes_recovery_state": _related_notes_recovery_payload(related_notes_export_recovery),
            "vocabulary_semantic_ingestion_pending": vocabulary_blocked_reason
            == "vocabulary_semantic_ingestion_pending",
            "vocabulary_eval_needs_review": vocabulary_blocked_reason == "vocabulary_map_blocked",
            "atomicity_split_plan": atomicity_split_plan,
            "atomicity_split_required": atomicity_split_required,
            "human_decision_required": observed_human_decision_needed,
            "human_decision_packets": human_decision_packets,
            "primary_human_decision_kind": primary_human_decision_kind,
            "failed": fsm_failed or bool(apply and plan_validation_status == "blocked"),
            "failed_reason_code": fsm_failed_reason_code,
            "linker_blocked": observed_linker_blocker,
            "linker_skipped_reason": linker_skipped_reason,
            "linker_apply": linker_apply,
            "graph_review_required": observed_human_decision_needed,
            "taxonomy_action_required": taxonomy_action_required,
            "style_rewrite_plan": rewrite_plan,
        }
    )
    fsm_progress_value = _fix_wiki_progress_status_from_runtime_source(
        status_probe_source,
        apply_taxonomy=apply_taxonomy,
    )
    taxonomy_apply_fields = _TaxonomyApplyFields.model_validate(taxonomy_apply or {})
    rollback_cmd = _rollback_command(config, taxonomy_apply_fields.receipt_path or None)
    summary_text = _summary(
        status=fsm_progress_value,
        requested_apply=apply,
        total_changed_count=total_changed_count,
        requires_llm_rewrite_count=requires_llm_rewrite_count,
        graph_error_count=graph_error_count,
        linker_blocked=observed_linker_blocker,
        related_notes_blocked=related_notes_blocker,
        taxonomy_action_required=taxonomy_action_required,
        taxonomy_apply_requires_confirmation=taxonomy_apply_requires_confirmation,
        human_decision_required=observed_human_decision_needed,
    )

    # This is the single blocker code projected into the FSM decision surface.
    # It is derived from typed domain facts above; public consumers must not
    # reconstruct it from reports, process return codes, or child payload text.
    primary_blocker_code = ""
    if apply and plan_validation_status == "blocked":
        primary_blocker_code = "stale_fix_wiki_plan"
    elif write_error_count:
        primary_blocker_code = "write_errors"
    elif vocabulary_blocked_reason:
        primary_blocker_code = vocabulary_blocked_reason
    elif alias_projection_blocked:
        alias_projection_apply_fields = _AliasProjectionApplyFields.model_validate(alias_projection_apply or {})
        primary_blocker_code = alias_projection_apply_fields.blocked_reason or "alias_projection_blocked"
    elif atomicity_split_required:
        primary_blocker_code = "atomicity_split_required"
    elif graph_error_count:
        primary_blocker_code = "graph_blockers"
    elif related_notes_blocker and _linker_blocked_only_by_related_notes(linker_payload):
        primary_blocker_code = "related_notes_blocked"
    elif observed_linker_blocker:
        linker_fields = _ConsumedLinkArtifactFields.model_validate(_consumed_link_artifact_source(linker_payload))
        primary_blocker_code = linker_skipped_reason or linker_fields.blocked_reason or "linker_blocked"
    elif related_notes_blocker:
        primary_blocker_code = "related_notes_blocked"
    elif requires_llm_rewrite_count:
        primary_blocker_code = "requires_llm_rewrite"
    elif taxonomy_action_required:
        primary_blocker_code = "taxonomy_action_required"
    elif observed_human_decision_needed:
        primary_blocker_code = "human_decision_required"
    blocking_reasons = _blocking_reasons(
        blocked_reason=primary_blocker_code,
        write_error_count=write_error_count,
        vocabulary_blocked_reason=vocabulary_blocked_reason,
        alias_projection_blocked=alias_projection_blocked,
        alias_projection_apply=alias_projection_apply,
        atomicity_split_required=atomicity_split_required,
        requires_llm_rewrite_count=requires_llm_rewrite_count,
        graph_error_count=graph_error_count,
        linker_blocked=observed_linker_blocker,
        linker_skipped_reason=linker_skipped_reason,
        linker_payload=linker_apply or linker_diagnosis,
        related_notes_blocker=related_notes_blocker,
        taxonomy_action_required=taxonomy_action_required,
        human_decision_required=observed_human_decision_needed,
        human_decision_kinds=human_decision_kinds,
    )
    fix_wiki_problems = _finalize_fix_wiki_problems(
        fix_wiki_problems,
        graph_error_count=graph_error_count,
        linker_blocked=observed_linker_blocker,
    )

    file_changes = _changed_paths_from(
        style_fix=style_fix,
        sources_backfill=sources_backfill,
        alias_projection_apply=alias_projection_apply,
        taxonomy_apply=taxonomy_apply,
        linker_apply=linker_apply,
        related_notes_safety_cleanup=related_notes_safety_cleanup,
        hygiene_pre_cleanup=hygiene_pre_cleanup,
        hygiene_cleanup=hygiene_cleanup,
    )
    fix_wiki_receipt_path = run_dir / "fix-wiki-receipt.json"
    human_report_path = run_dir / "fix-wiki-user-report.md"
    human_report_write_error = ""
    fix_wiki_receipt = build_fix_wiki_receipt_evidence(
        fix_wiki_receipt_path,
        run_id=run_id,
        status=fsm_progress_value,
        fix_wiki_plan=fix_wiki_plan,
        plan_validation=plan_validation,
        snapshot_hash_before=snapshot_hash,
        snapshot_hash_after=_wiki_snapshot_hash(config.wiki_dir),
        vocabulary_bootstrap=vocabulary_bootstrap_receipt.to_payload(),
        hygiene_pre_cleanup=hygiene_pre_cleanup,
        style_fix=style_fix,
        sources_backfill=sources_backfill,
        alias_projection_apply=alias_projection_apply,
        vocabulary_hash_sync=vocabulary_hash_sync,
        taxonomy_apply=taxonomy_apply,
        linker_diagnosis=linker_diagnosis,
        linker_apply=linker_apply,
        related_notes_export_recovery=related_notes_export_recovery,
        related_notes_safety_cleanup=related_notes_safety_cleanup,
        hygiene_cleanup=hygiene_cleanup,
        blockers=[problem for problem in fix_wiki_problems if _json_field(problem, "status") == "blocked"],
        skips=[{"phase": "linker", "reason": linker_skipped_reason}] if linker_skipped_reason else [],
        file_changes=file_changes,
    )
    if effective_apply and _fix_wiki_linker_apply_attempted_from_payload(linker_apply):
        linker_artifact_compaction = _compact_consumed_linker_artifacts(
            diagnosis_path=_path_from_payload(linker_apply, "diagnosis_path") or linker_diagnosis_path,
            receipt_path=_path_from_payload(linker_apply, "receipt_path"),
            additional_diagnosis_paths=tuple(
                path for path in (linker_diagnosis_path, body_only_linker_diagnosis_path) if path is not None
            ),
        )
    else:
        linker_artifact_compaction = _json_object({
            "schema": "medical-notes-workbench.consumed-link-artifacts-compaction.v1",
            "status": "skipped",
            "skipped_reason": "linker_apply_not_completed",
            "artifact_count": 0,
            "compacted_count": 0,
            "failed_count": 0,
            "bytes_saved": 0,
            "artifacts": [],
        })
    linker_action_fields = _FixWikiStatusActionFields.model_validate(linker_apply or linker_diagnosis)
    if observed_human_decision_needed:
        error_context_guidance = _human_decision_next_action(human_decision_packets) or (
            "Resolver a decisão humana pendente antes de continuar o workflow."
        )
    elif graph_error_count:
        error_context_guidance = _blocker_resolution_next_action(blocker_resolution, preferred_route="graph_link_repair")
    elif related_notes_blocker:
        error_context_guidance = _related_notes_public_next_action(
            related_notes_export_recovery=related_notes_export_recovery,
            related_notes_sync=related_notes_sync,
        )
    elif observed_linker_blocker and linker_action_fields.next_action:
        error_context_guidance = linker_action_fields.next_action
    elif _requires_subagent_orchestration(blocker_resolution):
        error_context_guidance = _blocker_resolution_next_action(
            blocker_resolution,
            preferred_route="style_rewrite" if primary_blocker_code == "requires_llm_rewrite" else "",
        )
    elif fsm_progress_value in {"completed", "completed_with_warnings"}:
        error_context_guidance = ""
    else:
        error_context_guidance = (
            blocker_resolution_fields.next_action
            or "Repetir a conferência da Wiki pela rota oficial após resolver os bloqueios atuais."
            or ""
        )
    report_required_inputs = _fix_wiki_required_inputs(
        blocked_reason=primary_blocker_code,
        related_notes_sync=related_notes_sync,
    )
    style_rewrite_batch_ready = _fix_wiki_has_executable_style_rewrite_batch(
        requested_apply=apply,
        effective_apply=effective_apply,
        human_decision_required=observed_human_decision_needed,
        graph_error_count=graph_error_count,
        linker_blocked=observed_linker_blocker,
        related_notes_blocked=related_notes_blocker,
        taxonomy_action_required=taxonomy_action_required,
        blocker_resolution=blocker_resolution,
        rewrite_plan=rewrite_plan,
        requires_llm_rewrite_count=requires_llm_rewrite_count,
    )
    apply_guidance = _fix_wiki_apply_guidance(
        requested_apply=apply,
        effective_apply=effective_apply,
        linker_diagnosis=linker_diagnosis,
        blocker_resolution=blocker_resolution,
        style_rewrite_batch_ready=style_rewrite_batch_ready,
    )
    graph_after_for_report = _demote_subreport_followup(
        graph_after,
        source_path="graph_audit_final",
        superseded_by="fix_wiki.fsm_projection",
        active_workflow_followup=error_context_guidance,
    )
    report_error_context = None
    if fsm_progress_value in {"blocked", "failed"} or primary_blocker_code:
        root_cause = primary_blocker_code or fsm_progress_value
        next_action_for_context = error_context_guidance or (
            "Repetir /mednotes:fix-wiki pela rota oficial após corrigir o blocker."
        )
        retry_scope = (
            "atomicity_split_then_rerun_fix_wiki"
            if root_cause == "atomicity_split_required"
            else "fix_wiki_official_route"
        )
        affected_artifact = "atomicity_split_plan" if root_cause == "atomicity_split_required" else "fix_wiki_plan"
        report_error_context = error_context(
            phase="fix_wiki_apply" if apply else "fix_wiki_dry_run",
            blocked_reason=root_cause,
            root_cause=root_cause,
            affected_artifact=affected_artifact,
            error_summary=summary_text,
            suggested_fix=next_action_for_context,
            next_action=next_action_for_context,
            retry_scope=retry_scope,
            human_decision_required=observed_human_decision_needed,
        )
    user_summary = _fix_wiki_user_summary(
        status=fsm_progress_value,
        blocked_reason=primary_blocker_code,
        total_changed_count=total_changed_count,
        linker_status=linker_action_fields.status or "skipped",
        rollback_command=rollback_cmd,
        human_report_path=human_report_path,
        related_notes_export_recovery=related_notes_export_recovery,
    )
    taxonomy_report_fields = _TaxonomyReportFields.model_validate(taxonomy_report)
    taxonomy_plan_after_fields = _TaxonomyPlanFields.model_validate(taxonomy_plan_after)
    fix_wiki_plan_fields = _FixWikiPlanFields.model_validate(fix_wiki_plan)
    hygiene_after_fields = _HygieneReportFields.model_validate(hygiene_after)
    final_validation_summary = _FixWikiFinalValidationFields.model_validate(
        {
            "graph": {
                "error_count": graph_error_count,
                "blocker_count": graph_after_fields.blocker_count or graph_error_count,
                "orphan_count": graph_after_fields.metrics.orphan_count,
            },
            "hygiene": {
                "bak_or_rewrite": hygiene_after_fields.bak_or_rewrite,
                "empty_dirs": hygiene_after_fields.empty_dirs,
                "empty_root_notes": hygiene_after_fields.empty_root_note_count,
                "duplicate_hash_groups": hygiene_after_fields.duplicate_hash_groups,
                "duplicate_filename_groups": hygiene_after_fields.duplicate_filename_groups,
            },
            "taxonomy": {
                "proposed_moves": len(taxonomy_report_fields.proposed_moves),
                "blocked_items": len(taxonomy_plan_after_fields.blocked_items),
                "duplicate_directory_groups": len(taxonomy_report_fields.duplicate_directory_groups),
                "ignored_items": ["attachments", "_Mock_Embeds", "_Índice_Medicina.md"],
            },
        }
    )
    # Human-facing artifacts are written after the FSM projection so the public
    # report, compact JSON, run state, and stdout share the same truth source.
    runtime_source = _FixWikiFsmRuntimeSource.model_validate(
        {
            "run_id": run_id,
            "requested_apply": apply,
            "effective_apply": effective_apply,
            "total_changed_count": total_changed_count,
            "change_count_context": change_count_context,
            "warning_count": style_fix_fields.warning_count,
            "graph_warning_count": graph_after_fields.warning_count,
            "completed_with_warnings": observed_warning_completion,
            "requires_llm_rewrite_count": requires_llm_rewrite_count,
            "final_validation": final_validation_summary,
            "version_control_safety": version_control_safety,
            "related_notes_blocked": related_notes_blocker,
            "related_notes_recovery_state": _related_notes_recovery_payload(related_notes_export_recovery),
            "vocabulary_semantic_ingestion_pending": vocabulary_blocked_reason
            == "vocabulary_semantic_ingestion_pending",
            "vocabulary_eval_needs_review": vocabulary_blocked_reason == "vocabulary_map_blocked",
            "atomicity_split_plan": atomicity_split_plan,
            "atomicity_split_required": atomicity_split_required,
            "human_decision_required": observed_human_decision_needed,
            "human_decision_packets": human_decision_packets,
            "human_decision_kinds": human_decision_kinds,
            "primary_human_decision_kind": primary_human_decision_kind,
            "human_decision_reason_code": primary_blocker_code,
            "failed": fsm_failed,
            "failed_reason_code": fsm_failed_reason_code,
            "required_inputs": report_required_inputs,
            "file_changes": file_changes,
            "linker_blocked": observed_linker_blocker,
            "linker_skipped_reason": linker_skipped_reason,
            "linker_apply": linker_apply,
            "graph_review_required": observed_human_decision_needed,
            "taxonomy_action_required": taxonomy_action_required,
            "error_context": report_error_context or {},
            "workflow_result_label": "fix-wiki",
            "user_summary": user_summary,
            "blocker_resolution": _diagnostic_blocker_resolution(blocker_resolution),
            "apply_guidance": apply_guidance,
            "fix_wiki_plan_validation": plan_validation,
            "diagnostic_payload": {
                "runtime_summary": summary_text,
                "blocking_reasons": blocking_reasons,
                "phases": _phase_list(
                    vocabulary_bootstrap=vocabulary_bootstrap_receipt,
                    vocabulary_map_diagnosis=vocabulary_map_diagnosis,
                    vocabulary_semantic_repair=vocabulary_semantic_repair,
                    sources_backfill=sources_backfill,
                    write_error_count=write_error_count,
                    linker_diagnosis=linker_diagnosis,
                    linker_apply=linker_apply,
                    related_notes_safety_cleanup=related_notes_safety_cleanup,
                    alias_projection_plan=alias_projection_plan,
                    taxonomy_phase=next(
                        (
                            phase.model_dump(mode="json", exclude_defaults=True)
                            for phase in fix_wiki_plan_fields.phases
                            if phase.phase == "taxonomy"
                        ),
                        {"status": "planned"},
                    ),
                ),
            },
            "fix_wiki_problems": fix_wiki_problems,
            "write_error_count": write_error_count,
            "write_errors": write_errors,
            "vocabulary_bootstrap": vocabulary_bootstrap_receipt,
            "vocabulary_map_diagnosis": vocabulary_map_diagnosis,
            "vocabulary_semantic_repair": vocabulary_semantic_repair,
            "vocabulary_curator_batch_plan": vocabulary_curator_batch_plan,
            "alias_projection_plan": alias_projection_plan,
            "alias_projection_apply": alias_projection_apply,
            "sources_backfill": sources_backfill,
            "markdown_node_runtime": markdown_node_runtime or {},
            "related_notes_sync": related_notes_sync,
            "related_notes_export_recovery": related_notes_export_recovery,
            "related_notes_safety_cleanup": related_notes_safety_cleanup,
            "linker_diagnosis": linker_diagnosis,
            "linker_artifact_compaction": linker_artifact_compaction,
            "graph_audit_final": graph_after_for_report,
            "taxonomy_apply": taxonomy_apply,
            "taxonomy_apply_enabled": bool(apply and apply_taxonomy),
            "taxonomy_apply_requires_confirmation": taxonomy_apply_requires_confirmation,
            "taxonomy_apply_skipped_reason": taxonomy_apply_skipped_reason,
            "version_control_mutation_summary": version_control_mutation_summary_payload,
            "fix_wiki_plan_path": str(fix_wiki_plan_path),
            "fix_wiki_receipt_path": str(fix_wiki_receipt_path),
            "sources_backfill_receipt_path": str(sources_backfill_receipt_path),
            "taxonomy_plan_path": _optional_path_string(taxonomy_plan_path),
            "taxonomy_receipt_path": _optional_path_string(taxonomy_receipt_path),
            "style_rewrite_plan": rewrite_plan,
            "style_rewrite_plan_path": _optional_path_string(style_rewrite_plan_path),
            "style_rewrite_manifest_path": _optional_path_string(style_rewrite_manifest_path),
            "human_report_path": str(human_report_path),
            "compact_report_path": str(run_dir / "compact-report.json"),
            "full_report_path": str(run_dir / "full-report.json"),
            "run_state_path": str(run_dir / "run_state.json"),
            "linker_diagnosis_path": str(linker_diagnosis_path),
            "linker_receipt_path": str(run_dir / "link-run-receipt.json"),
            "link_trigger_context_path": _optional_path_string(link_trigger_context_path),
            "related_notes_receipt_path": related_notes_sync.get("receipt_path", ""),
            "vocabulary_curator_batch_plan_path": vocabulary_curator_batch_plan_path,
            "atomicity_split_plan_path": atomicity_split_plan_path,
            "context_packets": context_packet_paths,
        }
    )
    fsm_report = _fix_wiki_fsm_payload_from_runtime_source(
        runtime_source,
        apply_taxonomy=apply_taxonomy,
        workflow_effect_executor=workflow_effect_executor if effective_apply else None,
    )
    fix_wiki_receipt = _write_fix_wiki_receipt_from_fsm(
        path=fix_wiki_receipt_path,
        receipt=fix_wiki_receipt,
        fsm_report=fsm_report,
    )
    fsm_report["artifacts"]["compact_report_path"] = str(run_dir / "compact-report.json")
    fsm_report["artifacts"]["full_report_path"] = str(run_dir / "full-report.json")
    fsm_report["artifacts"]["run_state_path"] = str(run_dir / "run_state.json")
    fsm_report = _attach_fix_wiki_primary_objective_summary(fsm_report)
    report_write_errors = []
    try:
        write_fix_wiki_user_report_v2(human_report_path, fsm_report)
    except (FileWriteError, OSError) as exc:
        human_report_write_error = str(exc)
        report_write_errors.append({"path": str(human_report_path), "error": human_report_write_error})
    compact_error = _try_write_json_file(run_dir / "compact-report.json", _compact_report(fsm_report))
    if compact_error:
        report_write_errors.append({"path": fsm_report["artifacts"]["compact_report_path"], "error": compact_error})
    state_error = _try_write_json_file(run_dir / "run_state.json", _run_state(fsm_report))
    if state_error:
        report_write_errors.append({"path": fsm_report["artifacts"]["run_state_path"], "error": state_error})
    full_error = _try_write_json_file(
        run_dir / "full-report.json",
        _full_report_artifact(fsm_report),
    )
    if full_error:
        report_write_errors.append({"path": fsm_report["artifacts"]["full_report_path"], "error": full_error})
    if report_write_errors:
        diagnostic_context = fsm_report.setdefault("diagnostic_context", {})
        if isinstance(diagnostic_context, dict):
            diagnostic_context["report_write_error_count"] = len(report_write_errors)
            diagnostic_context["report_write_errors"] = report_write_errors
    assert_fix_wiki_fsm_payload(fsm_report)
    return fsm_report


def _write_fix_wiki_receipt_from_fsm(
    *,
    path: Path,
    receipt: JsonObject,
    fsm_report: JsonObject,
) -> JsonObject:
    """Write the standalone receipt file as a projection of the FSM truth."""

    fsm_fields = _FixWikiFsmReceiptReportFields.model_validate(fsm_report)
    canonical_receipt = fsm_fields.receipt
    existing_receipt = _FixWikiExistingReceiptFields.model_validate(receipt)
    progress = fsm_fields.progress_view_model
    state_machine = fsm_fields.state_machine_snapshot
    synced = _json_object(receipt)
    safety_fields = _AgentStdoutVersionControlSafetyFields.model_validate(canonical_receipt.version_control_safety)
    changed_file_count = canonical_receipt.changed_file_count
    if changed_file_count is None:
        if canonical_receipt.changed_files:
            changed_file_count = len(canonical_receipt.changed_files)
        else:
            changed_file_count = safety_fields.changed_file_count
    mutated = canonical_receipt.mutated
    if mutated is None:
        mutated = not bool(safety_fields.no_resource_mutation)
    status = canonical_receipt.status or progress.status or existing_receipt.status
    synced.update(
        {
            "workflow": canonical_receipt.workflow or fsm_fields.workflow or "/mednotes:fix-wiki",
            "run_id": canonical_receipt.run_id or fsm_fields.run_id or existing_receipt.run_id,
            "status": status,
            "phase": progress.phase or existing_receipt.phase,
            "mutated": mutated,
            "next_action": canonical_receipt.next_action or progress.resume_action,
            "human_decision_required": canonical_receipt.human_decision_required,
            "human_decision_packet": canonical_receipt.human_decision_packet,
            "changed_file_count": changed_file_count,
            "artifact_count": len(canonical_receipt.artifacts),
            "phase_outcome_count": len(canonical_receipt.phase_outcomes),
            "phase_receipt_count": len(canonical_receipt.phase_receipts),
            "version_control_safety": canonical_receipt.version_control_safety,
            "progress_view_model": progress.to_payload(),
            "state_machine_snapshot": state_machine,
        }
    )
    if status in {"waiting_agent", "waiting_external", "waiting_human", "completed", "completed_with_warnings"}:
        synced.update({"blocked_reason": ""})
    _write_json_file(path, synced)
    return _json_object(synced)


def _fix_wiki_fsm_payload_from_runtime_source(
    source: JsonObject | _FixWikiFsmRuntimeSource,
    *,
    apply_taxonomy: bool,
    workflow_effect_executor: WorkflowEffectExecutor | None = None,
) -> JsonObject:
    runtime_source = _runtime_source_model(source)
    facts = _fix_wiki_facts_from_runtime_source(runtime_source, apply_taxonomy=apply_taxonomy)
    if workflow_effect_executor is not None:
        facts = _execute_fix_wiki_effects_until_pause(
            facts,
            workflow_effect_executor=workflow_effect_executor,
        )
    payload = build_fix_wiki_fsm_result(facts).to_payload()
    assert_fix_wiki_fsm_payload(payload)
    return payload


def _execute_fix_wiki_effects_until_pause(
    facts: FixWikiFsmFacts,
    *,
    workflow_effect_executor: WorkflowEffectExecutor,
) -> FixWikiFsmFacts:
    """Execute FSM-emitted effects as a StateChart-driven chain.

    Each adapter result is folded back through `fix_wiki_event_from_effect_result`
    before the next effect is considered. That keeps `health.py` as composition:
    it runs already-authorized effects, while the machine decides the next state
    and whether another effect is executable.
    """

    executed: set[tuple[str, str, str, str]] = set()
    for _ in range(16):
        effects = list(facts.machine_effects)
        if not effects:
            return facts
        progressed = False
        for effect in effects:
            key = (effect.kind.value, effect.target, effect.origin_state, effect.effect_id)
            if key in executed:
                return facts
            try:
                result = workflow_effect_executor.execute(effect)
            except MissingWorkflowEffectAdapter:
                if missing_fix_wiki_effect_adapter_is_optional(effect):
                    return facts
                raise
            executed.add(key)
            facts = _facts_after_effect_results(facts, [result])
            progressed = True
            if effect_result_stops_fix_wiki_execution(result):
                return facts
            break
        if not progressed:
            return facts
    raise RuntimeError("fix-wiki effect execution exceeded statechart loop limit")


def _runtime_source_model(source: JsonObject | _FixWikiFsmRuntimeSource) -> _FixWikiFsmRuntimeSource:
    """Validate the last fix-wiki runtime boundary before any FSM projection."""

    if isinstance(source, _FixWikiFsmRuntimeSource):
        return source
    return _FixWikiFsmRuntimeSource.model_validate(source)


def _fix_wiki_facts_from_runtime_source(
    runtime_source: _FixWikiFsmRuntimeSource,
    *,
    apply_taxonomy: bool,
) -> FixWikiFsmFacts:
    """Build the canonical StateChart facts from the strict runtime boundary."""

    decision = _fix_wiki_fsm_human_decision(runtime_source)
    human_decision_packet = decision.to_human_decision_packet() if decision is not None else None
    return fix_wiki_fsm_facts_from_runtime(
        run_id=runtime_source.run_id,
        requested_apply=runtime_source.requested_apply,
        effective_apply=runtime_source.effective_apply,
        total_changed_count=runtime_source.total_changed_count,
        vault_changed_file_count=runtime_source.change_count_context.vault_changed_file_count,
        written_count=runtime_source.change_count_context.written_count,
        warning_count=runtime_source.warning_count_for_fsm,
        requires_llm_rewrite_count=runtime_source.requires_llm_rewrite_count,
        final_validation=_json_object(runtime_source.final_validation.model_dump(mode="json")),
        version_control_safety=_fix_wiki_fsm_version_control_safety(runtime_source),
        artifacts=runtime_source.artifact_paths(),
        related_notes_blocked=runtime_source.related_notes_blocked,
        related_notes_recovery_state=runtime_source.related_notes_recovery_state,
        vocabulary_semantic_ingestion_pending=runtime_source.vocabulary_semantic_ingestion_pending,
        vocabulary_eval_needs_review=runtime_source.vocabulary_eval_needs_review,
        atomicity_split_required=runtime_source.atomicity_split_required,
        merge_review_required=runtime_source.merge_review_required,
        human_decision_required=decision is not None,
        decision=decision,
        human_decision_packet=human_decision_packet,
        changed_files=runtime_source.changed_files_for_fsm(),
        graph_error_count=_payload_int(runtime_source.graph_validation, "error_count"),
        graph_blocker_count=_payload_int(runtime_source.graph_validation, "blocker_count"),
        graph_review_required=runtime_source.graph_review_required,
        linker_blocked=_fix_wiki_linker_blocker_present(runtime_source),
        linker_apply_attempted=_fix_wiki_linker_apply_attempted(runtime_source),
        taxonomy_action_required=runtime_source.taxonomy_action_required,
        failed=runtime_source.failed,
        failed_reason_code=runtime_source.failed_reason_code,
        vault_guard_required=runtime_source.vault_guard_required,
        environment_windows_path_or_venv_blocked=runtime_source.environment_windows_path_or_venv_blocked,
        next_action=runtime_source.next_action_for_fsm(),
        required_inputs=runtime_source.required_inputs,
        resume_action="",
        pending_effects=_fix_wiki_pending_effects_from_report(runtime_source),
        diagnostic_context=runtime_source.diagnostic_context_for_fsm(apply_taxonomy=apply_taxonomy),
        error_context=runtime_source.error_context,
    )


def _fix_wiki_progress_status_from_runtime_source(
    source: JsonObject | _FixWikiFsmRuntimeSource,
    *,
    apply_taxonomy: bool,
) -> str:
    """Ask the StateChart projection for the public progress status."""

    runtime_source = _runtime_source_model(source)
    facts = _fix_wiki_facts_from_runtime_source(runtime_source, apply_taxonomy=apply_taxonomy)
    return build_fix_wiki_fsm_result(facts).progress_view_model.status.value


def _fix_wiki_pending_effects_from_report(source: _FixWikiFsmRuntimeSource) -> list[JsonObject]:
    """Delegate executable effect derivation to the fix-wiki effects domain."""

    return pending_effect_payloads_from_fix_wiki_runtime_source(source.to_lens_payload())


def _has_automatic_style_rewrite_group(blocker_resolution: _BlockerResolutionFields) -> bool:
    return any(group.route == "style_rewrite" and group.automatic for group in blocker_resolution.groups)


def _fix_wiki_fsm_human_decision(source: _FixWikiFsmRuntimeSource) -> WorkflowDecision | None:
    if not source.human_decision_required:
        return None
    blocked_reason = source.human_decision_reason_code or "human_decision_required"
    next_action = "Resolver a decisão humana pendente antes de continuar."
    decision = _fix_wiki_workflow_decision(
        source.human_decision_packets,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=source.required_inputs,
    )
    if decision is not None:
        return decision
    raise ValueError("human_decision_packet is required when fix-wiki reports human_decision_required")


def _fix_wiki_fsm_version_control_safety(source: _FixWikiFsmRuntimeSource) -> JsonObject:
    safety = source.version_control_safety
    mutated = source.effective_apply and (
        source.change_count_context.vault_changed_file_count > 0
        or source.total_changed_count > 0
        or source.change_count_context.written_count > 0
    )
    preserved_safety = dict(safety)
    preserved_safety["no_resource_mutation"] = not mutated
    preserved_safety["rollback_declared"] = bool(
        _json_field(_json_object(preserved_safety), "rollback_declared")
        or _json_field(_json_object(preserved_safety), "restore_point_before")
        or not mutated
    )
    if preserved_safety:
        return _json_object(preserved_safety)
    return _json_object({
        "no_resource_mutation": not mutated,
        "rollback_declared": not mutated,
    })


def _fix_wiki_linker_apply_attempted(source: _FixWikiFsmRuntimeSource) -> bool:
    """Authorize linker execution evidence only from the child link FSM artifact."""

    if source.linker_apply is None:
        return False
    return _fix_wiki_linker_apply_attempted_from_payload(source.linker_apply)


def _fix_wiki_linker_apply_attempted_from_payload(payload: JsonObject | None) -> bool:
    """Validate child link apply artifacts before they authorize follow-up work."""

    if payload is None:
        return False
    normalize_link_runtime_artifact(payload)
    return True


def _fix_wiki_linker_blocker_present(source: _FixWikiFsmRuntimeSource) -> bool:
    """Derive linker blocker truth from the child FSM, not legacy status fields."""

    fields = _FixWikiLinkerReportFields.model_validate(source.to_lens_payload())
    if fields.linker_blocked or bool(fields.linker_skipped_reason):
        return True
    if source.linker_apply is None:
        return False
    child_status = _link_fsm_progress_value(source.linker_apply)
    return child_status in {"blocked", "failed", "completed_with_link_blockers"}


def _human_decision_packets_for_unpacketized_blocker(
    *,
    graph_error_count: int,
    graph_audit_report: JsonObject,
    linker_skipped_reason: str,
) -> list[JsonObject]:
    """Create a structured decision packet for blockers detected outside resolution groups."""

    graph_errors = _GraphAuditErrorsFields.model_validate(graph_audit_report).errors
    if graph_error_count:
        return project_fix_wiki_human_decision_packets(
            {
                "groups": [
                    {
                        "route": "graph_review_required",
                        "automatic": False,
                        "reason": "O grafo ainda tem pendências que exigem revisão humana antes de concluir.",
                        "next_action": (
                            "Revisar os itens do grafo, resolver a classificação ou ligação ambígua "
                            "e repetir a conferência da Wiki pela rota oficial."
                        ),
                        "sample": _issue_sample(graph_errors),
                    }
                ]
            }
        )
    route = linker_skipped_reason.strip() or "human_decision_required"
    return project_fix_wiki_human_decision_packets(
        {
            "groups": [
                {
                    "route": route,
                    "automatic": False,
                    "reason": "O workflow encontrou uma escolha humana obrigatória antes de continuar.",
                    "next_action": "Revisar os itens bloqueados e repetir a conferência da Wiki pela rota oficial.",
                    "sample": [],
                }
            ]
        }
    )


def _human_decision_kinds(packets: Sequence[JsonObject]) -> list[str]:
    kinds: list[str] = []
    seen: set[str] = set()
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        packet_fields = _HumanDecisionPacketFields.model_validate(packet)
        kind = packet_fields.kind.strip()
        if kind and kind not in seen:
            kinds.append(kind)
            seen.add(kind)
    return kinds


def _blocking_reasons(
    *,
    blocked_reason: str,
    write_error_count: int,
    vocabulary_blocked_reason: str,
    alias_projection_blocked: bool,
    alias_projection_apply: JsonObject | None,
    atomicity_split_required: bool,
    requires_llm_rewrite_count: int,
    graph_error_count: int,
    linker_blocked: bool,
    linker_skipped_reason: str,
    linker_payload: JsonObject,
    related_notes_blocker: bool,
    taxonomy_action_required: bool,
    human_decision_required: bool,
    human_decision_kinds: list[str],
) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()

    def add(reason: str | None) -> None:
        value = reason.strip() if isinstance(reason, str) else ""
        if value and value not in seen:
            reasons.append(value)
            seen.add(value)

    alias_projection_fields = _FixWikiStatusActionFields.model_validate(alias_projection_apply or {})
    linker_fields = _FixWikiStatusActionFields.model_validate(linker_payload)

    add(blocked_reason)
    if write_error_count:
        add("write_errors")
    add(vocabulary_blocked_reason)
    if alias_projection_blocked:
        add(alias_projection_fields.blocked_reason or "alias_projection_blocked")
    if atomicity_split_required:
        add("atomicity_split_required")
    if requires_llm_rewrite_count:
        add("requires_llm_rewrite")
    if graph_error_count:
        add("graph_blockers")
    if linker_blocked:
        add(linker_skipped_reason or linker_fields.blocked_reason or "linker_blocked")
    if related_notes_blocker:
        add("related_notes_blocked")
    if taxonomy_action_required:
        add("taxonomy_action_required")
    for kind in human_decision_kinds:
        add(kind)
    if human_decision_required and not human_decision_kinds:
        add("human_decision_required")
    return reasons


def _summary(
    *,
    status: str,
    requested_apply: bool,
    total_changed_count: int,
    requires_llm_rewrite_count: int,
    graph_error_count: int,
    linker_blocked: bool,
    related_notes_blocked: bool,
    taxonomy_action_required: bool,
    taxonomy_apply_requires_confirmation: bool,
    human_decision_required: bool,
) -> str:
    return _summary_text(
        _summary_reason(
            status=status,
            requested_apply=requested_apply,
            total_changed_count=total_changed_count,
            requires_llm_rewrite_count=requires_llm_rewrite_count,
            graph_error_count=graph_error_count,
            linker_blocked=linker_blocked,
            related_notes_blocked=related_notes_blocked,
            taxonomy_action_required=taxonomy_action_required,
            taxonomy_apply_requires_confirmation=taxonomy_apply_requires_confirmation,
            human_decision_required=human_decision_required,
        )
    )


def _summary_reason(
    *,
    status: str,
    requested_apply: bool,
    total_changed_count: int,
    requires_llm_rewrite_count: int,
    graph_error_count: int,
    linker_blocked: bool,
    related_notes_blocked: bool,
    taxonomy_action_required: bool,
    taxonomy_apply_requires_confirmation: bool,
    human_decision_required: bool,
) -> FixWikiSummaryReason:
    if not requested_apply:
        if status == "completed":
            return FixWikiSummaryReason.DRY_RUN_CLEAN
        if status == "completed_with_warnings":
            return FixWikiSummaryReason.DRY_RUN_WARNINGS
        return FixWikiSummaryReason.DRY_RUN_BLOCKED
    if status == "completed":
        return FixWikiSummaryReason.COMPLETED
    if status == "completed_with_warnings":
        return FixWikiSummaryReason.COMPLETED_WITH_WARNINGS
    if taxonomy_apply_requires_confirmation:
        return FixWikiSummaryReason.TAXONOMY_CONFIRMATION
    if human_decision_required:
        return FixWikiSummaryReason.HUMAN_DECISION
    if graph_error_count:
        return FixWikiSummaryReason.GRAPH_BLOCKED
    if related_notes_blocked:
        return FixWikiSummaryReason.RELATED_NOTES_BLOCKED
    if linker_blocked:
        return FixWikiSummaryReason.LINKER_BLOCKED
    if requires_llm_rewrite_count:
        return FixWikiSummaryReason.STYLE_REWRITE_REQUIRED
    if taxonomy_action_required:
        return FixWikiSummaryReason.TAXONOMY_ACTION_REQUIRED
    if total_changed_count:
        return FixWikiSummaryReason.CHANGED_WITH_UNSPECIFIED_BLOCKER
    return FixWikiSummaryReason.OPERATIONAL_BLOCKER


def _summary_text(reason: FixWikiSummaryReason) -> str:
    match reason:
        case FixWikiSummaryReason.DRY_RUN_CLEAN:
            return "fix-wiki gerou diagnóstico dry-run sem blockers técnicos."
        case FixWikiSummaryReason.DRY_RUN_WARNINGS:
            return "fix-wiki gerou diagnóstico dry-run com warnings não bloqueantes."
        case FixWikiSummaryReason.DRY_RUN_BLOCKED:
            return "fix-wiki gerou diagnóstico dry-run e parou em pendências operacionais/semânticas."
        case FixWikiSummaryReason.COMPLETED:
            return "fix-wiki concluiu sem blockers técnicos."
        case FixWikiSummaryReason.COMPLETED_WITH_WARNINGS:
            return "fix-wiki concluiu os reparos determinísticos e deixou apenas warnings não bloqueantes."
        case FixWikiSummaryReason.TAXONOMY_CONFIRMATION:
            return "fix-wiki não moveu taxonomia; há movimentos aguardando confirmação explícita."
        case FixWikiSummaryReason.HUMAN_DECISION:
            return "fix-wiki aplicou reparos determinísticos e parou em decisões humanas/semânticas."
        case FixWikiSummaryReason.STYLE_REWRITE_REQUIRED:
            return "fix-wiki aplicou reparos determinísticos e precisa de reescrita assistida para concluir."
        case FixWikiSummaryReason.GRAPH_BLOCKED:
            return "fix-wiki aplicou reparos determinísticos, mas ainda há blockers de grafo."
        case FixWikiSummaryReason.RELATED_NOTES_BLOCKED:
            return "fix-wiki aplicou reparos determinísticos, mas ainda precisa atualizar o export do Related Notes."
        case FixWikiSummaryReason.LINKER_BLOCKED:
            return "fix-wiki aplicou reparos determinísticos, mas o linker ainda está bloqueado."
        case FixWikiSummaryReason.TAXONOMY_ACTION_REQUIRED:
            return "fix-wiki encontrou taxonomia pendente; revise o plano antes de mover pastas."
        case FixWikiSummaryReason.CHANGED_WITH_UNSPECIFIED_BLOCKER:
            return "fix-wiki aplicou mudanças e deixou uma pendência operacional para a rota oficial resolver."
        case FixWikiSummaryReason.OPERATIONAL_BLOCKER:
            return "fix-wiki está bloqueado por pendência operacional."


def _fix_wiki_user_summary(
    *,
    status: str,
    blocked_reason: str,
    total_changed_count: int,
    linker_status: str,
    rollback_command: str | None,
    human_report_path: Path,
    related_notes_export_recovery: JsonObject | None = None,
) -> str:
    parts = [
        f"status={status}",
        f"mudanças={total_changed_count}",
        f"linker={linker_status}",
        f"relatório={human_report_path}",
    ]
    if blocked_reason:
        parts.append(f"bloqueio={blocked_reason}")
    related_notes_recovery = _related_notes_recovery_summary(related_notes_export_recovery)
    if related_notes_recovery:
        parts.append(f"related_notes={related_notes_recovery}")
    if rollback_command:
        parts.append(f"rollback={rollback_command}")
    return "; ".join(parts)


def _related_notes_recovery_summary(payload: JsonObject | None) -> str:
    if not isinstance(payload, dict) or not payload:
        return ""
    fields = _RelatedNotesRecoveryPayloadFields.model_validate(payload)
    status = fields.status.strip()
    blocked_reason = fields.blocked_reason.strip()
    recovery_mode = (fields.selected_recovery_mode or fields.recovery_mode).strip()
    if blocked_reason:
        return ":".join(part for part in [status or "blocked", blocked_reason, recovery_mode] if part)
    if status and status != "recovered":
        return ":".join(part for part in [status, recovery_mode] if part)
    return ""


def _total_changed_count(
    *,
    style_fix: JsonObject,
    sources_backfill: JsonObject | None = None,
    alias_projection_apply: JsonObject | None,
    graph_fix: JsonObject,
    taxonomy_apply: JsonObject | None,
    linker_apply: JsonObject | None,
    hygiene_pre_cleanup: JsonObject | None,
    hygiene_cleanup: JsonObject | None,
    related_notes_safety_cleanup: JsonObject | None = None,
) -> int:
    paths: set[str] = set()
    style_fields = _FixWikiRepairFactsFields.model_validate(style_fix)
    sources_fields = _FixWikiRepairFactsFields.model_validate(sources_backfill or {})
    alias_fields = _AliasProjectionApplyFields.model_validate(alias_projection_apply or {})
    graph_fields = _GraphFixTriggerFields.model_validate(graph_fix)
    taxonomy_fields = _TaxonomyApplyFields.model_validate(taxonomy_apply or {})
    linker_fields = _ChangedMarkdownFilesFields.model_validate(linker_apply or {})
    related_cleanup_fields = _ChangedMarkdownFilesFields.model_validate(related_notes_safety_cleanup or {})
    for report in style_fields.reports:
        report_fields = _FixWikiWrittenReportFields.model_validate(_json_object_or_empty(report))
        if report_fields.wrote and report_fields.path:
            paths.add(report_fields.path)
    for receipt in alias_fields.receipts:
        if receipt.status == "applied" and receipt.before_hash != receipt.after_hash and receipt.note_path:
            paths.add(receipt.note_path)
    for report in graph_fields.reports:
        if report.wrote and report.path:
            paths.add(report.path)
    for operation in taxonomy_fields.applied_operations:
        path = operation.destination or operation.source
        if path:
            paths.add(path)
    for path in linker_fields.changed_files:
        if path:
            paths.add(path)
    for path in related_cleanup_fields.changed_files:
        if path:
            paths.add(path)
    if paths:
        return len(paths)
    return (
        style_fields.written_count
        + sources_fields.written_count
        + alias_fields.applied_count
        + graph_fields.written_count
        + taxonomy_fields.applied_count
        + linker_fields.files_changed
        + related_cleanup_fields.changed_file_count
    )


def _changed_paths_from(
    *,
    style_fix: JsonObject,
    sources_backfill: JsonObject,
    alias_projection_apply: JsonObject | None,
    taxonomy_apply: JsonObject | None,
    linker_apply: JsonObject | None,
    hygiene_pre_cleanup: JsonObject | None,
    hygiene_cleanup: JsonObject | None,
    related_notes_safety_cleanup: JsonObject | None = None,
) -> list[JsonObject]:
    changes: list[JsonObject] = []
    style_fields = _FixWikiRepairFactsFields.model_validate(style_fix)
    sources_fields = _FixWikiRepairFactsFields.model_validate(sources_backfill)
    alias_fields = _AliasProjectionApplyFields.model_validate(alias_projection_apply or {})
    taxonomy_fields = _TaxonomyApplyFields.model_validate(taxonomy_apply or {})
    linker_fields = _ChangedMarkdownFilesFields.model_validate(linker_apply or {})
    related_cleanup_fields = _ChangedMarkdownFilesFields.model_validate(related_notes_safety_cleanup or {})
    for report in style_fields.reports:
        report_fields = _FixWikiWrittenReportFields.model_validate(_json_object_or_empty(report))
        if report_fields.wrote:
            changes.append(_json_object({"path": report_fields.path, "phase": "style_yaml", "action": "rewrite_note"}))
    for report in sources_fields.reports:
        report_fields = _FixWikiWrittenReportFields.model_validate(_json_object_or_empty(report))
        if report_fields.wrote:
            changes.append(
                _json_object({"path": report_fields.path, "phase": "provenance_backfill", "action": "backfill_sources"})
            )
    for receipt in alias_fields.receipts:
        if receipt.status == "applied" and receipt.before_hash != receipt.after_hash:
            changes.append(_json_object({"path": receipt.note_path, "phase": "alias_projection", "action": "project_aliases"}))
    for operation in taxonomy_fields.applied_operations:
        changes.append(
            {
                "path": operation.destination or operation.source,
                "phase": "taxonomy",
                "action": operation.action or "move_dir",
            }
        )
    for path in linker_fields.changed_files:
        changes.append({"path": path, "phase": "linker", "action": "linker_apply"})
    for path in related_cleanup_fields.changed_files:
        changes.append({"path": path, "phase": "related_notes_safety_cleanup", "action": "remove_invalid_related_link"})
    for cleanup, phase in ((hygiene_pre_cleanup, "hygiene_preflight"), (hygiene_cleanup, "hygiene_final")):
        cleanup_fields = _PostStyleHygieneCleanupFields.model_validate(cleanup or {})
        if cleanup_fields.removed_empty_dir_entries:
            for fields in cleanup_fields.removed_empty_dir_entries:
                changes.append(
                    {
                        "path": fields.path,
                        "phase": fields.phase or "structure_empty_dir_cleanup",
                        "action": fields.action or "remove_empty_dir",
                        "problem_code": fields.problem_code or "structure.empty_dir.present",
                    }
                )
        else:
            for path in cleanup_fields.removed_empty_dirs:
                changes.append({"path": path, "phase": phase, "action": "remove_empty_dir"})
        for fields in cleanup_fields.removed_empty_root_note_entries:
            changes.append(
                {
                    "path": fields.path,
                    "phase": fields.phase or "structure_empty_root_note_cleanup",
                    "action": fields.action or "archive_empty_root_note",
                    "problem_code": fields.problem_code or "structure.empty_root_note.present",
                }
            )
        for fields in cleanup_fields.archived:
            changes.append({"path": fields.source, "phase": phase, "action": "archive_operational_file"})
    return [item for item in changes if _HygienePathEntryFields.model_validate(item).path]


def _fix_wiki_workflow_decision(
    packets: Sequence[JsonObject],
    *,
    blocked_reason: str,
    next_action: str,
    required_inputs: list[str] | None = None,
) -> WorkflowDecision | None:
    if not packets:
        return None
    primary = HumanDecisionPacket.model_validate(packets[0])
    resume_action = primary.resume_action or next_action
    summary = primary.decision_summary
    return WorkflowDecision(
        kind="ask_human",
        phase=summary.phase,
        reason_code=blocked_reason or summary.reason_code,
        public_summary=summary.public_summary,
        developer_summary=summary.developer_summary,
        evidence=summary.evidence,
        rejected_automations=primary.rejected_automations,
        next_action=resume_action,
        required_inputs=list(required_inputs or []),
        resume_action=resume_action,
        recommended_option_id=primary.recommended_option_id,
        options=primary.options,
        human_decision_kind=primary.kind,
    )


def _fix_wiki_decision_packet(
    packets: Sequence[JsonObject],
    *,
    blocked_reason: str,
    next_action: str,
    required_inputs: list[str] | None = None,
) -> JsonObject | None:
    if packets:
        return _json_object(packets[0])
    decision = _fix_wiki_workflow_decision(
        packets,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=required_inputs,
    )
    if decision is None:
        return None
    packet = decision.to_human_decision_packet()
    packet["decision_kind"] = decision.human_decision_kind
    return _json_object(packet)


def _human_decision_next_action(packets: Sequence[JsonObject]) -> str:
    if not packets:
        return ""
    primary = _HumanDecisionPacketFields.model_validate(packets[0])
    return primary.resume_action


def _demote_subreport_followup(
    payload: JsonObject,
    *,
    source_path: str,
    superseded_by: str,
    active_workflow_followup: str,
) -> JsonObject:
    result = _json_object(payload)
    local_next_action = _FixWikiStatusActionFields.model_validate(result).next_action
    if not local_next_action or not active_workflow_followup:
        return _json_object(result)
    result.pop("next_action", None)
    result["local_next_action"] = local_next_action
    result["next_action_status"] = "superseded"
    result["next_action_public"] = False
    result["superseded_by"] = superseded_by
    result["source_path"] = source_path
    return _json_object(result)


def _fix_wiki_has_executable_style_rewrite_batch(
    *,
    requested_apply: bool,
    effective_apply: bool,
    human_decision_required: bool,
    graph_error_count: int,
    linker_blocked: bool,
    related_notes_blocked: bool,
    taxonomy_action_required: bool,
    blocker_resolution: JsonObject,
    rewrite_plan: JsonObject | None,
    requires_llm_rewrite_count: int,
) -> bool:
    """Collapse style-rewrite readiness into one fact owned by the FSM projection."""

    if (
        not requested_apply
        or not effective_apply
        or human_decision_required
        or graph_error_count
        or linker_blocked
        or related_notes_blocked
        or taxonomy_action_required
        or requires_llm_rewrite_count <= 0
    ):
        return False
    if not isinstance(rewrite_plan, dict):
        return False
    typed_plan = _StyleRewritePlanFields.model_validate(rewrite_plan)
    if typed_plan.status != "ready":
        return False
    if not typed_plan.work_items:
        return False
    typed_blockers = _BlockerResolutionFields.model_validate(blocker_resolution)
    return _has_automatic_style_rewrite_group(typed_blockers)


def _fix_wiki_apply_guidance(
    *,
    requested_apply: bool,
    effective_apply: bool,
    linker_diagnosis: JsonObject,
    blocker_resolution: JsonObject,
    style_rewrite_batch_ready: bool,
) -> JsonObject:
    internal_follow_up: list[JsonObject] = []
    linker_fields = _FixWikiApplyGuidanceLinkerFields.model_validate(linker_diagnosis)
    blocker_fields = _BlockerResolutionFields.model_validate(blocker_resolution)
    linker_local_next_action = linker_fields.local_next_action or linker_fields.next_action
    if linker_local_next_action:
        internal_follow_up.append(
            {
                "source_path": "linker_diagnosis.local_next_action",
                "public": False,
                "authorized_now": False,
                "summary": linker_local_next_action,
            }
        )
    for group in blocker_fields.groups:
        if group.route != "style_rewrite":
            continue
        internal_follow_up.append(
            {
                "source_path": "blocker_resolution.groups[style_rewrite]",
                "public": False,
                "authorized_now": style_rewrite_batch_ready,
                "summary": group.next_action
                or "Preparar reescrita assistida, validar e aplicar somente versões aprovadas.",
            }
        )
    return _json_object({
        "schema": "medical-notes-workbench.fix-wiki-apply-guidance.v1",
        "requested_apply": requested_apply,
        "effective_apply": effective_apply,
        "apply_executed": bool(effective_apply),
        "public_apply_suggested": bool(requested_apply and style_rewrite_batch_ready),
        "internal_follow_up_mentions": internal_follow_up,
        "agent_instruction": (
            "Se perguntarem se houve apply: reporte requested_apply/effective_apply e diferencie "
            "execução pública de menções técnicas futuras em subplanos."
        ),
    })


def _fix_wiki_change_count_context(
    *,
    requested_apply: bool,
    effective_apply: bool,
    style_fix: JsonObject,
    total_changed_count: int,
    version_control_mutation_summary: JsonObject,
) -> JsonObject:
    style_fields = _FixWikiRepairFactsFields.model_validate(style_fix)
    changed_count = style_fields.changed_count
    written_count = style_fields.written_count
    raw_vault_changed_file_count = _payload_int(version_control_mutation_summary, "changed_file_count")
    vault_changed_file_count = _public_vault_changed_file_count(version_control_mutation_summary)
    return _json_object({
        "schema": "medical-notes-workbench.fix-wiki-change-count-context.v1",
        "requested_apply": requested_apply,
        "effective_apply": effective_apply,
        "changed_count": changed_count,
        "changed_count_meaning": "planned_style_repairs" if not effective_apply else "style_repairs_detected_in_apply_run",
        "changed_count_applied": bool(effective_apply),
        "planned_change_count": changed_count if not effective_apply else 0,
        "written_count": written_count,
        "total_changed_count": total_changed_count,
        "vault_changed_file_count": vault_changed_file_count,
        "raw_vault_changed_file_count": raw_vault_changed_file_count,
        "transient_vault_changed_file_count": (
            raw_vault_changed_file_count if raw_vault_changed_file_count != vault_changed_file_count else 0
        ),
        "agent_instruction": (
            "Em prévia, changed_count não é mutação no vault. Para mutação real, reporte "
            "written_count, total_changed_count e change_count_context.vault_changed_file_count. "
            "raw_vault_changed_file_count é diagnóstico técnico e pode incluir backups transitórios."
        ),
    })


def _requires_subagent_orchestration(blocker_resolution: JsonObject) -> bool:
    fields = _BlockerResolutionFields.model_validate(blocker_resolution)
    return any(
        group.route in {"style_rewrite", "note_merge_required"}
        for group in fields.groups
    )


def _blocker_resolution_next_action(blocker_resolution: JsonObject, *, preferred_route: str = "") -> str:
    fields = _BlockerResolutionFields.model_validate(blocker_resolution)
    if preferred_route:
        for group in fields.groups:
            if group.route == preferred_route and group.next_action:
                return group.next_action
    return fields.next_action


def _diagnostic_blocker_resolution(blocker_resolution: JsonObject) -> JsonObject:
    fields = _BlockerResolutionDiagnosticFields.model_validate(blocker_resolution)
    return _json_object(fields.model_dump(mode="json", by_alias=True, exclude_none=True))


def _specialist_agent_invocation_contract(agent_name: str) -> JsonObject:
    template_filename = f"{agent_name}.md"
    home = Path.home()
    return {
        "schema": "medical-notes-workbench.specialist-agent-invocation-contract.v1",
        "agent_name": agent_name,
        "model_policy": "medical_specialist_authoring.v1",
        "required_model_tier": "specialist",
        "preferred_model_tier": "pro",
        "authoring_mode": "parallel",
        "authoring_max_concurrency": DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY,
        "apply_mode": "serial",
        "serial_apply_required": True,
        "parent_packet_contract": {
            "input": "agent_directive.control.effects[].payload.current_batch_items[]",
            "forbidden_parent_payloads": ["raw_markdown_content", "raw_chat", "clinical_note_body"],
            "output": "temp_output from the work item",
            "receipt": "medical-notes-workbench.specialist-task-run-receipt.v1",
        },
        "gemini_cli": {
            "route_kind": "packaged_agent_effect",
            "tool": "invoke_agent",
            "agent_name": agent_name,
            "prompt_input": "agent_directive.control.effects[].payload.current_batch_items[]",
            "one_agent_per_item": True,
            "execution_mode": "parallel_authoring_serial_apply",
            "authoring_max_concurrency": DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY,
            "apply_mode": "serial",
            "wait_for_all_authoring_outputs_before_apply": True,
            "parent_steps": [
                "launch at most authoring_max_concurrency packaged specialist calls, one per current_batch_items entry",
                "collect temp_output and the official specialist-task-run-receipt for every launched item before apply",
                "do not add sleep, shell chaining or wait_for_previous; use the runtime's native task completion results",
                "do not fabricate specialist_task_run_receipt_path or receipt JSON manually",
                "apply accepted rewrites serially with apply-specialist-style-rewrite --specialist-run-receipt",
            ],
            "requires_opencode": False,
        },
        "antigravity_cli": {
            "route_kind": "packaged_template_subagent",
            "tool_sequence": ["view_file", "define_subagent", "invoke_subagent"],
            "agent_name": agent_name,
            "prompt_input": "agent_directive.control.effects[].payload.current_batch_items[]",
            "one_agent_per_item": True,
            "execution_mode": "parallel_authoring_serial_apply",
            "authoring_max_concurrency": DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY,
            "apply_mode": "serial",
            "wait_for_all_authoring_outputs_before_apply": True,
            "allowed_define_subagent": True,
            "define_subagent_source": "packaged_agent_template_only",
            "receipt_finalizer": {
                "command_family": "finalize-agy-specialist-task",
                "arguments": [
                    "--plan",
                    "agent_directive.control.effects[].payload.plan_path",
                    "--work-id",
                    "current_batch_items[].work_id",
                    "--transcript",
                    "agy_transcript_or_task_log_path",
                    "--runtime-log",
                    "agy_cli_log_path_when_settings_switch_used",
                    "--json",
                ],
                "requires_model_evidence": "agy_transcript_metadata_or_settings_switch_runtime_log",
            },
            "parent_steps": [
                "view_file agents/med-knowledge-architect.md from the installed medical-notes-workbench plugin before define_subagent",
                "define_subagent from the complete packaged template; do not invent a short system prompt",
                "launch at most authoring_max_concurrency invoke_subagent calls, each with Prompt equal to one typed current_batch_item JSON and no raw Markdown/chat content",
                "collect temp_output, then run finalize-agy-specialist-task for each item with the AGY transcript/task log evidence and runtime log when a settings-switch window is used",
                "collect official specialist-task-run-receipts for the launched batch before apply",
                "do not use the legacy Gemini runner, --gemini-binary, scratch scripts or mocks from AGY",
                "apply accepted rewrites serially with apply-specialist-style-rewrite --specialist-run-receipt",
            ],
            "requires_opencode": False,
            "blocked_reason_if_missing": "packaged_agent_template_unavailable",
            "agent_instruction": (
                "No AGY, use a rota packaged_template_subagent: leia o template empacotado completo, "
                "chame define_subagent, invoque um work_item tipado por vez com Prompt igual ao JSON do item e finalize com "
                "finalize-agy-specialist-task usando evidência de transcript/task log e runtime log quando houver settings switch. Não use "
                "rota Gemini legada, prompt mínimo ou mock/scratch script."
            ),
            "template_path_candidates": [
                str(home / ".gemini" / "config" / "plugins" / "medical-notes-workbench" / "agents" / template_filename),
                str(home / ".gemini" / "antigravity-cli" / "plugins" / "medical-notes-workbench" / "agents" / template_filename),
            ],
            "required_template_markers": [
                PACKAGED_AGENT_TEMPLATE_CONTRACT,
                'You = "A Mente"',
                "Parent packet contract:",
                "parent_raw_content_bypass",
            ],
            "required_prompt_fields": [
                "packaged_agent_template_contract",
                "name",
                "description",
                "model",
                "tools",
                "Parent packet contract",
                "parent_raw_content_bypass",
            ],
            "forbidden_system_prompt_sources": [
                "parent_memory",
                "short_handwritten_prompt",
                "scratch_script",
                "raw_markdown_content",
            ],
        },
        "opencode": {
            "route_kind": "task_subagent",
            "requires_installation": True,
            "requires_opencode": True,
            "agent_name": agent_name,
            "execution_mode": "parallel_authoring_serial_apply",
            "authoring_max_concurrency": DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY,
            "apply_mode": "serial",
            "parent_orchestrator_model": "antigravity/gemini-3.5-flash when available",
            "expected_model": "antigravity/gemini-3.1-pro",
            "model_policy": {
                "schema": "medical-notes-workbench.specialist-model-policy.v1",
                "orchestrator_allows_flash": True,
                "specialist_forbid_flash_fallback": True,
                "specialist_expected_model": "antigravity/gemini-3.1-pro",
                "accepted_specialist_model_tokens": ["pro", "opus", "sonnet", "specialist"],
                "forbidden_specialist_model_tokens": ["flash", "lite", "nano"],
            },
            "task_contract": {
                "tool": "task",
                "prompt_contract": "single_current_batch_items_json",
                "root_keys": ["current_batch_items"],
                "forbidden_prompt_sources": [
                    "raw_markdown_content",
                    "raw_chat_content",
                    "short_handwritten_prompt",
                ],
                "required_fields": [
                    "work_id",
                    "target_path",
                    "target_hash_before",
                    "rewrite_prompt",
                    "temp_output",
                    "specialist_task_run_receipt_path",
                    "subagent_output_contract",
                ],
            },
            "forbidden_parent_discovery_tools": [
                "glob",
                "grep",
                "grep_search",
                "list_dir",
                "list_directory",
                "search_file_content",
            ],
            "receipt_finalizer": {
                "command_family": "finalize-opencode-specialist-task",
                "executable_command": wiki_cli_relative_command("finalize-opencode-specialist-task"),
                "required_args": ["--plan", "--work-id", "--json"],
                "optional_args": ["--task-metadata"],
                "model_evidence_source": "opencode_task_metadata",
                "default_metadata_source": "hook-captured-by-work-id",
            },
            "parent_steps": [
                "OpenCode parent may use Flash for orchestration only.",
                "Launch at most authoring_max_concurrency OpenCode task subagents, each with pure JSON containing one current_batch_items[] item.",
                "Do not paste raw note Markdown or raw chat content into the task prompt.",
                "Do not use glob/grep/list_dir/search to rediscover target_path, route or finalizer while current_batch_items[] is executable.",
                f"After task completion, run `{wiki_cli_relative_command('finalize-opencode-specialist-task')}` without --task-metadata; the hook captures OpenCode task metadata by work_id.",
                "Apply accepted rewrites serially with apply-specialist-style-rewrite --specialist-run-receipt using next_apply_step.arguments.",
            ],
        },
        "direct_api": {
            "route_kind": "explicit_opt_in",
            "enabled_by_default": False,
            "requires_user_configuration": True,
        },
    }


def _agent_workspace_requirements_for_work_item(work_item: JsonObject) -> JsonObject:
    return _agent_workspace_requirements_for_work_items([work_item] if work_item else [])


def _agent_workspace_requirements_for_work_items(work_items: Sequence[JsonObject]) -> JsonObject:
    required_workspace_dirs: list[str] = []
    required_write_dirs: list[str] = []
    required_read_dirs: list[str] = []
    required_read_files: list[str] = []
    forbidden_discovery_roots: list[str] = []
    temp_outputs: list[str] = []
    gemini_cli_include_directories: list[str] = []

    def add_unique(values: list[str], value: str) -> None:
        if value and value not in values:
            values.append(value)

    for work_item in work_items:
        fields = _StyleRewriteWorkItemFields.model_validate(work_item)
        temp_dir = fields.temp_dir.strip()
        temp_output = fields.temp_output.strip()
        target_path = fields.target_path.strip()
        if temp_dir:
            add_unique(required_workspace_dirs, temp_dir)
            add_unique(required_write_dirs, temp_dir)
            add_unique(gemini_cli_include_directories, temp_dir)
        if target_path:
            add_unique(required_read_files, target_path)
            target_dir = str(Path(target_path).parent)
            add_unique(required_read_dirs, target_dir)
            add_unique(required_workspace_dirs, target_dir)
            add_unique(gemini_cli_include_directories, target_dir)
        context_docs = _AgentWorkspaceContextDocsFields.model_validate(fields.context_docs)
        for value in context_docs.required_read_files:
            path = value.strip()
            if path:
                add_unique(required_read_files, path)
                doc_dir = str(Path(path).parent)
                add_unique(required_read_dirs, doc_dir)
                add_unique(required_workspace_dirs, doc_dir)
                add_unique(gemini_cli_include_directories, doc_dir)
        for value in context_docs.forbidden_discovery_roots:
            path = value.strip()
            add_unique(forbidden_discovery_roots, path)
        if temp_output:
            temp_outputs.append(temp_output)
    return {
        "schema": "medical-notes-workbench.agent-workspace-requirements.v1",
        "batch_size": len(work_items),
        "required_workspace_dirs": required_workspace_dirs,
        "required_write_dirs": required_write_dirs,
        "required_read_dirs": required_read_dirs,
        "required_read_files": required_read_files,
        "forbidden_discovery_roots": forbidden_discovery_roots,
        "gemini_cli_include_directories": gemini_cli_include_directories,
        "temp_outputs": temp_outputs,
        "temp_output": temp_outputs[0] if temp_outputs else "",
        "blocked_reason_if_missing": "agent_workspace_missing",
        "agent_instruction": (
            "Antes de rodar o especialista oficial, garanta que required_workspace_dirs estão no workspace. "
            "A invocação especialista oficial prepara o escopo necessário para "
            "gemini_cli_include_directories; não faça preflight manual nem bloqueie por workspace antes do harness oficial. "
            "Leia required_read_files e não faça descoberta ampla em forbidden_discovery_roots. "
            "Se algum temp_output oficial não for gravável, bloqueie como agent_workspace_missing; "
            "não use scratch, run_command ou conteúdo colado como workaround."
        ),
    }

def _phase_list(
    *,
    vocabulary_bootstrap: _VocabularyBootstrapReceiptFields,
    vocabulary_map_diagnosis: JsonObject,
    vocabulary_semantic_repair: JsonObject,
    sources_backfill: JsonObject,
    write_error_count: int,
    linker_diagnosis: JsonObject,
    linker_apply: JsonObject | None,
    related_notes_safety_cleanup: JsonObject,
    alias_projection_plan: JsonObject,
    taxonomy_phase: JsonObject,
) -> list[JsonObject]:
    map_fields = _FixWikiStatusActionFields.model_validate(vocabulary_map_diagnosis)
    semantic_repair_fields = _FixWikiStatusActionFields.model_validate(vocabulary_semantic_repair)
    alias_projection_fields = _FixWikiStatusActionFields.model_validate(alias_projection_plan)
    taxonomy_fields = _FixWikiStatusActionFields.model_validate(taxonomy_phase)
    linker_fields = _FixWikiStatusActionFields.model_validate(
        _consumed_link_artifact_source(linker_apply or linker_diagnosis)
    )
    sources_fields = _FixWikiStatusActionFields.model_validate(sources_backfill)
    related_cleanup_fields = _FixWikiStatusActionFields.model_validate(related_notes_safety_cleanup)
    bootstrap_status = vocabulary_bootstrap.status or "skipped"
    map_status = map_fields.status or "skipped"
    semantic_status = semantic_repair_fields.status or "skipped"
    alias_projection_status = alias_projection_fields.status or "skipped"
    taxonomy_status = taxonomy_fields.status or "planned"
    linker_status = linker_fields.status or "skipped"
    sources_status = sources_fields.status or "skipped"
    related_cleanup_status = related_cleanup_fields.status or "skipped"
    items = [
        {"phase": "preflight", "status": "completed"},
        {"phase": "inventory", "status": "completed"},
        {"phase": "structural_diagnosis", "status": "blocked" if write_error_count else "completed"},
        {
            "phase": "vocabulary_bootstrap",
            "status": bootstrap_status,
            "trigger": vocabulary_bootstrap.trigger,
        },
        {
            "phase": "vocabulary_map_diagnosis",
            "status": map_status,
            "blocked_reason": _vocabulary_blocked_reason(map_status),
        },
        {
            "phase": "vocabulary_semantic_repair",
            "status": semantic_status,
            "blocked_reason": semantic_repair_fields.blocked_reason,
        },
        {
            "phase": "alias_projection_plan",
            "status": alias_projection_status,
            "blocked_reason": alias_projection_fields.blocked_reason,
        },
        {"phase": "style", "status": "completed"},
        {
            "phase": "provenance_backfill",
            "status": sources_status,
            "written_count": sources_fields.written_count,
            "warning_count": sources_fields.warning_count,
        },
        {"phase": "hygiene", "status": "completed"},
        {
            "phase": "taxonomy",
            "status": taxonomy_status,
            "blocked_reason": taxonomy_fields.blocked_reason,
        },
        {"phase": "graph", "status": "planned"},
        {"phase": "duplicates", "status": "planned"},
        {"phase": "linker", "status": linker_status},
        {
            "phase": "related_notes_safety_cleanup",
            "status": related_cleanup_status,
            "removed_link_count": related_cleanup_fields.removed_link_count,
        },
        {"phase": "final_validation", "status": "planned"},
    ]
    return [_json_object(item) for item in items]


def _compact_report(report: JsonObject) -> JsonObject:
    if _json_field(report, "schema") != "medical-notes-workbench.fix-wiki-fsm-result.v1":
        raise ValueError("fix-wiki compact report requires the canonical FSM result payload")
    diagnostic = _json_object_or_empty(_json_field(report, "diagnostic_context", None))
    reports = _json_object_or_empty(_json_field(report, "reports", None))
    compact = _json_object({
        "schema": "medical-notes-workbench.fix-wiki-compact-report.v2",
        "workflow": _json_field(report, "workflow"),
        "run_id": _json_field(report, "run_id"),
        "state_machine_snapshot": _json_field(report, "state_machine_snapshot"),
        "progress_view_model": _json_field(report, "progress_view_model"),
        "decision": _json_field(report, "decision"),
        "human_decision_packet": _json_field(report, "human_decision_packet", None),
        "receipt": _compact_fix_wiki_receipt(_json_field(report, "receipt", None)),
        "reports": reports,
        "agent_directive": _json_field(report, "agent_directive"),
        "artifacts": _json_field(report, "artifacts"),
        "version_control_safety": _json_field(report, "version_control_safety"),
        "diagnostic_context": {
            "outcome_reason": _json_field(diagnostic, "outcome_reason") or _json_field(diagnostic, "reason"),
            "apply": _json_field(diagnostic, "apply"),
            "final_validation": _json_field(diagnostic, "final_validation"),
            "blocking_reasons": _json_field(diagnostic, "blocking_reasons", []),
            "related_notes_recovery_state": _json_field(diagnostic, "related_notes_recovery_state", None),
            "related_notes_sync": _compact_related_notes_sync(_json_field(diagnostic, "related_notes_sync", None)),
            "linker_artifact_compaction": _json_field(diagnostic, "linker_artifact_compaction", None),
            "version_control_mutation_summary": _json_field(diagnostic, "version_control_mutation_summary", None),
        },
        "error_context": _json_field(report, "error_context"),
    })
    return _attach_fix_wiki_primary_objective_summary(compact)


def _attach_fix_wiki_primary_objective_summary(report: JsonObject) -> JsonObject:
    """Place the validator summary under reports.details without creating root state."""

    objective = fix_wiki_primary_objective_summary(report)
    if objective is None:
        return report
    reports = _json_object(_json_field(report, "reports", {}))
    details = _json_object(_json_field(reports, "details", {}))
    details["primary_objective_summary"] = objective.to_payload()
    reports["details"] = details
    report["reports"] = reports
    return report


def fix_wiki_agent_stdout_report(report: JsonObject) -> JsonObject:
    """Return the short agent-facing stdout payload for fix-wiki.

    The detailed plan stays in ``compact-report.json``. Stdout must stay small
    enough for terminal, TUI and transcript surfaces to remain usable.
    """

    compact = _compact_report(report)
    fields = _FixWikiAgentStdoutReportFields.model_validate(compact)
    compact_report_path = fields.artifacts.compact_report_path if isinstance(fields.artifacts.compact_report_path, str) else ""
    stdout_report = _json_object(
        {
            "schema": "medical-notes-workbench.fix-wiki-agent-stdout-report.v1",
            "workflow": fields.workflow,
            "run_id": fields.run_id,
            "state_machine_snapshot": _agent_stdout_state_machine(fields.state_machine_snapshot),
            "progress_view_model": _agent_stdout_progress_view_model(fields.progress_view_model),
            "decision": fields.decision,
            "human_decision_packet": fields.human_decision_packet,
            "receipt": _agent_stdout_receipt_summary(fields.receipt),
            "reports": _agent_stdout_reports_summary(fields.reports, compact_report_path=compact_report_path),
            "agent_directive": _agent_stdout_agent_directive(fields.agent_directive),
            "artifacts": _agent_stdout_artifacts(fields.artifacts),
            "version_control_safety": _agent_stdout_version_control_safety_summary(fields.version_control_safety),
            "diagnostic_context": {
                "outcome_reason": fields.diagnostic_context.outcome_reason,
                "apply": _agent_stdout_apply_summary(fields.diagnostic_context.apply),
                "final_validation": fields.diagnostic_context.final_validation,
                "blocking_reasons": fields.diagnostic_context.blocking_reasons,
                "related_notes_recovery_state": fields.diagnostic_context.related_notes_recovery_state,
                "related_notes_sync": _agent_stdout_related_notes_summary(fields.diagnostic_context.related_notes_sync),
                "version_control_mutation_summary": _agent_stdout_version_control_summary(
                    fields.diagnostic_context.version_control_mutation_summary
                ),
            },
            "error_context": fields.error_context,
        }
    )
    return _attach_fix_wiki_primary_objective_summary(stdout_report)


def _full_report_artifact(report: JsonObject) -> JsonObject:
    if _verbose_full_report_enabled():
        # Verbose mode may expose the full FSM payload, but not the pre-FSM
        # orchestration report. Keeping only one source avoids debug artifacts
        # becoming a backdoor legacy API.
        return _json_object({
            "schema": "medical-notes-workbench.fix-wiki-full-report.v2",
            "mode": "verbose_developer",
            "workflow": report,
        })
    stdout_report = fix_wiki_agent_stdout_report(report)
    stdout_fields = _FixWikiAgentStdoutReportFields.model_validate(stdout_report)
    return _json_object(
        {
            "schema": "medical-notes-workbench.fix-wiki-full-report.v2",
            "mode": "lightweight_default",
            "workflow": _json_field(report, "workflow"),
            "run_id": _json_field(report, "run_id"),
            "summary": "Verbose internal payload omitted by default to keep user runtime storage small.",
            "compact_report_path": stdout_fields.artifacts.compact_report_path or "",
            "run_state_path": stdout_fields.artifacts.run_state_path or "",
            "stdout_report": stdout_report,
            "omitted_sections": [
                "verbose diagnostic history",
                "per-note style arrays",
                "per-link plan arrays",
                "specialist rewrite prompts",
            ],
            "verbose_override_env": "MEDNOTES_WRITE_VERBOSE_FULL_REPORT=1",
        }
    )


def _verbose_full_report_enabled() -> bool:
    return os.environ.get("MEDNOTES_WRITE_VERBOSE_FULL_REPORT", "").strip().lower() in {"1", "true", "yes", "on"}


def _agent_stdout_state_machine(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return _contract_payload(_AgentStdoutStateMachineSummaryFields.model_validate(value))


def _agent_stdout_progress_view_model(value: object) -> object:
    if not isinstance(value, dict):
        return value
    fields = _AgentStdoutProgressViewFields.model_validate(value)
    return _contract_payload(fields, exclude=frozenset({"counts"})) | {"counts": _contract_payload(fields.counts)}


def _agent_stdout_apply_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return _contract_payload(_AgentStdoutApplyFields.model_validate(value))


def _agent_stdout_receipt_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return _contract_payload(_AgentStdoutReceiptFields.model_validate(value))


def _agent_stdout_reports_summary(reports: JsonObject, *, compact_report_path: str) -> JsonObject:
    _ = compact_report_path
    fields = _AgentStdoutReportsFields.model_validate(reports)
    summary = _json_object({"summary": fields.summary})
    public_report_summary = _agent_stdout_public_report_summary(fields.public_report)
    details_payload: JsonObject = {}
    if public_report_summary:
        summary.update(public_report=public_report_summary)
    if "details" in fields.model_fields_set and "primary_objective_summary" in fields.details.model_fields_set:
        details_payload = {"details": {"primary_objective_summary": fields.details.primary_objective_summary}}
        summary.update(details_payload)
    return _json_object(summary)


def _agent_stdout_agent_directive(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    fields = _AgentStdoutDirectiveFields.model_validate(value)
    control_payload = _contract_payload(fields.control, exclude=frozenset({"effects"}))
    return _json_object(
        {
            "schema": fields.schema_id,
            "workflow": fields.workflow,
            "run_id": fields.run_id,
            "control": control_payload | {"effects": _agent_stdout_agent_effects(fields.control.effects)},
            "summary": fields.summary,
            "instructions": [item for item in fields.instructions if isinstance(item, str)][:32],
        }
    )


def _agent_stdout_agent_effects(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    effects: list[JsonObject] = []
    for effect in value:
        if not isinstance(effect, dict):
            continue
        fields = _AgentStdoutEffectFields.model_validate(effect)
        effects.append(
            _json_object(
                _contract_payload(fields, exclude=frozenset({"payload"}))
                | {"payload": _agent_stdout_agent_effect_payload(fields.payload)}
            )
        )
    return effects


def _agent_stdout_agent_effect_payload(value: object) -> JsonObject:
    """Keep executable effect data inline without duplicating full diagnostics."""

    if not isinstance(value, dict):
        return {}
    fields = _AgentStdoutEffectPayloadFields.model_validate(value)
    compact = _contract_payload(fields, exclude=frozenset({"current_batch_items"}))
    current_batch_items = fields.current_batch_items
    if current_batch_items:
        if "current_batch_item_count" not in compact:
            compact["current_batch_item_count"] = len(current_batch_items)
        compact["current_batch_items"] = [
            _agent_stdout_work_item_summary(item)
            for item in current_batch_items
            if isinstance(item, dict)
        ]
    return _json_object(compact)


def _agent_stdout_inline_contract_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    omitted_prefixes = (
        "required_workspace_dirs:",
        "gemini_cli_include_directories:",
    )
    must_keep_fragments = (
        ("segundo plano", "rotulo tecnico"),
        ("resposta final deve cobrir objetivo primario",),
        ("inclua esta contagem na resposta final",),
        ("inclua tambem na resposta final",),
    )
    priority_fragments = (
        "resposta final deve cobrir objetivo primario",
        "inclua esta contagem na resposta final",
        "inclua tambem na resposta final",
        "CONTINUACAO AUTOMATICA PRONTA",
        "call_specialist_model",
        "AGY:",
        "nao use invoke_agent",
        "nao fabrique",
        "nao abra cli.py",
        "nao renderize target_path",
        "progress_view_model.status=waiting_agent",
        "se progress_view_model.status=waiting_agent",
        "nao use sucesso",
        "não use sucesso",
        "concluido",
        "concluído",
        "segundo plano",
        "rotulo tecnico",
        "list_permissions",
        "qualquer comando/tool call falho",
        "quality",
        "qualidade",
    )
    priority_lines: list[str] = []
    fallback_lines: list[str] = []
    must_keep_lines: list[str] = []
    lines: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        if item.startswith(omitted_prefixes):
            continue
        if any(all(fragment in item for fragment in fragments) for fragments in must_keep_fragments):
            must_keep_lines.append(item)
        elif any(fragment in item for fragment in priority_fragments):
            priority_lines.append(item)
        else:
            fallback_lines.append(item)
    for item in [*must_keep_lines, *priority_lines, *fallback_lines]:
        if item in lines:
            continue
        lines.append(item)
        if len(lines) >= 18:
            break
    return lines


def _agent_stdout_artifacts(artifacts: JsonObject | _AgentStdoutArtifactFields) -> JsonObject:
    fields = artifacts if isinstance(artifacts, _AgentStdoutArtifactFields) else _AgentStdoutArtifactFields.model_validate(artifacts)
    return _contract_payload(fields)


def _agent_stdout_work_item_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    fields = _StyleRewriteWorkItemFields.model_validate(value)
    payload = _contract_payload(fields, exclude=frozenset({"context_docs", "rewrite_prompt"}))
    if (rewrite_prompt := _agent_stdout_bounded_text(fields.rewrite_prompt)) is not None:
        payload["rewrite_prompt"] = rewrite_prompt
    return _json_object(payload)


def _agent_stdout_bounded_text(value: object, *, max_chars: int = 1200) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _sanitize_public_report_line(line: str) -> str:
    return line.replace("style rewrite", "reescrita especializada").replace(
        "style_rewrite",
        "reescrita especializada",
    )


def _agent_stdout_public_report_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    fields = _AgentStdoutPublicReportFields.model_validate(value)
    summary = _contract_payload(fields, exclude=frozenset({"lines"}))
    if "lines" in fields.model_fields_set and len(fields.lines) <= 12:
        line_chars = sum(len(line) for line in fields.lines if isinstance(line, str))
        if line_chars <= 3_000:
            summary["lines"] = [_sanitize_public_report_line(line) if isinstance(line, str) else line for line in fields.lines]
    return _json_object(summary)


def _public_related_notes_state(
    sync_result: LinkRelatedSyncResult,
    convergence: _RelatedNotesConvergenceProjection,
) -> str:
    recovery_state = sync_result.related_notes_recovery_state
    blocked_reason = sync_result.blocked_reason or recovery_state.blocked_reason
    status = sync_result.status
    convergence_status = convergence.status
    if recovery_state.status == "waiting_for_retry" and recovery_state.resume_supported:
        return "waiting_external"
    if blocked_reason:
        return "blocked"
    if status in {"completed", "success"} and convergence_status in {"", "stable", "converged", "completed", "success"}:
        return "updated"
    if status in {"completed_with_warnings", "warning", "warnings"}:
        return "updated_with_warnings"
    if status in {"skipped", "not_needed"}:
        return "not_needed"
    if status:
        return "pending"
    return "unknown"


def _public_related_notes_summary(public_state: str, applied_note_count: object) -> str:
    if public_state == "updated":
        return f"Notas Relacionadas atualizadas em {applied_note_count} nota(s)."
    if public_state == "updated_with_warnings":
        return f"Notas Relacionadas atualizadas com avisos em {applied_note_count} nota(s)."
    if public_state == "blocked":
        return "Notas Relacionadas ainda precisam de retomada pela rota oficial."
    if public_state == "not_needed":
        return "Notas Relacionadas não exigiram atualização nesta etapa."
    return "Estado de Notas Relacionadas ainda não está resolvido."


def _public_related_notes_sync_next_action(sync_result: LinkRelatedSyncResult) -> str:
    raw_next_action = sync_result.next_action.strip()
    if raw_next_action and not _looks_like_internal_cli_action(raw_next_action):
        return raw_next_action
    recovery_state = sync_result.related_notes_recovery_state
    blocked_reason = (sync_result.blocked_reason or recovery_state.blocked_reason).strip()
    if blocked_reason in {
        "related_notes_hash_mismatch",
        "related_notes_export_stale",
        "related_notes_export_still_stale",
        "related_notes_headless_embedding_failed",
    }:
        return "Atualizar o export do Related Notes pela rota oficial e repetir a correção da Wiki."
    if blocked_reason == "related_notes_headless_time_budget_exhausted":
        return "Aguardar a janela de atualização permitir a retomada e repetir a atualização das Notas Relacionadas pela rota oficial."
    if blocked_reason == "related_notes_headless_quota_exhausted":
        return "Aguardar a quota de embeddings do Gemini voltar e repetir a atualização das Notas Relacionadas pela rota oficial."
    if blocked_reason in {"obsidian_cli_unavailable", "obsidian_not_ready", "obsidian_cli_timeout"}:
        return "Abrir/configurar o Obsidian CLI para atualizar o export das Notas Relacionadas e repetir a correção da Wiki."
    if raw_next_action:
        return "Resolver o bloqueio das Notas Relacionadas e repetir a correção da Wiki pela rota oficial."
    return ""


def _agent_stdout_related_notes_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    if "public_state" in value:
        public = _RelatedNotesPublicSummaryFields.model_validate(value)
        return _json_object(
            public.model_dump(
                mode="json",
                by_alias=True,
                exclude_defaults=True,
                exclude_none=True,
            )
        )
    sync_result = LinkRelatedSyncResult.from_payload(value)
    report_fields = _RelatedNotesAgentReportFields.model_validate(value)
    public_state = _public_related_notes_state(sync_result, report_fields.convergence)
    return _json_object(
        {
            "schema": report_fields.schema_id,
            "status": sync_result.status,
            "public_state": public_state,
            "public_summary": _public_related_notes_summary(public_state, sync_result.applied_note_count),
            "applied_note_count": sync_result.applied_note_count,
            "update_count": report_fields.update_count,
            "hash_warning_count": report_fields.hash_warning_count,
            "convergence": {
                "public_state": _public_related_notes_state(sync_result, report_fields.convergence),
                "pass_count": report_fields.convergence.pass_count,
                "applied_note_count": report_fields.convergence.applied_note_count,
            },
        }
    )


def _agent_stdout_version_control_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return _contract_payload(_AgentStdoutVersionControlSummaryFields.model_validate(value))


def _agent_stdout_version_control_safety_summary(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return _contract_payload(_AgentStdoutVersionControlSafetyFields.model_validate(value), exclude=frozenset({"changed_file_count"}))


def _compact_fix_wiki_receipt(value: object) -> object:
    if not isinstance(value, dict):
        return value
    fields = _FixWikiCompactReceiptFields.model_validate(value)
    summary = _contract_payload(
        fields,
        exclude=frozenset({"changed_files", "artifacts", "phase_outcomes", "phase_receipts"}),
    )
    if "changed_files" in fields.model_fields_set:
        summary["changed_file_count"] = len(fields.changed_files)
    if "artifacts" in fields.model_fields_set:
        summary["artifact_count"] = len(fields.artifacts)
    if "phase_outcomes" in fields.model_fields_set:
        summary["phase_outcome_count"] = len(fields.phase_outcomes)
    if "phase_receipts" in fields.model_fields_set:
        summary["phase_receipt_count"] = len(fields.phase_receipts)
    return _json_object(summary)


def _compact_related_notes_sync(value: object) -> object:
    if not isinstance(value, dict):
        return value
    fields = _RelatedNotesCompactSyncFields.model_validate(value)
    summary = _json_object(_agent_stdout_related_notes_summary(value))
    summary.update(
        _contract_payload(
            fields,
            exclude=frozenset({"updates", "skipped_edges", "hash_warnings", "related_notes_export_recovery"}),
        )
    )
    if "updates" in fields.model_fields_set:
        summary["update_count"] = len(fields.updates)
    if "skipped_edges" in fields.model_fields_set:
        summary["skipped_edge_count"] = len(fields.skipped_edges)
    if "hash_warnings" in fields.model_fields_set:
        summary["hash_warning_count"] = len(fields.hash_warnings)
    if isinstance(fields.related_notes_export_recovery, dict):
        summary["related_notes_export_recovery"] = _compact_related_notes_recovery(fields.related_notes_export_recovery)
    return _json_object(summary)


def _compact_related_notes_recovery(value: JsonObject) -> JsonObject:
    """Preserve recovery evidence without making it a second state source.

    The FSM state lives in ``state_machine_snapshot`` and ``progress_view_model``.
    These fields are private artifact evidence used by gates to prove whether a
    Related Notes export was stale, recovered, retried, or blocked.
    """

    fields = _RelatedNotesRecoveryPayloadFields.model_validate(value)
    typed_state = RelatedNotesRecoveryState.from_payload(fields.related_notes_recovery_state)
    operation_payload = typed_state.operation_payload
    summary = _contract_payload(fields, exclude=frozenset({"related_notes_recovery_state", "headless_export"}))
    for key in (
        "stale_notes",
        "stale_note_count",
        "api_calls",
        "api_failures",
        "automatic_recovery_unavailable_reason",
        "export_relocation",
    ):
        if key not in summary and key in operation_payload:
            summary[key] = operation_payload[key]
    if typed_state:
        summary["related_notes_recovery_state"] = _related_notes_recovery_state_payload(typed_state)
    return _json_object(summary)


def _run_state(report: JsonObject) -> JsonObject:
    if _json_field(report, "schema") != "medical-notes-workbench.fix-wiki-fsm-result.v1":
        raise ValueError("fix-wiki run state requires the canonical FSM result payload")
    diagnostic = _json_object_or_empty(_json_field(report, "diagnostic_context", None))
    return {
        "schema": "medical-notes-workbench.fix-wiki-run-state.v2",
        "workflow": _json_field(report, "workflow"),
        "run_id": _json_field(report, "run_id"),
        "state_machine_snapshot": _json_field(report, "state_machine_snapshot"),
        "progress_view_model": _json_field(report, "progress_view_model"),
        "decision": _json_field(report, "decision"),
        "human_decision_packet": _json_field(report, "human_decision_packet", None),
        "receipt": _json_field(report, "receipt"),
        "artifacts": _json_field(report, "artifacts"),
        "version_control_safety": _json_field(report, "version_control_safety"),
        "diagnostic_context": {
            "outcome_reason": _json_field(diagnostic, "outcome_reason") or _json_field(diagnostic, "reason"),
            "related_notes_recovery_state": _json_field(diagnostic, "related_notes_recovery_state", None),
        },
        "error_context": _json_field(report, "error_context"),
    }
