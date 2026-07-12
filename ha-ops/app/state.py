import json
import os
import hashlib
import threading
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import i18n
import policies


def _(key, **values):
    return i18n.t(key, **values)


STATE_LOCK = threading.Lock()

# A cleanup rollback can outlive the App process.  Keep the recovery contract
# in state rather than deriving it from a transient job status.
DELETED_DEVICES_RECOVERY_NONE = "none"
DELETED_DEVICES_RECOVERY_RESTORE_REQUIRED = "restore_required"
DELETED_DEVICES_RECOVERY_RECOVERING = "recovering"
DELETED_DEVICES_RECOVERY_MANUAL = "manual_recovery"
DELETED_DEVICES_RECOVERY_ACTIVE = {
    DELETED_DEVICES_RECOVERY_RESTORE_REQUIRED,
    DELETED_DEVICES_RECOVERY_RECOVERING,
    DELETED_DEVICES_RECOVERY_MANUAL,
}

DOCKER_PRUNE_FENCE_KEY = "docker_build_cache_prune_fence"
DOCKER_PRUNE_ACTIVE_PHASES = {"accepted", "dispatching"}


def _valid_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


def classify_docker_prune_fence(value):
    if value is None:
        return {"kind": "idle"}
    valid = isinstance(value, dict) and value.get("schema") == 1
    operation_id = value.get("operation_id") if isinstance(value, dict) else None
    phase = value.get("phase") if isinstance(value, dict) else None
    try:
        parsed_uuid = uuid.UUID(operation_id) if isinstance(operation_id, str) else None
        valid = valid and str(parsed_uuid) == operation_id.lower() and parsed_uuid.variant == uuid.RFC_4122
    except (ValueError, AttributeError):
        valid = False
    valid = valid and phase in DOCKER_PRUNE_ACTIVE_PHASES | {"resolution_required"}
    valid = valid and _valid_timestamp(value.get("accepted_at"))
    for key in ("context", "error"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 2000):
            valid = False
    if valid:
        return {"kind": "valid", "phase": phase, "operation_id": operation_id.lower(), "value": value}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    token = "corrupt:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"kind": "corrupt", "phase": "resolution_required", "recovery_token": token, "value": value}


def new_docker_prune_fence(operation_id, accepted_at):
    return {"schema": 1, "operation_id": str(operation_id), "phase": "accepted", "accepted_at": accepted_at}


def transition_docker_prune_fence(path, operation_id, from_phases, phase, updates=None):
    with STATE_LOCK:
        current = read_state(path)
        classified = classify_docker_prune_fence(current.get(DOCKER_PRUNE_FENCE_KEY))
        if classified.get("kind") != "valid" or classified.get("operation_id") != operation_id or classified.get("phase") not in set(from_phases):
            return None
        fence = dict(classified["value"], phase=phase)
        if updates:
            fence.update(updates)
        current[DOCKER_PRUNE_FENCE_KEY] = fence
        _replace_state(path, current)
        return current


def clear_docker_prune_fence(path, mode, identity, updates=None):
    with STATE_LOCK:
        current = read_state(path)
        classified = classify_docker_prune_fence(current.get(DOCKER_PRUNE_FENCE_KEY))
        matches = (
            mode == "operation" and classified.get("kind") == "valid"
            and classified.get("phase") == "resolution_required" and classified.get("operation_id") == identity
        ) or (mode == "corrupt" and classified.get("kind") == "corrupt" and classified.get("recovery_token") == identity)
        if not matches:
            return None
        current[DOCKER_PRUNE_FENCE_KEY] = None
        if updates:
            current.update(updates)
        _replace_state(path, current)
        return current


def complete_docker_prune_fence(path, operation_id, updates):
    with STATE_LOCK:
        current = read_state(path)
        classified = classify_docker_prune_fence(current.get(DOCKER_PRUNE_FENCE_KEY))
        if (
            classified.get("kind") != "valid"
            or classified.get("operation_id") != operation_id
            or classified.get("phase") != "dispatching"
        ):
            return None
        current[DOCKER_PRUNE_FENCE_KEY] = None
        current.update(updates)
        _replace_state(path, current)
        return current


