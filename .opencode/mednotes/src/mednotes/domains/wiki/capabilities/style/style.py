"""Wiki_Medicina style validation and deterministic fixes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ConfigDict
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes import note_style
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text, read_note_meta
from mednotes.domains.wiki.capabilities.specialist.plan_attestation import (
    subagent_plan_attestation_blocked_reason,
    subagent_plan_hash,
    validate_subagent_plan_attestation,
)
from mednotes.domains.wiki.capabilities.specialist.specialist_receipts import (
    validate_specialist_task_run_receipt_attestation,
)
from mednotes.domains.wiki.capabilities.specialist.specialist_runtime import (
    specialist_dev_escape_enabled,
    transcript_command_untrusted_gemini_binary,
)
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import is_index_note_content, is_index_target
from mednotes.domains.wiki.common import FileWriteError, MissingPathError, ValidationError
from mednotes.domains.wiki.contracts.agents import SubagentBatchPlan
from mednotes.domains.wiki.contracts.specialist import SpecialistTaskRunReceipt
from mednotes.domains.wiki.contracts.style_rewrite import (
    FixWikiStyleResult,
    StyleRewriteApplyReceipt,
    StyleRewriteAtomicApplyResult,
    StyleRewriteManifest,
    StyleRewriteOutputAttestation,
    StyleRewriteOutputCollection,
    StyleRewriteOutputFinalization,
    StyleRewriteOutputReceipt,
)
from mednotes.domains.wiki.contracts.workflow_guardrails import error_context
from mednotes.domains.wiki.performance import cooperative_cpu_yield
from mednotes.kernel.base import ContractModel, JsonObject, JsonValue, contract_error

STYLE_REWRITE_MANIFEST_SCHEMA = "medical-notes-workbench.style-rewrite-output-manifest.v1"
STYLE_REWRITE_APPLY_RECEIPT_SCHEMA = "medical-notes-workbench.style-rewrite-apply-receipt.v1"
STYLE_REWRITE_OUTPUT_RECEIPT_SCHEMA = "medical-notes-workbench.style-rewrite-output.v1"
STYLE_REWRITE_OUTPUT_ATTESTATION_SCHEMA = "medical-notes-workbench.style-rewrite-output-attestation.v1"
STYLE_REWRITE_OUTPUT_FINALIZATION_SCHEMA = "medical-notes-workbench.style-rewrite-output-finalization.v1"
STYLE_REWRITE_ATOMIC_APPLY_RESULT_SCHEMA = "medical-notes-workbench.style-rewrite-atomic-apply-result.v1"
STYLE_REWRITE_ATTESTATION_KIND = "workbench_hmac_sha256.v1"
RELATED_NOTES_HEADING_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
H2_HEADING_RE = re.compile(r"(?m)^##\s+")
FOOTER_RULE_RE = re.compile(r"(?m)^---\s*$")


class _StyleRewriteWorkItemLens(ContractModel):
    """Typed view over one style-rewrite work item from the attested plan."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    work_id: str = ""
    target_path: str = ""
    target_hash_before: str = ""
    temp_output: str = ""
    output_path: str = ""
    output_receipt_path: str = ""
    output_attestation_path: str = ""
    agent: str = ""
    model_policy: str = ""
    required_model_tier: str = ""

    @property
    def planned_output_path(self) -> str:
        return self.temp_output.strip() or self.output_path.strip()

    @property
    def agent_or_default(self) -> str:
        return self.agent.strip() or "med-knowledge-architect"

    @property
    def model_policy_or_default(self) -> str:
        return self.model_policy.strip() or "medical_specialist_authoring.v1"

    @property
    def required_model_tier_or_default(self) -> str:
        return self.required_model_tier.strip() or "specialist"


def _style_rewrite_work_item_lens(raw_item: JsonObject) -> _StyleRewriteWorkItemLens:
    """Normalize raw plan JSON once before style-rewrite logic reads fields."""

    return _StyleRewriteWorkItemLens.model_validate(raw_item)


def _optional_path_text(path: Path | None) -> str:
    if path is None:
        return ""
    return str(path)


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return _sha256_bytes(encoded)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise MissingPathError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return payload


def _json_field(source: JsonObject, key: str, default: JsonValue = None) -> JsonValue:
    return source.get(key, default)


def _validate_style_rewrite_manifest(payload: dict[str, Any]) -> StyleRewriteManifest:
    try:
        return StyleRewriteManifest.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style_rewrite_manifest_invalid") from exc


def _validate_style_rewrite_output_receipt(payload: JsonObject) -> StyleRewriteOutputReceipt:
    try:
        return StyleRewriteOutputReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style_rewrite_output_receipt_invalid") from exc


def _validate_style_rewrite_output_attestation(payload: dict[str, Any]) -> StyleRewriteOutputAttestation:
    try:
        return StyleRewriteOutputAttestation.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style_rewrite_output_attestation_invalid") from exc


def _validate_style_rewrite_plan(payload: dict[str, Any]) -> SubagentBatchPlan:
    try:
        plan = SubagentBatchPlan.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style_rewrite_plan_contract_invalid") from exc
    if plan.phase != "style-rewrite":
        raise ValidationError("style_rewrite_plan_contract_invalid: phase must be style-rewrite")
    return plan


def _verify_style_rewrite_plan_attestation(payload: dict[str, Any]) -> str:
    return validate_subagent_plan_attestation(payload)


def _plan_attestation_next_action(blocked_reason: str) -> str:
    if blocked_reason == "subagent_plan_attestation_required":
        return (
            "Regere o plano pela rota oficial plan-subagents; plano JSON copiado, escrito ou editado pelo agente "
            "não pode ser usado para finalizar, coletar ou aplicar outputs."
        )
    return "Regere o plano pela rota oficial plan-subagents; a assinatura/hash do plano não confere."


def _style_rewrite_attestation_key_path() -> Path:
    configured = os.getenv("MEDNOTES_STYLE_REWRITE_ATTESTATION_KEY_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".gemini" / "medical-notes-workbench" / "style-rewrite-attestation.key"


def _style_rewrite_attestation_key(*, create: bool) -> bytes:
    configured = os.getenv("MEDNOTES_STYLE_REWRITE_ATTESTATION_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")
    key_path = _style_rewrite_attestation_key_path()
    if key_path.exists():
        return key_path.read_bytes().strip()
    if not create:
        raise MissingPathError(f"style rewrite attestation key not found: {key_path}")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode("ascii")
    tmp_path = key_path.with_name(f"{key_path.name}.tmp")
    tmp_path.write_bytes(key + b"\n")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, key_path)
    return key


