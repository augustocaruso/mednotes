#!/usr/bin/env python3
"""Capture a recoverable diff from an installed Medical Notes Workbench extension.

This script is intentionally self-contained. It is meant for the uncomfortable
case where a user already updated the Gemini CLI extension and we still need a
best-effort diff of local drift against the extension integrity manifest.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit, urlunsplit

MANIFEST_FILENAME = "extension-integrity-manifest.json"
INTEGRITY_MANIFEST_SCHEMA = "medical-notes-workbench.extension-integrity-manifest.v1"
PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA = "medical-notes-workbench.pre-update-extension-snapshot.v1"
TELEMETRY_ENVELOPE_SCHEMA = "medical-notes-workbench.workflow-telemetry-envelope.v1"
RUN_RECORD_SCHEMA = "medical-notes-workbench.workflow-run-record.v1"
MANUAL_REPORT_RECEIPT_SCHEMA = "medical-notes-workbench.manual-report-receipt.v1"
PAYLOAD_LEVEL = "trusted_extension_debug"

MAX_PATCH_CHARS = 160 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_GIT_HISTORY_COMMITS = 600
MAX_TEXT_BYTES = 768 * 1024
MAX_ZIP_FILE_BYTES = 6 * 1024 * 1024
DEFAULT_GITHUB_BASELINE_URL = "https://codeload.github.com/augustocaruso/medical-notes-workbench/zip/refs/heads/gemini-cli-extension"
DEFAULT_PRE_UPDATE_SNAPSHOT_MAX_DIRS = 5
DEFAULT_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS = 7

MONITORED_EXACT_FILES = {
    "GEMINI.md",
    "gemini-extension.json",
    "package.json",
    "pyproject.toml",
}
MONITORED_DIRS = {
    "agents",
    "commands",
    "docs",
    "hooks",
    "mcp",
    "policies",
    "scripts",
    "skills",
    "src",
}
MONITORED_SUFFIXES = {
    ".cjs",
    ".cmd",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd"}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "cache",
    "dist",
    "feedback",
    "node_modules",
    "outbox",
}
EXCLUDED_NAMES = {
    ".DS_Store",
    ".env",
    ".telemetry-defaults.json",
    MANIFEST_FILENAME,
    "extension-integrity-cache.json",
    "telemetry.defaults.json",
    "uv.lock",
}
PRE_UPDATE_PATCH_NOISE_PARTS = tuple(f"{part}/" for part in EXCLUDED_PARTS) + (".pyc", ".pyo", ".egg-info/")


@dataclass(frozen=True)
class TelemetrySettings:
    endpoint_url: str = ""
    auth_token: str = ""
    install_id: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.endpoint_url and self.auth_token and self.install_id)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def capture_extension_diff(
    extension_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    send: bool = False,
    endpoint_url: str = "",
    auth_token: str = "",
    config_path: str | Path | None = None,
    flush_digest: bool = False,
    include_existing_snapshots: bool = False,
    github_baseline_url: str = "",
) -> dict[str, Any]:
    """Capture extension drift and optionally send a trusted debug envelope."""
    root = Path(os.path.expandvars(str(extension_path))).expanduser().resolve()
    snapshot_id = _snapshot_id()
    snapshot_path = _default_snapshot_dir(snapshot_id) if output_dir is None else Path(output_dir).expanduser().resolve()
    snapshot_path.mkdir(parents=True, exist_ok=True)

    manifest_path = root / MANIFEST_FILENAME
    manifest = _read_manifest(manifest_path)
    expected = _manifest_files(manifest)
    current = _current_file_states(root)
    existing_snapshot_diffs, existing_snapshot_summaries = (
        _load_existing_pre_update_snapshot_diffs(exclude_dir=snapshot_path) if include_existing_snapshots else ([], [])
    )
    extension_diffs: list[dict[str, Any]] = []
    combined_patches: list[str] = []
    changed_paths: list[str] = []
    generated_scripts: list[dict[str, Any]] = []
    modified_files: list[dict[str, Any]] = []
    missing_files: list[dict[str, Any]] = []
    unexpected_files: list[dict[str, Any]] = []
    line_ending_only_files: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    git_diff_empty_count = 0
    baseline_recovered_count = 0
    github_baseline_diffs: list[dict[str, Any]] = []

    has_git = _has_git_worktree(root)
    for rel, item in sorted(expected.items()):
        if not _allowed_full_diff_path(rel):
            continue
        path = root / rel
        current_state = current.get(rel)
        if not path.exists() or not current_state:
            entry = _changed_file_entry(rel, item, None, change="missing")
            missing_files.append(entry)
            changed_paths.append(rel)
            patch, source, reason, used_git_empty = _patch_for_expected_file(root, rel, item, has_git=has_git, missing=True)
        elif current_state["sha256"] == str(item.get("sha256") or ""):
            continue
        elif _same_normalized(current_state, item):
            line_ending_only_files.append(_changed_file_entry(rel, item, current_state, change="line_ending_only"))
            continue
        else:
            entry = _changed_file_entry(rel, item, current_state, change="modified")
            modified_files.append(entry)
            changed_paths.append(rel)
            patch, source, reason, used_git_empty = _patch_for_expected_file(root, rel, item, has_git=has_git, missing=False)

        if used_git_empty:
            git_diff_empty_count += 1
        if source.startswith("git:") and source != "git:diff":
            baseline_recovered_count += 1
        diff_entry: dict[str, Any] = {
            "path": rel,
            "kind": _file_kind(rel),
            "change": "missing" if not path.exists() else "modified",
            "baseline_source": source,
        }
        if patch:
            sanitized = redact_operational_text(patch, max_chars=MAX_PATCH_CHARS)
            diff_entry["patch"] = sanitized
            diff_entry["truncated"] = len(sanitized) < len(patch)
            combined_patches.append(sanitized)
        else:
            diff_entry["full_diff_unavailable_reason"] = reason or "full_diff_unavailable"
            unavailable.append({"path": rel, "reason": diff_entry["full_diff_unavailable_reason"]})
        extension_diffs.append(diff_entry)

    for rel, state in sorted(current.items()):
        if rel in expected or not _allowed_full_diff_path(rel):
            continue
        unexpected_files.append(_changed_file_entry(rel, None, state, change="unexpected"))
        changed_paths.append(rel)
        patch = _new_file_patch(root, rel)
        diff_entry: dict[str, Any] = {
            "path": rel,
            "kind": _file_kind(rel),
            "change": "unexpected",
            "baseline_source": "new-file",
        }
        if patch:
            sanitized = redact_operational_text(patch, max_chars=MAX_PATCH_CHARS)
            diff_entry["patch"] = sanitized
            diff_entry["truncated"] = len(sanitized) < len(patch)
            combined_patches.append(sanitized)
        else:
            diff_entry["full_diff_unavailable_reason"] = "new_file_patch_unavailable"
            unavailable.append({"path": rel, "reason": "new_file_patch_unavailable"})
        extension_diffs.append(diff_entry)
        script = _generated_script(root, rel, state, source="unexpected_extension_file")
        if script:
            generated_scripts.append(script)

    if existing_snapshot_diffs:
        extension_diffs = existing_snapshot_diffs + extension_diffs
        for item in existing_snapshot_diffs:
            patch = str(item.get("patch") or "")
            if patch.strip():
                combined_patches.insert(0, patch)

    github_baseline: dict[str, Any] = {}
    if github_baseline_url:
        github_baseline, github_baseline_diffs = _capture_github_baseline_diffs(
            root,
            snapshot_path=snapshot_path,
            source_url=github_baseline_url,
        )
        extension_diffs = github_baseline_diffs + extension_diffs
        for item in github_baseline_diffs:
            rel = str(item.get("path") or "")
            if rel and rel not in changed_paths:
                changed_paths.append(rel)
            patch = str(item.get("patch") or "")
            if patch.strip():
                combined_patches.append(patch)
            if item.get("change") == "unexpected":
                state = current.get(rel)
                script = _generated_script(root, rel, state or {}, source="github_baseline_unexpected_file")
                if script:
                    generated_scripts.append(script)

    combined_patch = "\n\n".join(patch for patch in combined_patches if patch.strip())
    git_head = _git_stdout(root, "rev-parse", "HEAD") if has_git else ""
    manifest_version = str(manifest.get("app_version") or "")
    summary = {
        "modified_count": len(modified_files),
        "missing_count": len(missing_files),
        "unexpected_count": len(unexpected_files),
        "line_ending_only_count": len(line_ending_only_files),
        "changed_count": len(modified_files) + len(missing_files) + len(unexpected_files),
        "manifest_file_count": len(expected),
        "extension_diff_count": len(extension_diffs),
        "github_baseline_diff_count": len(github_baseline_diffs),
        "rescued_pre_update_diff_count": len(existing_snapshot_diffs),
        "existing_pre_update_snapshot_count": len(existing_snapshot_summaries),
        "generated_script_count": len(generated_scripts),
    }
    snapshot = {
        "schema": PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "recorded_at": now_iso(),
        "extension_name": "medical-notes-workbench",
        "extension_path": str(root),
        "snapshot_path": str(snapshot_path),
        "current_version": manifest_version,
        "target_version": "",
        "git_head": git_head,
        "git_available": has_git,
        "reason": "manual-extension-diff-rescue",
        "patch_id": "manual-extension-diff-rescue",
        "phase": "manual-extension-diff-capture",
        "changed_path_count": len(modified_files) + len(missing_files),
        "untracked_path_count": len(unexpected_files),
        "changed_paths": changed_paths[:200],
        "generated_scripts": generated_scripts,
        "summary": summary,
        "github_baseline": github_baseline,
        "modified_files": modified_files,
        "missing_files": missing_files,
        "unexpected_files": unexpected_files,
        "line_ending_only_files": line_ending_only_files,
        "extension_diffs": extension_diffs,
        "existing_pre_update_snapshots": existing_snapshot_summaries,
        "combined_patch": combined_patch,
        "git_diff_empty_count": git_diff_empty_count,
        "baseline_recovered_count": baseline_recovered_count,
        "diff_unavailable": unavailable,
    }
    snapshot["telemetry_evidence"] = _telemetry_evidence_from_snapshot(snapshot, send_path="manual_extension_diff_capture")

    _write_snapshot_files(snapshot_path, snapshot, combined_patch, unavailable)
    _write_json(snapshot_path / "existing-pre-update-snapshots.json", existing_snapshot_summaries)
    envelope = _build_envelope(snapshot, endpoint_url=endpoint_url, auth_token=auth_token, config_path=config_path)
    _write_json(snapshot_path / "telemetry-envelope.json", envelope)
    send_result = {"ok": False, "sent": False, "reason": "send_not_requested"}
    if send:
        send_result = _send_envelope(envelope, endpoint_url=endpoint_url, auth_token=auth_token, config_path=config_path)
        if flush_digest and send_result.get("ok"):
            send_result["digest_flush"] = _flush_digest(send_result.get("endpoint_url", ""), send_result.get("auth_token", ""))
    redacted_send_result = _redacted_send_result(send_result)
    _write_json(snapshot_path / "send-result.json", redacted_send_result)
    snapshot["send_result"] = redacted_send_result
    snapshot["manual_report_receipt"] = _manual_report_receipt(snapshot, redacted_send_result)
    _write_json(snapshot_path / "manual-report-receipt.json", snapshot["manual_report_receipt"])
    _write_json(snapshot_path / "capture-result.json", _public_capture_result(snapshot))
    zip_path = _write_zip(snapshot_path)
    snapshot["zip_path"] = str(zip_path) if zip_path else ""
    retention_result = _prune_pre_update_snapshots_for_path(snapshot_path)
    if retention_result:
        snapshot["local_retention"] = retention_result
    _write_json(snapshot_path / "capture-result.json", _public_capture_result(snapshot))
    return snapshot


def _snapshot_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-manual-rescue-{uuid.uuid4().hex[:8]}"


def _default_snapshot_dir(snapshot_id: str) -> Path:
    return _pre_update_snapshot_root() / snapshot_id


def _pre_update_snapshot_root() -> Path:
    return Path.home() / ".gemini" / "medical-notes-workbench" / "feedback" / "pre-update-snapshots"


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": INTEGRITY_MANIFEST_SCHEMA, "files": []}
    if not isinstance(data, dict):
        return {"schema": INTEGRITY_MANIFEST_SCHEMA, "files": []}
    return data


def _manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        return values
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = _clean_rel(item.get("path"))
        if rel and _allowed_full_diff_path(rel):
            values[rel] = dict(item, path=rel)
    return values


def _current_file_states(root: Path) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for path in _iter_public_files(root):
        rel = _relative_path(path, root)
        states[rel] = _file_state(path, root)
    return states


def _capture_github_baseline_diffs(root: Path, *, snapshot_path: Path, source_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    baseline_dir = snapshot_path / "github-baseline"
    archive_path = snapshot_path / "github-baseline.zip"
    metadata: dict[str, Any] = {
        "source_url": source_url,
        "archive_path": str(archive_path),
        "baseline_root": "",
        "ok": False,
        "diff_count": 0,
    }
    try:
        _download_baseline_archive(source_url, archive_path)
        if baseline_dir.exists():
            shutil.rmtree(baseline_dir)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(baseline_dir)
        baseline_root = _find_baseline_root(baseline_dir)
        metadata["baseline_root"] = str(baseline_root)
        current = _current_file_states(root)
        baseline = _current_file_states(baseline_root)
        diffs: list[dict[str, Any]] = []
        for rel in sorted(set(current) | set(baseline)):
            if not _allowed_full_diff_path(rel):
                continue
            current_path = root / rel
            baseline_path = baseline_root / rel
            current_exists = current_path.exists()
            baseline_exists = baseline_path.exists()
            if current_exists and baseline_exists:
                current_data = _read_bytes(current_path)
                baseline_data = _read_bytes(baseline_path)
                if _hash_bytes(_normalize_line_endings(current_data)) == _hash_bytes(_normalize_line_endings(baseline_data)):
                    continue
                change = "modified"
            elif current_exists:
                current_data = _read_bytes(current_path)
                baseline_data = b""
                change = "unexpected"
            else:
                current_data = b""
                baseline_data = _read_bytes(baseline_path)
                change = "missing"
            patch = _unified_diff_bytes(baseline_data, current_data, fromfile=f"github/{rel}", tofile=f"current/{rel}")
            entry: dict[str, Any] = {
                "path": rel,
                "kind": _file_kind(rel),
                "change": change,
                "baseline_source": "github-baseline",
            }
            if patch:
                sanitized = redact_operational_text(patch, max_chars=MAX_PATCH_CHARS)
                entry["patch"] = sanitized
                entry["truncated"] = len(sanitized) < len(patch)
            else:
                entry["full_diff_unavailable_reason"] = "github_baseline_diff_unavailable"
            diffs.append(entry)
        metadata["ok"] = True
        metadata["diff_count"] = len(diffs)
        return metadata, diffs
    except Exception as exc:
        metadata["error"] = redact_operational_text(str(exc), max_chars=2000)
        return metadata, []


def _download_baseline_archive(source_url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if source_url.startswith("file://"):
        with urllib_request.urlopen(source_url, timeout=8) as response:
            output.write_bytes(response.read(MAX_ZIP_FILE_BYTES * 2))
        return
    source_path = Path(os.path.expandvars(source_url)).expanduser()
    if source_path.exists():
        shutil.copy2(source_path, output)
        return
    request = urllib_request.Request(source_url, headers={"User-Agent": "medical-notes-workbench"})
    with urllib_request.urlopen(request, timeout=15) as response:
        output.write_bytes(response.read(MAX_ZIP_FILE_BYTES * 2))


def _find_baseline_root(extracted_dir: Path) -> Path:
    candidates = [path for path in extracted_dir.iterdir() if path.is_dir()]
    for candidate in candidates:
        if (candidate / "gemini-extension.json").exists() or (candidate / "GEMINI.md").exists():
            return candidate
    if len(candidates) == 1:
        return candidates[0]
    return extracted_dir


def _iter_public_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = _relative_path(path, root)
        if _is_public_file(rel):
            paths.append(path)
    return paths


def _is_public_file(rel: str) -> bool:
    rel = _clean_rel(rel)
    if not rel:
        return False
    parts = rel.split("/")
    if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in parts):
        return False
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return False
    if rel in MONITORED_EXACT_FILES:
        return True
    if parts[0] not in MONITORED_DIRS:
        return False
    return Path(rel).suffix.lower() in MONITORED_SUFFIXES


def _allowed_full_diff_path(rel: str) -> bool:
    rel = _clean_rel(rel)
    if not _is_public_file(rel):
        return False
    if rel in MONITORED_EXACT_FILES:
        return True
    first = rel.split("/", 1)[0]
    return first in MONITORED_DIRS


def _clean_rel(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    if not text or text.startswith("../") or "/../" in f"/{text}/":
        return ""
    return text


def _relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _file_state(path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = _decode_text(data)
    return {
        "path": _relative_path(path, root),
        "kind": _file_kind(_relative_path(path, root)),
        "sha256": _hash_bytes(data),
        "normalized_sha256": _hash_bytes(_normalize_line_endings(data)),
        "size_bytes": len(data),
        "line_count": len(text.splitlines()) if text is not None else 0,
    }


def _changed_file_entry(
    rel: str,
    expected: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    change: str,
) -> dict[str, Any]:
    return {
        "path": rel,
        "kind": _file_kind(rel),
        "change": change,
        "expected_sha256": str((expected or {}).get("sha256") or ""),
        "current_sha256": str((current or {}).get("sha256") or ""),
        "expected_size_bytes": _safe_int((expected or {}).get("size_bytes")),
        "current_size_bytes": _safe_int((current or {}).get("size_bytes")),
    }


def _same_normalized(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    current_hash = str(current.get("normalized_sha256") or "")
    expected_hash = str(expected.get("normalized_sha256") or "")
    return bool(current_hash and expected_hash and current_hash == expected_hash)


def _patch_for_expected_file(
    root: Path,
    rel: str,
    expected: dict[str, Any],
    *,
    has_git: bool,
    missing: bool,
) -> tuple[str, str, str, bool]:
    used_git_empty = False
    if has_git:
        patch = _git_diff(root, rel)
        if patch:
            return patch, "git:diff", "", False
        used_git_empty = True
    baseline, baseline_source = _recover_baseline_from_git(root, rel, expected) if has_git else (None, "")
    if baseline is not None:
        current = b"" if missing else _read_bytes(root / rel)
        patch = _unified_diff_bytes(baseline, current, fromfile=f"manifest/{rel}", tofile=f"current/{rel}")
        return patch, baseline_source, "", used_git_empty
    if not has_git:
        return "", "", "git_repository_not_available", used_git_empty
    return "", "", "git_diff_empty_and_manifest_baseline_not_found", used_git_empty


def _git_diff(root: Path, rel: str) -> str:
    result = _run_git(root, "diff", "--no-ext-diff", "--no-color", "--binary", "--", rel, timeout=8)
    if result.returncode not in {0, 1}:
        return ""
    return result.stdout


def _recover_baseline_from_git(root: Path, rel: str, expected: dict[str, Any]) -> tuple[bytes | None, str]:
    expected_sha = str(expected.get("sha256") or "")
    expected_normalized_sha = str(expected.get("normalized_sha256") or "")
    result = _run_git(root, "rev-list", "--all", "--", rel, timeout=10)
    if result.returncode != 0:
        return None, ""
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for commit in commits[:MAX_GIT_HISTORY_COMMITS]:
        show = _run_git(root, "show", f"{commit}:{rel}", timeout=10, text=False)
        if show.returncode != 0:
            continue
        data = show.stdout if isinstance(show.stdout, bytes) else str(show.stdout).encode("utf-8", errors="replace")
        if expected_sha and _hash_bytes(data) == expected_sha:
            return data, f"git:{commit[:12]}"
        if expected_normalized_sha and _hash_bytes(_normalize_line_endings(data)) == expected_normalized_sha:
            return data, f"git:{commit[:12]}:normalized"
    return None, ""


def _new_file_patch(root: Path, rel: str) -> str:
    path = root / rel
    data = _read_bytes(path)
    if not data:
        return ""
    return _unified_diff_bytes(b"", data, fromfile="/dev/null", tofile=f"current/{rel}")


def _unified_diff_bytes(old: bytes, new: bytes, *, fromfile: str, tofile: str) -> str:
    old_text = _decode_text(old)
    new_text = _decode_text(new)
    if old_text is None or new_text is None:
        return ""
    if len(old) > MAX_TEXT_BYTES or len(new) > MAX_TEXT_BYTES:
        return ""
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    )
    return "".join(lines)


def _generated_script(root: Path, rel: str, state: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    suffix = Path(rel).suffix.lower()
    if suffix not in SCRIPT_SUFFIXES:
        return None
    path = root / rel
    data = _read_bytes(path)
    content = _decode_text(data)
    script: dict[str, Any] = {
        "path": rel,
        "language": _language_for_suffix(suffix),
        "sha256": str(state.get("sha256") or _hash_bytes(data)),
        "size_bytes": len(data),
        "source": source,
        "capture_method": "manual_extension_diff_rescue",
    }
    if content is not None and len(data) <= MAX_TEXT_BYTES:
        script["content"] = redact_operational_text(content, max_chars=MAX_PATCH_CHARS)
        script["truncated"] = len(script["content"]) < len(content)
        risk_codes = _script_risk_codes(path=rel, content=content)
        if risk_codes:
            script["risk_codes"] = risk_codes
    else:
        script["content_omitted_reason"] = "script_not_text_or_too_large"
    return script


def _script_risk_codes(*, path: str, content: str) -> list[str]:
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


def _telemetry_evidence_from_snapshot(snapshot: dict[str, Any], *, send_path: str) -> dict[str, Any]:
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    extension_diffs = snapshot.get("extension_diffs") if isinstance(snapshot.get("extension_diffs"), list) else []
    generated_scripts = snapshot.get("generated_scripts") if isinstance(snapshot.get("generated_scripts"), list) else []
    counts = {
        "extension_diff_count": len(extension_diffs),
        "generated_script_count": len(generated_scripts),
        "command_event_count": 0,
        "hook_error_count": 0,
    }
    sources = []
    if extension_diffs:
        sources.append("manual_capture:extension_diffs")
    if _safe_int(summary.get("github_baseline_diff_count")):
        sources.append("github_baseline:extension_diffs")
    if _safe_int(summary.get("rescued_pre_update_diff_count")):
        sources.append("pre_update_snapshot:extension_diffs")
    if generated_scripts:
        sources.append("manual_capture:generated_scripts")
    quality_flags = []
    if generated_scripts:
        quality_flags.append("telemetry.command_events_missing")
    if _safe_int(summary.get("rescued_pre_update_diff_count")):
        quality_flags.append("telemetry.rescued_pre_update_snapshot")
    seed = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "recorded_at": snapshot.get("recorded_at"),
        "counts": counts,
        "sources": sources,
    }
    return {
        "schema": "medical-notes-workbench.telemetry-evidence.v1",
        "bundle_id": f"telem-{hashlib.sha256(json.dumps(seed, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]}",
        "sources": sources,
        "artifact_counts": counts,
        "timeline": _evidence_timeline_from_snapshot(snapshot, extension_diffs, generated_scripts),
        "quality_flags": quality_flags,
        "redaction_summary": {
            "applied": True,
            "blocked_fields": ["content", "markdown", "html", "raw_chat", "note_text", ".env", "tokens", "keys"],
            "operational_debug_fields": ["extension_diffs", "generated_scripts"],
        },
        "truncation_summary": {
            "truncated_artifacts": sum(1 for item in extension_diffs + generated_scripts if isinstance(item, dict) and item.get("truncated")),
            "omitted_artifacts": sum(
                1
                for item in extension_diffs + generated_scripts
                if isinstance(item, dict) and (item.get("full_diff_unavailable_reason") or item.get("content_omitted_reason"))
            ),
        },
        "send_path": send_path,
    }


def _evidence_timeline_from_snapshot(
    snapshot: dict[str, Any],
    extension_diffs: list[Any],
    generated_scripts: list[Any],
) -> list[dict[str, Any]]:
    at = str(snapshot.get("recorded_at") or now_iso())
    timeline: list[dict[str, Any]] = [
        {"at": at, "kind": "pre_update_snapshot", "label": str(snapshot.get("snapshot_id") or ""), "phase": str(snapshot.get("phase") or "")}
    ]
    for diff in extension_diffs[:4]:
        if isinstance(diff, dict):
            timeline.append({"at": at, "kind": "extension_diff", "label": str(diff.get("path") or ""), "change": str(diff.get("change") or "")})
    for script in generated_scripts[:4]:
        if isinstance(script, dict):
            timeline.append({"at": at, "kind": "generated_script", "label": str(script.get("path") or ""), "source": str(script.get("source") or "")})
    return timeline[:12]


def _write_snapshot_files(snapshot_path: Path, snapshot: dict[str, Any], combined_patch: str, unavailable: list[dict[str, str]]) -> None:
    _write_json(snapshot_path / "snapshot.json", _snapshot_metadata(snapshot))
    _write_json(snapshot_path / "status.json", _status_payload(snapshot))
    _write_json(snapshot_path / "diff-unavailable.json", unavailable)
    _write_text(snapshot_path / "tracked.diff", combined_patch)
    _write_text(snapshot_path / "extension-full.diff", combined_patch)
    _write_text(snapshot_path / "staged.diff", "")
    _write_text(snapshot_path / "untracked.diff", "")
    for idx, item in enumerate(snapshot.get("extension_diffs") or [], start=1):
        if not isinstance(item, dict) or not item.get("patch"):
            continue
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item.get("path") or f"diff-{idx}"))
        _write_text(snapshot_path / "diffs" / f"{idx:03d}-{name}.diff", str(item.get("patch") or ""))
    for idx, script in enumerate(snapshot.get("generated_scripts") or [], start=1):
        if not isinstance(script, dict) or not script.get("content"):
            continue
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(script.get("path") or f"script-{idx}"))
        _write_text(snapshot_path / "generated-scripts" / name, str(script.get("content") or ""))


def _load_existing_pre_update_snapshot_diffs(*, exclude_dir: Path, limit: int = 8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = Path.home() / ".gemini" / "medical-notes-workbench" / "feedback" / "pre-update-snapshots"
    diffs: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    if not root.exists():
        return diffs, summaries
    for metadata_path in sorted(root.glob("*/snapshot.json"), reverse=True):
        snapshot_dir = metadata_path.parent.resolve()
        if snapshot_dir == exclude_dir.resolve():
            continue
        try:
            snapshot = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict):
            continue
        snapshot_id = str(snapshot.get("snapshot_id") or snapshot_dir.name)
        snapshot_diffs = _read_existing_snapshot_diff_files(snapshot_dir, snapshot_id=snapshot_id)
        changed_count = _safe_int(snapshot.get("changed_path_count")) + _safe_int(snapshot.get("untracked_path_count"))
        summaries.append(
            {
                "snapshot_id": snapshot_id,
                "recorded_at": str(snapshot.get("recorded_at") or ""),
                "snapshot_path": str(snapshot_dir),
                "current_version": str(snapshot.get("current_version") or ""),
                "target_version": str(snapshot.get("target_version") or ""),
                "git_head": str(snapshot.get("git_head") or ""),
                "changed_count": changed_count,
                "diff_count": len(snapshot_diffs),
                "has_diff": bool(snapshot_diffs),
            }
        )
        diffs.extend(snapshot_diffs)
        if len(summaries) >= limit:
            break
    return diffs, summaries


def _read_existing_snapshot_diff_files(snapshot_dir: Path, *, snapshot_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for filename, change in (
        ("tracked.diff", "rescued_pre_update_tracked"),
        ("staged.diff", "rescued_pre_update_staged"),
        ("untracked.diff", "rescued_pre_update_untracked"),
        ("extension-full.diff", "rescued_manual_extension_full"),
    ):
        path = snapshot_dir / filename
        try:
            patch = path.read_text(encoding="utf-8")
        except OSError:
            continue
        patch = _filter_pre_update_patch_noise(patch)
        if not patch.strip():
            continue
        sanitized = redact_operational_text(patch, max_chars=MAX_PATCH_CHARS)
        out.append(
            {
                "path": f"existing-pre-update/{snapshot_id}/{filename}",
                "kind": "pre_update_snapshot",
                "change": change,
                "baseline_source": "existing-pre-update-snapshot",
                "patch": sanitized,
                "truncated": len(sanitized) < len(patch),
            }
        )
    return out


def _filter_pre_update_patch_noise(patch: str) -> str:
    blocks = re.split(r"(?m)(?=^diff --git )", patch)
    kept: list[str] = []
    for block in blocks:
        if not block.strip():
            continue
        normalized = block.replace("\\", "/").lower()
        if "git binary patch" in normalized:
            continue
        if any(part.lower() in normalized for part in PRE_UPDATE_PATCH_NOISE_PARTS):
            continue
        kept.append(block)
    return "\n".join(item.rstrip("\n") for item in kept if item.strip()) + ("\n" if kept else "")


def _snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "schema",
        "snapshot_id",
        "recorded_at",
        "extension_name",
        "extension_path",
        "snapshot_path",
        "current_version",
        "target_version",
        "git_head",
        "git_available",
        "reason",
        "patch_id",
        "phase",
        "changed_path_count",
        "untracked_path_count",
        "changed_paths",
        "generated_scripts",
        "summary",
        "baseline_recovered_count",
        "git_diff_empty_count",
        "github_baseline",
        "existing_pre_update_snapshots",
        "telemetry_evidence",
    }
    return {key: snapshot.get(key) for key in keys if key in snapshot}


def _status_payload(snapshot: dict[str, object]) -> dict[str, object]:
    summary = _object_dict(snapshot.get("summary"))
    return {
        "schema": "medical-notes-workbench.manual-extension-diff-capture.v1",
        "checked": True,
        "checked_at": snapshot.get("recorded_at"),
        "drift_detected": bool(summary.get("changed_count")),
        "root_label": _compact_path(str(snapshot.get("extension_path") or "")),
        "summary": summary,
        "modified_files": snapshot.get("modified_files") or [],
        "missing_files": snapshot.get("missing_files") or [],
        "unexpected_files": snapshot.get("unexpected_files") or [],
        "line_ending_only_files": snapshot.get("line_ending_only_files") or [],
        "extension_diffs": snapshot.get("extension_diffs") or [],
        "diff_unavailable": snapshot.get("diff_unavailable") or [],
        "existing_pre_update_snapshots": snapshot.get("existing_pre_update_snapshots") or [],
        "github_baseline": snapshot.get("github_baseline") or {},
        "telemetry_evidence": snapshot.get("telemetry_evidence") or {},
    }


def _manual_report_receipt(snapshot: dict[str, Any], send_result: dict[str, Any]) -> dict[str, object]:
    """Build the explicit /report receipt without depending on installed deps."""
    sent = bool(send_result.get("sent") or send_result.get("ok"))
    reason = str(send_result.get("reason") or "")
    status = "sent" if sent else "not_sent"
    return {
        "schema": MANUAL_REPORT_RECEIPT_SCHEMA,
        "status": status,
        "requested_by_user": True,
        "capture_schema": "medical-notes-workbench.manual-extension-diff-capture.v1",
        "envelope_schema": TELEMETRY_ENVELOPE_SCHEMA,
        "snapshot_path": str(snapshot.get("snapshot_path") or ""),
        "envelope_path": str(Path(str(snapshot.get("snapshot_path") or "")) / "telemetry-envelope.json"),
        "send_result_path": str(Path(str(snapshot.get("snapshot_path") or "")) / "send-result.json"),
        "sent": sent,
        "reason": reason or ("sent" if sent else "send_not_requested"),
        "next_action": ""
        if sent
        else "O relatório manual ficou salvo localmente; envie somente quando o usuário pedir /report com envio explícito.",
        "redaction_status": "redacted",
    }


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): nested for key, nested in value.items()}


def _build_envelope(
    snapshot: dict[str, Any],
    *,
    endpoint_url: str = "",
    auth_token: str = "",
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = _read_telemetry_settings(endpoint_url=endpoint_url, auth_token=auth_token, config_path=config_path, extension_path=snapshot.get("extension_path"))
    record = _run_record(snapshot)
    return _fit_envelope(
        {
            "schema": TELEMETRY_ENVELOPE_SCHEMA,
            "envelope_id": str(uuid.uuid4()),
            "generated_at": now_iso(),
            "install_id": settings.install_id or f"manual-rescue-{platform.node() or 'unknown'}",
            "payload_level": PAYLOAD_LEVEL,
            "client": {
                "app": "medical-notes-workbench",
                "app_version": str(snapshot.get("current_version") or "unknown"),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "capture_script": "scripts/mednotes/capture_extension_diff.py",
            },
            "records": [record],
            "limits": {"max_envelope_bytes": MAX_ENVELOPE_BYTES},
        },
        max_bytes=MAX_ENVELOPE_BYTES,
    )


def _run_record(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary = snapshot.get("summary") or {}
    changed = _safe_int(summary.get("changed_count"))
    return {
        "schema": RUN_RECORD_SCHEMA,
        "run_id": f"manual-extension-diff-capture-{snapshot.get('snapshot_id')}",
        "recorded_at": snapshot.get("recorded_at"),
        "workflow": "/mednotes:telemetry",
        "source": "manual_rescue",
        "command": "capture_extension_diff.py",
        "exit_code": 0,
        "duration_ms": 0,
        "status": "completed_with_warnings" if changed else "completed",
        "phase": "manual-extension-diff-capture",
        "blocked_reason": "",
        "next_action": "Analisar extension_diffs e preservar o snapshot antes de atualizar novamente a extensao.",
        "required_inputs": [],
        "human_decision_required": False,
        "dry_run": False,
        "apply": False,
        "payload_summary": {
            "counts": {
                "changed_path_count": _safe_int(snapshot.get("changed_path_count")),
                "untracked_path_count": _safe_int(snapshot.get("untracked_path_count")),
                "extension_diff_count": _safe_int(summary.get("extension_diff_count")),
                "github_baseline_diff_count": _safe_int(summary.get("github_baseline_diff_count")),
                "rescued_pre_update_diff_count": _safe_int(summary.get("rescued_pre_update_diff_count")),
                "existing_pre_update_snapshot_count": _safe_int(summary.get("existing_pre_update_snapshot_count")),
                "baseline_recovered_count": _safe_int(snapshot.get("baseline_recovered_count")),
                "git_diff_empty_count": _safe_int(snapshot.get("git_diff_empty_count")),
            },
            "warnings": ["manual_extension_diff_capture"],
            "errors": [],
            "required_inputs": [],
            "relevant_paths": (snapshot.get("changed_paths") or [])[:40],
            "path_hashes": {},
            "signals": ["extension.manual_diff_capture"],
            "status": "completed_with_warnings" if changed else "completed",
            "phase": "manual-extension-diff-capture",
        },
        "diagnostic_context": {
            "root_cause_code": "extension.manual_diff_capture",
            "root_cause_label": "Captura manual de drift da extensao",
            "recovery_command": "Abrir capture.zip ou extension-full.diff; se --send foi usado, verificar o email de telemetria.",
            "missing_inputs": [],
            "decision_context": {"types": [], "decisions": []},
            "blocker_context": {"codes": [], "counts": {}, "summaries": [], "samples": [], "routes": []},
            "contract_gaps": [],
        },
        "environment_context": {
            "extension_integrity": {
                "schema": PRE_UPDATE_EXTENSION_SNAPSHOT_SCHEMA,
                "drift_detected": bool(changed),
                "snapshot_id": snapshot.get("snapshot_id"),
                "snapshot_path": snapshot.get("snapshot_path"),
                "patch_id": snapshot.get("patch_id"),
                "phase": snapshot.get("phase"),
                "reason": snapshot.get("reason"),
                "extension_name": snapshot.get("extension_name"),
                "current_version": snapshot.get("current_version"),
                "target_version": snapshot.get("target_version"),
                "git_head": snapshot.get("git_head"),
                "git_available": snapshot.get("git_available"),
                "extension_path": snapshot.get("extension_path"),
                "summary": summary,
                "extension_diffs": snapshot.get("extension_diffs") or [],
                "github_baseline": snapshot.get("github_baseline") or {},
                "baseline_recovered_count": snapshot.get("baseline_recovered_count"),
                "git_diff_empty_count": snapshot.get("git_diff_empty_count"),
            }
        },
        "diagnostic_snippets": [],
        "telemetry_evidence": snapshot.get("telemetry_evidence") or _telemetry_evidence_from_snapshot(snapshot, send_path="manual_extension_diff_capture"),
        "extension_diffs": snapshot.get("extension_diffs") or [],
        "generated_scripts": snapshot.get("generated_scripts") or [],
        "command_events": [],
    }


def _fit_envelope(envelope: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) <= max_bytes:
        return envelope
    records = envelope.get("records")
    if not isinstance(records, list) or not records:
        return envelope
    record = records[0]
    if not isinstance(record, dict):
        return envelope
    diffs = record.get("extension_diffs")
    if isinstance(diffs, list):
        for item in diffs:
            if isinstance(item, dict) and isinstance(item.get("patch"), str) and len(item["patch"]) > 24 * 1024:
                item["patch"] = item["patch"][: 24 * 1024 - 3].rstrip() + "..."
                item["truncated"] = True
        if len(diffs) > 8:
            record["extension_diffs"] = diffs[:8] + [{"path": "", "kind": "summary", "change": "truncated", "omitted_count": len(diffs) - 8}]
    integrity = record.get("environment_context", {}).get("extension_integrity") if isinstance(record.get("environment_context"), dict) else None
    if isinstance(integrity, dict):
        integrity["extension_diffs"] = record.get("extension_diffs", [])
    return envelope


def _read_telemetry_settings(
    *,
    endpoint_url: str = "",
    auth_token: str = "",
    config_path: str | Path | None = None,
    extension_path: Any = None,
) -> TelemetrySettings:
    config = _read_toml_telemetry_section(Path(config_path).expanduser() if config_path else _default_config_path())
    defaults = _read_distribution_defaults(extension_path)
    endpoint = endpoint_url or str(config.get("endpoint_url") or defaults.get("endpoint_url") or "")
    token = auth_token or str(config.get("auth_token") or defaults.get("auth_token") or "")
    install_id = str(config.get("install_id") or defaults.get("install_id") or f"manual-rescue-{uuid.uuid4()}")
    return TelemetrySettings(endpoint_url=endpoint, auth_token=token, install_id=install_id)


def _default_config_path() -> Path:
    override = os.getenv("MEDNOTES_TELEMETRY_CONFIG")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    home = os.getenv("MEDNOTES_HOME")
    base = Path(os.path.expandvars(home)).expanduser() if home else Path.home() / ".mednotes"
    return base / "config.toml"


def _read_toml_telemetry_section(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        section = data.get("telemetry") if isinstance(data, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return _read_toml_section_fallback(path, "telemetry")


def _read_toml_section_fallback(path: Path, section_name: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.search(rf"(?ms)^\[{re.escape(section_name)}\]\s*(.*?)(?=^\[[^\n]+\]\s*|\Z)", text)
    if not match:
        return {}
    out: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower() in {"true", "false"}:
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def _read_distribution_defaults(extension_path: Any) -> dict[str, Any]:
    candidates: list[Path] = []
    env_path = os.getenv("MEDNOTES_TELEMETRY_DEFAULTS")
    if env_path:
        candidates.append(Path(os.path.expandvars(env_path)).expanduser())
    if extension_path:
        root = Path(str(extension_path)).expanduser()
        candidates.extend([root / "telemetry.defaults.json", root / ".telemetry-defaults.json"])
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _send_envelope(
    envelope: dict[str, Any],
    *,
    endpoint_url: str = "",
    auth_token: str = "",
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = _read_telemetry_settings(
        endpoint_url=endpoint_url,
        auth_token=auth_token,
        config_path=config_path,
        extension_path=(envelope.get("records") or [{}])[0].get("environment_context", {}).get("extension_integrity", {}).get("extension_path")
        if isinstance((envelope.get("records") or [{}])[0], dict)
        else "",
    )
    if not settings.endpoint_url or not settings.auth_token:
        return {"ok": False, "sent": False, "reason": "telemetry_endpoint_or_token_missing"}
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib_request.Request(
        settings.endpoint_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.auth_token}",
            "Content-Type": "application/json",
            "X-MedNotes-Telemetry-Schema": TELEMETRY_ENVELOPE_SCHEMA,
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=8) as response:
            response_body = response.read(64 * 1024).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 300,
                "sent": 200 <= response.status < 300,
                "status": response.status,
                "endpoint_url": settings.endpoint_url,
                "auth_token": settings.auth_token,
                "response": redact_operational_text(response_body, max_chars=2000),
            }
    except urllib_error.HTTPError as exc:
        response_body = exc.read(64 * 1024).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "sent": False,
            "status": exc.code,
            "endpoint_url": settings.endpoint_url,
            "auth_token": settings.auth_token,
            "reason": redact_operational_text(response_body or str(exc), max_chars=2000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "sent": False,
            "endpoint_url": settings.endpoint_url,
            "auth_token": settings.auth_token,
            "reason": redact_operational_text(str(exc), max_chars=2000),
        }


def _flush_digest(endpoint_url: str, auth_token: str) -> dict[str, Any]:
    if not endpoint_url or not auth_token:
        return {"ok": False, "reason": "endpoint_or_token_missing"}
    digest_url = _digest_url(endpoint_url)
    request = urllib_request.Request(
        digest_url,
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=8) as response:
            response_body = response.read(64 * 1024).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300, "status": response.status, "response": redact_operational_text(response_body, max_chars=2000)}
    except urllib_error.HTTPError as exc:
        response_body = exc.read(64 * 1024).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "reason": redact_operational_text(response_body or str(exc), max_chars=2000)}
    except Exception as exc:
        return {"ok": False, "reason": redact_operational_text(str(exc), max_chars=2000)}


def _digest_url(endpoint_url: str) -> str:
    parsed = urlsplit(endpoint_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/telemetry/workflow-runs"):
        path = path[: -len("/v1/telemetry/workflow-runs")] + "/v1/telemetry/digest/send"
    else:
        path = path + "/digest/send"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _redacted_send_result(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    if out.get("auth_token"):
        out["auth_token"] = "[redacted]"
    if out.get("endpoint_url"):
        out["endpoint_url"] = _redact_url(str(out["endpoint_url"]))
    return out


def _public_capture_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "snapshot_path": snapshot.get("snapshot_path"),
        "zip_path": snapshot.get("zip_path"),
        "summary": snapshot.get("summary"),
        "existing_pre_update_snapshots": snapshot.get("existing_pre_update_snapshots") or [],
        "baseline_recovered_count": snapshot.get("baseline_recovered_count"),
        "git_diff_empty_count": snapshot.get("git_diff_empty_count"),
        "send_result": snapshot.get("send_result"),
        "next_action": "Se --send foi usado, o envelope ficou no digest horario do Worker; use --send --flush apenas quando precisar forcar um email imediato.",
    }


def _write_zip(snapshot_path: Path) -> Path | None:
    zip_path = snapshot_path / "capture.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in snapshot_path.rglob("*"):
                if path == zip_path or not path.is_file():
                    continue
                if path.stat().st_size > MAX_ZIP_FILE_BYTES:
                    continue
                archive.write(path, path.relative_to(snapshot_path))
    except OSError:
        return None
    return zip_path


def _prune_pre_update_snapshots_for_path(snapshot_path: Path) -> dict[str, object]:
    root = _pre_update_snapshot_root()
    if not _path_is_relative_to(snapshot_path, root):
        return {}
    return _prune_pre_update_snapshots(root=root, keep=snapshot_path)


def _prune_pre_update_snapshots(*, root: Path, keep: Path) -> dict[str, object]:
    max_dirs = _env_int("MEDNOTES_PRE_UPDATE_SNAPSHOT_MAX_DIRS", DEFAULT_PRE_UPDATE_SNAPSHOT_MAX_DIRS, minimum=0)
    retention_days = _env_int(
        "MEDNOTES_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS",
        DEFAULT_PRE_UPDATE_SNAPSHOT_RETENTION_DAYS,
        minimum=0,
    )
    try:
        items = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return {
            "schema": "medical-notes-workbench.local-feedback-retention.v1",
            "target": "pre-update-snapshots",
            "max_items": max_dirs,
            "retention_days": retention_days,
            "removed_count": 0,
            "remaining_count": 0,
            "error": "list_failed",
        }

    victims = _retention_victims(items, max_items=max_dirs, retention_days=retention_days, keep=keep)
    removed = 0
    for victim in victims:
        try:
            shutil.rmtree(victim)
            removed += 1
        except OSError:
            continue

    try:
        remaining = sum(1 for item in root.iterdir() if item.is_dir())
    except OSError:
        remaining = 0
    return {
        "schema": "medical-notes-workbench.local-feedback-retention.v1",
        "target": "pre-update-snapshots",
        "max_items": max_dirs,
        "retention_days": retention_days,
        "removed_count": removed,
        "remaining_count": remaining,
    }


def _retention_victims(paths: list[Path], *, max_items: int, retention_days: int, keep: Path) -> list[Path]:
    keep_path = keep.resolve()
    ordered = sorted(paths, key=lambda item: (_mtime(item), item.name), reverse=True)
    kept: set[Path] = {keep_path}
    for item in ordered:
        resolved = item.resolve()
        if resolved in kept:
            continue
        if len(kept) < max_items:
            kept.add(resolved)

    cutoff = datetime.now(UTC).timestamp() - (retention_days * 24 * 60 * 60)
    victims: list[Path] = []
    for item in ordered:
        resolved = item.resolve()
        if resolved == keep_path:
            continue
        if resolved not in kept or _mtime(item) < cutoff:
            victims.append(item)
    return victims


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _run_git(root: Path, *args: str, timeout: float = 5, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _git_stdout(root: Path, *args: str) -> str:
    try:
        result = _run_git(root, *args, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout).strip()


def _has_git_worktree(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    return _git_stdout(root, "rev-parse", "--is-inside-work-tree").lower() == "true"


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_line_endings(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def redact_operational_text(value: Any, *, max_chars: int = MAX_PATCH_CHARS) -> str:
    text = str(value)
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
    text = re.sub(r"https?://[^\s)>\"]+", lambda match: _redact_url(match.group(0)), text)
    text = re.sub(r"\b[A-Za-z0-9_=-]{36,}\b", "[redacted-token]", text)
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _redact_url(url: str) -> str:
    if "?" not in url:
        return url
    head, _query = url.split("?", 1)
    return f"{head}?[redacted]"


def _compact_path(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _file_kind(rel: str) -> str:
    path = Path(rel)
    parts = rel.split("/")
    suffix = path.suffix.lower()
    if rel == "GEMINI.md" or (len(parts) >= 2 and parts[0] == "skills" and path.name == "SKILL.md"):
        return "prompt"
    if parts and parts[0] == "commands":
        return "launcher"
    if parts and parts[0] == "docs":
        return "runbook" if len(parts) > 1 and parts[1] == "workflows" else "documentation"
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if parts and parts[0] in {"agents", "policies", "mcp"}:
        return parts[0][:-1] if parts[0].endswith("s") else parts[0]
    return "metadata"


def _language_for_suffix(suffix: str) -> str:
    return {
        ".cjs": "javascript",
        ".cmd": "batch",
        ".js": "javascript",
        ".mjs": "javascript",
        ".ps1": "powershell",
        ".py": "python",
        ".sh": "shell",
    }.get(suffix, suffix.lstrip(".") or "text")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture trusted extension debug diff for Medical Notes Workbench.")
    parser.add_argument(
        "--extension-path",
        default=str(Path.home() / ".gemini" / "extensions" / "medical-notes-workbench"),
        help="Installed extension path. Defaults to ~/.gemini/extensions/medical-notes-workbench.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Snapshot output directory. Defaults to ~/.mednotes/feedback/pre-update-snapshots/<id>.",
    )
    parser.add_argument("--send", action="store_true", help="Post the trusted debug envelope to the configured telemetry Worker.")
    parser.add_argument("--flush", action="store_true", help="Request immediate digest delivery after --send. Use sparingly; normal delivery is hourly digest.")
    parser.add_argument("--no-flush", action="store_true", help="Compatibility no-op; immediate digest delivery is already disabled unless --flush is passed.")
    parser.add_argument(
        "--no-existing-snapshots",
        action="store_true",
        help="Do not attach previously captured pre-update snapshots from the feedback directory.",
    )
    parser.add_argument("--endpoint", default="", help="Telemetry endpoint override.")
    parser.add_argument("--token", default="", help="Telemetry auth token override.")
    parser.add_argument("--config", default="", help="Telemetry config.toml override.")
    parser.add_argument(
        "--github-baseline-url",
        default="",
        help=f"Downloaded GitHub extension bundle zip to compare against, for example {DEFAULT_GITHUB_BASELINE_URL}.",
    )
    args = parser.parse_args(argv)

    snapshot = capture_extension_diff(
        args.extension_path,
        output_dir=args.output_dir or None,
        send=args.send,
        endpoint_url=args.endpoint,
        auth_token=args.token,
        config_path=args.config or None,
        flush_digest=bool(args.flush and not args.no_flush),
        include_existing_snapshots=not args.no_existing_snapshots,
        github_baseline_url=args.github_baseline_url,
    )
    print(json.dumps(_public_capture_result(snapshot), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
