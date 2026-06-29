"""Local workflow feedback utilities for Medical Notes Workbench."""
from __future__ import annotations

from mednotes.kernel.guardrails import (
    BLOCKING_STATUSES_REQUIRING_NEXT_ACTION,
    CONTRACT_GAP_MISSING_NEXT_ACTION,
    blocked_payload_requires_next_action,
    default_contract_next_action,
    needs_next_action_hardening,
)
from mednotes.platform.feedback.core import (
    BACKLOG_SCHEMA,
    RUN_RECORD_SCHEMA,
    build_backlog,
    build_diagnostic_context,
    command_string,
    feedback_root,
    record_workflow_run,
    redact_snippet,
    safe_record_workflow_run,
    summarize_payload,
)
from mednotes.platform.feedback.integrity import (
    INTEGRITY_MANIFEST_SCHEMA,
    INTEGRITY_STATUS_SCHEMA,
    check_extension_integrity,
    generate_integrity_manifest,
    write_integrity_manifest,
)
from mednotes.platform.feedback.operational_contract import (
    TOOL_PARAMETER_CONTRACT_VIOLATION,
    agent_preamble_lines,
    validate_agent_tool_calls,
)
from mednotes.platform.feedback.telemetry import TELEMETRY_ENVELOPE_SCHEMA

__all__ = [
    "BACKLOG_SCHEMA",
    "BLOCKING_STATUSES_REQUIRING_NEXT_ACTION",
    "CONTRACT_GAP_MISSING_NEXT_ACTION",
    "INTEGRITY_MANIFEST_SCHEMA",
    "INTEGRITY_STATUS_SCHEMA",
    "RUN_RECORD_SCHEMA",
    "TELEMETRY_ENVELOPE_SCHEMA",
    "TOOL_PARAMETER_CONTRACT_VIOLATION",
    "agent_preamble_lines",
    "blocked_payload_requires_next_action",
    "build_diagnostic_context",
    "build_backlog",
    "check_extension_integrity",
    "command_string",
    "default_contract_next_action",
    "feedback_root",
    "generate_integrity_manifest",
    "needs_next_action_hardening",
    "record_workflow_run",
    "redact_snippet",
    "safe_record_workflow_run",
    "summarize_payload",
    "validate_agent_tool_calls",
    "write_integrity_manifest",
]
