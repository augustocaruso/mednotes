"""Local reporting helpers for image-enrichment quality decisions."""
from __future__ import annotations

import json
from pathlib import Path

from mednotes.kernel.base import JsonObject, JsonObjectAdapter


def write_quality_report(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = JsonObjectAdapter.validate_python(payload)
    path.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def note_report(path: Path, status: str, anchors: list[JsonObject]) -> JsonObject:
    return {
        "path": str(path),
        "status": status,
        "anchors": anchors,
    }
