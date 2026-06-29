"""Deterministic style fixes for Wiki_Medicina notes."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    FrontmatterYamlUnavailable,
    normalize_wiki_frontmatter,
)
from mednotes.domains.wiki.capabilities.notes.note_style.models import STYLE_REPORT_SCHEMA, WIKI_INDEX_LINK
from mednotes.domains.wiki.capabilities.notes.note_style.tables import (
    escape_wikilink_alias_pipes_in_tables,
    normalize_markdown_tables,
)
from mednotes.domains.wiki.capabilities.notes.note_style.validate import index_style_report, validate_note_style
from mednotes.domains.wiki.capabilities.notes.provenance import (
    ChatProvenance,
    apply_note_provenance,
    classify_note_provenance,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note_content, is_index_target
from mednotes.kernel.base import JsonObject

_HEADING_EMOJI_RE = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_LOCAL_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/Users/|/home/|/var/|/tmp/)")
_MALFORMED_ALIAS_RE = re.compile(r"\[\[([^\]\|]+)\]\]([A-ZÁÉÍÓÚÇ]{2,12})\b")
_CALLOUT_START_RE = re.compile(r"^>\s*\[![A-Za-z]+]")

_HEADING_EMOJI_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"quando\s+(pensar|suspeitar|usar)", re.I), "🎯"),
    (re.compile(r"(ideia\s+central|fisiopatologia|mecanismo|etiologia|anatomia|fisiologia)", re.I), "🧠"),
    (re.compile(r"(diagn[oó]stico|exames?|achados?|avalia[cç][aã]o)", re.I), "🔎"),
    (re.compile(r"(conduta|tratamento|manejo|terap[eê]utica)", re.I), "🩺"),
    (re.compile(r"(estratifica[cç][aã]o|classifica[cç][aã]o|risco|escore|componentes)", re.I), "⚖️"),
    (re.compile(r"(pegadinhas?|armadilhas?|pontos?\s+de\s+prova)", re.I), "⚠️"),
    (re.compile(r"fechamento", re.I), "🏁"),
    (re.compile(r"notas?\s+relacionadas?", re.I), "🔗"),
)


def _optional_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def fix_note_style(
    content: str,
    *,
    title: str,
    raw_meta: dict[str, str] | None = None,
    path: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if _is_operational_index_note(content, title=title, path=path):
        return content, index_style_report(content, title=title, path=path)
    fixed = content.replace("\r\n", "\n").replace("\r", "\n")
    fixes: list[str] = []

    stripped_lines = [line.rstrip() for line in fixed.split("\n")]
    stripped = "\n".join(stripped_lines)
    if stripped != fixed:
        fixed = stripped
        fixes.append("trim_trailing_whitespace")

    try:
        frontmatter_fixed, frontmatter_fixes = normalize_wiki_frontmatter(fixed, title=title, preserve_keys={"chats"})
    except FrontmatterYamlUnavailable as exc:
        return fixed, _blocked_report(title=title, path=path, exc=exc, fixes=fixes)
    if frontmatter_fixed != fixed:
        fixed = frontmatter_fixed
        fixes.extend(frontmatter_fixes or ["normalize_frontmatter"])

    heading_fixed = _fix_heading_emojis(fixed)
    if heading_fixed != fixed:
        fixed = heading_fixed
        fixes.append("add_known_heading_emojis")

    alias_fixed = _fix_malformed_alias_links(fixed)
    if alias_fixed != fixed:
        fixed = alias_fixed
        fixes.append("fix_wikilink_alias_suffixes")

    table_link_fixed = escape_wikilink_alias_pipes_in_tables(fixed)
    if table_link_fixed != fixed:
        fixed = table_link_fixed
        fixes.append("escape_wikilink_pipes_in_tables")

    table_fixed = normalize_markdown_tables(fixed)
    if table_fixed != fixed:
        fixed = table_fixed
        fixes.append("normalize_markdown_tables")

    spacing_fixed = _normalize_blank_lines(fixed)
    if spacing_fixed != fixed:
        fixed = spacing_fixed
        fixes.append("normalize_blank_lines")

    footer_links_fixed = _remove_trailing_invalid_footer_links(fixed)
    if footer_links_fixed != fixed:
        fixed = footer_links_fixed
        fixes.append("remove_invalid_footer_links")

    try:
        provenance_fixed = _fix_provenance(fixed, raw_meta or {}, title=title)
    except FrontmatterYamlUnavailable as exc:
        return fixed, _blocked_report(title=title, path=path, exc=exc, fixes=fixes)
    if provenance_fixed != fixed:
        fixed = provenance_fixed
        fixes.append("normalize_provenance")

    if not fixed.endswith("\n"):
        fixed += "\n"
        fixes.append("ensure_trailing_newline")

    report = validate_note_style(
        fixed,
        title=title,
        raw_meta=raw_meta,
        path=path,
        fixes_applied=fixes,
    )
    return fixed, report


def _is_operational_index_note(content: str, *, title: str, path: str | None = None) -> bool:
    stem = Path(path).stem if path else title
    return is_index_target(stem) or is_index_note_content(content)


def _blocked_report(
    *,
    title: str,
    path: str | None,
    exc: FrontmatterYamlUnavailable,
    fixes: list[str],
) -> JsonObject:
    return {
        "schema": STYLE_REPORT_SCHEMA,
        "path": path,
        "title": title,
        "ok": False,
        "errors": [{"code": exc.blocked_reason, "message": exc.next_action, "severity": "error"}],
        "warnings": [],
        "fixes_applied": fixes,
        "requires_llm_rewrite": False,
        "rewrite_prompt": None,
        "frontmatter_present": False,
        "status": "blocked",
        "blocked_reason": exc.blocked_reason,
        "next_action": exc.next_action,
    }


def _fix_heading_emojis(text: str) -> str:
    fixed_lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(##)\s+(.+?)\s*$", line)
        if not match:
            fixed_lines.append(line)
            continue
        heading = match.group(2).strip()
        if _HEADING_EMOJI_RE.match(heading):
            fixed_lines.append(line)
            continue
        emoji = _emoji_for_heading(heading)
        fixed_lines.append(f"## {emoji} {heading}" if emoji else line)
    return "\n".join(fixed_lines)


def _emoji_for_heading(heading: str) -> str:
    for pattern, emoji in _HEADING_EMOJI_RULES:
        if pattern.search(heading):
            return emoji
    return ""


def _fix_malformed_alias_links(text: str) -> str:
    return _MALFORMED_ALIAS_RE.sub(r"[[\1|\2]]", text)


def _normalize_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n+(## 🔗 Notas Relacionadas)", r"\n\n\1", text)
    return _normalize_callout_spacing(text)


def _normalize_callout_spacing(text: str) -> str:
    normalized: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        is_callout_start = bool(_CALLOUT_START_RE.match(stripped))
        is_quote = stripped.startswith(">")
        previous_is_quote = bool(normalized and normalized[-1].lstrip().startswith(">"))
        if is_callout_start and normalized and normalized[-1].strip():
            normalized.append("")
        elif stripped and not is_quote and previous_is_quote:
            normalized.append("")
        normalized.append(line)
    return "\n".join(normalized)


def _fix_provenance(text: str, raw_meta: dict[str, str], *, title: str) -> str:
    state = classify_note_provenance(text)
    chat = _chat_from_raw_meta(raw_meta)
    if state.status == "already_canonical":
        return text
    if state.status != "migratable" and chat is None:
        return text
    chats = [chat] if chat is not None else []
    result = apply_note_provenance(
        text,
        chats=chats,
        chat_lookup=_RawMetaLookup(raw_meta, fallback_title=title),
    )
    return str(result["text"])


def _remove_trailing_invalid_footer_links(text: str) -> str:
    lines = text.rstrip().splitlines()
    changed = True
    while changed and lines:
        changed = False
        tail_start = max(0, len(lines) - 8)
        for idx in range(len(lines) - 1, tail_start - 1, -1):
            stripped = lines[idx].strip()
            if (
                stripped == WIKI_INDEX_LINK
                or "_Índice_Medicina" in stripped
                or "Indice_Medicina" in stripped
                or stripped.startswith("obsidian://")
                or bool(_LOCAL_PATH_RE.search(stripped))
            ):
                lines.pop(idx)
                changed = True
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).rstrip() + "\n"


def _chat_from_raw_meta(raw_meta: dict[str, str]) -> ChatProvenance | None:
    fonte_id = _optional_text(raw_meta.get("fonte_id")).strip("/")
    if not fonte_id:
        return None
    return ChatProvenance(
        fonte_id,
        date_created=_optional_text(raw_meta.get("date_created")),
        date_exported=_optional_text(raw_meta.get("exported_at")) or _optional_text(raw_meta.get("date_exported")),
    )


class _RawMetaLookup:
    def __init__(self, raw_meta: dict[str, str], *, fallback_title: str) -> None:
        self.raw_meta = raw_meta
        self.fallback_title = fallback_title

    def lookup_chat(self, chat_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=chat_id,
            title=str(self.raw_meta.get("titulo_triagem") or self.fallback_title or f"Chat {chat_id[:8]}"),
            url=f"https://gemini.google.com/app/{chat_id}",
            date_created=_optional_text(self.raw_meta.get("date_created")),
            date_exported=_optional_text(self.raw_meta.get("exported_at")) or _optional_text(self.raw_meta.get("date_exported")),
        )
