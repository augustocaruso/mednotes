"""Batch planning/apply helpers for med-link-graph-curator vocabulary work."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_ingestion import apply_semantic_ingestion
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_map import initialize_vocabulary_db
from mednotes.domains.wiki.common import DOCS_RELPATH, ValidationError, wiki_cli_command
from mednotes.domains.wiki.contracts.curator import (
    CuratorApplyReceipt,
    CuratorBatchPlan,
    CuratorIgnoredOutputNotice,
    CuratorManifest,
    CuratorManifestItem,
    CuratorPromptEvalReport,
    NoteSemanticIngestionOutput,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, JsonValue, contract_error
from mednotes.platform.paths import extension_root as _resolve_extension_root

VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA = "medical-notes-workbench.vocabulary-curator-batch-plan.v1"
VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA = (
    "medical-notes-workbench.vocabulary-curator-batch-output-manifest.v1"
)
VOCABULARY_CURATOR_BATCH_RECEIPT_SCHEMA = "medical-notes-workbench.vocabulary-curator-batch-receipt.v1"
AGENT_WORK_PACKET_SCHEMA = "medical-notes-workbench.agent-work-packet.v1"
NOTE_SEMANTIC_INGESTION_SCHEMA = "medical-notes-workbench.note-semantic-ingestion.v1"
CURATOR_PROMPT_EVAL_SCHEMA = "medical-notes-workbench.curator-prompt-eval.v1"
CURATOR_PROMPT_IDENTITY_SCHEMA = "medical-notes-workbench.curator-prompt-identity.v1"
DEV_ESCAPE_ENV = "MEDNOTES_ALLOW_DEV_ESCAPE"

ALLOWED_CURATOR_ACTIONS = ["read_note", "write_semantic_ingestion_output", "defer_work_item"]
FORBIDDEN_CURATOR_ACTIONS = [
    "direct_sql_mutation",
    "markdown_edit",
    "subagent_call",
    "generated_write_script",
    "manual_manifest_editing",
    "hardcoded_local_path",
    "mass_markdown_rewrite",
]
CURATOR_STOP_CONDITIONS = [
    "schema_drift",
    "sqlite_integrity_error",
    "queue_inconsistent",
    "path_mismatch",
    "path_case_mismatch",
    "content_hash_mismatch",
    "timeout_or_max_turns",
    "missing_official_command",
]
CURATOR_QUALITY_RUBRIC = {
    "primary_meaning_atomicity": "primary_meaning must describe exactly one atomic medical concept represented by the note.",
    "atomicity_signal": "non_atomic_note deferred work must include body-based semantic_signal; DB decides split_required vs candidate/defer.",
    "alias_precision": "aliases must be medically useful, strict, and not broader than the note concept.",
    "link_policy_conservatism": "use direct only for one surface, one meaning, one canonical note; otherwise requires_context/blocked/defer.",
    "defer_when_uncertain": "split, duplicate, missing canonical note, or low confidence must become deferred_work_items, not guessed output.",
    "evidence_redaction": "summaries/receipts must not include raw clinical prose, Markdown body, images, HTML, embeddings, or tokens.",
}
CURATOR_OUTPUT_CONTRACT = {
    "must_include": [
        "schema",
        "workflow",
        "phase",
        "agent",
        "source_workflow",
        "note_path",
        "content_hash",
        "primary_meaning",
        "aliases",
        "deferred_work_items",
        "confidence",
        "agent_metrics",
    ],
    "must_not_include": ["raw_markdown", "clinical_body", "html", "images", "embeddings", "api_keys"],
}
COMPLEX_QUEUE_FLAGS = {
    "ambiguous_surface",
    "duplicate_candidate",
    "needs_merge",
    "requires_context",
    "suspected_non_atomic",
    "split_candidate",
    "yaml_alias_conflict",
}
CURATOR_PROMPT_SOURCE_PATHS = [
    "agents/med-link-graph-curator.md",
    "docs/agent-role-contracts.md",
    "docs/merge-policy.md",
    "docs/semantic-linker.md",
    "docs/atomicity-splitting-policy.md",
    "docs/agent-prompt-hardening.md",
]


def agent_output_ignored_notice(next_action: str = "") -> str:
    action = next_action.strip() or "repita pela rota oficial antes de aplicar."
    return f"ATENÇÃO: este output será ignorado e não será aplicado. Use a rota oficial: {action}"


def curator_agent_event(
    *,
    code: str,
    root_cause_code: str,
    next_action: str,
    artifact_path: str = "",
    reason: str = "",
    severity: str = "medium",
) -> dict[str, Any]:
    sample: dict[str, str] = {"root_cause_code": root_cause_code}
    if reason:
        sample["reason"] = reason
    return {
        "schema": "medical-notes-workbench.agent-event.v1",
        "type": "curator_contract_bypass",
        "code": code,
        "severity": severity,
        "root_cause_code": root_cause_code,
        "workflow": "/mednotes:link",
        "phase": "vocabulary_curation",
        "recovery_command": next_action,
        "artifact_path": artifact_path,
        "redacted_sample": sample,
        "next_action": next_action,
}


class _CuratorPromptIdentityFields(ContractModel):
    """Typed view of prompt identity hashes used to bind eval reports to plans."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    aggregate_hash: str = ""