def _style_rewrite_attestation_signing_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _style_rewrite_attestation_signature(payload: dict[str, Any], *, create_key: bool) -> str:
    digest = hmac.new(
        _style_rewrite_attestation_key(create=create_key),
        _style_rewrite_attestation_signing_payload(payload),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _style_rewrite_verify_attestation_signature(payload: dict[str, Any]) -> bool:
    try:
        expected = _style_rewrite_attestation_signature(payload, create_key=False)
    except MissingPathError:
        return False
    return hmac.compare_digest(str(payload.get("signature") or ""), expected)


def style_rewrite_agent_notice(next_action: str = "") -> str:
    action = next_action.strip() or "repita pela rota oficial antes de aplicar."
    return (
        "Output de style-rewrite ignorado para proteger a Wiki. "
        f"Não remende Markdown, manifest ou recibo manualmente; {action}"
    )


def style_rewrite_agent_event(
    *,
    code: str,
    root_cause_code: str,
    next_action: str,
    artifact_path: str = "",
) -> dict[str, object]:
    return {
        "schema": "medical-notes-workbench.agent-event.v1",
        "code": code,
        "severity": "high",
        "root_cause_code": root_cause_code,
        "summary": "Style rewrite apply blocked by typed workflow guardrail.",
        "action": next_action,
        "next_action": next_action,
        "artifact_path": artifact_path,
    }


def _finalize_style_rewrite_apply_receipt(payload: JsonObject) -> JsonObject:
    try:
        receipt = StyleRewriteApplyReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style rewrite apply receipt invalid") from exc
    return receipt.model_dump(mode="json", by_alias=True, exclude_none=True)


def finalize_style_rewrite_apply_receipt(payload: JsonObject) -> JsonObject:
    return _finalize_style_rewrite_apply_receipt(payload)


def _finalize_style_rewrite_output_finalization(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        finalization = StyleRewriteOutputFinalization.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style rewrite output finalization invalid") from exc
    return finalization.model_dump(mode="json", by_alias=True, exclude_none=True)


def _finalize_style_rewrite_output_collection(payload: JsonObject) -> JsonObject:
    try:
        collection = StyleRewriteOutputCollection.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style rewrite output collection invalid") from exc
    return collection.model_dump(mode="json", by_alias=True, exclude_none=True)


def _finalize_style_rewrite_atomic_apply_result(payload: JsonObject) -> JsonObject:
    try:
        result = StyleRewriteAtomicApplyResult.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style rewrite atomic apply result invalid") from exc
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def finalize_style_rewrite_atomic_apply_result(payload: JsonObject) -> JsonObject:
    return _finalize_style_rewrite_atomic_apply_result(payload)


def _style_rewrite_blocked_receipt(
    *,
    blocked_reason: str,
    next_action: str,
    plan_path: Path | None = None,
    output_manifest_path: Path | None = None,
    work_id: str = "unknown",
    target_path: Path | None = None,
    output_path: Path | None = None,
    source_plan_hash: str = "",
    manifest_hash: str = "",
    agent_event_code: str = "",
    required_inputs: list[str] | None = None,
) -> JsonObject:
    agent_events = []
    artifact_path = (
        _optional_path_text(output_manifest_path)
        or _optional_path_text(output_path)
        or _optional_path_text(plan_path)
    )
    if agent_event_code:
        agent_events.append(
            style_rewrite_agent_event(
                code=agent_event_code,
                root_cause_code=blocked_reason,
                next_action=next_action,
                artifact_path=artifact_path,
            )
        )
    return _finalize_style_rewrite_apply_receipt(
        {
            "schema": STYLE_REWRITE_APPLY_RECEIPT_SCHEMA,
            "phase": "style_rewrite",
            "status": "blocked",
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "agent_notice": style_rewrite_agent_notice(next_action),
            "required_inputs": required_inputs or ["plan", "manifest", "work_id"],
            "human_decision_required": False,
            "plan_path": _optional_path_text(plan_path),
            "output_manifest_path": _optional_path_text(output_manifest_path),
            "source_plan_hash": source_plan_hash,
            "manifest_hash": manifest_hash,
            "agent_events": agent_events,
            "items": [
                {
                    "work_id": work_id,
                    "target_path": _optional_path_text(target_path),
                    "output_path": _optional_path_text(output_path),
                    "status": "blocked",
                    "blocked_reason": blocked_reason,
                    "next_action": next_action,
                    "agent_notice": style_rewrite_agent_notice(next_action),
                }
            ],
            "error_context": error_context(
                phase="style_rewrite",
                blocked_reason=blocked_reason,
                root_cause=blocked_reason,
                affected_artifact=artifact_path or "style_rewrite_apply",
                error_summary="Style rewrite apply provenance could not be verified.",
                suggested_fix=next_action,
                next_action=next_action,
                retry_scope="collect_style_rewrite_outputs_then_apply",
            ),
        }
    )


def _style_report_error_message(report: dict[str, Any]) -> str:
    messages = [str(item.get("message", item.get("code", ""))) for item in report.get("errors", [])]
    return "Generated Wiki note does not match the Wiki_Medicina style contract: " + "; ".join(messages)


def _style_report_rewrite_message(report: dict[str, Any]) -> str:
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    codes = [str(item.get("code", "")) for item in warnings if isinstance(item, dict) and item.get("code")]
    joined_codes = ", ".join(codes) if codes else "style_rewrite_required"
    return (
        "requires_llm_rewrite: Generated Wiki note needs med-knowledge-architect rewrite before publication; "
        f"style issues: {joined_codes}."
    )


def validate_wiki_note_contract(content: str, *, title: str, raw_file: Path) -> dict[str, Any]:
    """Reject generated Wiki_Medicina notes that drift from the house style."""

    report = note_style.validate_note_style(
        content,
        title=title,
        raw_meta=read_note_meta(raw_file),
        path=str(raw_file),
    )
    if report["errors"]:
        raise ValidationError(_style_report_error_message(report))
    if report.get("requires_llm_rewrite"):
        raise ValidationError(_style_report_rewrite_message(report))
    return report


def _require_existing_file(path: Path, *, label: str) -> None:
    """Normalize filesystem probe failures at CLI validation boundaries."""

    try:
        exists = path.exists()
    except OSError as exc:
        raise MissingPathError(f"{label} path is invalid or too long: {path}") from exc
    if not exists:
        raise MissingPathError(f"{label} file not found: {path}")


def validate_note_style_file(content_path: Path, title: str, raw_file: Path | None = None) -> dict[str, Any]:
    _require_existing_file(content_path, label="Content")
    if raw_file is not None:
        _require_existing_file(raw_file, label="Raw")
    raw_meta = note_style.raw_meta_from_file(raw_file) if raw_file is not None else {}
    return note_style.validate_note_style(
        content_path.read_text(encoding="utf-8"),
        title=title,
        raw_meta=raw_meta,
        path=str(content_path),
    )


def fix_note_style_file(
    content_path: Path,
    title: str,
    output_path: Path,
    raw_file: Path | None = None,
) -> JsonObject:
    _require_existing_file(content_path, label="Content")
    if raw_file is not None:
        _require_existing_file(raw_file, label="Raw")
    raw_meta = note_style.raw_meta_from_file(raw_file) if raw_file is not None else {}
    fixed_content, report = note_style.fix_note_style(
        content_path.read_text(encoding="utf-8"),
        title=title,
        raw_meta=raw_meta,
        path=str(content_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, fixed_content)
    report["output_path"] = str(output_path)
    report["wrote_output"] = True
    return report


def validate_wiki_style(wiki_dir: Path) -> dict[str, Any]:
    if not wiki_dir.exists():
        raise MissingPathError(f"Wiki dir not found: {wiki_dir}")
    if not wiki_dir.is_dir():
        raise ValidationError(f"Wiki dir is not a directory: {wiki_dir}")
    audit = note_style.validate_wiki_dir(wiki_dir)
    reports = [
        _downgrade_invalid_root_note_report(report, wiki_dir=wiki_dir, path=Path(str(report.get("path") or "")))
        if isinstance(report, dict) and report.get("path")
        else report
        for report in audit.get("reports", [])
    ]
    return {
        **audit,
        "ok_count": sum(1 for item in reports if item["ok"]),
        "error_count": sum(1 for item in reports if item["errors"]),
        "warning_count": sum(1 for item in reports if item["warnings"]),
        "reports": reports,
    }


def _is_loose_root_note(wiki_dir: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(wiki_dir)
    except ValueError:
        return False
    return len(rel.parts) == 1 and not is_index_target(path.stem)


def _downgrade_invalid_root_note_report(report: dict[str, Any], *, wiki_dir: Path, path: Path) -> dict[str, Any]:
    if not _is_loose_root_note(wiki_dir, path) or not report.get("errors"):
        return report
    warning = {
        "code": "root_note.invalid_content",
        "message": "root-level Markdown is not a valid Wiki note; leaving it as a warning until content is repaired or removed",
        "severity": "warning",
        "source": "fix_wiki_style",
    }
    return {
        **report,
        "ok": True,
        "errors": [],
        "warnings": [*report.get("warnings", []), warning],
        "requires_llm_rewrite": False,
        "rewrite_prompt": None,
        "root_note_invalid": True,
        "root_note_invalid_original_errors": report.get("errors", []),
    }


def fix_wiki_style_result(wiki_dir: Path, apply: bool = False, backup: bool = False) -> FixWikiStyleResult:
    """Return the typed deterministic style preview/apply result."""

    backup = False
    if not wiki_dir.exists():
        raise MissingPathError(f"Wiki dir not found: {wiki_dir}")
    if not wiki_dir.is_dir():
        raise ValidationError(f"Wiki dir is not a directory: {wiki_dir}")
    files = iter_notes(wiki_dir)
    reports: list[dict[str, Any]] = []
    changed_count = 0
    written_count = 0
    backup_paths: list[str] = []
    write_errors: list[dict[str, Any]] = []
    for index, path in enumerate(files, start=1):
        cooperative_cpu_yield(index)
        original = path.read_text(encoding="utf-8")
        title = note_style.infer_title(original, path)
        if is_index_target(path.stem) or is_index_note_content(original):
            report = note_style.index_style_report(original, title=title, path=str(path))
            report["changed"] = False
            report["would_write"] = False
            report["wrote"] = False
            report["backup"] = None
            report["write_error"] = None
            reports.append(report)
            continue
        fixed, report = note_style.fix_note_style(original, title=title, path=str(path))
        if _is_loose_root_note(wiki_dir, path) and report.get("errors"):
            fixed = original
            report = _downgrade_invalid_root_note_report(report, wiki_dir=wiki_dir, path=path)
        changed = fixed != original
        report["changed"] = changed
        report["would_write"] = changed
        report["wrote"] = False
        report["backup"] = None
        report["write_error"] = None
        if changed:
            changed_count += 1
        if apply and changed:
            try:
                atomic_write_text(path, fixed)
            except (FileWriteError, OSError) as exc:
                report["backup"] = None
                report["write_error"] = str(exc)
                write_errors.append(
                    {
                        "path": str(path),
                        "backup": report["backup"],
                        "operation": "fix_wiki_style",
                        "error": str(exc),
                    }
                )
            else:
                report["wrote"] = True
                report["backup"] = None
                written_count += 1
        reports.append(report)
    return FixWikiStyleResult.model_validate(
        {
            "schema": note_style.STYLE_FIX_SCHEMA,
            "wiki_dir": str(wiki_dir),
            "dry_run": not apply,
            "apply": apply,
            "backup": backup,
            "file_count": len(files),
            "changed_count": changed_count,
            "written_count": written_count,
            "error_count": sum(1 for item in reports if item["errors"]),
            "warning_count": sum(1 for item in reports if item["warnings"]),
            "write_error_count": len(write_errors),
            "write_errors": write_errors,
            "backup_paths": backup_paths,
            "reports": reports,
        }
    )


def fix_wiki_style(wiki_dir: Path, apply: bool = False, backup: bool = False) -> JsonObject:
    """Serialize the style result for CLI/adapter edges; domain callers use the model."""

    return fix_wiki_style_result(wiki_dir, apply=apply, backup=backup).to_payload()


def _requires_style_rewrite(audit: dict[str, Any]) -> bool:
    return any(report.get("requires_llm_rewrite") for report in audit.get("reports", []))


def _managed_related_notes_span(content: str) -> tuple[int, int] | None:
    heading = RELATED_NOTES_HEADING_RE.search(content)
    if heading is None:
        return None
    end_candidates = [
        match.start()
        for match in (
            H2_HEADING_RE.search(content, heading.end()),
            FOOTER_RULE_RE.search(content, heading.end()),
        )
        if match is not None
    ]
    end = min(end_candidates) if end_candidates else len(content)
    return heading.start(), end


def _canonical_managed_related_notes_section(original_content: str) -> str:
    span = _managed_related_notes_span(original_content)
    if span is None:
        return "## 🔗 Notas Relacionadas\n\n"
    return original_content[span[0] : span[1]].rstrip() + "\n\n"


def _preserve_managed_related_notes_section(
    *,
    original_content: str,
    rewritten_content: str,
) -> tuple[str, bool]:
    replacement = _canonical_managed_related_notes_section(original_content)
    span = _managed_related_notes_span(rewritten_content)
    if span is None:
        updated = rewritten_content.rstrip() + "\n\n" + replacement
    else:
        updated = rewritten_content[: span[0]].rstrip() + "\n\n" + replacement + rewritten_content[span[1] :].lstrip("\n")
    if not updated.endswith("\n"):
        updated += "\n"
    return updated, updated != rewritten_content


def _prepare_style_rewrite_content(
    *,
    target_path: Path,
    original_content: str,
    rewritten_content: str,
) -> tuple[str, list[str]]:
    title = note_style.infer_title(rewritten_content, target_path)
    fixed, report = note_style.fix_note_style(rewritten_content, title=title, path=str(target_path))
    fixes = [str(item) for item in report.get("fixes_applied", []) if str(item).strip()]
    fixed, related_notes_changed = _preserve_managed_related_notes_section(
        original_content=original_content,
        rewritten_content=fixed,
    )
    if related_notes_changed:
        fixes.append("preserve_managed_related_notes_section")
    title = note_style.infer_title(fixed, target_path)
    fixed, report = note_style.fix_note_style(fixed, title=title, path=str(target_path))
    fixes.extend(str(item) for item in report.get("fixes_applied", []) if str(item).strip())
    deduped_fixes: list[str] = []
    for fix in fixes:
        if fix not in deduped_fixes:
            deduped_fixes.append(fix)
    return fixed, deduped_fixes


def _normalize_style_rewrite_output_file(*, target_path: Path, output_path: Path) -> list[str]:
    original = target_path.read_text(encoding="utf-8")
    rewritten = output_path.read_text(encoding="utf-8")
    fixed, fixes = _prepare_style_rewrite_content(
        target_path=target_path,
        original_content=original,
        rewritten_content=rewritten,
    )
    if fixed != rewritten:
        atomic_write_text(output_path, fixed)
    return fixes


def apply_style_rewrite(
    target_path: Path,
    content_path: Path,
    *,
    dry_run: bool = False,
    backup: bool = False,
    rewritten_content: str | None = None,
) -> JsonObject:
    backup = False
    if not target_path.exists():
        raise MissingPathError(f"Target note not found: {target_path}")
    if not content_path.exists():
        raise MissingPathError(f"Rewritten content file not found: {content_path}")
    original = target_path.read_text(encoding="utf-8")
    rewritten = content_path.read_text(encoding="utf-8") if rewritten_content is None else rewritten_content
    rewritten, deterministic_fixes = _prepare_style_rewrite_content(
        target_path=target_path,
        original_content=original,
        rewritten_content=rewritten,
    )
    title = note_style.infer_title(rewritten, target_path)
    original_title = note_style.infer_title(original, target_path)
    if original_title != target_path.stem and title != original_title:
        raise ValidationError(f"Rewritten note title changed from {original_title!r} to {title!r}")
    report = note_style.validate_note_style(rewritten, title=title, path=str(target_path))
    result: dict[str, Any] = {
        "target_path": str(target_path),
        "content_path": str(content_path),
        "title": title,
        "dry_run": dry_run,
        "backup": backup,
        "backup_path": None,
        "changed": rewritten != original,
        "written": False,
        "validation": report,
        "deterministic_fixes_applied": deterministic_fixes,
    }
    if report["errors"] or report.get("requires_llm_rewrite"):
        return result
    if not dry_run and rewritten != original:
        atomic_write_text(target_path, rewritten)
        result["written"] = True
        result["backup_path"] = None
    return result


def style_rewrite_manifest_required_receipt(
    *,
    target_path: Path | None = None,
    content_path: Path | None = None,
) -> JsonObject:
    return _style_rewrite_blocked_receipt(
        blocked_reason="style_rewrite_manifest_required",
        next_action=(
            "Coletar outputs de style-rewrite pela rota oficial e aplicar com plan, manifest e work_id. "
            "Não aplique Markdown solto."
        ),
        target_path=target_path,
        output_path=content_path,
        agent_event_code="agent.style_rewrite_manifest_required",
    )


def _style_rewrite_output_collection_blocked(
    *,
    blocked_reason: str,
    next_action: str,
    plan_path: Path,
    manifest_path: Path,
    source_plan_hash: str,
    missing_outputs: list[dict[str, str]] | None = None,
    missing_output_receipts: list[dict[str, str]] | None = None,
    invalid_output_receipts: list[dict[str, str]] | None = None,
    missing_output_attestations: list[dict[str, str]] | None = None,
    invalid_output_attestations: list[dict[str, str]] | None = None,
    required_inputs: list[str] | None = None,
) -> JsonObject:
    missing_outputs = missing_outputs or []
    missing_output_receipts = missing_output_receipts or []
    invalid_output_receipts = invalid_output_receipts or []
    missing_output_attestations = missing_output_attestations or []
    invalid_output_attestations = invalid_output_attestations or []
    affected = (
        missing_output_attestations[0].get("output_attestation_path", "")
        if missing_output_attestations
        else invalid_output_attestations[0].get("output_attestation_path", "")
        if invalid_output_attestations
        else missing_output_receipts[0].get("output_receipt_path", "")
        if missing_output_receipts
        else invalid_output_receipts[0].get("output_receipt_path", "")
        if invalid_output_receipts
        else missing_outputs[0].get("output_path", "")
        if missing_outputs
        else str(manifest_path)
    )
    required_inputs = required_inputs or ["style_rewrite_output_attestation"]
    if missing_output_receipts or invalid_output_receipts:
        required_inputs.append("style_rewrite_output_receipt")
    return _finalize_style_rewrite_output_collection(
        {
            "schema": "medical-notes-workbench.style-rewrite-output-collection.v1",
            "phase": "style_rewrite",
            "status": "blocked",
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "required_inputs": required_inputs,
            "human_decision_required": False,
            "plan_path": str(plan_path),
            "manifest_path": str(manifest_path),
            "source_plan_hash": source_plan_hash,
            "missing_output_count": len(missing_outputs),
            "missing_outputs": missing_outputs,
            "missing_output_attestation_count": len(missing_output_attestations),
            "missing_output_attestations": missing_output_attestations,
            "invalid_output_attestation_count": len(invalid_output_attestations),
            "invalid_output_attestations": invalid_output_attestations,
            "missing_output_receipt_count": len(missing_output_receipts),
            "missing_output_receipts": missing_output_receipts,
            "invalid_output_receipt_count": len(invalid_output_receipts),
            "invalid_output_receipts": invalid_output_receipts,
            "agent_notice": style_rewrite_agent_notice(next_action),
            "agent_events": [
                style_rewrite_agent_event(
                    code=f"agent.{blocked_reason}",
                    root_cause_code=blocked_reason,
                    next_action=next_action,
                    artifact_path=affected,
                )
            ],
            "error_context": error_context(
                phase="style_rewrite",
                blocked_reason=blocked_reason,
                root_cause=blocked_reason,
                affected_artifact=affected or str(manifest_path),
                error_summary="Style rewrite output was not proven by a Workbench-signed attestation.",
                suggested_fix=next_action,
                next_action=next_action,
                retry_scope="single_style_rewrite_work_item",
            ),
        }
    )


def _style_rewrite_output_receipt_path(raw_item: JsonObject, output_path: Path) -> Path:
    explicit = _style_rewrite_work_item_lens(raw_item).output_receipt_path.strip()
    return Path(explicit) if explicit else output_path.with_suffix(output_path.suffix + ".receipt.json")


def _style_rewrite_output_attestation_path(raw_item: JsonObject, output_path: Path) -> Path:
    explicit = _style_rewrite_work_item_lens(raw_item).output_attestation_path.strip()
    return Path(explicit) if explicit else output_path.with_suffix(output_path.suffix + ".attestation.json")


def _style_rewrite_model_policy(raw_item: JsonObject) -> str:
    return _style_rewrite_work_item_lens(raw_item).model_policy_or_default


def _style_rewrite_specialist_model_blocked(raw_item: JsonObject, *, actual_model: str) -> bool:
    required_tier = _style_rewrite_work_item_lens(raw_item).required_model_tier.strip().lower()
    if required_tier != "specialist":
        return False
    normalized_model = actual_model.strip().lower()
    if not normalized_model:
        return True
    if normalized_model in {"unknown", "runtime_model_or_unknown", "not_reported", "auto"}:
        return True
    return any(token in normalized_model for token in ("flash", "lite", "nano"))


def _style_rewrite_specialist_model_provenance_unverified(
    raw_item: JsonObject,
    *,
    model_verification_status: str,
) -> bool:
    required_tier = _style_rewrite_work_item_lens(raw_item).required_model_tier.strip().lower()
    if required_tier != "specialist":
        return False
    if model_verification_status == "verified_by_workbench":
        return False
    return not _style_rewrite_allow_unverified_specialist_model()


def _style_rewrite_allow_unverified_specialist_model() -> bool:
    enabled = os.getenv("MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL", "").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        return False
    return bool(os.getenv("MEDNOTES_ALLOW_UNVERIFIED_SPECIALIST_MODEL_REASON", "").strip())


def _style_rewrite_model_provenance_next_action() -> str:
    return (
        "Pare esta tentativa. Refaça este item pela rota oficial de autoria especializada, "
        "com recibo/proveniência do modelo validado pelo Workbench; não aceite modelo Pro "
        "apenas declarado pelo parent e não use escape de desenvolvedor."
    )


def _style_rewrite_receipt_matches_work_item(
    receipt: StyleRewriteOutputReceipt,
    *,
    raw_item: JsonObject,
    work_id: str,
    target_path: Path,
    output_path: Path,
    target_hash_before: str,
    actual_output_hash: str,
) -> str:
    if receipt.work_id != work_id:
        return "work_id"
    if receipt.target_path != str(target_path):
        return "target_path"
    if receipt.target_hash_before != target_hash_before:
        return "target_hash_before"
    if receipt.output_path != str(output_path):
        return "output_path"
    if receipt.output_sha256 != actual_output_hash:
        return "output_sha256"
    item = _style_rewrite_work_item_lens(raw_item)
    if receipt.agent != item.agent:
        return "agent"
    if receipt.model_policy != _style_rewrite_model_policy(raw_item):
        return "model_policy"
    if receipt.required_model_tier != item.required_model_tier:
        return "required_model_tier"
    return ""


def _style_rewrite_attestation_matches_work_item(
    attestation: StyleRewriteOutputAttestation,
    *,
    raw_item: JsonObject,
    source_plan_hash: str,
    work_id: str,
    target_path: Path,
    output_path: Path,
    target_hash_before: str,
    actual_output_hash: str,
) -> str:
    if attestation.attestation_kind != STYLE_REWRITE_ATTESTATION_KIND:
        return "attestation_kind"
    if attestation.work_id != work_id:
        return "work_id"
    if attestation.source_plan_hash != source_plan_hash:
        return "source_plan_hash"
    if attestation.target_path != str(target_path):
        return "target_path"
    if attestation.target_hash_before != target_hash_before:
        return "target_hash_before"
    if attestation.output_path != str(output_path):
        return "output_path"
    if attestation.output_sha256 != actual_output_hash:
        return "output_sha256"
    item = _style_rewrite_work_item_lens(raw_item)
    if attestation.agent != item.agent:
        return "agent"
    if attestation.model_policy != _style_rewrite_model_policy(raw_item):
        return "model_policy"
    if attestation.required_model_tier != item.required_model_tier:
        return "required_model_tier"
    return ""


def build_style_rewrite_output_attestation(
    *,
    raw_item: JsonObject,
    source_plan_hash: str,
    output_path: Path,
    actual_model: str = "",
    provider: str = "",
    model_claim_source: str | None = None,
    model_verification_status: str | None = None,
) -> dict[str, Any]:
    item = _style_rewrite_work_item_lens(raw_item)
    target_path = Path(item.target_path)
    output_hash = _sha256_bytes(output_path.read_bytes())
    claim_source = model_claim_source or ("parent_cli_argument_unverified" if actual_model or provider else "not_reported")
    verification_status = model_verification_status or "unverified_by_workbench"
    payload: JsonObject = {
        "schema": STYLE_REWRITE_OUTPUT_ATTESTATION_SCHEMA,
        "phase": "style_rewrite",
        "status": "completed",
        "attestation_kind": STYLE_REWRITE_ATTESTATION_KIND,
        "work_id": item.work_id,
        "source_plan_hash": source_plan_hash,
        "target_path": str(target_path),
        "target_hash_before": item.target_hash_before,
        "output_path": str(output_path),
        "output_sha256": output_hash,
        "agent": item.agent_or_default,
        "model_policy": _style_rewrite_model_policy(raw_item),
        "required_model_tier": item.required_model_tier_or_default,
        "actual_model": actual_model,
        "provider": provider,
        "model_claim_source": claim_source,
        "model_verification_status": verification_status,
        "nonce": secrets.token_hex(16),
        "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    payload["signature"] = _style_rewrite_attestation_signature(payload, create_key=True)
    attestation = _validate_style_rewrite_output_attestation(payload)
    return attestation.model_dump(mode="json", by_alias=True)


def write_style_rewrite_output_attestation(
    *,
    raw_item: JsonObject,
    source_plan_hash: str,
    output_path: Path,
    attestation_path: Path | None = None,
    actual_model: str = "",
    provider: str = "",
    model_claim_source: str | None = None,
    model_verification_status: str | None = None,
) -> dict[str, Any]:
    attestation = build_style_rewrite_output_attestation(
        raw_item=raw_item,
        source_plan_hash=source_plan_hash,
        output_path=output_path,
        actual_model=actual_model,
        provider=provider,
        model_claim_source=model_claim_source,
        model_verification_status=model_verification_status,
    )
    path = attestation_path or _style_rewrite_output_attestation_path(raw_item, output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(attestation, ensure_ascii=False, indent=2) + "\n")
    return attestation


def write_style_rewrite_output_receipt(
    *,
    raw_item: JsonObject,
    output_path: Path,
    receipt_path: Path | None = None,
    actual_model: str = "",
    provider: str = "",
) -> JsonObject:
    item = _style_rewrite_work_item_lens(raw_item)
    receipt = StyleRewriteOutputReceipt(
        schema=STYLE_REWRITE_OUTPUT_RECEIPT_SCHEMA,
        phase="style_rewrite",
        status="completed",
        work_id=item.work_id,
        target_path=item.target_path,
        target_hash_before=item.target_hash_before,
        output_path=str(output_path),
        output_sha256=_sha256_bytes(output_path.read_bytes()),
        agent="med-knowledge-architect",
        model_policy=_style_rewrite_model_policy(raw_item),
        required_model_tier=item.required_model_tier_or_default,
        actual_model=actual_model,
        provider=provider,
    ).to_payload()
    path = receipt_path or _style_rewrite_output_receipt_path(raw_item, output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def _style_rewrite_output_finalization_blocked(
    *,
    blocked_reason: str,
    next_action: str,
    plan_path: Path,
    work_id: str,
    target_path: Path | None = None,
    output_path: Path | None = None,
    source_plan_hash: str = "",
    validation: JsonObject | None = None,
    required_inputs: list[str] | None = None,
) -> JsonObject:
    artifact_path = _optional_path_text(output_path) or _optional_path_text(target_path) or str(plan_path)
    return _finalize_style_rewrite_output_finalization({
        "schema": STYLE_REWRITE_OUTPUT_FINALIZATION_SCHEMA,
        "phase": "style_rewrite",
        "status": "blocked",
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "required_inputs": required_inputs or ["plan", "work_id", "temp_output"],
        "human_decision_required": False,
        "plan_path": str(plan_path),
        "work_id": work_id,
        "target_path": _optional_path_text(target_path),
        "output_path": _optional_path_text(output_path),
        "source_plan_hash": source_plan_hash,
        "validation": validation or {},
        "agent_notice": style_rewrite_agent_notice(next_action),
        "agent_events": [
            style_rewrite_agent_event(
                code=f"agent.{blocked_reason}",
                root_cause_code=blocked_reason,
                next_action=next_action,
                artifact_path=artifact_path,
            )
        ],
        "error_context": error_context(
            phase="style_rewrite",
            blocked_reason=blocked_reason,
            root_cause=blocked_reason,
            affected_artifact=artifact_path,
            error_summary="Style rewrite output could not be finalized by the Workbench attestation boundary.",
            suggested_fix=next_action,
            next_action=next_action,
            retry_scope="single_style_rewrite_work_item",
        ),
    })


def _specialist_receipt_for_style_rewrite(
    path: Path | None,
    *,
    work_id: str,
    output_path: Path,
) -> SpecialistTaskRunReceipt | None:
    if path is None:
        return None
    raw = _read_json_object(path, label="specialist task run receipt")
    try:
        validate_specialist_task_run_receipt_attestation(raw)
        receipt = SpecialistTaskRunReceipt.from_operation_payload(raw)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task run receipt invalid") from exc
    except ValidationError as exc:
        raise ValidationError(f"specialist task run receipt invalid: {exc}") from exc
    if receipt.status.value != "completed":
        raise ValidationError("specialist task run receipt status must be completed")
    if receipt.phase != "style_rewrite":
        raise ValidationError("specialist task run receipt phase must be style_rewrite")
    if receipt.work_id != work_id:
        raise ValidationError("specialist task run receipt work_id does not match style rewrite work_id")
    if Path(receipt.output_path) != output_path:
        raise ValidationError("specialist task run receipt output_path does not match style rewrite output")
    actual_output_hash = _sha256_bytes(output_path.read_bytes()) if output_path.exists() else ""
    if receipt.output_sha256 != actual_output_hash:
        raise ValidationError("specialist task run receipt output_sha256 does not match current output")
    runtime_error = _specialist_receipt_runtime_provenance_error(receipt)
    if runtime_error:
        raise ValidationError(runtime_error)
    return receipt


def _specialist_receipt_runtime_provenance_error(receipt: SpecialistTaskRunReceipt) -> str:
    if "mock" in receipt.specialist_session_id.casefold():
        return "specialist task run receipt specialist_session_id appears to be mock data"
    transcript_path = Path(receipt.transcript_artifact_path)
    try:
        transcript = _read_json_object(transcript_path, label="specialist task run transcript")
    except (MissingPathError, ValidationError) as exc:
        return f"specialist task run transcript invalid: {exc}"
    transcript_text = json.dumps(transcript, ensure_ascii=False, sort_keys=True).casefold()
    if "mock-session-id" in transcript_text or "gemini_mock" in transcript_text:
        return "specialist task run transcript contains mock runtime evidence"
    if receipt.harness.value == "gemini_cli":
        untrusted_binary = transcript_command_untrusted_gemini_binary(transcript.get("command"))
        if untrusted_binary and not specialist_dev_escape_enabled():
            return f"specialist task run transcript used untrusted gemini binary: {untrusted_binary}"
    return ""


def finalize_style_rewrite_output(
    *,
    plan_path: Path,
    work_id: str,
    actual_model: str = "",
    provider: str = "",
    specialist_run_receipt_path: Path | None = None,
) -> dict[str, Any]:
    try:
        plan_payload = _read_json_object(plan_path, label="style rewrite plan")
    except MissingPathError:
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_plan_missing",
            next_action=(
                "Regerar o plano de style-rewrite pela rota oficial de /mednotes:fix-wiki "
                "antes de finalizar outputs."
            ),
            plan_path=plan_path,
            work_id=work_id,
            required_inputs=["plan"],
        )
    _validate_style_rewrite_plan(plan_payload)
    try:
        source_plan_hash = _verify_style_rewrite_plan_attestation(plan_payload)
    except ValidationError as exc:
        blocked_reason = subagent_plan_attestation_blocked_reason(exc)
        return _style_rewrite_output_finalization_blocked(
            blocked_reason=blocked_reason,
            next_action=_plan_attestation_next_action(blocked_reason),
            plan_path=plan_path,
            work_id=work_id,
            source_plan_hash=subagent_plan_hash(plan_payload),
            required_inputs=["subagent_plan_attestation"],
        )
    work_item = _style_rewrite_work_item(plan_payload, work_id)
    if work_item is None:
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regerar o plano de style-rewrite; o work_id solicitado não está coberto.",
            plan_path=plan_path,
            work_id=work_id,
            source_plan_hash=source_plan_hash,
        )
    target_path = Path(str(work_item.get("target_path") or ""))
    output_path = Path(str(work_item.get("temp_output") or work_item.get("output_path") or ""))
    target_hash_before = str(work_item.get("target_hash_before") or "")
    if not output_path.exists():
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_output_missing",
            next_action="Relançar a autoria médica especializada para este work_item; o temp_output oficial não existe.",
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
        )
    try:
        specialist_receipt = _specialist_receipt_for_style_rewrite(
            specialist_run_receipt_path,
            work_id=work_id,
            output_path=output_path,
        )
    except (MissingPathError, ValidationError) as exc:
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="specialist_task_run_receipt_invalid",
            next_action="Refaça a chamada ao especialista pela rota oficial e finalize usando o recibo Workbench válido.",
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=specialist_run_receipt_path or output_path,
            source_plan_hash=source_plan_hash,
            validation={"specialist_task_run_receipt_error": str(exc)},
            required_inputs=["specialist_task_run_receipt"],
        )
    model_claim_source: str | None = None
    model_verification_status: str | None = None
    if specialist_receipt is not None:
        actual_model = specialist_receipt.observed_model
        provider = specialist_receipt.model_evidence.observed_provider_id if specialist_receipt.model_evidence else provider
        model_claim_source = "specialist_task_run_receipt"
        model_verification_status = "verified_by_workbench"
    if _style_rewrite_specialist_model_blocked(work_item, actual_model=actual_model):
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_specialist_model_required",
            next_action=(
                "Refaça este item pela rota oficial de autoria especializada e finalize com "
                "--specialist-run-receipt apontando para o recibo validado pelo Workbench; "
                "Flash, modelo desconhecido ou modelo declarado manualmente não pode finalizar autoria médica especializada."
            ),
            plan_path=plan_path,
            work_id=work_id,
            source_plan_hash=source_plan_hash,
            required_inputs=["specialist_model"],
        )
    actual_target_hash = _sha256_bytes(target_path.read_bytes()) if target_path.exists() else ""
    if actual_target_hash != target_hash_before:
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_stale_target_hash",
            next_action="Replanejar style-rewrite; a nota alvo mudou desde o plano.",
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
        )
    if _style_rewrite_specialist_model_provenance_unverified(
        work_item,
        model_verification_status=model_verification_status or "unverified_by_workbench",
    ):
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_model_provenance_unverified",
            next_action=_style_rewrite_model_provenance_next_action(),
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            required_inputs=["specialist_model_provenance"],
        )
    deterministic_fixes = _normalize_style_rewrite_output_file(target_path=target_path, output_path=output_path)
    validation = apply_style_rewrite(target_path, output_path, dry_run=True)
    validation["deterministic_fixes_applied"] = deterministic_fixes
    if validation["validation"]["errors"]:
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_agent_contract_violation",
            next_action="Regenerar o rewrite pela rota de autoria médica especializada para este work_item.",
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            validation=validation,
        )
    if validation["validation"].get("requires_llm_rewrite"):
        return _style_rewrite_output_finalization_blocked(
            blocked_reason="style_rewrite_still_requires_rewrite",
            next_action=(
                "Regenerar o rewrite pela rota de autoria médica especializada até "
                "validation.requires_llm_rewrite=false; não finalize output que ainda pede reescrita."
            ),
            plan_path=plan_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            validation=validation,
        )
    output_receipt_path = _style_rewrite_output_receipt_path(work_item, output_path)
    output_receipt = write_style_rewrite_output_receipt(
        raw_item=work_item,
        output_path=output_path,
        receipt_path=output_receipt_path,
        actual_model=actual_model,
        provider=provider,
    )
    attestation_path = _style_rewrite_output_attestation_path(work_item, output_path)
    attestation = write_style_rewrite_output_attestation(
        raw_item=work_item,
        source_plan_hash=source_plan_hash,
        output_path=output_path,
        attestation_path=attestation_path,
        actual_model=actual_model,
        provider=provider,
        model_claim_source=model_claim_source,
        model_verification_status=model_verification_status,
    )
    return _finalize_style_rewrite_output_finalization({
        "schema": STYLE_REWRITE_OUTPUT_FINALIZATION_SCHEMA,
        "phase": "style_rewrite",
        "status": "completed",
        "blocked_reason": "",
        "next_action": "",
        "required_inputs": [],
        "human_decision_required": False,
        "plan_path": str(plan_path),
        "work_id": work_id,
        "target_path": str(target_path),
        "output_path": str(output_path),
        "output_sha256": _sha256_bytes(output_path.read_bytes()),
        "output_receipt_path": str(output_receipt_path),
        "output_receipt_sha256": _sha256_bytes(output_receipt_path.read_bytes()),
        "output_attestation_path": str(attestation_path),
        "output_attestation_sha256": _sha256_bytes(attestation_path.read_bytes()),
        "source_plan_hash": source_plan_hash,
        "actual_model": actual_model,
        "provider": provider,
        "model_claim_source": attestation.get("model_claim_source", "not_reported"),
        "model_verification_status": attestation.get("model_verification_status", "unverified_by_workbench"),
        "validation": validation,
        "receipt": output_receipt,
        "attestation": attestation,
    })


