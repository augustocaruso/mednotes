"""Workbench attestation for subagent plans."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _user_state_dir
from mednotes.domains.wiki.contracts.agents import SubagentBatchPlan, SubagentPlanAttestation
from mednotes.kernel.base import contract_error

SUBAGENT_PLAN_ATTESTATION_SCHEMA = "medical-notes-workbench.subagent-plan-attestation.v1"
SUBAGENT_PLAN_ATTESTATION_KIND = "workbench_hmac_sha256.v1"


def canonical_subagent_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "plan_attestation"}


def subagent_plan_hash(payload: dict[str, Any]) -> str:
    unsigned = canonical_subagent_plan_payload(payload)
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _subagent_plan_attestation_key_path() -> Path:
    configured = os.getenv("MEDNOTES_SUBAGENT_PLAN_ATTESTATION_KEY_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _user_state_dir() / "subagent-plan-attestation.key"


def _subagent_plan_attestation_key(*, create: bool) -> bytes:
    configured = os.getenv("MEDNOTES_SUBAGENT_PLAN_ATTESTATION_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")
    key_path = _subagent_plan_attestation_key_path()
    if key_path.exists():
        return key_path.read_bytes().strip()
    if not create:
        raise MissingPathError(f"subagent plan attestation key not found: {key_path}")
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


def _attestation_signing_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _attestation_signature(payload: dict[str, Any], *, create_key: bool) -> str:
    digest = hmac.new(
        _subagent_plan_attestation_key(create=create_key),
        _attestation_signing_payload(payload),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _verify_attestation_signature(payload: dict[str, Any]) -> bool:
    try:
        expected = _attestation_signature(payload, create_key=False)
    except MissingPathError:
        return False
    return hmac.compare_digest(str(payload.get("signature") or ""), expected)


def _typed_subagent_plan_for_attestation(payload: dict[str, Any]) -> SubagentBatchPlan:
    """Validate the full plan before its identity fields participate in signing."""

    try:
        return SubagentBatchPlan.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="subagent plan attestation payload invalid") from exc


def build_subagent_plan_attestation(payload: dict[str, Any]) -> dict[str, Any]:
    plan = _typed_subagent_plan_for_attestation(payload)
    attestation_payload: dict[str, Any] = {
        "schema": SUBAGENT_PLAN_ATTESTATION_SCHEMA,
        "phase": plan.phase,
        "plan_schema": plan.schema_,
        "plan_hash": subagent_plan_hash(payload),
        "attestation_kind": SUBAGENT_PLAN_ATTESTATION_KIND,
        "created_by": "plan-subagents",
        "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "nonce": secrets.token_hex(16),
    }
    attestation_payload["signature"] = _attestation_signature(attestation_payload, create_key=True)
    try:
        attestation = SubagentPlanAttestation.model_validate(attestation_payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="subagent plan attestation invalid") from exc
    return attestation.model_dump(mode="json", by_alias=True)


def attach_subagent_plan_attestation(payload: dict[str, Any]) -> dict[str, Any]:
    attested = canonical_subagent_plan_payload(payload)
    attested["plan_attestation"] = build_subagent_plan_attestation(attested)
    return attested


def validate_subagent_plan_attestation(payload: dict[str, Any]) -> str:
    raw_attestation = payload.get("plan_attestation")
    if isinstance(raw_attestation, dict):
        expected_hash = subagent_plan_hash(payload)
        if str(raw_attestation.get("plan_hash") or "") != expected_hash:
            raise ValidationError("subagent plan attestation invalid: plan_hash")
    plan = _typed_subagent_plan_for_attestation(payload)
    if plan.plan_attestation is None:
        raise ValidationError("subagent plan attestation required")
    attestation = plan.plan_attestation
    expected_hash = subagent_plan_hash(payload)
    if attestation.plan_hash != expected_hash:
        raise ValidationError("subagent plan attestation invalid: plan_hash")
    if attestation.phase != plan.phase:
        raise ValidationError("subagent plan attestation invalid: phase")
    if attestation.plan_schema != plan.schema_:
        raise ValidationError("subagent plan attestation invalid: plan_schema")
    if attestation.attestation_kind != SUBAGENT_PLAN_ATTESTATION_KIND:
        raise ValidationError("subagent plan attestation invalid: attestation_kind")
    if not _verify_attestation_signature(attestation.to_payload()):
        raise ValidationError("subagent plan attestation invalid: signature")
    return expected_hash


def subagent_plan_attestation_blocked_reason(exc: Exception) -> str:
    return (
        "subagent_plan_attestation_required"
        if "attestation required" in str(exc).lower()
        else "subagent_plan_attestation_invalid"
    )
