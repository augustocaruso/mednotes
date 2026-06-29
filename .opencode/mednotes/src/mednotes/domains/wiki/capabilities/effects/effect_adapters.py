from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.related_notes.related_notes import (
    recover_related_notes_export_operation_result,
    sync_related_notes_operation_result,
)
from mednotes.domains.wiki.capabilities.specialist.specialist_receipts import (
    validate_specialist_task_run_receipt_attestation,
)
from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkEffectBlockedOutcome,
    LinkEffectCompletedOutcome,
    LinkEffectFailedOutcome,
    LinkEffectGraphBlockedOutcome,
    LinkEffectLinkerBlockedOutcome,
    LinkSubworkflowEffectPayload,
    LinkWorkflowRunEffectPayload,
    RelatedNotesBlockedOutcome,
    RelatedNotesExportCompletedOutcome,
    RelatedNotesExportEffectPayload,
    RelatedNotesQuotaWaitOutcome,
    RelatedNotesRecoveryEffectPayload,
    RelatedNotesSyncCompletedOutcome,
    RelatedNotesSyncEffectPayload,
    RelatedNotesSyncSectionEffectPayload,
    RelatedNotesSyncWarningOutcome,
    SpecialistModelBlockedOutcome,
    SpecialistModelCapacityWaitOutcome,
    SpecialistModelCompletedOutcome,
    SpecialistModelEffectPayload,
    WaitExternalEffectOutcome,
    WaitExternalEffectPayload,
)
from mednotes.domains.wiki.flows.link.linking import run_linker
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.effect_executor import WorkflowEffectExecutionContext
from mednotes.kernel.effects import (
    WorkflowEffect,
    WorkflowEffectKind,
    WorkflowEffectResult,
    WorkflowEffectStatus,
)
from mednotes.kernel.workflow import VersionControlSafety

RELATED_NOTES_EXTERNAL_RETRY_REASONS = frozenset(
    {
        "related_notes_headless_quota_exhausted",
        "related_notes_headless_time_budget_exhausted",
    }
)


@dataclass
class WaitExternalEffectAdapter:
    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        del context
        if effect.kind != WorkflowEffectKind.WAIT_EXTERNAL:
            raise ValueError(f"WaitExternalEffectAdapter cannot run effect kind: {effect.kind.value}")
        try:
            typed = WaitExternalEffectPayload.from_effect_payload(effect.payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="wait_external_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(effect.payload),
            )
        recovery_state = typed.related_notes_recovery_state
        if recovery_state is not None:
            reason = recovery_state.blocked_reason or typed.blocked_reason or effect.target
            next_action = recovery_state.next_action or typed.next_action
        else:
            reason = typed.blocked_reason or typed.wait_target or effect.target or "workflow_external_wait"
            next_action = typed.next_action
        resume_action = effect.resume_action or next_action or "Aguardar a condição externa e retomar pela rota oficial."
        return WorkflowEffectResult(
            effect=effect,
            status=WorkflowEffectStatus.WAITING_EXTERNAL,
            outcome=WaitExternalEffectOutcome(reason_code=reason),
            public_summary="Workflow aguardando condição externa para continuar.",
            developer_summary=f"Workflow effect is waiting for external condition: {reason}.",
            payload=typed.to_payload(),
            next_action=resume_action,
            resume_action=resume_action,
            error_context={
                "phase": effect.origin_state,
                "blocked_reason": reason,
                "root_cause": reason,
                "affected_artifact": effect.target,
                "next_action": resume_action,
                "retry_scope": "wait_external_resume",
                "human_decision_required": False,
                "wait_target": typed.wait_target,
            },
        )


