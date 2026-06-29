"""Taxonomy tree and dry-run audit helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.notes import note_style
from mednotes.domains.wiki.capabilities.notes.note_iter import is_note_markdown
from mednotes.domains.wiki.capabilities.notes.note_policy import is_operational_index_note
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.normalize import _fold_taxonomy_segment
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.resolve import _visible_child_dirs
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.schema import (
    CANONICAL_TAXONOMY,
    _canonical_area_aliases_by_fold,
    _canonical_roots_by_fold,
    _canonical_specialties_by_fold,
    canonical_taxonomy_tree,
)
from mednotes.domains.wiki.common import MissingPathError, ValidationError

IGNORED_TOP_LEVEL_DIRS = {"attachments", "_Mock_Embeds"}
IGNORED_ROOT_NOTES = {"_Índice_Medicina.md"}


def taxonomy_tree(wiki_dir: Path, max_depth: int = 0) -> dict[str, Any]:
    if not wiki_dir.exists():
        raise MissingPathError(f"Wiki dir not found: {wiki_dir}")
    if not wiki_dir.is_dir():
        raise ValidationError(f"Wiki dir is not a directory: {wiki_dir}")

    directories: list[dict[str, Any]] = []
    for path in sorted((p for p in wiki_dir.rglob("*") if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.as_posix()):
        rel = path.relative_to(wiki_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        depth = len(rel.parts)
        if max_depth and depth > max_depth:
            continue
        direct_notes = sum(1 for child in path.glob("*.md") if is_note_markdown(wiki_dir, child))
        child_dirs = sum(1 for child in path.iterdir() if child.is_dir() and not child.name.startswith("."))
        directories.append(
            {
                "path": rel.as_posix(),
                "parts": list(rel.parts),
                "depth": depth,
                "direct_note_count": direct_notes,
                "child_dir_count": child_dirs,
            }
        )
    return {"wiki_dir": str(wiki_dir), "directory_count": len(directories), "directories": directories}


def _canonical_directory_paths() -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for root, specialties in CANONICAL_TAXONOMY:
        paths.append((root,))
        paths.extend((root, specialty) for specialty in specialties)
    return paths


def taxonomy_audit(wiki_dir: Path) -> dict[str, Any]:
    if not wiki_dir.exists():
        raise MissingPathError(f"Wiki dir not found: {wiki_dir}")
    if not wiki_dir.is_dir():
        raise ValidationError(f"Wiki dir is not a directory: {wiki_dir}")

    roots = _canonical_roots_by_fold()
    area_aliases = _canonical_area_aliases_by_fold()
    specialties = _canonical_specialties_by_fold()
    canonical_paths = _canonical_directory_paths()
    missing_canonical_dirs = [
        "/".join(parts)
        for parts in canonical_paths
        if not wiki_dir.joinpath(*parts).exists()
    ]

    proposed_moves: list[dict[str, Any]] = []
    compliant_top_level_dirs: list[str] = []
    unmapped_top_level_dirs: list[str] = []
    top_level_dirs = _visible_child_dirs(wiki_dir)
    destinations: dict[str, list[str]] = {}

    for directory in top_level_dirs:
        if directory.name in IGNORED_TOP_LEVEL_DIRS:
            continue
        folded = _fold_taxonomy_segment(directory.name)
        rel_source = directory.relative_to(wiki_dir).as_posix()
        if folded in roots:
            compliant_top_level_dirs.append(rel_source)
            continue
        if folded in area_aliases:
            destination = area_aliases[folded]
            destinations.setdefault(destination, []).append(rel_source)
            proposed_moves.append(
                {
                    "source": rel_source,
                    "destination": destination,
                    "reason": "known_area_alias",
                    "destination_exists": wiki_dir.joinpath(destination).exists(),
                }
            )
            continue
        if folded in specialties:
            root, specialty = specialties[folded]
            destination = f"{root}/{specialty}"
            destinations.setdefault(destination, []).append(rel_source)
            proposed_moves.append(
                {
                    "source": rel_source,
                    "destination": destination,
                    "reason": "known_specialty_or_alias",
                    "destination_exists": wiki_dir.joinpath(root, specialty).exists(),
                }
            )
        else:
            unmapped_top_level_dirs.append(rel_source)

    duplicate_destinations = [
        {"destination": destination, "sources": sources}
        for destination, sources in sorted(destinations.items())
        if len(sources) > 1
    ]
    duplicate_directory_groups: list[dict[str, Any]] = []
    by_folded: dict[str, list[str]] = {}
    for path in sorted((p for p in wiki_dir.rglob("*") if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.as_posix()):
        rel = path.relative_to(wiki_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if len(rel.parts) == 1 and path.name in IGNORED_TOP_LEVEL_DIRS:
            continue
        by_folded.setdefault(_fold_taxonomy_segment(path.name), []).append(rel.as_posix())
    for folded, paths in sorted(by_folded.items()):
        if len(paths) > 1:
            duplicate_directory_groups.append({"key": folded, "paths": paths})

    empty_root_notes = sorted(
        path.name
        for path in wiki_dir.glob("*.md")
        if is_note_markdown(wiki_dir, path)
        and path.name not in IGNORED_ROOT_NOTES
        and not _is_operational_index_note(path)
        and _is_strictly_empty(path)
    )
    invalid_root_notes = sorted(
        path.name
        for path in wiki_dir.glob("*.md")
        if is_note_markdown(wiki_dir, path)
        and path.name not in IGNORED_ROOT_NOTES
        and not _is_operational_index_note(path)
        and not _is_strictly_empty(path)
        and not _is_valid_root_note(path)
    )
    root_notes = sorted(
        path.name
        for path in wiki_dir.glob("*.md")
        if is_note_markdown(wiki_dir, path)
        and path.name not in IGNORED_ROOT_NOTES
        and not _is_operational_index_note(path)
        and not _is_strictly_empty(path)
        and _is_valid_root_note(path)
    )
    return {
        "wiki_dir": str(wiki_dir),
        "canonical_taxonomy": canonical_taxonomy_tree(),
        "missing_canonical_dirs": missing_canonical_dirs,
        "compliant_top_level_dirs": compliant_top_level_dirs,
        "proposed_moves": proposed_moves,
        "unmapped_top_level_dirs": unmapped_top_level_dirs,
        "duplicate_destinations": duplicate_destinations,
        "duplicate_directory_groups": duplicate_directory_groups,
        "root_notes": root_notes,
        "empty_root_notes": empty_root_notes,
        "invalid_root_notes": invalid_root_notes,
        "requires_review": bool(unmapped_top_level_dirs or duplicate_destinations or root_notes),
        "dry_run_only": True,
    }


def _is_strictly_empty(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == ""
    except OSError:
        return False


def _is_valid_root_note(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    title = note_style.infer_title(content, path)
    report = note_style.validate_note_style(content, title=title, path=str(path))
    return bool(report.get("ok"))


def _is_operational_index_note(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return is_operational_index_note(path, content)
