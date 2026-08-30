import json
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import app_context
import jobs as job_logic
import state as state_store
import web


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INGRESS_TOKEN = "local-ha-ops"
FORBIDDEN_ROOTS = (
    Path("/data"),
    Path("/homeassistant"),
    Path("/addon_configs"),
    Path("/backup"),
    Path("/Users/purportex/Work/HA/ha-config"),
)
ALLOWED_FAKE_READS = {
    ("GET", "/addons"),
    ("GET", "/addons/self/info"),
    ("GET", "/mounts"),
    ("GET", "/backups/info"),
    ("GET", "/services/mqtt"),
    ("GET", "/supervisor/ping"),
}
FORBIDDEN_LIVE_RISK_PREFIXES = (
    "/core/",
    "/addons/",
    "/supervisor/update",
    "/supervisor/restart",
    "/supervisor/repair",
    "/backups/new",
)


def _resolved(path):
    return Path(path).expanduser().resolve()


def safe_root_guard(root, explicit=False):
    root = _resolved(root)
    for forbidden in FORBIDDEN_ROOTS:
        forbidden = _resolved(forbidden)
        if root == forbidden or forbidden in root.parents:
            raise RuntimeError(f"dev harness refuses unsafe root: {root}")
    temp_root = _resolved(tempfile.gettempdir())
    repo_dev_root = _resolved(APP_ROOT / ".ha-ops-dev")
    if root == temp_root or temp_root in root.parents:
        return root
    if explicit and (root == repo_dev_root or repo_dev_root in root.parents):
        return root
    raise RuntimeError(
        "dev harness root must be a temporary directory or an explicit path under ha-ops/.ha-ops-dev"
    )


def _run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{command[0]} failed")
    return result


def seed_fixture_root(root):
    root = safe_root_guard(root, explicit=True)
    data_dir = root / "data"
    config_dir = root / "homeassistant"
    addon_configs_dir = root / "addon_configs"
    remote_dir = root / "remote.git"
    seed_repo = root / "seed-repo"
    for path in (data_dir, config_dir / ".storage", addon_configs_dir / "local_mqtt", data_dir / "work"):
        path.mkdir(parents=True, exist_ok=True)

    (config_dir / "configuration.yaml").write_text(
        "default_config:\ninput_boolean:\n  harness_live_only:\n    name: Harness live only\n"
    )
    (config_dir / ".storage" / "core.config").write_text(
        json.dumps({"version": 1, "data": {"time_zone": "UTC"}}, indent=2)
    )
    (addon_configs_dir / "local_mqtt" / "options.json").write_text(json.dumps({"fixture": True}, indent=2))

    _run(["git", "init", "--bare", str(remote_dir)])
    seed_repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], cwd=seed_repo)
    _run(["git", "config", "user.email", "ha-ops-dev-harness@example.invalid"], cwd=seed_repo)
    _run(["git", "config", "user.name", "HA Ops Dev Harness"], cwd=seed_repo)
    (seed_repo / "homeassistant").mkdir(parents=True, exist_ok=True)
    (seed_repo / "homeassistant" / "configuration.yaml").write_text(
        "default_config:\ninput_boolean:\n  harness_git_only:\n    name: Harness git only\n"
    )
    (seed_repo / "ha-ops.json").write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source": "homeassistant",
                        "apply": True,
                    }
                ]
            },
            indent=2,
        )
    )
    _run(["git", "add", "-A"], cwd=seed_repo)
    _run(["git", "commit", "-m", "Seed HA Ops harness fixture"], cwd=seed_repo)
    _run(["git", "remote", "add", "origin", str(remote_dir)], cwd=seed_repo)
    _run(["git", "push", "-u", "origin", "main"], cwd=seed_repo)
    for branch in ("ha-ops/ha-live", "ha-ops/base"):
        _run(["git", "branch", branch], cwd=seed_repo)
        _run(["git", "push", "origin", branch], cwd=seed_repo)

    options = {
        "repo_url": str(remote_dir),
        "repo_branch": "main",
        "repo_path": "ha-config",
        "manifest_path": "ha-ops.json",
        "apply_path": "homeassistant",
        "require_fresh_backup": False,
        "time_zone": "UTC",
    }
    (data_dir / "options.json").write_text(json.dumps(options, indent=2, sort_keys=True))
    return {
        "root": root,
        "data_dir": data_dir,
        "config_dir": config_dir,
        "addon_configs_dir": addon_configs_dir,
        "remote_dir": remote_dir,
        "seed_repo": seed_repo,
        "options": options,
    }


