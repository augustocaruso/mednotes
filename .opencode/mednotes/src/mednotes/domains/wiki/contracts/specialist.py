from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from pydantic.json_schema import JsonDict

from mednotes.kernel.base import ContractModel, JsonObject

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_MEDICAL_MODEL_TOKENS = ("flash", "lite", "nano")
_SPECIALIST_GRADE_MODEL_TOKENS = ("pro", "opus", "sonnet", "specialist")
SpecialistTaskPhase = Literal["style_rewrite", "note_merge", "medical_authoring"]
SpecialistOutputReceiptSchema = Literal[
    "medical-notes-workbench.style-rewrite-output.v1",
    "medical-notes-workbench.note-merge-output.v1",
    "medical-notes-workbench.medical-authoring-output.v1",
]
SpecialistOutputAttestationSchema = Literal[
    "medical-notes-workbench.style-rewrite-output-attestation.v1",
    "medical-notes-workbench.note-merge-output-attestation.v1",
    "medical-notes-workbench.medical-authoring-output-attestation.v1",
]

_SPECIALIST_OUTPUT_RECEIPT_SCHEMA_BY_PHASE: dict[SpecialistTaskPhase, SpecialistOutputReceiptSchema] = {
    "style_rewrite": "medical-notes-workbench.style-rewrite-output.v1",
    "note_merge": "medical-notes-workbench.note-merge-output.v1",
    "medical_authoring": "medical-notes-workbench.medical-authoring-output.v1",
}
_SPECIALIST_OUTPUT_ATTESTATION_SCHEMA_BY_PHASE: dict[SpecialistTaskPhase, SpecialistOutputAttestationSchema] = {
    "style_rewrite": "medical-notes-workbench.style-rewrite-output-attestation.v1",
    "note_merge": "medical-notes-workbench.note-merge-output-attestation.v1",
    "medical_authoring": "medical-notes-workbench.medical-authoring-output-attestation.v1",
}


def _completed_output_reference_json_schema(schema_id: str, phase: str) -> JsonDict:
    return {
        "type": "object",
        "required": ["schema", "work_id", "phase", "status", "output_path", "output_sha256"],
        "properties": {
            "schema": {"const": schema_id},
            "work_id": {"type": "string", "minLength": 1},
            "phase": {"const": phase},
            "status": {"const": "completed"},
            "output_path": {"type": "string", "minLength": 1},
            "output_sha256": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        },
    }


def _completed_phase_json_schema(
    phase: SpecialistTaskPhase,
    receipt_schema: SpecialistOutputReceiptSchema,
    attestation_schema: SpecialistOutputAttestationSchema,
) -> JsonDict:
    return {
        "if": {
            "properties": {
                "status": {"const": "completed"},
                "phase": {"const": phase},
            },
            "required": ["status", "phase"],
        },
        "then": {
            "required": ["phase", "specialist_output_receipt", "specialist_output_attestation"],
            "properties": {
                "phase": {"const": phase},
                "specialist_output_receipt": _completed_output_reference_json_schema(receipt_schema, phase),
                "specialist_output_attestation": _completed_output_reference_json_schema(attestation_schema, phase),
            },
        },
    }