@dataclass
class RelatedNotesEffectAdapter:
    config: object
    recover_related_notes_export_fn: Callable[..., object] = recover_related_notes_export_operation_result
    sync_related_notes_fn: Callable[..., object] = sync_related_notes_operation_result

    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        del context
        if effect.kind != WorkflowEffectKind.RUN_SUBWORKFLOW:
            raise ValueError(f"RelatedNotesEffectAdapter cannot run effect kind: {effect.kind.value}")
        match effect.target:
            case "related_notes.export" | "related_notes_export":
                return self._recover_export(effect)
            case "related_notes.section":
                return self._sync_section(effect)
            case _:
                raise ValueError(f"RelatedNotesEffectAdapter cannot run effect target: {effect.target}")

    def _recover_export(self, effect: WorkflowEffect) -> WorkflowEffectResult:
        try:
            intent = RelatedNotesExportEffectPayload.from_effect_payload(effect.payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="related_notes_export_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(effect.payload),
            )
        result = self.recover_related_notes_export_fn(
            self.config,
            export_path=_optional_path(intent.export_path),
            mode=intent.mode or "auto",
            workflow=effect.workflow,
            run_id=effect.run_id,
        )
        try:
            operation_payload = _json_object(result)
            typed = RelatedNotesRecoveryEffectPayload.from_operation_payload(operation_payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="related_notes_recovery_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(result),
            )
        recovery_state = typed.related_notes_recovery_state
        reason = str(
            (recovery_state.blocked_reason if recovery_state is not None else "") or typed.blocked_reason or ""
        )
        next_action = str(typed.next_action or (recovery_state.next_action if recovery_state is not None else "") or "")
        if (
            recovery_state is not None
            and recovery_state.status == "waiting_for_retry"
            and reason in RELATED_NOTES_EXTERNAL_RETRY_REASONS
        ):
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.WAITING_EXTERNAL,
                outcome=RelatedNotesQuotaWaitOutcome(reason_code=reason),
                public_summary="Notas Relacionadas aguardando cota externa.",
                developer_summary="Related Notes recovery paused with reusable progress.",
                payload=typed.to_payload(),
                next_action=next_action,
                resume_action=next_action or effect.resume_action,
            )
        if typed.status in {"recovered", "completed"}:
            if effect.requires_receipt and typed.receipt is None:
                return _effect_payload_contract_block_from_message(
                    effect,
                    payload_schema="related_notes_recovery_effect_payload",
                    loc="receipt",
                    message="completed related notes recovery effect payload requires receipt",
                    operation_payload=typed.to_payload(),
                )
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.COMPLETED,
                outcome=RelatedNotesExportCompletedOutcome(),
                public_summary="Export de Notas Relacionadas atualizado.",
                developer_summary="Related Notes export recovery completed.",
                payload=typed.to_payload(),
                receipt=typed.receipt if effect.requires_receipt else None,
            )
        return WorkflowEffectResult(
            effect=effect,
            status=WorkflowEffectStatus.BLOCKED,
            outcome=RelatedNotesBlockedOutcome(reason_code=reason or "related_notes_blocked"),
            public_summary="Notas Relacionadas bloqueadas antes de alterar a Wiki.",
            developer_summary="Related Notes recovery returned a blocking status.",
            payload=typed.to_payload(),
            next_action=next_action or "Corrigir o bloqueio de Related Notes pela rota oficial.",
            error_context={"blocked_reason": reason or "related_notes_blocked"},
        )

    def _sync_section(self, effect: WorkflowEffect) -> WorkflowEffectResult:
        try:
            intent = RelatedNotesSyncSectionEffectPayload.from_effect_payload(effect.payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="related_notes_sync_section_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(effect.payload),
            )
        result = self.sync_related_notes_fn(
            self.config,
            export_path=_optional_path(intent.export_path),
            apply=intent.apply,
            # Markdown .bak files are retired; vault guard/version control is
            # the recovery mechanism for workflow-driven mutations.
            backup=False,
            receipt_path=_optional_path(intent.receipt_path),
            min_score=float(intent.min_score),
            max_links=intent.max_links,
            max_age_hours=float(intent.max_age_hours),
        )
        try:
            operation_payload = _json_object(result)
            typed = RelatedNotesSyncEffectPayload.from_operation_payload(operation_payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="related_notes_sync_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(result),
            )
        if typed.status == "completed":
            safety = _version_control_safety_or_block(
                effect,
                typed.to_payload(),
                changed_file_count=typed.applied_note_count,
                outcome=RelatedNotesBlockedOutcome(reason_code="version_control_safety_evidence_missing"),
            )
            if isinstance(safety, WorkflowEffectResult):
                return safety
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.COMPLETED,
                outcome=RelatedNotesSyncCompletedOutcome(),
                public_summary="Notas Relacionadas sincronizadas.",
                developer_summary="Related Notes sync completed.",
                payload=typed.to_payload(),
                receipt=typed.receipt,
            )
        if typed.status in {"preview_ready", "completed_with_warnings"}:
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.COMPLETED_WITH_WARNINGS,
                outcome=RelatedNotesSyncWarningOutcome(reason_code=str(typed.blocked_reason or typed.status)),
                public_summary="Notas Relacionadas conferidas com aviso.",
                developer_summary="Related Notes sync returned a non-mutating or warning result.",
                payload=typed.to_payload(),
                receipt=typed.receipt,
                next_action=str(typed.next_action or "Revisar a previa de Notas Relacionadas."),
            )
        return WorkflowEffectResult(
            effect=effect,
            status=WorkflowEffectStatus.BLOCKED,
            outcome=RelatedNotesBlockedOutcome(reason_code=str(typed.blocked_reason or "related_notes_blocked")),
            public_summary="Notas Relacionadas nao foram sincronizadas.",
            developer_summary="Related Notes sync returned blocked status.",
            payload=typed.to_payload(),
            next_action=str(typed.next_action or "Atualizar o export do Related Notes e repetir a rota oficial."),
            error_context={"blocked_reason": str(typed.blocked_reason or "related_notes_blocked")},
        )


