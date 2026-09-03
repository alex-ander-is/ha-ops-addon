import json
import os
import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import i18n
import policies


def _(key, **values):
    return i18n.t(key, **values)


STATE_LOCK = threading.RLock()

READINESS_NOT_STARTED = "not_started"
READINESS_RUNNING = "running"
READINESS_REPAIRED = "repaired"
READINESS_BLOCKED = "blocked"
READINESS_BLOCKED_MESSAGE = "startup repair not complete; refresh/retry after HA Ops finishes recovery."

DIFF_FIELDS = {
    "last_diff": "apply",
    "last_save_diff": "save",
}
DIFF_CURSOR_FIELDS = {
    "last_diff": "last_diff_cursor",
    "last_save_diff": "last_save_diff_cursor",
}
REDACTED_TEXT_FIELDS = {
    "last_message",
    "last_details",
    "last_preview_warnings",
    "last_save_preview_warnings",
}
REDACTED_VALUE = "[REDACTED]"
REDACTED_URL = "[REDACTED_URL]"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_HOST = "[REDACTED_HOST]"
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|token)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
URL_RE = re.compile(r"\b(?:https?|ssh|git)://[^\s\"'<>]+|\bgit@[A-Za-z0-9.-]+:[^\s\"'<>]+")
WINDOWS_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s,;]+")
UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^/\s,;]+/)*[^/\s,;]+")
IPV4_RE = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")
HOSTNAME_RE = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\b"
)
PREVIEW_GENERATION_FIELDS = {
    *DIFF_FIELDS.keys(),
    *DIFF_CURSOR_FIELDS.values(),
    "last_diff_generated_at",
    "last_preview_commit",
    "last_preview_fingerprint",
    "last_preview_deletions",
    "last_preview_storage_changes",
    "last_preview_storage_paths",
    "last_preview_live_fingerprints",
    "last_preview_warnings",
    "last_preview_paths",
    "last_preview_conflicts",
    "last_preview_conflict_paths",
    "last_save_preview",
    "last_save_diff_generated_at",
    "last_save_preview_commit",
    "last_save_preview_fingerprint",
    "last_save_preview_warnings",
    "last_save_preview_paths",
    "last_save_preview_conflicts",
    "last_save_preview_conflict_paths",
}

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
TRANSIENT_SNAPSHOT_FIELDS = {
    "deleted_devices_pending_diff",
    "deleted_devices_pending_diff_error",
}


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
    current = sanitize_state_for_persistence(current)
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


CLEANUP_ACTIONS = {
    "preview",
    "save_preview",
    "apply",
    "save",
    "reset_git_state",
    "disk_usage",
    "deleted_devices_preview",
    "deleted_devices_delete",
    "deleted_devices_confirm",
    "deleted_devices_revert",
    "retained_devices_preview",
    "retained_devices_delete",
    "internal_ids_preview",
    "internal_ids_migrate",
    "docker_build_cache_prune",
}
CLEANUP_PENDING_ALLOWED_ACTIONS = {"deleted_devices_confirm", "deleted_devices_revert"}


def deleted_devices_pending_cleanup_active(state):
    return bool(state.get("deleted_devices_pending_confirmation"))


def cleanup_action_allowed(state, action):
    if deleted_devices_recovery_active(state):
        return action == "deleted_devices_revert"
    if deleted_devices_pending_cleanup_active(state) and action in CLEANUP_ACTIONS:
        return action in CLEANUP_PENDING_ALLOWED_ACTIONS
    return True

