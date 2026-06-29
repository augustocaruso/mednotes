"""Safe subagent planning for Wiki workflows."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictStr, field_validator

from mednotes.domains.wiki.capabilities.atomicity.atomicity import build_atomicity_split_plan
from mednotes.domains.wiki.capabilities.notes.artifacts import discover_artifact_manifests
from mednotes.domains.wiki.capabilities.notes.meaning_planner import plan_meaning_work_items
from mednotes.domains.wiki.capabilities.notes.note_plan import (
    PLANNED_MEANING_ACTION,
    TRIAGE_NOTE_PLAN_V2_SCHEMA,
    note_plan_summary,
    parse_triage_note_plan,
)
from mednotes.domains.wiki.capabilities.notes.raw_chats import covered_raw_chat_index, list_by_status, read_note_meta
from mednotes.domains.wiki.capabilities.specialist.plan_attestation import attach_subagent_plan_attestation
from mednotes.domains.wiki.capabilities.style.style import validate_wiki_style
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key
from mednotes.domains.wiki.capabilities.vocabulary.vocabulary_curator_batch import build_vocabulary_curator_batch_plan
from mednotes.domains.wiki.common import SUBAGENT_PLAN_SCHEMA, MedOpsError, ValidationError
from mednotes.domains.wiki.config import MedConfig, _user_state_dir
from mednotes.domains.wiki.contracts.agents import SubagentBatchPlan
from mednotes.domains.wiki.contracts.workflow_guardrails import (
    PROCESS_CHATS_REQUIRED_INPUTS,
    STYLE_REWRITE_REQUIRED_INPUTS,
    annotate_payload,
    note_target_index,
    plan_status,
)
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    RejectedAutomation,
    WorkflowDecision,
)
from mednotes.kernel.agent_directive import AgentDirective
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.public_report import WorkflowPublicReport
from mednotes.platform.user_config import ParallelismConfig

_DEFAULT_PARALLELISM = ParallelismConfig()
DEFAULT_PROCESS_CHATS_MAX_CONCURRENCY = _DEFAULT_PARALLELISM.process_chats_max_parallel_architects
DEFAULT_STYLE_REWRITE_MAX_CONCURRENCY = _DEFAULT_PARALLELISM.fix_wiki_max_parallel_rewrites
CANONICAL_MERGE_PLAN_SCHEMA = "medical-notes-workbench.canonical-merge-plan.v1"


class _PlannedMeaningTarget(ContractModel):
    """Typed target identity extracted from a validated triage note plan."""

    id: StrictStr
    title: StrictStr
    target_key: StrictStr


class _PlannedMatch(ContractModel):
    """Typed source-to-target match used to decide canonical merge routes."""

    raw_file: StrictStr
    work_id: StrictStr
    id: StrictStr
    title: StrictStr


class _TriageNotePlanItem(BaseModel):
    """Typed view of a triage note-plan item used for routing decisions."""

    model_config = ConfigDict(extra="ignore")

    id: StrictStr = ""
    action: StrictStr = ""
    staged_title: StrictStr = ""
    title: StrictStr = ""


class _TriageNotePlan(BaseModel):
    """Validated triage note plan plus its public JSON payload.

    The parent workflow still passes the full note-plan payload to downstream
    contracts, but all routing decisions in this module read this typed view.
    """

    model_config = ConfigDict(extra="ignore")

    schema_: StrictStr = Field(default="", alias="schema")
    items: list[_TriageNotePlanItem] = Field(default_factory=list)
    _payload: JsonObject = PrivateAttr(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: object) -> _TriageNotePlan:
        json_payload = JsonObjectAdapter.validate_python(payload)
        plan = cls.model_validate(json_payload)
        plan._payload = json_payload
        return plan

    def public_payload(self) -> JsonObject:
        return dict(self._payload)


class _DuplicateTarget(ContractModel):
    """Typed duplicate target; JSON projection happens only at boundaries."""

    id: StrictStr = ""
    title: StrictStr = ""
    target_key: StrictStr
    conflict_type: Literal["ambiguous_existing_wiki_note", "existing_wiki_note", "planned_in_batch"]
    existing_paths: list[StrictStr] = Field(default_factory=list)
    planned_matches: list[_PlannedMatch] = Field(default_factory=list)


class _SubagentAnnotationPayload(BaseModel):
    """Typed read model for annotating a public subagent plan payload."""

    model_config = ConfigDict(extra="ignore")

    phase: StrictStr = ""
    agent: StrictStr = ""
    work_items: list[JsonObject] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)


class _SubagentAnnotationWorkItem(BaseModel):
    """Typed read model for one work item while preserving its raw output."""

    model_config = ConfigDict(extra="ignore")

    phase: StrictStr = ""
    expected_output_schema: JsonObject | StrictStr | None = None


class _SubagentGeneratedPlanSummary(BaseModel):
    """Minimal typed view for generated plans before concurrency policy."""

    model_config = ConfigDict(extra="ignore")

    item_count: int = Field(default=0, ge=0)


class _StyleRewriteAuditReport(BaseModel):
    """Typed style-audit row used to plan specialist rewrite work."""

    model_config = ConfigDict(extra="ignore")

    requires_llm_rewrite: bool = False
    path: StrictStr = ""
    title: StrictStr = ""
    rewrite_prompt: StrictStr = ""
    errors: list[JsonObject] = Field(default_factory=list)
    warnings: list[JsonObject] = Field(default_factory=list)

    @field_validator("path", "title", "rewrite_prompt", mode="before")
    @classmethod
    def _optional_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("errors", "warnings", mode="before")
    @classmethod
    def _optional_json_list(cls, value: object) -> list[JsonObject]:
        if not isinstance(value, list):
            return []
        return [JsonObjectAdapter.validate_python(item) for item in value if isinstance(item, dict)]


class _StyleRewriteAuditSummary(BaseModel):
    """Typed style-audit summary used in the public subagent plan payload."""

    model_config = ConfigDict(extra="ignore")

    schema_: StrictStr = Field(default="", alias="schema")
    wiki_dir: StrictStr = ""
    file_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    reports: list[_StyleRewriteAuditReport] = Field(default_factory=list)

class _RawChatPlanningRow(BaseModel):
    """Typed raw-chat listing row used by subagent planning."""

    model_config = ConfigDict(extra="ignore")

    path: StrictStr
    titulo_triagem: StrictStr = ""
    fonte_id: StrictStr = ""


class _ReadNoteMeta(BaseModel):
    """Typed metadata slice read from raw chat YAML/frontmatter."""

    model_config = ConfigDict(extra="ignore")

    note_plan: StrictStr = ""


class _MeaningPlannerResult(BaseModel):
    """Typed view of meaning-planner output before architect fan-out."""

    model_config = ConfigDict(extra="ignore")

    work_items: list[JsonObject] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)
    next_action: StrictStr = ""


class _ArchitectParsedItem(ContractModel):
    """Internal typed architect planning row before public payload emission."""

    item: JsonObject
    note_plan: _TriageNotePlan
    targets: list[_PlannedMeaningTarget] = Field(default_factory=list)
    duplicate_targets: list[_DuplicateTarget] = Field(default_factory=list)


def _json_str_field(payload: JsonObject, key: str, default: str = "") -> str:
    """Read an optional public JSON string after the object boundary is typed."""

    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        return default
    return str(value)


def _json_list_field(payload: JsonObject, key: str) -> list[object]:
    """Read an optional public JSON list without `.get()` fallback semantics."""

    if key not in payload:
        return []
    value = payload[key]
    return list(value) if isinstance(value, list) else []


def _json_object_list_field(payload: JsonObject, key: str) -> list[JsonObject]:
    """Read an optional list of public JSON objects from a typed payload."""

    return [JsonObjectAdapter.validate_python(item) for item in _json_list_field(payload, key) if isinstance(item, dict)]


def _json_object_field(payload: JsonObject, key: str) -> JsonObject | None:
    """Read one optional public JSON object from a typed payload."""

    if key not in payload:
        return None
    value = payload[key]
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else None


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_text).strip("-._").lower()
    return slug or "raw"


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def configured_subagent_max_concurrency(config: MedConfig, phase: str) -> int:
    parallelism = config.user_config.parallelism
    defaults = {
        "triage": parallelism.process_chats_max_parallel_triagers,
        "architect": parallelism.process_chats_max_parallel_architects,
        "style-rewrite": parallelism.fix_wiki_max_parallel_rewrites,
        "note-merge": parallelism.fix_wiki_max_parallel_rewrites,
        "atomicity-split": parallelism.fix_wiki_max_parallel_rewrites,
        "vocabulary-curation": parallelism.link_max_parallel_curators,
    }
    if phase not in defaults:
        raise ValidationError(f"Unknown subagent planning phase: {phase}")
    return defaults[phase]


def _chunked(items: list[JsonObject], size: int) -> list[list[JsonObject]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _batch_refs(items: list[JsonObject], size: int) -> list[JsonObject]:
    batches: list[JsonObject] = []
    for batch_index, batch in enumerate(_chunked(items, size), start=1):
        batches.append(
            {
                "batch": batch_index,
                "max_concurrency": size,
                "item_count": len(batch),
                "work_ids": [str(item["work_id"]) for item in batch],
                "owner_keys": [str(item["owner_key"]) for item in batch],
            }
        )
    return batches


def _expected_output_schema_for_phase(phase: str) -> dict[str, str]:
    schemas = {
        "triage": {
            "schema": "medical-notes-workbench.triage-output.v1",
            "description": "Saida estruturada do triager com note_plan v2.",
        },
        "architect": {
            "schema": "medical-notes-workbench.architect-output.v1",
            "description": "Nota ou rewrite produzido pelo architect.",
        },
        "style-rewrite": {
            "schema": "medical-notes-workbench.style-rewrite-output-attestation.v1",
            "description": (
                "O subagente escreve Markdown em temp_output; o pai gera a atestação assinada do Workbench com "
                "finalize-style-rewrite-output."
            ),
        },
        "note-merge": {
            "schema": "medical-notes-workbench.note-merge-output.v1",
            "description": "Merge semantico para apply-note-merge.",
        },
        "atomicity-split": {
            "schema": "medical-notes-workbench.atomicity-split-bundle.v1",
            "description": "Bundle de split atomico para apply-atomicity-split.",
        },
        "atomicity_split": {
            "schema": "medical-notes-workbench.atomicity-split-bundle.v1",
            "description": "Bundle de split atomico para apply-atomicity-split.",
        },
        "vocabulary-curation": {
            "schema": "medical-notes-workbench.note-semantic-ingestion.v1",
            "description": "JSON de curadoria semantica para apply-curator-batch.",
        },
        "vocabulary_curation": {
            "schema": "medical-notes-workbench.note-semantic-ingestion.v1",
            "description": "JSON de curadoria semantica para apply-curator-batch.",
        },
    }
    if phase in schemas:
        return dict(schemas[phase])
    return dict(schemas["architect"])


def _annotate_work_items_for_subagent_contract(payload: JsonObject) -> JsonObject:
    typed_payload = _SubagentAnnotationPayload.model_validate(payload)
    phase = typed_payload.phase
    agent = typed_payload.agent
    annotated = dict(payload)
    work_items = []
    for raw_item in typed_payload.work_items:
        typed_item = _SubagentAnnotationWorkItem.model_validate(raw_item)
        item = dict(raw_item)
        item_phase = typed_item.phase or phase
        item.setdefault("phase", item_phase)
        item.setdefault("agent", agent)
        if not isinstance(typed_item.expected_output_schema, dict):
            item["expected_output_schema"] = _expected_output_schema_for_phase(item_phase)
        work_items.append(item)
    annotated["work_items"] = work_items
    annotated.setdefault("blocked_item_count", len(typed_payload.blocked_items))
    annotated.setdefault("parent_applies_outputs", True)
    return annotated


def _typed_subagent_plan_payload(payload: JsonObject) -> JsonObject:
    typed_payload = attach_subagent_plan_attestation(_annotate_work_items_for_subagent_contract(payload))
    SubagentBatchPlan.model_validate(typed_payload)
    return typed_payload


def _default_subagent_temp_root(phase: str) -> Path:
    base = _user_state_dir() / "tmp" / "agent-work"
    if phase == "triage":
        return base / "process-chats" / "triage"
    if phase == "architect":
        return base / "process-chats"
    if phase in {"style-rewrite", "note-merge", "atomicity-split", "atomicity_split"}:
        return base / "fix-wiki"
    if phase in {"vocabulary-curation", "vocabulary_curation"}:
        return base / "vocabulary-curation"
    return base / _slug(phase)


def _style_rewrite_subagent_output_contract() -> JsonObject:
    return {
        "schema": "medical-notes-workbench.subagent-output-contract.v1",
        "write_markdown_to": "temp_output",
        "subagent_must_create_attestation": False,
        "subagent_must_create_specialist_task_run_receipt": False,
        "parent_must_not_fabricate_specialist_task_run_receipt": True,
        "parent_may_call_specialist_task_receipt_finalizer": True,
        "official_receipt_finalizers": [
            "call_specialist_model",
            "finalize-agy-specialist-task",
            "finalize-opencode-specialist-task",
        ],
        "missing_specialist_task_run_receipt_action": "run_official_receipt_finalizer_or_stop",
        "parent_only_fields": ["output_attestation_path"],
        "runner_only_fields": ["specialist_task_run_receipt_path"],
        "attestation_created_by": "finalize-style-rewrite-output",
    }


def _extension_root() -> Path:
    from mednotes.platform.paths import extension_root

    return extension_root()


def _agent_readable_docs_root(root: Path) -> Path:
    """Prefer source docs when running the built extension from ignored dist/ in dev."""

    if root.name == "gemini-cli-extension" and root.parent.name == "dist":
        source_root = root.parents[1] / "extension"
        required = (
            source_root / "docs" / "agent-prompt-hardening.md",
            source_root / "docs" / "knowledge-architect.md",
            source_root / "docs" / "semantic-linker.md",
        )
        if all(path.exists() for path in required):
            return source_root
    return root


def _style_rewrite_context_docs() -> JsonObject:
    root = _agent_readable_docs_root(_extension_root())
    return {
        "schema": "medical-notes-workbench.subagent-context-docs.v1",
        "required_read_files": [
            str(root / "docs" / "agent-prompt-hardening.md"),
            str(root / "docs" / "knowledge-architect.md"),
            str(root / "docs" / "semantic-linker.md"),
        ],
        "forbidden_discovery_roots": [str(Path.home())],
        "agent_instruction": (
            "Leia os required_read_files empacotados antes de redigir a nota. "
            "Não faça descoberta ampla em forbidden_discovery_roots; se um doc faltar, bloqueie como packaged_agent_template_unavailable."
        ),
    }


def _planned_meaning_targets(note_plan: _TriageNotePlan) -> list[_PlannedMeaningTarget]:
    targets: list[_PlannedMeaningTarget] = []
    for item in note_plan.items:
        if item.action != PLANNED_MEANING_ACTION:
            continue
        title = str(item.staged_title or item.title).strip()
        if not title:
            continue
        targets.append(
            _PlannedMeaningTarget(
                id=item.id.strip(),
                title=title,
                target_key=normalize_key(title),
            )
        )
    return targets


def _launchable_work_item(item: JsonObject, *, write_policy: str = "temp_note_allowed") -> JsonObject:
    item["launchable"] = True
    item["write_policy"] = write_policy
    return item


def _non_launchable_blocked_item(item: JsonObject, *, write_policy: str = "no_temp_note") -> JsonObject:
    item["launchable"] = False
    item["write_policy"] = write_policy
    item.pop("temp_dir", None)
    item.pop("temp_output", None)
    return item


def _duplicate_next_action(blocked_reason: str = "duplicate_planned_meaning_targets") -> str:
    if blocked_reason == "canonical_merge_required":
        return (
            "Chame architect para merge canônico no alvo existente: gerar rewrite completo com delta validado, "
            "ou ajustar a triagem para not_a_note se não houver delta."
        )
    if blocked_reason == "human_decision_required.ambiguous_canonical_target":
        return (
            "Escolha explicitamente o alvo canônico antes de lançar architects; depois "
            "replaneje ou ajuste a triagem para planned_meaning/not_a_note."
        )
    return (
        "Revise o note_plan antes de arquitetura: converta duplicatas para "
        "not_a_note ou consolide fontes em um unico planned_meaning."
    )


def _duplicate_blocked_reason(duplicate_targets: Sequence[_DuplicateTarget]) -> str:
    if any(target.conflict_type == "ambiguous_existing_wiki_note" for target in duplicate_targets):
        return "human_decision_required.ambiguous_canonical_target"
    if any(target.conflict_type == "existing_wiki_note" for target in duplicate_targets):
        return "canonical_merge_required"
    return "duplicate_planned_meaning_targets"


def _decision_options_for_existing_paths(paths: Sequence[object]) -> list[JsonObject]:
    options: list[JsonObject] = []
    for index, path in enumerate(paths, start=1):
        label = str(path)
        options.append(
            {
                "id": f"use_existing_{index}",
                "label": label,
                "value": label,
                "consequence": "Consolidar informação nova nesse alvo canônico ou marcar como not_a_note.",
            }
        )
    return options


def _decision_options_for_planned_matches(matches: Sequence[_PlannedMatch]) -> list[JsonObject]:
    return [
        {
            "id": "canonical_merge",
            "label": "Fundir em uma nota canônica",
            "value": "canonical_merge",
            "consequence": "Um architect consolida todas as fontes e preserva múltiplas referências.",
        },
        {
            "id": "split_triage",
            "label": "Separar triagem",
            "value": "split_triage",
            "consequence": "Ajustar note_plan para separar temas ou remover duplicata antes da arquitetura.",
        },
        {
            "id": "mark_not_a_note",
            "label": "Marcar como já coberto",
            "value": "not_a_note",
            "consequence": "Atualizar note_plan como not_a_note quando a informação não exigir nota nova.",
        },
    ] + [
        {
            "id": f"inspect_{index}",
            "label": f"Inspecionar {Path(match.raw_file).name}",
            "value": match.raw_file,
            "consequence": "Usar este raw como evidência antes de escolher a rota.",
        }
        for index, match in enumerate(matches[:3], start=1)
    ]


def _planned_match_payloads(matches: Sequence[_PlannedMatch]) -> list[JsonObject]:
    """Serialize typed planned matches before they cross a JSON/public boundary."""

    return [match.to_payload() for match in matches]


def _packet_for_ambiguous_target(
    *,
    target_key: str,
    target_title: str,
    options: Sequence[object],
    planned_matches: list[_PlannedMatch] | None = None,
) -> JsonObject:
    option_payload = (
        _decision_options_for_existing_paths(options)
        if options and not isinstance(options[0], dict)
        else _decision_options_for_planned_matches(planned_matches or [])
    )
    return _ask_human_packet(
        kind="ambiguous_canonical_target",
        phase="architect",
        blocked_reason="human_decision_required.ambiguous_canonical_target",
        target_kind="wiki_note",
        target_key=target_key,
        question=f"Qual alvo canônico deve receber '{target_title}'?",
        options=option_payload,
        resume_action="Registrar a escolha no note_plan e reexecutar plan-subagents --phase architect.",
        context={"planned_matches": _planned_match_payloads(planned_matches or [])},
        evidence_summary=f"'{target_title}' tem mais de um alvo canônico plausível.",
        developer_summary="Ambiguous planned_meaning target after accent/case normalization.",
    )


def _packet_for_existing_canonical_target(target: _DuplicateTarget) -> JsonObject:
    title = target.title
    target_key = target.target_key
    existing_paths = target.existing_paths
    return _ask_human_packet(
        kind="canonical_merge_required",
        phase="architect",
        blocked_reason="canonical_merge_required",
        target_kind="existing_wiki_note",
        target_key=target_key,
        question=f"Como tratar a informação nova planejada para nota existente '{title}'?",
        options=[
            *_decision_options_for_existing_paths(existing_paths),
            {
                "id": "rename_new_note",
                "label": "Criar nota separada com outro título",
                "value": "rename_new_note",
                "consequence": "Ajustar staged_title no note_plan e repetir arquitetura.",
            },
        ],
        resume_action="Escolher rota de merge/renomeação, ajustar note_plan e reexecutar plan-subagents --phase architect.",
        context={"existing_paths": existing_paths, "target_title": title},
        evidence_summary=f"'{title}' já existe na Wiki e pode receber merge ou nota separada.",
        developer_summary="Existing canonical target requires an editorial route before architect fan-out.",
    )


def _ask_human_packet(
    *,
    kind: str,
    phase: str,
    blocked_reason: str,
    target_kind: str,
    target_key: str,
    question: str,
    options: list[JsonObject],
    resume_action: str,
    context: JsonObject,
    evidence_summary: str,
    developer_summary: str,
) -> JsonObject:
    recommended_option_id = _json_str_field(options[0], "id") if options else "inspect_first"
    if not options:
        options = [
            {
                "id": "inspect_first",
                "label": "Inspecionar antes de escolher",
                "value": "inspect_first",
                "consequence": "Replanejar depois de revisar os candidatos.",
            }
        ]
    decision = WorkflowDecision(
        kind="ask_human",
        phase=phase,
        reason_code=blocked_reason,
        public_summary=question,
        developer_summary=developer_summary,
        evidence=[
            DecisionEvidence(
                summary=evidence_summary,
                technical_code=blocked_reason,
                source=phase,
                candidates=[{"target_key": target_key, **context}],
                risk="Escolha automatica pode fundir ou separar notas canônicas incorretamente.",
            )
        ],
        rejected_automations=[
            RejectedAutomation(kind="auto_fix", reason_code="ambiguous_canonical_target", reason="Nao ha alvo unico dominante para corrigir automaticamente."),
            RejectedAutomation(kind="auto_defer", reason_code="blocks_architect", reason="Pular a escolha impediria cobertura correta do raw chat."),
            RejectedAutomation(kind="auto_plan", reason_code="plan_needs_canonical_target", reason="O plano precisa de um alvo canônico antes de lançar subagentes."),
        ],
        next_action=resume_action,
        resume_action=resume_action,
        recommended_option_id=recommended_option_id,
        options=options,
    )
    packet = decision.to_human_decision_packet()
    packet["kind"] = kind
    packet["type"] = kind
    packet["target_kind"] = target_kind
    packet["target_key"] = target_key
    packet.setdefault("context", {}).update(context)
    return packet


def _single_planned_meaning_target(parsed: _ArchitectParsedItem, target_key: str) -> bool:
    targets = parsed.targets
    if len(targets) != 1:
        return False
    return targets[0].target_key == target_key


def _find_note_plan_item(note_plan: _TriageNotePlan, item_id: str) -> JsonObject:
    raw_items = _json_list_field(note_plan.public_payload(), "items")
    for item in raw_items:
        if isinstance(item, dict):
            payload = JsonObjectAdapter.validate_python(item)
            if _json_str_field(payload, "id").strip() == item_id:
                return payload
    return {}


def _artifact_payload_for_raw(config: MedConfig, raw_file: Path) -> JsonObject:
    artifact_manifests = discover_artifact_manifests(raw_file, artifact_dir=config.artifact_dir)
    payload: JsonObject = {
        "artifact_manifest_count": len(artifact_manifests),
        "artifact_count": sum(len(manifest.artifacts) for manifest in artifact_manifests),
    }
    if artifact_manifests:
        payload["artifact_manifests"] = [manifest.to_json() for manifest in artifact_manifests]
    return payload


def _canonical_merge_work_item(
    config: MedConfig,
    spec: JsonObject,
    *,
    target_key: str,
    planned_matches: list[_PlannedMatch],
    parsed_by_work_id: dict[str, _ArchitectParsedItem],
    temp_root: Path,
    index: int,
) -> JsonObject:
    target_title = planned_matches[0].title
    work_id = f"canonical-merge-{index:03d}-{_slug(target_title)}"
    sources: list[JsonObject] = []
    artifact_manifest_count = 0
    artifact_count = 0
    artifact_manifests: list[JsonObject] = []
    for match in planned_matches:
        parsed = parsed_by_work_id[match.work_id]
        item = parsed.item
        raw_file = Path(str(item["raw_file"]))
        note_plan_item = _find_note_plan_item(parsed.note_plan, match.id)
        source: JsonObject = {
            "raw_file": str(raw_file),
            "work_id": str(item["work_id"]),
            "fonte_id": _json_str_field(item, "fonte_id"),
            "titulo_triagem": _json_str_field(item, "titulo_triagem"),
            "note_plan_item_id": match.id,
            "planned_title": match.title,
            "note_plan_item": note_plan_item,
        }
        artifact_payload = _artifact_payload_for_raw(config, raw_file)
        artifact_manifest_count += int(artifact_payload["artifact_manifest_count"])
        artifact_count += int(artifact_payload["artifact_count"])
        artifact_manifests.extend(_json_object_list_field(artifact_payload, "artifact_manifests"))
        sources.append(source)

    temp_dir = temp_root / work_id
    merge_plan_sources = [
        {
            "raw_file": source["raw_file"],
            "note_plan_item_id": source["note_plan_item_id"],
            "planned_title": source["planned_title"],
            "fonte_id": source["fonte_id"],
        }
        for source in sources
    ]
    item: JsonObject = {
        "work_id": work_id,
        "agent": spec["agent"],
        "item_type": "canonical_merge",
        "merge_action": "create_new_canonical_note",
        "target_kind": "new_wiki_note",
        "target_title": target_title,
        "target_key": target_key,
        "owner_key": f"target:{target_key}",
        "source_count": len(sources),
        "raw_files": [source["raw_file"] for source in sources],
        "sources": sources,
        "canonical_merge_plan": {
            "schema": CANONICAL_MERGE_PLAN_SCHEMA,
            "target_kind": "new_wiki_note",
            "target_title": target_title,
            "target_key": target_key,
            "sources": merge_plan_sources,
            "required_delta_per_source": True,
            "required_multi_reference_provenance": True,
        },
        "artifact_manifest_count": artifact_manifest_count,
        "artifact_count": artifact_count,
        "temp_dir": str(temp_dir),
        "temp_output": str(temp_dir / f"{_slug(target_title)}.md"),
    }
    if artifact_manifests:
        item["artifact_manifests"] = artifact_manifests
    return _launchable_work_item(item)


def _existing_canonical_merge_work_item(
    config: MedConfig,
    spec: JsonObject,
    *,
    target_key: str,
    planned_matches: list[_PlannedMatch],
    parsed_by_work_id: dict[str, _ArchitectParsedItem],
    existing_paths: Sequence[object],
    temp_root: Path,
    index: int,
) -> JsonObject:
    target_title = planned_matches[0].title
    existing_path = str(existing_paths[0])
    target_path = config.wiki_dir / existing_path
    work_id = f"canonical-existing-merge-{index:03d}-{_slug(target_path.stem)}"
    sources: list[JsonObject] = []
    artifact_manifest_count = 0
    artifact_count = 0
    artifact_manifests: list[JsonObject] = []
    for match in planned_matches:
        parsed = parsed_by_work_id[match.work_id]
        item = parsed.item
        raw_file = Path(str(item["raw_file"]))
        note_plan_item = _find_note_plan_item(parsed.note_plan, match.id)
        source: JsonObject = {
            "raw_file": str(raw_file),
            "work_id": str(item["work_id"]),
            "fonte_id": _json_str_field(item, "fonte_id"),
            "titulo_triagem": _json_str_field(item, "titulo_triagem"),
            "note_plan_item_id": match.id,
            "planned_title": match.title,
            "note_plan_item": note_plan_item,
        }
        artifact_payload = _artifact_payload_for_raw(config, raw_file)
        artifact_manifest_count += int(artifact_payload["artifact_manifest_count"])
        artifact_count += int(artifact_payload["artifact_count"])
        artifact_manifests.extend(_json_object_list_field(artifact_payload, "artifact_manifests"))
        sources.append(source)

    temp_dir = temp_root / work_id
    merge_plan_sources = [
        {
            "raw_file": source["raw_file"],
            "note_plan_item_id": source["note_plan_item_id"],
            "planned_title": source["planned_title"],
            "fonte_id": source["fonte_id"],
        }
        for source in sources
    ]
    item: JsonObject = {
        "work_id": work_id,
        "agent": spec["agent"],
        "item_type": "canonical_merge",
        "merge_action": "update_existing_canonical_note",
        "target_kind": "existing_wiki_note",
        "target_title": target_path.stem,
        "requested_title": target_title,
        "target_key": target_key,
        "target_path": str(target_path),
        "existing_paths": [str(path) for path in existing_paths],
        "owner_key": f"target:{target_key}",
        "source_count": len(sources),
        "raw_files": [source["raw_file"] for source in sources],
        "sources": sources,
        "canonical_merge_plan": {
            "schema": CANONICAL_MERGE_PLAN_SCHEMA,
            "target_kind": "existing_wiki_note",
            "target_title": target_path.stem,
            "requested_title": target_title,
            "target_key": target_key,
            "target_path": str(target_path),
            "existing_paths": [str(path) for path in existing_paths],
            "sources": merge_plan_sources,
            "required_delta_per_source": True,
            "required_multi_reference_provenance": True,
        },
        "apply_command": "apply-canonical-merge",
        "artifact_manifest_count": artifact_manifest_count,
        "artifact_count": artifact_count,
        "temp_dir": str(temp_dir),
        "temp_output": str(temp_dir / f"{target_path.stem}.rewrite.md"),
    }
    if artifact_manifests:
        item["artifact_manifests"] = artifact_manifests
    return _launchable_work_item(item, write_policy="existing_note_rewrite")


def _canonical_merge_blocked_item(
    *,
    target_key: str,
    planned_matches: list[_PlannedMatch],
    reason: str,
    message: str,
) -> JsonObject:
    first_match = planned_matches[0]
    item: JsonObject = {
        "work_id": f"canonical-merge-blocked-{_slug(target_key)}",
        "item_type": "canonical_merge",
        "blocked_reason": reason,
        "target_key": target_key,
        "target_title": first_match.title,
        "planned_matches": [match.to_payload() for match in planned_matches],
        "reason": message,
        "next_action": _duplicate_next_action(reason),
    }
    if reason == "human_decision_required.ambiguous_canonical_target":
        packet = _packet_for_ambiguous_target(
            target_key=target_key,
            target_title=first_match.title or target_key,
            options=[],
            planned_matches=planned_matches,
        )
        item["human_decision_packet"] = packet
    return _non_launchable_blocked_item(item)


def _decision_packets_from_blocked_items(blocked_items: list[JsonObject]) -> list[JsonObject]:
    packets: list[JsonObject] = []
    seen: set[str] = set()
    for item in blocked_items:
        packet = _json_object_field(item, "human_decision_packet")
        if packet is not None:
            packet_key = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if packet_key not in seen:
                packets.append(packet)
                seen.add(packet_key)
        for packet in _json_object_list_field(item, "human_decision_packets"):
            packet_key = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if packet_key in seen:
                continue
            packets.append(packet)
            seen.add(packet_key)
    return packets


def _is_canonical_merge_work_item(item: JsonObject) -> bool:
    """Count only typed work-item intent, not arbitrary payload text."""

    return _json_str_field(item, "item_type") == "canonical_merge"


def _with_decision_packets(payload: JsonObject, blocked_items: list[JsonObject]) -> JsonObject:
    packets = _decision_packets_from_blocked_items(blocked_items)
    if not packets:
        return payload
    payload["human_decision_packets"] = packets
    if len(packets) == 1:
        payload["human_decision_packet"] = packets[0]
    return payload


def _process_chats_no_pending_directive() -> JsonObject:
    return AgentDirective.model_validate(
        {
            "workflow": "/mednotes:process-chats",
            "run_id": "process-chats-architect-no-pending",
            "control": {
                "status": "completed",
                "state": "no_pending",
                "phase": "architect",
                "reason": "no_pending",
                "capabilities": {"continue": False, "final_report": True},
                "effects": [],
                "blockers": [],
                "resume": "",
                "report": {"requires": ["public_report"]},
                "limits": {"raw_content": False, "absolute_paths": False, "ad_hoc_scripts": False},
            },
            "summary": "Nenhum chat novo para processar.",
            "instructions": [
                "This completes /mednotes:process-chats because there are no new chats to process.",
                "Use reports.public_report.lines as the public response.",
                "Do not run validate-wiki, fix-wiki, run-linker, publish-batch or subagents.",
                "Report zero vault mutations and do not expose local paths or file links.",
                "Do not add a technical summary after reports.public_report.lines.",
                "Do not mention internal terminal-state field names, schemas, hashes or local paths in the public response.",
            ],
        }
    ).to_payload()


def _process_chats_terminal_no_pending_contract() -> JsonObject:
    directive = _process_chats_no_pending_directive()
    public_report = WorkflowPublicReport(
        workflow="/mednotes:process-chats",
        run_id="process-chats-architect-no-pending",
        headline="Nenhum chat novo para processar.",
        lines=[
            "Nenhuma nota foi publicada ou preparada.",
            "Nenhum raw chat novo foi processado.",
            "Nada foi escrito na Wiki.",
            "Coverage/manifest não se aplicam porque não houve publicação.",
            "O linker/grafo não precisa rodar porque nenhuma nota foi publicada.",
        ],
    ).to_payload()
    return {
        "workflow": "/mednotes:process-chats",
        "process_chats_terminal_state": "no_pending",
        "reports": {
            "summary": "Nenhum chat novo para processar.",
            "public_report": public_report,
        },
        "agent_directive": directive,
    }


def _plan_architect_subagents(
    config: MedConfig,
    spec: JsonObject,
    rows: list[JsonObject],
    *,
    total_available_count: int,
    concurrency: int,
    temp_root: Path,
    limit: int | None,
) -> JsonObject:
    existing_targets = note_target_index(config.wiki_dir, as_relative=True)
    parsed_items: list[_ArchitectParsedItem] = []
    meaning_work_items: list[JsonObject] = []
    blocked_items: list[JsonObject] = []
    seen: set[str] = set()

    typed_rows = [_RawChatPlanningRow.model_validate(row) for row in rows]
    for index, row in enumerate(typed_rows, start=1):
        raw_file = row.path
        raw_key = str(Path(raw_file).expanduser())
        if raw_key in seen:
            continue
        seen.add(raw_key)
        work_id = f"architect-{index:03d}-{_slug(Path(raw_file).stem)}"
        item: JsonObject = {
            "work_id": work_id,
            "agent": spec["agent"],
            "item_type": spec["item_type"],
            "raw_file": raw_file,
            "owner_key": raw_key,
            "titulo_triagem": row.titulo_triagem,
            "fonte_id": row.fonte_id,
        }
        try:
            raw_plan = _ReadNoteMeta.model_validate(read_note_meta(Path(raw_file))).note_plan
            if not raw_plan:
                raise ValidationError("Raw chat missing triage note_plan; rerun triage with --note-plan")
            note_plan = _TriageNotePlan.from_payload(parse_triage_note_plan(raw_plan, Path(raw_file)))
        except ValidationError as exc:
            item.update(
                {
                    "blocked_reason": "missing_or_invalid_note_plan",
                    "note_plan_error": str(exc),
                    "next_action": "Refaça a triagem com --note-plan exaustivo antes de planejar arquitetura.",
                }
            )
            _non_launchable_blocked_item(item)
            blocked_items.append(item)
            continue

        item["note_plan"] = note_plan.public_payload()
        item.update(note_plan_summary(note_plan.public_payload()))
        if note_plan.schema_ == TRIAGE_NOTE_PLAN_V2_SCHEMA:
            planner = _MeaningPlannerResult.model_validate(
                plan_meaning_work_items(
                    config,
                    note_plan.public_payload(),
                    raw_file=Path(raw_file),
                    temp_root=temp_root,
                    agent=str(spec["agent"]),
                )
            )
            for blocked_item in planner.blocked_items:
                blocked_item.setdefault("agent", spec["agent"])
                blocked_item.setdefault("source_work_id", work_id)
                blocked_item.setdefault("owner_key", raw_key)
                blocked_item.setdefault(
                    "next_action",
                    planner.next_action or "Corrija o triage-note-plan.v2 antes do architect.",
                )
                blocked_item["note_plan"] = note_plan.public_payload()
                blocked_item.update(note_plan_summary(note_plan.public_payload()))
                _non_launchable_blocked_item(blocked_item)
                blocked_items.append(blocked_item)
            item["meaning_planner_work_items"] = list(planner.work_items)
            targets = _planned_meaning_targets(note_plan)
            duplicate_targets: list[_DuplicateTarget] = []
            for target in targets:
                matches = existing_targets[target.target_key] if target.target_key in existing_targets else []
                if matches:
                    duplicate_targets.append(
                        _DuplicateTarget(
                            id=target.id,
                            title=target.title,
                            target_key=target.target_key,
                            conflict_type="ambiguous_existing_wiki_note" if len(matches) > 1 else "existing_wiki_note",
                            existing_paths=[str(path) for path in matches[:5]],
                        )
                    )
            parsed_items.append(
                _ArchitectParsedItem(
                    item=item,
                    note_plan=note_plan,
                    targets=targets,
                    duplicate_targets=duplicate_targets,
                )
            )
            continue
        targets = _planned_meaning_targets(note_plan)
        duplicate_targets: list[_DuplicateTarget] = []
        for target in targets:
            matches = existing_targets[target.target_key] if target.target_key in existing_targets else []
            if matches:
                duplicate_targets.append(
                    _DuplicateTarget(
                        id=target.id,
                        title=target.title,
                        target_key=target.target_key,
                        conflict_type="ambiguous_existing_wiki_note" if len(matches) > 1 else "existing_wiki_note",
                        existing_paths=[str(path) for path in matches[:5]],
                    )
                )
        parsed_items.append(
            _ArchitectParsedItem(
                item=item,
                note_plan=note_plan,
                targets=targets,
                duplicate_targets=duplicate_targets,
            )
        )

    planned_by_key: dict[str, list[_PlannedMatch]] = {}
    parsed_by_work_id: dict[str, _ArchitectParsedItem] = {}
    for parsed in parsed_items:
        item = parsed.item
        parsed_by_work_id[str(item["work_id"])] = parsed
        for target in parsed.targets:
            planned_by_key.setdefault(target.target_key, []).append(
                _PlannedMatch(
                    raw_file=str(item["raw_file"]),
                    work_id=str(item["work_id"]),
                    id=target.id,
                    title=target.title,
                )
            )

    work_items: list[JsonObject] = list(meaning_work_items)
    consumed_work_ids: set[str] = set()
    canonical_merge_index = 1
    for target_key, planned_matches in planned_by_key.items():
        if len(planned_matches) <= 1 or target_key in existing_targets:
            continue
        group = [parsed_by_work_id[match.work_id] for match in planned_matches]
        if not all(_single_planned_meaning_target(parsed, target_key) for parsed in group):
            blocked_items.append(
                _canonical_merge_blocked_item(
                    target_key=target_key,
                    planned_matches=planned_matches,
                    reason="human_decision_required.ambiguous_canonical_target",
                    message=(
                        "At least one raw chat has additional planned_meaning targets; choose whether to "
                        "split triage or make one canonical merge work item before spawning architects."
                    ),
                )
            )
            consumed_work_ids.update(match.work_id for match in planned_matches)
            continue
        try:
            work_items.append(
                _canonical_merge_work_item(
                    config,
                    spec,
                    target_key=target_key,
                    planned_matches=planned_matches,
                    parsed_by_work_id=parsed_by_work_id,
                    temp_root=temp_root,
                    index=canonical_merge_index,
                )
            )
        except MedOpsError as exc:
            blocked_items.append(
                _canonical_merge_blocked_item(
                    target_key=target_key,
                    planned_matches=planned_matches,
                    reason="missing_or_invalid_artifact_manifest",
                    message=str(exc),
                )
            )
        consumed_work_ids.update(match.work_id for match in planned_matches)
        canonical_merge_index += 1

    existing_merge_index = 1
    for target_key, planned_matches in planned_by_key.items():
        existing_matches = existing_targets[target_key] if target_key in existing_targets else []
        if len(existing_matches) != 1:
            continue
        group = [parsed_by_work_id[match.work_id] for match in planned_matches]
        if not all(_single_planned_meaning_target(parsed, target_key) for parsed in group):
            blocked_items.append(
                _canonical_merge_blocked_item(
                    target_key=target_key,
                    planned_matches=planned_matches,
                    reason="human_decision_required.ambiguous_canonical_target",
                    message=(
                        "At least one raw chat has additional planned_meaning targets; choose whether to "
                        "split triage or merge only the intended delta into the existing canonical note."
                    ),
                )
            )
            consumed_work_ids.update(match.work_id for match in planned_matches)
            continue
        try:
            work_items.append(
                _existing_canonical_merge_work_item(
                    config,
                    spec,
                    target_key=target_key,
                    planned_matches=planned_matches,
                    parsed_by_work_id=parsed_by_work_id,
                    existing_paths=existing_matches,
                    temp_root=temp_root,
                    index=existing_merge_index,
                )
            )
        except MedOpsError as exc:
            blocked_items.append(
                _canonical_merge_blocked_item(
                    target_key=target_key,
                    planned_matches=planned_matches,
                    reason="missing_or_invalid_artifact_manifest",
                    message=str(exc),
                )
            )
        consumed_work_ids.update(match.work_id for match in planned_matches)
        existing_merge_index += 1

    for parsed in parsed_items:
        item = parsed.item
        if str(item["work_id"]) in consumed_work_ids:
            continue
        duplicate_targets: list[_DuplicateTarget] = [
            target if isinstance(target, _DuplicateTarget) else _DuplicateTarget.model_validate(target)
            for target in parsed.duplicate_targets
        ]
        for target in parsed.targets:
            planned_matches = planned_by_key[target.target_key] if target.target_key in planned_by_key else []
            if len(planned_matches) > 1:
                duplicate_targets.append(
                    _DuplicateTarget(
                        id=target.id,
                        title=target.title,
                        target_key=target.target_key,
                        conflict_type="planned_in_batch",
                        planned_matches=planned_matches,
                    )
                )
        if duplicate_targets:
            blocked_reason = _duplicate_blocked_reason(duplicate_targets)
            item.update(
                {
                    "blocked_reason": blocked_reason,
                    "duplicate_targets": [target.to_payload() for target in duplicate_targets],
                    "next_action": _duplicate_next_action(blocked_reason),
                }
            )
            if blocked_reason == "canonical_merge_required":
                packet = _packet_for_existing_canonical_target(duplicate_targets[0])
                item["canonical_merge"] = {
                    "schema": CANONICAL_MERGE_PLAN_SCHEMA,
                    "target_kind": "existing_wiki_note",
                    "target_title": duplicate_targets[0].title,
                    "target_key": duplicate_targets[0].target_key,
                    "existing_paths": duplicate_targets[0].existing_paths,
                }
                item["human_decision_packet"] = packet
            elif blocked_reason == "human_decision_required.ambiguous_canonical_target":
                packets = [
                    _packet_for_ambiguous_target(
                        target_key=target.target_key,
                        target_title=target.title or target.target_key,
                        options=target.existing_paths,
                        planned_matches=target.planned_matches,
                    )
                    for target in duplicate_targets
                    if target.conflict_type == "ambiguous_existing_wiki_note"
                ]
                if not packets:
                    packets = [
                        _packet_for_ambiguous_target(
                            target_key=target.target_key,
                            target_title=target.title or target.target_key,
                            options=[],
                            planned_matches=target.planned_matches,
                        )
                        for target in duplicate_targets
                        if target.conflict_type == "planned_in_batch"
                ]
                item["human_decision_packets"] = packets
            _non_launchable_blocked_item(item)
            blocked_items.append(item)
            continue

        meaning_items = _json_object_list_field(item, "meaning_planner_work_items")
        if meaning_items:
            for planned in meaning_items:
                work_item = dict(planned)
                work_item["source_work_id"] = item["work_id"]
                work_item["titulo_triagem"] = _json_str_field(item, "titulo_triagem")
                work_item["fonte_id"] = _json_str_field(item, "fonte_id")
                work_item["note_plan"] = item["note_plan"]
                work_item.update(note_plan_summary(parsed.note_plan.public_payload()))
                target_path = _json_str_field(work_item, "target_path")
                note_plan_item_id = _json_str_field(work_item, "note_plan_item_id")
                work_item.setdefault(
                    "owner_key",
                    target_path or f"meaning:{note_plan_item_id or item['owner_key']}",
                )
                try:
                    work_item.update(_artifact_payload_for_raw(config, Path(item["raw_file"])))
                except MedOpsError as exc:
                    work_item.update(
                        {
                            "blocked_reason": "missing_or_invalid_artifact_manifest",
                            "artifact_manifest_error": str(exc),
                            "next_action": (
                                "Corrija o manifesto HTML do Gemini ou remova a dependência antes de lançar architects."
                            ),
                        }
                    )
                    _non_launchable_blocked_item(work_item)
                    blocked_items.append(work_item)
                    continue
                write_policy = (
                    "existing_note_rewrite"
                    if _json_str_field(work_item, "target_kind") == "existing_wiki_note"
                    else "temp_note_allowed"
                )
                _launchable_work_item(work_item, write_policy=write_policy)
                work_items.append(work_item)
            continue

        try:
            artifact_payload = _artifact_payload_for_raw(config, Path(item["raw_file"]))
        except MedOpsError as exc:
            item.update(
                {
                    "blocked_reason": "missing_or_invalid_artifact_manifest",
                    "artifact_manifest_error": str(exc),
                    "next_action": "Corrija o manifesto HTML do Gemini ou remova a dependência antes de lançar architects.",
                }
            )
            _non_launchable_blocked_item(item)
            blocked_items.append(item)
            continue
        item.update(artifact_payload)
        item["temp_dir"] = str(temp_root / item["work_id"])
        _launchable_work_item(item)
        work_items.append(item)

    batches = _batch_refs(work_items, concurrency)
    status, next_action, _blocked_requires_attention = plan_status(
        item_count=len(work_items),
        blocked_item_count=len(blocked_items),
    )
    human_decision_required = bool(_decision_packets_from_blocked_items(blocked_items))
    payload = _with_decision_packets({
        "schema": SUBAGENT_PLAN_SCHEMA,
        "phase": "architect",
        "agent": spec["agent"],
        "unit": spec["unit"],
        "max_concurrency": concurrency,
        "item_count": len(work_items),
        "total_available_count": total_available_count,
        "blocked_item_count": len(blocked_items),
        "blocked_items": blocked_items,
        "canonical_merge_item_count": sum(1 for item in work_items if _is_canonical_merge_work_item(item)),
        "limit": limit,
        "truncated": limit is not None and len(rows) < total_available_count,
        "parallel_safe": len(work_items) > 1,
        "launch_source": "work_items",
        "batch_contract": "batches contain work_id references only; never spawn from both work_items and batch references.",
        "work_items": work_items,
        "batches": batches,
        "rules": [
            "Spawn at most one subagent per work_item.owner_key.",
            "Never spawn multiple subagents for the same raw chat or generated note.",
            "Use work_items as the only full launch payload; batches only group existing work_ids.",
            "Only work_items with launchable=true may be sent to med-knowledge-architect; blocked_items are stop packets.",
            "Canonical merges into an existing Wiki note are launchable architect work_items with write_policy=existing_note_rewrite and must be applied with apply-canonical-merge.",
            "Blocked architect items with write_policy=no_temp_note must not produce temp Markdown and must not be deferred to fix-wiki.",
            "Do not split one raw chat across multiple med-knowledge-architect agents.",
            "Architect work_items must follow the triage-authored note_plan exactly.",
            "raw-coverage.v1 includes only coverage-bearing v2 items: planned_meaning and not_a_note. Do not include attach_to_planned_meaning or needs_context as raw-coverage items; attach details must be folded into the target note, and needs_context blocks before architect.",
            "raw-coverage.v1 must carry raw_file, exhaustive=true, items[], and the same batch_id/run_id/source_artifact_hash metadata as the note_plan when present.",
            "Architect planning blocks planned_meaning targets that duplicate existing Wiki notes or another planned raw chat after accent/case normalization.",
            "When several simple raw chats target the same new note, plan one canonical_merge work_item owned by target_key.",
            "Canonical merge work_items must preserve new information from every source and report delta_per_source plus multi-reference provenance.",
            "Every architect result must include an exhaustive raw coverage inventory before staging.",
            "If artifact_manifests is non-empty, the staged note group for that raw chat must cover every listed artifact: HTML needs iframe/link/provenance, image needs Markdown embed/Figura caption/provenance.",
            "Do not launch more subagents than item_count or max_concurrency.",
            "If item_count is 0 or 1, there is no useful fan-out for this phase.",
            "When limit is set, spawn only the returned work_items",
            "Rerun planning after serial consolidation before launching more.",
            "Run serial consolidation after each batch returns.",
        ],
        "serial_after": spec["serial_after"],
        "canonical_parent_commands": spec["canonical_parent_commands"],
    }, blocked_items)
    terminal_no_pending = not work_items and not blocked_items
    if terminal_no_pending:
        payload.update(_process_chats_terminal_no_pending_contract())
        payload["parent_applies_outputs"] = False
        payload["serial_after"] = ["terminal no-op: write the final no-pending report and stop"]
        payload["canonical_parent_commands"] = [
            "terminal no-op: no publish/link/fix command is valid when there are no new chats to process"
        ]
    return _typed_subagent_plan_payload(annotate_payload(payload,
        phase="architect",
        status=status,
        blocked_reason="preconditions_failed" if blocked_items and not work_items else "",
        next_action="" if terminal_no_pending else next_action,
        required_inputs=[] if terminal_no_pending else PROCESS_CHATS_REQUIRED_INPUTS,
        human_decision_required=human_decision_required,
    ))


def plan_subagents(
    config: MedConfig,
    phase: str,
    max_concurrency: int | None = None,
    temp_root: Path | None = None,
    limit: int | None = None,
    fix_wiki_plan_path: Path | None = None,
    style_audit: JsonObject | None = None,
) -> JsonObject:
    specs: dict[str, JsonObject] = {
        "triage": {
            "agent": "med-chat-triager",
            "mode": "pending",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "triage"),
            "item_type": "raw_chat",
            "unit": "one pending raw chat per subagent",
            "serial_after": [
                "official subagent runner saves the top-level triager output to work_item.triager_output_path and writes a signed subagent-run-receipt.v1 for that exact output",
                "parent extracts note_plan to work_item.note_plan_path, writes eval to work_item.triager_eval_path with --subagent-run-receipt and --require-subagent-run-receipt, and only applies triage when triager-prompt-eval.v1 passes and the signed receipt/output/note_plan chain revalidates",
                "parent must not create, edit, re-sign, or patch subagent-run-receipt.v1; missing/invalid receipt means re-run the packaged triager through the official runner",
                "parent never patches the triager output or note_plan by hand; failed eval means re-run the triager with error_context or stop",
                "parent does not read raw chat content before plan-subagents returns work_items and does not write triage artifacts under repo-root tmp/",
                "parent refreshes list-triados before architect planning",
            ],
            "canonical_parent_commands": [
                'eval triager output (triager-prompt-eval.v1): uv run python "<wiki/cli.py>" eval-triager-output --raw-file "<raw_file>" --output "<triager-output.json>" --subagent-run-receipt "<subagent-run-receipt.json>" --require-subagent-run-receipt --report "<triager-eval.json>" --json',
                'triage: uv run python "<wiki/cli.py>" triage --raw-file "<raw_file>" --tipo medicina --titulo "<titulo_triagem>" --fonte-id "<fonte_id>" --note-plan "<note-plan.json>" --triager-eval "<triager-eval.json>" --json',
                'discard: uv run python "<wiki/cli.py>" discard --raw-file "<raw_file>" --reason "<reason>"',
            ],
        },
        "architect": {
            "agent": "med-knowledge-architect",
            "mode": "triados",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "architect"),
            "item_type": "triaged_raw_chat",
            "unit": "one triaged raw chat per subagent, or one canonical merge target; all notes split from a raw chat stay together",
            "serial_after": [
                "parent validates/fixes each returned temp note or existing-note rewrite",
                "parent stages new notes with wiki/cli.py stage-note and the architect coverage inventory",
                "parent applies existing-note canonical rewrites with wiki/cli.py apply-canonical-merge",
                "catalog, dry-run, guard, publish and linker stay serial for staged new notes",
            ],
            "canonical_parent_commands": [
                'validate-note: uv run python "<wiki/cli.py>" validate-note --content "<temp.md>" --title "<title>" --raw-file "<raw_file>" --json',
                'fix-note: uv run python "<wiki/cli.py>" fix-note --content "<temp.md>" --title "<title>" --raw-file "<raw_file>" --output "<temp.md>" --json',
                'apply canonical merge dry-run: uv run python "<wiki/cli.py>" apply-canonical-merge --target "<existing-note.md>" --content "<rewrite.md>" --coverage "<coverage.json>" --dry-run --json',
                'apply canonical merge: uv run python "<wiki/cli.py>" apply-canonical-merge --target "<existing-note.md>" --content "<rewrite.md>" --coverage "<coverage.json>" --json',
                'stage-note: uv run python "<wiki/cli.py>" stage-note --manifest "<manifest.json>" --raw-file "<raw_file>" --coverage "<coverage.json>" --taxonomy "<taxonomy>" --title "<title>" --content "<temp.md>"',
                'publish dry-run: uv run python "<wiki/cli.py>" publish-batch --manifest "<manifest.json>" --dry-run',
                'publish: uv run python "<wiki/cli.py>" publish-batch --manifest "<manifest.json>"',
                'diagnose links: uv run python "<wiki/cli.py>" run-linker --diagnose --json',
                'apply links if diagnosis is safe: uv run python "<wiki/cli.py>" run-linker --apply --diagnosis "<link-diagnosis.json>" --json',
            ],
        },
        "style-rewrite": {
            "agent": "med-knowledge-architect",
            "mode": "wiki_style_rewrite",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "style-rewrite"),
            "item_type": "wiki_note_style_rewrite",
            "unit": "one existing Wiki_Medicina note per subagent; each target path is unique",
            "serial_after": [
                "parent applies each returned temp rewrite atomically with wiki/cli.py apply-specialist-style-rewrite",
                "parent refreshes the next style-rewrite batch with plan-subagents until the style queue is empty",
                "parent runs full fix-wiki verification once after the style queue is empty",
            ],
            "canonical_parent_commands": [
                "Gemini CLI specialist rewrite: consume the call_specialist_model WorkflowEffect with one current_batch_items entry and the packaged med-knowledge-architect agent",
                'AGY specialist receipt finalization after packaged invoke_subagent: uv run python "<wiki/cli.py>" finalize-agy-specialist-task --plan "<style-rewrite-plan.json>" --work-id "<work_id>" --transcript "<agy-transcript-or-task-log>" [--runtime-log "<agy-cli.log>"] --json',
                'OpenCode specialist receipt finalization after native task: uv run python "<wiki/cli.py>" finalize-opencode-specialist-task --plan "<style-rewrite-plan.json>" --work-id "<work_id>" --json',
                'apply specialist rewrite: uv run python "<wiki/cli.py>" apply-specialist-style-rewrite --plan "<style-rewrite-plan.json>" --manifest "<style-rewrite-manifest.json>" --work-id "<work_id>" --specialist-run-receipt "<specialist-task-run-receipt.json>" --json',
            ],
        },
        "note-merge": {
            "agent": "med-knowledge-architect",
            "mode": "wiki_note_merge",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "note-merge"),
            "item_type": "wiki_note_merge",
            "unit": "one semantic note merge group per subagent; title/stem duplicates are not sufficient",
            "serial_after": [
                "parent validates each returned merge with wiki/cli.py apply-note-merge --dry-run",
                "parent applies accepted merges serially with wiki/cli.py apply-note-merge",
                "parent runs /mednotes:link once after accepted note merges",
            ],
            "canonical_parent_commands": [
                'apply merge dry-run: uv run python "<wiki/cli.py>" apply-note-merge --plan "<plan.json>" --content "<merged.md>" --dry-run --json',
                'apply merge: uv run python "<wiki/cli.py>" apply-note-merge --plan "<plan.json>" --content "<merged.md>" --json',
            ],
        },
        "vocabulary-curation": {
            "agent": "med-link-graph-curator",
            "mode": "vocabulary_curation",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "vocabulary-curation"),
            "item_type": "vocabulary_semantic_ingestion",
            "unit": "one pending vocabulary note per subagent",
            "serial_after": [
                "parent collects note-semantic-ingestion.v1 outputs",
                "parent writes vocabulary-curator-batch-output-manifest.v1",
                "parent runs wiki/cli.py eval-curator-batch",
                "parent applies outputs with wiki/cli.py apply-curator-batch --prompt-eval",
            ],
            "canonical_parent_commands": [
                'eval curator batch: uv run python "<wiki/cli.py>" eval-curator-batch --plan "<plan.json>" --outputs "<manifest.json>" --report "<curator-prompt-eval.json>" --json',
                'apply curator batch: uv run python "<wiki/cli.py>" apply-curator-batch --plan "<plan.json>" --outputs "<manifest.json>" --prompt-eval "<curator-prompt-eval.json>" --receipt "<receipt.json>" --json',
            ],
        },
        "atomicity-split": {
            "agent": "med-knowledge-architect",
            "mode": "wiki_atomicity_split",
            "default_max_concurrency": configured_subagent_max_concurrency(config, "atomicity-split"),
            "item_type": "wiki_atomicity_split",
            "unit": "one non-atomic source note per subagent",
            "serial_after": [
                "parent collects atomicity-split-bundle.v1 outputs",
                "parent applies accepted bundles serially with wiki/cli.py apply-atomicity-split",
                "parent runs the linker once per parent batch unless apply-atomicity-split was not deferred",
            ],
            "canonical_parent_commands": [
                'apply split: uv run python "<wiki/cli.py>" apply-atomicity-split --bundle "<bundle.json>" --json',
            ],
        },
    }
    if phase not in specs:
        raise ValidationError(f"Unknown subagent planning phase: {phase}")
    spec = specs[phase]
    concurrency = int(max_concurrency) if max_concurrency is not None else int(spec["default_max_concurrency"])
    if concurrency < 1:
        raise ValidationError("--max-concurrency must be at least 1")
    if limit is not None and limit < 1:
        raise ValidationError("--limit must be at least 1")
    if temp_root is None:
        temp_root = _default_subagent_temp_root(phase)

    if phase == "vocabulary-curation":
        if config.vocabulary_db_path is None:
            raise ValidationError("vocabulary-curation requires a vocabulary DB path")
        if temp_root is None:
            raise ValidationError("Internal error: vocabulary-curation temp_root was not resolved")
        batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-vocabulary-curation")
        plan = build_vocabulary_curator_batch_plan(
            db_path=config.vocabulary_db_path,
            batch_id=batch_id,
            output_dir=temp_root,
            limit=limit or 20,
        )
        plan_summary = _SubagentGeneratedPlanSummary.model_validate(plan)
        if max_concurrency is not None:
            plan["max_concurrency"] = min(int(max_concurrency), plan_summary.item_count) if plan_summary.item_count else 0
        else:
            plan["max_concurrency"] = min(concurrency, plan_summary.item_count) if plan_summary.item_count else 0
        plan["serial_after"] = spec["serial_after"]
        plan["canonical_parent_commands"] = spec["canonical_parent_commands"]
        return _typed_subagent_plan_payload(plan)

    if phase == "atomicity-split":
        if fix_wiki_plan_path is None:
            raise ValidationError("atomicity-split requires --fix-wiki-plan <fix-wiki-plan.json>")
        if temp_root is None:
            raise ValidationError("Internal error: atomicity-split temp_root was not resolved")
        batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-atomicity-split")
        plan = build_atomicity_split_plan(
            fix_wiki_plan_path=fix_wiki_plan_path,
            batch_id=batch_id,
            temp_root=temp_root,
            limit=limit or 20,
        )
        plan_summary = _SubagentGeneratedPlanSummary.model_validate(plan)
        if max_concurrency is not None:
            plan["max_concurrency"] = min(int(max_concurrency), plan_summary.item_count) if plan_summary.item_count else 0
        else:
            plan["max_concurrency"] = min(concurrency, plan_summary.item_count) if plan_summary.item_count else 0
        plan["serial_after"] = spec["serial_after"]
        plan["canonical_parent_commands"] = spec["canonical_parent_commands"]
        return _typed_subagent_plan_payload(plan)

    if phase == "note-merge":
        return _typed_subagent_plan_payload({
            "schema": SUBAGENT_PLAN_SCHEMA,
            "phase": "note-merge",
            "agent": spec["agent"],
            "status": "skipped",
            "skipped_reason": "no_note_merge_work",
            "mode": spec["mode"],
            "item_type": spec["item_type"],
            "unit": spec["unit"],
            "max_concurrency": concurrency,
            "item_count": 0,
            "blocked_item_count": 0,
            "total_available_count": 0,
            "work_items": [],
            "blocked_items": [],
            "batches": [],
            "parallel_safe": False,
            "serial_after": spec["serial_after"],
            "canonical_parent_commands": spec["canonical_parent_commands"],
            "rules": [
                "Only semantic identity evidence may create a note-merge work item.",
                "Do not infer a merge from title, stem, accent, case, or path similarity alone.",
                "Every apply requires note-merge-plan.v1, preservation report, source hashes, expected aliases, and expected chats.",
            ],
            "next_action": "Sem grupos semânticos de note_merge prontos neste diagnóstico.",
        })

    if phase == "style-rewrite":
        if temp_root is None:
            raise ValidationError("Internal error: style-rewrite temp_root was not resolved")
        audit = _StyleRewriteAuditSummary.model_validate(
            style_audit if style_audit is not None else validate_wiki_style(config.wiki_dir)
        )
        work_items: list[JsonObject] = []
        seen: set[str] = set()
        rewrite_reports = [report for report in audit.reports if report.requires_llm_rewrite and report.path]
        total_available_count = len(rewrite_reports)
        if limit is not None:
            rewrite_reports = rewrite_reports[:limit]
        for index, report in enumerate(rewrite_reports, start=1):
            target_path = Path(report.path)
            owner_key = str(target_path.expanduser())
            if owner_key in seen:
                continue
            seen.add(owner_key)
            work_id = f"{phase}-{index:03d}-{_slug(target_path.stem)}"
            item: JsonObject = {
                "work_id": work_id,
                "agent": spec["agent"],
                "item_type": spec["item_type"],
                "target_path": str(target_path),
                "target_hash_before": _file_sha256(target_path),
                "owner_key": owner_key,
                "title": report.title or target_path.stem,
                "rewrite_prompt": report.rewrite_prompt,
                "model_policy": "medical_specialist_authoring.v1",
                "required_model_tier": "specialist",
                "preferred_model_tier": "pro",
                "errors": report.errors,
                "warnings": report.warnings,
                "temp_dir": str(temp_root / work_id),
                "temp_output": str(temp_root / work_id / f"{target_path.stem}.rewrite.md"),
                "output_attestation_path": str(temp_root / work_id / f"{target_path.stem}.rewrite.md.attestation.json"),
                "specialist_task_run_receipt_path": str(
                    temp_root / work_id / f"{target_path.stem}.specialist-task-run-receipt.json"
                ),
                "subagent_output_contract": _style_rewrite_subagent_output_contract(),
                "context_docs": _style_rewrite_context_docs(),
            }
            temp_dir = Path(str(item["temp_dir"]))
            temp_dir.mkdir(parents=True, exist_ok=True)
            (temp_dir / ".keep").touch(exist_ok=True)
            work_items.append(item)
        batches = _batch_refs(work_items, concurrency)
        return _typed_subagent_plan_payload({
            "schema": SUBAGENT_PLAN_SCHEMA,
            "phase": phase,
            "agent": spec["agent"],
            "status": "ready" if work_items else "skipped",
            "skipped_reason": "" if work_items else "no_style_rewrite_work",
            "unit": spec["unit"],
            "max_concurrency": concurrency,
            "item_count": len(work_items),
            "total_available_count": total_available_count,
            "blocked_item_count": 0,
            "limit": limit,
            "truncated": len(work_items) < total_available_count,
            "parallel_safe": len(work_items) > 1,
            "launch_source": "work_items",
            "batch_contract": "batches contain work_id references only; never spawn from both work_items and batch references.",
            "work_items": work_items,
            "batches": batches,
            "rules": [
                "Spawn at most one subagent per work_item.target_path.",
                "Never spawn multiple subagents for the same Wiki note.",
                "Use work_items as the only full launch payload; batches only group existing work_ids.",
                "Do not split one note rewrite across multiple med-knowledge-architect agents.",
                "Do not launch more subagents than item_count or max_concurrency.",
                "If item_count is 0 or 1, there is no useful fan-out for this phase.",
                "When limit is set, spawn only the returned work_items",
                "Rerun planning after serial consolidation before launching more.",
                "Run serial apply-style-rewrite validation and application after each batch returns.",
            ],
            "serial_after": spec["serial_after"],
            "canonical_parent_commands": spec["canonical_parent_commands"],
            "source_audit": {
                "schema": audit.schema_,
                "wiki_dir": audit.wiki_dir or str(config.wiki_dir),
                "file_count": audit.file_count,
                "error_count": audit.error_count,
                "warning_count": audit.warning_count,
            },
        })

    covered_ids = set(covered_raw_chat_index(config.wiki_dir)) if spec["mode"] == "pending" else set()
    rows = list_by_status(config.raw_dir, str(spec["mode"]), covered_raw_chat_ids=covered_ids)
    total_available_count = len(rows)
    if limit is not None:
        rows = rows[:limit]
    if phase == "architect":
        if temp_root is None:
            raise ValidationError("Internal error: architect temp_root was not resolved")
        return _plan_architect_subagents(
            config,
            spec,
            rows,
            total_available_count=total_available_count,
            concurrency=concurrency,
            temp_root=temp_root,
            limit=limit,
        )
    work_items: list[JsonObject] = []
    blocked_items: list[JsonObject] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        raw_file = str(row["path"])
        raw_key = str(Path(raw_file).expanduser())
        if raw_key in seen:
            continue
        seen.add(raw_key)
        work_id = f"{phase}-{index:03d}-{_slug(Path(raw_file).stem)}"
        item: JsonObject = {
            "work_id": work_id,
            "agent": spec["agent"],
            "item_type": spec["item_type"],
            "raw_file": raw_file,
            "owner_key": raw_key,
            "titulo_triagem": _json_str_field(row, "titulo_triagem"),
            "fonte_id": _json_str_field(row, "fonte_id"),
        }
        if temp_root is not None:
            temp_dir = temp_root / work_id
            item["temp_dir"] = str(temp_dir)
            if phase == "triage":
                item["triager_output_path"] = str(temp_dir / "triager-output.json")
                item["note_plan_path"] = str(temp_dir / "note-plan.json")
                item["triager_eval_path"] = str(temp_dir / "triager-eval.json")
        work_items.append(item)

    batches = _batch_refs(work_items, concurrency)
    status, next_action, _blocked_requires_attention = plan_status(
        item_count=len(work_items),
        blocked_item_count=len(blocked_items),
    )
    human_decision_required = bool(_decision_packets_from_blocked_items(blocked_items))
    return _typed_subagent_plan_payload(annotate_payload({
        "schema": SUBAGENT_PLAN_SCHEMA,
        "phase": phase,
        "agent": spec["agent"],
        "unit": spec["unit"],
        "max_concurrency": concurrency,
        "item_count": len(work_items),
        "total_available_count": total_available_count,
        "blocked_item_count": len(blocked_items),
        "blocked_items": blocked_items,
        "limit": limit,
        "truncated": limit is not None and len(rows) < total_available_count,
        "parallel_safe": len(work_items) > 1,
        "launch_source": "work_items",
        "batch_contract": "batches contain work_id references only; never spawn from both work_items and batch references.",
        "work_items": work_items,
        "batches": batches,
        "rules": [
            "Spawn at most one subagent per work_item.raw_file.",
            "Never spawn multiple subagents for the same raw chat or generated note.",
            "Use work_items as the only full launch payload; batches only group existing work_ids.",
            "Do not replace med-chat-triager by reading multiple raw chats in the parent agent.",
            "Parent must use work_item.triager_output_path, work_item.note_plan_path, and work_item.triager_eval_path; do not write workflow artifacts under repo-root tmp/.",
            "Do not launch more subagents than item_count or max_concurrency.",
            "If item_count is 0 or 1, there is no useful fan-out for this phase.",
            "When limit is set, spawn only the returned work_items",
            "Rerun planning after serial consolidation before launching more.",
            "Parent must apply triage or discard serially after each batch returns.",
        ],
        "serial_after": spec["serial_after"],
        "canonical_parent_commands": spec["canonical_parent_commands"],
    },
        phase=phase,
        status=status,
        blocked_reason="preconditions_failed" if blocked_items and not work_items else "",
        next_action=next_action,
        required_inputs=STYLE_REWRITE_REQUIRED_INPUTS if phase == "style-rewrite" else PROCESS_CHATS_REQUIRED_INPUTS,
        human_decision_required=human_decision_required,
    ))
