from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from mednotes.kernel.agent_directive import AgentDirective
from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.effects import WorkflowEffectResult
from mednotes.kernel.guardrails import OperationalErrorContext


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


STYLE_REWRITE_LINKER_BLOCKER_NEXT_ACTION = (
    "Resolver pendências do linker/grafo pela rota oficial antes de considerar a Wiki concluída."
)


class StyleRewriteLinkerEvidence(ContractModel):
    """Typed linker evidence carried by style-rewrite without becoming root state."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: str | None = Field(default=None, alias="schema")
    phase: str = ""
    status: str = ""
    trigger_context_path: str = ""
    diagnosis_path: str = ""
    receipt_path: str = ""
    diagnosis_status: str = ""
    diagnosis_blocked_reason: str = ""
    blocker_count: int = Field(default=0, ge=0, strict=True)
    linker_applied: bool = Field(default=False, strict=True)
    linker_skipped_reason: str = ""
    apply_status: str = ""
    apply_blocked_reason: str = ""
    files_changed: int = Field(default=0, ge=0, strict=True)
    changed_files: list[str] = Field(default_factory=list)
    workflow_effect_results: list[WorkflowEffectResult] = Field(default_factory=list)

    @property
    def blocker_reason(self) -> str:
        return (
            self.linker_skipped_reason.strip()
            or self.apply_blocked_reason.strip()
            or self.diagnosis_blocked_reason.strip()
        )

    @property
    def affected_artifact(self) -> str:
        return self.diagnosis_path.strip() or self.receipt_path.strip() or "workflow_linker"

    def to_payload(self) -> JsonObject:
        return super().to_payload()


class _StyleRewriteApplyReceiptSource(ContractModel):
    """Read-only input view for deriving style-rewrite link blockers."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    phase: str = "style_rewrite"
    human_decision_required: bool = Field(default=False, strict=True)
    linker: StyleRewriteLinkerEvidence | None = None


def _style_rewrite_linker_blocker_payload(value: object) -> object:
    """Project nested typed linker evidence into the style-rewrite receipt contract."""

    if not isinstance(value, dict):
        return value
    source = _StyleRewriteApplyReceiptSource.model_validate(value)
    if source.linker is None or source.linker.linker_applied or not source.linker.blocker_reason:
        return value
    next_action = STYLE_REWRITE_LINKER_BLOCKER_NEXT_ACTION
    error_context = OperationalErrorContext(
        phase=source.phase.strip() or "style_rewrite",
        blocked_reason=source.linker.blocker_reason,
        root_cause=source.linker.blocker_reason,
        affected_artifact=source.linker.affected_artifact,
        error_summary="O linker/grafo ficou bloqueado após aplicar a reescrita.",
        suggested_fix=next_action,
        next_action=next_action,
        retry_scope="run_mednotes_link_after_style_rewrite",
        missing_inputs=[],
        human_decision_required=source.human_decision_required,
    )
    return dict(value) | {
        "status": "completed_with_link_blockers",
        "blocked_reason": source.linker.blocker_reason,
        "next_action": next_action,
        "linker_skipped_reason": source.linker.blocker_reason,
        "error_context": error_context,
    }


class FixWikiStyleReport(ContractModel):
    """One note-style report after deterministic preview/apply normalization.

    The style capability may gather raw note metadata, but fix-wiki only consumes
    this closed model. That keeps style repair evidence from becoming a loose
    dict boundary that can steer the workflow.
    """

    schema_: Literal["medical-notes-workbench.wiki-note-style-report.v1"] = Field(
        default="medical-notes-workbench.wiki-note-style-report.v1",
        alias="schema",
        serialization_alias="schema",
    )
    path: str | None = None
    title: str = ""
    ok: bool = Field(default=True, strict=True)
    errors: list[JsonObject] = Field(default_factory=list)
    warnings: list[JsonObject] = Field(default_factory=list)
    fixes_applied: list[str] = Field(default_factory=list)
    requires_llm_rewrite: bool = Field(default=False, strict=True)
    rewrite_prompt: str | None = None
    frontmatter_present: bool = Field(default=False, strict=True)
    skipped: bool = Field(default=False, strict=True)
    skip_reason: str = ""
    changed: bool = Field(default=False, strict=True)
    would_write: bool = Field(default=False, strict=True)
    wrote: bool = Field(default=False, strict=True)
    backup: str | None = None
    write_error: str | None = None
    root_note_invalid: bool = Field(default=False, strict=True)
    root_note_invalid_original_errors: list[JsonObject] = Field(default_factory=list)
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""

    def to_audit_input(self) -> JsonObject:
        return self.to_payload()