class FakeSupervisor:
    def __init__(self):
        self.calls = []

    def call(self, method, path, payload=None, timeout=None):
        method = str(method or "GET").upper()
        path = str(path or "")
        record = {"method": method, "path": path, "payload": payload, "timeout": timeout}
        self.calls.append(record)
        if (method, path) in ALLOWED_FAKE_READS or (method == "GET" and path.startswith("/addons/") and path.endswith("/info")):
            return self._fake_response(path)
        if method != "GET" or any(path.startswith(prefix) for prefix in FORBIDDEN_LIVE_RISK_PREFIXES):
            record["forbidden"] = True
            raise RuntimeError(f"dev harness fake Supervisor forbids {method} {path}")
        return self._fake_response(path)

    def _fake_response(self, path):
        if path == "/addons":
            return {"addons": [{"slug": "local_mqtt", "name": "Local MQTT"}]}
        if path.startswith("/addons/") and path.endswith("/info"):
            slug = path.split("/")[2]
            return {"slug": slug, "name": slug.replace("_", " ").title(), "state": "started"}
        if path == "/mounts":
            return {"mounts": []}
        if path == "/backups/info":
            return {"backups": []}
        if path == "/services/mqtt":
            return {"host": "127.0.0.1", "port": 1883}
        return {"result": "ok"}


