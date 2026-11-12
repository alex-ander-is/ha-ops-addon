"""Explicit, journaled Git recovery. Never writes live HA or the remote user ref."""
from dataclasses import dataclass, replace
import hashlib
import json
import shutil
import uuid
from contextlib import contextmanager

import git_ops
import jobs
import state
import sync


@dataclass(frozen=True)
class ResetOutcome:
    ok: bool
    message: str
    operation: dict
    generation: int
    no_effect: bool = False
    candidate_cleanup: object = None

    def __bool__(self):
        return self.ok


class OwnershipChanged(RuntimeError):
    pass


def candidate_pin_ref(flow_id, name, oid):
    return f"refs/ha-ops/reset-candidates/{flow_id}/{name}/{oid}"


def scratch_owned(directory, intent):
    marker = directory / "owner.json"
    if directory.is_symlink() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        found = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(found, dict) and found.get("schema") == 1
            and found.get("flow_id") == intent["flow_id"] and found.get("identity") == intent["identity"]
            and state.canonical_uuid(found.get("owner_id")))


def remove_owned_scratch(directory, intent):
    """Retain the ownership marker until every owned payload entry is gone."""
    marker = directory / "owner.json"
    expected = json.loads(marker.read_text())
    for child in directory.iterdir():
        if child.name == "owner.json":
            continue
        if not scratch_owned(directory, intent) or json.loads(marker.read_text()) != expected:
            raise RuntimeError("Reset scratch ownership changed during cleanup.")
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
        if child.exists() or child.is_symlink():
            raise RuntimeError("Reset scratch cleanup is incomplete; explicitly retry Reset.")
    if not scratch_owned(directory, intent) or json.loads(marker.read_text()) != expected:
        raise RuntimeError("Reset scratch ownership changed during cleanup.")
    marker.unlink()
    directory.rmdir()


def clean_candidate_scratch(ctx, intent, check):
    root = ctx.work_dir / "reset-builds"
    if root.is_symlink():
        raise RuntimeError("Reset scratch root is symbolic; manual inspection is required.")
    if not root.exists():
        return
    for directory in root.iterdir():
        suffix = directory.name.removeprefix(intent["flow_id"] + "-")
        if directory.name != intent["flow_id"] and not (directory.name.startswith(intent["flow_id"] + "-") and state.canonical_uuid(suffix)):
            continue
        check()
        if scratch_owned(directory, intent):
            remove_owned_scratch(directory, intent)
        elif directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            # A crash between mkdir and marker publication owns no content.
            directory.rmdir()
        # Unverified files/nonempty directories/symlinks are preserved.


@contextmanager
def candidate_directory(ctx, intent, check):
    """Use a fresh owned location when an unverified old bootstrap path remains."""
    root = ctx.work_dir / "reset-builds"
    clean_candidate_scratch(ctx, intent, check)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / intent["flow_id"]
    if directory.exists() or directory.is_symlink():
        directory = root / (intent["flow_id"] + "-" + str(uuid.uuid4()))
    expected = {"schema": 1, "flow_id": intent["flow_id"], "identity": intent["identity"], "owner_id": intent["owner_id"]}
    directory.mkdir(mode=0o700)
    try:
        (directory / "owner.json").write_text(json.dumps(expected))
        yield directory
    finally:
        # Setup errors are covered too; no unverified recursive deletion.
        if scratch_owned(directory, intent) and json.loads((directory / "owner.json").read_text()) == expected:
            remove_owned_scratch(directory, intent)
        elif directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()


