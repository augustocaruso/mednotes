"""Problem-domain helpers for the fix-wiki master diagnosis."""
from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import ValidationError as PydanticValidationError
from pydantic import model_validator

from mednotes.domains.wiki.common import ValidationError
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter


class FixWikiProblemDomain(StrEnum):
    """Owned problem domains for `/mednotes:fix-wiki` master diagnosis."""

    STRUCTURE = "structure"
    IDENTITY = "identity"
    CONTENT = "content"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    PROVENANCE = "provenance"
    HYGIENE = "hygiene"
    RUNTIME = "runtime"


class FixWikiProblemSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"


class FixWikiProblemStatus(StrEnum):
    DIAGNOSED = "diagnosed"
    READY = "ready"
    NEEDS_DECISION = "needs_decision"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    SKIPPED = "skipped"


ALLOWED_PROBLEM_DOMAINS = tuple(domain.value for domain in FixWikiProblemDomain)


class FixWikiProblem(ContractModel):
    """Typed problem contract; only `to_problem_payload()` crosses as JSON."""

    domain: FixWikiProblemDomain
    code: str
    severity: FixWikiProblemSeverity
    risk: str
    problem: str
    recommendation: str
    status: FixWikiProblemStatus
    can_autofix: bool = False
    decision_required: bool = False
    recommended_action: str = ""
    resolver: str = ""
    context_packet: str = ""
    linker_trigger_after_resolve: bool = False
    evidence: JsonObject | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if not self.code.startswith(f"{self.domain.value}."):
            raise ValueError(f"Fix-wiki problem code must start with its domain: {self.code}")
        for field_name in ("problem", "recommendation", "risk"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"Fix-wiki problem missing required field: {field_name}")
        if self.decision_required:
            if not self.recommended_action.strip():
                raise ValueError("Fix-wiki decision problem requires recommended_action")
            if not self.context_packet.strip():
                raise ValueError("Fix-wiki decision problem requires context_packet")
        return self

    def to_problem_payload(self) -> JsonObject:
        payload = self.to_payload()
        for key, value in (
            ("recommended_action", self.recommended_action),
            ("resolver", self.resolver),
            ("context_packet", self.context_packet),
            ("evidence", self.evidence),
        ):
            if not value:
                payload.pop(key, None)
        if not self.linker_trigger_after_resolve:
            payload.pop("linker_trigger_after_resolve", None)
        return JsonObjectAdapter.validate_python(payload)


def _problem_from_payload(problem: object) -> FixWikiProblem:
    try:
        return FixWikiProblem.model_validate(problem)
    except PydanticValidationError as exc:
        text = str(exc)
        if "Input should be" in text and "domain" in text:
            raise ValidationError(f"Invalid fix-wiki problem domain: {text}") from exc
        if "Input should be" in text and "severity" in text:
            raise ValidationError(f"Invalid fix-wiki problem severity: {text}") from exc
        if "Input should be" in text and "status" in text:
            raise ValidationError(f"Invalid fix-wiki problem status: {text}") from exc
        raise ValidationError(text) from exc


def validate_problem(problem: object) -> JsonObject:
    return _problem_from_payload(problem).to_problem_payload()


def build_problem(
    *,
    domain: str,
    code: str,
    severity: str,
    problem: str,
    recommendation: str,
    risk: str,
    status: str,
    can_autofix: bool = False,
    decision_required: bool = False,
    recommended_action: str = "",
    resolver: str = "",
    context_packet: str = "",
    evidence: JsonObject | None = None,
    linker_trigger_after_resolve: bool = False,
) -> JsonObject:
    try:
        typed_problem = FixWikiProblem(
            domain=FixWikiProblemDomain(domain),
            code=code,
            severity=FixWikiProblemSeverity(severity),
            risk=risk,
            problem=problem,
            recommendation=recommendation,
            can_autofix=can_autofix,
            decision_required=decision_required,
            status=FixWikiProblemStatus(status),
            recommended_action=recommended_action,
            resolver=resolver,
            context_packet=context_packet,
            evidence=evidence,
            linker_trigger_after_resolve=linker_trigger_after_resolve,
        )
    except ValueError as exc:
        if domain not in ALLOWED_PROBLEM_DOMAINS:
            raise ValidationError(f"Invalid fix-wiki problem domain: {domain}") from exc
        if severity not in {item.value for item in FixWikiProblemSeverity}:
            raise ValidationError(f"Invalid fix-wiki problem severity: {severity}") from exc
        if status not in {item.value for item in FixWikiProblemStatus}:
            raise ValidationError(f"Invalid fix-wiki problem status: {status}") from exc
        raise ValidationError(str(exc)) from exc
    return typed_problem.to_problem_payload()
