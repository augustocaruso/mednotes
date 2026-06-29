"""Staging and publishing generated Wiki notes."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import (
    batch_state_from,
    file_sha256,
    merge_batch_state,
    require_compatible_batch_state,
)
from mednotes.domains.wiki.capabilities.graph.coverage import (
    validate_raw_coverage,
    validate_raw_coverage_structure,
)
from mednotes.domains.wiki.capabilities.markdown.markdown_query import (
    MarkdownQueryUnavailable,
    ensure_markdown_query_available,
    markdown_query_blocked_payload,
)
from mednotes.domains.wiki.capabilities.notes.artifacts import validate_artifact_batch, validate_note_artifacts
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import FrontmatterYamlUnavailable
from mednotes.domains.wiki.capabilities.notes.provenance import _apply_note_provenance_from_raw_files
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text, mutate_raw_frontmatter
from mednotes.domains.wiki.capabilities.publish.publish_receipts import build_publish_receipt_payload
from mednotes.domains.wiki.capabilities.style.style import validate_wiki_note_contract
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy import (
    _validate_taxonomy_not_title,
    normalize_taxonomy,
    resolve_target_for_note,
    resolve_taxonomy,
    safe_title,
)
from mednotes.domains.wiki.common import CollisionError, MedOpsError, MissingPathError, ValidationError, _now_iso
from mednotes.domains.wiki.config import MedConfig, _path
from mednotes.domains.wiki.contracts.publish import PublishManifest, PublishManifestBatch
from mednotes.domains.wiki.contracts.raw_coverage import (
    RawCoveragePlanBatch,
    RawCoverageSummary,
    coverage_summary_from_batches,
)
from mednotes.domains.wiki.contracts.workflow_guardrails import (
    PUBLISH_REQUIRED_INPUTS,
    annotate_payload,
    note_target_index,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_machine import (
    ProcessChatsErrorContext,
    ProcessChatsPublishRuntimeObservation,
    ProcessChatsState,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_runtime_result import (
    process_chats_fsm_payload_from_publish_result,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, contract_error


class _ArtifactBatchValidationFields(ContractModel):
    """Typed lens for child artifact validation reports aggregated by publish."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    required: bool = Field(default=False, strict=True)
    manifest_count: int = Field(default=0, ge=0, strict=True)
    artifact_count: int = Field(default=0, ge=0, strict=True)
    covered_artifact_count: int = Field(default=0, ge=0, strict=True)
    missing_artifact_count: int = Field(default=0, ge=0, strict=True)


class _ProcessChatsPublishSafetyFields(ContractModel):
    """Typed view used to decide whether publish mutated the vault."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    created: list[object] = Field(default_factory=list)
    processed_raw_count: int = Field(default=0, ge=0, strict=True)
    manifest_hash: str = ""


class _ProcessChatsRuntimeObservationErrorFields(ContractModel):
    """Typed lens from broad diagnostic context into FSM error context."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    root_cause: str = ""
    blocked_reason: str = ""
    affected_artifact: str = ""
    next_action: str = ""
    suggested_fix: str = ""
    retry_scope: str = ""


def _publish_json_object(value: object, *, prefix: str) -> JsonObject:
    try:
        return JsonObjectAdapter.validate_python(value)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix=prefix) from exc


def resolve_collision(path: Path, mode: str, reserved: set[Path]) -> Path:
    if mode not in {"abort", "suffix"}:
        raise ValidationError(f"Invalid collision mode: {mode}")
    if mode == "abort":
        if path.exists() or path in reserved:
            raise CollisionError(f"Target note already exists: {path}")
        return path

    candidate = path
    idx = 2
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(f"{path.stem} ({idx}){path.suffix}")
        idx += 1
    return candidate


