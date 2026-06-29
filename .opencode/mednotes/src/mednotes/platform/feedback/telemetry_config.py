"""Typed telemetry configuration boundary.

Remote telemetry is disabled by project policy, but config parsing still gates
preview/send code paths. Raw TOML dictionaries cannot decide whether telemetry
is ready; callers receive a strict model or a disabled fallback.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, ValidationError

from mednotes.kernel.base import ContractModel, JsonObject

DEFAULT_PAYLOAD_LEVEL = "diagnostic_redacted"
DEFAULT_MAX_ENVELOPE_BYTES = 256 * 1024
PROJECT_DISABLED_SOURCE = "project_disabled"


class TelemetryConfig(ContractModel):
    """Canonical telemetry config consumed by status/send code paths."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = False
    endpoint_url: str = ""
    auth_token: str = ""
    payload_level: str = DEFAULT_PAYLOAD_LEVEL
    consent_at: str = ""
    install_id: str = ""
    max_envelope_bytes: int = Field(default=DEFAULT_MAX_ENVELOPE_BYTES, ge=1024)
    source: str = PROJECT_DISABLED_SOURCE
    auto_enabled_at: str = ""
    opt_out_at: str = ""
    defaults_path: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.endpoint_url and self.auth_token and self.install_id)


class TelemetrySection(ContractModel):
    """Strict TOML `[telemetry]` section accepted at the raw-file boundary."""

    model_config = ConfigDict(extra="ignore", strict=True)

    enabled: bool = False
    endpoint_url: str = ""
    auth_token: str = ""
    payload_level: str = DEFAULT_PAYLOAD_LEVEL
    consent_at: str = ""
    install_id: str = ""
    max_envelope_bytes: int | None = None
    source: str = "user"
    auto_enabled_at: str = ""
    opt_out_at: str = ""
    defaults_path: str = ""

    @classmethod
    def from_payload(cls, payload: JsonObject) -> TelemetrySection:
        """Invalid TOML values become a disabled section instead of coercion."""

        try:
            return cls.model_validate(payload)
        except ValidationError:
            return cls()
