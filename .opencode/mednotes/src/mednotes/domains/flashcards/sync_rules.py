#!/usr/bin/env python3
"""Compare or update the vendored Anki MCP Twenty Rules prompt."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROMPT_RELATIVE = Path("dist/mcp/primitives/essential/prompts/twenty-rules.prompt/content.md")
UPSTREAM_PACKAGE = "@ankimcp/anki-mcp-server"


def _default_local_prompt() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        source_tree = parent / "extension" / "docs" / "anki-mcp-twenty-rules.md"
        if source_tree.exists():
            return source_tree
        bundled = parent / "docs" / "anki-mcp-twenty-rules.md"
        if bundled.exists():
            return bundled
    return current.parents[4] / "extension" / "docs" / "anki-mcp-twenty-rules.md"


LOCAL_PROMPT = _default_local_prompt()

EXIT_OK = 0
EXIT_DIFFERENT = 1
EXIT_USAGE = 2
EXIT_MISSING = 4
EXIT_IO = 5


class SyncError(Exception):
    exit_code = EXIT_IO


class UsageError(SyncError):
    exit_code = EXIT_USAGE


class MissingSourceError(SyncError):
    exit_code = EXIT_MISSING


def _json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _node_resolve_package() -> Path | None:
    try:
        result = subprocess.run(
            ["node", "-p", f"require.resolve('{UPSTREAM_PACKAGE}/package.json')"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    package_json = Path(result.stdout.strip())
    return package_json.parent if package_json.exists() else None


def _npm_global_package() -> Path | None:
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    package = root / UPSTREAM_PACKAGE
    return package if package.exists() else None


def find_upstream_source(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(_path(explicit))
    env_value = os.getenv("ANKI_MCP_TWENTY_RULES_PATH")
    if env_value:
        candidates.append(_path(env_value))
    for package_root in (_node_resolve_package(), _npm_global_package()):
        if package_root:
            candidates.append(package_root / PROMPT_RELATIVE)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    searched = "\n".join(f"- {candidate}" for candidate in candidates) or "- no candidates"
    raise MissingSourceError(f"Could not find upstream Twenty Rules prompt. Searched:\n{searched}")


def compare_prompts(local: Path, upstream: Path) -> dict[str, Any]:
    local_text = local.read_text(encoding="utf-8")
    upstream_text = upstream.read_text(encoding="utf-8")
    diff = list(
        difflib.unified_diff(
            local_text.splitlines(),
            upstream_text.splitlines(),
            fromfile=str(local),
            tofile=str(upstream),
            lineterm="",
        )
    )
    return {
        "local": str(local),
        "upstream": str(upstream),
        "changed": bool(diff),
        "diff": "\n".join(diff),
    }


def _cmd_check(args: argparse.Namespace) -> int:
    local = _path(args.local)
    upstream = find_upstream_source(args.source)
    result = compare_prompts(local, upstream)
    if args.json:
        _json(result)
    elif result["changed"]:
        print(result["diff"])
    else:
        print(f"Twenty Rules copy is up to date: {local}")
    return EXIT_DIFFERENT if result["changed"] else EXIT_OK


def _cmd_write(args: argparse.Namespace) -> int:
    local = _path(args.local)
    upstream = find_upstream_source(args.source)
    text = upstream.read_text(encoding="utf-8")
    local.write_text(text, encoding="utf-8")
    _json({"local": str(local), "upstream": str(upstream), "written": True})
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="compare local prompt with upstream prompt")
    check.add_argument("--source", help="explicit upstream content.md path")
    check.add_argument("--local", default=str(LOCAL_PROMPT), help="local vendored prompt path")
    check.add_argument("--json", action="store_true", help="emit JSON instead of unified diff")
    check.set_defaults(func=_cmd_check)

    write = sub.add_parser("write", help="replace local prompt with upstream prompt")
    write.add_argument("--source", help="explicit upstream content.md path")
    write.add_argument("--local", default=str(LOCAL_PROMPT), help="local vendored prompt path")
    write.set_defaults(func=_cmd_write)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
