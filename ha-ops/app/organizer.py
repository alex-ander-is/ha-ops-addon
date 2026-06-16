import json
import re
import shutil
import hashlib
from collections import Counter
from pathlib import Path

import rfc8785

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

try:
    from annotatedyaml.dumper import dump as annotated_yaml_dump
except ModuleNotFoundError:  # pragma: no cover
    annotated_yaml_dump = None


DEFAULT_ORGANIZED_ROOT = ".ha-ops/areas"
INDEX_NAME = "organizer-index.json"
UNKNOWN_BUCKET = ".unknown"
MIXED_BUCKET = ".mixed"
HEAP_FILES = {
    "automations": "automations.yaml",
    "scripts": "scripts.yaml",
    "scenes": "scenes.yaml",
}
ENTITY_DOMAINS = [
    "automation",
    "binary_sensor",
    "button",
    "calendar",
    "camera",
    "climate",
    "cover",
    "device_tracker",
    "event",
    "fan",
    "humidifier",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "light",
    "lock",
    "media_player",
    "number",
    "person",
    "remote",
    "scene",
    "script",
    "select",
    "sensor",
    "siren",
    "switch",
    "timer",
    "update",
    "vacuum",
    "water_heater",
    "weather",
    "zone",
]
ENTITY_RE = re.compile(r"\b(?:" + "|".join(re.escape(domain) for domain in ENTITY_DOMAINS) + r")\.[A-Za-z0-9_]+\b")


class UniqueKeyLoader(yaml.SafeLoader if yaml is not None else object):
    pass


if yaml is not None:
    try:
        HACompatibleDumper = yaml.CSafeDumper
    except AttributeError:  # pragma: no cover
        HACompatibleDumper = yaml.SafeDumper

    YAML_INT_TAG = "tag:yaml.org,2002:int"
    YAML_INT_PATTERN_WITHOUT_SEXAGESIMAL = re.compile(
        r"""^(?:[-+]?0b[0-1_]+
                    |[-+]?0[0-7_]+
                    |[-+]?(?:0|[1-9][0-9_]*)
                    |[-+]?0x[0-9a-fA-F_]+)$""",
        re.X,
    )

    UniqueKeyLoader.yaml_implicit_resolvers = {
        key: [item for item in resolvers if item[0] != YAML_INT_TAG]
        for key, resolvers in UniqueKeyLoader.yaml_implicit_resolvers.items()
    }
    UniqueKeyLoader.add_implicit_resolver(
        YAML_INT_TAG,
        YAML_INT_PATTERN_WITHOUT_SEXAGESIMAL,
        list("-+0123456789"),
    )

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise RuntimeError(f"duplicate YAML key: {key}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)


def require_yaml():
    if yaml is None:
        raise RuntimeError("PyYAML is required for HA Ops organizer. Install py3-yaml in the add-on image.")


def yaml_dump_text(data):
    require_yaml()
    if annotated_yaml_dump is not None:
        return annotated_yaml_dump(data)
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        Dumper=HACompatibleDumper,
    ).replace(": null\n", ":\n")


def yaml_load(path, default):
    require_yaml()
    if not path.exists():
        return default
    text = path.read_text()
    if not text.strip():
        return default
    data = yaml.load(text, Loader=UniqueKeyLoader)
    return default if data is None else data


def yaml_dump(path, data):
    require_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_dump_text(data))


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def organizer_options(options):
    return options if isinstance(options, dict) else {}


def organized_root_name(options=None):
    options = organizer_options(options)
    value = str(options.get("organized_root") or DEFAULT_ORGANIZED_ROOT)
    path = Path(value)
    if path.is_absolute():
        raise RuntimeError(f"Invalid organizer organized_root: {value}")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"Invalid organizer organized_root: {value}")
    return path.as_posix()


def organized_root(root, options=None):
    root = Path(root)
    path = root / organized_root_name(options)
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise RuntimeError(f"Organizer organized_root escapes target root: {path}")
    return path


