from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mednotes.domains.wiki.contracts.note_plan import MeaningClaim, TriageNotePlan, TriageNotePlanItem
from mednotes.kernel.agent_directive import AgentDirective
from mednotes.kernel.base import ContractModel, JsonValue
from mednotes.kernel.guardrails import OperationalErrorContext
from mednotes.kernel.public_report import WorkflowReports
from mednotes.kernel.workflow import HumanDecisionPacket, WorkflowDecisionSummary

SubagentPhase = Literal[
    "triage",
    "architect",
    "style-rewrite",
    "note-merge",
    "atomicity-split",
    "vocabulary-curation",
    "vocabulary_curation",
    "atomicity_split",
]
SubagentName = Literal["med-chat-triager", "med-knowledge-architect", "med-link-graph-curator"]


class ExpectedOutputSchema(ContractModel):
    schema_: str = Field(alias="schema", pattern=r"^medical-notes-workbench\..+\.v[0-9]+$")
    description: str = Field(min_length=1)


class SubagentOutputContract(ContractModel):
    schema_: Literal["medical-notes-workbench.subagent-output-contract.v1"] = Field(alias="schema")
    write_markdown_to: str | None = None
    subagent_must_create_attestation: bool = False
    subagent_must_create_specialist_task_run_receipt: bool = False
    parent_must_not_fabricate_specialist_task_run_receipt: bool = False
    parent_may_call_specialist_task_receipt_finalizer: bool = False
    official_receipt_finalizers: list[str] = Field(default_factory=list)
    missing_specialist_task_run_receipt_action: str | None = None
    parent_only_fields: list[str] = Field(default_factory=list)
    runner_only_fields: list[str] = Field(default_factory=list)
    attestation_created_by: str | None = None


class SubagentContextDocs(ContractModel):
    schema_: Literal["medical-notes-workbench.subagent-context-docs.v1"] = Field(alias="schema")
    required_read_files: list[str] = Field(default_factory=list)
    forbidden_discovery_roots: list[str] = Field(default_factory=list)
    agent_instruction: str = ""


class SubagentPlanAttestation(ContractModel):
    schema_: Literal["medical-notes-workbench.subagent-plan-attestation.v1"] = Field(alias="schema")
    phase: SubagentPhase
    plan_schema: Literal[
        "medical-notes-workbench.subagent-plan.v1",
        "medical-notes-workbench.vocabulary-curator-batch-plan.v1",
        "medical-notes-workbench.atomicity-split-plan.v1",
    ]
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    attestation_kind: Literal["workbench_hmac_sha256.v1"]
    created_by: Literal["plan-subagents"]
    issued_at: str = Field(min_length=1)
    nonce: str = Field(min_length=16)
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")


class SubagentStyleIssue(ContractModel):
    """Style-audit issue copied into a work item as evidence, not state."""

    code: str = ""
    message: str = ""
    severity: str = ""
    line: int | None = None
    section: str | None = None
    suggested_visual: str | None = None
    reason: str | None = None


class SubagentPathCaseCheck(ContractModel):
    """Filesystem preflight result used by vocabulary-curator work items."""

    status: str = ""
    expected_path: str = ""
    actual_path: str = ""


class SubagentArtifactItem(ContractModel):
    kind: str = ""
    chat_id: str = ""
    source_url: str = ""
    manifest: str = ""
    file: str = ""
    sha256: str = ""
    turn_index: str = ""
    mime_type: str = ""
    caption: str = ""


class SubagentArtifactManifest(ContractModel):
    schema_: str = Field(default="", alias="schema")
    path: str = ""
    chat_id: str = ""
    source_url: str = ""
    saved_count: int = Field(default=0, ge=0)
    artifacts: list[SubagentArtifactItem] = Field(default_factory=list)


class SubagentPlanSource(ContractModel):
    raw_file: str = Field(min_length=1)
    note_plan_item_id: str = ""
    planned_title: str = ""
    fonte_id: str = ""
    work_id: str = ""
    titulo_triagem: str = ""
    note_plan_item: TriageNotePlanItem | None = None


class SubagentCanonicalMergePlan(ContractModel):
    schema_: Literal["medical-notes-workbench.canonical-merge-plan.v1"] = Field(alias="schema")
    target_kind: Literal["new_wiki_note", "existing_wiki_note"]
    target_title: str = Field(min_length=1)
    target_key: str = ""
    requested_title: str = ""
    target_path: str = ""
    existing_paths: list[str] = Field(default_factory=list)
    sources: list[SubagentPlanSource] = Field(default_factory=list)
    required_delta_per_source: bool = False
    required_multi_reference_provenance: bool = False


