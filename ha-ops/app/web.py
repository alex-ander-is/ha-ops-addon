from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import base64
import hashlib
import json
import re
import select
import socket
import struct
import threading
import uuid

import conflicts as conflict_logic
import git_ops
import i18n
import jobs as job_logic
import manifest as manifest_logic
import registry_diff
import state as state_store
import sync as sync_logic
import diff_split


def _(key, **values):
    return i18n.t(key, **values)


def deleted_entries_label(device_count, entity_count):
    if device_count and entity_count:
        return _("label.deleted_devices_and_entities")
    if device_count:
        return _("label.deleted_devices")
    if entity_count:
        return _("label.deleted_entities")
    return _("label.deleted_devices")


def deleted_entries_label_from_state(state, pending=False):
    prefix = "deleted_devices_pending" if pending else "last_deleted_devices"
    return deleted_entries_label(
        int(state.get(f"{prefix}_device_count") or 0),
        int(state.get(f"{prefix}_entity_count") or 0),
    )


STATUS_LABEL_KEYS = {
    "busy": "status.busy",
    "conflicts": "status.conflicts",
    "error": "status.error",
    "idle": "status.idle",
    "interrupted": "status.interrupted",
    "pending": "status.pending",
    "pending decision": "status.pending_decision",
    "running": "status.running",
    "success": "status.done",
    "warning": "status.warning",
}


def current_manifest_preview(ctx):
    options = ctx.load_options()
    try:
        repo_dir = ctx.repo_checkout_path(options)
        try:
            addons = ctx.get_installed_addons()
        except Exception:
            addons = None
        if repo_dir.exists():
            manifest, _ = ctx.load_manifest(repo_dir, options, addons)
        else:
            manifest = ctx.default_manifest(options)
        try:
            targets = ctx.resolve_targets(repo_dir, manifest, addons or [], require_source=False)
        except Exception:
            targets = manifest.get("targets", [])
        previews = []
        for target in targets:
            previews.append(
                {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "source": target.get("source"),
                    "source_path": target.get("source_path"),
                    "live_path": target.get("live_path"),
                    "addon_slug": target.get("addon_slug"),
                    "addon_slug_suffix": target.get("addon_slug_suffix"),
                    "resolved_slug": target.get("resolved_slug"),
                    "allow_protected_storage": target.get("allow_protected_storage", False),
                    "organizer_enabled": manifest_logic.organizer_target_enabled(target),
                }
            )
        return previews
    except Exception:
        return []


def job_is_running(ctx, state=None):
    state = state if state is not None else ctx.read_state()
    run_lock = getattr(ctx, "run_lock", None)
    if run_lock is None:
        return state.get("last_status") == "running"
    if not run_lock.acquire(blocking=False):
        return True
    run_lock.release()
    return False


def repair_stale_running_state(ctx, state):
    if state.get("last_status") != "running":
        return state
    run_lock = getattr(ctx, "run_lock", None)
    if run_lock is None or not run_lock.acquire(blocking=False):
        return state
    try:
        current = ctx.read_state()
        if current.get("last_status") != "running":
            return current
        return ctx.write_state(
            {
                "last_run_at": ctx.utc_now(),
                "last_status": "interrupted",
                "last_message": _("message.previous_action_interrupted"),
            }
        )
    finally:
        run_lock.release()


def recovery_action_allowed(ctx, action):
    state = ctx.read_state()
    return action_allowed_in_state(state, action)


def action_allowed_in_state(state, action):
    return job_logic.recovery_action_allowed(state, action) and state_store.cleanup_action_allowed(state, action)


def reconcile_docker_prune_orphan(ctx, lock_acquired=False):
    run_lock = getattr(ctx, "run_lock", None)
    acquired_here = False
    if run_lock is not None and not lock_acquired:
        if not run_lock.acquire(blocking=False):
            return ctx.read_state(), False
        acquired_here = True
    try:
        current = ctx.read_state()
        classify = getattr(ctx, "classify_docker_prune_fence", None)
        fence = (
            classify(current)
            if classify is not None
            else state_store.classify_docker_prune_fence(current.get(state_store.DOCKER_PRUNE_FENCE_KEY))
        )
        if fence.get("kind") == "valid" and fence.get("phase") in state_store.DOCKER_PRUNE_ACTIVE_PHASES:
            transition = getattr(ctx, "transition_docker_prune_fence", None)
            updated = transition(
                    fence["operation_id"],
                    state_store.DOCKER_PRUNE_ACTIVE_PHASES,
                    "resolution_required",
                    {"context": _("message.docker_prune_orphaned")},
                ) if transition is not None else None
            if updated is not None:
                current = updated
        return current, True
    finally:
        if acquired_here:
            run_lock.release()


def reserve_action_slot(ctx, action="mutation"):
    if not recovery_action_allowed(ctx, action):
        return False, None, False
    run_lock = getattr(ctx, "run_lock", None)
    if run_lock is None:
        state = ctx.read_state()
        return not state.get("last_status") == "running" and action_allowed_in_state(state, action), state, False

    if not run_lock.acquire(blocking=False):
        return False, None, False
    try:
        state, reconciled = reconcile_docker_prune_orphan(ctx, lock_acquired=True)
        if not action_allowed_in_state(state, action):
            run_lock.release()
            return False, state, False
        return True, state, True
    except Exception:
        run_lock.release()
        raise


def release_action_slot(ctx, lock_acquired):
    if lock_acquired:
        ctx.run_lock.release()


def reserve_mutation_slot(ctx, action="mutation"):
    if job_is_running(ctx):
        return False, None, False
    return reserve_action_slot(ctx, action)


def addon_slug_value(addon):
    return addon.get("slug") or addon.get("name") or ""


def addon_display_name(addon):
    name = addon.get("name") or addon_slug_value(addon)
    slug = addon_slug_value(addon)
    return f"{name} ({slug})" if slug and slug not in name else name


def full_conflict_detail(text):
    return text


def file_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _("error.conflict_detail_unavailable", error=exc)


def file_diff(ctx, left_label, left_path, right_label, right_path):
    left_path = Path(left_path)
    right_path = Path(right_path)
    if not left_path.exists():
        return _("error.diff_unavailable_label_missing", label=left_label, path=left_path)
    if not right_path.exists():
        return _("error.diff_unavailable_label_missing", label=right_label, path=right_path)

    result = ctx.run_command(["diff", "-u", "-L", left_label, "-L", right_label, str(left_path), str(right_path)])
    if result.returncode == 0:
        return _("text.no_differences")
    if result.returncode == 1:
        return full_conflict_detail(result.stdout.strip())
    return f"{_('error.diff_unavailable')}\n{(result.stderr or result.stdout).strip()}"


def normalized_save_conflict_file_diff(ctx, left_label, left_path, right_label, right_path):
    diff_root = ctx.work_dir / "save-conflict-diff"
    ctx.clear_tree(diff_root)
    normalized_pair = sync_logic.normalize_storage_file_pair_for_diff(left_path, right_path, diff_root)
    if normalized_pair is None:
        return file_diff(ctx, left_label, left_path, right_label, right_path)
    return file_diff(ctx, left_label, normalized_pair[0], right_label, normalized_pair[1])


def save_conflict_detail(ctx, repo_dir, targets, path, include_redundant_data=False):
    safe_path = git_ops.safe_repo_relative_path(path)
    repo_file = Path(repo_dir) / safe_path
    for target in targets or []:
        source_path = Path(target.get("source_path", ""))
        target_id = str(target.get("id", ""))
        if not source_path or not target_id:
            continue
        try:
            source_root = source_path.relative_to(repo_dir).as_posix()
        except ValueError:
            continue
        if not safe_path.startswith(f"{source_root}/"):
            continue
        relative = Path(safe_path).relative_to(source_root)
        preview_file = ctx.work_dir / "save-preview" / target_id / relative
        if include_redundant_data:
            return file_diff(ctx, f"Git: {safe_path}", repo_file, f"HA: {safe_path}", preview_file)
        return normalized_save_conflict_file_diff(ctx, f"Git: {safe_path}", repo_file, f"HA: {safe_path}", preview_file)
    return _("error.diff_unavailable_no_target", path=safe_path)


def load_conflict_targets(ctx, options, state, repo_dir):
    targets = state.get("last_targets") or []
    if targets:
        return targets
    try:
        try:
            addons = ctx.get_installed_addons()
        except Exception:
            addons = None
        manifest, _ = ctx.load_manifest(repo_dir, options, addons)
        return ctx.resolve_targets(repo_dir, manifest, addons, require_source=False)
    except Exception:
        return []


def conflict_items(ctx, state, options):
    paths = state.get("conflicts", [])
    if not paths:
        return []

    try:
        repo_dir = ctx.repo_checkout_path(options)
    except Exception:
        return paths

    items = []
    conflict_type = state.get("conflict_type")
    targets = load_conflict_targets(ctx, options, state, repo_dir) if conflict_type == "save_unknown_base" else []
    for path in paths:
        try:
            safe_path = git_ops.safe_repo_relative_path(path)
            if conflict_type == "save_unknown_base":
                detail = save_conflict_detail(ctx, repo_dir, targets, safe_path, bool(state.get("include_redundant_data")))
            else:
                detail = full_conflict_detail(file_text(Path(repo_dir) / safe_path).strip())
        except Exception as exc:
            safe_path = str(path)
            detail = _("error.conflict_detail_unavailable", error=exc)
        items.append({"path": safe_path, "detail": detail})
    return items


