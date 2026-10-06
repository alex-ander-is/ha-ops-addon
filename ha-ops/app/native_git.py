"""Dormant local Git plumbing. Byte identities never imply live-write authority."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import base64
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
import uuid

from native_inventory import Blocked, Policy, path_bytes, linux_mounts, beneath

CHUNK = 64 * 1024
_REGULAR_MODES = (b"100644", b"100755")
_MODES = (b"000000", b"100644", b"100755", b"120000", b"160000", b"040000")
_MUTEXES = {}
_MUTEX_GUARD = threading.Lock()


class InvalidInput(Blocked):
    pass


class GitFailure(RuntimeError):
    def __init__(self, reason, *, stderr=b"", cleanup_errors=()):
        super().__init__(reason)
        self.stderr = stderr
        self.cleanup_errors = tuple(cleanup_errors)


class RefConflict(GitFailure):
    pass


class RefUncertain(GitFailure):
    def __init__(self, before, after, cause):
        super().__init__("ref-effect-uncertain")
        self.before, self.after, self.cause = before, after, cause


class CleanupDebt(GitFailure):
    def __init__(self, result):
        super().__init__("resource-cleanup-debt", cleanup_errors=result.cleanup_errors)
        self.result = result


@dataclass(frozen=True)
class StatusEntry:
    kind: bytes
    path: bytes
    original_path: bytes | None = None
    xy: bytes = b""
    fields: tuple = ()

    @property
    def display_path(self):
        return repr(os.fsdecode(self.path))[1:-1]


@dataclass(frozen=True)
class IndexEntry:
    path: bytes
    mode: bytes
    oid: str
    stage: int


@dataclass(frozen=True)
class TreeEntry:
    path: bytes
    mode: bytes
    oid: str
    kind: bytes


@dataclass(frozen=True)
class RefSnapshot:
    name: str
    oid: str | None


@dataclass(frozen=True)
class BlobResult:
    oid: str
    size: int


@dataclass(frozen=True)
class ResourceHandle:
    resource_id: str
    operation_id: str
    owner_id: str
    kind: str
    _binding: str


@dataclass(frozen=True)
class CleanupResult:
    resource_id: str
    state: str
    cleanup_errors: tuple = ()


def _oid(value, width, *, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % width, value):
        raise InvalidInput("full-lowercase-object-id-required")
    if not zero and value == "0" * width:
        raise InvalidInput("zero-object-id")
    return value


def _tokens(data):
    if not isinstance(data, bytes) or (data and not data.endswith(b"\0")):
        raise InvalidInput("truncated-nul-output")
    return data[:-1].split(b"\0") if data else []


def _path(value):
    try:
        return path_bytes(value)
    except (Blocked, TypeError) as exc:
        raise InvalidInput("noncanonical-relative-path") from exc


def parse_index(data, width=40):
    result = []
    seen = set()
    for token in _tokens(data):
        try:
            head, path = token.split(b"\t", 1)
            mode, oid, stage = head.split(b" ")
            if mode not in _MODES[1:] or stage not in (b"0", b"1", b"2", b"3"):
                raise ValueError()
            path = _path(path[:-1] if mode == b"040000" and path.endswith(b"/") else path)
            entry = IndexEntry(path, mode, _oid(oid.decode("ascii"), width), int(stage))
            if (path, entry.stage) in seen:
                raise ValueError()
            seen.add((path, entry.stage))
            result.append(entry)
        except (ValueError, UnicodeError) as exc:
            raise InvalidInput("malformed-index-output") from exc
    return tuple(result)


def parse_tree(data, width=40):
    result, seen = [], set()
    for token in _tokens(data):
        try:
            head, path = token.split(b"\t", 1)
            mode, kind, oid = head.split(b" ")
            pairs = {b"100644": b"blob", b"100755": b"blob", b"120000": b"blob", b"160000": b"commit", b"040000": b"tree"}
            if pairs.get(mode) != kind:
                raise ValueError()
            path = _path(path)
            if path in seen:
                raise ValueError()
            seen.add(path)
            result.append(TreeEntry(path, mode, _oid(oid.decode("ascii"), width), kind))
        except (ValueError, UnicodeError) as exc:
            raise InvalidInput("malformed-tree-output") from exc
    return tuple(result)


def parse_status(data, width=40):
    tokens, result, index = _tokens(data), [], 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        kind = token[:1]
        try:
            if kind in (b"?", b"!"):
                if token[:2] != kind + b" ":
                    raise ValueError()
                result.append(StatusEntry(kind, _path(token[2:])))
                continue
            count = {b"1": 8, b"2": 9, b"u": 10}.get(kind)
            if count is None:
                raise ValueError()
            fields = token.split(b" ", count)
            if len(fields) != count + 1 or fields[0] != kind:
                raise ValueError()
            xy, sub = fields[1:3]
            # Git collects HEAD/index and index/worktree axes independently.
            # Intent-to-add can therefore produce DA, not just the usual D.
            ordinary = bool(re.fullmatch(rb"[.MTAD]{2}", xy)) and xy not in (b"..", b"AA")
            renamed = bool(re.fullmatch(rb"(?:[RC][.MTAD]|[.MTAD][RC])", xy))
            unmerged = {b"DD", b"AU", b"UD", b"UA", b"DU", b"AA", b"UU"}
            if not {b"1": ordinary, b"2": renamed, b"u": xy in unmerged}[kind] or not re.fullmatch(rb"(?:N\.\.\.|S[.C][.M][.U])", sub):
                raise ValueError()
            modes = fields[3:7] if kind == b"u" else fields[3:6]
            ids = fields[7:10] if kind == b"u" else fields[6:8]
            if any(m not in _MODES for m in modes):
                raise ValueError()
            # A removed intent-to-add entry is ordinary DD, with no worktree
            # mode. Conflict DD instead uses the explicit u/stage record shape.
            if kind == b"1" and xy == b"DD" and modes[2] != b"000000":
                raise ValueError()
            for oid in ids:
                _oid(oid.decode("ascii"), width, zero=True)
            original = None
            if kind == b"2":
                if (not re.fullmatch(rb"[RC](?:100|[1-9]?[0-9])", fields[8])
                        or fields[8][:1] not in xy or index == len(tokens)):
                    raise ValueError()
                original = _path(tokens[index])
                index += 1
            result.append(StatusEntry(kind, _path(fields[-1]), original, xy, tuple(fields[1:-1])))
        except (ValueError, UnicodeError) as exc:
            raise InvalidInput("malformed-status-output") from exc
    return tuple(result)


def _identity(path, *, directory=False):
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise InvalidInput("unsafe-filesystem-route")
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]


def _components(path, *, missing=False):
    path = os.path.abspath(os.fsencode(path))
    current = b"/"
    for part in path.split(b"/")[1:]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if missing:
                return path
            raise InvalidInput("missing-filesystem-anchor")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise InvalidInput("unsafe-filesystem-anchor")
    return path


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError) as exc:
        raise InvalidInput("canonical-uuid-required") from exc
    return value


def _encode(path):
    return base64.b64encode(path).decode("ascii")


def _decode(value):
    try:
        result = base64.b64decode(value, validate=True)
        if _encode(result) != value:
            raise ValueError()
        return _path(result)
    except (ValueError, TypeError) as exc:
        raise InvalidInput("invalid-resource-path") from exc


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidInput("duplicate-journal-key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(InvalidInput("invalid-journal-number")))
    except InvalidInput:
        raise
    except (ValueError, UnicodeError) as exc:
        raise InvalidInput("invalid-resource-journal") from exc


def _write_all(sink, data):
    view = memoryview(data)
    while view:
        count = sink.write(view[:CHUNK])
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or count > min(len(view), CHUNK):
            raise GitFailure("invalid-or-short-sink-write")
        view = view[count:]


def _serialized_index(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        # One caller can validate, act and acknowledge a given issued index at
        # a time. Other processes cannot acquire the held resource lease.
        with self._mutex:
            return method(self, *args, **kwargs)
    return call


class NativeGit:
    """No subprocess, allocation or recovery occurs until an explicit method call."""
    def __init__(self, root, *, metadata=None, resources=None, timeout=30, policy=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise InvalidInput("finite-positive-timeout-required")
        self.root = _components(root)
        self.metadata = _components(metadata or os.path.join(self.root, b"data/native/repository.git"))
        self.resources_root = _components(resources or os.path.join(self.root, b"data/native/git-resources"), missing=True)
        if self.metadata != os.path.join(self.root, b"data/native/repository.git") or self.resources_root != os.path.join(self.root, b"data/native/git-resources"):
            raise InvalidInput("fixed-native-anchors-required")
        self.timeout, self.policy = timeout, policy or Policy()
        self._root_id = _identity(self.root, directory=True)
        self._meta_id = _identity(self.metadata, directory=True)
        self._active = {}
        self._storage_id = _identity(self.resources_root, directory=True) if os.path.lexists(self.resources_root) else None
        with _MUTEX_GUARD:
            self._mutex = _MUTEXES.setdefault(self.resources_root, threading.RLock())
        self._validate_metadata()

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise InvalidInput("finite-positive-timeout-required")
        self._timeout = value

    def _validate_metadata(self):
        _components(self.root)
        _components(self.metadata)
        if _identity(self.root, directory=True) != self._root_id or _identity(self.metadata, directory=True) != self._meta_id:
            raise InvalidInput("replaced-native-anchor")
        for forbidden in (b"commondir", b"gitdir", b"config.worktree", b"objects/info/alternates", b"objects/info/http-alternates"):
            if os.path.lexists(os.path.join(self.metadata, forbidden)):
                raise InvalidInput("unsupported-metadata-route")
        # Routing audit is independent of loose-object history: at most 256 fanout
        # directories, pack/info headers, ref routes and currently linked admin files.
        # The exact loose-object leaf is checked separately before its read/write.
        for item in os.scandir(self.metadata):
            if item.name.startswith(b"sharedindex."):
                raise InvalidInput("split-index-not-supported")
            if item.is_dir(follow_symlinks=False):
                _identity(item.path, directory=True)
            else:
                _identity(item.path)
        for route in (b"refs", b"logs", b"worktrees"):
            anchor = os.path.join(self.metadata, route)
            if not os.path.lexists(anchor):
                continue
            _identity(anchor, directory=True)
            for parent, directories, files in os.walk(anchor, followlinks=False):
                for name in directories:
                    _identity(os.path.join(parent, name), directory=True)
                for name in files:
                    try:
                        _identity(os.path.join(parent, name))
                    except FileNotFoundError:
                        # Concurrent Git CAS lock disappearance is not unsafe routing.
                        if not name.endswith(b".lock"):
                            raise
        objects = os.path.join(self.metadata, b"objects")
        _identity(objects, directory=True)
        for item in os.scandir(objects):
            _identity(item.path, directory=True)
            if item.name in (b"pack", b"info"):
                for child in os.scandir(item.path):
                    _identity(child.path)
            elif not re.fullmatch(rb"[0-9a-f]{2}", item.name):
                raise InvalidInput("unexpected-object-storage-route")
        _identity(os.path.join(self.metadata, b"HEAD"))
        _identity(os.path.join(self.metadata, b"config"))

    def _loose_route(self, oid):
        path = os.path.join(self.metadata, b"objects", oid[:2].encode(), oid[2:].encode())
        _components(os.path.dirname(path), missing=True)
        if os.path.lexists(path):
            _identity(path)

    def _environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
        return env

    def _argv(self, command, *, index=None, worktree=None):
        self._validate_metadata()
        env = self._environment()
        if index is not None:
            env["GIT_INDEX_FILE"] = os.fsdecode(index)
        options = ["core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.attributesFile=" + os.devnull,
                   "core.untrackedCache=false", "core.splitIndex=false", "gc.auto=0", "maintenance.auto=false",
                   "diff.external=", "protocol.allow=never", "submodule.recurse=false", "core.sparseCheckout=false"]
        argv = ["git", "--no-pager", "--literal-pathspecs", "--git-dir=" + os.fsdecode(self.metadata),
                "--work-tree=" + os.fsdecode(worktree or self.root)]
        for option in options:
            argv.extend(("-c", option))
        return argv + list(command), env

    def _raw(self, command, *, index=None, worktree=None, input=None, stdin=None, accepted=(0,), reject_stderr=False):
        argv, env = self._argv(command, index=index, worktree=worktree)
        process = subprocess.Popen(argv, cwd=self.root, env=env, stdin=stdin if stdin is not None else subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector, output, error = selectors.DefaultSelector(), bytearray(), bytearray()
        pending = memoryview(input or b"")
        deadline = time.monotonic() + self.timeout
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if stdin is None:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                else:
                    process.stdin.close()
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv, self.timeout)
                for key, events in selector.select(min(left, .1)):
                    if events & selectors.EVENT_WRITE:
                        try:
                            count = os.write(key.fd, pending[:CHUNK])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        output.extend(chunk)
                    elif len(error) < CHUNK:
                        error.extend(chunk[:CHUNK - len(error)])
            process.wait(timeout=max(.001, deadline - time.monotonic()))
            if process.returncode not in accepted:
                raise GitFailure("git-command-failed", stderr=bytes(error))
            if reject_stderr and error:
                raise GitFailure("git-observation-diagnostics", stderr=bytes(error))
            return process.returncode, bytes(output)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _config(self):
        _, output = self._raw(("config", "--null", "--local", "--no-includes", "--list"))
        width = 40
        for token in _tokens(output):
            key, sep, value = token.partition(b"\n")
            key = key.lower()
            if not sep:
                value = b""
            if key.startswith((b"include.", b"includeif.", b"filter.")) or key == b"extensions.worktreeconfig":
                raise InvalidInput("execution-capable-or-routed-config")
            if key == b"extensions.objectformat":
                if value not in (b"sha1", b"sha256"):
                    raise InvalidInput("unsupported-object-format")
                width = 64 if value == b"sha256" else 40
        return width

    def _run(self, command, **kwargs):
        self._config()
        return self._raw(command, **kwargs)[1]

    def _object(self, oid, kind=None):
        width = self._config()
        _oid(oid, width)
        self._loose_route(oid)
        actual = self._raw(("cat-file", "-t", oid))[1].rstrip(b"\n")
        if actual not in (b"blob", b"tree", b"commit", b"tag") or (kind and actual not in (kind if isinstance(kind, tuple) else (kind,))):
            raise InvalidInput("unexpected-object-type")
        return oid

    def _tree_routes(self, oid):
        self._object(oid, (b"tree", b"commit"))
        actual = self._run(("cat-file", "-t", oid)).strip()
        if actual == b"commit":
            data = self._run(("cat-file", "commit", oid))
            first = data.split(b"\n", 1)[0]
            if not first.startswith(b"tree "):
                raise InvalidInput("malformed-commit-tree")
            oid = _oid(first[5:].decode("ascii"), self._config())
        pending, seen = [oid], set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            self._object(current, b"tree")
            for entry in parse_tree(self._run(("ls-tree", "-z", current)), self._config()):
                self._loose_route(entry.oid)
                if entry.kind == b"tree":
                    pending.append(entry.oid)

    def _head_routes(self):
        with open(os.path.join(self.metadata, b"HEAD"), "rb") as stream:
            data = stream.read()
        if data.startswith(b"ref: "):
            name = data[5:].rstrip(b"\n").decode("ascii")
            oid = self.read_ref(name).oid
            if oid is None:
                return  # An unborn HEAD has no object route.
        else:
            oid = _oid(data.decode("ascii").rstrip("\n"), self._config())
        self._tree_routes(oid)

    def status(self, *, untracked_paths=()):
        paths = tuple(_path(p) for p in untracked_paths)
        command = ["status", "--porcelain=v2", "-z", "--ignore-submodules=all", "--untracked-files=no"]
        if paths:
            for path in paths:
                candidate = os.path.join(self.root, path)
                _components(os.path.dirname(candidate))
                _identity(candidate)
        width = self._config()
        for entry in self.index_entries():
            self._loose_route(entry.oid)
        self._head_routes()
        tracked = parse_status(self._raw(command)[1], width)
        if not paths:
            return tracked
        # A pathspec scopes all status kinds. Keep complete tracked status from
        # its own query, and accept only exact requested untracked identities.
        requested = set(paths)
        command[-1] = "--untracked-files=all"
        command += ["--", *map(os.fsdecode, paths)]
        extra = parse_status(self._raw(command)[1], width)
        seen = {entry.path for entry in tracked}
        result = list(tracked)
        for entry in extra:
            if entry.kind == b"?" and entry.path in requested and entry.path not in seen:
                result.append(entry)
                seen.add(entry.path)
        return tuple(result)

    @_serialized_index
    def index_entries(self, handle=None):
        index = self._index(handle) if handle else None
        width = self._config()
        return parse_index(self._raw(("ls-files", "--stage", "-z", "--sparse"), index=index)[1], width)

    def tree_entries(self, oid):
        self._tree_routes(oid)
        # A commit is an explicit OID; its tree is Git's structural object, not revision syntax.
        return parse_tree(self._run(("ls-tree", "-r", "-z", "--full-tree", oid)), self._config())

    def _ref_name(self, name):
        if not isinstance(name, str) or not name.startswith("refs/") or any(c in name for c in "\x00\r\n"):
            raise InvalidInput("qualified-direct-ref-required")
        self._run(("check-ref-format", name))
        path = os.path.join(self.metadata, os.fsencode(name))
        _components(os.path.dirname(path), missing=True)
        if os.path.lexists(path):
            _identity(path)
            with open(path, "rb") as stream:
                if stream.read(4) == b"ref:":
                    raise InvalidInput("symbolic-ref-not-supported")
        return name

    def _packed_ref_storage(self, width):
        packed = os.path.join(self.metadata, b"packed-refs")
        if not os.path.lexists(packed):
            return {}
        _identity(packed)
        seen, previous, peeled, header, ordered = {}, None, False, False, False
        with open(packed, "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise InvalidInput("malformed-packed-ref-storage")
                line = line[:-1]
                if line.startswith(b"# pack-refs with:") and previous is None and not seen:
                    if header:
                        raise InvalidInput("malformed-packed-ref-storage")
                    header, ordered = True, b"sorted" in line.split()
                    continue
                if line.startswith(b"^"):
                    if previous is None or peeled:
                        raise InvalidInput("malformed-packed-ref-storage")
                    try:
                        oid = _oid(line[1:].decode("ascii"), width)
                    except UnicodeError as exc:
                        raise InvalidInput("malformed-packed-ref-storage") from exc
                    self._object(oid)
                    peeled = True
                    continue
                fields = line.split(b" ")
                if len(fields) != 2 or not fields[1].startswith(b"refs/") or fields[1] in seen:
                    raise InvalidInput("malformed-packed-ref-storage")
                if ordered and previous is not None and fields[1] <= previous:
                    raise InvalidInput("malformed-packed-ref-order")
                try:
                    oid = _oid(fields[0].decode("ascii"), width)
                    name = os.fsdecode(fields[1])
                    self._raw(("check-ref-format", name), reject_stderr=True)
                    self._object(oid)
                except (ValueError, UnicodeError) as exc:
                    raise InvalidInput("malformed-packed-ref-storage") from exc
                seen[fields[1]] = oid
                previous, peeled = fields[1], False
        return seen

    def read_ref(self, name):
        self._ref_name(name)
        width = self._config()
        packed = self._packed_ref_storage(width)
        loose = os.path.join(self.metadata, os.fsencode(name))
        if os.path.lexists(loose):
            # Git --quiet also returns 1 for some broken existing loose refs.
            # Their existence cannot be converted into expected-absence authority.
            with open(loose, "rb") as stream:
                data = stream.read(width + 2)
            if not re.fullmatch(rb"[0-9a-f]{%d}\n?" % width, data):
                raise InvalidInput("malformed-existing-loose-ref")
            _oid(data.rstrip(b"\n").decode("ascii"), width)
        code, quiet_output = self._raw(("show-ref", "--verify", "--quiet", name), accepted=(0, 1), reject_stderr=True)
        if quiet_output:
            raise InvalidInput("malformed-quiet-ref-output")
        if code == 1:
            if os.path.lexists(loose) or os.fsencode(name) in packed:
                raise InvalidInput("existing-ref-observation-unavailable")
            return RefSnapshot(name, None)
        output = self._run(("show-ref", "--verify", "--hash", name), reject_stderr=True)
        try:
            value = output.decode("ascii")
            if not value.endswith("\n") or value.count("\n") != 1:
                raise ValueError()
            oid = _oid(value[:-1], width)
            self._loose_route(oid)
            return RefSnapshot(name, oid)
        except (ValueError, UnicodeError) as exc:
            raise InvalidInput("malformed-ref-output") from exc

    def _ref_change(self, name, new, expected_old):
        self._ref_name(name)
        width = self._config()
        old = "0" * width if expected_old is None else _oid(expected_old, width)
        if new is not None:
            self._object(new)
        elif expected_old is None:
            raise InvalidInput("deletion-requires-expected-object-id")
        before = self.read_ref(name)
        command = ("update-ref", "--no-deref", name, new, old) if new else ("update-ref", "--no-deref", "-d", name, old)
        try:
            self._run(command)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try:
                after = self.read_ref(name)
            except BaseException:
                after = None
            raise RefUncertain(before, after, exc) from exc
        except GitFailure as exc:
            if self.read_ref(name).oid != expected_old:
                raise RefConflict("ref-expectation-conflict", stderr=exc.stderr) from exc
            raise
        return self.read_ref(name)

    def update_ref(self, name, new_oid, *, expected_old):
        return self._ref_change(name, new_oid, expected_old)

    def delete_ref(self, name, *, expected_old):
        return self._ref_change(name, None, expected_old)

    def copy_blob(self, oid, destination):
        return self._copy_object(oid, b"blob", destination)

    def _copy_object(self, oid, kind, destination):
        if kind not in (b"blob", b"tree"):
            raise InvalidInput("unsupported-exact-stream-object-kind")
        self._object(oid, kind)
        declared = self._run(("cat-file", "-s", oid))
        if not re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", declared):
            raise InvalidInput("malformed-blob-size")
        expected_size = int(declared)
        digest = hashlib.sha256() if len(oid) == 64 else hashlib.sha1()
        digest.update(kind + b" " + str(expected_size).encode() + b"\0")
        argv, env = self._argv(("cat-file", kind.decode("ascii"), oid))
        process = subprocess.Popen(argv, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector, size, errors = selectors.DefaultSelector(), 0, bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv, self.timeout)
                for key, _ in selector.select(min(left, .1)):
                    chunk = os.read(key.fd, CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        _write_all(destination, chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    elif len(errors) < CHUNK:
                        errors.extend(chunk[:CHUNK - len(errors)])
            process.wait(timeout=max(.001, deadline - time.monotonic()))
            if process.returncode:
                raise GitFailure(kind.decode("ascii") + "-read-failed", stderr=bytes(errors))
            if size != expected_size or digest.hexdigest() != oid:
                raise GitFailure(kind.decode("ascii") + "-size-or-object-id-mismatch")
            return BlobResult(oid, size)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

    def _verify_stored_object(self, oid, kind):
        class Discard:
            def write(self, data):
                return len(data)
        return self._copy_object(oid, kind, Discard())

    def _preflight_object_write(self, oid, kind):
        self._loose_route(oid)
        code, _ = self._raw(("cat-file", "-e", oid), accepted=(0, 1))
        if code == 0:
            self._verify_stored_object(oid, kind)

    def _selection(self, paths):
        result, seen = [], set()
        for value in paths:
            path = _path(value)
            reason = self.policy.reason(path)
            if reason:
                raise InvalidInput(reason)
            if path in seen:
                raise InvalidInput("duplicate-selected-path")
            seen.add(path)
            result.append(path)
        for path in result:
            if any(path.startswith(other + b"/") for other in seen if path != other):
                raise InvalidInput("file-parent-collision")
        return tuple(result)

    def _mkdir_storage(self):
        _components(os.path.dirname(self.resources_root))
        try:
            os.mkdir(self.resources_root, 0o700)
        except FileExistsError:
            pass
        identity = _identity(self.resources_root, directory=True)
        if identity[2] != 0o700:
            raise InvalidInput("private-resource-directory-required")
        if self._storage_id is not None and self._storage_id != identity:
            raise InvalidInput("replaced-resource-storage")
        self._storage_id = identity

    @contextmanager
    def _journal(self, *, create=False):
        with self._mutex:
            if create:
                self._mkdir_storage()
            if not os.path.lexists(self.resources_root):
                yield None
                return
            _components(self.resources_root)
            identity = _identity(self.resources_root, directory=True)
            if identity[2] != 0o700 or (self._storage_id is not None and identity != self._storage_id):
                raise InvalidInput("replaced-or-nonprivate-resource-storage")
            self._storage_id = identity
            lockpath = os.path.join(self.resources_root, b"journal.lock")
            if not create and not os.path.lexists(lockpath):
                if os.path.lexists(os.path.join(self.resources_root, b"journal.json")):
                    raise InvalidInput("missing-stable-journal-lock")
                yield None
                return
            fd = os.open(lockpath, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW, 0o600)
            try:
                identity = _identity(lockpath)
                if identity[2] != 0o600:
                    raise InvalidInput("private-journal-lock-required")
                fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_ino != identity[1]:
                    raise InvalidInput("replaced-journal-lock")
                manifest = os.path.join(self.resources_root, b"journal.json")
                if os.path.lexists(manifest):
                    if _identity(manifest)[2] != 0o600:
                        raise InvalidInput("private-journal-required")
                    with open(manifest, "rb") as stream:
                        journal = _json(stream.read())
                    self._validate_journal(journal)
                else:
                    journal = {"schema": 1, "root": self._root_id, "metadata": self._meta_id, "storage": self._storage_id, "records": {}}
                yield journal
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _validate_journal(self, journal):
        if (not isinstance(journal, dict) or set(journal) != {"schema", "root", "metadata", "storage", "records"}
                or type(journal["schema"]) is not int or journal["schema"] != 1
                or journal["root"] != self._root_id or journal["metadata"] != self._meta_id or journal["storage"] != self._storage_id
                or not isinstance(journal["records"], dict)):
            raise InvalidInput("invalid-resource-journal")
        for key in ("root", "metadata", "storage"):
            identity = journal[key]
            if not isinstance(identity, list) or len(identity) != 3 or any(type(i) is not int or i < 0 for i in identity):
                raise InvalidInput("invalid-journal-anchor-identity")
        for rid, record in journal["records"].items():
            _uuid(rid)
            if not isinstance(record, dict) or set(record) != {"operation", "owner", "kind", "phase", "root", "lease", "entries", "admin", "errors"}:
                raise InvalidInput("invalid-resource-record")
            _uuid(record["operation"])
            _uuid(record["owner"])
            if record["kind"] not in ("index", "worktree", "spool") or record["phase"] not in ("intent", "allocated", "creating", "ready", "debt"):
                raise InvalidInput("invalid-resource-phase")
            for identity in (record["root"], record["lease"]):
                if identity is not None and (not isinstance(identity, list) or len(identity) != 3 or any(type(i) is not int or i < 0 for i in identity)):
                    raise InvalidInput("invalid-resource-identity")
            if not isinstance(record["entries"], dict) or not isinstance(record["errors"], list) or any(not isinstance(e, str) for e in record["errors"]):
                raise InvalidInput("invalid-resource-record")
            for name, item in record["entries"].items():
                _decode(name)
                if not isinstance(item, list) or len(item) != 4 or any(type(i) is not int or i < 0 for i in item[:3]) or not isinstance(item[3], str) or (item[3] != "directory" and not re.fullmatch(r"[0-9a-f]{64}", item[3])):
                    raise InvalidInput("invalid-entry-identity")
            admin = record["admin"]
            if admin is not None:
                if (record["kind"] != "worktree" or not isinstance(admin, dict) or set(admin) != {"name", "identity", "entries"}
                        or not isinstance(admin["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", admin["name"])
                        or not isinstance(admin["entries"], dict)):
                    raise InvalidInput("invalid-admin-binding")
                if not isinstance(admin["identity"], list) or len(admin["identity"]) != 3 or any(type(i) is not int or i < 0 for i in admin["identity"]):
                    raise InvalidInput("invalid-admin-binding")
                for name, item in admin["entries"].items():
                    _decode(name)
                    if not isinstance(item, list) or len(item) != 4 or any(type(i) is not int or i < 0 for i in item[:3]) or not isinstance(item[3], str) or (item[3] != "directory" and not re.fullmatch(r"[0-9a-f]{64}", item[3])):
                        raise InvalidInput("invalid-admin-binding")

    def _publish(self, journal):
        data = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
        name = os.path.join(self.resources_root, (".publish-" + str(uuid.uuid4())).encode())
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, os.path.join(self.resources_root, b"journal.json"))
            self._fsync(self.resources_root)
        finally:
            if os.path.lexists(name):
                os.unlink(name)

    @staticmethod
    def _fsync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _resource_path(self, rid):
        return os.path.join(self.resources_root, rid.encode())

    def _lease_path(self, rid):
        return os.path.join(self.resources_root, (rid + ".lease").encode())

    @contextmanager
    def _parent_fd(self, root, path):
        _path(path)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.split(b"/")[:-1]:
                nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nextfd
            yield fd, path.split(b"/")[-1]
        finally:
            os.close(fd)

    def _record_file(self, root, path, *, directory=False):
        with self._parent_fd(root, path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if ((directory and not stat.S_ISDIR(info.st_mode)) or
                    (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
                raise InvalidInput("unsafe-filesystem-route")
            identity = [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]
            digest = "directory"
            if not directory:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    actual = os.fstat(fd)
                    if (actual.st_dev, actual.st_ino, actual.st_nlink) != (info.st_dev, info.st_ino, 1):
                        raise InvalidInput("replaced-owned-file")
                    h = hashlib.sha256()
                    while chunk := os.read(fd, CHUNK):
                        h.update(chunk)
                    digest = h.hexdigest()
                finally:
                    os.close(fd)
            return identity + [digest]

    def _unlink_file(self, root, path, item):
        if self._record_file(root, path) != item:
            raise InvalidInput("replaced-owned-file")
        with self._parent_fd(root, path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)] != item[:3] or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InvalidInput("replaced-owned-file")
            os.unlink(name, dir_fd=parent)

    def _snapshot(self, root):
        result = {}
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = os.path.relpath(os.path.join(parent, name), root)
                result[_encode(path)] = self._record_file(root, path, directory=name in dirs)
        return result

    def _sync_snapshot(self, root, entries):
        directories = []
        for encoded, item in entries.items():
            path = _decode(encoded)
            if item[3] == "directory":
                directories.append(os.path.join(root, path))
                continue
            with self._parent_fd(root, path) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    if os.fstat(fd).st_ino != item[1]:
                        raise InvalidInput("replaced-owned-file")
                    os.fsync(fd)
                finally:
                    os.close(fd)
        for directory in sorted(directories, key=lambda p: -p.count(b"/")):
            self._fsync(directory)
        self._fsync(root)

    def _capture_paths(self, handle, paths):
        with self._journal() as journal:
            record = self._handle_record(handle, journal)
            root = self._resource_path(handle.resource_id)
            entries = {_encode(path): self._record_file(root, path) for path in paths}
            self._sync_snapshot(root, entries)
            record["entries"].update(entries)
            self._publish(journal)

    def _capture(self, handle, *, phase="ready"):
        with self._journal() as journal:
            record = self._handle_record(handle, journal)
            current = self._snapshot(self._resource_path(handle.resource_id))
            names = set(map(_decode, current))
            if record["kind"] == "index" and not names <= {b"owner.json", b"index", b"index.lock"}:
                raise InvalidInput("unknown-index-resource-entry")
            if record["kind"] == "spool" and not names <= {b"owner.json", b"spool"}:
                raise InvalidInput("unknown-spool-resource-entry")
            self._sync_snapshot(self._resource_path(handle.resource_id), current)
            record["entries"] = current
            record["phase"] = phase
            self._publish(journal)

    @contextmanager
    def _capture_after(self, handle):
        original = None
        try:
            yield
        except BaseException as exc:
            original = exc
            raise
        finally:
            try:
                self._capture(handle)
            except BaseException as exc:
                if original is None:
                    raise
                original.cleanup_errors = tuple(getattr(original, "cleanup_errors", ())) + ("ownership-publication:" + str(exc),)

    def _handle_record(self, handle, journal, *, check_owner=None):
        if not isinstance(handle, ResourceHandle) or handle._binding != self._binding() or handle.resource_id not in self._active or self._active[handle.resource_id][1] is not handle:
            raise InvalidInput("closed-or-foreign-resource-handle")
        record = journal["records"].get(handle.resource_id) if journal else None
        if not record or (record["operation"], record["owner"], record["kind"]) != (handle.operation_id, handle.owner_id, handle.kind):
            raise InvalidInput("resource-binding-mismatch")
        (check_owner or self._check_owner)(handle.resource_id, record)
        return record

    def _binding(self):
        return hashlib.sha256(self.root + b"\0" + self.metadata + repr((self._root_id, self._meta_id)).encode()).hexdigest()

    def _check_owner(self, rid, record):
        root = self._resource_path(rid)
        if _identity(root, directory=True) != record["root"]:
            raise InvalidInput("replaced-resource-root")
        marker = os.path.join(root, b"owner.json")
        expected = {"resource": rid, "operation": record["operation"], "owner": record["owner"], "binding": self._binding()}
        encoded = json.dumps(expected, sort_keys=True).encode()
        item = record["entries"].get(_encode(b"owner.json"))
        if not item or self._record_file(root, b"owner.json") != item:
            raise InvalidInput("unproven-resource-marker")
        with open(marker, "rb") as stream:
            if stream.read() != encoded:
                raise InvalidInput("foreign-resource-marker")

    @contextmanager
    def _resource(self, kind, operation_id, owner_id):
        _uuid(operation_id)
        _uuid(owner_id)
        rid, lease = str(uuid.uuid4()), None
        handle = ResourceHandle(rid, operation_id, owner_id, kind, self._binding())
        original = None
        try:
            with self._journal(create=True) as journal:
                record = {"operation": operation_id, "owner": owner_id, "kind": kind, "phase": "intent", "root": None,
                          "lease": None, "entries": {}, "admin": None, "errors": []}
                journal["records"][rid] = record
                self._publish(journal)
                lease = os.open(self._lease_path(rid), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                fcntl.flock(lease, fcntl.LOCK_EX)
                record["lease"] = _identity(self._lease_path(rid))
                self._publish(journal)
                root = self._resource_path(rid)
                os.mkdir(root, 0o700)
                record["root"] = _identity(root, directory=True)
                self._publish(journal)
                marker = {"resource": rid, "operation": operation_id, "owner": owner_id, "binding": self._binding()}
                fd = os.open(os.path.join(root, b"owner.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json.dumps(marker, sort_keys=True).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                self._fsync(root)
                self._fsync(self.resources_root)
                record["entries"] = self._snapshot(root)
                record["phase"] = "allocated"
                self._publish(journal)
                self._active[rid] = (lease, handle)
            yield handle
        except BaseException as exc:
            original = exc
            raise
        finally:
            self._active.pop(rid, None)
            try:
                result = self._cleanup(rid, owner_id, operation_id, held_lease=lease)
                if result.cleanup_errors:
                    if original is not None:
                        original.cleanup_errors = tuple(getattr(original, "cleanup_errors", ())) + result.cleanup_errors
                    else:
                        raise CleanupDebt(result)
            except BaseException as cleanup:
                if original is not None:
                    original.cleanup_errors = tuple(getattr(original, "cleanup_errors", ())) + (str(cleanup),)
                else:
                    raise
            finally:
                if lease is not None:
                    fcntl.flock(lease, fcntl.LOCK_UN)
                    os.close(lease)

    def _index(self, handle, *, initialize=False):
        with self._journal() as journal:
            record = self._handle_record(handle, journal)
            if record["kind"] != "index":
                raise InvalidInput("private-index-handle-required")
            root = self._resource_path(handle.resource_id)
            path = os.path.join(root, b"index")
            acknowledged = record["entries"].get(_encode(b"index"))
            if initialize:
                if record["phase"] != "allocated" or acknowledged is not None or os.path.lexists(path):
                    raise InvalidInput("index-initialization-already-started")
            elif acknowledged is None or not os.path.lexists(path):
                raise InvalidInput("missing-acknowledged-private-index")
            try:
                self._verify_snapshot(root, record["entries"])
            except (OSError, InvalidInput) as exc:
                raise InvalidInput("private-index-provenance-mismatch") from exc
            return path

    def _acknowledge_index_effect(self, handle):
        # Only success of a command that passed the complete preflight can grant
        # authority to its changed index. Failure never adopts current contents.
        root = self._resource_path(handle.resource_id)
        current = self._snapshot(root)
        if set(map(_decode, current)) != {b"owner.json", b"index"}:
            raise InvalidInput("unproven-index-effect-shape")
        self._capture(handle)

    @contextmanager
    def isolated_index(self, base_tree_oid, *, operation_id, owner_id):
        if base_tree_oid is not None:
            self._object(base_tree_oid, b"tree")
            self._tree_routes(base_tree_oid)
        with self._resource("index", operation_id, owner_id) as handle:
            index = self._index(handle, initialize=True)
            command = ("read-tree", "--empty") if base_tree_oid is None else ("read-tree", base_tree_oid)
            self._run(command, index=index)
            self._acknowledge_index_effect(handle)
            yield handle

    @_serialized_index
    def update_index(self, handle, entries=(), *, removals=()):
        entries = tuple(entries)
        selected = self._selection([e.path for e in entries] + list(removals))
        width = self._config()
        payload = bytearray()
        for entry in entries:
            if not isinstance(entry, IndexEntry) or entry.mode not in _REGULAR_MODES or entry.stage != 0:
                raise InvalidInput("regular-stage-zero-entry-required")
            self._object(entry.oid, b"blob")
            payload.extend(entry.mode + b" " + _oid(entry.oid, width).encode() + b"\t" + entry.path + b"\0")
        for path in selected[len(entries):]:
            payload.extend(b"0 " + b"0" * width + b"\t" + path + b"\0")
        index = self._index(handle)
        self._run(("update-index", "-z", "--index-info"), index=index, input=bytes(payload))
        self._acknowledge_index_effect(handle)

    def _planned_trees(self, entries):
        root = {}
        nodes = [root]
        for entry in entries:
            parent = root
            components = entry.path.split(b"/")
            for name in components[:-1]:
                if name not in parent:
                    parent[name] = {}
                    nodes.append(parent[name])
                if not isinstance(parent[name], dict):
                    raise InvalidInput("file-parent-collision")
                parent = parent[name]
            if components[-1] in parent:
                raise InvalidInput("duplicate-or-file-parent-collision")
            parent[components[-1]] = entry
        hashes, result = {}, []
        width = self._config()
        for node in reversed(nodes):
            payload = bytearray()
            for name, child in sorted(node.items(), key=lambda item: item[0] + (b"/" if isinstance(item[1], dict) else b"\0")):
                if isinstance(child, dict):
                    mode, oid = b"40000", hashes[id(child)]
                else:
                    mode, oid = child.mode, child.oid
                payload.extend(mode + b" " + name + b"\0" + bytes.fromhex(oid))
            digest = hashlib.sha256() if width == 64 else hashlib.sha1()
            digest.update(b"tree " + str(len(payload)).encode() + b"\0")
            digest.update(payload)
            oid = digest.hexdigest()
            hashes[id(node)] = oid
            result.append((oid, len(payload)))
        return hashes[id(root)], tuple(result)

    @_serialized_index
    def write_tree(self, handle, *, allowed_paths):
        allowed = set(self._selection(allowed_paths))
        entries = self.index_entries(handle)
        seen = set()
        for entry in entries:
            if entry.stage != 0 or entry.mode not in _REGULAR_MODES or entry.path in seen:
                raise InvalidInput("unsupported-complete-index-entry")
            reason = self.policy.reason(entry.path)
            if reason or entry.path not in allowed:
                raise InvalidInput(reason or "index-path-outside-allowed-set")
            seen.add(entry.path)
        for oid in {entry.oid for entry in entries}:
            self._object(oid, b"blob")
        expected_root, destinations = self._planned_trees(entries)
        for oid, _ in destinations:
            self._preflight_object_write(oid, b"tree")
        output = self._run(("write-tree",), index=self._index(handle))
        self._acknowledge_index_effect(handle)
        actual = _oid(output.decode("ascii").rstrip("\n"), self._config())
        if actual != expected_root:
            raise GitFailure("tree-write-object-id-mismatch")
        for oid, expected_size in destinations:
            if self._verify_stored_object(oid, b"tree").size != expected_size:
                raise GitFailure("tree-write-size-mismatch")
        return actual

    def write_blob_stream(self, producer):
        with self._resource("spool", str(uuid.uuid4()), str(uuid.uuid4())) as handle:
            path = os.path.join(self._resource_path(handle.resource_id), b"spool")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            size = 0
            with self._capture_after(handle):
                with os.fdopen(fd, "wb") as stream:
                    if hasattr(producer, "read"):
                        def chunks():
                            while data := producer.read(CHUNK):
                                yield data
                        source = chunks()
                    elif isinstance(producer, bytes):
                        source = (memoryview(producer)[i:i + CHUNK] for i in range(0, len(producer), CHUNK))
                    else:
                        source = iter(producer)
                    for data in source:
                        if not isinstance(data, (bytes, bytearray, memoryview)):
                            raise InvalidInput("binary-producer-required")
                        _write_all(stream, data)
                        size += len(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            # Git's no-filter blob identity permits validating the exact future leaf
            # before hash-object can write (or reuse) it.
            digest = hashlib.sha256() if self._config() == 64 else hashlib.sha1()
            digest.update(b"blob " + str(size).encode() + b"\0")
            with open(path, "rb") as stream:
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
            self._preflight_object_write(digest.hexdigest(), b"blob")
            with open(path, "rb") as stream:
                output = self._run(("hash-object", "-w", "--stdin", "--no-filters"), stdin=stream)
            oid = _oid(output.decode("ascii").rstrip("\n"), self._config())
            if oid != digest.hexdigest():
                raise GitFailure("blob-write-object-id-mismatch")
            if self._verify_stored_object(oid, b"blob").size != size:
                raise GitFailure("blob-write-size-mismatch")
            return BlobResult(oid, size)

    def _admin_binding(self, handle):
        root = self._resource_path(handle.resource_id)
        tree = os.path.join(root, b"tree")
        _components(tree)
        _identity(os.path.join(tree, b".git"))
        with open(os.path.join(tree, b".git"), "rb") as stream:
            data = stream.read()
        if not data.startswith(b"gitdir: ") or not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise InvalidInput("unproven-worktree-backlink")
        admin = data[8:-1]
        parent = os.path.join(self.metadata, b"worktrees")
        if os.path.dirname(admin) != parent:
            raise InvalidInput("foreign-worktree-admin")
        _components(admin)
        name = os.path.basename(admin).decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise InvalidInput("invalid-worktree-admin-name")
        expected = {b"gitdir": os.path.join(tree, b".git") + b"\n", b"commondir": b"../..\n", b"locked": self._lock_reason(handle).encode() + b"\n"}
        for filename, value in expected.items():
            _identity(os.path.join(admin, filename))
            with open(os.path.join(admin, filename), "rb") as stream:
                if stream.read() != value:
                    raise InvalidInput("unproven-worktree-backlink")
        with open(os.path.join(admin, b"HEAD"), "rb") as stream:
            _oid(stream.read().decode("ascii").rstrip("\n"), self._config())
        allowed = {b"HEAD", b"index", b"gitdir", b"commondir", b"locked", b"logs", b"logs/HEAD", b"ORIG_HEAD"}
        entries = self._snapshot(admin)
        if any(_decode(name) not in allowed for name in entries):
            raise InvalidInput("unknown-worktree-admin-entry")
        self._sync_snapshot(admin, entries)
        self._fsync(parent)
        return {"name": name, "identity": _identity(admin, directory=True), "entries": entries}

    @staticmethod
    def _lock_reason(handle):
        return "ha-ops-native:" + handle.resource_id + ":" + handle.owner_id + ":" + handle.operation_id

    @staticmethod
    def _materialization_stat(info):
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _materialization_directory(info):
        if not stat.S_ISDIR(info.st_mode):
            raise InvalidInput("replaced-materialization-parent")
        return (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))

    def _materialization_root(self, root, rootfd, parents):
        expected = parents[b""]
        if (self._materialization_directory(os.fstat(rootfd)) != expected or
                self._materialization_directory(os.stat(root, follow_symlinks=False)) != expected):
            raise InvalidInput("replaced-materialization-root")

    @contextmanager
    def _materialization_closing(self, close):
        primary = None
        try:
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                close()
            except BaseException as exc:
                if primary is None:
                    raise
                primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("materialization-close:" + str(exc),)

    @contextmanager
    def _materialization_fd(self, fd):
        owner = [fd]
        def close():
            current, owner[0] = owner[0], None
            if current is not None:
                # A failed close has uncertain FD state: never retry its number.
                os.close(current)
        with self._materialization_closing(close):
            yield owner

    @contextmanager
    def _materialization_parent(self, root, rootfd, parents, path):
        self._materialization_root(root, rootfd, parents)
        with self._materialization_fd(os.dup(rootfd)) as owner:
            prefix = b""
            for component in path.split(b"/")[:-1]:
                prefix = component if not prefix else prefix + b"/" + component
                with self._materialization_fd(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                      dir_fd=owner[0])) as next_owner:
                    if self._materialization_directory(os.fstat(next_owner[0])) != parents[prefix]:
                        raise InvalidInput("replaced-materialization-parent")
                    previous = owner[0]
                    owner[0], next_owner[0] = next_owner[0], None
                    os.close(previous)
            yield owner[0], path.split(b"/")[-1]

    @contextmanager
    def _materialization_record_parent(self, root, path):
        _path(path)
        identity = self._materialization_directory(os.stat(root, follow_symlinks=False))
        with self._materialization_fd(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)) as owner:
            if self._materialization_directory(os.fstat(owner[0])) != identity:
                raise InvalidInput("replaced-materialization-record-root")
            for component in path.split(b"/")[:-1]:
                expected = self._materialization_directory(os.stat(component, dir_fd=owner[0], follow_symlinks=False))
                with self._materialization_fd(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                      dir_fd=owner[0])) as next_owner:
                    if self._materialization_directory(os.fstat(next_owner[0])) != expected:
                        raise InvalidInput("replaced-materialization-record-parent")
                    previous = owner[0]
                    owner[0], next_owner[0] = next_owner[0], None
                    os.close(previous)
            yield owner[0], path.split(b"/")[-1]

    def _materialization_record_file(self, root, path, *, directory=False, expected_data=None):
        # Worktree-only readback: common index/spool record helpers retain their
        # existing semantics. Every actual hashing/traversal FD uses one owner.
        with self._materialization_record_parent(root, path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if directory:
                identity = self._materialization_directory(info)
                return list(identity) + ["directory"]
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InvalidInput("unsafe-filesystem-route")
            observed = self._materialization_stat(info)
            with self._materialization_fd(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                                  dir_fd=parent)) as owner:
                if self._materialization_stat(os.fstat(owner[0])) != observed:
                    raise InvalidInput("replaced-owned-file")
                digest, offset = hashlib.sha256(), 0
                while chunk := os.read(owner[0], CHUNK):
                    digest.update(chunk)
                    if expected_data is not None and chunk != expected_data[offset:offset + len(chunk)]:
                        raise InvalidInput("foreign-resource-marker")
                    offset += len(chunk)
                if expected_data is not None and offset != len(expected_data):
                    raise InvalidInput("foreign-resource-marker")
                if self._materialization_stat(os.fstat(owner[0])) != observed:
                    raise InvalidInput("changed-materialization-record-file")
                return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), digest.hexdigest()]

    def _materialization_check_owner(self, rid, record):
        root = self._resource_path(rid)
        if _identity(root, directory=True) != record["root"]:
            raise InvalidInput("replaced-resource-root")
        expected = {"resource": rid, "operation": record["operation"], "owner": record["owner"], "binding": self._binding()}
        encoded = json.dumps(expected, sort_keys=True).encode()
        item = record["entries"].get(_encode(b"owner.json"))
        if not item or self._materialization_record_file(root, b"owner.json", expected_data=encoded) != item:
            raise InvalidInput("unproven-resource-marker")
        if _identity(root, directory=True) != record["root"]:
            raise InvalidInput("replaced-resource-root")

    @contextmanager
    def _materialization_journal(self):
        # The shared schema and generic callers stay unchanged. This boundary
        # encloses worktree evidence checks, including manifest loading/release.
        with self._mutex:
            if not os.path.lexists(self.resources_root):
                yield None
                return
            _components(self.resources_root)
            identity = _identity(self.resources_root, directory=True)
            if identity[2] != 0o700 or (self._storage_id is not None and identity != self._storage_id):
                raise InvalidInput("replaced-or-nonprivate-resource-storage")
            self._storage_id = identity
            lockpath = os.path.join(self.resources_root, b"journal.lock")
            if not os.path.lexists(lockpath):
                if os.path.lexists(os.path.join(self.resources_root, b"journal.json")):
                    raise InvalidInput("missing-stable-journal-lock")
                yield None
                return
            with self._materialization_fd(os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)) as owner:
                # Unlock and close have independent owners: unlock failure must
                # still reach close, with the first exception retained.
                with self._materialization_closing(lambda: fcntl.flock(owner[0], fcntl.LOCK_UN)):
                    identity = _identity(lockpath)
                    if identity[2] != 0o600:
                        raise InvalidInput("private-journal-lock-required")
                    fcntl.flock(owner[0], fcntl.LOCK_EX)
                    info = os.fstat(owner[0])
                    if [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)] != identity or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise InvalidInput("replaced-journal-lock")
                    manifest = os.path.join(self.resources_root, b"journal.json")
                    if os.path.lexists(manifest):
                        observed = self._materialization_stat(os.stat(manifest, follow_symlinks=False))
                        if observed[2:5] != (stat.S_IFREG, 0o600, 1):
                            raise InvalidInput("private-journal-required")
                        with self._materialization_fd(os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as reader:
                            if self._materialization_stat(os.fstat(reader[0])) != observed:
                                raise InvalidInput("replaced-resource-journal")
                            chunks = []
                            while chunk := os.read(reader[0], CHUNK):
                                chunks.append(chunk)
                            journal = _json(b"".join(chunks))
                            if self._materialization_stat(os.fstat(reader[0])) != observed:
                                raise InvalidInput("changed-resource-journal")
                            self._validate_journal(journal)
                    else:
                        journal = {"schema": 1, "root": self._root_id, "metadata": self._meta_id, "storage": self._storage_id, "records": {}}
                    yield journal

    def _materialization_publish(self, journal):
        if _identity(self.resources_root, directory=True) != self._storage_id:
            raise InvalidInput("replaced-or-nonprivate-resource-storage")
        data = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
        name = os.path.join(self.resources_root, (".publish-" + str(uuid.uuid4())).encode())
        identity = None
        def remove():
            if os.path.lexists(name):
                if identity is None or _identity(name) != identity:
                    raise InvalidInput("unproven-materialization-publication-file")
                os.unlink(name)
        with self._materialization_closing(remove):
            with self._materialization_fd(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)) as owner:
                info = os.fstat(owner[0])
                identity = [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or identity[2] != 0o600:
                    raise InvalidInput("unsafe-materialization-publication-file")
                stream = os.fdopen(owner[0], "wb")
                owner[0] = None
                with self._materialization_closing(stream.close):
                    _write_all(stream, data)
                    stream.flush()
                    os.fsync(stream.fileno())
            if _identity(name) != identity:
                raise InvalidInput("replaced-materialization-publication-file")
            if _identity(self.resources_root, directory=True) != self._storage_id:
                raise InvalidInput("replaced-or-nonprivate-resource-storage")
            os.replace(name, os.path.join(self.resources_root, b"journal.json"))
            with self._materialization_fd(os.open(self.resources_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)) as parent:
                if list(self._materialization_directory(os.fstat(parent[0]))) != self._storage_id or _identity(self.resources_root, directory=True) != self._storage_id:
                    raise InvalidInput("replaced-or-nonprivate-resource-storage")
                os.fsync(parent[0])

    def _capture_materialization(self, handle, rootfd, evidence):
        # Only reservations and descriptor-observed writes may extend ownership.
        # Refused entries retain their prior evidence, never a foreign snapshot.
        resource = self._resource_path(handle.resource_id)
        root = os.path.join(resource, b"tree")
        parents, files = evidence["parents"], evidence["files"]
        verified, errors = {}, []
        try:
            self._materialization_root(root, rootfd, parents)
        except BaseException as exc:
            errors.append(exc)
        expected = set(evidence["baseline"])
        for path, identity in parents.items():
            if path:
                encoded = _encode(b"tree/" + path)
                expected.add(encoded)
                # Identity was obtained from an exclusively created directory.
                # Persist that original evidence even if its namespace moved.
                verified[encoded] = list(identity) + ["directory"]
                try:
                    with self._materialization_parent(root, rootfd, parents, path + b"/_") as (fd, _):
                        os.fsync(fd)
                except BaseException as exc:
                    errors.append(exc)
        for path, item in files.items():
            encoded = _encode(b"tree/" + path)
            expected.add(encoded)
            # Cached evidence comes only from the exclusive reservation or our
            # held writer; the current pathname cannot replace this identity.
            verified[encoded] = item["entry"]
            try:
                with self._materialization_parent(root, rootfd, parents, path) as (fd, name):
                    if self._materialization_stat(os.stat(name, dir_fd=fd, follow_symlinks=False)) != item["stat"]:
                        raise InvalidInput("changed-materialization-file")
            except BaseException as exc:
                errors.append(exc)
        try:
            names = set()
            for parent, dirs, leaves in os.walk(resource, followlinks=False):
                names.update(_encode(os.path.relpath(os.path.join(parent, name), resource)) for name in dirs + leaves)
            if names != expected:
                raise InvalidInput("unknown-or-missing-materialization-entry")
        except BaseException as exc:
            errors.append(exc)
        # Marker/backlink bytes are small and fixed. Completed blobs use cached
        # digests, never a whole-tree rehash. Do not recapture baseline evidence.
        for encoded, item in evidence["baseline"].items():
            try:
                if self._materialization_record_file(resource, _decode(encoded), directory=item[3] == "directory") != item:
                    raise InvalidInput("changed-materialization-baseline")
            except BaseException as exc:
                errors.append(exc)
        # A collected verification failure is already the primary exception.
        # Continue publishing only the original proven ownership/debt, retaining
        # every later durability/publication/release failure as a diagnostic.
        try:
            os.fsync(rootfd)
        except BaseException as exc:
            errors.append(exc)
        try:
            with self._materialization_journal() as journal:
                record = self._handle_record(handle, journal, check_owner=self._materialization_check_owner)
                record["entries"].update(verified)
                record["phase"] = "debt" if errors else "ready"
                if errors:
                    record["errors"] = ["materialization-refused:" + str(exc) for exc in errors]
                self._materialization_publish(journal)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            primary = errors[0]
            secondary = []
            for exc in errors[1:]:
                secondary.append("ownership-publication:" + str(exc))
                secondary.extend(getattr(exc, "cleanup_errors", ()))
            primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + tuple(secondary)
            raise primary

    def _materialize(self, handle, entries):
        resource = self._resource_path(handle.resource_id)
        root = os.path.join(resource, b"tree")
        with self._materialization_journal() as journal:
            baseline = dict(self._handle_record(handle, journal, check_owner=self._materialization_check_owner)["entries"])
        tree_identity = baseline[_encode(b"tree")]
        evidence = {"baseline": baseline, "parents": {b"": tuple(tree_identity[:3])}, "files": {}}
        parents, files = evidence["parents"], evidence["files"]
        parent_identities = {tuple(tree_identity[:2]): b""}
        with self._materialization_fd(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)) as root_owner:
            rootfd = root_owner[0]
            primary = None
            try:
                self._materialization_root(root, rootfd, parents)
                # Reserve the complete exact byte set before copying any blob, closing
                # each exclusive leaf immediately so selection size does not retain FDs.
                for entry in entries:
                    self._materialization_root(root, rootfd, parents)
                    with self._materialization_fd(os.dup(rootfd)) as parent_owner:
                        components, prefix = entry.path.split(b"/"), b""
                        for component in components[:-1]:
                            prefix = component if not prefix else prefix + b"/" + component
                            try:
                                os.mkdir(component, 0o700, dir_fd=parent_owner[0])
                            except FileExistsError:
                                if prefix not in parents:
                                    raise InvalidInput("unrepresentable-parent-component-set")
                            with self._materialization_fd(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                                  dir_fd=parent_owner[0])) as next_owner:
                                identity = self._materialization_directory(os.fstat(next_owner[0]))
                                if prefix in parents and parents[prefix] != identity:
                                    raise InvalidInput("replaced-materialization-parent")
                                if identity[:2] in parent_identities and parent_identities[identity[:2]] != prefix:
                                    raise InvalidInput("aliased-parent-component")
                                parents[prefix] = identity
                                parent_identities[identity[:2]] = prefix
                                previous = parent_owner[0]
                                parent_owner[0], next_owner[0] = next_owner[0], None
                                os.close(previous)
                        with self._materialization_fd(os.open(components[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                              0o600, dir_fd=parent_owner[0])) as leaf_owner:
                            info = os.fstat(leaf_owner[0])
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
                                raise InvalidInput("unsafe-materialization-reservation")
                            files[entry.path] = {"stat": self._materialization_stat(info),
                                                "entry": [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), hashlib.sha256(b"").hexdigest()]}
                            os.fsync(leaf_owner[0])
                for path in sorted(parents, key=lambda p: -p.count(b"/")):
                    if path:
                        with self._materialization_parent(root, rootfd, parents, path + b"/_") as (fd, _):
                            os.fsync(fd)
                os.fsync(rootfd)
                self._capture_materialization(handle, rootfd, evidence)
                for entry in entries:
                    item = files[entry.path]
                    with self._materialization_parent(root, rootfd, parents, entry.path) as (parentfd, name):
                        if self._materialization_stat(os.stat(name, dir_fd=parentfd, follow_symlinks=False)) != item["stat"]:
                            raise InvalidInput("changed-materialization-file")
                        with self._materialization_fd(os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                                              dir_fd=parentfd)) as leaf_owner:
                            if self._materialization_stat(os.fstat(leaf_owner[0])) != item["stat"]:
                                raise InvalidInput("changed-materialization-file")
                            stream = os.fdopen(leaf_owner[0], "wb", buffering=0)
                            leaf_owner[0] = None  # the stream is now the only descriptor owner
                            with self._materialization_closing(stream.close):
                                write_primary = None
                                try:
                                    self.copy_blob(entry.oid, stream)
                                    stream.flush()
                                    os.fchmod(stream.fileno(), 0o755 if entry.mode == b"100755" else 0o644)
                                    os.fsync(stream.fileno())
                                except BaseException as exc:
                                    write_primary = exc
                                    raise
                                finally:
                                    try:
                                        # Observe only our still-open inode, including actual
                                        # partial bytes left by a producer interruption.
                                        info = os.fstat(stream.fileno())
                                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                                                (info.st_dev, info.st_ino) != item["stat"][:2]):
                                            raise InvalidInput("changed-materialization-file")
                                        observed = self._materialization_stat(info)
                                        self._materialization_root(root, rootfd, parents)
                                        if self._materialization_stat(os.stat(name, dir_fd=parentfd, follow_symlinks=False)) != observed:
                                            raise InvalidInput("changed-materialization-file")
                                        os.fsync(stream.fileno())
                                        owned = self._materialization_record_file(resource, b"tree/" + entry.path)
                                        if owned[:3] != [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]:
                                            raise InvalidInput("changed-materialization-file")
                                        with self._materialization_parent(root, rootfd, parents, entry.path) as (checkfd, checkname):
                                            if self._materialization_stat(os.stat(checkname, dir_fd=checkfd, follow_symlinks=False)) != observed:
                                                raise InvalidInput("changed-materialization-file")
                                        item["stat"], item["entry"] = observed, owned
                                    except BaseException as exc:
                                        if write_primary is None:
                                            raise
                                        write_primary.cleanup_errors = tuple(getattr(write_primary, "cleanup_errors", ())) + ("ownership-publication:" + str(exc),)
                    self._capture_materialization(handle, rootfd, evidence)
                os.fsync(rootfd)
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    # Includes verified partial reservations/output after failures.
                    # The same checked boundary refuses foreign additions/replacements.
                    if primary is not None:
                        self._capture_materialization(handle, rootfd, evidence)
                except BaseException as exc:
                    if primary is None:
                        raise
                    primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("ownership-publication:" + str(exc),)

    @contextmanager
    def temporary_worktree(self, commit_oid, *, operation_id, owner_id, paths=()):
        selected = self._selection(paths)
        self._object(commit_oid, b"commit")
        table = {entry.path: entry for entry in self.tree_entries(commit_oid)}
        entries = []
        for path in selected:
            if path not in table or table[path].mode not in _REGULAR_MODES:
                raise InvalidInput("regular-tree-entry-required")
            entries.append(table[path])
        with self._resource("worktree", operation_id, owner_id) as handle:
            tree = os.path.join(self._resource_path(handle.resource_id), b"tree")
            self._capture(handle, phase="creating")
            primary = None
            try:
                self._run(("worktree", "add", "--detach", "--no-checkout", "--lock", "--reason", self._lock_reason(handle), os.fsdecode(tree), commit_oid))
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    with self._journal() as journal:
                        record = self._handle_record(handle, journal)
                        if os.path.lexists(os.path.join(tree, b".git")):
                            record["admin"] = self._admin_binding(handle)
                            record["entries"] = self._snapshot(self._resource_path(handle.resource_id))
                            self._publish(journal)
                except BaseException as exc:
                    if primary is None:
                        raise
                    primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("worktree-ownership:" + str(exc),)
            self._capture(handle)
            if entries:
                self._materialize(handle, entries)
            yield handle

    def worktree_path(self, handle):
        with self._journal() as journal:
            if self._handle_record(handle, journal)["kind"] != "worktree":
                raise InvalidInput("worktree-handle-required")
        return os.path.join(self._resource_path(handle.resource_id), b"tree")

    def _mount_guard(self, root):
        if os.path.exists("/proc/self/mountinfo"):
            if any(beneath(m.point, root) for m in linux_mounts()):
                raise InvalidInput("mounted-resource-descendant")
        elif os.path.ismount(root):
            raise InvalidInput("mounted-resource-descendant")

    def _verify_snapshot(self, root, entries):
        self._mount_guard(root)
        actual = self._snapshot(root)
        if actual != entries:
            raise InvalidInput("unknown-or-replaced-resource-entry")

    def _cleanup(self, rid, owner_id, operation_id, *, held_lease=None):
        lease, ownlease, errors = held_lease, False, []
        with self._journal() as journal:
            if journal is None or rid not in journal["records"]:
                return CleanupResult(rid, "absent")
            record = journal["records"][rid]
            if record["owner"] != owner_id or record["operation"] != operation_id:
                raise InvalidInput("resource-binding-mismatch")
            root, leasepath = self._resource_path(rid), self._lease_path(rid)
            try:
                if (held_lease is not None and record["kind"] == "worktree" and
                        any(error.startswith("materialization-refused:") for error in record["errors"])):
                    raise InvalidInput("refused-materialization-requires-addressed-recovery")
                if lease is None and os.path.lexists(leasepath):
                    if _identity(leasepath) != record["lease"]:
                        raise InvalidInput("replaced-resource-lease")
                    lease = os.open(leasepath, os.O_RDWR | os.O_NOFOLLOW)
                    ownlease = True
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return CleanupResult(rid, "busy")
                    if os.fstat(lease).st_ino != record["lease"][1]:
                        raise InvalidInput("replaced-resource-lease")
                elif lease is None and os.path.lexists(root):
                    raise InvalidInput("missing-resource-lease")
                if os.path.lexists(root):
                    self._check_owner(rid, record)
                    self._mount_guard(root)
                    # Recover completed Git registration only from exact token/backlink proof.
                    if record["kind"] == "worktree" and record["admin"] is None and os.path.lexists(os.path.join(root, b"tree/.git")):
                        handle = ResourceHandle(rid, operation_id, owner_id, "worktree", self._binding())
                        record["admin"] = self._admin_binding(handle)
                        current = self._snapshot(root)
                        if set(map(_decode, current)) != {b"owner.json", b"tree", b"tree/.git"}:
                            raise InvalidInput("unproven-partial-worktree-content")
                        record["entries"] = current
                        self._publish(journal)
                    if record["admin"] is not None:
                        try:
                            admin = os.path.join(self.metadata, b"worktrees", record["admin"]["name"].encode())
                            tree = os.path.join(root, b"tree")
                            if not os.path.lexists(tree) and not os.path.lexists(admin):
                                # A killed remover may have completed its effect before the
                                # durable acknowledgement. Both exact absences prove completion.
                                record["admin"] = None
                                record["entries"] = {_encode(b"owner.json"): record["entries"][_encode(b"owner.json")]}
                                self._publish(journal)
                            else:
                                self._verify_snapshot(root, record["entries"])
                            if record["admin"] is None:
                                pass
                            elif _identity(admin, directory=True) != record["admin"]["identity"]:
                                raise InvalidInput("replaced-worktree-admin")
                            if record["admin"] is not None:
                                self._verify_snapshot(admin, record["admin"]["entries"])
                                tree = os.path.join(root, b"tree")
                                self._run(("worktree", "unlock", os.fsdecode(tree)))
                                # Update exact admin evidence after our own unlock, even if remove fails.
                                record["admin"]["entries"] = self._snapshot(admin)
                                self._publish(journal)
                                self._run(("worktree", "remove", "--force", os.fsdecode(tree)))
                                if os.path.lexists(tree) or os.path.lexists(admin):
                                    raise InvalidInput("worktree-removal-not-proven")
                                record["admin"] = None
                                record["entries"] = {_encode(b"owner.json"): record["entries"][_encode(b"owner.json")]}
                                self._publish(journal)
                        except BaseException as exc:
                            errors.append(str(exc))
                    # Each independently known file is attempted; unknown files are preserved.
                    for encoded, item in list(record["entries"].items()):
                        path = _decode(encoded)
                        if path == b"owner.json" or beneath(path, b"tree") or item[3] == "directory":
                            continue
                        candidate = os.path.join(root, path)
                        try:
                            if os.path.lexists(candidate):
                                self._unlink_file(root, path, item)
                            record["entries"].pop(encoded)
                        except BaseException as exc:
                            errors.append(path.decode("ascii", "backslashreplace") + ":" + str(exc))
                    if not errors:
                        self._verify_snapshot(root, record["entries"])
                        if set(map(_decode, record["entries"])) != {b"owner.json"}:
                            raise InvalidInput("residual-owned-resource")
                        self._unlink_file(root, b"owner.json", record["entries"][_encode(b"owner.json")])
                        os.rmdir(root)
                        self._fsync(self.resources_root)
                elif record["admin"] is not None:
                    admin = os.path.join(self.metadata, b"worktrees", record["admin"]["name"].encode())
                    if os.path.lexists(admin):
                        raise InvalidInput("missing-tree-retained-admin-debt")
                if not errors and not os.path.lexists(root):
                    if os.path.lexists(leasepath):
                        if record["lease"] != _identity(leasepath):
                            raise InvalidInput("replaced-resource-lease")
                        os.unlink(leasepath)
                    self._fsync(self.resources_root)
                    del journal["records"][rid]
                    self._publish(journal)
                    return CleanupResult(rid, "removed")
            except BaseException as exc:
                errors.append(str(exc))
            finally:
                if ownlease:
                    fcntl.flock(lease, fcntl.LOCK_UN)
                    os.close(lease)
            record["phase"], record["errors"] = "debt", errors
            journal["records"][rid] = record
            try:
                self._publish(journal)
            except BaseException as exc:
                errors.append("journal-publication:" + str(exc))
            return CleanupResult(rid, "debt", tuple(errors))

    def recover_resource(self, resource_id, *, owner_id, operation_id):
        _uuid(resource_id)
        _uuid(owner_id)
        _uuid(operation_id)
        return self._cleanup(resource_id, owner_id, operation_id)

    def resources(self):
        with self._journal() as journal:
            if journal is None:
                return ()
            return tuple({"resource_id": rid, **json.loads(json.dumps(record))} for rid, record in journal["records"].items())

    def accounting(self):
        with self._journal() as journal:
            if journal is None:
                return {"active": 0, "unresolved": 0, "temporary_bytes": 0, "admin_bytes": 0, "errors": ()}
            total, admin_total, errors = 0, 0, []
            for rid, record in journal["records"].items():
                for root in (self._resource_path(rid),):
                    try:
                        if os.path.lexists(root):
                            self._check_owner(rid, record)
                            self._mount_guard(root)
                            for parent, dirs, files in os.walk(root, followlinks=False):
                                for name in dirs:
                                    _identity(os.path.join(parent, name), directory=True)
                                for name in files:
                                    path = os.path.join(parent, name)
                                    try:
                                        _identity(path)
                                        total += os.lstat(path).st_size
                                    except (OSError, InvalidInput) as exc:
                                        errors.append(rid + ":" + str(exc))
                    except (OSError, InvalidInput) as exc:
                        errors.append(rid + ":" + str(exc))
                if record["kind"] == "worktree" and record["admin"] is None and os.path.lexists(os.path.join(self._resource_path(rid), b"tree")):
                    errors.append(rid + ":unacknowledged-worktree-admin-accounting")
                if record["admin"]:
                    admin = os.path.join(self.metadata, b"worktrees", record["admin"]["name"].encode())
                    try:
                        for encoded, item in record["admin"]["entries"].items():
                            if item[3] != "directory":
                                path = os.path.join(admin, _decode(encoded))
                                _identity(path)
                                admin_total += os.lstat(path).st_size
                    except (OSError, InvalidInput) as exc:
                        errors.append(rid + ":" + str(exc))
            active = 0
            for rid, record in journal["records"].items():
                if rid in self._active:
                    active += 1
                    continue
                leasepath = self._lease_path(rid)
                if not os.path.lexists(leasepath):
                    continue
                fd = None
                try:
                    if _identity(leasepath) != record["lease"]:
                        raise InvalidInput("replaced-resource-lease")
                    fd = os.open(leasepath, os.O_RDWR | os.O_NOFOLLOW)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        active += 1
                except (OSError, InvalidInput) as exc:
                    errors.append(rid + ":" + str(exc))
                finally:
                    if fd is not None:
                        os.close(fd)
            return {"active": active, "unresolved": len(journal["records"]) - active, "temporary_bytes": total,
                    "admin_bytes": admin_total, "errors": tuple(errors)}
