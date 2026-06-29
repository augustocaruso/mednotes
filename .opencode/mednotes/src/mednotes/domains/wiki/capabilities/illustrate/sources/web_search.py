"""Adapter de busca web genérica via SerpAPI (engine ``google_images``).

Pra cobrir o que Wikimedia/fontes médicas curadas não têm. Pago — usuário
guarda a chave no keyring do sistema ou usa env como fallback técnico. Sem a
chave, ``search`` devolve ``[]`` silenciosamente. Cota/limite esgotado levanta
``SourceQuotaExceeded`` para o orquestrador parar o lote e avisar o usuário.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.illustrate.sources import ImageCandidate, SourceQuotaExceeded
from mednotes.platform.paths import find_config
from mednotes.platform.secrets import resolve_secret
from mednotes.platform.user_config import SecretConfig, load_user_config

NAME = "web_search"

_ENDPOINT = "https://serpapi.com/search.json"
_QUOTA_STATUS_CODES = {402, 429}
_QUOTA_MARKERS = (
    "quota",
    "exceeded",
    "exhaust",
    "run out",
    "monthly search",
    "searches per month",
    "credits",
    "rate limit",
    "too many requests",
)
_LANGUAGE_TO_GOOGLE_PARAMS = {
    "pt-br": {"hl": "pt-br", "gl": "br"},
    "en": {"hl": "en", "gl": "us"},
}


def _serpapi_secret_config() -> SecretConfig:
    try:
        return load_user_config(find_config(start=Path.cwd())).secrets.serpapi
    except (OSError, tomllib.TOMLDecodeError, PydanticValidationError):
        return SecretConfig()


def _serpapi_key(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    result = resolve_secret("serpapi", _serpapi_secret_config())
    return result.value if result.status == "available" else None


def search(
    query: str,
    visual_type: str,
    *,
    top_k: int = 4,
    client: httpx.Client | None = None,
    api_key: str | None = None,
    language: str | None = None,
    site_filter: str | None = None,
    source_label: str | None = None,
) -> list[ImageCandidate]:
    """Busca imagens via SerpAPI (Google Images).

    Sem chave via keyring/env e sem ``api_key`` explícito, devolve ``[]``.
    ``visual_type`` é aceito por uniformidade com outros adapters mas não
    é mapeado em facets do SerpAPI.

    ``language`` é mapeado para os params ``hl`` (UI language) e ``gl``
    (geolocation) do Google Images. Aceita ``"pt-br"`` e ``"en"``;
    qualquer outro valor (inclusive ``"any"`` e ``None``) → sem param.
    """
    key = _serpapi_key(api_key)
    if not key:
        return []

    search_query = _query_with_site_filter(query, site_filter)
    params: dict[str, str] = {
        "engine": "google_images",
        "q": search_query,
        "api_key": key,
        "num": str(max(top_k * 2, top_k)),
    }
    lang_params = _LANGUAGE_TO_GOOGLE_PARAMS.get((language or "").lower())
    if lang_params:
        params.update(lang_params)

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=15.0)
    try:
        resp = client.get(_ENDPOINT, params=params)
        error_message = _response_error_message(resp)
        if _is_quota_error(resp.status_code, error_message):
            raise SourceQuotaExceeded(
                NAME,
                f"SerpAPI bloqueou a busca por cota/limite: "
                f"{error_message or f'HTTP {resp.status_code}'}",
            )
        resp.raise_for_status()
        data = resp.json()
        api_error = _api_error_message(data)
        if _is_quota_error(resp.status_code, api_error):
            raise SourceQuotaExceeded(
                NAME,
                f"SerpAPI bloqueou a busca por cota/limite: {api_error}",
            )
    finally:
        if owns_client:
            client.close()

    return _parse(data, top_k=top_k, source_label=source_label)


def _query_with_site_filter(query: str, site_filter: str | None) -> str:
    if not site_filter:
        return query
    prefix = f"site:{site_filter}"
    if query.strip().lower().startswith(prefix.lower()):
        return query
    return f"{prefix} {query}"


def _domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _response_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()
    return _api_error_message(data)


def _api_error_message(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("error", "message"):
        value = data.get(key)
        if value:
            return str(value)
    errors = data.get("errors")
    if isinstance(errors, list):
        return "; ".join(str(item) for item in errors if item)
    if errors:
        return str(errors)
    return ""


def _is_quota_error(status_code: int, message: str) -> bool:
    lowered = (message or "").lower()
    if status_code in _QUOTA_STATUS_CODES:
        return True
    return bool(lowered and any(marker in lowered for marker in _QUOTA_MARKERS))


def _parse(
    data: dict[str, Any],
    *,
    top_k: int,
    source_label: str | None = None,
) -> list[ImageCandidate]:
    results = data.get("images_results") or []
    out: list[ImageCandidate] = []
    label = source_label or NAME
    for r in results:
        thumbnail_url = r.get("thumbnail")
        image_url = r.get("original") or thumbnail_url
        if not image_url:
            continue
        # `link` é a página onde a imagem aparece; `source` é o domínio.
        source_url = r.get("link") or image_url
        title = r.get("title", "") or ""
        description = r.get("snippet") or r.get("source") or title
        out.append(
            ImageCandidate(
                source=label,
                source_url=source_url,
                image_url=image_url,
                title=title,
                description=description,
                width=r.get("original_width"),
                height=r.get("original_height"),
                license=None,  # SerpAPI não devolve licença
                score=None,
                thumbnail_url=thumbnail_url,
                source_profile=source_label,
                page_domain=_domain(source_url),
            )
        )
        if len(out) >= top_k:
            break
    return out
