"""Reviewed insert preview/apply for PDF library figures."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, NonNegativeInt, StrictBool, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.platform.paths import extension_root as _resolve_extension_root

EXTENSION_SRC_DIR = _resolve_extension_root() / "src"
if str(EXTENSION_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC_DIR))

from mednotes.domains.wiki.capabilities.illustrate.core import frontmatter as image_frontmatter  # noqa: E402
from mednotes.domains.wiki.capabilities.illustrate.core.insert import (  # noqa: E402
    InsertedImage,
    insert_images,
    parse_sections,
)
from mednotes.domains.wiki.capabilities.illustrate.core.local_import import (  # noqa: E402
    StagedImage,
    commit_staged_image,
    stage_crop,
)
from mednotes.domains.wiki.capabilities.pdf import paths  # noqa: E402
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter  # noqa: E402
from mednotes.platform.vault_guard import VaultGuardError, active_guard_exists, require_vault_guard  # noqa: E402

PREVIEW_SCHEMA = "medical-notes-workbench.pdf-library-insert-preview.v1"
APPLY_SCHEMA = "medical-notes-workbench.pdf-library-insert-apply.v1"
WORKFLOW = "/mednotes:pdf-library"
IMAGE_FRONTMATTER_KEYS = frozenset({"images_enriched", "images_enriched_at", "image_count", "image_sources"})


class _PdfInsertPreviewReceiptPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    status: StrictStr
    workflow: StrictStr
    note_path: StrictStr
    note_sha256_before: StrictStr
    figure_uid: StrictStr
    image_sha256: StrictStr
    staged_image_path: StrictStr
    attachment_filename_planned: StrictStr
    section_path: list[StrictStr] = Field(default_factory=list)
    concept: StrictStr = "figura revisada de PDF"
    source_url: StrictStr
    frontmatter_patch_keys: list[StrictStr] = Field(default_factory=list)
    preview_payload_sha256: StrictStr
    created_at: StrictStr
    path: StrictStr = ""


class _PdfLibraryVersionControlSafetyPayload(ContractModel):
    resource_guard_active: StrictBool
    run_start_seen: StrictBool
    run_finish_seen: StrictBool
    restore_point_before: StrictBool
    restore_point_after: StrictBool
    sync_status: StrictStr
    backup_online: StrictStr
    direct_mutation_forbidden: StrictBool
    mutation_without_guard: StrictBool


class _PdfInsertApplyPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    workflow: StrictStr
    status: StrictStr
    phase: StrictStr
    note_path: StrictStr
    figure_uid: StrictStr
    attachment_filename: StrictStr
    inserted_count: NonNegativeInt
    no_linker_required_reason: StrictStr
    version_control_safety: _PdfLibraryVersionControlSafetyPayload
    workflow_run_record_path: StrictStr = ""
    receipt_path: StrictStr = ""


class _PdfLibraryBlockedPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    workflow: StrictStr
    status: StrictStr
    blocked_reason: StrictStr
    next_action: StrictStr


def preview(
    *,
    note_path: Path,
    figure_uid: str,
    section_path: list[str],
    crop_path: Path,
    app_home: Path | None = None,
    concept: str = "figura revisada de PDF",
    source_url: str | None = None,
) -> JsonObject:
    root = app_home or paths.app_home()
    note_path = note_path.expanduser().resolve(strict=False)
    note_text = note_path.read_text(encoding="utf-8")
    staged = stage_crop(crop_path, app_home=root)
    resolved_section_path = _resolve_section_path(note_text, section_path)
    item = InsertedImage(
        anchor_id=figure_uid,
        section_path=resolved_section_path,
        image_filename=staged.filename,
        concept=concept,
        source="PDF local",
        source_url=source_url or f"pdf://local?figure={figure_uid}",
    )
    planned = insert_images(note_text, [item])
    receipt = {
        "schema": PREVIEW_SCHEMA,
        "status": "ready",
        "workflow": WORKFLOW,
        "note_path": str(note_path),
        "note_sha256_before": _sha256_text(note_text),
        "figure_uid": figure_uid,
        "image_sha256": staged.sha256,
        "staged_image_path": str(staged.path),
        "attachment_filename_planned": staged.filename,
        "section_path": resolved_section_path,
        "concept": concept,
        "source_url": item.source_url,
        "frontmatter_patch_keys": ["images_enriched", "images_enriched_at", "image_count", "image_sources"],
        "preview_payload_sha256": _sha256_text(planned),
        "created_at": _now(),
    }
    receipts = root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts / f"preview-{uuid.uuid4().hex}.json"
    receipt["path"] = str(receipt_path)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return JsonObjectAdapter.validate_python(receipt)


def apply_preview(*, receipt_path: Path, confirm: bool) -> JsonObject:
    receipt_path = receipt_path.expanduser().resolve(strict=False)
    try:
        receipt = _PdfInsertPreviewReceiptPayload.model_validate(
            JsonObjectAdapter.validate_python(json.loads(receipt_path.read_text(encoding="utf-8")))
        )
    except (json.JSONDecodeError, PydanticValidationError):
        return _blocked("invalid_preview_receipt", "rerun insert preview")
    if not confirm:
        return _blocked("confirmation_required", "rerun with --confirm")
    note_path = Path(receipt.note_path)
    note_text = note_path.read_text(encoding="utf-8")
    if _sha256_text(note_text) != receipt.note_sha256_before:
        return _blocked("stale_preview_receipt", "rerun insert preview")
    staged_path = Path(receipt.staged_image_path)
    staged = StagedImage(path=staged_path, sha256=receipt.image_sha256, filename=receipt.attachment_filename_planned)
    if not staged.path.is_file() or _sha256_file(staged.path) != staged.sha256:
        return _blocked("staged_image_missing_or_changed", "rerun insert preview")
    vault_dir = _vault_dir_for(note_path)
    try:
        require_vault_guard(vault_dir, workflow=WORKFLOW, command="pdf-library insert apply")
    except VaultGuardError as exc:
        payload = exc.to_payload()
        payload.update({"schema": APPLY_SCHEMA, "workflow": WORKFLOW})
        return payload
    item = InsertedImage(
        anchor_id=receipt.figure_uid,
        section_path=list(receipt.section_path),
        image_filename=staged.filename,
        concept=receipt.concept or "figura revisada de PDF",
        source="PDF local",
        source_url=receipt.source_url or f"pdf://local?figure={receipt.figure_uid}",
    )
    new_text = insert_images(note_text, [item])
    if _unexpected_non_visual_mutation(note_text, new_text) is True:
        return _blocked("unexpected_non_visual_mutation", "rerun preview and inspect planned diff")
    attachments = _attachments_dir(vault_dir)
    imported = commit_staged_image(staged, attachments_dir=attachments)
    note_path.write_text(new_text, encoding="utf-8")
    payload = _PdfInsertApplyPayload(
        schema=APPLY_SCHEMA,
        workflow=WORKFLOW,
        status="applied",
        phase="insert_apply",
        note_path=str(note_path),
        figure_uid=receipt.figure_uid,
        attachment_filename=imported.filename,
        inserted_count=1,
        no_linker_required_reason="visual_enrichment_only_images_metadata",
        version_control_safety=_PdfLibraryVersionControlSafetyPayload(
            resource_guard_active=active_guard_exists(vault_dir),
            run_start_seen=True,
            run_finish_seen=True,
            restore_point_before=True,
            restore_point_after=True,
            sync_status="not_checked",
            backup_online="not_checked",
            direct_mutation_forbidden=True,
            mutation_without_guard=False,
        ),
        workflow_run_record_path="",
    ).to_payload()
    apply_path = receipt_path.with_name(f"apply-{uuid.uuid4().hex}.json")
    apply_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["receipt_path"] = str(apply_path)
    return JsonObjectAdapter.validate_python(payload)


def _vault_dir_for(note_path: Path) -> Path:
    configured = _configured_vault()
    if configured and _inside(note_path, configured):
        return configured
    return note_path.parent


def _configured_vault() -> Path | None:
    home = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home())
    path = home / ".mednotes" / "vault.path"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return Path(first).expanduser().resolve(strict=False) if first else None


def _attachments_dir(vault_dir: Path) -> Path:
    preferred = vault_dir / "Anexos"
    if preferred.exists():
        return preferred
    return vault_dir / "attachments" / "medicina"


def _resolve_section_path(note_text: str, section_path: list[str]) -> list[str]:
    wanted = list(section_path)
    sections = parse_sections(note_text)
    for section in sections:
        section_path_value = section.get("section_path")
        current = list(section_path_value) if isinstance(section_path_value, list) else []
        if current == wanted or (wanted and current[-len(wanted) :] == wanted):
            return current
    return wanted


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _unexpected_non_visual_mutation(before: str, after: str) -> bool:
    before_meta, before_body = image_frontmatter.read(before)
    after_meta, after_body = image_frontmatter.read(after)
    if _without_image_frontmatter(before_meta) != _without_image_frontmatter(after_meta):
        return True
    return not _body_is_visual_only_insert(before_body, after_body)


def _without_image_frontmatter(meta: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in meta.items() if key not in IMAGE_FRONTMATTER_KEYS}


def _body_is_visual_only_insert(before_body: str, after_body: str) -> bool:
    before_lines = before_body.splitlines()
    after_lines = after_body.splitlines()
    before_index = 0
    for line in after_lines:
        if before_index < len(before_lines) and line == before_lines[before_index]:
            before_index += 1
            continue
        if _visual_insert_line(line):
            continue
        return False
    return before_index == len(before_lines)


def _visual_insert_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped == ""
        or (stripped.startswith("![[") and stripped.endswith("]]"))
        or (stripped.startswith("*Figura:") and "Fonte:" in stripped)
    )


def _blocked(reason: str, next_action: str) -> JsonObject:
    status = "blocked_vault_guard_required" if reason == "vault_guard_required" else "blocked"
    return _PdfLibraryBlockedPayload(
        schema=APPLY_SCHEMA,
        workflow=WORKFLOW,
        status=status,
        blocked_reason=reason,
        next_action=next_action,
    ).to_payload()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
