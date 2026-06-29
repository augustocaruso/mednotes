"""Lightweight extension integrity manifest and runtime drift checks."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INTEGRITY_MANIFEST_SCHEMA = "medical-notes-workbench.extension-integrity-manifest.v1"
INTEGRITY_STATUS_SCHEMA = "medical-notes-workbench.extension-integrity-status.v1"
INTEGRITY_CACHE_SCHEMA = "medical-notes-workbench.extension-integrity-cache.v1"
MANIFEST_FILENAME = "extension-integrity-manifest.json"
CACHE_FILENAME = "extension-integrity-cache.json"

DEFAULT_THROTTLE_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 1.0
MAX_REPORTED_FILES = 24
MAX_DIFF_FILES = 3
MAX_DIFF_LINES = 28
MAX_SAMPLE_CHARS = 420
MAX_TEXT_BYTES = 128 * 1024
MAX_EXTENSION_DIFF_FILES = 12
MAX_EXTENSION_DIFF_CHARS = 96 * 1024
MAX_GIT_HISTORY_COMMITS = 600
PROMPT_ENCODING_CORRUPTION_CODE = "extension.prompt_encoding_corruption"

MONITORED_EXACT_FILES = {
    "GEMINI.md",
    "gemini-extension.json",
    "package.json",
    "pyproject.toml",
}
MONITORED_DIRS = {
    "commands",
    "docs",
    "skills",
    "agents",
    "hooks",
    "policies",
    "mcp",
    "scripts",
    "src",
}
MONITORED_SUFFIXES = {
    ".md",
    ".toml",
    ".json",
    ".py",
    ".mjs",
    ".js",
    ".cjs",
    ".sh",
    ".ps1",
    ".cmd",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_SUFFIXES = MONITORED_SUFFIXES | {""}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "feedback",
    "outbox",
    "cache",
}
EXCLUDED_NAMES = {
    MANIFEST_FILENAME,
    CACHE_FILENAME,
    ".DS_Store",
    ".env",
    ".telemetry-defaults.json",
    "telemetry.defaults.json",
    "uv.lock",
}

SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd"}
FULL_DIFF_EXACT_FILES = {"GEMINI.md", "gemini-extension.json", "package.json", "pyproject.toml"}
FULL_DIFF_PREFIXES = (
    "commands/",
    "docs/",
    "skills/",
    "agents/",
    "hooks/",
    "scripts/",
    "src/",
)

CORRUPTED_CANONICAL_HEADINGS = (
    "Fontes Consolidadas",
    "Notas Relacionadas",
    "Fechamento",
)
CORRUPTED_HEADING_RE = re.compile(
    r"(?m)^##\s+\?+\s+(" + "|".join(re.escape(item) for item in CORRUPTED_CANONICAL_HEADINGS) + r")\s*$"
)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def generate_integrity_manifest(root: str | Path, *, version: str = "") -> dict[str, Any]:
    """Build a manifest for public extension prompts, launchers and scripts."""
    root_path = Path(root).resolve()
    files = []
    for path in _iter_public_files(root_path):
        state = _file_state(path, root_path)
        files.append(
            {
                "path": state["path"],
                "kind": _file_kind(state["path"]),
                "sha256": state["sha256"],
                "normalized_sha256": state.get("normalized_sha256", ""),
                "size_bytes": state["size_bytes"],
                "line_count": state["line_count"],
            }
        )
    files.sort(key=lambda item: item["path"])
    return {
        "schema": INTEGRITY_MANIFEST_SCHEMA,
        "generated_at": now_iso(),
        "app_version": version or "unknown",
        "root_kind": "gemini-cli-extension",
        "file_count": len(files),
        "files": files,
    }


def write_integrity_manifest(root: str | Path, *, output_path: str | Path | None = None, version: str = "") -> Path:
    root_path = Path(root).resolve()
    manifest = generate_integrity_manifest(root_path, version=version)
    target = Path(output_path).resolve() if output_path else root_path / MANIFEST_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(target, manifest)
    return target


def check_extension_integrity(
    *,
    root: str | Path | None = None,
    manifest_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    include_diff: bool = False,
    force: bool = False,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compare the installed extension with its build-time manifest.

    The check is intentionally cheap after the first run: it stats expected
    files, reuses cached hashes while size/mtime match, and only scans for
    unexpected files when forced or the throttle window has expired.
    """
    started = time.monotonic()
    resolved_root, resolved_manifest = _resolve_root_and_manifest(root=root, manifest_path=manifest_path)
    if not resolved_root or not resolved_manifest:
        return _skipped_status("manifest_not_found", started=started)

    cache_path = _cache_path(cache_dir)
    cache = _read_cache(cache_path)
    manifest = _read_manifest(resolved_manifest)
    if not manifest:
        return _skipped_status("manifest_unreadable", started=started, root=resolved_root, manifest_path=resolved_manifest)

    manifest_hash = _hash_bytes(resolved_manifest.read_bytes())
    expected_files = _manifest_files(manifest)
    cache_fresh = _cache_fresh(cache, manifest_hash=manifest_hash, throttle_seconds=throttle_seconds)
    cached_states = cache.get("file_states") if isinstance(cache.get("file_states"), dict) else {}
    baseline_samples = cache.get("baseline_samples") if isinstance(cache.get("baseline_samples"), dict) else {}

    modified: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    line_ending_only: list[dict[str, Any]] = []
    encoding_corruption: list[dict[str, Any]] = []
    diff_samples: list[dict[str, Any]] = []
    next_states: dict[str, dict[str, Any]] = {}
    next_baseline_samples = dict(baseline_samples)
    reused_hashes = 0
    hashed_files = 0
    warnings: list[str] = []

    try:
        for rel, expected in expected_files.items():
            _raise_if_timeout(started, timeout_seconds)
            path = resolved_root / rel
            if not path.is_file():
                missing.append(_missing_entry(expected))
                continue
            stat = path.stat()
            cached_value = cached_states.get(rel)
            cached = cached_value if isinstance(cached_value, dict) else {}
            if _same_state(cached, stat):
                state = dict(cached)
                state.setdefault("path", rel)
                reused_hashes += 1
            else:
                state = _file_state(path, resolved_root, stat=stat)
                hashed_files += 1
            next_states[rel] = state
            if state.get("sha256") == expected.get("sha256"):
                if _is_text_file(path):
                    next_baseline_samples[rel] = _sample_text(path)
                continue
            corruption = _encoding_corruption_entry(rel, path)
            if corruption:
                encoding_corruption.append(corruption)
            if _same_normalized_text(state, expected):
                line_ending_only.append(_line_ending_only_entry(expected, state))
                if _is_text_file(path):
                    next_baseline_samples[rel] = _sample_text(path)
                continue
            entry = _modified_entry(expected, state)
            modified.append(entry)
            if include_diff and len(diff_samples) < MAX_DIFF_FILES:
                diff_samples.append(_diff_sample(rel, path, baseline_samples.get(rel), entry["kind"]))

        scan_unexpected = force or not cache_fresh or not cache.get("last_unexpected_scan_at")
        if scan_unexpected:
            for path in _iter_public_files(resolved_root):
                _raise_if_timeout(started, timeout_seconds)
                rel = _relative_path(path, resolved_root)
                if rel in expected_files:
                    continue
                state = _file_state(path, resolved_root)
                unexpected.append(_unexpected_entry(state))
                corruption = _encoding_corruption_entry(rel, path)
                if corruption:
                    encoding_corruption.append(corruption)
                if include_diff and len(diff_samples) < MAX_DIFF_FILES:
                    diff_samples.append(_diff_sample(rel, path, None, state["kind"]))
        else:
            warnings.append("unexpected_file_scan_throttled")
            cached_status = cache.get("status") if isinstance(cache.get("status"), dict) else {}
            unexpected = list(cached_status.get("unexpected_files") or [])
    except TimeoutError:
        status = _status(
            checked=False,
            skipped_reason="integrity_check_skipped_timeout",
            started=started,
            root=resolved_root,
            manifest_path=resolved_manifest,
            manifest=manifest,
            manifest_hash=manifest_hash,
            cache_hit=cache_fresh,
            warnings=["integrity_check_skipped_timeout"],
        )
        _write_cache(
            cache_path,
            {
                **cache,
                "schema": INTEGRITY_CACHE_SCHEMA,
                "checked_at": status["checked_at"],
                "status": status,
            },
        )
        return status

    summary = {
        "modified_count": len(modified),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "changed_count": len(modified) + len(missing) + len(unexpected),
        "line_ending_only_count": len(line_ending_only),
        "encoding_corruption_count": len(encoding_corruption),
        "manifest_file_count": len(expected_files),
        "reused_hash_count": reused_hashes,
        "hashed_file_count": hashed_files,
    }
    drift_detected = bool(summary["changed_count"])
    extension_diffs = _extension_diffs(
        resolved_root,
        modified=modified,
        missing=missing,
        unexpected=unexpected,
    ) if include_diff and drift_detected else []
    status = _status(
        checked=True,
        skipped_reason="",
        started=started,
        root=resolved_root,
        manifest_path=resolved_manifest,
        manifest=manifest,
        manifest_hash=manifest_hash,
        cache_hit=cache_fresh and not force and not hashed_files and not drift_detected,
        warnings=warnings,
        summary=summary,
        drift_detected=drift_detected,
        modified_files=modified[:MAX_REPORTED_FILES],
        missing_files=missing[:MAX_REPORTED_FILES],
        unexpected_files=unexpected[:MAX_REPORTED_FILES],
        line_ending_only_files=line_ending_only[:MAX_REPORTED_FILES],
        encoding_corruption_files=encoding_corruption[:MAX_REPORTED_FILES],
        diff_samples=diff_samples,
        extension_diffs=extension_diffs,
    )
    next_cache = {
        "schema": INTEGRITY_CACHE_SCHEMA,
        "root": str(resolved_root),
        "manifest_hash": manifest_hash,
        "checked_at": status["checked_at"],
        "last_unexpected_scan_at": status["checked_at"] if (force or not cache_fresh or not cache.get("last_unexpected_scan_at")) else cache.get("last_unexpected_scan_at"),
        "status": status,
        "file_states": next_states,
        "baseline_samples": next_baseline_samples,
    }
    _write_cache(cache_path, next_cache)
    return status


