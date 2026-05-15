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
