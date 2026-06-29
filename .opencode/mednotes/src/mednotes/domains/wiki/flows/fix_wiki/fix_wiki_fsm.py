from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic.json_schema import SkipJsonSchema

from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkWorkflowRunEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
)
from mednotes.domains.wiki.contracts.related_notes_runtime import RelatedNotesRecoveryState
from mednotes.domains.wiki.contracts.workflow_guardrails import error_context as build_error_context
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    HumanDecisionOption,
    RejectedAutomation,
    WorkflowDecision,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_machine import (
    FixWikiBoundaryEvent,
    FixWikiBoundaryEventAdapter,
    FixWikiMachine,
    FixWikiRuntimeObservation,
    RuntimeObservedEvent,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_primary_objective import fix_wiki_primary_objective_summary
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_states import (
    FIX_WIKI_WORKFLOW,
    FixWikiReason,
    FixWikiState,
    category_for_state,
    reason_for_state,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_states import (
    FixWikiDiagnosisLane as FixWikiDiagnosisLane,
)
from mednotes.kernel.agent_directive import (
    AgentDirective,
    agent_directive_from_progress_view_model,
    assert_agent_directive_matches_progress,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.progress import (
    WorkflowProgressCounts,
    WorkflowProgressEvent,
    WorkflowProgressEventType,
    WorkflowProgressState,
    WorkflowProgressStatus,
    WorkflowProgressViewModel,
    build_progress_view_model,
    progress_state_from_view_model,
)
from mednotes.kernel.public_report import (
    WorkflowPublicReport,
    WorkflowReports,
    assert_public_report_matches_progress,
    public_progress_followup_line,
)
from mednotes.kernel.state_machine import (
    WorkflowStateCategory,
    WorkflowStateMachineSnapshot,
    WorkflowTransition,
    send_workflow_event,
)
from mednotes.kernel.workflow import (
    HumanDecisionPacket,
    ReceiptStatus,
    VersionControlSafety,
    WorkflowPhaseOutcome,
    WorkflowReceiptPayload,
    assert_diagnostic_context_evidence_only,
    diagnostic_context_evidence_only,
)

FIX_WIKI_SCHEMA = "medical-notes-workbench.fix-wiki-fsm-result.v1"
FIX_WIKI_RECEIPT_SCHEMA = "medical-notes-workbench.fix-wiki-receipt.v3"
MEDNOTES_AGENT_DIRECTIVE_SCHEMA = "medical-notes-workbench.agent-directive.v1"

FIX_WIKI_ALLOWED_ROOT_KEYS = frozenset(
    {
        "schema",
        "workflow",
        "run_id",
        "state_machine_snapshot",
        "progress_view_model",
        "decision",
        "human_decision_packet",
        "receipt",
        "reports",
        "agent_directive",
        "artifacts",
        "version_control_safety",
        "diagnostic_context",
        "error_context",
    }
)
FIX_WIKI_FORBIDDEN_ROOT_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "next_command",
        "execution_gate",
        "resume_after_resolution",
        "orchestration_plan",
        "workflow_exit_code",
        "public_report",
        "requested_apply",
        "effective_apply",
        "blocker_resolution",
        "final_validation",
    }
)
FIX_WIKI_DIAGNOSTIC_PARALLEL_TRUTH_KEYS = frozenset(
    {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "next_command",
        "workflow_exit_code",
        "requested_apply",
        "effective_apply",
        "required_inputs",
        "human_decision_required",
        "human_decision_kinds",
        "primary_human_decision_kind",
        "human_decision_packet",
        "resume_action",
        "action_directives",
        "pending_effects",
    }
)
FIX_WIKI_DIAGNOSTIC_OPERATIONAL_PLAN_KEYS = frozenset(
    {
        "status",
        "phase",
        "route",
        "blocked_reason",
        "next_action",
        "next_command",
        "agent_instruction",
        "executable_now",
        "current_work_item",
        "current_batch_items",
        "continuation_steps",
        "parent_steps",
        "execution_contract",
        "runtime_execution",
        "resume_action",
    }
)
FIX_WIKI_RELATED_RECOVERY_COUNT_FIELDS = frozenset(
    {
        "fresh_record_count",
        "partial_record_count",
        "stale_record_count",
        "record_count",
        "total_note_count",
        "remaining_count",
        "embedded_count",
        "reused_count",
        "attempt_count",
        "next_retry_after_seconds",
    }
)

_PHASE_BY_STATE = {
    "diagnosis.running": "diagnosis",
    "environment.paths_missing": "environment",
    "environment.wiki_dir_missing": "environment",
    "environment.windows_path_or_venv_blocked": "environment",
    "vault_guard.running": "vault_guard",
    "vault_guard.decision_required": "vault_guard",
    "subagent_plan_attestation.required": "subagent_plan_attestation",
    "subagent_plan_attestation.invalid": "subagent_plan_attestation",
    "agent_tool_contract_violation": "agent_tool_contract",
    "deterministic_repairs.running": "deterministic_repairs",
    "deterministic_repairs.failed": "deterministic_repairs",
    "style_rewrite.specialist_requested": "style_rewrite",
    "style_rewrite.capacity_wait": "style_rewrite",
    "style_rewrite.review_required": "style_rewrite",
    "style_rewrite.apply_running": "style_rewrite",
    "taxonomy.decision_required": "taxonomy",
    "taxonomy.apply_running": "taxonomy",
    "vocabulary.curator_running": "vocabulary",
    "vocabulary.semantic_ingestion_pending": "vocabulary",
    "vocabulary.eval_running": "vocabulary",
    "vocabulary.eval_needs_review": "vocabulary",
    "vocabulary.apply_running": "vocabulary",
    "vocabulary.sqlite_integrity_failed": "vocabulary",
    "atomicity_split.running": "atomicity_split",
    "atomicity_split.review_required": "atomicity_split",
    "related_notes.export_running": "related_notes",
    "related_notes.quota_wait": "related_notes_recovery",
    "related_notes.obsidian_not_ready": "related_notes",
    "related_notes.blocked": "related_notes",
    "link.run_requested": "link",
    "link.graph_blocked": "link",
    "link.graph_review_required": "link",
    "link.linker_blocked": "link",
    "merge.running": "merge",
    "merge.review_required": "merge",
    "contract_gap.missing_next_action": "contract_gap",
    "contract_gap.missing_error_context": "contract_gap",
    "rollback.running": "rollback",
    "rollback.performed": "rollback",
    "rollback.failed": "rollback",
    "final_validation.running": "final_validation",
    "final_validation.failed": "final_validation",
    "preview.ready": "preview",
    "completed": "final_validation",
    "completed_with_warnings": "final_validation",
    "waiting_agent": "style_rewrite",
    "waiting_external": "external_wait",
    "waiting_for_external_quota": "related_notes_recovery",
    "waiting_human": "human_decision",
    "blocked": "blocked",
    "failed": "failure",
}



class FixWikiRuntimeFacts(ContractModel):
    """Typed adapter input produced by health/runtime before entering the FSM.

    This model is deliberately not the public FSM facts. It is the current
    runtime boundary that turns validated health facts into one canonical
    `FixWikiMachine` event, so diagnostic-only fields cannot fabricate a
    public state after this point.
    """

    run_id: str = Field(min_length=1)
    requested_apply: bool = Field(strict=True)
    effective_apply: bool = Field(strict=True)
    total_changed_count: int = Field(default=0, ge=0, strict=True)
    vault_changed_file_count: int = Field(default=0, ge=0, strict=True)
    written_count: int = Field(default=0, ge=0, strict=True)
    warning_count: int = Field(default=0, ge=0, strict=True)
    requires_llm_rewrite_count: int = Field(default=0, ge=0, strict=True)
    final_validation: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    artifacts: JsonObject = Field(default_factory=dict)
    related_notes_blocked: bool = Field(default=False, strict=True)
    related_notes_recovery_state: RelatedNotesRecoveryState = Field(default_factory=RelatedNotesRecoveryState)
    vocabulary_semantic_ingestion_pending: bool = Field(default=False, strict=True)
    vocabulary_eval_needs_review: bool = Field(default=False, strict=True)
    atomicity_split_required: bool = Field(default=False, strict=True)
    merge_review_required: bool = Field(default=False, strict=True)
    human_decision_required: bool = Field(default=False, strict=True)
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    changed_files: list[str] = Field(default_factory=list)
    graph_error_count: int = Field(default=0, ge=0, strict=True)
    graph_blocker_count: int = Field(default=0, ge=0, strict=True)
    graph_review_required: bool = Field(default=False, strict=True)
    linker_blocked: bool = Field(default=False, strict=True)
    linker_apply_attempted: bool = Field(default=False, strict=True)
    taxonomy_action_required: bool = Field(default=False, strict=True)
    failed: bool = Field(default=False, strict=True)
    failed_reason_code: str = ""
    vault_guard_required: bool = Field(default=False, strict=True)
    environment_windows_path_or_venv_blocked: bool = Field(default=False, strict=True)
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    resume_action: str = ""
    pending_effects: list[WorkflowEffect] = Field(default_factory=list)
    external_wait_reason_code: str = ""
    external_wait_resume_action: str = ""
    external_wait_payload: JsonObject = Field(default_factory=dict)
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_noncanonical_pending_effects_at_boundary(cls, value: object) -> object:
        """Reject noncanonical effect shims before projection logic can inspect them.

        `pending_effects` is an FSM-owned contract. The projector may validate a
        canonical `WorkflowEffect`, but it must not fill `phase`,
        `origin_state`, `workflow`, `run_id`, or `model_policy` on behalf of a
        noncanonical producer because that would make success depend on adapter
        glue rather than the StateChart transition that emitted the effect.
        """

        if not isinstance(value, dict):
            return value
        if "pending_effects" not in value:
            return value
        for raw_effect in value["pending_effects"]:
            if isinstance(raw_effect, WorkflowEffect):
                data = raw_effect.to_payload()
            else:
                data = dict(JsonObjectAdapter.validate_python(raw_effect))
            if "phase" in data:
                raise ValueError("pending effect must use origin_state, not phase")
            kind = str(data["kind"]) if "kind" in data else ""
            origin_state = str(data["origin_state"]).strip() if "origin_state" in data else ""
            specialist_origin = FixWikiState.STYLE_REWRITE_SPECIALIST_REQUESTED.value
            if not origin_state:
                raise ValueError("pending effect origin_state is required")
            if kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL.value and origin_state != specialist_origin:
                raise ValueError("pending effect origin_state must match style_rewrite.specialist_requested")
            if (
                "kind" in data
                and data["kind"] == WorkflowEffectKind.CALL_SPECIALIST_MODEL.value
                and ("model_policy" not in data or not data["model_policy"])
            ):
                raise ValueError("call_specialist_model pending effect requires model_policy")
        return value

    @field_validator("related_notes_recovery_state", mode="before")
    @classmethod
    def _coerce_related_notes_recovery_state(cls, value: object) -> RelatedNotesRecoveryState:
        if isinstance(value, RelatedNotesRecoveryState):
            return value
        if isinstance(value, dict):
            for field_name in FIX_WIKI_RELATED_RECOVERY_COUNT_FIELDS:
                if field_name not in value:
                    continue
                raw_value = value[field_name]
                if type(raw_value) is not int:
                    raise ValueError(f"invalid numeric recovery_state value: {raw_value}")
        return RelatedNotesRecoveryState.from_payload(value)

    @model_validator(mode="after")
    def _human_wait_requires_closed_packet(self) -> FixWikiRuntimeFacts:
        if self.human_decision_required:
            if self.decision is None:
                raise ValueError("human_decision_required requires decision")
            if self.human_decision_packet is None:
                raise ValueError("human_decision_required requires human_decision_packet")
            if self.decision.kind != "ask_human":
                raise ValueError("human_decision_required requires ask_human decision")
            if self.human_decision_packet.to_payload() != self.decision.to_human_decision_packet():
                raise ValueError("human_decision_packet must match decision")
        if self._would_complete_apply() and not _final_validation_has_evidence(self.final_validation):
            raise ValueError("final_validation evidence required before completed fix-wiki apply")
        _assert_final_validation_graph_matches_counters(
            self.final_validation,
            graph_error_count=self.graph_error_count,
            graph_blocker_count=self.graph_blocker_count,
        )
        return self

    def _would_complete_apply(self) -> bool:
        """Return true only for the clean apply path that would enter a terminal success state."""

        return self.effective_apply and not any(
            (
                self.failed,
                self.human_decision_required,
                self.related_notes_blocked,
                self.vocabulary_semantic_ingestion_pending,
                self.vocabulary_eval_needs_review,
                self.atomicity_split_required,
                self.merge_review_required,
                self.graph_review_required,
                self.graph_error_count,
                self.graph_blocker_count,
                self.linker_blocked,
                self.taxonomy_action_required,
                self.requires_llm_rewrite_count,
                self.pending_effects,
                self.external_wait_reason_code,
                self.related_notes_recovery_state.status,
            )
        )


def _final_validation_has_evidence(payload: JsonObject) -> bool:
    """Require concrete validation counters before apply can become success."""

    graph = payload["graph"] if "graph" in payload else None
    if not isinstance(graph, dict):
        return False
    for key in ("error_count", "blocker_count"):
        if key in graph and type(graph[key]) is int:
            return True
    return False


def _assert_final_validation_graph_matches_counters(
    payload: JsonObject,
    *,
    graph_error_count: int,
    graph_blocker_count: int,
) -> None:
    """Keep final validation as evidence for the canonical graph counters."""

    graph = payload["graph"] if "graph" in payload else None
    if not isinstance(graph, dict):
        return
    expected = {
        "error_count": graph_error_count,
        "blocker_count": graph_blocker_count,
    }
    for key, canonical_value in expected.items():
        if key not in graph:
            continue
        observed = graph[key]
        if type(observed) is not int:
            continue
        if observed != canonical_value:
            raise ValueError(f"final_validation graph {key} must match canonical graph counter")


class FixWikiFsmFacts(ContractModel):
    """Canonical public projector input: one valid StateChart edge plus context."""

    run_id: str = Field(min_length=1)
    initial_state: FixWikiState
    event: FixWikiBoundaryEvent
    runtime: FixWikiRuntimeFacts
    machine_effects: list[WorkflowEffect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _event_must_match_fsm_entry(self) -> FixWikiFsmFacts:
        if self.event.workflow != FIX_WIKI_WORKFLOW:
            raise ValueError(f"fix-wiki event workflow must be {FIX_WIKI_WORKFLOW}")
        if self.event.run_id != self.run_id:
            raise ValueError("fix-wiki event run_id must match FixWikiFsmFacts.run_id")
        if self.event.current_state != self.initial_state.value:
            raise ValueError("fix-wiki event current_state must match initial_state")
        if self.runtime.run_id != self.run_id:
            raise ValueError("runtime run_id must match FixWikiFsmFacts.run_id")
        return self

    def with_runtime_updates(self, update: dict[str, object]) -> FixWikiFsmFacts:
        """Rebuild the canonical event after adapter/runtime facts change."""

        runtime = FixWikiRuntimeFacts.model_validate({**self.runtime.model_dump(mode="python"), **update})
        return _fix_wiki_fsm_facts_from_runtime_model(runtime)

    @property
    def requested_apply(self) -> bool:
        return self.runtime.requested_apply

    @property
    def effective_apply(self) -> bool:
        return self.runtime.effective_apply

    @property
    def total_changed_count(self) -> int:
        return self.runtime.total_changed_count

    @property
    def vault_changed_file_count(self) -> int:
        return self.runtime.vault_changed_file_count

    @property
    def written_count(self) -> int:
        return self.runtime.written_count

    @property
    def warning_count(self) -> int:
        return self.runtime.warning_count

    @property
    def requires_llm_rewrite_count(self) -> int:
        return self.runtime.requires_llm_rewrite_count

    @property
    def final_validation(self) -> JsonObject:
        return self.runtime.final_validation

    @property
    def version_control_safety(self) -> VersionControlSafety:
        return self.runtime.version_control_safety

    @property
    def artifacts(self) -> JsonObject:
        return self.runtime.artifacts

    @property
    def related_notes_blocked(self) -> bool:
        return self.runtime.related_notes_blocked

    @property
    def related_notes_recovery_state(self) -> RelatedNotesRecoveryState:
        return self.runtime.related_notes_recovery_state

    @property
    def vocabulary_semantic_ingestion_pending(self) -> bool:
        return self.runtime.vocabulary_semantic_ingestion_pending

    @property
    def vocabulary_eval_needs_review(self) -> bool:
        return self.runtime.vocabulary_eval_needs_review

    @property
    def atomicity_split_required(self) -> bool:
        return self.runtime.atomicity_split_required

    @property
    def merge_review_required(self) -> bool:
        return self.runtime.merge_review_required

    @property
    def human_decision_required(self) -> bool:
        return self.runtime.human_decision_required

    @property
    def decision(self) -> WorkflowDecision | None:
        return self.runtime.decision

    @property
    def human_decision_packet(self) -> HumanDecisionPacket | None:
        return self.runtime.human_decision_packet

    @property
    def changed_files(self) -> list[str]:
        return self.runtime.changed_files

    @property
    def graph_error_count(self) -> int:
        return self.runtime.graph_error_count

    @property
    def graph_blocker_count(self) -> int:
        return self.runtime.graph_blocker_count

    @property
    def graph_review_required(self) -> bool:
        return self.runtime.graph_review_required

    @property
    def linker_blocked(self) -> bool:
        return self.runtime.linker_blocked

    @property
    def linker_apply_attempted(self) -> bool:
        return self.runtime.linker_apply_attempted

    @property
    def taxonomy_action_required(self) -> bool:
        return self.runtime.taxonomy_action_required

    @property
    def failed(self) -> bool:
        return self.runtime.failed

    @property
    def failed_reason_code(self) -> str:
        return self.runtime.failed_reason_code

    @property
    def next_action(self) -> str:
        return self.runtime.next_action

    @property
    def required_inputs(self) -> list[str]:
        return self.runtime.required_inputs

    @property
    def resume_action(self) -> str:
        return self.runtime.resume_action

    @property
    def pending_effects(self) -> list[WorkflowEffect]:
        return self.runtime.pending_effects

    @property
    def external_wait_reason_code(self) -> str:
        return self.runtime.external_wait_reason_code

    @property
    def external_wait_resume_action(self) -> str:
        return self.runtime.external_wait_resume_action

    @property
    def external_wait_payload(self) -> JsonObject:
        return self.runtime.external_wait_payload

    @property
    def diagnostic_context(self) -> JsonObject:
        return self.runtime.diagnostic_context

    @property
    def error_context(self) -> JsonObject:
        return self.runtime.error_context


class _FixWikiStateView(ContractModel):
    """Display/effect view derived from FixWikiMachine state and transition."""

    reason: FixWikiReason
    state: FixWikiState
    category: WorkflowStateCategory
    status: WorkflowProgressStatus
    event_type: WorkflowProgressEventType
    decision: WorkflowDecision | None = None
    next_action: str = ""
    resume_action: str = ""
    resume_supported: bool = False
    can_continue_now: bool = False
    message: str
    trigger: str


class _FixWikiPayloadProgressView(ContractModel):
    status: StrictStr


class _FixWikiPayloadSnapshot(ContractModel):
    current_category: StrictStr


class _FixWikiPayloadReceipt(ContractModel):
    status: StrictStr


class _FixWikiExternalWaitEffectFields(ContractModel):
    origin_state: StrictStr = ""


class _FixWikiExternalWaitProgressFields(ContractModel):
    model_config = ConfigDict(extra="ignore")

    status: StrictStr = ""
    state: StrictStr = ""


class _FixWikiExternalWaitSnapshotFields(ContractModel):
    model_config = ConfigDict(extra="ignore")

    current_state: StrictStr = ""


class _FixWikiExternalWaitDiagnosticFields(ContractModel):
    model_config = ConfigDict(extra="ignore")

    related_notes_recovery_state: JsonObject = Field(default_factory=dict)


class _FixWikiExternalWaitPayloadFields(ContractModel):
    """Typed lens for child FSM payloads returned by waiting-external effects."""

    model_config = ConfigDict(extra="ignore")

    progress_view_model: _FixWikiExternalWaitProgressFields = Field(default_factory=_FixWikiExternalWaitProgressFields)
    state_machine_snapshot: _FixWikiExternalWaitSnapshotFields = Field(default_factory=_FixWikiExternalWaitSnapshotFields)
    diagnostic_context: _FixWikiExternalWaitDiagnosticFields = Field(default_factory=_FixWikiExternalWaitDiagnosticFields)


class _FixWikiExistingErrorContextFields(ContractModel):
    model_config = ConfigDict(extra="ignore")

    blocked_reason: StrictStr = ""
    root_cause: StrictStr = ""
    next_action: StrictStr = ""


class _FixWikiArtifactPathFields(ContractModel):
    """Typed artifact lens used when a FSM leaf needs a concrete recovery file."""

    model_config = ConfigDict(extra="ignore")

    atomicity_split_plan_path: StrictStr = ""


class _FixWikiErrorRequiredInputs(ContractModel):
    """Typed lens for the only error-context field that can drive UX inputs."""

    model_config = ConfigDict(extra="ignore")

    required_inputs: list[StrictStr] = Field(default_factory=list)


class _FixWikiPendingEffectKind(ContractModel):
    model_config = ConfigDict(extra="ignore")

    kind: StrictStr = ""


class _FixWikiVocabularyBootstrapDiagnostic(ContractModel):
    model_config = ConfigDict(extra="ignore")

    trigger: StrictStr = ""


class _FixWikiPayloadFields(ContractModel):
    workflow: Literal["/mednotes:fix-wiki"]
    progress_view_model: _FixWikiPayloadProgressView
    state_machine_snapshot: _FixWikiPayloadSnapshot
    receipt: _FixWikiPayloadReceipt


class FixWikiFsmResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.fix-wiki-fsm-result.v1"] = Field(
        default=FIX_WIKI_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/mednotes:fix-wiki"] = FIX_WIKI_WORKFLOW
    run_id: str = Field(min_length=1)
    progress_state: SkipJsonSchema[WorkflowProgressState]
    progress_view_model: WorkflowProgressViewModel
    state_machine_snapshot: WorkflowStateMachineSnapshot
    decision: WorkflowDecision | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    receipt: WorkflowReceiptPayload
    reports: WorkflowReports
    agent_directive: JsonObject
    artifacts: JsonObject = Field(default_factory=dict)
    version_control_safety: VersionControlSafety
    diagnostic_context: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _hydrate_progress_state_from_public_payload(cls, value: object) -> object:
        """Accept public payloads where progress_state is intentionally hidden."""

        if not isinstance(value, dict) or "progress_state" in value or "progress_view_model" not in value:
            return value
        hydrated = dict(value)
        progress_view = WorkflowProgressViewModel.model_validate(value["progress_view_model"])
        hydrated["progress_state"] = progress_state_from_view_model(progress_view).to_payload()
        return hydrated

    @model_validator(mode="after")
    def _progress_view_model_matches_state(self) -> FixWikiFsmResult:
        expected = build_progress_view_model(self.progress_state).to_payload()
        if self.progress_view_model.to_payload() != expected:
            raise ValueError("progress_view_model must match progress_state")
        return self

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "schema": self.schema_id,
            "workflow": self.workflow,
            "run_id": self.run_id,
            "state_machine_snapshot": self.state_machine_snapshot.to_payload(),
            "progress_view_model": self.progress_view_model.to_payload(),
            "decision": self.decision.to_payload() if self.decision is not None else None,
            "human_decision_packet": self.human_decision_packet.to_payload()
            if self.human_decision_packet is not None
            else None,
            "receipt": self.receipt.to_payload(),
            "reports": self.reports.to_payload(),
            "agent_directive": dict(self.agent_directive),
            "artifacts": dict(self.artifacts),
            "version_control_safety": self.version_control_safety.to_payload(),
            "error_context": dict(self.error_context),
        }
        if self.diagnostic_context:
            payload["diagnostic_context"] = dict(self.diagnostic_context)
        payload = _payload_with_primary_objective_summary(payload)
        payload = JsonObjectAdapter.validate_python(payload)
        assert_fix_wiki_fsm_payload(payload)
        return payload


def fix_wiki_fsm_facts_from_runtime(**runtime_fields: object) -> FixWikiFsmFacts:
    """Normalize existing fix-wiki runtime facts into one StateChart event."""

    runtime = FixWikiRuntimeFacts.model_validate(runtime_fields)
    return _fix_wiki_fsm_facts_from_runtime_model(runtime)


def _fix_wiki_fsm_facts_from_runtime_model(runtime: FixWikiRuntimeFacts) -> FixWikiFsmFacts:
    initial_state = FixWikiState.DIAGNOSIS_RUNNING
    event = _runtime_observation_event_from_facts(runtime)
    model = _fix_wiki_model_after_event(initial_state, event)
    machine_effects = list(model.last_transition.effects) if model.last_transition is not None else []
    return FixWikiFsmFacts(
        run_id=runtime.run_id,
        initial_state=initial_state,
        event=event,
        runtime=runtime,
        machine_effects=machine_effects,
    )


def build_fix_wiki_fsm_result(facts: FixWikiFsmFacts) -> FixWikiFsmResult:
    model = _fix_wiki_model_after_event(facts.initial_state, facts.event)
    state_view = _state_view_from_model(facts, model)
    progress_state = _progress_state(facts, state_view)
    progress_view_model = build_progress_view_model(progress_state)
    snapshot = _snapshot_from_model(model, state_view, progress_state)
    human_decision_packet = facts.human_decision_packet or _projection_human_decision_packet(state_view)
    receipt = _receipt(facts, state_view, progress_state, snapshot, human_decision_packet=human_decision_packet)
    reports_model = _reports(facts, state_view)
    diagnostic_context = _diagnostic_context(
        facts,
        state_view,
    )
    agent_directive = _agent_directive(
        facts,
        state_view,
        progress_view_model=progress_view_model,
        user_visible_summary=_public_report_summary_text(reports_model.public_report),
    )
    diagnostic_context = _problem_diagnostic_context(diagnostic_context, state_view)
    reports_model = _reports_with_primary_objective_summary(
        reports_model,
        run_id=facts.run_id,
        progress_view_model=progress_view_model,
        receipt=receipt,
        diagnostic_context=diagnostic_context,
    )

    return FixWikiFsmResult(
        run_id=facts.run_id,
        progress_state=progress_state,
        progress_view_model=progress_view_model,
        state_machine_snapshot=snapshot,
        decision=state_view.decision,
        human_decision_packet=human_decision_packet,
        receipt=receipt,
        reports=reports_model,
        agent_directive=agent_directive,
        artifacts=facts.artifacts,
        version_control_safety=facts.version_control_safety,
        diagnostic_context=diagnostic_context,
        error_context=_error_context(facts, state_view),
    )


def _runtime_observation_event_from_facts(facts: FixWikiRuntimeFacts) -> RuntimeObservedEvent:
    """Build the only runtime bridge event; the StateChart owns leaf selection."""

    return RuntimeObservedEvent(
        run_id=facts.run_id,
        current_state=FixWikiState.DIAGNOSIS_RUNNING.value,
        observation=FixWikiRuntimeObservation(
            failed=facts.failed,
            failed_reason_code=facts.failed_reason_code,
            vault_guard_required=facts.vault_guard_required,
            environment_windows_path_or_venv_blocked=facts.environment_windows_path_or_venv_blocked,
            next_action=facts.next_action,
            human_decision_required=facts.human_decision_required,
            external_wait_reason_code=facts.external_wait_reason_code,
            related_notes_waiting_external=_related_notes_waiting_external(facts),
            vocabulary_semantic_ingestion_pending=facts.vocabulary_semantic_ingestion_pending,
            vocabulary_eval_needs_review=facts.vocabulary_eval_needs_review,
            atomicity_split_required=facts.atomicity_split_required,
            merge_review_required=facts.merge_review_required,
            graph_review_required=facts.graph_review_required,
            graph_blocker_count=facts.graph_blocker_count,
            graph_error_count=facts.graph_error_count,
            related_notes_blocked=facts.related_notes_blocked,
            linker_blocked=facts.linker_blocked,
            taxonomy_action_required=facts.taxonomy_action_required,
            specialist_model_waiting_agent=_specialist_model_waiting_agent(facts),
            requires_llm_rewrite_count=facts.requires_llm_rewrite_count,
            effective_apply=facts.effective_apply,
            warning_count=facts.warning_count,
            style_rewrite_effect=_style_rewrite_effect_input_from_runtime(facts),
            link_subworkflow_required=_link_subworkflow_required(facts),
            link_effect=_link_effect_input_from_runtime(facts),
            related_notes_recovery_state=RelatedNotesRecoveryStateEffectPayload.model_validate(
                facts.related_notes_recovery_state.to_payload()
            ),
        ),
        audit_evidence=_runtime_audit_evidence(facts, "runtime_observed"),
    )


def _runtime_audit_evidence(facts: FixWikiRuntimeFacts, reason: str) -> JsonObject:
    return JsonObjectAdapter.validate_python(
        {
            "runtime_reason": reason,
            "requested_apply": facts.requested_apply,
            "effective_apply": facts.effective_apply,
            "counts": {
                "total_changed_count": facts.total_changed_count,
                "vault_changed_file_count": facts.vault_changed_file_count,
                "written_count": facts.written_count,
                "warning_count": facts.warning_count,
                "requires_llm_rewrite_count": facts.requires_llm_rewrite_count,
                "graph_error_count": facts.graph_error_count,
                "graph_blocker_count": facts.graph_blocker_count,
            },
        }
    )


def _style_rewrite_effect_input_from_runtime(facts: FixWikiRuntimeFacts) -> WorkflowEffect | None:
    """Pass typed batch evidence into the StateChart without making it public truth."""

    if not _specialist_model_waiting_agent(facts):
        return None
    for effect in facts.pending_effects:
        if effect.kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL:
            return effect
    return None


def _link_subworkflow_required(facts: FixWikiRuntimeFacts) -> bool:
    """Return true when a fix-wiki mutation must be followed by `/mednotes:link`."""

    if facts.linker_apply_attempted or not facts.effective_apply:
        return False
    if _has_unresolved_work_before_link(facts):
        return False
    return bool(facts.changed_files or facts.vault_changed_file_count or facts.written_count)


def _has_unresolved_work_before_link(facts: FixWikiRuntimeFacts) -> bool:
    """Keep link execution behind higher-priority blockers and human choices."""

    return bool(
        facts.failed
        or facts.human_decision_required
        or facts.decision is not None
        or facts.human_decision_packet is not None
        or facts.external_wait_reason_code
        or _related_notes_waiting_external(facts)
        or facts.vocabulary_semantic_ingestion_pending
        or facts.vocabulary_eval_needs_review
        or facts.atomicity_split_required
        or facts.merge_review_required
        or facts.graph_review_required
        or facts.graph_blocker_count
        or facts.graph_error_count
        or facts.related_notes_blocked
        or facts.linker_blocked
        or facts.taxonomy_action_required
        or _specialist_model_waiting_agent(facts)
        or facts.requires_llm_rewrite_count
    )


def _link_effect_input_from_runtime(facts: FixWikiRuntimeFacts) -> WorkflowEffect | None:
    """Build the private link effect payload consumed by the StateChart action."""

    if not _link_subworkflow_required(facts):
        return None
    link_artifacts = facts.artifacts
    diagnosis_path = _artifact_text_field(link_artifacts, "linker_diagnosis_path")
    receipt_path = _artifact_text_field(link_artifacts, "linker_receipt_path") or _link_receipt_path(diagnosis_path)
    trigger_context_path = _artifact_text_field(link_artifacts, "link_trigger_context_path")
    return WorkflowEffect(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=facts.run_id,
        effect_id="fix-wiki-link-run",
        origin_state=FixWikiState.LINK_RUN_REQUESTED.value,
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:link",
        payload=LinkWorkflowRunEffectPayload(
            kind="link_run",
            diagnose=False,
            apply=True,
            diagnosis_path=diagnosis_path,
            receipt_path=receipt_path,
            trigger_context_path=trigger_context_path,
            no_related_notes=False,
            version_control_safety=facts.version_control_safety,
        ).to_payload(),
        mutates_resources=True,
        rollback_declared=True,
        requires_receipt=False,
    )


def _artifact_text_field(artifacts: JsonObject, key: str) -> str:
    if key not in artifacts:
        return ""
    value = artifacts[key]
    return value if isinstance(value, str) else ""


def _link_receipt_path(diagnosis_path: str) -> str:
    if not diagnosis_path.strip():
        return ""
    return str(Path(diagnosis_path).with_name("link-run-receipt.json"))


def _fix_wiki_model_after_event(initial_state: FixWikiState, event: FixWikiBoundaryEvent) -> WorkflowModel:
    event = FixWikiBoundaryEventAdapter.validate_python(event.to_payload())
    model = WorkflowModel.start(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=event.run_id,
        initial_state=initial_state.value,
    )
    send_workflow_event(
        FixWikiMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        event,
    )
    return model


def _state_view_from_model(facts: FixWikiFsmFacts, model: WorkflowModel) -> _FixWikiStateView:
    """Derive public presentation from the canonical StateChart result."""

    state = FixWikiState(model.state)
    category = category_for_state(state)
    status = WorkflowProgressStatus(category.value)
    reason = _state_reason_from_model(model, state)
    trigger = model.last_transition.trigger if model.last_transition is not None else reason.value
    if category == WorkflowStateCategory.WAITING_HUMAN:
        has_runtime_decision = facts.decision is not None
        decision = facts.decision or (model.last_transition.decision if model.last_transition is not None else None)
        if decision is None:
            raise ValueError("waiting_human state requires decision")
        if has_runtime_decision and decision.reason_code != reason.value:
            decision = _decision_with_leaf_reason(decision, reason_code=reason.value, phase=state.value)
        next_action = _default_next_action(facts, reason) if not has_runtime_decision else ""
        if not has_runtime_decision and next_action and next_action != decision.next_action:
            decision = _decision_with_recovery_action(decision, next_action=next_action)
        return _state_view(
            reason=reason,
            state=state,
            category=category,
            status=status,
            event_type=WorkflowProgressEventType.DECISION_EMITTED,
            decision=decision,
            next_action=decision.next_action,
            resume_action=decision.resume_action,
            resume_supported=bool(decision.resume_action),
            can_continue_now=False,
            message="Fix-wiki aguardando decisao humana antes de continuar.",
            trigger=decision.reason_code or trigger,
        )
    match reason:
        case FixWikiReason.COMPLETED:
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.WORKFLOW_COMPLETED,
                message="Wiki corrigida e conferida.",
                trigger=trigger,
            )
        case FixWikiReason.COMPLETED_WITH_WARNINGS:
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.WORKFLOW_COMPLETED,
                next_action=_default_next_action(facts, reason),
                message="Wiki corrigida com avisos pendentes.",
                trigger=trigger,
            )
        case FixWikiReason.PREVIEW_READY:
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.VALIDATION_COMPLETED,
                message="Previa do fix-wiki pronta.",
                trigger=trigger,
            )
        case (
            FixWikiReason.ENVIRONMENT_PATHS_MISSING
            | FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING
            | FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
        ):
            next_action = _default_next_action(facts, reason)
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="environment",
                reason_code=reason.value,
                public_summary="O ambiente precisa ser preparado antes de continuar o fix-wiki.",
                developer_summary=(
                    "Fix-wiki entered a recoverable environment leaf and emitted the typed "
                    "/mednotes:setup recovery effect."
                ),
                message="Fix-wiki bloqueado por preparacao de ambiente.",
                trigger=trigger,
                next_action=next_action,
                resume_action=next_action,
                resume_supported=True,
            )
        case FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            next_action = _default_next_action(facts, reason)
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.EXTERNAL_WAIT_STARTED,
                next_action=next_action,
                resume_action=facts.resume_action or next_action,
                resume_supported=facts.related_notes_recovery_state.resume_supported,
                can_continue_now=False,
                message=_related_notes_external_wait_message(facts),
                trigger=trigger,
            )
        case FixWikiReason.WAITING_EXTERNAL:
            next_action = facts.next_action or facts.external_wait_resume_action
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.EXTERNAL_WAIT_STARTED,
                next_action=next_action,
                resume_action=next_action,
                resume_supported=True,
                can_continue_now=False,
                message="Workflow aguardando condicao externa para retomar pela rota oficial.",
                trigger=trigger,
            )
        case FixWikiReason.STYLE_REWRITE_READY:
            next_action = _default_next_action(facts, reason)
            decision = WorkflowDecision(
                kind="auto_plan",
                phase="style_rewrite",
                reason_code=reason.value,
                public_summary="A reescrita especializada esta pronta para continuacao assistida.",
                developer_summary=(
                    "Fix-wiki generated a typed call_specialist_model effect and expects the agent "
                    "to continue with the official agent_directive effect route."
                ),
                evidence=[
                    DecisionEvidence(
                        summary="O workflow emitiu efeito tipado para modelo especialista.",
                        technical_code="call_specialist_model",
                        source="fix_wiki_fsm",
                    )
                ],
                next_action=next_action,
            )
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.STATE_ENTERED,
                decision=decision,
                next_action=next_action,
                resume_action=next_action,
                resume_supported=False,
                can_continue_now=True,
                message="Fix-wiki pronto para continuar com reescrita especializada.",
                trigger=trigger,
            )
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            next_action = _default_next_action(facts, reason)
            decision = WorkflowDecision(
                kind="auto_plan",
                phase="vocabulary",
                reason_code=reason.value,
                public_summary="A curadoria semantica do vocabulario esta pronta para continuacao assistida.",
                developer_summary=(
                    "Fix-wiki generated a typed run_subworkflow effect and expects the agent "
                    "to continue vocabulary semantic ingestion through agent_directive.control.effects."
                ),
                evidence=[
                    DecisionEvidence(
                        summary="O workflow emitiu efeito tipado para curadoria semantica do vocabulario.",
                        technical_code=WorkflowEffectKind.RUN_SUBWORKFLOW.value,
                        source="fix_wiki_fsm",
                    )
                ],
                next_action=next_action,
            )
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.STATE_ENTERED,
                decision=decision,
                next_action=next_action,
                resume_action=next_action,
                resume_supported=False,
                can_continue_now=True,
                message="Fix-wiki pronto para continuar com curadoria semantica do vocabulario.",
                trigger=trigger,
            )
        case FixWikiReason.GRAPH_BLOCKED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="graph_validation",
                reason_code="graph_blockers",
                public_summary="A Wiki ainda tem bloqueios de grafo antes de concluir.",
                developer_summary="Graph validation found blockers after fix-wiki StateChart.",
                message="Fix-wiki bloqueado por problemas de grafo.",
                trigger=trigger,
            )
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="atomicity_split",
                reason_code="atomicity_split_required",
                public_summary="Ha split de atomicidade pendente antes de concluir.",
                developer_summary="Fix-wiki found pending atomicity split work that must run by the official route.",
                message="Fix-wiki bloqueado por split de atomicidade pendente.",
                trigger=trigger,
            )
        case FixWikiReason.RELATED_NOTES_BLOCKED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="related_notes",
                reason_code="related_notes_blocked",
                public_summary="As Notas Relacionadas ainda precisam ser atualizadas antes de concluir.",
                developer_summary="Fix-wiki could not close because Related Notes sync/export is still blocked.",
                message="Fix-wiki bloqueado por Notas Relacionadas pendentes.",
                trigger=trigger,
            )
        case FixWikiReason.LINKER_BLOCKED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="linker",
                reason_code="linker_blocked",
                public_summary="O pacote de links ainda esta bloqueado.",
                developer_summary="Fix-wiki could not complete because the linker package reported a blocker.",
                message="Fix-wiki bloqueado pelo pacote de links.",
                trigger=trigger,
            )
        case FixWikiReason.TAXONOMY_BLOCKED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="taxonomy",
                reason_code="taxonomy_blocked",
                public_summary="A taxonomia exige acao antes de concluir.",
                developer_summary="Fix-wiki found a taxonomy action/block before final health could close.",
                message="Fix-wiki bloqueado por acao de taxonomia.",
                trigger=trigger,
            )
        case FixWikiReason.VAULT_GUARD_REQUIRED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="vault_guard",
                reason_code="vault_guard_required",
                public_summary="A protecao do vault precisa ser aberta antes de alterar a Wiki.",
                developer_summary="Fix-wiki apply was blocked because the official vault guard was not active.",
                message="Fix-wiki bloqueado pela protecao do vault.",
                trigger=trigger,
            )
        case FixWikiReason.STYLE_REWRITE_REQUIRED:
            return _blocked_state_view(
                facts=facts,
                state=state,
                reason=reason,
                phase="style_rewrite",
                reason_code="style_rewrite_required",
                public_summary="Ha reescrita semantica pendente antes de concluir.",
                developer_summary="Fix-wiki found notes that require the official semantic rewrite route.",
                message="Fix-wiki bloqueado por reescrita semantica pendente.",
                trigger=trigger,
            )
        case FixWikiReason.FAILED:
            next_action = _default_next_action(facts, reason)
            reason_code = facts.failed_reason_code or (
                state.value if state != FixWikiState.FAILED else "fix_wiki_failed"
            )
            decision = WorkflowDecision(
                kind="failed",
                phase="failure",
                reason_code=reason_code,
                public_summary="O fix-wiki falhou antes de concluir a conferencia.",
                developer_summary="Fix-wiki emitted a failed StateChart state.",
                evidence=[
                    DecisionEvidence(
                        summary="A execucao informou falha operacional.",
                        technical_code=reason_code,
                        source="fix_wiki_fsm",
                    )
                ],
                next_action=next_action,
            )
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=WorkflowProgressEventType.WORKFLOW_FAILED,
                decision=decision,
                next_action=next_action,
                message="Fix-wiki falhou antes de concluir.",
                trigger=trigger,
            )
        case _:
            return _state_view(
                reason=reason,
                state=state,
                category=category,
                status=status,
                event_type=_default_event_type_for_status(status),
                decision=model.last_transition.decision if model.last_transition is not None else None,
                next_action=_default_next_action(facts, reason),
                resume_action=model.last_transition.resume_action if model.last_transition is not None else "",
                resume_supported=bool(model.last_transition and model.last_transition.resume_action),
                can_continue_now=status == WorkflowProgressStatus.WAITING_AGENT,
                message=_default_message_for_state(state),
                trigger=trigger,
            )


