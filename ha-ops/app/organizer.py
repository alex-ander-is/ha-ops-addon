import json
import re
import shutil
import hashlib
from pathlib import Path

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


class OrganizerRemovedError(RuntimeError):
    pass


def _raise_removed():
    raise OrganizerRemovedError(
        "Home Assistant organizer area split is paused while the projection "
        "rewrite is pending. The .ha-ops/areas organizer projection "
        "implementation has been removed for a rewrite. Keep the organizer UI "
        "disabled until the round-trip model in "
        "ha-ops/docs/organizer-roundtrip-todo/README.md is implemented."
    )


def require_projection_available(options=None):
    if organizer_projection_enabled(options):
        _raise_removed()


def require_yaml():
    if yaml is None:
        raise RuntimeError("PyYAML is required for HA Ops organizer YAML helpers.")


def yaml_load(path, default):
    require_yaml()
    path = Path(path)
    if not path.exists():
        return default
    text = path.read_text()
    if not text.strip():
        return default
    data = yaml.load(text, Loader=UniqueKeyLoader)
    return default if data is None else data


def yaml_dump_text(data):
    require_yaml()
    if annotated_yaml_dump is not None:
        text = annotated_yaml_dump(data)
    else:
        text = yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            Dumper=HACompatibleDumper,
        )
    return text.replace(": null\n", ":\n")


def yaml_dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_dump_text(data))


def canonical_json_bytes(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def organizer_options(options):
    return options if isinstance(options, dict) else {}


def organizer_projection_enabled(options):
    if options is None or options is False:
        return False
    if isinstance(options, dict) and options.get("enabled") is False:
        return False
    return True


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
    return any(item.is_file() and item.name in HEAP_FILES.values() for item in path.rglob("*"))


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


def normalize_area(value):
    value = str(value or "").strip()
    if not value:
        return UNKNOWN_BUCKET
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.lower()).strip("_")
    return value or UNKNOWN_BUCKET


def heap_defaults():
    return {"automations": [], "scripts": {}, "scenes": []}


def read_heaps(root):
    root = Path(root)
    defaults = heap_defaults()
    automations = yaml_load(root / HEAP_FILES["automations"], defaults["automations"])
    scripts = yaml_load(root / HEAP_FILES["scripts"], defaults["scripts"])
    scenes = yaml_load(root / HEAP_FILES["scenes"], defaults["scenes"])
    if not isinstance(automations, list):
        raise RuntimeError("automations.yaml must contain a list")
    if not isinstance(scripts, dict):
        raise RuntimeError("scripts.yaml must contain a mapping")
    if not isinstance(scenes, list):
        raise RuntimeError("scenes.yaml must contain a list")
    return automations, scripts, scenes


def heap_summary(root):
    automations, scripts, scenes = read_heaps(root)
    return {
        "automations": {"input_count": len(automations), "output_count": len(automations)},
        "scripts": {"input_count": len(scripts), "output_count": len(scripts)},
        "scenes": {"input_count": len(scenes), "output_count": len(scenes)},
    }


def copy_heap_files(src_root, dest_root):
    src_root = Path(src_root)
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for filename in HEAP_FILES.values():
        src = src_root / filename
        dest = dest_root / filename
        if src.exists():
            if src.resolve() == dest.resolve():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        elif dest.exists() or dest.is_symlink():
            dest.unlink()


def split_live_heaps_to_git(live_root, git_root, options=None):
    if organizer_projection_enabled(options):
        _raise_removed()
    summary = heap_summary(live_root) if has_heap_files(live_root) else {
        "automations": {"input_count": 0, "output_count": 0},
        "scripts": {"input_count": 0, "output_count": 0},
        "scenes": {"input_count": 0, "output_count": 0},
    }
    copy_heap_files(live_root, git_root)
    clean_organized_root(git_root, options)
    return summary


def compose_git_view_to_live(git_root, live_root, options=None):
    if organizer_projection_enabled(options):
        _raise_removed()
    summary = heap_summary(git_root) if has_heap_files(git_root) else {
        "automations": {"input_count": 0, "output_count": 0},
        "scripts": {"input_count": 0, "output_count": 0},
        "scenes": {"input_count": 0, "output_count": 0},
    }
    copy_heap_files(git_root, live_root)
    clean_organized_root(live_root, options)
    return summary


def fingerprint_payload(automations, scripts, scenes):
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
        "scripts": {str(key): scripts[key] for key in sorted(scripts)},
        "scenes": scene_items,
    }


def fingerprint_for(automations, scripts, scenes):
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