class HarnessScenarioController:
    def __init__(self):
        self._lock = threading.Lock()
        self._gates = {}
        self.counters = {
            "started_jobs": {},
            "held_jobs": {},
            "completed_jobs": {},
            "duplicate_rejections": {},
            "ws_replays_seen": 0,
            "log_markers_emitted": 0,
        }
        self.events = []

    def arm(self, action, gate):
        action = self._normalize_action(action)
        gate = str(gate or "running")
        with self._lock:
            self._gates[(action, gate)] = {
                "armed": True,
                "held": threading.Event(),
                "release": threading.Event(),
                "phase": "gate-armed",
            }
            self.events.append({"action": action, "gate": gate, "phase": "gate-armed"})
        return {"ok": True, "action": action, "gate": gate}

    def release(self, action, gate):
        action = self._normalize_action(action)
        gate = str(gate or "running")
        with self._lock:
            item = self._gates.get((action, gate))
            if item is None:
                return {"ok": False, "status": 404, "message": "gate is not armed"}
            item["phase"] = "gate-released"
            self.events.append({"action": action, "gate": gate, "phase": "gate-released"})
            item["release"].set()
        return {"ok": True, "action": action, "gate": gate}

    def record_duplicate_rejection(self, action):
        action = self._normalize_action(action)
        with self._lock:
            self.counters["duplicate_rejections"][action] = self.counters["duplicate_rejections"].get(action, 0) + 1
            self.events.append({"action": action, "phase": "duplicate-rejected"})

    def record_ws_replay(self):
        with self._lock:
            self.counters["ws_replays_seen"] += 1
            self.events.append({"phase": "ws-replay"})

    def run_job(self, ctx, action, lock_acquired=False):
        action = self._normalize_action(action)
        if not job_logic.enter_run_lock(ctx, action, lock_acquired):
            return False
        gate = "running"
        details = [f"HARNESS {action} running-held"]
        try:
            with self._lock:
                self.counters["started_jobs"][action] = self.counters["started_jobs"].get(action, 0) + 1
                item = self._gates.get((action, gate))
                if item is not None:
                    item["phase"] = "running-held"
                    item["held"].set()
                    self.counters["held_jobs"][action] = self.counters["held_jobs"].get(action, 0) + 1
                    self.events.append({"action": action, "gate": gate, "phase": "running-held"})
                self.counters["log_markers_emitted"] += 1
            ctx.write_state(
                {
                    "last_run_at": ctx.utc_now(),
                    "last_status": "running",
                    "last_action": action,
                    "last_message": f"HARNESS {action} running-held",
                    "last_details": details,
                }
            )
            if item is not None and not item["release"].wait(timeout=30):
                raise RuntimeError(f"dev harness gate timed out: {action}/{gate}")
            if action == "preview":
                self._write_apply_preview(ctx, details)
            elif action == "save_preview":
                self._write_save_preview(ctx, details)
            elif action == "apply":
                self._write_apply_complete(ctx, details)
            else:
                self._write_save_complete(ctx, details)
            with self._lock:
                self.counters["completed_jobs"][action] = self.counters["completed_jobs"].get(action, 0) + 1
                self.events.append({"action": action, "phase": "terminal"})
            return True
        except Exception as exc:
            ctx.write_state(
                {
                    "last_run_at": ctx.utc_now(),
                    "last_status": "error",
                    "last_action": action,
                    "last_message": str(exc),
                    "last_details": [*details, str(exc)],
                }
            )
            return False
        finally:
            job_logic.release_run_lock(ctx)

    def diagnostics(self, ctx=None):
        with self._lock:
            gates = {
                f"{action}:{gate}": {
                    "phase": item["phase"],
                    "held": item["held"].is_set(),
                    "released": item["release"].is_set(),
                }
                for (action, gate), item in self._gates.items()
            }
            payload = {
                "ok": True,
                "gates": gates,
                "counters": json.loads(json.dumps(self.counters)),
                "events": list(self.events[-50:]),
            }
        if ctx is not None:
            state = ctx.read_state()
            payload["state"] = {
                "last_status": state.get("last_status"),
                "last_action": state.get("last_action"),
                "last_message": state.get("last_message"),
                "operation_generation": state.get("operation_generation"),
                "job_running": web.job_is_running(ctx, state),
                "state_sequence": ctx.state_change_sequence(),
            }
        return payload

    def _write_apply_preview(self, ctx, details):
        path = "homeassistant/configuration.yaml"
        second_path = "homeassistant/packages/harness.yaml"
        diff = (
            "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n"
            "--- a/homeassistant/configuration.yaml\n"
            "+++ b/homeassistant/configuration.yaml\n"
            "@@ -1,2 +1,3 @@\n"
            " default_config:\n"
            "+input_boolean:\n"
            "+  harness_git_only:\n"
            "diff --git a/homeassistant/packages/harness.yaml b/homeassistant/packages/harness.yaml\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/homeassistant/packages/harness.yaml\n"
            "@@ -0,0 +1,2 @@\n"
            "+input_boolean:\n"
            "+  harness_git_package:\n"
        )
        ctx.write_state(
            {
                "last_run_at": ctx.utc_now(),
                "last_status": "success",
                "last_action": "preview",
                "last_message": "Harness Git to HA preview finished.",
                "last_details": [*details, "Harness Git to HA preview finished."],
                "last_diff": diff,
                "last_diff_generated_at": ctx.utc_now(),
                "last_preview_commit": "harness-preview",
                "last_preview_fingerprint": "harness-preview-fingerprint",
                "last_preview_deletions": 0,
                "last_preview_storage_changes": False,
                "last_preview_storage_paths": [],
                "last_preview_live_fingerprints": {path: "live-fingerprint", second_path: "missing"},
                "last_preview_warnings": [],
                "last_preview_paths": [path, second_path],
                "last_preview_conflicts": False,
                "last_preview_conflict_paths": [],
                "apply_preview_resolutions": {},
                "apply_preview_selected_paths": [],
            }
        )

    def _write_save_preview(self, ctx, details):
        path = "homeassistant/configuration.yaml"
        second_path = "homeassistant/packages/live_harness.yaml"
        diff = (
            "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n"
            "--- a/homeassistant/configuration.yaml\n"
            "+++ b/homeassistant/configuration.yaml\n"
            "@@ -1,2 +1,3 @@\n"
            " default_config:\n"
            "+input_boolean:\n"
            "+  harness_live_only:\n"
            "diff --git a/homeassistant/packages/live_harness.yaml b/homeassistant/packages/live_harness.yaml\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/homeassistant/packages/live_harness.yaml\n"
            "@@ -0,0 +1,2 @@\n"
            "+input_boolean:\n"
            "+  harness_live_package:\n"
        )
        summary = (
            "Save preview changes (2):\n"
            "- Modified: homeassistant/configuration.yaml\n"
            "- Added: homeassistant/packages/live_harness.yaml"
        )
        ctx.write_state(
            {
                "last_run_at": ctx.utc_now(),
                "last_status": "success",
                "last_action": "save_preview",
                "last_message": "Harness HA to Git preview finished.",
                "last_details": [*details, "Harness HA to Git preview finished."],
                "last_save_preview": summary,
                "last_save_diff": diff,
                "last_save_diff_generated_at": ctx.utc_now(),
                "last_save_preview_commit": "harness-save-preview",
                "last_save_preview_fingerprint": "harness-save-preview-fingerprint",
                "last_save_preview_warnings": [],
                "last_save_preview_paths": [path, second_path],
                "last_save_preview_conflicts": False,
                "last_save_preview_conflict_paths": [],
                "last_save_commit_subject": "Harness save preview",
                "save_preview_resolutions": {},
                "save_preview_selected_paths": [],
            }
        )

    def _write_apply_complete(self, ctx, details):
        ctx.write_state(
            {
                **state_store.APPLY_PREVIEW_CLEAR_UPDATES,
                "last_run_at": ctx.utc_now(),
                "last_status": "success",
                "last_action": "apply",
                "last_message": "Harness Git automation applied to HA.",
                "last_details": [*details, "Harness Git automation applied to HA."],
                "post_apply_save_recommended": True,
            }
        )

    def _write_save_complete(self, ctx, details):
        ctx.write_state(
            {
                **state_store.SAVE_PREVIEW_CLEAR_UPDATES,
                "last_run_at": ctx.utc_now(),
                "last_status": "success",
                "last_action": "save",
                "last_message": "Harness live HA changes committed to Git.",
                "last_details": [*details, "Harness live HA changes committed to Git."],
                "post_apply_save_recommended": False,
            }
        )

    def _normalize_action(self, action):
        action = str(action or "").replace("-", "_")
        if action not in {"preview", "save_preview", "apply", "save"}:
            raise RuntimeError(f"unsupported dev harness action: {action}")
        return action