def _state_reason_from_model(model: WorkflowModel, state: FixWikiState) -> FixWikiReason:
    """Derive public reason from the canonical leaf state, not transition metadata."""

    return reason_for_state(state)


def _default_event_type_for_status(status: WorkflowProgressStatus) -> WorkflowProgressEventType:
    match status:
        case WorkflowProgressStatus.RUNNING | WorkflowProgressStatus.WAITING_AGENT:
            return WorkflowProgressEventType.STATE_ENTERED
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return WorkflowProgressEventType.EXTERNAL_WAIT_STARTED
        case WorkflowProgressStatus.WAITING_HUMAN | WorkflowProgressStatus.BLOCKED:
            return WorkflowProgressEventType.DECISION_EMITTED
        case WorkflowProgressStatus.FAILED:
            return WorkflowProgressEventType.WORKFLOW_FAILED
        case WorkflowProgressStatus.COMPLETED | WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return WorkflowProgressEventType.WORKFLOW_COMPLETED
    raise AssertionError(f"unsupported workflow progress status: {status}")


def _default_message_for_state(state: FixWikiState) -> str:
    phase = _PHASE_BY_STATE[state.value]
    return f"Fix-wiki em {phase}."


def _state_view(
    *,
    reason: FixWikiReason,
    state: FixWikiState,
    category: WorkflowStateCategory,
    status: WorkflowProgressStatus,
    event_type: WorkflowProgressEventType,
    message: str,
    trigger: str,
    decision: WorkflowDecision | None = None,
    next_action: str = "",
    resume_action: str = "",
    resume_supported: bool = False,
    can_continue_now: bool = False,
) -> _FixWikiStateView:
    return _FixWikiStateView(
        reason=reason,
        state=state,
        category=category,
        status=status,
        event_type=event_type,
        decision=decision,
        next_action=next_action,
        resume_action=resume_action,
        resume_supported=resume_supported,
        can_continue_now=can_continue_now,
        message=message,
        trigger=trigger,
    )


