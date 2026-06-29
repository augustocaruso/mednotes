"""Official recovery helpers for the vocabulary SQLite state."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import (
    initialize_vocabulary_db,
    note_content_hash,
    upsert_note,
)
from mednotes.domains.wiki.common import FileWriteError, ValidationError, _now_iso, wiki_cli_command
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue

VOCABULARY_STATUS_SCHEMA = "medical-notes-workbench.vocabulary-status.v1"
VOCABULARY_RECOVERY_PLAN_SCHEMA = "medical-notes-workbench.vocabulary-recovery-plan.v1"
VOCABULARY_RECOVERY_RECEIPT_SCHEMA = "medical-notes-workbench.vocabulary-recovery-receipt.v1"

QUEUE_STATUSES = ("pending", "claimed", "applied", "stale", "blocked")
REQUIRED_TABLE_COLUMNS = {
    "notes": {"id", "path", "title", "stem", "content_hash", "status"},
    "meanings": {"id", "label", "normalized_label", "semantic_type", "atomic_status", "status"},
    "surfaces": {"id", "normalized_surface", "best_display_text", "intrinsically_ambiguous"},
    "meaning_note_links": {"id", "meaning_id", "note_id", "role", "status", "confidence"},
    "surface_meaning_policy": {"id", "surface_id", "meaning_id", "link_policy", "source"},
    "note_semantic_ingestion_queue": {"id", "note_id", "note_path", "content_hash", "status"},
}


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


class _QueueCounts(ContractModel):
    pending: int = 0
    claimed: int = 0
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    orphan: int = 0


class _SchemaDriftIssue(ContractModel):
    code: str = ""
    message: str = ""
    table: str = ""
    column: str = ""


class _QueueIssue(ContractModel):
    code: str = ""
    message: str = ""
    queue_id: int = 0
    note_id: int = 0
    note_path: str = ""
    actual_path: str = ""
    content_hash: str = ""
    expected_hash: str = ""
    actual_hash: str = ""
    status: str = ""


class _VocabularyStatusPayload(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-status.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    status: Literal["ready", "degraded", "blocked"]
    schema_status: Literal["ready", "blocked"]
    blocked_reason: str = ""
    db_path: str = ""
    db_exists: bool = False
    queue_counts: _QueueCounts = Field(default_factory=_QueueCounts)
    object_counts: JsonObject = Field(default_factory=dict)
    schema_drift: list[_SchemaDriftIssue] = Field(default_factory=list)
    queue_issues: list[_QueueIssue] = Field(default_factory=list)
    queue_issue_count: int = 0
    mutated: bool = False
    recovery_command: str = ""
    next_action: str = ""
    degraded_reasons: list[str] = Field(default_factory=list)


class _CatalogHint(ContractModel):
    available: bool = False
    title: str = ""
    alias_count: int = 0


class _RecoveryActionFields(ContractModel):
    action: str = ""
    code: str = ""
    db_path: str = ""
    queue_id: int = 0
    note_id: int = 0
    note_path: str = ""
    actual_path: str = ""
    title: str = ""
    content_hash: str = ""
    expected_hash: str = ""
    actual_hash: str = ""
    from_status: str = ""
    to_status: str = ""
    reason: str = ""
    queue_flags: list[str] = Field(default_factory=list)
    assigned_agent: str = ""
    catalog_hint: _CatalogHint = Field(default_factory=_CatalogHint)
    skipped_reason: str = ""
    applied_count: int = 0


class _RecoveryPlanPayload(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-recovery-plan.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    status: Literal["planned", "blocked", "skipped"]
    blocked_reason: str = ""
    mode: str = ""
    db_path: str = ""
    backup_path: str = ""
    actions: list[JsonObject] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)
    created_at: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    plan_id: str = ""


class _RecoveryReceiptPayload(ContractModel):
    schema_: Literal["medical-notes-workbench.vocabulary-recovery-receipt.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    status: Literal["pending", "completed", "applied", "blocked"]
    blocked_reason: str = ""
    mode: str = ""
    plan_id: str = ""
    db_path: str = ""
    backup_path: str = ""
    applied_actions: list[JsonObject] = Field(default_factory=list)
    actions: list[JsonObject] = Field(default_factory=list)
    skipped_items: list[JsonObject] = Field(default_factory=list)
    error_items: list[JsonObject] = Field(default_factory=list)
    diagnosis_after: JsonObject = Field(default_factory=dict)
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    created_at: str = ""


def _status_payload(payload: object) -> _VocabularyStatusPayload:
    return _VocabularyStatusPayload.model_validate(_json_object(payload))


def _plan_payload(payload: object) -> _RecoveryPlanPayload:
    return _RecoveryPlanPayload.model_validate(_json_object(payload))


def _receipt_payload(payload: object) -> JsonObject:
    return _RecoveryReceiptPayload.model_validate(_json_object(payload)).to_payload()


def _action_fields(raw: JsonObject) -> _RecoveryActionFields | None:
    try:
        return _RecoveryActionFields.model_validate(raw)
    except PydanticValidationError:
        return None


def _action_name(raw: JsonObject) -> str:
    action = _action_fields(raw)
    return action.action if action is not None else ""


def _action_with(raw: JsonObject, key: str, value: JsonValue) -> JsonObject:
    return _json_object({**raw, key: value})


def _action_payload(action: _RecoveryActionFields) -> JsonObject:
    payload = action.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    return _json_object(payload)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _queue_counts(conn: sqlite3.Connection, *, has_queue: bool) -> dict[str, int]:
    counts = dict.fromkeys(QUEUE_STATUSES, 0)
    counts["orphan"] = 0
    if not has_queue:
        return counts
    for status, count in conn.execute(
        "SELECT status, COUNT(*) FROM note_semantic_ingestion_queue GROUP BY status"
    ).fetchall():
        if str(status) in counts:
            counts[str(status)] = int(count)
    try:
        counts["orphan"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM note_semantic_ingestion_queue AS q
                LEFT JOIN notes AS n ON n.id = q.note_id
                WHERE n.id IS NULL AND q.status IN ('pending', 'claimed', 'stale')
                """
            ).fetchone()[0]
        )
    except sqlite3.DatabaseError:
        counts["orphan"] = 0
    return counts


