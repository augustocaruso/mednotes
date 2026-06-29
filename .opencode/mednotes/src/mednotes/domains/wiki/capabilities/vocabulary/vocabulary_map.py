"""Vocabulary DB and YAML-claim diagnosis for Wiki link semantics."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, TypedDict

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import infer_title
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import extract_aliases, normalize_key
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note as _is_index_note
from mednotes.domains.wiki.common import wiki_cli_command
from mednotes.domains.wiki.contracts.workflow_blockers import decision_for_code
from mednotes.kernel.base import JsonObject

VOCABULARY_MAP_SCHEMA = "medical-notes-workbench.vocabulary-map.v1"
VocabularyHashRowValue: TypeAlias = str | int | float | None
VocabularyHashPayload: TypeAlias = dict[str, list[dict[str, VocabularyHashRowValue]]]


class VocabularyIssuePayload(TypedDict, total=False):
    severity: str
    code: str
    message: str
    phase: str
    note_path: str
    surface: str
    next_action: str
    required_inputs: list[str]
    stale_count: int
    surface_count: int
    meaning_id: str
    label: str
    decision_summary: JsonObject
    display_text: str


class VocabularyDiagnosisPayload(TypedDict):
    schema: str
    status: str
    db_path: str
    map_hash: str
    note_count: int
    meaning_count: int
    surface_count: int
    ambiguous_surface_count: int
    pending_semantic_ingestion_count: int
    issues: list[VocabularyIssuePayload]


@dataclass(frozen=True)
class KnownMeaningSeed:
    surface: str
    meaning: str
    note_title: str = ""
    semantic_type: str = "medical_concept"
    intrinsically_ambiguous: bool = False
    ambiguity_reason: str = ""


@dataclass(frozen=True)
class AliasClaim:
    note_path: str
    alias_text: str
    normalized_surface: str
    claim_status: str
    link_policy: str
    visible_in_yaml: bool = True
    meaning_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SurfaceInfo:
    normalized_surface: str
    best_display_text: str
    intrinsically_ambiguous: bool
    direct_link_allowed: bool
    link_policy: str


@dataclass(frozen=True)
class VocabularyBlocker:
    code: str
    message: str
    note_path: str = ""
    surface: str = ""


@dataclass(frozen=True)
class ProjectionAlias:
    text: str
    normalized_surface: str
    link_policy: str
    visible_in_yaml: bool
    source: str
    order: int


@dataclass
class VocabularyMap:
    schema: str = VOCABULARY_MAP_SCHEMA
    db_path: Path | None = None
    alias_claims: list[AliasClaim] = field(default_factory=list)
    surfaces: dict[str, SurfaceInfo] = field(default_factory=dict)
    blockers: list[VocabularyBlocker] = field(default_factory=list)
    note_aliases: dict[str, list[ProjectionAlias]] = field(default_factory=dict)
    map_hash: str = ""
    note_count: int = 0
    meaning_count: int = 0
    surface_count: int = 0
    ambiguous_surface_count: int = 0
    pending_semantic_ingestion_count: int = 0
    issues: list[VocabularyIssuePayload] = field(default_factory=list)

    def as_diagnosis_dict(self) -> VocabularyDiagnosisPayload:
        human_codes = {
            "vocabulary_map.duplicate_meaning",
            "vocabulary_map.non_atomic_note",
            "vocabulary_map.conflicting_alias",
        }
        has_human_issue = any(
            issue.get("severity") == "human_decision" or issue.get("code") in human_codes
            for issue in self.issues
        )
        if has_human_issue:
            status = "blocked_human"
        elif self.pending_semantic_ingestion_count > 0 or any(
            issue.get("severity") == "blocker" for issue in self.issues
        ):
            status = "blocked_pending"
        else:
            status = "ready"
        return {
            "schema": self.schema,
            "status": status,
            "db_path": str(self.db_path) if self.db_path else "",
            "map_hash": self.map_hash,
            "note_count": self.note_count,
            "meaning_count": self.meaning_count,
            "surface_count": self.surface_count,
            "ambiguous_surface_count": self.ambiguous_surface_count,
            "pending_semantic_ingestion_count": self.pending_semantic_ingestion_count,
            "issues": self.issues,
        }


def meaning_id_for(label: str) -> str:
    normalized = normalize_key(label).replace(" ", "_")
    return "meaning:" + (normalized or "unknown")


def initialize_vocabulary_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY,
              path TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              stem TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('active', 'deleted', 'renamed', 'merged')),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meanings (
              id TEXT PRIMARY KEY,
              label TEXT NOT NULL,
              normalized_label TEXT NOT NULL,
              semantic_type TEXT NOT NULL DEFAULT '',
              atomic_status TEXT NOT NULL CHECK (atomic_status IN ('atomic', 'suspected_non_atomic', 'duplicate_candidate', 'unknown')),
              status TEXT NOT NULL CHECK (status IN ('active', 'retired', 'needs_review')),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS surfaces (
              id INTEGER PRIMARY KEY,
              normalized_surface TEXT NOT NULL UNIQUE,
              best_display_text TEXT NOT NULL,
              intrinsically_ambiguous INTEGER NOT NULL DEFAULT 0,
              ambiguity_reason TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meaning_note_links (
              id INTEGER PRIMARY KEY,
              meaning_id TEXT NOT NULL REFERENCES meanings(id),
              note_id INTEGER NOT NULL REFERENCES notes(id),
              role TEXT NOT NULL CHECK (role IN ('canonical', 'alias_target', 'historical')),
              status TEXT NOT NULL CHECK (status IN ('active', 'retired', 'needs_review')),
              confidence REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(meaning_id, note_id, role)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_active_canonical_note_per_meaning
            ON meaning_note_links(meaning_id)
            WHERE role = 'canonical' AND status = 'active';

            CREATE UNIQUE INDEX IF NOT EXISTS one_active_primary_meaning_per_note
            ON meaning_note_links(note_id)
            WHERE role = 'canonical' AND status = 'active';

            CREATE TABLE IF NOT EXISTS surface_meaning_policy (
              id INTEGER PRIMARY KEY,
              surface_id INTEGER NOT NULL REFERENCES surfaces(id),
              meaning_id TEXT NOT NULL REFERENCES meanings(id),
              link_policy TEXT NOT NULL CHECK (link_policy IN ('direct', 'requires_context', 'blocked', 'no_link')),
              visible_in_yaml INTEGER NOT NULL DEFAULT 1,
              display_text TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL CHECK (source IN ('curator', 'yaml', 'projection', 'human', 'llm', 'system')),
              confidence REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(surface_id, meaning_id)
            );

            CREATE TABLE IF NOT EXISTS note_semantic_ingestion_queue (
              id INTEGER PRIMARY KEY,
              note_id INTEGER NOT NULL REFERENCES notes(id),
              note_path TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              queue_flags_json TEXT NOT NULL,
              assigned_agent TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'applied', 'blocked', 'stale')),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(note_path, content_hash)
            );

            CREATE TABLE IF NOT EXISTS yaml_alias_claims (
              id INTEGER PRIMARY KEY,
              note_id INTEGER NOT NULL REFERENCES notes(id),
              alias_text TEXT NOT NULL,
              normalized_surface TEXT NOT NULL,
              note_hash TEXT NOT NULL,
              source TEXT NOT NULL CHECK (source IN ('yaml', 'projection', 'human', 'llm')),
              claim_status TEXT NOT NULL CHECK (
                claim_status IN ('accepted_alias', 'contextual_alias', 'duplicate_alias', 'conflicting_alias', 'stale_alias')
              ),
              link_policy TEXT NOT NULL CHECK (link_policy IN ('direct', 'requires_context', 'blocked', 'no_link')),
              visible_in_yaml INTEGER NOT NULL DEFAULT 1,
              first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(note_id, normalized_surface)
            );

            CREATE TABLE IF NOT EXISTS deferred_work_items (
              work_id TEXT PRIMARY KEY,
              source_agent TEXT NOT NULL,
              assigned_agent TEXT NOT NULL,
              reason TEXT NOT NULL,
              note_path TEXT,
              content_hash TEXT,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'blocked', 'cancelled')),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS contextual_alias_decisions (
              occurrence_id TEXT PRIMARY KEY,
              note_path TEXT NOT NULL,
              normalized_surface TEXT NOT NULL,
              matched_text TEXT NOT NULL,
              context_hash TEXT NOT NULL,
              candidate_targets_json TEXT NOT NULL,
              action TEXT NOT NULL CHECK (action IN ('link', 'no_link', 'defer')),
              chosen_meaning_id TEXT NOT NULL DEFAULT '',
              chosen_target_path TEXT NOT NULL DEFAULT '',
              chosen_target TEXT NOT NULL DEFAULT '',
              confidence REAL NOT NULL DEFAULT 0,
              model TEXT NOT NULL DEFAULT '',
              response_hash TEXT NOT NULL DEFAULT '',
              reason_code TEXT NOT NULL DEFAULT '',
              rationale_summary TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL CHECK (status IN ('active', 'rejected', 'stale')),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def note_content_hash(path: Path) -> str:
    return "sha256:" + file_sha256(path)


def upsert_note(conn: sqlite3.Connection, *, path: Path, title: str, content_hash: str) -> int:
    conn.execute(
        """
        INSERT INTO notes(path, title, stem, content_hash, status)
        VALUES (?, ?, ?, ?, 'active')
        ON CONFLICT(path) DO UPDATE SET
          title=excluded.title,
          stem=excluded.stem,
          content_hash=excluded.content_hash,
          status='active',
          updated_at=CURRENT_TIMESTAMP
        """,
        (str(path), title, path.stem, content_hash),
    )
    row = conn.execute("SELECT id FROM notes WHERE path = ?", (str(path),)).fetchone()
    if row is None:  # pragma: no cover - sqlite invariant
        raise RuntimeError(f"failed to upsert note: {path}")
    return int(row[0])


def upsert_meaning(
    conn: sqlite3.Connection,
    *,
    meaning_id: str,
    label: str,
    semantic_type: str = "medical_concept",
    atomic_status: str = "atomic",
) -> None:
    conn.execute(
        """
        INSERT INTO meanings(id, label, normalized_label, semantic_type, atomic_status, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        ON CONFLICT(id) DO UPDATE SET
          label=excluded.label,
          normalized_label=excluded.normalized_label,
          semantic_type=excluded.semantic_type,
          atomic_status=excluded.atomic_status,
          status='active',
          updated_at=CURRENT_TIMESTAMP
        """,
        (meaning_id, label, normalize_key(label), semantic_type, atomic_status),
    )


def upsert_surface(
    conn: sqlite3.Connection,
    *,
    display_text: str,
    intrinsically_ambiguous: bool = False,
    ambiguity_reason: str = "",
) -> int:
    normalized = normalize_key(display_text)
    row = conn.execute("SELECT id, intrinsically_ambiguous, best_display_text FROM surfaces WHERE normalized_surface = ?", (normalized,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO surfaces(normalized_surface, best_display_text, intrinsically_ambiguous, ambiguity_reason)
            VALUES (?, ?, ?, ?)
            """,
            (normalized, display_text, int(intrinsically_ambiguous), ambiguity_reason),
        )
    else:
        existing_ambiguous = bool(row[1])
        best = _best_display_text(str(row[2]), display_text)
        conn.execute(
            """
            UPDATE surfaces
            SET best_display_text = ?, intrinsically_ambiguous = ?, ambiguity_reason = CASE WHEN ? != '' THEN ? ELSE ambiguity_reason END,
                updated_at = CURRENT_TIMESTAMP
            WHERE normalized_surface = ?
            """,
            (best, int(existing_ambiguous or intrinsically_ambiguous), ambiguity_reason, ambiguity_reason, normalized),
        )
    row = conn.execute("SELECT id FROM surfaces WHERE normalized_surface = ?", (normalized,)).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError(f"failed to upsert surface: {display_text}")
    return int(row[0])