def _blocked_state_view(
    *,
    facts: FixWikiFsmFacts,
    state: FixWikiState,
    reason: FixWikiReason,
    phase: str,
    reason_code: str,
    public_summary: str,
    developer_summary: str,
    message: str,
    trigger: str,
    next_action: str | None = None,
    resume_action: str = "",
    resume_supported: bool = False,
) -> _FixWikiStateView:
    next_action = next_action if next_action is not None else _default_next_action(facts, reason)
    effective_resume_action = resume_action or next_action
    category = category_for_state(state)
    status = WorkflowProgressStatus(category.value)
    if category == WorkflowStateCategory.WAITING_HUMAN:
        decision = _ask_human_decision(
            phase=phase,
            reason_code=reason_code,
            public_summary=public_summary,
            developer_summary=developer_summary,
            next_action=next_action,
            required_inputs=_required_inputs_for_block(facts, reason),
        )
    else:
        decision = _hard_block_decision(
            phase=phase,
            reason_code=reason_code,
            public_summary=public_summary,
            developer_summary=developer_summary,
            next_action=next_action,
            required_inputs=_required_inputs_for_block(facts, reason),
        )
    return _state_view(
        reason=reason,
        state=state,
        category=category,
        status=status,
        event_type=WorkflowProgressEventType.DECISION_EMITTED,
        decision=decision,
        next_action=next_action,
        resume_action=effective_resume_action,
        resume_supported=resume_supported,
        message=message,
        trigger=trigger,
    )