class _CuratorPromptEvalFingerprintsFields(ContractModel):
    """Typed view of the fingerprints that make prompt eval reports replay-safe."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    plan_hash: str = ""
    manifest_hash: str = ""


class _CuratorPromptEvalAggregateFields(ContractModel):
    """Typed aggregate counters used to decide whether curator apply can run."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    score: JsonValue = None
    issue_count: int = Field(default=0, ge=0, strict=True)


class _CuratorPromptEvalItemFields(ContractModel):
    """Per-output prompt-eval status consumed by the apply gate."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    status: str = ""


class _CuratorPromptEvalBlockFields(ContractModel):
    """Prompt-eval summary shape used to build a blocked apply receipt."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    status: str = "blocked"
    blocked_reason: str = ""
    next_action: str = ""
    agent_event: JsonObject | None = None


class _CuratorManifestPathFields(ContractModel):
    """Optional manifest path evidence for prompt-eval skip notices."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    path: str = ""


def _pending_queue_rows(db_path: Path, *, limit: int) -> list[sqlite3.Row]:
    initialize_vocabulary_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                """
                SELECT q.note_id, q.note_path, q.content_hash, q.queue_flags_json,
                       q.assigned_agent, q.status, n.title
                FROM note_semantic_ingestion_queue q
                LEFT JOIN notes n ON n.id = q.note_id
                WHERE q.status IN ('pending', 'claimed')
                ORDER BY q.updated_at ASC, q.note_path ASC
                LIMIT ?
                """,
                (limit,),
            )
        )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "note"


def _canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def curator_plan_hash(plan: dict[str, Any]) -> str:
    hash_material: dict[str, Any] = {
        "schema": plan.get("schema"),
        "phase": plan.get("phase"),
        "status": plan.get("status"),
        "skipped_reason": plan.get("skipped_reason"),
    }
    work_items = plan.get("work_items")
    normalized_items: list[Any] = []
    if isinstance(work_items, list):
        corpus_keys = (
            "schema",
            "work_id",
            "app",
            "workflow",
            "phase",
            "note_path",
            "note_path_exists",
            "path_case_check",
            "content_hash",
            "title",
            "queue_flags",
            "difficulty_route",
            "expected_output_schema",
        )
        for item in work_items:
            if isinstance(item, dict):
                normalized_items.append({key: item.get(key) for key in corpus_keys if key in item})
            else:
                normalized_items.append(item)
    hash_material["work_items"] = normalized_items
    return _canonical_payload_hash(hash_material)


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _default_extension_root() -> Path:
    return _resolve_extension_root()


def _source_fingerprint(*, extension_root: Path, relative_path: str) -> dict[str, Any]:
    path = extension_root / relative_path
    if not path.is_file():
        return {
            "path": relative_path,
            "exists": False,
            "sha256": "",
            "byte_count": 0,
            "word_count": 0,
        }
    content = path.read_bytes()
    text = content.decode("utf-8", errors="replace")
    return {
        "path": relative_path,
        "exists": True,
        "sha256": _sha256_bytes(content),
        "byte_count": len(content),
        "word_count": len(text.split()),
    }


def build_curator_prompt_identity(*, extension_root: Path | None = None) -> dict[str, Any]:
    root = extension_root or _default_extension_root()
    sources = [
        _source_fingerprint(extension_root=root, relative_path=relative_path)
        for relative_path in CURATOR_PROMPT_SOURCE_PATHS
    ]
    aggregate_material = [
        {"path": source["path"], "exists": source["exists"], "sha256": source["sha256"]}
        for source in sources
    ]
    return {
        "schema": CURATOR_PROMPT_IDENTITY_SCHEMA,
        "agent": "med-link-graph-curator",
        "aggregate_hash": _canonical_payload_hash(aggregate_material),
        "sources": sources,
    }


def _queue_flags(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]


def _path_case_check(path: Path) -> dict[str, str]:
    if not path.exists():
        return {"status": "missing", "expected_path": str(path), "actual_path": ""}
    current = path
    parts: list[str] = []
    while current.parent != current:
        parts.append(current.name)
        current = current.parent
        if current.exists():
            break
    actual = current
    for part in reversed(parts):
        try:
            matches = [child.name for child in actual.iterdir() if child.name.casefold() == part.casefold()]
        except OSError:
            return {"status": "unknown", "expected_path": str(path), "actual_path": str(path)}
        if not matches:
            return {"status": "missing", "expected_path": str(path), "actual_path": ""}
        actual = actual / matches[0]
    status = "exact" if str(actual) == str(path) else "case_mismatch"
    return {"status": status, "expected_path": str(path), "actual_path": str(actual)}


def _difficulty_route(*, flags: list[str], path_case_check: dict[str, str]) -> dict[str, Any]:
    path_status = str(path_case_check.get("status") or "")
    if path_status in {"missing", "case_mismatch"}:
        return {
            "route": "blocked_preflight",
            "max_turns": 2,
            "focus": ["return_blocked_output", path_status],
            "efficiency_rule": "Do not read or reason semantically until the parent fixes path/hash preflight.",
        }
    if set(flags) & COMPLEX_QUEUE_FLAGS:
        return {
            "route": "complex_semantic_review",
            "max_turns": 12,
            "focus": [
                "split_warning_or_deferred_work_expected",
                "alias_ambiguity_review",
                "duplicate_or_merge_detection",
            ],
            "efficiency_rule": "Spend budget on classification and deferral; do not solve merge/split inside this packet.",
        }
    return {
        "route": "simple_atomic",
        "max_turns": 8,
        "focus": ["primary_meaning", "strict_aliases", "direct_vs_requires_context"],
        "efficiency_rule": "Produce the smallest valid semantic-ingestion object; avoid broad taxonomy or note rewriting.",
    }


def _work_item_from_row(
    row: sqlite3.Row,
    *,
    db_path: Path,
    output_dir: Path,
    index: int,
    prompt_identity: dict[str, Any],
) -> dict[str, Any]:
    note_path = str(row["note_path"])
    path = Path(note_path)
    title = str(row["title"] or Path(note_path).stem)
    work_id = f"vocab-curation-{index:03d}-{_slug(title)}"
    flags = _queue_flags(row["queue_flags_json"])
    path_check = _path_case_check(path)
    route = _difficulty_route(flags=flags, path_case_check=path_check)
    return {
        "schema": AGENT_WORK_PACKET_SCHEMA,
        "work_id": work_id,
        "app": "medical-notes-workbench",
        "workflow": "/mednotes:link",
        "phase": "vocabulary_curation",
        "agent": str(row["assigned_agent"] or "med-link-graph-curator"),
        "source_workflow": "/mednotes:link",
        "db_path": str(db_path),
        "note_path": note_path,
        "note_path_exists": path.exists(),
        "path_case_check": path_check,
        "content_hash": str(row["content_hash"]),
        "title": title,
        "queue_flags": flags,
        "prompt_identity": dict(prompt_identity),
        "difficulty_route": route,
        "quality_rubric": dict(CURATOR_QUALITY_RUBRIC),
        "output_contract": dict(CURATOR_OUTPUT_CONTRACT),
        "allowed_actions": list(ALLOWED_CURATOR_ACTIONS),
        "forbidden_actions": list(FORBIDDEN_CURATOR_ACTIONS),
        "stop_conditions": list(CURATOR_STOP_CONDITIONS),
        "retry_scope": "single_work_item",
        "max_turns_policy": {"max_turns": 12, "on_exhaustion": "return_deferred_work_item"},
        "expected_output_schema": NOTE_SEMANTIC_INGESTION_SCHEMA,
        "output_path": str(output_dir / f"{work_id}.semantic-ingestion.json"),
        "error_context": {
            "phase": "vocabulary_curation",
            "retry_scope": "single_work_item",
            "next_action": "return blocked/deferred item; parent will decide official recovery command",
        },
        "instructions": [
            "Read exactly this note.",
            "Return medical-notes-workbench.note-semantic-ingestion.v1 with workflow=/mednotes:link, phase=vocabulary_curation, agent=med-link-graph-curator and source_workflow=/mednotes:link.",
            "Do not call subagents.",
            "Do not invoke @generalist",
            "Only the parent orchestrator may launch med-link-graph-curator directly",
            "Use deferred_work_items for duplicate, split, missing canonical note or merge work.",
            f"For atomicity, follow {DOCS_RELPATH}/atomicity-splitting-policy.md; include body-based semantic_signal and never use title-only atomicity claims.",
        ],
    }


def build_vocabulary_curator_batch_plan(
    *,
    db_path: Path,
    batch_id: str,
    output_dir: Path,
    limit: int = 20,
) -> dict[str, Any]:
    if limit < 1:
        raise ValidationError("vocabulary curator batch limit must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = _pending_queue_rows(db_path, limit=limit)
    prompt_identity = build_curator_prompt_identity()
    work_items = [
        _work_item_from_row(
            row,
            db_path=db_path,
            output_dir=output_dir,
            index=index,
            prompt_identity=prompt_identity,
        )
        for index, row in enumerate(rows, start=1)
    ]
    return {
        "schema": VOCABULARY_CURATOR_BATCH_PLAN_SCHEMA,
        "phase": "vocabulary_curation",
        "status": "ready" if work_items else "skipped",
        "skipped_reason": "" if work_items else "no_pending_semantic_ingestion",
        "batch_id": batch_id,
        "db_path": str(db_path),
        "prompt_identity": prompt_identity,
        "prompt_eval_report_path": str(output_dir.parent / "curator-prompt-eval.json"),
        "item_count": len(work_items),
        "work_items": work_items,
        "parallel_safe": len(work_items) > 1,
        "max_concurrency": min(5, max(1, len(work_items))) if work_items else 0,
        "rules": [
            "Parent orchestrator must launch med-link-graph-curator directly",
            "Never delegate vocabulary curation orchestration to @generalist",
            "Spawn at most one med-link-graph-curator per work_item.",
            "The subagent must not call another subagent.",
            "The subagent writes only output_path.",
            "Parent runs eval-curator-batch --report before apply-curator-batch.",
            "Parent applies outputs with apply-curator-batch --prompt-eval.",
        ],
    }


def _read_json_object(path: Path, *, label: str) -> JsonObject:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} not found: {path}") from exc
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            f"artifact_encoding.unsupported_utf16: {label} {path} is UTF-16; "
            "regenerate it with collect-curator-outputs/eval-curator-batch or write UTF-8 without BOM."
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"artifact_encoding.invalid_utf8: {label} {path} must be UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return JsonObjectAdapter.validate_python(payload)


def _validate_curator_batch_plan(plan: JsonObject) -> CuratorBatchPlan:
    try:
        return CuratorBatchPlan.model_validate(plan)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="curator batch plan invalid") from exc


def _validate_curator_manifest(manifest: JsonObject) -> CuratorManifest:
    try:
        return CuratorManifest.model_validate(manifest)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="curator batch manifest invalid") from exc


def _validate_curator_prompt_eval_report(report: JsonObject) -> CuratorPromptEvalReport:
    try:
        return CuratorPromptEvalReport.model_validate(report)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="curator prompt eval invalid") from exc


def _validate_note_semantic_ingestion_output(payload: JsonObject) -> NoteSemanticIngestionOutput:
    try:
        return NoteSemanticIngestionOutput.model_validate(payload)
    except PydanticValidationError as exc:
        for error in exc.errors():
            loc = tuple(error.get("loc", ()))
            if len(loc) >= 3 and loc[0] == "aliases" and loc[2] == "text":
                alias_index = loc[1]
                aliases = payload.get("aliases")
                alias = aliases[alias_index] if isinstance(alias_index, int) and isinstance(aliases, list) else None
                if isinstance(alias, dict) and alias.get("surface"):
                    raise ValidationError(
                        f"semantic ingestion aliases[{alias_index}].text is required; "
                        "use aliases[].text, not aliases[].surface"
                    ) from exc
        first_error = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first_error.get("loc", ())) or "$"
        msg = str(first_error.get("msg") or str(exc))
        invalid_input = first_error.get("input")
        input_suffix = f" (input={invalid_input!r})" if invalid_input is not None else ""
        raise ValidationError(f"subagent_output_contract.invalid: {loc}: {msg}{input_suffix}") from exc


def _finalize_curator_apply_receipt(payload: JsonObject) -> JsonObject:
    try:
        receipt = CuratorApplyReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="curator apply receipt invalid") from exc
    return receipt.model_dump(mode="json", by_alias=True, exclude_none=True)


def _json_text_field(payload: JsonObject, field_name: str, default: str = "") -> str:
    value = payload.get(field_name)
    if value is None:
        return default
    return str(value)


def _json_object_field(payload: JsonObject, field_name: str) -> JsonObject:
    value = payload.get(field_name)
    if isinstance(value, dict):
        return JsonObjectAdapter.validate_python(value)
    return {}


def _manifest_items(manifest: dict[str, Any]) -> list[dict[str, str]]:
    typed_manifest = _validate_curator_manifest(manifest)
    return [item.model_dump(mode="json", exclude_defaults=True) for item in typed_manifest.items]


def _prompt_eval_summary(
    *,
    plan: JsonObject,
    manifest: JsonObject,
    prompt_eval_path: Path | None,
    skip_prompt_eval: bool,
    skip_prompt_eval_reason: str = "",
) -> JsonObject:
    if prompt_eval_path is not None and skip_prompt_eval:
        raise ValidationError("curator_prompt_eval_options_conflict: --prompt-eval and --skip-prompt-eval are mutually exclusive")
    if skip_prompt_eval:
        reason = skip_prompt_eval_reason.strip()
        manifest_fields = _CuratorManifestPathFields.model_validate(manifest)
        if not reason:
            raise ValidationError("curator_prompt_eval_skip_reason_required: skip prompt eval requires an explicit reason")
        if os.environ.get(DEV_ESCAPE_ENV) != "1":
            next_action = (
                "Use eval-curator-batch --report e aplique com --prompt-eval. "
                "--skip-prompt-eval não aplica outputs de curadoria em workflows reais."
            )
            return {
                "status": "dev_escape_disabled",
                "blocked_reason": "curator_prompt_eval.dev_escape_disabled",
                "required_env": DEV_ESCAPE_ENV,
                "reason": reason,
                "next_action": next_action,
                "agent_event": curator_agent_event(
                    code="agent.curator_prompt_eval_skip_attempt",
                    root_cause_code="curator_prompt_eval.dev_escape_disabled",
                    next_action=next_action,
                    artifact_path=manifest_fields.path,
                    reason=reason,
                ),
            }
        next_action = (
            "Use eval-curator-batch --report e aplique com --prompt-eval. "
            "--skip-prompt-eval não aplica outputs de curadoria; o output será ignorado até passar pela rota oficial."
        )
        return {
            "status": "dev_escape_ignored",
            "blocked_reason": "curator_prompt_eval.dev_escape_ignored",
            "reason": reason,
            "next_action": next_action,
            "agent_event": curator_agent_event(
                code="agent.curator_prompt_eval_skip_attempt",
                root_cause_code="curator_prompt_eval.dev_escape_ignored",
                next_action=wiki_cli_command(
                    "eval-curator-batch",
                    "--plan",
                    "<plan>",
                    "--outputs",
                    "<manifest>",
                    "--report",
                    "<curator-prompt-eval.json>",
                    "--json",
                ),
                artifact_path=manifest_fields.path,
                reason=reason,
            ),
        }
    if prompt_eval_path is None:
        raise ValidationError(
            "curator_prompt_eval_required: curator prompt eval required before apply-curator-batch; run eval-curator-batch "
            "--plan <plan> --outputs <manifest> --report <report> --json and pass --prompt-eval <report>"
        )
    prompt_eval_report = _validate_curator_prompt_eval_report(
        _read_json_object(prompt_eval_path, label="curator prompt eval")
    )
    fingerprints = _CuratorPromptEvalFingerprintsFields.model_validate(prompt_eval_report.input_fingerprints)
    if not prompt_eval_report.input_fingerprints:
        raise ValidationError("curator prompt eval requires input_fingerprints")
    expected_plan_hash = curator_plan_hash(plan)
    expected_manifest_hash = f"sha256:{_validate_curator_manifest(manifest).fingerprint()}"
    plan_prompt = _CuratorPromptIdentityFields.model_validate(_json_object_field(plan, "prompt_identity"))
    report_prompt = _CuratorPromptIdentityFields.model_validate(prompt_eval_report.prompt_identity)
    if fingerprints.plan_hash != expected_plan_hash:
        raise ValidationError("curator prompt eval plan_hash mismatch")
    if fingerprints.manifest_hash != expected_manifest_hash:
        next_action = "Recriar manifest e avaliacao pela rota oficial antes de aplicar."
        return {
            "status": "blocked",
            "blocked_reason": "curator_prompt_eval.inconsistent_report",
            "root_cause": "curator_batch.prompt_eval_manifest_mismatch",
            "path": str(prompt_eval_path),
            "next_action": next_action,
            "prompt_identity": prompt_eval_report.prompt_identity,
            "input_fingerprints": {
                "plan_hash": expected_plan_hash,
                "manifest_hash": expected_manifest_hash,
                "prompt_identity_hash": plan_prompt.aggregate_hash,
            },
        }
    if plan_prompt.aggregate_hash != report_prompt.aggregate_hash:
        raise ValidationError("curator prompt eval prompt_identity mismatch")
    aggregate = _CuratorPromptEvalAggregateFields.model_validate(prompt_eval_report.aggregate)
    report_status = prompt_eval_report.status
    next_action = prompt_eval_report.next_action
    item_statuses = [_CuratorPromptEvalItemFields.model_validate(item).status for item in prompt_eval_report.items]
    pass_status_inconsistent = (
        report_status == "pass"
        and (
            aggregate.issue_count != 0
            or bool(prompt_eval_report.aggregate_issues)
            or any(status != "pass" for status in item_statuses)
        )
    )
    if pass_status_inconsistent:
        next_action = (
            "Regenerar curator-prompt-eval com eval-curator-batch a partir do plan/manifest oficiais; "
            "não edite o relatório de avaliação manualmente."
        )
        return {
            "status": "invalid",
            "blocked_reason": "curator_prompt_eval.inconsistent_report",
            "path": str(prompt_eval_path),
            "score": aggregate.score,
            "issue_count": aggregate.issue_count,
            "next_action": next_action,
            "agent_event": curator_agent_event(
                code="agent.curator_prompt_eval_manual_status_edit",
                root_cause_code="curator_prompt_eval.inconsistent_report",
                next_action=next_action,
                artifact_path=str(prompt_eval_path),
                reason="pass_status_inconsistent_with_report_issues",
            ),
            "prompt_identity": prompt_eval_report.prompt_identity,
            "input_fingerprints": {
                "plan_hash": expected_plan_hash,
                "manifest_hash": expected_manifest_hash,
                "prompt_identity_hash": plan_prompt.aggregate_hash,
            },
        }
    if report_status not in {"pass", "needs_review"}:
        return {
            "status": "invalid",
            "blocked_reason": "curator_prompt_eval.invalid_status",
            "path": str(prompt_eval_path),
            "score": aggregate.score,
            "issue_count": aggregate.issue_count,
            "next_action": (
                next_action
                or "Regenerar curator-prompt-eval com eval-curator-batch; status permitido é pass ou needs_review."
            ),
            "agent_event": curator_agent_event(
                code=(
                    "agent.curator_prompt_eval_manual_status_edit"
                    if report_status == "approved"
                    else "agent.curator_prompt_eval_invalid_status"
                ),
                root_cause_code="curator_prompt_eval.invalid_status",
                next_action=(
                    next_action
                    or "Regenerar curator-prompt-eval com eval-curator-batch; status permitido é pass ou needs_review."
                ),
                artifact_path=str(prompt_eval_path),
                reason=f"invalid_status:{report_status}",
            ),
            "prompt_identity": prompt_eval_report.prompt_identity,
            "input_fingerprints": {
                "plan_hash": expected_plan_hash,
                "manifest_hash": expected_manifest_hash,
                "prompt_identity_hash": plan_prompt.aggregate_hash,
            },
        }
    return {
        "status": report_status,
        "path": str(prompt_eval_path),
        "score": aggregate.score,
        "issue_count": aggregate.issue_count,
        "next_action": next_action,
        "prompt_identity": prompt_eval_report.prompt_identity,
        "input_fingerprints": {
            "plan_hash": expected_plan_hash,
            "manifest_hash": expected_manifest_hash,
            "prompt_identity_hash": plan_prompt.aggregate_hash,
        },
    }


def _blocked_by_prompt_eval_receipt(
    *,
    plan: JsonObject,
    db_path: Path,
    by_work_id: dict[str, JsonObject],
    manifest_items: list[dict[str, str]],
    prompt_eval: JsonObject,
) -> JsonObject:
    prompt_eval_fields = _CuratorPromptEvalBlockFields.model_validate(prompt_eval)
    plan_fields = CuratorBatchPlan.model_validate(plan)
    prompt_eval_status = prompt_eval_fields.status
    blocked_reason = (
        prompt_eval_fields.blocked_reason
        or (
            "curator_prompt_eval.needs_review"
            if prompt_eval_status == "needs_review"
            else "curator_prompt_eval.blocked"
        )
    )
    next_action = (
        prompt_eval_fields.next_action
        or "Revisar outputs e prompt/rubrica, regenerar curator-prompt-eval e repetir apply-curator-batch com --prompt-eval."
    )
    agent_events = [prompt_eval_fields.agent_event] if prompt_eval_fields.agent_event is not None else []
    prompt_eval_payload = {key: value for key, value in prompt_eval.items() if key != "agent_event"} if agent_events else prompt_eval
    return _finalize_curator_apply_receipt({
        "schema": VOCABULARY_CURATOR_BATCH_RECEIPT_SCHEMA,
        "phase": "vocabulary_curation",
        "status": "blocked",
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "agent_notice": agent_output_ignored_notice(next_action),
        "required_inputs": ["prompt_eval"],
        "human_decision_required": False,
        "batch_id": plan_fields.batch_id,
        "db_path": str(db_path),
        "prompt_eval": prompt_eval_payload,
        "agent_events": agent_events,
        "plan_item_count": len(by_work_id),
        "manifest_item_count": len(manifest_items),
        "applied_count": 0,
        "blocked_count": len(manifest_items),
        "items": [
            {
                "work_id": item["work_id"],
                "output_path": item["output_path"],
                "status": "blocked",
                "blocked_reason": blocked_reason,
                "next_action": next_action,
                "agent_notice": agent_output_ignored_notice(next_action),
                "note_path": str(by_work_id.get(item["work_id"], {}).get("note_path", "")),
                "content_hash": str(by_work_id.get(item["work_id"], {}).get("content_hash", "")),
            }
            for item in manifest_items
        ],
        "error_context": {
            "phase": "vocabulary_curation",
            "blocked_reason": blocked_reason,
            "root_cause": str(prompt_eval.get("root_cause") or blocked_reason),
            "affected_artifact": str(prompt_eval.get("path") or "curator_prompt_eval"),
            "error_summary": "curator prompt evaluation did not pass or was inconsistent",
            "suggested_fix": next_action,
            "next_action": next_action,
            "retry_scope": "curator_prompt_eval_then_apply",
        },
    })


def _work_id_from_semantic_output_path(path: Path) -> str:
    return path.name.removesuffix(".semantic-ingestion.json")


def _ignored_output_notices(
    *,
    work_items: list[Any],
    manifest_items: list[dict[str, str]],
) -> list[dict[str, str]]:
    manifest_paths = {
        Path(item["output_path"]).expanduser().resolve(strict=False)
        for item in manifest_items
        if str(item.get("output_path") or "")
    }
    output_dirs = {
        Path(str(item.output_path)).expanduser().parent
        for item in work_items
        if str(getattr(item, "output_path", "") or "")
    }
    notices: list[dict[str, str]] = []
    for output_dir in sorted(output_dirs):
        if not output_dir.is_dir():
            continue
        for output_path in sorted(output_dir.glob("*.semantic-ingestion.json")):
            normalized_output = output_path.expanduser().resolve(strict=False)
            if normalized_output in manifest_paths:
                continue
            notice = CuratorIgnoredOutputNotice(
                work_id=_work_id_from_semantic_output_path(output_path),
                output_path=str(output_path),
                reason="not_in_manifest",
                next_action="Recriar o manifest pela rota oficial se este output deve ser aplicado.",
            )
            notices.append(notice.model_dump(mode="json"))
    return notices


def collect_curator_outputs(
    *,
    plan: JsonObject,
    manifest_path: Path,
    include_missing: bool = False,
) -> JsonObject:
    typed_plan = _validate_curator_batch_plan(plan)
    items: list[CuratorManifestItem] = []
    missing_outputs: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_item in typed_plan.work_items:
        work_id = raw_item.work_id
        output_path = raw_item.output_path
        if work_id in seen:
            raise ValidationError(f"duplicate work_id in curator batch plan: {work_id}")
        seen.add(work_id)
        path = Path(output_path)
        if path.is_file():
            items.append(CuratorManifestItem(work_id=work_id, output_path=str(path), sha256=_sha256_bytes(path.read_bytes())))
        elif include_missing:
            missing_outputs.append({"work_id": work_id, "output_path": str(path)})
        else:
            missing_outputs.append({"work_id": work_id, "output_path": str(path)})

    manifest = CuratorManifest(
        schema=VOCABULARY_CURATOR_BATCH_OUTPUT_MANIFEST_SCHEMA,
        batch_id=typed_plan.batch_id,
        items=items,
    )
    manifest_payload = manifest.model_dump(mode="json", by_alias=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)
    return {
        "schema": "medical-notes-workbench.vocabulary-curator-output-collection.v1",
        "phase": "vocabulary_curation",
        "status": "completed_with_missing" if missing_outputs else "completed",
        "manifest_path": str(manifest_path),
        "batch_id": typed_plan.batch_id,
        "planned_count": typed_plan.item_count,
        "included_count": len(items),
        "missing_count": len(missing_outputs),
        "missing_outputs": missing_outputs,
        "include_missing": bool(include_missing),
    }


def apply_curator_batch_outputs(
    *,
    plan: JsonObject,
    manifest_path: Path,
    prompt_eval_path: Path | None = None,
    skip_prompt_eval: bool = False,
    skip_prompt_eval_reason: str = "",
) -> JsonObject:
    typed_plan = _validate_curator_batch_plan(plan)
    db_path = Path(typed_plan.db_path)

    by_work_id: dict[str, JsonObject] = {}
    for item in typed_plan.work_items:
        work_id = item.work_id
        if work_id in by_work_id:
            raise ValidationError(f"duplicate work_id in curator batch plan: {work_id}")
        by_work_id[work_id] = item.model_dump(mode="json", by_alias=True)

    manifest = _read_json_object(manifest_path, label="curator batch output manifest")
    manifest_items = _manifest_items(manifest)
    ignored_notices = _ignored_output_notices(work_items=typed_plan.work_items, manifest_items=manifest_items)
    prompt_eval = _prompt_eval_summary(
        plan=plan,
        manifest=manifest,
        prompt_eval_path=prompt_eval_path,
        skip_prompt_eval=skip_prompt_eval,
        skip_prompt_eval_reason=skip_prompt_eval_reason,
    )
    if prompt_eval.get("status") != "pass" and prompt_eval.get("status") != "skipped":
        return _blocked_by_prompt_eval_receipt(
            plan=plan,
            db_path=db_path,
            by_work_id=by_work_id,
            manifest_items=manifest_items,
            prompt_eval=prompt_eval,
        )
    agent_events = [prompt_eval["agent_event"]] if isinstance(prompt_eval.get("agent_event"), dict) else []
    if agent_events:
        prompt_eval = {key: value for key, value in prompt_eval.items() if key != "agent_event"}
    seen: set[str] = set()
    receipts: list[JsonObject] = []
    applied_count = 0
    blocked_count = 0
    for manifest_item in manifest_items:
        work_id = manifest_item["work_id"]
        if work_id in seen:
            raise ValidationError(f"duplicate work_id in curator batch manifest: {work_id}")
        seen.add(work_id)
        if work_id not in by_work_id:
            raise ValidationError(f"unknown work_id in curator batch manifest: {work_id}")
        output_path = Path(manifest_item["output_path"])
        try:
            expected_output_hash = str(manifest_item.get("sha256") or "")
            if not expected_output_hash:
                raise ValidationError(
                    "curator_output_manifest.missing_sha256: manifest item must be produced by collect-curator-outputs "
                    "before eval/apply so output changes can be detected."
                )
            try:
                output_bytes = output_path.read_bytes()
            except OSError as exc:
                raise ValidationError(f"curator_output_unreadable: {output_path}: {exc}") from exc
            actual_output_hash = _sha256_bytes(output_bytes)
            if actual_output_hash != expected_output_hash:
                raise ValidationError(
                    "curator_output_hash_mismatch: output changed after collect-curator-outputs; "
                    "regenerate manifest and curator prompt eval before applying"
                )
            if output_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
                raise ValidationError(
                    f"artifact_encoding.unsupported_utf16: curator batch output {output_path} is UTF-16; "
                    "regenerate it with med-link-graph-curator or write UTF-8 without BOM."
                )
            try:
                output_text = output_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    f"artifact_encoding.invalid_utf8: curator batch output {output_path} must be UTF-8: {exc}"
                ) from exc
            try:
                output_payload = json.loads(output_text)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"curator batch output is invalid JSON: {output_path}: {exc}") from exc
            if not isinstance(output_payload, dict):
                raise ValidationError(f"curator batch output must be a JSON object: {output_path}")
            output_payload = JsonObjectAdapter.validate_python(output_payload)
            typed_output = _validate_note_semantic_ingestion_output(output_payload)
            receipt = JsonObjectAdapter.validate_python(
                apply_semantic_ingestion(
                    db_path=db_path,
                    item=typed_output.model_dump(mode="json", by_alias=True, exclude_none=True),
                    require_contract=True,
                )
            )
        except ValidationError as exc:
            error_text = str(exc)
            blocked_reason = (
                "subagent_output_contract.invalid"
                if error_text.startswith("subagent_output_contract.invalid")
                else "curator_output_hash_mismatch"
                if error_text.startswith("curator_output_hash_mismatch")
                else "curator_output_manifest.missing_sha256"
                if error_text.startswith("curator_output_manifest.missing_sha256")
                else "semantic_ingestion.validation_error"
            )
            next_action = (
                "Regenerar o output com med-link-graph-curator direto a partir do work_item oficial; não use @generalist nem output sem workflow/phase/source_workflow."
                if blocked_reason == "subagent_output_contract.invalid"
                else "Regenerar o manifest com collect-curator-outputs, rodar eval-curator-batch novamente e repetir apply-curator-batch."
                if blocked_reason in {"curator_output_hash_mismatch", "curator_output_manifest.missing_sha256"}
                else "Corrigir o output note-semantic-ingestion.v1 e repetir apply-curator-batch após eval-curator-batch passar."
            )
            agent_event = None
            if blocked_reason == "curator_output_hash_mismatch":
                agent_event = curator_agent_event(
                    code="agent.curator_output_changed_after_collection",
                    root_cause_code=blocked_reason,
                    next_action=next_action,
                    artifact_path=str(output_path),
                    reason="output_hash_changed_after_collect",
                )
            elif blocked_reason == "semantic_ingestion.validation_error" and "aliases[" in error_text and "surface" in error_text:
                agent_event = curator_agent_event(
                    code="agent.curator_alias_surface_without_text",
                    root_cause_code=blocked_reason,
                    next_action=next_action,
                    artifact_path=str(output_path),
                    reason="aliases_surface_without_text",
                )
            receipt: JsonObject = {
                "schema": "medical-notes-workbench.note-semantic-ingestion-apply-receipt.v1",
                "status": "blocked",
                "blocked_reason": blocked_reason,
                "error": error_text,
                "next_action": next_action,
                "agent_notice": agent_output_ignored_notice(next_action),
                "note_path": str(by_work_id[work_id].get("note_path", "")),
                "content_hash": str(by_work_id[work_id].get("content_hash", "")),
                "error_context": {
                    "phase": "vocabulary_curation",
                    "blocked_reason": blocked_reason,
                    "root_cause": blocked_reason,
                    "affected_artifact": str(output_path),
                    "error_summary": error_text,
                    "suggested_fix": next_action,
                    "next_action": next_action,
                    "retry_scope": "single_curator_work_item",
                },
            }
            if agent_event is not None:
                receipt["agent_event"] = agent_event
        status = _json_text_field(receipt, "status", "blocked")
        if status == "applied":
            applied_count += 1
        else:
            blocked_count += 1
            receipt = JsonObjectAdapter.validate_python(dict(receipt))
            receipt_next_action = _json_text_field(
                receipt,
                "next_action",
                "Resolver o item bloqueado e repetir apply-curator-batch.",
            )
            receipt["agent_notice"] = agent_output_ignored_notice(receipt_next_action)
        receipt_item: JsonObject = {
            "work_id": work_id,
            "output_path": str(output_path),
            "status": status,
            "blocked_reason": _json_text_field(receipt, "blocked_reason"),
            "note_path": _json_text_field(
                receipt,
                "note_path",
                str(by_work_id[work_id].get("note_path", "")),
            ),
            "content_hash": (
                _json_text_field(receipt, "content_hash")
                or _json_text_field(receipt, "expected_hash")
                or str(by_work_id[work_id].get("content_hash", ""))
            ),
            "receipt": receipt,
        }
        if status != "applied":
            receipt_item["agent_notice"] = _json_text_field(receipt, "agent_notice", agent_output_ignored_notice())
            agent_event_payload = _json_object_field(receipt, "agent_event")
            if agent_event_payload:
                agent_events.append(agent_event_payload)
        receipts.append(
            receipt_item
        )

    result: JsonObject = {
        "schema": VOCABULARY_CURATOR_BATCH_RECEIPT_SCHEMA,
        "phase": "vocabulary_curation",
        "status": "completed" if blocked_count == 0 else "completed_with_blockers",
        "batch_id": typed_plan.batch_id,
        "db_path": str(db_path),
        "prompt_eval": prompt_eval,
        "plan_item_count": len(by_work_id),
        "manifest_item_count": len(manifest_items),
        "applied_count": applied_count,
        "blocked_count": blocked_count,
        "agent_events": agent_events,
        "agent_output_ignored_notices": ignored_notices,
        "items": receipts,
    }
    first_blocked = next((item for item in receipts if item.get("status") != "applied"), None)
    if first_blocked is not None:
        first_receipt = first_blocked.get("receipt") if isinstance(first_blocked.get("receipt"), dict) else {}
        next_action = str(first_receipt.get("next_action") or "Resolver os itens bloqueados e repetir apply-curator-batch.")
        blocked_reason = str(first_blocked.get("blocked_reason") or "")
        result["next_action"] = next_action
        result["agent_notice"] = str(first_blocked.get("agent_notice") or agent_output_ignored_notice(next_action))
        result["required_inputs"] = ["blocked_curator_work_items"]
        result["human_decision_required"] = False
        result["error_context"] = {
            "phase": "vocabulary_curation",
            "blocked_reason": blocked_reason,
            "root_cause": blocked_reason,
            "affected_artifact": str(first_blocked.get("output_path") or ""),
            "error_summary": str(first_receipt.get("error") or first_receipt.get("blocked_reason") or ""),
            "suggested_fix": next_action,
            "next_action": next_action,
            "retry_scope": "blocked_curator_work_items",
        }
    return _finalize_curator_apply_receipt(result)