def _replace_state(path, current):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(current, indent=2, sort_keys=True))
    os.replace(temp_path, path)


def deleted_devices_recovery_phase(state):
    """Return a compatible recovery phase for persisted cleanup state."""
    phase = state.get("deleted_devices_recovery_phase")
    if phase in DELETED_DEVICES_RECOVERY_ACTIVE:
        return phase
    # Before the coordinator existed, an interrupted delete with a snapshot
    # was the only persisted recovery signal.
    if (
        state.get("last_status") == "running"
        and state.get("last_action") in {"deleted_devices_delete", "deleted_devices_revert"}
        and state.get("deleted_devices_rollback_path")
    ):
        return DELETED_DEVICES_RECOVERY_RESTORE_REQUIRED
    if phase == DELETED_DEVICES_RECOVERY_NONE:
        return phase
    return DELETED_DEVICES_RECOVERY_NONE


def deleted_devices_recovery_active(state):
    return deleted_devices_recovery_phase(state) in DELETED_DEVICES_RECOVERY_ACTIVE


def deleted_devices_recovery_allows(state, action):
    """Only Revert can resolve a persisted manual/restart recovery fence."""
    return not deleted_devices_recovery_active(state) or action == "deleted_devices_revert"

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
    "last_preview_conflict_paths": [],
    "apply_preview_resolutions": {},
    "apply_preview_selected_paths": [],
}
SAVE_PREVIEW_CLEAR_UPDATES = {
    "last_save_preview": "",
    "last_save_diff": "",
    "last_save_diff_generated_at": None,
    "last_save_preview_commit": None,
    "last_save_preview_fingerprint": None,
    "last_save_preview_warnings": [],
    "last_save_preview_paths": [],
    "last_save_preview_conflicts": False,
    "last_save_preview_conflict_paths": [],
    "last_save_commit_subject": None,
    "save_preview_resolutions": {},
    "save_preview_selected_paths": [],
}
SAVE_PUSH_RETRY_CLEAR_UPDATES = {
    "save_push_retry_pending": False,
    "save_push_retry_commit": None,
}
DELETED_DEVICES_PREVIEW_CLEAR_UPDATES = {
    "last_deleted_devices_preview": "",
    "last_deleted_devices_rows": [],
    "last_deleted_devices_count": 0,
    "last_deleted_devices_device_count": 0,
    "last_deleted_devices_entity_count": 0,
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


def save_preview_clear_updates(clear_save_retry_pending=False):
    updates = dict(SAVE_PREVIEW_CLEAR_UPDATES)
    if clear_save_retry_pending:
        updates.update(SAVE_PUSH_RETRY_CLEAR_UPDATES)
    return updates


def display_clear_updates(preserve_save_retry=False, clear_save_retry_pending=False):
    updates = dict(DISPLAY_CLEAR_UPDATES)
    if preserve_save_retry:
        for key in SAVE_PREVIEW_CLEAR_UPDATES:
            updates.pop(key, None)
    elif clear_save_retry_pending:
        updates.update(SAVE_PUSH_RETRY_CLEAR_UPDATES)
    return updates


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
        "last_message": _("message.no_runs_yet"),
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
        "last_preview_conflict_paths": [],
        "apply_preview_resolutions": {},
        "apply_preview_selected_paths": [],
        "last_save_preview_commit": None,
        "last_save_preview_fingerprint": None,
        "last_save_preview_warnings": [],
        "last_save_preview_paths": [],
        "last_save_preview_conflicts": False,
        "last_save_preview_conflict_paths": [],
        "last_save_commit_subject": None,
        "save_preview_resolutions": {},
        "save_preview_selected_paths": [],
        "last_deleted_devices_preview": "",
        "last_deleted_devices_rows": [],
        "last_deleted_devices_count": 0,
        "last_deleted_devices_device_count": 0,
        "last_deleted_devices_entity_count": 0,
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
        "deleted_devices_rollback_format": None,
        "deleted_devices_rollback_fingerprint": None,
        "deleted_devices_applied_fingerprint": None,
        "deleted_devices_pending_device_count": 0,
        "deleted_devices_pending_entity_count": 0,
        "deleted_devices_recovery_phase": DELETED_DEVICES_RECOVERY_NONE,
        "managed_addons": [],
        "homeassistant_organizer_enabled": None,
        "include_redundant_data": False,
        "post_apply_save_recommended": False,
        "save_push_retry_pending": False,
        "save_push_retry_commit": None,
        DOCKER_PRUNE_FENCE_KEY: None,
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
        if updates.get("deleted_devices_pending_confirmation") is True:
            current.update(APPLY_PREVIEW_CLEAR_UPDATES)
            current.update(SAVE_PREVIEW_CLEAR_UPDATES)
        _replace_state(path, current)
        return current


def clear_display_state(path, preserve_save_retry=False, clear_save_retry_pending=False):
    updates = display_clear_updates(
        preserve_save_retry=preserve_save_retry,
        clear_save_retry_pending=clear_save_retry_pending,
    )
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
                "last_message": _("message.previous_transient_status_cleared"),
            }
        )
    return write_state(path, updates)


