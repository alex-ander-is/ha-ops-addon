from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import fnmatch
import html
import json
import os
import shutil
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path


HOST = "0.0.0.0"
PORT = 8099
ADDON_CONFIG_PATH = Path("/app/config.yaml")
OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/state.json")
RELEASES_DIR = Path("/data/releases")
CONFIG_DIR = Path("/homeassistant")
ADDON_CONFIGS_DIR = Path("/addon_configs")
WORK_DIR = Path("/data/work")
GENERATED_DEPLOY_KEY_PATH = WORK_DIR / "generated_deploy_key"
GENERATED_DEPLOY_KEY_PUB_PATH = WORK_DIR / "generated_deploy_key.pub"
EXPORT_BRANCH = "export"
EXPORT_EXCLUDES = [
    ".cloud/",
    ".cache/",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage/",
    ".vscode/",
    "__pycache__/",
    "backups/",
    "deps/",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    "*.pyc",
    "*.pyo",
    "node_modules/",
    "tts/",
    "www/community/",
    "www/media/",
    "www/tmp/",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]
STORAGE_EXPORT_ALLOWLIST = [
    "core.area_registry",
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.floor_registry",
    "core.label_registry",
    "core.logger",
    "core.uuid",
    "counter",
    "energy",
    "frontend_theme",
    "homeassistant.exposed_entities",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "lovelace",
    "lovelace.lovelace",
    "lovelace.map",
    "lovelace_dashboards",
    "lovelace_resources",
    "person",
    "schedule",
    "scene",
    "script",
    "tag",
    "timer",
    "zone",
]
EXPORT_CLEAN_PATHS = [
    ".cloud",
    ".cache",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage",
    ".vscode",
    "backups",
    "deps",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    "tts",
    "www/community",
    "www/media",
    "www/tmp",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]
EXPORT_CLEAN_DIR_NAMES = {"__pycache__", "node_modules"}
EXPORT_CLEAN_FILE_PATTERNS = ["*.pyc", "*.pyo"]

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


def addon_version():
    if not ADDON_CONFIG_PATH.exists():
        return "unknown"
    for line in ADDON_CONFIG_PATH.read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return "unknown"


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


def git_env(options):
    env = os.environ.copy()
    git_ssh_key = options.get("git_ssh_key", "").strip()
    if git_ssh_key:
        setup_git_ssh_env(env, key_text=git_ssh_key)
    elif generated_deploy_key_exists():
        setup_git_ssh_env(env, key_path=GENERATED_DEPLOY_KEY_PATH)
    return env


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