def safe_check_extension_integrity(**kwargs: Any) -> dict[str, Any]:
    try:
        return check_extension_integrity(**kwargs)
    except Exception as exc:
        return {
            "schema": INTEGRITY_STATUS_SCHEMA,
            "checked": False,
            "checked_at": now_iso(),
            "drift_detected": False,
            "skipped_reason": "integrity_check_failed",
            "error": redact_snippet(str(exc), max_chars=240),
            "summary": {
                "modified_count": 0,
                "missing_count": 0,
                "unexpected_count": 0,
            "changed_count": 0,
            "line_ending_only_count": 0,
            "encoding_corruption_count": 0,
        },
    }


def _iter_public_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for rel in MONITORED_EXACT_FILES:
        path = root / rel
        if _is_monitored_file(path, root) and path not in seen:
            seen.add(path)
            paths.append(path)
    for dirname in sorted(MONITORED_DIRS):
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if _is_monitored_file(path, root) and path not in seen:
                seen.add(path)
                paths.append(path)
    return sorted(paths, key=lambda item: _relative_path(item, root))


def _is_monitored_file(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    rel = _relative_path(path, root)
    parts = Path(rel).parts
    if (
        not parts
        or any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in parts)
        or path.name in EXCLUDED_NAMES
    ):
        return False
    if rel in MONITORED_EXACT_FILES:
        return True
    if parts[0] not in MONITORED_DIRS:
        return False
    return path.suffix.lower() in MONITORED_SUFFIXES