def retire_completed_candidate_pins(ctx):
    """Best-effort retirement only after the durable successful terminal record."""
    current = ctx.read_state()
    options = ctx.load_options()
    repo = ctx.repo_checkout_path(options)
    origin = ctx.run_command(["git", "remote", "get-url", "origin"], cwd=repo)
    if origin.returncode:
        return
    identity = hashlib.sha256((str(repo.resolve()) + "\0" + origin.stdout.strip() + "\0" + str(options.get("repo_branch") or "main")).encode()).hexdigest()
    for command_id, record in current["command_records"].items():
        result = record.get("result") if isinstance(record, dict) else None
        cleanup = result.get("reset_candidate_cleanup") if isinstance(result, dict) else None
        if (record.get("command") != "reset_git_state" or record.get("status") != "terminal"
            or not isinstance(result, dict) or result.get("ok") is not True or not isinstance(cleanup, dict)
            or cleanup.get("repo") != str(repo.resolve()) or cleanup.get("identity") != identity
            or not state.canonical_uuid(cleanup.get("flow_id"))
            or not isinstance(cleanup.get("candidates"), dict) or set(cleanup["candidates"]) != set(state.RESET_SERVICE_BRANCHES)):
            continue
        settled = True
        for name, oid in cleanup["candidates"].items():
            if not isinstance(oid, str) or len(oid) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in oid):
                settled = False
                continue
            ref = candidate_pin_ref(cleanup["flow_id"], name, oid)
            symbolic = ctx.run_command(["git", "symbolic-ref", "-q", ref], cwd=repo)
            if symbolic.returncode == 0:
                # A replacement is foreign. Relinquish this old ownership claim.
                continue
            found = ctx.run_command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo)
            if found.returncode == 0 and found.stdout.strip() == oid:
                removed = ctx.run_command(["git", "update-ref", "--no-deref", "-d", ref, oid], cwd=repo)
                if removed.returncode:
                    settled = False
            elif found.returncode not in {0, 1}:
                settled = False
        if settled:
            ctx.operation_store.settle_reset_pin_cleanup(command_id, cleanup)


