"""Versioned offline behavior corpus gates for agent prompt changes."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, NonNegativeInt, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.quality.curator_prompt_eval import (
    evaluate_curator_prompt_outputs,
    load_curator_prompt_expectations,
)
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import (
    VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA,
    build_curator_prompt_identity,
    curator_plan_hash,
)
from mednotes.domains.wiki.common import ValidationError
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue, contract_error

AGENT_BEHAVIOR_CORPUS_SCHEMA = "medical-notes-workbench.agent-behavior-corpus.v1"
AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA = "medical-notes-workbench.agent-behavior-corpus-report.v1"
AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA = "medical-notes-workbench.agent-behavior-contract-eval.v1"
AGENT_BEHAVIOR_CASE_DRAFT_SCHEMA = "medical-notes-workbench.agent-behavior-case-draft.v1"
AGENT_BEHAVIOR_CASE_DRAFT_REPORT_SCHEMA = "medical-notes-workbench.agent-behavior-case-draft-report.v1"

DEFAULT_TELEMETRY_APP = "medical-notes-workbench"
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_SIGNAL_SEVERITY = {
    "agent.retry_loop": "high",
    "agent.retry_without_input_change": "high",
    "agent.ignored_next_action": "high",
    "agent.wrong_phase": "high",
    "agent.generated_script_workaround": "high",
    "agent.unsafe_generated_script_recovery_bypass": "high",
    "agent.missing_error_context": "high",
    "agent.script_or_prompt_drift": "high",
    "agent.unexpected_mutation": "high",
    "agent.command_failed": "medium",
    "agent.workflow_blocked": "medium",
    "agent.dry_run_without_apply": "medium",
    "dry_run_without_apply": "medium",
    "extension_prompt_or_script_drift": "high",
    "resource.version_control_policy_bypassed": "critical",
    "resource.guard_missing": "critical",
    "resource.run_finish_missing": "high",
    "resource.restore_point_after_mutation": "critical",
    "resource.direct_mutation_attempt": "high",
}
RISK_CODES_THAT_CREATE_DRAFTS = {
    "mass_markdown_mutation",
    "hardcoded_user_path",
    "reads_obsidian_plugin_data",
    "writes_related_notes_section",
    "external_api_or_embedding_call",
    "no_dry_run",
    "encoding_corruption",
    "extension_prompt_or_script_drift",
    "direct_sql_mutation",
    "queue_truth_bypass",
    "unsafe_mass_wikilink_rewrite",
}
COMMAND_PROMPT_SOURCES = {
    "/flashcards": "commands/flashcards.toml",
    "/report": "commands/report.toml",
    "/mednotes:create": "commands/mednotes/create.toml",
    "/mednotes:enrich": "commands/mednotes/enrich.toml",
    "/mednotes:fix-wiki": "commands/mednotes/fix-wiki.toml",
    "/mednotes:history": "commands/mednotes/history.toml",
    "/mednotes:link": "commands/mednotes/link.toml",
    "/mednotes:link-body": "commands/mednotes/link-body.toml",
    "/mednotes:link-related": "commands/mednotes/link-related.toml",
    "/mednotes:pdf-library": "commands/mednotes/pdf-library.toml",
    "/mednotes:process-chats": "commands/mednotes/process-chats.toml",
    "/mednotes:setup": "commands/mednotes/setup.toml",
    "/mednotes:status": "commands/mednotes/status.toml",
    "/mednotes:telemetry": "commands/mednotes/telemetry.toml",
}
WORKFLOW_SKILL_PROMPT_SOURCES = {
    "flashcards": "skills/create-medical-flashcards/SKILL.md",
    "create": "skills/create-medical-note/SKILL.md",
    "enrich": "skills/enrich-medical-note/SKILL.md",
    "fix-wiki": "skills/fix-medical-wiki/SKILL.md",
    "link": "skills/link-medical-wiki/SKILL.md",
    "link-body": "skills/link-medical-wiki/SKILL.md",
    "link-related": "skills/link-medical-wiki/SKILL.md",
    "pdf-library": "skills/pdf-library/SKILL.md",
    "process-chats": "skills/process-medical-chats/SKILL.md",
    "report": "skills/workflow-report/SKILL.md",
    "setup": "skills/obsidian-ops/SKILL.md",
    "status": "skills/obsidian-ops/SKILL.md",
    "telemetry": "skills/obsidian-ops/SKILL.md",
}


class _AgentBehaviorCorpusFields(ContractModel):
    schema_id: StrictStr = Field(alias="schema", serialization_alias="schema")
    suite_id: StrictStr = ""
    agent: StrictStr = ""
    surface_type: StrictStr = ""
    evaluator: StrictStr = ""
    prompt_sources: list[StrictStr] = Field(default_factory=list)
    prompt_identity_hash: StrictStr = ""
    cases_path: StrictStr = ""
    plan_path: StrictStr = ""
    manifest_path: StrictStr = ""
    expectations_path: StrictStr = ""
    baseline_eval_path: StrictStr = ""
    case_count: NonNegativeInt = 0
    cases: list[JsonObject] = Field(default_factory=list)


class _AgentBehaviorAssertionFields(ContractModel):
    """Typed assertion read from behavior-case fixtures before evaluation."""

    model_config = ConfigDict(extra="ignore")

    op: StrictStr = ""
    path: StrictStr = ""
    value: JsonValue = None


class _AgentBehaviorCaseFields(ContractModel):
    """Fixture case boundary; raw JSON must validate before it can drive scoring."""

    model_config = ConfigDict(extra="ignore")

    case_id: StrictStr = ""
    behavior: StrictStr = ""
    output_path: StrictStr = ""
    assertions: list[_AgentBehaviorAssertionFields] = Field(default_factory=list)


class _AgentBehaviorCasesPayloadFields(ContractModel):
    """Root cases file consumed by the offline behavior-contract evaluator."""

    model_config = ConfigDict(extra="ignore")

    schema_id: StrictStr = Field(alias="schema")
    cases: list[_AgentBehaviorCaseFields] = Field(default_factory=list)


class _CuratorOutputManifestItemFields(ContractModel):
    """Typed lens for manifest fields that affect generated output resolution."""

    model_config = ConfigDict(extra="ignore")

    output_path: StrictStr = ""


class _CuratorOutputManifestFields(ContractModel):
    """Curator manifest boundary; the raw manifest is preserved only as audit payload."""

    model_config = ConfigDict(extra="ignore")

    schema_id: StrictStr = Field(alias="schema")
    items: list[JsonObject] = Field(default_factory=list)


class _TelemetryAgentEventFields(ContractModel):
    """Telemetry event fields that may become behavior-corpus signals."""

    model_config = ConfigDict(extra="ignore")

    code: StrictStr = ""
    type: StrictStr = ""
    severity: StrictStr = ""
    phase: StrictStr = ""
    expected_phase: StrictStr = ""
    next_action_expected: StrictStr = ""
    recovery_command: StrictStr = ""
    command_family: StrictStr = ""
    path: StrictStr = ""


class _TelemetryClientLens(ContractModel):
    """Typed app metadata nested inside telemetry evidence payloads."""

    model_config = ConfigDict(extra="ignore")

    app: StrictStr = ""
    app_version: StrictStr = ""


class _TelemetryPayloadLens(ContractModel):
    """External telemetry envelopes are validated before metadata drives routing."""

    model_config = ConfigDict(extra="ignore")

    schema_id: StrictStr = Field(default="", alias="schema", serialization_alias="schema")
    app: StrictStr = ""
    app_version: StrictStr = ""
    client: _TelemetryClientLens | None = None
    records: list[JsonObject] = Field(default_factory=list)


class _GeneratedScriptEvidenceLens(ContractModel):
    """Redacted generated-script evidence promoted into prevention suggestions."""

    model_config = ConfigDict(extra="ignore")

    path: StrictStr = ""
    risk_codes: list[StrictStr] = Field(default_factory=list)
    function_or_command: StrictStr = ""


class _CommandEventEvidenceLens(ContractModel):
    """Redacted command evidence promoted into prevention suggestions."""

    model_config = ConfigDict(extra="ignore")

    command: StrictStr = ""
    command_family: StrictStr = ""
    path: StrictStr = ""
    status: StrictStr = ""


class _TelemetryEnvironmentIntegrityLens(ContractModel):
    """Typed subset of environment integrity used only for version provenance."""

    model_config = ConfigDict(extra="ignore")

    app_version: StrictStr = ""


class _TelemetryEnvironmentContextLens(ContractModel):
    """Typed subset of record environment context used by draft provenance."""

    model_config = ConfigDict(extra="ignore")

    extension_integrity: _TelemetryEnvironmentIntegrityLens | None = None


class _TelemetryRecordMetadataLens(ContractModel):
    """Record fields allowed to affect draft naming, suite routing, and provenance."""

    model_config = ConfigDict(extra="ignore")

    workflow: StrictStr = ""
    agent: StrictStr = ""
    phase: StrictStr = ""
    recorded_at: StrictStr = ""
    app: StrictStr = ""
    app_version: StrictStr = ""
    client: _TelemetryClientLens | None = None
    environment_context: _TelemetryEnvironmentContextLens | None = None


class _BehaviorCandidatePayloadLens(ContractModel):
    """Typed edge for behavior-case candidate envelopes from telemetry or email."""

    model_config = ConfigDict(extra="ignore")

    behavior_case_candidates: list[JsonObject] = Field(default_factory=list)
    first_pass_prevention_candidates: list[JsonObject] = Field(default_factory=list)
    messages: list[JsonObject] = Field(default_factory=list)


class _BehaviorCandidateMessageLens(ContractModel):
    """Typed candidate lists nested inside inbox/telemetry message records."""

    model_config = ConfigDict(extra="ignore")

    id: StrictStr = ""
    source_kind: StrictStr = ""
    behavior_case_candidates: list[JsonObject] = Field(default_factory=list)
    first_pass_prevention_candidates: list[JsonObject] = Field(default_factory=list)


def _telemetry_payload_lens(payload: object) -> _TelemetryPayloadLens:
    if not isinstance(payload, dict):
        return _TelemetryPayloadLens()
    return _TelemetryPayloadLens.model_validate(payload)


def _telemetry_record_lens(record: object) -> _TelemetryRecordMetadataLens:
    if not isinstance(record, dict):
        return _TelemetryRecordMetadataLens()
    return _TelemetryRecordMetadataLens.model_validate(record)


def _agent_behavior_corpus_fields(corpus: JsonObject) -> _AgentBehaviorCorpusFields:
    try:
        return _AgentBehaviorCorpusFields.model_validate(corpus)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="agent behavior corpus") from exc


def _agent_behavior_cases_payload_fields(payload: JsonObject) -> _AgentBehaviorCasesPayloadFields:
    try:
        return _AgentBehaviorCasesPayloadFields.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="agent behavior cases") from exc


def _curator_output_manifest_fields(payload: JsonObject) -> _CuratorOutputManifestFields:
    try:
        return _CuratorOutputManifestFields.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="agent behavior corpus manifest") from exc


def _telemetry_agent_event_fields(payload: JsonObject) -> _TelemetryAgentEventFields:
    try:
        return _TelemetryAgentEventFields.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="agent behavior telemetry event") from exc


def _read_json_object(path: Path, *, label: str) -> JsonObject:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return JsonObjectAdapter.validate_python(payload)


def _corpus_files(path: Path) -> list[Path]:
    if path.is_dir():
        direct = path / "corpus.json"
        if direct.is_file():
            return [direct]
        discovered = sorted(child for child in path.rglob("corpus.json") if child.is_file())
        if discovered:
            return discovered
        return [direct]
    return [path]


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else base / path


def _serialized_output_path(base: Path, value: object) -> str:
    """Serialize corpus output references relative to their suite directory."""

    raw_path = Path(str(value or ""))
    output_path = raw_path if raw_path.is_absolute() else base / raw_path
    try:
        return output_path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError as exc:
        raise ValidationError(f"agent behavior output_path must stay under corpus suite root: {value}") from exc


def _serialized_evidence_source_path(source_path: Path) -> str:
    """Keep private local paths out of promoted behavior-case evidence."""

    return source_path.name if source_path.is_absolute() else source_path.as_posix()


def _relativize_output_paths(value: Any, *, base: Path) -> Any:
    if isinstance(value, list):
        return [_relativize_output_paths(item, base=base) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "output_path":
            normalized[key] = _serialized_output_path(base, item)
        else:
            normalized[key] = _relativize_output_paths(item, base=base)
    return normalized


def agent_behavior_baseline_paths(corpus_path: Path) -> list[Path]:
    """Return baseline files declared by a corpus file or corpus bank."""

    baselines: set[Path] = set()
    for corpus_file in _corpus_files(corpus_path):
        corpus = _read_json_object(corpus_file, label="agent behavior corpus")
        corpus_fields = _agent_behavior_corpus_fields(corpus)
        if corpus_fields.schema_id != AGENT_BEHAVIOR_CORPUS_SCHEMA:
            raise ValidationError(f"agent behavior corpus must use schema {AGENT_BEHAVIOR_CORPUS_SCHEMA}")
        baseline_value = corpus_fields.baseline_eval_path
        if baseline_value:
            baselines.add(_resolve(corpus_file.parent, baseline_value).expanduser().resolve())
    return sorted(baselines)


def validate_agent_behavior_report_path(*, corpus_path: Path, report_path: Path) -> None:
    """Prevent writing a corpus wrapper report over a promoted behavior baseline."""

    candidate = report_path.expanduser().resolve()
    for baseline_path in agent_behavior_baseline_paths(corpus_path):
        if candidate == baseline_path:
            raise ValidationError(
                "agent_behavior_corpus.report_would_overwrite_baseline: "
                "--report writes agent-behavior-corpus-report.v1, but this path is baseline_eval_path. "
                "Write the corpus report to a separate file and promote the nested suite eval as the baseline."
            )


def _with_current_prompt_identity(plan: dict[str, Any], prompt_identity: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(plan)
    normalized["prompt_identity"] = dict(prompt_identity)
    work_items: list[Any] = []
    for item in normalized.get("work_items") if isinstance(normalized.get("work_items"), list) else []:
        if isinstance(item, dict):
            normalized_item = dict(item)
            normalized_item["prompt_identity"] = dict(prompt_identity)
            work_items.append(normalized_item)
        else:
            work_items.append(item)
    normalized["work_items"] = work_items
    return normalized


def _manifest_with_absolute_outputs(*, base: Path, manifest_path: Path, output_dir: Path) -> tuple[Path, dict[str, Any]]:
    manifest = _read_json_object(manifest_path, label="agent behavior corpus manifest")
    manifest_fields = _curator_output_manifest_fields(manifest)
    if manifest_fields.schema_id != VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA:
        raise ValidationError(
            f"agent behavior corpus manifest must use schema {VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA}"
        )
    normalized = dict(manifest)
    items: list[dict[str, Any]] = []
    for raw in manifest_fields.items:
        item_fields = _CuratorOutputManifestItemFields.model_validate(raw)
        item = dict(raw)
        item["output_path"] = str(_resolve(base, item_fields.output_path))
        items.append(item)
    normalized["items"] = items
    normalized_path = output_dir / "manifest.absolute.json"
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized_path, manifest


def _issue(*, code: str, message: str) -> JsonObject:
    return {"code": code, "message": message}


def _canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _extension_root() -> Path:
    from mednotes.platform.paths import extension_root

    return extension_root()


def _source_fingerprint(relative_path: str) -> JsonObject:
    path = _extension_root() / relative_path
    if not path.is_file():
        return {"path": relative_path, "exists": False, "sha256": "", "byte_count": 0, "word_count": 0}
    content = path.read_bytes()
    text = content.decode("utf-8", errors="replace")
    return {
        "path": relative_path,
        "exists": True,
        "sha256": _sha256_bytes(content),
        "byte_count": len(content),
        "word_count": len(text.split()),
    }


def _prompt_identity_for_corpus(corpus: _AgentBehaviorCorpusFields) -> JsonObject:
    if not corpus.prompt_sources:
        return JsonObjectAdapter.validate_python(build_curator_prompt_identity())
    normalized_sources = [_source_fingerprint(source) for source in corpus.prompt_sources if source]
    aggregate_material = [
        {"path": source["path"], "exists": source["exists"], "sha256": source["sha256"]}
        for source in normalized_sources
    ]
    return JsonObjectAdapter.validate_python({
        "schema": "medical-notes-workbench.agent-prompt-identity.v1",
        "agent": corpus.agent,
        "aggregate_hash": _canonical_payload_hash(aggregate_material),
        "sources": normalized_sources,
    })


def _get_path(payload: JsonValue, path: str) -> tuple[bool, JsonValue]:
    current = payload
    if not path:
        return True, current
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _assertion_issue(case_id: str, assertion: _AgentBehaviorAssertionFields, message: str) -> dict[str, str]:
    return {
        "code": "behavior_contract_failed",
        "case_id": case_id,
        "assertion": assertion.op,
        "path": assertion.path,
        "message": message,
    }


def _expected_array_length(case_id: str, assertion: _AgentBehaviorAssertionFields) -> tuple[bool, int, list[dict[str, str]]]:
    """Validate array-length assertions without converting strings to numbers."""

    expected = assertion.value
    if isinstance(expected, int) and not isinstance(expected, bool):
        return True, expected, []
    return False, 0, [_assertion_issue(case_id, assertion, f"expected integer length, got {expected!r}")]


def _evaluate_assertion(*, case_id: str, payload: JsonObject, assertion: _AgentBehaviorAssertionFields) -> list[dict[str, str]]:
    op = assertion.op
    path = assertion.path
    exists, value = _get_path(payload, path)
    expected = assertion.value
    if op == "path_present":
        return [] if exists and value is not None else [_assertion_issue(case_id, assertion, "expected path to be present")]
    if op == "path_absent":
        return [] if not exists else [_assertion_issue(case_id, assertion, "expected path to be absent")]
    if op == "path_equals":
        return [] if exists and value == expected else [_assertion_issue(case_id, assertion, f"expected {expected!r}, got {value!r}")]
    if op == "path_in":
        choices = expected if isinstance(expected, list) else []
        return [] if exists and value in choices else [_assertion_issue(case_id, assertion, f"expected value in {choices!r}")]
    if op == "array_len_equals":
        valid, expected_len, issues = _expected_array_length(case_id, assertion)
        if not valid:
            return issues
        return [] if isinstance(value, list) and len(value) == expected_len else [
            _assertion_issue(case_id, assertion, f"expected list length {expected!r}")
        ]
    if op == "array_len_at_least":
        valid, expected_len, issues = _expected_array_length(case_id, assertion)
        if not valid:
            return issues
        return [] if isinstance(value, list) and len(value) >= expected_len else [
            _assertion_issue(case_id, assertion, f"expected list length >= {expected!r}")
        ]
    if op == "array_len_at_most":
        valid, expected_len, issues = _expected_array_length(case_id, assertion)
        if not valid:
            return issues
        return [] if isinstance(value, list) and len(value) <= expected_len else [
            _assertion_issue(case_id, assertion, f"expected list length <= {expected!r}")
        ]
    if op == "json_not_contains":
        if not isinstance(expected, str):
            return [_assertion_issue(case_id, assertion, f"expected forbidden text string, got {expected!r}")]
        text = json.dumps(payload, ensure_ascii=False)
        return [] if expected not in text else [
            _assertion_issue(case_id, assertion, f"forbidden text was present: {expected!r}")
        ]
    return [_assertion_issue(case_id, assertion, f"unknown assertion op: {op}")]


def _score(issue_count: int) -> int:
    return max(0, 100 - 25 * issue_count)


class _PromptIdentityFields(ContractModel):
    """Typed lens for prompt identity hashes embedded in corpus reports."""

    model_config = ConfigDict(extra="ignore")

    aggregate_hash: StrictStr = ""


class _BaselineMetadataFields(ContractModel):
    """Typed lens for baseline promotion state."""

    model_config = ConfigDict(extra="ignore")

    status: StrictStr = ""


class _ContractEvalAggregateFields(ContractModel):
    """Counts that decide corpus regression status."""

    model_config = ConfigDict(extra="ignore")

    case_count: int = Field(default=0, ge=0, strict=True)
    item_count: int = Field(default=0, ge=0, strict=True)
    issue_count: int = Field(default=0, ge=0, strict=True)
    score: int = Field(default=0, ge=0, strict=True)


class _ContractEvalReportFields(ContractModel):
    """Typed status/count lens before corpus reports can drive pass/fail."""

    model_config = ConfigDict(extra="ignore")

    schema_id: StrictStr = Field(default="", alias="schema")
    status: StrictStr = ""
    aggregate: _ContractEvalAggregateFields = Field(default_factory=_ContractEvalAggregateFields)
    prompt_identity: _PromptIdentityFields = Field(default_factory=_PromptIdentityFields)
    baseline_metadata: _BaselineMetadataFields = Field(default_factory=_BaselineMetadataFields)


class _BaselineComparisonFields(ContractModel):
    """Typed result of comparing the current eval with its locked baseline."""

    model_config = ConfigDict(extra="ignore")

    status: StrictStr = ""


def _compare_contract_baseline(*, current: JsonObject, baseline_path: Path) -> JsonObject:
    baseline = _read_json_object(baseline_path, label="agent behavior contract baseline")
    current_fields = _ContractEvalReportFields.model_validate(current)
    baseline_fields = _ContractEvalReportFields.model_validate(baseline)
    if baseline_fields.schema_id != AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA:
        raise ValidationError(f"agent behavior contract baseline must use schema {AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA}")
    comparability_flags: list[str] = []
    if baseline_fields.baseline_metadata.status != "active":
        comparability_flags.append("baseline_not_promoted")
    if current_fields.prompt_identity.aggregate_hash != baseline_fields.prompt_identity.aggregate_hash:
        comparability_flags.append("prompt_identity_changed")
    score_delta = current_fields.aggregate.score - baseline_fields.aggregate.score
    issue_count_delta = current_fields.aggregate.issue_count - baseline_fields.aggregate.issue_count
    regression_flags: list[str] = []
    if baseline_fields.status == "pass" and current_fields.status != "pass":
        regression_flags.append("status_regression")
    if score_delta < 0:
        regression_flags.append("score_regression")
    if issue_count_delta > 0:
        regression_flags.append("issue_count_regression")
    comparison_status = "not_comparable" if comparability_flags else (
        "regressed" if regression_flags else "improved_or_equal"
    )
    return JsonObjectAdapter.validate_python(
        {
            "baseline_status": baseline_fields.status,
            "current_status": current_fields.status,
            "score_delta": score_delta,
            "issue_count_delta": issue_count_delta,
            "comparability_flags": comparability_flags,
            "regression_flags": regression_flags,
            "status": comparison_status,
        }
    )


def _promote_contract_baseline(report: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    baseline = dict(report)
    baseline["baseline_metadata"] = {
        "status": "active",
        "source_eval_path": str(source_path),
        "source_eval_hash": _canonical_payload_hash(report),
    }
    return baseline


def evaluate_json_contract_corpus(
    *,
    corpus: _AgentBehaviorCorpusFields,
    base: Path,
    prompt_identity: JsonObject,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    cases_path = _resolve(base, corpus.cases_path)
    cases_payload = _read_json_object(cases_path, label="agent behavior cases")
    cases_fields = _agent_behavior_cases_payload_fields(cases_payload)
    if cases_fields.schema_id != "medical-notes-workbench.agent-behavior-cases.v1":
        raise ValidationError("agent behavior cases must use schema medical-notes-workbench.agent-behavior-cases.v1")
    cases: list[JsonObject] = []
    case_scores: list[int] = []
    assertion_counts: list[int] = []
    total_issues: list[dict[str, str]] = []
    for case in cases_fields.cases:
        case_id = case.case_id
        output_path = _resolve(base, case.output_path)
        payload = _read_json_object(output_path, label=f"agent behavior output {case_id}")
        case_issues: list[dict[str, str]] = []
        for assertion in case.assertions:
            case_issues.extend(_evaluate_assertion(case_id=case_id, payload=payload, assertion=assertion))
        total_issues.extend(case_issues)
        case_score = _score(len(case_issues))
        assertion_count = len(case.assertions)
        case_scores.append(case_score)
        assertion_counts.append(assertion_count)
        cases.append(
            JsonObjectAdapter.validate_python(
                {
                    "case_id": case_id,
                    "behavior": case.behavior,
                    "output_path": _serialized_output_path(base, output_path),
                    "status": "pass" if not case_issues else "needs_review",
                    "score": case_score,
                    "issues": case_issues,
                    "assertion_count": assertion_count,
                }
            )
        )
    issue_count = len(total_issues)
    report_status = "pass" if issue_count == 0 else "needs_review"
    report_next_action = "" if issue_count == 0 else "review behavior contract failures before accepting prompt changes"
    comparison: JsonObject | None = None
    if baseline_path is not None and baseline_path.is_file():
        comparison = _compare_contract_baseline(
            current=JsonObjectAdapter.validate_python(
                {
                    "schema": AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA,
                    "status": report_status,
                    "aggregate": {
                        "case_count": len(cases),
                        "issue_count": issue_count,
                        "score": round(sum(case_scores) / len(case_scores)) if case_scores else 100,
                    },
                    "prompt_identity": prompt_identity,
                }
            ),
            baseline_path=baseline_path,
        )
        comparison_fields = _BaselineComparisonFields.model_validate(comparison)
        if comparison_fields.status != "improved_or_equal":
            report_status = "needs_review"
            report_next_action = "review behavior corpus baseline before accepting prompt changes"

    report = {
        "schema": AGENT_BEHAVIOR_CONTRACT_EVAL_SCHEMA,
        "suite_id": corpus.suite_id,
        "agent": corpus.agent,
        "evaluator": "json_contract",
        "prompt_identity": prompt_identity,
        "status": report_status,
        "aggregate": {
            "case_count": len(cases),
            "issue_count": issue_count,
            "score": round(sum(case_scores) / len(case_scores)) if case_scores else 100,
            "assertion_count": sum(assertion_counts),
        },
        "cases": cases,
        "issues": total_issues,
        "next_action": report_next_action,
    }
    if comparison is not None:
        report["comparison"] = comparison
    return report


def _blocked_report(
    *,
    corpus: _AgentBehaviorCorpusFields,
    prompt_identity_hash: str,
    issues: list[JsonObject],
) -> JsonObject:
    return JsonObjectAdapter.validate_python({
        "schema": AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA,
        "status": "needs_review",
        "suite_id": corpus.suite_id,
        "agent": corpus.agent,
        "aggregate": {
            "suite_count": 1,
            "case_count": corpus.case_count,
            "prompt_identity_hash": prompt_identity_hash,
            "issue_codes": [issue["code"] for issue in issues],
        },
        "suites": [],
        "issues": issues,
        "next_action": "rerun the agent behavior corpus with the current prompt and promote a fresh baseline",
    })


def _evaluate_single_agent_behavior_corpus(corpus_file: Path) -> dict[str, Any]:
    base = corpus_file.parent
    corpus = _read_json_object(corpus_file, label="agent behavior corpus")
    corpus_fields = _agent_behavior_corpus_fields(corpus)
    if corpus_fields.schema_id != AGENT_BEHAVIOR_CORPUS_SCHEMA:
        raise ValidationError(f"agent behavior corpus must use schema {AGENT_BEHAVIOR_CORPUS_SCHEMA}")
    evaluator = corpus_fields.evaluator
    if evaluator not in {"curator_prompt_eval", "json_contract"}:
        raise ValidationError("agent behavior corpus supports evaluator=curator_prompt_eval or json_contract")

    prompt_identity = _prompt_identity_for_corpus(corpus_fields)
    prompt_identity_hash = str(prompt_identity.get("aggregate_hash") or "")
    locked_prompt_hash = corpus_fields.prompt_identity_hash
    issues: list[JsonObject] = []
    if locked_prompt_hash != prompt_identity_hash:
        issues.append(
            _issue(
                code="stale_prompt_identity",
                message="corpus prompt_identity_hash does not match the current prompt/runbook fingerprint",
            )
        )
        return _blocked_report(corpus=corpus_fields, prompt_identity_hash=prompt_identity_hash, issues=issues)

    baseline_path = _resolve(base, corpus_fields.baseline_eval_path)
    if not baseline_path.is_file():
        issues.append(_issue(code="missing_behavior_baseline", message=f"baseline eval not found: {baseline_path}"))
        return _blocked_report(corpus=corpus_fields, prompt_identity_hash=prompt_identity_hash, issues=issues)

    if evaluator == "json_contract":
        eval_report = evaluate_json_contract_corpus(
            corpus=corpus_fields,
            base=base,
            prompt_identity=prompt_identity,
            baseline_path=baseline_path,
        )
        eval_fields = _ContractEvalReportFields.model_validate(eval_report)
        suite_status = "pass" if eval_fields.status == "pass" else "needs_review"
        report_issues = list(issues)
        if suite_status != "pass":
            report_issues.append(
                _issue(
                    code="behavior_contract_failed",
                    message="agent behavior contract returned needs_review",
                )
            )
        return {
            "schema": AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA,
            "status": "pass" if not report_issues else "needs_review",
            "suite_id": corpus_fields.suite_id,
            "agent": corpus_fields.agent,
            "aggregate": {
                "suite_count": 1,
                "case_count": eval_fields.aggregate.case_count or corpus_fields.case_count,
                "prompt_identity_hash": prompt_identity_hash,
                "issue_codes": [issue["code"] for issue in report_issues],
            },
            "suites": [
                {
                    "suite_id": corpus_fields.suite_id,
                    "agent": corpus_fields.agent,
                    "evaluator": evaluator,
                    "status": suite_status,
                    "prompt_identity_hash": prompt_identity_hash,
                    "eval": eval_report,
                }
            ],
            "issues": report_issues,
            "next_action": ""
            if not report_issues
            else "review agent behavior corpus failures before accepting prompt changes",
        }

    plan_path = _resolve(base, corpus_fields.plan_path)
    manifest_path = _resolve(base, corpus_fields.manifest_path)
    expectations_path = _resolve(base, corpus_fields.expectations_path)
    plan = _with_current_prompt_identity(_read_json_object(plan_path, label="agent behavior corpus plan"), prompt_identity)
    plan["evaluation_expectations_by_work_id"] = load_curator_prompt_expectations(
        expectations_path,
        expected_plan_hash=curator_plan_hash(plan),
    )

    with tempfile.TemporaryDirectory(prefix="agent-behavior-corpus-") as temp_dir:
        normalized_manifest_path, manifest = _manifest_with_absolute_outputs(
            base=base,
            manifest_path=manifest_path,
            output_dir=Path(temp_dir),
        )
        manifest_prompt_hash = str(manifest.get("prompt_identity_hash") or "")
        if manifest_prompt_hash != prompt_identity_hash:
            issues.append(
                _issue(
                    code="stale_behavior_outputs",
                    message="manifest prompt_identity_hash does not match current prompt/runbook fingerprint",
                )
            )
            return _blocked_report(corpus=corpus_fields, prompt_identity_hash=prompt_identity_hash, issues=issues)
        baseline = _read_json_object(baseline_path, label="agent behavior corpus baseline")
        baseline_prompt = baseline.get("prompt_identity") if isinstance(baseline.get("prompt_identity"), dict) else {}
        if str(baseline_prompt.get("aggregate_hash") or "") != prompt_identity_hash:
            issues.append(
                _issue(
                    code="stale_behavior_baseline",
                    message="baseline prompt_identity does not match current prompt/runbook fingerprint",
                )
            )
            return _blocked_report(corpus=corpus_fields, prompt_identity_hash=prompt_identity_hash, issues=issues)
        eval_report = evaluate_curator_prompt_outputs(
            plan=plan,
            manifest_path=normalized_manifest_path,
            baseline_eval_path=baseline_path,
        )
        eval_report = _relativize_output_paths(eval_report, base=base)

    eval_fields = _ContractEvalReportFields.model_validate(eval_report)
    suite_status = "pass" if eval_fields.status == "pass" else "needs_review"
    case_count = eval_fields.aggregate.item_count or corpus_fields.case_count
    report_issues = list(issues)
    if suite_status != "pass":
        report_issues.append(
            _issue(
                code="behavior_corpus_eval_needs_review",
                message="curator behavior corpus eval returned needs_review",
            )
        )
    return {
        "schema": AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA,
        "status": "pass" if not report_issues else "needs_review",
        "suite_id": corpus_fields.suite_id,
        "agent": corpus_fields.agent,
        "aggregate": {
            "suite_count": 1,
            "case_count": case_count,
            "prompt_identity_hash": prompt_identity_hash,
            "issue_codes": [issue["code"] for issue in report_issues],
        },
        "suites": [
            {
                "suite_id": corpus_fields.suite_id,
                "agent": corpus_fields.agent,
                "evaluator": corpus_fields.evaluator,
                "status": suite_status,
                "plan_hash": curator_plan_hash(plan),
                "prompt_identity_hash": prompt_identity_hash,
                "eval": eval_report,
            }
        ],
        "issues": report_issues,
        "next_action": ""
        if not report_issues
        else "review agent behavior corpus failures before accepting prompt changes",
    }


class _CorpusAggregateFields(ContractModel):
    """Typed status/count lens for bank-level corpus aggregation."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    aggregate: JsonObject = Field(default_factory=dict)
    issues: list[JsonObject] = Field(default_factory=list)
    suites: list[JsonObject] = Field(default_factory=list)


