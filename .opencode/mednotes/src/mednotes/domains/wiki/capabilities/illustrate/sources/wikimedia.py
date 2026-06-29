"""Adapter Wikimedia Commons via action API (``commons.wikimedia.org/w/api.php``).

Usa ``generator=search`` no namespace de arquivos (6) + ``prop=imageinfo``
para colher URL, dimensões, MIME e metadata de licença em uma única chamada.

Ignora resultados sem ``imageinfo`` ou com MIME não-imagem (Commons hospeda
PDFs, audio etc. no mesmo namespace).
"""
from __future__ import annotations

from typing import Any

import httpx

from mednotes.domains.wiki.capabilities.illustrate.sources import ImageCandidate

NAME = "wikimedia"

_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
_USER_AGENT = (
    "medical-notes-workbench/0.1 (personal study; "
    "https://github.com/augustocaruso/medical-notes-workbench)"
)
_PREFERRED_THUMB_WIDTH = 1600


def search(
    query: str,
    visual_type: str,
    *,
    top_k: int = 4,
    client: httpx.Client | None = None,
) -> list[ImageCandidate]:
    """Devolve até ``top_k`` candidatos do Wikimedia Commons.

    ``visual_type`` é aceito por uniformidade com outros adapters mas não
    altera a query — Wikimedia não tem facets nesse eixo.
    """
    params: dict[str, str] = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(max(top_k * 2, top_k)),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": str(_PREFERRED_THUMB_WIDTH),
    }
    headers = {"User-Agent": _USER_AGENT}

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=15.0, headers=headers)
    try:
        resp = client.get(_ENDPOINT, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            client.close()

    return _parse(data, top_k=top_k)


def _parse(data: dict[str, Any], *, top_k: int) -> list[ImageCandidate]:
    pages = (data.get("query") or {}).get("pages") or []
    out: list[ImageCandidate] = []
    for page in pages:
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        mime = info.get("mime", "")
        if not mime.startswith("image/"):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value")
        description = (
            (meta.get("ImageDescription") or {}).get("value")
            or page.get("title", "")
        )
        out.append(
            ImageCandidate(
                source=NAME,
                source_url=info.get("descriptionurl", ""),
                image_url=url,
                title=page.get("title", ""),
                description=description,
                width=info.get("thumbwidth") or info.get("width"),
                height=info.get("thumbheight") or info.get("height"),
                license=license_name,
                score=None,
            )
        )
        if len(out) >= top_k:
            break
    return out
