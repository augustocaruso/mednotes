#!/usr/bin/env python3
"""Emit the current Wiki_Medicina folder tree with canonical taxonomy context."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_paths import ensure_runtime_paths  # noqa: E402

ensure_runtime_paths()

from mednotes.domains.wiki import api as wiki_api  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print Wiki_Medicina taxonomy context as JSON.")
    parser.add_argument("--config", help="Optional config.toml. Reads [chat_processor].")
    parser.add_argument("--raw-dir", help="Override Chats_Raw directory.")
    parser.add_argument("--wiki-dir", help="Override Wiki_Medicina directory.")
    parser.add_argument("--catalog-path", help="Override CATALOGO_WIKI.json path.")
    parser.add_argument("--max-depth", type=int, default=4, help="Current tree depth; 0 means all depths.")
    parser.add_argument("--audit", action="store_true", help="Include dry-run audit against the canonical taxonomy.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format.")
    parser.add_argument("--text", action="store_true", help="Shortcut for --format text.")
    return parser


def taxonomy_context(args: argparse.Namespace) -> dict[str, Any]:
    config = wiki_api.resolve_config(args)
    payload = {
        "wiki_dir": str(config.wiki_dir),
        "canonical_taxonomy": wiki_api.canonical_taxonomy_tree(),
        "current_tree": wiki_api.taxonomy_tree(config.wiki_dir, max_depth=args.max_depth),
    }
    if args.audit:
        payload["audit"] = wiki_api.taxonomy_audit(config.wiki_dir)
    return payload


def _format_canonical_taxonomy(canonical: dict[str, Any]) -> list[str]:
    lines = ["Taxonomia canônica:"]
    for area in canonical.get("areas", []):
        lines.append(f"- {area['area']}/")
        for specialty in area.get("specialties", []):
            lines.append(f"  - {specialty}/")
    return lines


def _format_current_tree(tree: dict[str, Any]) -> list[str]:
    lines = [f"Árvore atual: {tree.get('wiki_dir', '')}"]
    directories = tree.get("directories", [])
    if not directories:
        lines.append("- <sem pastas>")
        return lines
    for item in directories:
        parts = item.get("parts", [])
        if not parts:
            continue
        depth = int(item.get("depth", len(parts)))
        indent = "  " * max(depth - 1, 0)
        direct_notes = int(item.get("direct_note_count", 0))
        child_dirs = int(item.get("child_dir_count", 0))
        details = []
        if direct_notes:
            details.append(f"{direct_notes} nota{'s' if direct_notes != 1 else ''}")
        if child_dirs:
            details.append(f"{child_dirs} pasta{'s' if child_dirs != 1 else ''}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"{indent}- {parts[-1]}/{suffix}")
    return lines


def _format_audit(audit: dict[str, Any]) -> list[str]:
    lines = ["Auditoria dry-run:"]
    summary = (
        ("missing_canonical_dirs", "pastas canônicas ausentes"),
        ("proposed_moves", "movimentos propostos"),
        ("unmapped_top_level_dirs", "pastas de topo sem mapeamento"),
        ("duplicate_destinations", "destinos duplicados"),
        ("duplicate_directory_groups", "grupos duplicados"),
        ("root_notes", "notas na raiz"),
    )
    for key, label in summary:
        items = audit.get(key, [])
        lines.append(f"- {label}: {len(items)}")
        for item in items[:12]:
            if isinstance(item, dict) and "source" in item and "destination" in item:
                lines.append(f"  - {item['source']} -> {item['destination']} ({item.get('reason', 'review')})")
            else:
                lines.append(f"  - {item}")
        if len(items) > 12:
            lines.append(f"  - ... +{len(items) - 12}")
    lines.append(f"- requer revisão: {bool(audit.get('requires_review'))}")
    return lines


def taxonomy_context_text(payload: dict[str, Any]) -> str:
    sections = [
        _format_canonical_taxonomy(payload["canonical_taxonomy"]),
        _format_current_tree(payload["current_tree"]),
    ]
    if "audit" in payload:
        sections.append(_format_audit(payload["audit"]))
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.text:
            args.format = "text"
        payload = taxonomy_context(args)
        if args.format == "text":
            print(taxonomy_context_text(payload), end="")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return wiki_api.EXIT_OK
    except wiki_api.WikiPathResolutionError as exc:
        print(json.dumps(exc.payload(phase="taxonomy_context_path_resolution"), ensure_ascii=False, indent=2))
        return exc.exit_code
    except wiki_api.MedOpsError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
