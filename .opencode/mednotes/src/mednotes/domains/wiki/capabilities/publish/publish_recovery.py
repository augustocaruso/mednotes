"""Read-only publish/process-chats recovery diagnostics."""
from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import ConfigDict, Field, StrictInt, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.publish.publish import (
    publish_batch_operation_result,
)
from mednotes.domains.wiki.capabilities.publish.publish_receipts import require_publish_dry_run
from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.config import MedConfig
from mednotes.domains.wiki.contracts.workflow_guardrails import PUBLISH_REQUIRED_INPUTS, annotate_payload, error_context
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, contract_error

PUBLISH_STATE_DIAGNOSIS_SCHEMA = "medical-notes-workbench.publish-state-diagnosis.v1"


class _PublishStateDryRunReceipt(ContractModel):
    expires_at: StrictInt
    manifest_hash: StrictStr = ""
    manifest_sha256: StrictStr = ""
    dry_run_options_hash: StrictStr = ""
    batch_state: list[JsonObject] = Field(default_factory=list)

    def status_payload(self) -> JsonObject:
        return JsonObjectAdapter.validate_python(
            {
                "expires_at": self.expires_at,
                "manifest_hash": self.manifest_hash or self.manifest_sha256,
                "dry_run_options_hash": self.dry_run_options_hash,
                "batch_state": [dict(item) for item in self.batch_state],
            }
        )


class _PublishStatePlannedBatch(ContractModel):
    model_config = ConfigDict(extra="ignore")

    notes: list[JsonObject] = Field(default_factory=list)


class _PublishStatePreviewResult(ContractModel):
    """Typed lens over publish dry-run output consumed by publish-status."""

    model_config = ConfigDict(extra="ignore")

    status: StrictStr
    blocked_reason: StrictStr = ""
    next_action: StrictStr = ""
    planned_batches: list[_PublishStatePlannedBatch] = Field(default_factory=list)
    batch_state: list[JsonObject] = Field(default_factory=list)
    new_taxonomy_leaf_authorization: JsonObject = Field(default_factory=dict)
    error_context: JsonObject = Field(default_factory=dict)


def _dry_run_receipt_status_payload(receipt: object) -> JsonObject:
    try:
        payload = JsonObjectAdapter.validate_python(receipt)
        status_fields: JsonObject = {
            key: payload[key]
            for key in ("expires_at", "manifest_hash", "manifest_sha256", "dry_run_options_hash", "batch_state")
            if key in payload
        }
        return _PublishStateDryRunReceipt.model_validate(status_fields).status_payload()
    except PydanticValidationError as exc:
        detail = contract_error(exc, prefix="publish-status dry-run receipt invalid")
        raise ValidationError(
            "Bloqueado: o dry-run receipt retornado pela recuperação de publish é inválido. "
            "Rode publish-batch --dry-run novamente. "
            f"Detalhe: {detail}"
        ) from exc


def _cmd(manifest: Path, *, dry_run: bool = False) -> str:
    parts = ["publish-batch", "--manifest", str(manifest)]
    if dry_run:
        parts.append("--dry-run")
    return " ".join(shlex.quote(part) for part in parts)


def _blocked_payload(
    *,
    manifest: Path,
    status_payload: JsonObject,
    blocked_reason: str,
    affected_artifact: str,
    error_summary: str,
    suggested_fix: str,
    retry_scope: str,
    next_action: str,
    missing_inputs: list[str] | None = None,
) -> JsonObject:
    missing_inputs = missing_inputs or []
    payload: JsonObject = JsonObjectAdapter.validate_python({
        **status_payload,
        "dry_run_receipt_status": "invalid" if blocked_reason == "dry_run_receipt_invalid" else "not_checked",
        "mutated": False,
        "missing_inputs": missing_inputs,
    })
    payload["error_context"] = error_context(
        phase="publish-status",
        blocked_reason=blocked_reason,
        root_cause=blocked_reason,
        affected_artifact=affected_artifact,
        error_summary=error_summary,
        suggested_fix=suggested_fix,
        next_action=next_action,
        retry_scope=retry_scope,
        missing_inputs=missing_inputs,
    )
    return annotate_payload(
        payload,
        phase="publish_status",
        status="blocked",
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=PUBLISH_REQUIRED_INPUTS,
    )


