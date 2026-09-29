import os
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
        for name in ("release", "publish-release", "release-policy.py"):
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
        result = run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
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

    def test_publish_validates_against_previous_release_when_multiple_releases_are_ahead(self):
        versions = (
            ("patch", "0.11.1"),
            ("major", "1.0.0"),
            ("major", "2.0.0"),
            ("patch", "2.0.1"),
        )
        for kind, version in versions:
            config = self.repo / "ha-ops/config.yaml"
            config.write_text(f'version: "{version}"\n')
            changelog = self.repo / "ha-ops/CHANGELOG.md"
            current = changelog.read_text()
            changelog.write_text(current.replace(
                "# Changelog\n\n",
                f"# Changelog\n\n## {version}\n\n- Release {version}.\n\n",
                1,
            ))
            run(["git", "add", "ha-ops/config.yaml", "ha-ops/CHANGELOG.md"], self.repo)
            commit = run([
                "git", "commit", "-m", f"Release {version}",
                "-m", f"Release-Type: {kind}",
                "-m", f"Release-Impact: {version} changes user behavior",
            ], self.repo)
            self.assertEqual(commit.returncode, 0, commit.stderr)
            tag = run(["git", "tag", "-a", version, "-m", f"HA Ops {version}"], self.repo)
            self.assertEqual(tag.returncode, 0, tag.stderr)

        (self.repo / "publish-release.note").write_text("Release tooling maintenance.\n")
        run(["git", "add", "publish-release.note"], self.repo)
        maintenance = run(["git", "commit", "-m", "Fix release tooling"], self.repo)
        self.assertEqual(maintenance.returncode, 0, maintenance.stderr)

        fake_bin = self.repo.parent / f"{self.repo.name}-fake-bin"
        fake_bin.mkdir()
        push_log = self.repo.parent / f"{self.repo.name}-push.log"
        real_git = shutil.which("git")
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "push" ]; then printf "%s\\n" "$*" >> "$PUBLISH_PUSH_LOG"; exit 0; fi\n'
            f'exec "{real_git}" "$@"\n'
        )
        fake_git.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["PUBLISH_PUSH_LOG"] = str(push_log)

        result = run(["./publish-release"], self.repo, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("push origin main 2.0.1", push_log.read_text())

        (self.repo / "ha-ops/app.py").write_text("print('unreleased app change')\n")
        run(["git", "add", "ha-ops/app.py"], self.repo)
        app_change = run(["git", "commit", "-m", "Unreleased app change"], self.repo)
        self.assertEqual(app_change.returncode, 0, app_change.stderr)
        rejected = run(["./publish-release"], self.repo, env=env)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("HA Ops App changes after tag 2.0.1 need a new release", rejected.stderr)
        self.assertEqual(push_log.read_text().count("push origin main 2.0.1"), 1)


if __name__ == "__main__":
    unittest.main()
