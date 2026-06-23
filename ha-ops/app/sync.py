import fnmatch
import hashlib
import json
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import i18n
import storage_managed
import targets as target_model
import organizer


HA_LIVE_BRANCH = "ha-ops/ha-live"
HA_BASE_BRANCH = "ha-ops/base"


def _(key, **values):
    return i18n.t(key, **values)


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
        organizer.clean_organized_root(dest, organizer_options(target), preserve_unmanaged=True)


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
        return {"enabled": False}
    if value is True:
        return {}
    if not isinstance(value, dict):
        return None
    enabled = value.get("enabled", False)
    if not enabled:
        options = dict(value)
        options["enabled"] = False
        return options
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
    options = organizer_options(target)
    return options is not None and organizer.organizer_projection_enabled(options)


def require_homeassistant_organizer_projection_available(target):
    if target and target.get("type") == "homeassistant":
        organizer.require_projection_available(organizer_options(target))


def ensure_organized_view_is_enabled(src, target):
    options = organizer_options(target)
    if not target or target.get("type") != "homeassistant" or organizer.organizer_projection_enabled(options):
        return
    options = options or organizer_cleanup_options(target)
    if organizer.has_organized_view(src, options):
        root_name = organizer.organized_root_name(options)
        raise RuntimeError(
            f"Home Assistant organizer view exists in Git at {root_name}, but the .ha-ops/areas "
            "projection rewrite is pending and the organizer must stay disabled. Use Save HA to Git "
            "with the organizer disabled to convert Git back to heap YAML files, or remove the stale "
            ".ha-ops/areas view from Git."
        )


def organize_homeassistant_export(path, target, details, ctx):
    options = organizer_options(target)
    if options is None or not organizer.has_heap_files(path):
        return None
    projection_enabled = organizer.organizer_projection_enabled(options)
    summary = organizer.split_live_heaps_to_git(path, path, options=options)
    total = sum(summary[kind]["output_count"] for kind in ("automations", "scripts", "scenes"))
    if total and details is not None:
        message = (
            f"Organized {total} Home Assistant automation/script/scene item(s) for Git."
            if projection_enabled
            else f"Preserved {total} Home Assistant automation/script/scene item(s) as heap YAML for Git."
        )
        ctx.add_detail(details, message)
    return summary


def materialize_homeassistant_source(src, target, ctx):
    options = organizer_options(target)
    require_homeassistant_organizer_projection_available(target)
    ensure_organized_view_is_enabled(src, target)
    if options is None or not organizer.has_organized_view(src, options):
        return Path(src)

    temp = ctx.work_dir / "organizer-materialized" / safe_preview_name(target.get("id") or "homeassistant")
    clear_tree(temp, ctx.work_dir, ctx.run_command)
    sync_tree(Path(src), temp, True, None, ctx.run_command)
    organizer.compose_git_view_to_live(temp, temp, options=options)
    organizer.clean_organized_root(temp, options)
    return temp


def managed_ha_heaps_fingerprint(path):
    path = Path(path)
    if not all((path / filename).exists() for filename in organizer.HEAP_FILES.values()):
        return None
    return organizer.fingerprint_heaps(path)


def apply_homeassistant_config(src, dest, target, ctx, details=None, validate_protected_storage=False):
    src = materialize_homeassistant_source(src, target, ctx)
    if not src.exists() or not has_managed_content(src):
        if details is not None:
            ctx.add_detail(details, _("detail.skipped_homeassistant_no_git_config", target=target["id"]))
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
        validate_protected_registry=validate_protected_storage,
    )
    managed_result = storage_managed.apply_core_config_entries_projection(src, dest)
    if copied and details is not None:
        ctx.add_detail(details, _("detail.applied_homeassistant_paths", count=copied))
    if zigbee2mqtt_count and details is not None:
        ctx.add_detail(details, _("detail.applied_zigbee2mqtt_paths", count=zigbee2mqtt_count))
    if copied_count and details is not None:
        ctx.add_detail(details, _("detail.applied_storage_allowlist", count=copied_count))
    if managed_result["updated"] and details is not None:
        ctx.add_detail(details, _("detail.applied_managed_storage_projection", count=managed_result["updated"]))
    if managed_result.get("missing") and details is not None:
        ctx.add_detail(details, _("detail.skipped_managed_config_entries_missing"))
    if skipped_protected and details is not None:
        ctx.add_detail(details, _("detail.skipped_protected_storage_files", paths=", ".join(skipped_protected)))
    if not homeassistant_organizer_enabled(target):
        organizer.clean_organized_root(dest, organizer_cleanup_options(target))
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
    storage_managed.apply_core_config_entries_projection(src, dest)


