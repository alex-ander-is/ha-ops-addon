import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import MethodType


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "app" / "server.py"


def load_server():
    sys.modules.pop("server", None)
    spec = importlib.util.spec_from_file_location("server", SERVER_PATH)
    server = importlib.util.module_from_spec(spec)
    sys.modules["server"] = server
    spec.loader.exec_module(server)
    return server


class ServerTests(unittest.TestCase):
    def configure_paths(self, server, root):
        server.DATA_DIR = root / "data"
        server.WORK_DIR = server.DATA_DIR / "work"
        server.STATE_PATH = server.DATA_DIR / "state.json"
        server.OPTIONS_PATH = server.DATA_DIR / "options.json"
        server.RELEASES_DIR = server.DATA_DIR / "releases"
        server.CONFIG_DIR = root / "homeassistant"
        server.ADDON_CONFIGS_DIR = root / "addon_configs"
        server.DATA_DIR.mkdir(parents=True)
        server.WORK_DIR.mkdir(parents=True)
        server.RELEASES_DIR.mkdir(parents=True)
        server.CONFIG_DIR.mkdir(parents=True)
        server.ADDON_CONFIGS_DIR.mkdir(parents=True)
        server.log = lambda message: None

    def git(self, args, cwd):
        return subprocess.run(["git"] + args, cwd=cwd, check=True, text=True, capture_output=True)

    def git_commit_all(self, repo, message):
        self.git(["add", "-A"], repo)
        self.git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                message,
            ],
            repo,
        )

    def seed_remote(self, root, file_text="base\n"):
        remote = root / "remote.git"
        seed = root / "seed"
        self.git(["init", "--bare", str(remote)], root)
        self.git(["init", str(seed)], root)
        self.git(["checkout", "-b", "main"], seed)
        path = seed / "homeassistant" / "configuration.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(file_text)
        self.git_commit_all(seed, "base")
        self.git(["remote", "add", "origin", str(remote)], seed)
        self.git(["push", "-u", "origin", "main"], seed)
        return remote

    def remote_file(self, remote, path):
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "show", f"main:{path}"],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout

    def repo_status(self, repo):
        return self.git(["status", "--porcelain"], repo).stdout.strip()

    def make_rebase_conflict(self, server, root):
        remote = self.seed_remote(root)
        repo = server.DATA_DIR / "ha-config"
        self.git(["clone", str(remote), str(repo)], root)
        self.git(["checkout", "main"], repo)

        local_path = repo / "homeassistant" / "configuration.yaml"
        local_path.write_text("ha\n")
        self.git_commit_all(repo, "ha")

        updater = root / "updater"
        self.git(["clone", str(remote), str(updater)], root)
        self.git(["checkout", "main"], updater)
        updater_path = updater / "homeassistant" / "configuration.yaml"
        updater_path.write_text("git\n")
        self.git_commit_all(updater, "git")
        self.git(["push", "origin", "main"], updater)

        server.OPTIONS_PATH.write_text(
            json.dumps({"repo_url": str(remote), "repo_branch": "main", "repo_path": "ha-config"})
        )
        with self.assertRaises(RuntimeError):
            server.git_pull_rebase(repo, server.git_env(server.load_options()), "main")
        self.assertEqual(server.git_conflict_paths(repo), ["homeassistant/configuration.yaml"])
        return remote

    def test_state_write_replaces_temp_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            server.write_state({"last_status": "success", "last_message": "ok"})

            self.assertEqual(server.read_state()["last_status"], "success")
            self.assertEqual(server.read_state()["last_message"], "ok")
            self.assertFalse((server.STATE_PATH.parent / f".{server.STATE_PATH.name}.tmp").exists())

    def test_core_check_accepts_current_supervisor_success_payload(self):
        server = load_server()

        server.supervisor.do_core_check(lambda method, path: {"result": "ok", "data": {}})

        with self.assertRaisesRegex(RuntimeError, "config check failed"):
            server.supervisor.do_core_check(lambda method, path: {"result": "error", "data": {}})

    def test_clear_display_state_keeps_apply_safety_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_details": ["detail"],
                    "last_diff": "diff",
                    "last_diff_generated_at": "now",
                    "last_save_preview": "summary",
                    "last_save_diff": "save diff",
                    "last_save_diff_generated_at": "now",
                    "last_preview_commit": "abc",
                    "last_preview_fingerprint": "fingerprint",
                    "last_preview_storage_changes": True,
                    "last_preview_approved_fingerprint": "fingerprint",
                }
            )

            server.clear_display_state()
            state = server.read_state()

            self.assertEqual(state["last_details"], [])
            self.assertEqual(state["last_diff"], "")
            self.assertIsNone(state["last_diff_generated_at"])
            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_save_diff"], "")
            self.assertIsNone(state["last_save_diff_generated_at"])
            self.assertEqual(state["last_deleted_devices_preview"], "")
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIsNone(state["last_deleted_devices_fingerprint"])
            self.assertIsNone(state["last_deleted_devices_generated_at"])
            self.assertEqual(state["last_preview_commit"], "abc")
            self.assertEqual(state["last_preview_fingerprint"], "fingerprint")
            self.assertTrue(state["last_preview_storage_changes"])
            self.assertEqual(state["last_preview_approved_fingerprint"], "fingerprint")

    def test_render_page_formats_state_times_in_home_assistant_timezone(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            (server.CONFIG_DIR / ".storage").mkdir()
            (server.CONFIG_DIR / ".storage" / "core.config").write_text(
                json.dumps({"data": {"time_zone": "Europe/Prague"}})
            )
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_run_at": "2026-05-14T19:52:16+00:00",
                    "last_diff_generated_at": "2026-05-14T19:52:16+00:00",
                    "last_save_diff_generated_at": "2026-05-14T19:52:16+00:00",
                }
            )

            page = server.render_page()

            self.assertIn("2026-05-14T21:52:16+02:00", page)
            self.assertNotIn("2026-05-14T19:52:16+00:00", page)

    def test_startup_repairs_stale_running_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            server.write_state(
                {
                    "last_status": "running",
                    "last_message": "Building apply preview without changing live config.",
                    "last_details": ["Building apply preview without changing live config."],
                    "last_diff": "old diff",
                }
            )

            server._CTX.repair_startup_state()

            state = server.read_state()
            self.assertEqual(state["last_status"], "interrupted")
            self.assertEqual(state["last_message"], "Previous action was interrupted by HA Ops restart.")
            self.assertEqual(state["last_details"], [])
            self.assertEqual(state["last_diff"], "")

    def test_startup_reverts_interrupted_deleted_devices_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {
                "data": {
                    "devices": [],
                    "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                }
            }
            registry_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            rollback_path = server.WORK_DIR / "deleted-devices-rollback" / "core.device_registry"
            rollback_path.parent.mkdir(parents=True)
            rollback_path.write_text(json.dumps(original))
            events = []
            server.core_start = lambda: events.append("start")
            server.write_state(
                {
                    "last_status": "running",
                    "last_action": "deleted_devices_delete",
                    "last_message": "Deleting deleted_devices.",
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback_path),
                    "deleted_devices_rollback_fingerprint": "before",
                    "deleted_devices_applied_fingerprint": None,
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(events, ["start"])
            self.assertEqual(state["last_status"], "interrupted")
            self.assertEqual(state["last_message"], "Interrupted deleted_devices cleanup was reverted on startup.")
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertFalse(rollback_path.exists())
            self.assertEqual(state["last_deleted_devices_count"], 1)

    def test_startup_clears_transient_display_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "success",
                    "last_details": ["old detail"],
                    "last_diff": "old diff",
                    "last_save_preview": "old save",
                    "last_preview_fingerprint": "keep",
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_status"], "success")
            self.assertEqual(state["last_details"], [])
            self.assertEqual(state["last_diff"], "")
            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_preview_fingerprint"], "keep")

    def test_refresh_clears_transient_conflicts_from_display_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_status": "conflicts",
                    "last_message": "Resolve Git conflicts before continuing.",
                    "conflicts": ["homeassistant/configuration.yaml"],
                    "conflict_type": "save_unknown_base",
                    "save_conflict_resolutions": {"homeassistant/configuration.yaml": "git"},
                }
            )

            page = server.render_page()
            self.assertIn('<div class="badge conflicts">conflicts</div>', page)
            self.assertIn("<h2>Git Conflicts</h2>", page)

            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["conflicts"], [])
            self.assertIsNone(state["conflict_type"])
            self.assertEqual(state["save_conflict_resolutions"], {})
            self.assertNotIn('<div class="badge conflicts">conflicts</div>', page)
            self.assertNotIn("<h2>Git Conflicts</h2>", page)

    def test_refresh_clears_transient_success_status(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "success",
                    "last_action": "save",
                    "last_message": "Save finished successfully.",
                }
            )

            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_status"], "idle")
            self.assertIsNone(state["last_action"])
            self.assertNotIn('<div class="badge success">success</div>', page)
            self.assertIn("Previous transient status was cleared", page)

    def test_async_actions_do_not_clear_persisted_state_before_submit(self):
        server = load_server()

        page = server.render_page()
        submit_start = page.index("async function submitAsyncForm")
        submit_end = page.index("try {", submit_start)
        submit_setup = page[submit_start:submit_end]

        self.assertIn("clearTransientDisplay();", submit_setup)
        self.assertNotIn("clearDisplayState();", submit_setup)

    def test_startup_clears_empty_error_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "error",
                    "last_message": "",
                    "last_details": [],
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_status"], "idle")
            self.assertEqual(state["last_message"], "Previous stale error was cleared. Run an action when ready.")

    def test_startup_clears_stale_successful_config_check_error(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "error",
                    "last_action": "apply",
                    "last_message": "Home Assistant config check failed: {'result': 'ok', 'data': {}}",
                    "last_details": ["Home Assistant config check failed: {'result': 'ok', 'data': {}}"],
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_status"], "idle")
            self.assertEqual(state["last_message"], "Previous stale error was cleared. Run an action when ready.")

    def test_app_context_uses_injected_paths_and_callbacks(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = server.app_context.AppContext(
                data_dir=root / "data",
                config_dir=root / "homeassistant",
                addon_configs_dir=root / "addon_configs",
                addon_config_path=root / "config.yaml",
            )
            ctx.data_dir.mkdir(parents=True)
            ctx.work_dir.mkdir(parents=True)
            ctx.config_dir.mkdir(parents=True)
            ctx.addon_configs_dir.mkdir(parents=True)
            ctx.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            calls = []

            def fake_run_command(command, env=None, cwd=None):
                calls.append((command, cwd))
                return subprocess.CompletedProcess(command, 0, "", "")

            ctx.run_command = fake_run_command

            sync_deps = ctx.sync_deps()
            release_deps = ctx.release_deps()
            job_deps = ctx.job_deps()

            self.assertEqual(sync_deps.work_dir, ctx.work_dir)
            self.assertEqual(release_deps.releases_dir, ctx.releases_dir)
            self.assertIs(job_deps.run_lock, ctx.run_lock)
            self.assertEqual(ctx.read_state()["managed_addons"], ["local_zigbee2mqtt"])
            ctx.stage_all(root / "repo")
            self.assertEqual(calls[0][0], ["git", "add", "-A"])

    def test_default_app_context_uses_home_assistant_config_mount(self):
        server = load_server()

        ctx = server.app_context.AppContext()

        self.assertEqual(ctx.config_dir, Path("/homeassistant"))
        self.assertEqual(ctx.options_path, Path("/data/options.json"))

    def test_git_auth_module_uses_injected_paths_and_runner(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            key_path = work / "generated_deploy_key"
            pub_path = work / "generated_deploy_key.pub"

            self.assertEqual(server.git_auth.git_auth_mode({}, key_path, pub_path), "none")
            self.assertEqual(server.git_auth.git_auth_mode({"git_ssh_key": "KEY"}, key_path, pub_path), "manual")

            env = {}
            server.git_auth.setup_git_ssh_env(env, work, key_text="PRIVATE")
            self.assertIn("manual_deploy_key", env["GIT_SSH_COMMAND"])
            self.assertEqual((work / "manual_deploy_key").read_text(), "PRIVATE")

            pub_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text("generated-private")
            pub_path.write_text("generated-public\n")
            self.assertEqual(server.git_auth.git_auth_mode({}, key_path, pub_path), "generated")
            self.assertEqual(server.git_auth.load_generated_public_key(pub_path), "generated-public")

            commands = []

            def fake_keygen(command, env=None, cwd=None):
                commands.append(command)
                (work / "generated_deploy_key.new").write_text("new-private")
                (work / "generated_deploy_key.new.pub").write_text("new-public\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            public_key = server.git_auth.generate_deploy_key(work, key_path, pub_path, fake_keygen, lambda message: None)

            self.assertEqual(public_key, "new-public")
            self.assertEqual(key_path.read_text(), "new-private")
            self.assertEqual(commands[0][0], "ssh-keygen")

    def test_conflict_module_resolves_save_conflict_with_context_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = server.app_context.AppContext(data_dir=root / "data", config_dir=root / "ha", addon_configs_dir=root / "addons")
            ctx.data_dir.mkdir(parents=True)
            ctx.write_state({"conflicts": ["homeassistant/configuration.yaml"], "conflict_type": "save_unknown_base"})

            message = server.conflicts.resolve_git_conflict(ctx, "homeassistant/configuration.yaml", "git")
            state = ctx.read_state()

            self.assertIn("Run Save HA to Git again", message)
            self.assertEqual(state["conflicts"], [])
            self.assertEqual(state["save_conflict_resolutions"], {"homeassistant/configuration.yaml": "git"})

    def test_conflict_ui_explains_version_choices(self):
        server = load_server()

        content = server.ui.render_conflicts(
            [
                {
                    "path": "homeassistant/.storage/core.config_entries",
                    "detail": "--- Git\n+++ HA\n@@ -1 +1 @@\n-version: 0.4.10\n+version: 0.4.11",
                }
            ]
        )

        self.assertIn("there is no trusted common base", content)
        self.assertIn("Use HA Version", content)
        self.assertIn("Use Git Version", content)
        self.assertIn("table-scroll", content)
        self.assertIn("conflict-diff", content)
        self.assertIn("diff-wrap-toggle", content)
        self.assertIn("Wrap lines", content)
        self.assertIn("diff-del", content)
        self.assertIn("diff-add", content)
        self.assertIn("diff-changed", content)
        self.assertIn("0.4.1", content)

    def test_save_conflict_ui_can_approve_all_as_ha_version(self):
        server = load_server()

        content = server.ui.render_conflicts(
            [{"path": "homeassistant/.storage/core.device_registry", "detail": "--- Git\n+++ HA\n"}],
            conflict_type="save_unknown_base",
        )

        self.assertIn("Approve HA to Git", content)
        self.assertIn("approve-save-conflicts", content)

    def test_conflict_detail_is_not_truncated(self):
        server = load_server()

        detail = "x" * 40000

        self.assertEqual(server.web.full_conflict_detail(detail), detail)

    def test_save_preview_diff_is_not_truncated(self):
        server = load_server()
        diff = "x" * 70000

        def run_command(_args):
            return subprocess.CompletedProcess(_args, 1, stdout=diff, stderr="")

        self.assertEqual(server.sync_logic.save_preview_diff("/repo", "/preview", run_command), diff)
        self.assertNotIn("Diff truncated", server.sync_logic.save_preview_diff("/repo", "/preview", run_command))

    def test_save_preview_ignores_registry_order_only_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            preview = root / "preview"
            repo_storage = repo / "homeassistant" / ".storage"
            preview_storage = preview / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            preview_storage.mkdir(parents=True)
            repo_registry = {
                "data": {
                    "devices": [
                        {
                            "id": "device-1",
                            "connections": [["b", "2"], ["a", "1"]],
                            "config_entries_subentries": {"entry": [None, "b", "a"]},
                        },
                        {"id": "device-2", "connections": []},
                    ]
                }
            }
            preview_registry = {
                "data": {
                    "devices": [
                        {"id": "device-2", "connections": []},
                        {
                            "id": "device-1",
                            "connections": [["a", "1"], ["b", "2"]],
                            "config_entries_subentries": {"entry": ["a", "b", None]},
                        },
                    ]
                }
            }
            (repo_storage / "core.device_registry").write_text(json.dumps(repo_registry))
            (preview_storage / "core.device_registry").write_text(json.dumps(preview_registry))

            self.assertEqual(server.sync_logic.save_preview_status_lines(repo, preview), [])

    def test_save_preview_ignores_registry_volatile_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            preview = root / "preview"
            repo_storage = repo / "homeassistant" / ".storage"
            preview_storage = preview / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            preview_storage.mkdir(parents=True)
            repo_device = {"data": {"devices": [{"id": "device-1", "modified_at": "old"}]}}
            preview_device = {"data": {"devices": [{"id": "device-1", "modified_at": "new"}]}}
            repo_entity = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "platform": "mqtt",
                            "suggested_object_id": "test",
                            "modified_at": "old",
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "platform": "mobile_app",
                            "original_icon": "mdi:battery-10",
                            "modified_at": "old",
                        },
                    ]
                }
            }
            preview_entity = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "platform": "mqtt",
                            "suggested_object_id": "test_2",
                            "modified_at": "new",
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "platform": "mobile_app",
                            "original_icon": "mdi:battery-90",
                            "modified_at": "new",
                        },
                    ]
                }
            }
            (repo_storage / "core.device_registry").write_text(json.dumps(repo_device))
            (preview_storage / "core.device_registry").write_text(json.dumps(preview_device))
            (repo_storage / "core.entity_registry").write_text(json.dumps(repo_entity))
            (preview_storage / "core.entity_registry").write_text(json.dumps(preview_entity))

            self.assertEqual(server.sync_logic.save_preview_status_lines(repo, preview), [])

    def test_save_preview_keeps_real_registry_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            preview = root / "preview"
            repo_storage = repo / "homeassistant" / ".storage"
            preview_storage = preview / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            preview_storage.mkdir(parents=True)
            repo_device = {
                "data": {
                    "devices": [{"id": "device-1", "connections": [["a", "1"]], "sw_version": "1"}],
                    "deleted_devices": [{"id": "deleted-1"}],
                }
            }
            preview_device = {
                "data": {
                    "devices": [{"id": "device-1", "connections": [["a", "1"], ["b", "2"]], "sw_version": "2"}],
                    "deleted_devices": [],
                }
            }
            repo_entity = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "media_player.radio",
                            "capabilities": {"source_list": ["A", "B"]},
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.test",
                            "platform": "mqtt",
                            "disabled_by": "integration",
                            "options": {},
                        },
                        {
                            "id": "entity-3",
                            "entity_id": "sensor.icon",
                            "platform": "mqtt",
                            "original_icon": "mdi:a",
                        },
                    ],
                    "deleted_entities": [{"id": "deleted-entity-1"}],
                }
            }
            preview_entity = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "media_player.radio",
                            "capabilities": {"source_list": ["A"]},
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.test",
                            "platform": "mqtt",
                            "disabled_by": None,
                            "options": {"conversation": {"should_expose": False}},
                        },
                        {
                            "id": "entity-3",
                            "entity_id": "sensor.icon",
                            "platform": "mqtt",
                            "original_icon": "mdi:b",
                        },
                    ],
                    "deleted_entities": [],
                }
            }
            (repo_storage / "core.device_registry").write_text(json.dumps(repo_device))
            (preview_storage / "core.device_registry").write_text(json.dumps(preview_device))
            (repo_storage / "core.entity_registry").write_text(json.dumps(repo_entity))
            (preview_storage / "core.entity_registry").write_text(json.dumps(preview_entity))

            self.assertEqual(
                server.sync_logic.save_preview_status_lines(repo, preview),
                [
                    "- Modified: homeassistant/.storage/core.device_registry",
                    "- Modified: homeassistant/.storage/core.entity_registry",
                ],
            )

    def test_save_restores_registry_noise_only_worktree_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            self.git(["init", str(repo)], root)
            self.git(["checkout", "-b", "main"], repo)
            storage = repo / "homeassistant" / ".storage"
            storage.mkdir(parents=True)
            committed_registry = {
                "data": {
                    "devices": [
                        {"id": "device-1", "connections": [["b", "2"], ["a", "1"]]},
                        {"id": "device-2", "connections": []},
                    ]
                }
            }
            exported_registry = {
                "data": {
                    "devices": [
                        {"id": "device-2", "connections": []},
                        {"id": "device-1", "connections": [["a", "1"], ["b", "2"]]},
                    ]
                }
            }
            registry_path = storage / "core.device_registry"
            registry_path.write_text(json.dumps(committed_registry))
            self.git_commit_all(repo, "base")
            registry_path.write_text(json.dumps(exported_registry))

            class Ctx:
                def run_command(self, args, cwd=None):
                    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)

                def add_detail(self, details, detail):
                    details.append(detail)

            details = []
            restored = server.sync_logic.restore_normalized_equal_save_worktree(
                repo,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                details,
                Ctx(),
            )

            self.assertEqual(restored, ["homeassistant/.storage/core.device_registry"])
            self.assertEqual(self.git(["status", "--porcelain"], repo).stdout.strip(), "")

    def test_sync_code_has_no_diff_truncation_marker(self):
        sync_source = (ROOT / "app" / "sync.py").read_text()

        self.assertNotIn("Diff truncated", sync_source)

    def test_save_conflict_approve_all_records_ha_resolutions(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = server.app_context.AppContext(data_dir=root / "data", config_dir=root / "ha", addon_configs_dir=root / "addons")
            ctx.write_state(
                {
                    "conflicts": ["homeassistant/.storage/core.device_registry"],
                    "conflict_type": "save_unknown_base",
                    "save_conflict_resolutions": {},
                }
            )

            message = server.conflicts.approve_save_unknown_base_conflicts(ctx)
            state = ctx.read_state()

            self.assertIn("Approved 1 Save conflict", message)
            self.assertEqual(state["conflicts"], [])
            self.assertEqual(state["save_conflict_resolutions"], {"homeassistant/.storage/core.device_registry": "ha"})

    def test_web_handler_uses_context_for_health_and_post_actions(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.calls = []

            def run_save_job(self):
                self.calls.append("save")

            def run_save_preview_job(self):
                self.calls.append("save-preview")

            def run_deleted_devices_preview_job(self):
                self.calls.append("deleted-devices-preview")

            def run_deleted_devices_delete_job(self):
                self.calls.append("deleted-devices-delete")

            def run_deleted_devices_confirm_job(self):
                self.calls.append("deleted-devices-confirm")

            def run_deleted_devices_revert_job(self):
                self.calls.append("deleted-devices-revert")

            def clear_display_state(self):
                self.calls.append("clear-display")

            def set_homeassistant_organizer_enabled(self, enabled):
                self.calls.append(("organizer", enabled))

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)

        def invoke(method, path, body=b"", headers=None):
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            for key, value in (headers or {}).items():
                request.headers[key] = value
            if body and "Content-Length" not in request.headers:
                request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            getattr(request, method)()
            return request

        get_request = invoke("do_GET", "/health")
        self.assertEqual(get_request.responses[-1], 200)
        self.assertEqual(json.loads(get_request.wfile.getvalue().decode()), {"ok": True})

        post_request = invoke(
            "do_POST",
            "/save",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Save HA to Git started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, ["save"])

        post_request = invoke(
            "do_POST",
            "/save-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("HA to Git preview started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, ["save", "save-preview"])

        post_request = invoke(
            "do_POST",
            "/deleted-devices-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted_devices check started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, ["save", "save-preview", "deleted-devices-preview"])

        post_request = invoke(
            "do_POST",
            "/deleted-devices-delete",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted_devices deletion started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, ["save", "save-preview", "deleted-devices-preview", "deleted-devices-delete"])

        post_request = invoke(
            "do_POST",
            "/deleted-devices-confirm",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted_devices cleanup confirmation started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            ["save", "save-preview", "deleted-devices-preview", "deleted-devices-delete", "deleted-devices-confirm"],
        )

        post_request = invoke(
            "do_POST",
            "/deleted-devices-revert",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted_devices cleanup revert started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                "save",
                "save-preview",
                "deleted-devices-preview",
                "deleted-devices-delete",
                "deleted-devices-confirm",
                "deleted-devices-revert",
            ],
        )

        post_request = invoke(
            "do_POST",
            "/clear-display-state",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Display state cleared", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                "save",
                "save-preview",
                "deleted-devices-preview",
                "deleted-devices-delete",
                "deleted-devices-confirm",
                "deleted-devices-revert",
                "clear-display",
            ],
        )

        post_request = invoke(
            "do_POST",
            "/homeassistant-organizer",
            body=b"homeassistant_organizer=1",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Home Assistant Git layout updated", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                "save",
                "save-preview",
                "deleted-devices-preview",
                "deleted-devices-delete",
                "deleted-devices-confirm",
                "deleted-devices-revert",
                "clear-display",
                ("organizer", True),
            ],
        )

    def test_empty_git_preview_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            (live / "configuration.yaml").write_text("homeassistant:\n")
            source = root / "repo" / "homeassistant"
            preview = server.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                    }
                ]
            )
            self.assertEqual(preview["deletions"], 0)
            self.assertIn("no file changes", preview["diff"].lower())
            self.assertEqual((live / "configuration.yaml").read_text(), "homeassistant:\n")

    def test_apply_preview_progress_is_written_to_state_details(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            (live / "configuration.yaml").write_text("homeassistant:\n")
            source = root / "repo" / "homeassistant"
            details = []

            server._CTX.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                    }
                ],
                details,
            )

            state_details = server.read_state()["last_details"]
            self.assertIn("Preview homeassistant: start", details)
            self.assertIn("Preview homeassistant: building diff", state_details)

    def test_missing_git_source_does_not_delete_live_config(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            (live / "configuration.yaml").write_text("homeassistant:\n")
            server.apply_homeassistant_config(root / "missing", live, {"id": "homeassistant"})
            self.assertEqual((live / "configuration.yaml").read_text(), "homeassistant:\n")

    def test_apply_preview_shows_protected_storage_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.device_registry").write_text("live\n")
            (source / ".storage" / "core.device_registry").write_text("git\n")
            (source / ".storage" / "input_boolean").write_text("input\n")

            preview = server.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                    }
                ]
            )
            preview_storage = server.WORK_DIR / "apply-preview" / "homeassistant" / ".storage"
            self.assertEqual((preview_storage / "core.device_registry").read_text(), "git\n")
            self.assertEqual((preview_storage / "input_boolean").read_text(), "input\n")
            self.assertEqual(preview["skipped_protected"], [])
            self.assertTrue(preview["storage_changes"])
            self.assertIn("homeassistant/.storage/core.device_registry", preview["storage_change_paths"])

    def test_default_manifest_uses_selected_addons(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            manifest = server.default_manifest({"apply_path": "homeassistant"})
            targets = manifest["targets"]
            self.assertEqual(targets[0]["type"], "homeassistant")
            self.assertEqual(targets[1]["addon_slug"], "local_zigbee2mqtt")
            self.assertEqual(targets[1]["source"], "addons/local_zigbee2mqtt")
            self.assertFalse(targets[1]["delete"])

    def test_default_manifest_uses_homeassistant_organizer_ui_preference(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertNotIn("organizer", manifest["targets"][0])

            server.set_homeassistant_organizer_enabled(True)
            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertEqual(manifest["targets"][0]["organizer"], {"enabled": True})

            server.set_homeassistant_organizer_enabled(False)
            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertFalse(manifest["targets"][0]["organizer"])

    def test_loaded_manifest_keeps_organizer_until_ui_preference_is_set(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo.mkdir()
            (repo / "ha-ops.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "targets": [
                            {
                                "id": "homeassistant",
                                "type": "homeassistant",
                                "source": "homeassistant",
                                "organizer": {"enabled": True, "organized_root": ".custom"},
                            }
                        ],
                    }
                )
            )

            manifest, _path = server.load_manifest(repo, {"manifest_path": "ha-ops.json"}, [])
            self.assertEqual(
                manifest["targets"][0]["organizer"],
                {"enabled": True, "organized_root": ".custom"},
            )

            server.set_homeassistant_organizer_enabled(False)
            manifest, _path = server.load_manifest(repo, {"manifest_path": "ha-ops.json"}, [])
            self.assertFalse(manifest["targets"][0]["organizer"])

            server.set_homeassistant_organizer_enabled(True)
            manifest, _path = server.load_manifest(repo, {"manifest_path": "ha-ops.json"}, [])
            self.assertEqual(
                manifest["targets"][0]["organizer"],
                {"enabled": True, "organized_root": ".custom"},
            )

    def test_policy_booleans_are_centralized_for_manifest_and_targets(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            source = repo / "homeassistant"
            source.mkdir(parents=True)
            manifest = {
                "targets": [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source": "homeassistant",
                        "restart_after_apply": "true",
                    }
                ]
            }

            target = server.resolve_targets(repo, manifest, [], require_source=True)[0]

            self.assertTrue(target["restart_after_sync"])
            self.assertTrue(target["restart_core_after_apply"])
            self.assertTrue(target["start_core_after_storage_apply"])
            self.assertTrue(target["restart_core_after_rollback"])
            self.assertTrue(target["start_core_after_storage_rollback"])
            self.assertTrue(server.target_restore_delete({"delete": "true"}))
            self.assertFalse(server.target_apply_delete({"delete": "false"}))
            self.assertFalse(server.target_save_delete({"save_delete": "false"}))
            self.assertFalse(server.target_restore_delete({"restore_delete": "false", "delete": "true"}))

    def test_save_ha_to_git_initializes_empty_repo(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            self.assertTrue(server.run_save_job())
            state = server.read_state()
            details = "\n".join(server.read_state()["last_details"])
            self.assertIn("Git changes prepared for commit (1):", details)
            self.assertIn("- Added: homeassistant/configuration.yaml", details)
            self.assertEqual(state["last_message"], "Save finished successfully and pushed to Git.")
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("homeassistant/configuration.yaml", result.stdout)

    def test_save_ha_to_git_uses_homeassistant_organizer_ui_toggle(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("- id: live_auto\n  alias: Live Auto\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text("{}\n")
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.area_registry").write_text(
                json.dumps({"data": {"areas": [{"id": "home", "name": "Home"}]}})
            )
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "automation.live_auto",
                                    "unique_id": "live_auto",
                                    "area_id": "home",
                                }
                            ]
                        }
                    }
                )
            )
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.set_homeassistant_organizer_enabled(True)

            self.assertTrue(server.run_save_job())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertIn("homeassistant/.ha-ops/areas/home/automations.yaml", result.stdout)
            self.assertNotIn("homeassistant/automations.yaml", result.stdout)

    def test_save_unknown_base_blocks_same_file_difference(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertFalse(server.run_save_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "conflicts")
            self.assertEqual(state["conflict_type"], "save_unknown_base")
            self.assertEqual(state["conflicts"], ["homeassistant/configuration.yaml"])
            details = "\n".join(state["last_details"])
            self.assertIn("Save export candidates for homeassistant (1):", details)
            self.assertIn("- homeassistant/configuration.yaml", details)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")
            page = server.render_page()
            self.assertIn('<div class="badge conflicts">conflicts</div>', page)
            self.assertNotIn('<div class="badge error">error</div>', page)
            self.assertIn("Git: homeassistant/configuration.yaml", page)
            self.assertIn("HA: homeassistant/configuration.yaml", page)
            self.assertIn("diff-changed", page)
            self.assertIn("git", page)
            self.assertIn("ha", page)

    def test_save_unknown_base_use_git_keeps_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertFalse(server.run_save_job())
            message = server.resolve_git_conflict("homeassistant/configuration.yaml", "git")
            self.assertIn("Run Save HA to Git again", message)
            self.assertIn("Save export candidates for homeassistant (1):", "\n".join(server.read_state()["last_details"]))
            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_save_unknown_base_use_ha_overwrites_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertFalse(server.run_save_job())
            message = server.resolve_git_conflict("homeassistant/configuration.yaml", "ha")
            self.assertIn("Run Save HA to Git again", message)
            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")

    def test_save_unknown_base_allows_same_file_same_content(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "same\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("same\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_job())
            state = server.read_state()
            self.assertEqual(state["conflicts"], [])
            self.assertEqual(state["last_message"], "No live Home Assistant changes to save.")
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "same\n")

    def test_save_export_failure_does_not_dirty_checkout(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
            repo = server.DATA_DIR / "ha-config"
            self.git(["clone", str(remote), str(repo)], root)
            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "new.yaml").write_text("new\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            original_run_command = server.run_command

            def fail_save_export(command, env=None, cwd=None):
                if command and command[0] == "rsync" and any("save-export" in str(item) for item in command):
                    return subprocess.CompletedProcess(command, 1, "", "export failed")
                return original_run_command(command, env=env, cwd=cwd)

            server.run_command = fail_save_export

            self.assertFalse(server.run_save_job())
            self.assertEqual(self.repo_status(repo), "")
            self.assertFalse((repo / "homeassistant" / "packages" / "new.yaml").exists())

    def test_save_stage_failure_cleans_partial_checkout_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
            repo = server.DATA_DIR / "ha-config"
            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "new.yaml").write_text("new\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.stage_all = lambda repo_dir: (_ for _ in ()).throw(RuntimeError("stage failed"))

            self.assertFalse(server.run_save_job())
            self.assertEqual(self.repo_status(repo), "")
            self.assertFalse((repo / "homeassistant" / "packages" / "new.yaml").exists())
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "base\n")

    def test_save_exports_managed_config_entries_projection_when_storage_ignored(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / ".gitignore").write_text("homeassistant/.storage/\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / ".storage").mkdir()
            (server.CONFIG_DIR / ".storage" / "core.config_entries").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "data": {
                            "entries": [
                                {
                                    "domain": "workday",
                                    "entry_id": "workday-id",
                                    "source": "user",
                                    "title": "Workday",
                                    "unique_id": None,
                                    "data": {},
                                    "options": {"country": "CZ", "workdays": ["mon", "tue"]},
                                    "modified_at": "runtime",
                                },
                                {
                                    "domain": "google",
                                    "entry_id": "google-id",
                                    "source": "user",
                                    "title": "alex@example.com",
                                    "unique_id": "alex@example.com",
                                    "data": {"token": {"access_token": "secret"}},
                                    "options": {"calendar_access": "read_write"},
                                },
                            ]
                        },
                    }
                )
            )
            (server.CONFIG_DIR / ".storage" / "input_boolean").write_text("safe\n")
            (server.CONFIG_DIR / ".storage" / "auth").write_text("secret\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "homeassistant/.storage/input_boolean"), "safe\n")
            projection = json.loads(self.remote_file(remote, "homeassistant/.storage_managed/core.config_entries.json"))
            self.assertEqual(projection["source"], "core.config_entries")
            workday = next(entry for entry in projection["entries"] if entry["domain"] == "workday")
            google = next(entry for entry in projection["entries"] if entry["domain"] == "google")
            self.assertEqual(workday["apply"], "update")
            self.assertEqual(workday["options"], {"country": "CZ", "workdays": ["mon", "tue"]})
            self.assertEqual(google["apply"], "ignore")
            self.assertEqual(google["data"], {})
            self.assertNotIn("secret", json.dumps(projection))
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.storage/core.config_entries", result.stdout)
            self.assertNotIn("homeassistant/.storage/auth", result.stdout)

    def test_save_homeassistant_preserves_git_only_files_outside_managed_paths(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / "docs").mkdir(parents=True)
            (seed / "homeassistant" / "packages").mkdir()
            (seed / "homeassistant" / "README.md").write_text("manual\n")
            (seed / "homeassistant" / "docs" / "note.txt").write_text("manual\n")
            (seed / "homeassistant" / "old.yaml").write_text("stale\n")
            (seed / "homeassistant" / "packages" / "stale.yaml").write_text("stale\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "current.yaml").write_text("current\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "save_conflict_resolutions": {
                        "homeassistant/old.yaml": "ha",
                        "homeassistant/packages/stale.yaml": "ha",
                    }
                }
            )

            self.assertTrue(server.run_save_job())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertIn("homeassistant/README.md", result.stdout)
            self.assertIn("homeassistant/docs/note.txt", result.stdout)
            self.assertIn("homeassistant/configuration.yaml", result.stdout)
            self.assertIn("homeassistant/packages/current.yaml", result.stdout)
            self.assertNotIn("homeassistant/old.yaml", result.stdout)
            self.assertNotIn("homeassistant/packages/stale.yaml", result.stdout)

    def test_empty_git_apply_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                        "require_fresh_backup": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job())
            self.assertTrue(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "homeassistant:\n")

    def test_repo_path_rejects_empty_absolute_and_parent_escape(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            for value in ["", " ", ".", "/tmp/ha-config", "../ha-config", "ha-config/../other"]:
                with self.subTest(repo_path=value):
                    with self.assertRaises(RuntimeError):
                        server.repo_checkout_path({"repo_path": value})

            self.assertEqual(
                server.repo_checkout_path({"repo_path": "ha-config"}),
                (server.DATA_DIR / "ha-config").resolve(),
            )

    def test_invalid_repo_path_does_not_clean_external_checkout(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            outside = root / "outside"
            self.git(["init", str(outside)], root)
            marker = outside / "keep-me.txt"
            marker.write_text("untracked\n")

            with self.assertRaises(RuntimeError):
                server.ensure_repo({"repo_path": str(outside), "repo_url": "unused"})

            self.assertTrue(marker.exists())

    def test_preview_ignores_untracked_checkout_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            repo = server.DATA_DIR / "ha-config"
            self.git(["clone", str(remote), str(repo)], root)
            stale = repo / "homeassistant" / "configuration.yaml"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale:\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("live:\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_preview_job())
            state = server.read_state()
            self.assertIn("no file changes", state["last_diff"].lower())
            self.assertFalse(stale.exists())

    def test_live_only_addon_absent_from_git_is_not_deleted(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")

            server.apply_targets(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(root / "repo" / "addons" / "local_zigbee2mqtt"),
                        "live_path": str(live),
                        "restart_after_sync": True,
                    }
                ],
                [],
            )
            self.assertEqual((live / "configuration.yaml").read_text(), "live\n")

    def test_partial_addon_git_source_does_not_delete_live_only_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            (live / "database.db").write_text("live-only\n")

            server.apply_targets(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "restart_after_sync": False,
                    }
                ],
                [],
            )

            self.assertEqual((live / "configuration.yaml").read_text(), "git\n")
            self.assertEqual((live / "database.db").read_text(), "live-only\n")

    def test_explicit_addon_delete_removes_live_only_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            (live / "database.db").write_text("live-only\n")
            (live / "extra.yaml").write_text("live-only\n")

            server.apply_targets(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "restart_after_sync": False,
                        "delete": True,
                    }
                ],
                [],
            )

            self.assertEqual((live / "configuration.yaml").read_text(), "git\n")
            self.assertEqual((live / "database.db").read_text(), "live-only\n")
            self.assertFalse((live / "extra.yaml").exists())

    def test_addon_apply_ignores_excluded_runtime_files_from_git(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            (source / "database.db").write_text("git-runtime\n")
            (source / "home-assistant.log").write_text("git-log\n")
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            (live / "database.db").write_text("live-runtime\n")

            server.apply_targets(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "restart_after_sync": False,
                        "delete": True,
                    }
                ],
                [],
            )

            self.assertEqual((live / "configuration.yaml").read_text(), "git\n")
            self.assertEqual((live / "database.db").read_text(), "live-runtime\n")
            self.assertFalse((live / "home-assistant.log").exists())

    def test_core_check_runs_before_start_when_storage_stops_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            (source / ".storage").mkdir(parents=True)
            (source / ".storage" / "input_boolean").write_text("{}\n")
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_sync_if_storage": True,
                        "restart_after_sync": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["stop", "check", "start"])

    def test_core_check_failure_prevents_start_after_storage_sync(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            (source / ".storage").mkdir(parents=True)
            (source / ".storage" / "input_boolean").write_text("{}\n")
            events = []
            server.core_stop = lambda: events.append("stop")

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check
            server.core_start = lambda: events.append("start")

            with self.assertRaises(RuntimeError):
                server.apply_targets(
                    [
                        {
                            "id": "homeassistant",
                            "type": "homeassistant",
                            "source_path": str(source),
                            "live_path": str(server.CONFIG_DIR),
                            "stop_core_before_sync_if_storage": True,
                            "restart_after_sync": True,
                        }
                    ],
                    [],
                )

            self.assertEqual(events, ["stop", "check"])

    def test_yaml_apply_reloads_without_restart_by_default(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            events = []
            server.do_core_check = lambda: events.append("check")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "reload"])

    def test_yaml_apply_can_explicitly_restart_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            events = []
            server.do_core_check = lambda: events.append("check")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "restart_core_after_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "restart"])

    def test_homeassistant_directory_apply_preserves_live_only_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            (source / "packages").mkdir(parents=True)
            (source / "packages" / "git.yaml").write_text("git\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "live-only.yaml").write_text("live\n")

            server.apply_homeassistant_config(
                source,
                server.CONFIG_DIR,
                {"id": "homeassistant"},
            )

            self.assertEqual((server.CONFIG_DIR / "packages" / "git.yaml").read_text(), "git\n")
            self.assertEqual((server.CONFIG_DIR / "packages" / "live-only.yaml").read_text(), "live\n")

    def test_selected_addon_is_saved_to_git(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            addon_live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            addon_live.mkdir()
            (addon_live / "configuration.yaml").write_text("addon\n")
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}]

            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "addons/local_zigbee2mqtt/configuration.yaml"), "addon\n")

    def test_save_does_not_commit_untracked_checkout_junk(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
            repo = server.DATA_DIR / "ha-config"
            self.git(["clone", str(remote), str(repo)], root)
            (repo / "stale.txt").write_text("stale\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_job())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("stale.txt", result.stdout)

    def test_save_retries_unpushed_local_commit_when_no_new_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "new.yaml").write_text("homeassistant:\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_first_push(repo_dir, env, branch):
                calls["count"] += 1
                if calls["count"] <= 2:
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_push

            self.assertFalse(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "base\n")

            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")
            self.assertGreaterEqual(calls["count"], 2)

    def test_selected_addon_is_saved_when_manifest_exists(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "ha-ops.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "targets": [
                            {
                                "id": "homeassistant",
                                "type": "homeassistant",
                                "source": "homeassistant",
                                "delete": False,
                            }
                        ],
                    }
                )
            )
            self.git_commit_all(seed, "manifest")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            addon_live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            addon_live.mkdir()
            (addon_live / "configuration.yaml").write_text("addon\n")
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}]

            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "addons/local_zigbee2mqtt/configuration.yaml"), "addon\n")

    def test_selected_addon_with_gitkeep_source_is_saved_from_live(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            gitkeep = seed / "addons" / "local_zigbee2mqtt" / ".gitkeep"
            gitkeep.parent.mkdir(parents=True)
            gitkeep.write_text("")
            self.git_commit_all(seed, "scaffold addon")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            addon_live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            addon_live.mkdir()
            (addon_live / "configuration.yaml").write_text("addon\n")
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}]

            self.assertTrue(server.run_save_job())
            self.assertEqual(self.remote_file(remote, "addons/local_zigbee2mqtt/configuration.yaml"), "addon\n")

    def test_unchecked_manifest_addon_is_excluded(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo.mkdir()
            (repo / "ha-ops.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "targets": [
                            {
                                "id": "homeassistant",
                                "type": "homeassistant",
                                "source": "homeassistant",
                            },
                            {
                                "id": "addon-local_zigbee2mqtt",
                                "type": "addon",
                                "source": "addons/local_zigbee2mqtt",
                                "addon_slug": "local_zigbee2mqtt",
                            },
                        ],
                    }
                )
            )

            manifest, _path = server.load_manifest(
                repo,
                {"manifest_path": "ha-ops.json"},
                [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}],
            )

            self.assertEqual([target["type"] for target in manifest["targets"]], ["homeassistant"])

    def test_selected_manifest_addon_preserves_manifest_options(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo.mkdir()
            (repo / "ha-ops.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "targets": [
                            {
                                "id": "custom-z2m",
                                "type": "addon",
                                "source": "custom/z2m",
                                "addon_slug": "local_zigbee2mqtt",
                                "stop_addon_before_sync": True,
                            }
                        ],
                    }
                )
            )
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})

            manifest, _path = server.load_manifest(
                repo,
                {"manifest_path": "ha-ops.json"},
                [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}],
            )

            self.assertEqual(len(manifest["targets"]), 1)
            self.assertEqual(manifest["targets"][0]["source"], "custom/z2m")
            self.assertTrue(manifest["targets"][0]["stop_addon_before_sync"])

    def test_zigbee2mqtt_non_default_slug_uses_existing_config_path(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            z2m_path = server.CONFIG_DIR / "zigbee2mqtt"
            z2m_path.mkdir()
            addons = [{"slug": "local_z2m_custom", "name": "Zigbee2MQTT Edge"}]
            target = {
                "id": "addon-local_z2m_custom",
                "type": "addon",
                "source": "addons/local_z2m_custom",
                "addon_slug": "local_z2m_custom",
                "optional": True,
            }

            self.assertTrue(server.addon_is_zigbee2mqtt(addons[0]))
            resolved = server.resolve_targets(root / "repo", {"targets": [target]}, addons, require_source=False)
            self.assertEqual(resolved[0]["live_path"], str(z2m_path))

    def test_conflict_resolution_can_use_ha_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.make_rebase_conflict(server, root)

            message = server.resolve_git_conflict("homeassistant/configuration.yaml", "ha")
            self.assertIn("All conflicts resolved", message)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")

    def test_conflict_resolution_can_use_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.make_rebase_conflict(server, root)

            message = server.resolve_git_conflict("homeassistant/configuration.yaml", "git")
            self.assertIn("All conflicts resolved", message)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_conflict_resolution_retries_push_after_rebase_continued(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.make_rebase_conflict(server, root)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_first_push(repo_dir, env, branch):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_push

            with self.assertRaises(RuntimeError):
                server.resolve_git_conflict("homeassistant/configuration.yaml", "ha")
            self.assertEqual(server.git_conflict_paths(server.DATA_DIR / "ha-config"), [])
            self.assertEqual(server.read_state()["conflicts"], ["homeassistant/configuration.yaml"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

            message = server.resolve_git_conflict("homeassistant/configuration.yaml", "ha")
            self.assertIn("All conflicts resolved", message)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")
            self.assertEqual(server.read_state()["conflicts"], [])
            self.assertEqual(calls["count"], 2)

    def test_rebase_conflict_ui_shows_conflict_markers(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            self.make_rebase_conflict(server, root)

            page = server.render_page()

            self.assertIn("&lt;&lt;&lt;&lt;&lt;&lt;&lt;", page)
            self.assertIn("=======", page)
            self.assertIn("&gt;&gt;&gt;&gt;&gt;&gt;&gt;", page)

    def test_backup_gate_blocks_when_backup_is_missing_and_creation_disabled(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.latest_system_backup_status = lambda options: {
                "stale": True,
                "max_age_hours": 24,
                "message": "No system Home Assistant backups found.",
            }
            with self.assertRaises(RuntimeError):
                server.ensure_fresh_system_backup(
                    {"require_fresh_backup": True, "create_ha_backup": False},
                    [],
                )

    def test_latest_backup_accepts_homeassistant_automatic_backup_with_local_location(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            backup_date = (datetime.now(timezone.utc) - timedelta(hours=19)).replace(microsecond=0).isoformat()
            server.backup_manager_info = lambda: {
                "backups": [
                    {
                        "slug": "automatic",
                        "name": "Automatic backup",
                        "date": backup_date,
                        "type": "partial",
                        "content": {"homeassistant": True},
                        "location": None,
                    }
                ]
            }

            status = server.latest_system_backup_status({"backup_max_age_hours": 24, "backup_require_location": True})

            self.assertFalse(status["stale"])
            self.assertEqual(status["backup"]["slug"], "automatic")
            self.assertIn("1 location", status["message"])

    def test_pending_conflicts_block_apply(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"conflicts": ["homeassistant/configuration.yaml"]})

            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "conflicts")
            self.assertIn("Resolve Git conflicts", state["last_message"])

    def test_selected_addon_delete_true_preview_counts_managed_live_only_deletion(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            (live / "database.db").write_text("live-only\n")
            (live / "extra.yaml").write_text("live-only\n")

            preview = server.build_apply_preview(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": True,
                    }
                ]
            )

            self.assertEqual(preview["deletions"], 1)
            self.assertIn("extra.yaml", preview["diff"])
            preview_file = server.WORK_DIR / "apply-preview" / "addon-local_zigbee2mqtt" / "database.db"
            self.assertFalse(preview_file.exists())
            self.assertNotIn("database.db", preview["diff"])

    def test_selected_addon_delete_false_preview_preserves_live_only_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            (live / "database.db").write_text("live-only\n")

            preview = server.build_apply_preview(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                    }
                ]
            )

            self.assertEqual(preview["deletions"], 0)
            preview_file = server.WORK_DIR / "apply-preview" / "addon-local_zigbee2mqtt" / "database.db"
            self.assertFalse(preview_file.exists())
            self.assertNotIn("database.db", preview["diff"])

    def test_save_delete_delete_and_restore_delete_are_independent(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            source.mkdir(parents=True)
            (source / "repo-only.txt").write_text("keep\n")

            target = {
                "id": "addon-local_zigbee2mqtt",
                "type": "addon",
                "resolved_slug": "local_zigbee2mqtt",
                "source_path": str(source),
                "live_path": str(live),
                "delete": True,
                "save_delete": False,
                "restore_delete": False,
            }

            server.export_targets([target], [])
            self.assertTrue((source / "repo-only.txt").exists())
            release = server.create_release_snapshot([target], "abc123", None)
            metadata = json.loads((server.RELEASES_DIR / release / "release.json").read_text())
            self.assertFalse(metadata["targets"][0]["delete"])
            self.assertTrue(server.target_apply_delete(target))
            self.assertFalse(server.target_save_delete(target))
            self.assertFalse(server.target_restore_delete(target))

    def test_addon_save_recursively_removes_excluded_destination_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            live.mkdir()
            (live / "configuration.yaml").write_text("live\n")
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "old.db").write_text("old\n")
            (source / "nested" / "old.log").write_text("old\n")

            server.export_targets(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(source),
                        "live_path": str(live),
                        "save_delete": False,
                    }
                ],
                [],
            )

            self.assertFalse((source / "nested" / "old.db").exists())
            self.assertFalse((source / "nested" / "old.log").exists())
            self.assertEqual((source / "configuration.yaml").read_text(), "live\n")

    def test_allow_protected_storage_true_applies_protected_storage(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.device_registry").write_text("live\n")
            (source / ".storage" / "core.device_registry").write_text("git\n")

            skipped = server.apply_homeassistant_config(
                source,
                live,
                {"id": "homeassistant", "allow_protected_storage": True},
            )

            self.assertEqual(skipped, [])
            self.assertEqual((live / ".storage" / "core.device_registry").read_text(), "git\n")

    def test_allow_protected_storage_false_applies_safe_storage_only(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.device_registry").write_text("live\n")
            (source / ".storage" / "core.device_registry").write_text("git\n")
            (source / ".storage" / "input_boolean").write_text("safe\n")

            skipped = server.apply_homeassistant_config(
                source,
                live,
                {"id": "homeassistant", "allow_protected_storage": False},
            )

            self.assertEqual(skipped, ["core.device_registry"])
            self.assertEqual((live / ".storage" / "core.device_registry").read_text(), "live\n")
            self.assertEqual((live / ".storage" / "input_boolean").read_text(), "safe\n")

    def test_apply_blocks_storage_changes_until_approved(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / ".storage").mkdir(parents=True)
            (seed / "homeassistant" / ".storage" / "input_boolean").write_text("git-storage\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            (server.CONFIG_DIR / ".storage").mkdir(parents=True)
            (server.CONFIG_DIR / ".storage" / "input_boolean").write_text("live-storage\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job())
            state = server.read_state()
            self.assertTrue(state["last_preview_storage_changes"])
            self.assertIn("Approve Git to HA", server.render_page())

            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")
            self.assertIn("Approve Git to HA", server.read_state()["last_message"])

            server.write_state({"last_preview_approved_fingerprint": state["last_preview_fingerprint"]})
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "git-storage\n")

    def test_managed_config_entries_projection_updates_safe_fields_only(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage_managed").mkdir(parents=True)
            raw = {
                "version": 1,
                "data": {
                    "entries": [
                        {
                            "domain": "workday",
                            "entry_id": "workday-id",
                            "source": "user",
                            "title": "Workday",
                            "unique_id": None,
                            "data": {"keep": "live"},
                            "options": {"country": "US", "language": "en"},
                            "modified_at": "runtime",
                        },
                        {
                            "domain": "google",
                            "entry_id": "google-id",
                            "source": "user",
                            "title": "Google",
                            "unique_id": "alex@example.com",
                            "data": {"token": {"access_token": "live-token"}},
                            "options": {"calendar_access": "read_write"},
                        },
                    ]
                },
            }
            projection = {
                "version": 1,
                "source": "core.config_entries",
                "entries": [
                    {
                        "domain": "workday",
                        "entry_id": "workday-id",
                        "source": "user",
                        "title": "Workday",
                        "unique_id": None,
                        "apply": "update",
                        "data": {},
                        "options": {"country": "CZ"},
                    },
                    {
                        "domain": "google",
                        "entry_id": "google-id",
                        "source": "user",
                        "title": "Google",
                        "unique_id": "alex@example.com",
                        "apply": "update",
                        "data": {"token": {"access_token": "git-token"}},
                        "options": {"calendar_access": "read_only"},
                    },
                ],
            }
            (live / ".storage" / "core.config_entries").write_text(json.dumps(raw))
            (source / ".storage_managed" / "core.config_entries.json").write_text(json.dumps(projection))

            skipped = server.apply_homeassistant_config(source, live, {"id": "homeassistant"})

            updated = json.loads((live / ".storage" / "core.config_entries").read_text())
            entries = {entry["entry_id"]: entry for entry in updated["data"]["entries"]}
            self.assertEqual(skipped, [])
            self.assertEqual(entries["workday-id"]["options"]["country"], "CZ")
            self.assertEqual(entries["workday-id"]["options"]["language"], "en")
            self.assertEqual(entries["workday-id"]["data"], {"keep": "live"})
            self.assertEqual(entries["google-id"]["data"]["token"]["access_token"], "live-token")
            self.assertEqual(entries["google-id"]["options"]["calendar_access"], "read_write")

    def test_noop_managed_config_entries_projection_does_not_stop_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage_managed").mkdir(parents=True)
            raw = {
                "version": 1,
                "data": {
                    "entries": [
                        {
                            "domain": "workday",
                            "entry_id": "workday-id",
                            "source": "user",
                            "title": "Workday",
                            "unique_id": None,
                            "data": {},
                            "options": {"country": "CZ"},
                        }
                    ]
                },
            }
            projection = {
                "version": 1,
                "source": "core.config_entries",
                "entries": [
                    {
                        "domain": "workday",
                        "entry_id": "workday-id",
                        "source": "user",
                        "title": "Workday",
                        "unique_id": None,
                        "apply": "update",
                        "data": {},
                        "options": {"country": "CZ"},
                    }
                ],
            }
            (live / ".storage" / "core.config_entries").write_text(json.dumps(raw))
            (source / ".storage_managed" / "core.config_entries.json").write_text(json.dumps(projection))
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            logs = []
            server.log = lambda message: logs.append(message)
            server.do_core_check = lambda: events.append("check")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, [])

    def test_managed_config_entries_projection_skips_missing_live_raw_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (source / ".storage_managed").mkdir(parents=True)
            (source / ".storage_managed" / "core.config_entries.json").write_text(
                json.dumps({"version": 1, "source": "core.config_entries", "entries": []})
            )
            details = []

            skipped = server._CTX.apply_homeassistant_config(source, live, {"id": "homeassistant"}, details)

            self.assertEqual(skipped, [])
            self.assertFalse((live / ".storage" / "core.config_entries").exists())
            self.assertIn(
                "Skipped managed core.config_entries projection because live .storage/core.config_entries is missing.",
                details,
            )

    def test_apply_preview_skips_missing_live_config_entries_raw_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (source / ".storage_managed").mkdir(parents=True)
            (source / ".storage_managed" / "core.config_entries.json").write_text(
                json.dumps({"version": 1, "source": "core.config_entries", "entries": []})
            )

            preview = server.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                    }
                ]
            )

            self.assertEqual(preview["deletions"], 0)
            self.assertIn("no file changes", preview["diff"].lower())

    def test_homeassistant_apply_rejects_git_source_symlink(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            outside = root / "outside.yaml"
            outside.write_text("outside\n")
            (source / "packages").mkdir(parents=True)
            (source / "packages" / "link.yaml").symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, "contains symlink"):
                server.apply_homeassistant_config(source, live, {"id": "homeassistant"})
            self.assertFalse((live / "packages" / "link.yaml").exists())

    def test_addon_apply_rejects_git_source_symlink(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "addons" / "local_zigbee2mqtt"
            live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            outside = root / "outside.txt"
            outside.write_text("outside\n")
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "link.txt").symlink_to(outside)
            live.mkdir()

            with self.assertRaisesRegex(RuntimeError, "contains symlink"):
                server.apply_targets(
                    [
                        {
                            "id": "addon-local_zigbee2mqtt",
                            "type": "addon",
                            "source_path": str(source),
                            "live_path": str(live),
                            "resolved_slug": "local_zigbee2mqtt",
                            "restart_after_sync": False,
                        }
                    ],
                    [],
                )
            self.assertFalse((live / "nested" / "link.txt").exists())

    def test_apply_preview_rejects_git_source_symlink(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            outside = root / "outside.yaml"
            outside.write_text("outside\n")
            (source / "packages").mkdir(parents=True)
            (source / "packages" / "link.yaml").symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, "contains symlink"):
                server.build_apply_preview(
                    [
                        {
                            "id": "homeassistant",
                            "type": "homeassistant",
                            "source_path": str(source),
                            "live_path": str(server.CONFIG_DIR),
                            "delete": False,
                        }
                    ]
                )

    def test_apply_failure_restores_release_snapshot_and_starts_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, file_text="git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "restart_after_apply": True,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server.core_restart = lambda: events.append("restart")

            self.assertTrue(server.run_preview_job())
            server.write_state({"last_preview_approved_fingerprint": server.read_state()["last_preview_fingerprint"]})

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "live\n")
            self.assertEqual(events, ["check"])

    def test_apply_failure_after_core_stop_rolls_back_and_starts_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / ".storage").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git\n")
            (seed / "homeassistant" / ".storage" / "input_boolean").write_text("git-storage\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / ".storage").mkdir(parents=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            (server.CONFIG_DIR / ".storage" / "input_boolean").write_text("live-storage\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "restart_after_apply": True,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server.core_restart = lambda: events.append("restart")

            self.assertTrue(server.run_preview_job())
            server.write_state({"last_preview_approved_fingerprint": server.read_state()["last_preview_fingerprint"]})

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "live\n")
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")
            self.assertEqual(events, ["stop", "check", "start"])

    def test_failed_apply_rolls_back_new_homeassistant_directory_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / "packages").mkdir(parents=True)
            (seed / "homeassistant" / "packages" / "new.yaml").write_text("git\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "restart_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server.core_restart = lambda: events.append("restart")

            self.assertTrue(server.run_preview_job())

            def fail_check():
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            self.assertFalse(server.run_apply_job())
            self.assertFalse((server.CONFIG_DIR / "packages" / "new.yaml").exists())
            self.assertEqual(events, [])

    def test_core_start_failure_rolls_back_without_second_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / ".storage").mkdir(parents=True)
            (seed / "homeassistant" / ".storage" / "input_boolean").write_text("git-storage\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            (server.CONFIG_DIR / ".storage").mkdir(parents=True)
            (server.CONFIG_DIR / ".storage" / "input_boolean").write_text("live-storage\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "restart_after_apply": True,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_restart = lambda: events.append("restart")

            start_calls = {"count": 0}

            def start_or_fail_once():
                events.append("start")
                start_calls["count"] += 1
                if start_calls["count"] == 1:
                    raise RuntimeError("start failed")

            server.core_start = start_or_fail_once

            self.assertTrue(server.run_preview_job())
            server.write_state({"last_preview_approved_fingerprint": server.read_state()["last_preview_fingerprint"]})
            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")
            self.assertEqual(events, ["stop", "check", "start", "start"])

    def test_clean_git_checkout_imports_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            self.git(["clone", str(ROOT.parent), str(checkout)], root)
            status = self.git(["status", "--porcelain"], checkout).stdout.strip()
            self.assertEqual(status, "")

            script = (
                "import importlib.util, pathlib; "
                "path = pathlib.Path('ha-ops/app/server.py').resolve(); "
                "spec = importlib.util.spec_from_file_location('server_clean_checkout', path); "
                "module = importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(module); "
                "assert module.HOST == '0.0.0.0'"
            )
            subprocess.run(["python3", "-c", script], cwd=checkout, check=True, text=True, capture_output=True)

    def test_worktree_imports_server_without_sys_modules_registration(self):
        script = (
            "import importlib.util, pathlib; "
            f"path = pathlib.Path({str(SERVER_PATH)!r}); "
            "spec = importlib.util.spec_from_file_location('server_worktree_import', path); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module); "
            "assert module.HOST == '0.0.0.0'"
        )
        subprocess.run(["python3", "-c", script], check=True, text=True, capture_output=True)

    def test_render_page_survives_unavailable_backup_api(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.backup_manager_info = lambda: (_ for _ in ()).throw(RuntimeError("no supervisor"))
            server.get_installed_addons = lambda: []

            page = server.render_page()

            self.assertIn("Backup status unavailable", page)

    def test_render_page_suppresses_recovered_backup_gate_error(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.latest_system_backup_status = lambda options: {
                "stale": False,
                "message": "Automatic backup at 2026-05-14T01:15:00+00:00 (19 hour(s) ago, 1 location(s)).",
            }
            server.write_state(
                {
                    "last_status": "error",
                    "last_action": "apply",
                    "last_message": "No fresh system backup found within 24 hour(s): No system Home Assistant backups found.",
                }
            )

            page = server.render_page()

            self.assertNotIn(">error<", page)
            self.assertNotIn("No fresh system backup found", page)
            self.assertIn("Fresh system backup is now available", page)

    def test_render_page_suppresses_stale_successful_config_check_error(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.write_state(
                {
                    "last_status": "error",
                    "last_action": "apply",
                    "last_message": "Home Assistant config check failed: {'result': 'ok', 'data': {}}",
                }
            )

            page = server.render_page()

            self.assertNotIn(">error<", page)
            self.assertNotIn("Home Assistant config check failed", page)
            self.assertIn("Previous stale config-check error was cleared", page)

    def test_managed_addons_are_selected_in_targets_table(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}]

            page = server.render_page()

            self.assertIn("data-auto-submit='change'", page)
            self.assertIn("name='addon'", page)
            self.assertIn("<h2>Managed Targets</h2>", page)
            self.assertIn("<th>Managed</th>", page)
            self.assertIn("Zigbee2MQTT (local_zigbee2mqtt)", page)
            self.assertNotIn("<h2>Managed Add-ons</h2>", page)
            self.assertNotIn("Protected Storage", page)
            self.assertNotIn("Save Add-on Selection", page)

    def test_primary_actions_are_grouped_by_direction(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            page = server.render_page()

            ha_to_git = page.index('action="save-preview"')
            save = page.index('action="save"')
            git_to_ha = page.index('action="preview"')
            apply = page.index('action="apply"')
            self.assertLess(ha_to_git, save)
            self.assertLess(save, git_to_ha)
            self.assertLess(git_to_ha, apply)
            self.assertIn('<div class="action-row">', page)
            self.assertIn('<button type="submit" >Save HA to Git</button>', page)
            self.assertIn('<button type="submit" >Apply Git to HA</button>', page)
            self.assertIn('action="deleted-devices-preview"', page)
            self.assertIn("Check deleted_devices", page)
            self.assertIn("<h2>Log</h2>", page)
            self.assertNotIn("<h2>Last Run Details</h2>", page)
            self.assertNotIn("Preview deletions", page)
            self.assertNotIn("Apply Preview", page)
            self.assertNotIn("Save Preview", page)
            self.assertNotIn("No apply preview yet.", page)
            self.assertNotIn("No save preview yet.", page)
            self.assertNotIn("Deletion of deleted_devices Preview", page)
            self.assertNotIn("Approve Deletion", page)
            self.assertNotIn("Confirm Changes", page)
            self.assertNotIn("Revert Changes", page)

    def test_deleted_devices_preview_lists_entities_as_table(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.area_registry").write_text(
                json.dumps({"data": {"areas": [{"id": "bathroom", "name": "Bathroom"}]}})
            )
            (storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [],
                            "deleted_entities": [
                                {
                                    "device_id": "deleted-1",
                                    "area_id": "bathroom",
                                    "entity_id": "sensor.bathroom_presence_illuminance",
                                    "original_name": "Illuminance",
                                    "original_device_class": "illuminance",
                                }
                            ],
                        }
                    }
                )
            )
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "minor_version": 12,
                        "key": "core.device_registry",
                        "data": {
                            "devices": [],
                            "deleted_devices": [
                                {
                                    "id": "deleted-1",
                                    "name": "Bathroom Presence",
                                    "manufacturer": "Moes",
                                    "model": "Scene remote",
                                    "identifiers": [["mqtt", "old"]],
                                }
                            ],
                        },
                    }
                )
            )

            self.assertTrue(server.run_deleted_devices_preview_job())
            state = server.read_state()

            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertEqual(
                state["last_deleted_devices_rows"],
                [
                    {
                        "area": "Bathroom",
                        "entity_id": "sensor.bathroom_presence_illuminance",
                        "original_name": "Illuminance",
                        "original_device_class": "illuminance",
                        "id": "deleted-1",
                    }
                ],
            )
            page = server.render_page()
            self.assertIn("<th>Area</th>", page)
            self.assertIn("<th>ID</th>", page)
            self.assertNotIn("<th>Entity ID</th>", page)
            self.assertNotIn("sensor.bathroom_presence_illuminance", page)
            self.assertIn("Illuminance", page)
            self.assertIn("illuminance", page)
            self.assertIn("deleted-1", page)
            self.assertIn("Approve Deletion", page)
            self.assertNotIn("identifiers=mqtt:old", page)

    def test_approve_deleted_devices_clears_array_with_core_stopped(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "minor_version": 12,
                        "key": "core.device_registry",
                        "data": {
                            "devices": [{"id": "live"}],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        },
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            logs = []
            server.log = lambda message: logs.append(message)

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())

            data = json.loads(registry_path.read_text())
            self.assertEqual(data["data"]["deleted_devices"], [])
            self.assertEqual(data["data"]["devices"], [{"id": "live"}])
            self.assertEqual(events, ["stop", "start"])
            state = server.read_state()
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertTrue(Path(state["deleted_devices_rollback_path"]).exists())

    def test_confirm_deleted_devices_discards_rollback(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback_path = Path(server.read_state()["deleted_devices_rollback_path"])

            self.assertTrue(server.run_deleted_devices_confirm_job())
            state = server.read_state()

            self.assertFalse(rollback_path.exists())
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertIsNone(state["deleted_devices_rollback_path"])

    def test_revert_deleted_devices_restores_rollback(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {
                "data": {
                    "devices": [],
                    "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                }
            }
            registry_path.write_text(json.dumps(original))
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            self.assertEqual(json.loads(registry_path.read_text())["data"]["deleted_devices"], [])
            rollback_path = Path(server.read_state()["deleted_devices_rollback_path"])

            self.assertTrue(server.run_deleted_devices_revert_job())
            state = server.read_state()

            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(events, ["stop", "start", "stop", "start"])
            self.assertFalse(rollback_path.exists())
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertEqual(state["last_deleted_devices_count"], 1)

    def test_failed_deleted_devices_start_reverts_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {
                "data": {
                    "devices": [],
                    "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                }
            }
            registry_path.write_text(json.dumps(original))
            events = []
            server.core_stop = lambda: events.append("stop")
            start_calls = {"count": 0}

            def start_fails_then_succeeds():
                events.append("start")
                start_calls["count"] += 1
                if start_calls["count"] == 1:
                    raise RuntimeError("start failed")

            server.core_start = start_fails_then_succeeds

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertFalse(server.run_deleted_devices_delete_job())
            state = server.read_state()

            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(events, ["stop", "start", "start"])
            self.assertFalse(state.get("deleted_devices_pending_confirmation", False))
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertIn("Old Button", state["last_deleted_devices_preview"])
            self.assertIn("start failed", state["last_message"])

    def test_refresh_clears_deleted_devices_preview_without_pending_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_deleted_devices_preview": "deleted_devices entries to remove (1):\n- Old Button",
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "fingerprint",
                    "last_deleted_devices_generated_at": "2026-05-16T12:00:00+00:00",
                }
            )

            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_deleted_devices_preview"], "")
            self.assertEqual(state["last_deleted_devices_rows"], [])
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIsNone(state["last_deleted_devices_fingerprint"])
            self.assertIsNone(state["last_deleted_devices_generated_at"])
            self.assertNotIn("Deletion of deleted_devices Preview", page)
            self.assertNotIn("Approve Deletion", page)

    def test_refresh_preserves_deleted_devices_preview_during_pending_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_deleted_devices_preview": "No deleted_devices entries found.",
                    "last_deleted_devices_rows": [],
                    "last_deleted_devices_count": 0,
                    "last_deleted_devices_fingerprint": "after",
                    "last_deleted_devices_generated_at": "2026-05-16T12:00:00+00:00",
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": "/tmp/rollback",
                    "deleted_devices_rollback_fingerprint": "before",
                    "deleted_devices_applied_fingerprint": "after",
                }
            )

            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertEqual(state["last_deleted_devices_fingerprint"], "after")
            self.assertEqual(state["deleted_devices_rollback_path"], "/tmp/rollback")
            self.assertIn("Pending deleted_devices Diff", page)
            self.assertIn("Pending diff unavailable", page)
            self.assertIn("Confirm Changes", page)
            self.assertIn("Revert Changes", page)

    def test_pending_deleted_devices_cleanup_renders_decision_log_not_error(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-2", "name": "New Deleted Button"}],
                        }
                    }
                )
            )
            rollback_path = root / "work" / "deleted-devices-rollback" / "core.device_registry"
            rollback_path.parent.mkdir(parents=True)
            rollback_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.write_state(
                {
                    "last_status": "error",
                    "last_action": "deleted_devices_revert",
                    "last_message": "Device registry changed after deletion. Review manually before reverting.",
                    "last_details": [],
                    "last_deleted_devices_preview": "No deleted_devices entries found.",
                    "last_deleted_devices_rows": [],
                    "last_deleted_devices_count": 0,
                    "last_deleted_devices_fingerprint": "after",
                    "last_deleted_devices_generated_at": "2026-05-16T12:00:00+00:00",
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback_path),
                    "deleted_devices_rollback_fingerprint": "before",
                    "deleted_devices_applied_fingerprint": "after",
                }
            )

            page = server.render_page()

            self.assertIn('<div class="badge pending">pending decision</div>', page)
            self.assertNotIn('<div class="badge error">error</div>', page)
            self.assertIn("<h2>Log</h2>", page)
            self.assertNotIn("<h2>Last Run Details</h2>", page)
            self.assertNotIn("Preview deletions", page)
            self.assertIn("deleted_devices cleanup is waiting for your decision.", page)
            self.assertIn("Previous action: Revert Changes", page)
            self.assertIn("Last result: Device registry changed after deletion. Review manually before reverting.", page)
            self.assertIn("- removed by this cleanup: 1", page)
            self.assertIn("- currently in deleted_devices: 1", page)
            self.assertIn("- new deleted_devices after restart: 1", page)
            self.assertIn("- removed entries returned: 0", page)
            self.assertIn("Confirm Changes: keep this cleanup.", page)
            self.assertIn("Revert Changes: restore only entries removed by this cleanup.", page)
            self.assertIn("<h2>Pending deleted_devices Diff</h2>", page)
            self.assertNotIn("<h2>Deletion of deleted_devices Preview</h2>", page)
            self.assertIn("Confirm Changes accepts this diff.", page)
            self.assertIn("deleted_devices before cleanup", page)
            self.assertIn("deleted_devices now", page)
            self.assertIn("diff-del", page)
            self.assertIn("d Button", page)
            self.assertIn("diff-add", page)
            self.assertIn("New Delete", page)

    def test_pending_deleted_devices_cleanup_blocks_check_and_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            page = server.render_page()

            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Check deleted_devices</button>", page)
            self.assertNotIn("action='deleted-devices-delete'", page)
            self.assertIn("Confirm Changes", page)
            self.assertIn("Revert Changes", page)
            self.assertFalse(server.run_deleted_devices_preview_job())
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])
            self.assertFalse(server.run_deleted_devices_delete_job())
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])

    def test_pending_deleted_devices_cleanup_blocks_save_apply_and_previews(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"deleted_devices_pending_confirmation": True})

            self.assertFalse(server.run_save_preview_job())
            self.assertEqual(server.read_state()["last_action"], "save_preview")
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_save_job())
            self.assertEqual(server.read_state()["last_action"], "save")
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_preview_job())
            self.assertEqual(server.read_state()["last_action"], "preview")
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_apply_job())
            self.assertEqual(server.read_state()["last_action"], "apply")
            self.assertIn("pending deleted_devices cleanup", server.read_state()["last_message"])

            page = server.render_page()
            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Preview HA to Git</button>", page)
            self.assertIn("<button type=\"submit\" disabled>Save HA to Git</button>", page)
            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Preview Git to HA</button>", page)
            self.assertIn("<button type=\"submit\" disabled>Apply Git to HA</button>", page)
            self.assertIn("Confirm Changes", page)
            self.assertIn("Revert Changes", page)

    def test_failed_deleted_devices_preview_clears_old_approval(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_deleted_devices_preview": "old",
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "old",
                    "last_deleted_devices_generated_at": "2026-05-16T12:00:00+00:00",
                }
            )

            self.assertFalse(server.run_deleted_devices_preview_job())
            state = server.read_state()

            self.assertEqual(state["last_deleted_devices_preview"], "")
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIsNone(state["last_deleted_devices_fingerprint"])
            self.assertIsNone(state["last_deleted_devices_generated_at"])

    def test_stale_deleted_devices_fingerprint_fails_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")

            self.assertTrue(server.run_deleted_devices_preview_job())
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-2", "name": "New Old Button"}],
                        }
                    }
                )
            )

            self.assertFalse(server.run_deleted_devices_delete_job())

            self.assertEqual(events, [])
            self.assertIn("changed since preview", server.read_state()["last_message"])

    def test_deleted_devices_revalidates_after_backup_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")

            self.assertTrue(server.run_deleted_devices_preview_job())

            def mutate_during_backup(options, details):
                registry_path.write_text(
                    json.dumps(
                        {
                            "data": {
                                "devices": [],
                                "deleted_devices": [{"id": "deleted-2", "name": "New Old Button"}],
                            }
                        }
                    )
                )
                return "backup-slug"

            server.ensure_fresh_system_backup = mutate_during_backup

            self.assertFalse(server.run_deleted_devices_delete_job())
            state = server.read_state()

            self.assertEqual(events, [])
            self.assertEqual(state["last_backup_slug"], "backup-slug")
            self.assertIn("changed since preview", state["last_message"])

    def test_deleted_devices_partial_success_clears_approval_when_core_start_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")

            def fail_start():
                events.append("start")
                raise RuntimeError("start failed")

            server.core_start = fail_start

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertFalse(server.run_deleted_devices_delete_job())
            state = server.read_state()
            data = json.loads(registry_path.read_text())

            self.assertEqual(data["data"]["deleted_devices"], [{"id": "deleted-1", "name": "Old Button"}])
            self.assertEqual(events, ["stop", "start", "start", "start"])
            self.assertFalse(state.get("deleted_devices_pending_confirmation", False))
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertIn("Old Button", state["last_deleted_devices_preview"])
            self.assertIn("start failed", state["last_message"])

    def test_deleted_devices_failed_restore_preserves_manual_recovery_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")

            def fail_start():
                events.append("start")
                raise RuntimeError("start failed")

            def fail_restore(_rollback_path):
                raise RuntimeError("restore failed")

            server.core_start = fail_start
            server._CTX.restore_deleted_devices_rollback = fail_restore

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertFalse(server.run_deleted_devices_delete_job())
            state = server.read_state()
            data = json.loads(registry_path.read_text())

            self.assertEqual(data["data"]["deleted_devices"], [])
            self.assertEqual(events, ["stop", "start", "start"])
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertTrue(Path(state["deleted_devices_rollback_path"]).exists())
            self.assertIsNotNone(state["deleted_devices_rollback_fingerprint"])
            self.assertIsNotNone(state["deleted_devices_applied_fingerprint"])
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIn("Manual recovery is required", state["last_message"])
            self.assertIn("restore failed", "\n".join(state["last_details"]))

    def test_confirm_deleted_devices_allows_unrelated_registry_changes_after_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            data = json.loads(registry_path.read_text())
            data["data"]["devices"].append({"id": "new-live"})
            registry_path.write_text(json.dumps(data))

            self.assertTrue(server.run_deleted_devices_confirm_job())
            state = server.read_state()

            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertIn("Confirmed deleted_devices cleanup", state["last_message"])
            self.assertIn("removed deleted_devices did not return", "\n".join(state["last_details"]))

    def test_confirm_deleted_devices_allows_new_deleted_devices_after_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            data = json.loads(registry_path.read_text())
            data["data"]["deleted_devices"] = [{"id": "deleted-2", "name": "Returned Button"}]
            registry_path.write_text(json.dumps(data))

            self.assertTrue(server.run_deleted_devices_confirm_job())
            state = server.read_state()
            data = json.loads(registry_path.read_text())

            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertEqual(data["data"]["deleted_devices"], [{"id": "deleted-2", "name": "Returned Button"}])
            self.assertIn("Confirmed deleted_devices cleanup", state["last_message"])
            self.assertIn("new deleted_devices", "\n".join(state["last_details"]))

    def test_confirm_deleted_devices_fails_when_removed_entry_returns_after_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            data = json.loads(registry_path.read_text())
            data["data"]["deleted_devices"] = [{"id": "deleted-1", "name": "Old Button"}]
            registry_path.write_text(json.dumps(data))

            self.assertFalse(server.run_deleted_devices_confirm_job())
            state = server.read_state()

            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertIn("removed by this cleanup returned", state["last_message"])

    def test_revert_deleted_devices_restores_only_deleted_devices(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [],
                            "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            logs = []
            server.log = lambda message: logs.append(message)

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            data = json.loads(registry_path.read_text())
            data["data"]["devices"].append({"id": "new-live"})
            data["data"]["deleted_devices"] = [{"id": "deleted-2", "name": "New Deleted Button"}]
            registry_path.write_text(json.dumps(data))

            self.assertTrue(server.run_deleted_devices_revert_job())
            state = server.read_state()
            data = json.loads(registry_path.read_text())

            self.assertEqual(events, ["stop", "start", "stop", "start"])
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertEqual(data["data"]["devices"], [{"id": "new-live"}])
            self.assertEqual(
                data["data"]["deleted_devices"],
                [
                    {"id": "deleted-2", "name": "New Deleted Button"},
                    {"id": "deleted-1", "name": "Old Button"},
                ],
            )
            self.assertIn("Reverted deleted_devices cleanup", state["last_message"])
            self.assertIn("Preserved 1 current deleted_devices", "\n".join(state["last_details"]))
            self.assertIn("Preserved other current core.device_registry changes", "\n".join(state["last_details"]))
            self.assertIn("deleted_devices revert: restored deleted_devices", "\n".join(logs))

    def test_revert_deleted_devices_start_failure_disables_confirmation_after_restore(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {
                "data": {
                    "devices": [],
                    "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}],
                }
            }
            registry_path.write_text(json.dumps(original))
            events = []
            server.core_stop = lambda: events.append("stop")
            start_calls = {"count": 0}

            def start_fails_on_revert():
                events.append("start")
                start_calls["count"] += 1
                if start_calls["count"] >= 2:
                    raise RuntimeError("start failed")

            server.core_start = start_fails_on_revert

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            self.assertFalse(server.run_deleted_devices_revert_job())
            state = server.read_state()

            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(events, ["stop", "start", "stop", "start", "start"])
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertIsNone(state["deleted_devices_applied_fingerprint"])
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertIn("Old Button", state["last_deleted_devices_preview"])
            self.assertIn("start failed", state["last_message"])

    def test_homeassistant_organizer_toggle_is_in_main_action_card(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            page = server.render_page()

            toggle = page.index("homeassistant-organizer")
            actions = page.index('<div class="actions">')
            managed_targets = page.index("<h2>Managed Targets</h2>")
            self.assertLess(toggle, actions)
            self.assertLess(toggle, managed_targets)
            self.assertIn("Split automations, scripts, and scenes by area in Git", page)

    def test_save_preview_shows_candidates_without_commit_or_push(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            (server.CONFIG_DIR / "secrets.yaml").write_text("secret\n")
            (server.CONFIG_DIR / "home-assistant_v2.db").write_text("runtime\n")
            (server.CONFIG_DIR / "packages").mkdir()
            (server.CONFIG_DIR / "packages" / "lights.yaml").write_text("light:\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_preview_job())
            page = server.render_page()
            state = server.read_state()
            repo = server.DATA_DIR / "ha-config"

            self.assertIn("Save Preview", page)
            self.assertIn("Save preview changes (2):", page)
            self.assertIn("- Modified: homeassistant/configuration.yaml", page)
            self.assertIn("- Added: homeassistant/packages/lights.yaml", page)
            self.assertIn("- homeassistant/configuration.yaml", page)
            self.assertIn("- homeassistant/packages/lights.yaml", page)
            self.assertIn("diff-del", page)
            self.assertIn("diff-add", page)
            self.assertIn("diff-changed", page)
            self.assertNotIn("secrets.yaml", page)
            self.assertNotIn("home-assistant_v2.db", page)
            self.assertIn("last_save_diff", state)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "base\n")
            self.assertEqual(self.repo_status(repo), "")

    def test_manifest_source_symlink_escape_is_rejected(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (repo / "escape").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(RuntimeError):
                server.repo_source_path(repo, "escape", "homeassistant")

    def test_addon_manifest_live_path_outside_allowed_roots_is_rejected(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            target = {
                "id": "addon-local_zigbee2mqtt",
                "type": "addon",
                "source": "addons/local_zigbee2mqtt",
                "addon_slug": "local_zigbee2mqtt",
                "live_path": str(root / "wrong"),
            }

            with self.assertRaises(RuntimeError):
                server.resolve_targets(
                    root / "repo",
                    {"targets": [target]},
                    [{"slug": "local_zigbee2mqtt", "name": "Plain add-on"}],
                    require_source=False,
                )

    def test_addon_manifest_live_path_for_other_addon_is_rejected(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            target = {
                "id": "addon-local_zigbee2mqtt",
                "type": "addon",
                "source": "addons/local_zigbee2mqtt",
                "addon_slug": "local_zigbee2mqtt",
                "live_path": str(server.ADDON_CONFIGS_DIR / "other_addon"),
            }

            with self.assertRaises(RuntimeError):
                server.resolve_targets(
                    root / "repo",
                    {"targets": [target]},
                    [{"slug": "local_zigbee2mqtt", "name": "Plain add-on"}],
                    require_source=False,
                )

    def test_release_snapshot_excludes_runtime_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            (server.CONFIG_DIR / "home-assistant_v2.db").write_text("db\n")
            (server.CONFIG_DIR / "home-assistant.log").write_text("log\n")
            addon_live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            addon_live.mkdir()
            (addon_live / "configuration.yaml").write_text("addon\n")
            (addon_live / "nested").mkdir()
            (addon_live / "nested" / "runtime.db").write_text("db\n")

            release = server.create_release_snapshot(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(root / "repo" / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                    },
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(root / "repo" / "addons" / "local_zigbee2mqtt"),
                        "live_path": str(addon_live),
                    },
                ],
                "abc123",
                None,
            )

            release_dir = server.RELEASES_DIR / release
            self.assertTrue((release_dir / "homeassistant" / "configuration.yaml").exists())
            self.assertFalse((release_dir / "homeassistant" / "home-assistant_v2.db").exists())
            self.assertFalse((release_dir / "homeassistant" / "home-assistant.log").exists())
            self.assertTrue((release_dir / "addon-local_zigbee2mqtt" / "configuration.yaml").exists())
            self.assertFalse((release_dir / "addon-local_zigbee2mqtt" / "nested" / "runtime.db").exists())

    def test_addon_rollback_preserves_excluded_runtime_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            addon_live = server.ADDON_CONFIGS_DIR / "local_zigbee2mqtt"
            addon_live.mkdir()
            (addon_live / "configuration.yaml").write_text("snapshot\n")
            (addon_live / "nested").mkdir()
            (addon_live / "nested" / "runtime.db").write_text("runtime\n")

            release = server.create_release_snapshot(
                [
                    {
                        "id": "addon-local_zigbee2mqtt",
                        "type": "addon",
                        "resolved_slug": "local_zigbee2mqtt",
                        "source_path": str(root / "repo" / "addons" / "local_zigbee2mqtt"),
                        "live_path": str(addon_live),
                        "restart_after_sync": False,
                    }
                ],
                "abc123",
                None,
            )

            (addon_live / "configuration.yaml").write_text("changed\n")
            (addon_live / "extra.yaml").write_text("live-only\n")
            server.restore_release_snapshot(release, [])

            self.assertEqual((addon_live / "configuration.yaml").read_text(), "snapshot\n")
            self.assertFalse((addon_live / "extra.yaml").exists())
            self.assertEqual((addon_live / "nested" / "runtime.db").read_text(), "runtime\n")

    def test_pending_conflicts_block_preview_and_save(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"conflicts": ["homeassistant/configuration.yaml"]})

            self.assertFalse(server.run_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "conflicts")
            self.assertIn("Resolve Git conflicts", state["last_message"])

            self.assertFalse(server.run_save_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "conflicts")
            self.assertIn("Resolve Git conflicts", state["last_message"])


if __name__ == "__main__":
    unittest.main()
