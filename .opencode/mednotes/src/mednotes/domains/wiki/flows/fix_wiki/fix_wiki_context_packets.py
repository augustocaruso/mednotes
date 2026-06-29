"""Redacted context packets for fix-wiki diagnostics."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict, Field

from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.schema import canonical_taxonomy_invariants
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

FIX_WIKI_CONTEXT_PACKET_SCHEMA = "medical-notes-workbench.fix-wiki-context-packet.v1"
_IGNORED_DIR_NAMES = {".git", ".obsidian", "__pycache__"}


class _PacketProjection(ContractModel):
    """Typed read-model for broad upstream artifacts rendered as redacted context."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)


class FolderTreeNode(ContractModel):
    """Redacted folder inventory: counts and paths only, never note bodies."""

    name: str
    path: str
    note_count: int = Field(ge=0)
    empty: bool
    children: list[FolderTreeNode] = Field(default_factory=list)


class ContextProblem(_PacketProjection):
    """Problem projection used by context packets, tolerant of partial fixtures."""

    domain: str = ""
    code: str = "unknown"
    severity: str = ""
    risk: str = ""
    problem: str = ""
    recommendation: str = ""
    can_autofix: bool = False
    decision_required: bool = False
    status: str = ""
    recommended_action: str = ""
    resolver: str = ""
    context_packet: str = ""
    linker_trigger_after_resolve: bool = False
    evidence: JsonObject = Field(default_factory=dict)


class HistoricalAliasMapping(_PacketProjection):
    alias: str = ""
    canonical: str = ""


class TaxonomyInvariantProjection(_PacketProjection):
    specialty_paths: list[str] = Field(default_factory=list)
    legacy_alias_mappings: list[HistoricalAliasMapping] = Field(default_factory=list)


class TaxonomyPlanProjection(_PacketProjection):
    operations: list[JsonObject] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)
    blocked: list[JsonObject] = Field(default_factory=list)


class LinkerDiagnosisProjection(_PacketProjection):
    status: str = ""
    blocked_reason: str = ""
    blocker_count: int = Field(default=0, ge=0)
    links_planned: int = Field(default=0, ge=0)
    links_rewritten: int = Field(default=0, ge=0)
    diagnosis_path: str = ""


class VocabularyMapDiagnosisProjection(_PacketProjection):
    status: str = ""
    map_hash: str = ""
    pending_semantic_ingestion_count: int = Field(default=0, ge=0)


class LinkTriggerContextProjection(_PacketProjection):
    schema_id: str = Field(default="", alias="schema")
    events: list[JsonObject] = Field(default_factory=list)
    path: str = ""


class LinkerReceiptProjection(_PacketProjection):
    status: str = ""
    receipt_path: str = ""
    changed_files: list[JsonObject | str] = Field(default_factory=list)


def _ignore_path(path: Path) -> bool:
    return (
        path.name.startswith(".")
        or path.name in _IGNORED_DIR_NAMES
        or path.name.endswith(".bak")
        or path.name.endswith(".rewrite")
        or ".bak" in path.name
        or ".rewrite" in path.name
    )


def build_folder_tree(wiki_dir: Path) -> FolderTreeNode:
    def build_node(path: Path, rel: str) -> FolderTreeNode | None:
        if path != wiki_dir and _ignore_path(path):
            return None
        dirs: list[FolderTreeNode] = []
        note_count = 0
        if path.exists():
            for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
                if _ignore_path(child):
                    continue
                if child.is_dir():
                    node = build_node(child, child.relative_to(wiki_dir).as_posix())
                    if node is not None:
                        dirs.append(node)
                elif child.is_file() and child.suffix == ".md":
                    note_count += 1
        return FolderTreeNode(
            name=path.name if path != wiki_dir else wiki_dir.name,
            path=rel,
            note_count=note_count,
            empty=note_count == 0 and not dirs,
            children=dirs,
        )

    root = build_node(wiki_dir, ".")
    return root or FolderTreeNode(name=wiki_dir.name, path=".", note_count=0, empty=True)


def _render_tree(node: FolderTreeNode, *, indent: int = 0) -> list[str]:
    suffix = " [empty]" if node.empty else ""
    name = node.name or "."
    lines = ["  " * indent + f"{name}/{suffix}"]
    for child in node.children:
        lines.extend(_render_tree(child, indent=indent + 1))
    return lines


