def run_save_job(deps):
    run_lock = deps["run_lock"]
    write_state = deps["write_state"]
    utc_now = deps["utc_now"]

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
    options = deps["load_options"]()
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
        state = deps["read_state"]()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Save HA to Git.")

        repo_dir = deps["ensure_repo"](options, reset_to_origin=False)
        env = deps["git_env"](options)
        branch = options.get("repo_branch", "main")
        deps["git_pull_rebase"](repo_dir, env, branch)
        commit = deps["git_head_or_unborn"](repo_dir)
        addons = deps["get_installed_addons"]()
        manifest, manifest_path = deps["load_manifest"](repo_dir, options, addons)
        resolved_targets = deps["resolve_targets"](repo_dir, manifest, addons, require_source=False)

        deps["add_detail"](details, f"Using branch {branch} at commit {commit}.")
        deps["add_detail"](details, f"Using manifest {manifest_path}.")
        deps["add_detail"](details, "Saving live Home Assistant config to Git.")
        deps["export_targets"](resolved_targets, details)
        deps["stage_homeassistant_storage_allowlist"](repo_dir, options, details)
        deps["stage_all"](repo_dir)

        new_commit = deps["commit_if_needed"](repo_dir, f"Save Home Assistant config {deps['release_now']()}")
        if new_commit:
            deps["add_detail"](details, f"Created commit {new_commit}.")
            try:
                deps["push_branch"](repo_dir, env, branch)
            except RuntimeError:
                deps["git_pull_rebase"](repo_dir, env, branch)
                deps["push_branch"](repo_dir, env, branch)
            deps["add_detail"](details, f"Pushed to origin/{branch}.")
        else:
            deps["add_detail"](details, "No live Home Assistant changes to save.")

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
            repo_path = deps["repo_checkout_path"](options)
        except RuntimeError:
            repo_path = None
        conflicts = deps["git_conflict_paths"](repo_path) if repo_path and repo_path.exists() else []
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
        run_lock.release()


def run_apply_job(deps):
    run_lock = deps["run_lock"]
    write_state = deps["write_state"]
    utc_now = deps["utc_now"]

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
    options = deps["load_options"]()
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
        state = deps["read_state"]()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Apply Git to HA.")

        repo_dir = deps["ensure_repo"](options)
        commit = deps["git_head_or_unborn"](repo_dir)
        addons = deps["get_installed_addons"]()
        manifest, manifest_path = deps["load_manifest"](repo_dir, options, addons)
        resolved_targets = deps["resolve_targets"](repo_dir, manifest, addons, require_source=False)

        deps["add_detail"](details, f"Fetched repository at commit {commit}.")
        deps["add_detail"](details, f"Using manifest {manifest_path}.")
        deps["add_detail"](details, "Rebuilding apply preview for safety checks.")
        preview = deps["build_apply_preview"](resolved_targets)
        deps["ensure_preview_matches_state"](state, commit, preview)
        deps["enforce_apply_limits"](options, preview)

        backup_slug = deps["ensure_fresh_system_backup"](options, details)

        if deps["option_bool"](options, "create_release_snapshot", True):
            deps["add_detail"](details, "Creating local release snapshot.")
            release_name = deps["create_release_snapshot"](resolved_targets, commit, backup_slug)
            deps["add_detail"](details, f"Created local release snapshot {release_name}.")

        deps["apply_targets"](resolved_targets, details)
        pruned = deps["prune_release_snapshots"](options, protected_release=release_name)
        if pruned:
            deps["add_detail"](details, f"Pruned {len(pruned)} old local release snapshot(s): {', '.join(pruned)}.")

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
                deps["add_detail"](details, f"Restoring local release snapshot {release_name} after failure.")
                deps["restore_release_snapshot"](release_name, details)
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


def run_preview_job(deps):
    run_lock = deps["run_lock"]
    write_state = deps["write_state"]
    utc_now = deps["utc_now"]

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
    options = deps["load_options"]()
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
        state = deps["read_state"]()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Preview Git to HA.")

        repo_dir = deps["ensure_repo"](options)
        commit = deps["git_head_or_unborn"](repo_dir)
        addons = deps["get_installed_addons"]()
        manifest, manifest_path = deps["load_manifest"](repo_dir, options, addons)
        resolved_targets = deps["resolve_targets"](repo_dir, manifest, addons, require_source=False)

        deps["add_detail"](details, f"Fetched repository at commit {commit}.")
        deps["add_detail"](details, f"Using manifest {manifest_path}.")
        deps["add_detail"](details, "Building apply preview without changing live config.")
        preview = deps["build_apply_preview"](resolved_targets)

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
        run_lock.release()


def run_rollback_job(release_name, deps):
    run_lock = deps["run_lock"]
    write_state = deps["write_state"]
    utc_now = deps["utc_now"]

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
        metadata = deps["restore_release_snapshot"](release_name, details)
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