APPLY_PREVIEW_CLEAR_UPDATES = {
    "last_diff": "",
    "last_diff_cursor": None,
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
    "last_save_diff_cursor": None,
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
    "last_deleted_devices_tree": None,
    "last_deleted_devices_tree_error": "",
    "last_deleted_devices_enrichment": None,
    "last_deleted_devices_count": 0,
    "last_deleted_devices_device_count": 0,
    "last_deleted_devices_entity_count": 0,
    "last_deleted_devices_fingerprint": None,
    "last_deleted_devices_generated_at": None,
    "deleted_devices_pending_tree": None,
    "deleted_devices_pending_tree_error": "",
}
RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES = {
    "last_retained_devices_preview": "",
    "last_retained_devices_rows": [],
    "last_retained_devices_count": 0,
    "last_retained_devices_fingerprint": None,
    "last_retained_devices_generated_at": None,
    "last_retained_devices_device_registry_fingerprint": None,
    "last_retained_devices_scanned_paths": [],
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
        "last_diff_cursor": None,
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
        "last_save_preview": "",
        "last_save_diff": "",
        "last_save_diff_cursor": None,
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
        "last_deleted_devices_preview": "",
        "last_deleted_devices_rows": [],
        "last_deleted_devices_tree": None,
        "last_deleted_devices_tree_error": "",
        "last_deleted_devices_enrichment": None,
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
        "last_retained_devices_device_registry_fingerprint": None,
        "last_retained_devices_scanned_paths": [],
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
        "deleted_devices_pending_tree": None,
        "deleted_devices_pending_tree_error": "",
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
        "operation_generation": 0,
        "state_revision": 0,
        "command_records": {},
    }


def _artifact_root(path):
    return Path(path).parent / "diff-artifacts"


def _artifact_manifest_path(path, cursor):
    if not isinstance(cursor, dict) or cursor.get("schema") != 1:
        return None
    artifact = cursor.get("artifact")
    if not isinstance(artifact, str) or "/" in artifact or "\\" in artifact:
        return None
    return _artifact_root(path) / artifact


def _write_diff_artifact(path, text, kind, generation):
    payload = str(text or "").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    artifact_id = f"{kind}-g{int(generation)}-{digest[:16]}.diff"
    target = _artifact_root(path) / artifact_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp_path = target.with_name(f".{target.name}.tmp")
        temp_path.write_bytes(payload)
        os.replace(temp_path, target)
    return {
        "schema": 1,
        "kind": kind,
        "generation": int(generation),
        "artifact": artifact_id,
        "sha256": digest,
        "bytes": len(payload),
    }


def _read_diff_artifact(path, cursor, expected_generation=None):
    manifest = _artifact_manifest_path(path, cursor)
    if manifest is None:
        return ""
    if expected_generation is not None and int(cursor.get("generation", -1)) != int(expected_generation):
        return ""
    try:
        payload = manifest.read_bytes()
    except OSError:
        return ""
    if hashlib.sha256(payload).hexdigest() != cursor.get("sha256"):
        return ""
    return payload.decode("utf-8", errors="replace")


def hydrate_diff_fields(path, current):
    current = dict(default_state(), **dict(current or {}))
    generation = current.get("operation_generation")
    for field, cursor_field in DIFF_CURSOR_FIELDS.items():
        if not current.get(field) and current.get(cursor_field):
            current[field] = _read_diff_artifact(path, current.get(cursor_field), expected_generation=generation)
    return current


def sanitize_state_for_persistence(current):
    current = dict(default_state(), **dict(current or {}))
    for field in DIFF_FIELDS:
        if current.get(field):
            current[field] = ""
    for field in TRANSIENT_SNAPSHOT_FIELDS:
        current.pop(field, None)
    return current


def redact_sensitive_text(value):
    text = str(value)
    text = BEARER_RE.sub(REDACTED_VALUE, text)
    text = SENSITIVE_VALUE_RE.sub(lambda match: match.group(1) + REDACTED_VALUE, text)
    text = URL_RE.sub(REDACTED_URL, text)
    text = WINDOWS_PATH_RE.sub(REDACTED_PATH, text)
    text = UNIX_PATH_RE.sub(REDACTED_PATH, text)
    text = IPV4_RE.sub(REDACTED_HOST, text)
    return HOSTNAME_RE.sub(REDACTED_HOST, text)


def redact_diagnostic_value(value):
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [redact_diagnostic_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_diagnostic_value(item) for key, item in value.items()}
    return value


def redacted_state_snapshot(current):
    snapshot = sanitize_state_for_persistence(current)
    for field in DIFF_FIELDS:
        snapshot[field] = ""
    for field in REDACTED_TEXT_FIELDS:
        snapshot[field] = redact_diagnostic_value(snapshot.get(field))
    for command_id, record in snapshot.get("command_records", {}).items():
        if isinstance(record, dict) and record.get("result") is not None:
            snapshot["command_records"][command_id] = {
                **record,
                "result": redact_diagnostic_value(record.get("result")),
            }
    return snapshot


