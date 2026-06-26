from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
import sys
import traceback

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app_context
import backups as backup_policy
import conflicts
import git_auth
import git_ops
import manifest as manifest_logic
import policies
import releases as release_logic
import state as state_store
import supervisor
import sync as sync_logic
import targets as target_model
import ui
import web


_CTX = app_context.create_default_context()

_PATH_ATTRS = {
    "ADDON_CONFIG_PATH": "addon_config_path",
    "OPTIONS_PATH": "options_path",
    "STATE_PATH": "state_path",
    "RELEASES_DIR": "releases_dir",
    "DATA_DIR": "data_dir",
    "CONFIG_DIR": "config_dir",
    "ADDON_CONFIGS_DIR": "addon_configs_dir",
    "WORK_DIR": "work_dir",
    "GENERATED_DEPLOY_KEY_PATH": "generated_deploy_key_path",
    "GENERATED_DEPLOY_KEY_PUB_PATH": "generated_deploy_key_pub_path",
}
_VALUE_ATTRS = {"HOST": "host", "PORT": "port", "RUN_LOCK": "run_lock"}
_PATCHABLE_METHODS = {
    "run_command",
    "log",
    "add_detail",
    "get_installed_addons",
    "get_addon_info",
    "addon_action",
    "core_stop",
    "core_start",
    "core_restart",
    "core_reload_lovelace",
    "core_reload_yaml",
    "do_core_check",
    "backup_mount_info",
    "default_backup_mount",
    "create_ha_backup",
    "backup_manager_info",
    "latest_system_backup_status",
    "ensure_fresh_system_backup",
    "device_registry_fingerprint",
    "git_env",
    "git_pull_rebase",
    "stage_all",
    "push_branch",
    "apply_targets",
    "export_targets",
    "build_apply_preview",
    "restore_release_snapshot",
    "stage_homeassistant_storage_allowlist",
}
_CONTEXT_METHODS = {
    name for name in dir(app_context.AppContext) if not name.startswith("_")
}
_BACKUP_EXPORTS = {
    "parse_backup_date",
    "backup_slug",
    "backup_name",
    "backup_locations",
    "backup_has_location",
    "is_system_backup",
    "backup_age_hours",
    "backup_age_seconds",
    "backup_status_message",
    "find_backup_by_slug",
}
_MANIFEST_EXPORTS = {
    "default_homeassistant_manifest",
    "default_addon_target",
    "addon_target_slug",
    "organizer_target_enabled",
    "selected_addon_target",
    "resolve_addon_slug",
    "addon_by_slug",
    "path_from_metadata",
    "validate_target_id",
    "repo_source_path",
}
_SYNC_EXPORTS = {
    "has_managed_content",
    "ensure_dir",
    "safe_preview_name",
    "fingerprint_text",
    "count_preview_deletions",
}
_UI_EXPORTS = {
    "render_conflicts",
    "render_targets",
    "render_releases",
    "targets_allow_protected_storage",
}
_MODULE_EXPORTS = {
    **{name: (backup_policy, name) for name in _BACKUP_EXPORTS},
    **{name: (manifest_logic, name) for name in _MANIFEST_EXPORTS},
    **{name: (sync_logic, name) for name in _SYNC_EXPORTS},
    **{name: (ui, name) for name in _UI_EXPORTS},
    "default_state": (state_store, "default_state"),
    "supervisor_ok": (supervisor, "supervisor_ok"),
    "safe_repo_relative_path": (git_ops, "safe_repo_relative_path"),
    "target_apply_delete": (target_model, "apply_delete"),
    "target_save_delete": (target_model, "save_delete"),
    "target_restore_delete": (target_model, "restore_delete"),
}


def _refresh_generated_key_paths():
    _CTX.generated_deploy_key_path = _CTX.work_dir / "generated_deploy_key"
    _CTX.generated_deploy_key_pub_path = _CTX.work_dir / "generated_deploy_key.pub"


def _context_func(name):
    value = getattr(_CTX, name)
    if callable(value):
        return value
    return None


