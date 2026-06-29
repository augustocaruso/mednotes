"""Adapters de fontes de imagem.

Cada adapter expõe:
- ``NAME``: identificador curto (``"wikimedia"``, ``"openstax"``, ...).
- ``search(query, visual_type, *, top_k=4, client=None) -> list[ImageCandidate]``.

Falha comum de um adapter não derruba os outros — a etapa de busca chama todos
e ignora exceções individuais. Exceções fatais, como cota paga esgotada, devem
parar o orquestrador para evitar que o lote continue batendo na API.
"""
from __future__ import annotations

from dataclasses import dataclass


class SourceQuotaExceeded(RuntimeError):
    """Erro fatal quando uma fonte paga bloqueia busca por cota/limite."""

    def __init__(self, source: str, message: str):
        super().__init__(message)
        self.source = source


@dataclass(frozen=True)
class ImageCandidate:
    source: str               # ex: "wikimedia"
    source_url: str           # URL da página descritiva (rastreabilidade)
    image_url: str            # URL para download direto
    title: str
    description: str
    width: int | None
    height: int | None
    license: str | None    # informativo (uso pessoal/fair use)
    score: float | None    # relevância da fonte, opcional
    thumbnail_url: str | None = None  # fallback/proxy quando o original bloqueia
    source_profile: str | None = None
    page_domain: str | None = None
    quality_hints: tuple[str, ...] = ()
    trust_score: float | None = None


__all__ = ["ImageCandidate", "SourceQuotaExceeded"]
