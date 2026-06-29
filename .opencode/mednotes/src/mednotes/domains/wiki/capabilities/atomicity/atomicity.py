"""Plan and apply controlled atomicity split/rewrite bundles."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ConfigDict, Field

from mednotes.domains.wiki.capabilities.notes.note_style import infer_title, split_frontmatter, validate_note_style
from mednotes.domains.wiki.capabilities.notes.provenance import (
    ChatProvenance,
    apply_note_provenance,
    classify_note_provenance,
    merge_chat_provenance,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import FileWriteError, MissingPathError, ValidationError, wiki_cli_relative_command
from mednotes.domains.wiki.contracts.workflow_guardrails import (
    SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON,
    subagent_output_contract_errors,
)
from mednotes.domains.wiki.flows.link.link_triggers import LINK_TRIGGER_CONTEXT_SCHEMA, write_trigger_context
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

ATOMICITY_SPLIT_PLAN_SCHEMA = "medical-notes-workbench.atomicity-split-plan.v1"
ATOMICITY_SPLIT_BUNDLE_SCHEMA = "medical-notes-workbench.atomicity-split-bundle.v1"
ATOMICITY_SPLIT_RECEIPT_SCHEMA = "medical-notes-workbench.atomicity-split-receipt.v1"

ATOMICITY_REASONS = {"non_atomic_note", "one_note_multiple_meanings"}
ATOMICITY_PROBLEM_CODE = "identity.atomicity.one_note_multiple_meanings"
SUPPORTED_STRATEGIES = {"rename_source_and_create_notes", "rewrite_source_and_create_notes"}
IMAGE_FRONTMATTER_KEYS = {"images_enriched", "images_enriched_at", "image_count", "image_sources"}


class _AtomicityDeferredItemFields(ContractModel):
    """Typed fields consumed from deferred vocabulary/atomicity work items."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    reason: str = ""
    note_path: str = ""
    work_id: str = ""
    content_hash: str = ""
    semantic_signal: JsonObject = Field(default_factory=dict)
    atomicity_decision: str = ""


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_text).strip("-._").lower()
    return slug or "atomicity"


def _load_fix_wiki_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"Fix-wiki plan not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid fix-wiki plan JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "medical-notes-workbench.fix-wiki-plan.v1":
        raise ValidationError("Atomicity split planning requires medical-notes-workbench.fix-wiki-plan.v1.")
    return payload


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"Atomicity split bundle not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid atomicity split bundle JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ATOMICITY_SPLIT_BUNDLE_SCHEMA:
        raise ValidationError(f"Expected {ATOMICITY_SPLIT_BUNDLE_SCHEMA}: {path}")
    return payload


