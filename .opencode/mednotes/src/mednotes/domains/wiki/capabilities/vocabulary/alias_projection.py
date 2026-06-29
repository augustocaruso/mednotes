"""Plan and apply DB-to-YAML alias projections."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field, StrictBool, StrictStr

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import ProjectionAlias, VocabularyMap
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

ALIAS_PROJECTION_PLAN_SCHEMA = "medical-notes-workbench.alias-projection-plan.v1"
ALIAS_PROJECTION_RECEIPT_SCHEMA = "medical-notes-workbench.alias-projection-receipt.v1"


class _AliasProjectionOperationPayload(ContractModel):
    operation_id: StrictStr = ""
    note_path: StrictStr = Field(min_length=1)
    before_hash: StrictStr = ""
    after_aliases: list[StrictStr] = Field(default_factory=list)
    preserved_contextual_aliases: list[StrictStr] = Field(default_factory=list)
    blocked_aliases: list[StrictStr] = Field(default_factory=list)
    requires_backup: StrictBool = False
    backup: StrictBool = False
    changed: StrictBool = False


class _AliasProjectionReceiptPayload(ContractModel):
    status: StrictStr
    blocked_reason: StrictStr = ""
    note_path: StrictStr = ""
    before_hash: StrictStr = ""
    after_hash: StrictStr = ""
    backup_path: StrictStr = ""


@dataclass(frozen=True)
class AliasProjectionReceipt:
    status: str
    blocked_reason: str = ""
    note_path: str = ""
    before_hash: str = ""
    after_hash: str = ""
    backup_path: str = ""

    def as_dict(self) -> JsonObject:
        return _AliasProjectionReceiptPayload(
            status=self.status,
            blocked_reason=self.blocked_reason,
            note_path=self.note_path,
            before_hash=self.before_hash,
            after_hash=self.after_hash,
            backup_path=self.backup_path,
        ).to_payload()


@dataclass
class AliasProjectionPlan:
    note_path: Path
    before_hash: str
    after_aliases: list[str]
    preserved_contextual_aliases: list[str]
    changed: bool
    backup: bool = False
    requires_backup: bool = False
    blocked_aliases: list[str] = field(default_factory=list)

    def as_operation(self) -> JsonObject:
        return _AliasProjectionOperationPayload(
            operation_id=f"alias_projection.update:{self.note_path}",
            note_path=str(self.note_path),
            before_hash=self.before_hash,
            after_aliases=self.after_aliases,
            preserved_contextual_aliases=self.preserved_contextual_aliases,
            blocked_aliases=self.blocked_aliases,
            requires_backup=self.requires_backup,
            backup=False,
            changed=self.changed,
        ).to_payload()

    def apply(self) -> AliasProjectionReceipt:
        current_hash = _file_hash(self.note_path)
        if current_hash != self.before_hash:
            return AliasProjectionReceipt(
                status="blocked",
                blocked_reason="alias_projection.stale_note_hash",
                note_path=str(self.note_path),
                before_hash=self.before_hash,
            )
        original = self.note_path.read_text(encoding="utf-8")
        updated = replace_aliases_in_frontmatter(original, self.after_aliases)
        if updated != original:
            atomic_write_text(self.note_path, updated)
        return AliasProjectionReceipt(
            status="applied",
            note_path=str(self.note_path),
            before_hash=self.before_hash,
            after_hash=_file_hash(self.note_path),
            backup_path="",
        )


def plan_alias_projection(vocab: VocabularyMap, *, note_path: Path, backup: bool = True) -> AliasProjectionPlan:
    candidates = vocab.note_aliases.get(str(note_path), [])
    selected = _dedupe_aliases(candidates)
    note_text = note_path.read_text(encoding="utf-8")
    title_key = normalize_key(_h1_title(note_text) or note_path.stem)
    after_aliases = [
        item.text
        for item in selected
        if item.visible_in_yaml and item.link_policy in {"direct", "requires_context"}
        and item.normalized_surface != title_key
    ]
    contextual = [
        item.text
        for item in selected
        if item.link_policy == "requires_context" and item.visible_in_yaml and item.normalized_surface != title_key
    ]
    blocked = [item.text for item in selected if item.visible_in_yaml and item.link_policy in {"blocked", "no_link"}]
    current = _current_aliases(note_text)
    return AliasProjectionPlan(
        note_path=note_path,
        before_hash=_file_hash(note_path),
        after_aliases=after_aliases,
        preserved_contextual_aliases=contextual,
        changed=current != after_aliases,
        backup=False,
        requires_backup=False,
        blocked_aliases=blocked,
    )


def build_alias_projection_plan(vocab: VocabularyMap, *, backup: bool = True) -> JsonObject:
    operations: list[JsonObject] = []
    for raw_path in sorted(vocab.note_aliases):
        note_path = Path(raw_path)
        if not note_path.is_file():
            continue
        operations.append(plan_alias_projection(vocab, note_path=note_path, backup=backup).as_operation())
    changed_count = sum(1 for item in operations if item.get("changed"))
    return {
        "schema": ALIAS_PROJECTION_PLAN_SCHEMA,
        "status": "planned",
        "db_path": str(vocab.db_path) if vocab.db_path else "",
        "operation_count": len(operations),
        "changed_count": changed_count,
        "operations": operations,
    }


def apply_alias_projection_plan(plan: JsonObject) -> JsonObject:
    if plan.get("schema") != ALIAS_PROJECTION_PLAN_SCHEMA:
        return {
            "schema": ALIAS_PROJECTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "alias_projection.invalid_plan_schema",
            "applied_count": 0,
            "blocked_count": 0,
            "receipts": [],
        }
    receipts: list[JsonObject] = []
    db_path = Path(str(plan.get("db_path") or ""))
    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        operations = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        projection_plan = _plan_from_operation(operation)
        receipt = _AliasProjectionReceiptPayload.model_validate(projection_plan.apply().as_dict())
        if (
            receipt.status == "applied"
            and receipt.before_hash != receipt.after_hash
            and db_path.is_file()
        ):
            _sync_note_hash(db_path, projection_plan.note_path, receipt.after_hash)
        receipts.append(receipt.to_payload())
    typed_receipts = [_AliasProjectionReceiptPayload.model_validate(item) for item in receipts]
    blocked_count = sum(1 for item in typed_receipts if item.status == "blocked")
    applied_count = sum(
        1
        for item in typed_receipts
        if item.status == "applied" and item.before_hash != item.after_hash
    )
    return {
        "schema": ALIAS_PROJECTION_RECEIPT_SCHEMA,
        "status": "blocked" if blocked_count else "completed",
        "blocked_reason": "alias_projection.blocked_operations" if blocked_count else "",
        "operation_count": len(receipts),
        "applied_count": applied_count,
        "blocked_count": blocked_count,
        "receipts": receipts,
    }


def _plan_from_operation(operation: JsonObject) -> AliasProjectionPlan:
    typed = _AliasProjectionOperationPayload.model_validate(JsonObjectAdapter.validate_python(operation))
    return AliasProjectionPlan(
        note_path=Path(typed.note_path),
        before_hash=typed.before_hash,
        after_aliases=list(typed.after_aliases),
        preserved_contextual_aliases=list(typed.preserved_contextual_aliases),
        changed=typed.changed,
        backup=False,
        requires_backup=False,
        blocked_aliases=list(typed.blocked_aliases),
    )


def _sync_note_hash(db_path: Path, note_path: Path, content_hash: str) -> None:
    if not content_hash:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE notes
            SET content_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE path = ? AND status = 'active'
            """,
            (content_hash, str(note_path)),
        )


