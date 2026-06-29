"""Apply process-chats canonical merges into existing Wiki notes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.graph.coverage import validate_raw_coverage_structure
from mednotes.domains.wiki.capabilities.markdown.markdown_query import (
    MarkdownQueryUnavailable,
    ensure_markdown_query_available,
    markdown_query_blocked_payload,
)
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import FrontmatterYamlUnavailable, infer_title
from mednotes.domains.wiki.capabilities.notes.provenance import _apply_note_provenance_from_raw_files
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text, mutate_raw_frontmatter
from mednotes.domains.wiki.capabilities.style.style import apply_style_rewrite
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import MissingPathError, ValidationError, _now_iso
from mednotes.domains.wiki.config import MedConfig, _path
from mednotes.domains.wiki.contracts.workflow_guardrails import annotate_payload
from mednotes.domains.wiki.flows.link.link_triggers import LINK_TRIGGER_CONTEXT_SCHEMA

CANONICAL_MERGE_APPLY_SCHEMA = "medical-notes-workbench.canonical-merge-apply.v1"


def _load_coverage_primary_raw(path: Path) -> Path:
    if not path.exists():
        raise MissingPathError(f"Coverage inventory not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid coverage inventory JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Coverage inventory must be a JSON object")
    raw_file = str(data.get("raw_file") or "").strip()
    if not raw_file:
        raise ValidationError("Coverage inventory missing raw_file")
    return _path(raw_file)


def _load_coverage_planned_meaning_keys(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set()
    return {
        normalize_key(str(item.get("staged_title") or item.get("title") or ""))
        for item in items
        if isinstance(item, dict) and str(item.get("action") or "") == "planned_meaning"
    }


def _hash_if_present(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _wiki_relative_path(path: Path, config: MedConfig) -> str:
    try:
        return path.relative_to(config.wiki_dir).as_posix()
    except ValueError:
        return str(path)


def _rollback(originals: dict[Path, str]) -> dict[str, list[str]]:
    result = {"restored": [], "rollback_errors": []}
    for path, text in originals.items():
        try:
            atomic_write_text(path, text)
            result["restored"].append(str(path))
        except Exception as exc:  # pragma: no cover - OS-level rollback failure
            result["rollback_errors"].append(f"{path}: {exc}")
    return result


def _trigger_context(config: MedConfig, target_path: Path, *, coverage_path: Path) -> dict[str, Any]:
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": "/mednotes:process-chats",
        "batch_id": str(coverage_path),
        "changed_notes": [
            {
                "change_type": "modified",
                "content_change": "text",
                "path": _wiki_relative_path(target_path, config),
                "title": target_path.stem,
                "after_hash": _hash_if_present(target_path),
            }
        ],
    }


def _prepare_merge_content(content: str, *, content_path: Path, raw_paths: list[Path], coverage: dict[str, Any]) -> str:
    title = infer_title(content, content_path)
    try:
        result = _apply_note_provenance_from_raw_files(
            content,
            raw_files=raw_paths,
            title=title,
            coverage_summary=coverage,
        )
    except FrontmatterYamlUnavailable as exc:
        raise ValidationError(f"{exc.blocked_reason}: {exc.next_action}") from exc
    except ValueError as exc:
        raise ValidationError(f"chat_provenance_invalid: {exc}") from exc
    return str(result["text"])


def apply_canonical_merge(
    config: MedConfig,
    target_path: Path,
    content_path: Path,
    coverage_path: Path,
    *,
    dry_run: bool = False,
    backup: bool = False,
) -> dict[str, Any]:
    backup = False
    raw_file = _load_coverage_primary_raw(coverage_path)
    if not dry_run:
        try:
            ensure_markdown_query_available(
                wiki_dir=config.wiki_dir,
                raw_dir=config.raw_dir,
                state_dir=config.state_dir,
            )
        except MarkdownQueryUnavailable as exc:
            return annotate_payload(
                {
                    **markdown_query_blocked_payload(
                        phase="canonical_merge_apply",
                        required_inputs=["target", "content", "coverage"],
                    ),
                    "error_context": {
                        "blocked_reason": exc.blocked_reason,
                        "root_cause": "markdown_query_index_unavailable",
                        "affected_artifact": "markdown_query_index",
                        "error_summary": str(exc),
                        "suggested_fix": exc.next_action,
                        "next_action": exc.next_action,
                        "retry_scope": "setup_markdown_query_index_then_retry",
                        "details": exc.payload,
                    },
                },
                phase="canonical_merge_apply",
                status="blocked",
                blocked_reason=exc.blocked_reason,
                next_action=exc.next_action,
                required_inputs=["target", "content", "coverage"],
                human_decision_required=False,
            )
    coverage = validate_raw_coverage_structure(
        coverage_path,
        raw_file,
        require_triage_note_plan=True,
    )
    coverage_keys = _load_coverage_planned_meaning_keys(coverage_path)
    if normalize_key(target_path.stem) not in coverage_keys:
        raise ValidationError(
            "Canonical merge coverage does not include the existing target title: "
            f"{target_path.stem}"
        )

    raw_paths = [_path(path) for path in coverage["raw_files"]]
    prepared_content = _prepare_merge_content(
        content_path.read_text(encoding="utf-8"),
        content_path=content_path,
        raw_paths=raw_paths,
        coverage=coverage,
    )
    preflight = apply_style_rewrite(
        target_path,
        content_path,
        dry_run=True,
        backup=False,
        rewritten_content=prepared_content,
    )
    result: dict[str, Any] = {
        "schema": CANONICAL_MERGE_APPLY_SCHEMA,
        "phase": "canonical_merge_apply",
        "target_path": str(target_path),
        "content_path": str(content_path),
        "coverage_path": str(coverage_path),
        "dry_run": dry_run,
        "backup": backup,
        "coverage": coverage,
        "validation": preflight["validation"],
        "changed": preflight["changed"],
        "written": False,
        "backup_path": None,
        "raw_updates": [],
        "processed_raw_count": 0,
        "link_trigger_context": None,
    }
    if preflight["validation"]["errors"]:
        return annotate_payload(
            result,
            phase="canonical_merge_apply",
            status="blocked",
            blocked_reason="validation_errors",
            next_action="Corrigir o rewrite gerado pelo architect e repetir apply-canonical-merge --dry-run.",
            required_inputs=["target", "content", "coverage"],
        )
    if not preflight["changed"]:
        return annotate_payload(
            result,
            phase="canonical_merge_apply",
            status="blocked",
            blocked_reason="no_delta_to_merge",
            next_action="Reclassificar a unidade como not_a_note se não houver delta, ou pedir novo rewrite ao architect.",
            required_inputs=["target", "content", "coverage"],
        )
    if dry_run:
        return annotate_payload(
            result,
            phase="canonical_merge_apply",
            status="ready",
            blocked_reason="",
            next_action="Aplicar com apply-canonical-merge; rollback fica no ponto de restauração do vault.",
            required_inputs=["target", "content", "coverage"],
        )

    originals = {target_path: target_path.read_text(encoding="utf-8")}
    originals.update({path: path.read_text(encoding="utf-8") for path in raw_paths})
    try:
        applied = apply_style_rewrite(
            target_path,
            content_path,
            dry_run=False,
            backup=backup,
            rewritten_content=prepared_content,
        )
        if applied["validation"]["errors"]:
            result["validation"] = applied["validation"]
            return annotate_payload(
                result,
                phase="canonical_merge_apply",
                status="blocked",
                blocked_reason="validation_errors",
                next_action="Corrigir o rewrite gerado pelo architect e repetir apply-canonical-merge --dry-run.",
                required_inputs=["target", "content", "coverage"],
            )
        result["written"] = bool(applied.get("written"))
        result["backup_path"] = applied.get("backup_path")
        raw_updates = [
            mutate_raw_frontmatter(
                path,
                {"status": "processado", "processed_at": _now_iso()},
                backup=backup,
            )
            for path in raw_paths
        ]
    except Exception as exc:
        rollback = _rollback(originals)
        result["rollback"] = rollback
        result["error"] = str(exc)
        return annotate_payload(
            result,
            phase="canonical_merge_apply",
            status="failed",
            blocked_reason="io_error_rollback_performed",
            next_action="Inspecionar erro de IO/rollback e repetir somente depois de liberar arquivos bloqueados.",
            required_inputs=["target", "content", "coverage"],
        )

    result["raw_updates"] = raw_updates
    result["processed_raw_count"] = len(raw_updates)
    result["link_trigger_context"] = _trigger_context(config, target_path, coverage_path=coverage_path)
    return annotate_payload(
        result,
        phase="canonical_merge_apply",
        status="completed",
        blocked_reason="",
        next_action="Rodar linker a partir do trigger context gerado.",
        required_inputs=["target", "content", "coverage"],
    )
