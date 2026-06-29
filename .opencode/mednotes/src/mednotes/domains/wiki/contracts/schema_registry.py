from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from mednotes.domains.flashcards.contracts import FlashcardSourceManifest, FlashcardWritePlan
from mednotes.domains.flashcards.fsm import FlashcardsFsmResult
from mednotes.domains.history.history_fsm import HistoryFsmResult
from mednotes.domains.setup.setup_fsm import SetupFsmResult
from mednotes.domains.wiki.contracts.agent_report import (
    AgentRunReportFinding,
    AgentRunReportValidation,
    FixWikiPrimaryObjectiveSummary,
    ProcessChatsPrimaryObjectiveSummary,
)
from mednotes.domains.wiki.contracts.agents import (
    ExpectedOutputSchema,
    NextSpecialistTask,
    PlanOutputReceipt,
    PlanOutputReceiptWorkItem,
    SubagentBatchPlan,
    SubagentOutputContract,
    SubagentPlanAttestation,
    SubagentWorkItem,
)
from mednotes.domains.wiki.contracts.curator import (
    CuratorApplyReceipt,
    CuratorBatchPlan,
    CuratorManifest,
    CuratorPromptEvalReport,
    NoteSemanticIngestionOutput,
)
from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkSubworkflowEffectPayload,
    LinkWorkflowRunEffectPayload,
    RelatedNotesExportEffectPayload,
    RelatedNotesRecoveryEffectPayload,
    RelatedNotesRecoveryStateEffectPayload,
    RelatedNotesSyncEffectPayload,
    RelatedNotesSyncSectionEffectPayload,
    SpecialistModelEffectPayload,
    WaitExternalEffectPayload,
)
from mednotes.domains.wiki.contracts.happy_path import HappyPathRoundMetrics, HappyPathRunMetrics
from mednotes.domains.wiki.contracts.note_plan import TriageNotePlan
from mednotes.domains.wiki.contracts.paths import PathResolutionBlocker, PathResolutionResult, WorkbenchPathsConfig
from mednotes.domains.wiki.contracts.public_report import WorkflowPublicReportViewModel
from mednotes.domains.wiki.contracts.publish import PublishManifest, PublishReceipt
from mednotes.domains.wiki.contracts.raw_coverage import RawCoverage
from mednotes.domains.wiki.contracts.related_notes import RelatedNotesExport
from mednotes.domains.wiki.contracts.specialist import (
    SpecialistNextApplyStep,
    SpecialistTaskRunReceipt,
    SpecialistTaskRunReceiptAttestation,
)
from mednotes.domains.wiki.contracts.status import StatusSnapshot
from mednotes.domains.wiki.contracts.style_rewrite import (
    StyleRewriteApplyReceipt,
    StyleRewriteAtomicApplyAgentStdout,
    StyleRewriteHumanProgressCheckpoint,
    StyleRewriteManifest,
    StyleRewriteOutputAttestation,
    StyleRewriteOutputFinalization,
    StyleRewriteOutputReceipt,
)
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_fsm import FixWikiFsmFacts, FixWikiFsmResult
from mednotes.domains.wiki.flows.link.link_fsm import LinkFsmFacts, LinkFsmResult
from mednotes.domains.wiki.flows.link.related_notes_fsm import LinkRelatedFsmFacts, LinkRelatedFsmResult
from mednotes.domains.wiki.flows.process_chats.process_chats_fsm import (
    ProcessChatsFsmFacts,
    ProcessChatsFsmResult,
    ProcessChatsPublishOperationResult,
)
from mednotes.kernel.agent_directive import AgentDirective
from mednotes.kernel.base import ContractModel
from mednotes.kernel.blockers import BlockerEntryModel
from mednotes.kernel.effects import (
    WorkflowEffect,
    WorkflowEffectResult,
)
from mednotes.kernel.fsm_event import WorkflowEvent
from mednotes.kernel.fsm_model import WorkflowModel
from mednotes.kernel.fsm_transition_result import WorkflowTransitionResult
from mednotes.kernel.guardrails import OperationalErrorContext
from mednotes.kernel.progress import WorkflowProgressEvent, WorkflowProgressState, WorkflowProgressViewModel
from mednotes.kernel.public_report import WorkflowPrimaryObjectiveSummary, WorkflowPublicReport, WorkflowReports
from mednotes.kernel.state_machine import WorkflowStateMachineSnapshot, WorkflowTransition
from mednotes.kernel.workflow import (
    DecisionEvidence,
    RejectedAutomation,
    VersionControlSafety,
    WorkflowDecision,
    WorkflowPhaseOutcome,
    WorkflowPhaseReceipt,
    WorkflowReceiptPayload,
)
from mednotes.platform.feedback.contracts import ManualReportReceipt, TelemetryStatusSnapshot


