"""Figure mention extraction and linking."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mednotes.domains.wiki.capabilities.pdf import figure_ids


@dataclass(frozen=True)
class Mention:
    page_number: int
    figure_id: str
    sentence: str
    paragraph: str = ""
    section_path_guess: list[str] = field(default_factory=list)
    offset_start: int = 0
    offset_end: int = 0


def extract_mentions(text: str, *, page_number: int, section_path_guess: list[str] | None = None) -> list[Mention]:
    mentions: list[Mention] = []
    for _raw, normalized, start, end in figure_ids.find_ids(text):
        sentence = _sentence_around(text, start, end)
        mentions.append(
            Mention(
                page_number=page_number,
                figure_id=normalized,
                sentence=sentence,
                paragraph=sentence,
                section_path_guess=list(section_path_guess or []),
                offset_start=start,
                offset_end=end,
            )
        )
    return mentions


def link_mentions(caption_page: int, caption_section: list[str], candidates: list[Mention], *, figure_id: str, window_pages: int = 20) -> list[Mention]:
    linked: list[Mention] = []
    for mention in candidates:
        if mention.figure_id != figure_id:
            continue
        same_section = bool(caption_section and mention.section_path_guess and caption_section[:1] == mention.section_path_guess[:1])
        near = abs(mention.page_number - caption_page) <= window_pages
        if same_section or near:
            linked.append(mention)
    return linked


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_dot = text.find(".", end)
    right_newline = text.find("\n", end)
    candidates = [idx for idx in (right_dot, right_newline) if idx != -1]
    right = min(candidates) if candidates else min(len(text), end + 180)
    return re.sub(r"\s+", " ", text[left + 1 : right + 1]).strip()