def _optional_path(value: object) -> Path | None:
    text = _text_or_empty(value).strip()
    return Path(text) if text else None


def _version_control_safety_or_block(
    effect: WorkflowEffect,
    operation_payload: JsonObject,
    *,
    changed_file_count: int,
    outcome: ContractModel,
) -> VersionControlSafety | WorkflowEffectResult:
    """Accept only real guard evidence or explicit non-mutation contracts.

    Adapters must not fabricate safety from `mutates_resources`, apply mode or
    counters. A mutation with changed files needs safety copied from an
    operation payload or receipt produced by the guarded runtime.
    """

    safety = _version_control_safety_from_payload(operation_payload)
    if safety is None:
        if changed_file_count > 0 or effect.mutates_resources:
            return _version_control_safety_evidence_missing(
                effect,
                operation_payload=operation_payload,
                outcome=outcome,
            )
        return _no_resource_mutation_safety()
    if safety.no_resource_mutation and changed_file_count > 0:
        return _version_control_safety_evidence_missing(
            effect,
            operation_payload=operation_payload,
            outcome=outcome,
        )
    if safety.changed_file_count != changed_file_count:
        return _version_control_safety_evidence_mismatch(
            effect,
            operation_payload=operation_payload,
            outcome=outcome,
            expected_changed_file_count=changed_file_count,
            evidence_changed_file_count=safety.changed_file_count,
        )
    return safety


def _version_control_safety_from_payload(payload: JsonObject) -> VersionControlSafety | None:
    for candidate in (
        _json_field(payload, "version_control_safety"),
        _json_field(_json_object_or_empty(_json_field(payload, "receipt")), "version_control_safety"),
        _json_field(_json_object_or_empty(_json_field(payload, "guard_receipt")), "version_control_safety"),
    ):
        if isinstance(candidate, dict):
            return VersionControlSafety.model_validate(candidate)
    return None


def _version_control_safety_evidence_missing(
    effect: WorkflowEffect,
    *,
    operation_payload: JsonObject,
    outcome: ContractModel,
) -> WorkflowEffectResult:
    return WorkflowEffectResult(
        effect=effect,
        status=WorkflowEffectStatus.BLOCKED,
        outcome=outcome,
        public_summary="A alteração foi bloqueada porque faltou comprovante de proteção do recurso.",
        developer_summary="Mutating effect result did not carry typed version_control_safety evidence.",
        payload={
            "schema": "medical-notes-workbench.version-control-safety-error.v1",
            "operation_payload": operation_payload,
        },
        next_action="Reexecutar pela rota oficial com guard/receipt de versionamento ativo.",
        error_context={
            "root_cause": "version_control_safety_evidence_missing",
            "affected_artifact": effect.target,
            "retry_scope": "effect_adapter",
        },
    )


