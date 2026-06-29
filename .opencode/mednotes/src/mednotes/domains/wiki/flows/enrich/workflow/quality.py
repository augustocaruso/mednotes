"""Deterministic quality helpers for image candidate selection."""
from __future__ import annotations

from dataclasses import dataclass

from mednotes.domains.wiki.capabilities.illustrate.sources import ImageCandidate

DECORATIVE_MARKERS = ("stock", "icon", "clipart", "vector", "wallpaper", "logo")


@dataclass(frozen=True)
class CandidateQuality:
    candidate: ImageCandidate
    score: float
    flags: tuple[str, ...]


def assess_candidate(visual_type: str, candidate: ImageCandidate) -> CandidateQuality:
    flags: list[str] = []
    width = candidate.width or 0
    height = candidate.height or 0
    trust = candidate.trust_score if candidate.trust_score is not None else 0.50
    resolution_score = min(max(min(width, height) / 1200, 0.0), 1.0)
    text = f"{candidate.title} {candidate.description}".lower()

    if min(width, height) and min(width, height) < 600:
        flags.append("low_resolution")
    if any(marker in text for marker in DECORATIVE_MARKERS):
        flags.append("possibly_decorative")
    if (
        visual_type == "radiology"
        and candidate.source not in {"radiopaedia", "nih_open_i", "web_search"}
    ):
        flags.append("weak_radiology_source")

    penalty = 0.15 * len(flags)
    score = max(0.0, min(1.0, (0.65 * trust) + (0.35 * resolution_score) - penalty))
    return CandidateQuality(candidate=candidate, score=round(score, 4), flags=tuple(flags))


def rank_candidates_for_rerank(
    visual_type: str,
    candidates: list[ImageCandidate],
) -> list[ImageCandidate]:
    assessed = [assess_candidate(visual_type, item) for item in candidates]
    assessed.sort(
        key=lambda item: (
            -item.score,
            item.candidate.source,
            item.candidate.title.lower(),
            item.candidate.image_url,
        )
    )
    return [item.candidate for item in assessed]
