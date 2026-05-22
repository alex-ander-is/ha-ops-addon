import fnmatch
import hashlib
import json
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import storage_managed
import targets as target_model
import organizer


@dataclass(frozen=True)
class SyncContext:
    add_detail: Callable[..., Any]
    addon_action: Callable[..., Any]
    clean_dir_names: set
    clean_file_patterns: list
    clean_paths: list
    core_restart: Callable[[], Any]
    core_reload_yaml: Callable[[], Any]
    core_start: Callable[[], Any]
    core_stop: Callable[[], Any]
    do_core_check: Callable[[], Any]
    export_excludes: list
    ha_dirs: list
    ha_root_excludes: set
    ha_root_patterns: list
    log: Callable[..., Any]
    protected_storage_files: set
    restart_or_start_addon: Callable[..., Any]
    run_command: Callable[..., Any]
    stop_addon_for_sync: Callable[..., Any]
    storage_allowlist: list
    work_dir: Path
    zigbee2mqtt_paths: list


@dataclass
class ChangeSet:
    changed_yaml: bool = False
    changed_storage: bool = False
    changed_protected_storage: bool = False

    def any(self):
        return self.changed_yaml or self.changed_storage or self.changed_protected_storage


def has_managed_content(path):
    if path.is_file():
        return path.name != ".gitkeep"

    for child in path.rglob("*"):
        if child.is_file() and child.name != ".gitkeep":
            return True
        if child.is_symlink() and child.name != ".gitkeep":
            return True
    return False


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def sync_tree(src, dest, delete, excludes, run_command):
    ensure_dir(dest)
    command = ["rsync", "-a", "--checksum"]
    if delete:
        command.append("--delete")
    for pattern in excludes or []:
        command.append(f"--exclude={pattern}")
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Sync failed from {src} to {dest}:\n{result.stderr.strip()}")


def export_tree(src, dest, delete, export_excludes, run_command):
    ensure_dir(dest)
    command = ["rsync", "-a", "--checksum"]
    if delete:
        command.append("--delete")
    for pattern in export_excludes:
        command.append(f"--exclude={pattern}")
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Copy failed from {src} to {dest}:\n{result.stderr.strip()}")


def safe_remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def clean_path_matches(path, relative, pattern):
    pattern = pattern.rstrip("/")
    if not pattern:
        return False
    if "/" in pattern:
        return fnmatch.fnmatch(relative, pattern) or relative == pattern or relative.startswith(f"{pattern}/")
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(path.name, pattern)
    return path.name == pattern or relative == pattern


def clean_export_destination(dest, clean_paths, clean_dir_names, clean_file_patterns):
    ensure_dir(dest)
    removed = set()

    for path in sorted(list(dest.rglob("*")), key=lambda item: len(item.parts), reverse=True):
        if not path.exists() and not path.is_symlink():
            continue
        relative = str(path.relative_to(dest))
        if path.is_dir() and (path.name in clean_dir_names or any(clean_path_matches(path, relative, pattern) for pattern in clean_paths)):
            safe_remove_path(path)
            removed.add(relative)
            continue
        if path.is_file() and (
            any(clean_path_matches(path, relative, pattern) for pattern in clean_paths)
            or any(fnmatch.fnmatch(path.name, pattern) for pattern in clean_file_patterns)
        ):
            safe_remove_path(path)
            removed.add(relative)

    return len(removed)


def export_storage_allowlist(src, dest, storage_allowlist):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    for name in storage_allowlist:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def copy_homeassistant_path_allowlist(src, dest, paths, export_excludes, run_command):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name, export_excludes, run_command)
        copied += 1
    return copied


def copy_export_path(src, dest, export_excludes, run_command):
    ensure_dir(dest.parent)
    if src.is_dir():
        sync_tree(src, dest, True, export_excludes, run_command)
    else:
        shutil.copy2(src, dest)


def clear_tree(dest, work_dir, run_command):
    ensure_dir(dest)
    empty_dir = work_dir / "empty"
    ensure_dir(empty_dir)
    sync_tree(empty_dir, dest, True, None, run_command)


def clean_homeassistant_export_destination(dest, target, ctx):
    ensure_dir(dest)
    clean_export_destination(dest, ctx.clean_paths, ctx.clean_dir_names, ctx.clean_file_patterns)

    for pattern in ctx.ha_root_patterns:
        for dest_path in sorted(dest.glob(pattern)):
            if not dest_path.is_file() or dest_path.name in ctx.ha_root_excludes:
                continue
            safe_remove_path(dest_path)

    for name in ctx.ha_dirs:
        clear_managed_destination_path(dest / name, ctx.export_excludes, ctx.work_dir, ctx.run_command)

    if target and target.get("include_zigbee2mqtt_legacy"):
        for name in ctx.zigbee2mqtt_paths:
            clear_managed_destination_path(dest / name, ctx.export_excludes, ctx.work_dir, ctx.run_command)

    dest_storage = dest / ".storage"
    for name in [*ctx.storage_allowlist, storage_managed.CORE_CONFIG_ENTRIES_RAW]:
        dest_path = dest_storage / name
        if dest_path.exists() or dest_path.is_symlink():
            safe_remove_path(dest_path)
    clear_managed_destination_path(dest / storage_managed.MANAGED_DIR, ctx.export_excludes, ctx.work_dir, ctx.run_command)
    if target and homeassistant_organizer_enabled(target):
        organizer.clean_organized_root(dest, organizer_options(target))


def export_homeassistant_config(src, dest, target, ctx):
    clean_homeassistant_export_destination(dest, target, ctx)
    copied = 0

    for pattern in ctx.ha_root_patterns:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in ctx.ha_root_excludes:
                continue
            copy_export_path(src_path, dest / src_path.name, ctx.export_excludes, ctx.run_command)
            copied += 1

    for name in ctx.ha_dirs:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name, ctx.export_excludes, ctx.run_command)
        copied += 1

    zigbee2mqtt_count = 0
    if target and target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = copy_homeassistant_path_allowlist(
            src,
            dest,
            ctx.zigbee2mqtt_paths,
            ctx.export_excludes,
            ctx.run_command,
        )
    storage_count = export_storage_allowlist(src, dest, ctx.storage_allowlist)
    managed_storage_count = storage_managed.export_core_config_entries_projection(src, dest)
    return copied, zigbee2mqtt_count, storage_count, managed_storage_count


def organizer_options(target):
    if not target or "organizer" not in target:
        return None
    value = target.get("organizer")
    if value is False:
        return None
    if value is True:
        return {}
    if not isinstance(value, dict):
        return None
    enabled = value.get("enabled", False)
    if not enabled:
        return None
    options = dict(value)
    options.pop("enabled", None)
    return options


def organizer_cleanup_options(target):
    value = target.get("organizer") if target else None
    if isinstance(value, dict):
        options = dict(value)
        options.pop("enabled", None)
        return options
    return organizer_options(target) or {}


def homeassistant_organizer_enabled(target):
    return organizer_options(target) is not None


def ensure_organized_view_is_enabled(src, target):
    if not target or target.get("type") != "homeassistant" or organizer_options(target) is not None:
        return
    options = organizer_cleanup_options(target)
    if organizer.has_organized_view(src, options):
        root_name = organizer.organized_root_name(options)
        raise RuntimeError(
            f"Home Assistant organizer view exists in Git at {root_name}, but the organizer is disabled. "
            "Enable the Home Assistant Git layout toggle or save HA to Git with the toggle off to convert Git back to heap YAML files."
        )


