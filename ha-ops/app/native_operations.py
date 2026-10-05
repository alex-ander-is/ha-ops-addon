"""Inert durable native workflow primitives; deliberately unused by legacy dispatch.

Callers must supply fresh, complete adapter observations. This module performs
no Git, Supervisor, inventory, or live filesystem operations.
"""
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
MAX_STATE = 4 * 1024 * 1024
MAX_ARTIFACT = 8 * 1024 * 1024
DOMAINS = {"migration", "scope", "save", "apply"}
STATUSES = {"preview", "reviewed", "consumed", "stale", "cancelled", "completed", "recovery_required"}
PHASES = {"accepted", "effect_started", "completed", "failed", "invalidated", "recovery_required"}
WORKER_TRANSITIONS = {"accepted": {"effect_started", "failed", "invalidated"},
                      "effect_started": {"completed", "recovery_required"},
                      "recovery_required": {"completed", "recovery_required"}}
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class InvalidState(ValueError):
    """Unsupported/corrupt data is retained, never replaced with defaults."""


class Conflict(RuntimeError):
    """Revision, authority, or worker ownership no longer matches."""


def _require(condition, message):
    if not condition:
        raise InvalidState(message)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise InvalidState("noncanonical JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _hash(value):
    _require(isinstance(value, str) and DIGEST.fullmatch(value), "invalid digest")


def _id(value):
    try:
        _require(isinstance(value, str) and str(uuid.UUID(value)) == value, "invalid ID")
    except (ValueError, AttributeError) as exc:
        raise InvalidState("invalid ID") from exc


def _int(value):
    _require(type(value) is int and value >= 0, "invalid integer")


def _fields(value, names):
    _require(isinstance(value, dict) and set(value) == set(names.split()), "invalid fields")


def _text(value):
    _require(isinstance(value, str) and 0 < len(value) <= 1024 and "\x00" not in value, "invalid text")


def _path(value, absolute=False):
    _text(value)
    _require(value.startswith("/") == absolute and "\\" not in value,
             "invalid path")
    if absolute and value == "/":
        return
    parts = value[1:].split("/") if absolute else value.split("/")
    _require(all(part and part not in {".", ".."} for part in parts), "invalid path")


def _timestamp(value):
    _text(value)
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None, "timestamp needs timezone")
    except ValueError as exc:
        raise InvalidState("invalid timestamp") from exc


def _scope(value):
    _fields(value, "revision allowlist exclude_digest")
    _int(value["revision"])
    _hash(value["exclude_digest"])
    _require(isinstance(value["allowlist"], list), "invalid allowlist")
    for path in value["allowlist"]:
        _path(path)
    _require(value["allowlist"] == sorted(set(value["allowlist"])), "unsorted allowlist")


def _root(value):
    _fields(value, "path device inode mount_digest topology_digest")
    _path(value["path"], absolute=True)
    _int(value["device"])
    _int(value["inode"])
    _hash(value["mount_digest"])
    _hash(value["topology_digest"])


def _refs(value):
    _fields(value, "base local remote")
    for ref in value.values():
        _fields(ref, "name oid")
        _text(ref["name"])
        name = ref["name"]
        # git-check-ref-format rules, without invoking Git from the state API.
        _require(name.startswith("refs/") and not name.endswith(("/", ".")) and
                 not any(char in name for char in " ~^:?*[\\") and
                 not any(ord(char) < 32 or ord(char) == 127 for char in name) and
                 ".." not in name and "@{" not in name and
                 all(part and not part.startswith(".") and not part.endswith(".lock")
                     for part in name.split("/")), "invalid ref")
        _require(isinstance(ref["oid"], str) and OID.fullmatch(ref["oid"]), "invalid OID")


