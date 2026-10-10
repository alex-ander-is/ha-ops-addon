"""Dormant byte-native status/Preview; no production import or effect capability.

NativePreview(git, observer, binding, scratch_root=...) performs no IO on
construction. observer returns SourceObservation with refreshed/reachable exact
local remote ref evidence and optional explicit last-synchronized base. Network
freshness is trusted orchestration evidence, NOT proved by local ref equality.
Two complete scoped live observations detect cooperative changes; they are not
an atomic hostile-writer snapshot and do not authorize a future executor.

build('save'|'apply') always starts a fresh empty draft. view_file streams inert
escaped UTF-8 text to a caller-owned binary sink. select uses canonical bytes
(or decode_path transport tokens), never labels. review is diagnostic only;
ready means the selected review has no known blocker, NOT execution authority.
reset('refresh'|'restart'|'update') clears every binding. Sessions serialize API
calls with a nonblocking lock: concurrent/reentrant calls raise Busy without
changing state. No persistence, NativeOperationStore replay or worker exists.

Live bytes are hashed without retention. Per-file Git tools spool fixed-name
sides and output in an owned private directory outside live/Git roots. Memory
is bounded for file transfers; complete serialized metadata manifests and path
records are held in memory. Git internal memory is input-dependent. Disk footprint is before/after/base plus patch or merged
output and stderr; no guessed output ceiling or whole-storage scan is used.
Timeout/errors may leave partial caller output, which is unusable unless a
FileView was returned. Child termination/reaping has a bounded cleanup budget.
A surviving exact child and its scratch are retained as pending_children on the
original exception; callers own addressed cleanup, never implicit replay.
Failed stream close retains its exact owner in pending_streams plus the acquired
fd address. Close debt describes retained or uncertain ownership, never raw-fd
retry authority; no close is blindly retried or delegated to garbage collection.
Cleanup debt is attached to the original exception;
debt-bearing failures cross diagnostic boundaries as exceptions, never Issues.
foreign substitutions are retained. All namespace checks are cooperative.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import codecs
import hashlib
import html
import io
import math
import os
import re
import stat
import subprocess
import threading
import unicodedata
import uuid

from native_git import NativeGit
from native_inventory import (Blocked, Issue, beneath, inventory, normalize_scope,
                              path_bytes, _open_absolute_directory, linux_mounts, _has_cleanup_debt)
import native_metadata as metadata

CHUNK = 65536


class PreviewFailure(RuntimeError):
    """No successful fresh result; partial sink output has no validity."""


class Stale(PreviewFailure):
    pass


class Unavailable(PreviewFailure):
    pass


class Busy(PreviewFailure):
    pass


def _path(value):
    if type(value) is not bytes:
        raise Blocked('byte-path-required')
    return path_bytes(value)


def encode_path(path):
    return base64.b64encode(_path(path)).decode('ascii')


def decode_path(token):
    try:
        if type(token) is not str:
            raise ValueError()
        result = base64.b64decode(token, validate=True)
        if encode_path(result) != token:
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Blocked('canonical-path-token-required') from exc


@dataclass(frozen=True)
class SourceObservation:
    remote_ref: str
    remote_oid: str
    scope: tuple[bytes, ...]
    scope_revision: str
    exclude_digest: str
    reachable: bool = True
    refreshed: bool = True
    base_ref: str | None = None
    base_oid: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'scope', tuple(self.scope))


@dataclass(frozen=True)
class FileState:
    oid: str
    record: metadata.Record | None
    size: int | None = None
    binary: bool | None = None


@dataclass(frozen=True)
class PathChange:
    """None is no available regular state; viewable proves an absent side.

    Unsafe/unknown current bytes remain diagnostic content='unavailable', never
    an invented empty diff side. Metadata diagnostics do not grant byte safety.
    """
    path: bytes
    before: FileState | None
    after: FileState | None
    base: FileState | None
    content: str
    metadata_changed: bool
    conflict: str
    issues: tuple[str, ...] = ()
    parents: tuple[bytes, ...] = ()
    viewable: bool = False

    @property
    def selectable(self):
        return self.viewable and not any(not i.startswith('metadata-conflict:') for i in self.issues)

    @property
    def token(self):
        return encode_path(self.path)

    @property
    def display_path(self):
        return escaped(self.path)


@dataclass(frozen=True)
class ParentDetail:
    path: bytes
    before: metadata.Record | None
    after: metadata.Record | None
    dependents: tuple[bytes, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewSnapshot:
    snapshot_id: str
    source_digest: str
    direction: str
    source: SourceObservation
    rows: tuple[PathChange, ...]
    parents: tuple[ParentDetail, ...]
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class Draft:
    snapshot_id: str
    source_digest: str
    direction: str
    generation: int
    selected: tuple[bytes, ...]
    approved_parents: tuple[bytes, ...]


@dataclass(frozen=True)
class Review:
    draft: Draft
    selected: tuple[PathChange, ...]
    parents: tuple[ParentDetail, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self):
        return not self.blockers


@dataclass(frozen=True)
class FileView:
    snapshot_id: str
    source_digest: str
    path: bytes
    conflict: str
    binary: bool
    scratch_bytes: int


@dataclass(frozen=True)
class _Observed:
    source: SourceObservation
    remote: tuple
    base: tuple
    live: tuple
    remote_manifest: metadata.Manifest | None
    base_manifest: metadata.Manifest | None
    live_manifest: metadata.Manifest
    viewable_paths: tuple[bytes, ...]
    routes: tuple
    issues: tuple[Issue, ...]
    digest: str


def _semantic(info):
    # Directory mtime/size/link count reflects unrelated sibling churn.
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def _attributes(record):
    return None if record is None else (record.kind, record.uid, record.gid, record.mode)


def _display_record(record):
    if record is None:
        return 'absent-or-unavailable'
    return 'kind=%s uid=%d gid=%d mode=0o%o' % (record.kind, record.uid, record.gid, record.mode)


def _display_state(state):
    return 'absent' if state is None else ('metadata-unavailable' if state.record is None else _display_record(state.record))


def _same(a, b):
    return (a is None and b is None or a is not None and b is not None and a.oid == b.oid)


def _write(sink, data):
    view = memoryview(data)
    while view:
        count = sink.write(view[:CHUNK])
        if type(count) is not int or count <= 0 or count > min(len(view), CHUNK):
            raise OSError('invalid-or-zero-sink-write')
        view = view[count:]


def escaped(data):
    """Inert text; patch LF survives, all controls/invalid bytes are visible."""
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'surrogateescape')
    text = []
    for c in data:
        n = ord(c)
        if 0xdc80 <= n <= 0xdcff:
            text.append('\\x%02x' % (n - 0xdc00))
        elif c != '\n' and (unicodedata.category(c) in ('Cc', 'Cf', 'Cs') or n in (0x2028, 0x2029)):
            text.append('\\x%02x' % n if n <= 255 else '\\u%04x' % n)
        else:
            text.append(c)
    return html.escape(''.join(text), quote=True)


@contextmanager
def _route(binding, path, kind, *, absent=False):
    """Guarded existing ancestors establish absence, never inventory omission."""
    fds, links, routes = [], [], {}
    primary = None
    missing = None
    try:
        root = binding.root
        if type(root) is not bytes or not root.startswith(b'/') or os.path.abspath(root) != root:
            raise Blocked('canonical-absolute-root-required')
        fd = _open_absolute_directory(root)
        try:
            fds.append(fd)
        except BaseException as exc:
            _close_fd(fd, exc)
            raise
        mounts = linux_mounts() if os.path.exists('/proc/self/mountinfo') else None
        binding.check(b'', os.fstat(fd), mounts)
        routes[b''] = _semantic(os.fstat(fd))
        prefix = b''
        for i, part in enumerate(path.split(b'/') if path else ()):
            prefix = prefix + b'/' + part if prefix else part
            try:
                s = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                if _has_cleanup_debt(exc):
                    raise
                if not absent:
                    raise
                missing = (fd, part)
                fd = None
                break
            binding.check(prefix, s, mounts)
            directory = i < len(path.split(b'/')) - 1 or kind == 'directory'
            # A proved regular proper ancestor makes the descendant pathname
            # impossible. This is diagnostic absence, not shape readiness.
            blocked_by_file = absent and i < len(path.split(b'/')) - 1 and stat.S_ISREG(s.st_mode)
            if blocked_by_file:
                directory = False
            if not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)):
                raise Blocked('unsafe-object-type-or-shape')
            if not directory and s.st_nlink != 1:
                raise Blocked('multiply-linked-file')
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                            (os.O_DIRECTORY if directory else 0), dir_fd=fd)
            try:
                fds.append(child)
            except BaseException as exc:
                _close_fd(child, exc)
                raise
            if (_semantic(s) != _semantic(os.fstat(child)) or
                    (not directory and metadata._stat(s) != metadata._stat(os.fstat(child)))):
                raise Stale('changed-object-acquisition')
            links.append((fd, part, child, directory))
            routes[prefix] = _semantic(s)
            fd = child
            if blocked_by_file:
                fd = None
                break
        yield fd, tuple(sorted(routes.items()))
        if missing is not None:
            try:
                os.stat(missing[1], dir_fd=missing[0], follow_symlinks=False)
            except FileNotFoundError as exc:
                if _has_cleanup_debt(exc):
                    raise
                pass
            else:
                raise Stale('absence-changed')
        for parent, part, child, directory in links:
            if _semantic(os.stat(part, dir_fd=parent, follow_symlinks=False)) != _semantic(os.fstat(child)):
                raise Stale('changed-object-namespace')
        if _semantic(os.stat(root, follow_symlinks=False)) != routes[b'']:
            raise Stale('changed-root-namespace')
        for p, value in routes.items():
            held = fds[0 if not p else len(p.split(b'/'))]
            if _semantic(os.fstat(held)) != value:
                raise Stale('changed-route-attributes')
        # Re-read mount evidence after acquisition/streaming.
        current = linux_mounts() if mounts is not None else None
        for p, value in routes.items():
            index = 0 if not p else len(p.split(b'/'))
            binding.check(p, os.fstat(fds[index]), current)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failure = primary
        for fd in reversed(fds):
            try:
                os.close(fd)
            except BaseException as exc:
                if failure is None:
                    failure = PreviewFailure('descriptor-cleanup-failed')
                _record_cleanup(failure, 'route-fd-close:fd=%d:root=%r:path=%r' %
                                (fd, root, path), exc)
        if primary is None and failure is not None:
            raise failure


def _live_file(binding, path, width, sink=None):
    with _route(binding, path, 'file', absent=True) as (fd, routes):
        if fd is None:
            return None, routes
        before = os.fstat(fd)
        size = before.st_size
        h = hashlib.sha1() if width == 40 else hashlib.sha256()
        h.update(b'blob ' + str(size).encode() + b'\0')
        count, binary = 0, False
        while count < size:
            if metadata._stat(before) != metadata._stat(os.fstat(fd)):
                raise Stale('changed-live-content')
            data = os.read(fd, min(CHUNK, size - count))
            if not data or len(data) > size - count or metadata._stat(before) != metadata._stat(os.fstat(fd)):
                raise Stale('changed-live-content')
            count += len(data)
            h.update(data)
            binary |= b'\0' in data
            if sink is not None:
                _write(sink, data)
        if os.read(fd, 1) or count != size or metadata._stat(before) != metadata._stat(os.fstat(fd)):
            raise Stale('changed-live-content')
        record = metadata.Record(path, 'file', before.st_uid, before.st_gid,
                                 stat.S_IMODE(before.st_mode), h.hexdigest())
        return FileState(h.hexdigest(), record, size, binary), routes


def _record_cleanup(primary, address, cleanup, *, streams=()):
    """Capture an address before close; preserve primary and all existing debt."""
    details = (address + ':' + type(cleanup).__name__ + ':' + str(cleanup),)
    if cleanup is not primary:
        details += tuple(getattr(cleanup, 'cleanup_errors', ()))
    primary.cleanup_errors = tuple(getattr(primary, 'cleanup_errors', ())) + details
    for attribute, extra in (
            ('pending_children', () if cleanup is primary else getattr(cleanup, 'pending_children', ())),
            ('pending_streams', tuple(streams) + (() if cleanup is primary else tuple(getattr(cleanup, 'pending_streams', ()))))):
        if extra:
            retained = list(getattr(primary, attribute, ()))
            for owner in extra:
                if not any(owner is known for known in retained):
                    retained.append(owner)
            setattr(primary, attribute, tuple(retained))


def _close_fd(fd, primary):
    try:
        os.close(fd)
    except BaseException as exc:
        _record_cleanup(primary, 'raw-fd-close:fd=%d' % fd, exc)


def _child_running(process, errors):
    try:
        return process.poll() is None
    except BaseException as exc:
        errors.append('child-poll:pid=%d:%s' % (process.pid, exc))
        return True  # Unknown status retains exact addressed ownership debt.


@contextmanager
def _opened_stream(name, flags, mode, *, dir_fd, acquire, permissions=0o600):
    """Successful raw open is owned before fstat, callbacks or fdopen can fail."""
    fd = os.open(name, flags, permissions, dir_fd=dir_fd)
    try:
        acquire(fd)
        stream = os.fdopen(fd, mode)
    except BaseException as exc:
        _close_fd(fd, exc)
        raise
    with _closing(stream, fd):
        yield stream


@contextmanager
def _closing(stream, owned_fd):
    primary = None
    try:
        yield stream
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            stream.close()
        except BaseException as exc:
            target = exc if primary is None else primary
            _record_cleanup(target, 'stream-close:fd=%d' % owned_fd, exc, streams=(stream,))
            if primary is None:
                raise


class _Scratch:
    def __init__(self, owner):
        self.owner, self.files = owner, {}
        self.rootfd = self.fd = None
        self.path = self.name = None
        self.identity = None
        self.created = False
        self.total = 0
        self.pending_child = None

    def __enter__(self):
        root = self.owner.scratch_root
        if type(root) is not bytes or not root.startswith(b'/') or os.path.abspath(root) != root:
            raise Blocked('canonical-scratch-root-required')
        for forbidden in (self.owner.binding.root, self.owner.git.root,
                          self.owner.git.metadata, self.owner.git.resources_root):
            if beneath(root, forbidden) or beneath(forbidden, root):
                raise Blocked('scratch-must-be-outside-live-and-git-roots')
        self.rootfd = _open_absolute_directory(root)
        try:
            self.root_identity = _semantic(os.fstat(self.rootfd))
            self.name = ('native-preview-' + str(uuid.uuid4())).encode()
            os.mkdir(self.name, 0o700, dir_fd=self.rootfd)
            self.created = True
            self.path = os.path.join(root, self.name)
            self.identity = _semantic(os.stat(self.name, dir_fd=self.rootfd, follow_symlinks=False))
            self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.rootfd)
            if self.identity != _semantic(os.fstat(self.fd)):
                raise Stale('scratch-acquisition-substituted')
            if self.identity[3] != 0o700:
                raise Blocked('private-scratch-mode-required')
            self.path = os.path.join(root, self.name)
            self.check()
            return self
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def check(self):
        if (self.identity != _semantic(os.stat(self.name, dir_fd=self.rootfd, follow_symlinks=False))
                or self.root_identity != _semantic(os.stat(self.owner.scratch_root, follow_symlinks=False))):
            raise Stale('scratch-route-substituted')

    @contextmanager
    def file(self, name):
        if name not in ('before', 'after', 'base', 'patch', 'merged', 'stderr') or name in self.files:
            raise Blocked('fixed-unique-scratch-name-required')
        def acquire(fd):
            # An unavailable identity stays addressed debt, never guessed ownership.
            self.files[name] = None
            info = os.fstat(fd)
            self.files[name] = (info.st_dev, info.st_ino)
        with _opened_stream(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            'w+b', dir_fd=self.fd, acquire=acquire) as stream:
            yield stream

    @contextmanager
    def read(self, name):
        if name not in self.files or self.files[name] is None:
            raise Blocked('owned-scratch-reader-required')
        def acquire(fd):
            info = os.fstat(fd)
            if ((info.st_dev, info.st_ino) != self.files[name]
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise Stale('foreign-render-artifact')
        with _opened_stream(name, os.O_RDONLY | os.O_NOFOLLOW, 'rb',
                            dir_fd=self.fd, acquire=acquire) as stream:
            yield stream

    def __exit__(self, kind, primary, traceback):
        failure = primary
        def record(address, cleanup):
            nonlocal failure
            if failure is None:
                failure = PreviewFailure('scratch-cleanup-debt')
            _record_cleanup(failure, address, cleanup)
        errors = []
        pending = self.pending_child is not None and _child_running(self.pending_child, errors)
        if pending:
            errors.append('live-owned-child-scratch-retained:pid=%d:path=%r' % (self.pending_child.pid, self.path))
        elif self.fd is not None:
            for name, identity in self.files.items():
                try:
                    if identity is None:
                        raise Stale('unproved-owned-scratch-file-retained:' + repr(os.path.join(self.path, os.fsencode(name))))
                    s = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if (s.st_dev, s.st_ino) != identity or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                        raise Stale('foreign-scratch-file-retained:' + name)
                    self.total += s.st_size
                    os.unlink(name, dir_fd=self.fd)
                except BaseException as exc:
                    record('scratch-file-cleanup:path=%r' % os.path.join(self.path, os.fsencode(name)), exc)
            try:
                self.check()
                os.rmdir(self.name, dir_fd=self.rootfd)
            except BaseException as exc:
                record('scratch-directory-cleanup:path=%r' % self.path, exc)
        elif self.created:
            try:
                if self.identity is None:
                    raise Stale('unproved-owned-scratch-retained:' + repr(self.path))
                self.check()
                os.rmdir(self.name, dir_fd=self.rootfd)
            except BaseException as exc:
                record('scratch-directory-cleanup:path=%r' % self.path, exc)
        for fd in (self.fd, self.rootfd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as exc:
                    record('scratch-fd-close:fd=%d:path=%r' % (fd, self.path if fd == self.fd else self.owner.scratch_root), exc)
        if errors:
            if failure is None:
                failure = PreviewFailure('scratch-cleanup-debt')
            failure.cleanup_errors = tuple(getattr(failure, 'cleanup_errors', ())) + tuple(errors)
        if primary is None and failure is not None:
            raise failure
        return False


class NativePreview:
    def __init__(self, git, source_observer, binding, *, scratch_root, timeout=30):
        if (not isinstance(git, NativeGit) or not isinstance(binding, metadata.Binding)
                or not callable(source_observer) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0):
            raise Blocked('invalid-preview-construction')
        self.git, self.observer, self.binding = git, source_observer, binding
        self.scratch_root, self.timeout = os.fsencode(scratch_root), timeout
        self._lock = threading.Lock()
        self._generation = 0
        self._review_binding = None
        self._snapshot = self._observed = self._draft = self._review = None

    @property
    def draft(self):
        return self._draft

    @contextmanager
    def _call(self):
        if not self._lock.acquire(blocking=False):
            raise Busy('serialized-preview-session-busy')
        try:
            yield
        except BaseException:
            self._snapshot = self._observed = self._draft = self._review = None
            self._generation += 1
            raise
        finally:
            self._lock.release()

    def reset(self, reason='refresh'):
        if reason not in ('refresh', 'restart', 'update'):
            raise Blocked('invalid-reset-reason')
        with self._call():
            self._snapshot = self._observed = self._draft = self._review = None
            self._generation += 1

    def _source(self):
        try:
            source = self.observer()
            if (type(source) is not SourceObservation or source.reachable is not True
                    or source.refreshed is not True or not source.scope_revision
                    or type(source.scope_revision) is not str or type(source.exclude_digest) is not str
                    or not source.exclude_digest):
                raise Unavailable('fresh-source-observation-required')
            scope = tuple(_path(p) for p in source.scope)
            if normalize_scope(scope) != scope:
                raise Blocked('normalized-active-byte-scope-required')
            if self.git.root != self.binding.root or self.git.policy != self.binding.policy:
                raise Blocked('git-live-policy-binding-mismatch')
            width = self.git._config()
            for ref, oid in ((source.remote_ref, source.remote_oid), (source.base_ref, source.base_oid)):
                if ref is None and oid is None:
                    if ref == source.remote_ref:
                        raise Unavailable('remote-source-required')
                    continue
                if (type(ref) is not str or type(oid) is not str or
                        not re.fullmatch('[0-9a-f]{%d}' % width, oid) or oid == '0' * width):
                    raise Unavailable('exact-qualified-source-required')
                if self.git.read_ref(ref).oid != oid:
                    raise Stale('observed-ref-mismatch')
                self.git._object(oid, (b'commit', b'tree'))
            return source, width
        except (PreviewFailure, Blocked):
            raise
        except Exception as exc:
            if _has_cleanup_debt(exc):
                raise
            raise Unavailable('source-observation-failed') from exc

    def _manifest(self, tree, width, label, issues):
        entries = [e for e in tree if e.path == metadata.MANIFEST_PATH]
        try:
            if len(entries) != 1 or entries[0].mode != b'100644':
                raise Blocked('fixed-metadata-manifest-missing-or-mode')
            # Manifest memory is proportional to path metadata, not file content.
            sink = io.BytesIO()
            self.git.copy_blob(entries[0].oid, sink)
            m = metadata.decode(sink.getvalue(), width=width, policy=self.binding.policy)
            return metadata.validate(m, tree, width=width, policy=self.binding.policy)
        except (Blocked, ValueError) as exc:
            if _has_cleanup_debt(exc):
                raise
            issues.append(Issue(metadata.MANIFEST_PATH, label + '-metadata-unavailable:' + str(exc)))
            return None

    def _active(self, path, source):
        return (path != b'.gitignore' and path not in self.binding.policy.repository_files
                and any(beneath(path, p) for p in source.scope))

    @staticmethod
    def _live_records(observed):
        """Manifest closure plus observed leafless directory route attributes."""
        records = observed.live_manifest.by_path
        for path, info in observed.routes:
            if path and info[2] == stat.S_IFDIR:
                records.setdefault(path, metadata.Record(path, 'directory', info[4], info[5], info[3], None))
        return records

    def _observe_once(self, source, width):
        issues = []
        remote = self.git.tree_entries(source.remote_oid)
        base = self.git.tree_entries(source.base_oid) if source.base_oid else ()
        rm = self._manifest(remote, width, 'remote', issues)
        bm = self._manifest(base, width, 'base', issues) if source.base_oid else None
        inv = inventory(self.binding.root, source.scope, policy=self.binding.policy,
                        mounts=self.binding.mounts, confirmed_mounts=self.binding.confirmed_mounts,
                        topology_verified=self.binding.topology_verified)
        issues.extend(inv.blockers)
        paths = set(inv.managed_files)
        for e in remote + base:
            if self._active(e.path, source):
                reason = self.binding.policy.reason(e.path)
                if reason or e.mode not in (b'100644', b'100755'):
                    issues.append(Issue(e.path, reason or 'unsupported-tracked-kind'))
                else:
                    paths.add(e.path)
        live, routes, records = [], {}, {}
        viewable, proven_absent = set(), set()
        def retain_routes(seen):
            for route, identity in seen:
                if route in routes and routes[route] != identity:
                    raise Stale('necessary-route-changed-during-observation')
                routes.setdefault(route, identity)
        # Even empty scope must protect the root identity/topology.
        with _route(self.binding, b'', 'directory') as (fd, seen):
            retain_routes(seen)
        for path in sorted(paths):
            try:
                item, seen = _live_file(self.binding, path, width)
                retain_routes(seen)
                viewable.add(path)
                if item is None:
                    route_map = dict(seen)
                    shape = any(q and q != path and info[2] == stat.S_IFREG for q, info in seen)
                    if shape:
                        issues.append(Issue(path, 'unsupported-shape-replacement'))
                    else:
                        proven_absent.add(path)
                        # The first missing component was itself checked through
                        # guarded existing ancestors, even for absent folder scope.
                        prefixes = [b'/'.join(path.split(b'/')[:i]) for i in range(1, len(path.split(b'/')) + 1)]
                        proven_absent.add(next(q for q in prefixes if q not in route_map))
                if item is not None:
                    live.append((path, item))
                    records[path] = item.record
            except Stale:
                raise
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                issues.append(Issue(path, 'live-unavailable:' + type(exc).__name__ + ':' + str(exc)))
        for path in sorted(metadata.ancestors(records)):
            try:
                with _route(self.binding, path, 'directory') as (fd, seen):
                    s = os.fstat(fd)
                    records[path] = metadata.Record(path, 'directory', s.st_uid, s.st_gid,
                                                    stat.S_IMODE(s.st_mode), None)
                    retain_routes(seen)
            except Stale:
                raise
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                issues.append(Issue(path, 'parent-unavailable:' + type(exc).__name__ + ':' + str(exc)))
        lm = metadata.Manifest(tuple(records[p] for p in sorted(records)))
        # Complete live closure may be unavailable, but safe sibling rows survive.
        # Inventory's string diagnostic preserves errno; only its exact ENOENT
        # uncertainty can be superseded by an independent guarded absence proof.
        issues = [i for i in issues if not (i.path in proven_absent and
            re.match(r'^selected-path-unavailable:\[Errno 2\](?: |$)', i.reason))]
        issues = tuple(sorted(set(issues), key=lambda i: (i.path, i.reason)))
        values = (source, width, self.binding, remote, base, tuple(live), lm,
                  tuple(sorted(viewable)), tuple(sorted(routes.items())), issues)
        digest = hashlib.sha256(repr(values).encode('utf-8')).hexdigest()
        return _Observed(source, remote, base, tuple(live), rm, bm, lm,
                         tuple(sorted(viewable)), tuple(sorted(routes.items())), issues, digest)

    def _observe(self):
        source, width = self._source()
        first = self._observe_once(source, width)
        last = self._observe_once(source, width)
        final, final_width = self._source()
        if first.digest != last.digest or final != source or width != final_width:
            raise Stale('complete-preview-source-changed')
        return last

    def _current(self, snapshot_id):
        if type(snapshot_id) is not str or self._snapshot is None or self._snapshot.snapshot_id != snapshot_id:
            raise Stale('current-issued-snapshot-required')
        return self._snapshot

    def _revalidate(self, snapshot):
        current = self._observe()
        if current.digest != snapshot.source_digest:
            raise Stale('preview-sources-changed')
        return current

    def _states(self, tree, manifest, source):
        records = manifest.by_path if manifest is not None else {}
        return {e.path: FileState(e.oid, records.get(e.path)) for e in tree
                if self._active(e.path, source) and not self.binding.policy.reason(e.path)
                and e.mode in (b'100644', b'100755')}

    def _copy(self, scratch, name, path, side, state):
        if state is None:
            null = os.stat('/dev/null', follow_symlinks=False)
            if not stat.S_ISCHR(null.st_mode):
                raise Blocked('dev-null-not-character-device')
            return '/dev/null', False
        with scratch.file(name) as f:
            if side == 'live':
                actual, unused = _live_file(self.binding, path, len(state.oid), f)
                if actual != state:
                    raise Stale('spooled-live-source-changed')
            else:
                self.git.copy_blob(state.oid, f)
            f.flush()
            f.seek(0)
            binary = False
            while True:
                data = f.read(CHUNK)
                if not data:
                    break
                binary |= b'\0' in data
        return name, binary

    def _git_tool(self, scratch, command, output):
        scratch.check()
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(LC_ALL='C', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_CONFIG_SYSTEM='/dev/null', GIT_ATTR_NOSYSTEM='1',
                   GIT_CEILING_DIRECTORIES=os.fsdecode(self.scratch_root),
                   GIT_DISCOVERY_ACROSS_FILESYSTEM='0', GIT_OPTIONAL_LOCKS='0',
                   GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1')
        argv = ['git', '--no-pager', '-c', 'core.hooksPath=/dev/null', '-c',
                'core.attributesFile=/dev/null', '-c', 'core.quotePath=true',
                '-c', 'protocol.allow=never', *command]
        process = None
        with scratch.file(output) as out, scratch.file('stderr') as error:
            primary = None
            try:
                process = subprocess.Popen(argv, cwd=scratch.path, env=env, shell=False,
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=error)
                code = process.wait(timeout=self.timeout)
                out.flush()
                error.flush()
                error.seek(0)
                diagnostic = error.read(CHUNK)
                scratch.check()
                return code, diagnostic
            except BaseException as exc:
                primary = exc
                raise
            finally:
                errors = []
                original_primary = primary
                if process is not None:
                    for attempt in range(2):
                        if not _child_running(process, errors):
                            break
                        try:
                            process.kill()
                            break
                        except BaseException as exc:
                            errors.append('child-kill-attempt-%d:pid=%d:%s' % (attempt + 1, process.pid, exc))
                    try:
                        process.wait(timeout=min(self.timeout, 1.0))
                    except BaseException as exc:
                        errors.append('child-reap:pid=%d:%s' % (process.pid, exc))
                    if _child_running(process, errors):
                        scratch.pending_child = process
                        if primary is None:
                            primary = PreviewFailure('owned-child-cleanup-debt')
                        primary.pending_children = tuple(getattr(primary, 'pending_children', ())) + (process,)
                        errors.append('live-owned-child-retained:pid=%d:scratch=%r' % (process.pid, scratch.path))
                if errors:
                    if primary is not None:
                        primary.cleanup_errors = tuple(getattr(primary, 'cleanup_errors', ())) + tuple(errors)
                        if original_primary is None:
                            raise primary
                    else:
                        error = PreviewFailure('child-cleanup-debt')
                        error.cleanup_errors = tuple(errors)
                        raise error

    def _conflict(self, path, remote, live, base, baseline_known):
        if _same(remote, live):
            return 'clean'
        if not baseline_known:
            return 'base-unavailable'
        if _same(remote, base) or _same(live, base):
            return 'clean'
        if remote is None or live is None:
            return 'modify-delete'
        if base is None:
            return 'add-add'
        try:
            with _Scratch(self) as scratch:
                names = [self._copy(scratch, name, path, side, state)
                         for name, side, state in (('before', 'git', remote), ('base', 'git', base),
                                                   ('after', 'live', live))]
                if any(binary for name, binary in names):
                    return 'binary-concurrent'
                try:
                    code, error = self._git_tool(scratch, ['merge-file', '--stdout', '--diff3',
                        '-L', 'Git', '-L', 'Base', '-L', 'Live', 'before', 'base', 'after'], 'merged')
                except (subprocess.TimeoutExpired, OSError) as exc:
                    exc._preview_merge_tool_failure = True
                    raise
                if code == 0 and not error:
                    return 'clean'
                if 1 <= code <= 127:
                    return 'unresolved'
                return 'merge-unavailable'
        except (subprocess.TimeoutExpired, OSError) as exc:
            if getattr(exc, '_preview_merge_tool_failure', False) and not _has_cleanup_debt(exc):
                return 'merge-unavailable'
            raise

    def build(self, direction):
        if direction not in ('save', 'apply'):
            raise Blocked('save-or-apply-required')
        with self._call():
            self._snapshot = self._observed = self._draft = self._review = None
            self._generation += 1
            observed = self._observe()
            source = observed.source
            remote = self._states(observed.remote, observed.remote_manifest, source)
            remote_modes = {e.path: e.mode for e in observed.remote}
            base = self._states(observed.base, observed.base_manifest, source)
            live = dict(observed.live)
            before, after = (remote, live) if direction == 'save' else (live, remote)
            remote_records = observed.remote_manifest.by_path if observed.remote_manifest else {}
            live_records = self._live_records(observed)
            # Existing necessary parents remain observed even when their active
            # leaf is absent. Do not treat missing manifest closure as physical
            # directory deletion, and never traverse excluded sibling content.
            bmap, amap = (remote_records, live_records) if direction == 'save' else (live_records, remote_records)
            known_leaves = {q for q, r in remote_records.items() if r.kind == 'file'} | set(live)
            parents = []
            for p in sorted(set(bmap) | set(amap)):
                b, a = bmap.get(p), amap.get(p)
                if not any(r is not None and r.kind == 'directory' for r in (b, a)) or b == a:
                    continue
                if not any(beneath(q, p) for q in set(remote) | set(live)):
                    continue
                deps = tuple(sorted(q for q in known_leaves if q.startswith(p + b'/')))
                parents.append(ParentDetail(p, b, a, deps))
            numeric_conflicts = {}
            if observed.base_manifest and observed.remote_manifest:
                try:
                    merged = metadata.merge(observed.base_manifest, observed.remote_manifest,
                                            observed.live_manifest, width=len(source.remote_oid), policy=self.binding.policy)
                    numeric_conflicts = {c.path: tuple(f for f in c.facets if f != 'oid') for c in merged.conflicts}
                except Blocked as exc:
                    if _has_cleanup_debt(exc):
                        raise
                    numeric_conflicts = {p: ('live-metadata-incomplete',) for p in live}
            complete_base = observed.base_manifest.by_path if observed.base_manifest else {}
            parent_details = []
            for parent in parents:
                parent_issues = ()
                if not source.base_oid:
                    parent_issues = ('base-unavailable',)
                elif observed.base_manifest is None or observed.remote_manifest is None:
                    parent_issues = ('parent-metadata-unavailable',)
                else:
                    b = complete_base.get(parent.path)
                    r, l = remote_records.get(parent.path), live_records.get(parent.path)
                    if not (r == l or r == b or l == b):
                        parent_issues = tuple(f for f in ('kind', 'uid', 'gid', 'mode')
                            if r is not None and l is not None and getattr(r, f) != getattr(l, f)) or ('existence',)
                parent_details.append(ParentDetail(parent.path, parent.before, parent.after,
                                                   parent.dependents, parent_issues))
            parents = tuple(parent_details)
            rows = []
            allpaths = set(remote) | set(live)
            for path in sorted(allpaths):
                b, a = before.get(path), after.get(path)
                related = tuple(p.path for p in parents if path in p.dependents)
                issues = tuple(i.reason for i in observed.issues if i.path == path or not i.path
                               or (i.path != metadata.MANIFEST_PATH and path.startswith(i.path + b'/')))
                shape = any(path.startswith(q + b'/') or q.startswith(path + b'/') for q in allpaths if q != path)
                if shape:
                    issues += ('unsupported-shape-replacement',)
                changed = _attributes(b.record if b else None) != _attributes(a.record if a else None)
                if _same(b, a) and not changed and not related and not issues:
                    continue
                viewable = (path in observed.viewable_paths and
                            remote_modes.get(path) in (None, b'100644', b'100755'))
                if not viewable:
                    content = 'unavailable'
                elif b is None:
                    content = 'addition'
                elif a is None:
                    content = 'deletion'
                else:
                    content = 'unchanged' if _same(b, a) else 'modified'
                conflict = (self._conflict(path, remote.get(path), live.get(path), base.get(path), bool(source.base_oid))
                            if viewable else 'source-unavailable')
                if viewable and not source.base_oid and (changed or related):
                    conflict = 'base-unavailable'
                facets = numeric_conflicts.get(path, ())
                if facets:
                    issues += ('metadata-conflict:' + ','.join(facets),)
                rows.append(PathChange(path, b, a, base.get(path), content, changed, conflict, issues, related, viewable))
            # Classification itself reads/spools content; fence the complete source again.
            final = self._observe()
            if final.digest != observed.digest:
                raise Stale('source-changed-during-classification')
            snapshot = PreviewSnapshot(str(uuid.uuid4()), observed.digest, direction, source,
                                       tuple(rows), parents, observed.issues)
            self._snapshot, self._observed = snapshot, observed
            self._draft = Draft(snapshot.snapshot_id, snapshot.source_digest, direction,
                                self._generation, (), ())
            return snapshot

    def select(self, snapshot_id, selected_files, *, approved_parents=()):
        with self._call():
            snapshot = self._current(snapshot_id)
            selected = tuple(_path(p) for p in selected_files)
            approved = tuple(_path(p) for p in approved_parents)
            if len(set(selected)) != len(selected) or len(set(approved)) != len(approved):
                raise Blocked('duplicate-selection-or-parent')
            rows = {r.path: r for r in snapshot.rows}
            if any(p not in rows or not rows[p].selectable for p in selected):
                raise Blocked('current-selectable-row-required')
            parents = {p.path: p for p in snapshot.parents}
            if any(p not in parents or parents[p].issues or not any(q in parents[p].dependents for q in selected)
                   for p in approved):
                raise Blocked('related-resolved-parent-detail-required')
            self._revalidate(snapshot)
            self._draft = Draft(snapshot_id, snapshot.source_digest, snapshot.direction,
                                self._generation, tuple(sorted(selected)), tuple(sorted(approved)))
            self._review = None
            return self._draft

    def review(self, snapshot_id):
        with self._call():
            snapshot = self._current(snapshot_id)
            self._revalidate(snapshot)
            draft = self._draft
            if draft is None or draft.generation != self._generation:
                raise Stale('current-draft-required')
            selected = tuple(r for r in snapshot.rows if r.path in draft.selected)
            parents = tuple(p for p in snapshot.parents if any(q in draft.selected for q in p.dependents))
            blockers = [i.reason for i in snapshot.issues]
            for r in selected:
                blockers.extend(r.issues)
                if r.conflict != 'clean':
                    blockers.append(encode_path(r.path) + ':' + r.conflict)
                for side in (r.before, r.after):
                    if side is not None and side.record is None:
                        blockers.append('missing-restoration-metadata')
            for p in parents:
                blockers.extend(p.issues)
                if p.path not in draft.approved_parents:
                    blockers.append('explicit-parent-review-required:' + encode_path(p.path))
            self._review_binding = (self.binding, self.git, self.git.policy)
            self._review = Review(draft, selected, parents, tuple(sorted(set(blockers))))
            return self._review

    def _candidate_fence(self, review):
        """Only issued selected live states/routes, plus complete source freshness."""
        if type(review) is not Review or self._review is not review or review.draft is not self._draft:
            raise Stale('current-issued-ready-save-review-required')
        snapshot = self._current(review.draft.snapshot_id)
        if (self._review is not review or review.draft is not self._draft or
                review.draft.generation != self._generation or snapshot.direction != 'save' or
                not review.ready or not review.selected or
                self._review_binding != (self.binding, self.git, self.git.policy)):
            raise Stale('current-issued-ready-save-review-required')
        source, width = self._source()
        if source != snapshot.source:
            raise Stale('candidate-source-binding-changed')
        original_routes = dict(self._observed.routes)
        records = self._live_records(self._observed)
        for row in review.selected:
            actual, routes = _live_file(self.binding, row.path, width)
            if actual != row.after or any(original_routes.get(p) != v for p, v in routes):
                raise Stale('selected-live-state-or-route-changed')
        needed = metadata.ancestors(r.path for r in review.selected if r.after is not None)
        needed |= set(review.draft.approved_parents)
        for path in sorted(needed):
            expected = records.get(path)
            # An approved pruned parent may itself be independently absent.
            # Required surviving live parents always have present records.
            with _route(self.binding, path, 'directory', absent=expected is None) as (fd, routes):
                if fd is None:
                    actual = None
                else:
                    info = os.fstat(fd)
                    actual = metadata.Record(path, 'directory', info.st_uid, info.st_gid,
                                             stat.S_IMODE(info.st_mode), None)
                if actual != records.get(path) or any(original_routes.get(p) != v for p, v in routes):
                    raise Stale('necessary-reviewed-parent-changed')
        final, final_width = self._source()
        if final != source or final_width != width:
            raise Stale('candidate-source-changed-during-fence')
        return self._observed

    @contextmanager
    def candidate_context(self, review):
        """Dormant Save peer holds session authority until effects and teardown end.

        Review remains diagnostic for every other consumer. This exact issued
        handle is accepted only under this session lock; no serialized replay.
        Unselected live churn does not cause a whole-source reobservation here.
        """
        with self._call():
            if type(review) is not Review:
                raise Stale('issued-review-required')
            observed = self._candidate_fence(review)
            yield observed
            self._candidate_fence(review)

    def view_file(self, snapshot_id, byte_path, destination):
        with self._call():
            snapshot = self._current(snapshot_id)
            path = _path(byte_path)
            row = next((r for r in snapshot.rows if r.path == path), None)
            if row is None or not row.viewable:
                raise Blocked('current-safe-changed-row-required')
            self._revalidate(snapshot)
            binary = False
            with _Scratch(self) as scratch:
                live_before = snapshot.direction == 'apply'
                before, b = self._copy(scratch, 'before', path, 'live' if live_before else 'git', row.before)
                after, a = self._copy(scratch, 'after', path, 'git' if live_before else 'live', row.after)
                binary = b or a
                _write(destination, ('Path: ' + escaped(path).replace('\n', '\\n') + '\nDirection: ' + snapshot.direction +
                    '\nConflict: ' + row.conflict + '\nIssues: ' + escaped('; '.join(row.issues)) +
                    '\nMetadata: before[' + _display_state(row.before) + '] after[' + _display_state(row.after) + ']\n').encode())
                for p in snapshot.parents:
                    if p.path in row.parents:
                        _write(destination, ('Parent: ' + escaped(p.path) + ' before[' + _display_record(p.before) + '] after[' + _display_record(p.after) + ']' +
                            '\nDependents: ' + ', '.join(escaped(q) for q in p.dependents) + '\n').encode())
                if binary:
                    _write(destination, b'Binary file: whole-file comparison; no text patch.\n')
                else:
                    code, error = self._git_tool(scratch, ['diff', '--no-index', '--no-ext-diff', '--no-textconv',
                        '--no-color', '--no-renames', '--', before, after], 'patch')
                    if code not in (0, 1) or error:
                        raise Unavailable('git-diff-unavailable')
                    with scratch.read('patch') as stream:
                        decoder = codecs.getincrementaldecoder('utf-8')('surrogateescape')
                        while True:
                            data = stream.read(CHUNK)
                            if not data:
                                break
                            _write(destination, escaped(decoder.decode(data)).encode())
                        _write(destination, escaped(decoder.decode(b'', final=True)).encode())
                self._revalidate(snapshot)
            return FileView(snapshot_id, snapshot.source_digest, path, row.conflict, binary, scratch.total)
