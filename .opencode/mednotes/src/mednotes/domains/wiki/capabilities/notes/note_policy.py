"""Shared note classification policy for Wiki workflows."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note


class NoteKind(StrEnum):
    MEDICAL_WIKI = "medical_wiki"
    OPERATIONAL_INDEX = "operational_index"


def classify_note(path: Path, content: str) -> NoteKind:
    if is_index_note(path, content):
        return NoteKind.OPERATIONAL_INDEX
    return NoteKind.MEDICAL_WIKI


def is_operational_index_note(path: Path, content: str) -> bool:
    return classify_note(path, content) == NoteKind.OPERATIONAL_INDEX
