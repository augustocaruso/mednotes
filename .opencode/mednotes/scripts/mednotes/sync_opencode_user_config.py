#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXTENSION_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_SRC = EXTENSION_ROOT / "src"
if str(BUNDLE_SRC) not in sys.path:
    sys.path.insert(0, str(BUNDLE_SRC))

from mednotes.platform.opencode_runtime_config import sync_opencode_user_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync MedNotes TOML model settings into an OpenCode project.")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--user-config", type=Path, default=None)
    args = parser.parse_args()

    try:
        payload = sync_opencode_user_config(
            project_root=args.project,
            user_config_path=args.user_config,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
