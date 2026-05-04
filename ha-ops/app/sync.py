import fnmatch
import hashlib
import shutil
from pathlib import Path

import targets as target_model


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


def clean_export_destination(dest, clean_paths, clean_dir_names, clean_file_patterns):
    ensure_dir(dest)
    removed = set()

    for pattern in clean_paths:
        matches = list(dest.glob(pattern)) if any(char in pattern for char in "*?[") else [dest / pattern]
        for path in matches:
            if path.exists() or path.is_symlink():
                safe_remove_path(path)
                removed.add(str(path.relative_to(dest)))

    for path in list(dest.rglob("*")):
        relative = str(path.relative_to(dest))
        if path.is_dir() and path.name in clean_dir_names:
            safe_remove_path(path)
            removed.add(relative)
            continue
        if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in clean_file_patterns):
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


def export_homeassistant_config(src, dest, target, deps):
    clear_tree(dest, deps["work_dir"], deps["run_command"])
    copied = 0

    for pattern in deps["ha_root_patterns"]:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in deps["ha_root_excludes"]:
                continue
            copy_export_path(src_path, dest / src_path.name, deps["export_excludes"], deps["run_command"])
            copied += 1

    for name in deps["ha_dirs"]:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name, deps["export_excludes"], deps["run_command"])
        copied += 1

    zigbee2mqtt_count = 0
    if target and target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = copy_homeassistant_path_allowlist(
            src,
            dest,
            deps["zigbee2mqtt_paths"],
            deps["export_excludes"],
            deps["run_command"],
        )
    storage_count = export_storage_allowlist(src, dest, deps["storage_allowlist"])
    return copied, zigbee2mqtt_count, storage_count


def apply_homeassistant_config(src, dest, target, deps, details=None):
    if not src.exists() or not has_managed_content(src):
        if details is not None:
            deps["add_detail"](details, f"Skipping {target['id']} because Git has no Home Assistant config yet.")
        return []

    copied = 0
    for pattern in deps["ha_root_patterns"]:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in deps["ha_root_excludes"]:
                continue
            dest_path = dest / src_path.name
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
            copied += 1

    for name in deps["ha_dirs"]:
        src_path = src / name
        if not src_path.exists():
            continue
        sync_homeassistant_path_allowlist(src, dest, [name], deps["export_excludes"], deps["run_command"])
        copied += 1

    zigbee2mqtt_count = 0
    if target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = sync_homeassistant_path_allowlist(
            src,
            dest,
            deps["zigbee2mqtt_paths"],
            deps["export_excludes"],
            deps["run_command"],
        )
    copied_count, skipped_protected = sync_storage_allowlist(
        src,
        dest,
        deps["storage_allowlist"],
        deps["protected_storage_files"],
        allow_protected=target_model.allow_protected_storage(target),
    )
    if copied and details is not None:
        deps["add_detail"](details, f"Applied {copied} Home Assistant config path(s).")
    if zigbee2mqtt_count and details is not None:
        deps["add_detail"](details, f"Applied {zigbee2mqtt_count} Zigbee2MQTT config path(s).")
    if copied_count and details is not None:
        deps["add_detail"](details, f"Applied {copied_count} allowlisted .storage config file(s).")
    if skipped_protected and details is not None:
        deps["add_detail"](details, f"Skipped protected .storage file(s): {', '.join(skipped_protected)}.")
    return skipped_protected


def sync_homeassistant_path_allowlist(src, dest, paths, export_excludes, run_command):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        if src_path.is_dir():
            sync_tree(src_path, dest_path, True, export_excludes, run_command)
        else:
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


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
        shutil.copy2(src_path, dest_path)
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


def apply_targets(resolved_targets, details, deps):
    homeassistant_target = None
    core_stopped = False

    for target in resolved_targets:
        source_path = Path(target["source_path"])
        live_path = Path(target["live_path"])
        addon_was_started = False

        if target["type"] == "homeassistant":
            homeassistant_target = target
            allow_protected_storage = target_model.allow_protected_storage(target)
            if target.get("stop_core_before_sync_if_storage", False) and source_has_applicable_storage(
                source_path,
                deps["storage_allowlist"],
                deps["protected_storage_files"],
                allow_protected_storage,
            ) and not core_stopped:
                deps["add_detail"](details, "Stopping Home Assistant Core before syncing .storage.")
                deps["core_stop"]()
                core_stopped = True
        elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
            slug = target["resolved_slug"]
            deps["add_detail"](details, f"Stopping add-on {slug} before sync.")
            addon_was_started = deps["stop_addon_for_sync"](slug)

        deps["add_detail"](details, f"Syncing {target['id']} from {source_path} to {live_path}.")
        if target["type"] == "homeassistant":
            apply_homeassistant_config(source_path, live_path, target, deps, details)
        else:
            if not source_path.exists() or not has_managed_content(source_path):
                deps["add_detail"](details, f"Skipping {target['id']} because Git has no config for this add-on yet.")
                continue
            sync_tree(source_path, live_path, target_model.apply_delete(target), None, deps["run_command"])

        if target["type"] == "addon" and target.get("restart_after_sync", True):
            slug = target["resolved_slug"]
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    deps["add_detail"](details, f"Starting add-on {slug} after sync.")
                    deps["addon_action"](slug, "start")
            else:
                deps["add_detail"](details, f"Restarting add-on {slug}.")
                deps["restart_or_start_addon"](slug)

    if homeassistant_target is None:
        return

    deps["add_detail"](details, "Running Home Assistant config check.")
    deps["do_core_check"]()

    if core_stopped:
        if homeassistant_target.get("restart_after_sync", True):
            deps["add_detail"](details, "Starting Home Assistant Core after sync.")
            deps["core_start"]()
    else:
        if homeassistant_target.get("restart_after_sync", True):
            deps["add_detail"](details, "Restarting Home Assistant Core.")
            deps["core_restart"]()


