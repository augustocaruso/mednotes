#!/usr/bin/env python3
"""Project adapter JSON into public FSM payloads at the entrypoint layer.

This script is tooling/composition: it may import multiple MedNotes bounded
contexts because it lives under ``bundle/scripts``. Domain packages must not use
it as a backdoor; setup/history projections stay owned by their domains.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _runtime_paths import ensure_runtime_paths
from pydantic import ValidationError as PydanticValidationError

ensure_runtime_paths()

from mednotes.domains.history.history_fsm import (  # noqa: E402
    HISTORY_WORKFLOW,
    build_history_fsm_result_from_model,
    history_fsm_payload_from_vault_payload,
)
from mednotes.domains.history.history_machine import (  # noqa: E402
    HistoryFailedEvent,
    HistoryMachine,
    HistoryState,
)
from mednotes.domains.setup.setup_fsm import (  # noqa: E402
    SETUP_WORKFLOW,
    build_setup_fsm_result,
    setup_fsm_payload_from_config_validation_payload,
    setup_fsm_payload_from_vault_payload,
)
from mednotes.domains.setup.setup_machine import (  # noqa: E402
    SetupMachine,
    SetupState,
    UnsupportedByPolicyEvent,
)
from mednotes.kernel.base import JsonObject, JsonObjectAdapter  # noqa: E402
from mednotes.kernel.fsm_model import WorkflowModel  # noqa: E402
from mednotes.kernel.state_machine import send_workflow_event  # noqa: E402

EXIT_OK = 0
EXIT_CONTRACT = 3
EXIT_IO = 5


def _read_json_argument(path_value: str) -> object:
    """Read UTF-8 JSON from a path or stdin for adapter projection commands."""

    if path_value == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path_value).read_text(encoding="utf-8"))


def _emit_json(payload: object) -> None:
    """Keep stdout as parseable JSON for agent/tool consumers."""

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _minimal_adapter_contract(command: str, payload: object) -> JsonObject:
    """Validate the private adapter shape before projecting public FSM output."""

    if not isinstance(payload, dict):
        raise ValueError("adapter payload must be a JSON object")
    data = JsonObjectAdapter.validate_python(payload)
    schema = data.get("schema")
    status = data.get("status")
    if not isinstance(schema, str) or not schema:
        raise ValueError("adapter payload missing schema")
    if not isinstance(status, str) or not status:
        raise ValueError("adapter payload missing status")
    if command == "history":
        allowed_history_schemas = {
            "medical-notes-workbench.vault-timeline.v1",
            "medical-notes-workbench.vault-restore-plan.v1",
            "medical-notes-workbench.vault-restore-apply.v1",
        }
        if schema not in allowed_history_schemas:
            raise ValueError(f"unsupported history adapter schema: {schema}")
    if command == "setup-validate-config":
        allowed_setup_config_schemas = {"medical-notes-workbench.setup-config-validation.v1"}
        if schema not in allowed_setup_config_schemas:
            raise ValueError(f"unsupported setup config validation adapter schema: {schema}")
    return data


def _setup_failed_payload(*, run_id: str, root_cause: str, exc: Exception) -> JsonObject:
    """Project setup projection failures through the setup StateChart contract."""

    model = WorkflowModel.start(
        workflow=SETUP_WORKFLOW,
        run_id=run_id,
        initial_state=SetupState.POLICY_DECISION_REQUIRED.value,
    )
    send_workflow_event(
        SetupMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        UnsupportedByPolicyEvent(
            workflow=SETUP_WORKFLOW,
            run_id=run_id,
            current_state=SetupState.POLICY_DECISION_REQUIRED.value,
            reason_code="unsupported_by_policy",
            audit_evidence={"projection_error": exc.__class__.__name__},
        ),
    )
    return build_setup_fsm_result(
        model,
        error_context={
            "root_cause": root_cause,
            "blocked_reason": root_cause,
            "affected_artifact": "project_fsm_input",
            "error_summary": str(exc),
            "suggested_fix": "Reexecutar o adapter oficial e enviar JSON válido para project_fsm.py.",
            "next_action": "setup:rerun-adapter",
            "retry_scope": "setup_adapter_projection",
            "human_decision_required": False,
            "missing_inputs": [],
        },
    ).to_payload()


def _history_failed_payload(*, run_id: str, root_cause: str, exc: Exception) -> JsonObject:
    """Project history projection failures through the history StateChart contract."""

    model = WorkflowModel.start(
        workflow=HISTORY_WORKFLOW,
        run_id=run_id,
        initial_state=HistoryState.LISTING_RESTORE_POINTS.value,
    )
    send_workflow_event(
        HistoryMachine(model=model, state_field=WorkflowModel.STATECHART_STATE_FIELD),
        HistoryFailedEvent(
            workflow=HISTORY_WORKFLOW,
            run_id=run_id,
            current_state=HistoryState.LISTING_RESTORE_POINTS.value,
            reason_code=root_cause,
            next_action="history:timeline",
            audit_evidence={"projection_error": exc.__class__.__name__, "error_summary": str(exc)},
        ),
    )
    return build_history_fsm_result_from_model(
        model,
        version_control_safety={"no_resource_mutation": True, "rollback_declared": False},
    ).to_payload()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Project vault setup adapter JSON into setup-fsm-result.v1.")
    setup.add_argument("--input", required=True, help="UTF-8 JSON emitted by vault_git.py setup, or '-' for stdin.")
    setup.add_argument("--run-id", default="setup-vault", help="Run id to embed in the setup FSM payload.")
    setup.add_argument("--json", action="store_true", help="Emit JSON. Accepted for explicitness; output is always JSON.")

    setup_config = sub.add_parser(
        "setup-validate-config",
        help="Project setup config validation adapter JSON into setup-fsm-result.v1.",
    )
    setup_config.add_argument(
        "--input",
        required=True,
        help="UTF-8 JSON emitted by the setup config validation adapter, or '-' for stdin.",
    )
    setup_config.add_argument(
        "--run-id",
        default="setup-config-validation",
        help="Run id to embed in the setup FSM payload.",
    )
    setup_config.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON. Accepted for explicitness; output is always JSON.",
    )

    history = sub.add_parser("history", help="Project vault history adapter JSON into history-fsm-result.v1.")
    history.add_argument("--input", required=True, help="UTF-8 JSON emitted by vault_git.py timeline/restore, or '-' for stdin.")
    history.add_argument("--run-id", default="history-vault", help="Run id to embed in the history FSM payload.")
    history.add_argument("--json", action="store_true", help="Emit JSON. Accepted for explicitness; output is always JSON.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        raw_payload = _read_json_argument(args.input)
        adapter_payload = _minimal_adapter_contract(args.command, raw_payload)
        if args.command == "setup":
            _emit_json(setup_fsm_payload_from_vault_payload(adapter_payload, run_id=args.run_id))
            return EXIT_OK
        if args.command == "setup-validate-config":
            _emit_json(setup_fsm_payload_from_config_validation_payload(adapter_payload, run_id=args.run_id))
            return EXIT_OK
        if args.command == "history":
            _emit_json(history_fsm_payload_from_vault_payload(adapter_payload, run_id=args.run_id))
            return EXIT_OK
    except json.JSONDecodeError as exc:
        if args.command in {"setup", "setup-validate-config"}:
            _emit_json(_setup_failed_payload(run_id=args.run_id, root_cause="invalid_json", exc=exc))
        elif args.command == "history":
            _emit_json(_history_failed_payload(run_id=args.run_id, root_cause="invalid_json", exc=exc))
        return EXIT_IO
    except (PydanticValidationError, ValueError) as exc:
        if args.command in {"setup", "setup-validate-config"}:
            _emit_json(
                _setup_failed_payload(
                    run_id=args.run_id,
                    root_cause="effect_payload_contract_invalid",
                    exc=exc,
                )
            )
        elif args.command == "history":
            _emit_json(
                _history_failed_payload(
                    run_id=args.run_id,
                    root_cause="effect_payload_contract_invalid",
                    exc=exc,
                )
            )
        return EXIT_CONTRACT
    raise ValueError(f"unknown projection command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
