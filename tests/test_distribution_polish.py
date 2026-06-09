import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DistributionPolishTests(unittest.TestCase):
    def test_opencode_build_packages_core_for_npm_distribution(self) -> None:
        build = ROOT / "adapters" / "opencode" / "build.py"

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "opencode-package"
            result = subprocess.run(
                [sys.executable, str(build), "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            package = json.loads((output / "package.json").read_text(encoding="utf-8"))
            plugin = (output / "src" / "index.ts").read_text(encoding="utf-8")

            self.assertEqual(package["name"], "@augusto/mednotes")
            self.assertIn("core", package["files"])
            self.assertTrue((output / "core" / "scripts" / "public_guard.py").exists())
            self.assertTrue((output / "core" / "skills" / "mednotes-study.md").exists())
            self.assertIn('new URL("../core/scripts/public_guard.py"', plugin)
            self.assertNotIn("${directory}/core/scripts", plugin)

    def test_verify_script_runs_the_release_readiness_contract(self) -> None:
        verify = ROOT / "scripts" / "verify.py"
        text = verify.read_text(encoding="utf-8")

        self.assertIn("unittest discover -s tests -v", text)
        self.assertIn("adapters/antigravity/build.py", text)
        self.assertIn("agy plugin validate", text)
        self.assertIn("adapters/opencode/build.py", text)
        self.assertIn("npm pack --dry-run", text)

    def test_github_surface_is_release_ready(self) -> None:
        required = [
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/pull_request_template.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "docs/architecture.md",
            "docs/release.md",
        ]

        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).exists(), relative_path)

    def test_readme_has_public_install_and_status_sections(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("https://github.com/augustocaruso/mednotes/actions", readme)
        self.assertIn("Antigravity", readme)
        self.assertIn("opencode", readme)
        self.assertIn("python3 scripts/verify.py", readme)
        self.assertIn("agy plugin install https://github.com/augustocaruso/mednotes", readme)
        self.assertIn('"plugin": ["@augusto/mednotes"]', readme)


if __name__ == "__main__":
    unittest.main()