def has_heap_files(root):
    root = Path(root)
    return any((root / filename).exists() for filename in HEAP_FILES.values())


def has_organized_view(root, options=None):
    path = organized_root(root, options)
    if not path.exists() or not path.is_dir():
        return False
    return any((item.is_file() and item.name in HEAP_FILES.values()) for item in path.rglob("*"))


def clean_organized_root(root, options=None, preserve_unmanaged=False):
    path = organized_root(root, options)
    if not path.exists() and not path.is_symlink():
        return
    if not path.is_dir() or path.is_symlink():
        path.unlink()
        return
    if not preserve_unmanaged:
        shutil.rmtree(path)
        return
    for item in path.rglob("*"):
        if item.is_file() and item.name in {*HEAP_FILES.values(), INDEX_NAME}:
            item.unlink()
    for item in sorted(path.rglob("*"), key=lambda child: len(child.parts), reverse=True):
        if item.is_dir() and not any(item.iterdir()):
            item.rmdir()


def generated_organized_relative_files(root, options=None):
    path = organized_root(root, options)
    if not path.exists():
        return []
    generated_names = {*HEAP_FILES.values(), INDEX_NAME}
    return sorted(
        item.relative_to(root)
        for item in path.rglob("*")
        if item.is_file() and item.name in generated_names
    )


def normalize_area(value):
    value = str(value or "").strip()
    if not value:
        return UNKNOWN_BUCKET
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.lower()).strip("_")
    return value or UNKNOWN_BUCKET


def normalize_text(value):
    value = str(value or "")
    return re.sub(r"[^A-Za-z0-9_]+", "_", value.lower()).strip("_")


