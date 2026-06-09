#!/usr/bin/env python3
"""Build the Antigravity plugin bundle from core sources."""

import argparse
import json
import shutil
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
ADAPTER = ROOT / "adapters" / "antigravity"
DEFAULT_OUTPUT = ROOT / "dist" / "antigravity" / "mednotes"


def _copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Missing required source directory: {source}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _copy_files(files: Iterable[str], output: Path) -> None:
    for name in files:
        source = ADAPTER / name
        if not source.exists():
            raise FileNotFoundError(f"Missing adapter file: {source}")
        shutil.copy2(source, output / name)


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    output = output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    _copy_files(("plugin.json", "hooks.json"), output)
    _copy_tree(CORE / "skills", output / "skills")
    _copy_tree(CORE / "agents", output / "agents")
    _copy_tree(CORE / "scripts", output / "scripts")

    manifest = {
        "bundle": "mednotes",
        "output": str(output),
        "source": str(ROOT),
        "files": sorted(str(path.relative_to(output)) for path in output.rglob("*")),
    }
    (output / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = build(args.output)
    print(f"Built Antigravity bundle: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
