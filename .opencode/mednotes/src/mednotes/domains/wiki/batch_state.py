"""Batch identity and artifact hash helpers for process-chats."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mednotes.domains.wiki.common import ValidationError
from mednotes.kernel.base import JsonObject, JsonValue

BATCH_STATE_KEYS = (
    "batch_id",
    "run_id",
    "note_plan_hash",
    "coverage_hash",
    "source_artifact_hash",
)
NOTE_PLAN_SELF_HASH_KEYS = ("note_plan_hash",)
COVERAGE_SELF_HASH_KEYS = ("coverage_hash",)


def canonical_json_hash(value: JsonValue) -> str:
    """Return a stable SHA-256 for JSON-compatible values."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_json_hash(data: JsonObject, *, exclude_keys: tuple[str, ...] = ()) -> str:
    """Hash an artifact while ignoring optional self-hash fields."""
    excluded = set(exclude_keys)
    return canonical_json_hash({key: value for key, value in data.items() if key not in excluded})


def clean_state_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return ""


def batch_state_from(data: JsonObject) -> dict[str, str]:
    state: dict[str, str] = {}
    for key in BATCH_STATE_KEYS:
        value = clean_state_value(data.get(key))
        if value:
            state[key] = value
    return state


def require_compatible_batch_state(
    left: JsonObject,
    right: JsonObject,
    *,
    left_label: str,
    right_label: str,
    keys: tuple[str, ...] = BATCH_STATE_KEYS,
) -> None:
    for key in keys:
        left_value = clean_state_value(left.get(key))
        right_value = clean_state_value(right.get(key))
        if left_value and right_value and left_value != right_value:
            raise ValidationError(
                "batch_state_mismatch: "
                f"{left_label} {key}={left_value} does not match "
                f"{right_label} {key}={right_value}. "
                "Regenerate downstream artifacts from the current note_plan and rerun "
                "stage-note/publish-batch --dry-run."
            )


def merge_batch_state(
    target: JsonObject,
    source: JsonObject,
    *,
    target_label: str,
    source_label: str,
    keys: tuple[str, ...] = BATCH_STATE_KEYS,
) -> dict[str, str]:
    require_compatible_batch_state(
        target,
        source,
        left_label=target_label,
        right_label=source_label,
        keys=keys,
    )
    merged: dict[str, str] = {}
    for key in keys:
        value = clean_state_value(source.get(key)) or clean_state_value(target.get(key))
        if value:
            target[key] = value
            merged[key] = value
    return merged