class SubagentPlannedMatch(ContractModel):
    raw_file: str = ""
    work_id: str = ""
    id: str = ""
    title: str = ""


class SubagentDuplicateTarget(ContractModel):
    id: str = ""
    title: str = ""
    target_key: str = Field(min_length=1)
    conflict_type: Literal["ambiguous_existing_wiki_note", "existing_wiki_note", "planned_in_batch"]
    existing_paths: list[str] = Field(default_factory=list)
    planned_matches: list[SubagentPlannedMatch] = Field(default_factory=list)


class SubagentPromptIdentitySource(ContractModel):
    path: str = Field(min_length=1)
    exists: bool = False
    sha256: str = ""
    byte_count: int = Field(default=0, ge=0)
    word_count: int = Field(default=0, ge=0)


class SubagentPromptIdentity(ContractModel):
    schema_: str = Field(default="", alias="schema")
    agent: SubagentName | str = ""
    aggregate_hash: str = ""
    sources: list[SubagentPromptIdentitySource] = Field(default_factory=list)


class SubagentDifficultyRoute(ContractModel):
    route: str = ""
    max_turns: int = Field(default=0, ge=0)
    focus: list[str] = Field(default_factory=list)
    efficiency_rule: str = ""


class SubagentCuratorQualityRubric(ContractModel):
    primary_meaning_atomicity: str = ""
    atomicity_signal: str = ""
    alias_precision: str = ""
    link_policy_conservatism: str = ""
    defer_when_uncertain: str = ""
    evidence_redaction: str = ""


class SubagentCuratorOutputContract(ContractModel):
    must_include: list[str] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)


class SubagentMaxTurnsPolicy(ContractModel):
    max_turns: int = Field(default=0, ge=0)
    on_exhaustion: str = ""


class SubagentErrorContext(ContractModel):
    """Actionable context passed to a subagent without requiring a full blocker."""

    phase: str = ""
    blocked_reason: str = ""
    root_cause: str = ""
    affected_artifact: str = ""
    error_summary: str = ""
    suggested_fix: str = ""
    next_action: str = ""
    retry_scope: str = ""
    human_decision_required: bool = False
    missing_inputs: list[str] = Field(default_factory=list)
    details: JsonValue = None


class SubagentAtomicitySchemaHint(ContractModel):
    title: str = ""
    target_path: str = ""
    content_path: str = ""


class SubagentSemanticSignal(ContractModel):
    """Atomicity evidence stays structured, but domain scoring is validated downstream."""

    score: float | int | None = None
    fragment_risk: str = ""
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    concepts: list[dict[str, JsonValue]] = Field(default_factory=list)
    audit: dict[str, JsonValue] | None = None


class SubagentBatchRef(ContractModel):
    batch: int = Field(ge=1)
    max_concurrency: int = Field(ge=0)
    item_count: int = Field(ge=0)
    owner_keys: list[str] = Field(default_factory=list)
    work_ids: list[str] = Field(default_factory=list)


class SubagentSourceAudit(ContractModel):
    schema_: str = Field(default="", alias="schema")
    wiki_dir: str = ""
    file_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)


class SubagentCandidateTarget(ContractModel):
    path: str = ""
    title: str = ""
    confidence: str = ""


class SubagentMeaningPlannerWorkItem(ContractModel):
    work_id: str = Field(min_length=1)
    action: str = ""
    target_kind: str = ""
    raw_file: str = ""
    note_plan_item_id: str = ""
    meaning_claim: MeaningClaim | None = None
    target_path: str = ""
    agent: SubagentName | str = ""
    item_type: str = ""
    launchable: bool = False
    owner_key: str = ""
    temp_dir: str = ""
    temp_output: str = ""


