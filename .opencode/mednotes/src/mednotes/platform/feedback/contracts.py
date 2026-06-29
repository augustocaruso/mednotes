"""Typed adapter contracts for local telemetry and manual reports.

These models deliberately live in the platform layer: they describe local
adapter receipts, not domain policy. They are exported so launchers and agents
can audit `/mednotes:telemetry` and `/report` without falling back to loose
status dictionaries.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mednotes.kernel.base import ContractModel


class TelemetryRecentBundle(ContractModel):
    run_id: str = ""
    workflow: str = ""
    bundle_id: str = ""


class TelemetryHookHealth(ContractModel):
    recent_event_count: int = Field(ge=0)
    recent_error_count: int = Field(ge=0)
    latest_error_types: list[str] = Field(default_factory=list)


class TelemetryStatusSnapshot(ContractModel):
    schema_: Literal["medical-notes-workbench.workflow-telemetry-status.v1"] = Field(
        "medical-notes-workbench.workflow-telemetry-status.v1",
        alias="schema",
    )
    enabled: bool
    ready: bool
    endpoint_url: str = ""
    payload_level: str = Field(min_length=1)
    consent_at: str = ""
    auto_enabled_at: str = ""
    opt_out_at: str = ""
    source: str = Field(min_length=1)
    install_id: str = ""
    outbox_count: int = Field(ge=0)
    sent_run_count: int = Field(ge=0)
    config_path: str = Field(min_length=1)
    defaults_path: str = ""
    recent_bundles: list[TelemetryRecentBundle] = Field(default_factory=list)
    pending_pre_update_snapshot_count: int = Field(ge=0)
    hook_health: TelemetryHookHealth

    @model_validator(mode="after")
    def disabled_project_cannot_be_ready(self) -> TelemetryStatusSnapshot:
        if self.source == "project_disabled" and (self.enabled or self.ready):
            raise ValueError("project-disabled telemetry cannot be enabled or ready")
        return self


class ManualReportReceipt(ContractModel):
    schema_: Literal["medical-notes-workbench.manual-report-receipt.v1"] = Field(
        "medical-notes-workbench.manual-report-receipt.v1",
        alias="schema",
    )
    status: Literal["sent", "not_sent", "blocked"]
    requested_by_user: bool = True
    capture_schema: str = Field(min_length=1)
    envelope_schema: str = Field(min_length=1)
    snapshot_path: str = ""
    envelope_path: str = ""
    send_result_path: str = ""
    sent: bool = False
    reason: str = ""
    next_action: str = ""
    redaction_status: Literal["redacted"] = "redacted"

    @model_validator(mode="after")
    def status_matches_send_result(self) -> ManualReportReceipt:
        if self.status == "sent" and not self.sent:
            raise ValueError("sent manual report receipt requires sent=true")
        if self.status != "sent" and self.sent:
            raise ValueError("sent=true requires status=sent")
        if self.status in {"not_sent", "blocked"} and not self.reason.strip():
            raise ValueError("unsent manual report receipt requires reason")
        return self