class DevHarnessContext(app_context.AppContext):
    def __init__(self, root, host="127.0.0.1", port=0):
        fixture = seed_fixture_root(root)
        self.dev_harness_root = fixture["root"]
        self.dev_harness_enabled = True
        self.fake_supervisor = FakeSupervisor()
        self.harness_controller = HarnessScenarioController()
        super().__init__(
            data_dir=fixture["data_dir"],
            config_dir=fixture["config_dir"],
            addon_configs_dir=fixture["addon_configs_dir"],
            addon_config_path=APP_ROOT / "config.yaml",
            host=host,
            port=int(port),
        )

    def call_supervisor(self, method, path, payload=None, timeout=None):
        return self.fake_supervisor.call(method, path, payload=payload, timeout=timeout)

    def get_installed_addons(self):
        return self.fake_supervisor.call("GET", "/addons").get("addons", [])

    def get_addon_info(self, slug):
        return self.fake_supervisor.call("GET", f"/addons/{slug}/info")

    def addon_action(self, slug, action):
        return self.fake_supervisor.call("POST", f"/addons/{slug}/{action}")

    def core_stop(self):
        return self.fake_supervisor.call("POST", "/core/stop")

    def core_start(self):
        return self.fake_supervisor.call("POST", "/core/start")

    def core_restart(self):
        return self.fake_supervisor.call("POST", "/core/restart")

    def core_reload_yaml(self):
        return self.fake_supervisor.call("POST", "/core/reload")

    def core_reload_lovelace(self):
        return self.fake_supervisor.call("POST", "/core/reload_lovelace")

    def core_reload_themes(self):
        return self.fake_supervisor.call("POST", "/core/reload_themes")

    def create_ha_backup(self, name_prefix, backup_location=None):
        return self.fake_supervisor.call("POST", "/backups/new/full")

    def backup_manager_info(self):
        return self.fake_supervisor.call("GET", "/backups/info")

    def backup_mount_info(self):
        return self.fake_supervisor.call("GET", "/mounts")

    def latest_system_backup_status(self, options=None):
        return {"ok": True, "message": "Dev harness: fake backup status.", "backup": None}

    def run_preview_job(self, lock_acquired=False):
        return self.harness_controller.run_job(self, "preview", lock_acquired=lock_acquired)

    def run_save_preview_job(self, lock_acquired=False):
        return self.harness_controller.run_job(self, "save_preview", lock_acquired=lock_acquired)

    def run_apply_job(self, lock_acquired=False):
        return self.harness_controller.run_job(self, "apply", lock_acquired=lock_acquired)

    def run_save_job(self, commit_subject=None, lock_acquired=False):
        return self.harness_controller.run_job(self, "save", lock_acquired=lock_acquired)

    def dev_harness_record_duplicate_rejection(self, action):
        self.harness_controller.record_duplicate_rejection(action)

    def dev_harness_record_ws_replay(self):
        self.harness_controller.record_ws_replay()

    def dev_harness_handle_get(self, route, parsed):
        if route == "/__dev_harness__/diagnostics":
            payload = self.harness_controller.diagnostics(self)
            payload["supervisor_calls"] = [
                {"method": call["method"], "path": call["path"], "forbidden": bool(call.get("forbidden"))}
                for call in self.fake_supervisor.calls
            ]
            return payload
        return None

    def dev_harness_handle_post(self, route, body):
        if route == "/__dev_harness__/arm":
            return self.harness_controller.arm(_first(body, "action"), _first(body, "gate") or "running")
        if route == "/__dev_harness__/release":
            return self.harness_controller.release(_first(body, "action"), _first(body, "gate") or "running")
        if route == "/__dev_harness__/clear-previews":
            self.write_state(state_store.ALL_PREVIEW_CLEAR_UPDATES)
            return {"ok": True}
        return None


