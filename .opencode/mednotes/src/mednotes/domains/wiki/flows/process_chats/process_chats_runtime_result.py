"""Typed runtime boundary for `/mednotes:process-chats`.

`publish-batch` still returns an operational JSON payload. This module owns the
translation from that payload into one canonical `ProcessChatsMachine` event so
the public projector in `process_chats_fsm.py` does not classify runtime status.
"""

from __future__ import annotations

from mednotes.domains.wiki.flows.process_chats.process_chats_fsm import (
    ProcessChatsFsmFacts,
    ProcessChatsLinkerDiagnostic,
    ProcessChatsOperationalSummary,
    ProcessChatsPublishDiagnostic,
    ProcessChatsPublishOperationResult,
    build_process_chats_fsm_result,
)
from mednotes.domains.wiki.flows.process_chats.process_chats_machine import (
    PROCESS_CHATS_WORKFLOW,
    ProcessChatsErrorContext,
    ProcessChatsPublishRuntimeObservation,
    ProcessChatsPublishRuntimeObservedEvent,
    ProcessChatsState,
)
from mednotes.kernel.base import JsonObject
from mednotes.kernel.workflow import VersionControlSafety


def process_chats_fsm_payload_from_publish_result(
    result: JsonObject,
    *,
    run_id: str,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> JsonObject:
    return build_process_chats_fsm_result(
        process_chats_fsm_facts_from_publish_result(
            result,
            run_id=run_id,
            version_control_safety=version_control_safety,
        )
    ).to_payload()


def process_chats_fsm_facts_from_publish_result(
    result: JsonObject,
    *,
    run_id: str,
    version_control_safety: VersionControlSafety | dict[str, object],
) -> ProcessChatsFsmFacts:
    typed_result = ProcessChatsPublishOperationResult.model_validate(result)
    observation = _publish_observation_from_result(typed_result)
    initial_state = _publish_observation_source_state(observation)
    event = ProcessChatsPublishRuntimeObservedEvent(
        workflow=PROCESS_CHATS_WORKFLOW,
        run_id=run_id,
        current_state=initial_state.value,
        observation=observation,
        audit_evidence=_publish_observation_audit_evidence(typed_result),
    )
    event_error_context = observation.error_context
    error_context = typed_result.error_context
    if not error_context and isinstance(event_error_context, ProcessChatsErrorContext):
        error_context = event_error_context.to_payload()
    return ProcessChatsFsmFacts(
        run_id=run_id,
        initial_state=initial_state,
        event=event,
        operational_summary=_operational_summary_from_publish_result(typed_result),
        version_control_safety=version_control_safety,
        error_context=error_context,
    )


def _publish_observation_from_result(result: ProcessChatsPublishOperationResult) -> ProcessChatsPublishRuntimeObservation:
    """Return the canonical facts emitted by the publish/link producer."""

    return result.runtime_observation


def _publish_observation_source_state(observation: ProcessChatsPublishRuntimeObservation) -> ProcessChatsState:
    """Use the validated entry state supplied with the canonical observation."""

    return observation.source_state


def _operational_summary_from_publish_result(
    result: ProcessChatsPublishOperationResult,
) -> ProcessChatsOperationalSummary:
    """Build non-authoritative counts and diagnostics for reports only."""

    linker = result.linker
    return ProcessChatsOperationalSummary(
        note_count=_note_count(result),
        raw_count=_raw_count(result),
        coverage_raw_count=_coverage_raw_count(result),
        planned_note_count=_planned_note_count(result),
        mutated=_mutated(result),
        changed_files=_changed_files(result),
        blocked_item_count=max(1, linker.blocker_count) if linker is not None and linker.blocker_count else 0,
        next_action=result.next_action,
        publish=ProcessChatsPublishDiagnostic(
            status=result.status,
            receipt_status=result.publish_receipt.status if result.publish_receipt is not None else "",
            dry_run=result.dry_run,
            manifest=result.manifest,
            dry_run_receipt=result.dry_run_receipt,
            new_taxonomy_leaf_authorization=result.new_taxonomy_leaf_authorization,
        ),
        linker=ProcessChatsLinkerDiagnostic(
            status=linker.status if linker is not None else "",
            next_action=linker.next_action if linker is not None else "",
            diagnosis_status=linker.diagnosis_status if linker is not None else "",
            applied=_linker_applied(result),
            skipped_reason=result.linker_skipped_reason or (linker.linker_skipped_reason if linker is not None else ""),
            blocker_count=linker.blocker_count if linker is not None else 0,
        ),
        artifacts=_artifacts(result),
    )


def _note_count(payload: ProcessChatsPublishOperationResult) -> int:
    return payload.created_count or (payload.publish_receipt.published_count if payload.publish_receipt else 0) or len(payload.created)


def _raw_count(payload: ProcessChatsPublishOperationResult) -> int:
    return payload.processed_raw_count or len(payload.raw_updates)


def _coverage_raw_count(payload: ProcessChatsPublishOperationResult) -> int:
    coverage = payload.coverage_summary or payload.coverage
    count = (coverage.raw_file_count if coverage is not None else 0) or (coverage.covered_count if coverage is not None else 0)
    if count:
        return count
    total = 0
    for batch in payload.planned_batches:
        raw_files = batch.raw_files
        total += len(raw_files)
        if not raw_files and batch.raw_file:
            total += 1
    return total


def _planned_note_count(payload: ProcessChatsPublishOperationResult) -> int:
    return sum(len(batch.notes) for batch in payload.planned_batches)


def _linker_applied(payload: ProcessChatsPublishOperationResult) -> bool:
    return bool(payload.linker_applied or (payload.linker is not None and payload.linker.linker_applied))


def _changed_files(payload: ProcessChatsPublishOperationResult) -> list[str]:
    return [item for item in payload.created if item.strip()]


def _mutated(payload: ProcessChatsPublishOperationResult) -> bool:
    return not payload.dry_run and bool(_changed_files(payload) or _raw_count(payload))


def _artifacts(publish_result: ProcessChatsPublishOperationResult) -> JsonObject:
    artifacts: JsonObject = {}
    if publish_result.manifest:
        artifacts["manifest"] = publish_result.manifest
    if publish_result.link_trigger_context_path:
        artifacts["link_trigger_context_path"] = publish_result.link_trigger_context_path
    if publish_result.linker_diagnosis_path:
        artifacts["linker_diagnosis_path"] = publish_result.linker_diagnosis_path
    if publish_result.linker_receipt_path:
        artifacts["linker_receipt_path"] = publish_result.linker_receipt_path
    if publish_result.dry_run_receipt is not None and publish_result.dry_run_receipt.path:
        artifacts["dry_run_receipt_manifest"] = publish_result.dry_run_receipt.path
    return artifacts


def _publish_observation_audit_evidence(result: ProcessChatsPublishOperationResult) -> JsonObject:
    return {
        "adapter_schema": result.schema_id or "",
        "adapter_phase": result.phase,
        "adapter_status": result.status,
        "adapter_reason": result.blocked_reason,
        "counts": {
            "created_count": result.created_count,
            "processed_raw_count": result.processed_raw_count,
            "changed_file_count": len(_changed_files(result)),
        },
    }
