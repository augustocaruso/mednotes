"""Bootstrap-safe doctor and setup helpers."""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.pdf import config as config_mod
from mednotes.domains.wiki.capabilities.pdf import paths

DOCTOR_SCHEMA = "medical-notes-workbench.pdf-library-doctor.v1"
SETUP_SCHEMA = "medical-notes-workbench.pdf-library-setup.v1"
REQUIRED_MODULES = {
    "fitz": "PyMuPDF",
    "pytesseract": "pytesseract",
    "textual": "textual",
}


def run_doctor(*, cfg: config_mod.PdfLibraryConfig | None = None, app_home: Path | None = None) -> dict[str, Any]:
    cfg = cfg or config_mod.load_pdf_library_config()
    missing = [dist for module, dist in REQUIRED_MODULES.items() if importlib.util.find_spec(module) is None]
    missing_paths = [str(path) for path in cfg.paths if not path.exists()]
    payload: dict[str, Any] = {
        "schema": DOCTOR_SCHEMA,
        "status": "ok",
        "phase": "doctor",
        "app_home": str(app_home or paths.app_home()),
        "configured_paths": [str(path) for path in cfg.paths],
        "missing_paths": missing_paths,
        "dependencies": {
            "missing": missing,
            "tesseract_binary": shutil.which("tesseract") or "",
            "platform": platform.platform(),
        },
        "third_party_notices": [
            "PyMuPDF is optional and distributed under AGPL/commercial terms.",
        ],
        "created_at": _now(),
    }
    blockers: list[str] = []
    if missing:
        blockers.append("pdf_library_dependencies_missing")
    if missing_paths:
        blockers.append("pdf_library_paths_missing")
    if blockers:
        payload.update(
            {
                "status": "blocked",
                "blocked_reason": blockers[0],
                "next_action": "/mednotes:pdf-library setup" if "pdf_library_dependencies_missing" in blockers else "fix configured PDF paths",
                "required_inputs": ["pdf-library optional dependencies"] if missing else ["valid PDF paths"],
            }
        )
    return payload


def setup_payload(*, dry_run: bool, app_home: Path | None = None, extension_path: Path | None = None) -> dict[str, Any]:
    root = app_home or paths.app_home()
    venv = root.parent / ".venv"
    project = extension_path or paths.extension_root()
    cmd = ["uv", "sync", "--project", str(project), "--extra", "pdf-library"]
    payload: dict[str, Any] = {
        "schema": SETUP_SCHEMA,
        "status": "dry_run" if dry_run else "running",
        "phase": "setup",
        "uv_project_environment": str(venv),
        "command": cmd,
        "third_party_notices": [
            "PyMuPDF is optional and distributed under AGPL/commercial terms.",
        ],
    }
    if dry_run:
        return payload
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)
    payload.update(
        {
            "status": "ok" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    )
    if proc.returncode != 0:
        payload.update(
            {
                "blocked_reason": "pdf_library_setup_failed",
                "next_action": "corrigir ambiente uv/Python e rodar /mednotes:setup",
            }
        )
    return payload


def _now() -> str:
    return datetime.now(UTC).isoformat()
