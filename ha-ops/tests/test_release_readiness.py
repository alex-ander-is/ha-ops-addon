import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
