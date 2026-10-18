"""Dormant, explicit Core/App lifecycle with injected, bounded adapters.

There is deliberately no live adapter, startup hook, thread, or Apply integration.
Adapter evidence is an injected capability, not a Supervisor receipt API.
"""
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
from contextlib import contextmanager


class InvalidState(ValueError):
    pass


class Conflict(RuntimeError):
    pass


class Unavailable(RuntimeError):
    """Fixed diagnostics only; adapter exception text is never propagated."""


_RANK = {"initialize": 0, "system": 1, "services": 2, "core": 3,
         "application": 4, "once": 4}
_ERRORS = {None, "command_rejected", "adapter_unavailable", "invalid_response",
           "timeout", "observation_mismatch", "dependency_blocked", "unhealthy",
           "health_unknown", "ownership_unknown", "ownership_changed"}
_PHASES = {"prepared", "stopping", "stopped", "restoring", "restored",
           "recovery_required", "unhealthy", "uncertain"}


def _need(condition):
    if not condition:
        raise InvalidState("invalid native service state")


def _fields(value, fields):
    _need(type(value) is dict and set(value) == set(fields.split()))


def _uuid(value):
    try:
        _need(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise InvalidState("invalid native service identity") from None


def _target(value):
    _need(type(value) is str and len(value) <= 255 and re.fullmatch(r"(?:core|app:[A-Za-z0-9_-]+)", value) is not None)


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        _need(key not in out)
        out[key] = value
    return out


def _observation(value, target):
    _fields(value, "target state")
    _need(value["target"] == target and value["state"] in ("running", "stopped", "unknown"))
    return copy.deepcopy(value)


def _binding(value, state, target, kind):
    _fields(value, "operation owner attempt target kind proof before after result")
    for key in ("operation", "owner", "attempt"):
        _uuid(value[key])
    _need(value["operation"] == state["operation"] and value["owner"] == state["owner"]
          and value["target"] == target and value["kind"] == kind
          and value["proof"] in {"acknowledged", "injected_evidence"})
    _observation(value["before"], target); _observation(value["after"], target)
    _need(value["before"]["state"] == ("running" if kind == "stop" else "stopped"))
    _need(value["after"]["state"] == ("stopped" if kind == "stop" else "running"))
    _need(value["result"] == ("accepted" if value["proof"] == "acknowledged" else "completed"))


def _selection(entry, self_target):
    if entry["id"] == self_target:
        return "self"
    if entry["original"] == "stopped":
        return "originally_stopped"
    if not entry["manageable"]:
        return "capability_excluded"
    return None


def _durable_directory(path):
    """Qualify every namespace link, including one left by interrupted mkdir."""
    if path.parent != path:
        _durable_directory(path.parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if path.parent != path:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            primary = sys.exc_info()[0]
            try:
                os.close(parent)
            except OSError:
                if primary is None:
                    raise


def _validate(state):
    """Validate the authority graph, not merely individual field types."""
    _fields(state, "schema operation owner revision self phase entries order pending")
    _need(type(state["schema"]) is int and state["schema"] == 1)
    _uuid(state["operation"]); _uuid(state["owner"])
    _need(type(state["revision"]) is int and state["revision"] >= 0)
    _target(state["self"]); _need(state["self"].startswith("app:"))
    _need(state["phase"] in _PHASES and type(state["entries"]) is list)
    ids, selected, attempts = [], [], set()
    for entry in state["entries"]:
        _fields(entry, "id startup original manageable capability exclusion selected stop start observation health error uncertain stop_invalidated stop_disposition")
        _target(entry["id"]); ids.append(entry["id"])
        _need(entry["startup"] in _RANK and (entry["id"] == "core") == (entry["startup"] == "core"))
        _need(entry["original"] in {"running", "stopped"} and type(entry["manageable"]) is bool)
        _need(entry["capability"] in {"manageable", "cannot_manage"})
        _need(entry["manageable"] == (entry["capability"] == "manageable"))
        _need(entry["exclusion"] == _selection(entry, state["self"]))
        _need(type(entry["selected"]) is bool and entry["selected"] == (entry["exclusion"] is None))
        if entry["selected"]:
            selected.append(entry)
        _need(entry["health"] in {"unchecked", "healthy", "unhealthy", "unknown"} and entry["error"] in _ERRORS)
        if entry["observation"] is not None:
            _observation(entry["observation"], entry["id"])
        for kind in ("stop", "start"):
            receipt = entry[kind]
            if receipt is not None:
                _binding(receipt, state, entry["id"], kind)
                _need(entry["selected"] and receipt["attempt"] not in attempts)
                attempts.add(receipt["attempt"])
        _need(entry["start"] is None or entry["stop"] is not None)
        _need(type(entry["stop_invalidated"]) is bool)
        # This independently persisted obligation survives observation/diagnostic
        # replacement. A missing receipt must never turn prior ownership into
        # an untouched service, even when its latest process state is unknown.
        disposition = entry["stop_disposition"]
        _need(disposition in {"untouched", "stop_pending", "owned", "start_pending", "started", "invalidated"})
        _need((entry["stop"] is not None) == (disposition in {"owned", "start_pending", "started", "invalidated"}))
        _need((entry["start"] is not None) == (disposition == "started"))
        _need(entry["stop_invalidated"] == (disposition == "invalidated"))
        _need(entry["stop_invalidated"] == (entry["error"] == "ownership_changed"))
        _need(not entry["stop_invalidated"] or entry["stop"] is not None and entry["start"] is None and
              entry["error"] == "ownership_changed" and entry["uncertain"] is None)
        _need(entry["health"] == "unchecked" or entry["start"] is not None)
        _need(entry["error"] not in {"unhealthy", "health_unknown"} or entry["start"] is not None)
        if entry["health"] == "healthy":
            _need(entry["error"] is None and entry["observation"] == {"target": entry["id"], "state": "running"})
        if not entry["selected"]:
            _need(entry["observation"] is None and entry["health"] == "unchecked" and entry["error"] is None)
        if entry["start"] is not None:
            _need(entry["observation"] is not None)
    _need(len(ids) == len(set(ids)) and "core" in ids and state["self"] in ids)
    core = next(e for e in state["entries"] if e["id"] == "core")
    _need(core["manageable"])
    order = [e["id"] for e in sorted(selected, key=lambda e: (-_RANK[e["startup"]], e["id"]))]
    _need(state["order"] == order)
    pending = state["pending"]
    debts = [(pending, None)] if pending is not None else []
    debts += [(entry["uncertain"], entry) for entry in state["entries"] if entry["uncertain"] is not None]
    debt_targets = set()
    debt_kinds = {}
    for pending, debt_entry in debts:
        _fields(pending, "operation owner attempt target kind before dispatch result")
        for key in ("operation", "owner", "attempt"):
            _uuid(pending[key])
        _need(pending["operation"] == state["operation"] and pending["owner"] == state["owner"])
        _need(pending["kind"] in {"stop", "start"} and pending["dispatch"] in {"intent", "entered"})
        _need(pending["attempt"] not in attempts and pending["target"] in order)
        _need(pending["target"] not in debt_targets)
        debt_targets.add(pending["target"]); attempts.add(pending["attempt"])
        _observation(pending["before"], pending["target"])
        _need(pending["before"]["state"] == ("running" if pending["kind"] == "stop" else "stopped"))
        entry = next(e for e in selected if e["id"] == pending["target"])
        _need(not entry["stop_invalidated"])
        _need(entry["stop_disposition"] == ("stop_pending" if pending["kind"] == "stop" else "start_pending"))
        debt_kinds[entry["id"]] = pending["kind"]
        _need(debt_entry is None or debt_entry is entry)
        _need(entry[pending["kind"]] is None)
        _need(pending["kind"] != "start" or entry["stop"] is not None)
        _need(pending["result"] in {None, "accepted"})
        _need(pending["result"] is None or pending["dispatch"] == "entered")
        _need(state["phase"] in {"stopping", "restoring", "recovery_required"})
    pending = state["pending"]
    for entry in state["entries"]:
        expected_debt = {"stop_pending": "stop", "start_pending": "start"}.get(entry["stop_disposition"])
        _need(debt_kinds.get(entry["id"]) == expected_debt)
        # An observed stop without any ownership, unresolved command or failure
        # diagnostic is not an empty lifecycle: its recovery graph is corrupt.
        if entry["stop"] is None and entry["observation"] == {"target": entry["id"], "state": "stopped"}:
            _need(entry["id"] in debt_targets or entry["error"] is not None)
    if state["phase"] == "prepared":
        _need(not debts and all(e["stop"] is None and e["start"] is None and e["observation"] is None and
              e["error"] is None for e in selected))
    if state["phase"] == "stopped":
        _need(not debts and all(e["stop"] is not None and e["start"] is None and
              e["observation"] == {"target": e["id"], "state": "stopped"} and e["error"] is None and not e["stop_invalidated"] for e in selected))
    if state["phase"] == "restored":
        _need(not debts and all(e["start"] is not None and e["health"] == "healthy" and
              e["observation"] == {"target": e["id"], "state": "running"} and e["error"] is None
              for e in selected if e["stop"] is not None))
    if state["phase"] in {"unhealthy", "uncertain"}:
        _need(not debts and any(e["stop"] is not None for e in selected))
    return state


def validate(state):
    try:
        return _validate(state)
    except (TypeError, KeyError, AttributeError):
        raise InvalidState("invalid native service state") from None


class _Budget:
    def __init__(self, seconds, polls, clock, sleep):
        _need(type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0)
        _need(type(polls) is int and polls > 0)
        self.clock, self.sleep, self.polls = clock, sleep, polls
        self.deadline = clock() + seconds

    def remaining(self):
        left = self.deadline - self.clock()
        if not math.isfinite(left) or left <= 0:
            raise Unavailable("timeout")
        return left

    def call(self, function, *args):
        timeout = self.remaining()
        try:
            value = function(*args, timeout=timeout)
        except Exception:
            raise Unavailable("adapter_unavailable") from None
        self.remaining()
        return value


class ServiceStore:
    """One journal; discovery dispatches no commands; every worker is explicit.

    The directory must be private. Use /data/native-services in a future caller.
    Calls hold a stable nonblocking flock through commands and probes. Adapters
    must honor finite timeout values; arbitrary Python callbacks cannot be killed.
    """
    def __init__(self, directory, *, clock=time.monotonic, sleep=time.sleep):
        self.directory = Path(os.path.abspath(directory))
        self.clock, self.sleep = clock, sleep
        _durable_directory(self.directory)
        st = self.directory.lstat()
        _need(stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700)
        self._identity = (st.st_dev, st.st_ino)

    @contextmanager
    def _lock(self):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            ds = os.fstat(directory)
            _need((ds.st_dev, ds.st_ino) == self._identity and stat.S_IMODE(ds.st_mode) == 0o700)
            # The directory inode is a second stable fence if the lock name is
            # accidentally replaced by another process running as the same UID.
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("service worker busy") from None
            lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            ls = os.fstat(lock)
            _need(stat.S_ISREG(ls.st_mode) and ls.st_nlink == 1 and ls.st_uid == os.geteuid() and stat.S_IMODE(ls.st_mode) == 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("service worker busy") from None
            current = os.stat("lock", dir_fd=directory, follow_symlinks=False)
            _need((current.st_dev, current.st_ino) == (ls.st_dev, ls.st_ino))
            self._lease = (directory, ls.st_dev, ls.st_ino)
            yield directory
        finally:
            primary = sys.exc_info()[0]
            close_error = None
            try:
                if lock is not None:
                    try:
                        os.close(lock)
                    except OSError as exc:
                        close_error = exc
            finally:
                try:
                    os.close(directory)
                except OSError as exc:
                    if close_error is None:
                        close_error = exc
            if primary is None and close_error is not None:
                raise close_error

    def _load(self, directory):
        self._check_paths(directory)
        try:
            fd = os.open("state.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            self._pointer = None
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            st = os.fstat(stream.fileno())
            _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o600)
            try:
                value = json.load(stream, object_pairs_hook=_pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(InvalidState("invalid JSON")))
            except (ValueError, UnicodeError):
                raise InvalidState("invalid native service JSON") from None
        self._pointer = (st.st_dev, st.st_ino)
        return validate(value)

    def _check_paths(self, directory):
        current = self.directory.lstat()
        _need(stat.S_ISDIR(current.st_mode) and current.st_uid == os.geteuid() and
              stat.S_IMODE(current.st_mode) == 0o700 and (current.st_dev, current.st_ino) == self._identity)
        lock = os.stat("lock", dir_fd=directory, follow_symlinks=False)
        _need(stat.S_ISREG(lock.st_mode) and lock.st_nlink == 1 and lock.st_uid == os.geteuid() and
              stat.S_IMODE(lock.st_mode) == 0o600 and (directory, lock.st_dev, lock.st_ino) == self._lease)

    def _save(self, directory, state):
        self._check_paths(directory)
        try:
            current = os.stat("state.json", dir_fd=directory, follow_symlinks=False)
            _need(stat.S_ISREG(current.st_mode) and current.st_nlink == 1 and current.st_uid == os.geteuid() and
                  stat.S_IMODE(current.st_mode) == 0o600 and (current.st_dev, current.st_ino) == self._pointer)
        except FileNotFoundError:
            _need(self._pointer is None)
        state["revision"] += 1
        validate(state)
        name = "state-" + str(uuid.uuid4()) + ".tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, "state.json", src_dir_fd=directory, dst_dir_fd=directory)
            current = os.stat("state.json", dir_fd=directory, follow_symlinks=False)
            self._pointer = (current.st_dev, current.st_ino)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def discover(self):
        with self._lock() as directory:
            return copy.deepcopy(self._load(directory))

    def _fenced(self, directory, operation, owner, revision):
        state = self._load(directory)
        if state is None or type(revision) is not int or (state["operation"], state["owner"], state["revision"]) != (operation, owner, revision):
            raise Conflict("stale service authority")
        return state

    def prepare(self, adapter, *, seconds=60, polls=10, replace=None):
        budget = _Budget(seconds, polls, self.clock, self.sleep)
        with self._lock() as directory:
            old = self._load(directory)
            if old is not None:
                if old["phase"] != "restored" or replace != (old["operation"], old["owner"], old["revision"]):
                    raise Conflict("unresolved service lifecycle")
            inventory = budget.call(adapter.inventory)
            _fields(inventory, "complete self entries")
            _need(inventory["complete"] is True and type(inventory["entries"]) is list)
            entries = []
            for item in inventory["entries"]:
                _fields(item, "id startup state manageable capability")
                _target(item["id"])
                _need(type(item["startup"]) is str and item["startup"] in _RANK)
                _need(item["state"] in ("running", "stopped", "unknown"))
                _need(type(item["manageable"]) is bool and item["capability"] in ("manageable", "cannot_manage"))
                original = item["state"]
                if original == "unknown":
                    for _ in range(polls):
                        observation = _observation(budget.call(adapter.observe, item["id"]), item["id"])
                        original = observation["state"]
                        if original != "unknown":
                            break
                entry = dict(id=item["id"], startup=item["startup"], original=original,
                             manageable=item["manageable"], capability=item["capability"],
                             stop=None, start=None, observation=None, health="unchecked", error=None,
                             uncertain=None, stop_invalidated=False, stop_disposition="untouched")
                entry["exclusion"] = _selection(entry, inventory["self"])
                entry["selected"] = entry["exclusion"] is None
                entries.append(entry)
            state = dict(schema=1, operation=str(uuid.uuid4()), owner=str(uuid.uuid4()), revision=0,
                         self=inventory["self"], phase="prepared", entries=entries, pending=None,
                         order=[e["id"] for e in sorted(entries, key=lambda e: (-_RANK.get(e["startup"], -1), e["id"])) if e["selected"]])
            self._save(directory, state)
            return copy.deepcopy(state)

    def _observe(self, adapter, budget, entry):
        return _observation(budget.call(adapter.observe, entry["id"]), entry["id"])

    def _ack(self, state, entry, proof):
        pending = state["pending"]
        entry[pending["kind"]] = {key: pending[key] for key in ("operation", "owner", "attempt", "target", "kind")}
        entry[pending["kind"]]["proof"] = proof
        entry[pending["kind"]]["before"] = copy.deepcopy(pending["before"])
        entry[pending["kind"]]["after"] = copy.deepcopy(entry["observation"])
        entry[pending["kind"]]["result"] = "accepted" if proof == "acknowledged" else "completed"
        entry["stop_disposition"] = "owned" if pending["kind"] == "stop" else "started"
        entry["error"] = None
        state["pending"] = None

    def _command(self, directory, state, entry, kind, adapter, budget):
        before = self._observe(adapter, budget, entry)
        entry["observation"] = before
        expected = "running" if kind == "stop" else "stopped"
        if before["state"] != expected:
            entry["error"] = "observation_mismatch"
            if kind == "start" and before["state"] == "running":
                entry["stop_invalidated"] = True
                entry["stop_disposition"] = "invalidated"
                entry["error"] = "ownership_changed"
            self._save(directory, state)
            return False
        pending = dict(operation=state["operation"], owner=state["owner"], attempt=str(uuid.uuid4()),
                       target=entry["id"], kind=kind, before=before, dispatch="intent", result=None)
        state["pending"] = pending
        entry["stop_disposition"] = "stop_pending" if kind == "stop" else "start_pending"
        self._save(directory, state)
        budget.remaining()
        pending["dispatch"] = "entered"
        self._save(directory, state)
        self._check_paths(directory)
        result = budget.call(adapter.command, copy.deepcopy(pending))
        _fields(result, "operation owner attempt target kind outcome")
        _need(all(result[key] == pending[key] for key in ("operation", "owner", "attempt", "target", "kind")))
        _need(result["outcome"] in ("accepted", "rejected"))
        if result["outcome"] == "rejected":
            entry["error"] = "command_rejected"
            state["pending"] = None
            entry["stop_disposition"] = "untouched" if kind == "stop" else "owned"
            self._save(directory, state)
            return False
        pending["result"] = "accepted"
        self._save(directory, state)
        after = self._observe(adapter, budget, entry)
        entry["observation"] = after
        self._save(directory, state)
        if after["state"] != ("stopped" if kind == "stop" else "running"):
            entry["error"] = "observation_mismatch"
            entry["uncertain"], state["pending"] = pending, None
            self._save(directory, state)
            return False
        self._ack(state, entry, "acknowledged")
        self._save(directory, state)
        return True

    def _health(self, directory, state, entry, adapter, budget):
        entry["health"] = "unknown"
        entry["error"] = "health_unknown"
        for _ in range(budget.polls):
            entry["observation"] = self._observe(adapter, budget, entry)
            value = budget.call(adapter.readiness, entry["id"])
            _fields(value, "target status recovery_mode")
            _need(value["target"] == entry["id"] and value["status"] in ("healthy", "unhealthy", "unknown"))
            _need(value["recovery_mode"] is None or type(value["recovery_mode"]) is bool)
            health = value["status"]
            if entry["observation"]["state"] != "running":
                health = "unknown"
            if entry["id"] == "core":
                if value["recovery_mode"] is True:
                    health = "unhealthy"
                elif value["recovery_mode"] is None:
                    health = "unknown"
            entry["health"] = health
            entry["error"] = {"healthy": None, "unhealthy": "unhealthy", "unknown": "health_unknown"}[health]
            self._save(directory, state)
            if health != "unknown":
                break
            budget.sleep(min(0.05, budget.remaining()))
        return entry["health"] == "healthy"

    def _failure(self, directory, state, entry, exc):
        code = str(exc) if isinstance(exc, Unavailable) and str(exc) in _ERRORS else "invalid_response"
        entry["error"] = code
        if state["pending"] is not None:
            entry["uncertain"] = state["pending"]
            state["pending"] = None
        state["phase"] = "recovery_required"
        self._save(directory, state)

    def stop(self, operation, owner, revision, adapter, *, seconds=60, polls=10):
        budget = _Budget(seconds, polls, self.clock, self.sleep)
        with self._lock() as directory:
            state = self._fenced(directory, operation, owner, revision)
            if state["pending"] is not None or any(e["start"] is not None or e["uncertain"] is not None for e in state["entries"]):
                raise Conflict("explicit reconciliation or restoration required")
            state["phase"] = "stopping"; self._save(directory, state)
            failed_rank = None
            for target in state["order"]:
                entry = next(e for e in state["entries"] if e["id"] == target)
                if entry["stop"] is not None:
                    if entry["stop_invalidated"]:
                        failed_rank = _RANK[entry["startup"]]
                        continue
                    try:
                        entry["observation"] = self._observe(adapter, budget, entry)
                        if entry["observation"]["state"] != "stopped":
                            entry["error"] = "observation_mismatch"
                            if entry["observation"]["state"] == "running":
                                entry["stop_invalidated"] = True
                                entry["stop_disposition"] = "invalidated"
                                entry["error"] = "ownership_changed"
                            failed_rank = _RANK[entry["startup"]]
                        else:
                            entry["error"] = None
                    except (Unavailable, InvalidState) as exc:
                        self._failure(directory, state, entry, exc)
                        failed_rank = _RANK[entry["startup"]]
                    continue
                rank = _RANK[entry["startup"]]
                if failed_rank is not None and rank < failed_rank:
                    entry["error"] = "dependency_blocked"
                    continue
                try:
                    good = self._command(directory, state, entry, "stop", adapter, budget)
                except (Unavailable, InvalidState) as exc:
                    self._failure(directory, state, entry, exc); good = False
                if not good:
                    failed_rank = rank
                if state["pending"] is not None:
                    break
            state["phase"] = "stopped" if all(e["stop"] is not None and e["uncertain"] is None and e["error"] is None and e["observation"]["state"] == "stopped" for e in state["entries"] if e["selected"]) and state["pending"] is None else "recovery_required"
            self._save(directory, state)
            return copy.deepcopy(state)

    def restore(self, operation, owner, revision, adapter, *, seconds=60, polls=10):
        budget = _Budget(seconds, polls, self.clock, self.sleep)
        with self._lock() as directory:
            state = self._fenced(directory, operation, owner, revision)
            if state["pending"] is not None:
                entry = next(e for e in state["entries"] if e["id"] == state["pending"]["target"])
                entry["uncertain"], state["pending"] = state["pending"], None
            if state["phase"] == "restored":
                return copy.deepcopy(state)
            state["phase"] = "restoring"; self._save(directory, state)
            failed_rank = None
            owned = sorted((e for e in state["entries"] if e["stop"] is not None or e["uncertain"] is not None), key=lambda e: (_RANK[e["startup"]], e["id"]))
            for entry in owned:
                rank = _RANK[entry["startup"]]
                if entry["stop_invalidated"]:
                    failed_rank = rank if failed_rank is None else min(rank, failed_rank)
                    continue
                if entry["uncertain"] is not None:
                    failed_rank = rank if failed_rank is None else min(rank, failed_rank)
                    continue
                if failed_rank is not None and rank > failed_rank:
                    entry["error"] = "dependency_blocked"
                    if entry["start"] is not None:
                        entry["health"] = "unknown"
                    continue
                try:
                    good = entry["start"] is not None or self._command(directory, state, entry, "start", adapter, budget)
                    if good:
                        good = self._health(directory, state, entry, adapter, budget)
                except (Unavailable, InvalidState) as exc:
                    self._failure(directory, state, entry, exc); good = False
                if not good:
                    failed_rank = rank
                if state["pending"] is not None:
                    break
            if state["pending"] is not None or any(e["start"] is None or e["uncertain"] is not None for e in owned):
                state["phase"] = "recovery_required"
            elif any(e["health"] == "unhealthy" for e in owned):
                state["phase"] = "unhealthy"
            elif any(e["health"] != "healthy" for e in owned):
                state["phase"] = "uncertain"
            else:
                state["phase"] = "restored"
            self._save(directory, state)
            return copy.deepcopy(state)

    def reconcile(self, operation, owner, revision, adapter, *, seconds=60, polls=10):
        """Explicit evidence reconciliation; never resends an ambiguous command."""
        budget = _Budget(seconds, polls, self.clock, self.sleep)
        with self._lock() as directory:
            state = self._fenced(directory, operation, owner, revision)
            pending = state["pending"] or next((e["uncertain"] for e in state["entries"] if e["uncertain"] is not None), None)
            if pending is None:
                return copy.deepcopy(state)
            entry = next(e for e in state["entries"] if e["id"] == pending["target"])
            entry["uncertain"] = None
            state["pending"] = pending
            try:
                observation = self._observe(adapter, budget, entry)
                entry["observation"] = observation
                if pending["result"] == "accepted" and observation["state"] == ("stopped" if pending["kind"] == "stop" else "running"):
                    self._ack(state, entry, "acknowledged")
                    state["phase"] = "recovery_required"
                    self._save(directory, state)
                    return copy.deepcopy(state)
                evidence = budget.call(adapter.evidence, copy.deepcopy(pending))
                _fields(evidence, "operation owner attempt target kind outcome")
                _need(all(evidence[key] == pending[key] for key in ("operation", "owner", "attempt", "target", "kind")))
                _need(evidence["outcome"] in ("completed", "not_sent", "unknown"))
                expected = "stopped" if pending["kind"] == "stop" else "running"
                if evidence["outcome"] == "completed" and observation["state"] == expected:
                    self._ack(state, entry, "injected_evidence")
                elif evidence["outcome"] == "not_sent" and observation == pending["before"]:
                    state["pending"] = None
                    entry["stop_disposition"] = "untouched" if pending["kind"] == "stop" else "owned"
                    entry["error"] = None
                else:
                    entry["error"] = "ownership_unknown"
                    entry["uncertain"], state["pending"] = pending, None
            except (Unavailable, InvalidState) as exc:
                self._failure(directory, state, entry, exc)
            state["phase"] = "recovery_required"
            self._save(directory, state)
            return copy.deepcopy(state)