def action_label(action):
    return {
        "apply": _("action.apply"),
        "preview": _("action.preview_apply"),
        "save": _("action.save"),
        "save_preview": _("action.preview_save"),
        "deleted_devices_preview": _("action.check_deleted_devices"),
        "deleted_devices_delete": _("action.remove_deleted_entries"),
        "deleted_devices_confirm": _("action.confirm_changes"),
        "deleted_devices_revert": _("action.revert_changes"),
        "disk_usage": _("action.check_disk_usage"),
        "internal_ids_preview": _("action.check_actions_ids"),
        "internal_ids_migrate": _("action.migrate_and_save"),
        "rollback": _("action.rollback"),
    }.get(action or "", action or _("label.none"))


def log_text_for_state(ctx, state, last_status, pending_deleted_devices, rollback_path):
    message = str(state.get("last_message") or "")
    details = [str(item) for item in (state.get("last_details") or []) if str(item)]

    if pending_deleted_devices and rollback_path:
        entries = deleted_entries_label(
            int(state.get("deleted_devices_pending_device_count") or 0),
            int(state.get("deleted_devices_pending_entity_count") or 0),
        )
        lines = [
            _("message.deleted_devices_waiting", entries=entries),
            "",
            f"{_('label.previous_action')}: {action_label(state.get('last_action'))}",
        ]
        if message:
            lines.append(f"{_('label.last_result')}: {message}")
        lines.extend(["", _("text.current_state")])
        try:
            cleanup = ctx.deleted_devices_cleanup_status(rollback_path)
            removed_entries = deleted_entries_label(cleanup.get("removed_devices", 0), cleanup.get("removed_entities", 0))
            current_entries = deleted_entries_label(cleanup.get("current_devices", 0), cleanup.get("current_entities", 0))
            added_entries = deleted_entries_label(cleanup.get("added_devices", 0), cleanup.get("added_entities", 0))
            lines.extend(
                [
                    _("text.cleanup_removed", count=cleanup["removed"], entries=removed_entries),
                    _("text.cleanup_current", count=cleanup["current"], entries=current_entries),
                    _("text.cleanup_added", count=cleanup["added"], entries=added_entries),
                    _("text.cleanup_returned", count=cleanup["returned"]),
                ]
            )
            entries = removed_entries
        except Exception as exc:
            lines.append(_("text.rollback_status_unavailable", error=exc))
        lines.extend(
            [
                _("text.rollback_available"),
                "",
                _("notice.deleted_devices_confirm_effect", entries=entries),
                _("notice.deleted_devices_revert_effect", entries=entries),
            ]
        )
        if details:
            lines.extend(["", _("label.previous_details"), *details])
        return "\n".join(lines)

    if details:
        return "\n".join(details)
    if message:
        return message
    return _("state.running") if last_status == "running" else _("message.no_log_entries")


def render_page(ctx):
    """Serve the same inert client shell for every ingress request."""
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def start_background(target, *args, lock_acquired=False):
    kwargs = {"lock_acquired": True} if lock_acquired else {}
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def job_action(target):
    name = getattr(target, "__name__", "")
    return name.removeprefix("run_").removesuffix("_job") or "mutation"


PREVIEW_CONSUMING_ACTIONS = {"save", "apply"}
WS_MUTATING_COMMANDS = {
    "preview",
    "save_preview",
    "apply",
    "save",
    "resolve_save_preview",
    "resolve_apply_preview",
    "select_save_preview",
    "select_apply_preview",
    "reset_git_state",
    "disk_usage",
    "deleted_devices_preview",
    "retained_devices_preview",
    "retained_devices_delete",
    "select_retained_device",
    "internal_ids_preview",
    "internal_ids_migrate",
    "select_internal_ids",
    "acknowledge_recovery",
    "retry_interrupted_save",
    "deleted_devices_delete",
    "deleted_devices_confirm",
    "deleted_devices_revert",
    "rollback",
}


class StalePreviewDecision(RuntimeError):
    pass


def body_first(body, key, default=""):
    value = (body or {}).get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def _canonical_list(value):
    return sorted(str(item) for item in (value or []) if str(item))


def _canonical_dict(value):
    if not isinstance(value, dict):
        return {}
    return {str(key): str(value[key]) for key in sorted(value)}


def _cursor_identity(cursor):
    if not isinstance(cursor, dict):
        return None
    return {
        key: cursor.get(key)
        for key in ("schema", "kind", "generation", "artifact", "sha256", "bytes")
        if key in cursor
    }


def preview_identity_for_state(state, direction):
    if direction == "save":
        return {
            "direction": "save",
            "preview_id": state.get("save_preview_id"),
            "decision_revision": int(state.get("save_decision_revision") or 0),
            "commit": state.get("last_save_preview_commit"),
            "fingerprint": state.get("last_save_preview_fingerprint"),
            "paths": _canonical_list(state.get("last_save_preview_paths")),
            "conflict_paths": _canonical_list(state.get("last_save_preview_conflict_paths")),
            "diff_cursor": _cursor_identity(state.get("last_save_diff_cursor")),
        }
    return {
        "direction": "apply",
        "preview_id": state.get("apply_preview_id"),
        "decision_revision": int(state.get("apply_decision_revision") or 0),
        "commit": state.get("last_preview_commit"),
        "fingerprint": state.get("last_preview_fingerprint"),
        "live_fingerprints": _canonical_dict(state.get("last_preview_live_fingerprints")),
        "paths": _canonical_list(state.get("last_preview_paths")),
        "conflict_paths": _canonical_list(state.get("last_preview_conflict_paths")),
        "diff_cursor": _cursor_identity(state.get("last_diff_cursor")),
    }


