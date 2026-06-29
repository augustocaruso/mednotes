"""Structured local feedback records for public workflow executions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mednotes.kernel.base import JsonObject, JsonObjectAdapter, JsonValue
from mednotes.kernel.guardrails import (
    CONTRACT_GAP_MISSING_NEXT_ACTION,
)
from mednotes.kernel.guardrails import (
    default_contract_next_action as _shared_default_contract_next_action,
)
from mednotes.kernel.guardrails import (
    needs_next_action_hardening as _shared_needs_next_action_hardening,
)
from mednotes.kernel.public_report import FsmFirstPayloadSummary
from mednotes.platform.paths import user_state_dir

RUN_RECORD_SCHEMA = "medical-notes-workbench.workflow-run-record.v1"
BACKLOG_SCHEMA = "medical-notes-workbench.workflow-improvement-backlog.v1"
AGENT_HOOK_EVENT_SCHEMA = "medical-notes-workbench.agent-hook-event.v1"
AGENT_HOOK_ERROR_SCHEMA = "medical-notes-workbench.agent-hook-error.v1"
PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA = "medical-notes-workbench.pre-update-extension-snapshot.v1"
TELEMETRY_EVIDENCE_SCHEMA = "medical-notes-workbench.telemetry-evidence.v1"
ENVIRONMENT_BLOCKER_CODE = "environment_blocker.windows_path_or_venv"
DEFAULT_ROOT = "~/.mednotes/feedback"
FSM_FIRST_RUN_RECORD_SCHEMAS = {
    "medical-notes-workbench.fix-wiki-fsm-result.v1",
    "medical-notes-workbench.flashcards-fsm-result.v1",
    "medical-notes-workbench.link-fsm-result.v1",
    "medical-notes-workbench.link-related-fsm-result.v1",
    "medical-notes-workbench.process-chats-fsm-result.v1",
    "medical-notes-workbench.setup-fsm-result.v1",
    "medical-notes-workbench.history-fsm-result.v1",
}
MAX_SNIPPET_CHARS = 420
MAX_RELEVANT_PATHS = 24
MAX_PATH_HASH_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_ITEMS = 3
MAX_AGENT_EVENT_SAMPLES = 3
MAX_AGENT_EVENTS = 20
MAX_HOOK_EVENTS = 50
MAX_HOOK_ERRORS = 25
MAX_GENERATED_SCRIPTS = 12
MAX_COMMAND_EVENTS = 20
MAX_SCRIPT_CONTENT_CHARS = 48 * 1024
MAX_CONSOLE_TAIL_CHARS = 16 * 1024
MAX_HOOK_ERROR_CHARS = 8 * 1024
MAX_PRE_UPDATE_PATCH_CHARS = 160 * 1024
DEFAULT_RUN_RECORD_MAX_FILES = 200
DEFAULT_RUN_RECORD_RETENTION_DAYS = 14
DEFAULT_PRE_UPDATE_SNAPSHOT_MAX_DIRS = 5
DEFAULT_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS = 7
DEFAULT_HOOK_EVENT_RETENTION_DAYS = 1
AGENT_EMPTY_RECORD_INHERIT_SECONDS = 15 * 60
INHERITABLE_WORKFLOW_STATUSES = {
    "failed": 3,
    "error": 3,
    "blocked": 2,
    "completed_with_warnings": 1,
}
PRE_UPDATE_PATCH_NOISE_PARTS = (
    "__pycache__/",
    ".venv/",
    "node_modules/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".egg-info/",
)

ARTIFACT_STATE_KEYS = {
    "batch_id",
    "run_id",
    "note_plan_hash",
    "coverage_hash",
    "manifest_hash",
    "manifest_sha256",
    "source_artifact_hash",
    "dry_run_options_hash",
}

COUNT_KEYS = {
    "count",
    "file_count",
    "changed_count",
    "written_count",
    "error_count",
    "warning_count",
    "write_error_count",
    "requires_llm_rewrite_count",
    "taxonomy_issue_count",
    "taxonomy_applied_move_count",
    "graph_error_count",
    "blocker_count",
    "links_planned",
    "links_rewritten",
    "files_scanned",
    "files_changed",
    "candidate_count",
    "new_count",
    "duplicate_count",
    "changed_source_count",
    "anki_note_count",
    "processed_note_count",
    "created_card_count",
    "duplicate_card_count",
    "skipped_note_count",
    "model_error_count",
    "anki_error_count",
    "inserted_count",
    "enriched_count",
    "skipped_count",
    "no_insert_count",
    "failure_count",
}

PATH_KEY_HINTS = ("path", "file", "dir", "manifest", "receipt", "output", "target")
TITLE_KEY_HINTS = ("title", "titulo", "título")
HASH_KEY_HINTS = ("hash", "sha", "sha256", "digest")
SECRET_KEYS = {"token", "auth_token", "api_key", "apikey", "secret", "password", "authorization", "bearer"}
LONG_TEXT_KEYS = {"content", "markdown", "html", "raw_chat", "note_text"}
SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd"}
AGENT_RELEVANT_DRIFT_KINDS = {"launcher", "prompt", "runbook", "documentation", "script"}
AGENT_RELEVANT_DRIFT_PREFIXES = (
    "commands/",
    "docs/",
    "scripts/",
    "skills/",
)
RETRY_BUDGETS = {
    "rewrite": {
        "max_attempts": 2,
        "rule": "Reescrita clínica/determinística deve parar após duas tentativas e preservar error_context.",
    },
    "publish_rollback": {
        "max_attempts": 0,
        "rule": "Falha após início de publish não deve ser repetida automaticamente; rollback e revisão primeiro.",
    },
    "dry_run": {
        "max_attempts": 1,
        "rule": "Dry-run só deve repetir se manifest, blockers ou opções mudaram.",
    },
    "coverage_stage": {
        "max_attempts": 1,
        "rule": "Coverage/stage só deve repetir se coverage, manifest ou nota staged mudaram.",
    },
    "triage_correction": {
        "max_attempts": 1,
        "rule": "Correção de triagem só deve repetir se note_plan mudou.",
    },
}
ENVIRONMENT_PATTERN_CODES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bwinerror\s*\d+\b", re.IGNORECASE), "windows_error"),
    (re.compile(r"microsoft\\windowsapps", re.IGNORECASE), "windows_store_python_alias"),
    (
        re.compile(r"executionpolicy|running scripts is disabled|cannot be loaded because running scripts", re.IGNORECASE),
        "powershell_execution_policy",
    ),
    (
        re.compile(r"\buv(?:\.exe)?\b.{0,120}(not found|not recognized|could not find|no such file|failed)", re.IGNORECASE),
        "uv_unavailable",
    ),
    (
        re.compile(r"(not found|not recognized|could not find|no such file).{0,120}\buv(?:\.exe)?\b", re.IGNORECASE),
        "uv_unavailable",
    ),
    (re.compile(r"uv_project_environment|persistent_venv|\.venv[\\/](scripts|bin)", re.IGNORECASE), "persistent_venv"),
    (
        re.compile(
            r"\bpython(?:\.exe)?\b.{0,120}(not found|not recognized|could not find|no such file)|no module named",
            re.IGNORECASE,
        ),
        "python_environment",
    ),
    (re.compile(r"max_path|long path|filename too long|file name too long", re.IGNORECASE), "windows_long_path"),
    (re.compile(r"[A-Za-z]:\\[^\r\n]*", re.IGNORECASE), "windows_path"),
    (re.compile(r"\r\n"), "crlf"),
    (re.compile(r"\b(powershell|pwsh|set-content|out-file)\b", re.IGNORECASE), "powershell_command"),
)

STRONG_ENVIRONMENT_CODES = {
    "windows_error",
    "windows_store_python_alias",
    "powershell_execution_policy",
    "uv_unavailable",
    "persistent_venv",
    "python_environment",
    "windows_long_path",
}


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def feedback_root(root: str | Path | None = None) -> Path:
    value = root or os.getenv("MEDNOTES_FEEDBACK_DIR")
    if not value:
        value = user_state_dir() / "feedback"
    return Path(os.path.expandvars(str(value))).expanduser()


def command_string(argv: list[str] | None = None) -> str:
    values = list(sys.argv if argv is None else argv)
    return " ".join(_quote_arg(item) for item in values)


def _quote_arg(value: str) -> str:
    if not value:
        return "''"
    if re.search(r"\s|['\"]", value):
        return json.dumps(value, ensure_ascii=False)
    return value


def redact_snippet(value: Any, *, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    text = str(value)
    text = re.sub(r"```.*?```", "[code omitted]", text, flags=re.DOTALL)
    text = _redact_sensitive_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def redact_operational_text(value: Any, *, max_chars: int = MAX_SCRIPT_CONTENT_CHARS) -> str:
    text = _redact_sensitive_text(str(value))
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _redact_operational_identifier(value: Any, *, max_chars: int = 120) -> str:
    text = str(value or "").strip()
    if _looks_like_operational_identifier(text):
        return text[: max_chars - 3].rstrip() + "..." if len(text) > max_chars else text
    return redact_snippet(text, max_chars=max_chars)


def _redact_sensitive_text(text: str) -> str:
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)(\s*[:=]\s*)([\"']?)[^\s\"']+",
        r"\1\2[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:api-key|auth-token|token|secret|password)\s+)([^\s\"']+)",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"https?://[^\s)>\"]+",
        lambda match: _redact_url(match.group(0)),
        text,
    )
    return re.sub(
        r"\b[A-Za-z0-9_=-]{36,}\b",
        lambda match: match.group(0) if _looks_like_public_slug(match.group(0)) else "[redacted-token]",
        text,
    )


def _redact_url(url: str) -> str:
    if "?" not in url:
        return url
    head, _query = url.split("?", 1)
    return f"{head}?[redacted]"


def _looks_like_public_slug(value: str) -> bool:
    return bool(
        "-" in value
        and value.lower() == value
        and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*){2,}", value)
    )


def _looks_like_operational_identifier(value: str) -> bool:
    return bool(
        value
        and value.lower() == value
        and ("-" in value or "_" in value)
        and re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+", value)
    )


def _json_object_view(value: object) -> JsonObject:
    """Validate raw JSON-ish evidence before operational fields can be read."""

    if not isinstance(value, dict):
        return {}
    return JsonObjectAdapter.validate_python(value)


def _json_value(source: JsonObject, key: str) -> JsonValue:
    if key not in source:
        return None
    return JsonObjectAdapter.validate_python({"value": source[key]})["value"]


def _json_text(source: JsonObject, key: str) -> str:
    value = _json_value(source, key)
    return value.strip() if isinstance(value, str) else ""


def _json_object_field(source: JsonObject, key: str) -> JsonObject:
    return _json_object_view(_json_value(source, key))


def _json_list_field(source: JsonObject, key: str) -> list[JsonValue]:
    value = _json_value(source, key)
    return value if isinstance(value, list) else []


def _json_bool_field(source: JsonObject, key: str) -> bool:
    value = _json_value(source, key)
    return value if isinstance(value, bool) else False


def _json_int_field(source: JsonObject, key: str) -> int | None:
    value = _json_value(source, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def summarize_payload(payload: object) -> JsonObject:
    summary: JsonObject = {
        "counts": {},
        "warnings": [],
        "errors": [],
        "required_inputs": [],
        "relevant_paths": [],
        "path_hashes": {},
        "title_fields": {},
        "artifact_state": {},
        "signals": [],
    }
    if not isinstance(payload, dict):
        return summary

    payload_view = _json_object_view(payload)
    progress = _json_object_field(payload_view, "progress_view_model")
    decision = _json_object_field(payload_view, "decision")
    error_context = _json_object_field(payload_view, "error_context")
    receipt = _json_object_field(payload_view, "receipt")

    if _json_text(payload_view, "schema") in FSM_FIRST_RUN_RECORD_SCHEMAS:
        fsm_summary = FsmFirstPayloadSummary.from_payload(payload_view).to_payload()
        summary.update(fsm_summary)
    else:
        summary["phase"] = _json_text(payload_view, "phase") or _json_text(progress, "phase") or _json_text(payload_view, "command")
        summary["status"] = (
            _json_text(payload_view, "status")
            or _json_text(progress, "status")
            or _json_text(receipt, "status")
            or _status_from_payload(payload_view)
        )
        summary["blocked_reason"] = (
            _json_text(payload_view, "blocked_reason")
            or _json_text(error_context, "blocked_reason")
            or _json_text(error_context, "root_cause")
            or _json_text(decision, "reason_code")
            or _blocked_reason_from_payload(payload_view)
        )
        summary["next_action"] = (
            _json_text(payload_view, "next_action")
            or _json_text(decision, "next_action")
            or _json_text(error_context, "next_action")
            or _json_text(receipt, "next_action")
            or _json_text(payload_view, "next_command")
        )
        summary["human_decision_required"] = bool(
            _json_bool_field(payload_view, "human_decision_required")
            or bool(_json_object_field(payload_view, "human_decision_packet"))
            or bool(_json_list_field(payload_view, "human_decision_packets"))
            or _json_text(decision, "kind") == "ask_human"
        )
        summary["dry_run"] = _json_bool_field(payload_view, "dry_run") if "dry_run" in payload_view else None
        summary["apply"] = _json_bool_field(payload_view, "apply") if "apply" in payload_view else None
        workflow_exit_code = _json_int_field(payload_view, "workflow_exit_code")
        if workflow_exit_code is not None:
            summary["workflow_exit_code"] = workflow_exit_code
        for key in ("process_chats_terminal_state", "process_chats_backlog_state"):
            value = _json_text(payload_view, key)
            if value:
                summary[key] = _redact_operational_identifier(value, max_chars=120)

        required = _json_list_field(payload_view, "required_inputs")
        if not required:
            required = _json_list_field(decision, "required_inputs")
        if not required:
            required = _json_list_field(error_context, "required_inputs")
        if not required:
            required = _json_list_field(receipt, "required_inputs")
        if required:
            summary["required_inputs"] = [str(item) for item in required]

    counts: dict[str, int | float] = {}
    _collect_counts(payload, counts)
    summary["counts"] = counts

    warnings: list[str] = []
    errors: list[str] = []
    _collect_messages(payload, warnings=warnings, errors=errors)
    summary["warnings"] = warnings[:20]
    summary["errors"] = errors[:20]

    paths = _collect_paths(payload)
    summary["relevant_paths"] = paths
    summary["path_hashes"] = _hash_paths(paths)
    summary["title_fields"] = _collect_title_fields(payload)
    summary["artifact_state"] = _collect_artifact_state(payload)
    summary["signals"] = _signals_from_payload(payload, summary)
    return summary


def _is_empty_agent_feedback_payload(payload: dict[str, Any]) -> bool:
    meaningful_keys = {
        "status",
        "phase",
        "blocked_reason",
        "next_action",
        "required_inputs",
        "human_decision_required",
        "dry_run",
        "apply",
        "agent_events",
        "error_context",
        "diagnostic_context",
        "warnings",
        "errors",
    }
    for key in meaningful_keys:
        value = payload.get(key)
        if value not in (None, "", [], {}, False):
            return False
    return True


def _recorded_at_datetime(record: dict[str, Any]) -> datetime | None:
    raw = str(record.get("recorded_at") or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def prune_local_feedback(*, root: str | Path | None = None) -> dict[str, object]:
    feedback = feedback_root(root)
    return {
        "schema": "medical-notes-workbench.local-feedback-retention.v1",
        "runs": _prune_json_files(
            feedback / "runs",
            max_files=_env_int("MEDNOTES_FEEDBACK_RUN_MAX_FILES", DEFAULT_RUN_RECORD_MAX_FILES, minimum=0, maximum=5000),
            retention_days=_env_float(
                "MEDNOTES_FEEDBACK_RUN_RETENTION_DAYS",
                DEFAULT_RUN_RECORD_RETENTION_DAYS,
                minimum=0.04,
                maximum=365,
            ),
        ),
        "pre_update_snapshots": _prune_directories(
            feedback / "pre-update-snapshots",
            max_dirs=_env_int("MEDNOTES_PRE_UPDATE_SNAPSHOT_MAX_DIRS", DEFAULT_PRE_UPDATE_SNAPSHOT_MAX_DIRS, minimum=0, maximum=200),
            retention_days=_env_float(
                "MEDNOTES_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS",
                DEFAULT_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS,
                minimum=0.04,
                maximum=365,
            ),
        ),
        "hook_events": _prune_json_files(
            feedback / "hook-events",
            max_files=_env_int("MEDNOTES_HOOK_EVENT_MAX_FILES", MAX_HOOK_EVENTS, minimum=0, maximum=1000),
            retention_days=_hook_retention_days("MEDNOTES_HOOK_EVENT_RETENTION_HOURS"),
        ),
        "hook_errors": _prune_json_files(
            feedback / "hook-errors",
            max_files=_env_int("MEDNOTES_HOOK_ERROR_MAX_FILES", MAX_HOOK_ERRORS, minimum=0, maximum=1000),
            retention_days=_hook_retention_days("MEDNOTES_HOOK_ERROR_RETENTION_HOURS"),
        ),
    }


def _hook_retention_days(specific_env: str) -> float:
    hours = _env_float(
        specific_env,
        _env_float("MEDNOTES_HOOK_RETENTION_HOURS", DEFAULT_HOOK_EVENT_RETENTION_DAYS * 24, minimum=1, maximum=24 * 30),
        minimum=1,
        maximum=24 * 30,
    )
    return hours / 24


def _prune_json_files(directory: Path, *, max_files: int, retention_days: float) -> dict[str, object]:
    if not directory.exists():
        return {"path": str(directory), "removed_count": 0, "remaining_count": 0, "error_count": 0}
    try:
        files = [path for path in directory.glob("*.json") if path.is_file()]
    except OSError:
        return {"path": str(directory), "removed_count": 0, "remaining_count": 0, "error_count": 1}
    removed, errors = _remove_retention_victims(files, max_items=max_files, retention_days=retention_days, remove=lambda path: path.unlink())
    remaining = len([path for path in directory.glob("*.json") if path.is_file()])
    return {"path": str(directory), "removed_count": removed, "remaining_count": remaining, "error_count": errors}


def _prune_directories(directory: Path, *, max_dirs: int, retention_days: float) -> dict[str, object]:
    if not directory.exists():
        return {"path": str(directory), "removed_count": 0, "remaining_count": 0, "error_count": 0}
    try:
        dirs = [path for path in directory.iterdir() if path.is_dir()]
    except OSError:
        return {"path": str(directory), "removed_count": 0, "remaining_count": 0, "error_count": 1}
    removed, errors = _remove_retention_victims(dirs, max_items=max_dirs, retention_days=retention_days, remove=shutil.rmtree)
    remaining = len([path for path in directory.iterdir() if path.is_dir()])
    return {"path": str(directory), "removed_count": removed, "remaining_count": remaining, "error_count": errors}


def _remove_retention_victims(
    paths: list[Path],
    *,
    max_items: int,
    retention_days: float,
    remove: Callable[[Path], None],
) -> tuple[int, int]:
    cutoff = time.time() - retention_days * 86400
    ordered = sorted(paths, key=lambda item: (_mtime(item), item.name), reverse=True)
    victims: list[Path] = []
    survivors: list[Path] = []
    for item in ordered:
        if _mtime(item) < cutoff:
            victims.append(item)
        else:
            survivors.append(item)
    victims.extend(survivors[max(0, max_items):])
    removed = 0
    errors = 0
    for item in victims:
        try:
            remove(item)
            removed += 1
        except OSError:
            errors += 1
    return removed, errors


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _recent_inheritable_workflow_record(
    *,
    workflow: str,
    root: str | Path | None,
    started: float,
    ) -> JsonObject | None:
    runs_dir = feedback_root(root) / "runs"
    if not runs_dir.exists():
        return None
    cutoff = datetime.fromtimestamp(max(0, started - AGENT_EMPTY_RECORD_INHERIT_SECONDS), UTC)
    best: tuple[int, datetime, int, JsonObject] | None = None
    for path in sorted(runs_dir.glob("*.json"))[-80:]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        record_view = _json_object_view(record)
        if _json_text(record_view, "workflow") != workflow:
            continue
        status = _record_observed_text(record_view, "status")
        rank = INHERITABLE_WORKFLOW_STATUSES.get(status, 0)
        if rank <= 0:
            continue
        recorded_at = _recorded_at_datetime(record)
        if recorded_at is None or recorded_at < cutoff:
            continue
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        if best is None or rank > best[0] or (rank == best[0] and (recorded_at, mtime_ns) > (best[1], best[2])):
            best = (rank, recorded_at, mtime_ns, record_view)
    return best[3] if best else None


def _feedback_summary_command(command: str) -> bool:
    slug = _code_slug(command)
    return "feedback_report_py_record" in slug and "agent" in slug


def _record_observed_text(record: JsonObject, key: str) -> str:
    """Read run-record observations without treating legacy root fields as truth."""

    observed = _json_object_field(record, "observed")
    summary = _json_object_field(record, "payload_summary")
    return _json_text(observed, key) or _json_text(summary, key) or _json_text(record, key)


def _agent_feedback_payload_needs_inheritance(payload: dict[str, Any], *, command: str) -> bool:
    if _is_empty_agent_feedback_payload(payload):
        return True
    if not _feedback_summary_command(command):
        return False
    evidence_keys = (
        "error_context",
        "agent_events",
        "command_events",
        "diagnosis_path",
        "diagnosis",
        "manifest_path",
        "receipt_path",
        "operational_evidence",
    )
    return not any(payload.get(key) not in (None, "", [], {}) for key in evidence_keys)


def _inherited_workflow_error_context(previous: JsonObject) -> JsonObject:
    error_context = _json_object_field(previous, "error_context")
    if error_context:
        return error_context
    status = _record_observed_text(previous, "status")
    blocked_reason = _record_observed_text(previous, "blocked_reason")
    next_action = _record_observed_text(previous, "next_action")
    if status not in {"blocked", "failed", "error"} or not blocked_reason or not next_action:
        return {}
    required_inputs = [str(item) for item in _json_list_field(previous, "required_inputs") if str(item).strip()]
    affected = ", ".join(required_inputs[:5]) or _json_text(previous, "workflow") or "workflow"
    return _normalized_error_context(
        {
            "phase": _record_observed_text(previous, "phase") or "workflow",
            "blocked_reason": blocked_reason,
            "root_cause": f"Workflow reportou bloqueio acionavel: {blocked_reason}.",
            "affected_artifact": affected,
            "error_summary": f"Workflow terminou como {status} em {blocked_reason}.",
            "suggested_fix": next_action,
            "next_action": next_action,
            "retry_scope": "resolve_required_inputs_then_retry",
            "missing_inputs": required_inputs,
            "human_decision_required": _json_bool_field(previous, "human_decision_required"),
        }
    )


def _inherit_agent_feedback_payload(
    payload: dict[str, Any],
    *,
    workflow: str,
    root: str | Path | None,
    source: str,
    started: float,
    command: str,
) -> dict[str, Any]:
    if source != "agent" or not _agent_feedback_payload_needs_inheritance(payload, command=command):
        return payload
    previous = _recent_inheritable_workflow_record(workflow=workflow, root=root, started=started)
    if not previous:
        return payload
    inherited = dict(payload)
    diagnostic = dict(inherited.get("diagnostic_context") or {}) if isinstance(inherited.get("diagnostic_context"), dict) else {}
    previous_error_context = _inherited_workflow_error_context(previous)
    observed: JsonObject = {
        "status": _record_observed_text(previous, "status"),
        "phase": _record_observed_text(previous, "phase"),
        "blocked_reason": _record_observed_text(previous, "blocked_reason"),
        "next_action": _record_observed_text(previous, "next_action"),
    }
    inherited_feedback_context: JsonObject = {
        "run_id": str(previous.get("run_id") or ""),
        "source": str(previous.get("source") or ""),
        "command": str(previous.get("command") or ""),
        "observed": {key: value for key, value in observed.items() if value},
    }
    if previous_error_context:
        inherited_feedback_context["error_context"] = previous_error_context
    diagnostic["inherited_feedback_context"] = inherited_feedback_context
    inherited["diagnostic_context"] = diagnostic
    return inherited


def _default_contract_next_action(*, workflow: str, command: str) -> str:
    return _shared_default_contract_next_action(workflow=workflow, command=command)


def _needs_next_action_hardening(payload: JsonObject) -> bool:
    return _shared_needs_next_action_hardening(payload)


def _harden_payload_missing_next_action(
    payload: JsonObject,
    *,
    workflow: str,
    command: str,
) -> JsonObject:
    if not _needs_next_action_hardening(payload):
        return dict(payload)
    hardened = dict(payload)
    summary = summarize_payload(hardened)
    phase = _json_text(hardened, "phase") or str(summary.get("phase") or command or "unknown")
    original_blocked_reason = _json_text(hardened, "blocked_reason") or _json_text(summary, "blocked_reason")
    next_action = _default_contract_next_action(workflow=workflow, command=command)
    hardened = {
        **hardened,
        "status": "blocked",
        "blocked_reason": CONTRACT_GAP_MISSING_NEXT_ACTION,
        "next_action": next_action,
    }
    if "required_inputs" not in hardened or not isinstance(_json_value(hardened, "required_inputs"), list):
        hardened["required_inputs"] = []
    hardened.setdefault("human_decision_required", False)

    diagnostic: JsonObject = dict(_json_object_field(hardened, "diagnostic_context"))
    diagnostic["root_cause_code"] = CONTRACT_GAP_MISSING_NEXT_ACTION
    diagnostic["contract_gap"] = {
        "missing_fields": ["next_action"],
        "original_blocked_reason": original_blocked_reason,
        "workflow": workflow,
        "command": command,
    }
    hardened["diagnostic_context"] = diagnostic

    if not _json_object_field(hardened, "error_context"):
        hardened["error_context"] = {
            "phase": phase,
            "blocked_reason": CONTRACT_GAP_MISSING_NEXT_ACTION,
            "root_cause": CONTRACT_GAP_MISSING_NEXT_ACTION,
            "affected_artifact": phase,
            "error_summary": _json_text(hardened, "error")
            or _json_text(hardened, "message")
            or CONTRACT_GAP_MISSING_NEXT_ACTION,
            "suggested_fix": next_action,
            "next_action": next_action,
            "retry_scope": "restore_official_workflow_route",
            "missing_inputs": ["next_action"],
            "human_decision_required": _json_bool_field(hardened, "human_decision_required"),
        }
    return hardened


def _status_from_payload(payload: JsonObject) -> str:
    if _json_bool_field(payload, "blocked"):
        return "blocked"
    if _safe_int(_json_value(payload, "error_count")) > 0:
        return "failed"
    ok_value = _json_value(payload, "ok")
    if ok_value is False or _json_value(payload, "error") or _json_value(payload, "parse_error"):
        return "failed"
    if _json_value(payload, "warnings") or _json_value(payload, "warning_count"):
        return "completed_with_warnings"
    return "completed"


def _blocked_reason_from_payload(payload: JsonObject) -> str:
    if _json_value(payload, "blocker_count"):
        return "graph_blockers"
    if _safe_int(_json_value(payload, "error_count")) > 0:
        return "validation_errors"
    if _json_bool_field(payload, "blocked"):
        return "blocked"
    if _json_value(payload, "parse_error") or _json_value(payload, "error"):
        return "runtime_error"
    model_validation = _json_object_field(payload, "model_validation")
    if _json_value(model_validation, "ok") is False:
        return "anki_model_validation_failed"
    return ""


def _collect_counts(value: object, counts: dict[str, int | float], *, prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            leaf = str(key)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if leaf in COUNT_KEYS or leaf.endswith(("_count", "_planned")):
                    counts[name] = item
            elif isinstance(item, dict):
                _collect_counts(item, counts, prefix=name)
    elif isinstance(value, list):
        for item in value[:20]:
            if isinstance(item, dict):
                _collect_counts(item, counts, prefix=prefix)


def _collect_messages(value: Any, *, warnings: list[str], errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"warning", "warnings"}:
                _append_messages(item, warnings)
            elif lower in {"error", "errors", "write_errors", "anki_errors"}:
                _append_messages(item, errors)
            elif isinstance(item, (dict, list)):
                _collect_messages(item, warnings=warnings, errors=errors)
    elif isinstance(value, list):
        for item in value[:30]:
            _collect_messages(item, warnings=warnings, errors=errors)


def _append_messages(value: Any, target: list[str]) -> None:
    if isinstance(value, str):
        target.append(redact_snippet(value))
    elif isinstance(value, list):
        for item in value[:10]:
            _append_messages(item, target)
    elif isinstance(value, dict):
        message = value.get("message") or value.get("error") or value.get("reason") or value.get("code")
        if message:
            target.append(redact_snippet(message))


def _collect_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if len(paths) >= MAX_RELEVANT_PATHS:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for item in value[:20]:
                visit(item, key)
        elif isinstance(value, str) and _looks_like_path_key(key) and _looks_like_path_value(value):
            paths.append(value)

    visit(payload)
    deduped = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped[:MAX_RELEVANT_PATHS]


def _looks_like_path_key(key: str) -> bool:
    lower = key.lower()
    return any(hint in lower for hint in PATH_KEY_HINTS)


def _looks_like_path_value(value: str) -> bool:
    if value.startswith(("obsidian://", "http://", "https://")):
        return False
    return "/" in value or "\\" in value or value.endswith((".md", ".json", ".toml", ".html"))


def _hash_paths(paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in paths[:MAX_RELEVANT_PATHS]:
        path = Path(os.path.expandvars(raw)).expanduser()
        try:
            if not path.is_file() or path.stat().st_size > MAX_PATH_HASH_BYTES:
                continue
            hashes[raw] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def _collect_title_fields(payload: dict[str, Any]) -> dict[str, str]:
    titles: dict[str, str] = {}

    def visit(value: Any, prefix: str = "") -> None:
        if len(titles) >= 80:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                key = str(child_key)
                name = f"{prefix}.{key}" if prefix else key
                if isinstance(child_value, str) and _looks_like_title_key(key):
                    clean = redact_snippet(child_value, max_chars=240)
                    if clean:
                        titles[name] = clean
                if isinstance(child_value, (dict, list)):
                    visit(child_value, name)
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                if isinstance(item, (dict, list)):
                    visit(item, f"{prefix}.{index}" if prefix else str(index))

    visit(payload)
    return titles


def _looks_like_title_key(key: str) -> bool:
    lower = key.lower()
    return any(hint in lower for hint in TITLE_KEY_HINTS)


def _collect_artifact_state(payload: dict[str, Any]) -> dict[str, str]:
    state: dict[str, str] = {}

    def visit(value: Any, prefix: str = "") -> None:
        if len(state) >= 80:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                key = str(child_key)
                name = f"{prefix}.{key}" if prefix else key
                is_hash_key = _looks_like_hash_key(key)
                if key in ARTIFACT_STATE_KEYS or is_hash_key:
                    clean = str(child_value or "").strip()
                    if clean and len(clean) <= 160:
                        state[name] = clean if is_hash_key else redact_snippet(clean, max_chars=160)
                if isinstance(child_value, (dict, list)):
                    visit(child_value, name)
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                if isinstance(item, (dict, list)):
                    visit(item, f"{prefix}.{index}" if prefix else str(index))

    visit(payload)
    return state


def _looks_like_hash_key(key: str) -> bool:
    lower = key.lower()
    return any(hint in lower for hint in HASH_KEY_HINTS)


def _signals_from_payload(payload: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    signals: set[str] = set()
    blocked_reason = str(summary.get("blocked_reason") or "")
    status = str(summary.get("status") or "")
    search_text = " ".join(
        [
            blocked_reason,
            str(summary.get("next_action") or ""),
            " ".join(str(item) for item in summary.get("errors", [])),
            " ".join(str(item) for item in summary.get("warnings", [])),
        ]
    ).lower()
    if blocked_reason:
        signals.add(f"blocked:{blocked_reason}")
    raw_blocked_items = payload.get("blocked_items")
    if isinstance(raw_blocked_items, list):
        for item in raw_blocked_items[:20]:
            if isinstance(item, dict):
                code = _code_slug(item.get("blocked_reason") or "")
                if code:
                    signals.add(f"blocked:{code}")
    if "coverage_path" in search_text and status in {"blocked", "failed"}:
        signals.add("blocked:coverage_path_missing")
        signals.add("required_input:coverage_path")
    if summary.get("human_decision_required"):
        signals.add("human_decision_required")
    if status in {"blocked", "failed"}:
        for item in summary.get("required_inputs", []):
            signals.add(f"required_input:{item}")
    if status in {"blocked", "failed", "completed_with_warnings"} and not summary.get("next_action"):
        signals.add("missing_next_action")
    if summary.get("warnings"):
        signals.add("warnings")
    if summary.get("errors"):
        signals.add("errors")
    model_validation = payload.get("model_validation")
    if isinstance(model_validation, dict) and model_validation.get("ok") is False:
        signals.add("anki_model_validation_failed")
    if payload.get("requires_reprocess_confirmation"):
        signals.add("flashcards_reprocess_confirmation")
    if payload.get("dry_run") is True:
        signals.add("dry_run")
    if int(summary.get("counts", {}).get("blocker_count", 0) or 0):
        signals.add("blocked:graph_blockers")
    for event in _normalized_agent_events(payload):
        event_type = event.get("type")
        if event_type:
            signals.add(f"agent.{event_type}")
    if _environment_blocker_context(payload, summary):
        signals.add(ENVIRONMENT_BLOCKER_CODE)
    return sorted(signals)


def build_diagnostic_context(payload: Any, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a compact, redacted explanation of what likely needs attention."""
    if not isinstance(payload, dict):
        payload = {}
    summary = summary or summarize_payload(payload)
    decision_context = _decision_context(payload, summary)
    blocker_context = _blocker_context(payload, summary)
    agent_behavior_context = _agent_behavior_context(payload)
    environment_blocker_context = _environment_blocker_context(payload, summary)
    error_ctx = _normalized_error_context(payload.get("error_context"))
    if environment_blocker_context and not error_ctx:
        error_ctx = _environment_error_context(payload, summary, environment_blocker_context)
    retry_governance = _retry_governance_context(payload, summary, error_ctx)
    missing_inputs = _missing_inputs(payload, summary)
    contract_gaps = _contract_gaps(summary, decision_context)
    root_cause_code, root_cause_label = _root_cause(
        payload,
        summary,
        decision_context=decision_context,
        blocker_context=blocker_context,
        environment_blocker_context=environment_blocker_context,
        missing_inputs=missing_inputs,
        contract_gaps=contract_gaps,
    )
    recovery_command = _recovery_command(payload, summary, root_cause_code, decision_context, blocker_context)
    context = {
        "root_cause_code": root_cause_code,
        "root_cause_label": root_cause_label,
        "recovery_command": recovery_command,
        "missing_inputs": missing_inputs,
        "decision_context": decision_context,
        "blocker_context": blocker_context,
        "environment_blocker_context": environment_blocker_context,
        "agent_behavior_context": agent_behavior_context,
        "retry_governance": retry_governance,
        "contract_gaps": contract_gaps,
    }
    if error_ctx:
        context["error_context"] = error_ctx
    return context