@dataclass(frozen=True)
class ContractSchemaTarget:
    contract_id: str
    filename: str
    model: type[ContractModel]
    description: str


CONTRACT_SCHEMA_TARGETS: tuple[ContractSchemaTarget, ...] = (
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-decision.dev.v1",
        "workflow-decision.schema.json",
        WorkflowDecision,
        "Common workflow decision object, including automation evidence and human-decision packets.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-receipt-payload.dev.v1",
        "workflow-receipt-payload.schema.json",
        WorkflowReceiptPayload,
        "Common workflow receipt payload used by workflow receipt builders.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-phase-outcome.dev.v1",
        "workflow-phase-outcome.schema.json",
        WorkflowPhaseOutcome,
        "Typed phase outcome for multi-step workflow receipts.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-phase-receipt.dev.v1",
        "workflow-phase-receipt.schema.json",
        WorkflowPhaseReceipt,
        "Typed per-phase receipt summary.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.decision-evidence.dev.v1",
        "decision-evidence.schema.json",
        DecisionEvidence,
        "Evidence item required by workflow decisions.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.rejected-automation.dev.v1",
        "rejected-automation.schema.json",
        RejectedAutomation,
        "Evidence-backed rejected automation route.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.version-control-safety.dev.v1",
        "version-control-safety.schema.json",
        VersionControlSafety,
        "Common version-control safety projection for receipts.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-progress-event.dev.v1",
        "workflow-progress-event.schema.json",
        WorkflowProgressEvent,
        "Workflow progress event emitted during workflow orchestration.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-progress-state.dev.v1",
        "workflow-progress-state.schema.json",
        WorkflowProgressState,
        "Folded workflow progress state for receipt and UI projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-progress-view-model.dev.v1",
        "workflow-progress-view-model.schema.json",
        WorkflowProgressViewModel,
        "User-facing workflow progress view model derived from progress state.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.public-workflow-report.dev.v1",
        "public-workflow-report.schema.json",
        WorkflowPublicReport,
        "Human-visible text report embedded under reports.public_report in FSM-first workflows.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-reports.dev.v1",
        "workflow-reports.schema.json",
        WorkflowReports,
        "Shared reports envelope for FSM-first workflows; state lives outside this report.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-primary-objective-summary.dev.v1",
        "workflow-primary-objective-summary.schema.json",
        WorkflowPrimaryObjectiveSummary,
        "Minimum structured objective answer embedded in reports.details.primary_objective_summary.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-transition.dev.v1",
        "workflow-transition.schema.json",
        WorkflowTransition,
        "Workflow state-machine transition with effects and progress events.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-state-machine-snapshot.dev.v1",
        "workflow-state-machine-snapshot.schema.json",
        WorkflowStateMachineSnapshot,
        "Workflow state-machine snapshot embedded in receipts.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-event.dev.v1",
        "workflow-event.schema.json",
        WorkflowEvent,
        "Base typed event consumed by workflow StateChart callbacks.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-model.dev.v1",
        "workflow-model.schema.json",
        WorkflowModel,
        "Persisted workflow state plus event, transition and pending-effect logs.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-transition-result.dev.v1",
        "workflow-transition-result.schema.json",
        WorkflowTransitionResult,
        "Executable transition result returned by workflow StateChart callbacks.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.agent-directive.dev.v1",
        "agent-directive.schema.json",
        AgentDirective,
        "Canonical FSM-to-agent directive consumed by harnesses, validators and hooks.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-effect.dev.v1",
        "workflow-effect.schema.json",
        WorkflowEffect,
        "Executable effect emitted by a workflow FSM and materialized by the effect executor.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-effect-result.dev.v1",
        "workflow-effect-result.schema.json",
        WorkflowEffectResult,
        "Typed result returned after materializing one workflow effect.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-subworkflow-effect-payload.dev.v1",
        "link-subworkflow-effect-payload.schema.json",
        LinkSubworkflowEffectPayload,
        "Private typed payload consumed by the workflow effect executor after running /mednotes:link.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-workflow-run-effect-payload.dev.v1",
        "link-workflow-run-effect-payload.schema.json",
        LinkWorkflowRunEffectPayload,
        "Private typed intent emitted before running /mednotes:link.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-recovery-state-effect-payload.dev.v1",
        "related-notes-recovery-state-effect-payload.schema.json",
        RelatedNotesRecoveryStateEffectPayload,
        "Private typed Related Notes recovery progress embedded in effect results.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-recovery-effect-payload.dev.v1",
        "related-notes-recovery-effect-payload.schema.json",
        RelatedNotesRecoveryEffectPayload,
        "Private typed payload consumed by the workflow effect executor after recovering a Related Notes export.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-export-effect-payload.dev.v1",
        "related-notes-export-effect-payload.schema.json",
        RelatedNotesExportEffectPayload,
        "Private typed command payload emitted before recovering a Related Notes export.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-sync-effect-payload.dev.v1",
        "related-notes-sync-effect-payload.schema.json",
        RelatedNotesSyncEffectPayload,
        "Private typed payload consumed by the workflow effect executor after syncing the Related Notes section.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-sync-section-effect-payload.dev.v1",
        "related-notes-sync-section-effect-payload.schema.json",
        RelatedNotesSyncSectionEffectPayload,
        "Private typed command payload emitted before syncing the Related Notes section.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.wait-external-effect-payload.dev.v1",
        "wait-external-effect-payload.schema.json",
        WaitExternalEffectPayload,
        "Private typed payload consumed by the workflow effect executor for resumable external waits.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.specialist-model-effect-payload.dev.v1",
        "specialist-model-effect-payload.schema.json",
        SpecialistModelEffectPayload,
        "Private typed payload consumed by the workflow effect executor after calling a specialist model.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.specialist-task-run-receipt.dev.v1",
        "specialist-task-run-receipt.schema.json",
        SpecialistTaskRunReceipt,
        "Typed receipt proving a specialist model task ran through a supported harness.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.specialist-task-run-receipt-attestation.dev.v1",
        "specialist-task-run-receipt-attestation.schema.json",
        SpecialistTaskRunReceiptAttestation,
        "Workbench HMAC attestation proving a specialist task run receipt came from the official runner.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.agent-run-report-validation.dev.v1",
        "agent-run-report-validation.schema.json",
        AgentRunReportValidation,
        "Post-run validation report comparing an agent final report against workflow truth.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.agent-run-report-finding.dev.v1",
        "agent-run-report-finding.schema.json",
        AgentRunReportFinding,
        "Single final-report contract violation found after an agent-run validation.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.happy-path-run-metrics.dev.v1",
        "happy-path-run-metrics.schema.json",
        HappyPathRunMetrics,
        "Per-run happy-path metric produced by controlled experiments.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.happy-path-round-metrics.dev.v1",
        "happy-path-round-metrics.schema.json",
        HappyPathRoundMetrics,
        "Per-round happy-path prevalence metric for a controlled experiment batch.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workflow-public-report-view-model.dev.v1",
        "workflow-public-report-view-model.schema.json",
        WorkflowPublicReportViewModel,
        "Typed public report view model that tells the agent what the user-facing result must say.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.fix-wiki-primary-objective-summary.dev.v1",
        "fix-wiki-primary-objective-summary.schema.json",
        FixWikiPrimaryObjectiveSummary,
        "Derived answer to whether fix-wiki fixed the Wiki, what mutated, graph outcome, and Related Notes outcome.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.process-chats-primary-objective-summary.dev.v1",
        "process-chats-primary-objective-summary.schema.json",
        ProcessChatsPrimaryObjectiveSummary,
        "Derived answer to whether process-chats published notes, covered raws, wrote the Wiki, and ran linker.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.fix-wiki-fsm-facts.dev.v1",
        "fix-wiki-fsm-facts.schema.json",
        FixWikiFsmFacts,
        "Input facts consumed by the fix-wiki FSM projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.fix-wiki-fsm-result.dev.v1",
        "fix-wiki-fsm-result.schema.json",
        FixWikiFsmResult,
        "Canonical FSM-first result emitted by /mednotes:fix-wiki.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-fsm-facts.dev.v1",
        "link-fsm-facts.schema.json",
        LinkFsmFacts,
        "Input facts consumed by the /mednotes:link FSM projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-fsm-result.dev.v1",
        "link-fsm-result.schema.json",
        LinkFsmResult,
        "Canonical FSM-first result emitted by /mednotes:link.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.process-chats-publish-operation-result.dev.v1",
        "process-chats-publish-operation-result.schema.json",
        ProcessChatsPublishOperationResult,
        "Private typed publish operation result consumed by the /mednotes:process-chats FSM projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.process-chats-fsm-facts.dev.v1",
        "process-chats-fsm-facts.schema.json",
        ProcessChatsFsmFacts,
        "Input facts consumed by the /mednotes:process-chats FSM projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.process-chats-fsm-result.dev.v1",
        "process-chats-fsm-result.schema.json",
        ProcessChatsFsmResult,
        "Canonical FSM-first result emitted by /mednotes:process-chats.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-related-fsm-facts.dev.v1",
        "link-related-fsm-facts.schema.json",
        LinkRelatedFsmFacts,
        "Input facts consumed by the /mednotes:link-related FSM projection.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.link-related-fsm-result.dev.v1",
        "link-related-fsm-result.schema.json",
        LinkRelatedFsmResult,
        "Canonical FSM-first result emitted by /mednotes:link-related and Related Notes recovery.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.flashcards-fsm-result.dev.v1",
        "flashcards-fsm-result.schema.json",
        FlashcardsFsmResult,
        "Canonical FSM-first result emitted by /flashcards.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.setup-fsm-result.dev.v1",
        "setup-fsm-result.schema.json",
        SetupFsmResult,
        "Canonical FSM-first result emitted by /mednotes:setup.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.history-fsm-result.dev.v1",
        "history-fsm-result.schema.json",
        HistoryFsmResult,
        "Canonical FSM-first result emitted by /mednotes:history.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.status-snapshot.dev.v1",
        "status-snapshot.schema.json",
        StatusSnapshot,
        "Typed non-mutating /mednotes:status snapshot; not a workflow FSM.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.telemetry-status-snapshot.dev.v1",
        "telemetry-status-snapshot.schema.json",
        TelemetryStatusSnapshot,
        "Typed local telemetry adapter status while remote telemetry is disabled by project policy.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.manual-report-receipt.dev.v1",
        "manual-report-receipt.schema.json",
        ManualReportReceipt,
        "Typed receipt for explicit user-requested /report sending.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.flashcard-source-manifest.dev.v1",
        "flashcard-source-manifest.schema.json",
        FlashcardSourceManifest,
        "Source manifest consumed by the /flashcards workflow before candidate generation.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.flashcard-write-plan.dev.v1",
        "flashcard-write-plan.schema.json",
        FlashcardWritePlan,
        "Preview-first write plan consumed by /flashcards before creating Anki cards.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.blocker-entry.dev.v1",
        "blocker-entry.schema.json",
        BlockerEntryModel,
        "Registered blocker entry with default decision route.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.operational-error-context.dev.v1",
        "operational-error-context.schema.json",
        OperationalErrorContext,
        "Actionable error context used by agent retries and blockers.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.curator-batch-plan.dev.v1",
        "curator-batch-plan.schema.json",
        CuratorBatchPlan,
        "Curator batch plan consumed before collecting semantic ingestion outputs.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.curator-manifest.dev.v1",
        "curator-manifest.schema.json",
        CuratorManifest,
        "Manifest of collected curator outputs.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.curator-prompt-eval-report.dev.v1",
        "curator-prompt-eval-report.schema.json",
        CuratorPromptEvalReport,
        "Prompt evaluation report required before applying curator output.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.note-semantic-ingestion-output.dev.v1",
        "note-semantic-ingestion-output.schema.json",
        NoteSemanticIngestionOutput,
        "Semantic ingestion output emitted by med-link-graph-curator.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.curator-apply-receipt.dev.v1",
        "curator-apply-receipt.schema.json",
        CuratorApplyReceipt,
        "Curator batch apply receipt.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-manifest.dev.v1",
        "style-rewrite-manifest.schema.json",
        StyleRewriteManifest,
        "Manifest of collected specialist style rewrite outputs.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-output-receipt.dev.v1",
        "style-rewrite-output-receipt.schema.json",
        StyleRewriteOutputReceipt,
        "Receipt emitted by the specialist authoring route for one style rewrite output.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-output-attestation.dev.v1",
        "style-rewrite-output-attestation.schema.json",
        StyleRewriteOutputAttestation,
        "Workbench-signed attestation for one style rewrite output.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-output-finalization.dev.v1",
        "style-rewrite-output-finalization.schema.json",
        StyleRewriteOutputFinalization,
        "Finalization receipt emitted after validating and attesting one style rewrite output.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-apply-receipt.dev.v1",
        "style-rewrite-apply-receipt.schema.json",
        StyleRewriteApplyReceipt,
        "Receipt emitted when applying a style rewrite output.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-human-progress-checkpoint.dev.v1",
        "style-rewrite-human-progress-checkpoint.schema.json",
        StyleRewriteHumanProgressCheckpoint,
        "Human-readable progress checkpoint emitted after a specialist style rewrite apply.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.dev.v1",
        "style-rewrite-atomic-apply-agent-stdout.schema.json",
        StyleRewriteAtomicApplyAgentStdout,
        "Compact agent-facing stdout emitted after applying one specialist style rewrite.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.specialist-next-apply-step.dev.v1",
        "specialist-next-apply-step.schema.json",
        SpecialistNextApplyStep,
        "Typed instruction that tells an agent to apply a completed specialist receipt immediately.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.plan-output-receipt.dev.v1",
        "plan-output-receipt.schema.json",
        PlanOutputReceipt,
        "Compact receipt emitted when plan-subagents writes a plan to disk.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.next-specialist-task.dev.v1",
        "next-specialist-task.schema.json",
        NextSpecialistTask,
        "Actionable next specialist task emitted for agent continuation.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.plan-output-receipt-work-item.dev.v1",
        "plan-output-receipt-work-item.schema.json",
        PlanOutputReceiptWorkItem,
        "Compact work item summary embedded in plan-output-receipt.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.subagent-batch-plan.dev.v1",
        "subagent-batch-plan.schema.json",
        SubagentBatchPlan,
        "Subagent batch plan used by plan-subagents.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.subagent-plan-attestation.dev.v1",
        "subagent-plan-attestation.schema.json",
        SubagentPlanAttestation,
        "Workbench-signed attestation proving a subagent plan came from plan-subagents.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.subagent-work-item.dev.v1",
        "subagent-work-item.schema.json",
        SubagentWorkItem,
        "Single work item inside a subagent plan.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.subagent-output-contract.dev.v1",
        "subagent-output-contract.schema.json",
        SubagentOutputContract,
        "Machine-readable boundary between subagent output and parent finalization.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.expected-output-schema.dev.v1",
        "expected-output-schema.schema.json",
        ExpectedOutputSchema,
        "Expected output schema descriptor for subagent work.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.triage-note-plan.dev.v1",
        "triage-note-plan.schema.json",
        TriageNotePlan,
        "Triage note plan contract used by process-chats.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.raw-coverage.dev.v1",
        "raw-coverage.schema.json",
        RawCoverage,
        "Raw chat coverage contract used before publishing.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.publish-manifest.dev.v1",
        "publish-manifest.schema.json",
        PublishManifest,
        "Publish manifest contract for staged notes.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.publish-receipt.dev.v1",
        "publish-receipt.schema.json",
        PublishReceipt,
        "Publish dry-run/apply receipt contract.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.related-notes-export.dev.v1",
        "related-notes-export.schema.json",
        RelatedNotesExport,
        "Related Notes plugin export consumed by linker and fix-wiki.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.workbench-paths-config.dev.v1",
        "workbench-paths-config.schema.json",
        WorkbenchPathsConfig,
        "Workbench path configuration contract.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.path-resolution-result.dev.v1",
        "path-resolution-result.schema.json",
        PathResolutionResult,
        "Typed path resolution result.",
    ),
    ContractSchemaTarget(
        "medical-notes-workbench.path-resolution-blocker.dev.v1",
        "path-resolution-blocker.schema.json",
        PathResolutionBlocker,
        "Typed path resolution blocker.",
    ),
)


def iter_contract_schema_targets() -> Iterable[ContractSchemaTarget]:
    """Yield repo-owned schema targets in deterministic filename order."""

    return tuple(sorted(CONTRACT_SCHEMA_TARGETS, key=lambda target: target.filename))
