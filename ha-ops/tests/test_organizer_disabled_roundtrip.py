import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
ORGANIZER_PATH = APP_DIR / "organizer.py"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
README_PATH = ROOT / "README.md"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def load_organizer():
    sys.modules.pop("organizer", None)
    spec = importlib.util.spec_from_file_location("organizer", ORGANIZER_PATH)
    organizer = importlib.util.module_from_spec(spec)
    sys.modules["organizer"] = organizer
    spec.loader.exec_module(organizer)
    return organizer


ORGANIZER = load_organizer()


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def seed_synthetic_heap(root):
    write_text(
        root / "automations.yaml",
        "- id: morning\n"
        "  alias: Morning\n"
        "  trigger:\n"
        "  - platform: time\n"
        "    at: 06:30:00\n"
        "  action:\n"
        "  - service: light.turn_on\n",
    )
    write_text(
        root / "scripts.yaml",
        "announce:\n"
        "  alias: Announce\n"
        "  sequence:\n"
        "  - service: notify.notify\n",
    )
    write_text(root / "scenes.yaml", "[]\n")


def configured_external_fixture():
    configured = os.environ.get("HA_OPS_HA_CONFIG_FIXTURE")
    if not configured:
        return None
    path = Path(configured)
    if (path / "homeassistant").is_dir():
        path = path / "homeassistant"
    if not path.exists():
        return None
    if not any((path / filename).exists() for filename in ORGANIZER.HEAP_FILES.values()):
        return None
    return path


def copy_heap_fixture(src, dest):
    for filename in ORGANIZER.HEAP_FILES.values():
        src_path = src / filename
        if src_path.exists():
            dest_path = dest / filename
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)


def assert_heap_files_equal(testcase, left, right):
    for filename in ORGANIZER.HEAP_FILES.values():
        left_path = left / filename
        right_path = right / filename
        testcase.assertEqual(left_path.exists(), right_path.exists(), filename)
        if left_path.exists():
            testcase.assertEqual(left_path.read_bytes(), right_path.read_bytes(), filename)


def changelog_section(version):
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    marker = f"## {version}"
    start = text.index(marker)
    next_start = text.find("\n## ", start + len(marker))
    if next_start == -1:
        return text[start:]
    return text[start:next_start]


class OrganizerDisabledRoundTripTests(unittest.TestCase):
    def disabled_options(self):
        return {"enabled": False}

    def assert_disabled_heap_to_git_to_heap_round_trip(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            git = root / "git"
            composed = root / "composed"
            copy_heap_fixture(source, live)

            split_summary = ORGANIZER.split_live_heaps_to_git(live, git, options=self.disabled_options())
            compose_summary = ORGANIZER.compose_git_view_to_live(git, composed, options=self.disabled_options())

            self.assertEqual(split_summary, compose_summary)
            assert_heap_files_equal(self, live, git)
            assert_heap_files_equal(self, live, composed)
            self.assertFalse((git / ".ha-ops" / "areas").exists())
            self.assertFalse((composed / ".ha-ops" / "areas").exists())

    def assert_disabled_git_to_heap_to_git_round_trip(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git"
            live = root / "live"
            saved = root / "saved"
            copy_heap_fixture(source, git)

            compose_summary = ORGANIZER.compose_git_view_to_live(git, live, options=self.disabled_options())
            split_summary = ORGANIZER.split_live_heaps_to_git(live, saved, options=self.disabled_options())

            self.assertEqual(compose_summary, split_summary)
            assert_heap_files_equal(self, git, live)
            assert_heap_files_equal(self, git, saved)
            self.assertFalse((live / ".ha-ops" / "areas").exists())
            self.assertFalse((saved / ".ha-ops" / "areas").exists())

    def test_synthetic_heap_round_trips_with_disabled_organizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "homeassistant"
            seed_synthetic_heap(source)

            self.assert_disabled_heap_to_git_to_heap_round_trip(source)
            self.assert_disabled_git_to_heap_to_git_round_trip(source)

    def test_external_fixture_is_absent_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "homeassistant"
            seed_synthetic_heap(source)

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(configured_external_fixture())

    def test_configured_external_fixture_accepts_explicit_synthetic_homeassistant_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "homeassistant"
            seed_synthetic_heap(source)

            with mock.patch.dict(os.environ, {"HA_OPS_HA_CONFIG_FIXTURE": str(source)}):
                self.assertEqual(configured_external_fixture(), source)

    def test_configured_external_fixture_accepts_explicit_synthetic_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "homeassistant"
            seed_synthetic_heap(source)

            with mock.patch.dict(os.environ, {"HA_OPS_HA_CONFIG_FIXTURE": str(root)}):
                self.assertEqual(configured_external_fixture(), source)

    def test_external_heap_fixture_round_trips_with_disabled_organizer(self):
        source = configured_external_fixture()
        if source is None:
            self.skipTest("HA_OPS_HA_CONFIG_FIXTURE is not configured")

        self.assert_disabled_heap_to_git_to_heap_round_trip(source)
        self.assert_disabled_git_to_heap_to_git_round_trip(source)

    def test_enabled_area_projection_still_waits_for_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "homeassistant"
            git = Path(tmp) / "git"
            seed_synthetic_heap(source)

            with self.assertRaises(ORGANIZER.OrganizerRemovedError):
                ORGANIZER.split_live_heaps_to_git(source, git, options={})

    def test_release_docs_disclose_enabled_projection_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "homeassistant"
            git = Path(tmp) / "git"
            seed_synthetic_heap(source)

            with self.assertRaises(ORGANIZER.OrganizerRemovedError):
                ORGANIZER.split_live_heaps_to_git(source, git, options={})

        changelog = changelog_section("0.8.39").lower()
        readme = README_PATH.read_text(encoding="utf-8").lower()

        self.assertIn(".ha-ops/areas", changelog)
        self.assertIn("blocked pending", changelog)
        self.assertIn("production-safe", changelog)
        self.assertIn(".ha-ops/areas", readme)
        self.assertIn("blocked pending", readme)
        self.assertIn("keep organizer disabled", readme)
        self.assertNotIn("enabled git targets expose an area-first view", readme)
