"""Master plan builder for the fix-wiki workflow."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.notes.note_iter import iter_notes
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_problem import FixWikiProblem
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

FIX_WIKI_PLAN_SCHEMA = "medical-notes-workbench.fix-wiki-plan.v1"
ORDERED_FIX_WIKI_PHASES = (
    "preflight",
    "inventory",
    "vocabulary_bootstrap",
    "vocabulary_map_diagnosis",
    "style_yaml",
    "provenance_backfill",
    "hygiene",
    "duplicates",
    "taxonomy",
    "linker",
    "final_validation",
)


class FixWikiSnapshotFile(ContractModel):
    """Stable note identity used to detect stale fix-wiki plans before apply."""

    path: str
    hash: str


class FixWikiTaxonomyPlanItem(ContractModel):
    """Projection of taxonomy plan rows; only `source` drives phase scope."""

    action: str = ""
    source: str = ""
    destination: str = ""
    reason: str = ""
    blocked_reason: str = ""


class FixWikiPhase(ContractModel):
    """Single public phase row in the preview/apply plan."""

    phase: str
    status: str = "ready"
    can_apply: bool = True
    blocked_reason: str = ""
    requires_decision: str = ""
    requires_human: bool = False
    affected_paths: list[str] = Field(default_factory=list)
    planned_artifacts: list[str] = Field(default_factory=list)
    rollback_strategy: str = ""


class FixWikiPlan(ContractModel):
    """Typed root plan; `plan_hash` is calculated from its serialized payload."""

    schema_id: str = Field(default=FIX_WIKI_PLAN_SCHEMA, alias="schema")
    run_id: str
    wiki_dir: str
    snapshot_hash: str
    snapshot_files: list[FixWikiSnapshotFile] = Field(default_factory=list)
    context_packet_hashes: JsonObject = Field(default_factory=dict)
    git: JsonObject = Field(default_factory=dict)
    vocabulary_map_hash: str = ""
    phase_order: list[str]
    plan_hash: str = ""
    status: str
    problems: list[FixWikiProblem] = Field(default_factory=list)
    fix_wiki_problems: list[FixWikiProblem] = Field(default_factory=list)
    deferred_work_items: list[JsonObject] = Field(default_factory=list)
    phases: list[FixWikiPhase] = Field(default_factory=list)
    blockers: list[FixWikiProblem] = Field(default_factory=list)
    warnings: list[FixWikiProblem] = Field(default_factory=list)


def _vocabulary_blocked_reason(status: str) -> str:
    if status == "blocked_pending":
        return "vocabulary_semantic_ingestion_pending"
    if status == "blocked_human":
        return "vocabulary_map_blocked"
    return ""


def _phase(
    name: str,
    *,
    status: str = "ready",
    can_apply: bool = True,
    blocked_reason: str = "",
    requires_decision: str = "",
    requires_human: bool = False,
    affected_paths: list[str] | None = None,
    planned_artifacts: list[str] | None = None,
    rollback_strategy: str = "",
) -> FixWikiPhase:
    return FixWikiPhase(
        phase=name,
        status=status,
        can_apply=can_apply,
        blocked_reason=blocked_reason,
        requires_decision=requires_decision,
        requires_human=requires_human,
        affected_paths=affected_paths or [],
        planned_artifacts=planned_artifacts or [],
        rollback_strategy=rollback_strategy,
    )


def _hash_plan(payload: JsonObject) -> str:
    stable = {key: value for key, value in payload.items() if key != "plan_hash"}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def collect_fix_wiki_snapshot_files(wiki_dir: Path) -> list[JsonObject]:
    files: list[JsonObject] = []
    for path in iter_notes(wiki_dir) if wiki_dir.exists() else []:
        snapshot_file = FixWikiSnapshotFile(
            path=path.relative_to(wiki_dir).as_posix(),
            hash="sha256:" + file_sha256(path),
        )
        files.append(snapshot_file.to_payload())
    return files


def fix_wiki_snapshot_hash(wiki_dir: Path) -> str:
    encoded = json.dumps(
        collect_fix_wiki_snapshot_files(wiki_dir),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _context_packet_hashes(context_packets: JsonObject | None) -> JsonObject:
    hashes: JsonObject = {}
    for key, value in sorted((context_packets or {}).items()):
        path = Path(str(value))
        if path.is_file():
            hashes[key] = "sha256:" + file_sha256(path)
    return JsonObjectAdapter.validate_python(hashes)


def validate_fix_wiki_plan_snapshot(plan: object, wiki_dir: Path) -> JsonObject:
    typed_plan = FixWikiPlan.model_validate(plan)
    expected = typed_plan.snapshot_hash
    actual = fix_wiki_snapshot_hash(wiki_dir)
    if expected and expected != actual:
        return JsonObjectAdapter.validate_python(
            {
                "status": "blocked",
                "blocked_reason": "stale_fix_wiki_plan",
                "expected_snapshot_hash": expected,
                "actual_snapshot_hash": actual,
                "next_action": "Rodar /mednotes:fix-wiki novamente para gerar um plano atualizado antes de aplicar.",
            }
        )
    return JsonObjectAdapter.validate_python(
        {
            "status": "ready",
            "blocked_reason": "",
            "expected_snapshot_hash": expected,
            "actual_snapshot_hash": actual,
        }
    )


def _problem_items(problems: Sequence[object]) -> list[FixWikiProblem]:
    return [FixWikiProblem.model_validate(problem) for problem in problems]


def _taxonomy_items(items: Sequence[object]) -> list[FixWikiTaxonomyPlanItem]:
    return [FixWikiTaxonomyPlanItem.model_validate(item) for item in items]


def _json_object_list(items: Sequence[JsonObject] | None) -> list[JsonObject]:
    return [JsonObjectAdapter.validate_python(item) for item in (items or [])]


def build_fix_wiki_plan(
    *,
    run_id: str,
    wiki_dir: Path,
    snapshot_hash: str,
    vocabulary_status: str,
    problems: Sequence[object],
    taxonomy_operations: Sequence[object],
    taxonomy_blocked: Sequence[object],
    taxonomy_decision_approved: bool,
    snapshot_files: Sequence[JsonObject] | None = None,
    context_packets: JsonObject | None = None,
    git_state: JsonObject | None = None,
    vocabulary_map_hash: str = "",
    deferred_work_items: Sequence[JsonObject] | None = None,
) -> JsonObject:
    problem_items = _problem_items(problems)
    taxonomy_operation_items = _taxonomy_items(taxonomy_operations)
    taxonomy_blocked_items = _taxonomy_items(taxonomy_blocked)
    vocabulary_reason = _vocabulary_blocked_reason(vocabulary_status)
    context_artifacts = sorted(
        {
            problem.context_packet
            for problem in problem_items
            if problem.context_packet
        }
    )
    taxonomy_artifacts = (
        ["taxonomy-plan.json", *context_artifacts]
        if taxonomy_operation_items or taxonomy_blocked_items
        else context_artifacts
    )

    taxonomy_status = "ready"
    taxonomy_can_apply = True
    taxonomy_blocked_reason = ""
    taxonomy_requires_decision = ""
    taxonomy_requires_human = False
    if taxonomy_blocked_items:
        taxonomy_status = "blocked"
        taxonomy_can_apply = False
        taxonomy_blocked_reason = "taxonomy_plan_blocked"
    elif taxonomy_operation_items and not taxonomy_decision_approved:
        taxonomy_status = "needs_decision"
        taxonomy_can_apply = False
        taxonomy_blocked_reason = "taxonomy_decision_required"
        taxonomy_requires_decision = "approve_taxonomy_moves"
        taxonomy_requires_human = True

    phases = [
        _phase("preflight"),
        _phase("inventory"),
        _phase("vocabulary_bootstrap"),
        _phase("vocabulary_map_diagnosis", status=vocabulary_status or "skipped", blocked_reason=vocabulary_reason),
        _phase("style_yaml"),
        _phase("provenance_backfill"),
        _phase("hygiene"),
        _phase(
            "duplicates",
            status="blocked" if vocabulary_status == "blocked_human" else "ready",
            can_apply=vocabulary_status != "blocked_human",
            blocked_reason="vocabulary_map_blocked" if vocabulary_status == "blocked_human" else "",
        ),
        _phase(
            "taxonomy",
            status=taxonomy_status,
            can_apply=taxonomy_can_apply,
            blocked_reason=taxonomy_blocked_reason,
            requires_decision=taxonomy_requires_decision,
            requires_human=taxonomy_requires_human,
            affected_paths=[
                item.source
                for item in [*taxonomy_operation_items, *taxonomy_blocked_items]
                if item.source
            ],
            planned_artifacts=taxonomy_artifacts,
            rollback_strategy="taxonomy_receipt" if taxonomy_operation_items else "",
        ),
        _phase(
            "linker",
            status="blocked" if vocabulary_reason else "ready",
            can_apply=not bool(vocabulary_reason),
            blocked_reason=vocabulary_reason,
            planned_artifacts=["link-trigger-context.json", "link-diagnosis.json"],
            rollback_strategy="link-run-receipt",
        ),
        _phase("final_validation", can_apply=False),
    ]

    if taxonomy_blocked_items or vocabulary_status == "blocked_human":
        status = "blocked"
    elif any(problem.decision_required for problem in problem_items) or taxonomy_status == "needs_decision":
        status = "needs_decision"
    elif vocabulary_status == "blocked_pending":
        status = "blocked"
    else:
        status = "ready"

    plan = FixWikiPlan(
        run_id=run_id,
        wiki_dir=str(wiki_dir),
        snapshot_hash=snapshot_hash,
        snapshot_files=[FixWikiSnapshotFile.model_validate(item) for item in (snapshot_files or [])],
        context_packet_hashes=_context_packet_hashes(context_packets),
        git=JsonObjectAdapter.validate_python(git_state or {}),
        vocabulary_map_hash=vocabulary_map_hash,
        phase_order=list(ORDERED_FIX_WIKI_PHASES),
        status=status,
        problems=problem_items,
        fix_wiki_problems=problem_items,
        deferred_work_items=_json_object_list(deferred_work_items),
        phases=phases,
        blockers=[problem for problem in problem_items if problem.status == "blocked"],
        warnings=[problem for problem in problem_items if problem.severity == "low"],
    )
    plan.plan_hash = _hash_plan(plan.to_payload())
    return plan.to_payload()
