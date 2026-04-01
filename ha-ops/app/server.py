from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import html
import json
import os
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path


HOST = "0.0.0.0"
PORT = 8099
OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/state.json")
RELEASES_DIR = Path("/data/releases")
CONFIG_DIR = Path("/homeassistant")
ADDON_CONFIGS_DIR = Path("/addon_configs")
WORK_DIR = Path("/data/work")
GENERATED_DEPLOY_KEY_PATH = WORK_DIR / "generated_deploy_key"
GENERATED_DEPLOY_KEY_PUB_PATH = WORK_DIR / "generated_deploy_key.pub"

STATE_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def release_now():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def load_options():
    return load_json(OPTIONS_PATH, {})


def default_state():
    return {
        "last_run_at": None,
        "last_status": "idle",
        "last_action": None,
        "last_message": "No runs yet.",
        "last_details": [],
        "last_applied_commit": None,
        "last_fetched_commit": None,
        "last_release": None,
        "last_backup_slug": None,
        "last_targets": [],
    }


def read_state():
    return load_json(STATE_PATH, default_state())


def write_state(updates):
    with STATE_LOCK:
        state = read_state()
        state.update(updates)
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
        return state


def list_releases():
    releases = []
    if not RELEASES_DIR.exists():
        return releases

    for path in sorted(RELEASES_DIR.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        metadata_path = path / "release.json"
        metadata = load_json(metadata_path, {})
        releases.append(
            {
                "name": path.name,
                "created_at": metadata.get("created_at"),
                "commit": metadata.get("commit"),
                "backup_slug": metadata.get("backup_slug"),
                "targets": metadata.get("targets", []),
            }
        )
    return releases


def generated_deploy_key_exists():
    return GENERATED_DEPLOY_KEY_PATH.exists() and GENERATED_DEPLOY_KEY_PUB_PATH.exists()


def load_generated_public_key():
    if not GENERATED_DEPLOY_KEY_PUB_PATH.exists():
        return ""
    return GENERATED_DEPLOY_KEY_PUB_PATH.read_text().strip()


def git_auth_mode(options):
    if options.get("git_ssh_key", "").strip():
        return "manual"
    if generated_deploy_key_exists():
        return "generated"
    return "none"


def setup_git_ssh_env(env, key_text=None, key_path=None):
    if key_text:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        key_path = WORK_DIR / "manual_deploy_key"
        key_path.write_text(key_text)
        os.chmod(key_path, 0o600)

    if key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"


def generate_deploy_key():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for path in [GENERATED_DEPLOY_KEY_PATH, GENERATED_DEPLOY_KEY_PUB_PATH]:
        if path.exists():
            path.unlink()

    comment = f"ha-ops@{socket.gethostname()}"
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
                str(GENERATED_DEPLOY_KEY_PATH),
            ]
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ssh-keygen is not available inside the add-on image") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed:\n{result.stderr.strip() or result.stdout.strip()}")

    os.chmod(GENERATED_DEPLOY_KEY_PATH, 0o600)
    public_key = load_generated_public_key()
    log(f"Generated deploy key with comment {comment}")
    return public_key


def run_command(command, env=None, cwd=None):
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def log(message):
    print(f"[ha-ops] {message}", flush=True)


def add_detail(details, message):
    details.append(message)
    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_message": message,
            "last_details": details,
        }
    )


