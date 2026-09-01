import ast
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from types import MethodType
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "app" / "server.py"
I18N_PATH = ROOT / "app" / "i18n.py"
I18N_GUARD_PATHS = [
    ROOT / "app" / "app_context.py",
    ROOT / "app" / "conflicts.py",
    ROOT / "app" / "jobs.py",
    ROOT / "app" / "state.py",
    ROOT / "app" / "web.py",
]
I18N_APP_PATHS = sorted((ROOT / "app").glob("*.py"))


def load_server():
    sys.modules.pop("server", None)
    spec = importlib.util.spec_from_file_location("server", SERVER_PATH)
    server = importlib.util.module_from_spec(spec)
    sys.modules["server"] = server
    spec.loader.exec_module(server)
    return server


def load_i18n():
    sys.modules.pop("i18n", None)
    spec = importlib.util.spec_from_file_location("i18n", I18N_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n"] = module
    spec.loader.exec_module(module)
    return module


def load_dev_harness():
    server = load_server()
    sys.modules.pop("dev_harness", None)
    spec = importlib.util.spec_from_file_location("dev_harness", ROOT / "app" / "dev_harness.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_harness"] = module
    spec.loader.exec_module(module)
    return server, module


class ServerTests(unittest.TestCase):
    def select_all_save_preview_files(self, server):
        state = server.read_state()
        server.write_state({"save_preview_selected_paths": list(state.get("last_save_preview_paths") or [])})

    def select_all_apply_preview_files(self, server):
        state = server.read_state()
        server.write_state({"apply_preview_selected_paths": list(state.get("last_preview_paths") or [])})

    def assertEnglishTranslationText(self, text, context):
        offenders = []
        for index, char in enumerate(text):
            if ord(char) <= 0x7F or not unicodedata.category(char).startswith("L"):
                continue
            codepoint = f"U+{ord(char):04X}"
            name = unicodedata.name(char, "UNKNOWN")
            start = max(0, index - 24)
            end = min(len(text), index + 25)
            snippet = text[start:end].replace("\n", "\\n")
            offenders.append(f"{context}: {codepoint} {name} in {snippet!r}")
        if offenders:
            self.fail("Non-English alphabet text found:\n" + "\n".join(offenders))

    def test_english_translation_library_has_no_non_english_alphabet_text(self):
        i18n = load_i18n()
        self.assertEnglishTranslationText("ASCII punctuation, quotes, arrows ->, and Emoji 😀 stay allowed.", "guard sample")
        for key, value in i18n.EN_TEXT.items():
            self.assertEnglishTranslationText(value, key)

    def test_literal_translation_keys_exist_in_english_catalog(self):
        i18n = load_i18n()
        offenders = []
        for path in I18N_APP_PATHS:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                is_lookup = isinstance(func, ast.Name) and func.id == "_"
                is_i18n_lookup = (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"t", "error"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "i18n"
                )
                if not (is_lookup or is_i18n_lookup):
                    continue
                key_arg = node.args[0]
                if (
                    isinstance(key_arg, ast.Constant)
                    and isinstance(key_arg.value, str)
                    and key_arg.value not in i18n.EN_TEXT
                ):
                    offenders.append(f"{path.name}:{node.lineno}: {key_arg.value}")
        if offenders:
            self.fail("Translation lookup keys missing from English catalog:\n" + "\n".join(offenders))

    def test_user_facing_message_literals_use_translation_catalog(self):
        def is_catalog_or_exception_message(node):
            if isinstance(node, ast.Constant) and node.value == "":
                return True
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return False
            if isinstance(node, ast.JoinedStr):
                return False
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"_", "str"}:
                    return True
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "t"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "i18n"
                ):
                    return True
            if isinstance(node, ast.IfExp):
                return is_catalog_or_exception_message(node.body) and is_catalog_or_exception_message(node.orelse)
            return True

        def add_offender(path, source, node, label, offenders):
            segment = ast.get_source_segment(source, node) or type(node).__name__
            offenders.append(f"{path.name}:{node.lineno}: {label} uses {segment}")

        offenders = []
        for path in I18N_GUARD_PATHS:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "last_message"
                            and not is_catalog_or_exception_message(value)
                        ):
                            add_offender(path, source, value, "last_message", offenders)
                        if isinstance(key, ast.Constant) and key.value == "last_details":
                            if isinstance(value, ast.List):
                                for item in value.elts:
                                    if not is_catalog_or_exception_message(item):
                                        add_offender(path, source, item, "last_details list item", offenders)
                            elif not is_catalog_or_exception_message(value):
                                add_offender(path, source, value, "last_details", offenders)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send_json":
                    for arg in node.args[:1]:
                        if not isinstance(arg, ast.Dict):
                            continue
                        for key, value in zip(arg.keys, arg.values):
                            if (
                                isinstance(key, ast.Constant)
                                and key.value == "message"
                                and not is_catalog_or_exception_message(value)
                            ):
                                add_offender(path, source, value, "JSON message", offenders)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if (
                        node.func.attr == "append"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "details"
                        and node.args
                        and not is_catalog_or_exception_message(node.args[0])
                    ):
                        add_offender(path, source, node.args[0], "details.append", offenders)
                    if node.func.attr == "add_detail" and node.args:
                        message_arg = node.args[-1]
                        if not is_catalog_or_exception_message(message_arg):
                            add_offender(path, source, message_arg, "add_detail", offenders)
        if offenders:
            self.fail("User-facing messages bypass the translation catalog:\n" + "\n".join(offenders))

    def test_f013_runtime_and_preview_messages_use_translation_catalog(self):
        guarded = {
            ROOT / "app" / "app_context.py": {
                "ensure_preview_matches_state",
                "enforce_apply_limits",
            },
            ROOT / "app" / "jobs.py": {"run_deleted_devices_confirm_job"},
            ROOT / "app" / "registry_cleanup.py": {
                "build_deleted_devices_preview",
                "build_stale_mqtt_discovery_preview",
            },
            ROOT / "app" / "sync.py": {
                "build_apply_preview",
                "build_apply_preview_from_sources",
                "build_save_preview",
                "merge_status_lines",
                "save_preview_status_lines",
            },
        }

        def catalog_call(node):
            if not isinstance(node, ast.Call):
                return False
            if isinstance(node.func, ast.Name) and node.func.id == "_":
                return True
            return (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"error", "t", "user_message"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "i18n"
            )

        def user_text_literal(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return any(char.isalpha() for char in node.value)
            if isinstance(node, ast.JoinedStr):
                return any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(char.isalpha() for char in part.value)
                    for part in node.values
                )
            if isinstance(node, (ast.List, ast.Tuple)):
                return any(user_text_literal(item) for item in node.elts)
            if isinstance(node, ast.IfExp):
                return user_text_literal(node.body) or user_text_literal(node.orelse)
            return False

        def target_name(target):
            return target.id if isinstance(target, ast.Name) else None

        offenders = []
        for path, functions in guarded.items():
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in functions]:
                for node in ast.walk(function):
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                        if (
                            isinstance(node.exc.func, ast.Name)
                            and node.exc.func.id == "RuntimeError"
                            and node.exc.args
                            and user_text_literal(node.exc.args[0])
                        ):
                            offenders.append(f"{path.name}:{node.lineno}: RuntimeError text bypasses catalog")
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str":
                        if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "exc":
                            offenders.append(f"{path.name}:{node.lineno}: str(exc) used for guarded user-facing text")
                    if isinstance(node, ast.Assign):
                        names = {target_name(target) for target in node.targets}
                        if names.intersection({"summary", "diff_text", "lines"}) and user_text_literal(node.value):
                            offenders.append(f"{path.name}:{node.lineno}: preview text literal bypasses catalog")
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        receiver = node.func.value
                        if isinstance(receiver, ast.Name) and receiver.id in {"lines", "chunks"} and node.args:
                            if not catalog_call(node.args[0]) and user_text_literal(node.args[0]):
                                offenders.append(f"{path.name}:{node.lineno}: preview append bypasses catalog")

        if offenders:
            self.fail("F013 catalog guard found bypasses:\n" + "\n".join(offenders))

    def test_f013_catalog_backed_runtime_errors_render_from_library(self):
        server = load_server()
        i18n = server.app_context.i18n
        replacements = {
            "error.preview_commit_mismatch": "CATALOG: preview commit mismatch.",
            "error.apply_delete_limit": "CATALOG: delete limit {deletions}/{limit}.",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}
        try:
            i18n.EN_TEXT.update(replacements)
            with self.assertRaises(i18n.CatalogError) as commit_error:
                server._CTX.ensure_preview_matches_state(
                    {"last_preview_commit": "old"},
                    "new",
                    {"fingerprint": "same", "live_fingerprints": {}},
                )
            self.assertEqual(str(commit_error.exception), "CATALOG: preview commit mismatch.")

            with self.assertRaises(i18n.CatalogError) as limit_error:
                server._CTX.enforce_apply_limits({"max_apply_deletions": 0}, {"deletions": 2})
            self.assertEqual(str(limit_error.exception), "CATALOG: delete limit 2/0.")
        finally:
            i18n.EN_TEXT.update(originals)

    def test_f013_deleted_devices_confirm_failure_uses_catalog_message(self):
        server = load_server()
        i18n = server.app_context.job_logic.i18n
        key = "error.deleted_devices_cleanup_not_pending"
        original = i18n.EN_TEXT[key]
        i18n.EN_TEXT[key] = "CATALOG: no deleted_devices confirmation pending."
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self.configure_paths(server, Path(tmp))
                server.write_state({"deleted_devices_pending_confirmation": False})
                self.assertFalse(server.run_deleted_devices_confirm_job())
                state = server.read_state()
                self.assertEqual(state["last_message"], "CATALOG: no deleted_devices confirmation pending.")
                self.assertEqual(state["last_details"][-1], "CATALOG: no deleted_devices confirmation pending.")
                self.assertNotIn("No deleted_devices cleanup is pending confirmation.", state["last_details"])
        finally:
            i18n.EN_TEXT[key] = original

    def test_f014_job_and_sync_details_use_translation_catalog(self):
        guarded = {
            ROOT / "app" / "jobs.py": {
                "run_deleted_devices_delete_job",
                "run_internal_ids_migrate_job",
                "run_retained_devices_delete_job",
            },
            ROOT / "app" / "sync.py": {
                "add_save_export_candidate_details",
                "apply_homeassistant_config",
                "apply_targets",
            },
        }

        def user_text_literal(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return any(char.isalpha() for char in node.value)
            if isinstance(node, ast.JoinedStr):
                return any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(char.isalpha() for char in part.value)
                    for part in node.values
                )
            if isinstance(node, (ast.List, ast.Tuple)):
                return any(user_text_literal(item) for item in node.elts)
            if isinstance(node, ast.IfExp):
                return user_text_literal(node.body) or user_text_literal(node.orelse)
            return False

        offenders = []
        for path, functions in guarded.items():
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in functions]:
                for node in ast.walk(function):
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                        if (
                            isinstance(node.exc.func, ast.Name)
                            and node.exc.func.id == "RuntimeError"
                            and node.exc.args
                            and user_text_literal(node.exc.args[0])
                        ):
                            offenders.append(f"{path.name}:{node.lineno}: RuntimeError text bypasses catalog")
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if node.func.attr == "add_detail" and node.args and user_text_literal(node.args[-1]):
                            offenders.append(f"{path.name}:{node.lineno}: add_detail text bypasses catalog")
                        if (
                            node.func.attr == "append"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "details"
                            and node.args
                            and user_text_literal(node.args[0])
                        ):
                            offenders.append(f"{path.name}:{node.lineno}: details.append text bypasses catalog")

        if offenders:
            self.fail("F014 catalog guard found bypasses:\n" + "\n".join(offenders))

    def test_f014_precondition_failures_render_from_translation_catalog(self):
        server = load_server()
        i18n = server.app_context.job_logic.i18n
        replacements = {
            "error.deleted_devices_preview_changed": "CATALOG: deleted_devices preview changed.",
            "error.deleted_devices_preview_required": "CATALOG: deleted_devices preview required.",
            "error.internal_ids_preview_required": "CATALOG: internal IDs preview required.",
            "error.internal_ids_selection_required": "CATALOG: internal IDs selection required.",
            "error.retained_devices_no_topics": "CATALOG: retained devices have no topics.",
            "error.retained_devices_preview_changed": "CATALOG: retained devices preview changed.",
            "error.retained_devices_preview_required": "CATALOG: retained devices preview required.",
            "error.retained_devices_selection_required": "CATALOG: retained devices selection required.",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}

        class JobContext:
            def __init__(self, state):
                self.run_lock = threading.Lock()
                self.state = dict(state)
                self.updates = []

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def load_options(self):
                return {}

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.updates.append(updates)
                self.state.update(updates)

            def log(self, message):
                pass

            def build_deleted_devices_preview(self):
                return {"fingerprint": "fresh"}

            def device_registry_fingerprint(self):
                return "fresh"

        cases = [
            (
                "internal ids preview required",
                lambda ctx: server.app_context.job_logic.run_internal_ids_migrate_job(["0"], ctx),
                {},
                "CATALOG: internal IDs preview required.",
                "Run Check actions IDs before approving migration.",
            ),
            (
                "internal ids selection required",
                lambda ctx: server.app_context.job_logic.run_internal_ids_migrate_job([], ctx),
                {"last_internal_ids_rows": [{"path": "a.yaml", "changes": True}], "last_internal_ids_fingerprint": "fp"},
                "CATALOG: internal IDs selection required.",
                "Select at least one internal id migration file.",
            ),
            (
                "retained devices preview required",
                lambda ctx: server.app_context.job_logic.run_retained_devices_delete_job(["0"], ctx),
                {},
                "CATALOG: retained devices preview required.",
                "Run Check retained devices before approving deletion.",
            ),
            (
                "retained devices selection required",
                lambda ctx: server.app_context.job_logic.run_retained_devices_delete_job(
                    {
                        "candidate": [],
                        "retained_preview_fingerprint": ["fp"],
                        "retained_preview_generated_at": ["2026-06-15T12:00:00+00:00"],
                    },
                    ctx,
                ),
                {
                    "last_retained_devices_rows": [{"identity": "row", "retained_topics": ["homeassistant/sensor/stale/config"]}],
                    "last_retained_devices_fingerprint": "fp",
                    "last_retained_devices_generated_at": "2026-06-15T12:00:00+00:00",
                },
                "CATALOG: retained devices selection required.",
                "Select at least one retained device candidate to delete.",
            ),
            (
                "retained devices no topics",
                lambda ctx: server.app_context.job_logic.run_retained_devices_delete_job(
                    {
                        "candidate": ["row"],
                        "retained_preview_fingerprint": ["fp"],
                        "retained_preview_generated_at": ["2026-06-15T12:00:00+00:00"],
                    },
                    ctx,
                ),
                {
                    "last_retained_devices_rows": [{"identity": "row", "retained_topics": []}],
                    "last_retained_devices_fingerprint": "fp",
                    "last_retained_devices_generated_at": "2026-06-15T12:00:00+00:00",
                },
                "CATALOG: retained devices have no topics.",
                "Selected retained device candidates have no retained discovery topics.",
            ),
            (
                "deleted devices preview required",
                lambda ctx: server.app_context.job_logic.run_deleted_devices_delete_job(ctx),
                {},
                "CATALOG: deleted_devices preview required.",
                "Run Check deleted_devices before approving deletion.",
            ),
            (
                "deleted devices preview changed",
                lambda ctx: server.app_context.job_logic.run_deleted_devices_delete_job(ctx),
                {"last_deleted_devices_count": 1, "last_deleted_devices_fingerprint": "stale"},
                "CATALOG: deleted_devices preview changed.",
                "deleted devices changed since preview. Run Check deleted devices again.",
            ),
        ]

        try:
            i18n.EN_TEXT.update(replacements)
            for label, action, state, expected, forbidden in cases:
                with self.subTest(label=label):
                    ctx = JobContext(state)
                    self.assertFalse(action(ctx))
                    self.assertEqual(ctx.updates[-1]["last_message"], expected)
                    self.assertEqual(ctx.updates[-1]["last_details"][-1], expected)
                    self.assertNotIn(forbidden, ctx.updates[-1]["last_details"])

            for action, expected in (
                (
                    server.app_context.job_logic.run_deleted_devices_confirm_job,
                    "Confirming deleted entities cleanup.",
                ),
                (
                    server.app_context.job_logic.run_deleted_devices_revert_job,
                    "Reverting deleted entities cleanup.",
                ),
            ):
                with self.subTest(action=action.__name__):
                    ctx = JobContext(
                        {
                            "deleted_devices_pending_confirmation": True,
                            "deleted_devices_pending_entity_count": 1,
                        }
                    )
                    self.assertFalse(action(ctx))
                    self.assertEqual(ctx.updates[0]["last_message"], expected)
        finally:
            i18n.EN_TEXT.update(originals)

    def test_f017_stale_state_and_save_export_paths_use_translation_catalog(self):
        guarded = {
            ROOT / "app" / "jobs.py": {
                "run_deleted_devices_preview_job",
                "run_internal_ids_preview_job",
                "run_retained_devices_preview_job",
                "run_deleted_devices_revert_job",
            },
            ROOT / "app" / "sync.py": {
                "build_save_export",
                "apply_save_export",
                "restore_normalized_equal_save_files",
                "restore_normalized_equal_save_worktree",
                "save_unknown_base_conflicts",
            },
        }

        def user_text_literal(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return any(char.isalpha() for char in node.value)
            if isinstance(node, ast.JoinedStr):
                return any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(char.isalpha() for char in part.value)
                    for part in node.values
                )
            if isinstance(node, (ast.List, ast.Tuple)):
                return any(user_text_literal(item) for item in node.elts)
            if isinstance(node, ast.IfExp):
                return user_text_literal(node.body) or user_text_literal(node.orelse)
            return False

        offenders = []
        for path, functions in guarded.items():
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in functions]:
                for node in ast.walk(function):
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                        if (
                            isinstance(node.exc.func, ast.Name)
                            and node.exc.func.id == "RuntimeError"
                            and node.exc.args
                            and user_text_literal(node.exc.args[0])
                        ):
                            offenders.append(f"{path.name}:{node.lineno}: RuntimeError text bypasses catalog")
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if node.func.attr == "add_detail" and node.args and user_text_literal(node.args[-1]):
                            offenders.append(f"{path.name}:{node.lineno}: add_detail text bypasses catalog")
                        if (
                            node.func.attr == "append"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "details"
                            and node.args
                            and user_text_literal(node.args[0])
                        ):
                            offenders.append(f"{path.name}:{node.lineno}: details.append text bypasses catalog")

        if offenders:
            self.fail("F017 catalog guard found bypasses:\n" + "\n".join(offenders))

    def test_f017_deleted_devices_preconditions_render_from_translation_catalog(self):
        server = load_server()
        i18n = server.app_context.job_logic.i18n
        replacements = {
            "error.deleted_devices_cleanup_not_pending": "CATALOG: no cleanup pending.",
            "error.deleted_devices_pending_before_check": "CATALOG: pending before deleted_devices check.",
            "error.deleted_devices_pending_before_internal_ids": "CATALOG: pending before internal ids check.",
            "error.deleted_devices_pending_before_retained": "CATALOG: pending before retained check.",
            "error.deleted_devices_rollback_missing": "CATALOG: rollback missing.",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}

        class JobContext:
            def __init__(self, state):
                self.run_lock = threading.Lock()
                self.state = dict(state)
                self.updates = []

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def load_options(self):
                return {}

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.updates.append(updates)
                self.state.update(updates)

            def log(self, message):
                pass

        cases = [
            (
                "deleted_devices check blocked",
                server.app_context.job_logic.run_deleted_devices_preview_job,
                {"deleted_devices_pending_confirmation": True},
                "CATALOG: pending before deleted_devices check.",
                "Confirm or revert the pending deleted_devices cleanup before checking again.",
            ),
            (
                "internal ids check blocked",
                server.app_context.job_logic.run_internal_ids_preview_job,
                {"deleted_devices_pending_confirmation": True},
                "CATALOG: pending before internal ids check.",
                "Confirm or revert the pending deleted_devices cleanup before checking internal ids.",
            ),
            (
                "retained devices check blocked",
                server.app_context.job_logic.run_retained_devices_preview_job,
                {"deleted_devices_pending_confirmation": True},
                "CATALOG: pending before retained check.",
                "Confirm or revert the pending deleted_devices cleanup before checking retained devices.",
            ),
            (
                "revert not pending",
                server.app_context.job_logic.run_deleted_devices_revert_job,
                {"deleted_devices_pending_confirmation": False},
                "CATALOG: no cleanup pending.",
                "No deleted_devices cleanup is pending confirmation.",
            ),
            (
                "revert rollback missing",
                server.app_context.job_logic.run_deleted_devices_revert_job,
                {"deleted_devices_pending_confirmation": True},
                "CATALOG: rollback missing.",
                "Deleted devices rollback snapshot is missing.",
            ),
        ]

        try:
            i18n.EN_TEXT.update(replacements)
            for label, action, state, expected, forbidden in cases:
                with self.subTest(label=label):
                    ctx = JobContext(state)
                    self.assertFalse(action(ctx))
                    self.assertEqual(ctx.updates[-1]["last_message"], expected)
                    self.assertEqual(ctx.updates[-1]["last_details"][-1], expected)
                    self.assertNotIn(forbidden, ctx.updates[-1]["last_details"])
        finally:
            i18n.EN_TEXT.update(originals)

    def test_f017_save_export_details_render_from_translation_catalog(self):
        server = load_server()
        sync = server.sync_logic
        i18n = sync.i18n
        replacements = {
            "detail.exported_homeassistant_paths": "CATALOG: exported HA {count}.",
            "detail.exported_legacy_zigbee2mqtt_paths": "CATALOG: exported Z2M {count}.",
            "detail.exported_managed_storage_projection": "CATALOG: exported managed storage {count}.",
            "detail.exported_storage_allowlist": "CATALOG: exported storage {count}.",
            "detail.exporting_config_only": "CATALOG: exporting config {target} from {path}.",
            "detail.exporting_target": "CATALOG: exporting target {target} from {path}.",
            "detail.save_export_candidates": "CATALOG: candidates {target} {count}:",
            "detail.skipped_optional_target_missing": "CATALOG: skipped optional {target} at {path}.",
            "error.live_path_missing": "CATALOG: missing live path {target} at {path}.",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}

        class SyncContext:
            def __init__(self, root):
                self.work_dir = root / "work"
                self.work_dir.mkdir()

            def add_detail(self, details, message):
                details.append(message)

            def run_command(self, args, cwd=None):
                return subprocess.CompletedProcess(args, 0, "", "")

        original_export_homeassistant_config = sync.export_homeassistant_config
        original_export_target_to_path = sync.export_target_to_path
        original_organize_homeassistant_export = sync.organize_homeassistant_export

        def fake_export_homeassistant_config(src, dest, target, ctx):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "configuration.yaml").write_text("homeassistant:\n")
            return (1, 2, 3, 4)

        def fake_export_target_to_path(target, dest, ctx):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "config.yaml").write_text("addon: true\n")

        try:
            i18n.EN_TEXT.update(replacements)
            sync.export_homeassistant_config = fake_export_homeassistant_config
            sync.export_target_to_path = fake_export_target_to_path
            sync.organize_homeassistant_export = lambda path, target, details, ctx: None
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                live_ha = root / "live-ha"
                live_addon = root / "live-addon"
                live_ha.mkdir()
                live_addon.mkdir()
                ctx = SyncContext(root)
                details = []
                sync.build_save_export(
                    [
                        {
                            "id": "optional-addon",
                            "type": "addon",
                            "live_path": str(root / "missing-optional"),
                            "source_path": str(root / "git" / "optional-addon"),
                            "optional": True,
                        },
                        {
                            "id": "homeassistant",
                            "type": "homeassistant",
                            "live_path": str(live_ha),
                            "source_path": str(root / "git" / "homeassistant"),
                        },
                        {
                            "id": "addon",
                            "type": "addon",
                            "live_path": str(live_addon),
                            "source_path": str(root / "git" / "addon"),
                        },
                    ],
                    details,
                    ctx,
                )
                joined = "\n".join(details)
                self.assertIn("CATALOG: skipped optional optional-addon", joined)
                self.assertIn("CATALOG: exporting config homeassistant", joined)
                self.assertIn("CATALOG: exported HA 1.", joined)
                self.assertIn("CATALOG: exported Z2M 2.", joined)
                self.assertIn("CATALOG: exported storage 3.", joined)
                self.assertIn("CATALOG: exported managed storage 4.", joined)
                self.assertIn("CATALOG: exporting target addon", joined)
                self.assertIn("CATALOG: candidates homeassistant 1:", joined)
                self.assertIn("CATALOG: candidates addon 1:", joined)
                self.assertNotIn("Exporting config-only", joined)
                self.assertNotIn("Skipping optional target", joined)

                with self.assertRaises(i18n.CatalogError) as missing_error:
                    sync.build_save_export(
                        [
                            {
                                "id": "required-addon",
                                "type": "addon",
                                "live_path": str(root / "missing-required"),
                                "source_path": str(root / "git" / "required-addon"),
                            }
                        ],
                        [],
                        ctx,
                    )
                self.assertIn("CATALOG: missing live path required-addon", str(missing_error.exception))
                self.assertNotIn("Live path does not exist", str(missing_error.exception))
        finally:
            sync.export_homeassistant_config = original_export_homeassistant_config
            sync.export_target_to_path = original_export_target_to_path
            sync.organize_homeassistant_export = original_organize_homeassistant_export
            i18n.EN_TEXT.update(originals)

    def test_f020_preview_status_lines_render_from_translation_catalog(self):
        server = load_server()
        sync = server.sync_logic
        i18n = sync.i18n
        replacements = {
            "preview.change_added": "CATALOG_ADDED",
            "preview.change_copied": "CATALOG_COPIED",
            "preview.change_deleted": "CATALOG_DELETED",
            "preview.change_modified": "CATALOG_MODIFIED",
            "preview.change_renamed": "CATALOG_RENAMED",
            "preview.change_status_line": "- {label}: {path}",
            "preview.save_changes_title": "CATALOG save changes {count}:",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}

        class MergeStatusContext:
            def run_command(self, args, cwd=None):
                output = "\n".join(
                    [
                        "A\thomeassistant/added.yaml",
                        "D\thomeassistant/deleted.yaml",
                        "M\thomeassistant/modified.yaml",
                        "R100\thomeassistant/old.yaml\thomeassistant/renamed.yaml",
                        "C100\thomeassistant/source.yaml\thomeassistant/copied.yaml",
                    ]
                )
                return subprocess.CompletedProcess(args, 0, output, "")

        try:
            i18n.EN_TEXT.update(replacements)
            merge_lines, merge_paths = sync.merge_status_lines(Path("/repo"), MergeStatusContext())
            self.assertEqual(
                merge_lines,
                    [
                        "- CATALOG_ADDED: homeassistant/added.yaml",
                        "- CATALOG_DELETED: homeassistant/deleted.yaml",
                        "- CATALOG_MODIFIED: homeassistant/modified.yaml",
                        "- CATALOG_RENAMED: homeassistant/renamed.yaml",
                        "- CATALOG_COPIED: homeassistant/copied.yaml",
                ],
            )
            self.assertEqual(
                merge_paths,
                [
                    "homeassistant/added.yaml",
                    "homeassistant/copied.yaml",
                    "homeassistant/deleted.yaml",
                    "homeassistant/modified.yaml",
                    "homeassistant/renamed.yaml",
                ],
            )

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                preview = root / "preview"
                (repo / "homeassistant").mkdir(parents=True)
                (preview / "homeassistant").mkdir(parents=True)
                (repo / "homeassistant" / "deleted.yaml").write_text("old\n")
                (repo / "homeassistant" / "modified.yaml").write_text("git\n")
                (preview / "homeassistant" / "added.yaml").write_text("ha\n")
                (preview / "homeassistant" / "modified.yaml").write_text("ha\n")

                save_lines = sync.save_preview_status_lines(repo, preview)
                self.assertEqual(
                    save_lines,
                    [
                        "- CATALOG_ADDED: homeassistant/added.yaml",
                        "- CATALOG_DELETED: homeassistant/deleted.yaml",
                        "- CATALOG_MODIFIED: homeassistant/modified.yaml",
                    ],
                )

                self.configure_paths(server, root)
                server.get_installed_addons = lambda: []
                server.write_state(
                    {
                        "last_save_preview": "\n".join(
                            [i18n.t("preview.save_changes_title", count=len(save_lines)), *save_lines]
                        ),
                        "last_save_diff": "diff content",
                        "last_save_preview_paths": [
                            "homeassistant/added.yaml",
                            "homeassistant/deleted.yaml",
                            "homeassistant/modified.yaml",
                        ],
                    }
                )
                summary = server.read_state()["last_save_preview"]
                self.assertIn("CATALOG save changes 3:", summary)
                self.assertIn("- CATALOG_ADDED: homeassistant/added.yaml", summary)
                self.assertIn("- CATALOG_DELETED: homeassistant/deleted.yaml", summary)
                self.assertIn("- CATALOG_MODIFIED: homeassistant/modified.yaml", summary)
        finally:
            i18n.EN_TEXT.update(originals)

    def test_f013_retained_devices_preview_summary_uses_catalog(self):
        server = load_server()
        i18n = server.app_context.registry_cleanup.i18n
        key = "preview.retained_description"
        original = i18n.EN_TEXT[key]
        i18n.EN_TEXT[key] = "CATALOG: retained preview description."
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage = root / ".storage"
                storage.mkdir()
                (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
                preview = server.app_context.registry_cleanup.build_stale_mqtt_discovery_preview(root, retained_topics=[])
                self.assertIn("CATALOG: retained preview description.", preview["summary"])
        finally:
            i18n.EN_TEXT[key] = original

    def test_header_shows_version_next_to_title_without_footer_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))

            page = server.render_page()

            title_at = page.index("<h1>HA Ops</h1>")
            version_at = page.index(f'<div class="badge version" data-testid="version-badge">{server.addon_version()}</div>')
            status_at = page.index('data-testid="status-badge"')
            description_at = page.index("Git-backed config deployer")
            self.assertLess(title_at, status_at)
            self.assertLess(status_at, version_at)
            self.assertLess(version_at, description_at)
            self.assertNotIn(f"<footer>HA Ops {server.addon_version()}</footer>", page)

    def test_page_bootstraps_client_version_and_modal_text(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "1.2.3"\n')

            page = server.render_page()

            self.assertIn('window.__HA_OPS_BOOT_VERSION__ = "1.2.3";', page)
            self.assertIn('"Reload HA Ops"', page)
            self.assertIn('"Acknowledge Risks \\u0026 Continue"', page)
            self.assertIn('"New HA Ops Version Available"', page)
            self.assertIn("Correct client operation is not guaranteed", page)

    def test_run_save_job_status_message_comes_from_translation_catalog(self):
        server = load_server()

        class SaveContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.updates = []

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def load_options(self):
                return {}

            def read_state(self):
                return {"deleted_devices_pending_confirmation": True}

            def write_state(self, updates):
                self.updates.append(updates)

        i18n = server.app_context.job_logic.i18n
        original_preparing = i18n.EN_TEXT["message.preparing_save"]
        original_pending = i18n.EN_TEXT["message.pending_deleted_devices"]
        i18n.EN_TEXT["message.preparing_save"] = "CATALOG: preparing save."
        i18n.EN_TEXT["message.pending_deleted_devices"] = "CATALOG: pending deleted_devices cleanup."
        try:
            ctx = SaveContext()
            self.assertFalse(server.app_context.job_logic.run_save_job(ctx))
        finally:
            i18n.EN_TEXT["message.preparing_save"] = original_preparing
            i18n.EN_TEXT["message.pending_deleted_devices"] = original_pending

        self.assertEqual(ctx.updates[0]["last_message"], "CATALOG: preparing save.")
        self.assertEqual(ctx.updates[1]["last_message"], "CATALOG: pending deleted_devices cleanup.")

    def test_preparing_action_messages_include_button_context(self):
        server = load_server()
        i18n = server.app_context.job_logic.i18n

        self.assertEqual(i18n.EN_TEXT["message.preparing_save"], "Preparing HA to Git save.")
        self.assertEqual(
            i18n.EN_TEXT["message.preparing_save_preview"],
            "Checking HA changes after Git → HA; Git unchanged.",
        )
        self.assertEqual(i18n.EN_TEXT["message.preparing_apply"], "Preparing Git to HA apply.")
        self.assertEqual(i18n.EN_TEXT["message.preparing_apply_preview"], "Preparing Git to HA apply preview.")

    def test_job_detail_log_text_comes_from_translation_catalog(self):
        server = load_server()

        class DeletedDevicesPreviewContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.updates = {}
                self.writes = []

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def read_state(self):
                return {}

            def write_state(self, updates):
                self.writes.append(dict(updates))
                self.updates.update(updates)

            def add_detail(self, details, message):
                details.append(message)
                self.write_state({"last_details": details})

            def log(self, message):
                pass

            def build_deleted_devices_preview(self):
                return {
                    "summary": "No deleted_devices entries found.",
                    "rows": [],
                    "count": 0,
                    "fingerprint": "empty",
                }

        i18n = server.app_context.job_logic.i18n
        original_message = i18n.EN_TEXT["message.checking_deleted_devices"]
        i18n.EN_TEXT["message.checking_deleted_devices"] = "CATALOG: deleted_devices message sentinel."
        try:
            ctx = DeletedDevicesPreviewContext()
            self.assertTrue(server.app_context.job_logic.run_deleted_devices_preview_job(ctx))
        finally:
            i18n.EN_TEXT["message.checking_deleted_devices"] = original_message

        self.assertEqual(ctx.writes[0]["last_message"], "CATALOG: deleted_devices message sentinel.")
        self.assertEqual(ctx.writes[0]["last_details"], [])
        self.assertEqual(ctx.updates["last_message"], "Found 0 deleted devices.")
        self.assertEqual(ctx.updates["last_details"], [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(ctx.writes[0])

            page = server.render_page()

        self.assertIn("CATALOG: deleted_devices message sentinel.", page)
        self.assertNotIn("Checking Home Assistant deleted_devices.", page)

    def test_render_page_uses_external_reactive_module_without_inline_transport(self):
        server = load_server()
        page = server.render_page()
        self.assertIn('<ha-ops-app data-testid="ha-ops-app">', page)
        self.assertIn('<script type="module" src="assets/ha-ops.js"></script>', page)
        self.assertNotIn("function applyFragments", page)
        self.assertNotIn("data-ws-fragment", page)

    def test_log_scroll_sticks_to_bottom_unless_user_scrolls_up(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn('sessionStorage.getItem("haOpsLogScrollState")', script)
        self.assertIn("log.scrollHeight - log.scrollTop - log.clientHeight <= 4", script)
        self.assertIn("saved?.sticky === false", script)
        self.assertIn("log.scrollTop = log.scrollHeight", script)

    def test_log_wraps_long_lines(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            page = server.render_page()

        self.assertIn(
            ".details-card ha-ops-log {\n"
            "      flex: 1 1 auto;\n"
            "      min-height: 0;\n"
            "      display: block;",
            page,
        )
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("white-space: pre-wrap", script)

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

    def post_json(self, server, path, body=b""):
        return self.post_json_context(server.web, server.context(), path, body=body)

    def post_json_context(self, web_module, context, path, body=b""):
        handler = web_module.create_handler(context)
        request = handler.__new__(handler)
        request.path = path
        request.rfile = io.BytesIO(body)
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.headers["Accept"] = "application/json"
        request.headers["X-Requested-With"] = "fetch"
        if body:
            request.headers["Content-Length"] = str(len(body))
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)
        request.send_error = MethodType(lambda self, status, *args, **kwargs: self.responses.append(status), request)
        request.do_POST()
        return request

    def preview_identity_body(self, server, direction, values):
        payload = dict(values)
        payload["preview_identity"] = json.dumps(server.web.preview_identity_for_state(server.read_state(), direction))
        return urlencode(payload).encode()

    def get_json_context(self, web_module, context, path):
        handler = web_module.create_handler(context)
        request = handler.__new__(handler)
        request.path = path
        request.rfile = io.BytesIO()
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)
        request.send_error = MethodType(lambda self, status, *args, **kwargs: self.responses.append(status), request)
        request.do_GET()
        return request

    def wait_until(self, predicate, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

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

    def write_device_registry_file(self, path, devices=None, deleted_devices=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "minor_version": 12,
                    "key": "core.device_registry",
                    "data": {
                        "devices": devices or [],
                        "deleted_devices": deleted_devices or [],
                    },
                }
            )
        )

    def seed_deleted_devices_history_repo(self, server, root, source="homeassistant", manifest=None):
        repo = root / "data" / "ha-config"
        self.git(["init", str(repo)], root)
        server.OPTIONS_PATH.write_text(
            json.dumps(
                {
                    "repo_path": "ha-config",
                    "apply_path": "homeassistant",
                    **({"manifest_path": "ha-ops.json"} if manifest else {}),
                }
            )
        )
        if manifest:
            (repo / "ha-ops.json").write_text(json.dumps(manifest))
        registry = repo / source / ".storage" / "core.device_registry"
        self.write_device_registry_file(
            registry,
            devices=[
                {
                    "id": "deleted-1",
                    "name_by_user": "🛋️ living_room_xmas_train",
                    "manufacturer": "Tuya",
                    "model": "TS011F_plug",
                    "model_id": "TS011F_plug_3",
                    "identifiers": [["mqtt", "zigbee2mqtt_0x00124b0024abcdef"]],
                },
                {
                    "id": "former-live-id",
                    "name": "identifier fallback plug",
                    "manufacturer": "Tuya",
                    "model_id": "TS011F_plug_3",
                    "identifiers": [["mqtt", "zigbee2mqtt_0x00124b0024abcdee"]],
                },
            ],
        )
        self.git_commit_all(repo, "historical devices")
        self.write_device_registry_file(registry, devices=[], deleted_devices=[])
        self.git_commit_all(repo, "current devices")
        return repo

    def push_service_branches(self, seed):
        for branch in ("ha-ops/ha-live", "ha-ops/base"):
            exists = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=seed,
                text=True,
                capture_output=True,
            )
            if exists.returncode != 0:
                self.git(["branch", branch], seed)
        self.git(["push", "origin", "ha-ops/ha-live", "ha-ops/base"], seed)

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
        self.push_service_branches(seed)
        return remote

    def remote_file(self, remote, path):
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "show", f"main:{path}"],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout

    def prepare_empty_save_preview(self, server, root):
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
        self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
        self.select_all_save_preview_files(server)
        return remote

    def remote_main_subject(self, remote):
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "log", "-1", "--format=%s", "main"],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def write_heap_yaml_set(self, root, label):
        root.mkdir(parents=True, exist_ok=True)
        normalized = label.lower().replace(" ", "_")
        (root / "configuration.yaml").write_text(f"{normalized}:\n")
        (root / "automations.yaml").write_text(f"- id: {normalized}_auto\n  alias: {label} Auto\n")
        (root / "scripts.yaml").write_text(f"{normalized}_script:\n  sequence: []\n")
        (root / "scenes.yaml").write_text(f"- id: {normalized}_scene\n  name: {label} Scene\n  entities: {{}}\n")

    def write_stale_organizer_view(self, root):
        area = root / ".ha-ops" / "areas" / "home"
        area.mkdir(parents=True, exist_ok=True)
        (area / "automations.yaml").write_text("- id: stale_auto\n")
        (area / "scripts.yaml").write_text("stale_script:\n  sequence: []\n")
        (root / ".ha-ops" / "areas" / "organizer-index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "automations": {"count": 1, "ids": ["stale_auto"]},
                    "scripts": {"count": 1, "ids": ["stale_script"]},
                    "scenes": {"count": 0, "ids": []},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def remote_parents(self, remote, ref):
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-list", "--parents", "-n", "1", ref],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip().split()[1:]

    def remote_rev(self, remote, ref):
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", ref],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def repo_status(self, repo):
        return self.git(["status", "--porcelain"], repo).stdout.strip()

    def merge_head_exists(self, repo):
        path = self.git(["rev-parse", "--git-path", "MERGE_HEAD"], repo).stdout.strip()
        return (repo / path).exists()

    def seed_internal_ids_repo(self, server, root):
        repo = root / "data" / "ha-config"
        config = repo / "homeassistant"
        storage = config / ".storage"
        z2m = config / "zigbee2mqtt"
        storage.mkdir(parents=True)
        z2m.mkdir(parents=True)
        server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))
        (storage / "core.entity_registry").write_text(
            json.dumps(
                {
                    "data": {
                        "entities": [
                            {
                                "id": "11111111111111111111111111111111",
                                "entity_id": "switch.synthetic_switch",
                                "device_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            },
                            {
                                "id": "22222222222222222222222222222222",
                                "entity_id": "binary_sensor.synthetic_contact",
                                "device_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            },
                        ]
                    }
                }
            )
        )
        (storage / "core.device_registry").write_text(
            json.dumps(
                {
                    "data": {
                        "devices": [
                            {
                                "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                "identifiers": [["mqtt", "zigbee2mqtt_0x00124b00226b31f8"]],
                                "name": "old_registry_name",
                            },
                            {
                                "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                                "identifiers": [["mqtt", "zigbee2mqtt_0x00124b00226b31f9"]],
                                "name": "synthetic_contact",
                            },
                        ]
                    }
                }
            )
        )
        (z2m / "state.json").write_text(
            json.dumps(
                [
                    {
                        "ieee_address": "0x00124b00226b31f8",
                        "friendly_name": "synthetic_remote",
                    }
                ]
            )
        )
        return config

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
                    "last_preview_live_fingerprints": {"homeassistant": {"hash": "sha256:old"}},
                    "last_preview_storage_changes": True,
                    "last_preview_warnings": ["old warning"],
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
            self.assertEqual(state["last_preview_warnings"], [])
            self.assertEqual(state["last_deleted_devices_preview"], "")
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIsNone(state["last_deleted_devices_fingerprint"])
            self.assertIsNone(state["last_deleted_devices_generated_at"])
            self.assertEqual(state["last_preview_commit"], "abc")
            self.assertEqual(state["last_preview_fingerprint"], "fingerprint")
            self.assertEqual(state["last_preview_live_fingerprints"], {"homeassistant": {"hash": "sha256:old"}})
            self.assertTrue(state["last_preview_storage_changes"])

    def test_apply_preview_match_rejects_live_fingerprint_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            state = {
                "last_preview_commit": "abc",
                "last_preview_fingerprint": "diff",
                "last_preview_live_fingerprints": {"homeassistant": {"hash": "sha256:before"}},
            }
            preview = {
                "fingerprint": "diff",
                "live_fingerprints": {"homeassistant": {"hash": "sha256:after"}},
            }

            with self.assertRaisesRegex(RuntimeError, "automations/scripts/scenes changed"):
                server.ensure_preview_matches_state(state, "abc", preview)

    def test_preview_jobs_clear_stale_preview_state_when_started(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_diff": "old apply diff",
                    "last_diff_generated_at": "old",
                    "last_preview_fingerprint": "old",
                    "last_preview_live_fingerprints": {"homeassistant": {"hash": "sha256:old"}},
                    "last_preview_storage_changes": True,
                    "last_preview_warnings": ["old warning"],
                    "last_save_preview": "old save summary",
                    "last_save_diff": "old save diff",
                    "last_save_diff_generated_at": "old",
                    "last_deleted_devices_preview": "old deleted_devices",
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "old",
                    "last_deleted_devices_generated_at": "old",
                }
            )

            self.assertFalse(server.run_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_diff"], "")
            self.assertIsNone(state["last_diff_generated_at"])
            self.assertIsNone(state["last_preview_fingerprint"])
            self.assertEqual(state["last_preview_live_fingerprints"], {})
            self.assertFalse(state["last_preview_storage_changes"])
            self.assertEqual(state["last_preview_warnings"], [])

            server.write_state({"last_save_preview": "old", "last_save_diff": "old", "last_save_diff_generated_at": "old"})
            self.assertFalse(server.run_save_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_save_diff"], "")
            self.assertIsNone(state["last_save_diff_generated_at"])

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

    def test_initial_page_has_only_the_reactive_preview_dom_contract(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            page = server.render_page()

        app_markup = page.split('<ha-ops-app data-testid="ha-ops-app">', 1)[1].split("</ha-ops-app>", 1)[0]
        main_markup = page.split("<main>", 1)[1].split("</main>", 1)[0]
        reactive_script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        built_script = (ROOT / "app" / "static" / "ha-ops.js").read_text()
        self.assertNotIn("data-server-preview", page)
        self.assertNotIn("<ha-ops-preview", app_markup)
        self.assertNotIn('data-testid="diff-section"', app_markup)
        self.assertNotIn("preview-file-toggle", app_markup)
        self.assertNotIn("action='select-apply-preview'", app_markup)
        self.assertNotIn("action='select-save-preview'", app_markup)
        self.assertEqual(app_markup.count('data-testid="reactive-previews"'), 1)
        self.assertLess(main_markup.index('data-testid="reactive-previews"'), main_markup.index("<h2>Git Access</h2>"))
        self.assertNotIn('data-testid="reactive-previews"', reactive_script)
        self.assertNotIn("data-server-preview", reactive_script)
        self.assertNotIn("data-server-preview", built_script)

    def test_preview_initial_render_still_leaves_client_owning_diff_card(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_diff_generated_at": "2026-05-14T19:52:16+00:00",
                    "last_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_save_diff_generated_at": "2026-05-14T19:52:16+00:00",
                    "last_save_preview_paths": ["homeassistant/scripts.yaml"],
                }
            )

            page = server.render_page()

        app_markup = page.split('<ha-ops-app data-testid="ha-ops-app">', 1)[1].split("</ha-ops-app>", 1)[0]
        self.assertEqual(app_markup.count('data-testid="reactive-previews"'), 1)
        self.assertLess(app_markup.index('data-testid="reactive-previews"'), app_markup.index("<h2>Git Access</h2>"))
        self.assertNotIn("<ha-ops-preview", app_markup)
        self.assertNotIn('data-testid="diff-section"', app_markup)

    def test_reactive_diff_section_source_contract_covers_running_and_controls(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("const hasApplyPaths = Boolean(this.state.last_preview_paths?.length);", script)
        self.assertIn("const hasSavePaths = Boolean(this.state.last_save_preview_paths?.length);", script)
        self.assertIn("const previewRunning = this.isPreviewGenerationRunning();", script)
        self.assertIn("const hasDeletedPreview = Boolean(this.state.last_deleted_devices_generated_at);", script)
        self.assertIn("const hasRetainedPreview = Boolean(this.state.last_retained_devices_generated_at);", script)
        self.assertIn("const visible = hasApplyPaths || hasSavePaths || previewRunning || hasDeletedPreview || hasRetainedPreview || cleanupRunning;", script)
        self.assertIn("const loading = previewRunning && !hasApplyPaths && !hasSavePaths;", script)
        self.assertIn('data-testid="diff-section"', script)
        self.assertIn("TEXT.loadingPreviewDiff", script)
        self.assertIn("select_save_preview", script)
        self.assertIn("select_apply_preview", script)
        self.assertIn("resolve_save_preview", script)
        self.assertIn("resolve_apply_preview", script)
        self.assertIn('import "@vaadin/details";', script)
        self.assertIn("vaadin-details", script)
        self.assertIn("opened-changed", script)
        self.assertIn("diff-get", script)
        self.assertIn("stopKeyboardTogglePropagation", script)
        self.assertIn("dispatchChoice(choice)", script)
        self.assertNotIn("vaadin-radio-group", script)
        self.assertNotIn("@vaadin/radio-group", script)

    def test_reactive_diff_text_bootstrap_includes_preview_controls(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            page = server.render_page()
        self.assertIn("loadingPreviewDiff", page)
        self.assertIn("Loading Diff...", page)
        self.assertIn("includeFile", page)
        self.assertIn("Include preview file", page)
        self.assertIn("versionChoice", page)
        self.assertIn("Preview version choice", page)
        self.assertIn("useGitVersion", page)
        self.assertIn("Use Git Version", page)
        self.assertIn("useHaVersion", page)
        self.assertIn("Use HA Version", page)
        self.assertIn("approveDeletedDevices", page)
        self.assertIn("Approve Deletion", page)
        self.assertIn("deleteRetainedDevices", page)
        self.assertIn("Delete retained devices", page)
        self.assertIn("confirmDeletedDevicesDelete", page)
        self.assertIn("Stop Home Assistant Core and remove", page)
        self.assertIn("confirmRetainedDevicesDelete", page)
        self.assertIn("Clear selected MQTT retained discovery topics only", page)
        self.assertIn("retainedPreviewNotice", page)
        self.assertIn("These candidates come from stale retained Home Assistant MQTT discovery topics", page)
        self.assertIn("retainedDeleteNotice", page)
        self.assertIn("noDeletedDevices", page)
        self.assertIn("No deleted devices or entities found.", page)
        self.assertIn("noRetainedDevices", page)
        self.assertIn("No retained devices candidates found.", page)
        self.assertIn("deletedDevicesAndEntitiesLabel", page)
        self.assertIn("deleted devices and entities", page)

    def test_reactive_cleanup_preview_source_uses_text_catalog(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        for key in (
            "TEXT.confirmDeletedDevicesDelete",
            "TEXT.confirmRetainedDevicesDelete",
            "TEXT.approveDeletedDevices",
            "TEXT.deleteRetainedDevices",
            "TEXT.retainedPreviewNotice",
            "TEXT.retainedDeleteNotice",
            "TEXT.noDeletedDevices",
            "TEXT.noRetainedDevices",
            "TEXT.deletedDevicesAndEntitiesLabel",
            "TEXT.deletedEntitiesLabel",
            "TEXT.deletedDevicesLabel",
        ):
            self.assertIn(key, script)
        self.assertNotIn("Stop Home Assistant Core and remove {entries}?", script)
        self.assertNotIn("Clear selected MQTT retained discovery topics only? This does not delete files or registry/database records.", script)
        self.assertNotIn("No deleted devices or entities found.", script)
        self.assertNotIn("No retained devices candidates found.", script)

    def test_reactive_diff_highlighter_pairs_changed_line_substrings(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("function changedRanges(oldText, newText)", script)
        self.assertIn("renderChangedText(line.slice(1), changedRange)", script)
        self.assertIn('class="diff-changed"', script)
        self.assertIn("while (blockIndex < lines.length && lines[blockIndex].startsWith(\"-\")", script)
        self.assertIn("while (blockIndex < lines.length && lines[blockIndex].startsWith(\"+\")", script)
        self.assertNotIn("unsafeHTML", script)

    def test_running_job_disables_save_conflict_actions(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_status": "running",
                    "conflict_type": "save_unknown_base",
                    "conflicts": ["homeassistant/configuration.yaml"],
                }
            )

            server.context().run_lock.acquire()
            try:
                page = server.render_page()
            finally:
                server.context().run_lock.release()

            self.assertIn("<button type='submit' disabled>Approve HA to Git</button>", page)
            self.assertIn("<button type='submit' class='secondary' disabled>Use HA Version</button>", page)
            self.assertIn("<button type='submit' class='secondary' disabled>Use Git Version</button>", page)

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
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server.write_state(
                {
                    "last_status": "running",
                    "last_action": "deleted_devices_delete",
                    "last_message": "Deleting deleted_devices.",
                    "last_deleted_devices_rows": [{"id": "deleted-1", "recovered_name": "stale recovered"}],
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback_path),
                    "deleted_devices_rollback_fingerprint": "before",
                    "deleted_devices_applied_fingerprint": None,
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(events, ["stop", "start"])
            self.assertEqual(state["last_status"], "interrupted")
            self.assertEqual(state["last_message"], "Interrupted deleted devices cleanup was reverted on startup.")
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertFalse(rollback_path.exists())
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertNotIn("recovered_name", state["last_deleted_devices_rows"][0])

    def test_startup_recovery_does_not_restore_when_core_stop_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {"data": {"devices": [], "deleted_devices": [{"id": "deleted-1"}]}}
            registry_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            rollback_path = server.WORK_DIR / "deleted-devices-rollback" / "core.device_registry"
            rollback_path.parent.mkdir(parents=True)
            rollback_path.write_text(json.dumps(original))
            events = []

            def fail_stop():
                events.append("stop")
                raise RuntimeError("stop failed")

            server.core_stop = fail_stop
            server.core_start = lambda: events.append("start")
            server.write_state(
                {
                    "last_status": "running",
                    "last_action": "deleted_devices_delete",
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback_path),
                    "deleted_devices_recovery_phase": "restore_required",
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(events, ["stop"])
            self.assertEqual(json.loads(registry_path.read_text())["data"]["deleted_devices"], [])
            self.assertEqual(state["deleted_devices_recovery_phase"], "manual_recovery")
            self.assertTrue(Path(state["deleted_devices_rollback_path"]).exists())

    def test_manual_recovery_rejects_direct_mutating_job_and_releases_reserved_lock(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "deleted_devices_recovery_phase": "manual_recovery",
                    "deleted_devices_rollback_path": str(root / "rollback"),
                    "last_message": "Manual recovery is required.",
                }
            )

            self.assertTrue(server.RUN_LOCK.acquire(blocking=False))
            self.assertFalse(server.run_apply_job(lock_acquired=True))
            self.assertTrue(server.RUN_LOCK.acquire(blocking=False))
            server.RUN_LOCK.release()

    def test_manual_recovery_http_blocks_mutations_before_job_dispatch(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.calls = []
                self.state = {
                    "deleted_devices_recovery_phase": "manual_recovery",
                    "deleted_devices_rollback_path": "/tmp/rollback",
                    "last_message": "Manual recovery is required.",
                }

            def read_state(self):
                return dict(self.state)

            def run_apply_job(self, lock_acquired=False):
                self.calls.append("apply")

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                self.calls.append("save")

            def run_deleted_devices_confirm_job(self, lock_acquired=False):
                self.calls.append("confirm")

            def run_rollback_job(self, release, lock_acquired=False):
                self.calls.append("rollback")

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)

        for path, body in (("/apply", b""), ("/save", b""), ("/deleted-devices-confirm", b""), ("/rollback", b"release=x")):
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: None, request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            self.assertEqual(request.responses[-1], 409)
            self.assertFalse(json.loads(request.wfile.getvalue().decode())["ok"])

        self.assertEqual(ctx.calls, [])

    def test_startup_recovers_delete_crash_after_core_start(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {"data": {"devices": [], "deleted_devices": [{"id": "deleted-1"}]}}
            registry_path.write_text(json.dumps(original))
            server.core_stop = lambda: None

            def crash_after_start():
                raise BaseException("simulated power loss")

            server.core_start = crash_after_start
            self.assertTrue(server.run_deleted_devices_preview_job())
            with self.assertRaises(BaseException):
                server.run_deleted_devices_delete_job()
            self.assertEqual(server.read_state()["deleted_devices_recovery_phase"], "restore_required")

            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server._CTX.repair_startup_state()

            self.assertEqual(events, ["stop", "start"])
            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(server.read_state()["deleted_devices_recovery_phase"], "none")

    def test_startup_recovers_revert_crash_after_core_start(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            original = {"data": {"devices": [], "deleted_devices": [{"id": "deleted-1"}]}}
            registry_path.write_text(json.dumps(original))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())

            def crash_after_start():
                raise BaseException("simulated power loss")

            server.core_stop = lambda: None
            server.core_start = crash_after_start
            with self.assertRaises(BaseException):
                server.run_deleted_devices_revert_job()
            self.assertEqual(server.read_state()["deleted_devices_recovery_phase"], "restore_required")

            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server._CTX.repair_startup_state()

            self.assertEqual(events, ["stop", "start"])
            self.assertEqual(json.loads(registry_path.read_text()), original)
            self.assertEqual(server.read_state()["deleted_devices_recovery_phase"], "none")

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
                    "last_save_commit_subject": "Old custom subject",
                    "last_internal_ids_preview": "old internal ids preview",
                    "last_internal_ids_rows": [{"index": 0, "path": ".ha-ops/areas/synthetic/automations.yaml"}],
                    "last_internal_ids_count": 1,
                    "last_preview_fingerprint": "keep",
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_status"], "success")
            self.assertEqual(state["last_details"], [])
            self.assertEqual(state["last_diff"], "")
            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_internal_ids_preview"], "")
            self.assertEqual(state["last_internal_ids_rows"], [])
            self.assertEqual(state["last_internal_ids_count"], 0)
            self.assertEqual(state["last_preview_fingerprint"], "keep")

    def test_startup_clears_stale_status_after_addon_version_change(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.6.21"\n')
            server.write_state(
                {
                    "last_seen_addon_version": "0.6.20",
                    "last_status": "error",
                    "last_action": "apply_preview",
                    "last_message": "automation count mismatch: expected 159, got 158",
                    "last_details": ["automation count mismatch: expected 159, got 158"],
                    "last_diff": "old diff",
                    "last_preview_commit": "abc",
                    "last_preview_fingerprint": "old",
                    "last_preview_live_fingerprints": {"homeassistant": {"hash": "sha256:old"}},
                    "last_deleted_devices_rows": [{"id": "deleted-1", "recovered_name": "stale recovered"}],
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "old-deleted",
                    "last_deleted_devices_generated_at": "2026-05-22T12:00:00+00:00",
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_seen_addon_version"], "0.6.21")
            self.assertEqual(state["last_status"], "idle")
            self.assertIsNone(state["last_action"])
            self.assertIn("HA Ops updated to 0.6.21", state["last_message"])
            self.assertEqual(state["last_details"], [])
            self.assertEqual(state["last_diff"], "")
            self.assertIsNone(state["last_preview_commit"])
            self.assertIsNone(state["last_preview_fingerprint"])
            self.assertEqual(state["last_preview_live_fingerprints"], {})
            self.assertEqual(state["last_deleted_devices_rows"], [])
            self.assertEqual(state["last_deleted_devices_count"], 0)
            self.assertIsNone(state["last_deleted_devices_fingerprint"])

    def test_startup_clears_internal_ids_preview_after_addon_version_change(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.7.3"\n')
            server.write_state(
                {
                    "last_seen_addon_version": "0.7.2",
                    "last_status": "success",
                    "last_action": "internal_ids_preview",
                    "last_message": "Internal id migration preview found 1 file.",
                    "last_internal_ids_generated_at": "2026-05-22T12:00:00+00:00",
                    "last_internal_ids_preview": "old diff",
                    "last_internal_ids_count": 1,
                    "last_internal_ids_fingerprint": "old",
                    "last_internal_ids_rows": [
                        {
                            "index": 0,
                            "path": ".ha-ops/areas/synthetic/automations.yaml",
                            "selected": True,
                            "diff": "old diff",
                        }
                    ],
                    "last_internal_ids_unresolved": [{"path": "old"}],
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_internal_ids_preview"], "")
            self.assertEqual(state["last_internal_ids_rows"], [])
            self.assertEqual(state["last_internal_ids_count"], 0)
            self.assertIsNone(state["last_internal_ids_fingerprint"])
            self.assertIsNone(state["last_internal_ids_generated_at"])
            self.assertEqual(state["last_internal_ids_unresolved"], [])
            self.assertNotIn("Internal IDs Migration Preview", page)

    def test_startup_keeps_error_when_addon_version_is_unchanged(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.6.21"\n')
            server.write_state(
                {
                    "last_seen_addon_version": "0.6.21",
                    "last_status": "error",
                    "last_action": "apply_preview",
                    "last_message": "automation count mismatch: expected 159, got 158",
                    "last_details": ["automation count mismatch: expected 159, got 158"],
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_status"], "error")
            self.assertEqual(state["last_action"], "apply_preview")
            self.assertEqual(state["last_message"], "automation count mismatch: expected 159, got 158")
            self.assertEqual(state["last_details"], [])

    def test_startup_keeps_pending_deleted_devices_on_addon_version_change(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.6.21"\n')
            server.write_state(
                {
                    "last_seen_addon_version": "0.6.20",
                    "last_status": "pending",
                    "last_action": "deleted_devices_delete",
                    "last_message": "Deleted 1 deleted_devices entry. Confirm or revert the changes.",
                    "last_deleted_devices_preview": "old preview",
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "fingerprint",
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": "/tmp/rollback",
                    "deleted_devices_rollback_fingerprint": "before",
                    "deleted_devices_applied_fingerprint": "after",
                    "last_preview_commit": "apply-commit",
                    "last_preview_fingerprint": "apply-fingerprint",
                    "last_preview_live_fingerprints": {"homeassistant/configuration.yaml": "live"},
                    "last_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_preview_conflicts": True,
                    "apply_preview_resolutions": {"homeassistant/configuration.yaml": "git"},
                    "last_save_preview_commit": "save-commit",
                    "last_save_preview_fingerprint": "save-fingerprint",
                    "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_save_preview_conflicts": True,
                    "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertEqual(state["last_seen_addon_version"], "0.6.21")
            self.assertEqual(state["last_status"], "pending")
            self.assertEqual(state["last_action"], "deleted_devices_delete")
            self.assertEqual(state["last_message"], "Deleted 1 deleted_devices entry. Confirm or revert the changes.")
            self.assertEqual(state["last_deleted_devices_preview"], "old preview")
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertEqual(state["deleted_devices_rollback_path"], "/tmp/rollback")
            self.assertIsNone(state["last_preview_commit"])
            self.assertIsNone(state["last_preview_fingerprint"])
            self.assertEqual(state["last_preview_live_fingerprints"], {})
            self.assertEqual(state["last_preview_paths"], [])
            self.assertFalse(state["last_preview_conflicts"])
            self.assertEqual(state["apply_preview_resolutions"], {})
            self.assertIsNone(state["last_save_preview_commit"])
            self.assertIsNone(state["last_save_preview_fingerprint"])
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertFalse(state["last_save_preview_conflicts"])
            self.assertIsNone(state["last_save_commit_subject"])
            self.assertEqual(state["save_preview_resolutions"], {})

            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            rollback_path = server.WORK_DIR / "deleted-devices-rollback" / "core.device_registry"
            rollback_path.parent.mkdir(parents=True)
            rollback_path.write_text(
                json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "deleted-1", "name": "Old Button"}]}})
            )
            server.write_state({"deleted_devices_rollback_path": str(rollback_path)})
            self.assertTrue(server.run_deleted_devices_confirm_job())
            state = server.read_state()
            self.assertEqual(state["apply_preview_resolutions"], {})
            self.assertEqual(state["save_preview_resolutions"], {})

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
            self.assertIn('<div class="badge conflicts" data-status-code="conflicts" data-testid="status-badge">conflicts</div>', page)
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

    def test_post_apply_save_notice_survives_refresh_until_save_preview(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_status": "success",
                    "last_action": "apply",
                    "last_message": "Apply finished successfully.",
                    "post_apply_save_recommended": True,
                }
            )

            page = server.render_page()

            self.assertIn("Post-apply HA changes may need saving.", page)
            self.assertIn('class="warning" >Review Post-Apply HA Changes</button>', page)
            self.assertIn("This is still HA to Git preview.", page)

            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertTrue(state["post_apply_save_recommended"])
            self.assertIn("Post-apply HA changes may need saving.", page)
            self.assertIn('class="warning" >Review Post-Apply HA Changes</button>', page)
            self.assertIn("This is still HA to Git preview.", page)

    def test_post_apply_save_notice_clears_on_version_update(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.6.21"\n')
            server.write_state(
                {
                    "last_seen_addon_version": "0.6.20",
                    "post_apply_save_recommended": True,
                }
            )

            server._CTX.repair_startup_state()
            state = server.read_state()

            self.assertFalse(state["post_apply_save_recommended"])

    def test_refresh_clears_internal_ids_preview(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_internal_ids_generated_at": "2026-05-22T12:00:00+00:00",
                    "last_internal_ids_preview": "old diff",
                    "last_internal_ids_count": 1,
                    "last_internal_ids_rows": [{"index": 0, "path": ".ha-ops/areas/synthetic/automations.yaml"}],
                }
            )

            self.assertIn("Internal IDs Migration Preview", server.render_page())
            server.clear_display_state()
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_internal_ids_preview"], "")
            self.assertEqual(state["last_internal_ids_rows"], [])
            self.assertEqual(state["last_internal_ids_count"], 0)
            self.assertNotIn("Internal IDs Migration Preview", page)

    def test_success_status_is_displayed_as_done(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"last_status": "success", "last_message": "Preview finished successfully."})

            page = server.render_page()

            self.assertIn('<div class="badge " data-status-code="success" data-testid="status-badge">done</div>', page)
            self.assertNotIn('<div class="badge ">success</div>', page)

    def test_stale_running_status_is_repaired_when_lock_is_free(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state({"last_status": "running", "last_message": "Preparing HA to Git save preview."})

            page = server.render_page()
            state = server.read_state()

            self.assertEqual(state["last_status"], "interrupted")
            self.assertIn('<div class="badge interrupted" data-status-code="interrupted" data-testid="status-badge">interrupted</div>', page)
            self.assertIn('action="preview"', page)
            self.assertIn('<button type="submit" class="secondary" >Preview Git to HA</button>', page)

    def test_status_badge_labels_come_from_translation_catalog(self):
        server = load_server()
        i18n = server.web.i18n
        replacements = {
            "status.running": "CATALOG: running sentinel",
            "status.conflicts": "CATALOG: conflicts sentinel",
            "status.pending_decision": "CATALOG: pending decision sentinel",
        }
        originals = {key: i18n.EN_TEXT[key] for key in replacements}
        try:
            i18n.EN_TEXT.update(replacements)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.configure_paths(server, root)
                server.get_installed_addons = lambda: []

                server.write_state({"last_status": "running"})
                server.context().run_lock.acquire()
                try:
                    running_page = server.render_page()
                    self.assertIn(
                        '<div class="badge running" data-status-code="running" data-testid="status-badge">CATALOG: running sentinel</div>',
                        running_page,
                    )
                finally:
                    server.context().run_lock.release()
                self.assertNotIn(">running</div>", running_page)

                server.write_state(
                    {
                        "last_status": "idle",
                        "conflicts": ["homeassistant/configuration.yaml"],
                        "conflict_type": "save_unknown_base",
                    }
                )
                conflicts_page = server.render_page()
                self.assertIn(
                    '<div class="badge conflicts" data-status-code="conflicts" data-testid="status-badge">CATALOG: conflicts sentinel</div>',
                    conflicts_page,
                )
                self.assertNotIn(">conflicts</div>", conflicts_page)

                rollback_path = root / "work" / "deleted-devices-rollback" / "core.device_registry"
                rollback_path.parent.mkdir(parents=True)
                rollback_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
                server.write_state(
                    {
                        "conflicts": [],
                        "deleted_devices_pending_confirmation": True,
                        "deleted_devices_rollback_path": str(rollback_path),
                    }
                )
                pending_page = server.render_page()
                self.assertIn(
                    '<div class="badge pending" data-status-code="pending decision" data-testid="status-badge">CATALOG: pending decision sentinel</div>',
                    pending_page,
                )
                self.assertNotIn(">pending decision</div>", pending_page)
        finally:
            i18n.EN_TEXT.update(originals)

    def test_async_actions_do_not_clear_persisted_state_before_submit(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        submit_start = script.index("async dispatchMutation(form)")
        submit_end = script.index("connect()", submit_start)
        submit_block = script[submit_start:submit_end]
        self.assertNotIn("clear-display-state", submit_block)
        self.assertIn("command_id: uuid()", submit_block)

    def test_running_page_uses_websocket_replay_until_job_finishes(self):
        server = load_server()
        page = server.render_page()
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("<ha-ops-app", page)
        self.assertIn('command: "replay"', script)
        command_flow = script[script.index("async dispatchMutation(form)"):script.index("observeBackendVersion(version)")]
        self.assertNotIn("window.location.reload", command_flow)

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

    def test_diff_unicode_escape_hover_shows_character(self):
        server = load_server()
        table_setting = chr(0x1F37D)

        content = server.ui.render_conflicts(
            [
                {
                    "path": "homeassistant/.ha-ops/areas/dining_room/automations.yaml",
                    "detail": "\n".join(
                        [
                            "--- Git",
                            "+++ HA",
                            "@@ -1 +1 @@",
                            f"-title: {table_setting} {{{{ now().strftime('%H:%M') }}}} Dining Room",
                            r'+title: "\U0001F37D {{ now().strftime(\'%H:%M\') }} Dining Room"',
                        ]
                    ),
                }
            ]
        )

        self.assertIn("unicode-escape", content)
        self.assertIn(r"\U0001F37D", content)
        self.assertIn(f"title='{table_setting}'", content)
        self.assertIn(f"data-unicode-char='{table_setting}'", content)

    def test_diff_unicode_escape_hover_keeps_full_code_when_changed_range_splits_it(self):
        server = load_server()
        desktop = chr(0x1F5A5)

        content = server.ui.render_conflicts(
            [
                {
                    "path": "homeassistant/.ha-ops/areas/office/automations.yaml",
                    "detail": "\n".join(
                        [
                            "--- Git",
                            "+++ HA",
                            "@@ -1 +1 @@",
                            f"-  topic: z2m/{desktop} office_7_buttons",
                            r'+  topic: "z2m/\U0001F5A5 office_7_buttons"',
                        ]
                    ),
                }
            ]
        )

        self.assertIn(r"\U0001F5A5</span>", content)
        self.assertNotIn(r"\U0001F5A</span>5", content)
        self.assertIn(f"data-unicode-char='{desktop}'", content)

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

    def test_save_preview_diff_hides_registry_noise_but_keeps_real_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
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
                            "name": "Zigbee2MQTT Bridge",
                            "modified_at": "git-modified-at",
                            "sw_version": "2.10.1",
                        }
                    ]
                }
            }
            preview_registry = {
                "data": {
                    "devices": [
                        {
                            "id": "device-1",
                            "name": "Zigbee2MQTT Bridge",
                            "modified_at": "live-modified-at",
                            "sw_version": "2.10.2",
                        }
                    ]
                }
            }
            (repo_storage / "core.device_registry").write_text(json.dumps(repo_registry))
            (preview_storage / "core.device_registry").write_text(json.dumps(preview_registry))

            diff = server.sync_logic.save_preview_diff_normalized(
                repo,
                preview,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                server.app_context.AppContext(
                    data_dir=server.DATA_DIR,
                    config_dir=server.CONFIG_DIR,
                    addon_configs_dir=server.ADDON_CONFIGS_DIR,
                ).sync_deps(),
            )

            self.assertNotIn("sw_version", diff)
            self.assertNotIn("2.10.1", diff)
            self.assertNotIn("2.10.2", diff)
            self.assertNotIn("modified_at", diff)
            self.assertNotIn("git-modified-at", diff)
            self.assertNotIn("live-modified-at", diff)

    def test_save_preview_include_redundant_data_shows_registry_noise(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo_storage = repo / "homeassistant" / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            repo_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (repo_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}})
            )
            (live_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "1"}]}})
            )
            self.git(["init", str(repo)], root)
            self.git(["checkout", "-b", "main"], repo)
            self.git_commit_all(repo, "base")
            self.git(["branch", "ha-ops/ha-live"], repo)
            self.git(["branch", "ha-ops/base"], repo)
            details = []

            preview = server.sync_logic.build_save_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(repo / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                        "delete": False,
                    }
                ],
                repo,
                details,
                server.app_context.AppContext(
                    data_dir=server.DATA_DIR,
                    config_dir=server.CONFIG_DIR,
                    addon_configs_dir=server.ADDON_CONFIGS_DIR,
                ).sync_deps(),
                include_redundant_data=True,
            )

            self.assertIn("- Modified: homeassistant/.storage/core.device_registry", preview["summary"])
            self.assertIn("modified_at", preview["diff"])
            self.assertIn("git-modified-at", preview["diff"])
            self.assertIn("live-modified-at", preview["diff"])

    def test_save_preview_diff_creates_roots_for_ha_only_additions(self):
        server = load_server()

        class StrictDirectoryDiffContext(server.app_context.AppContext):
            def run_command(self, command, env=None, cwd=None, timeout=None):
                if command[:4] == ["diff", "-ruN", "-x", ".git"]:
                    before = Path(command[4])
                    after = Path(command[5])
                    if not before.is_dir() or not after.is_dir():
                        missing = before if not before.exists() else after
                        return subprocess.CompletedProcess(
                            command,
                            2,
                            stdout="",
                            stderr=f"diff: {missing}: Is a directory",
                        )
                return super().run_command(command, env=env, cwd=cwd, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            self.git(["init", str(repo)], root)
            self.git(["checkout", "-b", "main"], repo)
            (repo / "README.md").write_text("base\n")
            self.git_commit_all(repo, "base")
            self.git(["checkout", "-b", "ha-ops/ha-live"], repo)
            live_path = repo / "homeassistant" / "configuration.yaml"
            live_path.parent.mkdir(parents=True)
            live_path.write_text("homeassistant:\n")
            self.git_commit_all(repo, "live addition")
            self.git(["checkout", "main"], repo)
            self.git(["merge", "--no-commit", "--no-ff", "ha-ops/ha-live"], repo)

            ctx = StrictDirectoryDiffContext(
                data_dir=server.DATA_DIR,
                config_dir=server.CONFIG_DIR,
                addon_configs_dir=server.ADDON_CONFIGS_DIR,
            )
            preview = server.sync_logic.merge_preview_for_save(
                repo,
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(repo / "homeassistant"),
                    }
                ],
                False,
                ctx,
            )

            self.assertEqual(preview["paths"], ["homeassistant/configuration.yaml"])
            self.assertIn("homeassistant/configuration.yaml", preview["diff"])

    def test_initial_save_preview_offers_git_only_editor_settings(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            self.git(["init", str(repo)], root)
            self.git(["checkout", "-b", "main"], repo)
            (repo / "README.md").write_text("base\n")
            self.git_commit_all(repo, "base")
            server.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(repo / "homeassistant"),
                "live_path": str(server.CONFIG_DIR),
                "delete": False,
            }
            ctx = server.app_context.AppContext(
                data_dir=server.DATA_DIR,
                config_dir=server.CONFIG_DIR,
                addon_configs_dir=server.ADDON_CONFIGS_DIR,
            ).sync_deps()

            preview = server.sync_logic.build_save_preview([target], repo, [], ctx)

            self.assertIn(".editorconfig", preview["paths"])
            self.assertIn(".vscode/settings.json", preview["paths"])
            self.assertIn(".prettierignore", preview["paths"])
            self.assertTrue(preview["warnings"])
            self.assertIn("never applied to live Home Assistant", preview["warnings"][0])
            self.assertFalse((server.CONFIG_DIR / ".editorconfig").exists())
            self.assertFalse((server.CONFIG_DIR / ".vscode/settings.json").exists())
            self.assertFalse((server.CONFIG_DIR / ".prettierignore").exists())
            self.assertTrue(server.sync_logic.save_merge_path_is_managed(repo, [target], ".editorconfig", ctx))
            self.assertFalse(server.sync_logic.apply_merge_path_is_managed(repo, [target], ".editorconfig", ctx))

    def test_existing_homeassistant_repository_does_not_get_editor_setting_candidates(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source.mkdir(parents=True)
            (source / "configuration.yaml").write_text("homeassistant:\n")

            self.assertFalse(
                server.sync_logic.initial_homeassistant_save(
                    [{"type": "homeassistant", "source_path": str(source)}]
                )
            )

    def test_save_preview_job_toggle_controls_registry_noise(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            seed_storage = seed / "homeassistant" / ".storage"
            seed_storage.mkdir(parents=True)
            (seed_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}})
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "1"}]}})
            )
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

            server.write_state({"include_redundant_data": False, "post_apply_save_recommended": True})
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_diff"], "")
            self.assertFalse(state["post_apply_save_recommended"])

            server.write_state({"include_redundant_data": True})
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("- Modified: homeassistant/.storage/core.device_registry", state["last_save_preview"])
            self.assertIn("modified_at", state["last_save_diff"])
            self.assertIn("git-modified-at", state["last_save_diff"])
            self.assertIn("live-modified-at", state["last_save_diff"])

    def test_include_redundant_data_toggle_clears_stale_save_preview(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.updates = []

            def read_state(self):
                return {
                    "last_save_preview": "old preview",
                    "last_save_diff": "old huge diff",
                    "conflicts": ["homeassistant/.storage/core.device_registry"],
                    "conflict_type": "save_unknown_base",
                    "save_conflict_resolutions": {"homeassistant/.storage/core.device_registry": "ha"},
                }

            def write_state(self, updates):
                self.updates.append(updates)

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)
        request = handler.__new__(handler)
        request.path = "/include-redundant-data"
        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.headers["Accept"] = "application/json"
        request.headers["X-Requested-With"] = "fetch"
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)

        request.do_POST()

        self.assertEqual(request.responses[-1], 200)
        self.assertEqual(ctx.updates[-1]["include_redundant_data"], False)
        self.assertEqual(ctx.updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.updates[-1]["last_save_diff"], "")
        self.assertEqual(ctx.updates[-1]["conflicts"], [])
        self.assertIsNone(ctx.updates[-1]["conflict_type"])
        self.assertEqual(ctx.updates[-1]["save_conflict_resolutions"], {})

    def test_save_conflict_include_redundant_data_shows_registry_noise(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = root / "repo"
            repo_storage = repo / "homeassistant" / ".storage"
            preview_storage = server.WORK_DIR / "save-preview" / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            preview_storage.mkdir(parents=True)
            (repo_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}})
            )
            (preview_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "1"}]}})
            )
            ctx = server.app_context.AppContext(
                data_dir=server.DATA_DIR,
                config_dir=server.CONFIG_DIR,
                addon_configs_dir=server.ADDON_CONFIGS_DIR,
            )

            detail = server.web.save_conflict_detail(
                ctx,
                repo,
                [{"id": "homeassistant", "source_path": str(repo / "homeassistant")}],
                "homeassistant/.storage/core.device_registry",
                include_redundant_data=True,
            )

            self.assertIn("modified_at", detail)
            self.assertIn("git-modified-at", detail)
            self.assertIn("live-modified-at", detail)

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

    def test_save_restores_entity_registry_noise_only_worktree_changes(self):
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
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "modified_at": "git-modified-at",
                            "platform": "mqtt",
                            "suggested_object_id": "git_object",
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "modified_at": "git-phone-modified-at",
                            "original_icon": "mdi:battery-10",
                            "platform": "mobile_app",
                        },
                    ]
                }
            }
            exported_registry = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "modified_at": "live-phone-modified-at",
                            "original_icon": "mdi:battery-90",
                            "platform": "mobile_app",
                        },
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "modified_at": "live-modified-at",
                            "platform": "mqtt",
                            "suggested_object_id": "live_object",
                        },
                    ]
                }
            }
            registry_path = storage / "core.entity_registry"
            registry_path.write_text(json.dumps(committed_registry))
            self.git_commit_all(repo, "base")
            registry_path.write_text(json.dumps(exported_registry))

            class Ctx:
                def run_command(self, args, cwd=None):
                    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)

                def add_detail(self, details, detail):
                    details.append(detail)

            restored = server.sync_logic.restore_normalized_equal_save_worktree(
                repo,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                [],
                Ctx(),
            )

            self.assertEqual(restored, ["homeassistant/.storage/core.entity_registry"])
            self.assertEqual(self.git(["status", "--porcelain"], repo).stdout.strip(), "")

    def test_save_normalizes_changed_registry_worktree_preserves_hidden_fields(self):
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
                        {
                            "id": "device-1",
                            "connections": [["b", "2"], ["a", "1"]],
                            "manufacturer": "Git",
                            "modified_at": "git-modified-at",
                            "sw_version": "1",
                        },
                        {
                            "id": "device-2",
                            "modified_at": "git-kept-modified-at",
                            "sw_version": "same",
                        },
                    ]
                }
            }
            exported_registry = {
                "data": {
                    "devices": [
                        {
                            "id": "device-1",
                            "connections": [["a", "1"], ["b", "2"]],
                            "manufacturer": "Live",
                            "modified_at": "live-modified-at",
                            "sw_version": "2",
                        },
                        {
                            "id": "device-2",
                            "modified_at": "live-changed-modified-at",
                            "sw_version": "same",
                        },
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

            normalized = server.sync_logic.normalize_changed_save_registry_worktree(
                repo,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                [],
                Ctx(),
            )
            saved = json.loads(registry_path.read_text())
            text = registry_path.read_text()

            self.assertEqual(normalized, ["homeassistant/.storage/core.device_registry"])
            self.assertEqual(saved["data"]["devices"][0]["sw_version"], "1")
            self.assertEqual(saved["data"]["devices"][0]["manufacturer"], "Live")
            self.assertEqual(saved["data"]["devices"][0]["connections"], [["b", "2"], ["a", "1"]])
            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "git-modified-at")
            self.assertEqual(saved["data"]["devices"][1]["modified_at"], "git-kept-modified-at")
            self.assertIn(
                '      {"id":"device-1","connections":[["b","2"],["a","1"]],"manufacturer":"Live","modified_at":"git-modified-at","sw_version":"1"}',
                text,
            )
            self.assertIn(
                '      {"id":"device-2","modified_at":"git-kept-modified-at","sw_version":"same"}',
                text,
            )
            self.assertNotIn('\n        "id": "device-1"', text)

    def test_save_commit_matches_preview_for_hidden_registry_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            seed_storage = seed / "homeassistant" / ".storage"
            seed_storage.mkdir(parents=True)
            (seed_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "git-modified-at",
                                    "sw_version": "1",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "name": "Live Device",
                                    "modified_at": "live-modified-at",
                                    "sw_version": "2",
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
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_preview_job())
            state = server.read_state()
            self.assertNotIn("sw_version", state["last_save_diff"])
            self.assertNotIn('"sw_version": "2"', state["last_save_diff"])
            self.assertNotIn("modified_at", state["last_save_diff"])
            self.assertNotIn("git-modified-at", state["last_save_diff"])
            self.assertNotIn("live-modified-at", state["last_save_diff"])

            server.write_state(
                {
                    "save_preview_selected_paths": ["homeassistant/.storage/core.device_registry"],
                    "save_preview_resolutions": {
                        "homeassistant/.storage/core.device_registry": "ha",
                    }
                }
            )
            self.assertTrue(server.run_save_job())
            saved = json.loads(self.remote_file(remote, "homeassistant/.storage/core.device_registry"))
            saved_device = saved["data"]["devices"][0]
            self.assertEqual(saved_device["sw_version"], "1")
            self.assertEqual(saved_device["modified_at"], "git-modified-at")

    def test_save_commit_preserves_hidden_entity_registry_fields(self):
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
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "modified_at": "old-modified",
                            "suggested_object_id": "old_object",
                            "platform": "mqtt",
                            "supported_features": 1,
                            "original_name": "old name",
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "modified_at": "old-phone-modified",
                            "original_icon": "mdi:battery-10",
                            "platform": "mobile_app",
                            "supported_features": 1,
                            "original_name": "old phone name",
                        },
                    ]
                }
            }
            exported_registry = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.test",
                            "modified_at": "new-modified",
                            "suggested_object_id": "new_object",
                            "platform": "mqtt",
                            "supported_features": 2,
                            "original_name": "new name",
                        },
                        {
                            "id": "entity-2",
                            "entity_id": "sensor.phone",
                            "modified_at": "new-phone-modified",
                            "original_icon": "mdi:battery-90",
                            "platform": "mobile_app",
                            "supported_features": 2,
                            "original_name": "new phone name",
                        },
                    ]
                }
            }
            registry_path = storage / "core.entity_registry"
            registry_path.write_text(json.dumps(committed_registry))
            self.git_commit_all(repo, "base")
            registry_path.write_text(json.dumps(exported_registry))

            class Ctx:
                def run_command(self, args, cwd=None):
                    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)

                def add_detail(self, details, detail):
                    details.append(detail)

            normalized = server.sync_logic.normalize_changed_save_registry_worktree(
                repo,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                [],
                Ctx(),
            )
            saved = json.loads(registry_path.read_text())
            first, second = saved["data"]["entities"]

            self.assertEqual(normalized, ["homeassistant/.storage/core.entity_registry"])
            self.assertEqual(first["supported_features"], 1)
            self.assertEqual(first["modified_at"], "old-modified")
            self.assertEqual(first["suggested_object_id"], "old_object")
            self.assertEqual(first["original_name"], "new name")
            self.assertEqual(second["supported_features"], 1)
            self.assertEqual(second["modified_at"], "old-phone-modified")
            self.assertEqual(second["original_icon"], "mdi:battery-10")
            self.assertEqual(second["original_name"], "new phone name")

    def test_save_include_redundant_data_commits_live_registry_hidden_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            seed_storage = seed / "homeassistant" / ".storage"
            seed_storage.mkdir(parents=True)
            (seed_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}})
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "2"}]}})
            )
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
            server.write_state(
                {
                    "include_redundant_data": True,
                    "save_conflict_resolutions": {"homeassistant/.storage/core.device_registry": "ha"},
                }
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            server.write_state(
                {
                    "save_preview_selected_paths": ["homeassistant/.storage/core.device_registry"],
                    "save_preview_resolutions": {"homeassistant/.storage/core.device_registry": "ha"},
                }
            )
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            saved = json.loads(self.remote_file(remote, "homeassistant/.storage/core.device_registry"))

            self.assertEqual(saved["data"]["devices"][0]["sw_version"], "2")
            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "live-modified-at")

    def test_save_commit_preserves_hidden_registry_order_when_real_fields_change(self):
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
                        {
                            "id": "device-b",
                            "connections": [],
                            "sw_version": "same",
                        },
                        {
                            "id": "device-a",
                            "connections": [["b", "2"], ["a", "1"]],
                            "config_entries_subentries": {"entry": ["b", None, "a"]},
                            "manufacturer": "Git",
                            "sw_version": "1",
                        },
                    ]
                }
            }
            exported_registry = {
                "data": {
                    "devices": [
                        {
                            "id": "device-a",
                            "connections": [["a", "1"], ["b", "2"]],
                            "config_entries_subentries": {"entry": ["a", "b", None]},
                            "manufacturer": "Live",
                            "sw_version": "2",
                        },
                        {
                            "id": "device-b",
                            "connections": [],
                            "sw_version": "same",
                        },
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

            normalized = server.sync_logic.normalize_changed_save_registry_worktree(
                repo,
                [{"id": "homeassistant", "type": "homeassistant", "source_path": str(repo / "homeassistant")}],
                [],
                Ctx(),
            )
            saved_devices = json.loads(registry_path.read_text())["data"]["devices"]

            self.assertEqual(normalized, ["homeassistant/.storage/core.device_registry"])
            self.assertEqual([item["id"] for item in saved_devices], ["device-b", "device-a"])
            self.assertEqual(saved_devices[1]["sw_version"], "1")
            self.assertEqual(saved_devices[1]["manufacturer"], "Live")
            self.assertEqual(saved_devices[1]["connections"], [["b", "2"], ["a", "1"]])
            self.assertEqual(saved_devices[1]["config_entries_subentries"], {"entry": ["b", None, "a"]})

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

    def test_approve_save_conflicts_error_message_comes_from_translation_catalog(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.state = {}
                self.state_updates = []

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)
        request = handler.__new__(handler)
        request.path = "/approve-save-conflicts"
        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.headers["Accept"] = "application/json"
        request.headers["X-Requested-With"] = "fetch"
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)

        key = "message.no_save_conflicts_pending_approval"
        original = server.web.i18n.EN_TEXT[key]
        server.web.i18n.EN_TEXT[key] = "CATALOG: no Save approvals pending."
        try:
            request.do_POST()
        finally:
            server.web.i18n.EN_TEXT[key] = original

        self.assertEqual(request.responses[-1], 500)
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(response, {"ok": False, "message": "CATALOG: no Save approvals pending."})
        self.assertEqual(ctx.state_updates[-1]["last_message"], "CATALOG: no Save approvals pending.")
        self.assertEqual(ctx.state_updates[-1]["last_details"], ["CATALOG: no Save approvals pending."])
        self.assertNotIn("No Save conflicts are pending approval", json.dumps(response))

    def test_resolve_conflict_errors_come_from_translation_catalog(self):
        server = load_server()

        class FakeContext:
            def __init__(self, state, actual_conflicts=None):
                self.run_lock = threading.Lock()
                self.state = dict(state)
                self.state_updates = []
                self.actual_conflicts = list(actual_conflicts or [])

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def load_options(self):
                return {"repo_branch": "main"}

            def repo_checkout_path(self, _options):
                return Path("/tmp/ha-ops-test-repo")

            def git_conflict_paths(self, _repo_dir):
                return list(self.actual_conflicts)

        def invoke(ctx, body):
            handler = server.web.create_handler(ctx)
            request = handler.__new__(handler)
            request.path = "/resolve-conflict"
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["X-Requested-With"] = "fetch"
            request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            return request

        cases = [
            (
                "save invalid choice",
                "error.invalid_conflict_choice",
                {
                    "last_status": "idle",
                    "conflict_type": "save_unknown_base",
                    "conflicts": ["homeassistant/configuration.yaml"],
                    "save_conflict_resolutions": {},
                },
                None,
                b"path=homeassistant/configuration.yaml&choice=bad",
                "Invalid conflict choice",
            ),
            (
                "save non-pending path",
                "error.save_conflict_path_not_pending",
                {
                    "last_status": "idle",
                    "conflict_type": "save_unknown_base",
                    "conflicts": ["homeassistant/automations.yaml"],
                    "save_conflict_resolutions": {},
                },
                None,
                b"path=homeassistant/configuration.yaml&choice=ha",
                "Save conflict path is not pending",
            ),
            (
                "git invalid choice",
                "error.invalid_conflict_choice",
                {"last_status": "idle", "conflict_type": "git_rebase"},
                ["homeassistant/configuration.yaml"],
                b"path=homeassistant/configuration.yaml&choice=bad",
                "Invalid conflict choice",
            ),
            (
                "git non-pending path",
                "error.git_conflict_path_not_pending",
                {"last_status": "idle", "conflict_type": "git_rebase"},
                ["homeassistant/automations.yaml"],
                b"path=homeassistant/configuration.yaml&choice=ha",
                "Git conflict path is not pending",
            ),
        ]

        originals = {}
        try:
            for _name, key, *_rest in cases:
                originals.setdefault(key, server.web.i18n.EN_TEXT[key])
                server.web.i18n.EN_TEXT[key] = f"CATALOG: {key}"

            for name, key, state, actual_conflicts, body, old_text in cases:
                with self.subTest(name=name):
                    ctx = FakeContext(state, actual_conflicts)
                    request = invoke(ctx, body)
                    response = json.loads(request.wfile.getvalue().decode())
                    expected = f"CATALOG: {key}"

                    self.assertEqual(request.responses[-1], 500)
                    self.assertEqual(response, {"ok": False, "message": expected})
                    self.assertEqual(ctx.state_updates[-1]["last_message"], expected)
                    self.assertEqual(ctx.state_updates[-1]["last_details"], [expected])
                    self.assertNotIn(old_text, json.dumps(response))
        finally:
            server.web.i18n.EN_TEXT.update(originals)

    def test_preview_reserves_run_slot_before_background_worker_starts(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.calls = []
                self.state_updates = []
                self.state = {
                    "last_status": "idle",
                    "last_diff": "old apply preview",
                    "last_preview_commit": "old-commit",
                    "last_save_preview": "old save preview",
                    "last_save_diff": "old save diff",
                }

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def run_preview_job(self, lock_acquired=False):
                try:
                    self.calls.append(("preview", lock_acquired))
                    self.write_state(
                        {
                            "last_status": "success",
                            "last_action": "preview",
                            "last_message": "preview complete",
                        }
                    )
                finally:
                    if lock_acquired:
                        self.run_lock.release()

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                try:
                    self.calls.append(("save", commit_subject, lock_acquired))
                    self.write_state(
                        {
                            "last_status": "success",
                            "last_action": "save",
                            "last_message": "save complete",
                        }
                    )
                finally:
                    if lock_acquired:
                        self.run_lock.release()

        ctx = FakeContext()
        queued = []
        original_start_background = server.web.start_background

        def queue_background(target, *args, lock_acquired=False):
            queued.append((target, args, {"lock_acquired": lock_acquired}))

        handler = server.web.create_handler(ctx)

        def invoke(path):
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(b"")
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["X-Requested-With"] = "fetch"
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            return request

        server.web.start_background = queue_background
        try:
            preview_request = invoke("/preview")
            self.assertEqual(preview_request.responses[-1], 200)
            self.assertEqual(len(queued), 1)
            self.assertEqual(ctx.calls, [])
            self.assertEqual(ctx.state["last_diff"], "")
            self.assertIsNone(ctx.state["last_preview_commit"])
            state_after_reserved_preview = dict(ctx.state)
            update_count_after_reserved_preview = len(ctx.state_updates)

            save_request = invoke("/save")
            self.assertEqual(save_request.responses[-1], 409)
            save_response = json.loads(save_request.wfile.getvalue().decode())
            self.assertFalse(save_response["ok"])
            self.assertIn("already running", save_response["message"])
            self.assertEqual(ctx.state, state_after_reserved_preview)
            self.assertEqual(len(ctx.state_updates), update_count_after_reserved_preview)
            self.assertEqual(len(queued), 1)
            self.assertEqual(ctx.calls, [])

            target, args, kwargs = queued.pop()
            target(*args, **kwargs)
            self.assertEqual(ctx.calls, [("preview", True)])
            self.assertEqual(ctx.state["last_status"], "success")
            self.assertEqual(ctx.state["last_action"], "preview")
            self.assertNotEqual(ctx.state["last_status"], "busy")
            self.assertTrue(ctx.run_lock.acquire(blocking=False))
            ctx.run_lock.release()
        finally:
            server.web.start_background = original_start_background

    def test_preview_state_mutations_reject_when_job_reserves_after_running_check(self):
        server = load_server()

        class InterleavingRunLock:
            def __init__(self, owner):
                self.owner = owner
                self.locked = False

            def acquire(self, blocking=False):
                if self.locked:
                    return False
                self.locked = True
                return True

            def release(self):
                if not self.locked:
                    raise RuntimeError("run lock released while unlocked")
                self.locked = False
                if self.owner.interleave_on_next_release:
                    self.owner.interleave_on_next_release = False
                    self.owner.interleaved_reservations += 1
                    self.locked = True

        class FakeContext:
            def __init__(self, state):
                self.state = dict(state)
                self.state_updates = []
                self.calls = []
                self.interleave_on_next_release = True
                self.interleaved_reservations = 0
                self.run_lock = InterleavingRunLock(self)

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def utc_now(self):
                return "2026-06-15T12:00:00+00:00"

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                self.calls.append(("save", commit_subject, lock_acquired))

        def invoke(ctx, path, body=b""):
            handler = server.web.create_handler(ctx)
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["X-Requested-With"] = "fetch"
            if body:
                request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            return request

        original_approve = server.web.conflict_logic.approve_save_unknown_base_conflicts
        original_resolve = server.web.conflict_logic.resolve_git_conflict

        def fake_approve(handler_ctx):
            handler_ctx.write_state({"save_conflict_resolutions": {"homeassistant/configuration.yaml": "ha"}})
            return "approved"

        def fake_resolve(handler_ctx, path, choice):
            handler_ctx.write_state({"resolved_conflict": {path: choice}})
            return "resolved"

        server.web.conflict_logic.approve_save_unknown_base_conflicts = fake_approve
        server.web.conflict_logic.resolve_git_conflict = fake_resolve
        try:
            cases = [
                (
                    "/clear-preview",
                    b"direction=apply",
                    {
                        "last_status": "idle",
                        "last_diff": "apply preview",
                        "last_preview_commit": "apply-commit",
                    },
                ),
                (
                    "/resolve-apply-preview",
                    b"path=homeassistant/configuration.yaml&choice=git",
                    {
                        "last_status": "idle",
                        "last_preview_paths": ["homeassistant/configuration.yaml"],
                        "last_preview_conflicts": True,
                        "apply_preview_resolutions": {},
                    },
                ),
                (
                    "/include-redundant-data",
                    b"include_redundant_data=1",
                    {
                        "last_status": "idle",
                        "include_redundant_data": False,
                        "last_save_preview": "save preview",
                        "conflict_type": "save_unknown_base",
                        "conflicts": ["homeassistant/configuration.yaml"],
                        "save_conflict_resolutions": {},
                    },
                ),
                (
                    "/approve-save-conflicts",
                    b"",
                    {
                        "last_status": "idle",
                        "conflict_type": "save_unknown_base",
                        "conflicts": ["homeassistant/configuration.yaml"],
                        "save_conflict_resolutions": {},
                    },
                ),
                (
                    "/resolve-conflict",
                    b"path=homeassistant/configuration.yaml&choice=ha",
                    {
                        "last_status": "idle",
                        "conflict_type": "save_unknown_base",
                        "conflicts": ["homeassistant/configuration.yaml"],
                        "save_conflict_resolutions": {},
                    },
                ),
            ]

            for path, body, initial_state in cases:
                with self.subTest(path=path):
                    ctx = FakeContext(initial_state)
                    request = invoke(ctx, path, body)
                    response = json.loads(request.wfile.getvalue().decode())

                    self.assertEqual(request.responses[-1], 409)
                    self.assertFalse(response["ok"])
                    self.assertIn("already running", response["message"])
                    self.assertEqual(ctx.state, initial_state)
                    self.assertEqual(ctx.state_updates, [])
                    self.assertEqual(ctx.calls, [])
                    self.assertEqual(ctx.interleaved_reservations, 1)
                    self.assertTrue(ctx.run_lock.locked)
        finally:
            server.web.conflict_logic.approve_save_unknown_base_conflicts = original_approve
            server.web.conflict_logic.resolve_git_conflict = original_resolve

    def test_preview_choice_update_does_not_auto_start_apply(self):
        server = load_server()

        class FakeRunLock:
            def acquire(self, blocking=False):
                return True

            def release(self):
                pass

        class FakeContext:
            def __init__(self):
                self.run_lock = FakeRunLock()
                self.calls = []
                self.state = {
                    "last_status": "idle",
                    "last_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_preview_conflicts": False,
                    "apply_preview_resolutions": {},
                }

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state.update(updates)

            def utc_now(self):
                return "2026-06-17T12:00:00+00:00"

            def run_apply_job(self, lock_acquired=False):
                self.calls.append(("apply", lock_acquired))

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)
        request = handler.__new__(handler)
        body = urlencode({
            "path": "homeassistant/configuration.yaml",
            "choice": "ha",
            "preview_identity": json.dumps(server.web.preview_identity_for_state(ctx.state, "apply")),
        }).encode()
        request.path = "/resolve-apply-preview"
        request.rfile = io.BytesIO(body)
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.headers["Accept"] = "application/json"
        request.headers["X-Requested-With"] = "fetch"
        request.headers["Content-Length"] = str(len(body))
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)

        request.do_POST()

        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 200)
        self.assertTrue(response["ok"])
        self.assertEqual(ctx.state["apply_preview_resolutions"], {"homeassistant/configuration.yaml": "ha"})
        self.assertEqual(ctx.calls, [])

    def test_preview_file_selection_updates_selected_paths_without_starting_jobs(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.run_lock = threading.Lock()
                self.calls = []
                self.state = {
                    "last_status": "idle",
                    "last_save_preview_paths": [
                        "homeassistant/configuration.yaml",
                        "homeassistant/automations.yaml",
                    ],
                    "save_preview_selected_paths": [],
                    "last_preview_paths": [
                        "homeassistant/configuration.yaml",
                        "homeassistant/automations.yaml",
                    ],
                    "apply_preview_selected_paths": [],
                }

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state.update(updates)

            def utc_now(self):
                return "2026-06-17T12:00:00+00:00"

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                self.calls.append(("save", commit_subject, lock_acquired))

            def run_apply_job(self, lock_acquired=False):
                self.calls.append(("apply", lock_acquired))

        def invoke(ctx, path, body):
            handler = server.web.create_handler(ctx)
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["X-Requested-With"] = "fetch"
            request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            return request

        ctx = FakeContext()
        save_identity = json.dumps(server.web.preview_identity_for_state(ctx.state, "save"))
        apply_identity = json.dumps(server.web.preview_identity_for_state(ctx.state, "apply"))

        request = invoke(ctx, "/select-save-preview", urlencode({
            "path": "homeassistant/configuration.yaml",
            "selected": "1",
            "preview_identity": save_identity,
        }).encode())
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 200)
        self.assertTrue(response["ok"])
        self.assertEqual(ctx.state["save_preview_selected_paths"], ["homeassistant/configuration.yaml"])
        self.assertEqual(ctx.calls, [])

        request = invoke(ctx, "/select-save-preview", urlencode({
            "path": "homeassistant/configuration.yaml",
            "preview_identity": save_identity,
        }).encode())
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 200)
        self.assertTrue(response["ok"])
        self.assertEqual(ctx.state["save_preview_selected_paths"], [])
        self.assertEqual(ctx.calls, [])

        request = invoke(ctx, "/select-apply-preview", urlencode({
            "selection_action": "all",
            "preview_identity": apply_identity,
        }).encode())
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 200)
        self.assertTrue(response["ok"])
        self.assertEqual(
            ctx.state["apply_preview_selected_paths"],
            ["homeassistant/configuration.yaml", "homeassistant/automations.yaml"],
        )
        self.assertEqual(ctx.calls, [])

        request = invoke(ctx, "/select-apply-preview", urlencode({
            "selection_action": "none",
            "preview_identity": apply_identity,
        }).encode())
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 200)
        self.assertTrue(response["ok"])
        self.assertEqual(ctx.state["apply_preview_selected_paths"], [])
        self.assertEqual(ctx.calls, [])

        request = invoke(ctx, "/select-apply-preview", urlencode({
            "path": "../configuration.yaml",
            "selected": "1",
            "preview_identity": apply_identity,
        }).encode())
        response = json.loads(request.wfile.getvalue().decode())
        self.assertEqual(request.responses[-1], 400)
        self.assertFalse(response["ok"])
        self.assertEqual(ctx.state["apply_preview_selected_paths"], [])
        self.assertEqual(ctx.calls, [])

    def test_missing_preview_selection_state_is_not_treated_as_select_all(self):
        server = load_server()
        paths = ["homeassistant/configuration.yaml"]

        selected = server.app_context.job_logic.selected_preview_paths({}, paths, "save_preview_selected_paths")

        self.assertEqual(selected, [])

    def test_apply_preview_conflict_defaults_selected_paths_to_git(self):
        server = load_server()
        preview = {
            "paths": ["homeassistant/.storage/core.entity_registry"],
            "conflicts": ["homeassistant/.storage/core.entity_registry"],
        }

        self.assertEqual(
            server.app_context.job_logic.apply_preview_resolutions_for_current_preview(
                {"apply_preview_selected_paths": ["homeassistant/.storage/core.entity_registry"]}, preview
            ),
            {"homeassistant/.storage/core.entity_registry": "git"},
        )
        self.assertEqual(
            server.app_context.job_logic.apply_preview_resolutions_for_current_preview(
                {
                    "apply_preview_selected_paths": ["homeassistant/.storage/core.entity_registry"],
                    "apply_preview_resolutions": {"homeassistant/.storage/core.entity_registry": "ha"},
                },
                preview,
            ),
            {"homeassistant/.storage/core.entity_registry": "ha"},
        )
        self.assertEqual(
            server.app_context.job_logic.apply_preview_resolutions_for_current_preview(
                {"apply_preview_selected_paths": ["homeassistant/configuration.yaml"]},
                {
                    "paths": ["homeassistant/.storage/core.entity_registry", "homeassistant/configuration.yaml"],
                    "conflicts": ["homeassistant/.storage/core.entity_registry"],
                },
            ),
            {
                "homeassistant/.storage/core.entity_registry": "ha",
                "homeassistant/configuration.yaml": "git",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "Select at least one"):
            server.app_context.job_logic.apply_preview_resolutions_for_current_preview(
                {"apply_preview_selected_paths": []}, preview
            )

    def test_web_handler_uses_context_for_health_and_post_actions(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.state_updates = []
                self.state = {}
                self.run_lock = threading.Lock()

            def record_call(self, call, lock_acquired=False):
                try:
                    self.calls.append(call)
                finally:
                    if lock_acquired:
                        self.run_lock.release()

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                self.record_call(("save", commit_subject), lock_acquired)

            def run_save_preview_job(self, lock_acquired=False):
                self.record_call("save-preview", lock_acquired)

            def run_reset_git_state_job(self, lock_acquired=False):
                self.record_call("reset-git-state", lock_acquired)

            def run_disk_usage_job(self, lock_acquired=False):
                self.record_call("disk-usage", lock_acquired)

            def run_preview_job(self, lock_acquired=False):
                self.record_call("preview", lock_acquired)

            def run_apply_job(self, lock_acquired=False):
                self.record_call("apply", lock_acquired)

            def run_deleted_devices_preview_job(self, lock_acquired=False):
                self.record_call("deleted-devices-preview", lock_acquired)

            def run_retained_devices_preview_job(self, lock_acquired=False):
                self.record_call("retained-devices-preview", lock_acquired)

            def run_internal_ids_preview_job(self, lock_acquired=False):
                self.record_call("internal-ids-preview", lock_acquired)

            def run_internal_ids_migrate_job(self, selected, lock_acquired=False):
                self.record_call(("internal-ids-migrate", selected), lock_acquired)

            def run_retained_devices_delete_job(self, selected, lock_acquired=False):
                self.record_call(("retained-devices-delete", selected), lock_acquired)

            def run_deleted_devices_delete_job(self, lock_acquired=False):
                self.record_call("deleted-devices-delete", lock_acquired)

            def run_deleted_devices_confirm_job(self, lock_acquired=False):
                self.record_call("deleted-devices-confirm", lock_acquired)

            def run_deleted_devices_revert_job(self, lock_acquired=False):
                self.record_call("deleted-devices-revert", lock_acquired)

            def run_rollback_job(self, release, lock_acquired=False):
                self.record_call(("rollback", release), lock_acquired)

            def clear_display_state(self):
                self.calls.append("clear-display")

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def read_state(self):
                return dict(self.state)

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
            request.send_error = MethodType(lambda self, status, message=None: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            getattr(request, method)()
            return request

        get_request = invoke("do_GET", "/health")
        self.assertEqual(get_request.responses[-1], 200)
        self.assertEqual(json.loads(get_request.wfile.getvalue().decode()), {"ok": True})

        retained_delete_payload = {
            "candidate": ["0", "2"],
            "retained_preview_fingerprint": ["fp"],
            "retained_preview_generated_at": ["2026-08-31T10:00:00+00:00"],
        }
        post_request = invoke(
            "do_POST",
            "/save",
            body=b"commit_subject=Custom+HA+save",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Save HA to Git started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, [("save", "Custom HA save")])

        post_request = invoke(
            "do_POST",
            "/save",
            body=(
                b"commit_subject=Save+Home+Assistant+config+2026-06-24_17-00-00"
                b"&default_commit_subject=Save+Home+Assistant+config+2026-06-24_17-00-00"
            ),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Save HA to Git started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, [("save", "Custom HA save"), ("save", None)])

        post_request = invoke(
            "do_POST",
            "/save-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("HA to Git preview started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, [("save", "Custom HA save"), ("save", None), "save-preview"])
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])

        post_request = invoke(
            "do_POST",
            "/preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Git to HA preview started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls, [("save", "Custom HA save"), ("save", None), "save-preview", "preview"])
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])
        self.assertIsNone(ctx.state_updates[-1]["last_preview_fingerprint"])
        self.assertFalse(ctx.state_updates[-1]["last_preview_storage_changes"])

        post_request = invoke(
            "do_POST",
            "/reset-git-state",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Git state reset started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [("save", "Custom HA save"), ("save", None), "save-preview", "preview", "reset-git-state"],
        )
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])

        post_request = invoke(
            "do_POST",
            "/disk-usage",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Disk usage check started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [("save", "Custom HA save"), ("save", None), "save-preview", "preview", "reset-git-state", "disk-usage"],
        )

        post_request = invoke(
            "do_POST",
            "/approve-apply",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 404)
        self.assertEqual(
            ctx.calls,
            [("save", "Custom HA save"), ("save", None), "save-preview", "preview", "reset-git-state", "disk-usage"],
        )

        post_request = invoke(
            "do_POST",
            "/deleted-devices-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Deleted devices and entities check started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
            ],
        )
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_deleted_devices_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_deleted_devices_count"], 0)
        self.assertIsNone(ctx.state_updates[-1]["last_deleted_devices_generated_at"])

        post_request = invoke(
            "do_POST",
            "/retained-devices-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Retained devices check started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
            ],
        )
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_retained_devices_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_retained_devices_count"], 0)
        self.assertIsNone(ctx.state_updates[-1]["last_retained_devices_generated_at"])

        post_request = invoke(
            "do_POST",
            "/internal-ids-preview",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Internal ids check started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
            ],
        )
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])
        self.assertEqual(ctx.state_updates[-1]["last_internal_ids_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_internal_ids_count"], 0)
        self.assertIsNone(ctx.state_updates[-1]["last_internal_ids_generated_at"])

        ctx.state.update(
            {
                "last_retained_devices_fingerprint": "fp",
                "last_retained_devices_generated_at": "2026-08-31T10:00:00+00:00",
            }
        )
        post_request = invoke(
            "do_POST",
            "/retained-devices-delete",
            body=b"candidate=0&candidate=2&retained_preview_fingerprint=fp&retained_preview_generated_at=2026-08-31T10%3A00%3A00%2B00%3A00",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Retained devices deletion started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
                ("retained-devices-delete", retained_delete_payload),
            ],
        )

        post_request = invoke(
            "do_POST",
            "/deleted-devices-delete",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted devices deletion started", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.calls[-1], "deleted-devices-delete")

        post_request = invoke(
            "do_POST",
            "/deleted-devices-confirm",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted devices cleanup confirmation started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
                ("retained-devices-delete", retained_delete_payload),
                "deleted-devices-delete",
                "deleted-devices-confirm",
            ],
        )

        post_request = invoke(
            "do_POST",
            "/deleted-devices-revert",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("deleted devices cleanup revert started", post_request.wfile.getvalue().decode())
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
                ("retained-devices-delete", retained_delete_payload),
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
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
                ("retained-devices-delete", retained_delete_payload),
                "deleted-devices-delete",
                "deleted-devices-confirm",
                "deleted-devices-revert",
                "clear-display",
            ],
        )

        post_request = invoke(
            "do_POST",
            "/clear-preview",
            body=b"direction=save",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Save preview cancelled", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])
        self.assertNotIn("last_diff", ctx.state_updates[-1])

        post_request = invoke(
            "do_POST",
            "/clear-preview",
            body=b"direction=apply",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Apply preview cancelled", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.state_updates[-1]["last_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_diff_generated_at"])
        self.assertIsNone(ctx.state_updates[-1]["last_preview_commit"])
        self.assertNotIn("last_save_preview", ctx.state_updates[-1])

        post_request = invoke(
            "do_POST",
            "/clear-preview",
            body=b"direction=bad",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 400)
        self.assertIn("Invalid preview direction", post_request.wfile.getvalue().decode())

        ctx.state["last_status"] = "running"
        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path in ("/save", "/apply"):
                post_request = invoke(
                    "do_POST",
                    path,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        ctx.state.update(
            {
                "last_status": "running",
                "last_diff": "apply diff",
                "last_diff_generated_at": "2026-06-15T12:00:00+00:00",
                "last_preview_commit": "apply-commit",
                "last_preview_fingerprint": "apply-fingerprint",
                "last_preview_live_fingerprints": {"homeassistant/configuration.yaml": "live"},
                "last_preview_paths": ["homeassistant/configuration.yaml"],
                "last_preview_conflicts": True,
                "apply_preview_resolutions": {"homeassistant/configuration.yaml": "git"},
                "last_save_preview": "save preview",
                "last_save_diff": "save diff",
                "last_save_diff_generated_at": "2026-06-15T12:00:00+00:00",
                "last_save_preview_commit": "save-commit",
                "last_save_preview_fingerprint": "save-fingerprint",
                "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                "last_save_preview_conflicts": True,
                "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                "conflicts": ["homeassistant/.storage/core.device_registry"],
                "conflict_type": "save_unknown_base",
                "save_conflict_resolutions": {"homeassistant/.storage/core.device_registry": "ha"},
            }
        )
        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        ctx.run_lock.acquire()
        try:
            for direction in ("save", "apply"):
                post_request = invoke(
                    "do_POST",
                    "/clear-preview",
                    body=f"direction={direction}".encode(),
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
        finally:
            ctx.run_lock.release()
        ctx.state["last_status"] = "idle"

        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            post_request = invoke(
                "do_POST",
                "/include-redundant-data",
                body=b"include_redundant_data=1",
                headers={"Accept": "application/json", "X-Requested-With": "fetch"},
            )
            self.assertEqual(post_request.responses[-1], 409)
            response = json.loads(post_request.wfile.getvalue().decode())
            self.assertFalse(response["ok"])
            self.assertIn("already running", response["message"])
            self.assertEqual(ctx.state, expected_state)
            self.assertEqual(len(ctx.state_updates), update_count)
            self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path in ("/save", "/apply"):
                post_request = invoke(
                    "do_POST",
                    path,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        ctx.state.update(
            {
                "last_preview_conflicts": False,
                "apply_preview_resolutions": {},
                "last_save_preview_conflicts": False,
                "save_preview_resolutions": {},
            }
        )
        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path, body in (
                ("/resolve-save-preview", b"path=homeassistant/configuration.yaml&choice=ha"),
                ("/resolve-apply-preview", b"path=homeassistant/configuration.yaml&choice=git"),
            ):
                post_request = invoke(
                    "do_POST",
                    path,
                    body=body,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path in (
                "/preview",
                "/save-preview",
                "/deleted-devices-preview",
                "/retained-devices-preview",
                "/internal-ids-preview",
            ):
                post_request = invoke(
                    "do_POST",
                    path,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path, body in (
                ("/retained-devices-delete", b"candidate=0&candidate=2"),
                ("/internal-ids-migrate", b"candidate=0&candidate=2"),
                ("/deleted-devices-delete", b""),
                ("/deleted-devices-confirm", b""),
                ("/deleted-devices-revert", b""),
                ("/rollback", b"release=0.8.13"),
            ):
                post_request = invoke(
                    "do_POST",
                    path,
                    body=body,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        ctx.state.update(
            {
                "last_status": "idle",
                "conflict_type": "save_unknown_base",
                "conflicts": ["homeassistant/configuration.yaml"],
                "save_conflict_resolutions": {},
            }
        )
        expected_state = dict(ctx.state)
        update_count = len(ctx.state_updates)
        expected_calls = list(ctx.calls)
        ctx.run_lock.acquire()
        try:
            for path, body in (
                ("/approve-save-conflicts", b""),
                ("/resolve-conflict", b"path=homeassistant/configuration.yaml&choice=ha"),
            ):
                post_request = invoke(
                    "do_POST",
                    path,
                    body=body,
                    headers={"Accept": "application/json", "X-Requested-With": "fetch"},
                )
                self.assertEqual(post_request.responses[-1], 409)
                response = json.loads(post_request.wfile.getvalue().decode())
                self.assertFalse(response["ok"])
                self.assertIn("already running", response["message"])
                self.assertEqual(ctx.state, expected_state)
                self.assertEqual(len(ctx.state_updates), update_count)
                self.assertEqual(ctx.calls, expected_calls)
        finally:
            ctx.run_lock.release()

        original_resolve_git_conflict = server.web.conflict_logic.resolve_git_conflict
        original_resolved_message = server.web.i18n.EN_TEXT["message.resolved_conflict_refreshing"]

        def fake_resolve_git_conflict(handler_ctx, path, choice):
            self.assertIs(handler_ctx, ctx)
            self.assertEqual(path, "homeassistant/configuration.yaml")
            self.assertEqual(choice, "ha")
            return "fake conflict resolution"

        server.web.conflict_logic.resolve_git_conflict = fake_resolve_git_conflict
        server.web.i18n.EN_TEXT["message.resolved_conflict_refreshing"] = "CATALOG: {message}; client refresh pending."
        try:
            post_request = invoke(
                "do_POST",
                "/resolve-conflict",
                body=b"path=homeassistant/configuration.yaml&choice=ha",
                headers={"Accept": "application/json", "X-Requested-With": "fetch"},
            )
            self.assertEqual(post_request.responses[-1], 200)
            response = json.loads(post_request.wfile.getvalue().decode())
            self.assertTrue(response["ok"])
            self.assertEqual(response["message"], "CATALOG: fake conflict resolution; client refresh pending.")
            self.assertNotIn("Refreshing...", response["message"])
        finally:
            server.web.conflict_logic.resolve_git_conflict = original_resolve_git_conflict
            server.web.i18n.EN_TEXT["message.resolved_conflict_refreshing"] = original_resolved_message

        post_request = invoke(
            "do_POST",
            "/homeassistant-organizer",
            body=b"homeassistant_organizer=1",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 400)
        response = json.loads(post_request.wfile.getvalue().decode())
        self.assertFalse(response["ok"])
        self.assertIn("organizer area split is paused", response["message"])
        self.assertEqual(
            ctx.calls,
            [
                ("save", "Custom HA save"),
                ("save", None),
                "save-preview",
                "preview",
                "reset-git-state",
                "disk-usage",
                "deleted-devices-preview",
                "retained-devices-preview",
                "internal-ids-preview",
                ("retained-devices-delete", retained_delete_payload),
                "deleted-devices-delete",
                "deleted-devices-confirm",
                "deleted-devices-revert",
                "clear-display",
            ],
        )

        post_request = invoke(
            "do_POST",
            "/include-redundant-data",
            body=b"include_redundant_data=1",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(post_request.responses[-1], 200)
        self.assertIn("Redundant data setting updated", post_request.wfile.getvalue().decode())
        self.assertEqual(ctx.state_updates[-1]["include_redundant_data"], True)
        self.assertEqual(ctx.state_updates[-1]["last_save_preview"], "")
        self.assertEqual(ctx.state_updates[-1]["last_save_diff"], "")
        self.assertIsNone(ctx.state_updates[-1]["last_save_diff_generated_at"])

    def test_save_push_retry_blocks_unrelated_workflow_post_actions(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.state_updates = []
                self.state = {
                    "save_push_retry_pending": True,
                    "save_push_retry_commit": "pending-save",
                }
                self.run_lock = threading.Lock()

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def run_save_job(self, commit_subject=None, lock_acquired=False):
                try:
                    self.calls.append(("save", commit_subject, lock_acquired))
                finally:
                    if lock_acquired:
                        self.run_lock.release()

        ctx = FakeContext()
        queued = []
        original_start_background = server.web.start_background

        def queue_background(target, *args, lock_acquired=False):
            queued.append((target, args, {"lock_acquired": lock_acquired}))

        handler = server.web.create_handler(ctx)

        def invoke(path, body=b""):
            request = handler.__new__(handler)
            request.path = path
            request.rfile = io.BytesIO(body)
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Accept"] = "application/json"
            request.headers["X-Requested-With"] = "fetch"
            if body:
                request.headers["Content-Length"] = str(len(body))
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_error = MethodType(lambda self, status, message=None: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)
            request.do_POST()
            return request

        blocked_posts = [
            ("/apply", b""),
            ("/generate-key", b""),
            ("/resolve-save-preview", b"path=homeassistant%2Fconfiguration.yaml&choice=ha"),
            ("/resolve-apply-preview", b"path=homeassistant%2Fconfiguration.yaml&choice=git"),
            ("/clear-preview", b"direction=apply"),
            ("/preview", b""),
            ("/select-save-preview", b"selection_action=all"),
            ("/select-apply-preview", b"selection_action=all"),
            ("/save-preview", b""),
            ("/reset-git-state", b""),
            ("/disk-usage", b""),
            ("/deleted-devices-preview", b""),
            ("/retained-devices-preview", b""),
            ("/retained-devices-delete", b"candidate=0"),
            ("/internal-ids-preview", b""),
            ("/internal-ids-migrate", b"candidate=0"),
            ("/deleted-devices-delete", b""),
            ("/deleted-devices-confirm", b""),
            ("/deleted-devices-revert", b""),
            ("/approve-save-conflicts", b""),
            ("/addons", b"addon=local_zigbee2mqtt"),
            ("/homeassistant-organizer", b"homeassistant_organizer=on"),
            ("/include-redundant-data", b"include_redundant_data=on"),
            ("/resolve-conflict", b"path=homeassistant%2Fconfiguration.yaml&choice=git"),
            ("/rollback", b"release=0.8.44"),
        ]

        server.web.start_background = queue_background
        try:
            for path, body in blocked_posts:
                with self.subTest(path=path):
                    response = invoke(path, body)
                    payload = json.loads(response.wfile.getvalue().decode())
                    self.assertEqual(response.responses[-1], 409)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["message"], "Save push retry is still pending.")
                    self.assertEqual(ctx.calls, [])
                    self.assertEqual(ctx.state_updates, [])
                    self.assertEqual(queued, [])

            retry_response = invoke("/save")
            retry_payload = json.loads(retry_response.wfile.getvalue().decode())
            self.assertEqual(retry_response.responses[-1], 200)
            self.assertTrue(retry_payload["ok"])
            self.assertEqual(retry_payload["message"], "Save HA to Git started.")
            self.assertEqual(len(queued), 1)
            target, args, kwargs = queued.pop()
            target(*args, **kwargs)
            self.assertEqual(ctx.calls, [("save", None, True)])
        finally:
            server.web.start_background = original_start_background

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

    def test_apply_rejects_enabled_organizer_heap_source_before_heap_mode_copy(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            self.write_heap_yaml_set(source, "Git")
            (live / "configuration.yaml").write_text("live_only:\n")
            self.write_stale_organizer_view(live)

            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "organizer": {"enabled": True},
            }

            error = server.sync_logic.organizer.OrganizerRemovedError
            with self.assertRaisesRegex(error, "organizer area split is paused"):
                server.apply_homeassistant_config(source, live, target)

            self.assertEqual((live / "configuration.yaml").read_text(), "live_only:\n")

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

    def test_apply_preview_preserves_live_registry_hidden_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "live-modified-at",
                                    "sw_version": "2",
                                }
                            ]
                        }
                    }
                )
            )
            (source / ".storage" / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "git-modified-at",
                                    "sw_version": "1",
                                }
                            ]
                        }
                    }
                )
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
            preview_storage = server.WORK_DIR / "apply-preview" / "homeassistant" / ".storage"
            saved = json.loads((preview_storage / "core.device_registry").read_text())

            self.assertNotIn("sw_version", preview["diff"])
            self.assertEqual(saved["data"]["devices"][0]["sw_version"], "2")
            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "live-modified-at")
            self.assertFalse(preview["storage_changes"])
            self.assertEqual(preview["storage_change_paths"], [])
            self.assertNotIn("modified_at", preview["diff"])
            self.assertNotIn("git-modified-at", preview["diff"])
            self.assertNotIn("live-modified-at", preview["diff"])

    def test_selected_apply_targets_use_raw_preview_not_normalized_diff_storage(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "delete": False,
            }
            (live / ".storage" / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (source / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "input_datetime.time_battery_report",
                                    "modified_at": "2026-06-18T20:00:00+00:00",
                                    "platform": "input_datetime",
                                    "suggested_object_id": "time_battery_report",
                                    "supported_features": 0,
                                    "unique_id": "battery_report_time",
                                }
                            ]
                        }
                    }
                )
            )

            preview = server.build_apply_preview([target])
            raw_registry = json.loads(
                (
                    server.WORK_DIR / "apply-preview" / "homeassistant" / ".storage" / "core.entity_registry"
                ).read_text()
            )
            normalized_registry = json.loads(
                (
                    server.WORK_DIR
                    / "apply-preview-diff"
                    / "homeassistant"
                    / "preview"
                    / ".storage"
                    / "core.entity_registry"
                ).read_text()
            )
            selected_targets = server.selected_apply_targets_from_preview([target], [])
            selected_registry = json.loads(
                (Path(selected_targets[0]["source_path"]) / ".storage" / "core.entity_registry").read_text()
            )

            self.assertIn("homeassistant/.storage/core.entity_registry", preview["paths"])
            self.assertIn("modified_at", raw_registry["data"]["entities"][0])
            self.assertNotIn("modified_at", normalized_registry["data"]["entities"][0])
            self.assertEqual(
                selected_registry["data"]["entities"][0]["modified_at"],
                "2026-06-18T20:00:00+00:00",
            )
            self.assertEqual(selected_registry["data"]["entities"][0]["suggested_object_id"], "time_battery_report")
            self.assertEqual(selected_registry["data"]["entities"][0]["supported_features"], 0)

    def test_apply_preview_fingerprint_ignores_diff_header_timestamps(self):
        server = load_server()
        first = "\n".join(
            [
                "## homeassistant",
                "--- /tmp/left/core.device_registry\t2026-05-21 10:00:00.000000000 +0200",
                "+++ /tmp/right/core.device_registry\t2026-05-21 10:00:01.000000000 +0200",
                "@@ -1 +1 @@",
                "-old",
                "+new",
            ]
        )
        second = first.replace("10:00:00.000000000", "10:05:00.000000000").replace(
            "10:00:01.000000000",
            "10:05:01.000000000",
        )

        self.assertEqual(server.sync_logic.fingerprint_text(first), server.sync_logic.fingerprint_text(second))

    def test_apply_preview_warns_when_entity_registry_metadata_would_downgrade(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            live_registry = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.example",
                            "unique_id": "example",
                            "platform": "zha",
                            "device_id": "device-1",
                            "entity_category": "diagnostic",
                            "has_entity_name": True,
                            "capabilities": {"state_class": "total"},
                        }
                    ]
                }
            }
            git_registry = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "sensor.example",
                            "unique_id": "example",
                            "platform": "zha",
                        }
                    ]
                }
            }
            (live / ".storage" / "core.entity_registry").write_text(json.dumps(live_registry))
            (source / ".storage" / "core.entity_registry").write_text(json.dumps(git_registry))

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

            self.assertTrue(preview["warnings"])
            self.assertIn("sensor.example", preview["warnings"][0])
            self.assertIn("device_id", preview["warnings"][0])
            self.assertIn("Run HA to Git first", preview["warnings"][0])
            self.assertNotIn("## Warnings", preview["diff"])

    def test_apply_preview_warns_when_registry_items_would_be_removed(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            live_device_registry = {
                "data": {
                    "devices": [
                        {
                            "id": "device-1",
                            "name": "0xa4c13877facbdebd",
                            "identifiers": [["mqtt", "zigbee2mqtt_0xa4c13877facbdebd"]],
                        }
                    ]
                }
            }
            live_entity_registry = {
                "data": {
                    "entities": [
                        {
                            "id": "entity-1",
                            "entity_id": "switch.0xa4c13877facbdebd_l1",
                            "unique_id": "0xa4c13877facbdebd_switch_l1_z2m",
                        }
                    ]
                }
            }
            empty_registry = {"data": {"devices": []}}
            empty_entities = {"data": {"entities": []}}
            (live / ".storage" / "core.device_registry").write_text(json.dumps(live_device_registry))
            (live / ".storage" / "core.entity_registry").write_text(json.dumps(live_entity_registry))
            (source / ".storage" / "core.device_registry").write_text(json.dumps(empty_registry))
            (source / ".storage" / "core.entity_registry").write_text(json.dumps(empty_entities))

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

            joined = "\n".join(preview["warnings"])
            self.assertIn("core.device_registry devices", joined)
            self.assertIn("0xa4c13877facbdebd", joined)
            self.assertIn("core.entity_registry entities", joined)
            self.assertIn("switch.0xa4c13877facbdebd_l1", joined)
            self.assertNotIn("## Warnings", preview["diff"])

    def test_apply_preview_ignores_registry_hidden_only_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            live_registry = {"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "1"}]}}
            git_registry = {"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}}
            (live / ".storage" / "core.device_registry").write_text(json.dumps(live_registry))
            (source / ".storage" / "core.device_registry").write_text(json.dumps(git_registry))

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
            saved = json.loads((server.WORK_DIR / "apply-preview" / "homeassistant" / ".storage" / "core.device_registry").read_text())

            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "live-modified-at")
            self.assertFalse(preview["storage_changes"])
            self.assertEqual(preview["storage_change_paths"], [])
            self.assertIn("Target homeassistant: no file changes.", preview["diff"])
            self.assertNotIn("modified_at", preview["diff"])
            self.assertNotIn("git-modified-at", preview["diff"])

    def test_apply_preview_preserves_live_entity_registry_hidden_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / ".storage").mkdir(parents=True)
            (source / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "sensor.test",
                                    "modified_at": "live-modified-at",
                                    "platform": "mqtt",
                                    "suggested_object_id": "live_object",
                                    "supported_features": 2,
                                },
                                {
                                    "id": "entity-2",
                                    "entity_id": "sensor.phone",
                                    "modified_at": "live-phone-modified-at",
                                    "original_icon": "mdi:battery-90",
                                    "platform": "mobile_app",
                                    "supported_features": 2,
                                },
                            ]
                        }
                    }
                )
            )
            (source / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "sensor.test",
                                    "modified_at": "git-modified-at",
                                    "platform": "mqtt",
                                    "suggested_object_id": "git_object",
                                    "supported_features": 1,
                                },
                                {
                                    "id": "entity-2",
                                    "entity_id": "sensor.phone",
                                    "modified_at": "git-phone-modified-at",
                                    "original_icon": "mdi:battery-10",
                                    "platform": "mobile_app",
                                    "supported_features": 1,
                                },
                            ]
                        }
                    }
                )
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
            saved = json.loads((server.WORK_DIR / "apply-preview" / "homeassistant" / ".storage" / "core.entity_registry").read_text())

            self.assertFalse(preview["storage_changes"])
            self.assertEqual(preview["storage_change_paths"], [])
            self.assertIn("Target homeassistant: no file changes.", preview["diff"])
            self.assertEqual(saved["data"]["entities"][0]["modified_at"], "live-modified-at")
            self.assertEqual(saved["data"]["entities"][0]["suggested_object_id"], "live_object")
            self.assertEqual(saved["data"]["entities"][0]["supported_features"], 2)
            self.assertEqual(saved["data"]["entities"][1]["modified_at"], "live-phone-modified-at")
            self.assertEqual(saved["data"]["entities"][1]["original_icon"], "mdi:battery-90")
            self.assertEqual(saved["data"]["entities"][1]["supported_features"], 2)
            self.assertNotIn("modified_at", preview["diff"])
            self.assertNotIn("suggested_object_id", preview["diff"])
            self.assertNotIn("supported_features", preview["diff"])
            self.assertNotIn("original_icon", preview["diff"])
            self.assertNotIn("git_object", preview["diff"])
            self.assertNotIn("live_object", preview["diff"])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_apply_preview_organizer_diff_ignores_heap_order_rewrite(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            live_storage = live / ".storage"
            live_storage.mkdir(parents=True)
            live.joinpath("automations.yaml").write_text(
                "\n".join(
                    [
                        "- id: wardrobe_auto",
                        "  alias: Wardrobe Auto",
                        "- id: bathroom_auto",
                        "  alias: Bathroom Auto",
                        "",
                    ]
                )
            )
            live.joinpath("scripts.yaml").write_text("{}\n")
            live.joinpath("scenes.yaml").write_text("[]\n")
            (live_storage / "core.area_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "areas": [
                                {"id": "bathroom", "name": "Bathroom"},
                                {"id": "wardrobe", "name": "Wardrobe"},
                            ]
                        }
                    }
                )
            )
            (live_storage / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (live_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "automation.bathroom_auto",
                                    "unique_id": "bathroom_auto",
                                    "area_id": "bathroom",
                                },
                                {
                                    "entity_id": "automation.wardrobe_auto",
                                    "unique_id": "wardrobe_auto",
                                    "area_id": "wardrobe",
                                },
                            ]
                        }
                    }
                )
            )
            server.sync_logic.organizer.split_live_heaps_to_git(live, source, options={})

            preview = server.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                        "organizer": {"enabled": True},
                    }
                ]
            )

            self.assertIn("Target homeassistant: no file changes.", preview["diff"])
            self.assertNotIn("automations.yaml", preview["diff"])
            self.assertNotIn("wardrobe_auto", preview["diff"])
            self.assertNotIn("bathroom_auto", preview["diff"])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_apply_preview_organizer_diff_ignores_route_only_items(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            areas = source / ".ha-ops" / "areas"
            home = areas / "home"
            home.mkdir(parents=True)
            live.joinpath("automations.yaml").write_text(
                "\n".join(
                    [
                        "- id: battery_attention",
                        "  alias: Battery Attention",
                        "  trigger: []",
                        "  condition: []",
                        "  action:",
                        "  - service: script.battery_attention_scan",
                        "",
                    ]
                )
            )
            live.joinpath("scripts.yaml").write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention needed",
                        "",
                    ]
                )
            )
            live.joinpath("scenes.yaml").write_text("[]\n")
            (live / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.area_registry").write_text(
                json.dumps({"data": {"areas": [{"id": "home", "name": "Home"}]}})
            )
            (live / ".storage" / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (live / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "automation.battery_attention",
                                    "unique_id": "battery_attention",
                                },
                                {
                                    "entity_id": "script.battery_attention_scan",
                                    "unique_id": "battery_attention_scan",
                                },
                            ]
                        }
                    }
                )
            )
            (home / "automations.yaml").write_text((live / "automations.yaml").read_text())
            (home / "scripts.yaml").write_text((live / "scripts.yaml").read_text())
            (areas / "organizer-index.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 1, "ids": ["battery_attention"]},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    }
                )
            )
            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "delete": False,
                "organizer": {"enabled": True},
            }

            preview = server.build_apply_preview([target])

            self.assertIn("Target homeassistant: no file changes.", preview["diff"])
            self.assertEqual(preview["paths"], [])
            self.assertNotIn(".ha-ops/areas/.unknown/automations.yaml", preview["diff"])
            self.assertNotIn(".ha-ops/areas/.unknown/scripts.yaml", preview["diff"])
            self.assertNotIn(".ha-ops/areas/home/automations.yaml", preview["diff"])
            self.assertNotIn(".ha-ops/areas/home/scripts.yaml", preview["diff"])

            (home / "scripts.yaml").write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention changed",
                        "",
                    ]
                )
            )

            preview = server.build_apply_preview([target])

            self.assertIn(".ha-ops/areas/home/scripts.yaml", preview["diff"])
            self.assertIn("Battery attention changed", preview["diff"])
            self.assertIn("homeassistant/.ha-ops/areas/home/scripts.yaml", preview["paths"])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_apply_preview_organizer_diff_rejects_nested_heap_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            nested = source / ".ha-ops" / "areas" / "home" / "nested" / "automations.yaml"
            (live / "automations.yaml").write_text("[]\n")
            (live / "scripts.yaml").write_text("{}\n")
            (live / "scenes.yaml").write_text("[]\n")
            (live / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.area_registry").write_text(json.dumps({"data": {"areas": []}}))
            (live / ".storage" / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (live / ".storage" / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            nested.parent.mkdir(parents=True)
            nested.write_text(
                "\n".join(
                    [
                        "- id: battery_attention",
                        "  alias: Battery Attention",
                        "  trigger: []",
                        "  action: []",
                        "",
                    ]
                )
            )
            (source / ".ha-ops" / "areas" / "organizer-index.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 0, "ids": []},
                        "scenes": {"count": 0, "ids": []},
                    }
                )
            )
            target = {
                "id": "homeassistant",
                "type": "homeassistant",
                "source_path": str(source),
                "live_path": str(live),
                "delete": False,
                "organizer": {"enabled": True},
            }

            with self.assertRaisesRegex(RuntimeError, "unreferenced organizer file.*home/nested/automations.yaml"):
                server.build_apply_preview([target])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_apply_preview_organizer_diff_uses_git_organized_yaml_for_added_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live = server.CONFIG_DIR
            source = root / "repo" / "homeassistant"
            (live / "automations.yaml").write_text("[]\n")
            (live / "scripts.yaml").write_text("{}\n")
            (live / "scenes.yaml").write_text("[]\n")
            (live / ".storage").mkdir(parents=True)
            (live / ".storage" / "core.area_registry").write_text(json.dumps({"data": {"areas": []}}))
            (live / ".storage" / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (live / ".storage" / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            scripts = source / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            scripts.parent.mkdir(parents=True)
            (scripts.parent / "lighting-contract.md").write_text("# Contract\n")
            scripts.write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: battery_attention_scan",
                        "  sequence:",
                        "  - variables:",
                        "      current_silent_json: >-",
                        "        {%- set ns = namespace(items=[]) -%}",
                        "        {%- for item in states.sensor",
                        "            if item.entity_id.startswith('sensor.')",
                        "            and item.entity_id.endswith('_last_seen') -%}",
                        "          {{ item.entity_id }}",
                        "        {%- endfor -%}",
                        "        {{ ns.items | to_json }}",
                        "",
                    ]
                )
            )
            (source / ".ha-ops" / "areas" / "organizer-index.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    }
                )
            )

            preview = server.build_apply_preview(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(live),
                        "delete": False,
                        "organizer": {"enabled": True},
                    }
                ]
            )

            self.assertIn(".ha-ops/areas/home/scripts.yaml", preview["diff"])
            self.assertNotIn(".ha-ops/areas/.unknown/scripts.yaml", preview["diff"])
            self.assertNotIn("lighting-contract.md", preview["diff"])
            self.assertNotIn("lighting-contract.md", preview["paths"])
            self.assertIn("current_silent_json: >-", preview["diff"])
            self.assertNotIn('current_silent_json: "{%-', preview["diff"])
            self.assertNotIn("\\n", preview["diff"])

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

    def test_default_manifest_ignores_blocked_homeassistant_organizer_ui_preference(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertNotIn("organizer", manifest["targets"][0])

            server.write_state({"homeassistant_organizer_enabled": True})
            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertNotIn("organizer", manifest["targets"][0])

            server.set_homeassistant_organizer_enabled(False)
            manifest = server.default_manifest({"apply_path": "homeassistant"})
            self.assertFalse(manifest["targets"][0]["organizer"])

    def test_set_homeassistant_organizer_rejects_enabled_while_projection_is_blocked(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            with self.assertRaisesRegex(RuntimeError, "organizer area split is paused"):
                server.set_homeassistant_organizer_enabled(True)

            self.assertIsNone(server.read_state().get("homeassistant_organizer_enabled"))

    def test_homeassistant_organizer_control_is_disabled_while_projection_is_blocked(self):
        server = load_server()

        html = server.ui.render_homeassistant_organizer(True)

        self.assertIn("Area split organizer paused", html)
        self.assertIn(".ha-ops/areas projection is rewritten", html)
        self.assertIn("<input type='checkbox' name='homeassistant_organizer' value='1' disabled>", html)
        self.assertNotIn("checked", html)
        self.assertNotIn("Split automations, scripts, and scenes by area in Git", html)

    def test_loaded_manifest_ignores_stale_organizer_ui_preference(self):
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
                            }
                        ],
                    }
                )
            )

            server.write_state({"homeassistant_organizer_enabled": True})
            manifest, _path = server.load_manifest(repo, {"manifest_path": "ha-ops.json"}, [])
            self.assertNotIn("organizer", manifest["targets"][0])

    def test_loaded_manifest_keeps_organizer_until_disabled_ui_preference_is_set(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job())
            state = server.read_state()
            details = "\n".join(server.read_state()["last_details"])
            self.assertIn("Created commit", details)
            self.assertIn("Pushed to origin/main.", details)
            self.assertEqual(state["last_message"], "Save finished successfully and pushed to Git.")
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("homeassistant/configuration.yaml", result.stdout)

    def test_save_ha_to_git_uses_submitted_commit_subject(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self.prepare_empty_save_preview(server, root)
            self.assertTrue(
                server.run_save_job(commit_subject="Custom HA Save Subject"),
                server.read_state()["last_message"],
            )

            self.assertEqual(self.remote_main_subject(remote), "Custom HA Save Subject")

    def test_save_preview_writes_default_commit_subject_for_reactive_save_input(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server.context().release_now = lambda: "2026-06-24_17-00-00"
            self.prepare_empty_save_preview(server, root)

            self.assertEqual(
                server.read_state()["last_save_commit_subject"],
                "Save Home Assistant config 2026-06-24\u00a0•\u00a017-00-00",
            )

    def test_save_ha_to_git_keeps_committed_custom_subject_in_disabled_preview_input(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().release_now = lambda: "2026-06-24_20-00-00"
            remote = self.seed_remote(root)
            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            packages = server.CONFIG_DIR / "packages"
            packages.mkdir()
            (packages / "new.yaml").write_text("new:\n")
            (packages / "second.yaml").write_text("second:\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            preview_state = server.read_state()
            self.assertIn("homeassistant/packages/new.yaml", preview_state["last_save_preview_paths"])
            self.assertIn("homeassistant/packages/second.yaml", preview_state["last_save_preview_paths"])
            server.write_state({"save_preview_selected_paths": ["homeassistant/packages/new.yaml"]})
            self.assertTrue(
                server.run_save_job(commit_subject="Custom HA Save Subject"),
                server.read_state()["last_message"],
            )

            self.assertEqual(self.remote_main_subject(remote), "Custom HA Save Subject")
            state = server.read_state()
            self.assertEqual(state["last_save_commit_subject"], "Custom HA Save Subject")
            self.assertNotIn("homeassistant/packages/new.yaml", state["last_save_preview_paths"])
            self.assertIn("homeassistant/packages/second.yaml", state["last_save_preview_paths"])
            self.assertEqual(state["save_preview_selected_paths"], [])

            server.write_state({"save_preview_selected_paths": ["homeassistant/packages/second.yaml"]})
            selected_state = server.read_state()
            self.assertEqual(selected_state["last_save_commit_subject"], "Custom HA Save Subject")
            self.assertEqual(selected_state["save_preview_selected_paths"], ["homeassistant/packages/second.yaml"])

    def test_save_ha_to_git_recomputes_unchanged_default_commit_subject_at_job_start(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server.context().release_now = lambda: "2026-06-24_17-00-00"
            remote = self.prepare_empty_save_preview(server, root)
            rendered_default = "Save Home Assistant config 2026-06-24\u00a0•\u00a017-00-00"
            commit_subject = server.app_context.job_logic.save_commit_subject_from_submission(
                rendered_default,
                rendered_default,
            )
            self.assertIsNone(commit_subject)
            server.context().release_now = lambda: "2026-06-24_18-00-00"
            self.assertTrue(
                server.run_save_job(commit_subject=commit_subject),
                server.read_state()["last_message"],
            )

            self.assertEqual(
                self.remote_main_subject(remote),
                "Save Home Assistant config 2026-06-24\u00a0•\u00a018-00-00",
            )

    def test_save_ha_to_git_blank_commit_subject_falls_back_at_job_start(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server.context().release_now = lambda: "2026-06-24_19-00-00"
            remote = self.prepare_empty_save_preview(server, root)
            self.assertTrue(
                server.run_save_job(commit_subject=" \n\t "),
                server.read_state()["last_message"],
            )

            self.assertEqual(
                self.remote_main_subject(remote),
                "Save Home Assistant config 2026-06-24\u00a0•\u00a019-00-00",
            )

    @unittest.skip("enabled .ha-ops/areas projection is paused")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertIn("homeassistant/.ha-ops/areas/home/automations.yaml", result.stdout)
            self.assertNotIn("homeassistant/automations.yaml", result.stdout)

    def test_disabled_organizer_save_heap_view_then_apply_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            self.git(["init", "--bare", str(remote)], root)
            self.write_heap_yaml_set(server.CONFIG_DIR, "Live")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None
            server.set_homeassistant_organizer_enabled(False)

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            result = self.git(["--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], root)

            self.assertIn("homeassistant/configuration.yaml", result.stdout)
            self.assertIn("homeassistant/automations.yaml", result.stdout)
            self.assertIn("homeassistant/scripts.yaml", result.stdout)
            self.assertIn("homeassistant/scenes.yaml", result.stdout)
            self.assertNotIn("homeassistant/.ha-ops/areas", result.stdout)

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("no file changes", state["last_diff"].lower())
            self.assertEqual(state["last_preview_paths"], [])

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("no file changes", state["last_diff"].lower())
            self.assertEqual(state["last_preview_paths"], [])
            self.assertEqual((server.CONFIG_DIR / "automations.yaml").read_text(), self.remote_file(remote, "homeassistant/automations.yaml"))
            self.assertFalse((server.CONFIG_DIR / ".ha-ops" / "areas").exists())

    def test_disabled_organizer_apply_heap_view_then_save_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            self.write_heap_yaml_set(seed / "homeassistant", "Git")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)
            self.write_stale_organizer_view(server.CONFIG_DIR)
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "restart_after_apply": False,
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None
            server.set_homeassistant_organizer_enabled(False)

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            self.assertEqual(
                set(server.read_state()["last_preview_paths"]),
                {
                    "homeassistant/automations.yaml",
                    "homeassistant/configuration.yaml",
                    "homeassistant/scenes.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.select_all_apply_preview_files(server)
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])

            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), self.remote_file(remote, "homeassistant/configuration.yaml"))
            self.assertEqual((server.CONFIG_DIR / "automations.yaml").read_text(), self.remote_file(remote, "homeassistant/automations.yaml"))
            self.assertEqual((server.CONFIG_DIR / "scripts.yaml").read_text(), self.remote_file(remote, "homeassistant/scripts.yaml"))
            self.assertEqual((server.CONFIG_DIR / "scenes.yaml").read_text(), self.remote_file(remote, "homeassistant/scenes.yaml"))
            self.assertFalse((server.CONFIG_DIR / ".ha-ops" / "areas").exists())

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("no file changes", state["last_diff"].lower())
            self.assertEqual(state["last_preview_paths"], [])

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("no save changes", state["last_save_preview"].lower())
            self.assertEqual(state["last_save_preview_paths"], [])
            result = self.git(["--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], root)
            self.assertNotIn("homeassistant/.ha-ops/areas", result.stdout)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_preserves_organizer_contract_docs(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            area = seed / "homeassistant" / ".ha-ops" / "areas" / "dining_room"
            area.mkdir(parents=True)
            (area / "lighting-contract.md").write_text("# Contract\n")
            (area / "automations.yaml").write_text("- id: live_auto\n  alias: Live Auto\n")
            index = seed / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 1, "ids": ["live_auto"]},
                        "scripts": {"count": 0, "ids": []},
                        "scenes": {"count": 0, "ids": []},
                    }
                )
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            (server.CONFIG_DIR / "configuration.yaml").write_text("homeassistant:\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("- id: live_auto\n  alias: Live Auto\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text("{}\n")
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.area_registry").write_text(json.dumps({"data": {"areas": []}}))
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()

            self.assertNotIn("lighting-contract.md", state["last_save_preview"])
            self.assertNotIn("lighting-contract.md", state["last_save_diff"])
            repo_doc = server.DATA_DIR / "ha-config" / "homeassistant" / ".ha-ops" / "areas" / "dining_room" / "lighting-contract.md"
            self.assertEqual(repo_doc.read_text(), "# Contract\n")

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
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/configuration.yaml"])
            details = "\n".join(state["last_details"])
            self.assertIn("Save export candidates for homeassistant (1):", details)
            self.assertIn("- homeassistant/configuration.yaml", details)
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")
            page = server.render_page()
            self.assertIn('<div class="badge " data-status-code="warning" data-testid="status-badge">warning</div>', page)
            self.assertNotIn('<div class="badge error">error</div>', page)

    def test_save_preview_save_all_uses_preview_approval(self):
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertFalse(server.run_save_job())
            self.assertIn("Select at least one preview file", server.read_state()["last_message"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

            server.write_state({"save_preview_selected_paths": ["homeassistant/configuration.yaml"]})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertNotEqual(state["last_status"], "conflicts")
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")

    def test_save_preview_per_file_choice_keeps_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text("git-automations\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.git(["branch", "ha-ops/ha-live"], seed)
            self.git(["branch", "ha-ops/base"], seed)
            self.git(["push", "origin", "ha-ops/ha-live", "ha-ops/base"], seed)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha-config\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("ha-automations\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            server.write_state(
                {
                    "save_preview_selected_paths": ["homeassistant/automations.yaml"],
                    "save_preview_resolutions": {
                        "homeassistant/configuration.yaml": "ha",
                        "homeassistant/automations.yaml": "ha",
                    }
                }
            )

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertIn("homeassistant/configuration.yaml", state["last_save_preview"])
            self.assertNotIn("homeassistant/automations.yaml", state["last_save_preview"])
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git-config\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/automations.yaml"), "ha-automations\n")

    def test_partial_save_keeps_unselected_files_in_later_save_preview(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text("git-automations\n")
            (seed / "homeassistant" / "scripts.yaml").write_text("git-scripts\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha-config\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("ha-automations\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text("ha-scripts\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.assertEqual(
                set(server.read_state()["last_save_preview_paths"]),
                {
                    "homeassistant/automations.yaml",
                    "homeassistant/configuration.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            server.write_state({"save_preview_selected_paths": ["homeassistant/automations.yaml"]})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            first_save_parents = self.remote_parents(remote, "main")
            self.assertEqual(len(first_save_parents), 1)
            self.assertEqual(self.remote_file(remote, "homeassistant/automations.yaml"), "ha-automations\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git-config\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/scripts.yaml"), "git-scripts\n")
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/configuration.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.assertNotIn("homeassistant/automations.yaml", state["last_save_preview"])
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/configuration.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.assertNotIn("homeassistant/automations.yaml", state["last_save_preview"])
            server.write_state(
                {
                    "save_preview_selected_paths": [
                        "homeassistant/configuration.yaml",
                        "homeassistant/scripts.yaml",
                    ]
                }
            )
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(server.read_state()["last_save_preview"], "No Save changes.")
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha-config\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/scripts.yaml"), "ha-scripts\n")

    def test_reset_git_state_recovers_preview_after_old_partial_save_merge(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text("git-automations\n")
            (seed / "homeassistant" / "scripts.yaml").write_text("git-scripts\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            self.git(["checkout", "-B", "ha-ops/ha-live", "main"], seed)
            (seed / "homeassistant" / "configuration.yaml").write_text("ha-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text("ha-automations\n")
            (seed / "homeassistant" / "scripts.yaml").write_text("ha-scripts\n")
            self.git_commit_all(seed, "live export")
            self.git(["branch", "-f", "ha-ops/base", "main"], seed)
            self.git(["push", "origin", "ha-ops/ha-live", "ha-ops/base"], seed)

            self.git(["checkout", "main"], seed)
            self.git(["merge", "--no-commit", "--no-ff", "ha-ops/ha-live"], seed)
            self.git(["checkout", "HEAD", "--", "homeassistant/configuration.yaml", "homeassistant/scripts.yaml"], seed)
            self.git_commit_all(seed, "old partial save")
            self.git(["push", "origin", "main"], seed)
            bad_live = self.remote_rev(remote, "ha-ops/ha-live")
            self.assertIn(bad_live, self.remote_parents(remote, "main"))

            (server.CONFIG_DIR / "configuration.yaml").write_text("ha-config\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("ha-automations\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text("ha-scripts\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.assertEqual(server.read_state()["last_save_preview"], "No Save changes.")

            self.assertTrue(server.run_reset_git_state_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_status"], "success")
            self.assertEqual(state["last_message"], "Git state reset finished successfully.")
            self.assertEqual(state["last_save_preview"], "")
            reset_live = self.remote_rev(remote, "ha-ops/ha-live")
            self.assertNotEqual(reset_live, bad_live)
            self.assertEqual(self.remote_rev(remote, "main"), self.remote_rev(remote, "ha-ops/base"))

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/configuration.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.assertNotIn("homeassistant/automations.yaml", state["last_save_preview"])

    def test_ha_to_git_merge_preserves_git_only_battery_attention_addition(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            battery = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            battery.parent.mkdir(parents=True)
            battery.write_text("battery_attention_scan:\n  alias: battery_attention_scan\n")
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.assertEqual(server.read_state()["last_save_preview"], "No Save changes.")
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(
                self.remote_file(remote, "homeassistant/.ha-ops/areas/home/scripts.yaml"),
                "battery_attention_scan:\n  alias: battery_attention_scan\n",
            )

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_diff_ignores_route_only_battery_attention(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            battery = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            battery.parent.mkdir(parents=True)
            battery.write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention needed",
                        "",
                    ]
                )
            )
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(battery.read_text())
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertIn("homeassistant/configuration.yaml", state["last_save_preview"])
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview"])
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", state["last_save_preview"])
            self.assertNotIn(".ha-ops/areas/.unknown/scripts.yaml", state["last_save_diff"])
            self.assertNotIn(".ha-ops/areas/home/scripts.yaml", state["last_save_diff"])

            server.write_state({"save_preview_selected_paths": ["homeassistant/configuration.yaml"]})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/.ha-ops/areas/home/scripts.yaml"), battery.read_text())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", result.stdout)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_empty_save_preview_organizer_route_only_battery_attention_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            battery = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            battery.parent.mkdir(parents=True)
            battery.write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention needed",
                        "",
                    ]
                )
            )
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(battery.read_text())
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_preview_paths"], [])
            before_save = self.remote_rev(remote, "main")

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            self.assertEqual(self.remote_rev(remote, "main"), before_save)
            self.assertEqual(self.remote_file(remote, "homeassistant/.ha-ops/areas/home/scripts.yaml"), battery.read_text())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", result.stdout)
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertEqual(state["save_preview_selected_paths"], [])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_mixed_home_file_route_only_move_is_noop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            battery_script = "\n".join(
                [
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                ]
            )
            home_script = "\n".join(
                [
                    "  alias: Home Script",
                    "  sequence:",
                    "  - service: logbook.log",
                    "    data:",
                    "      message: home",
                ]
            )
            scripts = "\n".join(
                [
                    "battery_attention_scan:",
                    battery_script,
                    "home_script:",
                    home_script,
                    "",
                ]
            )

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            area_registry = json.dumps({"data": {"areas": [{"id": "home", "name": "Home"}]}})
            device_registry = json.dumps({"data": {"devices": []}})
            entity_registry = json.dumps(
                {
                    "data": {
                        "entities": [
                            {
                                "entity_id": "script.battery_attention_scan",
                                "unique_id": "battery_attention_scan",
                            },
                            {
                                "entity_id": "script.home_script",
                                "unique_id": "home_script",
                                "area_id": "home",
                            },
                        ]
                    }
                }
            )
            home_scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            home_scripts.parent.mkdir(parents=True)
            home_scripts.write_text(scripts)
            repo_storage = updater / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            (repo_storage / "core.area_registry").write_text(area_registry)
            (repo_storage / "core.device_registry").write_text(device_registry)
            (repo_storage / "core.entity_registry").write_text(entity_registry)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 2, "ids": ["battery_attention_scan", "home_script"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add home scripts")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.area_registry").write_text(area_registry)
            (live_storage / "core.device_registry").write_text(device_registry)
            (live_storage / "core.entity_registry").write_text(entity_registry)
            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(scripts)
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_diff"])
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", state["last_save_diff"])
            before_save = self.remote_rev(remote, "main")

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            self.assertEqual(self.remote_rev(remote, "main"), before_save)
            self.assertEqual(self.remote_file(remote, "homeassistant/.ha-ops/areas/home/scripts.yaml"), scripts)
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", result.stdout)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_real_addition_does_not_duplicate_route_only_item(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            battery_script = "\n".join(
                [
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                ]
            )
            new_script = "\n".join(
                [
                    "  alias: New Script",
                    "  sequence:",
                    "  - service: logbook.log",
                    "    data:",
                    "      message: new",
                ]
            )
            git_scripts = "\n".join(["battery_attention_scan:", battery_script, ""])
            live_scripts = "\n".join(
                [
                    "battery_attention_scan:",
                    battery_script,
                    "new_script:",
                    new_script,
                    "",
                ]
            )

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            home_scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            home_scripts.parent.mkdir(parents=True)
            home_scripts.write_text(git_scripts)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(live_scripts)
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/.ha-ops/areas/.unknown/scripts.yaml",
                    "homeassistant/.ha-ops/areas/organizer-index.json",
                },
            )
            self.assertIn("- Modified: homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview"])
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", state["last_save_preview"])
            self.assertIn("+new_script:", state["last_save_diff"])
            self.assertNotIn("+battery_attention_scan:", state["last_save_diff"])

            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", result.stdout)
            saved_scripts = self.remote_file(remote, "homeassistant/.ha-ops/areas/.unknown/scripts.yaml")
            self.assertEqual(saved_scripts.count("battery_attention_scan:"), 1)
            self.assertIn("new_script:", saved_scripts)
            saved_index = json.loads(self.remote_file(remote, "homeassistant/.ha-ops/areas/organizer-index.json"))
            self.assertEqual(saved_index["scripts"], {"count": 2, "ids": ["battery_attention_scan", "new_script"]})

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_include_redundant_data_hides_route_only_battery_attention(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            battery = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            battery.parent.mkdir(parents=True)
            battery.write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention needed",
                        "",
                    ]
                )
            )
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            repo_storage = updater / "homeassistant" / ".storage"
            repo_storage.mkdir(parents=True)
            repo_registry = {"data": {"devices": [{"id": "device-1", "modified_at": "git-modified-at", "sw_version": "1"}]}}
            live_registry = {"data": {"devices": [{"id": "device-1", "modified_at": "live-modified-at", "sw_version": "1"}]}}
            (repo_storage / "core.device_registry").write_text(json.dumps(repo_registry))
            self.git_commit_all(updater, "add battery attention and registry")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(json.dumps(live_registry))
            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(battery.read_text())
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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
            server.write_state({"include_redundant_data": True})

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/.storage/core.device_registry"])
            self.assertIn("homeassistant/.storage/core.device_registry", state["last_save_preview"])
            self.assertIn("modified_at", state["last_save_diff"])
            self.assertIn("git-modified-at", state["last_save_diff"])
            self.assertIn("live-modified-at", state["last_save_diff"])
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview"])
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", state["last_save_preview"])
            self.assertNotIn(".ha-ops/areas/.unknown/scripts.yaml", state["last_save_diff"])
            self.assertNotIn(".ha-ops/areas/home/scripts.yaml", state["last_save_diff"])

            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(json.loads(self.remote_file(remote, "homeassistant/.storage/core.device_registry")), live_registry)
            self.assertEqual(self.remote_file(remote, "homeassistant/.ha-ops/areas/home/scripts.yaml"), battery.read_text())
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", result.stdout)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_mixed_route_only_item_and_real_deletion_preserves_live_item(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            battery_script = "\n".join(
                [
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                ]
            )
            old_script = "\n".join(
                [
                    "  alias: Old Script",
                    "  sequence:",
                    "  - service: logbook.log",
                    "    data:",
                    "      message: old",
                ]
            )
            git_scripts = "\n".join(
                [
                    "battery_attention_scan:",
                    battery_script,
                    "old_script:",
                    old_script,
                    "",
                ]
            )
            live_scripts = "\n".join(["battery_attention_scan:", battery_script, ""])

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            home_scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            home_scripts.parent.mkdir(parents=True)
            home_scripts.write_text(git_scripts)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 2, "ids": ["battery_attention_scan", "old_script"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add scripts")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(live_scripts)
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/.ha-ops/areas/home/scripts.yaml",
                    "homeassistant/.ha-ops/areas/organizer-index.json",
                },
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview"])
            self.assertIn("old_script", state["last_save_diff"])
            self.assertIn("-old_script:", state["last_save_diff"])
            self.assertNotIn("-battery_attention_scan:", state["last_save_diff"])
            self.assertNotIn("-  alias: Battery Attention Scan", state["last_save_diff"])

            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", result.stdout)
            saved_scripts = self.remote_file(remote, "homeassistant/.ha-ops/areas/.unknown/scripts.yaml")
            self.assertIn("battery_attention_scan", saved_scripts)
            self.assertNotIn("old_script", saved_scripts)
            saved_index = json.loads(self.remote_file(remote, "homeassistant/.ha-ops/areas/organizer-index.json"))
            self.assertEqual(saved_index["scripts"], {"count": 1, "ids": ["battery_attention_scan"]})

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_selected_file_keeps_unchecked_index_at_git(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")

            battery_script = "\n".join(
                [
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                ]
            )
            old_script = "\n".join(
                [
                    "  alias: Old Script",
                    "  sequence:",
                    "  - service: logbook.log",
                    "    data:",
                    "      message: old",
                ]
            )
            git_scripts = "\n".join(
                [
                    "battery_attention_scan:",
                    battery_script,
                    "old_script:",
                    old_script,
                    "",
                ]
            )
            live_scripts = "\n".join(["battery_attention_scan:", battery_script, ""])

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            home_scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            home_scripts.parent.mkdir(parents=True)
            home_scripts.write_text(git_scripts)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            git_index = (
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 2, "ids": ["battery_attention_scan", "old_script"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            index.write_text(git_index)
            self.git_commit_all(updater, "add scripts")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(live_scripts)
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {
                    "homeassistant/.ha-ops/areas/home/scripts.yaml",
                    "homeassistant/.ha-ops/areas/organizer-index.json",
                },
            )

            server.write_state({"save_preview_selected_paths": ["homeassistant/.ha-ops/areas/home/scripts.yaml"]})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", result.stdout)
            saved_scripts = self.remote_file(remote, "homeassistant/.ha-ops/areas/.unknown/scripts.yaml")
            self.assertIn("battery_attention_scan", saved_scripts)
            self.assertNotIn("old_script", saved_scripts)
            self.assertEqual(
                self.remote_file(remote, "homeassistant/.ha-ops/areas/organizer-index.json"),
                git_index,
            )
            state = server.read_state()
            self.assertEqual(state["last_status"], "success")
            self.assertEqual(state["save_preview_selected_paths"], [])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_organizer_diff_keeps_changed_battery_attention_payload(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            scripts.parent.mkdir(parents=True)
            scripts.write_text(
                "\n".join(
                    [
                        "battery_attention_scan:",
                        "  alias: Battery Attention Scan",
                        "  sequence:",
                        "  - service: notify.mobile_app",
                        "    data:",
                        "      message: Battery attention needed",
                        "",
                    ]
                )
            )
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(
                scripts.read_text().replace("Battery attention needed", "Battery attention changed")
            )
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview_paths"])
            self.assertIn("Battery attention changed", state["last_save_diff"])

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_modified_route_only_battery_attention_removes_old_route(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")

            git_script = "\n".join(
                [
                    "battery_attention_scan:",
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                    "",
                ]
            )
            live_script = git_script.replace("Battery attention needed", "Battery attention changed")

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            scripts.parent.mkdir(parents=True)
            scripts.write_text(git_script)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)
            self.push_service_branches(updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text(live_script)
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", state["last_save_preview_paths"])
            self.assertIn("Battery attention changed", state["last_save_diff"])

            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertNotIn("homeassistant/.ha-ops/areas/home/scripts.yaml", result.stdout)
            self.assertIn("homeassistant/.ha-ops/areas/.unknown/scripts.yaml", result.stdout)
            saved_scripts = self.remote_file(remote, "homeassistant/.ha-ops/areas/.unknown/scripts.yaml")
            self.assertEqual(saved_scripts.count("battery_attention_scan:"), 1)
            self.assertIn("Battery attention changed", saved_scripts)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_save_preview_stale_service_branch_conflicted_organizer_index_does_not_crash(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")

            battery = "\n".join(
                [
                    "battery_attention_scan:",
                    "  alias: Battery Attention Scan",
                    "  sequence:",
                    "  - service: notify.mobile_app",
                    "    data:",
                    "      message: Battery attention needed",
                    "",
                ]
            )

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            scripts = updater / "homeassistant" / ".ha-ops" / "areas" / "home" / "scripts.yaml"
            scripts.parent.mkdir(parents=True)
            scripts.write_text(battery)
            index = updater / "homeassistant" / ".ha-ops" / "areas" / "organizer-index.json"
            index.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 0, "ids": []},
                        "scripts": {"count": 1, "ids": ["battery_attention_scan"]},
                        "scenes": {"count": 0, "ids": []},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.git_commit_all(updater, "add battery attention")
            self.git(["push", "origin", "main"], updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("base\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("[]\n")
            (server.CONFIG_DIR / "scripts.yaml").write_text("{}\n")
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertNotEqual(state["last_status"], "error")
            self.assertTrue(state["last_save_preview_conflicts"])
            self.assertIn("homeassistant/.ha-ops/areas/organizer-index.json", state["last_save_preview_paths"])
            self.assertIn("homeassistant/.ha-ops/areas/organizer-index.json", state["last_save_diff"])
            self.assertIn("Save preview conflicts", state["last_save_preview"])
            self.assertNotIn("JSONDecodeError", state["last_message"])

    def test_save_without_matching_preview_rebuilds_preview_and_warns(self):
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
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            self.assertIn("- Modified: homeassistant/configuration.yaml", state["last_save_preview"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_save_without_matching_preview_succeeds_when_rebuilt_preview_is_empty(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("git\n")
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
                    "last_save_preview": "stale save preview",
                    "last_save_diff": "stale save diff",
                    "last_save_preview_commit": "stale-save-commit",
                    "last_save_preview_fingerprint": "stale-save-fingerprint",
                    "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                    "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                    "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                }
            )

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_status"], "success")
            self.assertEqual(state["last_message"], "No live Home Assistant changes to save.")
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_diff"], "")
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertEqual(state["save_preview_resolutions"], {})
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertNotIn("State changed since this preview was created", "\n".join(state["last_details"]))
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_save_error_before_state_read_is_reported(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            original_read_state = server._CTX.read_state

            def fail_read_state():
                raise RuntimeError("state read failed")

            server._CTX.read_state = fail_read_state
            try:
                self.assertFalse(server.run_save_job())
            finally:
                server._CTX.read_state = original_read_state

            state = server.read_state()
            self.assertEqual(state["last_status"], "error")
            self.assertEqual(state["last_action"], "save")
            self.assertEqual(state["last_message"], "state read failed")
            self.assertFalse(state.get("save_push_retry_pending", False))

    def test_save_preview_bootstraps_existing_repo_without_live_branch(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            path = seed / "homeassistant" / "configuration.yaml"
            path.parent.mkdir(parents=True)
            path.write_text("git\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("- Modified: homeassistant/configuration.yaml", state["last_save_preview"])
            refs = subprocess.run(
                ["git", "--git-dir", str(remote), "show-ref", "--heads", "ha-ops/ha-live"],
                text=True,
                capture_output=True,
            )
            self.assertEqual(refs.returncode, 0, refs.stderr)

    def test_save_use_git_noop_aborts_merge(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            server.write_state({"save_preview_resolutions": {"homeassistant/configuration.yaml": "git"}})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            repo = server.DATA_DIR / "ha-config"
            self.assertFalse(self.merge_head_exists(repo))
            self.assertEqual(self.repo_status(repo), "")
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_save_same_content_divergent_merge_creates_merge_commit(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("same\n")
            self.git_commit_all(updater, "main same")
            self.git(["push", "origin", "main"], updater)
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.assertEqual(server.read_state()["last_save_preview"], "No Save changes.")
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            parents = self.remote_parents(remote, "main")
            self.assertEqual(len(parents), 2)
            self.assertEqual(self.remote_rev(remote, "ha-ops/base"), self.remote_rev(remote, "ha-ops/ha-live"))

    def test_save_preview_conflict_rejects_stale_live_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha1\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha2\n")
            server.write_state({"save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"}})

            self.assertFalse(server.run_save_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "git\n")

    def test_save_conflict_preview_includes_clean_merge_changes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            clean_live = server.CONFIG_DIR / "packages" / "clean.yaml"
            clean_live.parent.mkdir(parents=True)
            clean_live.write_text("ha-clean-1\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("Save preview conflicts (1):", state["last_save_preview"])
            self.assertIn("homeassistant/packages/clean.yaml", state["last_save_diff"])
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {"homeassistant/configuration.yaml", "homeassistant/packages/clean.yaml"},
            )
            self.assertEqual(state["last_save_preview_conflict_paths"], ["homeassistant/configuration.yaml"])
            clean_live.write_text("ha-clean-2\n")
            server.write_state({"save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"}})

            self.assertFalse(server.run_save_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "main:homeassistant/packages/clean.yaml"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_save_preview_lovelace_storage_conflict_diff_uses_git_stages(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            lovelace = seed / "homeassistant" / ".storage" / "lovelace.lovelace"
            lovelace.parent.mkdir(parents=True)
            lovelace.write_text(
                json.dumps(
                    {
                        "data": {
                            "config": {
                                "cards": [
                                    {
                                        "type": "custom:mushroom-template-card",
                                        "icon": "mdi:shoe-sneaker",
                                        "icon_color": "blue",
                                        "primary": "Keep",
                                        "secondary": "Light",
                                        "tap_action": {"action": "toggle"},
                                    }
                                ]
                            }
                        }
                    },
                    indent=2,
                )
                + "\n"
            )
            self.git_commit_all(seed, "base lovelace")
            self.git(["push", "origin", "main"], seed)
            self.git(["branch", "-f", "ha-ops/ha-live", "HEAD"], seed)
            self.git(["branch", "-f", "ha-ops/base", "HEAD"], seed)
            self.push_service_branches(seed)

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            git_lovelace = updater / "homeassistant" / ".storage" / "lovelace.lovelace"
            git_lovelace.write_text(
                json.dumps(
                    {
                        "data": {
                            "config": {
                                "cards": [
                                    {
                                        "type": "custom:mushroom-template-card",
                                        "icon": "mdi:shoe-sneaker",
                                        "icon_color": "grey",
                                        "primary": "Keep",
                                        "secondary": "Light",
                                        "tap_action": {"action": "none"},
                                    }
                                ]
                            }
                        }
                    },
                    indent=2,
                )
                + "\n"
            )
            self.git_commit_all(updater, "git lovelace")
            self.git(["push", "origin", "main"], updater)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "lovelace.lovelace").write_text(
                json.dumps(
                    {
                        "data": {
                            "config": {
                                "cards": [
                                    {
                                        "type": "custom:mushroom-template-card",
                                        "icon": "mdi:shoe-sneaker",
                                        "icon_color": "{% if is_state(\"input_boolean.hallway_keep_light_on\", \"on\") %}\n  orange\n{% else %}\n  grey\n{% endif %}\n",
                                        "primary": "Keep",
                                        "secondary": "Light",
                                        "tap_action": {
                                            "action": "call-service",
                                            "service": "script.hallway_toggle_light_no_timeout",
                                        },
                                    }
                                ]
                            }
                        }
                    },
                    indent=2,
                )
                + "\n"
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_conflict_paths"], ["homeassistant/.storage/lovelace.lovelace"])
            self.assertIn("homeassistant/.storage/lovelace.lovelace", state["last_save_diff"])
            self.assertNotIn("<<<<<<<", state["last_save_diff"])
            self.assertNotIn("=======", state["last_save_diff"])
            self.assertNotIn(">>>>>>>", state["last_save_diff"])
            self.assertIn('"action": "none"', state["last_save_diff"])
            self.assertIn('"action": "call-service"', state["last_save_diff"])
            self.assertIn("script.hallway_toggle_light_no_timeout", state["last_save_diff"])

    def test_save_preview_conflict_skips_unselected_clean_merge_change(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            clean_live = server.CONFIG_DIR / "packages" / "clean.yaml"
            clean_live.parent.mkdir(parents=True)
            clean_live.write_text("ha-clean\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_save_preview_paths"]),
                {"homeassistant/configuration.yaml", "homeassistant/packages/clean.yaml"},
            )
            server.write_state(
                {
                    "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                    "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                }
            )

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "ha\n")
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "main:homeassistant/packages/clean.yaml"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_save_preview_modify_delete_conflict_can_keep_git_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            package = seed / "homeassistant" / "packages" / "a.yaml"
            package.parent.mkdir(parents=True)
            package.write_text("base\n")
            self.git_commit_all(seed, "base package")
            self.git(["push", "origin", "main"], seed)
            self.git(["branch", "-f", "ha-ops/ha-live", "HEAD"], seed)
            self.git(["branch", "-f", "ha-ops/base", "HEAD"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "packages" / "a.yaml").unlink()
            self.git_commit_all(updater, "delete config")
            self.git(["push", "origin", "main"], updater)
            live_package = server.CONFIG_DIR / "packages" / "a.yaml"
            live_package.parent.mkdir(parents=True)
            live_package.write_text("ha\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            self.assertFalse(server.run_save_job())
            self.assertIn("Choose HA or Git version", server.read_state()["last_message"])
            server.write_state({"save_preview_resolutions": {"homeassistant/packages/a.yaml": "git"}})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "main:homeassistant/packages/a.yaml"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_save_unknown_base_registry_conflict_diff_hides_noise(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            registry = seed / "homeassistant" / ".storage" / "core.device_registry"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "git-modified-at",
                                    "sw_version": "1",
                                }
                            ]
                        }
                    }
                )
            )
            (seed / "homeassistant" / "configuration.yaml").unlink()
            self.git_commit_all(seed, "registry")
            self.git(["push", "origin", "main"], seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "name": "Live Device",
                                    "modified_at": "live-modified-at",
                                    "sw_version": "2",
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
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            diff = server.read_state()["last_save_diff"]
            self.assertIn("homeassistant/.storage/core.device_registry", diff)
            self.assertNotIn("sw_version", diff)
            self.assertNotIn("modified_at", diff)
            self.assertNotIn("git-modified-at", diff)
            self.assertNotIn("live-modified-at", diff)

    def test_save_unknown_base_entity_registry_conflict_diff_hides_hidden_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            registry = seed / "homeassistant" / ".storage" / "core.entity_registry"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "sensor.test",
                                    "modified_at": "git-modified-at",
                                    "platform": "mqtt",
                                    "suggested_object_id": "git_object",
                                    "supported_features": 1,
                                    "original_name": "Git Name",
                                }
                            ]
                        }
                    }
                )
            )
            (seed / "homeassistant" / "configuration.yaml").unlink()
            self.git_commit_all(seed, "registry")
            self.git(["push", "origin", "main"], seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "sensor.test",
                                    "modified_at": "live-modified-at",
                                    "platform": "mqtt",
                                    "suggested_object_id": "live_object",
                                    "supported_features": 2,
                                    "original_name": "Live Name",
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
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            diff = server.read_state()["last_save_diff"]
            self.assertIn("homeassistant/.storage/core.entity_registry", diff)
            self.assertNotIn("supported_features", diff)
            self.assertNotIn("modified_at", diff)
            self.assertNotIn("suggested_object_id", diff)
            self.assertNotIn("git_object", diff)
            self.assertNotIn("live_object", diff)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.assertIn("Save export candidates for homeassistant (1):", "\n".join(server.read_state()["last_details"]))
            self.select_all_save_preview_files(server)
            server.write_state({"save_preview_resolutions": {"homeassistant/configuration.yaml": "git"}})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            server.write_state({"save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"}})
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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
            self.push_service_branches(seed)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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
            self.push_service_branches(seed)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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

    def test_save_preview_ignores_repo_only_service_branch_files(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir()
            (seed / "homeassistant" / "scripts.yaml").write_text("old_script:\n  sequence: []\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            self.git(["checkout", "-B", "ha-ops/ha-live"], seed)
            (seed / "homeassistant" / "scripts.yaml").write_text("new_script:\n  sequence: []\n")
            (seed / "tests").mkdir()
            (seed / "tests" / "test_battery_attention_markdown_v2.py").write_text("def test_contract():\n    pass\n")
            self.git_commit_all(seed, "live service branch")
            self.git(["push", "-u", "origin", "ha-ops/ha-live"], seed)
            self.git(["branch", "-f", "ha-ops/base", "main"], seed)
            self.git(["push", "-u", "origin", "ha-ops/base"], seed)

            (server.CONFIG_DIR / "scripts.yaml").write_text("new_script:\n  sequence: []\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/scripts.yaml"])
            self.assertIn("homeassistant/scripts.yaml", state["last_save_preview"])
            self.assertNotIn("tests/test_battery_attention_markdown_v2.py", state["last_save_preview"])
            self.assertNotIn("tests/test_battery_attention_markdown_v2.py", state.get("last_save_preview_suppressed_paths", []))

            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("homeassistant/scripts.yaml", result.stdout)
            self.assertNotIn("tests/test_battery_attention_markdown_v2.py", result.stdout)

    def test_save_preview_ignores_repo_only_service_branch_conflicts(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir()
            (seed / "homeassistant" / "scripts.yaml").write_text("old_script:\n  sequence: []\n")
            (seed / "tests").mkdir()
            test_path = seed / "tests" / "test_battery_attention_markdown_v2.py"
            test_path.write_text("def test_contract():\n    return 'base'\n")
            self.git_commit_all(seed, "base")
            base = self.git(["rev-parse", "HEAD"], seed).stdout.strip()
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)

            self.git(["checkout", "-B", "ha-ops/ha-live", base], seed)
            (seed / "homeassistant" / "scripts.yaml").write_text("new_script:\n  sequence: []\n")
            test_path.write_text("def test_contract():\n    return 'live'\n")
            self.git_commit_all(seed, "live service branch")
            self.git(["push", "-u", "origin", "ha-ops/ha-live"], seed)
            self.git(["branch", "-f", "ha-ops/base", base], seed)
            self.git(["push", "-u", "origin", "ha-ops/base"], seed)

            self.git(["checkout", "-B", "main", base], seed)
            test_path.write_text("def test_contract():\n    return 'main'\n")
            self.git_commit_all(seed, "main repo-only test change")
            self.git(["push", "origin", "main"], seed)

            (server.CONFIG_DIR / "scripts.yaml").write_text("new_script:\n  sequence: []\n")
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertFalse(state["last_save_preview_conflicts"])
            self.assertEqual(state["last_save_preview_conflict_paths"], [])
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/scripts.yaml"])
            self.assertIn("homeassistant/scripts.yaml", state["last_save_preview"])
            self.assertNotIn("tests/test_battery_attention_markdown_v2.py", state["last_save_preview"])
            self.assertNotIn(
                "tests/test_battery_attention_markdown_v2.py",
                json.dumps(state.get("last_save_preview_fingerprint", "")),
            )

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
            self.assertTrue(server.read_state()["post_apply_save_recommended"])

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
            (server.CONFIG_DIR / ".storage").mkdir(parents=True)
            (server.CONFIG_DIR / ".storage" / "input_boolean").write_text("live-storage\n")
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
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")

    def test_apply_completes_entity_registry_missing_required_fields_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage = source / ".storage"
            live_storage.mkdir(parents=True)
            source_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (source_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "input_datetime.time_battery_report",
                                    "platform": "input_datetime",
                                    "unique_id": "battery_report_time",
                                }
                            ]
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "allow_protected_storage": True,
                        "stop_core_before_sync_if_storage": True,
                        "restart_after_sync": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["stop", "check", "start"])
            live_data = json.loads((live_storage / "core.entity_registry").read_text())
            [entity] = live_data["data"]["entities"]
            self.assertEqual(entity["entity_id"], "input_datetime.time_battery_report")
            self.assertIn("modified_at", entity)
            self.assertIsNone(entity["suggested_object_id"])
            self.assertEqual(entity["supported_features"], 0)

    def test_apply_completes_missing_entity_registry_required_fields_for_new_live_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            source_storage.mkdir(parents=True)
            (server.CONFIG_DIR / ".storage").mkdir(parents=True)
            (source_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "input_datetime.time_battery_report_evening",
                                    "platform": "input_datetime",
                                    "unique_id": "battery_report_time_evening",
                                }
                            ]
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "allow_protected_storage": True,
                        "stop_core_before_sync_if_storage": True,
                        "restart_after_sync": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["stop", "check", "start"])
            live_data = json.loads((server.CONFIG_DIR / ".storage" / "core.entity_registry").read_text())
            [entity] = live_data["data"]["entities"]
            self.assertEqual(entity["entity_id"], "input_datetime.time_battery_report_evening")
            self.assertIn("modified_at", entity)
            self.assertIsNone(entity["suggested_object_id"])
            self.assertEqual(entity["supported_features"], 0)

    def test_apply_commit_records_completed_entity_registry_required_fields(self):
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
            (seed / "homeassistant" / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "input_datetime.time_battery_report_evening",
                                    "platform": "input_datetime",
                                    "unique_id": "battery_report_time_evening",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "restart_after_apply": True,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            self.select_all_apply_preview_files(server)
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])

            live_data = json.loads((live_storage / "core.entity_registry").read_text())
            service_branch_data = json.loads(
                subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        str(remote),
                        "show",
                        "ha-ops/ha-live:homeassistant/.storage/core.entity_registry",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            [live_entity] = live_data["data"]["entities"]
            [service_branch_entity] = service_branch_data["data"]["entities"]
            self.assertIn("modified_at", live_entity)
            self.assertIn("modified_at", service_branch_entity)
            self.assertIsNone(service_branch_entity["suggested_object_id"])
            self.assertEqual(service_branch_entity["supported_features"], 0)

    def test_apply_rejects_invalid_entity_registry_json_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage = source / ".storage"
            live_storage.mkdir(parents=True)
            source_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (source_storage / "core.entity_registry").write_text('{"data":{"entities":[')
            events = []
            server.core_stop = lambda: events.append("stop")

            with self.assertRaisesRegex(RuntimeError, "invalid JSON.*core.entity_registry"):
                server.apply_targets(
                    [
                        {
                            "id": "homeassistant",
                            "type": "homeassistant",
                            "source_path": str(source),
                            "live_path": str(server.CONFIG_DIR),
                            "allow_protected_storage": True,
                            "stop_core_before_sync_if_storage": True,
                            "restart_after_sync": True,
                        }
                    ],
                    [],
                )

            self.assertEqual(events, [])
            self.assertEqual(json.loads((live_storage / "core.entity_registry").read_text()), {"data": {"entities": []}})

    def test_apply_rejects_deleted_entity_registry_missing_modified_at_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage = source / ".storage"
            live_storage.mkdir(parents=True)
            source_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(json.dumps({"data": {"deleted_entities": []}}))
            (source_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "deleted_entities": [
                                {
                                    "id": "entity-1",
                                    "entity_id": "sensor.deleted_example",
                                    "unique_id": "deleted-example",
                                }
                            ]
                        }
                    }
                )
            )
            events = []
            server.core_stop = lambda: events.append("stop")

            with self.assertRaisesRegex(RuntimeError, "deleted_entities sensor.deleted_example missing modified_at"):
                server.apply_targets(
                    [
                        {
                            "id": "homeassistant",
                            "type": "homeassistant",
                            "source_path": str(source),
                            "live_path": str(server.CONFIG_DIR),
                            "allow_protected_storage": True,
                            "stop_core_before_sync_if_storage": True,
                            "restart_after_sync": True,
                        }
                    ],
                    [],
                )

            self.assertEqual(events, [])
            self.assertEqual(json.loads((live_storage / "core.entity_registry").read_text()), {"data": {"deleted_entities": []}})

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

    def test_theme_apply_reloads_themes_without_general_yaml_reload(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant" / "themes" / "custom"
            live = server.CONFIG_DIR / "themes" / "custom"
            source.mkdir(parents=True)
            live.mkdir(parents=True)
            (source / "theme.yaml").write_text("custom: {primary-color: '#fff'}\n")
            (live / "theme.yaml").write_text("custom: {primary-color: '#000'}\n")
            events = []
            server.do_core_check = lambda: events.append("check")
            server.core_reload_themes = lambda: events.append("themes")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source.parents[1]),
                        "live_path": str(server.CONFIG_DIR),
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "themes"])
            self.assertEqual((live / "theme.yaml").read_text(), "custom: {primary-color: '#fff'}\n")

    def test_lovelace_resources_apply_reloads_without_stopping_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_reload_lovelace = lambda: events.append("lovelace")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "lovelace"])
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/git.js"}]}}\n')

    def test_lovelace_dashboard_storage_apply_still_stops_core(self):
        for storage_name in ("lovelace", "lovelace.lovelace", "lovelace.map", "lovelace_dashboards"):
            with self.subTest(storage_name=storage_name):
                server = load_server()
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.configure_paths(server, root)
                    source = root / "repo" / "homeassistant"
                    source_storage = source / ".storage"
                    live_storage = server.CONFIG_DIR / ".storage"
                    source_storage.mkdir(parents=True)
                    live_storage.mkdir(parents=True)
                    (source_storage / storage_name).write_text('{"data":{"config":{"title":"Git"}}}\n')
                    (live_storage / storage_name).write_text('{"data":{"config":{"title":"Live"}}}\n')
                    events = []
                    server.core_stop = lambda: events.append("stop")
                    server.do_core_check = lambda: events.append("check")
                    server.core_start = lambda: events.append("start")
                    server.core_reload_lovelace = lambda: events.append("lovelace")
                    server.core_reload_yaml = lambda: events.append("reload")
                    server.core_restart = lambda: events.append("restart")

                    server.apply_targets(
                        [
                            {
                                "id": "homeassistant",
                                "type": "homeassistant",
                                "source_path": str(source),
                                "live_path": str(server.CONFIG_DIR),
                                "stop_core_before_storage_apply": True,
                                "start_core_after_storage_apply": True,
                            }
                        ],
                        [],
                    )

                    self.assertEqual(events, ["stop", "check", "start"])
                    self.assertEqual((live_storage / storage_name).read_text(), '{"data":{"config":{"title":"Git"}}}\n')

    def test_lovelace_dashboard_and_resources_apply_uses_storage_lifecycle(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "lovelace.lovelace").write_text('{"data":{"config":{"title":"Git"}}}\n')
            (live_storage / "lovelace.lovelace").write_text('{"data":{"config":{"title":"Live"}}}\n')
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_reload_lovelace = lambda: events.append("lovelace")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["stop", "check", "start"])
            self.assertEqual((live_storage / "lovelace.lovelace").read_text(), '{"data":{"config":{"title":"Git"}}}\n')
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/git.js"}]}}\n')

    def test_lovelace_dashboard_and_resources_apply_can_restart_instead_of_reload(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "lovelace_dashboards").write_text('{"data":{"items":[{"title":"Git"}]}}\n')
            (live_storage / "lovelace_dashboards").write_text('{"data":{"items":[{"title":"Live"}]}}\n')
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_reload_lovelace = lambda: events.append("lovelace")
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
                        "stop_core_before_storage_apply": False,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "restart"])

    def test_lovelace_storage_apply_respects_explicit_restart(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_reload_lovelace = lambda: events.append("lovelace")
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
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "restart"])

    def test_lovelace_storage_apply_falls_back_to_restart_when_soft_reload_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")

            def fail_lovelace_reload():
                events.append("lovelace")
                raise RuntimeError("service unavailable")

            server.core_reload_lovelace = fail_lovelace_reload
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "lovelace", "restart"])

    def test_mixed_yaml_and_lovelace_apply_restart_fallback_skips_yaml_reload(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source / "configuration.yaml").write_text("git\n")
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            (source_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/git.js"}]}}\n')
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.do_core_check = lambda: events.append("check")

            def fail_lovelace_reload():
                events.append("lovelace")
                raise RuntimeError("service unavailable")

            def fail_yaml_reload():
                events.append("reload")
                raise AssertionError("YAML reload should not run after restart fallback")

            server.core_reload_lovelace = fail_lovelace_reload
            server.core_reload_yaml = fail_yaml_reload
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "reload_yaml_after_apply": True,
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["check", "lovelace", "restart"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "git\n")
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/git.js"}]}}\n')

    def test_non_lovelace_storage_apply_still_stops_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            source = root / "repo" / "homeassistant"
            source_storage = source / ".storage"
            live_storage = server.CONFIG_DIR / ".storage"
            source_storage.mkdir(parents=True)
            live_storage.mkdir(parents=True)
            (source_storage / "input_boolean").write_text("{}\n")
            (live_storage / "input_boolean").write_text('{"data":{"items":[]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.do_core_check = lambda: events.append("check")
            server.core_start = lambda: events.append("start")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.apply_targets(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(source),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_storage_apply": True,
                        "start_core_after_storage_apply": True,
                    }
                ],
                [],
            )

            self.assertEqual(events, ["stop", "check", "start"])

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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
            state = server.read_state()
            self.assertEqual(state["last_save_preview"], "No Save changes.")
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertEqual(state["save_preview_selected_paths"], [])

    def test_save_push_first_post_commit_push_failure_enters_retry_only(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"main": 0}

            def fail_first_main_push(repo_dir, env, branch):
                if branch == "main" and calls["main"] == 0:
                    calls["main"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            finally:
                server.push_branch = original_push_branch

            repo = root / "data" / "ha-config"
            state = server.read_state()
            pending_commit = state["save_push_retry_commit"]
            self.assertEqual(calls["main"], 1)
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(pending_commit, server.git_head_or_unborn(repo))
            self.assertEqual(self.remote_main_subject(remote), "base")
            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_main_subject(remote), "Original Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")

    def test_save_push_retry_remote_advance_rejects_exact_pending_commit_and_stays_pending(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_first_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_main_pushes

            self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            failed_state = server.read_state()
            self.assertTrue(failed_state["save_push_retry_pending"])
            repo = root / "data" / "ha-config"
            pending_commit = failed_state["save_push_retry_commit"]
            self.assertEqual(pending_commit, server.git_head_or_unborn(repo))

            server.push_branch = original_push_branch
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "remote.yaml").write_text("remote\n")
            self.git_commit_all(updater, "remote")
            self.git(["push", "origin", "main"], updater)
            remote_advanced_commit = self.remote_rev(remote, "main")

            self.assertFalse(
                server.run_save_job(commit_subject="Edited Retry Subject"),
                server.read_state()["last_message"],
            )
            state = server.read_state()
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(state["save_push_retry_commit"], pending_commit)
            self.assertEqual(server.git_head_or_unborn(repo), pending_commit)
            self.assertIn("git push failed", state["last_message"])
            self.assertEqual(self.remote_rev(remote, "main"), remote_advanced_commit)
            self.assertEqual(self.remote_main_subject(remote), "remote")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/packages/new.yaml")
            self.assertEqual(self.remote_file(remote, "homeassistant/remote.yaml"), "remote\n")

    def test_save_push_retry_does_not_create_new_commit_for_new_live_changes(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_first_main_push(repo_dir, env, branch):
                if branch == "main" and calls["count"] == 0:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            finally:
                server.push_branch = original_push_branch
            pending_commit = server.read_state()["save_push_retry_commit"]

            (server.CONFIG_DIR / "packages" / "second.yaml").write_text("second:\n")
            self.assertTrue(server.run_save_job(commit_subject="Second Save Subject"), server.read_state()["last_message"])

            self.assertEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_main_subject(remote), "Original Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/packages/second.yaml")
            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIn("homeassistant/packages/second.yaml", state["last_save_preview_paths"])
            self.assertNotIn("homeassistant/packages/new.yaml", state["last_save_preview_paths"])

    def test_save_push_retry_success_resets_local_commits_on_top_of_pending_commit(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_first_main_push(repo_dir, env, branch):
                if branch == "main" and calls["count"] == 0:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_first_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            finally:
                server.push_branch = original_push_branch

            repo = root / "data" / "ha-config"
            pending_commit = server.read_state()["save_push_retry_commit"]
            (repo / "homeassistant" / "manual.yaml").write_text("manual\n")
            self.git_commit_all(repo, "manual local commit on top")
            local_commit = server.git_head_or_unborn(repo)
            self.assertNotEqual(local_commit, pending_commit)

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(server.git_head_or_unborn(repo), pending_commit)
            self.assertNotEqual(server.git_head_or_unborn(repo), local_commit)
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/manual.yaml")

            (server.CONFIG_DIR / "packages" / "second.yaml").write_text("second:\n")
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            preview_state = server.read_state()
            self.assertIn("homeassistant/packages/second.yaml", preview_state["last_save_preview_paths"])
            self.assertNotIn("homeassistant/manual.yaml", preview_state["last_save_preview_paths"])
            server.write_state({"save_preview_selected_paths": ["homeassistant/packages/second.yaml"]})
            self.assertTrue(server.run_save_job(commit_subject="Second Save Subject"), server.read_state()["last_message"])

            self.assertNotEqual(self.remote_rev(remote, "main"), local_commit)
            self.assertEqual(self.remote_main_subject(remote), "Second Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/second.yaml"), "second:\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/manual.yaml")

    def test_save_push_retry_can_be_cancelled_without_push(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_pushes
            try:
                self.assertFalse(server.run_save_job())
            finally:
                server.push_branch = original_push_branch

            pending_commit = server.read_state()["save_push_retry_commit"]
            self.assertTrue(server.read_state()["save_push_retry_pending"])
            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)

            response = self.post_json(server, "/clear-preview", body=b"direction=save")
            self.assertEqual(response.responses[-1], 200)
            self.assertIn("Save preview cancelled", response.wfile.getvalue().decode())

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertEqual(self.remote_main_subject(remote), "base")
            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(server.git_head_or_unborn(root / "data" / "ha-config"), self.remote_rev(remote, "main"))
            self.assertNotIn("Confirm Save to Git", server.render_page())

            (server.CONFIG_DIR / "packages" / "second.yaml").write_text("second:\n")
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            server.write_state({"save_preview_selected_paths": ["homeassistant/packages/second.yaml"]})
            self.assertTrue(server.run_save_job(commit_subject="Second Save Subject"), server.read_state()["last_message"])

            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_main_subject(remote), "Second Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/second.yaml"), "second:\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/packages/new.yaml")

    def test_clear_display_state_preserves_save_push_retry_without_push(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_pushes
            try:
                self.assertFalse(server.run_save_job())
            finally:
                server.push_branch = original_push_branch

            pending_commit = server.read_state()["save_push_retry_commit"]
            response = self.post_json(server, "/clear-display-state")
            self.assertEqual(response.responses[-1], 200)
            self.assertIn("Display state cleared", response.wfile.getvalue().decode())

            state = server.read_state()
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(state["save_push_retry_commit"], pending_commit)
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/packages/new.yaml"])
            self.assertEqual(state["save_preview_selected_paths"], ["homeassistant/packages/new.yaml"])
            self.assertEqual(server.git_head_or_unborn(root / "data" / "ha-config"), pending_commit)
            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])
            self.assertEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")

    def test_save_push_retry_cancel_discards_first_commit_when_remote_branch_missing(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self.prepare_empty_save_preview(server, root)
            original_push_branch = server.push_branch

            def fail_main_push(repo_dir, env, branch):
                if branch == "main":
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Canceled First Save"))
            finally:
                server.push_branch = original_push_branch

            repo = root / "data" / "ha-config"
            pending_commit = server.read_state()["save_push_retry_commit"]
            self.assertTrue(server.read_state()["save_push_retry_pending"])
            self.assertEqual(server.git_head_or_unborn(repo), pending_commit)
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_rev(remote, "main")

            response = self.post_json(server, "/clear-preview", body=b"direction=save")
            self.assertEqual(response.responses[-1], 200)
            self.assertIn("Save preview cancelled", response.wfile.getvalue().decode())

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertNotEqual(server.git_head_or_unborn(repo), pending_commit)
            self.assertFalse(server.git_head_is_unpushed_commit(repo, "main", pending_commit))
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_rev(remote, "main")

            (server.CONFIG_DIR / "configuration.yaml").write_text("second:\n")
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            self.assertTrue(server.run_save_job(commit_subject="Second Save Subject"), server.read_state()["last_message"])

            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_main_subject(remote), "Second Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/configuration.yaml"), "second:\n")

    def test_save_push_retry_cancel_resets_local_commits_on_top_of_pending_commit(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch

            def fail_main_push(repo_dir, env, branch):
                if branch == "main":
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Canceled Save"))
            finally:
                server.push_branch = original_push_branch

            repo = root / "data" / "ha-config"
            pending_commit = server.read_state()["save_push_retry_commit"]
            (repo / "homeassistant" / "manual.yaml").write_text("manual\n")
            self.git_commit_all(repo, "manual local commit on top")
            local_commit = server.git_head_or_unborn(repo)
            self.assertNotEqual(local_commit, pending_commit)

            response = self.post_json(server, "/clear-preview", body=b"direction=save")
            self.assertEqual(response.responses[-1], 200)
            self.assertIn("Save preview cancelled", response.wfile.getvalue().decode())

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertEqual(server.git_head_or_unborn(repo), self.remote_rev(remote, "main"))
            self.assertEqual(self.remote_main_subject(remote), "base")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/packages/new.yaml")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/manual.yaml")

            (server.CONFIG_DIR / "packages" / "second.yaml").write_text("second:\n")
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            server.write_state({"save_preview_selected_paths": ["homeassistant/packages/second.yaml"]})
            self.assertTrue(server.run_save_job(commit_subject="Second Save Subject"), server.read_state()["last_message"])

            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertNotEqual(self.remote_rev(remote, "main"), local_commit)
            self.assertEqual(self.remote_main_subject(remote), "Second Save Subject")
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/second.yaml"), "second:\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/packages/new.yaml")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/manual.yaml")

    def test_save_push_retry_cancel_dirty_checkout_returns_conflict_and_keeps_retry(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch

            def fail_main_push(repo_dir, env, branch):
                if branch == "main":
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_push
            try:
                self.assertFalse(server.run_save_job(commit_subject="Dirty Cancel Save"))
            finally:
                server.push_branch = original_push_branch

            repo = root / "data" / "ha-config"
            pending_commit = server.read_state()["save_push_retry_commit"]
            (repo / "homeassistant" / "configuration.yaml").write_text("dirty local edit\n")

            response = self.post_json(server, "/clear-preview", body=b"direction=save")
            payload = json.loads(response.wfile.getvalue().decode())
            self.assertEqual(response.responses[-1], 409)
            self.assertFalse(payload["ok"])
            self.assertIn("uncommitted changes", payload["message"])

            state = server.read_state()
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(state["save_push_retry_commit"], pending_commit)
            self.assertEqual(server.git_head_or_unborn(repo), pending_commit)
            self.assertIn("homeassistant/configuration.yaml", self.repo_status(repo))
            self.assertEqual(self.remote_main_subject(remote), "base")

    def test_save_push_retry_survives_startup_refresh(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_pushes
            try:
                self.assertFalse(server.run_save_job())
            finally:
                server.push_branch = original_push_branch

            server.repair_startup_state()
            state = server.read_state()
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(state["save_push_retry_commit"], server.git_head_or_unborn(root / "data" / "ha-config"))
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/packages/new.yaml"])
            self.assertEqual(state["save_preview_selected_paths"], ["homeassistant/packages/new.yaml"])

    def test_stale_save_push_retry_flag_clears_when_no_unpushed_commit_exists(self):
        server = load_server()
        for action in ("cancel", "startup-refresh"):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.configure_paths(server, root)
                    server.write_state(
                        {
                            "save_push_retry_pending": True,
                            "last_save_preview": "stale save preview",
                            "last_save_diff": "stale save diff",
                            "last_save_diff_generated_at": "2026-06-24T12:00:00+00:00",
                            "last_save_preview_commit": "stale-save-commit",
                            "last_save_preview_fingerprint": "stale-save-fingerprint",
                            "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                            "last_save_preview_conflicts": False,
                            "save_preview_resolutions": {},
                            "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                            "save_push_retry_commit": "stale-save-commit",
                        }
                    )

                    if action == "cancel":
                        response = self.post_json(server, "/clear-preview", body=b"direction=save")
                        self.assertEqual(response.responses[-1], 200)
                        self.assertIn("Save preview cancelled", response.wfile.getvalue().decode())
                    else:
                        server.repair_startup_state()

                    state = server.read_state()
                    self.assertFalse(state.get("save_push_retry_pending", False))
                    self.assertIsNone(state.get("save_push_retry_commit"))
                    self.assertEqual(state["last_save_preview"], "")
                    self.assertEqual(state["last_save_diff"], "")
                    self.assertIsNone(state["last_save_diff_generated_at"])
                    self.assertIsNone(state["last_save_preview_commit"])
                    self.assertIsNone(state["last_save_preview_fingerprint"])
                    self.assertEqual(state["last_save_preview_paths"], [])
                    self.assertEqual(state["save_preview_selected_paths"], [])
                    self.assertNotIn("Confirm Save to Git", server.render_page())

    def test_stale_save_push_retry_flag_with_unrelated_unpushed_commit_is_not_preserved_or_pushed(self):
        server = load_server()
        for action in ("cancel", "startup-refresh", "confirm-save"):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.configure_paths(server, root)
                    remote = self.seed_remote(root)
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
                    repo = server.ensure_repo(server.load_options())
                    (repo / "homeassistant" / "manual.yaml").write_text("manual\n")
                    self.git_commit_all(repo, "manual unrelated local commit")
                    unrelated_commit = server.git_head_or_unborn(repo)
                    remote_before = self.remote_rev(remote, "main")
                    server.write_state(
                        {
                            "save_push_retry_pending": True,
                            "save_push_retry_commit": "stale-save-commit",
                            "last_save_preview": "stale save preview",
                            "last_save_diff": "stale save diff",
                            "last_save_diff_generated_at": "2026-06-24T12:00:00+00:00",
                            "last_save_preview_commit": "stale-save-commit",
                            "last_save_preview_fingerprint": "stale-save-fingerprint",
                            "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                            "last_save_preview_conflicts": False,
                            "save_preview_resolutions": {},
                            "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                        }
                    )

                    if action == "cancel":
                        response = self.post_json(server, "/clear-preview", body=b"direction=save")
                        self.assertEqual(response.responses[-1], 200)
                        self.assertIn("Save preview cancelled", response.wfile.getvalue().decode())
                    elif action == "startup-refresh":
                        server.repair_startup_state()
                    else:
                        self.assertFalse(server.run_save_job())
                        state = server.read_state()
                        self.assertEqual(
                            state["last_message"],
                            "Stale Save push retry was cleared. Review a fresh Save preview before confirming.",
                        )

                    state = server.read_state()
                    self.assertFalse(state.get("save_push_retry_pending", False))
                    self.assertIsNone(state.get("save_push_retry_commit"))
                    self.assertEqual(state["last_save_preview"], "")
                    self.assertEqual(state["last_save_diff"], "")
                    self.assertIsNone(state["last_save_diff_generated_at"])
                    self.assertIsNone(state["last_save_preview_commit"])
                    self.assertIsNone(state["last_save_preview_fingerprint"])
                    self.assertEqual(state["last_save_preview_paths"], [])
                    self.assertEqual(state["save_preview_selected_paths"], [])
                    self.assertEqual(server.git_head_or_unborn(repo), unrelated_commit)
                    self.assertEqual(self.remote_rev(remote, "main"), remote_before)
                    self.assertEqual(self.remote_main_subject(remote), "base")
                    self.assertNotIn("Confirm Save to Git", server.render_page())

    def test_stale_save_push_retry_does_not_commit_internal_ids_or_push_unrelated_commit(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
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
            repo = server.ensure_repo(server.load_options())
            area_file = repo / "homeassistant" / ".ha-ops" / "areas" / "kitchen" / "automations.yaml"
            area_file.parent.mkdir(parents=True)
            area_file.write_text("- id: kitchen_original\n")
            self.git_commit_all(repo, "add internal ids area")
            self.git(["push", "origin", "main"], repo)
            remote_before = self.remote_rev(remote, "main")

            (repo / "homeassistant" / "manual.yaml").write_text("manual\n")
            self.git_commit_all(repo, "manual unrelated local commit")
            unrelated_commit = server.git_head_or_unborn(repo)
            area_file.write_text("- id: kitchen_migrated\n  alias: Kitchen migrated\n")
            self.assertIn("homeassistant/.ha-ops/areas/kitchen/automations.yaml", self.repo_status(repo))
            server.write_state(
                {
                    "save_push_retry_pending": True,
                    "save_push_retry_commit": "stale-save-commit",
                    "last_save_preview": "stale save preview",
                    "last_save_diff": "stale save diff",
                    "last_save_diff_generated_at": "2026-06-24T12:00:00+00:00",
                    "last_save_preview_commit": "stale-save-commit",
                    "last_save_preview_fingerprint": "stale-save-fingerprint",
                    "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_save_preview_conflicts": False,
                    "save_preview_resolutions": {},
                    "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                }
            )

            self.assertFalse(server.run_save_job())

            state = server.read_state()
            self.assertEqual(
                state["last_message"],
                "Stale Save push retry was cleared. Review a fresh Save preview before confirming.",
            )
            self.assertFalse(state.get("save_push_retry_pending", False))
            self.assertIsNone(state.get("save_push_retry_commit"))
            self.assertEqual(server.git_head_or_unborn(repo), unrelated_commit)
            self.assertIn("homeassistant/.ha-ops/areas/kitchen/automations.yaml", self.repo_status(repo))
            self.assertEqual(self.remote_rev(remote, "main"), remote_before)
            self.assertEqual(self.remote_main_subject(remote), "add internal ids area")
            self.assertNotIn("Confirm Save to Git", server.render_page())

    def test_valid_save_push_retry_blocks_dirty_internal_ids_without_pushing_migration(self):
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
            repo = server.ensure_repo(server.load_options())
            area_file = repo / "homeassistant" / ".ha-ops" / "areas" / "kitchen" / "automations.yaml"
            area_file.parent.mkdir(parents=True)
            area_file.write_text("- id: kitchen_original\n")
            self.git_commit_all(repo, "add internal ids area")
            self.git(["push", "origin", "main"], repo)

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_pushes
            try:
                self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            finally:
                server.push_branch = original_push_branch
            failed_state = server.read_state()
            pending_commit = failed_state["save_push_retry_commit"]
            self.assertTrue(failed_state["save_push_retry_pending"])

            area_file.write_text("- id: kitchen_migrated\n  alias: Kitchen migrated\n")
            self.assertFalse(server.run_save_job())

            state = server.read_state()
            self.assertTrue(state["save_push_retry_pending"])
            self.assertEqual(state["save_push_retry_commit"], pending_commit)
            self.assertIn("Internal IDs migration changes", state["last_message"])
            self.assertEqual(self.remote_main_subject(remote), "add internal ids area")
            self.assertNotEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertIn("homeassistant/.ha-ops/areas/kitchen/automations.yaml", self.repo_status(repo))

    def test_valid_save_push_retry_pushes_pending_commit_with_unrelated_dirty_tracked_file(self):
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
            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
            original_push_branch = server.push_branch
            calls = {"count": 0}

            def fail_main_pushes(repo_dir, env, branch):
                if branch == "main" and calls["count"] < 2:
                    calls["count"] += 1
                    raise RuntimeError("temporary push failure")
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = fail_main_pushes
            try:
                self.assertFalse(server.run_save_job(commit_subject="Original Save Subject"))
            finally:
                server.push_branch = original_push_branch
            repo = root / "data" / "ha-config"
            pending_commit = server.read_state()["save_push_retry_commit"]
            (repo / "homeassistant" / "manual.yaml").write_text("manual\n")
            self.git_commit_all(repo, "manual local commit on top")
            local_commit = server.git_head_or_unborn(repo)
            self.assertNotEqual(local_commit, pending_commit)
            config_file = repo / "homeassistant" / "configuration.yaml"
            config_file.write_text("dirty local edit\n")

            self.assertTrue(server.run_save_job(), server.read_state()["last_message"])

            state = server.read_state()
            self.assertFalse(state["save_push_retry_pending"])
            self.assertIsNone(state["save_push_retry_commit"])
            self.assertEqual(state["last_status"], "warning")
            self.assertEqual(
                state["last_message"],
                "Save push retry pushed the pending commit. Local checkout changes are still present, so the Save preview was not rebuilt.",
            )
            self.assertEqual(state["last_save_preview_paths"], ["homeassistant/packages/new.yaml"])
            self.assertEqual(state["save_preview_selected_paths"], ["homeassistant/packages/new.yaml"])
            self.assertEqual(self.remote_rev(remote, "main"), pending_commit)
            self.assertEqual(self.remote_main_subject(remote), "Original Save Subject")
            self.assertEqual(server.git_head_or_unborn(repo), pending_commit)
            self.assertNotEqual(server.git_head_or_unborn(repo), local_commit)
            self.assertIn("homeassistant/configuration.yaml", self.repo_status(repo))
            self.assertIn("homeassistant/manual.yaml", self.repo_status(repo))
            self.assertEqual(self.remote_file(remote, "homeassistant/packages/new.yaml"), "homeassistant:\n")
            with self.assertRaises(subprocess.CalledProcessError):
                self.remote_file(remote, "homeassistant/manual.yaml")

    def test_stale_save_push_retry_clears_before_untracked_checkout_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root)
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
            repo = server.ensure_repo(server.load_options())
            untracked = repo / "homeassistant" / "local-note.txt"
            untracked.write_text("do not delete\n")
            server.write_state(
                {
                    "save_push_retry_pending": True,
                    "save_push_retry_commit": "stale-save-commit",
                    "last_save_preview": "stale save preview",
                    "last_save_diff": "stale save diff",
                    "last_save_diff_generated_at": "2026-06-24T12:00:00+00:00",
                    "last_save_preview_commit": "stale-save-commit",
                    "last_save_preview_fingerprint": "stale-save-fingerprint",
                    "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_save_preview_conflicts": False,
                    "save_preview_resolutions": {},
                    "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
                }
            )

            self.assertFalse(server.run_save_job())

            state = server.read_state()
            self.assertFalse(state.get("save_push_retry_pending", False))
            self.assertIsNone(state.get("save_push_retry_commit"))
            self.assertTrue(untracked.exists())
            self.assertEqual(
                state["last_message"],
                "Stale Save push retry was cleared. Review a fresh Save preview before confirming.",
            )

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
            self.push_service_branches(seed)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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
            self.push_service_branches(seed)

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

            self.assertTrue(server.run_save_preview_job(), server.read_state()["last_message"])
            self.select_all_save_preview_files(server)
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

            self.assertFalse(server.run_save_job())
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

    def test_apply_preview_apply_all_approves_storage_changes(self):
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
            self.push_service_branches(seed)
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
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
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
            self.assertEqual(state["apply_preview_selected_paths"], [])
            self.assertFalse(server.run_apply_job())
            self.assertIn("Select at least one preview file", server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")

            server.write_state({"apply_preview_selected_paths": state["last_preview_paths"]})
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "git-storage\n")

    def test_apply_preview_warns_when_internal_git_state_is_out_of_date(self):
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
            self.push_service_branches(seed)
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
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            original_push_branch = server.push_branch

            def push_branch(repo_dir, env, branch):
                if branch == "ha-ops/base":
                    raise RuntimeError(
                        "git push failed:\n"
                        " ! [rejected]        ha-ops/base -> ha-ops/base (non-fast-forward)\n"
                        "error: failed to push some refs"
                    )
                return original_push_branch(repo_dir, env, branch)

            server.push_branch = push_branch

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            details = "\n".join(state["last_details"])
            self.assertEqual(state["last_status"], "warning")
            self.assertEqual(state["last_message"], "HA Ops internal Git state is out of date.")
            self.assertTrue(state["last_preview_storage_changes"])
            self.assertIn("Skipped pushing ha-ops/base", details)
            self.assertIn("Use Reset Git State", details)
            self.assertIn("Confirm required for 1 .storage change(s)", details)

    def test_apply_preview_per_file_choice_keeps_ha_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text("git-automations\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.git(["branch", "ha-ops/ha-live"], seed)
            self.git(["branch", "ha-ops/base"], seed)
            self.git(["push", "origin", "ha-ops/ha-live", "ha-ops/base"], seed)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha-config\n")
            (server.CONFIG_DIR / "automations.yaml").write_text("ha-automations\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {"homeassistant/automations.yaml", "homeassistant/configuration.yaml"},
            )
            server.write_state(
                {
                    "apply_preview_selected_paths": ["homeassistant/automations.yaml"],
                    "apply_preview_resolutions": {
                        "homeassistant/configuration.yaml": "git",
                        "homeassistant/automations.yaml": "git",
                    }
                }
            )

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha-config\n")
            self.assertEqual((server.CONFIG_DIR / "automations.yaml").read_text(), "git-automations\n")

    def test_partial_apply_rebuilds_preview_with_unselected_files_only(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant").mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("git-config\n")
            (seed / "homeassistant" / "automations.yaml").write_text(
                "- id: git_auto\n  alias: Git Auto\n  trigger: []\n  condition: []\n  action: []\n"
            )
            (seed / "homeassistant" / "scripts.yaml").write_text("git_script:\n  sequence: []\n")
            (seed / "homeassistant" / "scenes.yaml").write_text("- id: git_scene\n  name: Git Scene\n  entities: {}\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha-config\n")
            (server.CONFIG_DIR / "automations.yaml").write_text(
                "- id: ha_auto\n  alias: HA Auto\n  trigger: []\n  condition: []\n  action: []\n"
            )
            (server.CONFIG_DIR / "scripts.yaml").write_text("ha_script:\n  sequence: []\n")
            (server.CONFIG_DIR / "scenes.yaml").write_text("- id: ha_scene\n  name: HA Scene\n  entities: {}\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {
                    "homeassistant/automations.yaml",
                    "homeassistant/configuration.yaml",
                    "homeassistant/scenes.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            old_fingerprint = state["last_preview_fingerprint"]
            old_live_fingerprints = state["last_preview_live_fingerprints"]
            server.write_state({"apply_preview_selected_paths": ["homeassistant/automations.yaml"]})

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {
                    "homeassistant/configuration.yaml",
                    "homeassistant/scenes.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.assertNotIn("homeassistant/automations.yaml", state["last_diff"])
            self.assertNotEqual(state["last_preview_fingerprint"], old_fingerprint)
            self.assertNotEqual(state["last_preview_live_fingerprints"], old_live_fingerprints)
            self.assertEqual(state["apply_preview_selected_paths"], [])
            self.assertEqual(state["apply_preview_resolutions"], {})
            repo_dir = root / "data" / "ha-config"
            main_commit = self.git(["rev-parse", "main"], repo_dir).stdout.strip()
            live_commit = self.git(["rev-parse", "ha-ops/ha-live"], repo_dir).stdout.strip()
            self.assertEqual(state["last_preview_commit"], main_commit)
            self.assertNotEqual(state["last_preview_commit"], live_commit)
            self.assertIn("git_auto", (server.CONFIG_DIR / "automations.yaml").read_text())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha-config\n")
            self.assertIn("ha_script", (server.CONFIG_DIR / "scripts.yaml").read_text())
            self.assertIn("ha_scene", (server.CONFIG_DIR / "scenes.yaml").read_text())

            server.write_state({"apply_preview_selected_paths": ["homeassistant/configuration.yaml"]})

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            state = server.read_state()
            details = "\n".join(state["last_details"])
            self.assertNotEqual(state["last_message"], "State changed since this preview was created. Review the updated preview before continuing.")
            self.assertNotIn("State changed since this preview was created", details)
            self.assertEqual(state["last_status"], "success")
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "git-config\n")
            self.assertIn("git_auto", (server.CONFIG_DIR / "automations.yaml").read_text())
            self.assertIn("ha_script", (server.CONFIG_DIR / "scripts.yaml").read_text())
            self.assertIn("ha_scene", (server.CONFIG_DIR / "scenes.yaml").read_text())
            self.assertEqual(
                set(state["last_preview_paths"]),
                {
                    "homeassistant/scenes.yaml",
                    "homeassistant/scripts.yaml",
                },
            )
            self.assertNotIn("homeassistant/configuration.yaml", state["last_diff"])
            main_commit = self.git(["rev-parse", "main"], repo_dir).stdout.strip()
            live_commit = self.git(["rev-parse", "ha-ops/ha-live"], repo_dir).stdout.strip()
            self.assertEqual(state["last_preview_commit"], main_commit)
            self.assertNotEqual(state["last_preview_commit"], live_commit)

    @unittest.skip("enabled .ha-ops/areas projection is paused")
    def test_partial_apply_organizer_paths_materializes_selected_heap_items_only(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            areas = seed / "homeassistant" / ".ha-ops" / "areas"
            (areas / "home").mkdir(parents=True)
            (areas / ".unknown").mkdir(parents=True)
            (areas / "home" / "automations.yaml").write_text(
                "- id: home_auto\n  alias: Git Home Auto\n  trigger: []\n  condition: []\n  action: []\n"
            )
            (areas / ".unknown" / "automations.yaml").write_text(
                "- id: unknown_auto\n  alias: Git Unknown Auto\n  trigger: []\n  condition: []\n  action: []\n"
            )
            (areas / "home" / "scripts.yaml").write_text("home_script:\n  alias: Git Home Script\n  sequence: []\n")
            (areas / ".unknown" / "scripts.yaml").write_text(
                "unknown_script:\n  alias: Git Unknown Script\n  sequence: []\n"
            )
            (areas / "organizer-index.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "automations": {"count": 2, "ids": ["home_auto", "unknown_auto"]},
                        "scripts": {"count": 2, "ids": ["home_script", "unknown_script"]},
                        "scenes": {"count": 0, "ids": []},
                    }
                )
            )
            (seed / "homeassistant" / ".storage").mkdir(parents=True)
            (seed / "homeassistant" / ".storage" / "input_boolean").write_text("git-storage\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            (server.CONFIG_DIR / "automations.yaml").write_text(
                "\n".join(
                    [
                        "- id: home_auto",
                        "  alias: Live Home Auto",
                        "  trigger: []",
                        "  condition: []",
                        "  action: []",
                        "- id: unknown_auto",
                        "  alias: Live Unknown Auto",
                        "  trigger: []",
                        "  condition: []",
                        "  action: []",
                        "",
                    ]
                )
            )
            (server.CONFIG_DIR / "scripts.yaml").write_text(
                "\n".join(
                    [
                        "home_script:",
                        "  alias: Live Home Script",
                        "  sequence: []",
                        "unknown_script:",
                        "  alias: Live Unknown Script",
                        "  sequence: []",
                        "",
                    ]
                )
            )
            (server.CONFIG_DIR / "scenes.yaml").write_text("[]\n")
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir(parents=True)
            (storage / "core.area_registry").write_text(json.dumps({"data": {"areas": [{"id": "home", "name": "Home"}]}}))
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": []}}))
            (storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "automation.home_auto",
                                    "unique_id": "home_auto",
                                    "area_id": "home",
                                },
                                {
                                    "entity_id": "script.home_script",
                                    "unique_id": "home_script",
                                    "area_id": "home",
                                },
                            ]
                        }
                    }
                )
            )
            (storage / "input_boolean").write_text("live-storage\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None
            server.set_homeassistant_organizer_enabled(True)

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {
                    "homeassistant/.ha-ops/areas/.unknown/automations.yaml",
                    "homeassistant/.ha-ops/areas/.unknown/scripts.yaml",
                    "homeassistant/.ha-ops/areas/home/automations.yaml",
                    "homeassistant/.ha-ops/areas/home/scripts.yaml",
                    "homeassistant/.ha-ops/areas/organizer-index.json",
                    "homeassistant/.storage/input_boolean",
                },
            )
            server.write_state(
                {
                    "apply_preview_selected_paths": [
                        "homeassistant/.ha-ops/areas/.unknown/scripts.yaml",
                        "homeassistant/.ha-ops/areas/home/automations.yaml",
                    ]
                }
            )

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {
                    "homeassistant/.ha-ops/areas/.unknown/automations.yaml",
                    "homeassistant/.ha-ops/areas/home/scripts.yaml",
                    "homeassistant/.ha-ops/areas/organizer-index.json",
                    "homeassistant/.storage/input_boolean",
                },
            )
            self.assertNotIn("Git Home Auto", state["last_diff"])
            self.assertNotIn("Git Unknown Script", state["last_diff"])
            self.assertIn("Git Unknown Auto", state["last_diff"])
            self.assertIn("Git Home Script", state["last_diff"])
            self.assertEqual((storage / "input_boolean").read_text(), "live-storage\n")
            automations_text = (server.CONFIG_DIR / "automations.yaml").read_text()
            scripts_text = (server.CONFIG_DIR / "scripts.yaml").read_text()
            self.assertIn("Git Home Auto", automations_text)
            self.assertIn("Live Unknown Auto", automations_text)
            self.assertIn("Live Home Script", scripts_text)
            self.assertIn("Git Unknown Script", scripts_text)
            self.assertEqual(state["apply_preview_selected_paths"], [])
            self.assertEqual(state["apply_preview_resolutions"], {})

    def test_apply_preview_conflict_uses_fresh_live_branch(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("Apply preview conflicts (1):", state["last_diff"])
            self.assertEqual(state["last_preview_paths"], ["homeassistant/configuration.yaml"])

    def test_apply_preview_ignores_repo_only_service_branch_conflicts(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            config_path = seed / "homeassistant" / "configuration.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("base\n")
            test_path = seed / "tests" / "test_battery_attention_report_html.py"
            test_path.parent.mkdir()
            test_path.write_text("base\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            (updater / "tests" / "test_battery_attention_report_html.py").write_text("git\n")
            self.git_commit_all(updater, "git changes")
            self.git(["push", "origin", "main"], updater)

            live_updater = root / "live-updater"
            self.git(["clone", str(remote), str(live_updater)], root)
            self.git(["checkout", "ha-ops/ha-live"], live_updater)
            (live_updater / "homeassistant" / "configuration.yaml").write_text("ha\n")
            (live_updater / "tests" / "test_battery_attention_report_html.py").write_text("ha\n")
            self.git_commit_all(live_updater, "live changes")
            self.git(["push", "origin", "ha-ops/ha-live"], live_updater)

            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertTrue(state["last_preview_conflicts"])
            self.assertEqual(state["last_preview_conflict_paths"], ["homeassistant/configuration.yaml"])
            self.assertEqual(state["last_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertIn("homeassistant/configuration.yaml", state["last_diff"])
            self.assertNotIn("tests/test_battery_attention_report_html.py", state["last_diff"])

    def test_apply_preview_uses_updated_remote_live_branch(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "git\n")
            live_config = server.CONFIG_DIR / "configuration.yaml"
            live_config.write_text("ha1\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            updater = root / "live-updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "ha-ops/ha-live"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("ha-remote\n")
            self.git_commit_all(updater, "remote live")
            self.git(["push", "origin", "ha-ops/ha-live"], updater)
            live_config.write_text("ha2\n")

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "ha-ops/ha-live:homeassistant/configuration.yaml"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout, "ha2\n")

    def test_apply_same_content_divergent_merge_creates_live_merge_commit(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("same\n")
            self.git_commit_all(updater, "main same")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("same\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            self.assertEqual(server.read_state()["last_diff"], "Target homeassistant: no file changes.")
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])

            parents = self.remote_parents(remote, "ha-ops/ha-live")
            self.assertEqual(len(parents), 2)
            self.assertEqual(self.remote_rev(remote, "ha-ops/base"), self.remote_rev(remote, "main"))

    def test_apply_preview_conflict_can_be_confirmed(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            clean_git = updater / "homeassistant" / "packages" / "clean.yaml"
            clean_git.parent.mkdir(parents=True)
            clean_git.write_text("git-clean\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("homeassistant/packages/clean.yaml", state["last_diff"])
            self.assertEqual(
                set(state["last_preview_paths"]),
                {"homeassistant/configuration.yaml", "homeassistant/packages/clean.yaml"},
            )
            self.assertEqual(state["last_preview_conflict_paths"], ["homeassistant/configuration.yaml"])
            self.select_all_apply_preview_files(server)
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "git\n")
            self.assertEqual((server.CONFIG_DIR / "packages" / "clean.yaml").read_text(), "git-clean\n")

    def test_apply_preview_conflict_uses_rebuilt_local_live_branch_for_ha_choice(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            live_config = server.CONFIG_DIR / "configuration.yaml"
            live_config.write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            other = root / "other"
            self.git(["clone", str(remote), str(other)], root)
            self.git(["checkout", "ha-ops/ha-live"], other)
            (other / "homeassistant" / "configuration.yaml").write_text("other-ha\n")
            self.git_commit_all(other, "other live")
            self.git(["push", "origin", "ha-ops/ha-live"], other)

            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/configuration.yaml": "ha"}})
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual(live_config.read_text(), "ha\n")
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "ha-ops/ha-live:homeassistant/configuration.yaml"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout, "ha\n")

    def test_apply_preview_conflict_applies_clean_git_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / "configuration.yaml").parent.mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("base\n")
            clean_delete = seed / "homeassistant" / "packages" / "clean_delete.yaml"
            clean_delete.parent.mkdir(parents=True)
            clean_delete.write_text("base-clean\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            (updater / "homeassistant" / "packages" / "clean_delete.yaml").unlink()
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            live_clean_delete = server.CONFIG_DIR / "packages" / "clean_delete.yaml"
            live_clean_delete.parent.mkdir(parents=True)
            live_clean_delete.write_text("base-clean\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertIn("homeassistant/packages/clean_delete.yaml", state["last_diff"])
            self.assertEqual(
                set(state["last_preview_paths"]),
                {"homeassistant/configuration.yaml", "homeassistant/packages/clean_delete.yaml"},
            )
            self.assertEqual(state["last_preview_conflict_paths"], ["homeassistant/configuration.yaml"])
            self.assertEqual(state["last_preview_deletions"], 1)
            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/configuration.yaml": "ha"}})

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha\n")
            self.assertFalse(live_clean_delete.exists())

    def test_apply_preview_conflict_skips_unselected_clean_git_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            (seed / "homeassistant" / "configuration.yaml").parent.mkdir(parents=True)
            (seed / "homeassistant" / "configuration.yaml").write_text("base\n")
            clean_delete = seed / "homeassistant" / "packages" / "clean_delete.yaml"
            clean_delete.parent.mkdir(parents=True)
            clean_delete.write_text("base-clean\n")
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            (updater / "homeassistant" / "packages" / "clean_delete.yaml").unlink()
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha\n")
            live_clean_delete = server.CONFIG_DIR / "packages" / "clean_delete.yaml"
            live_clean_delete.parent.mkdir(parents=True)
            live_clean_delete.write_text("base-clean\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(
                set(state["last_preview_paths"]),
                {"homeassistant/configuration.yaml", "homeassistant/packages/clean_delete.yaml"},
            )
            server.write_state(
                {
                    "apply_preview_selected_paths": ["homeassistant/configuration.yaml"],
                    "apply_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                }
            )

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha\n")
            self.assertTrue(live_clean_delete.exists())
            self.assertEqual(live_clean_delete.read_text(), "base-clean\n")

    def test_apply_conflict_git_delete_counts_against_max_apply_deletions(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").unlink()
            self.git_commit_all(updater, "git delete")
            self.git(["push", "origin", "main"], updater)
            live_config = server.CONFIG_DIR / "configuration.yaml"
            live_config.write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "max_apply_deletions": 0,
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertEqual(state["last_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertTrue(state["last_preview_conflicts"])
            self.assertEqual(state["last_preview_deletions"], 0)
            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/configuration.yaml": "git"}})

            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "error")
            self.assertIn("Apply would delete 1 file(s), above the limit of 0", state["last_message"])
            self.assertTrue(live_config.exists())
            self.assertEqual(live_config.read_text(), "ha\n")

    def test_apply_preview_conflict_rejects_stale_live_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "configuration.yaml").write_text("git\n")
            self.git_commit_all(updater, "git")
            self.git(["push", "origin", "main"], updater)
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha1\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            (server.CONFIG_DIR / "configuration.yaml").write_text("ha2\n")
            self.select_all_apply_preview_files(server)

            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha2\n")

    def test_apply_preview_protected_storage_conflict_can_apply_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            registry = seed / "homeassistant" / ".storage" / "core.device_registry"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "base-modified-at",
                                    "name": "base-storage",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(seed, "base storage")
            self.git(["push", "origin", "main"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / ".storage" / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "git-modified-at",
                                    "name": "git-storage",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(updater, "git storage")
            self.git(["push", "origin", "main"], updater)
            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "modified_at": "ha-modified-at",
                                    "name": "ha-storage",
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
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertTrue(state["last_preview_storage_changes"])
            self.assertEqual(state["last_preview_storage_paths"], ["homeassistant/.storage/core.device_registry"])
            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/.storage/core.device_registry": "git"}})

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            saved = json.loads((live_storage / "core.device_registry").read_text())
            self.assertEqual(saved["data"]["devices"][0]["name"], "git-storage")
            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "ha-modified-at")

    def test_apply_preview_entity_registry_conflict_records_completed_git_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            registry = seed / "homeassistant" / ".storage" / "core.entity_registry"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "input_datetime.time_battery_report_evening",
                                    "id": "entity-1",
                                    "modified_at": "base-modified-at",
                                    "name": "base-storage",
                                    "platform": "input_datetime",
                                    "suggested_object_id": "base_time_battery_report_evening",
                                    "supported_features": 0,
                                    "unique_id": "battery_report_time_evening",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(seed, "base entity registry")
            self.git(["push", "origin", "main"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / ".storage" / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "input_datetime.time_battery_report_evening",
                                    "id": "entity-1",
                                    "name": "git-storage",
                                    "platform": "input_datetime",
                                    "unique_id": "battery_report_time_evening",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(updater, "git entity registry")
            self.git(["push", "origin", "main"], updater)
            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "entity_id": "input_datetime.time_battery_report_evening",
                                    "id": "entity-1",
                                    "modified_at": "ha-modified-at",
                                    "name": "ha-storage",
                                    "platform": "input_datetime",
                                    "suggested_object_id": "ha_time_battery_report_evening",
                                    "supported_features": 0,
                                    "unique_id": "battery_report_time_evening",
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
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            state = server.read_state()
            self.assertTrue(state["last_preview_storage_changes"])
            self.assertEqual(state["last_preview_storage_paths"], ["homeassistant/.storage/core.entity_registry"])
            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/.storage/core.entity_registry": "git"}})

            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            saved = json.loads((live_storage / "core.entity_registry").read_text())
            service_branch = json.loads(
                subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        str(remote),
                        "show",
                        "ha-ops/ha-live:homeassistant/.storage/core.entity_registry",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout
            )
            live_entity = saved["data"]["entities"][0]
            service_branch_entity = service_branch["data"]["entities"][0]
            self.assertEqual(live_entity["name"], "git-storage")
            self.assertEqual(live_entity["modified_at"], "ha-modified-at")
            self.assertEqual(live_entity["suggested_object_id"], "ha_time_battery_report_evening")
            self.assertEqual(service_branch_entity["name"], "git-storage")
            self.assertEqual(service_branch_entity["modified_at"], "ha-modified-at")
            self.assertEqual(service_branch_entity["suggested_object_id"], "ha_time_battery_report_evening")

    def test_apply_preview_modify_delete_conflict_can_apply_git_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = self.seed_remote(root, "base\n")
            seed = root / "seed"
            package = seed / "homeassistant" / "packages" / "a.yaml"
            package.parent.mkdir(parents=True)
            package.write_text("base\n")
            self.git_commit_all(seed, "base package")
            self.git(["push", "origin", "main"], seed)
            self.git(["branch", "-f", "ha-ops/ha-live", "HEAD"], seed)
            self.git(["branch", "-f", "ha-ops/base", "HEAD"], seed)
            self.push_service_branches(seed)
            updater = root / "updater"
            self.git(["clone", str(remote), str(updater)], root)
            self.git(["checkout", "main"], updater)
            (updater / "homeassistant" / "packages" / "a.yaml").unlink()
            self.git_commit_all(updater, "delete config")
            self.git(["push", "origin", "main"], updater)
            live_package = server.CONFIG_DIR / "packages" / "a.yaml"
            live_package.parent.mkdir(parents=True)
            live_package.write_text("ha\n")
            server.OPTIONS_PATH.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            self.select_all_apply_preview_files(server)
            server.write_state({"apply_preview_resolutions": {"homeassistant/packages/a.yaml": "git"}})
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            self.assertFalse(live_package.exists())

    def test_apply_without_matching_preview_rebuilds_preview_and_warns(self):
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
                        "require_fresh_backup": False,
                        "create_ha_backup": False,
                        "create_release_snapshot": False,
                        "reload_yaml_after_apply": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            server.do_core_check = lambda: None
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}

            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertEqual(state["last_status"], "warning")
            self.assertIn("State changed since this preview was created", state["last_message"])
            self.assertIn("git", state["last_diff"])
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "ha\n")

    def test_confirmed_apply_preserves_live_registry_hidden_fields(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            remote = root / "remote.git"
            seed = root / "seed"
            self.git(["init", "--bare", str(remote)], root)
            self.git(["init", str(seed)], root)
            self.git(["checkout", "-b", "main"], seed)
            seed_storage = seed / "homeassistant" / ".storage"
            seed_storage.mkdir(parents=True)
            (seed_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "name": "Git Device",
                                    "modified_at": "git-old-modified-at",
                                    "sw_version": "1",
                                }
                            ]
                        }
                    }
                )
            )
            self.git_commit_all(seed, "base")
            self.git(["remote", "add", "origin", str(remote)], seed)
            self.git(["push", "-u", "origin", "main"], seed)
            self.push_service_branches(seed)

            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "device-1",
                                    "name": "Live Device",
                                    "modified_at": "live-fresh-modified-at",
                                    "sw_version": "2",
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
            self.assertNotIn("modified_at", state["last_diff"])

            self.select_all_apply_preview_files(server)
            self.assertTrue(server.run_apply_job(), server.read_state()["last_message"])
            saved = json.loads((live_storage / "core.device_registry").read_text())

            self.assertEqual(saved["data"]["devices"][0]["sw_version"], "2")
            self.assertEqual(saved["data"]["devices"][0]["modified_at"], "live-fresh-modified-at")

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

    def test_failed_apply_rolls_back_managed_config_entries_projection(self):
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
                            "options": {"country": "US", "language": "en"},
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

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            with self.assertRaises(RuntimeError):
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

            self.assertEqual(events, ["stop", "check"])
            restored = json.loads((live / ".storage" / "core.config_entries").read_text())
            [entry] = restored["data"]["entries"]
            self.assertEqual(entry["options"]["country"], "US")
            self.assertEqual(entry["options"]["language"], "en")

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

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            self.select_all_apply_preview_files(server)
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
            self.push_service_branches(seed)

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
            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check

            self.select_all_apply_preview_files(server)
            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "live\n")
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")
            self.assertEqual(events, ["stop", "check", "start"])

    def test_apply_failure_after_core_stop_without_release_snapshot_starts_core(self):
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
            self.push_service_branches(seed)

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
                        "create_release_snapshot": False,
                    }
                )
            )
            server.get_installed_addons = lambda: []
            events = []
            server.core_stop = lambda: events.append("stop")

            def fail_check():
                events.append("check")
                raise RuntimeError("bad config")

            server.do_core_check = fail_check
            server.core_start = lambda: events.append("start")

            self.assertTrue(server.run_preview_job(), server.read_state()["last_message"])
            self.select_all_apply_preview_files(server)
            self.assertFalse(server.run_apply_job())
            self.assertEqual((server.CONFIG_DIR / ".storage" / "input_boolean").read_text(), "live-storage\n")
            self.assertEqual(events, ["stop", "check", "start"])
            self.assertIn("Starting Home Assistant Core after failed Apply.", "\n".join(server.read_state()["last_details"]))

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
            self.push_service_branches(seed)

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

            self.select_all_apply_preview_files(server)
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
            self.push_service_branches(seed)

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
            self.select_all_apply_preview_files(server)
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
            original_message = server.web.i18n.EN_TEXT["message.fresh_system_backup_available"]
            server.web.i18n.EN_TEXT["message.fresh_system_backup_available"] = (
                "CATALOG: fresh backup recovered. Run an action when ready."
            )

            try:
                server.write_state(
                    {
                        "last_status": "error",
                        "last_action": "apply",
                        "last_message": "No fresh system backup found within 24 hour(s): No system Home Assistant backups found.",
                    }
                )

                page = server.render_page()
            finally:
                server.web.i18n.EN_TEXT["message.fresh_system_backup_available"] = original_message

            self.assertNotIn(">error<", page)
            self.assertNotIn("No fresh system backup found", page)
            self.assertIn("CATALOG: fresh backup recovered", page)

    def test_render_page_suppresses_stale_successful_config_check_error(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.latest_system_backup_status = lambda options: {"stale": False, "message": "Fresh backup"}
            original_message = server.web.i18n.EN_TEXT["message.stale_config_check_cleared"]
            server.web.i18n.EN_TEXT["message.stale_config_check_cleared"] = (
                "CATALOG: stale config check cleared. Run an action when ready."
            )

            try:
                server.write_state(
                    {
                        "last_status": "error",
                        "last_action": "apply",
                        "last_message": "Home Assistant config check failed: {'result': 'ok', 'data': {}}",
                    }
                )

                page = server.render_page()
            finally:
                server.web.i18n.EN_TEXT["message.stale_config_check_cleared"] = original_message

            self.assertNotIn(">error<", page)
            self.assertNotIn("Home Assistant config check failed", page)
            self.assertIn("CATALOG: stale config check cleared", page)

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
            self.assertIn("<table class='managed-targets-table'>", page)
            self.assertIn("<colgroup><col class='checkbox-col'><col><col><col><col><col></colgroup>", page)
            self.assertIn("<th class='checkbox-col'><span class='sr-only'>Managed</span></th>", page)
            self.assertIn(".sr-only", page)
            self.assertIn("<td class='checkbox-col'><input type='checkbox'", page)
            self.assertIn(".managed-targets-table .checkbox-col", page)
            self.assertIn("Zigbee2MQTT (local_zigbee2mqtt)", page)
            self.assertNotIn("<h2>Managed Apps</h2>", page)
            self.assertNotIn("Protected Storage", page)
            self.assertNotIn("Save App Selection", page)

    def test_primary_actions_are_grouped_by_direction(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            page = server.render_page()

            ha_to_git_section = page.index("<h2>HA to Git</h2>")
            ha_to_git = page.index('action="save-preview"')
            include_redundant = page.index("action='include-redundant-data'")
            git_to_ha_section = page.index("<h2>Git to HA</h2>")
            git_to_ha = page.index('action="preview"')
            reset_git_state_section = page.index("<h2>Reset Git State</h2>")
            reset_git_state = page.index('action="reset-git-state"')
            disk_usage_section = page.index("<h2>Disk Usage</h2>")
            disk_usage = page.index('action="disk-usage"')
            deleted_section = page.index("<h2>Deleted devices and entities</h2>")
            deleted = page.index('action="deleted-devices-preview"')
            retained_section = page.index("<h2>Retained Devices</h2>")
            retained = page.index('action="retained-devices-preview"')
            internal_ids_section = page.index("<h2>Actions IDs</h2>")
            internal_ids = page.index('action="internal-ids-preview"')
            self.assertLess(ha_to_git_section, ha_to_git)
            self.assertLess(ha_to_git, include_redundant)
            self.assertLess(include_redundant, git_to_ha_section)
            self.assertLess(git_to_ha_section, git_to_ha)
            self.assertLess(git_to_ha, reset_git_state_section)
            self.assertLess(reset_git_state_section, reset_git_state)
            self.assertLess(reset_git_state, disk_usage_section)
            self.assertLess(disk_usage_section, disk_usage)
            self.assertLess(disk_usage, deleted_section)
            self.assertLess(deleted_section, deleted)
            self.assertLess(deleted, retained_section)
            self.assertLess(retained_section, retained)
            self.assertLess(retained, internal_ids_section)
            self.assertLess(internal_ids_section, internal_ids)
            self.assertIn('<div class="action-row">', page)
            self.assertIn('<section class="action-section">', page)
            self.assertIn("<h2>Deleted devices and entities</h2>", page)
            self.assertNotIn('<button type="submit" >Save HA to Git</button>', page)
            self.assertNotIn('<button type="submit" >Apply Git to HA</button>', page)
            self.assertIn("Check deleted devices and entities", page)
            self.assertIn("Reset Git State", page)
            self.assertIn("Check disk usage", page)
            self.assertIn("Check actions IDs", page)
            self.assertIn("Previews deleted devices and entities.", page)
            self.assertIn("Rebuilds HA Ops service branches", page)
            self.assertIn("Prints a read-only disk usage summary to the Log", page)
            self.assertIn("Finds stale Zigbee2MQTT MQTT discovery topics.", page)
            self.assertIn("Previews Git-only rewrites", page)
            self.assertIn("Migrate and save selected files to Git, then run Git to HA.", page)
            self.assertNotIn("Check deleted_devices previews", page)
            self.assertNotIn("Check retained devices finds", page)
            self.assertNotIn("Check actions IDs previews", page)
            self.assertNotIn("Check internal ids", page)
            self.assertLess(deleted, retained)
            self.assertLess(retained, internal_ids)
            self.assertNotIn("<h2>Maintenance</h2>", page)
            self.assertIn("action='include-redundant-data'", page)
            self.assertIn("Include redundant data", page)
            self.assertIn(".actions .check-row", page)
            self.assertIn("border-bottom: 0", page)
            self.assertIn(".action-flow", page)
            self.assertIn('<div class="details-header">', page)
            self.assertLess(page.index('data-testid="status-badge"'), page.index("<h2>Log</h2>"))
            self.assertIn("<h2>Log</h2>", page)
            self.assertNotIn("<h2>Last Run Details</h2>", page)
            self.assertNotIn("Preview deletions", page)
            self.assertNotIn("Apply Preview", page)
            self.assertNotIn("Save Preview", page)
            self.assertNotIn("No apply preview yet.", page)
            self.assertNotIn("No save preview yet.", page)
            body_markup = page.split("<script>", 1)[0]
            self.assertNotIn("Deletion of deleted_devices Preview", body_markup)
            self.assertNotIn("Approve Deletion", body_markup)
            self.assertNotIn("Confirm Changes", page)
            self.assertNotIn("Revert Changes", page)

    def test_disk_usage_job_writes_read_only_summary_to_log(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            (server.CONFIG_DIR / "home-assistant_v2.db").write_bytes(b"x" * 2048)
            (server.CONFIG_DIR / "zigbee2mqtt").mkdir()
            (server.CONFIG_DIR / "zigbee2mqtt" / "state.json").write_bytes(b"x" * 1024)
            (server.CONFIG_DIR / ".storage").mkdir()
            (server.CONFIG_DIR / ".storage" / "core.config").write_bytes(b"x" * 256)
            (server.CONFIG_DIR / "custom_components").mkdir()
            (server.CONFIG_DIR / "custom_components" / "demo.py").write_bytes(b"x" * 128)
            (server.CONFIG_DIR / "www").mkdir()
            (server.CONFIG_DIR / "www" / "dashboard.js").write_bytes(b"x" * 64)
            (server.CONFIG_DIR / "home-assistant.log").write_bytes(b"x" * 32)
            (server.DATA_DIR / "state.json").write_text("{}")
            (server.ADDON_CONFIGS_DIR / "9336c2b0_zigbee2mqtt").mkdir()
            (server.ADDON_CONFIGS_DIR / "9336c2b0_zigbee2mqtt" / "configuration.yaml").write_bytes(b"x" * 512)

            def fake_run_command(command, env=None, cwd=None):
                if command[0] == "df":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "\n".join(
                            [
                                "Filesystem 1B-blocks Used Available Use% Mounted on",
                                "/dev/test 10G 6G 4G 60% /data",
                                "/dev/test 10G 6G 4G 60% /data",
                                "",
                            ]
                        ),
                        "",
                    )
                if command[0] == "docker":
                    return subprocess.CompletedProcess(command, 127, "", "docker: not found\n")
                return subprocess.CompletedProcess(command, 1, "", "unexpected command\n")

            server.context().run_command = fake_run_command

            self.assertTrue(server.run_disk_usage_job())
            state = server.read_state()
            details = "\n".join(state["last_details"])

            self.assertEqual(state["last_action"], "disk_usage")
            self.assertEqual(state["last_status"], "success")
            self.assertIn("Disk usage summary (read-only).", details)
            self.assertIn("Storage: 6.0 GB of 10.0 GB", details)
            self.assertIn("  - System: 6.0 GB", details)
            self.assertIn("  - App data:", details)
            self.assertIn("  - Home Assistant:", details)
            self.assertIn("  - Free space: 4.0 GB", details)
            self.assertIn("Visible filesystems (deduplicated):", details)
            self.assertEqual(details.count("/data on /dev/test"), 1)
            self.assertIn("DB", details)
            self.assertIn("zigbee2mqtt", details)
            self.assertIn(".storage", details)
            self.assertIn("custom_components", details)
            self.assertIn("www", details)
            self.assertIn("logs", details)
            self.assertIn("App configs", details)
            self.assertIn("Docker: unavailable from HA Ops App", details)
            self.assertNotIn("System journal:", details)
            self.assertIn("Disk usage summary finished.", details)

    def test_disk_usage_reads_supervisor_host_and_docker_socket_diagnostics(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "homeassistant"
            data_dir = root / "data"
            addon_configs_dir = root / "addon_configs"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()
            docker_payload = {
                "LayersSize": 8 * 1024**3,
                "Images": [{"RepoTags": ["homeassistant/aarch64-addon:latest"], "Size": 512 * 1024**2}],
                "Containers": [{"SizeRw": 128 * 1024**2}],
                "Volumes": [{"Name": "zigbee2mqtt-data", "UsageData": {"Size": 256 * 1024**2}}],
                "BuildCache": [{"Size": 64 * 1024**2}],
            }

            def fake_run_command(command, env=None, cwd=None):
                if command[0] == "df":
                    return subprocess.CompletedProcess(command, 0, "Filesystem Size Used Avail Use% Mounted on\n", "")
                return subprocess.CompletedProcess(command, 1, "", "unexpected command\n")

            def fake_call_supervisor(method, path, payload=None):
                self.assertEqual((method, path), ("GET", "/host/info"))
                return {"data": {"disk_total": "13.6 GB", "disk_used": "11.9 GB", "disk_free": "1.1 GB"}}

            lines = server.app_context.disk_usage.build_disk_usage_summary(
                config_dir,
                data_dir,
                addon_configs_dir,
                backup_dir,
                fake_run_command,
                fake_call_supervisor,
                root / "unused-docker.sock",
                lambda: docker_payload,
            )
            details = "\n".join(lines)

            self.assertIn("Host:", details)
            self.assertIn("disk_used: 11.9 GB", details)
            self.assertIn("Docker:", details)
            self.assertIn("Layers: 8.0 GB", details)
            self.assertIn("Images: 1 image(s), 512.0 MB total image size.", details)
            self.assertIn("homeassistant/aarch64-addon:latest", details)
            self.assertIn("Containers: 1 container(s), 128.0 MB writable.", details)
            self.assertIn("Volumes: 1 volume(s), 256.0 MB.", details)
            self.assertIn("Build cache: 1 item(s), 64.0 MB.", details)

    def test_disk_usage_counts_shared_backing_filesystem_once(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            data_dir = root / "data"
            addon_configs_dir = root / "addon"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()

            def fake_run_command(command, env=None, cwd=None):
                if command[0] == "df":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "\n".join(
                            [
                                "Filesystem 1B-blocks Used Available Use% Mounted on",
                                "/dev/root 100G 60G 40G 60% /config",
                                "/dev/root 100G 60G 40G 60% /data",
                                "/dev/root 100G 60G 40G 60% /addon",
                                "/dev/root 100G 60G 40G 60% /backup",
                                "",
                            ]
                        ),
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            lines = server.app_context.disk_usage.build_disk_usage_summary(
                config_dir,
                data_dir,
                addon_configs_dir,
                backup_dir,
                fake_run_command,
                docker_socket_path=root / "missing-docker.sock",
            )
            details = "\n".join(lines)

            self.assertIn("Storage: 60.0 GB of 100.0 GB", details)
            self.assertIn("  - Free space: 40.0 GB", details)
            self.assertNotIn("Storage: 240.0 GB of 400.0 GB", details)
            self.assertEqual(details.count(" on /dev/root:"), 1)

    def test_disk_usage_mapped_path_summary_reports_partial_when_walk_is_bounded(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "homeassistant"
            data_dir = root / "data"
            addon_configs_dir = root / "addon_configs"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()
            for index in range(5):
                (config_dir / f"file-{index}.txt").write_bytes(b"x" * 1024)

            def fake_run_command(command, env=None, cwd=None):
                return subprocess.CompletedProcess(command, 0, "", "")

            lines = server.app_context.disk_usage.build_disk_usage_summary(
                config_dir,
                data_dir,
                addon_configs_dir,
                backup_dir,
                fake_run_command,
                docker_socket_path=root / "missing-docker.sock",
                path_walk_max_entries=2,
            )
            details = "\n".join(lines)

            self.assertIn("  - Home Assistant:", details)
            self.assertIn("partial: stopped before full traversal", details)
            self.assertIn("(entries)", details)

    def test_disk_usage_optional_sections_time_out_individually(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "homeassistant"
            data_dir = root / "data"
            addon_configs_dir = root / "addon_configs"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()

            def slow_run_command(command, env=None, cwd=None):
                time.sleep(0.2)
                return subprocess.CompletedProcess(command, 0, f"{command[0]} completed\n", "")

            def slow_call_supervisor(method, path, payload=None):
                time.sleep(0.2)
                return {"data": {"disk_used": "11.9 GB"}}

            def slow_docker_system_df():
                time.sleep(0.2)
                return {"LayersSize": 0, "Images": [], "Containers": [], "Volumes": [], "BuildCache": []}

            started = time.monotonic()
            lines = server.app_context.disk_usage.build_disk_usage_summary(
                config_dir,
                data_dir,
                addon_configs_dir,
                backup_dir,
                slow_run_command,
                slow_call_supervisor,
                root / "missing-docker.sock",
                slow_docker_system_df,
                optional_timeout_seconds=0.01,
            )
            elapsed = time.monotonic() - started
            details = "\n".join(lines)

            self.assertLess(elapsed, 0.15)
            self.assertIn("Storage: unavailable from HA Ops App (timed out after 0.01s).", details)
            self.assertIn("Host: unavailable from HA Ops App (timed out after 0.01s).", details)
            self.assertIn("Docker: unavailable from HA Ops App (timed out after 0.01s).", details)
            self.assertNotIn("System journal:", details)

    def test_disk_usage_treats_docker_socket_errors_as_optional(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "homeassistant"
            data_dir = root / "data"
            addon_configs_dir = root / "addon_configs"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()

            def fake_run_command(command, env=None, cwd=None):
                return subprocess.CompletedProcess(command, 0, "", "")

            original_docker_system_df = server.app_context.disk_usage._docker_system_df
            server.app_context.disk_usage._docker_system_df = lambda socket_path: (_ for _ in ()).throw(
                PermissionError("socket denied")
            )
            try:
                lines = server.app_context.disk_usage.build_disk_usage_summary(
                    config_dir,
                    data_dir,
                    addon_configs_dir,
                    backup_dir,
                    fake_run_command,
                )
            finally:
                server.app_context.disk_usage._docker_system_df = original_docker_system_df
            details = "\n".join(lines)

            self.assertIn("Disk usage summary (read-only).", details)
            self.assertIn("Docker: unavailable from HA Ops App (socket denied).", details)

    def test_disk_usage_treats_unexpected_docker_payload_as_optional(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "homeassistant"
            data_dir = root / "data"
            addon_configs_dir = root / "addon_configs"
            backup_dir = root / "backup"
            for path in (config_dir, data_dir, addon_configs_dir, backup_dir):
                path.mkdir()

            def fake_run_command(command, env=None, cwd=None):
                return subprocess.CompletedProcess(command, 0, "", "")

            lines = server.app_context.disk_usage.build_disk_usage_summary(
                config_dir,
                data_dir,
                addon_configs_dir,
                backup_dir,
                fake_run_command,
                docker_system_df=lambda: {"Images": "not-a-list"},
            )
            details = "\n".join(lines)

            self.assertIn("Disk usage summary (read-only).", details)
            self.assertIn(
                "Docker: unavailable from HA Ops App (Docker API response has an unexpected schema.).",
                details,
            )

    def test_docker_socket_connection_has_short_timeout(self):
        server = load_server()

        connection = server.app_context.disk_usage._UnixSocketHTTPConnection(Path("/tmp/docker.sock"))

        self.assertEqual(connection.timeout, 5)

    def test_disk_usage_action_is_disabled_while_job_is_running(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []

            server.context().run_lock.acquire()
            try:
                page = server.render_page()
            finally:
                server.context().run_lock.release()

            disk_form_start = page.index('action="disk-usage"')
            disk_form = page[disk_form_start : page.index("</form>", disk_form_start)]
            self.assertIn('<button type="submit" class="secondary" disabled>Check disk usage</button>', disk_form)

    def test_disk_usage_post_is_rejected_while_job_is_running(self):
        server = load_server()

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.state = {"last_status": "running", "last_message": "Another job is active."}
                self.state_updates = []
                self.run_lock = threading.Lock()

            def read_state(self):
                return dict(self.state)

            def write_state(self, updates):
                self.state_updates.append(updates)
                self.state.update(updates)

            def run_disk_usage_job(self, lock_acquired=False):
                self.calls.append(("disk-usage", lock_acquired))
                if lock_acquired:
                    self.run_lock.release()

        ctx = FakeContext()
        handler = server.web.create_handler(ctx)
        request = handler.__new__(handler)
        request.path = "/disk-usage"
        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = Message()
        request.headers["Accept"] = "application/json"
        request.headers["X-Requested-With"] = "fetch"
        request.responses = []
        request.response_headers = []
        request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
        request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
        request.end_headers = MethodType(lambda self: None, request)

        expected_state = dict(ctx.state)
        ctx.run_lock.acquire()
        try:
            request.do_POST()
        finally:
            ctx.run_lock.release()

        self.assertEqual(request.responses[-1], 409)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(ctx.state, expected_state)
        self.assertEqual(ctx.state_updates, [])
        response = json.loads(request.wfile.getvalue().decode())
        self.assertFalse(response["ok"])
        self.assertIn("already running", response["message"])

    def test_disk_usage_action_stays_enabled_during_pending_deleted_devices(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(root / "missing-rollback"),
                }
            )

            page = server.render_page()

            disk_form_start = page.index('action="disk-usage"')
            disk_form = page[disk_form_start : page.index("</form>", disk_form_start)]
            deleted_form_start = page.index('action="deleted-devices-preview"')
            deleted_form = page[deleted_form_start : page.index("</form>", deleted_form_start)]
            self.assertNotIn("disabled", disk_form)
            self.assertIn("disabled", deleted_form)

    def test_deleted_devices_preview_lists_entities_as_grid_rows(self):
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

            self.assertEqual(state["last_deleted_devices_count"], 2)
            self.assertEqual(
                state["last_deleted_devices_rows"],
                [
                    {
                        "area": "Bathroom",
                        "entity_id": "sensor.bathroom_presence_illuminance",
                        "original_name": "Illuminance",
                        "original_device_class": "illuminance",
                        "id": "deleted-1",
                    },
                    {
                        "area": "Bathroom",
                        "id": "",
                        "entity_id": "sensor.bathroom_presence_illuminance",
                        "original_name": "Illuminance",
                        "original_device_class": "illuminance",
                        "kind": "deleted_entity",
                    },
                ],
            )
            page = server.render_page()
            table_start = page.index("<div class='deleted-devices-table'>")
            table = page[table_start : page.index("</section>", table_start)]
            self.assertIn("<div class='deleted-device-header'>", table)
            self.assertIn("deleted-device-line deleted-device-line-primary", table)
            self.assertIn("deleted-device-line deleted-device-line-secondary", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-area'>Area</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-id'>ID</div>", table)
            self.assertNotIn("<table class='deleted-devices-table'>", table)
            self.assertNotIn("<colgroup>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-entity-id", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-device", table)
            self.assertNotIn("deleted-device-header-cell deleted-device-col-original-device-class", table)
            self.assertIn("sensor.bathroom_presence_illuminance", table)
            self.assertIn("Illuminance", table)
            self.assertIn("deleted-1", table)
            self.assertIn("Approve Deletion", table)
            self.assertNotIn("identifiers=mqtt:old", table)

    def test_deleted_devices_table_keeps_grid_columns_aligned(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            server.write_state(
                {
                    "last_deleted_devices_generated_at": "2026-06-27T07:37:13+00:00",
                    "last_deleted_devices_count": 4,
                    "last_deleted_devices_rows": [
                        {
                            "id": "deleted-1",
                            "recovered_name": "Living Room Motion",
                            "recovered_manufacturer": "Aqara",
                            "recovered_model": "Motion sensor",
                            "recovered_model_id": "RTCGQ11LM",
                            "recovered_identifiers": [["mqtt", "zigbee2mqtt_0x00158d0001"]],
                            "source_commit": "0123456789abcdef0123456789abcdef01234567",
                            "source_path": "homeassistant/.storage/core.device_registry",
                        },
                        {
                            "id": "deleted-2",
                            "recovered_name": "Kitchen Button",
                        },
                        {
                            "id": "deleted-3",
                            "recovered_model": "Smart plug",
                            "recovered_model_id": "TS011F_plug_3",
                        },
                        {
                            "id": "deleted-4",
                            "recovered_manufacturer": "Tuya",
                        },
                    ],
                }
            )

            page = server.render_page()
            table_start = page.index("<div class='deleted-devices-table'>")
            table = page[table_start : page.index("</section>", table_start)]

            self.assertIn("min-width: 1200px", page)
            self.assertIn(
                "grid-template-columns: minmax(32ch, 1fr) minmax(18ch, 0.7fr) minmax(42ch, 1.4fr) minmax(34ch, 1.2fr)",
                page,
            )
            self.assertIn("column-gap: 0", page)
            self.assertIn("padding: 8px 0", page)
            self.assertIn("padding: 0 12px", page)
            self.assertNotIn(".deleted-device-line + .deleted-device-line", page)
            self.assertIn(".deleted-device-cell.deleted-device-col-id code", page)
            self.assertIn(".deleted-device-cell.deleted-device-col-identifiers code", page)
            self.assertIn("white-space: nowrap", page)
            generic_code_rule = page.index(".deleted-device-cell code")
            nowrap_rule = page.index(".deleted-device-cell.deleted-device-col-id code")
            self.assertLess(generic_code_rule, nowrap_rule)
            self.assertIn("deleted-device-line deleted-device-line-primary", table)
            self.assertIn("deleted-device-line deleted-device-line-secondary", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-area'>Area</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-id'>ID</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-entity-id'>Entity ID</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-name'>Name</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-original-name'>Original Name</div>", table)
            self.assertIn(
                "deleted-device-header-cell deleted-device-col-device'>Manufacturer and Model</div>",
                table,
            )
            self.assertNotIn("deleted-device-header-cell deleted-device-col-manufacturer", table)
            self.assertNotIn("deleted-device-header-cell deleted-device-col-model", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-identifiers'>Identifiers</div>", table)
            self.assertIn("deleted-device-header-cell deleted-device-col-source'>Source</div>", table)
            self.assertNotIn("deleted-device-header-cell deleted-device-col-original-device-class", table)
            primary_header_start = table.index("deleted-device-line deleted-device-line-primary")
            secondary_header_start = table.index("deleted-device-line deleted-device-line-secondary")
            self.assertLess(primary_header_start, secondary_header_start)
            primary_headers = [
                table.index("deleted-device-header-cell deleted-device-col-id", primary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-original-name", primary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-area", primary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-device", primary_header_start),
            ]
            secondary_headers = [
                table.index("deleted-device-header-cell deleted-device-col-identifiers", secondary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-name", secondary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-entity-id", secondary_header_start),
                table.index("deleted-device-header-cell deleted-device-col-source", secondary_header_start),
            ]
            self.assertEqual(primary_headers, sorted(primary_headers))
            self.assertEqual(secondary_headers, sorted(secondary_headers))
            self.assertNotIn("<table", table)
            self.assertNotIn("<colgroup", table)
            self.assertIn("Living Room Motion", table)
            self.assertIn("Aqara<br>Motion sensor / RTCGQ11LM", table)
            self.assertIn(">Smart plug / TS011F_plug_3</div>", table)
            self.assertIn(">Tuya</div>", table)
            self.assertIn("mqtt:zigbee2mqtt_0x00158d0001", table)
            self.assertIn("0123456789ab homeassistant/.storage/core.device_registry", table)
            self.assertIn("deleted-device-col-source", table)
            self.assertIn("deleted-device-cell-identifiers", table)
            self.assertIn("deleted-device-cell-device", table)

    def test_stale_mqtt_discovery_preview_finds_registry_device_missing_from_z2m(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (server.CONFIG_DIR / "zigbee2mqtt").mkdir()
            (server.CONFIG_DIR / "zigbee2mqtt" / "database.db").write_text('[{"ieeeAddr":"0x0017880104abcd12"}]')
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "stale-device",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0xabc123fffed45678"]],
                                    "name": "Detached Button",
                                    "manufacturer": "Example",
                                    "model": "Battery button",
                                },
                                {
                                    "id": "current-device",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0x0017880104abcd12"]],
                                    "name": "Current Bulb",
                                },
                            ]
                        }
                    }
                )
            )

            preview = server.app_context.registry_cleanup.build_stale_mqtt_discovery_preview(
                server.CONFIG_DIR,
                [
                    "homeassistant/device_automation/0xabc123fffed45678/action_double/config",
                    "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
                    "homeassistant/device_automation/0x0017880104abcd12/action_hold/config",
                ],
            )

            self.assertEqual(preview["count"], 1)
            self.assertEqual(preview["candidates"][0]["ieee"], "0xabc123fffed45678")
            self.assertEqual(
                preview["candidates"][0]["retained_topics"],
                [
                    "homeassistant/device_automation/0xabc123fffed45678/action_double/config",
                    "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
                ],
            )
            self.assertIn("Detached Button", preview["summary"])
            self.assertIn("retained Home Assistant MQTT discovery topics", preview["summary"])
            self.assertIn("does not delete files or registry/database records", preview["summary"])
            self.assertNotIn("Current Bulb", preview["summary"])

    def test_retained_devices_preview_explains_topic_only_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_retained_devices_generated_at": "2026-05-22T12:00:00+00:00",
                    "last_retained_devices_rows": [
                        {
                            "selected": True,
                            "identifiers": ["mqtt", "zigbee2mqtt_0xabc123fffed45678"],
                            "name": "detached_button",
                            "manufacturer": "Example",
                            "model": "Battery button",
                            "retained_topics": [
                                "homeassistant/device_automation/0xabc123fffed45678/action_hold/config"
                            ],
                            "identity": "row-identity",
                        }
                    ],
                    "last_retained_devices_fingerprint": "retained-fingerprint",
                }
            )

            page = server.render_page()

            self.assertIn("stale retained Home Assistant MQTT discovery topics", page)
            self.assertIn("clears selected MQTT retained discovery topics only", page)
            self.assertIn("does not delete files", page)
            self.assertIn("does not delete files or registry/database records", page)
            self.assertIn("<colgroup><col class='checkbox-col'>", page)
            self.assertIn("<th class='checkbox-col' aria-label='Delete'></th>", page)
            self.assertIn("name='retained_preview_fingerprint' value='retained-fingerprint'", page)
            self.assertIn("name='candidate' value='row-identity'", page)
            self.assertIn(".retained-devices-table .checkbox-col", page)
            self.assertIn("width: 42px;", page)

    def test_retained_devices_fingerprint_covers_topics_and_scanned_z2m_context(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            z2m = server.CONFIG_DIR / "zigbee2mqtt"
            storage.mkdir()
            z2m.mkdir()
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "stale-device",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0xabc123fffed45678"]],
                                    "name": "Detached Button",
                                },
                            ]
                        }
                    }
                )
            )

            preview_a = server.app_context.registry_cleanup.build_stale_mqtt_discovery_preview(
                server.CONFIG_DIR,
                ["homeassistant/device_automation/0xabc123fffed45678/action_hold/config"],
            )
            preview_b = server.app_context.registry_cleanup.build_stale_mqtt_discovery_preview(
                server.CONFIG_DIR,
                ["homeassistant/device_automation/0xabc123fffed45678/action_double/config"],
            )
            (z2m / "state.json").write_text('[{"ieee_address":"0x0017880104abcd12"}]')
            preview_c = server.app_context.registry_cleanup.build_stale_mqtt_discovery_preview(
                server.CONFIG_DIR,
                ["homeassistant/device_automation/0xabc123fffed45678/action_hold/config"],
            )

            self.assertNotEqual(preview_a["fingerprint"], preview_b["fingerprint"])
            self.assertNotEqual(preview_a["fingerprint"], preview_c["fingerprint"])
            self.assertEqual(preview_a["device_registry_fingerprint"], preview_b["device_registry_fingerprint"])
            self.assertEqual(preview_a["device_registry_fingerprint"], preview_c["device_registry_fingerprint"])
            self.assertTrue(preview_a["candidates"][0]["identity"])

    def test_retained_devices_delete_rejects_stale_preview_identity_before_clearing_topics(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            cleared = []
            server._CTX.clear_retained_discovery_topic = cleared.append
            row_a = {
                "identity": "identity-a",
                "selected": True,
                "retained_topics": ["homeassistant/device_automation/0xaaaabbbbccccdddd/action_hold/config"],
            }
            row_b = {
                "identity": "identity-b",
                "selected": True,
                "retained_topics": ["homeassistant/device_automation/0xffffbbbbccccdddd/action_hold/config"],
            }
            server.write_state(
                {
                    "last_retained_devices_rows": [row_a],
                    "last_retained_devices_count": 1,
                    "last_retained_devices_fingerprint": "fingerprint-a",
                    "last_retained_devices_generated_at": "2026-08-31T10:00:00+00:00",
                }
            )
            submitted = {
                "candidate": ["identity-a"],
                "retained_preview_fingerprint": ["fingerprint-a"],
                "retained_preview_generated_at": ["2026-08-31T10:00:00+00:00"],
            }
            server.write_state(
                {
                    "last_retained_devices_rows": [row_b],
                    "last_retained_devices_count": 1,
                    "last_retained_devices_fingerprint": "fingerprint-b",
                    "last_retained_devices_generated_at": "2026-08-31T10:01:00+00:00",
                }
            )

            self.assertFalse(server._CTX.run_retained_devices_delete_job(submitted))
            state = server.read_state()
            self.assertEqual(cleared, [])
            self.assertEqual(state["last_message"], "Retained devices preview changed. Run Check retained devices again.")
            self.assertEqual(state["last_retained_devices_rows"], [row_b])

    def test_retained_devices_delete_uses_stable_identity_not_row_index(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            cleared = []
            server._CTX.clear_retained_discovery_topic = cleared.append
            server.write_state(
                {
                    "last_retained_devices_rows": [
                        {
                            "identity": "identity-a",
                            "selected": True,
                            "retained_topics": ["homeassistant/device_automation/0xaaaabbbbccccdddd/action_hold/config"],
                        }
                    ],
                    "last_retained_devices_count": 1,
                    "last_retained_devices_fingerprint": "fingerprint-a",
                    "last_retained_devices_generated_at": "2026-08-31T10:00:00+00:00",
                }
            )

            self.assertTrue(
                server._CTX.run_retained_devices_delete_job(
                    {
                        "candidate": ["identity-a"],
                        "retained_preview_fingerprint": ["fingerprint-a"],
                        "retained_preview_generated_at": ["2026-08-31T10:00:00+00:00"],
                    }
                )
            )
            self.assertEqual(cleared, ["homeassistant/device_automation/0xaaaabbbbccccdddd/action_hold/config"])

    def test_pending_deleted_cleanup_blocks_cleanup_jobs_http_and_websocket_dispatch(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"deleted_devices_pending_confirmation": True})

            job_cases = [
                ("retained preview", lambda: server._CTX.run_retained_devices_preview_job()),
                ("retained delete", lambda: server._CTX.run_retained_devices_delete_job({})),
                ("internal preview", lambda: server._CTX.run_internal_ids_preview_job()),
                ("internal migrate", lambda: server._CTX.run_internal_ids_migrate_job(["0"])),
            ]
            for label, action in job_cases:
                with self.subTest(boundary="job", label=label):
                    self.assertFalse(action())

            for route in ("/retained-devices-preview", "/retained-devices-delete", "/internal-ids-preview", "/internal-ids-migrate"):
                with self.subTest(boundary="http", route=route):
                    response = self.post_json(server, route)
                    self.assertEqual(response.responses[-1], 409)

            scheduled = []

            def start_job(target, *args, **kwargs):
                scheduled.append(target.__name__)
                return True

            for command in ("retained_devices_preview", "retained_devices_delete", "internal_ids_preview", "internal_ids_migrate"):
                with self.subTest(boundary="websocket", command=command):
                    result = server.web.dispatch_command(server.context(), command, start_job=start_job)
                    self.assertFalse(result["ok"])
            self.assertEqual(scheduled, [])

    def test_clear_stale_mqtt_discovery_topics_publishes_empty_retained_payloads(self):
        server = load_server()
        published = []

        cleared = server.app_context.registry_cleanup.clear_stale_mqtt_discovery_topics(
            [
                "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
                "homeassistant/device_automation/0xabc123fffed45678/action_double/config",
                "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
            ],
            published.append,
        )

        self.assertEqual(
            cleared,
            [
                "homeassistant/device_automation/0xabc123fffed45678/action_double/config",
                "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
            ],
        )
        self.assertEqual(published, cleared)

    def test_retained_mqtt_discovery_uses_direct_mosquitto_client(self):
        server = load_server()
        commands = []

        def run_command(command):
            commands.append(command)
            return subprocess.CompletedProcess(
                command,
                124,
                stdout="homeassistant/device_automation/0xabc123fffed45678/action_hold/config {}\n",
                stderr="",
            )

        topics = server.app_context.registry_cleanup.list_retained_discovery_topics(
            run_command,
            {"host": "mqtt.local", "port": 1884, "username": "ha-ops", "password": "secret"},
        )

        self.assertEqual(topics, ["homeassistant/device_automation/0xabc123fffed45678/action_hold/config"])
        command = commands[0]
        self.assertEqual(
            command,
            [
                "timeout",
                "8",
                "mosquitto_sub",
                "-h",
                "mqtt.local",
                "-p",
                "1884",
                "-u",
                "ha-ops",
                "-P",
                "secret",
                "-t",
                "homeassistant/#",
                "-v",
            ],
        )
        self.assertNotIn("docker", command)
        self.assertNotIn("/data/system_user.json", command)

    def test_retained_mqtt_cleanup_uses_direct_mosquitto_client(self):
        server = load_server()
        commands = []

        def run_command(command):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        server.app_context.registry_cleanup.publish_empty_retained_topic(
            run_command,
            "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
            {"host": "mqtt.local", "port": 1884, "username": "ha-ops", "password": "secret"},
        )

        command = commands[0]
        self.assertEqual(
            command,
            [
                "mosquitto_pub",
                "-h",
                "mqtt.local",
                "-p",
                "1884",
                "-u",
                "ha-ops",
                "-P",
                "secret",
                "-r",
                "-n",
                "-t",
                "homeassistant/device_automation/0xabc123fffed45678/action_hold/config",
            ],
        )
        self.assertNotIn("docker", command)
        self.assertNotIn("/data/system_user.json", command)

    def test_retained_mqtt_discovery_gets_credentials_from_supervisor_service(self):
        server = load_server()
        commands = []
        calls = []
        ctx = server.app_context.AppContext()
        ctx.call_supervisor = lambda method, path, payload=None: (
            calls.append((method, path)),
            {"host": "mqtt.local", "port": 1884, "username": "ha-ops", "password": "secret"},
        )[1]
        ctx.run_command = lambda command, env=None, cwd=None: (
            commands.append(command),
            subprocess.CompletedProcess(command, 124, stdout="", stderr=""),
        )[1]

        ctx.list_retained_discovery_topics()

        self.assertEqual(calls, [("GET", "/services/mqtt")])
        self.assertIn("-u", commands[0])
        self.assertIn("ha-ops", commands[0])

    def test_retained_mqtt_service_errors_are_explicit(self):
        server = load_server()

        with self.assertRaisesRegex(RuntimeError, "No access to mqtt service"):
            server.app_context.supervisor.mqtt_service(
                lambda method, path: {"result": "error", "message": "No access to mqtt service!"}
            )

    def test_addon_declares_mqtt_service_dependency(self):
        config = (ROOT / "config.yaml").read_text(encoding="utf-8")

        self.assertIn("services:", config)
        self.assertIn("  - mqtt:need", config)

    def test_addon_declares_homeassistant_api_access(self):
        config = (ROOT / "config.yaml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("hassio_api: true", config)
        self.assertIn("homeassistant_api: true", config)
        self.assertIn("docker_api: true", config)
        self.assertIn("Docker API capability is broad", readme)
        self.assertIn("/system/df", readme)

    def test_internal_ids_preview_and_migrate_use_z2m_friendly_name(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = server.DATA_DIR / "ha-config"
            config = repo / "homeassistant"
            storage = config / ".storage"
            area = config / ".ha-ops" / "areas" / "office"
            z2m = config / "zigbee2mqtt"
            storage.mkdir(parents=True)
            area.mkdir(parents=True)
            z2m.mkdir(parents=True)
            server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))
            (storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [
                                {
                                    "id": "11111111111111111111111111111111",
                                    "entity_id": "switch.office_button",
                                    "device_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                }
                            ]
                        }
                    }
                )
            )
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0x00124b00226b31f8"]],
                                    "name": "old_registry_name",
                                }
                            ]
                        }
                    }
                )
            )
            (z2m / "state.json").write_text(
                json.dumps(
                    [
                        {
                            "ieee_address": "0x00124b00226b31f8",
                            "friendly_name": "office_remote_new",
                        }
                    ]
                )
            )
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: Synthetic button
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions:
  - type: turn_on
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    entity_id: '11111111111111111111111111111111'
    domain: switch
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_internal_ids_count"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["mqtt_triggers"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["actions"], 1)
            self.assertIn(
                "--- .ha-ops/areas/office/automations.yaml before internal id migration",
                state["last_internal_ids_rows"][0]["diff"],
            )
            self.assertIn("topic: z2m/office_remote_new", state["last_internal_ids_rows"][0]["diff"])

            page = server.render_page()
            self.assertIn("Check actions IDs", page)
            self.assertIn("Migrate and Save to Git", page)
            self.assertIn("Internal IDs Migration Preview", page)
            self.assertIn("Files: 1. Candidates: 2. Unresolved: 0.", page)
            self.assertIn("Select All", page)
            self.assertIn("Select None", page)
            self.assertIn("<div class='internal-ids-list' data-checkbox-scope='internal-ids'>", page)
            self.assertIn("<div class='internal-id-header'>", page)
            self.assertIn("<span></span><span>Migrate</span><span>File</span><span>Candidates</span><span>Unresolved</span>", page)
            self.assertIn("<details class='internal-id-row'>", page)
            self.assertNotIn("<th>Entity</th>", page)
            self.assertNotIn("<th>Z2M</th>", page)
            self.assertNotIn("<th>Action refs</th>", page)
            self.assertNotIn("<th>Condition refs</th>", page)
            self.assertIn("<span class='file-col'><code>.ha-ops/areas/office/automations.yaml</code></span>", page)
            self.assertIn("<span class='metric-col'>2</span>", page)
            self.assertIn(".internal-id-summary .file-col", page)
            self.assertIn("text-overflow: ellipsis", page)
            self.assertIn("white-space: nowrap", page)
            self.assertIn("grid-template-columns: 24px 82px minmax(0, 1fr) 96px 96px", page)
            self.assertIn(".internal-id-row summary::before", page)
            self.assertIn(".internal-id-summary {\n      display: contents;", page)
            reactive_script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
            self.assertIn('this.querySelectorAll(`[data-checkbox-scope="${button.dataset.checkboxScope}"] input[type="checkbox"]`)', reactive_script)
            self.assertNotIn("View diff:", page)
            self.assertNotIn("<details open><summary><code>.ha-ops/areas/office/automations.yaml</code></summary>", page)
            self.assertIn("run Preview Git to HA", page)
            self.assertIn(".ha-ops/areas/office/automations.yaml after internal id migration", page)

            server.write_state({"save_push_retry_pending": True, "save_push_retry_commit": "pending-save"})
            self.assertFalse(server.run_internal_ids_migrate_job(["0"]))
            self.assertIn("Save push retry is still pending", server.read_state()["last_message"])
            self.assertIn("device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", automation.read_text())
            server.write_state({"save_push_retry_pending": False, "save_push_retry_commit": None})

            self.assertTrue(server.run_internal_ids_migrate_job(["0"]))
            migrated = automation.read_text()
            self.assertIn("topic: z2m/office_remote_new", migrated)
            self.assertIn("value_template: '{{ trigger.payload_json.action == ''1_single'' }}'", migrated)
            self.assertIn("action: switch.turn_on", migrated)
            self.assertNotIn("device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", migrated)

            self.assertTrue(server.run_internal_ids_preview_job())
            self.assertEqual(server.read_state()["last_internal_ids_count"], 0)

    def test_internal_ids_preview_uses_split_zigbee2mqtt_addon_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = server.DATA_DIR / "ha-config"
            config = repo / "homeassistant"
            storage = config / ".storage"
            area = config / ".ha-ops" / "areas" / "office"
            addon = repo / "addons" / "local_zigbee2mqtt"
            storage.mkdir(parents=True)
            area.mkdir(parents=True)
            addon.mkdir(parents=True)
            server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))
            server.write_state({"managed_addons": ["local_zigbee2mqtt"]})
            server.get_installed_addons = lambda: [{"slug": "local_zigbee2mqtt", "name": "Zigbee2MQTT"}]
            (storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0x00124b00226b31f8"]],
                                    "name": "old_registry_name",
                                }
                            ]
                        }
                    }
                )
            )
            (addon / "configuration.yaml").write_text(
                """
devices:
  '0x00124b00226b31f8':
    friendly_name: split_remote
""".lstrip()
            )
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: Split Z2M button
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions:
  - action: script.synthetic
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_internal_ids_count"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["mqtt_triggers"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["unresolved"], 0)
            self.assertIn("topic: z2m/split_remote", state["last_internal_ids_rows"][0]["diff"])

    def test_internal_ids_preview_uses_live_zigbee2mqtt_context(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = server.DATA_DIR / "ha-config"
            config = repo / "homeassistant"
            storage = config / ".storage"
            area = config / ".ha-ops" / "areas" / "office"
            live_z2m = server.CONFIG_DIR / "zigbee2mqtt"
            storage.mkdir(parents=True)
            area.mkdir(parents=True)
            live_z2m.mkdir(parents=True)
            server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))
            (storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0x00124b00226b31f8"]],
                                    "name": "old_registry_name",
                                }
                            ]
                        }
                    }
                )
            )
            (live_z2m / "state.json").write_text(
                json.dumps(
                    [
                        {
                            "ieee_address": "0x00124b00226b31f8",
                            "friendly_name": "live_remote",
                        }
                    ]
                )
            )
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: Live Z2M button
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions:
  - action: script.synthetic
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_internal_ids_count"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["mqtt_triggers"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["unresolved"], 0)
            self.assertIn("topic: z2m/live_remote", state["last_internal_ids_rows"][0]["diff"])

    def test_internal_ids_preview_skips_stale_z2m_registry_device(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            repo = server.DATA_DIR / "ha-config"
            config = repo / "homeassistant"
            storage = config / ".storage"
            area = config / ".ha-ops" / "areas" / "terrace"
            z2m = config / "zigbee2mqtt"
            storage.mkdir(parents=True)
            area.mkdir(parents=True)
            z2m.mkdir(parents=True)
            server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))
            (storage / "core.entity_registry").write_text(json.dumps({"data": {"entities": []}}))
            (storage / "core.device_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "devices": [
                                {
                                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                    "identifiers": [["mqtt", "zigbee2mqtt_0x60a423fffed229de"]],
                                    "name": "living_room_switcher_terrace",
                                }
                            ]
                        }
                    }
                )
            )
            (z2m / "state.json").write_text(
                json.dumps(
                    [
                        {
                            "ieee_address": "0x00124b00226b31f8",
                            "friendly_name": "current_remote",
                        }
                    ]
                )
            )
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: terrace_light
  trigger:
  - platform: device
    domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: single
  condition: []
  action:
  - service: switch.toggle
    target:
      entity_id: switch.terrace_light
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_internal_ids_count"], 0)
            self.assertEqual(len(state["last_internal_ids_rows"]), 1)
            row = state["last_internal_ids_rows"][0]
            self.assertFalse(row["selected"])
            self.assertEqual(row["changes"], 0)
            self.assertEqual(row["unresolved"], 1)
            self.assertEqual(row["diff"], "")
            self.assertIn("check retained devices first", row["unresolved_items"][0]["reason"])
            self.assertNotIn("z2m/living_room_switcher_terrace", state["last_internal_ids_preview"])
            self.assertNotIn("z2m/living_room_switcher_terrace", server.render_page())
            self.assertEqual(automation.read_text().count("device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), 1)

    def test_internal_ids_preview_running_state_does_not_duplicate_detail_message(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            seen = {}

            def fake_preview():
                seen["state"] = server.read_state()
                seen["page"] = server.render_page()
                return {
                    "count": 0,
                    "rows": [],
                    "unresolved": [],
                    "fingerprint": "synthetic",
                    "summary": "No safe internal id migrations found.",
                }

            server.context().build_internal_ids_preview = fake_preview

            self.assertTrue(server.run_internal_ids_preview_job())
            self.assertEqual(seen["state"]["last_message"], "Checking internal ids.")
            self.assertEqual(seen["state"]["last_details"], ["Checking internal ids."])
            self.assertEqual(
                seen["page"].count("Checking HA Ops automations, scripts, and scenes for safe internal id migrations."),
                0,
            )

    def test_running_action_log_does_not_add_duplicate_context_details(self):
        jobs_source = (ROOT / "app" / "jobs.py").read_text()

        duplicate_detail_keys = [
            "detail.checking_deleted_devices",
            "detail.checking_internal_ids",
            "detail.checking_retained_devices",
            "detail.resetting_git_state",
        ]
        for key in duplicate_detail_keys:
            with self.subTest(key=key):
                self.assertNotIn(key, jobs_source)

    def test_internal_ids_preview_log_keeps_check_before_result(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_message"], "")
            self.assertEqual(
                state["last_details"],
                [
                    "Checking internal ids.",
                    "Found 0 internal id migration files.",
                ],
            )
            self.assertNotIn("Checking HA Ops automations, scripts, and scenes for safe internal id migrations.", state["last_details"])
            self.assertLess(
                page.index("Checking internal ids."),
                page.index("Found 0 internal id migration files."),
            )

    def test_deleted_devices_preview_log_keeps_check_before_result(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            server.write_state(
                {
                    "last_save_preview": "stale save preview",
                    "last_save_diff": "stale save diff",
                    "last_save_diff_generated_at": "old",
                    "last_diff": "stale apply diff",
                    "last_diff_generated_at": "old",
                }
            )

            self.assertTrue(server.run_deleted_devices_preview_job())
            state = server.read_state()
            page = server.render_page()

            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_save_diff"], "")
            self.assertIsNone(state["last_save_diff_generated_at"])
            self.assertEqual(state["last_diff"], "")
            self.assertIsNone(state["last_diff_generated_at"])
            self.assertNotIn("Save Preview", page)
            self.assertNotIn("stale save diff", page)
            self.assertEqual(state["last_message"], "Found 0 deleted devices.")
            self.assertEqual(
                state["last_details"],
                [
                    "Checking Home Assistant deleted devices and entities.",
                    "Found 0 deleted devices.",
                ],
            )
            self.assertNotIn("Checking deleted_devices.", state["last_details"])
            self.assertLess(
                page.index("Checking Home Assistant deleted devices and entities."),
                page.index("Found 0 deleted devices."),
            )

    def test_log_appends_message_after_details(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "success",
                    "last_action": "synthetic",
                    "last_message": "Finished.",
                    "last_details": ["Step 1.", "Step 2."],
                }
            )

            page = server.render_page()
            state = server.read_state()

            self.assertEqual(state["last_details"], ["Step 1.", "Step 2.", "Finished."])
            self.assertLess(page.index("Step 1."), page.index("Step 2."))
            self.assertLess(page.index("Step 2."), page.index("Finished."))

    def test_log_appends_running_message_after_details(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "running",
                    "last_action": "synthetic",
                    "last_message": "Preparing HA to Git save.",
                    "last_details": ["Using branch main."],
                }
            )

            page = server.render_page()
            state = server.read_state()

            self.assertEqual(state["last_details"], ["Using branch main.", "Preparing HA to Git save."])
            self.assertLess(page.index("Using branch main."), page.index("Preparing HA to Git save."))

    def test_add_detail_keeps_action_message_separate_from_details(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "running",
                    "last_message": "Checking HA changes after Git → HA; Git unchanged.",
                    "last_details": [],
                }
            )
            details = []

            server.context().add_detail(details, "Committed pending Internal IDs migration changes to Git: abc123.")
            state = server.read_state()

            self.assertEqual(
                state["last_message"],
                "Checking HA changes after Git → HA; Git unchanged.",
            )
            self.assertEqual(state["last_details"], ["Committed pending Internal IDs migration changes to Git: abc123."])

    def test_add_detail_does_not_restore_running_after_terminal_status(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_status": "success",
                    "last_message": "Save preview finished successfully.",
                    "last_details": ["Pushed to origin/ha-ops/base."],
                }
            )
            details = ["Pushed to origin/ha-ops/base."]

            server.context().add_detail(details, "Late detail from completed save preview.")
            state = server.read_state()

            self.assertEqual(state["last_status"], "success")
            self.assertEqual(
                state["last_details"],
                ["Pushed to origin/ha-ops/base.", "Late detail from completed save preview."],
            )

    def test_pending_internal_ids_migration_changes_are_committed_before_repo_actions(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self.seed_remote(root)
            ctx = server.app_context.AppContext(
                data_dir=root / "data",
                config_dir=root / "homeassistant",
                addon_configs_dir=root / "addon_configs",
                addon_config_path=root / "config.yaml",
            )
            ctx.work_dir.mkdir(parents=True)
            ctx.options_path.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                    }
                )
            )
            options = ctx.load_options()
            repo = ctx.ensure_repo(options)
            migrated = repo / "homeassistant" / ".ha-ops" / "areas" / "office" / "automations.yaml"
            migrated.parent.mkdir(parents=True)
            migrated.write_text("- alias: Migrated\n")
            details = []

            commit = server.app_context.job_logic.commit_pending_internal_ids_migration(ctx.job_deps(), options, details)

            self.assertIsNotNone(commit)
            self.assertEqual(self.repo_status(repo), "")
            self.assertIn("Committed pending Internal IDs migration changes to Git", details[0])
            self.assertEqual(self.remote_file(remote, "homeassistant/.ha-ops/areas/office/automations.yaml"), "- alias: Migrated\n")

    def test_pending_root_internal_ids_migration_changes_are_committed_before_repo_actions(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self.seed_remote(root)
            ctx = server.app_context.AppContext(
                data_dir=root / "data",
                config_dir=root / "homeassistant",
                addon_configs_dir=root / "addon_configs",
                addon_config_path=root / "config.yaml",
            )
            ctx.work_dir.mkdir(parents=True)
            ctx.options_path.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": ".",
                    }
                )
            )
            options = ctx.load_options()
            repo = ctx.ensure_repo(options)
            migrated = repo / ".ha-ops" / "areas" / "office" / "automations.yaml"
            migrated.parent.mkdir(parents=True)
            migrated.write_text("- alias: Migrated\n")

            commit = server.app_context.job_logic.commit_pending_internal_ids_migration(ctx.job_deps(), options, [])

            self.assertIsNotNone(commit)
            self.assertEqual(self.repo_status(repo), "")
            self.assertEqual(self.remote_file(remote, ".ha-ops/areas/office/automations.yaml"), "- alias: Migrated\n")

    def test_dirty_checkout_reports_paths_before_git_sync(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self.seed_remote(root)
            ctx = server.app_context.AppContext(
                data_dir=root / "data",
                config_dir=root / "homeassistant",
                addon_configs_dir=root / "addon_configs",
                addon_config_path=root / "config.yaml",
            )
            ctx.work_dir.mkdir(parents=True)
            ctx.options_path.write_text(
                json.dumps(
                    {
                        "repo_url": str(remote),
                        "repo_branch": "main",
                        "repo_path": "ha-config",
                        "apply_path": "homeassistant",
                    }
                )
            )
            options = ctx.load_options()
            repo = ctx.ensure_repo(options)
            changed = repo / "homeassistant" / "configuration.yaml"
            changed.write_text("dirty\n")

            with self.assertRaisesRegex(RuntimeError, "homeassistant/configuration.yaml"):
                server.app_context.job_logic.prepare_repo_checkout_for_sync(ctx.job_deps(), options, [], "Preview HA to Git")

    def test_save_preview_discards_export_leftovers_before_switching_from_ha_live_to_main(self):
        server = load_server()
        sync = server.sync_logic
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            self.git(["init", str(repo)], root)
            self.git(["checkout", "-b", "main"], repo)
            registry = repo / "homeassistant" / ".storage" / "core.entity_registry"
            registry.parent.mkdir(parents=True)
            registry.write_text('{"data": "main"}\n')
            self.git_commit_all(repo, "main")
            self.git(["checkout", "-b", "ha-ops/ha-live"], repo)
            registry.write_text('{"data": "exported"}\n')
            self.git_commit_all(repo, "live export")
            registry.write_text('{"data": "left behind by export"}\n')
            ctx = server.app_context.AppContext(data_dir=root / "data", config_dir=root / "config")

            conflicts = sync.merge_ha_live_into_git(repo, "main", ctx)

            self.assertEqual(conflicts, [])
            self.assertEqual(self.git(["branch", "--show-current"], repo).stdout.strip(), "main")
            self.assertEqual(registry.read_text(), '{"data": "exported"}\n')
            self.git(["merge", "--abort"], repo)
            self.git(["reset", "--hard", "HEAD"], repo)

    def test_internal_ids_mixed_trigger_gets_mqtt_guard_condition(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            area = config / ".ha-ops" / "areas" / "synthetic"
            area.mkdir(parents=True)
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: Mixed trigger
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  - entity_id:
    - input_boolean.synthetic
    to:
    - 'off'
    trigger: state
  conditions: []
  actions:
  - action: light.turn_off
    target:
      entity_id: light.synthetic
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            self.assertTrue(server.run_internal_ids_migrate_job(["0"]))

            migrated = automation.read_text()
            self.assertIn("topic: z2m/synthetic_remote", migrated)
            self.assertIn(
                "trigger.platform != ''mqtt'' or trigger.payload_json.action == ''1_single''",
                migrated,
            )

    def test_internal_ids_unresolved_blocker_is_not_selectable(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            area = config / ".ha-ops" / "areas" / "synthetic"
            area.mkdir(parents=True)
            (area / "automations.yaml").write_text(
                """
- id: '1'
  alias: Unsupported integration event
  triggers:
  - device_id: cccccccccccccccccccccccccccccccc
    domain: synthetic_integration
    type: synthetic_event
    trigger: device
  conditions: []
  actions: []
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_internal_ids_count"], 0)
            self.assertEqual(state["last_internal_ids_rows"][0]["changes"], 0)
            self.assertEqual(state["last_internal_ids_rows"][0]["unresolved"], 1)
            self.assertEqual(state["last_internal_ids_rows"][0]["unresolved_items"][0]["alias"], "Unsupported integration event")
            self.assertIn("device_id: cccccccccccccccccccccccccccccccc", state["last_internal_ids_rows"][0]["unresolved_items"][0]["yaml"])
            self.assertEqual(state["last_internal_ids_unresolved"][0]["alias"], "Unsupported integration event")

            page = server.render_page()
            self.assertNotIn("Unresolved device blocks", page)
            self.assertIn("unsupported device trigger", page)
            self.assertIn("<span class='no-candidates' title='No safe candidates'>None</span>", page)
            self.assertIn("device_id: cccccccccccccccccccccccccccccccc", page)
            self.assertIn("<button type='submit' disabled>Migrate and Save to Git</button>", page)
            self.assertIn("button:disabled,", page)
            self.assertIn("background: #e5e7eb", page)

    def test_internal_ids_migrate_reports_remaining_unresolved_items(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            office = config / ".ha-ops" / "areas" / "office"
            kitchen = config / ".ha-ops" / "areas" / "kitchen"
            office.mkdir(parents=True)
            kitchen.mkdir(parents=True)
            (office / "automations.yaml").write_text(
                """
- id: '1'
  alias: Migratable
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions: []
""".lstrip()
            )
            (kitchen / "automations.yaml").write_text(
                """
- id: '2'
  alias: Unsupported integration event
  triggers:
  - device_id: cccccccccccccccccccccccccccccccc
    domain: synthetic_integration
    type: synthetic_event
    trigger: device
  conditions: []
  actions: []
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            rows = server.read_state()["last_internal_ids_rows"]
            office_index = next(index for index, row in enumerate(rows) if row["path"].endswith("office/automations.yaml"))

            self.assertTrue(server.run_internal_ids_migrate_job([str(office_index)]))
            state = server.read_state()

            self.assertEqual(state["last_message"], "Migrated 1 file. 1 unresolved item remains.")
            self.assertIn("1 unresolved item remains. Review unresolved device blocks.", state["last_details"])

    def test_internal_ids_migrate_rejects_stale_preview(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            area = config / ".ha-ops" / "areas" / "synthetic"
            area.mkdir(parents=True)
            automation = area / "automations.yaml"
            automation.write_text(
                """
- id: '1'
  alias: Stale preview
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions: []
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            automation.write_text(automation.read_text() + "\n")

            self.assertFalse(server.run_internal_ids_migrate_job(["0"]))
            self.assertIn("changed since preview", server.read_state()["last_message"])

    def test_internal_ids_split_mode_applies_only_selected_file(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            kitchen = config / ".ha-ops" / "areas" / "kitchen"
            office = config / ".ha-ops" / "areas" / "office"
            kitchen.mkdir(parents=True)
            office.mkdir(parents=True)
            for path, alias in [
                (kitchen / "automations.yaml", "Kitchen synthetic"),
                (office / "automations.yaml", "Office synthetic"),
            ]:
                path.write_text(
                    f"""
- id: '{alias}'
  alias: {alias}
  triggers:
  - domain: mqtt
    device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    type: action
    subtype: 1_single
    trigger: device
  conditions: []
  actions: []
""".lstrip()
                )

            self.assertTrue(server.run_internal_ids_preview_job())
            rows = server.read_state()["last_internal_ids_rows"]
            self.assertEqual(len([row for row in rows if row["changes"]]), 2)
            office_index = next(index for index, row in enumerate(rows) if row["path"].endswith("office/automations.yaml"))

            page = server.render_page()
            self.assertIn(".ha-ops/areas/kitchen/automations.yaml after internal id migration", page)
            self.assertIn(".ha-ops/areas/office/automations.yaml after internal id migration", page)

            self.assertTrue(server.run_internal_ids_migrate_job([str(office_index)]))
            self.assertIn("topic: z2m/synthetic_remote", (office / "automations.yaml").read_text())
            self.assertIn("device_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", (kitchen / "automations.yaml").read_text())

    def test_internal_ids_no_changes_disables_migration(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            config = self.seed_internal_ids_repo(server, root)
            area = config / ".ha-ops" / "areas" / "synthetic"
            area.mkdir(parents=True)
            (area / "automations.yaml").write_text(
                """
- id: '1'
  alias: Already migrated
  triggers:
  - topic: z2m/synthetic_remote
    trigger: mqtt
  conditions:
  - condition: template
    value_template: '{{ trigger.payload_json.action == ''1_single'' }}'
  actions: []
""".lstrip()
            )

            self.assertTrue(server.run_internal_ids_preview_job())
            self.assertEqual(server.read_state()["last_internal_ids_count"], 0)

            page = server.render_page()
            self.assertIn("No internal id migration candidates found.", page)
            self.assertIn("<button type='submit' disabled>Migrate and Save to Git</button>", page)

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

    def test_deleted_entities_only_preview_and_delete(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            entity_path = storage / "core.entity_registry"
            entity_path.write_text(
                json.dumps(
                    {
                        "data": {
                            "entities": [],
                            "deleted_entities": [{"id": "entity-1", "entity_id": "switch.living_room_xmas_tree", "original_name": "Christmas tree"}],
                        }
                    }
                )
            )
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            preview = server.read_state()
            self.assertEqual(preview["last_deleted_devices_count"], 1)
            self.assertIn("Deleted entities", preview["last_deleted_devices_preview"])
            self.assertIn("deleted entities", preview["last_message"])
            preview_page = server.render_page()
            self.assertIn("switch.living_room_xmas_tree", preview_page)
            self.assertNotIn("deleted_devices", preview_page)

            self.assertTrue(server.run_deleted_devices_delete_job())
            self.assertEqual(json.loads(entity_path.read_text())["data"]["deleted_entities"], [])
            delete_state = server.read_state()
            self.assertIn("deleted entities", delete_state["last_message"])
            self.assertEqual(delete_state["deleted_devices_pending_device_count"], 0)
            self.assertEqual(delete_state["deleted_devices_pending_entity_count"], 1)
            self.assertNotIn("deleted_devices", delete_state["last_message"])
            self.assertNotIn("deleted_devices", server.render_page())
            pending_diff = server._CTX.deleted_devices_pending_diff(delete_state["deleted_devices_rollback_path"])
            self.assertIn("deleted entities before cleanup", pending_diff)
            self.assertIn("deleted entities now", pending_diff)
            self.assertNotIn("deleted_entities before cleanup", pending_diff)

            reloaded_server = load_server()
            reloaded_server.DATA_DIR = root / "data"
            reloaded_server.WORK_DIR = reloaded_server.DATA_DIR / "work"
            reloaded_server.STATE_PATH = reloaded_server.DATA_DIR / "state.json"
            reloaded_server.OPTIONS_PATH = reloaded_server.DATA_DIR / "options.json"
            reloaded_server.RELEASES_DIR = reloaded_server.DATA_DIR / "releases"
            reloaded_server.CONFIG_DIR = root / "homeassistant"
            reloaded_server.ADDON_CONFIGS_DIR = root / "addon_configs"
            reloaded_server.log = lambda message: None
            reloaded_state = reloaded_server.read_state()
            self.assertEqual(reloaded_state["deleted_devices_pending_entity_count"], 1)
            self.assertIn("Pending deleted entities Diff", reloaded_server.render_page())

            self.assertTrue(server.run_deleted_devices_confirm_job())
            confirmed = server.read_state()
            self.assertEqual(confirmed["last_message"], "Confirmed deleted entities cleanup.")
            self.assertTrue(
                any(
                    "Important: run HA to Git Preview and Save now to commit this registry cleanup" in detail
                    for detail in confirmed["last_details"]
                )
            )
            self.assertIn("run HA to Git Preview and Save now", server.render_page())
            self.assertNotIn("deleted_devices", server.render_page())
            page = server.render_page()
            section = page[page.index("<h2>Deleted devices and entities</h2>") : page.index("<h2>Retained Devices</h2>")]
            self.assertIn("deleted-devices-save-hint", section)
            self.assertIn("run HA to Git Preview and Save now", section)

    def test_deleted_devices_preview_includes_mixed_deleted_registry_entries(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(
                json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "device-1", "name": "Old Button"}]}})
            )
            (storage / "core.entity_registry").write_text(
                json.dumps({"data": {"entities": [], "deleted_entities": [{"id": "entity-1", "entity_id": "sensor.philips_1_lqi"}]}})
            )

            self.assertTrue(server.run_deleted_devices_preview_job())
            state = server.read_state()
            self.assertEqual(state["last_deleted_devices_count"], 2)
            self.assertIn("Old Button", state["last_deleted_devices_preview"])
            self.assertIn("sensor.philips_1_lqi", state["last_deleted_devices_preview"])

    def test_deleted_entities_fingerprint_blocks_stale_delete_before_core_stop(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            entity_path = storage / "core.entity_registry"
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [{"id": "entity-1", "entity_id": "sensor.old"}]}}))
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")

            self.assertTrue(server.run_deleted_devices_preview_job())
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [{"id": "entity-2", "entity_id": "sensor.new"}]}}))

            self.assertFalse(server.run_deleted_devices_delete_job())
            self.assertEqual(events, [])
            self.assertIn("changed since preview", server.read_state()["last_message"])

    def test_confirm_deleted_entities_keeps_pending_when_removed_entity_returns(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            entity_path = storage / "core.entity_registry"
            entity = {"id": "entity-1", "entity_id": "sensor.philips_1_lqi"}
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [entity]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [entity]}}))

            self.assertFalse(server.run_deleted_devices_confirm_job())
            self.assertTrue(server.read_state()["deleted_devices_pending_confirmation"])
            self.assertIn("returned", server.read_state()["last_message"])

    def test_revert_deleted_entities_preserves_new_entity_tombstones(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            entity_path = storage / "core.entity_registry"
            old = {"id": "entity-1", "entity_id": "sensor.philips_1_lqi"}
            new = {"id": "entity-2", "entity_id": "sensor.new_lqi"}
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [old]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [new]}}))
            self.assertTrue(server.run_deleted_devices_revert_job())
            self.assertEqual(json.loads(entity_path.read_text())["data"]["deleted_entities"], [new, old])

    def test_deleted_entities_write_failure_restores_devices_and_keeps_no_pending_cleanup(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            device_path = storage / "core.device_registry"
            original_devices = {"data": {"devices": [], "deleted_devices": [{"id": "device-1"}]}}
            original_entities = {"data": {"entities": [], "deleted_entities": [{"id": "entity-1", "entity_id": "sensor.old"}]}}
            device_path.write_text(json.dumps(original_devices))
            entity_path = storage / "core.entity_registry"
            entity_path.write_text(json.dumps(original_entities))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())

            registry_cleanup = server.app_context.registry_cleanup
            original_replace = registry_cleanup.os.replace

            def fail_entity_replace(source, destination):
                if Path(destination) == entity_path:
                    raise OSError("entity replace failed")
                return original_replace(source, destination)

            registry_cleanup.os.replace = fail_entity_replace
            try:
                self.assertFalse(server.run_deleted_devices_delete_job())
            finally:
                registry_cleanup.os.replace = original_replace

            self.assertEqual(json.loads(device_path.read_text()), original_devices)
            self.assertEqual(json.loads(entity_path.read_text()), original_entities)
            state = server.read_state()
            self.assertFalse(state["deleted_devices_pending_confirmation"])
            self.assertIsNone(state["deleted_devices_rollback_path"])
            self.assertEqual(list(storage.glob(".*.deleted-entries.*")), [])

    def test_deleted_entities_write_and_device_compensation_failure_keeps_manual_recovery(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            device_path = storage / "core.device_registry"
            device_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "device-1"}]}}))
            entity_path = storage / "core.entity_registry"
            entity_path.write_text(json.dumps({"data": {"entities": [], "deleted_entities": [{"id": "entity-1", "entity_id": "sensor.old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())

            registry_cleanup = server.app_context.registry_cleanup
            original_replace = registry_cleanup.os.replace

            def fail_entity_and_device_compensation(source, destination):
                source = Path(source)
                destination = Path(destination)
                if destination == entity_path:
                    raise OSError("entity replace failed")
                if destination == device_path and source.name.endswith(".deleted-entries.restore.tmp"):
                    raise OSError("device compensation failed")
                return original_replace(source, destination)

            registry_cleanup.os.replace = fail_entity_and_device_compensation
            try:
                self.assertFalse(server.run_deleted_devices_delete_job())
            finally:
                registry_cleanup.os.replace = original_replace

            state = server.read_state()
            rollback_path = Path(state["deleted_devices_rollback_path"])
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertTrue(rollback_path.exists())
            self.assertIn("Manual recovery is required", state["last_message"])
            self.assertTrue(any("device compensation failed" in detail for detail in state["last_details"]))

    def test_deleted_entities_absent_and_legacy_rollback_leave_entity_registry_untouched(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            device_path = storage / "core.device_registry"
            device_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "device-1"}]}}))
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertEqual(server.read_state()["last_deleted_devices_count"], 1)
            self.assertFalse((storage / "core.entity_registry").exists())
            self.assertTrue(server.run_deleted_devices_delete_job())
            self.assertFalse((storage / "core.entity_registry").exists())

            rollback = root / "work" / "deleted-devices-rollback" / "core.device_registry"
            rollback.parent.mkdir(parents=True)
            rollback.write_text(json.dumps({"data": {"deleted_devices": [{"id": "device-1"}]}}))
            entity_path = storage / "core.entity_registry"
            entity_path.write_text(json.dumps({"data": {"deleted_entities": [{"id": "entity-current"}]}}))
            status = server._CTX.deleted_devices_cleanup_status(str(rollback))
            self.assertEqual(status["removed"], 1)
            self.assertNotIn("deleted_entities before cleanup", server._CTX.deleted_devices_pending_diff(str(rollback)))
            server.write_state(
                {
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback),
                    "deleted_devices_applied_fingerprint": None,
                }
            )
            self.assertTrue(server.run_deleted_devices_confirm_job())
            self.assertEqual(json.loads(entity_path.read_text())["data"]["deleted_entities"], [{"id": "entity-current"}])

    def test_deleted_devices_preview_enriches_rows_from_homeassistant_target_git_history(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            storage = server.CONFIG_DIR / ".storage"
            self.write_device_registry_file(
                storage / "core.device_registry",
                deleted_devices=[
                    {
                        "id": "deleted-1",
                        "name": "Live tombstone",
                        "identifiers": [["mqtt", "zigbee2mqtt_0x00124b0024abcdef"]],
                    }
                ],
            )
            (storage / "core.entity_registry").write_text(
                json.dumps(
                    {
                        "data": {
                            "deleted_entities": [
                                {
                                    "device_id": "deleted-1",
                                    "area_id": "living",
                                    "entity_id": "switch.live_tombstone",
                                    "original_name": "Live entity name",
                                    "original_device_class": "outlet",
                                }
                            ]
                        }
                    }
                )
            )
            (storage / "core.area_registry").write_text(json.dumps({"data": {"areas": [{"id": "living", "name": "Living Room"}]}}))
            repo = self.seed_deleted_devices_history_repo(server, root)

            self.assertTrue(server.run_deleted_devices_preview_job())
            state = server.read_state()
            row = state["last_deleted_devices_rows"][0]
            page = server.render_page()

            self.assertEqual(row["area"], "Living Room")
            self.assertEqual(row["entity_id"], "switch.live_tombstone")
            self.assertEqual(row["original_name"], "Live entity name")
            self.assertEqual(row["original_device_class"], "outlet")
            self.assertEqual(row["recovered_name"], "living_room_xmas_train")
            self.assertEqual(row["recovered_manufacturer"], "Tuya")
            self.assertEqual(row["recovered_model"], "TS011F_plug")
            self.assertEqual(row["recovered_model_id"], "TS011F_plug_3")
            self.assertEqual(row["recovered_identifiers"], [["mqtt", "zigbee2mqtt_0x00124b0024abcdef"]])
            self.assertEqual(row["source_path"], "homeassistant/.storage/core.device_registry")
            self.assertRegex(row["source_commit"], r"^[0-9a-f]{40}$")
            self.assertIn("living_room_xmas_train", page)
            self.assertIn("Tuya", page)
            self.assertIn("TS011F_plug_3", page)
            self.assertIn("zigbee2mqtt_0x00124b0024abcdef", page)
            self.assertIn(row["source_commit"][:12], page)
            self.assertEqual(server.device_registry_fingerprint(), state["last_deleted_devices_fingerprint"])
            self.assertEqual(self.repo_status(repo), "")

    def test_deleted_devices_preview_enriches_device_without_entity_history_by_identifier(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            self.write_device_registry_file(
                server.CONFIG_DIR / ".storage" / "core.device_registry",
                deleted_devices=[
                    {
                        "id": "deleted-2",
                        "identifiers": [["mqtt", "zigbee2mqtt_0x00124b0024abcdee"]],
                    }
                ],
            )
            self.seed_deleted_devices_history_repo(server, root)

            self.assertTrue(server.run_deleted_devices_preview_job())
            row = server.read_state()["last_deleted_devices_rows"][0]

            self.assertEqual(row["id"], "deleted-2")
            self.assertEqual(row["entity_id"], "")
            self.assertEqual(row["recovered_name"], "identifier fallback plug")
            self.assertEqual(row["recovered_manufacturer"], "Tuya")
            self.assertEqual(row["recovered_model_id"], "TS011F_plug_3")

    def test_deleted_devices_history_uses_custom_manifest_homeassistant_source(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            self.write_device_registry_file(
                server.CONFIG_DIR / ".storage" / "core.device_registry",
                deleted_devices=[{"id": "deleted-1"}],
            )
            self.seed_deleted_devices_history_repo(
                server,
                root,
                source="targets/ha-prod",
                manifest={
                    "version": 1,
                    "targets": [
                        {
                            "id": "homeassistant-prod",
                            "type": "homeassistant",
                            "source": "targets/ha-prod",
                        }
                    ],
                },
            )

            self.assertTrue(server.run_deleted_devices_preview_job())
            row = server.read_state()["last_deleted_devices_rows"][0]

            self.assertEqual(row["recovered_name"], "living_room_xmas_train")
            self.assertEqual(row["source_path"], "targets/ha-prod/.storage/core.device_registry")

    def test_deleted_devices_history_uses_only_allowed_local_git_commands(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            self.write_device_registry_file(
                server.CONFIG_DIR / ".storage" / "core.device_registry",
                deleted_devices=[{"id": "deleted-1"}],
            )
            repo = self.seed_deleted_devices_history_repo(server, root)
            calls = []

            def capture_run_command(command, env=None, cwd=None, timeout=None):
                calls.append((command, Path(cwd) if cwd else None))
                return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)

            server.run_command = capture_run_command

            self.assertTrue(server.run_deleted_devices_preview_job())

            self.assertTrue(calls)
            for command, cwd in calls:
                self.assertEqual(cwd, repo.resolve())
                self.assertEqual(command[0], "git")
                self.assertIn(command[1], {"log", "show"})
                self.assertNotIn("fetch", command)
                self.assertNotIn("pull", command)
                self.assertNotIn("checkout", command)
                self.assertNotIn("-p", command)
                if command[1] == "log":
                    self.assertEqual(command, ["git", "log", "--format=%H", "--max-count=50", "--", "homeassistant/.storage/core.device_registry"])
                if command[1] == "show":
                    self.assertEqual(len(command), 3)
                    self.assertRegex(command[2], r"^[0-9a-f]{40}:homeassistant/\.storage/core\.device_registry$")

    def test_deleted_devices_fingerprint_stays_live_only_when_history_changes_or_disappears(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.get_installed_addons = lambda: []
            self.write_device_registry_file(
                server.CONFIG_DIR / ".storage" / "core.device_registry",
                deleted_devices=[{"id": "deleted-1"}],
            )
            repo = self.seed_deleted_devices_history_repo(server, root)

            self.assertTrue(server.run_deleted_devices_preview_job())
            first_fingerprint = server.read_state()["last_deleted_devices_fingerprint"]
            self.write_device_registry_file(
                repo / "homeassistant" / ".storage" / "core.device_registry",
                devices=[
                    {
                        "id": "deleted-1",
                        "name": "changed only in git",
                    }
                ],
            )
            self.git_commit_all(repo, "change history only")

            self.assertTrue(server.run_deleted_devices_preview_job())
            second_fingerprint = server.read_state()["last_deleted_devices_fingerprint"]
            self.assertEqual(second_fingerprint, first_fingerprint)

            (repo / ".git").rename(repo / ".git-disabled")
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertEqual(server.read_state()["last_deleted_devices_fingerprint"], first_fingerprint)

    def test_deleted_devices_history_failures_degrade_to_live_only_preview(self):
        server = load_server()
        failure_outputs = [
            (["git", "log", "--format=%H", "--max-count=50", "--", "homeassistant/.storage/core.device_registry"], 1, "", "log failed"),
            (["git", "log", "--format=%H", "--max-count=50", "--", "homeassistant/.storage/core.device_registry"], 0, "abc\n", ""),
            (["git", "show", "abc:homeassistant/.storage/core.device_registry"], 0, "{", ""),
            (
                ["git", "show", "abc:homeassistant/.storage/core.device_registry"],
                0,
                json.dumps({"data": {"devices": {}}}),
                "",
            ),
        ]
        for index, _case in enumerate(range(4)):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.configure_paths(server, root)
                server.get_installed_addons = lambda: []
                self.write_device_registry_file(
                    server.CONFIG_DIR / ".storage" / "core.device_registry",
                    deleted_devices=[{"id": "deleted-1", "name": "Live only"}],
                )
                repo = root / "data" / "ha-config"
                self.git(["init", str(repo)], root)
                server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant"}))

                def failing_run_command(command, env=None, cwd=None, timeout=None):
                    expected, returncode, stdout, stderr = failure_outputs[min(index, len(failure_outputs) - 1)]
                    if command == expected:
                        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
                    return subprocess.CompletedProcess(command, 1, "", "unexpected")

                if index >= 2:
                    def two_step_run_command(command, env=None, cwd=None, timeout=None):
                        if command[1] == "log":
                            return subprocess.CompletedProcess(command, 0, "abc\n", "")
                        expected, returncode, stdout, stderr = failure_outputs[index]
                        if command == expected:
                            return subprocess.CompletedProcess(command, returncode, stdout, stderr)
                        return subprocess.CompletedProcess(command, 1, "", "unexpected")

                    server.run_command = two_step_run_command
                else:
                    server.run_command = failing_run_command

                self.assertTrue(server.run_deleted_devices_preview_job())
                row = server.read_state()["last_deleted_devices_rows"][0]

                self.assertNotIn("recovered_name", row)
                self.assertEqual(row["original_name"], "Live only")

    def test_deleted_devices_delete_preflight_does_not_scan_git_history(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            server.get_installed_addons = lambda: []
            storage = server.CONFIG_DIR / ".storage"
            self.write_device_registry_file(storage / "core.device_registry", deleted_devices=[{"id": "deleted-1"}])
            self.seed_deleted_devices_history_repo(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"repo_path": "ha-config", "apply_path": "homeassistant", "require_fresh_backup": False}))
            server.core_stop = lambda: None
            server.core_start = lambda: None

            self.assertTrue(server.run_deleted_devices_preview_job())
            calls = []

            def fail_on_history(command, env=None, cwd=None, timeout=None):
                calls.append(command)
                if command[:2] == ["git", "log"] or command[:2] == ["git", "show"]:
                    raise AssertionError("delete preflight scanned git history")
                return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)

            server.run_command = fail_on_history

            self.assertTrue(server.run_deleted_devices_delete_job())
            self.assertFalse([command for command in calls if command[:2] in (["git", "log"], ["git", "show"])])

    def test_recovered_deleted_device_rows_clear_with_display_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "last_deleted_devices_preview": "old",
                    "last_deleted_devices_rows": [{"id": "deleted-1", "recovered_name": "living_room_xmas_train"}],
                    "last_deleted_devices_count": 1,
                    "last_deleted_devices_fingerprint": "fingerprint",
                    "last_deleted_devices_generated_at": "2026-05-16T12:00:00+00:00",
                }
            )

            server.clear_display_state()
            state = server.read_state()

            self.assertEqual(state["last_deleted_devices_rows"], [])
            self.assertEqual(state["last_deleted_devices_preview"], "")

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
            body_markup = page.split("<script>", 1)[0]
            self.assertNotIn("Deletion of deleted_devices Preview", body_markup)
            self.assertNotIn("Approve Deletion", body_markup)

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
            self.assertIn("Pending deleted devices Diff", page)
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
                    "last_message": "Registry entries changed after deletion. Review manually before reverting.",
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

            self.assertIn('<div class="badge pending" data-status-code="pending decision" data-testid="status-badge">pending decision</div>', page)
            self.assertNotIn('<div class="badge error">error</div>', page)
            self.assertIn("<h2>Log</h2>", page)
            self.assertNotIn("<h2>Last Run Details</h2>", page)
            self.assertNotIn("Preview deletions", page)
            self.assertIn("deleted devices cleanup is waiting for your decision.", page)
            self.assertIn("Previous action: Revert Changes", page)
            self.assertIn("Last result: Registry entries changed after deletion. Review manually before reverting.", page)
            self.assertIn("- deleted devices removed by this cleanup: 1", page)
            self.assertIn("- currently present deleted devices: 1", page)
            self.assertIn("- new deleted devices after restart: 1", page)
            self.assertIn("- removed entries returned: 0", page)
            self.assertIn("Confirm Changes: keep this cleanup. Removed deleted devices stay removed.", page)
            self.assertIn("Revert Changes: restore only deleted devices removed by this cleanup.", page)
            self.assertIn("<h2>Pending deleted devices Diff</h2>", page)
            self.assertNotIn("<h2>Deletion of deleted_devices Preview</h2>", page)
            self.assertIn("Confirm Changes accepts this diff.", page)
            self.assertIn("deleted devices before cleanup", page)
            self.assertIn("deleted devices now", page)
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

            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Check deleted devices and entities</button>", page)
            self.assertNotIn("action='deleted-devices-delete'", page)
            self.assertIn("Confirm Changes", page)
            self.assertIn("Revert Changes", page)
            self.assertFalse(server.run_deleted_devices_preview_job())
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])
            self.assertFalse(server.run_deleted_devices_delete_job())
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])

    def test_pending_deleted_devices_cleanup_blocks_save_apply_and_previews(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state(
                {
                    "deleted_devices_pending_confirmation": True,
                    "last_diff": "stale apply diff",
                    "last_diff_generated_at": "2026-06-13T12:00:00+00:00",
                    "last_preview_commit": "stale-apply-commit",
                    "last_preview_fingerprint": "stale-apply-fingerprint",
                    "last_preview_live_fingerprints": {"homeassistant/configuration.yaml": "live"},
                    "last_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_preview_conflicts": True,
                    "apply_preview_resolutions": {"homeassistant/configuration.yaml": "git"},
                    "last_save_preview": "stale save preview",
                    "last_save_diff": "stale save diff",
                    "last_save_diff_generated_at": "2026-06-13T12:00:00+00:00",
                    "last_save_preview_commit": "stale-save-commit",
                    "last_save_preview_fingerprint": "stale-save-fingerprint",
                    "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                    "last_save_preview_conflicts": True,
                    "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                }
            )
            state = server.read_state()
            self.assertEqual(state["last_diff"], "")
            self.assertIsNone(state["last_diff_generated_at"])
            self.assertIsNone(state["last_preview_commit"])
            self.assertIsNone(state["last_preview_fingerprint"])
            self.assertEqual(state["last_preview_live_fingerprints"], {})
            self.assertEqual(state["last_preview_paths"], [])
            self.assertFalse(state["last_preview_conflicts"])
            self.assertEqual(state["apply_preview_resolutions"], {})
            self.assertEqual(state["last_save_preview"], "")
            self.assertEqual(state["last_save_diff"], "")
            self.assertIsNone(state["last_save_diff_generated_at"])
            self.assertIsNone(state["last_save_preview_commit"])
            self.assertIsNone(state["last_save_preview_fingerprint"])
            self.assertEqual(state["last_save_preview_paths"], [])
            self.assertFalse(state["last_save_preview_conflicts"])
            self.assertEqual(state["save_preview_resolutions"], {})

            self.assertFalse(server.run_save_preview_job())
            self.assertEqual(server.read_state()["last_action"], "save_preview")
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_save_job())
            self.assertEqual(server.read_state()["last_action"], "save")
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_preview_job())
            self.assertEqual(server.read_state()["last_action"], "preview")
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])

            self.assertFalse(server.run_apply_job())
            self.assertEqual(server.read_state()["last_action"], "apply")
            self.assertIn("pending deleted devices cleanup", server.read_state()["last_message"])

            page = server.render_page()
            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Preview HA to Git</button>", page)
            self.assertIn("<button type=\"submit\" class=\"secondary\" disabled>Preview Git to HA</button>", page)
            self.assertNotIn("Save HA to Git</button>", page)
            self.assertNotIn("Apply Git to HA</button>", page)
            self.assertNotIn("<h2>Save Preview</h2>", page)
            self.assertNotIn("<h2>Apply Preview</h2>", page)
            self.assertNotIn("Confirm Save to Git", page)
            self.assertNotIn("Confirm Apply to HA", page)
            self.assertIn("Confirm Changes", page)
            self.assertIn("Revert Changes", page)

    def test_deleted_devices_cleanup_clears_stale_save_apply_previews_through_decision(self):
        server = load_server()
        for decision in ("confirm", "revert"):
            with self.subTest(decision=decision):
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
                    server.write_state(
                        {
                            "last_diff": "stale apply diff",
                            "last_diff_generated_at": "2026-06-13T12:00:00+00:00",
                            "last_preview_commit": "stale-apply-commit",
                            "last_preview_fingerprint": "stale-apply-fingerprint",
                            "last_preview_live_fingerprints": {"homeassistant/configuration.yaml": "live"},
                            "last_preview_paths": ["homeassistant/configuration.yaml"],
                            "last_preview_conflicts": True,
                            "apply_preview_resolutions": {"homeassistant/configuration.yaml": "git"},
                            "last_save_preview": "stale save preview",
                            "last_save_diff": "stale save diff",
                            "last_save_diff_generated_at": "2026-06-13T12:00:00+00:00",
                            "last_save_preview_commit": "stale-save-commit",
                            "last_save_preview_fingerprint": "stale-save-fingerprint",
                            "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                            "last_save_preview_conflicts": True,
                            "save_preview_resolutions": {"homeassistant/configuration.yaml": "ha"},
                        }
                    )

                    self.assertTrue(server.run_deleted_devices_preview_job())
                    self.assertTrue(server.run_deleted_devices_delete_job())
                    state = server.read_state()
                    page = server.render_page()

                    self.assertTrue(state["deleted_devices_pending_confirmation"])
                    self.assertEqual(state["last_diff"], "")
                    self.assertEqual(state["last_preview_paths"], [])
                    self.assertEqual(state["last_save_preview"], "")
                    self.assertEqual(state["last_save_preview_paths"], [])
                    self.assertNotIn("<h2>Save Preview</h2>", page)
                    self.assertNotIn("<h2>Apply Preview</h2>", page)
                    self.assertNotIn("Confirm Save to Git", page)
                    self.assertNotIn("Confirm Apply to HA", page)

                    if decision == "confirm":
                        self.assertTrue(server.run_deleted_devices_confirm_job())
                    else:
                        self.assertTrue(server.run_deleted_devices_revert_job())
                    state = server.read_state()
                    page = server.render_page()

                    self.assertFalse(state["deleted_devices_pending_confirmation"])
                    self.assertEqual(state["last_diff"], "")
                    self.assertEqual(state["last_preview_paths"], [])
                    self.assertEqual(state["last_save_preview"], "")
                    self.assertEqual(state["last_save_preview_paths"], [])
                    self.assertNotIn("<h2>Save Preview</h2>", page)
                    self.assertNotIn("<h2>Apply Preview</h2>", page)
                    self.assertNotIn("Confirm Save to Git", page)
                    self.assertNotIn("Confirm Apply to HA", page)

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

    def test_deleted_devices_partial_success_retains_manual_recovery_when_core_start_fails(self):
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
            self.assertTrue(state.get("deleted_devices_pending_confirmation", False))
            self.assertEqual(state["last_deleted_devices_count"], 1)
            self.assertIn("Old Button", state["last_deleted_devices_preview"])
            self.assertEqual(state["deleted_devices_recovery_phase"], "manual_recovery")
            self.assertIn("start failed", "\n".join(state["last_details"]))

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

    def test_confirm_keeps_v1_manifest_when_terminal_publication_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback = Path(server.read_state()["deleted_devices_rollback_path"])
            self.assertEqual(rollback.name, "rollback-manifest-v1.json")

            def fail_directory_fsync(_directory):
                raise OSError("directory fsync failed")

            registry_cleanup = sys.modules["registry_cleanup"]
            original_fsync = registry_cleanup._fsync_directory
            registry_cleanup._fsync_directory = fail_directory_fsync
            try:
                self.assertFalse(server.run_deleted_devices_confirm_job())
            finally:
                registry_cleanup._fsync_directory = original_fsync
            state = server.read_state()
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertTrue(rollback.exists())
            self.assertTrue((rollback.parent / "core.device_registry.snapshot").exists())

    def test_confirm_keeps_terminal_v1_manifest_when_sidecar_cleanup_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback = Path(server.read_state()["deleted_devices_rollback_path"])

            registry_cleanup = sys.modules["registry_cleanup"]
            original_unlink = registry_cleanup._durable_unlink

            def fail_device_sidecar_unlink(path):
                if Path(path).name == "core.device_registry.snapshot":
                    raise OSError("sidecar fsync failed")
                return original_unlink(path)

            registry_cleanup._durable_unlink = fail_device_sidecar_unlink
            try:
                self.assertFalse(server.run_deleted_devices_confirm_job())
            finally:
                registry_cleanup._durable_unlink = original_unlink

            self.assertTrue(rollback.exists())
            self.assertTrue((rollback.parent / "core.device_registry.snapshot").exists())
            self.assertEqual(json.loads(rollback.read_text())["phase"], "confirmed")
            self.assertTrue(server.read_state()["deleted_devices_pending_confirmation"])
            self.assertTrue(server.run_deleted_devices_confirm_job())
            self.assertFalse(rollback.exists())

    def test_revert_keeps_v1_manifest_and_manual_fence_when_terminal_publication_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry = storage / "core.device_registry"
            registry.write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback = Path(server.read_state()["deleted_devices_rollback_path"])
            real_phase = server._CTX.set_deleted_devices_rollback_phase

            def fail_reverted(path, phase):
                if phase != "reverted":
                    return real_phase(path, phase)
                registry_cleanup = sys.modules["registry_cleanup"]
                original_fsync = registry_cleanup._fsync_directory
                registry_cleanup._fsync_directory = lambda _directory: (_ for _ in ()).throw(OSError("directory fsync failed"))
                try:
                    return real_phase(path, phase)
                finally:
                    registry_cleanup._fsync_directory = original_fsync

            server._CTX.set_deleted_devices_rollback_phase = fail_reverted
            self.assertFalse(server.run_deleted_devices_revert_job())
            state = server.read_state()
            self.assertEqual(state["deleted_devices_recovery_phase"], "manual_recovery")
            self.assertTrue(state["deleted_devices_pending_confirmation"])
            self.assertTrue(rollback.exists())
            self.assertTrue((rollback.parent / "core.device_registry.snapshot").exists())

    def test_revert_keeps_terminal_v1_manifest_when_sidecar_cleanup_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry = storage / "core.device_registry"
            registry.write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback = Path(server.read_state()["deleted_devices_rollback_path"])

            registry_cleanup = sys.modules["registry_cleanup"]
            original_unlink = registry_cleanup._durable_unlink

            def fail_device_sidecar_unlink(path):
                if Path(path).name == "core.device_registry.snapshot":
                    raise OSError("sidecar fsync failed")
                return original_unlink(path)

            registry_cleanup._durable_unlink = fail_device_sidecar_unlink
            try:
                self.assertFalse(server.run_deleted_devices_revert_job())
            finally:
                registry_cleanup._durable_unlink = original_unlink

            self.assertTrue(rollback.exists())
            self.assertTrue((rollback.parent / "core.device_registry.snapshot").exists())
            self.assertEqual(json.loads(rollback.read_text())["phase"], "reverted")
            self.assertEqual(server.read_state()["deleted_devices_recovery_phase"], "manual_recovery")
            self.assertTrue(server.run_deleted_devices_revert_job())
            self.assertFalse(rollback.exists())

    def test_v1_revert_preserves_entity_registry_created_after_snapshot(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            entity_path = storage / "core.entity_registry"
            current_entities = {"data": {"entities": [{"entity_id": "sensor.new"}], "deleted_entities": [{"id": "new"}]}}
            entity_path.write_text(json.dumps(current_entities))

            self.assertTrue(server.run_deleted_devices_revert_job())

            self.assertEqual(json.loads(entity_path.read_text()), current_entities)

    def test_v1_startup_recovery_preserves_entity_registry_created_after_snapshot(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            self.assertTrue(server.run_deleted_devices_preview_job())
            self.assertTrue(server.run_deleted_devices_delete_job())
            rollback = server.read_state()["deleted_devices_rollback_path"]
            entity_path = storage / "core.entity_registry"
            current_entities = {"data": {"entities": [{"entity_id": "sensor.new"}], "deleted_entities": [{"id": "new"}]}}
            entity_path.write_text(json.dumps(current_entities))
            server._CTX.set_deleted_devices_rollback_phase(rollback, "restore_required")
            server.write_state({"deleted_devices_recovery_phase": "restore_required"})

            server._CTX.repair_startup_state()

            self.assertEqual(json.loads(entity_path.read_text()), current_entities)

    def test_stale_manifest_format_does_not_break_legacy_revert(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.OPTIONS_PATH.write_text(json.dumps({"require_fresh_backup": False}))
            storage = server.CONFIG_DIR / ".storage"
            storage.mkdir()
            registry_path = storage / "core.device_registry"
            registry_path.write_text(json.dumps({"data": {"devices": [], "deleted_devices": []}}))
            rollback = root / "work" / "deleted-devices-rollback" / "core.device_registry"
            rollback.parent.mkdir(parents=True)
            rollback.write_text(json.dumps({"data": {"devices": [], "deleted_devices": [{"id": "old"}]}}))
            server.core_stop = lambda: None
            server.core_start = lambda: None
            server.write_state(
                {
                    "deleted_devices_pending_confirmation": True,
                    "deleted_devices_rollback_path": str(rollback),
                    "deleted_devices_rollback_format": "manifest_v1",
                }
            )

            self.assertTrue(server.run_deleted_devices_revert_job())

            self.assertEqual(json.loads(registry_path.read_text())["data"]["deleted_devices"], [{"id": "old"}])
            self.assertFalse(rollback.exists())
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
            self.assertIn("Confirmed deleted devices cleanup", state["last_message"])
            self.assertIn("removed deleted devices did not return", "\n".join(state["last_details"]))

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
            self.assertIn("Confirmed deleted devices cleanup", state["last_message"])
            self.assertIn("new deleted devices", "\n".join(state["last_details"]))

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
            self.assertIn("Reverted deleted devices cleanup", state["last_message"])
            self.assertIn("Preserved 1 current deleted devices", "\n".join(state["last_details"]))
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

    def test_homeassistant_organizer_blocked_control_is_in_main_action_card(self):
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
            self.assertIn("Area split organizer paused", page)
            self.assertIn("name='homeassistant_organizer' value='1' disabled", page)
            self.assertNotIn("Split automations, scripts, and scenes by area in Git", page)

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
            state = server.read_state()
            repo = server.DATA_DIR / "ha-config"

            self.assertIn("- Modified: homeassistant/configuration.yaml", state["last_save_preview"])
            self.assertIn("- Added: homeassistant/packages/lights.yaml", state["last_save_preview"])
            self.assertIn("homeassistant/configuration.yaml", state["last_save_diff"])
            self.assertIn("homeassistant/packages/lights.yaml", state["last_save_diff"])
            self.assertNotIn("secrets.yaml", state["last_save_preview"])
            self.assertNotIn("home-assistant_v2.db", state["last_save_preview"])
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
                    [{"slug": "local_zigbee2mqtt", "name": "Plain App"}],
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
                    [{"slug": "local_zigbee2mqtt", "name": "Plain App"}],
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

    def test_lovelace_resources_release_rollback_reloads_without_stopping_core(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')

            release = server.create_release_snapshot(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(root / "repo" / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                        "stop_core_before_storage_rollback": True,
                        "start_core_after_storage_rollback": True,
                    }
                ],
                "abc123",
                None,
            )

            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            events = []
            server.core_stop = lambda: events.append("stop")
            server.core_start = lambda: events.append("start")
            server.core_reload_lovelace = lambda: events.append("lovelace")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.restore_release_snapshot(release, [])

            self.assertEqual(events, ["lovelace"])
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')

    def test_theme_release_rollback_reloads_themes_without_general_yaml_reload(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live_theme = server.CONFIG_DIR / "themes" / "custom" / "theme.yaml"
            live_theme.parent.mkdir(parents=True)
            live_theme.write_text("custom: {primary-color: '#fff'}\n")

            release = server.create_release_snapshot(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(root / "repo" / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                    }
                ],
                "abc123",
                None,
            )

            live_theme.write_text("custom: {primary-color: '#000'}\n")
            events = []
            server.core_reload_themes = lambda: events.append("themes")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.restore_release_snapshot(release, [])

            self.assertEqual(events, ["themes"])
            self.assertEqual(live_theme.read_text(), "custom: {primary-color: '#fff'}\n")

    def test_lovelace_resources_release_rollback_falls_back_to_restart_when_reload_fails(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')
            (server.CONFIG_DIR / "configuration.yaml").write_text("snapshot\n")

            release = server.create_release_snapshot(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(root / "repo" / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                        "reload_yaml_after_rollback": True,
                    }
                ],
                "abc123",
                None,
            )

            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            events = []

            def fail_lovelace_reload():
                events.append("lovelace")
                raise RuntimeError("service unavailable")

            def fail_yaml_reload():
                events.append("reload")
                raise AssertionError("YAML reload should not run after restart fallback")

            server.core_reload_lovelace = fail_lovelace_reload
            server.core_reload_yaml = fail_yaml_reload
            server.core_restart = lambda: events.append("restart")

            server.restore_release_snapshot(release, [])

            self.assertEqual(events, ["lovelace", "restart"])
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "snapshot\n")

    def test_mixed_yaml_and_lovelace_resources_release_rollback_reloads_both(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            live_storage = server.CONFIG_DIR / ".storage"
            live_storage.mkdir(parents=True)
            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')
            (server.CONFIG_DIR / "configuration.yaml").write_text("snapshot\n")

            release = server.create_release_snapshot(
                [
                    {
                        "id": "homeassistant",
                        "type": "homeassistant",
                        "source_path": str(root / "repo" / "homeassistant"),
                        "live_path": str(server.CONFIG_DIR),
                        "reload_yaml_after_rollback": True,
                    }
                ],
                "abc123",
                None,
            )

            (live_storage / "lovelace_resources").write_text('{"data":{"items":[{"url":"/local/live.js"}]}}\n')
            (server.CONFIG_DIR / "configuration.yaml").write_text("live\n")
            events = []
            server.core_reload_lovelace = lambda: events.append("lovelace")
            server.core_reload_yaml = lambda: events.append("reload")
            server.core_restart = lambda: events.append("restart")

            server.restore_release_snapshot(release, [])

            self.assertEqual(events, ["lovelace", "reload"])
            self.assertEqual((live_storage / "lovelace_resources").read_text(), '{"data":{"items":[{"url":"/local/snapshot.js"}]}}\n')
            self.assertEqual((server.CONFIG_DIR / "configuration.yaml").read_text(), "snapshot\n")

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

    def test_docker_api_negotiation_uses_numeric_versions_and_bounds(self):
        server = load_server()
        api = server.app_context.docker_api
        self.assertEqual(api.select_api_version("1.52", "1.24"), (1, 41))
        self.assertEqual(api.select_api_version("1.40", "1.39"), (1, 40))
        self.assertEqual(api.select_api_version("1.52", "1.52"), (1, 52))
        with self.assertRaises(api.DockerAPIError):
            api.select_api_version("1.38", "1.24")
        with self.assertRaises(api.DockerAPIError):
            api.select_api_version("1.52", "1.x")

    def test_docker_api_legacy_reclaimable_is_upper_bound(self):
        server = load_server()
        usage = server.app_context.docker_api.build_cache_usage(
            {
                "BuildCache": [
                    {"Size": 10, "InUse": False, "Shared": False},
                    {"Size": 20, "InUse": True, "Shared": False},
                    {"Size": 30, "InUse": True, "Shared": True},
                ]
            },
            (1, 41),
        )
        self.assertEqual(usage, {"count": 3, "size": 60, "reclaimable": 40})

    def test_docker_api_152_rejects_impossible_reclaimable_total(self):
        server = load_server()
        api = server.app_context.docker_api
        self.assertEqual(
            api.build_cache_usage(
                {
                    "BuildCacheUsage": {
                        "TotalSize": 50,
                        "Reclaimable": 20,
                        "Active": 1,
                        "TotalCount": 2,
                    }
                },
                (1, 52),
            )["reclaimable"],
            20,
        )
        with self.assertRaises(api.DockerAPIError):
            api.build_cache_usage({"BuildCacheUsage": {"TotalSize": 20, "Reclaimable": 50}}, (1, 52))

    def test_docker_prune_active_resolution_and_corrupt_ui_lifecycle(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            operation_id = "f9e35068-0f1d-4eca-83f8-93bfd878cbea"
            fence = server.app_context.state_store.new_docker_prune_fence(
                operation_id, "2026-07-12T18:00:00+00:00"
            )

            for phase in ("accepted", "dispatching"):
                with self.subTest(phase=phase):
                    server.write_state({"docker_build_cache_prune_fence": dict(fence, phase=phase)})
                    server.RUN_LOCK.acquire()
                    try:
                        page = server.render_page()
                    finally:
                        server.RUN_LOCK.release()
                    expected = (
                        "Build-cache cleanup is accepted and waiting to dispatch."
                        if phase == "accepted"
                        else "Build-cache cleanup request is in progress."
                    )
                    self.assertIn(expected, page)
                    self.assertNotIn(">Acknowledge</button>", page)
                    self.assertIn('action="docker-build-cache-prune"', page)
                    prune_form = page[page.index('action="docker-build-cache-prune"'):][:700]
                    self.assertIn('<button type="submit" class="secondary" disabled>', prune_form)

            server.write_state({"docker_build_cache_prune_fence": dict(fence, phase="resolution_required")})
            page = server.render_page()
            self.assertIn(">Acknowledge</button>", page)
            self.assertIn("name='mode' value='operation'", page)
            self.assertIn(f"name='operation_id' value='{operation_id}'", page)

            corrupt = {"schema": 99, "operation_id": operation_id}
            server.write_state({"docker_build_cache_prune_fence": corrupt})
            page = server.render_page()
            self.assertIn(">Acknowledge</button>", page)
            self.assertIn("name='mode' value='corrupt'", page)
            self.assertNotIn("name='operation_id'", page)
            self.assertEqual(server.read_state()["docker_build_cache_prune_fence"], corrupt)
            token = server.app_context.state_store.classify_docker_prune_fence(corrupt)["recovery_token"]
            response = self.post_json(
                server,
                "/docker-build-cache-prune-resolve",
                body=f"mode=corrupt&recovery_token={token}".encode(),
            )
            self.assertEqual(response.responses[-1], 200)
            self.assertIsNone(server.read_state()["docker_build_cache_prune_fence"])

    def test_docker_prune_acknowledgement_and_success_do_not_resurrect(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            operation_id = "f9e35068-0f1d-4eca-83f8-93bfd878cbea"
            fence = server.app_context.state_store.new_docker_prune_fence(
                operation_id, "2026-07-12T18:00:00+00:00"
            )
            server.write_state({"docker_build_cache_prune_fence": dict(fence, phase="resolution_required")})

            response = self.post_json(
                server,
                "/docker-build-cache-prune-resolve",
                body=f"mode=operation&operation_id={operation_id}".encode(),
            )
            self.assertEqual(response.responses[-1], 200)
            self.assertIsNone(server.read_state()["docker_build_cache_prune_fence"])
            self.assertNotIn(">Acknowledge</button>", server.render_page())

            server.clear_display_state()
            server.repair_startup_state()
            self.assertIsNone(server.read_state()["docker_build_cache_prune_fence"])

            server.write_state({"docker_build_cache_prune_fence": fence})
            server.context().docker_api.prune_build_cache = lambda: {
                "space_reclaimed": 12,
                "caches_deleted": ["cache-id"],
            }
            server.RUN_LOCK.acquire()
            self.assertTrue(server.run_docker_build_cache_prune_job(operation_id, lock_acquired=True))
            self.assertIsNone(server.read_state()["docker_build_cache_prune_fence"])
            self.assertEqual(server.read_state()["last_status"], "success")

            server.clear_display_state()
            server.repair_startup_state()
            self.assertIsNone(server.read_state()["docker_build_cache_prune_fence"])
            self.assertNotIn(">Acknowledge</button>", server.render_page())

    def test_docker_prune_fence_survives_refresh_restart_and_version_update(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            operation_id = "f9e35068-0f1d-4eca-83f8-93bfd878cbea"
            fence = server.app_context.state_store.new_docker_prune_fence(
                operation_id, "2026-07-12T18:00:00+00:00"
            )
            server.write_state({
                "last_seen_addon_version": "0.9.1",
                "docker_build_cache_prune_fence": fence,
            })

            server.clear_display_state()
            self.assertEqual(
                server.read_state()["docker_build_cache_prune_fence"]["operation_id"], operation_id
            )

            server.repair_startup_state()
            restarted = server.read_state()["docker_build_cache_prune_fence"]
            self.assertEqual(restarted["phase"], "resolution_required")
            self.assertEqual(restarted["operation_id"], operation_id)

            server.ADDON_CONFIG_PATH = Path(tmp) / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "0.9.2"\n')
            server.repair_startup_state()
            updated = server.read_state()["docker_build_cache_prune_fence"]
            self.assertEqual(updated["phase"], "resolution_required")
            self.assertEqual(updated["operation_id"], operation_id)
            self.assertIn(">Acknowledge</button>", server.render_page())

    def test_docker_prune_fence_corruption_token_is_canonical_and_durable(self):
        server = load_server()
        state_store = server.app_context.state_store
        first = state_store.classify_docker_prune_fence({"b": 2, "a": 1})
        second = state_store.classify_docker_prune_fence({"a": 1, "b": 2})
        self.assertEqual(first["kind"], "corrupt")
        self.assertEqual(first["recovery_token"], second["recovery_token"])
        self.assertTrue(first["recovery_token"].startswith("corrupt:"))

    def test_docker_prune_fence_requires_exact_acknowledgement_identity(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            operation_id = "f9e35068-0f1d-4eca-83f8-93bfd878cbea"
            server.app_context.state_store.write_state(
                path,
                {
                    "docker_build_cache_prune_fence": {
                        "schema": 1,
                        "operation_id": operation_id,
                        "phase": "resolution_required",
                        "accepted_at": "2026-07-12T18:00:00+00:00",
                    }
                },
            )
            self.assertIsNone(server.app_context.state_store.clear_docker_prune_fence(path, "operation", str(__import__("uuid").uuid4())))
            self.assertIsNotNone(server.app_context.state_store.clear_docker_prune_fence(path, "operation", operation_id))

    def test_docker_build_cache_capability_is_fail_closed_and_explains_remedy(self):
        server = load_server()
        capability = server.app_context.docker_capability
        cases = {
            "available": ({"data": {"protected": False, "docker_api": True}}, capability.AVAILABLE),
            "protected": ({"data": {"protected": True, "docker_api": True}}, capability.PROTECTION_ENABLED),
            "docker api": ({"data": {"protected": False, "docker_api": False}}, capability.DOCKER_API_UNAVAILABLE),
            "missing": ({"data": {"protected": False}}, capability.UNKNOWN),
            "non boolean": ({"data": {"protected": "false", "docker_api": True}}, capability.UNKNOWN),
            "malformed": ({"data": []}, capability.UNKNOWN),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(capability.classify_self_info(payload), expected)

        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "docker.sock"
            api = server.app_context.docker_api.DockerAPI(socket_path=socket_path)
            self.assertFalse(api.socket_is_available())
            socket_path.write_text("not a socket")
            self.assertFalse(api.socket_is_available())

        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            server.context().call_supervisor = lambda method, path, payload=None, timeout=None: {
                "data": {"protected": True, "docker_api": True}
            }
            page = server.render_page()
            section = page[page.index("<h2>Disk Usage</h2>") : page.index("<h2>Deleted devices", page.index("<h2>Disk Usage</h2>"))]
            self.assertIn('data-capability-available="false" data-action-ready="false"', section)
            self.assertIn('class="secondary" disabled', section)
            self.assertIn('class=\'action-hint docker-prune-hint\'', section)
            self.assertIn("Protection mode is enabled", section)
            self.assertIn("turn off Protection mode", section)
            self.assertIn("color: #d80", page)

    def test_docker_build_cache_capability_explains_missing_runtime_socket(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            server.context().docker_api.socket_is_available = lambda: False
            server.context().call_supervisor = lambda method, path, payload=None, timeout=None: {
                "data": {"protected": False, "docker_api": True}
            }
            capability = server.context().docker_build_cache_capability()
            page = server.render_page()

        self.assertFalse(capability["available"])
        self.assertEqual(capability["kind"], server.app_context.docker_capability.RUNTIME_SOCKET_UNAVAILABLE)
        self.assertIn("Docker API socket is not mounted", capability["reason"])
        self.assertIn("Restart HA Ops", capability["remedy"])
        section = page[page.index('action="docker-build-cache-prune"') : page.index("<p class=\"action-flow\"", page.index('action="docker-build-cache-prune"'))]
        self.assertIn('data-capability-available="false" data-action-ready="false"', section)
        self.assertIn("Docker API socket is not mounted", section)

    def test_docker_build_cache_prune_rechecks_capability_before_mutating_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            server.context().call_supervisor = lambda method, path, payload=None, timeout=None: {
                "data": {"protected": True, "docker_api": True}
            }
            before = server.read_state()
            response = self.post_json(server, "/docker-build-cache-prune")
            self.assertEqual(response.responses[-1], 409)
            self.assertEqual(server.read_state(), before)
            payload = json.loads(response.wfile.getvalue().decode())
            self.assertFalse(payload["ok"])
            self.assertIn("Protection mode is enabled", payload["message"])

    def test_disk_usage_skips_docker_after_unavailable_capability(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))
            calls = []

            def call_supervisor(method, path, payload=None, timeout=None):
                calls.append((method, path))
                return {"data": {"protected": True, "docker_api": True}}

            server.context().call_supervisor = call_supervisor
            server.context().docker_api.disk_usage = lambda: self.fail("Docker must not be opened")
            details = "\n".join(server.context().build_disk_usage_summary())
            self.assertIn("Protection mode is enabled", details)
            self.assertIn("turn off Protection mode", details)
            self.assertIn(("GET", "/addons/self/info"), calls)

    def test_docker_build_cache_readiness_matrix_has_disabled_reason(self):
        server = load_server()
        state_store = server.app_context.state_store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().docker_api.socket_is_available = lambda: True
            available = {"data": {"protected": False, "docker_api": True}}
            unavailable = {"data": {"protected": True, "docker_api": True}}
            cases = (
                ("capability", unavailable, {}, "Protection mode is enabled"),
                ("running", available, {"last_status": "running"}, None),
                ("save retry", available, {"save_push_retry_pending": True}, "A Save push retry is pending"),
                (
                    "fence",
                    available,
                    {state_store.DOCKER_PRUNE_FENCE_KEY: {"schema": 1, "operation_id": "fence-id", "phase": "resolution_required", "accepted_at": "2026-07-13T00:00:00+00:00"}},
                    "Resolve or acknowledge",
                ),
            )
            for name, capability, state, hint in cases:
                with self.subTest(name=name):
                    server.context().call_supervisor = lambda method, path, payload=None, timeout=None, capability=capability: capability
                    server.write_state(state)
                    lock = server.context().run_lock if name == "running" else None
                    if lock is not None:
                        self.assertTrue(lock.acquire(blocking=False))
                    try:
                        page = server.render_page()
                    finally:
                        if lock is not None:
                            lock.release()
                    section = page[page.index('action="docker-build-cache-prune"') : page.index("<p class=\"action-flow\"", page.index('action="docker-build-cache-prune"'))]
                    self.assertIn('data-action-ready="false"', section)
                    self.assertIn('class="secondary" disabled', section)
                    if hint is None:
                        self.assertNotIn("docker-prune-hint", section)
                    else:
                        self.assertIn("docker-prune-hint", section)
                        self.assertIn(hint, section)
                    server.write_state({"last_status": "idle", "save_push_retry_pending": False, state_store.DOCKER_PRUNE_FENCE_KEY: None})

    def test_docker_prune_uses_vaadin_confirmation_and_server_disabled_state(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().docker_api.socket_is_available = lambda: True
            server.context().call_supervisor = lambda method, path, payload=None, timeout=None: {
                "data": {"protected": False, "docker_api": True}
            }
            page = server.render_page()
        section = page[page.index('action="docker-build-cache-prune"'):page.index("</form>", page.index('action="docker-build-cache-prune"'))]
        self.assertIn("data-confirm=", section)
        self.assertNotIn(" disabled", section)
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("<vaadin-confirm-dialog", script)
        self.assertIn("confirmMutation", script)
        self.assertIn('form.dataset.confirmed = "true"', script)

    def test_disk_usage_controls_are_same_row_and_prune_copy_is_explicit(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().docker_api.socket_is_available = lambda: True
            server.context().call_supervisor = lambda method, path, payload=None, timeout=None: {
                "data": {"protected": False, "docker_api": True}
            }
            page = server.render_page()
        section = page[page.index("<h2>Disk Usage</h2>") : page.index("<h2>Deleted devices", page.index("<h2>Disk Usage</h2>"))]
        self.assertLess(section.index('action="disk-usage"'), section.index('action="docker-build-cache-prune"'))
        self.assertIn('data-capability-available="true"', section)
        self.assertIn('data-action-ready="true"', section)
        self.assertIn('class="secondary" >Clear build cache</button>', section)
        self.assertIn('data-confirm="', section)
        self.assertNotIn('class="warning"', section)
        self.assertNotIn('docker-prune-hint', section)

    def test_diff_bodies_are_artifactized_outside_state_and_debug_snapshot(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            raw_apply = "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+secret-looking raw body"
            raw_save = "diff --git a/homeassistant/automations.yaml b/homeassistant/automations.yaml\n+raw save body"

            server.write_state({
                "last_diff": raw_apply,
                "last_save_diff": raw_save,
                "last_diff_generated_at": "2026-08-26T00:00:00+00:00",
                "last_save_diff_generated_at": "2026-08-26T00:00:00+00:00",
            })

            state_file = server.STATE_PATH.read_text()
            self.assertNotIn(raw_apply, state_file)
            self.assertNotIn(raw_save, state_file)
            hydrated = server.read_state()
            self.assertEqual(hydrated["last_diff"], raw_apply)
            self.assertEqual(hydrated["last_save_diff"], raw_save)
            debug = server.context().debug_snapshot()
            debug_json = json.dumps(debug)
            self.assertNotIn(raw_apply, debug_json)
            self.assertNotIn(raw_save, debug_json)
            self.assertEqual(debug["state"]["last_diff"], "")
            self.assertEqual(debug["state"]["last_save_diff"], "")
            self.assertEqual(server.context().diff_get(hydrated["last_diff_cursor"]), raw_apply)

    def test_diff_get_rejects_stale_generation_cursor_after_preview_replace(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"last_diff": "old apply diff", "last_preview_paths": ["homeassistant/a.yaml"]})
            old_cursor = server.read_state()["last_diff_cursor"]
            server.write_state({"last_diff": "new apply diff", "last_preview_paths": ["homeassistant/b.yaml"]})

            with self.assertRaises(RuntimeError):
                server.context().diff_get(old_cursor)
            self.assertEqual(server.context().diff_get(server.read_state()["last_diff_cursor"]), "new apply diff")

    def test_diff_get_returns_only_requested_file_detail(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            diff = "\n".join([
                "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml",
                "--- a/homeassistant/a.yaml",
                "+++ b/homeassistant/a.yaml",
                "@@ -1 +1 @@",
                "-old-a",
                "+new-a",
                "diff --git a/homeassistant/b.yaml b/homeassistant/b.yaml",
                "--- a/homeassistant/b.yaml",
                "+++ b/homeassistant/b.yaml",
                "@@ -1 +1 @@",
                "-old-b",
                "+new-b",
            ])
            server.write_state({"last_diff": diff, "last_preview_paths": ["homeassistant/a.yaml", "homeassistant/b.yaml"]})
            cursor = server.read_state()["last_diff_cursor"]

            result = server.web.dispatch_command(server.context(), "diff_get", {"cursor": cursor, "path": "homeassistant/b.yaml"})

            self.assertTrue(result["ok"])
            self.assertIn("new-b", result["diff"])
            self.assertNotIn("new-a", result["diff"])

    def test_diff_cursor_survives_unrelated_state_writes(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"last_diff": "current apply diff", "last_preview_paths": ["homeassistant/a.yaml"]})
            cursor = server.read_state()["last_diff_cursor"]
            generation = cursor["generation"]

            server.write_state({"last_message": "background status update", "last_status": "running"})

            state = server.read_state()
            self.assertEqual(state["operation_generation"], generation)
            self.assertEqual(state["last_diff"], "current apply diff")
            self.assertEqual(server.context().diff_get(cursor), "current apply diff")

    def test_apply_preview_decisions_keep_generation_and_diff_cursor_current(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_diff": "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml\n+apply",
                "last_preview_commit": "apply-commit-a",
                "last_preview_fingerprint": "apply-fingerprint-a",
                "last_preview_live_fingerprints": {
                    "homeassistant/a.yaml": "live-a",
                    "homeassistant/b.yaml": "live-b",
                },
                "last_preview_paths": ["homeassistant/a.yaml", "homeassistant/b.yaml"],
                "last_preview_conflict_paths": ["homeassistant/a.yaml"],
            })
            identity = server.web.preview_identity_for_state(server.read_state(), "apply")
            cursor = server.read_state()["last_diff_cursor"]
            generation = server.read_state()["operation_generation"]
            revision = server.read_state()["state_revision"]

            select = server.web.dispatch_command(
                server.context(),
                "select_apply_preview",
                {
                    "command_id": "33333333-3333-4333-8333-333333333333",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/a.yaml",
                        "selected": "1",
                        "preview_identity": identity,
                    },
                },
            )
            resolve = server.web.dispatch_command(
                server.context(),
                "resolve_apply_preview",
                {
                    "command_id": "44444444-4444-4444-8444-444444444444",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/a.yaml",
                        "choice": "ha",
                        "preview_identity": identity,
                    },
                },
            )

            state = server.read_state()
            self.assertTrue(select["ok"])
            self.assertTrue(resolve["ok"])
            self.assertEqual(state["apply_preview_selected_paths"], ["homeassistant/a.yaml"])
            self.assertEqual(state["apply_preview_resolutions"], {"homeassistant/a.yaml": "ha"})
            self.assertEqual(state["operation_generation"], generation)
            self.assertGreater(state["state_revision"], revision)
            self.assertEqual(server.context().diff_get(cursor), "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml\n+apply")

    def test_save_preview_decisions_keep_generation_and_diff_cursor_current(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_save_diff": "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml\n+save",
                "last_save_preview_commit": "save-commit-a",
                "last_save_preview_fingerprint": "save-fingerprint-a",
                "last_save_preview_paths": ["homeassistant/a.yaml", "homeassistant/b.yaml"],
                "last_save_preview_conflict_paths": ["homeassistant/a.yaml"],
            })
            identity = server.web.preview_identity_for_state(server.read_state(), "save")
            cursor = server.read_state()["last_save_diff_cursor"]
            generation = server.read_state()["operation_generation"]
            revision = server.read_state()["state_revision"]

            select = server.web.dispatch_command(
                server.context(),
                "select_save_preview",
                {
                    "command_id": "55555555-5555-4555-8555-555555555555",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/a.yaml",
                        "selected": "1",
                        "preview_identity": identity,
                    },
                },
            )
            resolve = server.web.dispatch_command(
                server.context(),
                "resolve_save_preview",
                {
                    "command_id": "66666666-6666-4666-8666-666666666666",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/a.yaml",
                        "choice": "git",
                        "preview_identity": identity,
                    },
                },
            )

            state = server.read_state()
            self.assertTrue(select["ok"])
            self.assertTrue(resolve["ok"])
            self.assertEqual(state["save_preview_selected_paths"], ["homeassistant/a.yaml"])
            self.assertEqual(state["save_preview_resolutions"], {"homeassistant/a.yaml": "git"})
            self.assertEqual(state["operation_generation"], generation)
            self.assertGreater(state["state_revision"], revision)
            self.assertEqual(server.context().diff_get(cursor), "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml\n+save")

    def test_same_preview_identity_accepts_controls_captured_before_decision_refresh(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_diff": "diff --git a/homeassistant/a.yaml b/homeassistant/a.yaml\n+apply",
                "last_preview_commit": "apply-commit-a",
                "last_preview_fingerprint": "apply-fingerprint-a",
                "last_preview_live_fingerprints": {"homeassistant/a.yaml": "live-a", "homeassistant/b.yaml": "live-b"},
                "last_preview_paths": ["homeassistant/a.yaml", "homeassistant/b.yaml"],
            })
            identity = server.web.preview_identity_for_state(server.read_state(), "apply")
            generation = server.read_state()["operation_generation"]

            first = server.web.dispatch_command(
                server.context(),
                "select_apply_preview",
                {
                    "command_id": "77777777-7777-4777-8777-777777777777",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/a.yaml",
                        "selected": "1",
                        "preview_identity": identity,
                    },
                },
            )
            second = server.web.dispatch_command(
                server.context(),
                "select_apply_preview",
                {
                    "command_id": "88888888-8888-4888-8888-888888888888",
                    "generation": generation,
                    "payload": {
                        "path": "homeassistant/b.yaml",
                        "selected": "1",
                        "preview_identity": identity,
                    },
                },
            )

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(
                server.read_state()["apply_preview_selected_paths"],
                ["homeassistant/a.yaml", "homeassistant/b.yaml"],
            )

    def test_stale_preview_identity_rejects_same_path_decisions_after_preview_replace(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            path = "homeassistant/configuration.yaml"
            server.write_state({
                "last_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+old",
                "last_save_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+old-save",
                "last_preview_commit": "apply-old",
                "last_preview_fingerprint": "apply-old-fingerprint",
                "last_preview_live_fingerprints": {path: "old-live"},
                "last_preview_paths": [path],
                "last_preview_conflict_paths": [path],
                "last_save_preview_commit": "save-old",
                "last_save_preview_fingerprint": "save-old-fingerprint",
                "last_save_preview_paths": [path],
                "last_save_preview_conflict_paths": [path],
            })
            old_apply_identity = server.web.preview_identity_for_state(server.read_state(), "apply")
            old_save_identity = server.web.preview_identity_for_state(server.read_state(), "save")
            server.write_state({
                "last_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+new",
                "last_save_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+new-save",
                "last_preview_commit": "apply-new",
                "last_preview_fingerprint": "apply-new-fingerprint",
                "last_preview_live_fingerprints": {path: "new-live"},
                "last_preview_paths": [path],
                "last_preview_conflict_paths": [path],
                "apply_preview_selected_paths": [],
                "apply_preview_resolutions": {},
                "last_save_preview_commit": "save-new",
                "last_save_preview_fingerprint": "save-new-fingerprint",
                "last_save_preview_paths": [path],
                "last_save_preview_conflict_paths": [path],
                "save_preview_selected_paths": [],
                "save_preview_resolutions": {},
            })
            current_generation = server.read_state()["operation_generation"]
            cases = [
                ("select_apply_preview", "99999999-9999-4999-8999-999999999991", {"path": path, "selected": "1", "preview_identity": old_apply_identity}),
                ("resolve_apply_preview", "99999999-9999-4999-8999-999999999992", {"path": path, "choice": "ha", "preview_identity": old_apply_identity}),
                ("select_save_preview", "99999999-9999-4999-8999-999999999993", {"path": path, "selected": "1", "preview_identity": old_save_identity}),
                ("resolve_save_preview", "99999999-9999-4999-8999-999999999994", {"path": path, "choice": "git", "preview_identity": old_save_identity}),
            ]

            for command, command_id, payload in cases:
                with self.subTest(command=command):
                    result = server.web.dispatch_command(
                        server.context(),
                        command,
                        {
                            "command_id": command_id,
                            "generation": current_generation,
                            "payload": payload,
                        },
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], 409)

            state = server.read_state()
            self.assertEqual(state["apply_preview_selected_paths"], [])
            self.assertEqual(state["apply_preview_resolutions"], {})
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertEqual(state["save_preview_resolutions"], {})

    def test_legacy_preview_decisions_without_identity_fail_closed_for_non_empty_preview(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            path = "homeassistant/configuration.yaml"
            server.write_state({
                "last_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+apply",
                "last_save_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+save",
                "last_preview_commit": "apply-commit",
                "last_preview_fingerprint": "apply-fingerprint",
                "last_preview_live_fingerprints": {path: "live"},
                "last_preview_paths": [path],
                "last_preview_conflict_paths": [path],
                "last_save_preview_commit": "save-commit",
                "last_save_preview_fingerprint": "save-fingerprint",
                "last_save_preview_paths": [path],
                "last_save_preview_conflict_paths": [path],
            })
            cases = [
                ("/select-apply-preview", {"path": path, "selected": "1"}),
                ("/resolve-apply-preview", {"path": path, "choice": "ha"}),
                ("/select-save-preview", {"path": path, "selected": "1"}),
                ("/resolve-save-preview", {"path": path, "choice": "git"}),
            ]

            for route, payload in cases:
                with self.subTest(route=route):
                    response = self.post_json(server, route, body=urlencode(payload).encode())
                    self.assertEqual(response.responses[-1], 409)
                    result = json.loads(response.wfile.getvalue().decode())
                    self.assertFalse(result["ok"])

            state = server.read_state()
            self.assertEqual(state["apply_preview_selected_paths"], [])
            self.assertEqual(state["apply_preview_resolutions"], {})
            self.assertEqual(state["save_preview_selected_paths"], [])
            self.assertEqual(state["save_preview_resolutions"], {})

    def test_websocket_url_and_commands_are_ingress_relative(self):
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn('const url = new URL("ws", baseUrl());', script)
        self.assertIn('url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";', script)
        self.assertIn('return name.replaceAll("-", "_");', script)

    def test_preview_commands_are_in_websocket_registry(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            calls = []

            def start_job(target, *args, **kwargs):
                calls.append((target.__name__, args, kwargs))
                return True

            save_result = server.web.dispatch_command(server.context(), "save_preview", start_job=start_job)
            apply_result = server.web.dispatch_command(server.context(), "preview", start_job=start_job)

            self.assertTrue(save_result["ok"])
            self.assertTrue(apply_result["ok"])
            self.assertEqual(calls[0][0], "run_save_preview_job")
            self.assertEqual(calls[0][2], {"state_updates": server.app_context.state_store.ALL_PREVIEW_CLEAR_UPDATES, "command_id": None})
            self.assertEqual(calls[1][0], "run_preview_job")
            self.assertEqual(calls[1][2], {"state_updates": server.app_context.state_store.ALL_PREVIEW_CLEAR_UPDATES, "command_id": None})

    def test_ingress_prefixed_ws_route_accepts_upgrade(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            handler = server.web.create_handler(server.context())
            request = handler.__new__(handler)
            request.path = "/api/hassio_ingress/abc123/ws"
            request.rfile = io.BytesIO()
            request.wfile = io.BytesIO()
            request.headers = Message()
            request.headers["Sec-WebSocket-Key"] = "dGhlIHNhbXBsZSBub25jZQ=="
            request.responses = []
            request.response_headers = []
            request.send_response = MethodType(lambda self, status: self.responses.append(status), request)
            request.send_header = MethodType(lambda self, key, value: self.response_headers.append((key, value)), request)
            request.end_headers = MethodType(lambda self: None, request)

            request.do_GET()

            self.assertEqual(request.responses[0], 101)
            self.assertTrue(request.wfile.getvalue())

    def test_save_apply_dispatch_fail_closed_during_startup_repair_for_post_and_ws_registry(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().operation_store.begin_repair()

            save_response = self.post_json(
                server,
                "/save",
                body=b"commit_subject=Save+Subject&default_commit_subject=Default",
            )
            apply_response = self.post_json(server, "/apply")
            ws_save = server.web.dispatch_command(server.context(), "save", {"commit_subject": "WS Save"})
            ws_apply = server.web.dispatch_command(server.context(), "apply")

            self.assertEqual(save_response.responses[-1], 409)
            self.assertEqual(apply_response.responses[-1], 409)
            self.assertFalse(ws_save["ok"])
            self.assertFalse(ws_apply["ok"])
            self.assertNotEqual(server.read_state().get("last_status"), "running")

    def test_state_replay_snapshot_uses_redacted_generation_not_raw_diff(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_diff": "raw apply diff body",
                "last_preview_paths": ["homeassistant/configuration.yaml"],
            })

            replay = server.web.dispatch_command(server.context(), "replay")
            payload = json.dumps(replay)
            self.assertTrue(replay["ok"])
            self.assertEqual(replay["readiness"]["status"], server.app_context.state_store.READINESS_REPAIRED)
            self.assertIn("last_diff_cursor", replay["state"])
            self.assertNotIn("raw apply diff body", payload)

    def test_debug_snapshot_exposes_backend_version(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.ADDON_CONFIG_PATH = root / "config.yaml"
            server.ADDON_CONFIG_PATH.write_text('version: "1.2.3"\n')

            debug = server.web.dispatch_command(server.context(), "debug_snapshot")

            self.assertTrue(debug["ok"])
            self.assertEqual(debug["backend_version"], "1.2.3")

    def test_ws_state_frames_expose_backend_version_on_full_and_patch_frames(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)
            ctx.dev_harness_backend_version = "1.2.3"
            ctx.repair_startup_state()
            full = server.web.ws_state_frames(ctx)[0]
            ctx.write_state({"last_status": "running", "last_message": "Apply still running."})
            state = ctx.read_state()

            patch = server.web.ws_state_frames(ctx, base_revision=state["state_revision"] - 1)[0]

            self.assertEqual(full["type"], "state")
            self.assertEqual(full["backend_version"], "1.2.3")
            self.assertEqual(patch["type"], "state_patch")
            self.assertEqual(patch["backend_version"], "1.2.3")
            self.assertNotIn("backend_version", patch["patch"])

    def test_ws_replay_frames_carry_state_and_log_without_raw_diff(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_diff": "raw apply diff body",
                "last_message": "Apply still running.",
                "last_details": ["line one", "line two"],
            })

            frames = server.web.ws_state_frames(server.context())
            payload = json.dumps(frames)
            self.assertEqual(frames[0]["type"], "state")
            self.assertEqual(frames[0]["state"]["last_message"], "Apply still running.")
            self.assertEqual(frames[0]["state"]["last_details"], ["line one", "line two"])
            self.assertNotIn("raw apply diff body", payload)

    def test_debug_and_ws_snapshots_redact_sensitive_status_and_log_values(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            command_id = "abababab-abab-4bab-8bab-abababababab"
            secrets = {
                "token": "token=top-secret-token",
                "url": "https://operator:password@github.com/private/home-config.git",
                "host": "home-assistant.private.example",
                "path": "/config/secrets.yaml",
            }
            message = "Failure at {url} on {host} with {token}".format(**secrets)
            detail = "Could not read {path}; Authorization: Bearer bearer-secret-value".format(**secrets)
            server.write_state({
                "last_message": message,
                "last_details": [detail],
                "command_records": {
                    command_id: {
                        "command_id": command_id,
                        "command": "preview",
                        "status": "terminal",
                        "result": {"ok": False, "message": message},
                    },
                },
            })

            debug = server.context().debug_snapshot()
            replay = server.web.dispatch_command(server.context(), "replay")
            frames = server.web.ws_state_frames(server.context())
            payload = json.dumps({"debug": debug, "replay": replay, "frames": frames})

            for secret in secrets.values():
                self.assertNotIn(secret, payload)
            self.assertNotIn("bearer-secret-value", payload)
            self.assertIn("[REDACTED_URL]", payload)
            self.assertIn("[REDACTED_HOST]", payload)
            self.assertIn("[REDACTED_PATH]", payload)
            self.assertIn("[REDACTED]", payload)

    def test_ws_replay_frames_carry_redacted_state_without_fragments(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({
                "last_save_diff": "diff --git a/homeassistant/configuration.yaml b/homeassistant/configuration.yaml\n+new",
                "last_save_diff_generated_at": "2026-08-27T00:00:00+00:00",
                "last_save_preview_paths": ["homeassistant/configuration.yaml"],
                "save_preview_selected_paths": ["homeassistant/configuration.yaml"],
            })

            frames = server.web.ws_state_frames(server.context())
            self.assertEqual(frames[0]["type"], "state")
            self.assertNotIn("fragments", frames[0])
            self.assertEqual(frames[0]["state"]["last_save_preview_paths"], ["homeassistant/configuration.yaml"])
            self.assertEqual(frames[0]["state"]["last_save_diff"], "")

    def test_ws_patch_uses_explicit_base_and_revision(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)
            ctx.repair_startup_state()
            ctx.run_lock.acquire()
            try:
                ctx.write_state({"last_status": "running", "last_message": "Apply still running."})
                state = ctx.read_state()
                frame = server.web.ws_state_frames(ctx, base_revision=state["state_revision"] - 1)[0]
            finally:
                ctx.run_lock.release()

            self.assertEqual(frame["type"], "state_patch")
            self.assertEqual(frame["base_revision"], frame["revision"] - 1)
            self.assertEqual(frame["patch"]["last_status"], "running")

    def test_operation_store_state_change_sequence_advances_on_background_updates(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            initial = server.context().state_change_sequence()

            server.write_state({"last_status": "running", "last_details": ["background line"]})
            changed = server.context().wait_for_state_change(initial, timeout=0)

            self.assertGreater(changed, initial)
            frames = server.web.ws_state_frames(server.context())
            self.assertEqual(frames[0]["state"]["last_details"], ["background line"])

    def test_durable_command_claim_deduplicates_ws_and_http_dispatch(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            command_id = "11111111-1111-4111-8111-111111111111"
            generation = server.read_state()["operation_generation"]
            envelope = {
                "command_id": command_id,
                "generation": generation,
                "payload": {},
            }
            scheduled = []

            def start_job(target, *args, **kwargs):
                scheduled.append((target.__name__, args, kwargs))
                return True

            first = server.web.dispatch_command(server.context(), "preview", envelope, start_job=start_job)
            duplicate = server.web.dispatch_command(server.context(), "preview", envelope, start_job=start_job)

            self.assertTrue(first["ok"])
            self.assertTrue(duplicate["ok"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(server.read_state()["command_records"][command_id]["status"], "accepted")

    def test_restart_marks_unresolved_durable_command_failed_unknown(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            command_id = "22222222-2222-4222-8222-222222222222"
            store = server.app_context.state_store.OperationStore(server.STATE_PATH)
            generation = store.read_state()["operation_generation"]
            claimed, _record = store.claim_command(command_id, "preview", generation, {})
            self.assertTrue(claimed)
            store.update_command(command_id, "running")

            restarted = server.app_context.state_store.OperationStore(server.STATE_PATH)
            restarted.begin_repair()
            restarted.mark_repaired()

            record = restarted.read_state()["command_records"][command_id]
            self.assertEqual(record["status"], "failed_unknown")
            self.assertFalse(record["result"]["ok"])

    def test_terminal_command_id_remains_deduplicated_after_more_than_200_commands(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            store = server.app_context.state_store.OperationStore(server.STATE_PATH)
            generation = store.read_state()["operation_generation"]
            first_id = None

            for index in range(201):
                command_id = str(uuid.UUID(int=index + 1))
                first_id = first_id or command_id
                claimed, _record = store.claim_command(command_id, "preview", generation, {})
                self.assertTrue(claimed)
                terminal = store.update_command(
                    command_id,
                    "terminal",
                    {"ok": True, "message": f"completed {index}"},
                )
                self.assertEqual(terminal["status"], "terminal")

            claimed, record = store.claim_command(first_id, "preview", generation, {})

            self.assertFalse(claimed)
            self.assertEqual(record["status"], "terminal")
            self.assertEqual(record["result"], {"ok": True, "message": "completed 0"})
            self.assertEqual(len(store.read_state()["command_records"]), 201)

    def test_ws_command_path_does_not_write_http_response_after_upgrade_failure(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().operation_store.begin_repair()
            result = server.web.dispatch_command(server.context(), "apply")

            self.assertFalse(result["ok"])
            self.assertEqual(result["message"], server.app_context.state_store.READINESS_BLOCKED_MESSAGE)

    def test_ws_mvp_has_no_reload_dependency_for_running_or_command_results(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.write_state({"last_status": "running", "last_message": "Apply still running."})
            page = server.render_page()

        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        self.assertIn("this.connect();", script)
        self.assertNotIn("reloadSoon", script)
        command_flow = script[script.index("async dispatchMutation(form)"):script.index("observeBackendVersion(version)")]
        self.assertNotIn("window.location.reload", command_flow)
        self.assertIn("this.applyPatch(frame)", script)

    def test_frontend_version_mismatch_guard_is_present_in_source_and_build(self):
        source = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()
        built = (ROOT / "app" / "static" / "ha-ops.js").read_text()

        for script in (source, built):
            self.assertIn("observeBackendVersion", script)
            self.assertIn("acknowledgeVersionMismatch", script)
            self.assertIn("window.location.reload", script)
            self.assertIn("Reload HA Ops", script)
            self.assertIn("Acknowledge Risks & Continue", script)
            self.assertIn("rgba(0, 0, 0, 0.33)", script)
            self.assertIn("@cancel=", script)

    def test_reactive_components_keep_scrollable_log_and_preview_controls(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            page = server.render_page()

        style = page.split("<style>", 1)[1].split("</style>", 1)[0]
        script = (ROOT / "frontend" / "src" / "ha-ops.js").read_text()

        self.assertIn("height: var(--details-card-height, 500px);", style)
        self.assertIn("overflow-y: auto;", style)
        self.assertIn("observeLayout()", script)
        self.assertIn("setAll(expanded)", script)
        self.assertIn("setExpanded(expanded)", script)
        self.assertIn("vaadin-details", script)
        self.assertIn("opened-changed", script)
        self.assertIn("diff-get", script)
        self.assertIn('querySelector("#reactive-previews[data-testid=', script)
        self.assertIn("syncPreviewMount()", script)
        self.assertIn("customElements.define(\"ha-ops-preview\"", script)

    def test_operation_store_blocks_direct_job_calls_when_repair_not_repaired(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.configure_paths(server, root)
            server.context().operation_store.begin_repair()

            self.assertFalse(server.run_save_job())
            self.assertFalse(server.run_apply_job())
            state = server.read_state()
            self.assertIn("startup repair not complete", state["last_message"])

    def test_dev_harness_refuses_live_homeassistant_roots(self):
        _server, harness = load_dev_harness()
        for unsafe in ("/data", "/homeassistant", "/addon_configs", "/backup", "/Users/purportex/Work/HA/ha-config"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(RuntimeError):
                    harness.safe_root_guard(unsafe, explicit=True)

    def test_dev_harness_seeds_disposable_fixture_tree_and_fake_repo(self):
        _server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            fixture = harness.seed_fixture_root(Path(tmp))

            self.assertTrue((fixture["data_dir"] / "options.json").exists())
            self.assertTrue((fixture["config_dir"] / "configuration.yaml").exists())
            self.assertTrue((fixture["addon_configs_dir"] / "local_mqtt" / "options.json").exists())
            self.assertTrue((fixture["remote_dir"] / "HEAD").exists())
            self.assertEqual(fixture["options"]["repo_path"], "ha-config")
            self.assertTrue(str(fixture["root"]).startswith(str(Path(tempfile.gettempdir()).resolve())))

    def test_dev_harness_fake_supervisor_records_and_rejects_live_risk_calls(self):
        _server, harness = load_dev_harness()
        fake = harness.FakeSupervisor()

        addons = fake.call("GET", "/addons")
        self.assertEqual(addons["addons"][0]["slug"], "local_mqtt")
        with self.assertRaises(RuntimeError):
            fake.call("POST", "/core/restart")

        self.assertEqual(fake.calls[0]["path"], "/addons")
        self.assertEqual(fake.calls[1]["path"], "/core/restart")
        self.assertTrue(fake.calls[1]["forbidden"])

    def test_dev_harness_preview_wrappers_preserve_action_identity(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)

            self.assertEqual(server.web.job_action(ctx.run_preview_job), "preview")
            self.assertEqual(server.web.job_action(ctx.run_save_preview_job), "save_preview")

    def test_dev_harness_can_override_backend_version_without_state_patch_pollution(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)

            result = ctx.dev_harness_handle_post("/__dev_harness__/backend-version", {"version": ["9.8.7"]})
            frame = server.web.ws_state_frames(ctx)[0]

            self.assertEqual(result, {"ok": True, "backend_version": "9.8.7"})
            self.assertEqual(ctx.addon_version(), "9.8.7")
            self.assertEqual(frame["backend_version"], "9.8.7")
            self.assertNotIn("backend_version", frame["state"])

    def test_dev_harness_diagnostics_are_absent_from_default_context(self):
        server = load_server()
        with tempfile.TemporaryDirectory() as tmp:
            self.configure_paths(server, Path(tmp))

            response = self.get_json_context(server.web, server.context(), "/__dev_harness__/diagnostics")

            self.assertEqual(response.responses[-1], 404)
            self.assertNotIn(b"dev_harness", response.wfile.getvalue())

    def test_ingress_prefixed_post_preview_and_save_preview_use_normalized_routes(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)
            ctx.repair_startup_state()

            for action, endpoint in (("preview", "preview"), ("save_preview", "save-preview")):
                with self.subTest(action=action):
                    self.post_json_context(
                        server.web,
                        ctx,
                        f"/api/hassio_ingress/local-ha-ops/__dev_harness__/arm",
                        body=f"action={action}&gate=running".encode(),
                    )
                    first = self.post_json_context(
                        server.web,
                        ctx,
                        f"/api/hassio_ingress/local-ha-ops/{endpoint}",
                    )
                    self.assertEqual(first.responses[-1], 200)
                    self.assertTrue(
                        self.wait_until(
                            lambda: ctx.harness_controller.diagnostics(ctx)["gates"][f"{action}:running"]["held"]
                        )
                    )
                    duplicate = self.post_json_context(
                        server.web,
                        ctx,
                        f"/api/hassio_ingress/local-ha-ops/{endpoint}",
                    )
                    self.assertEqual(duplicate.responses[-1], 409)
                    diagnostics = ctx.harness_controller.diagnostics(ctx)
                    self.assertEqual(diagnostics["counters"]["started_jobs"][action], 1)
                    self.assertEqual(diagnostics["counters"]["duplicate_rejections"][action], 1)

                    self.post_json_context(
                        server.web,
                        ctx,
                        f"/api/hassio_ingress/local-ha-ops/__dev_harness__/release",
                        body=f"action={action}&gate=running".encode(),
                    )
                    self.assertTrue(
                        self.wait_until(
                            lambda: ctx.harness_controller.diagnostics(ctx)["counters"]["completed_jobs"].get(action) == 1
                        )
                    )
                    self.assertEqual(ctx.read_state()["last_status"], "success")

    def test_dev_harness_diagnostics_are_redacted_and_expose_no_raw_diff(self):
        server, harness = load_dev_harness()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = harness.create_context(root=Path(tmp), keep_root=True)
            ctx.repair_startup_state()
            ctx.harness_controller.arm("preview", "running")
            self.post_json_context(server.web, ctx, "/preview")
            self.assertTrue(
                self.wait_until(lambda: ctx.harness_controller.diagnostics(ctx)["gates"]["preview:running"]["held"])
            )
            ctx.harness_controller.release("preview", "running")
            self.assertTrue(
                self.wait_until(lambda: ctx.harness_controller.diagnostics(ctx)["state"]["last_status"] == "success")
            )

            response = self.get_json_context(server.web, ctx, "/__dev_harness__/diagnostics")
            payload = response.wfile.getvalue().decode()
            snapshot = self.get_json_context(server.web, ctx, "/debug-snapshot").wfile.getvalue().decode()

            self.assertEqual(response.responses[-1], 200)
            self.assertIn("preview", payload)
            self.assertNotIn("diff --git", payload)
            self.assertNotIn("diff --git", snapshot)

if __name__ == "__main__":
    unittest.main()
