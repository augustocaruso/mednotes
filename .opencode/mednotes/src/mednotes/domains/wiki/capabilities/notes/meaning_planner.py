"""Deterministic planner for triage-note-plan.v2 meaning work items."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.notes.note_plan import (
    PLANNED_MEANING_ACTION,
    TRIAGE_NOTE_PLAN_V2_SCHEMA,
    normalize_triage_note_plan_v2,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import meaning_id_for
from mednotes.domains.wiki.common import SUBAGENT_PLAN_SCHEMA, ValidationError
from mednotes.domains.wiki.config import MedConfig
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    RejectedAutomation,
    WorkflowDecision,
    attach_human_decision_packet,
)
from mednotes.kernel.base import JsonObject


@dataclass(frozen=True)
class MeaningClaim:
    label: str
    scope: str
    boundaries: tuple[str, ...]
    kind: str
    evidence_summary: str
    id: str = ""


@dataclass(frozen=True)
class MeaningWorkItem:
    work_id: str
    action: str
    target_kind: str
    raw_file: str
    note_plan_item_id: str
    meaning_claim: MeaningClaim
    target_path: str = ""


def plan_meaning_work_items(
    config: MedConfig,
    note_plan: dict[str, Any],
    *,
    raw_file: Path,
    temp_root: Path,
    agent: str = "med-knowledge-architect",
) -> dict[str, Any]:
    try:
        normalized = (
            normalize_triage_note_plan_v2(note_plan, raw_file)
            if note_plan.get("schema") == TRIAGE_NOTE_PLAN_V2_SCHEMA
            else note_plan
        )
    except ValidationError as exc:
        return _blocked_payload(
            blocked_reason="meaning_claim_invalid",
            message=str(exc),
            raw_file=raw_file,
            agent=agent,
        )
    if normalized.get("schema") != TRIAGE_NOTE_PLAN_V2_SCHEMA:
        return _blocked_payload(
            blocked_reason="note_plan_v2_required",
            message="meaning planner requires triage-note-plan.v2",
            raw_file=raw_file,
            agent=agent,
        )

    work_items: list[dict[str, Any]] = []
    blocked_items: list[JsonObject] = []
    for index, item in enumerate(normalized.get("items", []), start=1):
        if item.get("action") != PLANNED_MEANING_ACTION:
            continue
        try:
            claim = _meaning_claim(item.get("meaning_claim"))
        except ValidationError as exc:
            blocked_items.append(
                _blocked_item(
                    raw_file=raw_file,
                    note_plan_item_id=str(item.get("id") or f"M{index:03d}"),
                    blocked_reason="meaning_claim_invalid",
                    message=str(exc),
                )
            )
            continue
        matches = _canonical_matches(config, _claim_id(claim))
        if len(matches) > 1:
            blocked_item = {
                **_blocked_item(
                    raw_file=raw_file,
                    note_plan_item_id=str(item.get("id") or f"M{index:03d}"),
                    blocked_reason="human_decision_required.ambiguous_canonical_target",
                    message="More than one active canonical note is linked to this meaning_id.",
                ),
                "meaning_claim": asdict(claim),
                "candidate_targets": matches,
            }
            blocked_items.append(
                attach_human_decision_packet(
                    blocked_item,
                    packet=_ambiguous_canonical_target_packet(
                        claim=claim,
                        matches=matches,
                    ),
                )
            )
            continue
        if matches:
            action = "rewrite_existing_note"
            target_kind = "existing_wiki_note"
            target_path = str(config.wiki_dir / matches[0]["path"])
        else:
            action = "create_new_note"
            target_kind = "new_wiki_note"
            target_path = ""
        work_item = MeaningWorkItem(
            work_id=f"meaning-{index:03d}-{_slug(claim.label)}",
            action=action,
            target_kind=target_kind,
            raw_file=str(raw_file),
            note_plan_item_id=str(item.get("id") or f"M{index:03d}"),
            meaning_claim=claim,
            target_path=target_path,
        )
        payload = asdict(work_item)
        payload["agent"] = agent
        payload["item_type"] = "meaning_work_item"
        payload["launchable"] = True
        payload["owner_key"] = target_path or f"meaning:{_claim_id(claim)}"
        payload["temp_dir"] = str(temp_root / payload["work_id"])
        payload["temp_output"] = str(temp_root / payload["work_id"] / f"{_slug(claim.label)}.md")
        work_items.append(payload)

    status = "blocked" if blocked_items else "ready"
    return {
        "schema": SUBAGENT_PLAN_SCHEMA,
        "phase": "meaning-planner",
        "agent": agent,
        "status": status,
        "blocked_reason": "preconditions_failed" if blocked_items else "",
        "raw_file": str(raw_file),
        "item_count": len(work_items),
        "blocked_item_count": len(blocked_items),
        "work_items": work_items,
        "blocked_items": blocked_items,
        "next_action": "" if not blocked_items else "Corrigir meaning_claim ou escolher alvo canônico antes do architect.",
    }


def _meaning_claim(value: Any) -> MeaningClaim:
    if not isinstance(value, dict):
        raise ValidationError("meaning_claim missing")
    missing = [
        key
        for key in ("label", "scope", "boundaries", "kind", "evidence_summary")
        if key != "boundaries" and not str(value.get(key) or "").strip()
    ]
    boundaries = value.get("boundaries")
    if not isinstance(boundaries, list):
        missing.append("boundaries")
    if missing:
        raise ValidationError(f"meaning_claim missing required fields: {', '.join(missing)}")
    assert isinstance(boundaries, list)
    boundaries_list = boundaries
    return MeaningClaim(
        label=str(value["label"]).strip(),
        scope=str(value["scope"]).strip(),
        boundaries=tuple(str(item).strip() for item in boundaries_list if str(item).strip()),
        kind=str(value["kind"]).strip(),
        evidence_summary=str(value["evidence_summary"]).strip(),
        id=str(value.get("id") or value.get("meaning_id") or "").strip(),
    )


def _claim_id(claim: MeaningClaim) -> str:
    return claim.id or meaning_id_for(claim.label)


def _canonical_matches(config: MedConfig, meaning_id: str) -> list[dict[str, str]]:
    db_path = config.vocabulary_db_path
    if db_path is None or not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT n.path, n.title, l.confidence
            FROM meaning_note_links l
            JOIN meanings m ON m.id = l.meaning_id AND m.status = 'active'
            JOIN notes n ON n.id = l.note_id AND n.status = 'active'
            WHERE l.meaning_id = ?
              AND l.role = 'canonical'
              AND l.status = 'active'
            ORDER BY l.confidence DESC, n.path ASC
            """,
            (meaning_id,),
        ).fetchall()
    return [
        {
            "path": str(row["path"]),
            "title": str(row["title"]),
            "confidence": str(row["confidence"]),
        }
        for row in rows
    ]


