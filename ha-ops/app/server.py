from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import fnmatch
import hashlib
import html
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import state as state_store
import supervisor
import ui
import backups as backup_policy
import git_ops
import manifest as manifest_logic
import targets as target_model


HOST = "0.0.0.0"
PORT = 8099
ADDON_CONFIG_PATH = Path("/app/config.yaml")
OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/state.json")
RELEASES_DIR = Path("/data/releases")
DATA_DIR = Path("/data")
CONFIG_DIR = Path("/homeassistant")
ADDON_CONFIGS_DIR = Path("/addon_configs")
WORK_DIR = Path("/data/work")
GENERATED_DEPLOY_KEY_PATH = WORK_DIR / "generated_deploy_key"
GENERATED_DEPLOY_KEY_PUB_PATH = WORK_DIR / "generated_deploy_key.pub"
EXPORT_EXCLUDES = [
    ".cloud/",
    ".cache/",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage/",
    ".vscode/",
    "__pycache__/",
    "backups/",
    "deps/",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    "*.pyc",
    "*.pyo",
    ".tmp-*",
    "node_modules",
    "node_modules/",
    "tts/",
    "www/community/",
    "www/media/",
    "www/tmp/",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]
STORAGE_EXPORT_ALLOWLIST = [
    "core.area_registry",
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.floor_registry",
    "core.label_registry",
    "core.logger",
    "core.uuid",
    "counter",
    "energy",
    "frontend_theme",
    "homeassistant.exposed_entities",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "lovelace",
    "lovelace.lovelace",
    "lovelace.map",
    "lovelace_dashboards",
    "lovelace_resources",
    "person",
    "schedule",
    "scene",
    "script",
    "tag",
    "timer",
    "zone",
]
PROTECTED_STORAGE_FILES = {
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.uuid",
    "person",
}
DEFAULT_BACKUP_MAX_AGE_HOURS = 24
DEFAULT_MAX_APPLY_DELETIONS = 25
DEFAULT_RELEASE_KEEP_COUNT = 5
DEFAULT_RELEASE_KEEP_DAYS = 7
HOMEASSISTANT_EXPORT_ROOT_PATTERNS = ["*.yaml", "*.yml"]
HOMEASSISTANT_EXPORT_ROOT_EXCLUDES = {"secrets.yaml"}
HOMEASSISTANT_EXPORT_DIRS = [
    "blueprints",
    "custom_templates",
    "dashboards",
    "packages",
    "templates",
    "themes",
    "ui_lovelace_minimalist",
]
ZIGBEE2MQTT_CONFIG_PATHS = [
    "zigbee2mqtt/configuration.yaml",
    "zigbee2mqtt/external_converters",
    "zigbee2mqtt/scripts",
]
EXPORT_CLEAN_PATHS = [
    ".cloud",
    ".cache",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage",
    ".vscode",
    "backups",
    "deps",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    ".tmp-*",
    "node_modules",
    "tts",
    "www/community",
    "www/media",
    "www/tmp",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]
EXPORT_CLEAN_DIR_NAMES = {"__pycache__", "node_modules"}
EXPORT_CLEAN_FILE_PATTERNS = ["*.pyc", "*.pyo"]

RUN_LOCK = threading.Lock()


def utc_now():
    return state_store.utc_now()


def release_now():
    return state_store.release_now()


def load_json(path, default):
    return state_store.load_json(path, default)


def load_options():
    return state_store.load_options(OPTIONS_PATH)


def option_bool(options, name, default):
    return state_store.option_bool(options, name, default)


def option_int(options, name, default, minimum=0):
    return state_store.option_int(options, name, default, minimum)


def addon_version():
    if not ADDON_CONFIG_PATH.exists():
        return "unknown"
    for line in ADDON_CONFIG_PATH.read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return "unknown"


def default_state():
    return state_store.default_state()


def read_state():
    return state_store.read_state(STATE_PATH)


def write_state(updates):
    return state_store.write_state(STATE_PATH, updates)


def list_releases():
    releases = []
    if not RELEASES_DIR.exists():
        return releases

    for path in sorted(RELEASES_DIR.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        metadata_path = path / "release.json"
        metadata = load_json(metadata_path, {})
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


def generated_deploy_key_exists():
    return GENERATED_DEPLOY_KEY_PATH.exists() and GENERATED_DEPLOY_KEY_PUB_PATH.exists()


def load_generated_public_key():
    if not GENERATED_DEPLOY_KEY_PUB_PATH.exists():
        return ""
    return GENERATED_DEPLOY_KEY_PUB_PATH.read_text().strip()


def git_auth_mode(options):
    if options.get("git_ssh_key", "").strip():
        return "manual"
    if generated_deploy_key_exists():
        return "generated"
    return "none"


def setup_git_ssh_env(env, key_text=None, key_path=None):
    if key_text:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        key_path = WORK_DIR / "manual_deploy_key"
        key_path.write_text(key_text)
        os.chmod(key_path, 0o600)

    if key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"


def git_env(options):
    env = os.environ.copy()
    git_ssh_key = options.get("git_ssh_key", "").strip()
    if git_ssh_key:
        setup_git_ssh_env(env, key_text=git_ssh_key)
    elif generated_deploy_key_exists():
        setup_git_ssh_env(env, key_path=GENERATED_DEPLOY_KEY_PATH)
    return env


def generate_deploy_key():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    comment = f"ha-ops@{socket.gethostname()}"
    temp_key_path = WORK_DIR / "generated_deploy_key.new"
    temp_pub_path = WORK_DIR / "generated_deploy_key.new.pub"
    for path in [temp_key_path, temp_pub_path]:
        if path.exists():
            path.unlink()

    try:
        result = run_command(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(temp_key_path),
            ]
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ssh-keygen is not available inside the add-on image") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed:\n{result.stderr.strip() or result.stdout.strip()}")

    temp_key_path.replace(GENERATED_DEPLOY_KEY_PATH)
    temp_pub_path.replace(GENERATED_DEPLOY_KEY_PUB_PATH)
    os.chmod(GENERATED_DEPLOY_KEY_PATH, 0o600)
    public_key = load_generated_public_key()
    log(f"Generated deploy key with comment {comment}")
    return public_key