def sync_storage_allowlist(
    src,
    dest,
    storage_allowlist,
    protected_storage_files,
    allow_protected=False,
    validate_protected_registry=False,
):
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
        write_storage_apply_file(src_path, dest_path, validate_protected_registry=validate_protected_registry)
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
ENTITY_REGISTRY_METADATA_FIELDS = {
    "capabilities",
    "device_id",
    "entity_category",
    "has_entity_name",
    "object_id_base",
    "original_device_class",
    "original_icon",
    "original_name",
    "previous_unique_id",
    "translation_key",
    "unit_of_measurement",
}
REGISTRY_APPLY_REQUIRED_FIELDS = {
    "core.device_registry": {
        "devices": ("modified_at",),
        "deleted_devices": ("modified_at",),
    },
    "core.entity_registry": {
        "entities": ("modified_at", "suggested_object_id", "supported_features"),
        "deleted_entities": ("modified_at",),
    },
}
ENTITY_REGISTRY_APPLY_DEFAULT_FIELDS = {
    "modified_at": "1970-01-01T00:00:00+00:00",
    "suggested_object_id": None,
    "supported_features": 0,
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


def registry_item_label(item):
    if not isinstance(item, dict):
        return stable_json_key(item)
    return str(item.get("entity_id") or item.get("name") or item.get("unique_id") or item.get("id") or stable_json_key(item))


def registry_apply_missing_fields(name, text):
    required = REGISTRY_APPLY_REQUIRED_FIELDS.get(name)
    if not required:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Refusing to apply protected Home Assistant registry with invalid JSON: {name}: {exc}") from exc
    root = data.get("data")
    if not isinstance(root, dict):
        return []
    missing = []
    for collection, fields in required.items():
        items = root.get(collection)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_missing = [field for field in fields if field not in item]
            if item_missing:
                missing.append(
                    f"{name} {collection} {registry_item_label(item)} missing {', '.join(item_missing)}"
                )
    return missing


def validate_registry_apply_text(name, text, path):
    missing = registry_apply_missing_fields(name, text)
    if not missing:
        return
    preview = "; ".join(missing[:5])
    if len(missing) > 5:
        preview += f"; and {len(missing) - 5} more"
    raise RuntimeError(f"Refusing to apply protected Home Assistant registry with missing metadata in {path}: {preview}")


def complete_entity_registry_apply_text(name, text):
    if name != "core.entity_registry":
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    entities = data.get("data", {}).get("entities")
    if not isinstance(entities, list):
        return text
    changed = False
    for item in entities:
        if not isinstance(item, dict):
            continue
        for field, value in ENTITY_REGISTRY_APPLY_DEFAULT_FIELDS.items():
            if field not in item:
                item[field] = deepcopy(value)
                changed = True
    if not changed:
        return text
    return render_registry_commit_json(data, set(registry_collection_keys(name))) + "\n"


def sort_json_list(value):
    if isinstance(value, list):
        value.sort(key=stable_json_key)


def normalize_device_registry_item(item):
    if not isinstance(item, dict):
        return
    item.pop("modified_at", None)
    item.pop("sw_version", None)
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
    item.pop("supported_features", None)
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
        restore_field_from_head(merged, head_item, "sw_version")
        restore_device_order_hidden_fields(merged, head_item, current_item)
    elif name == "core.entity_registry":
        restore_field_from_head(merged, head_item, "suggested_object_id")
        restore_field_from_head(merged, head_item, "supported_features")
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


def registry_apply_text_for_write(src_path, dest_path):
    src_path = Path(src_path)
    dest_path = Path(dest_path)
    if src_path.name in NORMALIZED_STORAGE_FILES and dest_path.exists() and dest_path.is_file():
        try:
            merged = merged_normalized_storage_apply_text(src_path.name, dest_path.read_text(), src_path.read_text())
        except (OSError, UnicodeDecodeError):
            merged = None
        if merged is not None:
            return merged
    try:
        return src_path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def write_storage_apply_file(src_path, dest_path, validate_protected_registry=False):
    src_path = Path(src_path)
    dest_path = Path(dest_path)
    text = registry_apply_text_for_write(src_path, dest_path)
    if text is not None:
        text = complete_entity_registry_apply_text(src_path.name, text)
    if validate_protected_registry and text is not None:
        validate_registry_apply_text(src_path.name, text, src_path)
    if text is not None and src_path.name in NORMALIZED_STORAGE_FILES:
        dest_path.write_text(text)
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


def validate_homeassistant_storage_apply_source(src, dest, target, ctx):
    src = materialize_homeassistant_source(src, target, ctx)
    if not src.exists() or not has_managed_content(src):
        return

    src_storage = src / ".storage"
    if not src_storage.exists():
        return

    dest_storage = Path(dest) / ".storage"
    allow_protected = target_model.allow_protected_storage(target)
    for name in ctx.storage_allowlist:
        if name in ctx.protected_storage_files and not allow_protected:
            continue
        src_path = src_storage / name
        if not src_path.exists() or not src_path.is_file():
            continue
        text = registry_apply_text_for_write(src_path, dest_storage / name)
        if text is not None:
            text = complete_entity_registry_apply_text(name, text)
            validate_registry_apply_text(name, text, src_path)


def create_homeassistant_apply_rollback(live_path, target, ctx):
    rollback_path = ctx.work_dir / "apply-rollback" / safe_preview_name(str(target["id"]))
    export_homeassistant_config(live_path, rollback_path, target, ctx)
    return rollback_path


def restore_homeassistant_apply_rollback(rollback_path, live_path, target, ctx, details):
    if details is not None:
        ctx.add_detail(details, _("detail.restoring_apply_rollback"))
    restore_homeassistant_config(rollback_path, live_path, target, ctx)


def apply_targets(resolved_targets, details, ctx):
    homeassistant_target = None
    core_stopped = False
    homeassistant_changes = ChangeSet()
    homeassistant_rollback_path = None
    homeassistant_rollback_live_path = None
    homeassistant_apply_started = False

    try:
        for target in resolved_targets:
            source_path = Path(target["source_path"])
            live_path = Path(target["live_path"])
            addon_was_started = False

            if source_path.exists() and has_managed_content(source_path):
                reject_source_symlinks(target, ctx)

            if target["type"] == "homeassistant":
                homeassistant_target = target
                homeassistant_changes = homeassistant_change_set(source_path, live_path, target, ctx, mode="apply")
                if homeassistant_changes.changed_storage:
                    validate_homeassistant_storage_apply_source(source_path, live_path, target, ctx)
                if homeassistant_changes.any():
                    homeassistant_rollback_path = create_homeassistant_apply_rollback(live_path, target, ctx)
                    homeassistant_rollback_live_path = live_path
                if (
                    homeassistant_changes.changed_storage
                    and target_model.stop_core_before_storage_apply(target)
                    and not core_stopped
                ):
                    ctx.add_detail(details, _("detail.stopping_core_before_storage_sync"))
                    ctx.core_stop()
                    core_stopped = True
                elif homeassistant_changes.changed_storage:
                    ctx.add_detail(details, _("detail.warning_storage_core_running"))
            elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
                slug = target["resolved_slug"]
                ctx.add_detail(details, _("detail.stopping_addon_before_sync", slug=slug))
                addon_was_started = ctx.stop_addon_for_sync(slug)

            ctx.add_detail(details, _("detail.syncing_target", target=target["id"], source=source_path, destination=live_path))
            if target["type"] == "homeassistant":
                homeassistant_apply_started = True
                apply_homeassistant_config(source_path, live_path, target, ctx, details, validate_protected_storage=True)
            else:
                if not source_path.exists() or not has_managed_content(source_path):
                    ctx.add_detail(details, _("detail.skipped_addon_no_git_config", target=target["id"]))
                    continue
                sync_tree(source_path, live_path, target_model.apply_delete(target), ctx.export_excludes, ctx.run_command)

            if target["type"] == "addon" and target.get("restart_after_sync", True):
                slug = target["resolved_slug"]
                if target.get("stop_addon_before_sync", False):
                    if addon_was_started:
                        ctx.add_detail(details, _("detail.starting_addon_after_sync", slug=slug))
                        ctx.addon_action(slug, "start")
                else:
                    ctx.add_detail(details, _("detail.restarting_addon", slug=slug))
                    ctx.restart_or_start_addon(slug)

        if homeassistant_target is None:
            return {"core_stopped": core_stopped}
        if not homeassistant_changes.any():
            return {"core_stopped": core_stopped}

        ctx.add_detail(details, _("detail.running_homeassistant_config_check"))
        try:
            ctx.do_core_check()
        except Exception as exc:
            setattr(exc, "core_stopped", core_stopped)
            raise

        if core_stopped:
            if target_model.start_core_after_storage_apply(homeassistant_target):
                ctx.add_detail(details, _("detail.starting_core_after_sync"))
                try:
                    ctx.core_start()
                except Exception as exc:
                    setattr(exc, "core_stopped", True)
                    raise
            else:
                ctx.add_detail(details, _("detail.core_left_stopped_after_storage_sync"))
        else:
            if target_model.restart_core_after_apply(homeassistant_target):
                ctx.add_detail(details, _("detail.restarting_core"))
                ctx.core_restart()
            elif homeassistant_changes.changed_yaml and target_model.reload_yaml_after_apply(homeassistant_target):
                ctx.add_detail(details, _("detail.reloading_yaml_config"))
                ctx.core_reload_yaml()
        return {"core_stopped": False}
    except Exception as exc:
        if homeassistant_apply_started and homeassistant_rollback_path is not None and homeassistant_rollback_live_path is not None:
            try:
                restore_homeassistant_apply_rollback(
                    homeassistant_rollback_path,
                    homeassistant_rollback_live_path,
                    homeassistant_target,
                    ctx,
                    details,
                )
            except Exception as rollback_exc:
                if details is not None:
                    details.append(_("detail.apply_rollback_failed", error=rollback_exc))
        setattr(exc, "core_stopped", core_stopped)
        raise


def build_save_export(resolved_targets, details, ctx):
    export_root = ctx.work_dir / "save-export"
    clear_tree(export_root, ctx.work_dir, ctx.run_command)

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        if not live_path.exists():
            if target.get("optional", False):
                ctx.add_detail(details, _("detail.skipped_optional_target_missing", target=target["id"], path=live_path))
                continue
            raise i18n.error("error.live_path_missing", target=target["id"], path=live_path)

        export_path = export_root / target["id"]
        if target["type"] == "homeassistant":
            require_homeassistant_organizer_projection_available(target)
            ctx.add_detail(details, _("detail.exporting_config_only", target=target["id"], path=live_path))
            copied_count, zigbee2mqtt_count, storage_count, managed_storage_count = export_homeassistant_config(
                live_path, export_path, target, ctx
            )
            organize_homeassistant_export(export_path, target, details, ctx)
            ctx.add_detail(details, _("detail.exported_homeassistant_paths", count=copied_count))
            if zigbee2mqtt_count:
                ctx.add_detail(details, _("detail.exported_legacy_zigbee2mqtt_paths", count=zigbee2mqtt_count))
            if storage_count:
                ctx.add_detail(details, _("detail.exported_storage_allowlist", count=storage_count))
            if managed_storage_count:
                ctx.add_detail(details, _("detail.exported_managed_storage_projection", count=managed_storage_count))
        else:
            ctx.add_detail(details, _("detail.exporting_target", target=target["id"], path=live_path))
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
            ctx.add_detail(details, _("detail.saving_config_only", target=target["id"], path=source_path))
            clean_homeassistant_export_destination(source_path, target, ctx)
            if not homeassistant_organizer_enabled(target):
                organizer.clean_organized_root(source_path, organizer_cleanup_options(target))
            sync_tree(export_path, source_path, False, None, ctx.run_command)
        else:
            ctx.add_detail(details, _("detail.saving_target", target=target["id"], path=source_path))
            removed_count = clean_export_destination(
                source_path,
                ctx.clean_paths,
                ctx.clean_dir_names,
                ctx.clean_file_patterns,
            )
            if removed_count:
                ctx.add_detail(details, _("detail.removed_excluded_save_items", count=removed_count, target=target["id"]))
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
        ctx.add_detail(details, _("detail.ignored_registry_noise_save_changes", count=len(restored)))
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
            raise i18n.error("error.normalized_save_checkout_failed", error=checkout.stderr.strip())
        restored.append(relative.as_posix())
    if restored:
        ctx.add_detail(details, _("detail.ignored_registry_noise_save_changes", count=len(restored)))
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


def git_ref_exists(repo_dir, ref, ctx):
    result = ctx.run_command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir)
    return result.returncode == 0


