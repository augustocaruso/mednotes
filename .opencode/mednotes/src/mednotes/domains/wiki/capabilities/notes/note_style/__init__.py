"""Deterministic style contract for Wiki_Medicina notes."""
from __future__ import annotations

from importlib import import_module
from typing import Any

from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    canonical_wiki_tags,
    infer_title,
    normalize_wiki_frontmatter,
    parse_frontmatter,
    raw_meta_from_file,
    split_frontmatter,
    wiki_frontmatter_aliases,
)
from mednotes.domains.wiki.capabilities.notes.note_style.models import (
    NOTE_MODEL_CHANGE_RULE,
    NOTE_MODEL_ISSUE_COVERAGE,
    PREFERRED_H2_EMOJIS,
    REQUIRED_SECTION_LINES,
    REWRITE_REQUIRED_CODES,
    STYLE_AUDIT_SCHEMA,
    STYLE_FIX_SCHEMA,
    STYLE_REPORT_SCHEMA,
    WIKI_INDEX_LINK,
    StyleIssue,
)
from mednotes.domains.wiki.capabilities.notes.note_style.prompts import rewrite_prompt
from mednotes.domains.wiki.capabilities.notes.note_style.tables import (
    check_tables,
    escape_wikilink_alias_pipes_in_tables,
    normalize_markdown_tables,
)

_LAZY_EXPORTS = {
    "fix_note_style": ("mednotes.domains.wiki.capabilities.notes.note_style.fixes", "fix_note_style"),
    "index_style_report": ("mednotes.domains.wiki.capabilities.notes.note_style.validate", "index_style_report"),
    "validate_note_style": ("mednotes.domains.wiki.capabilities.notes.note_style.validate", "validate_note_style"),
    "validate_wiki_dir": ("mednotes.domains.wiki.capabilities.notes.note_style.validate", "validate_wiki_dir"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = [
    "NOTE_MODEL_CHANGE_RULE",
    "NOTE_MODEL_ISSUE_COVERAGE",
    "PREFERRED_H2_EMOJIS",
    "REQUIRED_SECTION_LINES",
    "REWRITE_REQUIRED_CODES",
    "STYLE_AUDIT_SCHEMA",
    "STYLE_FIX_SCHEMA",
    "STYLE_REPORT_SCHEMA",
    "StyleIssue",
    "WIKI_INDEX_LINK",
    "check_tables",
    "canonical_wiki_tags",
    "escape_wikilink_alias_pipes_in_tables",
    "fix_note_style",
    "infer_title",
    "index_style_report",
    "normalize_markdown_tables",
    "normalize_wiki_frontmatter",
    "parse_frontmatter",
    "raw_meta_from_file",
    "rewrite_prompt",
    "split_frontmatter",
    "validate_note_style",
    "validate_wiki_dir",
    "wiki_frontmatter_aliases",
]
