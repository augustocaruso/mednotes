"""State container for the Textual PDF library UI."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field

from mednotes.kernel.base import ContractModel


class _PdfLibrarySearchResultPayload(ContractModel):
    """Typed input payload accepted by the PDF library TUI state."""

    figure_uid: str = ""
    score: float = Field(default=0.0, strict=True)
    why: list[str] = Field(default_factory=list)
    evidence_level: str = ""
    is_low_confidence: bool = Field(default=False, strict=True)


@dataclass(frozen=True)
class PdfLibrarySearchResult:
    figure_uid: str
    score: float = 0.0
    why: tuple[str, ...] = ()
    evidence_level: str = ""
    is_low_confidence: bool = False

    @classmethod
    def from_payload(cls, value: PdfLibrarySearchResult | Mapping[str, object]) -> PdfLibrarySearchResult:
        if isinstance(value, PdfLibrarySearchResult):
            return value
        payload = _PdfLibrarySearchResultPayload.model_validate(value)
        return cls(
            figure_uid=payload.figure_uid,
            score=payload.score,
            why=tuple(payload.why),
            evidence_level=payload.evidence_level,
            is_low_confidence=payload.is_low_confidence,
        )


@dataclass
class PdfLibraryState:
    selected_note: Path | None = None
    app_home: Path | None = None
    active_screen: str = "doctor"
    ingest_queue: list[Path] = field(default_factory=list)
    search_results: list[PdfLibrarySearchResult] = field(default_factory=list)
    selected_figure_uid: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def select_note(self, path: Path) -> None:
        self.selected_note = path

    def queue_pdf(self, path: Path) -> None:
        self.ingest_queue.append(path)

    def set_search_results(self, results: list[PdfLibrarySearchResult | Mapping[str, object]]) -> None:
        self.search_results = [PdfLibrarySearchResult.from_payload(result) for result in results]

    def select_figure(self, figure_uid: str) -> None:
        self.selected_figure_uid = figure_uid
