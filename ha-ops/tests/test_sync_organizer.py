import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def load_sync():
    sys.modules.pop("sync", None)
    spec = importlib.util.spec_from_file_location("sync", APP_DIR / "sync.py")
    sync = importlib.util.module_from_spec(spec)
    sys.modules["sync"] = sync
    spec.loader.exec_module(sync)
    return sync


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def write_yaml_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class SyncOrganizerTests(unittest.TestCase):
    def run_command(self, command, cwd=None):
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True)

    def context(self, sync, work_dir):
        return sync.SyncContext(
            add_detail=lambda details, message: details.append(message),
            addon_action=lambda *args: None,
            clean_dir_names=set(),
            clean_file_patterns=[],
            clean_paths=[],
            core_restart=lambda: None,
            core_reload_yaml=lambda: None,
            core_start=lambda: None,
            core_stop=lambda: None,
            do_core_check=lambda: None,
            export_excludes=[],
            ha_dirs=[],
            ha_root_excludes={"secrets.yaml"},
            ha_root_patterns=["*.yaml", "*.yml"],
            log=lambda message: None,
            protected_storage_files=set(),
            restart_or_start_addon=lambda *args: None,
            run_command=self.run_command,
            stop_addon_for_sync=lambda *args: False,
            storage_allowlist=["core.area_registry", "core.device_registry", "core.entity_registry"],
            work_dir=work_dir,
            zigbee2mqtt_paths=[],
        )

    def seed_registries(self, live):
        storage = live / ".storage"
        write_json(storage / "core.area_registry", {"data": {"areas": [{"id": "home", "name": "Home"}]}})
        write_json(storage / "core.device_registry", {"data": {"devices": []}})
        write_json(
            storage / "core.entity_registry",
            {
                "data": {
                    "entities": [
                        {
                            "entity_id": "automation.live",
                            "unique_id": "live_auto",
                            "area_id": "home",
                        }
                    ]
                }
            },
        )

    def seed_stale_organizer_view(self, root):
        write_yaml_text(root / ".ha-ops" / "areas" / "home" / "automations.yaml", "- id: stale_auto\n")
        write_yaml_text(root / ".ha-ops" / "areas" / "home" / "scripts.yaml", "stale_script:\n  sequence: []\n")
        write_json(
            root / ".ha-ops" / "areas" / "organizer-index.json",
            {
                "version": 1,
                "automations": {"count": 1, "ids": ["stale_auto"]},
                "scripts": {"count": 1, "ids": ["stale_script"]},
                "scenes": {"count": 0, "ids": []},
            },
        )

    def assert_heap_files_equal(self, left, right):
        for filename in ("automations.yaml", "scripts.yaml", "scenes.yaml"):
            with self.subTest(filename=filename):
                self.assertEqual((left / filename).read_text(), (right / filename).read_text())

    @unittest.skip("enabled .ha-ops/areas projection is pending the organizer rewrite")
    def test_save_unknown_base_conflicts_reports_heap_file_removed_by_organizer(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(source / "automations.yaml", "- id: git_auto\n  alias: Git only\n")
            write_yaml_text(live / "automations.yaml", "- id: live_auto\n  alias: live\n")
            write_yaml_text(live / "scripts.yaml", "{}\n")
            write_yaml_text(live / "scenes.yaml", "[]\n")
            self.seed_registries(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": True},
            }
            details = []

            conflicts = sync.save_unknown_base_conflicts(
                [target],
                repo,
                {},
                details,
                self.context(sync, work),
            )

            self.assertEqual(conflicts, ["homeassistant/automations.yaml"])

    @unittest.skip("enabled .ha-ops/areas projection is pending the organizer rewrite")
    def test_save_unknown_base_conflicts_ignores_identical_heap_file_removed_by_organizer(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            heap = "- id: live_auto\n  alias: live\n"
            write_yaml_text(source / "automations.yaml", heap)
            write_yaml_text(live / "automations.yaml", heap)
            write_yaml_text(live / "scripts.yaml", "{}\n")
            write_yaml_text(live / "scenes.yaml", "[]\n")
            self.seed_registries(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": True},
            }

            conflicts = sync.save_unknown_base_conflicts(
                [target],
                repo,
                {},
                [],
                self.context(sync, work),
            )

            self.assertEqual(conflicts, [])

    def test_organizer_is_disabled_without_explicit_opt_in(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(live / "automations.yaml", "- id: live_auto\n  alias: live\n")
            write_yaml_text(live / "scripts.yaml", "{}\n")
            write_yaml_text(live / "scenes.yaml", "[]\n")
            self.seed_registries(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
            }

            sync.export_targets([target], [], self.context(sync, work))

            self.assertTrue((source / "automations.yaml").exists())
            self.assertTrue((source / "scripts.yaml").exists())
            self.assertTrue((source / "scenes.yaml").exists())
            self.assertFalse((source / ".ha-ops" / "areas").exists())

    def test_disabled_organizer_save_then_apply_preview_has_no_heap_diff(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            live = root / "live"
            apply_live = root / "apply-live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            apply_live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(live / "configuration.yaml", "homeassistant:\n")
            write_yaml_text(live / "automations.yaml", "- id: live_auto\n  alias: Live Auto\n")
            write_yaml_text(live / "scripts.yaml", "live_script:\n  sequence: []\n")
            write_yaml_text(live / "scenes.yaml", "- id: live_scene\n  name: Live Scene\n  entities: {}\n")
            self.seed_registries(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": False,
            }
            details = []
            ctx = self.context(sync, work)

            sync.export_targets([target], details, ctx)

            self.assertTrue((source / "automations.yaml").exists())
            self.assertTrue((source / "scripts.yaml").exists())
            self.assertTrue((source / "scenes.yaml").exists())
            self.assertFalse((source / ".ha-ops" / "areas").exists())
            self.assertIn("Preserved 3 Home Assistant automation/script/scene item(s) as heap YAML for Git.", details)

            preview = sync.build_apply_preview_from_sources([target], ctx)
            self.assertIn("no file changes", preview["diff"].lower())
            self.assertEqual(preview["paths"], [])

            apply_target = dict(target)
            apply_target["live_path"] = str(apply_live)
            sync.apply_targets([apply_target], [], ctx)

            self.assert_heap_files_equal(live, apply_live)
            self.assertFalse((apply_live / ".ha-ops" / "areas").exists())

    def test_disabled_organizer_apply_then_save_has_no_heap_diff(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            live = root / "live"
            saved = root / "saved" / "homeassistant"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            saved.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(source / "configuration.yaml", "homeassistant:\n")
            write_yaml_text(source / "automations.yaml", "- id: git_auto\n  alias: Git Auto\n")
            write_yaml_text(source / "scripts.yaml", "git_script:\n  sequence: []\n")
            write_yaml_text(source / "scenes.yaml", "- id: git_scene\n  name: Git Scene\n  entities: {}\n")
            self.seed_stale_organizer_view(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": False},
            }
            ctx = self.context(sync, work)

            sync.apply_targets([target], [], ctx)
            self.assert_heap_files_equal(source, live)
            self.assertFalse((live / ".ha-ops" / "areas").exists())

            repeat_preview = sync.build_apply_preview_from_sources([target], ctx)
            self.assertIn("no file changes", repeat_preview["diff"].lower())
            self.assertEqual(repeat_preview["paths"], [])

            save_target = dict(target)
            save_target["source_path"] = str(saved)
            sync.export_targets([save_target], [], ctx)

            self.assert_heap_files_equal(source, saved)
            self.assertFalse((saved / ".ha-ops" / "areas").exists())

            preview = sync.build_apply_preview_from_sources([target], ctx)
            self.assertIn("no file changes", preview["diff"].lower())
            self.assertEqual(preview["paths"], [])

    def test_enabled_organizer_rejects_heap_only_apply_before_heap_mode_copy(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(source / "configuration.yaml", "git_only:\n")
            write_yaml_text(source / "automations.yaml", "- id: git_auto\n  alias: Git Auto\n")
            write_yaml_text(source / "scripts.yaml", "git_script:\n  sequence: []\n")
            write_yaml_text(source / "scenes.yaml", "- id: git_scene\n  name: Git Scene\n  entities: {}\n")
            write_yaml_text(live / "configuration.yaml", "live_only:\n")
            self.seed_stale_organizer_view(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": True},
            }

            with self.assertRaisesRegex(sync.organizer.OrganizerRemovedError, "projection rewrite is pending"):
                sync.apply_targets([target], [], self.context(sync, work))

            self.assertEqual((live / "configuration.yaml").read_text(), "live_only:\n")

    @unittest.skip("enabled .ha-ops/areas projection is pending the organizer rewrite")
    def test_organizer_enabled_true_exports_area_view(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(live / "automations.yaml", "- id: live_auto\n  alias: live\n")
            write_yaml_text(live / "scripts.yaml", "{}\n")
            write_yaml_text(live / "scenes.yaml", "[]\n")
            self.seed_registries(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": True,
            }

            sync.export_targets([target], [], self.context(sync, work))

            self.assertFalse((source / "automations.yaml").exists())
            self.assertTrue((source / ".ha-ops" / "areas" / "home" / "automations.yaml").exists())

    def test_apply_rejects_split_git_view_when_organizer_is_disabled(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            live = root / "live"
            work = root / "work"
            live.mkdir(parents=True)
            work.mkdir()
            write_yaml_text(source / ".ha-ops" / "areas" / "home" / "automations.yaml", "- id: live_auto\n")
            write_json(
                source / ".ha-ops" / "areas" / "organizer-index.json",
                {
                    "version": 1,
                    "automations": {"count": 1, "ids": ["live_auto"]},
                    "scripts": {"count": 0, "ids": []},
                    "scenes": {"count": 0, "ids": []},
                },
            )

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
            }

            with self.assertRaises(RuntimeError) as raised:
                sync.materialize_homeassistant_source(source, target, self.context(sync, work))
            message = str(raised.exception)
            self.assertIn("organizer view exists in Git", message)
            self.assertIn("projection rewrite is pending", message)
            self.assertIn("Use Save HA to Git with the organizer disabled", message)
            self.assertNotIn("Enable the Home Assistant Git layout toggle", message)

    @unittest.skip("enabled .ha-ops/areas projection is pending the organizer rewrite")
    def test_apply_materialize_organizer_source_excludes_unmanaged_area_files(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            live = root / "live"
            work = root / "work"
            live.mkdir(parents=True)
            work.mkdir()
            write_yaml_text(source / ".ha-ops" / "areas" / "home" / "automations.yaml", "- id: live_auto\n")
            write_yaml_text(source / ".ha-ops" / "areas" / "dining_room" / "lighting-contract.md", "# Contract\n")
            write_json(
                source / ".ha-ops" / "areas" / "organizer-index.json",
                {
                    "version": 1,
                    "automations": {"count": 1, "ids": ["live_auto"]},
                    "scripts": {"count": 0, "ids": []},
                    "scenes": {"count": 0, "ids": []},
                },
            )

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": True},
            }

            materialized = sync.materialize_homeassistant_source(source, target, self.context(sync, work))

            self.assertEqual((materialized / "automations.yaml").read_text(), "- id: live_auto\n")
            self.assertFalse((materialized / ".ha-ops" / "areas").exists())
            self.assertFalse((materialized / ".ha-ops" / "areas" / "dining_room" / "lighting-contract.md").exists())

    def test_save_with_organizer_disabled_converts_git_back_to_heap_view(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "repo" / "homeassistant"
            export_root = root / "export"
            work = root / "work"
            source.mkdir(parents=True)
            export_path = export_root / "homeassistant"
            work.mkdir()
            write_yaml_text(source / ".ha-ops" / "areas" / "home" / "automations.yaml", "- id: old_split\n")
            write_yaml_text(export_path / "automations.yaml", "- id: live_auto\n")

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
            }

            sync.apply_save_export([target], export_root, [], self.context(sync, work))

            self.assertFalse((source / ".ha-ops" / "areas").exists())
            self.assertEqual((source / "automations.yaml").read_text(), "- id: live_auto\n")

    def test_save_unknown_base_conflicts_reports_git_only_managed_homeassistant_file_deletion(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "homeassistant"
            live = root / "live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(source / "configuration.yaml", "homeassistant:\n")

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
            }

            conflicts = sync.save_unknown_base_conflicts(
                [target],
                repo,
                {},
                [],
                self.context(sync, work),
            )

            self.assertEqual(conflicts, ["homeassistant/configuration.yaml"])

    def test_save_unknown_base_conflicts_reports_git_only_addon_file_deletion_when_mirrored(self):
        sync = load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            source = repo / "addons" / "demo"
            live = root / "addon-live"
            work = root / "work"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            work.mkdir()

            write_yaml_text(source / "configuration.yaml", "old: true\n")

            target = {
                "id": "addon-demo",
                "type": "addon",
                "source": "addons/demo",
                "source_path": str(source),
                "live_path": str(live),
                "save_delete": True,
            }

            conflicts = sync.save_unknown_base_conflicts(
                [target],
                repo,
                {},
                [],
                self.context(sync, work),
            )

            self.assertEqual(conflicts, ["addons/demo/configuration.yaml"])
