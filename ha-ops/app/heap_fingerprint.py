"""Semantic fingerprint of Home Assistant's normal heap YAML files."""

import hashlib
import json
import re
from pathlib import Path

import yaml


HEAP_FILES = {
    "automations": "automations.yaml",
    "scripts": "scripts.yaml",
    "scenes": "scenes.yaml",
}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


_INT_TAG = "tag:yaml.org,2002:int"
_INT_WITHOUT_SEXAGESIMAL = re.compile(
    r"""^(?:[-+]?0b[0-1_]+
                |[-+]?0[0-7_]+
                |[-+]?(?:0|[1-9][0-9_]*)
                |[-+]?0x[0-9a-fA-F_]+)$""",
    re.X,
)
UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [item for item in resolvers if item[0] != _INT_TAG]
    for key, resolvers in UniqueKeyLoader.yaml_implicit_resolvers.items()
}
UniqueKeyLoader.add_implicit_resolver(_INT_TAG, _INT_WITHOUT_SEXAGESIMAL, list("-+0123456789"))


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RuntimeError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def has_heap_files(root):
    root = Path(root)
    return any((root / name).exists() for name in HEAP_FILES.values())


def _load(path, default):
    path = Path(path)
    if not path.exists():
        return default
    text = path.read_text()
    if not text.strip():
        return default
    data = yaml.load(text, Loader=UniqueKeyLoader)
    return default if data is None else data


def _identity(item, index, kind):
    if isinstance(item, dict) and item.get("id"):
        return str(item["id"])
    if kind == "scenes" and isinstance(item, dict) and item.get("name"):
        return str(item["name"])
    return f"__missing_{'scene_identity' if kind == 'scenes' else 'id'}_{index}"


def fingerprint_heaps(root):
    root = Path(root)
    automations = _load(root / HEAP_FILES["automations"], [])
    scripts = _load(root / HEAP_FILES["scripts"], {})
    scenes = _load(root / HEAP_FILES["scenes"], [])
    if not isinstance(automations, list):
        raise RuntimeError("automations.yaml must contain a list")
    if not isinstance(scripts, dict):
        raise RuntimeError("scripts.yaml must contain a mapping")
    if not isinstance(scenes, list):
        raise RuntimeError("scenes.yaml must contain a list")

    automation_items = [
        {"id": _identity(item, index, "automations"), "payload": item}
        for index, item in enumerate(automations)
    ]
    scene_items = [
        {"id": _identity(item, index, "scenes"), "payload": item}
        for index, item in enumerate(scenes)
    ]
    automation_items.sort(key=lambda item: item["id"])
    scene_items.sort(key=lambda item: item["id"])
    payload = {
        "fingerprint_version": 1,
        "automations": automation_items,
        "scripts": {str(key): scripts[key] for key in sorted(scripts)},
        "scenes": scene_items,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "version": 1,
        "hash": f"sha256:{digest}",
        "counts": {
            "automations": len(automations),
            "scripts": len(scripts),
            "scenes": len(scenes),
        },
        "ids": {
            "automations": [_identity(item, index, "automations") for index, item in enumerate(automations)],
            "scripts": [str(key) for key in scripts.keys()],
            "scenes": [_identity(item, index, "scenes") for index, item in enumerate(scenes)],
        },
    }