def organize_homeassistant_export(path, target, details, ctx):
    options = organizer_options(target)
    if options is None or not organizer.has_heap_files(path):
        return None
    summary = organizer.split_live_heaps_to_git(path, path, options=options)
    total = sum(summary[kind]["output_count"] for kind in ("automations", "scripts", "scenes"))
    if total and details is not None:
        ctx.add_detail(details, f"Organized {total} Home Assistant automation/script/scene item(s) for Git.")
    return summary


def materialize_homeassistant_source(src, target, ctx):
    options = organizer_options(target)
    ensure_organized_view_is_enabled(src, target)
    if options is None or not organizer.has_organized_view(src, options):
        return Path(src)

    temp = ctx.work_dir / "organizer-materialized" / safe_preview_name(target.get("id") or "homeassistant")
    clear_tree(temp, ctx.work_dir, ctx.run_command)
    sync_tree(Path(src), temp, True, None, ctx.run_command)
    organizer.compose_git_view_to_live(temp, temp, options=options)
    return temp


def managed_ha_heaps_fingerprint(path):
    path = Path(path)
    if not all((path / filename).exists() for filename in organizer.HEAP_FILES.values()):
        return None
    return organizer.fingerprint_heaps(path)


def apply_homeassistant_config(src, dest, target, ctx, details=None):
    src = materialize_homeassistant_source(src, target, ctx)
    if not src.exists() or not has_managed_content(src):
        if details is not None:
            ctx.add_detail(details, f"Skipping {target['id']} because Git has no Home Assistant config yet.")
        return []
    reject_source_symlinks(target, ctx, src)

    copied = 0
    for pattern in ctx.ha_root_patterns:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in ctx.ha_root_excludes:
                continue
            dest_path = dest / src_path.name
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
            copied += 1

    for name in ctx.ha_dirs:
        src_path = src / name
        if not src_path.exists():
            continue
        sync_homeassistant_path_allowlist(src, dest, [name], ctx.export_excludes, ctx.run_command, delete=False)
        copied += 1

    zigbee2mqtt_count = 0
    if target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = sync_homeassistant_path_allowlist(
            src,
            dest,
            ctx.zigbee2mqtt_paths,
            ctx.export_excludes,
            ctx.run_command,
            delete=False,
        )
    copied_count, skipped_protected = sync_storage_allowlist(
        src,
        dest,
        ctx.storage_allowlist,
        ctx.protected_storage_files,
        allow_protected=target_model.allow_protected_storage(target),
    )
    managed_result = storage_managed.apply_core_config_entries_projection(src, dest)
    if copied and details is not None:
        ctx.add_detail(details, f"Applied {copied} Home Assistant config path(s).")
    if zigbee2mqtt_count and details is not None:
        ctx.add_detail(details, f"Applied {zigbee2mqtt_count} Zigbee2MQTT config path(s).")
    if copied_count and details is not None:
        ctx.add_detail(details, f"Applied {copied_count} allowlisted .storage config file(s).")
    if managed_result["updated"] and details is not None:
        ctx.add_detail(details, f"Applied {managed_result['updated']} managed .storage projection update(s).")
    if managed_result.get("missing") and details is not None:
        ctx.add_detail(details, "Skipped managed core.config_entries projection because live .storage/core.config_entries is missing.")
    if skipped_protected and details is not None:
        ctx.add_detail(details, f"Skipped protected .storage file(s): {', '.join(skipped_protected)}.")
    return skipped_protected


def with_protected_storage_allowed(target):
    if target_model.allow_protected_storage(target):
        return target
    updated = dict(target)
    updated["allow_protected_storage"] = True
    return updated


def sync_homeassistant_path_allowlist(src, dest, paths, export_excludes, run_command, delete=True):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        if src_path.is_dir():
            sync_tree(src_path, dest_path, delete, export_excludes, run_command)
        else:
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def clear_managed_destination_path(dest_path, export_excludes, work_dir, run_command):
    if not dest_path.exists() and not dest_path.is_symlink():
        return
    if dest_path.is_dir() and not dest_path.is_symlink():
        empty_dir = work_dir / "empty"
        ensure_dir(empty_dir)
        sync_tree(empty_dir, dest_path, True, export_excludes, run_command)
    else:
        safe_remove_path(dest_path)


def restore_homeassistant_config(src, dest, target, ctx):
    ensure_dir(dest)
    restored_root_names = set()

    for pattern in ctx.ha_root_patterns:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in ctx.ha_root_excludes:
                continue
            restored_root_names.add(src_path.name)
            dest_path = dest / src_path.name
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)

    for pattern in ctx.ha_root_patterns:
        for dest_path in sorted(dest.glob(pattern)):
            if not dest_path.is_file() or dest_path.name in ctx.ha_root_excludes:
                continue
            if dest_path.name not in restored_root_names:
                safe_remove_path(dest_path)

    for name in ctx.ha_dirs:
        src_path = src / name
        dest_path = dest / name
        if src_path.exists():
            sync_homeassistant_path_allowlist(src, dest, [name], ctx.export_excludes, ctx.run_command, delete=True)
        else:
            clear_managed_destination_path(dest_path, ctx.export_excludes, ctx.work_dir, ctx.run_command)

    if target.get("include_zigbee2mqtt_legacy"):
        for name in ctx.zigbee2mqtt_paths:
            src_path = src / name
            dest_path = dest / name
            if src_path.exists():
                sync_homeassistant_path_allowlist(src, dest, [name], ctx.export_excludes, ctx.run_command, delete=True)
            else:
                clear_managed_destination_path(dest_path, ctx.export_excludes, ctx.work_dir, ctx.run_command)

    src_storage = src / ".storage"
    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    for name in ctx.storage_allowlist:
        src_path = src_storage / name
        dest_path = dest_storage / name
        if src_path.exists():
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
        elif dest_path.exists() or dest_path.is_symlink():
            safe_remove_path(dest_path)


def sync_storage_allowlist(src, dest, storage_allowlist, protected_storage_files, allow_protected=False):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0, []

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    skipped_protected = []
    for name in storage_allowlist:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        if name in protected_storage_files and not allow_protected:
            skipped_protected.append(name)
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        write_storage_apply_file(src_path, dest_path)
        copied += 1
    return copied, skipped_protected


def source_has_applicable_storage(path, storage_allowlist, protected_storage_files, allow_protected=False):
    storage = path / ".storage"
    if not storage.exists():
        return False
    for name in storage_allowlist:
        if name in protected_storage_files and not allow_protected:
            continue
        if (storage / name).exists():
            return True
    return False


def file_differs(src_path, dest_path):
    if not dest_path.exists() or not dest_path.is_file():
        return True
    try:
        return src_path.read_bytes() != dest_path.read_bytes()
    except OSError:
        return True


NORMALIZED_STORAGE_FILES = {"core.device_registry", "core.entity_registry"}
DEVICE_REGISTRY_COLLECTION_KEYS = {
    "devices": ("id",),
    "deleted_devices": ("id",),
}
ENTITY_REGISTRY_COLLECTION_KEYS = {
    "entities": ("id", "entity_id", "unique_id"),
    "deleted_entities": ("id", "entity_id", "unique_id"),
}