def collect_style_rewrite_outputs(plan_path: Path, manifest_path: Path, *, work_id: str = "") -> JsonObject:
    plan_payload = _read_json_object(plan_path, label="style rewrite plan")
    _validate_style_rewrite_plan(plan_payload)
    try:
        source_plan_hash = _verify_style_rewrite_plan_attestation(plan_payload)
    except ValidationError as exc:
        blocked_reason = subagent_plan_attestation_blocked_reason(exc)
        return _style_rewrite_output_collection_blocked(
            blocked_reason=blocked_reason,
            next_action=_plan_attestation_next_action(blocked_reason),
            plan_path=plan_path,
            manifest_path=manifest_path,
            source_plan_hash=subagent_plan_hash(plan_payload),
            required_inputs=["subagent_plan_attestation"],
        )
    manifest_items: list[dict[str, str]] = []
    missing_outputs: list[dict[str, str]] = []
    missing_output_receipts: list[dict[str, str]] = []
    invalid_output_receipts: list[dict[str, str]] = []
    missing_output_attestations: list[dict[str, str]] = []
    invalid_output_attestations: list[dict[str, str]] = []
    requested_work_id = work_id.strip()
    raw_work_items = plan_payload.get("work_items", [])
    if requested_work_id:
        raw_work_items = [
            item
            for item in raw_work_items
            if isinstance(item, dict) and str(item.get("work_id") or "") == requested_work_id
        ]
        if not raw_work_items:
            return _style_rewrite_output_collection_blocked(
                blocked_reason="style_rewrite_plan_contract_invalid",
                next_action="Regerar o plano de style-rewrite; o work_id solicitado não está coberto.",
                plan_path=plan_path,
                manifest_path=manifest_path,
                source_plan_hash=source_plan_hash,
            )
    for raw_item in raw_work_items:
        if not isinstance(raw_item, dict):
            continue
        item = _style_rewrite_work_item_lens(raw_item)
        work_id = item.work_id.strip()
        target_path = Path(item.target_path)
        output_path = Path(item.planned_output_path)
        target_hash_before = item.target_hash_before.strip()
        if not work_id or not target_path or not output_path:
            continue
        if not output_path.exists():
            missing_outputs.append({"work_id": work_id, "output_path": str(output_path)})
            continue
        actual_output_hash = _sha256_bytes(output_path.read_bytes())
        output_attestation_path = _style_rewrite_output_attestation_path(raw_item, output_path)
        if not output_attestation_path.exists():
            missing_output_attestations.append(
                {
                    "work_id": work_id,
                    "output_path": str(output_path),
                    "output_attestation_path": str(output_attestation_path),
                }
            )
            continue
        try:
            raw_attestation = _read_json_object(output_attestation_path, label="style rewrite output attestation")
            output_attestation = _validate_style_rewrite_output_attestation(raw_attestation)
            mismatch = (
                ""
                if _style_rewrite_verify_attestation_signature(raw_attestation)
                else "signature"
            )
            if not mismatch:
                mismatch = _style_rewrite_attestation_matches_work_item(
                    output_attestation,
                    raw_item=raw_item,
                    source_plan_hash=source_plan_hash,
                    work_id=work_id,
                    target_path=target_path,
                    output_path=output_path,
                    target_hash_before=target_hash_before,
                    actual_output_hash=actual_output_hash,
                )
            if not mismatch and _style_rewrite_specialist_model_provenance_unverified(
                raw_item,
                model_verification_status=output_attestation.model_verification_status,
            ):
                mismatch = (
                    "model_verification_status unverified_by_workbench is not accepted for specialist "
                    "model output without official runtime provenance"
                )
        except (MissingPathError, ValidationError) as exc:
            invalid_output_attestations.append(
                {
                    "work_id": work_id,
                    "output_path": str(output_path),
                    "output_attestation_path": str(output_attestation_path),
                    "error": str(exc),
                }
            )
            continue
        if mismatch:
            invalid_output_attestations.append(
                {
                    "work_id": work_id,
                    "output_path": str(output_path),
                    "output_attestation_path": str(output_attestation_path),
                    "error": f"style rewrite output attestation does not match work item: {mismatch}",
                }
            )
            continue
        output_receipt_path = _style_rewrite_output_receipt_path(raw_item, output_path)
        output_receipt_sha256 = ""
        if output_receipt_path.exists():
            try:
                output_receipt = _validate_style_rewrite_output_receipt(
                    _read_json_object(output_receipt_path, label="style rewrite output receipt")
                )
                receipt_mismatch = _style_rewrite_receipt_matches_work_item(
                    output_receipt,
                    raw_item=raw_item,
                    work_id=work_id,
                    target_path=target_path,
                    output_path=output_path,
                    target_hash_before=target_hash_before,
                    actual_output_hash=actual_output_hash,
                )
            except (MissingPathError, ValidationError) as exc:
                invalid_output_receipts.append(
                    {
                        "work_id": work_id,
                        "output_path": str(output_path),
                        "output_receipt_path": str(output_receipt_path),
                        "error": str(exc),
                    }
                )
                continue
            if receipt_mismatch:
                invalid_output_receipts.append(
                    {
                        "work_id": work_id,
                        "output_path": str(output_path),
                        "output_receipt_path": str(output_receipt_path),
                        "error": f"style rewrite output receipt does not match work item: {receipt_mismatch}",
                    }
                )
                continue
            output_receipt_sha256 = _sha256_bytes(output_receipt_path.read_bytes())
        elif item.output_receipt_path.strip():
            missing_output_receipts.append(
                {
                    "work_id": work_id,
                    "output_path": str(output_path),
                    "output_receipt_path": str(output_receipt_path),
                }
            )
            continue
        manifest_items.append(
            {
                "work_id": work_id,
                "target_path": str(target_path),
                "target_hash_before": target_hash_before,
                "output_path": str(output_path),
                "sha256": actual_output_hash,
                "output_attestation_path": str(output_attestation_path),
                "output_attestation_sha256": _sha256_bytes(output_attestation_path.read_bytes()),
                "output_receipt_path": str(output_receipt_path) if output_receipt_sha256 else "",
                "output_receipt_sha256": output_receipt_sha256,
                "agent": item.agent_or_default,
                "model_policy": _style_rewrite_model_policy(raw_item),
                "required_model_tier": item.required_model_tier_or_default,
            }
        )
    if (
        missing_outputs
        or missing_output_attestations
        or invalid_output_attestations
        or missing_output_receipts
        or invalid_output_receipts
    ):
        if invalid_output_attestations:
            unverified_model = any(
                "model_verification_status unverified_by_workbench" in item.get("error", "")
                for item in invalid_output_attestations
            )
            if unverified_model:
                blocked_reason = "style_rewrite_model_provenance_unverified"
                next_action = _style_rewrite_model_provenance_next_action()
                required_inputs = ["specialist_model_provenance"]
            else:
                blocked_reason = "style_rewrite_output_attestation_invalid"
                next_action = (
                    "Regenerar o output pela porta oficial de style-rewrite para o work_item atual; "
                    "não assine, copie ou remende Markdown manualmente."
                )
                required_inputs = None
        elif missing_output_attestations:
            blocked_reason = "style_rewrite_output_attestation_required"
            next_action = (
                "Regenerar o output pela porta oficial de style-rewrite para o work_item atual; "
                "o collect só aceita output com style-rewrite-output-attestation.v1 assinada pelo Workbench."
            )
            required_inputs = None
        elif invalid_output_receipts:
            blocked_reason = "style_rewrite_output_receipt_invalid"
            next_action = (
                "Regenerar o output pela porta oficial de style-rewrite para o work_item atual; "
                "não remende recibo legado ou Markdown manualmente."
            )
            required_inputs = None
        elif missing_output_receipts:
            blocked_reason = "style_rewrite_output_receipt_required"
            next_action = (
                "Regenerar o output pela porta oficial de style-rewrite para o work_item atual; "
                "o recibo legado declarado no plano não existe."
            )
            required_inputs = None
        else:
            blocked_reason = "style_rewrite_output_missing"
            next_action = (
                "Regenerar o output pela porta oficial de style-rewrite para o work_item atual e repetir "
                "collect-style-rewrite-outputs depois que o temp_output existir."
            )
            required_inputs = None
        return _style_rewrite_output_collection_blocked(
            blocked_reason=blocked_reason,
            next_action=next_action,
            plan_path=plan_path,
            manifest_path=manifest_path,
            source_plan_hash=source_plan_hash,
            missing_outputs=missing_outputs,
            missing_output_receipts=missing_output_receipts,
            invalid_output_receipts=invalid_output_receipts,
            missing_output_attestations=missing_output_attestations,
            invalid_output_attestations=invalid_output_attestations,
            required_inputs=required_inputs,
        )
    manifest = _validate_style_rewrite_manifest(
        {
            "schema": STYLE_REWRITE_MANIFEST_SCHEMA,
            "source_plan_hash": source_plan_hash,
            "batch_id": str(plan_payload.get("batch_id") or ""),
            "items": manifest_items,
        }
    )
    manifest_payload = manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(manifest_path, json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n")
    return _finalize_style_rewrite_output_collection(
        {
            "schema": "medical-notes-workbench.style-rewrite-output-collection.v1",
            "phase": "style_rewrite",
            "status": "completed",
            "blocked_reason": "",
            "next_action": "",
            "required_inputs": [],
            "human_decision_required": False,
            "plan_path": str(plan_path),
            "manifest_path": str(manifest_path),
            "source_plan_hash": source_plan_hash,
            "manifest_hash": manifest.fingerprint(),
            "item_count": len(manifest.items),
            "items": manifest_payload["items"],
        }
    )


def _style_rewrite_work_item(plan_payload: dict[str, Any], work_id: str) -> dict[str, Any] | None:
    for item in plan_payload.get("work_items", []):
        if isinstance(item, dict) and str(item.get("work_id") or "") == work_id:
            return item
    return None


def apply_style_rewrite_from_manifest(
    *,
    plan_path: Path,
    outputs_path: Path,
    work_id: str,
    dry_run: bool = False,
    backup: bool = False,
) -> dict[str, Any]:
    plan_payload = _read_json_object(plan_path, label="style rewrite plan")
    _validate_style_rewrite_plan(plan_payload)
    manifest_payload = _read_json_object(outputs_path, label="style rewrite manifest")
    manifest = _validate_style_rewrite_manifest(manifest_payload)
    try:
        source_plan_hash = _verify_style_rewrite_plan_attestation(plan_payload)
    except ValidationError as exc:
        blocked_reason = subagent_plan_attestation_blocked_reason(exc)
        manifest_hash = manifest.fingerprint()
        return _style_rewrite_blocked_receipt(
            blocked_reason=blocked_reason,
            next_action=_plan_attestation_next_action(blocked_reason),
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            source_plan_hash=subagent_plan_hash(plan_payload),
            manifest_hash=manifest_hash,
            agent_event_code=f"agent.{blocked_reason}",
            required_inputs=["subagent_plan_attestation"],
        )
    manifest_hash = manifest.fingerprint()
    if manifest.source_plan_hash != source_plan_hash:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_manifest_invalid",
            next_action="Recriar o manifest de style-rewrite a partir do plano atual antes de aplicar.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_manifest_plan_mismatch",
        )
    work_item = _style_rewrite_work_item(plan_payload, work_id)
    manifest_item = next((item for item in manifest.items if item.work_id == work_id), None)
    if work_item is None or manifest_item is None:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_manifest_invalid",
            next_action="Recriar plano e manifest de style-rewrite; o work_id solicitado não está coberto.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_manifest_missing_work_id",
        )
    target_path = Path(str(work_item.get("target_path") or manifest_item.target_path))
    output_path = Path(str(manifest_item.output_path))
    expected_target_hash = str(work_item.get("target_hash_before") or "")
    if (
        str(manifest_item.target_path) != str(target_path)
        or str(manifest_item.target_hash_before) != expected_target_hash
        or str(manifest_item.required_model_tier) != str(work_item.get("required_model_tier") or "")
        or str(manifest_item.model_policy) != _style_rewrite_model_policy(work_item)
        or str(work_item.get("agent") or "") != "med-knowledge-architect"
    ):
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regerar o plano de style-rewrite pela rota oficial antes de aplicar.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_plan_contract_invalid",
        )
    actual_output_hash = _sha256_bytes(output_path.read_bytes()) if output_path.exists() else ""
    if actual_output_hash != manifest_item.sha256:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_output_hash_mismatch",
            next_action="Recriar o manifest depois de regenerar o output pela rota de autoria médica especializada.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_output_hash_mismatch",
        )
    attestation_path = Path(str(manifest_item.output_attestation_path))
    actual_output_attestation_hash = _sha256_bytes(attestation_path.read_bytes()) if attestation_path.exists() else ""
    if actual_output_attestation_hash != manifest_item.output_attestation_sha256:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_output_attestation_hash_mismatch",
            next_action="Recriar o manifest depois de regenerar o output pela porta oficial de style-rewrite.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=attestation_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_output_attestation_hash_mismatch",
        )
    output_attestation: StyleRewriteOutputAttestation | None = None
    try:
        raw_attestation = _read_json_object(attestation_path, label="style rewrite output attestation")
        output_attestation = _validate_style_rewrite_output_attestation(raw_attestation)
        attestation_mismatch = (
            ""
            if _style_rewrite_verify_attestation_signature(raw_attestation)
            else "signature"
        )
        if not attestation_mismatch:
            attestation_mismatch = _style_rewrite_attestation_matches_work_item(
                output_attestation,
                raw_item=work_item,
                source_plan_hash=source_plan_hash,
                work_id=work_id,
                target_path=target_path,
                output_path=output_path,
                target_hash_before=expected_target_hash,
                actual_output_hash=actual_output_hash,
            )
    except (MissingPathError, ValidationError) as exc:
        attestation_mismatch = str(exc)
    if attestation_mismatch:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_output_attestation_invalid",
            next_action="Regenerar o output pela porta oficial de style-rewrite e coletar novo manifest.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=attestation_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_output_attestation_invalid",
        )
    if output_attestation is None:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_output_attestation_invalid",
            next_action="Regenerar o output pela porta oficial de style-rewrite e coletar novo manifest.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=attestation_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_output_attestation_invalid",
        )
    if _style_rewrite_specialist_model_provenance_unverified(
        work_item,
        model_verification_status=output_attestation.model_verification_status,
    ):
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_model_provenance_unverified",
            next_action=_style_rewrite_model_provenance_next_action(),
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=attestation_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_model_provenance_unverified",
            required_inputs=["specialist_model_provenance"],
        )
    actual_target_hash = _sha256_bytes(target_path.read_bytes()) if target_path.exists() else ""
    if actual_target_hash != expected_target_hash:
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_stale_target_hash",
            next_action="Replanejar style-rewrite; a nota alvo mudou desde o plano.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_stale_target_hash",
        )
    applied = apply_style_rewrite(target_path, output_path, dry_run=dry_run, backup=backup)
    if applied["validation"]["errors"]:
        return _style_rewrite_blocked_receipt(
            blocked_reason="validation_errors",
            next_action="Regenerar o rewrite pela rota de autoria médica especializada e coletar novo manifest.",
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_validation_errors",
        )
    if applied["validation"].get("requires_llm_rewrite"):
        return _style_rewrite_blocked_receipt(
            blocked_reason="style_rewrite_still_requires_rewrite",
            next_action=(
                "Regenerar o rewrite pela rota de autoria médica especializada até "
                "validation.requires_llm_rewrite=false; não aplique output que ainda pede reescrita."
            ),
            plan_path=plan_path,
            output_manifest_path=outputs_path,
            work_id=work_id,
            target_path=target_path,
            output_path=output_path,
            source_plan_hash=source_plan_hash,
            manifest_hash=manifest_hash,
            agent_event_code="agent.style_rewrite_still_requires_rewrite",
        )
    item_status = "applied" if applied.get("written") else "idempotent"
    return _finalize_style_rewrite_apply_receipt(
        {
            "schema": STYLE_REWRITE_APPLY_RECEIPT_SCHEMA,
            "phase": "style_rewrite",
            "status": "completed",
            "blocked_reason": "",
            "next_action": "",
            "required_inputs": [],
            "human_decision_required": False,
            "plan_path": str(plan_path),
            "output_manifest_path": str(outputs_path),
            "source_plan_hash": source_plan_hash,
            "manifest_hash": manifest_hash,
            "items": [
                {
                    "work_id": work_id,
                    "target_path": str(target_path),
                    "output_path": str(output_path),
                    "status": item_status,
                    "changed": bool(applied.get("changed")),
                    "written": bool(applied.get("written")),
                    "backup_path": applied.get("backup_path"),
                }
            ],
        }
    )


