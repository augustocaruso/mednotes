"""Typed Git/linker state contracts.

Git status, worktree diffs and persisted link-state JSON are external adapter
inputs. These models are the first trusted shape before link diagnosis decides
trigger context, stale diagnosis or redundant blocked reruns.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

LINK_GIT_CONTEXT_SCHEMA = "medical-notes-workbench.link-git-context.v1"
LINK_STATE_SCHEMA = "medical-notes-workbench.link-state.v1"
LINK_STATE_SCHEMA_V2 = "medical-notes-workbench.link-state.v2"
GIT_TRIGGER_SOURCE = "/mednotes:link.git"

GitChangeType = Literal["created", "modified", "deleted", "renamed", "moved"]


def _without_empty_strings(payload: JsonObject) -> JsonObject:
    """Keep public Git payloads compact while the internal model stays explicit."""

    return JsonObjectAdapter.validate_python(
        {key: value for key, value in payload.items() if value not in ("", None, [])}
    )


class LinkGitChangeEvent(ContractModel):
    change_type: GitChangeType
    content_change: str = ""
    path: str = ""
    old_path: str = ""
    title: str = ""
    old_title: str = ""
    replacement_path: str = ""
    replacement_title: str = ""
    before_hash: str = ""
    after_hash: str = ""

    def to_payload(self) -> JsonObject:
        return _without_empty_strings(super().to_payload())


class LinkGitChangedPath(ContractModel):
    change_type: GitChangeType
    path: str = ""
    old_path: str = ""

    def to_payload(self) -> JsonObject:
        return _without_empty_strings(super().to_payload())


class LinkGitContext(ContractModel):
    schema_id: Literal["medical-notes-workbench.link-git-context.v1"] = Field(alias="schema")
    available: bool = Field(strict=True)
    repo_root: str
    branch: str
    head: str
    previous_link_head: str
    dirty: bool = Field(strict=True)
    changed_note_count: int = Field(ge=0, strict=True)
    changed_notes: list[LinkGitChangeEvent] = Field(default_factory=list)
    changed_paths: list[LinkGitChangedPath] = Field(default_factory=list)
    trigger_context_available: bool = Field(strict=True)
    status_hash: str
    unavailable_reason: str = ""

    def to_payload(self) -> JsonObject:
        payload = super().to_payload()
        if not self.unavailable_reason:
            payload.pop("unavailable_reason", None)
        payload["changed_notes"] = [event.to_payload() for event in self.changed_notes]
        payload["changed_paths"] = [event.to_payload() for event in self.changed_paths]
        return JsonObjectAdapter.validate_python(payload)


class LinkTriggerContextFromGit(ContractModel):
    schema_id: Literal["medical-notes-workbench.link-trigger-context.v1"] = Field(alias="schema")
    source_workflow: Literal["/mednotes:link.git"]
    changed_notes: list[LinkGitChangeEvent]
    catalog_changed: bool = Field(default=False, strict=True)
    related_notes_export_changed: bool = Field(default=False, strict=True)

    def to_payload(self) -> JsonObject:
        payload = super().to_payload()
        payload["changed_notes"] = [event.to_payload() for event in self.changed_notes]
        return JsonObjectAdapter.validate_python(payload)


class LinkState(ContractModel):
    schema_id: Literal[
        "medical-notes-workbench.link-state.v1",
        "medical-notes-workbench.link-state.v2",
    ] = Field(alias="schema")
    generated_at: str = ""
    snapshot_hash: str = ""
    git_head: str = ""
    git_status_hash: str = ""
    receipt_path: str = ""
    last_diagnosis_attempt: JsonObject | None = None

    def to_payload(self) -> JsonObject:
        payload = super().to_payload()
        if self.last_diagnosis_attempt is None:
            payload.pop("last_diagnosis_attempt", None)
        return JsonObjectAdapter.validate_python(payload)