def _aggregate_corpus_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    issue_codes: list[str] = []
    issues: list[dict[str, str]] = []
    suites: list[dict[str, Any]] = []
    case_count = 0
    prompt_identity_hash = ""
    typed_reports = [_CorpusAggregateFields.model_validate(report) for report in reports]
    for report in typed_reports:
        aggregate = report.aggregate
        case_count += int(aggregate.get("case_count") or 0)
        if not prompt_identity_hash:
            prompt_identity_hash = str(aggregate.get("prompt_identity_hash") or "")
        issue_codes.extend(str(code) for code in aggregate.get("issue_codes", []) if str(code))
        issues.extend(issue for issue in report.issues if isinstance(issue, dict))
        suites.extend(suite for suite in report.suites if isinstance(suite, dict))
    status = "pass" if all(report.status == "pass" for report in typed_reports) else "needs_review"
    return {
        "schema": AGENT_BEHAVIOR_CORPUS_REPORT_SCHEMA,
        "status": status,
        "suite_id": "agent_behavior_corpus_bank",
        "agent": "multiple",
        "aggregate": {
            "suite_count": len(reports),
            "case_count": case_count,
            "prompt_identity_hash": prompt_identity_hash,
            "issue_codes": issue_codes,
        },
        "suites": suites,
        "issues": issues,
        "next_action": ""
        if status == "pass"
        else "review agent behavior corpus failures before accepting prompt changes",
    }


