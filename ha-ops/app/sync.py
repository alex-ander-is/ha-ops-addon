import fnmatch
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import storage_managed
import targets as target_model


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


def apply_homeassistant_config(src, dest, target, ctx, details=None):
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
    if skipped_protected and details is not None:
        ctx.add_detail(details, f"Skipped protected .storage file(s): {', '.join(skipped_protected)}.")
    return skipped_protected


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


def file_differs(src_path, dest_path):
    if not dest_path.exists() or not dest_path.is_file():
        return True
    try:
        return src_path.read_bytes() != dest_path.read_bytes()
    except OSError:
        return True


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

    if storage_managed.source_has_managed_projection(src):
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


def save_preview_status_lines(repo_dir, preview_repo):
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
        elif before[path].read_bytes() != after[path].read_bytes():
            lines.append(f"- Modified: {path}")
    return lines


def save_preview_diff(repo_dir, preview_repo, run_command):
    result = run_command(["diff", "-ruN", "-x", ".git", str(repo_dir), str(preview_repo)])
    if result.returncode == 0:
        return "No Save changes."
    if result.returncode == 1:
        return truncate_diff(result.stdout.strip())
    raise RuntimeError(f"Save preview diff failed:\n{result.stderr.strip() or result.stdout.strip()}")


def build_save_preview(resolved_targets, repo_dir, details, ctx):
    export_root = build_save_export(resolved_targets, details, ctx)
    preview_repo = ctx.work_dir / "save-to-git-preview"
    clear_tree(preview_repo, ctx.work_dir, ctx.run_command)
    sync_tree(repo_dir, preview_repo, True, [".git/"], ctx.run_command)

    preview_targets = [repo_relative_target(target, repo_dir, preview_repo) for target in resolved_targets]
    apply_save_export(preview_targets, export_root, details, ctx)

    status_lines = save_preview_status_lines(repo_dir, preview_repo)
    summary = "\n".join([f"Save preview changes ({len(status_lines)}):", *status_lines]) if status_lines else "No Save changes."
    return {"summary": summary, "diff": save_preview_diff(repo_dir, preview_repo, ctx.run_command)}


def export_target_to_path(target, dest, ctx):
    live_path = Path(target["live_path"])
    if target["type"] == "homeassistant":
        export_homeassistant_config(live_path, dest, target, ctx)
        return
    clean_export_destination(
        dest,
        ctx.clean_paths,
        ctx.clean_dir_names,
        ctx.clean_file_patterns,
    )
    export_tree(live_path, dest, target_model.save_delete(target), ctx.export_excludes, ctx.run_command)


def save_unknown_base_conflicts(resolved_targets, repo_dir, resolutions, details, ctx):
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
            if file_differs(exported_path, source_file):
                conflicts.append(repo_relative)

    if conflicts:
        ctx.add_detail(details, f"Found {len(conflicts)} unknown-base Save conflict(s).")
    return conflicts


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


def sync_to_preview(target, preview_path, ctx):
    source_path = Path(target["source_path"])
    live_path = Path(target["live_path"])
    clear_tree(preview_path, ctx.work_dir, ctx.run_command)
    if live_path.exists():
        sync_tree(live_path, preview_path, True, None, ctx.run_command)

    if target["type"] == "homeassistant":
        if source_path.exists() and has_managed_content(source_path):
            reject_source_symlinks(target, ctx)
            skipped_protected = apply_homeassistant_config(source_path, preview_path, target, ctx)
        else:
            skipped_protected = []
    else:
        if source_path.exists() and has_managed_content(source_path):
            reject_source_symlinks(target, ctx)
            sync_tree(source_path, preview_path, target_model.apply_delete(target), ctx.export_excludes, ctx.run_command)
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


def build_apply_preview(resolved_targets, ctx):
    preview_root = ctx.work_dir / "apply-preview"
    clear_tree(preview_root, ctx.work_dir, ctx.run_command)
    chunks = []
    deletion_count = 0
    skipped_protected = []

    for target in resolved_targets:
        preview_path = preview_root / safe_preview_name(str(target["id"]))
        skipped = sync_to_preview(target, preview_path, ctx)
        if skipped:
            skipped_protected.extend(skipped)
            chunks.append(f"Target {target['id']}: skipped protected .storage file(s): {', '.join(skipped)}.\n")
        deletion_count += count_preview_deletions(target, preview_path)
        chunks.append(target_diff(target, preview_path, ctx.run_command))

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = "No file changes."

    return {
        "diff": truncate_diff(diff_text),
        "fingerprint": fingerprint_text(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
    }
