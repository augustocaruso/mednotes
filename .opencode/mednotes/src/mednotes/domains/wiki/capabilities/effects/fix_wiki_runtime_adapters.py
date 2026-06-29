"""Runtime adapter bridge for fix-wiki side effects.

`health.py` composes the public workflow, but it must not own subprocess/Git
inspection or construct platform adapters inline. This module is the narrow
boundary where fix-wiki turns typed effect intent into adapter execution and
collects runtime-only guard evidence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from mednotes.domains.wiki.config import MedConfig
from mednotes.domains.wiki.contracts.effect_payloads import (
    LinkWorkflowRunEffectPayload,
    RelatedNotesExportEffectPayload,
    RelatedNotesRecoveryEffectPayload,
)
from mednotes.domains.wiki.contracts.link_runtime_artifact import normalize_link_runtime_artifact
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.kernel.effect_executor import WorkflowEffectExecutor
from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind, WorkflowEffectResult
from mednotes.platform.vault_guard import active_guard_exists


class _WorkflowEffectErrorContextFields(ContractModel):
    """Typed lens for effect error_context fields that can affect recovery."""

    model_config = ConfigDict(extra="ignore")

    blocked_reason: str = ""
    reason_code: str = ""


class _WorkflowEffectPayloadEnvelope(ContractModel):
    """Typed envelope for adapter payloads that may carry a private operation payload."""

    model_config = ConfigDict(extra="ignore")

    operation_payload: JsonObject | None = None


class VaultVersionControlMutationSummary(ContractModel):
    """Typed Git/vault mutation evidence used before projecting safety."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_id: Literal["medical-notes-workbench.vault-version-control-mutation-summary.v1"] = Field(
        default="medical-notes-workbench.vault-version-control-mutation-summary.v1",
        alias="schema",
    )
    available: bool = False
    source: Literal["git_diff_head"] = "git_diff_head"
    repo_root: str = ""
    unavailable_reason: str = ""
    changed_file_count: int = Field(default=0, ge=0, strict=True)
    markdown_changed_file_count: int = Field(default=0, ge=0, strict=True)
    insertions: int = Field(default=0, ge=0, strict=True)
    deletions: int = Field(default=0, ge=0, strict=True)
    deleted_paths: list[str] = Field(default_factory=list)
    changed_paths_sample: list[str] = Field(default_factory=list)
    status_entries: list[JsonObject] = Field(default_factory=list)


class _ArtifactSchemaFields(ContractModel):
    """Typed schema discriminator for child artifact precedence decisions."""

    model_config = ConfigDict(extra="ignore", strict=True)

    schema_id: str = Field(default="", alias="schema")


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


def _exception_detail(exc: Exception) -> str:
    """Return diagnostic text without letting exception stringification drive flow."""

    if isinstance(exc, ValidationError):
        return json.dumps(exc.errors(), ensure_ascii=False)
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return exc.__class__.__name__


def _link_artifact_contract_error(*, detail: object, effect_status: str = "", origin_state: str = "") -> JsonObject:
    """Project invalid child workflow output without inventing link workflow state."""

    error_context: JsonObject = _json_object(
        {
            "root_cause": "effect_payload_contract_invalid",
            "detail": detail,
        }
    )
    if effect_status:
        error_context = _json_object({**error_context, "effect_status": effect_status})
    if origin_state:
        error_context = _json_object({**error_context, "origin_state": origin_state})
    return _json_object(
        {
            "schema": "medical-notes-workbench.child-workflow-artifact-error.v1",
            "status": "blocked",
            "blocked_reason": "link_artifact_contract_invalid",
            "next_action": "Reexecutar /mednotes:link pela rota oficial para gerar payload FSM canonico.",
            "error_context": error_context,
        }
    )


def _link_workflow_executor(config: MedConfig, workflow_effect_executor: WorkflowEffectExecutor | None) -> WorkflowEffectExecutor:
    if workflow_effect_executor is not None:
        return workflow_effect_executor
    from mednotes.domains.wiki.capabilities.effects.effect_adapters import (
        LinkWorkflowEffectAdapter,
        RelatedNotesEffectAdapter,
        WaitExternalEffectAdapter,
        WikiSubworkflowEffectAdapter,
    )

    link_adapter = LinkWorkflowEffectAdapter(config=config)
    related_adapter = RelatedNotesEffectAdapter(config=config)
    subworkflow_adapter = WikiSubworkflowEffectAdapter(
        link_adapter=link_adapter,
        related_notes_adapter=related_adapter,
    )
    return WorkflowEffectExecutor(
        adapters={
            WorkflowEffectKind.RUN_SUBWORKFLOW: subworkflow_adapter,
            WorkflowEffectKind.WAIT_EXTERNAL: WaitExternalEffectAdapter(),
        }
    )


