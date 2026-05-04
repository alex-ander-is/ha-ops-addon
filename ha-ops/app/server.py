from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import fnmatch
import hashlib
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
DATA_DIR = Path("/data")
CONFIG_DIR = Path("/homeassistant")
ADDON_CONFIGS_DIR = Path("/addon_configs")
WORK_DIR = Path("/data/work")
GENERATED_DEPLOY_KEY_PATH = WORK_DIR / "generated_deploy_key"
GENERATED_DEPLOY_KEY_PUB_PATH = WORK_DIR / "generated_deploy_key.pub"
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
    ".tmp-*",
    "node_modules",
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
PROTECTED_STORAGE_FILES = {
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.uuid",
    "person",
}
DEFAULT_BACKUP_MAX_AGE_HOURS = 24
DEFAULT_MAX_APPLY_DELETIONS = 25
DEFAULT_RELEASE_KEEP_COUNT = 5
DEFAULT_RELEASE_KEEP_DAYS = 7
HOMEASSISTANT_EXPORT_ROOT_PATTERNS = ["*.yaml", "*.yml"]
HOMEASSISTANT_EXPORT_ROOT_EXCLUDES = {"secrets.yaml"}
HOMEASSISTANT_EXPORT_DIRS = [
    "blueprints",
    "custom_templates",
    "dashboards",
    "packages",
    "templates",
    "themes",
    "ui_lovelace_minimalist",
]
ZIGBEE2MQTT_CONFIG_PATHS = [
    "zigbee2mqtt/configuration.yaml",
    "zigbee2mqtt/external_converters",
    "zigbee2mqtt/scripts",
]
HOMEASSISTANT_APPLY_EXCLUDES = EXPORT_EXCLUDES + [
    ".HA_VERSION",
    "custom_components/",
    "go2rtc-*",
    "image/",
    "secrets.yaml",
    "www/",
    "zha_quirks/",
    "zigbee2mqtt/",
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
    ".tmp-*",
    "node_modules",
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


def option_bool(options, name, default):
    value = options.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def option_int(options, name, default, minimum=0):
    try:
        value = int(options.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


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
        "last_release": None,
        "last_backup_slug": None,
        "last_targets": [],
        "last_diff": "",
        "last_diff_generated_at": None,
        "last_preview_commit": None,
        "last_preview_fingerprint": None,
        "last_preview_deletions": None,
        "managed_addons": [],
        "conflicts": [],
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
    comment = f"ha-ops@{socket.gethostname()}"
    temp_key_path = WORK_DIR / "generated_deploy_key.new"
    temp_pub_path = WORK_DIR / "generated_deploy_key.new.pub"
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

    temp_key_path.replace(GENERATED_DEPLOY_KEY_PATH)
    temp_pub_path.replace(GENERATED_DEPLOY_KEY_PUB_PATH)
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


def backup_mount_info():
    payload = call_supervisor("GET", "/mounts")
    return payload.get("data", payload)


def default_backup_mount():
    try:
        return backup_mount_info().get("default_backup_mount")
    except Exception:
        return None


def create_ha_backup(name_prefix, backup_location=None):
    payload = {"name": f"{name_prefix} {release_now()}"}
    payload["background"] = False
    if backup_location:
        payload["location"] = backup_location
    result = call_supervisor("POST", "/backups/new/full", payload)
    slug = result.get("data", {}).get("slug") or result.get("slug")
    if not slug:
        raise RuntimeError(f"Backup creation did not return a slug: {result}")
    return slug


def backup_manager_info():
    payload = call_supervisor("GET", "/backups/info")
    return payload.get("data", payload)


def parse_backup_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def backup_slug(backup):
    return backup.get("slug") or backup.get("id")


def backup_name(backup):
    return backup.get("name") or backup_slug(backup) or "unknown backup"


def backup_locations(backup):
    locations = backup.get("locations")
    if isinstance(locations, list):
        return len(locations)
    location = backup.get("location")
    if location:
        return 1
    return None


def backup_has_location(backup):
    locations = backup_locations(backup)
    return locations is not None and locations > 0


def is_system_backup(backup):
    backup_type = str(backup.get("type", "")).lower()
    return backup_type in {"full", "automatic", "auto"}


def backup_age_hours(backup_date):
    return max(0, int(backup_age_seconds(backup_date) // 3600))


def backup_age_seconds(backup_date):
    now = datetime.now(timezone.utc)
    return max(0, int((now - backup_date.astimezone(timezone.utc)).total_seconds()))


def backup_status_message(backup, backup_date):
    age_hours = backup_age_hours(backup_date)
    locations = backup_locations(backup)
    location_text = f", {locations} location(s)" if locations is not None else ""
    return f"{backup_name(backup)} at {backup.get('date')} ({age_hours} hour(s) ago{location_text})."


def find_backup_by_slug(backups, slug):
    for backup in backups:
        if backup_slug(backup) == slug:
            return backup
    return None


def latest_system_backup_status(options=None):
    options = options or load_options()
    max_age_hours = option_int(options, "backup_max_age_hours", DEFAULT_BACKUP_MAX_AGE_HOURS, minimum=1)
    require_location = option_bool(options, "backup_require_location", True)
    try:
        info = backup_manager_info()
        backups = info.get("backups", [])
        dated_backups = [
            (parse_backup_date(backup.get("date")), backup)
            for backup in backups
            if is_system_backup(backup) and (not require_location or backup_has_location(backup))
        ]
        dated_backups = [(date, backup) for date, backup in dated_backups if date is not None]
        if not dated_backups:
            return {
                "available": True,
                "message": "No system Home Assistant backups found.",
                "stale": True,
                "backup": None,
                "age_hours": None,
                "max_age_hours": max_age_hours,
                "require_location": require_location,
            }

        latest_date, latest = max(dated_backups, key=lambda item: item[0])
        age_hours = backup_age_hours(latest_date)
        stale = backup_age_seconds(latest_date) > max_age_hours * 3600
        return {
            "available": True,
            "message": backup_status_message(latest, latest_date),
            "stale": stale,
            "backup": latest,
            "age_hours": age_hours,
            "max_age_hours": max_age_hours,
            "require_location": require_location,
        }
    except Exception as exc:
        return {
            "available": False,
            "message": f"Backup status unavailable: {exc}",
            "stale": True,
            "backup": None,
            "age_hours": None,
            "max_age_hours": max_age_hours,
            "require_location": require_location,
        }


def ensure_fresh_system_backup(options, details):
    if not option_bool(options, "require_fresh_backup", True):
        add_detail(details, "Fresh system backup requirement is disabled.")
        return None

    status = latest_system_backup_status(options)
    if not status["stale"]:
        backup = status.get("backup") or {}
        add_detail(details, f"Fresh system backup found: {status['message']}")
        return backup_slug(backup)

    if not option_bool(options, "create_ha_backup", True):
        raise RuntimeError(
            f"No fresh system backup found within {status['max_age_hours']} hour(s): {status['message']}"
        )

    backup_location = default_backup_mount() if option_bool(options, "backup_require_location", True) else None
    if option_bool(options, "backup_require_location", True) and not backup_location:
        raise RuntimeError("No default backup location is configured. Configure Store in NAS or disable backup_require_location.")

    add_detail(details, f"No fresh system backup found within {status['max_age_hours']} hour(s). Creating full system backup.")
    slug = create_ha_backup(options.get("ha_backup_name_prefix", "ha-ops"), backup_location=backup_location)
    info = backup_manager_info()
    backup = find_backup_by_slug(info.get("backups", []), slug)
    if not backup:
        raise RuntimeError(f"Created backup {slug}, but it is not visible in Home Assistant backups.")

    backup_date = parse_backup_date(backup.get("date"))
    if not backup_date:
        raise RuntimeError(f"Created backup {slug}, but its date is unavailable.")
    if option_bool(options, "backup_require_location", True) and not backup_has_location(backup):
        raise RuntimeError(f"Created backup {slug}, but it is not stored in a configured backup location.")
    add_detail(details, f"Created fresh system backup: {backup_status_message(backup, backup_date)}")
    return slug


def repo_checkout_path(options):
    value = str(options.get("repo_path", "ha-config")).strip()
    path = Path(value)
    if not value or value == "." or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Invalid repo_path. Use a relative folder inside /data, for example ha-config.")

    repo_dir = (DATA_DIR / path).resolve()
    data_dir = DATA_DIR.resolve()
    if repo_dir == data_dir or data_dir not in repo_dir.parents:
        raise RuntimeError("Invalid repo_path. Use a relative folder inside /data, for example ha-config.")
    return repo_dir


def ensure_repo(options, reset_to_origin=True):
    repo_dir = repo_checkout_path(options)
    repo_url = options.get("repo_url", "").strip()
    if not repo_url:
        raise RuntimeError("repo_url is empty")

    env = git_env(options)

    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = run_command(["git", "clone", repo_url, str(repo_dir)], env=env)
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed:\n{clone.stderr.strip()}")

    clean_repo_untracked(repo_dir)

    fetch = run_command(["git", "fetch", "origin"], env=env, cwd=repo_dir)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed:\n{fetch.stderr.strip()}")

    branch = options.get("repo_branch", "main")
    remote_ref = f"refs/remotes/origin/{branch}"
    remote_exists = git_ref_exists(repo_dir, remote_ref)

    if not reset_to_origin and git_ref_exists(repo_dir, f"refs/heads/{branch}"):
        checkout = run_command(["git", "checkout", branch], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")
    elif remote_exists:
        checkout = run_command(["git", "checkout", "-B", branch, remote_ref], env=env, cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed:\n{checkout.stderr.strip()}")
    else:
        if git_ref_exists(repo_dir, "HEAD"):
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

    clean_repo_untracked(repo_dir)

    return repo_dir


def clean_repo_untracked(repo_dir):
    clean = run_command(["git", "clean", "-ffdx"], cwd=repo_dir)
    if clean.returncode != 0:
        raise RuntimeError(f"git clean failed:\n{clean.stderr.strip()}")


def git_commit(repo_dir, ref):
    result = run_command(["git", "rev-parse", ref], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git rev-parse {ref} failed")
    return result.stdout.strip()


def git_ref_exists(repo_dir, ref):
    result = run_command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir)
    return result.returncode == 0


def git_remote_head(repo_dir, env, branch):
    result = run_command(["git", "ls-remote", "--heads", "origin", branch], env=env, cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed:\n{result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        return None
    return output.split()[0]


def git_head_or_unborn(repo_dir):
    try:
        return git_commit(repo_dir, "HEAD")
    except RuntimeError:
        return "unborn"


def git_conflict_paths(repo_dir):
    result = run_command(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_pull_rebase(repo_dir, env, branch):
    remote_head = git_remote_head(repo_dir, env, branch)
    if not remote_head:
        return None
    pull = run_command(["git", "pull", "--rebase", "origin", branch], env=env, cwd=repo_dir)
    if pull.returncode != 0:
        conflicts = git_conflict_paths(repo_dir)
        if conflicts:
            write_state({"conflicts": conflicts})
        raise RuntimeError(f"git pull --rebase failed:\n{pull.stderr.strip() or pull.stdout.strip()}")
    return remote_head


def stage_all(repo_dir):
    add = run_command(["git", "add", "-A"], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed:\n{add.stderr.strip()}")


def commit_if_needed(repo_dir, message):
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
    return git_commit(repo_dir, "HEAD")


def push_branch(repo_dir, env, branch):
    push = run_command(["git", "push", "-u", "origin", branch], env=env, cwd=repo_dir)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed:\n{push.stderr.strip() or push.stdout.strip()}")


def selected_addon_slugs():
    state = read_state()
    return sorted(str(slug) for slug in state.get("managed_addons", []) if slug)


def set_selected_addon_slugs(slugs):
    cleaned = sorted(set(str(slug) for slug in slugs if slug))
    write_state({"managed_addons": cleaned})
    return cleaned


def default_homeassistant_manifest(options):
    return {
        "version": 1,
        "targets": [
            {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": options.get("apply_path", "homeassistant"),
                "delete": False,
                "allow_protected_storage": False,
                "stop_core_before_sync_if_storage": True,
                "restart_after_sync": options.get("restart_after_apply", True),
            }
        ],
    }


def default_addon_target(slug):
    return {
        "id": f"addon-{slug}",
        "type": "addon",
        "source": f"addons/{slug}",
        "addon_slug": slug,
        "delete": True,
        "restart_after_sync": True,
        "optional": True,
    }


def addon_target_slug(target, addons=None):
    exact = target.get("addon_slug")
    if exact:
        return exact
    if addons is None:
        return None
    try:
        return resolve_addon_slug(target, addons)
    except RuntimeError:
        return None


def selected_addon_target(slug, template=None):
    target = dict(template or default_addon_target(slug))
    target["type"] = "addon"
    target["addon_slug"] = slug
    target.pop("addon_slug_suffix", None)
    target.pop("addon_name_contains", None)
    target.setdefault("id", f"addon-{slug}")
    target.setdefault("source", f"addons/{slug}")
    target.setdefault("delete", True)
    target.setdefault("restart_after_sync", True)
    target.setdefault("optional", True)
    return target


def manifest_with_selected_addons(manifest, addons=None):
    selected = selected_addon_slugs()
    targets = []
    addon_templates = {}

    for target in manifest.get("targets", []):
        if target.get("type") != "addon":
            targets.append(target)
            continue
        slug = addon_target_slug(target, addons)
        if slug:
            addon_templates[slug] = target

    for slug in selected:
        targets.append(selected_addon_target(slug, addon_templates.get(slug)))

    effective = dict(manifest)
    effective["targets"] = targets
    return effective


def default_manifest(options):
    return manifest_with_selected_addons(default_homeassistant_manifest(options))


def load_manifest(repo_dir, options, addons=None):
    manifest_path = repo_dir / options.get("manifest_path", "ha-ops.json")
    if not manifest_path.exists():
        return manifest_with_selected_addons(default_homeassistant_manifest(options), addons), manifest_path

    return manifest_with_selected_addons(load_json(manifest_path, {}), addons), manifest_path


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


def addon_by_slug(addons, slug):
    for addon in addons:
        if addon.get("slug") == slug:
            return addon
    return {}


def path_from_metadata(value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute():
        return path
    return None


def addon_config_path_candidates(target, slug, addon):
    candidates = []
    for source in (target, addon):
        for key in ("live_path", "config_path", "configuration_path", "addon_config_path", "data_path"):
            path = path_from_metadata(source.get(key))
            if path:
                candidates.append(path)

    candidates.append(ADDON_CONFIGS_DIR / slug)

    if addon_is_zigbee2mqtt(addon or {"slug": slug}):
        candidates.append(CONFIG_DIR / "zigbee2mqtt")
        candidates.append(Path("/share/zigbee2mqtt"))

    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def resolve_addon_live_path(target, slug, addons):
    addon = addon_by_slug(addons, slug)
    candidates = addon_config_path_candidates(target, slug, addon)
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_targets(repo_dir, manifest, addons, require_source=True):
    options = load_options()
    targets = []
    for target in manifest.get("targets", []):
        target_id = str(target.get("id") or "")
        validate_target_id(target_id)
        target_type = target.get("type")
        source = repo_source_path(repo_dir, target.get("source", ""), target_id)
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
            resolved["live_path"] = str(resolve_addon_live_path(target, slug, addons))
        else:
            raise RuntimeError(f"Unsupported target type: {target_type}")

        targets.append(resolved)

    return targets


def validate_target_id(target_id):
    if not target_id or Path(target_id).name != target_id:
        raise RuntimeError(f"Invalid target id: {target_id}")


def repo_source_path(repo_dir, source, target_id):
    source_value = source or ""
    source_path = (repo_dir / source_value).resolve()
    repo_root = repo_dir.resolve()
    if source_path != repo_root and repo_root not in source_path.parents:
        raise RuntimeError(f"Source path escapes repository for target '{target_id}': {source_value}")
    return source_path


def has_managed_content(path):
    if path.is_file():
        return path.name != ".gitkeep"

    for child in path.rglob("*"):
        if child.is_file() and child.name != ".gitkeep":
            return True
        if child.is_symlink() and child.name != ".gitkeep":
            return True
    return False


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def sync_tree(src, dest, delete=True, excludes=None):
    ensure_dir(dest)
    command = ["rsync", "-a", "--checksum"]
    if delete:
        command.append("--delete")
    for pattern in excludes or []:
        command.append(f"--exclude={pattern}")
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Sync failed from {src} to {dest}:\n{result.stderr.strip()}")


def export_tree(src, dest, delete=True):
    ensure_dir(dest)
    command = ["rsync", "-a", "--checksum"]
    if delete:
        command.append("--delete")
    for pattern in EXPORT_EXCLUDES:
        command.append(f"--exclude={pattern}")
    command.extend([f"{src}/", f"{dest}/"])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Copy failed from {src} to {dest}:\n{result.stderr.strip()}")


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


def copy_homeassistant_path_allowlist(src, dest, paths):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name)
        copied += 1
    return copied


def copy_export_path(src, dest):
    ensure_dir(dest.parent)
    if src.is_dir():
        sync_tree(src, dest, delete=True, excludes=EXPORT_EXCLUDES)
    else:
        shutil.copy2(src, dest)


def export_homeassistant_config(src, dest, target=None):
    clear_tree(dest)
    copied = 0

    for pattern in HOMEASSISTANT_EXPORT_ROOT_PATTERNS:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in HOMEASSISTANT_EXPORT_ROOT_EXCLUDES:
                continue
            copy_export_path(src_path, dest / src_path.name)
            copied += 1

    for name in HOMEASSISTANT_EXPORT_DIRS:
        src_path = src / name
        if not src_path.exists():
            continue
        copy_export_path(src_path, dest / name)
        copied += 1

    zigbee2mqtt_count = 0
    if target and target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = copy_homeassistant_path_allowlist(src, dest, ZIGBEE2MQTT_CONFIG_PATHS)
    storage_count = export_storage_allowlist(src, dest)
    return copied, zigbee2mqtt_count, storage_count


def apply_homeassistant_config(src, dest, target, details=None):
    if not src.exists() or not has_managed_content(src):
        if details is not None:
            add_detail(details, f"Skipping {target['id']} because Git has no Home Assistant config yet.")
        return

    copied = 0
    for pattern in HOMEASSISTANT_EXPORT_ROOT_PATTERNS:
        for src_path in sorted(src.glob(pattern)):
            if not src_path.is_file() or src_path.name in HOMEASSISTANT_EXPORT_ROOT_EXCLUDES:
                continue
            dest_path = dest / src_path.name
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
            copied += 1

    for name in HOMEASSISTANT_EXPORT_DIRS:
        src_path = src / name
        if not src_path.exists():
            continue
        sync_homeassistant_path_allowlist(src, dest, [name])
        copied += 1

    zigbee2mqtt_count = 0
    if target.get("include_zigbee2mqtt_legacy"):
        zigbee2mqtt_count = sync_homeassistant_path_allowlist(src, dest, ZIGBEE2MQTT_CONFIG_PATHS)
    copied_count, skipped_protected = sync_storage_allowlist(
        src,
        dest,
        allow_protected=bool(target.get("allow_protected_storage", False)),
    )
    if copied:
        if details is not None:
            add_detail(details, f"Applied {copied} Home Assistant config path(s).")
    if zigbee2mqtt_count:
        if details is not None:
            add_detail(details, f"Applied {zigbee2mqtt_count} Zigbee2MQTT config path(s).")
    if copied_count:
        if details is not None:
            add_detail(details, f"Applied {copied_count} allowlisted .storage config file(s).")
    if skipped_protected:
        if details is not None:
            add_detail(details, f"Skipped protected .storage file(s): {', '.join(skipped_protected)}.")


def sync_homeassistant_path_allowlist(src, dest, paths):
    copied = 0
    for name in paths:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        if src_path.is_dir():
            sync_tree(src_path, dest_path, delete=True, excludes=EXPORT_EXCLUDES)
        else:
            ensure_dir(dest_path.parent)
            shutil.copy2(src_path, dest_path)
        copied += 1
    return copied


def sync_storage_allowlist(src, dest, allow_protected=False):
    src_storage = src / ".storage"
    if not src_storage.exists():
        return 0, []

    dest_storage = dest / ".storage"
    ensure_dir(dest_storage)
    copied = 0
    skipped_protected = []
    for name in STORAGE_EXPORT_ALLOWLIST:
        src_path = src_storage / name
        if not src_path.exists():
            continue
        if name in PROTECTED_STORAGE_FILES and not allow_protected:
            skipped_protected.append(name)
            continue
        dest_path = dest_storage / name
        ensure_dir(dest_path.parent)
        shutil.copy2(src_path, dest_path)
        copied += 1
    return copied, skipped_protected


def clear_tree(dest):
    ensure_dir(dest)
    empty_dir = WORK_DIR / "empty"
    ensure_dir(empty_dir)
    sync_tree(empty_dir, dest, delete=True)


def safe_release_dir(release_name):
    if not release_name or Path(release_name).name != release_name:
        raise RuntimeError("Invalid release name")
    release_dir = (RELEASES_DIR / release_name).resolve()
    releases_root = RELEASES_DIR.resolve()
    if release_dir.parent != releases_root:
        raise RuntimeError("Invalid release name")
    return release_dir


def source_has_applicable_storage(path, allow_protected=False):
    storage = path / ".storage"
    if not storage.exists():
        return False
    for name in STORAGE_EXPORT_ALLOWLIST:
        if name in PROTECTED_STORAGE_FILES and not allow_protected:
            continue
        if (storage / name).exists():
            return True
    return False


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


def release_created_at(path):
    metadata = load_json(path / "release.json", {})
    created_at = parse_backup_date(metadata.get("created_at"))
    if created_at:
        return created_at
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def prune_release_snapshots(options, protected_release=None):
    if not RELEASES_DIR.exists():
        return []

    keep_count = option_int(options, "release_snapshot_keep_count", DEFAULT_RELEASE_KEEP_COUNT, minimum=0)
    keep_days = option_int(options, "release_snapshot_keep_days", DEFAULT_RELEASE_KEEP_DAYS, minimum=0)
    now = datetime.now(timezone.utc)
    releases = []
    for path in RELEASES_DIR.iterdir():
        if not path.is_dir() or path.name == protected_release:
            continue
        releases.append((path, release_created_at(path)))

    to_delete = set()
    if keep_days:
        for path, created_at in releases:
            age_days = (now - created_at.astimezone(timezone.utc)).total_seconds() / 86400
            if age_days > keep_days:
                to_delete.add(path)

    remaining = sorted(
        [(path, created_at) for path, created_at in releases if path not in to_delete],
        key=lambda item: item[1],
        reverse=True,
    )
    protected_slots = 1 if protected_release else 0
    remaining_keep_count = max(0, keep_count - protected_slots)
    if len(remaining) > remaining_keep_count:
        for path, _created_at in remaining[remaining_keep_count:]:
            to_delete.add(path)

    removed = []
    for path in sorted(to_delete, key=lambda item: item.name):
        safe_dir = safe_release_dir(path.name)
        safe_remove_path(safe_dir)
        removed.append(path.name)
    return removed


def restore_release_snapshot(release_name, details):
    release_dir = safe_release_dir(release_name)
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
            allow_protected_storage = bool(target.get("allow_protected_storage", False))
            if target.get("stop_core_before_sync_if_storage", False) and source_has_applicable_storage(source_path, allow_protected_storage) and not core_stopped:
                add_detail(details, "Stopping Home Assistant Core before syncing .storage.")
                core_stop()
                core_stopped = True
        elif target["type"] == "addon" and target.get("stop_addon_before_sync", False):
            slug = target["resolved_slug"]
            add_detail(details, f"Stopping add-on {slug} before sync.")
            addon_was_started = stop_addon_for_sync(slug)

        add_detail(details, f"Syncing {target['id']} from {source_path} to {live_path}.")
        if target["type"] == "homeassistant":
            apply_homeassistant_config(source_path, live_path, target, details)
        else:
            if not source_path.exists() or not has_managed_content(source_path):
                add_detail(details, f"Skipping {target['id']} because Git has no config for this add-on yet.")
                continue
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

    add_detail(details, "Running Home Assistant config check.")
    do_core_check()

    if core_stopped:
        if homeassistant_target.get("restart_after_sync", True):
            add_detail(details, "Starting Home Assistant Core after sync.")
            core_start()
    else:
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

        if target["type"] == "homeassistant":
            add_detail(details, f"Saving config-only {target['id']} from {live_path} to {source_path}.")
            copied_count, zigbee2mqtt_count, storage_count = export_homeassistant_config(live_path, source_path, target)
            add_detail(details, f"Saved {copied_count} Home Assistant config path(s).")
            if zigbee2mqtt_count:
                add_detail(details, f"Saved {zigbee2mqtt_count} legacy Zigbee2MQTT config path(s).")
            if storage_count:
                add_detail(details, f"Saved {storage_count} allowlisted .storage config file(s).")
        else:
            add_detail(details, f"Saving {target['id']} from {live_path} to {source_path}.")
            removed_count = clean_export_destination(source_path)
            if removed_count:
                add_detail(details, f"Removed {removed_count} excluded item(s) from {target['id']} save destination.")
            export_tree(live_path, source_path, delete=bool(target.get("delete", True)))


def sync_to_preview(target, preview_path):
    source_path = Path(target["source_path"])
    live_path = Path(target["live_path"])
    clear_tree(preview_path)
    if live_path.exists():
        sync_tree(live_path, preview_path, delete=True)

    if target["type"] == "homeassistant":
        if source_path.exists() and has_managed_content(source_path):
            apply_homeassistant_config(source_path, preview_path, target)
            _copied, skipped_protected = sync_storage_allowlist(
                source_path,
                preview_path,
                allow_protected=bool(target.get("allow_protected_storage", False)),
            )
        else:
            skipped_protected = []
    else:
        if source_path.exists() and has_managed_content(source_path):
            sync_tree(source_path, preview_path, delete=bool(target.get("delete", True)))
        skipped_protected = []
    return skipped_protected


def target_diff(target, preview_path):
    live_path = Path(target["live_path"])
    if not live_path.exists():
        return f"Target {target['id']} live path does not exist: {live_path}\n"

    result = run_command(["diff", "-ruN", str(live_path), str(preview_path)])
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Diff failed for {target['id']}:\n{result.stderr.strip()}")
    if not result.stdout.strip():
        return f"Target {target['id']}: no file changes.\n"
    return f"## {target['id']}\n{result.stdout.strip()}\n"


def count_preview_deletions(target, preview_path):
    live_path = Path(target["live_path"])
    if not live_path.exists():
        return 0

    deleted = 0
    for path in live_path.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(live_path)
        if not (preview_path / relative).exists():
            deleted += 1
    return deleted


def safe_preview_name(value):
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value) or "target"


def fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_diff(diff_text):
    max_chars = 60000
    if len(diff_text) > max_chars:
        return diff_text[:max_chars] + "\n\n[Diff truncated. Use git or shell for full output.]"
    return diff_text


def build_apply_preview(resolved_targets):
    preview_root = WORK_DIR / "apply-preview"
    clear_tree(preview_root)
    chunks = []
    deletion_count = 0
    skipped_protected = []

    for target in resolved_targets:
        preview_path = preview_root / safe_preview_name(str(target["id"]))
        skipped = sync_to_preview(target, preview_path)
        if skipped:
            skipped_protected.extend(skipped)
            chunks.append(f"Target {target['id']}: skipped protected .storage file(s): {', '.join(skipped)}.\n")
        deletion_count += count_preview_deletions(target, preview_path)
        chunks.append(target_diff(target, preview_path))

    diff_text = "\n".join(chunks).strip()
    if not diff_text:
        diff_text = "No file changes."

    return {
        "diff": truncate_diff(diff_text),
        "fingerprint": fingerprint_text(diff_text),
        "deletions": deletion_count,
        "skipped_protected": sorted(set(skipped_protected)),
    }


def build_apply_diff(resolved_targets):
    return build_apply_preview(resolved_targets)["diff"]


def ensure_preview_matches_state(state, commit, preview):
    if state.get("last_preview_commit") != commit:
        raise RuntimeError("Run Preview Git to HA before Apply Git to HA. The preview commit does not match.")
    if state.get("last_preview_fingerprint") != preview["fingerprint"]:
        raise RuntimeError("Run Preview Git to HA again. The live diff changed since the last preview.")


def enforce_apply_limits(options, preview):
    max_deletions = option_int(options, "max_apply_deletions", DEFAULT_MAX_APPLY_DELETIONS, minimum=0)
    if preview["deletions"] > max_deletions:
        raise RuntimeError(
            f"Apply would delete {preview['deletions']} file(s), above the limit of {max_deletions}. Review the preview or raise max_apply_deletions."
        )


def git_status_porcelain(repo_dir):
    result = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def stage_homeassistant_storage_allowlist(repo_dir, options, details):
    manifest, _manifest_path = load_manifest(repo_dir, options)
    paths = []

    for target in manifest.get("targets", []):
        if target.get("type") != "homeassistant":
            continue

        source = repo_dir / target.get("source", options.get("apply_path", "homeassistant"))
        storage = source / ".storage"
        if not storage.exists():
            continue

        for name in STORAGE_EXPORT_ALLOWLIST:
            path = storage / name
            if path.exists():
                paths.append(str(path.relative_to(repo_dir)))

    if not paths:
        return 0

    add = run_command(["git", "add", "-f", "--"] + paths, cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add allowlisted .storage failed:\n{add.stderr.strip()}")

    add_detail(details, f"Staged {len(paths)} allowlisted .storage config file(s).")
    return len(paths)


def run_save_job():
    if not RUN_LOCK.acquire(blocking=False):
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
    options = load_options()
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
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Save HA to Git.")

        repo_dir = ensure_repo(options, reset_to_origin=False)
        env = git_env(options)
        branch = options.get("repo_branch", "main")
        git_pull_rebase(repo_dir, env, branch)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Using branch {branch} at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Saving live Home Assistant config to Git.")
        export_targets(resolved_targets, details)
        stage_homeassistant_storage_allowlist(repo_dir, options, details)
        stage_all(repo_dir)

        new_commit = commit_if_needed(repo_dir, f"Save Home Assistant config {release_now()}")
        if new_commit:
            add_detail(details, f"Created commit {new_commit}.")
            try:
                push_branch(repo_dir, env, branch)
            except RuntimeError:
                git_pull_rebase(repo_dir, env, branch)
                push_branch(repo_dir, env, branch)
            add_detail(details, f"Pushed to origin/{branch}.")
        else:
            add_detail(details, "No live Home Assistant changes to save.")

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
            repo_path = repo_checkout_path(options)
        except RuntimeError:
            repo_path = None
        conflicts = git_conflict_paths(repo_path) if repo_path and repo_path.exists() else []
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
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Apply Git to HA.")

        repo_dir = ensure_repo(options)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Fetched repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Rebuilding apply preview for safety checks.")
        preview = build_apply_preview(resolved_targets)
        ensure_preview_matches_state(state, commit, preview)
        enforce_apply_limits(options, preview)

        backup_slug = ensure_fresh_system_backup(options, details)

        if option_bool(options, "create_release_snapshot", True):
            add_detail(details, "Creating local release snapshot.")
            release_name = create_release_snapshot(resolved_targets, commit, backup_slug)
            add_detail(details, f"Created local release snapshot {release_name}.")

        apply_targets(resolved_targets, details)
        pruned = prune_release_snapshots(options, protected_release=release_name)
        if pruned:
            add_detail(details, f"Pruned {len(pruned)} old local release snapshot(s): {', '.join(pruned)}.")

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


def run_preview_job():
    if not RUN_LOCK.acquire(blocking=False):
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
    options = load_options()
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
        state = read_state()
        if state.get("conflicts"):
            raise RuntimeError("Resolve Git conflicts before running Preview Git to HA.")

        repo_dir = ensure_repo(options)
        commit = git_head_or_unborn(repo_dir)
        addons = get_installed_addons()
        manifest, manifest_path = load_manifest(repo_dir, options, addons)
        resolved_targets = resolve_targets(repo_dir, manifest, addons, require_source=False)

        add_detail(details, f"Fetched repository at commit {commit}.")
        add_detail(details, f"Using manifest {manifest_path}.")
        add_detail(details, "Building apply preview without changing live config.")
        preview = build_apply_preview(resolved_targets)

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


def start_preview():
    thread = threading.Thread(target=run_preview_job, daemon=True)
    thread.start()


def start_save():
    thread = threading.Thread(target=run_save_job, daemon=True)
    thread.start()


def start_rollback(release_name):
    thread = threading.Thread(target=run_rollback_job, args=(release_name,), daemon=True)
    thread.start()


def safe_repo_relative_path(value):
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Invalid conflict path")
    return str(path)


def resolve_git_conflict(path, choice):
    options = load_options()
    repo_dir = repo_checkout_path(options)
    branch = options.get("repo_branch", "main")
    safe_path = safe_repo_relative_path(path)
    if choice == "ha":
        checkout = run_command(["git", "checkout", "--theirs", "--", safe_path], cwd=repo_dir)
    elif choice == "git":
        checkout = run_command(["git", "checkout", "--ours", "--", safe_path], cwd=repo_dir)
    else:
        raise RuntimeError("Invalid conflict choice")
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout conflict version failed:\n{checkout.stderr.strip()}")

    add = run_command(["git", "add", "--", safe_path], cwd=repo_dir)
    if add.returncode != 0:
        raise RuntimeError(f"git add conflict resolution failed:\n{add.stderr.strip()}")

    conflicts = git_conflict_paths(repo_dir)
    if conflicts:
        write_state({"conflicts": conflicts})
        return f"Resolved {safe_path}. {len(conflicts)} conflict(s) remain."

    env = git_env(options)
    env["GIT_EDITOR"] = "true"
    cont = run_command(["git", "rebase", "--continue"], env=env, cwd=repo_dir)
    if cont.returncode != 0:
        output = cont.stderr.strip() or cont.stdout.strip()
        if "No changes" in output or "previous cherry-pick is now empty" in output:
            skip = run_command(["git", "rebase", "--skip"], env=env, cwd=repo_dir)
            if skip.returncode != 0:
                raise RuntimeError(f"git rebase --skip failed:\n{skip.stderr.strip() or skip.stdout.strip()}")
        else:
            raise RuntimeError(f"git rebase --continue failed:\n{output}")
    push_branch(repo_dir, env, branch)
    write_state({"conflicts": [], "last_status": "success", "last_message": "Conflicts resolved and pushed."})
    return "All conflicts resolved and pushed."


def current_manifest_preview():
    options = load_options()
    try:
        repo_dir = repo_checkout_path(options)
        try:
            addons = get_installed_addons()
        except Exception:
            addons = None
        if repo_dir.exists():
            manifest, _ = load_manifest(repo_dir, options, addons)
        else:
            manifest = default_manifest(options)
        previews = []
        for target in manifest.get("targets", []):
            previews.append(
                {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "source": target.get("source"),
                    "addon_slug": target.get("addon_slug"),
                    "addon_slug_suffix": target.get("addon_slug_suffix"),
                    "allow_protected_storage": target.get("allow_protected_storage", False),
                }
            )
        return previews
    except Exception:
        return []


def addon_slug_value(addon):
    return addon.get("slug") or addon.get("name") or ""


def addon_display_name(addon):
    name = addon.get("name") or addon_slug_value(addon)
    slug = addon_slug_value(addon)
    return f"{name} ({slug})" if slug and slug not in name else name


def addon_is_zigbee2mqtt(addon):
    text = f"{addon.get('slug', '')} {addon.get('name', '')} {addon.get('description', '')}".lower()
    return "zigbee2mqtt" in text or "zigbee2mqtt" in text.replace(" ", "")


def render_addons():
    selected = set(selected_addon_slugs())
    try:
        addons = sorted(get_installed_addons(), key=lambda addon: addon_display_name(addon).lower())
    except Exception as exc:
        return f"<p>Add-on discovery unavailable: {html.escape(str(exc))}</p>"

    if not addons:
        return "<p>No installed add-ons found.</p>"

    rows = []
    for addon in addons:
        slug = addon_slug_value(addon)
        if not slug:
            continue
        checked = "checked" if slug in selected else ""
        name = html.escape(addon_display_name(addon))
        hint = "Zigbee2MQTT candidate" if addon_is_zigbee2mqtt(addon) else ""
        rows.append(
            "<label class='check-row'>"
            f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}>"
            f"<span>{name}</span>"
            f"<small>{html.escape(hint)}</small>"
            "</label>"
        )

    return (
        "<form method='post' action='addons' data-async-form='true'>"
        "<div class='check-list'>"
        f"{''.join(rows)}"
        "</div>"
        "<div class='actions'><button type='submit' class='secondary'>Save Add-on Selection</button></div>"
        "</form>"
    )


def render_conflicts(conflicts):
    if not conflicts:
        return "<p>No unresolved Git conflicts.</p>"
    rows = []
    for path in conflicts:
        escaped = html.escape(path)
        rows.append(
            "<tr>"
            f"<td><code>{escaped}</code></td>"
            "<td class='actions'>"
            "<form method='post' action='resolve-conflict' data-async-form='true'>"
            f"<input type='hidden' name='path' value='{html.escape(path, quote=True)}'>"
            "<input type='hidden' name='choice' value='ha'>"
            "<button type='submit' class='secondary'>Use HA Version</button>"
            "</form>"
            "<form method='post' action='resolve-conflict' data-async-form='true'>"
            f"<input type='hidden' name='path' value='{html.escape(path, quote=True)}'>"
            "<input type='hidden' name='choice' value='git'>"
            "<button type='submit' class='secondary'>Use Git Version</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>File</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


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
        protected_storage = "yes" if item.get("allow_protected_storage") else "no"
        rows.append(
            "<tr>"
            f"<td><code>{target}</code></td>"
            f"<td>{target_type}</td>"
            f"<td><code>{source}</code></td>"
            f"<td><code>{addon}</code></td>"
            f"<td><code>{live_path}</code></td>"
            f"<td>{protected_storage}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Target</th><th>Type</th><th>Source</th><th>Add-on</th><th>Live Path</th><th>Protected Storage</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_releases(releases):
    if not releases:
        return "<p>No local release snapshots yet.</p>"

    rows = []
    for release in releases[:12]:
        name = html.escape(release["name"])
        created_at = html.escape(str(release.get("created_at")))
        backup_slug = html.escape(str(release.get("backup_slug")))
        rows.append(
            "<tr>"
            f"<td><code>{name}</code></td>"
            f"<td>{created_at}</td>"
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
        "<table><thead><tr><th>Release</th><th>Created</th><th>HA Backup</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def targets_allow_protected_storage(items):
    return any(bool(item.get("allow_protected_storage")) for item in items or [])


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
            "<p>Add this public key to GitHub as a Deploy Key with write access for <code>ha-config</code>.</p>"
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
    backup_status = latest_system_backup_status(options)
    releases = list_releases()
    manifest_preview = current_manifest_preview()
    target_state = state.get("last_targets") or manifest_preview
    last_status = state.get("last_status", "idle")
    status = html.escape(last_status)
    message = html.escape(state.get("last_message", ""))
    last_run = html.escape(str(state.get("last_run_at")))
    last_release = html.escape(str(state.get("last_release")))
    last_backup_slug = html.escape(str(state.get("last_backup_slug")))
    latest_backup = html.escape(backup_status.get("message", "Backup status unavailable."))
    repo_url = html.escape(options.get("repo_url", ""))
    branch = html.escape(options.get("repo_branch", "main"))
    manifest_path = html.escape(options.get("manifest_path", "ha-ops.json"))
    auth_mode = html.escape(git_auth_mode(options))
    details = "\n".join(state.get("last_details", []))
    conflicts = state.get("conflicts", [])
    details_placeholder = "Running..." if last_status == "running" else "No details yet."
    details_html = html.escape(details or details_placeholder)
    diff_text = state.get("last_diff", "")
    diff_generated_at = html.escape(str(state.get("last_diff_generated_at")))
    diff_html = html.escape(diff_text or "No apply preview yet.")
    preview_deletions = html.escape(str(state.get("last_preview_deletions")))
    action_disabled = "disabled" if last_status == "running" else ""
    confirm_messages = []
    if not option_bool(options, "require_fresh_backup", True):
        confirm_messages.append("Fresh system backup checks are disabled.")
    if targets_allow_protected_storage(target_state):
        confirm_messages.append("Protected .storage apply is enabled for at least one target.")
    apply_confirm = ""
    if confirm_messages:
        confirm_message = " ".join(confirm_messages) + " Continue?"
        apply_confirm = f"data-confirm='{html.escape(confirm_message, quote=True)}'"
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
    .check-list {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .check-row {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 8px 12px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--ha-border);
    }}
    .check-row small {{
      grid-column: 2;
      color: var(--ha-muted);
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
        <div class="badge {'error' if last_status == 'error' else 'running' if last_status == 'running' else ''}">{status}</div>
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
          <dt>Release snapshot</dt>
          <dd><code>{last_release}</code></dd>
          <dt>HA backup</dt>
          <dd><code>{last_backup_slug}</code></dd>
          <dt>Latest system backup</dt>
          <dd>{latest_backup}</dd>
          <dt>Preview deletions</dt>
          <dd><code>{preview_deletions}</code></dd>
        </dl>
        <p>{message}</p>
        <p id="client-status" class="client-status"></p>
        <div class="actions">
          <form method="post" action="save" data-async-form="true">
            <button type="submit" {action_disabled}>Save HA to Git</button>
          </form>
          <form method="post" action="preview" data-async-form="true">
            <button type="submit" class="secondary" {action_disabled}>Preview Git to HA</button>
          </form>
          <form method="post" action="apply" data-async-form="true" {apply_confirm}>
            <button type="submit" class="secondary" {action_disabled}>Apply Git to HA</button>
          </form>
        </div>
      </section>
      <section class="card">
        <h2>Last Run Details</h2>
        <pre>{details_html}</pre>
      </section>
    </div>

    <section class="card wide">
      <h2>Apply Preview</h2>
      <p>Generated at {diff_generated_at}</p>
      <pre>{diff_html}</pre>
    </section>

    <section class="card wide">
      <h2>Git Conflicts</h2>
      {render_conflicts(conflicts)}
    </section>

    <section class="card wide">
      <h2>Git Access</h2>
      {render_git_auth(options)}
    </section>

    <section class="card wide">
      <h2>Managed Targets</h2>
      {render_targets(target_state)}
    </section>

    <section class="card wide">
      <h2>Managed Add-ons</h2>
      {render_addons()}
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
        const confirmation = form.getAttribute("data-confirm");
        if (confirmation && !window.confirm(confirmation)) {{
          return;
        }}
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
                self.send_json({"ok": True, "message": "Apply Git to HA started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/preview":
            start_preview()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Git to HA preview started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/save":
            start_save()
            if self.wants_json():
                self.send_json({"ok": True, "message": "Save HA to Git started. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/addons":
            selected = body.get("addon", [])
            set_selected_addon_slugs(selected)
            if self.wants_json():
                self.send_json({"ok": True, "message": "Managed add-on selection saved. Refreshing..."})
            else:
                self.send_html(render_page())
            return

        if parsed.path == "/resolve-conflict":
            try:
                path = body.get("path", [""])[0]
                choice = body.get("choice", [""])[0]
                message = resolve_git_conflict(path, choice)
                if self.wants_json():
                    self.send_json({"ok": True, "message": f"{message} Refreshing..."})
                else:
                    self.send_html(render_page())
                return
            except Exception as exc:
                write_state(
                    {
                        "last_run_at": utc_now(),
                        "last_status": "error",
                        "last_action": "resolve_conflict",
                        "last_message": str(exc),
                        "last_details": [str(exc)],
                    }
                )
                if self.wants_json():
                    self.send_json({"ok": False, "message": str(exc)}, status=500)
                else:
                    self.send_html(render_page(), status=500)
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
