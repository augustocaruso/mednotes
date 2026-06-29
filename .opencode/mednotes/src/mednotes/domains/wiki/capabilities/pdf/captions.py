"""Caption extraction for PDF text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mednotes.domains.wiki.capabilities.pdf import figure_ids


@dataclass(frozen=True)
class Caption:
    page_number: int
    figure_id: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    section_path_guess: list[str] = field(default_factory=list)


_CAPTION_RE = re.compile(
    r"(?P<label>\b(?:fig(?:ure|ura)?|table|tabela|box|plate|algorithm|algoritmo)\.?\s*[0-9]+(?:[.\-][0-9]+)?\s*[- ]?\s*[a-zA-Z]?)\.?\s*(?P<body>[^\n]{0,240})",
    re.IGNORECASE,
)


def extract_captions(text: str, *, page_number: int, section_path_guess: list[str] | None = None) -> list[Caption]:
    captions: list[Caption] = []
    seen: set[tuple[str, str]] = set()
    for match in _CAPTION_RE.finditer(text):
        label = match.group("label")
        body = match.group("body").strip()
        sentence = f"{label}. {body}".strip()
        if len(sentence) < len(label) + 2:
            continue
        figure_id = figure_ids.normalize(label)
        key = (figure_id, sentence)
        if key in seen:
            continue
        seen.add(key)
        captions.append(
            Caption(
                page_number=page_number,
                figure_id=figure_id,
                text=sentence,
                section_path_guess=list(section_path_guess or []),
            )
        )
    return captions
