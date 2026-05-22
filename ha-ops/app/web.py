from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import html
import json
import threading

import conflicts as conflict_logic
import git_ops
import manifest as manifest_logic
import state as state_store
import sync as sync_logic
import ui


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
        return f"Unable to read conflict file: {exc}"


def file_diff(ctx, left_label, left_path, right_label, right_path):
    left_path = Path(left_path)
    right_path = Path(right_path)
    if not left_path.exists():
        return f"Diff unavailable: {left_label} file is missing: {left_path}"
    if not right_path.exists():
        return f"Diff unavailable: {right_label} file is missing: {right_path}"

    result = ctx.run_command(["diff", "-u", "-L", left_label, "-L", right_label, str(left_path), str(right_path)])
    if result.returncode == 0:
        return "No differences found."
    if result.returncode == 1:
        return full_conflict_detail(result.stdout.strip())
    return f"Diff unavailable:\n{(result.stderr or result.stdout).strip()}"


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
    return f"Diff unavailable: no managed target found for {safe_path}."


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
            detail = f"Conflict detail unavailable: {exc}"
        items.append({"path": safe_path, "detail": detail})
    return items


def action_label(action):
    return {
        "apply": "Apply Git to HA",
        "preview": "Preview Git to HA",
        "save": "Save HA to Git",
        "save_preview": "Preview HA to Git",
        "deleted_devices_preview": "Check deleted_devices",
        "deleted_devices_delete": "Approve Deletion",
        "deleted_devices_confirm": "Confirm Changes",
        "deleted_devices_revert": "Revert Changes",
        "internal_ids_preview": "Check internal ids",
        "internal_ids_migrate": "Migrate internal ids",
        "rollback": "Rollback",
    }.get(action or "", action or "None")


