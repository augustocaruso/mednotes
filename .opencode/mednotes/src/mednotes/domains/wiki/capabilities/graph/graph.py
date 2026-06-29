#!/usr/bin/env python3
"""Deterministic graph audit for Wiki_Medicina notes.

This module checks objective Obsidian graph health. It does not judge clinical
semantic quality; the managed ``Notas Relacionadas`` section is populated from
the Related Notes plugin export through ``related-notes-sync``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    INDEX_TARGET_KEYS,
    expand_path,
    extract_aliases,
    is_index_target,
    normalize_key,
    obsidian_target_name,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    aliases_from_entry as _entry_aliases,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    catalog_entries as _catalog_entries,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    is_index_note as _is_index_note,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    target_from_entry as _entry_target,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    WorkflowDecision,
    decision_payload_from_decision,
)
from mednotes.domains.wiki.performance import cooperative_cpu_yield
from mednotes.platform.paths import DEFAULT_CATALOG_PATH, resolve_wiki_dir

GRAPH_AUDIT_SCHEMA = "medical-notes-workbench.wiki-graph-audit.v1"
DEFAULT_WIKI_DIR = ""
RELATED_HEADING = "## 🔗 Notas Relacionadas"
NO_STRONG_LINKS_MARKER = "Sem conexões fortes no catálogo atual."
INDEX_TARGETS = INDEX_TARGET_KEYS

GENERIC_ALIASES = {
    "diagnóstico",
    "diagnostico",
    "tratamento",
    "manejo",
    "clínica",
    "clinica",
    "paciente",
    "doença",
    "doenca",
    "síndrome",
    "sindrome",
    "sinais",
    "sintomas",
    "exame",
    "exames",
    "terapia",
    "medicamento",
}

_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_RELATED_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
_NEXT_H2_RE = re.compile(r"(?m)^##\s+")


@dataclass(frozen=True)
class NoteRecord:
    path: Path
    relative_path: str
    stem: str
    aliases: tuple[str, ...]
    is_index_note: bool = False


def _note_files(wiki_dir: Path) -> list[Path]:
    return iter_notes(wiki_dir)


def _load_notes(wiki_dir: Path) -> list[NoteRecord]:
    notes: list[NoteRecord] = []
    for index, path in enumerate(_note_files(wiki_dir), start=1):
        cooperative_cpu_yield(index)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        notes.append(
            NoteRecord(
                path=path,
                relative_path=path.relative_to(wiki_dir).as_posix(),
                stem=path.stem,
                aliases=tuple(extract_aliases(content)),
                is_index_note=_is_index_note(path, content),
            )
        )
    return notes


def _obsidian_target(raw: str) -> str:
    target = raw.split(r"\|", 1)[0].split("|", 1)[0].split("#", 1)[0].strip()
    return obsidian_target_name(target)


def _line_number(text: str, start: int) -> int:
    return text.count("\n", 0, start) + 1


def _related_section_span(text: str) -> tuple[int, int] | None:
    match = _RELATED_RE.search(text)
    if not match:
        return None
    next_match = _NEXT_H2_RE.search(text, match.end())
    return (match.start(), next_match.start() if next_match else len(text))


def _wikilinks(text: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    related_span = _related_section_span(text)
    for match in _WIKILINK_RE.finditer(text):
        raw = match.group(1).strip()
        target = _obsidian_target(raw)
        if not target:
            continue
        links.append(
            {
                "raw": raw,
                "target": target,
                "line": _line_number(text, match.start()),
                "in_related": bool(related_span and related_span[0] <= match.start() < related_span[1]),
            }
        )
    return links


def _issue(code: str, message: str, severity: str, **extra: Any) -> dict[str, Any]:
    data = {"code": code, "message": message, "severity": severity}
    data.update({key: value for key, value in extra.items() if value is not None})
    return data


def _audit_catalog(
    catalog_path: Path | None,
    notes_by_stem: dict[str, list[NoteRecord]],
    *,
    ignored_target_keys: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    issues: list[dict[str, Any]] = []
    stats = {"catalog_entries": 0, "catalog_aliases": 0}
    if not catalog_path:
        return issues, stats
    if not catalog_path.exists():
        issues.append(_issue("catalog_missing", f"catalog not found: {catalog_path}", "warning", catalog_path=str(catalog_path)))
        return issues, stats

    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(_issue("catalog_invalid_json", f"invalid catalog JSON: {exc}", "error", catalog_path=str(catalog_path)))
        return issues, stats

    ignored_target_keys = ignored_target_keys or set()
    alias_targets: dict[str, set[str]] = {}
    for fallback_key, entry in _catalog_entries(data):
        stats["catalog_entries"] += 1
        target = _entry_target(entry, fallback_key)
        if not target:
            issues.append(_issue("catalog_entry_missing_target", "catalog entry has no target", "error"))
            continue
        if normalize_key(target) in ignored_target_keys:
            continue
        matches = notes_by_stem.get(normalize_key(target), [])
        if not matches:
            issues.append(_issue("catalog_target_missing", f"catalog target does not exist: {target}", "error", target=target))
        elif len(matches) > 1:
            issues.append(_issue("catalog_target_ambiguous", f"catalog target is ambiguous: {target}", "error", target=target))
        for alias in _entry_aliases(entry):
            stats["catalog_aliases"] += 1
            alias_key = normalize_key(alias)
            if alias_key in GENERIC_ALIASES:
                issues.append(_issue("generic_alias", f"catalog alias is too generic: {alias}", "warning", alias=alias, target=target))
            if len(alias.strip()) < 4 and not alias.strip().isupper():
                issues.append(_issue("short_alias", f"catalog alias is too short: {alias}", "warning", alias=alias, target=target))
            alias_targets.setdefault(alias_key, set()).add(normalize_key(target))

    for alias_key, targets in sorted(alias_targets.items()):
        if len(targets) > 1:
            issues.append(
                _issue(
                    "alias_conflict",
                    f"alias points to multiple targets: {alias_key}",
                    "error",
                    alias=alias_key,
                    targets=sorted(targets),
                )
            )
    return issues, stats


def audit_wiki_graph(wiki_dir: Path, catalog_path: Path | None = None) -> dict[str, Any]:
    if not wiki_dir.exists():
        decision = WorkflowDecision(
            kind="failed",
            phase="graph_audit",
            reason_code="wiki_dir_missing",
            public_summary="O caminho da Wiki nao foi encontrado.",
            developer_summary=f"Wiki dir not found: {wiki_dir}",
            evidence=[
                DecisionEvidence(
                    summary="wiki_dir ausente ou invalido.",
                    technical_code="wiki_dir_missing",
                    source="graph_audit",
                    affected_items=[str(wiki_dir)],
                )
            ],
            next_action="Rodar /mednotes:setup para configurar o caminho da Wiki antes de auditar o grafo.",
            required_inputs=["wiki_dir"],
        )
        payload: dict[str, Any] = {
            "schema": GRAPH_AUDIT_SCHEMA,
            "phase": "graph_audit",
            "status": "failed",
            "blocked_reason": "wiki_dir_missing",
            "next_action": "Rodar /mednotes:setup para configurar o caminho da Wiki antes de auditar o grafo.",
            "required_inputs": ["wiki_dir"],
            "ok": False,
            "error": f"Wiki dir not found: {wiki_dir}",
        }
        payload.update(decision_payload_from_decision(decision))
        return payload
    notes = _load_notes(wiki_dir)
    graph_notes = [note for note in notes if not note.is_index_note]
    ignored_target_keys = {normalize_key(note.stem) for note in notes if note.is_index_note}
    notes_by_stem: dict[str, list[NoteRecord]] = {}
    for note in graph_notes:
        notes_by_stem.setdefault(normalize_key(note.stem), []).append(note)

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    inbound: dict[str, int] = {note.relative_path: 0 for note in graph_notes}
    outbound: dict[str, int] = {note.relative_path: 0 for note in graph_notes}

    for stem_key, matches in notes_by_stem.items():
        if len(matches) > 1:
            errors.append(
                _issue(
                    "duplicate_stem",
                    f"multiple notes share the same Obsidian target name: {matches[0].stem}",
                    "error",
                    target=stem_key,
                    files=[item.relative_path for item in matches],
                )
            )

    catalog_issues, catalog_stats = _audit_catalog(catalog_path, notes_by_stem, ignored_target_keys=ignored_target_keys)
    for item in catalog_issues:
        (errors if item["severity"] == "error" else warnings).append(item)

    for index, note in enumerate(graph_notes, start=1):
        cooperative_cpu_yield(index)
        content = note.path.read_text(encoding="utf-8")
        related_span = _related_section_span(content)
        related_links: list[dict[str, Any]] = []
        has_no_strong_marker = bool(related_span and NO_STRONG_LINKS_MARKER in content[related_span[0] : related_span[1]])
        if related_span is None:
            warnings.append(_issue("missing_related_section", "missing ## 🔗 Notas Relacionadas section", "warning", file=note.relative_path))

        for link in _wikilinks(content):
            target = link["target"]
            target_key = normalize_key(target)
            if is_index_target(target) or target_key in ignored_target_keys:
                continue
            matches = notes_by_stem.get(target_key, [])
            issue_payload = {
                "file": note.relative_path,
                "line": link["line"],
                "target": target,
                "raw": link["raw"],
                "in_related": link["in_related"],
            }
            if not matches:
                errors.append(_issue("dangling_link", f"wikilink target does not exist: {target}", "error", **issue_payload))
                continue
            if len(matches) > 1:
                errors.append(_issue("ambiguous_link", f"wikilink target is ambiguous: {target}", "error", **issue_payload))
                continue
            target_note = matches[0]
            if target_note.relative_path == note.relative_path:
                errors.append(_issue("self_link", f"note links to itself: {target}", "error", **issue_payload))
                continue
            outbound[note.relative_path] += 1
            inbound[target_note.relative_path] += 1
            if link["in_related"]:
                related_links.append(link)

        if related_span is not None and len(related_links) < 2 and not has_no_strong_marker:
            warnings.append(
                _issue(
                    "few_related_links",
                    "related notes section has fewer than 2 valid links",
                    "warning",
                    file=note.relative_path,
                    valid_related_links=len(related_links),
                )
            )
        if related_span is not None and has_no_strong_marker and related_links:
            warnings.append(
                _issue(
                    "related_marker_with_links",
                    "remove the no-strong-links marker when related links are present",
                    "warning",
                    file=note.relative_path,
                )
            )

    orphan_notes = [
        note.relative_path
        for note in graph_notes
        if inbound[note.relative_path] == 0
    ]
    for rel_path in orphan_notes:
        warnings.append(_issue("orphan_note", "note has no inbound wiki links", "warning", file=rel_path))

    metrics = {
        "note_count": len(graph_notes),
        "ignored_index_note_count": len(notes) - len(graph_notes),
        "wikilink_count": sum(outbound.values()),
        "orphan_count": len(orphan_notes),
        **catalog_stats,
    }
    status = "failed" if errors else "completed_with_warnings" if warnings else "completed"
    blocked_reason = "graph_blockers" if errors else ""
    next_action = (
        "Rodar /mednotes:fix-wiki --dry-run para obter a rota segura antes de aplicar o linker."
        if errors
        else "Revisar warnings do graph-audit antes de aplicar mudanças no grafo."
        if warnings
        else ""
    )
    return {
        "schema": GRAPH_AUDIT_SCHEMA,
        "phase": "graph_audit",
        "status": status,
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "required_inputs": ["wiki_dir"],
        "human_decision_required": False,
        "ok": not errors,
        "wiki_dir": str(wiki_dir),
        "catalog_path": str(catalog_path) if catalog_path else None,
        "metrics": metrics,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "blocker_count": len(errors),
        "errors": errors,
        "warnings": warnings,
        "orphan_notes": orphan_notes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Wiki_Medicina graph health.")
    parser.add_argument("--config", help="Optional config.toml for legacy compatibility.")
    parser.add_argument("--wiki-dir", default=None)
    parser.add_argument("--catalog", "--catalog-path", default=os.getenv("MED_CATALOG_PATH", DEFAULT_CATALOG_PATH))
    parser.add_argument("--json", action="store_true", help="Emit JSON report. Accepted for explicitness; output is always JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolution = resolve_wiki_dir(explicit=args.wiki_dir, config=args.config, enable_gemini_probe=False)
    if not resolution.ok:
        print(json.dumps(resolution.as_payload(phase="graph_audit_path_resolution"), ensure_ascii=False, indent=2))
        return 3
    if resolution.path is None:
        print(json.dumps(resolution.as_payload(phase="graph_audit_path_resolution"), ensure_ascii=False, indent=2))
        return 3
    report = audit_wiki_graph(resolution.path, catalog_path=expand_path(args.catalog) if args.catalog else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
