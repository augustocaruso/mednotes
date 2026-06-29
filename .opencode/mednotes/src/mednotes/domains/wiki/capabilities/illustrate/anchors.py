"""Shared anchor generation/cache helper."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mednotes.domains.wiki.capabilities.illustrate.core.insert import parse_sections
from mednotes.domains.wiki.flows.enrich.workflow import gemini, parsing, prompts


@dataclass(frozen=True)
class AnchorProviderConfig:
    provider: Literal["gemini_cli"] = "gemini_cli"
    binary: str = "gemini"
    model: str | None = None
    timeout_seconds: int = 120
    skip_trust: bool = True


@dataclass(frozen=True)
class AnchorBuildResult:
    anchors: list[dict[str, object]]
    provider_receipt: dict[str, object]
    cache_key: str
    cache_hit: bool


def cache_key(
    note_body_sha256: str,
    provider_config: AnchorProviderConfig,
    preferred_language: str,
    max_anchors: int,
    prompt_version: str,
) -> str:
    material = {
        "note_body_sha256": note_body_sha256,
        "provider": provider_config.provider,
        "model": provider_config.model or "",
        "preferred_language": preferred_language,
        "max_anchors": max_anchors,
        "prompt_version": prompt_version,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_or_load_anchors(
    note_path: Path,
    *,
    cache_db: Path,
    max_anchors: int,
    preferred_language: str,
    provider_config: AnchorProviderConfig,
    prompt_version: str = "pdf-library-anchors-v1",
) -> AnchorBuildResult:
    note_text = note_path.read_text(encoding="utf-8")
    note_sha = hashlib.sha256(note_text.encode("utf-8")).hexdigest()
    key = cache_key(note_sha, provider_config, preferred_language, max_anchors, prompt_version)
    conn = _connect(cache_db)
    cached = _load(conn, key)
    if cached is not None:
        return AnchorBuildResult(cached, _receipt(provider_config, "anchors", "completed", len(cached)), key, True)
    if provider_config.provider != "gemini_cli":
        return AnchorBuildResult([], _receipt(provider_config, "anchors", "blocked", 0, "provider_not_implemented"), key, False)
    prompt = prompts.build_anchors_prompt(
        note_text,
        parse_sections(note_text),
        max_anchors=max_anchors,
        preferred_language=preferred_language,
    )
    anchors, _raw = gemini.call_gemini_json_with_retry(
        prompt,
        parsing.parse_anchors_json,
        binary=provider_config.binary,
        model=provider_config.model,
        timeout_seconds=provider_config.timeout_seconds,
        skip_trust=provider_config.skip_trust,
        label="pdf-library anchors",
    )
    normalized = [dict(anchor) for anchor in anchors]
    _store(conn, key, note_sha, note_path, normalized, provider_config, preferred_language, max_anchors, prompt_version)
    return AnchorBuildResult(normalized, _receipt(provider_config, "anchors", "completed", len(normalized)), key, False)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anchor_cache (
          cache_key TEXT NOT NULL,
          note_sha256 TEXT NOT NULL,
          anchor_id TEXT NOT NULL,
          note_path TEXT NOT NULL DEFAULT '',
          section_path_json TEXT NOT NULL DEFAULT '[]',
          concept TEXT NOT NULL,
          visual_type TEXT NOT NULL,
          search_queries_json TEXT NOT NULL DEFAULT '[]',
          provider TEXT NOT NULL DEFAULT '',
          model_id TEXT NOT NULL DEFAULT '',
          preferred_language TEXT NOT NULL DEFAULT '',
          max_anchors INTEGER NOT NULL DEFAULT 0,
          prompt_version TEXT NOT NULL DEFAULT 'pdf-library-anchors-v1',
          created_at TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          PRIMARY KEY (cache_key, anchor_id)
        )
        """
    )
    return conn


def _load(conn: sqlite3.Connection, key: str) -> list[dict[str, object]] | None:
    rows = conn.execute("SELECT payload_json FROM anchor_cache WHERE cache_key = ? ORDER BY anchor_id", (key,)).fetchall()
    if not rows:
        return None
    return [json.loads(str(row[0])) for row in rows]


def _store(
    conn: sqlite3.Connection,
    key: str,
    note_sha: str,
    note_path: Path,
    anchors: list[dict[str, object]],
    provider_config: AnchorProviderConfig,
    preferred_language: str,
    max_anchors: int,
    prompt_version: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    with conn:
        for index, anchor in enumerate(anchors):
            anchor_id = str(anchor.get("anchor_id") or f"a{index + 1}")
            conn.execute(
                """
                INSERT OR REPLACE INTO anchor_cache(
                  cache_key, note_sha256, anchor_id, note_path, section_path_json,
                  concept, visual_type, search_queries_json, provider, model_id,
                  preferred_language, max_anchors, prompt_version, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    note_sha,
                    anchor_id,
                    str(note_path),
                    json.dumps(anchor.get("section_path") or [], ensure_ascii=False),
                    str(anchor.get("concept") or ""),
                    str(anchor.get("visual_type") or ""),
                    json.dumps(anchor.get("search_queries") or [], ensure_ascii=False),
                    provider_config.provider,
                    provider_config.model or "",
                    preferred_language,
                    max_anchors,
                    prompt_version,
                    now,
                    json.dumps(anchor, ensure_ascii=False),
                ),
            )


def _receipt(
    provider_config: AnchorProviderConfig,
    purpose: str,
    status: str,
    item_count: int,
    blocked_reason: str = "",
) -> dict[str, object]:
    return {
        "schema": "medical-notes-workbench.pdf-library-provider-receipt.v1",
        "provider": provider_config.provider,
        "model": provider_config.model or "gemini-configured-default",
        "purpose": purpose,
        "status": status,
        "blocked_reason": blocked_reason,
        "quota_limited": False,
        "item_count": item_count,
        "created_at": datetime.now(UTC).isoformat(),
    }
