import importlib.util
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORGANIZER_PATH = ROOT / "app" / "organizer.py"


def load_optional_organizer():
    if not ORGANIZER_PATH.exists():
        return None
    sys.modules.pop("organizer", None)
    spec = importlib.util.spec_from_file_location("organizer", ORGANIZER_PATH)
    organizer = importlib.util.module_from_spec(spec)
    sys.modules["organizer"] = organizer
    spec.loader.exec_module(organizer)
    return organizer


try:
    import yaml
except ModuleNotFoundError:
    yaml = None


ORGANIZER = load_optional_organizer()
if ORGANIZER is not None and yaml is None:
    raise RuntimeError("PyYAML is required when app/organizer.py is present")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_yaml(path):
    with path.open() as handle:
        return yaml.safe_load(handle)


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


def area_registry():
    return {
        "version": 1,
        "data": {
            "areas": [
                {"id": "home", "name": "Home"},
                {"id": "office", "name": "Office"},
                {"id": "kitchen", "name": "Kitchen"},
                {"id": "bathroom", "name": "Bathroom"},
                {"id": "kids_room", "name": "Kids Room"},
                {"id": "hallway", "name": "Hallway"},
                {"id": "garage", "name": "Garage"},
            ]
        },
    }


def device_registry():
    return {
        "version": 1,
        "data": {
            "devices": [
                {"id": "dev_office_printer", "area_id": "office"},
                {"id": "dev_kitchen_motion", "area_id": "kitchen"},
                {"id": "dev_bath_fan", "area_id": "bathroom"},
                {"id": "dev_kids_light", "area_id": "kids_room"},
                {"id": "dev_hallway_button", "area_id": "hallway"},
                {"id": "dev_garage_door", "area_id": "garage"},
            ]
        },
    }


def entity_registry():
    return {
        "version": 1,
        "data": {
            "entities": [
                {
                    "entity_id": "automation.ui_home_cross_area",
                    "unique_id": "auto_ui_home",
                    "area_id": "home",
                },
                {
                    "entity_id": "automation.renamed_but_stable",
                    "unique_id": "auto_renamed",
                    "area_id": "office",
                },
                {
                    "entity_id": "automation.time_only",
                    "unique_id": "auto_time",
                    "area_id": "home",
                },
                {
                    "entity_id": "script.home_music",
                    "unique_id": "home_music",
                    "area_id": "home",
                },
                {
                    "entity_id": "script.kitchen_shades",
                    "unique_id": "kitchen_shades",
                    "area_id": "kitchen",
                },
                {
                    "entity_id": "script.alias_differs_from_key",
                    "unique_id": "alias_differs_from_key",
                    "area_id": "office",
                },
                {
                    "entity_id": "scene.movie_mode",
                    "unique_id": "scene_movie",
                    "area_id": "home",
                },
                {
                    "entity_id": "binary_sensor.kitchen_motion",
                    "device_id": "dev_kitchen_motion",
                },
                {
                    "entity_id": "fan.bathroom_fan",
                    "device_id": "dev_bath_fan",
                },
                {
                    "entity_id": "light.kids_ceiling",
                    "device_id": "dev_kids_light",
                },
                {
                    "entity_id": "button.hallway_switch",
                    "device_id": "dev_hallway_button",
                },
                {
                    "entity_id": "cover.garage_door",
                    "device_id": "dev_garage_door",
                },
                {
                    "entity_id": "light.turn_on",
                    "device_id": "dev_office_printer",
                },
            ]
        },
    }


