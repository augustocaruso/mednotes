#!/usr/bin/env python3
"""Local idempotency index for /flashcards candidate cards.

This script does not talk to Anki. It owns a deterministic local index used by
the Gemini command before/after Anki MCP writes:

- `check` filters candidate cards into new vs duplicate.
- `record` stores cards that Anki accepted.
- `summary` reports the current index.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import TypeAliasType

SCHEMA = "medical-notes-workbench.flashcards-index.v1"
CHECK_SCHEMA = "medical-notes-workbench.flashcards-index-check.v1"
SOURCE_STATUS_SCHEMA = "medical-notes-workbench.flashcards-source-status.v1"
DEFAULT_INDEX = "~/.mednotes/FLASHCARDS_INDEX.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_IO = 5


class IndexErrorWithCode(Exception):
    exit_code = EXIT_IO


class UsageError(IndexErrorWithCode):
    exit_code = EXIT_USAGE


JsonValue = TypeAliasType(
    "JsonValue",
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)
JsonObject = dict[str, JsonValue]
JsonValueAdapter = TypeAdapter(JsonValue)
JsonObjectAdapter = TypeAdapter(JsonObject)
JsonArrayAdapter = TypeAdapter(list[JsonValue])


class _FieldModel(BaseModel):
    # Flashcards index is a CLI boundary: parse permissively at the edge, then
    # use only declared fields for decisions and hashes.
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    def to_payload(self) -> JsonObject:
        return _json_object(self.model_dump(mode="json", by_alias=True), prefix=type(self).__name__)


class _SourceIndexRecordFields(_FieldModel):
    content_sha256: str = ""
    card_hashes: list[str] = Field(default_factory=list)


class _FlashcardIndexCardRecord(_FieldModel):
    card_hash: str = ""
    recorded_at: str = ""
    source_path: str = ""
    source_relative_path: str = ""
    source_content_sha256: str = ""
    source_excerpt: str = ""
    deck: str = ""
    note_model: str = ""
    front: str = ""
    back: str = ""
    extra: str = ""
    obsidian: str = ""


class _FlashcardIndexSourceRecord(_FieldModel):
    path: str = ""
    vault_relative_path: str = ""
    content_sha256: str = ""
    card_hashes: list[str] = Field(default_factory=list)
    updated_at: str = ""


class _FlashcardIndex(_FieldModel):
    schema_id: Literal["medical-notes-workbench.flashcards-index.v1"] = Field(
        default=SCHEMA,
        alias="schema",
    )
    version: int = 1
    updated_at: str | None = None
    cards: dict[str, _FlashcardIndexCardRecord] = Field(default_factory=dict)
    sources: dict[str, _FlashcardIndexSourceRecord] = Field(default_factory=dict)


class _SourceNote(_FieldModel):
    path: str = ""
    vault_relative_path: str = ""
    content_sha256: str = ""
    deck: str = ""
    deeplink: str = ""

    @field_validator("path", "vault_relative_path", "content_sha256", "deck", "deeplink", mode="before")
    @classmethod
    def _text_or_empty(cls, value: object) -> str:
        return "" if value is None else str(value)


class _SourceManifest(_FieldModel):
    notes: list[_SourceNote] = Field(default_factory=list)

    def notes_by_path(self) -> dict[str, _SourceNote]:
        return {note.path: note for note in self.notes if note.path}


class _CardFields(_FieldModel):
    fields: JsonObject = Field(default_factory=dict)


class _CandidateCard(_FieldModel):
    source_path: str = ""
    source: str = ""
    path: str = ""
    source_relative_path: str = ""
    vault_relative_path: str = ""
    source_content_sha256: str = ""
    content_sha256: str = ""
    note_sha256: str = ""
    source_excerpt: str = ""
    trecho: str = ""
    deck: str = ""
    note_model: str = ""
    model: str = ""
    fields: JsonObject = Field(default_factory=dict)
    card_hash: str = ""

    @field_validator(
        "source_path",
        "source",
        "path",
        "source_relative_path",
        "vault_relative_path",
        "source_content_sha256",
        "content_sha256",
        "note_sha256",
        "source_excerpt",
        "trecho",
        "deck",
        "note_model",
        "model",
        "card_hash",
        mode="before",
    )
    @classmethod
    def _text_or_empty(cls, value: object) -> str:
        return "" if value is None else str(value)

    @property
    def resolved_source_path(self) -> str:
        return self.source_path or self.source or self.path

    @property
    def resolved_relative_path(self) -> str:
        return self.source_relative_path or self.vault_relative_path

    @property
    def resolved_content_sha(self) -> str:
        return self.source_content_sha256 or self.content_sha256 or self.note_sha256

    @property
    def resolved_note_model(self) -> str:
        return self.note_model or self.model

    def with_source_note(self, source_note: _SourceNote | None) -> _CandidateCard:
        if source_note is None:
            return self
        fields = dict(self.fields)
        if "Obsidian" not in fields and source_note.deeplink:
            fields["Obsidian"] = source_note.deeplink
        return self.model_copy(
            update={
                "source_content_sha256": self.resolved_content_sha or source_note.content_sha256,
                "source_relative_path": self.resolved_relative_path or source_note.vault_relative_path,
                "deck": self.deck or source_note.deck,
                "fields": fields,
            }
        )

    def output_payload(self, digest: str) -> JsonObject:
        payload: JsonObject = {
            "source_path": self.resolved_source_path,
            "source_relative_path": self.resolved_relative_path,
            "source_content_sha256": self.resolved_content_sha,
            "deck": self.deck,
            "note_model": self.resolved_note_model,
            "fields": self.fields,
            "card_hash": digest,
        }
        if self.source_excerpt:
            payload["source_excerpt"] = self.source_excerpt
        return payload


class _CandidatePayload(_FieldModel):
    source_manifest: _SourceManifest | None = None
    candidate_cards: list[_CandidateCard] = Field(default_factory=list)
    accepted_cards: list[_CandidateCard] = Field(default_factory=list)
    new_cards: list[_CandidateCard] = Field(default_factory=list)
    cards: list[_CandidateCard] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_source_manifest(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "source_manifest" in value:
            return value
        if "notes" in value:
            normalized = dict(value)
            normalized["source_manifest"] = {"notes": value["notes"]}
            return normalized
        return value

    def selected_cards(self) -> list[_CandidateCard]:
        for cards in (self.candidate_cards, self.accepted_cards, self.new_cards, self.cards):
            if cards:
                return cards
        return []

    def notes_by_path(self) -> dict[str, _SourceNote]:
        if self.source_manifest is None:
            return {}
        return self.source_manifest.notes_by_path()


class _NormalizedCard(_FieldModel):
    source_path: str = ""
    source_relative_path: str = ""
    source_content_sha256: str = ""
    source_excerpt: str = ""
    deck: str = ""
    note_model: str = ""
    front: str = ""
    back: str = ""
    extra: str = ""
    obsidian: str = ""


class _RecordSummary(_FieldModel):
    accepted_count: int = Field(ge=0)
    added_count: int = Field(ge=0)
    already_present_count: int = Field(ge=0)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _validation_error(exc: PydanticValidationError, *, prefix: str) -> UsageError:
    first = exc.errors()[0] if exc.errors() else {}
    loc_parts = first["loc"] if "loc" in first else ()
    loc = ".".join(str(part) for part in loc_parts) or "$"
    msg = str(first["msg"] if "msg" in first else str(exc))
    return UsageError(f"{prefix}: {loc}: {msg}")


def _json_object(value: object, *, prefix: str) -> JsonObject:
    try:
        return JsonObjectAdapter.validate_python(value)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix=prefix) from exc


def _json_array(value: object, *, prefix: str) -> list[JsonValue]:
    try:
        return JsonArrayAdapter.validate_python(value)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix=prefix) from exc


def _source_index_record(value: JsonValue, *, label: str) -> _SourceIndexRecordFields | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UsageError(f"{label}: source record must be an object")
    payload = _json_object(value, prefix=label)
    fields: JsonObject = {}
    if "content_sha256" in payload:
        fields["content_sha256"] = payload["content_sha256"]
    if "card_hashes" in payload:
        fields["card_hashes"] = payload["card_hashes"]
    try:
        return _SourceIndexRecordFields.model_validate(fields)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix=label) from exc


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _index_path(value: str | None = None) -> Path:
    return _path(value or os.getenv("MED_FLASHCARDS_INDEX") or DEFAULT_INDEX)


def _read_json(path: str | Path) -> JsonValue:
    if str(path) == "-":
        raw = json.loads(sys.stdin.read())
    else:
        with Path(path).open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    try:
        return JsonValueAdapter.validate_python(raw)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix=f"JSON payload {path}") from exc


def _write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _index_model(value: object) -> _FlashcardIndex:
    try:
        return _FlashcardIndex.model_validate(value)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix="flashcards index") from exc


def _load_index(path: Path) -> JsonObject:
    if not path.exists():
        return _FlashcardIndex().to_payload()
    data = _read_json(path)
    if not isinstance(data, dict) or "schema" not in data or data["schema"] != SCHEMA:
        raise UsageError(f"Unsupported flashcards index schema in {path}")
    return _index_model(data).to_payload()


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.strip())


def _value_for_name(values: JsonObject, names: list[str]) -> JsonValue:
    casefold_values = {str(key).casefold(): value for key, value in values.items()}
    for name in names:
        if name in values:
            return values[name]
        lowered = name.casefold()
        if lowered in casefold_values:
            return casefold_values[lowered]
    return ""


def _field_from(card: _CandidateCard, names: list[str]) -> str:
    return _clean_text(_value_for_name(card.fields, names))


def normalize_card(card: object) -> dict[str, str]:
    candidate = _candidate_card(card, label="flashcard candidate")
    normalized = _NormalizedCard(
        source_path=candidate.resolved_source_path,
        source_relative_path=candidate.resolved_relative_path,
        source_content_sha256=candidate.resolved_content_sha,
        source_excerpt=_clean_text(candidate.source_excerpt or candidate.trecho),
        deck=_clean_text(candidate.deck),
        note_model=_clean_text(candidate.resolved_note_model),
        front=_field_from(candidate, ["Frente", "Front", "front", "pergunta"]),
        back=_field_from(candidate, ["Verso", "Back", "back", "resposta"]),
        extra=_field_from(candidate, ["Verso Extra", "Extra", "extra", "verso_extra"]),
        obsidian=_field_from(candidate, ["Obsidian", "obsidian"]),
    )
    return {key: str(value) for key, value in normalized.to_payload().items()}


def card_hash(card: object) -> str:
    normalized = normalize_card(card)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _payload_model(payload: object) -> _CandidatePayload:
    try:
        return _CandidatePayload.model_validate(payload)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix="flashcards candidate payload") from exc


def _source_notes(payload: object) -> dict[str, _SourceNote]:
    return _payload_model(payload).notes_by_path()


def _candidate_card(value: object, *, label: str) -> _CandidateCard:
    try:
        return _CandidateCard.model_validate(value)
    except PydanticValidationError as exc:
        raise _validation_error(exc, prefix=label) from exc


def _candidate_cards(payload: object) -> list[_CandidateCard]:
    if isinstance(payload, list):
        return [_candidate_card(card, label="flashcards candidate") for card in _json_array(payload, prefix="candidate cards")]

    parsed = _payload_model(payload)
    source_notes = parsed.notes_by_path()
    normalized_cards: list[_CandidateCard] = []
    for card in parsed.selected_cards():
        source_path = card.resolved_source_path
        source_note = source_notes[source_path] if source_path in source_notes else None
        normalized_cards.append(card.with_source_note(source_note))
    return normalized_cards


def check_candidates(payload: object, index: object) -> JsonObject:
    known = _index_model(index).cards
    new_cards: list[JsonObject] = []
    duplicate_cards: list[JsonObject] = []
    for card in _candidate_cards(payload):
        digest = card.card_hash or card_hash(card.to_payload())
        record = card.output_payload(digest)
        if digest in known:
            duplicate_cards.append({**record, "duplicate_of": digest})
        else:
            new_cards.append(record)
    return _json_object({
        "schema": CHECK_SCHEMA,
        "summary": {
            "candidate_count": len(new_cards) + len(duplicate_cards),
            "new_count": len(new_cards),
            "duplicate_count": len(duplicate_cards),
        },
        "new_cards": new_cards,
        "duplicate_cards": duplicate_cards,
    }, prefix="flashcards index check")


def record_cards(payload: object, index: object) -> tuple[JsonObject, JsonObject]:
    cards = _candidate_cards(payload)
    index_model = _index_model(index)
    now = _now_iso()
    added = 0
    already_present = 0
    for card in cards:
        digest = card.card_hash or card_hash(card.to_payload())
        normalized = normalize_card(card)
        if digest in index_model.cards:
            already_present += 1
        else:
            added += 1
        index_model.cards[digest] = _FlashcardIndexCardRecord(
            card_hash=digest,
            recorded_at=now,
            **normalized,
        )
        source_key = normalized["source_path"] or normalized["source_relative_path"] or "__unknown__"
        if source_key not in index_model.sources:
            index_model.sources[source_key] = _FlashcardIndexSourceRecord(
                path=normalized["source_path"],
                vault_relative_path=normalized["source_relative_path"],
                content_sha256=normalized["source_content_sha256"],
                updated_at=now,
            )
        source = index_model.sources[source_key]
        source.content_sha256 = normalized["source_content_sha256"]
        source.updated_at = now
        if digest not in source.card_hashes:
            source.card_hashes.append(digest)
    index_model.updated_at = now
    summary = _RecordSummary(
        accepted_count=len(cards),
        added_count=added,
        already_present_count=already_present,
    )
    return index_model.to_payload(), summary.to_payload()


def source_status(payload: object, index: object) -> JsonObject:
    notes = _source_notes(payload)
    records: list[JsonObject] = []
    new_count = 0
    unchanged_count = 0
    changed_count = 0
    sources = _index_model(index).sources

    for path, note in sorted(notes.items()):
        relative = note.vault_relative_path
        current_sha = note.content_sha256
        source_record = sources[path] if path in sources else sources[relative] if relative in sources else None
        existing = _source_index_record(
            source_record.to_payload() if source_record is not None else None,
            label=f"flashcards source {path}",
        )
        if not existing:
            status = "new"
            new_count += 1
        elif existing.content_sha256 == current_sha:
            status = "unchanged"
            unchanged_count += 1
        else:
            status = "changed"
            changed_count += 1
        records.append(
            {
                "path": path,
                "vault_relative_path": relative,
                "status": status,
                "current_content_sha256": current_sha,
                "indexed_content_sha256": existing.content_sha256 if existing else "",
                "indexed_card_count": len(existing.card_hashes) if existing else 0,
            }
        )

    summary: JsonObject = {
        "new_count": new_count,
        "unchanged_count": unchanged_count,
        "changed_count": changed_count,
    }
    return _json_object({"schema": SOURCE_STATUS_SCHEMA, "summary": summary, "sources": records}, prefix="source status")


def _cmd_check(args: argparse.Namespace) -> int:
    path = _index_path(args.index)
    result = check_candidates(_read_json(args.candidates), _load_index(path))
    result["index_path"] = str(path)
    _json(result)
    return EXIT_OK


def _cmd_source_status(args: argparse.Namespace) -> int:
    path = _index_path(args.index)
    result = source_status(_read_json(args.manifest), _load_index(path))
    result["index_path"] = str(path)
    _json(result)
    return EXIT_OK


def _cmd_record(args: argparse.Namespace) -> int:
    path = _index_path(args.index)
    index = _load_index(path)
    updated, summary = record_cards(_read_json(args.accepted), index)
    result = {
        "schema": SCHEMA,
        "index_path": str(path),
        "dry_run": args.dry_run,
        "summary": summary,
    }
    if not args.dry_run:
        _write_json_atomic(path, updated)
    _json(result)
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    path = _index_path(args.index)
    index = _index_model(_load_index(path))
    _json(
        {
            "schema": SCHEMA,
            "index_path": str(path),
            "updated_at": index.updated_at,
            "card_count": len(index.cards),
            "source_count": len(index.sources),
        }
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="split candidate cards into new and duplicate")
    check.add_argument("--candidates", required=True, help="candidate-card JSON file, or '-' for stdin")
    check.add_argument("--index", help=f"index path; default {DEFAULT_INDEX}")
    check.set_defaults(func=_cmd_check)

    status = sub.add_parser("source-status", help="compare source note hashes against the index")
    status.add_argument("--manifest", required=True, help="source manifest JSON file, or '-' for stdin")
    status.add_argument("--index", help=f"index path; default {DEFAULT_INDEX}")
    status.set_defaults(func=_cmd_source_status)

    record = sub.add_parser("record", help="record cards accepted by Anki")
    record.add_argument("--accepted", required=True, help="accepted-card JSON file, or '-' for stdin")
    record.add_argument("--index", help=f"index path; default {DEFAULT_INDEX}")
    record.add_argument("--dry-run", action="store_true", help="report without writing the index")
    record.set_defaults(func=_cmd_record)

    summary = sub.add_parser("summary", help="summarize the local flashcards index")
    summary.add_argument("--index", help=f"index path; default {DEFAULT_INDEX}")
    summary.set_defaults(func=_cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IndexErrorWithCode as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
