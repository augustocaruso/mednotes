"""Trigger context helpers for link graph repair."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, TypeAdapter, model_validator
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import ValidationError
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

LINK_TRIGGER_CONTEXT_SCHEMA = "medical-notes-workbench.link-trigger-context.v1"

ChangeType = Literal["created", "modified", "deleted", "renamed", "moved", "merged"]
ContentChange = Literal["text", "metadata", "structural", "image_only"]
_TRIGGER_BY_CHANGE = {
    "created": "note_created",
    "modified": "note_modified",
    "deleted": "note_deleted",
    "renamed": "note_renamed",
    "moved": "note_moved",
    "merged": "note_merged",
}


class LinkTriggerChangedNote(ContractModel):
    """One changed-note record that may force graph or Related Notes repair."""

    change_type: ChangeType
    content_change: ContentChange = "text"
    path: StrictStr | None = None
    old_path: StrictStr | None = None
    title: StrictStr | None = None
    old_title: StrictStr | None = None
    replacement_path: StrictStr | None = None
    replacement_title: StrictStr | None = None
    before_hash: StrictStr | None = None
    after_hash: StrictStr | None = None
    reason: StrictStr | None = None
    reasons: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _structural_changes_name_origin_and_destination(self) -> LinkTriggerChangedNote:
        if self.change_type in {"renamed", "moved", "merged"}:
            has_old = bool(self.old_title or self.old_path)
            has_new = bool(self.title or self.path or self.replacement_title or self.replacement_path)
            if not has_old or not has_new:
                raise ValueError(f"link trigger context {self.change_type} exige origem e destino explícitos.")
        if self.change_type == "deleted" and not (self.old_title or self.old_path):
            raise ValueError("link trigger context deleted exige old_title ou old_path.")
        return self

    def clean_payload(self) -> JsonObject:
        payload = self.model_dump(mode="json", exclude_none=True)
        if not self.reasons:
            payload.pop("reasons", None)
        return JsonObjectAdapter.validate_python(payload)

    def affected_path(self) -> str:
        return self.path or self.old_path or ""


class LinkTriggerContext(ContractModel):
    """Closed boundary for workflow-provided linker trigger context."""

    schema_id: Literal["medical-notes-workbench.link-trigger-context.v1"] = Field(
        default=LINK_TRIGGER_CONTEXT_SCHEMA,
        alias="schema",
    )
    source_workflow: StrictStr = Field(min_length=1)
    changed_notes: list[LinkTriggerChangedNote]
    catalog_changed: StrictBool = False
    related_notes_export_changed: StrictBool = False
    batch_id: StrictStr | None = None

    def clean_payload(self) -> JsonObject:
        payload = {
            "schema": self.schema_id,
            "source_workflow": self.source_workflow,
            "changed_notes": [note.clean_payload() for note in self.changed_notes],
            "catalog_changed": self.catalog_changed,
            "related_notes_export_changed": self.related_notes_export_changed,
        }
        if self.batch_id:
            payload["batch_id"] = self.batch_id
        return JsonObjectAdapter.validate_python(payload)

    def image_only(self) -> bool:
        if self.catalog_changed or self.related_notes_export_changed:
            return False
        return bool(self.changed_notes) and all(note.content_change == "image_only" for note in self.changed_notes)


LinkTriggerContextAdapter = TypeAdapter(LinkTriggerContext)


def _trigger_context(payload: object) -> LinkTriggerContext:
    try:
        return LinkTriggerContextAdapter.validate_python(payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
        message = str(first.get("msg") or "payload inválido")
        raise ValidationError(f"link trigger context inválido: {loc}: {message}") from exc


def _trigger_context_or_none(payload: object | None) -> LinkTriggerContext | None:
    return None if payload is None else _trigger_context(payload)


def validate_trigger_context(payload: object) -> JsonObject:
    return _trigger_context(payload).clean_payload()


def load_trigger_context(path: Path | None) -> JsonObject | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Trigger context não encontrado: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Trigger context inválido: {path}: {exc}") from exc
    return validate_trigger_context(payload)


def write_trigger_context(path: Path, payload: object) -> Path:
    clean = validate_trigger_context(payload)
    atomic_write_text(path, json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
    return path


def derive_triggers(payload: object | None) -> list[str]:
    context = _trigger_context_or_none(payload)
    if context is None:
        return ["manual_request"]
    if context.image_only():
        return ["image_only_change"]
    triggers: list[str] = []
    for note in context.changed_notes:
        trigger = _TRIGGER_BY_CHANGE[note.change_type]
        if trigger not in triggers:
            triggers.append(trigger)
    if context.catalog_changed:
        triggers.append("catalog_changed")
    if context.related_notes_export_changed:
        triggers.append("related_notes_export_changed")
    return triggers


def is_image_only_context(payload: object | None) -> bool:
    context = _trigger_context_or_none(payload)
    return False if context is None else context.image_only()


def affected_notes_from_context(payload: object | None) -> list[JsonObject]:
    context = _trigger_context_or_none(payload)
    if context is None:
        return []
    affected: list[JsonObject] = []
    for note in context.changed_notes:
        path = note.affected_path()
        entry: JsonObject = {
            "reason": note.change_type,
        }
        if path:
            entry["path"] = path
        affected.append(JsonObjectAdapter.validate_python(entry))
    return affected


def structural_events_from_context(payload: object | None) -> list[JsonObject]:
    context = _trigger_context_or_none(payload)
    if context is None:
        return []
    events: list[JsonObject] = []
    for note in context.changed_notes:
        if note.change_type in {"deleted", "renamed", "moved", "merged"}:
            events.append(note.clean_payload())
    return events
