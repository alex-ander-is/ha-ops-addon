from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import html
import json
import threading

import conflicts as conflict_logic
import git_ops
import i18n
import manifest as manifest_logic
import state as state_store
import sync as sync_logic
import ui


def _(key, **values):
    return i18n.t(key, **values)


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


def reserve_action_slot(ctx):
    run_lock = getattr(ctx, "run_lock", None)
    if run_lock is None:
        state = ctx.read_state()
        return not state.get("last_status") == "running", state, False

    if not run_lock.acquire(blocking=False):
        return False, None, False
    try:
        state = ctx.read_state()
        return True, state, True
    except Exception:
        run_lock.release()
        raise


def release_action_slot(ctx, lock_acquired):
    if lock_acquired:
        ctx.run_lock.release()


def reserve_mutation_slot(ctx):
    if job_is_running(ctx):
        return False, None, False
    return reserve_action_slot(ctx)


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
        lines = [
            _("message.deleted_devices_waiting"),
            "",
            f"{_('label.previous_action')}: {action_label(state.get('last_action'))}",
        ]
        if message:
            lines.append(f"{_('label.last_result')}: {message}")
        lines.extend(["", _("text.current_state")])
        try:
            cleanup = ctx.deleted_devices_cleanup_status(rollback_path)
            lines.extend(
                [
                    _("text.cleanup_removed", count=cleanup["removed"]),
                    _("text.cleanup_current", count=cleanup["current"]),
                    _("text.cleanup_added", count=cleanup["added"]),
                    _("text.cleanup_returned", count=cleanup["returned"]),
                ]
            )
        except Exception as exc:
            lines.append(_("text.rollback_status_unavailable", error=exc))
        lines.extend(
            [
                _("text.rollback_available"),
                "",
                _("notice.deleted_devices_confirm_effect"),
                _("notice.deleted_devices_revert_effect"),
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
    state = ctx.read_state()
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
    last_status = state.get("last_status", "idle")
    job_running = job_is_running(ctx, state)
    has_conflicts = bool(state.get("conflicts"))
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
    diff_text = state.get("last_diff", "")
    preview_warnings = [str(item) for item in (state.get("last_preview_warnings") or []) if str(item)]
    apply_preview_paths = [str(item) for item in (state.get("last_preview_paths") or []) if str(item)]
    apply_preview_resolutions = dict(state.get("apply_preview_resolutions") or {})
    apply_preview_selected_paths = [str(item) for item in (state.get("apply_preview_selected_paths") or []) if str(item)]
    apply_preview_conflicts = bool(state.get("last_preview_conflicts"))
    apply_preview_conflict_paths = [str(item) for item in (state.get("last_preview_conflict_paths") or []) if str(item)]
    save_preview_text = state.get("last_save_preview") or ""
    save_diff_text = state.get("last_save_diff") or ""
    save_preview_paths = [str(item) for item in (state.get("last_save_preview_paths") or []) if str(item)]
    save_preview_resolutions = dict(state.get("save_preview_resolutions") or {})
    save_preview_selected_paths = [str(item) for item in (state.get("save_preview_selected_paths") or []) if str(item)]
    save_preview_conflicts = bool(state.get("last_save_preview_conflicts"))
    save_preview_conflict_paths = [str(item) for item in (state.get("last_save_preview_conflict_paths") or []) if str(item)]
    deleted_devices_preview_text = state.get("last_deleted_devices_preview") or _("text.no_deleted_devices_preview")
    deleted_devices_rows = state.get("last_deleted_devices_rows") or []
    retained_devices_rows = state.get("last_retained_devices_rows") or []
    internal_ids_rows = state.get("last_internal_ids_rows") or []
    save_details_text = save_diff_text or save_preview_text
    save_summary_text = save_preview_text if save_diff_text and save_diff_text != save_preview_text else ""
    run_disabled = "disabled" if job_running else ""
    action_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
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
    check_deleted_devices_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
    check_disk_usage_disabled = "disabled" if run_disabled else ""
    check_retained_devices_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
    check_internal_ids_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
    deletion_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation or not deletion_ready else ""
    confirm_deletion_disabled = "disabled" if run_disabled or not deleted_devices_pending_confirmation else ""
    deleted_devices_actions_html = ""
    if deleted_devices_pending_confirmation:
        deleted_devices_actions_html = (
            "<div class='actions deletion-actions'>"
            "<div class='action-row'>"
            "<form method='post' action='deleted-devices-confirm' data-async-form='true'>"
            f"<button type='submit' class='secondary' {confirm_deletion_disabled}>{_('action.confirm_changes')}</button>"
            "</form>"
            "<form method='post' action='deleted-devices-revert' data-async-form='true' "
            f"data-confirm='{html.escape(_('confirm.deleted_devices_revert'), quote=True)}'>"
            f"<button type='submit' {confirm_deletion_disabled}>{_('action.revert_changes')}</button>"
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
            f"data-confirm='{html.escape(_('confirm.deleted_devices_delete'), quote=True)}'>"
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
            f"{ui.render_conflicts(conflict_items(ctx, state, options), state.get('conflict_type'), job_running)}"
            "</section>"
        )
    apply_preview_section_html = ""
    if state.get("last_diff_generated_at") or diff_text:
        apply_preview_warnings_html = ""
        if preview_warnings:
            warning_items = "".join(f"<li>{html.escape(item)}</li>" for item in preview_warnings)
            apply_preview_warnings_html = (
                "<div class='apply-preview-warning' role='alert'>"
                f"<strong>{_('heading.warnings')}</strong>"
                f"<ul>{warning_items}</ul>"
                "</div>"
            )
        apply_preview_section_html = (
            "<section class='card wide'>"
            f"<h2>{_('heading.apply_preview')}</h2>"
            f"<p>{_('label.generated_at')} "
            f"<span data-transient='apply-generated'>{html.escape(ctx.format_time(state.get('last_diff_generated_at'), options))}</span>"
            "</p>"
            f"{apply_preview_warnings_html}"
            f"<div data-transient='apply-preview'>"
            f"{ui.render_preview_decisions(apply_preview_paths, apply_preview_resolutions, 'apply', apply_preview_conflicts, diff_text, actions_disabled=job_running, selected_paths=apply_preview_selected_paths, required_paths=apply_preview_conflict_paths)}"
            "</div>"
            "</section>"
        )
    save_preview_section_html = ""
    if not has_conflicts and (state.get("last_save_diff_generated_at") or save_preview_text or save_diff_text):
        save_preview_section_html = (
            "<section class='card wide'>"
            f"<h2>{_('heading.save_preview')}</h2>"
            f"<p>{_('label.generated_at')} "
            f"<span data-transient='save-generated'>{html.escape(ctx.format_time(state.get('last_save_diff_generated_at'), options))}</span>"
            "</p>"
            f"<div data-transient='save-preview'>"
            f"{ui.render_preview_decisions(save_preview_paths, save_preview_resolutions, 'save', save_preview_conflicts, save_details_text, save_summary_text, job_running, selected_paths=save_preview_selected_paths, required_paths=save_preview_conflict_paths)}"
            "</div>"
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
            deleted_devices_heading = _("heading.pending_deleted_devices_diff")
            deleted_devices_generated_html = ""
            try:
                deleted_devices_preview_html = (
                    f"<p class='muted'>{_('notice.deleted_devices_pending')}</p>"
                    f"{ui.render_conflict_detail(ctx.deleted_devices_pending_diff(deleted_devices_rollback_path))}"
                )
            except Exception as exc:
                deleted_devices_preview_html = f"<p>{html.escape(_('error.pending_diff_unavailable', error=str(exc)))}</p>"
        else:
            deleted_devices_preview_html = (
                ui.render_deleted_devices_table(deleted_devices_rows)
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
        retained_delete_disabled = "disabled" if run_disabled or not retained_devices_rows else ""
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
            f"<div data-transient='retained-devices-preview'>{ui.render_retained_devices_table(retained_devices_rows)}</div>"
            "<div class='actions deletion-actions'><div class='action-row'>"
            f"<button type='submit' {retained_delete_disabled}>{_('action.delete_retained_devices')}</button>"
            "</div></div>"
            "</form>"
            "</section>"
        )

    internal_ids_section_html = ""
    if state.get("last_internal_ids_generated_at"):
        internal_ids_migrate_disabled = "disabled" if run_disabled or not any(row.get("changes") for row in internal_ids_rows) else ""
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
            "apply_preview_section_html": apply_preview_section_html,
            "save_preview_section_html": save_preview_section_html,
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
            "check_disk_usage_disabled": check_disk_usage_disabled,
            "check_retained_devices_disabled": check_retained_devices_disabled,
            "check_internal_ids_disabled": check_internal_ids_disabled,
            "deletion_disabled": deletion_disabled,
            "confirm_deletion_disabled": confirm_deletion_disabled,
            "apply_action": apply_action,
            "apply_button_text": apply_button_text,
            "apply_confirm": apply_confirm,
            "conflicts_section_html": conflicts_section_html,
            "git_auth_html": ui.render_git_auth(options, ctx.git_auth_mode, ctx.load_generated_public_key),
            "targets_html": ui.render_targets(
                target_state,
                ctx.selected_addon_slugs(),
                ctx.get_installed_addons,
                addon_slug_value,
                addon_display_name,
                ctx.addon_is_zigbee2mqtt,
            ),
            "organizer_html": ui.render_homeassistant_organizer(homeassistant_organizer_enabled),
            "include_redundant_data_html": ui.render_include_redundant_data(
                bool(state.get("include_redundant_data")),
                job_running,
            ),
            "releases_html": ui.render_releases(releases),
            "version": html.escape(ctx.addon_version()),
        }
    )


def start_background(target, *args, lock_acquired=False):
    kwargs = {"lock_acquired": True} if lock_acquired else {}
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def start_reserved_background(ctx, target, *args, state_updates=None, lock_acquired=False):
    if lock_acquired:
        ok, reserved_lock = True, True
    else:
        ok, _state, reserved_lock = reserve_action_slot(ctx)
    if not ok:
        return False
    try:
        if state_updates:
            ctx.write_state(state_updates)
        start_background(target, *args, lock_acquired=reserved_lock)
        return True
    except Exception:
        release_action_slot(ctx, reserved_lock)
        raise


def create_handler(ctx):
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

        def send_running_action(self):
            message = _("error.running_action")
            if self.wants_json():
                self.send_json({"ok": False, "message": message}, status=409)
            else:
                self.send_html(render_page(ctx), status=409)

        def start_job(self, target, *args, state_updates=None, lock_acquired=False):
            if start_reserved_background(ctx, target, *args, state_updates=state_updates, lock_acquired=lock_acquired):
                return True
            self.send_running_action()
            return False

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return

            self.send_html(render_page(ctx))

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = parse_qs(self.rfile.read(length).decode()) if length else {}

            if parsed.path == "/generate-key":
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

            if parsed.path == "/clear-display-state":
                ctx.clear_display_state()
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.display_state_cleared")})
                else:
                    self.send_response(204)
                    self.end_headers()
                return

            if parsed.path == "/clear-preview":
                direction = body.get("direction", [""])[0]
                ok, _state, lock_acquired = reserve_mutation_slot(ctx)
                if not ok:
                    self.send_running_action()
                    return
                try:
                    if direction == "save":
                        ctx.write_state(state_store.SAVE_PREVIEW_CLEAR_UPDATES)
                        message = _("message.save_preview_cancelled")
                    elif direction == "apply":
                        ctx.write_state(state_store.APPLY_PREVIEW_CLEAR_UPDATES)
                        message = _("message.apply_preview_cancelled")
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

            if parsed.path == "/apply":
                if not self.start_job(ctx.run_apply_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.apply_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path in {"/resolve-save-preview", "/resolve-apply-preview"}:
                direction = "save" if parsed.path == "/resolve-save-preview" else "apply"
                ok, state, lock_acquired = reserve_mutation_slot(ctx)
                if not ok:
                    self.send_running_action()
                    return
                raw_path = body.get("path", [""])[0]
                choice = body.get("choice", [""])[0]
                try:
                    safe_path = git_ops.safe_repo_relative_path(raw_path)
                    if choice not in {"ha", "git"}:
                        raise RuntimeError(_("error.invalid_preview_choice"))
                    paths_key = "last_save_preview_paths" if direction == "save" else "last_preview_paths"
                    resolutions_key = "save_preview_resolutions" if direction == "save" else "apply_preview_resolutions"
                    conflict_paths_key = "last_save_preview_conflict_paths" if direction == "save" else "last_preview_conflict_paths"
                    paths = [str(item) for item in (state.get(paths_key) or []) if str(item)]
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
                    if self.wants_json():
                        self.send_json({"ok": True, "message": ctx.read_state().get("last_message", "")})
                    else:
                        self.send_html(render_page(ctx))
                    return
                except Exception as exc:
                    ctx.write_state(
                        {
                            "last_run_at": ctx.utc_now(),
                            "last_status": "error",
                            "last_action": f"resolve_{direction}_preview",
                            "last_message": str(exc),
                            "last_details": [str(exc)],
                        }
                    )
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=400)
                    else:
                        self.send_html(render_page(ctx), status=400)
                    return
                finally:
                    release_action_slot(ctx, lock_acquired)

            if parsed.path == "/preview":
                if not self.start_job(ctx.run_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.apply_preview_started")})
                    return
                else:
                    self.send_html(render_page(ctx))
                    return

            if parsed.path in {"/select-save-preview", "/select-apply-preview"}:
                direction = "save" if parsed.path == "/select-save-preview" else "apply"
                ok, state, lock_acquired = reserve_mutation_slot(ctx)
                if not ok:
                    self.send_running_action()
                    return
                try:
                    paths_key = "last_save_preview_paths" if direction == "save" else "last_preview_paths"
                    selected_key = "save_preview_selected_paths" if direction == "save" else "apply_preview_selected_paths"
                    paths = [str(item) for item in (state.get(paths_key) or []) if str(item)]
                    path_set = set(paths)
                    action = body.get("selection_action", [""])[0]
                    if action == "all":
                        selected = paths
                    elif action == "none":
                        selected = []
                    else:
                        raw_path = body.get("path", [""])[0]
                        safe_path = git_ops.safe_repo_relative_path(raw_path)
                        if safe_path not in path_set:
                            raise RuntimeError(_("error.preview_path_not_pending"))
                        selected_set = {str(item) for item in (state.get(selected_key) or []) if str(item) in path_set}
                        if body.get("selected", [""])[0] == "1":
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
                    if self.wants_json():
                        self.send_json({"ok": True, "message": ctx.read_state().get("last_message", "")})
                    else:
                        self.send_html(render_page(ctx))
                    return
                except Exception as exc:
                    if self.wants_json():
                        self.send_json({"ok": False, "message": str(exc)}, status=400)
                    else:
                        self.send_html(render_page(ctx), status=400)
                    return
                finally:
                    release_action_slot(ctx, lock_acquired)

            if parsed.path == "/save-preview":
                if not self.start_job(ctx.run_save_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.save_preview_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/reset-git-state":
                if not self.start_job(ctx.run_reset_git_state_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.git_state_reset_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/disk-usage":
                if not self.start_job(ctx.run_disk_usage_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.disk_usage_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/save":
                if not self.start_job(ctx.run_save_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.save_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-preview":
                if not self.start_job(ctx.run_deleted_devices_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/retained-devices-preview":
                if not self.start_job(ctx.run_retained_devices_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.retained_devices_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/retained-devices-delete":
                selected = body.get("candidate", [])
                if not self.start_job(ctx.run_retained_devices_delete_job, selected):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.retained_devices_delete_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/internal-ids-preview":
                if not self.start_job(ctx.run_internal_ids_preview_job, state_updates=state_store.ALL_PREVIEW_CLEAR_UPDATES):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.internal_ids_check_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/internal-ids-migrate":
                selected = body.get("candidate", [])
                if not self.start_job(ctx.run_internal_ids_migrate_job, selected):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.internal_ids_migration_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-delete":
                if not self.start_job(ctx.run_deleted_devices_delete_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_delete_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-confirm":
                if not self.start_job(ctx.run_deleted_devices_confirm_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_cleanup_confirm_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-revert":
                if not self.start_job(ctx.run_deleted_devices_revert_job):
                    return
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.deleted_devices_cleanup_revert_started")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/approve-save-conflicts":
                ok, _state, lock_acquired = reserve_mutation_slot(ctx)
                if not ok:
                    self.send_running_action()
                    return
                try:
                    message = conflict_logic.approve_save_unknown_base_conflicts(ctx)
                    if not self.start_job(ctx.run_save_job, lock_acquired=lock_acquired):
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

            if parsed.path == "/addons":
                selected = body.get("addon", [])
                ctx.set_selected_addon_slugs(selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": _("message.addons_updated")})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/homeassistant-organizer":
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

            if parsed.path == "/include-redundant-data":
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

            if parsed.path == "/resolve-conflict":
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

            if parsed.path == "/rollback":
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
