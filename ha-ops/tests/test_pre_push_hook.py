import os
import subprocess
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
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)

        run(["git", "init", "--bare", str(self.remote)], cwd=self.root)
        run(["git", "init", "-b", "main", str(self.repo)], cwd=self.root)
        run(["git", "config", "user.email", "test@example.com"], cwd=self.repo)
        run(["git", "config", "user.name", "Test User"], cwd=self.repo)
        run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repo)
        self.write_file("ha-ops/config.yaml", 'version: "0.1.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n- Initial.\n")
        self.write_file("ha-ops/app.py", "print('hello')\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "Initial"], cwd=self.repo)
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

    def invoke_hook(self, stdin):
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env["PATH"]
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

    def test_allows_hook_only_push_without_release_bump(self):
        self.write_file(".githooks/pre-push", "#!/bin/sh\nexit 0\n")
        head = self.commit("Change hook")

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running HA Ops tests before push", result.stdout)

    def test_rejects_push_when_remote_main_moved(self):
        self.write_file("ha-ops/app.py", "print('remote change')\n")
        remote_head = self.commit("Remote update")
        run(["git", "push", "origin", "main"], cwd=self.repo)
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 0.1.0\n\n- Initial.\n")
        head = self.commit("Release from stale remote")
        run(["git", "tag", "0.2.0"], cwd=self.repo)

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"remote main moved from {self.base} to {remote_head}", result.stderr)

    def test_rejects_release_files_without_matching_tag(self):
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 0.1.0\n\n- Initial.\n")
        head = self.commit("Release without tag")

        result = self.invoke_hook(self.branch_push_stdin(head))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing local tag 0.2.0", result.stderr)

    def test_allows_release_files_with_matching_tag_pushed_together(self):
        self.write_file("ha-ops/config.yaml", 'version: "0.2.0"\n')
        self.write_file("ha-ops/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Change.\n\n## 0.1.0\n\n- Initial.\n")
        head = self.commit("Release with tag")
        run(["git", "tag", "0.2.0"], cwd=self.repo)
        stdin = self.branch_push_stdin(head)
        stdin += f"refs/tags/0.2.0 {head} refs/tags/0.2.0 {ZERO_SHA}\n"

        result = self.invoke_hook(stdin)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running HA Ops tests before push", result.stdout)


if __name__ == "__main__":
    unittest.main()
