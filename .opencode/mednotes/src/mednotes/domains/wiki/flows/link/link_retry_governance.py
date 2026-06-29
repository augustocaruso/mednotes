"""Retry governance for expensive linker diagnosis runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from mednotes.domains.wiki.batch_state import canonical_json_hash
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import _now_iso, wiki_cli_command
from mednotes.domains.wiki.flows.link.link_git import LINK_STATE_SCHEMA_V2, default_link_state_path, load_link_state
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

REDUNDANT_DIAGNOSIS_REASON = "redundant_diagnosis_without_state_change"
FORCE_DIAGNOSE_EVENT_CODE = "linker.force_diagnose_override"

_IDENTITY_FIELDS = (
    "snapshot_hash",
    "git_status_hash",
    "vocabulary_db_hash",
    "related_notes_export_hash",
    "diagnosis_options_hash",
    "trigger_context_hash",
)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


class _LooseRetryInput(ContractModel):
    """Typed adapter for persisted retry state and diagnosis JSON."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)


class DiagnosisIdentity(ContractModel):
    """Stable fingerprint fields that decide whether a failed diagnosis is redundant."""

    snapshot_hash: str = ""
    git_status_hash: str = ""
    git_head: str = ""
    vocabulary_db_hash: str = ""
    related_notes_export_hash: str = ""
    diagnosis_options_hash: str = ""
    trigger_context_hash: str = ""

    def field_value(self, field: str) -> str:
        match field:
            case "snapshot_hash":
                return self.snapshot_hash
            case "git_status_hash":
                return self.git_status_hash
            case "vocabulary_db_hash":
                return self.vocabulary_db_hash
            case "related_notes_export_hash":
                return self.related_notes_export_hash
            case "diagnosis_options_hash":
                return self.diagnosis_options_hash
            case "trigger_context_hash":
                return self.trigger_context_hash
            case _:
                return ""

    def compact_payload(self) -> JsonObject:
        return self.to_payload()


class _SnapshotIdentityFields(_LooseRetryInput):
    snapshot_hash: str = ""
    vocabulary_db_hash: str = ""
    related_notes_export_hash: str = ""

    @field_validator("snapshot_hash", "vocabulary_db_hash", "related_notes_export_hash", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)


class _GitIdentityFields(_LooseRetryInput):
    status_hash: str = ""
    head: str = ""

    @field_validator("status_hash", "head", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)


class _DiagnosisBlockerFingerprint(_LooseRetryInput):
    code: str = ""
    blocking_action_count: int = 0
    human_decision_count: int = 0

    @field_validator("code", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)

    @field_validator("blocking_action_count", "human_decision_count", mode="before")
    @classmethod
    def _coerce_count(cls, value: object) -> int:
        return _as_int(value)

    def compact_payload(self) -> JsonObject:
        return self.to_payload()


class _DiagnosisPayloadFields(_LooseRetryInput):
    status: str = ""
    blocked_reason: str = ""
    next_action: str = ""
    diagnosis_path: str = ""
    returncode: int = 3
    blockers: list[_DiagnosisBlockerFingerprint] = Field(default_factory=list)

    @field_validator("status", "blocked_reason", "next_action", "diagnosis_path", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)

    @field_validator("returncode", mode="before")
    @classmethod
    def _coerce_returncode(cls, value: object) -> int:
        return _as_int(value, default=3)


