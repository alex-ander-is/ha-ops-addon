import sys
import unittest
import difflib
import json
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import registry_diff
import sync


class RegistryDiffTests(unittest.TestCase):
    def test_entity_add_remove_and_rename_are_explained(self):
        patch = """--- a/core.entity_registry
+++ b/core.entity_registry
@@ -1,3 +1,3 @@
-      {"id":"same","entity_id":"sensor.old","platform":"mqtt"},
-      {"id":"gone","entity_id":"sensor.gone","platform":"mqtt"},
+      {"id":"same","entity_id":"sensor.new","platform":"mqtt"},
+      {"id":"fresh","entity_id":"sensor.fresh","platform":"mqtt"},
"""
        result = registry_diff.summarize_registry_diff(patch, "homeassistant/.storage/core.entity_registry")
        self.assertEqual(result["counts"], {"changed": 1, "removed": 1, "added": 1})
        self.assertIn({"kind": "changed", "label": "sensor.new", "old_label": "sensor.old", "fields": ["entity_id"], "review": True}, result["rows"])

    def test_device_new_internal_id_is_paired_by_unique_identifier(self):
        patch = """-      {"id":"old","name":"Washer","identifiers":[["mqtt","washer"]]},
+      {"id":"new","name":"Washer","identifiers":[["mqtt","washer"]]},
"""
        result = registry_diff.summarize_registry_diff(patch, "homeassistant/.storage/core.device_registry")
        self.assertEqual(result["counts"], {"changed": 1})
        self.assertEqual(result["rows"][0]["fields"], ["id"])

    def test_ambiguous_device_match_stays_as_additions_and_removals(self):
        patch = """-      {"id":"old-a","name":"A","identifiers":[["mqtt","shared"]]},
-      {"id":"old-b","name":"B","identifiers":[["mqtt","shared"]]},
+      {"id":"new","name":"New","identifiers":[["mqtt","shared"]]},
"""
        result = registry_diff.summarize_registry_diff(patch, "core.device_registry")
        self.assertEqual(result["counts"], {"removed": 2, "added": 1})

    def test_unknown_changes_fall_back_to_raw_diff(self):
        patch = """-  "settings": {"some": "old"}
+  "settings": {"some": "new"}
"""
        self.assertIsNone(registry_diff.summarize_registry_diff(patch, "core.entity_registry"))

    def test_real_normalized_patch_explains_only_meaningful_fields(self):
        def text(name, timestamp):
            return json.dumps({"data": {"entities": [{
                "id": "same", "entity_id": "sensor.test", "platform": "mqtt",
                "name": name, "modified_at": timestamp,
            }]}})

        before = sync.normalized_storage_pretty_text_from_text("core.entity_registry", text("Old", "yesterday"))
        after = sync.normalized_storage_pretty_text_from_text("core.entity_registry", text("New", "today"))
        patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True)))
        result = registry_diff.summarize_registry_diff(patch, "core.entity_registry")
        self.assertEqual(result["counts"], {"changed": 1})
        self.assertEqual(result["rows"][0]["fields"], ["name"])
        self.assertNotIn("modified_at", patch)


if __name__ == "__main__":
    unittest.main()