def write_new_note(path: Path, content: str, dry_run: bool = False, create_parent: bool = False) -> None:
    if dry_run:
        return
    if path.exists():
        raise CollisionError(f"Target note already exists: {path}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.exists():
        raise MissingPathError(f"Target taxonomy directory does not exist: {path.parent}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        if path.exists():
            raise CollisionError(f"Target note appeared during write: {path}")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_manifest(path: Path) -> JsonObject:
    if not path.exists():
        raise MissingPathError(f"Manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid manifest JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Manifest must be a JSON object")
    return _publish_json_object(data, prefix="publish manifest")


def _publish_batch_id(manifest: Path) -> str:
    try:
        return file_sha256(manifest)
    except OSError:
        return str(manifest)


def _blocked_publish_contract_receipt(
    *,
    manifest: Path,
    root_cause: str,
    blocked_reason: str | None = None,
    error_summary: str,
    next_action: str,
) -> JsonObject:
    blocked = blocked_reason or root_cause
    error_context = _publish_json_object({
        "phase": "publish_dry_run",
        "blocked_reason": blocked,
        "root_cause": root_cause,
        "affected_artifact": str(manifest),
        "error_summary": error_summary,
        "suggested_fix": next_action,
        "next_action": next_action,
        "retry_scope": "recreate_publish_manifest_then_retry",
    }, prefix="publish blocked receipt")
    return _publish_json_object(annotate_payload(
        {
            "dry_run": True,
            "backup": False,
            "manifest": str(manifest),
            "created": [],
            "raw_updates": [],
            "error_context": error_context,
            "publish_receipt": build_publish_receipt_payload(
                status="blocked",
                batch_id=_publish_batch_id(manifest),
                published_count=0,
                skipped_count=0,
                items=[],
                next_action=next_action,
                error_context=error_context,
            ),
            "runtime_observation": _process_chats_runtime_observation_payload(
                source_state=ProcessChatsState.NOTE_VALIDATION_RUNNING,
                validation_coverage_gap=blocked == "coverage_path_missing",
                validation_manifest_mismatch=blocked != "coverage_path_missing",
                reason_code=blocked,
                next_action=next_action,
                manifest_path=str(manifest),
                error_context=error_context,
            ),
        },
        phase="publish_dry_run",
        status="blocked",
        blocked_reason=blocked,
        next_action=next_action,
        required_inputs=PUBLISH_REQUIRED_INPUTS,
        human_decision_required=False,
    ), prefix="publish blocked receipt")


def _process_chats_runtime_observation_payload(
    *,
    source_state: ProcessChatsState,
    preview_ready: bool = False,
    publish_completed: bool = False,
    link_completed: bool = False,
    link_blocked: bool = False,
    rollback_recorded: bool = False,
    blocked: bool = False,
    quota_wait: bool = False,
    validation_coverage_gap: bool = False,
    validation_manifest_mismatch: bool = False,
    validation_content_invalid: bool = False,
    publish_dry_run_receipt_required: bool = False,
    publish_stale_receipt: bool = False,
    publish_duplicate_target: bool = False,
    publish_provenance_gap: bool = False,
    reason_code: str = "",
    next_action: str = "",
    manifest_path: str = "",
    dry_run_receipt_path: str = "",
    receipt_id: str = "",
    published_count: int = 0,
    link_trigger_context_path: str = "",
    link_receipt_id: str = "",
    link_changed_files: Sequence[str] | None = None,
    error_context: JsonObject | ProcessChatsErrorContext | None = None,
) -> JsonObject:
    """Build the canonical process-chats observation at the producer boundary."""

    typed_error_context = (
        _process_chats_runtime_error_context(error_context, fallback_artifact=manifest_path)
        if isinstance(error_context, dict)
        else error_context
    )
    return ProcessChatsPublishRuntimeObservation(
        source_state=source_state,
        preview_ready=preview_ready,
        publish_completed=publish_completed,
        link_completed=link_completed,
        link_blocked=link_blocked,
        rollback_recorded=rollback_recorded,
        blocked=blocked,
        quota_wait=quota_wait,
        validation_coverage_gap=validation_coverage_gap,
        validation_manifest_mismatch=validation_manifest_mismatch,
        validation_content_invalid=validation_content_invalid,
        publish_dry_run_receipt_required=publish_dry_run_receipt_required,
        publish_stale_receipt=publish_stale_receipt,
        publish_duplicate_target=publish_duplicate_target,
        publish_provenance_gap=publish_provenance_gap,
        reason_code=reason_code,
        next_action=next_action,
        manifest_path=manifest_path,
        dry_run_receipt_path=dry_run_receipt_path,
        receipt_id=receipt_id,
        published_count=published_count,
        link_trigger_context_path=link_trigger_context_path,
        link_receipt_id=link_receipt_id,
        link_changed_files=list(link_changed_files or []),
        error_context=typed_error_context,
    ).to_payload()


def _process_chats_runtime_error_context(
    error_context: JsonObject,
    *,
    fallback_artifact: str,
) -> ProcessChatsErrorContext:
    """Conform broad publish diagnostics to the strict FSM error context."""

    fields = _ProcessChatsRuntimeObservationErrorFields.model_validate(error_context)
    root_cause = fields.root_cause or fields.blocked_reason or "process_chats_blocked"
    next_action = fields.next_action or fields.suggested_fix or "Retomar /mednotes:process-chats pela rota oficial."
    return ProcessChatsErrorContext(
        root_cause=root_cause,
        affected_artifact=fields.affected_artifact or fallback_artifact or "process-chats",
        retry_scope=fields.retry_scope or "process_chats_official_retry",
        next_action=next_action,
    )


def _load_publish_manifest(path: Path) -> PublishManifest:
    try:
        return PublishManifest.model_validate(_load_manifest(path))
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="publish manifest") from exc


def _paths_match(left: str, right: Path) -> bool:
    left_path = _path(left)
    try:
        return left_path.resolve() == right.resolve()
    except OSError:
        return str(left_path) == str(right)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def _note_target_key(path: Path) -> str:
    return normalize_key(path.stem)


def _wiki_note_targets(wiki_dir: Path) -> dict[str, list[Path]]:
    raw_targets = note_target_index(wiki_dir, as_relative=False)
    return {key: [path for path in values if isinstance(path, Path)] for key, values in raw_targets.items()}


def _display_path(path: Path, wiki_dir: Path) -> str:
    try:
        return path.relative_to(wiki_dir).as_posix()
    except ValueError:
        return str(path)


def _validate_normalized_target_available(
    target: Path,
    wiki_dir: Path,
    existing_targets: dict[str, list[Path]],
    reserved_targets: dict[str, Path],
) -> None:
    target_key = _note_target_key(target)
    reserved = reserved_targets.get(target_key)
    if reserved is not None and not _same_path(reserved, target):
        raise CollisionError(
            "Target note would duplicate another note in this publish batch after "
            f"Obsidian target normalization: {_display_path(target, wiki_dir)} conflicts with "
            f"{_display_path(reserved, wiki_dir)}"
        )

    conflicts = [path for path in existing_targets.get(target_key, []) if not _same_path(path, target)]
    if conflicts:
        conflict_list = ", ".join(_display_path(path, wiki_dir) for path in conflicts[:5])
        extra = "" if len(conflicts) <= 5 else f" and {len(conflicts) - 5} more"
        raise CollisionError(
            "Target note would duplicate an existing Obsidian target after accent/case "
            f"normalization: {_display_path(target, wiki_dir)} conflicts with {conflict_list}{extra}. "
            "Use the existing note or merge/rename before publishing."
        )


def _manifest_note_count(manifest: PublishManifest) -> int:
    return sum(len(batch.notes) for batch in manifest.batches)


def _staged_manifest_counts(data: JsonObject, *, pending_note: bool = False) -> tuple[int, int]:
    """Count a manifest being staged before it is valid enough to publish."""
    batches = data["batches"] if "batches" in data else None
    if not isinstance(batches, list):
        raise ValidationError("publish manifest must use canonical batches[]")
    note_count = 1 if pending_note else 0
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValidationError("Each manifest batch must be an object")
        notes = batch["notes"] if "notes" in batch else None
        if not isinstance(notes, list):
            raise ValidationError("manifest batch notes must be a list")
        note_count += len(notes)
    return note_count, len(batches)


def _require_no_pending_human_decision(manifest: PublishManifest, *, label: str) -> None:
    if manifest.pending_human_decision():
        raise ValidationError(
            f"human_decision_required: {label} contains pending human_decision_packet; "
            "resolve the decision, update the manifest/note_plan, and rerun publish-batch --dry-run."
        )


def _raw_files_from_summary(summary: JsonObject | RawCoverageSummary, primary_raw_file: Path) -> list[Path]:
    values = summary.raw_files if isinstance(summary, RawCoverageSummary) else (
        summary["raw_files"] if "raw_files" in summary else None
    )
    raw_files: list[Path] = []
    if isinstance(values, list) and values:
        raw_files = [_path(str(value)) for value in values if str(value).strip()]
    else:
        raw_files = [primary_raw_file]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in raw_files:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            raise MissingPathError(f"Raw file not found: {path}")
        unique.append(path)
    unique = unique or [primary_raw_file]
    if not any(_same_path(path, primary_raw_file) for path in unique):
        raise ValidationError("provenance_gap: raw_files must include the primary raw_file")
    return unique


def _raw_files_from_batch(batch: PublishManifestBatch, primary_raw_file: Path) -> list[Path]:
    if not batch.raw_files:
        return [primary_raw_file]
    return _raw_files_from_summary(
        JsonObjectAdapter.validate_python({"raw_files": batch.raw_files}),
        primary_raw_file,
    )


def _prepare_note_content(
    content: str,
    *,
    title: str,
    raw_files: list[Path],
    coverage_summary: JsonObject | None = None,
) -> str:
    try:
        result = _apply_note_provenance_from_raw_files(
            content,
            raw_files=raw_files,
            title=title,
            coverage_summary=coverage_summary,
        )
    except FrontmatterYamlUnavailable as exc:
        raise ValidationError(f"{exc.blocked_reason}: {exc.next_action}") from exc
    except ValueError as exc:
        raise ValidationError(f"chat_provenance_invalid: {exc}") from exc
    return str(result["text"])


def _artifact_batch_for_raw_files(
    artifact_note_inputs: list[dict[str, str]],
    *,
    raw_files: list[Path],
    artifact_dir: Path | None,
) -> JsonObject:
    if len(raw_files) == 1:
        return validate_artifact_batch(
            artifact_note_inputs,
            raw_file=raw_files[0],
            artifact_dir=artifact_dir,
        )
    reports = [
        validate_artifact_batch(
            artifact_note_inputs,
            raw_file=raw_file,
            artifact_dir=artifact_dir,
        )
        for raw_file in raw_files
    ]
    report_fields = [_ArtifactBatchValidationFields.model_validate(report) for report in reports]
    return JsonObjectAdapter.validate_python({
        "schema": "medical-notes-workbench.artifact-html-validation.multi-source.v1",
        "scope": "multi_source_raw_chat_batch",
        "required": any(report.required for report in report_fields),
        "raw_file_count": len(raw_files),
        "manifest_count": sum(report.manifest_count for report in report_fields),
        "artifact_count": sum(report.artifact_count for report in report_fields),
        "covered_artifact_count": sum(report.covered_artifact_count for report in report_fields),
        "missing_artifact_count": sum(report.missing_artifact_count for report in report_fields),
        "reports": reports,
        "errors": [],
    })


def _batch_for_stage(data: JsonObject, raw_file: Path) -> JsonObject:
    raw_text = str(raw_file)
    batches = data["batches"] if "batches" in data else None
    if not isinstance(batches, list):
        raise ValidationError("publish manifest must use canonical batches[]")
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValidationError("Each manifest batch must be an object")
        if "raw_file" in batch and _paths_match(str(batch["raw_file"]), raw_file):
            notes = batch["notes"] if "notes" in batch else None
            if not isinstance(notes, list):
                raise ValidationError("manifest batch notes must be a list")
            validated = JsonObjectAdapter.validate_python(batch)
            batch.clear()
            batch.update(validated)
            return batch
    new_batch: JsonObject = {"raw_file": raw_text, "notes": []}
    batches.append(new_batch)
    return new_batch


def plan_publish_batch(
    manifest: PublishManifest,
    config: MedConfig,
    collision: str,
    allow_new_taxonomy_leaf: bool = True,
    require_coverage: bool = True,
) -> list[JsonObject]:
    planned_batches: list[JsonObject] = []
    reserved: set[Path] = set()
    reserved_targets: dict[str, Path] = {}
    existing_targets = _wiki_note_targets(config.wiki_dir)
    _require_no_pending_human_decision(manifest, label="manifest")
    for batch in manifest.batches:
        batch_payload = batch.to_payload()
        raw_file = _path(batch.raw_file)
        if not raw_file.exists():
            raise MissingPathError(f"Raw file not found: {raw_file}")
        notes: list[JsonObject] = []
        coverage_path_value = batch.coverage_path
        if require_coverage and not coverage_path_value:
            raise ValidationError(
                "Manifest batch missing coverage_path; create an exhaustive raw coverage inventory "
                "and stage notes with stage-note --coverage <coverage.json>"
            )
        coverage_structure: JsonObject | None = None
        raw_files = _raw_files_from_batch(batch, raw_file)
        if coverage_path_value:
            coverage_path = _path(coverage_path_value)
            coverage_structure = validate_raw_coverage_structure(
                coverage_path,
                raw_file,
                require_triage_note_plan=require_coverage,
            )
            raw_files = _raw_files_from_summary(coverage_structure, raw_file)
        artifact_note_inputs: list[dict[str, str]] = []
        for item in batch.notes:
            content_path = _path(item.content_path)
            if not content_path.exists():
                raise MissingPathError(f"Content file not found: {content_path}")
            content = content_path.read_text(encoding="utf-8")
            prepared_content = _prepare_note_content(
                content,
                title=item.title,
                raw_files=raw_files,
                coverage_summary=coverage_structure,
            )
            validate_wiki_note_contract(prepared_content, title=item.title, raw_file=raw_file)
            artifact_validation = validate_note_artifacts(
                prepared_content,
                raw_file=raw_file,
                artifact_dir=config.artifact_dir,
            )
            artifact_note_inputs.append(
                {
                    "title": item.title,
                    "content_path": str(content_path),
                    "content": prepared_content,
                }
            )
            target, taxonomy_resolution = resolve_target_for_note(
                config.wiki_dir,
                item.taxonomy,
                item.title,
                allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
            )
            target = resolve_collision(target, collision, reserved)
            _validate_normalized_target_available(target, config.wiki_dir, existing_targets, reserved_targets)
            reserved.add(target)
            reserved_targets[_note_target_key(target)] = target
            notes.append(
                {
                    "taxonomy": taxonomy_resolution.taxonomy,
                    "taxonomy_requested": taxonomy_resolution.requested_taxonomy,
                    "taxonomy_canonicalized": list(taxonomy_resolution.canonicalized),
                    "taxonomy_new_dirs": list(taxonomy_resolution.new_dirs),
                    "title": item.title,
                    "content_path": str(content_path),
                    "target_path": str(target),
                    "artifact_validation": artifact_validation,
                }
            )
        planned_batch: JsonObject = {
            "raw_file": str(raw_file),
            "raw_files": [str(path) for path in raw_files],
            "notes": notes,
            "artifact_validation": _artifact_batch_for_raw_files(
                artifact_note_inputs,
                raw_files=raw_files,
                artifact_dir=config.artifact_dir,
            ),
        }
        if coverage_path_value:
            coverage_path = _path(coverage_path_value)
            planned_batch["coverage_path"] = str(coverage_path)
            coverage_summary = validate_raw_coverage(
                coverage_path,
                raw_file,
                [str(note["title"]) for note in notes],
                require_triage_note_plan=require_coverage,
            )
            raw_files = _raw_files_from_summary(coverage_summary, raw_file)
            planned_batch["raw_files"] = [str(path) for path in raw_files]
            require_compatible_batch_state(
                batch_payload,
                coverage_summary,
                left_label="manifest batch",
                right_label="coverage inventory",
            )
            planned_batch["coverage"] = coverage_summary
            coverage_state_basis = {**coverage_summary, **batch_payload}
            if not coverage_state_basis.get("coverage_hash"):
                coverage_state_basis["coverage_hash"] = file_sha256(coverage_path)
            batch_state = batch_state_from(coverage_state_basis)
            if batch_state:
                planned_batch["batch_state"] = batch_state
        planned_batches.append(planned_batch)
    return planned_batches


def taxonomy_new_leaf_authorization_from_plan(plan: Sequence[JsonObject]) -> JsonObject:
    notes: list[JsonObject] = []
    for batch in plan:
        for item in batch.get("notes", []):
            new_dirs = [str(value) for value in item.get("taxonomy_new_dirs", [])]
            if not new_dirs:
                continue
            notes.append(
                {
                    "target_path": str(item["target_path"]),
                    "taxonomy": str(item["taxonomy"]),
                    "taxonomy_requested": str(item.get("taxonomy_requested", "")),
                    "taxonomy_new_dirs": new_dirs,
                }
            )
    return JsonObjectAdapter.validate_python({
        "required": bool(notes),
        "authorized_by_dry_run_receipt": bool(notes),
        "note_count": len(notes),
        "notes": notes,
    })


def _raw_coverage_plan_batches(plan: Sequence[object]) -> list[RawCoveragePlanBatch]:
    """Validate publish's planned-batch projection before deriving coverage truth."""

    try:
        return [RawCoveragePlanBatch.model_validate(batch) for batch in plan]
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="publish.raw_coverage_plan_batch_invalid") from exc


