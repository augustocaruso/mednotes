#!/usr/bin/env python3
"""Guided maintainer setup for email-based workflow telemetry."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION_ROOT = ROOT / "extension" if (ROOT / "extension").is_dir() else ROOT
EXAMPLE_DIR = DISTRIBUTION_ROOT / "examples" / "telemetry-email-worker"
DEFAULT_HOME = Path.home() / ".gemini" / "medical-notes-workbench"
DEFAULT_WORKER_NAME = "medical-notes-workbench-telemetry"
DEFAULT_PAYLOAD_LEVEL = "trusted_extension_debug"
PAYLOAD_LEVELS = {"diagnostic_redacted", "full_logs", "trusted_extension_debug"}
REMOTE_TELEMETRY_SETUP_DISABLED = True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = setup_receiver(args)
    except SetupError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "next_action": exc.next_action}, ensure_ascii=False, indent=2))
        return 2

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_text_result(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Configure Cloudflare Worker + Resend so workflow telemetry arrives "
            "as actionable emails."
        )
    )
    parser.add_argument("--to-email", help="Email that receives telemetry reports.")
    parser.add_argument("--from-email", help="Verified Resend sender, for example telemetry@your-domain.com.")
    parser.add_argument("--resend-api-key", help="Resend API key. Omit to type it securely.")
    parser.add_argument("--ingest-token", help="Shared ingest token. Omit to generate a strong token.")
    parser.add_argument("--worker-name", default=DEFAULT_WORKER_NAME, help=f"Cloudflare Worker name. Default: {DEFAULT_WORKER_NAME}")
    parser.add_argument("--payload-level", choices=sorted(PAYLOAD_LEVELS), default=DEFAULT_PAYLOAD_LEVEL)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Local setup/receipt directory.")
    parser.add_argument("--activate-local", action="store_true", help="Enable telemetry for this local checkout after deploy.")
    parser.add_argument(
        "--no-distribution-defaults",
        action="store_true",
        help="Compatibility no-op: distribution telemetry defaults are disabled for this project.",
    )
    parser.add_argument("--skip-test-email", action="store_true", help="Do not send a test telemetry email after deploy.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare files and print commands without calling wrangler.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def setup_receiver(args: argparse.Namespace) -> dict[str, Any]:
    if REMOTE_TELEMETRY_SETUP_DISABLED:
        raise SetupError(
            "remote telemetry setup is disabled for this project",
            "Use controlled experiment reports and local workflow feedback instead of email telemetry.",
        )
    if not EXAMPLE_DIR.exists():
        raise SetupError("worker template not found", "Check examples/telemetry-email-worker in this checkout.")
    to_email = args.to_email or _prompt("Email que vai receber os reports")
    from_email = args.from_email or _prompt("Remetente verificado no Resend")
    resend_api_key = args.resend_api_key or os.getenv("RESEND_API_KEY") or getpass.getpass("Resend API key: ").strip()
    ingest_token = args.ingest_token or secrets.token_urlsafe(32)
    if not _looks_like_email(to_email):
        raise SetupError("--to-email does not look like an email", "Pass a valid email address.")
    if not _looks_like_sender(from_email):
        raise SetupError("--from-email does not look valid", "Use a verified Resend sender such as telemetry@your-domain.com.")
    if not resend_api_key:
        raise SetupError("missing Resend API key", "Create a Resend API key and pass it through the prompt or --resend-api-key.")
    if not ingest_token:
        raise SetupError("missing ingest token", "Omit --ingest-token to generate one automatically.")

    work_dir = args.home.expanduser() / "telemetry-email-worker"
    receipt_path = args.home.expanduser() / "telemetry-receiver.json"
    _prepare_worker_dir(work_dir, args.worker_name)

    endpoint_url = ""
    kv_namespace = ""
    deploy_output = ""
    if args.dry_run:
        endpoint_url = f"https://{args.worker_name}.<your-workers-subdomain>.workers.dev/v1/telemetry/workflow-runs"
    else:
        _require_command("npm", "Install Node.js/npm or run this from an environment where npm is available.")
        kv_namespace = _configure_digest_kv(work_dir)
        _put_secret(work_dir, "INGEST_TOKEN", ingest_token)
        _put_secret(work_dir, "RESEND_API_KEY", resend_api_key)
        _put_secret(work_dir, "TO_EMAIL", to_email)
        _put_secret(work_dir, "FROM_EMAIL", from_email)
        deploy = _run(["npm", "exec", "--yes", "wrangler", "deploy"], cwd=work_dir)
        deploy_output = deploy.stdout + deploy.stderr
        endpoint_url = _extract_worker_url(deploy_output)
        if not endpoint_url:
            raise SetupError(
                "could not detect Worker URL from wrangler deploy output",
                "Open the Cloudflare Workers dashboard, copy the worker URL, then run telemetry enable with that endpoint.",
            )

    if endpoint_url and not endpoint_url.endswith("/v1/telemetry/workflow-runs"):
        endpoint_url = endpoint_url.rstrip("/") + "/v1/telemetry/workflow-runs"

    enable_command = _enable_command(endpoint_url=endpoint_url, token=ingest_token, payload_level=args.payload_level)
    defaults_path = DISTRIBUTION_ROOT / ".telemetry-defaults.json"
    result = {
        "ok": True,
        "worker_dir": str(work_dir),
        "receipt_path": str(receipt_path),
        "distribution_defaults_path": str(defaults_path),
        "endpoint_url": endpoint_url,
        "to_email": to_email,
        "from_email": from_email,
        "worker_name": args.worker_name,
        "payload_level": args.payload_level,
        "user_enable_command": enable_command,
        "dry_run": bool(args.dry_run),
        "digest_window_minutes": 60,
        "digest_min_interval_minutes": 60,
        "deploy_output_excerpt": deploy_output[-1200:] if deploy_output else "",
    }
    if not args.dry_run:
        result["kv_namespace"] = kv_namespace

    if not args.dry_run:
        if not args.skip_test_email:
            result["test_email"] = _send_test_email(endpoint_url=endpoint_url, token=ingest_token)
        receipt = {**result, "ingest_token": ingest_token}
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            receipt_path.chmod(0o600)
        except OSError:
            pass
        if not args.no_distribution_defaults:
            _write_distribution_defaults(
                defaults_path,
                endpoint_url=endpoint_url,
                token=ingest_token,
                payload_level=args.payload_level,
            )

    if args.activate_local and not args.dry_run:
        activation = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "mednotes" / "feedback_report.py"),
                "telemetry",
                "enable",
                "--endpoint",
                endpoint_url,
                "--token",
                ingest_token,
                "--payload-level",
                args.payload_level,
            ],
            cwd=ROOT,
        )
        result["local_activation"] = _try_json(activation.stdout) or {"stdout": activation.stdout.strip()}

    return result


def _prepare_worker_dir(work_dir: Path, worker_name: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EXAMPLE_DIR / "worker.js", work_dir / "worker.js")
    wrangler = (EXAMPLE_DIR / "wrangler.toml.example").read_text(encoding="utf-8")
    wrangler = re.sub(r'^name = ".*"$', f'name = "{worker_name}"', wrangler, flags=re.M)
    (work_dir / "wrangler.toml").write_text(wrangler, encoding="utf-8")


def _configure_digest_kv(work_dir: Path) -> dict[str, Any]:
    try:
        created = _run(["npm", "exec", "--yes", "wrangler", "kv", "namespace", "create", "TELEMETRY_BUFFER"], cwd=work_dir)
        created_preview = _run(
            ["npm", "exec", "--yes", "wrangler", "kv", "namespace", "create", "TELEMETRY_BUFFER", "--preview"],
            cwd=work_dir,
        )
        namespace_id = _extract_kv_id(created.stdout + created.stderr)
        preview_id = _extract_kv_id(created_preview.stdout + created_preview.stderr) or namespace_id
        if not namespace_id:
            raise SetupError(
                "could not detect KV namespace id",
                "Create a KV namespace manually, update wrangler.toml, then run wrangler deploy.",
            )
        _patch_kv_ids(work_dir / "wrangler.toml", namespace_id=namespace_id, preview_id=preview_id)
        return {"ok": True, "id": namespace_id, "preview_id": preview_id}
    except SetupError as exc:
        raise SetupError(
            "could not configure telemetry digest KV",
            (
                f"{exc.next_action} Sem KV o Worker cairia para email imediato por envelope, "
                "o que pode estourar a quota do Resend."
            ),
        ) from exc


def _extract_kv_id(output: str) -> str:
    match = re.search(r'id\s*=\s*"([^"]+)"', output)
    if match:
        return match.group(1)
    match = re.search(r"\b([0-9a-f]{32})\b", output, flags=re.I)
    return match.group(1) if match else ""


def _patch_kv_ids(path: Path, *, namespace_id: str, preview_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_KV_NAMESPACE_ID", namespace_id)
    text = text.replace("REPLACE_WITH_PREVIEW_KV_NAMESPACE_ID", preview_id)
    path.write_text(text, encoding="utf-8")


def _remove_kv_binding(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"\n# Optional but recommended for digest emails\..*?\[\[kv_namespaces\]\]\n.*?(?=\n# Secrets|\Z)",
        "\n",
        text,
        flags=re.S,
    )
    path.write_text(text, encoding="utf-8")


def _put_secret(cwd: Path, name: str, value: str) -> None:
    _run(["npm", "exec", "--yes", "wrangler", "secret", "put", name], cwd=cwd, input_text=value + "\n")


def _run(command: list[str], *, cwd: Path, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SetupError(
            f"command failed: {' '.join(command)}",
            detail[-1200:] or "Run `npm exec --yes wrangler login` and try again.",
        )
    return result


def _require_command(command: str, next_action: str) -> None:
    if shutil.which(command) is None:
        raise SetupError(f"missing command: {command}", next_action)


def _extract_worker_url(output: str) -> str:
    matches = re.findall(r"https://[^\s]+?\.workers\.dev(?:/[^\s]*)?", output)
    return matches[-1].rstrip(".,") if matches else ""


def _send_test_email(*, endpoint_url: str, token: str) -> dict[str, Any]:
    envelope = {
        "schema": "medical-notes-workbench.workflow-telemetry-envelope.v1",
        "envelope_id": f"setup-test-{secrets.token_hex(8)}",
        "generated_at": "setup-test",
        "install_id": "setup-test",
        "payload_level": DEFAULT_PAYLOAD_LEVEL,
        "client": {
            "app": "medical-notes-workbench",
            "source": "setup_telemetry_email.py",
        },
        "records": [
            {
                "run_id": "setup-test",
                "workflow": "/mednotes:telemetry",
                "status": "completed",
                "phase": "setup-email",
                "blocked_reason": None,
                "next_action": "Telemetry receiver is configured. Ignore this setup test.",
                "payload_summary": {
                    "counts": {"test_records": 1},
                    "warnings": [],
                },
                "diagnostic_snippets": ["setup email delivery test"],
            }
        ],
        "limits": {"max_envelope_bytes": 1048576 if DEFAULT_PAYLOAD_LEVEL == "trusted_extension_debug" else 262144},
        "truncated": False,
    }
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "response": _try_json(response_body) or response_body[:500]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SetupError(
            "test telemetry email failed",
            f"HTTP {exc.code}: {detail[:800]}. Check Resend sender/domain, RESEND_API_KEY, TO_EMAIL and FROM_EMAIL.",
        ) from exc
    except OSError as exc:
        raise SetupError(
            "could not reach telemetry Worker for test email",
            f"{exc}. Check network access and the Worker endpoint.",
        ) from exc


def _enable_command(*, endpoint_url: str, token: str, payload_level: str) -> str:
    return (
        "uv run python scripts/mednotes/feedback_report.py telemetry enable "
        f'--endpoint "{endpoint_url}" '
        f'--token "{token}" '
        f"--payload-level {payload_level}"
    )


def _write_distribution_defaults(path: Path, *, endpoint_url: str, token: str, payload_level: str) -> None:
    payload = {
        "schema": "medical-notes-workbench.telemetry-defaults.v1",
        "enabled": True,
        "endpoint_url": endpoint_url,
        "auth_token": token,
        "payload_level": payload_level,
        "max_envelope_bytes": 1048576 if payload_level == "trusted_extension_debug" else 262144,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _render_text_result(result: dict[str, Any]) -> str:
    lines = [
        "Telemetria por email configurada.",
        "",
        f"Endpoint: {result['endpoint_url']}",
        f"Reports chegam em: {result['to_email']}",
        f"Remetente: {result['from_email']}",
        f"Recibo local: {result['receipt_path']}",
        f"Defaults para build privado: {result['distribution_defaults_path']}",
        "",
        "Comando manual de override, caso algum usuário precise reativar:",
        "",
        result["user_enable_command"],
    ]
    if result.get("dry_run"):
        lines.insert(1, "DRY RUN: nada foi enviado ao Cloudflare.")
    if result.get("local_activation"):
        lines.extend(["", "Esta instalação local já foi ativada."])
    if not result.get("dry_run"):
        lines.extend(["", "Builds distribuídos não autoativam telemetria neste projeto."])
    return "\n".join(lines)


def _prompt(label: str) -> str:
    return input(f"{label}: ").strip()


def _looks_like_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))


def _looks_like_sender(value: str) -> bool:
    match = re.search(r"<([^>]+)>", value)
    email = match.group(1) if match else value
    return _looks_like_email(email.strip())


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


class SetupError(RuntimeError):
    def __init__(self, message: str, next_action: str) -> None:
        super().__init__(message)
        self.next_action = next_action


if __name__ == "__main__":
    raise SystemExit(main())