def _version_control_safety_evidence_mismatch(
    effect: WorkflowEffect,
    *,
    operation_payload: JsonObject,
    outcome: ContractModel,
    expected_changed_file_count: int,
    evidence_changed_file_count: int,
) -> WorkflowEffectResult:
    return WorkflowEffectResult(
        effect=effect,
        status=WorkflowEffectStatus.BLOCKED,
        outcome=outcome,
        public_summary="A alteração foi bloqueada porque o comprovante de proteção não bate com o resultado.",
        developer_summary="version_control_safety changed_file_count did not match the mutating effect result.",
        payload={
            "schema": "medical-notes-workbench.version-control-safety-error.v1",
            "operation_payload": operation_payload,
            "expected_changed_file_count": expected_changed_file_count,
            "evidence_changed_file_count": evidence_changed_file_count,
        },
        next_action="Reexecutar pela rota oficial; se o mismatch repetir, tratar como bug de contrato do adapter.",
        error_context={
            "root_cause": "version_control_safety_evidence_mismatch",
            "affected_artifact": effect.target,
            "retry_scope": "effect_adapter",
            "expected_changed_file_count": expected_changed_file_count,
            "evidence_changed_file_count": evidence_changed_file_count,
        },
    )


def _no_resource_mutation_safety() -> VersionControlSafety:
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


def _link_changed_file_count(payload: JsonObject) -> int:
    files_changed = _int_field(payload, "files_changed", 0)
    changed_files = _json_field(payload, "changed_files", [])
    if isinstance(changed_files, list):
        return max(files_changed, len(changed_files))
    return files_changed


def _child_effect_version_control_safety(
    parent_safety: VersionControlSafety,
    *,
    changed_file_count: int,
    mutates: bool,
) -> VersionControlSafety:
    """Project an already-active parent guard onto one child effect result.

    The child runtime owns the mutation count; the parent safety owns evidence
    that a rollback route/guard exists. This avoids accepting legacy receipts
    while also avoiding a false mismatch when the pre-link guard was captured
    before the link effect mutated files.
    """

    mutated = mutates or changed_file_count > 0
    payload = parent_safety.to_payload()
    payload["changed_file_count"] = changed_file_count
    payload["no_resource_mutation"] = not mutated
    payload["mutation_without_guard"] = bool(mutated and not parent_safety.rollback_declared)
    return VersionControlSafety.model_validate(payload)


def _json_object_or_empty(value: JsonValue) -> JsonObject:
    return _json_object(value) if isinstance(value, dict) else {}


