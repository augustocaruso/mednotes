"""Fresh vocabulary bootstrap/reset helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from pydantic import Field

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    FrontmatterYamlUnavailable,
    canonical_wiki_tags,
    dump_frontmatter_yaml,
    load_frontmatter_yaml,
    split_frontmatter,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import obsidian_target_name
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import (
    initialize_vocabulary_db,
    note_content_hash,
    upsert_note,
)
from mednotes.domains.wiki.common import _now_iso
from mednotes.domains.wiki.config import MedConfig
from mednotes.kernel.base import ContractModel, JsonObjectAdapter, JsonValue

BOOTSTRAP_PLAN_SCHEMA = "medical-notes-workbench.vocabulary-bootstrap-plan.v1"
BOOTSTRAP_RECEIPT_SCHEMA = "medical-notes-workbench.vocabulary-bootstrap-receipt.v1"


class PreservedFrontmatter(ContractModel):
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    chats: JsonValue | None = None
    image_fields: dict[str, str | list[str]] = Field(default_factory=dict)


class SemanticIngestionQueueItem(TypedDict):
    schema: str
    note_path: str
    content_hash: str
    queue_flags: list[str]
    assigned_agent: str


class BootstrapNoteOperationPayload(TypedDict):
    path: str
    before_hash: str
    after_hash: str
    queue_flags: list[str]
    changed: bool


class BootstrapPlanPayload(TypedDict):
    schema: str
    wiki_dir: str
    note_count: int
    note_operations: list[BootstrapNoteOperationPayload]


class BootstrapReceiptPayload(TypedDict, total=False):
    schema: str
    generated_at: str
    status: str
    trigger: str
    automatic: bool
    db_path: str
    wiki_dir: str
    plan_path: str
    queue_path: str
    receipt_path: str
    note_count: int
    queued_note_count: int
    changed_files: list[str]
    backup_paths: list[str]
    dry_run: bool
    note_count_deferred: bool
    deferred_reason: str


class SemanticIngestionQueuePayload(TypedDict):
    schema: str
    generated_at: str
    db_path: str
    item_count: int
    items: list[SemanticIngestionQueueItem]


@dataclass(frozen=True)
class BootstrapNoteOperation:
    path: Path
    before_hash: str
    after_text: str
    after_frontmatter: PreservedFrontmatter
    queue_flags: list[str]

    @property
    def changed(self) -> bool:
        return self.path.read_text(encoding="utf-8") != self.after_text if self.path.exists() else True

    def queue_item(self) -> SemanticIngestionQueueItem:
        return {
            "schema": "medical-notes-workbench.note-semantic-ingestion-queue.v1",
            "note_path": str(self.path),
            "content_hash": "sha256:" + _sha256_text(self.after_text),
            "queue_flags": self.queue_flags,
            "assigned_agent": "med-link-graph-curator",
        }


@dataclass
class BootstrapPlan:
    wiki_dir: Path
    note_operations: list[BootstrapNoteOperation] = field(default_factory=list)
    schema: str = BOOTSTRAP_PLAN_SCHEMA

    def as_dict(self) -> BootstrapPlanPayload:
        return {
            "schema": self.schema,
            "wiki_dir": str(self.wiki_dir),
            "note_count": len(self.note_operations),
            "note_operations": [
                {
                    "path": str(item.path),
                    "before_hash": item.before_hash,
                    "after_hash": "sha256:" + _sha256_text(item.after_text),
                    "queue_flags": item.queue_flags,
                    "changed": item.changed,
                }
                for item in self.note_operations
            ],
        }


def plan_fresh_bootstrap_reset(*, wiki_dir: Path) -> BootstrapPlan:
    operations: list[BootstrapNoteOperation] = []
    for path in iter_notes(wiki_dir) if wiki_dir.exists() else []:
        text = path.read_text(encoding="utf-8")
        if _is_index_note(path, text):
            continue
        after_text, after_frontmatter = _reset_note_text(text)
        operations.append(
            BootstrapNoteOperation(
                path=path,
                before_hash="sha256:" + file_sha256(path),
                after_text=after_text,
                after_frontmatter=after_frontmatter,
                queue_flags=["aliases_missing", "concept_mapping_missing"],
            )
        )
    return BootstrapPlan(wiki_dir=wiki_dir, note_operations=operations)


def apply_fresh_bootstrap_reset(
    *,
    wiki_dir: Path,
    db_path: Path,
    backup: bool = False,
    run_dir: Path | None = None,
    trigger: str = "vocabulary_db_missing",
    automatic: bool = True,
) -> BootstrapReceiptPayload:
    plan = plan_fresh_bootstrap_reset(wiki_dir=wiki_dir)
    run_dir = run_dir or db_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "vocabulary-bootstrap-plan.json"
    queue_path = run_dir / "note-semantic-ingestion-queue.json"
    receipt_path = run_dir / "vocabulary-bootstrap-receipt.json"
    atomic_write_text(plan_path, json.dumps(plan.as_dict(), ensure_ascii=False, indent=2) + "\n")

    backup_paths: list[str] = []
    changed_files: list[str] = []
    if db_path.exists():
        archive_path = run_dir / f"{db_path.name}.before-reset"
        archive_path.write_bytes(db_path.read_bytes())
        db_path.unlink()
    initialize_vocabulary_db(db_path)

    queue_items: list[SemanticIngestionQueueItem] = []
    with sqlite3.connect(db_path) as conn:
        for operation in plan.note_operations:
            if operation.changed:
                atomic_write_text(operation.path, operation.after_text)
                changed_files.append(str(operation.path))
            content_hash = note_content_hash(operation.path)
            note_id = upsert_note(conn, path=operation.path, title=_title_for(operation.after_text, operation.path), content_hash=content_hash)
            queue_item = {
                "schema": "medical-notes-workbench.note-semantic-ingestion-queue.v1",
                "note_path": str(operation.path),
                "content_hash": content_hash,
                "queue_flags": operation.queue_flags,
                "assigned_agent": "med-link-graph-curator",
            }
            queue_items.append(queue_item)
            conn.execute(
                """
                INSERT INTO note_semantic_ingestion_queue(note_id, note_path, content_hash, queue_flags_json, assigned_agent, status)
                VALUES (?, ?, ?, ?, 'med-link-graph-curator', 'pending')
                ON CONFLICT(note_path, content_hash) DO UPDATE SET
                  queue_flags_json=excluded.queue_flags_json,
                  assigned_agent='med-link-graph-curator',
                  status='pending',
                  updated_at=CURRENT_TIMESTAMP
                """,
                (note_id, str(operation.path), content_hash, json.dumps(operation.queue_flags, ensure_ascii=False)),
            )
    queue_payload: SemanticIngestionQueuePayload = {
        "schema": "medical-notes-workbench.note-semantic-ingestion-queue.v1",
        "generated_at": _now_iso(),
        "db_path": str(db_path),
        "item_count": len(queue_items),
        "items": queue_items,
    }
    atomic_write_text(queue_path, json.dumps(queue_payload, ensure_ascii=False, indent=2) + "\n")
    receipt: BootstrapReceiptPayload = {
        "schema": BOOTSTRAP_RECEIPT_SCHEMA,
        "generated_at": _now_iso(),
        "status": "queued_semantic_ingestion" if queue_items else "completed",
        "trigger": trigger,
        "automatic": automatic,
        "db_path": str(db_path),
        "wiki_dir": str(wiki_dir),
        "plan_path": str(plan_path),
        "queue_path": str(queue_path),
        "receipt_path": str(receipt_path),
        "note_count": len(plan.note_operations),
        "queued_note_count": len(queue_items),
        "changed_files": changed_files,
        "backup_paths": backup_paths,
    }
    atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def ensure_vocabulary_db(
    config: MedConfig,
    *,
    run_dir: Path | None = None,
    backup: bool = False,
    force_reset: bool = False,
    trigger: str = "vocabulary_db_missing",
) -> BootstrapReceiptPayload:
    backup = False
    db_path = getattr(config, "vocabulary_db_path", None)
    if db_path is None:
        return {
            "status": "skipped",
            "trigger": "vocabulary_db_unconfigured",
            "automatic": False,
            "db_path": "",
        }
    if db_path.exists() and not force_reset:
        return {
            "status": "existing",
            "trigger": "vocabulary_db_present",
            "automatic": False,
            "db_path": str(db_path),
        }
    return apply_fresh_bootstrap_reset(
        wiki_dir=config.wiki_dir,
        db_path=db_path,
        backup=backup,
        run_dir=run_dir,
        trigger="explicit_vocabulary_reset" if force_reset else trigger,
        automatic=not force_reset,
    )


def planned_vocabulary_bootstrap(
    config: MedConfig,
    *,
    force_reset: bool = False,
    scan_notes: bool = True,
) -> BootstrapReceiptPayload:
    db_path = config.vocabulary_db_path
    if db_path is None:
        return {
            "status": "skipped",
            "trigger": "vocabulary_db_unconfigured",
            "automatic": False,
            "db_path": "",
        }
    if db_path.exists() and not force_reset:
        return {
            "status": "existing",
            "trigger": "vocabulary_db_present",
            "automatic": False,
            "db_path": str(db_path),
        }
    if not scan_notes:
        return {
            "schema": BOOTSTRAP_RECEIPT_SCHEMA,
            "generated_at": _now_iso(),
            "status": "planned",
            "trigger": "explicit_vocabulary_reset" if force_reset else "vocabulary_db_missing",
            "automatic": not force_reset,
            "db_path": str(db_path),
            "wiki_dir": str(config.wiki_dir),
            "note_count": 0,
            "queued_note_count": 0,
            "changed_files": [],
            "backup_paths": [],
            "dry_run": True,
            "note_count_deferred": True,
            "deferred_reason": "apply_will_generate_vocabulary_bootstrap_receipt",
        }
    plan = plan_fresh_bootstrap_reset(wiki_dir=config.wiki_dir)
    return {
        "schema": BOOTSTRAP_RECEIPT_SCHEMA,
        "generated_at": _now_iso(),
        "status": "planned",
        "trigger": "explicit_vocabulary_reset" if force_reset else "vocabulary_db_missing",
        "automatic": not force_reset,
        "db_path": str(db_path),
        "wiki_dir": str(config.wiki_dir),
        "note_count": len(plan.note_operations),
        "queued_note_count": len(plan.note_operations),
        "changed_files": [],
        "backup_paths": [],
        "dry_run": True,
    }


def resolve_vocabulary_bootstrap(
    config: MedConfig,
    *,
    apply: bool,
    run_dir: Path | None = None,
    backup: bool = False,
    force_reset: bool = False,
    scan_notes: bool = True,
) -> BootstrapReceiptPayload:
    db_path = config.vocabulary_db_path
    should_apply = apply or force_reset or bool(db_path and db_path.exists())
    if should_apply:
        return ensure_vocabulary_db(config, run_dir=run_dir, backup=backup, force_reset=force_reset)
    return planned_vocabulary_bootstrap(config, force_reset=force_reset, scan_notes=scan_notes)


def _reset_note_text(text: str) -> tuple[str, PreservedFrontmatter]:
    frontmatter, body = split_frontmatter(text)
    after_frontmatter = _extract_preserved_frontmatter(frontmatter or "")
    body = _replace_wikilinks_preserving_protected_zones(body)
    rendered = _render_frontmatter(after_frontmatter)
    normalized_body = body.lstrip("\n")
    return (rendered + normalized_body if rendered else normalized_body), after_frontmatter


def _extract_preserved_frontmatter(frontmatter: str) -> PreservedFrontmatter:
    try:
        structured = load_frontmatter_yaml(f"---\n{frontmatter}---\n") if frontmatter.strip() else {}
    except FrontmatterYamlUnavailable:
        structured = {}
    tags = canonical_wiki_tags(_extract_yaml_list(frontmatter, "tags"))
    chats: JsonValue | None = None
    raw_chats: object = structured.get("chats")
    if raw_chats:
        chats = _coerce_json_value(raw_chats)
    image_fields: dict[str, str | list[str]] = {}
    for key in re.findall(r"(?m)^([A-Za-z0-9_-]+)\s*:", frontmatter):
        if key.startswith(("images_", "image_")):
            values = _extract_yaml_list(frontmatter, key)
            image_fields[key] = values if values else _extract_scalar(frontmatter, key)
    return PreservedFrontmatter(tags=tags, chats=chats, image_fields=image_fields)


def _coerce_json_value(value: object) -> JsonValue | None:
    if value is None:
        return None
    try:
        return JsonObjectAdapter.validate_python({"value": value})["value"]
    except ValueError:
        return str(value)


def _extract_yaml_list(frontmatter: str, key: str) -> list[str]:
    inline = re.search(rf"(?m)^{re.escape(key)}\s*:\s*\[(.*?)\]\s*$", frontmatter)
    if inline:
        return [_clean_yaml_item(item) for item in inline.group(1).split(",") if item.strip()]
    block = re.search(rf"(?m)^{re.escape(key)}[ \t]*:[ \t]*\n((?:[ \t]*-[ \t]*.*(?:\n|$))+)", frontmatter)
    if not block:
        return []
    values: list[str] = []
    for line in block.group(1).splitlines():
        match = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if match:
            values.append(_clean_yaml_item(match.group(1)))
    return [value for value in values if value]


def _extract_scalar(frontmatter: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}\s*:\s*(.*?)\s*$", frontmatter)
    return _clean_yaml_item(match.group(1)) if match else ""


def _clean_yaml_item(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _render_frontmatter(data: PreservedFrontmatter) -> str:
    lines: list[str] = []
    if data.aliases:
        lines.append("aliases:\n")
        lines.extend(f"  - {json.dumps(alias, ensure_ascii=False)}\n" for alias in data.aliases)
    if data.tags:
        lines.append("tags:\n")
        lines.extend(f"  - {tag}\n" if re.match(r"^[A-Za-z0-9_/-]+$", tag) else f"  - {json.dumps(tag, ensure_ascii=False)}\n" for tag in data.tags)
    if data.chats is not None:
        lines.extend(_dump_structured_frontmatter_item("chats", data.chats))
    for key, value in data.image_fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:\n")
            lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}\n" for item in value)
        elif value:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}\n")
    return f"---\n{''.join(lines)}---\n" if lines else ""


def _dump_structured_frontmatter_item(key: str, value: JsonValue) -> list[str]:
    return list(dump_frontmatter_yaml({key: value}).splitlines(keepends=True))


_RELATED_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
_NEXT_H2_RE = re.compile(r"(?m)^##\s+")
_FOOTER_RE = re.compile(r"(?m)^---\s*$")
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")


def _replace_wikilinks_preserving_protected_zones(text: str) -> str:
    spans = _protected_spans(text)
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(_replace_wikilinks(text[cursor:start]))
        parts.append(text[start:end])
        cursor = end
    parts.append(_replace_wikilinks(text[cursor:]))
    return "".join(parts)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"```.*?```", text, re.DOTALL):
        spans.append((match.start(), match.end()))
    for match in re.finditer(r"!\[\[[^\]]+\]\]", text):
        spans.append((match.start(), match.end()))
    for match in _FOOTER_RE.finditer(text):
        tail = text[match.start() :]
        if "[[_Índice_Medicina]]" in tail or "Chat Original" in tail:
            spans.append((match.start(), len(text)))
            break
    for line in re.finditer(r"(?m)^#.*$", text):
        spans.append((line.start(), line.end()))
    spans.extend(_related_section_spans(text))
    return _merge_spans(spans)


def _related_section_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _RELATED_RE.finditer(text):
        next_h2 = _NEXT_H2_RE.search(text, match.end())
        footer = _FOOTER_RE.search(text, match.end())
        candidates = [item.start() for item in (next_h2, footer) if item is not None]
        spans.append((match.start(), min(candidates) if candidates else len(text)))
    return spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _replace_wikilinks(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if "|" in raw:
            return raw.rsplit("|", 1)[1].strip()
        target = raw.split("#", 1)[0].strip()
        return obsidian_target_name(target) if target else raw

    return _WIKILINK_RE.sub(replace, text)


def _title_for(text: str, path: Path) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or path.stem
    return path.stem


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