def _object_counts(conn: sqlite3.Connection, *, tables: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("notes", "meanings", "surfaces", "surface_meaning_policy", "note_semantic_ingestion_queue"):
        if table not in tables:
            continue
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.DatabaseError:
            counts[table] = 0
    return counts


def _case_only_match(path: Path) -> Path | None:
    parent = path.parent
    if not parent.is_dir():
        return None
    wanted = path.name.casefold()
    try:
        for child in parent.iterdir():
            if child.name.casefold() == wanted and child.name != path.name:
                return child
    except OSError:
        return None
    return None


def _queue_integrity_issues(conn: sqlite3.Connection, *, has_queue: bool) -> list[JsonObject]:
    if not has_queue:
        return []
    issues: list[JsonObject] = []
    try:
        rows = conn.execute(
            """
            SELECT id, note_path, content_hash, status
            FROM note_semantic_ingestion_queue
            WHERE status IN ('pending', 'claimed', 'stale')
            ORDER BY id ASC
            """
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        return [_QueueIssue(code="queue_unreadable", message=str(exc)).to_payload()]
    for row in rows:
        queue_id = int(row[0])
        note_path = Path(str(row[1] or ""))
        expected_hash = str(row[2] or "")
        status = str(row[3] or "")
        actual_case_path = _case_only_match(note_path)
        if actual_case_path is not None:
            issues.append(
                _QueueIssue(
                    code="queue_note_path_case_mismatch",
                    queue_id=queue_id,
                    note_path=str(note_path),
                    actual_path=str(actual_case_path),
                    status=status,
                ).to_payload()
            )
            continue
        if not note_path.is_file():
            issues.append(
                _QueueIssue(
                    code="queue_note_path_missing",
                    queue_id=queue_id,
                    note_path=str(note_path),
                    status=status,
                ).to_payload()
            )
            continue
        actual_hash = note_content_hash(note_path)
        if expected_hash and actual_hash != expected_hash:
            issues.append(
                _QueueIssue(
                    code="queue_note_hash_stale",
                    queue_id=queue_id,
                    note_path=str(note_path),
                    content_hash=expected_hash,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    status=status,
                ).to_payload()
            )
        elif status == "stale":
            issues.append(
                _QueueIssue(
                    code="queue_stale_ready_for_retry",
                    queue_id=queue_id,
                    note_path=str(note_path),
                    content_hash=expected_hash,
                    actual_hash=actual_hash,
                    status=status,
                ).to_payload()
            )
    try:
        orphan_rows = conn.execute(
            """
            SELECT q.id, q.note_id, q.note_path, q.content_hash, q.status
            FROM note_semantic_ingestion_queue AS q
            LEFT JOIN notes AS n ON n.id = q.note_id
            WHERE n.id IS NULL AND q.status IN ('pending', 'claimed', 'stale')
            ORDER BY q.id ASC
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        orphan_rows = []
    for row in orphan_rows:
        issues.append(
            _QueueIssue(
                code="queue_orphan_note_id",
                queue_id=int(row[0]),
                note_id=int(row[1] or 0),
                note_path=str(row[2] or ""),
                content_hash=str(row[3] or ""),
                status=str(row[4] or ""),
            ).to_payload()
        )
    try:
        claimed_rows = conn.execute(
            """
            SELECT id, note_path, content_hash, status
            FROM note_semantic_ingestion_queue
            WHERE status='claimed'
            ORDER BY id ASC
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        claimed_rows = []
    for row in claimed_rows:
        issues.append(
            _QueueIssue(
                code="queue_claimed_without_active_agent",
                queue_id=int(row[0]),
                note_path=str(row[1] or ""),
                content_hash=str(row[2] or ""),
                status=str(row[3] or ""),
            ).to_payload()
        )
    return issues


def vocabulary_status(db_path: Path) -> JsonObject:
    db_path = Path(db_path)
    recovery = wiki_cli_command("vocabulary-recover", "--mode", "reconcile-queue", "--dry-run", "--json")
    if not db_path.exists():
        recovery = wiki_cli_command("vocabulary-recover", "--mode", "rebuild-db", "--dry-run", "--json")
        return _VocabularyStatusPayload(
            schema=VOCABULARY_STATUS_SCHEMA,
            status="blocked",
            schema_status="blocked",
            blocked_reason="vocabulary_db_missing",
            db_path=str(db_path),
            db_exists=False,
            queue_counts=_QueueCounts(),
            object_counts={},
            schema_drift=[
                _SchemaDriftIssue(code="db_missing", message="vocabulary DB does not exist")
            ],
            queue_issues=[],
            queue_issue_count=0,
            recovery_command=recovery,
            next_action=recovery,
        ).to_payload()

    schema_drift: list[JsonObject] = []
    queue_issues: list[JsonObject] = []
    object_counts: dict[str, int] = {}
    try:
        with _connect_readonly(db_path) as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
                if table not in tables:
                    schema_drift.append(
                        _SchemaDriftIssue(code="missing_table", table=table, message=f"missing table {table}").to_payload()
                    )
                    continue
                columns = _table_columns(conn, table)
                missing = sorted(required_columns - columns)
                for column in missing:
                    schema_drift.append(
                        _SchemaDriftIssue(
                            code="missing_column",
                            table=table,
                            column=column,
                            message=f"missing {table}.{column}",
                        ).to_payload()
                    )
                if table == "meanings" and "type" in columns:
                    schema_drift.append(
                        _SchemaDriftIssue(
                            code="legacy_column",
                            table=table,
                            column="type",
                            message="legacy meanings.type column detected",
                        ).to_payload()
                    )
                if table == "meanings" and "source" in columns:
                    schema_drift.append(
                        _SchemaDriftIssue(
                            code="legacy_column",
                            table=table,
                            column="source",
                            message="legacy meanings.source column detected",
                        ).to_payload()
                    )
            if "aliases" in tables:
                schema_drift.append(
                    _SchemaDriftIssue(code="legacy_table", table="aliases", message="legacy aliases table detected").to_payload()
                )
            has_queue = "note_semantic_ingestion_queue" in tables
            counts = _queue_counts(conn, has_queue=has_queue)
            object_counts = _object_counts(conn, tables=tables)
            if not schema_drift:
                queue_issues = _queue_integrity_issues(conn, has_queue=has_queue)
    except sqlite3.DatabaseError as exc:
        return _VocabularyStatusPayload(
            schema=VOCABULARY_STATUS_SCHEMA,
            status="blocked",
            schema_status="blocked",
            blocked_reason="vocabulary_sqlite_integrity_error",
            db_path=str(db_path),
            db_exists=True,
            queue_counts=_QueueCounts(),
            object_counts={},
            schema_drift=[_SchemaDriftIssue(code="database_unreadable", message=str(exc))],
            queue_issues=[],
            queue_issue_count=0,
            recovery_command=recovery,
            next_action=recovery,
        ).to_payload()
    unsupported_schema_drift = any(
        _SchemaDriftIssue.model_validate(issue).code in {"missing_column", "legacy_column", "legacy_table"}
        for issue in schema_drift
    )
    queue_blocking_codes = {"queue_orphan_note_id", "queue_claimed_without_active_agent"}
    queue_blocked = any(_QueueIssue.model_validate(issue).code in queue_blocking_codes for issue in queue_issues)
    blocked = bool(schema_drift) or queue_blocked
    queue_counts = _QueueCounts.model_validate(counts)
    degraded = (bool(queue_issues) and not queue_blocked) or queue_counts.stale > 0
    blocked_reason = ""
    if schema_drift:
        blocked_reason = (
            "vocabulary_schema_drift_unsupported"
            if unsupported_schema_drift
            else "vocabulary_schema_drift"
        )
    elif queue_blocked:
        blocked_reason = "vocabulary_queue_inconsistent"
    return _VocabularyStatusPayload(
        schema=VOCABULARY_STATUS_SCHEMA,
        status="blocked" if blocked else "degraded" if degraded else "ready",
        schema_status="blocked" if blocked else "ready",
        blocked_reason=blocked_reason,
        db_path=str(db_path),
        db_exists=True,
        queue_counts=queue_counts,
        object_counts=_json_object(object_counts),
        schema_drift=[_SchemaDriftIssue.model_validate(issue) for issue in schema_drift],
        queue_issues=[_QueueIssue.model_validate(issue) for issue in queue_issues],
        queue_issue_count=len(queue_issues),
        mutated=False,
        recovery_command=recovery if blocked or degraded else "",
        next_action=recovery if blocked or degraded else "",
    ).to_payload()


def diagnose_vocabulary_status(db_path: Path) -> JsonObject:
    typed = _status_payload(vocabulary_status(db_path))
    payload = typed.to_payload()
    if typed.status == "ready" and typed.queue_counts.pending > 0:
        payload.update(
            {
                "status": "degraded",
                "degraded_reasons": ["pending_semantic_ingestion"],
                "recovery_command": "",
                "next_action": "",
            }
        )
    elif typed.status == "degraded" and typed.queue_counts.pending > 0:
        payload.setdefault("degraded_reasons", ["pending_semantic_ingestion"])
    else:
        payload.setdefault("degraded_reasons", [])
    payload.update({"mutated": False})
    return _status_payload(payload).to_payload()


def _with_plan_id(plan: JsonObject) -> JsonObject:
    data = {key: value for key, value in plan.items() if key != "plan_id"}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return _json_object({"plan_id": f"sha256:{digest}", **data})


def _first_heading_or_stem(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or path.stem
    except OSError:
        return path.stem
    return path.stem


def _catalog_hints(catalog_path: Path | None) -> dict[str, _CatalogHint]:
    if not catalog_path or not catalog_path.is_file():
        return {}
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items: object = payload
    if isinstance(payload, dict):
        catalog = _json_object(payload)
        items = catalog["notes"] if "notes" in catalog else []
    hints: dict[str, _CatalogHint] = {}
    if isinstance(items, list):
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = _json_object(raw_item)
            path_value = item["path"] if "path" in item else item["file"] if "file" in item else ""
            raw_path = str(path_value or "")
            if not raw_path:
                continue
            aliases = item["aliases"] if "aliases" in item else []
            hints[Path(raw_path).name] = _CatalogHint(
                available=True,
                title=str(item["title"] if "title" in item else ""),
                alias_count=len(aliases if isinstance(aliases, list) else []),
            )
    return hints


def _schema_drift_is_schema_bootstrapable(schema_drift: Sequence[_SchemaDriftIssue]) -> bool:
    return bool(schema_drift) and all(issue.code == "missing_table" for issue in schema_drift)


def build_vocabulary_recovery_plan(
    db_path: Path | None = None,
    *,
    mode: str,
    wiki_dir: Path | None = None,
    catalog_path: Path | None = None,
) -> JsonObject:
    if db_path is None:
        raise TypeError("build_vocabulary_recovery_plan() missing required db_path")
    db_path = Path(db_path)
    actions: list[JsonObject] = []
    blocked_items: list[JsonObject] = []
    backup_path = str(db_path.with_name(f"{db_path.name}.{time.time_ns()}.bak")) if db_path.exists() else ""
    if mode == "reconcile-queue":
        if not db_path.exists():
            next_action = wiki_cli_command("vocabulary-recover", "--mode", "rebuild-db", "--dry-run", "--json")
            return _with_plan_id(
                _json_object(
                    {
                        "schema": VOCABULARY_RECOVERY_PLAN_SCHEMA,
                        "status": "blocked",
                        "blocked_reason": "vocabulary_db_missing",
                        "mode": mode,
                        "db_path": str(db_path),
                        "backup_path": backup_path,
                        "actions": [],
                        "blocked_items": [
                            {
                                "code": "db_missing",
                                "message": "vocabulary DB does not exist; rebuild-db is required before queue reconciliation.",
                            }
                        ],
                        "created_at": _now_iso(),
                        "next_action": next_action,
                        "required_inputs": ["wiki_dir"],
                        "human_decision_required": False,
                    }
                )
            )
        diagnosis = diagnose_vocabulary_status(db_path)
        diagnosis_model = _status_payload(diagnosis)
        schema_drift = diagnosis_model.schema_drift
        if schema_drift:
            if _schema_drift_is_schema_bootstrapable(schema_drift):
                actions.append(
                    _action_payload(
                        _RecoveryActionFields(
                            action="create_missing_schema",
                            code="create_missing_schema",
                            db_path=str(db_path),
                            reason="official_recovery_create_missing_vocabulary_schema",
                        )
                    )
                )
            else:
                blocked_items.append(
                    _SchemaDriftIssue(
                        code=diagnosis_model.blocked_reason or "vocabulary_schema_drift_unsupported",
                        message="Existing vocabulary DB schema drift is not safe to reconcile in place.",
                    ).to_payload()
                )
                return _with_plan_id(
                    _json_object({
                        "schema": VOCABULARY_RECOVERY_PLAN_SCHEMA,
                        "status": "blocked",
                        "mode": mode,
                        "db_path": str(db_path),
                        "backup_path": backup_path,
                        "actions": [],
                        "blocked_items": blocked_items,
                        "created_at": _now_iso(),
                        "next_action": "Não altere SQLite manualmente; rode rebuild-db com plano/recibo ou restaure backup válido.",
                    })
                )
            return _with_plan_id(
                _json_object({
                    "schema": VOCABULARY_RECOVERY_PLAN_SCHEMA,
                    "status": "planned",
                    "mode": mode,
                    "db_path": str(db_path),
                    "backup_path": backup_path,
                    "actions": actions,
                    "blocked_items": blocked_items,
                    "created_at": _now_iso(),
                })
            )
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = list(
                    conn.execute(
                        """
                        SELECT
                          q.id,
                          q.note_id,
                          q.note_path,
                          q.content_hash,
                          q.status,
                          n.id AS resolved_note_id
                        FROM note_semantic_ingestion_queue AS q
                        LEFT JOIN notes AS n ON n.id = q.note_id
                        WHERE q.status IN ('pending', 'claimed', 'stale')
                        ORDER BY q.id ASC
                        """
                    )
                )
            except sqlite3.DatabaseError as exc:
                blocked_items.append(_SchemaDriftIssue(code="schema_drift", message=str(exc)).to_payload())
                rows = []
            for row in rows:
                note_path = Path(str(row["note_path"]))
                content_hash = str(row["content_hash"])
                if row["resolved_note_id"] is None:
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="mark_orphan_queue_blocked",
                                code="mark_orphan_queue_blocked",
                                queue_id=int(row["id"]),
                                note_id=int(row["note_id"] or 0),
                                note_path=str(note_path),
                                content_hash=content_hash,
                                from_status=str(row["status"]),
                                to_status="blocked",
                                reason="orphan_queue_note_id",
                            )
                        )
                    )
                    continue
                if str(row["status"]) == "claimed":
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="reset_claimed_to_pending",
                                code="reset_claimed_to_pending",
                                queue_id=int(row["id"]),
                                note_path=str(note_path),
                                content_hash=content_hash,
                                from_status="claimed",
                                to_status="pending",
                                reason="claimed_without_active_agent",
                            )
                        )
                    )
                    continue
                actual_case_path = _case_only_match(note_path)
                if actual_case_path is not None:
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="fix_case_path",
                                queue_id=int(row["id"]),
                                note_path=str(note_path),
                                actual_path=str(actual_case_path),
                                content_hash=content_hash,
                                from_status=str(row["status"]),
                                to_status=str(row["status"]),
                                reason="note_path_case_mismatch",
                            )
                        )
                    )
                elif not note_path.exists():
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="mark_blocked",
                                queue_id=int(row["id"]),
                                note_path=str(note_path),
                                content_hash=content_hash,
                                from_status=str(row["status"]),
                                to_status="blocked",
                                reason="note_path_missing",
                            )
                        )
                    )
                elif str(row["status"]) == "stale":
                    actual_hash = note_content_hash(note_path)
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="refresh_stale_to_pending",
                                code="refresh_stale_to_pending",
                                queue_id=int(row["id"]),
                                note_path=str(note_path),
                                content_hash=content_hash,
                                actual_hash=actual_hash,
                                from_status="stale",
                                to_status="pending",
                                reason="stale_queue_ready_for_recuration",
                            )
                        )
                    )
                elif note_content_hash(note_path) != content_hash:
                    actions.append(
                        _action_payload(
                            _RecoveryActionFields(
                                action="mark_stale",
                                queue_id=int(row["id"]),
                                note_path=str(note_path),
                                content_hash=content_hash,
                                from_status=str(row["status"]),
                                to_status="stale",
                                reason="content_hash_mismatch",
                            )
                        )
                    )
    elif mode in {"rebuild-db", "catalog-assisted"}:
        if wiki_dir and Path(wiki_dir).is_dir():
            actions.append(
                _action_payload(
                    _RecoveryActionFields(action="reset_db", db_path=str(db_path), reason="official_recovery_rebuild")
                )
            )
            hints = _catalog_hints(catalog_path) if mode == "catalog-assisted" else {}
            for note in iter_notes(Path(wiki_dir)):
                note_hash = note_content_hash(note)
                hint = hints.get(note.name, _CatalogHint(available=False))
                actions.append(
                    _action_payload(
                        _RecoveryActionFields(
                            action="enqueue_semantic_work",
                            note_path=str(note),
                            title=_first_heading_or_stem(note),
                            content_hash=note_hash,
                            queue_flags=["needs_semantic_ingestion"],
                            assigned_agent="med-link-graph-curator",
                            to_status="pending",
                            catalog_hint=hint,
                            reason="curator_must_read_note_before_semantic_quality_claim",
                        )
                    )
                )
        else:
            blocked_items.append(_SchemaDriftIssue(code="wiki_dir_missing", message="wiki_dir required to enqueue semantic work").to_payload())
    else:
        raise ValidationError(f"unsupported vocabulary recovery mode: {mode}")

    return _with_plan_id(
        _json_object({
            "schema": VOCABULARY_RECOVERY_PLAN_SCHEMA,
            "status": "blocked" if blocked_items else "planned" if actions else "skipped",
            "mode": mode,
            "db_path": str(db_path),
            "backup_path": backup_path,
            "actions": actions,
            "blocked_items": blocked_items,
            "created_at": _now_iso(),
        })
    )


