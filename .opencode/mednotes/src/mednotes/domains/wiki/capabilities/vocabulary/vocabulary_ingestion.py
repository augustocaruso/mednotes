"""Apply med-link-graph-curator semantic ingestion items to the vocabulary DB."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import infer_title, split_frontmatter
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import (
    initialize_vocabulary_db,
    meaning_id_for,
    note_content_hash,
    upsert_meaning,
    upsert_note,
    upsert_policy,
    upsert_surface,
)
from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.contracts.vocabulary_ingestion import (
    INGESTION_RECEIPT_SCHEMA,
    INGESTION_SCHEMA,
    AtomicityBodySizeStats,
    AtomicityDeferredWorkDecision,
    AtomicityDeferredWorkEvaluation,
    AtomicityDeferredWorkPreflight,
    AtomicitySemanticSignal,
    SemanticAlias,
    SemanticDeferredWorkItem,
    SemanticIngestionIdentity,
    SemanticIngestionItem,
    SemanticPrimaryMeaning,
)
from mednotes.domains.wiki.contracts.workflow_guardrails import error_context
from mednotes.kernel.base import JsonObject, JsonObjectAdapter

__all__ = ["INGESTION_RECEIPT_SCHEMA", "INGESTION_SCHEMA", "apply_semantic_ingestion"]

ALLOWED_ATOMIC_STATUSES = {"atomic", "suspected_non_atomic", "duplicate_candidate", "unknown"}
ATOMICITY_DEFERRED_REASONS = {"non_atomic_note", "one_note_multiple_meanings"}
ATOMICITY_EVIDENCE_WEIGHTS = {
    "multiple_canonical_entities": 0.30,
    "different_entity_types": 0.25,
    "independent_definition_blocks": 0.20,
    "independent_management_blocks": 0.20,
    "independent_pathophysiology_blocks": 0.15,
    "separable_sections": 0.15,
    "linker_ambiguity": 0.15,
}
MIN_ATOMICITY_CHILD_BODY_CHARS = 240


def _bounded_float(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        parsed = default
    else:
        try:
            parsed = float(value)
        except ValueError:
            parsed = default
    return max(0.0, min(1.0, parsed))


def _body_char_count_from_text(text: str) -> int:
    _frontmatter, body = split_frontmatter(text)
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return len("\n".join(lines).strip())


def _body_char_count(path: Path) -> int:
    try:
        return _body_char_count_from_text(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def _body_size_stats(conn: sqlite3.Connection, *, current_note_path: Path) -> AtomicityBodySizeStats:
    counts: list[int] = []
    seen: set[str] = set()
    for (path_text,) in conn.execute("SELECT path FROM notes WHERE status = 'active'").fetchall():
        path = Path(str(path_text))
        if str(path) in seen:
            continue
        seen.add(str(path))
        count = _body_char_count(path)
        if count:
            counts.append(count)
    if str(current_note_path) not in seen:
        count = _body_char_count(current_note_path)
        if count:
            counts.append(count)
    if not counts:
        return AtomicityBodySizeStats(min_child_body_chars=MIN_ATOMICITY_CHILD_BODY_CHARS)
    mean = sum(counts) / len(counts)
    variance = sum((count - mean) ** 2 for count in counts) / len(counts)
    stddev = math.sqrt(variance)
    current_count = _body_char_count(current_note_path)
    threshold = mean + stddev
    return AtomicityBodySizeStats(
        sample_count=len(counts),
        mean_body_chars=mean,
        stddev_body_chars=stddev,
        long_note_threshold_chars=threshold,
        current_body_chars=current_count,
        current_above_one_stddev=current_count > threshold,
        min_child_body_chars=max(MIN_ATOMICITY_CHILD_BODY_CHARS, int(mean * 0.25)),
    )


def _atomicity_signal_score(signal: AtomicitySemanticSignal) -> tuple[float, list[str], int, set[str]]:
    evidence = [item for item in signal.evidence if item]
    concepts = signal.concepts
    concept_types = {concept.semantic_type for concept in concepts}
    weighted_score = sum(ATOMICITY_EVIDENCE_WEIGHTS.get(item, 0.0) for item in set(evidence))
    if len(concepts) >= 2:
        weighted_score += ATOMICITY_EVIDENCE_WEIGHTS["multiple_canonical_entities"]
        if len({item for item in concept_types if item}) >= 2:
            weighted_score += ATOMICITY_EVIDENCE_WEIGHTS["different_entity_types"]
    explicit_score = _bounded_float(signal.score)
    return min(1.0, max(weighted_score, explicit_score)), evidence, len(concepts), concept_types


def _child_body_counts(signal: AtomicitySemanticSignal) -> list[int]:
    counts: list[int] = []
    for child in signal.child_note_estimates:
        value = child.first_body_count()
        if value is not None:
            counts.append(max(0, value))
    return counts


def _evaluate_atomicity_deferred_work_item(
    *,
    raw_work: SemanticDeferredWorkItem,
    body_size_stats: AtomicityBodySizeStats,
) -> AtomicityDeferredWorkEvaluation:
    signal = raw_work.semantic_signal
    if signal is None:
        return AtomicityDeferredWorkEvaluation(
            status="blocked",
            blocked_reason="semantic_ingestion.atomicity_signal_required",
            decision="human_decision_required",
            message="Atomicity deferred work requires semantic_signal evidence from the note body.",
        )
    score, evidence, concept_count, _concept_types = _atomicity_signal_score(signal)
    if not evidence or concept_count < 2:
        return AtomicityDeferredWorkEvaluation(
            status="blocked",
            blocked_reason="semantic_ingestion.atomicity_signal_required",
            decision="human_decision_required",
            message="semantic_signal must include audit evidence and at least two developed concepts.",
            semantic_score=score,
            evidence=evidence,
            concept_count=concept_count,
        )
    relationship_score = _bounded_float(signal.relationship_score)
    explicit_fragment_risk = signal.fragment_risk.strip().casefold()
    child_counts = _child_body_counts(signal)
    min_child_chars = body_size_stats.min_child_body_chars or MIN_ATOMICITY_CHILD_BODY_CHARS
    child_fragment_risk = bool(child_counts and min(child_counts) < min_child_chars)
    fragment_risk = "high" if explicit_fragment_risk == "high" or child_fragment_risk else explicit_fragment_risk or "unknown"
    if relationship_score >= 0.75:
        decision = "relationship_note_valid"
    elif score >= 0.75 and fragment_risk != "high":
        decision = "split_required"
    elif score >= 0.75:
        decision = "split_deferred_fragment_risk"
    elif score >= 0.45 or (body_size_stats.current_above_one_stddev and score >= 0.35):
        decision = "split_candidate"
    else:
        decision = "no_action"
    db_status = "pending" if decision == "split_required" else "cancelled"
    return AtomicityDeferredWorkEvaluation(
        status="ready",
        decision=decision,
        db_status=db_status,
        source="db_semantic_signal_gate",
        semantic_score=score,
        semantic_threshold=0.75,
        evidence=evidence,
        concept_count=concept_count,
        relationship_score=relationship_score,
        fragmentation_gate=JsonObjectAdapter.validate_python({
            "fragment_risk": fragment_risk,
            "child_body_char_counts": child_counts,
            "min_child_body_chars": min_child_chars,
        }),
        body_size_gate=body_size_stats.to_payload(),
    )


def _atomicity_deferred_work_preflight(
    *,
    db_path: Path,
    item: SemanticIngestionItem,
    note_path: Path,
    content_hash: str,
) -> AtomicityDeferredWorkPreflight:
    decisions: list[AtomicityDeferredWorkDecision] = []
    evaluations: dict[str, AtomicityDeferredWorkEvaluation] = {}
    atomicity_work_items = [
        raw_work
        for raw_work in item.deferred_work_items
        if raw_work.reason in ATOMICITY_DEFERRED_REASONS
    ]
    if not atomicity_work_items:
        return AtomicityDeferredWorkPreflight(status="ready")
    initialize_vocabulary_db(db_path)
    with sqlite3.connect(db_path) as conn:
        body_size_stats = _body_size_stats(conn, current_note_path=note_path)
    for raw_work in atomicity_work_items:
        work_id = raw_work.effective_work_id(fallback_stem=note_path.stem)
        evaluation = _evaluate_atomicity_deferred_work_item(raw_work=raw_work, body_size_stats=body_size_stats)
        if evaluation.status == "blocked":
            _mark_queue_status(db_path=db_path, note_path=note_path, content_hash=content_hash, status="blocked")
            next_action = (
                "Regenerar o note-semantic-ingestion.v1 com semantic_signal auditável no corpo da nota; "
                "o DB só cria split pendente quando a evidência passa o gate semântico e anti-fragmentação."
            )
            blocked_reason = evaluation.blocked_reason or "semantic_ingestion.atomicity_signal_required"
            return AtomicityDeferredWorkPreflight(
                status="blocked",
                blocked_reason=blocked_reason,
                note_path=str(note_path),
                content_hash=content_hash,
                work_id=work_id,
                atomicity_evaluation=evaluation,
                next_action=next_action,
                error_context=error_context(
                    phase="semantic_ingestion",
                    blocked_reason=blocked_reason,
                    root_cause=blocked_reason,
                    affected_artifact="deferred_work_items",
                    error_summary=evaluation.message or "Atomicity deferred work lacks auditable semantic signal.",
                    suggested_fix=next_action,
                    next_action=next_action,
                    retry_scope="single_curator_work_item",
                    affected_items=[str(note_path)],
                ),
            )
        evaluations[work_id] = evaluation
        decisions.append(
            AtomicityDeferredWorkDecision(
                work_id=work_id,
                decision=evaluation.decision or "human_decision_required",
                status=evaluation.db_status or "blocked",
            )
        )
    return AtomicityDeferredWorkPreflight(status="ready", evaluations=evaluations, decisions=decisions)


def _mark_queue_status(
    *,
    db_path: Path,
    note_path: Path,
    content_hash: str,
    status: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    if conn is not None:
        conn.execute(
            """
            UPDATE note_semantic_ingestion_queue
            SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE note_path = ? AND content_hash = ? AND status IN ('pending', 'claimed')
            """,
            (status, str(note_path), content_hash),
        )
        return
    initialize_vocabulary_db(db_path)
    with sqlite3.connect(db_path) as owned_conn:
        _mark_queue_status(db_path=db_path, note_path=note_path, content_hash=content_hash, status=status, conn=owned_conn)


def _format_validation_location(location: tuple[object, ...]) -> str:
    path = ""
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path = f"{path}.{part}" if path else str(part)
    return path or "$"


def _semantic_ingestion_contract_error(exc: PydanticValidationError) -> str:
    details = "; ".join(
        f"{_format_validation_location(tuple(error.get('loc', ()) or ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors()
    )
    return f"semantic ingestion contract invalid: {details}"


def _semantic_ingestion_identity(value: object) -> SemanticIngestionIdentity:
    try:
        return SemanticIngestionIdentity.model_validate(value)
    except PydanticValidationError:
        return SemanticIngestionIdentity()


def _semantic_ingestion_item(value: object, *, db_path: Path, conn: sqlite3.Connection | None) -> SemanticIngestionItem:
    identity = _semantic_ingestion_identity(value)
    try:
        return SemanticIngestionItem.model_validate(value)
    except PydanticValidationError as exc:
        if identity.note_path is not None and identity.content_hash:
            _mark_queue_status(
                db_path=db_path,
                note_path=identity.note_path,
                content_hash=identity.content_hash,
                status="blocked",
                conn=conn,
            )
        raise ValidationError(_semantic_ingestion_contract_error(exc)) from exc


def apply_semantic_ingestion(
    *,
    db_path: Path,
    item: object,
    require_contract: bool = True,
    conn: sqlite3.Connection | None = None,
) -> JsonObject:
    del require_contract  # Kept for older callers; validation is no longer optional.
    typed_item = _semantic_ingestion_item(item, db_path=db_path, conn=conn)
    note_path = typed_item.note_path
    expected_hash = typed_item.content_hash
    primary = typed_item.primary_meaning
    try:
        return _apply_semantic_ingestion_unchecked(db_path=db_path, item=typed_item, conn=conn)
    except sqlite3.IntegrityError as exc:
        if str(note_path) and expected_hash:
            _mark_queue_status(db_path=db_path, note_path=note_path, content_hash=expected_hash, status="blocked", conn=conn)
        root_cause = "blocked.integrityerror"
        summary = _integrity_error_summary(exc)
        next_action = (
            "Resolve duplicate meaning merge or atomicity split via the official plan/apply "
            "workflow before retrying."
        )
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "semantic_ingestion.integrity_conflict",
            "note_path": str(note_path),
            "content_hash": expected_hash,
            "meaning_id": primary.id,
            "error_type": "IntegrityError",
            "error_summary": summary,
            "next_action": next_action,
            "diagnostic_context": {
                "root_cause_code": root_cause,
                "traceback_summary": summary,
                "recovery_command": next_action,
            },
            "error_context": error_context(
                phase="semantic_ingestion",
                blocked_reason="semantic_ingestion.integrity_conflict",
                root_cause=root_cause,
                affected_artifact="vocabulary_db",
                error_summary=summary,
                suggested_fix=next_action,
                next_action=next_action,
                retry_scope="semantic_ingestion_conflict_resolution",
                affected_items=[str(note_path)] if str(note_path) else None,
            ),
        }


def _integrity_error_summary(exc: sqlite3.IntegrityError) -> str:
    text = str(exc)
    if "UNIQUE constraint" in text or "CHECK constraint" in text or "FOREIGN KEY constraint" in text:
        text = "SQLite integrity constraint failed during semantic ingestion."
    return f"{exc.__class__.__name__}: {text}"[:1000]


def _apply_semantic_ingestion_unchecked(
    *,
    db_path: Path,
    item: SemanticIngestionItem,
    conn: sqlite3.Connection | None = None,
) -> JsonObject:
    note_path = item.note_path
    if not note_path.is_file():
        raise ValidationError(f"semantic ingestion note_path not found: {note_path}")
    actual_hash = note_content_hash(note_path)
    expected_hash = item.content_hash
    if expected_hash != actual_hash:
        _mark_queue_status(db_path=db_path, note_path=note_path, content_hash=expected_hash, status="stale", conn=conn)
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "semantic_ingestion.stale_note_hash",
            "note_path": str(note_path),
            "expected_hash": item.content_hash,
            "actual_hash": actual_hash,
        }

    primary = item.primary_meaning
    meaning_id = primary.id or meaning_id_for(primary.label)
    atomic_status = primary.atomic_status or "atomic"
    if atomic_status not in ALLOWED_ATOMIC_STATUSES:
        _mark_queue_status(db_path=db_path, note_path=note_path, content_hash=actual_hash, status="blocked", conn=conn)
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "semantic_ingestion.invalid_atomic_status",
            "note_path": str(note_path),
            "content_hash": actual_hash,
            "meaning_id": meaning_id,
            "atomic_status": atomic_status,
            "allowed_atomic_statuses": sorted(ALLOWED_ATOMIC_STATUSES),
        }
    text = note_path.read_text(encoding="utf-8")
    atomicity_preflight = _atomicity_deferred_work_preflight(
        db_path=db_path,
        item=item,
        note_path=note_path,
        content_hash=actual_hash,
    )
    if atomicity_preflight.status == "blocked":
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            **atomicity_preflight.to_payload(),
        }
    atomicity_evaluations = atomicity_preflight.evaluations
    deferred_atomicity_decisions = atomicity_preflight.decisions

    if conn is None:
        initialize_vocabulary_db(db_path)
        with sqlite3.connect(db_path) as owned_conn:
            return _apply_semantic_ingestion_db_ops(
                conn=owned_conn,
                db_path=db_path,
                item=item,
                note_path=note_path,
                actual_hash=actual_hash,
                primary=primary,
                meaning_id=meaning_id,
                atomic_status=atomic_status,
                text=text,
                aliases=item.aliases,
                atomicity_evaluations=atomicity_evaluations,
                deferred_atomicity_decisions=deferred_atomicity_decisions,
            )
    return _apply_semantic_ingestion_db_ops(
        conn=conn,
        db_path=db_path,
        item=item,
        note_path=note_path,
        actual_hash=actual_hash,
        primary=primary,
        meaning_id=meaning_id,
        atomic_status=atomic_status,
        text=text,
        aliases=item.aliases,
        atomicity_evaluations=atomicity_evaluations,
        deferred_atomicity_decisions=deferred_atomicity_decisions,
    )


def _apply_semantic_ingestion_db_ops(
    *,
    conn: sqlite3.Connection,
    db_path: Path,
    item: SemanticIngestionItem,
    note_path: Path,
    actual_hash: str,
    primary: SemanticPrimaryMeaning,
    meaning_id: str,
    atomic_status: str,
    text: str,
    aliases: list[SemanticAlias],
    atomicity_evaluations: dict[str, AtomicityDeferredWorkEvaluation],
    deferred_atomicity_decisions: list[AtomicityDeferredWorkDecision],
) -> JsonObject:
    note_id = upsert_note(conn, path=note_path, title=infer_title(text, note_path), content_hash=actual_hash)
    existing_note_link = conn.execute(
        """
        SELECT l.meaning_id, m.label
        FROM meaning_note_links l
        LEFT JOIN meanings m ON m.id = l.meaning_id
        WHERE l.note_id = ? AND l.role = 'canonical' AND l.status = 'active'
        """,
        (note_id,),
    ).fetchone()
    if existing_note_link and str(existing_note_link[0]) != meaning_id:
        existing_meaning_label = existing_note_link[1] if isinstance(existing_note_link[1], str) else ""
        conn.execute(
            """
            UPDATE note_semantic_ingestion_queue
            SET status='blocked', updated_at=CURRENT_TIMESTAMP
            WHERE note_path = ? AND content_hash = ? AND status IN ('pending', 'claimed')
            """,
            (str(note_path), actual_hash),
        )
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "semantic_ingestion.meaning_note_conflict",
            "note_path": str(note_path),
            "content_hash": actual_hash,
            "existing_meaning_id": str(existing_note_link[0]),
            "existing_meaning_label": existing_meaning_label,
            "proposed_meaning_id": meaning_id,
            "proposed_meaning_label": primary.label,
        }
    upsert_meaning(
        conn,
        meaning_id=meaning_id,
        label=primary.label,
        semantic_type=primary.semantic_type or "medical_concept",
        atomic_status=atomic_status,
    )
    existing_canonical = conn.execute(
        """
        SELECT l.note_id, n.path
        FROM meaning_note_links l
        JOIN notes n ON n.id = l.note_id
        WHERE l.meaning_id = ? AND l.role = 'canonical' AND l.status = 'active'
        """,
        (meaning_id,),
    ).fetchone()
    idempotent = bool(existing_canonical and int(existing_canonical[0]) == int(note_id))
    if existing_canonical and not idempotent:
        conn.execute(
            """
            UPDATE note_semantic_ingestion_queue
            SET status='blocked', updated_at=CURRENT_TIMESTAMP
            WHERE note_path = ? AND content_hash = ? AND status IN ('pending', 'claimed')
            """,
            (str(note_path), actual_hash),
        )
        return {
            "schema": INGESTION_RECEIPT_SCHEMA,
            "status": "blocked",
            "blocked_reason": "semantic_ingestion.meaning_canonical_conflict",
            "note_path": str(note_path),
            "content_hash": actual_hash,
            "meaning_id": meaning_id,
            "existing_note_id": int(existing_canonical[0]),
            "existing_note_path": str(existing_canonical[1]),
            "proposed_note_id": int(note_id),
            "proposed_note_path": str(note_path),
        }
    conn.execute(
        """
        INSERT INTO meaning_note_links(meaning_id, note_id, role, status, confidence)
        VALUES (?, ?, 'canonical', 'active', ?)
        ON CONFLICT(meaning_id, note_id, role) DO UPDATE SET
          status='active',
          confidence=excluded.confidence,
          updated_at=CURRENT_TIMESTAMP
        """,
        (meaning_id, note_id, item.confidence),
    )
    applied_aliases = 0
    for alias in aliases:
        source = alias.source or item.source or "curator"
        if source not in {"curator", "yaml", "projection", "human", "llm", "system"}:
            source = "curator"
        surface_id = upsert_surface(
            conn,
            display_text=alias.text,
            intrinsically_ambiguous=alias.intrinsically_ambiguous,
            ambiguity_reason=", ".join(value for value in alias.ambiguous_with if value),
        )
        upsert_policy(
            conn,
            surface_id=surface_id,
            meaning_id=meaning_id,
            link_policy=alias.link_policy or "requires_context",
            display_text=alias.text,
            visible_in_yaml=alias.visible_in_yaml,
            source=source,
            confidence=item.confidence,
        )
        applied_aliases += 1

    for raw_work in item.deferred_work_items:
        work_id = raw_work.effective_work_id(fallback_stem=note_path.stem)
        atomicity_evaluation = atomicity_evaluations[work_id] if work_id in atomicity_evaluations else None
        payload = raw_work.to_payload()
        status = raw_work.status or "pending"
        if atomicity_evaluation is not None:
            payload["atomicity_decision"] = atomicity_evaluation.decision or "human_decision_required"
            payload["atomicity_evaluation"] = atomicity_evaluation.to_payload()
            status = atomicity_evaluation.db_status or "blocked"
        conn.execute(
            """
            INSERT INTO deferred_work_items(
              work_id, source_agent, assigned_agent, reason, note_path, content_hash, payload_json, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
              payload_json=excluded.payload_json,
              status=excluded.status,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                work_id,
                raw_work.source_agent or "med-link-graph-curator",
                raw_work.assigned_agent or "med-knowledge-architect",
                raw_work.reason or "deferred_link_graph_work",
                str(raw_work.effective_note_path(fallback=note_path)),
                raw_work.effective_content_hash(fallback=actual_hash),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                status,
            ),
        )
    conn.execute(
        """
        UPDATE note_semantic_ingestion_queue
        SET status='applied', updated_at=CURRENT_TIMESTAMP
        WHERE note_path = ? AND content_hash = ? AND status IN ('pending', 'claimed')
        """,
        (str(note_path), actual_hash),
    )

    return {
        "schema": INGESTION_RECEIPT_SCHEMA,
        "status": "applied",
        "note_path": str(note_path),
        "content_hash": actual_hash,
        "meaning_id": meaning_id,
        "applied_alias_count": applied_aliases,
        "idempotent": idempotent,
        "deferred_atomicity_decisions": [decision.to_payload() for decision in deferred_atomicity_decisions],
    }
