"""Shared note iteration helpers for Wiki_Medicina."""
from __future__ import annotations

import os
from pathlib import Path

from mednotes.platform.backup_policy import is_canonical_backup_path

IGNORED_DIR_PARTS = {
    ".git",
    ".obsidian",
    ".trash",
    ".stfolder",
    ".venv",
    "__pycache__",
    "node_modules",
}


def iter_notes(wiki_dir: Path) -> list[Path]:
    """Return real Markdown notes, excluding app state, backups and generated drafts."""
    if not wiki_dir.exists():
        return []
    notes: list[Path] = []
    for root, dirnames, filenames in os.walk(wiki_dir):
        dirnames[:] = [dirname for dirname in dirnames if not _is_ignored_part(dirname)]
        root_path = Path(root)
        for filename in filenames:
            if _is_ignored_part(filename) or not filename.lower().endswith(".md"):
                continue
            path = root_path / filename
            if ".bak" in filename or ".rewrite" in filename or is_canonical_backup_path(path):
                continue
            notes.append(path)
    return sorted(notes)


def is_note_markdown(wiki_dir: Path, path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".md":
        return False
    try:
        rel = path.relative_to(wiki_dir)
    except ValueError:
        return False
    if _is_ignored_relative_path(rel):
        return False
    name = path.name
    if ".bak" in name or ".rewrite" in name or is_canonical_backup_path(path):
        return False
    return True


def is_ignored_path(wiki_dir: Path, path: Path) -> bool:
    """Return True for app/system/runtime paths that are not Wiki notes."""
    try:
        rel = path.relative_to(wiki_dir)
    except ValueError:
        return True
    return _is_ignored_relative_path(rel)


def _is_ignored_part(part: str) -> bool:
    return part.startswith(".") or part in IGNORED_DIR_PARTS


def _is_ignored_relative_path(path: Path) -> bool:
    return any(_is_ignored_part(part) for part in path.parts)
