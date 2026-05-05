import subprocess
import threading
from pathlib import Path

import backups as backup_policy
import git_auth
import git_ops
import jobs as job_logic
import manifest as manifest_logic
import policies
import releases as release_logic
import state as state_store
import supervisor
import sync as sync_logic


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
        return state_store.write_state(self.state_path, updates)

    def run_command(self, command, env=None, cwd=None):
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def log(self, message):
        print(f"[ha-ops] {message}", flush=True)

    def add_detail(self, details, message):
        details.append(message)
        self.write_state(
            {
                "last_run_at": self.utc_now(),
                "last_status": "running",
                "last_message": message,
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

    def call_supervisor(self, method, path, payload=None):
        return supervisor.call_supervisor(method, path, payload, self.run_command)

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

    def git_has_unpushed_commits(self, repo_dir, branch):
        return git_ops.git_has_unpushed_commits(repo_dir, branch, self.run_command)

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

    def stage_all(self, repo_dir):
        return git_ops.stage_all(repo_dir, self.run_command)

    def git_status_porcelain(self, repo_dir):
        return git_ops.git_status_porcelain(repo_dir, self.run_command)

    def commit_if_needed(self, repo_dir, message):
        return git_ops.commit_if_needed(repo_dir, message, self.run_command, self.git_status_porcelain)

    def push_branch(self, repo_dir, env, branch):
        return git_ops.push_branch(repo_dir, env, branch, self.run_command)

    def selected_addon_slugs(self):
        return manifest_logic.selected_addon_slugs(self.read_state)

    def set_selected_addon_slugs(self, slugs):
        return manifest_logic.set_selected_addon_slugs(slugs, self.write_state)

    def load_manifest(self, repo_dir, options, addons=None):
        return manifest_logic.load_manifest(repo_dir, options, self.selected_addon_slugs(), self.load_json, addons)

    def default_manifest(self, options):
        return manifest_logic.default_manifest(options, self.selected_addon_slugs())

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

    def export_targets(self, resolved_targets, details):
        return sync_logic.export_targets(resolved_targets, details, self.sync_deps())

    def build_apply_preview(self, resolved_targets):
        return sync_logic.build_apply_preview(resolved_targets, self.sync_deps())

    def save_unknown_base_conflicts(self, resolved_targets, repo_dir, resolutions, details):
        return sync_logic.save_unknown_base_conflicts(resolved_targets, repo_dir, resolutions, details, self.sync_deps())

    def restore_save_git_resolutions(self, repo_dir, resolutions, details):
        return sync_logic.restore_save_git_resolutions(repo_dir, resolutions, details, self.sync_deps())

    def clear_tree(self, dest):
        return sync_logic.clear_tree(dest, self.work_dir, self.run_command)

    def sync_deps(self):
        return sync_logic.SyncContext(
            add_detail=self.add_detail,
            addon_action=self.addon_action,
            clean_dir_names=policies.EXPORT_CLEAN_DIR_NAMES,
            clean_file_patterns=policies.EXPORT_CLEAN_FILE_PATTERNS,
            clean_paths=policies.EXPORT_CLEAN_PATHS,
            core_restart=self.core_restart,
            core_reload_yaml=self.core_reload_yaml,
            core_start=self.core_start,
            core_stop=self.core_stop,
            do_core_check=self.do_core_check,
            export_excludes=policies.EXPORT_EXCLUDES,
            ha_dirs=policies.HOMEASSISTANT_EXPORT_DIRS,
            ha_root_excludes=policies.HOMEASSISTANT_EXPORT_ROOT_EXCLUDES,
            ha_root_patterns=policies.HOMEASSISTANT_EXPORT_ROOT_PATTERNS,
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

        self.add_detail(details, f"Staged {len(paths)} allowlisted .storage config file(s).")
        return len(paths)

    def ensure_preview_matches_state(self, state, commit, preview):
        if state.get("last_preview_commit") != commit:
            raise RuntimeError("Run Preview Git to HA before Apply Git to HA. The preview commit does not match.")
        if state.get("last_preview_fingerprint") != preview["fingerprint"]:
            raise RuntimeError("Run Preview Git to HA again. The live diff changed since the last preview.")

    def enforce_apply_limits(self, options, preview):
        max_deletions = self.option_int(options, "max_apply_deletions", policies.DEFAULT_MAX_APPLY_DELETIONS, minimum=0)
        if preview["deletions"] > max_deletions:
            raise RuntimeError(
                f"Apply would delete {preview['deletions']} file(s), above the limit of {max_deletions}. Review the preview or raise max_apply_deletions."
            )

    def job_deps(self):
        return job_logic.JobContext(
            add_detail=self.add_detail,
            apply_targets=self.apply_targets,
            build_apply_preview=self.build_apply_preview,
            commit_if_needed=self.commit_if_needed,
            create_release_snapshot=self.create_release_snapshot,
            enforce_apply_limits=self.enforce_apply_limits,
            ensure_fresh_system_backup=self.ensure_fresh_system_backup,
            ensure_preview_matches_state=self.ensure_preview_matches_state,
            ensure_repo=self.ensure_repo,
            export_targets=self.export_targets,
            get_installed_addons=self.get_installed_addons,
            git_conflict_paths=self.git_conflict_paths,
            git_env=self.git_env,
            git_has_unpushed_commits=self.git_has_unpushed_commits,
            git_head_or_unborn=self.git_head_or_unborn,
            git_pull_rebase=self.git_pull_rebase,
            load_manifest=self.load_manifest,
            load_options=self.load_options,
            option_bool=self.option_bool,
            prune_release_snapshots=self.prune_release_snapshots,
            push_branch=self.push_branch,
            read_state=self.read_state,
            release_now=self.release_now,
            repo_checkout_path=self.repo_checkout_path,
            reset_repo_worktree=self.reset_repo_worktree,
            restore_save_git_resolutions=self.restore_save_git_resolutions,
            resolve_targets=self.resolve_targets,
            restore_release_snapshot=self.restore_release_snapshot,
            run_lock=self.run_lock,
            save_unknown_base_conflicts=self.save_unknown_base_conflicts,
            stage_all=self.stage_all,
            stage_homeassistant_storage_allowlist=self.stage_homeassistant_storage_allowlist,
            utc_now=self.utc_now,
            write_state=self.write_state,
        )

    def run_save_job(self):
        return job_logic.run_save_job(self.job_deps())

    def run_apply_job(self):
        return job_logic.run_apply_job(self.job_deps())

    def run_preview_job(self):
        return job_logic.run_preview_job(self.job_deps())

    def run_rollback_job(self, release_name):
        return job_logic.run_rollback_job(release_name, self.job_deps())


def create_default_context():
    return AppContext()
