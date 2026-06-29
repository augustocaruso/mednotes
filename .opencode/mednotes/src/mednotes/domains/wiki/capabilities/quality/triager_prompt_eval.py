"""Offline prompt-quality evaluation for med-chat-triager outputs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.notes.note_plan import (
    NOT_A_NOTE_ACTION,
    PLANNED_MEANING_ACTION,
    TRIAGE_NOTE_PLAN_SCHEMA,
    normalize_triage_note_plan,
    note_plan_hash,
    note_plan_summary,
)
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _user_state_dir
from mednotes.kernel.base import JsonObject, JsonObjectAdapter
from mednotes.platform.paths import extension_root as _resolve_extension_root

TRIAGER_PROMPT_EVAL_SCHEMA = "medical-notes-workbench.triager-prompt-eval.v1"
TRIAGER_PROMPT_EXPECTATIONS_SCHEMA = "medical-notes-workbench.triager-prompt-expectations.v1"
SUBAGENT_RUN_RECEIPT_SCHEMA = "medical-notes-workbench.subagent-run-receipt.v1"
SUBAGENT_RUN_RECEIPT_ATTESTATION_SCHEMA = "medical-notes-workbench.subagent-run-receipt-attestation.v1"
SUBAGENT_RUN_RECEIPT_ATTESTATION_KIND = "workbench_ed25519.v1"
SUBAGENT_RUN_RECEIPT_ATTESTATION_CREATED_BY = "mednotes-subagent-runner"
_SUBAGENT_PRIVATE_KEY_ENV = "MEDNOTES_SUBAGENT_RUN_RECEIPT_ATTESTATION_PRIVATE_KEY"
_SUBAGENT_PRIVATE_KEY_PATH_ENV = "MEDNOTES_SUBAGENT_RUN_RECEIPT_ATTESTATION_PRIVATE_KEY_PATH"
_SUBAGENT_PUBLIC_KEY_ENV = "MEDNOTES_SUBAGENT_RUN_RECEIPT_ATTESTATION_PUBLIC_KEY"
_SUBAGENT_PUBLIC_KEY_PATH_ENV = "MEDNOTES_SUBAGENT_RUN_RECEIPT_ATTESTATION_PUBLIC_KEY_PATH"
_SUBAGENT_PUBLIC_KEY_FILENAME = "subagent-run-receipt-attestation.ed25519.public.key"
# Raiz do repositório (pai do bundle/) — independente da profundidade do módulo.
REPO_ROOT = _resolve_extension_root().parent
TRIAGER_EVAL_RETRY_NEXT_ACTION = (
    "reenviar error_context ao med-chat-triager e gerar novo output/eval; "
    "não remendar output JSON, note_plan ou agent_metrics manualmente antes de triage --note-plan"
)


class _SubagentRunReceiptAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["medical-notes-workbench.subagent-run-receipt-attestation.v1"] = Field(alias="schema")
    attestation_kind: Literal["workbench_ed25519.v1"]
    created_by: Literal["mednotes-subagent-runner"]
    receipt_schema: Literal["medical-notes-workbench.subagent-run-receipt.v1"]
    receipt_hash: StrictStr
    agent: StrictStr
    work_item_id: StrictStr
    raw_file_hash: StrictStr
    output_hash: StrictStr
    key_id: StrictStr
    nonce: StrictStr
    issued_at: StrictStr
    signature: StrictStr

    def normalized(self) -> dict[str, str]:
        return {
            "schema": SUBAGENT_RUN_RECEIPT_ATTESTATION_SCHEMA,
            "attestation_kind": self.attestation_kind,
            "created_by": self.created_by,
            "receipt_schema": self.receipt_schema,
            "receipt_hash": self.receipt_hash.strip(),
            "agent": self.agent.strip(),
            "work_item_id": self.work_item_id.strip(),
            "raw_file_hash": self.raw_file_hash.strip(),
            "output_hash": self.output_hash.strip(),
            "key_id": self.key_id.strip(),
            "nonce": self.nonce.strip(),
            "issued_at": self.issued_at.strip(),
            "signature": self.signature.strip(),
        }


def _json_object_from_model(model: BaseModel, **dump_options: Any) -> JsonObject:
    # Contract models are the source of truth; this adapter keeps public JSON
    # payloads serializable without letting arbitrary Python objects leak back
    # into workflow decisions.
    return JsonObjectAdapter.validate_python(model.model_dump(mode="json", by_alias=True, **dump_options))


class _SubagentRunReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["medical-notes-workbench.subagent-run-receipt.v1"] = Field(alias="schema")
    issuer: StrictStr
    agent: StrictStr
    work_item_id: StrictStr
    raw_file: StrictStr
    raw_file_hash: StrictStr
    output_path: StrictStr = ""
    output_hash: StrictStr
    signature: StrictStr = ""
    receipt_attestation: _SubagentRunReceiptAttestation | None = None

    def payload_without_attestation(self) -> JsonObject:
        # Receipt hashes intentionally use only fields that were present in the
        # original receipt so existing runner signatures do not drift when the
        # Pydantic contract supplies defaults.
        return _json_object_from_model(
            self,
            exclude={"receipt_attestation"},
            exclude_unset=True,
        )

    def legacy_signature_payload(self) -> JsonObject:
        return _json_object_from_model(self, exclude={"signature"}, exclude_unset=True)

    def attested_payload(self, attestation: _SubagentRunReceiptAttestation) -> JsonObject:
        payload = self.payload_without_attestation()
        payload["receipt_attestation"] = attestation.normalized()
        return JsonObjectAdapter.validate_python(payload)

    def normalized(self) -> dict[str, str | dict[str, str] | None]:
        return {
            "schema": SUBAGENT_RUN_RECEIPT_SCHEMA,
            "issuer": self.issuer.strip(),
            "agent": self.agent.strip(),
            "work_item_id": self.work_item_id.strip(),
            "raw_file": self.raw_file.strip(),
            "raw_file_hash": self.raw_file_hash.strip(),
            "output_path": self.output_path.strip(),
            "output_hash": self.output_hash.strip(),
            "signature": self.signature.strip(),
            "receipt_attestation": self.receipt_attestation.normalized()
            if self.receipt_attestation is not None
            else None,
        }


class _SubagentRunReceiptStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    valid: bool
    required: bool
    issuer: StrictStr = ""
    agent: StrictStr = ""
    work_item_id: StrictStr = ""
    path: StrictStr = ""
    receipt_hash: StrictStr = ""
    signature_status: StrictStr = "not_present"

    def to_payload(self) -> JsonObject:
        return _json_object_from_model(self)


class _TriagerEvalInputFingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_file: StrictStr = ""
    raw_file_hash: StrictStr = ""
    output_hash: StrictStr = ""
    output_file_hash: StrictStr = ""
    subagent_run_receipt_path: StrictStr = ""
    subagent_run_receipt_hash: StrictStr = ""
    note_plan_hash: StrictStr = ""
    evaluation_expectations_present: bool = False
    evaluation_expectations_hash: StrictStr = ""


class _TriagerEvalAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = 0
    issue_count: int = 0
    error_count: int = 0
    redaction_issue_count: int = 0
    quality_flags: list[StrictStr] = Field(default_factory=list)
    metric_coverage: JsonObject = Field(default_factory=dict)
    subagent_run_receipt_coverage: _SubagentRunReceiptStatus = Field(
        default_factory=lambda: _SubagentRunReceiptStatus(present=False, valid=False, required=False)
    )
    expectation_coverage: JsonObject = Field(default_factory=dict)
    efficiency: JsonObject = Field(default_factory=dict)
    note_plan: JsonObject = Field(default_factory=dict)


class _TriagerPromptEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["medical-notes-workbench.triager-prompt-eval.v1"] = Field(alias="schema")
    phase: StrictStr = ""
    input_fingerprints: _TriagerEvalInputFingerprints
    status: StrictStr
    aggregate: _TriagerEvalAggregate
    issues: list[JsonObject] = Field(default_factory=list)
    agent_metrics: JsonObject = Field(default_factory=dict)
    subagent_run_receipt: _SubagentRunReceiptStatus = Field(
        default_factory=lambda: _SubagentRunReceiptStatus(present=False, valid=False, required=False)
    )
    next_action: StrictStr = ""
    comparison: JsonObject | None = None

    def to_payload(self) -> JsonObject:
        return _json_object_from_model(self, exclude_none=True)


@dataclass(frozen=True)
class _TriagerOutputParts:
    decision: str
    note_plan: JsonObject | None


def canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _receipt_without_attestation(receipt: _SubagentRunReceipt) -> JsonObject:
    return receipt.payload_without_attestation()


def subagent_run_receipt_hash(receipt: _SubagentRunReceipt) -> str:
    encoded = json.dumps(
        _receipt_without_attestation(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _base64_decode_key(raw: str, *, label: str) -> bytes:
    compact = raw.strip()
    if not compact:
        raise ValidationError(f"subagent run receipt attestation {label} required")
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError(f"subagent run receipt attestation {label} must be base64") from exc


def _key_bytes_from_env_or_path(*, env_name: str, path_env_name: str, label: str) -> bytes | None:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return _base64_decode_key(configured, label=label)
    configured_path = os.getenv(path_env_name, "").strip()
    if configured_path:
        key_path = Path(configured_path).expanduser()
        if not key_path.exists():
            raise MissingPathError(f"subagent run receipt attestation {label} not found: {key_path}")
        return _base64_decode_key(key_path.read_text(encoding="utf-8"), label=label)
    return None


def _local_public_key_path() -> Path:
    return _user_state_dir() / _SUBAGENT_PUBLIC_KEY_FILENAME


def _subagent_private_key() -> Ed25519PrivateKey:
    key_bytes = _key_bytes_from_env_or_path(
        env_name=_SUBAGENT_PRIVATE_KEY_ENV,
        path_env_name=_SUBAGENT_PRIVATE_KEY_PATH_ENV,
        label="private signing key",
    )
    if key_bytes is None:
        raise MissingPathError(
            "subagent run receipt attestation private signing key not configured; "
            f"set {_SUBAGENT_PRIVATE_KEY_ENV} or {_SUBAGENT_PRIVATE_KEY_PATH_ENV}"
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(key_bytes)
    except ValueError as exc:
        raise ValidationError("subagent run receipt attestation private signing key invalid") from exc


def _subagent_public_key() -> Ed25519PublicKey:
    key_bytes = _key_bytes_from_env_or_path(
        env_name=_SUBAGENT_PUBLIC_KEY_ENV,
        path_env_name=_SUBAGENT_PUBLIC_KEY_PATH_ENV,
        label="trusted public key",
    )
    if key_bytes is None:
        local_public_key = _local_public_key_path()
        if not local_public_key.exists():
            raise MissingPathError(
                "subagent run receipt attestation trusted public key not configured; "
                f"set {_SUBAGENT_PUBLIC_KEY_ENV} or {_SUBAGENT_PUBLIC_KEY_PATH_ENV}"
            )
        key_bytes = _base64_decode_key(local_public_key.read_text(encoding="utf-8"), label="trusted public key")
    try:
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError as exc:
        raise ValidationError("subagent run receipt attestation trusted public key invalid") from exc


def _public_key_raw_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    return _sha256_bytes(_public_key_raw_bytes(public_key))


def _attestation_signing_payload(payload: JsonObject) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature_bytes(signature: str) -> bytes:
    prefix = "ed25519:"
    if not signature.startswith(prefix):
        raise ValidationError("subagent run receipt attestation invalid: signature_kind")
    try:
        return base64.urlsafe_b64decode(signature[len(prefix):].encode("ascii"))
    except ValueError as exc:
        raise ValidationError("subagent run receipt attestation invalid: signature_encoding") from exc


def attach_subagent_run_receipt_attestation(payload: JsonObject) -> JsonObject:
    try:
        receipt = _SubagentRunReceipt.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"subagent run receipt contract invalid: {exc}") from exc
    private_key = _subagent_private_key()
    public_key = private_key.public_key()
    attestation_payload = {
        "schema": SUBAGENT_RUN_RECEIPT_ATTESTATION_SCHEMA,
        "attestation_kind": SUBAGENT_RUN_RECEIPT_ATTESTATION_KIND,
        "created_by": SUBAGENT_RUN_RECEIPT_ATTESTATION_CREATED_BY,
        "receipt_schema": receipt.schema_,
        "receipt_hash": subagent_run_receipt_hash(receipt),
        "agent": receipt.agent.strip(),
        "work_item_id": receipt.work_item_id.strip(),
        "raw_file_hash": receipt.raw_file_hash.strip(),
        "output_hash": receipt.output_hash.strip(),
        "key_id": _public_key_id(public_key),
        "nonce": secrets.token_hex(16),
        "issued_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    signature = private_key.sign(_attestation_signing_payload(attestation_payload))
    attestation_payload["signature"] = "ed25519:" + base64.urlsafe_b64encode(signature).decode("ascii")
    attestation = _SubagentRunReceiptAttestation.model_validate(attestation_payload)
    return receipt.attested_payload(attestation)


def _validate_subagent_run_receipt_attestation(receipt: _SubagentRunReceipt) -> None:
    if receipt.receipt_attestation is None:
        raise ValidationError("subagent run receipt attestation required")
    attestation = receipt.receipt_attestation
    normalized = attestation.normalized()
    if normalized["receipt_hash"] != subagent_run_receipt_hash(receipt):
        raise ValidationError("subagent run receipt attestation invalid: receipt_hash")
    for field in ("agent", "work_item_id", "raw_file_hash", "output_hash"):
        if normalized[field] != str(getattr(receipt, field)).strip():
            raise ValidationError(f"subagent run receipt attestation invalid: {field}")
    try:
        public_key = _subagent_public_key()
    except (MissingPathError, ValidationError) as exc:
        raise ValidationError(f"subagent run receipt attestation invalid: {exc}") from exc
    if normalized["key_id"] != _public_key_id(public_key):
        raise ValidationError("subagent run receipt attestation invalid: key_id")
    try:
        public_key.verify(_signature_bytes(normalized["signature"]), _attestation_signing_payload(normalized))
    except InvalidSignature as exc:
        raise ValidationError("subagent run receipt attestation invalid: signature") from exc


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


def _issue(*, code: str, severity: str, rubric_key: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "rubric_key": rubric_key, "message": message}


def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def _paths_match(left: str, right: Path) -> bool:
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return str(Path(left)) == str(right)


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


def _score(issues: list[dict[str, str]]) -> int:
    penalty = 0
    for issue in issues:
        penalty += 25 if issue.get("severity") == "error" else 10
    return max(0, 100 - penalty)


def _agent_metrics(payload: dict[str, Any]) -> dict[str, Any] | None:
    metrics = payload.get("agent_metrics")
    return metrics if isinstance(metrics, dict) else None


def _is_repo_root_artifact(path: Path) -> bool:
    try:
        return path.resolve().parent == REPO_ROOT.resolve()
    except OSError:
        return path.parent == REPO_ROOT


def subagent_run_receipt_signature_payload(receipt: _SubagentRunReceipt) -> JsonObject:
    return receipt.legacy_signature_payload()


def subagent_run_receipt_signature(receipt: _SubagentRunReceipt, *, signing_key: str) -> str:
    encoded = json.dumps(
        subagent_run_receipt_signature_payload(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(signing_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _subagent_run_receipt_issues(
    *,
    receipt_path: Path | None,
    raw_file: Path,
    output_path: Path,
    require_subagent_run_receipt: bool,
    signing_key: str = "",
) -> tuple[list[dict[str, str]], _SubagentRunReceiptStatus]:
    severity = "error" if require_subagent_run_receipt else "warning"
    if receipt_path is None:
        if not require_subagent_run_receipt:
            return [], _SubagentRunReceiptStatus(present=False, valid=False, required=False)
        return [
            _issue(
                code="missing_subagent_run_receipt",
                severity=severity,
                rubric_key="agent_output_provenance",
                message=(
                    "subagent_run_receipt is required; rerun the packaged med-chat-triager "
                    "through the official runner and do not fabricate or patch output in the parent."
                ),
            )
        ], _SubagentRunReceiptStatus(present=False, valid=False, required=True)
    try:
        raw_receipt = _read_json_object(receipt_path, label="subagent run receipt")
    except ValidationError as exc:
        return [
            _issue(
                code="subagent_run_receipt_invalid",
                severity="error",
                rubric_key="agent_output_provenance",
                message=str(exc),
            )
        ], _SubagentRunReceiptStatus(present=True, valid=False, required=require_subagent_run_receipt)
    issues: list[dict[str, str]] = []
    try:
        receipt = _SubagentRunReceipt.model_validate(raw_receipt)
    except PydanticValidationError as exc:
        return [
            _issue(
                code="subagent_run_receipt_invalid",
                severity="error",
                rubric_key="agent_output_provenance",
                message=f"subagent_run_receipt contract invalid: {exc}",
            )
        ], _SubagentRunReceiptStatus(present=True, valid=False, required=require_subagent_run_receipt)
    normalized = receipt.normalized()
    issuer = str(normalized["issuer"])
    agent = str(normalized["agent"])
    work_item_id = str(normalized["work_item_id"])
    raw_file_value = str(normalized["raw_file"])
    raw_file_hash = str(normalized["raw_file_hash"])
    output_path_value = str(normalized["output_path"])
    output_hash = str(normalized["output_hash"])
    legacy_signature = str(normalized["signature"])
    if issuer != "mednotes-subagent-runner":
        issues.append(
            _issue(
                code="subagent_run_receipt_wrong_issuer",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.issuer must be mednotes-subagent-runner.",
            )
        )
    if agent != "med-chat-triager":
        issues.append(
            _issue(
                code="subagent_run_receipt_wrong_agent",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.agent must be med-chat-triager.",
            )
        )
    for field in ("work_item_id", "raw_file_hash", "output_hash"):
        if not normalized[field]:
            issues.append(
                _issue(
                    code=f"subagent_run_receipt_{field}_missing",
                    severity="error",
                    rubric_key="agent_output_provenance",
                    message=f"subagent_run_receipt.{field} must be non-empty.",
                )
            )
    if not raw_file_value or not _paths_match(raw_file_value, raw_file):
        issues.append(
            _issue(
                code="subagent_run_receipt_raw_file_mismatch",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.raw_file does not match the assigned raw_file.",
            )
        )
    if output_path_value and not _paths_match(output_path_value, output_path):
        issues.append(
            _issue(
                code="subagent_run_receipt_output_path_mismatch",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.output_path does not match the evaluated output path.",
            )
        )
    actual_raw_hash = _file_sha256(raw_file)
    if raw_file_hash and raw_file_hash != actual_raw_hash:
        issues.append(
            _issue(
                code="subagent_run_receipt_raw_hash_mismatch",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.raw_file_hash is stale for the assigned raw_file.",
            )
        )
    actual_output_hash = _file_sha256(output_path)
    if output_hash and output_hash != actual_output_hash:
        issues.append(
            _issue(
                code="subagent_run_receipt_output_hash_mismatch",
                severity="error",
                rubric_key="agent_output_provenance",
                message="subagent_run_receipt.output_hash is stale for the evaluated output.",
            )
        )
    signature_status = "not_present"
    if legacy_signature:
        if not signing_key:
            issues.append(
                _issue(
                    code="subagent_run_receipt_signature_unverifiable",
                    severity="error",
                    rubric_key="agent_output_provenance",
                    message="subagent_run_receipt has a signature but no runner signing key was provided for verification.",
                )
            )
            signature_status = "unverifiable"
        else:
            expected = subagent_run_receipt_signature(receipt, signing_key=signing_key)
            if not hmac.compare_digest(legacy_signature, expected):
                issues.append(
                    _issue(
                        code="subagent_run_receipt_signature_invalid",
                        severity="error",
                        rubric_key="agent_output_provenance",
                        message="subagent_run_receipt.signature does not match the runner-issued payload.",
                    )
                )
                signature_status = "invalid"
            else:
                signature_status = "valid"
    if require_subagent_run_receipt:
        try:
            _validate_subagent_run_receipt_attestation(receipt)
            signature_status = "valid"
        except ValidationError as exc:
            message = str(exc)
            if "attestation required" in message:
                code = "subagent_run_receipt_signature_required"
                signature_status = "missing"
                message = (
                    "subagent_run_receipt requires runner Ed25519 attestation when it authorizes "
                    "mutating process-chats triage."
                )
            elif "trusted public key" in message:
                code = "subagent_run_receipt_signature_unverifiable"
                signature_status = "unverifiable"
            else:
                code = "subagent_run_receipt_signature_invalid"
                signature_status = "invalid"
            issues.append(
                _issue(
                    code=code,
                    severity="error",
                    rubric_key="agent_output_provenance",
                    message=message,
                )
            )
    return issues, _SubagentRunReceiptStatus(
        present=True,
        valid=not issues,
        required=require_subagent_run_receipt,
        issuer=issuer,
        agent=agent,
        work_item_id=work_item_id,
        path=str(receipt_path),
        receipt_hash=_file_sha256(receipt_path),
        signature_status=signature_status,
    )


def _metrics_issues(
    payload: dict[str, Any],
    *,
    require_agent_metrics: bool,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    severity = "error" if require_agent_metrics else "warning"
    metrics = _agent_metrics(payload)
    if metrics is None:
        return [
            _issue(
                code="missing_agent_metrics",
                severity=severity,
                rubric_key="efficiency_routing",
                message=(
                    "agent_metrics is unavailable; note_plan can still be evaluated, "
                    "but runtime efficiency cannot be trusted."
                ),
            )
        ], {"present": False, "valid": False}

    metrics_dict = metrics
    token_accounting = str(metrics_dict.get("token_accounting") or "")
    issues: list[dict[str, str]] = []

    def required_int(field: str, *, minimum: int, code: str) -> int:
        value = metrics_dict.get(field)
        if isinstance(value, bool) or value is None:
            issues.append(
                _issue(
                    code=code,
                    severity=severity,
                    rubric_key="efficiency_routing",
                    message=f"agent_metrics.{field} is required and must be an integer >= {minimum}.",
                )
            )
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            issues.append(
                _issue(
                    code=code,
                    severity=severity,
                    rubric_key="efficiency_routing",
                    message=f"agent_metrics.{field} must be an integer >= {minimum}.",
                )
            )
            return 0
        if parsed < minimum:
            issues.append(
                _issue(
                    code=code,
                    severity=severity,
                    rubric_key="efficiency_routing",
                    message=f"agent_metrics.{field} must be >= {minimum}.",
                )
            )
        return parsed

    if token_accounting not in {"exact", "estimated", "unavailable"}:
        issues.append(
            _issue(
                code="agent_metrics_token_accounting_missing",
                severity=severity,
                rubric_key="efficiency_routing",
                message="agent_metrics.token_accounting must be exact, estimated, or unavailable.",
            )
        )
    turns_used = required_int("turns_used", minimum=1, code="agent_metrics_turns_used_missing")
    retries = required_int("retries", minimum=0, code="agent_metrics_retries_missing")
    if token_accounting in {"exact", "estimated"}:
        prompt_tokens = required_int("prompt_tokens", minimum=1, code="agent_metrics_prompt_tokens_missing")
        completion_tokens = required_int(
            "completion_tokens",
            minimum=1,
            code="agent_metrics_completion_tokens_missing",
        )
    else:
        prompt_tokens = int(metrics.get("prompt_tokens") or 0)
        completion_tokens = int(metrics.get("completion_tokens") or 0)
    if turns_used > 12:
        issues.append(
            _issue(
                code="turn_budget_exceeded",
                severity="warning",
                rubric_key="efficiency_routing",
                message=f"turns_used={turns_used} exceeds triager max_turns=12.",
            )
        )
    return issues, {
        "present": True,
        "valid": not issues,
        "token_accounting": token_accounting,
        "turns_used": turns_used,
        "max_turns": 12,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "retries": retries,
    }


def _output_parts(payload: JsonObject) -> _TriagerOutputParts:
    if payload["schema"] == TRIAGE_NOTE_PLAN_SCHEMA if "schema" in payload else False:
        return _TriagerOutputParts(decision="triage", note_plan=payload)
    decision_value = payload["decision"] if "decision" in payload else ""
    note_plan_value = payload["note_plan"] if "note_plan" in payload else None
    note_plan = JsonObjectAdapter.validate_python(note_plan_value) if isinstance(note_plan_value, dict) else None
    return _TriagerOutputParts(decision=str(decision_value or "").strip(), note_plan=note_plan)


def _titles_for_action(plan: dict[str, Any], action: str, *, field: str = "title") -> set[str]:
    titles: set[str] = set()
    for item in plan.get("items", []):
        if not isinstance(item, dict) or item.get("action") != action:
            continue
        value = item.get(field) if field in item else item.get("staged_title") or item.get("title")
        if str(value or "").strip():
            titles.add(_norm_text(value))
    return titles


def _expectation_issues(
    *,
    expectations: dict[str, Any],
    decision: str,
    normalized_plan: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not expectations:
        return []

    issues: list[dict[str, str]] = []
    unsupported_keys = [
        key
        for key in (
            "required_create_titles",
            "forbidden_create_titles",
            "required_covered_existing_titles",
        )
        if key in expectations
    ]
    if unsupported_keys:
        issues.append(
            _issue(
                code="unsupported_triager_expectation_key",
                severity="error",
                rubric_key="golden_expectations",
                message="unsupported triager expectation keys: " + ", ".join(unsupported_keys),
            )
        )
    expected_decision = str(expectations.get("expected_decision") or "").strip()
    if expected_decision and decision != expected_decision:
        issues.append(
            _issue(
                code="expected_decision_mismatch",
                severity="error",
                rubric_key="golden_expectations",
                message="decision does not match triager expectations.",
            )
        )

    if normalized_plan is None:
        if any(
            isinstance(expectations.get(key), list)
            for key in (
                "required_planned_meaning_titles",
                "forbidden_planned_meaning_titles",
                "required_not_a_note_titles",
            )
        ):
            issues.append(
                _issue(
                    code="expected_note_plan_absent",
                    severity="error",
                    rubric_key="golden_expectations",
                    message="triager expectations require note_plan, but no valid plan was available.",
                )
            )
        return issues

    planned_titles = _titles_for_action(normalized_plan, PLANNED_MEANING_ACTION)
    not_a_note_titles = _titles_for_action(normalized_plan, NOT_A_NOTE_ACTION)

    required_planned = list(expectations.get("required_planned_meaning_titles") or [])
    forbidden_planned = list(expectations.get("forbidden_planned_meaning_titles") or [])

    for title in required_planned:
        if _norm_text(title) not in planned_titles:
            issues.append(
                _issue(
                    code="missing_required_planned_meaning_title",
                    severity="error",
                    rubric_key="golden_expectations",
                    message=f"required planned_meaning title absent: {title}",
                )
            )
    for title in forbidden_planned:
        if _norm_text(title) in planned_titles:
            issues.append(
                _issue(
                    code="forbidden_planned_meaning_title",
                    severity="error",
                    rubric_key="golden_expectations",
                    message=f"forbidden planned_meaning title present: {title}",
                )
            )
    for title in expectations.get("required_not_a_note_titles") or []:
        if _norm_text(title) not in not_a_note_titles:
            issues.append(
                _issue(
                    code="missing_required_not_a_note_title",
                    severity="error",
                    rubric_key="golden_expectations",
                    message=f"required not_a_note title absent: {title}",
                )
            )
    return issues


def _aggregate_efficiency(report: dict[str, Any]) -> dict[str, Any]:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
    return aggregate.get("efficiency") if isinstance(aggregate.get("efficiency"), dict) else {}


def _input_fingerprints(report: dict[str, Any]) -> dict[str, Any]:
    fingerprints = report.get("input_fingerprints")
    return fingerprints if isinstance(fingerprints, dict) else {}


def _compare_to_baseline(*, current: dict[str, Any], baseline_path: Path) -> dict[str, Any]:
    baseline = _read_json_object(baseline_path, label="triager prompt eval baseline")
    if baseline.get("schema") != TRIAGER_PROMPT_EVAL_SCHEMA:
        raise ValidationError(f"triager prompt eval baseline must use schema {TRIAGER_PROMPT_EVAL_SCHEMA}")
    current_aggregate = current.get("aggregate") if isinstance(current.get("aggregate"), dict) else {}
    baseline_aggregate = baseline.get("aggregate") if isinstance(baseline.get("aggregate"), dict) else {}
    current_efficiency = _aggregate_efficiency(current)
    baseline_efficiency = _aggregate_efficiency(baseline)
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
    }
    comparability_flags: list[str] = []
    current_expectations_present = bool(current_fingerprints.get("evaluation_expectations_present"))
    baseline_expectations_present = bool(baseline_fingerprints.get("evaluation_expectations_present"))
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
    comparison["comparability_flags"] = comparability_flags
    comparison["regression_flags"] = regression_flags
    if comparability_flags:
        comparison["status"] = "not_comparable"
    else:
        comparison["status"] = "regressed" if regression_flags else "improved_or_equal"
    return comparison


def load_triager_prompt_expectations(path: Path) -> dict[str, Any]:
    payload = _read_json_object(path, label="triager prompt expectations")
    if payload.get("schema") != TRIAGER_PROMPT_EXPECTATIONS_SCHEMA:
        raise ValidationError(f"triager prompt expectations must use schema {TRIAGER_PROMPT_EXPECTATIONS_SCHEMA}")
    expectations = payload.get("expectations")
    if not isinstance(expectations, dict):
        raise ValidationError("triager prompt expectations require expectations object")
    return expectations


def evaluate_triager_prompt_output(
    *,
    raw_file: Path,
    output_path: Path,
    expectations: dict[str, Any] | None = None,
    baseline_eval_path: Path | None = None,
    require_agent_metrics: bool = False,
    subagent_run_receipt_path: Path | None = None,
    require_subagent_run_receipt: bool = False,
    subagent_runner_signing_key: str = "",
) -> dict[str, Any]:
    output = _read_json_object(output_path, label="triager output")
    expectations = expectations or {}
    issues: list[dict[str, str]] = []

    if _is_repo_root_artifact(output_path):
        issues.append(
            _issue(
                code="agent_artifact_written_to_repo_root",
                severity="error",
                rubric_key="artifact_hygiene",
                message=(
                    "triager output was written directly in the workbench repo root; "
                    "use the work_item artifact path from plan-subagents."
                ),
            )
        )

    for key_path in _forbidden_key_hits(
        output,
        {"raw_markdown", "clinical_body", "html", "images", "embeddings", "api_keys"},
    ):
        issues.append(
            _issue(
                code="forbidden_output_key",
                severity="error",
                rubric_key="evidence_redaction",
                message=f"output contains forbidden key {key_path}",
            )
        )

    output_raw_file = str(output.get("raw_file") or "")
    if not output_raw_file:
        issues.append(
            _issue(
                code="missing_raw_file",
                severity="error",
                rubric_key="scope_control",
                message="triager output must include raw_file.",
            )
        )
    elif not _paths_match(output_raw_file, raw_file):
        issues.append(
            _issue(
                code="raw_file_mismatch",
                severity="error",
                rubric_key="scope_control",
                message="triager output raw_file differs from assigned raw_file.",
            )
        )

    output_parts = _output_parts(output)
    decision = output_parts.decision
    note_plan = output_parts.note_plan
    normalized_plan: dict[str, Any] | None = None
    if decision not in {"triage", "discard"}:
        issues.append(
            _issue(
                code="invalid_decision",
                severity="error",
                rubric_key="output_contract",
                message="triager decision must be triage or discard.",
            )
        )
    elif decision == "triage":
        if note_plan is None:
            issues.append(
                _issue(
                    code="missing_note_plan",
                    severity="error",
                    rubric_key="output_contract",
                    message="triage decision requires note_plan.",
                )
            )
        else:
            try:
                normalized_plan = normalize_triage_note_plan(note_plan, raw_file)
            except ValidationError as exc:
                issues.append(
                    _issue(
                        code="note_plan_invalid",
                        severity="error",
                        rubric_key="output_contract",
                        message=str(exc),
                    )
                )
    elif not str(output.get("reason") or "").strip():
        issues.append(
            _issue(
                code="missing_discard_reason",
                severity="error",
                rubric_key="output_contract",
                message="discard decision requires reason.",
            )
        )

    metric_issues, metrics = _metrics_issues(output, require_agent_metrics=require_agent_metrics)
    issues.extend(metric_issues)
    receipt_issues, run_receipt = _subagent_run_receipt_issues(
        receipt_path=subagent_run_receipt_path,
        raw_file=raw_file,
        output_path=output_path,
        require_subagent_run_receipt=require_subagent_run_receipt,
        signing_key=subagent_runner_signing_key,
    )
    issues.extend(receipt_issues)
    expectation_issues = _expectation_issues(
        expectations=expectations,
        decision=decision,
        normalized_plan=normalized_plan,
    )
    issues.extend(expectation_issues)

    issue_count = len(issues)
    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    failed_expectation_count = len(expectation_issues)
    quality_flags = []
    if not metrics.get("present") or not metrics.get("valid"):
        quality_flags.append("metric_coverage_incomplete")
    if receipt_issues or (require_subagent_run_receipt and not run_receipt.valid):
        quality_flags.append("agent_output_provenance_incomplete")
    if any(issue.get("rubric_key") == "artifact_hygiene" for issue in issues):
        quality_flags.append("agent_artifact_path_invalid")
    if failed_expectation_count:
        quality_flags.append("golden_expectation_failed")

    report = {
        "schema": TRIAGER_PROMPT_EVAL_SCHEMA,
        "phase": "triage",
        "input_fingerprints": {
            "raw_file": str(raw_file),
            "raw_file_hash": _file_sha256(raw_file),
            "output_hash": canonical_payload_hash(output),
            "output_file_hash": _file_sha256(output_path),
            "subagent_run_receipt_path": str(subagent_run_receipt_path) if subagent_run_receipt_path else "",
            "subagent_run_receipt_hash": run_receipt.receipt_hash,
            "note_plan_hash": note_plan_hash(normalized_plan) if normalized_plan else "",
            "evaluation_expectations_present": bool(expectations),
            "evaluation_expectations_hash": canonical_payload_hash(expectations) if expectations else "",
        },
        "status": "pass" if error_count == 0 else "needs_review",
        "aggregate": {
            "score": _score(issues),
            "issue_count": issue_count,
            "error_count": error_count,
            "redaction_issue_count": sum(1 for issue in issues if issue.get("rubric_key") == "evidence_redaction"),
            "quality_flags": quality_flags,
            "metric_coverage": {
                "items_with_agent_metrics": 1 if metrics.get("present") and metrics.get("valid") else 0,
                "items_total": 1,
                "status": "complete" if metrics.get("present") and metrics.get("valid") else "incomplete",
            },
            "subagent_run_receipt_coverage": {
                "present": run_receipt.present,
                "valid": run_receipt.valid,
                "required": run_receipt.required,
                "signature_status": run_receipt.signature_status,
            },
            "expectation_coverage": {
                "items_with_expectations": 1 if expectations else 0,
                "items_total": 1,
                "failed_expectation_count": failed_expectation_count,
            },
            "efficiency": {
                "total_prompt_tokens": int(metrics.get("prompt_tokens") or 0),
                "total_completion_tokens": int(metrics.get("completion_tokens") or 0),
                "total_retries": int(metrics.get("retries") or 0),
                "turns_used": int(metrics.get("turns_used") or 0),
                "turn_budget_exceeded_count": sum(1 for issue in issues if issue.get("code") == "turn_budget_exceeded"),
            },
            "note_plan": note_plan_summary(normalized_plan)
            if normalized_plan
            else {
                "note_plan_item_count": 0,
                "note_plan_planned_meaning_count": 0,
                "note_plan_attach_count": 0,
                "note_plan_not_a_note_count": 0,
                "note_plan_needs_context_count": 0,
            },
        },
        "issues": issues,
        "agent_metrics": metrics,
        "subagent_run_receipt": run_receipt.to_payload(),
        "next_action": "" if error_count == 0 else TRIAGER_EVAL_RETRY_NEXT_ACTION,
    }
    if baseline_eval_path is not None:
        comparison = _compare_to_baseline(current=report, baseline_path=baseline_eval_path)
        report["comparison"] = comparison
        if comparison.get("status") == "not_comparable":
            report["aggregate"]["quality_flags"].append("baseline_not_comparable")
            report["status"] = "needs_review"
            report["next_action"] = "usar o mesmo corpus de ouro antes de comparar triager prompt baselines"
        elif comparison.get("status") == "regressed":
            report["aggregate"]["quality_flags"].append("baseline_regression")
            report["status"] = "needs_review"
            report["next_action"] = "revisar regressao contra baseline antes de triage --note-plan"
    return report


def validate_triager_prompt_eval_for_note_plan(
    *,
    eval_path: Path,
    raw_file: Path,
    note_plan: dict[str, Any],
    require_subagent_run_receipt: bool = True,
) -> dict[str, Any]:
    """Validate that a triager eval report approves this exact raw/note_plan."""

    raw_report = _read_json_object(eval_path, label="triager prompt eval")
    try:
        report_model = _TriagerPromptEvalReport.model_validate(raw_report)
    except PydanticValidationError as exc:
        raise ValidationError(
            "triager_eval_invalid: triager eval report contract invalid; regenerate with eval-triager-output"
        ) from exc
    if report_model.schema_ != TRIAGER_PROMPT_EVAL_SCHEMA:
        raise ValidationError(
            f"triager_eval_invalid: triager eval report must use schema {TRIAGER_PROMPT_EVAL_SCHEMA}"
        )
    if report_model.status != "pass":
        raise ValidationError(
            f"triager_eval_failed: eval-triager-output did not pass; {TRIAGER_EVAL_RETRY_NEXT_ACTION}"
        )
    fingerprints = report_model.input_fingerprints
    report_raw_file = fingerprints.raw_file
    if not report_raw_file or not _paths_match(report_raw_file, raw_file):
        raise ValidationError(
            "triager_eval_stale: triager eval raw_file does not match --raw-file; regenerate eval-triager-output"
        )
    report_raw_hash = fingerprints.raw_file_hash
    if report_raw_hash and report_raw_hash != _file_sha256(raw_file):
        raise ValidationError(
            "triager_eval_stale: raw chat changed after eval-triager-output; regenerate triager output/eval"
        )
    normalized_plan = normalize_triage_note_plan(note_plan, raw_file)
    expected_hash = note_plan_hash(normalized_plan)
    report_plan_hash = fingerprints.note_plan_hash
    if not report_plan_hash:
        raise ValidationError(
            "triager_eval_stale: triager eval report missing note_plan_hash; regenerate eval-triager-output"
        )
    if report_plan_hash != expected_hash:
        raise ValidationError(
            "triager_eval_stale: triager eval note_plan_hash does not match --note-plan; "
            "regenerar eval-triager-output para o note_plan atual antes de triage"
        )
    if require_subagent_run_receipt:
        receipt_coverage = report_model.aggregate.subagent_run_receipt_coverage
        if receipt_coverage.valid is not True:
            raise ValidationError(
                "triager_eval_missing_subagent_run_receipt: triage mutante exige "
                "subagent_run_receipt válido emitido pelo runner oficial; rerun med-chat-triager "
                "pela rota oficial e repita eval-triager-output com --subagent-run-receipt."
            )
        receipt_path_text = fingerprints.subagent_run_receipt_path.strip()
        if not receipt_path_text:
            raise ValidationError(
                "triager_eval_invalid: triager eval claims subagent_run_receipt coverage but "
                "does not point to subagent_run_receipt_path; regenerate eval-triager-output with "
                "--subagent-run-receipt."
            )
        receipt_path = Path(receipt_path_text)
        report_receipt_hash = fingerprints.subagent_run_receipt_hash.strip()
        if not report_receipt_hash:
            raise ValidationError(
                "triager_eval_invalid: triager eval missing subagent_run_receipt_hash; "
                "regenerate eval-triager-output with --subagent-run-receipt."
            )
        try:
            actual_receipt_hash = _file_sha256(receipt_path)
        except FileNotFoundError as exc:
            raise ValidationError(
                "triager_eval_invalid: subagent_run_receipt_path not found; "
                "regenerate eval-triager-output with the official signed receipt."
            ) from exc
        if report_receipt_hash != actual_receipt_hash:
            raise ValidationError(
                "triager_eval_stale: subagent_run_receipt changed after eval-triager-output; "
                "rerun med-chat-triager/eval through the official runner."
            )
        try:
            receipt_payload = _read_json_object(receipt_path, label="subagent run receipt")
            receipt = _SubagentRunReceipt.model_validate(receipt_payload)
        except (ValidationError, PydanticValidationError) as exc:
            raise ValidationError(
                "triager_eval_invalid: subagent_run_receipt contract invalid; "
                "regenerate eval-triager-output with an official signed subagent-run-receipt."
            ) from exc
        output_path_text = receipt.output_path.strip()
        if not output_path_text:
            raise ValidationError(
                "triager_eval_invalid: subagent_run_receipt missing output_path; "
                "regenerate the triager output through the official runner."
            )
        output_path = Path(output_path_text)
        if not output_path.exists():
            raise ValidationError(
                "triager_eval_invalid: signed subagent_run_receipt points to missing triager output; "
                "regenerate the triager output through the official runner."
            )
        receipt_issues, receipt_status = _subagent_run_receipt_issues(
            receipt_path=receipt_path,
            raw_file=raw_file,
            output_path=output_path,
            require_subagent_run_receipt=True,
        )
        if receipt_issues or receipt_status.valid is not True:
            issue_codes = ", ".join(issue.get("code", "unknown") for issue in receipt_issues)
            raise ValidationError(
                "triager_eval_invalid: subagent_run_receipt failed signed-chain validation"
                + (f" ({issue_codes})" if issue_codes else "")
                + "; regenerate via the official med-chat-triager runner."
            )
        report_output_file_hash = fingerprints.output_file_hash.strip()
        if not report_output_file_hash:
            raise ValidationError(
                "triager_eval_invalid: triager eval missing output_file_hash; regenerate eval-triager-output."
            )
        actual_output_file_hash = _file_sha256(output_path)
        if report_output_file_hash != actual_output_file_hash:
            raise ValidationError(
                "triager_eval_stale: triager output changed after eval-triager-output; regenerate eval."
            )
        output_payload = _read_json_object(output_path, label="triager output")
        report_output_hash = fingerprints.output_hash.strip()
        if not report_output_hash:
            raise ValidationError(
                "triager_eval_invalid: triager eval missing output_hash; regenerate eval-triager-output."
            )
        if report_output_hash != canonical_payload_hash(output_payload):
            raise ValidationError(
                "triager_eval_stale: triager output payload changed after eval-triager-output; regenerate eval."
            )
        output_parts = _output_parts(output_payload)
        if output_parts.decision != "triage" or output_parts.note_plan is None:
            raise ValidationError(
                "triager_eval_invalid: signed triager output does not contain a triage note_plan."
            )
        output_plan = normalize_triage_note_plan(output_parts.note_plan, raw_file)
        if note_plan_hash(output_plan) != expected_hash:
            raise ValidationError(
                "triager_eval_stale: --note-plan does not match the signed med-chat-triager output; "
                "do not patch note_plan manually."
            )
    return report_model.to_payload()
