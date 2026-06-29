"""Legacy cleanup policy for retired local Markdown backups.

Adjacent ``.bak`` files were replaced by vault version control as the rollback
mechanism for Markdown mutations. The cleanup helpers remain so workflows can
identify, prune, and archive old backup files left by earlier versions.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import Field

from mednotes.kernel.base import ContractModel, JsonObject

BACKUP_CLEANUP_SCHEMA = "medical-notes-workbench.backup-cleanup.v1"
LEGACY_BACKUP_CLEANUP_SCHEMA = "medical-notes-workbench.legacy-backup-cleanup.v1"


@dataclass(frozen=True)
class BackupPolicy:
    """Retention policy for adjacent Markdown backups."""

    max_per_file: int = 5
    retention_days: int = 30

    def validate(self) -> None:
        if self.max_per_file < 0:
            raise ValueError("max_per_file must be >= 0")

    def to_json(self) -> dict[str, int]:
        return asdict(self)


DEFAULT_BACKUP_POLICY = BackupPolicy()

_NOTE_BACKUP_RE = re.compile(r"^(?P<original>.+\.md)\.bak(?:\.\d+)?$")
_LEGACY_BEFORE_MD_RE = re.compile(r"^(?P<stem>.+)\.(?P<suffix>bak|backup|old|orig|original|tmp|temp)\.md$", re.IGNORECASE)
_LEGACY_AFTER_MD_RE = re.compile(
    r"^(?P<original>.+\.md)[._~-](?P<suffix>bak|backup|old|orig|original|tmp|temp)(?:[._~-]?\d{4,14})?$",
    re.IGNORECASE,
)
_LEGACY_TILDE_RE = re.compile(r"^(?P<original>.+\.md)~$", re.IGNORECASE)
_COPY_RE = re.compile(r"^(?P<stem>.+?)(?: copy| - copy| copia| - copia| cópia| - cópia| \([Cc]opy\))\.md$", re.IGNORECASE)
_BACKUP_DIR_NAMES = {"backup", "backups", "_backup", "_backups", "old", "_old", "archive", "_archive"}


class RetiredBackupCandidate(ContractModel):
    """Typed candidate for hygiene-only archival of old adjacent backup files."""

    path: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    confidence: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    original_path: str = ""
    original_exists: bool = False


def policy_from_values(
    *,
    max_per_file: int | None = None,
    retention_days: int | None = None,
    policy: BackupPolicy = DEFAULT_BACKUP_POLICY,
) -> BackupPolicy:
    """Return ``policy`` with optional field overrides."""

    resolved = BackupPolicy(
        max_per_file=policy.max_per_file if max_per_file is None else max_per_file,
        retention_days=policy.retention_days if retention_days is None else retention_days,
    )
    resolved.validate()
    return resolved


def next_backup_path(path: Path) -> Path:
    """Return the next available adjacent ``.bak`` path for ``path``."""

    base = path.with_name(path.name + ".bak")
    if not base.exists():
        return base
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak.{idx}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many backups already exist for {path}")


def is_canonical_backup_path(path: Path) -> bool:
    """Return true when ``path`` is a managed adjacent Markdown backup."""

    return bool(_NOTE_BACKUP_RE.match(path.name))


def create_backup(path: Path, *, policy: BackupPolicy = DEFAULT_BACKUP_POLICY) -> Path:
    """Reject new adjacent Markdown backups.

    Kept as a compatibility tripwire for old callers. New workflow code should
    rely on vault restoration points and should not call this function.
    """

    policy.validate()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    raise RuntimeError("Markdown .bak backups are retired; use vault version control restore points.")


def cleanup_backups(root: Path, *, policy: BackupPolicy = DEFAULT_BACKUP_POLICY) -> JsonObject:
    """Prune adjacent Markdown backups under ``root`` using ``policy``.

    The cleanup groups files by original Markdown filename. Within each group it
    keeps the newest ``policy.max_per_file`` backups and deletes any backup
    older than ``policy.retention_days``. A negative ``retention_days`` disables
    age-based pruning, which is useful for focused tests.
    """

    policy.validate()
    if not root.exists():
        raise FileNotFoundError(f"Backup cleanup root not found: {root}")

    groups: dict[Path, list[Path]] = {}
    for path in root.rglob("*.bak*"):
        if not path.is_file():
            continue
        match = _NOTE_BACKUP_RE.match(path.name)
        if not match:
            continue
        groups.setdefault(path.with_name(match.group("original")), []).append(path)

    cutoff = time.time() - (policy.retention_days * 86400) if policy.retention_days >= 0 else None
    deleted: list[str] = []
    kept: list[str] = []
    for _original, backups in sorted(groups.items(), key=lambda item: item[0].as_posix()):
        ordered = sorted(backups, key=lambda item: item.stat().st_mtime, reverse=True)
        for idx, backup in enumerate(ordered):
            mtime = backup.stat().st_mtime
            too_many = idx >= policy.max_per_file
            too_old = cutoff is not None and mtime < cutoff
            if too_many or too_old:
                backup.unlink()
                deleted.append(str(backup))
            else:
                kept.append(str(backup))

    return {
        "schema": BACKUP_CLEANUP_SCHEMA,
        "root": str(root),
        "policy": policy.to_json(),
        "max_per_file": policy.max_per_file,
        "retention_days": policy.retention_days,
        "group_count": len(groups),
        "kept_count": len(kept),
        "deleted_count": len(deleted),
        "deleted": deleted,
    }


def collect_legacy_backup_candidates(root: Path, *, sample_limit: int = 20) -> JsonObject:
    """Report backup-looking Markdown files that are not in the canonical format.

    High-confidence candidates are files with a backup suffix and an existing
    sibling original, such as ``Note.bak.md`` or ``Note.md.old``. Ambiguous
    candidates are reported but not auto-archived.
    """

    if not root.exists():
        raise FileNotFoundError(f"Legacy backup scan root not found: {root}")
    candidates = _legacy_backup_candidates(root)
    high_confidence = [item for item in candidates if item.confidence == "high"]
    ambiguous = [item for item in candidates if item.confidence != "high"]
    return {
        "schema": LEGACY_BACKUP_CLEANUP_SCHEMA,
        "root": str(root),
        "candidate_count": len(candidates),
        "high_confidence_count": len(high_confidence),
        "ambiguous_count": len(ambiguous),
        "high_confidence": [item.to_payload() for item in high_confidence[:sample_limit]],
        "ambiguous": [item.to_payload() for item in ambiguous[:sample_limit]],
    }


def archive_legacy_backups(
    root: Path,
    *,
    archive_root: Path,
    apply: bool = False,
    sample_limit: int = 20,
) -> JsonObject:
    """Archive high-confidence legacy backup files and report ambiguous ones."""

    if not root.exists():
        raise FileNotFoundError(f"Legacy backup cleanup root not found: {root}")
    candidates = _legacy_backup_candidates(root)
    high_confidence = [item for item in candidates if item.confidence == "high"]
    ambiguous = [item for item in candidates if item.confidence != "high"]
    archived: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    if apply and high_confidence:
        archive_root.mkdir(parents=True, exist_ok=True)
    if apply:
        for item in high_confidence:
            source = Path(item.path)
            try:
                destination = _unique_destination(archive_root / item.relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                archived.append(
                    {
                        "source": item.relative_path,
                        "destination": str(destination),
                        "kind": item.kind,
                        "original": item.original_path,
                    }
                )
            except OSError as exc:
                errors.append({"path": str(source), "operation": "archive_legacy_backup", "error": str(exc)})

    return {
        "schema": LEGACY_BACKUP_CLEANUP_SCHEMA,
        "root": str(root),
        "archive_root": str(archive_root),
        "dry_run": not apply,
        "candidate_count": len(candidates),
        "high_confidence_count": len(high_confidence),
        "ambiguous_count": len(ambiguous),
        "archived_count": len(archived),
        "archived": archived[:sample_limit],
        "ambiguous": [item.to_payload() for item in ambiguous[:sample_limit]],
        "error_count": len(errors),
        "errors": errors[:sample_limit],
    }


def _legacy_backup_candidates(root: Path) -> list[RetiredBackupCandidate]:
    candidates: list[RetiredBackupCandidate] = []
    root_str = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        dirnames[:] = sorted(dirname for dirname in dirnames if not dirname.startswith("."))
        rel_dir = os.path.relpath(dirpath, root_str)
        rel_parts = () if rel_dir == "." else Path(rel_dir).parts
        in_backup_dir = any(part.lower() in _BACKUP_DIR_NAMES for part in rel_parts)
        for filename in sorted(filenames):
            if filename.startswith(".") or not _looks_like_legacy_backup_candidate(filename, in_backup_dir=in_backup_dir):
                continue
            path = Path(dirpath) / filename
            if not path.is_file():
                continue
            candidate = _legacy_backup_candidate(root, path, relative_parts=(*rel_parts, filename))
            if candidate:
                candidates.append(candidate)
    return candidates


def _looks_like_legacy_backup_candidate(filename: str, *, in_backup_dir: bool) -> bool:
    if is_canonical_backup_path(Path(filename)):
        return False
    if (
        _LEGACY_BEFORE_MD_RE.match(filename)
        or _LEGACY_AFTER_MD_RE.match(filename)
        or _LEGACY_TILDE_RE.match(filename)
        or _COPY_RE.match(filename)
    ):
        return True
    return in_backup_dir and filename.lower().endswith(".md")


def _legacy_backup_candidate(
    root: Path,
    path: Path,
    *,
    relative_parts: tuple[str, ...] | None = None,
) -> RetiredBackupCandidate | None:
    if relative_parts is not None and any(part.startswith(".") for part in relative_parts):
        return None
    if relative_parts is None and any(part.startswith(".") for part in path.relative_to(root).parts):
        return None
    if is_canonical_backup_path(path):
        return None
    name = path.name
    for pattern, kind in (
        (_LEGACY_BEFORE_MD_RE, "suffix_before_md"),
        (_LEGACY_AFTER_MD_RE, "suffix_after_md"),
        (_LEGACY_TILDE_RE, "editor_tilde"),
    ):
        match = pattern.match(name)
        if match:
            original_name = match.groupdict().get("original") or f"{match.group('stem')}.md"
            return _candidate_for_original(root, path, path.with_name(original_name), kind)

    match = _COPY_RE.match(name)
    if match:
        original = path.with_name(f"{match.group('stem')}.md")
        candidate = _candidate(
            root,
            path,
            "copy_name",
            "ambiguous",
            "copy_name_requires_review",
            original,
            relative_path=_relative_path_from_parts(root, path, relative_parts),
        )
        if candidate.original_exists and _same_file_content(path, original):
            candidate.confidence = "high"
            candidate.reason = "copy_name_identical_to_original"
        return candidate

    parts = relative_parts if relative_parts is not None else path.relative_to(root).parts
    if any(part.lower() in _BACKUP_DIR_NAMES for part in parts[:-1]) and path.suffix.lower() == ".md":
        return _candidate(
            root,
            path,
            "backup_directory",
            "ambiguous",
            "backup_directory_requires_review",
            None,
            relative_path=_relative_path_from_parts(root, path, relative_parts),
        )
    return None


def _candidate_for_original(root: Path, path: Path, original: Path, kind: str) -> RetiredBackupCandidate:
    original_exists = original.exists() and original.is_file()
    confidence = "high" if original_exists else "ambiguous"
    reason = "sibling_original_exists" if original_exists else "sibling_original_missing"
    return _candidate(root, path, kind, confidence, reason, original)


def _candidate(
    root: Path,
    path: Path,
    kind: str,
    confidence: str,
    reason: str,
    original: Path | None,
    *,
    relative_path: str | None = None,
) -> RetiredBackupCandidate:
    return RetiredBackupCandidate(
        path=str(path),
        relative_path=relative_path or path.relative_to(root).as_posix(),
        kind=kind,
        confidence=confidence,
        reason=reason,
        original_path=str(original) if original is not None else "",
        original_exists=bool(original is not None and original.exists() and original.is_file()),
    )


def _relative_path_from_parts(root: Path, path: Path, relative_parts: tuple[str, ...] | None) -> str:
    if relative_parts is None:
        return path.relative_to(root).as_posix()
    return "/".join(relative_parts)


def _same_file_content(left: Path, right: Path) -> bool:
    if not right.exists() or not right.is_file():
        return False
    return _sha256(left) == _sha256(right)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = path.with_name(f"{path.name}.{idx}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Too many archived files with same name: {path}")