def run_command(command, env=None, cwd=None):
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def log(message):
    print(f"[ha-ops] {message}", flush=True)


def add_detail(details, message):
    details.append(message)
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_message": message,
            "last_details": details,
        }
    )


def call_supervisor(method, path, payload=None):
    return supervisor.call_supervisor(method, path, payload, run_command)


def supervisor_ok(payload):
    return supervisor.supervisor_ok(payload)


def get_installed_addons():
    return supervisor.get_installed_addons(call_supervisor)


def get_addon_info(slug):
    return supervisor.get_addon_info(slug, call_supervisor)


def addon_action(slug, action):
    return supervisor.addon_action(slug, action, call_supervisor)


def core_stop():
    return supervisor.core_stop(call_supervisor)


def core_start():
    return supervisor.core_start(call_supervisor)


def core_restart():
    return supervisor.core_restart(call_supervisor)


def do_core_check():
    return supervisor.do_core_check(call_supervisor)


def backup_mount_info():
    return supervisor.backup_mount_info(call_supervisor)


def default_backup_mount():
    return supervisor.default_backup_mount(backup_mount_info)


def create_ha_backup(name_prefix, backup_location=None):
    return supervisor.create_ha_backup(name_prefix, backup_location, call_supervisor, release_now)


def backup_manager_info():
    return supervisor.backup_manager_info(call_supervisor)


def parse_backup_date(value):
    return backup_policy.parse_backup_date(value)


def backup_slug(backup):
    return backup_policy.backup_slug(backup)


def backup_name(backup):
    return backup_policy.backup_name(backup)


def backup_locations(backup):
    return backup_policy.backup_locations(backup)


def backup_has_location(backup):
    return backup_policy.backup_has_location(backup)


def is_system_backup(backup):
    return backup_policy.is_system_backup(backup)


def backup_age_hours(backup_date):
    return backup_policy.backup_age_hours(backup_date)


def backup_age_seconds(backup_date):
    return backup_policy.backup_age_seconds(backup_date)


def backup_status_message(backup, backup_date):
    return backup_policy.backup_status_message(backup, backup_date)


def find_backup_by_slug(backups, slug):
    return backup_policy.find_backup_by_slug(backups, slug)


def latest_system_backup_status(options=None):
    options = options or load_options()
    return backup_policy.latest_system_backup_status(
        options,
        DEFAULT_BACKUP_MAX_AGE_HOURS,
        option_int,
        option_bool,
        backup_manager_info,
    )


def ensure_fresh_system_backup(options, details):
    return backup_policy.ensure_fresh_system_backup(
        options,
        details,
        option_bool,
        add_detail,
        latest_system_backup_status,
        default_backup_mount,
        create_ha_backup,
        backup_manager_info,
    )


def repo_checkout_path(options):
    return git_ops.repo_checkout_path(options, DATA_DIR)


def ensure_repo(options, reset_to_origin=True):
    return git_ops.ensure_repo(options, DATA_DIR, git_env, run_command, reset_to_origin)


def clean_repo_untracked(repo_dir):
    return git_ops.clean_repo_untracked(repo_dir, run_command)


def git_commit(repo_dir, ref):
    return git_ops.git_commit(repo_dir, ref, run_command)


def git_ref_exists(repo_dir, ref):
    return git_ops.git_ref_exists(repo_dir, ref, run_command)


def git_remote_head(repo_dir, env, branch):
    return git_ops.git_remote_head(repo_dir, env, branch, run_command)


def git_head_or_unborn(repo_dir):
    return git_ops.git_head_or_unborn(repo_dir, run_command)


def git_conflict_paths(repo_dir):
    return git_ops.git_conflict_paths(repo_dir, run_command)


def git_pull_rebase(repo_dir, env, branch):
    return git_ops.git_pull_rebase(repo_dir, env, branch, run_command, lambda conflicts: write_state({"conflicts": conflicts}))


def stage_all(repo_dir):
    return git_ops.stage_all(repo_dir, run_command)


def commit_if_needed(repo_dir, message):
    return git_ops.commit_if_needed(repo_dir, message, run_command, git_status_porcelain)


def push_branch(repo_dir, env, branch):
    return git_ops.push_branch(repo_dir, env, branch, run_command)


def selected_addon_slugs():
    return manifest_logic.selected_addon_slugs(read_state)


def set_selected_addon_slugs(slugs):
    return manifest_logic.set_selected_addon_slugs(slugs, write_state)


