from dataclasses import dataclass
from typing import Any

import i18n
import state as state_store


def _(key, **values):
    return i18n.t(key, **values)


def enter_run_lock(ctx, action, lock_acquired=False):
    if lock_acquired:
        return True
    run_lock = ctx.run_lock
    if not run_lock.acquire(blocking=False):
        ctx.write_state(
            {
                "last_run_at": ctx.utc_now(),
                "last_status": "busy",
                "last_action": action,
                "last_message": _("error.running_action"),
            }
        )
        return False
    return True


def release_run_lock(ctx):
    ctx.run_lock.release()


def service_branch_push_out_of_date(branch, error):
    if branch != "ha-ops/base":
        return False
    text = str(error).lower()
    return any(
        token in text
        for token in (
            "non-fast-forward",
            "updates were rejected",
            "fetch first",
            "behind its remote",
            "[rejected]",
        )
    )


def record_service_branch_push_failure(ctx, details, branch, error):
    if branch == "ha-ops/base":
        ctx.add_detail(details, _("detail.skipped_ha_base_push", error=error))
    else:
        ctx.add_detail(details, _("detail.skipped_branch_push", branch=branch, error=error))
    out_of_date = service_branch_push_out_of_date(branch, error)
    if out_of_date:
        ctx.add_detail(details, _("detail.git_state_out_of_date_reset"))
    return out_of_date


@dataclass(frozen=True)
class JobContext:
    add_detail: Any
    apply_targets: Any
    build_deleted_devices_preview: Any
    build_disk_usage_summary: Any
    build_internal_ids_preview: Any
    build_retained_devices_preview: Any
    build_apply_preview: Any
    build_save_preview: Any
    clean_repo_untracked: Any
    clear_deleted_devices: Any
    clear_retained_discovery_topic: Any
    commit_if_needed: Any
    commit_apply_merge: Any
    delete_apply_conflict_live_deletions: Any
    core_start: Any
    core_stop: Any
    create_deleted_devices_rollback: Any
    create_release_snapshot: Any
    deleted_devices_cleanup_status: Any
    device_registry_fingerprint: Any
    discard_deleted_devices_rollback: Any
    enforce_apply_limits: Any
    ensure_fresh_system_backup: Any
    ensure_preview_matches_state: Any
    ensure_repo: Any
    export_targets: Any
    get_installed_addons: Any
    git_conflict_paths: Any
    git_env: Any
    git_has_unpushed_commits: Any
    git_head_or_unborn: Any
    git_pull_rebase: Any
    git_status_porcelain: Any
    commit_save_merge: Any
    load_manifest: Any
    load_options: Any
    log: Any
    option_bool: Any
    prune_release_snapshots: Any
    push_branch: Any
    push_branch_force_with_lease: Any
    read_state: Any
    release_now: Any
    repo_checkout_path: Any
    reset_repo_worktree: Any
    reset_service_branches_from_main: Any
    normalize_changed_save_registry_worktree: Any
    restore_normalized_equal_save_worktree: Any
    restore_save_git_resolutions: Any
    resolve_targets: Any
    selected_apply_targets_from_preview: Any
    approve_storage_apply_targets: Any
    restore_deleted_devices_rollback: Any
    restore_release_snapshot: Any
    apply_internal_ids_migration: Any
    run_lock: Any
    save_unknown_base_conflicts: Any
    stage_all: Any
    stage_paths: Any
    stage_homeassistant_storage_allowlist: Any
    utc_now: Any
    write_state: Any


def conflict_status_message(state, default_message=None):
    default_message = default_message or _("message.resolve_git_conflicts")
    if state.get("conflict_type") == "save_unknown_base":
        return _("message.resolve_unknown_base_save_conflicts")
    return default_message


def save_preview_matches_state(state, commit, preview):
    return (
        state.get("last_save_preview_commit") == commit
        and state.get("last_save_preview_fingerprint") == preview.get("fingerprint")
    )


def save_preview_resolutions_for_current_preview(state, commit, preview):
    paths = list(preview.get("paths") or [])
    if not paths or not save_preview_matches_state(state, commit, preview):
        return dict(state.get("save_conflict_resolutions", {}))

    selected = selected_preview_paths(state, paths, "save_preview_selected_paths")
    if not selected:
        raise RuntimeError(_("message.select_preview_files"))
    stored = dict(state.get("save_preview_resolutions", {}))
    selected_set = set(selected)
    conflict_paths = set(preview.get("conflicts") or [])
    if preview.get("conflicts"):
        missing = [path for path in selected if path in conflict_paths and path not in stored]
        if missing:
            raise RuntimeError(_("message.choose_save_preview_conflicts", count=len(missing)))
    return {path: stored.get(path, "ha") if path in selected_set else "git" for path in paths}


def selected_preview_paths(state, paths, key):
    selected = state.get(key)
    if selected is None:
        return []
    selected_set = {str(item) for item in selected if str(item)}
    return [path for path in paths if path in selected_set]


def preview_changed_message():
    return _("message.preview_changed")


def apply_preview_resolutions_for_current_preview(state, preview):
    paths = list(preview.get("paths") or [])
    selected = selected_preview_paths(state, paths, "apply_preview_selected_paths")
    if paths and not selected:
        raise RuntimeError(_("message.select_preview_files"))
    stored = dict(state.get("apply_preview_resolutions", {}))
    selected_set = set(selected)
    conflict_paths = set(preview.get("conflicts") or [])
    if preview.get("conflicts"):
        missing = [path for path in selected if path in conflict_paths and path not in stored]
        if missing:
            raise RuntimeError(_("message.choose_apply_preview_conflicts", count=len(missing)))
    return {path: stored.get(path, "git") if path in selected_set else "ha" for path in paths}


def write_pending_conflicts(ctx, action, message, details=None, targets=None):
    ctx.write_state(
        {
            "last_run_at": ctx.utc_now(),
            "last_status": "conflicts",
            "last_action": action,
            "last_message": message,
            "last_details": details or [],
            "last_targets": targets or [],
        }
    )


def log_action(ctx, message):
    try:
        ctx.log(message)
    except Exception:
        pass


def status_path(line):
    line = str(line)
    value = line[3:].strip() if len(line) >= 3 and line[2] == " " else line.split(maxsplit=1)[-1].strip()
    if " -> " in value:
        value = value.rsplit(" -> ", 1)[1]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value


def dirty_paths(ctx, repo_dir):
    return [status_path(line) for line in (ctx.git_status_porcelain(repo_dir) or "").splitlines() if line.strip()]