def _write_recovery_receipt(path: Path | None, receipt: JsonObject) -> JsonObject:
    typed_receipt = _receipt_payload(receipt)
    if path is not None:
        atomic_write_text(path, json.dumps(typed_receipt, ensure_ascii=False, indent=2) + "\n")
    return typed_receipt


def _receipt_blocked_unwritable(
    *,
    plan: _RecoveryPlanPayload,
    db_path: Path,
    receipt_path: Path,
    error: BaseException,
) -> JsonObject:
    return _receipt_payload({
        "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
        "status": "blocked",
        "blocked_reason": "vocabulary_recovery_receipt_unwritable",
        "plan_id": plan.plan_id,
        "db_path": str(db_path),
        "backup_path": plan.backup_path,
        "applied_actions": [],
        "skipped_items": [],
        "error_items": [
            {
                "code": "receipt_unwritable",
                "receipt_path": str(receipt_path),
                "message": str(error),
            }
        ],
        "next_action": "Escolher um caminho gravável para --receipt e repetir o apply antes de mutar SQLite.",
        "required_inputs": ["receipt_path"],
    })


def _reserve_recovery_receipt(*, plan: _RecoveryPlanPayload, db_path: Path, receipt_path: Path) -> JsonObject | None:
    pending_receipt = _receipt_payload({
        "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
        "status": "pending",
        "plan_id": plan.plan_id,
        "db_path": str(db_path),
        "backup_path": plan.backup_path,
        "applied_actions": [],
        "skipped_items": [],
        "error_items": [],
        "next_action": "Recovery em andamento; este recibo será finalizado após a aplicação do plano.",
        "created_at": _now_iso(),
    })
    try:
        _write_recovery_receipt(receipt_path, pending_receipt)
    except (FileWriteError, OSError) as exc:
        return _receipt_blocked_unwritable(plan=plan, db_path=db_path, receipt_path=receipt_path, error=exc)
    return None


