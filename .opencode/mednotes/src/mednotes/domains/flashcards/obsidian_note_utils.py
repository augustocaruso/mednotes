#!/usr/bin/env python3
"""Deterministic Obsidian note metadata helpers for Gemini extension agents.

The flashcard agent owns card reasoning. This script owns small, auditable note
metadata operations: creating Obsidian deeplinks and adding/removing frontmatter
tags after Anki writes succeed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mednotes.domains.flashcards.contracts import FlashcardsTaggingReceipt, FlashcardsVaultGuardReceipt
from mednotes.platform.paths import resolve_wiki_dir

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING = 4
EXIT_IO = 5

_DELIM = "---"
_TAG_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>tags?|Tags?)\s*:\s*(?P<value>.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(?P<value>.*?)\s*$")
_VALID_TAG_RE = re.compile(r"^[A-Za-z0-9_/-]+$")


class NoteUtilsError(Exception):
    exit_code = EXIT_IO


class UsageError(NoteUtilsError):
    exit_code = EXIT_USAGE


class MissingPathError(NoteUtilsError):
    exit_code = EXIT_MISSING


class FlashcardsTagAuthorization(BaseModel):
    """Typed guard proving an Obsidian tag write came from the FSM effect path."""

    model_config = ConfigDict(extra="forbid", strict=True)

    effect_target: Literal["flashcards.tag_obsidian"]
    receipt_path: Path
    workflow: Literal["/flashcards"]
    effect_kind: Literal["run_subworkflow"]
    receipt_effect_target: Literal["flashcards.tag_obsidian"]
    vault_guard_active: bool = Field(strict=True)
    rollback_declared: bool = Field(strict=True)
    receipt_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _requires_flashcards_effect_receipt(self) -> FlashcardsTagAuthorization:
        if not self.vault_guard_active:
            raise ValueError("vault guard receipt must prove resource_guard_active")
        if not self.rollback_declared:
            raise ValueError("vault guard receipt must declare rollback")
        return self


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _resolve_existing_file(value: str | os.PathLike[str]) -> Path:
    path = _path(value)
    if not path.exists():
        raise MissingPathError(f"File not found: {path}")
    if not path.is_file():
        raise UsageError(f"Expected a file path, got: {path}")
    return path.resolve()


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _candidate_vault_roots(path: Path, explicit_root: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_root:
        candidates.append(_path(explicit_root).resolve())

    resolution = resolve_wiki_dir(context_paths=[path], enable_gemini_probe=False)
    if resolution.ok and resolution.path:
        candidates.append(resolution.path.resolve())

    for parent in (path.parent, *path.parents):
        if (parent / ".obsidian").is_dir():
            candidates.append(parent.resolve())
        if parent.name == "Wiki_Medicina":
            candidates.append(parent.resolve())

    seen: set[Path] = set()
    roots: list[Path] = []
    for candidate in candidates:
        if candidate not in seen and _is_relative_to(path, candidate):
            roots.append(candidate)
            seen.add(candidate)
    return roots


def infer_vault_root(path: Path, explicit_root: str | None = None) -> Path | None:
    roots = _candidate_vault_roots(path, explicit_root)
    if not roots:
        return None
    return max(roots, key=lambda root: len(root.parts))


def obsidian_deeplink(
    path: Path,
    *,
    vault_root: str | None = None,
    vault_name: str | None = None,
    pane_type: str | None = None,
    absolute_path: bool = True,
    fallback_to_absolute_path: bool = True,
) -> str:
    """Return an Obsidian URI for a note path.

    By default, use the real resolved filesystem path. The caller already has
    access to the note file, so the link should not depend on guessing which
    vault contains it. Passing ``absolute_path=False`` opts into the portable
    `vault` + vault-relative `file` form when a vault root is inferable.
    """
    resolved = path.resolve()
    if absolute_path:
        encoded_path = quote(str(resolved), safe="")
        uri = f"obsidian://open?path={encoded_path}"
    else:
        root = infer_vault_root(resolved, explicit_root=vault_root)
        if root is None:
            if not fallback_to_absolute_path:
                raise UsageError(
                    "Could not infer the Obsidian vault root. Pass --vault-root or create a .obsidian "
                    "directory in the vault."
                )
            encoded_path = quote(str(resolved), safe="")
            uri = f"obsidian://open?path={encoded_path}"
        else:
            name = vault_name or root.name
            file_path = resolved.relative_to(root).as_posix()
            uri = f"obsidian://open?vault={quote(name, safe='')}&file={quote(file_path, safe='')}"
    if pane_type:
        uri += f"&paneType={quote(pane_type, safe='')}"
    return uri


def _split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _DELIM:
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _DELIM:
            return lines[1:idx], "".join(lines[idx + 1 :])
    return None, text


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_inline_comment(value: str) -> str:
    quote_char: str | None = None
    bracket_depth = 0
    for idx, char in enumerate(value):
        if char in {"'", '"'}:
            quote_char = None if quote_char == char else char
        elif char == "[" and quote_char is None:
            bracket_depth += 1
        elif char == "]" and quote_char is None and bracket_depth:
            bracket_depth -= 1
        if (
            char == "#"
            and quote_char is None
            and bracket_depth == 0
            and idx > 0
            and value[idx - 1].isspace()
        ):
            return value[:idx].rstrip()
    return value.strip()


def normalize_tag(tag: str) -> str:
    normalized = tag.strip().lstrip("#").strip()
    if not normalized:
        raise UsageError("Tag cannot be empty")
    if not _VALID_TAG_RE.match(normalized):
        raise UsageError(
            f"Unsupported tag {tag!r}; use Obsidian-style tags with letters, numbers, _, / or -"
        )
    return normalized


def _parse_inline_tags(value: str) -> list[str]:
    value = _strip_inline_comment(value).strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    raw_items = [part.strip() for part in value.split(",")] if "," in value else [value]
    return [normalize_tag(_strip_quotes(item)) for item in raw_items if _strip_quotes(item)]


def _find_tags_block(frontmatter: list[str]) -> tuple[int, int] | None:
    for idx, line in enumerate(frontmatter):
        match = _TAG_KEY_RE.match(line)
        if not match or match.group("indent"):
            continue
        value = _strip_inline_comment(match.group("value"))
        if value:
            return idx, idx + 1
        end = idx + 1
        while end < len(frontmatter) and _LIST_ITEM_RE.match(frontmatter[end]):
            end += 1
        return idx, end
    return None


def _read_tags_from_block(block: list[str]) -> list[str]:
    if not block:
        return []
    first = _TAG_KEY_RE.match(block[0])
    if not first:
        return []
    value = _strip_inline_comment(first.group("value"))
    raw_tags: list[str] = []
    if value:
        raw_tags.extend(_parse_inline_tags(value))
    else:
        for line in block[1:]:
            item = _LIST_ITEM_RE.match(line)
            if item:
                raw_tags.append(normalize_tag(_strip_quotes(_strip_inline_comment(item.group("value")))))

    seen: set[str] = set()
    tags: list[str] = []
    for tag in raw_tags:
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _format_yaml_tag(tag: str) -> str:
    if _VALID_TAG_RE.match(tag):
        return tag
    return json.dumps(tag, ensure_ascii=False)


def _format_tags_block(tags: list[str]) -> list[str]:
    return ["tags:\n", *(f"  - {_format_yaml_tag(tag)}\n" for tag in tags)]


def _mutate_frontmatter_tags(text: str, tag: str, action: str) -> tuple[str, list[str]]:
    target = normalize_tag(tag)
    frontmatter, body = _split_frontmatter(text)

    if frontmatter is None:
        if action == "remove-tag":
            return text, []
        frontmatter = _format_tags_block([target])
        return "---\n" + "".join(frontmatter) + "---\n" + text, [target]

    block_range = _find_tags_block(frontmatter)
    existing = _read_tags_from_block(frontmatter[block_range[0] : block_range[1]]) if block_range else []
    tags = list(existing)

    if action == "add-tag" and target not in tags:
        tags.append(target)
    elif action == "remove-tag":
        tags = [item for item in tags if item != target]

    if block_range:
        start, end = block_range
        replacement = _format_tags_block(tags) if tags else []
        frontmatter = [*frontmatter[:start], *replacement, *frontmatter[end:]]
    elif tags:
        frontmatter = [*frontmatter, *_format_tags_block(tags)]

    if not any(line.strip() for line in frontmatter):
        return body, tags
    return "---\n" + "".join(frontmatter) + "---\n" + body, tags


def _load_flashcards_tag_authorization(
    *,
    effect_target: str | None,
    receipt_path: str | None,
    dry_run: bool,
) -> FlashcardsTagAuthorization | None:
    """Require the official effect + vault receipt before any real note write."""

    if dry_run:
        return None
    if not effect_target or not receipt_path:
        raise UsageError(
            "Obsidian tag writes require WorkflowEffect target flashcards.tag_obsidian "
            "and --vault-guard-receipt."
        )
    receipt_file = _resolve_existing_file(receipt_path)
    try:
        vault_guard_receipt = FlashcardsVaultGuardReceipt.model_validate_json(
            receipt_file.read_text(encoding="utf-8")
        )
        return FlashcardsTagAuthorization.model_validate(
            {
                "effect_target": effect_target,
                "receipt_path": receipt_file,
                "workflow": vault_guard_receipt.workflow,
                "effect_kind": vault_guard_receipt.effect_kind,
                "receipt_effect_target": vault_guard_receipt.effect_target,
                "vault_guard_active": vault_guard_receipt.resource_guard_active,
                "rollback_declared": vault_guard_receipt.rollback_declared,
                "receipt_id": vault_guard_receipt.receipt_id,
            }
        )
    except json.JSONDecodeError as exc:
        raise UsageError(f"Invalid vault guard receipt JSON: {receipt_file}") from exc
    except ValidationError as exc:
        raise UsageError(f"Invalid vault guard receipt JSON: {receipt_file}: {exc}") from exc


def mutate_note_tag(
    path: Path,
    tag: str,
    action: str,
    *,
    dry_run: bool = False,
    backup: bool = False,
    authorization: FlashcardsTagAuthorization | None = None,
) -> dict[str, object]:
    old_text = path.read_text(encoding="utf-8")
    new_text, tags = _mutate_frontmatter_tags(old_text, tag, action)
    changed = new_text != old_text
    backup_file: Path | None = None

    if changed and not dry_run and authorization is None:
        raise UsageError(
            "Refusing direct Obsidian tag mutation without WorkflowEffect target "
            "flashcards.tag_obsidian and vault guard receipt."
        )
    if changed and not dry_run:
        _atomic_write_text(path, new_text)

    record: dict[str, object] = {
        "path": str(path),
        "action": action,
        "tag": normalize_tag(tag),
        "changed": changed,
        "dry_run": dry_run,
        "backup": str(backup_file) if backup_file else None,
        "tags": tags,
    }
    if authorization is not None:
        record["effect_target"] = authorization.effect_target
        record["version_control_safety"] = {
            "no_resource_mutation": False,
            "rollback_declared": authorization.rollback_declared,
            "resource_guard_active": authorization.vault_guard_active,
            "direct_mutation_forbidden": True,
            "mutation_without_guard": False,
            "changed_file_count": 1 if changed else 0,
        }
    if changed:
        record["link_trigger_context"] = {
            "schema": "medical-notes-workbench.link-trigger-context.v1",
            "reason": "obsidian_metadata_tag_changed",
            "workflow": "/flashcards",
            "path": str(path),
            "visual_only": False,
        }
    return record


def _cmd_deeplink(args: argparse.Namespace) -> int:
    records = []
    for raw_path in args.paths:
        path = _resolve_existing_file(raw_path)
        records.append(
            {
                "path": str(path),
                "deeplink": obsidian_deeplink(
                    path,
                    vault_root=args.vault_root,
                    vault_name=args.vault_name,
                    pane_type=args.pane_type,
                    absolute_path=args.absolute_path,
                ),
            }
        )
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_tag(args: argparse.Namespace) -> int:
    authorization = _load_flashcards_tag_authorization(
        effect_target=args.effect_target,
        receipt_path=args.vault_guard_receipt,
        dry_run=args.dry_run,
    )
    records = []
    for raw_path in args.paths:
        path = _resolve_existing_file(raw_path)
        records.append(
            mutate_note_tag(
                path,
                args.tag,
                args.action,
                dry_run=args.dry_run,
                backup=False,
                authorization=authorization,
            )
        )
    changed_files = [
        str(record["path"])
        for record in records
        if bool(record["changed"] if "changed" in record else False)
    ]
    vault_guard_receipt = (
        {
            "receipt_path": str(authorization.receipt_path),
            "receipt_id": authorization.receipt_id,
            "resource_guard_active": authorization.vault_guard_active,
            "rollback_declared": authorization.rollback_declared,
            "effect_target": authorization.effect_target,
        }
        if authorization is not None
        else {}
    )
    receipt = FlashcardsTaggingReceipt(
        status="dry_run" if args.dry_run else ("completed" if changed_files else "completed_noop"),
        changed_files=changed_files,
        tag=normalize_tag(args.tag),
        vault_guard_receipt=vault_guard_receipt,
        records=records,
    )
    print(json.dumps(receipt.to_payload(), ensure_ascii=False, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    deeplink = subparsers.add_parser("deeplink", help="emit Obsidian deeplink JSON for note paths")
    deeplink.add_argument("paths", nargs="+", help="Markdown note paths")
    deeplink.add_argument(
        "--vault-root",
        default=None,
        help=(
            "vault root path used with --vault-file; defaults to persistent "
            "app config.toml [paths].wiki_dir, legacy config/env, nearest .obsidian "
            "parent, or Wiki_Medicina"
        ),
    )
    deeplink.add_argument(
        "--vault-name",
        default=None,
        help="vault name to encode in the URI; defaults to the inferred vault root folder name",
    )
    deeplink.add_argument(
        "--absolute-path",
        action="store_true",
        default=True,
        help="emit obsidian://open?path=... using the real note path; this is the default",
    )
    deeplink.add_argument(
        "--vault-file",
        dest="absolute_path",
        action="store_false",
        help="emit obsidian://open?vault=...&file=... when a vault root can be inferred",
    )
    deeplink.add_argument(
        "--pane-type",
        choices=("tab", "split", "window"),
        default=None,
        help="optional Obsidian paneType parameter",
    )
    deeplink.set_defaults(func=_cmd_deeplink)

    for action in ("add-tag", "remove-tag"):
        tag_parser = subparsers.add_parser(action, help=f"{action} in note frontmatter")
        tag_parser.add_argument("paths", nargs="+", help="Markdown note paths")
        tag_parser.add_argument("--tag", default="anki", help="frontmatter tag, default: anki")
        tag_parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
        tag_parser.add_argument(
            "--effect-target",
            default=None,
            help="official WorkflowEffect target authorizing a real Obsidian tag write",
        )
        tag_parser.add_argument(
            "--vault-guard-receipt",
            default=None,
            help="JSON receipt proving vault guard and rollback safety for the effect",
        )
        tag_parser.set_defaults(func=_cmd_tag)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NoteUtilsError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
