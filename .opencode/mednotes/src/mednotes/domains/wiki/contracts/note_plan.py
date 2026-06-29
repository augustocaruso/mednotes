from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mednotes.domains.wiki.contracts.raw_coverage import (
    CoverageAction as CoverageAction,
)
from mednotes.domains.wiki.contracts.raw_coverage import (
    RawCoverage as RawCoverage,
)
from mednotes.domains.wiki.contracts.raw_coverage import (
    RawCoverageItem as RawCoverageItem,
)
from mednotes.kernel.base import ContractModel

PlanAction = Literal["planned_meaning", "attach_to_planned_meaning", "not_a_note", "needs_context"]


class MeaningClaim(ContractModel):
    label: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    boundaries: list[str]
    kind: Literal[
        "clinical_concept",
        "drug_concept",
        "diagnostic_criterion",
        "management_strategy",
        "procedure",
        "physiology_or_mechanism",
        "epidemiology_or_definition",
    ]
    evidence_summary: str = Field(min_length=1)
    id: str | None = None


class TriageNotePlanItem(ContractModel):
    id: str = Field(min_length=1)
    action: PlanAction
    title: str | None = None
    staged_title: str | None = None
    meaning_claim: MeaningClaim | None = None
    taxonomy_hint: str | None = None
    aliases: list[str] | None = None
    target_item_id: str | None = None
    reason_code: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> TriageNotePlanItem:
        if self.action == "planned_meaning":
            if not self.title or not self.staged_title or self.meaning_claim is None:
                raise ValueError("planned_meaning items require title, staged_title and meaning_claim")
        elif self.action == "attach_to_planned_meaning":
            if not self.target_item_id or not self.reason_code or not self.reason:
                raise ValueError("attach_to_planned_meaning items require target_item_id, reason_code and reason")
        elif self.action in {"not_a_note", "needs_context"} and (not self.reason_code or not self.reason):
            raise ValueError(f"{self.action} items require reason_code and reason")
        return self


class TriageNotePlan(ContractModel):
    schema_id: Literal["medical-notes-workbench.triage-note-plan.v2"] = Field(alias="schema")
    raw_file: str = Field(min_length=1)
    exhaustive: Literal[True] = True
    items: list[TriageNotePlanItem] = Field(min_length=1)
    batch_id: str | None = None
    run_id: str | None = None
    source_artifact_hash: str | None = None


# RawCoverage lives in raw_coverage.py because it is a publish boundary, not a
# triage-plan concept. These imports remain as a stable public import path for
# existing schema/test code while the implementation has a single source.
