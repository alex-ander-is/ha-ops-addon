"""Backup continuations exercise real command reservation and Apply execution locally."""
import copy
import io
import json
import tempfile
import unittest
import uuid
from email.message import Message
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import test_server


def backup_fixture(**updates):
    return {"slug": "candidate", "type": "full", "date": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "locations": ["nas"], **updates}


def invalid_backup_inventories():
    """Each malformed family must stay ineligible alone and beside valid backups."""
    entries = {
        "empty entry": {}, "null entry": None, "array entry": [], "text entry": "backup",
        "missing identity": {key: value for key, value in backup_fixture().items() if key != "slug"},
        "empty identity": backup_fixture(slug=""), "non-string identity": backup_fixture(slug=1),
        "unknown type": backup_fixture(type="unknown"), "null type": backup_fixture(type=None),
        "unclassified partial": backup_fixture(type="partial"),
        "null content": backup_fixture(content=None), "array content": backup_fixture(content=[]),
        "string content flag": backup_fixture(content={"homeassistant": "false"}),
        "number content flag": backup_fixture(content={"homeassistant": 1}),
        "contradictory full content": backup_fixture(content={"homeassistant": False}),
        "invalid date": backup_fixture(date="invalid"), "null date": backup_fixture(date=None),
        "number date": backup_fixture(date=123), "naive date": backup_fixture(date="2026-09-01T12:00:00"),
        "missing location": {key: value for key, value in backup_fixture().items() if key != "locations"},
        "empty locations": backup_fixture(locations=[]), "empty location map": backup_fixture(locations={}),
        "zero locations": backup_fixture(locations=0), "negative locations": backup_fixture(locations=-1),
        "boolean locations": backup_fixture(locations=True), "float locations": backup_fixture(locations=1.5),
        "null locations": backup_fixture(locations=None), "string locations": backup_fixture(locations="nas"),
        "empty location member": backup_fixture(locations=[""]),
        "whitespace location member": backup_fixture(locations=[" "]),
        "number location member": backup_fixture(locations=[1]),
        "object location member": backup_fixture(locations=[{}]),
        "duplicate locations": backup_fixture(locations=["nas", "nas"]),
        "invalid map key": backup_fixture(locations={"": {}}),
        "invalid map metadata": backup_fixture(locations={"nas": None}),
        "empty legacy location": {**backup_fixture(), "locations": ["nas"], "location": ""},
        "empty legacy singleton": {key: value for key, value in backup_fixture(location="").items() if key != "locations"},
        "boolean legacy location": backup_fixture(location=False),
        "contradictory locations": backup_fixture(location="other"),
        "contradictory local location": backup_fixture(location=None),
        "contradictory count": backup_fixture(locations=0, location=None),
        "contradictory map": backup_fixture(locations={"nas": {}}, location="other"),
        "fresh without storage": backup_fixture(date=datetime.now(timezone.utc).isoformat(), locations=[]),
    }
    for name, entry in entries.items():
        yield name, {"backups": [entry]}
        for age, date in (("old", (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()),
                          ("fresh", datetime.now(timezone.utc).isoformat())):
            valid = backup_fixture(slug="valid", date=date)
            yield f"{name} after {age}", {"backups": [valid, entry]}
            yield f"{name} before {age}", {"backups": [entry, valid]}
    for name, response in (
        ("missing list", {}), ("null response", None), ("array response", []),
        ("null list", {"backups": None}), ("mapping list", {"backups": {}}),
        ("duplicate identity", {"backups": [backup_fixture(), backup_fixture(date=datetime.now(timezone.utc).isoformat())]}),
    ):
        yield name, response


def invalid_backup_responses():
    for result in ("error", "unknown", None, False, 0, []):
        yield {"result": result, "data": {"backups": []}}
        yield {"result": result, "backups": []}


def invalid_apply_backup_inventories():
    # Policy tests exhaust every ordering. Exercise every singleton family
    # and the original six mixed-inventory counterexamples through real Apply.
    mixed_cases = {f"{case} {order} old" for case in ("invalid date", "fresh without storage", "empty entry")
                   for order in ("before", "after")}
    for name, response in invalid_backup_inventories():
        if (" after " in name or " before " in name) and name not in mixed_cases:
            continue
        yield name, response


def ambiguous_created_backup_inventories():
    fresh = backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat(), locations=[None])
    conflicts = {
        "duplicate slug": dict(fresh),
        "duplicate slug without storage": {**fresh, "locations": []},
        "duplicate slug with stale date": backup_fixture(slug="created"),
        "duplicate slug with invalid date": {**fresh, "date": "invalid"},
        "duplicate fallback identity": {key: value for key, value in {**fresh, "id": "created"}.items() if key != "slug"},
        "crossed identity aliases": {**fresh, "slug": "other", "id": "created"},
        "invalid unrelated entry": {**fresh, "slug": "other", "locations": []},
    }
    for name, conflict in conflicts.items():
        for order, entries in (("before", [conflict, fresh]), ("after", [fresh, conflict])):
            yield f"{name} {order}", {"backups": entries}


class BackupContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = s = test_server.load_server()
        self.helper = h = test_server.ServerTests()
        h.configure_paths(s, self.root)
        remote = h.seed_remote(self.root, file_text="homeassistant:\n  name: Git\n")
        (s.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n  name: Live\n")
        s.OPTIONS_PATH.write_text(json.dumps({
            "repo_url": str(remote), "repo_branch": "main", "repo_path": "ha-config",
            "apply_path": "homeassistant", "require_fresh_backup": True,
            "create_ha_backup": False, "restart_after_apply": False,
        }))
        s.get_installed_addons = lambda: []
        s.do_core_check = lambda: None
        s.core_reload_yaml = lambda: None
        s.backup_manager_info = Mock(return_value={"backups": []})
        self.assertTrue(s.run_preview_job())
        h.select_all_apply_preview_files(s)
        self.ctx = s.context()
        self.background = patch.object(s.web, "start_background", lambda callback: callback())
        self.background.start()
        self.addCleanup(self.background.stop)

    def envelope(self, mode="normal"):
        state = self.ctx.read_state()
        payload = {
            "preview_identity": self.server.web.preview_identity_for_state(state, "apply"),
            "decision_digest": self.server.web.preview_decision_digest(state, "apply"),
        }
        if mode != "normal":
            payload.update(backup_mode=mode, backup_refusal_id=(state.get("apply_backup_refusal") or {}).get("operation_id"))
        return {"command_id": str(uuid.uuid4()), "command": "apply", "generation": state["operation_generation"], "payload": payload}

    def send(self, envelope=None, http=False):
        envelope = envelope or self.envelope()
        if not http:
            result = self.server.web.dispatch_command(self.ctx, "apply", envelope)
        else:
            handler = self.server.web.create_handler(self.ctx)
            req = handler.__new__(handler)
            req.path = "/apply"
            body = json.dumps(envelope).encode()
            req.rfile, req.wfile = io.BytesIO(body), io.BytesIO()
            req.headers = Message()
            for key, value in {"Content-Type": "application/json", "Accept": "application/json", "Content-Length": str(len(body))}.items():
                req.headers[key] = value
            req.send_response = lambda *args: None
            req.send_header = lambda *args: None
            req.end_headers = lambda: None
            req.do_POST()
            result = json.loads(req.wfile.getvalue())
        return result, self.ctx.read_state()

    def refuse(self):
        envelope = self.envelope()
        result, state = self.send(envelope)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(state["active_operation"])
        self.assertEqual(state["apply_backup_refusal"]["operation_id"], envelope["command_id"])
        return state

    def websocket(self, envelope):
        web = self.server.web
        handler = web.create_handler(self.ctx)
        request = handler.__new__(handler)
        request.path = "/api/hassio_ingress/test/ws"
        request.rfile, request.wfile = io.BytesIO(), io.BytesIO()
        request.headers = Message()
        request.headers["Sec-WebSocket-Key"] = "test"
        request.send_response = lambda *args: None
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        frames = []
        envelope = {**envelope, "id": "continuation-wire"}
        with patch.object(web, "read_ws_frame", side_effect=[json.dumps(envelope), None]), patch.object(web, "write_ws_frame", side_effect=lambda stream, frame: frames.append(frame)):
            request.do_GET()
        return next(frame for frame in frames if frame.get("id") == "continuation-wire")

    def test_actual_websocket_acknowledgement_and_http_replay_share_apply_record(self):
        self.refuse()
        envelope = self.envelope("acknowledge")
        with patch.object(self.ctx, "ensure_fresh_system_backup", side_effect=AssertionError("backup gate called")):
            result = self.websocket(envelope)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["type"], "result")
        state = self.ctx.read_state()
        record = state["command_records"][envelope["command_id"]]
        self.assertEqual(record["command"], "apply")
        self.assertEqual(record["status"], "terminal")
        self.assertEqual(state["last_status"], "success")
        duplicate, _ = self.send(envelope, http=True)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["command_record"], record)

    def test_acknowledgement_does_not_authorize_a_later_normal_apply(self):
        self.refuse()
        self.send(self.envelope("acknowledge"))
        (self.server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n  name: Later live edit\n")
        self.assertTrue(self.server.run_preview_job())
        self.helper.select_all_apply_preview_files(self.server)
        with patch.object(self.ctx, "ensure_fresh_system_backup", wraps=self.ctx.ensure_fresh_system_backup) as gate, patch.object(self.ctx, "apply_targets") as apply:
            state = self.refuse()
        gate.assert_called_once()
        apply.assert_not_called()
        self.assertIsNotNone(state["apply_backup_refusal"])

    def test_crash_after_continuation_claim_cannot_restore_consent(self):
        self.refuse()
        envelope = self.envelope("acknowledge")
        queued = []
        with patch.object(self.server.web, "start_background", queued.append):
            self.assertTrue(self.send(envelope)[0]["ok"])
        accepted = self.ctx.read_state()
        self.assertEqual(accepted["active_operation"]["phase"], "accepted")
        self.assertEqual(accepted["active_operation"]["command"], "apply")
        self.assertIsNone(accepted["apply_backup_refusal"])
        restarted = self.server.app_context.state_store.OperationStore(self.server.STATE_PATH)
        restarted.begin_repair()
        restarted.reconcile_startup_fence()
        recovered = restarted.read_state()
        self.assertIsNone(recovered["active_operation"])
        self.assertIsNone(recovered["apply_backup_refusal"])
        self.assertEqual(recovered["command_records"][envelope["command_id"]]["status"], "terminal")
        self.assertFalse(recovered["command_records"][envelope["command_id"]]["result"]["ok"])
        self.assertIsNone(recovered["apply_preview_id"])
        self.assertIn("name: Live", (self.server.CONFIG_DIR / "configuration.yaml").read_text())
        self.ctx.run_lock.release()

    def test_crash_after_continuation_dispatch_preserves_apply_recovery_fence(self):
        self.refuse()
        envelope = self.envelope("acknowledge")
        with patch.object(self.server.web, "start_background", lambda callback: None):
            self.assertTrue(self.send(envelope)[0]["ok"])
        self.ctx.update_command(envelope["command_id"], "running")
        restarted = self.server.app_context.state_store.OperationStore(self.server.STATE_PATH)
        restarted.begin_repair()
        restarted.reconcile_startup_fence()
        recovered = restarted.read_state()
        self.assertEqual(recovered["active_operation"]["command"], "apply")
        self.assertEqual(recovered["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(recovered["apply_backup_refusal"])
        self.assertIn("name: Live", (self.server.CONFIG_DIR / "configuration.yaml").read_text())
        self.ctx.run_lock.release()

    def test_acknowledgement_rejects_new_limit_failure_before_snapshot_or_apply(self):
        self.refuse()
        with patch.object(self.ctx, "enforce_apply_limits", side_effect=RuntimeError("limit exceeded")), patch.object(self.ctx, "create_release_snapshot") as snapshot, patch.object(self.ctx, "apply_targets") as apply:
            _, state = self.send(self.envelope("acknowledge"))
        snapshot.assert_not_called()
        apply.assert_not_called()
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")

    def test_mixed_invalid_backup_inventory_cannot_publish_continuation(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.server.backup_manager_info = Mock(return_value={"backups": [
            {"slug": "old", "type": "full", "date": (now - timedelta(days=2)).isoformat(), "locations": ["nas"]},
            {"slug": "unknown-age", "type": "full", "date": "invalid", "locations": ["nas"]},
        ]})
        _, state = self.send()
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")

    def test_real_local_backup_stale_refusal_then_fresh_retry(self):
        # Supervisor's null list member denotes local /backup storage.
        self.server.backup_manager_info = Mock(return_value={"backups": [backup_fixture(locations=[None])]})
        self.refuse()
        self.server.backup_manager_info.return_value = {"backups": [backup_fixture(locations=[None], date=datetime.now(timezone.utc).isoformat())]}
        _, state = self.send(self.envelope("retry"))
        self.assertEqual(state["last_status"], "success", state["last_details"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertIsNone(state["active_operation"])
        self.assertIn("name: Git", (self.server.CONFIG_DIR / "configuration.yaml").read_text())

    def prepare_retry_with_backup_creation(self, response, post_create_backup=None, *, inventory=None):
        old = backup_fixture(slug="existing", locations=[None])
        self.server.backup_manager_info = Mock(return_value={"backups": [old]})
        self.refuse()
        options = json.loads(self.server.OPTIONS_PATH.read_text())
        self.server.OPTIONS_PATH.write_text(json.dumps({**options, "create_ha_backup": True}))
        self.server.default_backup_mount = Mock(return_value="nas")
        # Leave create_ha_backup real so its Supervisor envelope validation is
        # exercised together with the command, policy and Apply recovery path.
        self.ctx.call_supervisor = Mock(return_value=response)
        self.server.backup_manager_info = Mock(side_effect=[
            {"backups": [old]}, inventory if inventory is not None else {"backups": [post_create_backup]},
        ])
        return self.envelope("retry")

    def assert_retry_creation_failure(self, envelope, message):
        with patch.object(self.ctx, "create_release_snapshot", wraps=self.ctx.create_release_snapshot) as snapshot, patch.object(self.ctx, "commit_apply_merge", wraps=self.ctx.commit_apply_merge) as commit, patch.object(self.ctx, "apply_targets", wraps=self.ctx.apply_targets) as apply:
            result, state = self.send(envelope)
        self.assertTrue(result["ok"], result)
        self.assertEqual(state["last_status"], "error")
        self.assertIn(message, state["last_message"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertEqual(state["active_operation"]["command_id"], envelope["command_id"])
        self.assertFalse(state["command_records"][envelope["command_id"]]["result"]["ok"])
        self.assertNotIn("Created fresh system backup:", "\n".join(state["last_details"]))
        snapshot.assert_not_called(); commit.assert_not_called(); apply.assert_not_called()
        self.assertIn("name: Live", (self.server.CONFIG_DIR / "configuration.yaml").read_text())
        self.assertFalse(self.send(self.envelope("acknowledge"))[0]["ok"])

    def test_real_retry_rejects_creation_error_even_with_existing_slug(self):
        envelope = self.prepare_retry_with_backup_creation(
            {"result": "error", "data": {"slug": "existing"}},
            backup_fixture(slug="existing", locations=[None]),
        )
        self.assert_retry_creation_failure(envelope, "Backup creation failed")
        self.assertEqual(self.server.backup_manager_info.call_count, 1)

    def test_real_retry_rejects_successful_creation_with_stale_record(self):
        envelope = self.prepare_retry_with_backup_creation(
            {"result": "ok", "data": {"slug": "existing"}},
            backup_fixture(slug="existing", locations=[None]),
        )
        self.assert_retry_creation_failure(envelope, "older than 24 hour(s)")
        self.assertEqual(self.server.backup_manager_info.call_count, 2)

    def test_real_retry_applies_after_successfully_creating_fresh_backup(self):
        envelope = self.prepare_retry_with_backup_creation(
            {"result": "ok", "data": {"slug": "created"}},
            backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat(), locations=[None]),
        )
        _, state = self.send(envelope)
        self.ctx.call_supervisor.assert_called_once()
        method, endpoint, payload = self.ctx.call_supervisor.call_args.args
        self.assertEqual((method, endpoint), ("POST", "/backups/new/full"))
        self.assertTrue(payload["name"].startswith("ha-ops "))
        self.assertEqual(payload["background"], False)
        self.assertEqual(payload["location"], "nas")
        self.assertEqual(state["last_status"], "success", state["last_details"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertIsNone(state["active_operation"])
        self.assertIn("Created fresh system backup:", "\n".join(state["last_details"]))
        self.assertIn("name: Git", (self.server.CONFIG_DIR / "configuration.yaml").read_text())

    def test_real_apply_still_creates_configured_backup_before_writing(self):
        options = json.loads(self.server.OPTIONS_PATH.read_text())
        self.server.OPTIONS_PATH.write_text(json.dumps({**options, "create_ha_backup": True}))
        self.server.default_backup_mount = Mock(return_value="nas")
        self.server.create_ha_backup = Mock(return_value="created")
        self.server.backup_manager_info = Mock(side_effect=[{"backups": []}, {"backups": [backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat())]}])
        _, state = self.send()
        self.server.create_ha_backup.assert_called_once_with("ha-ops", backup_location="nas")
        self.assertEqual(state["last_status"], "success", state["last_details"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertIsNone(state["active_operation"])
        self.assertIn("name: Git", (self.server.CONFIG_DIR / "configuration.yaml").read_text())

    def test_websocket_replay_and_completion_during_snapshot_are_delivered(self):
        import socket
        web = self.server.web
        handler = web.create_handler(self.ctx)
        request = handler.__new__(handler)
        request.path = "/api/hassio_ingress/test/ws"
        request.rfile, request.wfile = io.BytesIO(), io.BytesIO()
        request.headers = Message(); request.headers["Sec-WebSocket-Key"] = "test"
        request.send_response = lambda *args: None
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        emitted, snapshots = [], []
        def frames(ctx, base_revision=None):
            snapshot = web._snapshot_payload(ctx)
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                ctx.write_state({"last_message": "terminal arrived during snapshot"})
            return [{"type": "state", **snapshot}]
        with patch.object(web, "read_ws_frame", side_effect=[json.dumps({"id": "replay-1", "command": "replay"}), socket.timeout(), None]), patch.object(web, "write_ws_frame", side_effect=lambda stream, frame: emitted.append(frame)), patch.object(web, "ws_state_frames", frames):
            request.do_GET()
        self.assertTrue(any(frame.get("id") == "replay-1" and frame["type"] == "replay" for frame in emitted))
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1]["state"]["last_message"], "terminal arrived during snapshot")

    def test_refusal_preserves_preview_diff_decisions_and_blocks_effects(self):
        before = self.ctx.read_state()
        with patch.object(self.ctx, "create_release_snapshot") as snapshot, patch.object(self.ctx, "commit_apply_merge") as commit, patch.object(self.ctx, "apply_targets") as apply:
            after = self.refuse()
        snapshot.assert_not_called(); commit.assert_not_called(); apply.assert_not_called()
        for key in ("apply_preview_id", "apply_decision_revision", "last_preview_commit", "last_preview_fingerprint", "apply_preview_selected_paths", "apply_preview_resolutions", "last_diff", "last_diff_cursor", "operation_generation"):
            self.assertEqual(before.get(key), after.get(key), key)
        self.assertTrue(self.ctx.operation_store.diff_get(after["last_diff_cursor"]))
        self.assertFalse(after["command_records"][after["apply_backup_refusal"]["operation_id"]]["result"]["ok"])

    def test_acknowledge_once_skips_only_backup_and_still_snapshots_http_and_ws(self):
        self.refuse()
        envelope = self.envelope("acknowledge")
        with patch.object(self.ctx, "ensure_fresh_system_backup", side_effect=AssertionError("backup gate called")) as gate, patch.object(self.ctx, "create_release_snapshot", wraps=self.ctx.create_release_snapshot) as snapshot, patch.object(self.ctx, "enforce_apply_limits", wraps=self.ctx.enforce_apply_limits) as limits:
            result, state = self.send(envelope, http=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(state["last_status"], "success", state["last_details"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertIsNone(state["active_operation"])
        gate.assert_not_called(); snapshot.assert_called_once(); limits.assert_called_once()
        self.assertIn("name: Git", (self.server.CONFIG_DIR / "configuration.yaml").read_text())
        duplicate, _ = self.send(envelope)
        self.assertTrue(duplicate["duplicate"])
        reused = copy.deepcopy(envelope); reused["command_id"] = str(uuid.uuid4())
        self.assertFalse(self.send(reused)[0]["ok"])
        self.assertTrue(json.loads(self.server.OPTIONS_PATH.read_text())["require_fresh_backup"])

    def test_retry_rechecks_and_issues_new_refusal_then_fresh_proceeds(self):
        first = self.refuse()["apply_backup_refusal"]
        result, second = self.send(self.envelope("retry"), http=True)
        self.assertTrue(result["ok"])
        self.assertNotEqual(first["operation_id"], second["apply_backup_refusal"]["operation_id"])
        with patch.object(self.ctx, "ensure_fresh_system_backup", return_value="fresh") as gate:
            result, third = self.send(self.envelope("retry"))
        gate.assert_called_once()
        self.assertEqual(third["last_status"], "success", third["last_details"])

    def test_fabricated_and_stale_acknowledgement_rejected_at_claim(self):
        self.assertFalse(self.send(self.envelope("acknowledge"))[0]["ok"])
        self.refuse()
        for field, value in (("backup_refusal_id", "fake"), ("decision_digest", "fake"), ("backup_mode", "skip")):
            with self.subTest(field=field):
                envelope = self.envelope("acknowledge"); envelope["payload"][field] = value
                self.assertFalse(self.send(envelope)[0]["ok"])
        for field in self.envelope("acknowledge")["payload"]["preview_identity"]:
            with self.subTest(identity_field=field):
                envelope = self.envelope("acknowledge"); envelope["payload"]["preview_identity"][field] = {"paths": ["different"], "conflict_paths": ["different"], "live_fingerprints": {"different": "hash"}, "decision_revision": 42}.get(field, "different")
                self.assertFalse(self.send(envelope)[0]["ok"])
        envelope = self.envelope("acknowledge"); envelope["generation"] -= 1
        self.assertFalse(self.send(envelope)[0]["ok"])
        self.assertIsNotNone(self.ctx.read_state()["apply_backup_refusal"])

    def test_claim_consumes_warning_reserves_apply_and_deduplicates(self):
        self.refuse()
        ack, retry = self.envelope("acknowledge"), self.envelope("retry")
        queued = []
        with patch.object(self.server.web, "start_background", queued.append):
            self.assertTrue(self.send(ack)[0]["ok"])
            state = self.ctx.read_state()
            self.assertIsNone(state["apply_backup_refusal"])
            self.assertEqual(state["active_operation"]["command"], "apply")
            self.assertTrue(self.send(ack)[0]["duplicate"])
            self.assertFalse(self.send(retry)[0]["ok"])
            changed = copy.deepcopy(ack); changed["payload"]["backup_mode"] = "retry"
            self.assertFalse(self.send(changed)[0]["ok"])
        self.assertEqual(len(queued), 1)
        queued[0]()
        self.assertEqual(self.ctx.read_state()["last_status"], "success")

    def test_acknowledge_rebuilds_preview_and_refuses_changed_live(self):
        self.refuse()
        envelope = self.envelope("acknowledge")
        (self.server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n  name: Changed\n")
        with patch.object(self.ctx, "ensure_fresh_system_backup") as gate, patch.object(self.ctx, "apply_targets") as apply:
            _, state = self.send(envelope)
        gate.assert_not_called(); apply.assert_not_called()
        self.assertEqual(state["last_status"], "warning")
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_downstream_failure_and_restart_keep_fence_without_warning(self):
        self.refuse()
        with patch.object(self.ctx, "create_release_snapshot", side_effect=RuntimeError("snapshot failed")):
            _, state = self.send(self.envelope("acknowledge"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(state.get("apply_backup_refusal"))
        store = self.server.app_context.state_store.OperationStore(self.server.STATE_PATH)
        store.begin_repair()
        self.assertEqual(store.reconcile_startup_fence()["phase"], "recovery_required")

    def test_interruption_at_gate_or_refusal_finalization_never_releases_fence(self):
        for after_refusal in (False, True):
            with self.subTest(after_refusal=after_refusal):
                envelope = self.envelope()
                if after_refusal:
                    original = self.ctx.update_command
                    def fail_finalize(command_id, status, result=None):
                        if result and result.get("backup_refusal"):
                            raise RuntimeError("interrupted finalization")
                        return original(command_id, status, result)
                    manager = patch.object(self.ctx, "update_command", fail_finalize)
                else:
                    manager = patch.object(self.ctx, "ensure_fresh_system_backup", side_effect=KeyboardInterrupt("interrupted"))
                with manager:
                    try:
                        self.send(envelope)
                    except (KeyboardInterrupt, RuntimeError):
                        pass
                state = self.ctx.read_state()
                self.assertEqual(state["active_operation"]["phase"], "recovery_required")
                self.assertIsNone(state.get("apply_backup_refusal"))
                self.ctx.write_state({"active_operation": None, "command_records": {}})

    def test_stale_refusal_outcome_cannot_release_another_operation(self):
        old = self.refuse()["apply_backup_refusal"]
        def run_apply_job(*args, lock_acquired=False):
            self.ctx.write_state({"last_status": "error", "last_message": "unrelated failure"})
            self.ctx.run_lock.release()
            return self.server.web.job_logic.ApplyBackupRefusal(old["operation_id"], old["generation"], 24)
        with patch.object(self.ctx, "run_apply_job", run_apply_job):
            _, state = self.send(self.envelope("retry"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_downstream_live_write_failure_rolls_back_with_snapshot(self):
        self.refuse()
        with patch.object(self.ctx, "apply_targets", side_effect=RuntimeError("live write failed")), patch.object(self.ctx, "restore_release_snapshot", wraps=self.ctx.restore_release_snapshot) as restore:
            _, state = self.send(self.envelope("acknowledge"))
        restore.assert_called_once()
        self.assertEqual(state["apply_intent"]["phase"], "caught_rollback_complete")
        self.assertIsNone(state["active_operation"])
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_downstream_failure_without_snapshot_stays_fenced(self):
        options = json.loads(self.server.OPTIONS_PATH.read_text()); options["create_release_snapshot"] = False
        self.server.OPTIONS_PATH.write_text(json.dumps(options))
        self.refuse()
        with patch.object(self.ctx, "apply_targets", side_effect=RuntimeError("live write failed")), patch.object(self.ctx, "create_release_snapshot") as snapshot:
            _, state = self.send(self.envelope("acknowledge"))
        snapshot.assert_not_called()
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_downstream_service_commit_failure_preserves_existing_rollback_rules(self):
        self.refuse()
        with patch.object(self.ctx, "commit_apply_merge", side_effect=RuntimeError("commit failed")), patch.object(self.ctx, "restore_release_snapshot", wraps=self.ctx.restore_release_snapshot) as restore:
            _, state = self.send(self.envelope("acknowledge"))
        restore.assert_called_once()
        self.assertIsNone(state["active_operation"])
        self.assertIsNone(state.get("apply_backup_refusal"))
        self.assertIn("name: Live", (self.server.CONFIG_DIR / "configuration.yaml").read_text())

    def test_rollback_failure_retains_recovery_fence(self):
        self.refuse()
        with patch.object(self.ctx, "apply_targets", side_effect=RuntimeError("live write failed")), patch.object(self.ctx, "restore_release_snapshot", side_effect=RuntimeError("rollback failed")):
            _, state = self.send(self.envelope("acknowledge"))
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_retry_and_acknowledge_concurrent_claims_accept_only_one(self):
        import threading
        self.refuse()
        envelopes = [self.envelope("retry"), self.envelope("acknowledge")]
        barrier = threading.Barrier(3)
        results, queued = [], []
        def submit(envelope):
            barrier.wait()
            results.append(self.send(envelope)[0]["ok"])
        with patch.object(self.server.web, "start_background", queued.append):
            workers = [threading.Thread(target=submit, args=(envelope,)) for envelope in envelopes]
            for worker in workers: worker.start()
            barrier.wait()
            for worker in workers: worker.join(timeout=5)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(queued), 1)
        queued[0]()

    def test_warning_expires_on_decisions_clear_preview_and_restart(self):
        for update in ({"apply_decision_revision": 2}, {"apply_preview_selected_paths": []}, {"apply_preview_resolutions": {}}, self.server.app_context.state_store.APPLY_PREVIEW_CLEAR_UPDATES):
            with self.subTest(update=update):
                self.ctx.write_state({"apply_backup_refusal": {"operation_id": "old"}})
                self.ctx.write_state(update)
                self.assertIsNone(self.ctx.read_state()["apply_backup_refusal"])
        self.ctx.write_state({"apply_backup_refusal": {"operation_id": "old"}})
        self.ctx.operation_store.begin_repair()
        self.assertIsNone(self.ctx.read_state()["apply_backup_refusal"])

    def test_restart_and_version_repair_expire_warning_without_auto_apply(self):
        original = self.refuse()
        stale = self.envelope("acknowledge")
        state_store = self.server.app_context.state_store
        for version in ("2.1.1", "2.2.0"):
            with self.subTest(version=version):
                self.ctx.write_state(original)
                self.ctx.write_state({"apply_backup_refusal": original["apply_backup_refusal"], "last_seen_addon_version": "2.1.1"})
                with patch.object(self.ctx, "apply_targets") as apply:
                    repaired = state_store.repair_startup_state(self.server.STATE_PATH, self.ctx.utc_now(), addon_version=version)
                apply.assert_not_called()
                self.assertIsNone(repaired.get("apply_backup_refusal"))
                self.assertFalse(self.send(stale)[0]["ok"])

    def test_acknowledgement_preserves_protected_storage_and_conflict_clean_deletion_contracts(self):
        # Reuse the full branch/stage fixtures, routing their final Apply through
        # actual refusal -> acknowledgement instead of duplicating fixture setup.
        load = test_server.load_server
        def load_with_acknowledged_apply():
            server = load()
            ctx = server.context()
            real_apply = ctx.run_apply_job
            def run_apply_job(backup_mode="normal", lock_acquired=False):
                if lock_acquired:
                    return real_apply(backup_mode=backup_mode, lock_acquired=True)
                def envelope(mode):
                    state = ctx.read_state()
                    payload = {"preview_identity": server.web.preview_identity_for_state(state, "apply"),
                               "decision_digest": server.web.preview_decision_digest(state, "apply")}
                    if mode == "acknowledge":
                        payload.update(backup_mode=mode, backup_refusal_id=state["apply_backup_refusal"]["operation_id"])
                    return {"command_id": str(uuid.uuid4()), "generation": state["operation_generation"], "payload": payload}
                import backups
                with patch.object(ctx, "ensure_fresh_system_backup", side_effect=backups.FreshBackupRequired(24, "missing")):
                    first = server.web.dispatch_command(ctx, "apply", envelope("normal"))
                self.assertTrue(first["ok"], first)
                self.assertIsNotNone(ctx.read_state().get("apply_backup_refusal"))
                with patch.object(ctx, "ensure_fresh_system_backup", side_effect=AssertionError("acknowledgement queried backup")):
                    second = server.web.dispatch_command(ctx, "apply", envelope("acknowledge"))
                self.assertTrue(second["ok"], second)
                return ctx.read_state()["last_status"] == "success"
            ctx.run_apply_job = run_apply_job
            return server
        for scenario in ("test_apply_preview_protected_storage_conflict_can_apply_git_version", "test_apply_preview_conflict_applies_clean_git_delete"):
            with self.subTest(scenario=scenario), patch.object(test_server, "load_server", load_with_acknowledged_apply):
                getattr(self.helper, scenario)()

    def test_unreserved_direct_job_cannot_request_backup_skip(self):
        with patch.object(self.ctx, "apply_targets") as apply:
            self.assertFalse(self.ctx.run_apply_job(backup_mode="acknowledge"))
        apply.assert_not_called()

    def test_same_text_generic_backup_failure_not_eligible(self):
        with patch.object(self.ctx, "ensure_fresh_system_backup", side_effect=RuntimeError("No fresh system backup found within 24 hour(s)")):
            _, state = self.send()
        self.assertEqual(state["active_operation"]["phase"], "recovery_required")
        self.assertIsNone(state.get("apply_backup_refusal"))

    def test_other_backup_callers_never_publish_apply_continuation_or_stop_core(self):
        # A typed refusal is eligible only at Apply's guarded backup stage.
        # The same policy exception must remain a normal failure elsewhere.
        initial = self.ctx.read_state()
        for action in ("deleted_devices_delete", "deleted_devices_revert"):
            with self.subTest(action=action):
                self.ctx.write_state({**copy.deepcopy(initial),
                    "last_deleted_devices_fingerprint": "reviewed-registry",
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_device_count": 1,
                    "deleted_devices_pending_confirmation": action.endswith("revert"),
                    "deleted_devices_rollback_path": str(self.root / "unused-rollback.json"),
                })
                with patch.object(self.ctx, "device_registry_fingerprint", return_value="reviewed-registry"), patch.object(self.ctx, "deleted_devices_cleanup_status", return_value={}), patch.object(self.ctx, "ensure_fresh_system_backup", wraps=self.ctx.ensure_fresh_system_backup) as gate, patch.object(self.ctx, "core_stop") as stop, patch.object(self.ctx, "core_start") as start, patch.object(self.ctx, "clear_deleted_devices") as clear, patch.object(self.ctx, "restore_deleted_devices_rollback") as restore:
                    self.assertFalse(getattr(self.ctx, f"run_{action}_job")())
                gate.assert_called_once()
                stop.assert_not_called(); start.assert_not_called(); clear.assert_not_called(); restore.assert_not_called()
                state = self.ctx.read_state()
                self.assertEqual(state["last_status"], "error")
                self.assertIn("No fresh system backup", state["last_message"])
                self.assertIsNone(state.get("apply_backup_refusal"))


@pytest.fixture
def continuation_case():
    # Keep the existing real Git/config setup and its cleanup stack, including
    # cleanup after a partial setup failure. Each parameter gets a fresh case.
    case = BackupContinuationTests()
    try:
        case.setUp()
        yield case
    finally:
        assert case.doCleanups(), "Backup continuation fixture cleanup failed"


@pytest.mark.parametrize("case_name", [name for name, _ in ambiguous_created_backup_inventories()])
def test_real_retry_rejects_ambiguous_post_creation_inventory_in_either_order(continuation_case, case_name):
    # Names are deterministic across xdist workers; build timestamped payloads
    # at execution time so a collection delay cannot age a fresh backup.
    inventory = dict(ambiguous_created_backup_inventories())[case_name]
    case = continuation_case
    envelope = case.prepare_retry_with_backup_creation(
        {"result": "ok", "data": {"slug": "created"}}, inventory=inventory,
    )
    case.assert_retry_creation_failure(envelope, "inventory is invalid")
    creation_calls = [call for call in case.ctx.call_supervisor.call_args_list
                      if call.args[:2] == ("POST", "/backups/new/full")]
    case.assertEqual(len(creation_calls), 1)
    case.assertEqual(case.server.backup_manager_info.call_count, 2)


@pytest.mark.parametrize("case_name", [name for name, _ in invalid_apply_backup_inventories()])
def test_invalid_inventory_families_retain_real_apply_fence_without_effects(continuation_case, case_name):
    case = continuation_case
    case.server.backup_manager_info = Mock(return_value=dict(invalid_apply_backup_inventories())[case_name])
    with patch.object(case.ctx, "create_release_snapshot") as snapshot, patch.object(case.ctx, "commit_apply_merge") as commit, patch.object(case.ctx, "apply_targets") as apply:
        envelope = case.envelope()
        result, state = case.send(envelope)
    case.assertTrue(result["ok"], result)
    case.assertIsNone(state.get("apply_backup_refusal"))
    case.assertEqual(state["active_operation"]["phase"], "recovery_required")
    case.assertEqual(state["active_operation"]["command_id"], envelope["command_id"])
    case.assertFalse(state["command_records"][envelope["command_id"]]["result"]["ok"])
    snapshot.assert_not_called(); commit.assert_not_called(); apply.assert_not_called()
    case.assertIn("name: Live", (case.server.CONFIG_DIR / "configuration.yaml").read_text())
    case.assertFalse(case.send(case.envelope("acknowledge"))[0]["ok"])


@pytest.mark.parametrize("response", [
    pytest.param(response, id=f"{index:02d}-{'wrapped' if 'data' in response else 'flat'}")
    for index, response in enumerate(invalid_backup_responses())
])
def test_error_response_with_backup_data_cannot_publish_continuation(continuation_case, response):
    import supervisor
    case = continuation_case
    case.server.backup_manager_info = lambda: supervisor.backup_manager_info(Mock(return_value=response))
    with patch.object(case.ctx, "create_release_snapshot") as snapshot, patch.object(case.ctx, "commit_apply_merge") as commit, patch.object(case.ctx, "apply_targets") as apply:
        _, state = case.send()
        case.assertFalse(case.send(case.envelope("acknowledge"))[0]["ok"])
    case.assertIsNone(state.get("apply_backup_refusal"))
    case.assertEqual(state["active_operation"]["phase"], "recovery_required")
    snapshot.assert_not_called(); commit.assert_not_called(); apply.assert_not_called()
    case.assertIn("name: Live", (case.server.CONFIG_DIR / "configuration.yaml").read_text())


class BackupPolicyTests(unittest.TestCase):
    def test_identity_alias_collisions_fail_in_both_phases_and_orders(self):
        s = self.server()
        import backups
        # Exercise id-id, slug-id, id-slug, and slug-slug overlap even
        # when the preferred slug would otherwise make rows look distinct.
        for left_key in ("slug", "id"):
            for right_key in ("slug", "id"):
                left = backup_fixture(slug="created", id="left-id", date=datetime.now(timezone.utc).isoformat())
                right = backup_fixture(slug="other", id="right-id")
                left[left_key] = right[right_key] = "collision"
                for entries in ([left, right], [right, left]):
                    for create in (False, True):
                        with self.subTest(left=left_key, right=right_key, entries=entries, create=create):
                            response = {"backups": entries}
                            s.backup_manager_info = Mock(side_effect=[{"backups": []}, response]) if create else Mock(return_value=response)
                            s.default_backup_mount = Mock(return_value="nas")
                            s.create_ha_backup = Mock(return_value=left["slug"])
                            with self.assertRaises(RuntimeError) as caught:
                                s.ensure_fresh_system_backup({"create_ha_backup": create}, [])
                            self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
                            self.assertIn("duplicate identities", str(caught.exception))

    def test_malformed_non_system_rows_cannot_be_discarded_in_either_phase(self):
        s = self.server()
        import backups
        fresh = backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat())
        for malformed in ({"date": "invalid"}, {"locations": []}, {"id": False}):
            unrelated = backup_fixture(slug="unrelated", type="partial", content={"homeassistant": False}, **malformed)
            for entries in ([fresh, unrelated], [unrelated, fresh]):
                for create in (False, True):
                    with self.subTest(malformed=malformed, entries=entries, create=create):
                        response = {"backups": entries}
                        s.backup_manager_info = Mock(side_effect=[{"backups": []}, response]) if create else Mock(return_value=response)
                        s.default_backup_mount = Mock(return_value="nas")
                        s.create_ha_backup = Mock(return_value="created")
                        with self.assertRaises(RuntimeError) as caught:
                            s.ensure_fresh_system_backup({"create_ha_backup": create}, [])
                        self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)

    def test_post_creation_error_envelopes_cannot_hide_behind_valid_inventory(self):
        s = self.server()
        import backups
        import supervisor
        fresh = backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat())
        for result in ("error", "unknown", None, False, 0, []):
            for wrapped in (False, True):
                with self.subTest(result=result, wrapped=wrapped):
                    inventory = {"backups": [fresh]}
                    response = {"result": result, **({"data": inventory} if wrapped else inventory)}
                    supervisor_call = Mock(side_effect=[{"result": "ok", "data": {"backups": []}}, response])
                    s.backup_manager_info = lambda: supervisor.backup_manager_info(supervisor_call)
                    s.default_backup_mount = Mock(return_value="nas")
                    s.create_ha_backup = Mock(return_value="created")
                    details = []
                    with self.assertRaises(RuntimeError) as caught:
                        s.ensure_fresh_system_backup({"create_ha_backup": True}, details)
                    self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
                    self.assertNotIn("Created fresh system backup:", "\n".join(details))
                    self.assertEqual(supervisor_call.call_count, 2)

    def test_created_id_only_and_equal_identity_aliases_preserve_local_storage(self):
        s = self.server()
        for identity in ({"id": "created"}, {"slug": "created", "id": "created"}):
            for location in ({"location": None}, {"locations": [None]}):
                with self.subTest(identity=identity, location=location):
                    fresh = {"type": "full", "date": datetime.now(timezone.utc).isoformat(), **identity, **location}
                    s.backup_manager_info = Mock(side_effect=[{"backups": []}, {"backups": [fresh]}])
                    s.default_backup_mount = Mock(return_value="nas")
                    s.create_ha_backup = Mock(return_value="created")
                    self.assertEqual(s.ensure_fresh_system_backup({"create_ha_backup": True}, []), "created")

    def test_create_backup_rejects_unsuccessful_or_invalid_response_with_slug(self):
        test_server.load_server()
        import supervisor
        for result in ("error", "unknown", None, False, 0, []):
            for data in ({"data": {"slug": "existing"}}, {"slug": "existing"}):
                with self.subTest(result=result, data=data):
                    with self.assertRaisesRegex(RuntimeError, "Backup creation failed"):
                        supervisor.create_ha_backup("ha-ops", None, Mock(return_value={"result": result, **data}), lambda: "now")
        for response in (None, [], {"result": "ok", "data": []}, {"data": None},
                         {"data": {"slug": True}}, {"data": {"slug": " "}}, {"data": {}}):
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    supervisor.create_ha_backup("ha-ops", None, Mock(return_value=response), lambda: "now")

    def test_create_backup_preserves_supported_success_envelopes(self):
        test_server.load_server()
        import supervisor
        for response in ({"result": "ok", "data": {"slug": "created"}},
                         {"data": {"slug": "created"}}, {"slug": "created"}):
            with self.subTest(response=response):
                self.assertEqual(supervisor.create_ha_backup("ha-ops", None, Mock(return_value=response), lambda: "now"), "created")

    def test_created_backup_obeys_configured_freshness_limit(self):
        s = self.server()
        import backups
        for max_age in (2, 72):
            for age_seconds in (max_age * 3600 - 1, max_age * 3600, max_age * 3600 + 1):
                with self.subTest(max_age=max_age, age_seconds=age_seconds):
                    # First inventory has no backup, forcing creation. Freeze
                    # its age to exercise the same inclusive limit as status.
                    s.backup_manager_info = Mock(side_effect=[{"backups": []}, {"backups": [backup_fixture(slug="created")]}])
                    s.default_backup_mount = Mock(return_value="nas")
                    s.create_ha_backup = Mock(return_value="created")
                    details = []
                    with patch.object(backups, "backup_age_seconds", return_value=age_seconds):
                        options = {"create_ha_backup": True, "backup_max_age_hours": max_age}
                        if age_seconds <= max_age * 3600:
                            self.assertEqual(s.ensure_fresh_system_backup(options, details), "created")
                            self.assertIn("Created fresh system backup:", "\n".join(details))
                        else:
                            with self.assertRaisesRegex(RuntimeError, f"older than {max_age} hour") as caught:
                                s.ensure_fresh_system_backup(options, details)
                            self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
                            self.assertNotIn("Created fresh system backup:", "\n".join(details))

    def server(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = test_server.load_server()
        test_server.ServerTests().configure_paths(server, Path(temp.name))
        return server

    def test_entire_inventory_must_be_classified_before_missing_or_stale(self):
        s = self.server()
        import backups
        for name, response in invalid_backup_inventories():
            with self.subTest(case=name):
                s.backup_manager_info = Mock(return_value=response)
                status = s.latest_system_backup_status({})
                self.assertFalse(status["available"])
                self.assertIsNone(status.get("refusal_reason"))
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)

    def test_backup_response_envelopes_preserve_success_and_reject_errors(self):
        s = self.server()
        import backups
        import supervisor
        for response in invalid_backup_responses():
            with self.subTest(response=response):
                s.backup_manager_info = lambda: supervisor.backup_manager_info(Mock(return_value=response))
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
        for response in ({"backups": []}, {"data": {"backups": []}}, {"result": "ok", "data": {"backups": []}}):
            with self.subTest(response=response):
                s.backup_manager_info = lambda: supervisor.backup_manager_info(Mock(return_value=response))
                with self.assertRaises(backups.FreshBackupRequired):
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])

    def test_supported_location_and_system_backup_shapes_keep_freshness_semantics(self):
        s = self.server()
        import backups
        locations = [
            {"location": None}, {"location": "nas"}, {"locations": [None]},
            {"locations": ["nas"]}, {"locations": [None, "nas"], "location": None},
            {"locations": ["nas", None], "location": "nas"},
            {"locations": {"nas": {}}}, {"locations": {".local": {}}, "location": None},
            {"locations": 1}, {"locations": 2, "location": "nas"},
        ]
        types = [
            {"type": "full"}, {"type": "FULL"}, {"type": "automatic"}, {"type": "auto"},
            {"type": "partial", "content": {"homeassistant": True}},
            {"content": {"homeassistant": True}},
        ]
        for location in locations:
            for backup_type in types:
                for stale in (False, True):
                    with self.subTest(location=location, backup_type=backup_type, stale=stale):
                        entry = {"id": "supported", "date": (datetime.now(timezone.utc) - timedelta(hours=48 if stale else 1)).isoformat(),
                                 **backup_type, **location}
                        s.backup_manager_info = Mock(return_value={"backups": [entry]})
                        if stale:
                            with self.assertRaises(backups.FreshBackupRequired):
                                s.ensure_fresh_system_backup({"create_ha_backup": False}, [])
                        else:
                            self.assertEqual(s.ensure_fresh_system_backup({"create_ha_backup": False}, []), "supported")

    def test_verified_non_system_inventory_is_missing(self):
        s = self.server()
        import backups
        for entries in ([], [backup_fixture(type="partial", content={"homeassistant": False})]):
            with self.subTest(entries=entries):
                s.backup_manager_info = Mock(return_value={"backups": entries})
                self.assertEqual(s.latest_system_backup_status({})["refusal_reason"], "missing")
                with self.assertRaises(backups.FreshBackupRequired):
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])

    def test_disabled_location_requirement_allows_missing_storage_but_not_malformed_metadata(self):
        s = self.server()
        import backups
        for location in ({}, {"locations": []}, {"locations": {}}, {"locations": 0}):
            with self.subTest(location=location):
                entry = {key: value for key, value in backup_fixture().items() if key != "locations"}
                s.backup_manager_info = Mock(return_value={"backups": [{**entry, **location}]})
                with self.assertRaises(backups.FreshBackupRequired):
                    s.ensure_fresh_system_backup({"create_ha_backup": False, "backup_require_location": False}, [])
        for location in ({"locations": [False]}, {"location": ""}):
            with self.subTest(location=location):
                s.backup_manager_info = Mock(return_value={"backups": [backup_fixture(**location)]})
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": False, "backup_require_location": False}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)

    def test_creation_recovers_from_missing_stale_or_unavailable_inventory(self):
        s = self.server()
        for initial in ({"backups": []}, {"backups": [backup_fixture()]}, {"backups": [{}]}, RuntimeError("offline")):
            for require_location in (True, False):
                with self.subTest(initial=initial, require_location=require_location):
                    fresh = backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat(), locations=[None])
                    s.backup_manager_info = Mock(side_effect=[initial, {"backups": [fresh]}])
                    s.default_backup_mount = Mock(return_value="nas")
                    s.create_ha_backup = Mock(return_value="created")
                    self.assertEqual(s.ensure_fresh_system_backup({"create_ha_backup": True, "backup_require_location": require_location}, []), "created")
                    s.create_ha_backup.assert_called_once_with("ha-ops", backup_location="nas" if require_location else None)

    def test_created_backup_metadata_uses_the_same_validation_without_granting_bypass(self):
        s = self.server()
        import backups
        for name, response in invalid_backup_inventories():
            entries = response.get("backups") if isinstance(response, dict) else None
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict) or entries[0].get("slug") != "candidate":
                continue
            with self.subTest(case=name):
                s.backup_manager_info = Mock(side_effect=[{"backups": []}, response])
                s.default_backup_mount = Mock(return_value="nas")
                s.create_ha_backup = Mock(return_value="candidate")
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": True}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)

    def test_entire_post_creation_inventory_uses_the_strict_classifier(self):
        s = self.server()
        import backups
        fresh = backup_fixture(slug="created", date=datetime.now(timezone.utc).isoformat(), locations=[None])
        cases = list(ambiguous_created_backup_inventories())
        for name, response in invalid_backup_inventories():
            entries = response.get("backups") if isinstance(response, dict) else None
            if isinstance(entries, list):
                cases.extend([(f"{name}, created first", {"backups": [fresh, *entries]}),
                              (f"{name}, created last", {"backups": [*entries, fresh]})])
            else:
                cases.append((name, response))
        for name, response in cases:
            with self.subTest(case=name):
                s.backup_manager_info = Mock(side_effect=[{"backups": []}, response])
                s.default_backup_mount = Mock(return_value="nas")
                s.create_ha_backup = Mock(return_value="created")
                details = []
                with self.assertRaisesRegex(RuntimeError, "inventory is invalid") as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": True}, details)
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
                self.assertNotIn("Created fresh system backup:", "\n".join(details))

    def test_initial_inventory_rejects_ambiguous_created_identities(self):
        s = self.server()
        for name, response in ambiguous_created_backup_inventories():
            with self.subTest(case=name):
                s.backup_manager_info = Mock(return_value=response)
                status = s.latest_system_backup_status({})
                self.assertFalse(status["available"])
                self.assertIsNone(status["refusal_reason"])

    def test_post_creation_selection_requires_the_returned_backup_to_be_eligible(self):
        s = self.server()
        import backups
        fresh = backup_fixture(slug="created", id="created", date=datetime.now(timezone.utc).isoformat(), locations=[None])
        other = {**fresh, "slug": "other", "id": "other-id"}
        cases = [
            ("fresh", fresh, None),
            ("stale", {**fresh, "date": backup_fixture()["date"]}, "older than"),
            ("non-system", {**fresh, "type": "partial", "content": {"homeassistant": False}}, "does not contain"),
            ("missing", None, "not visible"),
        ]
        for name, created, error in cases:
            for first in (True, False):
                with self.subTest(case=name, created_first=first):
                    entries = ([created, other] if first else [other, created]) if created else [other]
                    s.backup_manager_info = Mock(side_effect=[{"backups": []}, {"backups": entries}])
                    s.default_backup_mount = Mock(return_value="nas")
                    s.create_ha_backup = Mock(return_value="created")
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error) as caught:
                            s.ensure_fresh_system_backup({"create_ha_backup": True}, [])
                        self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
                    else:
                        self.assertEqual(s.ensure_fresh_system_backup({"create_ha_backup": True}, []), "created")

    def test_disabled_backup_policy_does_not_query_inventory(self):
        s = self.server()
        s.backup_manager_info = Mock(side_effect=AssertionError("unexpected backup query"))
        self.assertIsNone(s.ensure_fresh_system_backup({"require_fresh_backup": False}, []))
        s.backup_manager_info.assert_not_called()

    def test_only_completed_missing_or_stale_policy_refusal_is_typed(self):
        s = self.server()
        import backups
        for available in (True, False):
            for create in (False, True):
                with self.subTest(available=available, create=create):
                    s.latest_system_backup_status = lambda options: {"available": available, "refusal_reason": "missing", "stale": True, "max_age_hours": 24, "message": "missing"}
                    s.default_backup_mount = lambda: None
                    expected = backups.FreshBackupRequired if available and not create else RuntimeError
                    with self.assertRaises(expected) as caught:
                        s.ensure_fresh_system_backup({"create_ha_backup": create}, [])
                    self.assertEqual(isinstance(caught.exception, backups.FreshBackupRequired), available and not create)

    def test_actual_backup_status_classifies_missing_stale_location_and_unknown(self):
        from datetime import datetime, timedelta, timezone
        s = self.server()
        import backups
        now = datetime.now(timezone.utc)
        cases = [
            ({"backups": []}, True),
            ({"backups": [{"slug": "old", "type": "full", "date": (now - timedelta(days=2)).isoformat(), "locations": ["nas"]}]}, True),
            ({"backups": [{"slug": "bad", "type": "full", "date": now.isoformat(), "locations": []}]}, False),
            ({"backups": [{"slug": "bad", "type": "full", "date": "invalid", "locations": ["nas"]}]}, False),
            ({}, False),
            (RuntimeError("backup API unavailable"), False),
        ]
        for response, eligible in cases:
            with self.subTest(response=response):
                s.backup_manager_info = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])
                self.assertEqual(isinstance(caught.exception, backups.FreshBackupRequired), eligible)

    def test_creation_and_post_creation_failures_never_grant_bypass(self):
        s = self.server()
        import backups
        for stage in ("location", "create", "post_query", "not_visible", "no_date", "no_location"):
            with self.subTest(stage=stage):
                s.latest_system_backup_status = lambda options: {"available": True, "refusal_reason": "missing", "stale": True, "max_age_hours": 24, "message": "missing"}
                s.default_backup_mount = lambda: None if stage == "location" else "nas"
                s.create_ha_backup = Mock(side_effect=RuntimeError("create failed")) if stage == "create" else Mock(return_value="created")
                backup = {"slug": "created", "type": "full", "date": "2026-10-01T00:00:00Z", "locations": ["nas"]}
                if stage == "no_date": backup["date"] = None
                if stage == "no_location": backup["locations"] = []
                s.backup_manager_info = Mock(side_effect=RuntimeError("post query failed")) if stage == "post_query" else Mock(return_value={"backups": [] if stage == "not_visible" else [backup]})
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": True}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)

    def test_stale_valid_backup_does_not_hide_invalid_system_backup_metadata(self):
        from datetime import datetime, timedelta, timezone
        s = self.server()
        import backups
        now = datetime.now(timezone.utc)
        old = {"slug": "old", "type": "full", "date": (now - timedelta(days=2)).isoformat(), "locations": ["nas"]}
        for invalid in (
            {"slug": "unknown-age", "type": "full", "date": "invalid", "locations": ["nas"]},
            {"slug": "new-without-location", "type": "full", "date": now.isoformat(), "locations": []},
        ):
            with self.subTest(invalid=invalid):
                s.backup_manager_info = Mock(return_value={"backups": [old, invalid]})
                with self.assertRaises(RuntimeError) as caught:
                    s.ensure_fresh_system_backup({"create_ha_backup": False}, [])
                self.assertNotIsInstance(caught.exception, backups.FreshBackupRequired)
