"""Trusted web-profile image sources built on top of web_search."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from mednotes.domains.wiki.capabilities.illustrate.sources import ImageCandidate, web_search


@dataclass(frozen=True)
class WebProfile:
    name: str
    domains: tuple[str, ...]
    visual_types: tuple[str, ...]
    trust_score: float


PROFILES: dict[str, WebProfile] = {
    "radiopaedia": WebProfile(
        name="radiopaedia",
        domains=("radiopaedia.org",),
        visual_types=("radiology",),
        trust_score=0.95,
    ),
    "nih_open_i": WebProfile(
        name="nih_open_i",
        domains=("nih.gov", "ncbi.nlm.nih.gov"),
        visual_types=("diagram", "histology", "radiology", "photo", "chart"),
        trust_score=0.90,
    ),
    "openstax": WebProfile(
        name="openstax",
        domains=("openstax.org",),
        visual_types=("anatomy", "diagram", "chart"),
        trust_score=0.85,
    ),
    "dermnet": WebProfile(
        name="dermnet",
        domains=("dermnetnz.org",),
        visual_types=("photo",),
        trust_score=0.88,
    ),
    "teachmeanatomy": WebProfile(
        name="teachmeanatomy",
        domains=("teachmeanatomy.info",),
        visual_types=("anatomy",),
        trust_score=0.82,
    ),
}


def search_profile(
    profile_name: str,
    query: str,
    visual_type: str,
    *,
    top_k: int = 4,
    client: httpx.Client | None = None,
    language: str | None = None,
) -> list[ImageCandidate]:
    profile = PROFILES[profile_name]
    if visual_type not in profile.visual_types:
        return []
    out: list[ImageCandidate] = []
    per_domain = max(1, top_k // len(profile.domains))
    for domain in profile.domains:
        out.extend(
            web_search.search(
                query,
                visual_type,
                top_k=per_domain,
                client=client,
                language=language,
                site_filter=domain,
                source_label=profile.name,
            )
        )
    return [_with_profile_hints(item, profile) for item in out[:top_k]]


def _with_profile_hints(candidate: ImageCandidate, profile: WebProfile) -> ImageCandidate:
    hints = tuple(dict.fromkeys([*candidate.quality_hints, "trusted_web_profile"]))
    return ImageCandidate(
        source=candidate.source,
        source_url=candidate.source_url,
        image_url=candidate.image_url,
        title=candidate.title,
        description=candidate.description,
        width=candidate.width,
        height=candidate.height,
        license=candidate.license,
        score=candidate.score,
        thumbnail_url=candidate.thumbnail_url,
        source_profile=profile.name,
        page_domain=candidate.page_domain,
        quality_hints=hints,
        trust_score=profile.trust_score,
    )