def _external_wait_state(facts: FixWikiFsmFacts) -> FixWikiState:
    """Select the concrete waiting leaf from the canonical external-wait envelope."""

    return _external_wait_state_from_payload(facts.external_wait_payload)


def _external_wait_state_from_payload(payload: JsonObject) -> FixWikiState:
    """Map a waiting-external effect envelope to the concrete fix-wiki leaf."""

    child_payload = _optional_json_object_field(payload, "payload") or payload
    child = _FixWikiExternalWaitPayloadFields.model_validate(child_payload)
    child_state = child.state_machine_snapshot.current_state or child.progress_view_model.state
    if child.progress_view_model.status == "waiting_external" and child_state == "waiting_external_related_notes_quota":
        return FixWikiState.RELATED_NOTES_QUOTA_WAIT
    recovery = child.diagnostic_context.related_notes_recovery_state
    if _related_notes_recovery_waiting_external(recovery):
        return FixWikiState.RELATED_NOTES_QUOTA_WAIT
    effect = _FixWikiExternalWaitEffectFields.model_validate(
        _optional_json_object_subset(payload, "effect", ("origin_state",))
    )
    if effect.origin_state:
        try:
            state = FixWikiState(effect.origin_state)
        except ValueError:
            state = FixWikiState.STYLE_REWRITE_CAPACITY_WAIT
        if category_for_state(state) == WorkflowStateCategory.WAITING_EXTERNAL:
            return state
    return FixWikiState.STYLE_REWRITE_CAPACITY_WAIT


def _related_notes_recovery_waiting_external(recovery: JsonObject) -> bool:
    typed = RelatedNotesRecoveryState.from_payload(recovery)
    return (
        typed.status == "waiting_for_retry"
        and typed.resume_supported
        and typed.blocked_reason
        in {
            "related_notes_headless_quota_exhausted",
            "related_notes_headless_time_budget_exhausted",
        }
    )


def _hard_block_decision(
    *,
    phase: str,
    reason_code: str,
    public_summary: str,
    developer_summary: str,
    next_action: str,
    required_inputs: list[str] | None = None,
) -> WorkflowDecision:
    return WorkflowDecision(
        kind="hard_block",
        phase=phase,
        reason_code=reason_code,
        public_summary=public_summary,
        developer_summary=developer_summary,
        evidence=[
            DecisionEvidence(
                summary="O FSM classificou o resultado como bloqueado.",
                technical_code=reason_code,
                source="fix_wiki_fsm",
            )
        ],
        next_action=next_action,
        required_inputs=list(required_inputs or []),
    )


def _ask_human_decision(
    *,
    phase: str,
    reason_code: str,
    public_summary: str,
    developer_summary: str,
    next_action: str,
    required_inputs: list[str] | None = None,
) -> WorkflowDecision:
    return WorkflowDecision(
        kind="ask_human",
        phase=phase,
        reason_code=reason_code,
        public_summary=public_summary,
        developer_summary=developer_summary,
        evidence=[
            DecisionEvidence(
                summary="O FSM entrou em uma folha que exige escolha ou revisão humana.",
                technical_code=reason_code,
                source="fix_wiki_fsm",
            )
        ],
        next_action=next_action,
        required_inputs=list(required_inputs or []),
        resume_action=next_action,
        recommended_option_id="continue_official_route",
        human_decision_kind=reason_code,
        options=[
            HumanDecisionOption(
                id="continue_official_route",
                label="Continuar",
                description=next_action,
            ),
            HumanDecisionOption(
                id="stop_here",
                label="Parar",
                description="Encerrar este workflow sem aplicar a próxima etapa agora.",
            ),
        ],
        rejected_automations=[
            RejectedAutomation(
                kind="auto_fix",
                reason_code=reason_code,
                reason="A próxima ação depende de revisão ou escolha humana antes de mutar a Wiki.",
            ),
            RejectedAutomation(
                kind="auto_defer",
                reason_code=reason_code,
                reason="Adiar manteria a folha waiting_human aberta sem decisão registrada.",
            ),
            RejectedAutomation(
                kind="auto_plan",
                reason_code=reason_code,
                reason="O plano precisa da decisão humana para escolher a próxima rota segura.",
            ),
        ],
    )


def _projection_human_decision_packet(projection: _FixWikiStateView) -> JsonObject | None:
    if projection.status != WorkflowProgressStatus.WAITING_HUMAN:
        return None
    if projection.decision is None:
        return None
    return projection.decision.to_human_decision_packet()


