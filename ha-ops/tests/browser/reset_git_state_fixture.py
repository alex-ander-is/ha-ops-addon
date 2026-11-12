"""Disposable exact sibling-force-update incident; never targets live HA."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

APP = Path(__file__).resolve().parents[2] / "app"
sys.path.insert(0, str(APP))
import app_context
import dev_harness
import state


def legacy_updates(branch="main", accepted=304, current=305):
    owner = str(uuid.uuid4())
    digest = hashlib.sha256(b"{}").hexdigest()
    message = (f"Local {branch} contains commits absent from origin/{branch}. "
               "Preserve or publish them before this operation; HA Ops will not reset the branch.")
    operation = {"command_id": owner, "command": "reset_git_state", "payload_sha256": digest,
                 "accepted_generation": accepted, "phase": "recovery_required"}
    record = {"command_id": owner, "command": "reset_git_state", "generation": accepted,
              "payload_sha256": digest, "status": "terminal",
              "result": {"ok": False, "status": "error", "safe_terminal": False, "message": message}}
    return {"operation_generation": current, "active_operation": operation, "command_records": {owner: record},
            "last_status": "error", "last_action": "reset_git_state", "last_message": message,
            "last_details": [message], "reset_intent": None}


def seed_incident(ctx, service_checkout=False, unpublished=False):
    options = ctx.load_options()
    repo = ctx.ensure_repo(options)
    seed = ctx.dev_harness_root / "seed-repo"
    run = dev_harness._run
    parent = run(["git", "rev-parse", "main"], cwd=seed).stdout.strip()
    path = seed / "homeassistant" / "configuration.yaml"
    path.write_text("# published A\ndefault_config:\n")
    run(["git", "add", "-A"], cwd=seed)
    run(["git", "commit", "-m", "Published sibling A"], cwd=seed)
    if unpublished:
        # The same preservation path also protects a truly unpublished local tip.
        run(["git", "fetch", str(seed), "main"], cwd=repo)
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo)
    else:
        run(["git", "push", "origin", "main"], cwd=seed)
        ctx.ensure_repo(options)
    original = run(["git", "rev-parse", "main"], cwd=repo).stdout.strip()
    if not unpublished:
        run(["git", "reset", "--hard", parent], cwd=seed)
        path.write_text("# remote sibling B\ndefault_config:\n")
        run(["git", "add", "-A"], cwd=seed)
        run(["git", "commit", "-m", "Force-updated sibling B"], cwd=seed)
        run(["git", "push", "--force", "origin", "main"], cwd=seed)
    run(["git", "fetch", "origin"], cwd=repo)
    target = run(["git", "rev-parse", "origin/main"], cwd=repo).stdout.strip()
    if service_checkout:
        run(["git", "checkout", "ha-ops/ha-live"], cwd=repo)
    return repo, original, target


class FixtureContext(dev_harness.DevHarnessContext):
    def dev_harness_handle_post(self, route, body):
        if route == "/__dev_harness__/reset-case":
            case = dev_harness._first(body, "case")
            if case == "release":
                self.reset_gate.set()
                return {"ok": True}
            if case == "arm":
                self.reset_gate = threading.Event()
                return {"ok": True}
            updates = legacy_updates()
            if case in {"unknown", "apply", "save"}:
                op = updates["active_operation"]
                op["command"] = case
                updates["command_records"][op["command_id"]]["command"] = case
            elif case != "legacy":
                raise ValueError("unknown fixture case")
            self.write_state(updates)
            return {"ok": True}
        return super().dev_harness_handle_post(route, body)

    def run_command(self, command, **kwargs):
        gate = getattr(self, "reset_gate", None)
        if command[:3] == ["git", "fetch", "origin"] and gate is not None and not gate.is_set():
            if not gate.wait(30):
                raise RuntimeError("fixture Reset gate timed out")
        return super().run_command(command, **kwargs)

    def dev_harness_handle_get(self, route, parsed):
        if route == "/__dev_harness__/reset-proof":
            repo = self.repo_checkout_path(self.load_options())
            run = dev_harness._run
            ref = "refs/ha-ops/recovery/reset/" + self.reset_original
            current = self.read_state()
            return {"ok": True, "main": run(["git", "rev-parse", "main"], cwd=repo).stdout.strip(),
                    "preserved": run(["git", "rev-parse", ref], cwd=repo).stdout.strip(),
                    "remote_main": run(["git", "ls-remote", "origin", "refs/heads/main"], cwd=repo).stdout.split()[0],
                    "dirty": run(["git", "status", "--porcelain"], cwd=repo).stdout.strip(),
                    "live_unchanged": self.reset_live == {str(p): p.read_bytes() for p in self.config_dir.rglob("*") if p.is_file()},
                    "original": self.reset_original, "target": self.reset_target,
                    "active": current["active_operation"], "intent": current["reset_intent"],
                    "selections": [current["apply_preview_selected_paths"], current["save_preview_selected_paths"]]}
        return super().dev_harness_handle_get(route, parsed)

    # The actual production Preview catches the same divergence before any live write.
    run_preview_job = app_context.AppContext.run_preview_job


def create_fixture(root=None, service_checkout=True):
    root = Path(root or tempfile.mkdtemp(prefix="haops-reset-fixture-"))
    ctx = FixtureContext(root)
    ctx.releases_dir.mkdir(parents=True, exist_ok=True)
    ctx.repair_startup_state()
    repo, original, target = seed_incident(ctx, service_checkout=service_checkout)
    ctx.write_state(legacy_updates())
    ctx.reset_original = original
    ctx.reset_target = target
    ctx.reset_live = {str(p): p.read_bytes() for p in ctx.config_dir.rglob("*") if p.is_file()}
    return ctx, repo, original, target


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer
    import shutil
    import signal
    import web
    web.POST_ENDPOINTS += ("/__dev_harness__/reset-case",)
    web.GET_ENDPOINTS += ("/__dev_harness__/reset-proof",)
    root = Path(tempfile.mkdtemp(prefix="haops-reset-fixture-"))
    httpd = None
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        ctx, repo, original, target = create_fixture(root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.create_handler(ctx))
        ctx.port = httpd.server_address[1]
        print(json.dumps({"url": dev_harness.ingress_base_url(ctx), "root": str(ctx.dev_harness_root),
                          "repo": str(repo), "original": original, "target": target}), flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if httpd is not None:
            httpd.server_close()
        shutil.rmtree(root)
