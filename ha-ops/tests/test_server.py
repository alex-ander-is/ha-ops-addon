import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "app" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("server", SERVER_PATH)
    server = importlib.util.module_from_spec(spec)
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

    def test_missing_git_source_does_not_delete_live_config(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            (live / "configuration.yaml").write_text("homeassistant:\n")
            server.apply_homeassistant_config(root / "missing", live, {"id": "homeassistant"})
            self.assertEqual((live / "configuration.yaml").read_text(), "homeassistant:\n")

    def test_protected_storage_skipped_unless_enabled(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.config_entries").write_text("live\n")
            (source / ".storage" / "core.config_entries").write_text("git\n")
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
            self.assertEqual((preview_storage / "core.config_entries").read_text(), "live\n")
            self.assertEqual((preview_storage / "input_boolean").read_text(), "input\n")
            self.assertIn("core.config_entries", preview["skipped_protected"])

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
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("homeassistant/configuration.yaml", result.stdout)

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

            self.assertTrue(server.run_preview_job())
            self.assertTrue(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "homeassistant:\n")

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

    def test_pending_conflicts_block_apply(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"conflicts": ["homeassistant/configuration.yaml"]})

            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "error")
            self.assertIn("Resolve Git conflicts", state["last_message"])


if __name__ == "__main__":
    unittest.main()
