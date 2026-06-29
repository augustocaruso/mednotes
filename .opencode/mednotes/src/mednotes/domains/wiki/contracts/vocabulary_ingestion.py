"""Typed semantic-ingestion contracts for the linker vocabulary DB.

The med-link-graph-curator writes JSON, but that JSON becomes operational only
after this boundary validates workflow identity, note hash, aliases and deferred
atomicity work. The vocabulary adapter may serialize payloads for audit, but it
must make blocker/receipt decisions from these models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, StrictStr

from mednotes.domains.wiki.contracts.curator import CuratorAgentMetrics
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

INGESTION_SCHEMA = "medical-notes-workbench.note-semantic-ingestion.v1"
INGESTION_RECEIPT_SCHEMA = "medical-notes-workbench.note-semantic-ingestion-apply-receipt.v1"


def _compact_payload(payload: JsonObject) -> JsonObject:
    """Drop absent optional fields while preserving explicit false/zero values."""

    return JsonObjectAdapter.validate_python(
        {key: value for key, value in payload.items() if value not in ("", None, [])}
    )


class SemanticPrimaryMeaning(ContractModel):
    id: str = ""
    label: str = Field(min_length=1)
    semantic_type: str = "medical_concept"
    atomic_status: str = "atomic"


class VocabularyBootstrapPlan(ContractModel):
    """Typed link-runtime bootstrap receipt; invalid receipts block before effects run.

    The linker observes the same payload produced by the vocabulary bootstrap
    capability. Keep every operational/audit field explicit here so the link FSM
    never needs to inspect an untyped bootstrap dictionary.
    """

    schema_id: Literal["medical-notes-workbench.vocabulary-bootstrap-receipt.v1"] | None = Field(
        default=None,
        alias="schema",
        serialization_alias="schema",
    )
    generated_at: StrictStr = ""
    status: Literal[
        "planned",
        "skipped",
        "existing",
        "completed",
        "queued_semantic_ingestion",
        "blocked",
    ]
    db_path: StrictStr = Field(min_length=1)
    trigger: str = ""
    automatic: bool = False
    wiki_dir: StrictStr = ""
    plan_path: StrictStr = ""
    queue_path: StrictStr = ""
    receipt_path: StrictStr = ""
    note_count: int = Field(default=0, ge=0, strict=True)
    queued_note_count: int = Field(default=0, ge=0, strict=True)
    changed_files: list[StrictStr] = Field(default_factory=list)
    backup_paths: list[StrictStr] = Field(default_factory=list)
    dry_run: bool = False
    note_count_deferred: bool = False
    deferred_reason: str = ""


class SemanticAlias(ContractModel):
    text: str = Field(min_length=1)
    kind: str = "alias"
    link_policy: str = "requires_context"
    visible_in_yaml: bool = True
    intrinsically_ambiguous: bool = False
    ambiguous_with: list[str] = Field(default_factory=list)
    source: str = ""


class AtomicityConcept(ContractModel):
    name: str = ""
    semantic_type: str = Field(default="", alias="type")
    body_spans: list[str] = Field(default_factory=list)


class AtomicityChildNoteEstimate(ContractModel):
    title: str = ""
    body_char_count: int | None = Field(default=None, ge=0)
    estimated_body_char_count: int | None = Field(default=None, ge=0)
    char_count: int | None = Field(default=None, ge=0)

    def first_body_count(self) -> int | None:
        for value in (self.body_char_count, self.estimated_body_char_count, self.char_count):
            if value is not None:
                return value
        return None


class AtomicitySemanticSignal(ContractModel):
    score: float = Field(default=0.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    concepts: list[AtomicityConcept] = Field(default_factory=list)
    relationship_score: float = Field(default=0.0, ge=0, le=1)
    fragment_risk: str = ""
    child_note_estimates: list[AtomicityChildNoteEstimate] = Field(default_factory=list)

    def to_payload(self) -> JsonObject:
        return _compact_payload(super().to_payload())


class SemanticDeferredWorkItem(ContractModel):
    work_id: str = ""
    reason: str = Field(min_length=1)
    note_path: Path | None = None
    content_hash: str = ""
    semantic_signal: AtomicitySemanticSignal | None = None
    source_agent: str = "med-link-graph-curator"
    assigned_agent: str = "med-knowledge-architect"
    status: str = "pending"

    def effective_work_id(self, *, fallback_stem: str) -> str:
        return self.work_id or f"link-graph-work:{fallback_stem}"

    def effective_note_path(self, *, fallback: Path) -> Path:
        return self.note_path or fallback

    def effective_content_hash(self, *, fallback: str) -> str:
        return self.content_hash or fallback

    def to_payload(self) -> JsonObject:
        return _compact_payload(super().to_payload())


class SemanticIngestionItem(ContractModel):
    schema_id: Literal["medical-notes-workbench.note-semantic-ingestion.v1"] = Field(alias="schema")
    workflow: Literal["/mednotes:link"]
    phase: Literal["vocabulary_curation"]
    agent: Literal["med-link-graph-curator"]
    source_workflow: Literal["/mednotes:link"]
    note_path: Path
    content_hash: str = Field(min_length=1)
    primary_meaning: SemanticPrimaryMeaning
    aliases: list[SemanticAlias]
    duplicate_candidates: list[JsonObject] = Field(default_factory=list)
    split_warning: JsonObject | str | None = None
    deferred_work_items: list[SemanticDeferredWorkItem] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    agent_metrics: CuratorAgentMetrics | None = None
    source: str = "curator"

    def to_payload(self) -> JsonObject:
        return _compact_payload(super().to_payload())


class AtomicityBodySizeStats(ContractModel):
    sample_count: int = Field(default=0, ge=0)
    mean_body_chars: float = Field(default=0.0, ge=0)
    stddev_body_chars: float = Field(default=0.0, ge=0)
    long_note_threshold_chars: float = Field(default=0.0, ge=0)
    current_body_chars: int = Field(default=0, ge=0)
    current_above_one_stddev: bool = False
    min_child_body_chars: int = Field(default=0, ge=0)


class AtomicityDeferredWorkEvaluation(ContractModel):
    status: Literal["ready", "blocked"]
    blocked_reason: str = ""
    decision: str = "human_decision_required"
    db_status: str = "blocked"
    source: str = ""
    semantic_score: float = Field(default=0.0, ge=0, le=1)
    semantic_threshold: float = Field(default=0.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    concept_count: int = Field(default=0, ge=0)
    relationship_score: float = Field(default=0.0, ge=0, le=1)
    fragmentation_gate: JsonObject | None = None
    body_size_gate: JsonObject | None = None
    message: str = ""

    def to_payload(self) -> JsonObject:
        return _compact_payload(super().to_payload())


class AtomicityDeferredWorkDecision(ContractModel):
    work_id: str
    decision: str
    status: str


class AtomicityDeferredWorkPreflight(ContractModel):
    status: Literal["ready", "blocked"]
    evaluations: dict[str, AtomicityDeferredWorkEvaluation] = Field(default_factory=dict)
    decisions: list[AtomicityDeferredWorkDecision] = Field(default_factory=list)
    blocked_reason: str = ""
    note_path: str = ""
    content_hash: str = ""
    work_id: str = ""
    atomicity_evaluation: AtomicityDeferredWorkEvaluation | None = None
    next_action: str = ""
    error_context: JsonObject | None = None

    def to_payload(self) -> JsonObject:
        payload = super().to_payload()
        if self.atomicity_evaluation is None:
            payload.pop("atomicity_evaluation", None)
        if self.error_context is None:
            payload.pop("error_context", None)
        return _compact_payload(payload)


class SemanticIngestionIdentity(ContractModel):
    """Best-effort identity for marking queue rows when full validation fails."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    note_path: Path | None = None
    content_hash: str = ""
