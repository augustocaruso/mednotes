"""Canonical `/mednotes:link` artifact boundary for parent workflows.

Lower-level link adapters may still translate raw runtime output, but a parent
workflow can only consume the public child FSM payload. Accepting `link-run.v1`
here would recreate a second source of workflow state.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from mednotes.kernel.base import ContractModel, JsonObject
from mednotes.kernel.progress import WorkflowProgressViewModel


class LinkFsmArtifact(ContractModel):
    # Parent workflows consume a stable subset of the child FSM payload. The
    # schema discriminator rejects legacy artifacts; extra fields are ignored so
    # public child-FSM additions do not become a second parent-state contract.
    model_config = ConfigDict(extra="ignore", strict=True)

    schema_id: Literal["medical-notes-workbench.link-fsm-result.v1"] = Field(alias="schema")
    workflow: Literal["/mednotes:link", "/mednotes:link-body"]
    run_id: str = Field(min_length=1)
    state_machine_snapshot: JsonObject
    progress_view_model: WorkflowProgressViewModel
    decision: JsonObject | None = None
    human_decision_packet: JsonObject | None = None
    receipt: JsonObject
    reports: JsonObject
    agent_directive: JsonObject
    artifacts: JsonObject
    version_control_safety: JsonObject
    diagnostic_context: JsonObject | None = None
    error_context: JsonObject

    @property
    def operation_status(self) -> str:
        return self.progress_view_model.status.value


def normalize_link_runtime_artifact(payload: object) -> LinkFsmArtifact:
    """Validate the child FSM payload before its status can affect the parent."""

    if not isinstance(payload, dict):
        raise ValueError("link artifact must be an object")
    schema = payload["schema"] if "schema" in payload else ""
    if schema != "medical-notes-workbench.link-fsm-result.v1":
        raise ValueError("link artifact must be medical-notes-workbench.link-fsm-result.v1")
    return LinkFsmArtifact.model_validate(payload)
