"""Etapa 4 (numeração nova): fetch + validate + resize + dedupe.

Contrato:
- Recebe URL, ``vault_dir`` e parâmetros de redimensionamento/encoding.
- Baixa via ``httpx``, valida o conteúdo via ``Pillow.Image.open`` (magic
  number — proteção contra Google/Bing servir HTML quando o asset some).
- Redimensiona se ``max(width, height) > max_dim`` (LANCZOS).
- Decide encoding final: tenta WebP; mantém WebP se a economia for ≥
  ``webp_min_savings_pct``%, senão preserva o formato original
  (PNG/JPEG; GIF vira PNG single-frame; SVG não é suportado).
- SHA-256 sobre os **bytes finais** (após resize/recode), pra dedupe correta.
- Idempotência por dois níveis:
  1. ``cache.get_sha_for_url(url)``: se conhecemos a URL, recuperamos o SHA
     sem ir à rede.
  2. ``cache.get_image(sha)`` + arquivo existe: reusa.

Erros:
- :class:`DownloadError` para falhas HTTP, conteúdo não-imagem, formato não
  suportado.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from mednotes.domains.wiki.capabilities.illustrate.core.cache import Cache

__all__ = ["DownloadError", "download"]


class DownloadError(RuntimeError):
    """Falha no download/validação/encoding de uma imagem."""


_SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
_FORMAT_TO_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}

# Wikimedia (e outros) rejeitam UAs genéricos como `python-httpx/X.Y` com 403.
_DEFAULT_USER_AGENT = (
    "medical-notes-workbench/0.1 (personal study; "
    "https://github.com/augustocaruso/medical-notes-workbench)"
)


def download(
    url: str,
    *,
    vault_dir: Path,
    max_dim: int = 1600,
    webp_min_savings_pct: int = 30,
    cache: Cache | None = None,
    client: httpx.Client | None = None,
    source: str = "unknown",
    source_url: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """Baixa, valida, normaliza e indexa uma imagem.

    Devolve ``{sha, filename, path, width, height, bytes, source, source_url, cached}``.
    ``cached=True`` quando a imagem já estava conhecida (por URL ou SHA) e o
    arquivo no vault existe — nesse caso não houve fetch nem reescrita.
    """
    # 1) URL cache hit → evita HTTP inteiramente.
    if cache is not None:
        sha_known = cache.get_sha_for_url(url)
        if sha_known:
            existing = cache.get_image(sha_known)
            if existing:
                path = vault_dir / existing["filename"]
                if path.exists():
                    return _hit_dict(existing, path, source_url=source_url or url)

    # 2) Fetch.
    own = client is None
    request_headers = _browser_like_headers(
        user_agent=user_agent or _DEFAULT_USER_AGENT,
        referer=source_url,
    )
    if own:
        client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
        )
    try:
        try:
            resp = client.get(url, headers=request_headers)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise DownloadError(f"falha HTTP em {url}: {e}") from e
        raw = resp.content
    finally:
        if own:
            client.close()

    # 3) Valida (magic number via Pillow).
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise DownloadError(f"conteúdo de {url} não é imagem válida: {e}") from e

    fmt = (img.format or "").upper()
    if fmt not in _SUPPORTED_FORMATS:
        raise DownloadError(f"formato {fmt!r} não suportado ({url})")

    # 4) Resize.
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

    # 5) Decide encoding final.
    final_bytes, final_fmt = _encode(
        img, original_fmt=fmt, webp_min_savings_pct=webp_min_savings_pct
    )
    sha = hashlib.sha256(final_bytes).hexdigest()
    ext = _FORMAT_TO_EXT[final_fmt]
    filename = f"{sha[:12]}.{ext}"
    out_path = vault_dir / filename

    # 6) SHA cache hit → arquivo já existe (ou existia em outro lugar).
    if cache is not None:
        existing = cache.get_image(sha)
        if existing and (vault_dir / existing["filename"]).exists():
            cache.put_url_index(url, sha)
            return _hit_dict(existing, vault_dir / existing["filename"], source_url=source_url or url)

    # 7) Grava + indexa.
    vault_dir.mkdir(parents=True, exist_ok=True)
    if not out_path.exists():
        out_path.write_bytes(final_bytes)

    width, height = img.size
    size_bytes = len(final_bytes)

    if cache is not None:
        cache.put_image(
            sha,
            filename=filename,
            source=source,
            source_url=source_url or url,
            width=width,
            height=height,
            size_bytes=size_bytes,
        )
        cache.put_url_index(url, sha)

    return {
        "sha": sha,
        "filename": filename,
        "path": str(out_path),
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "source": source,
        "source_url": source_url or url,
        "cached": False,
    }


# --- helpers --------------------------------------------------------


def _hit_dict(existing: dict[str, Any], path: Path, *, source_url: str) -> dict[str, Any]:
    return {
        "sha": existing["sha"],
        "filename": existing["filename"],
        "path": str(path),
        "width": existing.get("width"),
        "height": existing.get("height"),
        "bytes": existing.get("bytes"),
        "source": existing.get("source"),
        "source_url": existing.get("source_url") or source_url,
        "cached": True,
    }


def _browser_like_headers(*, user_agent: str, referer: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _encode(
    img: Image.Image, *, original_fmt: str, webp_min_savings_pct: int
) -> tuple[bytes, str]:
    if original_fmt == "WEBP":
        return _encode_as(img, "WEBP"), "WEBP"

    target_fmt = "PNG" if original_fmt == "GIF" else original_fmt
    orig_bytes = _encode_as(img, target_fmt)
    webp_bytes = _encode_as(img, "WEBP")

    if not orig_bytes:
        return webp_bytes, "WEBP"

    savings_pct = (len(orig_bytes) - len(webp_bytes)) / len(orig_bytes) * 100
    if savings_pct >= webp_min_savings_pct:
        return webp_bytes, "WEBP"
    return orig_bytes, target_fmt


def _encode_as(img: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    save_img = img
    if fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        save_img = img.convert("RGB")
    if fmt == "WEBP":
        save_img.save(out, "WEBP", quality=88, method=6)
    elif fmt == "JPEG":
        save_img.save(out, "JPEG", quality=88, optimize=True)
    elif fmt == "PNG":
        save_img.save(out, "PNG", optimize=True)
    else:  # pragma: no cover
        raise ValueError(f"formato de saída não suportado: {fmt}")
    return out.getvalue()
