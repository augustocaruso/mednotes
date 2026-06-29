"""Official specialist task runners for Workbench-mediated medical authoring."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.capabilities.graph.coverage import validate_raw_coverage_structure
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.specialist.plan_attestation import validate_subagent_plan_attestation
from mednotes.domains.wiki.capabilities.specialist.specialist_receipts import (
    attach_specialist_task_run_receipt_attestation,
)
from mednotes.domains.wiki.capabilities.style.style import (
    _normalize_style_rewrite_output_file,
    _read_json_object,
    _sha256_bytes,
    _style_rewrite_model_policy,
    _style_rewrite_output_attestation_path,
    _style_rewrite_output_receipt_path,
    _style_rewrite_work_item,
    _validate_style_rewrite_plan,
    _verify_style_rewrite_plan_attestation,
    apply_style_rewrite,
    fix_note_style_file,
    validate_note_style_file,
)
from mednotes.domains.wiki.common import DOCS_RELPATH, MissingPathError, ValidationError
from mednotes.domains.wiki.contracts.agent_report import FixWikiPrimaryObjectiveSummary
from mednotes.domains.wiki.contracts.agents import SubagentBatchPlan, SubagentWorkItem
from mednotes.domains.wiki.contracts.specialist import (
    SpecialistHarness,
    SpecialistNextApplyStep,
    SpecialistRunStatus,
    SpecialistTaskRunReceipt,
)
from mednotes.domains.wiki.contracts.workflow_guardrails import error_context
from mednotes.domains.wiki.flows.fix_wiki.fix_wiki_primary_objective import fix_wiki_primary_objective_summary
from mednotes.kernel.agent_directive import AgentDirective
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter, contract_error

SPECIALIST_TASK_RUNNER_RESULT_SCHEMA = "medical-notes-workbench.specialist-task-runner-result.v1"
SPECIALIST_TASK_RUNNER_INPUT_SCHEMA = "medical-notes-workbench.specialist-task-runner-input.v1"
SPECIALIST_TASK_RUNNER_TRANSCRIPT_SCHEMA = "medical-notes-workbench.specialist-task-runner-transcript.v1"
MEDNOTES_AGENT_DIRECTIVE_SCHEMA = "medical-notes-workbench.agent-directive.v1"
AGY_SPECIALIST_TRANSCRIPT_ARTIFACT_SCHEMA = "medical-notes-workbench.agy-specialist-transcript-artifact.v1"
OPENCODE_SPECIALIST_TASK_METADATA_SCHEMA = "medical-notes-workbench.opencode-specialist-task-metadata.v1"
OPENCODE_SPECIALIST_TASK_ARTIFACT_SCHEMA = "medical-notes-workbench.opencode-specialist-task-artifact.v1"
ARCHITECT_TASK_RUNNER_RESULT_SCHEMA = "medical-notes-workbench.architect-task-runner-result.v1"
ARCHITECT_TASK_RUN_RECEIPT_SCHEMA = "medical-notes-workbench.architect-task-run-receipt.v1"
ARCHITECT_TASK_OUTPUT_SCHEMA = "medical-notes-workbench.architect-output.v1"
ARCHITECT_NEXT_SERIAL_STEP_SCHEMA = "medical-notes-workbench.architect-next-serial-step.v1"
GEMINI_CLI_SPECIALIST_ADAPTER = "gemini_cli_headless_runner"
AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER = "agy_packaged_template_subagent"
OPENCODE_TASK_SUBAGENT_ADAPTER = "opencode_task_subagent"
AGY_SELECTED_MODEL_OVERRIDE_RE = re.compile(
    r'Propagating selected model override to backend:\s+label="(?P<label>[^"]+)"'
)
_MAX_CAPTURE_CHARS_PER_STREAM = 512_000
_NO_MCP_SERVER_SENTINEL = "__mednotes_no_mcp__"


class SpecialistTaskRunnerResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.specialist-task-runner-result.v1"] = Field(
        default=SPECIALIST_TASK_RUNNER_RESULT_SCHEMA,
        alias="schema",
    )
    phase: Literal["style_rewrite"] = "style_rewrite"
    status: SpecialistRunStatus
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    work_id: str = Field(min_length=1)
    harness: SpecialistHarness
    adapter: str = Field(min_length=1)
    requested_agent: str = "med-knowledge-architect"
    requested_model: str = Field(min_length=1)
    observed_model: str = ""
    plan_path: str
    target_path: str = ""
    output_path: str = ""
    output_sha256: str = ""
    receipt_path: str = ""
    input_packet_path: str = ""
    transcript_artifact_path: str = ""
    validation: JsonObject = Field(default_factory=dict)
    next_apply_step: SpecialistNextApplyStep | None = None
    error_context: JsonObject = Field(default_factory=dict)
    parent_workflow_summary: JsonObject = Field(default_factory=dict)
    public_report: JsonObject = Field(default_factory=dict)
    agent_directive: JsonObject = Field(default_factory=dict)


class OpenCodeSpecialistTaskMetadata(ContractModel):
    schema_id: Literal["medical-notes-workbench.opencode-specialist-task-metadata.v1"] = Field(
        default=OPENCODE_SPECIALIST_TASK_METADATA_SCHEMA,
        alias="schema",
    )
    work_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    parent_session_id: str = Field(min_length=1)
    specialist_session_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_tier: str = Field(min_length=1)
    tool_sequence: list[str] = Field(default_factory=list)
    prompt_contract: Literal["single_current_batch_items_json"] = "single_current_batch_items_json"
    raw_content_embedded: bool = False
    capture_source: Literal["opencode_tool_execute_after"]
    capture_session_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_prompt_sha256: str = Field(min_length=1)
    tool_response_sha256: str = Field(min_length=1)
    captured_at: str = Field(min_length=1)


class ArchitectTaskOutput(ContractModel):
    """Structured output returned by med-knowledge-architect for process-chats."""

    schema_id: Literal["medical-notes-workbench.architect-output.v1"] = Field(
        default=ARCHITECT_TASK_OUTPUT_SCHEMA,
        alias="schema",
    )
    status: Literal["completed"]
    original_path: str = Field(min_length=1)
    temp_output_path: str = Field(min_length=1)
    coverage_path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    staged_title: str = Field(min_length=1)
    taxonomy: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    entity_proposals: list[JsonObject] = Field(default_factory=list)


class ArchitectNextSerialStep(ContractModel):
    """Next parent-owned command after a validated architect output."""

    schema_id: Literal["medical-notes-workbench.architect-next-serial-step.v1"] = Field(
        default=ARCHITECT_NEXT_SERIAL_STEP_SCHEMA,
        alias="schema",
    )
    command_family: Literal["stage-note"] = "stage-note"
    arguments: list[str] = Field(min_length=1)
    must_run_before: list[str] = Field(default_factory=list)
    agent_instruction: str = Field(min_length=1)


class ArchitectTaskRunReceipt(ContractModel):
    """Receipt proving one OpenCode architect task before process-chats stages it."""

    schema_id: Literal["medical-notes-workbench.architect-task-run-receipt.v1"] = Field(
        default=ARCHITECT_TASK_RUN_RECEIPT_SCHEMA,
        alias="schema",
    )
    phase: Literal["architect"] = "architect"
    status: Literal["completed"] = "completed"
    work_id: str = Field(min_length=1)
    harness: SpecialistHarness
    adapter: str = Field(min_length=1)
    requested_agent: str = "med-knowledge-architect"
    requested_model: str = Field(min_length=1)
    observed_model: str = Field(min_length=1)
    plan_path: str = Field(min_length=1)
    raw_file: str = Field(min_length=1)
    title: str = Field(min_length=1)
    taxonomy: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    coverage_path: str = Field(min_length=1)
    coverage_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    architect_output_path: str = Field(min_length=1)
    architect_output_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_metadata_path: str = Field(min_length=1)
    task_metadata_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_evidence: JsonObject


class ArchitectTaskRunnerResult(ContractModel):
    """Local finalizer result; process-chats FSM remains the workflow truth."""

    schema_id: Literal["medical-notes-workbench.architect-task-runner-result.v1"] = Field(
        default=ARCHITECT_TASK_RUNNER_RESULT_SCHEMA,
        alias="schema",
    )
    phase: Literal["architect"] = "architect"
    status: SpecialistRunStatus
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    human_decision_required: bool = False
    work_id: str = Field(min_length=1)
    harness: SpecialistHarness
    adapter: str = Field(min_length=1)
    requested_agent: str = "med-knowledge-architect"
    requested_model: str = Field(min_length=1)
    observed_model: str = ""
    plan_path: str = Field(min_length=1)
    raw_file: str = ""
    title: str = ""
    taxonomy: str = ""
    output_path: str = ""
    output_sha256: str = ""
    coverage_path: str = ""
    receipt_path: str = ""
    architect_output_path: str = ""
    task_metadata_path: str = ""
    validation: JsonObject = Field(default_factory=dict)
    next_serial_step: ArchitectNextSerialStep | None = None
    error_context: JsonObject = Field(default_factory=dict)


def _result(
    *,
    status: SpecialistRunStatus,
    work_id: str,
    harness: SpecialistHarness,
    requested_model: str,
    plan_path: Path,
    blocked_reason: str = "",
    next_action: str = "",
    required_inputs: list[str] | None = None,
    target_path: Path | None = None,
    output_path: Path | None = None,
    output_sha256: str = "",
    receipt_path: Path | None = None,
    input_packet_path: Path | None = None,
    transcript_artifact_path: Path | None = None,
    observed_model: str = "",
    validation: JsonObject | None = None,
    adapter: str = GEMINI_CLI_SPECIALIST_ADAPTER,
) -> JsonObject:
    parent_workflow_summary = _parent_workflow_summary_for_plan(plan_path)
    next_apply_step = _specialist_task_next_apply_step(
        status=status,
        plan_path=plan_path,
        work_id=work_id,
        receipt_path=receipt_path,
    )
    payload = SpecialistTaskRunnerResult(
        status=status,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=required_inputs or [],
        work_id=work_id,
        harness=harness,
        adapter=adapter,
        requested_model=requested_model,
        observed_model=observed_model,
        plan_path=str(plan_path),
        target_path=str(target_path or ""),
        output_path=str(output_path or ""),
        output_sha256=output_sha256,
        receipt_path=str(receipt_path or ""),
        input_packet_path=str(input_packet_path or ""),
        transcript_artifact_path=str(transcript_artifact_path or ""),
        validation=validation or {},
        next_apply_step=next_apply_step,
        error_context=error_context(
            phase="style_rewrite",
            blocked_reason=blocked_reason,
            root_cause=blocked_reason,
            affected_artifact=str(output_path or receipt_path or plan_path),
            error_summary="Specialist task runner could not complete the Workbench receipt boundary."
            if status != SpecialistRunStatus.COMPLETED
            else "",
            suggested_fix=next_action,
            next_action=next_action,
            retry_scope="single_style_rewrite_work_item",
        )
        if status != SpecialistRunStatus.COMPLETED
        else {},
        parent_workflow_summary=parent_workflow_summary,
        public_report=_specialist_task_public_report(
            status=status,
            blocked_reason=blocked_reason,
            target_title=(target_path.stem if target_path else work_id),
            parent_workflow_summary=parent_workflow_summary,
        ),
        agent_directive=_specialist_task_agent_directive(
            status=status,
            parent_workflow_summary=parent_workflow_summary,
            next_apply_step=next_apply_step,
            blocked_reason=blocked_reason,
            next_action=next_action,
        ),
    )
    return payload.to_payload()


def _parent_workflow_summary_for_plan(plan_path: Path) -> JsonObject:
    for candidate in (
        plan_path.parent / "compact-report.json",
        plan_path.parent / "full-report.json",
        plan_path.parent / "run_state.json",
    ):
        if not candidate.exists():
            continue
        try:
            payload = _read_json_object(candidate, label=f"parent workflow report {candidate.name}")
        except (MissingPathError, ValidationError):
            continue
        direct = _fix_wiki_summary_from_payload(payload)
        if direct:
            return direct
        nested_report = payload["report"] if "report" in payload else None
        if isinstance(nested_report, dict):
            nested = _fix_wiki_summary_from_payload(JsonObjectAdapter.validate_python(nested_report))
            if nested:
                return nested
    return {}


def _fix_wiki_summary_from_payload(payload: JsonObject) -> JsonObject:
    reports_payload = payload["reports"] if "reports" in payload else {}
    reports = JsonObjectAdapter.validate_python(reports_payload)
    details_payload = reports["details"] if "details" in reports else {}
    details = JsonObjectAdapter.validate_python(details_payload)
    summary_payload = details["primary_objective_summary"] if "primary_objective_summary" in details else None
    if isinstance(summary_payload, dict):
        try:
            return FixWikiPrimaryObjectiveSummary.model_validate(summary_payload).to_payload()
        except PydanticValidationError:
            pass
    try:
        objective = fix_wiki_primary_objective_summary(payload)
    except (TypeError, ValueError, PydanticValidationError):
        return {}
    return objective.to_payload() if objective is not None else {}


def _typed_subagent_work_item(payload: JsonObject) -> SubagentWorkItem:
    """Validate runner work items before they select outputs or receipt paths."""

    try:
        return SubagentWorkItem.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task runner work item invalid") from exc


def _specialist_task_next_apply_step(
    *,
    status: SpecialistRunStatus,
    plan_path: Path,
    work_id: str,
    receipt_path: Path | None,
) -> SpecialistNextApplyStep | None:
    if status != SpecialistRunStatus.COMPLETED or receipt_path is None:
        return None
    manifest_path = plan_path.with_name("style-rewrite-manifest.json")
    return SpecialistNextApplyStep.model_validate(
        {
            "schema": "medical-notes-workbench.specialist-next-apply-step.v1",
            "command_family": "apply-specialist-style-rewrite",
            "arguments": [
                "--plan",
                str(plan_path),
                "--manifest",
                str(manifest_path),
                "--work-id",
                work_id,
                "--specialist-run-receipt",
                str(receipt_path),
                "--json",
            ],
            "must_run_before": [
                "fix-wiki --apply",
                "plan-subagents --phase style-rewrite",
                "another specialist invocation",
                "read_file manifest/plan probing",
            ],
            "agent_instruction": (
                "Especialista validado. Aplique este recibo agora com apply-specialist-style-rewrite; "
                "nao reavalie a Wiki, nao rode plan-subagents e nao leia manifest/plan para descobrir a rota antes do apply."
            ),
        }
    )


def _specialist_task_public_report(
    *,
    status: SpecialistRunStatus,
    blocked_reason: str,
    target_title: str,
    parent_workflow_summary: JsonObject | None = None,
) -> JsonObject:
    if status == SpecialistRunStatus.COMPLETED:
        return {}
    parent_lines = _parent_workflow_public_lines(parent_workflow_summary or {})
    if blocked_reason == "specialist_model_quota_exhausted":
        headline = "A Wiki ainda precisa da reescrita médica especializada."
        lines = [
            "A Wiki não foi fixada por completo nesta execução.",
            *parent_lines,
            f"Nenhuma nota foi reescrita neste lote; o primeiro item era {target_title}.",
            "A etapa automática segura já chegou até a chamada do modelo médico, mas a capacidade desse modelo acabou agora.",
            "Retome pelo fluxo oficial quando a capacidade voltar; não substitua por outro modelo nem tente contornar manualmente.",
        ]
    else:
        headline = "A reescrita médica especializada parou antes de aplicar a nota."
        lines = [
            "A Wiki não foi fixada por completo nesta execução.",
            *parent_lines,
            f"Nenhuma nota foi reescrita neste lote; o primeiro item era {target_title}.",
            "A etapa de reescrita médica especializada parou antes de gerar uma nota validada.",
            "Retome pelo fluxo oficial depois de resolver o bloqueio indicado no relatório técnico.",
        ]
    return {
        "schema": "medical-notes-workbench.specialist-task-public-report.v1",
        "audience": "user",
        "status": str(status),
        "headline": headline,
        "lines": lines,
    }


def _parent_workflow_public_lines(parent_workflow_summary: JsonObject) -> list[str]:
    if not parent_workflow_summary:
        return []
    try:
        summary = FixWikiPrimaryObjectiveSummary.model_validate(parent_workflow_summary)
    except PydanticValidationError:
        return []
    return [
        summary.wiki_summary,
        summary.mutation_summary,
        summary.graph_summary,
        summary.related_notes_summary,
    ]


def _specialist_task_agent_directive(
    *,
    status: SpecialistRunStatus,
    parent_workflow_summary: JsonObject | None = None,
    next_apply_step: SpecialistNextApplyStep | None = None,
    blocked_reason: str = "",
    next_action: str = "",
) -> JsonObject:
    carry_forward_lines = []
    if parent_workflow_summary:
        carry_forward_lines = [
            "Este resultado do especialista nao substitui o resultado principal do fix-wiki.",
            "Preserve parent_workflow_summary.wiki_summary, mutation_summary, graph_summary e related_notes_summary na resposta final.",
            "Nao substitua as contagens de mutacao do parent_workflow_summary pela contagem deste item especialista.",
        ]
    if status == SpecialistRunStatus.COMPLETED:
        resume = ""
        if next_apply_step is not None:
            resume = " ".join([next_apply_step.command_family, *next_apply_step.arguments]).strip()
        directive = AgentDirective.model_validate(
            {
                "workflow": "/mednotes:fix-wiki",
                "run_id": "specialist-task",
                "control": {
                    "status": "running",
                    "state": "specialist_task_completed",
                    "phase": "style_rewrite",
                    "reason": "specialist_output_ready",
                    "capabilities": {"continue": True, "final_report": False},
                    "effects": [],
                    "blockers": [],
                    "resume": resume,
                    "report": {"requires": ["parent_workflow_summary", "specialist_checkpoint"]},
                    "limits": {"raw_content": False, "absolute_paths": False, "ad_hoc_scripts": False},
                },
                "summary": "O especialista gerou a saida; aplique o proximo passo oficial antes do relatorio final.",
                "instructions": [
                *carry_forward_lines,
                    "status=completed significa que o especialista gerou temp_output e receipt_path oficiais.",
                    "O proximo passo imediato e next_apply_step.command_family=apply-specialist-style-rewrite.",
                    "Nao rode fix-wiki --apply, plan-subagents, read_file de manifest/plan ou outra chamada especialista antes de aplicar next_apply_step.arguments.",
                    "Depois do apply, reporte um checkpoint humano curto de qualidade/YAML/proveniencia/links antes de planejar a proxima leva.",
                ],
            }
        ).to_payload()
        directive["schema"] = MEDNOTES_AGENT_DIRECTIVE_SCHEMA
        return JsonObjectAdapter.validate_python(directive)
    directive = AgentDirective.model_validate(
        {
            "workflow": "/mednotes:fix-wiki",
            "run_id": "specialist-task",
            "control": {
                "status": "blocked",
                "state": "specialist_task_blocked",
                "phase": "style_rewrite",
                "reason": blocked_reason or "specialist_task_blocked",
                "capabilities": {"continue": False, "final_report": False},
                "effects": [],
                "blockers": [blocked_reason or "specialist_task_blocked"],
                "resume": next_action,
                "report": {"requires": ["public_report", "error_context"]},
                "limits": {"raw_content": False, "absolute_paths": False, "ad_hoc_scripts": False},
            },
            "summary": "A tarefa especialista esta bloqueada; reporte o bloqueio sem declarar sucesso.",
            "instructions": [
            *carry_forward_lines,
                "This is a specialist-task payload, not the parent FSM result; use its root public_report.lines only for this local blocker.",
                "Do not use sucesso, com sucesso, concluido, concluiu, finalizado or pronto while this specialist task is blocked.",
                "Do not render blocked_reason, root_cause, returncode, exit code or internal blocker codes in the public response.",
                "Do not render target_path, output_path, receipt_path, transcript_artifact_path, file links or local absolute paths in the public response.",
                "If CPU samples show high CPU, report it as observed impact; do not call 100% CPU expected or harmless.",
                "No specialist note was rewritten unless status=completed and receipt_path exists.",
        ],
        }
    ).to_payload()
    directive["schema"] = MEDNOTES_AGENT_DIRECTIVE_SCHEMA
    return JsonObjectAdapter.validate_python(directive)


def _bounded_text(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_CAPTURE_CHARS_PER_STREAM:
        return value, False
    return value[:_MAX_CAPTURE_CHARS_PER_STREAM], True


def _timeout_stream_text(value: object, *, fallback: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return fallback


def _write_json(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_input_packet(*, path: Path, work_item: JsonObject, plan_path: Path, model: str) -> str:
    packet = JsonObjectAdapter.validate_python(
        {
            "schema": SPECIALIST_TASK_RUNNER_INPUT_SCHEMA,
            "phase": "style_rewrite",
            "work_id": str(work_item.get("work_id") or ""),
            "requested_agent": str(work_item.get("agent") or "med-knowledge-architect"),
            "requested_model": model,
            "model_policy": _style_rewrite_model_policy(work_item),
            "target_path": str(work_item.get("target_path") or ""),
            "target_hash_before": str(work_item.get("target_hash_before") or ""),
            "temp_output": str(work_item.get("temp_output") or work_item.get("output_path") or ""),
            "plan_path": str(plan_path),
            "agent_input_rule": "O especialista deve ler a nota alvo pelo target_path e gravar somente temp_output.",
            "raw_content_in_prompt": False,
        }
    )
    _write_json(path, packet)
    return _sha256_bytes(path.read_bytes())


def _extension_root() -> Path:
    from mednotes.platform.paths import extension_root

    return extension_root()


def _agent_template_path() -> Path:
    return _extension_root() / "agents" / "med-knowledge-architect.md"


def _required_read_files() -> tuple[Path, ...]:
    docs_dir = _extension_root() / "docs"
    return (
        docs_dir / "agent-prompt-hardening.md",
        docs_dir / "knowledge-architect.md",
        docs_dir / "semantic-linker.md",
    )


def _prompt_for_gemini_cli(
    *,
    work_item: JsonObject,
    input_packet_path: Path,
    attempt: int = 1,
    previous_failure: str = "",
) -> str:
    extension_root = _extension_root()
    pointer_json = json.dumps(
        {
            "work_id": str(work_item.get("work_id") or ""),
            "input_packet_path": str(input_packet_path),
            "target_path": str(work_item.get("target_path") or ""),
            "target_hash_before": str(work_item.get("target_hash_before") or ""),
            "temp_output": str(work_item.get("temp_output") or ""),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    required_read_files = "\n".join(f"- {path}" for path in _required_read_files())
    retry_block = ""
    if attempt > 1:
        if previous_failure.startswith("validation:"):
            retry_block = (
                "\nPrevious attempt failed validation.\n"
                f"Validation feedback: {previous_failure.removeprefix('validation:').strip()}.\n"
                "Regenerate the full note and fix every listed issue. Do not explain the failure as completion. "
                "Write the file first, then respond briefly.\n\n"
            )
        else:
            retry_block = (
                "\nPrevious attempt failed before producing the required temp_output file.\n"
                f"Failure reason: {previous_failure or 'temp_output was not created'}.\n"
                "This retry is still the official Workbench route. Do not explain the failure as completion. "
                "Write the file first, then respond briefly.\n\n"
            )
    return (
        "You are running the packaged med-knowledge-architect specialist task for Medical Notes Workbench.\n"
        "Do not paste raw clinical note content into the response. Read the target_path yourself and write the full "
        "rewritten Markdown note to temp_output. Do not create Workbench receipts or attestations; the runner creates "
        "them after validation. A textual answer without the temp_output file is a failed task.\n\n"
        f"{retry_block}"
        "Required action order:\n"
        "1. Read the input packet and the work_item target_path.\n"
        "2. Write the complete rewritten Markdown note to the exact work_item temp_output path.\n"
        "3. Verify that temp_output exists and contains the full note.\n"
        "4. Only then respond briefly; do not summarize instead of writing the file.\n\n"
        f"Packaged extension root: {extension_root}\n"
        f"Required packaged docs directory: {extension_root / 'docs'}\n"
        f"Packaged specialist template path: {_agent_template_path()}\n"
        f"Use only packaged paths shown in this prompt and in the input packet; do not resolve {DOCS_RELPATH} against "
        "the source checkout.\n\n"
        "Required read files before writing:\n"
        f"{required_read_files}\n\n"
        f"Input packet path: {input_packet_path}\n\n"
        "Official work_item pointer JSON. The full typed contract is in the input packet; read it there.\n"
        f"{pointer_json}\n"
    )


def _find_string_field(value: object, field_names: set[str]) -> str:
    if isinstance(value, dict):
        for key, raw in value.items():
            if key.lower() in field_names and isinstance(raw, str) and raw.strip():
                return raw.strip()
        for raw in value.values():
            found = _find_string_field(raw, field_names)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_string_field(item, field_names)
            if found:
                return found
    return ""


def _extract_gemini_metadata(stdout: str) -> tuple[str, str]:
    observed_model = ""
    session_id = ""
    for line in stdout.splitlines():
        compact = line.strip()
        if not compact:
            continue
        try:
            payload = json.loads(compact)
        except json.JSONDecodeError:
            continue
        if not observed_model:
            observed_model = _find_string_field(payload, {"model", "model_id", "modelid"})
        if not session_id:
            session_id = _find_string_field(payload, {"session_id", "sessionid", "conversation_id"})
        if observed_model and session_id:
            break
    return observed_model, session_id


def _gemini_cli_quota_exhausted(*, stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return (
        "terminalquotaerror" in combined
        or "quota_exhausted" in combined
        or "resource_exhausted" in combined
        or "exhausted your capacity" in combined
        or "exceeded your current quota" in combined
    )


def _gemini_cli_model_unavailable(*, stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return (
        "modelnotfounderror" in combined
        or "requested entity was not found" in combined
        or "model not found" in combined
    )


def _gemini_cli_policy_config_invalid(*, stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return (
        "invalid policy rule" in combined
        or "mcpname is required" in combined
        or "rule source: settings (mcp allowed)" in combined
    )


def _redact_prompt_argument(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for arg in command:
        if skip_next:
            skip_next = False
            continue
        if arg == "--prompt":
            redacted.append("--prompt=<redacted>")
            skip_next = True
            continue
        redacted.append(arg)
    return redacted


def _specialist_inter_call_delay_seconds() -> float:
    raw = os.environ.get("MEDNOTES_SPECIALIST_INTER_CALL_DELAY_SECONDS", "10").strip()
    try:
        delay = float(raw)
    except ValueError:
        return 10.0
    return max(0.0, delay)


def _specialist_max_attempts() -> int:
    raw = os.environ.get("MEDNOTES_SPECIALIST_MAX_ATTEMPTS", "4").strip()
    try:
        attempts = int(raw)
    except ValueError:
        return 4
    return min(6, max(1, attempts))


def _style_validation_retry_reason(validation_block: JsonObject) -> str:
    if validation_block.get("errors"):
        return "style_rewrite_agent_contract_violation"
    if validation_block.get("requires_llm_rewrite"):
        return "style_rewrite_still_requires_rewrite"
    return ""


def _style_validation_retry_feedback(validation_block: JsonObject) -> str:
    issues: list[str] = []
    for field in ("errors", "warnings"):
        raw_items = validation_block.get(field)
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            code = str(raw_item.get("code") or "").strip()
            message = str(raw_item.get("message") or "").strip()
            section = str(raw_item.get("section") or "").strip()
            if not code and not message:
                continue
            parts = [part for part in (code, message, f"section={section}" if section else "") if part]
            issue = ": ".join(parts[:2]) + (f" ({parts[2]})" if len(parts) > 2 else "")
            if code == "excessive_callouts":
                issue += "; action: keep at most two callouts in the whole note and convert the rest to normal prose or bullets"
            elif code == "didactic_visual_opportunity":
                suggested_visual = str(raw_item.get("suggested_visual") or "").strip().lower()
                if suggested_visual == "mermaid":
                    location = f" in {section}" if section else " in the indicated clinical section"
                    issue += (
                        "; action: add a ```mermaid fenced diagram"
                        f"{location}, immediately after the paragraph/table it clarifies"
                    )
                else:
                    issue += "; action: add the requested visual in the indicated clinical section"
            issues.append(issue)
    if issues:
        return "validation: " + "; ".join(issues[:6])
    return "validation: generated rewrite still failed Workbench style validation"


def _rate_limit_state_path(plan_path: Path) -> Path:
    return plan_path.parent / "specialist-task-runner-rate-limit.json"


def _enforce_specialist_inter_call_delay(*, plan_path: Path, work_id: str) -> JsonObject:
    delay_seconds = _specialist_inter_call_delay_seconds()
    state_path = _rate_limit_state_path(plan_path)
    waited_seconds = 0.0
    now = time.time()
    previous_started_at = 0.0
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                previous_started_at = float(state.get("last_started_at_epoch") or 0.0)
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            previous_started_at = 0.0
    if delay_seconds > 0 and previous_started_at > 0:
        elapsed = max(0.0, now - previous_started_at)
        waited_seconds = max(0.0, delay_seconds - elapsed)
        if waited_seconds > 0:
            print(
                "[mednotes] Aguardando intervalo controlado antes do proximo especialista "
                f"({waited_seconds:.1f}s).",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(waited_seconds)
    started_at = time.time()
    _write_json(
        state_path,
        {
            "schema": "medical-notes-workbench.specialist-task-runner-rate-limit.v1",
            "last_started_at_epoch": started_at,
            "last_work_id": work_id,
            "delay_seconds": delay_seconds,
        },
    )
    return JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.specialist-task-runner-rate-limit.v1",
            "state_path": str(state_path),
            "delay_seconds": delay_seconds,
            "waited_seconds": round(waited_seconds, 3),
            "previous_started_at_epoch": previous_started_at,
            "started_at_epoch": started_at,
        }
    )


def _write_transcript(
    *,
    path: Path,
    work_id: str,
    command: list[str],
    prompt_sha256: str,
    completed: subprocess.CompletedProcess[str],
) -> str:
    stdout, stdout_truncated = _bounded_text(completed.stdout or "")
    stderr, stderr_truncated = _bounded_text(completed.stderr or "")
    redacted_command = _redact_prompt_argument(command)
    transcript = JsonObjectAdapter.validate_python(
        {
            "schema": SPECIALIST_TASK_RUNNER_TRANSCRIPT_SCHEMA,
            "work_id": work_id,
            "harness": "gemini_cli",
            "adapter": GEMINI_CLI_SPECIALIST_ADAPTER,
            "command": redacted_command,
            "prompt_sha256": prompt_sha256,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stdout_truncated": stdout_truncated,
            "stderr": stderr,
            "stderr_truncated": stderr_truncated,
        }
    )
    _write_json(path, transcript)
    return _sha256_bytes(path.read_bytes())


def _transcript_path_for_attempt(base_path: Path, *, attempt: int, final: bool) -> Path:
    if final or attempt <= 0:
        return base_path
    return base_path.with_name(f"{base_path.stem}.attempt-{attempt}{base_path.suffix}")


def _retryable_gemini_stream_failure(*, stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return (
        "invalid stream" in combined
        or "empty response" in combined
        or "malformed tool call" in combined
        or "incomplete json" in combined
    )


def _attempt_record(
    *,
    attempt: int,
    completed: subprocess.CompletedProcess[str],
    transcript_path: Path,
    transcript_sha256: str,
    observed_model: str,
    retry_reason: str = "",
) -> JsonObject:
    return JsonObjectAdapter.validate_python(
        {
            "attempt": attempt,
            "returncode": completed.returncode,
            "observed_model": observed_model,
            "transcript_artifact_path": str(transcript_path),
            "transcript_sha256": transcript_sha256,
            "retry_reason": retry_reason,
        }
    )


def _completed_receipt(
    *,
    work_item: JsonObject,
    model: str,
    observed_model: str,
    input_packet_path: Path,
    input_packet_sha256: str,
    output_path: Path,
    transcript_artifact_path: Path,
    transcript_artifact_sha256: str,
    specialist_session_id: str,
    harness: SpecialistHarness = SpecialistHarness.GEMINI_CLI,
    adapter: str = GEMINI_CLI_SPECIALIST_ADAPTER,
    model_evidence: JsonObject | None = None,
    parent_session_id: str = "workbench-specialist-runner",
) -> JsonObject:
    typed_work_item = _typed_subagent_work_item(work_item)
    work_id = typed_work_item.work_id
    output_sha256 = _sha256_bytes(output_path.read_bytes())
    evidence = model_evidence or {
        "source": "gemini_cli_agent_metadata",
        "requested_model": model,
        "observed_provider_id": "gemini-cli",
        "observed_model_id": observed_model,
        "evidence_strength": "runtime_metadata",
        "evidence_excerpt": f"model: {observed_model}",
    }
    payload = JsonObjectAdapter.validate_python(
        {
            "schema": "medical-notes-workbench.specialist-task-run-receipt.v1",
            "work_id": work_id,
            "phase": "style_rewrite",
            "harness": harness.value,
            "adapter": adapter,
            "requested_agent": typed_work_item.agent,
            "requested_model_policy": _style_rewrite_model_policy(work_item),
            "requested_model": model,
            "observed_model": observed_model,
            "model_evidence": evidence,
            "input_packet_path": str(input_packet_path),
            "input_packet_sha256": input_packet_sha256,
            "output_path": str(output_path),
            "output_sha256": output_sha256,
            "status": "completed",
            "validation_status": "validated",
            "quality_review_status": "accepted",
            "parent_session_id": parent_session_id,
            "specialist_session_id": specialist_session_id or "gemini-cli-session",
            "transcript_artifact_path": str(transcript_artifact_path),
            "transcript_artifact_sha256": transcript_artifact_sha256,
            "error_context": {},
            "next_action": "",
            "specialist_output_receipt": {
                "schema": "medical-notes-workbench.style-rewrite-output.v1",
                "work_id": work_id,
                "phase": "style_rewrite",
                "status": "completed",
                "output_path": str(output_path),
                "output_sha256": output_sha256,
            },
            "specialist_output_attestation": {
                "schema": "medical-notes-workbench.style-rewrite-output-attestation.v1",
                "work_id": work_id,
                "phase": "style_rewrite",
                "status": "completed",
                "output_path": str(output_path),
                "output_sha256": output_sha256,
            },
        }
    )
    attested = attach_specialist_task_run_receipt_attestation(payload)
    try:
        receipt = SpecialistTaskRunReceipt.from_operation_payload(attested)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="specialist task run receipt invalid") from exc
    return receipt.to_payload()


def _receipt_path_for_work_item(work_item: JsonObject, output_path: Path) -> Path:
    typed_work_item = _typed_subagent_work_item(work_item)
    explicit = (typed_work_item.specialist_task_run_receipt_path or "").strip()
    return Path(explicit) if explicit else output_path.with_suffix(".specialist-task-run-receipt.json")


def _remove_existing_specialist_outputs(
    *,
    work_item: JsonObject,
    output_path: Path,
    receipt_path: Path,
) -> JsonObject:
    candidates = [
        output_path,
        receipt_path,
        _style_rewrite_output_receipt_path(work_item, output_path),
        _style_rewrite_output_attestation_path(work_item, output_path),
    ]
    removed: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            candidate.unlink()
            removed.append(str(candidate))
    return {
        "preexisting_output_removed": bool(removed),
        "preexisting_output_removed_paths": removed,
    }


def _remove_untrusted_specialist_outputs(
    *,
    work_item: JsonObject,
    output_path: Path,
    receipt_path: Path,
) -> JsonObject:
    cleanup = _remove_existing_specialist_outputs(
        work_item=work_item,
        output_path=output_path,
        receipt_path=receipt_path,
    )
    return {
        "untrusted_output_removed": bool(cleanup["preexisting_output_removed"]),
        "untrusted_output_removed_paths": cleanup["preexisting_output_removed_paths"],
    }


def _parsed_transcript_values(text: str) -> list[object]:
    values: list[object] = []
    stripped = text.strip()
    if stripped:
        try:
            values.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            values.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return values


def _find_list_field(value: object, field_names: set[str]) -> list[str]:
    if isinstance(value, dict):
        for key, raw in value.items():
            if key.lower() in field_names and isinstance(raw, list):
                return [str(item) for item in raw if isinstance(item, str) and item.strip()]
        for raw in value.values():
            found = _find_list_field(raw, field_names)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_list_field(item, field_names)
            if found:
                return found
    return []


def _agy_transcript_metadata(*, transcript_text: str, requested_model: str) -> JsonObject:
    parsed_values = _parsed_transcript_values(transcript_text)
    observed_model = ""
    provider = ""
    specialist_session_id = ""
    tool_sequence: list[str] = []
    for value in parsed_values:
        if not observed_model:
            observed_model = _find_string_field(
                value,
                {"model", "model_id", "modelid", "modelname", "observed_model_id", "selected_model"},
            )
        if not provider:
            provider = _find_string_field(value, {"provider", "provider_id", "observed_provider_id", "runtime"})
        if not specialist_session_id:
            specialist_session_id = _find_string_field(
                value,
                {"session_id", "sessionid", "conversation_id", "conversationid", "task_id", "taskid"},
            )
        if not tool_sequence:
            tool_sequence = _find_list_field(value, {"tool_sequence", "toolsequence", "tools"})
    if not observed_model and requested_model and requested_model in transcript_text:
        observed_model = requested_model
    if not provider and "antigravity" in transcript_text.casefold():
        provider = "antigravity-cli"
    return JsonObjectAdapter.validate_python(
        {
            "observed_model": observed_model,
            "provider": provider or "antigravity-cli",
            "specialist_session_id": specialist_session_id,
            "tool_sequence": tool_sequence,
        }
    )


def _agy_selected_model_from_runtime_log(runtime_log_text: str) -> str:
    labels = [match.group("label").strip() for match in AGY_SELECTED_MODEL_OVERRIDE_RE.finditer(runtime_log_text)]
    return labels[-1] if labels else ""


def _agy_transcript_has_native_specialist_invocation(*, transcript_text: str, metadata: JsonObject) -> bool:
    tool_sequence = metadata.get("tool_sequence")
    if isinstance(tool_sequence, list):
        names = {str(item).strip().lower() for item in tool_sequence}
        if {"define_subagent", "invoke_subagent"}.issubset(names):
            return True
    folded = transcript_text.casefold()
    return "define_subagent" in folded and "invoke_subagent" in folded


def _write_agy_transcript_artifact(
    *,
    path: Path,
    work_id: str,
    source_transcript_path: Path,
    source_transcript_sha256: str,
    metadata: JsonObject,
    model_evidence: JsonObject,
) -> str:
    artifact = JsonObjectAdapter.validate_python(
        {
            "schema": AGY_SPECIALIST_TRANSCRIPT_ARTIFACT_SCHEMA,
            "work_id": work_id,
            "harness": "agy",
            "adapter": AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            "source_transcript_path": str(source_transcript_path),
            "source_transcript_sha256": source_transcript_sha256,
            "observed_model": str(metadata.get("observed_model") or ""),
            "observed_provider_id": str(metadata.get("provider") or "antigravity-cli"),
            "specialist_session_id": str(metadata.get("specialist_session_id") or ""),
            "tool_sequence": metadata.get("tool_sequence") if isinstance(metadata.get("tool_sequence"), list) else [],
            "model_evidence": model_evidence,
            "raw_transcript_embedded": False,
        }
    )
    _write_json(path, artifact)
    return _sha256_bytes(path.read_bytes())


def _write_opencode_task_artifact(
    *,
    path: Path,
    metadata_path: Path,
    metadata_sha256: str,
    metadata: OpenCodeSpecialistTaskMetadata,
    model_evidence: JsonObject,
) -> str:
    artifact = JsonObjectAdapter.validate_python(
        {
            "schema": OPENCODE_SPECIALIST_TASK_ARTIFACT_SCHEMA,
            "work_id": metadata.work_id,
            "harness": "opencode",
            "adapter": OPENCODE_TASK_SUBAGENT_ADAPTER,
            "source_metadata_path": str(metadata_path),
            "source_metadata_sha256": metadata_sha256,
            "task_id": metadata.task_id,
            "parent_session_id": metadata.parent_session_id,
            "specialist_session_id": metadata.specialist_session_id,
            "observed_provider_id": metadata.provider_id,
            "observed_model": metadata.model_id,
            "model_tier": metadata.model_tier,
            "tool_sequence": metadata.tool_sequence,
            "prompt_contract": metadata.prompt_contract,
            "raw_content_embedded": metadata.raw_content_embedded,
            "model_evidence": model_evidence,
        }
    )
    _write_json(path, artifact)
    return _sha256_bytes(path.read_bytes())


def _opencode_model_has_forbidden_specialist_token(model_id: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", model_id.lower())
    return any(token in {"flash", "lite", "nano"} for token in tokens)


def _opencode_plan_work_item(raw_work_item: JsonObject) -> SubagentWorkItem:
    try:
        return SubagentWorkItem.model_validate(raw_work_item)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="style rewrite work item invalid") from exc


def finalize_agy_specialist_task(
    *,
    plan_path: Path,
    work_id: str,
    transcript_path: Path,
    runtime_log_path: Path | None = None,
    requested_model: str = "Gemini 3.1 Pro (High)",
) -> JsonObject:
    try:
        plan_payload = _read_json_object(plan_path, label="style rewrite plan")
        _validate_style_rewrite_plan(plan_payload)
        _verify_style_rewrite_plan_attestation(plan_payload)
    except (MissingPathError, ValidationError) as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regere o plano pela rota oficial plan-subagents antes de finalizar o especialista AGY.",
            required_inputs=["plan"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            validation={"error": str(exc)},
        )
    work_item = _style_rewrite_work_item(plan_payload, work_id)
    if work_item is None:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regere o plano; o work_id solicitado não existe no plano oficial.",
            required_inputs=["work_id"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        )
    target_path = Path(str(work_item.get("target_path") or ""))
    output_path = Path(str(work_item.get("temp_output") or work_item.get("output_path") or ""))
    receipt_path = _receipt_path_for_work_item(work_item, output_path)
    if not target_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_target_missing",
            next_action="Replaneje style-rewrite; a nota alvo não existe mais.",
            required_inputs=["target_path"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        )
    if _sha256_bytes(target_path.read_bytes()) != str(work_item.get("target_hash_before") or ""):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_stale_target_hash",
            next_action="Replaneje style-rewrite; a nota alvo mudou desde o plano.",
            required_inputs=["plan"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        )
    if not output_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_output_missing",
            next_action="Relance invoke_subagent no AGY para este work_item; o temp_output oficial não existe.",
            required_inputs=["temp_output"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        )
    if not transcript_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="agy_transcript_evidence_missing",
            next_action="Forneça o transcript/task log AGY oficial para finalizar o recibo do especialista.",
            required_inputs=["agy_transcript"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        )
    transcript_bytes = transcript_path.read_bytes()
    transcript_text = transcript_bytes.decode("utf-8", errors="replace")
    source_transcript_sha256 = _sha256_bytes(transcript_bytes)
    metadata = _agy_transcript_metadata(transcript_text=transcript_text, requested_model=requested_model)
    observed_model = str(metadata.get("observed_model") or "")
    model_evidence_source = "agy_transcript_metadata"
    model_evidence_excerpt = f"model: {observed_model}" if observed_model else ""
    if runtime_log_path is not None:
        try:
            runtime_log_text = runtime_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="agy_runtime_log_unavailable",
                next_action="Forneça o log AGY oficial para validar a troca de modelo da sessão especialista.",
                required_inputs=["agy_runtime_log"],
                work_id=work_id,
                harness=SpecialistHarness.AGY,
                requested_model=requested_model,
                plan_path=plan_path,
                target_path=target_path,
                output_path=output_path,
                receipt_path=receipt_path,
                adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
                validation={"error": str(exc), "source_transcript_sha256": source_transcript_sha256},
            )
        selected_model = _agy_selected_model_from_runtime_log(runtime_log_text)
        if selected_model and selected_model != requested_model:
            return _result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="agy_specialist_model_evidence_mismatch",
                next_action=(
                    "Repita a janela AGY settings switch com modelo especialista Pro/High; "
                    "o log runtime não comprova o modelo solicitado."
                ),
                required_inputs=["agy_model_evidence"],
                work_id=work_id,
                harness=SpecialistHarness.AGY,
                requested_model=requested_model,
                observed_model=selected_model,
                plan_path=plan_path,
                target_path=target_path,
                output_path=output_path,
                receipt_path=receipt_path,
                adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
                validation={
                    "source_transcript_sha256": source_transcript_sha256,
                    "runtime_log_path": str(runtime_log_path),
                    "observed_selected_model": selected_model,
                },
            )
        if selected_model:
            if not _agy_transcript_has_native_specialist_invocation(
                transcript_text=transcript_text,
                metadata=metadata,
            ):
                return _result(
                    status=SpecialistRunStatus.BLOCKED,
                    blocked_reason="agy_specialist_invocation_evidence_missing",
                    next_action=(
                        "Forneça transcript/task log AGY com define_subagent e invoke_subagent oficiais "
                        "para vincular a troca de modelo ao output especialista."
                    ),
                    required_inputs=["agy_transcript"],
                    work_id=work_id,
                    harness=SpecialistHarness.AGY,
                    requested_model=requested_model,
                    observed_model=selected_model,
                    plan_path=plan_path,
                    target_path=target_path,
                    output_path=output_path,
                    receipt_path=receipt_path,
                    adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
                    validation={
                        "source_transcript_sha256": source_transcript_sha256,
                        "runtime_log_path": str(runtime_log_path),
                        "observed_selected_model": selected_model,
                    },
                )
            observed_model = selected_model
            model_evidence_source = "agy_settings_snapshot"
            model_evidence_excerpt = f'selected_model_override: "{selected_model}"'
    if not observed_model:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="agy_specialist_model_evidence_missing",
            next_action=(
                "Repita a chamada AGY garantindo transcript/task log com metadado do modelo efetivo; "
                "não finalize autoria médica por alegação manual de modelo."
            ),
            required_inputs=["agy_model_evidence"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            validation={"source_transcript_sha256": source_transcript_sha256},
        )
    input_packet_path = output_path.with_suffix(".agy-input.json")
    transcript_artifact_path = output_path.with_suffix(".agy-transcript.json")
    input_packet_sha256 = _write_input_packet(
        path=input_packet_path,
        work_item=work_item,
        plan_path=plan_path,
        model=requested_model,
    )
    deterministic_fixes = _normalize_style_rewrite_output_file(target_path=target_path, output_path=output_path)
    validation = apply_style_rewrite(target_path, output_path, dry_run=True)
    validation["deterministic_fixes_applied"] = deterministic_fixes
    validation_block = validation.get("validation") if isinstance(validation, dict) else {}
    if isinstance(validation_block, dict) and validation_block.get("errors"):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_agent_contract_violation",
            next_action="Regenerar o rewrite pelo subagente AGY empacotado para este work_item.",
            required_inputs=["specialist_output"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            observed_model=observed_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            validation=validation,
        )
    if isinstance(validation_block, dict) and validation_block.get("requires_llm_rewrite"):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_still_requires_rewrite",
            next_action="Regenerar o rewrite no AGY até requires_llm_rewrite=false antes de assinar o recibo.",
            required_inputs=["specialist_output"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            observed_model=observed_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            validation=validation,
        )
    model_evidence = JsonObjectAdapter.validate_python(
        {
            "source": model_evidence_source,
            "requested_model": requested_model,
            "observed_provider_id": str(metadata.get("provider") or "antigravity-cli"),
            "observed_model_id": observed_model,
            "evidence_strength": "settings_and_transcript",
            "evidence_excerpt": model_evidence_excerpt,
        }
    )
    transcript_artifact_sha256 = _write_agy_transcript_artifact(
        path=transcript_artifact_path,
        work_id=work_id,
        source_transcript_path=transcript_path,
        source_transcript_sha256=source_transcript_sha256,
        metadata=metadata,
        model_evidence=model_evidence,
    )
    try:
        receipt = _completed_receipt(
            work_item=work_item,
            model=requested_model,
            observed_model=observed_model,
            input_packet_path=input_packet_path,
            input_packet_sha256=input_packet_sha256,
            output_path=output_path,
            transcript_artifact_path=transcript_artifact_path,
            transcript_artifact_sha256=transcript_artifact_sha256,
            specialist_session_id=str(metadata.get("specialist_session_id") or f"agy-{source_transcript_sha256[7:19]}"),
            harness=SpecialistHarness.AGY,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            model_evidence=model_evidence,
            parent_session_id="agy-parent-session",
        )
    except (MissingPathError, ValidationError, PydanticValidationError) as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="specialist_task_run_receipt_invalid",
            next_action="Corrija a evidência AGY/modelo e finalize novamente pela rota oficial.",
            required_inputs=["specialist_task_run_receipt"],
            work_id=work_id,
            harness=SpecialistHarness.AGY,
            requested_model=requested_model,
            observed_model=observed_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            transcript_artifact_path=transcript_artifact_path,
            adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
            validation={"error": str(exc), **validation},
        )
    _write_json(receipt_path, receipt)
    return _result(
        status=SpecialistRunStatus.COMPLETED,
        work_id=work_id,
        harness=SpecialistHarness.AGY,
        requested_model=requested_model,
        observed_model=observed_model,
        plan_path=plan_path,
        target_path=target_path,
        output_path=output_path,
        output_sha256=_sha256_bytes(output_path.read_bytes()),
        receipt_path=receipt_path,
        input_packet_path=input_packet_path,
        transcript_artifact_path=transcript_artifact_path,
        adapter=AGY_PACKAGED_TEMPLATE_SPECIALIST_ADAPTER,
        validation={
            "source_transcript_sha256": source_transcript_sha256,
            "transcript_artifact_sha256": transcript_artifact_sha256,
            **validation,
        },
    )


def finalize_opencode_specialist_task(
    *,
    plan_path: Path,
    work_id: str,
    task_metadata_path: Path | None,
    requested_model: str = "antigravity/gemini-3.1-pro",
) -> JsonObject:
    try:
        plan_payload = _read_json_object(plan_path, label="style rewrite plan")
        _validate_style_rewrite_plan(plan_payload)
        _verify_style_rewrite_plan_attestation(plan_payload)
    except (MissingPathError, ValidationError) as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regere o plano pela rota oficial plan-subagents antes de finalizar o especialista OpenCode.",
            required_inputs=["plan"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"error": str(exc)},
        )
    raw_work_item = _style_rewrite_work_item(plan_payload, work_id)
    if raw_work_item is None:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regere o plano; o work_id solicitado não existe no plano oficial.",
            required_inputs=["work_id"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    try:
        work_item = _opencode_plan_work_item(raw_work_item)
    except ValidationError as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_plan_contract_invalid",
            next_action="Regere o plano; o work_item de style-rewrite não passa no contrato tipado.",
            required_inputs=["plan"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"error": str(exc)},
        )
    target_path = Path(work_item.target_path or "")
    output_path = Path(work_item.temp_output or work_item.output_path or "")
    receipt_path = Path(work_item.specialist_task_run_receipt_path or "") if work_item.specialist_task_run_receipt_path else _receipt_path_for_work_item(raw_work_item, output_path)
    if not target_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_target_missing",
            next_action="Replaneje style-rewrite; a nota alvo não existe mais.",
            required_inputs=["target_path"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    if _sha256_bytes(target_path.read_bytes()) != (work_item.target_hash_before or ""):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_stale_target_hash",
            next_action="Replaneje style-rewrite; a nota alvo mudou desde o plano.",
            required_inputs=["plan"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    if not output_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_output_missing",
            next_action="Relance a task OpenCode para este work_item; o temp_output oficial não existe.",
            required_inputs=["temp_output"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    resolved_task_metadata_path = task_metadata_path or _default_opencode_task_metadata_path(work_id)
    if not resolved_task_metadata_path.exists():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_missing",
            next_action="Forneça o metadata oficial da task OpenCode para finalizar o recibo do especialista.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    try:
        metadata_payload = _read_json_object(resolved_task_metadata_path, label="OpenCode task metadata")
    except (MissingPathError, ValidationError) as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_invalid",
            next_action="Forneça metadata JSON oficial da task OpenCode.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"error": str(exc)},
        )
    if not str(metadata_payload.get("model_id") or "").strip():
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_model_evidence_missing",
            next_action="Repita a task OpenCode com metadata que exponha provider_id/model_id efetivos.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    try:
        metadata = OpenCodeSpecialistTaskMetadata.model_validate(metadata_payload)
    except PydanticValidationError as exc:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_invalid",
            next_action="Forneça metadata OpenCode que satisfaça o contrato opencode-specialist-task-metadata.v1.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"error": str(contract_error(exc, prefix="OpenCode task metadata invalid"))},
        )
    placeholder_field = _opencode_metadata_placeholder_field(metadata)
    if placeholder_field:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_placeholder",
            next_action="Forneça metadata OpenCode nativo da task; placeholders não comprovam a execução real.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"placeholder_field": placeholder_field},
        )
    if metadata.work_id != work_id:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_mismatch",
            next_action="Use metadata OpenCode gerado para o mesmo work_id do plano oficial.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"metadata_work_id": metadata.work_id},
        )
    if metadata.raw_content_embedded:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_raw_content_contract_violation",
            next_action="Relance a task OpenCode com prompt contendo apenas o work_item tipado e paths oficiais.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )
    if "task" not in metadata.tool_sequence:
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_task_metadata_invalid",
            next_action="Forneça metadata OpenCode que comprove chamada nativa de task.",
            required_inputs=["opencode_task_metadata"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={"tool_sequence": metadata.tool_sequence},
        )
    if _opencode_model_has_forbidden_specialist_token(metadata.model_id):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="opencode_specialist_model_fallback_forbidden",
            next_action="Repita a task OpenCode com modelo especialista aceito; Flash/Lite/Nano não podem assinar autoria médica.",
            required_inputs=["opencode_model_evidence"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        )

    input_packet_path = output_path.with_suffix(".opencode-input.json")
    transcript_artifact_path = output_path.with_suffix(".opencode-task.json")
    input_packet_sha256 = _write_input_packet(
        path=input_packet_path,
        work_item=raw_work_item,
        plan_path=plan_path,
        model=requested_model,
    )
    deterministic_fixes = _normalize_style_rewrite_output_file(target_path=target_path, output_path=output_path)
    validation = apply_style_rewrite(target_path, output_path, dry_run=True)
    validation["deterministic_fixes_applied"] = deterministic_fixes
    validation_block = validation.get("validation") if isinstance(validation, dict) else {}
    if isinstance(validation_block, dict) and validation_block.get("errors"):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_agent_contract_violation",
            next_action="Regenerar o rewrite pela task OpenCode oficial para este work_item.",
            required_inputs=["specialist_output"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation=validation,
        )
    if isinstance(validation_block, dict) and validation_block.get("requires_llm_rewrite"):
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="style_rewrite_still_requires_rewrite",
            next_action="Regenerar o rewrite no OpenCode até requires_llm_rewrite=false antes de assinar o recibo.",
            required_inputs=["specialist_output"],
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation=validation,
        )
    metadata_sha256 = _sha256_bytes(resolved_task_metadata_path.read_bytes())
    model_evidence = JsonObjectAdapter.validate_python(
        {
            "source": "opencode_task_metadata",
            "requested_model": requested_model,
            "observed_provider_id": metadata.provider_id,
            "observed_model_id": metadata.model_id,
            "evidence_strength": "runtime_metadata",
            "evidence_excerpt": f"opencode task metadata: {metadata.task_id}",
        }
    )
    transcript_artifact_sha256 = _write_opencode_task_artifact(
        path=transcript_artifact_path,
        metadata_path=resolved_task_metadata_path,
        metadata_sha256=metadata_sha256,
        metadata=metadata,
        model_evidence=model_evidence,
    )
    try:
        receipt = _completed_receipt(
            work_item=raw_work_item,
            model=requested_model,
            observed_model=metadata.model_id,
            input_packet_path=input_packet_path,
            input_packet_sha256=input_packet_sha256,
            output_path=output_path,
            transcript_artifact_path=transcript_artifact_path,
            transcript_artifact_sha256=transcript_artifact_sha256,
            specialist_session_id=metadata.specialist_session_id,
            harness=SpecialistHarness.OPENCODE,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            model_evidence=model_evidence,
            parent_session_id=metadata.parent_session_id,
        )
    except (MissingPathError, ValidationError, PydanticValidationError) as exc:
        lowered = str(exc).lower()
        if "forbids flash" in lowered or "forbids flash/lite/nano" in lowered:
            blocked_reason = "opencode_specialist_model_fallback_forbidden"
            required_inputs = ["opencode_model_evidence"]
            next_action = "Repita a task OpenCode com modelo especialista aceito; Flash/Lite/Nano não podem assinar autoria médica."
        elif "requires pro" in lowered or "specialist-grade" in lowered:
            blocked_reason = "opencode_specialist_model_evidence_missing"
            required_inputs = ["opencode_model_evidence"]
            next_action = "Repita a task OpenCode com metadata que comprove modelo especialista aceito."
        else:
            blocked_reason = "specialist_task_run_receipt_invalid"
            required_inputs = ["specialist_task_run_receipt"]
            next_action = "Corrija a evidência OpenCode/modelo e finalize novamente pela rota oficial."
        return _result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason=blocked_reason,
            next_action=next_action,
            required_inputs=required_inputs,
            work_id=work_id,
            harness=SpecialistHarness.OPENCODE,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            target_path=target_path,
            output_path=output_path,
            receipt_path=receipt_path,
            input_packet_path=input_packet_path,
            transcript_artifact_path=transcript_artifact_path,
            adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
            validation={
                "error": str(exc),
                "task_metadata_path": str(resolved_task_metadata_path),
                "task_metadata_sha256": metadata_sha256,
                **validation,
            },
        )
    _write_json(receipt_path, receipt)
    return _result(
        status=SpecialistRunStatus.COMPLETED,
        work_id=work_id,
        harness=SpecialistHarness.OPENCODE,
        requested_model=requested_model,
        observed_model=metadata.model_id,
        plan_path=plan_path,
        target_path=target_path,
        output_path=output_path,
        output_sha256=_sha256_bytes(output_path.read_bytes()),
        receipt_path=receipt_path,
        input_packet_path=input_packet_path,
        transcript_artifact_path=transcript_artifact_path,
        adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        validation={
            "status": "validated",
            "task_metadata_path": str(resolved_task_metadata_path),
            "task_metadata_sha256": metadata_sha256,
            "transcript_artifact_sha256": transcript_artifact_sha256,
            **validation,
        },
    )


def finalize_opencode_architect_task(
    *,
    plan_path: Path,
    work_id: str,
    task_metadata_path: Path | None,
    architect_output_path: Path | None,
    requested_model: str = "antigravity/gemini-3.1-pro",
) -> JsonObject:
    try:
        plan_payload = _read_json_object(plan_path, label="architect plan")
        plan = _validate_architect_plan(plan_payload)
        validate_subagent_plan_attestation(plan_payload)
    except (MissingPathError, ValidationError, PydanticValidationError) as exc:
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_plan_contract_invalid",
            next_action="Regere o plano pela rota oficial plan-subagents --phase architect antes de finalizar o architect OpenCode.",
            required_inputs=["plan"],
            work_id=work_id,
            requested_model=requested_model,
            plan_path=plan_path,
            validation={"error": str(exc)},
        )
    work_item = _architect_work_item(plan, work_id)
    if work_item is None:
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_work_item_missing",
            next_action="Regere o plano; o work_id solicitado não existe no plano official de architect.",
            required_inputs=["work_id"],
            work_id=work_id,
            requested_model=requested_model,
            plan_path=plan_path,
        )
    raw_file = Path(work_item.raw_file or "")
    output_path = Path(work_item.temp_output or "")
    if not raw_file.exists():
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_raw_file_missing",
            next_action="Regere o plano; o raw chat oficial não existe mais.",
            required_inputs=["raw_file"],
            work_id=work_id,
            requested_model=requested_model,
            plan_path=plan_path,
            raw_file=raw_file,
            output_path=output_path,
        )
    if not output_path.exists():
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_output_missing",
            next_action="Relance a task OpenCode para este work_item; o temp_output oficial não existe.",
            required_inputs=["temp_output"],
            work_id=work_id,
            requested_model=requested_model,
            plan_path=plan_path,
            raw_file=raw_file,
            output_path=output_path,
        )
    resolved_metadata_path = task_metadata_path or _default_opencode_task_metadata_path(work_id)
    metadata_result = _validated_opencode_task_metadata(
        path=resolved_metadata_path,
        work_id=work_id,
        requested_model=requested_model,
        plan_path=plan_path,
        raw_file=raw_file,
        output_path=output_path,
    )
    if "result" in metadata_result:
        return JsonObjectAdapter.validate_python(metadata_result["result"])
    metadata = OpenCodeSpecialistTaskMetadata.model_validate(metadata_result["metadata"])
    resolved_architect_output_path = architect_output_path or _default_opencode_task_output_path(work_id)
    try:
        architect_output = ArchitectTaskOutput.model_validate(
            _read_json_object(resolved_architect_output_path, label="architect output")
        )
    except (MissingPathError, ValidationError, PydanticValidationError) as exc:
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_output_contract_invalid",
            next_action="Forneça o artifact JSON architect-output.v1 capturado da task OpenCode.",
            required_inputs=["architect_output"],
            work_id=work_id,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            raw_file=raw_file,
            output_path=output_path,
            architect_output_path=resolved_architect_output_path,
            task_metadata_path=resolved_metadata_path,
            validation={"error": str(exc)},
        )
    if architect_output.original_path != str(raw_file) or architect_output.temp_output_path != str(output_path):
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_output_plan_mismatch",
            next_action="Relance a task usando exatamente o work_item do plano oficial; paths divergentes não podem seguir para stage.",
            required_inputs=["architect_output"],
            work_id=work_id,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            raw_file=raw_file,
            output_path=output_path,
            architect_output_path=resolved_architect_output_path,
            task_metadata_path=resolved_metadata_path,
            validation={
                "expected_raw_file": str(raw_file),
                "observed_raw_file": architect_output.original_path,
                "expected_output_path": str(output_path),
                "observed_output_path": architect_output.temp_output_path,
            },
        )
    coverage_path = Path(architect_output.coverage_path)
    try:
        coverage = validate_raw_coverage_structure(coverage_path, raw_file)
    except (MissingPathError, ValidationError, PydanticValidationError) as exc:
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_coverage_invalid",
            next_action="Regere a coverage raw-coverage.v1 a partir do note_plan e repita a finalização.",
            required_inputs=["coverage_path"],
            work_id=work_id,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            raw_file=raw_file,
            output_path=output_path,
            coverage_path=coverage_path,
            architect_output_path=resolved_architect_output_path,
            task_metadata_path=resolved_metadata_path,
            validation={"error": str(exc)},
        )

    note_style = validate_note_style_file(output_path, architect_output.staged_title, raw_file=raw_file)
    deterministic_fix: JsonObject = {}
    if _note_style_blocked(note_style):
        deterministic_fix = fix_note_style_file(output_path, architect_output.staged_title, output_path, raw_file=raw_file)
        note_style = validate_note_style_file(output_path, architect_output.staged_title, raw_file=raw_file)
    if _note_style_blocked(note_style):
        return _architect_task_result(
            status=SpecialistRunStatus.BLOCKED,
            blocked_reason="architect_note_validation_failed",
            next_action="Passe error_context e rewrite_prompt ao med-knowledge-architect e repita a finalização antes de stage-note.",
            required_inputs=["specialist_output"],
            work_id=work_id,
            requested_model=requested_model,
            observed_model=metadata.model_id,
            plan_path=plan_path,
            raw_file=raw_file,
            title=architect_output.staged_title,
            taxonomy=architect_output.taxonomy,
            output_path=output_path,
            coverage_path=coverage_path,
            architect_output_path=resolved_architect_output_path,
            task_metadata_path=resolved_metadata_path,
            validation={"note_style": note_style, "deterministic_fix": deterministic_fix},
        )

    metadata_sha256 = _sha256_bytes(resolved_metadata_path.read_bytes())
    architect_output_sha256 = _sha256_bytes(resolved_architect_output_path.read_bytes())
    model_evidence = JsonObjectAdapter.validate_python(
        {
            "source": "opencode_task_metadata",
            "requested_model": requested_model,
            "observed_provider_id": metadata.provider_id,
            "observed_model_id": metadata.model_id,
            "evidence_strength": "runtime_metadata",
            "evidence_excerpt": f"opencode task metadata: {metadata.task_id}",
        }
    )
    receipt_path = output_path.with_suffix(".architect-task-run-receipt.json")
    receipt = ArchitectTaskRunReceipt(
        work_id=work_id,
        harness=SpecialistHarness.OPENCODE,
        adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        requested_model=requested_model,
        observed_model=metadata.model_id,
        plan_path=str(plan_path),
        raw_file=str(raw_file),
        title=architect_output.staged_title,
        taxonomy=architect_output.taxonomy,
        output_path=str(output_path),
        output_sha256=_sha256_bytes(output_path.read_bytes()),
        coverage_path=str(coverage_path),
        coverage_sha256=_sha256_bytes(coverage_path.read_bytes()),
        architect_output_path=str(resolved_architect_output_path),
        architect_output_sha256=architect_output_sha256,
        task_metadata_path=str(resolved_metadata_path),
        task_metadata_sha256=metadata_sha256,
        model_evidence=model_evidence,
    ).to_payload()
    _write_json(receipt_path, receipt)
    next_step = _architect_next_serial_step(
        raw_file=raw_file,
        output_path=output_path,
        coverage_path=coverage_path,
        title=architect_output.staged_title,
        taxonomy=architect_output.taxonomy,
    )
    return _architect_task_result(
        status=SpecialistRunStatus.COMPLETED,
        work_id=work_id,
        requested_model=requested_model,
        observed_model=metadata.model_id,
        plan_path=plan_path,
        raw_file=raw_file,
        title=architect_output.staged_title,
        taxonomy=architect_output.taxonomy,
        output_path=output_path,
        coverage_path=coverage_path,
        receipt_path=receipt_path,
        architect_output_path=resolved_architect_output_path,
        task_metadata_path=resolved_metadata_path,
        next_serial_step=next_step,
        validation={
            "note_style": note_style,
            "coverage": coverage,
            "deterministic_fix": deterministic_fix,
            "task_metadata_sha256": metadata_sha256,
            "architect_output_sha256": architect_output_sha256,
        },
    )


def _architect_task_result(
    *,
    status: SpecialistRunStatus,
    work_id: str,
    requested_model: str,
    plan_path: Path,
    blocked_reason: str = "",
    next_action: str = "",
    required_inputs: list[str] | None = None,
    observed_model: str = "",
    raw_file: Path | None = None,
    title: str = "",
    taxonomy: str = "",
    output_path: Path | None = None,
    coverage_path: Path | None = None,
    receipt_path: Path | None = None,
    architect_output_path: Path | None = None,
    task_metadata_path: Path | None = None,
    validation: JsonObject | None = None,
    next_serial_step: ArchitectNextSerialStep | None = None,
) -> JsonObject:
    output_sha256 = ""
    if output_path is not None and output_path.exists():
        output_sha256 = _sha256_bytes(output_path.read_bytes())
    payload = ArchitectTaskRunnerResult(
        status=status,
        blocked_reason=blocked_reason,
        next_action=next_action,
        required_inputs=required_inputs or [],
        work_id=work_id,
        harness=SpecialistHarness.OPENCODE,
        adapter=OPENCODE_TASK_SUBAGENT_ADAPTER,
        requested_model=requested_model,
        observed_model=observed_model,
        plan_path=str(plan_path),
        raw_file=str(raw_file or ""),
        title=title,
        taxonomy=taxonomy,
        output_path=str(output_path or ""),
        output_sha256=output_sha256,
        coverage_path=str(coverage_path or ""),
        receipt_path=str(receipt_path or ""),
        architect_output_path=str(architect_output_path or ""),
        task_metadata_path=str(task_metadata_path or ""),
        validation=validation or {},
        next_serial_step=next_serial_step,
        error_context=error_context(
            phase="architect",
            blocked_reason=blocked_reason,
            root_cause=blocked_reason,
            affected_artifact=str(output_path or architect_output_path or plan_path),
            error_summary="Architect task finalizer could not validate the Workbench receipt boundary."
            if status != SpecialistRunStatus.COMPLETED
            else "",
            suggested_fix=next_action,
            next_action=next_action,
            retry_scope="single_architect_work_item",
        )
        if status != SpecialistRunStatus.COMPLETED
        else {},
    )
    return payload.to_payload()


def _validate_architect_plan(payload: JsonObject) -> SubagentBatchPlan:
    try:
        plan = SubagentBatchPlan.model_validate(payload)
    except PydanticValidationError as exc:
        raise contract_error(exc, prefix="architect_plan_contract_invalid") from exc
    if plan.phase != "architect":
        raise ValidationError("architect_plan_contract_invalid: phase must be architect")
    return plan


def _architect_work_item(plan: SubagentBatchPlan, work_id: str) -> SubagentWorkItem | None:
    for item in plan.work_items:
        if item.work_id == work_id:
            return item
    return None


def _validated_opencode_task_metadata(
    *,
    path: Path,
    work_id: str,
    requested_model: str,
    plan_path: Path,
    raw_file: Path,
    output_path: Path,
) -> JsonObject:
    if not path.exists():
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_missing",
                next_action="Forneça o metadata oficial da task OpenCode para finalizar o output do architect.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
            )
        }
    try:
        payload = _read_json_object(path, label="OpenCode architect task metadata")
    except (MissingPathError, ValidationError) as exc:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_invalid",
                next_action="Forneça metadata JSON oficial da task OpenCode.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
                validation={"error": str(exc)},
            )
        }
    if "model_id" not in payload or not str(payload["model_id"]).strip():
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_model_evidence_missing",
                next_action="Repita a task OpenCode com metadata que exponha provider_id/model_id efetivos.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
            )
        }
    try:
        metadata = OpenCodeSpecialistTaskMetadata.model_validate(payload)
    except PydanticValidationError as exc:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_invalid",
                next_action="Forneça metadata OpenCode que satisfaça o contrato opencode-specialist-task-metadata.v1.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
                validation={"error": str(contract_error(exc, prefix="OpenCode task metadata invalid"))},
            )
        }
    placeholder_field = _opencode_metadata_placeholder_field(metadata)
    if placeholder_field:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_placeholder",
                next_action="Forneça metadata OpenCode nativo da task; placeholders não comprovam a execução real.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                observed_model=metadata.model_id,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
                validation={"placeholder_field": placeholder_field},
            )
        }
    if metadata.work_id != work_id:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_mismatch",
                next_action="Use metadata OpenCode gerado para o mesmo work_id do plano oficial.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                observed_model=metadata.model_id,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
                validation={"metadata_work_id": metadata.work_id},
            )
        }
    if metadata.raw_content_embedded:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_raw_content_contract_violation",
                next_action="Relance a task OpenCode com prompt contendo apenas o work_item tipado e paths oficiais.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                observed_model=metadata.model_id,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
            )
        }
    if "task" not in metadata.tool_sequence:
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_task_metadata_invalid",
                next_action="Forneça metadata OpenCode que comprove chamada nativa de task.",
                required_inputs=["opencode_task_metadata"],
                work_id=work_id,
                requested_model=requested_model,
                observed_model=metadata.model_id,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
                validation={"tool_sequence": metadata.tool_sequence},
            )
        }
    if _opencode_model_has_forbidden_specialist_token(metadata.model_id):
        return {
            "result": _architect_task_result(
                status=SpecialistRunStatus.BLOCKED,
                blocked_reason="opencode_specialist_model_fallback_forbidden",
                next_action="Repita a task OpenCode com modelo especialista aceito; Flash/Lite/Nano não podem assinar autoria médica.",
                required_inputs=["opencode_model_evidence"],
                work_id=work_id,
                requested_model=requested_model,
                observed_model=metadata.model_id,
                plan_path=plan_path,
                raw_file=raw_file,
                output_path=output_path,
                task_metadata_path=path,
            )
        }
    return {"metadata": metadata.to_payload()}


def _note_style_blocked(payload: JsonObject) -> bool:
    errors = payload["errors"] if "errors" in payload and isinstance(payload["errors"], list) else []
    return bool(errors) or bool(payload["requires_llm_rewrite"] if "requires_llm_rewrite" in payload else False)


def _architect_next_serial_step(
    *,
    raw_file: Path,
    output_path: Path,
    coverage_path: Path,
    title: str,
    taxonomy: str,
) -> ArchitectNextSerialStep:
    return ArchitectNextSerialStep.model_validate(
        {
            "schema": ARCHITECT_NEXT_SERIAL_STEP_SCHEMA,
            "command_family": "stage-note",
            "arguments": [
                "--manifest",
                "<manifest.json>",
                "--raw-file",
                str(raw_file),
                "--coverage",
                str(coverage_path),
                "--taxonomy",
                taxonomy,
                "--title",
                title,
                "--content",
                str(output_path),
            ],
            "must_run_before": [
                "publish-batch --dry-run",
                "publish-batch",
                "run-linker",
                "another architect subagent",
            ],
            "agent_instruction": (
                "Architect output validated. Run stage-note with these arguments before publish-batch; "
                "do not inspect source code or infer an alternate finalizer."
            ),
        }
    )


def _opencode_metadata_placeholder_field(metadata: OpenCodeSpecialistTaskMetadata) -> str:
    for field_name in (
        "task_id",
        "parent_session_id",
        "specialist_session_id",
        "provider_id",
        "model_id",
    ):
        if _metadata_value_looks_placeholder(str(getattr(metadata, field_name))):
            return field_name
    return ""


def _metadata_value_looks_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return True
    if normalized in {"unknown", "none", "null", "n/a", "na", "manual", "fabricated"}:
        return True
    if normalized.startswith(("default", "placeholder")):
        return True
    return "mock" in normalized or "placeholder" in normalized


def _default_opencode_task_metadata_path(work_id: str) -> Path:
    return (
        _mednotes_app_home()
        / "hook-state"
        / "opencode-task-metadata"
        / "by-work-id"
        / f"{_safe_file_stem(work_id)}.json"
    )


def _default_opencode_task_output_path(work_id: str) -> Path:
    return (
        _mednotes_app_home()
        / "hook-state"
        / "opencode-task-output"
        / "by-work-id"
        / f"{_safe_file_stem(work_id)}.json"
    )


def _mednotes_app_home() -> Path:
    configured = os.environ.get("MEDNOTES_HOME") or str(Path.home() / ".mednotes")
    return Path(configured).expanduser()


def _safe_file_stem(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:120] or "unknown"