@dataclass
class LinkWorkflowEffectAdapter:
    config: object
    run_linker_fn: Callable[..., object] = run_linker

    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        del context
        if effect.kind != WorkflowEffectKind.RUN_SUBWORKFLOW or effect.target not in {
            "/mednotes:link",
            "/mednotes:link-body",
        }:
            raise ValueError(f"LinkWorkflowEffectAdapter cannot run effect: {effect.kind.value}:{effect.target}")
        try:
            intent = LinkWorkflowRunEffectPayload.from_effect_payload(effect.payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="link_workflow_run_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(effect.payload),
            )
        if intent.apply and not intent.diagnosis_path.strip():
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="link_workflow_run_effect_payload",
                loc="diagnosis_path",
                message="link workflow apply effects require diagnosis_path",
                operation_payload=_safe_operation_payload(effect.payload),
            )
        mode: Literal["apply", "diagnose"] = "apply" if intent.apply else "diagnose"
        include_related_notes = effect.target != "/mednotes:link-body" and not intent.no_related_notes
        parent_safety = intent.version_control_safety
        raw = self.run_linker_fn(
            self.config,
            diagnose=intent.diagnose,
            apply=intent.apply,
            diagnosis_path=_optional_path(intent.diagnosis_path),
            receipt_path=_optional_path(intent.receipt_path),
            trigger_context_path=_optional_path(intent.trigger_context_path),
            include_related_notes=include_related_notes,
            # The FSM effect contract no longer carries adjacent-backup policy.
            backup=False,
            force_diagnose=intent.force_diagnose,
            llm_disambiguation=intent.llm_disambiguation or "auto",
            llm_model=_optional_text(intent.llm_model),
            llm_timeout=intent.llm_timeout,
            version_control_guard_active=parent_safety is not None,
        )
        try:
            operation_payload = _json_object(raw)
            changed_file_count = _link_changed_file_count(operation_payload)
            if parent_safety is not None and _json_field(operation_payload, "version_control_safety") == {}:
                enriched = dict(operation_payload)
                enriched["version_control_safety"] = _child_effect_version_control_safety(
                    parent_safety,
                    changed_file_count=changed_file_count,
                    mutates=effect.mutates_resources,
                ).to_payload()
                operation_payload = JsonObjectAdapter.validate_python(enriched)
            if parent_safety is not None and _version_control_safety_from_payload(operation_payload) is None:
                enriched = dict(operation_payload)
                enriched["version_control_safety"] = _child_effect_version_control_safety(
                    parent_safety,
                    changed_file_count=changed_file_count,
                    mutates=effect.mutates_resources,
                ).to_payload()
                operation_payload = JsonObjectAdapter.validate_python(enriched)
            safety = _version_control_safety_or_block(
                effect,
                operation_payload,
                changed_file_count=changed_file_count,
                outcome=LinkEffectBlockedOutcome(reason_code="version_control_safety_evidence_missing"),
            )
            if isinstance(safety, WorkflowEffectResult):
                return safety
            fsm_payload = _link_fsm_payload_from_raw(
                effect=effect,
                raw=operation_payload,
                mode=mode,
                include_related_notes=include_related_notes,
                version_control_safety=safety,
            )
            fsm_payload = _attach_link_operation_report_details(fsm_payload, operation_payload)
            typed = LinkSubworkflowEffectPayload.model_validate(
                {
                    "schema": _json_field(fsm_payload, "schema"),
                    "progress_view_model": _json_field(fsm_payload, "progress_view_model"),
                    "receipt": _json_field(fsm_payload, "receipt"),
                    "reports": _json_field(fsm_payload, "reports"),
                    "error_context": _json_field(fsm_payload, "error_context", {}),
                    "fsm_payload": fsm_payload,
                }
            )
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="link_subworkflow_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(raw),
            )
        except ValueError as exc:
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="link_subworkflow_effect_payload",
                loc="$",
                message=str(exc),
                operation_payload=_safe_operation_payload(raw),
            )
        status = typed.progress_view_model.status.value
        effect_status = _effect_status_from_progress_status(status)
        reason_code = _link_effect_reason_code(typed.error_context, status=status)
        error_context = dict(typed.error_context)
        if effect_status in {
            WorkflowEffectStatus.BLOCKED,
            WorkflowEffectStatus.FAILED,
            WorkflowEffectStatus.WAITING_EXTERNAL,
            WorkflowEffectStatus.WAITING_HUMAN,
        } and reason_code:
            error_context.setdefault("blocked_reason", reason_code)
            error_context.setdefault("root_cause", reason_code)
        report_summary = _json_field(typed.reports, "summary")
        return WorkflowEffectResult(
            effect=effect,
            status=effect_status,
            outcome=_link_outcome_for_effect_status(effect_status, reason_code=reason_code or status),
            public_summary=_text_or_empty(report_summary),
            developer_summary="Link adapter returned an FSM-first payload.",
            payload=typed.fsm_payload,
            receipt=typed.receipt.to_payload() if effect.requires_receipt else None,
            next_action=_text_or_empty(typed.receipt.next_action),
            resume_action=_text_or_empty(typed.progress_view_model.resume_action),
            error_context=error_context,
        )


@dataclass
class WikiSubworkflowEffectAdapter:
    """Route generic subworkflow effects to the correct Wiki-domain adapter.

    `WorkflowEffectKind` stays domain-agnostic in the kernel. Wiki-specific
    work such as Related Notes is selected here from `target` plus the typed
    payload, so the FSM keeps one clean executable effect vocabulary.
    """

    link_adapter: LinkWorkflowEffectAdapter
    related_notes_adapter: RelatedNotesEffectAdapter

    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        if effect.kind != WorkflowEffectKind.RUN_SUBWORKFLOW:
            raise ValueError(f"WikiSubworkflowEffectAdapter cannot run effect kind: {effect.kind.value}")
        if effect.target in {"/mednotes:link", "/mednotes:link-body"}:
            return self.link_adapter.run(effect, context)
        if effect.target in {"related_notes.export", "related_notes_export", "related_notes.section"}:
            return self.related_notes_adapter.run(effect, context)
        raise ValueError(f"WikiSubworkflowEffectAdapter cannot route effect target: {effect.target}")


