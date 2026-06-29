"""Path and filename normalization helpers for Wiki taxonomy."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PureWindowsPath

from mednotes.domains.wiki.common import ValidationError

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNSAFE_TITLE_RE = re.compile(r'[\\/*?:"<>|\x00-\x1f]')
_UNSAFE_TAXONOMY_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def normalize_taxonomy(taxonomy: str) -> tuple[str, ...]:
    taxonomy = taxonomy.strip()
    if not taxonomy:
        raise ValidationError("Taxonomy cannot be empty")
    if _DRIVE_RE.match(taxonomy):
        raise ValidationError(f"Taxonomy must be relative, got drive path: {taxonomy}")
    normalized = taxonomy.replace("\\", "/")
    if normalized.startswith("/") or PureWindowsPath(normalized).is_absolute():
        raise ValidationError(f"Taxonomy must be relative: {taxonomy}")
    parts = tuple(part.strip() for part in normalized.split("/"))
    if any(not part for part in parts):
        raise ValidationError(f"Taxonomy has an empty segment: {taxonomy}")
    if any(part in {".", ".."} for part in parts):
        raise ValidationError(f"Taxonomy cannot contain '.' or '..': {taxonomy}")
    bad = [part for part in parts if _UNSAFE_TAXONOMY_RE.search(part)]
    if bad:
        raise ValidationError(f"Taxonomy has unsafe characters: {bad[0]}")
    folded = [_fold_taxonomy_segment(part) for part in parts]
    empty = [part for part, folded_part in zip(parts, folded, strict=False) if not folded_part]
    if empty:
        raise ValidationError(f"Taxonomy segment must contain letters or numbers: {empty[0]}")
    for idx in range(1, len(folded)):
        if folded[idx] == folded[idx - 1]:
            raise ValidationError(f"Taxonomy has duplicated adjacent segments: {parts[idx - 1]}/{parts[idx]}")
    return parts


def safe_title(title: str) -> str:
    cleaned = _UNSAFE_TITLE_RE.sub("", title).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise ValidationError("Title produced an empty filename")
    return cleaned


def _fold_taxonomy_segment(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", without_accents.casefold())


def _safe_relative_dir(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").strip("/")
    if not normalized:
        raise ValidationError("Relative directory path cannot be empty")
    if _DRIVE_RE.match(value) or Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValidationError(f"Directory path must be relative: {value}")
    parts = tuple(part.strip() for part in normalized.split("/"))
    if any(not part for part in parts):
        raise ValidationError(f"Directory path has an empty segment: {value}")
    if any(part in {".", ".."} for part in parts):
        raise ValidationError(f"Directory path cannot contain '.' or '..': {value}")
    if any(part.startswith(".") for part in parts):
        raise ValidationError(f"Directory path cannot contain hidden segments: {value}")
    bad = [part for part in parts if _UNSAFE_TAXONOMY_RE.search(part)]
    if bad:
        raise ValidationError(f"Directory path has unsafe characters: {bad[0]}")
    return parts