class SubagentDiagnosticContext(ContractModel):
    root_cause_code: str = ""
    error_context: SubagentErrorContext | OperationalErrorContext | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class SubagentBlockedItem(ContractModel):
    """Non-launchable item explaining why planning could not emit work."""

    work_id: str = Field(min_length=1)
    phase: SubagentPhase | None = None
    agent: SubagentName | None = None
    item_type: str = ""
    raw_file: str = ""
    owner_key: str = ""
    source_work_id: str = ""
    titulo_triagem: str = ""
    fonte_id: str = ""
    blocked_reason: str = Field(min_length=1)
    reason: str = ""
    next_action: str = ""
    launchable: Literal[False] = False
    write_policy: str = "no_temp_note"
    target_key: str = ""
    target_title: str = ""
    duplicate_targets: list[SubagentDuplicateTarget] = Field(default_factory=list)
    planned_matches: list[SubagentPlannedMatch] = Field(default_factory=list)
    canonical_merge: SubagentCanonicalMergePlan | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)
    note_plan_error: str = ""
    note_plan: TriageNotePlan | None = None
    note_plan_item_id: str = ""
    meaning_claim: MeaningClaim | None = None
    candidate_targets: list[SubagentCandidateTarget] = Field(default_factory=list)
    meaning_planner_work_items: list[SubagentMeaningPlannerWorkItem] = Field(default_factory=list)
    note_plan_item_count: int = Field(default=0, ge=0)
    note_plan_planned_meaning_count: int = Field(default=0, ge=0)
    note_plan_attach_count: int = Field(default=0, ge=0)
    note_plan_not_a_note_count: int = Field(default=0, ge=0)
    note_plan_needs_context_count: int = Field(default=0, ge=0)


class SubagentWorkItem(ContractModel):
    schema_: str | None = Field(default=None, alias="schema")
    work_id: str = Field(min_length=1)
    phase: SubagentPhase
    agent: SubagentName
    app: str | None = None
    workflow: str | None = None
    source_workflow: str | None = None
    action: str | None = None
    expected_output_schema: ExpectedOutputSchema
    subagent_output_contract: SubagentOutputContract | None = None
    item_type: str | None = None
    mode: str | None = None
    unit: str | None = None
    owner_key: str | None = None
    raw_file: str | None = None
    titulo_triagem: str | None = None
    fonte_id: str | None = None
    source_work_id: str | None = None
    temp_dir: str | None = None
    triager_output_path: str | None = None
    note_plan_path: str | None = None
    triager_eval_path: str | None = None
    meaning_claim: MeaningClaim | None = None
    note_plan_item_count: int = Field(default=0, ge=0)
    note_plan_planned_meaning_count: int = Field(default=0, ge=0)
    note_plan_attach_count: int = Field(default=0, ge=0)
    note_plan_not_a_note_count: int = Field(default=0, ge=0)
    note_plan_needs_context_count: int = Field(default=0, ge=0)
    note_path: str | None = None
    note_path_exists: bool | None = None
    path_case_check: SubagentPathCaseCheck | None = None
    target_path: str | None = None
    target_hash_before: str | None = None
    title: str | None = None
    rewrite_prompt: str | None = None
    model_policy: str | None = None
    required_model_tier: str | None = None
    preferred_model_tier: str | None = None
    context_docs: SubagentContextDocs | None = None
    errors: list[SubagentStyleIssue] = Field(default_factory=list)
    warnings: list[SubagentStyleIssue] = Field(default_factory=list)
    temp_output: str | None = None
    output_receipt_path: str | None = None
    output_attestation_path: str | None = None
    specialist_task_run_receipt_path: str | None = None
    merge_action: str | None = None
    target_kind: str | None = None
    target_title: str | None = None
    requested_title: str | None = None
    target_key: str | None = None
    existing_paths: list[str] = Field(default_factory=list)
    source_count: int | None = Field(default=None, ge=0)
    raw_files: list[str] = Field(default_factory=list)
    sources: list[SubagentPlanSource] = Field(default_factory=list)
    canonical_merge_plan: SubagentCanonicalMergePlan | None = None
    artifact_manifest_count: int = Field(default=0, ge=0)
    artifact_count: int = Field(default=0, ge=0)
    artifact_manifests: list[SubagentArtifactManifest] = Field(default_factory=list)
    apply_command: str | None = None
    blocked_reason: str | None = None
    duplicate_targets: list[SubagentDuplicateTarget] = Field(default_factory=list)
    planned_matches: list[SubagentPlannedMatch] = Field(default_factory=list)
    reason: str | None = None
    next_action: str | None = None
    canonical_merge: SubagentCanonicalMergePlan | None = None
    human_decision_packet: HumanDecisionPacket | None = None
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)
    launchable: bool | None = None
    write_policy: str | None = None
    note_plan_error: str | None = None
    note_plan: TriageNotePlan | None = None
    planned_title: str | None = None
    note_plan_item_id: str | None = None
    note_plan_item: TriageNotePlanItem | None = None
    meaning_planner_work_items: list[SubagentMeaningPlannerWorkItem] = Field(default_factory=list)
    artifact_manifest_error: str | None = None
    db_path: str | None = None
    content_hash: str | None = None
    queue_flags: list[str] = Field(default_factory=list)
    output_path: str | None = None
    prompt_identity: SubagentPromptIdentity | None = None
    difficulty_route: SubagentDifficultyRoute | None = None
    quality_rubric: SubagentCuratorQualityRubric | None = None
    output_contract: SubagentCuratorOutputContract | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    retry_scope: str | None = None
    max_turns_policy: SubagentMaxTurnsPolicy | None = None
    error_context: SubagentErrorContext | None = None
    instructions: list[str] = Field(default_factory=list)
    source_path: str | None = None
    source_hash: str | None = None
    bundle_output_path: str | None = None
    temp_markdown_dir: str | None = None
    semantic_signal: SubagentSemanticSignal | None = None
    atomicity_decision: str | None = None
    allowed_strategies: list[str] = Field(default_factory=list)
    required_bundle_fields: list[str] = Field(default_factory=list)
    replacement_source_schema: SubagentAtomicitySchemaHint | None = None
    created_notes_item_schema: SubagentAtomicitySchemaHint | None = None

    @model_validator(mode="after")
    def agent_matches_phase(self) -> SubagentWorkItem:
        expected = {
            "triage": "med-chat-triager",
            "architect": "med-knowledge-architect",
            "style-rewrite": "med-knowledge-architect",
            "note-merge": "med-knowledge-architect",
            "atomicity-split": "med-knowledge-architect",
            "atomicity_split": "med-knowledge-architect",
            "vocabulary-curation": "med-link-graph-curator",
            "vocabulary_curation": "med-link-graph-curator",
        }[self.phase]
        if self.agent != expected:
            raise ValueError("subagent agent must match phase")
        if self.phase == "style-rewrite":
            contract = self.subagent_output_contract
            if contract is None:
                raise ValueError("style-rewrite work items require subagent_output_contract")
            if contract.write_markdown_to != "temp_output":
                raise ValueError("style-rewrite subagent must write Markdown only to temp_output")
            if contract.subagent_must_create_attestation:
                raise ValueError("style-rewrite subagent must not create output attestation")
            if contract.subagent_must_create_specialist_task_run_receipt:
                raise ValueError("style-rewrite subagent must not create specialist task run receipt")
            if not contract.parent_must_not_fabricate_specialist_task_run_receipt:
                raise ValueError("style-rewrite parent must not fabricate specialist task run receipt")
            if not contract.parent_may_call_specialist_task_receipt_finalizer:
                raise ValueError("style-rewrite parent must call the official specialist receipt finalizer")
            if "finalize-agy-specialist-task" not in contract.official_receipt_finalizers:
                raise ValueError("style-rewrite AGY receipt finalizer must be declared")
            if "finalize-opencode-specialist-task" not in contract.official_receipt_finalizers:
                raise ValueError("style-rewrite OpenCode receipt finalizer must be declared")
            if contract.missing_specialist_task_run_receipt_action != "run_official_receipt_finalizer_or_stop":
                raise ValueError("style-rewrite missing specialist receipt action must run finalizer or stop")
            if "output_attestation_path" not in contract.parent_only_fields:
                raise ValueError("style-rewrite output_attestation_path must be parent-only")
            if "specialist_task_run_receipt_path" not in contract.runner_only_fields:
                raise ValueError("style-rewrite specialist_task_run_receipt_path must be runner-only")
            if contract.attestation_created_by != "finalize-style-rewrite-output":
                raise ValueError("style-rewrite attestation must be created by finalize-style-rewrite-output")
        return self