def upsert_policy(
    conn: sqlite3.Connection,
    *,
    surface_id: int,
    meaning_id: str,
    link_policy: str,
    display_text: str,
    visible_in_yaml: bool = True,
    source: str = "system",
    confidence: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO surface_meaning_policy(
          surface_id, meaning_id, link_policy, visible_in_yaml, display_text, source, confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(surface_id, meaning_id) DO UPDATE SET
          link_policy=excluded.link_policy,
          visible_in_yaml=excluded.visible_in_yaml,
          display_text=excluded.display_text,
          source=excluded.source,
          confidence=excluded.confidence,
          updated_at=CURRENT_TIMESTAMP
        """,
        (surface_id, meaning_id, link_policy, int(visible_in_yaml), display_text, source, confidence),
    )


def _best_display_text(left: str, right: str) -> str:
    def score(value: str) -> tuple[int, int, int]:
        return (
            int(any(ord(char) > 127 for char in value)),
            int(value.isupper() and len(value) <= 8),
            sum(1 for char in value if char.isupper()),
        )

    return max([left, right], key=score)


def _scan_notes(wiki_dir: Path) -> list[tuple[Path, str, str]]:
    notes: list[tuple[Path, str, str]] = []
    if not wiki_dir.exists():
        return notes
    for path in iter_notes(wiki_dir):
        text = path.read_text(encoding="utf-8")
        if _is_index_note(path, text):
            continue
        notes.append((path, infer_title(text, path), text))
    return notes


def _seed_meaning_id(seed: KnownMeaningSeed) -> str:
    return meaning_id_for(seed.meaning)


def rebuild_vocabulary_map(
    *,
    wiki_dir: Path,
    db_path: Path,
    import_yaml_aliases: bool,
    known_meanings: list[KnownMeaningSeed] | None = None,
) -> VocabularyMap:
    initialize_vocabulary_db(db_path)
    seeds = known_meanings or []
    result = VocabularyMap(db_path=db_path)
    notes = _scan_notes(wiki_dir)
    note_ids: dict[Path, int] = {}
    with sqlite3.connect(db_path) as conn:
        for path, title, _text in notes:
            note_ids[path] = upsert_note(conn, path=path, title=title, content_hash=note_content_hash(path))

        for seed in seeds:
            meaning_id = _seed_meaning_id(seed)
            upsert_meaning(conn, meaning_id=meaning_id, label=seed.meaning, semantic_type=seed.semantic_type)
            surface_id = upsert_surface(
                conn,
                display_text=seed.surface,
                intrinsically_ambiguous=seed.intrinsically_ambiguous,
                ambiguity_reason=seed.ambiguity_reason,
            )
            upsert_policy(
                conn,
                surface_id=surface_id,
                meaning_id=meaning_id,
                link_policy="requires_context" if seed.intrinsically_ambiguous else "direct",
                display_text=seed.surface,
                source="system",
            )
            if seed.note_title:
                matching = [path for path, title, _text in notes if normalize_key(title) == normalize_key(seed.note_title) or normalize_key(path.stem) == normalize_key(seed.note_title)]
                for path in matching:
                    try:
                        conn.execute(
                            """
                            INSERT INTO meaning_note_links(meaning_id, note_id, role, status, confidence)
                            VALUES (?, ?, 'canonical', 'active', 1.0)
                            ON CONFLICT(meaning_id, note_id, role) DO UPDATE SET status='active', updated_at=CURRENT_TIMESTAMP
                            """,
                            (meaning_id, note_ids[path]),
                        )
                    except sqlite3.IntegrityError:
                        result.blockers.append(
                            VocabularyBlocker(
                                code="vocabulary_map.duplicate_meaning",
                                message=f"Meaning {seed.meaning} maps to more than one active canonical note.",
                                note_path=str(path),
                                surface=seed.surface,
                            )
                        )

        if import_yaml_aliases:
            _import_yaml_alias_claims(conn, result, notes, note_ids, seeds)

        _load_surface_info(conn, result)
    return result


def _import_yaml_alias_claims(
    conn: sqlite3.Connection,
    result: VocabularyMap,
    notes: list[tuple[Path, str, str]],
    note_ids: dict[Path, int],
    seeds: list[KnownMeaningSeed],
) -> None:
    seeds_by_surface: dict[str, list[KnownMeaningSeed]] = {}
    for seed in seeds:
        seeds_by_surface.setdefault(normalize_key(seed.surface), []).append(seed)

    for path, title, text in notes:
        seen: set[str] = set()
        projection_items: list[ProjectionAlias] = []
        seed_order = 0
        for seed in seeds:
            if seed.note_title and normalize_key(seed.note_title) not in {normalize_key(title), normalize_key(path.stem)}:
                continue
            normalized = normalize_key(seed.surface)
            if normalized in seen:
                continue
            seen.add(normalized)
            policy = "requires_context" if _surface_requires_context(seeds_by_surface.get(normalized, [])) else "direct"
            projection_items.append(
                ProjectionAlias(
                    text=seed.surface,
                    normalized_surface=normalized,
                    link_policy=policy,
                    visible_in_yaml=True,
                    source="seed",
                    order=seed_order,
                )
            )
            seed_order += 1

        for alias in extract_aliases(text):
            normalized = normalize_key(alias)
            surface_seeds = seeds_by_surface.get(normalized, [])
            claim_status, link_policy, blocker = _classify_alias_claim(alias, title, surface_seeds)
            meaning_ids = tuple(_seed_meaning_id(seed) for seed in surface_seeds)
            claim = AliasClaim(
                note_path=str(path),
                alias_text=alias,
                normalized_surface=normalized,
                claim_status=claim_status,
                link_policy=link_policy,
                meaning_ids=meaning_ids,
            )
            result.alias_claims.append(claim)
            if blocker is not None:
                result.blockers.append(VocabularyBlocker(**{**blocker, "note_path": str(path), "surface": alias}))
            conn.execute(
                """
                INSERT INTO yaml_alias_claims(note_id, alias_text, normalized_surface, note_hash, source, claim_status, link_policy, visible_in_yaml)
                VALUES (?, ?, ?, ?, 'yaml', ?, ?, 1)
                ON CONFLICT(note_id, normalized_surface) DO UPDATE SET
                  alias_text=excluded.alias_text,
                  note_hash=excluded.note_hash,
                  claim_status=excluded.claim_status,
                  link_policy=excluded.link_policy,
                  visible_in_yaml=1,
                  last_seen_at=CURRENT_TIMESTAMP
                """,
                (note_ids[path], alias, normalized, note_content_hash(path), claim_status, link_policy),
            )
            if normalized not in seen and claim_status != "conflicting_alias":
                seen.add(normalized)
                projection_items.append(
                    ProjectionAlias(
                        text=alias,
                        normalized_surface=normalized,
                        link_policy=link_policy,
                        visible_in_yaml=True,
                        source="yaml",
                        order=seed_order,
                    )
                )
                seed_order += 1
        result.note_aliases[str(path)] = projection_items


def _classify_alias_claim(
    alias: str,
    note_title: str,
    surface_seeds: list[KnownMeaningSeed],
) -> tuple[str, str, dict[str, str] | None]:
    if not surface_seeds:
        return "contextual_alias", "requires_context", None
    meaning_to_titles: dict[str, set[str]] = {}
    for seed in surface_seeds:
        meaning_to_titles.setdefault(seed.meaning, set())
        if seed.note_title:
            meaning_to_titles[seed.meaning].add(seed.note_title)
    if any(len(titles) > 1 for titles in meaning_to_titles.values()):
        return (
            "conflicting_alias",
            "blocked",
            {
                "code": "vocabulary_map.duplicate_meaning",
                "message": f"Alias {alias} maps one meaning to multiple canonical notes.",
            },
        )
    if _surface_requires_context(surface_seeds):
        return "contextual_alias", "requires_context", None
    if len({seed.meaning for seed in surface_seeds}) == 1:
        return "accepted_alias", "direct", None
    return "contextual_alias", "requires_context", None


def _surface_requires_context(surface_seeds: list[KnownMeaningSeed]) -> bool:
    if len({seed.meaning for seed in surface_seeds}) > 1:
        return True
    return any(seed.intrinsically_ambiguous for seed in surface_seeds)


def _load_surface_info(conn: sqlite3.Connection, result: VocabularyMap) -> None:
    rows = conn.execute(
        """
        SELECT s.normalized_surface, s.best_display_text, s.intrinsically_ambiguous,
               COUNT(DISTINCT p.meaning_id) AS meaning_count,
               SUM(CASE WHEN p.link_policy = 'direct' THEN 1 ELSE 0 END) AS direct_count
        FROM surfaces s
        LEFT JOIN surface_meaning_policy p ON p.surface_id = s.id
        GROUP BY s.id
        """
    ).fetchall()
    for normalized, display, ambiguous, meaning_count, direct_count in rows:
        link_policy = "direct" if direct_count and meaning_count == 1 and not ambiguous else "requires_context"
        result.surfaces[str(normalized)] = SurfaceInfo(
            normalized_surface=str(normalized),
            best_display_text=str(display),
            intrinsically_ambiguous=bool(ambiguous),
            direct_link_allowed=link_policy == "direct",
            link_policy=link_policy,
        )


def pending_semantic_ingestion_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM note_semantic_ingestion_queue WHERE status IN ('pending', 'claimed')").fetchone()
    return int(row[0]) if row else 0


def _query_scalar_count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _hash_row_value(value: object) -> VocabularyHashRowValue:
    if value is None or isinstance(value, str | int | float):
        return value
    return str(value)


def vocabulary_map_hash(db_path: Path) -> str:
    initialize_vocabulary_db(db_path)
    tables = (
        "notes",
        "meanings",
        "surfaces",
        "meaning_note_links",
        "surface_meaning_policy",
        "yaml_alias_claims",
        "note_semantic_ingestion_queue",
        "deferred_work_items",
    )
    payload: VocabularyHashPayload = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            stable_columns = [column for column in columns if not column.endswith("_at")]
            order_by = ", ".join(stable_columns) if stable_columns else "rowid"
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
            payload[table] = [
                {key: _hash_row_value(row[key]) for key in row.keys() if not key.endswith("_at")}
                for row in rows
            ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _issue(
    *,
    severity: str,
    code: str,
    message: str,
    phase: str = "vocabulary_map_diagnosis",
    note_path: str = "",
    surface: str = "",
    next_action: str = "",
    required_inputs: list[str] | None = None,
) -> VocabularyIssuePayload:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "phase": phase,
        "note_path": note_path,
        "surface": surface,
        "next_action": next_action,
        "required_inputs": required_inputs or [],
    }


def _load_alias_claims(conn: sqlite3.Connection, result: VocabularyMap) -> None:
    rows = conn.execute(
        """
        SELECT n.path, c.alias_text, c.normalized_surface, c.claim_status, c.link_policy, c.visible_in_yaml
        FROM yaml_alias_claims c
        JOIN notes n ON n.id = c.note_id
        ORDER BY n.path, c.normalized_surface
        """
    ).fetchall()
    for note_path, alias_text, normalized_surface, claim_status, link_policy, visible in rows:
        claim = AliasClaim(
            note_path=str(note_path),
            alias_text=str(alias_text),
            normalized_surface=str(normalized_surface),
            claim_status=str(claim_status),
            link_policy=str(link_policy),
            visible_in_yaml=bool(visible),
        )
        result.alias_claims.append(claim)
        if claim.visible_in_yaml:
            result.note_aliases.setdefault(claim.note_path, []).append(
                ProjectionAlias(
                    text=claim.alias_text,
                    normalized_surface=claim.normalized_surface,
                    link_policy=claim.link_policy,
                    visible_in_yaml=True,
                    source="yaml",
                    order=len(result.note_aliases.get(claim.note_path, [])),
                )
            )


def _load_db_projection_aliases(conn: sqlite3.Connection, result: VocabularyMap) -> None:
    rows = conn.execute(
        """
        SELECT n.path,
               COALESCE(NULLIF(p.display_text, ''), s.best_display_text) AS display_text,
               s.normalized_surface,
               p.link_policy,
               p.visible_in_yaml,
               p.source
        FROM notes n
        JOIN meaning_note_links l ON l.note_id = n.id
        JOIN surface_meaning_policy p ON p.meaning_id = l.meaning_id
        JOIN surfaces s ON s.id = p.surface_id
        WHERE n.status = 'active'
          AND l.role = 'canonical'
          AND l.status = 'active'
          AND p.visible_in_yaml = 1
          AND p.link_policy IN ('direct', 'requires_context')
        ORDER BY n.path, s.normalized_surface
        """
    ).fetchall()
    for note_path, display_text, normalized_surface, link_policy, visible, source in rows:
        items = result.note_aliases.setdefault(str(note_path), [])
        items.append(
            ProjectionAlias(
                text=str(display_text),
                normalized_surface=str(normalized_surface),
                link_policy=str(link_policy),
                visible_in_yaml=bool(visible),
                source=str(source or "curator"),
                order=len(items),
            )
        )


def load_vocabulary_map_diagnosis(db_path: Path) -> VocabularyMap:
    initialize_vocabulary_db(db_path)
    result = VocabularyMap(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        result.note_count = _query_scalar_count(conn, "SELECT COUNT(*) FROM notes WHERE status = 'active'")
        result.meaning_count = _query_scalar_count(conn, "SELECT COUNT(*) FROM meanings WHERE status = 'active'")
        result.surface_count = _query_scalar_count(conn, "SELECT COUNT(*) FROM surfaces")
        result.ambiguous_surface_count = _query_scalar_count(conn, "SELECT COUNT(*) FROM surfaces WHERE intrinsically_ambiguous = 1")
        result.pending_semantic_ingestion_count = _query_scalar_count(
            conn,
            "SELECT COUNT(*) FROM note_semantic_ingestion_queue WHERE status IN ('pending', 'claimed')",
        )
        stale_semantic_ingestion_count = _query_scalar_count(
            conn,
            "SELECT COUNT(*) FROM note_semantic_ingestion_queue WHERE status='stale'",
        )
        _load_surface_info(conn, result)
        _load_db_projection_aliases(conn, result)
        _load_alias_claims(conn, result)
        if result.pending_semantic_ingestion_count:
            result.issues.append(
                _issue(
                    severity="blocker",
                    code="vocabulary_semantic_ingestion_pending",
                    message="Semantic ingestion queue has pending notes.",
                    next_action="Processar note_semantic_ingestion_queue com med-link-graph-curator.",
                    required_inputs=["vocabulary_semantic_ingestion"],
                )
            )
        if stale_semantic_ingestion_count:
            recovery = wiki_cli_command("vocabulary-recover", "--mode", "reconcile-queue", "--dry-run", "--json")
            result.issues.append(
                _issue(
                    severity="blocker",
                    code="vocabulary_semantic_ingestion_stale",
                    message="Semantic ingestion queue has stale notes that must be refreshed before curation can continue.",
                    next_action=recovery,
                    required_inputs=["vocabulary_recovery", "vocabulary_semantic_ingestion"],
                )
                | {"stale_count": stale_semantic_ingestion_count}
            )
        unresolved_surface_count = _query_scalar_count(
            conn,
            """
            SELECT COUNT(*)
            FROM surfaces s
            LEFT JOIN surface_meaning_policy p ON p.surface_id = s.id
            WHERE p.id IS NULL
            """,
        )
        if unresolved_surface_count:
            result.issues.append(
                _issue(
                    severity="blocker",
                    code="vocabulary_map.unresolved_surfaces_without_meanings",
                    message="Vocabulary DB has surfaces without a meaning policy.",
                    next_action=(
                        "Reconciliar ou reconstruir o vocabulary DB pelo fluxo oficial de /mednotes:link; "
                        "não projetar aliases nem rodar body linker."
                    ),
                    required_inputs=["vocabulary_recovery", "vocabulary_semantic_ingestion"],
                )
                | {"surface_count": unresolved_surface_count}
            )
        for _meaning_id, label in conn.execute(
            "SELECT id, label FROM meanings WHERE status = 'active' AND atomic_status = 'duplicate_candidate' ORDER BY id"
        ).fetchall():
            result.issues.append(
                _issue(
                    severity="human_decision",
                    code="vocabulary_map.duplicate_meaning",
                    message=f"Meaning requires duplicate/merge review: {label}",
                    next_action="Criar plano de merge preservando provenance.",
                    required_inputs=["note_merge_decision"],
                )
            )
        for meaning_id, label, note_path in conn.execute(
            """
            SELECT m.id, m.label, COALESCE(n.path, '')
            FROM meanings m
            LEFT JOIN meaning_note_links l
              ON l.meaning_id = m.id
             AND l.role = 'canonical'
             AND l.status = 'active'
            LEFT JOIN notes n ON n.id = l.note_id
            WHERE m.status = 'active'
              AND m.atomic_status = 'suspected_non_atomic'
            ORDER BY m.id, n.path
            """
        ).fetchall():
            issue = _issue(
                severity="human_decision",
                code="vocabulary_map.non_atomic_note",
                message=f"Meaning may not be atomic: {label}",
                note_path=str(note_path),
                next_action="Separar ou reescrever a nota antes de linkar automaticamente.",
                required_inputs=["atomicity_decision"],
            )
            issue["meaning_id"] = str(meaning_id)
            issue["label"] = str(label)
            result.issues.append(issue)
        for path, alias_text, normalized_surface in conn.execute(
            """
            SELECT n.path, c.alias_text, c.normalized_surface
            FROM yaml_alias_claims c
            JOIN notes n ON n.id = c.note_id
            WHERE c.claim_status = 'conflicting_alias'
            ORDER BY n.path, c.normalized_surface
            """
        ).fetchall():
            result.issues.append(
                _issue(
                    severity="human_decision",
                    code="vocabulary_map.conflicting_alias",
                    message=f"YAML alias conflicts with vocabulary meaning: {alias_text}",
                    note_path=str(path),
                    surface=str(normalized_surface),
                    next_action="Resolver alias conflitante no DB antes de projetar YAML/linkar corpo.",
                    required_inputs=["alias_conflict_decision"],
                )
            )
        for normalized_surface, display_text in conn.execute(
            """
            SELECT s.normalized_surface, s.best_display_text
            FROM surfaces s
            JOIN surface_meaning_policy p ON p.surface_id = s.id
            GROUP BY s.id
            HAVING
              SUM(CASE WHEN p.link_policy = 'direct' THEN 1 ELSE 0 END) > 0
              AND (
                COUNT(DISTINCT p.meaning_id) > 1
                OR MAX(s.intrinsically_ambiguous) = 1
              )
            ORDER BY s.normalized_surface
            """
        ).fetchall():
            decision = decision_for_code(
                "vocabulary_map.direct_policy_on_ambiguous_surface",
                phase="vocabulary_map_diagnosis",
                public_summary="Termo ambíguo tratado de forma contextual.",
                developer_summary="Direct alias on ambiguous surface downgraded before linking.",
                next_action="Continuar sem link direto automático para esta superfície.",
            )
            issue = _issue(
                severity="warning",
                code=decision.reason_code,
                message=decision.public_summary,
                surface=str(normalized_surface),
                next_action=decision.next_action,
                required_inputs=[],
            )
            issue["decision_summary"] = decision.decision_summary()
            result.issues.append(
                issue
                | {
                    "display_text": str(display_text),
                }
            )
    result.map_hash = vocabulary_map_hash(db_path)
    return result