def _link_fsm_payload_from_raw(
    *,
    effect: WorkflowEffect,
    raw: JsonObject,
    mode: Literal["apply", "diagnose"],
    include_related_notes: bool,
    version_control_safety: VersionControlSafety,
) -> JsonObject:
    from mednotes.domains.wiki.flows.link.link_fsm import build_link_fsm_result
    from mednotes.domains.wiki.flows.link.link_runtime_result import link_fsm_facts_from_linker_result

    result = build_link_fsm_result(
        link_fsm_facts_from_linker_result(
            raw,
            run_id=f"link-{effect.run_id}",
            mode=mode,
            include_related_notes=include_related_notes,
            version_control_safety=version_control_safety,
        )
    ).to_payload()
    return _json_object(result)


def _attach_link_operation_report_details(fsm_payload: JsonObject, operation_payload: JsonObject) -> JsonObject:
    """Expose compact child-operation facts inside the child FSM report.

    Parent workflows still consume `link-fsm-result.v1` as the only operational
    artifact. These details are a typed adapter projection from the raw linker
    result, kept under `reports.details` so callers do not need to parse the
    legacy `link-run.v1` receipt as workflow truth.
    """

    details: dict[str, object] = {}
    for key in (
        "related_notes_sync",
        "body_term_linker",
        "reference_repair",
        "reference_repair_apply",
        "graph_audit_before",
        "graph_audit_after",
        "blockers",
    ):
        value = _json_field(operation_payload, key)
        if isinstance(value, dict):
            details[key] = _json_object(value)
        elif isinstance(value, list):
            details[key] = list(value)
    for key in (
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "returncode",
        "diagnosis_path",
        "receipt_path",
        "files_changed",
        "changed_file_count",
        "blocker_count",
        "links_planned",
        "links_rewritten",
    ):
        value = _json_field(operation_payload, key)
        if value is not None:
            details[key] = value
    changed_files = _json_field(operation_payload, "changed_files")
    if isinstance(changed_files, list):
        details["changed_files"] = list(changed_files)

    reports = _json_object_or_empty(_json_field(fsm_payload, "reports"))
    existing_details = _json_object_or_empty(_json_field(reports, "details"))
    reports["details"] = JsonObjectAdapter.validate_python({**existing_details, **details})
    return JsonObjectAdapter.validate_python({**fsm_payload, "reports": reports})


def _effect_status_from_progress_status(status: str) -> WorkflowEffectStatus:
    match status:
        case "completed":
            return WorkflowEffectStatus.COMPLETED
        case "completed_with_warnings":
            return WorkflowEffectStatus.COMPLETED_WITH_WARNINGS
        case "completed_with_link_blockers":
            return WorkflowEffectStatus.BLOCKED
        case "waiting_external":
            return WorkflowEffectStatus.WAITING_EXTERNAL
        case "waiting_human":
            return WorkflowEffectStatus.WAITING_HUMAN
        case "blocked":
            return WorkflowEffectStatus.BLOCKED
        case "failed":
            return WorkflowEffectStatus.FAILED
        case _:
            return WorkflowEffectStatus.FAILED


def _link_effect_reason_code(error_context: JsonObject, *, status: str) -> str:
    """Preserve linker blocker semantics when converting FSM payloads to effects."""

    for value in (
        _json_field(error_context, "blocked_reason"),
        _json_field(error_context, "root_cause"),
    ):
        text = _text_or_empty(value)
        if text:
            return text
    if status == "completed_with_link_blockers":
        return "link_plan_blocked"
    return ""


