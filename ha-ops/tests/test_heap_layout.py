"""Regressions for Home Assistant's root heap file layout."""

import sys
import tempfile
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import internal_id_migration  # noqa: E402
import jobs  # noqa: E402
import manifest  # noqa: E402
import state  # noqa: E402
import sync  # noqa: E402


class HeapLayoutTests(unittest.TestCase):
    def test_internal_id_migration_scans_only_root_heap_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("automations.yaml", "scripts.yaml", "scenes.yaml"):
                (root / name).write_text("[]\n")
            legacy = root / ".ha-ops" / "areas" / "room"
            legacy.mkdir(parents=True)
            (legacy / "automations.yaml").write_text("[]\n")

            self.assertEqual(
                [path.name for path in internal_id_migration.managed_files(root)],
                ["automations.yaml", "scripts.yaml", "scenes.yaml"],
            )

    def test_old_area_source_is_rejected_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "automations.yaml").write_text("[]\n")
            legacy = source / ".ha-ops" / "areas"
            legacy.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "Unsupported old Home Assistant source layout"):
                sync.validated_homeassistant_source(source, {"type": "homeassistant"})
            self.assertTrue(legacy.is_dir())
            self.assertEqual((source / "automations.yaml").read_text(), "[]\n")

    def test_old_manifest_field_is_rejected_even_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "homeassistant").mkdir()
            for value in (False, {"enabled": False}, True):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(RuntimeError, "Unsupported Home Assistant target field"):
                        manifest.resolve_targets(
                            root,
                            {"targets": [{"id": "homeassistant", "type": "homeassistant",
                                          "source": "homeassistant", "organizer": value}]},
                            [], {}, root / "live", root / "addons", lambda _: False,
                        )

    def test_only_root_heaps_qualify_as_pending_internal_id_migration(self):
        self.assertEqual(
            jobs.internal_ids_migration_paths({"apply_path": "homeassistant"}),
            {"homeassistant/automations.yaml", "homeassistant/scripts.yaml", "homeassistant/scenes.yaml"},
        )
        self.assertEqual(
            jobs.internal_ids_migration_paths({"apply_path": "."}),
            {"automations.yaml", "scripts.yaml", "scenes.yaml"},
        )
        self.assertFalse(jobs.internal_ids_migration_path("homeassistant/packages/automations.yaml", {"apply_path": "homeassistant"}))
        self.assertFalse(jobs.internal_ids_migration_path("homeassistant/.ha-ops/areas/room/automations.yaml", {"apply_path": "homeassistant"}))

    def test_retired_preference_never_returns_from_persisted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"homeassistant_organizer_enabled": true, "last_status": "success"}')
            self.assertNotIn("homeassistant_organizer_enabled", state.read_state(path))
            self.assertNotIn("homeassistant_organizer_enabled", state.read_state(path, hydrate_diffs=False))
            state.write_state(path, {"last_message": "updated"})
            self.assertNotIn("homeassistant_organizer_enabled", path.read_text())


if __name__ == "__main__":
    unittest.main()
