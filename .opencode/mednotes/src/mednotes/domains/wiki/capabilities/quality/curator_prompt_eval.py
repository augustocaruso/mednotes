"""Offline prompt-quality evaluation for med-link-graph-curator outputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import (
    VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA,
    VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA,
    curator_plan_hash,
)
from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.contracts.curator import LinkPolicy
from mednotes.kernel.base import JsonObject, JsonObjectAdapter, JsonValue

CURATOR_PROMPT_EVAL_SCHEMA = "medical-notes-workbench.curator-prompt-eval.v1"
CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA = (
    "medical-notes-workbench.curator-prompt-golden-expectations.v1"
)
CURATOR_PROMPT_EXPECTATIONS_SCHEMA = CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA


def _json_object_from_model(model: BaseModel, **dump_options: Any) -> JsonObject:
    # Prompt eval reports cross a JSON boundary before they gate DB mutation;
    # every field used for promotion is parsed into this local contract first.
    return JsonObjectAdapter.validate_python(model.model_dump(mode="json", by_alias=True, **dump_options))


class _CuratorPromotionInputFingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_hash: StrictStr
    manifest_hash: StrictStr
    prompt_identity_hash: StrictStr = ""
    evaluation_expectations_present: bool = False
    evaluation_expectations_hash: StrictStr = ""


class _CuratorPromotionExpectationCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items_with_expectations: int = 0
    items_total: int = 0
    failed_expectation_count: int = 0
    unused_expectation_count: int = 0
    status: StrictStr = ""


class _CuratorPromotionAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = 0
    item_count: int = 0
    issue_count: int = 0
    redaction_issue_count: int = 0
    quality_flags: list[StrictStr] = Field(default_factory=list)
    route_counts: JsonObject = Field(default_factory=dict)
    metric_coverage: JsonObject = Field(default_factory=dict)
    efficiency: JsonObject = Field(default_factory=dict)
    expectation_coverage: _CuratorPromotionExpectationCoverage = Field(
        default_factory=_CuratorPromotionExpectationCoverage
    )
    unused_expectation_work_ids: list[StrictStr] = Field(default_factory=list)


class _CuratorPromotionEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["medical-notes-workbench.curator-prompt-eval.v1"] = Field(alias="schema")
    phase: StrictStr = ""
    prompt_identity: JsonObject = Field(default_factory=dict)
    input_fingerprints: _CuratorPromotionInputFingerprints
    prompt_eval_context: JsonObject = Field(default_factory=dict)
    status: StrictStr
    aggregate: _CuratorPromotionAggregate
    items: list[JsonObject] = Field(default_factory=list)
    aggregate_issues: list[JsonObject] = Field(default_factory=list)
    next_action: StrictStr = ""
    comparison: JsonObject | None = None
    baseline_metadata: JsonObject | None = None

    def to_payload(self) -> JsonObject:
        return _json_object_from_model(self, exclude_none=True)


@dataclass(frozen=True)
class _AliasLinkPolicyProjection:
    link_policy: str

    @classmethod
    def from_payload(cls, payload: JsonObject) -> _AliasLinkPolicyProjection:
        # This projection deliberately reads only the field needed for direct
        # alias counting; shape validation for full curator outputs remains in
        # the vocabulary curator contracts.
        value = payload["link_policy"] if "link_policy" in payload else ""
        return cls(link_policy=value.strip() if isinstance(value, str) else "")


def _canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def canonical_payload_hash(payload: Any) -> str:
    return _canonical_payload_hash(payload)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return JsonObjectAdapter.validate_python(payload)


def load_curator_prompt_expectations(path: Path, *, expected_plan_hash: str | None = None) -> dict[str, Any]:
    payload = _read_json_object(path, label="curator prompt golden expectations")
    if payload.get("schema") != CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA:
        raise ValidationError(
            f"curator prompt golden expectations must use schema {CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA}"
        )
    source_plan_hash = str(payload.get("source_plan_hash") or "")
    if expected_plan_hash and source_plan_hash and source_plan_hash != expected_plan_hash:
        raise ValidationError("curator prompt golden expectations source_plan_hash mismatch")
    expectations = payload.get("expectations_by_work_id")
    if not isinstance(expectations, dict):
        raise ValidationError("curator prompt golden expectations require expectations_by_work_id")
    normalized: dict[str, Any] = {}
    for work_id, expectation in expectations.items():
        if not isinstance(expectation, dict):
            raise ValidationError(f"expectation for work_id {work_id} must be a JSON object")
        normalized[str(work_id)] = expectation
    return normalized


def promote_curator_prompt_baseline(eval_path: Path) -> dict[str, Any]:
    try:
        report = _CuratorPromotionEvalReport.model_validate(_read_json_object(eval_path, label="curator prompt eval"))
    except PydanticValidationError as exc:
        raise ValidationError("curator prompt eval baseline promotion requires a valid eval report") from exc
    if report.schema_ != CURATOR_PROMPT_EVAL_SCHEMA:
        raise ValidationError(f"curator prompt eval must use schema {CURATOR_PROMPT_EVAL_SCHEMA}")
    if report.status != "pass":
        raise ValidationError("curator prompt baseline promotion requires status=pass")
    fingerprints = report.input_fingerprints
    if not fingerprints.plan_hash or not fingerprints.manifest_hash:
        raise ValidationError("curator prompt baseline promotion requires input_fingerprints plan_hash and manifest_hash")
    if not fingerprints.evaluation_expectations_present:
        raise ValidationError("curator prompt baseline promotion requires golden expectations")
    expectation_coverage = report.aggregate.expectation_coverage
    if expectation_coverage.status != "complete":
        raise ValidationError("curator prompt baseline promotion requires complete golden expectation coverage")
    if expectation_coverage.unused_expectation_count != 0:
        raise ValidationError("curator prompt baseline promotion rejects unused golden expectations")
    baseline = report.to_payload()
    baseline["baseline_metadata"] = {
        "status": "active",
        "source_eval_path": str(eval_path),
        "source_eval_hash": _canonical_payload_hash(report.to_payload()),
    }
    return baseline


def build_curator_prompt_expectations_template(plan: dict[str, Any]) -> dict[str, Any]:
    by_work_id = _plan_items(plan)
    items: list[dict[str, str]] = []
    expectations: dict[str, dict[str, Any]] = {}
    for work_id, item in by_work_id.items():
        route = item.get("difficulty_route") if isinstance(item.get("difficulty_route"), dict) else {}
        items.append(
            {
                "work_id": work_id,
                "note_path": str(item.get("note_path") or ""),
                "title": str(item.get("title") or ""),
                "route": str(route.get("route") or ""),
            }
        )
        expectations[work_id] = {
            "primary_label": "",
            "required_aliases": [],
            "expected_alias_policies": {},
            "forbidden_direct_aliases": [],
            "expected_deferred_work_codes": [],
        }
    return {
        "schema": CURATOR_PROMPT_GOLDEN_EXPECTATIONS_SCHEMA,
        "source_plan_hash": curator_plan_hash(plan),
        "item_count": len(items),
        "items": items,
        "expectations_by_work_id": expectations,
    }


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


def _manifest_payload_and_items(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
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
    return manifest, items


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


def _direct_alias_count(payload: JsonObject) -> int:
    aliases = _json_field(payload, "aliases")
    if not isinstance(aliases, list):
        return 0
    total = 0
    for alias in aliases:
        if isinstance(alias, dict) and _AliasLinkPolicyProjection.from_payload(alias).link_policy == LinkPolicy.DIRECT:
            total += 1
    return total


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _alias_entries(payload: JsonObject) -> list[JsonObject]:
    aliases = _json_field(payload, "aliases")
    if not isinstance(aliases, list):
        return []
    return [alias for alias in aliases if isinstance(alias, dict)]


def _golden_assertion_count(expectations: JsonObject) -> int:
    count = 0
    if _norm_text(_json_field(expectations, "primary_label")):
        count += 1
    required_aliases = _json_field(expectations, "required_aliases")
    if isinstance(required_aliases, list):
        count += sum(1 for alias in required_aliases if _norm_text(alias))
    expected_policies = _json_field(expectations, "expected_alias_policies")
    if isinstance(expected_policies, dict):
        count += sum(1 for alias_text in expected_policies if _norm_text(alias_text))
    forbidden_direct_aliases = _json_field(expectations, "forbidden_direct_aliases")
    if isinstance(forbidden_direct_aliases, list):
        count += sum(1 for alias in forbidden_direct_aliases if _norm_text(alias))
    expected_deferred_codes = _json_field(expectations, "expected_deferred_work_codes")
    if isinstance(expected_deferred_codes, list):
        count += sum(1 for code in expected_deferred_codes if _norm_text(code))
    return count


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source.get(key, default)


def _expectation_issues(*, expected: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, str]]:
    expectations = expected.get("evaluation_expectations")
    if not isinstance(expectations, dict):
        return []

    issues: list[dict[str, str]] = []
    expected_label = expectations.get("primary_label")
    required_aliases = expectations.get("required_aliases")
    expected_policies = expectations.get("expected_alias_policies")
    forbidden_direct_aliases = expectations.get("forbidden_direct_aliases")
    expected_deferred_codes = expectations.get("expected_deferred_work_codes")
    assertion_count = _golden_assertion_count(expectations)
    if assertion_count == 0:
        issues.append(
            _issue(
                code="empty_golden_expectations",
                severity="error",
                rubric_key="golden_expectations",
                message="golden expectations must contain at least one actionable assertion",
            )
        )
    elif assertion_count < 2:
        issues.append(
            _issue(
                code="weak_golden_expectations",
                severity="error",
                rubric_key="golden_expectations",
                message="golden expectations must contain at least two actionable assertions",
            )
        )

    primary = payload.get("primary_meaning") if isinstance(payload.get("primary_meaning"), dict) else {}
    if expected_label and _norm_text(primary.get("label")) != _norm_text(expected_label):
        issues.append(
            _issue(
                code="expected_primary_label_mismatch",
                severity="error",
                rubric_key="golden_expectations",
                message="primary_meaning.label does not match evaluation_expectations.primary_label",
            )
        )

    aliases = _alias_entries(payload)
    alias_texts = {_norm_text(alias.get("text")) for alias in aliases}
    if isinstance(required_aliases, list):
        for alias in required_aliases:
            if _norm_text(alias) not in alias_texts:
                issues.append(
                    _issue(
                        code="missing_required_alias",
                        severity="error",
                        rubric_key="golden_expectations",
                        message=f"required alias absent: {alias}",
                    )
                )

    if isinstance(expected_policies, dict):
        for alias_text, expected_policy in expected_policies.items():
            matches = [alias for alias in aliases if _norm_text(alias.get("text")) == _norm_text(alias_text)]
            if not matches:
                issues.append(
                    _issue(
                        code="missing_expected_alias_policy",
                        severity="error",
                        rubric_key="golden_expectations",
                        message=f"alias with expected policy absent: {alias_text}",
                    )
                )
                continue
            if not any(str(alias.get("link_policy") or "") == str(expected_policy) for alias in matches):
                issues.append(
                    _issue(
                        code="alias_policy_mismatch",
                        severity="error",
                        rubric_key="golden_expectations",
                        message=f"alias policy mismatch for {alias_text}",
                    )
                )

    if isinstance(forbidden_direct_aliases, list):
        forbidden = {_norm_text(alias) for alias in forbidden_direct_aliases}
        for alias in aliases:
            if _norm_text(alias.get("text")) in forbidden and str(alias.get("link_policy") or "") == "direct":
                issues.append(
                    _issue(
                        code="forbidden_direct_alias",
                        severity="error",
                        rubric_key="golden_expectations",
                        message=f"alias must not be direct: {alias.get('text')}",
                    )
                )

    if isinstance(expected_deferred_codes, list):
        deferred = payload.get("deferred_work_items")
        actual_codes = {
            _norm_text(item.get("code") or item.get("reason") or item.get("type"))
            for item in deferred
            if isinstance(item, dict)
        } if isinstance(deferred, list) else set()
        for code in expected_deferred_codes:
            if _norm_text(code) not in actual_codes:
                issues.append(
                    _issue(
                        code="missing_expected_deferred_work",
                        severity="error",
                        rubric_key="golden_expectations",
                        message=f"expected deferred work absent: {code}",
                    )
                )

    return issues


def _has_complex_signal(payload: dict[str, Any]) -> bool:
    deferred = payload.get("deferred_work_items")
    duplicates = payload.get("duplicate_candidates")
    split_warning = payload.get("split_warning")
    primary = payload.get("primary_meaning")
    atomic_status = str(primary.get("atomic_status") or "") if isinstance(primary, dict) else ""
    return (
        (isinstance(deferred, list) and len(deferred) > 0)
        or (isinstance(duplicates, list) and len(duplicates) > 0)
        or bool(split_warning)
        or atomic_status in {"non_atomic", "split_candidate", "uncertain"}
    )


def _agent_metrics(payload: dict[str, Any]) -> dict[str, Any] | None:
    metrics = payload.get("agent_metrics")
    return metrics if isinstance(metrics, dict) else None


def _evaluate_payload(*, expected: dict[str, Any], payload: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    output_contract = expected.get("output_contract") if isinstance(expected.get("output_contract"), dict) else {}
    required = output_contract.get("must_include") if isinstance(output_contract.get("must_include"), list) else []
    forbidden = output_contract.get("must_not_include") if isinstance(output_contract.get("must_not_include"), list) else []

    missing = [str(key) for key in required if str(key) not in payload]
    if missing:
        issues.append(
            _issue(
                code="missing_output_contract_fields",
                severity="error",
                rubric_key="output_contract",
                message=f"missing required fields: {', '.join(missing)}",
            )
        )

    forbidden_hits = _forbidden_key_hits(payload, {str(key) for key in forbidden})
    for path in forbidden_hits:
        issues.append(
            _issue(
                code="forbidden_output_key",
                severity="error",
                rubric_key="evidence_redaction",
                message=f"forbidden evidence key present at {path}",
            )
        )
    issues.extend(_expectation_issues(expected=expected, payload=payload))

    route = expected.get("difficulty_route") if isinstance(expected.get("difficulty_route"), dict) else {}
    route_name = str(route.get("route") or "unknown")
    if route_name == "simple_atomic" and _direct_alias_count(payload) > 3:
        issues.append(
            _issue(
                code="too_many_direct_aliases_for_simple_route",
                severity="warning",
                rubric_key="alias_precision",
                message="simple_atomic output has more than three direct aliases; review for over-broad surfaces",
            )
        )
    if route_name == "complex_semantic_review" and not _has_complex_signal(payload):
        issues.append(
            _issue(
                code="complex_route_without_defer_or_split_signal",
                severity="error",
                rubric_key="defer_when_uncertain",
                message="complex route output did not include deferred work, duplicate candidates, split warning, or uncertain atomic status",
            )
        )

    metrics = _agent_metrics(payload)
    metrics_summary: dict[str, Any] = {"present": metrics is not None}
    if metrics is None:
        issues.append(
            _issue(
                code="missing_agent_metrics",
                severity="error",
                rubric_key="efficiency_routing",
                message="agent_metrics is required so prompt quality can be evaluated for efficiency",
            )
        )
    else:
        max_turns = int(route.get("max_turns") or 0)
        turns_used = int(metrics.get("turns_used") or 0)
        prompt_tokens = int(metrics.get("prompt_tokens") or 0)
        completion_tokens = int(metrics.get("completion_tokens") or 0)
        retries = int(metrics.get("retries") or 0)
        token_accounting = str(metrics.get("token_accounting") or "")
        metrics_summary.update(
            {
                "token_accounting": token_accounting,
                "turns_used": turns_used,
                "max_turns": max_turns,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "retries": retries,
            }
        )
        if max_turns and turns_used > max_turns:
            issues.append(
                _issue(
                    code="turn_budget_exceeded",
                    severity="warning",
                    rubric_key="efficiency_routing",
                    message=f"turns_used={turns_used} exceeds route max_turns={max_turns}",
                )
            )
        if token_accounting not in {"exact", "estimated", "unavailable"}:
            issues.append(
                _issue(
                    code="agent_metrics_token_accounting_missing",
                    severity="warning",
                    rubric_key="efficiency_routing",
                    message="agent_metrics.token_accounting must be exact, estimated, or unavailable",
                )
            )
        if retries > 1:
            issues.append(
                _issue(
                    code="retry_count_high",
                    severity="warning",
                    rubric_key="efficiency_routing",
                    message=f"retries={retries}; inspect prompt clarity or packet completeness",
                )
            )
    return issues, metrics_summary


def _score(issues: list[dict[str, str]]) -> int:
    penalty = 0
    for issue in issues:
        penalty += 25 if issue.get("severity") == "error" else 10
    return max(0, 100 - penalty)


def _aggregate_efficiency(report: dict[str, Any]) -> dict[str, Any]:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
    return aggregate.get("efficiency") if isinstance(aggregate.get("efficiency"), dict) else {}


def _input_fingerprints(report: dict[str, Any]) -> dict[str, Any]:
    fingerprints = report.get("input_fingerprints")
    return fingerprints if isinstance(fingerprints, dict) else {}


def _compare_to_baseline(*, current: dict[str, Any], baseline_path: Path) -> dict[str, Any]:
    baseline = _read_json_object(baseline_path, label="curator prompt eval baseline")
    if baseline.get("schema") != CURATOR_PROMPT_EVAL_SCHEMA:
        raise ValidationError(f"curator prompt eval baseline must use schema {CURATOR_PROMPT_EVAL_SCHEMA}")
    current_aggregate = current.get("aggregate") if isinstance(current.get("aggregate"), dict) else {}
    baseline_aggregate = baseline.get("aggregate") if isinstance(baseline.get("aggregate"), dict) else {}
    current_efficiency = _aggregate_efficiency(current)
    baseline_efficiency = _aggregate_efficiency(baseline)
    current_prompt = current.get("prompt_identity") if isinstance(current.get("prompt_identity"), dict) else {}
    baseline_prompt = baseline.get("prompt_identity") if isinstance(baseline.get("prompt_identity"), dict) else {}
    current_fingerprints = _input_fingerprints(current)
    baseline_fingerprints = _input_fingerprints(baseline)
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
        "turn_budget_exceeded_count_delta": int(current_efficiency.get("turn_budget_exceeded_count") or 0)
        - int(baseline_efficiency.get("turn_budget_exceeded_count") or 0),
        "prompt_identity_changed": str(current_prompt.get("aggregate_hash") or "")
        != str(baseline_prompt.get("aggregate_hash") or ""),
    }
    comparability_flags: list[str] = []
    baseline_metadata = baseline.get("baseline_metadata") if isinstance(baseline.get("baseline_metadata"), dict) else {}
    if baseline_metadata.get("status") != "active":
        comparability_flags.append("baseline_not_promoted")
    current_metadata = current.get("baseline_metadata") if isinstance(current.get("baseline_metadata"), dict) else {}
    if current_metadata and current_metadata.get("status") != "active":
        comparability_flags.append("current_baseline_metadata_invalid")
    current_expectations_present = bool(current_fingerprints.get("evaluation_expectations_present"))
    baseline_expectations_present = bool(baseline_fingerprints.get("evaluation_expectations_present"))
    if not baseline_expectations_present:
        comparability_flags.append("baseline_missing_golden_expectations")
    if not current_expectations_present:
        comparability_flags.append("current_missing_golden_expectations")
    if current_expectations_present or baseline_expectations_present:
        current_expectations_hash = str(current_fingerprints.get("evaluation_expectations_hash") or "")
        baseline_expectations_hash = str(baseline_fingerprints.get("evaluation_expectations_hash") or "")
        if current_expectations_hash != baseline_expectations_hash:
            comparability_flags.append("evaluation_expectations_changed")
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
    if int(comparison["turn_budget_exceeded_count_delta"]) > 0:
        regression_flags.append("turn_budget_regression")
    comparison["comparability_flags"] = comparability_flags
    comparison["regression_flags"] = regression_flags
    if comparability_flags:
        comparison["status"] = "not_comparable"
    else:
        comparison["status"] = "regressed" if regression_flags else "improved_or_equal"
    return comparison


def evaluate_curator_prompt_outputs(
    *,
    plan: dict[str, Any],
    manifest_path: Path,
    baseline_eval_path: Path | None = None,
) -> dict[str, Any]:
    by_work_id = _plan_items(plan)
    expectations_by_work_id = (
        plan.get("evaluation_expectations_by_work_id")
        if isinstance(plan.get("evaluation_expectations_by_work_id"), dict)
        else {}
    )
    manifest, manifest_items = _manifest_payload_and_items(manifest_path)
    items: list[dict[str, Any]] = []
    route_counts: dict[str, int] = {}
    metrics_present = 0
    redaction_issue_count = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_retries = 0
    total_turns_used = 0
    turn_budget_exceeded_count = 0
    expectation_items = 0
    failed_expectation_count = 0
    expectations_active = bool(expectations_by_work_id)
    manifest_work_ids = {str(item["work_id"]) for item in manifest_items}
    unused_expectation_work_ids = sorted(str(work_id) for work_id in set(expectations_by_work_id) - manifest_work_ids)
    aggregate_issues: list[dict[str, str]] = []

    for manifest_item in manifest_items:
        work_id = manifest_item["work_id"]
        output_path = Path(manifest_item["output_path"])
        expected = by_work_id.get(work_id)
        expected_for_eval: dict[str, Any] | None = expected
        if isinstance(expected, dict) and isinstance(expectations_by_work_id.get(work_id), dict):
            expected_for_eval = dict(expected)
            expected_for_eval["evaluation_expectations"] = expectations_by_work_id[work_id]
        has_expectations = bool(
            isinstance(expected_for_eval, dict) and isinstance(expected_for_eval.get("evaluation_expectations"), dict)
        )
        evaluation_expectations = expected_for_eval.get("evaluation_expectations") if isinstance(expected_for_eval, dict) else None
        assertion_count = _golden_assertion_count(evaluation_expectations) if isinstance(evaluation_expectations, dict) else 0
        if expected is None:
            issues = [
                _issue(
                    code="unknown_work_id",
                    severity="error",
                    rubric_key="output_contract",
                    message="manifest work_id is absent from plan",
                )
            ]
            route_name = "unknown"
            metrics_summary = {"present": False}
        else:
            expected_for_eval = expected_for_eval or {}
            route = expected.get("difficulty_route") if isinstance(expected.get("difficulty_route"), dict) else {}
            route_name = str(route.get("route") or "unknown")
            try:
                payload = _read_json_object(output_path, label="curator batch output")
            except ValidationError as exc:
                issues = [
                    _issue(
                        code="invalid_output_json",
                        severity="error",
                        rubric_key="output_contract",
                        message=str(exc),
                    )
                ]
                metrics_summary = {"present": False}
            else:
                issues, metrics_summary = _evaluate_payload(expected=expected_for_eval, payload=payload)
            if expectations_active and not has_expectations:
                issues.append(
                    _issue(
                        code="missing_golden_expectations",
                        severity="error",
                        rubric_key="golden_expectations",
                        message="golden expectations missing for work_id",
                    )
                )
        route_counts[route_name] = route_counts.get(route_name, 0) + 1
        expectation_issue_count = sum(1 for issue in issues if issue.get("rubric_key") == "golden_expectations")
        if has_expectations:
            expectation_items += 1
        if expectations_active:
            failed_expectation_count += expectation_issue_count
        if metrics_summary.get("present"):
            metrics_present += 1
            total_prompt_tokens += int(metrics_summary.get("prompt_tokens") or 0)
            total_completion_tokens += int(metrics_summary.get("completion_tokens") or 0)
            total_retries += int(metrics_summary.get("retries") or 0)
            total_turns_used += int(metrics_summary.get("turns_used") or 0)
        redaction_issue_count += sum(1 for issue in issues if issue.get("rubric_key") == "evidence_redaction")
        turn_budget_exceeded_count += sum(1 for issue in issues if issue.get("code") == "turn_budget_exceeded")
        item_score = _score(issues)
        items.append(
            {
                "work_id": work_id,
                "output_path": str(output_path),
                "route": route_name,
                "status": "pass" if not issues else "needs_review",
                "score": item_score,
                "issues": issues,
                "agent_metrics": metrics_summary,
                "evaluation_expectations": {
                    "present": has_expectations,
                    "failed_count": expectation_issue_count,
                    "assertion_count": assertion_count,
                },
            }
        )

    total = len(items)
    aggregate_score = round(sum(int(item["score"]) for item in items) / total) if total else 100
    avg_turns_used = round(total_turns_used / metrics_present, 2) if metrics_present else 0.0
    metric_coverage_status = "complete" if metrics_present == total else "incomplete"
    quality_flags = []
    if metrics_present < total:
        quality_flags.append("metric_coverage_incomplete")
    if unused_expectation_work_ids:
        failed_expectation_count += len(unused_expectation_work_ids)
        aggregate_issues.append(
            _issue(
                code="unused_golden_expectations",
                severity="error",
                rubric_key="golden_expectations",
                message="golden expectations include work_id values absent from the evaluated manifest",
            )
        )
    if expectation_items == total and not unused_expectation_work_ids:
        expectation_coverage_status = "complete"
    elif unused_expectation_work_ids and expectation_items == total:
        expectation_coverage_status = "stale"
    else:
        expectation_coverage_status = "incomplete"
    if failed_expectation_count or (expectations_active and expectation_coverage_status != "complete"):
        quality_flags.append("golden_expectation_failed")
    if unused_expectation_work_ids:
        quality_flags.append("unused_golden_expectations")
    issue_count = sum(len(item["issues"]) for item in items) + len(aggregate_issues)
    report = {
        "schema": CURATOR_PROMPT_EVAL_SCHEMA,
        "phase": "vocabulary_curation",
        "prompt_identity": plan.get("prompt_identity") if isinstance(plan.get("prompt_identity"), dict) else {},
        "input_fingerprints": {
            "plan_hash": curator_plan_hash(plan),
            "manifest_hash": _canonical_payload_hash(manifest),
            "prompt_identity_hash": str(
                (plan.get("prompt_identity") if isinstance(plan.get("prompt_identity"), dict) else {}).get(
                    "aggregate_hash"
                )
                or ""
            ),
            "evaluation_expectations_present": bool(expectations_by_work_id),
            "evaluation_expectations_hash": _canonical_payload_hash(expectations_by_work_id),
        },
        "status": "pass" if issue_count == 0 else "needs_review",
        "aggregate": {
            "score": aggregate_score,
            "item_count": total,
            "issue_count": issue_count,
            "redaction_issue_count": redaction_issue_count,
            "quality_flags": quality_flags,
            "route_counts": route_counts,
            "metric_coverage": {
                "items_with_agent_metrics": metrics_present,
                "items_total": total,
                "status": metric_coverage_status,
            },
            "efficiency": {
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "total_retries": total_retries,
                "avg_turns_used": avg_turns_used,
                "turn_budget_exceeded_count": turn_budget_exceeded_count,
            },
        },
        "items": items,
        "aggregate_issues": aggregate_issues,
        "next_action": "" if issue_count == 0 else "revisar outputs e prompt/rubrica antes de apply-curator-batch",
    }
    if expectations_active:
        report["aggregate"]["expectation_coverage"] = {
            "items_with_expectations": expectation_items,
            "items_total": total,
            "failed_expectation_count": failed_expectation_count,
            "unused_expectation_count": len(unused_expectation_work_ids),
            "status": expectation_coverage_status,
        }
        if unused_expectation_work_ids:
            report["aggregate"]["unused_expectation_work_ids"] = unused_expectation_work_ids
    if baseline_eval_path is not None:
        comparison = _compare_to_baseline(current=report, baseline_path=baseline_eval_path)
        report["comparison"] = comparison
        if comparison.get("status") == "regressed":
            aggregate = report["aggregate"]
            quality_flags = aggregate["quality_flags"]
            if "baseline_regression" not in quality_flags:
                quality_flags.append("baseline_regression")
            report["status"] = "needs_review"
            report["next_action"] = "revisar regressao contra baseline antes de apply-curator-batch"
        elif comparison.get("status") == "not_comparable":
            aggregate = report["aggregate"]
            quality_flags = aggregate["quality_flags"]
            if "baseline_not_comparable" not in quality_flags:
                quality_flags.append("baseline_not_comparable")
            report["status"] = "needs_review"
            report["next_action"] = "revisar baseline/corpus de ouro antes de comparar engenharia de prompt"
    return report
