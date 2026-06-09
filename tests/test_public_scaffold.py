import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicScaffoldTests(unittest.TestCase):
    def test_public_guard_accepts_current_tree(self) -> None:
        guard = ROOT / "core" / "scripts" / "public_guard.py"

        result = subprocess.run(
            [sys.executable, str(guard), "--root", str(ROOT), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["issues"], [])

    def test_public_guard_blocks_private_lab_paths(self) -> None:
        guard = ROOT / "core" / "scripts" / "public_guard.py"

        with tempfile.TemporaryDirectory() as tmp:
            lab = Path(tmp) / "mednotes-lab"
            lab.mkdir()
            (lab / "draft.md").write_text("rascunho privado\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(guard), "--root", tmp, "--json"],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(
            any(issue["code"] == "private-path" for issue in payload["issues"])
        )

    def test_antigravity_build_copies_core_without_committing_dist(self) -> None:
        build = ROOT / "adapters" / "antigravity" / "build.py"

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            result = subprocess.run(
                [sys.executable, str(build), "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plugin = json.loads((output / "plugin.json").read_text(encoding="utf-8"))
            hooks = json.loads((output / "hooks.json").read_text(encoding="utf-8"))

            self.assertEqual(plugin["name"], "mednotes")
            self.assertTrue((output / "skills" / "mednotes-study.md").exists())
            self.assertTrue((output / "agents" / "study-coach.md").exists())
            self.assertTrue((output / "scripts" / "public_guard.py").exists())
            self.assertIn("PreToolUse", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