def evaluate_agent_behavior_corpus(corpus_path: Path) -> dict[str, Any]:
    corpus_files = _corpus_files(corpus_path)
    reports = [_evaluate_single_agent_behavior_corpus(corpus_file) for corpus_file in corpus_files]
    if len(reports) == 1:
        return reports[0]
    return _aggregate_corpus_reports(reports)


def _json_payload_files(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        return sorted(path for path in input_path.rglob("*.json") if path.is_file())
    return [input_path]


def _evidence_payload_files(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        return sorted(
            path
            for path in input_path.rglob("*")
            if path.is_file() and path.suffix.lower() in {".json", ".md", ".markdown", ".txt"}
        )
    return [input_path]


def _read_json_any(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"telemetry input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"telemetry input is invalid JSON: {path}: {exc}") from exc


def _schema_app(payload: dict[str, Any]) -> str:
    fields = _telemetry_payload_lens(payload)
    schema = fields.schema_id
    if ".workflow-telemetry-envelope." in schema:
        return schema.split(".workflow-telemetry-envelope.", 1)[0]
    if ".workflow-run-record." in schema:
        return schema.split(".workflow-run-record.", 1)[0]
    return ""


def _payload_app(payload: dict[str, Any]) -> str:
    fields = _telemetry_payload_lens(payload)
    client_app = fields.client.app if fields.client is not None else ""
    for value in (fields.app, client_app, _schema_app(payload)):
        if value:
            return value
    return ""


def _telemetry_records(input_path: Path) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    records: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    for path in _json_payload_files(input_path):
        payload = _read_json_any(path)
        payload_fields = _telemetry_payload_lens(payload)
        if isinstance(payload, dict) and payload_fields.records:
            envelope = payload
            for record in payload_fields.records:
                if isinstance(record, dict):
                    records.append((record, envelope, path))
        elif isinstance(payload, dict):
            records.append((payload, {}, path))
        elif isinstance(payload, list):
            for record in payload:
                if isinstance(record, dict):
                    records.append((record, {}, path))
    return records


def _record_app(record: dict[str, Any], envelope: dict[str, Any]) -> str:
    record_app = _payload_app(record)
    envelope_app = _payload_app(envelope)
    if record_app:
        return record_app
    if envelope_app:
        return envelope_app
    return DEFAULT_TELEMETRY_APP


def _record_app_version(record: dict[str, Any], envelope: dict[str, Any]) -> str:
    record_fields = _telemetry_record_lens(record)
    envelope_fields = _telemetry_payload_lens(envelope)
    record_client_version = record_fields.client.app_version if record_fields.client is not None else ""
    envelope_client_version = envelope_fields.client.app_version if envelope_fields.client is not None else ""
    integrity = (
        record_fields.environment_context.extension_integrity
        if record_fields.environment_context is not None
        else None
    )
    for value in (
        record_fields.app_version,
        record_client_version,
        envelope_client_version,
        integrity.app_version if integrity is not None else "",
    ):
        if value:
            return value
    return "unknown"


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "")]
    if str(value or ""):
        return [str(value)]
    return []


def _script_risk_codes(record: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    scripts = record.get("generated_scripts")
    if not isinstance(scripts, list):
        return codes
    for script in scripts:
        if not isinstance(script, dict):
            continue
        for code in _list_strings(script.get("risk_codes")):
            if code not in codes:
                codes.append(code)
    return codes


def _agent_events(record: dict[str, Any]) -> list[_TelemetryAgentEventFields]:
    events = record.get("agent_events")
    typed_events: list[_TelemetryAgentEventFields] = []
    if not isinstance(events, list):
        return typed_events
    for event in events:
        if isinstance(event, dict):
            typed_events.append(_telemetry_agent_event_fields(JsonObjectAdapter.validate_python(event)))
    return typed_events


def _signals_for_record(record: dict[str, Any]) -> list[str]:
    diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
    behavior = (
        diagnostic.get("agent_behavior_context")
        if isinstance(diagnostic.get("agent_behavior_context"), dict)
        else {}
    )
    signals: list[str] = []
    for value in _list_strings(behavior.get("codes")):
        if value not in signals:
            signals.append(value)
    root = str(diagnostic.get("root_cause_code") or "")
    if root and (root.startswith("agent.") or root in DEFAULT_SIGNAL_SEVERITY) and root not in signals:
        signals.append(root)
    for event in _agent_events(record):
        code = event.code
        if code and code not in signals:
            signals.append(code)
    risk_codes = set(_script_risk_codes(record))
    if risk_codes & RISK_CODES_THAT_CREATE_DRAFTS and "agent.generated_script_workaround" not in signals:
        signals.append("agent.generated_script_workaround")
    if "extension_prompt_or_script_drift" in risk_codes and "extension_prompt_or_script_drift" not in signals:
        signals.append("extension_prompt_or_script_drift")
    return signals


def _severity_for_signal(record: dict[str, Any], signal: str) -> str:
    severities: list[str] = []
    for event in _agent_events(record):
        if event.code == signal or event.type == signal:
            severity = event.severity.lower()
            if severity:
                severities.append(severity)
    severities.append(DEFAULT_SIGNAL_SEVERITY.get(signal, "low"))
    return max(severities, key=lambda item: SEVERITY_RANK.get(item, 0))


def _passes_min_severity(record: dict[str, Any], signal: str, min_severity: str) -> bool:
    return SEVERITY_RANK.get(_severity_for_signal(record, signal), 0) >= SEVERITY_RANK.get(min_severity, 2)


def _clean_text(value: Any, *, max_chars: int = 320) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(
        r"(?i)(token|auth[_-]?token|api[_-]?key|secret|authorization|bearer)\s*[:=]\s*['\"]?[^'\"\s]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?i)(RESEND_API_KEY|INGEST_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY)[^,\s]*", r"\1=<redacted>", text)
    return text[:max_chars]


def _event_sample(event: _TelemetryAgentEventFields) -> dict[str, str]:
    allowed = (
        "code",
        "type",
        "severity",
        "phase",
        "expected_phase",
        "next_action_expected",
        "recovery_command",
        "command_family",
        "path",
    )
    return {key: _clean_text(getattr(event, key)) for key in allowed if getattr(event, key)}


def _redacted_evidence(record: dict[str, Any], envelope: dict[str, Any], *, signal: str, source_path: Path) -> dict[str, Any]:
    diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
    behavior = (
        diagnostic.get("agent_behavior_context")
        if isinstance(diagnostic.get("agent_behavior_context"), dict)
        else {}
    )
    evidence = {
        "source_path": _serialized_evidence_source_path(source_path),
        "run_id": _clean_text(record.get("run_id")),
        "workflow": _clean_text(record.get("workflow")),
        "status": _clean_text(record.get("status")),
        "phase": _clean_text(record.get("phase")),
        "blocked_reason": _clean_text(record.get("blocked_reason")),
        "next_action": _clean_text(record.get("next_action")),
        "root_cause_code": _clean_text(diagnostic.get("root_cause_code")),
        "recovery_command": _clean_text(diagnostic.get("recovery_command")),
        "agent_behavior_codes": _list_strings(behavior.get("codes")),
        "risk_codes": _script_risk_codes(record),
        "event_samples": [_event_sample(event) for event in _agent_events(record)[:3]],
        "payload_level": _clean_text(envelope.get("payload_level")),
        "signal": signal,
    }
    return {key: value for key, value in evidence.items() if value not in ("", [], {})}


def _workflow_key(workflow: str) -> str:
    value = workflow.strip()
    if not value:
        return ""
    if value in COMMAND_PROMPT_SOURCES:
        return value.rsplit(":", 1)[-1].lstrip("/")
    if value.startswith("/mednotes:"):
        return value.split(":", 1)[1].split()[0]
    if value.startswith("/flashcards"):
        return "flashcards"
    return value.split()[0].replace("/mednotes:", "").replace("/", "")


def _command_source_for_workflow(workflow: str) -> str:
    normalized = workflow.strip().split()[0] if workflow.strip() else ""
    return COMMAND_PROMPT_SOURCES.get(normalized, "")


def _suggested_prompt_sources(workflow: str) -> list[str]:
    sources: list[str] = []
    command_source = _command_source_for_workflow(workflow)
    if command_source:
        sources.append(command_source)
    skill_source = WORKFLOW_SKILL_PROMPT_SOURCES.get(_workflow_key(workflow))
    if skill_source and skill_source not in sources:
        sources.append(skill_source)
    return sources


def _prompt_snippet(relative_path: str, *, signal: str) -> str:
    path = _extension_root() / relative_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    keywords = ["next_action", "blocked", "bloque", "script", "comando", "workflow"]
    if "tool" in signal or "command" in signal:
        keywords.extend(["exit code", "shell", "terminal"])
    if "script" in signal:
        keywords.extend(["workaround", "oficial", "manual"])
    selected = 0
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if any(keyword in lowered for keyword in keywords):
            selected = max(0, index - 1)
            break
    snippet = " ".join(line.strip() for line in lines[selected : selected + 3] if line.strip())
    return _clean_text(snippet, max_chars=420)


def _surface_items(value: Any, *, kind: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    allowed = (
        ("path", "snippet", "reason")
        if kind == "prompt"
        else ("path", "function_or_command", "reason")
    )
    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        clean = {key: _clean_text(item.get(key), max_chars=420) for key in allowed if _clean_text(item.get(key))}
        if clean.get("path"):
            items.append(clean)
    return items


def _suspect_prompts_from_sources(prompt_sources: list[str], *, signal: str) -> list[dict[str, str]]:
    prompts: list[dict[str, str]] = []
    for source in prompt_sources:
        path = _clean_text(source)
        if not path:
            continue
        snippet = _prompt_snippet(path, signal=signal)
        prompts.append(
            {
                "path": path,
                "snippet": snippet or "Trecho não disponível no bundle local.",
                "reason": f"Fonte de prompt vinculada ao workflow/sinal {signal}; revisar se deveria prevenir o desvio.",
            }
        )
    return prompts


def _suspect_scripts_from_record(record: dict[str, Any]) -> list[dict[str, str]]:
    scripts: list[dict[str, str]] = []
    raw_scripts = record.get("generated_scripts")
    if isinstance(raw_scripts, list):
        for item in raw_scripts:
            if not isinstance(item, dict):
                continue
            script = _GeneratedScriptEvidenceLens.model_validate(item)
            path = _clean_text(script.path)
            if not path:
                continue
            risks = ", ".join(_list_strings(script.risk_codes)[:5])
            scripts.append(
                {
                    "path": path,
                    "function_or_command": _clean_text(script.function_or_command or "generated_script"),
                    "reason": f"Script capturado na evidência; risk_codes={risks}" if risks else "Script capturado na evidência.",
                }
            )
    raw_commands = record.get("command_events")
    if isinstance(raw_commands, list):
        for item in raw_commands:
            if not isinstance(item, dict):
                continue
            event = _CommandEventEvidenceLens.model_validate(item)
            command = _clean_text(event.command or event.command_family, max_chars=260)
            if not command:
                continue
            scripts.append(
                {
                    "path": _clean_text(event.path or "terminal"),
                    "function_or_command": command,
                    "reason": _clean_text(event.status or "command_event"),
                }
            )
    return scripts


def _prevention_owner_note(*, prompts: list[dict[str, str]], scripts: list[dict[str, str]]) -> str:
    if prompts or scripts:
        return "Superfícies suspeitas listadas para revisão; isso não prova culpa sem reprodução."
    return "Nenhum prompt ou script encarregado de prevenir este comportamento foi identificado na evidência redigida."


def _target_suite(record: dict[str, Any]) -> str:
    fields = _telemetry_record_lens(record)
    workflow = fields.workflow
    if _command_source_for_workflow(workflow):
        return "extension_commands.core_behavior.v1"
    agent = fields.agent
    if agent:
        normalized = agent.replace("-", "_")
        return f"{normalized}.core_behavior.v1"
    return "extension_skills.core_behavior.v1"


def _suggested_assertions(signal: str) -> list[dict[str, Any]]:
    shared_block = [
        {"op": "path_equals", "path": "status", "value": "blocked"},
        {"op": "path_present", "path": "next_action"},
    ]
    mapping: dict[str, list[dict[str, Any]]] = {
        "agent.retry_loop": shared_block
        + [
            {"op": "path_present", "path": "diagnostic_context.agent_behavior_context.codes"},
            {"op": "path_present", "path": "error_context.retry_scope"},
        ],
        "agent.retry_without_input_change": shared_block
        + [
            {"op": "path_equals", "path": "blocked_reason", "value": "retry_without_input_change"},
            {"op": "path_present", "path": "error_context.input_hash"},
        ],
        "agent.ignored_next_action": shared_block
        + [
            {"op": "path_present", "path": "next_action_expected"},
            {"op": "path_equals", "path": "followed_next_action", "value": True},
        ],
        "agent.wrong_phase": shared_block
        + [
            {"op": "path_present", "path": "expected_phase"},
            {"op": "path_equals", "path": "mutated", "value": False},
        ],
        "agent.generated_script_workaround": shared_block
        + [
            {"op": "path_equals", "path": "used_official_recovery_command", "value": True},
            {"op": "path_equals", "path": "unsafe_workaround_created", "value": False},
        ],
        "agent.unsafe_generated_script_recovery_bypass": shared_block
        + [
            {"op": "path_equals", "path": "used_official_recovery_command", "value": True},
            {"op": "path_equals", "path": "unsafe_workaround_created", "value": False},
        ],
        "agent.missing_error_context": shared_block
        + [
            {"op": "path_present", "path": "error_context.cause"},
            {"op": "path_present", "path": "error_context.retry_scope"},
        ],
        "agent.script_or_prompt_drift": shared_block
        + [
            {"op": "path_equals", "path": "drift_classified", "value": True},
            {"op": "path_present", "path": "recovery_command"},
        ],
        "extension_prompt_or_script_drift": shared_block
        + [
            {"op": "path_equals", "path": "drift_classified", "value": True},
            {"op": "path_present", "path": "recovery_command"},
        ],
        "resource.version_control_policy_bypassed": [
            {"op": "path_equals", "path": "status", "value": "blocked"},
            {"op": "path_present", "path": "version_control_safety"},
            {"op": "path_equals", "path": "version_control_safety.mutation_without_guard", "value": False},
            {"op": "path_equals", "path": "version_control_safety.run_start_seen", "value": True},
            {"op": "path_equals", "path": "version_control_safety.run_finish_seen", "value": True},
        ],
        "resource.guard_missing": [
            {"op": "path_equals", "path": "status", "value": "blocked"},
            {"op": "path_equals", "path": "blocked_reason", "value": "vault_guard_required"},
            {"op": "path_equals", "path": "version_control_safety.mutation_without_guard", "value": False},
            {"op": "path_present", "path": "recovery_command"},
        ],
        "resource.run_finish_missing": [
            {"op": "path_equals", "path": "status", "value": "blocked"},
            {"op": "path_equals", "path": "version_control_safety.run_start_seen", "value": True},
            {"op": "path_equals", "path": "version_control_safety.run_finish_seen", "value": True},
            {"op": "path_present", "path": "version_control_safety.restore_point_after"},
        ],
        "resource.restore_point_after_mutation": [
            {"op": "path_equals", "path": "status", "value": "blocked"},
            {"op": "path_equals", "path": "version_control_safety.restore_point_before", "value": True},
            {"op": "path_equals", "path": "version_control_safety.restore_point_after", "value": True},
        ],
        "resource.direct_mutation_attempt": [
            {"op": "path_equals", "path": "status", "value": "blocked"},
            {"op": "path_equals", "path": "blocked_reason", "value": "direct_mutation_forbidden"},
            {"op": "path_equals", "path": "version_control_safety.direct_mutation_forbidden", "value": True},
            {"op": "path_present", "path": "recovery_command"},
        ],
        "agent.dry_run_without_apply": [
            {"op": "path_in", "path": "status", "value": ["ready_to_apply", "blocked", "discarded"]},
            {"op": "path_present", "path": "next_action"},
            {"op": "path_equals", "path": "dry_run_called_completed", "value": False},
        ],
        "dry_run_without_apply": [
            {"op": "path_in", "path": "status", "value": ["ready_to_apply", "blocked", "discarded"]},
            {"op": "path_present", "path": "next_action"},
            {"op": "path_equals", "path": "dry_run_called_completed", "value": False},
        ],
    }
    return mapping.get(
        signal,
        shared_block
        + [
            {"op": "path_present", "path": "diagnostic_context.root_cause_code"},
            {"op": "path_present", "path": "error_context.next_action"},
        ],
    )


def _promotion_checklist(signal: str) -> list[str]:
    return [
        "Confirmar que a evidência está redigida e não contém conteúdo clínico bruto, HTML, tokens ou chaves.",
        "Escolher a suite final e criar output fixture que reproduza o comportamento corrigido.",
        "Manter ao menos duas assertions fortes e promover baseline somente após o corpus passar.",
        f"Verificar que o caso falharia antes da correção do prompt para {signal}.",
    ]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "telemetry"


def _draft_date(record: dict[str, Any], envelope: dict[str, Any]) -> str:
    record_fields = _telemetry_record_lens(record)
    envelope_generated_at = envelope["generated_at"] if "generated_at" in envelope and isinstance(envelope["generated_at"], str) else ""
    for text in (record_fields.recorded_at, envelope_generated_at):
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            return text[:10]
    return datetime.now(UTC).date().isoformat()


def _unique_draft_output_path(output_dir: Path, stem: str, reserved: set[Path]) -> Path:
    output_path = output_dir / f"{stem}.json"
    suffix = 2
    while output_path.exists() or output_path in reserved:
        output_path = output_dir / f"{stem}-{suffix}.json"
        suffix += 1
    reserved.add(output_path)
    return output_path


def _draft_for_signal(
    *,
    record: dict[str, Any],
    envelope: dict[str, Any],
    signal: str,
    source_path: Path,
) -> dict[str, Any]:
    record_fields = _telemetry_record_lens(record)
    workflow = record_fields.workflow
    prompt_sources = _suggested_prompt_sources(workflow)
    suspect_prompts = _suspect_prompts_from_sources(prompt_sources, signal=signal)
    suspect_scripts = _suspect_scripts_from_record(record)
    return {
        "schema": AGENT_BEHAVIOR_CASE_DRAFT_SCHEMA,
        "status": "draft",
        "source": "telemetry",
        "app": _record_app(record, envelope),
        "app_version": _record_app_version(record, envelope),
        "workflow": _clean_text(workflow),
        "phase": _clean_text(record_fields.phase),
        "signal": signal,
        "severity": _severity_for_signal(record, signal),
        "target_suite": _target_suite(record),
        "prompt_sources_suggested": prompt_sources,
        "suspect_prompts": suspect_prompts,
        "suspect_scripts": suspect_scripts,
        "prevention_owner_note": _prevention_owner_note(prompts=suspect_prompts, scripts=suspect_scripts),
        "redacted_evidence": _redacted_evidence(record, envelope, signal=signal, source_path=source_path),
        "suggested_assertions": _suggested_assertions(signal),
        "promotion_checklist": _promotion_checklist(signal),
    }


def suggest_agent_behavior_cases_from_telemetry(
    input_path: Path,
    *,
    output_dir: Path,
    app: str = DEFAULT_TELEMETRY_APP,
    app_version: str | None = None,
    min_severity: str = "medium",
) -> dict[str, Any]:
    """Create reviewable behavior-corpus draft cases from redacted telemetry JSON."""
    drafts: list[dict[str, Any]] = []
    reserved_paths: set[Path] = set()
    skipped = 0
    for record, envelope, source_path in _telemetry_records(input_path):
        if _record_app(record, envelope) != app:
            skipped += 1
            continue
        if app_version and _record_app_version(record, envelope) != app_version:
            skipped += 1
            continue
        signals = _signals_for_record(record)
        selected = [signal for signal in signals if _passes_min_severity(record, signal, min_severity)]
        if not selected:
            skipped += 1
            continue
        for signal in selected:
            draft = _draft_for_signal(record=record, envelope=envelope, signal=signal, source_path=source_path)
            date_prefix = _draft_date(record, envelope)
            record_fields = _telemetry_record_lens(record)
            workflow_slug = _slug(record_fields.workflow or "workflow")
            signal_slug = _slug(signal)
            output_path = _unique_draft_output_path(
                output_dir,
                f"{date_prefix}-{signal_slug}-{workflow_slug}",
                reserved_paths,
            )
            drafts.append({"path": str(output_path), "draft": draft})
    if drafts:
        output_dir.mkdir(parents=True, exist_ok=True)
        for item in drafts:
            Path(item["path"]).write_text(json.dumps(item["draft"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": AGENT_BEHAVIOR_CASE_DRAFT_REPORT_SCHEMA,
        "status": "drafts_created" if drafts else "no_drafts",
        "app": app,
        "app_version": app_version or "",
        "min_severity": min_severity,
        "aggregate": {
            "draft_count": len(drafts),
            "skipped_record_count": skipped,
        },
        "drafts": [
            {
                "path": item["path"],
                "signal": item["draft"]["signal"],
                "target_suite": item["draft"]["target_suite"],
                "app_version": item["draft"]["app_version"],
            }
            for item in drafts
        ],
        "next_action": "review drafts, promote selected cases into a corpus suite, then rerun eval-agent-behavior-corpus"
        if drafts
        else "",
    }


def _looks_like_telemetry_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    fields = _telemetry_payload_lens(payload)
    schema = fields.schema_id
    return ".workflow-telemetry-envelope." in schema or ".workflow-run-record." in schema or bool(fields.records)


def _json_blocks_from_markdown(text: str) -> list[Any]:
    payloads: list[Any] = []
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            payloads.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return payloads


def _candidate_payloads(payload: Any) -> list[JsonObject]:
    candidates: list[JsonObject] = []
    if isinstance(payload, dict):
        fields = _BehaviorCandidatePayloadLens.model_validate(payload)
        candidates.extend(JsonObjectAdapter.validate_python(item) for item in fields.behavior_case_candidates)
        for item in fields.first_pass_prevention_candidates:
            enriched = dict(item)
            enriched.setdefault("case_kind", "first_pass_prevention")
            candidates.append(JsonObjectAdapter.validate_python(enriched))
        for message in fields.messages:
            message_fields = _BehaviorCandidateMessageLens.model_validate(message)
            for item in message_fields.behavior_case_candidates:
                enriched = dict(item)
                enriched.setdefault("source_message_id", message_fields.id)
                enriched.setdefault("source_kind", message_fields.source_kind)
                candidates.append(JsonObjectAdapter.validate_python(enriched))
            for item in message_fields.first_pass_prevention_candidates:
                enriched = dict(item)
                enriched.setdefault("case_kind", "first_pass_prevention")
                enriched.setdefault("source_message_id", message_fields.id)
                enriched.setdefault("source_kind", message_fields.source_kind)
                candidates.append(JsonObjectAdapter.validate_python(enriched))
    elif isinstance(payload, list):
        candidates.extend(JsonObjectAdapter.validate_python(item) for item in payload if isinstance(item, dict) and item.get("signal"))
    return candidates


def _sanitize_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(token in lower for token in ("content", "body", "html", "markdown", "raw", "token", "secret", "api_key", "script")):
                continue
            sanitized[str(key)] = _sanitize_evidence(item)
        return {key: item for key, item in sanitized.items() if item not in ("", [], {})}
    if isinstance(value, list):
        return [_sanitize_evidence(item) for item in value if _sanitize_evidence(item) not in ("", [], {})]
    if isinstance(value, str):
        return _clean_text(value, max_chars=700)
    return value


def _candidate_text_list(candidate: dict[str, Any], key: str) -> list[str]:
    value = candidate.get(key)
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, str) and value.strip():
        return [_clean_text(value)]
    return []


def _candidate_count_map(candidate: JsonObject, key: str) -> dict[str, int]:
    value = candidate.get(key)
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        name = _clean_text(raw_key)
        if not name:
            continue
        try:
            counts[name] = int(raw_count)
        except (TypeError, ValueError):
            counts[name] = 1
    return counts


def _candidate_signal(candidate: dict[str, Any]) -> str:
    signal = str(candidate.get("signal") or candidate.get("root_cause") or candidate.get("root_cause_code") or "")
    if signal:
        return signal
    evidence = json.dumps(candidate, ensure_ascii=False).lower()
    if "retry loop" in evidence or ("loop" in evidence and "retry" in evidence):
        return "agent.retry_loop"
    if "ignored next_action" in evidence or "ignorou next_action" in evidence:
        return "agent.ignored_next_action"
    if "wrong phase" in evidence or "fase errada" in evidence:
        return "agent.wrong_phase"
    if "generated script" in evidence or "script gerado" in evidence:
        return "agent.generated_script_workaround"
    if "missing error_context" in evidence or "sem error_context" in evidence:
        return "agent.missing_error_context"
    if "dry-run" in evidence and "apply" in evidence:
        return "dry_run_without_apply"
    return "agent.workflow_blocked"


def _candidate_workflow(candidate: JsonObject) -> str:
    raw_workflow = candidate.get("workflow")
    workflow = raw_workflow.strip() if isinstance(raw_workflow, str) else ""
    if workflow:
        return workflow
    text = json.dumps(candidate, ensure_ascii=False)
    match = re.search(r"/(?:mednotes:[a-z0-9_-]+|flashcards)", text, flags=re.I)
    return match.group(0) if match else ""


def _candidate_app_version(candidate: dict[str, Any]) -> str:
    for key in ("app_version", "version"):
        if str(candidate.get(key) or ""):
            return str(candidate[key])
    text = json.dumps(candidate, ensure_ascii=False)
    match = re.search(r"(?:app[_ ]version|vers[aã]o)\s*[:=` ]+\s*([0-9]+(?:\.[0-9]+){1,3})", text, flags=re.I)
    return match.group(1) if match else "unknown"


def _draft_from_candidate(
    candidate: dict[str, Any],
    *,
    source_path: Path,
    confidence: str,
) -> dict[str, Any]:
    signal = _candidate_signal(candidate)
    workflow = _candidate_workflow(candidate)
    source = str(candidate.get("source_kind") or candidate.get("source") or "agent_report")
    evidence = candidate.get("redacted_evidence") if isinstance(candidate.get("redacted_evidence"), dict) else {}
    sanitized_evidence = _sanitize_evidence(evidence or candidate)
    if isinstance(sanitized_evidence, dict):
        sanitized_evidence.setdefault("source_path", _serialized_evidence_source_path(source_path))
        sanitized_evidence.setdefault("signal", signal)
    else:
        sanitized_evidence = {
            "summary": _clean_text(sanitized_evidence),
            "source_path": _serialized_evidence_source_path(source_path),
            "signal": signal,
        }
    assertions = candidate.get("suggested_assertions")
    if not isinstance(assertions, list) or not all(isinstance(item, dict) for item in assertions):
        assertions = _suggested_assertions(signal)
    prompt_sources = candidate.get("prompt_sources_suggested")
    if not isinstance(prompt_sources, list):
        prompt_sources = candidate.get("prompt_surface")
    if not isinstance(prompt_sources, list):
        prompt_sources = _suggested_prompt_sources(workflow)
    prompt_sources = [str(item) for item in prompt_sources if str(item)]
    suspect_prompts = _surface_items(candidate.get("suspect_prompts"), kind="prompt")
    if not suspect_prompts:
        suspect_prompts = _suspect_prompts_from_sources(prompt_sources, signal=signal)
    suspect_scripts = _surface_items(candidate.get("suspect_scripts"), kind="script")
    target_suite = str(candidate.get("target_suite") or "")
    if not target_suite:
        target_suite = "extension_commands.core_behavior.v1" if _command_source_for_workflow(workflow) else "extension_skills.core_behavior.v1"
    draft = {
        "schema": AGENT_BEHAVIOR_CASE_DRAFT_SCHEMA,
        "status": "draft",
        "source": source,
        "confidence": confidence,
        "case_kind": str(candidate.get("case_kind") or "behavior_regression"),
        "app": str(candidate.get("app") or DEFAULT_TELEMETRY_APP),
        "app_version": _candidate_app_version(candidate),
        "workflow": _clean_text(workflow),
        "phase": _clean_text(candidate.get("phase")),
        "signal": signal,
        "severity": str(candidate.get("severity") or DEFAULT_SIGNAL_SEVERITY.get(signal, "medium")),
        "target_suite": target_suite,
        "prompt_sources_suggested": prompt_sources,
        "suspect_prompts": suspect_prompts,
        "suspect_scripts": suspect_scripts,
        "prevention_owner_note": _prevention_owner_note(prompts=suspect_prompts, scripts=suspect_scripts),
        "redacted_evidence": sanitized_evidence,
        "suggested_assertions": assertions,
        "promotion_checklist": _promotion_checklist(signal),
    }
    if draft["case_kind"] == "first_pass_prevention":
        prevention = {
            "prevention_type": _clean_text(candidate.get("prevention_type")),
            "optimization_class": _clean_text(candidate.get("optimization_class") or "first_pass_prevention"),
            "first_pass_failure_mode": _clean_text(candidate.get("first_pass_failure_mode"), max_chars=700),
            "bad_artifact_type": _clean_text(candidate.get("bad_artifact_type")),
            "failure_facets": _candidate_text_list(candidate, "failure_facets"),
            "suspected_upstream_prompt_source": _candidate_text_list(candidate, "suspected_upstream_prompt_source"),
            "desired_first_pass_behavior": _clean_text(candidate.get("desired_first_pass_behavior"), max_chars=700),
            "recommended_prompt_change": _clean_text(candidate.get("recommended_prompt_change"), max_chars=700),
            "recommended_contract_change": _clean_text(candidate.get("recommended_contract_change"), max_chars=700),
            "suggested_fixture": _clean_text(candidate.get("suggested_fixture")),
            "root_cause_counts": _candidate_count_map(candidate, "root_cause_counts"),
            "workflow_counts": _candidate_count_map(candidate, "workflow_counts"),
            "example_records": _sanitize_evidence(candidate.get("example_records") or []),
            "prompt_optimization_ready": bool(prompt_sources and assertions),
            "recovery_only": str(candidate.get("optimization_class") or "").lower() == "recovery_governance"
            or _clean_text(candidate.get("prevention_type")) == "recovery_only",
        }
        draft["first_pass_prevention"] = {
            key: value for key, value in prevention.items() if value not in ("", [], {})
        }
    if str(candidate.get("source_message_id") or ""):
        draft["source_message_id"] = str(candidate["source_message_id"])
    return draft


def _freeform_mentions_workbench(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("medical-notes-workbench", "wiki_medicina", "/mednotes:", "linker", "workbench"))


def _freeform_candidate(text: str, *, source_kind: str) -> dict[str, Any] | None:
    if not _freeform_mentions_workbench(text):
        return None
    lowered = text.lower()
    signal = ""
    if "retry loop" in lowered or ("retry" in lowered and "loop" in lowered) or "repetiu diagnóstico" in lowered:
        signal = "agent.retry_loop"
    elif "ignored next_action" in lowered or "ignorou next_action" in lowered:
        signal = "agent.ignored_next_action"
    elif "wrong phase" in lowered or "fase errada" in lowered:
        signal = "agent.wrong_phase"
    elif "generated script" in lowered or "script gerado" in lowered or "criou script" in lowered:
        signal = "agent.generated_script_workaround"
    elif "missing error_context" in lowered or "sem error_context" in lowered:
        signal = "agent.missing_error_context"
    if not signal:
        return None
    workflow_match = re.search(r"/(?:mednotes:[a-z0-9_-]+|flashcards)", text, flags=re.I)
    version_match = re.search(r"(?:app[_ ]version|vers[aã]o)\s*[:=` ]+\s*([0-9]+(?:\.[0-9]+){1,3})", text, flags=re.I)
    return {
        "source_kind": source_kind,
        "app_version": version_match.group(1) if version_match else "unknown",
        "workflow": workflow_match.group(0) if workflow_match else "",
        "signal": signal,
        "severity": DEFAULT_SIGNAL_SEVERITY.get(signal, "medium"),
        "redacted_evidence": {"summary": _clean_text(text, max_chars=700)},
    }


def _draft_items_from_evidence_payload(
    payload: Any,
    *,
    source_path: Path,
    source_kind: str,
) -> tuple[list[dict[str, Any]], str]:
    candidates = _candidate_payloads(payload)
    if candidates:
        return [
            _draft_from_candidate(candidate, source_path=source_path, confidence="medium")
            for candidate in candidates
        ], "structured_candidates"
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False)
    candidate = _freeform_candidate(text, source_kind=source_kind)
    if candidate:
        return [_draft_from_candidate(candidate, source_path=source_path, confidence="low")], "freeform_inference"
    return [], "no_candidate_signal"


class _DraftReportAggregateFields(ContractModel):
    """Counts that decide the wrapper report for draft generation."""

    model_config = ConfigDict(extra="ignore")

    draft_count: int = Field(default=0, ge=0, strict=True)
    skipped_record_count: int = Field(default=0, ge=0, strict=True)


class _DraftReportFields(ContractModel):
    """Typed lens for draft-generation reports before directory aggregation."""

    model_config = ConfigDict(extra="ignore")

    aggregate: _DraftReportAggregateFields = Field(default_factory=_DraftReportAggregateFields)
    drafts: list[JsonObject] = Field(default_factory=list)


def _write_draft_items(
    draft_payloads: list[dict[str, Any]],
    *,
    output_dir: Path,
    source_path: Path,
    app: str,
    app_version: str | None,
    min_severity: str,
    skipped: int,
    mode: str,
) -> dict[str, Any]:
    drafts: list[dict[str, Any]] = []
    reserved_paths: set[Path] = set()
    for draft in draft_payloads:
        if draft.get("app") != app:
            skipped += 1
            continue
        if app_version and draft.get("app_version") != app_version:
            skipped += 1
            continue
        if SEVERITY_RANK.get(str(draft.get("severity") or "low"), 0) < SEVERITY_RANK.get(min_severity, 2):
            skipped += 1
            continue
        date_prefix = datetime.now(UTC).date().isoformat()
        signal_slug = _slug(str(draft.get("signal") or "evidence"))
        workflow_slug = _slug(str(draft.get("workflow") or source_path.stem))
        output_path = _unique_draft_output_path(
            output_dir,
            f"{date_prefix}-{signal_slug}-{workflow_slug}",
            reserved_paths,
        )
        drafts.append({"path": str(output_path), "draft": draft})
    if drafts:
        output_dir.mkdir(parents=True, exist_ok=True)
        for item in drafts:
            Path(item["path"]).write_text(json.dumps(item["draft"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": AGENT_BEHAVIOR_CASE_DRAFT_REPORT_SCHEMA,
        "status": "drafts_created" if drafts else "no_drafts",
        "app": app,
        "app_version": app_version or "",
        "min_severity": min_severity,
        "mode": mode,
        "aggregate": {
            "draft_count": len(drafts),
            "skipped_record_count": skipped,
        },
        "drafts": [
            {
                "path": item["path"],
                "signal": item["draft"]["signal"],
                "target_suite": item["draft"]["target_suite"],
                "app_version": item["draft"]["app_version"],
                "source": item["draft"].get("source", ""),
                "confidence": item["draft"].get("confidence", ""),
            }
            for item in drafts
        ],
        "next_action": "review drafts, promote selected cases into a corpus suite, then rerun eval-agent-behavior-corpus"
        if drafts
        else "",
    }


def _merge_existing_draft_report(result: JsonObject, existing_drafts: list[JsonObject]) -> JsonObject:
    """Merge telemetry-created drafts into the directory-level report without dict mutation."""

    result_fields = _DraftReportFields.model_validate(result)
    drafts = [*existing_drafts, *result_fields.drafts]
    merged = dict(result)
    merged.update(
        {
            "status": "drafts_created",
            "aggregate": {
                **result_fields.aggregate.model_dump(mode="json"),
                "draft_count": len(drafts),
            },
            "drafts": drafts,
            "next_action": "review drafts, promote selected cases into a corpus suite, then rerun eval-agent-behavior-corpus",
        }
    )
    return JsonObjectAdapter.validate_python(merged)


def suggest_agent_behavior_cases_from_evidence(
    input_path: Path,
    *,
    output_dir: Path,
    app: str = DEFAULT_TELEMETRY_APP,
    app_version: str | None = None,
    min_severity: str = "medium",
    source_kind: str = "auto",
) -> dict[str, Any]:
    """Create reviewable behavior-corpus drafts from telemetry, reports, manifests, or freeform evidence."""
    if input_path.is_dir():
        draft_payloads: list[dict[str, Any]] = []
        existing_drafts: list[dict[str, Any]] = []
        skipped = 0
        modes: set[str] = set()
        for path in _evidence_payload_files(input_path):
            try:
                payload = _read_json_any(path)
                if _looks_like_telemetry_payload(payload):
                    telemetry_result = suggest_agent_behavior_cases_from_telemetry(
                        path,
                        output_dir=output_dir,
                        app=app,
                        app_version=app_version,
                        min_severity=min_severity,
                    )
                    telemetry_fields = _DraftReportFields.model_validate(telemetry_result)
                    modes.add("telemetry")
                    skipped += telemetry_fields.aggregate.skipped_record_count
                    existing_drafts.extend(telemetry_fields.drafts)
                    continue
                items, mode = _draft_items_from_evidence_payload(
                    payload,
                    source_path=path,
                    source_kind="agent_report" if source_kind == "auto" else source_kind,
                )
            except ValidationError:
                text = path.read_text(encoding="utf-8")
                items = []
                mode = "freeform_inference"
                for payload in _json_blocks_from_markdown(text):
                    block_items, block_mode = _draft_items_from_evidence_payload(
                        payload,
                        source_path=path,
                        source_kind="inbox_report" if source_kind == "auto" else source_kind,
                    )
                    if block_items:
                        mode = block_mode
                        items.extend(block_items)
                if not items:
                    candidate = _freeform_candidate(text, source_kind="agent_report" if source_kind == "auto" else source_kind)
                    if candidate:
                        items.append(_draft_from_candidate(candidate, source_path=path, confidence="low"))
            if items:
                draft_payloads.extend(items)
                modes.add(mode)
        result = _write_draft_items(
            draft_payloads,
            output_dir=output_dir,
            source_path=input_path,
            app=app,
            app_version=app_version,
            min_severity=min_severity,
            skipped=skipped,
            mode="+".join(sorted(modes)) if modes else "no_candidate_signal",
        )
        if existing_drafts:
            result = _merge_existing_draft_report(JsonObjectAdapter.validate_python(result), existing_drafts)
        return result

    try:
        payload = _read_json_any(input_path)
        if _looks_like_telemetry_payload(payload):
            return suggest_agent_behavior_cases_from_telemetry(
                input_path,
                output_dir=output_dir,
                app=app,
                app_version=app_version,
                min_severity=min_severity,
            )
        draft_payloads, mode = _draft_items_from_evidence_payload(
            payload,
            source_path=input_path,
            source_kind="agent_report" if source_kind == "auto" else source_kind,
        )
    except ValidationError:
        text = input_path.read_text(encoding="utf-8")
        json_payloads = _json_blocks_from_markdown(text)
        draft_payloads = []
        mode = "freeform_inference"
        for payload in json_payloads:
            items, item_mode = _draft_items_from_evidence_payload(
                payload,
                source_path=input_path,
                source_kind="inbox_report" if source_kind == "auto" else source_kind,
            )
            if items:
                mode = item_mode
                draft_payloads.extend(items)
        if not draft_payloads:
            candidate = _freeform_candidate(text, source_kind="agent_report" if source_kind == "auto" else source_kind)
            if candidate:
                draft_payloads.append(_draft_from_candidate(candidate, source_path=input_path, confidence="low"))
    return _write_draft_items(
        draft_payloads,
        output_dir=output_dir,
        source_path=input_path,
        app=app,
        app_version=app_version,
        min_severity=min_severity,
        skipped=0,
        mode=mode if "mode" in locals() else "no_candidate_signal",
    )
