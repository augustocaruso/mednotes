"""Domain effect payloads carried by workflow effects.

These are MedNotes **domain** contracts — the concrete payloads (link subworkflow,
related-notes, specialist) attached to the framework's WorkflowEffect/Result
(which live in the pure-framework mednotes.kernel.effects). They are kept out of
the framework so the FSM kernel stays domain-agnostic. Layering rule: framework
<- domain <- adapters; enforced by tools/audit/import_layering.py.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mednotes.domains.wiki.contracts.related_notes_runtime import RelatedNotesRecoveryState
from mednotes.domains.wiki.contracts.specialist import SpecialistTaskRunReceipt
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.progress import WorkflowProgressViewModel
from mednotes.kernel.workflow import VersionControlSafety, WorkflowReceiptPayload


class LinkEffectCompletedOutcome(ContractModel):
    """Domain outcome for a link effect that finished with safe evidence."""

    code: Literal["link.completed"] = "link.completed"


class LinkEffectBlockedOutcome(ContractModel):
    """Domain outcome for a link effect blocked before safe continuation."""

    code: Literal["link.blocked"] = "link.blocked"
    reason_code: str = ""


class LinkEffectGraphBlockedOutcome(ContractModel):
    """Domain outcome for graph-level blockers returned by the link workflow."""

    code: Literal["graph_blocked"] = "graph_blocked"


class LinkEffectLinkerBlockedOutcome(ContractModel):
    """Domain outcome for linker/runtime blockers returned by the link workflow."""

    code: Literal["linker_blocked"] = "linker_blocked"
    reason_code: str = ""


class LinkEffectFailedOutcome(ContractModel):
    """Domain outcome for failed link effect execution."""

    code: Literal["link.failed"] = "link.failed"
    reason_code: str = ""


class RelatedNotesExportCompletedOutcome(ContractModel):
    """Domain outcome for a refreshed Related Notes export."""

    code: Literal["related_notes.export_completed"] = "related_notes.export_completed"


class RelatedNotesQuotaWaitOutcome(ContractModel):
    """Domain outcome for resumable external waits while preserving progress."""

    code: Literal["related_notes.quota_wait"] = "related_notes.quota_wait"
    reason_code: str = ""


class RelatedNotesBlockedOutcome(ContractModel):
    """Domain outcome for non-resumable Related Notes blockers."""

    code: Literal["related_notes.blocked"] = "related_notes.blocked"
    reason_code: str = ""


class RelatedNotesSyncCompletedOutcome(ContractModel):
    """Domain outcome for mutating Related Notes section sync."""

    code: Literal["related_notes.sync_completed"] = "related_notes.sync_completed"


class RelatedNotesSyncWarningOutcome(ContractModel):
    """Domain outcome for non-mutating previews or warning states."""

    code: Literal["related_notes.sync_warning"] = "related_notes.sync_warning"
    reason_code: str = ""


class SpecialistModelCompletedOutcome(ContractModel):
    """Domain outcome for a validated specialist model receipt."""

    code: Literal["style.specialist_completed"] = "style.specialist_completed"


class SpecialistModelCapacityWaitOutcome(ContractModel):
    """Domain outcome for resumable specialist-capacity waits."""

    code: Literal["style.capacity_wait"] = "style.capacity_wait"
    reason_code: str = ""


class SpecialistModelBlockedOutcome(ContractModel):
    """Domain outcome for specialist output that cannot be applied."""

    code: Literal["style.blocked"] = "style.blocked"
    reason_code: str = ""


class WaitExternalEffectOutcome(ContractModel):
    """Domain-level wait outcome for generic resumable external waits."""

    code: Literal["wait_external.waiting"] = "wait_external.waiting"
    reason_code: str = ""


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source[key] if key in source else default


def _text_or_empty(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return f"{value}"


def _json_object_or_none(value: JsonValue) -> JsonObject | None:
    return _json_object(value) if isinstance(value, dict) else None


def _json_object_or_empty(value: JsonValue) -> JsonObject:
    return _json_object(value) if isinstance(value, dict) else {}


def _json_list_or_empty(value: JsonValue) -> list[JsonValue]:
    return list(value) if isinstance(value, list) else []


class LinkSubworkflowEffectPayload(ContractModel):
    """Typed result returned by the public `/mednotes:link` workflow adapter."""

    schema_id: Literal["medical-notes-workbench.link-fsm-result.v1"] = Field(
        default="medical-notes-workbench.link-fsm-result.v1",
        alias="schema",
    )
    progress_view_model: WorkflowProgressViewModel
    receipt: WorkflowReceiptPayload
    reports: JsonObject
    error_context: JsonObject = Field(default_factory=dict)
    fsm_payload: JsonObject


LinkWorkflowRunKind = Literal[
    "diagnose",
    "link_run",
    "vocabulary_bootstrap",
    "agent_disambiguation",
    "vocabulary_curator",
    "apply_body_links",
    "apply_related_notes",
    "apply_vocabulary_semantic_repair",
]
LINK_WORKFLOW_DIAGNOSTIC_KINDS = frozenset(
    {"diagnose", "vocabulary_bootstrap", "agent_disambiguation", "vocabulary_curator"}
)
LINK_WORKFLOW_APPLY_KINDS = frozenset(
    {"link_run", "apply_body_links", "apply_related_notes", "apply_vocabulary_semantic_repair"}
)


class LinkWorkflowRunEffectPayload(ContractModel):
    """Typed intent for executing the public `/mednotes:link` workflow.

    Parent FSMs and the link FSM itself must not ask the adapter to infer
    mutation from loose booleans or legacy targets. This payload is the single
    command contract consumed before `run_linker` or any equivalent adapter path
    is allowed to touch the vault.
    """

    schema_id: Literal["medical-notes-workbench.link-workflow-run-effect.v1"] = Field(
        default="medical-notes-workbench.link-workflow-run-effect.v1",
        alias="schema",
    )
    kind: LinkWorkflowRunKind
    diagnose: bool = Field(strict=True)
    apply: bool = Field(strict=True)
    diagnosis_path: str = ""
    receipt_path: str = ""
    trigger_context_path: str = ""
    no_related_notes: bool = Field(default=False, strict=True)
    force_diagnose: bool = Field(default=False, strict=True)
    llm_disambiguation: str = "auto"
    llm_model: str = ""
    llm_timeout: int = Field(default=120, ge=1, strict=True)
    db_path: str = ""
    work_item_count: int = Field(default=0, ge=0, strict=True)
    batch_plan_path: str = ""
    # Mutating parent workflows pass the guard receipt as a first-class field so
    # the adapter does not have to infer safety from opaque operation payloads.
    version_control_safety: VersionControlSafety | None = None
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _run_linker_mode_must_be_explicit(self) -> LinkWorkflowRunEffectPayload:
        if self.diagnose == self.apply:
            raise ValueError("link workflow effect requires exactly one of diagnose/apply")
        if self.kind in LINK_WORKFLOW_DIAGNOSTIC_KINDS and not self.diagnose:
            raise ValueError(f"link workflow effect kind {self.kind!r} requires diagnose mode")
        if self.kind in LINK_WORKFLOW_APPLY_KINDS and not self.apply:
            raise ValueError(f"link workflow effect kind {self.kind!r} requires apply mode")
        return self

    @classmethod
    def from_effect_payload(cls, payload: object) -> LinkWorkflowRunEffectPayload:
        operation_payload = _json_object(payload)
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.link-workflow-run-effect.v1",
                "kind": _json_field(operation_payload, "kind"),
                "diagnose": _json_field(operation_payload, "diagnose"),
                "apply": _json_field(operation_payload, "apply"),
                "diagnosis_path": _json_field(operation_payload, "diagnosis_path", ""),
                "receipt_path": _json_field(operation_payload, "receipt_path", ""),
                "trigger_context_path": _json_field(operation_payload, "trigger_context_path", ""),
                "no_related_notes": _json_field(operation_payload, "no_related_notes", False),
                "force_diagnose": _json_field(operation_payload, "force_diagnose", False),
                "llm_disambiguation": _json_field(operation_payload, "llm_disambiguation", "auto"),
                "llm_model": _json_field(operation_payload, "llm_model", ""),
                "llm_timeout": _json_field(operation_payload, "llm_timeout", 120),
                "db_path": _json_field(operation_payload, "db_path", ""),
                "work_item_count": _json_field(operation_payload, "work_item_count", 0),
                "batch_plan_path": _json_field(operation_payload, "batch_plan_path", ""),
                "version_control_safety": _json_field(operation_payload, "version_control_safety"),
                "operation_payload": operation_payload,
            }
        )


class RelatedNotesRecoveryStateEffectPayload(RelatedNotesRecoveryState):
    """Effect-facing alias of the canonical Related Notes recovery state.

    The wait-external adapter must validate the same recovery payload emitted by
    link, link-related and fix-wiki. Keeping this as a subclass preserves the
    effect schema name while avoiding a narrower parallel contract.
    """


class WaitExternalEffectPayload(ContractModel):
    """Typed intent payload for an actual resumable external wait.

    This prevents adapters from converting an arbitrary loose payload into
    `waiting_external`. Related Notes waits carry their full recovery state;
    other external waits carry an explicit target/blocker/resume contract.
    """

    schema_id: Literal["medical-notes-workbench.wait-external-effect-payload.v1"] = Field(
        default="medical-notes-workbench.wait-external-effect-payload.v1",
        alias="schema",
    )
    kind: Literal["wait_external"] = "wait_external"
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload | None = None
    wait_target: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    resume_supported: bool = Field(default=True, strict=True)
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _external_wait_requires_resumable_state(self) -> WaitExternalEffectPayload:
        if not self.resume_supported:
            raise ValueError("wait_external effect requires resume_supported")
        if self.related_notes_recovery_state is not None:
            if self.related_notes_recovery_state.status != "waiting_for_retry":
                raise ValueError("wait_external effect requires waiting_for_retry recovery state")
            if not self.related_notes_recovery_state.resume_supported:
                raise ValueError("wait_external effect requires resume_supported recovery state")
            if not self.related_notes_recovery_state.blocked_reason:
                raise ValueError("wait_external effect requires blocked_reason")
            return self
        if not self.wait_target:
            raise ValueError("wait_external effect requires wait_target when no recovery state exists")
        if not self.blocked_reason:
            raise ValueError("wait_external effect requires blocked_reason")
        return self

    @classmethod
    def from_effect_payload(cls, payload: object) -> WaitExternalEffectPayload:
        operation_payload = _json_object(payload)
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.wait-external-effect-payload.v1",
                "kind": _json_field(operation_payload, "kind", "wait_external"),
                "related_notes_recovery_state": _json_field(operation_payload, "related_notes_recovery_state"),
                "wait_target": _json_field(operation_payload, "wait_target", ""),
                "blocked_reason": _json_field(operation_payload, "blocked_reason", ""),
                "next_action": _json_field(operation_payload, "next_action", ""),
                "resume_supported": _json_field(operation_payload, "resume_supported", True),
                "operation_payload": operation_payload,
            }
        )


class RelatedNotesSyncSectionEffectPayload(ContractModel):
    """Typed intent for applying the Related Notes section sync.

    The operation result uses `RelatedNotesSyncEffectPayload`; this class is the
    FSM-owned command payload that decides whether the adapter may mutate.
    """

    schema_id: Literal["medical-notes-workbench.related-notes-sync-section-effect.v1"] = Field(
        default="medical-notes-workbench.related-notes-sync-section-effect.v1",
        alias="schema",
    )
    kind: Literal["sync_related_notes_section"] = "sync_related_notes_section"
    apply: bool = Field(strict=True)
    export_path: str = ""
    receipt_path: str = ""
    min_score: float = 0.2
    max_links: int = Field(default=10, ge=0, strict=True)
    max_age_hours: float = 168.0
    operation_payload: JsonObject = Field(default_factory=dict)

    @classmethod
    def from_effect_payload(cls, payload: object) -> RelatedNotesSyncSectionEffectPayload:
        operation_payload = _json_object(payload)
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.related-notes-sync-section-effect.v1",
                "kind": _json_field(operation_payload, "kind", "sync_related_notes_section"),
                "apply": _json_field(operation_payload, "apply"),
                "export_path": _json_field(operation_payload, "export_path", ""),
                "receipt_path": _json_field(operation_payload, "receipt_path", ""),
                "min_score": _json_field(operation_payload, "min_score", 0.2),
                "max_links": _json_field(operation_payload, "max_links", 10),
                "max_age_hours": _json_field(operation_payload, "max_age_hours", 168.0),
                "operation_payload": operation_payload,
            }
        )


class RelatedNotesExportEffectPayload(ContractModel):
    """Typed intent for recovering or refreshing the Related Notes export.

    Export recovery is non-mutating, but it still decides whether the workflow
    can continue, must wait for quota, or needs Obsidian/user recovery. Adapters
    must validate this command before invoking the runtime so a loose dict cannot
    fabricate a recovery lane.
    """

    schema_id: Literal["medical-notes-workbench.related-notes-export-effect.v1"] = Field(
        default="medical-notes-workbench.related-notes-export-effect.v1",
        alias="schema",
    )
    kind: Literal["related_notes_export"] = "related_notes_export"
    mode: str = "auto"
    export_path: str = ""
    reason_code: str = ""
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _export_mode_must_be_explicit(self) -> RelatedNotesExportEffectPayload:
        normalized = self.mode.replace("-", "_").strip()
        allowed = {
            "auto",
            "reindex_vault",
            "index_missing",
            "index_missing_notes",
            "export_only_diagnostic",
        }
        if normalized not in allowed:
            raise ValueError(f"unsupported related notes export mode: {self.mode!r}")
        return self

    @classmethod
    def from_effect_payload(cls, payload: object) -> RelatedNotesExportEffectPayload:
        operation_payload = _json_object(payload)
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.related-notes-export-effect.v1",
                "kind": _json_field(operation_payload, "kind"),
                "mode": _json_field(operation_payload, "mode", "auto"),
                "export_path": _json_field(operation_payload, "export_path", ""),
                "reason_code": _json_field(operation_payload, "reason_code", ""),
                "operation_payload": operation_payload,
            }
        )


class RelatedNotesRecoveryEffectPayload(ContractModel):
    schema_id: Literal["medical-notes-workbench.related-notes-export-recovery.v1"] = Field(
        default="medical-notes-workbench.related-notes-export-recovery.v1",
        alias="schema",
    )
    status: Literal["recovered", "completed", "blocked", "failed"]
    blocked_reason: str = ""
    next_action: str = ""
    related_notes_recovery_state: RelatedNotesRecoveryStateEffectPayload | None = None
    receipt: JsonObject | None = None
    operation_payload: JsonObject = Field(default_factory=dict)

    @classmethod
    def from_operation_payload(cls, payload: object) -> RelatedNotesRecoveryEffectPayload:
        operation_payload = _json_object(payload)
        receipt = _json_object_or_none(_json_field(operation_payload, "receipt"))
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.related-notes-export-recovery.v1",
                "status": _json_field(operation_payload, "status"),
                "blocked_reason": _json_field(operation_payload, "blocked_reason", ""),
                "next_action": _json_field(operation_payload, "next_action", ""),
                "related_notes_recovery_state": _json_field(operation_payload, "related_notes_recovery_state"),
                "receipt": receipt,
                "operation_payload": operation_payload,
            }
        )


class RelatedNotesSyncEffectPayload(ContractModel):
    schema_id: Literal["medical-notes-workbench.related-notes-sync.v1"] = Field(
        default="medical-notes-workbench.related-notes-sync.v1",
        alias="schema",
    )
    status: Literal["completed", "preview_ready", "completed_with_warnings", "blocked", "skipped"]
    phase: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    applied_note_count: int = Field(default=0, ge=0, strict=True)
    updates: list[JsonObject] = Field(default_factory=list)
    receipt: JsonObject | None = None
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _completed_requires_receipt(self) -> RelatedNotesSyncEffectPayload:
        if self.status == "completed" and self.receipt is None:
            raise ValueError("completed related notes sync effect payload requires receipt")
        return self

    @classmethod
    def from_operation_payload(cls, payload: object) -> RelatedNotesSyncEffectPayload:
        operation_payload = _json_object(payload)
        updates = _json_list_or_empty(_json_field(operation_payload, "updates"))
        receipt = _json_object_or_none(_json_field(operation_payload, "receipt"))
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema") or "medical-notes-workbench.related-notes-sync.v1",
                "status": _json_field(operation_payload, "status"),
                "phase": _json_field(operation_payload, "phase", ""),
                "blocked_reason": _json_field(operation_payload, "blocked_reason", ""),
                "next_action": _json_field(operation_payload, "next_action", ""),
                "applied_note_count": _json_field(operation_payload, "applied_note_count", 0),
                "updates": updates,
                "receipt": receipt,
                "operation_payload": operation_payload,
            }
        )


class SpecialistModelEffectPayload(ContractModel):
    schema_id: Literal["medical-notes-workbench.specialist-model-effect-payload.v1"] = Field(
        default="medical-notes-workbench.specialist-model-effect-payload.v1",
        alias="schema",
    )
    status: str = Field(min_length=1)
    blocked_reason: str = ""
    payload: JsonObject = Field(default_factory=dict)
    receipt: SpecialistTaskRunReceipt | None = None
    attestation: JsonObject | None = None
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    operation_payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _completed_requires_receipt_and_attestation(self) -> SpecialistModelEffectPayload:
        if self.status == "completed":
            if self.receipt is None:
                raise ValueError("completed specialist model effect payload requires receipt")
            if self.attestation is None:
                raise ValueError("completed specialist model effect payload requires attestation")
            if self.receipt.specialist_output_attestation is None:
                raise ValueError("completed specialist model effect payload requires receipt attestation")
            expected_attestation = self.receipt.specialist_output_attestation.to_payload()
            if self.attestation != expected_attestation:
                raise ValueError("completed specialist model effect payload attestation must match receipt attestation")
        return self

    @classmethod
    def from_operation_payload(cls, payload: object) -> SpecialistModelEffectPayload:
        operation_payload = _json_object(payload)
        specialist_payload = _json_object_or_empty(_json_field(operation_payload, "payload"))
        receipt_payload = _json_object_or_none(_json_field(operation_payload, "receipt"))
        attestation = _json_object_or_none(_json_field(operation_payload, "attestation"))
        receipt = (
            SpecialistTaskRunReceipt.from_operation_payload(receipt_payload)
            if receipt_payload is not None
            else None
        )
        return cls.model_validate(
            {
                "schema": _json_field(operation_payload, "schema")
                or "medical-notes-workbench.specialist-model-effect-payload.v1",
                "status": _json_field(operation_payload, "status"),
                "blocked_reason": _text_or_empty(_json_field(operation_payload, "blocked_reason")),
                "payload": specialist_payload,
                "receipt": receipt,
                "attestation": attestation,
                "next_action": _text_or_empty(_json_field(operation_payload, "next_action")),
                "required_inputs": _json_list_or_empty(_json_field(operation_payload, "required_inputs")),
                "operation_payload": operation_payload,
            }
        )