def render_structure_context_packet(
    *,
    wiki_dir: Path,
    folder_tree: FolderTreeNode | object,
    problems: Sequence[object],
    taxonomy_plan: object | None = None,
) -> str:
    tree = FolderTreeNode.model_validate(folder_tree)
    invariants = TaxonomyInvariantProjection.model_validate(canonical_taxonomy_invariants())
    canonical_paths = invariants.specialty_paths
    legacy_aliases = [
        f"{mapping.alias} -> {mapping.canonical}"
        for mapping in invariants.legacy_alias_mappings
        if mapping.alias and mapping.canonical
    ]
    problem_items = _context_problems(problems)
    lines = [
        "# Structure Context Packet",
        "",
        f"wiki_dir: {wiki_dir}",
        "domain: structure",
        "",
        "## Canonical invariants",
        "",
        *(f"- `{path}`" for path in canonical_paths),
        "",
        "## Historical aliases",
        "",
        *(f"- `{alias}`" for alias in legacy_aliases),
        "",
        "## Folder Tree",
        "",
        "```text",
        *_render_tree(tree),
        "```",
        "",
        "## Problems",
        "",
    ]
    if problem_items:
        for problem in problem_items:
            evidence = _problem_evidence(problem)
            evidence_path = _text_field(evidence, "path") or _text_field(evidence, "target")
            lines.append(f"- {problem.code}: {evidence_path}")
    else:
        lines.append("- none")
    plan = _taxonomy_plan(taxonomy_plan)
    operations = plan.operations
    blocked = plan.blocked_items
    lines.extend(["", "## Taxonomy Plan", "", f"- operations: {len(operations)}", f"- blocked: {len(blocked)}"])
    return "\n".join(lines) + "\n"


