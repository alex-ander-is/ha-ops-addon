from dataclasses import dataclass
from typing import Any

import state as state_store


@dataclass(frozen=True)
class JobContext:
    add_detail: Any
    apply_targets: Any
    build_deleted_devices_preview: Any
    build_internal_ids_preview: Any
    build_retained_devices_preview: Any
    build_apply_preview: Any
    build_save_preview: Any
    clean_repo_untracked: Any
    clear_deleted_devices: Any
    clear_retained_discovery_topic: Any
    commit_if_needed: Any
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
    log: Any
    option_bool: Any
    prune_release_snapshots: Any
    push_branch: Any
    read_state: Any
    release_now: Any
    repo_checkout_path: Any
    reset_repo_worktree: Any
    normalize_changed_save_registry_worktree: Any
    restore_normalized_equal_save_worktree: Any
    restore_save_git_resolutions: Any
    resolve_targets: Any
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
    ctx.add_detail(details, f"Committed pending Internal IDs migration changes to Git: {commit}.")
    return commit


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

        prepare_repo_checkout_for_sync(ctx, options, details, "Save HA to Git")
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
        include_redundant_data = bool(state.get("include_redundant_data"))
        if include_redundant_data:
            ctx.add_detail(details, "Including redundant registry data in Save.")
        save_conflicts = ctx.save_unknown_base_conflicts(
            resolved_targets,
            repo_dir,
            save_resolutions,
            details,
            include_redundant_data,
        )
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
        if not include_redundant_data:
            ctx.restore_normalized_equal_save_worktree(repo_dir, resolved_targets, details)
            ctx.normalize_changed_save_registry_worktree(repo_dir, resolved_targets, details)
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
            save_message = "Save finished successfully and pushed to Git."
        else:
            ctx.add_detail(details, "No live Home Assistant changes to save.")
            save_message = "No live Home Assistant changes to save."

        write_state({"conflicts": [], "conflict_type": None, "save_conflict_resolutions": {}})

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "save",
                "last_message": save_message,
                "last_details": details,
                "last_targets": resolved_targets,
                "post_apply_save_recommended": False,
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
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
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

        prepare_repo_checkout_for_sync(ctx, options, details, "Preview HA to Git")
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
        include_redundant_data = bool(state.get("include_redundant_data"))
        if include_redundant_data:
            ctx.add_detail(details, "Including redundant registry data in Save preview.")
        preview = ctx.build_save_preview(resolved_targets, repo_dir, details, include_redundant_data)

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
    log_action(ctx, "deleted_devices preview: started")
    write_state(
        {
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
            **state_store.DELETED_DEVICES_PREVIEW_CLEAR_UPDATES,
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "deleted_devices_preview",
            "last_message": "Checking deleted_devices.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("Confirm or revert the pending deleted_devices cleanup before checking again.")
        ctx.add_detail(details, "Checking Home Assistant deleted_devices.")
        preview = ctx.build_deleted_devices_preview()
        count = preview["count"]
        log_action(ctx, f"deleted_devices preview: found {count} deleted device(s)")
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
        run_lock.release()


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


def run_internal_ids_preview_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "internal_ids_preview",
                "last_message": "Another HA Ops action is already running.",
            }
        )
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
            "last_message": "Checking internal ids.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("Confirm or revert the pending deleted_devices cleanup before checking internal ids.")
        details.append("Checking HA Ops automations, scripts, and scenes for safe internal id migrations.")
        preview = ctx.build_internal_ids_preview()
        count = int(preview["count"])
        message = f"Found {count} internal id migration file{'s' if count != 1 else ''}."
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
        run_lock.release()


def run_internal_ids_migrate_job(selected, ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "internal_ids_migrate",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    log_action(ctx, "internal ids migrate: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "internal_ids_migrate",
            "last_message": "Migrating internal ids.",
            "last_details": details,
        }
    )

    try:
        options = ctx.load_options()
        state = ctx.read_state()
        rows = state.get("last_internal_ids_rows") or []
        fingerprint = state.get("last_internal_ids_fingerprint")
        if not rows or not fingerprint:
            raise RuntimeError("Run Check actions IDs before approving migration.")
        selected_indexes = {int(value) for value in selected}
        if not selected_indexes:
            raise RuntimeError("Select at least one internal id migration file.")
        selected_paths = []
        for index, row in enumerate(rows):
            if index in selected_indexes and row.get("changes"):
                selected_paths.append(row.get("path"))
        result = ctx.apply_internal_ids_migration(fingerprint, selected_paths)
        for row in result["changed"]:
            ctx.add_detail(details, f"Migrated internal ids in {row['path']}.")
        commit_pending_internal_ids_migration(ctx, options, details)
        preview = result["preview"]
        unresolved_count = len(preview.get("unresolved") or [])
        changed_count = result["changed_count"]
        file_word = "file" if changed_count == 1 else "files"
        if unresolved_count:
            item_word = "item" if unresolved_count == 1 else "items"
            verb = "remains" if unresolved_count == 1 else "remain"
            message = f"Migrated {changed_count} {file_word}. {unresolved_count} unresolved {item_word} {verb}."
            ctx.add_detail(details, f"{unresolved_count} unresolved {item_word} {verb}. Review unresolved device blocks.")
        else:
            message = f"Migrated {changed_count} {file_word}."
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
        run_lock.release()


