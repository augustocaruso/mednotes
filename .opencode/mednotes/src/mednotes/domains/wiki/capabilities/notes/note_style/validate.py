"""Validation entrypoints for the Wiki_Medicina note style contract."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.notes.markdown_zones import blank_protected_markdown, protected_markdown_zones
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.note_policy import is_operational_index_note
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    FrontmatterYamlUnavailable,
    infer_title,
    normalize_wiki_frontmatter,
    split_frontmatter,
)
from mednotes.domains.wiki.capabilities.notes.note_style.models import (
    PREFERRED_H2_EMOJIS,
    REQUIRED_SECTION_LINES,
    REWRITE_REQUIRED_CODES,
    REWRITE_TRIGGER_WARNING_CODES,
    STYLE_AUDIT_SCHEMA,
    STYLE_REPORT_SCHEMA,
    WIKI_INDEX_LINK,
    StyleIssue,
)
from mednotes.domains.wiki.capabilities.notes.note_style.prompts import rewrite_prompt
from mednotes.domains.wiki.capabilities.notes.note_style.tables import check_tables
from mednotes.domains.wiki.capabilities.notes.provenance import audit_note_provenance
from mednotes.domains.wiki.performance import cooperative_cpu_yield

_HEADING_EMOJI_RE = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_LOCAL_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/Users/|/home/|/var/|/tmp/)")
_MALFORMED_ALIAS_RE = re.compile(r"\[\[([^\]\|]+)\]\]([A-ZÁÉÍÓÚÇ]{2,12})\b")
_CALLOUT_START_RE = re.compile(r"^>\s*\[![A-Za-z]+]")
_CLINICAL_VISUAL_H2_EMOJIS = {"🧠", "🔎", "🩺", "⚖️"}
_MERMAID_KEYWORD_RE = re.compile(
    r"\b(?:algoritmo|cadeia\s+causal|classifica(?:cao|ção)|decis(?:ao|ão)|estratifica(?:cao|ção)|"
    r"etapas?|fluxo|sequ(?:e|ê)ncia|via)\b",
    re.IGNORECASE,
)
_CAUSAL_CONNECTOR_RE = re.compile(
    r"\b(?:causa|causam|emboliza|embolizar|evolui|gera|geram|gerar|leva|levam|progride|resulta)\b",
    re.IGNORECASE,
)
_EQUATION_KEYWORD_RE = re.compile(
    r"\b(?:calcular|calculo|cálculo|equacao|equação|formula|fórmula|pontuacao|pontuação|"
    r"razao|razão|relacao\s+entre|relação\s+entre)\b",
    re.IGNORECASE,
)


def validate_note_style(
    content: str,
    *,
    title: str,
    raw_meta: dict[str, str] | None = None,
    path: str | None = None,
    fixes_applied: list[str] | None = None,
) -> dict[str, Any]:
    if _is_operational_index_note(content, title=title, path=path):
        return index_style_report(content, title=title, path=path, fixes_applied=fixes_applied)
    frontmatter, body = split_frontmatter(content)
    errors: list[StyleIssue] = []
    warnings: list[StyleIssue] = []

    _check_frontmatter(content, title, errors)
    _check_title_and_definition(body, title, errors, warnings)
    _check_headings(body, errors, warnings)
    _check_required_sections(body, errors)
    _check_footer(content, body, errors, warnings)
    check_tables(body, errors)
    _check_style_warnings(body, warnings)
    _check_didactic_visual_opportunities(body, warnings)

    error_payload = [issue.to_json() for issue in errors]
    warning_payload = [issue.to_json() for issue in warnings]
    requires_llm_rewrite = any(issue.code in REWRITE_REQUIRED_CODES for issue in errors) or any(
        issue.code in REWRITE_TRIGGER_WARNING_CODES for issue in warnings
    )
    return {
        "schema": STYLE_REPORT_SCHEMA,
        "path": path,
        "title": title,
        "ok": not errors,
        "errors": error_payload,
        "warnings": warning_payload,
        "fixes_applied": fixes_applied or [],
        "requires_llm_rewrite": requires_llm_rewrite,
        "rewrite_prompt": rewrite_prompt(title, error_payload, warning_payload) if requires_llm_rewrite else None,
        "frontmatter_present": frontmatter is not None,
    }


def validate_wiki_dir(wiki_dir: Path) -> dict[str, Any]:
    files = iter_notes(wiki_dir)
    reports = []
    for index, path in enumerate(files, start=1):
        cooperative_cpu_yield(index)
        content = path.read_text(encoding="utf-8")
        title = infer_title(content, path)
        if is_operational_index_note(path, content):
            reports.append(index_style_report(content, title=title, path=str(path)))
            continue
        reports.append(validate_note_style(content, title=title, path=str(path)))
    return {
        "schema": STYLE_AUDIT_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "file_count": len(files),
        "ok_count": sum(1 for item in reports if item["ok"]),
        "error_count": sum(1 for item in reports if item["errors"]),
        "warning_count": sum(1 for item in reports if item["warnings"]),
        "reports": reports,
    }


def _is_operational_index_note(content: str, *, title: str, path: str | None = None) -> bool:
    note_path = Path(path) if path else Path(f"{title}.md")
    return is_operational_index_note(note_path, content)


def index_style_report(
    content: str,
    *,
    title: str,
    path: str | None = None,
    fixes_applied: list[str] | None = None,
) -> dict[str, Any]:
    frontmatter, _body = split_frontmatter(content)
    return {
        "schema": STYLE_REPORT_SCHEMA,
        "path": path,
        "title": title,
        "ok": True,
        "errors": [],
        "warnings": [],
        "fixes_applied": fixes_applied or [],
        "requires_llm_rewrite": False,
        "rewrite_prompt": None,
        "frontmatter_present": frontmatter is not None,
        "skipped": True,
        "skip_reason": "wiki_index",
    }


def _check_title_and_definition(
    body: str,
    title: str,
    errors: list[StyleIssue],
    warnings: list[StyleIssue],
) -> None:
    title_pattern = re.compile(rf"(?m)^#\s+{re.escape(title)}\s*$")
    title_match = title_pattern.search(body)
    if not title_match:
        errors.append(StyleIssue("missing_title_heading", f"use a level-1 heading exactly as '# {title}'", "error"))
        return

    after_title = body[title_match.end() :]
    before_first_h2 = re.split(r"(?m)^##\s+", after_title, maxsplit=1)[0]
    definition_lines = [
        line.strip()
        for line in before_first_h2.splitlines()
        if line.strip() and not line.lstrip().startswith((">", "-", "|"))
    ]
    if not definition_lines:
        warnings.append(StyleIssue("missing_definition", "add a short 2-4 line definition after the title", "warning"))
    elif len(definition_lines) > 4:
        warnings.append(StyleIssue("long_definition", "keep the opening definition to 2-4 lines", "warning"))


def _check_headings(body: str, errors: list[StyleIssue], warnings: list[StyleIssue]) -> None:
    h2_matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body))
    if not h2_matches:
        errors.append(StyleIssue("missing_h2_sections", "include level-2 sections with emoji-prefixed headings", "error"))
        return

    for match in h2_matches:
        heading = match.group(1).strip()
        line = body.count("\n", 0, match.start()) + 1
        if not _HEADING_EMOJI_RE.match(heading):
            errors.append(
                StyleIssue(
                    "h2_missing_emoji",
                    f"prefix this level-2 heading with a semantic emoji: ## {heading}",
                    "error",
                    line=line,
                )
            )
            continue
        emoji = heading.split(maxsplit=1)[0]
        if emoji not in PREFERRED_H2_EMOJIS:
            warnings.append(
                StyleIssue(
                    "non_preferred_h2_emoji",
                    f"prefer the fixed semantic emoji set for this heading: ## {heading}",
                    "warning",
                    line=line,
                )
            )


def _check_required_sections(body: str, errors: list[StyleIssue]) -> None:
    for line in REQUIRED_SECTION_LINES:
        if not re.search(rf"(?m)^{re.escape(line)}\s*$", body):
            errors.append(StyleIssue("missing_required_section", f"include the required section line '{line}'", "error"))


def _check_footer(content: str, body: str, errors: list[StyleIssue], warnings: list[StyleIssue]) -> None:
    nonempty_lines = [line.strip() for line in body.splitlines() if line.strip()]
    tail = nonempty_lines[-6:]
    if any(line.startswith("obsidian://") or _LOCAL_PATH_RE.search(line) for line in tail):
        errors.append(
            StyleIssue(
                "invalid_footer_link",
                "do not use obsidian deeplinks or local absolute paths in the final footer",
                "error",
            )
        )

    if any(line == WIKI_INDEX_LINK or "_Índice_Medicina" in line or "Indice_Medicina" in line for line in tail):
        errors.append(
            StyleIssue(
                "legacy_index_footer_link",
                "do not end notes with an index backlink; only _Índice_Medicina should link to notes",
                "error",
            )
        )

    provenance = audit_note_provenance(content, chat_lookup=None)
    provenance_errors = provenance.get("errors")
    for issue in provenance_errors if isinstance(provenance_errors, list) else []:
        if isinstance(issue, dict):
            _append_provenance_error(str(issue.get("code", "")), str(issue.get("message", "")), errors)
    provenance_warnings = provenance.get("warnings")
    for issue in provenance_warnings if isinstance(provenance_warnings, list) else []:
        if isinstance(issue, dict):
            _append_provenance_warning(str(issue.get("code", "")), str(issue.get("message", "")), warnings)


def _append_provenance_error(code: str, message: str, errors: list[StyleIssue]) -> None:
    if code == "provenance.legacy_chat_original_footer":
        errors.append(StyleIssue("provenance.legacy_chat_original_footer", message, "error"))
    elif code == "chats.shape_invalid":
        errors.append(StyleIssue("chats.shape_invalid", message, "error"))
    elif code == "provenance.sources_section_missing":
        errors.append(StyleIssue("provenance.sources_section_missing", message, "error"))


def _append_provenance_warning(code: str, message: str, warnings: list[StyleIssue]) -> None:
    if code == "chats.missing_unrecoverable":
        warnings.append(StyleIssue("chats.missing_unrecoverable", message, "warning"))


def _check_frontmatter(content: str, title: str, errors: list[StyleIssue]) -> None:
    frontmatter, _body = split_frontmatter(content)
    if frontmatter is None:
        return
    try:
        normalized, _fixes = normalize_wiki_frontmatter(content, title=title, preserve_keys={"chats"})
    except FrontmatterYamlUnavailable as exc:
        errors.append(
            StyleIssue(
                "frontmatter_yaml_unavailable",
                exc.next_action,
                "error",
            )
        )
        return
    if normalized != content:
        errors.append(
            StyleIssue(
                "frontmatter_not_canonical",
                "use canonical Wiki YAML: omit it when empty; otherwise keep only multiline aliases, tags, and workflow metadata",
                "error",
            )
        )


def _check_style_warnings(body: str, warnings: list[StyleIssue]) -> None:
    if re.search(r"\n{3,}## 🔗 Notas Relacionadas", body):
        warnings.append(
            StyleIssue(
                "extra_blank_lines_before_related",
                "use a single blank line before '## 🔗 Notas Relacionadas'",
                "warning",
            )
        )

    callout_count = len(re.findall(r"(?m)^>\s*\[!", body))
    if callout_count > 2:
        warnings.append(
            StyleIssue("excessive_callouts", "use callouts rarely; keep only the strongest 1-2 per note", "warning")
        )

    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if not _CALLOUT_START_RE.match(line.strip()):
            continue
        previous = lines[idx - 1].strip() if idx > 0 else ""
        if previous:
            warnings.append(
                StyleIssue(
                    "missing_blank_line_before_callout",
                    "add one blank line before standalone callouts",
                    "warning",
                    line=idx + 1,
                )
            )

    for match in _MALFORMED_ALIAS_RE.finditer(body):
        line = body.count("\n", 0, match.start()) + 1
        warnings.append(
            StyleIssue(
                "malformed_wikilink_alias",
                f"use '[[{match.group(1)}|{match.group(2)}]]' instead of '[[{match.group(1)}]]{match.group(2)}'",
                "warning",
                line=line,
            )
        )

    for paragraph, start_line in _paragraphs(body):
        if len(paragraph) > 650:
            warnings.append(
                StyleIssue(
                    "long_paragraph",
                    "split long paragraphs into shorter 2-4 line review blocks",
                    "warning",
                    line=start_line,
                )
            )


def _check_didactic_visual_opportunities(body: str, warnings: list[StyleIssue]) -> None:
    if any(zone.kind in {"fenced_mermaid", "display_math"} for zone in protected_markdown_zones(body)):
        return
    h2_matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body))
    for idx, match in enumerate(h2_matches):
        heading_text = match.group(1).strip()
        emoji = heading_text.split(maxsplit=1)[0] if heading_text else ""
        if emoji not in _CLINICAL_VISUAL_H2_EMOJIS:
            continue

        section_end = h2_matches[idx + 1].start() if idx + 1 < len(h2_matches) else len(body)
        section_text = body[match.start() : section_end]
        if any(zone.kind in {"fenced_mermaid", "display_math"} for zone in protected_markdown_zones(section_text)):
            continue

        searchable = blank_protected_markdown(section_text)
        suggested_visual, reason = _visual_opportunity(searchable)
        if suggested_visual is None:
            continue

        line = body.count("\n", 0, match.start()) + 1
        warnings.append(
            StyleIssue(
                "didactic_visual_opportunity",
                "add an embedded Mermaid diagram or display equation when it clarifies this clinical section",
                "warning",
                line=line,
                section=f"## {heading_text}",
                suggested_visual=suggested_visual,
                reason=reason,
            )
        )


def _visual_opportunity(section_text: str) -> tuple[str | None, str | None]:
    if _EQUATION_KEYWORD_RE.search(section_text):
        return "equation", "secao descreve calculo, formula ou relacao quantitativa que ficaria mais clara como equacao"

    if _MERMAID_KEYWORD_RE.search(section_text):
        return "mermaid", "secao descreve fluxo, classificacao, algoritmo ou cadeia causal visualizavel"

    if len(_CAUSAL_CONNECTOR_RE.findall(section_text)) >= 2:
        return "mermaid", "secao encadeia relacoes causais que ficariam mais claras como Mermaid"

    return None, None


def _paragraphs(body: str) -> list[tuple[str, int]]:
    paragraphs: list[tuple[str, int]] = []
    current: list[str] = []
    start_line = 1
    in_code = False
    for idx, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
        skip = (
            in_code
            or not stripped
            or stripped.startswith(("#", "-", ">", "|", "1.", "2.", "3.", "4.", "5.", "---"))
        )
        if skip:
            if current:
                paragraphs.append((" ".join(current), start_line))
                current = []
            continue
        if not current:
            start_line = idx
        current.append(stripped)
    if current:
        paragraphs.append((" ".join(current), start_line))
    return paragraphs
