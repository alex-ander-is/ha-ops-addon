from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import html
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import state as state_store
import supervisor
import sync as sync_logic
import ui
import backups as backup_policy
import git_ops
import jobs as job_logic
import manifest as manifest_logic
import policies
import releases as release_logic
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
    return release_logic.list_releases(release_deps())


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


def core_reload_yaml():
    return supervisor.core_reload_yaml(call_supervisor)


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
        policies.DEFAULT_BACKUP_MAX_AGE_HOURS,
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


def reset_repo_worktree(repo_dir):
    return git_ops.reset_repo_worktree(repo_dir, run_command)


def git_commit(repo_dir, ref):
    return git_ops.git_commit(repo_dir, ref, run_command)


def git_ref_exists(repo_dir, ref):
    return git_ops.git_ref_exists(repo_dir, ref, run_command)


def git_remote_head(repo_dir, env, branch):
    return git_ops.git_remote_head(repo_dir, env, branch, run_command)


def git_head_or_unborn(repo_dir):
    return git_ops.git_head_or_unborn(repo_dir, run_command)


def git_has_unpushed_commits(repo_dir, branch):
    return git_ops.git_has_unpushed_commits(repo_dir, branch, run_command)


def git_conflict_paths(repo_dir):
    return git_ops.git_conflict_paths(repo_dir, run_command)


def git_rebase_in_progress(repo_dir):
    return git_ops.git_rebase_in_progress(repo_dir, run_command)


def git_pull_rebase(repo_dir, env, branch):
    return git_ops.git_pull_rebase(
        repo_dir,
        env,
        branch,
        run_command,
        lambda conflicts: write_state({"conflicts": conflicts, "conflict_type": "git_rebase"}),
    )


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


def sync_deps():
    return sync_logic.SyncContext(
        add_detail=add_detail,
        addon_action=addon_action,
        clean_dir_names=policies.EXPORT_CLEAN_DIR_NAMES,
        clean_file_patterns=policies.EXPORT_CLEAN_FILE_PATTERNS,
        clean_paths=policies.EXPORT_CLEAN_PATHS,
        core_restart=core_restart,
        core_reload_yaml=core_reload_yaml,
        core_start=core_start,
        core_stop=core_stop,
        do_core_check=do_core_check,
        export_excludes=policies.EXPORT_EXCLUDES,
        ha_dirs=policies.HOMEASSISTANT_EXPORT_DIRS,
        ha_root_excludes=policies.HOMEASSISTANT_EXPORT_ROOT_EXCLUDES,
        ha_root_patterns=policies.HOMEASSISTANT_EXPORT_ROOT_PATTERNS,
        protected_storage_files=policies.PROTECTED_STORAGE_FILES,
        restart_or_start_addon=restart_or_start_addon,
        run_command=run_command,
        stop_addon_for_sync=stop_addon_for_sync,
        storage_allowlist=policies.STORAGE_EXPORT_ALLOWLIST,
        work_dir=WORK_DIR,
        zigbee2mqtt_paths=policies.ZIGBEE2MQTT_CONFIG_PATHS,
    )


def has_managed_content(path):
    return sync_logic.has_managed_content(path)


def ensure_dir(path):
    return sync_logic.ensure_dir(path)


def sync_tree(src, dest, delete=True, excludes=None):
    return sync_logic.sync_tree(src, dest, delete, excludes, run_command)


def export_tree(src, dest, delete=True):
    return sync_logic.export_tree(src, dest, delete, policies.EXPORT_EXCLUDES, run_command)


def safe_remove_path(path):
    return sync_logic.safe_remove_path(path)


def clean_export_destination(dest):
    return sync_logic.clean_export_destination(
        dest,
        policies.EXPORT_CLEAN_PATHS,
        policies.EXPORT_CLEAN_DIR_NAMES,
        policies.EXPORT_CLEAN_FILE_PATTERNS,
    )


def export_storage_allowlist(src, dest):
    return sync_logic.export_storage_allowlist(src, dest, policies.STORAGE_EXPORT_ALLOWLIST)


def copy_homeassistant_path_allowlist(src, dest, paths):
    return sync_logic.copy_homeassistant_path_allowlist(src, dest, paths, policies.EXPORT_EXCLUDES, run_command)


def copy_export_path(src, dest):
    return sync_logic.copy_export_path(src, dest, policies.EXPORT_EXCLUDES, run_command)


def export_homeassistant_config(src, dest, target=None):
    return sync_logic.export_homeassistant_config(src, dest, target, sync_deps())


def apply_homeassistant_config(src, dest, target, details=None):
    return sync_logic.apply_homeassistant_config(src, dest, target, sync_deps(), details)