def _file_state(path: Path, root: Path, *, stat: os.stat_result | None = None) -> dict[str, Any]:
    stat = stat or path.stat()
    text = _read_text_sample(path)
    full_text = _read_text(path) if _is_text_file(path) else ""
    return {
        "path": _relative_path(path, root),
        "kind": _file_kind(_relative_path(path, root)),
        "sha256": _hash_file(path),
        "normalized_sha256": _hash_normalized_text(full_text) if full_text else "",
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "line_count": _line_count(path, sample=full_text or text),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _line_count(path: Path, *, sample: str | None = None) -> int:
    if not _is_text_file(path):
        return 0
    try:
        text = sample if sample is not None else path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _sample_text(path: Path) -> str:
    return redact_snippet(_read_text_sample(path), max_chars=MAX_SAMPLE_CHARS)


def _read_text_sample(path: Path) -> str:
    if not _is_text_file(path):
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_TEXT_BYTES)
    except OSError:
        return ""


def _read_text(path: Path) -> str:
    if not _is_text_file(path):
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def _hash_normalized_text(text: str) -> str:
    return _hash_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def _normalize_line_endings_bytes(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _same_state(cached: dict[str, Any], stat: os.stat_result) -> bool:
    return bool(
        cached.get("sha256")
        and int(cached.get("size_bytes", -1)) == int(stat.st_size)
        and int(cached.get("mtime_ns", -1)) == int(stat.st_mtime_ns)
    )


def _same_normalized_text(state: dict[str, Any], expected: dict[str, Any]) -> bool:
    expected_normalized = str(expected.get("normalized_sha256") or "")
    actual_normalized = str(state.get("normalized_sha256") or "")
    return bool(expected_normalized and actual_normalized and expected_normalized == actual_normalized)


def _manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        result[str(item["path"])] = item
    return result


def _missing_entry(expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": expected.get("path", ""),
        "kind": expected.get("kind") or _file_kind(str(expected.get("path") or "")),
        "expected_sha256": expected.get("sha256", ""),
        "expected_normalized_sha256": expected.get("normalized_sha256", ""),
        "size_bytes": expected.get("size_bytes", 0),
        "line_count": expected.get("line_count", 0),
    }


def _modified_entry(expected: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": state.get("path", expected.get("path", "")),
        "kind": expected.get("kind") or state.get("kind") or _file_kind(str(state.get("path") or "")),
        "expected_sha256": expected.get("sha256", ""),
        "expected_normalized_sha256": expected.get("normalized_sha256", ""),
        "actual_sha256": state.get("sha256", ""),
        "size_delta": int(state.get("size_bytes", 0)) - int(expected.get("size_bytes", 0)),
        "line_delta": int(state.get("line_count", 0)) - int(expected.get("line_count", 0)),
    }


def _line_ending_only_entry(expected: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": state.get("path", expected.get("path", "")),
        "kind": expected.get("kind") or state.get("kind") or _file_kind(str(state.get("path") or "")),
        "expected_sha256": expected.get("sha256", ""),
        "actual_sha256": state.get("sha256", ""),
        "normalized_sha256": state.get("normalized_sha256", expected.get("normalized_sha256", "")),
        "size_delta": int(state.get("size_bytes", 0)) - int(expected.get("size_bytes", 0)),
        "line_delta": int(state.get("line_count", 0)) - int(expected.get("line_count", 0)),
        "change": "line_ending_only",
    }


def _unexpected_entry(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": state.get("path", ""),
        "kind": state.get("kind", ""),
        "actual_sha256": state.get("sha256", ""),
        "size_bytes": state.get("size_bytes", 0),
        "line_count": state.get("line_count", 0),
    }


def _encoding_corruption_entry(rel: str, path: Path) -> dict[str, Any] | None:
    if not _is_text_file(path):
        return None
    text = _read_text(path)
    matches = [
        {"heading": match.group(1), "line": text[: match.start()].count("\n") + 1}
        for match in CORRUPTED_HEADING_RE.finditer(text)
    ]
    if not matches:
        return None
    return {
        "path": rel,
        "kind": _file_kind(rel),
        "code": PROMPT_ENCODING_CORRUPTION_CODE,
        "matches": matches[:8],
    }


def _diff_sample(rel: str, path: Path, baseline_sample: Any, kind: str) -> dict[str, Any]:
    current = _sample_text(path)
    if not baseline_sample:
        return {
            "path": rel,
            "kind": kind,
            "diff_unavailable_reason": "baseline_not_cached",
            "current_excerpt": current,
        }
    before = str(baseline_sample).splitlines()
    after = current.splitlines()
    lines = list(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"expected/{rel}",
            tofile=f"current/{rel}",
            n=2,
            lineterm="",
        )
    )
    return {
        "path": rel,
        "kind": kind,
        "sample": redact_snippet("\n".join(lines[:MAX_DIFF_LINES]), max_chars=MAX_SAMPLE_CHARS),
        "truncated": len(lines) > MAX_DIFF_LINES,
    }


