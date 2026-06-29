"""Raw chat frontmatter and filesystem helpers."""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ConfigDict

from mednotes.domains.wiki.capabilities.notes.note_plan import note_plan_summary, parse_triage_note_plan
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    FrontmatterYamlUnavailable,
    load_frontmatter_yaml,
)
from mednotes.domains.wiki.common import FileWriteError, MissingPathError, ValidationError
from mednotes.kernel.base import ContractModel
from mednotes.platform.backup_policy import (
    DEFAULT_BACKUP_POLICY,
    BackupPolicy,
    cleanup_backups,
    policy_from_values,
)

_FRONTMATTER_DELIM = "---"
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")
_ATOMIC_WRITE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
_WINDOWS_LOCK_WINERRORS = {5, 32, 33}


class _RawSummaryDecisionFields(ContractModel):
    """Typed subset of raw chat summary fields used for status filtering."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    status: str = ""
    tipo: str = ""
    chat_id: str = ""


def split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            return lines[1:idx], "".join(lines[idx + 1 :])
    return None, text


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            pass
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    frontmatter, _body = split_frontmatter(text)
    if frontmatter is None:
        return {}
    parsed: dict[str, str] = {}
    for line in frontmatter:
        match = _KEY_RE.match(line.strip())
        if match:
            parsed[match.group(1)] = _strip_quotes(match.group(2))
    return parsed


def _format_yaml_value(value: str) -> str:
    if value == "":
        return '""'
    if re.match(r"^[A-Za-z0-9_./@+-]+$", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_frontmatter(text: str, updates: dict[str, str]) -> str:
    frontmatter, body = split_frontmatter(text)
    formatted = {key: f"{key}: {_format_yaml_value(value)}\n" for key, value in updates.items()}
    if frontmatter is None:
        return "---\n" + "".join(formatted.values()) + "---\n" + text

    seen: set[str] = set()
    out: list[str] = []
    for line in frontmatter:
        match = _KEY_RE.match(line.strip())
        if match and match.group(1) in formatted:
            key = match.group(1)
            out.append(formatted[key])
            seen.add(key)
        else:
            out.append(line)
    for key, line in formatted.items():
        if key not in seen:
            out.append(line)
    return "---\n" + "".join(out) + "---\n" + body


def read_note_meta(path: Path) -> dict[str, str]:
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"File not found: {path}") from exc


def create_backup(path: Path, *, policy: BackupPolicy = DEFAULT_BACKUP_POLICY) -> Path:
    policy.validate()
    if not path.exists():
        raise MissingPathError(f"File not found: {path}")
    raise RuntimeError("Markdown .bak backups are retired; use vault version control restore points.")


def prune_backup_files(
    root: Path,
    *,
    max_per_file: int | None = None,
    retention_days: int | None = None,
    policy: BackupPolicy = DEFAULT_BACKUP_POLICY,
) -> dict[str, Any]:
    resolved_policy = policy_from_values(
        max_per_file=max_per_file,
        retention_days=retention_days,
        policy=policy,
    )
    try:
        return cleanup_backups(root, policy=resolved_policy)
    except FileNotFoundError as exc:
        raise MissingPathError(str(exc)) from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _is_retryable_replace_error(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _WINDOWS_LOCK_WINERRORS:
        return True
    return getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM, errno.EBUSY}


def _replace_with_retries(path: Path, tmp: Path, retry_delays: tuple[float, ...]) -> None:
    attempts = len(retry_delays) + 1
    last_error: OSError | None = None
    for attempt_idx in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt_idx >= len(retry_delays) or not _is_retryable_replace_error(exc):
                break
            time.sleep(retry_delays[attempt_idx])

    raise FileWriteError(
        f"Could not atomically replace {path} after {attempts} attempts. "
        f"The file may be locked by Obsidian, iCloud Drive, antivirus, or another process. "
        f"Original error: {last_error}"
    ) from last_error


def atomic_write_text(path: Path, text: str, *, retry_delays: tuple[float, ...] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        _replace_with_retries(path, tmp, _ATOMIC_WRITE_RETRY_DELAYS if retry_delays is None else retry_delays)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def mutate_raw_frontmatter(raw_file: Path, updates: dict[str, str], dry_run: bool = False, backup: bool = False) -> dict[str, Any]:
    if not raw_file.exists():
        raise MissingPathError(f"Raw file not found: {raw_file}")
    original = raw_file.read_text(encoding="utf-8")
    updated = update_frontmatter(original, updates)
    if dry_run:
        return {"raw_file": str(raw_file), "backup": None, "updated": False, "updates": updates}
    atomic_write_text(raw_file, updated)
    return {"raw_file": str(raw_file), "backup": None, "updated": True, "updates": updates}


def list_raw_files(raw_dir: Path) -> list[Path]:
    if not raw_dir.exists():
        raise MissingPathError(f"Raw dir not found: {raw_dir}")
    return sorted(path for path in raw_dir.glob("*.md") if path.is_file())


def raw_summary(path: Path) -> dict[str, str]:
    meta = read_note_meta(path)
    chat_id = meta.get("fonte_id", "").strip()
    result = {
        "path": str(path),
        "status": meta.get("status", ""),
        "tipo": meta.get("tipo", ""),
        "titulo_triagem": meta.get("titulo_triagem", ""),
        "fonte_id": meta.get("fonte_id", ""),
        "chat_id": chat_id,
        "title": meta.get("titulo_triagem", ""),
        "url": f"https://gemini.google.com/app/{chat_id}" if chat_id else "",
        "date_created": meta.get("date_created", ""),
        "exported_at": meta.get("exported_at", ""),
    }
    raw_plan = meta.get("note_plan", "")
    if raw_plan:
        try:
            result.update({key: str(value) for key, value in note_plan_summary(parse_triage_note_plan(raw_plan, path)).items()})
        except ValidationError as exc:
            result["note_plan_error"] = str(exc)
    return result


def covered_raw_chat_index(wiki_dir: Path) -> dict[str, list[str]]:
    if not wiki_dir.exists():
        return {}
    index: dict[str, list[str]] = {}
    for note_path in sorted(wiki_dir.rglob("*.md")):
        if not note_path.is_file():
            continue
        try:
            frontmatter = load_frontmatter_yaml(note_path.read_text(encoding="utf-8"))
        except (OSError, FrontmatterYamlUnavailable):
            continue
        raw_chats = frontmatter.get("chats")
        if not isinstance(raw_chats, list):
            continue
        for chat in raw_chats:
            if not isinstance(chat, dict):
                continue
            chat_id = str(chat.get("id") or "").strip()
            if not chat_id:
                continue
            index.setdefault(chat_id, []).append(str(note_path))
    return index


def list_by_status(
    raw_dir: Path,
    mode: str,
    *,
    covered_raw_chat_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    covered_ids = covered_raw_chat_ids or set()
    rows: list[dict[str, str]] = []
    for path in list_raw_files(raw_dir):
        item = raw_summary(path)
        fields = _RawSummaryDecisionFields.model_validate(item)
        status = fields.status.lower()
        tipo = fields.tipo.lower()
        if mode == "pending" and status in {"", "pendente"}:
            raw_id = fields.chat_id or path.stem
            if raw_id in covered_ids:
                continue
            rows.append(item)
        elif mode == "triados" and status == "triado" and tipo == "medicina":
            rows.append(item)
    return rows