def _parse_preview_identity(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    identity = {
        "direction": value.get("direction"),
        "preview_id": value.get("preview_id"),
        "decision_revision": int(value.get("decision_revision") or 0),
        "commit": value.get("commit"),
        "fingerprint": value.get("fingerprint"),
        "paths": _canonical_list(value.get("paths")),
        "conflict_paths": _canonical_list(value.get("conflict_paths")),
        "diff_cursor": _cursor_identity(value.get("diff_cursor")),
    }
    if value.get("direction") == "apply":
        identity["live_fingerprints"] = _canonical_dict(value.get("live_fingerprints"))
    return identity


def assert_preview_decision_identity(state, direction, body):
    current = preview_identity_for_state(state, direction)
    if not current.get("paths"):
        return
    if _parse_preview_identity((body or {}).get("preview_identity")) != current:
        raise StalePreviewDecision(_("error.preview_stale_decision"))


def preview_decision_digest(state, direction):
    paths = sorted(state.get("last_save_preview_paths" if direction == "save" else "last_preview_paths") or [])
    selected = set(state.get("save_preview_selected_paths" if direction == "save" else "apply_preview_selected_paths") or [])
    resolutions = state.get("save_preview_resolutions" if direction == "save" else "apply_preview_resolutions") or {}
    decisions = [
        {"choice": (resolutions.get(path) or ("ha" if direction == "save" else "git")) if path in selected
         else ("git" if direction == "save" else "ha"), "path": path, "selected": path in selected}
        for path in paths
    ]
    canonical = json.dumps(decisions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def retained_preview_identity_matches_state(state, body):
    if not state.get("last_retained_devices_fingerprint") or not state.get("last_retained_devices_generated_at"):
        return False
    return (
        body_first(body, "retained_preview_fingerprint") == state.get("last_retained_devices_fingerprint")
        and body_first(body, "retained_preview_generated_at") == state.get("last_retained_devices_generated_at")
    )


def mutate_preview_decision(ctx, direction, action, body):
    ok, state, lock_acquired = reserve_mutation_slot(ctx)
    if not ok:
        return command_result(False, _("error.running_action"), status=409)
    try:
        assert_preview_decision_identity(state, direction, body)
        paths_key = "last_save_preview_paths" if direction == "save" else "last_preview_paths"
        selected_key = "save_preview_selected_paths" if direction == "save" else "apply_preview_selected_paths"
        revision_key = "save_decision_revision" if direction == "save" else "apply_decision_revision"
        resolutions_key = "save_preview_resolutions" if direction == "save" else "apply_preview_resolutions"
        conflict_paths_key = "last_save_preview_conflict_paths" if direction == "save" else "last_preview_conflict_paths"
        paths = [str(item) for item in (state.get(paths_key) or []) if str(item)]
        path_set = set(paths)
        if action == "resolve":
            raw_path = body_first(body, "path")
            choice = body_first(body, "choice")
            safe_path = git_ops.safe_repo_relative_path(raw_path)
            if choice not in {"ha", "git"}:
                raise RuntimeError(_("error.invalid_preview_choice"))
            if safe_path not in paths:
                raise RuntimeError(_("error.preview_path_not_pending"))
            resolutions = dict(state.get(resolutions_key) or {})
            resolutions[safe_path] = choice
            conflict_paths = [str(item) for item in (state.get(conflict_paths_key) or paths) if str(item)]
            remaining = [path for path in conflict_paths if path not in resolutions]
            ctx.write_state(
                {
                    resolutions_key: resolutions,
                    revision_key: int(state.get(revision_key) or 0) + 1,
                    "last_run_at": ctx.utc_now(),
                    "last_status": "idle",
                    "last_action": f"resolve_{direction}_preview",
                    "last_message": (
                        _("message.resolved_preview_file", path=safe_path, remaining=len(remaining))
                        if remaining
                        else _("message.resolved_all_preview_files", direction=direction)
                    ),
                }
            )
        else:
            selection_action = body_first(body, "selection_action")
            if selection_action == "all":
                selected = paths
            elif selection_action == "none":
                selected = []
            else:
                raw_path = body_first(body, "path")
                safe_path = git_ops.safe_repo_relative_path(raw_path)
                if safe_path not in path_set:
                    raise RuntimeError(_("error.preview_path_not_pending"))
                selected_set = {str(item) for item in (state.get(selected_key) or []) if str(item) in path_set}
                if body_first(body, "selected") == "1":
                    selected_set.add(safe_path)
                else:
                    selected_set.discard(safe_path)
                selected = [path for path in paths if path in selected_set]
            ctx.write_state(
                {
                    selected_key: selected,
                    revision_key: int(state.get(revision_key) or 0) + 1,
                    "last_run_at": ctx.utc_now(),
                    "last_status": "idle",
                    "last_action": f"select_{direction}_preview",
                    "last_message": _("message.selected_preview_files", count=len(selected)),
                }
            )
        return command_result(True, ctx.read_state().get("last_message", ""))
    except StalePreviewDecision as exc:
        return command_result(False, str(exc), status=409)
    except Exception as exc:
        if action == "resolve":
            ctx.write_state(
                {
                    "last_run_at": ctx.utc_now(),
                    "last_status": "error",
                    "last_action": f"resolve_{direction}_preview",
                    "last_message": str(exc),
                    "last_details": [str(exc)],
                }
            )
        return command_result(False, str(exc), status=400)
    finally:
        release_action_slot(ctx, lock_acquired)


def assert_command_readiness(ctx, action, expected_generation=None):
    if action not in PREVIEW_CONSUMING_ACTIONS:
        return expected_generation
    guard = getattr(ctx, "assert_repaired_for_current_preview_read", None)
    if guard is None:
        return expected_generation
    generation = guard(action)
    if expected_generation is not None and int(generation) != int(expected_generation):
        raise RuntimeError(state_store.READINESS_BLOCKED_MESSAGE)
    return generation


def start_reserved_background(ctx, target, *args, state_updates=None, lock_acquired=False, command_id=None):
    action = job_action(target)
    try:
        expected_generation = assert_command_readiness(ctx, action)
    except RuntimeError:
        if lock_acquired:
            ctx.run_lock.release()
        return False
    if not recovery_action_allowed(ctx, action):
        if lock_acquired:
            ctx.run_lock.release()
        return False
    if lock_acquired:
        # A caller may have reserved the lock before a concurrent recovery
        # fence was persisted; check again while it owns that reservation.
        state, _reconciled = reconcile_docker_prune_orphan(ctx, lock_acquired=True)
        if not action_allowed_in_state(state, action):
            ctx.run_lock.release()
            return False
        try:
            assert_command_readiness(ctx, action, expected_generation)
        except RuntimeError:
            ctx.run_lock.release()
            return False
        ok, reserved_lock = True, True
    else:
        ok, _state, reserved_lock = reserve_action_slot(ctx, action)
    if not ok:
        return False
    try:
        try:
            assert_command_readiness(ctx, action, expected_generation)
        except RuntimeError:
            release_action_slot(ctx, reserved_lock)
            return False
        if state_updates:
            ctx.write_state(state_updates)
        if command_id:
            def run_claimed_command():
                ctx.update_command(command_id, "running")
                try:
                    target(*args, lock_acquired=reserved_lock)
                    final_state = ctx.read_state()
                    ctx.update_command(
                        command_id,
                        "terminal",
                        {
                            "ok": final_state.get("last_status") not in {"error", "interrupted"},
                            "status": final_state.get("last_status"),
                            "message": final_state.get("last_message", ""),
                            "safe_terminal": action == "apply" and (
                                (final_state.get("apply_intent") or {}).get("phase") == "caught_rollback_complete"
                            ),
                        },
                    )
                except BaseException as exc:
                    ctx.update_command(command_id, "terminal", {"ok": False, "message": str(exc)})
                    raise
            start_background(run_claimed_command)
        else:
            start_background(target, *args, lock_acquired=reserved_lock)
        return True
    except Exception:
        release_action_slot(ctx, reserved_lock)
        raise


def command_result(ok, message="", **extra):
    payload = {"ok": bool(ok), "message": message}
    payload.update(extra)
    return payload


def _deleted_devices_transient_snapshot_fields(ctx, state):
    if state.get("deleted_devices_pending_confirmation") and state.get("deleted_devices_rollback_path"):
        pending_tree = state.get("deleted_devices_pending_tree")
        if (
            isinstance(pending_tree, dict)
            and pending_tree.get("schema") == 1
        ) or (pending_tree is None and state.get("deleted_devices_pending_tree_error")):
            return {}
        try:
            return {
                "deleted_devices_pending_tree": ctx.deleted_devices_pending_tree(state["deleted_devices_rollback_path"]),
                "deleted_devices_pending_tree_error": "",
            }
        except Exception as exc:
            return {
                "deleted_devices_pending_tree": None,
                "deleted_devices_pending_tree_error": state_store.redact_sensitive_text(str(exc)),
            }
    return {
        "deleted_devices_pending_tree": None,
        "deleted_devices_pending_tree_error": "",
    }


UI_STATE_FIELDS = frozenset({
    "last_seen_addon_version", "last_run_at", "last_status", "last_action", "last_message", "last_details",
    "last_release", "last_backup_slug", "last_diff_cursor", "last_diff_generated_at", "last_preview_commit", "last_preview_fingerprint",
    "last_preview_paths", "last_preview_conflicts", "last_preview_conflict_paths", "last_preview_live_fingerprints",
    "last_save_diff_cursor", "last_save_diff_generated_at", "last_save_preview_commit", "last_save_preview_fingerprint", "last_save_preview_paths",
    "last_save_preview_conflicts", "last_save_preview_conflict_paths", "last_save_commit_subject",
    "apply_preview_resolutions", "apply_preview_selected_paths", "save_preview_resolutions", "save_preview_selected_paths",
    "apply_preview_id", "apply_decision_revision", "save_preview_id", "save_decision_revision",
    "last_deleted_devices_rows", "last_deleted_devices_tree", "last_deleted_devices_count",
    "last_deleted_devices_device_count", "last_deleted_devices_entity_count", "last_deleted_devices_fingerprint",
    "last_deleted_devices_generated_at", "deleted_devices_pending_confirmation", "deleted_devices_pending_device_count",
    "deleted_devices_pending_entity_count", "deleted_devices_pending_tree", "deleted_devices_pending_tree_error",
    "deleted_devices_recovery_phase", "last_retained_devices_rows", "last_retained_devices_fingerprint",
    "last_retained_devices_generated_at", "last_internal_ids_rows", "last_internal_ids_preview_id", "last_internal_ids_fingerprint",
    "last_internal_ids_generated_at", "last_internal_ids_unresolved", "include_redundant_data",
    "conflicts", "conflict_type",
    "post_apply_save_recommended", "save_push_retry_pending", "operation_generation", "state_revision", "command_records",
})


def _snapshot_payload(ctx):
    if hasattr(ctx, "debug_snapshot"):
        payload = ctx.debug_snapshot()
    else:
        payload = {"state": state_store.redacted_state_snapshot(ctx.read_state())}
    raw_state = dict(payload.get("state") or {})
    raw_state.update(_deleted_devices_transient_snapshot_fields(ctx, raw_state))
    state = {key: raw_state.get(key) for key in UI_STATE_FIELDS}
    state["last_internal_ids_rows"] = [
        {key: row.get(key) for key in ("path", "changes", "unresolved", "selected", "diff_sha256")}
        for row in (raw_state.get("last_internal_ids_rows") or []) if isinstance(row, dict)
    ]
    state["last_internal_ids_unresolved"] = [
        {key: item.get(key) for key in ("path", "alias", "reason")}
        for item in (raw_state.get("last_internal_ids_unresolved") or []) if isinstance(item, dict)
    ]
    operation = raw_state.get(state_store.ACTIVE_OPERATION_KEY)
    authority_operation = ctx.read_state().get(state_store.ACTIVE_OPERATION_KEY) if isinstance(operation, dict) else None
    authority_evidence = authority_operation.get("evidence") if isinstance(authority_operation, dict) else None
    authority_evidence = authority_evidence if isinstance(authority_evidence, dict) else {}
    state["active_operation"] = (
        {"command": operation.get("command"), "command_id": operation.get("command_id"),
         "phase": operation.get("phase"), "message": operation.get("message", ""),
         "evidence_token": hashlib.sha256(json.dumps(authority_evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
         if authority_evidence else None,
         "ack_available": isinstance(authority_operation, dict) and (
             authority_operation.get("command") == "save" and authority_evidence.get("kind") == "precommit_verified"
             or authority_operation.get("command") == "apply" and authority_evidence.get("kind") == "prestate_observed"
             and authority_evidence.get("phase") in {
                 "before_backup", "before_snapshot", "before_service_commit",
             }
         ),
         "retry_available": isinstance(authority_operation, dict)
         and authority_operation.get("command") == "save"
         and authority_evidence.get("kind") == "exact_retry_available"
         and not (isinstance(authority_operation.get("retry"), dict)
                  and authority_operation["retry"].get("phase") in {"accepted", "dispatching"}),
         "evidence": {key: operation.get("evidence", {}).get(key) for key in
                      ("kind", "marked_commits", "head", "dirty", "remote_verified", "remote_tip", "parent_verified", "remote_ancestor", "service_refs_match", "guidance",
                       "affected_targets", "selected_path_count", "observed_path_count",
                       "refs_match_pre", "refs_match_intended", "optional_snapshot_recorded",
                       "optional_snapshot_available", "optional_backup_recorded", "phase")}
         if isinstance(operation.get("evidence"), dict) else None}
        if isinstance(operation, dict) else None
    )
    state["docker_build_cache_prune_fence"] = bool(raw_state.get(state_store.DOCKER_PRUNE_FENCE_KEY))
    if state["active_operation"] or state.get("last_status") == "running":
        for key in (
            "last_diff_cursor", "last_preview_paths", "last_save_diff_cursor", "last_save_preview_paths",
            "last_internal_ids_rows", "last_deleted_devices_rows", "last_retained_devices_rows",
        ):
            state[key] = None if key.endswith("_cursor") else []
    payload = {**payload, "state": state}
    options = ctx.load_options()
    display_time_fields = (
        "last_run_at", "last_diff_generated_at", "last_save_diff_generated_at",
        "last_deleted_devices_generated_at", "last_retained_devices_generated_at", "last_internal_ids_generated_at",
    )
    display_times = {
        key: ctx.format_time(raw_state.get(key), options) if hasattr(ctx, "format_time") else raw_state.get(key)
        for key in display_time_fields
    }
    try:
        addons = [
            {"slug": addon_slug_value(addon), "name": addon.get("name") or addon_slug_value(addon)}
            for addon in ctx.get_installed_addons()
        ]
    except Exception:
        addons = []
    targets = [
        {key: item.get(key) for key in ("id", "type", "source", "addon_slug", "resolved_slug", "organizer_enabled")}
        for item in current_manifest_preview(ctx)
    ]
    releases = [
        {key: release.get(key) for key in ("name", "created_at", "backup_slug")}
        for release in ctx.list_releases()[:12]
    ]
    docker_capability_status = ctx.docker_build_cache_capability() if hasattr(ctx, "docker_build_cache_capability") else {
        "available": False, "reason": _("docker_capability.unknown.reason"),
        "remedy": _("docker_capability.unknown.remedy"),
    }
    docker_fence = state_store.classify_docker_prune_fence(
        ctx.read_state().get(state_store.DOCKER_PRUNE_FENCE_KEY)
    )
    docker_recovery = {
        "kind": docker_fence.get("kind"),
        "phase": docker_fence.get("phase"),
        "operation_id": docker_fence.get("operation_id"),
        "recovery_token": docker_fence.get("recovery_token"),
    }
    return {
        **payload,
        "schema_version": 1,
        "backend_version": ctx.addon_version(),
        "text": {key: i18n.t(key) for key in i18n.EN_TEXT},
        "view": {
            "branch": str(options.get("repo_branch") or "main"),
            "manifest": str(options.get("manifest_path") or "ha-ops.json"),
            "auth_mode": ctx.git_auth_mode(options),
            "display_times": display_times,
            "targets": targets,
            "addons": addons,
            "selected_addons": ctx.selected_addon_slugs(),
            "releases": releases,
            "docker_build_cache": {
                key: docker_capability_status.get(key) for key in ("kind", "available", "reason", "remedy")
            },
            "docker_prune_recovery": docker_recovery,
        },
    }


def dispatch_command(ctx, command, body=None, start_job=None):
    body = body or {}
    def record_duplicate_rejection(action):
        recorder = getattr(ctx, "dev_harness_record_duplicate_rejection", None)
        if recorder is not None and job_is_running(ctx):
            recorder(action)

    def finalize_rejected(command_id, ok, message=None):
        if command_id and not ok:
            ctx.update_command(
                command_id,
                "terminal",
                {"ok": False, "message": message or state_store.READINESS_BLOCKED_MESSAGE},
            )

    if command == "state_get" or command == "replay":
        return command_result(True, "state snapshot", **_snapshot_payload(ctx))
    if command == "debug_snapshot":
        return command_result(True, "debug snapshot", **_snapshot_payload(ctx))
    if command == "diff_get":
        try:
            cursor = body.get("cursor")
            if isinstance(cursor, str):
                cursor = json.loads(cursor)
            diff = ctx.diff_get(cursor)
            path = body.get("path")
            if isinstance(path, list):
                path = path[0] if path else ""
            if path:
                by_path, _summary = diff_split.split_preview_diff_by_path(diff, [str(path)])
                diff = by_path.get(str(path), "")
                if not diff:
                    raise RuntimeError(_("error.diff_file_missing"))
            semantic = registry_diff.summarize_registry_diff(diff, str(path)) if path else None
            return command_result(True, "diff", diff=diff, semantic=semantic)
        except Exception as exc:
            return command_result(False, str(exc))
    if command == "internal_ids_diff_get":
        state = ctx.read_state()
        if state.get("active_operation") or state.get("last_status") == "running":
            return command_result(False, _("error.active_operation"), status=409)
        preview_id = body.get("preview_id")
        path = body.get("path")
        if preview_id != state.get("last_internal_ids_preview_id") or not preview_id or not isinstance(path, str):
            return command_result(False, _("error.internal_ids_preview_required"), status=409)
        row = next((item for item in (state.get("last_internal_ids_rows") or []) if item.get("path") == path), None)
        if not row or not row.get("diff") or not row.get("diff_sha256"):
            return command_result(False, _("error.internal_ids_preview_required"), status=409)
        return command_result(True, "diff", diff=row["diff"], diff_sha256=row["diff_sha256"])
    if command == "conflict_diff_get":
        state = ctx.read_state()
        path = body.get("path")
        if (state.get("active_operation") or state.get("last_status") == "running"
            or not isinstance(path, str) or path not in (state.get("conflicts") or [])
            or str(body.get("generation")) != str(state.get("operation_generation"))):
            return command_result(False, _("error.git_conflict_path_not_pending"), status=409)
        item = next((item for item in conflict_items(ctx, state, ctx.load_options()) if item["path"] == path), None)
        latest = ctx.read_state()
        if (item is None or latest.get("active_operation") or latest.get("last_status") == "running"
            or latest.get("operation_generation") != state.get("operation_generation")
            or path not in (latest.get("conflicts") or [])):
            return command_result(False, _("error.git_conflict_path_not_pending"), status=409)
        return command_result(True, "diff", path=path, generation=state.get("operation_generation"), diff=item["detail"])
    if command == "pending_deleted_devices_diff_get":
        try:
            state = ctx.read_state()
            if state.get(state_store.ACTIVE_OPERATION_KEY) or state.get("last_status") == "running":
                raise RuntimeError(_("error.active_operation"))
            if not state.get("deleted_devices_pending_confirmation") or not state.get("deleted_devices_rollback_path"):
                raise RuntimeError(_("error.deleted_devices_cleanup_not_pending"))
            return command_result(
                True,
                "pending deleted devices diff",
                diff=ctx.deleted_devices_pending_diff(state["deleted_devices_rollback_path"]),
            )
        except Exception as exc:
            return command_result(False, str(exc))
    if command == "acknowledge_recovery":
        payload = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(payload, dict) or not body.get("command_id"):
            return command_result(False, _("error.command_envelope_required"), status=400)
        try:
            claimed, record = ctx.acknowledge_verified_recovery(
                body["command_id"], payload.get("operation_id"), body.get("generation"), payload.get("evidence_token"),
            )
            return command_result(True, _("recovery.acknowledged_short"), duplicate=not claimed,
                                  command_record=record, **_snapshot_payload(ctx))
        except Exception as exc:
            return command_result(False, str(exc), status=409, **_snapshot_payload(ctx))
    if command == "retry_interrupted_save":
        payload = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(payload, dict) or not body.get("command_id"):
            return command_result(False, _("error.command_envelope_required"), status=400)
        try:
            claimed, record = ctx.retry_interrupted_save(
                body["command_id"], payload.get("operation_id"), body.get("generation"), payload.get("evidence_token"),
            )
            recorded_result = record.get("result") if isinstance(record.get("result"), dict) else {}
            ok = bool(recorded_result.get("ok"))
            return command_result(ok, str(recorded_result.get("message") or ""), duplicate=not claimed,
                                  command_record=record, status=200 if ok else 409, **_snapshot_payload(ctx))
        except Exception as exc:
            return command_result(False, str(exc), status=409, **_snapshot_payload(ctx))
    if command in WS_MUTATING_COMMANDS:
        envelope_payload = body.get("payload", {})
        command_id = body.get("command_id")
        generation = body.get("generation")
        if command_id is not None:
            try:
                def validate_preview_at_claim(current):
                    if command in {"save", "apply", "select_save_preview", "select_apply_preview", "resolve_save_preview", "resolve_apply_preview"}:
                        direction = "save" if "save" in command else "apply"
                        assert_preview_decision_identity(current, direction, envelope_payload)
                        if command in {"save", "apply"} and envelope_payload.get("decision_digest") != preview_decision_digest(current, direction):
                            raise StalePreviewDecision(_("error.preview_stale_decision"))
                    if command == "internal_ids_migrate":
                        selected = envelope_payload.get("selected")
                        rows = {row.get("path"): row for row in current.get("last_internal_ids_rows") or []
                                if row.get("changes") and row.get("diff") and row.get("selected")}
                        paths = [item.get("path") for item in selected if isinstance(item, dict)] if isinstance(selected, list) else []
                        if (not envelope_payload.get("preview_id")
                            or envelope_payload.get("preview_id") != current.get("last_internal_ids_preview_id")
                            or not selected or len(paths) != len(selected) or len(set(paths)) != len(paths)
                            or any(path not in rows or item.get("diff_sha256") != rows[path].get("diff_sha256")
                                   for path, item in zip(paths, selected))):
                            raise StalePreviewDecision(_("error.internal_ids_preview_required"))
                    if command == "select_internal_ids":
                        preview_id = envelope_payload.get("preview_id")
                        path = envelope_payload.get("path")
                        digest = envelope_payload.get("diff_sha256")
                        selected = envelope_payload.get("selected")
                        rows = current.get("last_internal_ids_rows") or []
                        if (not isinstance(preview_id, str) or preview_id != current.get("last_internal_ids_preview_id")
                            or not isinstance(path, str) or not isinstance(digest, str)
                            or not isinstance(selected, bool)):
                            raise StalePreviewDecision(_("error.internal_ids_preview_required"))
                        row = next((item for item in rows if item.get("path") == path), None)
                        if not row or not row.get("changes") or not row.get("diff") or row.get("diff_sha256") != digest:
                            raise StalePreviewDecision(_("error.internal_ids_preview_required"))
                        return {"last_internal_ids_rows": [
                            {**item, "selected": selected} if item.get("path") == path else item
                            for item in rows
                        ]}
                    if command == "select_retained_device":
                        if not retained_preview_identity_matches_state(current, envelope_payload):
                            raise StalePreviewDecision(_("error.retained_devices_preview_changed"))
                        identity = envelope_payload.get("identity")
                        selected = envelope_payload.get("selected")
                        rows = current.get("last_retained_devices_rows") or []
                        row = next((item for item in rows if item.get("identity") == identity), None)
                        if not isinstance(identity, str) or not identity or not isinstance(selected, bool) or not row or not row.get("retained_topics"):
                            raise StalePreviewDecision(_("error.retained_devices_preview_changed"))
                        return {"last_retained_devices_rows": [
                            {**item, "selected": selected} if item.get("identity") == identity else item
                            for item in rows
                        ]}
                    if command == "retained_devices_delete":
                        if not retained_preview_identity_matches_state(current, envelope_payload):
                            raise StalePreviewDecision(_("error.retained_devices_preview_changed"))
                        submitted = envelope_payload.get("candidate")
                        selected = [item.get("identity") for item in current.get("last_retained_devices_rows") or [] if item.get("selected")]
                        if not isinstance(submitted, list) or len(set(submitted)) != len(submitted) or set(submitted) != set(selected) or not selected:
                            raise StalePreviewDecision(_("error.retained_devices_preview_changed"))
                claimed, record = ctx.claim_command(
                    command_id, command, generation, envelope_payload,
                    validate=validate_preview_at_claim, immediate=command in {"select_internal_ids", "select_retained_device"},
                )
            except Exception as exc:
                record_duplicate_rejection(command)
                return command_result(False, str(exc), status=409, **_snapshot_payload(ctx))
            if not claimed:
                recorded_result = record.get("result") if isinstance(record.get("result"), dict) else None
                return command_result(
                    bool(recorded_result.get("ok")) if recorded_result else record.get("status") in {"accepted", "running"},
                    str(recorded_result.get("message")) if recorded_result else _("message.duplicate_command"),
                    duplicate=True,
                    command_record=record,
                )
        else:
            return command_result(False, "command_id is required", status=400)
    if command in {"resolve_save_preview", "resolve_apply_preview", "select_save_preview", "select_apply_preview"}:
        direction = "save" if command.endswith("save_preview") else "apply"
        action = "resolve" if command.startswith("resolve_") else "select"
        result = mutate_preview_decision(ctx, direction, action, envelope_payload)
        if command_id:
            ctx.update_command(
                command_id,
                "terminal",
                {"ok": bool(result.get("ok")), "message": str(result.get("message", ""))},
            )
        return result
    if command in {"select_internal_ids", "select_retained_device"}:
        return command_result(True, "Selection updated.", **_snapshot_payload(ctx))
    if command == "preview":
        if start_job is None:
            ok = start_reserved_background(
                ctx, ctx.run_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES, command_id=command_id
            )
        else:
            ok = start_job(ctx.run_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES, command_id=command_id)
        if not ok:
            record_duplicate_rejection("preview")
        finalize_rejected(command_id, ok)
        return command_result(ok, _("message.apply_preview_started") if ok else state_store.READINESS_BLOCKED_MESSAGE)
    if command == "save_preview":
        if start_job is None:
            ok = start_reserved_background(
                ctx, ctx.run_save_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES, command_id=command_id
            )
        else:
            ok = start_job(ctx.run_save_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES, command_id=command_id)
        if not ok:
            record_duplicate_rejection("save_preview")
        finalize_rejected(command_id, ok)
        return command_result(ok, _("message.save_preview_started") if ok else state_store.READINESS_BLOCKED_MESSAGE)
    if command == "apply":
        if start_job is None:
            ok = start_reserved_background(ctx, ctx.run_apply_job, command_id=command_id)
        else:
            ok = start_job(ctx.run_apply_job, command_id=command_id)
        finalize_rejected(command_id, ok)
        return command_result(ok, _("message.apply_started") if ok else state_store.READINESS_BLOCKED_MESSAGE)
    if command == "save":
        raw_subject = envelope_payload.get("commit_subject", [None])
        raw_default = envelope_payload.get("default_commit_subject", [None])
        commit_subject = raw_subject[0] if isinstance(raw_subject, list) else raw_subject
        default_subject = raw_default[0] if isinstance(raw_default, list) else raw_default
        commit_subject = job_logic.save_commit_subject_from_submission(commit_subject, default_subject)
        if start_job is None:
            ok = start_reserved_background(ctx, ctx.run_save_job, commit_subject, command_id=command_id)
        else:
            ok = start_job(ctx.run_save_job, commit_subject, command_id=command_id)
        finalize_rejected(command_id, ok)
        return command_result(ok, _("message.save_started") if ok else state_store.READINESS_BLOCKED_MESSAGE)
    job_commands = {
        "reset_git_state": (ctx.run_reset_git_state_job, [], state_store.ALL_PREVIEW_CLEAR_UPDATES, "message.git_state_reset_started"),
        "disk_usage": (ctx.run_disk_usage_job, [], None, "message.disk_usage_started"),
        "deleted_devices_preview": (ctx.run_deleted_devices_preview_job, [], state_store.ALL_PREVIEW_CLEAR_UPDATES, "message.deleted_devices_check_started"),
        "retained_devices_preview": (ctx.run_retained_devices_preview_job, [], state_store.ALL_PREVIEW_CLEAR_UPDATES, "message.retained_devices_check_started"),
        "retained_devices_delete": (ctx.run_retained_devices_delete_job, [envelope_payload], None, "message.retained_devices_delete_started"),
        "internal_ids_preview": (ctx.run_internal_ids_preview_job, [], state_store.ALL_PREVIEW_CLEAR_UPDATES, "message.internal_ids_check_started"),
        "internal_ids_migrate": (ctx.run_internal_ids_migrate_job, [envelope_payload], None, "message.internal_ids_migration_started"),
        "deleted_devices_delete": (ctx.run_deleted_devices_delete_job, [], None, "message.deleted_devices_delete_started"),
        "deleted_devices_confirm": (ctx.run_deleted_devices_confirm_job, [], None, "message.deleted_devices_cleanup_confirm_started"),
        "deleted_devices_revert": (ctx.run_deleted_devices_revert_job, [], None, "message.deleted_devices_cleanup_revert_started"),
        "rollback": (ctx.run_rollback_job, [envelope_payload.get("release", "")], None, "message.rollback_started"),
    }
    if command in job_commands:
        state = ctx.read_state()
        if not state_store.cleanup_action_allowed(state, command):
            message = job_logic.cleanup_blocked_message(state, command)
            finalize_rejected(command_id, False, message)
            return command_result(False, message, status=409)
        if (
            command == "retained_devices_delete"
            and not job_is_running(ctx)
            and not retained_preview_identity_matches_state(ctx.read_state(), envelope_payload)
        ):
            return command_result(False, _("error.retained_devices_preview_changed"), status=409)
        target, args, state_updates, message_key = job_commands[command]
        if start_job is None:
            ok = start_reserved_background(ctx, target, *args, state_updates=state_updates, command_id=command_id)
        else:
            ok = start_job(target, *args, state_updates=state_updates, command_id=command_id)
        finalize_rejected(command_id, ok)
        return command_result(ok, _("message.command_accepted") if ok else state_store.READINESS_BLOCKED_MESSAGE)
    return command_result(False, "unknown command")


def ingress_route(path, *endpoints):
    if path in endpoints:
        return path
    for endpoint in endpoints:
        if path.endswith(endpoint) and path[: -len(endpoint)]:
            return endpoint
    return path


GET_ENDPOINTS = ("/health", "/api/v1/state", "/debug-snapshot", "/diff-get", "/internal-ids-diff-get", "/conflict-diff-get", "/pending-deleted-devices-diff-get", "/ws", "/__dev_harness__/diagnostics")

POST_ENDPOINTS = (
    "/generate-key",
    "/clear-display-state",
    "/clear-preview",
    "/apply",
    "/save",
    "/preview",
    "/save-preview",
    "/resolve-save-preview",
    "/resolve-apply-preview",
    "/select-save-preview",
    "/select-apply-preview",
    "/reset-git-state",
    "/disk-usage",
    "/docker-build-cache-prune",
    "/docker-build-cache-prune-resolve",
    "/deleted-devices-preview",
    "/retained-devices-preview",
    "/retained-devices-delete",
    "/select-retained-device",
    "/internal-ids-preview",
    "/internal-ids-migrate",
    "/select-internal-ids",
    "/acknowledge-recovery",
    "/retry-interrupted-save",
    "/deleted-devices-delete",
    "/deleted-devices-confirm",
    "/deleted-devices-revert",
    "/approve-save-conflicts",
    "/addons",
    "/homeassistant-organizer",
    "/include-redundant-data",
    "/resolve-conflict",
    "/rollback",
    "/__dev_harness__/arm",
    "/__dev_harness__/release",
    "/__dev_harness__/clear-previews",
    "/__dev_harness__/seed-registry-preview",
    "/__dev_harness__/replace-retained-preview",
    "/__dev_harness__/backend-version",
)


def ws_state_frames(ctx, base_revision=None):
    snapshot = _snapshot_payload(ctx)
    state = snapshot.get("state", {})
    revision = int(state.get("state_revision") or 0)
    if base_revision is not None and revision > int(base_revision):
        return [{
            "type": "state_patch",
            "base_revision": int(base_revision),
            "revision": revision,
            "patch": state,
            "readiness": snapshot.get("readiness", {}),
            "backend_version": snapshot.get("backend_version"),
            "schema_version": snapshot.get("schema_version"),
            "view": snapshot.get("view"),
            "text": snapshot.get("text"),
        }]
    return [{"type": "state", "revision": revision, **snapshot}]


def websocket_accept(key):
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def read_ws_frame(rfile):
    header = rfile.read(2)
    if len(header) < 2:
        return None
    first, second = header
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", rfile.read(8))[0]
    mask = rfile.read(4) if second & 0x80 else b""
    payload = rfile.read(length)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if opcode == 8:
        return None
    return payload.decode("utf-8", errors="replace")


def write_ws_frame(wfile, payload):
    data = json.dumps(payload).encode("utf-8")
    if len(data) < 126:
        header = bytes([0x81, len(data)])
    elif len(data) < 65536:
        header = bytes([0x81, 126]) + struct.pack("!H", len(data))
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", len(data))
    wfile.write(header + data)
    wfile.flush()


def create_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        def send_html(self, content, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))

        def send_json(self, payload, status=200):
            command_id = getattr(self, "active_command_id", None)
            if command_id and not getattr(self, "command_scheduled", False) and isinstance(payload, dict):
                ctx.update_command(
                    command_id,
                    "terminal",
                    {"ok": bool(payload.get("ok", status < 400)), "message": str(payload.get("message", ""))},
                )
                self.active_command_id = None
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def wants_json(self):
            if getattr(self, "_force_json", False):
                return True
            accept = self.headers.get("Accept", "")
            requested_with = self.headers.get("X-Requested-With", "")
            return "application/json" in accept or requested_with == "fetch"

        def send_running_action(self):
            message = _("error.running_action")
            if self.wants_json():
                self.send_json({"ok": False, "message": message}, status=409)
            else:
                self.send_html(render_page(ctx), status=409)

        def send_recovery_blocked(self, action=None):
            state = ctx.read_state()
            action = action or (job_action(getattr(self, "blocked_target", None)) if getattr(self, "blocked_target", None) else "mutation")
            message = job_logic.cleanup_blocked_message(state, action)
            if self.wants_json():
                self.send_json({"ok": False, "message": message}, status=409)
            else:
                self.send_html(render_page(ctx), status=409)

        def send_startup_repair_blocked(self):
            message = state_store.READINESS_BLOCKED_MESSAGE
            if self.wants_json():
                self.send_json({"ok": False, "message": message}, status=409)
            else:
                self.send_html(render_page(ctx), status=409)

        def save_retry_pending(self):
            return bool(ctx.read_state().get("save_push_retry_pending"))

        def send_save_retry_pending(self):
            message = _("message.save_push_retry_still_pending")
            if self.wants_json():
                self.send_json({"ok": False, "message": message}, status=409)
            else:
                self.send_html(render_page(ctx), status=409)

        def start_job(self, target, *args, state_updates=None, lock_acquired=False, command_id=None):
            action = job_action(target)
            command_id = command_id or getattr(self, "active_command_id", None)
            if start_reserved_background(
                ctx,
                target,
                *args,
                state_updates=state_updates,
                lock_acquired=lock_acquired,
                command_id=command_id,
            ):
                self.command_scheduled = True
                return True
            readiness = ctx.readiness_snapshot() if hasattr(ctx, "readiness_snapshot") else {"status": state_store.READINESS_REPAIRED}
            if action in PREVIEW_CONSUMING_ACTIONS and readiness.get("status") != state_store.READINESS_REPAIRED:
                self.send_startup_repair_blocked()
                return False
            if not recovery_action_allowed(ctx, action):
                self.blocked_target = target
                self.send_recovery_blocked()
                self.blocked_target = None
            else:
                recorder = getattr(ctx, "dev_harness_record_duplicate_rejection", None)
                if recorder is not None and job_is_running(ctx):
                    recorder(action)
                self.send_running_action()
            return False

        def do_GET(self):
            parsed = urlparse(self.path)
            route = ingress_route(parsed.path, *GET_ENDPOINTS)
            dev_harness_get = getattr(ctx, "dev_harness_handle_get", None)
            if dev_harness_get is not None:
                result = dev_harness_get(route, parsed)
                if result is not None:
                    self.send_json(result, status=200 if result.get("ok", True) else int(result.get("status", 409)))
                    return
            if route.startswith("/__dev_harness__/"):
                self.send_json({"ok": False, "message": _("error.not_found")}, status=404)
                return
            if route == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return
            if parsed.path.endswith("/assets/ha-ops.js") or parsed.path.endswith("/assets/ha-ops.css"):
                filename = "ha-ops.css" if parsed.path.endswith(".css") else "ha-ops.js"
                asset = Path(__file__).parent / "static" / filename
                try:
                    content = asset.read_bytes()
                except OSError:
                    self.send_json({"ok": False, "message": _("error.asset_not_found")}, status=404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8" if filename.endswith(".css") else "text/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
                return
            if route in {"/api/v1/state", "/debug-snapshot"}:
                self.send_json(dispatch_command(ctx, "debug_snapshot"))
                return
            if route == "/diff-get":
                query = parse_qs(parsed.query)
                cursor = query.get("cursor", [""])[0]
                path = query.get("path", [""])[0]
                result = dispatch_command(ctx, "diff_get", {"cursor": cursor, "path": path})
                self.send_json(result, status=200 if result.get("ok") else 409)
                return
            if route == "/internal-ids-diff-get":
                query = parse_qs(parsed.query)
                result = dispatch_command(ctx, "internal_ids_diff_get", {
                    "preview_id": query.get("preview_id", [""])[0], "path": query.get("path", [""])[0],
                })
                self.send_json(result, status=200 if result.get("ok") else 409)
                return
            if route == "/conflict-diff-get":
                query = parse_qs(parsed.query)
                result = dispatch_command(ctx, "conflict_diff_get", {
                    "generation": query.get("generation", [""])[0], "path": query.get("path", [""])[0],
                })
                self.send_json(result, status=200 if result.get("ok") else 409)
                return
            if route == "/pending-deleted-devices-diff-get":
                result = dispatch_command(ctx, "pending_deleted_devices_diff_get")
                self.send_json(result, status=200 if result.get("ok") else 409)
                return
            if route == "/ws":
                key = self.headers.get("Sec-WebSocket-Key")
                if not key:
                    self.send_json({"ok": False, "message": _("error.missing_websocket_key")}, status=400)
                    return
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", websocket_accept(key))
                self.end_headers()
                last_sequence = ctx.state_change_sequence() if hasattr(ctx, "state_change_sequence") else 0
                replay_recorder = getattr(ctx, "dev_harness_record_ws_replay", None)
                if replay_recorder is not None:
                    replay_recorder()
                write_ws_frame(self.wfile, {"type": "ready", **dispatch_command(ctx, "replay")})
                last_revision = int(_snapshot_payload(ctx).get("state", {}).get("state_revision") or 0)
                while True:
                    try:
                        if getattr(self, "connection", None) is not None:
                            readable, writable_ready, exceptional_ready = select.select([self.connection], [], [], 0.5)
                            if not readable:
                                raise socket.timeout()
                        message = read_ws_frame(self.rfile)
                    except (socket.timeout, TimeoutError):
                        next_sequence = (
                            ctx.wait_for_state_change(last_sequence, timeout=0)
                            if hasattr(ctx, "wait_for_state_change")
                            else last_sequence
                        )
                        if next_sequence != last_sequence:
                            last_sequence = next_sequence
                            for frame in ws_state_frames(ctx, base_revision=last_revision):
                                write_ws_frame(self.wfile, frame)
                                last_revision = int(frame.get("revision") or last_revision)
                        continue
                    if message is None:
                        return
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError as exc:
                        write_ws_frame(self.wfile, {"type": "result", "ok": False, "message": str(exc)})
                        continue
                    command = payload.get("command") or payload.get("type")
                    result = dispatch_command(ctx, command, payload)
                    write_ws_frame(
                        self.wfile,
                        {
                            "id": payload.get("id"),
                            "type": "result",
                            **result,
                        },
                    )
                    if command in {
                        "state_get",
                        "replay",
                        "save_preview",
                        "preview",
                        "save",
                        "apply",
                        "diff_get",
                        "resolve_save_preview",
                        "resolve_apply_preview",
                        "select_save_preview",
                        "select_apply_preview",
                        "select_internal_ids",
                        "select_retained_device",
                        "acknowledge_recovery",
                        "retry_interrupted_save",
                        "deleted_devices_preview",
                        "retained_devices_preview",
                        "retained_devices_delete",
                        "internal_ids_preview",
                        "internal_ids_migrate",
                        "deleted_devices_delete",
                        "deleted_devices_confirm",
                        "deleted_devices_revert",
                    }:
                        for frame in ws_state_frames(ctx, base_revision=last_revision):
                            write_ws_frame(self.wfile, frame)
                            last_revision = int(frame.get("revision") or last_revision)
                        last_sequence = ctx.state_change_sequence() if hasattr(ctx, "state_change_sequence") else last_sequence

            ingress_shell = re.fullmatch(r"/api/hassio_ingress/[^/]+/?", parsed.path) is not None
            if ("/api/" in parsed.path and not ingress_shell) or parsed.path.endswith(".json"):
                self.send_json({"ok": False, "message": _("error.not_found")}, status=404)
                return
            self.send_html(render_page(ctx))

        def do_POST(self):
            self._force_json = True
            parsed = urlparse(self.path)
            route = ingress_route(parsed.path, *POST_ENDPOINTS)
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            if "application/json" in self.headers.get("Content-Type", ""):
                try:
                    body = json.loads(raw_body.decode()) if raw_body else {}
                except json.JSONDecodeError as exc:
                    self.send_json({"ok": False, "message": str(exc)}, status=400)
                    return
            else:
                body = parse_qs(raw_body.decode()) if raw_body else {}
            if not route.startswith("/__dev_harness__/") and (
                not isinstance(body, dict)
                or not body.get("command_id")
                or not isinstance(body.get("payload"), dict)
                or not isinstance(body.get("command"), str)
                or body.get("command") != route.removeprefix("/").replace("-", "_")
            ):
                self.send_json({"ok": False, "message": _("error.command_envelope_required")}, status=400)
                return
            if route.removeprefix("/").replace("-", "_") in WS_MUTATING_COMMANDS:
                result = dispatch_command(ctx, body["command"], body)
                status = int(result.pop("status", 200 if result.get("ok") else 409))
                self.send_json(result, status=status)
                return
            if route == "/docker-build-cache-prune":
                capability = ctx.docker_build_cache_capability()
                if not capability["available"]:
                    self.send_json({"ok": False, "message": f"{capability['reason']} {capability['remedy']}".strip()}, status=409)
                    return
            envelope_commands = {"/apply", "/save"}
            if isinstance(body, dict) and "command_id" in body and route not in envelope_commands:
                command = str(body.get("command") or route.removeprefix("/").replace("-", "_"))
                payload = body.get("payload")
                try:
                    claimed, record = ctx.claim_command(
                        body.get("command_id"),
                        command,
                        body.get("generation"),
                        payload,
                    )
                except Exception as exc:
                    self.send_json({"ok": False, "message": str(exc)}, status=409)
                    return
                if not claimed:
                    self.send_json({"ok": True, "message": _("message.duplicate_command"), "duplicate": True, "command_record": record})
                    return
                self.active_command_id = body.get("command_id")
                self.command_scheduled = False
                body = {key: value if isinstance(value, list) else [value] for key, value in payload.items()}
                if command in state_store.EFFECTFUL_COMMANDS:
                    ctx.update_command(self.active_command_id, "running")
            dev_harness_post = getattr(ctx, "dev_harness_handle_post", None)
            if dev_harness_post is not None:
                result = dev_harness_post(route, body)
                if result is not None:
                    self.send_json(result, status=200 if result.get("ok", True) else int(result.get("status", 409)))
                    return
            if route.startswith("/__dev_harness__/"):
                self.send_json({"ok": False, "message": _("error.not_found")}, status=404)
                return

            # The cleanup/recovery fence is authoritative at the HTTP boundary:
            # reject before a direct endpoint can mutate state or queue work.
            route_action = route.removeprefix("/").replace("-", "_")
            if not state_store.cleanup_action_allowed(ctx.read_state(), route_action):
                self.send_recovery_blocked(route_action)
                return
            if (
                state_store.deleted_devices_recovery_active(ctx.read_state())
                and route != "/deleted-devices-revert"
            ):
                self.send_recovery_blocked(route_action)
                return

            if route == "/generate-key":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                try:
                    public_key = ctx.generate_deploy_key()
                    ctx.write_state(
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "idle",
                            "last_action": "generate_key",
                            "last_message": _("message.generated_deploy_key"),
                            "last_details": [public_key],
                        }
                    )
                    ctx.log("Generate Deploy Key completed successfully")
                    if self.wants_json():
                        self.send_json(
                            {
                                "ok": True,
                                "message": _("message.generated_deploy_key_reload"),
                                "public_key": public_key,
                            }
                        )
                        return
                except Exception as exc:
                    ctx.log(f"Generate Deploy Key failed: {exc}")
                    ctx.write_state(
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "error",
                            "last_action": "generate_key",
                            "last_message": str(exc),
                            "last_details": [str(exc)],
                        }
                    )
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=500)
                        return
                self.send_html(render_page(ctx))
                return

            if route == "/clear-display-state":
                ctx.clear_display_state()
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.display_state_cleared")})
                else:
                    self.send_response(204)
                    self.end_headers()
                return

            if route == "/clear-preview":
                direction = body.get("direction", [""])[0]
                if self.save_retry_pending() and direction != "save":
                    self.send_save_retry_pending()
                    return
                ok, _state, lock_acquired = reserve_mutation_slot(ctx, "clear_preview")
                if not ok:
                    self.send_running_action()
                    return
                try:
                    if direction == "save":
                        state = ctx.read_state()
                        try:
                            if state.get("save_push_retry_pending"):
                                ctx.discard_save_push_retry_commit(state)
                            ctx.write_state(
                                state_store.save_preview_clear_updates(
                                    clear_save_retry_pending=bool(state.get("save_push_retry_pending"))
                                )
                            )
                        except RuntimeError as exc:
                            if self.wants_json():
                                self.send_json({"ok": False, "message": str(exc)}, status=409)
                            else:
                                self.send_html(render_page(ctx), status=409)
                            return
                        message = _("message.save_preview_cancelled")
                    elif direction == "apply":
                        ctx.write_state(state_store.APPLY_PREVIEW_CLEAR_UPDATES)
                        message = _("message.apply_preview_cancelled")
                    elif direction == "retained":
                        ctx.write_state(state_store.RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
                        message = _("message.retained_devices_preview_cancelled")
                    else:
                        if self.wants_json():
                            self.send_json({"ok": False, "message": _("error.invalid_preview_direction")}, status=400)
                        else:
                            self.send_html(render_page(ctx), status=400)
                        return
                finally:
                    release_action_slot(ctx, lock_acquired)
                if self.wants_json():
                    self.send_json({"ok": True, "message": message})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/apply":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                result = dispatch_command(ctx, "apply", body, self.start_job)
                if not result.get("ok"):
                    return
                if self.wants_json():
                    self.send_json(result)
                else:
                    self.send_html(render_page(ctx))
                return

            if route in {"/resolve-save-preview", "/resolve-apply-preview"}:
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                direction = "save" if route == "/resolve-save-preview" else "apply"
                result = mutate_preview_decision(ctx, direction, "resolve", body)
                status = int(result.pop("status", 200 if result.get("ok") else 400))
                if self.wants_json():
                    self.send_json(result, status=status)
                else:
                    self.send_html(render_page(ctx), status=status)
                return

            if route == "/preview":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.apply_preview_started")})
                    return
                else:
                    self.send_html(render_page(ctx))
                    return

            if route in {"/select-save-preview", "/select-apply-preview"}:
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                direction = "save" if route == "/select-save-preview" else "apply"
                result = mutate_preview_decision(ctx, direction, "select", body)
                status = int(result.pop("status", 200 if result.get("ok") else 400))
                if self.wants_json():
                    self.send_json(result, status=status)
                else:
                    self.send_html(render_page(ctx), status=status)
                return

            if route == "/save-preview":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_save_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.save_preview_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/reset-git-state":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_reset_git_state_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.git_state_reset_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/disk-usage":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_disk_usage_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.disk_usage_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/docker-build-cache-prune":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                capability = ctx.docker_build_cache_capability()
                if not capability["available"]:
                    message = f"{capability['reason']} {capability['remedy']}".strip()
                    if self.wants_json():
                        self.send_json({"ok": False, "message": message}, status=409)
                    else:
                        self.send_html(render_page(ctx), status=409)
                    return
                ok, state, lock_acquired = reserve_action_slot(ctx, "docker_build_cache_prune")
                if not ok:
                    if state is not None and not action_allowed_in_state(state, "docker_build_cache_prune"):
                        self.send_recovery_blocked("docker_build_cache_prune")
                    else:
                        self.send_running_action()
                    return
                operation_id = str(uuid.uuid4())
                transferred = False
                try:
                    fence = ctx.classify_docker_prune_fence(state)
                    if fence.get("kind") != "idle":
                        self.send_running_action()
                        return
                    ctx.write_state(
                        {
                            state_store.DOCKER_PRUNE_FENCE_KEY: state_store.new_docker_prune_fence(
                                operation_id, ctx.utc_now()
                            ),
                            "last_run_at": ctx.utc_now(),
                            "last_status": "running",
                            "last_action": "docker_build_cache_prune",
                            "last_message": _("message.docker_prune_accepted"),
                        }
                    )
                    try:
                        command_id = getattr(self, "active_command_id", None)
                        def run_claimed_prune():
                            if command_id:
                                ctx.update_command(command_id, "running")
                            try:
                                ctx.run_docker_build_cache_prune_job(operation_id, lock_acquired=True)
                                final_state = ctx.read_state()
                                if command_id:
                                    ctx.update_command(command_id, "terminal", {
                                        "ok": final_state.get("last_status") == "success",
                                        "message": final_state.get("last_message", ""),
                                    })
                            except BaseException as exc:
                                if command_id:
                                    ctx.update_command(command_id, "terminal", {"ok": False, "message": str(exc)})
                                raise
                        start_background(run_claimed_prune)
                        self.command_scheduled = True
                        transferred = True
                    except Exception as exc:
                        ctx.transition_docker_prune_fence(
                            operation_id,
                            {"accepted"},
                            "resolution_required",
                            {"context": _("message.docker_prune_thread_failed"), "error": str(exc)[:2000]},
                        )
                        raise
                except Exception as exc:
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=500)
                    else:
                        self.send_html(render_page(ctx), status=500)
                    return
                finally:
                    if lock_acquired and not transferred:
                        release_action_slot(ctx, True)
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.docker_prune_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/docker-build-cache-prune-resolve":
                ok, state, lock_acquired = reserve_action_slot(ctx, "docker_build_cache_prune_resolve")
                if not ok:
                    self.send_running_action()
                    return
                try:
                    mode = body.get("mode", [""])[0]
                    identity = (
                        body.get("operation_id", [""])[0]
                        if mode == "operation"
                        else body.get("recovery_token", [""])[0]
                    )
                    cleared = ctx.clear_docker_prune_fence(
                        mode,
                        identity,
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "idle",
                            "last_action": "docker_build_cache_prune_resolve",
                            "last_message": _("message.docker_prune_acknowledged"),
                            "last_details": [],
                        },
                    )
                    if cleared is None:
                        if self.wants_json():
                            self.send_json({"ok": False, "message": _("message.docker_prune_acknowledgement_stale")}, status=409)
                        else:
                            self.send_html(render_page(ctx), status=409)
                        return
                finally:
                    release_action_slot(ctx, lock_acquired)
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.docker_prune_acknowledged")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/save":
                result = dispatch_command(ctx, "save", body, self.start_job)
                if not result.get("ok"):
                    return
                if self.wants_json():
                    self.send_json(result)
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/deleted-devices-preview":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_deleted_devices_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/retained-devices-preview":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_retained_devices_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.retained_devices_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/retained-devices-delete":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not job_is_running(ctx) and not retained_preview_identity_matches_state(ctx.read_state(), body):
                    self.send_json({"ok": False, "message": _("error.retained_devices_preview_changed")}, status=409)
                    return
                if not self.start_job(ctx.run_retained_devices_delete_job, body):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.retained_devices_delete_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/internal-ids-preview":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                if not self.start_job(ctx.run_internal_ids_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.internal_ids_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/internal-ids-migrate":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                selected = body.get("candidate", [])
                if not self.start_job(ctx.run_internal_ids_migrate_job, selected):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.internal_ids_migration_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/deleted-devices-delete":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                entries = deleted_entries_label_from_state(ctx.read_state())
                if not self.start_job(ctx.run_deleted_devices_delete_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_delete_started", entries=entries)})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/deleted-devices-confirm":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                entries = deleted_entries_label_from_state(ctx.read_state(), pending=True)
                if not self.start_job(ctx.run_deleted_devices_confirm_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_cleanup_confirm_started", entries=entries)})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/deleted-devices-revert":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                entries = deleted_entries_label_from_state(ctx.read_state(), pending=True)
                if not self.start_job(ctx.run_deleted_devices_revert_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_cleanup_revert_started", entries=entries)})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/approve-save-conflicts":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                ok, _state, lock_acquired = reserve_mutation_slot(ctx, "approve_save_conflicts")
                if not ok:
                    self.send_running_action()
                    return
                try:
                    message = conflict_logic.approve_save_unknown_base_conflicts(ctx)
                    if not self.start_job(ctx.run_save_job, lock_acquired=lock_acquired):
                        # start_job consumed a pre-reserved lock on rejection.
                        lock_acquired = False
                        return
                    lock_acquired = False
                    if self.wants_json():
                        self.send_json({"ok": True, "message": _("message.approve_save_conflicts_saving", message=message)})
                    else:
                        self.send_html(render_page(ctx))
                    return
                except Exception as exc:
                    ctx.write_state(
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "error",
                            "last_action": "approve_save_conflicts",
                            "last_message": str(exc),
                            "last_details": [str(exc)],
                        }
                    )
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=500)
                    else:
                        self.send_html(render_page(ctx), status=500)
                    return
                finally:
                    release_action_slot(ctx, lock_acquired)

            if route == "/addons":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                selected = body.get("addon", [])
                ctx.set_selected_addon_slugs(selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.addons_updated")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/homeassistant-organizer":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                enabled = "homeassistant_organizer" in body
                if enabled and not manifest_logic.ORGANIZER_PROJECTION_AVAILABLE:
                    message = _("message.homeassistant_organizer_blocked")
                    if self.wants_json():
                        self.send_json({"ok": False, "message": message}, status=400)
                    else:
                        self.send_html(render_page(ctx), status=400)
                    return
                ctx.set_homeassistant_organizer_enabled(enabled)
                if self.wants_json():
                    message = _("message.homeassistant_layout_updated")
                    self.send_json({"ok": True, "message": message})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/include-redundant-data":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                ok, state, lock_acquired = reserve_mutation_slot(ctx)
                if not ok:
                    self.send_running_action()
                    return
                try:
                    enabled = "include_redundant_data" in body
                    updates = {
                        **state_store.SAVE_PREVIEW_CLEAR_UPDATES,
                        "include_redundant_data": enabled,
                    }
                    if state.get("conflict_type") == "save_unknown_base":
                        updates.update({"conflicts": [], "conflict_type": None, "save_conflict_resolutions": {}})
                    ctx.write_state(updates)
                finally:
                    release_action_slot(ctx, lock_acquired)
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.redundant_data_updated")})
                else:
                    self.send_html(render_page(ctx))
                return

            if route == "/resolve-conflict":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                ok, _state, lock_acquired = reserve_mutation_slot(ctx, "resolve_conflict")
                if not ok:
                    self.send_running_action()
                    return
                try:
                    path = body.get("path", [""])[0]
                    choice = body.get("choice", [""])[0]
                    message = conflict_logic.resolve_git_conflict(ctx, path, choice)
                    if self.wants_json():
                        self.send_json({"ok": True, "message": _("message.resolved_conflict_refreshing", message=message)})
                    else:
                        self.send_html(render_page(ctx))
                    return
                except Exception as exc:
                    ctx.write_state(
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "error",
                            "last_action": "resolve_conflict",
                            "last_message": str(exc),
                            "last_details": [str(exc)],
                        }
                    )
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=500)
                    else:
                        self.send_html(render_page(ctx), status=500)
                    return
                finally:
                    release_action_slot(ctx, lock_acquired)

            if route == "/rollback":
                if self.save_retry_pending():
                    self.send_save_retry_pending()
                    return
                release = body.get("release", [""])[0]
                if not release:
                    if self.wants_json():
                        self.send_json({"ok": False, "message": _("error.missing_release")}, status=400)
                    else:
                        self.send_error(400, _("error.missing_release"))
                    return
                if not self.start_job(ctx.run_rollback_job, release):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.rollback_started", release=release)})
                else:
                    self.send_html(render_page(ctx))
                return

            self.send_json({"ok": False, "message": _("error.not_found")}, status=404)

        def log_message(self, format, *args):
            return

    return Handler
