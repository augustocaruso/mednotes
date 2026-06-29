"""Canonical chat provenance helpers for Wiki notes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    SOURCE_METADATA_KEYS,
    dump_frontmatter_yaml,
    load_frontmatter_yaml,
    normalize_wiki_frontmatter,
    split_frontmatter,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import read_note_meta
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key

CONSOLIDATED_SOURCES_HEADING = "## 🧬 Fontes Consolidadas"
CHAT_ORIGINAL_LABEL = "Chat Original"

_CHAT_ORIGINAL_RE = re.compile(r"\[Chat Original\]\((https://gemini\.google\.com/app/([^)\s]+))\)")
_GEMINI_URL_RE = re.compile(r"https://gemini\.google\.com/app/([^)\s]+)")
_H2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_LEGACY_CHAT_FOOTER_RE = re.compile(
    r"(?ms)\n*\n---\s*\n\[Chat Original\]\(https://gemini\.google\.com/app/[^)\s]+\)\s*"
)
_CONSOLIDATED_SOURCES_SECTION_RE = re.compile(
    r"(?ms)^##\s+(?:\S+\s+)?Fontes Consolidadas\s*$.*?(?=^##\s+|\Z)"
)
_LEGACY_SOURCE_DELTA_RE = re.compile(
    r"-\s*(?P<delta>.+?)\s+adicionad[ao]s?\s+a\s+partir\s+de:\s*"
    r"\[Chat Original\]\(https://gemini\.google\.com/app/(?P<chat_id>[^)\s]+)\)",
    re.IGNORECASE,
)
_SOURCE_LINK_RE = re.compile(r"^-\s+\[[^\]]+\]\(https://gemini\.google\.com/app/(?P<chat_id>[^)\s]+)\)\s*$")
_MARKDOWN_SOURCE_URL_RE = re.compile(r"\[[^\]]+\]\((?P<url>https?://[^)\s]+)\)")
_SOURCE_DELTA_RE = re.compile(r"^\s+-\s+Delta:\s*(?P<delta>.+?)\s*$")


def _optional_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class ChatProvenance:
    id: str
    date_created: str = ""
    date_exported: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalize_chat_id(self.id))
        object.__setattr__(self, "date_created", _optional_text(self.date_created))
        object.__setattr__(self, "date_exported", _optional_text(self.date_exported))


@dataclass(frozen=True)
class ProvenanceState:
    status: str
    chat_ids: tuple[str, ...]
    legacy_urls: tuple[str, ...]


def classify_note_provenance(text: str) -> ProvenanceState:
    legacy = _legacy_urls(text)
    try:
        chats = _frontmatter_chats(text)
    except ValueError:
        return ProvenanceState("invalid", (), tuple(legacy))
    chat_ids = tuple(chat.id for chat in chats)
    if legacy:
        return ProvenanceState("migratable", tuple(_chat_id_from_url(url) for url in legacy), tuple(legacy))
    if chat_ids and _has_final_consolidated_sources(text):
        return ProvenanceState("already_canonical", chat_ids, tuple(legacy))
    return ProvenanceState("unrecoverable", chat_ids, ())


def audit_note_provenance(text: str, *, chat_lookup: Any) -> dict[str, object]:
    try:
        chats = _frontmatter_chats(text)
    except ValueError as exc:
        return {
            "status": "blocked",
            "blocked_reason": "chats.shape_invalid",
            "errors": [{"code": "chats.shape_invalid", "message": str(exc), "severity": "error"}],
            "warnings": [],
        }
    state = classify_note_provenance(text)
    if state.status == "already_canonical":
        return {
            "status": "ok",
            "blocked_reason": "",
            "chat_ids": list(state.chat_ids),
            "errors": [],
            "warnings": [],
        }
    if state.status == "migratable":
        return {
            "status": "fixable",
            "blocked_reason": "provenance.legacy_chat_original_footer",
            "chat_ids": list(state.chat_ids),
            "legacy_urls": list(state.legacy_urls),
            "errors": [
                {
                    "code": "provenance.legacy_chat_original_footer",
                    "message": "migrate legacy Chat Original footer to YAML chats[] and Fontes Consolidadas",
                    "severity": "error",
                }
            ],
            "warnings": [],
        }
    if chats and not _has_final_consolidated_sources(text):
        return {
            "status": "blocked",
            "blocked_reason": "provenance.sources_section_missing",
            "chat_ids": [chat.id for chat in chats],
            "errors": [
                {
                    "code": "provenance.sources_section_missing",
                    "message": "canonical chat provenance requires final Fontes Consolidadas section",
                    "severity": "error",
                }
            ],
            "warnings": [],
        }
    if _has_non_chat_source_provenance(text):
        return {
            "status": "ok",
            "blocked_reason": "",
            "chat_ids": [],
            "errors": [],
            "warnings": [],
            "source_kind": "non_chat",
        }
    return {
        "status": "warning",
        "blocked_reason": "",
        "chat_ids": [],
        "errors": [],
        "warnings": [
            {
                "code": "chats.missing_unrecoverable",
                "message": "no recoverable Gemini chat provenance found",
                "severity": "warning",
            }
        ],
    }


def apply_note_provenance(
    text: str,
    *,
    chats: list[ChatProvenance],
    chat_lookup: Any,
    deltas: dict[str, str] | None = None,
) -> dict[str, object]:
    existing = _frontmatter_chats(text)
    legacy = [ChatProvenance(_chat_id_from_url(url)) for url in _legacy_urls(text)]
    merged = _enrich_chats(merge_chat_provenance(existing, legacy, chats), chat_lookup)
    if not merged:
        return {"status": "unchanged", "text": text, "chat_ids": []}

    frontmatter, body = split_frontmatter(text)
    data: dict[str, object] = load_frontmatter_yaml(text) if frontmatter is not None else {}
    data["chats"] = [_chat_to_yaml(chat) for chat in merged]
    legacy_deltas = _legacy_consolidated_source_deltas(body)
    rendered_deltas = {**legacy_deltas, **(deltas or {})}
    body = _remove_legacy_footer(body)
    body = _replace_consolidated_sources(body, _render_sources_section(merged, chat_lookup, rendered_deltas))
    with_frontmatter = f"---\n{dump_frontmatter_yaml(data)}---\n{body.lstrip(chr(10))}"
    normalized, fixes = normalize_wiki_frontmatter(with_frontmatter, preserve_keys={"chats"})
    status = "already_canonical" if normalized == text else "updated"
    return {
        "status": status,
        "text": normalized,
        "chat_ids": [chat.id for chat in merged],
        "fixes_applied": fixes,
    }


def merge_chat_provenance(*groups: list[ChatProvenance]) -> list[ChatProvenance]:
    by_id: dict[str, ChatProvenance] = {}
    for group in groups:
        for chat in group:
            chat_id = chat.id.strip()
            if not chat_id:
                continue
            existing = by_id.get(chat_id)
            if existing is None:
                by_id[chat_id] = ChatProvenance(chat_id, chat.date_created, chat.date_exported)
                continue
            by_id[chat_id] = ChatProvenance(
                chat_id,
                existing.date_created or chat.date_created,
                existing.date_exported or chat.date_exported,
            )
    return sorted(by_id.values(), key=lambda item: (not item.date_created, item.date_created, item.id))


def _apply_note_provenance_from_raw_files(
    text: str,
    *,
    raw_files: list[Path],
    title: str,
    coverage_summary: dict[str, Any] | None = None,
) -> dict[str, object]:
    unique_raw_files = _unique_paths(raw_files)
    chats = [
        chat
        for raw_file in unique_raw_files
        for chat in [_chat_from_raw_file(raw_file)]
        if chat is not None
    ]
    return apply_note_provenance(
        text,
        chats=chats,
        chat_lookup=_RawFilesChatLookup(unique_raw_files, fallback_title=title),
        deltas=_coverage_deltas(coverage_summary, title=title),
    )


class _RawFilesChatLookup:
    def __init__(self, raw_files: list[Path], *, fallback_title: str) -> None:
        self.fallback_title = fallback_title
        self.by_id: dict[str, SimpleNamespace] = {}
        for raw_file in raw_files:
            meta = read_note_meta(raw_file)
            chat = _chat_from_raw_meta(meta)
            if chat is None:
                continue
            title = str(meta.get("titulo_triagem") or fallback_title or f"Chat {chat.id[:8]}").strip()
            source = _optional_text(meta.get("fonte_id"))
            self.by_id[chat.id] = SimpleNamespace(
                id=chat.id,
                title=title,
                url=source if source.startswith(("http://", "https://")) else _chat_url(chat.id),
                date_created=chat.date_created,
                date_exported=chat.date_exported,
            )

    def lookup_chat(self, chat_id: str) -> SimpleNamespace:
        normalized_id = _normalize_chat_id(chat_id)
        return self.by_id.get(
            normalized_id,
            SimpleNamespace(
                id=normalized_id,
                title=f"Chat {normalized_id[:8]}",
                url=_chat_url(normalized_id),
                date_created="",
                date_exported="",
            ),
        )


def _chat_from_raw_file(raw_file: Path) -> ChatProvenance | None:
    return _chat_from_raw_meta(read_note_meta(raw_file))


def _chat_from_raw_meta(meta: dict[str, str]) -> ChatProvenance | None:
    source = _optional_text(meta.get("fonte_id"))
    if not source:
        return None
    return ChatProvenance(
        id=source,
        date_created=_optional_text(meta.get("date_created")),
        date_exported=_optional_text(meta.get("exported_at")) or _optional_text(meta.get("date_exported")),
    )


def _coverage_deltas(coverage_summary: dict[str, Any] | None, *, title: str) -> dict[str, str]:
    if not coverage_summary:
        return {}
    sources = coverage_summary.get("sources")
    if not isinstance(sources, list):
        return {}
    title_key = normalize_key(title)
    deltas: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        if str(source.get("status") or "") != "covered":
            continue
        source_title = str(source.get("target_title") or "").strip()
        if source_title and normalize_key(source_title) != title_key:
            continue
        raw_file_value = str(source.get("raw_file") or "").strip()
        if not raw_file_value:
            continue
        chat = _chat_from_raw_file(Path(raw_file_value))
        if chat is None:
            continue
        delta = str(source.get("new_information_summary") or "").strip()
        if delta:
            deltas[chat.id] = delta
    return deltas


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _legacy_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in _CHAT_ORIGINAL_RE.finditer(text):
        url = match.group(1)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _chat_id_from_url(url: str) -> str:
    match = _GEMINI_URL_RE.search(url)
    return match.group(1).strip().strip("/") if match else ""


def _normalize_chat_id(value: str) -> str:
    value = str(value or "").strip().strip("/")
    match = _GEMINI_URL_RE.search(value)
    return match.group(1).strip().strip("/") if match else value


def _chat_url(chat_id: str) -> str:
    return f"https://gemini.google.com/app/{_normalize_chat_id(chat_id)}"


def _frontmatter_chats(text: str) -> list[ChatProvenance]:
    data = load_frontmatter_yaml(text)
    value = data.get("chats")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("chats must be a list of objects")
    chats: list[ChatProvenance] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"chats item #{index} must be an object")
        chat_id = str(item.get("id") or "").strip()
        if not chat_id:
            raise ValueError(f"chats item #{index} missing id")
        chats.append(
            ChatProvenance(
                id=chat_id,
                date_created=str(item.get("date_created") or "").strip(),
                date_exported=str(item.get("date_exported") or "").strip(),
            )
        )
    return chats


def _has_final_consolidated_sources(text: str) -> bool:
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return False
    return matches[-1].group(0).strip() == CONSOLIDATED_SOURCES_HEADING


def _has_non_chat_source_provenance(text: str) -> bool:
    data = load_frontmatter_yaml(text)
    if any(_has_value(value) for key, value in data.items() if str(key).strip().lower() in SOURCE_METADATA_KEYS):
        return True
    source_section = _final_consolidated_sources_section(text)
    if not source_section:
        return False
    for match in _MARKDOWN_SOURCE_URL_RE.finditer(source_section):
        if "://gemini.google.com/app/" not in match.group("url"):
            return True
    return False


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    return True


def _final_consolidated_sources_section(text: str) -> str:
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return ""
    last = matches[-1]
    if last.group(0).strip() != CONSOLIDATED_SOURCES_HEADING:
        return ""
    return text[last.end() :]


def _lookup_chat(chat_lookup: Any, chat_id: str) -> Any:
    if hasattr(chat_lookup, "lookup_chat"):
        return chat_lookup.lookup_chat(chat_id)
    if callable(chat_lookup):
        return chat_lookup(chat_id)
    return None


def _enrich_chats(chats: list[ChatProvenance], chat_lookup: Any) -> list[ChatProvenance]:
    enriched: list[ChatProvenance] = []
    for chat in chats:
        meta = _lookup_chat(chat_lookup, chat.id)
        enriched.append(
            ChatProvenance(
                id=chat.id,
                date_created=chat.date_created or str(getattr(meta, "date_created", "") or ""),
                date_exported=chat.date_exported or str(getattr(meta, "date_exported", "") or ""),
            )
        )
    return merge_chat_provenance(enriched)


def _chat_to_yaml(chat: ChatProvenance) -> dict[str, str]:
    item = {"id": chat.id}
    if chat.date_created:
        item["date_created"] = chat.date_created
    if chat.date_exported:
        item["date_exported"] = chat.date_exported
    return item


def _remove_legacy_footer(body: str) -> str:
    match = _LEGACY_CHAT_FOOTER_RE.search(body)
    if match:
        body = body[: match.start()]
    return body.rstrip() + "\n"


def _replace_consolidated_sources(body: str, section: str) -> str:
    without_existing = _CONSOLIDATED_SOURCES_SECTION_RE.sub("", body.rstrip()).rstrip()
    return without_existing + "\n\n" + section


def _legacy_consolidated_source_deltas(body: str) -> dict[str, str]:
    deltas: dict[str, str] = {}
    for section in _CONSOLIDATED_SOURCES_SECTION_RE.findall(body):
        for match in _LEGACY_SOURCE_DELTA_RE.finditer(section):
            chat_id = _normalize_chat_id(match.group("chat_id"))
            delta = re.sub(r"\s+", " ", match.group("delta")).strip()
            if chat_id and delta:
                deltas.setdefault(chat_id, delta)
        current_chat_id = ""
        for line in section.splitlines():
            source_match = _SOURCE_LINK_RE.match(line.strip())
            if source_match:
                current_chat_id = _normalize_chat_id(source_match.group("chat_id"))
                continue
            delta_match = _SOURCE_DELTA_RE.match(line)
            if delta_match and current_chat_id:
                delta = re.sub(r"\s+", " ", delta_match.group("delta")).strip()
                if delta:
                    deltas.setdefault(current_chat_id, delta)
    return deltas


def _render_sources_section(chats: list[ChatProvenance], chat_lookup: Any, deltas: dict[str, str]) -> str:
    lines = [CONSOLIDATED_SOURCES_HEADING]
    for chat in chats:
        meta = _lookup_chat(chat_lookup, chat.id)
        title = str(getattr(meta, "title", "") or f"Chat {chat.id[:8]}")
        url = str(getattr(meta, "url", "") or f"https://gemini.google.com/app/{chat.id}")
        lines.append(f"- [{title}]({url})")
        delta = str(deltas.get(chat.id) or "").strip()
        if delta:
            lines.append(f"  - Delta: {delta}")
    return "\n".join(lines).rstrip() + "\n"
