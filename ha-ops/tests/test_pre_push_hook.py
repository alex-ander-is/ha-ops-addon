import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".githooks" / "pre-push"
ZERO_SHA = "0" * 40


def run(command, cwd, **kwargs):
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True, **kwargs)


class PrePushHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        python = self.fake_bin / "python3"
        python.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys\n'
            'from pathlib import Path\n'
            'if sys.argv[1:2] == ["dev/test_runner.py"]:\n'
            '    log = Path(os.environ["PYTHON_COMMAND_LOG"])\n'
            '    phase = len(log.read_text().splitlines()) + 1 if log.exists() else 1\n'
            '    with log.open("a") as stream:\n'
            '        stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            '    sys.exit(int(os.environ.get(f"HOOK_PHASE_{phase}_STATUS", "0")))\n'
            f'os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n'
        )
        python.chmod(0o755)
        self.python_command_log = self.root / "python-command.log"

        run(["git", "init", "--bare", str(self.remote)], cwd=self.root)
        run(["git", "init", "-b", "main", str(self.repo)], cwd=self.root)
        run(["git", "config", "user.email", "test@example.com"], cwd=self.repo)
        run(["git", "config", "user.name", "Test User"], cwd=self.repo)
        run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repo)
        self.write_file("ha-ops/config.yaml", 'version: "0.1.1"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.1.1\n\n- Initial.\n")
        self.write_file("ha-ops/app.py", "print('hello')\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "Initial"], cwd=self.repo)
        run(["git", "tag", "-a", "0.1.1", "-m", "HA Ops 0.1.1"], cwd=self.repo)
        self.base = self.rev_parse("HEAD")
        run(["git", "push", "origin", "main"], cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def write_file(self, relative_path, text):
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def rev_parse(self, ref):
        return run(["git", "rev-parse", ref], cwd=self.repo).stdout.strip()

    def commit(self, message):
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", message], cwd=self.repo)
        return self.rev_parse("HEAD")

    def release_commit(self, kind, version, note="Change"):
        self.write_file("ha-ops/config.yaml", f'version: "{version}"\n')
        self.write_file(
            "ha-ops/CHANGELOG.md",
            f"# Changelog\n\n## {version}\n\n- {note}.\n\n## 0.1.1\n\n- Initial.\n",
        )
        return self.commit(f"{note}\n\nRelease-Type: {kind}\nRelease-Impact: {note} impacts users")

    def invoke_hook(self, stdin, **overrides):
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env["PATH"]
        env["PYTHON_COMMAND_LOG"] = str(self.python_command_log)
        env.update(overrides)
        return subprocess.run(
            [str(HOOK), "origin", str(self.remote)],
            cwd=self.repo,
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
        )

    def branch_push_stdin(self, head):
        return f"refs/heads/main {head} refs/heads/main {self.base}\n"

    def test_rejects_main_push_without_config_bump(self):
        self.write_file("ha-ops/app.py", "print('changed')\n")
        head = self.commit("Change app")

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ha-ops/config.yaml", result.stderr)
        self.assertFalse(self.python_command_log.exists())

    def test_allows_hook_only_push_without_release_bump(self):
        self.write_file(".githooks/pre-push", "#!/bin/sh\nexit 0\n")
        head = self.commit("Change hook")

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running HA Ops tests before push", result.stdout)
        self.assertEqual(self.logged_commands(), self.expected_commands())

    @staticmethod
    def expected_commands():
        return [
            ["dev/test_runner.py", "--", "-n", "auto",
             "--ignore=ha-ops/tests/test_test_runner_guard.py",
             "--ignore=ha-ops/tests/test_test_plan.py", "ha-ops/tests"],
            ["dev/test_runner.py", "--", "-n", "2",
             "ha-ops/tests/test_test_runner_guard.py", "ha-ops/tests/test_test_plan.py"],
        ]

    def logged_commands(self):
        return [json.loads(line) for line in self.python_command_log.read_text().splitlines()]

    def test_ordinary_failure_preserves_status_and_stops_before_isolated_group(self):
        result = self.invoke_hook("", HOOK_PHASE_1_STATUS="37")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(self.logged_commands(), self.expected_commands()[:1])

    def test_isolated_failure_preserves_status(self):
        result = self.invoke_hook("", HOOK_PHASE_2_STATUS="43")
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertEqual(self.logged_commands(), self.expected_commands())

    def collect_phase(self, phase):
        command = self.expected_commands()[phase]
        return subprocess.run(
            [sys.executable, str(ROOT / command[0]), "--artifacts", str(self.root.resolve() / "evidence"),
             *command[1:], "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=self.repo, text=True, capture_output=True, timeout=30,
        )

    def test_recursive_ordinary_selector_discovers_new_tests(self):
        self.write_file("ha-ops/tests/test_future_ordinary.py", "def test_new_case(): pass\n")
        self.write_file("ha-ops/tests/test_test_runner_guard.py", "def test_guard(): pass\n")
        self.write_file("ha-ops/tests/test_test_plan.py", "def test_planner(): pass\n")
        result = self.collect_phase(0)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("test_future_ordinary.py", result.stdout)
        self.assertIn("test_new_case", result.stdout)
        self.assertNotIn("test_test_runner_guard.py", result.stdout)
        self.assertNotIn("test_test_plan.py", result.stdout)

    def test_missing_required_isolated_path_fails_qualification(self):
        for missing, present in (
            ("test_test_runner_guard.py", "test_test_plan.py"),
            ("test_test_plan.py", "test_test_runner_guard.py"),
        ):
            with self.subTest(missing=missing):
                (self.repo / "ha-ops/tests" / missing).unlink(missing_ok=True)
                self.write_file(f"ha-ops/tests/{present}", "def test_required(): pass\n")
                result = self.collect_phase(1)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(missing, result.stderr + result.stdout)

    def test_rejects_push_when_remote_main_moved(self):
        self.write_file("ha-ops/app.py", "print('remote change')\n")
        remote_head = self.commit("Remote update")
        run(["git", "push", "origin", "main"], cwd=self.repo)
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 10\n\n- Initial.\n")
        head = self.commit("Release from stale remote")
        run(["git", "tag", "0.2.0"], cwd=self.repo)

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"remote main moved from {self.base} to {remote_head}", result.stderr)

    def test_rejects_release_files_without_matching_tag(self):
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 0.1.1\n\n- Initial.\n")
        head = self.commit("Release without tag")

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing local tag 0.2.0", result.stderr)

    def test_allows_release_files_with_matching_tag_pushed_together(self):
        head = self.release_commit("minor", "0.2.0")
        run(["git", "tag", "-a", "0.2.0", "-m", "HA Ops 0.2.0"], cwd=self.repo)
        stdin = self.branch_push_stdin(head)
        stdin += f"refs/tags/0.2.0 {self.rev_parse('0.2.0')} refs/tags/0.2.0 {ZERO_SHA}\n"

        result = self.invoke_hook(stdin)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running HA Ops tests before push", result.stdout)
        self.assertNotIn("Verified minor release", result.stdout)

    def test_rejects_patch_classification_for_minor_bump(self):
        head = self.release_commit("patch", "0.2.0")
        run(["git", "tag", "-a", "0.2.0", "-m", "HA Ops 0.2.0"], cwd=self.repo)

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patch release from 0.1.1 must be 0.1.2", result.stderr)

    def test_rejects_release_without_impact_trailers(self):
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 0.1.1\n\n- Initial.\n")
        head = self.commit("Change")
        run(["git", "tag", "-a", "0.2.0", "-m", "HA Ops 0.2.0"], cwd=self.repo)

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Release-Type", result.stderr)

    def test_rejects_lightweight_release_tag(self):
        head = self.release_commit("minor", "0.2.0")
        run(["git", "tag", "0.2.0"], cwd=self.repo)

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be annotated", result.stderr)

    def test_accepts_major_bump_from_zero_series(self):
        head = self.release_commit("major", "1.0.0", note="Break old API")
        run(["git", "tag", "-a", "1.0.0", "-m", "HA Ops 1.0.0"], cwd=self.repo)
        stdin = self.branch_push_stdin(head)
        stdin += f"refs/tags/1.0.0 {self.rev_parse('1.0.0')} refs/tags/1.0.0 {ZERO_SHA}\n"

        result = self.invoke_hook(stdin)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_release_after_multiple_unpublished_versions(self):
        releases = (
            ("patch", "0.1.2"),
            ("minor", "0.2.0"),
            ("major", "1.0.0"),
            ("major", "2.0.0"),
            ("patch", "2.0.1"),
        )
        for kind, version in releases:
            self.write_file("ha-ops/config.yaml", f'version: "{version}"\n')
            changelog = self.repo / "ha-ops/CHANGELOG.md"
            current = changelog.read_text()
            self.write_file(
                "ha-ops/CHANGELOG.md",
                current.replace(
                    "# Changelog\n\n",
                    f"# Changelog\n\n## {version}\n\n- Release {version}.\n\n",
                    1,
                ),
            )
            head = self.commit(
                f"Release {version}\n\nRelease-Type: {kind}\nRelease-Impact: {version} changes behavior"
            )
            run(["git", "tag", "-a", version, "-m", f"HA Ops {version}"], cwd=self.repo)

        self.write_file("release-tooling.note", "Release tooling maintenance.\n")
        head = self.commit("Fix release tooling")

        stdin = self.branch_push_stdin(head)
        stdin += f"refs/tags/2.0.1 {self.rev_parse('2.0.1')} refs/tags/2.0.1 {ZERO_SHA}\n"

        result = self.invoke_hook(stdin)

        self.assertEqual(result.returncode, 0, result.stderr)

        self.write_file("ha-ops/app.py", "print('unreleased app change')\n")
        head = self.commit("Unreleased app change")
        rejected = self.invoke_hook(self.branch_push_stdin(head))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("HA Ops App changes after tag 2.0.1 need a new release", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