def _plan_stale_receipt(
    *,
    plan: _RecoveryPlanPayload,
    db_path: Path,
    backup_path: str,
    skipped: Sequence[JsonObject],
) -> JsonObject:
    return _receipt_payload({
        "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
        "status": "blocked",
        "blocked_reason": "vocabulary_recovery_plan_stale",
        "plan_id": plan.plan_id,
        "db_path": str(db_path),
        "backup_path": backup_path,
        "applied_actions": [],
        "skipped_items": skipped,
        "error_items": skipped,
        "next_action": "Gerar novo vocabulary-recover --dry-run e revisar skipped_items antes de repetir apply.",
    })


def _receipt_finalization_failed_receipt(
    *,
    plan: _RecoveryPlanPayload,
    db_path: Path,
    backup_path: str,
    error: BaseException,
) -> JsonObject:
    return _receipt_payload({
        "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
        "status": "blocked",
        "blocked_reason": "vocabulary_recovery_receipt_finalization_failed",
        "plan_id": plan.plan_id,
        "db_path": str(db_path),
        "backup_path": backup_path,
        "applied_actions": [],
        "skipped_items": [],
        "error_items": [{"code": "receipt_finalization_failed", "message": str(error)}],
        "next_action": "Rollback do SQLite executado; corrigir o caminho do receipt e repetir o apply.",
    })