def _legacy_func(name):
    if name == "context":
        return lambda: _CTX
    if name in _CONTEXT_METHODS:
        value = _context_func(name)
        if value is not None:
            return value
    if name in _MODULE_EXPORTS:
        module, attr = _MODULE_EXPORTS[name]
        return getattr(module, attr)
    if name == "git_commit":
        return lambda repo_dir, ref: git_ops.git_commit(repo_dir, ref, _CTX.run_command)
    if name == "git_ref_exists":
        return lambda repo_dir, ref: git_ops.git_ref_exists(repo_dir, ref, _CTX.run_command)
    if name == "git_remote_head":
        return lambda repo_dir, env, branch: git_ops.git_remote_head(repo_dir, env, branch, _CTX.run_command)
    if name == "manifest_with_selected_addons":
        return lambda manifest, addons=None: manifest_logic.manifest_with_selected_addons(
            manifest, _CTX.selected_addon_slugs(), addons, _CTX.homeassistant_organizer_preference()
        )
    if name == "addon_config_path_candidates":
        return lambda target, slug, addon: manifest_logic.addon_config_path_candidates(
            target, slug, addon, _CTX.addon_configs_dir, _CTX.config_dir, _CTX.addon_is_zigbee2mqtt
        )
    if name == "resolve_addon_live_path":
        return lambda target, slug, addons: manifest_logic.resolve_addon_live_path(
            target, slug, addons, _CTX.addon_configs_dir, _CTX.config_dir, _CTX.addon_is_zigbee2mqtt
        )
    if name == "sync_tree":
        return lambda src, dest, delete=True, excludes=None: _CTX.sync_tree(src, dest, delete, excludes)
    if name == "export_tree":
        return lambda src, dest, delete=True: _CTX.export_tree(src, dest, delete)
    if name == "clean_export_destination":
        return lambda dest: sync_logic.clean_export_destination(
            dest,
            policies.EXPORT_CLEAN_PATHS,
            policies.EXPORT_CLEAN_DIR_NAMES,
            policies.EXPORT_CLEAN_FILE_PATTERNS,
        )
    if name == "export_storage_allowlist":
        return lambda src, dest: sync_logic.export_storage_allowlist(src, dest, policies.STORAGE_EXPORT_ALLOWLIST)
    if name == "copy_homeassistant_path_allowlist":
        return lambda src, dest, paths: sync_logic.copy_homeassistant_path_allowlist(
            src, dest, paths, policies.EXPORT_EXCLUDES, _CTX.run_command
        )
    if name == "copy_export_path":
        return lambda src, dest: sync_logic.copy_export_path(src, dest, policies.EXPORT_EXCLUDES, _CTX.run_command)
    if name == "sync_homeassistant_path_allowlist":
        return lambda src, dest, paths: sync_logic.sync_homeassistant_path_allowlist(
            src, dest, paths, policies.EXPORT_EXCLUDES, _CTX.run_command
        )
    if name == "sync_storage_allowlist":
        return lambda src, dest, allow_protected=False: sync_logic.sync_storage_allowlist(
            src, dest, policies.STORAGE_EXPORT_ALLOWLIST, policies.PROTECTED_STORAGE_FILES, allow_protected
        )
    if name == "source_has_applicable_storage":
        return lambda path, allow_protected=False: sync_logic.source_has_applicable_storage(
            path, policies.STORAGE_EXPORT_ALLOWLIST, policies.PROTECTED_STORAGE_FILES, allow_protected
        )
    if name == "safe_release_dir":
        return lambda release_name: release_logic.safe_release_dir(release_name, _CTX.release_deps())
    if name == "release_created_at":
        return lambda path: release_logic.release_created_at(path, _CTX.release_deps())
    if name == "sync_to_preview":
        return lambda target, preview_path: sync_logic.sync_to_preview(target, Path(target["live_path"]), preview_path, _CTX.sync_deps())
    if name == "target_diff":
        return lambda target, preview_path: sync_logic.target_diff(target, Path(target["live_path"]), preview_path, _CTX.run_command)
    if name == "build_apply_diff":
        return lambda resolved_targets: _CTX.build_apply_preview(resolved_targets)["diff"]
    if name == "start_apply":
        return lambda: web.start_background(_CTX.run_apply_job)
    if name == "start_preview":
        return lambda: web.start_background(_CTX.run_preview_job)
    if name == "start_save_preview":
        return lambda: web.start_background(_CTX.run_save_preview_job)
    if name == "start_reset_git_state":
        return lambda: web.start_background(_CTX.run_reset_git_state_job)
    if name == "start_disk_usage":
        return lambda: web.start_background(_CTX.run_disk_usage_job)
    if name == "start_save":
        return lambda: web.start_background(_CTX.run_save_job)
    if name == "start_deleted_devices_preview":
        return lambda: web.start_background(_CTX.run_deleted_devices_preview_job)
    if name == "start_deleted_devices_delete":
        return lambda: web.start_background(_CTX.run_deleted_devices_delete_job)
    if name == "start_deleted_devices_confirm":
        return lambda: web.start_background(_CTX.run_deleted_devices_confirm_job)
    if name == "start_deleted_devices_revert":
        return lambda: web.start_background(_CTX.run_deleted_devices_revert_job)
    if name == "start_rollback":
        return lambda release_name: web.start_background(_CTX.run_rollback_job, release_name)
    if name == "resolve_save_unknown_base_conflict":
        return lambda path, choice: conflicts.resolve_save_unknown_base_conflict(_CTX, path, choice)
    if name == "finish_git_conflict_resolution":
        return lambda repo_dir, env, branch: conflicts.finish_git_conflict_resolution(_CTX, repo_dir, env, branch)
    if name == "resolve_git_conflict":
        return lambda path, choice: conflicts.resolve_git_conflict(_CTX, path, choice)
    if name == "current_manifest_preview":
        return lambda: web.current_manifest_preview(_CTX)
    if name == "addon_slug_value":
        return web.addon_slug_value
    if name == "addon_display_name":
        return web.addon_display_name
    if name == "render_addons":
        return lambda: web.render_addons(_CTX)
    if name == "render_git_auth":
        return lambda options: ui.render_git_auth(options, _CTX.git_auth_mode, _CTX.load_generated_public_key)
    if name == "render_page":
        return lambda: web.render_page(_CTX)
    return None