def coverage_summary_from_plan(plan: Sequence[object]) -> JsonObject:
    return coverage_summary_from_batches(_raw_coverage_plan_batches(plan)).to_payload()


def taxonomy_new_leaf_authorization_for_manifest(
    manifest: Path,
    config: MedConfig,
    *,
    collision: str = "abort",
    allow_new_taxonomy_leaf: bool = True,
    require_coverage: bool = True,
) -> JsonObject:
    publish_manifest = _load_publish_manifest(manifest)
    if require_coverage:
        publish_manifest.require_coverage()
    plan = plan_publish_batch(
        publish_manifest,
        config,
        collision,
        allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
        require_coverage=require_coverage,
    )
    return taxonomy_new_leaf_authorization_from_plan(plan)


def _missing_parent_dirs_before_write(target: Path, wiki_dir: Path) -> list[Path]:
    missing: list[Path] = []
    current = target.parent
    while current != wiki_dir and not current.exists():
        missing.append(current)
        current = current.parent
    return missing


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _rollback_publish_failure(
    created_paths: list[Path],
    raw_originals: dict[Path, str],
    raw_restore_order: list[Path],
    parent_dirs_to_prune: list[Path],
) -> dict[str, list[str]]:
    rollback = {
        "deleted_notes": [],
        "restored_raw_files": [],
        "removed_dirs": [],
        "rollback_errors": [],
    }

    for path in reversed(created_paths):
        try:
            if path.exists():
                path.unlink()
                rollback["deleted_notes"].append(str(path))
        except Exception as exc:  # pragma: no cover - exercised only on OS-level rollback failures
            rollback["rollback_errors"].append(f"delete note {path}: {exc}")

    for raw_file in reversed(_unique_paths(raw_restore_order)):
        original = raw_originals.get(raw_file)
        if original is None:
            continue
        try:
            atomic_write_text(raw_file, original)
            rollback["restored_raw_files"].append(str(raw_file))
        except Exception as exc:  # pragma: no cover - exercised only on OS-level rollback failures
            rollback["rollback_errors"].append(f"restore raw chat {raw_file}: {exc}")

    dirs = sorted(_unique_paths(parent_dirs_to_prune), key=lambda item: len(item.parts), reverse=True)
    for directory in dirs:
        try:
            if directory.exists() and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                rollback["removed_dirs"].append(str(directory))
        except Exception as exc:  # pragma: no cover - exercised only on OS-level rollback failures
            rollback["rollback_errors"].append(f"remove directory {directory}: {exc}")

    return rollback


