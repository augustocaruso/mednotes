"""Triage-authored note plan validation (schema v2, hard cut).

v2 actions:

- ``planned_meaning``: durable semantic unit declared from the raw chat.
  Requires ``meaning_claim`` with label/scope/boundaries/kind/evidence_summary.
- ``attach_to_planned_meaning``: subordinate detail that belongs to another
  unit of the same raw chat. Carries ``target_item_id`` pointing to a sibling
  ``planned_meaning`` item.
- ``not_a_note``: content that should not become a Wiki note.
- ``needs_context``: raw chat does not support safe segmentation for this
  unit.

Older note-plan schemas are rejected at the boundary. Runtime code does not
migrate or reinterpret them; the triage contract must be regenerated as v2.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import NOTE_PLAN_SELF_HASH_KEYS, artifact_json_hash, clean_state_value
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _path
from mednotes.domains.wiki.contracts.note_plan import TriageNotePlan
from mednotes.kernel.base import JsonObject, JsonObjectAdapter, contract_error

TRIAGE_NOTE_PLAN_SCHEMA = "medical-notes-workbench.triage-note-plan.v2"
TRIAGE_NOTE_PLAN_V2_SCHEMA = TRIAGE_NOTE_PLAN_SCHEMA

PLANNED_MEANING_ACTION = "planned_meaning"
ATTACH_TO_PLANNED_MEANING_ACTION = "attach_to_planned_meaning"
NOT_A_NOTE_ACTION = "not_a_note"
NEEDS_CONTEXT_ACTION = "needs_context"

ALLOWED_ACTIONS = {
    PLANNED_MEANING_ACTION,
    ATTACH_TO_PLANNED_MEANING_ACTION,
    NOT_A_NOTE_ACTION,
    NEEDS_CONTEXT_ACTION,
}
ALLOWED_V2_ACTIONS = ALLOWED_ACTIONS

# Closed sets used by triage-policy.md.
MEANING_CLAIM_KINDS = {
    "clinical_concept",
    "drug_concept",
    "diagnostic_criterion",
    "management_strategy",
    "procedure",
    "physiology_or_mechanism",
    "epidemiology_or_definition",
}
MEANING_CLAIM_REQUIRED_FIELDS = ("label", "scope", "boundaries", "kind", "evidence_summary")

ATTACH_REASON_CODES = {
    "supporting_detail",
    "boundary_clarification",
    "example_or_case",
    "cross_reference",
}
NOT_A_NOTE_REASON_CODES = {
    "administrative_chatter",
    "repetition_no_new_information",
    "out_of_scope_for_medical_wiki",
    "low_value_fragment",
}
NEEDS_CONTEXT_REASON_CODES = {
    "evidence_insufficient",
    "multiple_topics_undifferentiated",
    "clinical_ambiguity",
    "language_or_encoding_blocker",
}

TRIAGE_NOTE_PLAN_BATCH_KEYS = ("batch_id", "run_id", "source_artifact_hash")
_FENCED_JSON_RE = re.compile(r"```(?:json|JSON)?[ \t]*\n(?P<body>.*?)\n```", re.DOTALL)
_MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â€|â€“|â€”|�)")


def _json_object(value: object, *, message: str) -> JsonObject:
    """Normalize raw JSON before note-plan policy reads operational fields."""

    if not isinstance(value, dict):
        raise ValidationError(message)
    return JsonObjectAdapter.validate_python(value)


def _json_field(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return str(value).strip() if value else ""


def _paths_match(left: str, right: Path) -> bool:
    left_path = _path(left)
    try:
        return left_path.resolve() == right.resolve()
    except OSError:
        return str(left_path) == str(right)


def _load_json_file(path: Path) -> JsonObject:
    if not path.exists():
        raise MissingPathError(f"Triage note plan not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid triage note plan JSON: {exc}") from exc
    return _json_object(data, message="Triage note plan must be a JSON object")


def _validate_meaning_claim(item_id: str, raw_claim: object) -> JsonObject:
    raw_claim = _json_object(
        raw_claim,
        message=f"note_plan_meaning_claim_missing: item {item_id} requires meaning_claim object",
    )
    claim: JsonObject = {}
    for field in MEANING_CLAIM_REQUIRED_FIELDS:
        if field not in raw_claim:
            raise ValidationError(
                f"note_plan_meaning_claim_missing: item {item_id} meaning_claim missing {field!r}"
            )
    label = _json_str_field(raw_claim, "label")
    if not label:
        raise ValidationError(
            f"Triage note plan item {item_id} meaning_claim.label must be non-empty"
        )
    scope = _json_str_field(raw_claim, "scope")
    if not scope:
        raise ValidationError(
            f"Triage note plan item {item_id} meaning_claim.scope must be non-empty"
        )
    boundaries_raw = _json_field(raw_claim, "boundaries")
    if not isinstance(boundaries_raw, list):
        raise ValidationError(
            f"Triage note plan item {item_id} meaning_claim.boundaries must be a list"
        )
    boundaries = [str(entry).strip() for entry in boundaries_raw if str(entry).strip()]
    kind = _json_str_field(raw_claim, "kind")
    if kind not in MEANING_CLAIM_KINDS:
        raise ValidationError(
            f"Triage note plan item {item_id} meaning_claim.kind={kind!r} is not one of "
            f"{sorted(MEANING_CLAIM_KINDS)}"
        )
    evidence_summary = _json_str_field(raw_claim, "evidence_summary")
    if not evidence_summary:
        raise ValidationError(
            f"Triage note plan item {item_id} meaning_claim.evidence_summary must be non-empty"
        )
    for field, value in {
        "meaning_claim.label": label,
        "meaning_claim.scope": scope,
        "meaning_claim.kind": kind,
        "meaning_claim.evidence_summary": evidence_summary,
        **{f"meaning_claim.boundaries[{idx}]": boundary for idx, boundary in enumerate(boundaries)},
    }.items():
        _reject_mojibake(value, field=field, item_id=item_id)
    claim["label"] = label
    claim["scope"] = scope
    claim["boundaries"] = boundaries
    claim["kind"] = kind
    claim["evidence_summary"] = evidence_summary
    meaning_id = _json_str_field(raw_claim, "id") or _json_str_field(raw_claim, "meaning_id")
    if meaning_id:
        claim["id"] = meaning_id
    return claim


def _reject_mojibake(value: str, *, field: str, item_id: str) -> None:
    if _MOJIBAKE_RE.search(value):
        raise ValidationError(
            f"Triage note plan item {item_id} {field} contains mojibake/encoding corruption: {value}"
        )


def _normalized_items(items: object) -> list[JsonObject]:
    if not isinstance(items, list) or not items:
        raise ValidationError("Triage note plan must contain a non-empty items list")

    normalized: list[JsonObject] = []
    seen_ids: set[str] = set()
    seen_planned_titles: dict[str, str] = {}
    planned_ids: set[str] = set()
    pending_attach: list[tuple[int, str, str]] = []

    for index, raw_item in enumerate(items, start=1):
        raw_item = _json_object(raw_item, message=f"Triage note plan item #{index} must be an object")
        item_id = _json_str_field(raw_item, "id") or f"T{index:03d}"
        action = _json_str_field(raw_item, "action")
        if not item_id:
            raise ValidationError(f"Triage note plan item #{index} missing id")
        if item_id in seen_ids:
            raise ValidationError(f"Triage note plan item id duplicated: {item_id}")
        if action not in ALLOWED_ACTIONS:
            raise ValidationError(
                f"Triage note plan item {item_id} has invalid action {action!r}; "
                f"expected one of {', '.join(sorted(ALLOWED_ACTIONS))}"
            )

        item: JsonObject = {"id": item_id, "action": action}

        if action == PLANNED_MEANING_ACTION:
            title = _json_str_field(raw_item, "title")
            if not title:
                raise ValidationError(
                    f"Triage note plan item {item_id} missing title"
                )
            staged_title = _json_str_field(raw_item, "staged_title") or title
            if not staged_title:
                raise ValidationError(
                    f"Triage note plan item {item_id} missing staged_title"
                )
            _reject_mojibake(title, field="title", item_id=item_id)
            _reject_mojibake(staged_title, field="staged_title", item_id=item_id)
            staged_key = normalize_key(staged_title)
            if staged_key in seen_planned_titles:
                raise ValidationError(
                    "note_plan_duplicate_meaning: planned_meaning title duplicated after accent/case "
                    f"normalization: {staged_title} conflicts with "
                    f"{seen_planned_titles[staged_key]}"
                )
            seen_planned_titles[staged_key] = staged_title
            item["title"] = title
            item["staged_title"] = staged_title
            item["meaning_claim"] = _validate_meaning_claim(item_id, _json_field(raw_item, "meaning_claim"))
            taxonomy_hint = _json_str_field(raw_item, "taxonomy_hint")
            if taxonomy_hint:
                _reject_mojibake(taxonomy_hint, field="taxonomy_hint", item_id=item_id)
                item["taxonomy_hint"] = taxonomy_hint
            aliases = _json_field(raw_item, "aliases")
            if isinstance(aliases, list):
                clean_aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
                if clean_aliases:
                    for alias_idx, alias in enumerate(clean_aliases):
                        _reject_mojibake(alias, field=f"aliases[{alias_idx}]", item_id=item_id)
                    item["aliases"] = clean_aliases
            planned_ids.add(item_id)
        elif action == ATTACH_TO_PLANNED_MEANING_ACTION:
            target = _json_str_field(raw_item, "target_item_id")
            if not target:
                raise ValidationError(
                    f"note_plan_target_item_id_missing: item {item_id} attach action missing target_item_id"
                )
            reason_code = _json_str_field(raw_item, "reason_code")
            if reason_code not in ATTACH_REASON_CODES:
                raise ValidationError(
                    f"note_plan_reason_code_missing: item {item_id} attach reason_code={reason_code!r} "
                    f"is not one of {sorted(ATTACH_REASON_CODES)}"
                )
            reason = _json_str_field(raw_item, "reason")
            if not reason:
                raise ValidationError(
                    f"Triage note plan item {item_id} attach action missing reason"
                )
            item["target_item_id"] = target
            item["reason_code"] = reason_code
            item["reason"] = reason
            pending_attach.append((index, item_id, target))
        elif action == NOT_A_NOTE_ACTION:
            reason_code = _json_str_field(raw_item, "reason_code")
            if reason_code not in NOT_A_NOTE_REASON_CODES:
                raise ValidationError(
                    f"note_plan_reason_code_missing: item {item_id} not_a_note reason_code={reason_code!r} "
                    f"is not one of {sorted(NOT_A_NOTE_REASON_CODES)}"
                )
            reason = _json_str_field(raw_item, "reason")
            if not reason:
                raise ValidationError(
                    f"Triage note plan item {item_id} not_a_note missing reason"
                )
            item["reason_code"] = reason_code
            item["reason"] = reason
        elif action == NEEDS_CONTEXT_ACTION:
            reason_code = _json_str_field(raw_item, "reason_code")
            if reason_code not in NEEDS_CONTEXT_REASON_CODES:
                raise ValidationError(
                    f"note_plan_reason_code_missing: item {item_id} needs_context reason_code={reason_code!r} "
                    f"is not one of {sorted(NEEDS_CONTEXT_REASON_CODES)}"
                )
            reason = _json_str_field(raw_item, "reason")
            if not reason:
                raise ValidationError(
                    f"Triage note plan item {item_id} needs_context missing reason"
                )
            item["reason_code"] = reason_code
            item["reason"] = reason

        normalized.append(item)
        seen_ids.add(item_id)

    for _, attach_id, target in pending_attach:
        if target not in planned_ids:
            raise ValidationError(
                f"note_plan_target_item_id_missing: item {attach_id} attach target_item_id={target!r} "
                "must reference a sibling planned_meaning item"
            )

    return normalized


def _reject_unsupported_schema(schema_value: str) -> None:
    raise ValidationError(
        f"Triage note plan schema must be {TRIAGE_NOTE_PLAN_SCHEMA}, got {schema_value!r}"
    )


def normalize_triage_note_plan(data: JsonObject, raw_file: Path) -> JsonObject:
    data = _json_object(data, message="Triage note plan must be a JSON object")
    schema_value = _json_str_field(data, "schema")
    if schema_value != TRIAGE_NOTE_PLAN_SCHEMA:
        _reject_unsupported_schema(schema_value)
    raw_value = _json_str_field(data, "raw_file")
    if not raw_value:
        raise ValidationError("Triage note plan missing raw_file")
    if not _paths_match(raw_value, raw_file):
        raise ValidationError(f"note_plan_raw_file_mismatch: triage note plan raw_file does not match: {raw_value}")
    if _json_field(data, "exhaustive") is not True:
        raise ValidationError("Triage note plan must set exhaustive: true")
    normalized = {
        "schema": TRIAGE_NOTE_PLAN_SCHEMA,
        "raw_file": str(raw_file),
        "exhaustive": True,
        "items": _normalized_items(_json_field(data, "items")),
    }
    for key in TRIAGE_NOTE_PLAN_BATCH_KEYS:
        value = clean_state_value(_json_field(data, key))
        if value:
            normalized[key] = value
    try:
        return TriageNotePlan.model_validate(normalized).to_payload()
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="triage_note_plan.contract_invalid") from exc


def normalize_triage_note_plan_v2(data: JsonObject, raw_file: Path) -> JsonObject:
    return normalize_triage_note_plan(data, raw_file)


def load_triage_note_plan(path: Path, raw_file: Path) -> JsonObject:
    return normalize_triage_note_plan(_load_json_file(path), raw_file)


def parse_triage_note_plan(value: str, raw_file: Path) -> JsonObject:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        fences = [match.group("body").strip() for match in _FENCED_JSON_RE.finditer(value)]
        if len(fences) > 1:
            raise ValidationError(
                "note_plan_invalid: multiple fenced JSON blocks found; provide exactly one triage-note-plan object"
            ) from exc
        if len(fences) == 1:
            try:
                data = json.loads(fences[0])
            except json.JSONDecodeError as fenced_exc:
                raise ValidationError(
                    f"note_plan_invalid: invalid fenced triage note plan JSON: {fenced_exc}"
                ) from fenced_exc
        else:
            raise ValidationError(f"Invalid triage note plan in raw frontmatter: {exc}") from exc
    return normalize_triage_note_plan(
        _json_object(data, message="Triage note plan in raw frontmatter must be a JSON object"),
        raw_file,
    )


def serialize_triage_note_plan(data: JsonObject, raw_file: Path) -> str:
    normalized = normalize_triage_note_plan(data, raw_file)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def note_plan_hash(plan: JsonObject) -> str:
    return artifact_json_hash(plan, exclude_keys=NOTE_PLAN_SELF_HASH_KEYS)


def planned_meaning_titles(plan: JsonObject) -> set[str]:
    validated = TriageNotePlan.model_validate(plan)
    return {
        title
        for item in validated.items
        if item.action == PLANNED_MEANING_ACTION
        for title in [(item.staged_title or item.title or "").strip()]
        if title
    }


def note_plan_summary(plan: JsonObject) -> dict[str, int]:
    validated = TriageNotePlan.model_validate(plan)
    counts = dict.fromkeys(ALLOWED_ACTIONS, 0)
    for item in validated.items:
        action = item.action
        if action in counts:
            counts[action] += 1
    return {
        "note_plan_item_count": sum(counts.values()),
        "note_plan_planned_meaning_count": counts[PLANNED_MEANING_ACTION],
        "note_plan_attach_count": counts[ATTACH_TO_PLANNED_MEANING_ACTION],
        "note_plan_not_a_note_count": counts[NOT_A_NOTE_ACTION],
        "note_plan_needs_context_count": counts[NEEDS_CONTEXT_ACTION],
    }
