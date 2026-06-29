"""Headless Related Notes export compatible with the Obsidian plugin contract."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from pydantic import ValidationError

from mednotes.domains.wiki.capabilities.notes.provenance import CHAT_ORIGINAL_LABEL
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.contracts.related_notes import RelatedNotesExportNote, RelatedNotesHashMigrationExport
from mednotes.domains.wiki.contracts.related_notes_headless import (
    GeminiBatchEmbeddingResponse,
    GeminiEmbedding,
    GeminiEmbeddingResponse,
    GeminiErrorResponse,
    RelatedNotesHashMigrationCache,
    RelatedNotesHeadlessSettings,
    RelatedNotesVectorIndex,
    RelatedNotesVectorRecord,
)
from mednotes.domains.wiki.performance import cooperative_cpu_yield
from mednotes.kernel.base import JsonObjectAdapter, JsonValue
from mednotes.platform.paths import user_state_dir

RELATED_NOTES_EXPORT_SCHEMA = "medical-notes-workbench.related-notes-export.v1"
RELATED_NOTES_HASH_MIGRATION_CACHE_SCHEMA = "medical-notes-workbench.related-notes-hash-migration-cache.v1"
PLUGIN_ID = "related-notes-obsidian"
PLUGIN_EXPORT_NAME = "medical-notes-export.json"
PLUGIN_INDEX_NAME = "index.json"
PLUGIN_SETTINGS_NAME = "data.json"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_PROFILE_ID = "clean_v1"
PROFILE_VERSION = 1
MAX_EMBEDDING_CHARS = 12000
DEFAULT_BATCH_SIZE = 32
MIN_EMBEDDING_REQUEST_DELAY_SECONDS = 10.0
DEFAULT_MAX_EMBEDDING_SECONDS = 120.0
TRANSIENT_EMBEDDING_RETRY_LIMIT = 3

EmbeddingClient = Callable[..., list[list[float]]]
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_FRONTMATTER_YAML_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)")
_FRONTMATTER_TOML_RE = re.compile(r"^\+\+\+\r?\n[\s\S]*?\r?\n\+\+\+\s*(?:\r?\n|$)")
_RELATED_HEADING_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
_NEXT_H2_RE = re.compile(r"(?m)^##\s+")
_GENERATED_FOOTER_RE = re.compile(
    rf"\n---\s*\n(?:\[[^\]]*{re.escape(CHAT_ORIGINAL_LABEL)}[^\]]*\]\([^)]+\)|{re.escape(CHAT_ORIGINAL_LABEL)}\b|Gerado|Generated|Exportado|Fonte|Source|Criado|Created)[\s\S]*$",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_OBSIDIAN_IMAGE_RE = re.compile(r"!\[\[[^\]]+\]\]")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_WIKILINK_ALIAS_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


@dataclass
class HeadlessRelatedNotesExportError(Exception):
    blocked_reason: str
    next_action: str
    detail: str = ""
    partial_record_count: int = 0
    fresh_record_count: int = 0
    stale_record_count: int = 0
    record_count: int = 0
    embedded_count: int = 0
    reused_count: int = 0
    total_note_count: int = 0
    remaining_count: int = 0
    next_retry_after_seconds: int = 0

    def __str__(self) -> str:
        return self.detail or self.blocked_reason


class BatchEmbeddingUnavailable(RuntimeError):
    """Raised when the Gemini batch embedding endpoint cannot handle the request."""

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class _MarkdownNote:
    rel_path: str
    abs_path: Path
    title: str
    markdown: str
    raw_hash: str
    representation: str
    representation_hash: str


def generate_headless_related_notes_export(
    wiki_dir: Path,
    *,
    export_path: Path | None = None,
    settings_path: Path | None = None,
    index_path: Path | None = None,
    embedding_client: EmbeddingClient | None = None,
    sleep: SleepFn | None = None,
    now_iso: str | None = None,
    now_ms: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_embedding_seconds: float | None = None,
    monotonic: ClockFn | None = None,
) -> dict[str, Any]:
    """Rebuild the plugin index and Workbench export without opening Obsidian."""
    wiki = wiki_dir.resolve(strict=False)
    plugin_dir = wiki / ".obsidian" / "plugins" / PLUGIN_ID
    settings_file = settings_path or plugin_dir / PLUGIN_SETTINGS_NAME
    export_file = export_path or plugin_dir / PLUGIN_EXPORT_NAME
    index_file = index_path or plugin_dir / PLUGIN_INDEX_NAME
    settings = _load_settings(settings_file)
    settings_model = RelatedNotesHeadlessSettings.model_validate(settings)
    api_key = settings_model.gemini_api_key.strip()
    if not api_key:
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_api_key_missing",
            next_action="Configurar a chave do plugin Related Notes e repetir a recuperação do export.",
            detail="Related Notes plugin data.json exists but geminiApiKey is empty.",
        )

    profile_id = _profile_id(settings_model.default_embedding_profile)
    related_limit = _related_limit(settings_model.related_notes_limit)
    delay_seconds = _delay_seconds(settings_model.embedding_request_delay_ms)
    notes = _load_markdown_notes(wiki, profile_id)
    existing_index = _load_vector_index(index_file)
    records = _current_records(existing_index, profile_id)
    reused_count = 0
    missing: list[_MarkdownNote] = []
    index_dirty = False
    for note in notes:
        record = records.get(note.rel_path)
        if _record_is_current(record, note, profile_id):
            reused_count += 1
            continue
        if _record_is_legacy_clean_v1_current(record, note, profile_id):
            vector = _migration_vector(record, note, profile_id)
            if vector is None:
                missing.append(note)
                continue
            records[note.rel_path] = _record(
                note,
                vector,
                profile_id=profile_id,
                updated_at=now_ms if now_ms is not None else _now_ms(),
            )
            reused_count += 1
            index_dirty = True
            continue
        missing.append(note)

    embedded_count = 0
    using_default_client = embedding_client is None
    client = embedding_client or _default_embedding_client
    sleeper = sleep or time.sleep
    clock = monotonic or time.monotonic
    started_at = clock()
    embedding_time_budget = _max_embedding_seconds(max_embedding_seconds)
    normalized_batch_size = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
    transient_retry_count = 0

    def flush_vector_index() -> None:
        nonlocal existing_index, index_dirty
        timestamp = now_ms if now_ms is not None else _now_ms()
        vector_index = _vector_index(
            existing_index,
            records,
            profile_id=profile_id,
            updated_at=timestamp,
        )
        index_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(index_file, json.dumps(vector_index, ensure_ascii=False, indent=2) + "\n")
        existing_index = vector_index
        index_dirty = False

    def time_budget_exhausted() -> bool:
        return embedding_time_budget > 0 and (clock() - started_at) >= embedding_time_budget

    def raise_time_budget_exhausted() -> None:
        progress = _recovery_progress_counts(
            records=records,
            notes=notes,
            reused_count=reused_count,
            embedded_count=embedded_count,
        )
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_time_budget_exhausted",
            next_action="Retomar a recuperação do Related Notes pela rota oficial; o índice parcial será reaproveitado.",
            detail="Related Notes headless export paused after reaching the execution time budget.",
            partial_record_count=progress["fresh_record_count"],
            fresh_record_count=progress["fresh_record_count"],
            stale_record_count=progress["stale_record_count"],
            record_count=progress["record_count"],
            embedded_count=embedded_count,
            reused_count=reused_count,
            total_note_count=len(notes),
            remaining_count=progress["remaining_count"],
            next_retry_after_seconds=int(math.ceil(delay_seconds)),
        )

    try:
        index = 0
        while index < len(missing):
            if embedded_count and time_budget_exhausted():
                raise_time_budget_exhausted()
            batch = missing[index : index + normalized_batch_size]
            batch_transient_retries = 0
            try:
                while True:
                    try:
                        vectors = client(
                            [note.representation for note in batch],
                            api_key=api_key,
                            model=DEFAULT_EMBEDDING_MODEL,
                        )
                        break
                    except httpx.TransportError:
                        if not using_default_client or batch_transient_retries >= TRANSIENT_EMBEDDING_RETRY_LIMIT:
                            raise
                        batch_transient_retries += 1
                        transient_retry_count += 1
                        sleeper(delay_seconds)
            except BatchEmbeddingUnavailable as exc:
                if using_default_client and normalized_batch_size > 1:
                    if exc.rate_limited and delay_seconds:
                        sleeper(delay_seconds)
                    normalized_batch_size = max(1, len(batch) // 2) if exc.rate_limited else 1
                    continue
                raise
            if len(vectors) != len(batch):
                raise HeadlessRelatedNotesExportError(
                    blocked_reason="related_notes_headless_embedding_failed",
                    next_action="Repetir a recuperação; o provedor de embeddings retornou contagem inconsistente.",
                    detail="Embedding response count did not match request count.",
                )
            timestamp = now_ms if now_ms is not None else _now_ms()
            for note, vector in zip(batch, vectors, strict=True):
                records[note.rel_path] = _record(note, vector, profile_id=profile_id, updated_at=timestamp)
                embedded_count += 1
                index_dirty = True
            if index_dirty and embedded_count % 10 == 0:
                flush_vector_index()
            if embedded_count < len(missing) and time_budget_exhausted():
                raise_time_budget_exhausted()
            if delay_seconds and embedded_count < len(missing):
                sleeper(delay_seconds)
            index += len(batch)
    except HeadlessRelatedNotesExportError as exc:
        if index_dirty:
            flush_vector_index()
        progress = _recovery_progress_counts(
            records=records,
            notes=notes,
            reused_count=reused_count,
            embedded_count=embedded_count,
        )
        exc.record_count = exc.record_count or progress["record_count"]
        exc.fresh_record_count = exc.fresh_record_count or progress["fresh_record_count"]
        exc.stale_record_count = exc.stale_record_count or progress["stale_record_count"]
        exc.partial_record_count = exc.partial_record_count or progress["fresh_record_count"]
        exc.embedded_count = exc.embedded_count or embedded_count
        exc.reused_count = exc.reused_count or reused_count
        exc.total_note_count = exc.total_note_count or len(notes)
        exc.remaining_count = exc.remaining_count or progress["remaining_count"]
        exc.next_retry_after_seconds = exc.next_retry_after_seconds or int(math.ceil(delay_seconds))
        raise
    except Exception as exc:
        if index_dirty:
            flush_vector_index()
        progress = _recovery_progress_counts(
            records=records,
            notes=notes,
            reused_count=reused_count,
            embedded_count=embedded_count,
        )
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_embedding_failed",
            next_action="Verificar chave/quota/rede do Gemini embeddings e repetir a recuperação do export.",
            detail=_redact_error(str(exc)),
            partial_record_count=progress["fresh_record_count"],
            fresh_record_count=progress["fresh_record_count"],
            stale_record_count=progress["stale_record_count"],
            record_count=progress["record_count"],
            embedded_count=embedded_count,
            reused_count=reused_count,
            total_note_count=len(notes),
            remaining_count=progress["remaining_count"],
            next_retry_after_seconds=int(math.ceil(delay_seconds)),
        ) from exc

    timestamp = now_ms if now_ms is not None else _now_ms()
    vector_index = _vector_index(existing_index, records, profile_id=profile_id, updated_at=timestamp)
    payload = _export_payload(
        notes,
        records,
        wiki,
        profile_id=profile_id,
        related_limit=related_limit,
        generated_at=now_iso or _now_iso(),
    )
    index_file.parent.mkdir(parents=True, exist_ok=True)
    export_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(index_file, json.dumps(vector_index, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(export_file, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "schema": "medical-notes-workbench.related-notes-headless-export.v1",
        "status": "completed",
        "phase": "related_notes_headless_export",
        "export_path": str(export_file),
        "index_path": str(index_file),
        "wiki_dir": str(wiki),
        "note_count": len(payload["notes"]),
        "edge_count": len(payload["edges"]),
        "record_count": len(records),
        "fresh_record_count": len(notes),
        "stale_record_count": max(0, len(records) - len(notes)),
        "remaining_count": 0,
        "embedded_count": embedded_count,
        "reused_count": reused_count,
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_profile_id": profile_id,
        "embedding_request_delay_ms": int(delay_seconds * 1000),
        "embedding_transient_retry_count": transient_retry_count,
        "related_notes_limit": related_limit,
    }


def _related_notes_hash_migration_cache_path() -> Path:
    return user_state_dir() / "related-notes-hash-migration-cache.json"


def _related_notes_hash_migration_file_identity(path: Path) -> dict[str, object] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path.resolve(strict=False)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _related_notes_hash_migration_cache_identity(
    *,
    export_file: Path,
    index_file: Path,
    wiki_dir: Path,
) -> dict[str, object] | None:
    export_identity = _related_notes_hash_migration_file_identity(export_file)
    index_identity = _related_notes_hash_migration_file_identity(index_file)
    if export_identity is None or index_identity is None:
        return None
    return {
        "wiki_dir": str(wiki_dir.resolve(strict=False)),
        "export": export_identity,
        "index": index_identity,
        "profile_id": "clean_v1",
        "profile_version": PROFILE_VERSION,
        "migration": "clean_v1_table_hashes",
    }


def _related_notes_hash_migration_cache_hit(
    *,
    export_file: Path,
    index_file: Path,
    wiki_dir: Path,
) -> dict[str, object] | None:
    identity = _related_notes_hash_migration_cache_identity(
        export_file=export_file,
        index_file=index_file,
        wiki_dir=wiki_dir,
    )
    if identity is None:
        return None
    try:
        payload = json.loads(_related_notes_hash_migration_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        cache = RelatedNotesHashMigrationCache.model_validate(payload)
    except ValidationError:
        return None
    if cache.identity != identity:
        return None
    return {
        "status": "skipped",
        "skipped_reason": "cached_clean_v1_hash_migration",
        "cache_status": "hit",
        "cache_path": str(_related_notes_hash_migration_cache_path()),
        "cached_status": cache.status,
        "migrated_note_count": cache.migrated_note_count,
        "skipped_note_count": cache.skipped_note_count,
    }


def _write_related_notes_hash_migration_cache(
    *,
    export_file: Path,
    index_file: Path,
    wiki_dir: Path,
    status: str,
    migrated_note_count: int,
    skipped_note_count: int,
) -> None:
    identity = _related_notes_hash_migration_cache_identity(
        export_file=export_file,
        index_file=index_file,
        wiki_dir=wiki_dir,
    )
    if identity is None:
        return
    payload = {
        "schema": RELATED_NOTES_HASH_MIGRATION_CACHE_SCHEMA,
        "identity": identity,
        "status": status,
        "migrated_note_count": migrated_note_count,
        "skipped_note_count": skipped_note_count,
    }
    try:
        cache_path = _related_notes_hash_migration_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        return


def migrate_related_notes_clean_v1_table_hashes(
    wiki_dir: Path,
    *,
    export_path: Path | None = None,
    index_path: Path | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Migrate legacy clean_v1 hashes when only table padding normalization changed."""
    wiki = wiki_dir.resolve(strict=False)
    plugin_dir = wiki / ".obsidian" / "plugins" / PLUGIN_ID
    export_file = export_path or plugin_dir / PLUGIN_EXPORT_NAME
    index_file = index_path or plugin_dir / PLUGIN_INDEX_NAME
    base = {
        "schema": "medical-notes-workbench.related-notes-hash-migration.v1",
        "phase": "related_notes_hash_migration",
        "export_path": str(export_file),
        "index_path": str(index_file),
        "wiki_dir": str(wiki),
        "embedding_api_calls": 0,
        "migrated_note_count": 0,
    }
    if not export_file.is_file():
        return {**base, "status": "skipped", "skipped_reason": "export_missing"}
    if not index_file.is_file():
        return {**base, "status": "skipped", "skipped_reason": "index_missing"}
    cached = _related_notes_hash_migration_cache_hit(export_file=export_file, index_file=index_file, wiki_dir=wiki)
    if cached is not None:
        return {**base, **cached}
    try:
        export_payload = json.loads(export_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "status": "skipped", "skipped_reason": "export_unreadable", "detail": _redact_error(str(exc))}
    try:
        export = RelatedNotesHashMigrationExport.model_validate(export_payload)
    except ValidationError:
        return {**base, "status": "skipped", "skipped_reason": "export_schema_unsupported"}
    profile_id = _profile_id(export.model_info.embedding_profile_id)
    if profile_id != "clean_v1":
        return {**base, "status": "skipped", "skipped_reason": "embedding_profile_not_clean_v1"}
    current_notes = {note.rel_path: note for note in _load_markdown_notes(wiki, profile_id)}
    # The migration lens is intentionally minimal, but the on-disk export must
    # keep the full plugin contract. Preserve the original JSON and update only
    # note hashes validated through RelatedNotesExportNote.
    export_payload_for_write = JsonObjectAdapter.validate_python(export_payload)
    export_note_indexes: dict[str, int] = {}
    raw_notes_value = export_payload_for_write["notes"] if "notes" in export_payload_for_write else []
    raw_notes_for_write: list[JsonValue] = raw_notes_value if isinstance(raw_notes_value, list) else []
    for index, raw_note in enumerate(raw_notes_for_write):
        if not isinstance(raw_note, dict):
            continue
        typed_note = RelatedNotesExportNote.model_validate(raw_note)
        export_note_indexes[typed_note.path] = index
    migration_candidates: list[tuple[RelatedNotesExportNote, _MarkdownNote, str]] = []
    migrated_count = 0
    skipped_count = 0
    for item in export.notes:
        rel_path = item.path
        note = current_notes[rel_path] if rel_path in current_notes else None
        if note is None:
            skipped_count += 1
            continue
        exported_hash = item.content_hash
        current_hash = "sha256:" + note.representation_hash
        if exported_hash.lower() == current_hash.lower():
            continue
        legacy_hash = related_notes_legacy_clean_v1_content_hash(
            path=note.rel_path,
            title=note.title,
            markdown=note.markdown,
        )
        if exported_hash.lower() != legacy_hash.lower():
            skipped_count += 1
            continue
        migration_candidates.append((item, note, current_hash))
    if not migration_candidates:
        _write_related_notes_hash_migration_cache(
            export_file=export_file,
            index_file=index_file,
            wiki_dir=wiki,
            status="no_legacy_clean_v1_hashes",
            migrated_note_count=0,
            skipped_note_count=skipped_count,
        )
        return {**base, "status": "skipped", "skipped_reason": "no_legacy_clean_v1_hashes", "skipped_note_count": skipped_count}
    existing_index = _load_vector_index(index_file)
    records = _current_records(existing_index, profile_id)
    updated_paths: list[str] = []
    timestamp = now_ms if now_ms is not None else _now_ms()
    for _item, note, current_hash in migration_candidates:
        record = records.get(note.rel_path)
        vector = _migration_vector(record, note, profile_id)
        if vector is None:
            skipped_count += 1
            continue
        records[note.rel_path] = _record(note, vector, profile_id=profile_id, updated_at=timestamp)
        export_note_index = export_note_indexes.get(note.rel_path)
        if export_note_index is None:
            skipped_count += 1
            continue
        raw_note = raw_notes_for_write[export_note_index]
        note_payload = JsonObjectAdapter.validate_python(raw_note if isinstance(raw_note, dict) else {})
        note_payload["content_hash"] = current_hash
        raw_notes_for_write[export_note_index] = note_payload
        migrated_count += 1
        updated_paths.append(note.rel_path)
    if not migrated_count:
        _write_related_notes_hash_migration_cache(
            export_file=export_file,
            index_file=index_file,
            wiki_dir=wiki,
            status="legacy_clean_v1_vectors_missing",
            migrated_note_count=0,
            skipped_note_count=skipped_count,
        )
        return {
            **base,
            "status": "skipped",
            "skipped_reason": "legacy_clean_v1_vectors_missing",
            "skipped_note_count": skipped_count,
        }
    try:
        vector_index = _vector_index(existing_index, records, profile_id=profile_id, updated_at=timestamp)
        atomic_write_text(index_file, json.dumps(vector_index, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(export_file, json.dumps(export_payload_for_write, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        return {
            **base,
            "status": "blocked",
            "blocked_reason": "related_notes_hash_migration_write_failed",
            "next_action": "Verificar permissões do export/índice do Related Notes antes de aplicar correções na Wiki.",
            "detail": _redact_error(str(exc)),
            "migrated_note_count": migrated_count,
        }
    _write_related_notes_hash_migration_cache(
        export_file=export_file,
        index_file=index_file,
        wiki_dir=wiki,
        status="migrated_clean_v1_hashes",
        migrated_note_count=migrated_count,
        skipped_note_count=skipped_count,
    )
    return {
        **base,
        "status": "completed",
        "migrated_note_count": migrated_count,
        "skipped_note_count": skipped_count,
        "updated_paths": updated_paths[:25],
    }


def headless_plugin_settings_available(wiki_dir: Path) -> bool:
    return (wiki_dir / ".obsidian" / "plugins" / PLUGIN_ID / PLUGIN_SETTINGS_NAME).is_file()


def normalize_related_notes_profile_id(value: Any) -> str:
    return _profile_id(value)


def related_notes_representation_hash(
    *,
    path: str,
    title: str,
    markdown: str,
    profile_id: str = DEFAULT_PROFILE_ID,
) -> str:
    representation = _build_representation(
        path=path,
        title=title,
        markdown=markdown,
        profile_id=normalize_related_notes_profile_id(profile_id),
    )
    return _sha256_text(representation)


def related_notes_content_hash(
    *,
    path: str,
    title: str,
    markdown: str,
    profile_id: str = DEFAULT_PROFILE_ID,
) -> str:
    return "sha256:" + related_notes_representation_hash(
        path=path,
        title=title,
        markdown=markdown,
        profile_id=profile_id,
    )


def _load_settings(settings_path: Path) -> dict[str, Any]:
    if not settings_path.is_file():
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_plugin_settings_missing",
            next_action="Instalar/configurar o plugin Related Notes neste vault e repetir a recuperação do export.",
            detail="Related Notes plugin data.json was not found.",
        )
    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_plugin_settings_invalid",
            next_action="Corrigir data.json do plugin Related Notes e repetir a recuperação do export.",
            detail=str(exc),
        ) from exc
    return parsed if isinstance(parsed, dict) else {}


def _profile_id(value: Any) -> str:
    text = str(value or DEFAULT_PROFILE_ID)
    return text if text in {"clean_v1", "raw_v1", "legacy_v0"} else DEFAULT_PROFILE_ID


def _related_limit(value: Any) -> int:
    if isinstance(value, int | float) and math.isfinite(value):
        return max(1, min(50, int(value)))
    return 10


def _delay_seconds(value: Any) -> float:
    if isinstance(value, int | float) and math.isfinite(value):
        return max(MIN_EMBEDDING_REQUEST_DELAY_SECONDS, float(int(value)) / 1000.0)
    return MIN_EMBEDDING_REQUEST_DELAY_SECONDS


def _max_embedding_seconds(value: float | None) -> float:
    if value is not None and math.isfinite(value):
        return max(0.0, float(value))
    raw = os.environ.get("MEDNOTES_RELATED_NOTES_HEADLESS_MAX_SECONDS", "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            return DEFAULT_MAX_EMBEDDING_SECONDS
        if math.isfinite(parsed):
            return max(0.0, parsed)
    return DEFAULT_MAX_EMBEDDING_SECONDS


def _load_markdown_notes(wiki_dir: Path, profile_id: str) -> list[_MarkdownNote]:
    notes: list[_MarkdownNote] = []
    for index, path in enumerate(sorted(wiki_dir.rglob("*.md")), start=1):
        cooperative_cpu_yield(index)
        rel = path.relative_to(wiki_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        markdown = path.read_text(encoding="utf-8")
        rel_path = rel.as_posix()
        title = path.stem
        representation = _build_representation(path=rel_path, title=title, markdown=markdown, profile_id=profile_id)
        representation_hash = related_notes_representation_hash(
            path=rel_path,
            title=title,
            markdown=markdown,
            profile_id=profile_id,
        )
        notes.append(
            _MarkdownNote(
                rel_path=rel_path,
                abs_path=path,
                title=title,
                markdown=markdown,
                raw_hash=_sha256_text(markdown),
                representation=representation,
                representation_hash=representation_hash,
            )
        )
    return notes


def _build_representation(*, path: str, title: str, markdown: str, profile_id: str) -> str:
    body = _profile_body(markdown, profile_id)
    truncated = body[:MAX_EMBEDDING_CHARS]
    return f"Título: {title}\nCaminho: {path}\n\nConteúdo:\n{truncated}"


def _profile_body(markdown: str, profile_id: str) -> str:
    if profile_id == "raw_v1":
        return markdown
    if profile_id == "legacy_v0":
        return _clean_markdown_legacy(markdown)
    return _clean_markdown_v1(markdown)


def _clean_markdown_legacy(markdown: str) -> str:
    text = _FRONTMATTER_YAML_RE.sub("", markdown)
    text = _CODE_BLOCK_RE.sub("[CODE BLOCK]", text)
    text = _WIKILINK_ALIAS_RE.sub(r"\2", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_markdown_v1(markdown: str) -> str:
    return _clean_markdown_v1_with_table_normalization(markdown)


def _clean_markdown_v1_legacy_table_spacing(markdown: str) -> str:
    return _clean_markdown_v1_base(markdown, normalize_tables=False)


def _clean_markdown_v1_with_table_normalization(markdown: str) -> str:
    return _clean_markdown_v1_base(markdown, normalize_tables=True)


def _clean_markdown_v1_base(markdown: str, *, normalize_tables: bool) -> str:
    code_blocks: list[str] = []

    def stash_code_block(match: re.Match[str]) -> str:
        token = f"@@RELATED_NOTES_CODE_BLOCK_{len(code_blocks)}@@"
        code_blocks.append(match.group(0))
        return token

    text = _CODE_BLOCK_RE.sub(stash_code_block, markdown)
    text = _FRONTMATTER_YAML_RE.sub("", text)
    text = _FRONTMATTER_TOML_RE.sub("", text)
    text = _remove_related_notes_section(text)
    text = _GENERATED_FOOTER_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    text = _OBSIDIAN_IMAGE_RE.sub("", text)
    text = _MARKDOWN_IMAGE_RE.sub("", text)
    text = _WIKILINK_ALIAS_RE.sub(r"\2", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    if normalize_tables:
        text = _normalize_markdown_table_padding(text)
    text = _restore_code_blocks(text, code_blocks)
    return _normalize_clean_whitespace(text)


def _normalize_markdown_table_padding(text: str) -> str:
    return "\n".join(_normalize_markdown_table_row(line) for line in text.splitlines())


def _normalize_markdown_table_row(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 2:
        return line
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return "| " + " | ".join(_normalize_markdown_table_cell(cell) for cell in cells) + " |"


def _normalize_markdown_table_cell(cell: str) -> str:
    compact = cell.replace(" ", "").replace("\t", "")
    if re.fullmatch(r":?-{3,}:?", compact):
        return f"{':' if compact.startswith(':') else ''}---{':' if compact.endswith(':') else ''}"
    return cell


def _remove_related_notes_section(text: str) -> str:
    match = _RELATED_HEADING_RE.search(text)
    if not match:
        return text
    next_heading = _NEXT_H2_RE.search(text, match.end())
    end = next_heading.start() if next_heading else len(text)
    return f"{text[: match.start()]}{text[end:]}"


def _restore_code_blocks(text: str, code_blocks: list[str]) -> str:
    def restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return code_blocks[index] if 0 <= index < len(code_blocks) else ""

    return re.sub(r"@@RELATED_NOTES_CODE_BLOCK_(\d+)@@", restore, text)


def _normalize_clean_whitespace(text: str) -> str:
    return (
        "\n".join(line.rstrip() for line in text.splitlines())
        .replace("\n\n\n", "\n\n")
        .strip()
    )


def _load_vector_index(index_path: Path) -> dict[str, Any]:
    if not index_path.is_file():
        return {}
    try:
        parsed = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _current_records(index: dict[str, Any], profile_id: str) -> dict[str, RelatedNotesVectorRecord]:
    vector_index = RelatedNotesVectorIndex.model_validate(index)
    return vector_index.records_for_profile(profile_id)


def _record_is_current(record: RelatedNotesVectorRecord | None, note: _MarkdownNote, profile_id: str) -> bool:
    if not record:
        return False
    return (
        record.representation_hash == note.representation_hash
        and record.embedding_model == DEFAULT_EMBEDDING_MODEL
        and record.embedding_profile == profile_id
        and record.embedding_profile_version == PROFILE_VERSION
        and _valid_vector(record.vector)
    )


def _record_is_legacy_clean_v1_current(
    record: RelatedNotesVectorRecord | None,
    note: _MarkdownNote,
    profile_id: str,
) -> bool:
    if profile_id != "clean_v1" or not record or not _valid_vector(record.vector):
        return False
    return (
        record.representation_hash == related_notes_legacy_clean_v1_representation_hash(
            path=note.rel_path,
            title=note.title,
            markdown=note.markdown,
        )
        and record.embedding_model == DEFAULT_EMBEDDING_MODEL
        and record.embedding_profile == profile_id
        and record.embedding_profile_version == PROFILE_VERSION
    )


def _migration_vector(record: RelatedNotesVectorRecord | None, note: _MarkdownNote, profile_id: str) -> list[float] | None:
    if not record or not _valid_vector(record.vector):
        return None
    if not (_record_is_current(record, note, profile_id) or _record_is_legacy_clean_v1_current(record, note, profile_id)):
        return None
    return [float(item) for item in record.vector]


def related_notes_legacy_clean_v1_content_hash(*, path: str, title: str, markdown: str) -> str:
    return "sha256:" + related_notes_legacy_clean_v1_representation_hash(path=path, title=title, markdown=markdown)


def related_notes_legacy_clean_v1_representation_hash(*, path: str, title: str, markdown: str) -> str:
    representation = (
        f"Título: {title}\n"
        f"Caminho: {path}\n\n"
        "Conteúdo:\n"
        f"{_clean_markdown_v1_legacy_table_spacing(markdown)[:MAX_EMBEDDING_CHARS]}"
    )
    return _sha256_text(representation)


def _recovery_progress_counts(
    *,
    records: dict[str, RelatedNotesVectorRecord],
    notes: list[_MarkdownNote],
    reused_count: int,
    embedded_count: int,
) -> dict[str, int]:
    total_note_count = len(notes)
    fresh_record_count = min(total_note_count, max(0, reused_count) + max(0, embedded_count))
    record_count = len(records)
    return {
        "record_count": record_count,
        "fresh_record_count": fresh_record_count,
        "stale_record_count": max(0, record_count - fresh_record_count),
        "remaining_count": max(0, total_note_count - fresh_record_count),
    }


def _record(note: _MarkdownNote, vector: list[float], *, profile_id: str, updated_at: int) -> RelatedNotesVectorRecord:
    return RelatedNotesVectorRecord(
        path=note.rel_path,
        title=note.title,
        folder=str(Path(note.rel_path).parent).replace(".", "") if "/" in note.rel_path else "",
        preview=_make_preview(note.representation),
        rawContentHash=note.raw_hash,
        representationHash=note.representation_hash,
        contentHash=note.representation_hash,
        mtime=int(note.abs_path.stat().st_mtime * 1000),
        embeddingModel=DEFAULT_EMBEDDING_MODEL,
        embeddingProfile=profile_id,
        embeddingProfileVersion=PROFILE_VERSION,
        vector=[float(item) for item in vector],
        updatedAt=updated_at,
    )


def _make_preview(representation: str, limit: int = 200) -> str:
    lines = representation.split("\n")
    try:
        content_index = next(index for index, line in enumerate(lines) if line.startswith("Conteúdo:"))
    except StopIteration:
        content_index = -1
    content = " ".join(lines[content_index + 1 :])
    return content[:limit] + ("..." if len(content) > limit else "")


def _vector_index(
    existing_index: dict[str, Any],
    records: dict[str, RelatedNotesVectorRecord],
    *,
    profile_id: str,
    updated_at: int,
) -> dict[str, Any]:
    profiles = RelatedNotesVectorIndex.model_validate(existing_index).other_profiles_payload(profile_id)
    profiles[profile_id] = {
        "profileId": profile_id,
        "profileVersion": PROFILE_VERSION,
        "embeddingModel": DEFAULT_EMBEDDING_MODEL,
        "updatedAt": updated_at,
        "records": {key: value.to_payload() for key, value in sorted(records.items())},
    }
    return {
        "schema": "related-notes-obsidian.vector-index.v2",
        "updatedAt": updated_at,
        "profiles": profiles,
    }


def _export_payload(
    notes: list[_MarkdownNote],
    records: dict[str, RelatedNotesVectorRecord],
    wiki_dir: Path,
    *,
    profile_id: str,
    related_limit: int,
    generated_at: str,
) -> dict[str, Any]:
    indexed_notes = [note for note in notes if note.rel_path in records]
    note_paths = {note.rel_path for note in indexed_notes}
    normalized_vectors = {
        note.rel_path: normalized
        for note in indexed_notes
        if (normalized := _normalized_vector(records[note.rel_path].vector)) is not None
    }
    candidates_by_source: dict[str, list[tuple[float, str]]] = {note.rel_path: [] for note in indexed_notes}
    for source_index, source in enumerate(indexed_notes):
        source_vector = normalized_vectors.get(source.rel_path)
        for target in indexed_notes[source_index + 1 :]:
            target_vector = normalized_vectors.get(target.rel_path)
            score = _cosine_normalized(source_vector, target_vector) if source_vector is not None else 0.0
            if source_vector is not None:
                candidates_by_source[source.rel_path].append((score, target.rel_path))
            if target_vector is not None:
                candidates_by_source[target.rel_path].append((score, source.rel_path))
    edges: list[dict[str, Any]] = []
    for note in indexed_notes:
        for rank, (score, target_path) in enumerate(
            sorted(candidates_by_source.get(note.rel_path, []), key=lambda item: (-item[0], item[1]))[:related_limit],
            start=1,
        ):
            if target_path in note_paths:
                edges.append(
                    {
                        "source_path": note.rel_path,
                        "target_path": target_path,
                        "score": _clamp_score(score),
                        "rank": rank,
                        "source": PLUGIN_ID,
                    }
                )
    return {
        "schema": RELATED_NOTES_EXPORT_SCHEMA,
        "generated_at": generated_at,
        "vault_root": ".",
        "plugin": {"name": PLUGIN_ID, "version": _plugin_version(wiki_dir)},
        "model": {
            "embedding_model": DEFAULT_EMBEDDING_MODEL,
            "embedding_profile_id": profile_id,
            "embedding_profile_version": PROFILE_VERSION,
            "representation_hash_basis": _representation_hash_basis(profile_id),
        },
        "score_scale": "0_to_1",
        "notes": [
            {"path": note.rel_path, "title": note.title, "content_hash": "sha256:" + note.representation_hash}
            for note in sorted(indexed_notes, key=lambda item: item.rel_path)
        ],
        "edges": edges,
    }


def _plugin_version(wiki_dir: Path) -> str:
    manifest = wiki_dir / ".obsidian" / "plugins" / PLUGIN_ID / "manifest.json"
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "headless"
    return str(parsed.get("version") or "headless") if isinstance(parsed, dict) else "headless"


def _representation_hash_basis(profile_id: str) -> str:
    if profile_id == "raw_v1":
        return "raw_markdown"
    if profile_id == "legacy_v0":
        return "legacy_hybrid_markdown"
    return "profile_cleaned_markdown"


def _normalized_vector(value: Any) -> tuple[float, ...] | None:
    if not _valid_vector(value):
        return None
    floats = tuple(float(item) for item in value)
    norm = math.sqrt(sum(item * item for item in floats))
    if norm == 0:
        return None
    return tuple(item / norm for item in floats)


def _cosine_normalized(left: tuple[float, ...], right: tuple[float, ...] | None) -> float:
    if right is None or len(left) != len(right):
        return 0.0
    sumprod = getattr(math, "sumprod", None)
    if callable(sumprod):
        typed_sumprod = cast(Callable[[Iterable[float], Iterable[float]], float], sumprod)
        return float(typed_sumprod(left, right))
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cosine(left: Any, right: Any) -> float:
    if not _valid_vector(left) or not _valid_vector(right) or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _valid_vector(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, int | float) for item in value)


def _clamp_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _chunks(items: list[_MarkdownNote], size: int) -> Iterable[list[_MarkdownNote]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _default_embedding_client(texts: list[str], *, api_key: str, model: str) -> list[list[float]]:
    if len(texts) > 1:
        return _batch_embed(texts, api_key=api_key, model=model)
    return [_single_embed(text, api_key=api_key, model=model) for text in texts]


def _batch_embed(texts: list[str], *, api_key: str, model: str) -> list[list[float]]:
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents",
        params={"key": api_key},
        json={
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                }
                for text in texts
            ]
        },
        timeout=60.0,
    )
    if response.status_code in {400, 404}:
        raise BatchEmbeddingUnavailable("batch embedding endpoint unavailable")
    if response.status_code == 429:
        raise BatchEmbeddingUnavailable(_response_error_text(response), rate_limited=True)
    if response.status_code >= 400:
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_embedding_failed",
            next_action="Verificar chave/quota/rede do Gemini embeddings e repetir a recuperação do export.",
            detail=_redact_error(response.text),
        )
    try:
        payload = GeminiBatchEmbeddingResponse.model_validate(response.json())
    except ValidationError as exc:
        raise RuntimeError("batch embedding response missing embeddings[]") from exc
    return [_extract_embedding(item) for item in payload.embeddings]


def _single_embed(text: str, *, api_key: str, model: str) -> list[float]:
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent",
        params={"key": api_key},
        json={"model": f"models/{model}", "content": {"parts": [{"text": text}]}},
        timeout=60.0,
    )
    if response.status_code >= 400:
        if response.status_code == 429:
            raise _quota_error(response)
        raise HeadlessRelatedNotesExportError(
            blocked_reason="related_notes_headless_embedding_failed",
            next_action="Verificar chave/quota/rede do Gemini embeddings e repetir a recuperação do export.",
            detail=_redact_error(response.text),
        )
    try:
        payload = GeminiEmbeddingResponse.model_validate(response.json())
    except ValidationError as exc:
        raise RuntimeError("embedding response missing values[]") from exc
    return _extract_embedding(payload.embedding)


def _quota_error(response: httpx.Response) -> HeadlessRelatedNotesExportError:
    return HeadlessRelatedNotesExportError(
        blocked_reason="related_notes_headless_quota_exhausted",
        next_action=(
            "Aguardar a quota do Gemini embeddings voltar ou trocar a chave no plugin Related Notes; "
            "depois repetir a atualização das Notas Relacionadas pela rota oficial."
        ),
        detail=_redact_error(_response_error_text(response)),
    )


def _extract_embedding(value: GeminiEmbedding) -> list[float]:
    return [float(item) for item in value.values]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redact_error(message: str) -> str:
    return re.sub(r"key=[A-Za-z0-9_\-]+", "key=<redacted>", message)[:500]


def _response_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    try:
        error_payload = GeminiErrorResponse.model_validate(payload)
    except ValidationError:
        return response.text
    if error_payload.error is not None:
        status = error_payload.error.status
        message = error_payload.error.message
        code = str(error_payload.error.code or response.status_code)
        return " ".join(part for part in [code, status, message] if part)
    return response.text