class FixWikiStyleResult(ContractModel):
    """Typed result of deterministic fix-wiki style preview/apply."""

    schema_: Literal["medical-notes-workbench.wiki-note-style-fix.v1"] = Field(
        default="medical-notes-workbench.wiki-note-style-fix.v1",
        alias="schema",
        serialization_alias="schema",
    )
    wiki_dir: str = ""
    dry_run: bool = Field(default=True, strict=True)
    apply: bool = Field(default=False, strict=True)
    backup: bool = Field(default=False, strict=True)
    file_count: int = Field(default=0, ge=0, strict=True)
    changed_count: int = Field(default=0, ge=0, strict=True)
    written_count: int = Field(default=0, ge=0, strict=True)
    error_count: int = Field(default=0, ge=0, strict=True)
    warning_count: int = Field(default=0, ge=0, strict=True)
    write_error_count: int = Field(default=0, ge=0, strict=True)
    write_errors: list[JsonObject] = Field(default_factory=list)
    backup_paths: list[str] = Field(default_factory=list)
    reports: list[FixWikiStyleReport] = Field(default_factory=list)

    def to_audit_reports(self) -> list[JsonObject]:
        return [report.to_audit_input() for report in self.reports]


class StyleRewriteManifestItem(ContractModel):
    work_id: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    target_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_attestation_path: str = Field(min_length=1)
    output_attestation_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_receipt_path: str = ""
    output_receipt_sha256: str = ""
    agent: Literal["med-knowledge-architect"]
    model_policy: str = Field(default="medical_specialist_authoring.v1", min_length=1)
    required_model_tier: str = Field(min_length=1)

    @property
    def path(self) -> Path:
        return Path(self.output_path)


