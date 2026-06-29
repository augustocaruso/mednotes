"""DB-backed body term linker diagnosis and application."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import cast

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.markdown_zones import protected_markdown_zones
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import (
    is_index_note_content,
    is_index_target,
    normalize_key,
    obsidian_target_name,
)
from mednotes.domains.wiki.capabilities.vocabulary.llm_disambiguation import (
    LinkDisambiguationRequiresOrchestrator,
    call_contextual_alias_disambiguator,
)
from mednotes.domains.wiki.performance import cooperative_cpu_yield
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue, contract_error

CONTEXTUAL_ALIAS_SCHEMA = "medical-notes-workbench.contextual-alias-disambiguation.v1"
DEFAULT_LLM_DISAMBIGUATION_MODEL = "antigravity/gemini-3.5-flash"
DEFAULT_LLM_TIMEOUT_SECONDS = 60
LLM_CONFIDENCE_THRESHOLD = 0.82
_GOTA_DISEASE_ALLOW_RE = re.compile(
    r"\b("
    r"crise de gota|gota aguda|gota cronica|gota tofacea|"
    r"artrite gotosa|podagra|hiperuricemia|urato|urico|tofo|tofos"
    r")\b"
)
_GOTA_COMMON_NOUN_DENY_RE = re.compile(
    r"\b("
    r"sinal da gota|sinal de gota|gota de orvalho|[0-9]+ gotas?|uma gota|gotas? em cada olho"
    r")\b"
)

ContextualAliasDisambiguator = Callable[[list[JsonObject]], object]


class _BodyLinkerApplyAction(ContractModel):
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)
    replacement: StrictStr
    term: StrictStr = ""
    raw: StrictStr = ""
    matched_text: StrictStr = ""
    target: StrictStr = ""
    old_target: StrictStr = ""
    new_target: StrictStr = ""
    display_text: StrictStr = ""
    source: StrictStr = ""
    occurrence_id: StrictStr = ""
    context_hash: StrictStr = ""
    reason_code: StrictStr = ""
    confidence: JsonValue = None


class _BodyLinkerApplyPlan(ContractModel):
    file: StrictStr = Field(min_length=1)
    changed: StrictBool = False
    insertions: list[_BodyLinkerApplyAction] = Field(default_factory=list)
    rewrites: list[_BodyLinkerApplyAction] = Field(default_factory=list)
    skipped: list[JsonObject] = Field(default_factory=list)
    index_updated: StrictBool = False
    index_entries: StrictInt = Field(default=0, ge=0)


class _BodyLinkerApplyFields(ContractModel):
    blocked: StrictBool = False
    plans: list[_BodyLinkerApplyPlan] = Field(default_factory=list)


class _ContextualAliasDecisionInput(ContractModel):
    occurrence_id: StrictStr = Field(min_length=1)
    action: StrictStr = Field(min_length=1)
    chosen_meaning_id: StrictStr = ""
    chosen_target: StrictStr = ""
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    reason_code: StrictStr = ""
    rationale_summary: StrictStr = ""


class _SafeContextualDecision(ContractModel):
    occurrence_id: StrictStr = Field(min_length=1)
    file: StrictStr = Field(min_length=1)
    surface: StrictStr = Field(min_length=1)
    matched_text: StrictStr = Field(min_length=1)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)
    context_hash: StrictStr = Field(min_length=1)
    candidate_targets: list[StrictStr] = Field(default_factory=list)
    action: StrictStr = Field(min_length=1)
    chosen_meaning_id: StrictStr = ""
    chosen_target: StrictStr = ""
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    reason_code: StrictStr = ""
    rationale_summary: StrictStr = ""
    safe_auto_apply: StrictBool = False
    rejected: StrictBool = False
    source: StrictStr = ""


class _ContextualAliasResponse(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    model: StrictStr = ""
    decisions: list[_ContextualAliasDecisionInput] = Field(default_factory=list)


@dataclass(frozen=True)
class BodyLinkCandidate:
    source_path: str
    surface: str
    matched_text: str
    target: str
    replacement: str
    start: int
    end: int
    link_policy: str
    meaning_count: int
    canonical_note_count: int
    intrinsically_ambiguous: bool
    in_protected_markdown_zone: bool
    stale_snapshot: bool = False
    occurrence_id: str = ""
    meaning_id: str = ""
    context_hash: str = ""
    decision_action: str = ""
    confidence: float = 0.0
    reason_code: str = ""
    rationale_summary: str = ""
    source: str = "vocabulary_db"

    @property
    def automatic(self) -> bool:
        if self.link_policy == "requires_context":
            return (
                self.decision_action == "link"
                and self.confidence >= LLM_CONFIDENCE_THRESHOLD
                and bool(self.target)
                and not self.in_protected_markdown_zone
                and not self.stale_snapshot
            )
        return (
            self.link_policy == "direct"
            and self.meaning_count == 1
            and self.canonical_note_count == 1
            and not self.intrinsically_ambiguous
            and not self.in_protected_markdown_zone
            and not self.stale_snapshot
        )


@dataclass
class BodyLinkDiagnosis:
    wiki_dir: Path
    db_path: Path
    candidates: list[BodyLinkCandidate] = field(default_factory=list)
    rewrites: list[BodyLinkCandidate] = field(default_factory=list)
    skipped: list[JsonObject] = field(default_factory=list)
    contextual_alias_disambiguation: JsonObject = field(default_factory=dict)
    skipped_reason: str = ""

    def as_diagnosis_payload(self) -> JsonObject:
        automatic = [item for item in self.candidates if item.automatic]
        automatic_rewrites = [item for item in self.rewrites if item.automatic]
        blockers: list[JsonObject] = []
        contextual = self.contextual_alias_disambiguation or _empty_contextual_alias_payload(
            "skipped", "no_contextual_aliases"
        )
        if contextual.get("status") == "blocked":
            blockers.append(
                {
                    "code": "body_linker.contextual_alias_disambiguation_failed",
                    "message": str(contextual.get("blocked_reason") or "Falha na desambiguação contextual."),
                }
            )
        return _json_object(
            {
                "ok": not blockers,
                "blocked": bool(blockers),
                "phase": "body_term_linker_diagnosis",
                "status": "blocked" if blockers else "diagnosis_ready",
                "blocked_reason": "contextual_alias_disambiguation_failed" if blockers else "",
                "next_action": "Resolver decisões contextuais pela orquestração oficial de agente; se não houver orquestrador, pular aliases contextuais por segurança."
                if blockers
                else "",
                "body_linker_mode": "vocabulary_db",
                "vocabulary_db_path": str(self.db_path),
                "vocabulary_count": len(_load_policies(self.db_path)) if self.db_path.exists() else 0,
                "files_scanned": len({item.source_path for item in [*self.candidates, *self.rewrites]}),
                "files_changed": 0,
                "links_planned": len(automatic),
                "links_rewritten": len(automatic_rewrites),
                "contextual_alias_disambiguation": contextual,
                "blocker_count": len(blockers),
                "blockers": blockers,
                "plans": _plans_from_candidates(automatic, automatic_rewrites, self.skipped),
            }
        )

    def as_linker_payload(self, *, dry_run: bool) -> JsonObject:
        automatic = [item for item in self.candidates if item.automatic]
        automatic_rewrites = [item for item in self.rewrites if item.automatic]
        blockers: list[JsonObject] = []
        contextual = self.contextual_alias_disambiguation or _empty_contextual_alias_payload(
            "skipped", "no_contextual_aliases"
        )
        if contextual.get("status") == "blocked":
            blockers.append(
                {
                    "code": "body_linker.contextual_alias_disambiguation_failed",
                    "message": str(contextual.get("blocked_reason") or "Falha na desambiguação contextual."),
                }
            )
        return _json_object(
            {
                "ok": not blockers,
                "blocked": bool(blockers),
                "dry_run": dry_run,
                "phase": "run_linker_dry_run" if dry_run else "run_linker_apply",
                "status": "blocked" if blockers else "preview_ready" if dry_run else "completed",
                "blocked_reason": "contextual_alias_disambiguation_failed" if blockers else "",
                "next_action": "Resolver decisões contextuais pela orquestração oficial de agente; se não houver orquestrador, pular aliases contextuais por segurança."
                if blockers
                else "",
                "body_linker_mode": "vocabulary_db",
                "vocabulary_db_path": str(self.db_path),
                "vocabulary_count": len(_load_policies(self.db_path)) if self.db_path.exists() else 0,
                "files_scanned": len({item.source_path for item in [*self.candidates, *self.rewrites]}),
                "files_changed": len({item.source_path for item in [*automatic, *automatic_rewrites]})
                if not dry_run
                else 0,
                "links_planned": len(automatic),
                "links_rewritten": len(automatic_rewrites),
                "contextual_alias_disambiguation": contextual,
                "blocker_count": len(blockers),
                "blockers": blockers,
                "plans": _plans_from_candidates(automatic, automatic_rewrites, self.skipped),
            }
        )


@dataclass(frozen=True)
class SurfacePolicy:
    surface: str
    normalized_surface: str
    target: str
    link_policy: str
    meaning_count: int
    canonical_note_count: int
    intrinsically_ambiguous: bool
    meaning_id: str = ""
    meaning_label: str = ""
    target_path: str = ""


@dataclass(frozen=True)
class ContextualOccurrence:
    source_path: str
    surface: str
    normalized_surface: str
    matched_text: str
    start: int
    end: int
    context_preview: str
    context_hash: str
    occurrence_id: str
    candidates: tuple[SurfacePolicy, ...]
    in_table: bool


@dataclass(frozen=True)
class SurfaceSearchTerm:
    normalized_surface: str
    display: str
    policies: tuple[SurfacePolicy, ...]
    contextual: bool
    pattern: str


@dataclass(frozen=True)
class SurfaceMatch:
    start: int
    end: int
    matched_text: str
    term: SurfaceSearchTerm


@dataclass
class _AhoNode:
    transitions: dict[str, int] = field(default_factory=dict)
    failure: int = 0
    outputs: list[SurfaceSearchTerm] = field(default_factory=list)


def diagnose_body_links(
    *,
    wiki_dir: Path,
    db_path: Path,
    llm_mode: str = "off",
    llm_model: str | None = None,
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    llm_disambiguator: Callable[..., object] | None = None,
) -> BodyLinkDiagnosis:
    if llm_mode not in {"auto", "off", "required"}:
        raise ValueError("llm_mode must be one of: auto, off, required")
    policies = _load_policies(db_path)
    diagnosis = BodyLinkDiagnosis(wiki_dir=wiki_dir, db_path=db_path)
    contextual_occurrences: list[ContextualOccurrence] = []
    policies_by_surface: dict[str, list[SurfacePolicy]] = {}
    for policy in policies:
        policies_by_surface.setdefault(policy.normalized_surface, []).append(policy)
    search_terms = _search_terms_from_policies(policies_by_surface)
    surface_automaton = _SurfaceAutomaton(search_terms) if search_terms else None
    for index, path in enumerate(iter_notes(wiki_dir) if wiki_dir.exists() else [], start=1):
        cooperative_cpu_yield(index)
        text = path.read_text(encoding="utf-8")
        if is_index_target(path.stem) or is_index_note_content(text):
            continue
        diagnosis.rewrites.extend(_rewrite_candidates(path, text, policies))
        protected = _protected_spans(text)
        used: list[tuple[int, int]] = []
        source_title = normalize_key(path.stem)
        matches = surface_automaton.finditer(text) if surface_automaton is not None else []
        for match in matches:
            start, end = match.start, match.end
            usable = _usable_policies_for_source(match.term.policies, source_title)
            if not usable:
                continue
            protected_match = _inside_spans(start, end, protected) or _inside_spans(start, end, used)
            if protected_match:
                continue
            display = _best_policy_display(usable)
            guardrail_skip = _surface_context_guardrail_skip(
                path=path,
                text=text,
                normalized_surface=match.term.normalized_surface,
                matched_text=match.matched_text,
                start=start,
                end=end,
            )
            if guardrail_skip is not None:
                diagnosis.skipped.append(guardrail_skip)
                used.append((start, end))
                continue
            if _requires_context(usable):
                occurrence = _contextual_occurrence(
                    path=path,
                    text=text,
                    surface=display,
                    normalized_surface=match.term.normalized_surface,
                    matched_text=match.matched_text,
                    start=start,
                    end=end,
                    candidates=usable,
                )
                contextual_occurrences.append(occurrence)
                diagnosis.candidates.append(_placeholder_contextual_candidate(occurrence))
                used.append((start, end))
                continue
            policy = usable[0]
            candidate = BodyLinkCandidate(
                source_path=str(path),
                surface=display,
                matched_text=match.matched_text,
                target=policy.target,
                replacement=_replacement(policy.target, match.matched_text, in_table=_is_table_match(text, start)),
                start=start,
                end=end,
                link_policy=policy.link_policy,
                meaning_count=policy.meaning_count,
                canonical_note_count=policy.canonical_note_count,
                intrinsically_ambiguous=policy.intrinsically_ambiguous,
                in_protected_markdown_zone=False,
                meaning_id=policy.meaning_id,
            )
            diagnosis.candidates.append(candidate)
            used.append((start, end))
    _resolve_contextual_occurrences(
        diagnosis,
        contextual_occurrences,
        llm_mode=llm_mode,
        llm_model=llm_model or DEFAULT_LLM_DISAMBIGUATION_MODEL,
        llm_timeout=llm_timeout,
        llm_disambiguator=llm_disambiguator,
    )
    return diagnosis


def run_body_linker(
    *,
    wiki_dir: Path,
    db_path: Path,
    dry_run: bool,
    llm_mode: str = "off",
    llm_model: str | None = None,
    llm_timeout: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    llm_disambiguator: Callable[..., object] | None = None,
) -> JsonObject:
    diagnosis = diagnose_body_links(
        wiki_dir=wiki_dir,
        db_path=db_path,
        llm_mode=llm_mode if dry_run else "off",
        llm_model=llm_model,
        llm_timeout=llm_timeout,
        llm_disambiguator=llm_disambiguator,
    )
    payload = diagnosis.as_linker_payload(dry_run=dry_run)
    fields = _body_linker_apply_fields(payload)
    if not dry_run and not fields.blocked:
        payload = apply_body_linker_plan(wiki_dir=wiki_dir, body_linker_payload=payload)
    return payload


def _load_policies(db_path: Path) -> list[SurfacePolicy]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            WITH surface_counts AS (
              SELECT surface_id,
                     COUNT(DISTINCT meaning_id) AS meaning_count
              FROM surface_meaning_policy
              WHERE visible_in_yaml = 1
              GROUP BY surface_id
            ),
            canonical_counts AS (
              SELECT p.surface_id,
                     COUNT(DISTINCT l.note_id) AS canonical_note_count
              FROM surface_meaning_policy p
              JOIN meaning_note_links l ON l.meaning_id = p.meaning_id
               AND l.role = 'canonical'
               AND l.status = 'active'
              GROUP BY p.surface_id
            )
            SELECT p.display_text,
                   s.normalized_surface,
                   n.title,
                   p.link_policy,
                   COALESCE(sc.meaning_count, 0),
                   COALESCE(cc.canonical_note_count, 0),
                   s.intrinsically_ambiguous,
                   p.meaning_id,
                   m.label,
                   n.path
            FROM surface_meaning_policy p
            JOIN surfaces s ON s.id = p.surface_id
            JOIN meanings m ON m.id = p.meaning_id AND m.status = 'active'
            JOIN meaning_note_links l ON l.meaning_id = p.meaning_id
             AND l.role = 'canonical'
             AND l.status = 'active'
            JOIN notes n ON n.id = l.note_id AND n.status = 'active'
            LEFT JOIN surface_counts sc ON sc.surface_id = p.surface_id
            LEFT JOIN canonical_counts cc ON cc.surface_id = p.surface_id
            WHERE p.visible_in_yaml = 1
            ORDER BY length(s.normalized_surface) DESC, p.display_text
            """
        ).fetchall()
    policies: list[SurfacePolicy] = []
    seen: set[tuple[str, str, str]] = set()
    for (
        surface,
        normalized,
        target,
        policy,
        meaning_count,
        canonical_count,
        ambiguous,
        meaning_id,
        label,
        target_path,
    ) in rows:
        link_target = _obsidian_target_from_note_path(str(target_path), fallback=str(target))
        key = (str(normalized), link_target, str(meaning_id))
        if key in seen:
            continue
        seen.add(key)
        policies.append(
            SurfacePolicy(
                surface=str(surface),
                normalized_surface=str(normalized),
                target=link_target,
                link_policy=str(policy),
                meaning_count=int(meaning_count or 0),
                canonical_note_count=int(canonical_count or 0),
                intrinsically_ambiguous=bool(ambiguous),
                meaning_id=str(meaning_id),
                meaning_label=str(label),
                target_path=str(target_path),
            )
        )
    return policies


