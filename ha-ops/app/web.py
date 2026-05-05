from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import html
import json
import threading

import conflicts as conflict_logic
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
        previews = []
        for target in manifest.get("targets", []):
            previews.append(
                {
                    "id": target.get("id"),
                    "type": target.get("type"),
                    "source": target.get("source"),
                    "addon_slug": target.get("addon_slug"),
                    "addon_slug_suffix": target.get("addon_slug_suffix"),
                    "allow_protected_storage": target.get("allow_protected_storage", False),
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


def render_page(ctx):
    options = ctx.load_options()
    state = ctx.read_state()
    backup_status = ctx.latest_system_backup_status(options)
    releases = ctx.list_releases()
    manifest_preview = current_manifest_preview(ctx)
    target_state = state.get("last_targets") or manifest_preview
    last_status = state.get("last_status", "idle")
    details = "\n".join(state.get("last_details", []))
    details_placeholder = "Running..." if last_status == "running" else "No details yet."
    diff_text = state.get("last_diff", "")
    action_disabled = "disabled" if last_status == "running" else ""
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
            "status": html.escape(last_status),
            "badge_class": "error" if last_status == "error" else "running" if last_status == "running" else "",
            "message": html.escape(state.get("last_message", "")),
            "last_run": html.escape(str(state.get("last_run_at"))),
            "last_release": html.escape(str(state.get("last_release"))),
            "last_backup_slug": html.escape(str(state.get("last_backup_slug"))),
            "latest_backup": html.escape(backup_status.get("message", "Backup status unavailable.")),
            "repo_url": html.escape(options.get("repo_url", "")),
            "branch": html.escape(options.get("repo_branch", "main")),
            "manifest_path": html.escape(options.get("manifest_path", "ha-ops.json")),
            "auth_mode": html.escape(ctx.git_auth_mode(options)),
            "details_html": html.escape(details or details_placeholder),
            "diff_generated_at": html.escape(str(state.get("last_diff_generated_at"))),
            "diff_html": html.escape(diff_text or "No apply preview yet."),
            "preview_deletions": html.escape(str(state.get("last_preview_deletions"))),
            "action_disabled": action_disabled,
            "apply_confirm": apply_confirm,
            "conflicts_html": ui.render_conflicts(state.get("conflicts", [])),
            "git_auth_html": ui.render_git_auth(options, ctx.git_auth_mode, ctx.load_generated_public_key),
            "targets_html": ui.render_targets(target_state),
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

            if parsed.path == "/apply":
                start_background(ctx.run_apply_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Apply Git to HA started. Refreshing..."})
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

            if parsed.path == "/save":
                start_background(ctx.run_save_job)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Save HA to Git started. Refreshing..."})
                else:
                    self.send_html(render_page(ctx))
                return

            if parsed.path == "/addons":
                selected = body.get("addon", [])
                ctx.set_selected_addon_slugs(selected)
                if self.wants_json():
                    self.send_json({"ok": True, "message": "Managed add-on selection saved. Refreshing..."})
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