def _link_outcome_for_effect_status(status: WorkflowEffectStatus, *, reason_code: str = "") -> ContractModel:
    """Map link FSM status to link-domain outcomes, not generic status buckets."""

    match status:
        case WorkflowEffectStatus.COMPLETED:
            return LinkEffectCompletedOutcome()
        case WorkflowEffectStatus.COMPLETED_WITH_WARNINGS:
            return LinkEffectLinkerBlockedOutcome(reason_code=reason_code)
        case WorkflowEffectStatus.WAITING_EXTERNAL:
            return LinkEffectBlockedOutcome(reason_code=reason_code)
        case WorkflowEffectStatus.BLOCKED:
            if reason_code in {"graph_blocked", "graph_blockers", "link_plan_blocked"}:
                return LinkEffectGraphBlockedOutcome()
            if reason_code in {"linker_blocked", "body_term_linker", "related_notes_blocked"}:
                return LinkEffectLinkerBlockedOutcome(reason_code=reason_code)
            return LinkEffectBlockedOutcome(reason_code=reason_code)
        case WorkflowEffectStatus.FAILED:
            return LinkEffectFailedOutcome(reason_code=reason_code)
        case WorkflowEffectStatus.SKIPPED:
            return LinkEffectBlockedOutcome(reason_code=reason_code or "skipped")
        case _:
            return LinkEffectFailedOutcome(reason_code=reason_code or status.value)


@dataclass
class SpecialistModelEffectAdapter:
    runner: Callable[[WorkflowEffect], object] | None = None

    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        del context
        if effect.kind != WorkflowEffectKind.CALL_SPECIALIST_MODEL:
            raise ValueError(f"SpecialistModelEffectAdapter cannot run effect kind: {effect.kind.value}")
        if self.runner is None:
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.WAITING_AGENT,
                outcome=SpecialistModelBlockedOutcome(reason_code="specialist_agent_required"),
                public_summary="Aguardando modelo especializado antes de continuar.",
                developer_summary=(
                    "No Python runner is available; the agent must execute the specialist effect through "
                    "agent_directive.control instead of treating this as external quota."
                ),
                payload={"model_policy": dict(effect.model_policy)},
                next_action="Executar a chamada ao especialista pela rota oficial do agente.",
            )
        raw_result = self.runner(effect)
        if not isinstance(raw_result, dict):
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="specialist_model_effect_payload",
                loc="$",
                message=f"specialist runner returned {type(raw_result).__name__}, expected object",
                operation_payload={"raw_result_type": type(raw_result).__name__},
            )
        try:
            operation_payload = _json_object(raw_result)
            result = SpecialistModelEffectPayload.from_operation_payload(operation_payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="specialist_model_effect_payload",
                exc=exc,
                operation_payload=_safe_operation_payload(raw_result),
            )
        if result.status == "waiting_external":
            reason = str(result.blocked_reason or result.status)
            next_action = str(result.next_action or "Aguardar capacidade do modelo especialista e retomar pela rota oficial.")
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.WAITING_EXTERNAL,
                outcome=SpecialistModelCapacityWaitOutcome(reason_code=reason),
                public_summary="Modelo especialista aguardando capacidade externa.",
                developer_summary="Specialist runner returned a resumable external wait.",
                payload=result.to_payload(),
                next_action=next_action,
                resume_action=next_action,
                error_context={
                    "blocked_reason": reason,
                    "root_cause": reason,
                    "required_inputs": list(result.required_inputs),
                },
            )
        if result.status != "completed":
            reason = str(result.blocked_reason or result.status or "blocked")
            return WorkflowEffectResult(
                effect=effect,
                status=WorkflowEffectStatus.BLOCKED,
                outcome=SpecialistModelBlockedOutcome(reason_code=reason),
                public_summary="A chamada ao especialista nao produziu saida aplicavel.",
                developer_summary="Specialist runner returned non-completed status.",
                payload=result.to_payload(),
                next_action=str(result.next_action or "Repetir a chamada ao especialista pela rota oficial."),
                error_context={"status": str(result.status or "blocked"), "blocked_reason": reason},
            )
        receipt = result.receipt
        if receipt is None:
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="specialist_model_effect_payload",
                loc="receipt",
                message="completed specialist model effect payload requires typed receipt",
                operation_payload=result.to_payload(),
            )
        try:
            raw_receipt_payload = _json_object(_json_field(result.operation_payload, "receipt"))
            validate_specialist_task_run_receipt_attestation(raw_receipt_payload)
        except PydanticValidationError as exc:
            return _effect_payload_contract_block(
                effect,
                payload_schema="specialist_model_effect_payload",
                exc=exc,
                operation_payload=result.to_payload(),
            )
        except ValidationError as exc:
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="specialist_model_effect_payload",
                loc="receipt.receipt_attestation",
                message=str(exc),
                operation_payload=result.to_payload(),
            )
        if receipt.specialist_output_attestation is None:
            return _effect_payload_contract_block_from_message(
                effect,
                payload_schema="specialist_model_effect_payload",
                loc="receipt.specialist_output_attestation",
                message="completed specialist model effect payload requires receipt attestation",
                operation_payload=result.to_payload(),
            )
        receipt_payload = receipt.to_payload()
        attestation_payload = receipt.specialist_output_attestation.to_payload()
        return WorkflowEffectResult(
            effect=effect,
            status=WorkflowEffectStatus.COMPLETED,
            outcome=SpecialistModelCompletedOutcome(),
            public_summary="Saida especializada recebida e validada.",
            developer_summary="Specialist runner returned a typed specialist task receipt and attestation.",
            payload={**result.payload, "specialist_task_run_receipt": receipt_payload},
            receipt=receipt_payload,
            attestation=attestation_payload,
        )