def _restore_db_backup(*, db_path: Path, backup_path: str, db_existed: bool) -> None:
    if db_existed and backup_path and Path(backup_path).is_file():
        shutil.copy2(Path(backup_path), db_path)
    elif db_path.exists():
        db_path.unlink()


def _queue_row_exists(conn: sqlite3.Connection, *, queue_id: int, content_hash: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM note_semantic_ingestion_queue
            WHERE id=? AND content_hash=? AND status IN ('pending', 'claimed')
            """,
            (queue_id, content_hash),
        ).fetchone()
    )


def _queue_row_exists_with_status(
    conn: sqlite3.Connection,
    *,
    queue_id: int,
    content_hash: str,
    statuses: tuple[str, ...],
) -> bool:
    placeholders = ",".join("?" for _ in statuses)
    return bool(
        conn.execute(
            f"""
            SELECT 1
            FROM note_semantic_ingestion_queue
            WHERE id=? AND content_hash=? AND status IN ({placeholders})
            """,
            (queue_id, content_hash, *statuses),
        ).fetchone()
    )


def _validate_recovery_actions(conn: sqlite3.Connection | None, actions: Sequence[JsonObject]) -> list[JsonObject]:
    skipped: list[JsonObject] = []
    for raw in actions:
        fields = _action_fields(raw)
        if fields is None:
            skipped.append(_json_object({"action": "", "skipped_reason": "invalid_action"}))
            continue
        action = fields.action
        if action in {"reset_db", "create_missing_schema"}:
            continue
        if action in {"reset_claimed_to_pending", "mark_orphan_queue_blocked"}:
            if conn is not None:
                from_status = fields.from_status
                statuses = ("pending", "claimed", "stale") if from_status == "stale" else ("pending", "claimed")
                if not _queue_row_exists_with_status(
                    conn,
                    queue_id=fields.queue_id,
                    content_hash=fields.content_hash,
                    statuses=statuses,
                ):
                    skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action == "fix_case_path":
            actual_path = Path(fields.actual_path)
            if not actual_path.is_file():
                skipped.append(_action_with(raw, "skipped_reason", "actual_path_missing"))
                continue
            content_hash = fields.content_hash
            if note_content_hash(actual_path) != content_hash:
                skipped.append(_action_with(raw, "skipped_reason", "content_hash_mismatch"))
                continue
            if conn is not None:
                from_status = fields.from_status
                statuses = ("pending", "claimed", "stale") if from_status == "stale" else ("pending", "claimed")
                if not _queue_row_exists_with_status(
                    conn,
                    queue_id=fields.queue_id,
                    content_hash=content_hash,
                    statuses=statuses,
                ):
                    skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action == "enqueue_semantic_work":
            note_path = Path(fields.note_path)
            if not note_path.is_file():
                skipped.append(_action_with(raw, "skipped_reason", "note_path_missing"))
                continue
            content_hash = fields.content_hash
            if note_content_hash(note_path) != content_hash:
                skipped.append(_action_with(raw, "skipped_reason", "content_hash_mismatch"))
            continue
        if action == "refresh_stale_to_pending":
            note_path = Path(fields.note_path)
            if not note_path.is_file():
                skipped.append(_action_with(raw, "skipped_reason", "note_path_missing"))
                continue
            actual_hash = fields.actual_hash
            if note_content_hash(note_path) != actual_hash:
                skipped.append(_action_with(raw, "skipped_reason", "content_hash_mismatch"))
                continue
            queue_id = fields.queue_id
            if conn is not None:
                if not _queue_row_exists_with_status(
                    conn,
                    queue_id=queue_id,
                    content_hash=fields.content_hash,
                    statuses=("stale",),
                ):
                    skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
                    continue
                duplicate = conn.execute(
                    """
                    SELECT 1
                    FROM note_semantic_ingestion_queue
                    WHERE note_path=? AND content_hash=? AND id<>?
                    """,
                    (str(note_path), actual_hash, queue_id),
                ).fetchone()
                if duplicate:
                    skipped.append(_action_with(raw, "skipped_reason", "target_queue_row_exists"))
            continue
        if action not in {"mark_stale", "mark_blocked"}:
            skipped.append(_action_with(raw, "skipped_reason", "not_db_state_transition"))
            continue
        if conn is not None:
            from_status = fields.from_status
            statuses = ("pending", "claimed", "stale") if from_status == "stale" else ("pending", "claimed")
            if not _queue_row_exists_with_status(
                conn,
                queue_id=fields.queue_id,
                content_hash=fields.content_hash,
                statuses=statuses,
            ):
                skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
    return skipped


def _apply_recovery_actions(conn: sqlite3.Connection, actions: Sequence[JsonObject]) -> tuple[list[JsonObject], list[JsonObject]]:
    applied: list[JsonObject] = []
    skipped: list[JsonObject] = []
    for raw in actions:
        fields = _action_fields(raw)
        if fields is None:
            skipped.append(_json_object({"action": "", "skipped_reason": "invalid_action"}))
            continue
        action = fields.action
        if action in {"reset_db", "create_missing_schema"}:
            applied.append(raw)
            continue
        if action == "reset_claimed_to_pending":
            cursor = conn.execute(
                """
                UPDATE note_semantic_ingestion_queue
                SET status='pending', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND content_hash=? AND status='claimed'
                """,
                (fields.queue_id, fields.content_hash),
            )
            if cursor.rowcount:
                applied.append(raw)
            else:
                skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action == "mark_orphan_queue_blocked":
            from_status = fields.from_status
            allowed_statuses = "'pending', 'claimed', 'stale'" if from_status == "stale" else "'pending', 'claimed'"
            cursor = conn.execute(
                f"""
                UPDATE note_semantic_ingestion_queue
                SET status='blocked', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND content_hash=? AND status IN ({allowed_statuses})
                """,
                (fields.queue_id, fields.content_hash),
            )
            if cursor.rowcount:
                applied.append(raw)
            else:
                skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action == "fix_case_path":
            content_hash = fields.content_hash
            actual_path = Path(fields.actual_path)
            from_status = fields.from_status
            allowed_statuses = "'pending', 'claimed', 'stale'" if from_status == "stale" else "'pending', 'claimed'"
            cursor = conn.execute(
                f"""
                UPDATE note_semantic_ingestion_queue
                SET note_path=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND content_hash=? AND status IN ({allowed_statuses})
                """,
                (str(actual_path), fields.queue_id, content_hash),
            )
            if cursor.rowcount:
                applied.append(raw)
            else:
                skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action == "enqueue_semantic_work":
            note_path = Path(fields.note_path)
            content_hash = fields.content_hash
            note_id = upsert_note(
                conn,
                path=note_path,
                title=fields.title or note_path.stem,
                content_hash=content_hash,
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO note_semantic_ingestion_queue(
                  note_id, note_path, content_hash, queue_flags_json, assigned_agent, status
                )
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    note_id,
                    str(note_path),
                    content_hash,
                    json.dumps(fields.queue_flags or ["needs_semantic_ingestion"], ensure_ascii=False),
                    fields.assigned_agent or "med-link-graph-curator",
                ),
            )
            applied.append(raw)
            continue
        if action == "refresh_stale_to_pending":
            queue_id = fields.queue_id
            actual_hash = fields.actual_hash
            note_path = Path(fields.note_path)
            conn.execute(
                """
                UPDATE notes
                SET content_hash=?, updated_at=CURRENT_TIMESTAMP
                WHERE id = (
                  SELECT note_id
                  FROM note_semantic_ingestion_queue
                  WHERE id=?
                )
                """,
                (actual_hash, queue_id),
            )
            cursor = conn.execute(
                """
                UPDATE note_semantic_ingestion_queue
                SET note_path=?, content_hash=?, status='pending', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND content_hash=? AND status='stale'
                """,
                (str(note_path), actual_hash, queue_id, fields.content_hash),
            )
            if cursor.rowcount:
                applied.append(raw)
            else:
                skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
            continue
        if action not in {"mark_stale", "mark_blocked"}:
            skipped.append(_action_with(raw, "skipped_reason", "not_db_state_transition"))
            continue
        status = "stale" if action == "mark_stale" else "blocked"
        from_status = fields.from_status
        allowed_statuses = "'pending', 'claimed', 'stale'" if from_status == "stale" else "'pending', 'claimed'"
        cursor = conn.execute(
            f"""
            UPDATE note_semantic_ingestion_queue
            SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND content_hash=? AND status IN ({allowed_statuses})
            """,
            (status, fields.queue_id, fields.content_hash),
        )
        if cursor.rowcount:
            applied.append(raw)
        else:
            skipped.append(_action_with(raw, "skipped_reason", "queue_row_changed"))
    return applied, skipped


def _legacy_direct_apply_allowed(actions: Sequence[JsonObject]) -> bool:
    allowed = {"create_missing_schema", "reset_claimed_to_pending", "mark_orphan_queue_blocked"}
    return all(_action_name(raw) in allowed for raw in actions)


def _completed_receipt_status(actions: Sequence[JsonObject]) -> Literal["completed", "applied"]:
    completed = {"create_missing_schema", "reset_claimed_to_pending", "mark_orphan_queue_blocked"}
    return "completed" if any(_action_name(raw) in completed for raw in actions) else "applied"


def _action_summaries(actions: Sequence[JsonObject]) -> list[JsonObject]:
    summaries: list[JsonObject] = []
    for action in actions:
        fields = _action_fields(action)
        code = fields.code or fields.action if fields is not None else ""
        summaries.append(_json_object({**action, "code": code, "applied_count": 1}))
    return summaries


def apply_vocabulary_recovery_plan(
    *args: object,
    db_path: Path | None = None,
    plan: JsonObject | None = None,
    receipt_path: Path | None = None,
) -> JsonObject:
    legacy_direct_apply = False
    if args:
        if len(args) != 1 or not isinstance(args[0], dict) or plan is not None:
            raise TypeError("apply_vocabulary_recovery_plan() accepts either plan or keyword arguments")
        plan = _json_object(args[0])
        db_path = Path(_plan_payload(plan).db_path)
        legacy_direct_apply = True
    if plan is None or db_path is None:
        raise TypeError("apply_vocabulary_recovery_plan() missing required plan/db_path")
    plan_data = _json_object(plan)
    plan_model = _plan_payload(plan_data)
    expected = _with_plan_id(_json_object({key: value for key, value in plan_data.items() if key != "plan_id"}))
    if expected["plan_id"] != plan_model.plan_id:
        raise ValidationError("vocabulary recovery plan_id mismatch")
    if plan_model.db_path != str(db_path):
        raise ValidationError("vocabulary recovery db_path mismatch")
    actions = plan_model.actions
    blocked_items = plan_model.blocked_items
    if plan_model.status == "blocked" or blocked_items:
        return _write_recovery_receipt(
            receipt_path,
            _receipt_payload({
                "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
                "status": "blocked",
                "blocked_reason": "vocabulary_recovery_plan_blocked",
                "plan_id": plan_model.plan_id,
                "db_path": str(db_path),
                "backup_path": plan_model.backup_path,
                "applied_actions": [],
                "skipped_items": [],
                "error_items": blocked_items,
                "next_action": "Corrigir blockers do plano e gerar novo vocabulary-recover --dry-run antes do apply.",
            }),
        )
    if actions and receipt_path is None and not (
        legacy_direct_apply and _legacy_direct_apply_allowed(actions)
    ):
        return _receipt_payload({
            "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "vocabulary_recovery_receipt_required",
            "plan_id": plan_model.plan_id,
            "db_path": str(db_path),
            "backup_path": plan_model.backup_path,
            "applied_actions": [],
            "skipped_items": [],
            "error_items": [],
            "next_action": "Repetir com receipt_path/--receipt para preservar rollback/auditoria antes de mutar SQLite.",
            "required_inputs": ["receipt_path"],
        })

    db_path = Path(db_path)
    if actions and receipt_path is not None:
        blocked_receipt = _reserve_recovery_receipt(plan=plan_model, db_path=db_path, receipt_path=receipt_path)
        if blocked_receipt is not None:
            return blocked_receipt
    backup_path = plan_model.backup_path
    needs_reset = any(_action_name(raw) == "reset_db" for raw in actions)
    needs_schema_create = any(_action_name(raw) == "create_missing_schema" for raw in actions)
    if not needs_reset:
        if needs_schema_create:
            initialize_vocabulary_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            skipped = _validate_recovery_actions(conn, actions)
            if skipped:
                conn.rollback()
                return _write_recovery_receipt(
                    receipt_path,
                    _plan_stale_receipt(plan=plan_model, db_path=db_path, backup_path=backup_path, skipped=skipped),
                )
            applied, skipped = _apply_recovery_actions(conn, actions)
            if skipped:
                conn.rollback()
                return _write_recovery_receipt(
                    receipt_path,
                    _plan_stale_receipt(plan=plan_model, db_path=db_path, backup_path=backup_path, skipped=skipped),
                )
            final_receipt = _receipt_payload({
                "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
                "status": _completed_receipt_status(actions),
                "mode": plan_model.mode,
                "plan_id": plan_model.plan_id,
                "db_path": str(db_path),
                "backup_path": backup_path,
                "applied_actions": applied,
                "actions": _action_summaries(applied),
                "skipped_items": [],
                "error_items": [],
                "diagnosis_after": {},
                "next_action": "",
            })
            try:
                _write_recovery_receipt(receipt_path, final_receipt)
            except (FileWriteError, OSError) as exc:
                conn.rollback()
                return _receipt_finalization_failed_receipt(
                    plan=plan_model,
                    db_path=db_path,
                    backup_path=backup_path,
                    error=exc,
                )
            conn.commit()
            if receipt_path is None:
                final_receipt["diagnosis_after"] = diagnose_vocabulary_status(db_path)
            return _receipt_payload(final_receipt)

    if backup_path and db_path.exists():
        backup = Path(backup_path)
        if backup.exists():
            return _write_recovery_receipt(
                receipt_path,
                _receipt_payload({
                    "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
                    "status": "blocked",
                    "blocked_reason": "vocabulary_recovery_backup_exists",
                    "plan_id": plan_model.plan_id,
                    "db_path": str(db_path),
                    "backup_path": str(backup),
                    "applied_actions": [],
                    "skipped_items": [],
                    "error_items": [{"code": "backup_exists", "backup_path": str(backup)}],
                    "next_action": "Gerar novo vocabulary-recover --dry-run para obter um novo backup/plan antes de aplicar novamente.",
                }),
            )
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup)
    db_existed_before_reset = bool(backup_path)
    skipped = _validate_recovery_actions(None, actions)
    if skipped:
        return _write_recovery_receipt(
            receipt_path,
            _plan_stale_receipt(plan=plan_model, db_path=db_path, backup_path=backup_path, skipped=skipped),
        )
    if needs_reset and db_path.exists():
        db_path.unlink()
    if needs_reset:
        initialize_vocabulary_db(db_path)

    with sqlite3.connect(db_path) as conn:
        applied, skipped = _apply_recovery_actions(conn, actions)
    if skipped:
        _restore_db_backup(db_path=db_path, backup_path=backup_path, db_existed=db_existed_before_reset)
        return _write_recovery_receipt(
            receipt_path,
            _plan_stale_receipt(plan=plan_model, db_path=db_path, backup_path=backup_path, skipped=skipped),
        )
    final_receipt = _receipt_payload({
        "schema": VOCABULARY_RECOVERY_RECEIPT_SCHEMA,
        "status": _completed_receipt_status(actions),
        "mode": plan_model.mode,
        "plan_id": plan_model.plan_id,
        "db_path": str(db_path),
        "backup_path": backup_path,
        "applied_actions": applied,
        "actions": _action_summaries(applied),
        "skipped_items": [],
        "error_items": [],
        "diagnosis_after": diagnose_vocabulary_status(db_path),
        "next_action": "",
    })
    try:
        return _write_recovery_receipt(receipt_path, final_receipt)
    except (FileWriteError, OSError) as exc:
        _restore_db_backup(db_path=db_path, backup_path=backup_path, db_existed=db_existed_before_reset)
        return _receipt_finalization_failed_receipt(
            plan=plan_model,
            db_path=db_path,
            backup_path=backup_path,
            error=exc,
        )
