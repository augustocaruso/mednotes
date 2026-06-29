"""JSON response parsing for Gemini outputs."""
from __future__ import annotations

import json
import re

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _strip_fences(s: str) -> str:
    m = _JSON_FENCE_RE.search(s)
    return m.group(1) if m else s.strip()


def parse_anchors_json(raw: str) -> list[dict]:
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"esperava lista de âncoras, recebi {type(data).__name__}")
    out = []
    for i, a in enumerate(data):
        for k in ("section_path", "concept", "visual_type", "search_queries"):
            if k not in a:
                raise ValueError(f"âncora #{i} sem chave obrigatória {k!r}")
        a.setdefault("anchor_id", f"a{i+1}")
        out.append(a)
    return out


def parse_rerank_json(raw: str) -> dict:
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    if "chosen_index" not in data:
        raise ValueError("rerank sem chave 'chosen_index'")
    if "minimum_quality_met" not in data:
        data["minimum_quality_met"] = data.get("chosen_index") is not None
    candidates = data.get("candidates", [])
    if candidates is None:
        candidates = []
    if not isinstance(candidates, list):
        raise ValueError("rerank candidates precisa ser lista")
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("cada item de candidates precisa ser objeto")
        if "index" not in item:
            raise ValueError("candidate rubric sem index")
    data["candidates"] = candidates
    return data
