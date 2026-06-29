from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject

PathBlockerReason = Literal[
    "paths.wiki_dir_missing",
    "paths.raw_dir_missing",
    "paths.ambiguous",
    "paths.config_invalid",
]
PathRequiredInput = Literal["wiki_dir", "raw_dir", "config_path"]
_WIKI_REASON_MAP: dict[str, PathBlockerReason] = {
    "missing_wiki_dir": "paths.wiki_dir_missing",
    "ambiguous_wiki_dir": "paths.ambiguous",
    "invalid_wiki_dir": "paths.config_invalid",
}
_DEFAULT_REASON: PathBlockerReason = "paths.config_invalid"


class WorkbenchPathsConfig(ContractModel):
    wiki_dir: Path
    raw_dir: Path | None = None

    @field_validator("wiki_dir")
    @classmethod
    def wiki_dir_must_exist(cls, value: Path) -> Path:
        if not value.exists() or not value.is_dir():
            raise ValueError("wiki_dir must be an existing directory")
        return value

    @field_validator("raw_dir")
    @classmethod
    def raw_dir_must_exist_when_present(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.exists() or not value.is_dir()):
            raise ValueError("raw_dir must be an existing directory when provided")
        return value


class PathResolutionBlocker(ContractModel):
    blocked_reason: PathBlockerReason
    legacy_blocked_reason: str = ""
    next_action: str = Field(min_length=1)
    required_inputs: list[PathRequiredInput] = Field(default_factory=list)
    human_decision_required: bool = False
    human_decision_packet: JsonObject | None = None

    @model_validator(mode="after")
    def human_decision_requires_packet(self) -> PathResolutionBlocker:
        if self.human_decision_required and self.human_decision_packet is None:
            raise ValueError("human_decision_required requires human_decision_packet")
        return self


class PathResolutionResult(ContractModel):
    status: Literal["ready", "blocked"]
    paths: WorkbenchPathsConfig | None = None
    blocker: PathResolutionBlocker | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> PathResolutionResult:
        if self.status == "ready" and self.blocker is not None:
            raise ValueError("ready path resolution cannot include blocker")
        if self.status == "ready" and self.paths is None:
            raise ValueError("ready path resolution requires paths")
        if self.status == "blocked" and self.blocker is None:
            raise ValueError("blocked path resolution requires blocker")
        return self


class WikiPathResolutionPayload(ContractModel):
    schema_: Literal["medical-notes-workbench.path-resolution.v1"] = Field(
        "medical-notes-workbench.path-resolution.v1",
        alias="schema",
    )
    phase: str = Field(min_length=1)
    status: Literal["completed", "blocked"]
    blocked_reason: str = ""
    legacy_blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    wiki_dir: str = ""
    wiki_source: str = ""
    wiki_dir_source: str = ""
    memory_path: str = ""
    config_path: str = ""
    candidates: list[JsonObject] = Field(default_factory=list)
    compat_warnings: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    human_decision_packet: JsonObject | None = None
    human_decision_packets: list[JsonObject] = Field(default_factory=list)


def blocker_from_wiki_resolution(payload: dict[str, object]) -> PathResolutionBlocker:
    resolution = WikiPathResolutionPayload.model_validate(payload)
    raw_reason = resolution.blocked_reason
    mapped_reason = _WIKI_REASON_MAP.get(raw_reason, _DEFAULT_REASON)
    required_inputs: list[PathRequiredInput] = (
        ["wiki_dir"]
        if mapped_reason in {"paths.wiki_dir_missing", "paths.ambiguous", "paths.config_invalid"}
        else []
    )
    return PathResolutionBlocker(
        blocked_reason=mapped_reason,
        legacy_blocked_reason=raw_reason,
        next_action=resolution.next_action or "Configurar wiki_dir valido em /mednotes:setup.",
        required_inputs=required_inputs,
        human_decision_required=resolution.human_decision_required,
        human_decision_packet=resolution.human_decision_packet,
    )
