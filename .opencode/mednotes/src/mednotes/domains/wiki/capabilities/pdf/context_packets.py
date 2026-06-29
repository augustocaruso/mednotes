"""Redacted context packets for PDF figures."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from mednotes.domains.wiki.capabilities.pdf.captions import Caption
from mednotes.domains.wiki.capabilities.pdf.mentions import Mention

SCHEMA = "medical-notes-workbench.pdf-figure-context-packet.v1"
EVIDENCE_LEVELS = {"caption_and_mentions", "caption_only", "mentions_only", "page_context_only", "visual_only"}


def build_packet(
    *,
    caption: Caption | None = None,
    mentions: list[Mention] | None = None,
    heading_window: str | None = None,
    figure_uid: str | None = None,
) -> dict[str, Any]:
    mentions = list(mentions or [])
    evidence_level = _evidence_level(caption, mentions)
    if evidence_level not in EVIDENCE_LEVELS:
        return {"status": "blocked", "blocked_reason": "invalid_evidence_level"}
    conflict_reason = _conflict_reason(caption, mentions, heading_window)
    is_low_confidence = bool(conflict_reason or evidence_level in {"page_context_only", "visual_only", "mentions_only"})
    figure_id = caption.figure_id if caption else mentions[0].figure_id if mentions else ""
    return {
        "schema": SCHEMA,
        "figure_uid": figure_uid or _uid(figure_id, caption.text if caption else ""),
        "figure_id": figure_id,
        "caption": {"page_number": caption.page_number, "text": caption.text} if caption else None,
        "mentions": [
            {
                "page_number": mention.page_number,
                "sentence": mention.sentence,
                "section_path_guess": list(mention.section_path_guess),
            }
            for mention in mentions
        ],
        "section_path_guess": list(caption.section_path_guess if caption else mentions[0].section_path_guess if mentions else []),
        "evidence_level": evidence_level,
        "is_low_confidence": is_low_confidence,
        "conflict_reason": conflict_reason,
        "source_coordinates": {
            "caption_page": caption.page_number if caption else None,
            "mention_pages": [mention.page_number for mention in mentions],
        },
    }


def _evidence_level(caption: Caption | None, mentions: list[Mention]) -> str:
    if caption and mentions:
        return "caption_and_mentions"
    if caption:
        return "caption_only"
    if mentions:
        return "mentions_only"
    return "visual_only"


def _conflict_reason(caption: Caption | None, mentions: list[Mention], heading_window: str | None) -> str:
    if not caption or not mentions or heading_window:
        return ""
    for mention in mentions:
        if caption.section_path_guess and mention.section_path_guess and caption.section_path_guess[:1] == mention.section_path_guess[:1]:
            continue
        if abs(mention.page_number - caption.page_number) > 20:
            return "same_id_distant_sections"
    return ""


def _uid(*parts: str) -> str:
    material = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"ctx:{hashlib.sha256(material).hexdigest()[:16]}"
