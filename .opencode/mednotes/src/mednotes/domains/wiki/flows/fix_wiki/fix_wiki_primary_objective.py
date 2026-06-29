from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mednotes.domains.wiki.contracts.agent_report import (
    FixWikiGraphStatus,
    FixWikiObjectiveStatus,
    FixWikiPrimaryObjectiveSummary,
    FixWikiRelatedNotesStatus,
)
from mednotes.kernel.base import JsonObject, JsonObjectAdapter, JsonValue

_INTERNAL_RELATED_NOTES_SKIP_REASONS = {
    "linker_not_run",
    "not_needed",
    "no_link_trigger",
    "no_related_notes_changes",
}


class _ObjectiveModel(BaseModel):
    # These are partial typed lenses over larger payloads: unknown keys are
    # ignored, but known operational fields remain strict and cannot be
    # fabricated through Pydantic coercion.
    model_config = ConfigDict(extra="ignore", populate_by_name=True, strict=True)


class _ObjectiveCounts(_ObjectiveModel):
    mutated_files: int = 0
    written_files: int = 0


class _ObjectiveProgress(_ObjectiveModel):
    status: str = ""
    counts: _ObjectiveCounts | None = None

    @field_validator("counts", mode="before")
    @classmethod
    def _empty_counts(cls, value: object) -> object:
        return value if isinstance(value, dict) else None


class _ObjectiveReceipt(_ObjectiveModel):
    status: str = ""


class _ObjectiveApplyContext(_ObjectiveModel):
    vault_changed_file_count: int = 0
    written_count: int = 0


class _ObjectiveGraph(_ObjectiveModel):
    error_count: int = 0
    blocker_count: int = 0
    blockers: JsonValue = 0


class _ObjectiveFinalValidation(_ObjectiveModel):
    graph: _ObjectiveGraph | None = None


class _ObjectiveGraphAudit(_ObjectiveModel):
    error_count: int = 0


class _ObjectiveRelatedNotesSync(_ObjectiveModel):
    status: str = ""
    blocked_reason: str = ""
    skipped_reason: str = ""
    applied_note_count: int = 0


class _ObjectiveLinkerApply(_ObjectiveModel):
    related_notes_sync: _ObjectiveRelatedNotesSync | None = None


class _ObjectiveRelatedRecoveryState(_ObjectiveModel):
    status: str = ""
    blocked_reason: str = ""
    remaining_count: int = 0
    total_note_count: int = 0
    fresh_record_count: int = 0
    partial_record_count: int = 0


class _ObjectiveDiagnostic(_ObjectiveModel):
    apply: _ObjectiveApplyContext | None = None
    final_validation: _ObjectiveFinalValidation | None = None
    graph_audit_before_linker: _ObjectiveGraphAudit | None = None
    graph_error_count_before_linker: int | None = None
    related_notes_recovery_state: _ObjectiveRelatedRecoveryState | None = None
    related_notes_sync: _ObjectiveRelatedNotesSync | None = None
    related_notes_applied: bool = False
    linker_apply: _ObjectiveLinkerApply | None = None

    @field_validator(
        "apply",
        "final_validation",
        "graph_audit_before_linker",
        "related_notes_recovery_state",
        "related_notes_sync",
        "linker_apply",
        mode="before",
    )
    @classmethod
    def _empty_optional_object(cls, value: object) -> object:
        return value if isinstance(value, dict) else None


class _FixWikiObjectivePayload(_ObjectiveModel):
    schema_id: str = Field(default="", alias="schema")
    workflow: str = ""
    progress_view_model: _ObjectiveProgress | None = None
    receipt: _ObjectiveReceipt | None = None
    diagnostic_context: _ObjectiveDiagnostic | None = None


