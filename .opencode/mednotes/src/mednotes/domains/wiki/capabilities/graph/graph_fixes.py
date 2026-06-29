"""Deterministic graph fixes for Wiki_Medicina notes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.graph import graph as wiki_graph
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_target
from mednotes.domains.wiki.common import FileWriteError

GRAPH_FIX_SCHEMA = "medical-notes-workbench.wiki-graph-fix.v1"


def fix_wiki_graph(
    wiki_dir: Path,
    *,
    catalog_path: Path | None = None,
    apply: bool = False,
    backup: bool = False,
) -> dict[str, Any]:
    audit = wiki_graph.audit_wiki_graph(wiki_dir, catalog_path=catalog_path)
    link_issues = [
        issue
        for issue in audit.get("errors", [])
        if issue.get("code") in {"dangling_link", "self_link", "ambiguous_link"} and issue.get("file")
    ]
    issues_by_file: dict[str, list[dict[str, Any]]] = {}
    for issue in link_issues:
        issues_by_file.setdefault(str(issue["file"]), []).append(issue)

    reports: list[dict[str, Any]] = []
    changed_count = 0
    written_count = 0
    backup_paths: list[str] = []
    write_errors: list[dict[str, Any]] = []
    for relative_file, issues in sorted(issues_by_file.items()):
        path = wiki_dir / relative_file
        if not path.exists() or not path.is_file():
            continue
        report = {
            "path": str(path),
            "relative_path": relative_file,
            "changed": False,
            "would_write": False,
            "wrote": False,
            "backup": None,
            "write_error": None,
            "fixes_applied": [],
            "issue_codes": sorted({str(issue.get("code", "")) for issue in issues if issue.get("code")}),
            "delegated_to": "/mednotes:link/reference_repair",
            "link_repair_delegated": True,
            "delegated_issue_count": len(issues),
        }
        reports.append(report)

    duplicate_report = _fix_exact_duplicate_stems(
        wiki_dir,
        audit.get("errors", []),
        apply=apply,
        backup=backup,
    )
    backup_paths.extend(duplicate_report["backup_paths"])
    write_errors.extend(duplicate_report["write_errors"])

    return {
        "schema": GRAPH_FIX_SCHEMA,
        "wiki_dir": str(wiki_dir),
        "dry_run": not apply,
        "apply": apply,
        "backup": backup,
        "delegated_link_issue_count": len(link_issues),
        "changed_count": changed_count,
        "written_count": written_count,
        "write_error_count": len(write_errors),
        "write_errors": write_errors,
        "backup_paths": backup_paths,
        "reports": reports,
        "duplicates": duplicate_report,
        "unresolved_blocker_count": duplicate_report["merge_required_count"],
    }


def _fix_exact_duplicate_stems(
    wiki_dir: Path,
    errors: list[dict[str, Any]],
    *,
    apply: bool,
    backup: bool,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    backup_paths: list[str] = []
    write_errors: list[dict[str, Any]] = []
    removed_count = 0
    merge_required_count = 0

    for issue in errors:
        if issue.get("code") != "duplicate_stem":
            continue
        files = [str(item) for item in issue.get("files", []) if isinstance(item, str)]
        paths = [wiki_dir / file for file in files]
        existing = [path for path in paths if path.exists() and path.is_file()]
        if len(existing) < 2:
            continue
        existing = sorted(existing, key=lambda path: (len(path.relative_to(wiki_dir).parts), path.relative_to(wiki_dir).as_posix()))
        fingerprints = {_content_fingerprint(path) for path in existing}
        keep = existing[0]
        remove = existing[1:]
        if len(fingerprints) != 1:
            merge_required_count += 1
            reports.append(
                {
                    "target": issue.get("target"),
                    "files": [path.relative_to(wiki_dir).as_posix() for path in existing],
                    "action": "manual_merge_required",
                    "removed": [],
                }
            )
            continue
        removed: list[str] = []
        for path in remove:
            try:
                if apply:
                    path.unlink()
            except (FileWriteError, OSError) as exc:
                write_errors.append(
                    {
                        "path": str(path),
                        "backup": None,
                        "operation": "remove_exact_duplicate",
                        "error": str(exc),
                    }
                )
                continue
            removed.append(path.relative_to(wiki_dir).as_posix())
            removed_count += 1
        reports.append(
            {
                "target": issue.get("target"),
                "keep": keep.relative_to(wiki_dir).as_posix(),
                "action": "remove_exact_duplicates",
                "removed": removed,
            }
        )

    return {
        "removed_count": removed_count if apply else sum(len(item.get("removed", [])) for item in reports),
        "merge_required_count": merge_required_count,
        "backup_paths": backup_paths,
        "write_error_count": len(write_errors),
        "write_errors": write_errors,
        "reports": reports,
    }


def _content_fingerprint(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if is_index_target(path.stem) or _is_index_note(path, text):
        return ""
    return re.sub(r"\s+", "\n", text).strip()