_SPECIALIST_TASK_RUN_RECEIPT_JSON_SCHEMA_EXTRA: JsonDict = {
    "allOf": [
        {
            "if": {
                "properties": {"status": {"const": "completed"}},
                "required": ["status"],
            },
            "then": {
                "required": [
                    "input_packet_path",
                    "input_packet_sha256",
                    "phase",
                    "model_evidence",
                    "output_path",
                    "output_sha256",
                    "parent_session_id",
                    "receipt_attestation",
                    "specialist_session_id",
                    "specialist_output_receipt",
                    "specialist_output_attestation",
                    "transcript_artifact_path",
                    "transcript_artifact_sha256",
                    "validation_status",
                    "quality_review_status",
                ],
                "properties": {
                    "input_packet_path": {"type": "string", "minLength": 1},
                    "input_packet_sha256": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                    "model_evidence": {"not": {"type": "null"}},
                    "phase": {"enum": ["style_rewrite", "note_merge", "medical_authoring"]},
                    "output_path": {"type": "string", "minLength": 1},
                    "output_sha256": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                    "parent_session_id": {"type": "string", "minLength": 1},
                    "receipt_attestation": {"not": {"type": "null"}},
                    "specialist_session_id": {"type": "string", "minLength": 1},
                    "specialist_output_receipt": {"not": {"type": "null"}},
                    "specialist_output_attestation": {"not": {"type": "null"}},
                    "transcript_artifact_path": {"type": "string", "minLength": 1},
                    "transcript_artifact_sha256": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                    "validation_status": {"const": "validated"},
                    "quality_review_status": {"const": "accepted"},
                },
            },
        },
        _completed_phase_json_schema(
            "style_rewrite",
            "medical-notes-workbench.style-rewrite-output.v1",
            "medical-notes-workbench.style-rewrite-output-attestation.v1",
        ),
        _completed_phase_json_schema(
            "note_merge",
            "medical-notes-workbench.note-merge-output.v1",
            "medical-notes-workbench.note-merge-output-attestation.v1",
        ),
        _completed_phase_json_schema(
            "medical_authoring",
            "medical-notes-workbench.medical-authoring-output.v1",
            "medical-notes-workbench.medical-authoring-output-attestation.v1",
        ),
        {
            "if": {
                "properties": {"status": {"enum": ["waiting_external", "blocked", "failed"]}},
                "required": ["status"],
            },
            "then": {
                "required": ["next_action", "validation_status", "quality_review_status"],
                "properties": {
                    "next_action": {"type": "string", "minLength": 1},
                    "validation_status": {"enum": ["not_run", "invalid"]},
                    "quality_review_status": {"enum": ["not_reviewed", "needs_review", "rejected"]},
                    "specialist_output_receipt": {"type": "null"},
                    "specialist_output_attestation": {"type": "null"},
                },
            },
        },
    ],
}


class SpecialistHarness(StrEnum):
    GEMINI_CLI = "gemini_cli"
    AGY = "agy"
    OPENCODE = "opencode"
    DIRECT_API = "direct_api"


class SpecialistRunStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_EXTERNAL = "waiting_external"
    BLOCKED = "blocked"
    FAILED = "failed"


class SpecialistValidationStatus(StrEnum):
    VALIDATED = "validated"
    NOT_RUN = "not_run"
    INVALID = "invalid"


class SpecialistQualityReviewStatus(StrEnum):
    ACCEPTED = "accepted"
    NOT_REVIEWED = "not_reviewed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class SpecialistNextApplyStep(ContractModel):
    schema_id: Literal["medical-notes-workbench.specialist-next-apply-step.v1"] = Field(
        default="medical-notes-workbench.specialist-next-apply-step.v1",
        alias="schema",
    )
    command_family: Literal["apply-specialist-style-rewrite"]
    arguments: list[str] = Field(min_length=9)
    must_run_before: list[str] = Field(min_length=1)
    agent_instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_arguments(self) -> SpecialistNextApplyStep:
        required_flags = (
            "--plan",
            "--manifest",
            "--work-id",
            "--specialist-run-receipt",
            "--json",
        )
        missing = [flag for flag in required_flags if flag not in self.arguments]
        if missing:
            raise ValueError(f"specialist next apply step missing arguments: {', '.join(missing)}")
        return self


class SpecialistModelEvidence(ContractModel):
    source: Literal[
        "gemini_cli_agent_metadata",
        "agy_transcript_metadata",
        "agy_settings_snapshot",
        "opencode_task_metadata",
        "direct_api_response_metadata",
    ]
    requested_model: str = Field(min_length=1)
    observed_provider_id: str = Field(min_length=1)
    observed_model_id: str = Field(min_length=1)
    evidence_strength: Literal["runtime_metadata", "settings_and_transcript", "api_response"]
    evidence_excerpt: str = ""


class _SpecialistOutputReferenceBase(ContractModel):
    work_id: str = Field(min_length=1)
    phase: SpecialistTaskPhase | None = None
    status: SpecialistRunStatus | None = None
    output_path: str = ""
    output_sha256: str = ""


class SpecialistOutputReceiptReference(_SpecialistOutputReferenceBase):
    schema_id: SpecialistOutputReceiptSchema = Field(alias="schema")


class SpecialistOutputAttestationReference(_SpecialistOutputReferenceBase):
    schema_id: SpecialistOutputAttestationSchema = Field(alias="schema")