def validate_identity(value, domain):
    keys = "direction execution_policy scope refs root paths effect"
    if domain == "migration":
        keys += " migration"
    if domain == "scope":
        keys += " scope_transaction"
    _fields(value, keys)
    _require(domain in DOMAINS and value["direction"] == domain, "wrong domain")
    _text(value["execution_policy"])
    _scope(value["scope"])
    _refs(value["refs"])
    _root(value["root"])
    _fields(value["effect"], "before after")
    for effect in value["effect"].values():
        _hash(effect)
    _require(isinstance(value["paths"], list), "invalid paths")
    paths = []
    for item in value["paths"]:
        _fields(item, "path type mode content_digest metadata_digest")
        _path(item["path"])
        paths.append(item["path"])
        _require(item["type"] in {"file", "directory", "symlink", "missing"}, "invalid type")
        if item["type"] == "missing":
            _require(all(item[k] is None for k in ("mode", "content_digest", "metadata_digest")), "invalid absence")
        else:
            _int(item["mode"])
            _require(item["mode"] <= 0o177777, "invalid mode")
            expected_type = {"file": stat.S_IFREG, "directory": stat.S_IFDIR, "symlink": stat.S_IFLNK}[item["type"]]
            _require(stat.S_IFMT(item["mode"]) == expected_type, "mode/type mismatch")
            _hash(item["metadata_digest"])
            if item["type"] == "directory":
                _require(item["content_digest"] is None, "directory content")
            else:
                _hash(item["content_digest"])
    _require(paths == sorted(set(paths)), "unsorted paths")
    if domain == "migration":
        extra = value["migration"]
        _fields(extra, "original_checkout candidate_commit post_refs")
        _root(extra["original_checkout"])
        _require(isinstance(extra["candidate_commit"], str) and OID.fullmatch(extra["candidate_commit"]), "invalid commit")
        _refs(extra["post_refs"])
    if domain == "scope":
        extra = value["scope_transaction"]
        _fields(extra, "old new")
        _scope(extra["old"])
        _scope(extra["new"])
        _require(extra["old"] == value["scope"] and
                 extra["new"]["revision"] == extra["old"]["revision"] + 1, "invalid scope transition")


def _decisions(value, identity):
    _require(isinstance(value, dict), "invalid decisions")
    allowed = {item["path"] for item in identity["paths"]}
    _require(set(value) <= allowed and all(choice in {"ha", "git"} for choice in value.values()), "invalid decisions")
    if identity["direction"] in {"save", "apply"}:
        _require(all(any(path == root or path.startswith(root + "/")
                         for root in identity["scope"]["allowlist"]) for path in value),
                 "selection outside declared scope")


def _approved_decisions(value, identity):
    _decisions(value, identity)
    _require(bool(value) or identity["direction"] in {"migration", "scope"}, "empty selection")


def _binding(record, decisions):
    return digest({"identity": record["identity"], "artifact": record["artifact"], "decisions": decisions})


def _evidence(value):
    _fields(value, "before after marker")
    _hash(value["before"])
    _hash(value["after"])
    _id(value["marker"])


