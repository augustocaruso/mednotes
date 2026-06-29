"""Workbench attestation for specialist task run receipts."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _user_state_dir
from mednotes.domains.wiki.contracts.specialist import (
    SpecialistHarness,
    SpecialistModelEvidence,
    SpecialistOutputAttestationReference,
    SpecialistOutputReceiptReference,
    SpecialistQualityReviewStatus,
    SpecialistRunStatus,
    SpecialistTaskPhase,
    SpecialistTaskRunReceipt,
    SpecialistTaskRunReceiptAttestation,
    SpecialistValidationStatus,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, contract_error

SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_SCHEMA = (
    "medical-notes-workbench.specialist-task-run-receipt-attestation.v1"
)
SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_KIND = "workbench_ed25519.v1"
SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_CREATED_BY = "specialist-task-runner"
_PRIVATE_KEY_ENV = "MEDNOTES_SPECIALIST_TASK_RECEIPT_ATTESTATION_PRIVATE_KEY"
_PRIVATE_KEY_PATH_ENV = "MEDNOTES_SPECIALIST_TASK_RECEIPT_ATTESTATION_PRIVATE_KEY_PATH"
_PUBLIC_KEY_ENV = "MEDNOTES_SPECIALIST_TASK_RECEIPT_ATTESTATION_PUBLIC_KEY"
_PUBLIC_KEY_PATH_ENV = "MEDNOTES_SPECIALIST_TASK_RECEIPT_ATTESTATION_PUBLIC_KEY_PATH"
_DEFAULT_PRIVATE_KEY_NAME = "specialist-task-receipt-attestation.ed25519.private.key"
_DEFAULT_PUBLIC_KEY_NAME = "specialist-task-receipt-attestation.ed25519.public.key"


class _UnsignedSpecialistTaskRunReceipt(ContractModel):
    """Closed pre-signing view of the receipt fields bound into attestation."""

    schema_id: Literal["medical-notes-workbench.specialist-task-run-receipt.v1"] = Field(
        default="medical-notes-workbench.specialist-task-run-receipt.v1",
        alias="schema",
    )
    work_id: str = Field(min_length=1)
    phase: SpecialistTaskPhase
    harness: SpecialistHarness
    adapter: str = Field(min_length=1)
    requested_agent: str = Field(min_length=1)
    requested_model_policy: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    observed_model: str = ""
    model_evidence: SpecialistModelEvidence | None = None
    input_packet_path: str = Field(min_length=1)
    input_packet_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_path: str = ""
    output_sha256: str = ""
    status: SpecialistRunStatus
    validation_status: SpecialistValidationStatus
    quality_review_status: SpecialistQualityReviewStatus
    parent_session_id: str = ""
    specialist_session_id: str = ""
    transcript_artifact_path: str = ""
    transcript_artifact_sha256: str = ""
    error_context: JsonObject = Field(default_factory=dict)
    next_action: str = ""
    specialist_output_receipt: SpecialistOutputReceiptReference | None = None
    specialist_output_attestation: SpecialistOutputAttestationReference | None = None


def _unsigned_receipt_for_attestation(payload: JsonObject) -> _UnsignedSpecialistTaskRunReceipt:
    try:
        return _UnsignedSpecialistTaskRunReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task run receipt payload invalid") from exc


def _receipt_without_attestation(payload: JsonObject) -> JsonObject:
    return JsonObjectAdapter.validate_python(
        {key: value for key, value in payload.items() if key != "receipt_attestation"}
    )


def specialist_task_run_receipt_hash(payload: JsonObject) -> str:
    encoded = json.dumps(
        _receipt_without_attestation(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _base64_decode_key(raw: str, *, label: str) -> bytes:
    compact = raw.strip()
    if not compact:
        raise ValidationError(f"specialist task run receipt attestation {label} required")
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError(f"specialist task run receipt attestation {label} must be base64") from exc


def _key_bytes_from_env_or_path(
    *,
    env_name: str,
    path_env_name: str,
    label: str,
) -> bytes | None:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return _base64_decode_key(configured, label=label)
    configured_path = os.getenv(path_env_name, "").strip()
    if configured_path:
        key_path = Path(configured_path).expanduser()
        if not key_path.exists():
            raise MissingPathError(f"specialist task run receipt attestation {label} not found: {key_path}")
        return _base64_decode_key(key_path.read_text(encoding="utf-8"), label=label)
    return None


def _local_private_key_path() -> Path:
    return _user_state_dir() / _DEFAULT_PRIVATE_KEY_NAME


def _local_public_key_path() -> Path:
    return _user_state_dir() / _DEFAULT_PUBLIC_KEY_NAME


def _write_local_key(path: Path, key_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(key_bytes) + b"\n"
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _private_key_raw_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_key_raw_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _create_local_key_pair() -> bytes:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = _private_key_raw_bytes(private_key)
    _write_local_key(_local_private_key_path(), private_bytes)
    _write_local_key(_local_public_key_path(), _public_key_raw_bytes(private_key.public_key()))
    return private_bytes


def _local_private_key_bytes(*, create: bool) -> bytes:
    private_path = _local_private_key_path()
    if private_path.exists():
        return _base64_decode_key(private_path.read_text(encoding="utf-8"), label="private signing key")
    if create:
        return _create_local_key_pair()
    raise MissingPathError(f"specialist task run receipt attestation private signing key not found: {private_path}")


def _derive_local_public_key_from_private() -> bytes | None:
    private_path = _local_private_key_path()
    if not private_path.exists():
        return None
    private_bytes = _base64_decode_key(private_path.read_text(encoding="utf-8"), label="private signing key")
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    except ValueError as exc:
        raise ValidationError("specialist task run receipt attestation private signing key invalid") from exc
    public_bytes = _public_key_raw_bytes(private_key.public_key())
    _write_local_key(_local_public_key_path(), public_bytes)
    return public_bytes


def _local_public_key_bytes() -> bytes:
    public_path = _local_public_key_path()
    if public_path.exists():
        return _base64_decode_key(public_path.read_text(encoding="utf-8"), label="trusted public key")
    derived = _derive_local_public_key_from_private()
    if derived is not None:
        return derived
    raise MissingPathError(f"specialist task run receipt attestation trusted public key not found: {public_path}")


def _private_key() -> Ed25519PrivateKey:
    key_bytes = _key_bytes_from_env_or_path(
        env_name=_PRIVATE_KEY_ENV,
        path_env_name=_PRIVATE_KEY_PATH_ENV,
        label="private signing key",
    ) or _local_private_key_bytes(create=True)
    try:
        return Ed25519PrivateKey.from_private_bytes(key_bytes)
    except ValueError as exc:
        raise ValidationError("specialist task run receipt attestation private signing key invalid") from exc


def _public_key() -> Ed25519PublicKey:
    key_bytes = _key_bytes_from_env_or_path(
        env_name=_PUBLIC_KEY_ENV,
        path_env_name=_PUBLIC_KEY_PATH_ENV,
        label="trusted public key",
    ) or _local_public_key_bytes()
    try:
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError as exc:
        raise ValidationError("specialist task run receipt attestation trusted public key invalid") from exc


def _public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    return _public_key_raw_bytes(public_key)


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    return "sha256:" + hashlib.sha256(_public_key_bytes(public_key)).hexdigest()


def _attestation_signing_payload(payload: JsonObject) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _attestation_signature(payload: JsonObject, private_key: Ed25519PrivateKey) -> str:
    signature = private_key.sign(_attestation_signing_payload(payload))
    return "ed25519:" + base64.urlsafe_b64encode(signature).decode("ascii")


def _signature_bytes(signature: str) -> bytes:
    prefix = "ed25519:"
    if not signature.startswith(prefix):
        raise ValidationError("specialist task run receipt attestation invalid: signature_kind")
    try:
        return base64.urlsafe_b64decode(signature[len(prefix):].encode("ascii"))
    except ValueError as exc:
        raise ValidationError("specialist task run receipt attestation invalid: signature_encoding") from exc


def attach_specialist_task_run_receipt_attestation(payload: JsonObject) -> JsonObject:
    private_key = _private_key()
    public_key = private_key.public_key()
    attested = JsonObjectAdapter.validate_python(dict(payload))
    attested.pop("receipt_attestation", None)
    receipt = _unsigned_receipt_for_attestation(attested)
    attestation_payload = JsonObjectAdapter.validate_python({
        "schema": SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_SCHEMA,
        "attestation_kind": SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_KIND,
        "created_by": SPECIALIST_TASK_RUN_RECEIPT_ATTESTATION_CREATED_BY,
        "receipt_schema": receipt.schema_id,
        "receipt_hash": specialist_task_run_receipt_hash(attested),
        "work_id": receipt.work_id,
        "phase": receipt.phase,
        "harness": receipt.harness.value,
        "adapter": receipt.adapter,
        "key_id": _public_key_id(public_key),
        "nonce": secrets.token_hex(16),
        "issued_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    })
    attestation_payload["signature"] = _attestation_signature(attestation_payload, private_key)
    try:
        attestation = SpecialistTaskRunReceiptAttestation.model_validate(attestation_payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task run receipt attestation invalid") from exc
    attested["receipt_attestation"] = attestation.to_payload()
    return attested


def _validate_receipt_artifact(
    *,
    path_value: str,
    sha_value: str,
    path_field: str,
    sha_field: str,
) -> None:
    if not path_value:
        raise ValidationError(f"specialist task run receipt artifact invalid: {path_field} required")
    if not sha_value:
        raise ValidationError(f"specialist task run receipt artifact invalid: {sha_field} required")
    artifact_path = Path(path_value)
    if not artifact_path.exists():
        raise ValidationError(f"specialist task run receipt artifact invalid: {path_field} not found")
    content = artifact_path.read_bytes()
    if len(content.strip()) <= 2:
        raise ValidationError(f"specialist task run receipt artifact invalid: {path_field} is empty")
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual != sha_value:
        raise ValidationError(f"specialist task run receipt artifact invalid: {sha_field}")


def validate_specialist_task_run_receipt_attestation(
    payload: JsonObject,
    *,
    require_artifacts: bool = True,
) -> None:
    try:
        receipt = SpecialistTaskRunReceipt.from_operation_payload(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task run receipt invalid") from exc
    if receipt.receipt_attestation is None:
        raise ValidationError("specialist task run receipt receipt_attestation required")
    attestation = receipt.receipt_attestation
    raw_attestation = attestation.to_payload()
    expected_hash = specialist_task_run_receipt_hash(payload)
    if attestation.receipt_hash != expected_hash:
        raise ValidationError("specialist task run receipt attestation invalid: receipt_hash")
    if attestation.receipt_schema != receipt.schema_id:
        raise ValidationError("specialist task run receipt attestation invalid: receipt_schema")
    if attestation.work_id != receipt.work_id:
        raise ValidationError("specialist task run receipt attestation invalid: work_id")
    if attestation.phase != receipt.phase:
        raise ValidationError("specialist task run receipt attestation invalid: phase")
    if attestation.harness != receipt.harness:
        raise ValidationError("specialist task run receipt attestation invalid: harness")
    if attestation.adapter != receipt.adapter:
        raise ValidationError("specialist task run receipt attestation invalid: adapter")
    try:
        public_key = _public_key()
    except (MissingPathError, ValidationError) as exc:
        raise ValidationError(f"specialist task run receipt attestation invalid: {exc}") from exc
    if attestation.key_id != _public_key_id(public_key):
        raise ValidationError("specialist task run receipt attestation invalid: key_id")
    try:
        public_key.verify(_signature_bytes(attestation.signature), _attestation_signing_payload(raw_attestation))
    except InvalidSignature as err:
        raise ValidationError("specialist task run receipt attestation invalid: signature") from err
    if require_artifacts and receipt.status == SpecialistRunStatus.COMPLETED:
        _validate_receipt_artifact(
            path_value=receipt.input_packet_path,
            sha_value=receipt.input_packet_sha256,
            path_field="input_packet_path",
            sha_field="input_packet_sha256",
        )
        _validate_receipt_artifact(
            path_value=receipt.transcript_artifact_path,
            sha_value=receipt.transcript_artifact_sha256,
            path_field="transcript_artifact_path",
            sha_field="transcript_artifact_sha256",
        )