class SpecialistTaskRunReceiptAttestation(ContractModel):
    schema_id: Literal["medical-notes-workbench.specialist-task-run-receipt-attestation.v1"] = Field(
        default="medical-notes-workbench.specialist-task-run-receipt-attestation.v1",
        alias="schema",
    )
    attestation_kind: Literal["workbench_ed25519.v1"]
    created_by: Literal["specialist-task-runner"]
    receipt_schema: Literal["medical-notes-workbench.specialist-task-run-receipt.v1"]
    receipt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    work_id: str = Field(min_length=1)
    phase: SpecialistTaskPhase
    harness: SpecialistHarness
    adapter: str = Field(min_length=1)
    key_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    nonce: str = Field(min_length=16)
    issued_at: str = Field(min_length=1)
    signature: str = Field(pattern=r"^ed25519:[A-Za-z0-9_-]+={0,2}$")


class SpecialistTaskRunReceipt(ContractModel):
    model_config = ConfigDict(json_schema_extra=_SPECIALIST_TASK_RUN_RECEIPT_JSON_SCHEMA_EXTRA)

    schema_id: Literal["medical-notes-workbench.specialist-task-run-receipt.v1"] = Field(
        default="medical-notes-workbench.specialist-task-run-receipt.v1",
        alias="schema",
    )
    work_id: str = Field(min_length=1)
    phase: SpecialistTaskPhase = "style_rewrite"
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
    receipt_attestation: SpecialistTaskRunReceiptAttestation | None = None

    @model_validator(mode="after")
    def _validate_status_contract(self) -> SpecialistTaskRunReceipt:
        match self.status:
            case SpecialistRunStatus.COMPLETED:
                self._validate_completed_receipt()
                self._validate_medical_model_policy()
            case SpecialistRunStatus.WAITING_EXTERNAL | SpecialistRunStatus.BLOCKED | SpecialistRunStatus.FAILED:
                self._validate_non_completed_receipt()
        return self

    def _validate_completed_receipt(self) -> None:
        if not self.output_path.strip() or not self.output_sha256.strip():
            raise ValueError("completed specialist run requires output_path and output_sha256")
        if _SHA256_PATTERN.fullmatch(self.output_sha256) is None:
            raise ValueError("completed specialist run requires sha256-shaped output hash")
        if not self.input_packet_path.strip() or _SHA256_PATTERN.fullmatch(self.input_packet_sha256) is None:
            raise ValueError("completed specialist run requires input_packet_path and input_packet_sha256")
        if not self.parent_session_id.strip():
            raise ValueError("completed specialist run requires parent_session_id")
        if not self.specialist_session_id.strip():
            raise ValueError("completed specialist run requires specialist_session_id")
        if (
            not self.transcript_artifact_path.strip()
            or _SHA256_PATTERN.fullmatch(self.transcript_artifact_sha256) is None
        ):
            raise ValueError("completed specialist run requires transcript_artifact_path and transcript_artifact_sha256")
        if self.model_evidence is None:
            raise ValueError("completed specialist run requires model_evidence")
        if self.validation_status != SpecialistValidationStatus.VALIDATED:
            raise ValueError("completed specialist run requires validation_status=validated")
        if self.quality_review_status != SpecialistQualityReviewStatus.ACCEPTED:
            raise ValueError("completed specialist run requires quality_review_status=accepted")
        if self.specialist_output_receipt is None:
            raise ValueError("completed specialist run requires specialist_output_receipt")
        if self.specialist_output_attestation is None:
            raise ValueError("completed specialist run requires specialist_output_attestation")
        if self.receipt_attestation is None:
            raise ValueError("completed specialist run requires receipt_attestation")
        self._validate_completed_output_reference(
            self.specialist_output_receipt,
            "specialist_output_receipt",
            _SPECIALIST_OUTPUT_RECEIPT_SCHEMA_BY_PHASE[self.phase],
        )
        self._validate_completed_output_reference(
            self.specialist_output_attestation,
            "specialist_output_attestation",
            _SPECIALIST_OUTPUT_ATTESTATION_SCHEMA_BY_PHASE[self.phase],
        )
        self._validate_completed_receipt_attestation()

    def _validate_completed_receipt_attestation(self) -> None:
        if self.receipt_attestation is None:
            raise ValueError("completed specialist run requires receipt_attestation")
        if self.receipt_attestation.receipt_schema != self.schema_id:
            raise ValueError("completed specialist run requires receipt_attestation.receipt_schema to match schema")
        if self.receipt_attestation.work_id != self.work_id:
            raise ValueError("completed specialist run requires receipt_attestation.work_id to match work_id")
        if self.receipt_attestation.phase != self.phase:
            raise ValueError("completed specialist run requires receipt_attestation.phase to match phase")
        if self.receipt_attestation.harness != self.harness:
            raise ValueError("completed specialist run requires receipt_attestation.harness to match harness")
        if self.receipt_attestation.adapter != self.adapter:
            raise ValueError("completed specialist run requires receipt_attestation.adapter to match adapter")

    def _validate_completed_output_reference(
        self,
        reference: SpecialistOutputReceiptReference | SpecialistOutputAttestationReference,
        field_name: str,
        expected_schema: str,
    ) -> None:
        if reference.schema_id != expected_schema:
            raise ValueError(f"completed specialist run requires {field_name}.schema={expected_schema}")
        if reference.work_id != self.work_id:
            raise ValueError(f"completed specialist run requires {field_name}.work_id to match work_id")
        if reference.phase is None:
            raise ValueError(f"completed specialist run requires {field_name}.phase")
        if reference.status is None:
            raise ValueError(f"completed specialist run requires {field_name}.status")
        if not reference.output_path.strip() or not reference.output_sha256.strip():
            raise ValueError(f"completed specialist run requires {field_name} output_path and output_sha256")
        if reference.output_path != self.output_path:
            raise ValueError(f"completed specialist run requires {field_name}.output_path to match output_path")
        if reference.output_sha256 != self.output_sha256:
            raise ValueError(f"completed specialist run requires {field_name}.output_sha256 to match output_sha256")
        if reference.status != SpecialistRunStatus.COMPLETED:
            raise ValueError(f"completed specialist run requires {field_name}.status=completed")
        if reference.phase != self.phase:
            raise ValueError(f"completed specialist run requires {field_name}.phase to match phase")

    def _validate_non_completed_receipt(self) -> None:
        if not self.next_action.strip():
            raise ValueError(f"{self.status.value} specialist run requires next_action")
        if self.validation_status == SpecialistValidationStatus.VALIDATED:
            raise ValueError("non-completed specialist run cannot be validated")
        if self.quality_review_status == SpecialistQualityReviewStatus.ACCEPTED:
            raise ValueError("non-completed specialist run cannot be accepted")
        if (
            self.specialist_output_receipt is not None
            or self.specialist_output_attestation is not None
            or self.receipt_attestation is not None
        ):
            raise ValueError(
                "non-completed specialist run cannot include specialist output references or receipt_attestation"
            )

    def _validate_medical_model_policy(self) -> None:
        if self.requested_model_policy.strip().lower() != "medical_specialist_authoring.v1":
            return
        observed_tokens = self._observed_model_and_provider_policy_tokens()
        if any(
            forbidden_token in observed_token
            for observed_token in observed_tokens
            for forbidden_token in _FORBIDDEN_MEDICAL_MODEL_TOKENS
        ):
            raise ValueError("specialist model policy forbids flash/lite/nano for medical authoring")
        observed_model_tokens = self._observed_model_identity_policy_tokens()
        if not any(token in _SPECIALIST_GRADE_MODEL_TOKENS for token in observed_model_tokens):
            raise ValueError("specialist model policy requires pro/specialist-grade observed model")

    def _observed_model_and_provider_policy_tokens(self) -> tuple[str, ...]:
        observed_values = [self.observed_model]
        if self.model_evidence is not None:
            observed_values.extend(
                [
                    self.model_evidence.observed_provider_id,
                    self.model_evidence.observed_model_id,
                ]
            )
        observed_text = " ".join(value.lower() for value in observed_values if value)
        return tuple(re.findall(r"[a-z0-9]+", observed_text))

    def _observed_model_identity_policy_tokens(self) -> tuple[str, ...]:
        observed_values = [self.observed_model]
        if self.model_evidence is not None:
            observed_values.append(self.model_evidence.observed_model_id)
        observed_text = " ".join(value.lower() for value in observed_values if value)
        return tuple(re.findall(r"[a-z0-9]+", observed_text))

    @classmethod
    def from_operation_payload(cls, payload: object) -> SpecialistTaskRunReceipt:
        return cls.model_validate(payload)