def default_homeassistant_manifest(options):
    return manifest_logic.default_homeassistant_manifest(options)


def default_addon_target(slug):
    return manifest_logic.default_addon_target(slug)


def addon_target_slug(target, addons=None):
    return manifest_logic.addon_target_slug(target, addons)


def selected_addon_target(slug, template=None):
    return manifest_logic.selected_addon_target(slug, template)


def manifest_with_selected_addons(manifest, addons=None):
    return manifest_logic.manifest_with_selected_addons(manifest, selected_addon_slugs(), addons)


def default_manifest(options):
    return manifest_logic.default_manifest(options, selected_addon_slugs())


def load_manifest(repo_dir, options, addons=None):
    return manifest_logic.load_manifest(repo_dir, options, selected_addon_slugs(), load_json, addons)


def resolve_addon_slug(target, addons):
    return manifest_logic.resolve_addon_slug(target, addons)


def addon_by_slug(addons, slug):
    return manifest_logic.addon_by_slug(addons, slug)


def path_from_metadata(value):
    return manifest_logic.path_from_metadata(value)


def addon_config_path_candidates(target, slug, addon):
    return manifest_logic.addon_config_path_candidates(
        target,
        slug,
        addon,
        ADDON_CONFIGS_DIR,
        CONFIG_DIR,
        addon_is_zigbee2mqtt,
    )


def resolve_addon_live_path(target, slug, addons):
    return manifest_logic.resolve_addon_live_path(
        target,
        slug,
        addons,
        ADDON_CONFIGS_DIR,
        CONFIG_DIR,
        addon_is_zigbee2mqtt,
    )


def resolve_targets(repo_dir, manifest, addons, require_source=True):
    options = load_options()
    return manifest_logic.resolve_targets(
        repo_dir,
        manifest,
        addons,
        options,
        CONFIG_DIR,
        ADDON_CONFIGS_DIR,
        addon_is_zigbee2mqtt,
        require_source,
    )


def validate_target_id(target_id):
    return manifest_logic.validate_target_id(target_id)


def repo_source_path(repo_dir, source, target_id):
    return manifest_logic.repo_source_path(repo_dir, source, target_id)


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


def sync_tree(src, dest, delete=True, excludes=None):
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


def export_tree(src, dest, delete=True):
    ensure_dir(dest)
    command = ["rsync", "-a", "--checksum"]
    if delete:
        command.append("--delete")
    for pattern in EXPORT_EXCLUDES:
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


def clean_export_destination(dest):
    ensure_dir(dest)
    removed = set()

    for pattern in EXPORT_CLEAN_PATHS:
        matches = list(dest.glob(pattern)) if any(char in pattern for char in "*?[") else [dest / pattern]
        for path in matches:
            if path.exists() or path.is_symlink():
                safe_remove_path(path)
                removed.add(str(path.relative_to(dest)))

    for path in list(dest.rglob("*")):
        relative = str(path.relative_to(dest))
        if path.is_dir() and path.name in EXPORT_CLEAN_DIR_NAMES:
            safe_remove_path(path)
            removed.add(relative)
            continue
        if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in EXPORT_CLEAN_FILE_PATTERNS):
            safe_remove_path(path)
            removed.add(relative)

    return len(removed)


def export_storage_allowlist(src, dest):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    for name in STORAGE_EXPORT_ALLOWLIST:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def copy_homeassistant_path_allowlist(src, dest, paths):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name)
        copied += 1
    return copied


def copy_export_path(src, dest):
    ensure_dir(dest.parent)
    if src.is_dir():
        sync_tree(src, dest, delete=True, excludes=EXPORT_EXCLUDES)
    else:
        shutil.copy2(src, dest)


def export_homeassistant_config(src, dest, target=None):
    clear_tree(dest)
    copied = 0

    for pattern in HOMEASSISTANT_EXPORT_ROOT_PATTERNS:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in HOMEASSISTANT_EXPORT_ROOT_EXCLUDES:
                continue
            copy_export_path(src_path, dest / src_path.name)
            copied += 1

    for name in HOMEASSISTANT_EXPORT_DIRS:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name)
        copied += 1

    zigbee2mqtt_count = 0
    if target and target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = copy_homeassistant_path_allowlist(src, dest, ZIGBEE2MQTT_CONFIG_PATHS)
    storage_count = export_storage_allowlist(src, dest)
    return copied, zigbee2mqtt_count, storage_count


def apply_homeassistant_config(src, dest, target, details=None):
    if not src.exists() or not has_managed_content(src):
        if details is not None:
            add_detail(details, f"Skipping {target['id']} because Git has no Home Assistant config yet.")
        return []

    copied = 0
    for pattern in HOMEASSISTANT_EXPORT_ROOT_PATTERNS:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in HOMEASSISTANT_EXPORT_ROOT_EXCLUDES:
                continue
            dest_path = dest / src_path.name
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
            copied += 1

    for name in HOMEASSISTANT_EXPORT_DIRS:
        src_path = src / name
        if not src_path.exists():
            continue
        sync_homeassistant_path_allowlist(src, dest, [name])
        copied += 1

    zigbee2mqtt_count = 0
    if target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = sync_homeassistant_path_allowlist(src, dest, ZIGBEE2MQTT_CONFIG_PATHS)
    copied_count, skipped_protected = sync_storage_allowlist(
        src,
        dest,
        allow_protected=target_model.allow_protected_storage(target),
    )
    if copied:
        if details is not None:
            add_detail(details, f"Applied {copied} Home Assistant config path(s).")
    if zigbee2mqtt_count:
        if details is not None:
            add_detail(details, f"Applied {zigbee2mqtt_count} Zigbee2MQTT config path(s).")
    if copied_count:
        if details is not None:
            add_detail(details, f"Applied {copied_count} allowlisted .storage config file(s).")
    if skipped_protected:
        if details is not None:
            add_detail(details, f"Skipped protected .storage file(s): {', '.join(skipped_protected)}.")
    return skipped_protected


