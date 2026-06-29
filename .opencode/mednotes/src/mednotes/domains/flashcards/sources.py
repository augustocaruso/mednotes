#!/usr/bin/env python3
"""Resolve /flashcards note scopes into a deterministic JSON manifest.

The Gemini agent owns flashcard reasoning and Anki writes. This script owns the
auditable filesystem step before that: expanding files/directories/globs/tags,
building portable Obsidian deeplinks, and deriving the destination deck for each
source note.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
MEDNOTES_DIR = SCRIPT_DIR.parent
if str(MEDNOTES_DIR) not in sys.path:
    sys.path.insert(0, str(MEDNOTES_DIR))

from mednotes.domains.flashcards.contracts import FlashcardSourceManifest, FlashcardSourceNote  # noqa: E402
from mednotes.domains.flashcards.obsidian_links import (  # noqa: E402
    build_obsidian_link_candidates,
    detect_path_style,
)
from mednotes.domains.flashcards.obsidian_note_utils import (  # noqa: E402
    EXIT_IO,
    EXIT_OK,
    MissingPathError,
    NoteUtilsError,
    UsageError,
    infer_vault_root,
    normalize_tag,
)
from mednotes.platform.feedback import command_string, safe_record_workflow_run  # noqa: E402
from mednotes.platform.paths import resolve_wiki_dir  # noqa: E402

SCHEMA = "medical-notes-workbench.flashcard-sources.v1"
DEFAULT_CONFIRM_FILE_LIMIT = 10
DEFAULT_CONFIRM_CARD_LIMIT = 40
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".obsidian",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "dist",
    "attachments",
    "assets",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
INLINE_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z0-9_/-]+)")
TAG_KEY_RE = re.compile(r"^\s*(tags?|Tags?)\s*:\s*(?P<value>.*)$")
LIST_ITEM_RE = re.compile(r"^\s*-\s*(?P<value>.*?)\s*$")
PATHISH_RE = re.compile(r"^([~./$]|[A-Za-z]:|.*[/\\]|.*\.(?:md|markdown)$|.*[*?\[\]]).*$")


@dataclass(frozen=True)
class Scope:
    raw: str
    explicit_inputs: tuple[str, ...]
    tags: tuple[str, ...]
    skip_tags: tuple[str, ...]
    folders: tuple[str, ...]
    tag_match: str


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _dedupe_preserve(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return tuple(result)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _existing_pathish_token_span(tokens: list[str], start: int) -> tuple[str, int] | None:
    max_span = min(len(tokens), start + 16)
    for end in range(max_span, start, -1):
        candidate = " ".join(tokens[start:end])
        path = _path(candidate)
        if path.exists() or (_has_glob(str(path)) and glob.glob(str(path), recursive=True)):
            return candidate, end
    return None


def _join_existing_path_tokens(tokens: list[str]) -> list[str]:
    joined: list[str] = []
    idx = 0
    while idx < len(tokens):
        span = _existing_pathish_token_span(tokens, idx)
        if span:
            value, idx = span
            joined.append(value)
            continue
        joined.append(tokens[idx])
        idx += 1
    return joined


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


def _parse_tag_values(value: str) -> list[str]:
    value = _strip_inline_comment(value).strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    raw_items = [part.strip() for part in value.split(",")] if "," in value else [value]
    tags: list[str] = []
    for raw in raw_items:
        item = _strip_quotes(raw).lstrip("#").strip()
        if item:
            tags.append(normalize_tag(item))
    return tags


def split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[1:idx], "".join(lines[idx + 1 :])
    return None, text


def extract_frontmatter_tags(text: str) -> list[str]:
    frontmatter, _body = split_frontmatter(text)
    if frontmatter is None:
        return []

    tags: list[str] = []
    idx = 0
    while idx < len(frontmatter):
        line = frontmatter[idx]
        match = TAG_KEY_RE.match(line)
        if not match:
            idx += 1
            continue
        value = _strip_inline_comment(match.group("value"))
        if value:
            tags.extend(_parse_tag_values(value))
            idx += 1
            continue
        idx += 1
        while idx < len(frontmatter):
            item = LIST_ITEM_RE.match(frontmatter[idx])
            if not item:
                break
            item_value = _strip_inline_comment(item.group("value"))
            tags.extend(_parse_tag_values(item_value))
            idx += 1

    return list(dict.fromkeys(tags))


def extract_inline_tags(text: str) -> list[str]:
    _frontmatter, body = split_frontmatter(text)
    tags: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in INLINE_TAG_RE.finditer(line):
            try:
                tags.append(normalize_tag(match.group(1)))
            except UsageError:
                continue
    return list(dict.fromkeys(tags))


def _has_glob(value: str) -> bool:
    return any(char in value for char in "*?[")


def _ignored_path(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def _markdown_files_under(directory: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        root_path = Path(root)
        if _ignored_path(root_path):
            continue
        for name in sorted(names):
            candidate = root_path / name
            if candidate.suffix.lower() in MARKDOWN_EXTENSIONS and not _ignored_path(candidate):
                files.append(candidate.resolve())
    return files


def _expand_input(value: str, *, strict: bool = True) -> list[Path]:
    path = _path(value)
    matches: list[Path] = []
    if _has_glob(str(path)):
        matches = [Path(match) for match in glob.glob(str(path), recursive=True)]
        if not matches and strict:
            raise MissingPathError(f"No files matched glob: {value}")
    elif path.exists():
        matches = [path]
    elif strict:
        raise MissingPathError(f"Path not found: {value}")
    else:
        return []

    files: list[Path] = []
    for match in matches:
        resolved = match.resolve()
        if resolved.is_dir():
            files.extend(_markdown_files_under(resolved))
        elif resolved.is_file() and resolved.suffix.lower() in MARKDOWN_EXTENSIONS:
            files.append(resolved)
        elif strict and resolved.exists():
            raise UsageError(f"Expected Markdown file or directory, got: {resolved}")
    return files


def _normalize_scope(args: argparse.Namespace) -> Scope:
    raw_parts = list(args.scope or [])
    raw = " ".join(raw_parts).strip()
    tokens: list[str] = []
    for part in raw_parts:
        try:
            tokens.extend(shlex.split(part, posix=os.name != "nt"))
        except ValueError:
            tokens.extend(part.split())
    tokens = _join_existing_path_tokens(tokens)
    looks_like_note_query = any(
        token.lower() in {"nota", "notas", "arquivo", "arquivos"} for token in tokens
    )

    explicit_inputs = list(args.inputs or [])
    tags = [normalize_tag(tag) for tag in (args.tag or [])]
    skip_tags = [normalize_tag(tag) for tag in (args.skip_tag or [])]
    folders = list(args.folder or [])

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        lower = token.lower()
        if token.startswith("#"):
            tags.append(normalize_tag(token))
        elif lower in {"tag", "tags"} and idx + 1 < len(tokens):
            tags.append(normalize_tag(tokens[idx + 1]))
            idx += 1
        elif lower in {"pasta", "folder"} and idx + 1 < len(tokens):
            folders.append(tokens[idx + 1])
            idx += 1
        elif (
            lower in {"em", "na", "no", "dentro"}
            and idx + 2 < len(tokens)
            and tokens[idx + 1].lower() in {"pasta", "folder"}
        ):
            folders.append(tokens[idx + 2])
            idx += 2
        elif lower in {"em", "na", "no", "dentro"} and idx + 1 < len(tokens):
            candidate = tokens[idx + 1]
            if PATHISH_RE.match(candidate) or _path(candidate).exists():
                explicit_inputs.append(candidate)
            elif looks_like_note_query:
                folders.append(candidate)
            idx += 1
        elif PATHISH_RE.match(token) or _path(token).exists():
            explicit_inputs.append(token)
        idx += 1

    return Scope(
        raw=raw,
        explicit_inputs=_dedupe_preserve(explicit_inputs),
        tags=_dedupe_preserve(tags),
        skip_tags=_dedupe_preserve(skip_tags),
        folders=_dedupe_preserve(folders),
        tag_match=args.tag_match,
    )


def _root_from_args(args: argparse.Namespace) -> Path | None:
    root_value = args.vault_root or args.wiki_dir
    resolution = resolve_wiki_dir(explicit=root_value, config=args.config, enable_gemini_probe=False)
    return resolution.path if resolution.ok else None


def _find_folder(root: Path, name_or_path: str) -> Path:
    direct = _path(name_or_path)
    if direct.exists():
        if not direct.is_dir():
            raise UsageError(f"Expected folder path, got: {direct}")
        return direct.resolve()

    matches: list[Path] = []
    target = name_or_path.casefold()
    for current, dirs, _names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for dirname in dirs:
            if dirname.casefold() == target:
                matches.append((Path(current) / dirname).resolve())

    if not matches:
        raise MissingPathError(f"Folder not found under {root}: {name_or_path}")
    if len(matches) > 1:
        joined = "\n".join(f"- {match}" for match in matches[:20])
        raise UsageError(f"Ambiguous folder name {name_or_path!r}; pass a full path. Matches:\n{joined}")
    return matches[0]


def _candidate_files(scope: Scope, args: argparse.Namespace) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    files: list[Path] = []

    for raw_input in scope.explicit_inputs:
        expanded = _expand_input(raw_input, strict=not args.scope)
        if not expanded and args.scope:
            warnings.append(f"No Markdown files matched input: {raw_input}")
        files.extend(expanded)

    root = _root_from_args(args)
    if scope.folders:
        if root is None:
            raise UsageError(
                "Folder filters need --vault-root, --wiki-dir, or app config.toml [paths].wiki_dir."
            )
        for folder in scope.folders:
            files.extend(_markdown_files_under(_find_folder(root, folder)))

    if scope.tags and not files:
        if root is None:
            raise UsageError(
                "Tag filters need --vault-root, --wiki-dir, or app config.toml [paths].wiki_dir."
            )
        files.extend(_markdown_files_under(root))

    if not files and scope.raw:
        warnings.append("No Markdown note scope was resolved; treat the input as pasted text or ask for a path.")

    deduped = sorted(set(files), key=lambda item: item.as_posix().casefold())
    return deduped, warnings


def _note_matches_tags(tags: set[str], desired: tuple[str, ...], mode: str) -> bool:
    if not desired:
        return True
    desired_set = set(desired)
    if mode == "any":
        return bool(tags & desired_set)
    return desired_set.issubset(tags)


def _deck_for_note(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    parts = [root.name, *relative.with_suffix("").parts]
    return "::".join(parts)


def _fallback_deck_for_note(path: Path) -> str:
    parent = path.parent.name if path.parent.name else "Inbox"
    return f"Medicina::{parent}::{path.stem}"


def _manifest_note(path: Path, args: argparse.Namespace) -> FlashcardSourceNote:
    text = path.read_text(encoding="utf-8")
    root = infer_vault_root(path, explicit_root=args.vault_root or args.wiki_dir)
    configured = _root_from_args(args)
    if root is None and configured and _is_relative_to(path, configured):
        root = configured

    frontmatter_tags = extract_frontmatter_tags(text)
    inline_tags = extract_inline_tags(text)
    all_tags = list(dict.fromkeys([*frontmatter_tags, *inline_tags]))
    absolute_path = str(path)
    path_style = detect_path_style(absolute_path)
    relative = path.relative_to(root).as_posix() if root else path.name
    vault_name = (args.vault_name or root.name) if root else ""
    link_candidates = build_obsidian_link_candidates(
        absolute_path=absolute_path,
        path_style=path_style,
        vault_name=vault_name,
        vault_relative_path=relative if root else "",
    )
    selected = link_candidates[0]
    heading_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))

    return FlashcardSourceNote.model_validate(
        {
            "path": str(path),
            "title": path.stem,
            "absolute_path": absolute_path,
            "path_style": str(path_style),
            "vault_root": str(root) if root else "",
            "vault_name": vault_name,
            "vault_relative_path": relative,
            "link_mode": str(selected.mode),
            "deeplink": selected.uri,
            "deeplink_mode": str(selected.mode),
            "deeplink_candidates": [candidate.uri for candidate in link_candidates],
            "deck": _deck_for_note(path, root) if root else _fallback_deck_for_note(path),
            "frontmatter_tags": frontmatter_tags,
            "inline_tags": inline_tags,
            "tags": all_tags,
            "already_marked_anki": "anki" in all_tags,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "line_count": len(text.splitlines()),
            "heading_count": heading_count,
        }
    )


def resolve_manifest(args: argparse.Namespace) -> dict[str, Any]:
    scope = _normalize_scope(args)
    files, warnings = _candidate_files(scope, args)
    candidate_notes = [_manifest_note(path, args) for path in files]
    tag_matched_notes = [
        note
        for note in candidate_notes
        if _note_matches_tags(set(note.tags), scope.tags, scope.tag_match)
    ]
    skipped_notes: list[FlashcardSourceNote] = []
    notes: list[FlashcardSourceNote] = []
    skip_tags = set(scope.skip_tags)
    for note in tag_matched_notes:
        matched_skip_tags = sorted(set(note.tags) & skip_tags)
        if matched_skip_tags:
            skipped_notes.append(
                FlashcardSourceNote.model_validate(
                    {
                        **note.to_payload(),
                        "skip_reason": "skip_tag",
                        "skip_tags": matched_skip_tags,
                    }
                )
            )
        else:
            notes.append(note)

    confirmation_reasons: list[str] = []
    if len(notes) > args.confirm_file_limit:
        confirmation_reasons.append(f"more_than_{args.confirm_file_limit}_files")

    scope_root = _root_from_args(args)
    manifest = {
        "schema": SCHEMA,
        "dry_run": args.dry_run,
        "scope": {
            "raw": scope.raw,
            "inputs": list(scope.explicit_inputs),
            "tags": list(scope.tags),
            "skip_tags": list(scope.skip_tags),
            "folders": list(scope.folders),
            "tag_match": scope.tag_match,
            "vault_root": str(scope_root) if scope_root else "",
            "vault_name": args.vault_name or "",
        },
        "summary": {
            "candidate_file_count": len(tag_matched_notes),
            "file_count": len(notes),
            "skipped_count": len(skipped_notes),
            "requires_confirmation": bool(confirmation_reasons),
            "confirmation_reasons": confirmation_reasons,
            "card_candidate_confirmation_limit": args.confirm_card_limit,
        },
        "notes": [note.to_payload() for note in notes],
        "skipped_notes": [note.to_payload() for note in skipped_notes],
        "warnings": warnings,
    }
    manifest_model = FlashcardSourceManifest.model_validate(manifest)
    return manifest_model.to_payload()


def _cmd_resolve(args: argparse.Namespace) -> int:
    started_at = time.time()
    manifest = resolve_manifest(args)
    _json(manifest)
    _record_feedback(manifest, EXIT_OK, started_at, phase="flashcards_sources_resolve")
    return EXIT_OK


def format_preview(manifest: dict[str, Any]) -> str:
    summary = manifest["summary"]
    scope = manifest["scope"]
    lines = [
        "Flashcard source preview",
        f"- Processar: {summary['file_count']} nota(s)",
        f"- Puladas: {summary['skipped_count']} nota(s)",
        f"- Candidatas antes dos filtros de pulo: {summary['candidate_file_count']} nota(s)",
        f"- Confirmacao necessaria: {'sim' if summary['requires_confirmation'] else 'nao'}",
    ]
    if scope["tags"]:
        lines.append(f"- Tags exigidas: {', '.join(scope['tags'])}")
    if scope["skip_tags"]:
        lines.append(f"- Tags puladas: {', '.join(scope['skip_tags'])}")
    if scope["folders"]:
        lines.append(f"- Pastas: {', '.join(scope['folders'])}")

    if manifest["notes"]:
        lines.append("")
        lines.append("Notas que serao processadas:")
        for note in manifest["notes"]:
            lines.append(f"- {note['vault_relative_path']} -> {note['deck']}")

    if manifest["skipped_notes"]:
        lines.append("")
        lines.append("Notas puladas:")
        for note in manifest["skipped_notes"]:
            reason = note.get("skip_reason", "skip")
            tags = ", ".join(note.get("skip_tags", []))
            suffix = f" ({reason}: {tags})" if tags else f" ({reason})"
            lines.append(f"- {note['vault_relative_path']}{suffix}")

    if manifest["warnings"]:
        lines.append("")
        lines.append("Avisos:")
        lines.extend(f"- {warning}" for warning in manifest["warnings"])

    return "\n".join(lines) + "\n"


def _cmd_preview(args: argparse.Namespace) -> int:
    started_at = time.time()
    manifest = resolve_manifest(args)
    print(format_preview(manifest), end="")
    _record_feedback(manifest, EXIT_OK, started_at, phase="flashcards_sources_preview")
    return EXIT_OK


def _record_feedback(manifest: dict[str, Any], exit_code: int, started_at: float, *, phase: str) -> None:
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    safe_record_workflow_run(
        workflow="/flashcards",
        command=command_string(),
        payload={
            **manifest,
            "phase": phase,
            "status": "completed_with_warnings" if manifest.get("warnings") or summary.get("requires_confirmation") else "completed",
            "next_action": "Confirmar escopo amplo antes de criar cards." if summary.get("requires_confirmation") else "",
            "required_inputs": ["notes", "scope"],
        },
        exit_code=exit_code,
        started_at=started_at,
    )


def _add_resolve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="*", help="explicit Markdown files, directories, or globs")
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="free-form /flashcards argument text to parse for paths, folders, and #tags",
    )
    parser.add_argument("--tag", action="append", default=[], help="Obsidian tag filter")
    parser.add_argument("--skip-tag", action="append", default=[], help="exclude notes with this Obsidian tag")
    parser.add_argument("--folder", action="append", default=[], help="folder name/path filter")
    parser.add_argument("--tag-match", choices=("all", "any"), default="all")
    parser.add_argument("--vault-root", help="Obsidian vault/wiki root used for links and deck names")
    parser.add_argument("--wiki-dir", help="alias for --vault-root in this project")
    parser.add_argument("--vault-name", help="override the vault name encoded in obsidian:// links")
    parser.add_argument("--config", help="optional config.toml; legacy fallback for wiki_dir/vault.path")
    parser.add_argument("--dry-run", action="store_true", help="mark the manifest as preview-only")
    parser.add_argument("--confirm-file-limit", type=int, default=DEFAULT_CONFIRM_FILE_LIMIT)
    parser.add_argument("--confirm-card-limit", type=int, default=DEFAULT_CONFIRM_CARD_LIMIT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="resolve files/folders/globs/tags into a JSON manifest")
    _add_resolve_arguments(resolve)
    resolve.set_defaults(func=_cmd_resolve)

    preview = sub.add_parser("preview", help="print a human-readable preview for a resolved /flashcards scope")
    _add_resolve_arguments(preview)
    preview.set_defaults(func=_cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NoteUtilsError as exc:
        safe_record_workflow_run(
            workflow="/flashcards",
            command=command_string(),
            payload={
                "phase": f"flashcards_sources_{getattr(args, 'command', 'unknown')}",
                "status": "failed",
                "blocked_reason": exc.__class__.__name__,
                "next_action": "Corrigir o escopo de fontes e rodar /flashcards novamente.",
                "error": str(exc),
            },
            exit_code=exc.exit_code,
            started_at=time.time(),
            snippets=[str(exc)],
        )
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        safe_record_workflow_run(
            workflow="/flashcards",
            command=command_string(),
            payload={
                "phase": f"flashcards_sources_{getattr(args, 'command', 'unknown')}",
                "status": "failed",
                "blocked_reason": "OSError",
                "next_action": "Corrigir caminho/permissao e rodar /flashcards novamente.",
                "error": str(exc),
            },
            exit_code=EXIT_IO,
            started_at=time.time(),
            snippets=[str(exc)],
        )
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
