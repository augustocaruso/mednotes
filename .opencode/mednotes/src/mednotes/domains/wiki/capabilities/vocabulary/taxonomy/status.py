"""Human-facing taxonomy status reports."""
from __future__ import annotations

from pathlib import Path

from pydantic import ConfigDict, Field, NonNegativeInt, StrictBool, StrictStr

from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.audit import taxonomy_audit
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.migration import taxonomy_migration_plan
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.policy import TAXONOMY_POLICY_VERSION
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

TAXONOMY_STATUS_SCHEMA = "medical-notes-workbench.taxonomy-status.v1"


class _TaxonomyStatusSummaryPayload(ContractModel):
    safe_move_count: NonNegativeInt
    blocked_count: NonNegativeInt
    unmapped_top_level_dir_count: NonNegativeInt
    root_note_count: NonNegativeInt


class _TaxonomyStatusOperationPayload(ContractModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    source: StrictStr
    destination: StrictStr
    reason: StrictStr = ""


class _TaxonomyStatusBlockedItemPayload(ContractModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    source: StrictStr
    blocked_reason: StrictStr


class _TaxonomyStatusPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    wiki_dir: StrictStr
    taxonomy_policy_version: StrictStr
    status: StrictStr
    blocked_reason: StrictStr = ""
    next_action: StrictStr
    recommended_next_action: StrictStr
    required_inputs: list[StrictStr] = Field(default_factory=list)
    human_decision_required: StrictBool = False
    summary: _TaxonomyStatusSummaryPayload
    operations: list[_TaxonomyStatusOperationPayload] = Field(default_factory=list)
    # Public workflow payloads reserve root `blocked` for a boolean guardrail
    # signal. Taxonomy keeps the internal plan's blockers under an explicit
    # item-list name so the generic FSM hardener never mistakes it for state.
    blocked_items: list[_TaxonomyStatusBlockedItemPayload] = Field(default_factory=list)
    audit: JsonObject = Field(default_factory=dict)
    human_report_markdown: StrictStr = ""


def _status_from_counts(operation_count: int, blocked_count: int) -> str:
    if blocked_count:
        return "needs_review"
    if operation_count:
        return "ready_to_plan"
    return "ok"


def _next_action(status: str) -> str:
    return {
        "ok": "no_action_needed",
        "ready_to_plan": "write_plan_then_apply_if_reviewed",
        "needs_review": "review_blockers_before_apply",
    }[status]


def _blocked_reason(status: str) -> str:
    return "taxonomy_review_required" if status == "needs_review" else ""


def render_taxonomy_status_markdown(payload: JsonObject) -> str:
    typed = _TaxonomyStatusPayload.model_validate(JsonObjectAdapter.validate_python(payload))
    lines = [
        "# Taxonomy Status",
        "",
        f"status: {typed.status}",
        f"taxonomy_policy_version: {typed.taxonomy_policy_version}",
        "",
        "## Summary",
        "",
        f"- safe moves: {typed.summary.safe_move_count}",
        f"- blockers: {typed.summary.blocked_count}",
        f"- unmapped top-level dirs: {typed.summary.unmapped_top_level_dir_count}",
        f"- root notes: {typed.summary.root_note_count}",
        "",
        "## Safe Moves",
        "",
    ]
    if typed.operations:
        for item in typed.operations:
            lines.append(f"- `{item.source}` -> `{item.destination}` ({item.reason})")
    else:
        lines.append("- none")
    lines.extend(["", "## Blockers", ""])
    if typed.blocked_items:
        for item in typed.blocked_items:
            lines.append(f"- `{item.source}`: {item.blocked_reason}")
    else:
        lines.append("- none")
    lines.extend(["", "## Next Action", "", f"- {typed.recommended_next_action}"])
    return "\n".join(lines) + "\n"


def taxonomy_status(wiki_dir: Path) -> JsonObject:
    audit = taxonomy_audit(wiki_dir)
    plan = taxonomy_migration_plan(wiki_dir)
    status = _status_from_counts(len(plan["operations"]), len(plan["blocked_items"]))
    next_action = _next_action(status)
    payload = _TaxonomyStatusPayload(
        schema=TAXONOMY_STATUS_SCHEMA,
        wiki_dir=str(wiki_dir),
        taxonomy_policy_version=TAXONOMY_POLICY_VERSION,
        status=status,
        blocked_reason=_blocked_reason(status),
        next_action=next_action,
        recommended_next_action=next_action,
        required_inputs=["taxonomy_review"] if status == "needs_review" else [],
        human_decision_required=False,
        summary=_TaxonomyStatusSummaryPayload(
            safe_move_count=len(plan["operations"]),
            blocked_count=len(plan["blocked_items"]),
            unmapped_top_level_dir_count=len(audit["unmapped_top_level_dirs"]),
            root_note_count=len(audit["root_notes"]),
        ),
        operations=[JsonObjectAdapter.validate_python(item) for item in plan["operations"]],
        blocked_items=[JsonObjectAdapter.validate_python(item) for item in plan["blocked_items"]],
        audit=JsonObjectAdapter.validate_python(audit),
    ).to_payload()
    payload["human_report_markdown"] = render_taxonomy_status_markdown(payload)
    return JsonObjectAdapter.validate_python(payload)