def sync_homeassistant_path_allowlist(src, dest, paths):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        if src_path.is_dir():
            sync_tree(src_path, dest_path, delete=True, excludes=EXPORT_EXCLUDES)
        else:
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def sync_storage_allowlist(src, dest, allow_protected=False):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0, []

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    skipped_protected = []
    for name in STORAGE_EXPORT_ALLOWLIST:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        if name in PROTECTED_STORAGE_FILES and not allow_protected:
            skipped_protected.append(name)
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied, skipped_protected


def clear_tree(dest):
    ensure_dir(dest)
    empty_dir = WORK_DIR / "empty"
    ensure_dir(empty_dir)
    sync_tree(empty_dir, dest, delete=True)


def safe_release_dir(release_name):
    if not release_name or Path(release_name).name != release_name:
        raise RuntimeError("Invalid release name")
    release_dir = (RELEASES_DIR / release_name).resolve()
    releases_root = RELEASES_DIR.resolve()
    if release_dir.parent != releases_root:
        raise RuntimeError("Invalid release name")
    return release_dir


def source_has_applicable_storage(path, allow_protected=False):
    storage = path / ".storage"
    if not storage.exists():
        return False
    for name in STORAGE_EXPORT_ALLOWLIST:
        if name in PROTECTED_STORAGE_FILES and not allow_protected:
            continue
        if (storage / name).exists():
            return True
    return False