def _first(body, name):
    value = body.get(name, [""])
    return value[0] if isinstance(value, list) else value


def create_context(root=None, host="127.0.0.1", port=0, keep_root=False):
    if root is None:
        root = tempfile.mkdtemp(prefix="ha-ops-dev-harness-")
        explicit = False
    else:
        root = safe_root_guard(root, explicit=True)
        explicit = True
        Path(root).mkdir(parents=True, exist_ok=True)
    ctx = DevHarnessContext(root, host=host, port=port)
    ctx.dev_harness_keep_root = bool(keep_root or explicit)
    return ctx


def serve_context(ctx):
    ctx.releases_dir.mkdir(parents=True, exist_ok=True)
    ctx.repair_startup_state()
    httpd = ThreadingHTTPServer((ctx.host, ctx.port), web.create_handler(ctx))
    ctx.port = httpd.server_address[1]
    return httpd


def ingress_base_url(ctx, token=DEFAULT_INGRESS_TOKEN):
    return f"http://{ctx.host}:{ctx.port}/api/hassio_ingress/{token}/"


def cleanup_context(ctx):
    if getattr(ctx, "dev_harness_keep_root", False):
        return
    root = getattr(ctx, "dev_harness_root", None)
    if root:
        shutil.rmtree(root, ignore_errors=True)
