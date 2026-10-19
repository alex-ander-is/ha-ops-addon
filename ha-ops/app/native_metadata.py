"""Dormant Etckeeper attribute model, stored as data rather than executable code.

Explicit callers own materialization/review. No Apply, journal, account lookup,
content writes, shell execution or production initialization lives here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import base64
import hashlib
import io
import json
import os
import re
import stat

from native_inventory import Blocked, Policy, path_bytes, _validate_mount, linux_mounts

MANIFEST_PATH = b"data/native/repository-files/etckeeper-metadata.json"
MAX_ID = (1 << 32) - 2  # Linux uid_t/gid_t, excluding the unchanged sentinel.
CHUNK = 65536


@dataclass(frozen=True)
class Record:
    path: bytes
    kind: str
    uid: int
    gid: int
    mode: int
    oid: str | None


@dataclass(frozen=True)
class Manifest:
    entries: tuple[Record, ...]

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))

    @property
    def by_path(self):
        return {r.path: r for r in self.entries}

    @property
    def encoded_size(self):
        return len(_encode(self.by_path))

    @property
    def digest(self):
        return hashlib.sha256(_encode(self.by_path)).hexdigest()


def _path(value):
    if not isinstance(value, bytes):
        raise Blocked("byte-path-required")
    path = path_bytes(value)
    if b"\n" in path or b"\r" in path or b".git" in path.split(b"/"):
        raise Blocked("unsafe-path")
    return path


def ancestors(paths):
    return frozenset(b"/".join(p.split(b"/")[:i]) for p in paths
                     for i in range(1, len(p.split(b"/"))))


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise Blocked("invalid-numeric-attribute")


def _records(manifest, *, width=40, policy=None):
    policy = policy or Policy()
    if width not in (40, 64) or type(width) is not int or not isinstance(manifest, Manifest):
        raise Blocked("invalid-manifest-or-object-width")
    records = {}
    for r in manifest.entries:
        if not isinstance(r, Record):
            raise Blocked("invalid-record")
        _path(r.path)
        if r.path in records:
            raise Blocked("duplicate-path")
        _integer(r.uid, MAX_ID)
        _integer(r.gid, MAX_ID)
        _integer(r.mode, 0o7777)
        if r.kind == "file":
            if (policy.reason(r.path) or r.path == b".gitignore"
                    or r.path in policy.repository_files or r.path == MANIFEST_PATH):
                raise Blocked("non-managed-file")
            if not isinstance(r.oid, str) or not re.fullmatch("[0-9a-f]{%d}" % width, r.oid):
                raise Blocked("invalid-blob-id")
        elif r.kind == "directory":
            if r.oid is not None:
                raise Blocked("directory-blob-id")
            reason = policy.reason(r.path)
            # Proper ancestors of explicit live roots can lie outside leaf scope.
            if reason and reason != "non-managed-root":
                raise Blocked("denied-parent")
        else:
            raise Blocked("unsupported-kind")
        records[r.path] = r
    files = {p for p, r in records.items() if r.kind == "file"}
    parents = ancestors(files)
    if files & parents or {p for p, r in records.items() if r.kind == "directory"} != parents:
        raise Blocked("incomplete-or-conflicting-parent-closure")
    return records


def validate(manifest, tree, *, width=40, policy=None):
    """Bind the complete manifest to exact stage-zero Git file content/mode."""
    policy = policy or Policy()
    records = _records(manifest, width=width, policy=policy)
    files = {}
    seen = set()
    for e in tree:
        _path(e.path)
        if e.path in seen or policy.reason(e.path):
            raise Blocked("duplicate-or-denied-tree-path")
        seen.add(e.path)
        if (getattr(e, "stage", 0) != 0 or getattr(e, "kind", b"blob") != b"blob"
                or e.mode not in (b"100644", b"100755")):
            raise Blocked("unsupported-tree-entry")
        if not isinstance(e.oid, str) or not re.fullmatch("[0-9a-f]{%d}" % width, e.oid):
            raise Blocked("invalid-tree-blob-id")
        if e.path == MANIFEST_PATH and e.mode != b"100644":
            raise Blocked("manifest-must-be-nonexecutable")
        if e.path == b".gitignore" or e.path in policy.repository_files:
            continue
        files[e.path] = e
    if set(files) != {p for p, r in records.items() if r.kind == "file"}:
        raise Blocked("tree-manifest-file-set-mismatch")
    for p, e in files.items():
        r = records[p]
        if r.oid != e.oid or e.mode != (b"100755" if r.mode & 0o100 else b"100644"):
            raise Blocked("tree-manifest-content-or-executable-mismatch")
    return manifest


def encode(manifest, *, width=40, policy=None):
    records = _records(manifest, width=width, policy=policy)
    return _encode(records)


def _encode(records):
    return (json.dumps({"entries": [dict(path=base64.b64encode(r.path).decode("ascii"),
            kind=r.kind, uid=r.uid, gid=r.gid, mode=r.mode, oid=r.oid)
            for _, r in sorted(records.items())]}, sort_keys=True, ensure_ascii=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def decode(data, *, width=40, policy=None):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise Blocked("duplicate-json-key")
            result[k] = v
        return result
    def constant(value):
        raise Blocked("nonfinite-json-number")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if type(obj) is not dict or set(obj) != {"entries"} or type(obj["entries"]) is not list:
            raise Blocked("invalid-manifest-fields")
        rows = []
        for r in obj["entries"]:
            if type(r) is not dict or set(r) != {"path", "kind", "uid", "gid", "mode", "oid"}:
                raise Blocked("invalid-record-fields")
            p = base64.b64decode(r["path"], validate=True)
            if base64.b64encode(p).decode("ascii") != r["path"]:
                raise Blocked("noncanonical-base64-path")
            rows.append(Record(p, r["kind"], r["uid"], r["gid"], r["mode"], r["oid"]))
        result = Manifest(tuple(sorted(rows, key=lambda r: r.path)))
        _records(result, width=width, policy=policy)
        return result
    except (ValueError, TypeError, UnicodeError, KeyError) as exc:
        raise Blocked("invalid-manifest-data") from exc


@dataclass(frozen=True)
class ParentReview:
    path: bytes
    before: Record | None
    after: Record
    dependents: tuple[bytes, ...]

    def __post_init__(self):
        object.__setattr__(self, "dependents", tuple(self.dependents))


@dataclass(frozen=True)
class Selection:
    manifest: Manifest
    parents: tuple[ParentReview, ...]
    baseline: Manifest
    selected: tuple[bytes, ...]

    def __post_init__(self):
        object.__setattr__(self, "parents", tuple(self.parents))
        object.__setattr__(self, "selected", tuple(self.selected))


def select(baseline, fresh, selected, *, approved_parents=(), width=40, policy=None):
    """Overlay selected leaves only; fresh is a complete, validated capture."""
    before = _records(baseline, width=width, policy=policy)
    after = _records(fresh, width=width, policy=policy)
    selected, approved = frozenset(_path(p) for p in selected), frozenset(_path(p) for p in approved_parents)
    files = {p: r for p, r in before.items() if r.kind == "file"}
    for p in selected:
        if p not in files and (p not in after or after[p].kind != "file"):
            raise Blocked("selected-file-missing")
        if p in after and after[p].kind == "file":
            files[p] = after[p]
        else:
            # A former leaf may become a required directory. Its old file is
            # deleted here; added descendants and the new parent need review.
            files.pop(p, None)
    required = ancestors(files)
    if set(files) & required:
        raise Blocked("selected-file-parent-collision")
    if not approved <= required & ancestors(selected):
        raise Blocked("unrelated-parent-approval")
    reviews, result = [], dict(files)
    for p in sorted(required):
        old = before.get(p)
        new = after.get(p)
        if p in approved:
            if new is None or new.kind != "directory":
                raise Blocked("reviewed-parent-missing")
            result[p] = new
            reviews.append(ParentReview(p, old, new, tuple(sorted(c for c in files if c.startswith(p + b"/")))))
        elif old is not None and old.kind == "directory":
            result[p] = old
        else:
            raise Blocked("new-parent-review-required:" + repr(p))
    candidate = Manifest(tuple(result[p] for p in sorted(result)))
    _records(candidate, width=width, policy=policy)
    return Selection(candidate, tuple(reviews), baseline, tuple(sorted(selected)))


def _validate_selection(selection, *, width, policy):
    """Reconstruct declared review data; public values are not authority."""
    if not isinstance(selection, Selection):
        raise Blocked("selection-required")
    candidate = _records(selection.manifest, width=width, policy=policy)
    selected = tuple(_path(p) for p in selection.selected)
    if len(set(selected)) != len(selected):
        raise Blocked("duplicate-selected-leaf")
    reviews = {}
    for review in selection.parents:
        if not isinstance(review, ParentReview):
            raise Blocked("invalid-parent-review")
        path = _path(review.path)
        if path in reviews:
            raise Blocked("duplicate-parent-review")
        for record in (review.before, review.after):
            if record is not None:
                if not isinstance(record, Record):
                    raise Blocked("invalid-parent-review-record")
                _path(record.path)
                _integer(record.uid, MAX_ID)
                _integer(record.gid, MAX_ID)
                _integer(record.mode, 0o7777)
        reviews[path] = review
    expected = select(selection.baseline, selection.manifest, selected,
                      approved_parents=tuple(reviews), width=width, policy=policy)
    if expected.manifest.by_path != candidate:
        raise Blocked("candidate-selection-delta-mismatch")
    for wanted in expected.parents:
        review = reviews[wanted.path]
        dependents = tuple(sorted(_path(p) for p in review.dependents))
        if (review.before != wanted.before or review.after != wanted.after
                or dependents != wanted.dependents):
            raise Blocked("inconsistent-parent-review")
    return selection


@dataclass(frozen=True)
class Conflict:
    path: bytes
    base: Record | None
    ours: Record | None
    theirs: Record | None
    facets: tuple[str, ...]
    dependents: tuple[bytes, ...]


@dataclass(frozen=True)
class Merge:
    entries: tuple[Record, ...]
    conflicts: tuple[Conflict, ...]

    def resolved(self, tree, *, width=40, policy=None):
        if self.conflicts:
            raise Blocked("unresolved-metadata-conflicts")
        return validate(Manifest(self.entries), tree, width=width, policy=policy)


def merge(base, ours, theirs, *, width=40, policy=None):
    maps = [_records(m, width=width, policy=policy) for m in (base, ours, theirs)]
    result, conflicts = {}, []
    all_paths = sorted(set().union(*maps))
    def conflict(p, facets):
        values = tuple(m.get(p) for m in maps)
        children = tuple(q for q in all_paths if q.startswith(p + b"/") and
                         any(m.get(q) is not None and m[q].kind == "file" for m in maps))
        conflicts.append(Conflict(p, *values, tuple(facets), children))
    for p in all_paths:
        b, o, t = (m.get(p) for m in maps)
        if o == t:
            chosen = o
        elif o == b:
            chosen = t
        elif t == b:
            chosen = o
        else:
            facets = [name for name in ("kind", "uid", "gid", "mode", "oid")
                      if o is not None and t is not None and getattr(o, name) != getattr(t, name)]
            conflict(p, facets or ("existence",))
            continue
        if chosen is not None:
            result[p] = chosen
    files = {p for p, r in result.items() if r.kind == "file"}
    possible_files = files | {c.path for c in conflicts if any(r is not None and r.kind == "file"
                             for r in (c.base, c.ours, c.theirs))}
    parents = ancestors(possible_files)
    direct = {c.path for c in conflicts}
    bad = (files & parents) | (parents - {p for p, r in result.items() if r.kind == "directory"})
    bad |= {p for p, r in result.items() if r.kind == "directory"} - parents
    for p in sorted(bad - direct):
        conflict(p, ("parent-closure",))
    return Merge(tuple(result[p] for p in sorted(result)), tuple(sorted(conflicts, key=lambda c: c.path)))


def stage_candidate(git, handle, selection, *, entries=(), removals=(), width=40, admission=None):
    """Caller owns the isolated handle; return only a completely audited tree.

    A failed private-index update must be discarded by exiting its owner context.
    No canonical index, commit, ref, or live file is touched.
    """
    if git.policy.repository_files != (MANIFEST_PATH,):
        raise Blocked("exact-manifest-policy-exception-required")
    if admission is not handle._admission:
        raise Blocked("candidate-admission-handle-mismatch")
    entries, removals = tuple(entries), tuple(removals)
    current = {e.path: e for e in git.index_entries(handle)}
    if MANIFEST_PATH not in current:
        raise Blocked("baseline-manifest-missing")
    original = io.BytesIO()
    git.copy_blob(current[MANIFEST_PATH].oid, original)
    prior = decode(original.getvalue(), width=width, policy=git.policy)
    validate(prior, tuple(current.values()), width=width, policy=git.policy)
    if not isinstance(selection, Selection):
        raise Blocked("selection-required")
    if prior.by_path != _records(selection.baseline, width=width, policy=git.policy):
        raise Blocked("baseline-manifest-stale")
    _validate_selection(selection, width=width, policy=git.policy)
    if not {e.path for e in entries} | set(removals) <= set(selection.selected):
        raise Blocked("unselected-content-update")
    for p in removals:
        current.pop(_path(p), None)
    for e in entries:
        current[e.path] = e
    validate(selection.manifest, tuple(current.values()), width=width, policy=git.policy)
    payload = encode(selection.manifest, width=width, policy=git.policy)
    blob = (git.write_blob_stream(io.BytesIO(payload), operation_id=handle.operation_id,
                owner_id=handle.owner_id, admission=admission) if admission is not None
            else git.write_blob_stream(io.BytesIO(payload)))
    manifest_entry = replace(current[MANIFEST_PATH], mode=b"100644", oid=blob.oid, stage=0)
    # NativeGit forbids simultaneous ancestor/descendant selections. Removing
    # old leaves first admits both reviewed Git shape transitions privately.
    if removals:
        git.update_index(handle, removals=removals)
    git.update_index(handle, entries + (manifest_entry,))
    complete = git.index_entries(handle)
    validate(selection.manifest, complete, width=width, policy=git.policy)
    sink = io.BytesIO()
    git.copy_blob(next(e.oid for e in complete if e.path == MANIFEST_PATH), sink)
    if sink.getvalue() != payload:
        raise Blocked("candidate-manifest-readback-mismatch")
    return git.write_tree(handle, allowed_paths=tuple(e.path for e in complete))


@dataclass(frozen=True)
class Binding:
    root: bytes
    policy: Policy
    mounts: tuple
    confirmed_mounts: tuple[int, ...]
    topology_verified: bool

    def __post_init__(self):
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "confirmed_mounts", tuple(self.confirmed_mounts))
        if not isinstance(self.policy, Policy) or type(self.topology_verified) is not bool:
            raise Blocked("invalid-observation-binding")
        if any(type(i) is not int or i <= 0 for i in self.confirmed_mounts):
            raise Blocked("invalid-confirmed-mount-id")

    def check(self, path, info, current_mounts):
        if not self.topology_verified or not self.mounts:
            raise Blocked("verified-explicit-mount-evidence-required")
        for mount in self.mounts:
            _validate_mount(mount)
        if len({m.id for m in self.mounts}) != len(self.mounts):
            raise Blocked("duplicate-mount-evidence")
        absolute = (self.root.rstrip(b"/") + (b"/" + path if path else b"")) or b"/"
        matches = [m for m in self.mounts if absolute == m.point or
                   absolute.startswith(m.point.rstrip(b"/") + b"/")]
        if not matches:
            raise Blocked("unknown-mount-boundary")
        deepest = max(len(m.point) for m in matches)
        matches = [m for m in matches if len(m.point) == deepest]
        if len(matches) != 1 or matches[0].id not in self.confirmed_mounts:
            raise Blocked("unconfirmed-mount-boundary")
        if matches[0].device != (str(os.major(info.st_dev)) + ":" + str(os.minor(info.st_dev))).encode():
            raise Blocked("descriptor-mount-device-mismatch")
        if current_mounts is not None:
            current = [m for m in current_mounts if absolute == m.point or
                       absolute.startswith(m.point.rstrip(b"/") + b"/")]
            if not current:
                raise Blocked("mount-evidence-stale")
            deepest = max(len(m.point) for m in current)
            current = [m for m in current if len(m.point) == deepest]
            if current != matches:
                raise Blocked("mount-evidence-stale")


def _stat(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_uid, s.st_gid,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _directory_acquisition(info):
    """Named, live directory identity and numeric metadata, not child churn."""
    if not stat.S_ISDIR(info.st_mode) or info.st_nlink == 0:
        raise Blocked('unlinked-or-unsafe-directory-route')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


@dataclass(frozen=True)
class Observation:
    path: bytes
    attributes: tuple
    oid: str | None


@dataclass(frozen=True)
class Before:
    binding: Binding
    entries: tuple[Observation, ...]

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))


@contextmanager
def _opened(binding, path, kind):
    """No-follow absolute/root/relative route; descriptors bounded by depth."""
    fds, routes = [], {}
    primary = None
    try:
        root = binding.root
        if not isinstance(root, bytes) or not root.startswith(b"/") or os.path.abspath(root) != root:
            raise Blocked("canonical-absolute-root-required")
        fd = os.open(b"/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        current_mounts = linux_mounts() if os.path.exists("/proc/self/mountinfo") else None
        for part in (root.split(b"/")[1:] if root != b"/" else ()):
            if not part or part in (b".", b".."):
                raise Blocked("noncanonical-root")
            s = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            fds.append(child)
            acquired = os.fstat(child)
            if (_directory_acquisition(s) != _directory_acquisition(acquired) or
                    _directory_acquisition(os.stat(part, dir_fd=fd, follow_symlinks=False)) != _directory_acquisition(acquired)):
                raise Blocked("changed-root-route")
            fd = child
        binding.check(b"", os.fstat(fd), current_mounts)
        routes[b""] = _stat(os.fstat(fd))
        prefix = b""
        for i, part in enumerate(path.split(b"/") if path else ()):
            prefix = prefix + b"/" + part if prefix else part
            last = i == len(path.split(b"/")) - 1
            s = os.stat(part, dir_fd=fd, follow_symlinks=False)
            binding.check(prefix, s, current_mounts)
            directory = not last or kind == "directory"
            if not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)):
                raise Blocked("unsafe-object-type")
            if not directory and s.st_nlink != 1:
                raise Blocked("multiply-linked-file")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            fds.append(child)
            acquired = os.fstat(child)
            if directory:
                if (_directory_acquisition(s) != _directory_acquisition(acquired) or
                        _directory_acquisition(os.stat(part, dir_fd=fd, follow_symlinks=False)) != _directory_acquisition(acquired)):
                    raise Blocked("changed-object-route")
            elif _stat(s) != _stat(acquired):
                raise Blocked("changed-object-route")
            fd = child
            routes[prefix] = _stat(s)
        yield fd, routes
    except BaseException as exc:
        primary = exc
        raise
    finally:
        errors = []
        for fd in reversed(fds):
            try:
                os.close(fd)
            except BaseException as exc:
                errors.append(type(exc).__name__ + ":" + str(exc))
        if errors:
            if primary is not None:
                primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + tuple(errors)
            else:
                raise OSError("descriptor-close-failed:" + ";".join(errors))


def _hash(fd, width):
    before = _stat(os.fstat(fd))
    if not stat.S_ISREG(before[2]) or before[3] != 1:
        raise Blocked("unsafe-file")
    h = hashlib.sha256() if width == 64 else hashlib.sha1()
    h.update(b"blob " + str(before[6]).encode("ascii") + b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    count = 0
    while chunk := os.read(fd, CHUNK):
        count += len(chunk)
        h.update(chunk)
    if count != before[6] or before != _stat(os.fstat(fd)):
        raise Blocked("changed-during-content-observation")
    return h.hexdigest()


def _write_set(records, selected, approved_parents):
    files = frozenset(_path(p) for p in selected)
    parents = frozenset(_path(p) for p in approved_parents)
    if any(p not in records or records[p].kind != "file" for p in files):
        raise Blocked("unknown-selected-file")
    if not parents <= ancestors(files) or any(records[p].kind != "directory" for p in parents):
        raise Blocked("unrelated-approved-parent")
    order = tuple(sorted(files)) + tuple(sorted(parents, key=lambda p: (-p.count(b"/"), p)))
    return order, files | parents | ancestors(files) | {b""}


def observe(binding, manifest, selected, *, approved_parents=(), width=40):
    """Read fresh before evidence for the explicit write set and routing closure."""
    records = _records(manifest, width=width, policy=binding.policy)
    order, required = _write_set(records, selected, approved_parents)
    seen = {}
    for p in sorted(required):
        kind = records[p].kind if p in records else "directory"
        with _opened(binding, p, kind) as (fd, routes):
            for route, attributes in routes.items():
                if route in seen and seen[route].attributes != attributes:
                    raise Blocked("changed-observation-route")
                if route not in seen:
                    seen[route] = Observation(route, attributes, None)
            oid = _hash(fd, width) if kind == "file" else None
            if kind == "file" and oid != records[p].oid:
                raise Blocked("materialized-content-mismatch")
            seen[p] = Observation(p, _stat(os.fstat(fd)), oid)
        # Reopen the namespace after read and descriptor close.
        with _opened(binding, p, kind) as (fd, routes):
            if any(seen[q].attributes != s for q, s in routes.items()):
                raise Blocked("changed-observation-namespace")
    # A later selected read must not silently stale an earlier recorded inode.
    for p in sorted(required):
        kind = records[p].kind if p in records else "directory"
        with _opened(binding, p, kind) as (fd, routes):
            if any(seen[q].attributes != s for q, s in routes.items()):
                raise Blocked("changed-final-observation")
            if kind == "file" and _hash(fd, width) != seen[p].oid:
                raise Blocked("changed-final-content")
    return Before(binding, tuple(seen[p] for p in sorted(seen)))


def capture(binding, tree, *, width=40):
    """Capture only caller-selected exact managed Git entries, plus proper parents."""
    tree = tuple(tree)
    placeholder = []
    for e in tree:
        placeholder.append(Record(e.path, "file", 0, 0, 0o755 if e.mode == b"100755" else 0o644, e.oid))
    placeholder.extend(Record(p, "directory", 0, 0, 0o755, None) for p in ancestors(e.path for e in tree))
    manifest = Manifest(tuple(placeholder))
    validate(manifest, tree, width=width, policy=binding.policy)
    before = observe(binding, manifest, (e.path for e in tree), approved_parents=ancestors(e.path for e in tree), width=width)
    measured = {o.path: o for o in before.entries}
    result = Manifest(tuple(sorted((Record(r.path, r.kind, measured[r.path].attributes[4],
                    measured[r.path].attributes[5], stat.S_IMODE(measured[r.path].attributes[2]), r.oid)
                    for r in manifest.entries), key=lambda r: r.path)))
    validate(result, tree, width=width, policy=binding.policy)
    return result


@dataclass(frozen=True)
class Effects:
    completed: tuple[bytes, ...]
    attempted: tuple[bytes, ...]
    uncertain: tuple[bytes, ...]
    failed: bytes | None
    unattempted: tuple[bytes, ...]
    secondary: tuple[str, ...] = ()


class RestoreFailure(RuntimeError):
    def __init__(self, cause, effects):
        super().__init__("metadata-restore-failed:" + type(cause).__name__ + ":" + str(cause))
        self.cause, self.effects = cause, effects


def restore(binding, manifest, tree, selected, before, *, approved_parents=(), width=40):
    """Metadata-only exact restore; errors carry truthful partial-effect evidence."""
    validate(manifest, tree, width=width, policy=binding.policy)
    records = manifest.by_path
    selected, approved_parents = tuple(selected), tuple(approved_parents)
    order, required = _write_set(records, selected, approved_parents)
    if not isinstance(before, Before) or before.binding != binding:
        raise Blocked("before-binding-mismatch")
    expected = {o.path: o for o in before.entries}
    if len(expected) != len(before.entries) or set(expected) != required:
        raise Blocked("incomplete-before-observations")
    completed, attempted = [], []
    current = None
    def check(p, fd, routes, *, after=False):
        for q, s in routes.items():
            wanted = expected[q].attributes
            if q == p and after:
                if s[:2] != wanted[:2] or s[3] != wanted[3] or stat.S_IFMT(s[2]) != stat.S_IFMT(wanted[2]):
                    raise Blocked("changed-restored-identity")
            elif s != wanted:
                raise Blocked("stale-before-observation")
        if records.get(p) is not None and records[p].kind == "file":
            oid = _hash(fd, width)
            if expected[p].oid != records[p].oid or oid != records[p].oid:
                raise Blocked("stale-reviewed-content")
    def fence(p, fd, *, changed=False):
        with _opened(binding, p, records[p].kind) as (other, routes):
            held = _stat(os.fstat(fd))
            if routes[p] != held:
                raise Blocked("changed-immediate-namespace")
            for q, s in routes.items():
                if q != p and s != expected[q].attributes:
                    raise Blocked("changed-immediate-parent")
            if not changed and held != expected[p].attributes:
                raise Blocked("changed-immediate-object")
    try:
        # Complete prevalidation precedes the first metadata syscall.
        for current in order:
            with _opened(binding, current, records[current].kind) as (fd, routes):
                check(current, fd, routes)
        for current in order:
            r = records[current]
            with _opened(binding, current, r.kind) as (fd, routes):
                check(current, fd, routes)
                s = os.fstat(fd)
                fence(current, fd)
                attempted.append(current)  # A raising syscall can already have effects.
                if (s.st_uid, s.st_gid) != (r.uid, r.gid):
                    os.fchown(fd, r.uid, r.gid)
                fence(current, fd, changed=True)
                os.fchmod(fd, r.mode)  # Mandatory after chown, even owner-only changes.
                s = os.fstat(fd)
                if (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) != (r.uid, r.gid, r.mode):
                    raise Blocked("attribute-readback-mismatch")
                if r.kind == "file" and _hash(fd, width) != r.oid:
                    raise Blocked("post-effect-content-mismatch")
                os.fsync(fd)
            with _opened(binding, current, r.kind) as (fd, routes):
                check(current, fd, routes, after=True)
                s = os.fstat(fd)
                if (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) != (r.uid, r.gid, r.mode):
                    raise Blocked("namespace-attribute-readback-mismatch")
                updated = Observation(current, _stat(s), r.oid)
            expected[current] = updated
            completed.append(current)
    except BaseException as exc:
        effects = Effects(tuple(completed), tuple(attempted), tuple(p for p in attempted if p not in completed),
                          current, tuple(p for p in order if p not in attempted), tuple(getattr(exc, "cleanup_errors", ())))
        raise RestoreFailure(exc, effects) from exc
    return Effects(tuple(completed), tuple(attempted), (), None, ())