def _link_effect_report_from_result(result: WorkflowEffectResult) -> JsonObject:
    """Return canonical child FSM payload or a contract error, never legacy state."""

    if result.payload:
        payload = JsonObjectAdapter.validate_python(result.payload)
        try:
            normalize_link_runtime_artifact(payload)
        except (ValidationError, ValueError) as exc:
            return _link_artifact_contract_error(
                detail=_exception_detail(exc),
                effect_status=result.status.value,
                origin_state=result.effect.origin_state,
            )
        return payload

    error_context_payload = _json_object(result.error_context) if result.error_context else {}
    error_context_fields = _WorkflowEffectErrorContextFields.model_validate(error_context_payload)
    detail: JsonObject = _json_object(
        {
            "blocked_reason": error_context_fields.blocked_reason,
            "reason_code": error_context_fields.reason_code,
            "error_context": error_context_payload,
        }
    )
    return _link_artifact_contract_error(
        detail=detail,
        effect_status=result.status.value,
        origin_state=result.effect.origin_state,
    )


def _link_artifact_payload(path: Path | None) -> JsonObject:
    """Load the official link diagnosis/receipt emitted by the subworkflow adapter."""

    if path is None or not path.exists():
        return _json_object({})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _json_object({})
    if not isinstance(payload, dict):
        return _json_object({})
    artifact = JsonObjectAdapter.validate_python(payload)
    try:
        normalize_link_runtime_artifact(artifact)
    except (ValidationError, ValueError) as exc:
        return _link_artifact_contract_error(detail=_exception_detail(exc))
    return artifact


def _merge_link_effect_report_with_artifact(effect_report: JsonObject, artifact: JsonObject) -> JsonObject:
    """Prefer official link artifacts for phase payloads that drive the fix-wiki FSM."""

    if not artifact:
        return effect_report
    effect_schema = _ArtifactSchemaFields.model_validate(effect_report).schema_id
    artifact_schema = _ArtifactSchemaFields.model_validate(artifact).schema_id
    if (
        effect_schema == "medical-notes-workbench.link-fsm-result.v1"
        and artifact_schema == "medical-notes-workbench.child-workflow-artifact-error.v1"
    ):
        return effect_report
    return _json_object({**effect_report, **artifact})


def execute_link_subworkflow(
    config: MedConfig,
    *,
    workflow_effect_executor: WorkflowEffectExecutor | None,
    run_id: str,
    effect_id: str,
    diagnose: bool,
    apply: bool,
    diagnosis_path: Path,
    receipt_path: Path | None = None,
    trigger_context_path: Path | None = None,
    include_related_notes: bool = True,
    backup: bool = False,
    force_diagnose: bool = False,
    version_control_safety: JsonObject | None = None,
) -> JsonObject:
    """Execute the official `/mednotes:link` effect and return its typed artifact payload."""

    payload = LinkWorkflowRunEffectPayload(
        kind="link_run" if apply else "diagnose",
        diagnose=diagnose,
        apply=apply,
        diagnosis_path=str(diagnosis_path),
        receipt_path=str(receipt_path) if receipt_path is not None else "",
        trigger_context_path=str(trigger_context_path) if trigger_context_path is not None else "",
        no_related_notes=not include_related_notes,
        force_diagnose=force_diagnose,
        llm_disambiguation="auto",
        version_control_safety=version_control_safety if apply and version_control_safety else None,
    ).to_payload()
    effect = WorkflowEffect(
        workflow="/mednotes:fix-wiki",
        run_id=run_id,
        effect_id=effect_id,
        origin_state="run_linker_package",
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="/mednotes:link",
        payload=payload,
        mutates_resources=apply,
        rollback_declared=apply,
        no_resource_mutation=not apply,
        requires_receipt=False,
    )
    result = _link_workflow_executor(config, workflow_effect_executor).execute(effect)
    effect_report = _link_effect_report_from_result(result)
    artifact_path = receipt_path if receipt_path is not None else diagnosis_path
    return _merge_link_effect_report_with_artifact(effect_report, _link_artifact_payload(artifact_path))