def replace_aliases_in_frontmatter(text: str, aliases: list[str]) -> str:
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        alias_block = _format_aliases(aliases)
        return f"---\n{alias_block}---\n{body.lstrip(chr(10))}" if alias_block else body
    lines = frontmatter.splitlines(keepends=True)
    out: list[str] = []
    idx = 0
    replaced = False
    while idx < len(lines):
        key = _key_for(lines[idx])
        if key == "aliases":
            if aliases:
                out.append(_format_aliases(aliases))
            replaced = True
            idx += 1
            while idx < len(lines) and _key_for(lines[idx]) is None:
                idx += 1
            continue
        out.append(lines[idx])
        idx += 1
    if aliases and not replaced:
        out.insert(0, _format_aliases(aliases))
    rendered = "".join(out)
    if not rendered.strip():
        return body.lstrip("\n")
    return f"---\n{rendered}---\n{body.lstrip(chr(10))}"


def _dedupe_aliases(candidates: list[ProjectionAlias]) -> list[ProjectionAlias]:
    best: dict[str, ProjectionAlias] = {}
    for candidate in candidates:
        key = candidate.normalized_surface
        existing = best.get(key)
        if existing is None or _alias_score(candidate) > _alias_score(existing):
            best[key] = candidate
    return sorted(best.values(), key=lambda item: (_is_acronym(item.text), item.order, item.text.casefold()))


def _alias_score(candidate: ProjectionAlias) -> tuple[int, int, int, int]:
    policy_score = {"direct": 3, "requires_context": 2, "no_link": 1, "blocked": 0}.get(candidate.link_policy, 0)
    return (policy_score, *_display_score(candidate.text))


def _display_score(value: str) -> tuple[int, int, int]:
    return (
        int(any(ord(char) > 127 for char in value)),
        int(_is_acronym(value)),
        sum(1 for char in value if char.isupper()),
    )


def _is_acronym(value: str) -> bool:
    return value.isupper() and len(value) <= 8


def _file_hash(path: Path) -> str:
    return "sha256:" + file_sha256(path)


def _h1_title(text: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "".join(lines[1:idx]), "".join(lines[idx + 1 :])
    return None, text


def _key_for(line: str) -> str | None:
    match = re.match(r"^([A-Za-z0-9_-]+)\s*:", line)
    return match.group(1) if match else None


def _format_aliases(aliases: list[str]) -> str:
    if not aliases:
        return ""
    return "aliases:\n" + "".join(f"  - {json.dumps(alias, ensure_ascii=False)}\n" for alias in aliases)


def _current_aliases(text: str) -> list[str]:
    frontmatter, _body = _split_frontmatter(text)
    if not frontmatter:
        return []
    inline = re.search(r"(?m)^aliases\s*:\s*\[(.*?)\]\s*$", frontmatter)
    if inline:
        return [item.strip().strip("'\"") for item in inline.group(1).split(",") if item.strip()]
    block = re.search(r"(?m)^aliases\s*:\s*\n((?:\s*-\s*.*(?:\n|$))+)", frontmatter)
    if not block:
        return []
    aliases: list[str] = []
    for line in block.group(1).splitlines():
        match = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if match:
            aliases.append(match.group(1).strip().strip("'\""))
    return aliases