class _DiagnosisAttempt(_LooseRetryInput):
    diagnosis_path: str = ""
    status: str = ""
    blocked_reason: str = ""
    snapshot_hash: str = ""
    git_status_hash: str = ""
    vocabulary_db_hash: str = ""
    related_notes_export_hash: str = ""
    diagnosis_options_hash: str = ""
    trigger_context_hash: str = ""
    blocker_fingerprint: str = ""
    next_action: str = ""
    created_at: str = ""

    @field_validator(
        "diagnosis_path",
        "status",
        "blocked_reason",
        "snapshot_hash",
        "git_status_hash",
        "vocabulary_db_hash",
        "related_notes_export_hash",
        "diagnosis_options_hash",
        "trigger_context_hash",
        "blocker_fingerprint",
        "next_action",
        "created_at",
        mode="before",
    )
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)

    def matches_identity(self, identity: DiagnosisIdentity) -> bool:
        return all(self.identity_field(field) == identity.field_value(field) for field in _IDENTITY_FIELDS)

    def identity_field(self, field: str) -> str:
        match field:
            case "snapshot_hash":
                return self.snapshot_hash
            case "git_status_hash":
                return self.git_status_hash
            case "vocabulary_db_hash":
                return self.vocabulary_db_hash
            case "related_notes_export_hash":
                return self.related_notes_export_hash
            case "diagnosis_options_hash":
                return self.diagnosis_options_hash
            case "trigger_context_hash":
                return self.trigger_context_hash
            case _:
                return ""


class _LinkState(_LooseRetryInput):
    last_diagnosis_attempt: _DiagnosisAttempt | None = None


class _ForceDiagnoseAgentEvent(ContractModel):
    schema_id: Literal["medical-notes-workbench.agent-event.v1"] = Field(
        default="medical-notes-workbench.agent-event.v1",
        alias="schema",
    )
    type: Literal["force_diagnose_override"] = "force_diagnose_override"
    code: Literal["linker.force_diagnose_override"] = FORCE_DIAGNOSE_EVENT_CODE
    severity: Literal["medium"] = "medium"
    root_cause_code: Literal["force_diagnose_override"] = "force_diagnose_override"
    workflow: Literal["/mednotes:link"] = "/mednotes:link"
    phase: Literal["diagnosis"] = "diagnosis"
    recovery_command: str
    artifact_path: str
    redacted_sample: JsonObject
    next_action: str


def diagnosis_options_hash(
    *,
    include_related_notes: bool,
    llm_disambiguation: str,
    llm_model: str | None,
    llm_timeout: int,
) -> str:
    return "sha256:" + canonical_json_hash(
        {
            "include_related_notes": include_related_notes,
            "llm_disambiguation": llm_disambiguation,
            "llm_model": llm_model or "",
            "llm_timeout": int(llm_timeout),
        }
    )


def trigger_context_hash(trigger_context: object | None) -> str:
    payload = JsonObjectAdapter.validate_python(trigger_context or {})
    return "sha256:" + canonical_json_hash(payload)


def build_diagnosis_identity(
    *,
    snapshot: object,
    git_context: object,
    trigger_context: object | None,
    include_related_notes: bool,
    llm_disambiguation: str,
    llm_model: str | None,
    llm_timeout: int,
) -> DiagnosisIdentity:
    snapshot_fields = _SnapshotIdentityFields.model_validate(snapshot)
    git_fields = _GitIdentityFields.model_validate(git_context)
    return DiagnosisIdentity(
        snapshot_hash=snapshot_fields.snapshot_hash,
        git_status_hash=git_fields.status_hash,
        git_head=git_fields.head,
        vocabulary_db_hash=snapshot_fields.vocabulary_db_hash,
        related_notes_export_hash=snapshot_fields.related_notes_export_hash,
        diagnosis_options_hash=diagnosis_options_hash(
            include_related_notes=include_related_notes,
            llm_disambiguation=llm_disambiguation,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
        ),
        trigger_context_hash=trigger_context_hash(trigger_context),
    )


