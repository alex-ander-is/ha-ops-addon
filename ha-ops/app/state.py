import json
import os
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import policies


STATE_LOCK = threading.Lock()

APPLY_PREVIEW_CLEAR_UPDATES = {
    "last_diff": "",
    "last_diff_generated_at": None,
    "last_preview_commit": None,
    "last_preview_fingerprint": None,
    "last_preview_deletions": None,
    "last_preview_storage_changes": False,
    "last_preview_storage_paths": [],
    "last_preview_live_fingerprints": {},
    "last_preview_warnings": [],
    "last_preview_paths": [],
    "last_preview_conflicts": False,
    "apply_preview_resolutions": {},
}
SAVE_PREVIEW_CLEAR_UPDATES = {
    "last_save_preview": "",
    "last_save_diff": "",
    "last_save_diff_generated_at": None,
    "last_save_preview_commit": None,
    "last_save_preview_fingerprint": None,
    "last_save_preview_paths": [],
    "last_save_preview_conflicts": False,
    "save_preview_resolutions": {},
}
DELETED_DEVICES_PREVIEW_CLEAR_UPDATES = {
    "last_deleted_devices_preview": "",
    "last_deleted_devices_rows": [],
    "last_deleted_devices_count": 0,
    "last_deleted_devices_fingerprint": None,
    "last_deleted_devices_generated_at": None,
}
RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES = {
    "last_retained_devices_preview": "",
    "last_retained_devices_rows": [],
    "last_retained_devices_count": 0,
    "last_retained_devices_fingerprint": None,
    "last_retained_devices_generated_at": None,
}
INTERNAL_IDS_PREVIEW_CLEAR_UPDATES = {
    "last_internal_ids_preview": "",
    "last_internal_ids_rows": [],
    "last_internal_ids_count": 0,
    "last_internal_ids_fingerprint": None,
    "last_internal_ids_generated_at": None,
    "last_internal_ids_unresolved": [],
}
ALL_PREVIEW_CLEAR_UPDATES = {
    **APPLY_PREVIEW_CLEAR_UPDATES,
    **SAVE_PREVIEW_CLEAR_UPDATES,
    **DELETED_DEVICES_PREVIEW_CLEAR_UPDATES,
    **RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES,
    **INTERNAL_IDS_PREVIEW_CLEAR_UPDATES,
}
DISPLAY_CLEAR_UPDATES = {
    "last_details": [],
    "last_diff": "",
    "last_diff_generated_at": None,
    "last_preview_warnings": [],
    **SAVE_PREVIEW_CLEAR_UPDATES,
    "conflicts": [],
    "conflict_type": None,
    "save_conflict_resolutions": {},
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def release_now():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def format_time(value, time_zone_name=None):
    if value in (None, ""):
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if time_zone_name:
        try:
            parsed = parsed.astimezone(ZoneInfo(time_zone_name))
        except ZoneInfoNotFoundError:
            parsed = parsed.astimezone()
    else:
        parsed = parsed.astimezone()
    return parsed.replace(microsecond=0).isoformat()


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
        "last_seen_addon_version": None,
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
        "last_preview_storage_changes": False,
        "last_preview_storage_paths": [],
        "last_preview_live_fingerprints": {},
        "last_preview_warnings": [],
        "last_preview_paths": [],
        "last_preview_conflicts": False,
        "apply_preview_resolutions": {},
        "last_save_preview_commit": None,
        "last_save_preview_fingerprint": None,
        "last_save_preview_paths": [],
        "last_save_preview_conflicts": False,
        "save_preview_resolutions": {},
        "last_deleted_devices_preview": "",
        "last_deleted_devices_rows": [],
        "last_deleted_devices_count": 0,
        "last_deleted_devices_fingerprint": None,
        "last_deleted_devices_generated_at": None,
        "last_retained_devices_preview": "",
        "last_retained_devices_rows": [],
        "last_retained_devices_count": 0,
        "last_retained_devices_fingerprint": None,
        "last_retained_devices_generated_at": None,
        "last_internal_ids_preview": "",
        "last_internal_ids_rows": [],
        "last_internal_ids_count": 0,
        "last_internal_ids_fingerprint": None,
        "last_internal_ids_generated_at": None,
        "last_internal_ids_unresolved": [],
        "deleted_devices_pending_confirmation": False,
        "deleted_devices_rollback_path": None,
        "deleted_devices_rollback_fingerprint": None,
        "deleted_devices_applied_fingerprint": None,
        "managed_addons": [],
        "homeassistant_organizer_enabled": None,
        "include_redundant_data": False,
        "post_apply_save_recommended": False,
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


def clear_display_state(path):
    updates = dict(DISPLAY_CLEAR_UPDATES)
    current = read_state(path)
    if not current.get("deleted_devices_pending_confirmation"):
        updates.update(DELETED_DEVICES_PREVIEW_CLEAR_UPDATES)
    updates.update(RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
    updates.update(INTERNAL_IDS_PREVIEW_CLEAR_UPDATES)
    if current.get("last_status") in {"success", "conflicts"}:
        updates.update(
            {
                "last_status": "idle",
                "last_action": None,
                "last_message": "Previous transient status was cleared.",
            }
        )
    return write_state(path, updates)


def has_error_context(state):
    return bool(state.get("last_message") or state.get("last_details") or state.get("conflicts"))


def is_recovered_stale_error(state):
    message = str(state.get("last_message", ""))
    return message == "Home Assistant config check failed: {'result': 'ok', 'data': {}}"


def repair_startup_state(path, now, addon_version=None):
    state_file_exists = path.exists()
    current = read_state(path)
    stored_version = current.get("last_seen_addon_version")
    known_addon_version = addon_version if addon_version and addon_version != "unknown" else None
    version_changed = bool(known_addon_version) and state_file_exists and stored_version != known_addon_version
    if known_addon_version:
        addon_version = known_addon_version
        current["last_seen_addon_version"] = addon_version

    if version_changed and not current.get("deleted_devices_pending_confirmation"):
        current.update(DISPLAY_CLEAR_UPDATES)
        current.update(ALL_PREVIEW_CLEAR_UPDATES)
        current["post_apply_save_recommended"] = False
        if current.get("last_status") != "running":
            current.update(
                {
                    "last_status": "idle",
                    "last_action": None,
                    "last_message": f"HA Ops updated to {addon_version}. Previous transient status was cleared.",
                }
            )
            return write_state(path, current)

    current.update(DISPLAY_CLEAR_UPDATES)
    if not current.get("deleted_devices_pending_confirmation"):
        current.update(DELETED_DEVICES_PREVIEW_CLEAR_UPDATES)
    current.update(RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
    current.update(INTERNAL_IDS_PREVIEW_CLEAR_UPDATES)
    if current.get("last_status") == "error" and (not has_error_context(current) or is_recovered_stale_error(current)):
        current.update(
            {
                "last_status": "idle",
                "last_action": None,
                "last_message": "Previous stale error was cleared. Run an action when ready.",
            }
        )
    if current.get("last_status") != "running":
        return write_state(path, current)

    current.update(
        {
            "last_run_at": now,
            "last_status": "interrupted",
            "last_message": "Previous action was interrupted by HA Ops restart.",
        }
    )
    return write_state(path, current)