def execute_related_notes_export_recovery(
    config: MedConfig,
    *,
    workflow_effect_executor: WorkflowEffectExecutor | None,
    run_id: str,
    mode: str,
) -> JsonObject:
    """Run the Related Notes export recovery adapter and normalize its operation payload."""

    effect = WorkflowEffect(
        workflow="/mednotes:fix-wiki",
        run_id=run_id,
        effect_id="fix-wiki-related-notes-export-recovery",
        origin_state="related_notes_export_recovery",
        kind=WorkflowEffectKind.RUN_SUBWORKFLOW,
        target="related_notes.export",
        payload=RelatedNotesExportEffectPayload(mode=mode).to_payload(),
        requires_receipt=False,
        no_resource_mutation=True,
    )
    result = _link_workflow_executor(config, workflow_effect_executor).execute(effect)
    payload = _json_object(result.payload)
    envelope = _WorkflowEffectPayloadEnvelope.model_validate(payload)
    operation_payload = envelope.operation_payload if envelope.operation_payload is not None else payload
    try:
        recovery = RelatedNotesRecoveryEffectPayload.from_operation_payload(operation_payload)
    except ValidationError as exc:
        return _json_object({
            "schema": "medical-notes-workbench.related-notes-export-recovery.v1",
            "phase": "related_notes_export_recovery",
            "status": "blocked",
            "blocked_reason": "related_notes_export_recovery_contract_invalid",
            "next_action": result.next_action or "Corrigir o contrato tipado do efeito e repetir pela rota oficial.",
            "error_context": {
                "root_cause": "effect_payload_contract_invalid",
                "detail": _exception_detail(exc),
            },
            "workflow_effect": {
                "kind": result.effect.kind.value,
                "effect_id": result.effect.effect_id,
                "status": result.status.value,
            },
        })
    recovery_payload = recovery.to_payload()
    merged: JsonObject = _json_object(
        {
            **_json_object(recovery.operation_payload),
            **{key: value for key, value in recovery_payload.items() if key != "operation_payload"},
        }
    )
    if "phase" not in merged:
        merged = _json_object({**merged, "phase": "related_notes_export_recovery"})
    if result.next_action and "next_action" not in merged:
        merged = _json_object({**merged, "next_action": result.next_action})
    if result.error_context and "error_context" not in merged:
        merged = _json_object({**merged, "error_context": _json_object(result.error_context)})
    return _json_object({
        **merged,
        "workflow_effect": {
            "kind": result.effect.kind.value,
            "effect_id": result.effect.effect_id,
            "status": result.status.value,
        },
    })


def _git_status_path(line: str) -> str:
    if len(line) < 4:
        return ""
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip()
    return path.strip('"')


def version_control_mutation_summary(wiki_dir: Path) -> JsonObject:
    """Collect Git mutation evidence used by the fix-wiki vault safety projector."""

    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(wiki_dir), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    try:
        repo = run_git("rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _json_object({"available": False, "source": "git_diff_head", "unavailable_reason": str(exc)})
    if repo.returncode != 0:
        return _json_object({
            "available": False,
            "source": "git_diff_head",
            "unavailable_reason": "not_a_git_repo",
        })
    try:
        status = run_git("status", "--short", "--porcelain")
        numstat = run_git("diff", "--numstat", "HEAD", "--", ".")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _json_object({"available": False, "source": "git_diff_head", "unavailable_reason": str(exc)})
    changed_paths: set[str] = set()
    deleted_paths: list[str] = []
    status_entries: list[JsonObject] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = _git_status_path(line)
        if not path:
            continue
        changed_paths.add(path)
        if "D" in code:
            deleted_paths.append(path)
        status_entries.append(_json_object({"code": code.strip() or "modified", "path": path}))
    insertions = 0
    deletions = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
        changed_paths.add(parts[2])
    markdown_paths = [path for path in changed_paths if path.endswith(".md")]
    return _json_object({
        "schema": "medical-notes-workbench.vault-version-control-mutation-summary.v1",
        "available": True,
        "source": "git_diff_head",
        "repo_root": repo.stdout.strip(),
        "changed_file_count": len(changed_paths),
        "markdown_changed_file_count": len(markdown_paths),
        "insertions": insertions,
        "deletions": deletions,
        "deleted_paths": sorted(set(deleted_paths)),
        "changed_paths_sample": sorted(changed_paths)[:25],
        "status_entries": status_entries[:50],
    })


def fix_wiki_version_control_safety(
    wiki_dir: Path,
    *,
    effective_apply: bool,
    total_changed_count: int,
    version_control_mutation_summary: JsonObject,
) -> JsonObject:
    """Project guard/Git evidence into the canonical version-control safety payload."""

    guard_active = active_guard_exists(wiki_dir)
    summary = VaultVersionControlMutationSummary.model_validate(version_control_mutation_summary)
    changed_file_count = summary.changed_file_count
    mutated = effective_apply and (total_changed_count > 0 or changed_file_count > 0)
    git_rollback_available = summary.available
    rollback_available = bool(guard_active or git_rollback_available or not mutated)
    restore_point_before: bool | str = False
    if guard_active:
        restore_point_before = "vault-guard"
    elif git_rollback_available:
        restore_point_before = "git-head"
    return {
        "resource_guard_active": guard_active,
        "run_start_seen": guard_active,
        "run_finish_seen": False,
        "restore_point_before": restore_point_before,
        "restore_point_after": False,
        "sync_status": "pending_run_finish" if guard_active else "not_checked",
        "backup_online": "pending_run_finish" if guard_active else "not_checked",
        "direct_mutation_forbidden": True,
        "mutation_without_guard": bool(mutated and not rollback_available),
        "rollback_declared": rollback_available,
        "no_resource_mutation": not mutated,
        "changed_file_count": changed_file_count,
        "agent_instruction": (
            "Este payload é emitido antes do run-finish; confira o recibo vault-run-finish "
            "para restore_point_after/sync_status finais."
        ),
    }