def has_error_context(state):
    return bool(state.get("last_message") or state.get("last_details") or state.get("conflicts"))


def is_recovered_stale_error(state):
    message = str(state.get("last_message", ""))
    return message == "Home Assistant config check failed: {'result': 'ok', 'data': {}}"


def repair_startup_state(path, now, addon_version=None, preserve_save_retry=False, clear_save_retry_pending=False):
    state_file_exists = path.exists()
    current = read_state(path)
    stored_version = current.get("last_seen_addon_version")
    known_addon_version = addon_version if addon_version and addon_version != "unknown" else None
    version_changed = bool(known_addon_version) and state_file_exists and stored_version != known_addon_version
    if known_addon_version:
        addon_version = known_addon_version
        current["last_seen_addon_version"] = addon_version

    if version_changed:
        current.update(
            display_clear_updates(
                preserve_save_retry=preserve_save_retry,
                clear_save_retry_pending=clear_save_retry_pending,
            )
        )
        current.update(APPLY_PREVIEW_CLEAR_UPDATES)
        if not preserve_save_retry:
            current.update(save_preview_clear_updates(clear_save_retry_pending=clear_save_retry_pending))
        if not current.get("deleted_devices_pending_confirmation"):
            current.update(DELETED_DEVICES_PREVIEW_CLEAR_UPDATES)
        current.update(RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
        current.update(INTERNAL_IDS_PREVIEW_CLEAR_UPDATES)
        current["post_apply_save_recommended"] = False
        if current.get("last_status") != "running" and not current.get("deleted_devices_pending_confirmation"):
            current.update(
                {
                    "last_status": "idle",
                    "last_action": None,
                    "last_message": _("message.addon_updated_status_cleared", version=addon_version),
                }
            )
            return write_state(path, current)

    current.update(
        display_clear_updates(
            preserve_save_retry=preserve_save_retry,
            clear_save_retry_pending=clear_save_retry_pending,
        )
    )
    if not current.get("deleted_devices_pending_confirmation"):
        current.update(DELETED_DEVICES_PREVIEW_CLEAR_UPDATES)
    current.update(RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
    current.update(INTERNAL_IDS_PREVIEW_CLEAR_UPDATES)
    if current.get("last_status") == "error" and (not has_error_context(current) or is_recovered_stale_error(current)):
        current.update(
            {
                "last_status": "idle",
                "last_action": None,
                "last_message": _("message.previous_stale_error_cleared"),
            }
        )
    if current.get("last_status") != "running":
        return write_state(path, current)

    current.update(
        {
            "last_run_at": now,
            "last_status": "interrupted",
            "last_message": _("message.previous_action_interrupted"),
        }
    )
    return write_state(path, current)