def ensure_repo(options, reset_to_origin=True):
    repo_dir = Path("/data") / options.get("repo_path", "ha-config")
    repo_url = options.get("repo_url", "").strip()
    if not repo_url:
        raise RuntimeError("repo_url is empty")

    env = git_env(options)

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
        if reset_to_origin:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")
        checkout = run_command(["git", "checkout", "-B", branch], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout -B {branch} failed:\n{checkout.stderr.strip()}")

    if not reset_to_origin:
        return repo_dir

    reset = run_command(["git", "reset", "--hard", f"origin/{branch}"], env=env, cwd=repo_dir)
    if reset.returncode != 0:
        raise RuntimeError(f"git reset to origin/{branch} failed:\n{reset.stderr.strip()}")

    return repo_dir


def git_commit(repo_dir, ref):
    result = run_command(["git", "rev-parse", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git rev-parse {ref} failed")
    return result.stdout.strip()


def git_ref_exists(repo_dir, ref):
    result = run_command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir)
    return result.returncode == 0


def git_current_branch(repo_dir):
    result = run_command(["git", "branch", "--show-current"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git branch failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def git_ahead_count(repo_dir, local_ref, remote_ref):
    if not git_ref_exists(repo_dir, remote_ref):
        return None

    result = run_command(["git", "rev-list", "--count", f"{remote_ref}..{local_ref}"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list failed:\n{result.stderr.strip()}")
    return int(result.stdout.strip() or "0")


def git_remote_head(repo_dir, env, branch):
    result = run_command(["git", "ls-remote", "--heads", "origin", branch], env=env, cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed:\n{result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        return None
    return output.split()[0]


def checkout_export_branch(repo_dir, env, base_branch):
    fetch = run_command(["git", "fetch", "origin"], env=env, cwd=repo_dir)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed:\n{fetch.stderr.strip()}")

    if git_ref_exists(repo_dir, f"refs/remotes/origin/{base_branch}"):
        checkout = run_command(["git", "checkout", "-B", EXPORT_BRANCH, f"origin/{base_branch}"], env=env, cwd=repo_dir)
    elif git_ref_exists(repo_dir, "HEAD"):
        checkout = run_command(["git", "checkout", "-B", EXPORT_BRANCH], env=env, cwd=repo_dir)
    else:
        checkout = run_command(["git", "checkout", "--orphan", EXPORT_BRANCH], env=env, cwd=repo_dir)

    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout {EXPORT_BRANCH} failed:\n{checkout.stderr.strip()}")


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


def ensure_manifest_file(manifest, manifest_path):
    if manifest_path.exists():
        return
    ensure_dir(manifest_path.parent)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


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


def resolve_targets(repo_dir, manifest, addons, require_source=True):
    options = load_options()
    targets = []
    for target in manifest.get("targets", []):
        target_type = target.get("type")
        source = repo_dir / target.get("source", "")
        optional = bool(target.get("optional", False))

        if require_source and not source.exists():
            if optional:
                continue
            raise RuntimeError(f"Source path does not exist for target '{target.get('id')}': {source}")

        resolved = dict(target)
        resolved["source_path"] = str(source)
        resolved["restart_after_sync"] = bool(
            target.get("restart_after_sync", options.get("restart_after_apply", True))
        )

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


def sync_tree(src, dest, delete=True, excludes=None):
    ensure_dir(dest)
    command = ["rsync", "-a"]
    if delete:
        command.append("--delete")
    for pattern in excludes or []:
        command.extend(["--exclude", pattern])
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Sync failed from {src} to {dest}:\n{result.stderr.strip()}")


def export_tree(src, dest, delete=True):
    ensure_dir(dest)
    command = ["rsync", "-a"]
    if delete:
        command.append("--delete")
    for pattern in EXPORT_EXCLUDES:
        command.extend(["--exclude", pattern])
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Export failed from {src} to {dest}:\n{result.stderr.strip()}")


def safe_remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def clean_export_destination(dest):
    ensure_dir(dest)
    removed = set()

    for pattern in EXPORT_CLEAN_PATHS:
        matches = list(dest.glob(pattern)) if any(char in pattern for char in "*?[") else [dest / pattern]
        for path in matches:
            if path.exists() or path.is_symlink():
                safe_remove_path(path)
                removed.add(str(path.relative_to(dest)))

    for path in list(dest.rglob("*")):
        relative = str(path.relative_to(dest))
        if path.is_dir() and path.name in EXPORT_CLEAN_DIR_NAMES:
            safe_remove_path(path)
            removed.add(relative)
            continue
        if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in EXPORT_CLEAN_FILE_PATTERNS):
            safe_remove_path(path)
            removed.add(relative)

    return len(removed)


def export_storage_allowlist(src, dest):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    for name in STORAGE_EXPORT_ALLOWLIST:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def sync_storage_allowlist(src, dest):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    for name in STORAGE_EXPORT_ALLOWLIST:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


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
        if target["type"] == "homeassistant" and source_has_storage(source_path):
            sync_tree(source_path, live_path, delete=bool(target.get("delete", True)), excludes=[".storage/"])
            copied_count = sync_storage_allowlist(source_path, live_path)
            add_detail(details, f"Synced {copied_count} allowlisted .storage config file(s).")
        else:
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


def export_targets(resolved_targets, details):
    for target in resolved_targets:
        live_path = Path(target["live_path"])
        source_path = Path(target["source_path"])
        if not live_path.exists():
            if target.get("optional", False):
                add_detail(details, f"Skipping optional target {target['id']} because {live_path} does not exist.")
                continue
            raise RuntimeError(f"Live path does not exist for target '{target['id']}': {live_path}")

        add_detail(details, f"Exporting {target['id']} from {live_path} to {source_path}.")
        removed_count = clean_export_destination(source_path)
        if removed_count:
            add_detail(details, f"Removed {removed_count} excluded item(s) from {target['id']} export destination.")
        export_tree(live_path, source_path, delete=bool(target.get("delete", True)))
        if target["type"] == "homeassistant":
            copied_count = export_storage_allowlist(live_path, source_path)
            if copied_count:
                add_detail(details, f"Exported {copied_count} allowlisted .storage config file(s).")


def git_status_porcelain(repo_dir):
    result = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def run_export_job():
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "export",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = load_options()
    resolved_targets = []

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "export",
            "last_message": "Preparing export.",
            "last_details": details,
        }
    )

    try:
        repo_dir = ensure_repo(options, reset_to_origin=False)
        env = git_env(options)
        checkout_export_branch(repo_dir, env, options.get("repo_branch", "main"))
        try:
            commit = git_commit(repo_dir, "HEAD")
        except RuntimeError:
            commit = "unborn"
        manifest, manifest_path = load_manifest(repo_dir, options)
        ensure_manifest_file(manifest, manifest_path)
        addons = get_installed_addons()
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Using repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Export excludes database, log, backup, deps, and tts files.")
        export_targets(resolved_targets, details)

        status = git_status_porcelain(repo_dir)
        if status:
            add_detail(details, f"Export created local Git changes on branch {EXPORT_BRANCH}. Use Push to commit and send them.")
        else:
            add_detail(details, f"Export finished with no Git changes on branch {EXPORT_BRANCH}.")

        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "export",
                "last_message": "Export finished successfully.",
                "last_details": details,
                "last_fetched_commit": commit,
                "last_targets": resolved_targets,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "export",
                "last_message": str(exc),
                "last_details": details,
                "last_targets": resolved_targets,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


def run_push_job():
    if not RUN_LOCK.acquire(blocking=False):
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "busy",
                "last_action": "push",
                "last_message": "Another HA Ops action is already running.",
            }
        )
        return False

    details = []
    options = load_options()

    write_state(
        {
            "last_run_at": utc_now(),
            "last_status": "running",
            "last_action": "push",
            "last_message": "Preparing push.",
            "last_details": details,
        }
    )

    try:
        repo_dir = Path("/data") / options.get("repo_path", "ha-config")
        if not repo_dir.exists():
            raise RuntimeError("Local checkout does not exist. Run Export or Pull And Apply first.")

        env = git_env(options)
        current_branch = git_current_branch(repo_dir)
        if current_branch != EXPORT_BRANCH:
            raise RuntimeError(f"Local checkout is on branch {current_branch or '(detached)'}. Run Export before Push.")
        status = git_status_porcelain(repo_dir)
        if status:
            add_detail(details, "Committing local exported changes.")
            add = run_command(["git", "add", "-A"], cwd=repo_dir)
            if add.returncode != 0:
                raise RuntimeError(f"git add failed:\n{add.stderr.strip()}")

            message = f"Export Home Assistant config {release_now()}"
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
            commit_summary = commit.stdout.strip().splitlines()[0] if commit.stdout.strip() else "Created export commit."
            add_detail(details, commit_summary)
        else:
            add_detail(details, "No local Git changes to commit.")

        ahead_count = git_ahead_count(repo_dir, EXPORT_BRANCH, f"refs/remotes/origin/{EXPORT_BRANCH}")
        if ahead_count == 0:
            add_detail(details, "No local export commits to push.")
            write_state(
                {
                    "last_run_at": utc_now(),
                    "last_status": "success",
                    "last_action": "push",
                    "last_message": "No local export commits to push.",
                    "last_details": details,
                }
            )
            return True
        if ahead_count is None:
            add_detail(details, f"Remote branch origin/{EXPORT_BRANCH} does not exist yet.")
        else:
            add_detail(details, f"Local branch {EXPORT_BRANCH} is ahead by {ahead_count} commit(s).")

        remote_export_head = git_remote_head(repo_dir, env, EXPORT_BRANCH)
        push_command = ["git", "push", "-u"]
        if remote_export_head:
            add_detail(details, f"Remote origin/{EXPORT_BRANCH} is at {remote_export_head}.")
            push_command.append(f"--force-with-lease=refs/heads/{EXPORT_BRANCH}:{remote_export_head}")
        else:
            add_detail(details, f"Remote branch origin/{EXPORT_BRANCH} does not exist yet.")
        push_command.extend(["origin", EXPORT_BRANCH])

        add_detail(details, f"Pushing to origin/{EXPORT_BRANCH}.")
        push = run_command(push_command, env=env, cwd=repo_dir)
        if push.returncode != 0:
            raise RuntimeError(f"git push failed:\n{push.stderr.strip() or push.stdout.strip()}")

        commit = git_commit(repo_dir, "HEAD")
        add_detail(details, f"Pushed commit {commit}.")
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "success",
                "last_action": "push",
                "last_message": "Push finished successfully.",
                "last_details": details,
                "last_fetched_commit": commit,
            }
        )
        return True
    except Exception as exc:
        details.append(str(exc))
        write_state(
            {
                "last_run_at": utc_now(),
                "last_status": "error",
                "last_action": "push",
                "last_message": str(exc),
                "last_details": details,
            }
        )
        return False
    finally:
        RUN_LOCK.release()


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


def start_export():
    thread = threading.Thread(target=run_export_job, daemon=True)
    thread.start()


def start_push():
    thread = threading.Thread(target=run_push_job, daemon=True)
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
            f"<form method='post' action='rollback' data-async-form='true'>"
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
        "<form method='post' action='generate-key' data-async-form='true'>"
        "<button type='submit' class='secondary'>Generate Deploy Key</button>"
        "</form>"
    )
    if mode == "generated":
        action = (
            "<form method='post' action='generate-key' data-async-form='true'>"
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
    action_disabled = "disabled" if status == "running" else ""
    version = html.escape(addon_version())

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HA Ops</title>
  <style>
    :root {{
      color-scheme: light dark;
      --ha-bg: var(--primary-background-color, #f6f8fb);
      --ha-card-bg: var(--card-background-color, #ffffff);
      --ha-text: var(--primary-text-color, #111827);
      --ha-muted: var(--secondary-text-color, #6b7280);
      --ha-border: var(--divider-color, rgba(0, 0, 0, 0.12));
      --ha-primary: var(--primary-color, #03a9f4);
      --ha-primary-contrast: var(--text-primary-color, #ffffff);
      --ha-error: var(--error-color, #db4437);
      --ha-success: var(--success-color, #43a047);
      --ha-info: var(--info-color, #039be5);
      --ha-radius: var(--ha-card-border-radius, 12px);
      --ha-shadow: var(--ha-card-box-shadow, none);
      --ha-font: var(--paper-font-common-base_-_font-family, system-ui, sans-serif);
      --ha-code-bg: var(--secondary-background-color, rgba(127, 127, 127, 0.08));
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: var(--ha-font);
      color: var(--ha-text);
      background: var(--ha-bg);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--ha-card-bg);
      border: 1px solid var(--ha-border);
      border-radius: var(--ha-radius);
      padding: 20px;
      box-shadow: var(--ha-shadow);
    }}
    h1, h2 {{
      margin: 0 0 14px;
      color: var(--ha-text);
    }}
    h1 {{
      font-size: 2rem;
    }}
    h2 {{
      font-size: 1.1rem;
    }}
    p, li {{
      color: var(--ha-muted);
      line-height: 1.55;
    }}
    dl {{
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 10px 14px;
      margin: 18px 0 0;
    }}
    dt {{
      color: var(--ha-muted);
    }}
    dd {{
      margin: 0;
      word-break: break-word;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 0.8rem;
      text-transform: uppercase;
      background: color-mix(in srgb, var(--ha-success) 14%, transparent);
      color: var(--ha-success);
      border: 1px solid color-mix(in srgb, var(--ha-success) 30%, transparent);
    }}
    .badge.error {{
      background: color-mix(in srgb, var(--ha-error) 14%, transparent);
      color: var(--ha-error);
      border-color: color-mix(in srgb, var(--ha-error) 30%, transparent);
    }}
    .badge.running {{
      background: color-mix(in srgb, var(--ha-info) 14%, transparent);
      color: var(--ha-info);
      border-color: color-mix(in srgb, var(--ha-info) 30%, transparent);
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 22px;
    }}
    button {{
      border: 1px solid color-mix(in srgb, var(--ha-primary) 35%, transparent);
      border-radius: 999px;
      background: var(--ha-primary);
      color: var(--ha-primary-contrast);
      font-size: 0.96rem;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }}
    button:disabled {{
      opacity: 0.6;
      cursor: default;
    }}
    button.secondary {{
      background: var(--ha-card-bg);
      color: var(--ha-text);
      border-color: var(--ha-border);
    }}
    pre {{
      margin: 0;
      background: var(--ha-code-bg);
      border: 1px solid var(--ha-border);
      border-radius: calc(var(--ha-radius) - 2px);
      padding: 14px;
      overflow: auto;
      white-space: pre-wrap;
      line-height: 1.45;
      color: var(--ha-text);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--ha-border);
      vertical-align: top;
    }}
    th {{
      color: var(--ha-muted);
      font-weight: 600;
    }}
    code {{
      font-family: ui-monospace, monospace;
      font-size: 0.92em;
      color: var(--ha-text);
    }}
    .wide {{
      margin-top: 18px;
    }}
    .client-status {{
      margin-top: 14px;
      min-height: 1.4em;
      color: var(--ha-muted);
    }}
    footer {{
      margin-top: 18px;
      color: var(--ha-muted);
      font-size: 0.86rem;
      text-align: center;
    }}
    @media (max-width: 640px) {{
      main {{
        padding: 12px;
      }}
      .card {{
        padding: 16px;
      }}
      dl {{
        grid-template-columns: 1fr;
      }}
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
        <p id="client-status" class="client-status"></p>
        <div class="actions">
          <form method="post" action="apply" data-async-form="true">
            <button type="submit" {action_disabled}>Pull And Apply</button>
          </form>
          <form method="post" action="export" data-async-form="true">
            <button type="submit" class="secondary" {action_disabled}>Export</button>
          </form>
          <form method="post" action="push" data-async-form="true">
            <button type="submit" class="secondary" {action_disabled}>Push</button>
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
    <footer>HA Ops {version}</footer>
  </main>
  <script>
    (() => {{
      const clientStatus = document.getElementById("client-status");

      function setClientStatus(message) {{
        if (clientStatus) {{
          clientStatus.textContent = message || "";
        }}
      }}

      async function submitAsyncForm(form) {{
        const button = form.querySelector("button[type='submit']");
        const originalText = button ? button.textContent : "";
        if (button) {{
          button.disabled = true;
          button.textContent = "Working...";
        }}
        setClientStatus("Working...");

        try {{
          const response = await fetch(form.getAttribute("action"), {{
            method: "POST",
            headers: {{
              "Accept": "application/json",
              "X-Requested-With": "fetch"
            }},
            body: new URLSearchParams(new FormData(form))
          }});

          let payload = {{}};
          try {{
            payload = await response.json();
          }} catch (_error) {{
            payload = {{}};
          }}

          if (!response.ok || payload.ok === false) {{
            setClientStatus(payload.message || "Request failed.");
            window.setTimeout(() => window.location.reload(), 600);
          }} else {{
            setClientStatus(payload.message || "Done. Refreshing...");
            window.setTimeout(() => window.location.reload(), 350);
          }}
        }} catch (error) {{
          setClientStatus(error?.message || "Network error.");
        }} finally {{
          if (button) {{
            button.disabled = false;
            button.textContent = originalText;
          }}
        }}
      }}

      for (const form of document.querySelectorAll("form[data-async-form='true']")) {{
        form.addEventListener("submit", (event) => {{
          event.preventDefault();
          submitAsyncForm(form);
        }});
      }}

      const badge = document.querySelector(".badge");
      if (badge && badge.textContent.trim().toLowerCase() === "running") {{
        window.setTimeout(() => window.location.reload(), 3000);
      }}
    }})();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_html(self, content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def wants_json(self):
        accept = self.headers.get("Accept", "")
        requested_with = self.headers.get("X-Requested-With", "")
        return "application/json" in accept or requested_with == "fetch"

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
                if self.wants_json():
                    self.send_json(
                        {
                            "ok": True,
                            "message": "Generated a new deploy key. Reloading UI.",
                            "public_key": public_key,
                        }
                    )
                    return
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
                if self.wants_json():
                    self.send_json({"ok": False, "message": str(exc)}, status=500)
                    return
            self.send_html(render_page())
            return

        if parsed.path == "/apply":
            start_apply()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Apply started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/export":
            start_export()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Export started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/push":
            start_push()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Push started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/rollback":
            release = body.get("release", [""])[0]
            if not release:
                if self.wants_json():
                    self.send_json({"ok": False, "message": "Missing release"}, status=400)
                else:
                    self.send_error(400, "Missing release")
                return
            start_rollback(release)
            if self.wants_json():
                self.send_json({"ok": True, "message": f"Rollback to {release} started. Refreshing..."})
            else:
                self.send_html(render_page())
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
