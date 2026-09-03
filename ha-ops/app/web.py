from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import base64
import hashlib
import html
import json
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
import state as state_store
import sync as sync_logic
import ui


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


def render_addons(ctx):
    return ui.render_addons(
        ctx.selected_addon_slugs(),
        ctx.get_installed_addons,
        addon_slug_value,
        addon_display_name,
        ctx.addon_is_zigbee2mqtt,
    )


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
        "deleted_devices_delete": _("action.approve_deleted_devices"),
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
    options = ctx.load_options()
    state, reconciled = reconcile_docker_prune_orphan(ctx)
    backup_status = ctx.latest_system_backup_status(options)
    if (
        state.get("last_status") == "error"
        and state.get("last_action") == "apply"
        and str(state.get("last_message", "")).startswith("No fresh system backup found")
        and not backup_status.get("stale", True)
    ):
        state = dict(state)
        state.update(
            {
                "last_status": "idle",
                "last_action": None,
                "last_message": _("message.fresh_system_backup_available"),
            }
        )
    elif (
        state.get("last_status") == "error"
        and state.get("last_action") == "apply"
        and str(state.get("last_message", "")) == "Home Assistant config check failed: {'result': 'ok', 'data': {}}"
    ):
        state = dict(state)
        state.update(
            {
                "last_status": "idle",
                "last_action": None,
                "last_message": _("message.stale_config_check_cleared"),
            }
        )
    releases = ctx.list_releases()
    manifest_preview = current_manifest_preview(ctx)
    target_state = state.get("last_targets") or manifest_preview
    homeassistant_organizer_enabled = any(
        target.get("type") == "homeassistant" and target.get("organizer_enabled")
        for target in manifest_preview
    )
    state = repair_stale_running_state(ctx, state)
    classify_prune = getattr(ctx, "classify_docker_prune_fence", None)
    docker_prune = (
        classify_prune(state)
        if classify_prune is not None
        else state_store.classify_docker_prune_fence(state.get(state_store.DOCKER_PRUNE_FENCE_KEY))
    )
    last_status = state.get("last_status", "idle")
    last_action = state.get("last_action")
    job_running = job_is_running(ctx, state)
    has_conflicts = bool(state.get("conflicts"))
    deleted_devices_recovery_active = state_store.deleted_devices_recovery_active(state)
    deleted_devices_pending_confirmation = bool(state.get("deleted_devices_pending_confirmation"))
    deleted_devices_rollback_path = state.get("deleted_devices_rollback_path")
    pending_deleted_devices_decision = bool(deleted_devices_pending_confirmation and deleted_devices_rollback_path)
    display_status = "conflicts" if has_conflicts else "pending decision" if pending_deleted_devices_decision else last_status
    display_status_label = _(STATUS_LABEL_KEYS.get(display_status, display_status))
    details = log_text_for_state(
        ctx,
        state,
        last_status,
        deleted_devices_pending_confirmation,
        deleted_devices_rollback_path,
    )
    save_push_retry_pending = bool(state.get("save_push_retry_pending"))
    deleted_devices_preview_text = state.get("last_deleted_devices_preview") or _("text.no_deleted_devices_preview")
    deleted_devices_rows = state.get("last_deleted_devices_rows") or []
    deleted_devices_tree = state.get("last_deleted_devices_tree")
    retained_devices_rows = state.get("last_retained_devices_rows") or []
    internal_ids_rows = state.get("last_internal_ids_rows") or []
    run_disabled = "disabled" if job_running else ""
    action_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or deleted_devices_recovery_active else ""
    apply_action = "apply"
    apply_button_text = _("action.apply")
    post_apply_save_recommended = bool(state.get("post_apply_save_recommended"))
    save_preview_button_class = "warning" if post_apply_save_recommended else "secondary"
    save_preview_button_text = _("action.review_post_apply_save") if post_apply_save_recommended else _("action.preview_save")
    save_preview_hint_html = ""
    if post_apply_save_recommended:
        save_preview_hint_html = f"<p class='action-hint'>{_('notice.post_apply_save_button')}</p>"
    post_apply_notice_html = ""
    if post_apply_save_recommended:
        post_apply_notice_html = (
            "<div class='post-apply-alert' role='alert'>"
            f"<strong>{_('notice.post_apply_save_title')}</strong>"
            f"<span>{_('notice.post_apply_save')}</span>"
            "</div>"
        )
    deleted_devices_count = int(state.get("last_deleted_devices_count") or 0)
    deletion_ready = bool(
        deleted_devices_count > 0
        and state.get("last_deleted_devices_preview")
        and state.get("last_deleted_devices_generated_at")
        and state.get("last_deleted_devices_fingerprint")
    )
    check_deleted_devices_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or deleted_devices_recovery_active else ""
    deleted_devices_save_hint_html = ""
    if last_action == "deleted_devices_confirm" and last_status == "success":
        deleted_devices_save_hint_html = (
            "<p class='action-hint deleted-devices-save-hint' role='alert'>"
            f"{_('detail.save_deleted_registry_cleanup')}"
            "</p>"
        )
    if save_push_retry_pending:
        action_disabled = "disabled"
        check_deleted_devices_disabled = "disabled"
    check_disk_usage_disabled = (
        "disabled"
        if run_disabled or save_push_retry_pending or deleted_devices_pending_confirmation or deleted_devices_recovery_active
        else ""
    )
    docker_capability_status = ctx.docker_build_cache_capability()
    docker_prune_ready = bool(
        docker_capability_status["available"]
        and not job_running
        and not save_push_retry_pending
        and not deleted_devices_pending_confirmation
        and not deleted_devices_recovery_active
        and docker_prune.get("kind") == "idle"
    )
    docker_prune_disabled = "" if docker_prune_ready else "disabled"
    docker_prune_hint_html = ""
    if not docker_capability_status["available"]:
        docker_prune_hint_html = (
            "<p class='action-hint docker-prune-hint'>"
            f"{html.escape(docker_capability_status['reason'])} {html.escape(docker_capability_status['remedy'])}</p>"
        )
    elif save_push_retry_pending:
        docker_prune_hint_html = f"<p class='action-hint docker-prune-hint'>{_('docker_prune.disabled.save_retry')}</p>"
    elif docker_prune.get("kind") != "idle":
        docker_prune_hint_html = f"<p class='action-hint docker-prune-hint'>{_('docker_prune.disabled.fence')}</p>"
    if docker_prune.get("kind") == "valid" and docker_prune.get("phase") in state_store.DOCKER_PRUNE_ACTIVE_PHASES:
        docker_prune_status_html = f"<p class='action-flow'>{_('message.docker_prune_phase_' + docker_prune['phase'])}</p>"
    elif docker_prune.get("phase") == "resolution_required":
        if docker_prune.get("kind") == "valid":
            hidden = (
                "<input type='hidden' name='mode' value='operation'>"
                f"<input type='hidden' name='operation_id' value='{html.escape(docker_prune['operation_id'], quote=True)}'>"
            )
            copy = _("message.docker_prune_ambiguity_valid")
        else:
            hidden = (
                "<input type='hidden' name='mode' value='corrupt'>"
                f"<input type='hidden' name='recovery_token' value='{html.escape(docker_prune['recovery_token'], quote=True)}'>"
            )
            copy = _("message.docker_prune_ambiguity_corrupt")
        docker_prune_status_html = (
            f"<p class='action-flow'>{copy}</p>"
            "<form method='post' action='docker-build-cache-prune-resolve' data-async-form='true'>"
            f"{hidden}<button type='submit' class='secondary'>{_('action.acknowledge_docker_prune')}</button></form>"
        )
    else:
        docker_prune_status_html = ""
    check_retained_devices_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or deleted_devices_recovery_active or save_push_retry_pending else ""
    check_internal_ids_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or deleted_devices_recovery_active or save_push_retry_pending else ""
    deletion_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or deleted_devices_recovery_active or save_push_retry_pending or not deletion_ready else ""
    confirm_deletion_disabled = (
        "disabled" if run_disabled or save_push_retry_pending or deleted_devices_recovery_active or not deleted_devices_pending_confirmation else ""
    )
    revert_deletion_disabled = (
        "disabled"
        if run_disabled or save_push_retry_pending or not deleted_devices_pending_confirmation or not deleted_devices_rollback_path
        else ""
    )
    deleted_devices_actions_html = ""
    preview_entries = deleted_entries_label(
        int(state.get("last_deleted_devices_device_count") or 0),
        int(state.get("last_deleted_devices_entity_count") or 0),
    )
    pending_entries = deleted_entries_label(
        int(state.get("deleted_devices_pending_device_count") or 0),
        int(state.get("deleted_devices_pending_entity_count") or 0),
    )
    if deleted_devices_pending_confirmation:
        deleted_devices_actions_html = (
            "<div class='actions deletion-actions'>"
            "<div class='action-row'>"
            "<form method='post' action='deleted-devices-confirm' data-async-form='true'>"
            f"<button type='submit' class='secondary' {confirm_deletion_disabled}>{_('action.confirm_changes')}</button>"
            "</form>"
            "<form method='post' action='deleted-devices-revert' data-async-form='true' "
            f"data-confirm='{html.escape(_('confirm.deleted_devices_revert', entries=pending_entries), quote=True)}'>"
            f"<button type='submit' {revert_deletion_disabled}>{_('action.revert_changes')}</button>"
            "</form>"
            "</div>"
            "</div>"
        )
    elif deletion_ready:
        deleted_devices_actions_html = (
            "<div class='actions deletion-actions'>"
            "<div class='action-row'>"
            "<form method='post' action='deleted-devices-delete' data-async-form='true' "
            "data-preserve-display-state='true' "
            f"data-confirm='{html.escape(_('confirm.deleted_devices_delete', entries=preview_entries), quote=True)}'>"
            f"<button type='submit' {deletion_disabled}>{_('action.approve_deleted_devices')}</button>"
            "</form>"
            "</div>"
            "</div>"
        )
    confirm_messages = []
    if not ctx.option_bool(options, "require_fresh_backup", True):
        confirm_messages.append(_("notice.apply_confirm_backup_disabled"))
    if ui.targets_allow_protected_storage(target_state):
        confirm_messages.append(_("notice.apply_confirm_protected_storage"))
    apply_confirm = ""
    if confirm_messages:
        confirm_message = _("confirm.apply", message=" ".join(confirm_messages))
        apply_confirm = f"data-confirm='{html.escape(confirm_message, quote=True)}'"
    conflicts_section_html = ""
    if has_conflicts:
        conflicts_section_html = (
            "<section class='card wide'>"
            f"<h2>{_('heading.git_conflicts')}</h2>"
            f"{ui.render_conflicts(conflict_items(ctx, state, options), state.get('conflict_type'), job_running or save_push_retry_pending)}"
            "</section>"
        )
    deleted_devices_section_html = ""
    if state.get("last_deleted_devices_generated_at") or deleted_devices_pending_confirmation:
        deleted_devices_heading = _("heading.deleted_devices_preview")
        deleted_devices_generated_html = (
            f"<p>{_('label.generated_at')} "
            f"<span data-transient='deleted-devices-generated'>{html.escape(ctx.format_time(state.get('last_deleted_devices_generated_at'), options))}</span>"
            "</p>"
        )
        if pending_deleted_devices_decision:
            deleted_devices_heading = _(
                "heading.pending_deleted_devices_diff",
                entries=deleted_entries_label(
                    int(state.get("deleted_devices_pending_device_count") or 0),
                    int(state.get("deleted_devices_pending_entity_count") or 0),
                ),
            )
            deleted_devices_generated_html = ""
            tree = state.get("deleted_devices_pending_tree")
            tree_error = state.get("deleted_devices_pending_tree_error") or ""
            if not tree and not tree_error:
                try:
                    tree = ctx.deleted_devices_pending_tree(deleted_devices_rollback_path)
                except Exception as exc:
                    tree_error = str(exc)
            deleted_devices_preview_html = (
                f"<p class='muted'>{_('notice.deleted_devices_pending')}</p>"
                + (ui.render_deleted_devices_tree(tree) if tree else f"<p>{html.escape(_('error.pending_diff_unavailable', error=tree_error))}</p>")
                + ui.render_pending_deleted_devices_raw_fallback()
            )
        else:
            deleted_devices_preview_html = (
                ui.render_deleted_devices_tree(deleted_devices_tree)
                if deleted_devices_tree
                else ui.render_deleted_devices_table(deleted_devices_rows)
                if state.get("last_deleted_devices_generated_at")
                else html.escape(deleted_devices_preview_text)
            )
        deleted_devices_section_html = (
            "<section class='card wide'>"
            f"<h2>{deleted_devices_heading}</h2>"
            f"{deleted_devices_generated_html}"
            f"<div data-transient='deleted-devices-preview'>{deleted_devices_preview_html}</div>"
            f"{deleted_devices_actions_html}"
            "</section>"
        )
    retained_devices_section_html = ""
    if state.get("last_retained_devices_generated_at"):
        retained_delete_disabled = (
            "disabled"
            if run_disabled
            or save_push_retry_pending
            or deleted_devices_pending_confirmation
            or deleted_devices_recovery_active
            or not retained_devices_rows
            else ""
        )
        retained_controls_disabled = bool(retained_delete_disabled)
        retained_identity_fields = (
            f"<input type='hidden' name='retained_preview_fingerprint' value='{html.escape(str(state.get('last_retained_devices_fingerprint') or ''), quote=True)}'>"
            f"<input type='hidden' name='retained_preview_generated_at' value='{html.escape(str(state.get('last_retained_devices_generated_at') or ''), quote=True)}'>"
        )
        retained_devices_section_html = (
            "<section class='card wide'>"
            f"<h2>{_('heading.retained_devices_preview')}</h2>"
            f"<p class='muted'>{_('notice.retained_devices_preview')}</p>"
            f"<p class='muted'>{_('notice.retained_devices_delete')}</p>"
            f"<p>{_('label.generated_at')} "
            f"<span data-transient='retained-devices-generated'>{html.escape(ctx.format_time(state.get('last_retained_devices_generated_at'), options))}</span>"
            "</p>"
            "<form method='post' action='retained-devices-delete' data-async-form='true' "
            "data-preserve-display-state='true' "
            f"data-confirm='{html.escape(_('confirm.retained_devices_delete'), quote=True)}'>"
            f"{retained_identity_fields}"
            f"<div data-transient='retained-devices-preview'>{ui.render_retained_devices_table(retained_devices_rows, disabled=retained_controls_disabled)}</div>"
            "<div class='actions deletion-actions'><div class='action-row'>"
            f"<button type='submit' {retained_delete_disabled}>{_('action.delete_retained_devices')}</button>"
            "</div></div>"
            "</form>"
            "</section>"
        )

    internal_ids_section_html = ""
    if state.get("last_internal_ids_generated_at"):
        internal_ids_migrate_disabled = (
            "disabled"
            if run_disabled
            or save_push_retry_pending
            or deleted_devices_pending_confirmation
            or deleted_devices_recovery_active
            or not any(row.get("changes") for row in internal_ids_rows)
            else ""
        )
        internal_ids_changed_files = sum(1 for row in internal_ids_rows if row.get("changes"))
        internal_ids_totals = {
            "changes": sum(int(row.get("changes") or 0) for row in internal_ids_rows),
            "unresolved": sum(int(row.get("unresolved") or 0) for row in internal_ids_rows),
        }
        internal_ids_summary_html = (
            "<p>"
            f"{_('label.files')}: {internal_ids_changed_files}. "
            f"{_('label.candidates')}: {internal_ids_totals['changes']}. "
            f"{_('label.unresolved')}: {internal_ids_totals['unresolved']}."
            "</p>"
        )
        internal_ids_section_html = (
            "<section class='card wide'>"
            f"<h2>{_('heading.internal_ids_preview')}</h2>"
            f"<p class='muted'>{_('notice.internal_ids_preview_scope')}</p>"
            f"<p class='muted'>{_('notice.internal_ids_preview_apply')}</p>"
            f"<p>{_('label.generated_at')} "
            f"<span data-transient='internal-ids-generated'>{html.escape(ctx.format_time(state.get('last_internal_ids_generated_at'), options))}</span>"
            "</p>"
            f"{internal_ids_summary_html}"
            "<form method='post' action='internal-ids-migrate' data-async-form='true' "
            "data-preserve-display-state='true' "
            f"data-confirm='{html.escape(_('confirm.internal_ids_migrate'), quote=True)}'>"
            f"<div data-transient='internal-ids-preview'>{ui.render_internal_ids_table(internal_ids_rows, ui.render_conflict_detail)}</div>"
            "<div class='actions deletion-actions'><div class='action-row'>"
            f"<button type='submit' {internal_ids_migrate_disabled}>{_('action.migrate_and_save')}</button>"
            "</div></div>"
            "</form>"
            "</section>"
        )

    return ui.render_page(
        {
            "status": html.escape(display_status_label),
            "status_code": html.escape(display_status, quote=True),
            "badge_class": (
                "conflicts"
                if has_conflicts
                else "pending"
                if pending_deleted_devices_decision
                else "error"
                if last_status == "error"
                else "interrupted"
                if last_status == "interrupted"
                else "running"
                if last_status == "running"
                else ""
            ),
            "last_run": html.escape(ctx.format_time(state.get("last_run_at"), options)),
            "last_release": html.escape(str(state.get("last_release"))),
            "last_backup_slug": html.escape(str(state.get("last_backup_slug"))),
            "latest_backup": html.escape(backup_status.get("message", _("text.backup_status_unavailable"))),
            "repo_url": html.escape(options.get("repo_url", "")),
            "branch": html.escape(options.get("repo_branch", "main")),
            "manifest_path": html.escape(options.get("manifest_path", "ha-ops.json")),
            "auth_mode": html.escape(ctx.git_auth_mode(options)),
            "details_html": html.escape(details),
            "deleted_devices_section_html": deleted_devices_section_html,
            "retained_devices_section_html": retained_devices_section_html,
            "internal_ids_section_html": internal_ids_section_html,
            "action_disabled": action_disabled,
            "job_running_json": "true" if job_running else "false",
            "post_apply_notice_html": post_apply_notice_html,
            "save_preview_button_class": save_preview_button_class,
            "save_preview_button_text": save_preview_button_text,
            "save_preview_hint_html": save_preview_hint_html,
            "check_deleted_devices_disabled": check_deleted_devices_disabled,
            "deleted_devices_save_hint_html": deleted_devices_save_hint_html,
            "check_disk_usage_disabled": check_disk_usage_disabled,
            "docker_prune_disabled": docker_prune_disabled,
            "docker_prune_available": "true" if docker_capability_status["available"] else "false",
            "docker_prune_ready": "true" if docker_prune_ready else "false",
            "docker_prune_hint_html": docker_prune_hint_html,
            "docker_prune_status_html": docker_prune_status_html,
            "check_retained_devices_disabled": check_retained_devices_disabled,
            "check_internal_ids_disabled": check_internal_ids_disabled,
            "deletion_disabled": deletion_disabled,
            "confirm_deletion_disabled": confirm_deletion_disabled,
            "apply_action": apply_action,
            "apply_button_text": apply_button_text,
            "apply_confirm": apply_confirm,
            "conflicts_section_html": conflicts_section_html,
            "git_auth_html": ui.render_git_auth(
                options,
                ctx.git_auth_mode,
                ctx.load_generated_public_key,
                disabled=job_running or save_push_retry_pending,
            ),
            "targets_html": ui.render_targets(
                target_state,
                ctx.selected_addon_slugs(),
                ctx.get_installed_addons,
                addon_slug_value,
                addon_display_name,
                ctx.addon_is_zigbee2mqtt,
                disabled=job_running or save_push_retry_pending,
            ),
            "organizer_html": ui.render_homeassistant_organizer(
                homeassistant_organizer_enabled,
                disabled=job_running or save_push_retry_pending,
            ),
            "include_redundant_data_html": ui.render_include_redundant_data(
                bool(state.get("include_redundant_data")),
                job_running or save_push_retry_pending,
            ),
            "releases_html": ui.render_releases(releases, disabled=job_running or save_push_retry_pending),
            "version": html.escape(ctx.addon_version()),
        }
    )


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
    "internal_ids_preview",
    "internal_ids_migrate",
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
            "commit": state.get("last_save_preview_commit"),
            "fingerprint": state.get("last_save_preview_fingerprint"),
            "paths": _canonical_list(state.get("last_save_preview_paths")),
            "conflict_paths": _canonical_list(state.get("last_save_preview_conflict_paths")),
            "diff_cursor": _cursor_identity(state.get("last_save_diff_cursor")),
        }
    return {
        "direction": "apply",
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


def _snapshot_payload(ctx):
    if hasattr(ctx, "debug_snapshot"):
        payload = ctx.debug_snapshot()
    else:
        payload = {"state": state_store.redacted_state_snapshot(ctx.read_state())}
    state = dict(payload.get("state") or {})
    state.update(_deleted_devices_transient_snapshot_fields(ctx, state))
    payload = {**payload, "state": state}
    return {**payload, "backend_version": ctx.addon_version()}


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
                by_path, _summary = ui.split_preview_diff_by_path(diff, [str(path)])
                diff = by_path.get(str(path), "")
                if not diff:
                    raise RuntimeError(_("error.diff_file_missing"))
            return command_result(True, "diff", diff=diff)
        except Exception as exc:
            return command_result(False, str(exc))
    if command == "pending_deleted_devices_diff_get":
        try:
            state = ctx.read_state()
            if not state.get("deleted_devices_pending_confirmation") or not state.get("deleted_devices_rollback_path"):
                raise RuntimeError(_("error.deleted_devices_cleanup_not_pending"))
            return command_result(
                True,
                "pending deleted devices diff",
                diff=ctx.deleted_devices_pending_diff(state["deleted_devices_rollback_path"]),
            )
        except Exception as exc:
            return command_result(False, str(exc))
    if command in WS_MUTATING_COMMANDS:
        envelope_payload = body.get("payload", {})
        command_id = body.get("command_id")
        generation = body.get("generation")
        if command_id is not None:
            try:
                claimed, record = ctx.claim_command(command_id, command, generation, envelope_payload)
            except Exception as exc:
                return command_result(False, str(exc))
            if not claimed:
                return command_result(
                    True,
                    _("message.duplicate_command"),
                    duplicate=True,
                    command_record=record,
                )
        else:
            command_id = None
            envelope_payload = body
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
        "internal_ids_migrate": (ctx.run_internal_ids_migrate_job, [envelope_payload.get("candidate", [])], None, "message.internal_ids_migration_started"),
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


GET_ENDPOINTS = ("/health", "/debug-snapshot", "/diff-get", "/pending-deleted-devices-diff-get", "/ws", "/__dev_harness__/diagnostics")

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
    "/internal-ids-preview",
    "/internal-ids-migrate",
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
                self.send_error(404)
                return
            if route == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return
            if parsed.path.endswith("/assets/ha-ops.js") or parsed.path == "/assets/ha-ops.js":
                asset = Path(__file__).parent / "static" / "ha-ops.js"
                try:
                    content = asset.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
                return
            if route == "/debug-snapshot":
                self.send_json(dispatch_command(ctx, "debug_snapshot"))
                return
            if route == "/diff-get":
                query = parse_qs(parsed.query)
                cursor = query.get("cursor", [""])[0]
                path = query.get("path", [""])[0]
                result = dispatch_command(ctx, "diff_get", {"cursor": cursor, "path": path})
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
                            readable, _, _ = select.select([self.connection], [], [], 0.5)
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

            self.send_html(render_page(ctx))

        def do_POST(self):
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
            envelope_commands = {"/preview", "/save-preview", "/apply", "/save"}
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
            dev_harness_post = getattr(ctx, "dev_harness_handle_post", None)
            if dev_harness_post is not None:
                result = dev_harness_post(route, body)
                if result is not None:
                    self.send_json(result, status=200 if result.get("ok", True) else int(result.get("status", 409)))
                    return
            if route.startswith("/__dev_harness__/"):
                self.send_error(404)
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
                ok, _state, lock_acquired = reserve_mutation_slot(ctx)
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
                        start_background(
                            ctx.run_docker_build_cache_prune_job,
                            operation_id,
                            lock_acquired=True,
                        )
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
                ok, _state, lock_acquired = reserve_mutation_slot(ctx)
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
                ok, _state, lock_acquired = reserve_mutation_slot(ctx)
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

            self.send_error(404)

        def log_message(self, format, *args):
            return

    return Handler
