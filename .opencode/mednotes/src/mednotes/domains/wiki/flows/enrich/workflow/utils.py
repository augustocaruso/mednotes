"""Small logging and formatting helpers."""
from __future__ import annotations

import sys


def _log(message: str = "", *, err: bool = False) -> None:
    print(message, file=sys.stderr if err else sys.stdout, flush=True)


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "(nenhuma)"


def _section_label(section_path: list[str]) -> str:
    return " > ".join(section_path) if section_path else "(raiz)"


def _short(text: str | None, *, limit: int = 96) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _format_bytes(n: int | None) -> str:
    if n is None:
        return "tamanho desconhecido"
    units = ["B", "KB", "MB", "GB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
