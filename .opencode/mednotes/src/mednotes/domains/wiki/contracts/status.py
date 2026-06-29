"""Typed snapshot contract for the non-mutating Workbench status surface.

`/mednotes:status` is intentionally not a workflow FSM: it observes local
configuration and reports recovery routes. This model keeps that adapter output
typed so status fields cannot become a parallel source of truth for FSM-first
workflows.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mednotes.kernel.base import ContractModel, JsonObject


class StatusSnapshotPhase(StrEnum):
    STATUS = "status"


class StatusSnapshotStatus(StrEnum):
    READY = "ready"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    BLOCKED = "blocked"


class StatusSnapshot(ContractModel):
    schema_: Literal["medical-notes-workbench.status.v1"] = Field(
        "medical-notes-workbench.status.v1",
        alias="schema",
    )
    phase: StatusSnapshotPhase = StatusSnapshotPhase.STATUS
    status: StatusSnapshotStatus
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    raw_dir: str = ""
    raw_dir_exists: bool = False
    wiki_dir: str = ""
    wiki_dir_exists: bool = False
    wiki_source: str = ""
    wiki_memory_path: str = ""
    config_path: str = ""
    catalog_path: str = ""
    catalog_path_exists: bool = False
    vocabulary_db_path: str = ""
    vocabulary_db_exists: bool = False
    warnings: list[str] = Field(default_factory=list)
    path_resolution: JsonObject | None = None
    environment_preflight: JsonObject = Field(default_factory=dict)
    validate_environment: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def blocked_status_has_recovery_route(self) -> StatusSnapshot:
        if self.status == StatusSnapshotStatus.BLOCKED:
            if not self.blocked_reason.strip():
                raise ValueError("blocked status snapshot requires blocked_reason")
            if not self.next_action.strip():
                raise ValueError("blocked status snapshot requires next_action")
        return self
