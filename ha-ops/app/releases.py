import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import policies
import sync as sync_logic
import targets as target_model


@dataclass(frozen=True)
class ReleaseContext:
    add_detail: Callable[..., Any]
    addon_action: Callable[..., Any]
    clear_tree: Callable[..., Any]
    core_reload_lovelace: Callable[[], Any]
    core_reload_themes: Callable[[], Any]
    core_reload_yaml: Callable[[], Any]
    core_restart: Callable[[], Any]
    core_start: Callable[[], Any]
    core_stop: Callable[[], Any]
    export_homeassistant_config: Callable[..., Any]
    export_tree: Callable[..., Any]
    load_json: Callable[..., Any]
    option_int: Callable[..., Any]
    parse_backup_date: Callable[..., Any]
    release_now: Callable[[], str]
    releases_dir: Path
    restart_or_start_addon: Callable[..., Any]
    restore_homeassistant_config: Callable[..., Any]
    safe_remove_path: Callable[..., Any]
    stop_addon_for_sync: Callable[..., Any]
    sync_deps: Callable[[], Any]
    sync_tree: Callable[..., Any]
    utc_now: Callable[[], str]


def safe_release_dir(release_name, ctx):
    if not release_name or Path(release_name).name != release_name:
        raise RuntimeError("Invalid release name")
    release_dir = (ctx.releases_dir / release_name).resolve()
    releases_root = ctx.releases_dir.resolve()
    if release_dir.parent != releases_root:
        raise RuntimeError("Invalid release name")
    return release_dir


