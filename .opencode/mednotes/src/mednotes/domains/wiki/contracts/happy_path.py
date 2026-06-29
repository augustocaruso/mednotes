from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from mednotes.kernel.base import ContractModel, JsonObject


class HappyPathViolationCategory(StrEnum):
    ROUTE = "route"
    TOOL_CONTRACT = "tool_contract"
    FSM_CONTRACT = "fsm_contract"
    ARTIFACT_INTEGRITY = "artifact_integrity"
    PUBLIC_COMMUNICATION = "public_communication"
    PERFORMANCE = "performance"
    PRIMARY_OBJECTIVE = "primary_objective"
    HUMAN_ATTENTION = "human_attention"


class HappyPathViolation(ContractModel):
    code: str = Field(min_length=1)
    category: HappyPathViolationCategory
    severity: Literal["low", "medium", "high", "critical"]
    message: str = Field(min_length=1)
    evidence: JsonObject = Field(default_factory=dict)


class HappyPathFindingInput(ContractModel):
    # Agent report findings carry richer audit fields; happy-path scoring only
    # consumes the stable violation surface below.
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    code: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"]
    message: str = Field(min_length=1)
    evidence: JsonObject = Field(default_factory=dict)


class HappyPathRunMetrics(ContractModel):
    schema_: Literal["medical-notes-workbench.happy-path-run-metrics.v1"] = Field(
        "medical-notes-workbench.happy-path-run-metrics.v1",
        alias="schema",
    )
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: Literal["happy", "degraded", "failed"]
    score: int = Field(ge=0, le=100)
    primary_objective_completed: bool
    legitimate_stop_reason: str = ""
    violation_count: int = Field(ge=0)
    violations: list[HappyPathViolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _count_and_score_match(self) -> HappyPathRunMetrics:
        if self.violation_count != len(self.violations):
            raise ValueError("violation_count must match violations length")
        if self.violation_count == 0 and self.score != 100:
            raise ValueError("perfect happy path must score 100")
        if self.status == "happy" and self.violation_count != 0:
            raise ValueError("happy status cannot include violations")
        return self


class HappyPathRoundMetrics(ContractModel):
    schema_: Literal["medical-notes-workbench.happy-path-round-metrics.v1"] = Field(
        "medical-notes-workbench.happy-path-round-metrics.v1",
        alias="schema",
    )
    workflow: str = Field(min_length=1)
    run_count: int = Field(ge=0)
    happy_run_count: int = Field(ge=0)
    happy_path_prevalence_percent: int = Field(ge=0, le=100)
    target_prevalence_percent: int = 100
    runs: list[HappyPathRunMetrics] = Field(default_factory=list)

    @model_validator(mode="after")
    def _round_counts_match_runs(self) -> HappyPathRoundMetrics:
        if self.run_count != len(self.runs):
            raise ValueError("run_count must match runs length")
        if self.happy_run_count != sum(1 for run in self.runs if run.status == "happy"):
            raise ValueError("happy_run_count must match happy runs")
        expected = 100 if self.run_count == 0 else int((self.happy_run_count / self.run_count) * 100)
        if self.happy_path_prevalence_percent != expected:
            raise ValueError("happy_path_prevalence_percent must match run counts")
        return self


_FINDING_CATEGORY_BY_CODE: dict[str, HappyPathViolationCategory] = {
    "agent.public_output_internal_term_leak": HappyPathViolationCategory.PUBLIC_COMMUNICATION,
    "agent.final_report_success_claim_mismatch": HappyPathViolationCategory.PUBLIC_COMMUNICATION,
    "agent.final_report_progress_status_mismatch": HappyPathViolationCategory.FSM_CONTRACT,
    "agent.final_report_receipt_status_mismatch": HappyPathViolationCategory.FSM_CONTRACT,
    "agent.workflow_contract_contradiction": HappyPathViolationCategory.FSM_CONTRACT,
    "agent.final_report_tool_deviation_omitted": HappyPathViolationCategory.ROUTE,
    "agent.runtime_route_probe_observed": HappyPathViolationCategory.ROUTE,
    "agent.waiting_external_continuation_attempted": HappyPathViolationCategory.ROUTE,
    "agent.ready_continuation_stopped": HappyPathViolationCategory.ROUTE,
    "agent.runtime_performance_bug": HappyPathViolationCategory.PERFORMANCE,
    "agent.final_report_primary_objective_omitted": HappyPathViolationCategory.PRIMARY_OBJECTIVE,
    "agent.process_chats_primary_objective_unresolved": HappyPathViolationCategory.PRIMARY_OBJECTIVE,
    "agent.process_chats_vault_deletion_without_receipt": HappyPathViolationCategory.ARTIFACT_INTEGRITY,
    "agent.specialist_model_policy_violation": HappyPathViolationCategory.ROUTE,
}


def happy_path_metrics_from_findings(
    *,
    workflow: str,
    run_id: str,
    findings: Sequence[object],
    primary_objective_completed: bool,
    legitimate_stop_reason: str,
) -> HappyPathRunMetrics:
    violations = [_violation_from_finding(finding) for finding in findings]
    if not primary_objective_completed and not legitimate_stop_reason:
        violations.append(
            HappyPathViolation(
                code="workflow.primary_objective_not_completed",
                category=HappyPathViolationCategory.PRIMARY_OBJECTIVE,
                severity="high",
                message="Workflow did not complete its declared primary objective.",
            )
        )
    score = max(0, 100 - sum(_severity_penalty(item.severity) for item in violations))
    status: Literal["happy", "degraded", "failed"]
    if not violations:
        status = "happy"
    elif any(item.severity in {"high", "critical"} for item in violations):
        status = "failed"
    else:
        status = "degraded"
    return HappyPathRunMetrics(
        workflow=workflow,
        run_id=run_id,
        status=status,
        score=score,
        primary_objective_completed=primary_objective_completed,
        legitimate_stop_reason=legitimate_stop_reason,
        violation_count=len(violations),
        violations=violations,
    )


def happy_path_round_metrics(*, workflow: str, runs: list[HappyPathRunMetrics]) -> HappyPathRoundMetrics:
    happy_run_count = sum(1 for run in runs if run.status == "happy")
    run_count = len(runs)
    prevalence = 100 if run_count == 0 else int((happy_run_count / run_count) * 100)
    return HappyPathRoundMetrics(
        workflow=workflow,
        run_count=run_count,
        happy_run_count=happy_run_count,
        happy_path_prevalence_percent=prevalence,
        runs=runs,
    )


def _violation_from_finding(finding: object) -> HappyPathViolation:
    typed = _finding_input(finding)
    return HappyPathViolation(
        code=typed.code,
        category=_FINDING_CATEGORY_BY_CODE.get(typed.code, HappyPathViolationCategory.ROUTE),
        severity=typed.severity,
        message=typed.message,
        evidence=typed.evidence,
    )


def _finding_input(finding: object) -> HappyPathFindingInput:
    if isinstance(finding, HappyPathFindingInput):
        return finding
    if isinstance(finding, ContractModel):
        return HappyPathFindingInput.model_validate(finding.to_payload())
    return HappyPathFindingInput.model_validate(finding)


def _severity_penalty(severity: str) -> int:
    match severity:
        case "critical":
            return 100
        case "high":
            return 50
        case "medium":
            return 25
        case "low":
            return 10
        case _:
            return 25