def _normalized_public_report(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return redact_snippet(value)
    if isinstance(value, str):
        return redact_snippet(value, max_chars=700)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_normalized_public_report(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            clean[str(key)] = _normalized_public_report(item, depth=depth + 1)
        return clean
    return redact_snippet(value)


def _materialized_agent_directive(payload: dict[str, Any]) -> dict[str, Any]:
    directive = payload.get("agent_directive")
    if not isinstance(directive, dict):
        return {}
    normalized = _normalized_public_report(directive)
    return normalized if isinstance(normalized, dict) else {}


def _normalized_error_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    required = (
        "phase",
        "blocked_reason",
        "root_cause",
        "affected_artifact",
        "error_summary",
        "suggested_fix",
        "next_action",
        "retry_scope",
    )
    context: dict[str, Any] = {}
    for key in required:
        text = str(value.get(key) or "").strip()
        if text:
            context[key] = (
                _redact_operational_identifier(text, max_chars=120)
                if key in {"phase", "retry_scope"}
                else redact_snippet(text, max_chars=500)
            )
    for key in ("affected_items", "missing_inputs"):
        items = value.get(key)
        if isinstance(items, list):
            clean = [redact_snippet(item, max_chars=160) for item in items if str(item).strip()]
            if clean:
                context[key] = clean[:20]
    for key in ("max_attempts", "attempt_index"):
        if key in value:
            try:
                context[key] = int(value[key])
            except (TypeError, ValueError):
                pass
    if "human_decision_required" in value:
        context["human_decision_required"] = bool(value.get("human_decision_required"))
    if not all(key in context for key in required):
        return {}
    return context


def _prompt_hardening_context(
    payload: dict[str, Any],
    summary: dict[str, Any],
    *,
    workflow: str,
    command: str,
) -> dict[str, Any]:
    error_context = _normalized_error_context(payload.get("error_context"))
    evidence_field_candidates = (
        "diagnosis_path",
        "diagnosis",
        "db_path",
        "manifest_path",
        "manifest",
        "plan_path",
        "receipt_path",
        "receipt",
        "dry_run_receipt_path",
        "dry_run",
        "output_path",
    )
    evidence_fields = [
        field
        for field in evidence_field_candidates
        if payload.get(field) not in (None, "", [], {})
    ]
    field_values = {
        "app_version": payload.get("app_version") or payload.get("version"),
        "workflow": payload.get("workflow") or workflow,
        "phase": summary.get("phase") or payload.get("phase"),
        "command": payload.get("command") or command,
        "blocked_reason": summary.get("blocked_reason") or payload.get("blocked_reason"),
        "next_action": summary.get("next_action") or payload.get("next_action"),
        "error_context": error_context,
        "operational_evidence": evidence_fields,
    }
    required_fields = [
        "app_version",
        "workflow",
        "phase",
        "command",
        "blocked_reason",
        "next_action",
        "error_context",
        "operational_evidence",
    ]
    present_fields = [field for field in required_fields if bool(field_values.get(field))]
    missing_fields = [field for field in required_fields if field not in present_fields]
    quality_flags = []
    if missing_fields:
        quality_flags.append("prompt_context_incomplete")
    if "error_context" in missing_fields and str(summary.get("status") or "") in {"blocked", "failed", "error"}:
        quality_flags.append("missing_error_context")
    if "operational_evidence" in missing_fields:
        quality_flags.append("missing_operational_evidence")
    return {
        "status": "complete" if not missing_fields else "incomplete",
        "required_fields": required_fields,
        "present_fields": present_fields,
        "missing_fields": missing_fields,
        "evidence_fields": evidence_fields,
        "quality_flags": quality_flags,
    }


def _inherited_feedback_summary_context(payload: dict[str, Any], *, workflow: str, command: str) -> dict[str, Any]:
    diagnostic = payload.get("diagnostic_context") if isinstance(payload.get("diagnostic_context"), dict) else {}
    inherited = diagnostic.get("inherited_feedback_context") if isinstance(diagnostic, dict) else {}
    fields = [
        "workflow",
        "phase",
        "command",
        "blocked_reason",
        "next_action",
        "error_context",
        "operational_evidence",
    ]
    present = [
        field
        for field, value in {
            "workflow": workflow,
            "phase": payload.get("phase"),
            "command": command,
            "blocked_reason": payload.get("blocked_reason"),
            "next_action": payload.get("next_action"),
            "error_context": payload.get("error_context"),
            "operational_evidence": inherited,
        }.items()
        if value not in (None, "", [], {})
    ]
    return {
        "status": "inherited_summary",
        "required_fields": fields,
        "present_fields": present,
        "missing_fields": [],
        "evidence_fields": ["inherited_feedback_context"],
        "quality_flags": [],
    }


def _retry_governance_context(
    payload: dict[str, Any],
    summary: dict[str, Any],
    error_context: dict[str, Any],
) -> dict[str, Any]:
    category = _retry_category(payload, summary, error_context)
    budget = RETRY_BUDGETS.get(category, {})
    return {
        "category": category,
        "max_attempts": int(budget.get("max_attempts", 1)),
        "rule": str(budget.get("rule") or "Retry deve seguir next_action e mudar input relevante antes de repetir."),
        "requires_input_change": category in {"dry_run", "coverage_stage", "triage_correction"},
    }


def _retry_category(payload: dict[str, Any], summary: dict[str, Any], error_context: dict[str, Any]) -> str:
    phase = _code_slug(summary.get("phase") or payload.get("phase") or "")
    retry_scope = _code_slug(error_context.get("retry_scope") or payload.get("retry_scope") or "")
    blocked_reason = _code_slug(summary.get("blocked_reason") or "")
    if "rollback" in phase or "rollback" in retry_scope:
        return "publish_rollback"
    if payload.get("dry_run") is True or "dry_run" in phase or "dry_run" in retry_scope:
        return "dry_run"
    if "rewrite" in phase or "rewrite" in retry_scope or "fix_note" in phase:
        return "rewrite"
    if "triage" in phase or "note_plan" in retry_scope or blocked_reason == "note_plan_invalid":
        return "triage_correction"
    if "stage" in phase or "coverage" in phase or "coverage" in retry_scope or blocked_reason in {
        "coverage_invalid",
        "coverage_path_missing",
        "provenance_gap",
    }:
        return "coverage_stage"
    return "generic"


def _environment_blocker_context(payload: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    codes: list[str] = []
    samples: list[str] = []
    status = str(summary.get("status") or payload.get("status") or "").lower()
    blocked_reason = _code_slug(summary.get("blocked_reason") or payload.get("blocked_reason") or "")
    problem_status = status in {"blocked", "failed", "error"}
    explicit_environment = False

    preflight = payload.get("environment_preflight")
    if isinstance(preflight, dict):
        preflight_blockers = [
            _code_slug(item)
            for item in preflight.get("blockers", [])
            if str(item).strip()
        ]
        if preflight_blockers or _code_slug(preflight.get("blocked_reason") or "") == _code_slug(ENVIRONMENT_BLOCKER_CODE):
            codes.extend(preflight_blockers or [ENVIRONMENT_BLOCKER_CODE])
            samples.extend(_environment_preflight_samples(preflight))
            problem_status = True
            explicit_environment = True

    if blocked_reason in {
        _code_slug(ENVIRONMENT_BLOCKER_CODE),
        "windows_path_or_venv",
        "uv_unavailable",
        "python_environment",
    }:
        codes.append(blocked_reason)
        problem_status = True
        explicit_environment = True

    command_failed = _payload_has_failed_command(payload)
    if explicit_environment or command_failed:
        for text in _environment_text_candidates(payload, summary):
            matched = False
            for pattern, code in ENVIRONMENT_PATTERN_CODES:
                if pattern.search(text):
                    codes.append(code)
                    matched = True
            if matched and len(samples) < MAX_DIAGNOSTIC_ITEMS:
                samples.append(redact_snippet(text, max_chars=260))

    codes = _dedupe(_code_slug(code) for code in codes if code)
    if not explicit_environment and not any(code in STRONG_ENVIRONMENT_CODES for code in codes):
        return {}
    if not codes:
        return {}

    next_action = _environment_recovery_action(codes, payload, summary)
    severity = "high" if problem_status or command_failed else "medium"
    return {
        "code": ENVIRONMENT_BLOCKER_CODE,
        "kind": "windows_path_or_venv",
        "severity": severity,
        "codes": codes[:8],
        "samples": _dedupe(samples)[:MAX_DIAGNOSTIC_ITEMS],
        "setup_command": "/mednotes:setup",
        "reset_command": (
            "scripts\\bootstrap_windows_python_uv.ps1; fallback scripts\\reset_windows_python_uv.ps1 -FullReset"
        ),
        "next_action": next_action,
    }


def _environment_preflight_samples(preflight: dict[str, Any]) -> list[str]:
    samples: list[str] = []
    for key in ("next_action", "setup_command", "reset_command"):
        value = str(preflight.get(key) or "").strip()
        if value:
            samples.append(redact_snippet(value, max_chars=260))
    checks = preflight.get("checks")
    if isinstance(checks, list):
        for check in checks[:20]:
            if not isinstance(check, dict) or check.get("ok") is not False:
                continue
            name = str(check.get("name") or "")
            detail = str(check.get("detail") or "")
            samples.append(redact_snippet(f"{name}: {detail}", max_chars=260))
            if len(samples) >= MAX_DIAGNOSTIC_ITEMS:
                break
    return samples


def _payload_has_failed_command(payload: dict[str, Any]) -> bool:
    events = payload.get("command_events")
    if not isinstance(events, list):
        return False
    for event in events[:MAX_COMMAND_EVENTS]:
        if not isinstance(event, dict):
            continue
        status = _code_slug(event.get("status") or "")
        exit_code = event.get("exit_code")
        if status in {"failed", "error"} or (isinstance(exit_code, int) and exit_code != 0) or event.get("error"):
            return True
    return False


def _environment_text_candidates(payload: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(
        str(item or "")
        for item in (
            summary.get("blocked_reason"),
            summary.get("next_action"),
            " ".join(str(item) for item in summary.get("errors", [])),
            " ".join(str(item) for item in summary.get("warnings", [])),
        )
    )

    interesting_keys = {
        "blocked_reason",
        "next_action",
        "error",
        "errors",
        "warning",
        "warnings",
        "message",
        "detail",
        "command",
        "stdout",
        "stderr",
        "output",
        "stdout_tail",
        "stderr_tail",
        "output_tail",
        "path",
        "python",
        "uv_path",
        "persistent_venv",
        "platform",
    }

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 4 or len(values) >= 80:
            return
        if isinstance(value, dict):
            for child_key, child_value in list(value.items())[:40]:
                visit(child_value, str(child_key), depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                visit(item, key, depth + 1)
        elif isinstance(value, str):
            lower = key.lower()
            if lower in LONG_TEXT_KEYS:
                return
            if lower in interesting_keys or any(token in lower for token in ("command", "error", "path", "venv")):
                values.append(value)

    visit(payload)
    return _dedupe(text for text in values if str(text).strip())[:80]


def _environment_recovery_action(
    codes: list[str],
    payload: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    preflight = payload.get("environment_preflight")
    if isinstance(preflight, dict) and preflight.get("next_action"):
        return redact_snippet(preflight["next_action"], max_chars=300)
    value = str(summary.get("next_action") or payload.get("next_action") or "").strip()
    if value and any(token in _code_slug(value) for token in ("setup", "bootstrap", "reset_windows", "uv_project_environment")):
        return redact_snippet(value, max_chars=300)
    windowsish = any(code.startswith(("windows", "powershell", "crlf")) for code in codes)
    if windowsish:
        return (
            "Rodar /mednotes:setup. Se persistir no Windows, executar "
            "scripts\\bootstrap_windows_python_uv.ps1; como fallback, "
            "scripts\\reset_windows_python_uv.ps1 -FullReset. Nao editar scripts/runbooks como workaround."
        )
    return (
        "Rodar /mednotes:setup, configurar UV_PROJECT_ENVIRONMENT para a venv persistente, "
        "executar uv sync e repetir o workflow sem editar scripts/runbooks como workaround."
    )


def _environment_error_context(
    payload: dict[str, Any],
    summary: dict[str, Any],
    environment_blocker_context: dict[str, Any],
) -> dict[str, Any]:
    codes = environment_blocker_context.get("codes") if isinstance(environment_blocker_context.get("codes"), list) else []
    samples = environment_blocker_context.get("samples") if isinstance(environment_blocker_context.get("samples"), list) else []
    error_summary = "Ambiente/path/venv bloqueou a execucao."
    if codes:
        error_summary = f"Ambiente/path/venv bloqueou a execucao: {', '.join(str(code) for code in codes[:5])}."
    if samples:
        error_summary += f" Amostra: {samples[0]}"
    context = {
        "phase": summary.get("phase") or payload.get("phase") or "environment",
        "blocked_reason": ENVIRONMENT_BLOCKER_CODE,
        "root_cause": "Preflight ou console indicou problema de Python, uv, venv persistente, PowerShell ou path Windows.",
        "affected_artifact": ", ".join(str(code) for code in codes[:5]) or "python/uv/persistent_venv/path",
        "error_summary": error_summary,
        "suggested_fix": "Corrigir setup/venv/path pelo comando oficial; nao reescrever scripts, prompts ou runbooks para contornar ambiente.",
        "next_action": environment_blocker_context.get("next_action") or _environment_recovery_action(codes, payload, summary),
        "retry_scope": "setup_reset_then_retry",
        "missing_inputs": ["python", "uv", "persistent_venv", "wiki_dir"],
        "human_decision_required": False,
    }
    return _normalized_error_context(context)


def _agent_behavior_context(payload: dict[str, Any]) -> dict[str, Any]:
    events = _normalized_agent_events(payload)
    type_counts = Counter(str(event.get("type") or "unknown") for event in events)
    severity_counts = Counter(str(event.get("severity") or "low") for event in events)
    codes = _dedupe(str(event.get("code") or "") for event in events if event.get("code"))
    return {
        "event_count": len(events),
        "types": dict(type_counts),
        "severities": dict(severity_counts),
        "highest_severity": _highest_agent_event_severity(events),
        "codes": codes[:8],
        "samples": events[:MAX_AGENT_EVENT_SAMPLES],
    }


def _normalized_agent_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = payload.get("agent_events")
    if not isinstance(raw_events, list):
        return []
    events: list[dict[str, Any]] = []
    for item in raw_events[:MAX_AGENT_EVENTS]:
        if not isinstance(item, dict):
            continue
        event_type = _code_slug(item.get("type") or "unknown") or "unknown"
        severity = _normalize_severity(item.get("severity"))
        event = {
            "type": event_type,
            "code": f"agent.{event_type}",
            "phase": _redact_operational_identifier(item.get("phase") or payload.get("phase") or "", max_chars=80),
            "severity": severity,
            "summary": redact_snippet(item.get("summary") or "", max_chars=220),
            "action": redact_snippet(item.get("action") or "", max_chars=220),
            "target_kind": _code_slug(item.get("target_kind") or ""),
            "result": _code_slug(item.get("result") or ""),
        }
        optional_text = {
            "expected_phase": _redact_operational_identifier(item.get("expected_phase") or "", max_chars=80),
            "actual_phase": _redact_operational_identifier(
                item.get("actual_phase") or item.get("executed_phase") or "",
                max_chars=80,
            ),
            "executed_action": redact_snippet(item.get("executed_action") or item.get("actual_action") or "", max_chars=220),
            "command_family": _code_slug(item.get("command_family") or ""),
            "blocked_reason": _code_slug(item.get("blocked_reason") or ""),
            "next_action_expected": redact_snippet(
                item.get("next_action_expected") or item.get("expected_next_action") or "",
                max_chars=220,
            ),
            "snippet": redact_snippet(item.get("snippet") or "", max_chars=220),
        }
        for key, value in optional_text.items():
            if value:
                event[key] = value
        if item.get("path"):
            event["path"] = _compact_path_label(str(item.get("path")))
        events.append(event)
    return events


def _normalize_severity(value: Any) -> str:
    severity = _code_slug(value or "low")
    if severity in {"high", "medium", "low"}:
        return severity
    return "low"


def _highest_agent_event_severity(events: list[dict[str, Any]]) -> str:
    highest = "low"
    for event in events:
        severity = str(event.get("severity") or "low")
        if _severity_rank(severity) > _severity_rank(highest):
            highest = severity
    return highest if events else ""


def _with_derived_agent_events(
    payload: dict[str, Any],
    summary: dict[str, Any],
    error_context: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    if source != "agent":
        return dict(payload)
    derived = _derived_agent_events(payload, summary, error_context)
    if not derived:
        return dict(payload)
    enriched = dict(payload)
    existing = payload.get("agent_events")
    events = list(existing) if isinstance(existing, list) else []
    existing_types = {
        _code_slug(item.get("type") or "")
        for item in events
        if isinstance(item, dict)
    }
    for event in derived:
        if _code_slug(event.get("type") or "") not in existing_types:
            events.append(event)
            existing_types.add(_code_slug(event.get("type") or ""))
    enriched["agent_events"] = events
    return enriched


def _agent_timeout_or_max_turns_detected(payload: dict[str, Any], *, blocked_reason: str) -> bool:
    candidates = [
        blocked_reason,
        payload.get("blocked_reason"),
        payload.get("error"),
        payload.get("error_summary"),
        payload.get("root_cause"),
        payload.get("stop_condition"),
        payload.get("failure_reason"),
    ]
    for value in candidates:
        slug = _code_slug(value or "")
        if "timeout" in slug or "max_turns" in slug or "turn_budget" in slug:
            return True
    metrics = payload.get("agent_metrics")
    if isinstance(metrics, dict):
        turns_used = _safe_int(metrics.get("turns_used"))
        max_turns = _safe_int(metrics.get("max_turns"))
        if max_turns and turns_used >= max_turns:
            return True
    return False


def _derived_agent_events(
    payload: dict[str, Any],
    summary: dict[str, Any],
    error_context: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    status = str(summary.get("status") or "")
    phase = str(summary.get("phase") or payload.get("phase") or "")
    blocked_reason = str(summary.get("blocked_reason") or "")
    next_action = str(summary.get("next_action") or "")
    executed_action = str(
        payload.get("executed_action")
        or payload.get("actual_action")
        or payload.get("agent_action")
        or payload.get("command")
        or ""
    )
    expected_next_action = str(payload.get("expected_next_action") or payload.get("next_action_expected") or next_action)
    expected_phase = str(payload.get("expected_phase") or payload.get("allowed_phase") or payload.get("expected_next_phase") or "")
    actual_phase = str(payload.get("actual_phase") or payload.get("executed_phase") or phase)
    timeout_or_max_turns = status in {"blocked", "failed", "error"} and _agent_timeout_or_max_turns_detected(
        payload,
        blocked_reason=blocked_reason,
    )

    if expected_phase and actual_phase and _code_slug(expected_phase) != _code_slug(actual_phase):
        events.append(
            {
                "type": "wrong_phase",
                "phase": actual_phase,
                "expected_phase": expected_phase,
                "actual_phase": actual_phase,
                "severity": "high",
                "summary": f"Agente executou fase {actual_phase} quando a fase esperada era {expected_phase}.",
                "action": "Voltar para a fase esperada antes de mutar qualquer artefato.",
                "target_kind": "workflow",
                "result": "blocked",
                "expected_next_action": expected_next_action,
                "executed_action": executed_action,
            }
        )

    if expected_next_action and executed_action and not _actions_are_compatible(expected_next_action, executed_action):
        events.append(
            {
                "type": "ignored_next_action",
                "phase": phase,
                "severity": "high" if status in {"blocked", "failed", "error"} else "medium",
                "summary": "Agente executou ação fora da rota indicada por next_action.",
                "action": "Interromper retry e seguir a next_action esperada.",
                "target_kind": "workflow",
                "result": status or "detected",
                "expected_next_action": expected_next_action,
                "executed_action": executed_action,
                "blocked_reason": blocked_reason,
            }
        )

    if timeout_or_max_turns:
        events.append(
            {
                "type": "timeout_or_max_turns",
                "phase": phase,
                "severity": "high",
                "summary": "Subagente excedeu timeout ou max_turns antes de entregar output aplicavel.",
                "action": next_action
                or "Parar retry cego, registrar blocked packet com error_context e reduzir o escopo do work item.",
                "target_kind": "subagent",
                "result": status,
                "blocked_reason": blocked_reason or "timeout_or_max_turns",
                "expected_next_action": next_action,
            }
        )

    if status in {"blocked", "failed", "error"} and blocked_reason:
        events.append(
            {
                "type": "workflow_blocked",
                "phase": phase,
                "severity": "medium" if next_action else "high",
                "summary": f"Workflow parou em {blocked_reason}.",
                "action": next_action or "Adicionar next_action/error_context antes de repetir.",
                "target_kind": "workflow",
                "result": status,
                "blocked_reason": blocked_reason,
                "expected_next_action": next_action,
            }
        )

    if timeout_or_max_turns and not isinstance(payload.get("agent_metrics"), dict):
        events.append(
            {
                "type": "missing_agent_metrics",
                "phase": phase,
                "severity": "high",
                "summary": "Subagente bloqueado por timeout/max_turns sem agent_metrics estruturado.",
                "action": "Exigir agent_metrics no blocked packet antes de reexecutar ou comparar baseline de prompt.",
                "target_kind": "subagent",
                "result": status,
                "blocked_reason": blocked_reason or "timeout_or_max_turns",
                "expected_next_action": next_action,
            }
        )

    if status in {"blocked", "failed", "error"} and not error_context:
        events.append(
            {
                "type": "missing_error_context",
                "phase": phase,
                "severity": "high",
                "summary": "Run agentico bloqueou/falhou sem error_context estruturado.",
                "action": "Registrar error_context com causa, artefato afetado, retry_scope e next_action antes de tentar novamente.",
                "target_kind": "workflow",
                "result": status,
                "blocked_reason": blocked_reason,
                "expected_next_action": next_action,
            }
        )

    if payload.get("manual_intervention") or payload.get("manual_intervention_required"):
        events.append(
            {
                "type": "manual_intervention",
                "phase": phase,
                "severity": "medium",
                "summary": "Run exigiu intervenção manual.",
                "action": str(payload.get("manual_intervention_action") or next_action or "Registrar decisão humana estruturada."),
                "target_kind": "workflow",
                "result": status or "pending",
            }
        )

    return events


def _actions_are_compatible(expected: str, executed: str) -> bool:
    expected_slug = _code_slug(expected)
    executed_slug = _code_slug(executed)
    if not expected_slug or not executed_slug:
        return True
    expected_command = _command_hint(expected_slug)
    executed_command = _command_hint(executed_slug)
    if expected_command and executed_command:
        return expected_command == executed_command
    expected_tokens = {token for token in expected_slug.split("_") if len(token) >= 4}
    executed_tokens = {token for token in executed_slug.split("_") if len(token) >= 4}
    if not expected_tokens or not executed_tokens:
        return True
    return bool(expected_tokens & executed_tokens)


def _command_hint(slug: str) -> str:
    commands = (
        "stage_note",
        "publish_batch",
        "triage",
        "plan_subagents",
        "validate_note",
        "fix_note",
        "run_linker",
        "taxonomy_resolve",
        "fix_wiki",
        "apply_style_rewrite",
        "apply_note_merge",
    )
    for command in commands:
        if command in slug:
            return command
    return ""


def _compact_path_label(value: str) -> str:
    text = redact_snippet(value, max_chars=220).replace(str(Path.home()), "~")
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    if text.startswith("~"):
        prefix = "~"
    else:
        prefix = ""
    label = "/".join(parts[-3:]) if len(parts) > 3 else "/".join(parts) or text
    return f"{prefix}/{label}" if prefix and not label.startswith("~") else label


def _decision_context(payload: JsonObject, summary: JsonObject) -> JsonObject:
    decisions: list[JsonObject] = []
    raw_packets: list[JsonObject] = []
    human_decision_packet = _json_object_field(payload, "human_decision_packet")
    if human_decision_packet:
        raw_packets.append(human_decision_packet)
    for packet in _json_list_field(payload, "human_decision_packets"):
        packet_view = _json_object_view(packet)
        if packet_view:
            raw_packets.append(packet_view)
    decision_summary = _json_object_field(payload, "decision_summary")
    if decision_summary and not bool(_json_value(summary, "human_decision_required")):
        reason_code = _json_text(decision_summary, "reason_code")
        next_action = _json_text(payload, "next_action")
        decisions.append(
            {
                "kind": _code_slug(_json_text(decision_summary, "kind")),
                "type": _code_slug(_json_text(decision_summary, "kind")),
                "question": redact_snippet(_json_text(decision_summary, "public_summary"), max_chars=240),
                "options": [],
                "next_action": redact_snippet(next_action, max_chars=300),
                "continue_after_choice": redact_snippet(next_action, max_chars=300),
                "resume_action": redact_snippet(next_action, max_chars=300),
                "reason_code": reason_code,
            }
        )
    for item in _json_list_field(payload, "blocked_items")[:MAX_DIAGNOSTIC_ITEMS]:
        item_view = _json_object_view(item)
        if not item_view:
            continue
        blocked_packet = _json_object_field(item_view, "human_decision_packet")
        if blocked_packet:
            raw_packets.append(blocked_packet)
        for packet in _json_list_field(item_view, "human_decision_packets"):
            packet_view = _json_object_view(packet)
            if packet_view:
                raw_packets.append(packet_view)
    for packet in raw_packets[:MAX_DIAGNOSTIC_ITEMS]:
        options = []
        for option in _json_list_field(packet, "options")[:5]:
            option_view = _json_object_view(option)
            if option_view:
                options.append(
                    {
                        "id": redact_snippet(_json_text(option_view, "id"), max_chars=80),
                        "label": redact_snippet(_json_text(option_view, "label"), max_chars=160),
                    }
                )
        decisions.append(
            {
                "kind": _code_slug(_json_text(packet, "kind") or _json_text(packet, "type") or "manual_review"),
                "question": redact_snippet(_json_text(packet, "question"), max_chars=240),
                "options": options,
                "next_action": redact_snippet(_json_text(packet, "next_action"), max_chars=300),
                "continue_after_choice": redact_snippet(_json_text(packet, "resume_action"), max_chars=300),
            }
        )
    kinds = [str(item.get("kind") or "manual_review") for item in decisions]
    if not kinds and bool(_json_value(summary, "human_decision_required")):
        blocked_reason = _json_text(summary, "blocked_reason") or "manual_review"
        kinds.append(_code_slug(blocked_reason))
    return {
        "types": _dedupe(kinds)[:MAX_DIAGNOSTIC_ITEMS],
        "decisions": decisions,
    }


def _blocker_context(payload: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    codes: list[str] = []
    summaries: list[dict[str, Any]] = []
    raw_summary = payload.get("blocker_summary")
    decision_summary = payload.get("decision_summary")
    if isinstance(decision_summary, dict) and decision_summary.get("reason_code"):
        codes.append(_code_slug(decision_summary.get("reason_code")))
    if isinstance(raw_summary, list):
        for item in raw_summary[:MAX_DIAGNOSTIC_ITEMS]:
            if not isinstance(item, dict):
                continue
            code = _code_slug(item.get("code") or item.get("kind") or "unknown")
            codes.append(code)
            summaries.append(
                {
                    "code": code,
                    "count": _safe_int(item.get("count")),
                    "message": redact_snippet(item.get("message") or item.get("reason") or "", max_chars=220),
                }
            )

    samples: list[Any] = []
    raw_samples = payload.get("blockers_sample")
    if isinstance(raw_samples, list):
        for item in raw_samples[:MAX_DIAGNOSTIC_ITEMS]:
            if isinstance(item, dict) and item.get("code"):
                codes.append(_code_slug(item.get("code")))
            samples.append(_compact_diagnostic_value(item))

    routes: list[dict[str, Any]] = []
    raw_blocked_items = payload.get("blocked_items")
    if isinstance(raw_blocked_items, list):
        for item in raw_blocked_items[:MAX_DIAGNOSTIC_ITEMS]:
            if not isinstance(item, dict):
                continue
            code = _code_slug(item.get("blocked_reason") or "")
            if code:
                codes.append(code)
            summaries.append(
                {
                    "code": code,
                    "count": 1,
                    "message": redact_snippet(item.get("reason") or item.get("next_action") or "", max_chars=220),
                }
            )
            if item.get("next_action"):
                routes.append(
                    {
                        "route": code,
                        "count": 1,
                        "automatic": False,
                        "reason": redact_snippet(item.get("reason") or "", max_chars=240),
                        "next_action": redact_snippet(item.get("next_action") or "", max_chars=300),
                    }
                )

    blocker_resolution = payload.get("blocker_resolution")
    if isinstance(blocker_resolution, dict):
        raw_groups = blocker_resolution.get("groups")
        if isinstance(raw_groups, list):
            for group in raw_groups[:MAX_DIAGNOSTIC_ITEMS]:
                if not isinstance(group, dict):
                    continue
                route = _code_slug(group.get("route") or "unknown")
                codes.append(route)
                for code in group.get("codes") or []:
                    codes.append(_code_slug(code))
                routes.append(
                    {
                        "route": route,
                        "count": _safe_int(group.get("count")),
                        "automatic": bool(group.get("automatic", False)),
                        "reason": redact_snippet(group.get("reason") or "", max_chars=240),
                        "next_action": redact_snippet(group.get("next_action") or "", max_chars=300),
                    }
                )

    counts: dict[str, int | float] = {}
    summary_counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    for key, value in summary_counts.items():
        leaf = str(key).split(".")[-1]
        if leaf in {"blocker_count", "graph_error_count", "error_count", "warning_count"}:
            counts[str(key)] = value

    return {
        "codes": _dedupe([code for code in codes if code and code != "unknown"])[:8],
        "counts": counts,
        "summaries": summaries,
        "samples": samples,
        "routes": routes,
    }


def _missing_inputs(payload: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in ("missing_inputs", "required_inputs_missing"):
        value = payload.get(key)
        if isinstance(value, list):
            missing.extend(str(item) for item in value if str(item).strip())
    text = " ".join(
        [
            str(summary.get("blocked_reason") or ""),
            str(summary.get("next_action") or ""),
            " ".join(str(item) for item in summary.get("errors", [])),
            " ".join(str(item) for item in summary.get("warnings", [])),
        ]
    ).lower()
    if "coverage_path" in text:
        missing.append("coverage_path")
    required_inputs = summary.get("required_inputs") if isinstance(summary.get("required_inputs"), list) else []
    if "coverage_path" in required_inputs and str(summary.get("status") or "") in {"blocked", "failed"} and "coverage" in text:
        missing.append("coverage_path")
    return _dedupe(_code_slug(item) for item in missing)


def _contract_gaps(summary: JsonObject, decision_context: JsonObject) -> list[str]:
    gaps: list[str] = []
    status = str(summary.get("status") or "")
    statuses_requiring_next_action = {
        "blocked",
        "failed",
        "completed_with_warnings",
        "preview_ready",
        "ready_to_publish",
        "published",
        "completed_with_link_blockers",
    }
    if status in statuses_requiring_next_action and not summary.get("next_action"):
        gaps.append("missing_next_action")
    if status in {"blocked", "failed"} and not summary.get("blocked_reason"):
        gaps.append("empty_blocked_reason")
    if summary.get("human_decision_required") and not decision_context.get("decisions"):
        gaps.append("missing_human_decision_packet")
    return gaps


def _root_cause(
    payload: JsonObject,
    summary: JsonObject,
    *,
    decision_context: JsonObject,
    blocker_context: JsonObject,
    environment_blocker_context: JsonObject,
    missing_inputs: list[str],
    contract_gaps: list[str],
) -> tuple[str, str]:
    blocked_reason = _code_slug(summary.get("blocked_reason") or "")
    status = str(summary.get("status") or "")
    if blocked_reason == "contract_gap_missing_next_action":
        return CONTRACT_GAP_MISSING_NEXT_ACTION, "Workflow bloqueado sem próximo passo"
    if environment_blocker_context:
        return ENVIRONMENT_BLOCKER_CODE, "Bloqueio de ambiente Windows/path/venv"
    if blocked_reason == "batch_state_mismatch":
        return "batch_state_mismatch", "Artefatos incompatíveis entre fases do processamento de chats"
    if blocked_reason == "coverage_invalid":
        return "coverage_invalid", "Coverage inválida no processamento de chats"
    if blocked_reason == "provenance_gap":
        return "provenance_gap", "Proveniência multi-fonte incompleta no processamento de chats"
    if "coverage_path" in missing_inputs or blocked_reason == "coverage_path_missing":
        return "coverage_path_missing", "Coverage ausente no processamento de chats"
    if blocked_reason == "note_plan_invalid":
        return "note_plan_invalid", "Plano de triagem inválido no processamento de chats"
    if blocked_reason == "manifest_invalid":
        return "manifest_invalid", "Manifest inválido no processamento de chats"
    if blocked_reason == "dry_run_receipt_invalid":
        return "dry_run_receipt_invalid", "Recibo de dry-run ausente ou incompatível"
    if blocked_reason == "taxonomy_resolution_required":
        return "taxonomy_resolution_required", "Taxonomia precisa de resolução antes de avançar"
    blocker_codes = set(blocker_context.get("codes") or [])
    if "canonical_merge_required" in blocker_codes:
        return "canonical_merge_required", "Merge canônico necessário antes de publicar"
    if "human_decision_required_ambiguous_canonical_target" in blocker_codes:
        return (
            "human_decision_required.ambiguous_canonical_target",
            "Decisão humana: escolher alvo canônico",
        )
    if blocked_reason == "human_decision_required" or decision_context.get("decisions"):
        kind = _first_or_default(decision_context.get("types"), "manual_review")
        code = f"human_decision_required.{_code_slug(kind)}"
        return code, _human_decision_label(kind)
    model_validation = _json_object_field(payload, "model_validation")
    if _json_value(model_validation, "ok") is False:
        return "anki_model_validation_failed", "Modelo Anki bloqueou criação de cards"
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    if blocked_reason == "graph_blockers" or int(counts.get("blocker_count", 0) or 0) or blocker_context.get("codes"):
        code = _first_or_default(blocker_context.get("codes"), "unknown")
        if code and code != "unknown":
            return f"graph_blockers.{_code_slug(code)}", _graph_blocker_label(code)
        return "graph_blockers", "Blockers de grafo recorrentes"
    if "missing_next_action" in contract_gaps:
        return "contract_gap.missing_next_action", "Workflow bloqueado sem próximo passo"
    if blocked_reason:
        return f"blocked.{blocked_reason}", f"Bloqueio recorrente: {blocked_reason}"
    if status in {"blocked", "failed", "error"}:
        return f"status.{_code_slug(status)}", f"Run terminou como {status}"
    return "no_issue_detected", "Nenhum padrão de falha detectado"


def _recovery_command(
    payload: JsonObject,
    summary: JsonObject,
    root_cause_code: str,
    decision_context: JsonObject,
    blocker_context: JsonObject,
) -> str:
    for decision in _json_list_field(decision_context, "decisions"):
        decision_view = _json_object_view(decision)
        value = _json_text(decision_view, "continue_after_choice") or _json_text(decision_view, "next_action")
        if value:
            return redact_snippet(value, max_chars=300)
    for route in _json_list_field(blocker_context, "routes"):
        route_view = _json_object_view(route)
        value = _json_text(route_view, "next_action")
        if value:
            return redact_snippet(value, max_chars=300)
    if root_cause_code == ENVIRONMENT_BLOCKER_CODE:
        context = _environment_blocker_context(payload, summary)
        if context.get("next_action"):
            return redact_snippet(context["next_action"], max_chars=300)
    value = summary.get("next_action") or payload.get("next_command") or ""
    if value:
        return redact_snippet(value, max_chars=300)
    if root_cause_code == "coverage_path_missing":
        return "Gerar coverage_path a partir do note_plan, repetir stage-note --coverage <coverage.json> e depois publish-batch --dry-run."
    if root_cause_code == "note_plan_invalid":
        return "Corrigir o note_plan conforme triage-note-plan.v2 e repetir somente triage --note-plan antes de architect/stage/publish."
    if root_cause_code == "coverage_invalid":
        return "Corrigir ou regenerar coverage a partir do note_plan e repetir stage-note --coverage antes do publish-batch --dry-run."
    if root_cause_code == "provenance_gap":
        return "Completar coverage.sources e as Fontes Consolidadas da nota canônica antes de repetir stage-note/publish-batch --dry-run."
    if root_cause_code == "batch_state_mismatch":
        return "Regenerar coverage, manifest e dry-run a partir do note_plan atual antes de avançar."
    if root_cause_code == "manifest_invalid":
        return "Regenerar manifest via stage-note --coverage e repetir publish-batch --dry-run."
    if root_cause_code == "dry_run_receipt_invalid":
        return "Rodar publish-batch --dry-run com o mesmo manifest/opções antes do publish real."
    if root_cause_code == "taxonomy_resolution_required":
        return "Resolver taxonomia com categoria existente ou decisão explícita antes de repetir a fase."
    if root_cause_code == "canonical_merge_required":
        return "Consolidar informação nova no alvo canônico, preservar referências múltiplas e validar antes de aplicar."
    if root_cause_code == "human_decision_required.ambiguous_canonical_target":
        return "Escolher explicitamente o alvo canônico, ajustar o note_plan e reexecutar plan-subagents --phase architect."
    if root_cause_code.startswith("graph_blockers"):
        return "Rodar /mednotes:fix-wiki --dry-run para obter a rota segura antes de aplicar o linker."
    return ""


def _human_decision_label(kind: str) -> str:
    labels = {
        "note_merge_required": "Decisão humana: fundir ou separar notas com identidade semântica confirmada",
        "taxonomy_review_required": "Decisão humana: revisar taxonomia",
        "io_retry": "Decisão humana: liberar arquivo e tentar novamente",
        "manual_review": "Decisão humana pendente",
    }
    return labels.get(_code_slug(kind), f"Decisão humana: {_code_slug(kind)}")


def _graph_blocker_label(code: str) -> str:
    labels = {
        "duplicate_stem": "Blocker de grafo: notas duplicadas",
        "dangling_link": "Blocker de grafo: link sem alvo",
        "self_link": "Blocker de grafo: auto-link",
        "ambiguous_link": "Blocker de grafo: link ambíguo",
        "catalog_repair": "Blocker de grafo: catálogo precisa de reparo",
        "unknown_graph_blocker": "Blocker de grafo sem reparo conhecido",
    }
    return labels.get(_code_slug(code), f"Blocker de grafo: {_code_slug(code)}")


def _compact_diagnostic_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 4:
        return "[max-depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for child_key, child_value in list(value.items())[:16]:
            lower = str(child_key).lower()
            if lower in SECRET_KEYS:
                out[str(child_key)] = "[redacted]"
            else:
                out[str(child_key)] = _compact_diagnostic_value(child_value, key=str(child_key), depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_compact_diagnostic_value(item, key=key, depth=depth + 1) for item in value[:MAX_DIAGNOSTIC_ITEMS]]
    if isinstance(value, str):
        if key.lower() in LONG_TEXT_KEYS:
            return redact_snippet(value, max_chars=160)
        if key.lower() in {"phase", "expected_phase", "actual_phase", "retry_scope"}:
            return _redact_operational_identifier(value, max_chars=120)
        if _looks_like_path_key(key) and _looks_like_path_value(value):
            return _path_label(value)
        return redact_snippet(value, max_chars=240)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_snippet(value, max_chars=120)


def _path_label(path: str) -> str:
    expanded_home = str(Path.home())
    if path.startswith(expanded_home):
        path = "~" + path[len(expanded_home):]
    p = Path(path)
    parts = p.parts
    if len(parts) >= 3:
        return "/".join(parts[-3:])
    return p.name or path


def _code_slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _dedupe(items: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_or_default(value: Any, default: str) -> str:
    if isinstance(value, list) and value:
        return str(value[0] or default)
    return default


def record_workflow_run(
    *,
    workflow: str,
    command: str | None = None,
    payload: object = None,
    exit_code: int = 0,
    started_at: float | None = None,
    duration_ms: int | None = None,
    snippets: list[object] | None = None,
    root: str | Path | None = None,
    source: str = "cli",
    extra: JsonObject | None = None,
) -> JsonObject:
    started = started_at if started_at is not None else time.time()
    if duration_ms is None:
        duration_ms = max(0, int((time.time() - started) * 1000))
    effective_command = command or command_string()
    payload_dict = _json_object_view(payload)
    payload_dict = _inherit_agent_feedback_payload(
        payload_dict,
        workflow=workflow,
        root=root,
        source=source,
        started=started,
        command=effective_command,
    )
    if isinstance(payload_dict, dict):
        initial_summary = summarize_payload(payload_dict)
        initial_error_context = _normalized_error_context(payload_dict.get("error_context"))
        initial_environment_context = _environment_blocker_context(payload_dict, initial_summary)
        if _needs_next_action_hardening(payload_dict) and initial_environment_context.get("next_action"):
            payload_dict = {
                **payload_dict,
                "status": "blocked",
                "blocked_reason": ENVIRONMENT_BLOCKER_CODE,
                "next_action": _json_text(initial_environment_context, "next_action"),
            }
            if not initial_error_context:
                payload_dict["error_context"] = _environment_error_context(
                    payload_dict,
                    summarize_payload(payload_dict),
                    initial_environment_context,
                )
        else:
            payload_dict = _harden_payload_missing_next_action(
                payload_dict,
                workflow=workflow,
                command=effective_command,
            )
    provisional_summary = summarize_payload(payload_dict)
    provisional_error_context = _normalized_error_context(payload_dict.get("error_context"))
    provisional_environment_context = _environment_blocker_context(payload_dict, provisional_summary)
    if provisional_environment_context and not provisional_error_context:
        provisional_error_context = _environment_error_context(
            payload_dict,
            provisional_summary,
            provisional_environment_context,
        )
    payload_dict = _with_derived_agent_events(
        payload_dict,
        provisional_summary,
        provisional_error_context,
        source=source,
    )
    payload_for_context = payload_dict if payload_dict else payload
    payload_summary = summarize_payload(payload_for_context)
    agent_events = _normalized_agent_events(payload_dict)
    error_context = _normalized_error_context(payload_dict.get("error_context"))
    diagnostic_context = build_diagnostic_context(payload_for_context, payload_summary)
    payload_diagnostic = payload_dict.get("diagnostic_context") if isinstance(payload_dict, dict) else {}
    if isinstance(payload_diagnostic, dict) and isinstance(payload_diagnostic.get("contract_gap"), dict):
        diagnostic_context["contract_gap"] = payload_diagnostic["contract_gap"]
    if isinstance(payload_diagnostic, dict) and isinstance(payload_diagnostic.get("inherited_feedback_context"), dict):
        diagnostic_context["inherited_feedback_context"] = payload_diagnostic["inherited_feedback_context"]
    public_report = _json_object_field(_json_object_view(payload_diagnostic), "public_report")
    if public_report:
        diagnostic_context = {
            **diagnostic_context,
            "public_report": _normalized_public_report(public_report),
        }
    if source == "agent":
        if _feedback_summary_command(effective_command) and isinstance(
            diagnostic_context.get("inherited_feedback_context"),
            dict,
        ):
            diagnostic_context["prompt_hardening_context"] = _inherited_feedback_summary_context(
                payload_dict,
                workflow=workflow,
                command=effective_command,
            )
        else:
            diagnostic_context["prompt_hardening_context"] = _prompt_hardening_context(
                payload_dict,
                payload_summary,
                workflow=workflow,
                command=effective_command,
            )
    if not error_context and isinstance(diagnostic_context.get("error_context"), dict):
        error_context = diagnostic_context["error_context"]
    environment_context = _environment_context(root=root)
    integrity = environment_context.get("extension_integrity") if isinstance(environment_context, dict) else None
    if isinstance(integrity, dict) and integrity.get("drift_detected"):
        diagnostic_context.setdefault("environment_warnings", []).append("extension_integrity_drift")
        summary = integrity.get("summary") if isinstance(integrity.get("summary"), dict) else {}
        if int(summary.get("encoding_corruption_count", 0) or 0):
            diagnostic_context.setdefault("environment_warnings", []).append("extension.prompt_encoding_corruption")
    elif isinstance(integrity, dict) and integrity.get("skipped_reason") == "integrity_check_skipped_timeout":
        diagnostic_context.setdefault("environment_warnings", []).append("integrity_check_skipped_timeout")
    if source == "agent" and isinstance(integrity, dict):
        drift_event = _append_agent_integrity_drift_event(diagnostic_context, payload_summary, integrity)
        drift_code = _json_text(drift_event, "code") if isinstance(drift_event, dict) else ""
        if drift_event and not any(_json_text(_json_object_view(event), "code") == drift_code for event in agent_events):
            agent_events.append(drift_event)
    hook_since = datetime.fromtimestamp(max(0, started - 300), UTC).isoformat()
    hook_debug = _debug_from_hook_events(
        load_hook_events(since=hook_since, root=root),
        errors=load_hook_errors(since=hook_since, root=root),
    )
    generated_scripts = _merge_generated_scripts(
        _normalized_generated_scripts(payload_dict.get("generated_scripts", []), source="payload"),
        hook_debug["generated_scripts"],
    )
    command_events = _merge_command_events(
        _normalized_command_events(payload_dict.get("command_events", []), source="payload"),
        hook_debug["command_events"],
    )
    hook_errors = _merge_hook_errors(
        _normalized_hook_errors(payload_dict.get("hook_errors", []), source="payload"),
        hook_debug["hook_errors"],
    )
    hook_failure_event = _telemetry_hook_failed_agent_event(
        hook_errors,
        workflow=workflow,
        phase=str(payload_summary.get("phase") or payload_dict.get("phase") or "telemetry_capture"),
    )
    hook_failure_code = _json_text(hook_failure_event, "code") if hook_failure_event else ""
    if hook_failure_event and not any(_json_text(_json_object_view(event), "code") == hook_failure_code for event in agent_events):
        agent_events.append(hook_failure_event)
    status = str(payload_summary.get("status") or ("completed" if exit_code == 0 else "failed"))
    if exit_code != 0 and status == "completed":
        status = "failed"
    workflow_exit_code = (
        payload_dict.get("workflow_exit_code")
        if isinstance(payload_dict.get("workflow_exit_code"), int)
        else int(exit_code)
        if _json_text(payload_dict, "schema").endswith("-fsm-result.v1") and int(exit_code) != 0
        else None
    )
    record = {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": _run_id(workflow),
        "recorded_at": now_iso(),
        "workflow": workflow,
        "source": source,
        "command": redact_snippet(effective_command, max_chars=700),
        "exit_code": int(exit_code),
        "workflow_exit_code": workflow_exit_code,
        "duration_ms": int(duration_ms),
        "status": status,
        "phase": payload_summary.get("phase") or "",
        "blocked_reason": payload_summary.get("blocked_reason") or "",
        "next_action": payload_summary.get("next_action") or "",
        "next_command": payload_dict.get("next_command") if isinstance(payload_dict, dict) else None,
        "resume_command": payload_dict.get("resume_command") if isinstance(payload_dict, dict) else None,
        "rollback_command": payload_dict.get("rollback_command") if isinstance(payload_dict, dict) else None,
        "execution_gate": payload_dict.get("execution_gate") if isinstance(payload_dict, dict) else None,
        "required_inputs": payload_summary.get("required_inputs") or [],
        "human_decision_required": bool(payload_summary.get("human_decision_required")),
        "process_chats_terminal_state": payload_summary.get("process_chats_terminal_state") or "",
        "process_chats_backlog_state": payload_summary.get("process_chats_backlog_state") or "",
        "dry_run": payload_summary.get("dry_run"),
        "apply": payload_summary.get("apply"),
        "payload_summary": payload_summary,
        "diagnostic_context": diagnostic_context,
        "environment_context": environment_context,
        "diagnostic_snippets": [
            redact_snippet(item) for item in (snippets or []) if str(item).strip()
        ][:10],
        "extra": extra or {},
    }
    if _run_record_root_truth_is_observational(
        payload_dict,
        command=effective_command,
    ):
        _move_legacy_root_truth_to_observed(record, source_payload=payload_dict)
    if agent_events:
        record["agent_events"] = agent_events[:MAX_AGENT_EVENTS]
    if error_context:
        record["error_context"] = error_context
    if isinstance(payload_dict.get("human_decision_packet"), dict):
        record["human_decision_packet"] = payload_dict["human_decision_packet"]
    if isinstance(payload_dict.get("human_decision_packets"), list):
        record["human_decision_packets"] = [
            packet for packet in payload_dict["human_decision_packets"][:MAX_DIAGNOSTIC_ITEMS]
            if isinstance(packet, dict)
        ]
    materialized_directive = _materialized_agent_directive(payload_dict)
    if materialized_directive:
        record = {**record, "agent_directive": materialized_directive}
    if generated_scripts:
        record["generated_scripts"] = generated_scripts[:MAX_GENERATED_SCRIPTS]
    if command_events:
        record["command_events"] = command_events[:MAX_COMMAND_EVENTS]
    if hook_debug["hook_event_ids"]:
        record["hook_event_ids"] = hook_debug["hook_event_ids"][:MAX_HOOK_EVENTS]
    if hook_errors:
        record["hook_errors"] = hook_errors[:MAX_HOOK_ERRORS]
    if hook_debug["hook_error_ids"]:
        record["hook_error_ids"] = hook_debug["hook_error_ids"][:MAX_HOOK_ERRORS]
    _apply_generated_script_risk_signals(record)
    _apply_process_chats_retry_loop_guard(record, root=root)
    attach_telemetry_evidence(record)
    runs_dir = feedback_root(root) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record['run_id']}.json"
    record["record_path"] = str(path)
    _atomic_write_json(path, record)
    try:
        from mednotes.platform.feedback.telemetry import safe_auto_send_record

        safe_auto_send_record(record, raw_payload=payload, root=root)
    except Exception:
        pass
    try:
        prune_local_feedback(root=root)
    except Exception:
        pass
    return record


def _run_record_root_truth_is_observational(payload: JsonObject, *, command: str) -> bool:
    """Return true when legacy workflow fields must be demoted from record root.

    FSM-first payloads and agent summary records are observations about a run.
    Keeping `status`/`phase`/`next_action` at root makes them look executable to
    hooks and reports, so those values move under `observed`/`payload_summary`.
    """

    return _json_text(payload, "schema") in FSM_FIRST_RUN_RECORD_SCHEMAS or _feedback_summary_command(command)


def _move_legacy_root_truth_to_observed(record: dict[str, Any], *, source_payload: JsonObject | None = None) -> None:
    """Demote record roots and preserve stale payload roots as legacy evidence."""

    observed: dict[str, Any] = {}
    for key in ("status", "phase", "blocked_reason", "next_action", "workflow_exit_code"):
        value = record.pop(key, None)
        if value not in (None, "", [], {}):
            observed[key] = value
    legacy_root_fields: dict[str, Any] = {}
    source = source_payload or {}
    if _json_text(source, "schema") in FSM_FIRST_RUN_RECORD_SCHEMAS:
        for key in ("status", "phase", "blocked_reason", "next_action", "workflow_exit_code"):
            value = source[key] if key in source else None
            if value not in (None, "", [], {}):
                legacy_root_fields[key] = value
    if legacy_root_fields:
        observed["legacy_root_fields"] = legacy_root_fields
    if observed:
        record["observed"] = observed


def _append_agent_integrity_drift_event(
    diagnostic_context: dict[str, Any],
    payload_summary: dict[str, Any],
    integrity: dict[str, Any],
) -> dict[str, Any] | None:
    drift_paths = _agent_relevant_integrity_paths(integrity)
    if not drift_paths:
        return None
    event = {
        "type": "script_or_prompt_drift",
        "code": "agent.script_or_prompt_drift",
        "phase": str(payload_summary.get("phase") or ""),
        "severity": "high",
        "summary": f"Instalacao com drift em {len(drift_paths)} arquivo(s) de comando, prompt, runbook ou script.",
        "action": "Rodar /mednotes:status para revisar integrity drift; reinstalar/publicar update se a mudanca nao foi intencional.",
        "target_kind": str(drift_paths[0].get("kind") or ""),
        "result": "detected",
        "path": str(drift_paths[0].get("path") or ""),
    }
    context = diagnostic_context.setdefault(
        "agent_behavior_context",
        {"event_count": 0, "types": {}, "severities": {}, "highest_severity": "", "codes": [], "samples": []},
    )
    context["event_count"] = int(context.get("event_count") or 0) + 1
    _increment_dict(context.setdefault("types", {}), event["type"])
    _increment_dict(context.setdefault("severities", {}), event["severity"])
    context["highest_severity"] = _higher_severity(str(context.get("highest_severity") or ""), event["severity"])
    codes = context.setdefault("codes", [])
    if event["code"] not in codes:
        codes.append(event["code"])
    samples = context.setdefault("samples", [])
    if len(samples) < MAX_AGENT_EVENT_SAMPLES:
        samples.append(event)
    signals = payload_summary.setdefault("signals", [])
    if "agent.script_or_prompt_drift" not in signals:
        signals.append("agent.script_or_prompt_drift")
    return event


def _agent_relevant_integrity_paths(integrity: dict[str, Any]) -> list[dict[str, Any]]:
    if not integrity.get("drift_detected"):
        return []
    items: list[dict[str, Any]] = []
    for key in ("modified_files", "missing_files", "unexpected_files"):
        for item in integrity.get(key) or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            kind = str(item.get("kind") or "")
            if _is_agent_relevant_drift(path, kind):
                items.append({"path": path, "kind": kind or "unknown", "change": key.removesuffix("_files")})
    return items


def _is_agent_relevant_drift(path: str, kind: str) -> bool:
    if path == "GEMINI.md":
        return True
    if kind in AGENT_RELEVANT_DRIFT_KINDS:
        return True
    return path.startswith(AGENT_RELEVANT_DRIFT_PREFIXES)


def _increment_dict(counts: JsonObject, key: str) -> None:
    counts[key] = int(counts.get(key) or 0) + 1


def _higher_severity(left: str, right: str) -> str:
    if not left:
        return right
    return right if _severity_rank(right) > _severity_rank(left) else left


def safe_record_workflow_run(
    *,
    workflow: str,
    command: str | None = None,
    payload: object = None,
    exit_code: int = 0,
    started_at: float | None = None,
    duration_ms: int | None = None,
    snippets: list[object] | None = None,
    root: str | Path | None = None,
    source: str = "cli",
    extra: JsonObject | None = None,
) -> JsonObject | None:
    """Fail-open wrapper around the typed feedback recorder.

    Feedback persistence must never alter public workflow exit behavior, but the
    boundary still keeps the same typed contract as `record_workflow_run`; a
    catch-all `**kwargs` here would reintroduce an untyped operational API.
    """

    try:
        return record_workflow_run(
            workflow=workflow,
            command=command,
            payload=payload,
            exit_code=exit_code,
            started_at=started_at,
            duration_ms=duration_ms,
            snippets=snippets,
            root=root,
            source=source,
            extra=extra,
        )
    except Exception:
        return None


def _apply_process_chats_retry_loop_guard(record: dict[str, Any], *, root: str | Path | None = None) -> None:
    if str(record.get("workflow") or "") != "/mednotes:process-chats":
        return
    if str(record.get("status") or "") not in {"blocked", "failed", "error"}:
        return
    grouping = _record_grouping_dimensions(record, "agent.retry_loop")
    if not grouping.get("phase") or not grouping.get("root_cause"):
        return
    if not grouping.get("input_hash"):
        return
    retry_governance = record.get("diagnostic_context", {}).get("retry_governance", {})
    if not isinstance(retry_governance, dict):
        retry_governance = {}
    max_attempts = max(1, _safe_int(retry_governance.get("max_attempts") or 1))
    previous = [
        item
        for item in load_records(since="24h", root=root)
        if _same_retry_loop_signature(grouping, _record_grouping_dimensions(item, "agent.retry_loop"))
    ]
    if len(previous) < max_attempts:
        return

    previous_next_action = str(record.get("next_action") or "")
    phase = str(grouping.get("phase") or "unknown")
    root_cause = str(grouping.get("root_cause") or "unknown")
    attempt_count = len(previous) + 1
    next_action = (
        f"Parar retries automáticos: o mesmo bloqueio em {phase} ({root_cause}) já ocorreu "
        f"{attempt_count} vez(es) sem mudança relevante. Preserve os artefatos atuais, revise o "
        "error_context e só repita depois de alterar o input indicado ou pedir decisão humana."
    )
    if previous_next_action:
        next_action += f" Última rota esperada: {previous_next_action}"

    record["next_action"] = next_action
    summary: dict[str, Any] = record.get("payload_summary") if isinstance(record.get("payload_summary"), dict) else {}
    summary["next_action"] = next_action
    signals = summary.setdefault("signals", [])
    if "agent.retry_loop" not in signals:
        signals.append("agent.retry_loop")

    error_context = record.get("error_context") if isinstance(record.get("error_context"), dict) else {}
    target_kind = str(error_context.get("affected_artifact") or "workflow")
    if error_context:
        error_context["next_action"] = next_action

    diagnostic: dict[str, Any] = (
        record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
    )
    diagnostic["recovery_command"] = next_action
    agent_context = diagnostic.setdefault(
        "agent_behavior_context",
        {"event_count": 0, "types": {}, "severities": {}, "highest_severity": "", "codes": [], "samples": []},
    )
    event = {
        "type": "retry_loop",
        "code": "agent.retry_loop",
        "phase": phase,
        "severity": "high",
        "summary": f"Mesmo bloqueio repetido {attempt_count} vez(es) sem mudança relevante.",
        "action": next_action,
        "target_kind": _code_slug(target_kind),
        "result": "blocked",
        "blocked_reason": _code_slug(record.get("blocked_reason") or root_cause),
        "next_action_expected": previous_next_action,
    }
    _append_agent_context_event(agent_context, event)
    events = record.setdefault("agent_events", [])
    if isinstance(events, list) and not any(isinstance(item, dict) and item.get("code") == "agent.retry_loop" for item in events):
        events.append(event)


def _same_retry_loop_signature(current: dict[str, str], previous: dict[str, str]) -> bool:
    for key in ("phase", "root_cause"):
        if str(current.get(key) or "") != str(previous.get(key) or ""):
            return False
    compared = False
    for key in ("target_canonical", "input_hash", "error_hash"):
        current_value = str(current.get(key) or "")
        previous_value = str(previous.get(key) or "")
        if not current_value:
            continue
        compared = True
        if current_value != previous_value:
            return False
    return compared


def _append_agent_context_event(context: dict[str, Any], event: dict[str, Any]) -> None:
    context["event_count"] = int(context.get("event_count") or 0) + 1
    _increment_dict(context.setdefault("types", {}), str(event.get("type") or "unknown"))
    severity = str(event.get("severity") or "low")
    _increment_dict(context.setdefault("severities", {}), severity)
    context["highest_severity"] = _higher_severity(str(context.get("highest_severity") or ""), severity)
    codes = context.setdefault("codes", [])
    code = str(event.get("code") or "")
    if code and code not in codes:
        codes.append(code)
    samples = context.setdefault("samples", [])
    if len(samples) < MAX_AGENT_EVENT_SAMPLES:
        samples.append(event)


def attach_telemetry_evidence(record: dict[str, Any], *, send_path: str = "workflow_record") -> dict[str, Any]:
    record["telemetry_evidence"] = build_telemetry_evidence(record, send_path=send_path)
    return record


def build_telemetry_evidence(record: dict[str, Any], *, send_path: str = "workflow_record") -> dict[str, Any]:
    extension_diffs = _record_extension_diffs(record)
    generated_scripts = record.get("generated_scripts") if isinstance(record.get("generated_scripts"), list) else []
    command_events = record.get("command_events") if isinstance(record.get("command_events"), list) else []
    hook_errors = record.get("hook_errors") if isinstance(record.get("hook_errors"), list) else []
    hook_event_ids = record.get("hook_event_ids") if isinstance(record.get("hook_event_ids"), list) else []
    hook_error_ids = record.get("hook_error_ids") if isinstance(record.get("hook_error_ids"), list) else []
    counts = {
        "extension_diff_count": len(extension_diffs),
        "generated_script_count": len(generated_scripts),
        "command_event_count": len(command_events),
        "hook_error_count": len(hook_errors),
        "hook_event_id_count": len(hook_event_ids),
        "hook_error_id_count": len(hook_error_ids),
    }
    sources = _evidence_sources(record, extension_diffs, generated_scripts, command_events, hook_errors)
    quality_flags = _evidence_quality_flags(record, extension_diffs, generated_scripts, command_events, hook_errors)
    timeline = _evidence_timeline(record, extension_diffs, generated_scripts, command_events, hook_errors)
    seed = {
        "run_id": record.get("run_id"),
        "recorded_at": record.get("recorded_at"),
        "sources": sources,
        "counts": counts,
        "timeline": timeline[:8],
    }
    return {
        "schema": TELEMETRY_EVIDENCE_SCHEMA,
        "bundle_id": f"telem-{hashlib.sha256(json.dumps(seed, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]}",
        "sources": sources,
        "artifact_counts": counts,
        "timeline": timeline[:12],
        "quality_flags": quality_flags,
        "redaction_summary": {
            "applied": True,
            "blocked_fields": ["content", "markdown", "html", "raw_chat", "note_text", ".env", "tokens", "keys"],
            "operational_debug_fields": ["extension_diffs", "generated_scripts", "command_events", "hook_errors"],
        },
        "truncation_summary": _evidence_truncation_summary(extension_diffs, generated_scripts, command_events, hook_errors),
        "send_path": send_path,
    }


def _record_extension_diffs(record: dict[str, Any]) -> list[dict[str, Any]]:
    direct = record.get("extension_diffs") if isinstance(record.get("extension_diffs"), list) else []
    if direct:
        return [item for item in direct if isinstance(item, dict)]
    environment = record.get("environment_context") if isinstance(record.get("environment_context"), dict) else {}
    integrity = environment.get("extension_integrity") if isinstance(environment.get("extension_integrity"), dict) else {}
    diffs = integrity.get("extension_diffs") if isinstance(integrity.get("extension_diffs"), list) else []
    return [item for item in diffs if isinstance(item, dict)]


def _evidence_sources(
    record: dict[str, Any],
    extension_diffs: list[dict[str, Any]],
    generated_scripts: list[Any],
    command_events: list[Any],
    hook_errors: list[Any],
) -> list[str]:
    sources: list[str] = []
    if extension_diffs:
        environment = record.get("environment_context") if isinstance(record.get("environment_context"), dict) else {}
        integrity = environment.get("extension_integrity") if isinstance(environment.get("extension_integrity"), dict) else {}
        if integrity.get("snapshot_id"):
            sources.append("pre_update_snapshot:extension_diffs")
        else:
            sources.append("workflow_record:extension_diffs")
    if generated_scripts:
        sources.append(f"{_dominant_item_source(generated_scripts, 'payload')}:generated_scripts")
    if command_events:
        sources.append(f"{_dominant_item_source(command_events, 'payload')}:command_events")
    if hook_errors:
        sources.append(f"{_dominant_item_source(hook_errors, 'hook')}:hook_errors")
    if record.get("hook_event_ids"):
        sources.append("hook:hook_event_ids")
    if record.get("hook_error_ids"):
        sources.append("hook:hook_error_ids")
    return _dedupe(sources)


def _dominant_item_source(items: list[Any], default: str) -> str:
    for item in items:
        if isinstance(item, dict) and item.get("source"):
            return str(item.get("source"))
    return default


def _evidence_quality_flags(
    record: dict[str, Any],
    extension_diffs: list[dict[str, Any]],
    generated_scripts: list[Any],
    command_events: list[Any],
    hook_errors: list[Any],
) -> list[str]:
    flags: list[str] = []
    if generated_scripts and not command_events:
        flags.append("telemetry.command_events_missing")
    if hook_errors:
        flags.append("telemetry.hook_capture_failed")
    environment = record.get("environment_context") if isinstance(record.get("environment_context"), dict) else {}
    integrity = environment.get("extension_integrity") if isinstance(environment.get("extension_integrity"), dict) else {}
    summary = integrity.get("summary") if isinstance(integrity.get("summary"), dict) else {}
    if summary.get("snapshot_changed_path_count_mismatch") or (extension_diffs and not _safe_int(summary.get("changed_count")) and integrity.get("drift_detected") is False):
        flags.append("telemetry.snapshot_counts_mismatch")
    if any(_safe_int(item.get("noise_filtered_count")) for item in extension_diffs if isinstance(item, dict)):
        flags.append("telemetry.noisy_diff_filtered")
    return _dedupe(flags)


def _telemetry_hook_failed_agent_event(
    hook_errors: list[Any],
    *,
    workflow: str,
    phase: str,
) -> dict[str, Any] | None:
    normalized = [item for item in hook_errors if isinstance(item, dict)]
    if not normalized:
        return None
    sample = normalized[0]
    return {
        "schema": "medical-notes-workbench.agent-event.v1",
        "type": "telemetry_hook_failed",
        "code": "agent.telemetry_hook_failed",
        "severity": "medium",
        "root_cause_code": "telemetry_capture_failed",
        "workflow": workflow,
        "phase": phase or "telemetry_capture",
        "summary": "Falha de hook de telemetria reduziu a evidencia capturada durante o workflow.",
        "action": "Rodar /report ou capture_extension_diff antes de continuar mutacao arriscada.",
        "target_kind": "telemetry",
        "result": "evidence_degraded",
        "recovery_command": "Run /report or capture_extension_diff before continuing risky mutation.",
        "artifact_path": str(sample.get("error_path") or ""),
        "redacted_sample": {
            "hook_error_count": len(normalized),
            "hook_event_name": str(sample.get("hook_event_name") or ""),
            "type": str(sample.get("type") or ""),
            "mode": str(sample.get("mode") or ""),
        },
        "next_action": "Run /report or capture_extension_diff before continuing risky mutation.",
    }


def _evidence_timeline(
    record: dict[str, Any],
    extension_diffs: list[dict[str, Any]],
    generated_scripts: list[Any],
    command_events: list[Any],
    hook_errors: list[Any],
) -> list[dict[str, Any]]:
    at = str(record.get("recorded_at") or now_iso())
    timeline: list[dict[str, Any]] = [
        {"at": at, "kind": "run_record", "label": str(record.get("workflow") or "unknown"), "phase": str(record.get("phase") or "")}
    ]
    for diff in extension_diffs[:4]:
        timeline.append({"at": at, "kind": "extension_diff", "label": str(diff.get("path") or ""), "change": str(diff.get("change") or "")})
    for script in generated_scripts[:4]:
        if isinstance(script, dict):
            timeline.append({"at": at, "kind": "generated_script", "label": str(script.get("path") or ""), "source": str(script.get("source") or "")})
    for event in command_events[:4]:
        if isinstance(event, dict):
            timeline.append({"at": at, "kind": "command_event", "label": str(event.get("command_family") or "shell"), "status": str(event.get("status") or "")})
    for error in hook_errors[:4]:
        if isinstance(error, dict):
            timeline.append({"at": str(error.get("recorded_at") or at), "kind": "hook_error", "label": str(error.get("type") or "hook_error")})
    return timeline


def _evidence_truncation_summary(*groups: list[Any]) -> dict[str, Any]:
    truncated = 0
    omitted = 0
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            if item.get("truncated"):
                truncated += 1
            if item.get("content_omitted_reason") or item.get("full_diff_unavailable_reason"):
                omitted += 1
    return {"truncated_artifacts": truncated, "omitted_artifacts": omitted}


def _apply_generated_script_risk_signals(record: dict[str, Any]) -> None:
    scripts = record.get("generated_scripts") if isinstance(record.get("generated_scripts"), list) else []
    if not scripts:
        return
    all_codes = {
        str(code)
        for script in scripts
        if isinstance(script, dict)
        for code in (script.get("risk_codes") or [])
        if str(code).strip()
    }
    events: list[dict[str, Any]] = []
    phase = str(record.get("phase") or "")
    first_path = next((str(script.get("path") or "") for script in scripts if isinstance(script, dict) and script.get("path")), "")
    events.append(
        {
            "type": "generated_script_workaround",
            "code": "agent.generated_script_workaround",
            "phase": phase,
            "severity": "medium",
            "summary": f"Agente criou ou editou {len(scripts)} script(s) operacional(is) durante o workflow.",
            "action": "Revisar o script gerado e transformar qualquer logica util em implementacao testada da extensao.",
            "target_kind": "script",
            "result": "detected",
            "path": first_path,
        }
    )
    if {"reads_obsidian_plugin_data", "writes_related_notes_section"} & all_codes:
        events.append(
            {
                "type": "related_notes_wrong_strategy",
                "code": "agent.related_notes_wrong_strategy",
                "phase": phase,
                "severity": "high",
                "summary": "Agente tentou reconstruir Notas Relacionadas por script improvisado em vez de usar o produto validado do plugin.",
                "action": "Reimplementar a integracao Related Notes dentro da extensao, com contrato de entrada/saida e dry-run.",
                "target_kind": "related_notes",
                "result": "detected",
                "path": first_path,
            }
        )
    if "mass_markdown_mutation" in all_codes and "no_dry_run" in all_codes:
        events.append(
            {
                "type": "mass_mutation_without_dry_run",
                "code": "agent.mass_mutation_without_dry_run",
                "phase": phase,
                "severity": "high",
                "summary": "Script gerado parece mutar muitas notas Markdown sem dry-run detectavel.",
                "action": "Bloquear aplicacao direta; exigir preview, backup e limite de escopo antes de mutar o vault.",
                "target_kind": "vault",
                "result": "detected",
                "path": first_path,
            }
        )
    if {"direct_sql_mutation", "queue_truth_bypass", "unsafe_mass_wikilink_rewrite"} & all_codes:
        root_cause = (
            "queue_truth_bypass"
            if "queue_truth_bypass" in all_codes
            else "direct_sql_mutation"
            if "direct_sql_mutation" in all_codes
            else "unsafe_mass_wikilink_rewrite"
        )
        events.append(
            {
                "type": "generated_script_risk",
                "code": "agent.unsafe_generated_script_recovery_bypass",
                "phase": phase,
                "severity": "high",
                "summary": "Script gerado tenta contornar o workflow oficial de recovery com mutação direta.",
                "action": "Descartar o workaround e usar os comandos oficiais com dry-run, plano e recibo.",
                "target_kind": "workflow_state",
                "result": "detected",
                "path": first_path,
                "root_cause_code": root_cause,
                "recovery_command": _generated_script_recovery_command(all_codes),
            }
        )
    if "extension_prompt_or_script_drift" in all_codes:
        events.append(
            {
                "type": "script_or_prompt_drift",
                "code": "agent.script_or_prompt_drift",
                "phase": phase,
                "severity": "high",
                "summary": "Script gerado toca area allowlisted da extensao.",
                "action": "Comparar o diff e decidir se vira update publicado ou rollback.",
                "target_kind": "extension",
                "result": "detected",
                "path": first_path,
            }
        )
    if not events:
        return
    diagnostic = record.setdefault("diagnostic_context", {})
    if not isinstance(diagnostic, dict):
        return
    context = diagnostic.setdefault(
        "agent_behavior_context",
        {"event_count": 0, "types": {}, "severities": {}, "highest_severity": "", "codes": [], "samples": []},
    )
    existing_events = record.setdefault("agent_events", [])
    if not isinstance(existing_events, list):
        existing_events = []
        record["agent_events"] = existing_events
    summary = record.get("payload_summary") if isinstance(record.get("payload_summary"), dict) else {}
    signals = summary.setdefault("signals", [])
    for event in events:
        event.setdefault("schema", "medical-notes-workbench.agent-event.v1")
        event.setdefault("root_cause_code", str(event.get("code") or "").replace("agent.", ""))
        event.setdefault("workflow", str(record.get("workflow") or ""))
        event.setdefault("recovery_command", _generated_script_recovery_command(all_codes))
        event.setdefault("artifact_path", first_path)
        event.setdefault("redacted_sample", {"path": first_path, "risk_codes": sorted(all_codes)[:12]})
        event.setdefault("next_action", str(event.get("action") or ""))
        if not any(isinstance(item, dict) and item.get("code") == event["code"] for item in existing_events):
            existing_events.append(event)
            _append_agent_context_event(context, event)
        if event["code"] not in signals:
            signals.append(event["code"])


def _generated_script_recovery_command(risk_codes: set[str]) -> str:
    if "queue_truth_bypass" in risk_codes or "direct_sql_mutation" in risk_codes:
        return "uv run python scripts/mednotes/wiki/cli.py vocabulary-recover --mode reconcile-queue --dry-run --json"
    if "unsafe_mass_wikilink_rewrite" in risk_codes or "mass_markdown_mutation" in risk_codes:
        return "uv run python scripts/mednotes/wiki/cli.py run-linker --diagnose --json"
    if {"reads_obsidian_plugin_data", "writes_related_notes_section"} & risk_codes:
        return "uv run python scripts/mednotes/wiki/cli.py related-notes-sync --recover-export --mode auto --json"
    return "uv run python scripts/mednotes/wiki/cli.py environment-preflight --json"


def load_hook_events(*, since: str = "2h", root: str | Path | None = None, limit: int = MAX_HOOK_EVENTS) -> list[dict[str, Any]]:
    cutoff = _parse_since(since)
    events_dir = feedback_root(root) / "hook-events"
    events: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("schema") != AGENT_HOOK_EVENT_SCHEMA:
            continue
        recorded_at = _parse_datetime(str(data.get("recorded_at") or data.get("timestamp") or ""))
        if recorded_at and recorded_at < cutoff:
            continue
        data.setdefault("event_path", str(path))
        events.append(data)
        if len(events) >= limit:
            break
    return list(reversed(events))


def load_hook_errors(*, since: str = "2h", root: str | Path | None = None, limit: int = MAX_HOOK_ERRORS) -> list[dict[str, Any]]:
    cutoff = _parse_since(since)
    errors_dir = feedback_root(root) / "hook-errors"
    errors: list[dict[str, Any]] = []
    for path in sorted(errors_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("schema") != AGENT_HOOK_ERROR_SCHEMA:
            continue
        recorded_at = _parse_datetime(str(data.get("recorded_at") or data.get("timestamp") or ""))
        if recorded_at and recorded_at < cutoff:
            continue
        data.setdefault("error_path", str(path))
        errors.append(data)
        if len(errors) >= limit:
            break
    return list(reversed(errors))


def hook_debug_record(
    *,
    events: list[dict[str, Any]],
    errors: list[dict[str, Any]] | None = None,
    since: str = "2h",
) -> dict[str, Any] | None:
    debug = _debug_from_hook_events(events, errors=errors or [])
    if not debug["generated_scripts"] and not debug["command_events"] and not debug["hook_errors"]:
        return None
    failed_commands = any(_command_event_failed(event) for event in debug["command_events"])
    hook_error_detected = bool(debug["hook_errors"])
    workflow_hints = _workflow_hints_from_command_events(debug["command_events"])
    digest = hashlib.sha256(
        json.dumps(
            {"hook_event_ids": debug["hook_event_ids"], "hook_error_ids": debug["hook_error_ids"]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    record = {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": f"hook-events-{digest}",
        "recorded_at": now_iso(),
        "workflow": "/mednotes:agent-session",
        "source": "agent",
        "command": "Gemini CLI hooks",
        "exit_code": 0,
        "duration_ms": 0,
        "status": "completed_with_warnings",
        "phase": "hook-events",
        "workflow_hints": workflow_hints,
        "blocked_reason": "",
        "next_action": "Revisar scripts gerados, erros de console e falhas internas dos hooks capturados pela telemetria.",
        "required_inputs": [],
        "human_decision_required": False,
        "dry_run": None,
        "apply": None,
        "payload_summary": {
            "counts": {
                "generated_script_count": len(debug["generated_scripts"]),
                "command_event_count": len(debug["command_events"]),
                "hook_error_count": len(debug["hook_errors"]),
            },
            "warnings": [],
            "errors": [],
            "required_inputs": [],
            "relevant_paths": [item.get("path", "") for item in debug["generated_scripts"] if item.get("path")],
            "path_hashes": {},
            "signals": _dedupe((["agent.command_failed"] if failed_commands else []) + (["telemetry.hook_error"] if hook_error_detected else [])),
            "status": "completed_with_warnings",
            "phase": "hook-events",
            "workflow_hints": workflow_hints,
        },
        "diagnostic_context": {
            "root_cause_code": "agent.hook_debug",
            "root_cause_label": "Eventos tecnicos capturados por hooks",
            "recovery_command": "Revisar os scripts gerados e erros de console no email de telemetria.",
            "missing_inputs": [],
            "decision_context": {"types": [], "decisions": []},
            "blocker_context": {"codes": [], "counts": {}, "summaries": [], "samples": [], "routes": []},
            "contract_gaps": [],
        },
        "environment_context": {},
        "diagnostic_snippets": [],
        "generated_scripts": debug["generated_scripts"],
        "command_events": debug["command_events"],
        "hook_errors": debug["hook_errors"],
        "hook_event_ids": debug["hook_event_ids"],
        "hook_error_ids": debug["hook_error_ids"],
        "hook_debug_since": since,
    }
    hook_failure_event = _telemetry_hook_failed_agent_event(
        debug["hook_errors"],
        workflow="/mednotes:agent-session",
        phase="hook-events",
    )
    if hook_failure_event:
        record["agent_events"] = [hook_failure_event]
    _apply_generated_script_risk_signals(record)
    return attach_telemetry_evidence(record, send_path="hook_debug_record")


def _command_event_failed(event: JsonObject) -> bool:
    status = _json_text(event, "status").lower()
    exit_code = _json_value(event, "exit_code")
    return bool(status in {"failed", "error"} or (isinstance(exit_code, int) and exit_code != 0) or _json_value(event, "error"))


def _workflow_hints_from_command_events(events: list[JsonObject]) -> list[JsonObject]:
    hints: list[JsonObject] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events[:MAX_COMMAND_EVENTS]:
        event_view = _json_object_view(event)
        workflow = _json_text(event_view, "workflow")
        phase = _json_text(event_view, "phase")
        status = _json_text(event_view, "workflow_status")
        blocked_reason = _json_text(event_view, "blocked_reason")
        if not (workflow or phase or status or blocked_reason):
            continue
        key = (workflow, phase, status, blocked_reason)
        if key in seen:
            continue
        seen.add(key)
        hints.append(
            {
                "workflow": workflow,
                "phase": phase,
                "status": status,
                "blocked_reason": blocked_reason,
                "exit_code": _json_int_field(event_view, "workflow_exit_code")
                if _json_int_field(event_view, "workflow_exit_code") is not None
                else _json_int_field(event_view, "exit_code"),
            }
        )
    return hints[:8]


def _debug_from_hook_events(events: list[dict[str, Any]], *, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    generated_scripts: list[dict[str, Any]] = []
    command_events: list[dict[str, Any]] = []
    hook_errors: list[dict[str, Any]] = []
    event_ids: list[str] = []
    error_ids: list[str] = []
    for event in events[:MAX_HOOK_EVENTS]:
        event_id = str(event.get("event_id") or "")
        if event_id:
            event_ids.append(event_id)
        generated_scripts.extend(_normalized_generated_scripts(event.get("generated_scripts", []), source="hook"))
        command_events.extend(_normalized_command_events(event.get("command_events", []), source="hook"))
    for error in (errors or [])[:MAX_HOOK_ERRORS]:
        error_id = str(error.get("error_id") or "")
        if error_id:
            error_ids.append(error_id)
        hook_errors.extend(_normalized_hook_errors([error], source="hook"))
    return {
        "generated_scripts": _merge_generated_scripts(generated_scripts)[:MAX_GENERATED_SCRIPTS],
        "command_events": _merge_command_events(command_events)[:MAX_COMMAND_EVENTS],
        "hook_errors": _merge_hook_errors(hook_errors)[:MAX_HOOK_ERRORS],
        "hook_event_ids": _dedupe(event_ids)[:MAX_HOOK_EVENTS],
        "hook_error_ids": _dedupe(error_ids)[:MAX_HOOK_ERRORS],
    }


def _normalized_generated_scripts(value: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    scripts: list[dict[str, Any]] = []
    for item in value[:MAX_GENERATED_SCRIPTS]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        suffix = Path(path).suffix.lower()
        if suffix not in SCRIPT_SUFFIXES:
            continue
        content = str(item.get("content") or "")
        script: dict[str, Any] = {
            "path": _compact_path_label(path),
            "language": _language_for_suffix(suffix),
            "sha256": str(item.get("sha256") or _hash_text(content)),
            "size_bytes": _safe_int(item.get("size_bytes") if item.get("size_bytes") is not None else len(content.encode("utf-8"))),
            "source": str(item.get("source") or source),
            "capture_method": str(item.get("capture_method") or ""),
        }
        if content:
            script["content"] = redact_operational_text(content, max_chars=MAX_SCRIPT_CONTENT_CHARS)
            script["truncated"] = len(str(script["content"])) < len(content)
            risk_codes = _generated_script_risk_codes(path=path, content=content)
            if risk_codes:
                script["risk_codes"] = risk_codes
        if item.get("content_omitted_reason"):
            script["content_omitted_reason"] = redact_snippet(item.get("content_omitted_reason"), max_chars=160)
        scripts.append(script)
    return scripts


def _generated_script_risk_codes(*, path: str, content: str) -> list[str]:
    text = str(content or "")
    lowered = text.lower()
    path_lower = str(path or "").replace("\\", "/").lower()
    codes: list[str] = []

    def add(code: str, condition: bool) -> None:
        if condition and code not in codes:
            codes.append(code)

    markdown_scan = (
        "rglob(\"*.md\")" in lowered
        or "rglob('*.md')" in lowered
        or "glob(\"**/*.md\")" in lowered
        or "glob('**/*.md')" in lowered
        or ("os.walk" in lowered and ".md" in lowered)
    )
    writes_files = bool(
        re.search(r"\bwrite_text\s*\(", lowered)
        or re.search(r"\bopen\s*\([^)]*['\"]w", lowered)
        or "fs.writefile" in lowered
        or "set-content" in lowered
        or "out-file" in lowered
        or ".unlink(" in lowered
        or "shutil.move" in lowered
    )
    add("mass_markdown_mutation", markdown_scan and writes_files)
    add(
        "unsafe_mass_wikilink_rewrite",
        markdown_scan and writes_files and bool(re.search(r"\[\[|wikilink|wiki\s*link", lowered)),
    )
    add(
        "direct_sql_mutation",
        bool(
            ("sqlite3.connect" in lowered or ".sqlite" in lowered or "vocabulary.sqlite" in lowered)
            and re.search(r"\b(update|insert|delete|drop|alter|replace)\b", lowered)
        ),
    )
    add(
        "queue_truth_bypass",
        "note_semantic_ingestion_queue" in lowered
        and "status" in lowered
        and bool(re.search(r"\b(applied|completed|done)\b", lowered)),
    )
    add("hardcoded_user_path", bool(re.search(r"(?i)([a-z]:\\\\|/users/|/home/|~[/\\])", text)))
    add("reads_obsidian_plugin_data", ".obsidian/plugins" in lowered or "related-notes" in lowered or "related notes" in lowered)
    add("writes_related_notes_section", "notas relacionadas" in lowered or "related notes" in lowered)
    add(
        "external_api_or_embedding_call",
        bool(
            re.search(r"\b(openai|anthropic|gemini|embedding|embeddings)\b", lowered)
            or re.search(r"\b(requests|httpx)\.(post|get|request)\s*\(", lowered)
            or re.search(r"\bfetch\s*\(", lowered)
        ),
    )
    add("no_dry_run", writes_files and "dry_run" not in lowered and "--dry-run" not in lowered and "dry-run" not in lowered)
    add("encoding_corruption", "\ufffd" in text or bool(re.search(r"(?m)^##\s+\?+\s+(notas relacionadas|fontes consolidadas|fechamento)\b", lowered)))
    add(
        "extension_prompt_or_script_drift",
        path_lower == "gemini.md"
        or path_lower.startswith(("commands/", "skills/", "docs/", "hooks/", "scripts/", "src/"))
        or "/extensions/medical-notes-workbench/" in path_lower,
    )
    return codes


def _normalized_command_events(value: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, Any]] = []
    for item in value[:MAX_COMMAND_EVENTS]:
        if not isinstance(item, dict):
            continue
        event: dict[str, Any] = {
            "command_family": _code_slug(item.get("command_family") or "shell"),
            "command": redact_operational_text(item.get("command") or "", max_chars=2000),
            "exit_code": item.get("exit_code") if isinstance(item.get("exit_code"), int) else None,
            "status": _code_slug(item.get("status") or "unknown"),
            "source": str(item.get("source") or source),
            "capture_method": str(item.get("capture_method") or ""),
        }
        for key in ("workflow", "phase", "workflow_status", "blocked_reason"):
            if item.get(key):
                event[key] = str(item.get(key)) if key == "workflow" else _code_slug(item.get(key))
        if isinstance(item.get("workflow_exit_code"), int):
            event["workflow_exit_code"] = item.get("workflow_exit_code")
        for key in ("stdout_tail", "stderr_tail", "output_tail", "error"):
            if item.get(key):
                event[key] = redact_operational_text(item.get(key), max_chars=MAX_CONSOLE_TAIL_CHARS)
        events.append(event)
    return events


def _normalized_hook_errors(value: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in value[:MAX_HOOK_ERRORS]:
        if not isinstance(item, dict):
            continue
        error: dict[str, Any] = {
            "error_id": str(item.get("error_id") or ""),
            "recorded_at": str(item.get("recorded_at") or ""),
            "type": _code_slug(item.get("type") or "hook_internal_error"),
            "severity": _code_slug(item.get("severity") or "warning"),
            "mode": _code_slug(item.get("mode") or ""),
            "hook_event_name": _code_slug(item.get("hook_event_name") or ""),
            "tool_name": _code_slug(item.get("tool_name") or ""),
            "source": str(item.get("source") or source),
        }
        for key in ("message", "stack_tail", "stdout_tail", "stderr_tail", "error"):
            if item.get(key):
                error[key] = redact_operational_text(item.get(key), max_chars=MAX_HOOK_ERROR_CHARS)
        if isinstance(item.get("exit_code"), int):
            error["exit_code"] = item.get("exit_code")
        details = item.get("details")
        if isinstance(details, dict):
            error["details"] = _redact_hook_error_details(details)
        errors.append(error)
    return errors


def _redact_hook_error_details(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[max-depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            lower = str(key).lower()
            if lower in SECRET_KEYS:
                out[str(key)] = "[redacted]"
            elif lower in LONG_TEXT_KEYS:
                out[str(key)] = f"[{lower} omitted]"
            else:
                out[str(key)] = _redact_hook_error_details(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_hook_error_details(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return redact_operational_text(value, max_chars=600)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_operational_text(str(value), max_chars=300)


def _merge_generated_scripts(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            key = (str(item.get("path") or ""), str(item.get("sha256") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _merge_command_events(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, Any]] = set()
    for group in groups:
        for item in group:
            key = (str(item.get("command") or ""), str(item.get("output_tail") or item.get("stderr_tail") or ""), item.get("exit_code"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _merge_hook_errors(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for group in groups:
        for item in group:
            key = (
                str(item.get("type") or ""),
                str(item.get("mode") or ""),
                str(item.get("tool_name") or ""),
                str(item.get("message") or item.get("error") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _language_for_suffix(suffix: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".sh": "shell",
        ".ps1": "powershell",
        ".cmd": "batch",
    }.get(suffix, suffix.lstrip(".") or "text")


def load_pre_update_snapshot_records(
    *,
    since: str = "30d",
    root: str | Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    cutoff = _parse_since(since)
    snapshots_dir = feedback_root(root) / "pre-update-snapshots"
    records: list[dict[str, Any]] = []
    for metadata_path in sorted(snapshots_dir.glob("*/snapshot.json"), reverse=True):
        try:
            snapshot = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if snapshot.get("schema") != PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA:
            continue
        recorded_at = _parse_datetime(str(snapshot.get("recorded_at") or ""))
        if recorded_at and recorded_at < cutoff:
            continue
        record = pre_update_snapshot_record(snapshot)
        if record:
            records.append(record)
        if len(records) >= limit:
            break
    return list(reversed(records))


def pre_update_snapshot_record(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    snapshot_path = Path(str(snapshot.get("snapshot_path") or ""))
    if not snapshot_id or not snapshot_path.exists():
        return None
    patch_id = str(snapshot.get("patch_id") or "")
    patch_phase = str(snapshot.get("phase") or "")
    snapshot_reason = str(snapshot.get("reason") or patch_id or "pre_update_extension_snapshot")
    extension_diffs = _snapshot_extension_diffs(snapshot_path)
    generated_scripts = _normalized_generated_scripts(
        snapshot.get("generated_scripts", []),
        source="pre_update_snapshot",
    )
    if not extension_diffs and not generated_scripts:
        return None
    snapshot_changed_path_count = _safe_int(snapshot.get("changed_path_count"))
    snapshot_untracked_path_count = _safe_int(snapshot.get("untracked_path_count"))
    snapshot_changed_count = snapshot_changed_path_count + snapshot_untracked_path_count
    evidence_changed_count = len(extension_diffs) + len(generated_scripts)
    changed_count = max(snapshot_changed_count, evidence_changed_count)
    record = {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": f"pre-update-extension-snapshot-{snapshot_id}",
        "recorded_at": str(snapshot.get("recorded_at") or now_iso()),
        "workflow": "/mednotes:telemetry",
        "source": "agent",
        "command": snapshot_reason,
        "exit_code": 0,
        "duration_ms": 0,
        "status": "completed_with_warnings" if changed_count else "completed",
        "phase": "pre-update-snapshot",
        "blocked_reason": "",
        "next_action": "Atualizar a extensao somente depois de confirmar que o snapshot pre-update foi recebido ou preservado.",
        "required_inputs": [],
        "human_decision_required": False,
        "dry_run": None,
        "apply": None,
        "payload_summary": {
            "counts": {
                "changed_path_count": _safe_int(snapshot.get("changed_path_count")),
                "untracked_path_count": _safe_int(snapshot.get("untracked_path_count")),
                "snapshot_changed_path_count": snapshot_changed_path_count,
                "snapshot_untracked_path_count": snapshot_untracked_path_count,
                "generated_script_count": len(generated_scripts),
            },
            "warnings": ["pre_update_extension_snapshot_captured"],
            "errors": [],
            "required_inputs": [],
            "relevant_paths": snapshot.get("changed_paths", [])[:24],
            "path_hashes": {},
            "signals": ["extension.pre_update_snapshot"],
            "status": "completed_with_warnings" if changed_count else "completed",
            "phase": "pre-update-snapshot",
        },
        "diagnostic_context": {
            "root_cause_code": "extension.pre_update_snapshot",
            "root_cause_label": "Snapshot pre-update da extensao",
            "recovery_command": "Preservar estes patches antes de rodar gemini extensions update medical-notes-workbench.",
            "missing_inputs": [],
            "decision_context": {"types": [], "decisions": []},
            "blocker_context": {"codes": [], "counts": {}, "summaries": [], "samples": [], "routes": []},
            "contract_gaps": [],
        },
        "environment_context": {
            "extension_integrity": {
                "schema": PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA,
                "drift_detected": bool(changed_count),
                "snapshot_id": snapshot_id,
                "snapshot_path": str(snapshot_path),
                "patch_id": patch_id,
                "phase": patch_phase,
                "reason": snapshot_reason,
                "extension_name": str(snapshot.get("extension_name") or ""),
                "current_version": str(snapshot.get("current_version") or ""),
                "target_version": str(snapshot.get("target_version") or ""),
                "git_head": str(snapshot.get("git_head") or ""),
                "git_available": bool(snapshot.get("git_available")),
                "extension_path": str(snapshot.get("extension_path") or ""),
                "summary": {
                    "changed_count": changed_count,
                    "modified_count": max(snapshot_changed_path_count, len(extension_diffs)),
                    "unexpected_count": snapshot_untracked_path_count,
                    "snapshot_changed_path_count": snapshot_changed_path_count,
                    "snapshot_untracked_path_count": snapshot_untracked_path_count,
                    "snapshot_changed_count": snapshot_changed_count,
                    "snapshot_changed_path_count_mismatch": bool(extension_diffs and not snapshot_changed_count),
                },
                "extension_diffs": extension_diffs,
            }
        },
        "diagnostic_snippets": [],
        "extension_diffs": extension_diffs,
        "generated_scripts": generated_scripts,
    }
    _apply_generated_script_risk_signals(record)
    return attach_telemetry_evidence(record, send_path="pre_update_snapshot")


def _snapshot_extension_diffs(snapshot_path: Path) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for filename, change in (
        ("tracked.diff", "pre_update_tracked"),
        ("staged.diff", "pre_update_staged"),
        ("untracked.diff", "pre_update_untracked"),
    ):
        path = snapshot_path / filename
        try:
            patch = path.read_text(encoding="utf-8")
        except OSError:
            continue
        patch = _filter_pre_update_patch_noise(patch)
        if not patch.strip():
            continue
        sanitized = redact_operational_text(patch, max_chars=MAX_PRE_UPDATE_PATCH_CHARS)
        diffs.append(
            {
                "path": f"pre-update/{filename}",
                "kind": "pre_update_snapshot",
                "change": change,
                "patch": sanitized,
                "truncated": len(sanitized) < len(patch),
            }
        )
    return diffs


def _filter_pre_update_patch_noise(patch: str) -> str:
    blocks = re.split(r"(?m)(?=^diff --git )", patch)
    kept: list[str] = []
    for block in blocks:
        if not block.strip():
            continue
        normalized = block.replace("\\", "/").lower()
        if "git binary patch" in normalized:
            continue
        if ".pyc" in normalized or any(part in normalized for part in PRE_UPDATE_PATCH_NOISE_PARTS):
            continue
        kept.append(block)
    return "\n".join(item.rstrip("\n") for item in kept if item.strip()) + ("\n" if kept else "")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _environment_context(*, root: str | Path | None = None) -> dict[str, Any]:
    try:
        from mednotes.platform.feedback.integrity import safe_check_extension_integrity

        return {
            "extension_integrity": safe_check_extension_integrity(
                cache_dir=feedback_root(root) / "integrity",
                include_diff=True,
            )
        }
    except Exception:
        return {}


def _run_id(workflow: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", workflow).strip("-").lower() or "workflow"
    suffix = hashlib.sha256(f"{workflow}:{time.time_ns()}".encode()).hexdigest()[:8]
    return f"{stamp}-{slug}-{suffix}"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_backlog(*, since: str = "30d", root: str | Path | None = None) -> dict[str, Any]:
    records = load_records(since=since, root=root)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        summary = record.get("payload_summary") if isinstance(record.get("payload_summary"), dict) else {}
        raw_signals = summary.get("signals") if isinstance(summary.get("signals"), list) else []
        signals = {str(signal) for signal in raw_signals if str(signal).strip()}
        diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
        root_signal = str(diagnostic.get("root_cause_code") or "")
        if root_signal and root_signal != "no_issue_detected":
            signals.add(root_signal)
        for signal in signals:
            if signal == "dry_run":
                continue
            _append_backlog_group(grouped, record, signal)
    for record in _dry_runs_without_apply(records):
        signal = "agent.dry_run_without_apply" if record.get("source") == "agent" else "dry_run_without_apply"
        _append_backlog_group(grouped, record, signal)
    for workflow, group in _retry_loop_groups(records):
        for record in group:
            _append_backlog_group(grouped, record, "agent.retry_loop", workflow=workflow)
    for workflow, signal, group in _retry_without_input_change_groups(records):
        for record in group:
            _append_backlog_group(grouped, record, signal, workflow=workflow)

    items = [_backlog_item(workflow, signal, group) for (workflow, signal, _group_key), group in grouped.items()]
    items.sort(key=lambda item: (-_severity_rank(item["severity"]), -item["occurrence_count"], item["workflow"], item["signal"]))
    return {
        "schema": BACKLOG_SCHEMA,
        "generated_at": now_iso(),
        "since": since,
        "run_count": len(records),
        "items": items,
    }


def _append_backlog_group(
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]],
    record: dict[str, Any],
    signal: str,
    *,
    workflow: str | None = None,
) -> None:
    workflow_value = workflow or str(record.get("workflow") or "unknown")
    grouping = _record_grouping_dimensions(record, signal)
    group_key = "|".join(
        str(grouping.get(key) or "")
        for key in ("phase", "root_cause", "target_canonical", "input_hash", "error_hash")
    )
    grouped[(workflow_value, signal, group_key)].append(record)


def load_records(*, since: str = "30d", root: str | Path | None = None) -> list[dict[str, Any]]:
    cutoff = _parse_since(since)
    runs_dir = feedback_root(root) / "runs"
    records: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("schema") != RUN_RECORD_SCHEMA:
            continue
        recorded_at = _parse_datetime(str(data.get("recorded_at") or ""))
        if recorded_at and recorded_at < cutoff:
            continue
        data.setdefault("record_path", str(path))
        records.append(data)
    return records


def _parse_since(value: str) -> datetime:
    value = str(value or "30d").strip()
    match = re.fullmatch(r"(\d+)([dhm])", value)
    now = datetime.now(UTC)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "d":
            return now - timedelta(days=amount)
        if unit == "h":
            return now - timedelta(hours=amount)
        return now - timedelta(minutes=amount)
    parsed = _parse_datetime(value)
    return parsed or (now - timedelta(days=30))


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _dry_runs_without_apply(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    apply_seen: set[str] = set()
    dry_runs: list[dict[str, Any]] = []
    for record in records:
        workflow = str(record.get("workflow") or "")
        if record.get("apply") is True or (record.get("dry_run") is False and record.get("exit_code") == 0):
            apply_seen.add(workflow)
        if record.get("dry_run") is True and record.get("exit_code") == 0:
            dry_runs.append(record)
    return [record for record in dry_runs if str(record.get("workflow") or "") not in apply_seen]


def _retry_loop_groups(records: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        status = str(record.get("status") or "")
        if status not in {"blocked", "failed", "error"}:
            continue
        workflow = str(record.get("workflow") or "unknown")
        phase = str(record.get("phase") or "unknown")
        diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
        cause = str(diagnostic.get("root_cause_code") or record.get("blocked_reason") or status)
        grouped[(workflow, phase, cause)].append(record)
    loops: list[tuple[str, list[dict[str, Any]]]] = []
    for (workflow, _phase, _cause), group in grouped.items():
        if len(group) >= 3:
            loops.append((workflow, group))
    return loops


def _retry_without_input_change_groups(records: list[dict[str, Any]]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        status = str(record.get("status") or "")
        if status not in {"blocked", "failed", "error"}:
            continue
        fingerprint = _record_input_fingerprint(record)
        if not fingerprint:
            continue
        workflow = str(record.get("workflow") or "unknown")
        phase = str(record.get("phase") or "unknown")
        diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
        cause = str(diagnostic.get("root_cause_code") or record.get("blocked_reason") or status)
        grouped[(workflow, phase, cause, fingerprint)].append(record)

    loops: list[tuple[str, str, list[dict[str, Any]]]] = []
    for (workflow, _phase, _cause, _fingerprint), group in grouped.items():
        if len(group) >= 2:
            signal = "agent.retry_without_input_change" if any(record.get("source") == "agent" for record in group) else "retry_without_input_change"
            loops.append((workflow, signal, group))
    return loops


def _record_input_fingerprint(record: dict[str, Any]) -> str:
    summary = record.get("payload_summary") if isinstance(record.get("payload_summary"), dict) else {}
    path_hashes = summary.get("path_hashes") if isinstance(summary.get("path_hashes"), dict) else {}
    artifact_state = summary.get("artifact_state") if isinstance(summary.get("artifact_state"), dict) else {}
    components: dict[str, Any] = {}
    if path_hashes:
        components["path_hashes"] = sorted((str(key), str(value)) for key, value in path_hashes.items())
    if artifact_state:
        components["artifact_state"] = sorted((str(key), str(value)) for key, value in artifact_state.items())
    if not components:
        return ""
    return hashlib.sha256(json.dumps(components, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _record_grouping_dimensions(record: dict[str, Any], signal: str) -> dict[str, str]:
    summary = record.get("payload_summary") if isinstance(record.get("payload_summary"), dict) else {}
    diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
    return {
        "phase": str(record.get("phase") or summary.get("phase") or "unknown"),
        "root_cause": str(diagnostic.get("root_cause_code") or record.get("blocked_reason") or signal or "unknown"),
        "target_canonical": _record_canonical_target(record, diagnostic, summary),
        "input_hash": _record_input_fingerprint(record)[:12],
        "error_hash": _record_error_fingerprint(record, diagnostic, summary)[:12],
    }


def _record_canonical_target(
    record: dict[str, Any],
    diagnostic: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    candidates: list[Any] = [
        record.get("canonical_target"),
        record.get("target_canonical"),
        record.get("target_key"),
        summary.get("canonical_target"),
        summary.get("target_canonical"),
        summary.get("target_key"),
    ]
    error_context = record.get("error_context") if isinstance(record.get("error_context"), dict) else {}
    candidates.append(error_context.get("affected_artifact"))
    decision_context = _json_object_field(_json_object_view(diagnostic), "decision_context")
    for decision in _json_list_field(decision_context, "decisions"):
        decision_view = _json_object_view(decision)
        if decision_view:
            candidates.extend(
                [
                    _json_text(decision_view, "target_key"),
                    _json_text(decision_view, "canonical_target"),
                    _json_text(decision_view, "affected_artifact"),
                ]
            )
    blocker_context = diagnostic.get("blocker_context") if isinstance(diagnostic.get("blocker_context"), dict) else {}
    for key in ("samples", "summaries", "routes"):
        values_candidate = blocker_context.get(key)
        values = values_candidate if isinstance(values_candidate, list) else []
        for item in values[:MAX_DIAGNOSTIC_ITEMS]:
            if isinstance(item, dict):
                candidates.extend(
                    [
                        item.get("target_key"),
                        item.get("canonical_target"),
                        item.get("target_canonical"),
                        item.get("keep_path"),
                        item.get("path"),
                    ]
                )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return redact_snippet(text, max_chars=160)
    return ""


def _record_error_fingerprint(
    record: dict[str, Any],
    diagnostic: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    error_context = record.get("error_context") if isinstance(record.get("error_context"), dict) else {}
    values: list[Any] = [
        diagnostic.get("root_cause_code"),
        record.get("blocked_reason"),
        error_context.get("error_summary"),
        error_context.get("root_cause"),
    ]
    if isinstance(summary.get("errors"), list):
        values.extend(summary["errors"][:5])
    if isinstance(summary.get("warnings"), list):
        values.extend(summary["warnings"][:5])
    command_events = record.get("command_events") if isinstance(record.get("command_events"), list) else []
    for event in command_events[:3]:
        if isinstance(event, dict):
            values.extend([event.get("error"), event.get("stderr_tail"), event.get("output_tail")])
    text = "\n".join(redact_snippet(value, max_chars=600) for value in values if str(value or "").strip())
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _backlog_item(workflow: str, signal: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    statuses = Counter(str(record.get("status") or "unknown") for record in records)
    sample_run_ids = [str(record.get("run_id")) for record in records[:5]]
    title, improvement_type, recommendation, suggested_test = _recommendation(signal)
    severity = _severity_for(signal, count, statuses)
    grouping = _merged_grouping_dimensions(records, signal)
    evidence_bits = []
    grouping_evidence = _format_grouping_evidence(grouping)
    if grouping_evidence:
        evidence_bits.append(grouping_evidence)
    blocked = Counter(str(record.get("blocked_reason") or "") for record in records if record.get("blocked_reason"))
    if blocked:
        evidence_bits.append("blocked_reason: " + ", ".join(f"{key}={value}" for key, value in blocked.most_common(3)))
    evidence_bits.append("status: " + ", ".join(f"{key}={value}" for key, value in statuses.most_common(4)))
    item = {
        "id": hashlib.sha256(
            json.dumps({"workflow": workflow, "signal": signal, "grouping": grouping}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12],
        "title": title,
        "workflow": workflow,
        "signal": signal,
        "grouping": grouping,
        "occurrence_count": count,
        "severity": severity,
        "improvement_type": improvement_type,
        "evidence": "; ".join(evidence_bits),
        "recommendation": recommendation,
        "suggested_test": suggested_test,
        "sample_run_ids": sample_run_ids,
    }
    retry_governance = _group_retry_governance(records, signal)
    if retry_governance:
        item["retry_governance"] = retry_governance
        item["evidence"] += (
            f"; retry_budget: {retry_governance['category']}<={retry_governance['max_attempts']} "
            f"attempt(s)"
        )
    return item


def _merged_grouping_dimensions(records: list[dict[str, Any]], signal: str) -> dict[str, str]:
    values = [_record_grouping_dimensions(record, signal) for record in records]
    grouping: dict[str, str] = {}
    for key in ("phase", "root_cause", "target_canonical", "input_hash", "error_hash"):
        counter = Counter(str(item.get(key) or "") for item in values if str(item.get(key) or ""))
        if not counter:
            grouping[key] = ""
        elif len(counter) == 1:
            grouping[key] = next(iter(counter))
        else:
            grouping[key] = "mixed:" + ",".join(f"{value}={count}" for value, count in counter.most_common(3))
    return grouping


def _format_grouping_evidence(grouping: dict[str, str]) -> str:
    parts = [
        f"phase={grouping.get('phase')}" if grouping.get("phase") else "",
        f"root_cause={grouping.get('root_cause')}" if grouping.get("root_cause") else "",
        f"target={grouping.get('target_canonical')}" if grouping.get("target_canonical") else "",
        f"input_hash={grouping.get('input_hash')}" if grouping.get("input_hash") else "",
        f"error_hash={grouping.get('error_hash')}" if grouping.get("error_hash") else "",
    ]
    return "group: " + "; ".join(item for item in parts if item) if any(parts) else ""


def _group_retry_governance(records: list[dict[str, Any]], signal: str) -> dict[str, Any]:
    if "retry" not in signal and "dry_run_without_apply" not in signal:
        return {}
    categories = Counter()
    selected: dict[str, Any] = {}
    for record in records:
        diagnostic = record.get("diagnostic_context") if isinstance(record.get("diagnostic_context"), dict) else {}
        governance = diagnostic.get("retry_governance") if isinstance(diagnostic.get("retry_governance"), dict) else {}
        category = str(governance.get("category") or "")
        if category:
            categories[category] += 1
            if not selected:
                selected = governance
    if not selected:
        selected = RETRY_BUDGETS.get("dry_run" if "dry_run" in signal else "generic", {})
        category = "dry_run" if "dry_run" in signal else "generic"
    else:
        category = categories.most_common(1)[0][0]
        if selected.get("category") != category:
            selected = {"category": category, **RETRY_BUDGETS.get(category, {})}
    return {
        "category": category,
        "max_attempts": int(selected.get("max_attempts", 1)),
        "rule": str(selected.get("rule") or "Retry deve seguir next_action e mudar input relevante antes de repetir."),
    }


def _recommendation(signal: str) -> tuple[str, str, str, str]:
    if signal.startswith("agent."):
        return _agent_recommendation(signal)
    if signal == ENVIRONMENT_BLOCKER_CODE:
        return (
            "Ambiente Windows/path/venv bloqueando workflow",
            "setup/preflight",
            "Guiar o agente para /mednotes:setup, bootstrap/reset oficial e retry apenas apos ambiente corrigido, sem editar scripts/runbooks como workaround.",
            "Fixture com erro de uv/PowerShell/path Windows deve gerar environment_blocker.windows_path_or_venv e error_context estruturado.",
        )
    if signal == "canonical_merge_required":
        return (
            "Merge canônico necessário",
            "workflow/canonical-merge",
            "Fundir informação nova no alvo canônico, preservar múltiplas referências e validar coverage/proveniência antes de publicar.",
            "Payload com canonical_merge_required deve agrupar por alvo canônico e sugerir merge antes do publish.",
        )
    if signal == "human_decision_required.ambiguous_canonical_target":
        return (
            "Escolha de alvo canônico pendente",
            "workflow/canonical-merge",
            "Coletar escolha explícita do alvo canônico, ajustar note_plan e seguir a rota indicada sem lançar architects antes da decisão.",
            "Payload com alvo canônico ambíguo deve manter human_decision_packet e continue_after_choice.",
        )
    if signal == "provenance_gap":
        return (
            "Lacuna de proveniência multi-fonte",
            "contract/provenance",
            "Bloquear publish até completar coverage.sources e Fontes Consolidadas para todas as fontes novas.",
            "Payload com provenance_gap deve preservar error_context e não marcar raw como processado.",
        )
    if signal == "batch_state_mismatch":
        return (
            "Artefatos de lote incompatíveis",
            "contract/batch-state",
            "Regenerar coverage, manifest e dry-run a partir do note_plan atual antes de avançar.",
            "Artefatos com hashes divergentes devem bloquear e agrupar por input_hash.",
        )
    if signal == "missing_next_action":
        return (
            "Adicionar next_action acionavel quando houver warning, bloqueio ou falha",
            "mensagem/contrato",
            "Atualizar o payload do workflow para sempre explicar o comando seguro seguinte ou a decisao humana pendente.",
            "Fixture com status nao-concluido deve conter next_action nao vazio.",
        )
    if signal == "human_decision_required":
        return (
            "Reduzir ou explicitar decisoes humanas recorrentes",
            "UX/guardrail",
            "Agrupar decisoes repetidas, melhorar opcoes visiveis e identificar casos que podem virar regra deterministica segura.",
            "Fixture com human_decision_required deve conter human_decision_packet e resume_action.",
        )
    if signal == "dry_run_without_apply":
        return (
            "Dry-run sem continuidade detectado",
            "UX",
            "Melhorar o resumo de preview para deixar a confirmacao seguinte mais obvia e registrar quando o usuario descarta o plano.",
            "Fixture de dry-run deve sugerir apply/confirmacao ou descarte explicito.",
        )
    if signal == "retry_without_input_change":
        return (
            "Retry repetido sem mudanca de input",
            "guardrail/observabilidade",
            "Comparar hashes do artefato consumido e interromper repeticao quando a fase falha de novo sem coverage/manifest/note_plan alterado.",
            "Dois bloqueios iguais com os mesmos hashes de input devem gerar retry_without_input_change.",
        )
    if signal == "anki_model_validation_failed":
        return (
            "Modelo Anki bloqueou criacao de cards",
            "setup/preflight",
            "Antecipar validacao/provisionamento de modelos antes da etapa de formulacao ou tornar a recuperacao mais direta.",
            "Fixture com modelo incompleto deve bloquear antes de montar notas Anki.",
        )
    if signal.startswith("required_input:coverage_path"):
        return (
            "Coverage ausente ou incompleto bloqueou publicacao",
            "guardrail/docs",
            "Melhorar preflight e mensagem da fase anterior para garantir coverage derivado do note_plan antes do stage/publish.",
            "Fixture de publish sem coverage_path deve falhar antes de mutar e recomendar stage-note --coverage.",
        )
    if signal.startswith("blocked:graph_blockers"):
        return (
            "Blockers de grafo recorrentes",
            "guardrail",
            "Priorizar uma regra deterministica em fix-wiki ou um resumo melhor com amostras e rota de resolucao.",
            "Fixture com blocker de grafo deve gerar rota em blocker_resolution e pular linker real.",
        )
    if signal.startswith("blocked:"):
        reason = signal.split(":", 1)[1]
        return (
            f"Bloqueio recorrente: {reason}",
            "guardrail",
            "Verificar se o bloqueio pode ser antecipado por preflight, explicado melhor ou coberto por teste patologico.",
            f"Fixture deve reproduzir {reason} e confirmar status/blocked_reason/next_action.",
        )
    if signal == "warnings":
        return (
            "Warnings recorrentes nos workflows",
            "qualidade",
            "Separar warning aceitavel de warning que merece correcao automatica, doc ou teste de regressao.",
            "Fixture deve preservar warning esperado e evitar regressao silenciosa.",
        )
    if signal == "errors":
        return (
            "Erros recorrentes nos workflows",
            "bug/preflight",
            "Agrupar mensagens de erro e mover a falha para uma validacao mais cedo quando possivel.",
            "Fixture com erro conhecido deve retornar JSON/exit code contratual sem traceback.",
        )
    return (
        f"Padrao recorrente: {signal}",
        "investigacao",
        "Revisar os runs amostrados e transformar o padrao em ajuste de contrato, mensagem, guardrail ou teste.",
        "Adicionar fixture cobrindo o padrao recorrente.",
    )


def _severity_for(signal: str, count: int, statuses: Counter[str]) -> str:
    if signal.startswith("agent."):
        if signal in {
            "agent.retry_loop",
            "agent.script_or_prompt_drift",
            "agent.unexpected_mutation",
            "agent.missing_error_context",
            "agent.missing_agent_metrics",
            "agent.timeout_or_max_turns",
        }:
            return "high"
        if statuses.get("failed") or statuses.get("error") or statuses.get("blocked"):
            return "high" if count >= 2 else "medium"
        return "medium" if count >= 2 else "low"
    if signal == ENVIRONMENT_BLOCKER_CODE:
        return "high" if statuses.get("failed") or statuses.get("blocked") or count >= 2 else "medium"
    if signal.startswith("blocked:") or signal == "errors" or statuses.get("failed"):
        return "high" if count >= 2 else "medium"
    if signal in {"human_decision_required", "anki_model_validation_failed"}:
        return "high" if count >= 3 else "medium"
    if signal == "retry_without_input_change":
        return "high" if count >= 2 else "medium"
    if signal == "missing_next_action":
        return "medium"
    return "medium" if count >= 3 else "low"


def _severity_rank(severity: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(severity, 0)


def _agent_recommendation(signal: str) -> tuple[str, str, str, str]:
    labels = {
        "agent.retry_loop": (
            "Loop ou retry improdutivo do agente",
            "agent-behavior/loop",
            "Identificar a fase repetida, antecipar o bloqueio e instruir o agente a parar com next_action claro.",
            "Fixture com 3 falhas iguais no mesmo workflow/fase deve gerar agent.retry_loop.",
        ),
        "agent.script_or_prompt_drift": (
            "Agente alterou prompt/runbook/script local",
            "agent-behavior/integrity",
            "Comparar o drift, decidir se vira update publicado ou rollback/reinstalação da extensão.",
            "Fixture com source=agent e integrity drift em script/command/skill deve gerar agent.script_or_prompt_drift.",
        ),
        "agent.ignored_next_action": (
            "Agente ignorou next_action",
            "agent-behavior/contract",
            "Reforçar o contrato de resposta para executar apenas a rota segura indicada pelo workflow.",
            "Payload com agent_events ignored_next_action deve aparecer no backlog e no email.",
        ),
        "agent.wrong_phase": (
            "Agente executou fase errada",
            "agent-behavior/phase",
            "Deixar a fase permitida explícita no resumo e bloquear mutações fora da fase esperada quando possível.",
            "Payload com wrong_phase deve preservar fase esperada e ação de recuperação redigidas.",
        ),
        "agent.unexpected_mutation": (
            "Agente fez mutação inesperada",
            "agent-behavior/safety",
            "Adicionar preflight/guardrail para impedir escrita fora do workflow ou da confirmação esperada.",
            "Payload com unexpected_mutation deve virar severidade alta.",
        ),
        "agent.command_failed": (
            "Comando conduzido pelo agente falhou",
            "agent-behavior/command",
            "Agrupar família de comando, erro e próxima ação para transformar falha repetida em preflight ou teste.",
            "Payload com command_failed deve incluir command_family e snippet redigido.",
        ),
        "agent.workflow_blocked": (
            "Agente encontrou workflow bloqueado",
            "agent-behavior/blocker",
            "Transformar bloqueios recorrentes em mensagem de parada, rota de recuperação ou correção determinística.",
            "Payload com workflow_blocked deve preservar blocked_reason e next_action esperado.",
        ),
        "agent.missing_error_context": (
            "Agente bloqueou sem error_context",
            "agent-behavior/contract",
            "Exigir error_context em todo bloqueio/falha agent-driven para que o próximo retry tenha causa, artefato e escopo claros.",
            "Run source=agent bloqueado sem error_context deve gerar agent.missing_error_context.",
        ),
        "agent.missing_agent_metrics": (
            "Subagente bloqueou sem agent_metrics",
            "agent-behavior/contract",
            "Exigir agent_metrics mesmo em blocked packet para separar prompt ruim, timeout real e escopo excessivo.",
            "Timeout/max_turns sem agent_metrics deve gerar agent.missing_agent_metrics.",
        ),
        "agent.timeout_or_max_turns": (
            "Subagente estourou timeout ou max_turns",
            "agent-behavior/timeout",
            "Parar retry cego, reduzir o work item ou criar recuperação oficial antes de rodar outro subagente.",
            "Payload agentico bloqueado por timeout_or_max_turns deve gerar agent.timeout_or_max_turns.",
        ),
        "agent.manual_intervention": (
            "Agente precisou de intervenção manual",
            "agent-behavior/manual",
            "Avaliar se a decisão pode virar default seguro, checklist preflight ou pergunta estruturada.",
            "Payload com manual_intervention deve registrar ação e resultado sem conteúdo sensível.",
        ),
        "agent.dry_run_without_apply": (
            "Dry-run limpo sem apply posterior",
            "agent-behavior/follow-through",
            "Garantir que o agente execute o apply indicado ou registre explicitamente por que parou após o dry-run.",
            "Sequência com dry-run agentico limpo sem apply posterior deve gerar agent.dry_run_without_apply.",
        ),
        "agent.retry": (
            "Retry do agente registrado",
            "agent-behavior/retry",
            "Distinguir retry útil de loop improdutivo e limitar repetição antes de pedir decisão humana.",
            "Payload com retry deve ser redigido e agrupável por fase.",
        ),
        "agent.retry_without_input_change": (
            "Agente repetiu sem mudar input",
            "agent-behavior/loop",
            "Fazer o agente parar após a segunda falha com os mesmos hashes e seguir o error_context em vez de tentar de novo.",
            "Dois bloqueios agenticos iguais com mesmos hashes devem gerar agent.retry_without_input_change.",
        ),
    }
    if signal in labels:
        return labels[signal]
    event = signal.split(".", 1)[1] if "." in signal else signal
    return (
        f"Comportamento do agente: {event}",
        "agent-behavior/investigacao",
        "Revisar os eventos do agente e transformar o padrão em contrato, guardrail ou teste.",
        "Adicionar fixture cobrindo agent_events para esse padrão.",
    )