class StyleRewriteOutputReceipt(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-output.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed"]
    work_id: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    target_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_path: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    agent: Literal["med-knowledge-architect"]
    model_policy: str = Field(default="medical_specialist_authoring.v1", min_length=1)
    required_model_tier: str = Field(min_length=1)
    actual_model: str = ""
    provider: str = ""


class StyleRewriteOutputAttestation(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-output-attestation.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed"]
    attestation_kind: Literal["workbench_hmac_sha256.v1"]
    work_id: str = Field(min_length=1)
    source_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_path: str = Field(min_length=1)
    target_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_path: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    agent: Literal["med-knowledge-architect"]
    model_policy: str = Field(default="medical_specialist_authoring.v1", min_length=1)
    required_model_tier: str = Field(min_length=1)
    actual_model: str = ""
    provider: str = ""
    model_claim_source: Literal[
        "not_reported",
        "parent_cli_argument_unverified",
        "specialist_task_run_receipt",
    ] = "not_reported"
    model_verification_status: Literal["unverified_by_workbench", "verified_by_workbench"] = "unverified_by_workbench"
    nonce: str = Field(min_length=16)
    issued_at: str = Field(min_length=1)
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")


class StyleRewriteOutputFinalization(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-output-finalization.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = ""
    work_id: str = Field(min_length=1)
    target_path: str = ""
    output_path: str = ""
    output_sha256: str = ""
    output_receipt_path: str = ""
    output_receipt_sha256: str = ""
    output_attestation_path: str = ""
    output_attestation_sha256: str = ""
    source_plan_hash: str = ""
    actual_model: str = ""
    provider: str = ""
    model_claim_source: Literal[
        "not_reported",
        "parent_cli_argument_unverified",
        "specialist_task_run_receipt",
    ] = "not_reported"
    model_verification_status: Literal["unverified_by_workbench", "verified_by_workbench"] = "unverified_by_workbench"
    validation: JsonObject = Field(default_factory=dict)
    receipt: JsonObject = Field(default_factory=dict)
    attestation: JsonObject = Field(default_factory=dict)
    agent_notice: str = ""
    agent_events: list[JsonObject] = Field(default_factory=list)
    error_context: OperationalErrorContext | None = None

    @field_validator("source_plan_hash", "output_sha256", "output_receipt_sha256", "output_attestation_sha256")
    @classmethod
    def hash_when_present(cls, value: str) -> str:
        if value and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("hash fields must be sha256:<64 lowercase hex chars>")
        return value

    @model_validator(mode="after")
    def blocked_finalization_requires_context(self) -> StyleRewriteOutputFinalization:
        if self.status == "blocked":
            if not self.blocked_reason:
                raise ValueError("blocked style rewrite finalization requires blocked_reason")
            if not self.next_action:
                raise ValueError("blocked style rewrite finalization requires next_action")
            if self.error_context is None:
                raise ValueError("blocked style rewrite finalization requires error_context")
        return self


class StyleRewriteManifest(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-output-manifest.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    source_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    batch_id: str = ""
    items: list[StyleRewriteManifestItem]

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
        return _canonical_hash(payload)


class StyleRewriteOutputCollectionIssue(ContractModel):
    work_id: str = ""
    output_path: str = ""
    output_attestation_path: str = ""
    output_receipt_path: str = ""
    error: str = ""


class StyleRewriteOutputCollection(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-output-collection.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = ""
    manifest_path: str = ""
    source_plan_hash: str = ""
    manifest_hash: str = ""
    item_count: int = 0
    items: list[StyleRewriteManifestItem] = Field(default_factory=list)
    missing_output_count: int = 0
    missing_outputs: list[StyleRewriteOutputCollectionIssue] = Field(default_factory=list)
    missing_output_attestation_count: int = 0
    missing_output_attestations: list[StyleRewriteOutputCollectionIssue] = Field(default_factory=list)
    invalid_output_attestation_count: int = 0
    invalid_output_attestations: list[StyleRewriteOutputCollectionIssue] = Field(default_factory=list)
    missing_output_receipt_count: int = 0
    missing_output_receipts: list[StyleRewriteOutputCollectionIssue] = Field(default_factory=list)
    invalid_output_receipt_count: int = 0
    invalid_output_receipts: list[StyleRewriteOutputCollectionIssue] = Field(default_factory=list)
    agent_notice: str = ""
    agent_events: list[JsonObject] = Field(default_factory=list)
    error_context: OperationalErrorContext | None = None

    @field_validator("source_plan_hash", "manifest_hash")
    @classmethod
    def hash_when_present(cls, value: str) -> str:
        if value and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("hash fields must be sha256:<64 lowercase hex chars>")
        return value

    @model_validator(mode="after")
    def blocked_collection_requires_context(self) -> StyleRewriteOutputCollection:
        if self.status == "blocked":
            if not self.blocked_reason:
                raise ValueError("blocked style rewrite output collection requires blocked_reason")
            if not self.next_action:
                raise ValueError("blocked style rewrite output collection requires next_action")
            if self.error_context is None:
                raise ValueError("blocked style rewrite output collection requires error_context")
        return self


class StyleRewriteApplyItemReceipt(ContractModel):
    work_id: str = Field(min_length=1)
    target_path: str = ""
    output_path: str = ""
    status: Literal["applied", "blocked", "idempotent"]
    blocked_reason: str = ""
    changed: bool = False
    written: bool = False
    backup_path: str | None = None
    next_action: str = ""
    agent_notice: str = ""

    @model_validator(mode="after")
    def blocked_items_need_reason(self) -> StyleRewriteApplyItemReceipt:
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked style rewrite items require blocked_reason")
        return self


class StyleRewriteApplyReceipt(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-apply-receipt.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed", "completed_with_link_blockers", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    agent_notice: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = ""
    output_manifest_path: str = ""
    source_plan_hash: str = ""
    manifest_hash: str = ""
    agent_events: list[JsonObject] = Field(default_factory=list)
    error_context: OperationalErrorContext | None = None
    items: list[StyleRewriteApplyItemReceipt]
    linker: StyleRewriteLinkerEvidence | None = None
    link_trigger_context_path: str = ""
    linker_trigger_context_path: str = ""
    linker_diagnosis_path: str = ""
    linker_receipt_path: str = ""
    linker_applied: bool = False
    linker_skipped_reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _derive_link_blocker_from_nested_linker(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return _style_rewrite_linker_blocker_payload(value)

    @field_validator("source_plan_hash", "manifest_hash")
    @classmethod
    def hash_when_present(cls, value: str) -> str:
        if value and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("hash fields must be sha256:<64 lowercase hex chars>")
        return value

    @model_validator(mode="after")
    def blocked_receipt_requires_context(self) -> StyleRewriteApplyReceipt:
        if self.status == "completed_with_link_blockers":
            if not self.blocked_reason:
                raise ValueError("style rewrite receipts with link blockers require blocked_reason")
            if not self.linker_skipped_reason:
                raise ValueError("style rewrite receipts with link blockers require linker_skipped_reason")
            if not self.next_action:
                raise ValueError("style rewrite receipts with link blockers require next_action")
            if self.error_context is None:
                raise ValueError("style rewrite receipts with link blockers require error_context")
        if self.status == "blocked":
            if not self.blocked_reason:
                raise ValueError("blocked style rewrite receipts require blocked_reason")
            if not self.next_action:
                raise ValueError("blocked style rewrite receipts require next_action")
            if self.error_context is None:
                raise ValueError("blocked style rewrite receipts require error_context")
        return self


class _StyleRewriteAtomicApplySource(ContractModel):
    """Read-only input view for propagating typed apply receipt blockers."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    apply: StyleRewriteApplyReceipt | None = None


class StyleRewriteAtomicApplyResult(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-atomic-apply-result.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["style_rewrite"]
    status: Literal["completed", "completed_with_link_blockers", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = ""
    manifest_path: str = ""
    work_id: str = Field(min_length=1)
    specialist_run_receipt_path: str = ""
    finalization: StyleRewriteOutputFinalization | None = None
    collection: StyleRewriteOutputCollection | None = None
    apply: StyleRewriteApplyReceipt | None = None
    agent_notice: str = ""
    error_context: OperationalErrorContext | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_link_blocker_from_apply_receipt(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        source = _StyleRewriteAtomicApplySource.model_validate(value)
        if source.apply is None:
            return value
        apply_receipt = source.apply
        if apply_receipt.status != "completed_with_link_blockers":
            return value
        return dict(value) | {
            "status": apply_receipt.status,
            "blocked_reason": apply_receipt.blocked_reason,
            "next_action": apply_receipt.next_action,
            "error_context": apply_receipt.error_context,
        }

    @model_validator(mode="after")
    def blocked_atomic_apply_requires_context(self) -> StyleRewriteAtomicApplyResult:
        if self.status in {"blocked", "completed_with_link_blockers"}:
            if not self.blocked_reason:
                raise ValueError("blocked atomic style rewrite result requires blocked_reason")
            if not self.next_action:
                raise ValueError("blocked atomic style rewrite result requires next_action")
            if self.error_context is None:
                raise ValueError("blocked atomic style rewrite result requires error_context")
        if self.status == "completed" and self.apply is None:
            raise ValueError("completed atomic style rewrite result requires apply receipt")
        return self


class StyleRewriteAtomicApplyAgentItem(ContractModel):
    work_id: str = ""
    target_path: str = ""
    output_path: str = ""
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    changed: bool = False
    written: bool = False


class StyleRewriteBatchProgressAgentHandoff(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-batch-progress-agent-handoff.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    required: bool = True
    agent_instruction: str = Field(min_length=1)


class StyleRewriteHumanProgressCheckpoint(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-human-progress-checkpoint.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    summary: str = Field(min_length=1)
    content_quality: str = Field(min_length=1)
    preserved: list[str] = Field(default_factory=list)
    linker_summary: str = ""
    remaining_summary: str = Field(min_length=1)


class StyleRewriteAtomicApplyAgentStdout(ContractModel):
    schema_: Literal["medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    source_schema: Literal["medical-notes-workbench.style-rewrite-atomic-apply-result.v1"]
    phase: Literal["style_rewrite"]
    status: Literal["completed", "completed_with_link_blockers", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = ""
    manifest_path: str = ""
    work_id: str = Field(min_length=1)
    specialist_run_receipt_path: str = ""
    item_count: int = 0
    written_count: int = 0
    changed_count: int = 0
    items: list[StyleRewriteAtomicApplyAgentItem] = Field(default_factory=list)
    linker_trigger_context_path: str = ""
    linker_diagnosis_path: str = ""
    linker_receipt_path: str = ""
    linker_applied: bool = False
    linker_skipped_reason: str = ""
    linker: JsonObject = Field(default_factory=dict)
    human_progress_checkpoint: StyleRewriteHumanProgressCheckpoint
    batch_progress_report: StyleRewriteBatchProgressAgentHandoff
    agent_directive: AgentDirective
    error_context: OperationalErrorContext | None = None
