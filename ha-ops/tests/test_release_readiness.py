import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
CONFIG_PATH = ROOT / "config.yaml"
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}


def repo_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if "__pycache__" in path.parts or ".codex-audit" in path.parts:
            continue
        yield path


def changelog_section(text, version):
    marker = f"## {version}"
    start = text.find(marker)
    if start < 0:
        return None
    next_start = text.find("\n## ", start + len(marker))
    if next_start < 0:
        return text[start:]
    return text[start:next_start]


class ReleaseReadinessTests(unittest.TestCase):
    def test_displayed_terminology_uses_apps(self):
        legacy_term = re.compile(r"\badd-" + r"ons?\b", re.IGNORECASE)
        paths = [REPO_ROOT / "AGENTS.md", REPO_ROOT / "README.md", REPO_ROOT / "repository.yaml", *repo_text_files()]
        matches = []

        for path in paths:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if legacy_term.search(text):
                matches.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual(matches, [])
        self.assertIn('name: HA Ops Apps', (REPO_ROOT / "repository.yaml").read_text(encoding="utf-8"))

    def test_repo_text_files_do_not_contain_local_fixture_markers(self):
        private_tmp = "/" + "private" + "/" + "tmp"
        live_artifacts = "ha-ops-live-" + "artifacts"
        run_id = re.compile(r"20\d{6}-\d{6}(?:-[A-Za-z0-9_.-]+)?")
        failures = []

        for path in repo_text_files():
            relative = path.relative_to(ROOT)
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in (private_tmp, live_artifacts):
                if marker in text:
                    failures.append(f"{relative}: contains local fixture marker")
            if run_id.search(text):
                failures.append(f"{relative}: contains audit-loop run identifier")

        self.assertEqual([], failures)

    def test_duplicate_running_log_changelog_note_stays_in_its_release(self):
        changelog = CHANGELOG_PATH.read_text()
        documented_section = changelog_section(changelog, "0.8.54")
        self.assertIsNotNone(documented_section)
        duplicate_running_log_note = (
            "Avoid duplicate running log context lines"
        )
        self.assertIn(duplicate_running_log_note, documented_section)

        old_section = changelog_section(changelog, "0.8.53")
        self.assertIsNotNone(old_section)
        self.assertNotIn(duplicate_running_log_note, old_section)


if __name__ == "__main__":
    unittest.main()