def _json_object(value: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _text_field(source: JsonObject, key: str) -> str:
    if key not in source:
        return ""
    return str(source[key])


def _problem_evidence(problem: ContextProblem) -> JsonObject:
    return JsonObjectAdapter.validate_python(problem.evidence)


def _context_problems(problems: Sequence[object]) -> list[ContextProblem]:
    return [ContextProblem.model_validate(problem) for problem in problems]


def _taxonomy_plan(taxonomy_plan: object | None) -> TaxonomyPlanProjection:
    return TaxonomyPlanProjection.model_validate(_json_object(taxonomy_plan))


def _problem_line(problem: ContextProblem) -> str:
    evidence = _problem_evidence(problem)
    evidence_bits: list[str] = []
    for key in ("path", "target", "source", "link_diagnosis_path"):
        value = _text_field(evidence, key)
        if value:
            evidence_bits.append(f"{key}={value}")
    paths = evidence["paths"] if "paths" in evidence else []
    if isinstance(paths, list):
        evidence_bits.append("paths=" + ", ".join(str(item) for item in paths[:8]))
    suffix = f" ({'; '.join(evidence_bits)})" if evidence_bits else ""
    return f"- {problem.code}: {problem.problem}{suffix}"


def _render_generic_context_packet(
    *,
    title: str,
    domain: str,
    wiki_dir: Path,
    problems: Sequence[object],
    extra_sections: JsonObject | None = None,
) -> str:
    problem_items = _context_problems(problems)
    lines = [
        f"# {title}",
        "",
        f"wiki_dir: {wiki_dir}",
        f"domain: {domain}",
        "",
        "## Rules",
        "",
        "- This packet is redacted.",
        "- It may include paths, titles, hashes, counts, problem codes and operational summaries.",
        "- It must not include raw clinical Markdown, raw chat text or textual diffs.",
        "",
        "## Problems",
        "",
    ]
    if problem_items:
        lines.extend(_problem_line(problem) for problem in problem_items)
    else:
        lines.append("- none")
    for section, value in (extra_sections or {}).items():
        lines.extend(["", f"## {section}", "", "```json", json.dumps(value, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"


def _safe_problem(problem: ContextProblem) -> JsonObject:
    payload = problem.to_payload()
    for key in ("severity", "risk", "problem", "recommendation", "status"):
        if not getattr(problem, key):
            payload.pop(key, None)
    for key in ("recommended_action", "resolver", "context_packet", "evidence"):
        if not getattr(problem, key):
            payload.pop(key, None)
    if not problem.can_autofix:
        payload.pop("can_autofix", None)
    if not problem.decision_required:
        payload.pop("decision_required", None)
    if not problem.linker_trigger_after_resolve:
        payload.pop("linker_trigger_after_resolve", None)
    return JsonObjectAdapter.validate_python(payload)


def _write_domain_packet(
    *,
    run_dir: Path,
    wiki_dir: Path,
    domain: str,
    title: str,
    problems: Sequence[object],
    extra_sections: JsonObject | None = None,
) -> JsonObject:
    md_path = run_dir / f"{domain.replace('_', '-')}-context-packet.md"
    json_path = run_dir / f"{domain.replace('_', '-')}-context-packet.json"
    problem_items = _context_problems(problems)
    atomic_write_text(
        md_path,
        _render_generic_context_packet(
            title=title,
            domain=domain,
            wiki_dir=wiki_dir,
            problems=problems,
            extra_sections=extra_sections,
        ),
    )
    payload = {
        "schema": FIX_WIKI_CONTEXT_PACKET_SCHEMA,
        "domain": domain,
        "wiki_dir": str(wiki_dir),
        "problem_count": len(problem_items),
        "problems": [_safe_problem(problem) for problem in problem_items],
        "summaries": extra_sections or {},
    }
    for section, value in (extra_sections or {}).items():
        payload[section.lower().replace(" ", "_")] = value
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    prefix = domain
    return {
        f"{prefix}_markdown": str(md_path),
        f"{prefix}_json": str(json_path),
    }


def write_context_packets(
    *,
    run_dir: Path,
    wiki_dir: Path,
    problems: Sequence[object],
    taxonomy_plan: object | None = None,
    vocabulary_map_diagnosis: object | None = None,
    linker_diagnosis: object | None = None,
    link_trigger_context: object | None = None,
    linker_receipt: JsonObject | None = None,
) -> JsonObject:
    outputs: JsonObject = {}
    run_dir.mkdir(parents=True, exist_ok=True)
    problem_items = _context_problems(problems)
    if any(problem.domain == "structure" for problem in problem_items):
        tree = build_folder_tree(wiki_dir)
        structure_problems = [problem for problem in problem_items if problem.domain == "structure"]
        md_path = run_dir / "structure-context-packet.md"
        json_path = run_dir / "structure-context-packet.json"
        taxonomy = _taxonomy_plan(taxonomy_plan)
        atomic_write_text(
            md_path,
            render_structure_context_packet(
                wiki_dir=wiki_dir,
                folder_tree=tree,
                problems=structure_problems,
                taxonomy_plan=taxonomy_plan,
            ),
        )
        atomic_write_text(
            json_path,
            json.dumps(
                {
                    "schema": FIX_WIKI_CONTEXT_PACKET_SCHEMA,
                    "domain": "structure",
                    "wiki_dir": str(wiki_dir),
                    "folder_tree": tree.to_payload(),
                    "problem_count": len(structure_problems),
                    "problems": [_safe_problem(problem) for problem in structure_problems],
                    "taxonomy_plan_summary": {
                        "operation_count": len(taxonomy.operations),
                        "blocked_count": len(taxonomy.blocked_items),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        outputs["structure_markdown"] = str(md_path)
        outputs["structure_json"] = str(json_path)
    identity_problems = [problem for problem in problem_items if problem.domain == "identity"]
    if identity_problems:
        outputs.update(
            _write_domain_packet(
                run_dir=run_dir,
                wiki_dir=wiki_dir,
                domain="identity",
                title="Identity Context Packet",
                problems=identity_problems,
                extra_sections={
                    "Identity Invariants": {
                        "one_note_many_meanings": "identity.atomicity.one_note_multiple_meanings",
                        "many_notes_one_meaning": "identity.duplication.same_meaning_multiple_notes",
                        "canonical_rule": "1 meaning = 1 canonical note",
                    }
                },
            )
        )
    content_problems = [problem for problem in problem_items if problem.domain == "content"]
    if content_problems:
        outputs.update(
            _write_domain_packet(
                run_dir=run_dir,
                wiki_dir=wiki_dir,
                domain="content",
                title="Content Context Packet",
                problems=content_problems,
                extra_sections={
                    "Rewrite Constraints": {
                        "preserve": ["YAML aliases/operational tags/images_*", "footer", "images", "embeds", "code blocks"],
                        "rewrite_scope": "Only paths listed in problems[].evidence.path",
                    }
                },
            )
        )
    graph_problems = [problem for problem in problem_items if problem.domain == "knowledge_graph"]
    if graph_problems or linker_diagnosis or link_trigger_context or linker_receipt or vocabulary_map_diagnosis:
        linker = LinkerDiagnosisProjection.model_validate(_json_object(linker_diagnosis))
        vocabulary = VocabularyMapDiagnosisProjection.model_validate(_json_object(vocabulary_map_diagnosis))
        trigger = LinkTriggerContextProjection.model_validate(_json_object(link_trigger_context))
        receipt = LinkerReceiptProjection.model_validate(_json_object(linker_receipt))
        linker_summary = {
            "status": linker.status,
            "blocked_reason": linker.blocked_reason,
            "blocker_count": linker.blocker_count,
            "links_planned": linker.links_planned,
            "links_rewritten": linker.links_rewritten,
            "diagnosis_path": linker.diagnosis_path,
        }
        outputs.update(
            _write_domain_packet(
                run_dir=run_dir,
                wiki_dir=wiki_dir,
                domain="knowledge_graph",
                title="Knowledge Graph Context Packet",
                problems=graph_problems,
                extra_sections={
                    "Boundary": {
                        "owner": "/mednotes:link",
                        "fix_wiki_role": "orchestrates linker diagnosis/apply and reports the receipt",
                    },
                    "Vocabulary Map": {
                        "status": vocabulary.status,
                        "map_hash": vocabulary.map_hash,
                        "pending_semantic_ingestion_count": vocabulary.pending_semantic_ingestion_count,
                    },
                    "Trigger Context": {
                        "schema": trigger.schema_id,
                        "event_count": len(trigger.events),
                        "path": trigger.path,
                    },
                    "Linker Diagnosis": linker_summary,
                    "Linker Receipt": {
                        "status": receipt.status,
                        "receipt_path": receipt.receipt_path,
                        "files_changed": len(receipt.changed_files),
                    },
                },
            )
        )
    return outputs
