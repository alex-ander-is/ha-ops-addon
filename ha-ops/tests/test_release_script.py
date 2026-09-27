import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run(args, cwd, **kwargs):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, **kwargs)


class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "ha-ops").mkdir()
        (self.repo / "ha-ops/config.yaml").write_text('version: "0.11.0"\n')
        (self.repo / "ha-ops/CHANGELOG.md").write_text("# Changelog\n\n## 0.11.0\n\n- Previous release.\n")
        for name in ("release", "release-policy.py"):
            shutil.copy2(ROOT / name, self.repo / name)
        for args in (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "test@example.invalid"],
            ["git", "config", "user.name", "Test User"],
            ["git", "remote", "add", "origin", "git@github.com:alex-ander-is/ha-ops-addon.git"],
            ["git", "add", "."],
            ["git", "commit", "-m", "Previous release"],
            ["git", "tag", "-a", "0.11.0", "-m", "HA Ops 0.11.0"],
        ):
            result = run(args, self.repo)
            self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def test_help_describes_classification_and_no_publish_option(self):
        result = run(["./release", "--help"], self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--kind major|minor|patch [version]", result.stdout)
        self.assertIn("N leaves release files staged", result.stdout)
        self.assertIn("./publish-release", result.stdout)

    def test_each_release_kind_computes_one_expected_increment(self):
        for kind, expected in (("major", "1.0.0"), ("minor", "0.12.0"), ("patch", "0.11.1")):
            with self.subTest(kind=kind):
                result = run([sys.executable, "release-policy.py", "next", "0.11.0", kind], self.repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_major_release_from_zero_series_prepares_one_dot_zero(self):
        result = run(
            ["./release", "--kind", "major", "1.0.0"], self.repo,
            input="Break old API\n- Replace the old API.\n.\nN\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('version: "1.0.0"', (self.repo / "ha-ops/config.yaml").read_text())
        self.assertIn("## 1.0.0", (self.repo / "ha-ops/CHANGELOG.md").read_text())
        self.assertIn("ha-ops/config.yaml", run(["git", "diff", "--cached", "--name-only"], self.repo).stdout)
        self.assertEqual(run(["git", "tag", "-l", "1.0.0"], self.repo).stdout, "")
        self.assertEqual(run(["git", "rev-list", "--count", "HEAD"], self.repo).stdout.strip(), "1")

    def test_empty_changelog_restores_release_files_before_confirmation(self):
        result = run(["./release", "--kind", "patch"], self.repo, input="Internal maintenance\n.\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changelog needs a nonempty line", result.stderr)
        self.assertEqual((self.repo / "ha-ops/config.yaml").read_text(), 'version: "0.11.0"\n')
        self.assertEqual(run(["git", "status", "--porcelain"], self.repo).stdout, "")

    def test_failure_after_config_edit_restores_files_and_index(self):
        malformed = "# Changelog without a version section\n"
        (self.repo / "ha-ops/CHANGELOG.md").write_text(malformed)
        self.assertEqual(run(["git", "add", "ha-ops/CHANGELOG.md"], self.repo).returncode, 0)
        self.assertEqual(run(["git", "commit", "-m", "Malformed test fixture"], self.repo).returncode, 0)

        result = run(
            ["./release", "--kind", "patch"], self.repo,
            input="Internal maintenance\n- Tighten release checks.\n.\n",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changelog has no version section", result.stderr)
        self.assertEqual((self.repo / "ha-ops/config.yaml").read_text(), 'version: "0.11.0"\n')
        self.assertEqual((self.repo / "ha-ops/CHANGELOG.md").read_text(), malformed)
        self.assertEqual(run(["git", "status", "--porcelain"], self.repo).stdout, "")

    def test_explicit_version_must_match_kind(self):
        result = run(["./release", "--kind", "patch", "0.12.0"], self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patch release from 0.11.0 must be 0.11.1", result.stderr)


if __name__ == "__main__":
    unittest.main()
