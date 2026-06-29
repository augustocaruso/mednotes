"""Git-derived change context for the Wiki linker.

This module never emits raw diffs or note body snippets. It turns Git metadata
into the same trigger-context shape that workflow-aware callers already use.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from mednotes.domains.wiki.batch_state import canonical_json_hash
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import _now_iso
from mednotes.domains.wiki.contracts.link_git import (
    GIT_TRIGGER_SOURCE,
    LINK_GIT_CONTEXT_SCHEMA,
    LINK_STATE_SCHEMA,
    LINK_STATE_SCHEMA_V2,
    LinkGitChangedPath,
    LinkGitChangeEvent,
    LinkGitContext,
    LinkState,
    LinkTriggerContextFromGit,
)
from mednotes.kernel.base import JsonObject
from mednotes.platform.paths import user_state_dir

__all__ = [
    "GIT_TRIGGER_SOURCE",
    "LINK_GIT_CONTEXT_SCHEMA",
    "LINK_STATE_SCHEMA",
    "LINK_STATE_SCHEMA_V2",
    "LinkGitChangedPath",
    "LinkGitChangeEvent",
    "LinkGitContext",
    "LinkState",
    "LinkTriggerContextFromGit",
    "collect_git_context",
    "default_link_state_path",
    "load_link_state",
    "trigger_context_from_git",
    "write_link_state",
]


def default_link_state_path() -> Path:
    return user_state_dir() / "link-state.json"


def load_link_state(path: Path | None = None) -> LinkState | None:
    """Read persisted linker state and validate it before workflow use."""

    state_path = path or default_link_state_path()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LinkState.model_validate(data)
    except PydanticValidationError:
        return None


def write_link_state(
    *,
    snapshot_hash: str,
    git_context: LinkGitContext,
    receipt_path: Path,
    path: Path | None = None,
) -> LinkState:
    """Persist the compact typed state used to detect redundant diagnoses."""

    previous = load_link_state(path)
    state_payload: JsonObject = {
        "schema": LINK_STATE_SCHEMA_V2,
        "generated_at": _now_iso(),
        "snapshot_hash": snapshot_hash,
        "git_head": git_context.head,
        "git_status_hash": git_context.status_hash,
        "receipt_path": str(receipt_path),
    }
    if previous is not None and previous.last_diagnosis_attempt is not None:
        state_payload["last_diagnosis_attempt"] = previous.last_diagnosis_attempt
    state = LinkState.model_validate(state_payload)
    state_path = path or default_link_state_path()
    atomic_write_text(state_path, json.dumps(state.to_payload(), ensure_ascii=False, indent=2) + "\n")
    return state


def collect_git_context(wiki_dir: Path, *, previous_state: LinkState | None = None) -> LinkGitContext:
    """Collect Git state and normalize it before linker diagnosis can branch."""

    repo_root = _repo_root(wiki_dir)
    if repo_root is None:
        return _unavailable("git_repository_not_available")

    head = _git_text(repo_root, ["rev-parse", "--verify", "HEAD"], allow_fail=True)
    branch = _git_text(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], allow_fail=True)
    previous_head = previous_state.git_head if previous_state is not None else ""
    events: list[LinkGitChangeEvent] = []
    if previous_head and head and previous_head != head:
        events.extend(_diff_events(repo_root, wiki_dir, previous_head, head))
    if head:
        events.extend(_worktree_events(repo_root, wiki_dir, head))
    else:
        events.extend(_untracked_events(repo_root, wiki_dir))
    events = _coalesce_delete_create_renames(_dedupe_events(events))

    context_payload = {
        "schema": LINK_GIT_CONTEXT_SCHEMA,
        "available": True,
        "repo_root": str(repo_root),
        "branch": branch,
        "head": head,
        "previous_link_head": previous_head,
        "dirty": bool(events),
        "changed_note_count": len(events),
        "changed_notes": events,
        "changed_paths": _changed_paths(events),
        "trigger_context_available": bool(events),
    }
    context_payload["status_hash"] = "sha256:" + canonical_json_hash(
        {
            "repo_root": str(repo_root),
            "branch": branch,
            "head": head,
            "changed_notes": [event.to_payload() for event in events],
        }
    )
    return LinkGitContext.model_validate(context_payload)


def trigger_context_from_git(git_context: LinkGitContext) -> LinkTriggerContextFromGit | None:
    if not git_context.available or not git_context.changed_notes:
        return None
    return LinkTriggerContextFromGit.model_validate({
        "schema": "medical-notes-workbench.link-trigger-context.v1",
        "source_workflow": GIT_TRIGGER_SOURCE,
        "changed_notes": git_context.changed_notes,
        "catalog_changed": False,
        "related_notes_export_changed": False,
    })


def _unavailable(reason: str) -> LinkGitContext:
    return LinkGitContext.model_validate({
        "schema": LINK_GIT_CONTEXT_SCHEMA,
        "available": False,
        "unavailable_reason": reason,
        "repo_root": "",
        "branch": "",
        "head": "",
        "previous_link_head": "",
        "dirty": False,
        "changed_note_count": 0,
        "changed_notes": [],
        "changed_paths": [],
        "trigger_context_available": False,
        "status_hash": "",
    })


def _repo_root(wiki_dir: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(wiki_dir), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def _git_text(repo_root: Path, args: list[str], *, allow_fail: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        if allow_fail:
            return ""
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_bytes(
    repo_root: Path,
    args: list[str],
    *,
    allow_fail: bool = False,
    input_bytes: bytes | None = None,
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        if allow_fail:
            return b""
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _wiki_prefix(repo_root: Path, wiki_dir: Path) -> str:
    try:
        rel = wiki_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return ""
    return "." if rel == "." else rel


def _repo_to_wiki_path(repo_root: Path, wiki_dir: Path, repo_path: str) -> str:
    prefix = _wiki_prefix(repo_root, wiki_dir)
    normalized = repo_path.replace("\\", "/").strip("/")
    if prefix and prefix != ".":
        prefix = prefix.strip("/")
        if normalized == prefix:
            return ""
        if not normalized.startswith(prefix + "/"):
            return ""
        normalized = normalized[len(prefix) + 1 :]
    return normalized


def _is_note_relpath(rel_path: str) -> bool:
    if not rel_path or not rel_path.endswith(".md"):
        return False
    parts = Path(rel_path).parts
    if any(part.startswith(".") for part in parts):
        return False
    name = parts[-1] if parts else ""
    return ".bak" not in name and ".rewrite" not in name


def _diff_events(
    repo_root: Path,
    wiki_dir: Path,
    base_ref: str,
    target_ref: str | None = None,
) -> list[LinkGitChangeEvent]:
    prefix = _wiki_prefix(repo_root, wiki_dir)
    args = ["diff", "--name-status", "--find-renames=70%", "-z", base_ref]
    if target_ref:
        args.append(target_ref)
    args.extend(["--", prefix])
    output = _git_bytes(repo_root, args, allow_fail=True)
    return _events_from_name_status(repo_root, wiki_dir, output, base_ref=base_ref, target_ref=target_ref)


def _worktree_events(repo_root: Path, wiki_dir: Path, head: str) -> list[LinkGitChangeEvent]:
    return [*_diff_events(repo_root, wiki_dir, head), *_untracked_events(repo_root, wiki_dir)]


def _untracked_events(repo_root: Path, wiki_dir: Path) -> list[LinkGitChangeEvent]:
    prefix = _wiki_prefix(repo_root, wiki_dir)
    output = _git_bytes(repo_root, ["ls-files", "--others", "--exclude-standard", "-z", "--", prefix], allow_fail=True)
    events: list[LinkGitChangeEvent] = []
    for raw_path in _nul_items(output):
        rel = _repo_to_wiki_path(repo_root, wiki_dir, raw_path)
        if not _is_note_relpath(rel):
            continue
        path = wiki_dir / rel
        events.append(
            _clean_event(
                {
                    "change_type": "created",
                    "content_change": "text",
                    "path": rel,
                    "title": _title_from_file(path),
                    "after_hash": _hash_file(path),
                }
            )
        )
    return events


def _events_from_name_status(
    repo_root: Path,
    wiki_dir: Path,
    output: bytes,
    *,
    base_ref: str,
    target_ref: str | None,
) -> list[LinkGitChangeEvent]:
    items = _nul_items(output)
    blob_hashes = _blob_hashes_from_name_status(repo_root, output, base_ref=base_ref)
    events: list[LinkGitChangeEvent] = []
    index = 0
    while index < len(items):
        status = items[index]
        index += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"} and index + 1 < len(items):
            old_repo = items[index]
            new_repo = items[index + 1]
            index += 2
            event = _rename_event(
                repo_root,
                wiki_dir,
                old_repo,
                new_repo,
                base_ref=base_ref,
                target_ref=target_ref,
                blob_hashes=blob_hashes,
            )
        elif index < len(items):
            repo_path = items[index]
            index += 1
            event = _single_path_event(
                repo_root,
                wiki_dir,
                code,
                repo_path,
                base_ref=base_ref,
                target_ref=target_ref,
                blob_hashes=blob_hashes,
            )
        else:
            break
        if event is not None:
            events.append(event)
    return events


def _blob_hashes_from_name_status(repo_root: Path, output: bytes, *, base_ref: str) -> dict[tuple[str, str], str]:
    if not base_ref:
        return {}
    items = _nul_items(output)
    repo_paths: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(items):
        status = items[index]
        index += 1
        if not status:
            continue
        code = status[0]
        repo_path = ""
        if code in {"R", "C"} and index + 1 < len(items):
            repo_path = items[index]
            index += 2
        elif index < len(items):
            candidate = items[index]
            index += 1
            if code in {"D", "M", "T"}:
                repo_path = candidate
        else:
            break
        if repo_path and repo_path not in seen:
            repo_paths.append(repo_path)
            seen.add(repo_path)
    return _hash_blobs(repo_root, base_ref, repo_paths)


def _hash_blobs(repo_root: Path, ref: str, repo_paths: list[str]) -> dict[tuple[str, str], str]:
    if not ref or not repo_paths:
        return {}
    input_bytes = "".join(f"{ref}:{repo_path}\n" for repo_path in repo_paths).encode("utf-8")
    output = _git_bytes(repo_root, ["cat-file", "--batch"], allow_fail=True, input_bytes=input_bytes)
    hashes: dict[tuple[str, str], str] = {}
    offset = 0
    for repo_path in repo_paths:
        line_end = output.find(b"\n", offset)
        if line_end < 0:
            break
        header = output[offset:line_end].decode("utf-8", errors="replace")
        offset = line_end + 1
        header_parts = header.rsplit(" ", 2)
        if len(header_parts) != 3 or header_parts[1] != "blob":
            continue
        try:
            size = int(header_parts[2])
        except ValueError:
            continue
        blob = output[offset : offset + size]
        if len(blob) != size:
            break
        hashes[(ref, repo_path)] = _hash_note_bytes(blob)
        offset += size
        if output[offset : offset + 1] == b"\n":
            offset += 1
    return hashes


def _single_path_event(
    repo_root: Path,
    wiki_dir: Path,
    code: str,
    repo_path: str,
    *,
    base_ref: str,
    target_ref: str | None,
    blob_hashes: Mapping[tuple[str, str], str],
) -> LinkGitChangeEvent | None:
    rel = _repo_to_wiki_path(repo_root, wiki_dir, repo_path)
    if not _is_note_relpath(rel):
        return None
    path = wiki_dir / rel
    if code == "D":
        return _clean_event(
            {
                "change_type": "deleted",
                "content_change": "structural",
                "old_path": rel,
                "old_title": Path(rel).stem,
                "before_hash": _hash_blob_cached(repo_root, base_ref, repo_path, blob_hashes),
            }
        )
    if code in {"A", "C"}:
        return _clean_event(
            {
                "change_type": "created",
                "content_change": "text",
                "path": rel,
                "title": _title_from_file_or_blob(repo_root, path, target_ref, repo_path),
                "after_hash": _hash_file_or_blob(repo_root, path, target_ref, repo_path),
            }
        )
    if code in {"M", "T"}:
        return _clean_event(
            {
                "change_type": "modified",
                "content_change": "text",
                "path": rel,
                "title": _title_from_file_or_blob(repo_root, path, target_ref, repo_path),
                "before_hash": _hash_blob_cached(repo_root, base_ref, repo_path, blob_hashes),
                "after_hash": _hash_file_or_blob(repo_root, path, target_ref, repo_path),
            }
        )
    return None


def _rename_event(
    repo_root: Path,
    wiki_dir: Path,
    old_repo_path: str,
    new_repo_path: str,
    *,
    base_ref: str,
    target_ref: str | None,
    blob_hashes: Mapping[tuple[str, str], str],
) -> LinkGitChangeEvent | None:
    old_rel = _repo_to_wiki_path(repo_root, wiki_dir, old_repo_path)
    new_rel = _repo_to_wiki_path(repo_root, wiki_dir, new_repo_path)
    if not _is_note_relpath(old_rel) or not _is_note_relpath(new_rel):
        return None
    old_stem = Path(old_rel).stem
    new_stem = Path(new_rel).stem
    new_path = wiki_dir / new_rel
    return _clean_event(
        {
            "change_type": "moved" if old_stem == new_stem else "renamed",
            "content_change": "structural",
            "old_path": old_rel,
            "old_title": old_stem,
            "path": new_rel,
            "title": _title_from_file_or_blob(repo_root, new_path, target_ref, new_repo_path),
            "replacement_path": new_rel,
            "replacement_title": new_stem,
            "before_hash": _hash_blob_cached(repo_root, base_ref, old_repo_path, blob_hashes),
            "after_hash": _hash_file_or_blob(repo_root, new_path, target_ref, new_repo_path),
        }
    )


def _coalesce_delete_create_renames(events: list[LinkGitChangeEvent]) -> list[LinkGitChangeEvent]:
    deletes = [item for item in events if item.change_type == "deleted" and item.before_hash]
    creates = [item for item in events if item.change_type == "created" and item.after_hash]
    used_deletes: set[int] = set()
    used_creates: set[int] = set()
    replacements: list[LinkGitChangeEvent] = []
    for delete_index, deleted in enumerate(deletes):
        match_index = next(
            (
                create_index
                for create_index, created in enumerate(creates)
                if create_index not in used_creates and created.after_hash == deleted.before_hash
            ),
            None,
        )
        if match_index is None:
            continue
        created = creates[match_index]
        old_rel = deleted.old_path
        new_rel = created.path
        if not old_rel or not new_rel:
            continue
        old_stem = Path(old_rel).stem
        new_stem = Path(new_rel).stem
        replacements.append(
            _clean_event(
                {
                    "change_type": "moved" if old_stem == new_stem else "renamed",
                    "content_change": "structural",
                    "old_path": old_rel,
                    "old_title": old_stem,
                    "path": new_rel,
                    "title": created.title or new_stem,
                    "replacement_path": new_rel,
                    "replacement_title": new_stem,
                    "before_hash": deleted.before_hash,
                    "after_hash": created.after_hash,
                }
            )
        )
        used_deletes.add(delete_index)
        used_creates.add(match_index)

    skipped_delete_keys = {
        (deletes[index].old_path, deletes[index].before_hash)
        for index in used_deletes
    }
    skipped_create_keys = {
        (creates[index].path, creates[index].after_hash)
        for index in used_creates
    }
    output: list[LinkGitChangeEvent] = []
    for event in events:
        if event.change_type == "deleted" and (event.old_path, event.before_hash) in skipped_delete_keys:
            continue
        if event.change_type == "created" and (event.path, event.after_hash) in skipped_create_keys:
            continue
        output.append(event)
    output.extend(replacements)
    return _dedupe_events(output)


def _dedupe_events(events: list[LinkGitChangeEvent]) -> list[LinkGitChangeEvent]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[LinkGitChangeEvent] = []
    for event in events:
        key = (event.change_type, event.old_path, event.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _changed_paths(events: list[LinkGitChangeEvent]) -> list[LinkGitChangedPath]:
    paths: list[LinkGitChangedPath] = []
    for event in events:
        paths.append(
            LinkGitChangedPath.model_validate(
                {
                    "change_type": event.change_type,
                    "old_path": event.old_path,
                    "path": event.path,
                }
            )
        )
    return paths


def _nul_items(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]


def _title_from_file(path: Path) -> str:
    try:
        return _title_from_text(path.read_text(encoding="utf-8"), fallback=path.stem)
    except OSError:
        return path.stem


def _title_from_file_or_blob(repo_root: Path, path: Path, ref: str | None, repo_path: str) -> str:
    if path.is_file():
        return _title_from_file(path)
    if ref:
        text = _blob_text(repo_root, ref, repo_path)
        if text:
            return _title_from_text(text, fallback=Path(repo_path).stem)
    return path.stem


def _title_from_text(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def _hash_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return _hash_note_bytes(path.read_bytes())


def _hash_file_or_blob(repo_root: Path, path: Path, ref: str | None, repo_path: str) -> str:
    if path.is_file():
        return _hash_file(path)
    return _hash_blob(repo_root, ref, repo_path) if ref else ""


def _hash_blob_cached(
    repo_root: Path,
    ref: str | None,
    repo_path: str,
    blob_hashes: Mapping[tuple[str, str], str],
) -> str:
    if not ref:
        return ""
    key = (ref, repo_path)
    if key in blob_hashes:
        return blob_hashes[key]
    return _hash_blob(repo_root, ref, repo_path)


def _hash_blob(repo_root: Path, ref: str | None, repo_path: str) -> str:
    if not ref:
        return ""
    data = _git_bytes(repo_root, ["show", f"{ref}:{repo_path}"], allow_fail=True)
    return _hash_note_bytes(data) if data else ""


def _hash_note_bytes(data: bytes) -> str:
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return "sha256:" + sha256(normalized).hexdigest()


def _blob_text(repo_root: Path, ref: str, repo_path: str) -> str:
    data = _git_bytes(repo_root, ["show", f"{ref}:{repo_path}"], allow_fail=True)
    return data.decode("utf-8", errors="replace") if data else ""


def _clean_event(event: object) -> LinkGitChangeEvent:
    return LinkGitChangeEvent.model_validate(event)