def fix_wiki_primary_objective_summary(payload: JsonObject) -> FixWikiPrimaryObjectiveSummary | None:
    payload = JsonObjectAdapter.validate_python(payload)
    root = _FixWikiObjectivePayload.model_validate(payload)
    if not _is_fix_wiki_payload(root):
        return None

    progress = root.progress_view_model or _ObjectiveProgress()
    receipt = root.receipt or _ObjectiveReceipt()
    diagnostic = root.diagnostic_context or _ObjectiveDiagnostic()
    status = _first_status(progress.status, receipt.status)

    mutation_count, written_count = _mutation_counts(
        progress=progress,
        diagnostic=diagnostic,
    )
    graph_status, graph_summary = _graph_outcome(diagnostic=diagnostic)
    related_status, related_summary = _related_notes_outcome(diagnostic=diagnostic)
    wiki_fixed, wiki_summary = _wiki_outcome(status=status, graph_status=graph_status, related_status=related_status)

    return FixWikiPrimaryObjectiveSummary(
        wiki_fixed=wiki_fixed,
        wiki_summary=wiki_summary,
        mutation_count=mutation_count,
        written_count=written_count,
        mutation_summary=_mutation_summary(mutation_count=mutation_count, written_count=written_count),
        graph_status=graph_status,
        graph_summary=graph_summary,
        related_notes_status=related_status,
        related_notes_summary=related_summary,
    )


def _is_fix_wiki_payload(payload: _FixWikiObjectivePayload) -> bool:
    return payload.workflow == "/mednotes:fix-wiki" or payload.schema_id == "medical-notes-workbench.fix-wiki-fsm-result.v1"


def _mutation_counts(
    *,
    progress: _ObjectiveProgress,
    diagnostic: _ObjectiveDiagnostic,
) -> tuple[int, int]:
    mutation_count = (
        _as_int((progress.counts or _ObjectiveCounts()).mutated_files)
        or _as_int((diagnostic.apply or _ObjectiveApplyContext()).vault_changed_file_count)
    )
    written_count = (
        _as_int((progress.counts or _ObjectiveCounts()).written_files)
        or _as_int((diagnostic.apply or _ObjectiveApplyContext()).written_count)
    )
    return mutation_count, written_count


def _mutation_summary(*, mutation_count: int, written_count: int) -> str:
    if mutation_count == 0 and written_count == 0:
        return "Nenhum arquivo da Wiki foi alterado nesta etapa."
    if mutation_count == written_count:
        return f"{mutation_count} arquivo(s) da Wiki alterado(s)."
    return f"{mutation_count} arquivo(s) da Wiki mudaram; {written_count} arquivo(s) foram gravados pelo reparo automático."


def _graph_outcome(
    *,
    diagnostic: _ObjectiveDiagnostic,
) -> tuple[FixWikiGraphStatus, str]:
    final_validation = diagnostic.final_validation or _ObjectiveFinalValidation()
    graph = final_validation.graph
    if graph is None and diagnostic.graph_error_count_before_linker is None and diagnostic.graph_audit_before_linker is None:
        return "unknown", "O payload não trouxe comparação suficiente do grafo."
    graph = graph or _ObjectiveGraph()
    after_errors = _as_int(graph.error_count)
    after_blockers = _as_int(graph.blocker_count or graph.blockers)
    if diagnostic.graph_error_count_before_linker is not None:
        before_errors = _optional_int(diagnostic.graph_error_count_before_linker)
    elif diagnostic.graph_audit_before_linker is not None:
        before_errors = _optional_int(diagnostic.graph_audit_before_linker.error_count)
    else:
        before_errors = None

    if before_errors is not None:
        if after_errors < before_errors:
            return "improved", f"O grafo melhorou: {before_errors} erro(s) antes, {after_errors} depois."
        if after_errors > before_errors:
            return "worse", f"O grafo piorou: {before_errors} erro(s) antes, {after_errors} depois."
        if after_errors == 0:
            return "clean", "O grafo terminou sem erros."
        return "unchanged", f"O grafo não melhorou: permaneceu com {after_errors} erro(s)."

    if after_errors == 0 and after_blockers == 0:
        return "clean", "O grafo terminou sem bloqueios."
    if after_errors or after_blockers:
        count = after_blockers or after_errors
        return "blocked", f"O grafo ainda tem {count} bloqueio(s)."
    return "unknown", "O payload não trouxe comparação suficiente do grafo."


