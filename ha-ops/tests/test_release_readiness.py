import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
        if "__pycache__" in path.parts:
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

    def test_lovelace_apply_changelog_note_is_under_current_version(self):
        config = CONFIG_PATH.read_text()
        match = re.search(r'^version:\s*"([^"]+)"\s*$', config, re.MULTILINE)
        self.assertIsNotNone(match)
        current_version = match.group(1)
        changelog = CHANGELOG_PATH.read_text()
        current_section = changelog_section(changelog, current_version)
        self.assertIsNotNone(current_section)
        lovelace_note = (
            "Reload Lovelace resources instead of stopping and starting Core when "
            "Git-to-HA Apply only changes `lovelace_resources`"
        )
        self.assertIn(lovelace_note, current_section)

        old_section = changelog_section(changelog, "0.8.48")
        self.assertIsNotNone(old_section)
        self.assertNotIn(lovelace_note, old_section)


if __name__ == "__main__":
    unittest.main()