def create_release_snapshot(resolved_targets, commit, backup_slug):
    release_name = release_now()
    release_dir = RELEASES_DIR / release_name
    ensure_dir(release_dir)

    metadata = {
        "created_at": utc_now(),
        "commit": commit,
        "backup_slug": backup_slug,
        "targets": [],
    }

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        target_snapshot = release_dir / target["id"]
        ensure_dir(target_snapshot)

        existed = live_path.exists()
        if existed:
            sync_tree(live_path, target_snapshot, delete=True)

        metadata["targets"].append(
            {
                "id": target["id"],
                "type": target["type"],
                "resolved_slug": target.get("resolved_slug"),
                "live_path": target["live_path"],
                "source_path": target["source_path"],
                "delete": target_restore_delete(target),
                "restart_after_sync": bool(target.get("restart_after_sync", True)),
                "stop_addon_before_sync": bool(target.get("stop_addon_before_sync", False)),
                "stop_core_before_sync_if_storage": bool(target.get("stop_core_before_sync_if_storage", False)),
                "existed": existed,
            }
        )

    (release_dir / "release.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    return release_name


def release_created_at(path):
    metadata = load_json(path / "release.json", {})
    created_at = parse_backup_date(metadata.get("created_at"))
    if created_at:
        return created_at
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def prune_release_snapshots(options, protected_release=None):
    if not RELEASES_DIR.exists():
        return []

    keep_count = option_int(options, "release_snapshot_keep_count", DEFAULT_RELEASE_KEEP_COUNT, minimum=0)
    keep_days = option_int(options, "release_snapshot_keep_days", DEFAULT_RELEASE_KEEP_DAYS, minimum=0)
    now = datetime.now(timezone.utc)
    releases = []
    for path in RELEASES_DIR.iterdir():
        if not path.is_dir() or path.name == protected_release:
            continue
        releases.append((path, release_created_at(path)))

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
        safe_dir = safe_release_dir(path.name)
        safe_remove_path(safe_dir)
        removed.append(path.name)
    return removed


def restore_release_snapshot(release_name, details):
    release_dir = safe_release_dir(release_name)
    metadata_path = release_dir / "release.json"
    if not metadata_path.exists():
        raise RuntimeError(f"Release metadata not found for {release_name}")

    metadata = load_json(metadata_path, {})
    targets = metadata.get("targets", [])
    core_stopped = False
    homeassistant_seen = False

    for target in targets:
        live_path = Path(target["live_path"])
        snapshot_path = release_dir / target["id"]
        target_type = target.get("type")
        addon_was_started = False

        if target_type == "homeassistant" and not core_stopped:
            add_detail(details, f"Stopping Home Assistant Core for rollback of release {release_name}.")
            core_stop()
            core_stopped = True
            homeassistant_seen = True
        elif target_type == "addon" and target.get("stop_addon_before_sync", False):
            slug = target.get("resolved_slug")
            add_detail(details, f"Stopping add-on {slug} before rollback sync.")
            addon_was_started = stop_addon_for_sync(slug)

        if target.get("existed", True):
            add_detail(details, f"Restoring {target['id']} from release {release_name}.")
            sync_tree(snapshot_path, live_path, delete=bool(target.get("delete", True)))
        else:
            add_detail(details, f"Clearing {target['id']} because it did not exist in release {release_name}.")
            clear_tree(live_path)

        if target_type == "addon" and target.get("restart_after_sync", True):
            slug = target.get("resolved_slug")
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    add_detail(details, f"Starting add-on {slug} after rollback.")
                    addon_action(slug, "start")
            else:
                add_detail(details, f"Restarting add-on {slug} after rollback.")
                restart_or_start_addon(slug)

    if homeassistant_seen:
        add_detail(details, "Starting Home Assistant Core after rollback.")
        core_start()

    return metadata


def restart_or_start_addon(slug):
    info = get_addon_info(slug)
    state = info.get("state")
    if state == "started":
        addon_action(slug, "restart")
    else:
        addon_action(slug, "start")


def stop_addon_for_sync(slug):
    info = get_addon_info(slug)
    was_started = info.get("state") == "started"
    if was_started:
        addon_action(slug, "stop")
    return was_started


def target_apply_delete(target):
    return target_model.apply_delete(target)


def target_save_delete(target):
    return target_model.save_delete(target)


def target_restore_delete(target):
    return target_model.restore_delete(target)


def apply_targets(resolved_targets, details):
    homeassistant_target = None
    core_stopped = False

    for target in resolved_targets:
        source_path = Path(target["source_path"])
        live_path = Path(target["live_path"])
        addon_was_started = False

        if target["type"] == "homeassistant":
            homeassistant_target = target
            allow_protected_storage = target_model.allow_protected_storage(target)
            if target.get("stop_core_before_sync_if_storage", False) and source_has_applicable_storage(source_path, allow_protected_storage) and not core_stopped:
                add_detail(details, "Stopping Home Assistant Core before syncing .storage.")
                core_stop()
                core_stopped = True
        elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
            slug = target["resolved_slug"]
            add_detail(details, f"Stopping add-on {slug} before sync.")
            addon_was_started = stop_addon_for_sync(slug)

        add_detail(details, f"Syncing {target['id']} from {source_path} to {live_path}.")
        if target["type"] == "homeassistant":
            apply_homeassistant_config(source_path, live_path, target, details)
        else:
            if not source_path.exists() or not has_managed_content(source_path):
                add_detail(details, f"Skipping {target['id']} because Git has no config for this add-on yet.")
                continue
            sync_tree(source_path, live_path, delete=target_apply_delete(target))

        if target["type"] == "addon" and target.get("restart_after_sync", True):
            slug = target["resolved_slug"]
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    add_detail(details, f"Starting add-on {slug} after sync.")
                    addon_action(slug, "start")
            else:
                add_detail(details, f"Restarting add-on {slug}.")
                restart_or_start_addon(slug)

    if homeassistant_target is None:
        return

    add_detail(details, "Running Home Assistant config check.")
    do_core_check()

    if core_stopped:
        if homeassistant_target.get("restart_after_sync", True):
            add_detail(details, "Starting Home Assistant Core after sync.")
            core_start()
    else:
        if homeassistant_target.get("restart_after_sync", True):
            add_detail(details, "Restarting Home Assistant Core.")
            core_restart()


def export_targets(resolved_targets, details):
    for target in resolved_targets:
        live_path = Path(target["live_path"])
        source_path = Path(target["source_path"])
        if not live_path.exists():
            if target.get("optional", False):
                add_detail(details, f"Skipping optional target {target['id']} because {live_path} does not exist.")
                continue
            raise RuntimeError(f"Live path does not exist for target '{target['id']}': {live_path}")

        if target["type"] == "homeassistant":
            add_detail(details, f"Saving config-only {target['id']} from {live_path} to {source_path}.")
            copied_count, zigbee2mqtt_count, storage_count = export_homeassistant_config(live_path, source_path, target)
            add_detail(details, f"Saved {copied_count} Home Assistant config path(s).")
            if zigbee2mqtt_count:
                add_detail(details, f"Saved {zigbee2mqtt_count} legacy Zigbee2MQTT config path(s).")
            if storage_count:
                add_detail(details, f"Saved {storage_count} allowlisted .storage config file(s).")
        else:
            add_detail(details, f"Saving {target['id']} from {live_path} to {source_path}.")
            removed_count = clean_export_destination(source_path)
            if removed_count:
                add_detail(details, f"Removed {removed_count} excluded item(s) from {target['id']} save destination.")
            export_tree(live_path, source_path, delete=target_save_delete(target))


def sync_to_preview(target, preview_path):
    source_path = Path(target["source_path"])
    live_path = Path(target["live_path"])
    clear_tree(preview_path)
    if live_path.exists():
        sync_tree(live_path, preview_path, delete=True)

    if target["type"] == "homeassistant":
        if source_path.exists() and has_managed_content(source_path):
            skipped_protected = apply_homeassistant_config(source_path, preview_path, target)
        else:
            skipped_protected = []
    else:
        if source_path.exists() and has_managed_content(source_path):
            sync_tree(source_path, preview_path, delete=target_apply_delete(target))
        skipped_protected = []
    return skipped_protected


def target_diff(target, preview_path):
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


def build_apply_preview(resolved_targets):
    preview_root = WORK_DIR / "apply-preview"
    clear_tree(preview_root)
    chunks = []
    deletion_count = 0
    skipped_protected = []

    for target in resolved_targets:
        preview_path = preview_root / safe_preview_name(str(target["id"]))
        skipped = sync_to_preview(target, preview_path)
        if skipped:
            skipped_protected.extend(skipped)
            chunks.append(f"Target {target['id']}: skipped protected .storage file(s): {', '.join(skipped)}.\n")
        deletion_count += count_preview_deletions(target, preview_path)
        chunks.append(target_diff(target, preview_path))

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = "No file changes."

    return {
        "diff": truncate_diff(diff_text),
        "fingerprint": fingerprint_text(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
    }


def build_apply_diff(resolved_targets):
    return build_apply_preview(resolved_targets)["diff"]


def ensure_preview_matches_state(state, commit, preview):
    if state.get("last_preview_commit") != commit:
        raise RuntimeError("Run Preview Git to HA before Apply Git to HA. The preview commit does not match.")
    if state.get("last_preview_fingerprint") != preview["fingerprint"]:
        raise RuntimeError("Run Preview Git to HA again. The live diff changed since the last preview.")


def enforce_apply_limits(options, preview):
    max_deletions = option_int(options, "max_apply_deletions", DEFAULT_MAX_APPLY_DELETIONS, minimum=0)
    if preview["deletions"] > max_deletions:
        raise RuntimeError(
            f"Apply would delete {preview['deletions']} file(s), above the limit of {max_deletions}. Review the preview or raise max_apply_deletions."
        )


def git_status_porcelain(repo_dir):
    return git_ops.git_status_porcelain(repo_dir, run_command)


def stage_homeassistant_storage_allowlist(repo_dir, options, details):
    manifest, _manifest_path = load_manifest(repo_dir, options)
    paths = []

    for target in manifest.get("targets", []):
        if target.get("type") != "homeassistant":
            continue

        source = repo_dir / target.get("source", options.get("apply_path", "homeassistant"))
        storage = source / ".storage"
        if not storage.exists():
            continue

        for name in STORAGE_EXPORT_ALLOWLIST:
            path = storage / name
            if path.exists():
                paths.append(str(path.relative_to(repo_dir)))

    if not paths:
        return 0

    add = run_command(["git", "add", "-f", "--"] + paths, cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add allowlisted .storage failed:\n{add.stderr.strip()}")

    add_detail(details, f"Staged {len(paths)} allowlisted .storage config file(s).")
    return len(paths)


def run_save_job():
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "save",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = load_options()
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "save",
            "last_message": "Preparing save.",
            "last_details": details,
        }
    )

    try:
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Save HA to Git.")

        repo_dir = ensure_repo(options, reset_to_origin=False)
        env = git_env(options)
        branch = options.get("repo_branch", "main")
        git_pull_rebase(repo_dir, env, branch)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Using branch {branch} at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Saving live Home Assistant config to Git.")
        export_targets(resolved_targets, details)
        stage_homeassistant_storage_allowlist(repo_dir, options, details)
        stage_all(repo_dir)

        new_commit = commit_if_needed(repo_dir, f"Save Home Assistant config {release_now()}")
        if new_commit:
            add_detail(details, f"Created commit {new_commit}.")
            try:
                push_branch(repo_dir, env, branch)
            except RuntimeError:
                git_pull_rebase(repo_dir, env, branch)
                push_branch(repo_dir, env, branch)
            add_detail(details, f"Pushed to origin/{branch}.")
        else:
            add_detail(details, "No live Home Assistant changes to save.")

        write_state({"conflicts": []})

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "save",
                "last_message": "Save finished successfully.",
                "last_details": details,
                "last_targets": resolved_targets,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        try:
            repo_path = repo_checkout_path(options)
        except RuntimeError:
            repo_path = None
        conflicts = git_conflict_paths(repo_path) if repo_path and repo_path.exists() else []
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "save",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
                "conflicts": conflicts,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


def run_apply_job():
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "apply",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = load_options()
    release_name = None
    backup_slug = None
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "apply",
            "last_message": "Preparing apply.",
            "last_details": details,
        }
    )

    try:
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Apply Git to HA.")

        repo_dir = ensure_repo(options)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Fetched repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Rebuilding apply preview for safety checks.")
        preview = build_apply_preview(resolved_targets)
        ensure_preview_matches_state(state, commit, preview)
        enforce_apply_limits(options, preview)

        backup_slug = ensure_fresh_system_backup(options, details)

        if option_bool(options, "create_release_snapshot", True):
            add_detail(details, "Creating local release snapshot.")
            release_name = create_release_snapshot(resolved_targets, commit, backup_slug)
            add_detail(details, f"Created local release snapshot {release_name}.")

        apply_targets(resolved_targets, details)
        pruned = prune_release_snapshots(options, protected_release=release_name)
        if pruned:
            add_detail(details, f"Pruned {len(pruned)} old local release snapshot(s): {', '.join(pruned)}.")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "apply",
                "last_message": "Apply finished successfully.",
                "last_details": details,
                "last_release": release_name,
                "last_backup_slug": backup_slug,
                "last_targets": resolved_targets,
                "last_preview_deletions": preview["deletions"],
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        if release_name:
            try:
                add_detail(details, f"Restoring local release snapshot {release_name} after failure.")
                restore_release_snapshot(release_name, details)
            except Exception as rollback_exc:
                details.append(f"Rollback from local release failed: {rollback_exc}")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "apply",
                "last_message": str(exc),
                "last_details": details,
                "last_release": release_name,
                "last_backup_slug": backup_slug,
                "last_targets": resolved_targets,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


