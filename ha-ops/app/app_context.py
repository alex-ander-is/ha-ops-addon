import os
import subprocess
import threading
from pathlib import Path

import backups as backup_policy
import disk_usage
import git_auth
import git_ops
import jobs as job_logic
import internal_id_migration
import manifest as manifest_logic
import policies
import registry_cleanup
import releases as release_logic
import state as state_store
import supervisor
import sync as sync_logic
import i18n


def _(key, **values):
    return i18n.t(key, **values)


class AppContext:
    def __init__(
        self,
        data_dir=Path("/data"),
        config_dir=Path("/homeassistant"),
        addon_configs_dir=Path("/addon_configs"),
        addon_config_path=Path("/app/config.yaml"),
        host="0.0.0.0",
        port=8099,
    ):
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir)
        self.work_dir = self.data_dir / "work"
        self.state_path = self.data_dir / "state.json"
        self.options_path = self.data_dir / "options.json"
        self.releases_dir = self.data_dir / "releases"
        self.config_dir = Path(config_dir)
        self.addon_configs_dir = Path(addon_configs_dir)
        self.addon_config_path = Path(addon_config_path)
        self.generated_deploy_key_path = self.work_dir / "generated_deploy_key"
        self.generated_deploy_key_pub_path = self.work_dir / "generated_deploy_key.pub"
        self.run_lock = threading.Lock()

    def utc_now(self):
        return state_store.utc_now()

    def release_now(self):
        return state_store.release_now()

    def local_time_zone(self, options=None):
        options = options if options is not None else self.load_options()
        configured = options.get("time_zone") or options.get("timezone")
        if configured:
            return configured

        core_config = self.config_dir / ".storage" / "core.config"
        if core_config.exists():
            try:
                data = self.load_json(core_config, {}).get("data", {})
                if data.get("time_zone"):
                    return data["time_zone"]
            except Exception:
                pass

        return os.environ.get("TZ")

    def format_time(self, value, options=None):
        return state_store.format_time(value, self.local_time_zone(options))

    def load_json(self, path, default):
        return state_store.load_json(path, default)

    def load_options(self):
        return state_store.load_options(self.options_path)

    def option_bool(self, options, name, default):
        return state_store.option_bool(options, name, default)

    def option_int(self, options, name, default, minimum=0):
        return state_store.option_int(options, name, default, minimum)

    def read_state(self):
        return state_store.read_state(self.state_path)

    def write_state(self, updates):
        if "last_details" in updates and "last_message" in updates and updates.get("last_action"):
            message = str(updates.get("last_message") or "")
            if message:
                details = updates.get("last_details")
                if not isinstance(details, list):
                    details = []
                if not details or str(details[-1]) != message:
                    details.append(message)
                updates = {**updates, "last_details": details}
        return state_store.write_state(self.state_path, updates)

    def save_push_retry_has_pending_commit(self, repo_dir, branch, state=None):
        state = state if state is not None else self.read_state()
        if not state.get("save_push_retry_pending"):
            return False
        commit = str(state.get("save_push_retry_commit") or "").strip()
        if not commit:
            return False
        return self.git_head_is_unpushed_commit(repo_dir, branch, commit)

    def save_push_retry_has_unpushed_commit(self, state=None):
        state = state if state is not None else self.read_state()
        if not state.get("save_push_retry_pending"):
            return False
        try:
            options = self.load_options()
            repo_dir = self.repo_checkout_path(options)
            if not repo_dir.exists():
                return False
            return self.save_push_retry_has_pending_commit(repo_dir, options.get("repo_branch", "main"), state)
        except Exception as exc:
            self.log(f"Preserving Save push retry state because retry status could not be verified: {exc}")
            return True

    def discard_save_push_retry_commit(self, state=None):
        state = state if state is not None else self.read_state()
        if not state.get("save_push_retry_pending"):
            return False
        commit = str(state.get("save_push_retry_commit") or "").strip()
        if not commit:
            return False
        options = self.load_options()
        repo_dir = self.repo_checkout_path(options)
        if not repo_dir.exists() or not (repo_dir / ".git").exists():
            return False
        env = self.git_env(options)
        branch = options.get("repo_branch", "main")
        self.fetch_origin(repo_dir, env)
        return git_ops.discard_unpushed_head_commit(repo_dir, env, branch, commit, self.run_command)

    def clear_display_state(self):
        state = self.read_state()
        return state_store.clear_display_state(
            self.state_path,
            preserve_save_retry=bool(state.get("save_push_retry_pending")),
        )

    def repair_startup_state(self):
        state = self.read_state()
        if (
            state.get("last_status") == "running"
            and state.get("last_action") == "deleted_devices_delete"
            and state.get("deleted_devices_rollback_path")
        ):
            return self.repair_interrupted_deleted_devices_cleanup(state)
        preserve_save_retry = self.save_push_retry_has_unpushed_commit(state)
        return state_store.repair_startup_state(
            self.state_path,
            self.utc_now(),
            self.addon_version(),
            preserve_save_retry=preserve_save_retry,
            clear_save_retry_pending=bool(state.get("save_push_retry_pending") and not preserve_save_retry),
        )

    def repair_interrupted_deleted_devices_cleanup(self, state):
        details = list(state.get("last_details") or [])
        rollback_path = state.get("deleted_devices_rollback_path")
        restored = False
        try:
            self.restore_deleted_devices_rollback(rollback_path)
            restored = True
            details.append(_("detail.restored_deleted_devices_after_restart"))
            self.core_start()
            details.append(_("detail.started_core_after_restore"))
            self.discard_deleted_devices_rollback(rollback_path)
            preview = self.build_deleted_devices_preview()
            return self.write_state(
                {
                    "last_run_at": self.utc_now(),
                    "last_status": "interrupted",
                    "last_action": "deleted_devices_delete",
                    "last_message": _("message.interrupted_deleted_devices_reverted"),
                    "last_details": details,
                    "last_deleted_devices_preview": preview["summary"],
                    "last_deleted_devices_rows": preview["rows"],
                    "last_deleted_devices_count": preview["count"],
                    "last_deleted_devices_fingerprint": preview["fingerprint"],
                    "last_deleted_devices_generated_at": self.utc_now(),
                    "deleted_devices_pending_confirmation": False,
                    "deleted_devices_rollback_path": None,
                    "deleted_devices_rollback_fingerprint": None,
                    "deleted_devices_applied_fingerprint": None,
                }
            )
        except Exception as exc:
            details.append(_("detail.startup_deleted_devices_recovery_failed", error=exc))
            try:
                self.core_start()
                details.append(_("detail.started_core_after_startup_recovery_failure"))
            except Exception as start_exc:
                details.append(_("detail.failed_start_core_after_startup_recovery_failure", error=start_exc))
            updates = {
                "last_run_at": self.utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_delete",
                "last_message": _("message.interrupted_deleted_devices_manual_recovery"),
                "last_details": details,
                "deleted_devices_pending_confirmation": bool(rollback_path and not restored),
            }
            if restored:
                try:
                    preview = self.build_deleted_devices_preview()
                    updates.update(
                        {
                            "last_deleted_devices_preview": preview["summary"],
                            "last_deleted_devices_rows": preview["rows"],
                            "last_deleted_devices_count": preview["count"],
                            "last_deleted_devices_fingerprint": preview["fingerprint"],
                            "last_deleted_devices_generated_at": self.utc_now(),
                            "deleted_devices_pending_confirmation": False,
                            "deleted_devices_applied_fingerprint": None,
                        }
                    )
                except Exception:
                    pass
            return self.write_state(updates)

    def run_command(self, command, env=None, cwd=None, timeout=None):
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def log(self, message):
        print(f"[ha-ops] {message}", flush=True)

    def log_state_summary(self, prefix="State"):
        state = self.read_state()
        pending = "yes" if state.get("deleted_devices_pending_confirmation") else "no"
        rollback = "yes" if state.get("deleted_devices_rollback_path") else "no"
        conflicts = len(state.get("conflicts") or [])
        self.log(
            f"{prefix}: status={state.get('last_status')} action={state.get('last_action')} "
            f"pending_deleted_devices={pending} rollback={rollback} conflicts={conflicts}"
        )

    def add_detail(self, details, message):
        details.append(message)
        self.write_state(
            {
                "last_run_at": self.utc_now(),
                "last_details": details,
            }
        )

    def addon_version(self):
        if not self.addon_config_path.exists():
            return "unknown"
        for line in self.addon_config_path.read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
        return "unknown"

    def generated_deploy_key_exists(self):
        return git_auth.generated_deploy_key_exists(self.generated_deploy_key_path, self.generated_deploy_key_pub_path)

    def load_generated_public_key(self):
        return git_auth.load_generated_public_key(self.generated_deploy_key_pub_path)

    def git_auth_mode(self, options):
        return git_auth.git_auth_mode(options, self.generated_deploy_key_path, self.generated_deploy_key_pub_path)

    def setup_git_ssh_env(self, env, key_text=None, key_path=None):
        return git_auth.setup_git_ssh_env(env, self.work_dir, key_text, key_path)

    def git_env(self, options):
        return git_auth.git_env(options, self.work_dir, self.generated_deploy_key_path, self.generated_deploy_key_pub_path)

    def generate_deploy_key(self):
        return git_auth.generate_deploy_key(
            self.work_dir,
            self.generated_deploy_key_path,
            self.generated_deploy_key_pub_path,
            self.run_command,
            self.log,
        )

    def call_supervisor(self, method, path, payload=None, timeout=None):
        if timeout is None:
            return supervisor.call_supervisor(method, path, payload, self.run_command)

        def run_command(command):
            return self.run_command(command, timeout=timeout)

        return supervisor.call_supervisor(method, path, payload, run_command)

    def get_installed_addons(self):
        return supervisor.get_installed_addons(self.call_supervisor)

    def get_addon_info(self, slug):
        return supervisor.get_addon_info(slug, self.call_supervisor)

    def addon_action(self, slug, action):
        return supervisor.addon_action(slug, action, self.call_supervisor)

    def core_stop(self):
        return supervisor.core_stop(self.call_supervisor)

    def core_start(self):
        return supervisor.core_start(self.call_supervisor)

    def core_restart(self):
        return supervisor.core_restart(self.call_supervisor)

    def core_reload_yaml(self):
        return supervisor.core_reload_yaml(self.call_supervisor)

    def core_reload_lovelace(self):
        return supervisor.core_reload_lovelace(self.call_supervisor)

    def do_core_check(self):
        return supervisor.do_core_check(self.call_supervisor)

    def backup_mount_info(self):
        return supervisor.backup_mount_info(self.call_supervisor)

    def default_backup_mount(self):
        return supervisor.default_backup_mount(self.backup_mount_info)

    def create_ha_backup(self, name_prefix, backup_location=None):
        return supervisor.create_ha_backup(name_prefix, backup_location, self.call_supervisor, self.release_now)

    def backup_manager_info(self):
        return supervisor.backup_manager_info(self.call_supervisor)

    def mqtt_service(self):
        return supervisor.mqtt_service(self.call_supervisor)

    def latest_system_backup_status(self, options=None):
        options = options or self.load_options()
        return backup_policy.latest_system_backup_status(
            options,
            policies.DEFAULT_BACKUP_MAX_AGE_HOURS,
            self.option_int,
            self.option_bool,
            self.backup_manager_info,
        )

    def ensure_fresh_system_backup(self, options, details):
        return backup_policy.ensure_fresh_system_backup(
            options,
            details,
            self.option_bool,
            self.add_detail,
            self.latest_system_backup_status,
            self.default_backup_mount,
            self.create_ha_backup,
            self.backup_manager_info,
        )

    def repo_checkout_path(self, options):
        return git_ops.repo_checkout_path(options, self.data_dir)

    def ensure_repo(self, options, reset_to_origin=True):
        return git_ops.ensure_repo(options, self.data_dir, self.git_env, self.run_command, reset_to_origin)

    def clean_repo_untracked(self, repo_dir):
        return git_ops.clean_repo_untracked(repo_dir, self.run_command)

    def reset_repo_worktree(self, repo_dir):
        return git_ops.reset_repo_worktree(repo_dir, self.run_command)

    def git_head_or_unborn(self, repo_dir):
        return git_ops.git_head_or_unborn(repo_dir, self.run_command)

    def git_commit(self, repo_dir, ref):
        return git_ops.git_commit(repo_dir, ref, self.run_command)

    def git_has_unpushed_commits(self, repo_dir, branch):
        return git_ops.git_has_unpushed_commits(repo_dir, branch, self.run_command)

    def git_head_is_unpushed_commit(self, repo_dir, branch, commit):
        return git_ops.git_head_is_unpushed_commit(repo_dir, branch, commit, self.run_command)

    def git_conflict_paths(self, repo_dir):
        return git_ops.git_conflict_paths(repo_dir, self.run_command)

    def git_rebase_in_progress(self, repo_dir):
        return git_ops.git_rebase_in_progress(repo_dir, self.run_command)

    def git_pull_rebase(self, repo_dir, env, branch):
        return git_ops.git_pull_rebase(
            repo_dir,
            env,
            branch,
            self.run_command,
            lambda conflicts: self.write_state({"conflicts": conflicts, "conflict_type": "git_rebase"}),
        )

    def fetch_origin(self, repo_dir, env):
        return git_ops.fetch_origin(repo_dir, env, self.run_command)

    def stage_all(self, repo_dir):
        return git_ops.stage_all(repo_dir, self.run_command)

    def stage_paths(self, repo_dir, paths):
        return git_ops.stage_paths(repo_dir, paths, self.run_command)

    def git_status_porcelain(self, repo_dir):
        return git_ops.git_status_porcelain(repo_dir, self.run_command)

    def commit_if_needed(self, repo_dir, message):
        return git_ops.commit_if_needed(repo_dir, message, self.run_command, self.git_status_porcelain)

    def push_branch(self, repo_dir, env, branch):
        return git_ops.push_branch(repo_dir, env, branch, self.run_command)

    def push_commit_to_branch(self, repo_dir, env, commit, branch):
        return git_ops.push_commit_to_branch(repo_dir, env, commit, branch, self.run_command)

    def reset_branch_to_commit(self, repo_dir, env, branch, commit, hard=True):
        return git_ops.reset_branch_to_commit(repo_dir, env, branch, commit, self.run_command, hard=hard)

    def push_branch_force_with_lease(self, repo_dir, env, branch):
        return git_ops.push_branch_force_with_lease(repo_dir, env, branch, self.run_command)

    def selected_addon_slugs(self):
        return manifest_logic.selected_addon_slugs(self.read_state)

    def set_selected_addon_slugs(self, slugs):
        return manifest_logic.set_selected_addon_slugs(slugs, self.write_state)

    def homeassistant_organizer_preference(self):
        return manifest_logic.homeassistant_organizer_preference(self.read_state)

    def set_homeassistant_organizer_enabled(self, enabled):
        return manifest_logic.set_homeassistant_organizer_enabled(enabled, self.write_state)

    def load_manifest(self, repo_dir, options, addons=None):
        return manifest_logic.load_manifest(
            repo_dir,
            options,
            self.selected_addon_slugs(),
            self.load_json,
            addons,
            self.homeassistant_organizer_preference(),
        )

    def default_manifest(self, options):
        return manifest_logic.default_manifest(
            options,
            self.selected_addon_slugs(),
            self.homeassistant_organizer_preference(),
        )

    def resolve_targets(self, repo_dir, manifest, addons, require_source=True):
        options = self.load_options()
        return manifest_logic.resolve_targets(
            repo_dir,
            manifest,
            addons,
            options,
            self.config_dir,
            self.addon_configs_dir,
            self.addon_is_zigbee2mqtt,
            require_source,
        )

    def addon_is_zigbee2mqtt(self, addon):
        text = f"{addon.get('slug', '')} {addon.get('name', '')} {addon.get('description', '')}".lower()
        return "zigbee2mqtt" in text or "zigbee2mqtt" in text.replace(" ", "")

    def restart_or_start_addon(self, slug):
        info = self.get_addon_info(slug)
        state = info.get("state")
        if state == "started":
            self.addon_action(slug, "restart")
        else:
            self.addon_action(slug, "start")

    def stop_addon_for_sync(self, slug):
        info = self.get_addon_info(slug)
        was_started = info.get("state") == "started"
        if was_started:
            self.addon_action(slug, "stop")
        return was_started

    def sync_tree(self, src, dest, delete=True, excludes=None):
        return sync_logic.sync_tree(src, dest, delete, excludes, self.run_command)

    def export_tree(self, src, dest, delete=True):
        return sync_logic.export_tree(src, dest, delete, policies.EXPORT_EXCLUDES, self.run_command)

    def safe_remove_path(self, path):
        return sync_logic.safe_remove_path(path)

    def export_homeassistant_config(self, src, dest, target=None):
        return sync_logic.export_homeassistant_config(src, dest, target, self.sync_deps())

    def restore_homeassistant_config(self, src, dest, target):
        return sync_logic.restore_homeassistant_config(src, dest, target, self.sync_deps())

    def apply_homeassistant_config(self, src, dest, target, details=None):
        return sync_logic.apply_homeassistant_config(src, dest, target, self.sync_deps(), details)

    def apply_targets(self, resolved_targets, details):
        return sync_logic.apply_targets(resolved_targets, details, self.sync_deps())

    def selected_apply_targets_from_preview(self, resolved_targets, keep_ha_paths):
        return sync_logic.selected_apply_targets_from_preview(resolved_targets, keep_ha_paths, self.sync_deps())

    def export_targets(self, resolved_targets, details):
        return sync_logic.export_targets(resolved_targets, details, self.sync_deps())

    def build_save_preview(self, resolved_targets, repo_dir, details, include_redundant_data=False):
        return sync_logic.build_save_preview(resolved_targets, repo_dir, details, self.sync_deps(), include_redundant_data)

    def commit_save_merge(self, repo_dir, main_branch, resolved_targets, resolutions, message, details):
        return sync_logic.commit_save_merge(
            repo_dir,
            main_branch,
            resolved_targets,
            resolutions,
            message,
            details,
            self.sync_deps(),
        )

    def reset_service_branches_from_main(self, resolved_targets, repo_dir, main_branch, details, include_redundant_data=False):
        return sync_logic.reset_service_branches_from_main(
            resolved_targets,
            repo_dir,
            main_branch,
            details,
            self.sync_deps(),
            include_redundant_data=include_redundant_data,
        )

    def commit_apply_merge(self, repo_dir, main_branch, resolved_targets, keep_ha_paths, message, details, sync_applied_storage=False):
        return sync_logic.commit_apply_merge(
            repo_dir,
            main_branch,
            resolved_targets,
            keep_ha_paths,
            message,
            details,
            self.sync_deps(),
            sync_applied_storage=sync_applied_storage,
        )

    def delete_apply_conflict_live_deletions(
        self,
        resolved_targets,
        repo_dir,
        main_branch,
        resolutions,
        details,
        clean_git_delete_paths=None,
    ):
        return sync_logic.delete_apply_conflict_live_deletions(
            resolved_targets,
            repo_dir,
            main_branch,
            resolutions,
            details,
            self.sync_deps(),
            clean_git_delete_paths,
        )

    def build_apply_preview(self, resolved_targets, details=None, repo_dir=None, main_branch="main", prefer_local_live=False):
        return sync_logic.build_apply_preview(
            resolved_targets,
            self.sync_deps(),
            details,
            repo_dir,
            main_branch,
            prefer_local_live=prefer_local_live,
        )

    def save_unknown_base_conflicts(self, resolved_targets, repo_dir, resolutions, details, include_redundant_data=False):
        return sync_logic.save_unknown_base_conflicts(
            resolved_targets,
            repo_dir,
            resolutions,
            details,
            self.sync_deps(),
            include_redundant_data,
        )

    def restore_save_git_resolutions(self, repo_dir, resolutions, details):
        return sync_logic.restore_save_git_resolutions(repo_dir, resolutions, details, self.sync_deps())

    def restore_normalized_equal_save_worktree(self, repo_dir, resolved_targets, details):
        return sync_logic.restore_normalized_equal_save_worktree(repo_dir, resolved_targets, details, self.sync_deps())

    def normalize_changed_save_registry_worktree(self, repo_dir, resolved_targets, details):
        return sync_logic.normalize_changed_save_registry_worktree(repo_dir, resolved_targets, details, self.sync_deps())

    def build_deleted_devices_preview(self):
        return registry_cleanup.build_deleted_devices_preview(self.config_dir)

    def list_retained_discovery_topics(self):
        return registry_cleanup.list_retained_discovery_topics(self.run_command, self.mqtt_service())

    def build_retained_devices_preview(self):
        return registry_cleanup.build_stale_mqtt_discovery_preview(self.config_dir, self.list_retained_discovery_topics())

    def clear_retained_discovery_topic(self, topic):
        return registry_cleanup.publish_empty_retained_topic(self.run_command, topic, self.mqtt_service())

    def internal_ids_config_dir(self):
        options = self.load_options()
        repo_dir = self.repo_checkout_path(options)
        apply_path = options.get("apply_path") or "homeassistant"
        return repo_dir / apply_path

    def internal_ids_z2m_dirs(self):
        options = self.load_options()
        repo_dir = self.repo_checkout_path(options)
        try:
            addons = self.get_installed_addons()
        except Exception:
            addons = []
        manifest, _path = self.load_manifest(repo_dir, options, addons)
        dirs = []
        seen = set()

        def add_dir(path):
            path = Path(path)
            key = str(path)
            if key not in seen and path.exists():
                seen.add(key)
                dirs.append(path)

        add_dir(self.config_dir / "zigbee2mqtt")
        for target in manifest.get("targets", []):
            if target.get("type") != "addon":
                continue
            target_id = str(target.get("id") or "")
            slug = manifest_logic.addon_target_slug(target, addons)
            addon = manifest_logic.addon_by_slug(addons, slug) if slug else {}
            candidate = {
                "slug": slug or target.get("addon_slug") or target.get("id") or "",
                "name": " ".join(
                    str(value or "")
                    for value in (
                        target.get("name"),
                        target.get("addon_name_contains"),
                        addon.get("name"),
                        target.get("source"),
                    )
                ),
                "description": addon.get("description") or "",
            }
            if not self.addon_is_zigbee2mqtt(candidate):
                continue
            try:
                add_dir(manifest_logic.repo_source_path(repo_dir, target.get("source", ""), target_id))
            except RuntimeError:
                pass
            if slug:
                for path in manifest_logic.addon_config_path_candidates(
                    target,
                    slug,
                    addon,
                    self.addon_configs_dir,
                    self.config_dir,
                    self.addon_is_zigbee2mqtt,
                ):
                    add_dir(path)
        return dirs

    def build_internal_ids_preview(self):
        return internal_id_migration.build_internal_ids_preview(
            self.internal_ids_config_dir(),
            self.internal_ids_z2m_dirs(),
        )

    def apply_internal_ids_migration(self, expected_fingerprint, selected_paths):
        return internal_id_migration.apply_internal_ids_migration(
            self.internal_ids_config_dir(),
            expected_fingerprint,
            selected_paths,
            self.internal_ids_z2m_dirs(),
        )

    def device_registry_fingerprint(self):
        return registry_cleanup.device_registry_fingerprint(self.config_dir)

    def clear_deleted_devices(self, expected_fingerprint):
        return registry_cleanup.clear_deleted_devices(self.config_dir, expected_fingerprint)

    def create_deleted_devices_rollback(self, expected_fingerprint):
        return registry_cleanup.create_deleted_devices_rollback(self.config_dir, self.work_dir, expected_fingerprint)

    def deleted_devices_cleanup_status(self, rollback_path):
        return registry_cleanup.deleted_devices_cleanup_status(self.config_dir, rollback_path)

    def deleted_devices_pending_diff(self, rollback_path):
        return registry_cleanup.deleted_devices_pending_diff(self.config_dir, rollback_path)

    def restore_deleted_devices_rollback(self, rollback_path):
        return registry_cleanup.restore_deleted_devices_rollback(self.config_dir, rollback_path)

    def discard_deleted_devices_rollback(self, rollback_path):
        return registry_cleanup.discard_deleted_devices_rollback(rollback_path)

    def clear_tree(self, dest):
        return sync_logic.clear_tree(dest, self.work_dir, self.run_command)

    def sync_deps(self):
        return sync_logic.SyncContext(
            add_detail=self.add_detail,
            addon_action=self.addon_action,
            clean_dir_names=policies.EXPORT_CLEAN_DIR_NAMES,
            clean_file_patterns=policies.EXPORT_CLEAN_FILE_PATTERNS,
            clean_paths=policies.EXPORT_CLEAN_PATHS,
            core_reload_lovelace=self.core_reload_lovelace,
            core_restart=self.core_restart,
            core_reload_yaml=self.core_reload_yaml,
            core_start=self.core_start,
            core_stop=self.core_stop,
            do_core_check=self.do_core_check,
            export_excludes=policies.EXPORT_EXCLUDES,
            ha_dirs=policies.HOMEASSISTANT_EXPORT_DIRS,
            ha_root_excludes=policies.HOMEASSISTANT_EXPORT_ROOT_EXCLUDES,
            ha_root_patterns=policies.HOMEASSISTANT_EXPORT_ROOT_PATTERNS,
            log=self.log,
            protected_storage_files=policies.PROTECTED_STORAGE_FILES,
            restart_or_start_addon=self.restart_or_start_addon,
            run_command=self.run_command,
            stop_addon_for_sync=self.stop_addon_for_sync,
            storage_allowlist=policies.STORAGE_EXPORT_ALLOWLIST,
            work_dir=self.work_dir,
            zigbee2mqtt_paths=policies.ZIGBEE2MQTT_CONFIG_PATHS,
        )

    def release_deps(self):
        return release_logic.ReleaseContext(
            add_detail=self.add_detail,
            addon_action=self.addon_action,
            clear_tree=self.clear_tree,
            core_reload_lovelace=self.core_reload_lovelace,
            core_reload_yaml=self.core_reload_yaml,
            core_restart=self.core_restart,
            core_start=self.core_start,
            core_stop=self.core_stop,
            export_homeassistant_config=self.export_homeassistant_config,
            export_tree=self.export_tree,
            load_json=self.load_json,
            option_int=self.option_int,
            parse_backup_date=backup_policy.parse_backup_date,
            release_now=self.release_now,
            releases_dir=self.releases_dir,
            restart_or_start_addon=self.restart_or_start_addon,
            restore_homeassistant_config=self.restore_homeassistant_config,
            safe_remove_path=self.safe_remove_path,
            stop_addon_for_sync=self.stop_addon_for_sync,
            sync_deps=self.sync_deps,
            sync_tree=self.sync_tree,
            utc_now=self.utc_now,
        )

    def create_release_snapshot(self, resolved_targets, commit, backup_slug):
        return release_logic.create_release_snapshot(resolved_targets, commit, backup_slug, self.release_deps())

    def prune_release_snapshots(self, options, protected_release=None):
        return release_logic.prune_release_snapshots(options, protected_release, self.release_deps())

    def restore_release_snapshot(self, release_name, details, core_already_stopped=False):
        return release_logic.restore_release_snapshot(release_name, details, core_already_stopped, self.release_deps())

    def list_releases(self):
        return release_logic.list_releases(self.release_deps())

    def stage_homeassistant_storage_allowlist(self, repo_dir, options, details):
        manifest, _manifest_path = self.load_manifest(repo_dir, options)
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

        add = self.run_command(["git", "add", "-f", "--"] + paths, cwd=repo_dir)
        if add.returncode != 0:
            raise RuntimeError(f"git add allowlisted .storage failed:\n{add.stderr.strip()}")

        self.add_detail(details, _("detail.staged_storage_allowlist", count=len(paths)))
        return len(paths)

    def ensure_preview_matches_state(self, state, commit, preview):
        if state.get("last_preview_commit") != commit:
            raise i18n.error("error.preview_commit_mismatch")
        if state.get("last_preview_live_fingerprints") != preview.get("live_fingerprints", {}):
            raise i18n.error("error.preview_live_changed")
        if state.get("last_preview_fingerprint") != preview["fingerprint"]:
            raise i18n.error("error.preview_diff_changed")

    def approve_storage_apply_targets(self, resolved_targets):
        approved = []
        for target in resolved_targets:
            if target.get("type") == "homeassistant":
                updated = dict(target)
                updated["allow_protected_storage"] = True
                approved.append(updated)
            else:
                approved.append(target)
        return approved

    def enforce_apply_limits(self, options, preview):
        max_deletions = self.option_int(options, "max_apply_deletions", policies.DEFAULT_MAX_APPLY_DELETIONS, minimum=0)
        if preview["deletions"] > max_deletions:
            raise i18n.error("error.apply_delete_limit", deletions=preview["deletions"], limit=max_deletions)

    def job_deps(self):
        return job_logic.JobContext(
            add_detail=self.add_detail,
            apply_targets=self.apply_targets,
            build_apply_preview=self.build_apply_preview,
            build_disk_usage_summary=self.build_disk_usage_summary,
            build_save_preview=self.build_save_preview,
            build_deleted_devices_preview=self.build_deleted_devices_preview,
            build_internal_ids_preview=self.build_internal_ids_preview,
            build_retained_devices_preview=self.build_retained_devices_preview,
            clean_repo_untracked=self.clean_repo_untracked,
            clear_deleted_devices=self.clear_deleted_devices,
            clear_retained_discovery_topic=self.clear_retained_discovery_topic,
            commit_if_needed=self.commit_if_needed,
            commit_apply_merge=self.commit_apply_merge,
            delete_apply_conflict_live_deletions=self.delete_apply_conflict_live_deletions,
            commit_save_merge=self.commit_save_merge,
            core_start=self.core_start,
            core_stop=self.core_stop,
            create_release_snapshot=self.create_release_snapshot,
            create_deleted_devices_rollback=self.create_deleted_devices_rollback,
            deleted_devices_cleanup_status=self.deleted_devices_cleanup_status,
            device_registry_fingerprint=self.device_registry_fingerprint,
            discard_deleted_devices_rollback=self.discard_deleted_devices_rollback,
            enforce_apply_limits=self.enforce_apply_limits,
            ensure_fresh_system_backup=self.ensure_fresh_system_backup,
            ensure_preview_matches_state=self.ensure_preview_matches_state,
            ensure_repo=self.ensure_repo,
            export_targets=self.export_targets,
            fetch_origin=self.fetch_origin,
            get_installed_addons=self.get_installed_addons,
            git_conflict_paths=self.git_conflict_paths,
            git_env=self.git_env,
            git_commit=self.git_commit,
            git_has_unpushed_commits=self.git_has_unpushed_commits,
            save_push_retry_has_pending_commit=self.save_push_retry_has_pending_commit,
            git_head_or_unborn=self.git_head_or_unborn,
            git_pull_rebase=self.git_pull_rebase,
            git_status_porcelain=self.git_status_porcelain,
            load_manifest=self.load_manifest,
            load_options=self.load_options,
            log=self.log,
            option_bool=self.option_bool,
            prune_release_snapshots=self.prune_release_snapshots,
            push_branch=self.push_branch,
            push_commit_to_branch=self.push_commit_to_branch,
            push_branch_force_with_lease=self.push_branch_force_with_lease,
            read_state=self.read_state,
            release_now=self.release_now,
            reset_branch_to_commit=self.reset_branch_to_commit,
            repo_checkout_path=self.repo_checkout_path,
            reset_repo_worktree=self.reset_repo_worktree,
            reset_service_branches_from_main=self.reset_service_branches_from_main,
            normalize_changed_save_registry_worktree=self.normalize_changed_save_registry_worktree,
            restore_normalized_equal_save_worktree=self.restore_normalized_equal_save_worktree,
            restore_save_git_resolutions=self.restore_save_git_resolutions,
            resolve_targets=self.resolve_targets,
            selected_apply_targets_from_preview=self.selected_apply_targets_from_preview,
            approve_storage_apply_targets=self.approve_storage_apply_targets,
            restore_deleted_devices_rollback=self.restore_deleted_devices_rollback,
            restore_release_snapshot=self.restore_release_snapshot,
            apply_internal_ids_migration=self.apply_internal_ids_migration,
            run_lock=self.run_lock,
            save_unknown_base_conflicts=self.save_unknown_base_conflicts,
            stage_all=self.stage_all,
            stage_paths=self.stage_paths,
            stage_homeassistant_storage_allowlist=self.stage_homeassistant_storage_allowlist,
            utc_now=self.utc_now,
            write_state=self.write_state,
        )

    def run_save_job(self, commit_subject=None, lock_acquired=False):
        return job_logic.run_save_job(self.job_deps(), commit_subject=commit_subject, lock_acquired=lock_acquired)

    def run_apply_job(self, lock_acquired=False):
        return job_logic.run_apply_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_preview_job(self, lock_acquired=False):
        return job_logic.run_preview_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_save_preview_job(self, lock_acquired=False):
        return job_logic.run_save_preview_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_reset_git_state_job(self, lock_acquired=False):
        return job_logic.run_reset_git_state_job(self.job_deps(), lock_acquired=lock_acquired)

    def build_disk_usage_summary(self):
        return disk_usage.build_disk_usage_summary(
            self.config_dir,
            self.data_dir,
            self.addon_configs_dir,
            Path("/backup"),
            self.run_command,
            self.call_supervisor,
        )

    def run_disk_usage_job(self, lock_acquired=False):
        return job_logic.run_disk_usage_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_deleted_devices_preview_job(self, lock_acquired=False):
        return job_logic.run_deleted_devices_preview_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_retained_devices_preview_job(self, lock_acquired=False):
        return job_logic.run_retained_devices_preview_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_retained_devices_delete_job(self, selected, lock_acquired=False):
        return job_logic.run_retained_devices_delete_job(selected, self.job_deps(), lock_acquired=lock_acquired)

    def run_internal_ids_preview_job(self, lock_acquired=False):
        return job_logic.run_internal_ids_preview_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_internal_ids_migrate_job(self, selected, lock_acquired=False):
        return job_logic.run_internal_ids_migrate_job(selected, self.job_deps(), lock_acquired=lock_acquired)

    def run_deleted_devices_delete_job(self, lock_acquired=False):
        return job_logic.run_deleted_devices_delete_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_deleted_devices_confirm_job(self, lock_acquired=False):
        return job_logic.run_deleted_devices_confirm_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_deleted_devices_revert_job(self, lock_acquired=False):
        return job_logic.run_deleted_devices_revert_job(self.job_deps(), lock_acquired=lock_acquired)

    def run_rollback_job(self, release_name, lock_acquired=False):
        return job_logic.run_rollback_job(release_name, self.job_deps(), lock_acquired=lock_acquired)


def create_default_context():
    return AppContext()
