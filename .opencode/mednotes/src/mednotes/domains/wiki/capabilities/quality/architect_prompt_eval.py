"""Offline prompt-quality evaluation for med-knowledge-architect outputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.graph.coverage import RAW_COVERAGE_SCHEMA
from mednotes.domains.wiki.capabilities.notes.note_plan import PLANNED_MEANING_ACTION, TRIAGE_NOTE_PLAN_SCHEMA
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.common import ValidationError

ARCHITECT_PROMPT_EVAL_SCHEMA = "medical-notes-workbench.architect-prompt-eval.v1"


def canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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


def _issue(*, code: str, severity: str, rubric_key: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "rubric_key": rubric_key, "message": message}


def _forbidden_key_hits(value: Any, forbidden: set[str], *, prefix: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}"
            if key_text in forbidden:
                hits.append(path)
            hits.extend(_forbidden_key_hits(nested, forbidden, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(_forbidden_key_hits(nested, forbidden, prefix=f"{prefix}[{index}]"))
    return hits


def _planned_meaning_titles_from_note_plan(note_plan: dict[str, Any]) -> set[str]:
    if note_plan.get("schema") != TRIAGE_NOTE_PLAN_SCHEMA:
        raise ValidationError(f"architect work item note_plan must use schema {TRIAGE_NOTE_PLAN_SCHEMA}")
    if note_plan.get("exhaustive") is not True:
        raise ValidationError("architect work item note_plan must be exhaustive")
    items = note_plan.get("items")
    if not isinstance(items, list):
        raise ValidationError("architect work item note_plan requires items[]")
    return {
        str(item.get("staged_title") or item.get("title") or "").strip()
        for item in items
        if isinstance(item, dict)
        and item.get("action") == PLANNED_MEANING_ACTION
        and str(item.get("staged_title") or item.get("title") or "").strip()
    }


def _planned_meaning_titles_from_coverage(coverage: dict[str, Any]) -> set[str]:
    items = coverage.get("items")
    if not isinstance(items, list):
        return set()
    return {
        str(item.get("staged_title") or item.get("title") or "").strip()
        for item in items
        if isinstance(item, dict)
        and item.get("action") == PLANNED_MEANING_ACTION
        and str(item.get("staged_title") or item.get("title") or "").strip()
    }


def _note_titles(notes: list[dict[str, Any]]) -> set[str]:
    return {str(note.get("title") or "").strip() for note in notes if str(note.get("title") or "").strip()}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _score(issues: list[dict[str, str]]) -> int:
    penalty = 0
    for issue in issues:
        penalty += 25 if issue.get("severity") == "error" else 10
    return max(0, 100 - penalty)


def _metrics_summary(agent_metrics: dict[str, Any] | None) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    if not isinstance(agent_metrics, dict):
        return [
            _issue(
                code="missing_agent_metrics",
                severity="error",
                rubric_key="efficiency_routing",
                message="agent_metrics is required so architect prompt quality can be evaluated.",
            )
        ], {"present": False}
    token_accounting = str(agent_metrics.get("token_accounting") or "")
    turns_used = int(agent_metrics.get("turns_used") or 0)
    prompt_tokens = int(agent_metrics.get("prompt_tokens") or 0)
    completion_tokens = int(agent_metrics.get("completion_tokens") or 0)
    retries = int(agent_metrics.get("retries") or 0)
    if token_accounting not in {"exact", "estimated", "unavailable"}:
        issues.append(
            _issue(
                code="agent_metrics_token_accounting_missing",
                severity="warning",
                rubric_key="efficiency_routing",
                message="agent_metrics.token_accounting must be exact, estimated, or unavailable.",
            )
        )
    if turns_used > 24:
        issues.append(
            _issue(
                code="turn_budget_exceeded",
                severity="warning",
                rubric_key="efficiency_routing",
                message=f"turns_used={turns_used} exceeds architect max_turns=24.",
            )
        )
    return issues, {
        "present": True,
        "token_accounting": token_accounting,
        "turns_used": turns_used,
        "max_turns": 24,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "retries": retries,
    }


def _compare_to_baseline(*, current: dict[str, Any], baseline_path: Path) -> dict[str, Any]:
    baseline = _read_json_object(baseline_path, label="architect prompt eval baseline")
    if baseline.get("schema") != ARCHITECT_PROMPT_EVAL_SCHEMA:
        raise ValidationError(f"architect prompt eval baseline must use schema {ARCHITECT_PROMPT_EVAL_SCHEMA}")
    current_aggregate = current.get("aggregate") if isinstance(current.get("aggregate"), dict) else {}
    baseline_aggregate = baseline.get("aggregate") if isinstance(baseline.get("aggregate"), dict) else {}
    current_efficiency = current_aggregate.get("efficiency") if isinstance(current_aggregate.get("efficiency"), dict) else {}
    baseline_efficiency = baseline_aggregate.get("efficiency") if isinstance(baseline_aggregate.get("efficiency"), dict) else {}
    comparison: dict[str, Any] = {
        "baseline_status": str(baseline.get("status") or ""),
        "current_status": str(current.get("status") or ""),
        "score_delta": int(current_aggregate.get("score") or 0) - int(baseline_aggregate.get("score") or 0),
        "issue_count_delta": int(current_aggregate.get("issue_count") or 0)
        - int(baseline_aggregate.get("issue_count") or 0),
        "total_prompt_tokens_delta": int(current_efficiency.get("total_prompt_tokens") or 0)
        - int(baseline_efficiency.get("total_prompt_tokens") or 0),
        "total_completion_tokens_delta": int(current_efficiency.get("total_completion_tokens") or 0)
        - int(baseline_efficiency.get("total_completion_tokens") or 0),
        "total_retries_delta": int(current_efficiency.get("total_retries") or 0)
        - int(baseline_efficiency.get("total_retries") or 0),
    }
    regression_flags: list[str] = []
    if comparison["baseline_status"] == "pass" and comparison["current_status"] != "pass":
        regression_flags.append("status_regression")
    if int(comparison["score_delta"]) < 0:
        regression_flags.append("score_regression")
    if int(comparison["issue_count_delta"]) > 0:
        regression_flags.append("issue_count_regression")
    if int(comparison["total_prompt_tokens_delta"]) > 0:
        regression_flags.append("prompt_token_regression")
    if int(comparison["total_completion_tokens_delta"]) > 0:
        regression_flags.append("completion_token_regression")
    if int(comparison["total_retries_delta"]) > 0:
        regression_flags.append("retry_regression")
    comparison["regression_flags"] = regression_flags
    comparison["status"] = "regressed" if regression_flags else "improved_or_equal"
    return comparison


def evaluate_architect_prompt_outputs(
    *,
    work_item: dict[str, Any],
    coverage_path: Path,
    notes: list[dict[str, Any]],
    agent_metrics: dict[str, Any] | None,
    baseline_eval_path: Path | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if str(work_item.get("agent") or "") != "med-knowledge-architect":
        issues.append(
            _issue(
                code="unexpected_agent",
                severity="error",
                rubric_key="scope_control",
                message="architect prompt eval expects med-knowledge-architect work items.",
            )
        )
    raw_file = str(work_item.get("raw_file") or "")
    temp_dir = Path(str(work_item.get("temp_dir") or ""))
    note_plan = work_item.get("note_plan") if isinstance(work_item.get("note_plan"), dict) else {}
    planned_titles = _planned_meaning_titles_from_note_plan(note_plan)
    coverage = _read_json_object(coverage_path, label="raw coverage")
    if coverage.get("schema") != RAW_COVERAGE_SCHEMA:
        issues.append(
            _issue(
                code="coverage_invalid_schema",
                severity="error",
                rubric_key="coverage_contract",
                message=f"coverage must use schema {RAW_COVERAGE_SCHEMA}.",
            )
        )
    if str(coverage.get("raw_file") or "") != raw_file:
        issues.append(
            _issue(
                code="coverage_raw_file_mismatch",
                severity="error",
                rubric_key="coverage_contract",
                message="coverage raw_file differs from assigned raw_file.",
            )
        )
    if coverage.get("exhaustive") is not True:
        issues.append(
            _issue(
                code="coverage_not_exhaustive",
                severity="error",
                rubric_key="coverage_contract",
                message="coverage must be exhaustive.",
            )
        )
    coverage_titles = _planned_meaning_titles_from_coverage(coverage)
    note_titles = _note_titles(notes)
    missing_coverage = sorted(planned_titles - coverage_titles, key=normalize_key)
    if missing_coverage:
        issues.append(
            _issue(
                code="coverage_missing_planned_meaning",
                severity="error",
                rubric_key="coverage_contract",
                message="coverage missing planned_meaning targets: " + ", ".join(missing_coverage),
            )
        )
    missing_notes = sorted(planned_titles - note_titles, key=normalize_key)
    if missing_notes:
        issues.append(
            _issue(
                code="staged_note_missing_for_planned_meaning",
                severity="error",
                rubric_key="output_contract",
                message="notes missing planned_meaning targets: " + ", ".join(missing_notes),
            )
        )
    forbidden = {"raw_markdown", "clinical_body", "html", "images", "embeddings", "api_keys"}
    for path in _forbidden_key_hits({"work_item": work_item, "coverage": coverage, "notes": notes}, forbidden):
        issues.append(
            _issue(
                code="forbidden_output_key",
                severity="error",
                rubric_key="evidence_redaction",
                message=f"forbidden evidence key present at {path}",
            )
        )
    for note in notes:
        content_path = Path(str(note.get("content_path") or ""))
        if not str(note.get("title") or "").strip():
            issues.append(
                _issue(
                    code="note_output_missing_title",
                    severity="error",
                    rubric_key="output_contract",
                    message="note output is missing title.",
                )
            )
        if not content_path.is_file():
            issues.append(
                _issue(
                    code="note_content_path_missing",
                    severity="error",
                    rubric_key="output_contract",
                    message=f"note content_path not found: {content_path}",
                )
            )
        elif not _is_relative_to(content_path, temp_dir):
            issues.append(
                _issue(
                    code="note_path_outside_temp_dir",
                    severity="error",
                    rubric_key="scope_control",
                    message="architect output note is outside parent-supplied temp_dir.",
                )
            )
    metric_issues, metrics = _metrics_summary(agent_metrics)
    issues.extend(metric_issues)
    score = _score(issues)
    result = {
        "schema": ARCHITECT_PROMPT_EVAL_SCHEMA,
        "phase": "architect",
        "input_fingerprints": {
            "work_item_hash": canonical_payload_hash(work_item),
            "coverage_hash": canonical_payload_hash(coverage),
            "notes_hash": canonical_payload_hash(notes),
        },
        "status": "pass" if not issues else "needs_review",
        "aggregate": {
            "score": score,
            "issue_count": len(issues),
            "quality_flags": [] if not issues else ["architect_prompt_contract_needs_review"],
            "coverage": {
                "planned_create_count": len(planned_titles),
                "coverage_create_count": len(coverage_titles),
                "staged_note_count": len(note_titles),
            },
            "metric_coverage": {
                "status": "complete" if metrics.get("present") else "missing",
                "items_with_agent_metrics": 1 if metrics.get("present") else 0,
                "items_total": 1,
            },
            "efficiency": {
                "total_prompt_tokens": int(metrics.get("prompt_tokens") or 0),
                "total_completion_tokens": int(metrics.get("completion_tokens") or 0),
                "total_retries": int(metrics.get("retries") or 0),
                "turns_used": int(metrics.get("turns_used") or 0),
            },
        },
        "issues": issues,
        "agent_metrics": metrics,
        "next_action": "" if not issues else "revisar output do architect antes de stage-note",
    }
    if baseline_eval_path is not None:
        comparison = _compare_to_baseline(current=result, baseline_path=baseline_eval_path)
        result["comparison"] = comparison
        if comparison.get("status") == "regressed":
            result["aggregate"]["quality_flags"].append("baseline_regression")
            result["status"] = "needs_review"
            result["next_action"] = "revisar regressao contra baseline antes de stage-note"
    return result
