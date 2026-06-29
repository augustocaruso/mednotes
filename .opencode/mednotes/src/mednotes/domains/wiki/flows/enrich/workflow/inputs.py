"""Input expansion and note path validation."""
from __future__ import annotations

import glob
import re
from pathlib import Path

from mednotes.domains.wiki.flows.enrich.workflow.models import NoteResult

_EXCLUDED_NOTE_DIR_NAMES = {
    ".git",
    ".hg",
    ".obsidian",
    ".svn",
    ".venv",
    "__pycache__",
    "_attachments",
    "anexos",
    "attachments",
    "build",
    "dist",
    "images",
    "node_modules",
    "venv",
}
_GLOB_CHARS_RE = re.compile(r"[*?\[]")


def _has_glob(value: str) -> bool:
    return bool(_GLOB_CHARS_RE.search(value))


def _sort_key(path: Path) -> str:
    return path.as_posix().casefold()


def _note_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _is_excluded_note_path(path: Path) -> bool:
    return any(part.casefold() in _EXCLUDED_NOTE_DIR_NAMES for part in path.parts)


def _markdown_files_in_dir(directory: Path) -> list[Path]:
    files = []
    for path in directory.rglob("*.md"):
        try:
            relative = path.relative_to(directory)
        except ValueError:
            relative = path
        if _is_excluded_note_path(relative):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=_sort_key)


def _resolve_note_inputs(inputs: list[Path]) -> tuple[list[Path], list[NoteResult]]:
    notes: list[Path] = []
    errors: list[NoteResult] = []
    seen: set[str] = set()

    def add_note(path: Path) -> None:
        key = _note_key(path)
        if key in seen:
            return
        seen.add(key)
        notes.append(path)

    def add_error(path: Path, message: str) -> None:
        errors.append(NoteResult(note=path, code=2, status="failed", message=message))

    for raw in inputs:
        path = raw.expanduser()
        raw_text = str(path)
        if _has_glob(raw_text):
            matches = sorted((Path(match) for match in glob.glob(raw_text, recursive=True)), key=_sort_key)
            if not matches:
                add_error(path, f"glob sem correspondências: {raw}")
                continue
            found: list[Path] = []
            for match in matches:
                if match.is_dir():
                    found.extend(_markdown_files_in_dir(match))
                elif match.is_file() and match.suffix.lower() == ".md":
                    if not _is_excluded_note_path(match):
                        found.append(match)
            if not found:
                add_error(path, f"glob não encontrou notas .md: {raw}")
                continue
            for note in sorted(found, key=_sort_key):
                add_note(note)
            continue

        if path.is_dir():
            found = _markdown_files_in_dir(path)
            if not found:
                add_error(path, f"diretório sem notas .md: {path}")
                continue
            for note in found:
                add_note(note)
            continue

        add_note(path)

    return notes, errors


def _validate_note_path(note: Path) -> str | None:
    if note.suffix.lower() != ".md":
        return f"caminho não é uma nota .md: {note}"
    if not note.exists():
        return f"caminho não encontrado: {note}"
    if not note.is_file():
        return f"caminho não é arquivo: {note}"
    return None