def _extension_diffs(
    root: Path,
    *,
    modified: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    unexpected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for change, values in (("modified", modified), ("missing", missing), ("unexpected", unexpected)):
        for item in values:
            rel = str(item.get("path") or "")
            if _allowed_full_diff_path(rel):
                items.append((change, item))
    diffs: list[dict[str, Any]] = []
    has_git = _has_git_worktree(root)
    for change, item in items[:MAX_EXTENSION_DIFF_FILES]:
        rel = str(item.get("path") or "")
        entry = {
            "path": rel,
            "kind": item.get("kind") or _file_kind(rel),
            "change": change,
        }
        patch = ""
        reason = ""
        if has_git and change in {"modified", "missing"}:
            patch = _git_diff(root, rel)
            if not patch:
                reason = "git_diff_empty"
                baseline, baseline_source = _recover_baseline_from_git(root, rel, item)
                if baseline is not None:
                    current = b"" if change == "missing" else _read_bytes(root / rel)
                    patch = _unified_diff_bytes(baseline, current, fromfile=f"manifest/{rel}", tofile=f"current/{rel}")
                    if patch:
                        entry["baseline_source"] = baseline_source
                        reason = ""
            else:
                entry["baseline_source"] = "git:diff"
        elif not has_git:
            reason = "git_repository_not_available"
        if not patch and has_git and change == "unexpected":
            patch = _new_file_patch(root, rel)
            if not patch:
                reason = reason or "new_file_patch_unavailable"
            else:
                entry["baseline_source"] = "new-file"
        if patch:
            sanitized = redact_operational_text(patch, max_chars=MAX_EXTENSION_DIFF_CHARS)
            entry["patch"] = sanitized
            entry["truncated"] = len(sanitized) < len(patch)
        else:
            entry["full_diff_unavailable_reason"] = reason or "full_diff_unavailable"
        diffs.append(entry)
    if len(items) > MAX_EXTENSION_DIFF_FILES:
        diffs.append(
            {
                "path": "",
                "kind": "summary",
                "change": "truncated",
                "full_diff_unavailable_reason": f"only_first_{MAX_EXTENSION_DIFF_FILES}_diffs_included",
                "omitted_count": len(items) - MAX_EXTENSION_DIFF_FILES,
            }
        )
    return diffs


def _allowed_full_diff_path(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel in FULL_DIFF_EXACT_FILES:
        return True
    return rel.startswith(FULL_DIFF_PREFIXES)


def _git_diff(root: Path, rel: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-ext-diff", "--no-color", "--binary", "--", rel],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode not in {0, 1}:
        return ""
    return result.stdout


def _recover_baseline_from_git(root: Path, rel: str, item: dict[str, Any]) -> tuple[bytes | None, str]:
    expected_sha = str(item.get("expected_sha256") or "")
    expected_normalized_sha = str(item.get("expected_normalized_sha256") or "")
    try:
        revs = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--all", "--", rel],
            text=True,
            capture_output=True,
            check=False,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if revs.returncode != 0:
        return None, ""
    commits = [line.strip() for line in revs.stdout.splitlines() if line.strip()]
    for commit in commits[:MAX_GIT_HISTORY_COMMITS]:
        try:
            show = subprocess.run(
                ["git", "-C", str(root), "show", f"{commit}:{rel}"],
                capture_output=True,
                check=False,
                timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if show.returncode != 0:
            continue
        data = show.stdout if isinstance(show.stdout, bytes) else str(show.stdout).encode("utf-8", errors="replace")
        if expected_sha and _hash_bytes(data) == expected_sha:
            return data, f"git:{commit[:12]}"
        if expected_normalized_sha and _hash_bytes(_normalize_line_endings_bytes(data)) == expected_normalized_sha:
            return data, f"git:{commit[:12]}:normalized"
    return None, ""


def _has_git_worktree(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _new_file_patch(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file() or not _is_text_file(path):
        return ""
    data = _read_bytes(path)
    if not data:
        return ""
    return _unified_diff_bytes(b"", data, fromfile="/dev/null", tofile=f"current/{rel}")


def _unified_diff_bytes(old: bytes, new: bytes, *, fromfile: str, tofile: str) -> str:
    if len(old) > MAX_TEXT_BYTES or len(new) > MAX_TEXT_BYTES:
        return ""
    old_text = old.decode("utf-8", errors="replace")
    new_text = new.decode("utf-8", errors="replace")
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )
    return "".join(lines)


def _file_kind(rel: str) -> str:
    path = Path(rel)
    parts = path.parts
    suffix = path.suffix.lower()
    if rel == "GEMINI.md" or (len(parts) >= 2 and parts[0] == "skills" and path.name == "SKILL.md"):
        return "prompt"
    if parts and parts[0] == "commands":
        return "launcher"
    if parts and parts[0] == "docs":
        return "runbook" if len(parts) > 1 and parts[1] == "workflows" else "documentation"
    if suffix in {".py", ".mjs", ".js", ".cjs", ".sh", ".ps1", ".cmd"}:
        return "script"
    if parts and parts[0] in {"agents", "policies", "mcp"}:
        return parts[0][:-1] if parts[0].endswith("s") else parts[0]
    return "metadata"


def _resolve_root_and_manifest(
    *,
    root: str | Path | None,
    manifest_path: str | Path | None,
) -> tuple[Path | None, Path | None]:
    if manifest_path:
        manifest = Path(os.path.expandvars(str(manifest_path))).expanduser().resolve()
        return manifest.parent, manifest if manifest.is_file() else None
    if root:
        root_path = Path(os.path.expandvars(str(root))).expanduser().resolve()
        manifest = root_path / MANIFEST_FILENAME
        return root_path, manifest if manifest.is_file() else None
    env_root = os.getenv("MEDNOTES_EXTENSION_ROOT")
    if env_root:
        root_path = Path(os.path.expandvars(env_root)).expanduser().resolve()
        manifest = root_path / MANIFEST_FILENAME
        return root_path, manifest if manifest.is_file() else None
    for start in [Path.cwd(), Path(__file__).resolve()]:
        for parent in [start, *start.parents]:
            manifest = parent / MANIFEST_FILENAME
            if manifest.is_file():
                return parent, manifest
    return None, None


def _cache_path(cache_dir: str | Path | None) -> Path | None:
    if not cache_dir:
        return None
    return Path(os.path.expandvars(str(cache_dir))).expanduser() / CACHE_FILENAME


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != INTEGRITY_MANIFEST_SCHEMA:
        return None
    return data


def _read_cache(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path | None, data: dict[str, Any] | None = None, *, status: dict[str, Any] | None = None) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data or {"schema": INTEGRITY_CACHE_SCHEMA, "checked_at": now_iso(), "status": status or {}}
    _atomic_write_json(path, payload)


def _cache_fresh(cache: dict[str, Any], *, manifest_hash: str, throttle_seconds: int) -> bool:
    if cache.get("manifest_hash") != manifest_hash:
        return False
    checked_at = _parse_time(str(cache.get("checked_at") or ""))
    if checked_at <= 0:
        return False
    return time.time() - checked_at < max(0, throttle_seconds)


def _parse_time(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return parsed.timestamp()


def _status(
    *,
    checked: bool,
    skipped_reason: str,
    started: float,
    root: Path | None = None,
    manifest_path: Path | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_hash: str = "",
    cache_hit: bool = False,
    warnings: list[str] | None = None,
    summary: dict[str, Any] | None = None,
    drift_detected: bool = False,
    modified_files: list[dict[str, Any]] | None = None,
    missing_files: list[dict[str, Any]] | None = None,
    unexpected_files: list[dict[str, Any]] | None = None,
    line_ending_only_files: list[dict[str, Any]] | None = None,
    encoding_corruption_files: list[dict[str, Any]] | None = None,
    diff_samples: list[dict[str, Any]] | None = None,
    extension_diffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest = manifest or {}
    summary = summary or {
        "modified_count": 0,
        "missing_count": 0,
        "unexpected_count": 0,
        "changed_count": 0,
        "line_ending_only_count": 0,
        "encoding_corruption_count": 0,
        "manifest_file_count": manifest.get("file_count", 0),
        "reused_hash_count": 0,
        "hashed_file_count": 0,
    }
    encoding_files = encoding_corruption_files or []
    status_warnings = list(warnings or [])
    if encoding_files and PROMPT_ENCODING_CORRUPTION_CODE not in status_warnings:
        status_warnings.append(PROMPT_ENCODING_CORRUPTION_CODE)
    return {
        "schema": INTEGRITY_STATUS_SCHEMA,
        "checked": checked,
        "checked_at": now_iso(),
        "skipped_reason": skipped_reason,
        "drift_detected": drift_detected,
        "cache_hit": cache_hit,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "root_label": _path_label(root) if root else "",
        "manifest_path": _path_label(manifest_path) if manifest_path else "",
        "manifest_hash": manifest_hash,
        "manifest_generated_at": manifest.get("generated_at", ""),
        "app_version": manifest.get("app_version", "unknown"),
        "summary": summary,
        "modified_files": modified_files or [],
        "missing_files": missing_files or [],
        "unexpected_files": unexpected_files or [],
        "line_ending_only_files": line_ending_only_files or [],
        "encoding_corruption_files": encoding_files,
        "diff_samples": diff_samples or [],
        "extension_diffs": extension_diffs or [],
        "warnings": status_warnings,
    }


def _skipped_status(
    reason: str,
    *,
    started: float,
    root: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    return _status(
        checked=False,
        skipped_reason=reason,
        started=started,
        root=root,
        manifest_path=manifest_path,
        warnings=[reason],
    )


def _raise_if_timeout(started: float, timeout_seconds: float) -> None:
    if timeout_seconds > 0 and time.monotonic() - started > timeout_seconds:
        raise TimeoutError


def _relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _path_label(path: Path | None) -> str:
    if not path:
        return ""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home):
        return "~" + text[len(home):]
    parts = path.parts
    if len(parts) > 4:
        return "/".join(parts[-4:])
    return text


def redact_snippet(value: Any, *, max_chars: int = MAX_SAMPLE_CHARS) -> str:
    text = str(value)
    text = re.sub(r"```.*?```", "[code omitted]", text, flags=re.DOTALL)
    text = _redact_sensitive_operational_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def redact_operational_text(value: Any, *, max_chars: int = MAX_EXTENSION_DIFF_CHARS) -> str:
    text = _redact_sensitive_operational_text(str(value))
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _redact_sensitive_operational_text(text: str) -> str:
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
    return re.sub(r"(?<!\w)/(?:Users|home|private|tmp|var|Volumes)/[^\s\"')]+", lambda m: _short_path(m.group(0)), text)


def _redact_url(url: str) -> str:
    if "?" not in url:
        return url
    head, _query = url.split("?", 1)
    return f"{head}?[redacted]"


def _short_path(value: str) -> str:
    parts = Path(value).parts
    if len(parts) <= 3:
        return value
    return "/.../" + "/".join(parts[-3:])


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
