import os
import socket


def generated_deploy_key_exists(key_path, pub_path):
    return key_path.exists() and pub_path.exists()


def load_generated_public_key(pub_path):
    if not pub_path.exists():
        return ""
    return pub_path.read_text().strip()


def git_auth_mode(options, key_path, pub_path):
    if options.get("git_ssh_key", "").strip():
        return "manual"
    if generated_deploy_key_exists(key_path, pub_path):
        return "generated"
    return "none"


def setup_git_ssh_env(env, work_dir, key_text=None, key_path=None):
    if key_text:
        work_dir.mkdir(parents=True, exist_ok=True)
        key_path = work_dir / "manual_deploy_key"
        key_path.write_text(key_text)
        os.chmod(key_path, 0o600)

    if key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return env


def git_env(options, work_dir, generated_key_path, generated_pub_path):
    env = os.environ.copy()
    git_ssh_key = options.get("git_ssh_key", "").strip()
    if git_ssh_key:
        setup_git_ssh_env(env, work_dir, key_text=git_ssh_key)
    elif generated_deploy_key_exists(generated_key_path, generated_pub_path):
        setup_git_ssh_env(env, work_dir, key_path=generated_key_path)
    return env


def generate_deploy_key(work_dir, key_path, pub_path, run_command, log):
    work_dir.mkdir(parents=True, exist_ok=True)
    comment = f"ha-ops@{socket.gethostname()}"
    temp_key_path = work_dir / "generated_deploy_key.new"
    temp_pub_path = work_dir / "generated_deploy_key.new.pub"
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

    temp_key_path.replace(key_path)
    temp_pub_path.replace(pub_path)
    os.chmod(key_path, 0o600)
    public_key = load_generated_public_key(pub_path)
    log(f"Generated deploy key with comment {comment}")
    return public_key