def export_targets(resolved_targets, details, deps):
    for target in resolved_targets:
        live_path = Path(target["live_path"])
        source_path = Path(target["source_path"])
        if not live_path.exists():
            if target.get("optional", False):
                deps["add_detail"](details, f"Skipping optional target {target['id']} because {live_path} does not exist.")
                continue
            raise RuntimeError(f"Live path does not exist for target '{target['id']}': {live_path}")

        if target["type"] == "homeassistant":
            deps["add_detail"](details, f"Saving config-only {target['id']} from {live_path} to {source_path}.")
            copied_count, zigbee2mqtt_count, storage_count = export_homeassistant_config(live_path, source_path, target, deps)
            deps["add_detail"](details, f"Saved {copied_count} Home Assistant config path(s).")
            if zigbee2mqtt_count:
                deps["add_detail"](details, f"Saved {zigbee2mqtt_count} legacy Zigbee2MQTT config path(s).")
            if storage_count:
                deps["add_detail"](details, f"Saved {storage_count} allowlisted .storage config file(s).")
        else:
            deps["add_detail"](details, f"Saving {target['id']} from {live_path} to {source_path}.")
            removed_count = clean_export_destination(
                source_path,
                deps["clean_paths"],
                deps["clean_dir_names"],
                deps["clean_file_patterns"],
            )
            if removed_count:
                deps["add_detail"](details, f"Removed {removed_count} excluded item(s) from {target['id']} save destination.")
            export_tree(live_path, source_path, target_model.save_delete(target), deps["export_excludes"], deps["run_command"])


def sync_to_preview(target, preview_path, deps):
    source_path = Path(target["source_path"])
    live_path = Path(target["live_path"])
    clear_tree(preview_path, deps["work_dir"], deps["run_command"])
    if live_path.exists():
        sync_tree(live_path, preview_path, True, None, deps["run_command"])

    if target["type"] == "homeassistant":
        if source_path.exists() and has_managed_content(source_path):
            skipped_protected = apply_homeassistant_config(source_path, preview_path, target, deps)
        else:
            skipped_protected = []
    else:
        if source_path.exists() and has_managed_content(source_path):
            sync_tree(source_path, preview_path, target_model.apply_delete(target), None, deps["run_command"])
        skipped_protected = []
    return skipped_protected


def target_diff(target, preview_path, run_command):
    live_path = Path(target["live_path"])
    if not live_path.exists():
        return f"Target {target['id']} live path does not exist: {live_path}\n"

    result = run_command(["diff", "-ruN", str(live_path), str(preview_path)])
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Diff failed for {target['id']}:\n{result.stderr.strip()}")
    if not result.stdout.strip():
        return f"Target {target['id']}: no file changes.\n"
    return f"## {target['id']}\n{result.stdout.strip()}\n"


def count_preview_deletions(target, preview_path):
    live_path = Path(target["live_path"])
    if not live_path.exists():
        return 0

    deleted = 0
    for path in live_path.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(live_path)
        if not (preview_path / relative).exists():
            deleted += 1
    return deleted


def safe_preview_name(value):
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value) or "target"


def fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_diff(diff_text):
    max_chars = 60000
    if len(diff_text) > max_chars:
        return diff_text[:max_chars] + "\n\n[Diff truncated. Use git or shell for full output.]"
    return diff_text


def build_apply_preview(resolved_targets, deps):
    preview_root = deps["work_dir"] / "apply-preview"
    clear_tree(preview_root, deps["work_dir"], deps["run_command"])
    chunks = []
    deletion_count = 0
    skipped_protected = []

    for target in resolved_targets:
        preview_path = preview_root / safe_preview_name(str(target["id"]))
        skipped = sync_to_preview(target, preview_path, deps)
        if skipped:
            skipped_protected.extend(skipped)
            chunks.append(f"Target {target['id']}: skipped protected .storage file(s): {', '.join(skipped)}.\n")
        deletion_count += count_preview_deletions(target, preview_path)
        chunks.append(target_diff(target, preview_path, deps["run_command"]))

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = "No file changes."

    return {
        "diff": truncate_diff(diff_text),
        "fingerprint": fingerprint_text(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
    }
