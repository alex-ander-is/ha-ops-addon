import git_ops
import i18n


def _(key, **values):
    return i18n.t(key, **values)


def resolve_save_unknown_base_conflict(ctx, path, choice):
    safe_path = git_ops.safe_repo_relative_path(path)
    if choice not in {"ha", "git"}:
        raise RuntimeError(_("error.invalid_conflict_choice"))

    state = ctx.read_state()
    conflicts = list(state.get("conflicts", []))
    if safe_path not in conflicts:
        raise RuntimeError(_("error.save_conflict_path_not_pending"))

    resolutions = dict(state.get("save_conflict_resolutions", {}))
    resolutions[safe_path] = choice
    remaining = [item for item in conflicts if item != safe_path]
    if remaining:
        message = _("message.resolved_save_conflict_remaining", path=safe_path, count=len(remaining))
        ctx.write_state(
            {
                "conflicts": remaining,
                "conflict_type": "save_unknown_base",
                "save_conflict_resolutions": resolutions,
                "last_status": "conflicts",
                "last_message": message,
                "last_details": state.get("last_details", []),
            }
        )
        return message

    ctx.write_state(
        {
            "conflicts": [],
            "conflict_type": None,
            "save_conflict_resolutions": resolutions,
            "last_status": "idle",
            "last_message": _("message.save_conflicts_resolved_rerun"),
            "last_details": state.get("last_details", []),
        }
    )
    return _("message.all_save_conflicts_resolved_rerun")


def approve_save_unknown_base_conflicts(ctx):
    state = ctx.read_state()
    if state.get("conflict_type") != "save_unknown_base":
        raise RuntimeError(_("message.no_save_conflicts_pending_approval"))
    conflicts = list(state.get("conflicts", []))
    if not conflicts:
        raise RuntimeError(_("message.no_save_conflicts_pending_approval"))

    resolutions = dict(state.get("save_conflict_resolutions", {}))
    for path in conflicts:
        resolutions[git_ops.safe_repo_relative_path(path)] = "ha"

    ctx.write_state(
        {
            "conflicts": [],
            "conflict_type": None,
            "save_conflict_resolutions": resolutions,
            "last_status": "idle",
            "last_message": _("message.approved_save_conflicts_saving_state"),
            "last_details": state.get("last_details", []),
        }
    )
    return _("message.approved_save_conflicts_count", count=len(conflicts))


def finish_git_conflict_resolution(ctx, repo_dir, env, branch):
    if ctx.git_rebase_in_progress(repo_dir):
        env["GIT_EDITOR"] = "true"
        cont = ctx.run_command(["git", "rebase", "--continue"], env=env, cwd=repo_dir)
        if cont.returncode != 0:
            output = cont.stderr.strip() or cont.stdout.strip()
            if "No changes" in output or "previous cherry-pick is now empty" in output:
                skip = ctx.run_command(["git", "rebase", "--skip"], env=env, cwd=repo_dir)
                if skip.returncode != 0:
                    raise RuntimeError(f"git rebase --skip failed:\n{skip.stderr.strip() or skip.stdout.strip()}")
            else:
                raise RuntimeError(f"git rebase --continue failed:\n{output}")

    ctx.push_branch(repo_dir, env, branch)
    ctx.write_state(
        {
            "conflicts": [],
            "conflict_type": None,
            "save_conflict_resolutions": {},
            "last_status": "success",
            "last_message": _("message.conflicts_resolved_pushed"),
        }
    )
    return _("message.all_conflicts_resolved_pushed")


def resolve_git_conflict(ctx, path, choice):
    state = ctx.read_state()
    if state.get("conflict_type") == "save_unknown_base":
        return resolve_save_unknown_base_conflict(ctx, path, choice)

    options = ctx.load_options()
    repo_dir = ctx.repo_checkout_path(options)
    branch = options.get("repo_branch", "main")
    safe_path = git_ops.safe_repo_relative_path(path)
    if choice not in {"ha", "git"}:
        raise RuntimeError(_("error.invalid_conflict_choice"))

    actual_conflicts = ctx.git_conflict_paths(repo_dir)
    if actual_conflicts:
        if safe_path not in actual_conflicts:
            raise RuntimeError(_("error.git_conflict_path_not_pending"))
        if choice == "ha":
            checkout = ctx.run_command(["git", "checkout", "--theirs", "--", safe_path], cwd=repo_dir)
        else:
            checkout = ctx.run_command(["git", "checkout", "--ours", "--", safe_path], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout conflict version failed:\n{checkout.stderr.strip()}")

        add = ctx.run_command(["git", "add", "--", safe_path], cwd=repo_dir)
        if add.returncode != 0:
            raise RuntimeError(f"git add conflict resolution failed:\n{add.stderr.strip()}")

        conflicts = ctx.git_conflict_paths(repo_dir)
        if conflicts:
            ctx.write_state({"conflicts": conflicts, "conflict_type": "git_rebase", "last_status": "conflicts"})
            return _("message.resolved_conflict_remaining", path=safe_path, count=len(conflicts))

    env = ctx.git_env(options)
    return finish_git_conflict_resolution(ctx, repo_dir, env, branch)
