"""Remote telemetry for workflow feedback records."""
from __future__ import annotations

import json
import os
import platform
import re
import uuid
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import httpx

import mednotes.platform.feedback.core as core
from mednotes.kernel.base import JsonObject, JsonObjectAdapter
from mednotes.platform.feedback.contracts import TelemetryStatusSnapshot
from mednotes.platform.feedback.telemetry_config import TelemetryConfig, TelemetrySection
from mednotes.platform.paths import default_config_path

TELEMETRY_ENVELOPE_SCHEMA = "medical-notes-workbench.workflow-telemetry-envelope.v1"
TELEMETRY_STATUS_SCHEMA = "medical-notes-workbench.workflow-telemetry-status.v1"
TELEMETRY_SENT_SCHEMA = "medical-notes-workbench.workflow-telemetry-sent.v1"
TRUSTED_DEBUG_PAYLOAD_LEVEL = "trusted_extension_debug"
PAYLOAD_LEVELS = {"diagnostic_redacted", "full_logs", TRUSTED_DEBUG_PAYLOAD_LEVEL}
DEFAULT_PAYLOAD_LEVEL = "diagnostic_redacted"
DEFAULT_MAX_ENVELOPE_BYTES = 256 * 1024
TRUSTED_DEBUG_MAX_ENVELOPE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
CONFIG_ENV_VAR = "MEDNOTES_TELEMETRY_CONFIG"
DISABLED_ENV_VAR = "MEDNOTES_TELEMETRY_DISABLED"
DEFAULTS_ENV_VAR = "MEDNOTES_TELEMETRY_DEFAULTS"
DEFAULTS_DISABLED_ENV_VAR = "MEDNOTES_TELEMETRY_DEFAULTS_DISABLED"
DEFAULTS_FILE_NAME = "telemetry.defaults.json"
LOCAL_DEFAULTS_FILE_NAME = ".telemetry-defaults.json"
PROJECT_DISABLED_SOURCE = "project_disabled"
REMOTE_TELEMETRY_DISABLED_REASON = "remote_telemetry_disabled_by_project"
REMOTE_TELEMETRY_DISABLED = True


def telemetry_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(os.path.expandvars(str(path))).expanduser()
    override = os.getenv(CONFIG_ENV_VAR)
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    value = os.getenv("MEDNOTES_CONFIG")
    if value:
        return Path(os.path.expandvars(value)).expanduser()
    return default_config_path()


def read_telemetry_config(path: str | Path | None = None) -> TelemetryConfig:
    data = _read_config(path)
    raw_section = data["telemetry"] if "telemetry" in data else {}
    section = JsonObjectAdapter.validate_python(raw_section if isinstance(raw_section, dict) else {})
    if REMOTE_TELEMETRY_DISABLED:
        return _project_disabled_config(section)
    defaults = _read_distribution_defaults()
    if _should_apply_distribution_defaults(section, defaults):
        section = _materialize_distribution_defaults(path, section, defaults or {})
    return _config_from_section(section)


def _config_from_section(section: JsonObject) -> TelemetryConfig:
    typed_section = TelemetrySection.from_payload(section)
    payload_level = typed_section.payload_level
    if payload_level not in PAYLOAD_LEVELS:
        payload_level = DEFAULT_PAYLOAD_LEVEL
    default_max_bytes = _default_max_envelope_bytes(payload_level)
    max_bytes = typed_section.max_envelope_bytes or default_max_bytes
    return TelemetryConfig(
        enabled=typed_section.enabled,
        endpoint_url=typed_section.endpoint_url,
        auth_token=typed_section.auth_token,
        payload_level=payload_level,
        consent_at=typed_section.consent_at,
        install_id=typed_section.install_id,
        max_envelope_bytes=max(16 * 1024, min(2 * 1024 * 1024, max_bytes)),
        source=typed_section.source,
        auto_enabled_at=typed_section.auto_enabled_at,
        opt_out_at=typed_section.opt_out_at,
        defaults_path=typed_section.defaults_path,
    )


