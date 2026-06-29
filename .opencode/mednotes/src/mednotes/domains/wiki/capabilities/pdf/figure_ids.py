"""Figure/table identifier normalization."""
from __future__ import annotations

import re
import unicodedata

_PREFIX_RE = re.compile(r"\b(fig(?:ure|ura)?|table|tabela|box|plate|algorithm|algoritmo)\.?\s*([0-9]+(?:[.\-][0-9]+)?\s*[- ]?\s*[a-zA-Z]?)", re.IGNORECASE)
_PREFIX_MAP = {
    "fig": "fig",
    "figure": "fig",
    "figura": "fig",
    "table": "table",
    "tabela": "table",
    "box": "box",
    "plate": "plate",
    "algorithm": "algorithm",
    "algoritmo": "algorithm",
}


def normalize(value: str) -> str:
    text = _strip_accents(value).lower()
    match = _PREFIX_RE.search(text)
    if not match:
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    prefix = _PREFIX_MAP.get(match.group(1).lower(), match.group(1).lower())
    ident = match.group(2).replace(" ", "").replace("-a", "a")
    ident = re.sub(r"[^a-z0-9.]+", "-", ident).strip("-")
    ident = ident.replace(".", "-")
    return f"{prefix}-{ident}"


def find_ids(text: str) -> list[tuple[str, str, int, int]]:
    out: list[tuple[str, str, int, int]] = []
    for match in _PREFIX_RE.finditer(_strip_accents(text)):
        raw = match.group(0)
        out.append((raw, normalize(raw), match.start(), match.end()))
    return out


def _strip_accents(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))