def _obsidian_target_from_note_path(note_path: str, *, fallback: str) -> str:
    normalized = note_path.replace("\\", "/").strip()
    if normalized:
        return obsidian_target_name(normalized)
    return fallback


def _plans_from_candidates(
    candidates: list[BodyLinkCandidate],
    rewrites: list[BodyLinkCandidate] | None = None,
    skipped: list[JsonObject] | None = None,
) -> list[JsonObject]:
    by_file: dict[str, list[BodyLinkCandidate]] = {}
    rewrites_by_file: dict[str, list[BodyLinkCandidate]] = {}
    skipped_by_file: dict[str, list[JsonObject]] = {}
    for candidate in candidates:
        by_file.setdefault(candidate.source_path, []).append(candidate)
    for rewrite in rewrites or []:
        rewrites_by_file.setdefault(rewrite.source_path, []).append(rewrite)
    for item in skipped or []:
        file = item.get("file")
        if isinstance(file, str):
            skipped_by_file.setdefault(file, []).append(item)
    files = sorted(set(by_file) | set(rewrites_by_file) | set(skipped_by_file))
    plans: list[JsonObject] = []
    for file in files:
        plans.append(
            _json_object(
                {
                    "file": file,
                    "changed": bool(by_file.get(file) or rewrites_by_file.get(file)),
                    "insertions": [
                        {
                            "term": item.surface,
                            "matched_text": item.matched_text,
                            "target": item.target,
                            "replacement": item.replacement,
                            "start": item.start,
                            "end": item.end,
                            "source": item.source,
                            "occurrence_id": item.occurrence_id,
                            "context_hash": item.context_hash,
                            "confidence": item.confidence,
                            "reason_code": item.reason_code,
                        }
                        for item in by_file.get(file, [])
                    ],
                    "rewrites": [
                        {
                            "raw": item.matched_text,
                            "old_target": item.matched_text.split("|", 1)[0],
                            "new_target": item.target,
                            "display_text": item.surface,
                            "replacement": item.replacement,
                            "start": item.start,
                            "end": item.end,
                            "source": item.source,
                        }
                        for item in rewrites_by_file.get(file, [])
                    ],
                    "skipped": [
                        _json_object({key: value for key, value in item.items() if key != "file"})
                        for item in skipped_by_file.get(file, [])
                    ],
                    "index_updated": False,
                    "index_entries": 0,
                }
            )
        )
    return plans


