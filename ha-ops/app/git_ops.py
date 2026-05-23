from pathlib import Path


def repo_checkout_path(options, data_dir):
    value = str(options.get("repo_path", "ha-config")).strip()
    path = Path(value)
    if not value or value == "." or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Invalid repo_path. Use a relative folder inside /data, for example ha-config.")

    repo_dir = (data_dir / path).resolve()
    data_root = data_dir.resolve()
    if repo_dir == data_root or data_root not in repo_dir.parents:
        raise RuntimeError("Invalid repo_path. Use a relative folder inside /data, for example ha-config.")
    return repo_dir


def ensure_repo(options, data_dir, git_env, run_command, reset_to_origin=True):
    repo_dir = repo_checkout_path(options, data_dir)
    repo_url = options.get("repo_url", "").strip()
    if not repo_url:
        raise RuntimeError("repo_url is empty")

    env = git_env(options)

    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = run_command(["git", "clone", repo_url, str(repo_dir)], env=env)
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed:\n{clone.stderr.strip()}")

    clean_repo_untracked(repo_dir, run_command)

    fetch = run_command(["git", "fetch", "origin"], env=env, cwd=repo_dir)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed:\n{fetch.stderr.strip()}")

    branch = options.get("repo_branch", "main")
    remote_ref = f"refs/remotes/origin/{branch}"
    remote_exists = git_ref_exists(repo_dir, remote_ref, run_command)

    if not reset_to_origin and git_ref_exists(repo_dir, f"refs/heads/{branch}", run_command):
        checkout = run_command(["git", "checkout", branch], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")
    elif remote_exists:
        checkout = run_command(["git", "checkout", "-B", branch, remote_ref], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")
    else:
        if git_ref_exists(repo_dir, "HEAD", run_command):
            checkout = run_command(["git", "checkout", "-B", branch], env=env, cwd=repo_dir)
        else:
            checkout = run_command(["git", "checkout", "--orphan", branch], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")

    if not reset_to_origin:
        return repo_dir

    if remote_exists:
        reset = run_command(["git", "reset", "--hard", f"origin/{branch}"], env=env, cwd=repo_dir)
        if reset.returncode != 0:
            raise RuntimeError(f"git reset to origin/{branch} failed:\n{reset.stderr.strip()}")

    clean_repo_untracked(repo_dir, run_command)
    return repo_dir


def clean_repo_untracked(repo_dir, run_command):
    clean = run_command(["git", "clean", "-ffdx"], cwd=repo_dir)
    if clean.returncode != 0:
        raise RuntimeError(f"git clean failed:\n{clean.stderr.strip()}")


def reset_repo_worktree(repo_dir, run_command):
    reset = run_command(["git", "reset", "--hard", "HEAD"], cwd=repo_dir)
    if reset.returncode != 0:
        unstage = run_command(["git", "rm", "-r", "--cached", "--ignore-unmatch", "."], cwd=repo_dir)
        if unstage.returncode != 0:
            raise RuntimeError(f"git reset failed:\n{reset.stderr.strip() or reset.stdout.strip()}")

    clean_repo_untracked(repo_dir, run_command)


def git_commit(repo_dir, ref, run_command):
    result = run_command(["git", "rev-parse", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git rev-parse {ref} failed")
    return result.stdout.strip()


def git_ref_exists(repo_dir, ref, run_command):
    result = run_command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir)
    return result.returncode == 0


def git_remote_head(repo_dir, env, branch, run_command):
    result = run_command(["git", "ls-remote", "--heads", "origin", branch], env=env, cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed:\n{result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        return None
    return output.split()[0]


def git_has_unpushed_commits(repo_dir, branch, run_command):
    remote_ref = f"refs/remotes/origin/{branch}"
    if not git_ref_exists(repo_dir, remote_ref, run_command):
        return git_ref_exists(repo_dir, "HEAD", run_command)

    result = run_command(["git", "rev-list", "--count", f"{remote_ref}..HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list failed:\n{result.stderr.strip()}")
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError as exc:
        raise RuntimeError(f"git rev-list returned invalid count: {result.stdout.strip()}") from exc


def git_head_or_unborn(repo_dir, run_command):
    try:
        return git_commit(repo_dir, "HEAD", run_command)
    except RuntimeError:
        return "unborn"


def git_conflict_paths(repo_dir, run_command):
    result = run_command(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_path(repo_dir, name, run_command):
    result = run_command(["git", "rev-parse", "--git-path", name], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse --git-path failed:\n{result.stderr.strip()}")
    path = Path(result.stdout.strip())
    if path.is_absolute():
        return path
    return repo_dir / path


def git_rebase_in_progress(repo_dir, run_command):
    return git_path(repo_dir, "rebase-merge", run_command).exists() or git_path(repo_dir, "rebase-apply", run_command).exists()


def git_pull_rebase(repo_dir, env, branch, run_command, write_conflicts):
    remote_head = git_remote_head(repo_dir, env, branch, run_command)
    if not remote_head:
        return None
    pull = run_command(["git", "pull", "--rebase", "origin", branch], env=env, cwd=repo_dir)
    if pull.returncode != 0:
        conflicts = git_conflict_paths(repo_dir, run_command)
        if conflicts:
            write_conflicts(conflicts)
        raise RuntimeError(f"git pull --rebase failed:\n{pull.stderr.strip() or pull.stdout.strip()}")
    return remote_head


def stage_all(repo_dir, run_command):
    add = run_command(["git", "add", "-A"], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed:\n{add.stderr.strip()}")


def stage_paths(repo_dir, paths, run_command):
    paths = [str(path) for path in paths if str(path)]
    if not paths:
        return
    add = run_command(["git", "add", "--", *paths], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed:\n{add.stderr.strip()}")


def commit_if_needed(repo_dir, message, run_command, git_status_porcelain):
    status = git_status_porcelain(repo_dir)
    if not status:
        return None
    commit = run_command(
        [
            "git",
            "-c",
            "user.name=HA Ops",
            "-c",
            "user.email=ha-ops@local",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed:\n{commit.stderr.strip() or commit.stdout.strip()}")
    return git_commit(repo_dir, "HEAD", run_command)


def push_branch(repo_dir, env, branch, run_command):
    push = run_command(["git", "push", "-u", "origin", branch], env=env, cwd=repo_dir)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed:\n{push.stderr.strip() or push.stdout.strip()}")


def git_status_porcelain(repo_dir, run_command):
    result = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def safe_repo_relative_path(value):
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Invalid conflict path")
    return str(path)