def run_retained_devices_preview_job(ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "retained_devices_preview",
                "last_message": "Another HA Ops action is already running.",
            }
        )
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
            "last_message": "Checking retained devices.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        if state.get("deleted_devices_pending_confirmation"):
            raise RuntimeError("Confirm or revert the pending deleted_devices cleanup before checking retained devices.")
        ctx.add_detail(details, "Checking retained MQTT discovery against current Zigbee2MQTT files.")
        preview = ctx.build_retained_devices_preview()
        rows = retained_device_rows(preview["candidates"])
        count = len(rows)
        log_action(ctx, f"retained devices preview: found {count} candidate(s)")
        message = f"Found {count} retained device candidate{'s' if count != 1 else ''}."
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
        run_lock.release()


def run_retained_devices_delete_job(selected, ctx):
    run_lock = ctx.run_lock
    write_state = ctx.write_state
    utc_now = ctx.utc_now

    if not run_lock.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "retained_devices_delete",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    log_action(ctx, "retained devices delete: started")
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "retained_devices_delete",
            "last_message": "Deleting retained devices.",
            "last_details": details,
        }
    )

    try:
        state = ctx.read_state()
        rows = state.get("last_retained_devices_rows") or []
        if not rows:
            raise RuntimeError("Run Check retained devices before approving deletion.")
        selected_indexes = {int(value) for value in selected}
        if not selected_indexes:
            raise RuntimeError("Select at least one retained device candidate to delete.")
        topics = []
        for index, row in enumerate(rows):
            if index not in selected_indexes:
                continue
            topics.extend(row.get("retained_topics") or [])
        if not topics:
            raise RuntimeError("Selected retained device candidates have no retained discovery topics.")

        cleared = []
        for topic in sorted(set(topics)):
            ctx.clear_retained_discovery_topic(topic)
            cleared.append(topic)
            ctx.add_detail(details, f"Cleared retained MQTT discovery topic: {topic}")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "retained_devices_delete",
                "last_message": f"Deleted retained discovery for {len(selected_indexes)} retained device candidate(s).",
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
    log_action(ctx, "deleted_devices delete: started")
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
        log_action(ctx, f"deleted_devices delete: cleared {removed} deleted device(s)")
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
        log_action(ctx, "deleted_devices delete: Core restarted, waiting for confirmation")

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
        log_action(ctx, f"deleted_devices delete: failed: {exc}")
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
    log_action(ctx, "deleted_devices confirm: started")
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
        rollback_path = state.get("deleted_devices_rollback_path")
        if not rollback_path:
            raise RuntimeError("deleted_devices rollback snapshot is missing.")
        applied_fingerprint = state.get("deleted_devices_applied_fingerprint")
        cleanup_status = ctx.deleted_devices_cleanup_status(rollback_path)
        if cleanup_status["returned"] > 0:
            raise RuntimeError("deleted_devices entries removed by this cleanup returned. Review manually before confirming.")
        if applied_fingerprint and cleanup_status["fingerprint"] != applied_fingerprint:
            if cleanup_status["added"] > 0:
                details.append(
                    f"Device registry contains {cleanup_status['added']} new deleted_devices entr"
                    f"{'y' if cleanup_status['added'] == 1 else 'ies'}; keeping them."
                )
                log_action(ctx, "deleted_devices confirm: new deleted_devices are present and preserved")
            else:
                details.append("Device registry changed after deletion, but removed deleted_devices did not return.")
                log_action(ctx, "deleted_devices confirm: registry fingerprint changed, removed deleted_devices did not return")
        ctx.discard_deleted_devices_rollback(rollback_path)
        log_action(ctx, "deleted_devices confirm: confirmed and discarded rollback")
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
        log_action(ctx, f"deleted_devices confirm: failed: {exc}")
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
    log_action(ctx, "deleted_devices revert: started")
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
        rollback_path = state.get("deleted_devices_rollback_path")
        if not rollback_path:
            raise RuntimeError("deleted_devices rollback snapshot is missing.")

        backup_slug = ctx.ensure_fresh_system_backup(options, details)
        ctx.add_detail(details, "Stopping Home Assistant Core before restoring deleted_devices.")
        ctx.core_stop()
        core_stopped = True
        result = ctx.restore_deleted_devices_rollback(rollback_path)
        restore_applied = True
        ctx.add_detail(details, f"Restored {result.get('restored', 0)} deleted_devices entry(s).")
        if result.get("preserved", 0) > 0:
            ctx.add_detail(details, f"Preserved {result.get('preserved', 0)} current deleted_devices entry(s).")
        ctx.add_detail(details, "Preserved other current core.device_registry changes.")
        ctx.add_detail(details, "Starting Home Assistant Core.")
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
        log_action(ctx, f"deleted_devices revert: failed: {exc}")
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

        prepare_repo_checkout_for_sync(ctx, options, details, "Apply Git to HA")
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
                "post_apply_save_recommended": True,
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
            **state_store.ALL_PREVIEW_CLEAR_UPDATES,
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

        prepare_repo_checkout_for_sync(ctx, options, details, "Preview Git to HA")
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
                "last_preview_live_fingerprints": preview.get("live_fingerprints", {}),
                "last_preview_warnings": preview.get("warnings", []),
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