def _apply_candidates(candidates: list[BodyLinkCandidate]) -> None:
    by_file: dict[Path, list[BodyLinkCandidate]] = {}
    for candidate in candidates:
        by_file.setdefault(Path(candidate.source_path), []).append(candidate)
    for path, items in by_file.items():
        text = path.read_text(encoding="utf-8")
        updated = text
        for item in sorted(items, key=lambda candidate: candidate.start, reverse=True):
            updated = updated[: item.start] + item.replacement + updated[item.end :]
        if updated != text:
            atomic_write_text(path, updated)


def apply_body_linker_plan(*, wiki_dir: Path, body_linker_payload: JsonObject) -> JsonObject:
    payload = JsonObjectAdapter.validate_python(body_linker_payload)
    fields = _body_linker_apply_fields(payload)
    changed_files: set[str] = set()
    for plan in fields.plans:
        if not plan.changed:
            continue
        path = Path(plan.file)
        if not path.is_absolute():
            path = wiki_dir / path
        text = path.read_text(encoding="utf-8")
        updated = text
        actions = [*plan.insertions, *plan.rewrites]
        for action in sorted(actions, key=lambda item: item.start, reverse=True):
            start = action.start
            end = action.end
            replacement = action.replacement
            updated = updated[:start] + replacement + updated[end:]
        if updated != text:
            atomic_write_text(path, updated)
            changed_files.add(str(path))
    result = dict(payload)
    result["dry_run"] = False
    result["phase"] = "run_linker_apply"
    result["status"] = "completed" if not fields.blocked else "blocked"
    result["files_changed"] = len(changed_files)
    changed_file_values: list[JsonValue] = cast(list[JsonValue], sorted(changed_files))
    result["changed_files"] = changed_file_values
    result["returncode"] = 3 if fields.blocked else 0
    return JsonObjectAdapter.validate_python(result)


