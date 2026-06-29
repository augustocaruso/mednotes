"""Validation helpers for med-link-graph-curator batch outputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import (
    VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA,
    VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA,
    curator_agent_event,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_ingestion import INGESTION_SCHEMA
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import note_content_hash
from mednotes.domains.wiki.common import ValidationError

CURATOR_OUTPUT_VALIDATION_SCHEMA = "medical-notes-workbench.curator-output-validation.v1"


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return payload


def _error(*, work_id: str, path: str, code: str, message: str) -> dict[str, str]:
    return {"work_id": work_id, "path": path, "code": code, "message": message}


def _plan_items(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if plan.get("schema") != VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA:
        raise ValidationError(f"curator batch plan must use schema {VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA}")
    raw_items = plan.get("work_items")
    if not isinstance(raw_items, list):
        raise ValidationError("curator batch plan requires work_items[]")
    items: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("work_id"):
            raise ValidationError("curator batch work_items require work_id")
        work_id = str(raw["work_id"])
        if work_id in items:
            raise ValidationError(f"duplicate work_id in curator batch plan: {work_id}")
        items[work_id] = raw
    return items


def _manifest_items(manifest_path: Path) -> list[dict[str, str]]:
    manifest = _read_json_object(manifest_path, label="curator batch output manifest")
    if manifest.get("schema") != VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA:
        raise ValidationError(
            f"curator batch manifest must use schema {VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA}"
        )
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("curator batch manifest requires items[]")
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("work_id") or not raw.get("output_path"):
            raise ValidationError("each curator batch manifest item requires work_id and output_path")
        work_id = str(raw["work_id"])
        if work_id in seen:
            raise ValidationError(f"duplicate work_id in curator batch manifest: {work_id}")
        seen.add(work_id)
        items.append({"work_id": work_id, "output_path": str(raw["output_path"])})
    return items


def _validate_output_payload(
    *,
    work_id: str,
    expected: dict[str, Any],
    output_path: Path,
    payload: dict[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    path_text = str(output_path)
    if payload.get("schema") != INGESTION_SCHEMA:
        errors.append(_error(work_id=work_id, path=path_text, code="missing_schema", message=f"expected {INGESTION_SCHEMA}"))
    note_path = str(payload.get("note_path") or "")
    if note_path != str(expected.get("note_path") or ""):
        errors.append(_error(work_id=work_id, path=path_text, code="note_path_mismatch", message="output note_path differs from work item"))
    expected_hash = str(expected.get("content_hash") or "")
    content_hash = str(payload.get("content_hash") or "")
    if content_hash != expected_hash:
        errors.append(_error(work_id=work_id, path=path_text, code="content_hash_mismatch", message="output content_hash differs from work item"))
    note = Path(str(expected.get("note_path") or ""))
    if note.is_file() and expected_hash and note_content_hash(note) != expected_hash:
        errors.append(_error(work_id=work_id, path=path_text, code="stale_content_hash", message="note changed after work packet was created"))
    primary = payload.get("primary_meaning")
    if not isinstance(primary, dict) or not primary.get("label"):
        errors.append(_error(work_id=work_id, path=path_text, code="missing_primary_meaning_label", message="primary_meaning.label is required"))
    aliases = payload.get("aliases")
    if not isinstance(aliases, list):
        errors.append(_error(work_id=work_id, path=path_text, code="aliases_not_list", message="aliases must be a list"))
    else:
        for index, alias in enumerate(aliases):
            if not isinstance(alias, dict):
                errors.append(
                    _error(
                        work_id=work_id,
                        path=path_text,
                        code="alias_not_object",
                        message=f"aliases[{index}] must be an object",
                    )
                )
                continue
            if not str(alias.get("text") or "").strip():
                hint = "; use aliases[].text, not aliases[].surface" if alias.get("surface") else ""
                errors.append(
                    _error(
                        work_id=work_id,
                        path=path_text,
                        code="alias_missing_text",
                        message=f"aliases[{index}].text is required{hint}",
                    )
                )
    deferred = payload.get("deferred_work_items", [])
    if deferred is not None and not isinstance(deferred, list):
        errors.append(_error(work_id=work_id, path=path_text, code="deferred_work_items_not_list", message="deferred_work_items must be a list"))
    return errors


def validate_curator_batch_outputs(*, plan: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    by_work_id = _plan_items(plan)
    manifest_items = _manifest_items(manifest_path)
    errors: list[dict[str, str]] = []
    agent_events: list[dict[str, Any]] = []
    validated_count = 0
    for manifest_item in manifest_items:
        work_id = manifest_item["work_id"]
        output_path = Path(manifest_item["output_path"])
        expected = by_work_id.get(work_id)
        if expected is None:
            errors.append(_error(work_id=work_id, path=str(output_path), code="unknown_work_id", message="manifest work_id is absent from plan"))
            continue
        try:
            payload = _read_json_object(output_path, label="curator batch output")
        except ValidationError as exc:
            errors.append(_error(work_id=work_id, path=str(output_path), code="invalid_json", message=str(exc)))
            continue
        item_errors = _validate_output_payload(work_id=work_id, expected=expected, output_path=output_path, payload=payload)
        if item_errors:
            errors.extend(item_errors)
            if any(error.get("code") == "alias_missing_text" and "surface" in error.get("message", "") for error in item_errors):
                agent_events.append(
                    curator_agent_event(
                        code="agent.curator_alias_surface_without_text",
                        root_cause_code="semantic_ingestion.validation_error",
                        next_action="Corrigir aliases[].text e repetir collect-curator-outputs, eval-curator-batch e apply-curator-batch.",
                        artifact_path=str(output_path),
                        reason="aliases_surface_without_text",
                    )
                )
        else:
            validated_count += 1
    result = {
        "schema": CURATOR_OUTPUT_VALIDATION_SCHEMA,
        "phase": "vocabulary_curation",
        "status": "valid" if not errors else "blocked",
        "blocked_reason": "" if not errors else "semantic_ingestion.validation_error",
        "errors": errors,
        "validated_count": validated_count,
        "manifest_item_count": len(manifest_items),
        "next_action": "" if not errors else "corrigir outputs inválidos antes de aplicar",
    }
    if agent_events:
        result["agent_events"] = agent_events
    return result
