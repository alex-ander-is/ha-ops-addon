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


PROJECTION_SKIP_REASON = "full .ha-ops/areas projection is pending the organizer rewrite"


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


def ha_dump(data):
    try:
        dumper = yaml.CSafeDumper
    except AttributeError:
        dumper = yaml.SafeDumper
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        Dumper=dumper,
    ).replace(": null\n", ":\n")


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

    def test_loader_keeps_unquoted_sexagesimal_values_as_strings(self):
        cases = [
            "1:02",
            "1:02:03",
            "1:2:3",
            "9:59:59",
            "10:00:00",
            "21:00:00",
            "23:59:59",
            "+1:02:03",
            "-1:02:03",
            "1_2:34:56",
        ]
        for value in cases:
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "values.yaml"
                    path.write_text(f"value: {value}\n")

                    data = ORGANIZER.yaml_load(path, {})

                    self.assertEqual(data["value"], value)

    def test_loader_still_parses_non_sexagesimal_int_values(self):
        cases = [
            ("0", 0),
            ("42", 42),
            ("+42", 42),
            ("-42", -42),
            ("1_000", 1000),
            ("0b1010", 10),
            ("0755", 493),
            ("0x10", 16),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "values.yaml"
                    path.write_text(f"value: {source}\n")

                    data = ORGANIZER.yaml_load(path, {})

                    self.assertEqual(data["value"], expected)
                    self.assertIsInstance(data["value"], int)

    def test_loader_does_not_change_global_safe_loader_sexagesimal_behavior(self):
        self.assertEqual(yaml.safe_load("value: 21:00:00\n")["value"], 75600)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "values.yaml"
            path.write_text("value: 21:00:00\n")

            data = ORGANIZER.yaml_load(path, {})

            self.assertEqual(data["value"], "21:00:00")

    def test_automation_time_fields_survive_unquoted_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "automations.yaml"
            path.write_text(
                "- id: time\n"
                "  trigger:\n"
                "  - platform: time\n"
                "    at: 21:00:00\n"
                "  condition:\n"
                "  - condition: time\n"
                "    after: 10:00:00\n"
                "    before: 06:00:00\n"
                "  data:\n"
                "    count: 42\n"
                "    hex: 0x10\n"
            )

            data = ORGANIZER.yaml_load(path, [])
            trigger = data[0]["trigger"][0]
            condition = data[0]["condition"][0]

            self.assertEqual(trigger["at"], "21:00:00")
            self.assertEqual(condition["after"], "10:00:00")
            self.assertEqual(condition["before"], "06:00:00")
            self.assertEqual(data[0]["data"]["count"], 42)
            self.assertEqual(data[0]["data"]["hex"], 16)

    def test_dump_uses_home_assistant_null_cleanup(self):
        self.assertEqual(ORGANIZER.yaml_dump_text({"value": None}), "value:\n")

    def test_dump_cleans_annotatedyaml_null_values(self):
        original_dump = ORGANIZER.annotated_yaml_dump
        try:
            ORGANIZER.annotated_yaml_dump = lambda data: "sequence:\n  thumbnail: null\n"

            self.assertEqual(ORGANIZER.yaml_dump_text({"thumbnail": None}), "sequence:\n  thumbnail:\n")
        finally:
            ORGANIZER.annotated_yaml_dump = original_dump

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_split_uses_home_assistant_style_for_single_line_template_scalars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            storage = live / ".storage"
            storage.mkdir(parents=True)
            long_template = "{{ trigger.payload_json.action in ['2_single', '2_double'] }}"
            automations = [
                {
                    "id": "long_auto",
                    "alias": "long_auto",
                    "triggers": [{"topic": "z2m/button", "trigger": "mqtt"}],
                    "conditions": [{"condition": "template", "value_template": long_template}],
                    "actions": [{"action": "script.hallway_toggle_light"}],
                }
            ]
            write_yaml(
                live / "automations.yaml",
                automations,
            )
            write_yaml(live / "scripts.yaml", {})
            write_yaml(live / "scenes.yaml", [])
            write_json(storage / "core.area_registry", area_registry())
            write_json(storage / "core.device_registry", device_registry())
            write_json(storage / "core.entity_registry", entity_registry())

            git = root / "git" / "homeassistant"
            ORGANIZER.split_live_heaps_to_git(
                live,
                git,
                options={"overrides": {"automations": {"long_auto": "home"}}},
            )

            text = (git / ".ha-ops" / "areas" / "home" / "automations.yaml").read_text()
            self.assertEqual(text, ha_dump(automations))
            self.assertIn("value_template: '{{ trigger.payload_json.action in [''2_single'', ''2_double'']\n", text)

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_split_uses_home_assistant_style_for_multi_statement_jinja_scalars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            storage = live / ".storage"
            storage.mkdir(parents=True)
            brightness_template = (
                "{% set fallback = 25 %} "
                "{% set minimum = state_attr('input_number.dining_room_dimmer_range', 'min') | float(0) %} "
                "{% set maximum = state_attr('input_number.dining_room_dimmer_range', 'max') | float(100) %} "
                "{{ [minimum, [fallback, maximum] | min] | max }}"
            )
            automations = [
                {
                    "id": "brightness_auto",
                    "alias": "brightness_auto",
                    "triggers": [{"entity_id": "switch.dining_room_light", "trigger": "state"}],
                    "conditions": [],
                    "actions": [
                        {
                            "action": "light.turn_on",
                            "data": {"brightness_pct": brightness_template},
                            "target": {"entity_id": "light.dining_room_dimmer"},
                        }
                    ],
                }
            ]
            write_yaml(
                live / "automations.yaml",
                automations,
            )
            write_yaml(live / "scripts.yaml", {})
            write_yaml(live / "scenes.yaml", [])
            write_json(storage / "core.area_registry", area_registry())
            write_json(storage / "core.device_registry", device_registry())
            write_json(storage / "core.entity_registry", entity_registry())

            git = root / "git" / "homeassistant"
            ORGANIZER.split_live_heaps_to_git(
                live,
                git,
                options={"overrides": {"automations": {"brightness_auto": "home"}}},
            )

            text = (git / ".ha-ops" / "areas" / "home" / "automations.yaml").read_text()
            self.assertEqual(text, ha_dump(automations))
            self.assertIn("brightness_pct: '{% set fallback = 25 %}", text)

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_split_uses_home_assistant_style_for_jinja_notification_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            storage = live / ".storage"
            storage.mkdir(parents=True)
            title = "🛀 {{ now().strftime('%H:%M') }} Washing Machine"
            automations = [
                {
                    "id": "laundry_auto",
                    "alias": "laundry_auto",
                    "triggers": [{"entity_id": "sensor.washing_machine", "trigger": "state"}],
                    "conditions": [],
                    "actions": [
                        {
                            "action": "notify.alex",
                            "data": {
                                "title": title,
                                "message": "You most likely forgot your laundry",
                            },
                        }
                    ],
                }
            ]
            write_yaml(
                live / "automations.yaml",
                automations,
            )
            write_yaml(live / "scripts.yaml", {})
            write_yaml(live / "scenes.yaml", [])
            write_json(storage / "core.area_registry", area_registry())
            write_json(storage / "core.device_registry", device_registry())
            write_json(storage / "core.entity_registry", entity_registry())

            git = root / "git" / "homeassistant"
            ORGANIZER.split_live_heaps_to_git(
                live,
                git,
                options={"overrides": {"automations": {"laundry_auto": "home"}}},
            )

            text = (git / ".ha-ops" / "areas" / "home" / "automations.yaml").read_text()
            self.assertEqual(text, ha_dump(automations))
            self.assertIn('title: "\\U0001F6C0 {{ now().strftime(\'%H:%M\') }} Washing Machine"\n', text)

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_split_routes_scenes_by_ui_area_and_entity_map_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())

            self.assertIn("scene_movie", ids(read_yaml(area_file(git, "home", "scenes.yaml"))))
            self.assertIn("scene_garage", ids(read_yaml(area_file(git, "garage", "scenes.yaml"))))

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
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

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_integrity_rejects_missing_index_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            write_yaml(area_file(git, "home", "automations.yaml"), [{"id": "one", "alias": "One"}])

            with self.assertRaisesRegex(RuntimeError, "organizer-index"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_integrity_rejects_unreferenced_nested_heap_file_before_writing_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = root / "git" / "homeassistant"
            composed = root / "composed"
            nested = git / ".ha-ops" / "areas" / "home" / "nested" / "automations.yaml"
            write_yaml(nested, [{"id": "hidden", "alias": "Hidden"}])
            write_json(
                git / ".ha-ops" / "areas" / "organizer-index.json",
                {
                    "version": 1,
                    "automations": {"count": 0, "ids": []},
                    "scripts": {"count": 0, "ids": []},
                    "scenes": {"count": 0, "ids": []},
                },
            )

            with self.assertRaisesRegex(RuntimeError, "unreferenced organizer file.*home/nested/automations.yaml"):
                ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())
            self.assertFalse((composed / "automations.yaml").exists())

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_compose_allows_source_deletions_relative_to_old_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = seed_live_homeassistant(root)
            git = root / "git" / "homeassistant"
            composed = root / "composed"

            ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
            home_path = area_file(git, "home", "automations.yaml")
            automations = [item for item in read_yaml(home_path) if item["id"] != "auto_time"]
            write_yaml(home_path, automations)

            summary = ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())

            applied = read_yaml(composed / "automations.yaml")
            self.assertNotIn("auto_time", ids(applied))
            self.assertEqual(summary["automations"]["input_count"], len(automation_super_set()) - 1)
            self.assertEqual(summary["automations"]["output_count"], len(automation_super_set()) - 1)

    @unittest.skip(PROJECTION_SKIP_REASON)
    def test_compose_allows_script_and_scene_deletions_relative_to_old_index(self):
        cases = ("scripts", "scenes")
        for kind in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    live = seed_live_homeassistant(root)
                    git = root / "git" / "homeassistant"
                    composed = root / "composed"

                    ORGANIZER.split_live_heaps_to_git(live, git, options=self.options())
                    if kind == "scripts":
                        scripts_path = area_file(git, "home", "scripts.yaml")
                        scripts = read_yaml(scripts_path)
                        scripts.pop("home_music")
                        write_yaml(scripts_path, scripts)
                        expected = len(script_super_set()) - 1
                    else:
                        scenes_path = area_file(git, "home", "scenes.yaml")
                        scenes = [item for item in read_yaml(scenes_path) if item["id"] != "scene_movie"]
                        write_yaml(scenes_path, scenes)
                        expected = len(scene_super_set()) - 1

                    summary = ORGANIZER.compose_git_view_to_live(git, composed, options=self.options())

                    self.assertEqual(summary[kind]["input_count"], expected)
                    self.assertEqual(summary[kind]["output_count"], expected)

    def test_fingerprint_is_stable_for_object_and_rule_order(self):
        automations = [
            {"id": "two", "alias": "Two", "action": [{"service": "notify.notify"}]},
            {"action": [{"service": "light.turn_on"}], "alias": "One", "id": "one"},
        ]
        reordered = [
            {"id": "one", "alias": "One", "action": [{"service": "light.turn_on"}]},
            {"alias": "Two", "action": [{"service": "notify.notify"}], "id": "two"},
        ]

        left = ORGANIZER.fingerprint_for(automations, {"b": {"sequence": []}, "a": {"sequence": []}}, [])
        right = ORGANIZER.fingerprint_for(reordered, {"a": {"sequence": []}, "b": {"sequence": []}}, [])

        self.assertEqual(left, right)

    def test_fingerprint_changes_when_action_order_changes(self):
        automations = [
            {
                "id": "one",
                "action": [
                    {"service": "light.turn_on"},
                    {"service": "notify.notify"},
                ],
            }
        ]
        reordered_actions = [
            {
                "id": "one",
                "action": [
                    {"service": "notify.notify"},
                    {"service": "light.turn_on"},
                ],
            }
        ]

        self.assertNotEqual(
            ORGANIZER.fingerprint_for(automations, {}, []),
            ORGANIZER.fingerprint_for(reordered_actions, {}, []),
        )
