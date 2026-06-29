"""Local PDF library search and ranking."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.pdf import cloud, db, paths
from mednotes.kernel.base import JsonObject, JsonObjectAdapter, JsonValue

SCHEMA = "medical-notes-workbench.pdf-library-search-results.v1"


@dataclass(frozen=True)
class SearchHit:
    figure_uid: str
    score: float
    why: list[str]
    evidence_level: str
    is_low_confidence: bool
    provider_receipts: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class SearchRequest:
    query_text: str = ""
    note_path: Path | None = None
    anchor_id: str = ""
    provider: str = "local"
    top_k: int = 20


def search(request: SearchRequest, *, app_home: Path | None = None) -> dict[str, Any]:
    provider = cloud.resolve_provider(request.provider)
    if provider["status"] == "blocked":
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "phase": "search",
            "blocked_reason": provider["blocked_reason"],
            "next_action": provider["next_action"],
            "provider_receipts": [provider],
        }
    query = request.query_text.strip()
    if not query and request.note_path is not None:
        query = _query_from_note_anchor(request, app_home=app_home)
        if not query:
            return _blocked("anchor_not_found", "choose an existing anchor or pass --query")
    if not query:
        return _blocked("missing_query_or_note", "pass --query TEXT or --note NOTE")
    root = app_home or paths.app_home()
    conn = db.open_database(paths.database_path(root))
    hits = _local_search(conn, query, request.top_k)
    payload = {
        "schema": SCHEMA,
        "status": "ok",
        "phase": "search",
        "query_text": query,
        "results": [hit.__dict__ for hit in hits],
        "provider_receipts": [] if request.provider == "local" else [provider],
    }
    _record_receipt(conn, request, payload)
    return payload


def _local_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[SearchHit]:
    scores: dict[str, float] = {}
    why: dict[str, set[str]] = {}
    for source, table, uid_col, reason in (
        ("figure", "figure_fts", "figure_uid", "caption match"),
        ("mention", "mention_fts", "mention_uid", "mention match"),
        ("page", "page_fts", "page_number", "page text match"),
    ):
        try:
            rows = conn.execute(f"SELECT * FROM {table} WHERE {table} MATCH ? LIMIT 100", (query,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            if source == "figure":
                figure_uid = str(row[uid_col])
            elif source == "mention":
                mention = conn.execute("SELECT figure_uid FROM mentions WHERE mention_uid = ?", (row[uid_col],)).fetchone()
                if not mention or not mention["figure_uid"]:
                    continue
                figure_uid = str(mention["figure_uid"])
            else:
                figure = conn.execute(
                    "SELECT figure_uid FROM figures WHERE pdf_sha256 = ? AND page_number = ? LIMIT 1",
                    (row["pdf_sha256"], row["page_number"]),
                ).fetchone()
                if not figure:
                    continue
                figure_uid = str(figure["figure_uid"])
            scores[figure_uid] = scores.get(figure_uid, 0.0) + (7.0 if source == "figure" else 4.0 if source == "mention" else 1.5)
            why.setdefault(figure_uid, set()).add(reason)
    if not scores:
        like = f"%{query}%"
        for row in conn.execute(
            """
            SELECT figure_uid FROM figures
            JOIN documents USING(pdf_sha256)
            WHERE documents.removed_at IS NULL AND (caption LIKE ? OR display_label LIKE ?)
            LIMIT 100
            """,
            (like, like),
        ):
            scores[str(row["figure_uid"])] = 3.0
            why.setdefault(str(row["figure_uid"]), set()).add("caption contains query")
    hits: list[SearchHit] = []
    for figure_uid, score in scores.items():
        row = conn.execute(
            """
            SELECT figures.figure_uid, figures.evidence_level, figures.is_low_confidence, figures.conflict_reason
            FROM figures JOIN documents USING(pdf_sha256)
            WHERE figures.figure_uid = ? AND documents.removed_at IS NULL
            """,
            (figure_uid,),
        ).fetchone()
        if not row:
            continue
        evidence = str(row["evidence_level"])
        if evidence == "caption_and_mentions":
            score += 4
        if evidence == "visual_only":
            score -= 3
        if row["conflict_reason"]:
            score -= 4
        hits.append(
            SearchHit(
                figure_uid=figure_uid,
                score=round(score, 3),
                why=sorted(why.get(figure_uid, set())),
                evidence_level=evidence,
                is_low_confidence=bool(row["is_low_confidence"]),
            )
        )
    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]


def _query_from_note_anchor(request: SearchRequest, *, app_home: Path | None) -> str:
    if request.note_path is None:
        return ""
    conn = db.open_database(paths.database_path(app_home or paths.app_home()))
    note_path = str(request.note_path)
    row = conn.execute(
        "SELECT payload_json FROM anchor_cache WHERE note_path = ? AND anchor_id = ? LIMIT 1",
        (note_path, request.anchor_id),
    ).fetchone()
    if not row:
        return ""
    payload = JsonObjectAdapter.validate_python(json.loads(str(row["payload_json"])))
    queries = payload.get("search_queries")
    if isinstance(queries, list) and queries:
        return str(queries[0])
    return str(payload.get("concept") or "")


def _optional_path_text(path: Path | None) -> str:
    return str(path) if path is not None else ""


def _record_receipt(conn: sqlite3.Connection, request: SearchRequest, payload: JsonObject) -> None:
    with conn:
        conn.execute(
            "INSERT INTO search_receipts(receipt_uid, query_text, note_path, anchor_id, provider, status, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                request.query_text,
                _optional_path_text(request.note_path),
                request.anchor_id,
                request.provider,
                str(_json_field(payload, "status")),
                datetime.now(UTC).isoformat(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def _blocked(reason: str, next_action: str) -> JsonObject:
    return {"schema": SCHEMA, "status": "blocked", "phase": "search", "blocked_reason": reason, "next_action": next_action}


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source.get(key, default)