class _ServerModule(ModuleType):
    def __getattribute__(self, name):
        if name in _PATH_ATTRS:
            return getattr(_CTX, _PATH_ATTRS[name])
        if name in _VALUE_ATTRS:
            return getattr(_CTX, _VALUE_ATTRS[name])
        if name in _PATCHABLE_METHODS:
            return getattr(_CTX, name)
        return super().__getattribute__(name)

    def __getattr__(self, name):
        value = _legacy_func(name)
        if value is not None:
            return value
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in _PATH_ATTRS:
            setattr(_CTX, _PATH_ATTRS[name], Path(value))
            if name == "DATA_DIR":
                _CTX.work_dir = _CTX.data_dir / "work"
                _CTX.state_path = _CTX.data_dir / "state.json"
                _CTX.options_path = _CTX.data_dir / "options.json"
                _CTX.releases_dir = _CTX.data_dir / "releases"
            if name in {"DATA_DIR", "WORK_DIR"}:
                _refresh_generated_key_paths()
            return
        if name in _VALUE_ATTRS:
            setattr(_CTX, _VALUE_ATTRS[name], value)
            return
        if name in _PATCHABLE_METHODS:
            setattr(_CTX, name, value)
            return
        super().__setattr__(name, value)


def __getattr__(name):
    if name in _PATH_ATTRS:
        return getattr(_CTX, _PATH_ATTRS[name])
    if name in _VALUE_ATTRS:
        return getattr(_CTX, _VALUE_ATTRS[name])
    if name in _PATCHABLE_METHODS:
        return getattr(_CTX, name)
    value = _legacy_func(name)
    if value is not None:
        return value
    raise AttributeError(name)


_MODULE = sys.modules.get(__name__)
if _MODULE is not None:
    _MODULE.__class__ = _ServerModule

Handler = web.create_handler(_CTX)


def main():
    try:
        _CTX.log(f"Starting HA Ops { _CTX.addon_version() } on {_CTX.host}:{_CTX.port}")
        _CTX.releases_dir.mkdir(parents=True, exist_ok=True)
        _CTX.repair_startup_state()
        _CTX.log_state_summary("Startup state")
        httpd = ThreadingHTTPServer((_CTX.host, _CTX.port), web.create_handler(_CTX))
        httpd.serve_forever()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