def _decision_with_recovery_action(decision: WorkflowDecision, *, next_action: str) -> WorkflowDecision:
    """Revalidate a StateChart decision after adding artifact-specific recovery text."""

    updated = decision.model_copy(update={"next_action": next_action, "resume_action": next_action})
    return WorkflowDecision.model_validate(updated.to_payload())


def _decision_with_leaf_reason(decision: WorkflowDecision, *, reason_code: str, phase: str) -> WorkflowDecision:
    """Make the public decision reason follow the reached leaf, not runtime metadata."""

    human_kind = decision.human_decision_kind or decision.reason_code
    updated = decision.model_copy(update={"reason_code": reason_code, "phase": phase, "human_decision_kind": human_kind})
    return WorkflowDecision.model_validate(updated.to_payload())


def _required_inputs_for_block(facts: FixWikiFsmFacts, reason: FixWikiReason) -> list[str]:
    match reason:
        case (
            FixWikiReason.GRAPH_BLOCKED
            | FixWikiReason.LINKER_BLOCKED
            | FixWikiReason.RELATED_NOTES_BLOCKED
            | FixWikiReason.TAXONOMY_BLOCKED
            | FixWikiReason.VAULT_GUARD_REQUIRED
        ):
            return _clean_required_inputs(facts.required_inputs)
        case FixWikiReason.FAILED:
            error_fields = _FixWikiErrorRequiredInputs.model_validate(facts.error_context)
            return _clean_required_inputs(error_fields.required_inputs)
        case _:
            return []


def _clean_required_inputs(value: object) -> list[str]:
    if value is None:
        return []
    raw_items = JsonObjectAdapter.validate_python({"items": value})["items"]
    if not isinstance(raw_items, list):
        raise ValueError("required_inputs must be a list of strings")
    cleaned: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("required_inputs must contain only strings")
        text = item.strip()
        if text:
            cleaned.append(text)
    return cleaned


def _default_next_action(facts: FixWikiFsmFacts, reason: FixWikiReason) -> str:
    candidate = facts.next_action.strip()
    match reason:
        case FixWikiReason.ENVIRONMENT_PATHS_MISSING | FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING:
            return candidate or "setup:set-paths"
        case FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED:
            return candidate or "setup:bootstrap-python"
        case FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            return candidate or "Aguardar a cota externa e retomar pela rota oficial."
        case FixWikiReason.WAITING_EXTERNAL:
            return facts.external_wait_resume_action or "Aguardar a condicao externa e retomar pela rota oficial."
        case FixWikiReason.WAITING_HUMAN:
            return candidate or "Responder a decisao solicitada para continuar."
        case FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED:
            return "Executar a reescrita semantica oficial antes de concluir."
        case FixWikiReason.TAXONOMY_DECISION_REQUIRED:
            return "Resolver a acao de taxonomia pela rota oficial antes de concluir."
        case FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW:
            return "Revisar a avaliacao do vocabulario e retomar pela rota oficial."
        case FixWikiReason.GRAPH_REVIEW_REQUIRED:
            return candidate or "Revisar os bloqueios de grafo e retomar pela rota oficial."
        case FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED:
            return _atomicity_split_recovery_action(facts)
        case FixWikiReason.MERGE_REVIEW_REQUIRED:
            return "Revisar o merge de notas e retomar pela rota oficial."
        case FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID:
            return "Reemitir o plano de subagente pela rota oficial com atestacao valida."
        case FixWikiReason.STYLE_REWRITE_READY:
            return "Continuar pela reescrita especializada, aplicar a versao validada e repetir a conferencia da Wiki."
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            return candidate or "Executar a curadoria semantica do vocabulario e repetir a conferencia da Wiki."
        case FixWikiReason.GRAPH_BLOCKED:
            return candidate or "Executar /mednotes:link para reparar WikiLinks e grafo pela rota oficial."
        case FixWikiReason.LINK_RUN_REQUESTED:
            return candidate or "Executar /mednotes:link pela rota oficial antes de concluir o fix-wiki."
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED:
            return candidate or _atomicity_split_recovery_action(facts)
        case FixWikiReason.RELATED_NOTES_BLOCKED:
            return candidate or (
                "Conferir o export do Related Notes: o workflow não conseguiu provar que o export cobre esta Wiki; "
                "atualize as Notas Relacionadas e repita a conferência."
            )
        case FixWikiReason.LINKER_BLOCKED:
            return candidate or "Retomar o pacote de links pela rota oficial antes de concluir."
        case FixWikiReason.TAXONOMY_BLOCKED:
            return candidate or "Resolver a acao de taxonomia pela rota oficial antes de concluir."
        case FixWikiReason.VAULT_GUARD_REQUIRED:
            return candidate or "Abrir a protecao do vault pela rota oficial e repetir o apply."
        case FixWikiReason.STYLE_REWRITE_REQUIRED:
            return "Executar a reescrita semantica oficial antes de concluir."
        case FixWikiReason.FAILED:
            return candidate or "Revisar o erro e retomar pela rota oficial indicada."
        case FixWikiReason.COMPLETED_WITH_WARNINGS:
            return candidate or "Revisar os avisos pendentes quando possivel."
        case FixWikiReason.PREVIEW_READY | FixWikiReason.COMPLETED:
            return ""


def _atomicity_split_recovery_action(facts: FixWikiFsmFacts) -> str:
    """Render the official recovery route from the FSM artifact snapshot."""

    plan_path = _FixWikiArtifactPathFields.model_validate(facts.artifacts).atomicity_split_plan_path.strip()
    command = "apply-atomicity-split"
    if plan_path:
        return (
            f"Revisar {plan_path}, executar {command} para os bundles aprovados "
            "e repetir /mednotes:fix-wiki."
        )
    return f"Executar {command} para os bundles aprovados e repetir /mednotes:fix-wiki."