def log_text_for_state(ctx, state, last_status, pending_deleted_devices, rollback_path):
    message = str(state.get("last_message") or "")
    details = [str(item) for item in (state.get("last_details") or []) if str(item)]

    if pending_deleted_devices and rollback_path:
        lines = [
            "deleted_devices cleanup is waiting for your decision.",
            "",
            f"Previous action: {action_label(state.get('last_action'))}",
        ]
        if message:
            lines.append(f"Last result: {message}")
        lines.extend(["", "Current state:"])
        try:
            cleanup = ctx.deleted_devices_cleanup_status(rollback_path)
            lines.extend(
                [
                    f"- removed by this cleanup: {cleanup['removed']}",
                    f"- currently in deleted_devices: {cleanup['current']}",
                    f"- new deleted_devices after restart: {cleanup['added']}",
                    f"- removed entries returned: {cleanup['returned']}",
                ]
            )
        except Exception as exc:
            lines.append(f"- rollback status unavailable: {exc}")
        lines.extend(
            [
                "- rollback: available",
                "",
                "Confirm Changes: keep this cleanup. Entries removed by this cleanup stay removed. New deleted_devices entries are kept.",
                "Revert Changes: restore only entries removed by this cleanup. Other registry changes and new deleted_devices entries are kept.",
            ]
        )
        if details:
            lines.extend(["", "Previous details:", *details])
        return "\n".join(lines)

    lines = []
    if message:
        lines.append(message)
    if details:
        if lines:
            lines.append("")
        lines.extend(details)
    if lines:
        return "\n".join(lines)
    return "Running..." if last_status == "running" else "No log entries yet."


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
                "last_message": "Fresh system backup is now available. Run an action when ready.",
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
                "last_message": "Previous stale config-check error was cleared. Run an action when ready.",
            }
        )
    releases = ctx.list_releases()
    manifest_preview = current_manifest_preview(ctx)
    target_state = state.get("last_targets") or manifest_preview
    homeassistant_organizer_enabled = any(
        target.get("type") == "homeassistant" and target.get("organizer_enabled")
        for target in manifest_preview
    )
    last_status = state.get("last_status", "idle")
    has_conflicts = bool(state.get("conflicts"))
    deleted_devices_pending_confirmation = bool(state.get("deleted_devices_pending_confirmation"))
    deleted_devices_rollback_path = state.get("deleted_devices_rollback_path")
    pending_deleted_devices_decision = bool(deleted_devices_pending_confirmation and deleted_devices_rollback_path)
    display_status = "conflicts" if has_conflicts else "pending decision" if pending_deleted_devices_decision else last_status
    if display_status == "success":
        display_status = "done"
    details = log_text_for_state(
        ctx,
        state,
        last_status,
        deleted_devices_pending_confirmation,
        deleted_devices_rollback_path,
    )
    diff_text = state.get("last_diff", "")
    save_preview_text = state.get("last_save_preview") or ""
    save_diff_text = state.get("last_save_diff") or ""
    deleted_devices_preview_text = state.get("last_deleted_devices_preview") or "No deleted_devices preview yet."
    deleted_devices_rows = state.get("last_deleted_devices_rows") or []
    retained_devices_rows = state.get("last_retained_devices_rows") or []
    internal_ids_rows = state.get("last_internal_ids_rows") or []
    save_details_html = html.escape(save_preview_text)
    if save_diff_text and save_diff_text != save_preview_text:
        save_details_html = f"<pre class='preview-summary'>{html.escape(save_preview_text)}</pre>{ui.render_conflict_detail(save_diff_text)}"
    elif save_diff_text:
        save_details_html = ui.render_conflict_detail(save_diff_text)
    run_disabled = "disabled" if last_status == "running" else ""
    action_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
    storage_approval_pending = bool(
        state.get("last_preview_storage_changes")
        and state.get("last_preview_fingerprint")
        and state.get("last_preview_approved_fingerprint") != state.get("last_preview_fingerprint")
    )
    apply_action = "approve-apply" if storage_approval_pending else "apply"
    apply_button_text = "Approve Git to HA" if storage_approval_pending else "Apply Git to HA"
    deleted_devices_count = int(state.get("last_deleted_devices_count") or 0)
    deletion_ready = bool(
        deleted_devices_count > 0
        and state.get("last_deleted_devices_preview")
        and state.get("last_deleted_devices_generated_at")
        and state.get("last_deleted_devices_fingerprint")
    )
    check_deleted_devices_disabled = "disabled" if run_disabled or deleted_devices_pending_confirmation else ""
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
            f"<button type='submit' class='secondary' {confirm_deletion_disabled}>Confirm Changes</button>"
            "</form>"
            "<form method='post' action='deleted-devices-revert' data-async-form='true' "
            "data-confirm='Stop Home Assistant Core and revert deleted_devices cleanup?'>"
            f"<button type='submit' {confirm_deletion_disabled}>Revert Changes</button>"
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
            "data-confirm='Stop Home Assistant Core and remove all deleted_devices from core.device_registry?'>"
            f"<button type='submit' {deletion_disabled}>Approve Deletion</button>"
            "</form>"
            "</div>"
            "</div>"
        )
    confirm_messages = []
    if not ctx.option_bool(options, "require_fresh_backup", True):
        confirm_messages.append("Fresh system backup checks are disabled.")
    if ui.targets_allow_protected_storage(target_state):
        confirm_messages.append("Protected .storage apply is enabled for at least one target.")
    apply_confirm = ""
    if confirm_messages:
        confirm_message = " ".join(confirm_messages) + " Continue?"
        apply_confirm = f"data-confirm='{html.escape(confirm_message, quote=True)}'"
    conflicts_section_html = ""
    if has_conflicts:
        conflicts_section_html = (
            "<section class='card wide'>"
            "<h2>Git Conflicts</h2>"
            f"{ui.render_conflicts(conflict_items(ctx, state, options), state.get('conflict_type'))}"
            "</section>"
        )
    apply_preview_section_html = ""
    if state.get("last_diff_generated_at") or diff_text:
        apply_preview_section_html = (
            "<section class='card wide'>"
            "<h2>Apply Preview</h2>"
            "<p>Generated at "
            f"<span data-transient='apply-generated'>{html.escape(ctx.format_time(state.get('last_diff_generated_at'), options))}</span>"
            "</p>"
            f"<div data-transient='apply-preview'>{ui.render_conflict_detail(diff_text) if diff_text else ''}</div>"
            "</section>"
        )
    save_preview_section_html = ""
    if not has_conflicts and (state.get("last_save_diff_generated_at") or save_preview_text or save_diff_text):
        save_preview_section_html = (
            "<section class='card wide'>"
            "<h2>Save Preview</h2>"
            "<p>Generated at "
            f"<span data-transient='save-generated'>{html.escape(ctx.format_time(state.get('last_save_diff_generated_at'), options))}</span>"
            "</p>"
            f"<div data-transient='save-preview'>{save_details_html}</div>"
            "</section>"
        )
    deleted_devices_section_html = ""
    if state.get("last_deleted_devices_generated_at") or deleted_devices_pending_confirmation:
        deleted_devices_heading = "Deletion of deleted_devices Preview"
        deleted_devices_generated_html = (
            "<p>Generated at "
            f"<span data-transient='deleted-devices-generated'>{html.escape(ctx.format_time(state.get('last_deleted_devices_generated_at'), options))}</span>"
            "</p>"
        )
        if pending_deleted_devices_decision:
            deleted_devices_heading = "Pending deleted_devices Diff"
            deleted_devices_generated_html = ""
            try:
                deleted_devices_preview_html = (
                    "<p class='muted'>Confirm Changes accepts this diff. Revert Changes restores removed lines while keeping any new current entries.</p>"
                    f"{ui.render_conflict_detail(ctx.deleted_devices_pending_diff(deleted_devices_rollback_path))}"
                )
            except Exception as exc:
                deleted_devices_preview_html = f"<p>Pending diff unavailable: {html.escape(str(exc))}</p>"
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
            "<h2>Retained Devices Preview</h2>"
            "<p class='muted'>These candidates come from stale retained Home Assistant MQTT discovery topics for Zigbee2MQTT devices missing from current Zigbee2MQTT files.</p>"
            "<p class='muted'>Delete retained devices clears selected MQTT retained discovery topics only. It does not delete files, Home Assistant registry entries, or Zigbee2MQTT database records.</p>"
            "<p>Generated at "
            f"<span data-transient='retained-devices-generated'>{html.escape(ctx.format_time(state.get('last_retained_devices_generated_at'), options))}</span>"
            "</p>"
            "<form method='post' action='retained-devices-delete' data-async-form='true' "
            "data-preserve-display-state='true' "
            "data-confirm='Clear selected MQTT retained discovery topics only? This does not delete files or registry/database records.'>"
            f"<div data-transient='retained-devices-preview'>{ui.render_retained_devices_table(retained_devices_rows)}</div>"
            "<div class='actions deletion-actions'><div class='action-row'>"
            f"<button type='submit' {retained_delete_disabled}>Delete retained devices</button>"
            "</div></div>"
            "</form>"
            "</section>"
        )

    internal_ids_section_html = ""
    if state.get("last_internal_ids_generated_at"):
        internal_ids_migrate_disabled = "disabled" if run_disabled or not any(row.get("changes") for row in internal_ids_rows) else ""
        internal_ids_changed_files = sum(1 for row in internal_ids_rows if row.get("changes"))
        internal_ids_totals = {
            "entity_triggers": sum(int(row.get("entity_triggers") or 0) for row in internal_ids_rows),
            "mqtt_triggers": sum(int(row.get("mqtt_triggers") or 0) for row in internal_ids_rows),
            "actions": sum(int(row.get("actions") or 0) for row in internal_ids_rows),
            "conditions": sum(int(row.get("conditions") or 0) for row in internal_ids_rows),
            "unresolved": sum(int(row.get("unresolved") or 0) for row in internal_ids_rows),
        }
        internal_ids_summary_html = (
            "<p>"
            f"Files: {internal_ids_changed_files}. "
            f"Entity triggers: {internal_ids_totals['entity_triggers']}. "
            f"Z2M triggers: {internal_ids_totals['mqtt_triggers']}. "
            f"Actions: {internal_ids_totals['actions']}. "
            f"Conditions: {internal_ids_totals['conditions']}. "
            f"Unresolved: {internal_ids_totals['unresolved']}."
            "</p>"
        )
        unresolved = state.get("last_internal_ids_unresolved") or []
        unresolved_html = ""
        if unresolved:
            rendered = []
            for item in unresolved[:50]:
                rendered.append(
                    "<li>"
                    f"<code>{html.escape(str(item.get('path') or ''))}</code>: "
                    f"{html.escape(str(item.get('alias') or ''))} - {html.escape(str(item.get('reason') or 'unsupported'))}"
                    "</li>"
                )
            unresolved_html = (
                "<details><summary>Unresolved device blocks</summary>"
                f"<ul>{''.join(rendered)}</ul>"
                "</details>"
            )
        internal_ids_section_html = (
            "<section class='card wide'>"
            "<h2>Internal IDs Migration Preview</h2>"
            "<p class='muted'>This migrates only HA Ops YAML in the Git checkout. It does not change live Home Assistant until the normal Git to HA apply flow.</p>"
            "<p class='muted'>After migrating, run Preview Git to HA before applying to live Home Assistant.</p>"
            "<p>Generated at "
            f"<span data-transient='internal-ids-generated'>{html.escape(ctx.format_time(state.get('last_internal_ids_generated_at'), options))}</span>"
            "</p>"
            f"{internal_ids_summary_html}"
            "<form method='post' action='internal-ids-migrate' data-async-form='true' "
            "data-preserve-display-state='true' "
            "data-confirm='Migrate selected HA Ops YAML files from internal ids to stable entity_id or Zigbee2MQTT MQTT references?'>"
            f"<div data-transient='internal-ids-preview'>{ui.render_internal_ids_table(internal_ids_rows)}{unresolved_html}{ui.render_internal_ids_diffs(internal_ids_rows, ui.render_conflict_detail)}</div>"
            "<div class='actions deletion-actions'><div class='action-row'>"
            f"<button type='submit' {internal_ids_migrate_disabled}>Migrate selected files</button>"
            "</div></div>"
            "</form>"
            "</section>"
        )

    return ui.render_page(
        {
            "status": html.escape(display_status),
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
            "latest_backup": html.escape(backup_status.get("message", "Backup status unavailable.")),
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
            "check_deleted_devices_disabled": check_deleted_devices_disabled,
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
            "include_redundant_data_html": ui.render_include_redundant_data(bool(state.get("include_redundant_data"))),
            "releases_html": ui.render_releases(releases),
            "version": html.escape(ctx.addon_version()),
        }
    )