def _record(value, domain):
    _fields(value, "id domain identity artifact identity_digest status decisions approval created_at validated_at checkpoints")
    _id(value["id"])
    _require(value["domain"] == domain and value["status"] in STATUSES, "invalid record status")
    validate_identity(value["identity"], domain)
    _hash(value["artifact"])
    _require(value["identity_digest"] == digest(value["identity"]), "identity mismatch")
    _timestamp(value["created_at"])
    if value["validated_at"] is not None:
        _timestamp(value["validated_at"])
    if value["status"] in {"reviewed", "consumed", "completed", "recovery_required"}:
        _approved_decisions(value["decisions"], value["identity"])
        _fields(value["approval"], "id binding")
        _id(value["approval"]["id"])
        _require(value["approval"]["binding"] == _binding(value, value["decisions"]), "approval mismatch")
    else:
        _decisions(value["decisions"], value["identity"])
        _require(value["approval"] is None and not value["decisions"], "unexpected authority")
    _require(isinstance(value["checkpoints"], list), "invalid checkpoints")
    for checkpoint in value["checkpoints"]:
        _fields(checkpoint, "operation_id owner epoch phase evidence")
        _id(checkpoint["operation_id"])
        _id(checkpoint["owner"])
        _int(checkpoint["epoch"])
        _require(checkpoint["phase"] in PHASES, "invalid checkpoint")
        if checkpoint["evidence"] is not None:
            _evidence(checkpoint["evidence"])


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _parse(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_require(False, "nonfinite JSON")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidState("invalid native state JSON") from exc


def _now():
    return datetime.now(timezone.utc).isoformat()


class NativeOperationStore:
    """Private /data store. Opening/reading is observational, not a takeover."""

    def __init__(self, data_root, owner=None, fault=None):
        self.data_root = Path(data_root).absolute()
        self.owner = owner or str(uuid.uuid4())
        _id(self.owner)
        self.fault = fault or (lambda stage: None)
        with _LOCKS_GUARD:
            self.mutex = _LOCKS.setdefault(str(self.data_root), threading.RLock())

    @contextmanager
    def _locked(self):
        with self.mutex:
            descriptors = []
            try:
                fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                descriptors.append(fd)
                for part in self.data_root.parts[1:]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(fd)
                for part in ("native-operations", "artifacts"):
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(fd)
                    _require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, "unsafe directory permissions")
                root, artifacts = descriptors[-2:]
                lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
                descriptors.append(lock)
                self._regular(lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield root, artifacts
            finally:
                for fd in reversed(descriptors):
                    os.close(fd)

    @staticmethod
    def _regular(fd):
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                 stat.S_IMODE(info.st_mode) == 0o600, "unsafe file")

    def _read_file(self, directory, name, limit):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._regular(fd)
            _require(os.fstat(fd).st_size <= limit, "file too large")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            _require(len(data) <= limit, "file too large")
            return bytes(data)
        finally:
            os.close(fd)

    def _artifact(self, directory, key):
        _hash(key)
        data = self._read_file(directory, key + ".diff", MAX_ARTIFACT)
        _require(hashlib.sha256(data).hexdigest() == key, "artifact mismatch")
        return data

    def _write(self, directory, name, data, artifact=False):
        temporary = ".tmp-" + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                self.fault("artifact_written" if artifact else "state_written")
                os.fsync(stream.fileno())
            self.fault("artifact_fsynced" if artifact else "state_fsynced")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            self.fault("artifact_replaced" if artifact else "state_replaced")
            os.fsync(directory)
            self.fault("artifact_durable" if artifact else "state_durable")
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _validate(self, state, artifacts):
        _fields(state, "schema revision instance_id updated_at app_version epoch records current workers active commands baseline_record_id baseline_scope active_scope")
        _require(type(state["schema"]) is int and state["schema"] == SCHEMA, "unsupported schema")
        _int(state["revision"])
        _int(state["epoch"])
        _id(state["instance_id"])
        _timestamp(state["updated_at"])
        _text(state["app_version"])
        _fields(state["current"], "migration scope save apply")
        for key in ("records", "workers", "commands"):
            _require(isinstance(state[key], dict), "invalid ledger")
        for key, record in state["records"].items():
            _id(key)
            _require(record["id"] == key, "record ID mismatch")
            _record(record, record["domain"])
            self._artifact(artifacts, record["artifact"])
        for domain, key in state["current"].items():
            _require(key is None or key in state["records"] and state["records"][key]["domain"] == domain, "invalid current record")
        for key, worker in state["workers"].items():
            _id(key)
            _fields(worker, "id record_id owner epoch phase marker evidence outcome binding")
            _require(worker["id"] == key and worker["record_id"] in state["records"], "invalid worker reference")
            _id(worker["owner"])
            _id(worker["marker"])
            _hash(worker["binding"])
            _int(worker["epoch"])
            _require(0 < worker["epoch"] <= state["epoch"] and worker["phase"] in PHASES, "invalid worker")
            _require(worker["outcome"] is None or worker["outcome"] in {"completed", "failed", "invalidated"}, "invalid outcome")
            if worker["evidence"] is not None:
                _evidence(worker["evidence"])
                _require(worker["evidence"]["marker"] == worker["marker"], "wrong effect marker")
            record = state["records"][worker["record_id"]]
            expected_status = {"accepted": "consumed", "effect_started": "consumed",
                               "recovery_required": "recovery_required", "completed": "completed",
                               "failed": "stale", "invalidated": "stale"}[worker["phase"]]
            allowed_statuses = {expected_status, "cancelled"} if worker["phase"] in {"failed", "invalidated"} else {expected_status}
            _require(record["status"] in allowed_statuses, "worker/record status mismatch")
            terminal = worker["phase"] in {"completed", "failed", "invalidated"}
            _require(worker["outcome"] == (worker["phase"] if terminal else None), "outcome mismatch")
            if worker["phase"] == "completed":
                _require(worker["evidence"] == dict(record["identity"]["effect"], marker=worker["marker"]),
                         "completion evidence mismatch")
            else:
                _require(worker["evidence"] is None, "unexpected completion evidence")
            if record["approval"] is not None:
                _require(worker["binding"] == record["approval"]["binding"], "worker approval mismatch")
        for record in state["records"].values():
            linked = [worker for worker in state["workers"].values() if worker["record_id"] == record["id"]]
            _require(len(linked) <= 1, "reused candidate consent")
            if record["status"] in {"consumed", "recovery_required", "completed"}:
                _require(len(linked) == 1, "missing candidate worker")
            for checkpoint in record["checkpoints"]:
                worker = state["workers"].get(checkpoint["operation_id"])
                _require(worker is not None and worker["record_id"] == record["id"] and
                         0 < checkpoint["epoch"] <= worker["epoch"], "checkpoint reference mismatch")
                if checkpoint["phase"] == "completed":
                    _require(checkpoint["evidence"] == worker["evidence"] and worker["phase"] == "completed",
                             "checkpoint evidence mismatch")
                else:
                    _require(checkpoint["evidence"] is None, "unexpected checkpoint evidence")
            if linked:
                self._validate_history(record["checkpoints"], linked[0])
            else:
                _require(not record["checkpoints"], "unclaimed candidate history")
        active = state["active"]
        _require(active is None or active in state["workers"] and state["workers"][active]["phase"] in
                 {"accepted", "effect_started", "recovery_required"}, "invalid active worker")
        unresolved = {key for key, worker in state["workers"].items() if worker["phase"] in
                      {"accepted", "effect_started", "recovery_required"}}
        _require(unresolved == ({active} if active else set()), "multiple unresolved workers")
        epochs = {}
        for record in state["records"].values():
            for checkpoint in record["checkpoints"]:
                epoch = checkpoint["epoch"]
                operation = checkpoint["operation_id"]
                _require(epoch not in epochs or epochs[epoch] == operation, "claim epoch reused by another worker")
                epochs[epoch] = operation
        _require(len(epochs) == state["epoch"] and (not epochs or min(epochs) == 1 and max(epochs) == state["epoch"]),
                 "incomplete claim epoch history")
        if active:
            _require(state["workers"][active]["epoch"] == state["epoch"], "active worker is not current owner epoch")
        for key, command in state["commands"].items():
            _id(key)
            _fields(command, "payload_digest operation_id")
            _hash(command["payload_digest"])
            _require(command["operation_id"] in state["workers"], "invalid command reference")
            worker = state["workers"][command["operation_id"]]
            _require(command["payload_digest"] == digest({"record_id": worker["record_id"], "binding": worker["binding"]}),
                     "command payload mismatch")
        _require(len(state["commands"]) == len(state["workers"]) and
                 len({command["operation_id"] for command in state["commands"].values()}) == len(state["workers"]),
                 "command/worker graph mismatch")
        self._validate_scope_history(state)

    @staticmethod
    def _validate_history(history, worker):
        # Current status cannot erase an intent retained in durable history.
        _require(bool(history) and history[0]["phase"] == "accepted", "missing acceptance checkpoint")
        for previous, current in zip(history, history[1:]):
            _require(current["phase"] in WORKER_TRANSITIONS.get(previous["phase"], set()),
                     "impossible checkpoint transition")
            if current["epoch"] == previous["epoch"]:
                _require(current["owner"] == previous["owner"], "owner changed without takeover")
                _require(not (previous["phase"] == current["phase"] == "recovery_required"),
                         "recovery takeover requires a new epoch")
            else:
                _require(current["epoch"] == previous["epoch"] + 1 and
                         (previous["phase"], current["phase"]) in {
                             ("accepted", "invalidated"), ("effect_started", "recovery_required"),
                             ("recovery_required", "recovery_required")}, "impossible takeover epoch")
        latest = history[-1]
        _require(all(latest[field] == worker[field] for field in ("phase", "epoch", "owner")),
                 "worker/history mismatch")

    @staticmethod
    def _validate_scope_history(state):
        records = list(state["records"].values())
        if not records:
            _require(all(state[key] is None for key in ("baseline_record_id", "baseline_scope", "active_scope")),
                     "scope without baseline provenance")
            return
        _scope(state["baseline_scope"])
        _scope(state["active_scope"])
        _id(state["baseline_record_id"])
        source = state["records"].get(state["baseline_record_id"])
        _require(source is not None and source["identity"]["scope"] == state["baseline_scope"],
                 "baseline lacks retained candidate provenance")
        expected = state["baseline_scope"]
        policies = {expected["revision"]: expected}
        # Exclusive contiguous epoch ranges establish actual operation order.
        # Policy revisions describe effects, and cannot establish that order.
        workers = sorted(state["workers"].values(), key=lambda worker:
                         state["records"][worker["record_id"]]["checkpoints"][0]["epoch"])
        for worker in workers:
            record = state["records"][worker["record_id"]]
            _require(record["identity"]["scope"] == expected,
                     "worker scope does not follow prior confirmed publication")
            if record["domain"] == "scope" and worker["phase"] == "completed":
                # The preceding validators require exact completion evidence.
                expected = record["identity"]["scope_transaction"]["new"]
                _require(expected["revision"] not in policies, "duplicate scope publication")
                policies[expected["revision"]] = expected
        for record in records:
            policy = record["identity"]["scope"]
            _require(policy == policies.get(policy["revision"]), "record lacks established policy provenance")
        _require(state["active_scope"] == expected, "active scope lacks confirmed publication")

    def _load(self, root, artifacts):
        try:
            state = _parse(self._read_file(root, "state.json", MAX_STATE))
        except FileNotFoundError:
            state = {"schema": SCHEMA, "revision": 0, "instance_id": str(uuid.uuid4()),
                     "updated_at": _now(), "app_version": "unactivated", "epoch": 0,
                     "records": {}, "current": dict.fromkeys(sorted(DOMAINS)), "workers": {},
                     "active": None, "commands": {}, "baseline_record_id": None,
                     "baseline_scope": None, "active_scope": None}
            self._write(root, "state.json", canonical(state))
        try:
            self._validate(state, artifacts)
        except (KeyError, TypeError, RecursionError) as exc:
            raise InvalidState("invalid native state graph") from exc
        return state

    def _commit(self, root, artifacts, state):
        state["revision"] += 1
        state["updated_at"] = _now()
        self._validate(state, artifacts)
        encoded = canonical(state)
        _require(len(encoded) <= MAX_STATE, "state too large")
        self._write(root, "state.json", encoded)

    @staticmethod
    def _cas(state, expected):
        _int(expected)
        if state["revision"] != expected:
            raise Conflict("native state revision changed")

    def read(self):
        with self._locked() as (root, artifacts):
            return self._load(root, artifacts)

    def refresh(self):
        """Diagnostic projection only: never initializes editable decisions."""
        state = self.read()
        return {"revision": state["revision"], "candidates": {
            domain: None if key is None else {field: copy.deepcopy(state["records"][key][field])
                                              for field in ("id", "status", "artifact", "identity_digest", "checkpoints")}
            for domain, key in state["current"].items()},
            "worker": None if state["active"] is None else {
                field: state["workers"][state["active"]][field] for field in ("id", "phase", "epoch", "outcome")},
            "draft": {}}

    def read_diff(self, record_id):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            return self._artifact(artifacts, state["records"][record_id]["artifact"])

    def preview(self, expected_revision, domain, identity, diff):
        validate_identity(identity, domain)
        _require(type(diff) is bytes and len(diff) <= MAX_ARTIFACT, "invalid artifact bytes")
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state["active"]:
                raise Conflict("native worker unresolved")
            if state["active_scope"] is not None and identity["scope"] != state["active_scope"]:
                raise Conflict("scope change requires completed publication")
            key = hashlib.sha256(diff).hexdigest()
            try:
                self._artifact(artifacts, key)
            except FileNotFoundError:
                self._write(artifacts, key + ".diff", diff, artifact=True)
            record_id = str(uuid.uuid4())
            record = {"id": record_id, "domain": domain, "identity": copy.deepcopy(identity),
                      "identity_digest": digest(identity), "artifact": key, "status": "preview",
                      "decisions": {}, "approval": None, "created_at": _now(),
                      "validated_at": None, "checkpoints": []}
            previous = state["current"][domain]
            if previous:
                self._invalidate(state["records"][previous])
            state["records"][record_id] = record
            state["current"][domain] = record_id
            if state["baseline_record_id"] is None:
                # First durable Preview in any domain anchors the supplied policy
                # atomically with its immutable source record. Later cancellation,
                # refresh or restart never opens bootstrap authority again.
                state["baseline_record_id"] = record_id
                state["baseline_scope"] = copy.deepcopy(identity["scope"])
                state["active_scope"] = copy.deepcopy(identity["scope"])
            self._commit(root, artifacts, state)
            return copy.deepcopy(record)

    @staticmethod
    def _invalidate(record):
        if record["status"] not in {"completed", "cancelled"}:
            record.update(status="stale", decisions={}, approval=None, validated_at=None)

    @staticmethod
    def _matches(record, observation):
        if not isinstance(observation, dict) or set(observation) != {"available", "identity"} or observation["available"] is not True:
            return False
        try:
            validate_identity(observation["identity"], record["domain"])
        except (InvalidState, TypeError):
            return False
        return observation["identity"] == record["identity"]

    @staticmethod
    def _scope_matches(state, record):
        return state["active_scope"] is not None and record["identity"]["scope"] == state["active_scope"]

    def review(self, expected_revision, record_id, decisions, approval_id, observation):
        _id(approval_id)
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            record = state["records"][record_id]
            if state["active"] or state["current"][record["domain"]] != record_id or record["status"] != "preview":
                raise Conflict("fresh Preview required")
            if not self._matches(record, observation) or not self._scope_matches(state, record):
                self._invalidate(record)
                self._commit(root, artifacts, state)
                raise Conflict("source changed; fresh Preview required")
            _approved_decisions(decisions, record["identity"])
            record.update(status="reviewed", decisions=copy.deepcopy(decisions), validated_at=_now())
            record["approval"] = {"id": approval_id, "binding": _binding(record, decisions)}
            self._commit(root, artifacts, state)
            return copy.deepcopy(record)

    def claim(self, expected_revision, command_id, record_id, approval_binding, observation):
        _id(command_id)
        _hash(approval_binding)
        payload = digest({"record_id": record_id, "binding": approval_binding})
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            previous = state["commands"].get(command_id)
            if previous:
                if previous["payload_digest"] != payload:
                    raise Conflict("command ID reused with different payload")
                return False, copy.deepcopy(state["workers"][previous["operation_id"]])
            self._cas(state, expected_revision)
            record = state["records"][record_id]
            if state["active"] or record["status"] != "reviewed" or state["current"][record["domain"]] != record_id:
                raise Conflict("native authority unavailable")
            if record["approval"]["binding"] != approval_binding:
                raise Conflict("review binding changed")
            if not self._matches(record, observation) or not self._scope_matches(state, record):
                self._invalidate(record)
                self._commit(root, artifacts, state)
                raise Conflict("source changed; fresh Preview required")
            operation_id = str(uuid.uuid4())
            state["epoch"] += 1
            worker = {"id": operation_id, "record_id": record_id, "owner": self.owner,
                      "epoch": state["epoch"], "phase": "accepted", "marker": str(uuid.uuid4()),
                      "evidence": None, "outcome": None, "binding": approval_binding}
            record["status"] = "consumed"
            state["workers"][operation_id] = worker
            state["active"] = operation_id
            state["commands"][command_id] = {"payload_digest": payload, "operation_id": operation_id}
            self._append_checkpoint(record, worker)
            self._commit(root, artifacts, state)
            return True, copy.deepcopy(worker)

    def _owned(self, state, operation_id, epoch):
        _id(operation_id)
        _int(epoch)
        worker = state["workers"].get(operation_id)
        if not worker or state["active"] != operation_id or worker["owner"] != self.owner or worker["epoch"] != epoch:
            raise Conflict("native worker fenced")
        return worker, state["records"][worker["record_id"]]

    def checkpoint(self, expected_revision, operation_id, epoch, phase, observation=None, evidence=None):
        """Durable effect intent precedes future adapter calls; this makes none."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            worker, record = self._owned(state, operation_id, epoch)
            transitions = {key: phases - {"invalidated"} for key, phases in WORKER_TRANSITIONS.items()}
            transitions["recovery_required"] = {"completed"}
            if phase not in transitions.get(worker["phase"], set()):
                raise Conflict("invalid native worker transition")
            if phase == "effect_started":
                if not self._matches(record, observation) or not self._scope_matches(state, record):
                    self._invalidate(record)
                    worker.update(phase="invalidated", outcome="invalidated")
                    state["active"] = None
                    self._append_checkpoint(record, worker)
                    self._commit(root, artifacts, state)
                    raise Conflict("source changed; fresh Preview required")
            if phase == "completed":
                _evidence(evidence)
                expected = dict(record["identity"]["effect"], marker=worker["marker"])
                if evidence != expected:
                    raise Conflict("exact effect evidence required")
                worker["evidence"] = copy.deepcopy(evidence)
                record["status"] = "completed"
                if record["domain"] == "scope":
                    state["active_scope"] = copy.deepcopy(record["identity"]["scope_transaction"]["new"])
            elif phase == "recovery_required":
                record["status"] = "recovery_required"
            elif phase == "failed":
                self._invalidate(record)
            worker["phase"] = phase
            if phase in {"completed", "failed"}:
                worker["outcome"] = phase
                state["active"] = None
            self._append_checkpoint(record, worker)
            self._commit(root, artifacts, state)
            return copy.deepcopy(worker)

    def lifecycle(self, expected_revision, app_version, observations):
        """Explicit restart/update takeover; never schedules or retries work."""
        _text(app_version)
        _require(isinstance(observations, dict), "invalid observations")
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            active = state["active"]
            active_record = state["workers"][active]["record_id"] if active else None
            for key in state["current"].values():
                if key and key != active_record:
                    record = state["records"][key]
                    if not self._matches(record, observations.get(key)) or not self._scope_matches(state, record):
                        self._invalidate(record)
            if active:
                worker = state["workers"][active]
                record = state["records"][worker["record_id"]]
                state["epoch"] += 1
                worker.update(owner=self.owner, epoch=state["epoch"])
                if worker["phase"] == "accepted":
                    self._invalidate(record)
                    worker.update(phase="invalidated", outcome="invalidated")
                    state["active"] = None
                else:
                    worker["phase"] = "recovery_required"
                    record["status"] = "recovery_required"
                self._append_checkpoint(record, worker)
            state["app_version"] = app_version
            self._commit(root, artifacts, state)
            return self._projection(state)

    @staticmethod
    def _append_checkpoint(record, worker):
        record["checkpoints"].append({"operation_id": worker["id"], "owner": worker["owner"],
                                      "epoch": worker["epoch"], "phase": worker["phase"],
                                      "evidence": copy.deepcopy(worker["evidence"])})

    @staticmethod
    def _projection(state):
        return {"revision": state["revision"], "active": state["active"], "app_version": state["app_version"]}

    def cancel(self, expected_revision, record_id):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            record = state["records"][record_id]
            if state["active"] or record["status"] not in {"preview", "reviewed", "stale"}:
                raise Conflict("cannot cancel pending effect")
            record.update(status="cancelled", decisions={}, approval=None, validated_at=None)
            self._commit(root, artifacts, state)