def collect_values(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(collect_values(item))
        return found
    if isinstance(value, dict):
        found = []
        for item in value.values():
            found.extend(collect_values(item))
        return found
    return []


def collect_references(value):
    refs = {"areas": [], "devices": [], "entities": []}

    def walk(item):
        if isinstance(item, dict):
            for key, val in item.items():
                key = str(key)
                if ENTITY_RE.fullmatch(key):
                    refs["entities"].append(key)
                if key == "area_id":
                    refs["areas"].extend(collect_values(val))
                elif key == "device_id":
                    refs["devices"].extend(collect_values(val))
                elif key == "entity_id":
                    refs["entities"].extend(collect_values(val))
                elif key == "service":
                    for service in collect_values(val):
                        if service.startswith("script.") and ENTITY_RE.fullmatch(service):
                            refs["entities"].append(service)
                else:
                    walk(val)
        elif isinstance(item, list):
            for val in item:
                walk(val)
        elif isinstance(item, str):
            refs["entities"].extend(ENTITY_RE.findall(item))

    walk(value)
    return refs


def area_from_candidates(candidates):
    clean = [normalize_area(value) for value in candidates if normalize_area(value) != UNKNOWN_BUCKET]
    if not clean:
        return None
    counts = Counter(clean)
    if len(counts) == 1:
        return clean[0]
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return MIXED_BUCKET
    return top[0][0]


class RegistryIndex:
    def __init__(self, root):
        storage = Path(root) / ".storage"
        self.area_slug_by_id = {}
        self.entity_by_unique = {}
        self.area_by_entity = {}
        self.device_by_entity = {}
        self.area_by_device = {}

        for area in load_json(storage / "core.area_registry", {}).get("data", {}).get("areas", []):
            area_id = area.get("id") or area.get("area_id")
            if area_id:
                self.area_slug_by_id[area_id] = normalize_area(area.get("name") or area_id)

        for device in load_json(storage / "core.device_registry", {}).get("data", {}).get("devices", []):
            device_id = device.get("id")
            area_id = device.get("area_id")
            if device_id and area_id:
                self.area_by_device[device_id] = self.area_slug(area_id)

        for entity in load_json(storage / "core.entity_registry", {}).get("data", {}).get("entities", []):
            entity_id = entity.get("entity_id")
            unique_id = entity.get("unique_id")
            domain = str(entity_id or "").split(".", 1)[0]
            if domain and unique_id is not None:
                self.entity_by_unique[(domain, str(unique_id))] = entity
            if entity_id and entity.get("area_id"):
                self.area_by_entity[entity_id] = self.area_slug(entity.get("area_id"))
            if entity_id and entity.get("device_id"):
                self.device_by_entity[entity_id] = entity.get("device_id")

    def area_slug(self, area_id):
        return self.area_slug_by_id.get(area_id, normalize_area(area_id))

    def entity_area_by_unique(self, domain, unique_id):
        entity = self.entity_by_unique.get((domain, str(unique_id)))
        if not entity or not entity.get("area_id"):
            return None
        return self.area_slug(entity.get("area_id"))

    def device_area(self, device_id):
        return self.area_by_device.get(device_id)

    def entity_area(self, entity_id):
        if entity_id in self.area_by_entity:
            return self.area_by_entity[entity_id]
        device_id = self.device_by_entity.get(entity_id)
        if device_id:
            return self.device_area(device_id)
        return None


def overrides_for(kind, options):
    overrides = organizer_options(options).get("overrides", {})
    if not isinstance(overrides, dict):
        return {}
    value = overrides.get(kind, {})
    return value if isinstance(value, dict) else {}


def prefix_area(text_values, options):
    prefixes = organizer_options(options).get("prefixes", {})
    if not isinstance(prefixes, dict):
        return None
    normalized_values = [normalize_text(value) for value in text_values if normalize_text(value)]
    for area, area_prefixes in prefixes.items():
        if isinstance(area_prefixes, str):
            area_prefixes = [area_prefixes]
        for prefix in area_prefixes or []:
            normalized_prefix = normalize_text(prefix)
            if normalized_prefix and any(value.startswith(normalized_prefix) for value in normalized_values):
                return normalize_area(area)
    return None


def route_item(kind, identity, item, registry, options, text_values):
    override = overrides_for(kind, options).get(str(identity))
    if override:
        return normalize_area(override)

    domain = {"automations": "automation", "scripts": "script", "scenes": "scene"}[kind]
    ui_area = registry.entity_area_by_unique(domain, identity)
    if ui_area:
        return ui_area

    area = prefix_area(text_values, options)
    if area:
        return area

    refs = collect_references(item)
    area = area_from_candidates(registry.area_slug(area_id) for area_id in refs["areas"])
    if area:
        return area

    area = area_from_candidates(registry.device_area(device_id) for device_id in refs["devices"])
    if area:
        return area

    area = area_from_candidates(registry.entity_area(entity_id) for entity_id in refs["entities"])
    if area:
        return area

    return UNKNOWN_BUCKET


def automation_identity(item, index):
    if isinstance(item, dict) and item.get("id"):
        return str(item.get("id"))
    return f"__missing_id_{index}"


def scene_identity(item, index):
    if isinstance(item, dict) and item.get("id"):
        return str(item.get("id"))
    if isinstance(item, dict) and item.get("name"):
        return str(item.get("name"))
    return f"__missing_scene_identity_{index}"


def validate_unique(values, label):
    seen = set()
    for value in values:
        if not value or str(value).startswith("__missing_"):
            continue
        if value in seen:
            raise RuntimeError(f"duplicate {label}: {value}")
        seen.add(value)


def read_heaps(root):
    root = Path(root)
    automations = yaml_load(root / HEAP_FILES["automations"], [])
    scripts = yaml_load(root / HEAP_FILES["scripts"], {})
    scenes = yaml_load(root / HEAP_FILES["scenes"], [])
    if not isinstance(automations, list):
        raise RuntimeError("automations.yaml must contain a list")
    if not isinstance(scripts, dict):
        raise RuntimeError("scripts.yaml must contain a mapping")
    if not isinstance(scenes, list):
        raise RuntimeError("scenes.yaml must contain a list")
    return automations, scripts, scenes


def index_for(automations, scripts, scenes):
    automation_ids = [automation_identity(item, index) for index, item in enumerate(automations)]
    script_ids = [str(key) for key in scripts.keys()]
    scene_ids = [scene_identity(item, index) for index, item in enumerate(scenes)]
    validate_unique(automation_ids, "automation id")
    validate_unique(script_ids, "script key")
    validate_unique(scene_ids, "scene identity")
    return {
        "version": 1,
        "automations": {"count": len(automations), "ids": automation_ids},
        "scripts": {"count": len(scripts), "ids": script_ids},
        "scenes": {"count": len(scenes), "ids": scene_ids},
    }


def canonical_json_bytes(data):
    try:
        return rfc8785.dumps(data)
    except Exception as exc:
        raise RuntimeError(f"failed to canonicalize organizer fingerprint data: {exc}") from exc


def fingerprint_payload(automations, scripts, scenes):
    script_items = {str(key): scripts[key] for key in scripts}
    automation_items = [
        {"id": automation_identity(item, index), "payload": item}
        for index, item in enumerate(automations)
    ]
    scene_items = [
        {"id": scene_identity(item, index), "payload": item}
        for index, item in enumerate(scenes)
    ]
    automation_items.sort(key=lambda item: item["id"])
    scene_items.sort(key=lambda item: item["id"])
    return {
        "fingerprint_version": 1,
        "automations": automation_items,
        "scripts": {key: script_items[key] for key in sorted(script_items)},
        "scenes": scene_items,
    }


def fingerprint_for(automations, scripts, scenes):
    validate_unique(
        [automation_identity(item, index) for index, item in enumerate(automations)],
        "automation id",
    )
    validate_unique([str(key) for key in scripts.keys()], "script key")
    validate_unique(
        [scene_identity(item, index) for index, item in enumerate(scenes)],
        "scene identity",
    )
    digest = hashlib.sha256(canonical_json_bytes(fingerprint_payload(automations, scripts, scenes))).hexdigest()
    return f"sha256:{digest}"


def fingerprint_heaps(root):
    automations, scripts, scenes = read_heaps(root)
    return {
        "version": 1,
        "hash": fingerprint_for(automations, scripts, scenes),
        "counts": {
            "automations": len(automations),
            "scripts": len(scripts),
            "scenes": len(scenes),
        },
        "ids": {
            "automations": [automation_identity(item, index) for index, item in enumerate(automations)],
            "scripts": [str(key) for key in scripts.keys()],
            "scenes": [scene_identity(item, index) for index, item in enumerate(scenes)],
        },
    }


def summary_from_index(index):
    return {
        kind: {"input_count": index[kind]["count"], "output_count": index[kind]["count"]}
        for kind in ("automations", "scripts", "scenes")
    }


def route_heaps(root, automations, scripts, scenes, options):
    registry = RegistryIndex(root)
    routed = {"automations": {}, "scripts": {}, "scenes": {}}

    for index, item in enumerate(automations):
        identity = automation_identity(item, index)
        alias = item.get("alias") if isinstance(item, dict) else ""
        area = route_item("automations", identity, item, registry, options, [alias, identity])
        routed["automations"].setdefault(area, []).append(item)

    for key, item in scripts.items():
        alias = item.get("alias") if isinstance(item, dict) else ""
        area = route_item("scripts", key, item, registry, options, [key, alias])
        routed["scripts"].setdefault(area, {})[key] = item

    for index, item in enumerate(scenes):
        identity = scene_identity(item, index)
        name = item.get("name") if isinstance(item, dict) else ""
        area = route_item("scenes", identity, item, registry, options, [name, identity])
        routed["scenes"].setdefault(area, []).append(item)

    return routed


def split_live_heaps_to_git(live_root, git_root, options=None):
    if not has_heap_files(live_root):
        return {
            "automations": {"input_count": 0, "output_count": 0},
            "scripts": {"input_count": 0, "output_count": 0},
            "scenes": {"input_count": 0, "output_count": 0},
        }

    options = organizer_options(options)
    automations, scripts, scenes = read_heaps(live_root)
    index = index_for(automations, scripts, scenes)
    routed = route_heaps(live_root, automations, scripts, scenes, options)

    git_root = Path(git_root)
    root = organized_root(git_root, options)
    clean_organized_root(git_root, options, preserve_unmanaged=True)
    root.mkdir(parents=True, exist_ok=True)

    for kind, filename in HEAP_FILES.items():
        for area, payload in routed[kind].items():
            yaml_dump(root / area / filename, payload)

    write_json(root / INDEX_NAME, index)

    if not options.get("keep_heap_files", False):
        for filename in HEAP_FILES.values():
            path = git_root / filename
            if path.exists() or path.is_symlink():
                path.unlink()

    return summary_from_index(index)


def read_organized_heaps(git_root, options=None):
    options = organizer_options(options)
    root = organized_root(git_root, options)
    automations = []
    scripts = {}
    scenes = []

    for area_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        automation_path = area_dir / HEAP_FILES["automations"]
        if automation_path.exists():
            data = yaml_load(automation_path, [])
            if not isinstance(data, list):
                raise RuntimeError(f"{automation_path} must contain a list")
            automations.extend(data)

        scripts_path = area_dir / HEAP_FILES["scripts"]
        if scripts_path.exists():
            data = yaml_load(scripts_path, {})
            if not isinstance(data, dict):
                raise RuntimeError(f"{scripts_path} must contain a mapping")
            for key, value in data.items():
                if key in scripts:
                    raise RuntimeError(f"duplicate script key: {key}")
                scripts[key] = value

        scenes_path = area_dir / HEAP_FILES["scenes"]
        if scenes_path.exists():
            data = yaml_load(scenes_path, [])
            if not isinstance(data, list):
                raise RuntimeError(f"{scenes_path} must contain a list")
            scenes.extend(data)

    return automations, scripts, scenes


def validate_index_payload(index, actual):
    for kind in ("automations", "scripts", "scenes"):
        if kind not in index or "count" not in index[kind] or "ids" not in index[kind]:
            raise RuntimeError(f"organizer index missing {kind} integrity data")


def verify_written_heaps(live_root, expected):
    applied = index_for(*read_heaps(live_root))
    for kind in ("automations", "scripts", "scenes"):
        if expected[kind]["count"] != applied[kind]["count"]:
            raise RuntimeError(
                f"{kind[:-1]} apply count mismatch: expected {expected[kind]['count']}, got {applied[kind]['count']}"
            )
        if sorted(expected[kind]["ids"]) != sorted(applied[kind]["ids"]):
            raise RuntimeError(f"{kind[:-1]} apply identity mismatch")


def compose_git_view_to_live(git_root, live_root, options=None):
    options = organizer_options(options)
    if not has_organized_view(git_root, options):
        return {
            "automations": {"input_count": 0, "output_count": 0},
            "scripts": {"input_count": 0, "output_count": 0},
            "scenes": {"input_count": 0, "output_count": 0},
        }

    automations, scripts, scenes = read_organized_heaps(git_root, options)
    actual = index_for(automations, scripts, scenes)
    index_path = organized_root(git_root, options) / INDEX_NAME
    if not index_path.exists():
        raise RuntimeError(f"{INDEX_NAME} is required for organized Home Assistant config")
    validate_index_payload(load_json(index_path, {}), actual)

    live_root = Path(live_root)
    yaml_dump(live_root / HEAP_FILES["automations"], automations)
    yaml_dump(live_root / HEAP_FILES["scripts"], scripts)
    yaml_dump(live_root / HEAP_FILES["scenes"], scenes)
    verify_written_heaps(live_root, actual)

    return {
        kind: {"input_count": actual[kind]["count"], "output_count": actual[kind]["count"]}
        for kind in ("automations", "scripts", "scenes")
    }
