"""Vault hygiene checks and cleanup for ``fix-wiki``."""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from mednotes.domains.wiki.capabilities.notes.note_iter import is_note_markdown, iter_notes
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_target, normalize_key
from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.platform.backup_policy import (
    LEGACY_BACKUP_CLEANUP_SCHEMA,
    archive_legacy_backups,
    collect_legacy_backup_candidates,
    is_canonical_backup_path,
)

WIKI_HYGIENE_SCHEMA = "medical-notes-workbench.wiki-hygiene.v1"
WIKI_HYGIENE_CLEANUP_SCHEMA = "medical-notes-workbench.wiki-hygiene-cleanup.v1"
ROOT_HYGIENE_AUDIT_SCHEMA = "medical-notes-workbench.root-hygiene-audit.v1"
IGNORED_EMPTY_DIR_NAMES = {"attachments", "_Mock_Embeds"}
ROOT_HYGIENE_SUFFIXES = {".py", ".json", ".md", ".bak", ".log", ".ps1", ".cmd", ".sh"}
ROOT_HYGIENE_AGENT_MARKERS = (
    "medical-notes-workbench",
    "mednotes",
    "Wiki_Medicina",
    "related-notes",
    "super_linker",
)


@dataclass(frozen=True)
class _HygieneNoteSnapshot:
    path: Path
    content: str
    sha256: str
    is_index_note: bool