def automation_super_set():
    return [
        {
            "id": "auto_ui_home",
            "alias": "ui_home_cross_area",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.kitchen_motion"}],
            "action": [{"service": "light.turn_on", "target": {"entity_id": "light.kids_ceiling"}}],
        },
        {
            "id": "auto_renamed",
            "alias": "office_new_alias_after_rename",
            "trigger": [{"platform": "state", "entity_id": "cover.garage_door"}],
            "action": [{"service": "script.alias_differs_from_key"}],
        },
        {
            "id": "auto_time",
            "alias": "time_only",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [{"service": "script.home_music"}],
        },
        {
            "id": "auto_override",
            "alias": "anything_can_be_overridden",
            "trigger": [{"platform": "state", "entity_id": "cover.garage_door"}],
            "action": [{"service": "notify.notify"}],
        },
        {
            "id": "auto_prefix",
            "alias": "office_prefix_routes_without_registry",
            "trigger": [{"platform": "event", "event_type": "office_event"}],
            "action": [{"service": "notify.notify"}],
        },
        {
            "id": "auto_direct_area",
            "alias": "direct_area_reference",
            "condition": [{"condition": "state", "area_id": "bathroom"}],
            "action": [{"service": "fan.turn_on", "target": {"entity_id": "fan.bathroom_fan"}}],
        },
        {
            "id": "auto_device",
            "alias": "device_id_reference",
            "trigger": [{"platform": "device", "device_id": "dev_hallway_button"}],
            "action": [{"service": "light.toggle"}],
        },
        {
            "id": "auto_entity",
            "alias": "entity_reference",
            "trigger": [{"platform": "state", "entity_id": "light.kids_ceiling"}],
            "action": [{"service": "light.turn_off", "target": {"entity_id": "light.kids_ceiling"}}],
        },
        {
            "id": "auto_called_script",
            "alias": "called_script_reference",
            "trigger": [{"platform": "time", "at": "21:00:00"}],
            "action": [{"service": "script.kitchen_shades"}],
        },
        {
            "id": "auto_mixed",
            "alias": "balanced_cross_area_reference",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.kitchen_motion"}],
            "condition": [{"condition": "state", "entity_id": "light.kids_ceiling"}],
            "action": [{"service": "notify.notify"}],
        },
        {
            "id": "auto_unknown",
            "alias": "cannot_route",
            "trigger": [{"platform": "event", "event_type": "external"}],
            "action": [{"service": "notify.notify"}],
        },
    ]


def script_super_set():
    return {
        "home_music": {
            "alias": "Home Music",
            "sequence": [{"service": "media_player.play_media", "target": {"area_id": "kitchen"}}],
        },
        "kitchen_shades": {
            "alias": "Kitchen Shades",
            "sequence": [{"service": "cover.close_cover", "target": {"area_id": "kitchen"}}],
        },
        "alias_differs_from_key": {
            "alias": "Office Scene Wrapper",
            "sequence": [{"service": "light.turn_on", "target": {"entity_id": "light.kids_ceiling"}}],
        },
        "office_prefix_script": {
            "alias": "office_prefix_script",
            "sequence": [{"service": "notify.notify"}],
        },
        "unknown_script": {
            "alias": "No Route",
            "sequence": [{"delay": "00:00:01"}],
        },
    }


def scene_super_set():
    return [
        {
            "id": "scene_movie",
            "name": "Movie Mode",
            "entities": {"light.kids_ceiling": {"state": "off"}},
        },
        {
            "id": "scene_garage",
            "name": "Garage Ready",
            "entities": {"cover.garage_door": {"state": "closed"}},
        },
    ]


def seed_live_homeassistant(root):
    live = root / "live"
    storage = live / ".storage"
    storage.mkdir(parents=True)
    write_yaml(live / "automations.yaml", automation_super_set())
    write_yaml(live / "scripts.yaml", script_super_set())
    write_yaml(live / "scenes.yaml", scene_super_set())
    write_json(storage / "core.area_registry", area_registry())
    write_json(storage / "core.device_registry", device_registry())
    write_json(storage / "core.entity_registry", entity_registry())
    return live


def ids(items):
    return [item.get("id") for item in items]


def script_keys(items):
    return list(items.keys())


def scene_identity(item):
    return item.get("id") or item.get("name")


def area_file(root, area, filename):
    return root / ".ha-ops" / "areas" / area / filename