def stable_json_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compact_json_text(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def stable_collection_key(keys, item):
    if not isinstance(item, dict):
        return (stable_json_key(item),)
    return (*[str(item.get(key) or "") for key in keys], stable_json_key(item))


def registry_collection_identity(keys, item):
    if not isinstance(item, dict):
        return stable_json_key(item)
    values = tuple(str(item.get(key) or "") for key in keys)
    if any(values):
        return values
    return stable_json_key(item)


def sort_json_list(value):
    if isinstance(value, list):
        value.sort(key=stable_json_key)


def normalize_device_registry_item(item):
    if not isinstance(item, dict):
        return
    item.pop("modified_at", None)
    sort_json_list(item.get("connections"))
    subentries = item.get("config_entries_subentries")
    if isinstance(subentries, dict):
        for values in subentries.values():
            sort_json_list(values)


def normalize_entity_registry_item(item):
    if not isinstance(item, dict):
        return
    item.pop("modified_at", None)
    item.pop("suggested_object_id", None)
    if item.get("platform") == "mobile_app":
        item.pop("original_icon", None)


def normalize_registry_collection(data, name, keys, item_normalizer):
    collection = data.get("data", {}).get(name)
    if not isinstance(collection, list):
        return
    for item in collection:
        item_normalizer(item)
    collection.sort(key=lambda item: stable_collection_key(keys, item))


def normalized_storage_data(name, data):
    if name == "core.device_registry":
        for collection, keys in DEVICE_REGISTRY_COLLECTION_KEYS.items():
            normalize_registry_collection(data, collection, keys, normalize_device_registry_item)
    elif name == "core.entity_registry":
        for collection, keys in ENTITY_REGISTRY_COLLECTION_KEYS.items():
            normalize_registry_collection(data, collection, keys, normalize_entity_registry_item)
    return data


def normalized_storage_bytes_from_text(name, text):
    if name not in NORMALIZED_STORAGE_FILES:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    data = normalized_storage_data(name, data)
    return stable_json_key(data).encode()


def normalized_storage_pretty_text_from_text(name, text):
    if name not in NORMALIZED_STORAGE_FILES:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    data = normalized_storage_data(name, data)
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def registry_collection_keys(name):
    if name == "core.device_registry":
        return DEVICE_REGISTRY_COLLECTION_KEYS
    if name == "core.entity_registry":
        return ENTITY_REGISTRY_COLLECTION_KEYS
    return {}


def registry_item_normalizer(name):
    if name == "core.device_registry":
        return normalize_device_registry_item
    if name == "core.entity_registry":
        return normalize_entity_registry_item
    return lambda _item: None


def normalized_registry_item(name, item):
    item = deepcopy(item)
    registry_item_normalizer(name)(item)
    return item


def registry_item_signature(name, item):
    return stable_json_key(normalized_registry_item(name, item))


def restore_field_from_head(merged, head_item, field):
    if field in head_item:
        merged[field] = deepcopy(head_item[field])
    else:
        merged.pop(field, None)


def restore_device_order_hidden_fields(merged, head_item, current_item):
    head_connections = head_item.get("connections")
    current_connections = current_item.get("connections")
    if isinstance(head_connections, list) and isinstance(current_connections, list):
        if sorted(head_connections, key=stable_json_key) == sorted(current_connections, key=stable_json_key):
            merged["connections"] = deepcopy(head_connections)

    head_subentries = head_item.get("config_entries_subentries")
    current_subentries = current_item.get("config_entries_subentries")
    if isinstance(head_subentries, dict) and isinstance(current_subentries, dict):
        restored = deepcopy(current_subentries)
        for key, head_values in head_subentries.items():
            current_values = current_subentries.get(key)
            if isinstance(head_values, list) and isinstance(current_values, list):
                if sorted(head_values, key=stable_json_key) == sorted(current_values, key=stable_json_key):
                    restored[key] = deepcopy(head_values)
        merged["config_entries_subentries"] = restored


def merge_registry_item_for_commit(name, head_item, current_item):
    merged = deepcopy(current_item)
    restore_field_from_head(merged, head_item, "modified_at")
    if name == "core.device_registry":
        restore_device_order_hidden_fields(merged, head_item, current_item)
    elif name == "core.entity_registry":
        restore_field_from_head(merged, head_item, "suggested_object_id")
        if current_item.get("platform") == "mobile_app" or head_item.get("platform") == "mobile_app":
            restore_field_from_head(merged, head_item, "original_icon")
    return merged


def merge_normalized_storage_for_commit(name, head_data, current_data):
    collections = registry_collection_keys(name)
    if not collections:
        return normalized_storage_data(name, deepcopy(current_data))
    data = deepcopy(head_data)
    for key, value in current_data.items():
        if key != "data":
            data[key] = deepcopy(value)
    if not isinstance(data.get("data"), dict):
        data["data"] = {}
    head_collections = head_data.get("data", {}) if isinstance(head_data.get("data"), dict) else {}
    current_collections = current_data.get("data", {}) if isinstance(current_data.get("data"), dict) else {}
    target_collections = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
    for key, value in current_collections.items():
        if key not in collections:
            target_collections[key] = deepcopy(value)
    for collection, keys in collections.items():
        head_items = head_collections.get(collection)
        current_items = current_collections.get(collection)
        if not isinstance(head_items, list) or not isinstance(current_items, list):
            normalize_registry_collection(data, collection, keys, registry_item_normalizer(name))
            continue
        current_by_key = {registry_collection_identity(keys, item): item for item in current_items}
        kept = set()
        merged = []
        for head_item in head_items:
            key = registry_collection_identity(keys, head_item)
            current_item = current_by_key.get(key)
            if current_item is None:
                continue
            kept.add(key)
            if registry_item_signature(name, head_item) == registry_item_signature(name, current_item):
                merged.append(deepcopy(head_item))
            else:
                merged.append(merge_registry_item_for_commit(name, head_item, current_item))
        for current_item in current_items:
            key = registry_collection_identity(keys, current_item)
            if key in kept:
                continue
            merged.append(deepcopy(current_item))
        target_collections[collection] = merged
    return data


def render_registry_commit_json(value, compact_collection_names, indent=0, key_name=None):
    space = " " * indent
    next_space = " " * (indent + 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            rendered = render_registry_commit_json(item, compact_collection_names, indent + 2, key)
            comma = "," if index < len(items) - 1 else ""
            key_text = json.dumps(key, ensure_ascii=False)
            rendered_lines = rendered.splitlines()
            if len(rendered_lines) == 1:
                lines.append(f"{next_space}{key_text}: {rendered_lines[0]}{comma}")
            else:
                lines.append(f"{next_space}{key_text}: {rendered_lines[0]}")
                lines.extend(rendered_lines[1:-1])
                lines.append(f"{rendered_lines[-1]}{comma}")
        lines.append(f"{space}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for index, item in enumerate(value):
            comma = "," if index < len(value) - 1 else ""
            if key_name in compact_collection_names:
                lines.append(f"{next_space}{compact_json_text(item)}{comma}")
            else:
                rendered = render_registry_commit_json(item, compact_collection_names, indent + 2)
                rendered_lines = rendered.splitlines()
                lines.append(f"{next_space}{rendered_lines[0]}")
                lines.extend(rendered_lines[1:-1])
                lines.append(f"{rendered_lines[-1]}{comma}")
        lines.append(f"{space}]")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False)


def normalized_storage_commit_text_from_text(name, head_text, current_text):
    if name not in NORMALIZED_STORAGE_FILES:
        return None
    try:
        head_data = json.loads(head_text)
        current_data = json.loads(current_text)
    except json.JSONDecodeError:
        return None
    data = merge_normalized_storage_for_commit(name, head_data, current_data)
    return render_registry_commit_json(data, set(registry_collection_keys(name))) + "\n"


def merged_normalized_storage_apply_text(name, live_text, git_text):
    if name not in NORMALIZED_STORAGE_FILES:
        return None
    try:
        live_data = json.loads(live_text)
        git_data = json.loads(git_text)
    except json.JSONDecodeError:
        return None
    data = merge_normalized_storage_for_commit(name, live_data, git_data)
    return render_registry_commit_json(data, set(registry_collection_keys(name))) + "\n"


def write_storage_apply_file(src_path, dest_path):
    src_path = Path(src_path)
    dest_path = Path(dest_path)
    if src_path.name in NORMALIZED_STORAGE_FILES and dest_path.exists() and dest_path.is_file():
        try:
            merged = merged_normalized_storage_apply_text(src_path.name, dest_path.read_text(), src_path.read_text())
        except (OSError, UnicodeDecodeError):
            merged = None
        if merged is not None:
            dest_path.write_text(merged)
            return
    shutil.copy2(src_path, dest_path)


def normalized_storage_text_from_path(path):
    path = Path(path)
    try:
        normalized = normalized_storage_pretty_text_from_text(path.name, path.read_text())
    except (OSError, UnicodeDecodeError):
        return None
    return normalized


def write_normalized_storage_file(src_path, dest_path):
    text = normalized_storage_text_from_path(src_path)
    if text is None:
        return False
    dest_path = Path(dest_path)
    ensure_dir(dest_path.parent)
    dest_path.write_text(text)
    return True


def normalize_storage_file_pair_for_diff(left_path, right_path, dest_root):
    left_path = Path(left_path)
    right_path = Path(right_path)
    if left_path.name not in NORMALIZED_STORAGE_FILES or right_path.name != left_path.name:
        return None
    left_dest = Path(dest_root) / "left" / left_path.name
    right_dest = Path(dest_root) / "right" / right_path.name
    if not write_normalized_storage_file(left_path, left_dest):
        return None
    if not write_normalized_storage_file(right_path, right_dest):
        return None
    return left_dest, right_dest


def normalized_storage_bytes(path):
    path = Path(path)
    try:
        return normalized_storage_bytes_from_text(path.name, path.read_text())
    except (OSError, UnicodeDecodeError):
        return None


def save_file_differs(src_path, dest_path):
    if not dest_path.exists() or not dest_path.is_file():
        return True
    src_normalized = normalized_storage_bytes(src_path)
    dest_normalized = normalized_storage_bytes(dest_path)
    if src_normalized is not None and dest_normalized is not None:
        return src_normalized != dest_normalized
    return file_differs(src_path, dest_path)


def is_excluded_path(path, root, patterns):
    relative = str(path.relative_to(root))
    return any(clean_path_matches(path, relative, pattern) for pattern in patterns)


def collect_symlinks_under(path, root, excludes=None):
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        return [path.relative_to(root).as_posix()]
    if not path.is_dir():
        return []

    found = []
    for item in path.rglob("*"):
        if not item.is_symlink():
            continue
        if excludes and is_excluded_path(item, root, excludes):
            continue
        found.append(item.relative_to(root).as_posix())
    return found


def homeassistant_source_symlinks(src, target, ctx):
    found = []

    for pattern in ctx.ha_root_patterns:
        for src_path in sorted(src.glob(pattern)):
            if src_path.name in ctx.ha_root_excludes:
                continue
            if src_path.is_symlink():
                found.append(src_path.relative_to(src).as_posix())

    managed_dirs = list(ctx.ha_dirs)
    if target.get("include_zigbee2mqtt_legacy"):
        managed_dirs.extend(ctx.zigbee2mqtt_paths)
    for name in managed_dirs:
        found.extend(collect_symlinks_under(src / name, src, ctx.export_excludes))

    options = organizer_options(target)
    if options is not None:
        found.extend(collect_symlinks_under(organizer.organized_root(src, options), src, ctx.export_excludes))

    src_storage = src / ".storage"
    for name in ctx.storage_allowlist:
        src_path = src_storage / name
        if src_path.is_symlink():
            found.append(src_path.relative_to(src).as_posix())

    return sorted(set(found))


def target_source_symlinks(target, ctx, source_path=None):
    source_path = Path(source_path or target["source_path"])
    if not source_path.exists() and not source_path.is_symlink():
        return []
    if source_path.is_symlink():
        return ["."]
    if target.get("type", "homeassistant") == "homeassistant":
        return homeassistant_source_symlinks(source_path, target, ctx)
    return sorted(set(collect_symlinks_under(source_path, source_path, ctx.export_excludes)))


def reject_source_symlinks(target, ctx, source_path=None):
    symlinks = target_source_symlinks(target, ctx, source_path)
    if symlinks:
        preview = ", ".join(symlinks[:10])
        if len(symlinks) > 10:
            preview += f", and {len(symlinks) - 10} more"
        raise RuntimeError(f"Git source for target '{target['id']}' contains symlink(s), refusing to apply: {preview}")


def source_tree_has_overlay_changes(src, dest, excludes):
    if src.is_file():
        return file_differs(src, dest)
    if not src.exists():
        return False
    for src_path in src.rglob("*"):
        if not src_path.is_file() or is_excluded_path(src_path, src, excludes):
            continue
        relative = src_path.relative_to(src)
        if file_differs(src_path, dest / relative):
            return True
    return False


def destination_tree_has_managed_extra(src, dest, excludes):
    if not dest.exists():
        return False
    if dest.is_file():
        return not src.exists()
    for dest_path in dest.rglob("*"):
        if not dest_path.is_file() or is_excluded_path(dest_path, dest, excludes):
            continue
        relative = dest_path.relative_to(dest)
        if not (src / relative).exists():
            return True
    return False


def homeassistant_change_set(src, dest, target, ctx, mode="apply"):
    src = materialize_homeassistant_source(src, target, ctx)
    changes = ChangeSet()
    if not src.exists() or not has_managed_content(src):
        return changes

    restored_root_names = set()
    for pattern in ctx.ha_root_patterns:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in ctx.ha_root_excludes:
                continue
            restored_root_names.add(src_path.name)
            if file_differs(src_path, dest / src_path.name):
                changes.changed_yaml = True

    if mode == "rollback":
        for pattern in ctx.ha_root_patterns:
            for dest_path in sorted(dest.glob(pattern)):
                if not dest_path.is_file() or dest_path.name in ctx.ha_root_excludes:
                    continue
                if dest_path.name not in restored_root_names:
                    changes.changed_yaml = True

    managed_dirs = list(ctx.ha_dirs)
    if target.get("include_zigbee2mqtt_legacy"):
        managed_dirs.extend(ctx.zigbee2mqtt_paths)

    for name in managed_dirs:
        src_path = src / name
        dest_path = dest / name
        if source_tree_has_overlay_changes(src_path, dest_path, ctx.export_excludes):
            changes.changed_yaml = True
        if mode == "rollback" and destination_tree_has_managed_extra(src_path, dest_path, ctx.export_excludes):
            changes.changed_yaml = True

    src_storage = src / ".storage"
    dest_storage = dest / ".storage"
    allow_protected = mode == "rollback" or target_model.allow_protected_storage(target)
    for name in ctx.storage_allowlist:
        is_protected = name in ctx.protected_storage_files
        if is_protected and not allow_protected:
            continue
        src_path = src_storage / name
        dest_path = dest_storage / name
        changed = False
        if src_path.exists():
            changed = file_differs(src_path, dest_path)
        elif mode == "rollback" and (dest_path.exists() or dest_path.is_symlink()):
            changed = True
        if changed:
            changes.changed_storage = True
            if is_protected:
                changes.changed_protected_storage = True

    if storage_managed.core_config_entries_projection_would_update(src, dest):
        changes.changed_storage = True
        changes.changed_protected_storage = True

    return changes


def apply_targets(resolved_targets, details, ctx):
    homeassistant_target = None
    core_stopped = False
    homeassistant_changes = ChangeSet()

    for target in resolved_targets:
        source_path = Path(target["source_path"])
        live_path = Path(target["live_path"])
        addon_was_started = False

        if source_path.exists() and has_managed_content(source_path):
            reject_source_symlinks(target, ctx)

        if target["type"] == "homeassistant":
            homeassistant_target = target
            homeassistant_changes = homeassistant_change_set(source_path, live_path, target, ctx, mode="apply")
            if (
                homeassistant_changes.changed_storage
                and target_model.stop_core_before_storage_apply(target)
                and not core_stopped
            ):
                ctx.add_detail(details, "Stopping Home Assistant Core before syncing .storage.")
                ctx.core_stop()
                core_stopped = True
            elif homeassistant_changes.changed_storage:
                ctx.add_detail(details, "Warning: .storage will be written while Home Assistant Core is running.")
        elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
            slug = target["resolved_slug"]
            ctx.add_detail(details, f"Stopping add-on {slug} before sync.")
            addon_was_started = ctx.stop_addon_for_sync(slug)

        ctx.add_detail(details, f"Syncing {target['id']} from {source_path} to {live_path}.")
        if target["type"] == "homeassistant":
            apply_homeassistant_config(source_path, live_path, target, ctx, details)
        else:
            if not source_path.exists() or not has_managed_content(source_path):
                ctx.add_detail(details, f"Skipping {target['id']} because Git has no config for this add-on yet.")
                continue
            sync_tree(source_path, live_path, target_model.apply_delete(target), ctx.export_excludes, ctx.run_command)

        if target["type"] == "addon" and target.get("restart_after_sync", True):
            slug = target["resolved_slug"]
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    ctx.add_detail(details, f"Starting add-on {slug} after sync.")
                    ctx.addon_action(slug, "start")
            else:
                ctx.add_detail(details, f"Restarting add-on {slug}.")
                ctx.restart_or_start_addon(slug)

    if homeassistant_target is None:
        return {"core_stopped": core_stopped}
    if not homeassistant_changes.any():
        return {"core_stopped": core_stopped}

    ctx.add_detail(details, "Running Home Assistant config check.")
    try:
        ctx.do_core_check()
    except Exception as exc:
        setattr(exc, "core_stopped", core_stopped)
        raise

    if core_stopped:
        if target_model.start_core_after_storage_apply(homeassistant_target):
            ctx.add_detail(details, "Starting Home Assistant Core after sync.")
            try:
                ctx.core_start()
            except Exception as exc:
                setattr(exc, "core_stopped", True)
                raise
        else:
            ctx.add_detail(details, "Home Assistant Core was left stopped after .storage sync by policy.")
    else:
        if target_model.restart_core_after_apply(homeassistant_target):
            ctx.add_detail(details, "Restarting Home Assistant Core.")
            ctx.core_restart()
        elif homeassistant_changes.changed_yaml and target_model.reload_yaml_after_apply(homeassistant_target):
            ctx.add_detail(details, "Reloading Home Assistant YAML config.")
            ctx.core_reload_yaml()
    return {"core_stopped": False}


def build_save_export(resolved_targets, details, ctx):
    export_root = ctx.work_dir / "save-export"
    clear_tree(export_root, ctx.work_dir, ctx.run_command)

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        if not live_path.exists():
            if target.get("optional", False):
                ctx.add_detail(details, f"Skipping optional target {target['id']} because {live_path} does not exist.")
                continue
            raise RuntimeError(f"Live path does not exist for target '{target['id']}': {live_path}")

        export_path = export_root / target["id"]
        if target["type"] == "homeassistant":
            ctx.add_detail(details, f"Exporting config-only {target['id']} from {live_path} to a temporary tree.")
            copied_count, zigbee2mqtt_count, storage_count, managed_storage_count = export_homeassistant_config(
                live_path, export_path, target, ctx
            )
            organize_homeassistant_export(export_path, target, details, ctx)
            ctx.add_detail(details, f"Exported {copied_count} Home Assistant config path(s).")
            if zigbee2mqtt_count:
                ctx.add_detail(details, f"Exported {zigbee2mqtt_count} legacy Zigbee2MQTT config path(s).")
            if storage_count:
                ctx.add_detail(details, f"Exported {storage_count} allowlisted .storage config file(s).")
            if managed_storage_count:
                ctx.add_detail(details, f"Exported {managed_storage_count} managed .storage projection(s).")
        else:
            ctx.add_detail(details, f"Exporting {target['id']} from {live_path} to a temporary tree.")
            export_target_to_path(target, export_path, ctx)
        add_save_export_candidate_details(target, export_path, details, ctx)

    return export_root


def apply_save_export(resolved_targets, export_root, details, ctx):
    for target in resolved_targets:
        export_path = Path(export_root) / target["id"]
        if not export_path.exists():
            continue

        source_path = Path(target["source_path"])
        if target["type"] == "homeassistant":
            ctx.add_detail(details, f"Saving config-only {target['id']} to {source_path}.")
            clean_homeassistant_export_destination(source_path, target, ctx)
            organizer.clean_organized_root(source_path, organizer_cleanup_options(target))
            sync_tree(export_path, source_path, False, None, ctx.run_command)
        else:
            ctx.add_detail(details, f"Saving {target['id']} to {source_path}.")
            removed_count = clean_export_destination(
                source_path,
                ctx.clean_paths,
                ctx.clean_dir_names,
                ctx.clean_file_patterns,
            )
            if removed_count:
                ctx.add_detail(details, f"Removed {removed_count} excluded item(s) from {target['id']} save destination.")
            sync_tree(export_path, source_path, target_model.save_delete(target), None, ctx.run_command)


def export_targets(resolved_targets, details, ctx):
    export_root = build_save_export(resolved_targets, details, ctx)
    apply_save_export(resolved_targets, export_root, details, ctx)


def repo_relative_target(target, repo_dir, preview_repo):
    updated = dict(target)
    try:
        relative = Path(target["source_path"]).relative_to(repo_dir)
    except ValueError:
        relative = Path(target.get("source") or target["id"])
    updated["source_path"] = str(Path(preview_repo) / relative)
    return updated


def normalized_save_registry_paths(resolved_targets, repo_dir):
    paths = []
    repo_dir = Path(repo_dir)
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        try:
            source_relative = Path(target["source_path"]).relative_to(repo_dir)
        except ValueError:
            source_relative = Path(target.get("source") or Path(target["source_path"]).name)
        for name in sorted(NORMALIZED_STORAGE_FILES):
            paths.append(source_relative / ".storage" / name)
    return paths


def restore_normalized_equal_save_files(repo_dir, dest_root, resolved_targets, details, ctx):
    restored = []
    repo_dir = Path(repo_dir)
    dest_root = Path(dest_root)
    for relative in normalized_save_registry_paths(resolved_targets, repo_dir):
        source_file = repo_dir / relative
        dest_file = dest_root / relative
        if not source_file.exists() or not dest_file.exists():
            continue
        if file_differs(dest_file, source_file) and not save_file_differs(dest_file, source_file):
            shutil.copy2(source_file, dest_file)
            restored.append(relative.as_posix())
    if restored:
        ctx.add_detail(details, f"Ignored {len(restored)} registry noise-only Save change(s).")
    return restored


def git_head_storage_text(repo_dir, relative, ctx):
    result = ctx.run_command(["git", "show", f"HEAD:{relative.as_posix()}"], cwd=repo_dir)
    if result.returncode != 0:
        return None
    return result.stdout


def restore_normalized_equal_save_worktree(repo_dir, resolved_targets, details, ctx):
    restored = []
    repo_dir = Path(repo_dir)
    for relative in normalized_save_registry_paths(resolved_targets, repo_dir):
        path = repo_dir / relative
        if not path.exists():
            continue
        head_text = git_head_storage_text(repo_dir, relative, ctx)
        if head_text is None:
            continue
        head_bytes = normalized_storage_bytes_from_text(relative.name, head_text)
        worktree_bytes = normalized_storage_bytes(path)
        if head_bytes is None or worktree_bytes is None or head_bytes != worktree_bytes:
            continue
        try:
            if path.read_text() == head_text:
                continue
        except (OSError, UnicodeDecodeError):
            continue
        checkout = ctx.run_command(["git", "checkout", "--", relative.as_posix()], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout normalized Save file failed:\n{checkout.stderr.strip()}")
        restored.append(relative.as_posix())
    if restored:
        ctx.add_detail(details, f"Ignored {len(restored)} registry noise-only Save change(s).")
    return restored


def normalize_changed_save_registry_worktree(repo_dir, resolved_targets, details, ctx):
    normalized = []
    repo_dir = Path(repo_dir)
    for relative in normalized_save_registry_paths(resolved_targets, repo_dir):
        path = repo_dir / relative
        if not path.exists():
            continue
        head_text = git_head_storage_text(repo_dir, relative, ctx)
        if head_text is None:
            continue
        try:
            path_text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        current_text = normalized_storage_commit_text_from_text(relative.name, head_text, path_text)
        head_bytes = normalized_storage_bytes_from_text(relative.name, head_text)
        current_bytes = normalized_storage_bytes_from_text(relative.name, path_text)
        if current_text is None or head_bytes is None or current_bytes is None or head_bytes == current_bytes:
            continue
        if path_text != current_text:
            path.write_text(current_text)
            normalized.append(relative.as_posix())
    if normalized:
        ctx.add_detail(details, f"Normalized {len(normalized)} changed registry file(s) for Git.")
    return normalized


def save_preview_status_lines(repo_dir, preview_repo, include_redundant_data=False):
    repo_dir = Path(repo_dir)
    preview_repo = Path(preview_repo)

    def files(root):
        found = {}
        if not root.exists():
            return found
        for path in root.rglob("*"):
            if ".git" in path.relative_to(root).parts or not path.is_file():
                continue
            found[path.relative_to(root).as_posix()] = path
        return found

    before = files(repo_dir)
    after = files(preview_repo)
    lines = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            lines.append(f"- Added: {path}")
        elif path not in after:
            lines.append(f"- Deleted: {path}")
        elif (file_differs(after[path], before[path]) if include_redundant_data else save_file_differs(after[path], before[path])):
            lines.append(f"- Modified: {path}")
    return lines


def save_preview_diff(repo_dir, preview_repo, run_command):
    result = run_command(["diff", "-ruN", "-x", ".git", str(repo_dir), str(preview_repo)])
    if result.returncode == 0:
        return ""
    if result.returncode == 1:
        return result.stdout.strip()
    raise RuntimeError(f"Save preview diff failed:\n{result.stderr.strip() or result.stdout.strip()}")


def normalized_storage_paths_under(root):
    root = Path(root)
    return [Path(".storage") / name for name in sorted(NORMALIZED_STORAGE_FILES) if (root / ".storage" / name).exists()]


def normalize_save_preview_diff_files(repo_copy, preview_copy, registry_paths):
    for relative in registry_paths:
        repo_file = repo_copy / relative
        preview_file = preview_copy / relative
        if not repo_file.exists() or not preview_file.exists():
            continue
        write_normalized_storage_file(repo_file, repo_file)
        write_normalized_storage_file(preview_file, preview_file)


def normalize_organizer_apply_diff_files(baseline_copy, preview_copy, target):
    options = organizer_options(target)
    if options is None:
        return False
    if not organizer.has_heap_files(baseline_copy) and not organizer.has_heap_files(preview_copy):
        return False
    organizer.split_live_heaps_to_git(baseline_copy, baseline_copy, options=options)
    organizer.split_live_heaps_to_git(preview_copy, preview_copy, options=options)
    normalize_organizer_index_for_diff(baseline_copy, options)
    normalize_organizer_index_for_diff(preview_copy, options)
    return True


def normalize_organizer_index_for_diff(root, options):
    index_path = organizer.organized_root(root, options) / organizer.INDEX_NAME
    try:
        data = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    changed = False
    for kind in ("automations", "scripts", "scenes"):
        ids = data.get(kind, {}).get("ids")
        if isinstance(ids, list):
            sorted_ids = sorted(ids, key=str)
            if ids != sorted_ids:
                data[kind]["ids"] = sorted_ids
                changed = True
    if changed:
        index_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return changed


def save_preview_diff_normalized(repo_dir, preview_repo, resolved_targets, ctx):
    registry_paths = normalized_save_registry_paths(resolved_targets, repo_dir)
    if not registry_paths:
        return save_preview_diff(repo_dir, preview_repo, ctx.run_command)

    diff_root = ctx.work_dir / "save-preview-diff"
    repo_copy = diff_root / "repo"
    preview_copy = diff_root / "preview"
    clear_tree(diff_root, ctx.work_dir, ctx.run_command)
    sync_tree(repo_dir, repo_copy, True, [".git/"], ctx.run_command)
    sync_tree(preview_repo, preview_copy, True, [".git/"], ctx.run_command)
    normalize_save_preview_diff_files(repo_copy, preview_copy, registry_paths)
    return save_preview_diff(repo_copy, preview_copy, ctx.run_command)


def build_save_preview(resolved_targets, repo_dir, details, ctx, include_redundant_data=False):
    export_root = build_save_export(resolved_targets, details, ctx)
    preview_repo = ctx.work_dir / "save-to-git-preview"
    clear_tree(preview_repo, ctx.work_dir, ctx.run_command)
    sync_tree(repo_dir, preview_repo, True, [".git/"], ctx.run_command)

    preview_targets = [repo_relative_target(target, repo_dir, preview_repo) for target in resolved_targets]
    apply_save_export(preview_targets, export_root, details, ctx)
    if not include_redundant_data:
        restore_normalized_equal_save_files(repo_dir, preview_repo, resolved_targets, details, ctx)

    status_lines = save_preview_status_lines(repo_dir, preview_repo, include_redundant_data)
    summary = "\n".join([f"Save preview changes ({len(status_lines)}):", *status_lines]) if status_lines else "No Save changes."
    if not status_lines:
        diff = ""
    elif include_redundant_data:
        diff = save_preview_diff(repo_dir, preview_repo, ctx.run_command)
    else:
        diff = save_preview_diff_normalized(repo_dir, preview_repo, resolved_targets, ctx)
    return {"summary": summary, "diff": diff}


def export_target_to_path(target, dest, ctx):
    live_path = Path(target["live_path"])
    if target["type"] == "homeassistant":
        export_homeassistant_config(live_path, dest, target, ctx)
        organize_homeassistant_export(dest, target, None, ctx)
        return
    clean_export_destination(
        dest,
        ctx.clean_paths,
        ctx.clean_dir_names,
        ctx.clean_file_patterns,
    )
    export_tree(live_path, dest, target_model.save_delete(target), ctx.export_excludes, ctx.run_command)


def save_compare_differs(exported_path, source_file, include_redundant_data):
    if include_redundant_data:
        return file_differs(exported_path, source_file)
    return save_file_differs(exported_path, source_file)


def save_unknown_base_conflicts(resolved_targets, repo_dir, resolutions, details, ctx, include_redundant_data=False):
    preview_root = ctx.work_dir / "save-preview"
    clear_tree(preview_root, ctx.work_dir, ctx.run_command)
    repo_dir = Path(repo_dir)
    conflicts = []

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        source_path = Path(target["source_path"])
        if not live_path.exists():
            if target.get("optional", False):
                continue
            raise RuntimeError(f"Live path does not exist for target '{target['id']}': {live_path}")
        if not has_managed_content(source_path):
            continue

        preview_path = preview_root / target["id"]
        export_target_to_path(target, preview_path, ctx)
        if not preview_path.exists():
            continue
        add_save_export_candidate_details(target, preview_path, details, ctx)

        for exported_path in sorted(preview_path.rglob("*")):
            if not exported_path.is_file():
                continue
            relative = exported_path.relative_to(preview_path)
            source_file = source_path / relative
            if not source_file.exists() or not source_file.is_file():
                continue
            repo_relative = source_file.relative_to(repo_dir).as_posix()
            if resolutions.get(repo_relative) in {"ha", "git"}:
                continue
            if save_compare_differs(exported_path, source_file, include_redundant_data):
                conflicts.append(repo_relative)

        conflicts.extend(
            save_deleted_source_conflicts(
                target,
                live_path,
                source_path,
                preview_path,
                repo_dir,
                resolutions,
                ctx,
                include_redundant_data,
            )
        )

    if conflicts:
        ctx.add_detail(details, f"Found {len(conflicts)} unknown-base Save conflict(s).")
    return sorted(set(conflicts))


def save_deleted_source_conflicts(target, live_path, source_path, preview_path, repo_dir, resolutions, ctx, include_redundant_data=False):
    conflicts = []
    for relative in managed_save_source_files(target, source_path, ctx):
        if (preview_path / relative).exists():
            continue
        source_file = source_path / relative
        repo_relative = source_file.relative_to(repo_dir).as_posix()
        if resolutions.get(repo_relative) in {"ha", "git"}:
            continue
        live_file = live_path / relative
        if not live_file.exists() or save_compare_differs(live_file, source_file, include_redundant_data):
            conflicts.append(repo_relative)
    return conflicts


def managed_save_source_files(target, source_path, ctx):
    source_path = Path(source_path)
    if target.get("type") == "homeassistant":
        return homeassistant_managed_save_source_files(target, source_path, ctx)
    if target_model.save_delete(target):
        return source_tree_files(source_path, ctx.export_excludes)
    return []


def source_tree_files(root, excludes=None):
    root = Path(root)
    if not root.exists():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (excludes and is_excluded_path(path, root, excludes)):
            continue
        files.append(path.relative_to(root))
    return files


def add_managed_files_under(files, root, relative_root, excludes=None):
    path = root / relative_root
    if not path.exists():
        return
    if path.is_file():
        files.add(relative_root)
        return
    for relative in source_tree_files(path, excludes):
        files.add(relative_root / relative)


def homeassistant_managed_save_source_files(target, source_path, ctx):
    files = set()

    for pattern in ctx.ha_root_patterns:
        for path in sorted(source_path.glob(pattern)):
            if path.is_file() and path.name not in ctx.ha_root_excludes:
                files.add(path.relative_to(source_path))

    for name in ctx.ha_dirs:
        add_managed_files_under(files, source_path, Path(name), ctx.export_excludes)

    if target and target.get("include_zigbee2mqtt_legacy"):
        for name in ctx.zigbee2mqtt_paths:
            add_managed_files_under(files, source_path, Path(name), ctx.export_excludes)

    for name in ctx.storage_allowlist:
        path = Path(".storage") / name
        if (source_path / path).is_file():
            files.add(path)

    add_managed_files_under(files, source_path, Path(storage_managed.MANAGED_DIR), ctx.export_excludes)

    if target and homeassistant_organizer_enabled(target):
        add_managed_files_under(files, source_path, Path(organizer.organized_root_name(organizer_options(target))), ctx.export_excludes)

    return sorted(files)


def save_export_candidate_paths(target, export_path):
    export_path = Path(export_path)
    source_prefix = Path(target.get("source") or Path(target["source_path"]).name)
    if not export_path.exists():
        return []

    paths = []
    for exported_path in sorted(export_path.rglob("*")):
        if not exported_path.is_file():
            continue
        relative = exported_path.relative_to(export_path)
        paths.append((source_prefix / relative).as_posix())
    return paths


def add_save_export_candidate_details(target, export_path, details, ctx):
    paths = save_export_candidate_paths(target, export_path)
    if not paths:
        return
    ctx.add_detail(details, "\n".join([f"Save export candidates for {target['id']} ({len(paths)}):", *[f"- {path}" for path in paths]]))


def restore_save_git_resolutions(repo_dir, resolutions, details, ctx):
    git_paths = sorted(path for path, choice in resolutions.items() if choice == "git")
    if not git_paths:
        return 0

    for path in git_paths:
        safe_path = Path(path)
        if safe_path.is_absolute() or ".." in safe_path.parts:
            raise RuntimeError("Invalid Save conflict path")

    result = ctx.run_command(["git", "checkout", "--"] + git_paths, cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout Save conflict resolution failed:\n{result.stderr.strip()}")
    ctx.add_detail(details, f"Kept {len(git_paths)} Git version file(s) selected during Save conflict resolution.")
    return len(git_paths)


def build_preview_baseline(target, baseline_path, ctx):
    live_path = Path(target["live_path"])
    clear_tree(baseline_path, ctx.work_dir, ctx.run_command)
    if not live_path.exists():
        return
    if target["type"] == "homeassistant":
        ctx.log(f"Preview {target['id']}: exporting live Home Assistant config from {live_path}")
        export_homeassistant_config(live_path, baseline_path, target, ctx)
    else:
        ctx.log(f"Preview {target['id']}: exporting live add-on config from {live_path}")
        export_target_to_path(target, baseline_path, ctx)


def sync_to_preview(target, baseline_path, preview_path, ctx):
    source_path = Path(target["source_path"])
    clear_tree(preview_path, ctx.work_dir, ctx.run_command)
    sync_tree(baseline_path, preview_path, True, None, ctx.run_command)

    if target["type"] == "homeassistant":
        if source_path.exists() and has_managed_content(source_path):
            reject_source_symlinks(target, ctx)
            ctx.log(f"Preview {target['id']}: applying Git Home Assistant config from {source_path}")
            skipped_protected = apply_homeassistant_config(
                source_path,
                preview_path,
                with_protected_storage_allowed(target),
                ctx,
            )
        else:
            skipped_protected = []
    else:
        if source_path.exists() and has_managed_content(source_path):
            reject_source_symlinks(target, ctx)
            ctx.log(f"Preview {target['id']}: applying Git add-on config from {source_path}")
            sync_tree(source_path, preview_path, target_model.apply_delete(target), ctx.export_excludes, ctx.run_command)
        skipped_protected = []
    return skipped_protected


def target_diff(target, baseline_path, preview_path, run_command):
    result = run_command(["diff", "-ruN", "-x", ".git", str(baseline_path), str(preview_path)])
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Diff failed for {target['id']}:\n{result.stderr.strip()}")
    if not result.stdout.strip():
        return f"Target {target['id']}: no file changes.\n"
    return f"## {target['id']}\n{result.stdout.strip()}\n"


def target_diff_normalized(target, baseline_path, preview_path, ctx):
    if target["type"] != "homeassistant":
        return target_diff(target, baseline_path, preview_path, ctx.run_command)

    registry_paths = sorted(set(normalized_storage_paths_under(baseline_path)) | set(normalized_storage_paths_under(preview_path)))
    normalize_organizer = organizer_options(target) is not None
    if not registry_paths and not normalize_organizer:
        return target_diff(target, baseline_path, preview_path, ctx.run_command)

    diff_root = ctx.work_dir / "apply-preview-diff" / safe_preview_name(str(target["id"]))
    baseline_copy = diff_root / "baseline"
    preview_copy = diff_root / "preview"
    clear_tree(diff_root, ctx.work_dir, ctx.run_command)
    sync_tree(baseline_path, baseline_copy, True, [".git/"], ctx.run_command)
    sync_tree(preview_path, preview_copy, True, [".git/"], ctx.run_command)
    normalize_organizer_apply_diff_files(baseline_copy, preview_copy, target)
    normalize_save_preview_diff_files(baseline_copy, preview_copy, registry_paths)
    return target_diff(target, baseline_copy, preview_copy, ctx.run_command)


def count_preview_deletions(baseline_path, preview_path):
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        return 0

    deleted = 0
    for path in baseline_path.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(baseline_path)
        if not (preview_path / relative).exists():
            deleted += 1
    return deleted


def managed_storage_change_paths(baseline_path, preview_path):
    paths = set()
    for dirname in (".storage", storage_managed.MANAGED_DIR):
        left_root = Path(baseline_path) / dirname
        right_root = Path(preview_path) / dirname
        relatives = set()
        for root in (left_root, right_root):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file() or path.is_symlink():
                    relatives.add(path.relative_to(root))
        for relative in relatives:
            left = left_root / relative
            right = right_root / relative
            display = f"{dirname}/{relative.as_posix()}"
            if not left.exists() or not right.exists():
                paths.add(display)
                continue
            if left.is_symlink() or right.is_symlink():
                if not (left.is_symlink() and right.is_symlink() and left.readlink() == right.readlink()):
                    paths.add(display)
                continue
            if dirname == ".storage" and left.name in NORMALIZED_STORAGE_FILES and left.name == right.name:
                left_normalized = normalized_storage_bytes(left)
                right_normalized = normalized_storage_bytes(right)
                if left_normalized is not None and right_normalized is not None:
                    if left_normalized != right_normalized:
                        paths.add(display)
                    continue
            if left.read_bytes() != right.read_bytes():
                paths.add(display)
    return sorted(paths)


def safe_preview_name(value):
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value) or "target"


def diff_text_for_fingerprint(text):
    lines = []
    for line in text.splitlines():
        if line.startswith(("--- ", "+++ ")) and "\t" in line:
            line = line.split("\t", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def fingerprint_text(text):
    return hashlib.sha256(diff_text_for_fingerprint(text).encode("utf-8")).hexdigest()


def preview_progress(ctx, details, message):
    ctx.log(message)
    if details is not None:
        ctx.add_detail(details, message)


def build_apply_preview(resolved_targets, ctx, details=None):
    preview_root = ctx.work_dir / "apply-preview"
    baseline_root = ctx.work_dir / "apply-preview-baseline"
    clear_tree(preview_root, ctx.work_dir, ctx.run_command)
    clear_tree(baseline_root, ctx.work_dir, ctx.run_command)
    chunks = []
    deletion_count = 0
    skipped_protected = []
    storage_change_paths = []
    live_fingerprints = {}

    for target in resolved_targets:
        preview_progress(ctx, details, f"Preview {target['id']}: start")
        baseline_path = baseline_root / safe_preview_name(str(target["id"]))
        preview_path = preview_root / safe_preview_name(str(target["id"]))
        build_preview_baseline(target, baseline_path, ctx)
        if target["type"] == "homeassistant":
            fingerprint = managed_ha_heaps_fingerprint(baseline_path)
            if fingerprint:
                live_fingerprints[target["id"]] = fingerprint
        skipped = sync_to_preview(target, baseline_path, preview_path, ctx)
        if skipped:
            skipped_protected.extend(skipped)
            chunks.append(f"Target {target['id']}: skipped protected .storage file(s): {', '.join(skipped)}.\n")
        if target["type"] == "homeassistant":
            storage_change_paths.extend(
                [f"{target['id']}/{path}" for path in managed_storage_change_paths(baseline_path, preview_path)]
            )
        preview_progress(ctx, details, f"Preview {target['id']}: counting deletions")
        deletion_count += count_preview_deletions(baseline_path, preview_path)
        preview_progress(ctx, details, f"Preview {target['id']}: building diff")
        chunks.append(target_diff_normalized(target, baseline_path, preview_path, ctx))
        preview_progress(ctx, details, f"Preview {target['id']}: done")

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = "No file changes."

    return {
        "diff": diff_text,
        "fingerprint": fingerprint_text(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
        "storage_changes": bool(storage_change_paths),
        "storage_change_paths": sorted(set(storage_change_paths)),
        "live_fingerprints": live_fingerprints,
    }