class SpecialistContinuationWorkItem(ContractModel):
    work_id: str = Field(min_length=1)
    phase: Literal["style-rewrite"] = "style-rewrite"
    agent: Literal["med-knowledge-architect"] = "med-knowledge-architect"
    item_type: str = ""
    target_path: str = Field(min_length=1)
    target_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    title: str = ""
    rewrite_prompt: str = Field(min_length=1)
    model_policy: str = "medical_specialist_authoring.v1"
    required_model_tier: str = Field(min_length=1)
    preferred_model_tier: str = ""
    temp_output: str = Field(min_length=1)
    specialist_task_run_receipt_path: str = Field(min_length=1)
    subagent_output_contract: SubagentOutputContract


class SubagentBatchPlan(ContractModel):
    schema_: Literal[
        "medical-notes-workbench.subagent-plan.v1",
        "medical-notes-workbench.vocabulary-curator-batch-plan.v1",
        "medical-notes-workbench.atomicity-split-plan.v1",
    ] = Field(alias="schema")
    workflow: str | None = None
    phase: SubagentPhase
    status: Literal["ready", "skipped", "blocked", "completed", "ready_with_blockers"]
    process_chats_terminal_state: Literal["no_pending"] | None = None
    skipped_reason: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    human_decision_packet: HumanDecisionPacket | None = None
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)
    decision_summary: WorkflowDecisionSummary | None = None
    diagnostic_context: SubagentDiagnosticContext | None = None
    reports: WorkflowReports | None = None
    agent: SubagentName | None = None
    mode: str | None = None
    item_type: str | None = None
    unit: str | None = None
    batch_id: str | None = None
    db_path: str | None = None
    source_fix_wiki_plan_path: str | None = None
    source_plan_hash: str | None = None
    source_snapshot_hash: str | None = None
    prompt_identity: SubagentPromptIdentity | None = None
    prompt_eval_report_path: str | None = None
    max_concurrency: int = Field(default=0, ge=0)
    item_count: int = Field(ge=0)
    total_available_count: int = Field(default=0, ge=0)
    blocked_item_count: int = Field(ge=0)
    blocked_items: list[SubagentBlockedItem] = Field(default_factory=list)
    canonical_merge_item_count: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)
    truncated: bool = False
    parallel_safe: bool = False
    launch_source: str | None = None
    batch_contract: str | None = None
    work_items: list[SubagentWorkItem] = Field(default_factory=list)
    batches: list[SubagentBatchRef] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    serial_after: list[str] = Field(default_factory=list)
    source_audit: SubagentSourceAudit | None = None
    parent_applies_outputs: bool = False
    canonical_parent_commands: list[str] = Field(default_factory=list)
    agent_directive: AgentDirective | None = None
    plan_attestation: SubagentPlanAttestation | None = None

    @model_validator(mode="after")
    def counts_and_parent_commands_must_match(self) -> SubagentBatchPlan:
        if self.item_count != len(self.work_items):
            raise ValueError("item_count must match work_items length")
        if self.status == "ready" and self.item_count == 0:
            raise ValueError("ready subagent plans require work_items")
        if self.parent_applies_outputs and not self.canonical_parent_commands:
            raise ValueError("parent_applies_outputs requires canonical_parent_commands")
        return self


