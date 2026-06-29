"""Primary objective projection for `/mednotes:process-chats`.

The FSM payload is the only source of truth. This module intentionally does not
reconstruct success from old root fields such as `status`, `phase`,
`blocked_reason`, `created_count` or `linker_applied`; those fields are legacy
run-record evidence and must not fabricate a primary objective result.
"""
from __future__ import annotations

from mednotes.domains.wiki.contracts.agent_report import ProcessChatsPrimaryObjectiveSummary
from mednotes.domains.wiki.flows.process_chats.process_chats_fsm import PROCESS_CHATS_SCHEMA
from mednotes.kernel.base import JsonObject, JsonObjectAdapter


def process_chats_primary_objective_summary(payload: JsonObject) -> ProcessChatsPrimaryObjectiveSummary | None:
    """Return the FSM-authored primary objective summary, if this is process-chats."""
    normalized = JsonObjectAdapter.validate_python(payload)
    schema = str(normalized["schema"]) if "schema" in normalized else ""
    workflow = str(normalized["workflow"]) if "workflow" in normalized else ""
    if schema != PROCESS_CHATS_SCHEMA or workflow != "/mednotes:process-chats":
        return None

    reports = JsonObjectAdapter.validate_python(normalized["reports"] if "reports" in normalized else {})
    details = JsonObjectAdapter.validate_python(reports["details"] if "details" in reports else {})
    if "primary_objective_summary" not in details:
        return None
    summary = JsonObjectAdapter.validate_python(details["primary_objective_summary"])
    return ProcessChatsPrimaryObjectiveSummary.model_validate(summary)