def redundant_diagnosis_payload(link_state: object, identity: DiagnosisIdentity) -> JsonObject | None:
    state = _LinkState.model_validate(link_state)
    last = state.last_diagnosis_attempt
    if last is None:
        return None
    if last.status not in {"blocked", "failed"}:
        return None
    if not last.matches_identity(identity):
        return None
    diagnosis_path = Path(last.diagnosis_path)
    if not diagnosis_path.is_file():
        return None
    try:
        raw_payload = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_payload = {}
    payload = JsonObjectAdapter.validate_python(raw_payload if isinstance(raw_payload, dict) else {})
    payload_fields = _DiagnosisPayloadFields.model_validate(payload)
    blocked_reason = last.blocked_reason or payload_fields.blocked_reason or "link_plan_blocked"
    next_action = last.next_action or payload_fields.next_action
    payload.update(
        {
            "status": last.status or "blocked",
            "blocked_reason": blocked_reason,
            "diagnosis_path": str(diagnosis_path),
            "skipped_reason": REDUNDANT_DIAGNOSIS_REASON,
            "next_action": next_action,
            "retry_governance": {
                "skipped": True,
                "skipped_reason": REDUNDANT_DIAGNOSIS_REASON,
                "state_file": str(default_link_state_path()),
                "blocker_fingerprint": last.blocker_fingerprint,
            },
            "agent_events": [
                {
                    "schema": "medical-notes-workbench.agent-event.v1",
                    "type": "redundant_diagnosis_without_state_change",
                    "code": "agent.redundant_diagnosis_without_state_change",
                    "severity": "medium",
                    "root_cause_code": REDUNDANT_DIAGNOSIS_REASON,
                    "workflow": "/mednotes:link",
                    "phase": "diagnosis",
                    "recovery_command": last.next_action,
                    "artifact_path": str(diagnosis_path),
                    "redacted_sample": {
                        "blocked_reason": last.blocked_reason,
                        "blocker_fingerprint": last.blocker_fingerprint,
                    },
                    "next_action": last.next_action,
                }
            ],
        }
    )
    payload["returncode"] = payload_fields.returncode
    return payload


def record_diagnosis_attempt(
    payload: object,
    *,
    identity: DiagnosisIdentity,
    state_path: Path | None = None,
) -> JsonObject:
    payload_fields = _DiagnosisPayloadFields.model_validate(payload)
    state_model = load_link_state(state_path)
    state: JsonObject = state_model.to_payload() if state_model is not None else {}
    state["schema"] = LINK_STATE_SCHEMA_V2
    state["generated_at"] = _now_iso()
    if "snapshot_hash" not in state and identity.snapshot_hash:
        state["snapshot_hash"] = identity.snapshot_hash
    if "git_head" not in state and identity.git_head:
        state["git_head"] = identity.git_head
    if "git_status_hash" not in state and identity.git_status_hash:
        state["git_status_hash"] = identity.git_status_hash
    last = _DiagnosisAttempt(
        diagnosis_path=payload_fields.diagnosis_path,
        status=payload_fields.status,
        blocked_reason=payload_fields.blocked_reason,
        snapshot_hash=identity.snapshot_hash,
        git_status_hash=identity.git_status_hash,
        vocabulary_db_hash=identity.vocabulary_db_hash,
        related_notes_export_hash=identity.related_notes_export_hash,
        diagnosis_options_hash=identity.diagnosis_options_hash,
        trigger_context_hash=identity.trigger_context_hash,
        blocker_fingerprint=blocker_fingerprint(payload_fields),
        next_action=payload_fields.next_action,
        created_at=_now_iso(),
    )
    state["last_diagnosis_attempt"] = last.to_payload()
    path = state_path or default_link_state_path()
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


def blocker_fingerprint(payload: object) -> str:
    payload_fields = _DiagnosisPayloadFields.model_validate(payload)
    clean_blockers = [blocker.compact_payload() for blocker in payload_fields.blockers]
    fingerprint_payload = JsonObjectAdapter.validate_python(
        {
            "status": payload_fields.status,
            "blocked_reason": payload_fields.blocked_reason,
            "blockers": clean_blockers,
        }
    )
    return "sha256:" + canonical_json_hash(fingerprint_payload)


def force_diagnose_event(*, diagnosis_path: Path) -> JsonObject:
    return _ForceDiagnoseAgentEvent(
        recovery_command=wiki_cli_command("run-linker", "--diagnose", "--json"),
        artifact_path=str(diagnosis_path),
        redacted_sample={"override": "--force-diagnose"},
        next_action="Confirmar que algum input mudou ou registrar por que o override era necessario.",
    ).to_payload()