def run_preview_job():
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "preview",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = load_options()
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "preview",
            "last_message": "Preparing apply preview.",
            "last_details": details,
        }
    )

    try:
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Preview Git to HA.")

        repo_dir = ensure_repo(options)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Fetched repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Building apply preview without changing live config.")
        preview = build_apply_preview(resolved_targets)

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "preview",
                "last_message": "Apply preview finished successfully.",
                "last_details": details,
                "last_targets": resolved_targets,
                "last_diff": preview["diff"],
                "last_diff_generated_at": utc_now(),
                "last_preview_commit": commit,
                "last_preview_fingerprint": preview["fingerprint"],
                "last_preview_deletions": preview["deletions"],
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "preview",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


def run_rollback_job(release_name):
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "rollback",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "rollback",
            "last_message": f"Rolling back release {release_name}.",
            "last_details": details,
        }
    )

    try:
        metadata = restore_release_snapshot(release_name, details)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "rollback",
                "last_message": f"Rollback to {release_name} finished successfully.",
                "last_details": details,
                "last_release": release_name,
                "last_backup_slug": metadata.get("backup_slug"),
                "last_targets": metadata.get("targets", []),
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "rollback",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


def start_apply():
    thread = threading.Thread(target=run_apply_job, daemon=True)
    thread.start()


