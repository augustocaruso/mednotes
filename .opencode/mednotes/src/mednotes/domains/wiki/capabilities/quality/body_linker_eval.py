"""Redacted body-linker evaluation harness."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.body_link.body_linker import diagnose_body_links
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.kernel.base import JsonObject

BODY_LINKER_EVAL_REPORT_SCHEMA = "medical-notes-workbench.body-linker-eval-report.v1"
BODY_LINKER_EVAL_SUITE_SCHEMA = "medical-notes-workbench.body-linker-eval-suite.v1"


def _load_suite(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"Body linker eval suite not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid body linker eval suite JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != BODY_LINKER_EVAL_SUITE_SCHEMA:
        raise ValidationError(f"Expected {BODY_LINKER_EVAL_SUITE_SCHEMA}: {path}")
    if not isinstance(payload.get("cases"), list):
        raise ValidationError("Body linker eval suite requires cases[].")
    return payload


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return slug or "case"


def _mock_disambiguator(mock_decisions: list[JsonObject]):
    def fake(requests: list[dict[str, Any]], *, model: str, timeout_seconds: int) -> dict[str, Any]:
        unused = list(mock_decisions)
        decisions: list[JsonObject] = []
        for request in requests:
            surface = normalize_key(str(request.get("surface") or ""))
            selected: JsonObject | None = None
            for index, item in enumerate(unused):
                if normalize_key(str(item.get("surface") or "")) == surface:
                    selected = unused.pop(index)
                    break
            if selected is None and unused:
                selected = unused.pop(0)
            selected = selected or {"action": "defer", "confidence": 0.0, "reason_code": "missing_mock_decision"}
            decisions.append(
                {
                    "occurrence_id": request["occurrence_id"],
                    "action": str(selected.get("action") or "defer"),
                    "chosen_target": str(selected.get("chosen_target") or selected.get("target") or ""),
                    "chosen_meaning_id": str(selected.get("chosen_meaning_id") or ""),
                    "confidence": float(selected.get("confidence") or 0.0),
                    "reason_code": str(selected.get("reason_code") or "mock_decision"),
                    "rationale_summary": str(selected.get("rationale_summary") or "Mocked eval decision."),
                }
            )
        return {
            "schema": "medical-notes-workbench.contextual-alias-disambiguation.v1",
            "model": model,
            "decisions": decisions,
        }

    return fake


def _planned_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for plan in payload.get("plans", []) if isinstance(payload.get("plans"), list) else []:
        if not isinstance(plan, dict):
            continue
        for item in plan.get("insertions", []) if isinstance(plan.get("insertions"), list) else []:
            if isinstance(item, dict):
                actions.append(
                    {
                        "surface": str(item.get("term") or ""),
                        "action": "link",
                        "target": str(item.get("target") or ""),
                        "reason_code": str(item.get("reason_code") or ""),
                    }
                )
        for item in plan.get("skipped", []) if isinstance(plan.get("skipped"), list) else []:
            if isinstance(item, dict):
                actions.append(
                    {
                        "surface": str(item.get("term") or ""),
                        "action": str(item.get("action") or "defer"),
                        "target": str(item.get("target") or ""),
                        "reason_code": str(item.get("reason_code") or ""),
                    }
                )
    return actions


def _matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if normalize_key(str(expected.get("surface") or "")) != normalize_key(str(actual.get("surface") or "")):
        return False
    if str(expected.get("action") or "") != str(actual.get("action") or ""):
        return False
    if expected.get("action") == "link":
        return normalize_key(str(expected.get("target") or "")) == normalize_key(str(actual.get("target") or ""))
    return True


def _case_metrics(case: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    expected = [item for item in case.get("expected", []) if isinstance(item, dict)]
    actual = _planned_actions(payload)
    matched_actual: set[int] = set()
    true_positive_count = 0
    false_negative_count = 0
    deferred_count = 0
    failures: list[dict[str, Any]] = []
    for expected_item in expected:
        match_index = next(
            (index for index, actual_item in enumerate(actual) if index not in matched_actual and _matches(expected_item, actual_item)),
            None,
        )
        if match_index is None:
            false_negative_count += 1
            failures.append(
                {
                    "type": "missing_expected_action",
                    "surface": str(expected_item.get("surface") or ""),
                    "expected_action": str(expected_item.get("action") or ""),
                    "expected_target": str(expected_item.get("target") or ""),
                }
            )
            continue
        matched_actual.add(match_index)
        if expected_item.get("action") == "link":
            true_positive_count += 1
        elif expected_item.get("action") == "defer":
            deferred_count += 1
    false_positives = [
        item
        for index, item in enumerate(actual)
        if index not in matched_actual and item.get("action") == "link"
    ]
    false_positive_count = len(false_positives)
    for item in false_positives:
        failures.append(
            {
                "type": "unexpected_link",
                "surface": item.get("surface", ""),
                "target": item.get("target", ""),
                "reason_code": item.get("reason_code", ""),
            }
        )
    return {
        "name": str(case.get("name") or "case"),
        "expected_count": len(expected),
        "planned_action_count": len(actual),
        "true_positive_count": true_positive_count,
        "false_positive_count": false_positive_count,
        "false_negative_count": false_negative_count,
        "deferred_contextual_alias_count": deferred_count,
        "protected_zone_violation_count": 0,
        "failures": failures,
    }


def _surface_metrics(cases: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for case in cases:
        for expected in case.get("expected", []) if isinstance(case.get("expected"), list) else []:
            if not isinstance(expected, dict):
                continue
            surface = str(expected.get("surface") or "")
            item = metrics.setdefault(
                surface,
                {
                    "expected_count": 0,
                    "expected_link_count": 0,
                    "expected_defer_count": 0,
                },
            )
            item["expected_count"] += 1
            if expected.get("action") == "link":
                item["expected_link_count"] += 1
            elif expected.get("action") == "defer":
                item["expected_defer_count"] += 1
    return metrics


def evaluate_body_linker_cases(
    *,
    fixture_path: Path,
    vocabulary_db_path: Path,
    max_false_positive_rate: float = 0.0,
) -> dict[str, Any]:
    suite = _load_suite(fixture_path)
    cases = [item for item in suite.get("cases", []) if isinstance(item, dict)]
    case_reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="body-linker-eval-") as tmp:
        root = Path(tmp)
        for index, case in enumerate(cases, start=1):
            wiki_dir = root / f"case-{index:03d}" / "Wiki_Medicina"
            note_path = wiki_dir / "Eval" / f"{_slug(str(case.get('name') or str(index)))}.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(str(case.get("input_markdown") or ""), encoding="utf-8")
            mock_decisions = [item for item in case.get("mock_decisions", []) if isinstance(item, dict)]
            diagnosis = diagnose_body_links(
                wiki_dir=wiki_dir,
                db_path=vocabulary_db_path,
                llm_mode="auto" if mock_decisions else "off",
                llm_model="body-linker-eval-mock",
                llm_disambiguator=_mock_disambiguator(mock_decisions) if mock_decisions else None,
            )
            case_reports.append(_case_metrics(case, diagnosis.as_diagnosis_payload()))

    true_positive_count = sum(int(item["true_positive_count"]) for item in case_reports)
    false_positive_count = sum(int(item["false_positive_count"]) for item in case_reports)
    false_negative_count = sum(int(item["false_negative_count"]) for item in case_reports)
    deferred_count = sum(int(item["deferred_contextual_alias_count"]) for item in case_reports)
    protected_zone_violation_count = sum(int(item["protected_zone_violation_count"]) for item in case_reports)
    denominator = max(1, true_positive_count + false_positive_count)
    false_positive_rate = false_positive_count / denominator
    return {
        "schema": BODY_LINKER_EVAL_REPORT_SCHEMA,
        "suite_path": str(fixture_path),
        "vocabulary_db_path": str(vocabulary_db_path),
        "case_count": len(case_reports),
        "true_positive_count": true_positive_count,
        "false_positive_count": false_positive_count,
        "false_negative_count": false_negative_count,
        "deferred_contextual_alias_count": deferred_count,
        "protected_zone_violation_count": protected_zone_violation_count,
        "per_surface_metrics": _surface_metrics(cases),
        "quality_gate": {
            "status": "passed" if false_positive_rate <= max_false_positive_rate else "failed",
            "max_false_positive_rate": max_false_positive_rate,
            "actual_false_positive_rate": false_positive_rate,
        },
        "cases": case_reports,
    }
