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

SCHEMA = 2
CHUNK_SIZE = 65536
DEFAULT_RETENTION = 32
_OMITTED = object()
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
    payload = {"identity": record["identity"], "artifact": record["artifact"], "decisions": decisions}
    if record.get("binding_version", 1) == 2:
        payload["artifact"] = {"digest": record["artifact"], "size": record["artifact_size"]}
        payload["namespace"] = {"instance_id": record["instance_id"], "generation": record["generation"]}
    return digest(payload)


def _evidence(value):
    _fields(value, "before after marker")
    _hash(value["before"])
    _hash(value["after"])
    _id(value["marker"])


def _record(value, domain, schema=1):
    names = "id domain identity artifact identity_digest status decisions approval created_at validated_at checkpoints"
    if schema == 2:
        names += " instance_id generation artifact_size binding_version recovery_range"
        _id(value["instance_id"])
        _int(value["generation"])
        _require(value["generation"] > 0, "invalid candidate generation")
        _int(value["artifact_size"])
        _require(type(value["binding_version"]) is int and value["binding_version"] in {1, 2}, "invalid binding version")
    _fields(value, names)
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

    def __init__(self, data_root, owner=None, fault=None, retention=DEFAULT_RETENTION):
        self.data_root = Path(data_root).absolute()
        self.owner = owner or str(uuid.uuid4())
        _id(self.owner)
        self.fault = fault or (lambda stage: None)
        _int(retention)
        self.retention = retention
        self.cleanup_errors = []
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

    def _read_file(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._regular(fd)
            data = bytearray()
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    break
                data.extend(chunk)
            self._regular(fd)
            return bytes(data)
        finally:
            os.close(fd)

    @contextmanager
    def _verified_artifact(self, directory, key, size=None):
        _hash(key)
        fd = os.open(key + ".diff", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._regular(fd)
            fingerprint = hashlib.sha256()
            measured = 0
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    break
                measured += len(chunk)
                fingerprint.update(chunk)
            self._regular(fd)
            _require(fingerprint.hexdigest() == key and (size is None or measured == size), "artifact mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            yield fd, measured
        finally:
            os.close(fd)

    def _artifact(self, directory, key, size=None):
        # Bytes convenience only; authority validation uses the streaming path.
        with self._verified_artifact(directory, key, size) as (fd, measured):
            data = bytearray()
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)

    def _verify(self, directory, key, size=None):
        with self._verified_artifact(directory, key, size) as (_, measured):
            return measured

    @staticmethod
    def _reader_chunks(producer):
        while True:
            chunk = producer.read(CHUNK_SIZE)
            _require(type(chunk) is bytes, "invalid artifact chunk")
            if not chunk:
                return
            yield chunk

    def _produce_artifact(self, directory, producer):
        temporary = ".tmp-" + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        fingerprint, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as stream:
                chunks = self._reader_chunks(producer) if hasattr(producer, "read") else iter(producer)
                for supplied in chunks:
                    _require(type(supplied) is bytes, "invalid artifact chunk")
                    # A producer owns its supplied chunk; no second giant copy is made.
                    for offset in range(0, len(supplied), CHUNK_SIZE):
                        chunk = memoryview(supplied)[offset:offset + CHUNK_SIZE]
                        stream.write(chunk)
                        fingerprint.update(chunk)
                        size += len(chunk)
                stream.flush()
                self.fault("artifact_written")
                os.fsync(stream.fileno())
            self.fault("artifact_fsynced")
            key = fingerprint.hexdigest()
            try:
                self._verify(directory, key, size)
            except FileNotFoundError:
                os.replace(temporary, key + ".diff", src_dir_fd=directory, dst_dir_fd=directory)
                self.fault("artifact_replaced")
            # A matching name may be an orphan left before directory durability.
            # Adoption therefore has the same durability gate as a new rename.
            os.fsync(directory)
            self.fault("artifact_durable")
            return key, size
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

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

    def _validate_v1(self, state, artifacts):
        _fields(state, "schema revision instance_id updated_at app_version epoch records current workers active commands baseline_record_id baseline_scope active_scope")
        _require(type(state["schema"]) is int and state["schema"] == 1, "unsupported schema")
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
            self._verify(artifacts, record["artifact"])
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

    def _validate_v2(self, state, artifacts):
        _fields(state, "schema revision instance_id updated_at app_version epoch records current workers active commands baseline_record_id baseline_scope active_scope generation retention checkpoint anchors baseline_provenance recovery")
        _require(type(state["schema"]) is int and state["schema"] == 2, "unsupported schema")
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
            _record(record, record["domain"], 2)
            self._verify(artifacts, record["artifact"], record["artifact_size"])
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
                self._validate_bounded_history(record, linked[0])
            else:
                _require(not record["checkpoints"], "unclaimed candidate history")
        active = state["active"]
        _require(active is None or active in state["workers"] and state["workers"][active]["phase"] in
                 {"accepted", "effect_started", "recovery_required"}, "invalid active worker")
        unresolved = {key for key, worker in state["workers"].items() if worker["phase"] in
                      {"accepted", "effect_started", "recovery_required"}}
        _require(unresolved == ({active} if active else set()), "multiple unresolved workers")
        _int(state["generation"])
        _int(state["retention"])
        generations = set()
        for record in state["records"].values():
            _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"] and
                     record["generation"] not in generations, "candidate namespace mismatch")
            generations.add(record["generation"])
            if not record["checkpoints"]:
                _require(record["recovery_range"] is None, "unclaimed recovery range")
        recovery = state["recovery"]
        if recovery is not None:
            _fields(recovery, "record_id identity_digest")
            _require(recovery["record_id"] in state["records"] and
                     recovery["identity_digest"] == state["records"][recovery["record_id"]]["identity_digest"], "invalid recovery pin")
        operation_ids, legacy_ids = set(), set()
        for key, command in state["commands"].items():
            _fields(command, "command_id generation request_version payload_digest operation_id")
            _id(command["command_id"])
            _int(command["generation"])
            _require(type(command["request_version"]) is int and command["request_version"] in {1, 2}, "invalid request version")
            _hash(command["payload_digest"])
            _require(command["operation_id"] in state["workers"] and command["operation_id"] not in operation_ids,
                     "invalid command reference")
            operation_ids.add(command["operation_id"])
            worker = state["workers"][command["operation_id"]]
            record = state["records"][worker["record_id"]]
            _require(command["generation"] == record["generation"] and key == self._command_key(command["generation"], command["command_id"]),
                     "command namespace mismatch")
            if command["request_version"] == 1:
                _require(record["binding_version"] == 1 and command["command_id"] not in legacy_ids, "native or duplicate receipt in legacy namespace")
                legacy_ids.add(command["command_id"])
            _require(command["payload_digest"] == self._payload(state, record, worker["binding"], command["request_version"]),
                     "command payload mismatch")
        _require(operation_ids == set(state["workers"]), "command/worker graph mismatch")
        self._validate_provenance(state)

    def _validate(self, state, artifacts):
        _require(isinstance(state, dict) and type(state.get("schema")) is int, "unsupported schema")
        if state["schema"] == 1:
            self._validate_v1(state, artifacts)
        else:
            self._validate_v2(state, artifacts)

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

    @staticmethod
    def _checkpoint(through_epoch=0, scope=None, publications=0, migration=None):
        result = {"through_epoch": through_epoch, "scope": copy.deepcopy(scope),
                  "publications": publications, "migration": copy.deepcopy(migration)}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _baseline(record):
        result = {key: copy.deepcopy(record[key]) for key in
                  ("id", "domain", "identity", "identity_digest", "artifact", "artifact_size")}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _capsule(record, worker):
        result = {"record": copy.deepcopy(record), "worker": copy.deepcopy(worker)}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _check_digest(value):
        _require(value["digest"] == digest({key: item for key, item in value.items() if key != "digest"}),
                 "checkpoint certificate mismatch")

    def _validate_capsule(self, value, domain):
        _fields(value, "record worker digest")
        self._check_digest(value)
        record, worker = value["record"], value["worker"]
        _record(record, domain, 2)
        _fields(worker, "id record_id owner epoch phase marker evidence outcome binding")
        for key in ("id", "record_id", "owner", "marker"):
            _id(worker[key])
        _int(worker["epoch"])
        _require(worker["epoch"] > 0, "invalid certificate epoch")
        _require(worker["record_id"] == record["id"] and record["domain"] == domain and
                 record["status"] == "completed" and worker["phase"] == worker["outcome"] == "completed" and
                 worker["binding"] == record["approval"]["binding"], "invalid publication certificate")
        _evidence(worker["evidence"])
        _require(worker["evidence"] == dict(record["identity"]["effect"], marker=worker["marker"]),
                 "certificate completion evidence mismatch")
        for point in record["checkpoints"]:
            _require(point["operation_id"] == worker["id"] and 0 < point["epoch"] <= worker["epoch"] and
                     point["evidence"] == (worker["evidence"] if point["phase"] == "completed" else None),
                     "certificate checkpoint mismatch")
        self._validate_bounded_history(record, worker)
        return record, worker

    @staticmethod
    def _validate_bounded_history(record, worker):
        history, summary = record["checkpoints"], record["recovery_range"]
        if summary is None:
            NativeOperationStore._validate_history(history, worker)
            _require(len(history) <= 6, "unbounded worker history")
            return
        _fields(summary, "first_epoch last_epoch count owner")
        for key in ("first_epoch", "last_epoch", "count"):
            _int(summary[key])
        _id(summary["owner"])
        _require(summary["last_epoch"] > summary["first_epoch"] and
                 summary["count"] == summary["last_epoch"] - summary["first_epoch"] + 1,
                 "invalid recovery range")
        recoveries = [point for point in history if point["phase"] == "recovery_required"]
        _require(len(recoveries) == 2 and len(history) in {4, 5}, "invalid summarized history")
        first, last = recoveries
        _require(first["epoch"] == summary["first_epoch"] and last["epoch"] == summary["last_epoch"] and
                 last["owner"] == summary["owner"], "recovery range mismatch")
        # Validate all retained legal transitions; the one erased range has a
        # typed recovery-only interval, never an arbitrary phase shortcut.
        before = history[:3]
        _require([point["phase"] for point in before] == ["accepted", "effect_started", "recovery_required"],
                 "missing summarized effect intent")
        NativeOperationStore._validate_history(before, dict(worker, **{key: first[key] for key in ("phase", "owner", "epoch")}))
        after = history[3:]
        NativeOperationStore._validate_history([dict(last, phase="accepted"),
                                               dict(last, phase="effect_started"), *after], worker)
        _require(all(point["operation_id"] == worker["id"] for point in history), "summary operation mismatch")

    def _maintain_anchors(self, state):
        if state["baseline_scope"] is None:
            return
        baseline = state["baseline_scope"]
        key = digest(baseline)
        if not state["anchors"]:
            state["anchors"][key] = {"policy": copy.deepcopy(baseline), "start_after_epoch": 0,
                                      "end_epoch": None, "kind": "baseline", "source": copy.deepcopy(state["baseline_provenance"])}
        for worker in sorted(state["workers"].values(), key=lambda item: item["epoch"]):
            record = state["records"][worker["record_id"]]
            if record["domain"] != "scope" or worker["phase"] != "completed" or worker["epoch"] <= state["checkpoint"]["through_epoch"]:
                continue
            old, new = (record["identity"]["scope_transaction"][part] for part in ("old", "new"))
            new_key = digest(new)
            if new_key not in state["anchors"]:
                old_anchor = state["anchors"].get(digest(old))
                _require(old_anchor is not None, "publication predecessor unavailable")
                old_anchor["end_epoch"] = worker["epoch"]
                state["anchors"][new_key] = {"policy": copy.deepcopy(new), "start_after_epoch": worker["epoch"],
                                             "end_epoch": None, "kind": "publication", "source": self._capsule(record, worker)}

    def _validate_provenance(self, state):
        checkpoint = state["checkpoint"]
        _fields(checkpoint, "through_epoch scope publications migration digest")
        self._check_digest(checkpoint)
        _int(checkpoint["through_epoch"])
        _int(checkpoint["publications"])
        frontier = checkpoint["through_epoch"]
        _require(frontier <= state["epoch"], "future compacted frontier")
        _require(isinstance(state["anchors"], dict), "invalid anchors")
        baseline = state["baseline_provenance"]
        if baseline is None:
            _require(state["epoch"] == state["generation"] == 0, "uncovered empty store high-water marks")
            _require(not state["records"] and not state["workers"] and not state["anchors"] and
                     state["baseline_record_id"] is None and state["baseline_scope"] is None and
                     state["active_scope"] is None and checkpoint == self._checkpoint(), "scope without baseline provenance")
            return
        _fields(baseline, "id domain identity identity_digest artifact artifact_size digest")
        self._check_digest(baseline)
        _id(baseline["id"])
        _hash(baseline["artifact"])
        _int(baseline["artifact_size"])
        validate_identity(baseline["identity"], baseline["domain"])
        _require(baseline["identity_digest"] == digest(baseline["identity"]) and
                 baseline["id"] == state["baseline_record_id"] and
                 baseline["identity"]["scope"] == state["baseline_scope"], "baseline provenance mismatch")
        if baseline["id"] in state["records"]:
            _require(self._baseline(state["records"][baseline["id"]]) == baseline, "changed retained baseline")
        _scope(checkpoint["scope"])
        _scope(state["active_scope"])
        _require(checkpoint["scope"]["revision"] == state["baseline_scope"]["revision"] + checkpoint["publications"],
                 "compacted publication count mismatch")
        ordered = []
        for key, anchor in state["anchors"].items():
            _fields(anchor, "policy start_after_epoch end_epoch kind source")
            _scope(anchor["policy"])
            _require(key == digest(anchor["policy"]), "policy anchor mismatch")
            _int(anchor["start_after_epoch"])
            if anchor["end_epoch"] is not None:
                _int(anchor["end_epoch"])
                _require(anchor["start_after_epoch"] < anchor["end_epoch"] <= state["epoch"], "invalid policy interval")
            if anchor["kind"] == "baseline":
                _require(anchor["policy"] == state["baseline_scope"] and anchor["source"] == baseline and
                         anchor["start_after_epoch"] == 0, "invalid baseline anchor")
            else:
                _require(anchor["kind"] == "publication", "invalid anchor kind")
                record, worker = self._validate_capsule(anchor["source"], "scope")
                _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"] and
                         record["identity"]["scope_transaction"]["new"] == anchor["policy"] and
                         worker["epoch"] == anchor["start_after_epoch"] <= state["epoch"], "invalid anchor publication")
            ordered.append(anchor)
        ordered.sort(key=lambda item: item["policy"]["revision"])
        for left, right in zip(ordered, ordered[1:]):
            _require(left["end_epoch"] is not None and left["end_epoch"] <= right["start_after_epoch"] and
                     left["policy"]["revision"] < right["policy"]["revision"], "overlapping policy anchors")
            if right["policy"]["revision"] == left["policy"]["revision"] + 1:
                _require(left["end_epoch"] == right["start_after_epoch"] and
                         right["source"]["record"]["identity"]["scope"] == left["policy"], "adjacent policy boundary mismatch")
        def anchor_for(policy):
            anchor = state["anchors"].get(digest(policy))
            _require(anchor is not None and anchor["policy"] == policy, "record lacks established policy provenance")
            return anchor
        boundary = anchor_for(checkpoint["scope"])
        _require(boundary["start_after_epoch"] <= frontier and
                 (boundary["end_epoch"] is None or frontier <= boundary["end_epoch"]), "checkpoint boundary outside policy")
        intervals, archived = [], []
        for worker in state["workers"].values():
            record = state["records"][worker["record_id"]]
            start, end = record["checkpoints"][0]["epoch"], worker["epoch"]
            anchor = anchor_for(record["identity"]["scope"])
            _require(anchor["start_after_epoch"] < start <= end and
                     (anchor["end_epoch"] is None or end <= anchor["end_epoch"]), "worker outside policy interval")
            if end <= frontier:
                _require(worker["phase"] in {"completed", "failed", "invalidated"}, "archived unresolved worker")
                archived.append((start, end, worker))
            else:
                _require(start > frontier, "worker crosses frontier")
                intervals.append((start, end, worker))
            if record["domain"] == "scope" and worker["phase"] == "completed":
                new_anchor = state["anchors"].get(digest(record["identity"]["scope_transaction"]["new"]))
                _require(anchor["end_epoch"] == end, "publisher policy does not close")
                if new_anchor is not None:
                    _require(new_anchor["start_after_epoch"] == end and new_anchor["source"] == self._capsule(record, worker),
                             "retained publication certificate mismatch")
        for before, after in zip(sorted(archived), sorted(archived)[1:]):
            _require(before[1] < after[0], "overlapping archived workers")
        expected, next_epoch = checkpoint["scope"], frontier + 1
        for start, end, worker in sorted(intervals):
            _require(start == next_epoch, "incomplete claim epoch history")
            next_epoch = end + 1
            record = state["records"][worker["record_id"]]
            _require(record["identity"]["scope"] == expected, "worker scope does not follow prior confirmed publication")
            if record["domain"] == "scope" and worker["phase"] == "completed":
                expected = record["identity"]["scope_transaction"]["new"]
        _require(next_epoch == state["epoch"] + 1, "incomplete claim epoch history")
        active_anchor = anchor_for(state["active_scope"])
        _require(expected == state["active_scope"] and active_anchor["end_epoch"] is None, "active scope lacks confirmed publication")
        for record in state["records"].values():
            anchor_for(record["identity"]["scope"])
        if state["active"]:
            _require(state["workers"][state["active"]]["epoch"] == state["epoch"], "active worker not current epoch")
        self._validate_migration_checkpoint(state)
        self._validate_capsule_sources(state)

    def _validate_capsule_sources(self, state):
        """All provenance shares the same available candidate/worker namespace."""
        capsules = [anchor["source"] for anchor in state["anchors"].values() if anchor["kind"] == "publication"]
        if state["checkpoint"]["migration"] is not None:
            capsules.append(state["checkpoint"]["migration"])
        records_by_id = dict(state["records"])
        records_by_generation = {record["generation"]: record for record in state["records"].values()}
        workers_by_id = dict(state["workers"])
        workers_by_record = {worker["record_id"]: worker for worker in state["workers"].values()}
        intervals = [(state["records"][worker["record_id"]]["checkpoints"][0]["epoch"], worker["epoch"],
                      state["records"][worker["record_id"]], worker) for worker in state["workers"].values()]
        baseline = state["baseline_provenance"]
        for capsule in capsules:
            record, worker = capsule["record"], capsule["worker"]
            _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"],
                     "capsule namespace mismatch")
            for retained in (records_by_id.get(record["id"]), records_by_generation.get(record["generation"])):
                if retained is not None:
                    _require(retained == record and workers_by_record.get(record["id"]) == worker,
                             "capsule retained candidate/source mismatch")
            retained_worker = workers_by_id.get(worker["id"])
            if retained_worker is not None:
                _require(retained_worker == worker and records_by_id.get(worker["record_id"]) == record,
                         "capsule retained operation/source mismatch")
            if record["id"] == baseline["id"]:
                _require(self._baseline(record) == baseline, "capsule baseline source mismatch")
            records_by_id[record["id"]] = record
            records_by_generation[record["generation"]] = record
            workers_by_id[worker["id"]] = worker
            workers_by_record[record["id"]] = worker
            intervals.append((record["checkpoints"][0]["epoch"], worker["epoch"], record, worker))
        intervals.sort(key=lambda interval: (interval[0], interval[1]))
        for left, right in zip(intervals, intervals[1:]):
            if left[1] >= right[0]:
                _require(left[2:] == right[2:], "capsule epoch/source overlap")

    def _validate_migration_checkpoint(self, state):
        checkpoint = state["checkpoint"]
        frontier, capsule = checkpoint["through_epoch"], checkpoint["migration"]
        folded = [worker for worker in state["workers"].values()
                  if worker["epoch"] <= frontier and worker["phase"] == "completed" and
                  state["records"][worker["record_id"]]["domain"] == "migration"]
        if capsule is None:
            _require(not folded, "missing retained migration provenance")
            return
        record, worker = self._validate_capsule(capsule, "migration")
        start, end = record["checkpoints"][0]["epoch"], worker["epoch"]
        _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"],
                 "migration namespace mismatch")
        _require(end <= frontier, "migration outside compacted prefix")
        if folded:
            latest = max(folded, key=lambda item: item["epoch"])
            _require(latest["epoch"] <= end, "missing latest retained migration provenance")
            if latest["epoch"] == end:
                _require(capsule == self._capsule(state["records"][latest["record_id"]], latest),
                         "latest migration certificate mismatch")
        # Prefix policy evidence remains checkable even when the full source
        # and its old policy anchor have deliberately retired.
        policy = record["identity"]["scope"]
        _require(state["baseline_scope"]["revision"] <= policy["revision"] <= checkpoint["scope"]["revision"],
                 "migration policy outside compacted prefix")
        for retained_anchor in state["anchors"].values():
            if retained_anchor["kind"] != "publication":
                continue
            source_record, source_worker = retained_anchor["source"]["record"], retained_anchor["source"]["worker"]
            old_policy = source_record["identity"]["scope"]
            if policy["revision"] == old_policy["revision"]:
                _require(policy == old_policy and end <= source_worker["epoch"], "migration publication policy mismatch")
        for confirmed in (state["baseline_scope"], checkpoint["scope"]):
            if policy["revision"] == confirmed["revision"]:
                _require(policy == confirmed, "migration policy mismatch")
        anchor = next((item for item in state["anchors"].values()
                       if item["policy"]["revision"] == policy["revision"]), None)
        if anchor is not None:
            _require(anchor["policy"] == policy and anchor["start_after_epoch"] < start and
                     (anchor["end_epoch"] is None or end <= anchor["end_epoch"]), "migration outside retained policy interval")

    @staticmethod
    def _pins(state):
        pins = {key for key in state["current"].values() if key is not None}
        if state["active"]:
            pins.add(state["workers"][state["active"]]["record_id"])
        if state["recovery"]:
            pins.add(state["recovery"]["record_id"])
        return pins

    def _compact_graph(self, state):
        checkpoint = state["checkpoint"]
        frontier = checkpoint["through_epoch"]
        suffix = sorted((worker for worker in state["workers"].values() if worker["epoch"] > frontier),
                        key=lambda worker: state["records"][worker["record_id"]]["checkpoints"][0]["epoch"])
        terminal = [worker for worker in suffix if worker["phase"] in {"completed", "failed", "invalidated"}]
        folded = terminal[:max(0, len(terminal) - state["retention"])]
        boundary, count, migration = checkpoint["scope"], checkpoint["publications"], checkpoint["migration"]
        for worker in folded:
            record = state["records"][worker["record_id"]]
            frontier = worker["epoch"]
            if worker["phase"] == "completed":
                if record["domain"] == "scope":
                    boundary = record["identity"]["scope_transaction"]["new"]
                    count += 1
                elif record["domain"] == "migration":
                    migration = self._capsule(record, worker)
        state["checkpoint"] = self._checkpoint(frontier, boundary, count, migration)
        keep = self._pins(state) | {worker["record_id"] for worker in suffix if worker["epoch"] > frontier}
        state["records"] = {key: record for key, record in state["records"].items() if key in keep}
        state["workers"] = {key: worker for key, worker in state["workers"].items() if worker["record_id"] in keep}
        state["commands"] = {key: receipt for key, receipt in state["commands"].items() if receipt["operation_id"] in state["workers"]}
        policies = {digest(record["identity"]["scope"]) for record in state["records"].values()}
        if boundary is not None:
            policies.update((digest(boundary), digest(state["active_scope"])))
        state["anchors"] = {key: anchor for key, anchor in state["anchors"].items() if key in policies}
        _require(len(state["records"]) <= state["retention"] + 6 and len(state["anchors"]) <= state["retention"] + 8,
                 "retention partition exceeds bound")

    def _validate_retention(self, state):
        frontier = state["checkpoint"]["through_epoch"]
        suffix = [worker for worker in state["workers"].values() if worker["epoch"] > frontier]
        keep = self._pins(state) | {worker["record_id"] for worker in suffix}
        _require(set(state["records"]) == keep and
                 sum(worker["phase"] in {"completed", "failed", "invalidated"} for worker in suffix) <= state["retention"] and
                 len(state["records"]) <= state["retention"] + 6, "invalid retention partition")
        policies = {digest(record["identity"]["scope"]) for record in state["records"].values()}
        if state["baseline_scope"] is not None:
            policies.update((digest(state["checkpoint"]["scope"]), digest(state["active_scope"])))
        _require(set(state["anchors"]) == policies and len(policies) <= state["retention"] + 8,
                 "invalid retained anchor set")

    def _sweep(self, directory, state, manifests=False):
        marked = {record["artifact"] + ".diff" for record in state["records"].values()}
        self.cleanup_errors = [] if not manifests else self.cleanup_errors
        try:
            names = os.listdir(directory)
        except OSError as exc:
            self.cleanup_errors.append(str(exc))
            return
        for name in names:
            temporary = re.fullmatch(r"\.tmp-([0-9a-f-]{36})", name)
            owned_temp = False
            if temporary:
                try:
                    _id(temporary[1])
                    owned_temp = True
                except InvalidState:
                    pass
            owned_artifact = not manifests and name.endswith(".diff") and DIGEST.fullmatch(name[:-5])
            if not (owned_temp or owned_artifact) or name in marked:
                continue
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                self._regular(fd)
                before = os.fstat(fd)
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _require((before.st_dev, before.st_ino) == (entry.st_dev, entry.st_ino), "cleanup entry changed")
                self.fault("cleanup_before_unlink")
                os.unlink(name, dir_fd=directory)
                self.fault("cleanup_unlinked")
            except (OSError, InvalidState) as exc:
                self.cleanup_errors.append(str(exc))
            finally:
                if fd is not None:
                    os.close(fd)
        try:
            os.fsync(directory)
            self.fault("cleanup_durable")
        except OSError as exc:
            self.cleanup_errors.append(str(exc))

    def compact(self, expected_revision):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            self._commit(root, artifacts, state)
            return self._projection(state)

    def recovery_pin(self, expected_revision, expected_slot, record_id=None, identity_digest=None):
        """Exact CAS protection only; no recovery journal or rollback is performed."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state["recovery"] != expected_slot:
                raise Conflict("recovery slot changed")
            if record_id is None:
                _require(identity_digest is None, "invalid closing identity")
                state["recovery"] = None
            else:
                record = state["records"].get(record_id)
                if record is None or record["identity_digest"] != identity_digest:
                    raise Conflict("recovery record identity changed")
                state["recovery"] = {"record_id": record_id, "identity_digest": identity_digest}
            self._commit(root, artifacts, state)
            return copy.deepcopy(state["recovery"])

    def copy_diff(self, record_id, destination):
        """Verify to EOF before emitting bytes; hold the cooperative writer lock."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            record = state["records"][record_id]
            with self._verified_artifact(artifacts, record["artifact"], record.get("artifact_size")) as (fd, size):
                while True:
                    chunk = os.read(fd, CHUNK_SIZE)
                    if not chunk:
                        return size
                    view = memoryview(chunk)
                    while view:
                        written = destination.write(view)
                        _require(type(written) is int and 0 < written <= len(view), "invalid artifact consumer write")
                        view = view[written:]

    def accounting(self):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            live = {record["artifact"] for record in state["records"].values()}
            stored = reclaimable = temporary = 0
            for directory in (root, artifacts):
                for name in os.listdir(directory):
                    if not (re.fullmatch(r"[0-9a-f]{64}\.diff", name) or re.fullmatch(r"\.tmp-[0-9a-f-]{36}", name)):
                        continue
                    fd = None
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        self._regular(fd)
                        size = os.fstat(fd).st_size
                        if name.startswith(".tmp-"):
                            temporary += size
                        else:
                            stored += size
                            if name[:-5] not in live:
                                reclaimable += size
                    except (OSError, InvalidState):
                        pass
                    finally:
                        if fd is not None:
                            os.close(fd)
            return {"artifact_bytes": stored, "manifest_bytes": len(canonical(state)),
                    "temporary_bytes": temporary, "reclaimable_bytes": reclaimable}

    def upgrade(self, expected_revision):
        """Explicit CAS conversion; validate the entire original graph first."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state["schema"] == SCHEMA:
                return self._projection(state)
            state.update(schema=SCHEMA, generation=0, retention=self.retention, recovery=None,
                         anchors={}, baseline_provenance=None, checkpoint=self._checkpoint(scope=state["baseline_scope"]))
            for number, record in enumerate(sorted(state["records"].values(), key=lambda item: (item["created_at"], item["id"])), 1):
                record.update(instance_id=state["instance_id"], generation=number, binding_version=1,
                              artifact_size=self._verify(artifacts, record["artifact"]), recovery_range=None)
                state["generation"] = number
                linked = next((worker for worker in state["workers"].values() if worker["record_id"] == record["id"]), None)
                if linked:
                    history = record["checkpoints"]
                    record["checkpoints"] = []
                    for point in history:
                        self._append_checkpoint(record, dict(linked, **{key: point[key] for key in ("phase", "owner", "epoch", "evidence")}))
            if state["baseline_record_id"]:
                state["baseline_provenance"] = self._baseline(state["records"][state["baseline_record_id"]])
            commands = {}
            for command_id, receipt in state["commands"].items():
                record = state["records"][state["workers"][receipt["operation_id"]]["record_id"]]
                commands[self._command_key(record["generation"], command_id)] = dict(receipt, command_id=command_id,
                                                                                   generation=record["generation"], request_version=1)
            state["commands"] = commands
            self._commit(root, artifacts, state)
            return self._projection(state)

    def _load(self, root, artifacts):
        try:
            state = _parse(self._read_file(root, "state.json"))
        except FileNotFoundError:
            state = {"schema": SCHEMA, "revision": 0, "instance_id": str(uuid.uuid4()),
                     "updated_at": _now(), "app_version": "unactivated", "epoch": 0,
                     "records": {}, "current": dict.fromkeys(sorted(DOMAINS)), "workers": {},
                     "active": None, "commands": {}, "baseline_record_id": None,
                     "baseline_scope": None, "active_scope": None,
                     "generation": 0, "retention": self.retention, "checkpoint": self._checkpoint(),
                     "anchors": {}, "baseline_provenance": None, "recovery": None}
            self._write(root, "state.json", canonical(state))
        try:
            self._validate(state, artifacts)
            if state["schema"] == 2:
                self._validate_retention(state)
        except (KeyError, TypeError, RecursionError) as exc:
            raise InvalidState("invalid native state graph") from exc
        return state

    def _commit(self, root, artifacts, state):
        state["revision"] += 1
        state["updated_at"] = _now()
        _require(state["schema"] == SCHEMA, "explicit schema upgrade required")
        self._maintain_anchors(state)
        self._validate(state, artifacts)
        self._compact_graph(state)
        self._validate(state, artifacts)
        self._write(root, "state.json", canonical(state))
        self._sweep(artifacts, state)
        self._sweep(root, state, manifests=True)

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
            record = state["records"][record_id]
            return self._artifact(artifacts, record["artifact"], record.get("artifact_size"))

    def preview(self, expected_revision, domain, identity, diff):
        validate_identity(identity, domain)
        _require(type(diff) is bytes, "invalid artifact bytes")
        return self.preview_stream(expected_revision, domain, identity, (diff,))

    def preview_stream(self, expected_revision, domain, identity, producer):
        validate_identity(identity, domain)
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state["active"]:
                raise Conflict("native worker unresolved")
            if state["active_scope"] is not None and identity["scope"] != state["active_scope"]:
                raise Conflict("scope change requires completed publication")
            _require(state["schema"] == SCHEMA, "explicit schema upgrade required")
            key, size = self._produce_artifact(artifacts, producer)
            state["generation"] += 1
            record_id = str(uuid.uuid4())
            record = {"id": record_id, "domain": domain, "identity": copy.deepcopy(identity),
                      "identity_digest": digest(identity), "artifact": key, "status": "preview",
                      "decisions": {}, "approval": None, "created_at": _now(),
                      "validated_at": None, "checkpoints": [], "recovery_range": None,
                      "artifact_size": size, "binding_version": 2, "instance_id": state["instance_id"],
                      "generation": state["generation"]}
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
                state["baseline_provenance"] = self._baseline(record)
                state["checkpoint"] = self._checkpoint(scope=identity["scope"])
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

    @staticmethod
    def _command_key(generation, command_id):
        return str(generation) + ":" + command_id

    @staticmethod
    def _payload(state, record, binding, request_version):
        payload = {"record_id": record["id"], "binding": binding}
        if request_version == 2:
            payload.update(instance_id=state["instance_id"], generation=record["generation"])
        return digest(payload)

    def claim(self, expected_revision, command_id, record_id, approval_binding, observation,
              *, instance_id=_OMITTED, generation=_OMITTED):
        _id(command_id)
        _hash(approval_binding)
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            legacy_payload = digest({"record_id": record_id, "binding": approval_binding})
            if state["schema"] == 1:
                if instance_id is not _OMITTED or generation is not _OMITTED:
                    raise Conflict("candidate namespace unavailable before schema upgrade")
                previous = state["commands"].get(command_id)
                if previous and previous["payload_digest"] == legacy_payload:
                    return False, copy.deepcopy(state["workers"][previous["operation_id"]])
                raise Conflict("explicit schema upgrade required")
            if instance_id is _OMITTED and generation is _OMITTED:
                previous = next((receipt for receipt in state["commands"].values()
                                 if receipt["request_version"] == 1 and receipt["command_id"] == command_id), None)
                if previous and previous["payload_digest"] == legacy_payload:
                    return False, copy.deepcopy(state["workers"][previous["operation_id"]])
                raise Conflict("candidate namespace unavailable or different payload")
            if instance_id != state["instance_id"] or type(generation) is not int or generation <= 0:
                raise Conflict("candidate namespace unavailable")
            record = state["records"].get(record_id)
            if record is None or record["generation"] != generation:
                raise Conflict("candidate generation unavailable")
            key = self._command_key(generation, command_id)
            previous = state["commands"].get(key)
            payload = self._payload(state, record, approval_binding, previous["request_version"] if previous else 2)
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
            state["commands"][key] = {"command_id": command_id, "generation": generation, "request_version": 2,
                                       "payload_digest": payload, "operation_id": operation_id}
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
        if "recovery_range" in record:
            history = record["checkpoints"]
            recoveries = [point for point in history if point["phase"] == "recovery_required"]
            if len(recoveries) > 3 or record["recovery_range"] is not None:
                first, latest = recoveries[0], recoveries[-1]
                record["recovery_range"] = {"first_epoch": first["epoch"], "last_epoch": latest["epoch"],
                                            "count": latest["epoch"] - first["epoch"] + 1,
                                            "owner": latest["owner"]}
                record["checkpoints"] = [point for point in history if point["phase"] != "recovery_required"]
                record["checkpoints"].insert(2, first)
                if latest is not first:
                    record["checkpoints"].insert(3, latest)

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