def _error_context(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> JsonObject:
    existing = JsonObjectAdapter.validate_python(facts.error_context or {})
    if projection.status not in {
        WorkflowProgressStatus.BLOCKED,
        WorkflowProgressStatus.FAILED,
        WorkflowProgressStatus.WAITING_HUMAN,
    }:
        return existing
    expected_reason = projection.decision.reason_code if projection.decision is not None else projection.reason.value
    if _existing_error_context_matches(existing, expected_reason, projection.next_action):
        return existing
    return build_error_context(
        phase=projection.decision.phase if projection.decision is not None else _progress_phase(facts, projection),
        blocked_reason=expected_reason,
        root_cause=expected_reason,
        affected_artifact=_affected_artifact_for_reason(projection.reason),
        error_summary=projection.message,
        suggested_fix=projection.next_action,
        next_action=projection.next_action,
        retry_scope=_retry_scope_for_reason(projection.reason),
        human_decision_required=projection.status == WorkflowProgressStatus.WAITING_HUMAN,
    )


def _existing_error_context_matches(context: JsonObject, expected_reason: str, expected_next_action: str) -> bool:
    if not context:
        return False
    fields = _FixWikiExistingErrorContextFields.model_validate(context)
    return expected_reason in {fields.blocked_reason, fields.root_cause} and fields.next_action == expected_next_action


def _affected_artifact_for_reason(reason: FixWikiReason) -> str:
    match reason:
        case FixWikiReason.ENVIRONMENT_PATHS_MISSING | FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING:
            return "workbench_paths_config"
        case FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED:
            return "python_environment"
        case (
            FixWikiReason.STYLE_REWRITE_REQUIRED
            | FixWikiReason.STYLE_REWRITE_READY
            | FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
        ):
            return "style_rewrite_plan"
        case FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID:
            return "subagent_plan"
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            return "vocabulary_semantic_repair"
        case FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW:
            return "vocabulary_eval_report"
        case FixWikiReason.RELATED_NOTES_BLOCKED | FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            return "related_notes_export"
        case FixWikiReason.LINKER_BLOCKED | FixWikiReason.GRAPH_BLOCKED | FixWikiReason.GRAPH_REVIEW_REQUIRED:
            return "linker_diagnosis"
        case FixWikiReason.TAXONOMY_BLOCKED | FixWikiReason.TAXONOMY_DECISION_REQUIRED:
            return "taxonomy_plan"
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED | FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED:
            return "atomicity_split_plan"
        case FixWikiReason.MERGE_REVIEW_REQUIRED:
            return "note_merge_plan"
        case _:
            return "fix_wiki_plan"


def _retry_scope_for_reason(reason: FixWikiReason) -> str:
    match reason:
        case (
            FixWikiReason.ENVIRONMENT_PATHS_MISSING
            | FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING
            | FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
        ):
            return "setup_then_rerun_fix_wiki"
        case (
            FixWikiReason.STYLE_REWRITE_REQUIRED
            | FixWikiReason.STYLE_REWRITE_READY
            | FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
        ):
            return "style_rewrite_official_route"
        case FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID:
            return "subagent_plan_attestation"
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            return "vocabulary_semantic_ingestion_then_rerun_fix_wiki"
        case FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW:
            return "vocabulary_eval_review"
        case FixWikiReason.GRAPH_REVIEW_REQUIRED:
            return "link_review_then_rerun_fix_wiki"
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED | FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED:
            return "atomicity_split_then_rerun_fix_wiki"
        case FixWikiReason.MERGE_REVIEW_REQUIRED:
            return "note_merge_review"
        case FixWikiReason.RELATED_NOTES_BLOCKED | FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            return "related_notes_then_rerun_fix_wiki"
        case FixWikiReason.LINKER_BLOCKED | FixWikiReason.GRAPH_BLOCKED:
            return "link_then_rerun_fix_wiki"
        case FixWikiReason.TAXONOMY_DECISION_REQUIRED:
            return "taxonomy_official_route"
        case _:
            return "fix_wiki_official_route"


def _related_notes_waiting_external(facts: FixWikiRuntimeFacts) -> bool:
    state = facts.related_notes_recovery_state
    return (
        facts.related_notes_blocked
        and state.status == "waiting_for_retry"
        and state.resume_supported
        and state.blocked_reason
        in {
            "related_notes_headless_quota_exhausted",
            "related_notes_headless_time_budget_exhausted",
        }
    )


def _specialist_model_waiting_agent(facts: FixWikiRuntimeFacts) -> bool:
    if facts.requires_llm_rewrite_count <= 0:
        return False
    for effect in facts.pending_effects:
        if effect.kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL:
            return True
    return False


def _related_notes_external_wait_message(facts: FixWikiFsmFacts) -> str:
    blocked_reason = facts.related_notes_recovery_state.blocked_reason
    if blocked_reason == "related_notes_headless_time_budget_exhausted":
        return "Related Notes pausou a indexação para evitar uma execução longa; a próxima tentativa retoma do índice parcial."
    if blocked_reason == "related_notes_headless_quota_exhausted":
        return "Related Notes aguardando cota externa para retomar pela rota oficial."
    return "Related Notes aguardando condição externa para retomar pela rota oficial."


def _progress_user_action(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> str:
    """Domain-owned user action text; kernel progress stays domain-agnostic."""

    if projection.status != WorkflowProgressStatus.WAITING_EXTERNAL:
        return ""
    if projection.reason == FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
        blocked_reason = facts.related_notes_recovery_state.blocked_reason
        if blocked_reason == "related_notes_headless_time_budget_exhausted":
            return (
                "A indexacao pausou para evitar uma execucao longa; "
                "o progresso foi preservado para retomar pela acao oficial."
            )
        if blocked_reason == "related_notes_headless_quota_exhausted":
            return "Aguarde a cota externa; o progresso foi preservado para retomar pela acao oficial."
    if (
        projection.reason == FixWikiReason.WAITING_EXTERNAL
        and facts.external_wait_reason_code == "specialist_model_capacity_unavailable"
    ):
        return "Aguarde o modelo especializado antes de retomar pela rota oficial."
    return ""


def _progress_state(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> WorkflowProgressState:
    current = _related_current(facts.related_notes_recovery_state)
    total = _related_total(facts.related_notes_recovery_state)
    remaining = _related_remaining(facts.related_notes_recovery_state, current=current, total=total)
    if total and current > total:
        current = max(0, total - remaining) if remaining else total
    if total == 0:
        current = max(0, facts.total_changed_count)
        total = max(current, facts.total_changed_count)
        remaining = max(0, total - current)
    counts = WorkflowProgressCounts(
        planned_items=total,
        processed_items=current,
        warnings=facts.warning_count,
        mutated_files=_applied_mutation_file_count(facts),
        written_files=_applied_written_file_count(facts),
        remaining_items=remaining,
        blocked_items=_blocked_item_count(facts),
    )
    return WorkflowProgressState(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=facts.run_id,
        state=projection.state.value,
        phase=_progress_phase(facts, projection),
        event_type=projection.event_type,
        message=projection.message,
        status=projection.status,
        current=current,
        total=total,
        counts=counts,
        resume_action=projection.resume_action,
        resume_supported=projection.resume_supported,
        can_continue_now=projection.can_continue_now,
        user_action=_progress_user_action(facts, projection),
        decision=projection.decision.decision_summary() if projection.decision is not None else None,
        technical_context={
            "reason": projection.reason.value,
            "trigger": projection.trigger,
            "related_notes_blocked_reason": facts.related_notes_recovery_state.blocked_reason,
        },
    )


def _fsm_directive_instructions(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> list[str]:
    common = [
        (
            "agent_instruction: use progress_view_model, state_machine_snapshot, receipt, "
            "reports.public_report.lines and reports.details.primary_objective_summary as source of truth."
        ),
        "agent_instruction: ignore diagnostic-only fields when they contradict FSM fields.",
    ]
    match projection.reason:
        case FixWikiReason.PREVIEW_READY:
            specific = [
                "agent_instruction: report that this was a preview and no vault files were changed.",
                "agent_instruction: do not run extra probes or repeat fix-wiki before the user requests apply.",
            ]
        case FixWikiReason.COMPLETED:
            specific = [
                "agent_instruction: write the final report now using reports.public_report.lines and reports.details.primary_objective_summary.",
                "agent_instruction: do not run additional diagnostics after completed status.",
            ]
        case FixWikiReason.COMPLETED_WITH_WARNINGS:
            specific = [
                "agent_instruction: write the final report now and explicitly include the remaining warnings.",
                "agent_instruction: do not describe completed_with_warnings as a clean completion.",
            ]
        case FixWikiReason.STYLE_REWRITE_READY:
            specific = [
                "agent_instruction: do not write the final report yet.",
                (
                    "agent_instruction: execute agent_directive.control.effects through the official "
                    "harness route before final report."
                ),
                (
                    "agent_instruction: do not repeat /mednotes:fix-wiki preview before the executable "
                    "effects are applied or blocked."
                ),
                "agent_instruction: do not invent specialist prompts or substitute packaged specialists.",
                "agent_instruction: do not edit receipts, manifests, attestations or specialist outputs by hand.",
                (
                    "agent_instruction: follow agent_directive.control.effects returned by each official apply; "
                    "only rerun /mednotes:fix-wiki when the style queue is empty."
                ),
            ]
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            specific = [
                "agent_instruction: do not write the final report yet.",
                (
                    "agent_instruction: execute agent_directive.control.effects through the official "
                    "harness route before final report."
                ),
                (
                    "agent_instruction: do not classify vocabulary semantic ingestion as linker failure; "
                    "this is an executable waiting_agent state."
                ),
                "agent_instruction: rerun /mednotes:fix-wiki only after the vocabulary effect completes or blocks.",
            ]
        case FixWikiReason.WAITING_EXTERNAL | FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            specific = [
                "agent_instruction: report the external wait and preserved progress; do not claim the Wiki is fixed.",
                "agent_instruction: do not manually call external APIs or regenerate external indexes outside the official route.",
            ]
            if projection.resume_action:
                specific.append(f"agent_instruction: resume only through resume_action: {projection.resume_action}.")
        case (
            FixWikiReason.WAITING_HUMAN
            | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED
            | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID
            | FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
            | FixWikiReason.TAXONOMY_DECISION_REQUIRED
            | FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW
            | FixWikiReason.GRAPH_REVIEW_REQUIRED
            | FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED
            | FixWikiReason.MERGE_REVIEW_REQUIRED
        ):
            specific = [
                "agent_instruction: present human_decision_packet options; do not choose on behalf of the user.",
                "agent_instruction: do not mutate the vault until the human decision is provided.",
            ]
        case (
            FixWikiReason.ENVIRONMENT_PATHS_MISSING
            | FixWikiReason.ENVIRONMENT_WIKI_DIR_MISSING
            | FixWikiReason.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED
        ):
            specific = [
                "agent_instruction: do not claim fix-wiki failed; this is a recoverable setup blocker.",
                "agent_instruction: execute only the typed /mednotes:setup effect before rerunning fix-wiki.",
                "agent_instruction: do not patch scripts or prompts as a workaround for environment setup.",
            ]
        case (
            FixWikiReason.GRAPH_BLOCKED
            | FixWikiReason.RELATED_NOTES_BLOCKED
            | FixWikiReason.LINKER_BLOCKED
            | FixWikiReason.TAXONOMY_BLOCKED
            | FixWikiReason.STYLE_REWRITE_REQUIRED
            | FixWikiReason.ATOMICITY_SPLIT_REQUIRED
        ):
            specific = [
                "agent_instruction: report the blocker and next action; do not treat tool success as workflow success.",
                "agent_instruction: do not mutate the vault or launch alternate commands outside the FSM next action.",
            ]
        case FixWikiReason.FAILED:
            specific = [
                "agent_instruction: report the failure root cause and next action; do not claim success.",
                "agent_instruction: do not retry with ad hoc commands outside the official route.",
            ]
        case _:
            specific = [
                "agent_instruction: follow the StateChart status and report only after the FSM reaches a terminal state.",
            ]
    if facts.pending_effects and projection.reason != FixWikiReason.STYLE_REWRITE_READY:
        specific.append("agent_instruction: pending_effects exist; do not ignore them in the final report.")
    return [*common, *specific]


def _progress_phase(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> str:
    if projection.reason == FixWikiReason.WAITING_EXTERNAL:
        if projection.state == FixWikiState.RELATED_NOTES_QUOTA_WAIT:
            return _PHASE_BY_STATE[projection.state.value]
        payload = facts.external_wait_payload
        effect = _FixWikiExternalWaitEffectFields.model_validate(
            _optional_json_object_subset(payload, "effect", ("origin_state",))
        )
        phase = effect.origin_state.strip()
        if phase:
            return _PHASE_BY_STATE.get(phase, phase)
        if facts.external_wait_reason_code == "specialist_model_capacity_unavailable":
            return "style_rewrite"
    if projection.reason == FixWikiReason.STYLE_REWRITE_READY:
        return "style_rewrite"
    return _PHASE_BY_STATE[projection.state.value]


def _snapshot_from_model(
    model: WorkflowModel,
    projection: _FixWikiStateView,
    progress_state: WorkflowProgressState,
) -> WorkflowStateMachineSnapshot:
    if model.state != projection.state.value:
        raise ValueError("FixWikiMachine state must match public projection state")
    progress_event = WorkflowProgressEvent(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=model.run_id,
        state=projection.state.value,
        phase=progress_state.phase,
        event_type=projection.event_type,
        message=projection.message,
        status=projection.status,
        current=progress_state.current,
        total=progress_state.total,
        counts=progress_state.counts,
        resume_action=projection.resume_action,
        resume_supported=projection.resume_supported,
        can_continue_now=projection.can_continue_now,
        decision=progress_state.decision,
        technical_context=progress_state.technical_context,
    )
    transitions: list[WorkflowTransition] = []
    for index, transition in enumerate(model.transition_log):
        progress_events = [progress_event] if index == len(model.transition_log) - 1 else []
        transitions.append(
            WorkflowTransition(
                workflow=transition.workflow,
                from_state=transition.from_state,
                to_state=transition.to_state,
                to_category=category_for_state(transition.to_state),
                trigger=transition.trigger,
                effects=list(transition.effects),
                progress_events=progress_events,
                decision=transition.decision,
                resume_action=transition.resume_action,
            )
        )
    return WorkflowStateMachineSnapshot(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=model.run_id,
        current_state=model.state,
        current_category=category_for_state(model.state),
        transitions=transitions,
        metadata={"reason": projection.reason.value, "source": "FixWikiMachine"},
    )


def _transition_effects(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> list[WorkflowEffect]:
    allowed_kinds = _allowed_effect_kinds_for_fix_wiki_state(
        category=projection.category,
        current_state=projection.state.value,
    )
    if not allowed_kinds:
        return []
    return [effect for effect in facts.machine_effects if effect.kind in allowed_kinds]


def _allowed_effect_kinds_for_fix_wiki_state(
    *,
    category: WorkflowStateCategory,
    current_state: str,
) -> set[WorkflowEffectKind]:
    """Return effect kinds executable from a concrete fix-wiki StateChart leaf."""

    allowed_kinds = _allowed_effect_kinds_for_category(category)
    if current_state in {
        FixWikiState.ENVIRONMENT_PATHS_MISSING.value,
        FixWikiState.ENVIRONMENT_WIKI_DIR_MISSING.value,
        FixWikiState.ENVIRONMENT_WINDOWS_PATH_OR_VENV_BLOCKED.value,
        FixWikiState.LINK_RUN_REQUESTED.value,
    }:
        allowed_kinds = allowed_kinds | {WorkflowEffectKind.RUN_SUBWORKFLOW}
    return allowed_kinds


def _allowed_effect_kinds_for_category(category: WorkflowStateCategory) -> set[WorkflowEffectKind]:
    match category:
        case WorkflowStateCategory.WAITING_AGENT:
            return {WorkflowEffectKind.CALL_SPECIALIST_MODEL, WorkflowEffectKind.RUN_SUBWORKFLOW}
        case WorkflowStateCategory.WAITING_EXTERNAL:
            return {WorkflowEffectKind.WAIT_EXTERNAL}
        case WorkflowStateCategory.WAITING_HUMAN:
            return {WorkflowEffectKind.ASK_HUMAN}
        case _:
            return set()


def _receipt(
    facts: FixWikiFsmFacts,
    projection: _FixWikiStateView,
    progress_state: WorkflowProgressState,
    snapshot: WorkflowStateMachineSnapshot,
    *,
    human_decision_packet: JsonObject | HumanDecisionPacket | None = None,
) -> WorkflowReceiptPayload:
    view_model = build_progress_view_model(progress_state)
    return WorkflowReceiptPayload(
        schema=FIX_WIKI_RECEIPT_SCHEMA,
        workflow=FIX_WIKI_WORKFLOW,
        run_id=facts.run_id,
        status=_receipt_status(projection),
        mutated=_applied_mutation_file_count(facts) > 0,
        next_action=_receipt_next_action(projection),
        human_decision_required=projection.status == WorkflowProgressStatus.WAITING_HUMAN,
        human_decision_packet=human_decision_packet,
        phase_outcomes=_receipt_phase_outcomes(projection, human_decision_packet=human_decision_packet),
        artifacts=_receipt_artifacts(facts.artifacts),
        changed_files=list(facts.changed_files),
        version_control_safety=facts.version_control_safety,
        progress_state=progress_state,
        progress_view_model=view_model,
        state_machine_snapshot=snapshot,
    )


def _receipt_phase_outcomes(
    projection: _FixWikiStateView,
    *,
    human_decision_packet: JsonObject | HumanDecisionPacket | None,
) -> list[WorkflowPhaseOutcome]:
    """Embed the FSM decision in the receipt instead of duplicating blocker fields."""

    if projection.decision is None:
        return []
    packet = None
    if human_decision_packet is not None:
        packet = HumanDecisionPacket.model_validate(human_decision_packet)
    return [
        WorkflowPhaseOutcome(
            phase=projection.decision.phase,
            decision_summary=projection.decision.decision_summary(),
            human_decision_packet=packet,
        )
    ]


def _applied_mutation_file_count(facts: FixWikiFsmFacts) -> int:
    if not facts.effective_apply:
        return 0
    if facts.vault_changed_file_count > 0:
        return facts.vault_changed_file_count
    if facts.total_changed_count > 0:
        return facts.total_changed_count
    if facts.written_count > 0:
        return facts.written_count
    return len(_non_backup_changed_files(facts.changed_files))


def _applied_written_file_count(facts: FixWikiFsmFacts) -> int:
    if not facts.effective_apply:
        return 0
    if facts.written_count > 0:
        return facts.written_count
    if facts.total_changed_count > 0:
        return facts.total_changed_count
    if facts.vault_changed_file_count > 0:
        return facts.vault_changed_file_count
    return len(_non_backup_changed_files(facts.changed_files))


def _non_backup_changed_files(paths: list[str]) -> list[str]:
    return [path for path in paths if path.strip() and not path.endswith(".bak")]


def _receipt_status(projection: _FixWikiStateView) -> ReceiptStatus:
    match projection.status:
        case WorkflowProgressStatus.COMPLETED:
            return "completed"
        case WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
            return "completed_with_warnings"
        case WorkflowProgressStatus.WAITING_AGENT:
            return "waiting_agent"
        case WorkflowProgressStatus.WAITING_EXTERNAL:
            return "waiting_external"
        case WorkflowProgressStatus.WAITING_HUMAN:
            return "waiting_human"
        case WorkflowProgressStatus.RUNNING:
            return "running"
        case WorkflowProgressStatus.BLOCKED:
            return "blocked"
        case WorkflowProgressStatus.FAILED:
            return "failed"
        case _:
            return "blocked"


def _receipt_next_action(projection: _FixWikiStateView) -> str:
    if projection.status == WorkflowProgressStatus.COMPLETED:
        return ""
    return projection.next_action


def _receipt_artifacts(artifacts: JsonObject) -> list[dict[str, str]]:
    receipt_artifacts: list[dict[str, str]] = []
    for key, value in artifacts.items():
        if value is None:
            continue
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if nested_value is None:
                    continue
                nested_path = str(nested_value).strip()
                if nested_path:
                    receipt_artifacts.append({"kind": f"{key}.{nested_key}", "path": nested_path})
            continue
        receipt_artifacts.append({"kind": str(key), "path": str(value)})
    return receipt_artifacts


def _reports(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> WorkflowReports:
    match projection.reason:
        case FixWikiReason.COMPLETED:
            summary = "Corrigi e conferi a Wiki."
        case FixWikiReason.COMPLETED_WITH_WARNINGS:
            summary = "Corrigi a Wiki com avisos pendentes."
        case FixWikiReason.PREVIEW_READY:
            summary = "Conferi a Wiki; nada foi alterado."
        case FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            summary = "Pausei a etapa do Related Notes porque ela depende de cota externa."
        case FixWikiReason.WAITING_EXTERNAL:
            if (
                facts.external_wait_reason_code == "specialist_model_capacity_unavailable"
                and facts.requires_llm_rewrite_count > 0
            ):
                rewrite_label = (
                    "reescrita especializada"
                    if facts.requires_llm_rewrite_count == 1
                    else "reescritas especializadas"
                )
                summary = (
                    "Apliquei os reparos seguros e pausei porque "
                    f"{facts.requires_llm_rewrite_count} {rewrite_label} dependem do modelo especializado."
                )
            else:
                summary = "Pausei o workflow porque ele depende de uma condicao externa."
        case FixWikiReason.STYLE_REWRITE_READY:
            summary = "Apliquei os reparos seguros e vou continuar pela reescrita especializada."
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            summary = "Apliquei os reparos seguros e vou continuar pela curadoria semantica do vocabulario."
        case (
            FixWikiReason.WAITING_HUMAN
            | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_REQUIRED
            | FixWikiReason.SUBAGENT_PLAN_ATTESTATION_INVALID
            | FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
            | FixWikiReason.TAXONOMY_DECISION_REQUIRED
            | FixWikiReason.VOCABULARY_EVAL_NEEDS_REVIEW
            | FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED
            | FixWikiReason.MERGE_REVIEW_REQUIRED
        ):
            summary = "Preciso de uma escolha sua antes de continuar."
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED:
            summary = "O fix-wiki parou porque ha split de atomicidade pendente."
        case FixWikiReason.GRAPH_BLOCKED:
            summary = "A Wiki ainda precisa de reparo de grafo pela rota oficial."
        case FixWikiReason.RELATED_NOTES_BLOCKED:
            summary = "O fix-wiki parou porque as Notas Relacionadas ainda precisam ser atualizadas."
        case FixWikiReason.LINKER_BLOCKED:
            summary = "O pacote de links ainda esta bloqueado."
        case FixWikiReason.TAXONOMY_BLOCKED:
            summary = "A taxonomia exige acao antes de concluir."
        case FixWikiReason.STYLE_REWRITE_REQUIRED:
            summary = "Ha reescrita semantica pendente antes de concluir."
        case FixWikiReason.FAILED:
            summary = "O fix-wiki falhou antes de concluir."
        case _:
            summary = projection.message
    return WorkflowReports(
        summary=summary,
        public_report=_public_report(facts, projection, summary),
    )


def _reports_with_primary_objective_summary(
    reports: WorkflowReports,
    *,
    run_id: str,
    progress_view_model: WorkflowProgressViewModel,
    receipt: WorkflowReceiptPayload,
    diagnostic_context: JsonObject,
) -> WorkflowReports:
    """Attach the typed validator summary under the shared reports envelope."""

    objective = fix_wiki_primary_objective_summary(
        JsonObjectAdapter.validate_python(
            {
                "schema": FIX_WIKI_SCHEMA,
                "workflow": FIX_WIKI_WORKFLOW,
                "run_id": run_id,
                "progress_view_model": progress_view_model.to_payload(),
                "receipt": receipt.to_payload(),
                "diagnostic_context": dict(diagnostic_context),
            }
        )
    )
    if objective is None:
        return reports
    details = dict(reports.details)
    details["primary_objective_summary"] = objective.to_payload()
    return reports.model_copy(update={"details": JsonObjectAdapter.validate_python(details)})


def _payload_with_primary_objective_summary(payload: JsonObject) -> JsonObject:
    """Refresh the structured validator summary on the final serialized payload."""

    objective = fix_wiki_primary_objective_summary(payload)
    if objective is None:
        return payload
    reports = JsonObjectAdapter.validate_python(payload["reports"] if "reports" in payload else {})
    details = JsonObjectAdapter.validate_python(reports["details"] if "details" in reports else {})
    details["primary_objective_summary"] = objective.to_payload()
    reports["details"] = details
    payload["reports"] = reports
    return payload


def _public_report(facts: FixWikiFsmFacts, projection: _FixWikiStateView, summary: str) -> WorkflowPublicReport:
    """Human UX projection owned by the same FSM result as the machine state."""

    changed_count = _applied_mutation_file_count(facts)
    vault_has_changes = changed_count > 0
    human_decision_required = (
        projection.status == WorkflowProgressStatus.WAITING_HUMAN
        or (projection.decision is not None and projection.decision.kind == "ask_human")
    )
    can_continue_without_human = projection.status == WorkflowProgressStatus.WAITING_AGENT and projection.can_continue_now

    if can_continue_without_human:
        headline = (
            "Apliquei reparos iniciais e vou continuar automaticamente."
            if vault_has_changes
            else "Preparei a próxima etapa e vou continuar automaticamente."
        )
    elif not facts.requested_apply and not vault_has_changes:
        headline = "Conferi a Wiki; nada foi alterado."
    elif vault_has_changes and projection.status == WorkflowProgressStatus.COMPLETED:
        headline = "Corrigi a Wiki."
    elif vault_has_changes and projection.status == WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
        headline = "Corrigi a Wiki; restaram avisos não bloqueantes."
    elif vault_has_changes:
        headline = "Apliquei reparos na Wiki, mas ainda falta concluir."
    else:
        headline = "Não alterei a Wiki."

    lines: list[str] = [headline]
    if vault_has_changes:
        lines.append(f"Alterei {changed_count} arquivo(s) da Wiki nesta etapa.")
    elif not facts.requested_apply:
        lines.append("Esta foi uma conferência: nenhum arquivo da Wiki foi alterado.")
    else:
        lines.append("Nenhum arquivo da Wiki foi alterado nesta etapa.")

    if can_continue_without_human:
        lines.append(_public_followup_line(facts, projection))

    blockers = _public_blockers(facts, projection, human_decision_required=human_decision_required)
    if blockers:
        lines.append("Ainda falta concluir; " + "; ".join(blockers) + ".")
    elif projection.status == WorkflowProgressStatus.COMPLETED_WITH_WARNINGS:
        lines.append("A Wiki ficou sem bloqueios técnicos, mas ainda há avisos para revisar.")
    elif projection.status == WorkflowProgressStatus.COMPLETED:
        lines.append("Não encontrei bloqueios técnicos restantes.")

    if human_decision_required:
        question = ""
        if projection.decision is not None and projection.decision.public_summary:
            question = projection.decision.public_summary.strip()
        if question:
            lines.append(f"Preciso da sua decisão: {question}")
        else:
            lines.append(_public_followup_line(facts, projection))
    elif projection.status == WorkflowProgressStatus.WAITING_EXTERNAL:
        lines.append(_waiting_external_public_line(facts, projection))
    elif not can_continue_without_human and projection.status not in {
        WorkflowProgressStatus.COMPLETED,
        WorkflowProgressStatus.COMPLETED_WITH_WARNINGS,
    }:
        followup_line = _public_followup_line(facts, projection)
        if followup_line:
            lines.append(followup_line)

    return WorkflowPublicReport(
        workflow=FIX_WIKI_WORKFLOW,
        run_id=facts.run_id,
        headline=headline,
        lines=lines,
    )


def _public_blockers(
    facts: FixWikiFsmFacts,
    projection: _FixWikiStateView,
    *,
    human_decision_required: bool,
) -> list[str]:
    blockers: list[str] = []
    match projection.reason:
        case (
            FixWikiReason.STYLE_REWRITE_READY
            | FixWikiReason.STYLE_REWRITE_REQUIRED
            | FixWikiReason.STYLE_REWRITE_REVIEW_REQUIRED
        ):
            blockers.append("há nota(s) que precisam de reescrita assistida antes de concluir")
        case FixWikiReason.VOCABULARY_SEMANTIC_INGESTION_PENDING:
            blockers.append("a curadoria semântica do vocabulário ainda precisa ser aplicada")
        case FixWikiReason.GRAPH_BLOCKED | FixWikiReason.GRAPH_REVIEW_REQUIRED:
            blockers.append("o grafo de links ainda tem referência(s) que precisam ser corrigidas")
        case FixWikiReason.RELATED_NOTES_BLOCKED | FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES:
            blockers.append("as Notas Relacionadas ainda precisam ser atualizadas")
        case FixWikiReason.LINKER_BLOCKED:
            blockers.append("a atualização de links ainda não pôde ser concluída")
        case FixWikiReason.TAXONOMY_BLOCKED | FixWikiReason.TAXONOMY_DECISION_REQUIRED:
            blockers.append("a organização por pastas ainda precisa de revisão")
        case FixWikiReason.ATOMICITY_SPLIT_REQUIRED | FixWikiReason.ATOMICITY_SPLIT_REVIEW_REQUIRED:
            blockers.append("há nota(s) que precisam ser divididas antes de concluir")
        case _:
            pass
    if human_decision_required:
        blockers.append("há uma decisão humana pendente antes de continuar")
    return blockers


def _waiting_external_public_line(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> str:
    if projection.reason == FixWikiReason.WAITING_EXTERNAL_RELATED_NOTES or facts.related_notes_recovery_state.status:
        return "As Notas Relacionadas dependem de quota externa antes de retomar com segurança."
    return _public_followup_line(facts, projection)


def _public_followup_line(facts: FixWikiFsmFacts, projection: _FixWikiStateView) -> str:
    """Render a safe next-step sentence without copying technical next_action text."""

    if projection.reason in {
        FixWikiReason.GRAPH_BLOCKED,
        FixWikiReason.GRAPH_REVIEW_REQUIRED,
        FixWikiReason.LINKER_BLOCKED,
    } and _graph_curator_followup(facts.next_action):
        return "Retomar a curadoria do grafo pela rota oficial antes de concluir."
    return public_progress_followup_line(_progress_state(facts, projection))


def _graph_curator_followup(next_action: str) -> bool:
    """Recognize graph-curator workflow hints only to choose closed public wording."""

    normalized = next_action.casefold()
    return any(marker in normalized for marker in ("med-link-graph-curator", "collect-curator-outputs", "curator-batch"))


def _public_report_summary_text(public_report: WorkflowPublicReport) -> str:
    """Use the public report lines as the single human-visible summary channel."""

    return public_report.summary_text()


def _diagnostic_context(
    facts: FixWikiFsmFacts,
    projection: _FixWikiStateView,
) -> JsonObject:
    """Build explanatory diagnostics without carrying executable control."""

    context: JsonObject = dict(facts.diagnostic_context)
    for key in FIX_WIKI_DIAGNOSTIC_PARALLEL_TRUTH_KEYS:
        if key in context:
            context.pop(key)
    apply_context = _optional_json_object_field(context, "apply")
    context.update(
        {
            "schema": "medical-notes-workbench.fix-wiki-fsm-diagnostic-context.v1",
            "reason": projection.reason.value,
            "state": projection.state.value,
            "apply": {
                **apply_context,
                "requested_apply": facts.requested_apply,
                "effective_apply": facts.effective_apply,
            },
            "counts": {
                "total_changed_count": facts.total_changed_count,
                "vault_changed_file_count": facts.vault_changed_file_count,
                "written_count": facts.written_count,
                "warning_count": facts.warning_count,
                "requires_llm_rewrite_count": facts.requires_llm_rewrite_count,
                "graph_error_count": facts.graph_error_count,
                "graph_blocker_count": facts.graph_blocker_count,
            },
            "final_validation": dict(facts.final_validation),
            "linker_blocked": facts.linker_blocked,
            "related_notes_blocked": facts.related_notes_blocked,
            "atomicity_split_required": facts.atomicity_split_required,
            "graph_review_required": facts.graph_review_required,
            "taxonomy_action_required": facts.taxonomy_action_required,
        }
    )
    if facts.failed_reason_code:
        context["root_cause"] = facts.failed_reason_code
    if facts.related_notes_recovery_state:
        context["related_notes_recovery_state"] = facts.related_notes_recovery_state.operation_payload
    if facts.external_wait_payload:
        context["external_wait_payload"] = dict(facts.external_wait_payload)
    return diagnostic_context_evidence_only(context)


def _agent_directive(
    facts: FixWikiFsmFacts,
    projection: _FixWikiStateView,
    *,
    progress_view_model: WorkflowProgressViewModel,
    user_visible_summary: str,
) -> JsonObject:
    """Build the root executable agent contract directly from FSM state."""

    typed = agent_directive_from_progress_view_model(
        progress_view_model,
        schema=MEDNOTES_AGENT_DIRECTIVE_SCHEMA,
        reason=projection.reason.value,
        effects=_transition_effects(facts, projection),
        blockers=_blocked_by_for_guidance(projection),
        resume=projection.resume_action,
        report_requires=["primary_objective", "mutations", "graph", "related_notes"],
        summary=user_visible_summary,
        instructions=_plain_agent_directive_instructions(_fsm_directive_instructions(facts, projection)),
    )
    return JsonObjectAdapter.validate_python(typed.to_payload())


def _problem_diagnostic_context(context: JsonObject, projection: _FixWikiStateView) -> JsonObject:
    vocabulary_bootstrap = _FixWikiVocabularyBootstrapDiagnostic.model_validate(
        context["vocabulary_bootstrap"]
        if "vocabulary_bootstrap" in context and isinstance(context["vocabulary_bootstrap"], dict)
        else {}
    )
    explicit_vocabulary_reset = vocabulary_bootstrap.trigger == "explicit_vocabulary_reset"
    if projection.status == WorkflowProgressStatus.COMPLETED:
        if explicit_vocabulary_reset:
            return JsonObjectAdapter.validate_python(context)
        return {}
    return JsonObjectAdapter.validate_python(context)


def _blocked_by_for_guidance(projection: _FixWikiStateView) -> list[str]:
    if projection.status not in {
        WorkflowProgressStatus.BLOCKED,
        WorkflowProgressStatus.FAILED,
        WorkflowProgressStatus.WAITING_EXTERNAL,
        WorkflowProgressStatus.WAITING_HUMAN,
    }:
        return []
    if projection.decision is not None:
        return [projection.decision.reason_code]
    return [projection.trigger or projection.reason.value]


def _plain_agent_directive_instructions(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        text = line.strip()
        prefix = "agent_instruction:"
        if text.casefold().startswith(prefix):
            text = text[len(prefix):].strip()
        if text:
            cleaned.append(text)
    return cleaned


def fix_wiki_cli_exit_code(payload: JsonObject) -> int:
    progress = _FixWikiPayloadProgressView.model_validate(
        _json_object_subset(payload, "progress_view_model", ("status",))
    )
    status = progress.status
    match status:
        case "completed" | "completed_with_warnings":
            return 0
        case "waiting_agent" | "waiting_external" | "waiting_human" | "blocked":
            return 3
        case "failed":
            return 5
        case _:
            return 1


def assert_fix_wiki_fsm_payload(payload: JsonObject) -> None:
    forbidden_root_keys = set(payload) & FIX_WIKI_FORBIDDEN_ROOT_KEYS
    if forbidden_root_keys:
        raise ValueError(f"fix-wiki FSM payload contains noncanonical root fields: {sorted(forbidden_root_keys)}")
    required_root_keys = FIX_WIKI_ALLOWED_ROOT_KEYS - {"diagnostic_context"}
    missing_keys = required_root_keys - set(payload)
    if missing_keys:
        raise ValueError(f"fix-wiki FSM payload missing canonical root fields: {sorted(missing_keys)}")
    unexpected_keys = set(payload) - FIX_WIKI_ALLOWED_ROOT_KEYS
    if unexpected_keys:
        raise ValueError(f"fix-wiki FSM payload contains unexpected root fields: {sorted(unexpected_keys)}")
    diagnostic_context = payload["diagnostic_context"] if "diagnostic_context" in payload else {}
    assert_diagnostic_context_evidence_only(diagnostic_context)
    fields = _fix_wiki_payload_fields(payload)
    reports_model = WorkflowReports.model_validate(payload["reports"])
    if fields.progress_view_model.status != fields.state_machine_snapshot.current_category:
        raise ValueError("fix-wiki FSM status must match state_machine_snapshot category")
    if fields.receipt.status != fields.progress_view_model.status:
        raise ValueError("fix-wiki FSM receipt status must match progress view status")
    snapshot = WorkflowStateMachineSnapshot.model_validate(payload["state_machine_snapshot"])
    progress_view_model = WorkflowProgressViewModel.model_validate(payload["progress_view_model"])
    assert_public_report_matches_progress(
        reports_model.public_report,
        workflow=FIX_WIKI_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        label="fix-wiki FSM",
    )
    _assert_fix_wiki_machine_snapshot(snapshot)
    _assert_fix_wiki_agent_directive_matches_snapshot(payload, snapshot, progress_view_model)
    diagnostic_context = payload["diagnostic_context"] if "diagnostic_context" in payload else None
    if isinstance(diagnostic_context, dict) and "agent_directive" in diagnostic_context:
        raise ValueError("fix-wiki FSM diagnostic_context must not contain agent_directive")
    if isinstance(diagnostic_context, dict):
        _assert_fix_wiki_diagnostic_context_is_non_operational(diagnostic_context)


def _assert_fix_wiki_diagnostic_context_is_non_operational(diagnostic_context: JsonObject) -> None:
    """Keep diagnostics as evidence only; executable work belongs in agent_directive."""

    for key in FIX_WIKI_DIAGNOSTIC_PARALLEL_TRUTH_KEYS:
        if key in diagnostic_context:
            raise ValueError(f"fix-wiki FSM diagnostic_context contains parallel truth field: {key}")
    for key in diagnostic_context:
        if str(key).startswith("human_decision"):
            raise ValueError(f"fix-wiki FSM diagnostic_context contains parallel truth field: {key}")
    for plan_key in ("orchestration_plan", "continuation_plan"):
        plan = diagnostic_context[plan_key] if plan_key in diagnostic_context else None
        if not isinstance(plan, dict):
            continue
        plan_payload = JsonObjectAdapter.validate_python(plan)
        operational_keys = sorted(set(plan_payload) & FIX_WIKI_DIAGNOSTIC_OPERATIONAL_PLAN_KEYS)
        if operational_keys:
            raise ValueError(
                f"fix-wiki FSM diagnostic_context.{plan_key} contains operational fields: {operational_keys}"
            )


def _assert_fix_wiki_machine_snapshot(snapshot: WorkflowStateMachineSnapshot) -> None:
    if snapshot.workflow != FIX_WIKI_WORKFLOW:
        raise ValueError("fix-wiki FSM snapshot has invalid workflow")
    if snapshot.current_category != category_for_state(snapshot.current_state):
        raise ValueError("fix-wiki FSM snapshot category does not match StateChart state")
    edges = _fix_wiki_machine_edges()
    for transition in snapshot.transitions:
        if transition.to_category != category_for_state(transition.to_state):
            raise ValueError("fix-wiki FSM transition category does not match StateChart state")
        for effect in transition.effects:
            if effect.origin_state != transition.to_state:
                raise ValueError("fix-wiki FSM transition effect origin_state must match transition target")
            if effect.kind == WorkflowEffectKind.ASK_HUMAN and transition.to_category != WorkflowStateCategory.WAITING_HUMAN:
                raise ValueError("fix-wiki ask_human effects are only allowed for waiting_human states")
            if effect.kind == WorkflowEffectKind.WAIT_EXTERNAL and transition.to_category != WorkflowStateCategory.WAITING_EXTERNAL:
                raise ValueError("fix-wiki wait_external effects are only allowed for waiting_external states")
            if (
                effect.kind == WorkflowEffectKind.CALL_SPECIALIST_MODEL
                and transition.to_category != WorkflowStateCategory.WAITING_AGENT
            ):
                raise ValueError("fix-wiki specialist effects are only allowed for waiting_agent states")
        edge = (transition.trigger, transition.from_state, transition.to_state)
        if edge not in edges:
            raise ValueError(f"unauthorized FSM transition: {edge}")


def _assert_fix_wiki_agent_directive_matches_snapshot(
    payload: JsonObject,
    snapshot: WorkflowStateMachineSnapshot,
    progress_view_model: WorkflowProgressViewModel,
) -> None:
    """Keep the public agent route anchored to the current StateChart leaf."""

    try:
        directive = AgentDirective.model_validate(_optional_json_object_field(payload, "agent_directive"))
    except PydanticValidationError as exc:
        raise ValueError("fix-wiki FSM payload invalid: agent_directive") from exc
    if directive.workflow != FIX_WIKI_WORKFLOW:
        raise ValueError("fix-wiki FSM agent_directive workflow must match workflow")
    if directive.schema_ != MEDNOTES_AGENT_DIRECTIVE_SCHEMA:
        raise ValueError("fix-wiki FSM agent_directive schema must match public MedNotes contract")
    category = WorkflowStateCategory(snapshot.current_category)
    allowed_effect_kinds = _allowed_effect_kinds_for_fix_wiki_state(
        category=category,
        current_state=snapshot.current_state,
    )
    assert_agent_directive_matches_progress(
        directive,
        workflow=FIX_WIKI_WORKFLOW,
        run_id=str(payload["run_id"]),
        progress_view_model=progress_view_model,
        snapshot=snapshot,
        allowed_effect_kinds=allowed_effect_kinds,
        label="fix-wiki FSM",
    )


def _fix_wiki_machine_edges() -> set[tuple[str, str, str]]:
    from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_machine import FixWikiMachine

    edges: set[tuple[str, str, str]] = set()
    for event in FixWikiMachine.events:
        for transition in event._transitions:
            for target in transition._targets:
                edges.add((event.id, str(transition.source.value), str(target.value)))
    return edges


def _fix_wiki_payload_fields(payload: JsonObject) -> _FixWikiPayloadFields:
    raw_fields: JsonObject = {
        "workflow": payload["workflow"],
        "progress_view_model": _json_object_subset(payload, "progress_view_model", ("status",)),
        "state_machine_snapshot": _json_object_subset(payload, "state_machine_snapshot", ("current_category",)),
        "receipt": _json_object_subset(payload, "receipt", ("status",)),
    }
    try:
        return _FixWikiPayloadFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        msg = str(first.get("msg") or str(exc))
        raise ValueError(f"fix-wiki FSM payload invalid: {loc}: {msg}") from exc


def _json_object_subset(payload: JsonObject, field_name: str, keys: tuple[str, ...]) -> JsonObject:
    try:
        source = JsonObjectAdapter.validate_python(payload[field_name])
    except PydanticValidationError as exc:
        raise ValueError(f"fix-wiki FSM payload invalid: {field_name} must be an object") from exc
    return {key: source[key] for key in keys if key in source}


def _optional_json_object_field(payload: JsonObject, field_name: str) -> JsonObject:
    if field_name not in payload:
        return {}
    try:
        return JsonObjectAdapter.validate_python(payload[field_name])
    except PydanticValidationError as exc:
        raise ValueError(f"fix-wiki FSM payload invalid: {field_name} must be an object") from exc


def _optional_json_object_subset(payload: JsonObject, field_name: str, keys: tuple[str, ...]) -> JsonObject:
    if field_name not in payload:
        return {}
    return _json_object_subset(payload, field_name, keys)


def _related_current(recovery_state: RelatedNotesRecoveryState) -> int:
    return recovery_state.fresh_record_count or recovery_state.partial_record_count


def _related_total(recovery_state: RelatedNotesRecoveryState) -> int:
    return recovery_state.total_note_count


def _related_remaining(recovery_state: RelatedNotesRecoveryState, *, current: int, total: int) -> int:
    remaining = recovery_state.remaining_count
    if remaining:
        return min(remaining, total) if total else remaining
    return max(0, total - current)


def _blocked_item_count(facts: FixWikiFsmFacts) -> int:
    return (
        facts.graph_blocker_count
        + int(facts.linker_blocked)
        + int(facts.related_notes_blocked)
        + int(facts.taxonomy_action_required)
        + int(facts.atomicity_split_required)
        + int(facts.graph_review_required)
        + facts.requires_llm_rewrite_count
    )
