"""CLI for local workflow feedback records and improvement backlog."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from mednotes.platform.feedback.core import (
    BACKLOG_SCHEMA,
    build_backlog,
    feedback_root,
    record_workflow_run,
)
from mednotes.platform.feedback.integrity import check_extension_integrity
from mednotes.platform.feedback.telemetry import (
    DEFAULT_PAYLOAD_LEVEL,
    PAYLOAD_LEVELS,
    disable_telemetry,
    enable_telemetry,
    preview_envelope,
    send_telemetry,
    telemetry_status,
)


def _read_json(path: str | None) -> Any:
    if not path:
        return {}
    if path == "-":
        return json.loads(sys.stdin.read() or "{}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_record(args: argparse.Namespace) -> int:
    payload = _read_json(args.payload)
    if not isinstance(payload, dict):
        payload = {}
    if args.status:
        payload["status"] = args.status
    if args.phase:
        payload["phase"] = args.phase
    if args.blocked_reason:
        payload["blocked_reason"] = args.blocked_reason
    if args.next_action:
        payload["next_action"] = args.next_action
    if args.required_input:
        payload["required_inputs"] = args.required_input
    if args.human_decision_required:
        payload["human_decision_required"] = True
    if args.dry_run:
        payload["dry_run"] = True
    if args.apply:
        payload["apply"] = True
    record = record_workflow_run(
        workflow=args.workflow,
        command=args.command,
        payload=payload,
        exit_code=args.exit_code,
        started_at=time.time(),
        duration_ms=args.duration_ms,
        snippets=args.snippet,
        source="agent" if args.agent else "cli",
    )
    _json(record)
    return 0


def _cmd_backlog(args: argparse.Namespace) -> int:
    backlog = build_backlog(since=args.since)
    if args.format == "json":
        _json(backlog)
    else:
        print(format_backlog_markdown(backlog), end="")
    return 0


def _cmd_telemetry_enable(args: argparse.Namespace) -> int:
    _json(
        enable_telemetry(
            endpoint_url=args.endpoint,
            auth_token=args.token,
            payload_level=args.payload_level,
        )
    )
    return 0


def _cmd_telemetry_disable(args: argparse.Namespace) -> int:
    _json(disable_telemetry())
    return 0


def _cmd_telemetry_status(args: argparse.Namespace) -> int:
    _json(telemetry_status())
    return 0


def _cmd_telemetry_preview(args: argparse.Namespace) -> int:
    _json(preview_envelope(since=args.since, limit=args.limit))
    return 0


def _cmd_telemetry_send(args: argparse.Namespace) -> int:
    result = send_telemetry(since=args.since, limit=args.limit)
    _json(result)
    return 0 if result.get("ok") else 3


def _cmd_integrity_status(args: argparse.Namespace) -> int:
    status = check_extension_integrity(
        include_diff=args.include_diff,
        force=args.force,
        cache_dir=feedback_root() / "integrity",
    )
    if args.format == "json":
        _json(status)
    else:
        print(format_integrity_markdown(status), end="")
    return 0 if status.get("checked") or status.get("skipped_reason") in {"manifest_not_found"} else 4


def format_backlog_markdown(backlog: dict[str, Any]) -> str:
    lines = [
        "# Workflow Feedback Backlog",
        "",
        f"- Schema: `{BACKLOG_SCHEMA}`",
        f"- Desde: `{backlog.get('since')}`",
        f"- Runs analisados: {backlog.get('run_count', 0)}",
        f"- Itens: {len(backlog.get('items', []))}",
        "",
    ]
    items = backlog.get("items") if isinstance(backlog.get("items"), list) else []
    if not items:
        lines.append("Nenhum padrão recorrente encontrado.")
        return "\n".join(lines) + "\n"
    for item in items:
        lines.extend(
            [
                f"## {item['title']}",
                "",
                f"- Workflow: `{item['workflow']}`",
                f"- Sinal: `{item['signal']}`",
                f"- Severidade: `{item['severity']}`",
                f"- Ocorrências: {item['occurrence_count']}",
                f"- Tipo: `{item['improvement_type']}`",
                f"- Evidência: {item['evidence']}",
                f"- Recomendação: {item['recommendation']}",
                f"- Teste sugerido: {item['suggested_test']}",
                f"- Runs exemplo: {', '.join(item['sample_run_ids'])}",
                "",
            ]
        )
    return "\n".join(lines)


def format_integrity_markdown(status: dict[str, Any]) -> str:
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    lines = [
        "# Extension Integrity",
        "",
        f"- Checked: `{status.get('checked')}`",
        f"- Drift detected: `{status.get('drift_detected')}`",
        f"- App version: `{status.get('app_version', 'unknown')}`",
        f"- Manifest: `{status.get('manifest_path') or '(not found)'}`",
        f"- Changed: {summary.get('changed_count', 0)}",
        f"- Modified: {summary.get('modified_count', 0)}",
        f"- Missing: {summary.get('missing_count', 0)}",
        f"- Unexpected: {summary.get('unexpected_count', 0)}",
        f"- Line-ending only: {summary.get('line_ending_only_count', 0)}",
    ]
    if status.get("skipped_reason"):
        lines.append(f"- Skipped reason: `{status.get('skipped_reason')}`")
    for label, key in (("Modified files", "modified_files"), ("Missing files", "missing_files"), ("Unexpected files", "unexpected_files")):
        files = status.get(key) if isinstance(status.get(key), list) else []
        if not files:
            continue
        lines.extend(["", f"## {label}"])
        for item in files[:8]:
            lines.append(f"- `{item.get('path')}` ({item.get('kind')})")
    samples = status.get("diff_samples") if isinstance(status.get("diff_samples"), list) else []
    if samples:
        lines.extend(["", "## Diff samples"])
        for sample in samples[:3]:
            body = sample.get("sample") or sample.get("current_excerpt") or sample.get("diff_unavailable_reason") or ""
            lines.extend([f"- `{sample.get('path')}`", "", "```", str(body), "```"])
    extension_diffs = status.get("extension_diffs") if isinstance(status.get("extension_diffs"), list) else []
    if extension_diffs:
        lines.extend(["", "## Extension diffs"])
        for diff in extension_diffs[:3]:
            body = diff.get("patch") or diff.get("full_diff_unavailable_reason") or ""
            lines.extend([f"- `{diff.get('path')}` ({diff.get('change')})", "", "```diff", str(body), "```"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record an agent-driven workflow result")
    record.add_argument("--workflow", required=True)
    record.add_argument("--command", default="")
    record.add_argument("--payload", help="JSON file, or '-' for stdin")
    record.add_argument("--status")
    record.add_argument("--phase")
    record.add_argument("--blocked-reason")
    record.add_argument("--next-action")
    record.add_argument("--required-input", action="append", default=[])
    record.add_argument("--human-decision-required", action="store_true")
    record.add_argument("--dry-run", action="store_true")
    record.add_argument("--apply", action="store_true")
    record.add_argument("--exit-code", type=int, default=0)
    record.add_argument("--duration-ms", type=int, default=0)
    record.add_argument("--snippet", action="append", default=[])
    record.add_argument("--agent", action="store_true", help="mark source as agent-driven")
    record.set_defaults(func=_cmd_record)

    backlog = sub.add_parser("backlog", help="build an actionable improvement backlog")
    backlog.add_argument("--since", default="30d", help="Window such as 30d, 12h or an ISO timestamp.")
    backlog.add_argument("--format", choices=("markdown", "json"), default="markdown")
    backlog.set_defaults(func=_cmd_backlog)

    integrity = sub.add_parser("integrity", help="inspect extension prompt/script drift")
    integrity_sub = integrity.add_subparsers(dest="integrity_command", required=True)
    integrity_status = integrity_sub.add_parser("status", help="compare the installed extension with its manifest")
    integrity_status.add_argument("--format", choices=("markdown", "json"), default="json")
    integrity_status.add_argument("--include-diff", action="store_true", help="include short redacted diff samples")
    integrity_status.add_argument("--force", action="store_true", help="ignore throttle/cache and scan now")
    integrity_status.set_defaults(func=_cmd_integrity_status)

    telemetry = sub.add_parser("telemetry", help="manage remote telemetry, status and opt-out")
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_command", required=True)

    enable = telemetry_sub.add_parser("enable", help="manually enable or override telemetry endpoint")
    enable.add_argument("--endpoint", required=True, help="HTTPS endpoint that receives telemetry envelopes")
    enable.add_argument("--token", required=True, help="Bearer token expected by the telemetry endpoint")
    enable.add_argument("--payload-level", choices=sorted(PAYLOAD_LEVELS), default=DEFAULT_PAYLOAD_LEVEL)
    enable.set_defaults(func=_cmd_telemetry_enable)

    disable = telemetry_sub.add_parser("disable", help="disable remote telemetry")
    disable.set_defaults(func=_cmd_telemetry_disable)

    status = telemetry_sub.add_parser("status", help="show telemetry config and outbox state")
    status.set_defaults(func=_cmd_telemetry_status)

    preview = telemetry_sub.add_parser("preview", help="show the next telemetry envelope without sending it")
    preview.add_argument("--since", default="30d")
    preview.add_argument("--limit", type=int, default=20)
    preview.set_defaults(func=_cmd_telemetry_preview)

    send = telemetry_sub.add_parser("send", help="send unsent telemetry records and retry outbox")
    send.add_argument("--since", default="30d")
    send.add_argument("--limit", type=int, default=20)
    send.set_defaults(func=_cmd_telemetry_send)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