class _BackupCleanupSummaryFields(ContractModel):
    """Typed summary consumed from retired-backup hygiene reports."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    candidate_count: int = Field(default=0, ge=0, strict=True)
    ambiguous_count: int = Field(default=0, ge=0, strict=True)
    archived_count: int = Field(default=0, ge=0, strict=True)
    archived: list[JsonObject] = Field(default_factory=list)
    errors: list[JsonObject] = Field(default_factory=list)


def collect_wiki_hygiene(wiki_dir: Path, *, sample_limit: int = 20) -> dict[str, Any]:
    note_snapshots = _note_snapshots(wiki_dir)
    backup_files = _backup_or_temp_files(wiki_dir, kind="backup")
    rewrite_files = _backup_or_temp_files(wiki_dir, kind="rewrite")
    legacy_backups: JsonObject = (
        collect_legacy_backup_candidates(wiki_dir, sample_limit=sample_limit)
        if wiki_dir.exists()
        else {
            "schema": LEGACY_BACKUP_CLEANUP_SCHEMA,
            "root": str(wiki_dir),
            "candidate_count": 0,
            "ambiguous_count": 0,
            "high_confidence_count": 0,
            "high_confidence": [],
            "ambiguous": [],
        }
    )
    legacy_fields = _BackupCleanupSummaryFields.model_validate(legacy_backups)
    empty_dirs = _empty_dirs(wiki_dir)
    duplicate_hash_groups = _duplicate_hash_groups_from_snapshots(wiki_dir, note_snapshots)
    duplicate_filename_groups = _duplicate_filename_groups_from_snapshots(wiki_dir, note_snapshots)
    depth_issues = _note_depth_issues_from_snapshots(wiki_dir, note_snapshots)
    loose_root_notes = _loose_root_note_snapshots(wiki_dir, note_snapshots)
    empty_root_notes = [snapshot.path for snapshot in loose_root_notes if snapshot.content.strip() == ""]
    legacy_count = legacy_fields.candidate_count

    return {
        "schema": WIKI_HYGIENE_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "bak_or_rewrite": len(backup_files) + len(rewrite_files) + legacy_count,
        "backup_files": _rel_sample(wiki_dir, backup_files, sample_limit),
        "rewrite_files": _rel_sample(wiki_dir, rewrite_files, sample_limit),
        "backup_file_count": len(backup_files),
        "rewrite_file_count": len(rewrite_files),
        "legacy_backup_cleanup": legacy_backups,
        "legacy_backup_candidate_count": legacy_count,
        "legacy_backup_ambiguous_count": legacy_fields.ambiguous_count,
        "empty_dirs": len(empty_dirs),
        "empty_dir_paths": _rel_sample(wiki_dir, empty_dirs, sample_limit),
        "duplicate_hash_groups": len(duplicate_hash_groups),
        "duplicate_hash_samples": duplicate_hash_groups[:sample_limit],
        "duplicate_filename_groups": len(duplicate_filename_groups),
        "duplicate_filename_samples": duplicate_filename_groups[:sample_limit],
        "note_depth_issues": len(depth_issues),
        "note_depth_samples": depth_issues[:sample_limit],
        "loose_root_note_count": len(loose_root_notes),
        "loose_root_note_samples": _rel_sample(wiki_dir, [snapshot.path for snapshot in loose_root_notes], sample_limit),
        "empty_root_note_count": len(empty_root_notes),
        "empty_root_note_samples": _rel_sample(wiki_dir, empty_root_notes, sample_limit),
    }


def cleanup_wiki_hygiene(
    wiki_dir: Path,
    *,
    archive_root: Path,
    archive_backups: bool = True,
    remove_rewrites: bool = True,
    remove_empty_dirs: bool = True,
    remove_empty_root_notes: bool = True,
    sample_limit: int = 20,
) -> dict[str, Any]:
    archive_root.mkdir(parents=True, exist_ok=True)
    archived: list[dict[str, str]] = []
    archived_total_count = 0
    removed_rewrites: list[str] = []
    removed_empty_dirs: list[str] = []
    removed_empty_dir_entries: list[dict[str, str]] = []
    removed_empty_root_notes: list[str] = []
    removed_empty_root_note_entries: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    legacy_cleanup: JsonObject | None = None

    if remove_empty_root_notes:
        for path in _empty_root_note_files(wiki_dir):
            try:
                archived.append(_archive_file(wiki_dir, path, archive_root))
                archived_total_count += 1
                rel = path.relative_to(wiki_dir).as_posix()
                removed_empty_root_notes.append(rel)
                removed_empty_root_note_entries.append(
                    {
                        "path": rel,
                        "action": "archive_empty_root_note",
                        "problem_code": "structure.empty_root_note.present",
                        "phase": "structure_empty_root_note_cleanup",
                    }
                )
            except OSError as exc:
                errors.append({"path": str(path), "operation": "archive_empty_root_note", "error": str(exc)})

    if archive_backups:
        for path in _backup_or_temp_files(wiki_dir, kind="backup"):
            try:
                archived.append(_archive_file(wiki_dir, path, archive_root))
                archived_total_count += 1
            except OSError as exc:
                errors.append({"path": str(path), "operation": "archive_backup", "error": str(exc)})
        try:
            legacy_cleanup = archive_legacy_backups(
                wiki_dir,
                archive_root=archive_root,
                apply=True,
                sample_limit=sample_limit,
            )
            legacy_cleanup_fields = _BackupCleanupSummaryFields.model_validate(legacy_cleanup)
            archived.extend(legacy_cleanup_fields.archived)
            archived_total_count += legacy_cleanup_fields.archived_count
            errors.extend(legacy_cleanup_fields.errors)
        except OSError as exc:
            errors.append({"path": str(wiki_dir), "operation": "archive_legacy_backups", "error": str(exc)})

    if remove_rewrites:
        for path in _backup_or_temp_files(wiki_dir, kind="rewrite"):
            try:
                archived.append(_archive_file(wiki_dir, path, archive_root))
                archived_total_count += 1
                removed_rewrites.append(path.relative_to(wiki_dir).as_posix())
            except OSError as exc:
                errors.append({"path": str(path), "operation": "archive_rewrite", "error": str(exc)})

    if remove_empty_dirs:
        for path in sorted(_empty_dirs(wiki_dir), key=lambda item: len(item.relative_to(wiki_dir).parts), reverse=True):
            try:
                path.rmdir()
                rel = path.relative_to(wiki_dir).as_posix()
                removed_empty_dirs.append(rel)
                removed_empty_dir_entries.append(
                    {
                        "path": rel,
                        "action": "remove_empty_dir",
                        "problem_code": "structure.empty_dir.present",
                        "phase": "structure_empty_dir_cleanup",
                    }
                )
            except OSError as exc:
                errors.append({"path": str(path), "operation": "remove_empty_dir", "error": str(exc)})

    return {
        "schema": WIKI_HYGIENE_CLEANUP_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "archive_root": str(archive_root),
        "archived_count": archived_total_count,
        "archived": archived[:sample_limit],
        "legacy_backup_cleanup": legacy_cleanup,
        "legacy_backup_ambiguous_count": (
            _BackupCleanupSummaryFields.model_validate(legacy_cleanup).ambiguous_count if legacy_cleanup else 0
        ),
        "removed_rewrite_count": len(removed_rewrites),
        "removed_rewrites": removed_rewrites[:sample_limit],
        "removed_empty_dir_count": len(removed_empty_dirs),
        "removed_empty_dirs": removed_empty_dirs[:sample_limit],
        "removed_empty_dir_entries": removed_empty_dir_entries[:sample_limit],
        "removed_empty_root_note_count": len(removed_empty_root_notes),
        "removed_empty_root_notes": removed_empty_root_notes[:sample_limit],
        "removed_empty_root_note_entries": removed_empty_root_note_entries[:sample_limit],
        "error_count": len(errors),
        "errors": errors[:sample_limit],
    }


def audit_user_root_hygiene(user_root: Path | None = None, *, sample_limit: int = 50) -> dict[str, Any]:
    """Audit suspicious loose files in a user root without deleting anything."""
    root = (user_root or Path.home()).expanduser()
    candidates: list[dict[str, Any]] = []
    if root.exists() and root.is_dir():
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in ROOT_HYGIENE_SUFFIXES:
                continue
            candidates.append(_root_hygiene_entry(root, path))
    unexpected = [item for item in candidates if item.get("signal") == "agent.unexpected_mutation"]
    return {
        "schema": ROOT_HYGIENE_AUDIT_SCHEMA,
        "phase": "environment/root_hygiene_audit",
        "status": "completed_with_warnings" if candidates else "completed",
        "blocked_reason": "",
        "next_action": (
            "Revisar candidates[] e remover manualmente apenas arquivos confirmados como artefatos soltos."
            if candidates
            else ""
        ),
        "required_inputs": ["user_root"],
        "human_decision_required": False,
        "user_root": str(root),
        "candidate_count": len(candidates),
        "unexpected_mutation_count": len(unexpected),
        "candidates": candidates[:sample_limit],
    }


def _root_hygiene_entry(root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rel = path.relative_to(root).as_posix()
    signal = "agent.unexpected_mutation" if _looks_agent_generated_root_file(path) else "loose_root_file"
    return {
        "path": rel,
        "absolute_path": str(path),
        "size_bytes": stat.st_size,
        "sha256": f"sha256:{digest}",
        "suffix": path.suffix.lower(),
        "signal": signal,
        "action": "manual_review",
    }


def _looks_agent_generated_root_file(path: Path) -> bool:
    name = path.name.casefold()
    if any(marker.casefold() in name for marker in ROOT_HYGIENE_AGENT_MARKERS):
        return True
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:2048].casefold()
    except OSError:
        return False
    return any(marker.casefold() in sample for marker in ROOT_HYGIENE_AGENT_MARKERS)


def _backup_or_temp_files(wiki_dir: Path, *, kind: str) -> list[Path]:
    if not wiki_dir.exists():
        return []
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(wiki_dir):
        dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith(".")]
        root_path = Path(root)
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = root_path / filename
            if kind == "backup" and is_canonical_backup_path(path):
                files.append(path)
            elif kind == "rewrite" and ".rewrite" in filename:
                files.append(path)
    return sorted(files, key=lambda item: item.as_posix())


def _empty_dirs(wiki_dir: Path) -> list[Path]:
    if not wiki_dir.exists():
        return []
    empty: list[Path] = []
    for path in sorted((item for item in wiki_dir.rglob("*") if item.is_dir()), key=lambda item: item.as_posix()):
        rel = path.relative_to(wiki_dir)
        if not rel.parts or any(part.startswith(".") for part in rel.parts):
            continue
        if path.name in IGNORED_EMPTY_DIR_NAMES:
            continue
        try:
            if not any(path.iterdir()):
                empty.append(path)
        except OSError:
            continue
    return empty


def _note_snapshots(wiki_dir: Path) -> list[_HygieneNoteSnapshot]:
    snapshots: list[_HygieneNoteSnapshot] = []
    for path in _note_files(wiki_dir):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        content = raw.decode("utf-8", errors="replace")
        snapshots.append(
            _HygieneNoteSnapshot(
                path=path,
                content=content,
                sha256=hashlib.sha256(raw).hexdigest(),
                is_index_note=_is_index_note_snapshot(path, content),
            )
        )
    return snapshots


def _is_index_note_snapshot(path: Path, content: str) -> bool:
    return is_index_target(path.stem) or _is_index_note(path, content)


def _duplicate_hash_groups(wiki_dir: Path) -> list[dict[str, Any]]:
    by_hash: dict[str, list[str]] = {}
    for path in _note_files(wiki_dir):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        by_hash.setdefault(digest, []).append(path.relative_to(wiki_dir).as_posix())
    return [
        {"sha256": digest, "files": sorted(files), "count": len(files)}
        for digest, files in sorted(by_hash.items())
        if len(files) > 1
    ]


def _duplicate_hash_groups_from_snapshots(
    wiki_dir: Path,
    snapshots: list[_HygieneNoteSnapshot],
) -> list[dict[str, Any]]:
    by_hash: dict[str, list[str]] = {}
    for snapshot in snapshots:
        by_hash.setdefault(snapshot.sha256, []).append(snapshot.path.relative_to(wiki_dir).as_posix())
    return [
        {"sha256": digest, "files": sorted(files), "count": len(files)}
        for digest, files in sorted(by_hash.items())
        if len(files) > 1
    ]


def _duplicate_filename_groups(wiki_dir: Path) -> list[dict[str, Any]]:
    by_name: dict[str, list[str]] = {}
    for path in _note_files(wiki_dir):
        if _is_index_note_file(path):
            continue
        by_name.setdefault(normalize_key(path.stem), []).append(path.relative_to(wiki_dir).as_posix())
    return [
        {"key": key, "files": sorted(files), "count": len(files)}
        for key, files in sorted(by_name.items())
        if len(files) > 1
    ]


def _duplicate_filename_groups_from_snapshots(
    wiki_dir: Path,
    snapshots: list[_HygieneNoteSnapshot],
) -> list[dict[str, Any]]:
    by_name: dict[str, list[str]] = {}
    for snapshot in snapshots:
        if snapshot.is_index_note:
            continue
        by_name.setdefault(normalize_key(snapshot.path.stem), []).append(snapshot.path.relative_to(wiki_dir).as_posix())
    return [
        {"key": key, "files": sorted(files), "count": len(files)}
        for key, files in sorted(by_name.items())
        if len(files) > 1
    ]


def _note_depth_issues(wiki_dir: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in _note_files(wiki_dir):
        rel = path.relative_to(wiki_dir)
        if _is_index_note_file(path):
            continue
        if len(rel.parts) != 4:
            issues.append({"file": rel.as_posix(), "depth": len(rel.parts)})
    return issues


def _note_depth_issues_from_snapshots(
    wiki_dir: Path,
    snapshots: list[_HygieneNoteSnapshot],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for snapshot in snapshots:
        rel = snapshot.path.relative_to(wiki_dir)
        if snapshot.is_index_note:
            continue
        if len(rel.parts) != 4:
            issues.append({"file": rel.as_posix(), "depth": len(rel.parts)})
    return issues


def _loose_root_note_files(wiki_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in _note_files(wiki_dir)
        if len(path.relative_to(wiki_dir).parts) == 1 and not _is_index_note_file(path)
    )


def _loose_root_note_snapshots(
    wiki_dir: Path,
    snapshots: list[_HygieneNoteSnapshot],
) -> list[_HygieneNoteSnapshot]:
    return sorted(
        (
            snapshot
            for snapshot in snapshots
            if len(snapshot.path.relative_to(wiki_dir).parts) == 1 and not snapshot.is_index_note
        ),
        key=lambda snapshot: snapshot.path.relative_to(wiki_dir).as_posix(),
    )


def _empty_root_note_files(wiki_dir: Path) -> list[Path]:
    return [path for path in _loose_root_note_files(wiki_dir) if _is_strictly_empty(path)]


def _is_strictly_empty(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == ""
    except OSError:
        return False


def _note_files(wiki_dir: Path) -> list[Path]:
    return iter_notes(wiki_dir)


def _is_index_note_file(path: Path) -> bool:
    if is_index_target(path.stem):
        return True
    try:
        return _is_index_note(path, path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _is_note_candidate(wiki_dir: Path, path: Path) -> bool:
    return is_note_markdown(wiki_dir, path)


def _archive_file(wiki_dir: Path, path: Path, archive_root: Path) -> dict[str, str]:
    rel = path.relative_to(wiki_dir)
    destination = _unique_destination(archive_root / rel)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))
    return {"source": rel.as_posix(), "destination": str(destination)}


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.name}.{idx}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Too many archived files with same name: {path}")


def _rel_sample(wiki_dir: Path, paths: list[Path], limit: int) -> list[str]:
    return [path.relative_to(wiki_dir).as_posix() for path in paths[:limit]]
