#!/usr/bin/env python3
"""Check that the repository tree is safe to package publicly."""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional


SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
}

PRIVATE_PATH_PARTS = {
    "mednotes-lab",
    "private",
    "secrets",
}

SECRET_FILE_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
}

SECRET_TEXT_MARKERS = tuple(
    "".join(parts)
    for parts in (
        ("BEGIN ", "PRIVATE KEY"),
        ("AWS_SECRET", "_ACCESS_KEY"),
        ("GOOGLE_APPLICATION", "_CREDENTIALS"),
    )
)


Issue = Dict[str, str]


def _relative_parts(path: Path, root: Path) -> Iterable[str]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _should_skip(path: Path, root: Path) -> bool:
    return any(part in SKIP_DIRS for part in _relative_parts(path, root))


def _issue(code: str, path: Path, root: Path, message: str) -> Issue:
    try:
        display_path = str(path.relative_to(root))
    except ValueError:
        display_path = str(path)
    return {"code": code, "path": display_path, "message": message}


def _scan_text_file(path: Path, root: Path) -> List[Issue]:
    issues: List[Issue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return issues
    except OSError as exc:
        issues.append(_issue("read-error", path, root, str(exc)))
        return issues

    for marker in SECRET_TEXT_MARKERS:
        if marker in text:
            issues.append(
                _issue(
                    "secret-marker",
                    path,
                    root,
                    "File contains a known secret marker.",
                )
            )
    return issues


def scan(root: Path) -> List[Issue]:
    root = root.resolve()
    issues: List[Issue] = []

    if not root.exists():
        return [_issue("missing-root", root, root, "Root path does not exist.")]

    for path in root.rglob("*"):
        if _should_skip(path, root):
            continue

        parts = {part.lower() for part in _relative_parts(path, root)}
        private_part = parts.intersection(PRIVATE_PATH_PARTS)
        if private_part:
            issues.append(
                _issue(
                    "private-path",
                    path,
                    root,
                    "Private or lab-only path is not allowed in the public tree.",
                )
            )

        if path.is_file() and path.name in SECRET_FILE_NAMES:
            issues.append(
                _issue(
                    "secret-file",
                    path,
                    root,
                    "Secret-bearing config files must not be packaged publicly.",
                )
            )

        if path.is_file():
            issues.extend(_scan_text_file(path, root))

    return issues


def build_payload(root: Path) -> Dict[str, object]:
    issues = scan(root)
    return {
        "status": "blocked" if issues else "ok",
        "root": str(root.resolve()),
        "issues": issues,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    payload = build_payload(Path(args.root))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload["status"])
        for issue in payload["issues"]:
            print(f"{issue['code']}: {issue['path']} - {issue['message']}")

    return 1 if payload["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