def start_preview():
    thread = threading.Thread(target=run_preview_job, daemon=True)
    thread.start()


def start_save():
    thread = threading.Thread(target=run_save_job, daemon=True)
    thread.start()


def start_rollback(release_name):
    thread = threading.Thread(target=run_rollback_job, args=(release_name,), daemon=True)
    thread.start()


def safe_repo_relative_path(value):
    return git_ops.safe_repo_relative_path(value)


def resolve_git_conflict(path, choice):
    options = load_options()
    repo_dir = repo_checkout_path(options)
    branch = options.get("repo_branch", "main")
    safe_path = safe_repo_relative_path(path)
    if choice == "ha":
        checkout = run_command(["git", "checkout", "--theirs", "--", safe_path], cwd=repo_dir)
    elif choice == "git":
        checkout = run_command(["git", "checkout", "--ours", "--", safe_path], cwd=repo_dir)
    else:
        raise RuntimeError("Invalid conflict choice")
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout conflict version failed:\n{checkout.stderr.strip()}")

    add = run_command(["git", "add", "--", safe_path], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add conflict resolution failed:\n{add.stderr.strip()}")

    conflicts = git_conflict_paths(repo_dir)
    if conflicts:
        write_state({"conflicts": conflicts})
        return f"Resolved {safe_path}. {len(conflicts)} conflict(s) remain."

    env = git_env(options)
    env["GIT_EDITOR"] = "true"
    cont = run_command(["git", "rebase", "--continue"], env=env, cwd=repo_dir)
    if cont.returncode != 0:
        output = cont.stderr.strip() or cont.stdout.strip()
        if "No changes" in output or "previous cherry-pick is now empty" in output:
            skip = run_command(["git", "rebase", "--skip"], env=env, cwd=repo_dir)
            if skip.returncode != 0:
                raise RuntimeError(f"git rebase --skip failed:\n{skip.stderr.strip() or skip.stdout.strip()}")
        else:
            raise RuntimeError(f"git rebase --continue failed:\n{output}")
    push_branch(repo_dir, env, branch)
    write_state({"conflicts": [], "last_status": "success", "last_message": "Conflicts resolved and pushed."})
    return "All conflicts resolved and pushed."


def current_manifest_preview():
    options = load_options()
    try:
        repo_dir = repo_checkout_path(options)
        try:
            addons = get_installed_addons()
        except Exception:
            addons = None
        if repo_dir.exists():
            manifest, _ = load_manifest(repo_dir, options, addons)
        else:
            manifest = default_manifest(options)
        previews = []
        for target in manifest.get("targets", []):
            previews.append(
                {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "source": target.get("source"),
                    "addon_slug": target.get("addon_slug"),
                    "addon_slug_suffix": target.get("addon_slug_suffix"),
                    "allow_protected_storage": target.get("allow_protected_storage", False),
                }
            )
        return previews
    except Exception:
        return []


def addon_slug_value(addon):
    return addon.get("slug") or addon.get("name") or ""


def addon_display_name(addon):
    name = addon.get("name") or addon_slug_value(addon)
    slug = addon_slug_value(addon)
    return f"{name} ({slug})" if slug and slug not in name else name


def addon_is_zigbee2mqtt(addon):
    text = f"{addon.get('slug', '')} {addon.get('name', '')} {addon.get('description', '')}".lower()
    return "zigbee2mqtt" in text or "zigbee2mqtt" in text.replace(" ", "")


def render_addons():
    return ui.render_addons(
        selected_addon_slugs(),
        get_installed_addons,
        addon_slug_value,
        addon_display_name,
        addon_is_zigbee2mqtt,
    )


def render_conflicts(conflicts):
    return ui.render_conflicts(conflicts)


def render_targets(items):
    return ui.render_targets(items)


def render_releases(releases):
    return ui.render_releases(releases)


def targets_allow_protected_storage(items):
    return ui.targets_allow_protected_storage(items)


def render_git_auth(options):
    return ui.render_git_auth(options, git_auth_mode, load_generated_public_key)


def render_page():
    options = load_options()
    state = read_state()
    backup_status = latest_system_backup_status(options)
    releases = list_releases()
    manifest_preview = current_manifest_preview()
    target_state = state.get("last_targets") or manifest_preview
    last_status = state.get("last_status", "idle")
    details = "\n".join(state.get("last_details", []))
    details_placeholder = "Running..." if last_status == "running" else "No details yet."
    diff_text = state.get("last_diff", "")
    action_disabled = "disabled" if last_status == "running" else ""
    confirm_messages = []
    if not option_bool(options, "require_fresh_backup", True):
        confirm_messages.append("Fresh system backup checks are disabled.")
    if targets_allow_protected_storage(target_state):
        confirm_messages.append("Protected .storage apply is enabled for at least one target.")
    apply_confirm = ""
    if confirm_messages:
        confirm_message = " ".join(confirm_messages) + " Continue?"
        apply_confirm = f"data-confirm='{html.escape(confirm_message, quote=True)}'"

    return ui.render_page(
        {
            "status": html.escape(last_status),
            "badge_class": "error" if last_status == "error" else "running" if last_status == "running" else "",
            "message": html.escape(state.get("last_message", "")),
            "last_run": html.escape(str(state.get("last_run_at"))),
            "last_release": html.escape(str(state.get("last_release"))),
            "last_backup_slug": html.escape(str(state.get("last_backup_slug"))),
            "latest_backup": html.escape(backup_status.get("message", "Backup status unavailable.")),
            "repo_url": html.escape(options.get("repo_url", "")),
            "branch": html.escape(options.get("repo_branch", "main")),
            "manifest_path": html.escape(options.get("manifest_path", "ha-ops.json")),
            "auth_mode": html.escape(git_auth_mode(options)),
            "details_html": html.escape(details or details_placeholder),
            "diff_generated_at": html.escape(str(state.get("last_diff_generated_at"))),
            "diff_html": html.escape(diff_text or "No apply preview yet."),
            "preview_deletions": html.escape(str(state.get("last_preview_deletions"))),
            "action_disabled": action_disabled,
            "apply_confirm": apply_confirm,
            "conflicts_html": render_conflicts(state.get("conflicts", [])),
            "git_auth_html": render_git_auth(options),
            "targets_html": render_targets(target_state),
            "addons_html": render_addons(),
            "releases_html": render_releases(releases),
            "version": html.escape(addon_version()),
        }
    )


class Handler(BaseHTTPRequestHandler):
    def send_html(self, content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def wants_json(self):
        accept = self.headers.get("Accept", "")
        requested_with = self.headers.get("X-Requested-With", "")
        return "application/json" in accept or requested_with == "fetch"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            return

        self.send_html(render_page())

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = parse_qs(self.rfile.read(length).decode()) if length else {}

        if parsed.path == "/generate-key":
            try:
                public_key = generate_deploy_key()
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "idle",
                        "last_action": "generate_key",
                        "last_message": "Generated a new deploy key. Add the public key to GitHub Deploy Keys.",
                        "last_details": [public_key],
                    }
                )
                log("Generate Deploy Key completed successfully")
                if self.wants_json():
                    self.send_json(
                        {
                            "ok": True,
                            "message": "Generated a new deploy key. Reloading UI.",
                            "public_key": public_key,
                        }
                    )
                    return
            except Exception as exc:
                log(f"Generate Deploy Key failed: {exc}")
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "error",
                        "last_action": "generate_key",
                        "last_message": str(exc),
                        "last_details": [str(exc)],
                    }
                )
                if self.wants_json():
                    self.send_json({"ok": False, "message": str(exc)}, status=500)
                    return
            self.send_html(render_page())
            return

        if parsed.path == "/apply":
            start_apply()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Apply Git to HA started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/preview":
            start_preview()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Git to HA preview started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/save":
            start_save()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Save HA to Git started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/addons":
            selected = body.get("addon", [])
            set_selected_addon_slugs(selected)
            if self.wants_json():
                self.send_json({"ok": True, "message": "Managed add-on selection saved. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/resolve-conflict":
            try:
                path = body.get("path", [""])[0]
                choice = body.get("choice", [""])[0]
                message = resolve_git_conflict(path, choice)
                if self.wants_json():
                    self.send_json({"ok": True, "message": f"{message} Refreshing..."})
                else:
                    self.send_html(render_page())
                return
            except Exception as exc:
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "error",
                        "last_action": "resolve_conflict",
                        "last_message": str(exc),
                        "last_details": [str(exc)],
                    }
                )
                if self.wants_json():
                    self.send_json({"ok": False, "message": str(exc)}, status=500)
                else:
                    self.send_html(render_page(), status=500)
                return

        if parsed.path == "/rollback":
            release = body.get("release", [""])[0]
            if not release:
                if self.wants_json():
                    self.send_json({"ok": False, "message": "Missing release"}, status=400)
                else:
                    self.send_error(400, "Missing release")
                return
            start_rollback(release)
            if self.wants_json():
                self.send_json({"ok": True, "message": f"Rollback to {release} started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        self.send_error(404)

    def log_message(self, format, *args):
        return


def main():
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    write_state(read_state())
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
