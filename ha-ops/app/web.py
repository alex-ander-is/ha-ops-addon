from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import html
import json
import threading

import conflicts as conflict_logic
import git_ops
import manifest as manifest_logic
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


def truncate_conflict_detail(text):
    max_chars = 30000
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[Conflict detail truncated.]"
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
        return truncate_conflict_detail(result.stdout.strip())
    return f"Diff unavailable:\n{(result.stderr or result.stdout).strip()}"


def save_conflict_detail(ctx, repo_dir, targets, path):
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
        return file_diff(ctx, f"Git: {safe_path}", repo_file, f"HA: {safe_path}", preview_file)
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
                detail = save_conflict_detail(ctx, repo_dir, targets, safe_path)
            else:
                detail = truncate_conflict_detail(file_text(Path(repo_dir) / safe_path).strip())
        except Exception as exc:
            safe_path = str(path)
            detail = f"Conflict detail unavailable: {exc}"
        items.append({"path": safe_path, "detail": detail})
    return items


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
    display_status = "conflicts" if has_conflicts else last_status
    details = "\n".join(state.get("last_details", []))
    details_placeholder = "Running..." if last_status == "running" else "No details yet."
    diff_text = state.get("last_diff", "")
    save_preview_text = state.get("last_save_preview") or "No save preview yet."
    save_diff_text = state.get("last_save_diff") or ""
    save_details_html = html.escape(save_preview_text)
    if save_diff_text and save_diff_text != save_preview_text:
        save_details_html = f"<pre class='preview-summary'>{html.escape(save_preview_text)}</pre>{ui.render_conflict_detail(save_diff_text)}"
    elif save_diff_text:
        save_details_html = ui.render_conflict_detail(save_diff_text)
    action_disabled = "disabled" if last_status == "running" else ""
    storage_approval_pending = bool(
        state.get("last_preview_storage_changes")
        and state.get("last_preview_fingerprint")
        and state.get("last_preview_approved_fingerprint") != state.get("last_preview_fingerprint")
    )
    apply_action = "approve-apply" if storage_approval_pending else "apply"
    apply_button_text = "Approve Git to HA" if storage_approval_pending else "Apply Git to HA"
    confirm_messages = []
    if not ctx.option_bool(options, "require_fresh_backup", True):
        confirm_messages.append("Fresh system backup checks are disabled.")
    if ui.targets_allow_protected_storage(target_state):
        confirm_messages.append("Protected .storage apply is enabled for at least one target.")
    apply_confirm = ""
    if confirm_messages:
        confirm_message = " ".join(confirm_messages) + " Continue?"
        apply_confirm = f"data-confirm='{html.escape(confirm_message, quote=True)}'"

    return ui.render_page(
        {
            "status": html.escape(display_status),
            "badge_class": (
                "conflicts"
                if has_conflicts
                else "error"
                if last_status == "error"
                else "interrupted"
                if last_status == "interrupted"
                else "running"
                if last_status == "running"
                else ""
            ),
            "message": html.escape(state.get("last_message", "")),
            "last_run": html.escape(ctx.format_time(state.get("last_run_at"), options)),
            "last_release": html.escape(str(state.get("last_release"))),
            "last_backup_slug": html.escape(str(state.get("last_backup_slug"))),
            "latest_backup": html.escape(backup_status.get("message", "Backup status unavailable.")),
            "repo_url": html.escape(options.get("repo_url", "")),
            "branch": html.escape(options.get("repo_branch", "main")),
            "manifest_path": html.escape(options.get("manifest_path", "ha-ops.json")),
            "auth_mode": html.escape(ctx.git_auth_mode(options)),
            "details_html": html.escape(details or details_placeholder),
            "diff_generated_at": html.escape(ctx.format_time(state.get("last_diff_generated_at"), options)),
            "diff_html": ui.render_conflict_detail(diff_text) if diff_text else html.escape("No apply preview yet."),
            "save_diff_generated_at": html.escape(ctx.format_time(state.get("last_save_diff_generated_at"), options)),
            "save_details_html": save_details_html,
            "preview_deletions": html.escape(str(state.get("last_preview_deletions"))),
            "action_disabled": action_disabled,
            "apply_action": apply_action,
            "apply_button_text": apply_button_text,
            "apply_confirm": apply_confirm,
            "conflicts_html": ui.render_conflicts(conflict_items(ctx, state, options), state.get("conflict_type")),
            "git_auth_html": ui.render_git_auth(options, ctx.git_auth_mode, ctx.load_generated_public_key),
            "targets_html": ui.render_targets(target_state),
            "organizer_html": ui.render_homeassistant_organizer(homeassistant_organizer_enabled),
            "addons_html": render_addons(ctx),
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
                start_background(ctx.run_preview_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Git to HA preview started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/save-preview":
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
