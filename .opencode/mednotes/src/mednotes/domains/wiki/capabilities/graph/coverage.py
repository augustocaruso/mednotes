"""Coverage inventory validation for raw chat publishing."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import COVERAGE_SELF_HASH_KEYS, artifact_json_hash, clean_state_value
from mednotes.domains.wiki.capabilities.notes.note_plan import (
    PLANNED_MEANING_ACTION,
    note_plan_hash,
    note_plan_summary,
    parse_triage_note_plan,
    planned_meaning_titles,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import read_note_meta
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _path
from mednotes.domains.wiki.contracts.note_plan import TriageNotePlan, TriageNotePlanItem
from mednotes.domains.wiki.contracts.raw_coverage import (
    RAW_COVERAGE_SCHEMA,
    RawCoverage,
    RawCoverageItem,
    RawCoverageSource,
    RawCoverageSummary,
)
from mednotes.kernel.base import JsonObject, JsonObjectAdapter, contract_error

NOT_A_NOTE_ACTION = "not_a_note"
COVERAGE_NOTE_PLAN_BINDING_KEYS = ("batch_id", "run_id", "source_artifact_hash")
MULTI_SOURCE_STATUSES = {"covered", "already_covered", "not_relevant"}


@dataclass(frozen=True)
class _LoadedCoverage:
    """Raw JSON plus its canonical typed view.

    ``payload`` exists only for stable hashing. Every workflow decision below
    reads ``inventory`` so a malformed coverage file cannot fabricate success.
    """

    payload: JsonObject
    inventory: RawCoverage


def _paths_match(left: str, right: Path) -> bool:
    left_path = _path(left)
    try:
        return left_path.resolve() == right.resolve()
    except OSError:
        return str(left_path) == str(right)


def _load_coverage(path: Path) -> _LoadedCoverage:
    if not path.exists():
        raise MissingPathError(f"Coverage inventory not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid coverage inventory JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Coverage inventory must be a JSON object")
    try:
        payload = JsonObjectAdapter.validate_python(raw)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="raw_coverage.json_invalid") from exc
    if "schema" not in payload or payload["schema"] != RAW_COVERAGE_SCHEMA:
        raise ValidationError(f"Coverage inventory schema must be {RAW_COVERAGE_SCHEMA}")
    try:
        inventory = RawCoverage.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="raw_coverage.contract_invalid") from exc
    return _LoadedCoverage(payload=payload, inventory=inventory)


def _triage_note_plan(raw_file: Path, *, required: bool) -> JsonObject | None:
    raw_meta = read_note_meta(raw_file)
    raw_plan = raw_meta["note_plan"] if "note_plan" in raw_meta else ""
    if not raw_plan:
        if required:
            raise ValidationError("Raw chat missing triage note_plan; rerun triage with --note-plan")
        return None
    return parse_triage_note_plan(raw_plan, raw_file)


def _coverage_planned_meaning_titles(items: Sequence[RawCoverageItem]) -> set[str]:
    return {
        item.planned_title
        for item in items
        if item.action == PLANNED_MEANING_ACTION and item.planned_title
    }


def _coverage_planned_meaning_keys(items: Sequence[RawCoverageItem]) -> set[str]:
    return {normalize_key(title) for title in _coverage_planned_meaning_titles(items)}


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _coverage_raw_files(data: RawCoverage, primary_raw_file: Path) -> list[Path]:
    if not data.raw_files:
        return [primary_raw_file]
    raw_files: list[Path] = []
    seen: set[str] = set()
    for index, value in enumerate(data.raw_files, start=1):
        raw_file_text = value.strip()
        path = _path(raw_file_text)
        if not raw_file_text:
            raise ValidationError(f"provenance_gap: coverage raw_files item #{index} is empty")
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            raise MissingPathError(f"Raw file not found: {path}")
        raw_files.append(path)
    if _path_key(primary_raw_file) not in {_path_key(path) for path in raw_files}:
        raise ValidationError("provenance_gap: coverage raw_files must include the primary raw_file")
    return raw_files


def _item_signature(item: RawCoverageItem) -> tuple[str, str, str, str]:
    """Canonical signature used to compare coverage and note_plan items.

    raw-coverage stores only coverage-bearing v2 actions. ``planned_meaning``
    drives note output; ``not_a_note`` records explicitly discarded material.
    """
    return (item.id.strip(), item.action, item.planned_title, "")


def _note_plan_item_as_coverage_signature(item: TriageNotePlanItem) -> tuple[str, str, str, str] | None:
    """Translate a v2 note_plan item to a coverage-comparison signature.

    attach_to_planned_meaning and needs_context items do not appear in
    raw-coverage.v1 inventories: attach is folded into the target's note, and
    needs_context blocks the architect. They are skipped on this translation
    layer.
    """
    if item.action not in {PLANNED_MEANING_ACTION, NOT_A_NOTE_ACTION}:
        return None
    title = (item.staged_title or item.title or "").strip()
    return (item.id.strip(), item.action, title, "")


def _coverage_metadata_value(data: RawCoverage, key: str) -> str:
    return clean_state_value(getattr(data, key, None))


def _json_metadata_value(data: JsonObject, key: str) -> str:
    return clean_state_value(data[key] if key in data else None)


def _validate_coverage_sources(
    data: RawCoverage,
    raw_files: list[Path],
    items: Sequence[RawCoverageItem],
) -> list[RawCoverageSource]:
    if not data.sources:
        if len(raw_files) == 1:
            return []
        raise ValidationError("provenance_gap: multi-source coverage must include sources[]")

    raw_file_keys = {_path_key(path) for path in raw_files}
    seen: set[str] = set()
    sources: list[RawCoverageSource] = []
    coverage_titles = _coverage_planned_meaning_titles(items)
    coverage_title_keys = {normalize_key(title) for title in coverage_titles}
    for index, source in enumerate(data.sources, start=1):
        raw_file_value = source.raw_file.strip()
        if not raw_file_value:
            raise ValidationError(f"provenance_gap: coverage source #{index} missing raw_file")
        raw_file = _path(raw_file_value)
        raw_key = _path_key(raw_file)
        if raw_key not in raw_file_keys:
            raise ValidationError(f"provenance_gap: coverage source raw_file is not in raw_files: {raw_file}")
        if raw_key in seen:
            raise ValidationError(f"provenance_gap: duplicate coverage source for raw_file: {raw_file}")
        seen.add(raw_key)
        status = source.status
        if status not in MULTI_SOURCE_STATUSES:
            raise ValidationError(
                f"provenance_gap: coverage source {raw_file} has invalid status {status!r}; "
                f"expected one of {', '.join(sorted(MULTI_SOURCE_STATUSES))}"
            )
        clean_source = source.model_copy(update={"raw_file": str(raw_file)})
        if status == "covered":
            if normalize_key(clean_source.target_title.strip()) not in coverage_title_keys:
                raise ValidationError(
                    f"provenance_gap: covered source {raw_file} target_title is absent from coverage items"
                )
        sources.append(clean_source)

    missing_sources = [str(path) for path in raw_files if _path_key(path) not in seen]
    if missing_sources:
        raise ValidationError("provenance_gap: coverage sources missing raw_files: " + ", ".join(missing_sources))
    return sources


def _validate_multi_source_note_plans(
    raw_files: list[Path],
    items: Sequence[RawCoverageItem],
    *,
    required: bool,
) -> JsonObject:
    coverage_keys = _coverage_planned_meaning_keys(items)
    union_plan_keys: set[str] = set()
    note_plan_hashes: dict[str, str] = {}
    planned_meaning_count = 0
    item_count = 0
    for raw_file in raw_files:
        note_plan = _triage_note_plan(raw_file, required=required)
        if not note_plan:
            continue
        plan_titles = planned_meaning_titles(note_plan)
        plan_keys = {normalize_key(title) for title in plan_titles}
        missing = sorted(plan_titles, key=normalize_key)
        missing = [title for title in missing if normalize_key(title) not in coverage_keys]
        if missing:
            raise ValidationError(
                f"provenance_gap: coverage is missing triage-planned notes for {raw_file}: "
                + ", ".join(missing)
            )
        union_plan_keys.update(plan_keys)
        note_plan_hashes[str(raw_file)] = note_plan_hash(note_plan)
        summary = note_plan_summary(note_plan)
        planned_meaning_count += int(summary["note_plan_planned_meaning_count"])
        item_count += int(summary["note_plan_item_count"])
    extra_keys = sorted(coverage_keys - union_plan_keys)
    if required and extra_keys:
        raise ValidationError(
            "provenance_gap: coverage has planned_meaning targets absent from all source note_plans: "
            + ", ".join(extra_keys)
        )
    return JsonObjectAdapter.validate_python({
        "note_plan_source_count": len(note_plan_hashes),
        "note_plan_hashes": note_plan_hashes,
        "note_plan_item_count": item_count,
        "note_plan_planned_meaning_count": planned_meaning_count,
    })


def _merge_note_plan_metadata(
    result: RawCoverageSummary,
    coverage_data: RawCoverage,
    note_plan: JsonObject | None,
) -> RawCoverageSummary:
    if note_plan is None:
        updates: dict[str, object] = {}
        coverage_note_plan_hash = _coverage_metadata_value(coverage_data, "note_plan_hash")
        if coverage_note_plan_hash:
            updates["note_plan_hash"] = coverage_note_plan_hash
        for key in COVERAGE_NOTE_PLAN_BINDING_KEYS:
            value = _coverage_metadata_value(coverage_data, key)
            if value:
                updates[key] = value
        return result.model_copy(update=updates)

    expected_note_plan_hash = note_plan_hash(note_plan)
    coverage_note_plan_hash = _coverage_metadata_value(coverage_data, "note_plan_hash")
    if coverage_note_plan_hash and coverage_note_plan_hash != expected_note_plan_hash:
        raise ValidationError(
            "batch_state_mismatch: coverage note_plan_hash does not match raw triage note_plan. "
            "Regenerate raw coverage from the current note_plan and rerun stage-note."
        )
    updates = {"note_plan_hash": expected_note_plan_hash}

    for key in COVERAGE_NOTE_PLAN_BINDING_KEYS:
        coverage_value = _coverage_metadata_value(coverage_data, key)
        plan_value = _json_metadata_value(note_plan, key)
        if coverage_value and plan_value and coverage_value != plan_value:
            raise ValidationError(
                "batch_state_mismatch: "
                f"coverage {key}={coverage_value} does not match raw triage note_plan {key}={plan_value}. "
                "Regenerate raw coverage from the current note_plan and rerun stage-note."
            )
        value = coverage_value or plan_value
        if value:
            updates[key] = value
    return result.model_copy(update=updates)


def _validate_raw_coverage_structure_model(
    path: Path,
    raw_file: Path,
    *,
    require_triage_note_plan: bool = True,
) -> RawCoverageSummary:
    """Validate structure and raw-file binding without checking staged notes."""

    loaded = _load_coverage(path)
    data = loaded.inventory
    raw_value = data.raw_file
    if not raw_value:
        raise ValidationError("coverage_invalid: Coverage inventory missing raw_file")
    if not _paths_match(raw_value, raw_file):
        raise ValidationError(f"coverage_invalid: Coverage inventory raw_file does not match manifest batch: {raw_value}")
    raw_files = _coverage_raw_files(data, raw_file)
    items = data.items

    planned_meaning_count = sum(1 for item in items if item.action == PLANNED_MEANING_ACTION)
    not_a_note_count = sum(1 for item in items if item.action == NOT_A_NOTE_ACTION)

    sources = _validate_coverage_sources(data, raw_files, items)
    result = RawCoverageSummary(
        coverage_path=str(path),
        coverage_hash=artifact_json_hash(loaded.payload, exclude_keys=COVERAGE_SELF_HASH_KEYS),
        raw_file=str(raw_file),
        raw_files=[str(path) for path in raw_files],
        multi_source=len(raw_files) > 1,
        source_count=len(sources) or len(raw_files),
        exhaustive=True,
        item_count=len(items),
        planned_meaning_count=planned_meaning_count,
        not_a_note_count=not_a_note_count,
        raw_file_count=len(raw_files),
        covered_count=len(raw_files),
    )
    if sources:
        status_counts = dict.fromkeys(sorted(MULTI_SOURCE_STATUSES), 0)
        for source in sources:
            status_counts[source.status] += 1
        result = result.model_copy(update={"sources": sources, "source_status_counts": status_counts})
    if len(raw_files) > 1:
        result = result.model_copy(
            update=_validate_multi_source_note_plans(
                raw_files,
                items,
                required=require_triage_note_plan,
            ),
        )
        return _merge_note_plan_metadata(result, data, None)

    note_plan = _triage_note_plan(raw_file, required=require_triage_note_plan)
    if note_plan:
        plan_titles = planned_meaning_titles(note_plan)
        coverage_titles = _coverage_planned_meaning_titles(items)
        missing = sorted(plan_titles - coverage_titles)
        extra = sorted(coverage_titles - plan_titles)
        if missing:
            raise ValidationError(
                "Coverage inventory is missing triage-planned notes: " + ", ".join(missing)
            )
        if extra:
            raise ValidationError(
                "Coverage inventory has planned_meaning items absent from triage note_plan: " + ", ".join(extra)
            )
        plan_signatures: set[tuple[str, str, str, str]] = set()
        for plan_item in TriageNotePlan.model_validate(note_plan).items:
            sig = _note_plan_item_as_coverage_signature(plan_item)
            if sig is not None:
                plan_signatures.add(sig)
        coverage_signatures = {_item_signature(item) for item in items}
        if plan_signatures != coverage_signatures:
            raise ValidationError("Coverage inventory does not match triage note_plan items")
        result = result.model_copy(update=note_plan_summary(note_plan))
    return _merge_note_plan_metadata(result, data, note_plan)


def validate_raw_coverage_structure(path: Path, raw_file: Path, *, require_triage_note_plan: bool = True) -> JsonObject:
    """Validate structure and raw-file binding without checking staged notes."""

    return _validate_raw_coverage_structure_model(
        path,
        raw_file,
        require_triage_note_plan=require_triage_note_plan,
    ).to_payload()


def validate_raw_coverage(
    path: Path,
    raw_file: Path,
    staged_titles: list[str],
    *,
    require_triage_note_plan: bool = True,
) -> JsonObject:
    """Validate that the exhaustive inventory and staged manifest agree."""

    summary = _validate_raw_coverage_structure_model(
        path,
        raw_file,
        require_triage_note_plan=require_triage_note_plan,
    )
    data = _load_coverage(path).inventory
    items = data.items
    staged = {title.strip() for title in staged_titles if title.strip()}
    planned_titles: set[str] = set()
    for item in items:
        if item.action != PLANNED_MEANING_ACTION:
            continue
        planned_titles.add(item.planned_title)

    missing = sorted(planned_titles - staged)
    unexpected = sorted(staged - planned_titles)
    if missing:
        raise ValidationError(
            "coverage_invalid: Coverage inventory has planned_meaning items not staged in manifest: "
            + ", ".join(missing)
        )
    if unexpected:
        raise ValidationError(
            "coverage_invalid: Manifest has staged notes absent from coverage inventory: " + ", ".join(unexpected)
        )

    return summary.model_copy(update={"staged_note_count": len(staged)}).to_payload()