def finalize_collect_apply_style_rewrite(
    *,
    plan_path: Path,
    manifest_path: Path,
    work_id: str,
    specialist_run_receipt_path: Path | None,
    backup: bool = False,
) -> JsonObject:
    if specialist_run_receipt_path is None:
        next_action = (
            "Pare esta tentativa. Refaça a chamada ao especialista pela rota oficial e aplique "
            "usando o recibo Workbench válido; não passe recibo vazio nem tente declarar modelo manualmente."
        )
        return _finalize_style_rewrite_atomic_apply_result(
            {
                "schema": STYLE_REWRITE_ATOMIC_APPLY_RESULT_SCHEMA,
                "phase": "style_rewrite",
                "status": "blocked",
                "blocked_reason": "specialist_task_run_receipt_invalid",
                "next_action": next_action,
                "required_inputs": ["specialist_task_run_receipt"],
                "human_decision_required": False,
                "plan_path": str(plan_path),
                "manifest_path": str(manifest_path),
                "work_id": work_id,
                "specialist_run_receipt_path": "",
                "finalization": None,
                "collection": None,
                "apply": None,
                "agent_notice": style_rewrite_agent_notice(next_action),
                "error_context": error_context(
                    phase="style_rewrite",
                    blocked_reason="specialist_task_run_receipt_invalid",
                    root_cause="specialist_task_run_receipt_invalid",
                    affected_artifact=str(manifest_path),
                    error_summary="specialist_run_receipt_path ausente ou vazio.",
                    suggested_fix=next_action,
                    next_action=next_action,
                    retry_scope="single_style_rewrite_work_item",
                    missing_inputs=["specialist_task_run_receipt"],
                ),
            }
        )
    finalization_payload = finalize_style_rewrite_output(
        plan_path=plan_path,
        work_id=work_id,
        specialist_run_receipt_path=specialist_run_receipt_path,
    )
    finalization = StyleRewriteOutputFinalization.model_validate(finalization_payload)
    if finalization.status == "blocked":
        return _finalize_style_rewrite_atomic_apply_result(
            {
                "schema": STYLE_REWRITE_ATOMIC_APPLY_RESULT_SCHEMA,
                "phase": "style_rewrite",
                "status": "blocked",
                "blocked_reason": finalization.blocked_reason,
                "next_action": finalization.next_action,
                "required_inputs": finalization.required_inputs,
                "human_decision_required": finalization.human_decision_required,
                "plan_path": str(plan_path),
                "manifest_path": str(manifest_path),
                "work_id": work_id,
                "specialist_run_receipt_path": str(specialist_run_receipt_path),
                "finalization": finalization.model_dump(mode="json", by_alias=True, exclude_none=True),
                "collection": None,
                "apply": None,
                "agent_notice": "A etapa atômica parou antes de coletar/aplicar porque a finalização do output falhou.",
                "error_context": finalization.error_context.model_dump(mode="json", by_alias=True)
                if finalization.error_context
                else None,
            }
        )

    collection_payload = collect_style_rewrite_outputs(plan_path, manifest_path, work_id=work_id)
    collection = StyleRewriteOutputCollection.model_validate(collection_payload)
    if collection.status == "blocked":
        return _finalize_style_rewrite_atomic_apply_result(
            {
                "schema": STYLE_REWRITE_ATOMIC_APPLY_RESULT_SCHEMA,
                "phase": "style_rewrite",
                "status": "blocked",
                "blocked_reason": collection.blocked_reason,
                "next_action": collection.next_action,
                "required_inputs": collection.required_inputs,
                "human_decision_required": collection.human_decision_required,
                "plan_path": str(plan_path),
                "manifest_path": str(manifest_path),
                "work_id": work_id,
                "specialist_run_receipt_path": str(specialist_run_receipt_path),
                "finalization": finalization.model_dump(mode="json", by_alias=True, exclude_none=True),
                "collection": collection.model_dump(mode="json", by_alias=True, exclude_none=True),
                "apply": None,
                "agent_notice": "A etapa atômica parou antes de aplicar porque a coleta tipada do output falhou.",
                "error_context": collection.error_context.model_dump(mode="json", by_alias=True)
                if collection.error_context
                else None,
            }
        )

    apply_payload = apply_style_rewrite_from_manifest(
        plan_path=plan_path,
        outputs_path=manifest_path,
        work_id=work_id,
        dry_run=False,
        backup=backup,
    )
    apply_receipt = StyleRewriteApplyReceipt.model_validate(apply_payload)
    return _finalize_style_rewrite_atomic_apply_result(
        {
            "schema": STYLE_REWRITE_ATOMIC_APPLY_RESULT_SCHEMA,
            "phase": "style_rewrite",
            "status": apply_receipt.status,
            "blocked_reason": apply_receipt.blocked_reason,
            "next_action": apply_receipt.next_action,
            "required_inputs": apply_receipt.required_inputs,
            "human_decision_required": apply_receipt.human_decision_required,
            "plan_path": str(plan_path),
            "manifest_path": str(manifest_path),
            "work_id": work_id,
            "specialist_run_receipt_path": str(specialist_run_receipt_path),
            "finalization": finalization.model_dump(mode="json", by_alias=True, exclude_none=True),
            "collection": collection.model_dump(mode="json", by_alias=True, exclude_none=True),
            "apply": apply_receipt.model_dump(mode="json", by_alias=True, exclude_none=True),
            "agent_notice": (
                "Finalização, coleta e aplicação deste item foram executadas como uma etapa atômica "
                "para evitar corrida entre comandos dependentes."
            ),
            "error_context": apply_receipt.error_context.model_dump(mode="json", by_alias=True)
            if apply_receipt.error_context
            else None,
        }
    )
