from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from mednotes.kernel.base import ContractModel

WorkflowPublicObjectiveAnswer = Literal[
    "yes",
    "partial",
    "no",
    "waiting_agent",
    "waiting_external",
    "waiting_human",
    "failed",
]
WorkflowPublicMutationState = Literal["changed", "unchanged", "not_applicable"]

POSITIVE_FILE_COUNT_RE = re.compile(r"\b[1-9]\d*\s+arquivo")
ZERO_FILE_COUNT_RE = re.compile(r"\b0\s+arquivo")


class WorkflowPublicReportViewModel(ContractModel):
    schema_: Literal["medical-notes-workbench.workflow-public-report-view-model.v1"] = Field(
        "medical-notes-workbench.workflow-public-report-view-model.v1",
        alias="schema",
    )
    workflow: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    objective_answer: WorkflowPublicObjectiveAnswer
    headline: str = Field(min_length=1)
    mutation_state: WorkflowPublicMutationState
    mutation_summary: str = Field(min_length=1)
    remaining_work_summary: str = Field(min_length=1)
    next_step_summary: str = Field(min_length=1)
    user_attention_required: bool
    human_reason: str = ""
    internal_terms_present: bool = False

    @model_validator(mode="after")
    def _public_report_is_coherent(self) -> WorkflowPublicReportViewModel:
        folded_mutation = self.mutation_summary.casefold()
        has_positive_file_count = POSITIVE_FILE_COUNT_RE.search(folded_mutation) is not None
        has_only_zero_file_count = ZERO_FILE_COUNT_RE.search(folded_mutation) is not None and not has_positive_file_count
        if self.mutation_state == "changed" and ("nada foi alterado" in folded_mutation or has_only_zero_file_count):
            raise ValueError("public report mutation contradiction")
        if self.user_attention_required and not self.human_reason.strip():
            raise ValueError("public report user attention requires human reason")
        if self.internal_terms_present:
            raise ValueError("public report cannot expose internal terms")
        return self