def collect_automation_locations(git):
    locations = []
    for path in sorted((git / ".ha-ops" / "areas").glob("*/automations.yaml")):
        for item in read_yaml(path) or []:
            locations.append((item.get("id"), path.parent.name))
    return locations


def collect_script_locations(git):
    locations = []
    for path in sorted((git / ".ha-ops" / "areas").glob("*/scripts.yaml")):
        for key in (read_yaml(path) or {}).keys():
            locations.append((key, path.parent.name))
    return locations


def collect_scene_locations(git):
    locations = []
    for path in sorted((git / ".ha-ops" / "areas").glob("*/scenes.yaml")):
        for item in read_yaml(path) or []:
            locations.append((scene_identity(item), path.parent.name))
    return locations


@unittest.skipUnless(ORGANIZER is not None, "pending app/organizer.py implementation")
class OrganizerContractTests(unittest.TestCase):
    def options(self):
        return {
            "organized_root": ".ha-ops/areas",
            "overrides": {
                "automations": {
                    "auto_override": "garage",
                }
            },
            "prefixes": {
                "office": ["office_"],
            },
        }

    def test_split_creates_area_first_git_view_from_live_heap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"

            summary = ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            areas = git / ".ha-ops" / "areas"
            self.assertTrue((areas / "home" / "automations.yaml").exists())
            self.assertTrue((areas / "office" / "scripts.yaml").exists())
            self.assertTrue((areas / "garage" / "automations.yaml").exists())
            self.assertEqual(summary["automations"]["input_count"], len(automation_super_set()))
            self.assertEqual(summary["automations"]["output_count"], len(automation_super_set()))
            self.assertEqual(summary["scripts"]["input_count"], len(script_super_set()))
            self.assertEqual(summary["scripts"]["output_count"], len(script_super_set()))
            self.assertEqual(summary["scenes"]["input_count"], len(scene_super_set()))
            self.assertEqual(summary["scenes"]["output_count"], len(scene_super_set()))

            home_automations = read_yaml(areas / "home" / "automations.yaml")
            office_automations = read_yaml(areas / "office" / "automations.yaml")
            garage_automations = read_yaml(areas / "garage" / "automations.yaml")
            self.assertIn("auto_ui_home", ids(home_automations))
            self.assertIn("auto_time", ids(home_automations))
            self.assertIn("auto_renamed", ids(office_automations))
            self.assertIn("auto_prefix", ids(office_automations))
            self.assertIn("auto_override", ids(garage_automations))

            automation_locations = collect_automation_locations(git)
            script_locations = collect_script_locations(git)
            scene_locations = collect_scene_locations(git)
            self.assertEqual(Counter(item for item, area in automation_locations), Counter(ids(automation_super_set())))
            self.assertEqual(Counter(item for item, area in script_locations), Counter(script_keys(script_super_set())))
            self.assertEqual(Counter(item for item, area in scene_locations), Counter(scene_identity(item) for item in scene_super_set()))

    def test_rejects_unsafe_organized_root_values(self):
        unsafe_values = [".", "../areas", "/tmp/areas"]
        for value in unsafe_values:
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    live = seed_live_homeassistant(root)
                    git = root / "git" / "homeassistant"

                    with self.assertRaisesRegex(RuntimeError, "organized_root"):
                        ORGANIZER.split_live_heaps_to_git(live, git, options={"organized_root": value})

    def test_split_uses_deterministic_fallbacks_when_ui_area_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            areas = git / ".ha-ops" / "areas"
            self.assertIn("auto_direct_area", ids(read_yaml(areas / "bathroom" / "automations.yaml")))
            self.assertIn("auto_device", ids(read_yaml(areas / "hallway" / "automations.yaml")))
            self.assertIn("auto_entity", ids(read_yaml(areas / "kids_room" / "automations.yaml")))
            self.assertIn("auto_called_script", ids(read_yaml(areas / "kitchen" / "automations.yaml")))
            self.assertIn("auto_mixed", ids(read_yaml(areas / ".mixed" / "automations.yaml")))
            self.assertIn("auto_unknown", ids(read_yaml(areas / ".unknown" / "automations.yaml")))
            self.assertIn("office_prefix_script", script_keys(read_yaml(areas / "office" / "scripts.yaml")))
            self.assertIn("unknown_script", script_keys(read_yaml(areas / ".unknown" / "scripts.yaml")))

    def test_split_routes_scenes_by_ui_area_and_entity_map_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            self.assertIn("scene_movie", ids(read_yaml(area_file(git, "home", "scenes.yaml"))))
            self.assertIn("scene_garage", ids(read_yaml(area_file(git, "garage", "scenes.yaml"))))

    def test_service_names_are_not_treated_as_entity_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            automations = [
                {
                    "id": "auto_service_only",
                    "alias": "service_only",
                    "trigger": [{"platform": "event", "event_type": "manual"}],
                    "action": [{"service": "light.turn_on"}],
                }
            ]
            write_yaml(live / "automations.yaml", automations)
            write_yaml(live / "scripts.yaml", {})
            write_yaml(live / "scenes.yaml", [])

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            self.assertIn("auto_service_only", ids(read_yaml(area_file(git, ".unknown", "automations.yaml"))))
            self.assertFalse(area_file(git, "office", "automations.yaml").exists())

    def test_real_unknown_area_does_not_conflict_with_unknown_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            write_json(
                live / ".storage" / "core.area_registry",
                {
                    "version": 1,
                    "data": {
                        "areas": [
                            {"id": "unknown_area", "name": "Unknown"},
                        ]
                    },
                },
            )
            write_json(
                live / ".storage" / "core.entity_registry",
                {
                    "version": 1,
                    "data": {
                        "entities": [
                            {
                                "entity_id": "automation.real_unknown_area",
                                "unique_id": "real_unknown_area",
                                "area_id": "unknown_area",
                            },
                        ]
                    },
                },
            )
            write_yaml(
                live / "automations.yaml",
                [
                    {"id": "real_unknown_area", "alias": "Real Unknown Area", "trigger": [], "action": []},
                    {"id": "unroutable", "alias": "Unroutable", "trigger": [], "action": []},
                ],
            )
            write_yaml(live / "scripts.yaml", {})
            write_yaml(live / "scenes.yaml", [])

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            self.assertIn("real_unknown_area", ids(read_yaml(area_file(git, "unknown", "automations.yaml"))))
            self.assertIn("unroutable", ids(read_yaml(area_file(git, ".unknown", "automations.yaml"))))

    def test_compose_rebuilds_live_heaps_without_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            composed = root / "composed"

            split_summary = ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
            compose_summary = ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())

            automations = read_yaml(composed / "automations.yaml")
            scripts = read_yaml(composed / "scripts.yaml")
            scenes = read_yaml(composed / "scenes.yaml")
            self.assertEqual(len(automations), split_summary["automations"]["input_count"])
            self.assertEqual(len(scripts), split_summary["scripts"]["input_count"])
            self.assertEqual(len(scenes), split_summary["scenes"]["input_count"])
            self.assertEqual(compose_summary["automations"]["output_count"], len(automation_super_set()))
            self.assertEqual(compose_summary["scripts"]["output_count"], len(script_super_set()))
            self.assertEqual(compose_summary["scenes"]["output_count"], len(scene_super_set()))
            self.assertEqual(set(ids(automations)), set(ids(automation_super_set())))
            self.assertEqual(set(script_keys(scripts)), set(script_keys(script_super_set())))

    def test_round_trip_preserves_super_set_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            composed = root / "composed"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
            ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())

            self.assertEqual(
                sorted(read_yaml(composed / "automations.yaml"), key=lambda item: item.get("id", "")),
                sorted(automation_super_set(), key=lambda item: item.get("id", "")),
            )
            self.assertEqual(read_yaml(composed / "scripts.yaml"), script_super_set())
            self.assertEqual(
                sorted(read_yaml(composed / "scenes.yaml"), key=lambda item: item.get("id", item.get("name", ""))),
                sorted(scene_super_set(), key=lambda item: item.get("id", item.get("name", ""))),
            )

    def test_integrity_rejects_duplicate_automation_ids_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            areas = git / ".ha-ops" / "areas"
            write_yaml(areas / "home" / "automations.yaml", [{"id": "dup", "alias": "One"}])
            write_yaml(areas / "office" / "automations.yaml", [{"id": "dup", "alias": "Two"}])
            write_yaml(areas / "home" / "scripts.yaml", {})
            write_yaml(areas / "home" / "scenes.yaml", [])

            with self.assertRaisesRegex(RuntimeError, "duplicate.*automation.*dup"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    def test_integrity_rejects_duplicate_script_keys_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            areas = git / ".ha-ops" / "areas"
            write_yaml(areas / "home" / "scripts.yaml", {"dup": {"alias": "One"}})
            write_yaml(areas / "office" / "scripts.yaml", {"dup": {"alias": "Two"}})

            with self.assertRaisesRegex(RuntimeError, "duplicate.*script.*dup"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "scripts.yaml").exists())

    def test_integrity_rejects_duplicate_scene_identities_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            areas = git / ".ha-ops" / "areas"
            write_yaml(areas / "home" / "scenes.yaml", [{"id": "dup", "name": "One"}])
            write_yaml(areas / "office" / "scenes.yaml", [{"id": "dup", "name": "Two"}])

            with self.assertRaisesRegex(RuntimeError, "duplicate.*scene.*dup"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "scenes.yaml").exists())

    def test_integrity_rejects_malformed_yaml_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            path = area_file(git, "home", "automations.yaml")
            path.parent.mkdir(parents=True)
            path.write_text("- id: broken\n  alias: [unterminated\n")

            with self.assertRaises(Exception):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    def test_integrity_rejects_wrong_top_level_types_before_writing_live(self):
        cases = [
            ("automations.yaml", {"not": "a list"}, "must contain a list"),
            ("scripts.yaml", [{"not": "a mapping"}], "must contain a mapping"),
            ("scenes.yaml", {"not": "a list"}, "must contain a list"),
        ]
        for filename, payload, message in cases:
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    git = root / "git" / "homeassistant"
                    composed = root / "composed"
                    write_yaml(area_file(git, "home", filename), payload)

                    with self.assertRaisesRegex(RuntimeError, message):
                        ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
                    self.assertFalse((composed / "automations.yaml").exists())
                    self.assertFalse((composed / "scripts.yaml").exists())
                    self.assertFalse((composed / "scenes.yaml").exists())

    def test_integrity_rejects_missing_index_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            write_yaml(area_file(git, "home", "automations.yaml"), [{"id": "one", "alias": "One"}])

            with self.assertRaisesRegex(RuntimeError, "organizer-index"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    def test_integrity_rejects_count_mismatch_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            composed = root / "composed"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
            index_path = git / ".ha-ops" / "areas" / "organizer-index.json"
            index = json.loads(index_path.read_text())
            index["automations"]["count"] = index["automations"]["count"] + 1
            write_json(index_path, index)

            with self.assertRaisesRegex(RuntimeError, "automation.*count"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    def test_integrity_rejects_script_and_scene_count_mismatch_before_writing_live(self):
        cases = [
            ("scripts", "script.*count"),
            ("scenes", "scene.*count"),
        ]
        for kind, message in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    live = seed_live_homeassistant(root)
                    git = root / "git" / "homeassistant"
                    composed = root / "composed"

                    ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
                    index_path = git / ".ha-ops" / "areas" / "organizer-index.json"
                    index = json.loads(index_path.read_text())
                    index[kind]["count"] = index[kind]["count"] + 1
                    write_json(index_path, index)

                    with self.assertRaisesRegex(RuntimeError, message):
                        ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
                    self.assertFalse((composed / "automations.yaml").exists())
                    self.assertFalse((composed / "scripts.yaml").exists())
                    self.assertFalse((composed / "scenes.yaml").exists())