def _format_rollback_message(exc: Exception, rollback: dict[str, list[str]]) -> str:
    return (
        "Batch publish failed; automatic rollback attempted. "
        f"Original error: {exc}. "
        f"Deleted notes: {rollback['deleted_notes']}. "
        f"Restored raw chats: {rollback['restored_raw_files']}. "
        f"Removed empty directories: {rollback['removed_dirs']}. "
        f"Rollback errors: {rollback['rollback_errors']}."
    )


def publish_batch(
    manifest: Path,
    config: MedConfig,
    collision: str = "abort",
    dry_run: bool = False,
    backup: bool = False,
    allow_new_taxonomy_leaf: bool = True,
    require_coverage: bool = True,
) -> JsonObject:
    result = publish_batch_operation_result(
        manifest,
        config,
        collision=collision,
        dry_run=dry_run,
        backup=backup,
        allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
        require_coverage=require_coverage,
    )
    return process_chats_fsm_payload_from_publish_result(
        result,
        run_id=_process_chats_run_id(manifest, result),
        version_control_safety=_process_chats_version_control_safety(result, applying=not dry_run),
    )


def publish_batch_operation_result(
    manifest: Path,
    config: MedConfig,
    collision: str = "abort",
    dry_run: bool = False,
    backup: bool = False,
    allow_new_taxonomy_leaf: bool = True,
    require_coverage: bool = True,
) -> JsonObject:
    backup = False
    try:
        typed_manifest = _load_publish_manifest(manifest)
        if require_coverage:
            typed_manifest.require_coverage()
    except (PydanticValidationError, ValidationError, ValueError) as exc:
        blocked_reason = "coverage_path_missing" if "coverage_path" in str(exc) else "manifest_invalid"
        return _blocked_publish_contract_receipt(
            manifest=manifest,
            root_cause="publish.manifest_contract_invalid",
            blocked_reason=blocked_reason,
            error_summary=str(exc),
            next_action="Recriar manifest, note_plan e coverage pela rota oficial antes de publicar.",
    )
    if not dry_run:
        try:
            ensure_markdown_query_available(
                wiki_dir=config.wiki_dir,
                raw_dir=config.raw_dir,
                state_dir=config.state_dir,
            )
        except MarkdownQueryUnavailable as exc:
            error_context = {
                "blocked_reason": exc.blocked_reason,
                "root_cause": "markdown_query_index_unavailable",
                "affected_artifact": "markdown_query_index",
                "error_summary": str(exc),
                "suggested_fix": exc.next_action,
                "next_action": exc.next_action,
                "retry_scope": "setup_markdown_query_index_then_retry",
                "details": exc.payload,
            }
            return annotate_payload(
                {
                    **markdown_query_blocked_payload(
                        phase="publish_apply",
                        required_inputs=PUBLISH_REQUIRED_INPUTS,
                    ),
                    "error_context": error_context,
                    "publish_receipt": build_publish_receipt_payload(
                        status="blocked",
                        batch_id=_publish_batch_id(manifest),
                        published_count=0,
                        skipped_count=0,
                        items=[],
                        next_action=exc.next_action,
                        error_context=error_context,
                    ),
                    "runtime_observation": _process_chats_runtime_observation_payload(
                        source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
                        blocked=True,
                        publish_stale_receipt=True,
                        reason_code=exc.blocked_reason,
                        next_action=exc.next_action,
                        manifest_path=str(manifest),
                        receipt_id=_publish_batch_id(manifest),
                        error_context=error_context,
                    ),
                },
                phase="publish_apply",
                status="blocked",
                blocked_reason=exc.blocked_reason,
                next_action=exc.next_action,
                required_inputs=PUBLISH_REQUIRED_INPUTS,
                human_decision_required=False,
            )
    plan = plan_publish_batch(
        typed_manifest,
        config,
        collision,
        allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
        require_coverage=require_coverage,
    )
    new_leaf_authorization = taxonomy_new_leaf_authorization_from_plan(plan)
    created: list[str] = []
    created_paths: list[Path] = []
    parent_dirs_to_prune: list[Path] = []
    raw_restore_order: list[Path] = []
    raw_updates: list[JsonObject] = []
    if dry_run:
        return annotate_payload({
            "dry_run": True,
            "backup": backup,
            "manifest": str(manifest),
            "manifest_hash": file_sha256(manifest),
            "allow_new_taxonomy_leaf": allow_new_taxonomy_leaf,
            "require_coverage": require_coverage,
            "batch_state": [batch["batch_state"] for batch in plan if batch.get("batch_state")],
            "coverage_summary": coverage_summary_from_plan(plan),
            "new_taxonomy_leaf_authorization": new_leaf_authorization,
            "planned_batches": plan,
            "created": [],
            "raw_updates": [],
            "publish_receipt": build_publish_receipt_payload(
                status="ready_to_publish",
                batch_id=_publish_batch_id(manifest),
                published_count=0,
                skipped_count=0,
                items=[],
                next_action="Revisar o plano e então rodar publish-batch sem --dry-run com o mesmo manifest.",
            ),
            "runtime_observation": _process_chats_runtime_observation_payload(
                source_state=ProcessChatsState.STAGING_MANIFEST_READY,
                preview_ready=True,
                reason_code="ready_to_publish",
                next_action="Revisar o plano e então rodar publish-batch sem --dry-run com o mesmo manifest.",
                manifest_path=str(manifest),
                dry_run_receipt_path=str(manifest),
                receipt_id=_publish_batch_id(manifest),
            ),
        },
            phase="publish_dry_run",
            status="ready_to_publish",
            next_action="Revisar o plano e então rodar publish-batch sem --dry-run com o mesmo manifest.",
            required_inputs=PUBLISH_REQUIRED_INPUTS,
        )

    raw_files_to_update = _unique_paths(
        [
            Path(raw_file)
            for batch in plan
            for raw_file in (batch.get("raw_files") or [batch["raw_file"]])
        ]
    )
    raw_originals = {
        raw_file: raw_file.read_text(encoding="utf-8")
        for raw_file in raw_files_to_update
    }

    try:
        for batch in plan:
            batch_raw_files = [Path(path) for path in (batch.get("raw_files") or [batch["raw_file"]])]
            coverage_value = batch.get("coverage")
            coverage_summary = JsonObjectAdapter.validate_python(coverage_value) if isinstance(coverage_value, dict) else None
            for item in batch["notes"]:
                content = Path(item["content_path"]).read_text(encoding="utf-8")
                prepared_content = _prepare_note_content(
                    content,
                    title=str(item["title"]),
                    raw_files=batch_raw_files,
                    coverage_summary=coverage_summary,
                )
                target_path = Path(item["target_path"])
                parent_dirs_to_prune.extend(_missing_parent_dirs_before_write(target_path, config.wiki_dir))
                write_new_note(target_path, prepared_content, create_parent=bool(item.get("taxonomy_new_dirs")))
                created_paths.append(target_path)
                created.append(item["target_path"])
        for raw_file in raw_files_to_update:
            raw_restore_order.append(raw_file)
            raw_updates.append(
                mutate_raw_frontmatter(
                    raw_file,
                    {"status": "processado", "processed_at": _now_iso()},
                    dry_run=False,
                    backup=backup,
                )
            )
    except Exception as exc:
        rollback = _rollback_publish_failure(
            created_paths,
            raw_originals,
            raw_restore_order,
            parent_dirs_to_prune,
        )
        raise MedOpsError(_format_rollback_message(exc, rollback)) from exc

    return annotate_payload({
        "dry_run": False,
        "backup": backup,
        "manifest": str(manifest),
        "manifest_hash": file_sha256(manifest),
        "allow_new_taxonomy_leaf": allow_new_taxonomy_leaf,
        "require_coverage": require_coverage,
        "batch_state": [batch["batch_state"] for batch in plan if batch.get("batch_state")],
        "coverage_summary": coverage_summary_from_plan(plan),
        "created": created,
        "raw_updates": raw_updates,
        "created_count": len(created),
        "processed_raw_count": len(raw_updates),
        "new_taxonomy_leaf_authorization": {
            **new_leaf_authorization,
            "authorized_by_dry_run_receipt": new_leaf_authorization["required"],
        },
        "publish_receipt": build_publish_receipt_payload(
            status="published",
            batch_id=_publish_batch_id(manifest),
            published_count=len(created),
            skipped_count=0,
            items=[{"path": path, "status": "published"} for path in created],
            next_action=(
                "Rodar run-linker --diagnose para gerar o diagnóstico do grafo e, se seguro, "
                "run-linker --apply --diagnosis <link-diagnosis.json>."
            ),
        ),
        "runtime_observation": _process_chats_runtime_observation_payload(
            source_state=ProcessChatsState.PUBLISH_APPLY_REQUESTED,
            publish_completed=True,
            reason_code="published",
            next_action=(
                "Rodar run-linker --diagnose para gerar o diagnóstico do grafo e, se seguro, "
                "run-linker --apply --diagnosis <link-diagnosis.json>."
            ),
            manifest_path=str(manifest),
            receipt_id=_publish_batch_id(manifest),
            published_count=len(created),
        ),
    },
        phase="publish_apply",
        status="published",
        next_action=(
            "Rodar run-linker --diagnose para gerar o diagnóstico do grafo e, se seguro, "
            "run-linker --apply --diagnosis <link-diagnosis.json>."
        ),
        required_inputs=PUBLISH_REQUIRED_INPUTS,
    )