def read_state(path, hydrate_diffs=True):
    current = load_json(path, default_state())
    return hydrate_diff_fields(path, current) if hydrate_diffs else current


def write_state(path, updates):
    with STATE_LOCK:
        current = read_state(path, hydrate_diffs=False)
        current.update(updates)
        current["state_revision"] = int(current.get("state_revision") or 0) + 1
        current["operation_generation"] = int(current.get("operation_generation") or 0)
        advance_generation = any(key in PREVIEW_GENERATION_FIELDS for key in updates)
        if updates.get("deleted_devices_pending_confirmation") is True:
            advance_generation = True
        if advance_generation:
            current["operation_generation"] = int(current.get("operation_generation") or 0) + 1
        generation = current["operation_generation"]
        for field, kind in DIFF_FIELDS.items():
            if field in updates:
                cursor_field = DIFF_CURSOR_FIELDS[field]
                text = updates.get(field) or ""
                if text:
                    current[cursor_field] = _write_diff_artifact(path, text, kind, generation)
                else:
                    current[cursor_field] = None
                current[field] = ""
        if updates.get("deleted_devices_pending_confirmation") is True:
            current.update(APPLY_PREVIEW_CLEAR_UPDATES)
            current.update(SAVE_PREVIEW_CLEAR_UPDATES)
        _replace_state(path, current)
        return hydrate_diff_fields(path, current)


