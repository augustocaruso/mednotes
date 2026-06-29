"""Image candidate search and thumbnail preparation."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mednotes.domains.wiki.capabilities.illustrate.core.download import DownloadError
from mednotes.domains.wiki.capabilities.illustrate.core.download import download as download_image
from mednotes.domains.wiki.capabilities.illustrate.sources import (
    ImageCandidate,
    SourceQuotaExceeded,
    web_profiles,
    web_search,
    wikimedia,
)
from mednotes.domains.wiki.flows.enrich.workflow import quality
from mednotes.domains.wiki.flows.enrich.workflow.models import CandidateReport
from mednotes.domains.wiki.flows.enrich.workflow.utils import _log


class _SearchAdapter(Protocol):
    """Uniform image-search adapter used by the enrichment workflow."""

    NAME: str

    def search(
        self,
        query: str,
        visual_type: str,
        *,
        top_k: int = 4,
        language: str | None = None,
    ) -> list[ImageCandidate]: ...


class _WikimediaAdapter:
    NAME = wikimedia.NAME

    def search(
        self,
        query: str,
        visual_type: str,
        *,
        top_k: int = 4,
        language: str | None = None,
    ) -> list[ImageCandidate]:
        return wikimedia.search(query, visual_type, top_k=top_k)


class _WebSearchAdapter:
    NAME = web_search.NAME

    def search(
        self,
        query: str,
        visual_type: str,
        *,
        top_k: int = 4,
        language: str | None = None,
    ) -> list[ImageCandidate]:
        return web_search.search(query, visual_type, top_k=top_k, language=language)


class _ProfileAdapter:
    def __init__(self, name: str):
        self.NAME = name

    def search(
        self,
        query: str,
        visual_type: str,
        *,
        top_k: int = 4,
        language: str | None = None,
    ) -> list[ImageCandidate]:
        return web_profiles.search_profile(
            self.NAME,
            query,
            visual_type,
            top_k=top_k,
            language=language,
        )


_SOURCE_REGISTRY: dict[str, _SearchAdapter] = {
    wikimedia.NAME: _WikimediaAdapter(),
    web_search.NAME: _WebSearchAdapter(),
    **{name: _ProfileAdapter(name) for name in web_profiles.PROFILES},
}


def gather_candidates(
    anchor: dict,
    *,
    sources_enabled: list[str],
    top_k_per_source: int,
    max_total: int,
    preferred_language: str = "any",
) -> list[ImageCandidate]:
    return gather_candidate_report(
        anchor,
        sources_enabled=sources_enabled,
        top_k_per_source=top_k_per_source,
        max_total=max_total,
        preferred_language=preferred_language,
    ).candidates


def gather_candidate_report(
    anchor: dict,
    *,
    sources_enabled: list[str],
    top_k_per_source: int,
    max_total: int,
    preferred_language: str = "any",
) -> CandidateReport:
    seen_urls: set[str] = set()
    out: list[ImageCandidate] = []
    counts_by_source = dict.fromkeys(sources_enabled, 0)
    failed_queries: list[tuple[str, str, str]] = []
    for source_name in sources_enabled:
        adapter = _SOURCE_REGISTRY.get(source_name)
        if adapter is None:
            failed_queries.append((source_name, "(adapter)", "fonte desconhecida"))
            continue
        for query in anchor["search_queries"]:
            try:
                cs = adapter.search(
                    query,
                    anchor["visual_type"],
                    top_k=top_k_per_source,
                    language=preferred_language,
                )
            except SourceQuotaExceeded:
                raise
            except Exception as e:
                failed_queries.append((source_name, query, str(e)))
                continue
            for c in cs:
                if c.image_url in seen_urls:
                    continue
                seen_urls.add(c.image_url)
                out.append(c)
                counts_by_source[source_name] += 1
    ranked = quality.rank_candidates_for_rerank(anchor["visual_type"], out)
    return CandidateReport(
        candidates=ranked[:max_total],
        counts_by_source=counts_by_source,
        failed_queries=failed_queries,
        capped=len(ranked) > max_total,
    )


def _candidate_image_urls(c: ImageCandidate) -> list[str]:
    urls = [c.image_url]
    thumbnail_url = getattr(c, "thumbnail_url", None)
    if thumbnail_url and thumbnail_url not in urls:
        urls.append(thumbnail_url)
    return urls


def fetch_thumbs(
    candidates: list[ImageCandidate],
    *,
    tmp_dir: Path,
    user_agent: str | None = None,
) -> list[Path | None]:
    """Baixa thumbnails (256px) sem usar cache do projeto. Falha por candidata
    é tolerada — devolve None na posição correspondente."""
    out: list[Path | None] = []
    for i, c in enumerate(candidates):
        thumb_path = None
        last_error = None
        for url in _candidate_image_urls(c):
            try:
                res = download_image(
                    url,
                    vault_dir=tmp_dir,
                    max_dim=256,
                    webp_min_savings_pct=0,  # sempre WebP nos thumbs
                    cache=None,
                    source=c.source,
                    source_url=c.source_url,
                    user_agent=user_agent,
                )
                thumb_path = Path(res["path"])
                break
            except DownloadError as e:
                last_error = e
        if thumb_path is None:
            _log(f"    [warn] thumb #{i} falhou: {last_error}", err=True)
        out.append(thumb_path)
    return out