def _effect_payload_contract_block(
    effect: WorkflowEffect,
    *,
    payload_schema: str,
    exc: PydanticValidationError,
    operation_payload: JsonObject,
    outcome: ContractModel | None = None,
) -> WorkflowEffectResult:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
    msg = str(first.get("msg") or str(exc))
    return WorkflowEffectResult(
        effect=effect,
        status=WorkflowEffectStatus.BLOCKED,
        outcome=outcome or LinkEffectBlockedOutcome(reason_code="effect_payload_contract_invalid"),
        public_summary="O resultado interno do workflow falhou na validação antes de continuar.",
        developer_summary=f"{payload_schema} invalid at {loc}: {msg}",
        payload={
            "schema": "medical-notes-workbench.workflow-effect-payload-contract-error.v1",
            "payload_schema": payload_schema,
            "operation_payload": operation_payload,
        },
        next_action="Corrigir o contrato tipado do efeito e repetir pela rota oficial.",
        error_context={
            "root_cause": "effect_payload_contract_invalid",
            "payload_schema": payload_schema,
            "contract_error": {"loc": loc, "message": msg},
        },
    )


def _effect_payload_contract_block_from_message(
    effect: WorkflowEffect,
    *,
    payload_schema: str,
    loc: str,
    message: str,
    operation_payload: JsonObject,
    outcome: ContractModel | None = None,
) -> WorkflowEffectResult:
    return WorkflowEffectResult(
        effect=effect,
        status=WorkflowEffectStatus.BLOCKED,
        outcome=outcome or LinkEffectBlockedOutcome(reason_code="effect_payload_contract_invalid"),
        public_summary="O resultado interno do workflow falhou na validação antes de continuar.",
        developer_summary=f"{payload_schema} invalid at {loc}: {message}",
        payload={
            "schema": "medical-notes-workbench.workflow-effect-payload-contract-error.v1",
            "payload_schema": payload_schema,
            "operation_payload": operation_payload,
        },
        next_action="Corrigir o contrato tipado do efeito e repetir pela rota oficial.",
        error_context={
            "root_cause": "effect_payload_contract_invalid",
            "payload_schema": payload_schema,
            "contract_error": {"loc": loc, "message": message},
        },
    )


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


def _safe_operation_payload(payload: object) -> JsonObject:
    try:
        return _json_object(payload)
    except PydanticValidationError:
        return {"raw_result_type": type(payload).__name__}


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source[key] if key in source else default


def _optional_text(value: JsonValue) -> str | None:
    text = _text_or_empty(value).strip()
    return text or None


def _text_or_empty(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return f"{value}"


def _float_field(source: JsonObject, key: str, default: float) -> float:
    value = _json_field(source, key, default)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _int_field(source: JsonObject, key: str, default: int) -> int:
    value = _json_field(source, key, default)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default
