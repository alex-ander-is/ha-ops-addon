import json
import threading
from datetime import datetime, timezone


STATE_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def release_now():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def load_options(path):
    return load_json(path, {})


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


def read_state(path):
    return load_json(path, default_state())


def write_state(path, updates):
    with STATE_LOCK:
        current = read_state(path)
        current.update(updates)
        path.write_text(json.dumps(current, indent=2, sort_keys=True))
        return current
