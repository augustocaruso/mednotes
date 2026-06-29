from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject


class LinkPolicy(StrEnum):
    DIRECT = "direct"
    REQUIRES_CONTEXT = "requires_context"
    NEVER = "never"


class CuratorAlias(ContractModel):
    text: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    link_policy: LinkPolicy
    visible_in_yaml: bool = True
    intrinsically_ambiguous: bool = False


class CuratorPrimaryMeaning(ContractModel):
    id: str | None = None
    label: str = Field(min_length=1)
    semantic_type: str = Field(min_length=1)
    atomic_status: str = Field(min_length=1)
    description: str | None = None


class CuratorAgentMetrics(ContractModel):
    token_accounting: str = Field(min_length=1)
    turns_used: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    retries: int = Field(ge=0)


class NoteSemanticIngestionOutput(ContractModel):
    schema_: Literal["medical-notes-workbench.note-semantic-ingestion.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    workflow: Literal["/mednotes:link"]
    phase: Literal["vocabulary_curation"]
    agent: Literal["med-link-graph-curator"]
    source_workflow: str = Field(min_length=1)
    note_path: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    primary_meaning: CuratorPrimaryMeaning
    aliases: list[CuratorAlias] = Field(min_length=1)
    duplicate_candidates: list[JsonObject] = Field(default_factory=list)
    split_warning: JsonObject | None = None
    deferred_work_items: list[JsonObject] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    agent_metrics: CuratorAgentMetrics | None = None


class CuratorWorkItem(ContractModel):
    schema_: str = Field(alias="schema", serialization_alias="schema", min_length=1)
    app: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    source_workflow: str = Field(min_length=1)
    db_path: str = Field(min_length=1)
    note_path: str = Field(min_length=1)
    note_path_exists: bool
    path_case_check: JsonObject = Field(default_factory=dict)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    queue_flags: list[str] = Field(default_factory=list)
    output_path: str = Field(min_length=1)
    prompt_identity: JsonObject = Field(default_factory=dict)
    difficulty_route: JsonObject = Field(default_factory=dict)
    quality_rubric: JsonObject = Field(default_factory=dict)
    output_contract: JsonObject = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    retry_scope: str = Field(min_length=1)
    max_turns_policy: JsonObject = Field(default_factory=dict)
    expected_output_schema: str | JsonObject
    error_context: JsonObject = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)


class CuratorBatchPlan(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-curator-batch-plan.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["vocabulary_curation"]
    batch_id: str = Field(min_length=1)
    status: Literal["ready", "skipped"]
    skipped_reason: str = ""
    db_path: str = Field(min_length=1)
    prompt_identity: JsonObject = Field(default_factory=dict)
    prompt_eval_report_path: str = Field(min_length=1)
    item_count: int = Field(ge=0)
    work_items: list[CuratorWorkItem]
    parallel_safe: bool = False
    max_concurrency: int = Field(ge=0)
    rules: list[str] = Field(default_factory=list)
    serial_after: list[str] = Field(default_factory=list)
    canonical_parent_commands: list[str] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)

    @model_validator(mode="after")
    def ready_requires_items(self) -> CuratorBatchPlan:
        if self.item_count != len(self.work_items):
            raise ValueError("item_count must match work_items length")
        if self.status == "ready" and not self.work_items:
            raise ValueError("ready curator batch plans require at least one item")
        return self


class CuratorManifestItem(ContractModel):
    work_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    sha256: str = ""

    @field_validator("sha256")
    @classmethod
    def sha256_must_be_hash_when_present(cls, value: str) -> str:
        if value and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("sha256 must be sha256:<64 lowercase hex chars>")
        return value

    @property
    def path(self) -> Path:
        return Path(self.output_path)


class CuratorManifest(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-curator-batch-output-manifest.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    batch_id: str = ""
    items: list[CuratorManifestItem]

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class CuratorPromptEvalIssue(ContractModel):
    code: str = Field(min_length=1)
    severity: str = ""
    rubric_key: str = ""
    message: str = Field(min_length=1)


class CuratorPromptEvalReport(ContractModel):
    schema_: Literal["medical-notes-workbench.curator-prompt-eval.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["vocabulary_curation"] = "vocabulary_curation"
    status: str = Field(min_length=1)
    prompt_identity: JsonObject = Field(default_factory=dict)
    input_fingerprints: JsonObject = Field(default_factory=dict)
    prompt_eval_context: JsonObject = Field(default_factory=dict)
    aggregate: JsonObject
    items: list[JsonObject] = Field(default_factory=list)
    aggregate_issues: list[CuratorPromptEvalIssue] = Field(default_factory=list)
    next_action: str = ""

    @property
    def blocks_apply(self) -> bool:
        return self.status not in {"pass", "skipped"}


class CuratorIgnoredOutputNotice(ContractModel):
    work_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    reason: Literal["not_in_manifest", "hash_mismatch", "prompt_eval_blocked", "stale_note_hash", "shape_invalid"]
    next_action: str = Field(min_length=1)


class CuratorApplyItemReceipt(ContractModel):
    work_id: str = Field(min_length=1)
    output_path: str = ""
    status: Literal["applied", "blocked", "ignored", "idempotent"]
    meaning_id: str | None = None
    blocked_reason: str | None = None
    note_path: str = ""
    content_hash: str = ""
    receipt: JsonObject = Field(default_factory=dict)
    agent_notice: str = ""
    next_action: str = ""
    ignored_notice: CuratorIgnoredOutputNotice | None = None

    @model_validator(mode="after")
    def require_reason_for_non_applied_item(self) -> CuratorApplyItemReceipt:
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked curator items require blocked_reason")
        if self.status == "ignored" and self.ignored_notice is None:
            raise ValueError("ignored curator items require ignored_notice")
        return self


class CuratorErrorContext(ContractModel):
    phase: str = Field(min_length=1)
    blocked_reason: str = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    affected_artifact: str = Field(min_length=1)
    error_summary: str = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    retry_scope: str = Field(min_length=1)


class CuratorApplyReceipt(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-curator-batch-receipt.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["vocabulary_curation"]
    status: Literal["completed", "completed_with_blockers", "blocked"]
    batch_id: str = Field(min_length=1)
    db_path: str = ""
    prompt_eval: JsonObject = Field(default_factory=dict)
    plan_item_count: int = Field(ge=0)
    manifest_item_count: int = Field(ge=0)
    applied_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    agent_events: list[JsonObject] = Field(default_factory=list)
    agent_output_ignored_notices: list[CuratorIgnoredOutputNotice] = Field(default_factory=list)
    items: list[CuratorApplyItemReceipt]
    blocked_reason: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    agent_notice: str = ""
    error_context: CuratorErrorContext | None = None
    next_action: str = ""

    @field_validator("next_action")
    @classmethod
    def next_action_required_when_present(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def blocked_receipt_requires_context(self) -> CuratorApplyReceipt:
        if self.status == "blocked" and self.error_context is None:
            raise ValueError("blocked curator apply receipts require error_context")
        return self