def restore_homeassistant_config(src, dest, target):
    return sync_logic.restore_homeassistant_config(src, dest, target, sync_deps())


def sync_homeassistant_path_allowlist(src, dest, paths):
    return sync_logic.sync_homeassistant_path_allowlist(src, dest, paths, policies.EXPORT_EXCLUDES, run_command)


def sync_storage_allowlist(src, dest, allow_protected=False):
    return sync_logic.sync_storage_allowlist(
        src,
        dest,
        policies.STORAGE_EXPORT_ALLOWLIST,
        policies.PROTECTED_STORAGE_FILES,
        allow_protected,
    )


def clear_tree(dest):
    return sync_logic.clear_tree(dest, WORK_DIR, run_command)


def release_deps():
    return release_logic.ReleaseContext(
        add_detail=add_detail,
        addon_action=addon_action,
        clear_tree=clear_tree,
        core_reload_yaml=core_reload_yaml,
        core_restart=core_restart,
        core_start=core_start,
        core_stop=core_stop,
        export_homeassistant_config=export_homeassistant_config,
        export_tree=export_tree,
        load_json=load_json,
        option_int=option_int,
        parse_backup_date=parse_backup_date,
        release_now=release_now,
        releases_dir=RELEASES_DIR,
        restart_or_start_addon=restart_or_start_addon,
        restore_homeassistant_config=restore_homeassistant_config,
        safe_remove_path=safe_remove_path,
        stop_addon_for_sync=stop_addon_for_sync,
        sync_deps=sync_deps,
        sync_tree=sync_tree,
        utc_now=utc_now,
    )


def safe_release_dir(release_name):
    return release_logic.safe_release_dir(release_name, release_deps())


def source_has_applicable_storage(path, allow_protected=False):
    return sync_logic.source_has_applicable_storage(
        path,
        policies.STORAGE_EXPORT_ALLOWLIST,
        policies.PROTECTED_STORAGE_FILES,
        allow_protected,
    )


def create_release_snapshot(resolved_targets, commit, backup_slug):
    return release_logic.create_release_snapshot(resolved_targets, commit, backup_slug, release_deps())


def release_created_at(path):
    return release_logic.release_created_at(path, release_deps())


def prune_release_snapshots(options, protected_release=None):
    return release_logic.prune_release_snapshots(options, protected_release, release_deps())


def restore_release_snapshot(release_name, details, core_already_stopped=False):
    return release_logic.restore_release_snapshot(release_name, details, core_already_stopped, release_deps())


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
    return sync_logic.apply_targets(resolved_targets, details, sync_deps())


def export_targets(resolved_targets, details):
    return sync_logic.export_targets(resolved_targets, details, sync_deps())


def save_unknown_base_conflicts(resolved_targets, repo_dir, resolutions, details):
    return sync_logic.save_unknown_base_conflicts(resolved_targets, repo_dir, resolutions, details, sync_deps())


def restore_save_git_resolutions(repo_dir, resolutions, details):
    return sync_logic.restore_save_git_resolutions(repo_dir, resolutions, details, sync_deps())


def sync_to_preview(target, preview_path):
    return sync_logic.sync_to_preview(target, preview_path, sync_deps())


def target_diff(target, preview_path):
    return sync_logic.target_diff(target, preview_path, run_command)


def count_preview_deletions(target, preview_path):
    return sync_logic.count_preview_deletions(target, preview_path)


def safe_preview_name(value):
    return sync_logic.safe_preview_name(value)


def fingerprint_text(text):
    return sync_logic.fingerprint_text(text)


def truncate_diff(diff_text):
    return sync_logic.truncate_diff(diff_text)


def build_apply_preview(resolved_targets):
    return sync_logic.build_apply_preview(resolved_targets, sync_deps())


def build_apply_diff(resolved_targets):
    return build_apply_preview(resolved_targets)["diff"]


def ensure_preview_matches_state(state, commit, preview):
    if state.get("last_preview_commit") != commit:
        raise RuntimeError("Run Preview Git to HA before Apply Git to HA. The preview commit does not match.")
    if state.get("last_preview_fingerprint") != preview["fingerprint"]:
        raise RuntimeError("Run Preview Git to HA again. The live diff changed since the last preview.")


def enforce_apply_limits(options, preview):
    max_deletions = option_int(options, "max_apply_deletions", policies.DEFAULT_MAX_APPLY_DELETIONS, minimum=0)
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

        for name in policies.STORAGE_EXPORT_ALLOWLIST:
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