def list_releases(ctx):
    releases = []
    if not ctx.releases_dir.exists():
        return releases

    for path in sorted(ctx.releases_dir.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        metadata_path = path / "release.json"
        metadata = ctx.load_json(metadata_path, {})
        releases.append(
            {
                "name": path.name,
                "created_at": metadata.get("created_at"),
                "commit": metadata.get("commit"),
                "backup_slug": metadata.get("backup_slug"),
                "targets": metadata.get("targets", []),
            }
        )
    return releases


def create_release_snapshot(resolved_targets, commit, backup_slug, ctx):
    release_name = ctx.release_now()
    release_dir = ctx.releases_dir / release_name
    sync_logic.ensure_dir(release_dir)

    metadata = {
        "created_at": ctx.utc_now(),
        "commit": commit,
        "backup_slug": backup_slug,
        "targets": [],
    }

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        target_snapshot = release_dir / target["id"]
        sync_logic.ensure_dir(target_snapshot)

        existed = live_path.exists()
        if existed:
            if target.get("type") == "homeassistant":
                ctx.export_homeassistant_config(live_path, target_snapshot, target)
            else:
                ctx.export_tree(live_path, target_snapshot, delete=True)

        metadata["targets"].append(
            {
                "id": target["id"],
                "type": target["type"],
                "resolved_slug": target.get("resolved_slug"),
                "live_path": target["live_path"],
                "source_path": target["source_path"],
                "delete": target_model.restore_delete(target),
                "restart_after_sync": bool(target.get("restart_after_sync", True)),
                "reload_yaml_after_rollback": bool(target.get("reload_yaml_after_rollback", False)),
                "restart_core_after_rollback": bool(target.get("restart_core_after_rollback", False)),
                "stop_core_before_storage_rollback": bool(target.get("stop_core_before_storage_rollback", True)),
                "start_core_after_storage_rollback": bool(target.get("start_core_after_storage_rollback", True)),
                "stop_addon_before_sync": bool(target.get("stop_addon_before_sync", False)),
                "stop_core_before_sync_if_storage": bool(target.get("stop_core_before_sync_if_storage", False)),
                "existed": existed,
            }
        )

    (release_dir / "release.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    return release_name


def release_created_at(path, ctx):
    metadata = ctx.load_json(path / "release.json", {})
    created_at = ctx.parse_backup_date(metadata.get("created_at"))
    if created_at:
        return created_at
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def prune_release_snapshots(options, protected_release, ctx):
    if not ctx.releases_dir.exists():
        return []

    keep_count = ctx.option_int(options, "release_snapshot_keep_count", policies.DEFAULT_RELEASE_KEEP_COUNT, minimum=0)
    keep_days = ctx.option_int(options, "release_snapshot_keep_days", policies.DEFAULT_RELEASE_KEEP_DAYS, minimum=0)
    now = datetime.now(timezone.utc)
    releases = []
    for path in ctx.releases_dir.iterdir():
        if not path.is_dir() or path.name == protected_release:
            continue
        releases.append((path, release_created_at(path, ctx)))

    to_delete = set()
    if keep_days:
        for path, created_at in releases:
            age_days = (now - created_at.astimezone(timezone.utc)).total_seconds() / 86400
            if age_days > keep_days:
                to_delete.add(path)

    remaining = sorted(
        [(path, created_at) for path, created_at in releases if path not in to_delete],
        key=lambda item: item[1],
        reverse=True,
    )
    protected_slots = 1 if protected_release else 0
    remaining_keep_count = max(0, keep_count - protected_slots)
    if len(remaining) > remaining_keep_count:
        for path, _created_at in remaining[remaining_keep_count:]:
            to_delete.add(path)

    removed = []
    for path in sorted(to_delete, key=lambda item: item.name):
        safe_dir = safe_release_dir(path.name, ctx)
        ctx.safe_remove_path(safe_dir)
        removed.append(path.name)
    return removed


def restore_release_snapshot(release_name, details, core_already_stopped, ctx):
    release_dir = safe_release_dir(release_name, ctx)
    metadata_path = release_dir / "release.json"
    if not metadata_path.exists():
        raise RuntimeError(f"Release metadata not found for {release_name}")

    metadata = ctx.load_json(metadata_path, {})
    targets = metadata.get("targets", [])
    core_stopped = core_already_stopped
    homeassistant_seen = False
    homeassistant_should_restart = False
    homeassistant_should_start = False
    homeassistant_should_reload = False
    homeassistant_should_reload_lovelace = False
    homeassistant_should_reload_themes = False

    for target in targets:
        live_path = Path(target["live_path"])
        snapshot_path = release_dir / target["id"]
        target_type = target.get("type")
        addon_was_started = False

        if target_type == "homeassistant":
            homeassistant_seen = True
            homeassistant_changes = sync_logic.homeassistant_change_set(
                snapshot_path,
                live_path,
                target,
                ctx.sync_deps(),
                mode="rollback",
            )
            should_stop_for_storage = bool(
                homeassistant_changes.changed_storage and target_model.stop_core_before_storage_rollback(target)
            )
            if should_stop_for_storage and not core_stopped:
                ctx.add_detail(details, f"Stopping Home Assistant Core for rollback of release {release_name}.")
                ctx.core_stop()
                core_stopped = True
            elif homeassistant_changes.changed_storage:
                ctx.add_detail(details, "Warning: .storage will be restored while Home Assistant Core is running.")
            if target_model.restart_core_after_rollback(target):
                homeassistant_should_restart = True
            elif homeassistant_changes.changed_yaml and target_model.reload_yaml_after_rollback(target):
                homeassistant_should_reload = True
            if homeassistant_changes.changed_lovelace_resource_storage:
                homeassistant_should_reload_lovelace = True
            if homeassistant_changes.changed_themes:
                homeassistant_should_reload_themes = True
            if core_stopped and target_model.start_core_after_storage_rollback(target):
                homeassistant_should_start = True
        elif target_type == "addon" and target.get("stop_addon_before_sync", False):
            slug = target.get("resolved_slug")
            ctx.add_detail(details, f"Stopping App {slug} before rollback sync.")
            addon_was_started = ctx.stop_addon_for_sync(slug)

        if target.get("existed", True):
            ctx.add_detail(details, f"Restoring {target['id']} from release {release_name}.")
            if target_type == "homeassistant":
                ctx.restore_homeassistant_config(snapshot_path, live_path, target)
            else:
                ctx.sync_tree(
                    snapshot_path,
                    live_path,
                    delete=bool(target.get("delete", True)),
                    excludes=policies.EXPORT_EXCLUDES,
                )
        else:
            ctx.add_detail(details, f"Clearing {target['id']} because it did not exist in release {release_name}.")
            ctx.clear_tree(live_path)

        if target_type == "addon" and target.get("restart_after_sync", True):
            slug = target.get("resolved_slug")
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    ctx.add_detail(details, f"Starting App {slug} after rollback.")
                    ctx.addon_action(slug, "start")
            else:
                ctx.add_detail(details, f"Restarting App {slug} after rollback.")
                ctx.restart_or_start_addon(slug)

    if homeassistant_seen and homeassistant_should_start:
        ctx.add_detail(details, "Starting Home Assistant Core after rollback.")
        ctx.core_start()
    elif homeassistant_seen and core_stopped:
        ctx.add_detail(details, "Home Assistant Core was left stopped after .storage rollback by policy.")
    elif homeassistant_seen and homeassistant_should_restart:
        ctx.add_detail(details, "Restarting Home Assistant Core after rollback.")
        ctx.core_restart()
    elif homeassistant_seen:
        core_restarted = False
        if homeassistant_should_reload_lovelace:
            try:
                ctx.add_detail(details, "Reloading Lovelace resources after rollback.")
                ctx.core_reload_lovelace()
            except Exception as exc:
                ctx.add_detail(details, f"Lovelace reload failed after rollback; restarting Home Assistant Core instead: {exc}")
                ctx.add_detail(details, "Restarting Home Assistant Core after rollback.")
                ctx.core_restart()
                core_restarted = True
        if not core_restarted and homeassistant_should_reload_themes:
            ctx.add_detail(details, "Reloading Home Assistant themes after rollback.")
            ctx.core_reload_themes()
        if not core_restarted and homeassistant_should_reload:
            ctx.add_detail(details, "Reloading Home Assistant YAML config after rollback.")
            ctx.core_reload_yaml()

    return metadata