def _body_linker_apply_fields(payload: JsonObject) -> _BodyLinkerApplyFields:
    raw_fields: JsonObject = {}
    for name in ("blocked", "plans"):
        if name in payload:
            raw_fields[name] = payload[name]
    try:
        return _BodyLinkerApplyFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="body linker apply payload invalid") from exc


def _json_object(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return cast(JsonObject, payload)
    return JsonObjectAdapter.validate_python(payload)


def _empty_contextual_alias_payload(status: str, reason: str = "") -> JsonObject:
    return _json_object(
        {
            "schema": CONTEXTUAL_ALIAS_SCHEMA,
            "phase": "contextual_alias_disambiguation",
            "status": status,
            "mode": "off",
            "skipped_reason": reason,
            "candidate_count": 0,
            "decision_count": 0,
            "linked_count": 0,
            "deferred_count": 0,
            "no_link_count": 0,
            "rejected_count": 0,
            "llm_error": "",
            "decisions": [],
        }
    )


def _best_policy_display(policies: list[SurfacePolicy]) -> str:
    return max((policy.surface for policy in policies), key=lambda value: (len(normalize_key(value)), len(value)))


def _requires_context(policies: list[SurfacePolicy]) -> bool:
    if len({policy.meaning_id for policy in policies}) > 1:
        return True
    if len({normalize_key(policy.target) for policy in policies}) > 1:
        return True
    return any(
        policy.link_policy == "requires_context"
        or policy.intrinsically_ambiguous
        or policy.meaning_count != 1
        or policy.canonical_note_count != 1
        for policy in policies
    )


def _search_terms_for_path(path: Path, policies_by_surface: dict[str, list[SurfacePolicy]]) -> list[SurfaceSearchTerm]:
    terms: list[SurfaceSearchTerm] = []
    source_title = normalize_key(path.stem)
    for normalized_surface, surface_policies in policies_by_surface.items():
        usable = _usable_policies_for_source(surface_policies, source_title)
        if not usable:
            continue
        display = _best_policy_display(usable)
        pattern = _fold_for_search(display)
        if not pattern:
            continue
        terms.append(
            SurfaceSearchTerm(
                normalized_surface=normalized_surface,
                display=display,
                policies=tuple(usable),
                contextual=_requires_context(usable),
                pattern=pattern,
            )
        )
    return terms


def _search_terms_from_policies(policies_by_surface: dict[str, list[SurfacePolicy]]) -> list[SurfaceSearchTerm]:
    terms: list[SurfaceSearchTerm] = []
    for normalized_surface, surface_policies in policies_by_surface.items():
        display = _best_policy_display(surface_policies)
        pattern = _fold_for_search(display)
        if not pattern:
            continue
        terms.append(
            SurfaceSearchTerm(
                normalized_surface=normalized_surface,
                display=display,
                policies=tuple(surface_policies),
                contextual=_requires_context(surface_policies),
                pattern=pattern,
            )
        )
    return terms


def _usable_policies_for_source(policies: tuple[SurfacePolicy, ...] | list[SurfacePolicy], source_title: str) -> list[SurfacePolicy]:
    return [policy for policy in policies if normalize_key(policy.target) != source_title]


class _SurfaceAutomaton:
    def __init__(self, terms: list[SurfaceSearchTerm]):
        self._nodes = [_AhoNode()]
        for term in terms:
            self._add(term)
        self._build_failures()

    def _add(self, term: SurfaceSearchTerm) -> None:
        state = 0
        for char in term.pattern:
            if char not in self._nodes[state].transitions:
                self._nodes[state].transitions[char] = self._new_node()
            state = self._nodes[state].transitions[char]
        self._nodes[state].outputs.append(term)

    def _new_node(self) -> int:
        self._nodes.append(_AhoNode())
        return len(self._nodes) - 1

    def _build_failures(self) -> None:
        queue: deque[int] = deque()
        for next_state in self._nodes[0].transitions.values():
            self._nodes[next_state].failure = 0
            queue.append(next_state)

        while queue:
            state = queue.popleft()
            for char, next_state in self._nodes[state].transitions.items():
                queue.append(next_state)
                failure = self._nodes[state].failure
                while failure and char not in self._nodes[failure].transitions:
                    failure = self._nodes[failure].failure
                self._nodes[next_state].failure = self._nodes[failure].transitions.get(char, 0)
                self._nodes[next_state].outputs.extend(self._nodes[self._nodes[next_state].failure].outputs)

    def finditer(self, text: str) -> list[SurfaceMatch]:
        matches: list[SurfaceMatch] = []
        state = 0
        searchable = _fold_for_search(text)
        for index, char in enumerate(searchable):
            while state and char not in self._nodes[state].transitions:
                state = self._nodes[state].failure
            state = self._nodes[state].transitions.get(char, 0)
            for term in self._nodes[state].outputs:
                end = index + 1
                start = end - len(term.pattern)
                if start < 0 or not _has_term_boundaries(text, start, end):
                    continue
                matches.append(
                    SurfaceMatch(
                        start=start,
                        end=end,
                        matched_text=text[start:end],
                        term=term,
                    )
                )
        return sorted(
            matches,
            key=lambda match: (
                match.start,
                -(match.end - match.start),
                match.term.display.casefold(),
                match.term.normalized_surface,
            ),
        )


def _surface_matches(text: str, terms: list[SurfaceSearchTerm]) -> list[SurfaceMatch]:
    if not terms:
        return []
    return _SurfaceAutomaton(terms).finditer(text)


def _fold_for_search(value: str) -> str:
    lowered = value.lower()
    if len(lowered) == len(value):
        return lowered
    return "".join(_fold_char(char) for char in value)


def _fold_char(value: str) -> str:
    folded = value.lower()
    return folded if len(folded) == 1 else value


def _contextual_occurrence(
    *,
    path: Path,
    text: str,
    surface: str,
    normalized_surface: str,
    matched_text: str,
    start: int,
    end: int,
    candidates: list[SurfacePolicy],
) -> ContextualOccurrence:
    context_start, context_end = _line_bounds(text, start)
    context_preview = text[context_start:context_end]
    context_hash = "sha256:" + sha256(context_preview.encode("utf-8")).hexdigest()
    occurrence_key = json.dumps(
        {
            "path": str(path),
            "surface": normalized_surface,
            "matched_text": matched_text,
            "start": start,
            "end": end,
            "context_hash": context_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    occurrence_id = "ctx:" + sha256(occurrence_key.encode("utf-8")).hexdigest()
    return ContextualOccurrence(
        source_path=str(path),
        surface=surface,
        normalized_surface=normalized_surface,
        matched_text=matched_text,
        start=start,
        end=end,
        context_preview=context_preview,
        context_hash=context_hash,
        occurrence_id=occurrence_id,
        candidates=tuple(candidates),
        in_table=_is_table_match(text, start),
    )


def _surface_context_guardrail_skip(
    *,
    path: Path,
    text: str,
    normalized_surface: str,
    matched_text: str,
    start: int,
    end: int,
) -> JsonObject | None:
    if normalized_surface != "gota":
        return None
    context_start, context_end = _line_bounds(text, start)
    context = normalize_key(text[context_start:context_end])
    if _GOTA_DISEASE_ALLOW_RE.search(context):
        return None
    if not _GOTA_COMMON_NOUN_DENY_RE.search(context):
        return None
    context_hash = "sha256:" + sha256(text[context_start:context_end].encode("utf-8")).hexdigest()
    return _json_object(
        {
            "file": str(path),
            "term": "Gota",
            "matched_text": matched_text,
            "start": start,
            "end": end,
            "occurrence_id": "",
            "context_hash": context_hash,
            "action": "no_link",
            "reason_code": "surface_context_guardrail_no_link",
            "confidence": 1.0,
            "source": "surface_context_guardrail",
        }
    )


def _placeholder_contextual_candidate(occurrence: ContextualOccurrence) -> BodyLinkCandidate:
    first = occurrence.candidates[0]
    return BodyLinkCandidate(
        source_path=occurrence.source_path,
        surface=occurrence.surface,
        matched_text=occurrence.matched_text,
        target=first.target,
        replacement=_replacement(first.target, occurrence.matched_text, in_table=occurrence.in_table),
        start=occurrence.start,
        end=occurrence.end,
        link_policy="requires_context",
        meaning_count=first.meaning_count,
        canonical_note_count=first.canonical_note_count,
        intrinsically_ambiguous=True,
        in_protected_markdown_zone=False,
        occurrence_id=occurrence.occurrence_id,
        meaning_id=first.meaning_id,
        context_hash=occurrence.context_hash,
    )


def _request_from_occurrence(occurrence: ContextualOccurrence) -> JsonObject:
    return _json_object(
        {
            "occurrence_id": occurrence.occurrence_id,
            "file": occurrence.source_path,
            "surface": occurrence.surface,
            "normalized_surface": occurrence.normalized_surface,
            "matched_text": occurrence.matched_text,
            "start": occurrence.start,
            "end": occurrence.end,
            "context_preview": occurrence.context_preview,
            "context_hash": occurrence.context_hash,
            "candidates": [
                {
                    "meaning_id": candidate.meaning_id,
                    "meaning_label": candidate.meaning_label,
                    "target": candidate.target,
                    "target_path": candidate.target_path,
                    "link_policy": candidate.link_policy,
                }
                for candidate in occurrence.candidates
            ],
        }
    )


def _safe_decision_payload(
    *,
    occurrence: ContextualOccurrence,
    action: str,
    chosen: SurfacePolicy | None,
    confidence: float,
    reason_code: str,
    rationale_summary: str,
    rejected: bool = False,
) -> JsonObject:
    return _json_object(
        {
            "occurrence_id": occurrence.occurrence_id,
            "file": occurrence.source_path,
            "surface": occurrence.surface,
            "matched_text": occurrence.matched_text,
            "start": occurrence.start,
            "end": occurrence.end,
            "context_hash": occurrence.context_hash,
            "candidate_targets": [candidate.target for candidate in occurrence.candidates],
            "action": action,
            "chosen_meaning_id": chosen.meaning_id if chosen else "",
            "chosen_target": chosen.target if chosen else "",
            "confidence": confidence,
            "reason_code": reason_code,
            "rationale_summary": rationale_summary[:180],
            "safe_auto_apply": action == "link" and chosen is not None and confidence >= LLM_CONFIDENCE_THRESHOLD,
            "rejected": rejected,
        }
    )


def _skip_from_decision(decision: JsonObject) -> JsonObject:
    return _json_object(
        {
            "file": decision["file"],
            "term": decision["surface"],
            "matched_text": decision["matched_text"],
            "start": decision["start"],
            "end": decision["end"],
            "occurrence_id": decision["occurrence_id"],
            "context_hash": decision["context_hash"],
            "action": decision["action"],
            "reason_code": decision["reason_code"],
            "confidence": decision["confidence"],
            "source": "contextual_alias_disambiguation",
        }
    )


def _resolve_contextual_occurrences(
    diagnosis: BodyLinkDiagnosis,
    occurrences: list[ContextualOccurrence],
    *,
    llm_mode: str,
    llm_model: str,
    llm_timeout: int,
    llm_disambiguator: Callable[..., object] | None,
) -> None:
    if not occurrences:
        diagnosis.contextual_alias_disambiguation = _empty_contextual_alias_payload("skipped", "no_contextual_aliases")
        return
    deterministic_occurrences: list[ContextualOccurrence] = []
    unresolved_occurrences: list[ContextualOccurrence] = []
    for occurrence in occurrences:
        if _is_deterministic_contextual_occurrence(occurrence):
            deterministic_occurrences.append(occurrence)
        else:
            unresolved_occurrences.append(occurrence)
    resolved_decisions: list[JsonObject] = []
    linked_candidates: list[BodyLinkCandidate] = []
    for occurrence in deterministic_occurrences:
        chosen = occurrence.candidates[0]
        decision = _safe_decision_payload(
            occurrence=occurrence,
            action="link",
            chosen=chosen,
            confidence=1.0,
            reason_code="exact_canonical_surface",
            rationale_summary="Termo igual ao único alvo canônico; LLM não é necessário.",
        )
        decision["source"] = "deterministic_contextual_alias"
        resolved_decisions.append(decision)
        linked_candidates.append(
            _contextual_link_candidate(
                occurrence=occurrence,
                chosen=chosen,
                confidence=1.0,
                reason_code=str(decision["reason_code"]),
                rationale_summary=str(decision["rationale_summary"]),
                source="deterministic_contextual_alias",
            )
        )
    if deterministic_occurrences:
        deterministic_ids = {occurrence.occurrence_id for occurrence in deterministic_occurrences}
        diagnosis.candidates = [
            item
            for item in diagnosis.candidates
            if item.link_policy != "requires_context" or item.occurrence_id not in deterministic_ids
        ]
        diagnosis.candidates.extend(linked_candidates)
    if not unresolved_occurrences:
        diagnosis.contextual_alias_disambiguation = _contextual_summary(
            mode="deterministic",
            status="completed",
            decisions=resolved_decisions,
        )
        return
    if llm_mode == "off":
        decisions = [
            _safe_decision_payload(
                occurrence=occurrence,
                action="defer",
                chosen=None,
                confidence=0.0,
                reason_code="llm_disabled",
                rationale_summary="Desambiguação LLM desativada.",
            )
            for occurrence in unresolved_occurrences
        ]
        diagnosis.skipped.extend(_skip_from_decision(decision) for decision in decisions)
        diagnosis.contextual_alias_disambiguation = _contextual_summary(
            mode=llm_mode,
            status="skipped",
            decisions=[*resolved_decisions, *decisions],
            skipped_reason="llm_disabled",
        )
        return

    requests = [_request_from_occurrence(occurrence) for occurrence in unresolved_occurrences]
    provider = llm_disambiguator or call_contextual_alias_disambiguator
    try:
        raw = provider(requests, model=llm_model, timeout_seconds=llm_timeout)
    except LinkDisambiguationRequiresOrchestrator as exc:
        if llm_mode == "required":
            diagnosis.contextual_alias_disambiguation = _contextual_summary(
                mode=llm_mode,
                status="blocked",
                decisions=resolved_decisions,
                blocked_reason="orchestrator_required",
                llm_error=str(exc),
            )
            return
        decisions = [
            _safe_decision_payload(
                occurrence=occurrence,
                action="defer",
                chosen=None,
                confidence=0.0,
                reason_code="orchestrator_required",
                rationale_summary="Desambiguação contextual exige orquestração por agente; ocorrência pulada por segurança.",
            )
            for occurrence in unresolved_occurrences
        ]
        diagnosis.skipped.extend(_skip_from_decision(decision) for decision in decisions)
        diagnosis.contextual_alias_disambiguation = _contextual_summary(
            mode=llm_mode,
            status="skipped",
            decisions=[*resolved_decisions, *decisions],
            skipped_reason="orchestrator_required",
            llm_error=str(exc),
        )
        return
    except Exception as exc:  # pragma: no cover - exercised through required mode in integration
        if llm_mode == "required":
            diagnosis.contextual_alias_disambiguation = _contextual_summary(
                mode=llm_mode,
                status="blocked",
                decisions=[],
                blocked_reason="llm_disambiguation_failed",
                llm_error=str(exc),
            )
            return
        decisions = [
            _safe_decision_payload(
                occurrence=occurrence,
                action="defer",
                chosen=None,
                confidence=0.0,
                reason_code="llm_unavailable",
                rationale_summary="Gemini indisponível; ocorrência contextual pulada.",
            )
            for occurrence in unresolved_occurrences
        ]
        diagnosis.skipped.extend(_skip_from_decision(decision) for decision in decisions)
        diagnosis.contextual_alias_disambiguation = _contextual_summary(
            mode=llm_mode,
            status="skipped",
            decisions=[*resolved_decisions, *decisions],
            skipped_reason="llm_unavailable",
            llm_error=str(exc),
        )
        return

    try:
        decisions_by_id, response_payload = _decisions_by_occurrence(raw)
    except Exception as exc:
        if llm_mode == "required":
            diagnosis.contextual_alias_disambiguation = _contextual_summary(
                mode=llm_mode,
                status="blocked",
                decisions=[],
                blocked_reason="llm_disambiguation_invalid_response",
                llm_error=str(exc),
            )
            return
        decisions = [
            _safe_decision_payload(
                occurrence=occurrence,
                action="defer",
                chosen=None,
                confidence=0.0,
                reason_code="llm_invalid_response",
                rationale_summary="Gemini retornou JSON inválido para desambiguação contextual.",
                rejected=True,
            )
            for occurrence in unresolved_occurrences
        ]
        diagnosis.skipped.extend(_skip_from_decision(decision) for decision in decisions)
        diagnosis.contextual_alias_disambiguation = _contextual_summary(
            mode=llm_mode,
            status="skipped",
            decisions=[*resolved_decisions, *decisions],
            skipped_reason="llm_invalid_response",
            llm_error=str(exc),
        )
        return
    decisions: list[JsonObject] = [*resolved_decisions]
    llm_linked_candidates: list[BodyLinkCandidate] = []
    skipped: list[JsonObject] = []
    for occurrence in unresolved_occurrences:
        decision = decisions_by_id.get(occurrence.occurrence_id)
        safe_decision, candidate = _validate_llm_decision(occurrence, decision)
        decisions.append(safe_decision)
        if candidate is not None:
            llm_linked_candidates.append(candidate)
        else:
            skipped.append(_skip_from_decision(safe_decision))
    unresolved_ids = {occurrence.occurrence_id for occurrence in unresolved_occurrences}
    diagnosis.candidates = [
        item
        for item in diagnosis.candidates
        if item.link_policy != "requires_context" or item.occurrence_id not in unresolved_ids
    ]
    diagnosis.candidates.extend(llm_linked_candidates)
    diagnosis.skipped.extend(skipped)
    diagnosis.contextual_alias_disambiguation = _contextual_summary(
        mode=llm_mode,
        status="completed",
        decisions=decisions,
        model=llm_model,
    )
    _persist_contextual_decisions(diagnosis.db_path, decisions, model=llm_model, response_payload=response_payload)


def _is_deterministic_contextual_occurrence(occurrence: ContextualOccurrence) -> bool:
    if len(occurrence.candidates) != 1:
        return False
    candidate = occurrence.candidates[0]
    return (
        candidate.meaning_count == 1
        and candidate.canonical_note_count == 1
        and normalize_key(candidate.target) == occurrence.normalized_surface
        and normalize_key(occurrence.matched_text) == occurrence.normalized_surface
    )


def _decisions_by_occurrence(raw: object) -> tuple[dict[str, _ContextualAliasDecisionInput], JsonObject]:
    payload = JsonObjectAdapter.validate_python(raw)
    try:
        response = _ContextualAliasResponse.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="contextual alias disambiguation response invalid") from exc
    if response.schema_ != CONTEXTUAL_ALIAS_SCHEMA:
        raise ValueError(f"LLM response must use schema {CONTEXTUAL_ALIAS_SCHEMA}")
    result: dict[str, _ContextualAliasDecisionInput] = {}
    for item in response.decisions:
        result[item.occurrence_id] = item
    return result, payload


def _validate_llm_decision(
    occurrence: ContextualOccurrence,
    decision: _ContextualAliasDecisionInput | None,
) -> tuple[JsonObject, BodyLinkCandidate | None]:
    if decision is None:
        return (
            _safe_decision_payload(
                occurrence=occurrence,
                action="defer",
                chosen=None,
                confidence=0.0,
                reason_code="missing_decision",
                rationale_summary="LLM não retornou decisão para a ocorrência.",
                rejected=True,
            ),
            None,
        )
    action = decision.action
    confidence = decision.confidence
    reason_code = decision.reason_code
    rationale = decision.rationale_summary
    chosen_target = decision.chosen_target
    chosen_meaning = decision.chosen_meaning_id
    candidates = list(occurrence.candidates)
    chosen = next(
        (
            candidate
            for candidate in candidates
            if (chosen_target and normalize_key(candidate.target) == normalize_key(chosen_target))
            or (chosen_meaning and candidate.meaning_id == chosen_meaning)
        ),
        None,
    )
    if action != "link":
        safe = _safe_decision_payload(
            occurrence=occurrence,
            action="defer" if action not in {"defer", "no_link"} else action,
            chosen=None,
            confidence=confidence,
            reason_code=reason_code or "not_linked",
            rationale_summary=rationale,
        )
        return safe, None
    if chosen is None:
        safe = _safe_decision_payload(
            occurrence=occurrence,
            action="defer",
            chosen=None,
            confidence=confidence,
            reason_code="invalid_target",
            rationale_summary="LLM escolheu alvo fora da lista fechada.",
            rejected=True,
        )
        return safe, None
    if confidence < LLM_CONFIDENCE_THRESHOLD:
        safe = _safe_decision_payload(
            occurrence=occurrence,
            action="defer",
            chosen=chosen,
            confidence=confidence,
            reason_code="confidence_below_threshold",
            rationale_summary=rationale,
        )
        return safe, None
    safe = _safe_decision_payload(
        occurrence=occurrence,
        action="link",
        chosen=chosen,
        confidence=confidence,
        reason_code=reason_code or "context_match",
        rationale_summary=rationale,
    )
    candidate = _contextual_link_candidate(
        occurrence=occurrence,
        chosen=chosen,
        confidence=confidence,
        reason_code=str(safe["reason_code"]),
        rationale_summary=rationale,
        source="contextual_alias_disambiguation",
    )
    return safe, candidate


def _contextual_link_candidate(
    *,
    occurrence: ContextualOccurrence,
    chosen: SurfacePolicy,
    confidence: float,
    reason_code: str,
    rationale_summary: str,
    source: str,
) -> BodyLinkCandidate:
    return BodyLinkCandidate(
        source_path=occurrence.source_path,
        surface=occurrence.surface,
        matched_text=occurrence.matched_text,
        target=chosen.target,
        replacement=_replacement(chosen.target, occurrence.matched_text, in_table=occurrence.in_table),
        start=occurrence.start,
        end=occurrence.end,
        link_policy="requires_context",
        meaning_count=chosen.meaning_count,
        canonical_note_count=chosen.canonical_note_count,
        intrinsically_ambiguous=chosen.intrinsically_ambiguous,
        in_protected_markdown_zone=False,
        occurrence_id=occurrence.occurrence_id,
        meaning_id=chosen.meaning_id,
        context_hash=occurrence.context_hash,
        decision_action="link",
        confidence=confidence,
        reason_code=reason_code,
        rationale_summary=rationale_summary,
        source=source,
    )


def _contextual_summary(
    *,
    mode: str,
    status: str,
    decisions: list[JsonObject],
    model: str = "",
    skipped_reason: str = "",
    blocked_reason: str = "",
    llm_error: str = "",
) -> JsonObject:
    safe_decisions = [_SafeContextualDecision.model_validate(item) for item in decisions]
    return _json_object(
        {
            "schema": CONTEXTUAL_ALIAS_SCHEMA,
            "phase": "contextual_alias_disambiguation",
            "status": status,
            "mode": mode,
            "model": model,
            "skipped_reason": skipped_reason,
            "blocked_reason": blocked_reason,
            "candidate_count": len(safe_decisions),
            "decision_count": len(safe_decisions),
            "linked_count": sum(1 for item in safe_decisions if item.action == "link" and item.safe_auto_apply),
            "deferred_count": sum(1 for item in safe_decisions if item.action == "defer"),
            "no_link_count": sum(1 for item in safe_decisions if item.action == "no_link"),
            "rejected_count": sum(1 for item in safe_decisions if item.rejected),
            "llm_error": llm_error,
            "decisions": decisions,
        }
    )


def _persist_contextual_decisions(
    db_path: Path,
    decisions: list[JsonObject],
    *,
    model: str,
    response_payload: JsonObject,
) -> None:
    if not db_path.exists() or not decisions:
        return
    response_hash = (
        "sha256:" + sha256(json.dumps(response_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
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
            )
            """
        )
        for decision in decisions:
            safe_decision = _SafeContextualDecision.model_validate(decision)
            conn.execute(
                """
                INSERT INTO contextual_alias_decisions(
                  occurrence_id, note_path, normalized_surface, matched_text, context_hash,
                  candidate_targets_json, action, chosen_meaning_id, chosen_target_path,
                  chosen_target, confidence, model, response_hash, reason_code,
                  rationale_summary, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(occurrence_id) DO UPDATE SET
                  action=excluded.action,
                  chosen_meaning_id=excluded.chosen_meaning_id,
                  chosen_target_path=excluded.chosen_target_path,
                  chosen_target=excluded.chosen_target,
                  confidence=excluded.confidence,
                  model=excluded.model,
                  response_hash=excluded.response_hash,
                  reason_code=excluded.reason_code,
                  rationale_summary=excluded.rationale_summary,
                  status=excluded.status,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    safe_decision.occurrence_id,
                    safe_decision.file,
                    normalize_key(safe_decision.surface),
                    safe_decision.matched_text,
                    safe_decision.context_hash,
                    json.dumps(safe_decision.candidate_targets, ensure_ascii=False, sort_keys=True),
                    safe_decision.action,
                    safe_decision.chosen_meaning_id,
                    "",
                    safe_decision.chosen_target,
                    safe_decision.confidence,
                    model,
                    response_hash,
                    safe_decision.reason_code,
                    safe_decision.rationale_summary,
                    "rejected" if safe_decision.rejected else "active",
                ),
            )


_EXISTING_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")


def _rewrite_candidates(path: Path, text: str, policies: list[SurfacePolicy]) -> list[BodyLinkCandidate]:
    candidates: list[BodyLinkCandidate] = []
    protected = _rewrite_protected_spans(text)
    by_surface = {policy.normalized_surface: policy for policy in policies}
    for match in _EXISTING_WIKILINK_RE.finditer(text):
        if _inside_spans(match.start(), match.end(), protected):
            continue
        raw = match.group(1).strip()
        old_target = obsidian_target_name(raw.split("|", 1)[0].split("#", 1)[0].strip())
        policy = by_surface.get(normalize_key(old_target))
        if policy is None or normalize_key(policy.target) in {normalize_key(old_target), normalize_key(path.stem)}:
            continue
        display = raw.rsplit("|", 1)[1].strip() if "|" in raw else old_target
        candidates.append(
            BodyLinkCandidate(
                source_path=str(path),
                surface=display,
                matched_text=raw,
                target=policy.target,
                replacement=_replacement(policy.target, display, in_table=_is_table_match(text, match.start())),
                start=match.start(),
                end=match.end(),
                link_policy=policy.link_policy,
                meaning_count=policy.meaning_count,
                canonical_note_count=policy.canonical_note_count,
                intrinsically_ambiguous=policy.intrinsically_ambiguous,
                in_protected_markdown_zone=False,
            )
        )
    return candidates


_WORD_CHAR_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ]")


def _has_term_boundaries(text: str, start: int, end: int) -> bool:
    left_ok = start == 0 or not _is_word_char(text[start - 1])
    right_ok = end >= len(text) or not _is_word_char(text[end])
    return left_ok and right_ok


def _is_word_char(value: str) -> bool:
    return bool(_WORD_CHAR_RE.fullmatch(value))


def _replacement(target: str, matched_text: str, *, in_table: bool) -> str:
    if matched_text == target:
        return f"[[{target}]]"
    separator = r"\|" if in_table else "|"
    return f"[[{target}{separator}{matched_text}]]"


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = _markdown_zone_spans(text)
    for pattern in (
        r"```.*?```",
        r"`[^`\n]+`",
        r"https?://\S+",
        r"!\[[^\]]*\]\([^)]+\)",
        r"!\[\[[^\]]+\]\]",
        r"\[[^\]]+\]\([^)]+\)",
        r"(?<!!)\[\[.*?\]\]",
    ):
        spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.DOTALL))
    spans.extend(_section_spans(text, "Notas Relacionadas"))
    spans.extend((m.start(), m.end()) for m in re.finditer(r"(?m)^#.*$", text))
    return sorted(spans)


