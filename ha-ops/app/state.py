import json
import os
import threading
from datetime import datetime, timezone

import policies


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
    return policies.option_bool(options, name, default)


def option_int(options, name, default, minimum=0):
    return policies.option_int(options, name, default, minimum)


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
        "conflict_type": None,
        "save_conflict_resolutions": {},
    }


def read_state(path):
    return load_json(path, default_state())


def write_state(path, updates):
    with STATE_LOCK:
        current = read_state(path)
        current.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text(json.dumps(current, indent=2, sort_keys=True))
        os.replace(temp_path, path)
        return current


def repair_startup_state(path, now):
    current = read_state(path)
    if current.get("last_status") != "running":
        return write_state(path, current)

    details = list(current.get("last_details") or [])
    details.append("HA Ops restarted while an action was running. The action was interrupted.")
    current.update(
        {
            "last_run_at": now,
            "last_status": "interrupted",
            "last_message": "Previous action was interrupted by HA Ops restart.",
            "last_details": details,
        }
    )
    return write_state(path, current)
