#!/usr/bin/env python3
"""Prepare and apply deterministic /flashcards write plans.

This script glues together the local contracts around the LLM-owned card
formulation step. It does not call Anki itself; it prepares the payload the
agent should send to Anki MCP and records/report accepted results afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

SCRIPT_DIR = Path(__file__).resolve().parent
# Direct script execution must import the local package, not an installed stale
# distribution. The package root for ``mednotes.domains...`` is ``bundle/src``.
PACKAGE_ROOT = SCRIPT_DIR.parents[2]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from mednotes.domains.flashcards import index as flashcard_index  # noqa: E402
from mednotes.domains.flashcards import model as anki_model_validator  # noqa: E402
from mednotes.domains.flashcards import report as flashcard_report  # noqa: E402
from mednotes.domains.flashcards.contracts import (  # noqa: E402
    FLASHCARD_APPLY_SCHEMA,
    FlashcardAnkiNote,
    FlashcardApplyResult,
    FlashcardCandidateBatch,
    FlashcardIndexCheck,
    FlashcardIndexSummary,
    FlashcardModelPreference,
    FlashcardModelValidation,
    FlashcardObsidianLinkError,
    FlashcardPreparedCard,
    FlashcardSourceStatus,
    FlashcardWritePlan,
    FlashcardWriteSummary,
    normalize_candidate_batch,
)
from mednotes.domains.flashcards.flashcards_machine import (  # noqa: E402
    AnkiWriteCompletedEvent,
    CandidateGenerationCompletedEvent,
    FlashcardsBlockedEvent,
    FlashcardsFailedEvent,
    FlashcardsMachine,
    FlashcardsState,
    NoCardsToCreateEvent,
    SourcesResolvedEvent,
)
from mednotes.domains.flashcards.fsm import (  # noqa: E402
    flashcards_fsm_payload_from_model,
)
from mednotes.kernel.base import JsonObject, JsonObjectAdapter  # noqa: E402
from mednotes.kernel.fsm_model import WorkflowModel  # noqa: E402
from mednotes.kernel.state_machine import send_workflow_event  # noqa: E402
from mednotes.platform.feedback import command_string, safe_record_workflow_run  # noqa: E402

PREPARE_SCHEMA = "medical-notes-workbench.flashcard-write-plan.v1"
APPLY_SCHEMA = FLASHCARD_APPLY_SCHEMA
EXIT_OK = 0
EXIT_BLOCKED = 3
EXIT_IO = 5


def _read_json(path: str) -> object:
    if path == "-":
        return json.loads(sys.stdin.read())
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _json_field(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return value if isinstance(value, str) else ""


def _json_object_field(payload: JsonObject, key: str) -> JsonObject:
    value = _json_field(payload, key)
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _record_feedback(payload: JsonObject, exit_code: int, started_at: float) -> None:
    safe_record_workflow_run(
        workflow="/flashcards",
        command=command_string(),
        payload=payload,
        exit_code=exit_code,
        started_at=started_at,
    )


def _index_path(value: str | None = None) -> Path:
    return Path(
        os.path.expandvars(
            value or os.getenv("MED_FLASHCARDS_INDEX") or flashcard_index.DEFAULT_INDEX
        )
    ).expanduser()


def _models_payload(payload: JsonObject) -> object:
    for key in ("models", "model_fields", "anki_models"):
        value = _json_field(payload, key)
        if value:
            return value
    return {}


def _source_manifest(payload: JsonObject) -> JsonObject:
    return _json_object_field(payload, "source_manifest")


def _field(card: FlashcardPreparedCard, name: str) -> str:
    match name:
        case "Frente":
            return card.fields.Frente
        case "Verso":
            return card.fields.Verso
        case "Verso Extra":
            return card.fields.Verso_Extra
        case "Obsidian":
            return card.fields.Obsidian
        case "Texto":
            return card.fields.Texto
    return ""


def _anki_note_for(card: FlashcardPreparedCard, model_name: str) -> FlashcardAnkiNote:
    return FlashcardAnkiNote(
        deckName=card.deck,
        modelName=card.note_model or model_name,
        fields=card.fields,
    )


def _find_query_for(card: FlashcardPreparedCard) -> str:
    obsidian = _field(card, "Obsidian")
    deck = card.deck
    note_model = card.note_model
    is_cloze = note_model == anki_model_validator.DEFAULT_CLOZE_MODEL or bool(
        _field(card, "Texto")
    )
    parts = []
    if deck:
        parts.append(f'deck:"{deck}"')
    if obsidian:
        parts.append(f'Obsidian:"{obsidian}"')
    if is_cloze:
        text = _field(card, "Texto")
        if text:
            parts.append(f'Texto:"{text}"')
    else:
        front = _field(card, "Frente")
        if front:
            parts.append(f'Frente:"{front}"')
    return " ".join(parts)


def _payload_uses_model_set(payload: JsonObject, candidate_batch: FlashcardCandidateBatch) -> bool:
    if _json_object_field(payload, "preferred_models"):
        return True
    return any(card.note_model == anki_model_validator.DEFAULT_CLOZE_MODEL for card in candidate_batch.candidate_cards)


def _model_preference(payload: JsonObject) -> FlashcardModelPreference:
    preferred_models = _json_object_field(payload, "preferred_models")
    return FlashcardModelPreference.model_validate(
        {
            "preferred_model": _json_field(payload, "preferred_model"),
            "preferred_models": preferred_models,
        }
    )


def _candidate_batch_payload(payload: JsonObject) -> JsonObject:
    return {
        "source_manifest": _json_field(payload, "source_manifest"),
        "candidate_cards": _json_field(payload, "candidate_cards"),
    }


def _invalid_obsidian_plan() -> FlashcardWritePlan:
    return FlashcardWritePlan(
        blocked=True,
        blocked_reason="invalid_obsidian_deeplink",
        next_action=(
            "Regenerar o manifest de fontes/links Obsidian e preparar novamente "
            "antes de criar cards no Anki."
        ),
        model_validation=FlashcardModelValidation(ok=False, missing_fields=["Obsidian"]),
        source_status=FlashcardSourceStatus(),
        index_check=FlashcardIndexCheck(
            summary=FlashcardIndexSummary(candidate_count=0, new_count=0, duplicate_count=0),
        ),
        summary=FlashcardWriteSummary(
            candidate_count=0,
            new_count=0,
            duplicate_count=0,
            changed_source_count=0,
            anki_note_count=0,
        ),
    )


def _invalid_candidate_batch_plan() -> FlashcardWritePlan:
    return FlashcardWritePlan(
        blocked=True,
        blocked_reason="invalid_flashcard_candidate_batch",
        next_action=(
            "Regenerar a prévia de flashcards a partir do manifest de fontes atual "
            "antes de criar cards no Anki."
        ),
        model_validation=FlashcardModelValidation(ok=False),
        source_status=FlashcardSourceStatus(),
        index_check=FlashcardIndexCheck(
            summary=FlashcardIndexSummary(candidate_count=0, new_count=0, duplicate_count=0),
        ),
        summary=FlashcardWriteSummary(
            candidate_count=0,
            new_count=0,
            duplicate_count=0,
            changed_source_count=0,
            anki_note_count=0,
        ),
    )


def prepare_write_plan(payload: JsonObject, index: JsonObject) -> FlashcardWritePlan:
    try:
        candidate_batch = FlashcardCandidateBatch.model_validate(_candidate_batch_payload(payload))
    except PydanticValidationError:
        return _invalid_candidate_batch_plan()

    try:
        candidate_batch = normalize_candidate_batch(candidate_batch)
    except FlashcardObsidianLinkError:
        return _invalid_obsidian_plan()

    normalized_payload: JsonObject = {
        **payload,
        "source_manifest": candidate_batch.source_manifest.to_payload(),
        "candidate_cards": [card.to_payload() for card in candidate_batch.candidate_cards],
    }

    use_model_set = _payload_uses_model_set(normalized_payload, candidate_batch)
    model_preference = _model_preference(normalized_payload)
    if use_model_set:
        model_validation = FlashcardModelValidation.model_validate(
            anki_model_validator.validate_model_set(
                _models_payload(normalized_payload),
                preferred_qa_model=model_preference.preferred_models.qa
                or anki_model_validator.DEFAULT_QA_MODEL,
                preferred_cloze_model=model_preference.preferred_models.cloze
                or anki_model_validator.DEFAULT_CLOZE_MODEL,
            )
        )
        default_model_name = model_validation.qa.model if model_validation.qa is not None else ""
    else:
        model_validation = FlashcardModelValidation.model_validate(
            anki_model_validator.validate_models(
                _models_payload(normalized_payload),
                preferred_model=model_preference.preferred_model or None,
            )
        )
        default_model_name = model_validation.model
    source_status = FlashcardSourceStatus.model_validate(
        flashcard_index.source_status(_source_manifest(normalized_payload), index)
    )
    index_check = FlashcardIndexCheck.model_validate(flashcard_index.check_candidates(normalized_payload, index))
    new_cards = index_check.new_cards if model_validation.ok else []
    model_name = default_model_name

    changed_sources = [item for item in source_status.sources if item.status == "changed"]
    anki_notes = [_anki_note_for(card, model_name) for card in new_cards]
    find_queries = [
        {"card_hash": card.card_hash, "query": _find_query_for(card)} for card in new_cards
    ]

    plan_payload = {
        "schema": PREPARE_SCHEMA,
        "blocked": not model_validation.ok,
        "blocked_reason": "anki_model_validation_failed" if not model_validation.ok else "",
        "next_action": (
            "Corrigir/provisionar modelos Anki e preparar novamente."
            if not model_validation.ok
            else ""
        ),
        "requires_reprocess_confirmation": bool(changed_sources),
        "model_validation": model_validation,
        "source_status": source_status,
        "index_check": index_check,
        "changed_sources": changed_sources,
        "anki_find_queries": find_queries,
        "anki_notes": anki_notes,
        "new_cards": new_cards,
        "duplicate_cards": index_check.duplicate_cards,
        "summary": {
            "candidate_count": index_check.summary.candidate_count,
            "new_count": index_check.summary.new_count if model_validation.ok else 0,
            "duplicate_count": index_check.summary.duplicate_count,
            "changed_source_count": len(changed_sources),
            "anki_note_count": len(anki_notes),
        },
    }
    return FlashcardWritePlan.model_validate(plan_payload)


def apply_accepted(payload: JsonObject, index: JsonObject) -> tuple[JsonObject, JsonObject]:
    updated, record_summary = flashcard_index.record_cards(payload, index)
    apply_result = FlashcardApplyResult(
        summary=JsonObjectAdapter.validate_python(record_summary),
        report=flashcard_report.build_report_model(payload),
    )
    return JsonObjectAdapter.validate_python(updated), apply_result.to_payload()


def _apply_fsm_payload_from_result(
    result: JsonObject,
    *,
    run_id: str,
    index_path: Path,
    dry_run: bool,
) -> JsonObject:
    """Project Anki success without fabricating Obsidian tag completion.

    The next executable step is the machine-owned `flashcards.tag_obsidian`
    effect. Only the adapter receipt for that effect may later emit
    `ObsidianTaggingCompletedEvent`.
    """

    apply_result = FlashcardApplyResult.model_validate(result)
    created_card_count = apply_result.report.summary.created_card_count
    model = WorkflowModel.start(
        workflow="/flashcards",
        run_id=run_id,
        initial_state=FlashcardsState.WRITING_ANKI.value,
    )
    machine = FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    send_workflow_event(
        machine,
        AnkiWriteCompletedEvent(
            workflow="/flashcards",
            run_id=run_id,
            current_state=FlashcardsState.WRITING_ANKI.value,
            created_card_count=created_card_count,
        ),
    )
    fsm_payload = flashcards_fsm_payload_from_model(model)
    artifacts = _json_object_field(fsm_payload, "artifacts")
    return {
        **fsm_payload,
        "artifacts": {
            **artifacts,
            "apply_result": apply_result.to_payload(),
            "index_path": str(index_path),
            "dry_run": dry_run,
        },
    }


def _failed_fsm_payload_for_command(command: str, exc: Exception, *, run_id: str) -> JsonObject:
    """Convert CLI exceptions into the `/flashcards` FSM contract on stdout."""

    initial_state = FlashcardsState.WRITING_ANKI if command == "apply" else FlashcardsState.CHECKING_SOURCES
    model = WorkflowModel.start(
        workflow="/flashcards",
        run_id=run_id,
        initial_state=initial_state.value,
    )
    send_workflow_event(
        FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        FlashcardsFailedEvent(
            workflow="/flashcards",
            run_id=run_id,
            current_state=initial_state.value,
            reason_code="flashcards_apply_failed" if command == "apply" else "flashcards_prepare_failed",
            next_action="Corrigir o input/indice local e rodar /flashcards novamente.",
            audit_evidence={
                "exception_type": exc.__class__.__name__,
                "error_summary": str(exc),
            },
        ),
    )
    return flashcards_fsm_payload_from_model(model)


def _prepare_fsm_payload_from_plan(plan: FlashcardWritePlan, *, run_id: str) -> JsonObject:
    """Use FlashcardsMachine for operational states; keep facts path only for no-op completion."""

    if plan.blocked:
        model = WorkflowModel.start(
            workflow="/flashcards",
            run_id=run_id,
            initial_state=FlashcardsState.CHECKING_SOURCES.value,
        )
        send_workflow_event(
            FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
            FlashcardsBlockedEvent(
                workflow="/flashcards",
                run_id=run_id,
                current_state=FlashcardsState.CHECKING_SOURCES.value,
                reason_code=plan.blocked_reason or "flashcards_prepare_blocked",
                next_action=plan.next_action or "Corrigir o bloqueio indicado e preparar novamente.",
            ),
        )
        return flashcards_fsm_payload_from_model(model)

    if plan.summary.new_count > 0:
        model = WorkflowModel.start(
            workflow="/flashcards",
            run_id=run_id,
            initial_state=FlashcardsState.CHECKING_SOURCES.value,
        )
        machine = FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
        send_workflow_event(
            machine,
            SourcesResolvedEvent(
                workflow="/flashcards",
                run_id=run_id,
                current_state=FlashcardsState.CHECKING_SOURCES.value,
                source_count=len(plan.source_status.sources),
            ),
        )
        send_workflow_event(
            machine,
            CandidateGenerationCompletedEvent(
                workflow="/flashcards",
                run_id=run_id,
                current_state=FlashcardsState.WAITING_AGENT_CANDIDATES.value,
                candidate_count=plan.summary.candidate_count,
                new_card_count=plan.summary.new_count,
            ),
        )
        return flashcards_fsm_payload_from_model(model)

    model = WorkflowModel.start(
        workflow="/flashcards",
        run_id=run_id,
        initial_state=FlashcardsState.CHECKING_SOURCES.value,
    )
    machine = FlashcardsMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD)
    send_workflow_event(
        machine,
        SourcesResolvedEvent(
            workflow="/flashcards",
            run_id=run_id,
            current_state=FlashcardsState.CHECKING_SOURCES.value,
            source_count=len(plan.source_status.sources),
        ),
    )
    send_workflow_event(
        machine,
        NoCardsToCreateEvent(
            workflow="/flashcards",
            run_id=run_id,
            current_state=FlashcardsState.WAITING_AGENT_CANDIDATES.value,
            source_count=len(plan.source_status.sources),
            candidate_count=plan.summary.candidate_count,
            duplicate_card_count=plan.summary.duplicate_count,
        ),
    )
    return flashcards_fsm_payload_from_model(model)


def _cmd_prepare(args: argparse.Namespace) -> int:
    started_at = time.time()
    payload = _json_object(_read_json(args.input))
    path = _index_path(args.index)
    plan = prepare_write_plan(payload, JsonObjectAdapter.validate_python(flashcard_index._load_index(path)))
    fsm_payload = _prepare_fsm_payload_from_plan(plan, run_id=f"flashcards-{int(started_at)}")
    diagnostic_context = _json_object_field(fsm_payload, "diagnostic_context")
    artifacts = _json_object_field(fsm_payload, "artifacts")
    public_payload = {
        **fsm_payload,
        "artifacts": {
            **artifacts,
            "write_plan": plan.to_payload(),
            "index_path": str(path),
        },
    }
    if diagnostic_context:
        public_payload["diagnostic_context"] = diagnostic_context
    _json(public_payload)
    progress = _json_object_field(public_payload, "progress_view_model")
    progress_status = _json_str_field(progress, "status")
    exit_code = EXIT_BLOCKED if progress_status == "blocked" else EXIT_OK
    _record_feedback(public_payload, exit_code, started_at)
    return exit_code


def _cmd_apply(args: argparse.Namespace) -> int:
    started_at = time.time()
    payload = _json_object(_read_json(args.input))
    path = _index_path(args.index)
    updated, result = apply_accepted(payload, JsonObjectAdapter.validate_python(flashcard_index._load_index(path)))
    if not args.dry_run:
        flashcard_index._write_json_atomic(path, updated)
    fsm_payload = _apply_fsm_payload_from_result(
        result,
        run_id=f"flashcards-{int(started_at)}",
        index_path=path,
        dry_run=args.dry_run,
    )
    _json(fsm_payload)
    _record_feedback(
        fsm_payload,
        EXIT_OK,
        started_at,
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="prepare an Anki write plan from candidate cards")
    prepare.add_argument("--input", required=True, help="candidate run JSON, or '-' for stdin")
    prepare.add_argument("--index", help=f"index path; default {flashcard_index.DEFAULT_INDEX}")
    prepare.set_defaults(func=_cmd_prepare)

    apply = sub.add_parser("apply", help="record accepted Anki cards and emit final report")
    apply.add_argument("--input", required=True, help="accepted run JSON, or '-' for stdin")
    apply.add_argument("--index", help=f"index path; default {flashcard_index.DEFAULT_INDEX}")
    apply.add_argument("--dry-run", action="store_true", help="report without writing index")
    apply.set_defaults(func=_cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        OSError,
        json.JSONDecodeError,
        flashcard_index.IndexErrorWithCode,
        flashcard_report.FlashcardReportInputError,
    ) as exc:
        exit_code = getattr(exc, "exit_code", EXIT_IO)
        fsm_payload = _failed_fsm_payload_for_command(
            str(getattr(args, "command", "unknown")),
            exc,
            run_id=f"flashcards-{int(time.time())}",
        )
        _json(fsm_payload)
        safe_record_workflow_run(
            workflow="/flashcards",
            command=command_string(),
            payload=fsm_payload,
            exit_code=exit_code,
            started_at=time.time(),
            snippets=[str(exc)],
        )
        print(str(exc), file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
