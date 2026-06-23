import json
import re
import shutil
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


DEFAULT_ORGANIZED_ROOT = ".ha-ops/areas"
INDEX_NAME = "organizer-index.json"
UNKNOWN_BUCKET = ".unknown"
MIXED_BUCKET = ".mixed"
HEAP_FILES = {
    "automations": "automations.yaml",
    "scripts": "scripts.yaml",
    "scenes": "scenes.yaml",
}


class OrganizerRemovedError(RuntimeError):
    pass


def _raise_removed():
    raise OrganizerRemovedError(
        "The .ha-ops/areas organizer projection implementation has been "
        "removed for a rewrite. Keep the organizer UI disabled until the "
        "round-trip model in ha-ops/docs/organizer-roundtrip-todo/README.md "
        "is implemented."
    )


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
    data = yaml.safe_load(text)
    return default if data is None else data


def yaml_dump_text(data):
    require_yaml()
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def yaml_dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_dump_text(data))


def canonical_json_bytes(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


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


def split_live_heaps_to_git(live_root, git_root, options=None):
    _raise_removed()


def compose_git_view_to_live(git_root, live_root, options=None):
    _raise_removed()


def fingerprint_heaps(root):
    _raise_removed()