def start_background(target, *args):
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


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
                            "last_message": "Generated a new deploy key. Add the public key to GitHub Deploy Keys.",
                            "last_details": [public_key],
                        }
                    )
                    ctx.log("Generate Deploy Key completed successfully")
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
                    self.send_json({"ok": True, "message": "Display state cleared."})
                else:
                    self.send_response(204)
                    self.end_headers()
                return

            if parsed.path == "/apply":
                start_background(ctx.run_apply_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Apply Git to HA started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/approve-apply":
                state = ctx.read_state()
                fingerprint = state.get("last_preview_fingerprint")
                if not fingerprint or not state.get("last_preview_storage_changes"):
                    message = "Run Preview Git to HA with .storage changes before approval."
                    if self.wants_json():
                        self.send_json({"ok": False, "message": message}, status=400)
                    else:
                        self.send_html(render_page(ctx), status=400)
                    return
                ctx.write_state({"last_preview_approved_fingerprint": fingerprint})
                start_background(ctx.run_apply_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Approved Git to HA. Applying..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/preview":
                ctx.write_state(state_store.APPLY_PREVIEW_CLEAR_UPDATES)
                start_background(ctx.run_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Git to HA preview started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/save-preview":
                ctx.write_state(state_store.SAVE_PREVIEW_CLEAR_UPDATES)
                start_background(ctx.run_save_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "HA to Git preview started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/save":
                start_background(ctx.run_save_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Save HA to Git started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-preview":
                ctx.write_state(state_store.DELETED_DEVICES_PREVIEW_CLEAR_UPDATES)
                start_background(ctx.run_deleted_devices_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "deleted_devices check started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/retained-devices-preview":
                ctx.write_state(state_store.RETAINED_DEVICES_PREVIEW_CLEAR_UPDATES)
                start_background(ctx.run_retained_devices_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Retained devices check started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/retained-devices-delete":
                selected = body.get("candidate", [])
                start_background(ctx.run_retained_devices_delete_job, selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Retained devices deletion started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/internal-ids-preview":
                ctx.write_state(state_store.INTERNAL_IDS_PREVIEW_CLEAR_UPDATES)
                start_background(ctx.run_internal_ids_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Internal ids check started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/internal-ids-migrate":
                selected = body.get("candidate", [])
                start_background(ctx.run_internal_ids_migrate_job, selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Internal ids migration started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-delete":
                start_background(ctx.run_deleted_devices_delete_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "deleted_devices deletion started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-confirm":
                start_background(ctx.run_deleted_devices_confirm_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "deleted_devices cleanup confirmation started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/deleted-devices-revert":
                start_background(ctx.run_deleted_devices_revert_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "deleted_devices cleanup revert started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/approve-save-conflicts":
                try:
                    message = conflict_logic.approve_save_unknown_base_conflicts(ctx)
                    start_background(ctx.run_save_job)
                    if self.wants_json():
                        self.send_json({"ok": True, "message": f"{message} Saving..."})
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

            if parsed.path == "/addons":
                selected = body.get("addon", [])
                ctx.set_selected_addon_slugs(selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Managed add-ons updated. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/homeassistant-organizer":
                enabled = "homeassistant_organizer" in body
                ctx.set_homeassistant_organizer_enabled(enabled)
                if self.wants_json():
                    message = "Home Assistant Git layout updated. Refreshing..."
                    self.send_json({"ok": True, "message": message})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/include-redundant-data":
                enabled = "include_redundant_data" in body
                state = ctx.read_state()
                updates = {
                    **state_store.SAVE_PREVIEW_CLEAR_UPDATES,
                    "include_redundant_data": enabled,
                }
                if state.get("conflict_type") == "save_unknown_base":
                    updates.update({"conflicts": [], "conflict_type": None, "save_conflict_resolutions": {}})
                ctx.write_state(updates)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Redundant data setting updated. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/resolve-conflict":
                try:
                    path = body.get("path", [""])[0]
                    choice = body.get("choice", [""])[0]
                    message = conflict_logic.resolve_git_conflict(ctx, path, choice)
                    if self.wants_json():
                        self.send_json({"ok": True, "message": f"{message} Refreshing..."})
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

            if parsed.path == "/rollback":
                release = body.get("release", [""])[0]
                if not release:
                    if self.wants_json():
                        self.send_json({"ok": False, "message": "Missing release"}, status=400)
                    else:
                        self.send_error(400, "Missing release")
                    return
                start_background(ctx.run_rollback_job, release)
                if self.wants_json():
                    self.send_json({"ok": True, "message": f"Rollback to {release} started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            self.send_error(404)

        def log_message(self, format, *args):
            return

    return Handler