def call_supervisor(method, path, payload=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")

    command = [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        method,
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
    ]
    if payload is not None:
        command.extend(["-d", json.dumps(payload)])
    command.append(f"http://supervisor{path}")

    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Supervisor API call failed for {path}:\n{result.stderr.strip() or result.stdout.strip()}")

    body = result.stdout.strip()
    if not body:
        return {}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def supervisor_ok(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("result") == "ok":
        return True
    return "data" in payload and "result" not in payload


def get_installed_addons():
    payload = call_supervisor("GET", "/addons")
    addons = payload.get("data", {}).get("addons")
    if addons is None:
        addons = payload.get("addons", [])
    return addons


def get_addon_info(slug):
    payload = call_supervisor("GET", f"/addons/{slug}/info")
    if "data" in payload:
        return payload["data"]
    return payload


def addon_action(slug, action):
    payload = call_supervisor("POST", f"/addons/{slug}/{action}")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Add-on {slug} {action} failed: {payload}")


def core_stop():
    payload = call_supervisor("POST", "/core/stop")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core stop failed: {payload}")


def core_start():
    payload = call_supervisor("POST", "/core/start")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core start failed: {payload}")


def core_restart():
    payload = call_supervisor("POST", "/core/restart")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core restart failed: {payload}")


def do_core_check():
    payload = call_supervisor("POST", "/core/check")
    data = payload.get("data", {})
    if payload.get("result") == "ok" and data.get("result") == "valid":
        return
    raise RuntimeError(f"Home Assistant config check failed: {payload}")


def create_ha_backup(name_prefix, resolved_targets):
    addons = []
    include_homeassistant = False
    for target in resolved_targets:
        if target["type"] == "homeassistant":
            include_homeassistant = True
        elif target["type"] == "addon":
            addons.append(target["resolved_slug"])

    payload = {}
    if include_homeassistant:
        payload["homeassistant"] = True
    if addons:
        payload["addons"] = addons
    if not payload:
        return None

    payload["name"] = f"{name_prefix} {release_now()}"
    payload["background"] = False
    result = call_supervisor("POST", "/backups/new/partial", payload)
    slug = result.get("data", {}).get("slug") or result.get("slug")
    if not slug:
        raise RuntimeError(f"Backup creation did not return a slug: {result}")
    return slug


def ensure_repo(options):
    repo_dir = Path("/data") / options.get("repo_path", "ha-config")
    repo_url = options.get("repo_url", "").strip()
    if not repo_url:
        raise RuntimeError("repo_url is empty")

    env = os.environ.copy()
    git_ssh_key = options.get("git_ssh_key", "").strip()
    if git_ssh_key:
        setup_git_ssh_env(env, key_text=git_ssh_key)
    elif generated_deploy_key_exists():
        setup_git_ssh_env(env, key_path=GENERATED_DEPLOY_KEY_PATH)

    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = run_command(["git", "clone", repo_url, str(repo_dir)], env=env)
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed:\n{clone.stderr.strip()}")

    fetch = run_command(["git", "fetch", "origin"], env=env, cwd=repo_dir)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed:\n{fetch.stderr.strip()}")

    branch = options.get("repo_branch", "main")
    checkout = run_command(["git", "checkout", branch], env=env, cwd=repo_dir)
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")

    reset = run_command(["git", "reset", "--hard", f"origin/{branch}"], env=env, cwd=repo_dir)
    if reset.returncode != 0:
        raise RuntimeError(f"git reset to origin/{branch} failed:\n{reset.stderr.strip()}")

    return repo_dir


def git_commit(repo_dir, ref):
    result = run_command(["git", "rev-parse", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git rev-parse {ref} failed")
    return result.stdout.strip()


def load_manifest(repo_dir, options):
    manifest_path = repo_dir / options.get("manifest_path", "ha-ops.json")
    if not manifest_path.exists():
        fallback = {
            "version": 1,
            "targets": [
                {
                    "id": "homeassistant",
                    "type": "homeassistant",
                    "source": options.get("apply_path", "homeassistant"),
                    "delete": True,
                    "stop_core_before_sync_if_storage": True,
                    "restart_after_sync": options.get("restart_after_apply", True),
                }
            ],
        }
        return fallback, manifest_path

    return load_json(manifest_path, {}), manifest_path


def resolve_addon_slug(target, addons):
    exact = target.get("addon_slug")
    if exact:
        for addon in addons:
            if addon.get("slug") == exact:
                return exact
        if target.get("optional"):
            return None
        raise RuntimeError(f"Configured add-on slug was not found: {exact}")

    suffix = target.get("addon_slug_suffix")
    if suffix:
        matches = [addon for addon in addons if addon.get("slug", "").endswith(suffix)]
        if len(matches) == 1:
            return matches[0]["slug"]
        if not matches and target.get("optional"):
            return None
        raise RuntimeError(f"Expected one add-on slug ending with '{suffix}', found {len(matches)}")

    name_contains = target.get("addon_name_contains")
    if name_contains:
        matches = [
            addon
            for addon in addons
            if name_contains.lower() in addon.get("name", "").lower()
        ]
        if len(matches) == 1:
            return matches[0]["slug"]
        if not matches and target.get("optional"):
            return None
        raise RuntimeError(f"Expected one add-on name containing '{name_contains}', found {len(matches)}")

    raise RuntimeError(f"Add-on target '{target.get('id')}' is missing resolver fields")


def resolve_targets(repo_dir, manifest, addons):
    targets = []
    for target in manifest.get("targets", []):
        target_type = target.get("type")
        source = repo_dir / target.get("source", "")
        optional = bool(target.get("optional", False))

        if not source.exists():
            if optional:
                continue
            raise RuntimeError(f"Source path does not exist for target '{target.get('id')}': {source}")

        resolved = dict(target)
        resolved["source_path"] = str(source)

        if target_type == "homeassistant":
            resolved["resolved_slug"] = None
            resolved["live_path"] = str(CONFIG_DIR)
        elif target_type == "addon":
            slug = resolve_addon_slug(target, addons)
            if slug is None:
                continue
            resolved["resolved_slug"] = slug
            resolved["live_path"] = str(ADDON_CONFIGS_DIR / slug)
        else:
            raise RuntimeError(f"Unsupported target type: {target_type}")

        targets.append(resolved)

    if not targets:
        raise RuntimeError("No managed targets resolved from the manifest")

    return targets


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def sync_tree(src, dest, delete=True):
    ensure_dir(dest)
    command = ["rsync", "-a"]
    if delete:
        command.append("--delete")
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Sync failed from {src} to {dest}:\n{result.stderr.strip()}")


def clear_tree(dest):
    ensure_dir(dest)
    empty_dir = Path("/data/work/empty")
    ensure_dir(empty_dir)
    sync_tree(empty_dir, dest, delete=True)


def source_has_storage(path):
    return (path / ".storage").exists()


def create_release_snapshot(resolved_targets, commit, backup_slug):
    release_name = release_now()
    release_dir = RELEASES_DIR / release_name
    ensure_dir(release_dir)

    metadata = {
        "created_at": utc_now(),
        "commit": commit,
        "backup_slug": backup_slug,
        "targets": [],
    }

    for target in resolved_targets:
        live_path = Path(target["live_path"])
        target_snapshot = release_dir / target["id"]
        ensure_dir(target_snapshot)

        existed = live_path.exists()
        if existed:
            sync_tree(live_path, target_snapshot, delete=True)

        metadata["targets"].append(
            {
                "id": target["id"],
                "type": target["type"],
                "resolved_slug": target.get("resolved_slug"),
                "live_path": target["live_path"],
                "source_path": target["source_path"],
                "delete": bool(target.get("delete", True)),
                "restart_after_sync": bool(target.get("restart_after_sync", True)),
                "stop_addon_before_sync": bool(target.get("stop_addon_before_sync", False)),
                "stop_core_before_sync_if_storage": bool(target.get("stop_core_before_sync_if_storage", False)),
                "existed": existed,
            }
        )

    (release_dir / "release.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    return release_name


def restore_release_snapshot(release_name, details):
    release_dir = RELEASES_DIR / release_name
    metadata_path = release_dir / "release.json"
    if not metadata_path.exists():
        raise RuntimeError(f"Release metadata not found for {release_name}")

    metadata = load_json(metadata_path, {})
    targets = metadata.get("targets", [])
    core_stopped = False
    homeassistant_seen = False

    for target in targets:
        live_path = Path(target["live_path"])
        snapshot_path = release_dir / target["id"]
        target_type = target.get("type")
        addon_was_started = False

        if target_type == "homeassistant" and not core_stopped:
            add_detail(details, f"Stopping Home Assistant Core for rollback of release {release_name}.")
            core_stop()
            core_stopped = True
            homeassistant_seen = True
        elif target_type == "addon" and target.get("stop_addon_before_sync", False):
            slug = target.get("resolved_slug")
            add_detail(details, f"Stopping add-on {slug} before rollback sync.")
            addon_was_started = stop_addon_for_sync(slug)

        if target.get("existed", True):
            add_detail(details, f"Restoring {target['id']} from release {release_name}.")
            sync_tree(snapshot_path, live_path, delete=bool(target.get("delete", True)))
        else:
            add_detail(details, f"Clearing {target['id']} because it did not exist in release {release_name}.")
            clear_tree(live_path)

        if target_type == "addon" and target.get("restart_after_sync", True):
            slug = target.get("resolved_slug")
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    add_detail(details, f"Starting add-on {slug} after rollback.")
                    addon_action(slug, "start")
            else:
                add_detail(details, f"Restarting add-on {slug} after rollback.")
                restart_or_start_addon(slug)

    if homeassistant_seen:
        add_detail(details, "Starting Home Assistant Core after rollback.")
        core_start()

    return metadata


def restart_or_start_addon(slug):
    info = get_addon_info(slug)
    state = info.get("state")
    if state == "started":
        addon_action(slug, "restart")
    else:
        addon_action(slug, "start")


def stop_addon_for_sync(slug):
    info = get_addon_info(slug)
    was_started = info.get("state") == "started"
    if was_started:
        addon_action(slug, "stop")
    return was_started


def apply_targets(resolved_targets, details):
    homeassistant_target = None
    core_stopped = False

    for target in resolved_targets:
        source_path = Path(target["source_path"])
        live_path = Path(target["live_path"])
        addon_was_started = False

        if target["type"] == "homeassistant":
            homeassistant_target = target
            if target.get("stop_core_before_sync_if_storage", False) and source_has_storage(source_path) and not core_stopped:
                add_detail(details, "Stopping Home Assistant Core before syncing .storage.")
                core_stop()
                core_stopped = True
        elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
            slug = target["resolved_slug"]
            add_detail(details, f"Stopping add-on {slug} before sync.")
            addon_was_started = stop_addon_for_sync(slug)

        add_detail(details, f"Syncing {target['id']} from {source_path} to {live_path}.")
        sync_tree(source_path, live_path, delete=bool(target.get("delete", True)))

        if target["type"] == "addon" and target.get("restart_after_sync", True):
            slug = target["resolved_slug"]
            if target.get("stop_addon_before_sync", False):
                if addon_was_started:
                    add_detail(details, f"Starting add-on {slug} after sync.")
                    addon_action(slug, "start")
            else:
                add_detail(details, f"Restarting add-on {slug}.")
                restart_or_start_addon(slug)

    if homeassistant_target is None:
        return

    if core_stopped:
        if homeassistant_target.get("restart_after_sync", True):
            add_detail(details, "Starting Home Assistant Core after sync.")
            core_start()
    else:
        add_detail(details, "Running Home Assistant config check.")
        do_core_check()
        if homeassistant_target.get("restart_after_sync", True):
            add_detail(details, "Restarting Home Assistant Core.")
            core_restart()


def run_apply_job():
    if not RUN_LOCK.acquire(blocking=False):
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
    options = load_options()
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
        repo_dir = ensure_repo(options)
        commit = git_commit(repo_dir, "HEAD")
        manifest, manifest_path = load_manifest(repo_dir, options)
        addons = get_installed_addons()
        resolved_targets = resolve_targets(repo_dir, manifest, addons)

        add_detail(details, f"Fetched repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")

        if options.get("create_ha_backup", True):
            add_detail(details, "Creating Home Assistant partial backup for managed targets.")
            backup_slug = create_ha_backup(options.get("ha_backup_name_prefix", "ha-ops"), resolved_targets)
            add_detail(details, f"Created Home Assistant backup {backup_slug}.")

        if options.get("create_release_snapshot", True):
            add_detail(details, "Creating local release snapshot.")
            release_name = create_release_snapshot(resolved_targets, commit, backup_slug)
            add_detail(details, f"Created local release snapshot {release_name}.")

        apply_targets(resolved_targets, details)

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "apply",
                "last_message": "Apply finished successfully.",
                "last_details": details,
                "last_applied_commit": commit,
                "last_fetched_commit": commit,
                "last_release": release_name,
                "last_backup_slug": backup_slug,
                "last_targets": resolved_targets,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        if release_name:
            try:
                add_detail(details, f"Restoring local release snapshot {release_name} after failure.")
                restore_release_snapshot(release_name, details)
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
        RUN_LOCK.release()


def run_rollback_job(release_name):
    if not RUN_LOCK.acquire(blocking=False):
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
        metadata = restore_release_snapshot(release_name, details)
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "rollback",
                "last_message": f"Rollback to {release_name} finished successfully.",
                "last_details": details,
                "last_release": release_name,
                "last_applied_commit": metadata.get("commit"),
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
        RUN_LOCK.release()


def start_apply():
    thread = threading.Thread(target=run_apply_job, daemon=True)
    thread.start()


def start_rollback(release_name):
    thread = threading.Thread(target=run_rollback_job, args=(release_name,), daemon=True)
    thread.start()


def current_manifest_preview():
    options = load_options()
    repo_dir = Path("/data") / options.get("repo_path", "ha-config")
    if not repo_dir.exists():
        return []

    try:
        manifest, _ = load_manifest(repo_dir, options)
        previews = []
        for target in manifest.get("targets", []):
            previews.append(
                {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "source": target.get("source"),
                    "addon_slug": target.get("addon_slug"),
                    "addon_slug_suffix": target.get("addon_slug_suffix"),
                }
            )
        return previews
    except Exception:
        return []


def render_targets(items):
    if not items:
        return "<p>No target preview yet. Run an apply after configuring the repository.</p>"

    rows = []
    for item in items:
        target = html.escape(str(item.get("id")))
        target_type = html.escape(str(item.get("type")))
        source = html.escape(str(item.get("source") or item.get("source_path")))
        live_path = html.escape(str(item.get("live_path", "")))
        addon = html.escape(str(item.get("resolved_slug") or item.get("addon_slug") or item.get("addon_slug_suffix") or ""))
        rows.append(
            "<tr>"
            f"<td><code>{target}</code></td>"
            f"<td>{target_type}</td>"
            f"<td><code>{source}</code></td>"
            f"<td><code>{addon}</code></td>"
            f"<td><code>{live_path}</code></td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Target</th><th>Type</th><th>Source</th><th>Add-on</th><th>Live Path</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_releases(releases):
    if not releases:
        return "<p>No local release snapshots yet.</p>"

    rows = []
    for release in releases[:12]:
        name = html.escape(release["name"])
        created_at = html.escape(str(release.get("created_at")))
        commit = html.escape(str(release.get("commit")))
        backup_slug = html.escape(str(release.get("backup_slug")))
        rows.append(
            "<tr>"
            f"<td><code>{name}</code></td>"
            f"<td>{created_at}</td>"
            f"<td><code>{commit}</code></td>"
            f"<td><code>{backup_slug}</code></td>"
            "<td>"
            f"<form method='post' action='/rollback'>"
            f"<input type='hidden' name='release' value='{name}'>"
            "<button type='submit' class='secondary'>Rollback</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Release</th><th>Created</th><th>Commit</th><th>HA Backup</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_git_auth(options):
    mode = git_auth_mode(options)
    public_key = html.escape(load_generated_public_key())
    repo_url = options.get("repo_url", "")
    uses_ssh = repo_url.startswith("git@") or repo_url.startswith("ssh://")

    if mode == "manual":
        status = "<p>Using the private key from <code>git_ssh_key</code> in add-on configuration.</p>"
        key_block = ""
    elif mode == "generated":
        status = "<p>Using the deploy key generated and stored inside HA Ops.</p>"
        key_block = (
            "<p>Add this public key to GitHub as a read-only Deploy Key for <code>ha-config</code>.</p>"
            f"<pre>{public_key}</pre>"
        )
    else:
        status = "<p>No SSH key is configured yet.</p>"
        key_block = ""

    hint = ""
    if uses_ssh and mode == "none":
        hint = "<p>Click <strong>Generate Deploy Key</strong>, then paste the public key into GitHub Deploy Keys.</p>"
    elif not uses_ssh:
        hint = "<p>Your repository URL is not SSH-based, so a deploy key may not be needed.</p>"

    action = (
        "<form method='post' action='/generate-key'>"
        "<button type='submit' class='secondary'>Generate Deploy Key</button>"
        "</form>"
    )
    if mode == "generated":
        action = (
            "<form method='post' action='/generate-key'>"
            "<button type='submit' class='secondary'>Regenerate Deploy Key</button>"
            "</form>"
        )

    return f"{status}{hint}<div class='actions'>{action}</div>{key_block}"


def render_page():
    options = load_options()
    state = read_state()
    releases = list_releases()
    manifest_preview = current_manifest_preview()
    target_state = state.get("last_targets") or manifest_preview
    status = html.escape(state.get("last_status", "idle"))
    message = html.escape(state.get("last_message", ""))
    last_run = html.escape(str(state.get("last_run_at")))
    last_release = html.escape(str(state.get("last_release")))
    last_backup_slug = html.escape(str(state.get("last_backup_slug")))
    last_applied_commit = html.escape(str(state.get("last_applied_commit")))
    last_fetched_commit = html.escape(str(state.get("last_fetched_commit")))
    repo_url = html.escape(options.get("repo_url", ""))
    branch = html.escape(options.get("repo_branch", "main"))
    manifest_path = html.escape(options.get("manifest_path", "ha-ops.json"))
    auth_mode = html.escape(git_auth_mode(options))
    details = "\n".join(state.get("last_details", []))
    details_html = html.escape(details)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HA Ops</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #070b16;
      --panel: rgba(15, 23, 42, 0.92);
      --line: rgba(148, 163, 184, 0.2);
      --text: #e5eef9;
      --muted: #9fb0c6;
      --accent: #22c55e;
      --accent2: #14b8a6;
      --warn: #fb923c;
      --danger: #f87171;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(20, 184, 166, 0.28), transparent 32%),
        radial-gradient(circle at top right, rgba(34, 197, 94, 0.18), transparent 28%),
        linear-gradient(180deg, #0f172a, #020617 68%);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 22px;
      box-shadow: 0 24px 80px rgba(2, 6, 23, 0.34);
      backdrop-filter: blur(16px);
    }}
    h1, h2 {{
      margin: 0 0 14px;
      letter-spacing: -0.03em;
    }}
    h1 {{
      font-size: 2.1rem;
    }}
    h2 {{
      font-size: 1.1rem;
    }}
    p, li {{
      color: var(--muted);
      line-height: 1.55;
    }}
    dl {{
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 10px 14px;
      margin: 18px 0 0;
    }}
    dt {{
      color: var(--muted);
    }}
    dd {{
      margin: 0;
      word-break: break-word;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 0.8rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      background: rgba(34, 197, 94, 0.16);
      color: #bbf7d0;
    }}
    .badge.error {{
      background: rgba(248, 113, 113, 0.16);
      color: #fecaca;
    }}
    .badge.running {{
      background: rgba(20, 184, 166, 0.16);
      color: #99f6e4;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 22px;
    }}
    button {{
      border: none;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      color: white;
      font-size: 0.96rem;
      padding: 12px 18px;
      cursor: pointer;
      font-weight: 600;
    }}
    button.secondary {{
      background: rgba(148, 163, 184, 0.16);
      color: var(--text);
    }}
    pre {{
      margin: 0;
      background: rgba(2, 6, 23, 0.7);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      overflow: auto;
      white-space: pre-wrap;
      line-height: 1.45;
      color: #dbeafe;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    code {{
      font-family: ui-monospace, monospace;
      font-size: 0.92em;
    }}
    .wide {{
      margin-top: 18px;
    }}
  </style>
</head>
<body>
  <main>
    <div class="grid">
      <section class="card">
        <h1>HA Ops</h1>
        <p>Git-backed config deployer for Home Assistant, Mosquitto, and Zigbee2MQTT.</p>
        <div class="badge {'error' if status == 'error' else 'running' if status == 'running' else ''}">{status}</div>
        <dl>
          <dt>Repo URL</dt>
          <dd><code>{repo_url or "(not configured)"}</code></dd>
          <dt>Branch</dt>
          <dd><code>{branch}</code></dd>
          <dt>Manifest</dt>
          <dd><code>{manifest_path}</code></dd>
          <dt>Git auth</dt>
          <dd><code>{auth_mode}</code></dd>
          <dt>Last run</dt>
          <dd>{last_run}</dd>
          <dt>Fetched commit</dt>
          <dd><code>{last_fetched_commit}</code></dd>
          <dt>Applied commit</dt>
          <dd><code>{last_applied_commit}</code></dd>
          <dt>Release snapshot</dt>
          <dd><code>{last_release}</code></dd>
          <dt>HA backup</dt>
          <dd><code>{last_backup_slug}</code></dd>
        </dl>
        <p>{message}</p>
        <div class="actions">
          <form method="post" action="/apply">
            <button type="submit">Pull And Apply</button>
          </form>
        </div>
      </section>
      <section class="card">
        <h2>Last Run Details</h2>
        <pre>{details_html or "No details yet."}</pre>
      </section>
    </div>

    <section class="card wide">
      <h2>Git Access</h2>
      {render_git_auth(options)}
    </section>

    <section class="card wide">
      <h2>Managed Targets</h2>
      {render_targets(target_state)}
    </section>

    <section class="card wide">
      <h2>Release Snapshots</h2>
      {render_releases(releases)}
    </section>
  </main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_html(self, content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            return

        self.send_html(render_page())

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = parse_qs(self.rfile.read(length).decode()) if length else {}

        if parsed.path == "/generate-key":
            try:
                public_key = generate_deploy_key()
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "idle",
                        "last_action": "generate_key",
                        "last_message": "Generated a new deploy key. Add the public key to GitHub Deploy Keys.",
                        "last_details": [public_key],
                    }
                )
                log("Generate Deploy Key completed successfully")
            except Exception as exc:
                log(f"Generate Deploy Key failed: {exc}")
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "error",
                        "last_action": "generate_key",
                        "last_message": str(exc),
                        "last_details": [str(exc)],
                    }
                )
            self.send_html(render_page())
            return

        if parsed.path == "/apply":
            start_apply()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if parsed.path == "/rollback":
            release = body.get("release", [""])[0]
            if not release:
                self.send_error(400, "Missing release")
                return
            start_rollback(release)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        self.send_error(404)

    def log_message(self, format, *args):
        return


def main():
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    write_state(read_state())
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
