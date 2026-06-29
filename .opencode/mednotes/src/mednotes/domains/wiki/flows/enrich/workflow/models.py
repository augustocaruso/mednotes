"""Shared models and constants for the image enrichment workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mednotes.domains.wiki.capabilities.illustrate.sources import ImageCandidate


class GeminiError(RuntimeError):
    pass


_DEFAULT_GEMINI_TIMEOUT_SECONDS = 120
_EXIT_SOURCE_QUOTA = 9


@dataclass
class CandidateReport:
    candidates: list[ImageCandidate]
    counts_by_source: dict[str, int]
    failed_queries: list[tuple[str, str, str]]
    capped: bool = False


@dataclass
class NoteResult:
    note: Path
    code: int
    status: str
    inserted_count: int = 0
    sources_count: dict[str, int] = field(default_factory=dict)
    message: str = ""
    quality_report: dict[str, object] | None = None