def internal_ids_migration_prefixes(options):
    apply_path = str(options.get("apply_path") or "homeassistant").strip().strip("/")
    prefixes = [".ha-ops/"]
    if apply_path:
        prefixes.insert(0, f"{apply_path}/.ha-ops/")
    return prefixes


def internal_ids_migration_path(path, options):
    return any(path.startswith(prefix) for prefix in internal_ids_migration_prefixes(options))


def dirty_checkout_message(paths, action):
    rendered = "\n".join(f"- {path}" for path in paths[:30])
    if len(paths) > 30:
        rendered += f"\n- and {len(paths) - 30} more"
    return (
        "Git checkout has uncommitted changes, so HA Ops cannot pull from origin.\n\n"
        f"Changed paths:\n{rendered}\n\n"
        f"Commit, save, or clean these changes before running {action}."
    )


def ensure_clean_checkout_for_pull(ctx, repo_dir, action):
    paths = dirty_paths(ctx, repo_dir)
    if paths:
        raise RuntimeError(dirty_checkout_message(paths, action))


def prepare_repo_checkout_for_sync(ctx, options, details, action):
    commit_pending_internal_ids_migration(ctx, options, details)
    repo_dir = ctx.repo_checkout_path(options)
    if repo_dir.exists() and (repo_dir / ".git").exists():
        ctx.clean_repo_untracked(repo_dir)
        ensure_clean_checkout_for_pull(ctx, repo_dir, action)


def commit_pending_internal_ids_migration(ctx, options, details):
    repo_dir = ctx.repo_checkout_path(options)
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        return None
    paths = dirty_paths(ctx, repo_dir)
    if not paths:
        return None
    if any(not internal_ids_migration_path(path, options) for path in paths):
        return None

    ctx.stage_paths(repo_dir, paths)
    commit = ctx.commit_if_needed(repo_dir, "Migrate HA Ops internal ids")
    if not commit:
        return None

    env = ctx.git_env(options)
    branch = options.get("repo_branch", "main")
    try:
        ctx.push_branch(repo_dir, env, branch)
    except RuntimeError:
        ensure_clean_checkout_for_pull(ctx, repo_dir, "pushing pending Internal IDs migration changes")
        ctx.git_pull_rebase(repo_dir, env, branch)
        ctx.push_branch(repo_dir, env, branch)
    ctx.add_detail(details, _("detail.committed_pending_internal_ids", commit=commit))
    return commit


def pending_deleted_devices_message():
    return _("message.pending_deleted_devices")


def write_pending_deleted_devices(ctx, action, details=None, targets=None):
    ctx.write_state(
        {
            "last_run_at": ctx.utc_now(),
            "last_status": "error",
            "last_action": action,
            "last_message": pending_deleted_devices_message(),
            "last_details": details or [],
            "last_targets": targets or [],
        }
    )


def refresh_deleted_devices_preview_updates(ctx):
    preview = ctx.build_deleted_devices_preview()
    return {
        "last_deleted_devices_preview": preview["summary"],
        "last_deleted_devices_rows": preview["rows"],
        "last_deleted_devices_count": preview["count"],
        "last_deleted_devices_fingerprint": preview["fingerprint"],
        "last_deleted_devices_generated_at": ctx.utc_now(),
    }


def save_change_lines(status):
    labels = {
        "?": "Untracked",
        "A": "Added",
        "C": "Copied",
        "D": "Deleted",
        "M": "Modified",
        "R": "Renamed",
        "T": "Type changed",
        "U": "Unmerged",
    }
    lines = []
    for raw_line in status.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        path = raw_line[3:].strip() if len(raw_line) > 3 else raw_line.strip()
        status_code = code[0] if code[0] != " " else code[1]
        label = labels.get(status_code, "Changed")
        lines.append(f"- {label}: {path}")
    return lines


def add_save_change_details(ctx, details, status):
    lines = save_change_lines(status)
    if not lines:
        return
    ctx.add_detail(details, "\n".join([_("detail.git_changes_prepared", count=len(lines)), *lines]))


def run_save_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "save", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    resolved_targets = []
    repo_dir = None
    checkout_dirty_for_save = False
    save_commit_created = False
    state = {}

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "save",
            "last_message": _("message.preparing_save"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "save", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(ctx, "save", conflict_status_message(state), details, resolved_targets)
            return False

        prepare_repo_checkout_for_sync(ctx, options, details, "Save HA to Git")
        repo_dir = ctx.ensure_repo(options, reset_to_origin=False)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        ctx.git_pull_rebase(repo_dir, env, branch)
        commit = ctx.git_head_or_unborn(repo_dir)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, _("detail.using_branch_commit", branch=branch, commit=commit))
        ctx.add_detail(details, _("detail.using_manifest", path=manifest_path))
        if state.get("save_push_retry_pending") and ctx.git_has_unpushed_commits(repo_dir, branch):
            ctx.add_detail(details, _("detail.retried_save_push"))
            try:
                ctx.push_branch(repo_dir, env, branch)
            except RuntimeError:
                ctx.git_pull_rebase(repo_dir, env, branch)
                ctx.push_branch(repo_dir, env, branch)
            ctx.add_detail(details, _("detail.pushed_branch", branch=branch))
            retry_preview = ctx.build_save_preview(resolved_targets, repo_dir, details, bool(state.get("include_redundant_data")))
            retry_commit = ctx.git_head_or_unborn(repo_dir)
            git_state_out_of_date = False
            for service_branch in ("ha-ops/ha-live", "ha-ops/base"):
                try:
                    ctx.push_branch(repo_dir, env, service_branch)
                    ctx.add_detail(details, _("detail.pushed_branch", branch=service_branch))
                except RuntimeError as exc:
                    git_state_out_of_date = (
                        record_service_branch_push_failure(ctx, details, service_branch, exc)
                        or git_state_out_of_date
                    )
            write_state(
                {
                    "last_run_at": utc_now(),
                    "last_status": "warning" if git_state_out_of_date else "success",
                    "last_action": "save",
                    "last_message": (
                        _("message.git_state_out_of_date")
                        if git_state_out_of_date
                        else _("message.save_finished_pushed")
                    ),
                    "last_details": details,
                    "last_targets": resolved_targets,
                    "last_save_preview": retry_preview["summary"],
                    "last_save_diff": retry_preview["diff"],
                    "last_save_diff_generated_at": utc_now(),
                    "last_save_preview_commit": retry_commit,
                    "last_save_preview_fingerprint": retry_preview["fingerprint"],
                    "last_save_preview_paths": retry_preview.get("paths", []),
                    "last_save_preview_conflicts": bool(retry_preview.get("conflicts")),
                    "last_save_preview_conflict_paths": retry_preview.get("conflicts", []),
                    "save_preview_resolutions": {},
                    "save_preview_selected_paths": [],
                    "post_apply_save_recommended": False,
                    "save_push_retry_pending": False,
                }
            )
            return True
        include_redundant_data = bool(state.get("include_redundant_data"))
        if include_redundant_data:
            ctx.add_detail(details, _("detail.including_redundant_save"))
        current_preview = ctx.build_save_preview(resolved_targets, repo_dir, details, include_redundant_data)
        commit = ctx.git_head_or_unborn(repo_dir)
        if not save_preview_matches_state(state, commit, current_preview):
            if not current_preview.get("paths"):
                ctx.add_detail(details, _("detail.no_live_changes_to_save"))
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "success",
                        "last_action": "save",
                        "last_message": _("message.no_live_changes_to_save"),
                        "last_details": details,
                        "last_targets": resolved_targets,
                        "last_save_preview": current_preview["summary"],
                        "last_save_diff": current_preview["diff"],
                        "last_save_diff_generated_at": utc_now(),
                        "last_save_preview_commit": commit,
                        "last_save_preview_fingerprint": current_preview["fingerprint"],
                        "last_save_preview_paths": [],
                        "last_save_preview_conflicts": False,
                        "last_save_preview_conflict_paths": [],
                        "save_preview_resolutions": {},
                        "save_preview_selected_paths": [],
                        "post_apply_save_recommended": False,
                    }
                )
                return True
            message = preview_changed_message()
            details.append(message)
            write_state(
                {
                    "last_run_at": utc_now(),
                    "last_status": "warning",
                    "last_action": "save",
                    "last_message": message,
                    "last_details": details,
                    "last_targets": resolved_targets,
                    "last_save_preview": current_preview["summary"],
                    "last_save_diff": current_preview["diff"],
                    "last_save_diff_generated_at": utc_now(),
                    "last_save_preview_commit": commit,
                    "last_save_preview_fingerprint": current_preview["fingerprint"],
                    "last_save_preview_paths": current_preview.get("paths", []),
                    "last_save_preview_conflicts": bool(current_preview.get("conflicts")),
                    "last_save_preview_conflict_paths": current_preview.get("conflicts", []),
                    "save_preview_resolutions": {},
                    "save_preview_selected_paths": [],
                    "post_apply_save_recommended": False,
                }
            )
            return False
        save_resolutions = save_preview_resolutions_for_current_preview(state, commit, current_preview)

        ctx.add_detail(details, _("detail.merged_live_export"))
        checkout_dirty_for_save = True
        new_commit = ctx.commit_save_merge(
            repo_dir,
            branch,
            resolved_targets,
            save_resolutions,
            f"Save Home Assistant config {ctx.release_now()}",
            details,
        )
        add_save_change_details(ctx, details, ctx.git_status_porcelain(repo_dir))

        if new_commit:
            save_commit_created = True
            ctx.add_detail(details, _("detail.created_commit", commit=new_commit))

        if ctx.git_has_unpushed_commits(repo_dir, branch):
            try:
                ctx.push_branch(repo_dir, env, branch)
            except RuntimeError:
                ctx.git_pull_rebase(repo_dir, env, branch)
                ctx.push_branch(repo_dir, env, branch)
            ctx.add_detail(details, _("detail.pushed_branch", branch=branch))
            save_message = _("message.save_finished_pushed")
        else:
            ctx.add_detail(details, _("detail.no_live_changes_to_save"))
            save_message = _("message.no_live_changes_to_save")
        post_save_preview = ctx.build_save_preview(resolved_targets, repo_dir, details, include_redundant_data)
        post_save_commit = ctx.git_head_or_unborn(repo_dir)
        git_state_out_of_date = False
        for service_branch in ("ha-ops/ha-live", "ha-ops/base"):
            try:
                ctx.push_branch(repo_dir, env, service_branch)
                ctx.add_detail(details, _("detail.pushed_branch", branch=service_branch))
            except RuntimeError as exc:
                git_state_out_of_date = (
                    record_service_branch_push_failure(ctx, details, service_branch, exc)
                    or git_state_out_of_date
                )

        write_state(
            {
                "conflicts": [],
                "conflict_type": None,
                "save_conflict_resolutions": {},
                "save_preview_resolutions": {},
                "save_preview_selected_paths": [],
                "save_push_retry_pending": False,
            }
        )

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "warning" if git_state_out_of_date else "success",
                "last_action": "save",
                "last_message": _("message.git_state_out_of_date") if git_state_out_of_date else save_message,
                "last_details": details,
                "last_targets": resolved_targets,
                "last_save_preview": post_save_preview["summary"],
                "last_save_diff": post_save_preview["diff"],
                "last_save_diff_generated_at": utc_now(),
                "last_save_preview_commit": post_save_commit,
                "last_save_preview_fingerprint": post_save_preview["fingerprint"],
                "last_save_preview_paths": post_save_preview.get("paths", []),
                "last_save_preview_conflicts": bool(post_save_preview.get("conflicts")),
                "last_save_preview_conflict_paths": post_save_preview.get("conflicts", []),
                "post_apply_save_recommended": False,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        if repo_dir and checkout_dirty_for_save and not save_commit_created:
            try:
                ctx.reset_repo_worktree(repo_dir)
                details.append(_("detail.cleaned_incomplete_save"))
            except Exception as cleanup_exc:
                details.append(_("detail.failed_to_clean_incomplete_save", error=cleanup_exc))
        try:
            repo_path = ctx.repo_checkout_path(options)
        except RuntimeError:
            repo_path = None
        conflicts = ctx.git_conflict_paths(repo_path) if repo_path and repo_path.exists() else []
        status = "conflicts" if conflicts else "error"
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": status,
                "last_action": "save",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
                "conflicts": conflicts,
                "save_push_retry_pending": bool(state.get("save_push_retry_pending") or save_commit_created),
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_save_preview_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "save_preview", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    resolved_targets = []

    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "save_preview",
            "last_message": _("message.preparing_save_preview"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "save_preview", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(
                ctx,
                "save_preview",
                conflict_status_message(state, _("message.resolve_git_conflicts_before_save_preview")),
                details,
                resolved_targets,
            )
            return False

        prepare_repo_checkout_for_sync(ctx, options, details, "Preview HA to Git")
        repo_dir = ctx.ensure_repo(options, reset_to_origin=False)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        ctx.git_pull_rebase(repo_dir, env, branch)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, _("detail.using_branch_commit", branch=branch, commit=ctx.git_head_or_unborn(repo_dir)))
        ctx.add_detail(details, _("detail.using_manifest", path=manifest_path))
        ctx.add_detail(details, _("detail.building_save_preview"))
        include_redundant_data = bool(state.get("include_redundant_data"))
        if include_redundant_data:
            ctx.add_detail(details, _("detail.including_redundant_save_preview"))
        preview = ctx.build_save_preview(resolved_targets, repo_dir, details, include_redundant_data)
        commit = ctx.git_head_or_unborn(repo_dir)
        ctx.push_branch(repo_dir, env, "ha-ops/ha-live")
        ctx.add_detail(details, _("detail.pushed_ha_live"))
        git_state_out_of_date = False
        try:
            ctx.push_branch(repo_dir, env, "ha-ops/base")
            ctx.add_detail(details, _("detail.pushed_ha_base"))
        except RuntimeError as exc:
            git_state_out_of_date = record_service_branch_push_failure(ctx, details, "ha-ops/base", exc)

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "warning" if git_state_out_of_date else "success",
                "last_action": "save_preview",
                "last_message": (
                    _("message.git_state_out_of_date")
                    if git_state_out_of_date
                    else _("message.save_preview_finished")
                ),
                "last_details": details,
                "last_targets": resolved_targets,
                "last_save_preview": preview["summary"],
                "last_save_diff": preview["diff"],
                "last_save_diff_generated_at": utc_now(),
                "last_save_preview_commit": commit,
                "last_save_preview_fingerprint": preview["fingerprint"],
                "last_save_preview_paths": preview.get("paths", []),
                "last_save_preview_conflicts": bool(preview.get("conflicts")),
                "last_save_preview_conflict_paths": preview.get("conflicts", []),
                "save_preview_resolutions": {},
                "save_preview_selected_paths": [],
                "post_apply_save_recommended": False,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "save_preview",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_reset_git_state_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "reset_git_state", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "reset_git_state",
            "last_message": _("message.resetting_git_state"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "reset_git_state", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(ctx, "reset_git_state", conflict_status_message(state), details, resolved_targets)
            return False

        prepare_repo_checkout_for_sync(ctx, options, details, "Reset Git State")
        repo_dir = ctx.ensure_repo(options)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, _("detail.using_branch_commit", branch=branch, commit=ctx.git_head_or_unborn(repo_dir)))
        ctx.add_detail(details, _("detail.using_manifest", path=manifest_path))
        ctx.add_detail(details, _("detail.resetting_git_state"))
        ctx.reset_service_branches_from_main(resolved_targets, repo_dir, branch, details)
        for service_branch in ("ha-ops/ha-live", "ha-ops/base"):
            ctx.push_branch_force_with_lease(repo_dir, env, service_branch)
            ctx.add_detail(details, _("detail.pushed_branch", branch=service_branch))

        write_state(
            {
                **state_store.ALL_PREVIEW_CLEAR_UPDATES,
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "reset_git_state",
                "last_message": _("message.git_state_reset"),
                "last_details": details,
                "last_targets": resolved_targets,
                "post_apply_save_recommended": False,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "reset_git_state",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_disk_usage_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "disk_usage", lock_acquired):
        return False

    details = []
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "disk_usage",
            "last_message": _("message.checking_disk_usage"),
            "last_details": details,
        }
    )

    try:
        details.extend(ctx.build_disk_usage_summary())
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "disk_usage",
                "last_message": _("message.disk_usage_finished"),
                "last_details": details,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "disk_usage",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_deleted_devices_preview_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "deleted_devices_preview", lock_acquired):
        return False

    details = []
    log_action(ctx, "deleted_devices preview: started")
    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            **state_store.DELETED_DEVICES_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_preview",
            "last_message": _("message.checking_deleted_devices"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_pending_before_check")
        ctx.add_detail(details, _("detail.checking_deleted_devices"))
        preview = ctx.build_deleted_devices_preview()
        count = preview["count"]
        log_action(ctx, f"deleted_devices preview: found {count} deleted device(s)")
        message = _("message.deleted_devices_found", count=count, entry_word="entry" if count == 1 else "entries")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_preview",
                "last_message": message,
                "last_details": details,
                "last_deleted_devices_preview": preview["summary"],
                "last_deleted_devices_rows": preview["rows"],
                "last_deleted_devices_count": count,
                "last_deleted_devices_fingerprint": preview["fingerprint"],
                "last_deleted_devices_generated_at": utc_now(),
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"deleted_devices preview: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_preview",
                "last_message": str(exc),
                "last_details": details,
                "last_deleted_devices_preview": "",
                "last_deleted_devices_rows": [],
                "last_deleted_devices_count": 0,
                "last_deleted_devices_fingerprint": None,
                "last_deleted_devices_generated_at": None,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def retained_device_rows(candidates):
    rows = []
    for item in candidates:
        rows.append(
            {
                "selected": bool(item.get("retained_topics")),
                "identifiers": item.get("identifiers") or ["mqtt", f"zigbee2mqtt_{item.get('ieee', '')}"],
                "name": item.get("name") or "",
                "manufacturer": item.get("manufacturer") or "",
                "model": item.get("model") or "",
                "retained_topics": item.get("retained_topics") or [],
                "ieee": item.get("ieee") or "",
                "id": item.get("id") or "",
            }
        )
    return rows


def run_internal_ids_preview_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "internal_ids_preview", lock_acquired):
        return False

    details = []
    log_action(ctx, "internal ids preview: started")
    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            **state_store.INTERNAL_IDS_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "internal_ids_preview",
            "last_message": _("message.checking_internal_ids"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_pending_before_internal_ids")
        details.append(_("detail.checking_internal_ids"))
        preview = ctx.build_internal_ids_preview()
        count = int(preview["count"])
        message = _("message.internal_ids_found", count=count, suffix="" if count == 1 else "s")
        details.append(message)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "internal_ids_preview",
                "last_message": "",
                "last_details": details,
                "last_internal_ids_preview": preview["summary"],
                "last_internal_ids_rows": preview["rows"],
                "last_internal_ids_count": count,
                "last_internal_ids_fingerprint": preview["fingerprint"],
                "last_internal_ids_generated_at": utc_now(),
                "last_internal_ids_unresolved": preview["unresolved"],
            }
        )
        log_action(ctx, f"internal ids preview: found {count} file(s)")
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"internal ids preview: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "internal_ids_preview",
                "last_message": str(exc),
                "last_details": details,
                **state_store.INTERNAL_IDS_PREVIEW_CLEAR_UPDATES,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_internal_ids_migrate_job(selected, ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "internal_ids_migrate", lock_acquired):
        return False

    details = []
    log_action(ctx, "internal ids migrate: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "internal_ids_migrate",
            "last_message": _("message.migrating_internal_ids"),
            "last_details": details,
        }
    )

    try:
        options = ctx.load_options()
        state = ctx.read_state()
        rows = state.get("last_internal_ids_rows") or []
        fingerprint = state.get("last_internal_ids_fingerprint")
        if not rows or not fingerprint:
            raise i18n.error("error.internal_ids_preview_required")
        selected_indexes = {int(value) for value in selected}
        if not selected_indexes:
            raise i18n.error("error.internal_ids_selection_required")
        selected_paths = []
        for index, row in enumerate(rows):
            if index in selected_indexes and row.get("changes"):
                selected_paths.append(row.get("path"))
        result = ctx.apply_internal_ids_migration(fingerprint, selected_paths)
        for row in result["changed"]:
            ctx.add_detail(details, _("detail.migrated_internal_ids_path", path=row["path"]))
        commit_pending_internal_ids_migration(ctx, options, details)
        preview = result["preview"]
        unresolved_count = len(preview.get("unresolved") or [])
        changed_count = result["changed_count"]
        file_word = "file" if changed_count == 1 else "files"
        if unresolved_count:
            item_word = "item" if unresolved_count == 1 else "items"
            verb = "remains" if unresolved_count == 1 else "remain"
            message = _(
                "message.internal_ids_migrated_with_unresolved",
                changed=changed_count,
                file_word=file_word,
                unresolved=unresolved_count,
                item_word=item_word,
                verb=verb,
            )
            ctx.add_detail(details, _("detail.unresolved_internal_ids", count=unresolved_count, item_word=item_word, verb=verb))
        else:
            message = _("message.internal_ids_migrated", changed=changed_count, file_word=file_word)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "internal_ids_migrate",
                "last_message": message,
                "last_details": details,
                "last_internal_ids_preview": preview["summary"],
                "last_internal_ids_rows": preview["rows"],
                "last_internal_ids_count": preview["count"],
                "last_internal_ids_fingerprint": preview["fingerprint"],
                "last_internal_ids_generated_at": utc_now(),
                "last_internal_ids_unresolved": preview["unresolved"],
            }
        )
        log_action(ctx, f"internal ids migrate: changed {result['changed_count']} file(s)")
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"internal ids migrate: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "internal_ids_migrate",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_retained_devices_preview_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "retained_devices_preview", lock_acquired):
        return False

    details = []
    log_action(ctx, "retained devices preview: started")
    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            **state_store.RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "retained_devices_preview",
            "last_message": _("message.checking_retained_devices"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_pending_before_retained")
        ctx.add_detail(details, _("detail.checking_retained_devices"))
        preview = ctx.build_retained_devices_preview()
        rows = retained_device_rows(preview["candidates"])
        count = len(rows)
        log_action(ctx, f"retained devices preview: found {count} candidate(s)")
        message = _("message.retained_devices_found", count=count, suffix="" if count == 1 else "s")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "retained_devices_preview",
                "last_message": message,
                "last_details": details,
                "last_retained_devices_preview": preview["summary"],
                "last_retained_devices_rows": rows,
                "last_retained_devices_count": count,
                "last_retained_devices_fingerprint": preview["fingerprint"],
                "last_retained_devices_generated_at": utc_now(),
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"retained devices preview: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "retained_devices_preview",
                "last_message": str(exc),
                "last_details": details,
                **state_store.RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_retained_devices_delete_job(selected, ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "retained_devices_delete", lock_acquired):
        return False

    details = []
    log_action(ctx, "retained devices delete: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "retained_devices_delete",
            "last_message": _("message.deleting_retained_devices"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        rows = state.get("last_retained_devices_rows") or []
        if not rows:
            raise i18n.error("error.retained_devices_preview_required")
        selected_indexes = {int(value) for value in selected}
        if not selected_indexes:
            raise i18n.error("error.retained_devices_selection_required")
        topics = []
        for index, row in enumerate(rows):
            if index not in selected_indexes:
                continue
            topics.extend(row.get("retained_topics") or [])
        if not topics:
            raise i18n.error("error.retained_devices_no_topics")

        cleared = []
        for topic in sorted(set(topics)):
            ctx.clear_retained_discovery_topic(topic)
            cleared.append(topic)
            ctx.add_detail(details, _("detail.cleared_retained_topic", topic=topic))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "retained_devices_delete",
                "last_message": _("message.deleted_retained_devices", count=len(selected_indexes)),
                "last_details": details,
                **state_store.RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES,
            }
        )
        log_action(ctx, f"retained devices delete: cleared {len(cleared)} topic(s)")
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"retained devices delete: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "retained_devices_delete",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_deleted_devices_delete_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "deleted_devices_delete", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    backup_slug = None
    core_stopped = False
    registry_changed = False
    result = None
    rollback = None
    restored_preview = None
    rollback_restore_failed = False
    log_action(ctx, "deleted_devices delete: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_delete",
            "last_message": _("message.deleting_deleted_devices"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_pending_before_delete")
        fingerprint = state.get("last_deleted_devices_fingerprint")
        count = int(state.get("last_deleted_devices_count") or 0)
        if not fingerprint or count <= 0:
            raise i18n.error("error.deleted_devices_preview_required")
        current_preview = ctx.build_deleted_devices_preview()
        if current_preview["fingerprint"] != fingerprint:
            raise i18n.error("error.deleted_devices_preview_changed")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)
        current_preview = ctx.build_deleted_devices_preview()
        if current_preview["fingerprint"] != fingerprint:
            raise i18n.error("error.deleted_devices_preview_changed")
        ctx.add_detail(details, _("detail.stopping_core_for_deleted_devices"))
        ctx.core_stop()
        core_stopped = True
        rollback = ctx.create_deleted_devices_rollback(fingerprint)
        write_state(
            {
                "deleted_devices_pending_confirmation": True,
                "deleted_devices_rollback_path": rollback["path"],
                "deleted_devices_rollback_fingerprint": rollback["fingerprint"],
                "deleted_devices_applied_fingerprint": None,
            }
        )
        ctx.add_detail(details, _("detail.saved_deleted_devices_rollback"))
        result = ctx.clear_deleted_devices(fingerprint)
        registry_changed = True
        removed = result["removed"]
        log_action(ctx, f"deleted_devices delete: cleared {removed} deleted device(s)")
        ctx.add_detail(details, _("detail.removed_deleted_devices", count=removed, entry_word="entry" if removed == 1 else "entries"))
        ctx.add_detail(details, _("detail.starting_core"))
        try:
            ctx.core_start()
        except Exception:
            if rollback:
                ctx.add_detail(details, _("detail.homeassistant_core_failed_reverting_deleted_devices"))
                try:
                    ctx.restore_deleted_devices_rollback(rollback["path"])
                    restored_preview = refresh_deleted_devices_preview_updates(ctx)
                except Exception:
                    rollback_restore_failed = True
                    raise
                ctx.core_start()
                core_stopped = False
                ctx.discard_deleted_devices_rollback(rollback["path"])
                details.append(_("detail.reverted_deleted_devices_after_core_failure"))
                registry_changed = False
                raise
        core_stopped = False
        log_action(ctx, "deleted_devices delete: Core restarted, waiting for confirmation")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_delete",
                "last_message": _(
                    "message.deleted_deleted_devices",
                    count=removed,
                    entry_word="entry" if removed == 1 else "entries",
                ),
                "last_details": details,
                "last_backup_slug": backup_slug,
                "last_deleted_devices_preview": _("text.no_deleted_devices"),
                "last_deleted_devices_rows": [],
                "last_deleted_devices_count": 0,
                "last_deleted_devices_fingerprint": result["fingerprint"],
                "last_deleted_devices_generated_at": utc_now(),
                "deleted_devices_pending_confirmation": True,
                "deleted_devices_rollback_path": rollback["path"] if rollback else None,
                "deleted_devices_rollback_fingerprint": rollback["fingerprint"] if rollback else None,
                "deleted_devices_applied_fingerprint": result["fingerprint"],
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"deleted_devices delete: failed: {exc}")
        if core_stopped:
            try:
                ctx.core_start()
                details.append(_("detail.started_core_after_deletion_failure"))
            except Exception as start_exc:
                details.append(_("detail.failed_start_core_after_deletion_failure", error=start_exc))
        if rollback and not registry_changed and not restored_preview:
            try:
                ctx.discard_deleted_devices_rollback(rollback["path"])
            except Exception as rollback_exc:
                details.append(_("detail.failed_discard_deleted_devices_rollback", error=rollback_exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_delete",
                "last_message": (
                    _("message.deleted_devices_cleanup_manual_recovery")
                    if rollback_restore_failed
                    else str(exc)
                ),
                "last_details": details,
                "last_backup_slug": backup_slug,
                **(restored_preview or {}),
                **(
                    {
                        "last_deleted_devices_preview": _("text.no_deleted_devices"),
                        "last_deleted_devices_rows": [],
                        "last_deleted_devices_count": 0,
                        "last_deleted_devices_fingerprint": result.get("fingerprint") if result else None,
                        "last_deleted_devices_generated_at": utc_now(),
                        "deleted_devices_pending_confirmation": True,
                        "deleted_devices_rollback_path": rollback["path"],
                        "deleted_devices_rollback_fingerprint": rollback["fingerprint"],
                        "deleted_devices_applied_fingerprint": result.get("fingerprint") if result else None,
                    }
                    if rollback_restore_failed
                    else {}
                ),
                **(
                    {
                        "last_deleted_devices_preview": _("text.no_deleted_devices"),
                        "last_deleted_devices_rows": [],
                        "last_deleted_devices_count": 0,
                        "last_deleted_devices_fingerprint": result.get("fingerprint") if result else None,
                        "last_deleted_devices_generated_at": utc_now(),
                        "deleted_devices_pending_confirmation": False,
                        "deleted_devices_rollback_path": None,
                        "deleted_devices_rollback_fingerprint": None,
                        "deleted_devices_applied_fingerprint": None,
                    }
                    if registry_changed and not restored_preview and not rollback_restore_failed
                    else {}
                ),
                **(
                    {
                        "deleted_devices_pending_confirmation": False,
                        "deleted_devices_rollback_path": None,
                        "deleted_devices_rollback_fingerprint": None,
                        "deleted_devices_applied_fingerprint": None,
                    }
                    if restored_preview or (rollback and not registry_changed)
                    else {}
                ),
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_deleted_devices_confirm_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "deleted_devices_confirm", lock_acquired):
        return False

    details = []
    log_action(ctx, "deleted_devices confirm: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_confirm",
            "last_message": _("message.confirming_deleted_devices_cleanup"),
            "last_details": details,
        }
    )
    try:
        state = ctx.read_state()
        if not state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_cleanup_not_pending")
        rollback_path = state.get("deleted_devices_rollback_path")
        if not rollback_path:
            raise i18n.error("error.deleted_devices_rollback_missing")
        applied_fingerprint = state.get("deleted_devices_applied_fingerprint")
        cleanup_status = ctx.deleted_devices_cleanup_status(rollback_path)
        if cleanup_status["returned"] > 0:
            raise i18n.error("error.deleted_devices_removed_returned")
        if applied_fingerprint and cleanup_status["fingerprint"] != applied_fingerprint:
            if cleanup_status["added"] > 0:
                details.append(
                    _(
                        "detail.device_registry_new_deleted_devices",
                        count=cleanup_status["added"],
                        entry_word="entry" if cleanup_status["added"] == 1 else "entries",
                    )
                )
                log_action(ctx, "deleted_devices confirm: new deleted_devices are present and preserved")
            else:
                details.append(_("detail.device_registry_changed_after_deletion"))
                log_action(ctx, "deleted_devices confirm: registry fingerprint changed, removed deleted_devices did not return")
        ctx.discard_deleted_devices_rollback(rollback_path)
        log_action(ctx, "deleted_devices confirm: confirmed and discarded rollback")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_confirm",
                "last_message": _("message.confirmed_deleted_devices_cleanup"),
                "last_details": details,
                "deleted_devices_pending_confirmation": False,
                "deleted_devices_rollback_path": None,
                "deleted_devices_rollback_fingerprint": None,
                "deleted_devices_applied_fingerprint": None,
            }
        )
        return True
    except Exception as exc:
        message = i18n.user_message(exc)
        details.append(message)
        log_action(ctx, f"deleted_devices confirm: failed: {exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_confirm",
                "last_message": message,
                "last_details": details,
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_deleted_devices_revert_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "deleted_devices_revert", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    backup_slug = None
    core_stopped = False
    restore_applied = False
    result = None
    log_action(ctx, "deleted_devices revert: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_revert",
            "last_message": _("message.reverting_deleted_devices_cleanup"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if not state.get("deleted_devices_pending_confirmation"):
            raise i18n.error("error.deleted_devices_cleanup_not_pending")
        rollback_path = state.get("deleted_devices_rollback_path")
        if not rollback_path:
            raise i18n.error("error.deleted_devices_rollback_missing")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)
        ctx.add_detail(details, _("detail.stopping_core_for_deleted_devices_restore"))
        ctx.core_stop()
        core_stopped = True
        result = ctx.restore_deleted_devices_rollback(rollback_path)
        restore_applied = True
        ctx.add_detail(details, _("detail.restored_deleted_devices", count=result.get("restored", 0)))
        if result.get("preserved", 0) > 0:
            ctx.add_detail(details, _("detail.preserved_current_deleted_devices", count=result.get("preserved", 0)))
        ctx.add_detail(details, _("detail.preserved_other_registry_changes"))
        ctx.add_detail(details, _("detail.starting_core"))
        ctx.core_start()
        core_stopped = False
        ctx.discard_deleted_devices_rollback(rollback_path)
        preview_updates = refresh_deleted_devices_preview_updates(ctx)
        log_action(ctx, "deleted_devices revert: restored deleted_devices and restarted Core")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_revert",
                "last_message": _("message.reverted_deleted_devices_cleanup"),
                "last_details": details,
                "last_backup_slug": backup_slug,
                **preview_updates,
                "deleted_devices_pending_confirmation": False,
                "deleted_devices_rollback_path": None,
                "deleted_devices_rollback_fingerprint": None,
                "deleted_devices_applied_fingerprint": None,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        log_action(ctx, f"deleted_devices revert: failed: {exc}")
        if core_stopped:
            try:
                ctx.core_start()
                details.append(_("detail.started_core_after_revert_failure"))
            except Exception as start_exc:
                details.append(_("detail.failed_start_core_after_revert_failure", error=start_exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_revert",
                "last_message": str(exc),
                "last_details": details,
                "last_backup_slug": backup_slug,
                **(
                    {
                        **(
                            refresh_deleted_devices_preview_updates(ctx)
                            if result
                            else {}
                        ),
                        "deleted_devices_pending_confirmation": False,
                        "deleted_devices_applied_fingerprint": None,
                    }
                    if restore_applied
                    else {}
                ),
            }
        )
        return False
    finally:
        release_run_lock(ctx)


def run_apply_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "apply", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    release_name = None
    backup_slug = None
    resolved_targets = []
    core_stopped_for_apply = False

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "apply",
            "last_message": _("message.preparing_apply"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "apply", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(
                ctx,
                "apply",
                conflict_status_message(state, _("message.resolve_git_conflicts_before_apply")),
                details,
                resolved_targets,
            )
            return False

        prepare_repo_checkout_for_sync(ctx, options, details, "Apply Git to HA")
        repo_dir = ctx.ensure_repo(options)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, _("detail.fetched_repository_commit", commit=ctx.git_head_or_unborn(repo_dir)))
        ctx.add_detail(details, _("detail.using_manifest", path=manifest_path))
        ctx.add_detail(details, _("detail.rebuilding_apply_preview"))
        preview = ctx.build_apply_preview(resolved_targets, details, repo_dir, branch)
        commit = ctx.git_head_or_unborn(repo_dir)
        try:
            ctx.ensure_preview_matches_state(state, commit, preview)
        except RuntimeError:
            message = preview_changed_message()
            details.append(message)
            write_state(
                {
                    "last_run_at": utc_now(),
                    "last_status": "warning",
                    "last_action": "apply",
                    "last_message": message,
                    "last_details": details,
                    "last_targets": resolved_targets,
                    "last_diff": preview["diff"],
                    "last_diff_generated_at": utc_now(),
                    "last_preview_commit": commit,
                    "last_preview_fingerprint": preview["fingerprint"],
                    "last_preview_deletions": preview["deletions"],
                    "last_preview_storage_changes": preview.get("storage_changes", False),
                    "last_preview_storage_paths": preview.get("storage_change_paths", []),
                    "last_preview_live_fingerprints": preview.get("live_fingerprints", {}),
                    "last_preview_warnings": preview.get("warnings", []),
                    "last_preview_paths": preview.get("paths", []),
                    "last_preview_conflicts": bool(preview.get("conflicts")),
                    "last_preview_conflict_paths": preview.get("conflicts", []),
                    "apply_preview_resolutions": {},
                    "apply_preview_selected_paths": [],
                }
            )
            return False
        apply_resolutions = apply_preview_resolutions_for_current_preview(state, preview)
        apply_selected_paths = selected_preview_paths(state, list(preview.get("paths") or []), "apply_preview_selected_paths")
        selected_clean_delete_paths = {
            path
            for path in preview.get("clean_git_delete_paths", [])
            if apply_resolutions.get(path) == "git"
        }
        if preview.get("conflicts"):
            selected_delete_paths = {
                path
                for path in preview.get("conflict_git_delete_paths", [])
                if apply_resolutions.get(path) == "git"
            }
            if selected_clean_delete_paths or selected_delete_paths:
                preview = dict(preview)
                preview["deletions"] = len(selected_clean_delete_paths | selected_delete_paths)
        ctx.enforce_apply_limits(options, preview)
        keep_ha_paths = [path for path, choice in apply_resolutions.items() if choice == "ha"]
        conflict_preview = bool(preview.get("conflicts"))
        apply_commit = None
        if preview.get("paths") and not conflict_preview:
            resolved_targets = ctx.selected_apply_targets_from_preview(resolved_targets, keep_ha_paths)
            ctx.add_detail(details, _("detail.approved_apply_preview_files", count=len(apply_selected_paths)))
        elif conflict_preview:
            ctx.add_detail(details, _("detail.approved_apply_preview_conflicts", count=len(apply_selected_paths)))
            if preview.get("storage_changes"):
                resolved_targets = ctx.approve_storage_apply_targets(resolved_targets)

        backup_slug = ctx.ensure_fresh_system_backup(options, details)

        if ctx.option_bool(options, "create_release_snapshot", True):
            ctx.add_detail(details, _("detail.creating_release_snapshot"))
            release_name = ctx.create_release_snapshot(resolved_targets, commit, backup_slug)
            ctx.add_detail(details, _("detail.created_release_snapshot", release=release_name))

        if conflict_preview:
            apply_commit = ctx.commit_apply_merge(
                repo_dir,
                branch,
                resolved_targets,
                keep_ha_paths,
                f"Apply Git config to Home Assistant {ctx.release_now()}",
                details,
            )
            if apply_commit:
                ctx.add_detail(details, _("detail.updated_ha_live", commit=apply_commit))

        apply_result = ctx.apply_targets(resolved_targets, details) or {}
        core_stopped_for_apply = bool(apply_result.get("core_stopped"))
        if conflict_preview:
            ctx.delete_apply_conflict_live_deletions(
                resolved_targets,
                repo_dir,
                branch,
                apply_resolutions,
                details,
                sorted(selected_clean_delete_paths),
            )
        if not conflict_preview:
            apply_commit = ctx.commit_apply_merge(
                repo_dir,
                branch,
                resolved_targets,
                keep_ha_paths,
                f"Apply Git config to Home Assistant {ctx.release_now()}",
                details,
                sync_applied_storage=True,
            )
            if apply_commit:
                ctx.add_detail(details, _("detail.updated_ha_live", commit=apply_commit))
        git_state_out_of_date = False
        for service_branch in ("ha-ops/ha-live", "ha-ops/base"):
            try:
                ctx.push_branch(repo_dir, env, service_branch)
                ctx.add_detail(details, _("detail.pushed_branch", branch=service_branch))
            except RuntimeError as exc:
                git_state_out_of_date = (
                    record_service_branch_push_failure(ctx, details, service_branch, exc)
                    or git_state_out_of_date
                )
        pruned = ctx.prune_release_snapshots(options, protected_release=release_name)
        if pruned:
            ctx.add_detail(details, _("detail.pruned_release_snapshots", count=len(pruned), releases=", ".join(pruned)))

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "warning" if git_state_out_of_date else "success",
                "last_action": "apply",
                "last_message": (
                    _("message.git_state_out_of_date")
                    if git_state_out_of_date
                    else _("message.apply_finished")
                ),
                "last_details": details,
                "last_release": release_name,
                "last_backup_slug": backup_slug,
                "last_targets": resolved_targets,
                "last_preview_deletions": preview["deletions"],
                "apply_preview_resolutions": {},
                "apply_preview_selected_paths": [],
                "last_preview_conflict_paths": [],
                "post_apply_save_recommended": True,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        core_stopped_for_apply = core_stopped_for_apply or bool(getattr(exc, "core_stopped", False))
        if release_name:
            try:
                ctx.add_detail(details, _("detail.restoring_release_snapshot_after_failure", release=release_name))
                ctx.restore_release_snapshot(release_name, details, core_already_stopped=core_stopped_for_apply)
                core_stopped_for_apply = False
            except Exception as rollback_exc:
                details.append(_("detail.rollback_from_release_failed", error=rollback_exc))
        if core_stopped_for_apply:
            try:
                ctx.add_detail(details, _("detail.starting_core_after_apply_failure"))
                ctx.core_start()
                core_stopped_for_apply = False
            except Exception as start_exc:
                details.append(_("detail.start_core_after_apply_failure_failed", error=start_exc))

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
        release_run_lock(ctx)


def run_preview_job(ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "preview", lock_acquired):
        return False

    details = []
    options = ctx.load_options()
    resolved_targets = []

    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "preview",
            "last_message": _("message.preparing_apply_preview"),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "preview", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(
                ctx,
                "preview",
                conflict_status_message(state, _("message.resolve_git_conflicts_before_apply_preview")),
                details,
                resolved_targets,
            )
            return False

        prepare_repo_checkout_for_sync(ctx, options, details, "Preview Git to HA")
        repo_dir = ctx.ensure_repo(options)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, _("detail.fetched_repository_commit", commit=ctx.git_head_or_unborn(repo_dir)))
        ctx.add_detail(details, _("detail.using_manifest", path=manifest_path))
        ctx.add_detail(details, _("detail.building_apply_preview"))
        preview = ctx.build_apply_preview(resolved_targets, details, repo_dir, branch)
        commit = ctx.git_head_or_unborn(repo_dir)
        ctx.push_branch(repo_dir, env, "ha-ops/ha-live")
        ctx.add_detail(details, _("detail.pushed_ha_live"))
        git_state_out_of_date = False
        try:
            ctx.push_branch(repo_dir, env, "ha-ops/base")
            ctx.add_detail(details, _("detail.pushed_ha_base"))
        except RuntimeError as exc:
            git_state_out_of_date = record_service_branch_push_failure(ctx, details, "ha-ops/base", exc)
        message = _("message.apply_preview_finished")
        if preview.get("storage_changes"):
            paths = preview.get("storage_change_paths") or []
            ctx.add_detail(details, _("detail.confirm_apply_storage_changes", count=len(paths)))
            message = _("message.apply_preview_storage_confirm")
        if git_state_out_of_date:
            message = _("message.git_state_out_of_date")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "warning" if git_state_out_of_date else "success",
                "last_action": "preview",
                "last_message": message,
                "last_details": details,
                "last_targets": resolved_targets,
                "last_diff": preview["diff"],
                "last_diff_generated_at": utc_now(),
                "last_preview_commit": commit,
                "last_preview_fingerprint": preview["fingerprint"],
                "last_preview_deletions": preview["deletions"],
                "last_preview_storage_changes": preview.get("storage_changes", False),
                "last_preview_storage_paths": preview.get("storage_change_paths", []),
                "last_preview_live_fingerprints": preview.get("live_fingerprints", {}),
                "last_preview_warnings": preview.get("warnings", []),
                "last_preview_paths": preview.get("paths", []),
                "last_preview_conflicts": bool(preview.get("conflicts")),
                "last_preview_conflict_paths": preview.get("conflicts", []),
                "apply_preview_resolutions": {},
                "apply_preview_selected_paths": [],
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
        release_run_lock(ctx)


def run_rollback_job(release_name, ctx, lock_acquired=False):
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not enter_run_lock(ctx, "rollback", lock_acquired):
        return False

    details = []
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "rollback",
            "last_message": _("message.rolling_back_release", release=release_name),
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "rollback", details)
            return False
        metadata = ctx.restore_release_snapshot(release_name, details)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "rollback",
                "last_message": _("message.rollback_finished", release=release_name),
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
        release_run_lock(ctx)