def git_rev_parse(repo_dir, ref, ctx):
    result = ctx.run_command(["git", "rev-parse", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse {ref} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_has_head(repo_dir, ctx):
    return git_ref_exists(repo_dir, "HEAD", ctx)


def git_current_branch(repo_dir, ctx):
    result = ctx.run_command(["git", "branch", "--show-current"], cwd=repo_dir)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_checkout(repo_dir, ref, ctx):
    result = ctx.run_command(["git", "checkout", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {ref} failed:\n{result.stderr.strip() or result.stdout.strip()}")


def git_checkout_branch_from_best_ref(repo_dir, branch, ctx):
    remote_ref = f"refs/remotes/origin/{branch}"
    local_ref = f"refs/heads/{branch}"
    if git_ref_exists(repo_dir, remote_ref, ctx):
        result = ctx.run_command(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    elif git_ref_exists(repo_dir, local_ref, ctx):
        result = ctx.run_command(["git", "checkout", branch], cwd=repo_dir)
    else:
        return False
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {branch} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return True


def git_reset_hard(repo_dir, ctx):
    if not git_has_head(repo_dir, ctx):
        result = ctx.run_command(["git", "clean", "-ffdx"], cwd=repo_dir)
        if result.returncode != 0:
            raise RuntimeError(f"git clean failed:\n{result.stderr.strip() or result.stdout.strip()}")
        return
    result = ctx.run_command(["git", "reset", "--hard", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git reset failed:\n{result.stderr.strip() or result.stdout.strip()}")


def git_abort_merge(repo_dir, ctx):
    path = ctx.run_command(["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=repo_dir)
    if path.returncode == 0 and (Path(repo_dir) / path.stdout.strip()).exists():
        ctx.run_command(["git", "merge", "--abort"], cwd=repo_dir)


def git_merge_in_progress(repo_dir, ctx):
    path = ctx.run_command(["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=repo_dir)
    return path.returncode == 0 and (Path(repo_dir) / path.stdout.strip()).exists()


def git_ensure_head(repo_dir, ctx, details=None):
    if git_has_head(repo_dir, ctx):
        return None
    result = ctx.run_command(
        [
            "git",
            "-c",
            "user.name=HA Ops",
            "-c",
            "user.email=ha-ops@local",
            "commit",
            "--allow-empty",
            "-m",
            "Initialize HA Ops merge base",
        ],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git empty base commit failed:\n{result.stderr.strip() or result.stdout.strip()}")
    rev = ctx.run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if rev.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed:\n{rev.stderr.strip()}")
    commit = rev.stdout.strip()
    if details is not None:
        ctx.add_detail(details, f"Created empty HA Ops merge base {commit}.")
    return commit


def git_head_tree_empty(repo_dir, ctx):
    if not git_has_head(repo_dir, ctx):
        return True
    result = ctx.run_command(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-tree failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return not result.stdout.strip()


def target_repo_source_relative(repo_dir, target):
    try:
        return Path(target["source_path"]).relative_to(repo_dir)
    except ValueError:
        return Path(target.get("source") or Path(target["source_path"]).name)


def stage_managed_save_worktree(repo_dir, resolved_targets, ctx):
    repo_dir = Path(repo_dir)
    collapse_duplicate_organizer_route_items(repo_dir, resolved_targets, ctx)

    add = ctx.run_command(["git", "add", "-A"], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed:\n{add.stderr.strip()}")

    storage_paths = []
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        source_relative = target_repo_source_relative(repo_dir, target)
        storage = repo_dir / source_relative / ".storage"
        if not storage.exists():
            continue
        for name in ctx.storage_allowlist:
            path = storage / name
            if path.exists():
                storage_paths.append(str(path.relative_to(repo_dir)))

    if storage_paths:
        forced = ctx.run_command(["git", "add", "-f", "--", *storage_paths], cwd=repo_dir)
        if forced.returncode != 0:
            raise RuntimeError(f"git add allowlisted .storage failed:\n{forced.stderr.strip()}")


def sync_applied_normalized_storage_to_repo_worktree(repo_dir, resolved_targets, ctx):
    repo_dir = Path(repo_dir)
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        source_root = materialize_homeassistant_source(Path(target["source_path"]), target, ctx)
        source_storage = source_root / ".storage"
        live_storage = Path(target["live_path"]) / ".storage"
        repo_storage = repo_dir / target_repo_source_relative(repo_dir, target) / ".storage"
        for name in NORMALIZED_STORAGE_FILES:
            source_path = source_storage / name
            live_path = live_storage / name
            repo_path = repo_storage / name
            if not source_path.exists() or not live_path.exists():
                continue
            ensure_dir(repo_path.parent)
            shutil.copy2(live_path, repo_path)


def merge_apply_normalized_storage_metadata_into_repo_worktree(repo_dir, resolved_targets, ctx):
    repo_dir = Path(repo_dir)
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        repo_storage = repo_dir / target_repo_source_relative(repo_dir, target) / ".storage"
        for name in NORMALIZED_STORAGE_FILES:
            repo_path = repo_storage / name
            if not repo_path.exists() or not repo_path.is_file():
                continue
            try:
                current_text = repo_path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            relative = repo_path.relative_to(repo_dir).as_posix()
            head = ctx.run_command(["git", "show", f"HEAD:{relative}"], cwd=repo_dir)
            if head.returncode == 0:
                text = merged_normalized_storage_apply_text(name, head.stdout, current_text)
            else:
                text = None
            if text is None:
                text = current_text
            text = complete_entity_registry_apply_text(name, text)
            if text != current_text:
                repo_path.write_text(text)


def git_status_porcelain(repo_dir, ctx):
    result = ctx.run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def git_commit_if_needed(repo_dir, message, ctx):
    merge_in_progress = git_merge_in_progress(repo_dir, ctx)
    if not git_status_porcelain(repo_dir, ctx) and not merge_in_progress:
        return None
    result = ctx.run_command(
        [
            "git",
            "-c",
            "user.name=HA Ops",
            "-c",
            "user.email=ha-ops@local",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed:\n{result.stderr.strip() or result.stdout.strip()}")
    rev = ctx.run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if rev.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed:\n{rev.stderr.strip()}")
    return rev.stdout.strip()


def git_commit_index_as_single_parent_if_needed(repo_dir, message, ctx):
    diff = ctx.run_command(["git", "diff", "--cached", "--quiet", "HEAD", "--"], cwd=repo_dir)
    if diff.returncode == 0:
        git_abort_merge(repo_dir, ctx)
        git_reset_hard(repo_dir, ctx)
        return None
    if diff.returncode != 1:
        raise RuntimeError(f"git diff --cached failed:\n{diff.stderr.strip() or diff.stdout.strip()}")

    tree = ctx.run_command(["git", "write-tree"], cwd=repo_dir)
    if tree.returncode != 0:
        raise RuntimeError(f"git write-tree failed:\n{tree.stderr.strip() or tree.stdout.strip()}")
    parent = ctx.run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if parent.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed:\n{parent.stderr.strip() or parent.stdout.strip()}")
    commit = ctx.run_command(
        [
            "git",
            "-c",
            "user.name=HA Ops",
            "-c",
            "user.email=ha-ops@local",
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            parent.stdout.strip(),
            "-m",
            message,
        ],
        cwd=repo_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit-tree failed:\n{commit.stderr.strip() or commit.stdout.strip()}")
    new_commit = commit.stdout.strip()
    reset = ctx.run_command(["git", "reset", "--hard", new_commit], cwd=repo_dir)
    if reset.returncode != 0:
        raise RuntimeError(f"git reset failed:\n{reset.stderr.strip() or reset.stdout.strip()}")
    return new_commit


def ensure_live_branch_available(repo_dir, ctx, prefer_local=False):
    if prefer_local and git_ref_exists(repo_dir, f"refs/heads/{HA_LIVE_BRANCH}", ctx):
        git_checkout(repo_dir, HA_LIVE_BRANCH, ctx)
        return
    if not git_checkout_branch_from_best_ref(repo_dir, HA_LIVE_BRANCH, ctx):
        result = ctx.run_command(["git", "checkout", "-B", HA_LIVE_BRANCH, "HEAD"], cwd=repo_dir)
        if result.returncode != 0:
            raise RuntimeError(f"git checkout {HA_LIVE_BRANCH} failed:\n{result.stderr.strip() or result.stdout.strip()}")
        return


def update_ha_live_branch(resolved_targets, repo_dir, details, ctx, include_redundant_data=False, prefer_local_live=False):
    export_root = build_save_export(resolved_targets, details, ctx)
    ensure_live_branch_available(repo_dir, ctx, prefer_local=prefer_local_live)
    apply_save_export(resolved_targets, export_root, details, ctx)
    if not include_redundant_data:
        restore_normalized_equal_save_worktree(repo_dir, resolved_targets, details, ctx)
        normalize_changed_save_registry_worktree(repo_dir, resolved_targets, details, ctx)
    stage_managed_save_worktree(repo_dir, resolved_targets, ctx)
    commit = git_commit_if_needed(repo_dir, "Update HA live export", ctx)
    if commit and details is not None:
        ctx.add_detail(details, f"Updated {HA_LIVE_BRANCH} at {commit}.")
    return commit


def reset_service_branches_from_main(resolved_targets, repo_dir, main_branch, details, ctx, include_redundant_data=False):
    repo_dir = Path(repo_dir)
    git_checkout(repo_dir, main_branch, ctx)
    git_abort_merge(repo_dir, ctx)
    git_reset_hard(repo_dir, ctx)

    export_root = build_save_export(resolved_targets, details, ctx)
    checkout = ctx.run_command(["git", "checkout", "-B", HA_LIVE_BRANCH, main_branch], cwd=repo_dir)
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout {HA_LIVE_BRANCH} failed:\n{checkout.stderr.strip() or checkout.stdout.strip()}")

    apply_save_export(resolved_targets, export_root, details, ctx)
    if not include_redundant_data:
        restore_normalized_equal_save_worktree(repo_dir, resolved_targets, details, ctx)
        normalize_changed_save_registry_worktree(repo_dir, resolved_targets, details, ctx)
    stage_managed_save_worktree(repo_dir, resolved_targets, ctx)
    git_commit_if_needed(repo_dir, "Reset HA live export", ctx)
    live_commit = git_rev_parse(repo_dir, HA_LIVE_BRANCH, ctx)
    base = update_base_branch(repo_dir, main_branch, ctx)
    if details is not None:
        ctx.add_detail(details, f"Reset {HA_LIVE_BRANCH} to {live_commit}.")
        if base:
            ctx.add_detail(details, f"Reset {HA_BASE_BRANCH} to {base}.")
    git_checkout(repo_dir, main_branch, ctx)
    git_reset_hard(repo_dir, ctx)
    return {"ha_live": live_commit, "ha_base": base}


def merge_status_lines(repo_dir, ctx):
    result = ctx.run_command(["git", "diff", "--name-status", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise i18n.error("error.git_diff_name_status_failed", error=result.stderr.strip())
    labels = {
        "A": _("preview.change_added"),
        "M": _("preview.change_modified"),
        "D": _("preview.change_deleted"),
        "R": _("preview.change_renamed"),
        "C": _("preview.change_copied"),
    }
    lines = []
    paths = []
    for raw in result.stdout.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        status = parts[0]
        path = parts[-1]
        label = labels.get(status[:1], _("preview.change_modified"))
        lines.append(_("preview.change_status_line", label=label, path=path))
        paths.append(path)
    return lines, sorted(set(paths))


def preview_status_line(path, status):
    labels = {
        "A": _("preview.change_added"),
        "M": _("preview.change_modified"),
        "D": _("preview.change_deleted"),
        "R": _("preview.change_renamed"),
        "C": _("preview.change_copied"),
    }
    return _("preview.change_status_line", label=labels.get(status[:1], _("preview.change_modified")), path=path)


def merge_diff(repo_dir, ctx):
    result = ctx.run_command(["git", "diff", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def merge_change_paths(repo_dir, ctx):
    result = ctx.run_command(["git", "diff", "--name-only", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed:\n{result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def merge_preview_candidate_paths(repo_dir, resolved_targets, ctx):
    return sorted(set(merge_change_paths(repo_dir, ctx)) | set(organizer_generated_save_merge_paths(repo_dir, resolved_targets, ctx)))


def organizer_generated_save_merge_paths(repo_dir, resolved_targets, ctx):
    repo_dir = Path(repo_dir)
    paths = set()
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        options = organizer_options(target)
        if options is None:
            continue
        source_relative = target_repo_source_relative(repo_dir, target)
        organized_relative = source_relative / organizer.organized_root_name(options)
        result = ctx.run_command(
            ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", organized_relative.as_posix()],
            cwd=repo_dir,
        )
        if result.returncode == 0:
            paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        source_path = repo_dir / source_relative
        for relative in organizer.generated_organized_relative_files(source_path, options):
            paths.add((source_relative / relative).as_posix())
    return sorted(paths)


def organizer_heap_fingerprint_for_diff(root, options, scratch_root, ctx):
    clear_tree(scratch_root, ctx.work_dir, ctx.run_command)
    sync_tree(root, scratch_root, True, [".git/"], ctx.run_command)
    if organizer.has_organized_view(scratch_root, options):
        organizer.compose_git_view_to_live(scratch_root, scratch_root, options=options)
    if not organizer.has_heap_files(scratch_root):
        return None
    return organizer.fingerprint_heaps(scratch_root)


def rewrite_equal_organizer_save_diff_to_live_route(root, options):
    if organizer.has_organized_view(root, options):
        organizer.compose_git_view_to_live(root, root, options=options)
    if organizer.has_heap_files(root):
        organizer.split_live_heaps_to_git(root, root, options=options)
        normalize_organizer_index_for_diff(root, options)


def organizer_payload_key(value):
    return organizer.canonical_json_bytes(value)


def organizer_parse_exceptions():
    yaml_error = getattr(getattr(organizer, "yaml", None), "YAMLError", None)
    exceptions = [OSError, UnicodeDecodeError, RuntimeError, json.JSONDecodeError]
    if yaml_error is not None:
        exceptions.append(yaml_error)
    return tuple(exceptions)


def organizer_file_items(path, kind):
    if kind == "automations":
        data = organizer.yaml_load(path, [])
        return {
            organizer.automation_identity(item, index): organizer_payload_key(item)
            for index, item in enumerate(data)
        }
    if kind == "scripts":
        data = organizer.yaml_load(path, {})
        return {str(key): organizer_payload_key(value) for key, value in data.items()}
    data = organizer.yaml_load(path, [])
    return {
        organizer.scene_identity(item, index): organizer_payload_key(item)
        for index, item in enumerate(data)
    }


def organizer_file_ordered_items(path, kind):
    if kind == "automations":
        data = organizer.yaml_load(path, [])
        return [
            (organizer.automation_identity(item, index), organizer_payload_key(item), item)
            for index, item in enumerate(data)
        ]
    if kind == "scripts":
        data = organizer.yaml_load(path, {})
        return [(str(key), organizer_payload_key(value), (key, value)) for key, value in data.items()]
    data = organizer.yaml_load(path, [])
    return [
        (organizer.scene_identity(item, index), organizer_payload_key(item), item)
        for index, item in enumerate(data)
    ]


def write_organizer_file_ordered_items(path, kind, items):
    if kind == "scripts":
        organizer.yaml_dump(path, {key: value for _identity, _payload, (key, value) in items})
        return
    organizer.yaml_dump(path, [item for _identity, _payload, item in items])


def organizer_items_by_identity(root, options, kind):
    items = {}
    filename = organizer.HEAP_FILES[kind]
    for relative in organizer.generated_organized_relative_files(root, options):
        if relative.name != filename:
            continue
        items.update(organizer_file_items(Path(root) / relative, kind))
    return items


def organizer_file_duplicates_items(path, kind, existing_items):
    file_items = organizer_file_items(path, kind)
    if not file_items:
        return False
    return all(existing_items.get(identity) == payload for identity, payload in file_items.items())


def mirror_duplicate_organizer_items(source_path, dest_path, kind, existing_items):
    source_items = organizer_file_ordered_items(source_path, kind)
    duplicate_items = [
        item
        for item in source_items
        if existing_items.get(item[0]) == item[1]
    ]
    if not duplicate_items:
        return
    ensure_dir(dest_path.parent)
    if len(duplicate_items) == len(source_items):
        shutil.copy2(source_path, dest_path)
        return
    write_organizer_file_ordered_items(dest_path, kind, duplicate_items)


def organizer_kind_for_generated_path(path):
    filename = Path(path).name
    for kind, heap_filename in organizer.HEAP_FILES.items():
        if filename == heap_filename:
            return kind
    return None


def organizer_target_relative_path(path, source_relative, options):
    try:
        target_relative = Path(path).relative_to(source_relative)
    except ValueError:
        return None
    organized_root = Path(organizer.organized_root_name(options))
    try:
        target_relative.relative_to(organized_root)
    except ValueError:
        return None
    return target_relative


def organizer_git_ref_file_items(repo_dir, ref, path, kind, scratch_root, ctx):
    result = ctx.run_command(["git", "show", f"{ref}:{path}"], cwd=repo_dir)
    if result.returncode != 0:
        return {}
    scratch_path = scratch_root / safe_preview_name(path)
    ensure_dir(scratch_path.parent)
    scratch_path.write_text(result.stdout)
    return organizer_file_items(scratch_path, kind)


def organizer_worktree_file_items(repo_dir, path, kind):
    worktree_path = Path(repo_dir) / path
    if not worktree_path.exists() or not worktree_path.is_file():
        return {}
    return organizer_file_items(worktree_path, kind)


def organizer_suppressed_save_preserve_triggers(repo_dir, resolved_targets, suppressed_paths, visible_paths, ctx):
    repo_dir = Path(repo_dir)
    suppressed_set = {str(path) for path in suppressed_paths if str(path)}
    visible_set = {str(path) for path in visible_paths if str(path)}
    if not suppressed_set or not visible_set:
        return {}

    scratch_root = ctx.work_dir / "save-merge-organizer-preserve"
    clear_tree(scratch_root, ctx.work_dir, ctx.run_command)
    preserve_triggers = {}
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        options = organizer_options(target)
        if options is None:
            continue
        source_relative = target_repo_source_relative(repo_dir, target)
        for suppressed_path in sorted(suppressed_set):
            suppressed_relative = organizer_target_relative_path(suppressed_path, source_relative, options)
            if suppressed_relative is None:
                continue
            kind = organizer_kind_for_generated_path(suppressed_relative)
            if kind is None:
                continue
            suppressed_items = organizer_worktree_file_items(repo_dir, suppressed_path, kind)
            if not suppressed_items:
                suppressed_items = organizer_git_ref_file_items(
                    repo_dir,
                    "HEAD",
                    suppressed_path,
                    kind,
                    scratch_root / safe_preview_name(suppressed_path),
                    ctx,
                )
            if not suppressed_items:
                continue
            triggers = []
            for visible_path in sorted(visible_set):
                visible_relative = organizer_target_relative_path(visible_path, source_relative, options)
                if visible_relative is None or visible_path == suppressed_path:
                    continue
                if organizer_kind_for_generated_path(visible_relative) != kind:
                    continue
                head_items = organizer_git_ref_file_items(
                    repo_dir,
                    "HEAD",
                    visible_path,
                    kind,
                    scratch_root / safe_preview_name(visible_path),
                    ctx,
                )
                if not head_items:
                    head_items = {}
                try:
                    worktree_items = organizer_worktree_file_items(repo_dir, visible_path, kind)
                except organizer_parse_exceptions():
                    continue
                removes_duplicate_item = any(
                    head_items.get(identity) == payload
                    and worktree_items.get(identity) != payload
                    for identity, payload in suppressed_items.items()
                )
                adds_duplicate_item = any(
                    worktree_items.get(identity) == payload
                    and head_items.get(identity) != payload
                    for identity, payload in suppressed_items.items()
                )
                if removes_duplicate_item or adds_duplicate_item:
                    triggers.append(visible_path)
            if triggers:
                preserve_triggers[suppressed_path] = sorted(set(triggers))
    return preserve_triggers


def collapse_duplicate_organizer_route_items(repo_dir, resolved_targets, ctx):
    repo_dir = Path(repo_dir)
    scratch_root = ctx.work_dir / "save-merge-organizer-collapse"
    clear_tree(scratch_root, ctx.work_dir, ctx.run_command)
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        options = organizer_options(target)
        if options is None:
            continue
        source_relative = target_repo_source_relative(repo_dir, target)
        source_path = repo_dir / source_relative
        if not organizer.has_organized_view(source_path, options):
            continue
        safe_name = safe_preview_name(source_relative.as_posix())
        collapse_target_duplicate_organizer_route_items(
            repo_dir,
            source_relative,
            source_path,
            options,
            scratch_root / safe_name,
            ctx,
        )


def collapse_target_duplicate_organizer_route_items(repo_dir, source_relative, source_path, options, scratch_root, ctx):
    for kind, filename in organizer.HEAP_FILES.items():
        relatives = [
            relative
            for relative in organizer.generated_organized_relative_files(source_path, options)
            if relative.name == filename
        ]
        if len(relatives) < 2:
            continue
        ha_live_items = {}
        current_items = {}
        occurrences = {}
        for relative in relatives:
            repo_path = (source_relative / relative).as_posix()
            ha_live_items[relative] = organizer_git_ref_file_items(
                repo_dir,
                HA_LIVE_BRANCH,
                repo_path,
                kind,
                scratch_root / safe_preview_name(repo_path),
                ctx,
            )
            items = organizer_file_ordered_items(source_path / relative, kind)
            current_items[relative] = items
            for index, item in enumerate(items):
                occurrences.setdefault(item[0], []).append((relative, index, item))

        removals = {}
        for identity, items in occurrences.items():
            if len(items) < 2:
                continue
            ha_matches = [
                item
                for item in items
                if ha_live_items.get(item[0], {}).get(identity) == item[2][1]
            ]
            if not ha_matches:
                continue
            keep = sorted(ha_matches, key=lambda item: item[0].as_posix())[0]
            for item in items:
                if item == keep:
                    continue
                removals.setdefault(item[0], set()).add(item[1])

        for relative, indexes in removals.items():
            path = source_path / relative
            kept = [
                item
                for index, item in enumerate(current_items.get(relative, []))
                if index not in indexes
            ]
            if kept:
                write_organizer_file_ordered_items(path, kind, kept)
            elif path.exists() or path.is_symlink():
                safe_remove_path(path)
                prune_empty_organizer_dirs(path.parent, organizer.organized_root(source_path, options))


def prune_empty_organizer_dirs(path, root):
    path = Path(path)
    root = Path(root)
    while path != root and root in path.parents and path.exists() and path.is_dir() and not any(path.iterdir()):
        path.rmdir()
        path = path.parent


def hide_duplicate_organizer_route_files(before_target, after_target, options):
    for kind, filename in organizer.HEAP_FILES.items():
        before_files = {
            relative
            for relative in organizer.generated_organized_relative_files(before_target, options)
            if relative.name == filename
        }
        after_files = {
            relative
            for relative in organizer.generated_organized_relative_files(after_target, options)
            if relative.name == filename
        }
        for relative in sorted(after_files - before_files):
            before_items = organizer_items_by_identity(before_target, options, kind)
            mirror_duplicate_organizer_items(after_target / relative, before_target / relative, kind, before_items)
        for relative in sorted(before_files - after_files):
            after_items = organizer_items_by_identity(after_target, options, kind)
            mirror_duplicate_organizer_items(before_target / relative, after_target / relative, kind, after_items)


def remove_duplicate_organizer_route_items_from_modified_files(before_target, after_target, options):
    for kind, filename in organizer.HEAP_FILES.items():
        relatives = sorted(
            {
                relative
                for relative in organizer.generated_organized_relative_files(before_target, options)
                if relative.name == filename
            }
            | {
                relative
                for relative in organizer.generated_organized_relative_files(after_target, options)
                if relative.name == filename
            }
        )
        before_items_by_file = {}
        after_items_by_file = {}
        try:
            for relative in relatives:
                before_path = before_target / relative
                after_path = after_target / relative
                if before_path.exists():
                    before_items_by_file[relative] = organizer_file_ordered_items(before_path, kind)
                if after_path.exists():
                    after_items_by_file[relative] = organizer_file_ordered_items(after_path, kind)
        except organizer_parse_exceptions():
            continue

        def items_outside(items_by_file, current_relative):
            items = {}
            for relative, file_items in items_by_file.items():
                if relative == current_relative:
                    continue
                for identity, payload, _item in file_items:
                    items[identity] = payload
            return items

        def prune_route_only_items(root, relative, source_items_by_file, other_items_by_file):
            path = root / relative
            current_items = source_items_by_file.get(relative)
            if not current_items or not path.exists():
                return
            counterpart_items = {
                identity: payload
                for identity, payload, _item in other_items_by_file.get(relative, [])
            }
            other_items = items_outside(other_items_by_file, relative)
            kept = [
                item
                for item in current_items
                if not (
                    counterpart_items.get(item[0]) != item[1]
                    and other_items.get(item[0]) == item[1]
                )
            ]
            if len(kept) == len(current_items):
                return
            if kept:
                write_organizer_file_ordered_items(path, kind, kept)
            elif path.exists() or path.is_symlink():
                safe_remove_path(path)
                prune_empty_organizer_dirs(path.parent, organizer.organized_root(root, options))

        for relative in relatives:
            prune_route_only_items(before_target, relative, before_items_by_file, after_items_by_file)
            prune_route_only_items(after_target, relative, after_items_by_file, before_items_by_file)


def normalize_organizer_save_diff_files(before_root, after_root, resolved_targets, repo_dir, ctx):
    repo_dir = Path(repo_dir)
    scratch_root = ctx.work_dir / "save-merge-organizer-fingerprint"
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        options = organizer_options(target)
        if options is None:
            continue
        source_relative = target_repo_source_relative(repo_dir, target)
        before_target = before_root / source_relative
        after_target = after_root / source_relative
        if not before_target.exists() or not after_target.exists():
            continue
        if not organizer.has_organized_view(before_target, options) and not organizer.has_organized_view(after_target, options):
            continue

        hide_duplicate_organizer_route_files(before_target, after_target, options)
        remove_duplicate_organizer_route_items_from_modified_files(before_target, after_target, options)
        safe_name = safe_preview_name(source_relative.as_posix())
        try:
            before_fingerprint = organizer_heap_fingerprint_for_diff(
                before_target,
                options,
                scratch_root / safe_name / "before",
                ctx,
            )
            after_fingerprint = organizer_heap_fingerprint_for_diff(
                after_target,
                options,
                scratch_root / safe_name / "after",
                ctx,
            )
        except (RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not before_fingerprint or before_fingerprint != after_fingerprint:
            continue

        rewrite_equal_organizer_save_diff_to_live_route(before_target, options)
        rewrite_equal_organizer_save_diff_to_live_route(after_target, options)
        mirror_organizer_generated_view_for_apply_diff(after_target, before_target, options)


def merge_diff_normalized(repo_dir, resolved_targets, ctx, normalize_registry=True):
    paths = sorted(set(merge_change_paths(repo_dir, ctx)) | set(organizer_generated_save_merge_paths(repo_dir, resolved_targets, ctx)))
    if not paths:
        return ""

    repo_dir = Path(repo_dir)
    diff_root = ctx.work_dir / "save-merge-diff"
    before_root = diff_root / "before"
    after_root = diff_root / "after"
    clear_tree(diff_root, ctx.work_dir, ctx.run_command)
    for raw_path in paths:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        path_text = relative.as_posix()
        if (
            relative.name in NORMALIZED_STORAGE_FILES
            and git_conflict_stage_exists(repo_dir, 2, path_text, ctx)
            and git_conflict_stage_exists(repo_dir, 3, path_text, ctx)
        ):
            before_path = before_root / relative
            after_path = after_root / relative
            ensure_dir(before_path.parent)
            ensure_dir(after_path.parent)
            before_path.write_text(git_stage_text(repo_dir, 2, path_text, ctx))
            after_path.write_text(git_stage_text(repo_dir, 3, path_text, ctx))
        else:
            head = ctx.run_command(["git", "show", f"HEAD:{path_text}"], cwd=repo_dir)
            if head.returncode == 0:
                before_path = before_root / relative
                ensure_dir(before_path.parent)
                before_path.write_text(head.stdout)
            worktree_path = repo_dir / relative
            if worktree_path.exists() and worktree_path.is_file():
                after_path = after_root / relative
                ensure_dir(after_path.parent)
                shutil.copy2(worktree_path, after_path)

    if normalize_registry:
        normalize_save_preview_diff_files(
            before_root,
            after_root,
            normalized_save_registry_paths(resolved_targets, repo_dir),
        )
    normalize_organizer_save_diff_files(before_root, after_root, resolved_targets, repo_dir, ctx)
    return save_preview_diff(before_root, after_root, ctx.run_command)


def merge_ha_live_into_git(repo_dir, main_branch, ctx):
    git_checkout(repo_dir, main_branch, ctx)
    git_abort_merge(repo_dir, ctx)
    git_reset_hard(repo_dir, ctx)
    result = ctx.run_command(["git", "merge", "--no-commit", "--no-ff", HA_LIVE_BRANCH], cwd=repo_dir)
    conflicts = []
    if result.returncode != 0:
        conflict_result = ctx.run_command(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
        conflicts = [line.strip() for line in conflict_result.stdout.splitlines() if line.strip()]
        if not conflicts:
            raise RuntimeError(f"git merge {HA_LIVE_BRANCH} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return conflicts


def update_base_branch(repo_dir, main_branch, ctx):
    merge_base = ctx.run_command(["git", "merge-base", main_branch, HA_LIVE_BRANCH], cwd=repo_dir)
    if merge_base.returncode != 0:
        return None
    base = merge_base.stdout.strip()
    if not base:
        return None
    result = ctx.run_command(["git", "branch", "-f", HA_BASE_BRANCH, base], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git branch {HA_BASE_BRANCH} failed:\n{result.stderr.strip()}")
    return base


def git_conflict_stage_exists(repo_dir, stage, path, ctx):
    result = ctx.run_command(["git", "cat-file", "-e", f":{stage}:{path}"], cwd=repo_dir)
    return result.returncode == 0


def git_ref_path_exists(repo_dir, ref, path, ctx):
    result = ctx.run_command(["git", "cat-file", "-e", f"{ref}:{path}"], cwd=repo_dir)
    return result.returncode == 0


def git_resolve_conflict_path(repo_dir, path, stage, ctx):
    safe_path = Path(path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise RuntimeError("Invalid preview path")
    path_text = safe_path.as_posix()
    if git_conflict_stage_exists(repo_dir, stage, path_text, ctx):
        checkout = ctx.run_command(["git", "checkout-index", "-f", f"--stage={stage}", "--", path_text], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout conflict path failed:\n{checkout.stderr.strip()}")
        add = ctx.run_command(["git", "add", "--", path_text], cwd=repo_dir)
        if add.returncode != 0:
            raise RuntimeError(f"git add conflict path failed:\n{add.stderr.strip()}")
        return
    remove = ctx.run_command(["git", "rm", "-f", "--ignore-unmatch", "--", path_text], cwd=repo_dir)
    if remove.returncode != 0:
        raise RuntimeError(f"git rm conflict path failed:\n{remove.stderr.strip()}")


def git_restore_path_from_ref(repo_dir, ref, path, ctx):
    safe_path = Path(path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise RuntimeError("Invalid preview path")
    path_text = safe_path.as_posix()
    if git_ref_path_exists(repo_dir, ref, path_text, ctx):
        checkout = ctx.run_command(["git", "checkout", ref, "--", path_text], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout preview path failed:\n{checkout.stderr.strip()}")
        return
    remove = ctx.run_command(["git", "rm", "-f", "--ignore-unmatch", "--", path_text], cwd=repo_dir)
    if remove.returncode != 0:
        raise RuntimeError(f"git rm preview path failed:\n{remove.stderr.strip()}")


def git_stage_text(repo_dir, stage, path, ctx):
    result = ctx.run_command(["git", "show", f":{stage}:{path}"], cwd=repo_dir)
    if result.returncode != 0:
        return ""
    return result.stdout


def conflict_fingerprint(conflicts, repo_dir, ctx):
    chunks = []
    for path in sorted(conflicts):
        chunks.append(f"path:{path}")
        chunks.append(f"stage2:{git_stage_text(repo_dir, 2, path, ctx)}")
        chunks.append(f"stage3:{git_stage_text(repo_dir, 3, path, ctx)}")
    return fingerprint_text("\n".join(chunks))


def merge_conflict_fingerprint(conflicts, repo_dir, diff, ctx):
    return fingerprint_text("\n".join([diff, conflict_fingerprint(conflicts, repo_dir, ctx)]))


def merge_diff_for_save_preview(repo_dir, resolved_targets, include_redundant_data, ctx):
    return merge_diff_normalized(repo_dir, resolved_targets, ctx, normalize_registry=not include_redundant_data)


def merge_preview_for_save(repo_dir, resolved_targets, include_redundant_data, ctx):
    raw_status_lines, raw_status_paths = merge_status_lines(repo_dir, ctx)
    if not raw_status_paths:
        return {
            "status_lines": raw_status_lines,
            "paths": raw_status_paths,
            "diff": "",
            "suppressed_paths": [],
        }

    raw_paths = sorted(set(raw_status_paths) | set(organizer_generated_save_merge_paths(repo_dir, resolved_targets, ctx)))
    diff = merge_diff_normalized(repo_dir, resolved_targets, ctx, normalize_registry=not include_redundant_data)
    paths = diff_change_paths(diff)
    if diff and not paths:
        paths = raw_paths

    diff_status_by_path = diff_change_statuses(diff)
    status_by_path = {line.split(": ", 1)[1]: line for line in raw_status_lines if ": " in line}
    status_lines = [
        preview_status_line(path, diff_status_by_path[path])
        if path in diff_status_by_path
        else status_by_path.get(path, preview_status_line(path, "M"))
        for path in paths
    ]
    path_set = set(paths)
    suppressed_paths = sorted(path for path in raw_paths if path not in path_set)
    return {
        "status_lines": status_lines,
        "paths": paths,
        "diff": diff if paths else "",
        "suppressed_paths": suppressed_paths,
        "suppressed_preserve_triggers": organizer_suppressed_save_preserve_triggers(
            repo_dir,
            resolved_targets,
            suppressed_paths,
            paths,
            ctx,
        ),
    }


def commit_save_merge(repo_dir, main_branch, resolved_targets, resolutions, message, details, ctx):
    conflicts = merge_ha_live_into_git(repo_dir, main_branch, ctx)
    if conflicts:
        missing = [path for path in conflicts if (resolutions or {}).get(path) not in {"ha", "git"}]
        if missing:
            raise RuntimeError(f"Save merge has unresolved conflict(s): {', '.join(missing)}")
        for path in conflicts:
            stage = 3 if resolutions.get(path) == "ha" else 2
            git_resolve_conflict_path(repo_dir, path, stage, ctx)
        if details is not None:
            ctx.add_detail(details, f"Resolved {len(conflicts)} Save merge conflict(s) from preview decisions.")

    for path, choice in sorted((resolutions or {}).items()):
        if path in conflicts:
            continue
        if choice != "git":
            continue
        git_restore_path_from_ref(repo_dir, "HEAD", path, ctx)

    stage_managed_save_worktree(repo_dir, resolved_targets, ctx)
    partial_save = any(choice == "git" for choice in (resolutions or {}).values())
    commit = (
        git_commit_index_as_single_parent_if_needed(repo_dir, message, ctx)
        if partial_save
        else git_commit_if_needed(repo_dir, message, ctx)
    )
    base = update_base_branch(repo_dir, main_branch, ctx)
    if base and details is not None:
        ctx.add_detail(details, f"Updated {HA_BASE_BRANCH} at {base}.")
    return commit


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
            lines.append(_("preview.change_status_line", label=_("preview.change_added"), path=path))
        elif path not in after:
            lines.append(_("preview.change_status_line", label=_("preview.change_deleted"), path=path))
        elif (file_differs(after[path], before[path]) if include_redundant_data else save_file_differs(after[path], before[path])):
            lines.append(_("preview.change_status_line", label=_("preview.change_modified"), path=path))
    return lines


def save_preview_change_paths(status_lines):
    paths = []
    for line in status_lines:
        marker = ": "
        if marker in line:
            paths.append(line.split(marker, 1)[1])
    return sorted(set(paths))


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


def restore_source_organized_view_for_apply_diff(preview_copy, target, ctx):
    options = organizer_options(target)
    if options is None:
        return False
    source_path = Path(target.get("source_path") or "")
    if not organizer.has_organized_view(source_path, options):
        return False

    organizer.clean_organized_root(preview_copy, options, preserve_unmanaged=True)
    for relative in organizer.generated_organized_relative_files(source_path, options):
        source_file = source_path / relative
        preview_file = preview_copy / relative
        ensure_dir(preview_file.parent)
        shutil.copy2(source_file, preview_file)
    return True


def mirror_organizer_generated_view_for_apply_diff(source_root, dest_root, options):
    organizer.clean_organized_root(dest_root, options, preserve_unmanaged=True)
    for relative in organizer.generated_organized_relative_files(source_root, options):
        source_file = source_root / relative
        dest_file = dest_root / relative
        ensure_dir(dest_file.parent)
        shutil.copy2(source_file, dest_file)


def normalize_organizer_apply_diff_files(baseline_copy, preview_copy, target, ctx):
    options = organizer_options(target)
    if options is None:
        return False
    if not organizer.has_heap_files(baseline_copy) and not organizer.has_heap_files(preview_copy):
        return False
    baseline_fingerprint = organizer.fingerprint_heaps(baseline_copy) if organizer.has_heap_files(baseline_copy) else None
    preview_fingerprint = organizer.fingerprint_heaps(preview_copy) if organizer.has_heap_files(preview_copy) else None
    organizer.split_live_heaps_to_git(baseline_copy, baseline_copy, options=options)
    organizer.split_live_heaps_to_git(preview_copy, preview_copy, options=options)
    restore_source_organized_view_for_apply_diff(preview_copy, target, ctx)
    if baseline_fingerprint and baseline_fingerprint == preview_fingerprint:
        mirror_organizer_generated_view_for_apply_diff(preview_copy, baseline_copy, options)
    normalize_organizer_index_for_diff(baseline_copy, options)
    normalize_organizer_index_for_diff(preview_copy, options)
    return True


def load_storage_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def entity_registry_metadata_downgrade_warnings(target_id, baseline_path, preview_path):
    baseline = load_storage_json(Path(baseline_path) / ".storage" / "core.entity_registry")
    preview = load_storage_json(Path(preview_path) / ".storage" / "core.entity_registry")
    if not isinstance(baseline, dict) or not isinstance(preview, dict):
        return []

    baseline_entities = baseline.get("data", {}).get("entities")
    preview_entities = preview.get("data", {}).get("entities")
    if not isinstance(baseline_entities, list) or not isinstance(preview_entities, list):
        return []

    keys = ENTITY_REGISTRY_COLLECTION_KEYS["entities"]
    preview_by_identity = {registry_collection_identity(keys, item): item for item in preview_entities}
    downgrades = []
    for live_item in baseline_entities:
        if not isinstance(live_item, dict):
            continue
        preview_item = preview_by_identity.get(registry_collection_identity(keys, live_item))
        if not isinstance(preview_item, dict):
            continue
        missing_fields = sorted(
            field
            for field in ENTITY_REGISTRY_METADATA_FIELDS
            if field in live_item and field not in preview_item
        )
        if missing_fields:
            downgrades.append((registry_item_label(live_item), missing_fields))

    if not downgrades:
        return []

    shown = ", ".join(
        f"{label} missing {', '.join(fields[:4])}{'...' if len(fields) > 4 else ''}"
        for label, fields in downgrades[:5]
    )
    suffix = f" and {len(downgrades) - 5} more" if len(downgrades) > 5 else ""
    return [
        "Warning: Git to HA would downgrade live core.entity_registry metadata "
        f"for target {target_id}: {shown}{suffix}. Run HA to Git first or update Git registry before applying."
    ]


def registry_deleted_item_warnings(target_id, registry_name, collection, keys, baseline_path, preview_path):
    baseline = load_storage_json(Path(baseline_path) / ".storage" / registry_name)
    preview = load_storage_json(Path(preview_path) / ".storage" / registry_name)
    if not isinstance(baseline, dict) or not isinstance(preview, dict):
        return []

    baseline_items = baseline.get("data", {}).get(collection)
    preview_items = preview.get("data", {}).get(collection)
    if not isinstance(baseline_items, list) or not isinstance(preview_items, list):
        return []

    preview_keys = {
        registry_collection_identity(keys, item)
        for item in preview_items
    }
    deleted = [
        registry_item_label(item)
        for item in baseline_items
        if registry_collection_identity(keys, item) not in preview_keys
    ]
    if not deleted:
        return []

    shown = ", ".join(deleted[:8])
    suffix = f" and {len(deleted) - 8} more" if len(deleted) > 8 else ""
    return [
        f"Warning: Git to HA would remove live {registry_name} {collection} "
        f"for target {target_id}: {shown}{suffix}. Run HA to Git first or update Git registry before applying."
    ]


def storage_registry_warnings(target_id, baseline_path, preview_path):
    warnings = []
    warnings.extend(entity_registry_metadata_downgrade_warnings(target_id, baseline_path, preview_path))
    warnings.extend(
        registry_deleted_item_warnings(
            target_id,
            "core.device_registry",
            "devices",
            DEVICE_REGISTRY_COLLECTION_KEYS["devices"],
            baseline_path,
            preview_path,
        )
    )
    warnings.extend(
        registry_deleted_item_warnings(
            target_id,
            "core.entity_registry",
            "entities",
            ENTITY_REGISTRY_COLLECTION_KEYS["entities"],
            baseline_path,
            preview_path,
        )
    )
    return warnings


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
    repo_dir = Path(repo_dir)
    main_branch = git_current_branch(repo_dir, ctx) or "main"
    try:
        git_ensure_head(repo_dir, ctx, details)
        update_ha_live_branch(resolved_targets, repo_dir, details, ctx, include_redundant_data)
        conflicts = merge_ha_live_into_git(repo_dir, main_branch, ctx)
        update_base_branch(repo_dir, main_branch, ctx)
        if conflicts:
            raw_paths = sorted(set(conflicts) | set(merge_preview_candidate_paths(repo_dir, resolved_targets, ctx)))
            summary = "\n".join(
                [_("preview.save_conflicts_title", count=len(conflicts)), *[_("preview.conflict_item", path=path) for path in conflicts]]
            )
            diff = merge_diff_for_save_preview(repo_dir, resolved_targets, include_redundant_data, ctx)
            diff_paths = diff_change_paths(diff)
            if diff and not diff_paths:
                diff_paths = raw_paths
            paths = sorted(set(conflicts) | set(diff_paths))
            suppressed_paths = sorted(path for path in raw_paths if path not in set(paths))
            return {
                "summary": summary,
                "diff": diff or summary,
                "paths": paths,
                "conflicts": conflicts,
                "fingerprint": merge_conflict_fingerprint(conflicts, repo_dir, diff, ctx),
                "suppressed_paths": suppressed_paths,
                "suppressed_preserve_triggers": organizer_suppressed_save_preserve_triggers(
                    repo_dir,
                    resolved_targets,
                    suppressed_paths,
                    paths,
                    ctx,
                ),
            }

        preview = merge_preview_for_save(repo_dir, resolved_targets, include_redundant_data, ctx)
        status_lines = preview["status_lines"]
        paths = preview["paths"]
        summary = (
            "\n".join([_("preview.save_changes_title", count=len(status_lines)), *status_lines])
            if status_lines
            else _("preview.no_save_changes")
        )
        diff = preview["diff"] if status_lines else ""
        return {
            "summary": summary,
            "diff": diff,
            "paths": paths,
            "fingerprint": fingerprint_text(diff),
            "suppressed_paths": preview["suppressed_paths"],
            "suppressed_preserve_triggers": preview.get("suppressed_preserve_triggers", {}),
        }
    finally:
        git_abort_merge(repo_dir, ctx)
        git_reset_hard(repo_dir, ctx)
        git_checkout(repo_dir, main_branch, ctx)


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
            raise i18n.error("error.live_path_missing", target=target["id"], path=live_path)
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
        ctx.add_detail(details, _("detail.unknown_base_save_conflicts", count=len(conflicts)))
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
        files.update(organizer.generated_organized_relative_files(source_path, organizer_options(target)))

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
    ctx.add_detail(details, "\n".join([_("detail.save_export_candidates", target=target["id"], count=len(paths)), *[f"- {path}" for path in paths]]))


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
    normalize_organizer_apply_diff_files(baseline_copy, preview_copy, target, ctx)
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


def diff_path_relative(path):
    if not path or path == "/dev/null":
        return None
    for marker in ("/save-to-git-preview/", "/apply-preview/", "/preview/", "/baseline/", "/before/", "/after/"):
        if marker in path:
            return path.rsplit(marker, 1)[1]
    return None


def diff_change_paths(diff_text):
    current_target = None
    previous_old = None
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("## "):
            current_target = line[3:].strip()
            continue
        if line.startswith("--- "):
            previous_old = line[4:].split("\t", 1)[0]
            continue
        if not line.startswith("+++ "):
            continue
        new_path = line[4:].split("\t", 1)[0]
        relative = diff_path_relative(new_path) or diff_path_relative(previous_old)
        if not relative:
            continue
        if current_target and not relative.startswith(f"{current_target}/"):
            relative = f"{current_target}/{relative}"
        paths.append(relative)
    return sorted(set(paths))


def diff_change_statuses(diff_text):
    current_target = None
    previous_old = None
    pending_relative = None
    pending_status = None
    statuses = {}

    def flush_pending():
        nonlocal pending_relative, pending_status
        if pending_relative and pending_relative not in statuses:
            statuses[pending_relative] = pending_status or "M"
        pending_relative = None
        pending_status = None

    for line in diff_text.splitlines():
        if line.startswith("## "):
            flush_pending()
            current_target = line[3:].strip()
            continue
        if line.startswith("--- "):
            flush_pending()
            previous_old = line[4:].split("\t", 1)[0]
            continue
        if not line.startswith("+++ "):
            if line.startswith("@@ ") and pending_relative:
                parts = line.split()
                old_zero = len(parts) > 1 and parts[1] in {"-0", "-0,0"}
                new_zero = len(parts) > 2 and parts[2] in {"+0", "+0,0"}
                if old_zero and not new_zero:
                    pending_status = "A"
                elif new_zero and not old_zero:
                    pending_status = "D"
                else:
                    pending_status = "M"
                flush_pending()
            continue
        new_path = line[4:].split("\t", 1)[0]
        old_relative = diff_path_relative(previous_old)
        new_relative = diff_path_relative(new_path)
        relative = new_relative or old_relative
        if not relative:
            continue
        if current_target and not relative.startswith(f"{current_target}/"):
            relative = f"{current_target}/{relative}"
        if old_relative is None:
            pending_status = "A"
        elif new_relative is None:
            pending_status = "D"
        else:
            pending_status = "M"
        pending_relative = relative
    flush_pending()
    return statuses


def restore_preview_paths_from_baseline(preview_path, baseline_path, keep_paths):
    for relative in sorted(set(keep_paths)):
        preview_file = preview_path / relative
        baseline_file = baseline_path / relative
        if baseline_file.exists():
            ensure_dir(preview_file.parent)
            shutil.copy2(baseline_file, preview_file)
        elif preview_file.exists() or preview_file.is_symlink():
            safe_remove_path(preview_file)


def is_organizer_generated_relative(path, options):
    relative = Path(path)
    organized_root = Path(organizer.organized_root_name(options))
    try:
        under_root = relative.relative_to(organized_root)
    except ValueError:
        return False
    return len(under_root.parts) >= 1 and relative.name in {*organizer.HEAP_FILES.values(), organizer.INDEX_NAME}


def apply_organizer_preview_path_decisions(selected_path, baseline_path, preview_path, target, keep_paths, ctx):
    options = organizer_options(target)
    if options is None:
        return False
    if not organizer.has_heap_files(baseline_path) and not organizer.has_heap_files(preview_path):
        return False

    organizer_keep_paths = [
        Path(path)
        for path in keep_paths
        if is_organizer_generated_relative(path, options)
    ]
    if not organizer_keep_paths:
        return False

    safe_id = safe_preview_name(str(target["id"]))
    decision_root = ctx.work_dir / "apply-preview-selected-organizer" / safe_id
    baseline_organized = decision_root / "baseline"
    selected_organized = decision_root / "selected"
    clear_tree(decision_root, ctx.work_dir, ctx.run_command)
    sync_tree(baseline_path, baseline_organized, True, [".git/"], ctx.run_command)
    sync_tree(preview_path, selected_organized, True, [".git/"], ctx.run_command)

    organizer.split_live_heaps_to_git(baseline_organized, baseline_organized, options=options)
    organizer.split_live_heaps_to_git(selected_organized, selected_organized, options=options)
    restore_source_organized_view_for_apply_diff(selected_organized, target, ctx)
    restore_preview_paths_from_baseline(selected_organized, baseline_organized, organizer_keep_paths)
    organizer.compose_git_view_to_live(selected_organized, selected_path, options=options)
    return True


def selected_apply_targets_from_preview(resolved_targets, keep_ha_paths, ctx):
    selected_root = ctx.work_dir / "apply-preview-selected"
    clear_tree(selected_root, ctx.work_dir, ctx.run_command)
    selected_targets = []
    keep_by_target = {}
    for path in keep_ha_paths:
        target_id, _, relative = path.partition("/")
        if target_id and relative:
            keep_by_target.setdefault(target_id, []).append(Path(relative))

    for target in resolved_targets:
        target_id = str(target["id"])
        safe_id = safe_preview_name(target_id)
        baseline_path = ctx.work_dir / "apply-preview-baseline" / safe_id
        preview_path = ctx.work_dir / "apply-preview" / safe_id
        if not preview_path.exists():
            raise RuntimeError(f"Raw apply preview for target '{target_id}' is missing; run Preview Git to HA again.")
        selected_path = selected_root / safe_id
        sync_tree(preview_path, selected_path, True, [".git/"], ctx.run_command)
        keep_paths = keep_by_target.get(target_id, [])
        restore_preview_paths_from_baseline(selected_path, baseline_path, keep_paths)
        if target.get("type") == "homeassistant":
            apply_organizer_preview_path_decisions(
                selected_path,
                baseline_path,
                preview_path,
                target,
                keep_paths,
                ctx,
            )
        updated = dict(target)
        updated["source_path"] = str(selected_path)
        if updated.get("type") == "homeassistant":
            updated["allow_protected_storage"] = True
        selected_targets.append(updated)
    return selected_targets


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


def build_apply_preview_from_sources(resolved_targets, ctx, details=None):
    preview_root = ctx.work_dir / "apply-preview"
    baseline_root = ctx.work_dir / "apply-preview-baseline"
    clear_tree(preview_root, ctx.work_dir, ctx.run_command)
    clear_tree(baseline_root, ctx.work_dir, ctx.run_command)
    chunks = []
    deletion_count = 0
    skipped_protected = []
    storage_change_paths = []
    warnings = []
    live_fingerprints = {}

    for target in resolved_targets:
        preview_progress(ctx, details, _("text.preview_target_start", target=target["id"]))
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
            chunks.append(_("preview.skipped_protected_storage", target=target["id"], paths=", ".join(skipped)) + "\n")
        if target["type"] == "homeassistant":
            storage_change_paths.extend(
                [f"{target['id']}/{path}" for path in managed_storage_change_paths(baseline_path, preview_path)]
            )
            for warning in storage_registry_warnings(target["id"], baseline_path, preview_path):
                warnings.append(warning)
                if details is not None:
                    ctx.add_detail(details, warning)
        preview_progress(ctx, details, _("text.preview_target_counting_deletions", target=target["id"]))
        deletion_count += count_preview_deletions(baseline_path, preview_path)
        preview_progress(ctx, details, _("text.preview_target_building_diff", target=target["id"]))
        chunks.append(target_diff_normalized(target, baseline_path, preview_path, ctx))
        preview_progress(ctx, details, _("text.preview_target_done", target=target["id"]))

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = _("preview.no_file_changes")

    return {
        "diff": diff_text,
        "fingerprint": fingerprint_text(diff_text),
        "paths": diff_change_paths(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
        "storage_changes": bool(storage_change_paths),
        "storage_change_paths": sorted(set(storage_change_paths)),
        "warnings": warnings,
        "live_fingerprints": live_fingerprints,
    }


def merge_git_into_ha_live(repo_dir, main_branch, ctx, prefer_local_live=False):
    if git_current_branch(repo_dir, ctx) != HA_LIVE_BRANCH:
        ensure_live_branch_available(repo_dir, ctx, prefer_local=prefer_local_live)
    result = ctx.run_command(["git", "merge", "--no-commit", "--no-ff", main_branch], cwd=repo_dir)
    conflicts = []
    if result.returncode != 0:
        conflict_result = ctx.run_command(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
        conflicts = [line.strip() for line in conflict_result.stdout.splitlines() if line.strip()]
        if not conflicts:
            raise RuntimeError(f"git merge {main_branch} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return conflicts


def storage_change_paths_for_repo_paths(paths, resolved_targets, repo_dir):
    repo_dir = Path(repo_dir)
    storage_paths = []
    for target in resolved_targets:
        if target.get("type") != "homeassistant":
            continue
        try:
            source_relative = Path(target["source_path"]).relative_to(repo_dir)
        except ValueError:
            source_relative = Path(target.get("source") or Path(target["source_path"]).name)
        storage_prefix = source_relative / ".storage"
        for path in paths:
            relative = Path(path)
            try:
                storage_relative = relative.relative_to(storage_prefix)
            except ValueError:
                continue
            if len(storage_relative.parts) == 1:
                storage_paths.append(f"{target['id']}/.storage/{storage_relative.as_posix()}")
    return sorted(set(storage_paths))


def conflict_storage_change_paths(conflicts, resolved_targets, repo_dir):
    return storage_change_paths_for_repo_paths(conflicts, resolved_targets, repo_dir)


def merge_storage_change_paths(resolved_targets, repo_dir, ctx):
    return storage_change_paths_for_repo_paths(merge_change_paths(repo_dir, ctx), resolved_targets, repo_dir)


def apply_live_path_for_repo_path(resolved_targets, repo_dir, path):
    repo_dir = Path(repo_dir)
    safe_path = Path(path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        raise RuntimeError("Invalid apply preview path")
    for target in resolved_targets:
        try:
            source_relative = Path(target["source_path"]).relative_to(repo_dir)
        except ValueError:
            source_relative = Path(target.get("source") or Path(target["source_path"]).name)
        try:
            live_relative = safe_path.relative_to(source_relative)
        except ValueError:
            continue
        live_path = Path(target["live_path"]) / live_relative
        return live_path, f"{target['id']}/{live_relative.as_posix()}"
    return None, None


def clean_apply_git_delete_paths(resolved_targets, repo_dir, main_branch, conflicts, ctx):
    repo_dir = Path(repo_dir)
    conflict_paths = set(conflicts or [])
    delete_paths = []
    for raw_path in merge_change_paths(repo_dir, ctx):
        safe_path = Path(raw_path)
        if safe_path.is_absolute() or ".." in safe_path.parts:
            raise RuntimeError("Invalid apply preview path")
        path_text = safe_path.as_posix()
        if path_text in conflict_paths:
            continue
        if git_ref_path_exists(repo_dir, main_branch, path_text, ctx):
            continue
        worktree_path = repo_dir / safe_path
        if worktree_path.exists() or worktree_path.is_symlink():
            continue
        live_path, _display = apply_live_path_for_repo_path(resolved_targets, repo_dir, safe_path)
        if live_path is not None:
            delete_paths.append(path_text)
    return sorted(set(delete_paths))


def apply_conflict_git_delete_paths(resolved_targets, repo_dir, main_branch, conflicts, ctx):
    repo_dir = Path(repo_dir)
    delete_paths = []
    for raw_path in conflicts or []:
        safe_path = Path(raw_path)
        if safe_path.is_absolute() or ".." in safe_path.parts:
            raise RuntimeError("Invalid apply preview path")
        path_text = safe_path.as_posix()
        if git_ref_path_exists(repo_dir, main_branch, path_text, ctx):
            continue
        live_path, _display = apply_live_path_for_repo_path(resolved_targets, repo_dir, safe_path)
        if live_path is not None:
            delete_paths.append(path_text)
    return sorted(set(delete_paths))


def delete_apply_conflict_live_deletions(
    resolved_targets,
    repo_dir,
    main_branch,
    resolutions,
    details,
    ctx,
    clean_git_delete_paths=None,
):
    deleted = []
    repo_dir = Path(repo_dir)
    delete_paths = set(clean_git_delete_paths or [])
    for path, choice in sorted((resolutions or {}).items()):
        safe_path = Path(path)
        if safe_path.is_absolute() or ".." in safe_path.parts:
            raise RuntimeError("Invalid apply preview path")
        path_text = safe_path.as_posix()
        if choice == "git" and not git_ref_path_exists(repo_dir, main_branch, path_text, ctx):
            delete_paths.add(path_text)

    for path_text in sorted(delete_paths):
        live_path, display_path = apply_live_path_for_repo_path(resolved_targets, repo_dir, path_text)
        if live_path is None:
            continue
        if live_path.exists() or live_path.is_symlink():
            safe_remove_path(live_path)
            deleted.append(display_path)
    if deleted and details is not None:
        ctx.add_detail(details, f"Removed {len(deleted)} live file(s) from Git deletions.")
    return deleted


def commit_apply_merge(repo_dir, main_branch, resolved_targets, keep_ha_paths, message, details, ctx, sync_applied_storage=False):
    conflicts = merge_git_into_ha_live(repo_dir, main_branch, ctx, prefer_local_live=True)
    keep_ha = set(keep_ha_paths or [])
    if conflicts:
        for path in conflicts:
            stage = 2 if path in keep_ha else 3
            git_resolve_conflict_path(repo_dir, path, stage, ctx)
        if details is not None:
            ctx.add_detail(details, f"Resolved {len(conflicts)} Apply merge conflict(s) from preview decisions.")

    for path in sorted(keep_ha):
        if path in conflicts:
            continue
        git_restore_path_from_ref(repo_dir, "HEAD", path, ctx)

    merge_apply_normalized_storage_metadata_into_repo_worktree(repo_dir, resolved_targets, ctx)
    if sync_applied_storage:
        sync_applied_normalized_storage_to_repo_worktree(repo_dir, resolved_targets, ctx)
    stage_managed_save_worktree(repo_dir, resolved_targets, ctx)
    commit = git_commit_if_needed(repo_dir, message, ctx)
    base = update_base_branch(repo_dir, main_branch, ctx)
    if base and details is not None:
        ctx.add_detail(details, f"Updated {HA_BASE_BRANCH} at {base}.")
    return commit


def build_apply_preview(resolved_targets, ctx, details=None, repo_dir=None, main_branch="main", prefer_local_live=False):
    if repo_dir is None:
        return build_apply_preview_from_sources(resolved_targets, ctx, details)

    repo_dir = Path(repo_dir)
    original_branch = git_current_branch(repo_dir, ctx) or main_branch
    try:
        git_ensure_head(repo_dir, ctx, details)
        update_ha_live_branch(resolved_targets, repo_dir, details, ctx, prefer_local_live=prefer_local_live)
        conflicts = merge_git_into_ha_live(repo_dir, main_branch, ctx)
        update_base_branch(repo_dir, main_branch, ctx)
        if conflicts:
            paths = sorted(set(conflicts) | set(merge_change_paths(repo_dir, ctx)))
            summary = "\n".join(
                [_("preview.apply_conflicts_title", count=len(conflicts)), *[_("preview.conflict_item", path=path) for path in conflicts]]
            )
            diff = merge_diff_normalized(repo_dir, resolved_targets, ctx)
            full_diff = "\n\n".join([part for part in [summary, diff] if part])
            fingerprint = merge_conflict_fingerprint(conflicts, repo_dir, diff, ctx)
            storage_change_paths = merge_storage_change_paths(resolved_targets, repo_dir, ctx)
            clean_git_delete_paths = clean_apply_git_delete_paths(resolved_targets, repo_dir, main_branch, conflicts, ctx)
            conflict_git_delete_paths = apply_conflict_git_delete_paths(resolved_targets, repo_dir, main_branch, conflicts, ctx)
            return {
                "diff": full_diff,
                "fingerprint": fingerprint,
                "paths": paths,
                "deletions": len(clean_git_delete_paths),
                "clean_git_delete_paths": clean_git_delete_paths,
                "conflict_git_delete_paths": conflict_git_delete_paths,
                "skipped_protected": [],
                "storage_changes": bool(storage_change_paths),
                "storage_change_paths": storage_change_paths,
                "warnings": [],
                "live_fingerprints": {"ha-ops/ha-live-conflicts": fingerprint},
                "conflicts": conflicts,
            }
        git_checkout(repo_dir, main_branch, ctx)
        return build_apply_preview_from_sources(resolved_targets, ctx, details)
    finally:
        git_abort_merge(repo_dir, ctx)
        git_reset_hard(repo_dir, ctx)
        git_checkout(repo_dir, original_branch, ctx)