class OperationStore:
    """Linearized readiness, generation and diff-artifact boundary."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._readiness = READINESS_REPAIRED
        self._readiness_generation = self._persisted_generation()
        self._change_sequence = 0
        self._blocked_message = ""

    def _persisted_generation(self):
        try:
            current = read_state(self.path, hydrate_diffs=False)
            return int(current.get("operation_generation") or 0)
        except Exception:
            return 0

    def begin_repair(self):
        with self._condition:
            self._readiness_generation += 1
            self._readiness = READINESS_RUNNING
            self._blocked_message = ""
            self._change_sequence += 1
            self._condition.notify_all()
            return self._readiness_generation

    def mark_repaired(self):
        with self._condition:
            current = read_state(self.path, hydrate_diffs=False)
            current["operation_generation"] = max(
                int(current.get("operation_generation") or 0),
                int(self._readiness_generation),
            )
            now = datetime.now(timezone.utc).isoformat()
            records = dict(current.get("command_records") or {})
            for command_id, record in records.items():
                if record.get("status") in {"accepted", "running"}:
                    records[command_id] = {
                        **record,
                        "status": "failed_unknown",
                        "updated_at": now,
                        "result": {"ok": False, "message": "Command outcome is unknown after HA Ops restart."},
                    }
            current["command_records"] = records
            current["state_revision"] = int(current.get("state_revision") or 0) + 1
            _replace_state(self.path, current)
            self._readiness_generation = int(current.get("operation_generation") or 0)
            self._readiness = READINESS_REPAIRED
            self._blocked_message = ""
            self._change_sequence += 1
            self._condition.notify_all()
            return self.readiness_snapshot()

    def mark_blocked(self, message=None):
        with self._condition:
            self._readiness = READINESS_BLOCKED
            self._blocked_message = str(message or READINESS_BLOCKED_MESSAGE)
            self._change_sequence += 1
            self._condition.notify_all()
            return self.readiness_snapshot()

    def state_change_sequence(self):
        with self._condition:
            return int(self._change_sequence)

    def wait_for_state_change(self, after_sequence, timeout=None):
        with self._condition:
            self._condition.wait_for(
                lambda: self._change_sequence != int(after_sequence),
                timeout=timeout,
            )
            return int(self._change_sequence)

    def readiness_snapshot(self):
        with self._condition:
            return {
                "status": self._readiness,
                "generation": int(self._readiness_generation),
                "message": self._blocked_message if self._readiness == READINESS_BLOCKED else "",
            }

    def assert_repaired_for_current_preview_read(self, reason="current-preview"):
        snapshot = self.readiness_snapshot()
        if snapshot["status"] != READINESS_REPAIRED:
            raise RuntimeError(READINESS_BLOCKED_MESSAGE)
        return snapshot["generation"]

    def read_state(self, hydrate_diffs=True, require_repaired=False):
        with self._lock:
            if require_repaired:
                self.assert_repaired_for_current_preview_read()
            return read_state(self.path, hydrate_diffs=hydrate_diffs)

    def write_state(self, updates):
        with self._condition:
            current = write_state(self.path, updates)
            self._readiness_generation = int(current.get("operation_generation") or 0)
            self._change_sequence += 1
            self._condition.notify_all()
            return current

    def claim_command(self, command_id, command, generation, payload):
        """Durably claim one browser mutation before any work is scheduled."""
        try:
            parsed = uuid.UUID(str(command_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("command_id must be a UUID") from exc
        canonical_id = str(parsed)
        if canonical_id != str(command_id).lower():
            raise ValueError("command_id must use canonical UUID form")
        if not isinstance(command, str) or not command:
            raise ValueError("command is required")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        canonical_payload = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        payload_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        with self._condition:
            current = read_state(self.path, hydrate_diffs=False)
            records = dict(current.get("command_records") or {})
            existing = records.get(canonical_id)
            if existing is not None:
                if existing.get("command") != command or existing.get("payload_sha256") != payload_digest:
                    raise ValueError("command_id was already used for a different command")
                return False, dict(existing)
            current_generation = int(current.get("operation_generation") or 0)
            if int(generation) != current_generation:
                raise RuntimeError("stale command generation; replay state and try again")
            now = datetime.now(timezone.utc).isoformat()
            record = {
                "command_id": canonical_id,
                "command": command,
                "generation": current_generation,
                "payload_sha256": payload_digest,
                "status": "accepted",
                "accepted_at": now,
                "updated_at": now,
                "job_id": canonical_id,
                "result": None,
            }
            records[canonical_id] = record
            current["command_records"] = records
            current["state_revision"] = int(current.get("state_revision") or 0) + 1
            _replace_state(self.path, current)
            self._change_sequence += 1
            self._condition.notify_all()
            return True, dict(record)

    def update_command(self, command_id, status, result=None):
        if status not in {"accepted", "running", "terminal", "failed_unknown"}:
            raise ValueError("invalid command status")
        with self._condition:
            current = read_state(self.path, hydrate_diffs=False)
            records = dict(current.get("command_records") or {})
            record = records.get(str(command_id))
            if record is None:
                return None
            record = {
                **record,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "result": result if result is not None else record.get("result"),
            }
            records[str(command_id)] = record
            current["command_records"] = records
            current["state_revision"] = int(current.get("state_revision") or 0) + 1
            _replace_state(self.path, current)
            self._change_sequence += 1
            self._condition.notify_all()
            return dict(record)

    def current_preview_snapshot(self, hydrate_diffs=True):
        with self._lock:
            generation = self.assert_repaired_for_current_preview_read()
            current = read_state(self.path, hydrate_diffs=hydrate_diffs)
            if int(current.get("operation_generation") or 0) != int(generation):
                raise RuntimeError(READINESS_BLOCKED_MESSAGE)
            return generation, current

    def redacted_snapshot(self):
        with self._lock:
            readiness = self.readiness_snapshot()
            readiness["message"] = redact_sensitive_text(readiness.get("message", ""))
            return {
                "readiness": readiness,
                "state": redacted_state_snapshot(read_state(self.path, hydrate_diffs=False)),
            }

    def diff_get(self, cursor):
        with self._lock:
            generation = self.assert_repaired_for_current_preview_read("diff_get")
            if not isinstance(cursor, dict) or int(cursor.get("generation", -1)) != int(generation):
                raise RuntimeError("stale preview diff cursor; run a fresh Preview.")
            text = _read_diff_artifact(self.path, cursor, expected_generation=generation)
            if not text:
                raise RuntimeError("preview diff artifact is unavailable; run a fresh Preview.")
            return text


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