class NextSpecialistTask(ContractModel):
    schema_: Literal["medical-notes-workbench.next-specialist-task.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    kind: Literal["call_specialist_model"] = "call_specialist_model"
    work_id: str = Field(min_length=1)
    agent: SubagentName
    title: str = ""
    execution_mode: Literal["parallel_authoring_serial_apply"] = "parallel_authoring_serial_apply"
    authoring_mode: Literal["parallel"] = "parallel"
    authoring_max_concurrency: int = Field(default=1, ge=1)
    apply_mode: Literal["serial"] = "serial"
    serial_apply_required: bool = True
    wait_for_all_authoring_outputs_before_apply: bool = True
    current_batch_items: list[SpecialistContinuationWorkItem] = Field(default_factory=list, max_length=1)
    agent_instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def specialist_work_item_is_actionable(self) -> NextSpecialistTask:
        if self.current_batch_items and self.current_batch_items[0].work_id != self.work_id:
            raise ValueError("next specialist task current_batch_items[0].work_id must match work_id")
        return self


class PlanOutputReceiptWorkItem(ContractModel):
    work_id: str = Field(min_length=1)
    phase: SubagentPhase
    agent: SubagentName
    title: str = ""
    item_type: str = ""


class PlanOutputReceipt(ContractModel):
    schema_: Literal["medical-notes-workbench.plan-output-receipt.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    phase: Literal["plan-subagents"]
    status: Literal["written", "blocked"]
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_path: str = Field(min_length=1)
    plan_schema: str = Field(min_length=1)
    plan_phase: SubagentPhase
    plan_attestation: SubagentPlanAttestation | None = None
    plan_hash: str = ""
    plan_status: str = Field(min_length=1)
    item_count: int = Field(ge=0)
    blocked_item_count: int = Field(ge=0)
    batch_id: str = ""
    current_batch_items: list[PlanOutputReceiptWorkItem] = Field(default_factory=list)
    agent_directive: AgentDirective | None = None
    error_context: OperationalErrorContext | None = None

    @model_validator(mode="after")
    def receipt_matches_status(self) -> PlanOutputReceipt:
        if self.status == "blocked":
            if not self.blocked_reason:
                raise ValueError("blocked plan output receipt requires blocked_reason")
            if not self.next_action:
                raise ValueError("blocked plan output receipt requires next_action")
        if (
            self.status == "written"
            and self.plan_status == "ready"
            and self.plan_phase == "style-rewrite"
            and self.current_batch_items
            and self.agent_directive is None
        ):
            raise ValueError("ready written plan output receipt requires agent_directive")
        return self