def job_deps():
    return job_logic.JobContext(
        add_detail=add_detail,
        apply_targets=apply_targets,
        build_apply_preview=build_apply_preview,
        commit_if_needed=commit_if_needed,
        create_release_snapshot=create_release_snapshot,
        enforce_apply_limits=enforce_apply_limits,
        ensure_fresh_system_backup=ensure_fresh_system_backup,
        ensure_preview_matches_state=ensure_preview_matches_state,
        ensure_repo=ensure_repo,
        export_targets=export_targets,
        get_installed_addons=get_installed_addons,
        git_conflict_paths=git_conflict_paths,
        git_env=git_env,
        git_has_unpushed_commits=git_has_unpushed_commits,
        git_head_or_unborn=git_head_or_unborn,
        git_pull_rebase=git_pull_rebase,
        load_manifest=load_manifest,
        load_options=load_options,
        option_bool=option_bool,
        prune_release_snapshots=prune_release_snapshots,
        push_branch=push_branch,
        read_state=read_state,
        release_now=release_now,
        repo_checkout_path=repo_checkout_path,
        reset_repo_worktree=reset_repo_worktree,
        restore_save_git_resolutions=restore_save_git_resolutions,
        resolve_targets=resolve_targets,
        restore_release_snapshot=restore_release_snapshot,
        run_lock=RUN_LOCK,
        save_unknown_base_conflicts=save_unknown_base_conflicts,
        stage_all=stage_all,
        stage_homeassistant_storage_allowlist=stage_homeassistant_storage_allowlist,
        utc_now=utc_now,
        write_state=write_state,
    )


def run_save_job():
    return job_logic.run_save_job(job_deps())


def run_apply_job():
    return job_logic.run_apply_job(job_deps())


def run_preview_job():
    return job_logic.run_preview_job(job_deps())


def run_rollback_job(release_name):
    return job_logic.run_rollback_job(release_name, job_deps())


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


def resolve_save_unknown_base_conflict(path, choice):
    safe_path = safe_repo_relative_path(path)
    if choice not in {"ha", "git"}:
        raise RuntimeError("Invalid conflict choice")

    state = read_state()
    conflicts = list(state.get("conflicts", []))
    if safe_path not in conflicts:
        raise RuntimeError("Save conflict path is not pending")

    resolutions = dict(state.get("save_conflict_resolutions", {}))
    resolutions[safe_path] = choice
    remaining = [item for item in conflicts if item != safe_path]
    if remaining:
        write_state(
            {
                "conflicts": remaining,
                "conflict_type": "save_unknown_base",
                "save_conflict_resolutions": resolutions,
                "last_status": "error",
                "last_message": f"Resolved {safe_path}. {len(remaining)} Save conflict(s) remain.",
            }
        )
        return f"Resolved {safe_path}. {len(remaining)} Save conflict(s) remain."

    write_state(
        {
            "conflicts": [],
            "conflict_type": None,
            "save_conflict_resolutions": resolutions,
            "last_status": "idle",
            "last_message": "Save conflicts resolved. Run Save HA to Git again.",
        }
    )
    return "All Save conflicts resolved. Run Save HA to Git again."


def finish_git_conflict_resolution(repo_dir, env, branch):
    if git_rebase_in_progress(repo_dir):
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
    write_state(
        {
            "conflicts": [],
            "conflict_type": None,
            "save_conflict_resolutions": {},
            "last_status": "success",
            "last_message": "Conflicts resolved and pushed.",
        }
    )
    return "All conflicts resolved and pushed."


def resolve_git_conflict(path, choice):
    state = read_state()
    if state.get("conflict_type") == "save_unknown_base":
        return resolve_save_unknown_base_conflict(path, choice)

    options = load_options()
    repo_dir = repo_checkout_path(options)
    branch = options.get("repo_branch", "main")
    safe_path = safe_repo_relative_path(path)
    if choice not in {"ha", "git"}:
        raise RuntimeError("Invalid conflict choice")

    actual_conflicts = git_conflict_paths(repo_dir)
    if actual_conflicts:
        if safe_path not in actual_conflicts:
            raise RuntimeError("Git conflict path is not pending")
        if choice == "ha":
            checkout = run_command(["git", "checkout", "--theirs", "--", safe_path], cwd=repo_dir)
        else:
            checkout = run_command(["git", "checkout", "--ours", "--", safe_path], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout conflict version failed:\n{checkout.stderr.strip()}")

        add = run_command(["git", "add", "--", safe_path], cwd=repo_dir)
        if add.returncode != 0:
            raise RuntimeError(f"git add conflict resolution failed:\n{add.stderr.strip()}")

        conflicts = git_conflict_paths(repo_dir)
        if conflicts:
            write_state({"conflicts": conflicts, "conflict_type": "git_rebase"})
            return f"Resolved {safe_path}. {len(conflicts)} conflict(s) remain."

    env = git_env(options)
    return finish_git_conflict_resolution(repo_dir, env, branch)


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