def run(ctx, command_id=None, lock_acquired=False):
    options = ctx.load_options()
    current = ctx.read_state()
    operation = current.get(state.ACTIVE_OPERATION_KEY)
    direct = not lock_acquired
    if (not direct and command_id is not None
        and (not isinstance(operation, dict) or operation.get("command_id") != command_id)):
        jobs.release_run_lock(ctx)
        return ResetOutcome(False, "Reset ownership changed; explicit recovery is required.",
                            {"command_id": command_id}, current["operation_generation"])
    if direct:
        # CLI/internal callers use exactly the same durable admission as HTTP/WS.
        try:
            owner = str(uuid.uuid4())
            ctx.claim_command(owner, "reset_git_state", current["operation_generation"], {})
            ctx.update_command(owner, "running")
            ctx.write_state(state.ALL_PREVIEW_CLEAR_UPDATES)
        except (RuntimeError, ValueError):
            return False
    if not jobs.enter_run_lock(ctx, "reset_git_state", lock_acquired):
        if direct:
            refused = ctx.read_state()
            ctx.update_command(owner, "terminal", outcome_result(ResetOutcome(
                False, "Reset could not reserve the job lock.", refused.get(state.ACTIVE_OPERATION_KEY) or {},
                refused["operation_generation"], no_effect=refused.get("reset_intent") is None)))
        return False
    current = ctx.read_state()
    if not direct and current.get(state.ACTIVE_OPERATION_KEY) != operation:
        jobs.release_run_lock(ctx)
        return ResetOutcome(False, "Reset ownership changed; explicit recovery is required.",
                            operation or {}, current["operation_generation"])
    operation = current.get(state.ACTIVE_OPERATION_KEY)
    generation = current["operation_generation"]
    details = []
    intent = current.get("reset_intent")
    journal_started = intent is not None
    outcome = None

    def publish(updates):
        nonlocal generation
        try:
            result = ctx.operation_store.publish_reset_state(updates, operation, generation)
        except RuntimeError as exc:
            raise OwnershipChanged(str(exc)) from exc
        generation = result["operation_generation"]
        return result

    def check():
        publish({})

    def command(args, **kwargs):
        check()
        result = ctx.run_command(args, **kwargs)
        check()
        return result

    def git(args):
        result = command(["git", *args], cwd=repo, env=env)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Git recovery command failed.")
        return result.stdout.strip()

    def checkpoint(phase, **updates):
        nonlocal intent, journal_started
        candidate = {**intent, **updates, "phase": phase, "generation": generation}
        publish({"reset_intent": candidate})
        intent = candidate
        journal_started = True

    def pins(candidates, remove=False):
        for name, oid in candidates.items():
            ref = candidate_pin_ref(intent["flow_id"], name, oid)
            symbolic = command(["git", "symbolic-ref", "-q", ref], cwd=repo, env=env)
            if symbolic.returncode == 0:
                raise RuntimeError("Reset candidate pin is symbolic; refusing overwrite.")
            observed = command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, env=env)
            if observed.returncode == 0 and observed.stdout.strip() != oid:
                raise RuntimeError("Reset candidate pin collision; refusing overwrite.")
            if remove:
                if observed.returncode == 0:
                    git(["update-ref", "--no-deref", "-d", ref, oid])
            elif observed.returncode != 0:
                git(["update-ref", "--no-deref", ref, oid, "0" * len(oid)])

    def clean():
        git_ops.assert_no_untracked_files(repo, command)
        if git(["status", "--porcelain"]):
            raise RuntimeError("Git checkout contains uncommitted changes; commit or move them before Reset Git State.")
        if git_ops.git_rebase_in_progress(repo, command) or (repo / ".git" / "MERGE_HEAD").exists():
            raise RuntimeError("Git merge/rebase is in progress; resolve it before Reset Git State.")

    def remote_refs():
        output = git(["ls-remote", "--refs", "origin", *[f"refs/heads/{name}" for name in (branch, *state.RESET_SERVICE_BRANCHES)]])
        return {ref.removeprefix("refs/heads/"): oid for oid, ref in (line.split() for line in output.splitlines())}

    def verify_remote():
        observed = remote_refs()
        if observed.get(branch) != intent["target_tip"]:
            raise RuntimeError("Remote user branch changed during Reset; inspect Git and explicitly retry recovery.")
        for name, old in intent["service_old"].items():
            if observed.get(name) not in {old, intent["service_candidates"].get(name)}:
                raise RuntimeError(f"Remote {name} changed during Reset; refusing to overwrite another writer.")
        return observed

    try:
        if (not isinstance(operation, dict) or operation.get("command") != "reset_git_state"
            or command_id is not None and operation.get("command_id") != command_id):
            raise OwnershipChanged("Reset has no durable owner.")
        publish({"last_run_at": ctx.utc_now(), "last_action": "reset_git_state", "last_status": "running",
                 "last_message": "Resetting Git state; previous local history will be preserved.", "last_details": []})
        branch = str(options.get("repo_branch") or "main")
        if (not branch or branch.startswith("-") or any(branch == name or branch.startswith(name + "/") or name.startswith(branch + "/") for name in state.RESET_SERVICE_BRANCHES)):
            raise RuntimeError("Configured user branch conflicts with HA Ops service branches.")
        repo = ctx.repo_checkout_path(options)
        env = ctx.git_env(options)
        if not (repo / ".git").is_dir():
            # Initial setup follows the normal safe clone path, with no local history to recover.
            repo = ctx.ensure_repo(options, reset_to_origin=False)
            check()
        git(["check-ref-format", f"refs/heads/{branch}"])
        clean()
        origin = git(["remote", "get-url", "origin"])
        if origin != str(options.get("repo_url") or "").strip():
            raise RuntimeError("Checkout origin does not match configured repository.")
        identity = hashlib.sha256((str(repo.resolve()) + "\0" + origin + "\0" + branch).encode()).hexdigest()
        git(["fetch", "origin"])
        target = git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"])
        original = git(["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"])
        if intent is not None:
            if (not state.valid_reset_intent(intent, operation, ctx.read_state()["command_records"])
                or intent.get("identity") != identity or intent["repo"] != str(repo.resolve())
                or intent["origin"] != hashlib.sha256(origin.encode()).hexdigest() or intent["branch"] != branch):
                raise RuntimeError("Reset checkpoint does not match this repository; manual inspection is required.")
            if original not in {intent["original_tip"], intent["target_tip"], intent.get("alignment_from")}:
                raise RuntimeError("User branch changed since Reset checkpoint; refusing unsafe recovery.")
            if target != intent["target_tip"]:
                # A fresh explicit successor may adopt a newer remote user tip;
                # retain the original A and the intermediate aligned tip too.
                observed = remote_refs()
                if observed.get(branch) != target:
                    raise RuntimeError("Remote user branch changed after fetch; explicitly retry Reset.")
                for name, old in intent["service_old"].items():
                    if observed.get(name) not in {old, intent["service_candidates"].get(name)}:
                        raise RuntimeError(f"Remote {name} changed during Reset; refusing to overwrite another writer.")
                if intent.get("candidate_pins"):
                    pins(intent["candidate_pins"], remove=True)
                intent = {key: value for key, value in intent.items() if key not in {"service_local_old", "candidate_pins"}}
                checkpoint("prepared", target_tip=target, alignment_from=original,
                           service_old={name: observed.get(name) for name in state.RESET_SERVICE_BRANCHES}, service_candidates={})
            verify_remote()
        else:
            observed = remote_refs()
            if observed.get(branch) != target:
                raise RuntimeError("Remote user branch changed after fetch; explicitly retry Reset.")
            intent = {"schema": 1, "flow_id": operation["command_id"], "owner_id": operation["command_id"],
                      "accepted_generation": operation["accepted_generation"], "generation": generation,
                      "identity": identity, "repo": str(repo.resolve()), "origin": hashlib.sha256(origin.encode()).hexdigest(), "branch": branch,
                      "original_tip": original, "target_tip": target,
                      "recovery_ref": "refs/ha-ops/recovery/reset/" + original,
                      "service_old": {name: observed.get(name) for name in state.RESET_SERVICE_BRANCHES},
                      "service_candidates": {}, "phase": "prepared"}
            checkpoint("prepared")
        for preserved_tip in {intent["original_tip"], intent.get("alignment_from", intent["original_tip"])}:
            recovery = "refs/ha-ops/recovery/reset/" + preserved_tip
            symbolic = command(["git", "symbolic-ref", "-q", recovery], cwd=repo, env=env)
            if symbolic.returncode == 0:
                raise RuntimeError("Recovery reference is symbolic; refusing Reset.")
            existing = command(["git", "rev-parse", "--verify", "--quiet", recovery], cwd=repo, env=env)
            if existing.returncode == 0:
                if existing.stdout.strip() != preserved_tip:
                    raise RuntimeError("Recovery reference collision; refusing Reset.")
            else:
                git(["update-ref", "--no-deref", recovery, preserved_tip, "0" * len(preserved_tip)])
            if git(["rev-parse", "--verify", recovery]) != preserved_tip:
                raise RuntimeError("Previous local history could not be preserved.")
        if intent["phase"] == "prepared":
            checkpoint("preserved")
        if original in {intent["original_tip"], intent.get("alignment_from")} and original != intent["target_tip"]:
            clean()
            verify_remote()
            if git(["rev-parse", f"refs/heads/{branch}"]) != original:
                raise RuntimeError("Local user branch changed before alignment; refusing Reset.")
            git(["checkout", branch])
            clean()
            if git(["rev-parse", f"refs/heads/{branch}"]) != original:
                raise RuntimeError("Local user branch changed before alignment; refusing Reset.")
            git(["reset", "--keep", intent["target_tip"]])
        if git(["rev-parse", f"refs/heads/{branch}"]) != intent["target_tip"]:
            raise RuntimeError("Local user branch does not match Reset target.")
        if intent["phase"] in {"prepared", "preserved"}:
            checkpoint("aligned")
        # The canonical checkout is only aligned with --keep. All export, hard
        # reset, staging and commits happen in an exclusively owned scratch clone.
        git(["checkout", branch])
        clean()
        def verify_local():
            clean()
            if git(["rev-parse", f"refs/heads/{branch}"]) != intent["target_tip"]:
                raise RuntimeError("Local user branch changed during Reset; refusing publication.")
        verify_local()
        if not intent["service_candidates"]:
            if intent.get("candidate_pins"):
                pins(intent["candidate_pins"], remove=True)
                intent = {key: value for key, value in intent.items() if key != "candidate_pins"}
                checkpoint(intent["phase"])
            if "service_local_old" not in intent:
                local_old = {}
                for name in state.RESET_SERVICE_BRANCHES:
                    observed = command(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo, env=env)
                    local_old[name] = observed.stdout.strip() if observed.returncode == 0 else None
                checkpoint(intent["phase"], service_local_old=local_old)
            addons = ctx.get_installed_addons()
            check()
            verify_local()
            with candidate_directory(ctx, intent, check) as scratch:
                candidate_repo = scratch / "repo"
                clone = command(["git", "clone", "--no-hardlinks", "--no-checkout", "--", str(repo), str(candidate_repo)], env=env)
                if clone.returncode:
                    raise RuntimeError(clone.stderr.strip() or "Reset candidate clone failed.")
                # Materialize the immutable target before reading its manifest.
                checkout = command(["git", "checkout", "--detach", intent["target_tip"]], cwd=candidate_repo, env=env)
                if checkout.returncode:
                    raise RuntimeError(checkout.stderr.strip() or "Reset candidate checkout failed.")
                manifest, _path = ctx.load_manifest(candidate_repo, options, addons)
                check()
                targets = ctx.resolve_targets(candidate_repo, manifest, addons, require_source=False)
                check()
                verify_local()
                def add_detail(_details, message):
                    details.append(message)
                    publish({"last_details": list(details)})
                deps = replace(ctx.sync_deps(), run_command=command, add_detail=add_detail, work_dir=scratch / "work")
                deps.work_dir.mkdir()
                sync.reset_service_branches_from_main(targets, candidate_repo, intent["target_tip"], details, deps)
                check()
                candidates = {}
                for name in state.RESET_SERVICE_BRANCHES:
                    result = command(["git", "rev-parse", f"refs/heads/{name}"], cwd=candidate_repo, env=env)
                    if result.returncode:
                        raise RuntimeError("Reset candidate reference is unavailable.")
                    candidates[name] = result.stdout.strip()
                verify_local()
                # Transfer objects only; no canonical branch or index is changed.
                git(["fetch", "--no-tags", str(candidate_repo), *candidates.values()])
                verify_local()
                # Journal ownership before creating pins; every published candidate
                # checkpoint is rooted before scratch can be retired or pruned.
                checkpoint("aligned", candidate_pins=candidates)
                pins(candidates)
                checkpoint("services_prepared", service_candidates=candidates)
        pins(intent["service_candidates"])
        clean_candidate_scratch(ctx, intent, check)
        # CAS updates retain the traditional local service refs and keep imported
        # candidate commits reachable across process restart and Git maintenance.
        if "service_local_old" not in intent:
            local_old = {}
            for name in state.RESET_SERVICE_BRANCHES:
                observed = command(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo, env=env)
                local_old[name] = observed.stdout.strip() if observed.returncode == 0 else None
            checkpoint(intent["phase"], service_local_old=local_old)
        for name in state.RESET_SERVICE_BRANCHES:
            verify_local()
            ref = f"refs/heads/{name}"
            symbolic = command(["git", "symbolic-ref", "-q", ref], cwd=repo, env=env)
            if symbolic.returncode == 0:
                raise RuntimeError("Local service reference is symbolic; refusing Reset.")
            observed = command(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, env=env)
            tip = observed.stdout.strip() if observed.returncode == 0 else None
            old, candidate = intent["service_local_old"][name], intent["service_candidates"][name]
            if tip not in {old, candidate}:
                raise RuntimeError("Local service branch changed during Reset; refusing overwrite.")
            if tip != candidate:
                git(["update-ref", "--no-deref", ref, candidate, old or "0" * len(candidate)])
        clean()
        for name in state.RESET_SERVICE_BRANCHES:
            verify_local()
            observed = verify_remote()
            verify_local()
            checkpoint("publishing")
            candidate = intent["service_candidates"][name]
            if observed.get(name) != candidate:
                old = intent["service_old"][name] or ""
                git(["push", f"--force-with-lease=refs/heads/{name}:{old}", "origin", f"{candidate}:refs/heads/{name}"])
            verify_remote()
        observed = verify_remote()
        if any(observed.get(name) != intent["service_candidates"][name] for name in state.RESET_SERVICE_BRANCHES):
            raise RuntimeError("Service branch publication is incomplete; explicitly retry Reset.")
        git(["checkout", branch])
        verify_local()
        verify_remote()
        verify_local()
        clean_candidate_scratch(ctx, intent, check)
        # No further blocking checkout/build/push follows the final remote check.
        verify_remote()
        checkpoint("complete")
        publish({**state.ALL_PREVIEW_CLEAR_UPDATES, "last_status": "success", "last_action": "reset_git_state",
                 "last_message": "Git state reset finished successfully.", "last_details": list(details),
                 "post_apply_save_recommended": False})
        outcome = ResetOutcome(True, "Git state reset finished successfully.", operation, generation,
            candidate_cleanup={"flow_id": intent["flow_id"], "repo": intent["repo"], "identity": intent["identity"], "candidates": intent["service_candidates"]})
    except Exception as exc:
        message = str(exc)
        no_effect = False
        if not journal_started:
            try:
                # No preservation/alignment/publication command can precede the
                # first acknowledged checkpoint. Read it under the same owner
                # fence to distinguish pre-write failure from a lost write ACK.
                durable = publish({})
                no_effect = durable.get("reset_intent") is None
            except Exception:
                pass
        try:
            publish({"last_status": "error", "last_action": "reset_git_state", "last_message": message,
                     "last_details": [*details, message]})
        except OwnershipChanged:
            pass
        outcome = ResetOutcome(False, message, operation, generation, no_effect=no_effect)
    finally:
        jobs.release_run_lock(ctx)
    if direct:
        ctx.update_command(operation["command_id"], "terminal", outcome_result(outcome))
        try:
            if outcome.ok:
                retire_completed_candidate_pins(ctx)
        except Exception:
            # The terminal record retains the exact cleanup manifest for a later
            # explicit successful Reset. No unknown ref is ever deleted.
            pass
    return outcome


def outcome_result(outcome):
    return {"ok": outcome.ok, "status": "success" if outcome.ok else "error", "message": outcome.message,
            "safe_terminal": outcome.no_effect, "reset_candidate_cleanup": outcome.candidate_cleanup,
            "reset_terminal_binding": {"operation": outcome.operation, "generation": outcome.generation}}
