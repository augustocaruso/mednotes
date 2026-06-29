"""Dry-run receipts for destructive publish-batch CLI runs."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from pydantic import Field, StrictBool, StrictInt, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import batch_state_from, canonical_json_hash
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import ValidationError, _now_iso
from mednotes.domains.wiki.config import MedConfig, _path, _user_state_dir
from mednotes.domains.wiki.contracts.publish import PublishReceipt
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, contract_error

PUBLISH_DRY_RUN_RECEIPTS_SCHEMA = "medical-notes-workbench.publish-dry-run-receipts.v1"
DEFAULT_PUBLISH_DRY_RUN_TTL_SECONDS = 30 * 60


def _default_new_leaf_authorization() -> JsonObject:
    return {"required": False, "note_count": 0, "notes": []}


def _default_new_leaf_authorization_hash() -> str:
    return canonical_json_hash(_default_new_leaf_authorization())


class _NewTaxonomyLeafAuthorizationFields(ContractModel):
    required: StrictBool = False


class _PublishDryRunReceipt(ContractModel):
    manifest: StrictStr = Field(min_length=1)
    manifest_sha256: StrictStr = Field(min_length=1)
    manifest_hash: StrictStr = ""
    cwd: StrictStr = Field(min_length=1)
    wiki_dir: StrictStr = Field(min_length=1)
    raw_dir: StrictStr = Field(min_length=1)
    collision: StrictStr = Field(min_length=1)
    allow_new_taxonomy_leaf: StrictBool
    require_coverage: StrictBool
    dry_run_options_hash: StrictStr = Field(min_length=1)
    batch_state: list[JsonObject] = Field(default_factory=list)
    new_taxonomy_leaf_authorization: JsonObject = Field(default_factory=_default_new_leaf_authorization)
    new_taxonomy_leaf_authorization_hash: StrictStr = Field(default_factory=_default_new_leaf_authorization_hash)
    dry_run_at: StrictStr = Field(min_length=1)
    expires_at: StrictInt


def build_publish_receipt_payload(
    *,
    status: str,
    batch_id: str,
    published_count: int,
    skipped_count: int,
    items: list[JsonObject],
    next_action: str = "",
    error_context: JsonObject | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "schema": "medical-notes-workbench.publish-receipt.v1",
        "status": status,
        "batch_id": batch_id,
        "published_count": published_count,
        "skipped_count": skipped_count,
        "items": JsonObjectAdapter.validate_python({"items": items})["items"],
        "next_action": next_action,
    }
    if error_context is not None:
        payload["error_context"] = JsonObjectAdapter.validate_python({"error_context": error_context})[
            "error_context"
        ]
    return PublishReceipt.model_validate(payload).to_payload()


def publish_receipts_path() -> Path:
    override = os.environ.get("MEDNOTES_PUBLISH_RECEIPTS_PATH")
    if override:
        return _path(override)
    return _user_state_dir() / "publish-dry-run-receipts.json"


def publish_receipt_ttl_seconds() -> int:
    value = os.environ.get("MEDNOTES_PUBLISH_DRY_RUN_TTL_SECONDS")
    if not value:
        return DEFAULT_PUBLISH_DRY_RUN_TTL_SECONDS
    try:
        seconds = int(value)
    except ValueError:
        return DEFAULT_PUBLISH_DRY_RUN_TTL_SECONDS
    return min(24 * 60 * 60, max(1, seconds))


def _manifest_key(manifest: Path) -> str:
    try:
        return str(manifest.resolve())
    except OSError:
        return str(manifest)


def _sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"Manifest not found: {path}") from exc
    return hashlib.sha256(data).hexdigest()


def _manifest_batch_state(manifest: Path) -> list[dict[str, str]]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw_batches = data.get("batches") if "batches" in data else [data]
    if not isinstance(raw_batches, list):
        return []
    states: list[dict[str, str]] = []
    for batch in raw_batches:
        if not isinstance(batch, dict):
            continue
        state = batch_state_from(batch)
        raw_file = batch.get("raw_file")
        raw_files = batch.get("raw_files")
        coverage_path = batch.get("coverage_path")
        if raw_file:
            state["raw_file"] = str(raw_file)
        if isinstance(raw_files, list) and raw_files:
            state["raw_files"] = ",".join(str(item) for item in raw_files if str(item).strip())
        if coverage_path:
            state["coverage_path"] = str(coverage_path)
        if state:
            states.append(state)
    return states


def _dry_run_options_hash(
    *,
    cwd: str,
    wiki_dir: str,
    raw_dir: str,
    collision: str,
    allow_new_taxonomy_leaf: bool,
    require_coverage: bool,
) -> str:
    return canonical_json_hash(
        {
            "cwd": cwd,
            "wiki_dir": wiki_dir,
            "raw_dir": raw_dir,
            "collision": collision,
            "allow_new_taxonomy_leaf": bool(allow_new_taxonomy_leaf),
            "require_coverage": bool(require_coverage),
        }
    )


def _empty_state() -> JsonObject:
    return {"schema": PUBLISH_DRY_RUN_RECEIPTS_SCHEMA, "receipts": {}}


def _load_state(path: Path | None = None) -> JsonObject:
    state_path = path or publish_receipts_path()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_state()
    try:
        payload = JsonObjectAdapter.validate_python(data)
    except PydanticValidationError:
        return _empty_state()
    receipts = payload["receipts"] if "receipts" in payload else {}
    if not isinstance(receipts, dict):
        payload["receipts"] = {}
    payload["schema"] = PUBLISH_DRY_RUN_RECEIPTS_SCHEMA
    return payload


def _save_state(state: JsonObject, path: Path | None = None) -> None:
    state_path = path or publish_receipts_path()
    state["schema"] = PUBLISH_DRY_RUN_RECEIPTS_SCHEMA
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _signature(
    manifest: Path,
    config: MedConfig,
    *,
    collision: str,
    allow_new_taxonomy_leaf: bool,
    require_coverage: bool,
) -> JsonObject:
    manifest_hash = _sha256_file(manifest)
    cwd = str(Path.cwd().resolve())
    wiki_dir = str(config.wiki_dir)
    raw_dir = str(config.raw_dir)
    return JsonObjectAdapter.validate_python({
        "manifest": _manifest_key(manifest),
        "manifest_sha256": manifest_hash,
        "manifest_hash": manifest_hash,
        "cwd": cwd,
        "wiki_dir": wiki_dir,
        "raw_dir": raw_dir,
        "collision": collision,
        "allow_new_taxonomy_leaf": bool(allow_new_taxonomy_leaf),
        "require_coverage": bool(require_coverage),
        "dry_run_options_hash": _dry_run_options_hash(
            cwd=cwd,
            wiki_dir=wiki_dir,
            raw_dir=raw_dir,
            collision=collision,
            allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
            require_coverage=require_coverage,
        ),
        "batch_state": _manifest_batch_state(manifest),
    })


def _new_leaf_authorization_payload(authorization: object | None) -> JsonObject:
    if authorization is None:
        return _default_new_leaf_authorization()
    try:
        return JsonObjectAdapter.validate_python(authorization)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="new taxonomy leaf authorization invalid") from exc


def _new_leaf_authorization_fields(authorization: JsonObject) -> _NewTaxonomyLeafAuthorizationFields:
    raw_fields: JsonObject = {}
    if "required" in authorization:
        raw_fields["required"] = authorization["required"]
    try:
        return _NewTaxonomyLeafAuthorizationFields.model_validate(raw_fields)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="new taxonomy leaf authorization invalid") from exc


def _new_leaf_authorization_hash(authorization: object | None) -> str:
    return canonical_json_hash(_new_leaf_authorization_payload(authorization))


def _state_receipts(state: JsonObject) -> JsonObject:
    receipts = state["receipts"] if "receipts" in state else {}
    if not isinstance(receipts, dict):
        return {}
    return JsonObjectAdapter.validate_python(receipts)


def _receipt_from_state(state: JsonObject, key: str) -> object | None:
    receipts = state["receipts"] if "receipts" in state else {}
    if not isinstance(receipts, dict) or key not in receipts:
        return None
    return receipts[key]


def _dry_run_receipt_fields(receipt: object) -> _PublishDryRunReceipt:
    try:
        payload = JsonObjectAdapter.validate_python(receipt)
        return _PublishDryRunReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        detail = contract_error(exc, prefix="publish dry-run receipt invalid")
        raise ValidationError(
            "Bloqueado: o dry-run receipt salvo é inválido. Rode publish-batch --dry-run novamente. "
            f"Detalhe: {detail}"
        ) from exc


def record_publish_dry_run(
    manifest: Path,
    config: MedConfig,
    *,
    collision: str,
    allow_new_taxonomy_leaf: bool,
    require_coverage: bool,
    new_taxonomy_leaf_authorization: JsonObject | None = None,
) -> JsonObject:
    state = _load_state()
    now = int(time.time())
    authorization_payload = _new_leaf_authorization_payload(new_taxonomy_leaf_authorization)
    receipt = {
        **_signature(
            manifest,
            config,
            collision=collision,
            allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
            require_coverage=require_coverage,
        ),
        "new_taxonomy_leaf_authorization": authorization_payload,
        "new_taxonomy_leaf_authorization_hash": _new_leaf_authorization_hash(authorization_payload),
        "dry_run_at": _now_iso(),
        "expires_at": now + publish_receipt_ttl_seconds(),
    }
    receipts = _state_receipts(state)
    receipts[_manifest_key(manifest)] = JsonObjectAdapter.validate_python(receipt)
    state["receipts"] = receipts
    _save_state(state)
    return JsonObjectAdapter.validate_python(receipt)


def require_publish_dry_run(
    manifest: Path,
    config: MedConfig,
    *,
    collision: str,
    allow_new_taxonomy_leaf: bool,
    require_coverage: bool,
    new_taxonomy_leaf_authorization: JsonObject | None = None,
) -> JsonObject:
    state = _load_state()
    key = _manifest_key(manifest)
    receipt_value = _receipt_from_state(state, key)
    if receipt_value is None:
        raise ValidationError(
            "dry_run_receipt_invalid: rode publish-batch --dry-run para este manifest antes do publish real."
        )
    receipt = _dry_run_receipt_fields(receipt_value)

    if int(time.time()) > receipt.expires_at:
        raise ValidationError(
            "dry_run_receipt_invalid: o dry-run desse manifest expirou. Rode publish-batch --dry-run novamente."
        )

    current = _signature(
        manifest,
        config,
        collision=collision,
        allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
        require_coverage=require_coverage,
    )
    if receipt.manifest_sha256 != current["manifest_sha256"]:
        raise ValidationError(
            "dry_run_receipt_invalid: o manifest mudou desde o dry-run. Rode publish-batch --dry-run novamente."
        )
    receipt_signature = {
        "cwd": receipt.cwd,
        "wiki_dir": receipt.wiki_dir,
        "raw_dir": receipt.raw_dir,
        "collision": receipt.collision,
        "allow_new_taxonomy_leaf": receipt.allow_new_taxonomy_leaf,
        "require_coverage": receipt.require_coverage,
    }
    for field, value in receipt_signature.items():
        if value != current[field]:
            raise ValidationError(
                "dry_run_receipt_invalid: caminhos ou opcoes de publish mudaram desde o dry-run. "
                "Rode publish-batch --dry-run novamente."
            )
    expected_auth = _new_leaf_authorization_payload(new_taxonomy_leaf_authorization)
    if _new_leaf_authorization_fields(expected_auth).required:
        expected_hash = _new_leaf_authorization_hash(expected_auth)
        if receipt.new_taxonomy_leaf_authorization_hash != expected_hash:
            raise ValidationError("new_taxonomy_leaf_requires_dry_run_authorization")
    return receipt.to_payload()


def clear_publish_dry_run(manifest: Path) -> None:
    state = _load_state()
    receipts = state.setdefault("receipts", {})
    if receipts.pop(_manifest_key(manifest), None) is not None:
        _save_state(state)
