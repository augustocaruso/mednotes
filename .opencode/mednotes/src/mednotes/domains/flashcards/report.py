#!/usr/bin/env python3
"""Generate deterministic reports and previews for /flashcards runs."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

SCRIPT_DIR = Path(__file__).resolve().parent
MEDNOTES_DIR = SCRIPT_DIR.parent
if str(MEDNOTES_DIR) not in sys.path:
    sys.path.insert(0, str(MEDNOTES_DIR))

from mednotes.domains.flashcards.contracts import (  # noqa: E402
    FLASHCARD_REPORT_SCHEMA,
    FlashcardModelValidation,
    FlashcardReport,
    FlashcardReportSummary,
    FlashcardWritePlan,
)
from mednotes.domains.flashcards.fsm import FlashcardReportInput, FlashcardsPrimaryObjectiveSummary  # noqa: E402
from mednotes.kernel.base import JsonObject, JsonObjectAdapter  # noqa: E402
from mednotes.platform.feedback import command_string, safe_record_workflow_run  # noqa: E402

SCHEMA = FLASHCARD_REPORT_SCHEMA
PREVIEW_SCHEMA = "medical-notes-workbench.flashcard-card-preview.v1"
EXIT_OK = 0
EXIT_IO = 5


class FlashcardReportInputError(ValueError):
    pass


def _read_json(path: str) -> object:
    if path == "-":
        return json.loads(sys.stdin.read())
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _object_field(value: object, key: str) -> object:
    if not isinstance(value, dict) or key not in value:
        return None
    return value[key]


def _json_field(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return value if isinstance(value, str) else ""


def _json_int_field(payload: JsonObject, key: str) -> int:
    value = _json_field(payload, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _json_object_field(payload: JsonObject, key: str) -> JsonObject:
    value = _json_field(payload, key)
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _json_object_list_field(payload: JsonObject, key: str) -> list[JsonObject]:
    value = _json_field(payload, key)
    if not isinstance(value, list):
        return []
    return [JsonObjectAdapter.validate_python(item) for item in value if isinstance(item, dict)]


def _pydantic_first_error(exc: PydanticValidationError) -> tuple[str, str]:
    """Return compact Pydantic error context without depending on raw error dicts."""

    first = exc.errors()[0] if exc.errors() else {}
    loc = _object_field(first, "loc")
    if isinstance(loc, (list, tuple)):
        location = ".".join(str(part) for part in loc) or "$"
    else:
        location = "$"
    message = _object_field(first, "msg")
    return location, str(message or exc)


def _cards(payload: JsonObject, *keys: str) -> list[JsonObject]:
    for key in keys:
        cards = _json_object_list_field(payload, key)
        if cards:
            return cards
    return []


def _nested_cards(payload: JsonObject, container: str, key: str) -> list[JsonObject]:
    return _cards(_json_object_field(payload, container), key)


def _artifact_write_plan(payload: JsonObject) -> FlashcardWritePlan | None:
    artifacts = _json_object_field(payload, "artifacts")
    if not artifacts:
        return None
    if "write_plan" not in artifacts:
        return None
    write_plan_payload = artifacts["write_plan"]
    if not isinstance(write_plan_payload, dict):
        raise FlashcardReportInputError("artifacts.write_plan must be a FlashcardWritePlan object")
    try:
        return FlashcardWritePlan.model_validate(write_plan_payload)
    except PydanticValidationError as exc:
        loc, msg = _pydantic_first_error(exc)
        raise FlashcardReportInputError(
            f"artifacts.write_plan failed FlashcardWritePlan contract validation at {loc}: {msg}"
        ) from exc


def _artifact_new_cards(payload: JsonObject) -> list[JsonObject]:
    plan = _artifact_write_plan(payload)
    if plan is None:
        return []
    return [card.to_payload() for card in plan.new_cards]


def _artifact_duplicate_cards(payload: JsonObject) -> list[JsonObject]:
    plan = _artifact_write_plan(payload)
    if plan is None:
        return []
    return [card.to_payload() for card in plan.duplicate_cards]


def _validate_report_input(payload: JsonObject) -> FlashcardReportInput:
    report_payload: JsonObject = {}
    accepted_cards = _json_field(payload, "accepted_cards")
    if accepted_cards is None:
        accepted_cards = _json_field(payload, "created_cards")
    if accepted_cards is not None:
        report_payload["accepted_cards"] = accepted_cards
    reports = _json_field(payload, "reports")
    if reports is not None:
        report_payload["reports"] = reports

    try:
        return FlashcardReportInput.model_validate(report_payload)
    except PydanticValidationError as exc:
        loc, msg = _pydantic_first_error(exc)
        raise FlashcardReportInputError(
            f"FlashcardReportInput failed contract validation at {loc}: {msg}"
        ) from exc


def _obsidian_links_valid(report_input: FlashcardReportInput) -> bool:
    if report_input.reports is not None:
        return _flashcards_primary_objective_summary(report_input).obsidian_links_valid
    return all(card.fields.Obsidian.strip() for card in report_input.accepted_cards)


def _flashcards_primary_objective_summary(report_input: FlashcardReportInput) -> FlashcardsPrimaryObjectiveSummary:
    """Read the structured flashcards evidence from the shared reports envelope."""

    if report_input.reports is None:
        raise FlashcardReportInputError("reports must be present to read flashcards primary objective")
    details = report_input.reports.details
    raw_summary = details["primary_objective_summary"] if "primary_objective_summary" in details else None
    if not isinstance(raw_summary, dict):
        raise FlashcardReportInputError("reports.details.primary_objective_summary is required for flashcards")
    try:
        return FlashcardsPrimaryObjectiveSummary.model_validate(raw_summary)
    except PydanticValidationError as exc:
        loc, msg = _pydantic_first_error(exc)
        raise FlashcardReportInputError(
            f"reports.details.primary_objective_summary failed contract validation at {loc}: {msg}"
        ) from exc


def _field(card: JsonObject, name: str) -> str:
    fields = _json_object_field(card, "fields")
    return _json_str_field(fields, name)


def _preview_cards(payload: JsonObject) -> list[JsonObject]:
    return (
        _cards(payload, "new_cards")
        or _nested_cards(payload, "index_check", "new_cards")
        or _artifact_new_cards(payload)
        or _cards(payload, "candidate_cards", "cards")
    )


def _model_validation(payload: JsonObject) -> FlashcardModelValidation | None:
    write_plan = _artifact_write_plan(payload)
    if write_plan is not None:
        return write_plan.model_validation
    raw_model_validation = _json_field(payload, "model_validation")
    if not isinstance(raw_model_validation, dict):
        return None
    return FlashcardModelValidation.model_validate(raw_model_validation)


def _summary_items(value: object, allowed_keys: tuple[str, ...]) -> list[JsonObject]:
    """Normalize report-only evidence without promoting it to FSM state truth."""

    if not isinstance(value, list):
        return []
    items: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_payload = JsonObjectAdapter.validate_python(item)
        summary = {key: item_payload[key] for key in allowed_keys if key in item_payload}
        if summary:
            items.append(summary)
    return items


def _duplicate_card_summaries(payload: JsonObject) -> list[JsonObject]:
    write_plan = _artifact_write_plan(payload)
    if write_plan is not None:
        return [card.to_payload() for card in write_plan.index_check.duplicate_cards]
    raw_index_check = _json_object_field(payload, "index_check")
    if raw_index_check:
        return _summary_items(
            _json_field(raw_index_check, "duplicate_cards"),
            ("card_hash", "source_path", "source_relative_path", "duplicate_of"),
        )
    return _summary_items(
        _json_field(payload, "duplicate_cards"),
        ("card_hash", "source_path", "source_relative_path", "duplicate_of"),
    )


def _skipped_note_summaries(payload: JsonObject) -> list[JsonObject]:
    raw_manifest = _json_object_field(payload, "source_manifest")
    if raw_manifest:
        return _summary_items(
            _json_field(raw_manifest, "skipped_notes"),
            ("path", "vault_relative_path", "skip_reason", "skip_tags"),
        )
    return _summary_items(
        _json_field(payload, "skipped_notes"),
        ("path", "vault_relative_path", "skip_reason", "skip_tags"),
    )


def _anki_error_summaries(payload: JsonObject) -> list[str]:
    value = _json_field(payload, "anki_errors")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def build_report_model(payload: JsonObject) -> FlashcardReport:
    report_input = _validate_report_input(payload)
    model_validation = _model_validation(payload)
    accepted_cards = [card.to_payload() for card in report_input.accepted_cards]
    duplicate_cards = _duplicate_card_summaries(payload)
    skipped_notes = _skipped_note_summaries(payload)
    anki_errors = _anki_error_summaries(payload)
    obsidian_links_valid = _obsidian_links_valid(report_input)

    processed_sources = sorted(
        {
            _json_str_field(card, "source_path") or _json_str_field(card, "source")
            for card in accepted_cards
            if _json_str_field(card, "source_path") or _json_str_field(card, "source")
        }
    )
    model_error = None
    if model_validation is not None and not model_validation.ok:
        model_error = {
            "required_fields": model_validation.required_fields,
            "checked_models": [checked.to_payload() for checked in model_validation.checked_models],
        }

    return FlashcardReport.model_validate(
        {
            "schema": SCHEMA,
            "summary": FlashcardReportSummary(
                processed_note_count=len(processed_sources),
                created_card_count=len(accepted_cards),
                duplicate_card_count=len(duplicate_cards),
                skipped_note_count=len(skipped_notes),
                model_error_count=1 if model_error else 0,
                anki_error_count=len(anki_errors),
                obsidian_links_valid=obsidian_links_valid,
            ).to_payload(),
            "processed_sources": processed_sources,
            "duplicate_cards": duplicate_cards,
            "skipped_notes": skipped_notes,
            "model_error": model_error,
            "anki_errors": anki_errors,
        }
    )


def build_report(payload: JsonObject) -> JsonObject:
    return build_report_model(payload).to_payload()


def format_report(report: JsonObject) -> str:
    summary = _json_object_field(report, "summary")
    lines = [
        "Flashcards final report",
        f"- Notas processadas: {_json_int_field(summary, 'processed_note_count')}",
        f"- Cards criados: {_json_int_field(summary, 'created_card_count')}",
        f"- Cards pulados por duplicidade: {_json_int_field(summary, 'duplicate_card_count')}",
        f"- Notas puladas: {_json_int_field(summary, 'skipped_note_count')}",
        f"- Erros de modelo/campos: {_json_int_field(summary, 'model_error_count')}",
        f"- Erros do Anki MCP: {_json_int_field(summary, 'anki_error_count')}",
        f"- Links Obsidian validos: {'sim' if _json_field(summary, 'obsidian_links_valid') is True else 'nao'}",
    ]
    processed_sources = _json_field(report, "processed_sources")
    if isinstance(processed_sources, list) and processed_sources:
        lines.append("")
        lines.append("Fontes com cards criados:")
        lines.extend(f"- {source}" for source in processed_sources)
    skipped_notes = _json_object_list_field(report, "skipped_notes")
    if skipped_notes:
        lines.append("")
        lines.append("Notas puladas:")
        for note in skipped_notes:
            label = _json_str_field(note, "vault_relative_path") or _json_str_field(note, "path") or "nota"
            reason = _json_str_field(note, "skip_reason") or "skip"
            lines.append(f"- {label} ({reason})")
    model_error = _json_object_field(report, "model_error")
    if model_error:
        lines.append("")
        lines.append("Modelo Anki incompleto:")
        required_values = _json_field(model_error, "required_fields")
        required = ", ".join(str(item) for item in required_values) if isinstance(required_values, list) else ""
        lines.append(f"- Campos exigidos: {required}")
    return "\n".join(lines) + "\n"


def build_card_preview(payload: JsonObject) -> JsonObject:
    index_check = _json_object_field(payload, "index_check")
    _artifact_write_plan(payload)
    cards = _preview_cards(payload)
    duplicate_cards = (
        _cards(payload, "duplicate_cards")
        or _cards(index_check, "duplicate_cards")
        or _artifact_duplicate_cards(payload)
    )
    return {
        "schema": PREVIEW_SCHEMA,
        "summary": {
            "card_count": len(cards),
            "duplicate_card_count": len(duplicate_cards),
        },
        "cards": cards,
        "duplicate_cards": duplicate_cards,
    }


def format_card_preview(preview: JsonObject) -> str:
    summary = _json_object_field(preview, "summary")
    lines = [
        "Flashcards preview",
        f"- Cards candidatos para criar: {_json_int_field(summary, 'card_count')}",
        f"- Cards pulados por duplicidade local: {_json_int_field(summary, 'duplicate_card_count')}",
    ]
    for index, card in enumerate(_json_object_list_field(preview, "cards"), start=1):
        source = (
            _json_str_field(card, "source_path")
            or _json_str_field(card, "source")
            or _json_str_field(card, "source_relative_path")
        )
        deck = _json_str_field(card, "deck")
        model = _json_str_field(card, "note_model") or _json_str_field(card, "model")
        lines.extend(
            [
                "",
                f"Card {index}",
                f"Deck: {deck}",
                f"Modelo: {model}",
            ]
        )
        if source:
            lines.append(f"Fonte: {source}")
        lines.extend(
            [
                f"Frente: {_field(card, 'Frente')}",
                f"Verso: {_field(card, 'Verso')}",
            ]
        )
        extra = _field(card, "Verso Extra")
        if extra:
            lines.append(f"Verso Extra: {extra}")
        obsidian = _field(card, "Obsidian")
        if obsidian:
            lines.append(f"Obsidian: {obsidian}")
    return "\n".join(lines) + "\n"


def _cmd_final(args: argparse.Namespace) -> int:
    started_at = time.time()
    payload = _json_object(_read_json(args.input))
    report = build_report(payload)
    if args.json:
        _json(report)
    else:
        print(format_report(report), end="")
    _record_feedback(
        {
            **report,
            "phase": "flashcards_report_final",
            "status": "completed_with_warnings"
            if _json_int_field(_json_object_field(report, "summary"), "model_error_count")
            or _json_int_field(_json_object_field(report, "summary"), "anki_error_count")
            else "completed",
            "next_action": "Corrigir modelo Anki ou erros MCP antes de repetir."
            if _json_int_field(_json_object_field(report, "summary"), "model_error_count")
            or _json_int_field(_json_object_field(report, "summary"), "anki_error_count")
            else "",
            "required_inputs": ["run-result"],
        },
        EXIT_OK,
        started_at,
    )
    return EXIT_OK


def _cmd_preview_cards(args: argparse.Namespace) -> int:
    started_at = time.time()
    payload = _json_object(_read_json(args.input))
    preview = build_card_preview(payload)
    if args.json:
        _json(preview)
    else:
        print(format_card_preview(preview), end="")
    _record_feedback(
        {
            **preview,
            "phase": "flashcards_preview_cards",
            "status": "completed",
            "next_action": "Confirmar criacao dos cards ou revisar candidatos.",
            "required_inputs": ["candidate_cards"],
        },
        EXIT_OK,
        started_at,
    )
    return EXIT_OK


def _record_feedback(payload: JsonObject, exit_code: int, started_at: float) -> None:
    safe_record_workflow_run(
        workflow="/flashcards",
        command=command_string(),
        payload=payload,
        exit_code=exit_code,
        started_at=started_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    final = sub.add_parser("final", help="format a final /flashcards report")
    final.add_argument("--input", required=True, help="run-result JSON file, or '-' for stdin")
    final.add_argument("--json", action="store_true", help="emit structured JSON")
    final.set_defaults(func=_cmd_final)

    preview = sub.add_parser("preview-cards", help="format candidate cards before Anki writes")
    preview.add_argument("--input", required=True, help="candidate/write-plan JSON file, or '-' for stdin")
    preview.add_argument("--json", action="store_true", help="emit structured JSON")
    preview.set_defaults(func=_cmd_preview_cards)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, json.JSONDecodeError, FlashcardReportInputError) as exc:
        safe_record_workflow_run(
            workflow="/flashcards",
            command=command_string(),
            payload={
                "phase": f"flashcards_report_{getattr(args, 'command', 'unknown')}",
                "status": "failed",
                "blocked_reason": exc.__class__.__name__,
                "next_action": "Corrigir o JSON de entrada e gerar o relatorio novamente.",
                "error": str(exc),
            },
            exit_code=EXIT_IO,
            started_at=time.time(),
            snippets=[str(exc)],
        )
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