def _process_chats_run_id(manifest: Path, result: JsonObject) -> str:
    fields = _ProcessChatsPublishSafetyFields.model_validate(result)
    basis = fields.manifest_hash
    if not basis:
        try:
            basis = file_sha256(manifest)
        except OSError:
            basis = manifest.name
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in basis)[:48].strip("-")
    return f"process-chats-{safe or 'run'}"


def _process_chats_version_control_safety(result: JsonObject, *, applying: bool) -> JsonObject:
    fields = _ProcessChatsPublishSafetyFields.model_validate(result)
    mutated = applying and (bool(fields.created) or fields.processed_raw_count > 0)
    return {
        "resource_guard_active": mutated,
        "run_start_seen": mutated,
        "run_finish_seen": mutated,
        "restore_point_before": "vault-guard" if mutated else "",
        "restore_point_after": "vault-guard" if mutated else "",
        "sync_status": "not_checked",
        "backup_online": "not_checked",
        "direct_mutation_forbidden": True,
        "mutation_without_guard": False,
        "rollback_declared": mutated,
        "no_resource_mutation": not mutated,
    }


def stage_note(
    manifest: Path,
    raw_file: Path,
    taxonomy: str,
    title: str,
    content_path: Path,
    dry_run: bool = False,
    config: MedConfig | None = None,
    allow_new_taxonomy_leaf: bool = True,
    coverage_path: Path | None = None,
) -> JsonObject:
    taxonomy_resolution = (
        resolve_taxonomy(config.wiki_dir, taxonomy, title=title, allow_new_leaf=allow_new_taxonomy_leaf)
        if config is not None
        else None
    )
    canonical_taxonomy = taxonomy_resolution.taxonomy if taxonomy_resolution else "/".join(normalize_taxonomy(taxonomy))
    _validate_taxonomy_not_title(tuple(canonical_taxonomy.split("/")), title)
    filename = safe_title(title)
    if taxonomy_resolution is not None and config is not None:
        target = config.wiki_dir.joinpath(*taxonomy_resolution.parts, f"{filename}.md")
        _validate_normalized_target_available(target, config.wiki_dir, _wiki_note_targets(config.wiki_dir), {})
    if not raw_file.exists():
        raise MissingPathError(f"Raw file not found: {raw_file}")
    if not content_path.exists():
        raise MissingPathError(f"Content file not found: {content_path}")
    content = content_path.read_text(encoding="utf-8")
    if manifest.exists():
        data = _load_manifest(manifest)
        _load_publish_manifest(manifest)
    else:
        data = {"schema": "medical-notes-workbench.publish-manifest.v1", "batches": []}
    item = {
        "taxonomy": canonical_taxonomy,
        "title": title,
        "content_path": str(content_path),
        "safe_filename": f"{filename}.md",
    }
    batch = _batch_for_stage(data, raw_file)
    notes = batch["notes"]
    coverage_summary: JsonObject | None = None
    raw_files = [raw_file]
    if coverage_path is not None:
        coverage_summary = validate_raw_coverage_structure(coverage_path, raw_file)
        existing_coverage = batch.get("coverage_path")
        if existing_coverage and not _paths_match(str(existing_coverage), coverage_path):
            raise ValidationError(
                f"Manifest batch already has a different coverage_path: {existing_coverage}"
            )
        batch["coverage_path"] = str(coverage_path)
        raw_files = _raw_files_from_summary(coverage_summary, raw_file)
        batch["raw_files"] = [str(path) for path in raw_files]
        merge_batch_state(
            batch,
            coverage_summary,
            target_label="manifest batch",
            source_label="coverage inventory",
        )
    prepared_content = _prepare_note_content(
        content,
        title=title,
        raw_files=raw_files,
        coverage_summary=coverage_summary,
    )
    validate_wiki_note_contract(prepared_content, title=title, raw_file=raw_file)
    artifact_validation = validate_note_artifacts(
        prepared_content,
        raw_file=raw_file,
        artifact_dir=config.artifact_dir if config is not None else None,
    )
    if not dry_run:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        notes.append(item)
        atomic_write_text(manifest, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    note_count, batch_count = _staged_manifest_counts(data, pending_note=dry_run)
    result: JsonObject = {
        "manifest": str(manifest),
        "dry_run": dry_run,
        "staged": item,
        "artifact_validation": artifact_validation,
        "note_count": note_count,
        "batch_count": batch_count,
    }
    if coverage_path is not None:
        result["coverage_path"] = str(coverage_path)
    if coverage_summary is not None:
        result["raw_files"] = [str(path) for path in _raw_files_from_summary(coverage_summary, raw_file)]
        batch_state = batch_state_from(coverage_summary)
        if batch_state:
            result["batch_state"] = batch_state
    if taxonomy_resolution is not None and config is not None:
        result["taxonomy_resolution"] = taxonomy_resolution.to_json(config.wiki_dir, title=title)
    return annotate_payload(
        result,
        phase="stage_note",
        status="preview_ready",
        next_action=(
            "Adicionar as demais notas/coberturas ao manifest antes do publish-batch --dry-run."
            if not dry_run
            else "Se a nota estiver correta, repetir stage-note sem --dry-run."
        ),
        required_inputs=["raw_file", "taxonomy", "title", "content_path", "coverage_path"],
    )