def _ambiguous_canonical_target_packet(
    *,
    claim: MeaningClaim,
    matches: list[dict[str, str]],
) -> dict[str, Any]:
    options = [
        {
            "id": f"use_{idx}",
            "label": match["path"],
            "value": match["path"],
            "consequence": "O architect reescreve esse alvo canônico com o delta do raw chat.",
        }
        for idx, match in enumerate(matches, start=1)
    ]
    decision = WorkflowDecision(
        kind="ask_human",
        phase="meaning-planner",
        reason_code="human_decision_required.ambiguous_canonical_target",
        public_summary=f"Qual nota canônica deve receber '{claim.label}'?",
        developer_summary="More than one active canonical note is linked to this meaning_id.",
        evidence=[
            DecisionEvidence(
                summary=f"'{claim.label}' possui múltiplos alvos canônicos ativos.",
                technical_code="human_decision_required.ambiguous_canonical_target",
                source="meaning-planner",
                candidates=[{"meaning_claim": asdict(claim), "candidate_targets": matches}],
                risk="Escolha automática pode reescrever a nota canônica errada.",
            )
        ],
        rejected_automations=[
            RejectedAutomation(
                kind="auto_fix",
                reason_code="ambiguous_canonical_target",
                reason="Nao ha alvo canonico unico para corrigir automaticamente.",
            ),
            RejectedAutomation(
                kind="auto_defer",
                reason_code="blocks_architect",
                reason="Pular a escolha impediria cobertura correta do raw chat.",
            ),
            RejectedAutomation(
                kind="auto_plan",
                reason_code="plan_needs_canonical_target",
                reason="O plano precisa de um alvo canonico antes de lancar o architect.",
            ),
        ],
        next_action="Registrar a escolha e reexecutar plan-subagents --phase architect.",
        resume_action="Registrar a escolha e reexecutar plan-subagents --phase architect.",
        recommended_option_id=str(options[0]["id"]),
        options=options,
    )
    packet = decision.to_human_decision_packet()
    packet["kind"] = "ambiguous_canonical_target"
    packet["type"] = "ambiguous_canonical_target"
    return packet


def _blocked_item(*, raw_file: Path, note_plan_item_id: str, blocked_reason: str, message: str) -> JsonObject:
    return {
        "work_id": f"meaning-blocked-{_slug(note_plan_item_id)}",
        "item_type": "meaning_work_item",
        "raw_file": str(raw_file),
        "note_plan_item_id": note_plan_item_id,
        "blocked_reason": blocked_reason,
        "reason": message,
        "launchable": False,
        "write_policy": "no_temp_note",
    }


def _blocked_payload(*, blocked_reason: str, message: str, raw_file: Path, agent: str) -> JsonObject:
    item = _blocked_item(raw_file=raw_file, note_plan_item_id="", blocked_reason=blocked_reason, message=message)
    return {
        "schema": SUBAGENT_PLAN_SCHEMA,
        "phase": "meaning-planner",
        "agent": agent,
        "status": "blocked",
        "blocked_reason": blocked_reason,
        "raw_file": str(raw_file),
        "item_count": 0,
        "blocked_item_count": 1,
        "work_items": [],
        "blocked_items": [item],
        "next_action": "Corrigir triage-note-plan.v2 antes de lançar architect.",
    }


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", ascii_text).strip("-._").lower() or "meaning"
