#!/usr/bin/env python3
"""Run the local release-readiness checks.

Contract commands:
- python3 -m unittest discover -s tests -v
- python3 adapters/antigravity/build.py --output dist/antigravity/mednotes
- agy plugin validate dist/antigravity/mednotes
- python3 adapters/opencode/build.py --output dist/opencode/package
- npm pack --dry-run
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[1]


def run(command: List[str], cwd: Optional[Path] = None) -> None:
    display_cwd = cwd or ROOT
    print(f"$ {' '.join(command)}  # cwd={display_cwd}")
    subprocess.run(command, cwd=display_cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-agy",
        action="store_true",
        help="Skip agy plugin validate when Antigravity CLI is unavailable.",
    )
    args = parser.parse_args()

    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    run(
        [
            sys.executable,
            "adapters/antigravity/build.py",
            "--output",
            "dist/antigravity/mednotes",
        ]
    )

    if args.skip_agy:
        print("$ agy plugin validate dist/antigravity/mednotes  # skipped")
    elif shutil.which("agy"):
        run(["agy", "plugin", "validate", "dist/antigravity/mednotes"])
    else:
        raise SystemExit("agy is not installed; rerun with --skip-agy only in CI.")

    run(
        [
            sys.executable,
            "adapters/opencode/build.py",
            "--output",
            "dist/opencode/package",
        ]
    )
    run(["npm", "pack", "--dry-run"], cwd=ROOT / "dist" / "opencode" / "package")
    run(
        [
            "bun",
            "-e",
            "const m=await import('./dist/opencode/package/src/index.ts');"
            "const p=await m.default({$:()=>({}),directory:process.cwd()});"
            "if(typeof p['tool.execute.before']!=='function')"
            "throw new Error('missing opencode hook');",
        ]
    )
    run([sys.executable, "core/scripts/public_guard.py", "--root", ".", "--json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
