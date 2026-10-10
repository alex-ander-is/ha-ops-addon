"""Dormant, metadata-only native scope diagnostics. Never a write capability.

Paths are Unix filesystem bytes. No production module imports this API.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
import re
import stat
import subprocess
import sys


class Blocked(ValueError):
    """An input cannot safely produce a complete diagnostic."""


def _has_cleanup_debt(error):
    """Diagnostic conversion is safe only after all owned resources are settled."""
    return bool(getattr(error, "cleanup_errors", ()) or
                getattr(error, "pending_children", ()))


def path_bytes(value):
    value = os.fsencode(value)
    if (not value or value.startswith(b"/") or b"\0" in value
            or any(p in (b"", b".", b"..") for p in value.split(b"/"))):
        raise Blocked("noncanonical-relative-path")
    return value


def beneath(path, parent):
    return path == parent or path.startswith(parent + b"/")


def normalize_scope(paths):
    result = []
    for path in sorted({path_bytes(p) for p in paths}):
        if not any(beneath(path, p) for p in result):
            result.append(path)
    return tuple(result)


@dataclass(frozen=True)
class Issue:
    path: bytes
    reason: str

    @property
    def display_path(self):
        return repr(os.fsdecode(self.path))[1:-1]


@dataclass(frozen=True)
class Mount:
    id: int
    parent: int
    device: bytes
    root: bytes
    point: bytes
    options: bytes
    filesystem: bytes
    source: bytes
    super_options: bytes


def _mount_path(value):
    if re.search(rb"\\(?!040|011|012|134)", value):
        raise Blocked("malformed-mount-escape")
    return re.sub(rb"\\(040|011|012|134)", lambda m: bytes([int(m[1], 8)]), value)


def _validate_mount(mount):
    if (not isinstance(mount, Mount) or not isinstance(mount.id, int) or mount.id <= 0
            or not isinstance(mount.parent, int) or mount.parent < 0
            or not isinstance(mount.device, bytes) or not re.fullmatch(rb"\d+:\d+", mount.device)):
        raise Blocked("malformed-mountinfo")
    if any(not isinstance(value, bytes) for value in (mount.options, mount.filesystem, mount.source, mount.super_options)):
        raise Blocked("malformed-mountinfo")
    for value in (mount.point, mount.root):
        if not isinstance(value, bytes) or not value.startswith(b"/"):
            raise Blocked("malformed-mountinfo")
        if value != b"/":
            path_bytes(value[1:])


def parse_mountinfo(data):
    mounts = []
    for line in data.split(b"\n"):
        if not line:
            continue
        fields = line.split(b" ")
        try:
            sep = fields.index(b"-")
            if sep < 6 or len(fields) != sep + 4 or not re.fullmatch(rb"\d+:\d+", fields[2]):
                raise ValueError()
            mount = Mount(int(fields[0]), int(fields[1]), fields[2], _mount_path(fields[3]),
                          _mount_path(fields[4]), fields[5], fields[sep + 1],
                          _mount_path(fields[sep + 2]), fields[sep + 3])
            _validate_mount(mount)
            mounts.append(mount)
        except (ValueError, IndexError) as exc:
            raise Blocked("malformed-mountinfo") from exc
    if not mounts or len({m.id for m in mounts}) != len(mounts):
        raise Blocked("missing-or-duplicate-mountinfo")
    return tuple(mounts)


def linux_mounts():
    """Read only the kernel topology record, never configuration contents."""
    with open("/proc/self/mountinfo", "rb") as stream:
        return parse_mountinfo(stream.read())


# This finite matrix is deliberately independent of legacy export projections.
DENY_MATRIX = b"native-known-secrets-v1:secrets.yaml;storage/core.config_entries,auth,auth_provider.homeassistant;.cloud;.google.token;.ssh;.gnupg;.git-credentials;.git;zigbee2mqtt/coordinator_backup*.json;reserved/config,data,backup,system;unsupported/LF,CR"
POLICY_VERSION = "native-known-secrets-v1"
POLICY_DIGEST = hashlib.sha256(DENY_MATRIX).hexdigest()


@dataclass(frozen=True)
class Policy:
    self_slug: bytes = b"07ef30c0_ha_ops"
    legacy_repo: bytes = b"data/repo"
    extra_denies: tuple = ()
    extra_live_roots: tuple = ()
    repository_files: tuple = ()

    def __post_init__(self):
        slug = path_bytes(self.self_slug)
        if b"/" in slug:
            raise Blocked("invalid-self-slug")
        object.__setattr__(self, "self_slug", slug)
        legacy = path_bytes(self.legacy_repo)
        if not beneath(legacy, b"data") or legacy == b"data" or beneath(legacy, b"data/native"):
            raise Blocked("invalid-legacy-repo")
        object.__setattr__(self, "legacy_repo", legacy)
        for attr in ("extra_denies", "extra_live_roots", "repository_files"):
            object.__setattr__(self, attr, normalize_scope(getattr(self, attr)))
        for path in self.repository_files:
            if not beneath(path, b"data/native/repository-files") or path == b"data/native/repository-files":
                raise Blocked("invalid-repository-only-file")

    @property
    def live_roots(self):
        return (b"homeassistant", b"addon_configs") + self.extra_live_roots

    @property
    def digest(self):
        fields = (self.self_slug, self.legacy_repo) + self.extra_denies + (b"",) + self.extra_live_roots + (b"",) + self.repository_files
        return hashlib.sha256(DENY_MATRIX + b"\0" + b"\0".join(fields)).hexdigest()

    def reason(self, path):
        path = path_bytes(path)
        if b"\n" in path or b"\r" in path:
            return "unsupported-line-break-name"
        parts = path.split(b"/")
        if b".git" in parts:
            return "git-metadata"
        if any(beneath(path, p) for p in self.extra_denies):
            return "configured-deny"
        if beneath(path, b"config"):
            return "duplicate-config-alias"
        if beneath(path, self.legacy_repo):
            return "legacy-checkout"
        if beneath(path, b"backup"):
            return "backup-root"
        if parts[0] in (b"proc", b"sys", b"dev", b"run", b"tmp", b"bin", b"sbin", b"usr", b"lib", b"lib64", b"etc", b"root", b"var", b"opt"):
            return "container-system-root"
        if beneath(path, b"data"):
            if path in self.repository_files:
                return None
            return "service-data"
        if path == b".gitignore":
            return None
        if not any(beneath(path, root) for root in self.live_roots):
            return "non-managed-root"
        if b"secrets.yaml" in parts:
            return "known-secret-secrets-yaml"
        if any(p in (b".ssh", b".gnupg", b".git-credentials") for p in parts):
            return "known-credentials"
        if b".cloud" in parts or b".google.token" in parts:
            return "known-cloud-credentials"
        for index, component in enumerate(parts[:-1]):
            if component == b".storage" and parts[index + 1] in (b"core.config_entries", b"auth", b"auth_provider.homeassistant"):
                return "known-storage-credentials"
            if component == b"zigbee2mqtt" and parts[index + 1].startswith(b"coordinator_backup") and parts[index + 1].endswith(b".json"):
                return "known-coordinator-backup"
        return None

    def scaffold(self, path):
        return any(p.startswith(path + b"/") for p in self.repository_files)

    def warnings(self, path, size):
        result = []
        if b".storage" in path.split(b"/") and path.split(b"/")[-1] not in (b"core.device_registry", b"core.entity_registry"):
            result.append("unclassified-storage-content")
        if (path.endswith((b".db", b".db-wal", b".db-shm", b".log"))
                or any(p in (b".cache", b"node_modules", b"cache", b"media") for p in path.split(b"/"))):
            result.append("runtime-changing-file")
        if size >= 64 * 1024 * 1024:
            result.append("large-file-no-admission-check")
        return tuple(result)


@dataclass(frozen=True)
class Entry:
    path: bytes
    kind: str
    size: int
    device: int
    inode: int
    mode: int
    links: int
    mount_id: int | None
    selected: bool
    reason: str | None
    warnings: tuple = ()
    alias_target: bytes | None = None

    @property
    def display_path(self):
        return repr(os.fsdecode(self.path))[1:-1]


@dataclass(frozen=True)
class Inventory:
    scope: tuple
    entries: tuple
    blockers: tuple
    topology_verified: bool
    diagnostics: tuple = ()

    @property
    def managed_files(self):
        return tuple(e.path for e in self.entries if e.selected and e.reason is None and e.kind == "file")


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _observation(info):
    return _identity(info) + (info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def _open_absolute_directory(path):
    """Check every namespace component; no resolve(), no linked ancestors."""
    path = os.fsencode(os.path.abspath(path))
    fd = os.open(b"/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split(b"/")[1:]:
            if not part:
                continue
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            # The new child is owned before fstat or parent handoff can fail.
            try:
                if _identity(before) != _identity(os.fstat(child)):
                    raise Blocked("changed-directory")
            except BaseException as primary:
                try:
                    os.close(child)
                except BaseException as cleanup:
                    primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("child-fd-close:fd=%d:" % child + type(cleanup).__name__ + ":" + str(cleanup),)
                raise
            parent, fd = fd, child
            try:
                os.close(parent)
            except BaseException as primary:
                # close may have succeeded before raising; never retry its fd.
                primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("parent-handoff-close:fd=%d:" % parent + type(primary).__name__ + ":" + str(primary),)
                raise
        return fd
    except BaseException as primary:
        try:
            os.close(fd)
        except BaseException as cleanup:
            primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("parent-fd-close:fd=%d:" % fd + type(cleanup).__name__ + ":" + str(cleanup),)
        raise


def _directory_at(root_fd, path, *, before_open=None):
    """Acquire no-follow descendants, optionally fenced before each component.

    Inventory supplies its mount guard for every acquisition. Separate policy
    and Git diagnostics retain their existing explicit-location contract.
    """
    fd = os.dup(root_fd)
    try:
        relative = b""
        for part in path.split(b"/") if path else ():
            relative = relative + b"/" + part if relative else part
            if before_open is not None:
                before_open(relative)
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            # The new child is owned before fstat or parent handoff can fail.
            try:
                if _identity(before) != _identity(os.fstat(child)):
                    raise Blocked("changed-directory")
            except BaseException as primary:
                try:
                    os.close(child)
                except BaseException as cleanup:
                    primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("child-fd-close:fd=%d:" % child + type(cleanup).__name__ + ":" + str(cleanup),)
                raise
            parent, fd = fd, child
            try:
                os.close(parent)
            except BaseException as primary:
                # close may have succeeded before raising; never retry its fd.
                primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("parent-handoff-close:fd=%d:" % parent + type(primary).__name__ + ":" + str(primary),)
                raise
        return fd
    except BaseException as primary:
        try:
            os.close(fd)
        except BaseException as cleanup:
            primary.cleanup_errors = tuple(getattr(primary, "cleanup_errors", ())) + ("parent-fd-close:fd=%d:" % fd + type(cleanup).__name__ + ":" + str(cleanup),)
        raise


def inventory(root, selected=(), *, policy=None, mounts=(), confirmed_mounts=(), topology_verified=False):
    """Inspect a supplied root. Synthetic evidence remains explicitly unverified.

    A mounted descendant is crossed only by explicit mount ID confirmation.
    Denied directory rows remain visible, but their contents are never enumerated.
    """
    policy = policy or Policy()
    scope = normalize_scope(selected)
    root = os.fsencode(os.path.abspath(root))
    mounts = tuple(mounts)
    try:
        for mount in mounts:
            _validate_mount(mount)
        if len({m.id for m in mounts}) != len(mounts):
            raise Blocked("duplicate-mount-id")
    except Blocked as exc:
        if _has_cleanup_debt(exc):
            raise
        issue = Issue(b"", "invalid-mount-evidence")
        return Inventory(scope, (), (issue,), topology_verified, (issue,))
    def close_owned(fd):
        # Preserve a propagating primary even if this later close also fails.
        # A close failure itself is addressed debt, never a plain Issue.
        _, primary, trace = sys.exc_info()
        owner_frame = sys._getframe(1)
        # An enclosing caller's handled exception is not this operation's
        # primary. Only a traceback through this exact inventory frame counts.
        while trace is not None and trace.tb_frame is not owner_frame:
            trace = trace.tb_next
        if trace is None:
            primary = None
        try:
            os.close(fd)
        except BaseException as cleanup:
            debt = ("inventory-fd-close:fd=%d:" % fd + type(cleanup).__name__ + ":" + str(cleanup),)
            target = primary if primary is not None else cleanup
            target.cleanup_errors = tuple(getattr(target, "cleanup_errors", ())) + debt + (
                tuple(getattr(cleanup, "cleanup_errors", ())) if cleanup is not target else ())
            if cleanup is not target and getattr(cleanup, "pending_children", ()):
                target.pending_children = tuple(getattr(target, "pending_children", ())) + tuple(cleanup.pending_children)
            if primary is None:
                raise
    confirmed = frozenset(confirmed_mounts)
    entries, issues, blockers, observations = [], [], [], []
    established = {}
    def chosen(path):
        return policy.reason(path) is None and any(beneath(path, p) for p in scope)
    def parent_role(path):
        # Root and proper selected-route prefixes retain their safety role
        # even when first observed with an unsafe non-directory kind.
        return not path or any(p.startswith(path + b"/") for p in scope)
    def required(path):
        return parent_role(path) or chosen(path) or path in scope
    def issue(path, reason, blocking=False):
        item = Issue(path, reason)
        if item not in issues:
            issues.append(item)
        if blocking and item not in blockers:
            blockers.append(item)
    def establish(path, observation):
        established.setdefault(path, observation)
        if parent_role(path) and not stat.S_ISDIR(observation[2]):
            issue(path, "unsafe-required-parent", True)
    def kind_of(info):
        return ("directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode)
                else "symlink" if stat.S_ISLNK(info.st_mode) else "special")
    def traversable(path):
        reason = policy.reason(path)
        return reason is None or (reason == "service-data" and policy.scaffold(path))
    def unknown_or_relevant(path, error):
        # Only actual disappearance can reuse a known unsupported leaf's
        # exclusion. Other failures leave its current eligible kind unknown.
        before = established.get(path)
        return (path in scope or parent_role(path) or
                (chosen(path) and (not isinstance(error, FileNotFoundError) or before is None
                                  or stat.S_ISREG(before[2]) or stat.S_ISDIR(before[2]))))
    def relevant_observation(path, before, after):
        if parent_role(path):
            return after is None or before[:3] != after[:3]
        if after is not None and before[:3] == after[:3] and stat.S_ISDIR(before[2]):
            return False
        return required(path) and (stat.S_ISDIR(before[2]) or
                                  (stat.S_ISREG(before[2]) and chosen(path)) or path in scope)
    points = {}
    for mount in mounts:
        points.setdefault(mount.point, []).append(mount)
    def mount_at(absolute):
        matching = [p for p in points if absolute == p or absolute.startswith(p.rstrip(b"/") + b"/")]
        if not matching:
            return None
        records = points[max(matching, key=len)]
        return records[0] if len(records) == 1 else None
    def filesystem_path(mount, absolute):
        suffix = absolute[len(mount.point.rstrip(b"/")):].lstrip(b"/")
        return mount.root.rstrip(b"/") + b"/" + suffix if suffix else mount.root.rstrip(b"/") or b"/"
    try:
        root_fd = _open_absolute_directory(root)
    except (OSError, Blocked) as exc:
        if _has_cleanup_debt(exc):
            raise
        item = Issue(b"", "root-unavailable:" + type(exc).__name__)
        return Inventory(scope, (), (item,), topology_verified, (item,))
    try:
        root_info = os.fstat(root_fd)
        base_mount = mount_at(root)
        if base_mount is None or base_mount.id not in confirmed:
            item = Issue(b"", "unconfirmed-root-mount")
            return Inventory(scope, (), (item,), topology_verified, (item,))
        def require_confirmed_component(path):
            mount = mount_at(root.rstrip(b"/") + b"/" + path)
            if mount is None or mount.id not in confirmed:
                issue(path, "unconfirmed-mount-boundary", required(path))
                raise Blocked("unconfirmed-mount-boundary")
        def inventory_directory(path):
            def before_open(component):
                require_confirmed_component(component)
                if not traversable(component):
                    reason = policy.reason(component)
                    issue(component, reason, required(component))
                    raise Blocked(reason)
            return _directory_at(root_fd, path, before_open=before_open)
        def check_directory(fd, directory, before_dir, names, children):
            current_names = tuple(sorted(os.fsencode(p) for p in os.listdir(fd)))
            after_children = {}
            for name in current_names:
                path = directory + b"/" + name if directory else name
                try:
                    after_children[name] = _observation(os.stat(name, dir_fd=fd, follow_symlinks=False))
                except OSError as exc:
                    if _has_cleanup_debt(exc):
                        raise
                    issue(path, "incomplete-or-changed:" + str(exc), unknown_or_relevant(path, exc))
            previous = dict(children)
            if current_names != names:
                issue(directory, "changed-directory-membership")
            def eligible(values):
                return {name for name, obs in values.items()
                        if policy.reason(directory + b"/" + name if directory else name) is None
                        and (stat.S_ISREG(obs[2]) or stat.S_ISDIR(obs[2]))}
            if directory and chosen(directory) and eligible(previous) != eligible(after_children):
                issue(directory, "changed-selected-membership", True)
            for name, before in children:
                path = directory + b"/" + name if directory else name
                after = after_children.get(name)
                if after != before:
                    issue(path, "changed-entry", relevant_observation(path, before, after))
            after_dir = _observation(os.fstat(fd))
            if after_dir != before_dir:
                issue(directory, "changed-directory",
                      required(directory) and after_dir[:3] != before_dir[:3])
        # Alias evidence requires both mount/source identity and directory identity.
        alias_ok = False
        alias_present = False
        try:
            alias_info = os.stat(b"config", dir_fd=root_fd, follow_symlinks=False)
            alias_present = True
            canonical = b"addon_configs/" + policy.self_slug
            canonical_fd = inventory_directory(canonical)
            try:
                alias_path = root.rstrip(b"/") + b"/config"
                canonical_path = root.rstrip(b"/") + b"/" + canonical
                left, right = mount_at(alias_path), mount_at(canonical_path)
                alias_ok = (stat.S_ISDIR(alias_info.st_mode) and _identity(alias_info) == _identity(os.fstat(canonical_fd))
                            and left is not None and right is not None and left.id != right.id
                            and left.source == right.source and left.device == right.device
                            and filesystem_path(left, alias_path) == filesystem_path(right, canonical_path)
                            and left.id in confirmed and right.id in confirmed)
            finally:
                close_owned(canonical_fd)
            if not alias_ok:
                issue(b"config", "unconfirmed-config-alias")
        except FileNotFoundError as exc:
            if _has_cleanup_debt(exc):
                raise
            if alias_present:
                issue(b"config", "unconfirmed-config-alias")
        except (OSError, Blocked) as exc:
            if _has_cleanup_debt(exc):
                raise
            issue(b"config", "unconfirmed-config-alias")
        pending = [(b"", root_info)]
        while pending:
            directory, expected = pending.pop()
            fd = None
            identity_validated = False
            try:
                fd = inventory_directory(directory)
                if _identity(os.fstat(fd)) != _identity(expected):
                    raise Blocked("changed-directory")
                identity_validated = True
                names = tuple(sorted(os.fsencode(p) for p in os.listdir(fd)))
                directory_observations = []
                for name in names:
                    path = directory + b"/" + name if directory else name
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError as exc:
                        if _has_cleanup_debt(exc):
                            raise
                        issue(path, "incomplete-or-changed:" + str(exc), unknown_or_relevant(path, exc))
                        continue
                    directory_observations.append((name, _observation(info)))
                    establish(path, _observation(info))
                    kind = kind_of(info)
                    reason = policy.reason(path)
                    selected_path = any(beneath(path, p) for p in scope)
                    mount = mount_at(root.rstrip(b"/") + b"/" + path)
                    if kind in ("symlink", "special") and reason is None:
                        reason = "unsupported-" + kind
                    mount_ok = mount is not None and mount.id in confirmed
                    if not mount_ok:
                        reason = reason or "unconfirmed-mount-boundary"
                        issue(path, "unconfirmed-mount-boundary", required(path))
                    if path == b"config" and not alias_ok and not any(i.path == path and i.reason == "unconfirmed-config-alias" for i in issues):
                        issue(path, "unconfirmed-config-alias")
                    if path in scope and reason is not None:
                        issue(path, reason, True)
                    target = b"addon_configs/" + policy.self_slug if path == b"config" else None
                    entries.append(Entry(path, kind, info.st_size, info.st_dev, info.st_ino, info.st_mode,
                                         info.st_nlink, mount.id if mount else None, selected_path and reason is None and mount_ok,
                                         reason, policy.warnings(path, info.st_size) if reason is None else (), target))
                    if kind == "directory" and mount_ok and traversable(path):
                        pending.append((path, info))
                observations.append((directory, _observation(expected), names, tuple(directory_observations)))
                check_directory(fd, directory, _observation(expected), names, directory_observations)
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                # Acquisition/identity protects every required parent, even
                # after recovery. Listing protects selected folder membership;
                # exact leaves retain independent proof below.
                issue(directory, "incomplete-or-changed:" + str(exc),
                      (chosen(directory) if directory else False) if identity_validated else required(directory))
            finally:
                if fd is not None:
                    close_owned(fd)
        # Recheck namespace membership after traversal, including scanned ancestors.
        for directory, identity, names, children in observations:
            fd = None
            identity_validated = False
            try:
                fd = inventory_directory(directory)
                if _identity(os.fstat(fd)) != identity[:3]:
                    raise Blocked("changed-directory")
                identity_validated = True
                check_directory(fd, directory, identity, names, children)
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                issue(directory, "changed-directory",
                      (chosen(directory) if directory else False) if identity_validated else required(directory))
            finally:
                if fd is not None:
                    close_owned(fd)
        # Prove required prefixes and exact selections without sibling listing.
        # Never reset an observation already established by the broad scan.
        needed = {p for path in scope for p in
                  (b"/".join(path.split(b"/")[:i]) for i in range(1, len(path.split(b"/")) + 1))}
        needed.update(e.path for e in entries if e.selected)
        seen = {e.path for e in entries}
        for path in sorted(needed):
            fd = None
            try:
                reason = policy.reason(path)
                if reason and path in scope:
                    issue(path, reason, True)
                    continue
                if not traversable(path):
                    continue
                parent, _, name = path.rpartition(b"/")
                fd = inventory_directory(parent)
                require_confirmed_component(path)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                after = _observation(info)
                before = established.get(path)
                establish(path, after)
                if before is not None and after != before:
                    issue(path, "changed-entry", relevant_observation(path, before, after))
                kind = kind_of(info)
                if kind not in ("file", "directory"):
                    issue(path, "unsupported-" + kind, True)
                if path not in seen and path in scope:
                    if kind == "directory":
                        issue(path, "selected-path-unavailable", True)
                    elif kind == "file" and reason is None:
                        # Two direct observations establish a previously
                        # unseen exact leaf; metadata only, no content opens.
                        again = _observation(os.stat(name, dir_fd=fd, follow_symlinks=False))
                        if again != after:
                            issue(path, "changed-entry", True)
                        mount = mount_at(root.rstrip(b"/") + b"/" + path)
                        entries.append(Entry(path, kind, info.st_size, info.st_dev, info.st_ino, info.st_mode,
                                             info.st_nlink, mount.id, True, None, policy.warnings(path, info.st_size)))
                        seen.add(path)
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                issue(path, "selected-path-unavailable:" + str(exc), True)
            finally:
                if fd is not None:
                    close_owned(fd)
        # A fallback must prove its newly acquired prefixes remain the same
        # namespace, as well as retaining every earlier selected observation.
        for path in sorted(needed):
            if path not in established or not traversable(path):
                continue
            fd = None
            try:
                parent, _, name = path.rpartition(b"/")
                fd = inventory_directory(parent)
                require_confirmed_component(path)
                after = _observation(os.stat(name, dir_fd=fd, follow_symlinks=False))
                before = established[path]
                if after != before:
                    issue(path, "changed-entry", relevant_observation(path, before, after))
            except (OSError, Blocked) as exc:
                if _has_cleanup_debt(exc):
                    raise
                issue(path, "selected-path-unavailable:" + str(exc), True)
            finally:
                if fd is not None:
                    close_owned(fd)
        fresh = _open_absolute_directory(root)
        try:
            if _identity(os.fstat(fresh)) != _identity(root_info):
                issue(b"", "changed-root", True)
        finally:
            close_owned(fresh)
    except (OSError, Blocked) as exc:
        if _has_cleanup_debt(exc):
            raise
        issue(b"", "incomplete-or-changed:" + str(exc), True)
    finally:
        close_owned(root_fd)
    return Inventory(scope, tuple(sorted(entries, key=lambda e: e.path)), tuple(blockers), topology_verified, tuple(issues))


def _literal(path):
    if b"\n" in path or b"\r" in path:
        raise Blocked("unrepresentable-policy-path")
    return b"".join(b"\\" + bytes([c]) if c in b"\\*?[]!# " else bytes([c]) for c in path)


def render_ignore(snapshot, policy=None):
    policy = policy or Policy()
    if snapshot.blockers:
        raise Blocked("incomplete-scope")
    files = set(snapshot.managed_files) | {b".gitignore"}
    if any(policy.reason(p) is not None for p in files):
        raise Blocked("prohibited-policy-path")
    directories = {b"/".join(p.split(b"/")[:i]) for p in files for i in range(1, len(p.split(b"/")))}
    rows = [b"/*"]
    for path in sorted(directories):
        literal = _literal(path)
        rows.extend((b"!/" + literal + b"/", b"/" + literal + b"/*"))
    rows.extend(b"!/" + _literal(p) for p in sorted(files))
    return b"\n".join(rows) + b"\n"


@dataclass(frozen=True)
class Audit:
    blockers: tuple = ()

    @property
    def clean(self):
        return not self.blockers


def allowed_path(path, scope, policy):
    return policy.reason(path) is None and (path == b".gitignore" or any(beneath(path, p) for p in scope))


def audit_index_output(data, scope, policy=None, *, returncode=0):
    policy = policy or Policy()
    scope = normalize_scope(scope)
    issues, seen = [], set()
    if returncode != 0:
        return Audit((Issue(b"", "index-command-failed"),))
    if data and not data.endswith(b"\0"):
        issues.append(Issue(b"", "truncated-index-output"))
    for record in data.split(b"\0")[:-1]:
        try:
            header, path = record.split(b"\t", 1)
            mode, oid, stage = header.split(b" ")
            path_bytes(path)
            if not re.fullmatch(rb"[0-7]{6}", mode) or not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid) or stage not in (b"0", b"1", b"2", b"3"):
                raise ValueError()
            if path in seen:
                issues.append(Issue(path, "duplicate-or-conflicting-index-entry"))
            seen.add(path)
            if stage != b"0":
                issues.append(Issue(path, "unmerged-index-stage"))
            if mode not in (b"100644", b"100755"):
                issues.append(Issue(path, "unsupported-index-mode"))
            if not allowed_path(path, scope, policy):
                issues.append(Issue(path, policy.reason(path) or "outside-selected-scope"))
        except (ValueError, Blocked):
            issues.append(Issue(b"", "malformed-index-record"))
    return Audit(tuple(issues))


def audit_ignore_output(data, paths, scope, policy=None, *, returncode=0):
    policy = policy or Policy()
    expected = tuple(sorted({path_bytes(p) for p in paths}))
    snapshot = scope if isinstance(scope, Inventory) else None
    scope = snapshot.scope if snapshot else normalize_scope(scope)
    allowed = set(snapshot.managed_files) | {b".gitignore"} if snapshot else None
    parents = {b"/".join(p.split(b"/")[:i]) for p in (allowed if allowed is not None else scope)
               for i in range(1, len(p.split(b"/")))}
    if returncode not in (0, 1):
        return Audit((Issue(b"", "ignore-command-failed"),))
    fields = data.split(b"\0")[:-1]
    issues, seen = [], set()
    if (data and not data.endswith(b"\0")) or len(fields) % 4:
        issues.append(Issue(b"", "malformed-ignore-output"))
    for i in range(0, len(fields) - 3, 4):
        source, line, pattern, path = fields[i:i + 4]
        if path not in expected or path in seen:
            issues.append(Issue(path, "unexpected-or-duplicate-ignore-path"))
        seen.add(path)
        if not ((not source and not line and not pattern) or (source and re.fullmatch(rb"[1-9][0-9]*", line) and pattern)):
            issues.append(Issue(path, "malformed-ignore-record"))
        ignored = bool(pattern) and not pattern.startswith(b"!")
        should_allow = path in parents or (path in allowed if allowed is not None else allowed_path(path, scope, policy))
        if ignored == should_allow:
            issues.append(Issue(path, "effective-ignore-mismatch"))
    issues.extend(Issue(p, "missing-ignore-response") for p in expected if p not in seen)
    return Audit(tuple(issues))


def _policy_bytes(root_fd, path):
    path = path_bytes(path)
    parent, _, name = path.rpartition(b"/")
    fd = _directory_at(root_fd, parent)
    leaf = None
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise Blocked("unsafe-policy-file")
        # Nofollow prevents link traversal, but a regular-to-FIFO race can
        # still block open. Nonblocking open and fstat precede every read.
        leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        opened = os.fstat(leaf)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _observation(opened) != _observation(before):
            raise Blocked("changed-policy-file")
        chunks = []
        while True:
            chunk = os.read(leaf, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if _observation(os.fstat(leaf)) != _observation(before) or _observation(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _observation(before):
            raise Blocked("changed-policy-file")
        fresh = _directory_at(root_fd, parent)
        try:
            if _identity(os.fstat(fresh)) != _identity(os.fstat(fd)):
                raise Blocked("changed-policy-ancestor")
        finally:
            os.close(fresh)
        return b"".join(chunks)
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(fd)


def audit_policy_copies(root, expected, saved=b"data/native/root-ignore-policy"):
    if path_bytes(saved) != b"data/native/root-ignore-policy":
        return Audit((Issue(path_bytes(saved), "unsupported-policy-location"),))
    issues = []
    fd = None
    try:
        fd = _open_absolute_directory(root)
        for path in (b".gitignore", path_bytes(saved)):
            try:
                data = _policy_bytes(fd, path)
                if not data:
                    issues.append(Issue(path, "empty-policy"))
                elif data != expected:
                    issues.append(Issue(path, "inconsistent-policy"))
            except FileNotFoundError:
                issues.append(Issue(path, "missing-policy"))
            except (OSError, Blocked) as exc:
                issues.append(Issue(path, "unsafe-policy:" + str(exc)))
        fresh = _open_absolute_directory(root)
        try:
            if _identity(os.fstat(fresh)) != _identity(os.fstat(fd)):
                issues.append(Issue(b"", "changed-policy-root"))
        finally:
            os.close(fresh)
    except (OSError, Blocked):
        issues.append(Issue(b"", "unsafe-policy-root"))
    finally:
        if fd is not None:
            os.close(fd)
    return Audit(tuple(issues))


def _git(root, metadata, arguments, data=None, timeout=15):
    """Only two callers with fixed read-only commands; no general Git adapter."""
    root = os.fsencode(os.path.abspath(root))
    metadata = path_bytes(metadata)
    if metadata != b"data/native/repository.git":
        raise Blocked("unsupported-metadata-location")
    fd = _open_absolute_directory(root)
    meta_fd = None
    try:
        meta_fd = _directory_at(fd, metadata)
        # A routed gitfile/commondir or alternate index is not accepted.
        for name in (b"commondir", b"gitdir"):
            try:
                os.stat(name, dir_fd=meta_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Blocked("unsafe-metadata-routing")
        for name in (b"index", b"config", b"HEAD"):
            try:
                info = os.stat(name, dir_fd=meta_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Blocked("unsafe-metadata-file")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_ATTR_NOSYSTEM="1", LC_ALL="C")
        command = [b"git", b"--git-dir=" + root.rstrip(b"/") + b"/" + metadata, b"--work-tree=" + root,
                   b"-c", b"core.hooksPath=/dev/null", b"-c", b"core.fsmonitor=false",
                   b"-c", b"core.excludesFile=/dev/null", b"-c", b"core.attributesFile=/dev/null"] + arguments
        return subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=root, env=environment, timeout=timeout, check=False)
    finally:
        if meta_fd is not None:
            os.close(meta_fd)
        os.close(fd)


def audit_index(root, scope, policy=None, *, metadata=b"data/native/repository.git", timeout=15):
    try:
        result = _git(root, metadata, [b"ls-files", b"--stage", b"-z", b"--sparse"], timeout=timeout)
        return audit_index_output(result.stdout, scope, policy, returncode=result.returncode)
    except (OSError, Blocked, subprocess.TimeoutExpired):
        return Audit((Issue(b"", "index-command-unavailable"),))


def audit_ignore(root, paths, scope, policy=None, *, metadata=b"data/native/repository.git", timeout=15):
    paths = tuple(sorted({path_bytes(p) for p in paths}))
    if not paths:
        return Audit()
    try:
        result = _git(root, metadata, [b"check-ignore", b"--no-index", b"--stdin", b"-z", b"--verbose", b"--non-matching"],
                      b"\0".join(paths) + b"\0", timeout)
        return audit_ignore_output(result.stdout, paths, scope, policy, returncode=result.returncode)
    except (OSError, Blocked, subprocess.TimeoutExpired):
        return Audit((Issue(b"", "ignore-command-unavailable"),))
