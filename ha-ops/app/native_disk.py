"""Dormant native storage diagnostics and cooperative, context-only admission.

No executor is installed here. See docs/native-disk.md for the supported
allocation/executor profile and the obligations on a future caller.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
import base64
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import threading
import time
import weakref

from native_inventory import Inventory, beneath, linux_mounts, path_bytes

WARNING_BYTES = 500 * 1024**2
CATEGORIES = ("git", "staging", "diff", "recovery", "service")
_THREAD_LOCKS = {}
_THREAD_LOCKS_MUTEX = threading.Lock()
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _after_fork():
    global _THREAD_LOCKS, _THREAD_LOCKS_MUTEX
    _THREAD_LOCKS = {}
    _THREAD_LOCKS_MUTEX = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class Blocked(ValueError):
    """Cannot establish authority; distinct from proven capacity shortfall."""


class InsufficientSpace(Blocked):
    pass


def _observation(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_nlink,
            s.st_mtime_ns, s.st_ctime_ns, s.st_blocks)


def _identity(s):
    return s.st_dev, s.st_ino, s.st_mode


def _digest(value):
    return hashlib.sha256(repr(value).encode("utf-8", "backslashreplace")).hexdigest()


def _control_json(data):
    """Owner-equivalent strict JSON grammar, preserving raw-byte invalidity."""
    from native_operations import _parse
    try:
        data.decode("utf-8")  # Owner serialization is UTF-8, not guessed encodings.
        value = _parse(data)
        # JSON's exponent overflow can produce infinity without parse_constant.
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
            elif isinstance(current, float) and not math.isfinite(current):
                raise Blocked("nonfinite-control-json")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise Blocked("invalid-control-json:" + str(exc)) from exc


def _round(size, quantum):
    return ((size + quantum - 1) // quantum) * quantum


def _zbound(size):
    return size + (size >> 12) + (size >> 14) + (size >> 25) + 13


def _loose(kind, size):
    return _zbound(len(kind) + 1 + len(str(size)) + 1 + size)


@contextmanager
def _directory(path, observations=None):
    """Acquire every component without following links; at most two FDs."""
    path = os.fsencode(os.path.abspath(path))
    fd = os.open(b"/", _DIR_FLAGS)
    route = []
    try:
        current = b""
        for part in path.split(b"/")[1:]:
            if not part:
                continue
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            try:
                if _identity(before) != _identity(os.fstat(child)):
                    raise Blocked("changed-directory-component")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
            current += b"/" + part
            route.append((current, _identity(before)))
            if observations is not None:
                observations[current] = _identity(before)
        yield fd
        # Do not erase an earlier error by retrying a replaced route.
        for name, identity in route:
            if _identity(os.stat(name, follow_symlinks=False)) != identity:
                raise Blocked("replaced-directory-route")
    finally:
        os.close(fd)


def _optional_stat(path, observations=None):
    """Prove optional absence through held nofollow parents, never lexists.

    A missing stat is confirmed with a separate nofollow open. Failure after
    observing a component is uncertainty, including directory acquisition loss.
    This is a targeted route walk; no directory or storage enumeration occurs.
    """
    path = os.fsencode(os.path.abspath(path))
    fd = os.open(b"/", _DIR_FLAGS)
    route = []
    current = b""
    parts = [p for p in path.split(b"/") if p]
    try:
        for index, part in enumerate(parts):
            current += b"/" + part
            final = index == len(parts) - 1
            flags = (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK) if final else _DIR_FLAGS
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                try:
                    probe = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if observations is not None:
                        observations[current] = None  # retain observed component loss
                    return None
                except OSError as failure:
                    raise Blocked("unverifiable-optional-presence:" + repr(current)) from failure
                else:
                    os.close(probe)
                    raise Blocked("unverifiable-optional-presence:" + repr(current)) from exc
            if observations is not None:
                observations[current] = _identity(info)
            route.append((current, _identity(info)))
            if final:
                return info
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise Blocked("unverifiable-optional-parent:" + repr(current)) from exc
            try:
                if _identity(info) != _identity(os.fstat(child)):
                    raise Blocked("changed-optional-parent")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return os.fstat(fd)
    finally:
        try:
            for name, identity in route:
                if _identity(os.stat(name, follow_symlinks=False)) != identity:
                    raise Blocked("changed-optional-route")
        except OSError as exc:
            raise Blocked("unavailable-optional-route") from exc
        finally:
            os.close(fd)


@dataclass(frozen=True)
class Limits:
    entries: int = 200_000
    seconds: float = 20.0
    control_bytes: int = 16 * 1024**2
    git_bytes: int = 64 * 1024**2

    def __post_init__(self):
        if any(type(getattr(self, name)) is not int or getattr(self, name) <= 0
               for name in ("entries", "control_bytes", "git_bytes")):
            raise Blocked("positive-integer-work-budget-required")
        if type(self.seconds) not in (int, float) or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise Blocked("finite-positive-deadline-required")


@dataclass(frozen=True)
class Layout:
    """Legacy copies are opt-in; live roots and user backup roots are excluded."""
    legacy: tuple = ()

    def __post_init__(self):
        paths = tuple(path_bytes(p) for p in self.legacy)
        if any(not beneath(p, b"data") or p == b"data" or beneath(p, b"data/native")
               or beneath(p, b"data/native-operations") for p in paths):
            raise Blocked("legacy-copy-must-be-private-data")
        object.__setattr__(self, "legacy", paths)

    @property
    def anchors(self):
        return (b"data/native", b"data/native-operations") + self.legacy


@dataclass(frozen=True)
class Source:
    path: bytes
    observation: tuple

    def __post_init__(self):
        object.__setattr__(self, "path", os.fsencode(self.path))
        object.__setattr__(self, "observation", tuple(self.observation))
        if len(self.observation) != 8 or any(type(v) is not int for v in self.observation):
            raise Blocked("invalid-source-observation")

    @property
    def size(self):
        return self.observation[3]


@dataclass(frozen=True)
class Volume:
    key: tuple
    mounts: tuple
    available: int
    inodes: int | None
    quantum: int
    filesystem: bytes
    supported: bool
    allocated: int = 0
    largest_control: int = 0

    @property
    def warning(self):
        return self.available < WARNING_BYTES


@dataclass(frozen=True)
class Snapshot:
    timestamp: float
    duration: float
    categories: tuple
    volumes: tuple[Volume, ...]
    observations: tuple
    issues: tuple[str, ...]
    digest: str
    layout_digest: str
    root_identity: tuple

    @property
    def complete(self):
        return not self.issues

    @property
    def logical(self):
        return sum(row[1] for row in self.categories)

    @property
    def allocated(self):
        return sum(row[2] for row in self.categories)


@dataclass(frozen=True)
class Target:
    """An exact regular output, or a directory containing generated outputs."""
    path: bytes
    kind: str = "file"

    def __post_init__(self):
        object.__setattr__(self, "path", path_bytes(self.path))
        if self.kind not in ("file", "directory"):
            raise Blocked("typed-output-required")


def _target(value):
    return value if isinstance(value, Target) else Target(value)


@dataclass(frozen=True)
class Control:
    """Full exact prospective serialization, never a caller-supplied byte budget."""
    target: bytes
    payload: bytes
    operation_id: str | None = None
    owner_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "target", _target(self.target))
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise Blocked("exact-control-serialization-required")
        object.__setattr__(self, "payload", bytes(self.payload))
        if (self.operation_id is None) != (self.owner_id is None):
            raise Blocked("complete-control-identity-required")
        if self.operation_id is not None and any(not isinstance(v, str) or not v for v in (self.operation_id, self.owner_id)):
            raise Blocked("immutable-control-identity-required")


@dataclass(frozen=True)
class Evidence:
    operation_id: str
    owner_id: str
    selected: tuple = ()  # (Git byte pathname, independently observed Source)
    base_tree: str | None = None
    controls: tuple[Control, ...] = ()
    materialize: bool = False
    diff: Source | None = None
    commit: bytes | None = None
    copies: tuple = ()  # (Source, destination relative to native root)
    replacements: tuple = ()  # (verified local Git blob OID, destination)
    metadata: tuple = ()  # exact encoded metadata artifacts, same shape as copies
    incoming: Source | None = None
    incoming_index: Source | None = None
    executor: str = "git-native-disk-v1"
    candidate_manifest: bytes | None = None
    candidate_removals: tuple[bytes, ...] = ()
    candidate_final: tuple[bytes, ...] = ()
    ref: bytes | None = None
    reflog_entry: bytes | None = None
    publication: object | None = None
    scope_preparation: object | None = None
    candidate_rules: bytes | None = None

    def __post_init__(self):
        # A frozen dataclass does not freeze caller-owned lists/buffers. Capture
        # every nested sequence before it can become an issued recipe.
        for field in ("operation_id", "owner_id", "executor"):
            if not isinstance(getattr(self, field), str):
                raise Blocked("immutable-operation-text-required")
        if type(self.materialize) is not bool:
            raise Blocked("immutable-materialization-choice-required")
        if self.base_tree is not None and not isinstance(self.base_tree, str):
            raise Blocked("immutable-base-tree-required")
        for field in ("selected", "copies", "metadata", "replacements"):
            rows = []
            for pair in getattr(self, field):
                if len(pair) != 2:
                    raise Blocked("invalid-operation-evidence-pair")
                left, right = pair
                if field == "selected":
                    left = path_bytes(left)
                    if not isinstance(right, Source):
                        raise Blocked("selected-source-required")
                elif field == "replacements":
                    if not isinstance(left, str):
                        raise Blocked("replacement-object-id-required")
                    right = _target(right)
                else:
                    if not isinstance(left, Source):
                        raise Blocked("copy-source-required")
                    right = _target(right)
                rows.append((left, right))
            object.__setattr__(self, field, tuple(rows))
        object.__setattr__(self, "controls", tuple(self.controls))
        if any(not isinstance(c, Control) for c in self.controls):
            raise Blocked("exact-control-required")
        for field in ("candidate_removals", "candidate_final"):
            object.__setattr__(self, field, tuple(path_bytes(p) for p in getattr(self, field)))
            if len(set(getattr(self, field))) != len(getattr(self, field)):
                raise Blocked("duplicate-candidate-path")
        for field in ("commit", "reflog_entry", "candidate_manifest", "candidate_rules"):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, (bytes, bytearray, memoryview)):
                    raise Blocked("exact-serialized-output-required")
                object.__setattr__(self, field, bytes(value))
        if self.ref is not None:
            object.__setattr__(self, "ref", path_bytes(self.ref))
        for field in ("diff", "incoming", "incoming_index"):
            if getattr(self, field) is not None and not isinstance(getattr(self, field), Source):
                raise Blocked("observed-source-required")


@dataclass(frozen=True)
class Allocation:
    volume: tuple
    stage: str
    output: str
    logical: int
    allocated: int
    inodes: int


@dataclass(frozen=True)
class Reserve:
    volume: tuple
    current_control: int
    pending_control: int
    quantum: int
    observed_growth: int
    history: tuple

    @property
    def total(self):
        return self.current_control + self.pending_control + self.quantum + self.observed_growth


@dataclass(frozen=True)
class Decision:
    status: str
    volume: tuple
    additional_peak: int
    reserve: int
    required_inodes: int
    available: int
    available_inodes: int | None
    shortfall: int
    warning: bool


@dataclass(frozen=True)
class Plan:
    kind: str
    evidence: Evidence
    snapshot_digest: str
    layout_digest: str
    allocations: tuple[Allocation, ...]
    reserves: tuple[Reserve, ...]
    decisions: tuple[Decision, ...]
    unknown: tuple[str, ...]
    provenance: tuple
    retained_growth: tuple
    namespace: tuple = ()
    future_directories: tuple = ()
    output_files: tuple = ()
    network_cap: int = 0

    @property
    def supported(self):
        return not self.unknown


@dataclass(frozen=True)
class _Context:
    snapshot: Snapshot
    cache: tuple
    sources: tuple
    routes: tuple
    dependencies: tuple
    memberships: tuple
    owner_locks: tuple
    mounts: tuple
    resource_counts: tuple
    resource_bytes: int
    resource_entries: int
    network_cap: int
    owner_binding: tuple | None
    state_bytes: int
    state_shape: str | None
    existing_resources: tuple
    index_bytes: int
    spool_sizes: tuple
    materialized_sizes: tuple
    prospective_owner: tuple | None
    prospective_shape: str | None
    publication_states: tuple = ()


class NativeDisk:
    def __init__(self, root, *, layout=None, limits=None):
        # Deliberately no stat, mkdir, scan, subprocess or lock at construction.
        self.root = os.fsencode(os.path.abspath(root))
        self.layout = layout or Layout()
        self.limits = limits or Limits()
        if not isinstance(self.layout, Layout) or not isinstance(self.limits, Limits):
            raise Blocked("immutable-layout-and-limits-required")
        self._plans = weakref.WeakValueDictionary()  # never durable authority
        self._contexts = {}
        self._plan_refs = {}
        self._snapshots = weakref.WeakValueDictionary()
        self._active_operation = None
        self._candidate_admission = None

    @property
    def layout_digest(self):
        return _digest((self.root, self.layout))

    def source(self, path):
        """Capture a selected source without reading its content."""
        path = os.fsencode(os.path.abspath(path))
        with _directory(os.path.dirname(path)) as parent:
            before = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Blocked("single-link-regular-source-required")
            fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            try:
                if _observation(before) != _observation(os.fstat(fd)):
                    raise Blocked("changed-source")
                return Source(path, _observation(before))
            finally:
                os.close(fd)

    def _require_absent(self, path, initial=None, routes=None):
        path = os.fsencode(os.path.abspath(path))
        # A failed known leaf acquisition is already disqualifying evidence.
        # A later observation of the same inode cannot undo that loss; ancestor
        # identity checks never requalify a source or its complete preimage.
        if initial is not None and initial.get(path) is not None:
            raise Blocked("missing-or-unacquired-snapshot-known-dependency:" + repr(path))
        observed = {}
        if _optional_stat(path, observed) is not None:
            raise Blocked("unverifiable-existing-presence:" + repr(path))
        if initial is not None:
            for name, identity in observed.items():
                if identity is None and initial.get(name) is not None:
                    raise Blocked("observed-loss-of-snapshot-known-ancestor:" + repr(name))
            current = os.path.dirname(path)  # leaf and ancestors have distinct duties
            while current != self.root:
                if initial.get(current) is not None:
                    info = _optional_stat(current)
                    if info is None or _identity(info) != initial[current][:3]:
                        raise Blocked("missing-or-changed-snapshot-known-dependency:" + repr(current))
                current = os.path.dirname(current)
        if routes is not None:
            routes.update((name, identity) for name, identity in observed.items() if identity is not None)

    def _optional_source(self, path, initial=None, routes=None):
        try:
            source = self.source(path)
        except FileNotFoundError:
            self._require_absent(path, initial, routes)
            return None
        if routes is not None:
            with _directory(os.path.dirname(source.path), observations=routes):
                pass
        if initial is not None and initial.get(source.path) is not None:
            if source.observation != initial[source.path]:
                raise Blocked("changed-snapshot-known-dependency:" + repr(source.path))
        return source

    def selection(self, inventory, source_root, *, operation_id, owner_id, controls,
                  base_tree=None, materialize=False, diff=None, commit=None):
        if not isinstance(inventory, Inventory) or inventory.blockers or not inventory.topology_verified:
            raise Blocked("fresh-complete-native-inventory-required")
        selected = []
        for e in inventory.entries:
            if e.path not in inventory.managed_files:
                continue
            source = self.source(os.path.join(os.fsencode(source_root), e.path))
            if (e.device, e.inode, e.mode, e.size, e.links) != (
                    source.observation[0], source.observation[1], source.observation[2],
                    source.size, source.observation[4]):
                raise Blocked("stale-native-inventory")
            selected.append((e.path, source))
        return Evidence(operation_id, owner_id, tuple(selected), base_tree,
                        tuple(controls), materialize, diff, commit)

    def _mount(self, path, info, mounts):
        if mounts is None:
            return (info.st_dev,), ("host-device", info.st_dev), b"unknown", False
        relevant = [m for m in mounts if beneath(path, m.point) or m.point == b"/"]
        if not relevant:
            raise Blocked("unavailable-mount")
        depth = max(len(m.point) for m in relevant)
        chosen = [m for m in relevant if len(m.point) == depth]
        if len(chosen) != 1:
            raise Blocked("stacked-mount")
        m = chosen[0]
        actual = (os.major(info.st_dev), os.minor(info.st_dev))
        if tuple(map(int, m.device.split(b":"))) != actual:
            raise Blocked("mount-device-mismatch")
        key = (info.st_dev, m.filesystem, m.source)
        identity = (m.id, m.device, m.root, m.point, m.filesystem, m.source, m.options, m.super_options)
        flags = set(m.options.split(b",")) | set(m.super_options.split(b","))
        supported = m.filesystem in (b"ext4", b"tmpfs") and b"rw" in flags and not flags.intersection(
            {b"ro", b"quota", b"usrquota", b"grpquota", b"prjquota"})
        return key, identity, m.filesystem, supported

    def _volume(self, path, fd, mounts):
        key, mount, fs, supported = self._mount(path, os.fstat(fd), mounts)
        v = os.fstatvfs(fd)
        if v.f_frsize <= 0 or v.f_bavail < 0:
            raise Blocked("unavailable-capacity")
        return Volume(key, (mount,), v.f_bavail * v.f_frsize,
                      v.f_favail if v.f_files > 0 else None, v.f_frsize, fs,
                      supported and not (v.f_flag & getattr(os, "ST_RDONLY", 1)))

    def _destination(self, relative, mounts, *, namespace=None, file_target=False):
        relative = path_bytes(relative)
        path = self.root + b"/" + relative
        if file_target:
            parent = os.path.dirname(relative)
            if parent:
                volume, missing = self._destination(parent, mounts, namespace=namespace)
            else:
                with _directory(self.root, observations=namespace) as root_fd:
                    volume, missing = self._volume(self.root, root_fd, mounts), ()
            if namespace is not None:
                namespace[path] = None
            if missing:
                return volume, missing
            with _directory(os.path.dirname(path)) as fd:
                try:
                    s = os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._require_absent(path, routes=namespace)
                    return volume, missing
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    raise Blocked("regular-output-leaf-required")
                child = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    if _observation(os.fstat(child)) != _observation(s):
                        raise Blocked("changed-output-leaf")
                    leaf_volume = self._volume(path, child, mounts)
                    parent_mount = self._mount(os.path.dirname(path), os.fstat(fd), mounts)[1]
                    leaf_mount = self._mount(path, s, mounts)[1]
                    if leaf_volume.key != volume.key or leaf_mount != parent_mount:
                        raise Blocked("mounted-output-leaf-unavailable")
                    if namespace is not None:
                        namespace[path] = _identity(s)
                finally:
                    os.close(child)
            return volume, missing
        # A future absent destination is resolved through its held real parent.
        missing = []
        while True:
            try:
                with _directory(path, observations=namespace) as fd:
                    return self._volume(path, fd, mounts), tuple(reversed(missing))
            except FileNotFoundError:
                self._require_absent(path, routes=namespace)
                if path == self.root:
                    raise Blocked("absent-stable-root")
                missing.append(path)
                if namespace is not None:
                    namespace[path] = None
                path = os.path.dirname(path)

    def _control(self, path):
        source = self._optional_source(path)
        if source is None:
            raise FileNotFoundError(path)
        if source.path in (self.root + b"/data/native/git-resources/journal.json",
                           self.root + b"/data/native-operations/state.json"):
            if stat.S_IMODE(source.observation[2]) != 0o600:
                raise Blocked("private-journal-required" if source.path.endswith(b"journal.json") else "private-state-file-required")
        if source.path == self.root + b"/data/native/git-resources/journal.json":
            try:
                lock = self.source(self.root + b"/data/native/git-resources/journal.lock")
            except FileNotFoundError as exc:
                raise Blocked("missing-stable-journal-lock") from exc
            if stat.S_IMODE(lock.observation[2]) != 0o600:
                raise Blocked("private-journal-lock-required")
        if source.size > self.limits.control_bytes:
            raise Blocked("control-observation-budget")
        with _directory(os.path.dirname(source.path)) as parent:
            fd = os.open(os.path.basename(source.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            try:
                data = bytearray()
                while True:
                    chunk = os.read(fd, min(65536, self.limits.control_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > self.limits.control_bytes:
                        raise Blocked("control-observation-budget")
                if _observation(os.fstat(fd)) != source.observation or self.source(path) != source:
                    raise Blocked("changed-control")
                return _control_json(bytes(data))
            finally:
                os.close(fd)

    def _read_regular(self, path, *, limit=None):
        """One held no-follow descriptor, raw bytes and stable route evidence."""
        limit = self.limits.control_bytes if limit is None else limit
        route = {}
        with _directory(os.path.dirname(path), observations=route) as parent:
            before = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise Blocked("bounded-single-link-regular-control-required")
            fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if _observation(os.fstat(fd)) != _observation(before):
                    raise Blocked("changed-control-descriptor")
                data = bytearray()
                while True:
                    chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise Blocked("control-observation-budget")
                if (len(data) != before.st_size or _observation(os.fstat(fd)) != _observation(before) or
                        _observation(os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)) != _observation(before)):
                    raise Blocked("changed-control-readback")
                return bytes(data), Source(path, _observation(before)), tuple(sorted(route.items()))
            finally:
                os.close(fd)

    def _check_marker(self, resource, rid, record, evidence):
        with _directory(self.root) as root_fd, _directory(self.root + b"/data/native/repository.git") as metadata_fd:
            root_info, meta_info = os.fstat(root_fd), os.fstat(metadata_fd)
            root_id = [root_info.st_dev, root_info.st_ino, stat.S_IMODE(root_info.st_mode)]
            meta_id = [meta_info.st_dev, meta_info.st_ino, stat.S_IMODE(meta_info.st_mode)]
        binding = hashlib.sha256(self.root + b"\0" + self.root + b"/data/native/repository.git" + repr((root_id, meta_id)).encode()).hexdigest()
        expected = {"resource": rid, "operation": evidence.operation_id, "owner": evidence.owner_id, "binding": binding}
        encoded = json.dumps(expected, sort_keys=True).encode()
        data, source, _ = self._read_regular(resource + b"/owner.json")
        observed = source.observation
        entry = record["entries"].get(base64.b64encode(b"owner.json").decode())
        if (data != encoded or len(data) != len(encoded) or stat.S_IMODE(observed[2]) != 0o600 or
                entry != [observed[0], observed[1], stat.S_IMODE(observed[2]), hashlib.sha256(data).hexdigest()]):
            raise Blocked("unproved-same-operation-resource-marker")
        with _directory(resource) as fd:
            info = os.fstat(fd)
            if record["root"] != [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]:
                raise Blocked("changed-resource-marker-root")

    def _check_worktree(self, resource, rid, record, evidence):
        # Observe canonical backlink bytes; never call NativeGit._admin_binding,
        # which syncs and fsyncs a recursively enumerated snapshot.
        admin = record["admin"]
        if admin is None:
            raise Blocked("unqualified-worktree-admin")
        path = self.root + b"/data/native/repository.git/worktrees/" + admin["name"].encode()
        with _directory(path) as fd:
            info = os.fstat(fd)
            if admin["identity"] != [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]:
                raise Blocked("replaced-worktree-admin")
        tree = resource + b"/tree"
        expected = ((tree + b"/.git", b"gitdir: " + path + b"\n", record["entries"], b"tree/.git"),
                    (path + b"/gitdir", tree + b"/.git\n", admin["entries"], b"gitdir"),
                    (path + b"/commondir", b"../..\n", admin["entries"], b"commondir"),
                    (path + b"/locked", ("ha-ops-native:" + rid + ":" + evidence.owner_id + ":" + evidence.operation_id).encode() + b"\n", admin["entries"], b"locked"))
        for target, encoded, entries, name in expected:
            data, source, _ = self._read_regular(target)
            observed = source.observation
            if (data != encoded or entries.get(base64.b64encode(name).decode()) !=
                    [observed[0], observed[1], stat.S_IMODE(observed[2]), hashlib.sha256(data).hexdigest()]):
                raise Blocked("unproved-worktree-backlink")

    @staticmethod
    def _state_binding(state):
        if state is None or state["active"] is None:
            return None
        worker = state["workers"][state["active"]]
        record = state["records"][worker["record_id"]]
        return (state["instance_id"], state["epoch"], state["generation"], state["active"],
                worker["owner"], worker["epoch"], worker["record_id"], worker["marker"],
                worker["binding"], record["generation"], record["identity_digest"])

    @staticmethod
    def _state_shape(state):
        # Canonical owner graph validation is still required separately.
        value = copy.deepcopy(state)
        for field in ("revision", "updated_at"):
            value.pop(field, None)
        if value["active"] is not None:
            worker = value["workers"][value["active"]]
            worker.pop("phase", None)
            value["records"][worker["record_id"]].pop("checkpoints", None)
        return _digest(value)

    def _state_envelope(self, state):
        from native_operations import canonical, NativeOperationStore
        candidates = [canonical(state)]
        if state["active"] is not None:
            worker = state["workers"][state["active"]]
            if worker["phase"] not in ("accepted", "effect_started"):
                raise Blocked("operation-not-authorized-for-normal-effects")
            if worker["phase"] == "accepted":
                future = copy.deepcopy(state)
                next_worker = future["workers"][future["active"]]
                next_worker["phase"] = "effect_started"
                NativeOperationStore._append_checkpoint(future["records"][next_worker["record_id"]], next_worker)
                future["revision"] += 1
                candidates.append(canonical(future))
        return max(map(len, candidates))

    def _qualify_local_objects(self, oids):
        """One explicit-OID batch; never an all-object or content traversal."""
        ordered = sorted(set(oids) - self._relevant_oids)
        if not ordered:
            return
        output = self._git(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                           input=b"".join(oid + b"\n" for oid in ordered))
        rows = output.splitlines()
        if len(rows) != len(ordered) or not output.endswith(b"\n"):
            raise Blocked("incomplete-packed-ref-object-proof")
        for oid, row in zip(ordered, rows):
            fields = row.split(b" ")
            if (len(fields) != 3 or fields[0] != oid or
                    fields[1] not in (b"blob", b"tree", b"commit", b"tag") or
                    not re.fullmatch(rb"[0-9]+", fields[2])):
                raise Blocked("unqualified-packed-ref-object")
        self._relevant_oids.update(ordered)

    def _packed_ref_structure(self, data, width):
        """One bounded initial grammar pass, without unrelated object walks.

        Follow NativeGit._packed_ref_storage's line/header/order/peeled rules;
        malformed rows can never disappear into a targeted absence lookup.
        Relevant object qualification is separate and retained in the context.
        """
        if len(data) > self.limits.git_bytes or (data and not data.endswith(b"\n")):
            raise Blocked("malformed-or-unbounded-packed-ref-storage")
        seen, previous, peeled, header, ordered = {}, None, False, False, False
        deadline = time.monotonic() + self.limits.seconds
        oid_pattern = rb"[0-9a-f]{" + str(width).encode() + rb"}"
        def oid(value):
            if not re.fullmatch(oid_pattern, value) or not value.strip(b"0"):
                raise Blocked("malformed-packed-ref-object-id")
            return value
        for count, line in enumerate(data.split(b"\n")[:-1], 1):
            if count > self.limits.entries or time.monotonic() >= deadline:
                raise Blocked("incomplete-packed-ref-grammar-budget")
            if line.startswith(b"# pack-refs with:") and previous is None and not seen:
                if header:
                    raise Blocked("duplicate-packed-ref-header")
                header, ordered = True, b"sorted" in line.split()
                continue
            if line.startswith(b"^"):
                if previous is None or peeled:
                    raise Blocked("malformed-packed-ref-peeled-record")
                seen[previous] = (seen[previous][0], oid(line[1:]))
                peeled = True
                continue
            fields = line.split(b" ")
            if len(fields) != 2 or not fields[1].startswith(b"refs/") or fields[1] in seen:
                raise Blocked("malformed-or-duplicate-packed-ref-storage")
            value, name = oid(fields[0]), fields[1]
            if ordered and previous is not None and name <= previous:
                raise Blocked("malformed-packed-ref-order")
            if b"\0" in name or self._git(["check-ref-format", os.fsdecode(name)]):
                raise Blocked("malformed-packed-ref-name")
            if time.monotonic() >= deadline:
                raise Blocked("incomplete-packed-ref-grammar-budget")
            seen[name] = (value, None)
            previous, peeled = name, False
        return seen

    def _compile_context(self, plan, snapshot, cache, mounts):
        routes, dependencies, mount_proofs, memberships = {}, {}, {}, {}
        initial_observations = dict(snapshot.observations)
        with _directory(self.root) as fd:
            if _identity(os.fstat(fd)) != snapshot.root_identity:
                raise Blocked("changed-snapshot-root")
        sources = [s for _, s in plan.evidence.selected] + [s for s, _ in plan.evidence.copies + plan.evidence.metadata]
        sources += [s for s in (plan.evidence.diff, plan.evidence.incoming, plan.evidence.incoming_index) if s is not None]
        # Reflog preimage is also a content/source dependency.
        sources += [proof[1] for _, proof in plan.output_files if proof is not None and proof[0] == "source-append"]
        for source in sources:
            with _directory(os.path.dirname(source.path), observations=routes):
                if self.source(source.path) != source:
                    raise Blocked("stale-source-evidence")
        metadata = self.root + b"/data/native/repository.git"
        def dependency(path):
            dependencies[path] = self._optional_source(path, initial_observations, routes)
        # Whole packed-ref contents are never consulted during phases. Any
        # changed packed file refuses; exact loose/HEAD routes remain bound.
        for relative in (b"config", b"HEAD", b"packed-refs", b"commondir", b"gitdir", b"config.worktree",
                         b"objects/info/alternates", b"objects/info/http-alternates", b"shallow", b"info/grafts"):
            dependency(metadata + b"/" + relative)
        if self._profile_source is not None and dependencies[metadata + b"/config"] != self._profile_source:
            raise Blocked("changed-git-config-during-projection")
        def direct_oid(data):
            pattern = (rb"[0-9a-f]{" + str(self._oid_width).encode() + rb"}\n?"
                       if self._oid_width is not None else rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\n?")
            if not re.fullmatch(pattern, data) or not data.rstrip(b"\n").strip(b"0"):
                raise Blocked("unsupported-symbolic-or-malformed-loose-ref")
            oid = data.rstrip(b"\n")
            if self._oid_width is not None and oid not in self._relevant_oids:
                kind = self._git(["cat-file", "-t", oid.decode("ascii")]).strip()
                if kind not in (b"blob", b"tree", b"commit", b"tag"):
                    raise Blocked("unqualified-direct-ref-object")
                self._relevant_oids.add(oid)
        packed_refs = {}
        if dependencies[metadata + b"/packed-refs"] is not None and self._oid_width is not None:
            if plan.kind=='publication' and self._publication_packed is not None:
                packed_data,source,packed_refs=self._publication_packed
            else:
                packed_data, source, _ = self._read_regular(metadata + b"/packed-refs", limit=self.limits.git_bytes)
                packed_refs = self._packed_ref_structure(packed_data, self._oid_width)
            if source != dependencies[metadata + b"/packed-refs"]:
                raise Blocked("changed-packed-ref-during-projection")
            # Validate all structure before selecting rows, including when a
            # relevant loose override exists. Object reads stay targeted.
            # The owner validates every packed/peeled object before any loose
            # override. Qualify that finite set once, then bind its storage.
            self._qualify_local_objects(oid for pair in packed_refs.values() for oid in pair if oid is not None)
        def direct_ref(ref):
            path = metadata + b"/" + ref
            dependency(path)
            if ref in packed_refs:
                for oid in packed_refs[ref]:
                    if oid is not None:
                        direct_oid(oid + b"\n")
            if dependencies[path] is not None:
                data, source, _ = self._read_regular(path, limit=66)
                if source != dependencies[path]:
                    raise Blocked("changed-ref-during-projection")
                # NativeGit accepts only direct loose OIDs, never aliases.
                direct_oid(data)
        if plan.evidence.ref is not None:
            direct_ref(plan.evidence.ref)
        if plan.kind == "publication":
            from native_publication import validate_intent
            intent = validate_intent(plan.evidence.publication.artifact)
            for pin in (intent.tree_pin, intent.commit_pin):
                direct_ref(pin.encode("ascii"))
        head = metadata + b"/HEAD"
        if dependencies[head] is not None:
            data, _, _ = self._read_regular(head)
            if data.startswith(b"ref: "):
                ref = path_bytes(data[5:].rstrip(b"\n"))
                if not ref.startswith(b"refs/") or data != b"ref: " + ref + b"\n":
                    raise Blocked("unsupported-symbolic-head")
                if self._oid_width is not None:
                    self._git(["check-ref-format", os.fsdecode(ref)])
                direct_ref(ref)
            else:
                direct_oid(data)
        # Relevant loose-object routes are explicit dependencies. If any
        # relevant object is packed, immutable initial pack/index storage is the
        # dependency; unrelated loose history is never revisited in phases.
        object_files = {path: Source(path, observed) for path, observed in snapshot.observations
                        if observed is not None and stat.S_ISREG(observed[2]) and
                        beneath(path, metadata + b"/objects")}
        loose = {metadata + b"/objects/" + oid[:2] + b"/" + oid[2:] for oid in self._relevant_oids}
        future_objects = {path for path,proof in plan.output_files if proof is not None and proof[0] == "git-object"}
        packed_needed = bool(loose - object_files.keys() - future_objects)
        for path, source in object_files.items():
            if (plan.kind == "repack" or path in loose or
                    (packed_needed and beneath(path, metadata + b"/objects/pack"))):
                if self.source(path) != source:
                    raise Blocked("changed-initial-object-storage")
                dependencies[path] = source
                with _directory(os.path.dirname(path), observations=routes):
                    pass
        for path in loose - object_files.keys():
            dependency(path)  # loose override creation is a relevant route change
        if packed_needed and not any(beneath(path, metadata + b"/objects/pack") for path in object_files):
            raise Blocked("unqualified-local-object-storage")
        if plan.kind == "repack":
            # A complete all-object recipe includes unreachable objects. Bind
            # the initial membership fingerprints of objects/, pack/ and each
            # existing fanout. Targeted fstat detects additions/removals without
            # any phase directory enumeration; selection ignores unrelated churn.
            for path, observed in snapshot.observations:
                relative = path[len(metadata + b"/objects/"):]
                if (observed is not None and stat.S_ISDIR(observed[2]) and
                        (path == metadata + b"/objects" or relative == b"pack" or
                         re.fullmatch(rb"[0-9a-f]{2}", relative))):
                    with _directory(path, observations=routes) as fd:
                        if _observation(os.fstat(fd)) != observed:
                            raise Blocked("changed-object-membership-during-projection")
                    memberships[path] = observed
        owner_locks = {}
        for relative in (b"data/native-operations/lock", b"data/native/git-resources/journal.lock"):
            path = self.root + b"/" + relative
            initial = initial_observations.get(path)
            try:
                source = self._optional_source(path, initial_observations, routes)
            except Blocked as exc:
                raise Blocked("unverifiable-existing-owner-lock-presence:" + str(exc)) from exc
            # Presence is compared before any prospective-lock classification.
            # A missing known leaf or parent/control cannot erase a snapshot ID.
            if (initial is None) != (source is None):
                raise Blocked("changed-owner-lock-presence-during-projection")
            if source is None:
                owner_locks[relative] = None
                continue
            if stat.S_IMODE(source.observation[2]) != 0o600:
                raise Blocked("private-existing-owner-lock-required")
            if initial[:3] != source.observation[:3]:
                raise Blocked("changed-owner-lock-during-projection")
            owner_locks[relative] = source.observation[:3]
            with _directory(os.path.dirname(path), observations=routes):
                pass
        for path, identity in plan.namespace:
            current = path
            while not _optional_stat(current):
                current = os.path.dirname(current)
            info = os.stat(current, follow_symlinks=False)
            mount_proofs[path] = self._mount(current, info, mounts)[1]
        state = None
        try:
            state = self._control(self.root + b"/data/native-operations/state.json")
            self._validate_operation_state(state, cache)
        except FileNotFoundError as exc:
            self._require_absent(self.root + b"/data/native-operations/state.json", initial_observations, routes)
            state = None
        if state is not None:
            # Include initially verified artifact parent routes in the context.
            for token in cache:
                routes.update(token[1])
        binding = self._state_binding(state)
        if binding is not None and binding[3:5] != (plan.evidence.operation_id, plan.evidence.owner_id):
            raise Blocked("competing-operation-state")
        selection = plan.kind in ("initial_selection", "scope", "save", "migration", "candidate")
        counts = {"index": int(selection), "spool": len(plan.evidence.selected) + int(plan.kind == "candidate") + int(plan.evidence.candidate_rules is not None) if selection else 0, "worktree": int(selection and plan.evidence.materialize),
                  'transport':int(bool(plan.network_cap))}
        journal = None
        try:
            journal = self._control(self.root + b"/data/native/git-resources/journal.json")
            self._validate_resource_journal(journal)
        except FileNotFoundError:
            self._require_absent(self.root + b"/data/native/git-resources/journal.json", initial_observations, routes)
        if journal:
            if not selection and not plan.network_cap:
                self._resource_bound = len(json.dumps(journal, sort_keys=True, separators=(",", ":")).encode())
                self._resource_entry_bound = max([3] + [len(r["entries"]) for r in journal["records"].values()])
            for record in journal["records"].values():
                counts[record["kind"]] += 1
        prospective = None
        publication_states = ()
        if plan.evidence.scope_preparation is not None:
            prepared=plan.evidence.scope_preparation; prepared.check()
            from native_operations import _parse
            prospective=_parse(prepared.state)
            publication_states=(prepared.state,)
            state_limit=max(len(prepared.initial),len(prepared.state))
        elif plan.kind == "publication":
            prepared = plan.evidence.publication
            prepared.check()
            publication_states = tuple(value for _, value in prepared.states)
            prospective = prepared.state("accepted")
            state_limit = max([len(value) for value in publication_states] +
                              ([len(__import__("native_operations").canonical(state))] if state is not None else [0]))
        else:
            for control in plan.evidence.controls:
                value = _control_json(control.payload)
                if isinstance(value, dict) and "workers" in value and value["active"] is not None:
                    if prospective is not None and prospective != value:
                        raise Blocked("multiple-prospective-worker-envelopes")
                    prospective = value
            state_limit = max(self._state_envelope(state) if state is not None else 0,
                              self._state_envelope(prospective) if prospective is not None else 0)
        return _Context(snapshot, tuple(cache.items()), tuple(sources), tuple(sorted(routes.items())),
                        tuple(sorted(dependencies.items())), tuple(sorted(memberships.items())), tuple(sorted(owner_locks.items())), tuple(sorted(mount_proofs.items())), tuple(sorted(counts.items())),
                        self._resource_bound, self._resource_entry_bound,plan.network_cap,binding,
                        state_limit,
                        self._state_shape(state) if state is not None else None,
                        tuple((rid, _digest(record)) for rid, record in journal["records"].items()) if journal else (),
                        self._index_bound, tuple(sorted([s.size for _, s in plan.evidence.selected] +
                            ([len(plan.evidence.candidate_manifest)] if plan.kind == "candidate" else []) +
                            ([len(plan.evidence.candidate_rules)] if plan.evidence.candidate_rules is not None else []))),
                        tuple(sorted(self._materialized_sizes.items())),
                        self._state_binding(prospective) if prospective is not None else None,
                        self._state_shape(prospective) if prospective is not None else None, publication_states)

    def _category(self, path):
        relative = path[len(self.root) + 1:]
        if beneath(relative, b"data/native/repository.git"):
            return "git"
        if beneath(relative, b"data/native/git-resources"):
            return "staging"
        if beneath(relative, b"data/native/diffs") or beneath(relative, b"data/native-operations/artifacts"):
            return "diff"
        if beneath(relative, b"data/native/recovery"):
            return "recovery"
        if any(beneath(relative, p) for p in self.layout.legacy):
            return "staging"
        return "service"

    def _scan(self, mounts, deadline):
        observations, issues, volumes, objects = {}, [], {}, {}
        work = 0

        def issue(reason):
            if reason not in issues:
                issues.append(reason)

        def merge_volume(v):
            old = volumes.get(v.key)
            if old:
                if old.quantum != v.quantum:
                    raise Blocked("ambiguous-allocation-quantum")
                v = replace(v, mounts=tuple(sorted(set(old.mounts + v.mounts), key=repr)),
                            available=min(old.available, v.available),
                            inodes=min(old.inodes, v.inodes) if old.inodes is not None and v.inodes is not None else None,
                            supported=old.supported and v.supported)
            volumes[v.key] = v

        def observe(path, s, category, volume):
            key = (s.st_dev, s.st_ino)
            value = _observation(s)
            observations[path] = value
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                issue("unsafe-private-object:" + repr(path))
                return
            if not hasattr(s, "st_blocks"):
                issue("unavailable-allocated-blocks")
                return
            if key not in objects:
                objects[key] = [category, s.st_size if stat.S_ISREG(s.st_mode) else 0,
                                512 * s.st_blocks, volume, s.st_nlink, set(), path]
            elif CATEGORIES.index(category) < CATEGORIES.index(objects[key][0]):
                objects[key][0] = category
            objects[key][5].add(path)

        try:
            with _directory(self.root) as fd:
                root_identity = _identity(os.fstat(fd))
            pending = [(self.root + b"/" + p, None) for p in sorted(set(self.layout.anchors))]
            visited_paths = set()
            while pending:
                if time.monotonic() > deadline or work >= self.limits.entries:
                    issue("incomplete-scan-work-budget")
                    break
                path, forced = pending.pop()
                if path in visited_paths:
                    continue
                visited_paths.add(path)
                work += 1
                try:
                    with _directory(os.path.dirname(path)) as parent:
                        try:
                            s = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            self._require_absent(path)
                            # Missing optional anchors have a verified real ancestor.
                            if path in (self.root + b"/" + p for p in self.layout.anchors):
                                v, _ = self._destination(path[len(self.root) + 1:], mounts)
                                merge_volume(v)
                                observations[path] = None
                                continue
                            raise
                        if stat.S_ISDIR(s.st_mode):
                            fd = os.open(os.path.basename(path), _DIR_FLAGS, dir_fd=parent)
                            try:
                                if _observation(os.fstat(fd)) != _observation(s):
                                    raise Blocked("changed-private-directory")
                                v = self._volume(path, fd, mounts)
                                merge_volume(v)
                                observe(path, s, forced or self._category(path), v.key)
                                with os.scandir(fd) as names:
                                    for entry in names:
                                        work += 1
                                        if work > self.limits.entries or time.monotonic() > deadline:
                                            raise Blocked("incomplete-scan-work-budget")
                                        pending.append((path + b"/" + os.fsencode(entry.name), forced))
                                if _observation(os.fstat(fd)) != _observation(s):
                                    raise Blocked("changed-private-membership")
                            finally:
                                os.close(fd)
                        else:
                            if stat.S_ISREG(s.st_mode):
                                leaf = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                                try:
                                    if _observation(os.fstat(leaf)) != _observation(s):
                                        raise Blocked("changed-private-leaf-descriptor")
                                    v = self._volume(path, leaf, mounts)
                                    if _observation(os.fstat(leaf)) != _observation(s):
                                        raise Blocked("changed-private-leaf")
                                finally:
                                    os.close(leaf)
                            else:
                                v = self._volume(os.path.dirname(path), parent, mounts)
                            merge_volume(v)
                            observe(path, s, forced or self._category(path), v.key)
                            if _observation(os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)) != _observation(s):
                                raise Blocked("changed-private-leaf")
                except (OSError, ValueError) as exc:
                    issue(str(exc) + ":" + repr(path))
            for category, logical, allocated, key, links, names, path in objects.values():
                if stat.S_ISREG(observations[path][2]) and links != len(names):
                    issue("uncertain-cross-boundary-hardlink:" + repr(path))
                v = volumes[key]
                control = logical if os.path.basename(path) in (b"state.json", b"journal.json", b"manifest.json") else 0
                volumes[key] = replace(v, allocated=v.allocated + allocated,
                                       largest_control=max(v.largest_control, control))
            rows = tuple((c, sum(o[1] for o in objects.values() if o[0] == c),
                          sum(o[2] for o in objects.values() if o[0] == c)) for c in CATEGORIES)
            return root_identity, rows, tuple(sorted(volumes.values(), key=lambda v: repr(v.key))), tuple(sorted(observations.items())), tuple(issues)
        except (OSError, ValueError) as exc:
            issue(str(exc))
            return (), tuple((c, 0, 0) for c in CATEGORIES), (), tuple(sorted(observations.items())), tuple(issues)

    def snapshot(self):
        if self._active_operation is not None:
            raise Blocked("initial-calculation-already-held")
        start, wall = time.monotonic(), time.time()
        issues = []
        try:
            mounts = linux_mounts() if os.path.exists("/proc/self/mountinfo") else None
        except (OSError, ValueError) as exc:
            return Snapshot(wall, time.monotonic() - start, (), (), (), (str(exc),), "", self.layout_digest, ())
        first = self._scan(mounts, start + self.limits.seconds)
        second = self._scan(mounts, start + self.limits.seconds)
        issues.extend(first[4])
        issues.extend(i for i in second[4] if i not in issues)
        if first[0] != second[0] or first[3] != second[3]:
            issues.append("changed-two-pass-snapshot")
        if mounts is not None:
            try:
                if linux_mounts() != mounts:
                    issues.append("changed-mount-topology")
            except (OSError, ValueError) as exc:
                issues.append(str(exc))
        digest = _digest((second[:4], self.layout_digest))
        result = Snapshot(wall, time.monotonic() - start, second[1], second[2], second[3],
                          tuple(issues), digest, self.layout_digest, second[0])
        self._snapshots[id(result)] = result
        return result

    def _git(self, args, *, input=None):
        """Bounded streamed read-only Git observations; no NativeGit accounting."""
        metadata = self.root + b"/data/native/repository.git"
        with _directory(metadata):
            pass
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        # Pin the same semantic and side-effect profile as NativeGit._argv.
        env.update(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
        options = ["core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.attributesFile=" + os.devnull,
                   "core.untrackedCache=false", "core.splitIndex=false", "gc.auto=0", "maintenance.auto=false",
                   "diff.external=", "protocol.allow=never", "submodule.recurse=false", "core.sparseCheckout=false",
                   "index.version=2", "index.threads=1", "index.sparse=false",
                   "core.compression=-1", "core.looseCompression=-1"]
        cmd = ["git", "--no-pager", "--literal-pathspecs", "--git-dir=" + os.fsdecode(metadata),
               "--work-tree=" + os.fsdecode(self.root)]
        for option in options:
            cmd.extend(("-c", option))
        cmd.extend(args)
        if input is not None and len(input) > self.limits.git_bytes:
            raise Blocked("incomplete-git-input-budget")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, env=env)
        selector = selectors.DefaultSelector()
        for stream in (proc.stdout, proc.stderr):
            selector.register(stream, selectors.EVENT_READ)
        offset = 0
        if input is not None:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE)
        data, errors = bytearray(), bytearray()
        deadline = time.monotonic() + self.limits.seconds
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise Blocked("incomplete-git-deadline")
                for key, _ in selector.select(min(.1, max(.001, deadline - time.monotonic()))):
                    if key.fileobj is proc.stdin:
                        if offset == len(input):
                            selector.unregister(proc.stdin)
                            proc.stdin.close()
                        else:
                            offset += os.write(key.fd, input[offset:offset + 4096])
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        target = data if key.fileobj is proc.stdout else errors
                        target.extend(chunk)
                        if len(data) + len(errors) > self.limits.git_bytes:
                            raise Blocked("incomplete-git-observation-budget")
            if proc.wait(timeout=max(.001, deadline - time.monotonic())) != 0 or errors:
                raise Blocked("git-observation-failed")
            return bytes(data)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            selector.close()
            proc.stdout.close()
            proc.stderr.close()
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()

    def _profile(self):
        before = self.source(self.root + b"/data/native/repository.git/config")
        config = self._git(["config", "--null", "--local", "--no-includes", "--list"])
        if self.source(before.path) != before:
            raise Blocked("changed-git-config-during-observation")
        self._profile_source = before
        width = 20
        permitted = {b"core.repositoryformatversion", b"core.filemode", b"core.bare",
                     b"core.logallrefupdates", b"core.ignorecase", b"core.precomposeunicode",
                     b"extensions.objectformat"}
        for field in config.split(b"\0"):
            if not field:
                continue
            key, _, value = field.partition(b"\n")
            routing = re.fullmatch(rb"remote\.[^.]+\.(url|fetch|pushurl)", key.lower()) or re.fullmatch(
                rb"branch\.[^.]+\.(remote|merge|pushremote)", key.lower())
            if key.lower() == b"core.worktree":
                route = os.path.abspath(os.path.join(self.root + b"/data/native/repository.git", value))
                if route != self.root:
                    raise Blocked("unsupported-git-worktree-routing")
                continue  # Both NativeGit and accounting pin this exact root.
            if key.lower() not in permitted and not routing:
                raise Blocked("unsupported-git-config:" + repr(key))
            if key.lower() == b"extensions.objectformat":
                if value not in (b"sha1", b"sha256"):
                    raise Blocked("unsupported-object-format")
                width = 32 if value == b"sha256" else 20
        # Alternates/shallow/promisor/grafts can hide objects or change routing.
        for route in (b"commondir", b"gitdir", b"config.worktree", b"objects/info/alternates",
                      b"objects/info/http-alternates", b"shallow", b"info/grafts"):
            if _optional_stat(self.root + b"/data/native/repository.git/" + route):
                raise Blocked("unsupported-git-routing")
        self._oid_width = width * 2
        return width, _digest(config)

    def _entries(self, tree, width):
        if tree is None:
            return []
        if not re.fullmatch("[0-9a-f]{" + str(width * 2) + "}", tree):
            raise Blocked("invalid-base-tree")
        kind = self._git(["cat-file", "-t", tree]).strip()
        if kind not in (b"tree", b"commit"):
            raise Blocked("base-must-be-local-tree-or-commit")
        tree_oid = self._git(["rev-parse", tree + "^{tree}"]).strip()
        if not re.fullmatch(rb"[0-9a-f]{" + str(width * 2).encode() + rb"}", tree_oid):
            raise Blocked("invalid-resolved-base-tree")
        self._relevant_oids.update((tree.encode(), tree_oid))
        data = self._git(["ls-tree", "-r", "-t", "-z", "--full-tree", tree])
        entries, materialized = [], {}
        proofs = {tree.encode(): kind, tree_oid: b"tree"}
        for row in data.split(b"\0"):
            if not row:
                continue
            header, sep, path = row.partition(b"\t")
            fields = header.split(b" ")
            if not sep or len(fields) != 3 or fields[0] not in (b"100644", b"100755", b"040000") or fields[1] not in (b"blob", b"tree"):
                raise Blocked("unsupported-base-tree-entry")
            path_bytes(path)
            if len(fields[2]) != width * 2:
                raise Blocked("malformed-base-tree-object")
            self._relevant_oids.add(fields[2])
            if fields[2] in proofs and proofs[fields[2]] != fields[1]:
                raise Blocked("conflicting-base-object-kind")
            proofs[fields[2]] = fields[1]
            if fields[0] == b"040000":
                if fields[1] != b"tree":
                    raise Blocked("unsupported-base-tree-entry")
                continue
            if fields[1] != b"blob":
                raise Blocked("unsupported-base-tree-entry")
            entries.append(path)
            materialized[path] = fields[2]
        ordered = sorted(proofs)
        output = self._git(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                           input=b"".join(oid + b"\n" for oid in ordered))
        rows = output.splitlines()
        if len(rows) != len(ordered) or not output.endswith(b"\n"):
            raise Blocked("incomplete-base-object-proof")
        sizes = {}
        for oid, row in zip(ordered, rows):
            fields = row.split(b" ")
            if len(fields) != 3 or fields[:2] != [oid, proofs[oid]] or not re.fullmatch(rb"[0-9]+", fields[2]):
                raise Blocked("invalid-base-object-proof")
            sizes[oid] = int(fields[2])
        if self._materialize_requested:
            self._materialized_sizes.update((path, sizes[oid]) for path, oid in materialized.items())
        if len(set(entries)) != len(entries):
            raise Blocked("duplicate-base-entry")
        return entries

    def _objects(self, width):
        data = self._git(["cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)"])
        if data and not data.endswith(b"\n"):
            raise Blocked("truncated-object-observation")
        result = []
        seen = set()
        for row in data.splitlines():
            fields = row.split(b" ")
            if (len(fields) != 3 or not re.fullmatch(rb"[0-9a-f]{" + str(width * 2).encode() + rb"}", fields[0])
                    or fields[1] not in (b"blob", b"tree", b"commit", b"tag")
                    or not re.fullmatch(rb"[0-9]+", fields[2]) or fields[0] in seen):
                raise Blocked("malformed-object-observation")
            seen.add(fields[0])
            result.append((fields[0], fields[1], int(fields[2])))
        return tuple(result)

    def project(self, kind, snapshot, evidence, *, previous=None):
        if self._active_operation is not None:
            raise Blocked("initial-calculation-already-held")
        if self._snapshots.get(id(snapshot)) is not snapshot:
            raise Blocked("unissued-or-recreated-snapshot")
        if previous is not None and (self._snapshots.get(id(previous)) is not previous or
                not previous.complete or previous.layout_digest != snapshot.layout_digest or
                previous.root_identity != snapshot.root_identity or
                tuple((v.key, v.mounts, v.quantum, v.filesystem) for v in previous.volumes) !=
                tuple((v.key, v.mounts, v.quantum, v.filesystem) for v in snapshot.volumes)):
            raise Blocked("incomparable-growth-snapshot")
        artifact_cache = {}
        scope_preparation=evidence.scope_preparation if isinstance(evidence,Evidence) else None
        if scope_preparation is not None:
            from native_operations import PreparedScope
            if kind not in ("candidate","scope_control") or type(scope_preparation) is not PreparedScope:
                raise Blocked("issued-scope-candidate-recipe-required")
            scope_preparation.check()
            if (evidence.operation_id,evidence.owner_id)!=(scope_preparation.operation_id,scope_preparation.owner_id):
                raise Blocked("scope-owner-binding-required")
        publication = evidence.publication if isinstance(evidence, Evidence) else None
        if kind == "publication":
            from native_operations import PreparedPublication
            if type(publication) is not PreparedPublication:
                raise Blocked("issued-publication-preparation-required")
            publication.check()
            if (publication.operation_id, publication.owner_id) != (evidence.operation_id, evidence.owner_id):
                raise Blocked("publication-owner-binding-required")
        elif publication is not None:
            raise Blocked("publication-recipe-required")
        if not isinstance(kind, str):
            raise Blocked("immutable-operation-kind-required")
        if not isinstance(evidence, Evidence) or not evidence.operation_id or not evidence.owner_id:
            raise Blocked("operation-bound-evidence-required")
        if snapshot.layout_digest != self.layout_digest:
            raise Blocked("different-layout-snapshot")
        unknown = list(snapshot.issues)
        allocations, volumes, provenance = [], {v.key: v for v in snapshot.volumes}, [("root", snapshot.root_identity)]
        generated_controls = {}
        self._resource_bound = 0
        self._resource_entry_bound = 3
        self._index_bound = 0
        self._materialized_sizes = {}
        self._profile_source = None
        self._publication_packed = None
        network_cap=0
        self._oid_width = None
        self._relevant_oids = set()
        try:
            state = self._control(self.root + b"/data/native-operations/state.json")
            self._validate_operation_state(state, artifact_cache)
            state_bound = len(__import__("native_operations").canonical(state)) if kind == "publication" else self._state_envelope(state)
        except FileNotFoundError as exc:
            state_bound = 0
            try:
                self._require_absent(self.root + b"/data/native-operations/state.json", dict(snapshot.observations))
            except Blocked as failure:
                unknown.append("unverifiable-competing-operation-state:" + str(failure))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state_bound = 0
            unknown.append("unverifiable-competing-operation-state:" + str(exc))
        namespace, future_directories, output_files = {}, set(), {}
        mounts = linux_mounts() if os.path.exists("/proc/self/mountinfo") else None
        self._materialize_requested = evidence.materialize
        if evidence.executor != "git-native-disk-v1":
            unknown.append("unsupported-executor-profile")

        def source(s):
            if not isinstance(s, Source) or self.source(s.path) != s:
                raise Blocked("stale-source-evidence")
            provenance.append((s.path, s.observation))
            return s.size

        def add(target, size, name, stage="allocate", directory=False, file_target=False, proof=None):
            if isinstance(target, Target):
                file_target = target.kind == "file"
                target = target.path
            if file_target:
                output_files[self.root + b"/" + target] = proof
            v, missing = self._destination(target, mounts, namespace=namespace, file_target=file_target)
            future_directories.update(missing)
            old = volumes.get(v.key)
            if old:
                v = replace(v, available=min(v.available, old.available), allocated=old.allocated,
                            largest_control=old.largest_control,
                            mounts=tuple(sorted(set(v.mounts + old.mounts), key=repr)))
            volumes[v.key] = v
            provenance.append(("destination", target, v.key, v.mounts))
            if not v.supported:
                unknown.append("unsupported-destination-allocation-profile:" + repr(v.key))
            q = v.quantum
            data = _round(size, q)
            # Directory entries and extent metadata are operation demand, not reserve.
            # Ext4: pessimistically one metadata block per data block, plus two
            # extent-tree/admin blocks. tmpfs has no on-disk extent tree.
            metadata = data + 2 * q if v.filesystem == b"ext4" and not directory else 0
            allocations.append(Allocation(v.key, stage, name, size,
                                          data + metadata + q, 1))
            for path in missing:
                allocations.append(Allocation(v.key, stage, "parent:" + repr(path), 0, q, 1))
            return v

        try:
            if not snapshot.complete:
                raise Blocked("incomplete-accounting-no-git-content-observation")
            if state_bound:
                v = add(b"data/native-operations", state_bound, "full-prospective-operation-state")
                generated_controls[v.key] = state_bound
            if not evidence.controls and kind not in ("candidate","scope_control"):
                raise Blocked("exact-prospective-control-envelope-required")
            if scope_preparation is not None:
                from native_operations import _parse, _publication_graph
                prospective=_parse(scope_preparation.state)
                _publication_graph(scope_preparation.store,prospective,None,scope_preparation.artifact,
                    historical={r['artifact']:r['artifact_size'] for r in _parse(scope_preparation.initial)['records'].values()})
                for target,payload in ((b'data/native-operations/state.json',scope_preparation.state),
                        (b'data/native-operations/artifacts/'+hashlib.sha256(scope_preparation.artifact).hexdigest().encode()+b'.diff',scope_preparation.artifact),
                        (b'data/native-operations/lock',b'')):
                    add(Target(target,'file'),len(payload),'exact-scope-control',proof=('bytes',payload))
                v=add(b'data/native-operations',len(scope_preparation.state),'full-scope-state-overlap')
                generated_controls[v.key]=max(generated_controls.get(v.key,0),len(scope_preparation.state))
                add(b'data/native-operations/artifacts',len(scope_preparation.artifact),'full-scope-artifact-overlap')
                from native_publication import save
                ContentSink=save.ContentSink
                for ref,producer in scope_preparation.payloads:
                    sink=ContentSink(ref.size,ref.object_format);producer(sink)
                    if sink.reference()!=ref:raise Blocked('scope-source-receipt-changed')
                    target=b'data/native-operations/artifacts/'+ref.key.encode()+b'.blob'
                    add(Target(target,'file'),ref.size,'scope-immutable-content',proof=('scope-blob',ref))
                    add(b'data/native-operations/artifacts',ref.size,'scope-content-temp-overlap')

            for control in evidence.controls:
                if not isinstance(control, Control) or not isinstance(control.payload, bytes):
                    raise Blocked("exact-control-serialization-required")
                if kind == "publication":
                    self._bind_publication_control(control, evidence, artifact_cache)
                else:
                    self._bind_control(control, evidence, artifact_cache)
                value = _control_json(control.payload)
                if isinstance(value, dict) and "workers" in value:
                    v = add(b"data/native-operations", (len(control.payload) if kind == "publication" else self._state_envelope(value)), "full-prospective-worker-state")
                    generated_controls[v.key] = max(generated_controls.get(v.key, 0), (len(control.payload) if kind == "publication" else self._state_envelope(value)))
                add(control.target, len(control.payload), "full-pending-control", proof=("bytes", control.payload))
                provenance.append(("full-control", control.target, control.operation_id, control.owner_id, hashlib.sha256(control.payload).hexdigest()))
            if kind in ("initial_selection", "scope", "save", "migration", "candidate"):
                width, config = self._profile()
                entries = set(self._entries(evidence.base_tree, width))
                provenance.append(("git-config", config, "base-tree", evidence.base_tree))
                initial_entries = set(entries)
                if kind == "candidate":
                    if (evidence.base_tree is None or evidence.candidate_manifest is None or
                            evidence.controls or evidence.materialize or evidence.commit is not None or
                            evidence.ref is not None or evidence.reflog_entry is not None or
                            evidence.copies or evidence.metadata or evidence.replacements or evidence.diff or
                            evidence.incoming or evidence.incoming_index):
                        raise Blocked("exact-candidate-only-recipe-required")
                    removed = set(evidence.candidate_removals)
                    if not removed <= entries or removed & {p for p, _ in evidence.selected}:
                        raise Blocked("qualified-candidate-removals-required")
                    if scope_preparation is None:
                        for path in removed:
                            self._require_absent(self.root + b"/" + path)
                    elif evidence.candidate_rules is None:
                        raise Blocked("exact-generated-scope-rules-required")
                    from native_metadata import MANIFEST_PATH, decode, encode
                    from native_inventory import Policy
                    policy = Policy(repository_files=(MANIFEST_PATH,))
                    manifest = decode(evidence.candidate_manifest, width=width * 2, policy=policy)
                    if encode(manifest, width=width * 2, policy=policy) != evidence.candidate_manifest:
                        raise Blocked("canonical-candidate-manifest-required")
                    final_entries = (entries - removed) | {p for p, _ in evidence.selected}
                    if scope_preparation is not None:final_entries.add(b".gitignore")
                    if final_entries != set(evidence.candidate_final):
                        raise Blocked("exact-candidate-final-layout-required")
                    if {r.path for r in manifest.entries if r.kind == "file"} != final_entries - {MANIFEST_PATH, b".gitignore"}:
                        raise Blocked("candidate-manifest-layout-mismatch")
                    entries = final_entries
                    size = len(evidence.candidate_manifest)
                    add(b"data/native/git-resources", size, "generated-manifest-spool")
                    add(b"data/native/repository.git/objects", _loose("blob", size), "generated-manifest-blob")
                    add(b"data/native/repository.git/objects", 0, "generated-manifest-fanout", directory=True)
                    if evidence.candidate_rules is not None:
                        if scope_preparation is None: raise Blocked('scope-rules-require-issued-recipe')
                        size=len(evidence.candidate_rules)
                        add(b'data/native/git-resources',size,'generated-scope-rules-spool')
                        add(b'data/native/repository.git/objects',_loose('blob',size),'generated-scope-rules-blob')
                        add(b'data/native/repository.git/objects',0,'generated-scope-rules-fanout',directory=True)
                    provenance.append(("candidate-layouts", tuple(sorted(initial_entries)),
                                       tuple(sorted(initial_entries - removed)), tuple(sorted(entries)),
                                       hashlib.sha256(evidence.candidate_manifest).hexdigest()))
                for path, s in evidence.selected:
                    path_bytes(path)
                    size = source(s)
                    entries.add(path)
                    add(b"data/native/git-resources", size, "stream-spool:" + repr(path))
                    add(b"data/native/repository.git/objects", _loose("blob", size), "loose-blob:" + repr(path))
                    add(b"data/native/repository.git/objects", 0, "fanout:" + repr(path), directory=True)
                    if evidence.materialize:
                        self._materialized_sizes[path] = size
                        add(b"data/native/git-resources", size, "materialization:" + repr(path))
                if evidence.materialize:
                    selected_paths = {path for path, _ in evidence.selected}
                    for path, size in self._materialized_sizes.items():
                        if path not in selected_paths:
                            add(b"data/native/git-resources", size, "base-materialization:" + repr(path))
                    parents = {b"/".join(path.split(b"/")[:n]) for path in self._materialized_sizes for n in range(1, len(path.split(b"/")))}
                    for parent in parents:
                        add(b"data/native/git-resources", 0, "materialization-parent:" + repr(parent), directory=True)
                # Known NativeGit resource outputs must not disappear into an
                # arbitrary per-file allowance. Serialize its complete journal
                # shape, with Linux uint64 bounds for future inode identities.
                with _directory(self.root) as root_fd, _directory(self.root + b"/data/native/repository.git") as meta_fd:
                    ri, mi = os.fstat(root_fd), os.fstat(meta_fd)
                    root_id = [ri.st_dev, ri.st_ino, stat.S_IMODE(ri.st_mode)]
                    meta_id = [mi.st_dev, mi.st_ino, stat.S_IMODE(mi.st_mode)]
                binding = hashlib.sha256(self.root + b"\0" + self.root + b"/data/native/repository.git" + repr((root_id, meta_id)).encode()).hexdigest()
                try:
                    journal = self._control(self.root + b"/data/native/git-resources/journal.json")
                    self._validate_resource_journal(journal)
                except FileNotFoundError:
                    self._require_absent(self.root + b"/data/native/git-resources/journal.json", dict(snapshot.observations))
                    journal = {"schema": 1, "root": root_id, "metadata": meta_id,
                               "storage": [ri.st_dev, 2**64 - 1, 0o700], "records": {}}
                for i, resource_kind in enumerate(["spool"] * (len(evidence.selected) + int(kind == "candidate") + int(evidence.candidate_rules is not None)) + ["index"]):
                    hex_id = hashlib.sha256((evidence.operation_id + ":disk:" + str(i)).encode()).hexdigest()[:32]
                    rid = "-".join((hex_id[:8], hex_id[8:12], hex_id[12:16], hex_id[16:20], hex_id[20:]))
                    marker = {"resource": rid, "operation": evidence.operation_id, "owner": evidence.owner_id, "binding": binding}
                    encoded = json.dumps(marker, sort_keys=True).encode()
                    add(b"data/native/git-resources", len(encoded), "resource-owner-marker:" + rid)
                    add(b"data/native/git-resources", 0, "resource-lease:" + rid)
                    add(b"data/native/git-resources", 0, "resource-directory:" + rid, directory=True)
                    file_record = [ri.st_dev, 2**64 - 1, 0o600, "f" * 64]
                    resource_entries = {base64.b64encode(b"owner.json").decode(): file_record,
                                        base64.b64encode(resource_kind.encode()).decode(): file_record}
                    if resource_kind == "index":
                        resource_entries[base64.b64encode(b"index.lock").decode()] = file_record
                    journal["records"][rid] = {"operation": evidence.operation_id, "owner": evidence.owner_id,
                        "kind": resource_kind, "phase": "allocated", "root": [ri.st_dev, 2**64 - 1, 0o700],
                        "lease": [ri.st_dev, 2**64 - 1, 0o600], "entries": resource_entries,
                        "admin": None, "errors": []}
                # A materialized worktree has bounded tree/admin entries and
                # exact UUID/backlink/control shapes; retain its full envelope.
                if evidence.materialize:
                    rid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
                    tree_entries = {base64.b64encode(b"owner.json").decode(): file_record}
                    for path in sorted(entries):
                        tree_entries[base64.b64encode(b"tree/" + path).decode()] = file_record
                        for n in range(1, len(path.split(b"/"))):
                            name = b"tree/" + b"/".join(path.split(b"/")[:n])
                            tree_entries[base64.b64encode(name).decode()] = [ri.st_dev, 2**64 - 1, 0o700, "directory"]
                    for name in (b"tree", b"tree/.git"):
                        tree_entries[base64.b64encode(name).decode()] = file_record
                    admin_entries = {base64.b64encode(name).decode(): file_record for name in
                                     (b"HEAD", b"index", b"gitdir", b"commondir", b"locked", b"logs", b"logs/HEAD", b"ORIG_HEAD")}
                    admin_entries[base64.b64encode(b"refs").decode()] = [ri.st_dev, 2**64 - 1, 0o700, "directory"]
                    journal["records"][rid] = {"operation": evidence.operation_id, "owner": evidence.owner_id,
                        "kind": "worktree", "phase": "creating", "root": [ri.st_dev, 2**64 - 1, 0o700],
                        "lease": [ri.st_dev, 2**64 - 1, 0o600], "entries": tree_entries,
                        "admin": {"name": "tree" + "9" * 20, "identity": [ri.st_dev, 2**64 - 1, 0o700], "entries": admin_entries}, "errors": []}
                    for name in ("owner.json", ".git", "HEAD", "gitdir", "commondir", "locked", "index", "logs/HEAD", "ORIG_HEAD"):
                        size = 12 + width + sum(_round(42 + width + len(p) + 1, 8) for p in entries) + 8 + sum(len(p) + 2 * len(str(len(entries))) + 3 + width for p in entries) if name == "index" else len(self.root) * 2 + 256
                        destination = b"data/native/git-resources" if name in ("owner.json", ".git") else b"data/native/repository.git/worktrees"
                        add(destination, size, "worktree-control:" + name)
                    add(b"data/native/repository.git/worktrees", 0, "worktree-admin-directory", directory=True)
                    add(b"data/native/repository.git/worktrees", 0, "worktree-admin-refs-directory", directory=True)
                    add(b"data/native/git-resources", 0, "worktree-directory", directory=True)
                    add(b"data/native/git-resources", 0, "worktree-lease")
                self._resource_entry_bound = max([3] + [len(r["entries"]) for r in journal["records"].values()])
                journal_bytes = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
                self._resource_bound = len(journal_bytes)
                v = add(b"data/native/git-resources", len(journal_bytes), "full-prospective-resource-journal")
                generated_controls[v.key] = max(generated_controls.get(v.key, 0), len(journal_bytes))
                if not _optional_stat(self.root + b"/data/native/git-resources/journal.lock"):
                    add(b"data/native/git-resources", 0, "resource-journal-lock")
                # v2 complete entries, checksum, TREE extension bound. No split,
                # sparse, untracked, fsmonitor, EOIE or parallel IEOT extensions.
                nodes = {b"": {}}
                for path in sorted(entries):
                    parts = path.split(b"/")
                    if any(b"/".join(parts[:i]) in entries for i in range(1, len(parts))):
                        raise Blocked("file-parent-collision")
                    for i, name in enumerate(parts):
                        parent = b"/".join(parts[:i])
                        nodes.setdefault(parent, {})[name] = i < len(parts) - 1
                        if i < len(parts) - 1:
                            nodes.setdefault(b"/".join(parts[:i + 1]), {})
                base = 12 + width + sum(_round(42 + width + len(p) + 1, 8) for p in entries)
                cache = 8 + sum(len(p.rsplit(b"/", 1)[-1]) + 1 + 2 * len(str(len(entries))) + 3 + width for p in nodes)
                self._index_bound = base + cache
                if kind == "candidate":
                    def index_bound(paths):
                        parents = {b""} | {b"/".join(p.split(b"/")[:i]) for p in paths for i in range(1, len(p.split(b"/")))}
                        return (12 + width + sum(_round(42 + width + len(p) + 1, 8) for p in paths) +
                                8 + sum(len(p.rsplit(b"/", 1)[-1]) + 1 + 2 * len(str(len(paths))) + 3 + width for p in parents))
                    self._index_bound = max(index_bound(paths) for paths in
                        (initial_entries, initial_entries - set(evidence.candidate_removals), entries))
                for label in ("complete-index", "index-lock-overlap"):
                    add(b"data/native/git-resources", self._index_bound, label)
                for path, children in nodes.items():
                    tree_size = sum((5 if directory else 6) + 1 + len(name) + 1 + width
                                    for name, directory in children.items())
                    add(b"data/native/repository.git/objects", _loose("tree", tree_size), "generated-tree:" + repr(path))
                    add(b"data/native/repository.git/objects", 0, "tree-fanout:" + repr(path), directory=True)
                if kind == "save":
                    if evidence.commit is None:
                        raise Blocked("exact-commit-envelope-required")
                    if evidence.ref is None or evidence.reflog_entry is None:
                        raise Blocked("exact-save-ref-and-reflog-route-required")
                    ref = evidence.ref
                    if not ref.startswith(b"refs/"):
                        raise Blocked("qualified-save-ref-required")
                    self._git(["check-ref-format", os.fsdecode(ref)])
                    if (not evidence.reflog_entry.endswith(b"\n") or b"\n" in evidence.reflog_entry[:-1]
                            or not re.match(rb"[0-9a-f]{" + str(width * 2).encode() + rb"} [0-9a-f]{" + str(width * 2).encode() + rb"} ", evidence.reflog_entry)):
                        raise Blocked("exact-reflog-entry-required")
                    provenance.append(("save-output", ref, hashlib.sha256(evidence.commit).hexdigest(),
                                       hashlib.sha256(evidence.reflog_entry).hexdigest()))
                    add(b"data/native/repository.git/objects", _loose("commit", len(evidence.commit)), "commit-object")
                    ref_path = b"data/native/repository.git/" + ref
                    log_path = b"data/native/repository.git/logs/" + ref
                    commit_oid = hashlib.new("sha1" if width == 20 else "sha256", b"commit " + str(len(evidence.commit)).encode() + b"\0" + evidence.commit).hexdigest().encode()
                    ref_proof = ("bytes", commit_oid + b"\n")
                    add(ref_path, width * 2 + 1, "ref-atomic-output", file_target=True, proof=ref_proof)
                    add(ref_path + b".lock", width * 2 + 1, "ref-lock-overlap", file_target=True, proof=ref_proof)
                    try:
                        existing_log = self._optional_source(self.root + b"/" + log_path, dict(snapshot.observations))
                        log_size = source(existing_log) if existing_log is not None else 0
                    except FileNotFoundError:
                        self._require_absent(self.root + b"/" + log_path, dict(snapshot.observations))
                        log_size = 0
                    add(log_path, log_size + len(evidence.reflog_entry), "full-reflog-copy-append", file_target=True, proof=("bytes", evidence.reflog_entry) if not log_size else ("source-append", existing_log, evidence.reflog_entry))
                    add(log_path + b".lock", log_size + len(evidence.reflog_entry), "reflog-atomic-overlap", file_target=True, proof=("bytes", evidence.reflog_entry) if not log_size else ("source-append", existing_log, evidence.reflog_entry))
            elif kind == "publication":
                from native_publication import validate_intent, packed_pin_removal
                intent = validate_intent(publication.artifact)
                from native_operations import _parse
                terminal=publication.purpose=='recovery' and _parse(publication.initial)['workers'][publication.operation_id]['phase'] in ('completed','failed')
                expected_tree=intent.candidate.baseline_tree_oid if terminal else intent.candidate.tree_oid
                if (evidence.selected or evidence.materialize or evidence.copies or evidence.metadata or
                        evidence.replacements or evidence.diff or evidence.incoming or evidence.incoming_index or
                        evidence.candidate_manifest is not None or evidence.base_tree != expected_tree or
                        evidence.commit != intent.raw_commit or evidence.ref != intent.local_ref.encode("ascii")):
                    raise Blocked("exact-publication-only-recipe-required")
                width, config = self._profile()
                if publication.purpose!='abandon' and not terminal: self._entries(evidence.base_tree,width)
                provenance.append(("git-config",config,"publication",hashlib.sha256(publication.artifact).hexdigest()))
                for name,template in publication.states:
                    if not publication.is_outcome_recipe(name): continue
                    value=_control_json(template)
                    try: self._validate_operation_state(value,artifact_cache,prospective=publication)
                    except FileNotFoundError:
                        if publication._initial_bytes is not None: raise
                        from native_operations import _publication_graph
                        _publication_graph(publication.store,value,None,publication.artifact)
                    # Fixed-size strict ASCII slots determine an exact serialized
                    # extent. This template is an accounting input, never an
                    # authorized control output or an observed refusal.
                    state_target=b'data/native-operations/state.json'
                    add(state_target,len(template),'publication-outcome-recipe:'+name,file_target=True,
                        proof=('publication-outcome-recipe',name,len(template)))
                    v=add(b'data/native-operations',len(template),'publication-outcome-atomic-overlap:'+name)
                    generated_controls[v.key]=max(generated_controls.get(v.key,0),len(template))
                commit_oid = intent.commit_oid.encode()
                if not terminal:
                    self._relevant_oids.add(commit_oid)
                    object_path=b"data/native/repository.git/objects/"+commit_oid[:2]+b"/"+commit_oid[2:]
                    # The exact raw bytes feed hash-object's stdin. There is no disk
                    # spool/index/worktree or guessed outgoing-pack allocation.
                    add(object_path,_loose("commit",len(evidence.commit)),"publication-loose-commit",file_target=True,
                        proof=("git-object",b"commit",intent.commit_oid,intent.raw_commit))
                    add(b"data/native/repository.git/objects",_loose("commit",len(evidence.commit)),"publication-object-atomic-overlap")
                for name, oid in ((intent.tree_pin,intent.candidate.tree_oid),(intent.commit_pin,intent.commit_oid),
                                  (intent.local_ref,intent.commit_oid)):
                    relative=b"data/native/repository.git/"+name.encode("ascii")
                    self._git(["check-ref-format",name])
                    add(relative,width*2+1,"publication-ref:"+name,file_target=True,proof=("bytes",oid.encode()+b"\n"))
                    add(relative+b".lock",width*2+1,"publication-ref-lock:"+name,file_target=True,proof=("bytes",oid.encode()+b"\n"))
                packed_relative=b'data/native/repository.git/packed-refs'
                packed_source=self._optional_source(self.root+b'/'+packed_relative,dict(snapshot.observations))
                if packed_source is not None:
                    packed_raw, actual_source, _=self._read_regular(packed_source.path,limit=self.limits.git_bytes)
                    if actual_source!=packed_source: raise Blocked('changed-packed-ref-during-projection')
                    packed_refs=self._packed_ref_structure(packed_raw,width*2)
                    self._publication_packed=(packed_raw,packed_source,packed_refs)
                    variants=[((),packed_raw)]
                    pins=((intent.commit_pin,intent.commit_oid),(intent.tree_pin,intent.candidate.tree_oid))
                    for name,oid in pins:
                        if name.encode() not in packed_refs: continue
                        if packed_refs[name.encode()]!=(oid.encode(),None):
                            raise Blocked('unexpected-packed-publication-pin')
                        for removed,raw in tuple(variants):
                            variants.append((removed+(name,),packed_pin_removal(raw,name,oid)))
                    if len(variants)>1:
                        proof=('publication-packed',packed_source,tuple(variants))
                        size=max(len(raw) for _,raw in variants)
                        add(packed_relative,size,'publication-packed-retained-atomic-output',file_target=True,proof=proof)
                        add(packed_relative+b'.lock',size,'publication-packed-lock-overlap',file_target=True,proof=proof)
                for relative, payload in intent.reflogs:
                    source_path=self.root+b"/"+relative
                    existing=self._optional_source(source_path,dict(snapshot.observations))
                    previous_size=source(existing) if existing is not None else 0
                    proof=("source-append",existing,payload) if existing is not None else ("bytes",payload)
                    add(relative,previous_size+len(payload),"publication-reflog",file_target=True,proof=proof)
                    add(relative+b".lock",previous_size+len(payload),"publication-reflog-lock",file_target=True,proof=proof)
                artifact_target=b"data/native-operations/artifacts/"+hashlib.sha256(publication.artifact).hexdigest().encode()+b".diff"
                add(artifact_target,len(publication.artifact),"publication-immutable-intent",file_target=True,proof=("bytes",publication.artifact))
                add(b"data/native-operations/artifacts",len(publication.artifact),"publication-artifact-atomic-overlap")
                add(b"data/native-operations/lock",0,"publication-owner-lock",file_target=True,proof=("bytes",b""))
                add(b"data/native-operations",max(len(value) for _,value in publication.states),"publication-state-atomic-overlap")
                if intent.endpoint.startswith(b'{') and not terminal:
                    # Pinned protocol-v0 kept-pack execution has one pack, one
                    # index, one bounded keep message, and one shallow temp.
                    # The kernel cap applies to every writer/child. It is not
                    # a total budget: all three variable file extents are held.
                    destination=b'data/native/git-resources'
                    volume,_=self._destination(destination,mounts)
                    original=next((v for v in snapshot.volumes if v.key==volume.key),None)
                    available=min(volume.available,original.available) if original is not None else volume.available
                    add(destination,512,'network-transport-keep')
                    for name in ('objects','objects/pack','refs'):
                        add(destination,0,'network-transport-directory:'+name,directory=True)
                    add(destination,0,'network-pack-promote-name-overlap',directory=True)
                    add(destination,0,'network-index-promote-name-overlap',directory=True)
                    add(destination,512,'network-transport-config')
                    add(destination,128,'network-transport-head')
                    # Exact canonical resource envelope uses uint64 descriptor
                    # bounds and the longest permitted pack/hash filenames.
                    with _directory(self.root) as root_fd,_directory(self.root+b'/data/native/repository.git') as meta_fd:
                        ri,mi=os.fstat(root_fd),os.fstat(meta_fd)
                        root_id=[ri.st_dev,ri.st_ino,stat.S_IMODE(ri.st_mode)]
                        meta_id=[mi.st_dev,mi.st_ino,stat.S_IMODE(mi.st_mode)]
                    binding=hashlib.sha256(self.root+b'\0'+self.root+b'/data/native/repository.git'+repr((root_id,meta_id)).encode()).hexdigest()
                    try:
                        journal=self._control(self.root+b'/data/native/git-resources/journal.json')
                        self._validate_resource_journal(journal)
                    except FileNotFoundError:
                        journal={'schema':1,'root':root_id,'metadata':meta_id,'storage':[ri.st_dev,2**64-1,0o700],'records':{}}
                    if any(record['kind']=='transport' for record in journal['records'].values()):
                        raise Blocked('prior-transport-scratch-requires-explicit-settlement')
                    rid='ffffffff-ffff-4fff-8fff-ffffffffffff'
                    marker={'resource':rid,'operation':evidence.operation_id,'owner':evidence.owner_id,'binding':binding}
                    add(destination,len(json.dumps(marker,sort_keys=True).encode()),'network-resource-marker')
                    add(destination,0,'network-resource-lease')
                    add(destination,0,'network-resource-root',directory=True)
                    file_record=[ri.st_dev,2**64-1,0o600,'f'*64]
                    names=(b'owner.json',b'HEAD',b'config',b'objects/pack/pack-'+b'f'*(width*2)+b'.pack',
                           b'objects/pack/pack-'+b'f'*(width*2)+b'.idx',b'objects/pack/pack-'+b'f'*(width*2)+b'.keep',b'shallow_XXXXXX')
                    entries={base64.b64encode(name).decode():file_record for name in names}
                    for name in (b'objects',b'objects/pack',b'refs'):
                        entries[base64.b64encode(name).decode()]=[ri.st_dev,2**64-1,0o700,'directory']
                    journal['records'][rid]={'operation':evidence.operation_id,'owner':evidence.owner_id,'kind':'transport',
                        'phase':'allocated','root':[ri.st_dev,2**64-1,0o700],'lease':[ri.st_dev,2**64-1,0o600],
                        'entries':entries,'admin':None,'errors':[],'writer':2**31-1}
                    self._resource_entry_bound=max([3]+[len(record['entries']) for record in journal['records'].values()])
                    self._resource_bound=len(json.dumps(journal,sort_keys=True,separators=(',',':')).encode())
                    v=add(destination,self._resource_bound,'network-resource-journal')
                    generated_controls[v.key]=max(generated_controls.get(v.key,0),self._resource_bound)
                    if not _optional_stat(self.root+b'/data/native/git-resources/journal.lock'):
                        add(destination,0,'network-resource-journal-lock')
                    # All fixed publication/control/journal extents are now
                    # known. Allocate the remaining admitted physical capacity
                    # equally to the three independently capped writer files.
                    # Ext4's existing allocator charges one metadata block per
                    # data block and two fixed extent/admin blocks per file.
                    volume=volumes[v.key];q=volume.quantum
                    control=max([generated_controls.get(v.key,0)]+[len(control.payload) for control in evidence.controls
                        if self._destination(control.target.path,mounts,file_target=control.target.kind=='file')[0].key==v.key])
                    prior=next((p for p in previous.volumes if p.key==v.key),None) if previous else None
                    growth=max(0,prior.available-volume.available,volume.allocated-prior.allocated) if prior else 0
                    reserve=_round(volume.largest_control,q)+_round(control,q)+q+growth
                    fixed=sum(row.allocated for row in allocations if row.volume==v.key)
                    _,missing=self._destination(destination,mounts)
                    factor=2 if volume.filesystem==b'ext4' else 1
                    overhead=3*q*(1+len(missing)+(2 if factor==2 else 0))
                    network_cap=max(0,(volume.available-fixed-reserve-overhead)//(3*factor)//q*q)
                    if network_cap<q:raise InsufficientSpace('network-transfer-capacity-insufficient')
                    for name in ('pack','index','shallow-temporary'):
                        add(destination,network_cap,'network-transport-'+name)
            elif kind == "scope_control":
                if scope_preparation is None or evidence.selected or evidence.candidate_manifest is not None:
                    raise Blocked("exact-scope-control-recipe-required")
            elif kind == "repack":
                width, config = self._profile()
                objects = self._objects(width)
                self._relevant_oids.update(oid for oid, _, _ in objects)
                provenance.append(("all-local-objects", _digest(objects), config))
                pack = 12 + width + sum(1 + (size.bit_length() + 6) // 7 + _zbound(size)
                                        for _, _, size in objects)
                index = 8 + 256 * 4 + len(objects) * (width + 4 + 4 + 8) + 2 * width
                add(b"data/native/repository.git/objects/pack", pack, "new-pack-old-packs-retained")
                add(b"data/native/repository.git/objects/pack", index, "pack-index")
                add(b"data/native/repository.git/objects/pack", pack, "pack-temporary-overlap")
            elif kind == "fetch":
                if evidence.incoming is None:
                    unknown.append("remote-incoming-and-thin-repair-unbounded")
                    provenance.append(("estimated-local-baseline", snapshot.allocated))
                else:
                    size = source(evidence.incoming)
                    if evidence.incoming_index is None:
                        raise Blocked("complete-incoming-pack-index-required")
                    index_size = source(evidence.incoming_index)
                    if (not evidence.incoming.path.endswith(b".pack")
                            or evidence.incoming_index.path != evidence.incoming.path[:-5] + b".idx"):
                        raise Blocked("paired-complete-pack-index-required")
                    width, config = self._profile()
                    output = self._git(["verify-pack", "-v", os.fsdecode(evidence.incoming_index.path)])
                    # verify-pack validates checksums and every object, including
                    # deltas; a thin pack without its bases cannot pass this route.
                    count = 0
                    for line in output.splitlines():
                        fields = line.split()
                        if fields and re.fullmatch(rb"[0-9a-f]{" + str(width * 2).encode() + rb"}", fields[0]):
                            if len(fields) not in (5, 7) or fields[1] not in (b"blob", b"tree", b"commit", b"tag"):
                                raise Blocked("malformed-incoming-object-proof")
                            count += 1
                    if not output.rstrip().endswith(b": ok"):
                        raise Blocked("incomplete-incoming-object-proof")
                    provenance.append(("verified-standalone-pack", count, _digest(output), config))
                    add(b"data/native/git-resources", size, "incoming-spool")
                    add(b"data/native/repository.git/objects/pack", size, "copied-complete-pack")
                    add(b"data/native/repository.git/objects/pack", index_size, "copied-complete-index")
                    add(b"data/native/repository.git/objects/pack", 8 + 1024 + count * (width + 16) + 2 * width, "index-replacement-overlap")
            elif kind == "apply":
                if not evidence.metadata:
                    unknown.append("exact-recovery-metadata-adapter-required")
                if evidence.replacements:
                    width, config = self._profile()
                    for oid, target in evidence.replacements:
                        if not re.fullmatch("[0-9a-f]{" + str(width * 2) + "}", oid):
                            raise Blocked("invalid-replacement-blob")
                        if self._git(["cat-file", "-t", oid]).strip() != b"blob":
                            raise Blocked("replacement-must-be-local-blob")
                        raw = self._git(["cat-file", "-s", oid]).strip()
                        if not re.fullmatch(rb"[0-9]+", raw):
                            raise Blocked("invalid-local-blob-size")
                        self._relevant_oids.add(oid.encode())
                        provenance.append(("replacement-blob", oid, int(raw), config))
                        add(target, int(raw), "replacement-temporary:" + oid, proof=("blob", oid, int(raw)))
            else:
                unknown.append("unsupported-operation-kind")
            for s, target in evidence.copies:
                add(target, source(s), "independent-copy:" + repr(s.path), proof=("source", s))
            for s, target in evidence.metadata:
                add(target, source(s), "exact-encoded-metadata:" + repr(s.path), proof=("source", s))
            if evidence.diff is not None:
                add(b"data/native/diffs", source(evidence.diff), "exact-diff-artifact")
            # Commands may take time: recheck every selected source after the
            # observation, preserving a mutation instead of replacing evidence.
            checked = [s for _, s in evidence.selected] + [s for s, _ in evidence.copies + evidence.metadata]
            checked += [s for s in (evidence.diff, evidence.incoming, evidence.incoming_index) if s is not None]
            for s in checked:
                if self.source(s.path) != s:
                    raise Blocked("changed-source-during-projection")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            unknown.append(str(exc))
        pending = dict(generated_controls)
        for control in evidence.controls:
            try:
                v, _ = self._destination(control.target.path, mounts, file_target=control.target.kind == "file")
                pending[v.key] = max(pending.get(v.key, 0), len(control.payload))
            except (OSError, ValueError) as exc:
                unknown.append(str(exc))
        reserves, decisions = [], []
        for key in sorted({a.volume for a in allocations}, key=repr):
            v = volumes[key]
            prior = next((p for p in previous.volumes if p.key == key), None) if previous else None
            growth = max(0, prior.available - v.available, v.allocated - prior.allocated) if prior else 0
            r = Reserve(key, _round(v.largest_control, v.quantum), _round(pending.get(key, 0), v.quantum),
                        v.quantum, growth, (previous.timestamp, snapshot.timestamp) if prior else ())
            reserves.append(r)
            peak = sum(a.allocated for a in allocations if a.volume == key)
            # Reserve has an atomic replacement inode and a failure-control inode.
            inode_need = sum(a.inodes for a in allocations if a.volume == key) + 2
            shortfall = max(0, peak + r.total - v.available)
            status = "cannot-establish-admission" if unknown or not v.supported else (
                "insufficient-space" if shortfall or (v.inodes is not None and inode_need > v.inodes) else "fits")
            decisions.append(Decision(status, key, peak, r.total, inode_need, v.available, v.inodes, shortfall, v.warning))
        plan = Plan(kind, evidence, snapshot.digest, self.layout_digest, tuple(allocations), tuple(reserves),
                    tuple(decisions), tuple(dict.fromkeys(unknown)), tuple(provenance),
                    tuple((key, sum(a.allocated for a in allocations if a.volume == key)) for key in sorted({a.volume for a in allocations}, key=repr)),
                    tuple(sorted((path, identity) for path, identity in namespace.items() if beneath(path, self.root))),
                    tuple(sorted(future_directories)), tuple(sorted(output_files.items())),network_cap)
        try:
            self._contexts[id(plan)] = self._compile_context(plan, snapshot, artifact_cache, mounts) if plan.supported else None
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            plan = replace(plan, unknown=tuple(dict.fromkeys(plan.unknown + (str(exc),))),
                           decisions=tuple(replace(d, status="cannot-establish-admission") for d in plan.decisions))
            self._contexts[id(plan)] = None
        key, owner = id(plan), weakref.ref(self)
        def expired(_):
            disk = owner()
            if disk is not None:
                disk._contexts.pop(key, None)
                disk._plan_refs.pop(key, None)
        self._plans[key] = plan
        self._plan_refs[key] = weakref.ref(plan, expired)
        return plan

    @contextmanager
    def _idle_lock(self, relative, expected=None):
        """Observe an existing owner lock without creating or waiting for it."""
        path = self.root + b"/" + relative
        try:
            source = self.source(path)
        except FileNotFoundError:
            if expected is not None:
                raise Blocked("missing-stable-owner-lock")
            self._require_absent(path)
            yield None
            return
        if expected is not None and source.observation[:3] != expected:
            raise Blocked("replaced-stable-owner-lock")
        if stat.S_IMODE(source.observation[2]) != 0o600:
            raise Blocked("unverifiable-existing-owner-lock")
        with _directory(os.path.dirname(path)) as parent:
            fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if _observation(os.fstat(fd)) != source.observation:
                    raise Blocked("changed-existing-owner-lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Blocked("competing-existing-owner-critical-section") from exc
                yield source.observation[:3]
                if self.source(path) != source:
                    raise Blocked("replaced-existing-owner-lock")
            finally:
                os.close(fd)

    def _validate_resource_journal(self, value):
        # Canonical validation is observational: only these actual FD identities
        # are required. Never enter NativeGit._journal(), which can initialize.
        from native_git import NativeGit
        observer = object.__new__(NativeGit)
        with ExitStack() as stack:
            for field, relative in (("_root_id", b""), ("_meta_id", b"data/native/repository.git"),
                                    ("_storage_id", b"data/native/git-resources")):
                fd = stack.enter_context(_directory(self.root + (b"/" + relative if relative else b"")))
                info = os.fstat(fd)
                if field == "_storage_id" and stat.S_IMODE(info.st_mode) != 0o700:
                    raise Blocked("private-resource-storage-required")
                setattr(observer, field, [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)])
            observer._validate_journal(value)

    def _validate_operation_state(self, value, cache, prospective=None):
        from native_operations import NativeOperationStore
        route = {}
        with _directory(self.root + b"/data/native-operations", observations=route) as state_root, _directory(self.root + b"/data/native-operations/artifacts", observations=route) as artifacts:
            if any(stat.S_IMODE(os.fstat(fd).st_mode) != 0o700 for fd in (state_root, artifacts)):
                raise Blocked("private-state-directories-required")
            deadline = time.monotonic() + self.limits.seconds
            class Observer(NativeOperationStore):
                def _validate_scope_content(observer,state,directory):
                    for ref in observer._scope_refs(state,directory):
                        name=ref.key+'.blob'
                        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                        try:
                            observer._regular(fd);before=_observation(os.fstat(fd))
                            token=(name,tuple(sorted(route.items())),_identity(os.fstat(directory)),before,ref,ref.size)
                            if token not in cache:
                                with observer._verified_scope_blob(directory,ref):pass
                                cache[token]=ref.size
                            if (_observation(os.fstat(fd))!=before or
                                    _observation(os.stat(name,dir_fd=directory,follow_symlinks=False))!=before):
                                raise Blocked('changed-pending-scope-content')
                        finally:os.close(fd)
                def _verify(observer, directory, key, size=None):
                    # Keep canonical graph validation; replace only its
                    # artifact reader with a bounded, context-local proof.
                    from native_operations import _hash
                    _hash(key)
                    if prospective is not None and key == hashlib.sha256(prospective.artifact).hexdigest():
                        if size != len(prospective.artifact):
                            raise Blocked("prospective-artifact-size-mismatch")
                        try:
                            os.stat(key + ".diff",dir_fd=directory,follow_symlinks=False)
                        except FileNotFoundError:
                            return size
                    if time.monotonic() >= deadline:
                        raise Blocked("artifact-verification-deadline")
                    name = key + ".diff"
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        observer._regular(fd)
                        before = _observation(os.fstat(fd))
                        parent = _identity(os.fstat(directory))
                        token = (name, tuple(sorted(route.items())), parent, before, key, size)
                        if token in cache:
                            if _observation(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before:
                                raise Blocked("changed-cached-artifact-route")
                            return cache[token]
                        fingerprint, measured = hashlib.sha256(), 0
                        while True:
                            if time.monotonic() >= deadline:
                                raise Blocked("artifact-verification-deadline")
                            chunk = os.read(fd, 65536)
                            if time.monotonic() >= deadline:
                                raise Blocked("artifact-verification-deadline")
                            if not chunk:
                                break
                            measured += len(chunk)
                            fingerprint.update(chunk)
                        observer._regular(fd)
                        if (_observation(os.fstat(fd)) != before or
                                _observation(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before or
                                _identity(os.fstat(directory)) != parent):
                            raise Blocked("changed-artifact-during-verification")
                        if fingerprint.hexdigest() != key or (size is not None and size != measured):
                            raise Blocked("artifact mismatch")
                        cache[token] = measured
                        return measured
                    finally:
                        os.close(fd)
            Observer(os.fsdecode(self.root + b"/data"))._validate(value, artifacts)

    def _bind_publication_control(self, control, evidence, cache):
        from native_operations import _parse, _publication_graph
        prepared = evidence.publication.check()
        if (control.target.path != b"data/native-operations/state.json" or
                control.payload not in dict(prepared.states).values() or
                (control.operation_id,control.owner_id) != (prepared.operation_id,prepared.owner_id)):
            raise Blocked("exact-issued-publication-state-required")
        value = _parse(control.payload)
        try:
            self._validate_operation_state(value,cache,prospective=prepared)
        except FileNotFoundError:
            # No initial store: canonical graph validator permits only this exact
            # independently hashed prospective intent, not absent history.
            if prepared._initial_bytes is not None:
                raise
            _publication_graph(prepared.store,value,None,prepared.artifact)

    def _bind_control(self, control, evidence, cache):
        value = _control_json(control.payload)
        if not isinstance(value, dict):
            raise Blocked("control-object-required")
        declared = (control.operation_id, control.owner_id)
        expected = (evidence.operation_id, evidence.owner_id)
        if declared != (None, None) and declared != expected:
            raise Blocked("control-operation-binding-required")
        target = control.target.path
        state = target == b"data/native-operations/state.json" or "workers" in value
        journal = target == b"data/native/git-resources/journal.json" or {"root", "metadata", "storage"}.issubset(value)
        if state:
            self._validate_operation_state(value, cache)
            active = value["active"]
            derived = (active, value["workers"][active]["owner"]) if active is not None else (None, None)
            if active is not None and derived != expected:
                raise Blocked("foreign-prospective-operation-state")
            if declared == (None, None) and derived != expected:
                raise Blocked("control-operation-binding-required")
        elif journal:
            self._validate_resource_journal(value)
            if declared != expected:
                raise Blocked("control-operation-binding-required")
        else:
            embedded = (value.get("operation_id"), value.get("owner_id"))
            if embedded != (None, None) and embedded != expected:
                raise Blocked("control-operation-binding-required")
            if declared != expected and embedded != expected:
                raise Blocked("control-operation-binding-required")

    def _competitors(self, evidence, cache=None, admission=None):
        with ExitStack() as stack:
            locks = {}
            for path in (b"data/native-operations/lock", b"data/native/git-resources/journal.lock"):
                expected = admission._owner_locks[path] if admission is not None else None
                scope=evidence.scope_preparation
                held=getattr(scope.store,'_scope_execution',None) if scope is not None else None
                if path==b'data/native-operations/lock' and held is not None and admission is not None:
                    scope.check()
                    if (held[:3]!=(threading.get_ident(),scope,admission) or not admission.active or
                            admission.plan.evidence.scope_preparation is not scope):
                        raise Blocked('foreign-scope-critical-section')
                    info=os.fstat(held[5]);source=self.source(self.root+b'/'+path)
                    if _observation(info)!=source.observation or source.observation[:3]!=expected:
                        raise Blocked('changed-scope-critical-section')
                    fcntl.flock(held[5],fcntl.LOCK_EX|fcntl.LOCK_NB)
                    locks[path]=source.observation[:3]
                else:
                    locks[path] = stack.enter_context(self._idle_lock(path, expected))
            self._inspect_competitors(evidence, cache if cache is not None else {}, admission)
            if admission is not None:
                # A prospective lock is adopted only while its actual kernel
                # lock is held and the canonical current owner graph/namespace
                # has passed. This bounded receipt never lowers initial demand.
                for path, identity in locks.items():
                    if identity is not None and admission._owner_locks[path] is None:
                        control = (b"data/native/git-resources/journal.json" if path.endswith(b"journal.lock")
                                   else b"data/native-operations/state.json")
                        if not _optional_stat(self.root + b"/" + control):
                            raise Blocked("unqualified-new-owner-lock")
                        admission._owner_locks[path] = identity

    def _inspect_competitors(self, evidence, cache, admission=None):
        observed_state = None
        spool_total = 0
        known = {b"journal.json", b"journal.lock"}
        for path in (b"data/native/git-resources/journal.json", b"data/native-operations/state.json"):
            full = self.root + b"/" + path
            try:
                value = self._control(full)
            except FileNotFoundError:
                self._require_absent(full, dict(admission._context.snapshot.observations) if admission is not None else None)
                if admission is not None and path in admission._owner_controls:
                    raise Blocked("missing-established-owner-control")
                continue
            if path.endswith(b"journal.json"):
                try:
                    self._validate_resource_journal(value)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise Blocked("unverifiable-resource-state:" + str(exc)) from exc
                records = value["records"]
                if admission is not None:
                    admission._validate_resources(value)
                    worktrees = self.root + b"/data/native/repository.git/worktrees"
                    if (dict(admission.plan.namespace).get(worktrees, False) is None and
                            admission._namespace.get(worktrees) is not None and
                            not any(record["kind"] == "worktree" for record in records.values()) and
                            not _optional_stat(worktrees)):
                        admission._namespace[worktrees] = None
                for rid, record in records.items():
                    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f-]{36}", rid) or not isinstance(record, dict):
                        raise Blocked("unreconciled-native-resource-allocation")
                    known.update((rid.encode(), (rid + ".lease").encode()))
                    lease = self.root + b"/data/native/git-resources/" + (rid + ".lease").encode()
                    s = self.source(lease)
                    actual = [s.observation[0], s.observation[1], stat.S_IMODE(s.observation[2])]
                    if record.get("lease") != actual or actual[2] != 0o600:
                        raise Blocked("unverifiable-resource-lease")
                    with _directory(os.path.dirname(lease)) as parent:
                        fd = os.open(os.path.basename(lease), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        try:
                            if _observation(os.fstat(fd)) != s.observation:
                                raise Blocked("changed-resource-lease")
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                live = False
                            except BlockingIOError:
                                live = True
                            if live and (record.get("operation"), record.get("owner")) != (evidence.operation_id, evidence.owner_id):
                                raise Blocked("competing-native-resource-writer")
                            if live:
                                resource = self.root + b"/data/native/git-resources/" + rid.encode()
                                with _directory(resource) as resource_fd:
                                    info = os.fstat(resource_fd)
                                    if record.get("root") != [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]:
                                        raise Blocked("replaced-same-operation-resource")
                                if admission is not None:
                                    admission._validate_resource_files(resource, record)
                                    if record["kind"] == "spool":
                                        spool = record["entries"].get(base64.b64encode(b"spool").decode())
                                        if spool is not None:
                                            spool_total += self.source(resource + b"/spool").size
                                            if spool_total > sum(admission._context.spool_sizes):
                                                raise Blocked("spool-total-exceeds-initial-envelope")
                                self._check_marker(resource, rid, record, evidence)
                                if record["kind"] == "worktree":
                                    self._check_worktree(resource, rid, record, evidence)
                                if admission is not None:
                                    admission._adopt_resource_directories(record)
                            if self.source(lease) != s or _observation(os.fstat(fd)) != s.observation:
                                raise Blocked("changed-resource-lease")
                        finally:
                            os.close(fd)
            else:
                # Reuse the canonical owner validator without read(), _locked(),
                # initialization, publication or a second workflow ledger.
                from native_operations import NativeOperationStore
                try:
                    if stat.S_IMODE(self.source(full).observation[2]) != 0o600:
                        raise Blocked("private-state-file-required")
                    self._validate_operation_state(value, cache)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise Blocked("unverifiable-competing-operation-state:" + str(exc)) from exc
                observed_state = value
                if value["active"] is not None:
                    worker = value["workers"][value["active"]]
                    if value["active"] != evidence.operation_id or worker["owner"] != evidence.owner_id:
                        raise Blocked("competing-operation-state")
            if admission is not None:
                admission._owner_controls.add(path)
        if admission is not None:
            admission._validate_state(observed_state)
        resource = self.root + b"/data/native/git-resources"
        try:
            with _directory(resource) as fd:
                with os.scandir(fd) as entries:
                    for item in entries:
                        if os.fsencode(item.name) not in known:
                            raise Blocked("unreconciled-private-resource")
        except FileNotFoundError:
            self._require_absent(resource)
        for legacy in self.layout.legacy:
            # No cooperation receipt exists for active legacy writers yet.
            if _optional_stat(self.root + b"/" + legacy + b"/.git/index.lock"):
                raise Blocked("competing-legacy-writer")

    @contextmanager
    def guard(self, plan):
        if self._plans.get(id(plan)) is not plan or plan.layout_digest != self.layout_digest:
            raise Blocked("unissued-or-recreated-diagnostic-plan")
        if self._active_operation is not None:
            raise Blocked("nested-or-competing-guard")
        if id(plan) not in self._contexts:
            raise Blocked("used-or-ended-diagnostic-plan")
        lockpath = self.root + b"/data/native/disk-admission.lock"
        with _THREAD_LOCKS_MUTEX:
            local = _THREAD_LOCKS.setdefault(lockpath, threading.Lock())
        if not local.acquire(blocking=False):
            raise Blocked("nested-or-competing-guard")
        fd = None
        primary = None
        try:
            with _directory(os.path.dirname(lockpath)) as parent:
                fd = os.open(os.path.basename(lockpath), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                    raise Blocked("private-single-link-lock-required")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Blocked("competing-process-guard") from exc
                admission = Admission(self, plan, fd, lockpath, _identity(info))
                try:
                    admission.revalidate()
                    self._active_operation = plan.evidence.operation_id
                    self._candidate_admission = admission if plan.kind in ("candidate", "publication", "scope_control") else None
                    # Consumption is one-shot only after a successful entry.
                    self._contexts.pop(id(plan), None)
                    yield admission
                except BaseException as exc:
                    primary = exc
                    raise
                finally:
                    try:
                        admission._close()
                    except BaseException as cleanup:
                        if primary is None:
                            raise
                        primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + (
                            "admission-context-close:path=%r:%s" % (lockpath, cleanup),)
                    finally:
                        self._active_operation = None
                        self._candidate_admission = None
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if fd is not None:
                    os.close(fd)  # kernel releases flock, including process death
            except BaseException as cleanup:
                if primary is None:
                    primary = Blocked("admission-lock-close-debt")
                    primary.cleanup_errors = ("admission-lock-close:fd=%d:path=%r:%s" % (fd, lockpath, cleanup),)
                    raise primary from cleanup
                primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + (
                    "admission-lock-close:fd=%d:path=%r:%s" % (fd, lockpath, cleanup),)
            finally:
                local.release()


class Admission:
    __slots__ = ("_disk", "_baseline", "_fd", "_path", "_identity", "_active", "_last_snapshot", "_current", "_namespace", "_artifact_cache", "_created_files", "_context", "_owner_binding", "_owner_seen", "_owner_locks", "_owner_controls", "_candidate_git", "_issued_plan", "_publication_preimages")

    def __init__(self, disk, plan, fd, path, identity):
        self._disk, self._baseline, self._fd, self._path, self._identity = disk, plan, fd, path, identity
        self._active = True
        self._candidate_git = None
        self._publication_preimages = {}
        if plan.kind == "publication":
            for path,proof in plan.output_files:
                if proof is not None and proof[0] == "source-append":
                    self._publication_preimages[path] = disk._read_regular(proof[1].path)[0]
                elif proof is not None and proof[0]=='publication-packed' and not path.endswith(b'.lock'):
                    self._publication_preimages[path]=proof[2][0][1]
        self._issued_plan = plan
        self._last_snapshot = disk._contexts[id(plan)].snapshot if disk._contexts[id(plan)] else None
        self._current = None
        self._namespace = dict(plan.namespace)
        self._context = disk._contexts[id(plan)]
        self._artifact_cache = dict(self._context.cache) if self._context else {}
        self._created_files = {}
        self._owner_binding = self._context.owner_binding if self._context else None
        self._owner_seen = self._owner_binding is not None
        self._owner_locks = dict(self._context.owner_locks) if self._context else {}
        initial = dict(self._context.snapshot.observations) if self._context else {}
        self._owner_controls = {path for path in (b"data/native-operations/state.json", b"data/native/git-resources/journal.json")
                                if initial.get(disk.root + b"/" + path) is not None}

    disk = property(lambda self: self._disk)
    plan = property(lambda self: self._baseline)
    fd = property(lambda self: self._fd)
    path = property(lambda self: self._path)
    identity = property(lambda self: self._identity)
    active = property(lambda self: self._active)
    last_snapshot = property(lambda self: self._last_snapshot)
    current = property(lambda self: self._current)

    def qualify_candidate(self, git, operation_id, owner_id):
        """Issuer-registered active capability; copied/constructed values grant nothing."""
        from native_git import NativeGit
        if (type(self) is not Admission or self.disk._candidate_admission is not self or
                not self.active or self.plan is not self._issued_plan or
                self.disk._plans.get(id(self.plan)) is not self.plan or
                self.disk._active_operation != operation_id or
                self.plan.kind != "candidate" or self.plan.evidence.executor != "git-native-disk-v1" or
                (operation_id, owner_id) != (self.plan.evidence.operation_id, self.plan.evidence.owner_id) or
                not isinstance(git, NativeGit) or git.root != self.disk.root or
                git.metadata != self.disk.root + b"/data/native/repository.git" or
                git.resources_root != self.disk.root + b"/data/native/git-resources" or
                self._candidate_git is not None and self._candidate_git is not git):
            raise Blocked("issued-active-candidate-admission-required")
        if self._candidate_git is None:
            self._candidate_git = git
        self.revalidate()
        return self

    def qualify_scope(self,prepared):
        from native_operations import PreparedScope
        if (type(prepared) is not PreparedScope or self.plan.evidence.scope_preparation is not prepared or
                self.plan.kind not in ('candidate','scope_control') or not self.active or self.plan is not self._issued_plan):
            raise Blocked('issued-active-scope-admission-required')
        prepared.check();self.revalidate()
        return self

    def create_scope_directories(self,prepared):
        self.qualify_scope(prepared)
        for path in sorted(self.plan.future_directories,key=lambda p:(p.count(b'/'),p)):
            if self._namespace.get(path) is not None:continue
            self._check_namespace()
            with _directory(os.path.dirname(path)) as parent:
                os.mkdir(os.path.basename(path),0o700,dir_fd=parent)
                fd=os.open(os.path.basename(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    os.fsync(fd);os.fsync(parent)
                    self.record_directory_creation(path[len(self.disk.root)+1:],fd)
                finally:os.close(fd)

    def acknowledge_scope_blob(self,prepared,ref):
        prepared.check()
        if self.plan.evidence.scope_preparation is not prepared or (ref not in tuple(row[0] for row in prepared.payloads)):
            raise Blocked('issued-scope-payload-receipt-required')
        relative=b'data/native-operations/artifacts/'+ref.key.encode()+b'.blob'
        path=self.disk.root+b'/'+relative
        if dict(self.plan.output_files).get(path)!=('scope-blob',ref):raise Blocked('unplanned-scope-payload')
        with _directory(os.path.dirname(path)) as directory:
            with prepared.store._verified_scope_blob(directory,ref) as fd:
                info=os.fstat(fd)
                self._namespace[path]=_identity(info);self._created_files[path]=_observation(info)
        self.revalidate()

    def acknowledge_scope(self,prepared):
        prepared.check()
        if self.plan.evidence.scope_preparation is not prepared or not prepared.used or prepared.current!=prepared.state:
            raise Blocked('issued-scope-owner-receipt-required')
        outputs=[(b'data/native-operations/state.json',prepared.state),
            (b'data/native-operations/artifacts/'+hashlib.sha256(prepared.artifact).hexdigest().encode()+b'.diff',prepared.artifact),
            (b'data/native-operations/lock',b'')]
        self._publication_outputs(outputs);self.revalidate()

    def qualify_publication(self, prepared, *, failure_checkpoint=False):
        if (type(self) is not Admission or not self.active or self.disk._candidate_admission is not self or
                self.plan is not self._issued_plan or self.disk._plans.get(id(self.plan)) is not self.plan or
                self.plan.kind != "publication" or self.plan.evidence.publication is not prepared or
                self.disk._active_operation != prepared.operation_id or
                (self.plan.evidence.operation_id,self.plan.evidence.owner_id) != (prepared.operation_id,prepared.owner_id)):
            raise Blocked("issued-active-publication-admission-required")
        if self.plan.network_cap!=self._context.network_cap:
            raise Blocked('issued-network-transfer-cap-changed')
        prepared.check()
        if failure_checkpoint:
            # This grants only the already projected canonical recovery state,
            # never more Git effects or adoption of the conflicting dependency.
            from native_operations import _parse
            worker=_parse(prepared._current)['workers'][prepared.operation_id]
            if worker['phase']!='effect_started': raise Blocked('failure-checkpoint-phase-required')
            from native_publication import validate_intent
            intent=validate_intent(prepared.artifact)
            self._check_namespace(except_path=self.disk.root+b'/data/native/repository.git/'+intent.local_ref.encode())
            for path,identity in self._context.routes:
                with _directory(path) as fd:
                    if _identity(os.fstat(fd))!=identity: raise Blocked('failure-checkpoint-proof-route-changed')
            if _identity(os.fstat(self._fd))!=self._identity or _identity(os.stat(self._path,follow_symlinks=False))!=self._identity:
                raise Blocked('failure-checkpoint-reserve-owner-changed')
            for relative in (b'data/native-operations/state.json',b'data/native-operations/lock',
                    b'data/native-operations/artifacts/'+hashlib.sha256(prepared.artifact).hexdigest().encode()+b'.diff'):
                path=self.disk.root+b'/'+relative
                source=self.disk.source(path)
                if source.observation!=self._created_files.get(path):
                    raise Blocked('failure-checkpoint-owner-route-changed')
        else:
            self.revalidate()
        return self

    def create_publication_directories(self,prepared,*,transport_only=False):
        """Own planned parent creation before any Git/owner implicit mkdir."""
        self.qualify_publication(prepared)
        for path in sorted(self.plan.future_directories,key=lambda p:(p.count(b'/'),p)):
            if transport_only and not beneath(self.disk.root+b'/data/native/git-resources',path):continue
            if self._namespace.get(path) is not None: continue
            self._check_namespace()
            with _directory(os.path.dirname(path)) as parent:
                # An unexpected competing mkdir is never adopted.
                os.mkdir(os.path.basename(path),0o700,dir_fd=parent)
                fd=os.open(os.path.basename(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    os.fsync(fd); os.fsync(parent)
                    self.record_directory_creation(path[len(self.disk.root)+1:],fd)
                finally:
                    primary=__import__('sys').exc_info()[1]
                    try: os.close(fd)
                    except BaseException as secondary:
                        if primary is None: raise
                        primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('publication-directory-close:'+os.fsdecode(path)+':'+str(secondary),)

    def acknowledge_publication_owner(self, prepared, payload):
        if self.plan.kind != "publication" or self.plan.evidence.publication is not prepared or not self.active:
            raise Blocked("foreign-publication-owner-receipt")
        prepared.check()
        prepared.qualify_payload(payload)
        if payload != prepared._current or (payload not in self._context.publication_states and
                not self._publication_compiled_payload(prepared,payload)):
            raise Blocked("unprepared-publication-owner-receipt")
        outputs = [(b"data/native-operations/state.json",payload),
                   (b"data/native-operations/artifacts/" + hashlib.sha256(prepared.artifact).hexdigest().encode() + b".diff",prepared.artifact),
                   (b"data/native-operations/lock",b"")]
        self._publication_outputs(outputs)
        self.revalidate()

    def _publication_compiled_payload(self,prepared,payload):
        compiled=next((entry for entry in getattr(prepared.store,'_publication_compiled',()) if entry[:2]==(prepared,payload)),None)
        if compiled is None: return False
        template=dict(prepared.states).get(compiled[2])
        return template in self._context.publication_states and len(payload)==len(template)

    def qualify_publication_outcome(self,prepared,payload):
        """Before-write grant for the sole owner's exact issued compilation."""
        self.qualify_publication(prepared)
        prepared.qualify_payload(payload)
        if not self._publication_compiled_payload(prepared,payload):
            raise Blocked('unadmitted-compiled-publication-outcome')
        from native_operations import _parse
        self.disk._validate_operation_state(_parse(payload),self._artifact_cache,prospective=prepared)

    def _publication_outputs(self, outputs):
        """Composite receipt after one acknowledged sole-writer effect.

        Every new descriptor/byte route is independently verified before any
        receipt is installed; original forecast demand is never reclaimed.
        """
        if (self.plan.kind != "publication" and self.plan.evidence.scope_preparation is None) or not self.active:
            raise Blocked("publication-receipt-required")
        pending_files, pending_directories = {}, {}
        planned = dict(self.plan.output_files)
        for relative,payload in outputs:
            path = self.disk.root+b"/"+path_bytes(relative)
            if path not in planned:
                raise Blocked("unplanned-publication-output")
            with _directory(os.path.dirname(path)) as parent:
                fd=os.open(os.path.basename(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise Blocked("regular-publication-output-required")
                    if (self._created_files.get(path)==_observation(info) and payload is not None and
                            planned[path][0]=='bytes' and planned[path][1]==payload):
                        # An unchanged issued immutable descriptor needs no second stream.
                        pending_files[path]=(_observation(info),_identity(info))
                        continue
                    actual,source,_=self.disk._read_regular(path)
                    if payload is not None and actual != payload:
                        raise Blocked("publication-output-byte-mismatch")
                    proof=planned[path]
                    if proof[0]=='publication-packed' and actual not in tuple(raw for _,raw in proof[2]):
                        raise Blocked('unprepared-packed-publication-output')
                    if payload is None and proof[0] == "git-object":
                        import zlib
                        raw=zlib.decompress(actual)
                        if raw != proof[1]+b" "+str(len(proof[3])).encode()+b"\0"+proof[3]:
                            raise Blocked("publication-object-byte-mismatch")
                    elif payload is None and proof[0] == "source-append":
                        previous=proof[1]
                        # Retain the exact preimage commitment captured before CAS.
                        old=self._publication_preimages.get(path)
                        if old is None or actual != old+proof[2]:
                            raise Blocked("publication-reflog-byte-mismatch")
                    if _observation(info) != source.observation or _observation(os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False)) != source.observation:
                        raise Blocked("publication-output-descriptor-changed")
                    pending_files[path]=(source.observation,_identity(info))
                finally:
                    primary=__import__('sys').exc_info()[1]
                    try: os.close(fd)
                    except BaseException as secondary:
                        if primary is None: raise
                        primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('publication-output-close:'+os.fsdecode(path)+':'+str(secondary),)
        for path in self.plan.future_directories:
            if path in self._namespace and self._namespace[path] is None and _optional_stat(path):
                raise Blocked('unacknowledged-publication-directory')
        for path,identity in pending_directories.items(): self._namespace[path]=identity
        for path,(observed,identity) in pending_files.items():
            self._namespace[path]=identity; self._created_files[path]=observed

    def acknowledge_publication_ref(self, name, oid):
        relative=b"data/native/repository.git/"+name.encode("ascii")
        self._publication_outputs([(relative,oid.encode()+b"\n")])
        # Local branch updates may append only the exact frozen reflog entries.
        from native_publication import validate_intent
        intent=validate_intent(self.plan.evidence.publication.artifact)
        if name == intent.local_ref:
            for route,_ in intent.reflogs:
                self._publication_outputs([(route,None)])
        self.revalidate()

    def acknowledge_publication_lock(self,slot,path,descriptor,parent,token,payload):
        """Reopened lock adoption requires the canonical exact stat commitment."""
        prepared=self.plan.evidence.publication;self.qualify_publication(prepared)
        from native_operations import _parse,PUBLICATION_RESOURCE_EMPTY
        from native_publication import validate_intent
        from native_git import publication_resource_token
        intent=validate_intent(prepared.artifact)
        routes={'commit':intent.commit_pin,'tree':intent.tree_pin}
        expected=self.disk.root+b'/data/native/repository.git/'+(b'packed-refs.lock' if slot.endswith('-packed') else routes[slot].encode()+b'.lock')
        resources=_parse(prepared._current)['workers'][prepared.operation_id]['publication_resources']
        if path!=expected or token==PUBLICATION_RESOURCE_EMPTY or resources.get(slot)!=token or publication_resource_token(os.fstat(descriptor),os.fstat(parent))!=token or publication_resource_token(os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False),os.fstat(parent))!=token or os.pread(descriptor,len(payload)+1,0)!=payload:
            raise Blocked('exact canonical publication lock receipt required')
        if path not in dict(self.plan.output_files): raise Blocked('unadmitted recovered publication lock')
        self._namespace[path]=_identity(os.fstat(descriptor));self._created_files[path]=_observation(os.fstat(descriptor))
        self.revalidate()

    def acknowledge_publication_pin_deletion(self,name,held=()):
        from native_publication import validate_intent
        intent=validate_intent(self.plan.evidence.publication.artifact)
        if name not in (intent.tree_pin,intent.commit_pin):
            raise Blocked("only exact owned publication pins may be retired")
        path=self.disk.root+b"/data/native/repository.git/"+name.encode("ascii")
        if _optional_stat(path):
            raise Blocked("publication-pin-deletion-unacknowledged")
        self._namespace[path]=None
        self._created_files.pop(path,None)
        # A recovered pin was an original dependency; its acknowledged deletion
        # has an explicit absence receipt, not a missing-source exemption.
        self._publication_preimages[path]=None
        lock=path+b'.lock'
        if _optional_stat(lock): raise Blocked('publication-pin-lock-deletion-unacknowledged')
        if _optional_stat(self.disk.root+b'/data/native/repository.git/packed-refs.lock'): raise Blocked('packed-publication-lock-deletion-unacknowledged')
        # An interrupted deletion can already have removed both resources. The
        # caller still must execute parent durability and the actual absence ACK;
        # canonical authorization alone never issues the retirement capability.
        self._namespace[lock]=None; self._created_files.pop(lock,None)
        self._publication_preimages[lock]=None
        self.revalidate()

    def acknowledge_publication_commit(self, oid):
        relative=b"data/native/repository.git/objects/"+oid[:2].encode()+b"/"+oid[2:].encode()
        self._publication_outputs([(relative,None)])
        self.revalidate()

    def publication_packed_hardening(self,descriptor):
        """Fence the exact current packed bytes/descriptor before durability."""
        self.qualify_publication(self.plan.evidence.publication)
        path=self.disk.root+b'/data/native/repository.git/packed-refs'
        source=next((source for route,source in self._context.dependencies if route==path),None)
        observed=self._created_files.get(path,source.observation if source is not None else None)
        payload=self._publication_preimages.get(path)
        if payload is None:
            captured=self.disk._publication_packed
            if captured is None or captured[1]!=source: raise Blocked('captured packed hardening source required')
            payload=captured[0]
        if observed is None or _observation(os.fstat(descriptor))!=observed or _observation(os.stat(path,follow_symlinks=False))!=observed or os.pread(descriptor,len(payload)+1,0)!=payload:
            raise Blocked('captured packed hardening bytes changed')
        return observed

    def publication_packed_effect(self,name):
        """Exact pre-admitted transition; no post-effect packed scan."""
        self.qualify_publication(self.plan.evidence.publication)
        path=self.disk.root+b'/data/native/repository.git/packed-refs'
        proof=dict(self.plan.output_files).get(path)
        if proof is None or proof[0]!='publication-packed': return None
        before=self._publication_preimages[path]
        for removed,raw in proof[2]:
            if raw==before and name not in removed:
                wanted=set(removed)|{name}
                matches=[value for keys,value in proof[2] if set(keys)==wanted]
                if len(matches)!=1: continue
                return before,matches[0],self._created_files.get(path,proof[1].observation)
        return None

    def acknowledge_publication_packed(self,name,payload):
        path=self.disk.root+b'/data/native/repository.git/packed-refs'
        # The old descriptor intentionally disappears at atomic replacement.
        # Install only the exact precompiled transition after independent read.
        proof=dict(self.plan.output_files).get(path)
        if proof is None or proof[0]!='publication-packed': raise Blocked('packed-publication-proof-required')
        before=self._publication_preimages[path]
        valid=any(raw==before and name not in removed and any(set(keys)==set(removed)|{name} and value==payload
                  for keys,value in proof[2]) for removed,raw in proof[2])
        if not valid: raise Blocked('unprepared-packed-publication-transition')
        lock=path+b'.lock'
        if lock not in self._created_files or _optional_stat(lock): raise Blocked('packed-publication-lock-retirement-required')
        self._publication_outputs([(b'data/native/repository.git/packed-refs',payload)])
        self._publication_preimages[path]=payload
        self._namespace[lock]=None; self._created_files.pop(lock)
        return self._created_files[path]

    def _close(self):
        self._active = False
        self._artifact_cache.clear()
        self._context = None

    def record_directory_creation(self, relative, descriptor):
        self._record_creation(relative, descriptor, "directory")

    def record_file_creation(self, relative, descriptor):
        """Receipt immediately after executor-owned O_EXCL creation/write.

        The held no-follow descriptor and exact planned content are evidence;
        receipts preserve the original outstanding allocation floor.
        """
        self._record_creation(relative, descriptor, "file")

    def _record_creation(self, relative, descriptor, kind):
        if not self._active:
            raise Blocked("guard-context-ended")
        path = self._disk.root + b"/" + path_bytes(relative)
        files = dict(self._baseline.output_files)
        planned = path in (files if kind == "file" else self._baseline.future_directories)
        if not planned or path not in self._namespace or self._namespace[path] is not None:
            raise Blocked("unplanned-or-already-bound-output")
        self._check_namespace(except_path=path)
        info = os.fstat(descriptor)
        if kind == "directory":
            if not stat.S_ISDIR(info.st_mode):
                raise Blocked("created-directory-descriptor-required")
            with _directory(path) as current:
                if _identity(os.fstat(current)) != _identity(info):
                    raise Blocked("changed-created-directory")
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Blocked("created-regular-descriptor-required")
            proof = files[path]
            if proof is None:
                raise Blocked("exact-created-file-proof-unavailable")
            with _directory(os.path.dirname(path)) as parent:
                fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    if _observation(os.fstat(fd)) != _observation(info):
                        raise Blocked("changed-created-file")
                    deadline = time.monotonic() + self._disk.limits.seconds
                    def digest(fd, prefix=b"", algorithm="sha256", suffix=b""):
                        h = hashlib.new(algorithm, prefix)
                        while True:
                            if time.monotonic() >= deadline:
                                raise Blocked("created-file-verification-deadline")
                            chunk = os.read(fd, 65536)
                            if time.monotonic() >= deadline:
                                raise Blocked("created-file-verification-deadline")
                            if not chunk:
                                h.update(suffix)
                                return h.hexdigest()
                            h.update(chunk)
                    if proof[0] == "bytes":
                        valid = info.st_size == len(proof[1]) and digest(fd) == hashlib.sha256(proof[1]).hexdigest()
                    elif proof[0]=='publication-packed':
                        valid=digest(fd) in tuple(hashlib.sha256(raw).hexdigest() for _,raw in proof[2])
                    elif proof[0] == "blob":
                        valid = info.st_size == proof[2] and digest(fd, b"blob " + str(proof[2]).encode() + b"\0",
                            "sha1" if len(proof[1]) == 40 else "sha256") == proof[1]
                    else:
                        source = proof[1]
                        if self._disk.source(source.path) != source:
                            raise Blocked("changed-created-file-source")
                        with _directory(os.path.dirname(source.path)) as source_parent:
                            source_fd = os.open(os.path.basename(source.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
                            try:
                                suffix = proof[2] if proof[0] == "source-append" else b""
                                valid = info.st_size == source.size + len(suffix) and digest(fd) == digest(source_fd, suffix=suffix)
                                if _observation(os.fstat(source_fd)) != source.observation or self._disk.source(source.path) != source:
                                    raise Blocked("changed-created-file-source")
                            finally:
                                os.close(source_fd)
                    if not valid or _observation(os.fstat(fd)) != _observation(info) or _observation(os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)) != _observation(info):
                        raise Blocked("created-file-content-mismatch")
                finally:
                    os.close(fd)
        self._namespace[path] = _identity(info)
        if kind == "file":
            self._created_files[path] = _observation(info)

    def _check_namespace(self, except_path=None):
        for path, identity in self._namespace.items():
            if path == except_path:
                continue
            if identity is None:
                if _optional_stat(path):
                    raise Blocked("unacknowledged-created-destination")
            elif path in (self.disk.root + b"/data/native-operations/state.json",
                           self.disk.root + b"/data/native/git-resources/journal.json") and path not in self._created_files:
                continue
            elif not _optional_stat(path):
                raise Blocked("changed-destination-namespace")
            elif _identity(os.stat(path, follow_symlinks=False)) != identity:
                raise Blocked("changed-destination-namespace")
            elif path in self._created_files and _observation(os.stat(path, follow_symlinks=False)) != self._created_files[path]:
                raise Blocked("changed-created-file-proof")

    def _validate_context(self):
        context = self._context
        mounts = linux_mounts() if os.path.exists("/proc/self/mountinfo") else None
        for path, identity in context.routes:
            try:
                with _directory(path) as fd:
                    if _identity(os.fstat(fd)) != identity:
                        raise Blocked("changed-proof-route")
            except OSError as exc:
                raise Blocked("unavailable-proof-route:" + repr(path)) from exc
        for path, observed in context.memberships:
            try:
                with _directory(path) as fd:
                    if _observation(os.fstat(fd)) != observed:
                        raise Blocked("changed-git-object-membership:" + repr(path))
            except OSError as exc:
                raise Blocked("unavailable-object-membership-route:" + repr(path)) from exc
        for source in context.sources:
            if source.path in self._created_files:
                continue
            if self.disk.source(source.path) != source:
                raise Blocked("stale-source-evidence")
        for path, source in context.dependencies:
            if path in self._created_files:
                continue  # the exact output receipt replaced an absent dependency
            if path in self._publication_preimages and self._publication_preimages[path] is None:
                if self.disk._optional_source(path) is not None:
                    raise Blocked("retired-publication-pin-reappeared")
                continue
            try:
                current = self.disk._optional_source(path, {path: source.observation} if source is not None else None)
            except Blocked as exc:
                raise Blocked("changed-git-dependency:" + repr(path) + ":" + str(exc)) from exc
            if current != source:
                raise Blocked("changed-git-dependency:" + repr(path))
        for path, mount in context.mounts:
            current = path
            while not _optional_stat(current):
                current = os.path.dirname(current)
            info = os.stat(current, follow_symlinks=False)
            if self.disk._mount(current, info, mounts)[1] != mount:
                raise Blocked("changed-destination-mount")

    def _adopt_resource_directories(self, record):
        # Canonical same-owner journal/lease/marker checks supply the creation
        # receipt for NativeGit's fixed generated directories. Demand is frozen.
        paths = [self.disk.root + b"/data/native/git-resources"]
        if record["kind"] == "worktree":
            paths.append(self.disk.root + b"/data/native/repository.git/worktrees")
        for path in paths:
            if path in self._namespace and self._namespace[path] is None:
                with _directory(path) as fd:
                    self._namespace[path] = _identity(os.fstat(fd))

    def _validate_resources(self, journal):
        context = self._context
        counts = dict(context.resource_counts)
        original = dict(context.existing_resources)
        for rid, record in journal["records"].items():
            counts[record["kind"]] -= 1
            if (counts[record["kind"]] < 0 or len(record["entries"]) > context.resource_entries or
                    ((record["phase"] == "debt" or record["errors"]) and original.get(rid) != _digest(record)) or
                    (record["admin"] is not None and len(record["admin"]["entries"]) > 8)):
                raise Blocked("unqualified-resource-transition")
        size = len(json.dumps(journal, sort_keys=True, separators=(",", ":")).encode())
        if size > context.resource_bytes:
            raise Blocked("resource-control-exceeds-initial-envelope")

    def _validate_resource_files(self, resource, record):
        from native_git import _decode
        context = self._context
        sizes = dict(context.materialized_sizes)
        directories = {b"tree"} | {b"tree/" + b"/".join(path.split(b"/")[:n])
                                    for path in sizes for n in range(1, len(path.split(b"/")))}
        for encoded, identity in record["entries"].items():
            name = _decode(encoded)
            if record["kind"] == "index":
                allowed = name in (b"owner.json", b"index", b"index.lock")
                bound = context.index_bytes
            elif record["kind"] == "spool":
                allowed = name in (b"owner.json", b"spool")
                bound = max(context.spool_sizes, default=0)
            elif record['kind']=='transport':
                from native_publication import transport_family
                transport_family(record['entries'])
                allowed=True;bound=self.plan.network_cap
                if identity[3]=='directory':
                    with _directory(resource+b'/'+name) as fd:info=os.fstat(fd)
                    if identity[:3]!=[info.st_dev,info.st_ino,stat.S_IMODE(info.st_mode)]:
                        raise Blocked('network-resource-directory-replaced')
                    continue
                if name in (b'HEAD',b'config'):bound=512
                elif name.endswith(b'.keep'):bound=512
            else:
                allowed = name in (b"owner.json", b"tree/.git") or name in directories or (name.startswith(b"tree/") and name[5:] in sizes)
                bound = sizes.get(name[5:], 0)
            if not allowed:
                raise Blocked("unplanned-resource-output")
            if name == b"owner.json" or name == b"tree/.git":
                continue  # exact bounded raw controls are checked separately
            path = resource + b"/" + name
            if name in directories and identity[3] == "directory":
                with _directory(path) as fd:
                    info = os.fstat(fd)
            else:
                source = self.disk.source(path)
                if source.size > bound:
                    raise Blocked("resource-output-exceeds-initial-envelope")
                info = os.stat(path, follow_symlinks=False)
            if identity[:3] != [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]:
                raise Blocked("resource-output-identity-mismatch")

    def _validate_state(self, state):
        context = self._context
        if self.plan.evidence.scope_preparation is not None:
            from native_operations import canonical
            prepared=self.plan.evidence.scope_preparation;prepared.check()
            if state is None or canonical(state)!=prepared.current:
                raise Blocked('scope-owner-lifecycle-fenced')
            return
        if self.plan.kind == "publication":
            from native_operations import canonical
            prepared = self.plan.evidence.publication
            prepared.check()
            if state is None:
                if prepared._initial_bytes is not None:
                    raise Blocked("publication-owner-state-disappeared")
                return
            payload = canonical(state)
            if payload != prepared.initial:
                prepared.qualify_payload(payload)
            if payload != prepared.initial and payload not in context.publication_states and not self._publication_compiled_payload(prepared,payload):
                raise Blocked("publication-owner-lifecycle-fenced")
            if prepared._used and payload != prepared._current:
                raise Blocked("unacknowledged-publication-owner-transition")
            return
        binding = self.disk._state_binding(state)
        if self._owner_seen and binding != self._owner_binding:
            raise Blocked("operation-owner-lifecycle-fenced")
        if state is None:
            if context.state_shape is not None:
                raise Blocked("operation-state-disappeared")
            return
        if binding is not None:
            worker = state["workers"][state["active"]]
            if worker["phase"] not in ("accepted", "effect_started"):
                raise Blocked("operation-not-authorized-for-normal-effects")
        shape = self.disk._state_shape(state)
        initial = shape == context.state_shape and binding == context.owner_binding
        prospective = shape == context.prospective_shape and binding == context.prospective_owner
        if not initial and not prospective:
            raise Blocked("unqualified-operation-state-transition")
        from native_operations import canonical
        if len(canonical(state)) > context.state_bytes:
            raise Blocked("operation-control-exceeds-initial-envelope")
        if binding is not None:
            self._owner_binding, self._owner_seen = binding, True

    def revalidate(self):
        """Validate targeted authority against the immutable initial forecast."""
        if not self.active:
            raise Blocked("guard-context-ended")
        if self._disk.layout_digest != self._baseline.layout_digest:
            raise Blocked("changed-guard-layout")
        info = os.stat(self.path, follow_symlinks=False)
        if _identity(info) != self.identity or info.st_nlink != 1 or _identity(os.fstat(self.fd)) != self.identity:
            raise Blocked("replaced-admission-lock")
        if self._baseline.unknown or not self._baseline.decisions or self._context is None:
            raise Blocked("cannot-establish-admission:" + repr(self._baseline.unknown))
        if any(d.status == "insufficient-space" for d in self._baseline.decisions):
            raise InsufficientSpace("insufficient-space:" + repr(self._baseline.decisions))
        self.disk._competitors(self.plan.evidence, self._artifact_cache, self)
        self._check_namespace()
        self._validate_context()
        self._current = self._baseline
        return self._current