def _rewrite_protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = _markdown_zone_spans(text)
    for pattern in (
        r"```.*?```",
        r"`[^`\n]+`",
        r"!\[[^\]]*\]\([^)]+\)",
        r"!\[\[[^\]]+\]\]",
    ):
        spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.DOTALL))
    spans.extend(_section_spans(text, "Notas Relacionadas"))
    spans.extend((m.start(), m.end()) for m in re.finditer(r"(?m)^#.*$", text))
    return sorted(spans)


def _markdown_zone_spans(text: str) -> list[tuple[int, int]]:
    return [(zone.start, zone.end) for zone in protected_markdown_zones(text)]


def _section_spans(text: str, heading_text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pattern = re.compile(rf"(?m)^##\s+(?:🔗\s+)?{re.escape(heading_text)}\s*$")
    next_h2 = re.compile(r"(?m)^##\s+")
    for match in pattern.finditer(text):
        next_match = next_h2.search(text, match.end())
        spans.append((match.start(), next_match.start() if next_match else len(text)))
    return spans


def _inside_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _line_bounds(text: str, start: int) -> tuple[int, int]:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = len(text)
    return line_start, line_end


def _is_table_match(text: str, start: int) -> bool:
    line_start, line_end = _line_bounds(text, start)
    line = text[line_start:line_end]
    return "|" in line and not line.lstrip().startswith(">")