def _sha256_bytes(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_wiki_path(value: str, wiki_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = wiki_dir / path
    try:
        path.resolve(strict=False).relative_to(wiki_dir.resolve(strict=False))
    except ValueError as exc:
        raise ValidationError(f"Atomicity split path is outside wiki_dir: {path}") from exc
    return path


def _wiki_relative(path: Path, wiki_dir: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(wiki_dir.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)


def _frontmatter_blocks(frontmatter: str) -> dict[str, str]:
    lines = frontmatter.splitlines(keepends=True)
    blocks: dict[str, str] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:", line)
        if not match:
            idx += 1
            continue
        key = match.group(1).strip()
        block_lines = [line]
        idx += 1
        while idx < len(lines):
            next_line = lines[idx]
            if re.match(r"^[A-Za-z0-9_-]+\s*:", next_line):
                break
            block_lines.append(next_line)
            idx += 1
        blocks[key] = "".join(block_lines).strip()
    return blocks


def _image_metadata(text: str) -> dict[str, str]:
    frontmatter, _body = split_frontmatter(text)
    if frontmatter is None:
        return {}
    return {key: value for key, value in _frontmatter_blocks(frontmatter).items() if key in IMAGE_FRONTMATTER_KEYS}


def _chat_provenance_urls(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    state = classify_note_provenance(text)
    for value in [*state.chat_ids, *state.legacy_urls]:
        chat_id = ChatProvenance(str(value)).id
        if not chat_id:
            continue
        url = f"https://gemini.google.com/app/{chat_id}"
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _chat_provenance_from_text(text: str) -> list[ChatProvenance]:
    state = classify_note_provenance(text)
    chats = [ChatProvenance(str(value)) for value in state.chat_ids]
    chats.extend(ChatProvenance(str(value)) for value in state.legacy_urls)
    return merge_chat_provenance(chats)


class _SourceChatLookup:
    def __init__(self, source_path: Path, source_text: str) -> None:
        self.title = infer_title(source_text, source_path)

    def lookup_chat(self, chat_id: str) -> SimpleNamespace:
        chat = ChatProvenance(chat_id)
        return SimpleNamespace(
            id=chat.id,
            title=self.title or f"Chat {chat.id[:8]}",
            url=f"https://gemini.google.com/app/{chat.id}",
            date_created=chat.date_created,
            date_exported=chat.date_exported,
        )


def _canonicalize_output_provenance(text: str, *, source_path: Path, source_text: str) -> str:
    chats = _chat_provenance_from_text(source_text)
    if not chats:
        return text
    result = apply_note_provenance(
        text,
        chats=chats,
        chat_lookup=_SourceChatLookup(source_path, source_text),
    )
    return str(result["text"])


def _output_items(bundle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    replacement = bundle.get("replacement_source")
    if not isinstance(replacement, dict):
        raise ValidationError("atomicity-split-bundle requires replacement_source.")
    items = [("replacement_source", replacement)]
    created = bundle.get("created_notes") if isinstance(bundle.get("created_notes"), list) else []
    for item in created:
        if isinstance(item, dict):
            items.append(("created_notes", item))
    return items


def _output_paths(bundle: dict[str, Any], wiki_dir: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for kind, item in _output_items(bundle):
        title = str(item.get("title") or "").strip()
        target_path = _safe_wiki_path(str(item.get("target_path") or ""), wiki_dir)
        content_path = Path(str(item.get("content_path") or "")).expanduser()
        outputs.append(
            {
                "kind": kind,
                "title": title,
                "target_path": target_path,
                "content_path": content_path,
            }
        )
    return outputs


def _validation_errors(bundle: dict[str, Any], *, wiki_dir: Path) -> list[JsonObject]:
    errors: list[JsonObject] = []
    if not str(bundle.get("work_id") or "").strip():
        errors.append(
            {
                "code": "work_id_missing",
                "message": "Bundle must copy work_id from the official atomicity work item.",
            }
        )
    strategy = str(bundle.get("strategy") or "")
    if not strategy:
        errors.append({"code": "strategy_missing", "message": "atomicity-split-bundle requires strategy."})
    elif strategy not in SUPPORTED_STRATEGIES:
        errors.append({"code": "unsupported_strategy", "message": f"Unsupported atomicity strategy: {strategy}"})
    try:
        source_path = _safe_wiki_path(str(bundle.get("source_path") or ""), wiki_dir)
        outputs = _output_paths(bundle, wiki_dir)
    except ValidationError as exc:
        code = "output_contract_missing" if "requires replacement_source" in str(exc) else "unsafe_path"
        return [{"code": code, "message": str(exc)}]

    if not source_path.is_file():
        errors.append({"code": "source_missing", "path": str(source_path), "message": "Source note is missing."})
        return errors
    expected_hash = str(bundle.get("source_hash") or "")
    actual_hash = _sha256_bytes(source_path)
    if not expected_hash:
        errors.append(
            {
                "code": "source_hash_missing",
                "path": str(source_path),
                "actual_hash": actual_hash,
                "message": "Bundle must copy source_hash from the official atomicity work item.",
            }
        )
    elif expected_hash != actual_hash:
        errors.append(
            {
                "code": "source_changed",
                "path": str(source_path),
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
                "message": "Source note changed since atomicity bundle was produced.",
            }
        )

    source_text = source_path.read_text(encoding="utf-8")
    source_urls = _chat_provenance_urls(source_text)
    combined_output_text = ""
    image_policy = str(bundle.get("image_metadata_policy") or "")
    for output in outputs:
        title = str(output["title"])
        target_path = output["target_path"]
        content_path = output["content_path"]
        if not title:
            errors.append({"code": "title_missing", "path": str(target_path), "message": "Output title is required."})
        if title and target_path.stem != title:
            errors.append(
                {
                    "code": "title_path_mismatch",
                    "path": str(target_path),
                    "title": title,
                    "message": "Output title must match target filename stem.",
                }
            )
        if not content_path.is_file():
            errors.append(
                {"code": "content_missing", "path": str(content_path), "message": "Output Markdown content is missing."}
            )
            continue
        text = _canonicalize_output_provenance(
            content_path.read_text(encoding="utf-8"),
            source_path=source_path,
            source_text=source_text,
        )
        combined_output_text += "\n" + text
        inferred = infer_title(text, target_path)
        if title and inferred != title:
            errors.append(
                {
                    "code": "content_title_mismatch",
                    "path": str(content_path),
                    "title": title,
                    "actual_title": inferred,
                    "message": "Output H1/title must match bundle title.",
                }
            )
        style_report = validate_note_style(text, title=title or target_path.stem, path=str(target_path))
        for issue in style_report.get("errors", []):
            errors.append(
                {
                    "code": "style_contract_failed",
                    "path": str(content_path),
                    "message": str(issue.get("message") or issue.get("code") or "Wiki style validation failed"),
                    "issue": issue,
                }
            )
        if output["kind"] == "created_notes" and _image_metadata(text):
            if not image_policy:
                errors.append(
                    {
                        "code": "created_note_image_metadata_without_policy",
                        "path": str(content_path),
                        "message": "Created note includes images_* metadata without explicit image metadata policy.",
                    }
                )
            elif image_policy == "do_not_copy_images_to_new_notes":
                errors.append(
                    {
                        "code": "created_note_image_metadata_forbidden",
                        "path": str(content_path),
                        "message": "Created note includes images_* metadata despite do_not_copy_images_to_new_notes.",
                    }
                )

    for url in source_urls:
        if url not in combined_output_text:
            errors.append({"code": "provenance_url_missing", "url": url, "message": f"Missing source URL: {url}"})

    replacement_target = outputs[0]["target_path"] if outputs else source_path
    rewrite_same_target = strategy == "rewrite_source_and_create_notes" and replacement_target == source_path
    if replacement_target.exists() and not rewrite_same_target and replacement_target != source_path:
        errors.append(
            {
                "code": "replacement_target_exists",
                "path": str(replacement_target),
                "message": "Replacement target already exists.",
            }
        )
    for output in outputs[1:]:
        target = output["target_path"]
        if target.exists():
            errors.append({"code": "created_target_exists", "path": str(target), "message": "Created target exists."})
    return errors


def _blocked_receipt(
    *,
    bundle_path: Path,
    wiki_dir: Path,
    blocked_reason: str,
    next_action: str,
    validation_errors: list[JsonObject] | None = None,
) -> JsonObject:
    return JsonObjectAdapter.validate_python({
        "schema": ATOMICITY_SPLIT_RECEIPT_SCHEMA,
        "phase": "atomicity_split_apply",
        "status": "blocked",
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "bundle_path": str(bundle_path),
        "wiki_dir": str(wiki_dir),
        "validation_errors": validation_errors or [],
        "written_count": 0,
        "backup_paths": [],
        "linker_status": "skipped",
    })


def _trigger_context_payload(
    *,
    wiki_dir: Path,
    bundle_path: Path,
    source_path: Path,
    replacement_target: Path,
    created_targets: list[Path],
) -> dict[str, Any]:
    changed_notes: list[dict[str, Any]] = []
    if replacement_target == source_path:
        changed_notes.append(
            {
                "change_type": "modified",
                "content_change": "text",
                "path": _wiki_relative(replacement_target, wiki_dir),
                "title": replacement_target.stem,
                "after_hash": _sha256_bytes(replacement_target),
            }
        )
    else:
        changed_notes.append(
            {
                "change_type": "renamed",
                "content_change": "text",
                "old_path": _wiki_relative(source_path, wiki_dir),
                "old_title": source_path.stem,
                "path": _wiki_relative(replacement_target, wiki_dir),
                "title": replacement_target.stem,
                "after_hash": _sha256_bytes(replacement_target),
            }
        )
    for path in created_targets:
        changed_notes.append(
            {
                "change_type": "created",
                "content_change": "text",
                "path": _wiki_relative(path, wiki_dir),
                "title": path.stem,
                "after_hash": _sha256_bytes(path),
            }
        )
    return {
        "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
        "source_workflow": "/mednotes:fix-wiki",
        "batch_id": bundle_path.stem,
        "changed_notes": changed_notes,
    }


def _same_path(left: str, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return str(Path(left).expanduser()) == str(right)


def _deferred_work_item_preflight(
    *,
    db_path: Path | None,
    bundle: dict[str, Any],
    source_path: Path,
) -> dict[str, Any]:
    work_id = str(bundle.get("work_id") or "")
    if db_path is None:
        return {"status": "skipped", "skipped_reason": "vocabulary_db_missing", "work_id": work_id}
    if not db_path.exists():
        return {"status": "skipped", "skipped_reason": "vocabulary_db_not_found", "work_id": work_id}
    if not work_id:
        return {"status": "skipped", "skipped_reason": "work_id_missing", "work_id": ""}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT work_id, note_path, content_hash, status
                FROM deferred_work_items
                WHERE work_id = ?
                """,
                (work_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        return {
            "status": "blocked",
            "blocked_reason": "deferred_work_item_read_failed",
            "work_id": work_id,
            "error": str(exc),
        }
    if row is None:
        return {"status": "skipped", "skipped_reason": "deferred_work_item_not_found", "work_id": work_id}
    status = str(row["status"] or "")
    if status not in {"pending", "claimed"}:
        return {
            "status": "blocked",
            "blocked_reason": "deferred_work_item_not_pending",
            "work_id": work_id,
            "current_status": status,
        }
    note_path = str(row["note_path"] or "")
    if note_path and not _same_path(note_path, source_path):
        return {
            "status": "blocked",
            "blocked_reason": "deferred_work_item_source_mismatch",
            "work_id": work_id,
            "expected_note_path": note_path,
            "actual_note_path": str(source_path),
        }
    content_hash = str(row["content_hash"] or "")
    source_hash = str(bundle.get("source_hash") or "")
    if content_hash and source_hash and content_hash != source_hash:
        return {
            "status": "blocked",
            "blocked_reason": "deferred_work_item_stale",
            "work_id": work_id,
            "expected_hash": content_hash,
            "actual_hash": source_hash,
        }
    return {"status": "ready", "work_id": work_id, "previous_status": status}


def _complete_deferred_work_item(
    *,
    db_path: Path | None,
    preflight: dict[str, Any],
    source_path: Path,
    replacement_target: Path,
    created_targets: list[Path],
    receipt_path: Path,
) -> dict[str, Any]:
    if preflight.get("status") != "ready":
        return dict(preflight)
    if db_path is None:
        return {"status": "skipped", "skipped_reason": "vocabulary_db_missing"}
    work_id = str(preflight.get("work_id") or "")
    payload = {
        "completed_by": "apply-atomicity-split",
        "source_path": str(source_path),
        "replacement_target_path": str(replacement_target),
        "created_paths": [str(path) for path in created_targets],
        "receipt_path": str(receipt_path),
    }
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE deferred_work_items
                SET status = 'completed',
                    payload_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE work_id = ? AND status IN ('pending', 'claimed')
                """,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), work_id),
            )
    except sqlite3.Error as exc:
        return {"status": "failed", "blocked_reason": "deferred_work_item_update_failed", "work_id": work_id, "error": str(exc)}
    if cursor.rowcount != 1:
        return {"status": "failed", "blocked_reason": "deferred_work_item_update_missed", "work_id": work_id}
    return {"status": "completed", "work_id": work_id}


def _problem_note_paths(problem: dict[str, Any]) -> list[str]:
    direct_path = str(problem.get("note_path") or "")
    if direct_path:
        return [direct_path]
    evidence = problem.get("evidence") if isinstance(problem.get("evidence"), dict) else {}
    issue_path = str(evidence.get("note_path") or "")
    if issue_path:
        return [issue_path]
    issues = evidence.get("issues") if isinstance(evidence.get("issues"), list) else []
    paths: list[str] = []
    for issue in issues:
        if isinstance(issue, dict) and issue.get("note_path"):
            paths.append(str(issue["note_path"]))
    return paths


def _deferred_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = item.get("payload_json")
    if isinstance(payload_json, str) and payload_json.strip():
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _atomicity_source_items(*, problems: list[Any], deferred: list[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for item in problems:
        if not isinstance(item, dict) or item.get("code") != ATOMICITY_PROBLEM_CODE:
            continue
        for path in _problem_note_paths(item):
            if path not in seen:
                seen.add(path)
                items.append({"source_path": path})
    for item in deferred:
        if not isinstance(item, dict) or item.get("reason") not in ATOMICITY_REASONS:
            continue
        fields = _AtomicityDeferredItemFields.model_validate(item)
        path = fields.note_path
        if path and path not in seen:
            seen.add(path)
            payload = _deferred_payload(item)
            payload_fields = _AtomicityDeferredItemFields.model_validate(payload)
            signal = fields.semantic_signal or payload_fields.semantic_signal
            atomicity_decision = fields.atomicity_decision or payload_fields.atomicity_decision
            items.append(
                {
                    "source_path": path,
                    "work_id": fields.work_id,
                    "content_hash": fields.content_hash,
                    "semantic_signal": signal,
                    "atomicity_decision": atomicity_decision,
                }
            )
    return items


def _atomicity_work_item(
    *,
    source_path: str,
    temp_root: Path,
    index: int,
    work_id: str = "",
    content_hash: str = "",
    semantic_signal: dict[str, Any] | None = None,
    atomicity_decision: str = "",
) -> dict[str, Any]:
    source = Path(source_path)
    official_work_id = work_id or f"atomicity-split-{index:03d}-{_slug(source.stem)}"
    work_dir_name = _slug(official_work_id)
    source_hash = _sha256_bytes(source) if source.is_file() else content_hash
    return {
        "work_id": official_work_id,
        "agent": "med-knowledge-architect",
        "item_type": "wiki_atomicity_split",
        "mode": "wiki_atomicity_split",
        "source_path": source_path,
        "source_hash": source_hash,
        "owner_key": source_path,
        "bundle_output_path": str(temp_root / work_dir_name / "atomicity-split-bundle.json"),
        "temp_markdown_dir": str(temp_root / work_dir_name / "markdown"),
        "semantic_signal": semantic_signal or {},
        "atomicity_decision": atomicity_decision,
        "allowed_strategies": sorted(SUPPORTED_STRATEGIES),
        "required_bundle_fields": [
            "schema",
            "workflow",
            "phase",
            "agent",
            "source_workflow",
            "work_id",
            "source_path",
            "source_hash",
            "strategy",
            "replacement_source",
            "created_notes",
        ],
        "replacement_source_schema": {"title": "string", "target_path": "wiki-relative-or-absolute", "content_path": "temp markdown path"},
        "created_notes_item_schema": {"title": "string", "target_path": "wiki-relative-or-absolute", "content_path": "temp markdown path"},
        "instructions": [
            "Read exactly the source note and the provided context packet.",
            "Return atomicity-split-bundle.v1 with workflow=/mednotes:fix-wiki, phase=atomicity_split, agent=med-knowledge-architect and source_workflow=/mednotes:fix-wiki.",
            "Copy work_id exactly from this work item.",
            "Copy source_path and source_hash exactly from this work item.",
            "Never compute, shorten, patch, or invent source_hash; block if it is missing.",
            "Use one allowed strategy exactly as listed in allowed_strategies.",
            "replacement_source must be an object with title, target_path and content_path.",
            "created_notes must contain objects with title, target_path and content_path.",
            "Preserve all chat provenance in the resulting notes.",
            "Do not mutate the Wiki, call subagents, or invent aliases.",
        ],
    }


def build_atomicity_split_plan(
    *,
    fix_wiki_plan_path: Path,
    batch_id: str,
    temp_root: Path,
    limit: int = 20,
) -> dict[str, Any]:
    payload = _load_fix_wiki_plan(fix_wiki_plan_path)
    problems = payload.get("problems")
    if not isinstance(problems, list):
        problems = payload.get("fix_wiki_problems") if isinstance(payload.get("fix_wiki_problems"), list) else []
    deferred = payload.get("deferred_work_items") if isinstance(payload.get("deferred_work_items"), list) else []
    source_items = _atomicity_source_items(problems=problems, deferred=deferred)
    work_items = [
        _atomicity_work_item(
            source_path=str(source_item.get("source_path") or ""),
            temp_root=temp_root,
            index=index,
            work_id=str(source_item.get("work_id") or ""),
            content_hash=str(source_item.get("content_hash") or ""),
            semantic_signal=source_item.get("semantic_signal") if isinstance(source_item.get("semantic_signal"), dict) else {},
            atomicity_decision=str(source_item.get("atomicity_decision") or ""),
        )
        for index, source_item in enumerate(source_items[:limit], start=1)
    ]
    return {
        "schema": ATOMICITY_SPLIT_PLAN_SCHEMA,
        "phase": "atomicity_split",
        "status": "ready" if work_items else "skipped",
        "skipped_reason": "" if work_items else "no_atomicity_work",
        "batch_id": batch_id,
        "source_fix_wiki_plan_path": str(fix_wiki_plan_path),
        "source_plan_hash": str(payload.get("plan_hash") or ""),
        "source_snapshot_hash": str(payload.get("snapshot_hash") or ""),
        "item_count": len(work_items),
        "work_items": work_items,
        "canonical_parent_commands": [
            f"apply split: {wiki_cli_relative_command('apply-atomicity-split --bundle /tmp/mnw/atomicity-split-bundle.json --json')}"
        ],
        "rules": [
            "Each med-knowledge-architect writes one atomicity-split-bundle.v1.",
            "Subagents may write temporary Markdown outputs only under temp_root.",
            "Subagents do not mutate the Wiki and do not call subagents.",
        ],
    }


def apply_atomicity_split_bundle(
    *,
    bundle_path: Path,
    wiki_dir: Path,
    backup: bool,
    defer_linker: bool = False,
    parent_batch_id: str = "",
    vocabulary_db_path: Path | None = None,
) -> dict[str, Any]:
    backup = False
    if defer_linker and not parent_batch_id:
        return {
            "schema": ATOMICITY_SPLIT_RECEIPT_SCHEMA,
            "phase": "atomicity_split_apply",
            "status": "blocked",
            "blocked_reason": "invalid_linker_deferral",
            "next_action": "Passe parent_batch_id ou rode o linker neste apply.",
            "bundle_path": str(bundle_path),
            "wiki_dir": str(wiki_dir),
            "written_count": 0,
        }

    bundle = _load_bundle(bundle_path)
    contract_errors = subagent_output_contract_errors(
        bundle,
        expected_schema=ATOMICITY_SPLIT_BUNDLE_SCHEMA,
        expected_workflow="/mednotes:fix-wiki",
        expected_phase="atomicity_split",
        allowed_agents={"med-knowledge-architect"},
        source_workflow="/mednotes:fix-wiki",
    )
    if contract_errors:
        return _blocked_receipt(
            bundle_path=bundle_path,
            wiki_dir=wiki_dir,
            blocked_reason=SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON,
            next_action=(
                "Regenerar o atomicity-split-bundle com med-knowledge-architect direto a partir do work item oficial; "
                "não use @generalist nem output sem workflow/phase/source_workflow."
            ),
            validation_errors=[
                {
                    "code": error["code"],
                    "message": (
                        f"{SUBAGENT_OUTPUT_CONTRACT_BLOCKED_REASON}: {error['field']} "
                        f"expected {error['expected']} got {error['actual']}"
                    ),
                }
                for error in contract_errors
            ],
        )
    errors = _validation_errors(bundle, wiki_dir=wiki_dir)
    if errors:
        return _blocked_receipt(
            bundle_path=bundle_path,
            wiki_dir=wiki_dir,
            blocked_reason="validation_failed",
            next_action=(
                "Regenerar o atomicity-split-bundle a partir do work item oficial; não edite "
                "source_hash, strategy, replacement_source ou created_notes manualmente."
            ),
            validation_errors=errors,
        )

    source_path = _safe_wiki_path(str(bundle["source_path"]), wiki_dir)
    deferred_work_item = _deferred_work_item_preflight(
        db_path=vocabulary_db_path,
        bundle=bundle,
        source_path=source_path,
    )
    if deferred_work_item.get("status") == "blocked":
        return _blocked_receipt(
            bundle_path=bundle_path,
            wiki_dir=wiki_dir,
            blocked_reason=str(deferred_work_item.get("blocked_reason") or "deferred_work_item_blocked"),
            next_action="Regenerar o plano de atomicidade pelo fix-wiki atual antes de aplicar este bundle.",
            validation_errors=[
                {
                    "code": str(deferred_work_item.get("blocked_reason") or "deferred_work_item_blocked"),
                    "message": json.dumps(deferred_work_item, ensure_ascii=False, sort_keys=True),
                }
            ],
        )
    outputs = _output_paths(bundle, wiki_dir)
    replacement = outputs[0]
    replacement_target = replacement["target_path"]
    created_outputs = outputs[1:]
    created_targets = [item["target_path"] for item in created_outputs]
    receipt_path = bundle_path.with_name("atomicity-split-receipt.json")
    trigger_context_path = bundle_path.with_name("atomicity-link-trigger-context.json")
    backup_paths: list[str] = []
    originals = {source_path: source_path.read_text(encoding="utf-8")}
    if replacement_target.exists():
        originals[replacement_target] = replacement_target.read_text(encoding="utf-8")
    source_text = originals[source_path]
    try:
        replacement_text = _canonicalize_output_provenance(
            replacement["content_path"].read_text(encoding="utf-8"),
            source_path=source_path,
            source_text=source_text,
        )
        atomic_write_text(replacement_target, replacement_text)
        for output in created_outputs:
            output_text = _canonicalize_output_provenance(
                output["content_path"].read_text(encoding="utf-8"),
                source_path=source_path,
                source_text=source_text,
            )
            atomic_write_text(output["target_path"], output_text)
        if str(bundle.get("strategy") or "") == "rename_source_and_create_notes" and replacement_target != source_path:
            source_path.unlink()
    except (FileWriteError, OSError) as exc:
        rollback_errors: list[dict[str, str]] = []
        for path, text in originals.items():
            try:
                atomic_write_text(path, text)
            except (FileWriteError, OSError) as rollback_exc:
                rollback_errors.append({"path": str(path), "error": str(rollback_exc)})
        return {
            "schema": ATOMICITY_SPLIT_RECEIPT_SCHEMA,
            "phase": "atomicity_split_apply",
            "status": "failed",
            "blocked_reason": "io_error_rollback_performed",
            "next_action": "Inspecionar erro de IO/rollback antes de repetir o split.",
            "bundle_path": str(bundle_path),
            "wiki_dir": str(wiki_dir),
            "io_error": str(exc),
            "rollback": {"performed": True, "errors": rollback_errors},
            "written_count": 0,
            "backup_paths": backup_paths,
        }

    trigger_context = _trigger_context_payload(
        wiki_dir=wiki_dir,
        bundle_path=bundle_path,
        source_path=source_path,
        replacement_target=replacement_target,
        created_targets=created_targets,
    )
    write_trigger_context(trigger_context_path, trigger_context)
    deferred_work_item = _complete_deferred_work_item(
        db_path=vocabulary_db_path,
        preflight=deferred_work_item,
        source_path=source_path,
        replacement_target=replacement_target,
        created_targets=created_targets,
        receipt_path=receipt_path,
    )
    receipt = {
        "schema": ATOMICITY_SPLIT_RECEIPT_SCHEMA,
        "phase": "atomicity_split_apply",
        "status": "completed",
        "blocked_reason": "",
        "next_action": (
            "Acumular trigger contexts do lote e rodar /mednotes:link uma vez."
            if defer_linker
            else "Rodar /mednotes:link com o trigger context emitido."
        ),
        "bundle_path": str(bundle_path),
        "wiki_dir": str(wiki_dir),
        "strategy": str(bundle.get("strategy") or ""),
        "source_path": str(source_path),
        "replacement_target_path": str(replacement_target),
        "created_paths": [str(path) for path in created_targets],
        "written_count": 1 + len(created_targets),
        "backup": backup,
        "backup_paths": backup_paths,
        "receipt_path": str(receipt_path),
        "link_trigger_context": trigger_context,
        "link_trigger_context_path": str(trigger_context_path),
        "linker_trigger_context_path": str(trigger_context_path),
        "linker_status": "deferred" if defer_linker else "not_run",
        "parent_batch_id": parent_batch_id,
        "linker_pending_reason": "parent_batch_will_run_linker_once" if defer_linker else "",
        "deferred_work_item": deferred_work_item,
    }
    atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt
