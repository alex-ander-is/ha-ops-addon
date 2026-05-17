from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobContext:
    add_detail: Any
    apply_targets: Any
    build_deleted_devices_preview: Any
    build_apply_preview: Any
    build_save_preview: Any
    clear_deleted_devices: Any
    commit_if_needed: Any
    core_start: Any
    core_stop: Any
    create_deleted_devices_rollback: Any
    create_release_snapshot: Any
    device_registry_fingerprint: Any
    discard_deleted_devices_rollback: Any
    enforce_apply_limits: Any
    ensure_fresh_system_backup: Any
    ensure_preview_matches_state: Any
    ensure_storage_apply_approved: Any
    ensure_repo: Any
    export_targets: Any
    get_installed_addons: Any
    git_conflict_paths: Any
    git_env: Any
    git_has_unpushed_commits: Any
    git_head_or_unborn: Any
    git_pull_rebase: Any
    git_status_porcelain: Any
    load_manifest: Any
    load_options: Any
    option_bool: Any
    prune_release_snapshots: Any
    push_branch: Any
    read_state: Any
    release_now: Any
    repo_checkout_path: Any
    reset_repo_worktree: Any
    restore_save_git_resolutions: Any
    resolve_targets: Any
    approve_storage_apply_targets: Any
    restore_deleted_devices_rollback: Any
    restore_release_snapshot: Any
    run_lock: Any
    save_unknown_base_conflicts: Any
    stage_all: Any
    stage_homeassistant_storage_allowlist: Any
    utc_now: Any
    write_state: Any


def conflict_status_message(state, default_message="Resolve Git conflicts before continuing."):
    if state.get("conflict_type") == "save_unknown_base":
        return "Resolve unknown-base Save conflicts before running Save HA to Git."
    return default_message


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


def pending_deleted_devices_message():
    return "Confirm or revert the pending deleted_devices cleanup before running another HA Ops action."


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
    ctx.add_detail(details, "\n".join([f"Git changes prepared for commit ({len(lines)}):", *lines]))


def run_save_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
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
    options = ctx.load_options()
    resolved_targets = []
    repo_dir = None
    checkout_dirty_for_save = False
    save_commit_created = False

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
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "save", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(ctx, "save", conflict_status_message(state), details, resolved_targets)
            return False

        repo_dir = ctx.ensure_repo(options, reset_to_origin=False)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        ctx.git_pull_rebase(repo_dir, env, branch)
        commit = ctx.git_head_or_unborn(repo_dir)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, f"Using branch {branch} at commit {commit}.")
        ctx.add_detail(details, f"Using manifest {manifest_path}.")
        save_resolutions = state.get("save_conflict_resolutions", {})
        save_conflicts = ctx.save_unknown_base_conflicts(resolved_targets, repo_dir, save_resolutions, details)
        if save_conflicts:
            message = "Resolve unknown-base Save conflicts before running Save HA to Git."
            write_state(
                {
                    "last_run_at": utc_now(),
                    "last_status": "conflicts",
                    "last_action": "save",
                    "last_message": message,
                    "last_details": details,
                    "last_targets": resolved_targets,
                    "conflicts": save_conflicts,
                    "conflict_type": "save_unknown_base",
                    "save_conflict_resolutions": save_resolutions,
                }
            )
            return False

        ctx.add_detail(details, "Saving live Home Assistant config to Git.")
        checkout_dirty_for_save = True
        ctx.export_targets(resolved_targets, details)
        ctx.restore_save_git_resolutions(repo_dir, save_resolutions, details)
        ctx.stage_homeassistant_storage_allowlist(repo_dir, options, details)
        ctx.stage_all(repo_dir)
        add_save_change_details(ctx, details, ctx.git_status_porcelain(repo_dir))

        new_commit = ctx.commit_if_needed(repo_dir, f"Save Home Assistant config {ctx.release_now()}")
        if new_commit:
            save_commit_created = True
            ctx.add_detail(details, f"Created commit {new_commit}.")

        if ctx.git_has_unpushed_commits(repo_dir, branch):
            try:
                ctx.push_branch(repo_dir, env, branch)
            except RuntimeError:
                ctx.git_pull_rebase(repo_dir, env, branch)
                ctx.push_branch(repo_dir, env, branch)
            ctx.add_detail(details, f"Pushed to origin/{branch}.")
        else:
            ctx.add_detail(details, "No live Home Assistant changes to save.")

        write_state({"conflicts": [], "conflict_type": None, "save_conflict_resolutions": {}})

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
        if repo_dir and checkout_dirty_for_save and not save_commit_created:
            try:
                ctx.reset_repo_worktree(repo_dir)
                details.append("Cleaned incomplete Save changes from the checkout.")
            except Exception as cleanup_exc:
                details.append(f"Failed to clean incomplete Save changes from the checkout: {cleanup_exc}")
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
            }
        )
        return False
    finally:
        run_lock.release()


def run_save_preview_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "save_preview",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = ctx.load_options()
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "save_preview",
            "last_message": "Preparing save preview.",
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
                conflict_status_message(state, "Resolve Git conflicts before running Preview HA to Git."),
                details,
                resolved_targets,
            )
            return False

        repo_dir = ctx.ensure_repo(options, reset_to_origin=False)
        env = ctx.git_env(options)
        branch = options.get("repo_branch", "main")
        ctx.git_pull_rebase(repo_dir, env, branch)
        commit = ctx.git_head_or_unborn(repo_dir)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, f"Using branch {branch} at commit {commit}.")
        ctx.add_detail(details, f"Using manifest {manifest_path}.")
        ctx.add_detail(details, "Building save preview without committing or pushing.")
        preview = ctx.build_save_preview(resolved_targets, repo_dir, details)

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "save_preview",
                "last_message": "Save preview finished successfully.",
                "last_details": details,
                "last_targets": resolved_targets,
                "last_save_preview": preview["summary"],
                "last_save_diff": preview["diff"],
                "last_save_diff_generated_at": utc_now(),
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
        run_lock.release()


def run_deleted_devices_preview_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "deleted_devices_preview",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_preview",
            "last_message": "Checking deleted_devices.",
            "last_details": details,
            "last_deleted_devices_preview": "",
            "last_deleted_devices_rows": [],
            "last_deleted_devices_count": 0,
            "last_deleted_devices_fingerprint": None,
            "last_deleted_devices_generated_at": None,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("Confirm or revert the pending deleted_devices cleanup before checking again.")
        ctx.add_detail(details, "Checking Home Assistant deleted_devices.")
        preview = ctx.build_deleted_devices_preview()
        count = preview["count"]
        message = f"Found {count} deleted_devices entr{'y' if count == 1 else 'ies'}."
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
        run_lock.release()


def run_deleted_devices_delete_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "deleted_devices_delete",
                "last_message": "Another HA Ops action is already running.",
            }
        )
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
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_delete",
            "last_message": "Deleting deleted_devices.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("Confirm or revert the pending deleted_devices cleanup before approving another deletion.")
        fingerprint = state.get("last_deleted_devices_fingerprint")
        count = int(state.get("last_deleted_devices_count") or 0)
        if not fingerprint or count <= 0:
            raise RuntimeError("Run Check deleted_devices before approving deletion.")
        current_preview = ctx.build_deleted_devices_preview()
        if current_preview["fingerprint"] != fingerprint:
            raise RuntimeError("Device registry changed since preview. Run Check deleted_devices again.")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)
        current_preview = ctx.build_deleted_devices_preview()
        if current_preview["fingerprint"] != fingerprint:
            raise RuntimeError("Device registry changed since preview. Run Check deleted_devices again.")
        ctx.add_detail(details, "Stopping Home Assistant Core before updating core.device_registry.")
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
        ctx.add_detail(details, "Saved deleted_devices rollback snapshot.")
        result = ctx.clear_deleted_devices(fingerprint)
        registry_changed = True
        removed = result["removed"]
        ctx.add_detail(details, f"Removed {removed} deleted_devices entr{'y' if removed == 1 else 'ies'}.")
        ctx.add_detail(details, "Starting Home Assistant Core.")
        try:
            ctx.core_start()
        except Exception:
            if rollback:
                ctx.add_detail(details, "Home Assistant Core failed to start. Reverting deleted_devices cleanup.")
                try:
                    ctx.restore_deleted_devices_rollback(rollback["path"])
                    restored_preview = refresh_deleted_devices_preview_updates(ctx)
                except Exception:
                    rollback_restore_failed = True
                    raise
                ctx.core_start()
                core_stopped = False
                ctx.discard_deleted_devices_rollback(rollback["path"])
                details.append("Reverted deleted_devices cleanup because Home Assistant Core failed to start.")
                registry_changed = False
            raise
        core_stopped = False

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_delete",
                "last_message": f"Deleted {removed} deleted_devices entr{'y' if removed == 1 else 'ies'}. Confirm or revert the changes.",
                "last_details": details,
                "last_backup_slug": backup_slug,
                "last_deleted_devices_preview": "No deleted_devices entries found.",
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
        if core_stopped:
            try:
                ctx.core_start()
                details.append("Started Home Assistant Core after deletion failure.")
            except Exception as start_exc:
                details.append(f"Failed to start Home Assistant Core after deletion failure: {start_exc}")
        if rollback and not registry_changed and not restored_preview:
            try:
                ctx.discard_deleted_devices_rollback(rollback["path"])
            except Exception as rollback_exc:
                details.append(f"Failed to discard unused deleted_devices rollback snapshot: {rollback_exc}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_delete",
                "last_message": (
                    "deleted_devices cleanup changed the registry and rollback restore failed. Manual recovery is required."
                    if rollback_restore_failed
                    else str(exc)
                ),
                "last_details": details,
                "last_backup_slug": backup_slug,
                **(restored_preview or {}),
                **(
                    {
                        "last_deleted_devices_preview": "No deleted_devices entries found.",
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
                        "last_deleted_devices_preview": "No deleted_devices entries found.",
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
        run_lock.release()


def run_deleted_devices_confirm_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "deleted_devices_confirm",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_confirm",
            "last_message": "Confirming deleted_devices cleanup.",
            "last_details": details,
        }
    )
    try:
        state = ctx.read_state()
        if not state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("No deleted_devices cleanup is pending confirmation.")
        applied_fingerprint = state.get("deleted_devices_applied_fingerprint")
        if not applied_fingerprint:
            raise RuntimeError("deleted_devices applied registry fingerprint is missing.")
        if ctx.device_registry_fingerprint() != applied_fingerprint:
            raise RuntimeError("Device registry changed after deletion. Review manually before confirming.")
        rollback_path = state.get("deleted_devices_rollback_path")
        if rollback_path:
            ctx.discard_deleted_devices_rollback(rollback_path)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_confirm",
                "last_message": "Confirmed deleted_devices cleanup.",
                "last_details": details,
                "deleted_devices_pending_confirmation": False,
                "deleted_devices_rollback_path": None,
                "deleted_devices_rollback_fingerprint": None,
                "deleted_devices_applied_fingerprint": None,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "deleted_devices_confirm",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        run_lock.release()


def run_deleted_devices_revert_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "deleted_devices_revert",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = ctx.load_options()
    backup_slug = None
    core_stopped = False
    restore_applied = False
    result = None
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_revert",
            "last_message": "Reverting deleted_devices cleanup.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if not state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("No deleted_devices cleanup is pending confirmation.")
        applied_fingerprint = state.get("deleted_devices_applied_fingerprint")
        if not applied_fingerprint:
            raise RuntimeError("deleted_devices applied registry fingerprint is missing.")
        if ctx.device_registry_fingerprint() != applied_fingerprint:
            raise RuntimeError("Device registry changed after deletion. Review manually before reverting.")
        rollback_path = state.get("deleted_devices_rollback_path")
        if not rollback_path:
            raise RuntimeError("deleted_devices rollback snapshot is missing.")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)
        ctx.add_detail(details, "Stopping Home Assistant Core before reverting core.device_registry.")
        ctx.core_stop()
        core_stopped = True
        result = ctx.restore_deleted_devices_rollback(rollback_path)
        restore_applied = True
        ctx.add_detail(details, "Restored deleted_devices rollback snapshot.")
        ctx.add_detail(details, "Starting Home Assistant Core.")
        ctx.core_start()
        core_stopped = False
        ctx.discard_deleted_devices_rollback(rollback_path)
        preview_updates = refresh_deleted_devices_preview_updates(ctx)

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "deleted_devices_revert",
                "last_message": "Reverted deleted_devices cleanup.",
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
        if core_stopped:
            try:
                ctx.core_start()
                details.append("Started Home Assistant Core after revert failure.")
            except Exception as start_exc:
                details.append(f"Failed to start Home Assistant Core after revert failure: {start_exc}")
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
        run_lock.release()


def run_apply_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
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
            "last_message": "Preparing apply.",
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
                conflict_status_message(state, "Resolve Git conflicts before running Apply Git to HA."),
                details,
                resolved_targets,
            )
            return False

        repo_dir = ctx.ensure_repo(options)
        commit = ctx.git_head_or_unborn(repo_dir)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, f"Fetched repository at commit {commit}.")
        ctx.add_detail(details, f"Using manifest {manifest_path}.")
        ctx.add_detail(details, "Rebuilding apply preview for safety checks.")
        preview = ctx.build_apply_preview(resolved_targets, details)
        ctx.ensure_preview_matches_state(state, commit, preview)
        ctx.ensure_storage_apply_approved(state, preview)
        ctx.enforce_apply_limits(options, preview)
        if preview.get("storage_changes"):
            resolved_targets = ctx.approve_storage_apply_targets(resolved_targets)
            ctx.add_detail(details, "Approved .storage changes for Git to HA apply.")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)

        if ctx.option_bool(options, "create_release_snapshot", True):
            ctx.add_detail(details, "Creating local release snapshot.")
            release_name = ctx.create_release_snapshot(resolved_targets, commit, backup_slug)
            ctx.add_detail(details, f"Created local release snapshot {release_name}.")

        apply_result = ctx.apply_targets(resolved_targets, details) or {}
        core_stopped_for_apply = bool(apply_result.get("core_stopped"))
        pruned = ctx.prune_release_snapshots(options, protected_release=release_name)
        if pruned:
            ctx.add_detail(details, f"Pruned {len(pruned)} old local release snapshot(s): {', '.join(pruned)}.")

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
        core_stopped_for_apply = core_stopped_for_apply or bool(getattr(exc, "core_stopped", False))
        if release_name:
            try:
                ctx.add_detail(details, f"Restoring local release snapshot {release_name} after failure.")
                ctx.restore_release_snapshot(release_name, details, core_already_stopped=core_stopped_for_apply)
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
        run_lock.release()


def run_preview_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
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
    options = ctx.load_options()
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
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            write_pending_deleted_devices(ctx, "preview", details, resolved_targets)
            return False
        if state.get("conflicts"):
            write_pending_conflicts(
                ctx,
                "preview",
                conflict_status_message(state, "Resolve Git conflicts before running Preview Git to HA."),
                details,
                resolved_targets,
            )
            return False

        repo_dir = ctx.ensure_repo(options)
        commit = ctx.git_head_or_unborn(repo_dir)
        addons = ctx.get_installed_addons()
        manifest, manifest_path = ctx.load_manifest(repo_dir, options, addons)
        resolved_targets = ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)

        ctx.add_detail(details, f"Fetched repository at commit {commit}.")
        ctx.add_detail(details, f"Using manifest {manifest_path}.")
        ctx.add_detail(details, "Building apply preview without changing live config.")
        preview = ctx.build_apply_preview(resolved_targets, details)
        message = "Apply preview finished successfully."
        if preview.get("storage_changes"):
            paths = preview.get("storage_change_paths") or []
            ctx.add_detail(details, f"Approval required for {len(paths)} .storage change(s) before Apply Git to HA.")
            message = "Apply preview contains .storage changes. Approve Git to HA before applying."

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
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
                "last_preview_approved_fingerprint": None,
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
        run_lock.release()


def run_rollback_job(release_name, ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
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
        run_lock.release()