def _related_notes_outcome(
    *,
    diagnostic: _ObjectiveDiagnostic,
) -> tuple[FixWikiRelatedNotesStatus, str]:
    recovery_state = diagnostic.related_notes_recovery_state or _ObjectiveRelatedRecoveryState()
    related_sync = diagnostic.related_notes_sync or _ObjectiveRelatedNotesSync()
    linker_apply = diagnostic.linker_apply or _ObjectiveLinkerApply()
    linker_related = linker_apply.related_notes_sync or _ObjectiveRelatedNotesSync()
    if not _related_notes_sync_has_signal(related_sync) and _related_notes_sync_has_signal(linker_related):
        related_sync = linker_related

    remaining = _as_int(recovery_state.remaining_count)
    total = _as_int(recovery_state.total_note_count)
    fresh = _as_int(recovery_state.fresh_record_count or recovery_state.partial_record_count)
    recovery_reason = recovery_state.blocked_reason
    if recovery_state.status == "waiting_for_retry" or remaining:
        reason = "cota externa" if "quota" in recovery_reason or "cota" in recovery_reason else "condição externa"
        count = f" ({fresh}/{total}, faltam {remaining})" if total else ""
        return "pending", f"Notas Relacionadas ficou pendente por {reason}{count}."

    if related_sync.status == "completed" or diagnostic.related_notes_applied is True:
        applied = _as_int(related_sync.applied_note_count)
        if applied:
            return "updated", f"Notas Relacionadas foi atualizado em {applied} nota(s)."
        return "updated", "Notas Relacionadas foi atualizado."

    blocked_reason = related_sync.blocked_reason
    if related_sync.status == "blocked" or blocked_reason:
        return "blocked", f"Notas Relacionadas ficou bloqueado: {blocked_reason or 'bloqueio não especificado'}."

    skipped_reason = related_sync.skipped_reason
    if related_sync.status == "skipped" or skipped_reason:
        reason = _public_related_notes_skip_reason(skipped_reason)
        return "skipped", f"Notas Relacionadas não foi alterado: {reason}."

    return "unknown", "O payload não confirmou atualização de Notas Relacionadas."


def _public_related_notes_skip_reason(value: str) -> str:
    """Translate internal skip codes before they reach agent-facing reports."""

    if not value:
        return "fora do escopo desta etapa"
    if value.strip().casefold() in _INTERNAL_RELATED_NOTES_SKIP_REASONS:
        return "fora do escopo desta etapa"
    return value


def _wiki_outcome(
    *,
    status: str,
    graph_status: FixWikiGraphStatus,
    related_status: FixWikiRelatedNotesStatus,
) -> tuple[FixWikiObjectiveStatus, str]:
    if status == "completed":
        return "yes", "Sim, o fix-wiki concluiu a correção da Wiki."
    if status == "completed_with_warnings":
        return "partial", "A Wiki foi corrigida com avisos pendentes."
    if status == "waiting_external":
        return "waiting_external", "A Wiki ficou parcialmente corrigida e aguarda uma condição externa."
    if status == "waiting_agent":
        return "waiting_agent", "A Wiki ficou parcialmente corrigida e aguarda continuação assistida pelo agente."
    if status == "failed":
        return "failed", "Não, o fix-wiki falhou antes de concluir."
    if status in {"blocked", "waiting_human"}:
        return "no", "Não, o fix-wiki ainda não concluiu a correção da Wiki."
    if graph_status in {"clean", "improved"} and related_status == "updated":
        return "yes", "Sim, os sinais principais indicam Wiki corrigida."
    return "unknown", "O payload não permite confirmar se o fix-wiki concluiu a Wiki."


def _related_notes_sync_has_signal(sync: _ObjectiveRelatedNotesSync) -> bool:
    return bool(sync.status or sync.blocked_reason or sync.skipped_reason or sync.applied_note_count)


def _first_status(*sources: str) -> str:
    for value in sources:
        if value:
            return value
    return ""


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("numeric fix-wiki fields must be numbers, not booleans")
    if isinstance(value, str):
        raise ValueError("numeric fix-wiki fields must be numbers, not strings")
    if isinstance(value, int | float):
        return max(0, int(value))
    raise ValueError("numeric fix-wiki fields must be numbers")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("numeric fix-wiki fields must be numbers, not booleans")
    if isinstance(value, str):
        raise ValueError("numeric fix-wiki fields must be numbers, not strings")
    if isinstance(value, int | float):
        return max(0, int(value))
    raise ValueError("numeric fix-wiki fields must be numbers")