def enable_telemetry(
    *,
    endpoint_url: str,
    auth_token: str,
    payload_level: str = DEFAULT_PAYLOAD_LEVEL,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    endpoint_url = endpoint_url.strip()
    auth_token = auth_token.strip()
    if not endpoint_url.startswith(("https://", "http://")):
        raise ValueError("--endpoint must be an http(s) URL")
    if not auth_token:
        raise ValueError("--token is required")
    if payload_level not in PAYLOAD_LEVELS:
        raise ValueError(f"--payload-level must be one of: {', '.join(sorted(PAYLOAD_LEVELS))}")
    current = read_telemetry_config(config_path)
    if REMOTE_TELEMETRY_DISABLED:
        _write_telemetry_section(
            config_path,
            _project_disabled_values(
                {
                    "payload_level": payload_level,
                    "install_id": current.install_id,
                    "opt_out_at": core.now_iso(),
                }
            ),
        )
        return telemetry_status(config_path=config_path)
    max_envelope_bytes = current.max_envelope_bytes
    if payload_level == TRUSTED_DEBUG_PAYLOAD_LEVEL:
        max_envelope_bytes = max(max_envelope_bytes, TRUSTED_DEBUG_MAX_ENVELOPE_BYTES)
    values = {
        "enabled": True,
        "endpoint_url": endpoint_url,
        "auth_token": auth_token,
        "payload_level": payload_level,
        "consent_at": core.now_iso(),
        "install_id": current.install_id or str(uuid.uuid4()),
        "max_envelope_bytes": max_envelope_bytes,
        "source": "user",
        "auto_enabled_at": current.auto_enabled_at,
        "opt_out_at": "",
        "defaults_path": current.defaults_path,
    }
    _write_telemetry_section(config_path, values)
    return telemetry_status(config_path=config_path)


def disable_telemetry(*, config_path: str | Path | None = None) -> dict[str, Any]:
    current = read_telemetry_config(config_path)
    source = PROJECT_DISABLED_SOURCE if REMOTE_TELEMETRY_DISABLED else "user_disabled"
    values = _project_disabled_values(
        {
            "payload_level": current.payload_level,
            "install_id": current.install_id,
            "source": source,
            "opt_out_at": core.now_iso(),
        }
    )
    _write_telemetry_section(config_path, values)
    return telemetry_status(config_path=config_path)


def telemetry_status(*, config_path: str | Path | None = None, root: str | Path | None = None) -> dict[str, Any]:
    config = read_telemetry_config(config_path)
    sent = _load_sent(root=root)
    outbox_dir = _outbox_dir(root)
    outbox_count = len(list(outbox_dir.glob("*.json"))) if outbox_dir.exists() else 0
    recent_records = core.load_records(since="7d", root=root)[-5:]
    recent_bundles = [
        {
            "run_id": str(record.get("run_id") or ""),
            "workflow": str(record.get("workflow") or ""),
            "bundle_id": str(
                (record.get("telemetry_evidence") if isinstance(record.get("telemetry_evidence"), dict) else core.build_telemetry_evidence(record)).get("bundle_id")
            ),
        }
        for record in recent_records
    ]
    recent_hook_errors = core.load_hook_errors(since="24h", root=root, limit=5)
    recent_hook_events = core.load_hook_events(since="24h", root=root, limit=5)
    pending_snapshots = core.load_pre_update_snapshot_records(since="30d", root=root, limit=5)
    return TelemetryStatusSnapshot(
        enabled=config.enabled,
        ready=config.ready,
        endpoint_url=_redact_endpoint(config.endpoint_url),
        payload_level=config.payload_level,
        consent_at=config.consent_at,
        auto_enabled_at=config.auto_enabled_at,
        opt_out_at=config.opt_out_at,
        source=config.source,
        install_id=config.install_id,
        outbox_count=outbox_count,
        sent_run_count=len(sent.get("sent_run_ids", [])),
        config_path=str(telemetry_config_path(config_path)),
        defaults_path=config.defaults_path,
        recent_bundles=recent_bundles,
        pending_pre_update_snapshot_count=len(pending_snapshots),
        hook_health={
            "recent_event_count": len(recent_hook_events),
            "recent_error_count": len(recent_hook_errors),
            "latest_error_types": [str(item.get("type") or "") for item in recent_hook_errors[:5]],
        },
    ).to_payload()


def preview_envelope(
    *,
    since: str = "30d",
    limit: int = 20,
    config_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    config = read_telemetry_config(config_path)
    records = _records_for_envelope(since=since, limit=limit, root=root, config=config)
    return build_envelope(records, config=config)


def send_telemetry(
    *,
    since: str = "30d",
    limit: int = 20,
    config_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    config = read_telemetry_config(config_path)
    if not config.ready:
        reason = REMOTE_TELEMETRY_DISABLED_REASON if config.source == PROJECT_DISABLED_SOURCE else "telemetry_not_enabled"
        return {"ok": False, "sent": 0, "queued": 0, "reason": reason, "status": telemetry_status(config_path=config_path, root=root)}
    first_flush = flush_outbox(config=config, root=root)
    if first_flush.get("failed", 0):
        first_flush["queued"] = 0
        return first_flush
    records = _records_for_envelope(since=since, limit=limit, root=root, config=config)
    queued = 0
    if records:
        envelope = build_envelope(records, config=config)
        _enqueue_envelope(envelope, root=root)
        queued = 1
    result = flush_outbox(config=config, root=root)
    result["sent"] = int(result.get("sent", 0)) + int(first_flush.get("sent", 0))
    result["queued"] = queued
    return result


def safe_auto_send_record(record: dict[str, Any], *, raw_payload: Any = None, root: str | Path | None = None) -> dict[str, Any] | None:
    if os.getenv(DISABLED_ENV_VAR) == "1":
        return None
    try:
        config = read_telemetry_config()
        if not config.ready:
            return None
        envelope = build_envelope([record], config=config, raw_payloads={str(record.get("run_id")): raw_payload})
        _enqueue_envelope(envelope, root=root)
        return flush_outbox(config=config, root=root, limit=3)
    except Exception:
        return None


def build_envelope(
    records: list[dict[str, Any]],
    *,
    config: TelemetryConfig | None = None,
    raw_payloads: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or read_telemetry_config()
    raw_payloads = raw_payloads or {}
    envelope = {
        "schema": TELEMETRY_ENVELOPE_SCHEMA,
        "envelope_id": str(uuid.uuid4()),
        "generated_at": core.now_iso(),
        "install_id": config.install_id,
        "payload_level": config.payload_level,
        "client": _client_context(),
        "records": [
            _telemetry_record(record, payload_level=config.payload_level, raw_payload=raw_payloads.get(str(record.get("run_id"))))
            for record in records
        ],
        "limits": {
            "max_envelope_bytes": config.max_envelope_bytes,
        },
    }
    return _fit_envelope(envelope, max_bytes=config.max_envelope_bytes)


def flush_outbox(*, config: TelemetryConfig | None = None, root: str | Path | None = None, limit: int = 20) -> dict[str, Any]:
    config = config or read_telemetry_config()
    if not config.ready:
        reason = REMOTE_TELEMETRY_DISABLED_REASON if config.source == PROJECT_DISABLED_SOURCE else "telemetry_not_enabled"
        return {"ok": False, "sent": 0, "failed": 0, "reason": reason}
    sent = 0
    failed = 0
    errors: list[str] = []
    for path in sorted(_outbox_dir(root).glob("*.json"))[:limit]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            _post_envelope(envelope, config=config)
            _mark_sent(envelope, root=root)
            path.unlink()
            sent += 1
        except Exception as exc:
            failed += 1
            errors.append(core.redact_snippet(str(exc)))
            _bump_attempt(path)
    return {"ok": failed == 0, "sent": sent, "failed": failed, "errors": errors[:5]}


def _read_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = telemetry_config_path(path)
    if not config_path.exists():
        return {}
    import tomllib

    with config_path.open("rb") as fh:
        return tomllib.load(fh)


def _write_telemetry_section(path: str | Path | None, values: dict[str, Any]) -> None:
    config_path = telemetry_config_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    section = _render_telemetry_section(values)
    pattern = re.compile(r"(?ms)^\[telemetry\]\n.*?(?=^\[[^\n]+\]\s*$|\Z)")
    if pattern.search(text):
        updated = pattern.sub(section.rstrip() + "\n\n", text)
    else:
        updated = text.rstrip() + ("\n\n" if text.strip() else "") + section
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(config_path)


def _render_telemetry_section(values: dict[str, Any]) -> str:
    lines = ["[telemetry]"]
    keys = (
        "enabled",
        "endpoint_url",
        "auth_token",
        "payload_level",
        "consent_at",
        "install_id",
        "max_envelope_bytes",
        "source",
        "auto_enabled_at",
        "opt_out_at",
        "defaults_path",
    )
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            if key.endswith("_path"):
                value = str(value or "").replace("\\", "/")
            rendered = json.dumps(str(value or ""), ensure_ascii=False)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _project_disabled_values(section: dict[str, Any]) -> dict[str, Any]:
    typed_section = TelemetrySection.from_payload(JsonObjectAdapter.validate_python(section))
    payload_level = typed_section.payload_level
    if payload_level not in PAYLOAD_LEVELS:
        payload_level = DEFAULT_PAYLOAD_LEVEL
    return {
        "enabled": False,
        "endpoint_url": "",
        "auth_token": "",
        "payload_level": payload_level,
        "consent_at": "",
        "install_id": typed_section.install_id,
        "max_envelope_bytes": _coerce_max_envelope_bytes(typed_section.max_envelope_bytes, payload_level=payload_level),
        "source": PROJECT_DISABLED_SOURCE,
        "auto_enabled_at": "",
        "opt_out_at": typed_section.opt_out_at,
        "defaults_path": "",
    }


def _project_disabled_config(section: JsonObject) -> TelemetryConfig:
    values = _project_disabled_values({**section, "source": PROJECT_DISABLED_SOURCE})
    return _config_from_section(values)


def _should_apply_distribution_defaults(section: dict[str, Any], defaults: dict[str, Any] | None) -> bool:
    if not _distribution_defaults_ready(defaults):
        return False
    if section.get("enabled") is False and (
        section.get("opt_out_at")
        or section.get("consent_at")
        or section.get("endpoint_url")
        or section.get("auth_token")
    ):
        return False
    if section.get("enabled") is True and section.get("endpoint_url") and section.get("auth_token"):
        return _should_refresh_distribution_defaults(section, defaults or {})
    return True


def _distribution_defaults_ready(defaults: dict[str, Any] | None) -> bool:
    return bool(defaults and defaults.get("enabled") and defaults.get("endpoint_url") and defaults.get("auth_token"))


def _should_refresh_distribution_defaults(section: dict[str, Any], defaults: dict[str, Any]) -> bool:
    if not _same_distribution_channel(section, defaults):
        return False
    desired_payload = _distribution_payload_level(section, defaults)
    current_payload = str(section.get("payload_level") or DEFAULT_PAYLOAD_LEVEL)
    if current_payload not in PAYLOAD_LEVELS:
        current_payload = DEFAULT_PAYLOAD_LEVEL
    desired_max = _coerce_max_envelope_bytes(defaults.get("max_envelope_bytes"), payload_level=desired_payload)
    current_max = _coerce_max_envelope_bytes(section.get("max_envelope_bytes"), payload_level=current_payload)
    return (
        desired_payload != current_payload
        or desired_max > current_max
        or str(section.get("defaults_path") or "") != str(defaults.get("_path") or "")
    )


def _same_distribution_channel(section: dict[str, Any], defaults: dict[str, Any]) -> bool:
    if str(section.get("source") or "") == "distribution_default":
        return True
    if section.get("defaults_path"):
        return True
    return (
        str(section.get("endpoint_url") or "") == str(defaults.get("endpoint_url") or "")
        and str(section.get("auth_token") or "") == str(defaults.get("auth_token") or "")
    )


def _distribution_payload_level(section: dict[str, Any], defaults: dict[str, Any]) -> str:
    for value in (defaults.get("payload_level"), section.get("payload_level"), DEFAULT_PAYLOAD_LEVEL):
        payload_level = str(value or "")
        if payload_level in PAYLOAD_LEVELS:
            return payload_level
    return DEFAULT_PAYLOAD_LEVEL


def _materialize_distribution_defaults(
    path: str | Path | None,
    section: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    now = core.now_iso()
    payload_level = _distribution_payload_level(section, defaults)
    current_max = _coerce_max_envelope_bytes(section.get("max_envelope_bytes"), payload_level=payload_level)
    default_max = _coerce_max_envelope_bytes(defaults.get("max_envelope_bytes"), payload_level=payload_level)
    values = {
        "enabled": True,
        "endpoint_url": str(section.get("endpoint_url") or defaults.get("endpoint_url") or ""),
        "auth_token": str(section.get("auth_token") or defaults.get("auth_token") or ""),
        "payload_level": payload_level,
        "consent_at": str(section.get("consent_at") or defaults.get("consent_at") or ""),
        "install_id": str(section.get("install_id") or str(uuid.uuid4())),
        "max_envelope_bytes": max(current_max, default_max),
        "source": "distribution_default",
        "auto_enabled_at": str(section.get("auto_enabled_at") or now),
        "opt_out_at": "",
        "defaults_path": str(defaults.get("_path") or ""),
    }
    _write_telemetry_section(path, values)
    return values


def _default_max_envelope_bytes(payload_level: str) -> int:
    return TRUSTED_DEBUG_MAX_ENVELOPE_BYTES if payload_level == TRUSTED_DEBUG_PAYLOAD_LEVEL else DEFAULT_MAX_ENVELOPE_BYTES


def _coerce_max_envelope_bytes(value: Any, *, payload_level: str = DEFAULT_PAYLOAD_LEVEL) -> int:
    try:
        parsed = int(value or _default_max_envelope_bytes(payload_level))
    except (TypeError, ValueError):
        parsed = _default_max_envelope_bytes(payload_level)
    return max(16 * 1024, min(2 * 1024 * 1024, parsed))


def _read_distribution_defaults() -> dict[str, Any] | None:
    if os.getenv(DEFAULTS_DISABLED_ENV_VAR) == "1":
        return None
    for path in _distribution_default_candidates():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        telemetry = data.get("telemetry") if isinstance(data.get("telemetry"), dict) else data
        if not isinstance(telemetry, dict):
            continue
        telemetry = dict(telemetry)
        telemetry["_path"] = str(path)
        return telemetry
    return None


def _distribution_default_candidates() -> list[Path]:
    override = os.getenv(DEFAULTS_ENV_VAR)
    if override:
        return [Path(os.path.expandvars(override)).expanduser()]
    module_path = Path(__file__).resolve()
    roots: list[Path] = []
    for parent in module_path.parents:
        if parent.name in {"src", "gemini-cli-extension", "medical-notes-workbench"}:
            roots.append(parent.parent if parent.name == "src" else parent)
    roots.append(module_path.parents[2])
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in roots:
        for name in (DEFAULTS_FILE_NAME, LOCAL_DEFAULTS_FILE_NAME):
            candidate = root / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _unsent_records(*, since: str, limit: int, root: str | Path | None) -> list[dict[str, Any]]:
    sent = set(_load_sent(root=root).get("sent_run_ids", []))
    records = [record for record in core.load_records(since=since, root=root) if str(record.get("run_id")) not in sent]
    return records[: max(1, limit)]


def _referenced_hook_ids(records: list[dict[str, Any]], key: str) -> set[str]:
    referenced: set[str] = set()
    for record in records:
        values = record.get(key)
        if isinstance(values, list):
            referenced.update(str(value) for value in values if str(value))
    return referenced


def _unreferenced_hook_debug_record(
    *,
    records: list[dict[str, Any]],
    since: str,
    root: str | Path | None,
    sent_run_ids: set[str],
) -> dict[str, Any] | None:
    referenced_event_ids = _referenced_hook_ids(records, "hook_event_ids")
    referenced_error_ids = _referenced_hook_ids(records, "hook_error_ids")
    events = [
        event
        for event in core.load_hook_events(since=since, root=root, limit=100)
        if str(event.get("event_id") or "") not in referenced_event_ids
    ]
    errors = [
        error
        for error in core.load_hook_errors(since=since, root=root, limit=50)
        if str(error.get("error_id") or "") not in referenced_error_ids
    ]
    synthetic = core.hook_debug_record(events=events, errors=errors, since=since)
    if not synthetic or str(synthetic.get("run_id")) in sent_run_ids:
        return None
    return synthetic


def _records_for_envelope(
    *,
    since: str,
    limit: int,
    root: str | Path | None,
    config: TelemetryConfig,
) -> list[dict[str, Any]]:
    records = _unsent_records(since=since, limit=limit, root=root)
    if config.payload_level == TRUSTED_DEBUG_PAYLOAD_LEVEL:
        sent = set(_load_sent(root=root).get("sent_run_ids", []))
        remaining = max(0, limit - len(records))
        if remaining:
            snapshots = [
                record
                for record in core.load_pre_update_snapshot_records(since=since, root=root, limit=remaining)
                if str(record.get("run_id")) not in sent
            ]
            records.extend(snapshots)
        if records:
            synthetic = _unreferenced_hook_debug_record(
                records=records,
                since=since,
                root=root,
                sent_run_ids=sent,
            )
            if synthetic and len(records) < max(1, limit):
                records.append(synthetic)
            return records[: max(1, limit)]
    if records or config.payload_level != TRUSTED_DEBUG_PAYLOAD_LEVEL:
        return records
    events = core.load_hook_events(since=since, root=root, limit=min(100, max(1, limit * 5)))
    errors = core.load_hook_errors(since=since, root=root, limit=min(50, max(1, limit * 3)))
    synthetic = core.hook_debug_record(events=events, errors=errors, since=since)
    if not synthetic:
        return []
    sent = set(_load_sent(root=root).get("sent_run_ids", []))
    return [] if str(synthetic.get("run_id")) in sent else [synthetic]


def _telemetry_record(record: dict[str, Any], *, payload_level: str, raw_payload: Any = None) -> dict[str, Any]:
    payload_summary = record.get("payload_summary", {})
    summary = payload_summary if isinstance(payload_summary, dict) else {}
    diagnostic_context = record.get("diagnostic_context")
    if not isinstance(diagnostic_context, dict):
        diagnostic_context = core.build_diagnostic_context(
            summary,
            summary,
        )
    base = {
        "run_id": record.get("run_id"),
        "recorded_at": record.get("recorded_at"),
        "workflow": record.get("workflow"),
        "source": record.get("source"),
        "exit_code": record.get("exit_code"),
        "duration_ms": record.get("duration_ms"),
        "status": record.get("status") or summary.get("status"),
        "phase": record.get("phase") or summary.get("phase"),
        "blocked_reason": record.get("blocked_reason") or summary.get("blocked_reason"),
        "next_action": record.get("next_action") or summary.get("next_action"),
        "required_inputs": record.get("required_inputs", []) or summary.get("required_inputs", []),
        "human_decision_required": (
            record.get("human_decision_required")
            if record.get("human_decision_required") is not None
            else summary.get("human_decision_required")
        ),
        "dry_run": record.get("dry_run") if record.get("dry_run") is not None else summary.get("dry_run"),
        "apply": record.get("apply") if record.get("apply") is not None else summary.get("apply"),
        "payload_summary": _telemetry_payload_summary(
            summary,
            payload_level=payload_level,
        ),
        "diagnostic_context": redact_object(diagnostic_context),
        "agent_events": redact_object(record.get("agent_events", [])),
        "environment_context": redact_object(record.get("environment_context", {})),
        "diagnostic_snippets": record.get("diagnostic_snippets", []),
        "telemetry_evidence": redact_object(core.build_telemetry_evidence(record, send_path="telemetry_envelope")),
    }
    if payload_level == "full_logs":
        base["command"] = record.get("command", "")
        base["extra"] = redact_object(record.get("extra", {}))
        if raw_payload is not None:
            base["raw_payload_redacted"] = redact_object(raw_payload)
        else:
            base["raw_payload_redacted"] = {"unavailable": True, "reason": "historical_record_has_no_raw_payload"}
    if payload_level == TRUSTED_DEBUG_PAYLOAD_LEVEL:
        integrity = record.get("environment_context", {}).get("extension_integrity", {}) if isinstance(record.get("environment_context"), dict) else {}
        base["extension_diffs"] = _trusted_debug_object(record.get("extension_diffs") or integrity.get("extension_diffs", []))
        base["generated_scripts"] = _trusted_debug_object(record.get("generated_scripts", []))
        base["command_events"] = _trusted_debug_object(record.get("command_events", []))
        base["hook_errors"] = _trusted_debug_object(record.get("hook_errors", []))
        base["hook_event_ids"] = _operational_id_list(record.get("hook_event_ids", []))
        base["hook_error_ids"] = _operational_id_list(record.get("hook_error_ids", []))
    return base


def _operational_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _trusted_debug_object(value: Any, *, depth: int = 0, key_context: str = "") -> Any:
    if depth > 6:
        return "[max-depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            lower = str(key).lower()
            if lower in {"token", "auth_token", "api_key", "apikey", "secret", "password", "authorization"}:
                out[str(key)] = "[redacted]"
            elif lower in {"html", "raw_chat", "note_text", "markdown"}:
                out[str(key)] = f"[{lower} omitted]"
            elif lower in {"patch", "content", "stdout_tail", "stderr_tail", "output_tail", "error", "command"} and isinstance(item, str):
                out[str(key)] = core.redact_operational_text(item, max_chars=96 * 1024)
            elif isinstance(item, str) and (_looks_like_hash_key(lower) or key_context == "path_hashes"):
                out[str(key)] = item if _looks_like_hash_value(item) else core.redact_operational_text(item, max_chars=16 * 1024)
            else:
                out[str(key)] = _trusted_debug_object(item, depth=depth + 1, key_context=lower)
        return out
    if isinstance(value, list):
        return [_trusted_debug_object(item, depth=depth + 1, key_context=key_context) for item in value[:100]]
    if isinstance(value, str):
        return core.redact_operational_text(value, max_chars=16 * 1024)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return core.redact_operational_text(str(value), max_chars=300)


def _looks_like_hash_key(key: str) -> bool:
    lower = key.lower()
    return "hash" in lower or "sha" in lower or "digest" in lower


def _looks_like_hash_value(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Fa-f0-9]{32,128}", value.strip()))


def _telemetry_payload_summary(summary: dict[str, Any], *, payload_level: str) -> dict[str, Any]:
    if payload_level == TRUSTED_DEBUG_PAYLOAD_LEVEL:
        value = _trusted_debug_object(summary)
        return value if isinstance(value, dict) else {}
    value = _redact_paths(summary)
    return value if isinstance(value, dict) else {}


def redact_object(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[max-depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            lower = str(key).lower()
            if lower in {"token", "auth_token", "api_key", "apikey", "secret", "password", "authorization"}:
                out[str(key)] = "[redacted]"
            elif lower in {"content", "markdown", "html", "raw_chat", "note_text"} and isinstance(item, str):
                out[str(key)] = core.redact_snippet(item, max_chars=240)
            else:
                out[str(key)] = redact_object(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [redact_object(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return core.redact_snippet(value, max_chars=1200)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return core.redact_snippet(str(value), max_chars=300)


def _redact_paths(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "relevant_paths" and isinstance(item, list):
                out[key] = [_path_label(str(path)) for path in item]
            elif key == "path_hashes" and isinstance(item, dict):
                out[key] = {_path_label(str(path)): str(hash_value) for path, hash_value in item.items()}
            else:
                out[key] = _redact_paths(item)
        return out
    if isinstance(value, list):
        return [_redact_paths(item) for item in value]
    return value


def _path_label(path: str) -> str:
    p = Path(path)
    suffix = "/".join(p.parts[-3:]) if len(p.parts) >= 3 else p.name or path
    return suffix.replace(str(Path.home()), "~")


def _fit_envelope(envelope: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    def size(data: dict[str, Any]) -> int:
        return len(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    envelope["truncated"] = False
    while size(envelope) > max_bytes and len(envelope.get("records", [])) > 1:
        envelope["records"].pop()
        envelope["truncated"] = True
    if size(envelope) > max_bytes:
        for record in envelope.get("records", []):
            record.pop("raw_payload_redacted", None)
            record["raw_payload_omitted"] = "envelope_size_limit"
        envelope["truncated"] = True
    return envelope


def _client_context() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "app": "medical-notes-workbench",
        "app_version": _app_version(),
    }


def _app_version() -> str:
    try:
        return importlib_metadata.version("medical-notes-workbench")
    except importlib_metadata.PackageNotFoundError:
        pass
    for path in _version_file_candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path.name == "package.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            version = data.get("version") if isinstance(data, dict) else None
            if version:
                return str(version)
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    return "unknown"


def _version_file_candidates() -> list[Path]:
    module_path = Path(__file__).resolve()
    roots: list[Path] = []
    for parent in module_path.parents:
        if parent.name in {"src", "gemini-cli-extension", "medical-notes-workbench"}:
            roots.append(parent.parent if parent.name == "src" else parent)
    roots.append(module_path.parents[2])
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in roots:
        for name in ("pyproject.toml", "package.json"):
            candidate = root / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _enqueue_envelope(envelope: dict[str, Any], *, root: str | Path | None = None) -> Path:
    outbox = _outbox_dir(root)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{envelope.get('generated_at', core.now_iso()).replace(':', '').replace('+', 'Z')}-{envelope['envelope_id']}.json"
    envelope = {**envelope, "queued_at": core.now_iso(), "attempts": int(envelope.get("attempts", 0))}
    core._atomic_write_json(path, envelope)
    return path


def _outbox_dir(root: str | Path | None = None) -> Path:
    return core.feedback_root(root) / "outbox"


def _sent_path(root: str | Path | None = None) -> Path:
    return core.feedback_root(root) / "telemetry-sent.json"


def _load_sent(*, root: str | Path | None = None) -> dict[str, Any]:
    path = _sent_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": TELEMETRY_SENT_SCHEMA, "sent_run_ids": []}
    if not isinstance(data, dict):
        return {"schema": TELEMETRY_SENT_SCHEMA, "sent_run_ids": []}
    data["schema"] = TELEMETRY_SENT_SCHEMA
    if not isinstance(data.get("sent_run_ids"), list):
        data["sent_run_ids"] = []
    return data


def _mark_sent(envelope: dict[str, Any], *, root: str | Path | None = None) -> None:
    sent = _load_sent(root=root)
    run_ids = {str(item) for item in sent.get("sent_run_ids", [])}
    for record in envelope.get("records", []):
        if isinstance(record, dict) and record.get("run_id"):
            run_ids.add(str(record["run_id"]))
    sent["sent_run_ids"] = sorted(run_ids)
    sent["updated_at"] = core.now_iso()
    path = _sent_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    core._atomic_write_json(path, sent)


def _bump_attempt(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["attempts"] = int(data.get("attempts", 0)) + 1
        data["last_attempt_at"] = core.now_iso()
        core._atomic_write_json(path, data)
    except Exception:
        pass


def _post_envelope(envelope: dict[str, Any], *, config: TelemetryConfig) -> None:
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(body) > config.max_envelope_bytes:
        raise ValueError("telemetry envelope exceeds max_envelope_bytes")
    headers = {
        "Authorization": f"Bearer {config.auth_token}",
        "Content-Type": "application/json",
        "X-MedNotes-Telemetry-Schema": TELEMETRY_ENVELOPE_SCHEMA,
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = client.post(config.endpoint_url, content=body, headers=headers)
        response.raise_for_status()


def _redact_endpoint(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"([?&](?:token|key|secret)=)[^&]+", r"\1[redacted]", url)
