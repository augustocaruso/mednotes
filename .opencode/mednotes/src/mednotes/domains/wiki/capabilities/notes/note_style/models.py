"""Shared constants and models for the Wiki_Medicina style contract."""
from __future__ import annotations

from dataclasses import dataclass

StyleIssuePayload = dict[str, str | int]

STYLE_REPORT_SCHEMA = "medical-notes-workbench.wiki-note-style-report.v1"
STYLE_AUDIT_SCHEMA = "medical-notes-workbench.wiki-note-style-audit.v1"
STYLE_FIX_SCHEMA = "medical-notes-workbench.wiki-note-style-fix.v1"
WIKI_INDEX_LINK = "[[_Índice_Medicina]]"

NOTE_MODEL_CHANGE_RULE = (
    "Any Wiki_Medicina note model change, including data, metadata, YAML, "
    "formatting, footer, sections, tables, aliases, or provenance, must be "
    "implemented in both /mednotes:process-chats for new notes and "
    "/mednotes:fix-wiki as a retroactive path. If retroactive repair is not "
    "deterministic, fix-wiki must block with a rewrite or human decision instead "
    "of reporting green health."
)

PREFERRED_H2_EMOJIS = {"🎯", "🧠", "🔎", "🩺", "⚖️", "⚠️", "🏁", "🔗", "🧬"}

REQUIRED_SECTION_LINES = (
    "## 🏁 Fechamento",
    "### Resumo",
    "### Key Points",
    "### Frase de Prova",
    "## 🔗 Notas Relacionadas",
)

REWRITE_REQUIRED_CODES = {
    "missing_title_heading",
    "missing_h2_sections",
    "missing_required_section",
}

REWRITE_TRIGGER_WARNING_CODES = {
    "didactic_visual_opportunity",
}

NOTE_MODEL_ISSUE_COVERAGE = {
    "didactic_visual_opportunity": {
        "severity": "warning",
        "note_model_attribute": "embedded_mermaid_or_equation_when_clarifying",
        "process_chats_new_notes": "rewrite_required",
        "fix_wiki_retroactive": "llm_style_rewrite",
    },
    "excessive_callouts": {
        "severity": "warning",
        "note_model_attribute": "callout_density",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "extra_blank_lines_before_related": {
        "severity": "warning",
        "note_model_attribute": "related_notes_spacing",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "frontmatter_not_canonical": {
        "severity": "error",
        "note_model_attribute": "canonical_yaml_frontmatter",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "h2_missing_emoji": {
        "severity": "error",
        "note_model_attribute": "emoji_prefixed_h2_headings",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "chats.missing_unrecoverable": {
        "severity": "warning",
        "note_model_attribute": "chat_provenance_missing",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "chats.shape_invalid": {
        "severity": "error",
        "note_model_attribute": "chat_provenance_yaml",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "manual_or_llm_blocker",
    },
    "frontmatter_yaml_unavailable": {
        "severity": "error",
        "note_model_attribute": "canonical_yaml_frontmatter",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "manual_or_llm_blocker",
    },
    "invalid_footer_link": {
        "severity": "error",
        "note_model_attribute": "canonical_footer",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "provenance.legacy_chat_original_footer": {
        "severity": "error",
        "note_model_attribute": "chat_provenance",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "provenance.sources_section_missing": {
        "severity": "error",
        "note_model_attribute": "chat_provenance",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "legacy_index_footer_link": {
        "severity": "error",
        "note_model_attribute": "canonical_footer",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "long_definition": {
        "severity": "warning",
        "note_model_attribute": "opening_definition",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "long_paragraph": {
        "severity": "warning",
        "note_model_attribute": "paragraph_length",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "malformed_markdown_table": {
        "severity": "error",
        "note_model_attribute": "markdown_tables",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "manual_or_llm_blocker",
    },
    "malformed_wikilink_alias": {
        "severity": "warning",
        "note_model_attribute": "wikilink_alias_syntax",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "missing_blank_line_before_callout": {
        "severity": "warning",
        "note_model_attribute": "callout_spacing",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "deterministic_fix",
    },
    "missing_definition": {
        "severity": "warning",
        "note_model_attribute": "opening_definition",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "missing_h2_sections": {
        "severity": "error",
        "note_model_attribute": "section_structure",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "llm_style_rewrite",
    },
    "missing_required_section": {
        "severity": "error",
        "note_model_attribute": "required_closing_sections",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "llm_style_rewrite",
    },
    "missing_title_heading": {
        "severity": "error",
        "note_model_attribute": "title_heading",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "llm_style_rewrite",
    },
    "non_preferred_h2_emoji": {
        "severity": "warning",
        "note_model_attribute": "preferred_heading_emoji_set",
        "process_chats_new_notes": "allow_warning",
        "fix_wiki_retroactive": "diagnostic_warning",
    },
    "unescaped_wikilink_pipe_in_table": {
        "severity": "error",
        "note_model_attribute": "markdown_tables",
        "process_chats_new_notes": "block_error",
        "fix_wiki_retroactive": "deterministic_fix",
    },
}


@dataclass(frozen=True)
class StyleIssue:
    code: str
    message: str
    severity: str
    line: int | None = None
    section: str | None = None
    suggested_visual: str | None = None
    reason: str | None = None

    def to_json(self) -> StyleIssuePayload:
        data: StyleIssuePayload = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.line is not None:
            data["line"] = self.line
        if self.section is not None:
            data["section"] = self.section
        if self.suggested_visual is not None:
            data["suggested_visual"] = self.suggested_visual
        if self.reason is not None:
            data["reason"] = self.reason
        return data
