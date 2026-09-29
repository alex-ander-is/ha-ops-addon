"""Focused regressions for the static client command and preview boundary."""

import hashlib
import json
import re
import subprocess
import threading
from types import SimpleNamespace
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import state  # noqa: E402
import web  # noqa: E402
import app_context  # noqa: E402
import sync  # noqa: E402


class ClientContractTests(unittest.TestCase):
    def test_runtime_has_no_server_ui_renderer_and_client_owns_conflicts(self):
        self.assertFalse((APP / "ui.py").exists())
        for name in ("server.py", "web.py"):
            source = (APP / name).read_text(encoding="utf-8")
            self.assertNotRegex(source, re.compile(r"^import ui$", re.MULTILINE))
        source = (APP.parent / "frontend" / "src" / "ha-ops.js").read_text(encoding="utf-8")
        self.assertIn('<vaadin-details @opened-changed=', source)
        self.assertIn('this.actionButton("resolve_conflict"', source)
        self.assertTrue('"approve_save_conflicts", t("action.use_ha_for_all_conflicts")' in source)
        self.assertTrue('t("text.split_organizer_blocked")' in source)
        self.assertIn('class="unicode-escape"', source)

    def test_static_shell_has_ingress_relative_assets_and_no_request_data(self):
        shell = web.render_page(object())
        self.assertIn('href="assets/ha-ops.css"', shell)
        self.assertIn('src="assets/ha-ops.js"', shell)
        self.assertIn("<ha-ops-app", shell)
        self.assertNotIn("<form", shell)
        self.assertNotIn("automations.yaml", shell)
        source = (APP.parent / "frontend" / "src" / "ha-ops.js").read_text(encoding="utf-8")
        self.assertIn('fetch("api/v1/state", { cache: "no-store" })', source)
        self.assertIn("this.scheduleHttpPoll()", source)
        self.assertIn("<vaadin-text-field", source)
        self.assertIn('if (WS_COMMANDS.has(command) && socket && socket.readyState !== window.WebSocket?.CLOSED)', source)
        self.assertNotRegex(source, re.compile(r"<(?:input|button|select|form|textarea)\b"))

    def test_claim_is_durable_and_deduplicated_before_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state.OperationStore(Path(directory) / "state.json")
            command_id = str(uuid.uuid4())
            claimed, record = store.claim_command(command_id, "save", 0, {"value": 1})
            self.assertTrue(claimed)
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(store.read_state()["active_operation"]["phase"], "accepted")
            self.assertFalse(store.claim_command(command_id, "save", 0, {"value": 1})[0])
            with self.assertRaisesRegex(ValueError, "different command"):
                store.claim_command(command_id, "save", 0, {"value": 2})
            with self.assertRaisesRegex(RuntimeError, "active"):
                store.claim_command(str(uuid.uuid4()), "save", 0, {})

    def test_concurrent_command_claims_admit_exactly_one_effectful_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state.OperationStore(Path(directory) / "state.json")
            gate = threading.Barrier(3)
            results = []
            results_lock = threading.Lock()

            def submit(command_id):
                gate.wait()
                try:
                    store.claim_command(command_id, "preview", 0, {})
                    outcome = "accepted"
                except RuntimeError:
                    outcome = "blocked"
                with results_lock:
                    results.append(outcome)

            workers = [threading.Thread(target=submit, args=(str(uuid.uuid4()),)) for _ in range(2)]
            for worker in workers:
                worker.start()
            gate.wait()
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(sorted(results), ["accepted", "blocked"])
            current = store.read_state()
            self.assertEqual(len(current["command_records"]), 1)
            self.assertEqual(current["active_operation"]["phase"], "accepted")

    def test_pending_save_push_retry_blocks_every_unrelated_mutation_at_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state.OperationStore(Path(directory) / "state.json")
            store.write_state({"save_push_retry_pending": True, "save_push_retry_commit": "pending"})
            before = store.read_state()
            commands = set(state.EFFECTFUL_COMMANDS) | {
                "select_apply_preview", "resolve_save_preview", "include_redundant_data",
                "addons", "select_internal_ids", "select_retained_device",
            }
            for command in sorted(commands - {"save"}):
                with self.subTest(command=command), self.assertRaisesRegex(RuntimeError, "Save push retry"):
                    store.claim_command(str(uuid.uuid4()), command, before["operation_generation"], {})
            after = store.read_state()
            self.assertEqual(after["command_records"], before["command_records"])
            self.assertIsNone(after["active_operation"])
            command_id = str(uuid.uuid4())
            claimed, record = store.claim_command(command_id, "clear_preview", before["operation_generation"],
                                                   {"direction": "save"})
            self.assertTrue(claimed)
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(store.read_state()["active_operation"]["command_id"], command_id)

    def test_startup_repair_blocks_claim_before_any_command_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = state.OperationStore(Path(directory) / "state.json")
            prior_id = str(uuid.uuid4())
            store.claim_command(prior_id, "select_apply_preview", 0, {})
            store.update_command(prior_id, "terminal", {"ok": True})
            store.begin_repair()
            with self.assertRaisesRegex(RuntimeError, "startup repair not complete"):
                store.claim_command(str(uuid.uuid4()), "preview", 0, {})
            self.assertEqual(len(store.read_state()["command_records"]), 1)
            self.assertFalse(store.claim_command(prior_id, "select_apply_preview", 0, {})[0])

    def test_orphaned_dispatch_remains_fenced_and_accepted_predispatch_clears(self):
        for dispatching in (False, True):
            with self.subTest(dispatching=dispatching), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                store = state.OperationStore(path)
                command_id = str(uuid.uuid4())
                store.claim_command(command_id, "apply", 0, {})
                if dispatching:
                    store.update_command(command_id, "running")
                new_store = state.OperationStore(path)
                fence = new_store.reconcile_startup_fence()
                if dispatching:
                    self.assertEqual(fence["phase"], "recovery_required")
                    self.assertEqual(new_store.read_state()["active_operation"]["phase"], "recovery_required")
                else:
                    self.assertIsNone(fence)
                    self.assertEqual(new_store.read_state()["command_records"][command_id]["status"], "terminal")

    def test_every_effectful_command_class_stays_fenced_after_dispatch_crash(self):
        for command in sorted(state.EFFECTFUL_COMMANDS):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                store = state.OperationStore(path)
                command_id = str(uuid.uuid4())
                store.claim_command(command_id, command, 0, {})
                store.update_command(command_id, "running")
                restarted = state.OperationStore(path)
                restarted.reconcile_startup_fence()
                restarted.mark_repaired()
                current = restarted.read_state()
                self.assertEqual(current["active_operation"]["phase"], "recovery_required")
                self.assertIsNone(current["last_diff_cursor"])
                with self.assertRaisesRegex(RuntimeError, "active|uncertain"):
                    restarted.claim_command(str(uuid.uuid4()), "disk_usage", current["operation_generation"], {})

    def test_caught_apply_failure_clears_only_with_verified_rollback_result(self):
        for safe_terminal in (False, True):
            with self.subTest(safe_terminal=safe_terminal), tempfile.TemporaryDirectory() as directory:
                store = state.OperationStore(Path(directory) / "state.json")
                command_id = str(uuid.uuid4())
                store.claim_command(command_id, "apply", 0, {})
                store.update_command(command_id, "running")
                store.update_command(command_id, "terminal", {"ok": False, "safe_terminal": safe_terminal})
                operation = store.read_state()["active_operation"]
                self.assertEqual(operation is None, safe_terminal)
                if operation:
                    self.assertEqual(operation["phase"], "recovery_required")

    def test_reviewed_save_retry_crash_remains_fenced_before_and_after_dispatch(self):
        for dispatching in (False, True):
            with self.subTest(dispatching=dispatching), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                store = state.OperationStore(path)
                operation_id, retry_id = str(uuid.uuid4()), str(uuid.uuid4())
                store.claim_command(operation_id, "save", 0, {})
                store.update_command(operation_id, "running")
                store.update_command(operation_id, "terminal", {"ok": False})
                evidence = {"kind": "exact_retry_available", "marked_commits": ["commit"],
                            "head": "commit", "parent_verified": True, "remote_ancestor": True}
                store.write_state({"active_operation": {"command": "save", "command_id": operation_id,
                                                       "phase": "recovery_required", "evidence": evidence},
                                   "save_intent": {"operation_id": operation_id}})
                token = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                store.begin_reviewed_save_retry(retry_id, operation_id, store.read_state()["operation_generation"], token)
                if dispatching:
                    store.transition_reviewed_save_retry(retry_id, "running")
                restarted = state.OperationStore(path)
                restarted.reconcile_startup_fence()
                restarted.mark_repaired()
                current = restarted.read_state()
                self.assertEqual(current["active_operation"]["phase"], "recovery_required")
                self.assertEqual(current["command_records"][retry_id]["status"], "failed_unknown")
                with self.assertRaisesRegex(RuntimeError, "active|uncertain"):
                    restarted.claim_command(str(uuid.uuid4()), "disk_usage", current["operation_generation"], {})

    def test_fresh_preview_identity_and_revision_reject_stale_tab(self):
        current = state.default_state()
        current.update({
            "save_preview_id": str(uuid.uuid4()),
            "last_save_preview_paths": ["homeassistant/automations.yaml"],
            "last_save_preview_fingerprint": "same-content",
        })
        prior = web.preview_identity_for_state(current, "save")
        current["save_decision_revision"] = 1
        with self.assertRaises(web.StalePreviewDecision):
            web.assert_preview_decision_identity(current, "save", {"preview_identity": prior})
        current["save_decision_revision"] = 0
        current["save_preview_id"] = str(uuid.uuid4())
        with self.assertRaises(web.StalePreviewDecision):
            web.assert_preview_decision_identity(current, "save", {"preview_identity": prior})

    def test_confirm_digest_covers_selection_and_choices(self):
        current = state.default_state()
        current["last_preview_paths"] = ["b.yaml", "a.yaml"]
        current["apply_preview_selected_paths"] = ["a.yaml"]
        expected = hashlib.sha256(json.dumps([
            {"choice": "git", "path": "a.yaml", "selected": True},
            {"choice": "ha", "path": "b.yaml", "selected": False},
        ], separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(web.preview_decision_digest(current, "apply"), expected)
        current["apply_preview_resolutions"] = {"a.yaml": "ha"}
        self.assertNotEqual(web.preview_decision_digest(current, "apply"), expected)

    def test_internal_ids_diff_is_hidden_while_active(self):
        preview_id = str(uuid.uuid4())
        diff = "--- a\n+++ b\n"
        current = state.default_state()
        current.update({
            "last_internal_ids_preview_id": preview_id,
            "last_internal_ids_rows": [{"path": "automations.yaml", "changes": 1, "diff": diff,
                                        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()}],
        })

        class Context:
            def read_state(self):
                return current

        result = web.dispatch_command(Context(), "internal_ids_diff_get", {"preview_id": preview_id, "path": "automations.yaml"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["diff"], diff)
        current["active_operation"] = {"command": "save", "phase": "dispatching"}
        self.assertFalse(web.dispatch_command(Context(), "internal_ids_diff_get", {"preview_id": preview_id, "path": "automations.yaml"})["ok"])

    def test_internal_ids_migrate_rejects_stale_id_path_and_digest_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            preview_id = str(uuid.uuid4())
            digest = hashlib.sha256(b"exact diff").hexdigest()
            ctx.write_state({"last_internal_ids_preview_id": preview_id,
                             "last_internal_ids_rows": [{"path": "automations.yaml", "changes": 1,
                                                         "diff": "exact diff", "diff_sha256": digest}]})
            dispatched = []

            def fake_start(*args, **kwargs):
                dispatched.append((args, kwargs))
                return True

            def submit(selection):
                return web.dispatch_command(ctx, "internal_ids_migrate", {
                    "command_id": str(uuid.uuid4()), "generation": ctx.read_state()["operation_generation"],
                    "payload": selection,
                }, fake_start)

            for selection in (
                {"preview_id": str(uuid.uuid4()), "selected": [{"path": "automations.yaml", "diff_sha256": digest}]},
                {"preview_id": preview_id, "selected": [{"path": "other.yaml", "diff_sha256": digest}]},
                {"preview_id": preview_id, "selected": [{"path": "automations.yaml", "diff_sha256": "wrong"}]},
                {"preview_id": preview_id, "selected": [0]},
            ):
                self.assertFalse(submit(selection)["ok"])
                self.assertEqual(dispatched, [])

            valid = {"preview_id": preview_id, "selected": [{"path": "automations.yaml", "diff_sha256": digest}]}
            ctx.write_state({"last_internal_ids_rows": [{"path": "automations.yaml", "changes": 1,
                                                         "diff": "exact diff", "diff_sha256": digest, "selected": True}]})
            command_id = str(uuid.uuid4())
            envelope = {"command_id": command_id, "generation": ctx.read_state()["operation_generation"],
                        "payload": valid}
            self.assertTrue(web.dispatch_command(ctx, "internal_ids_migrate", envelope, fake_start)["ok"])
            self.assertEqual(len(dispatched), 1)
            ctx.update_command(command_id, "terminal", {"ok": True, "message": "Accepted"})
            ctx.write_state({"last_internal_ids_preview_id": str(uuid.uuid4())})
            duplicate = web.dispatch_command(ctx, "internal_ids_migrate", envelope, fake_start)
            self.assertTrue(duplicate["duplicate"])
            self.assertTrue(duplicate["ok"])
            self.assertEqual(len(dispatched), 1)

    def test_internal_ids_selection_commits_with_command_result_and_rejects_stale_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            preview_id = str(uuid.uuid4())
            digest = hashlib.sha256(b"exact diff").hexdigest()
            ctx.write_state({"last_internal_ids_preview_id": preview_id,
                             "last_internal_ids_rows": [{"path": "automations.yaml", "changes": 1,
                                                         "diff": "exact diff", "diff_sha256": digest, "selected": False}]})

            def submit(observed_preview):
                command_id = str(uuid.uuid4())
                result = web.dispatch_command(ctx, "select_internal_ids", {
                    "command_id": command_id, "generation": ctx.read_state()["operation_generation"],
                    "payload": {"preview_id": observed_preview, "path": "automations.yaml",
                                "diff_sha256": digest, "selected": True},
                })
                return command_id, result

            stale_id, stale = submit(str(uuid.uuid4()))
            self.assertFalse(stale["ok"])
            self.assertNotIn(stale_id, ctx.read_state()["command_records"])
            command_id, selected = submit(preview_id)
            self.assertTrue(selected["ok"])
            current = ctx.read_state()
            self.assertTrue(current["last_internal_ids_rows"][0]["selected"])
            self.assertEqual(current["command_records"][command_id]["status"], "terminal")
            self.assertTrue(web._snapshot_payload(ctx)["state"]["last_internal_ids_rows"][0]["selected"])

    def test_retained_selection_is_server_authoritative_and_delete_matches_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            ctx.write_state({"last_retained_devices_fingerprint": "fingerprint",
                             "last_retained_devices_generated_at": "2026-09-29T00:00:00Z",
                             "last_retained_devices_rows": [{"identity": "device-one", "retained_topics": ["topic"],
                                                            "selected": True}]})

            def submit(command, payload, starter=None):
                return web.dispatch_command(ctx, command, {
                    "command_id": str(uuid.uuid4()), "generation": ctx.read_state()["operation_generation"],
                    "payload": payload,
                }, starter)

            identity = {"retained_preview_fingerprint": "fingerprint",
                        "retained_preview_generated_at": "2026-09-29T00:00:00Z"}
            stale = submit("select_retained_device", {**identity, "identity": "device-one",
                                                       "selected": False, "retained_preview_fingerprint": "old"})
            self.assertFalse(stale["ok"])
            self.assertTrue(ctx.read_state()["last_retained_devices_rows"][0]["selected"])
            selected = submit("select_retained_device", {**identity, "identity": "device-one", "selected": False})
            self.assertTrue(selected["ok"])
            self.assertFalse(ctx.read_state()["last_retained_devices_rows"][0]["selected"])
            calls = []
            rejected = submit("retained_devices_delete", {**identity, "candidate": ["device-one"]},
                              lambda *args, **kwargs: calls.append((args, kwargs)))
            self.assertFalse(rejected["ok"])
            self.assertEqual(calls, [])

    def test_projection_excludes_exact_internal_diff_and_hides_preview_under_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            ctx.write_state({"last_internal_ids_preview_id": str(uuid.uuid4()),
                             "last_internal_ids_rows": [{"path": "automations.yaml", "changes": 1,
                                                         "diff": "SECRET_EXACT_DIFF", "diff_sha256": "digest"}]})
            snapshot = web._snapshot_payload(ctx)
            self.assertNotIn("SECRET_EXACT_DIFF", json.dumps(snapshot))
            self.assertEqual(snapshot["state"]["last_internal_ids_rows"][0]["path"], "automations.yaml")
            ctx.write_state({"active_operation": {"command": "apply", "phase": "recovery_required"}})
            hidden = web._snapshot_payload(ctx)["state"]
            self.assertEqual(hidden["last_internal_ids_rows"], [])

    def test_interrupted_save_inspects_local_and_remote_without_changing_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare, repo = root / "origin.git", root / "checkout"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git("config", "user.name", "HA Ops Test")
            git("config", "user.email", "ha-ops@example.invalid")
            git("checkout", "-b", "main")
            (repo / "config.yaml").write_text("before\n")
            git("add", "config.yaml")
            git("commit", "-m", "Before")
            git("push", "-u", "origin", "main")
            pre_tip = git("rev-parse", "HEAD")
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            ctx.load_options = lambda: {"repo_branch": "main"}
            ctx.repo_checkout_path = lambda _options: repo
            ctx.git_env = lambda _options: None
            operation_id = str(uuid.uuid4())
            ctx.write_state({"active_operation": {"command": "save", "command_id": operation_id,
                                                  "phase": "recovery_required"},
                             "save_intent": {"operation_id": operation_id, "branch": "main",
                                             "pre_user_tip": pre_tip, "expected_remote_tip": pre_tip}})
            ctx.inspect_interrupted_save()
            self.assertEqual(ctx.read_state()["active_operation"]["evidence"]["kind"], "precommit_verified")
            projected = web._snapshot_payload(ctx)["state"]["active_operation"]
            self.assertTrue(projected["ack_available"])
            acknowledgement_id = str(uuid.uuid4())
            claimed, record = ctx.acknowledge_verified_recovery(
                acknowledgement_id, operation_id, ctx.read_state()["operation_generation"], projected["evidence_token"],
            )
            self.assertTrue(claimed)
            self.assertEqual(record["status"], "terminal")
            self.assertIsNone(ctx.read_state()["active_operation"])
            duplicate, replay = ctx.acknowledge_verified_recovery(
                acknowledgement_id, operation_id, record["generation"], projected["evidence_token"],
            )
            self.assertFalse(duplicate)
            self.assertEqual(replay, record)
            ctx.write_state({"active_operation": {"command": "save", "command_id": operation_id,
                                                  "phase": "recovery_required"}})

            (repo / "config.yaml").write_text("after\n")
            git("add", "config.yaml")
            git("commit", "-m", f"Save\n\nHA-Ops-Operation: {operation_id}")
            committed_tip = git("rev-parse", "HEAD")
            ctx.inspect_interrupted_save()
            self.assertEqual(ctx.read_state()["active_operation"]["evidence"]["kind"], "exact_retry_available")
            self.assertEqual(git("rev-parse", "HEAD"), committed_tip)
            self.assertEqual(git("rev-parse", "refs/remotes/origin/main"), pre_tip)

            retry_projection = web._snapshot_payload(ctx)["state"]["active_operation"]
            self.assertTrue(retry_projection["retry_available"])
            retry_id = str(uuid.uuid4())
            attempted, retry_record = ctx.retry_interrupted_save(
                retry_id, operation_id, ctx.read_state()["operation_generation"], retry_projection["evidence_token"],
            )
            self.assertTrue(attempted)
            self.assertTrue(retry_record["result"]["ok"])
            repeated, repeated_record = ctx.retry_interrupted_save(
                retry_id, operation_id, retry_record["generation"], retry_projection["evidence_token"],
            )
            self.assertFalse(repeated)
            self.assertEqual(repeated_record, retry_record)
            self.assertEqual(ctx.read_state()["active_operation"]["evidence"]["kind"], "pushed_observed")
            pushed = web._snapshot_payload(ctx)["state"]["active_operation"]
            self.assertFalse(pushed["ack_available"])
            with self.assertRaisesRegex(RuntimeError, "does not prove"):
                ctx.acknowledge_verified_recovery(
                    str(uuid.uuid4()), operation_id, ctx.read_state()["operation_generation"], pushed["evidence_token"],
                )

    def test_apply_recovery_inventory_covers_live_and_intended_managed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, live = root / "source", root / "live"
            for base in (source, live):
                (base / "packages").mkdir(parents=True)
                (base / ".storage").mkdir()
            (source / "configuration.yaml").write_text("intended\n")
            (live / "configuration.yaml").write_text("before\n")
            (source / "packages" / "room.yaml").write_text("intended room\n")
            (live / "packages" / "room.yaml").write_text("before room\n")
            (live / ".storage" / "core.config_entries").write_text("before projection\n")
            dependencies = SimpleNamespace(
                ha_root_patterns=["*.yaml"], ha_root_excludes=set(), ha_dirs=["packages"],
                zigbee2mqtt_paths=[], storage_allowlist=["core.entity_registry"],
            )
            target = {"id": "homeassistant", "type": "homeassistant", "source_path": str(source),
                      "live_path": str(live), "organizer": {"enabled": True}}
            inventory = sync.apply_recovery_inventory([target], dependencies)["homeassistant"]
            self.assertIn("configuration.yaml", inventory["live"])
            self.assertIn("packages/room.yaml", inventory["live"])
            self.assertIn(".storage/core.config_entries", inventory["live"])
            self.assertNotEqual(inventory["live"]["configuration.yaml"], inventory["intended"]["configuration.yaml"])
            (live / "packages" / "unsafe").symlink_to(root)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                sync.apply_recovery_inventory([target], dependencies)

    def test_interrupted_apply_reconciles_recorded_paths_and_refs_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, source, live = root / "checkout", root / "source", root / "live"
            for path in (repo, source, live):
                path.mkdir()
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git("config", "user.name", "HA Ops Test")
            git("config", "user.email", "ha-ops@example.invalid")
            git("checkout", "-b", "main")
            (repo / "README").write_text("before\n")
            git("add", "README")
            git("commit", "-m", "Before")
            git("branch", "ha-ops/ha-live")
            tip = git("rev-parse", "HEAD")
            (source / "config.txt").write_text("intended\n")
            (live / "config.txt").write_text("before\n")
            target = {"id": "addon-demo", "type": "addon", "source_path": str(source), "live_path": str(live)}
            inventory = sync.apply_recovery_inventory([target], SimpleNamespace())
            ctx = app_context.AppContext(data_dir=root / "data", config_dir=root / "ha",
                                         addon_configs_dir=root / "addons", addon_config_path=root / "config.yaml")
            ctx.load_options = lambda: {"repo_branch": "main"}
            ctx.repo_checkout_path = lambda _options: repo
            operation_id = str(uuid.uuid4())
            intent = {"operation_id": operation_id, "phase": "before_service_commit",
                      "live_path_inventory": inventory,
                      "pre_ref_tips": {"refs/heads/main": tip, "refs/heads/ha-ops/ha-live": tip},
                      "selected_paths": ["addon-demo/config.txt"], "release_name": None, "backup_slug": None}
            ctx.write_state({"active_operation": {"command": "apply", "command_id": operation_id,
                                                  "phase": "recovery_required"}, "apply_intent": intent})
            ctx.inspect_interrupted_apply()
            self.assertEqual(ctx.read_state()["active_operation"]["evidence"]["kind"], "prestate_observed")
            self.assertEqual((live / "config.txt").read_text(), "before\n")
            projected = web._snapshot_payload(ctx)["state"]["active_operation"]
            self.assertTrue(projected["ack_available"])
            ctx.write_state({"apply_intent": {**intent, "phase": "before_live_write"}})
            ctx.inspect_interrupted_apply()
            unsafe = web._snapshot_payload(ctx)["state"]["active_operation"]
            self.assertFalse(unsafe["ack_available"])
            with self.assertRaisesRegex(RuntimeError, "does not prove"):
                ctx.acknowledge_verified_recovery(
                    str(uuid.uuid4()), operation_id, ctx.read_state()["operation_generation"], unsafe["evidence_token"],
                )
            ctx.write_state({"apply_intent": intent})
            ctx.inspect_interrupted_apply()
            projected = web._snapshot_payload(ctx)["state"]["active_operation"]
            ctx.acknowledge_verified_recovery(
                str(uuid.uuid4()), operation_id, ctx.read_state()["operation_generation"], projected["evidence_token"],
            )
            self.assertIsNone(ctx.read_state()["active_operation"])
            ctx.write_state({"active_operation": {"command": "apply", "command_id": operation_id,
                                                  "phase": "recovery_required"}})

            (live / "config.txt").write_text("intended\n")
            ctx.write_state({"apply_intent": {**intent, "phase": "before_service_push", "service_commit": tip}})
            ctx.inspect_interrupted_apply()
            self.assertEqual(ctx.read_state()["active_operation"]["evidence"]["kind"], "intended_local_state_observed")
            self.assertFalse(web._snapshot_payload(ctx)["state"]["active_operation"]["ack_available"])
            self.assertEqual(git("rev-parse", "HEAD"), tip)


if __name__ == "__main__":
    unittest.main()