def _blocked_payload_from_preview(
    *,
    manifest: Path,
    status_payload: JsonObject,
    preview: _PublishStatePreviewResult,
) -> JsonObject:
    error_ctx = preview.error_context
    blocked_reason = str(
        error_ctx.get("blocked_reason")
        or error_ctx.get("root_cause")
        or preview.blocked_reason
        or "publish_state_blocked"
    )
    next_action = str(
        error_ctx.get("next_action")
        or preview.next_action
        or "Corrigir o erro estruturado e repetir publish-status antes de publicar."
    )
    missing_inputs = error_ctx.get("missing_inputs")
    return _blocked_payload(
        manifest=manifest,
        status_payload=status_payload,
        blocked_reason=blocked_reason,
        affected_artifact=str(error_ctx.get("affected_artifact") or "publish_state"),
        error_summary=str(error_ctx.get("error_summary") or "Publish dry-run blocked."),
        suggested_fix=str(error_ctx.get("suggested_fix") or next_action),
        retry_scope=str(error_ctx.get("retry_scope") or "inspect_publish_state"),
        next_action=next_action,
        missing_inputs=[str(item) for item in missing_inputs] if isinstance(missing_inputs, list) else [],
    )


def _dry_run_receipt_blocked_payload(*, manifest: Path, status_payload: JsonObject, message: str) -> JsonObject:
    next_action = f"Rodar {_cmd(manifest, dry_run=True)} com as mesmas opções antes do publish real."
    return _blocked_payload(
        manifest=manifest,
        status_payload=status_payload,
        blocked_reason="dry_run_receipt_invalid",
        affected_artifact="dry_run_receipt",
        error_summary=message,
        suggested_fix="Gerar novo dry-run receipt com publish-batch --dry-run.",
        retry_scope="publish_dry_run_then_apply",
        next_action=next_action,
        missing_inputs=["dry_run_receipt"],
    )


def diagnose_publish_state(
    manifest: Path,
    config: MedConfig,
    *,
    collision: str = "abort",
    allow_new_taxonomy_leaf: bool = True,
    require_coverage: bool = True,
) -> JsonObject:
    """Return a non-mutating publish readiness diagnosis for agents."""
    manifest = Path(manifest)
    status_payload: JsonObject = {
        "schema": PUBLISH_STATE_DIAGNOSIS_SCHEMA,
        "manifest": str(manifest),
        "manifest_exists": manifest.exists(),
        "manifest_hash": file_sha256(manifest) if manifest.exists() else "",
        "collision": collision,
        "allow_new_taxonomy_leaf": bool(allow_new_taxonomy_leaf),
        "require_coverage": bool(require_coverage),
        "planned_batch_count": 0,
        "planned_note_count": 0,
        "batch_state": [],
        "mutated": False,
    }
    preview = _PublishStatePreviewResult.model_validate(
        publish_batch_operation_result(
            manifest,
            config,
            collision=collision,
            dry_run=True,
            allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
            require_coverage=require_coverage,
        )
    )
    if preview.status == "blocked":
        return _blocked_payload_from_preview(manifest=manifest, status_payload=status_payload, preview=preview)
    status_payload.update(
        {
            "planned_batch_count": len(preview.planned_batches),
            "planned_note_count": sum(len(batch.notes) for batch in preview.planned_batches),
            "batch_state": preview.batch_state,
            "new_taxonomy_leaf_authorization_required": bool(preview.new_taxonomy_leaf_authorization.get("required")),
        }
    )
    try:
        receipt = require_publish_dry_run(
            manifest,
            config,
            collision=collision,
            allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
            require_coverage=require_coverage,
            new_taxonomy_leaf_authorization=preview.new_taxonomy_leaf_authorization,
        )
    except ValidationError as exc:
        return _dry_run_receipt_blocked_payload(
            manifest=manifest,
            status_payload=status_payload,
            message=str(exc),
        )

    status_payload["dry_run_receipt_status"] = "valid"
    status_payload["dry_run_receipt"] = _dry_run_receipt_status_payload(receipt)
    return annotate_payload(
        status_payload,
        phase="publish_status",
        status="ready",
        next_action=f"Rodar {_cmd(manifest)} com as mesmas opções.",
        required_inputs=PUBLISH_REQUIRED_INPUTS,
    )
