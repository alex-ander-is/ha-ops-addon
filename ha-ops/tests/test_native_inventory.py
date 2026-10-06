"""Inert byte-path scope contracts; real Git and Linux topology fixtures."""
import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import native_inventory as n
from native_topology_helpers import git, METADATA


def write(root, path, content=b"fixture\n"):
    full = os.fsencode(root) + b"/" + os.fsencode(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as stream:
        stream.write(content)


def evidence(root):
    return (n.Mount(1, 0, b"1:1", b"/", os.fsencode(root), b"rw", b"fixture", b"fixture", b"rw"),)


def scan(root, selected=(), **kwargs):
    return n.inventory(root, selected, mounts=evidence(root), confirmed_mounts=(1,), **kwargs)


def fixture_git(root, scope, policy=None):
    snapshot = scan(root, scope, policy=policy)
    rules = n.render_ignore(snapshot, policy)
    write(root, b".gitignore", rules)
    write(root, b"data/native/root-ignore-policy", rules)
    git(root, "init", "--initial-branch=fixture")
    return snapshot, rules


def bytes_snapshot(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(base) / name
            if not path.is_symlink():
                result[os.fsencode(path.relative_to(root))] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).digest())
    return result


@pytest.mark.parametrize("path", [b"", b"/x", b"x/", b"x//y", b".", b"x/../y", b"./x", b"x\0y"])
def test_noncanonical_paths_rejected(path):
    with pytest.raises(n.Blocked):
        n.normalize_scope([path])


def test_component_selection_bytes_and_immutability(tmp_path):
    names = [b"foo", b"foobar", b"\xff", b"tab\tname", b"line\nname", b"cr\rname", b"back\\slash",
             b"#name", b"!name", b"star*", b"question?", b"[bracket]", b"trailing ", b"Case", b"case",
             "é".encode(), "e\u0301".encode()]
    assert len(n.normalize_scope(names)) == len(names)
    if sys.platform == "darwin":
        # APFS cannot create invalid UTF-8 and may fold case/Unicode. Linux
        # executes the full filesystem identity proof; byte algorithms run here.
        names = [p for p in names if p not in (b"\xff", b"Case", "é".encode(), "e\u0301".encode())]
    for name in names:
        write(tmp_path, b"homeassistant/" + name)
    os.link(os.fsencode(tmp_path) + b"/homeassistant/foo", os.fsencode(tmp_path) + b"/homeassistant/hardlink")
    selected = scan(tmp_path, [b"homeassistant/foo", b"homeassistant/foo", b"homeassistant/foo/nested"])
    assert selected.scope == (b"homeassistant/foo",)
    assert selected.managed_files == (b"homeassistant/foo",)
    broad = scan(tmp_path, [b"homeassistant"])
    assert not broad.blockers
    assert set(broad.managed_files) == {b"homeassistant/" + p for p in names + [b"hardlink"]}
    assert len({e.path for e in broad.entries}) == len(broad.entries)
    assert [e.links for e in broad.entries if e.path in (b"homeassistant/foo", b"homeassistant/hardlink")] == [2, 2]
    with pytest.raises(dataclasses.FrozenInstanceError):
        broad.entries = ()
    assert scan(tmp_path).managed_files == ()
    assert selected.managed_files == (b"homeassistant/foo",)
    with pytest.raises(n.Blocked, match="unrepresentable"):
        n.render_ignore(broad)


SECRET_PATHS = [b"homeassistant/secrets.yaml", b"homeassistant/nested/secrets.yaml", b"addon_configs/app/secrets.yaml",
                b"homeassistant/.storage/core.config_entries", b"homeassistant/.storage/auth",
                b"homeassistant/.storage/auth_provider.homeassistant", b"homeassistant/.cloud/file",
                b"homeassistant/.google.token", b"homeassistant/.ssh/key", b"homeassistant/.gnupg/key",
                b"homeassistant/.git-credentials", b"homeassistant/zigbee2mqtt/coordinator_backup.json",
                b"addon_configs/app/zigbee2mqtt/coordinator_backup_old.json"]


@pytest.mark.parametrize("path", SECRET_PATHS)
def test_secret_matrix_and_ancestor_selection(tmp_path, path):
    write(tmp_path, path, b"secret sentinel not to read")
    parent = path.split(b"/")[0]
    result = scan(tmp_path, [parent])
    assert not result.blockers
    assert path not in result.managed_files
    assert n.Policy().reason(path).startswith("known-")
    assert scan(tmp_path, [path]).blockers
    assert path not in n.render_ignore(result)


@pytest.mark.parametrize("path", [b"homeassistant/.storage/core.device_registry", b"homeassistant/.storage/core.entity_registry",
                                  b"homeassistant/.storage/unknown.integration", b"homeassistant/core.config_entries",
                                  b"homeassistant/notsecrets.yaml", b"homeassistant/secrets.yaml.bak",
                                  b"homeassistant/database.db", b"homeassistant/database.db-wal", b"homeassistant/a.log",
                                  b"homeassistant/.cache/a", b"homeassistant/node_modules/a", b"homeassistant/media/a"])
def test_whole_files_optional_runtime_and_adjacent_names(tmp_path, path):
    write(tmp_path, path)
    result = scan(tmp_path, [path])
    assert result.managed_files == (path,) and not result.blockers
    assert n.render_ignore(result)
    entry = next(e for e in result.entries if e.path == path)
    if path.endswith(b"unknown.integration"):
        assert entry.warnings == ("unclassified-storage-content",)


def test_sparse_large_regular_file_has_no_size_ceiling(tmp_path):
    write(tmp_path, b"homeassistant/large.db", b"")
    with open(tmp_path / "homeassistant/large.db", "wb") as stream:
        stream.truncate(10 * 1024 ** 3)
    result = scan(tmp_path, [b"homeassistant"])
    entry = next(e for e in result.entries if e.kind == "file")
    assert entry.size == 10 * 1024 ** 3 and entry.selected
    assert "large-file-no-admission-check" in entry.warnings


@pytest.mark.parametrize("path", [b"data/options.json", b"data/key", b"data/repo/file", b"backup/file", b".git/file", b"proc/file", b"config/settings.yaml"])
def test_reserved_roots_cannot_reopen(tmp_path, path):
    write(tmp_path, path)
    result = scan(tmp_path, [path])
    assert not result.managed_files and result.blockers
    assert n.Policy(extra_live_roots=(path.split(b"/")[0],)).reason(path)


def test_repository_only_exact_file_scaffolding_and_denies(tmp_path):
    allowed = b"data/native/repository-files/README.md"
    write(tmp_path, allowed)
    write(tmp_path, b"data/native/repository-files/key")
    write(tmp_path, b"data/options.json")
    policy = n.Policy(repository_files=(allowed,))
    result = scan(tmp_path, [allowed], policy=policy)
    assert result.managed_files == (allowed,) and not result.blockers
    assert b"options.json" not in n.render_ignore(result, policy)
    assert scan(tmp_path, [b"data"], policy=policy).blockers
    assert n.Policy(repository_files=(allowed,), extra_denies=(allowed,)).reason(allowed) == "configured-deny"
    assert n.Policy(repository_files=(allowed,), extra_denies=(b"data/native",)).digest != policy.digest
    denied = scan(tmp_path, [allowed], policy=n.Policy(repository_files=(allowed,), extra_denies=(b"data/native",)))
    assert allowed not in {e.path for e in denied.entries}
    for invalid in (b"data", b"data/native/repository.git/config", b"homeassistant/a"):
        with pytest.raises(n.Blocked):
            n.Policy(repository_files=(invalid,))


def test_nofollow_leaf_special_secret_and_ancestor_guards(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/a")
    write(tmp_path, b"homeassistant/secrets.yaml")
    (tmp_path / "homeassistant/link").symlink_to(tmp_path / "homeassistant/a")
    os.mkfifo(tmp_path / "homeassistant/fifo")
    original = os.open
    def guarded(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY, "inventory opened leaf contents"
        assert flags & os.O_NOFOLLOW
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    result = scan(tmp_path, [b"homeassistant"])
    assert not result.blockers
    assert {e.kind for e in result.entries} >= {"file", "directory", "symlink", "special"}
    assert result.managed_files == (b"homeassistant/a",)
    assert scan(tmp_path, [b"homeassistant/link"]).blockers
    linked_root = tmp_path.parent / (tmp_path.name + "-link")
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert scan(linked_root).blockers
        assert n.inventory(linked_root / "homeassistant", mounts=evidence(tmp_path), confirmed_mounts=(1,)).blockers
    finally:
        linked_root.unlink()


def test_missing_and_changed_membership_are_blockers(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/a")
    original = os.listdir
    calls = 0
    def changed(fd):
        nonlocal calls
        calls += 1
        names = original(fd)
        if "a" in names and calls > 2:
            return names + ["disappeared"]
        return names
    monkeypatch.setattr(os, "listdir", changed)
    assert scan(tmp_path, [b"homeassistant"]).blockers
    monkeypatch.setattr(os, "listdir", original)
    assert scan(tmp_path, [b"homeassistant/missing"]).blockers


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_changed_child_and_root_replacement_are_blockers(tmp_path, monkeypatch, replacement):
    write(tmp_path, b"homeassistant/a")
    original = os.open
    done = False
    def swapped(path, flags, *args, **kwargs):
        nonlocal done
        if path == b"homeassistant" and not done:
            done = True
            (tmp_path / "homeassistant").rename(tmp_path / "old")
            if replacement == "symlink":
                (tmp_path / "homeassistant").symlink_to(tmp_path / "old", target_is_directory=True)
            else:
                os.mkfifo(tmp_path / "homeassistant")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapped)
    assert scan(tmp_path, [b"homeassistant"]).blockers


@pytest.mark.parametrize("failure", ["unreadable", "disappeared", "root-swap"])
def test_namespace_failures_never_become_empty_success(tmp_path, monkeypatch, failure):
    write(tmp_path, b"homeassistant/a")
    original_stat, original_listdir = os.stat, os.listdir
    triggered = False
    def failed_stat(path, *args, **kwargs):
        if path == b"a" and kwargs.get("dir_fd") is not None and failure == "disappeared":
            raise FileNotFoundError("fixture disappearance")
        return original_stat(path, *args, **kwargs)
    def failed_listdir(fd):
        nonlocal triggered
        names = original_listdir(fd)
        if "a" in names and not triggered:
            triggered = True
            if failure == "unreadable":
                raise PermissionError("fixture denial")
            if failure == "root-swap":
                old = tmp_path.parent / (tmp_path.name + "-old")
                tmp_path.rename(old)
                tmp_path.mkdir()
        return names
    monkeypatch.setattr(os, "stat", failed_stat)
    monkeypatch.setattr(os, "listdir", failed_listdir)
    result = scan(tmp_path, [b"homeassistant"])
    assert result.blockers
    with pytest.raises(n.Blocked):
        n.render_ignore(result)


def test_policy_read_replacement_is_blocked(tmp_path, monkeypatch):
    _, rules = fixture_git(tmp_path, [])
    original = os.read
    done = False
    def changed(fd, amount):
        nonlocal done
        content = original(fd, amount)
        if not done:
            done = True
            saved = tmp_path / ".gitignore"
            saved.rename(tmp_path / "old-policy")
            saved.write_bytes(rules)
        return content
    monkeypatch.setattr(os, "read", changed)
    assert n.audit_policy_copies(tmp_path, rules).blockers


def test_deep_tree_no_recursion_and_descriptor_cleanup(tmp_path):
    deep = b"homeassistant/" + b"/".join([b"d"] * 150) + b"/file"
    write(tmp_path, deep)
    before = len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").exists() else None
    for _ in range(2):
        result = scan(tmp_path, [b"homeassistant"])
        assert result.managed_files == (deep,) and not result.blockers
    if before is not None:
        assert len(os.listdir("/proc/self/fd")) == before


def test_mount_parser_and_same_device_unknown_boundary(tmp_path):
    data = b"7 1 8:1 /a\\040b /m\\011x rw shared:1 - ext4 /dev/a rw\n8 7 8:1 /a\\134b /m\\012x rw - ext4 /dev/a rw\n"
    mounts = n.parse_mountinfo(data)
    assert mounts[0].root == b"/a b" and mounts[0].point == b"/m\tx"
    assert mounts[1].root == b"/a\\b" and mounts[1].point == b"/m\nx"
    write(tmp_path, b"homeassistant/file")
    child = dataclasses.replace(evidence(tmp_path)[0], id=2, parent=1, point=os.fsencode(tmp_path) + b"/homeassistant")
    result = n.inventory(tmp_path, [b"homeassistant"], mounts=evidence(tmp_path) + (child,), confirmed_mounts=(1,))
    assert result.blockers and b"homeassistant/file" not in {e.path for e in result.entries}
    confirmed = n.inventory(tmp_path, [b"homeassistant"], mounts=evidence(tmp_path) + (child,), confirmed_mounts=(1, 2))
    assert not confirmed.blockers and confirmed.managed_files == (b"homeassistant/file",)
    assert not confirmed.topology_verified
    conflict = n.inventory(tmp_path, mounts=evidence(tmp_path) + (dataclasses.replace(evidence(tmp_path)[0], id=3),), confirmed_mounts=(1, 3))
    assert conflict.blockers


@pytest.mark.parametrize("location", [b"homeassistant/mounted-file", b"data"])
@pytest.mark.parametrize("confirmation", ["unconfirmed", "confirmed", "stacked"])
def test_every_mount_kind_and_repository_scaffold_require_confirmation(tmp_path, location, confirmation):
    """Consolidates independent contract/failure probes and positive neighbors."""
    path = b"data/native/repository-files/README.md" if location == b"data" else location
    write(tmp_path, path)
    write(tmp_path, b"data/private-name")
    policy = n.Policy(repository_files=(path,)) if location == b"data" else n.Policy()
    base = evidence(tmp_path)[0]
    boundary = dataclasses.replace(base, id=2, parent=1, point=os.fsencode(tmp_path) + b"/" + location)
    mounts = (base, boundary)
    confirmed = (1, 2) if confirmation == "confirmed" else (1,)
    if confirmation == "stacked":
        mounts += (dataclasses.replace(boundary, id=3),)
        confirmed = (1, 2, 3)
    result = n.inventory(tmp_path, [path], policy=policy, mounts=mounts, confirmed_mounts=confirmed)
    if confirmation == "confirmed":
        assert not result.blockers and result.managed_files == (path,)
        assert n.render_ignore(result, policy)
    else:
        assert any(i.path == location and i.reason == "unconfirmed-mount-boundary" for i in result.blockers)
        assert not result.managed_files
        if location == b"data":
            assert not any(e.path.startswith(b"data/") for e in result.entries)
        with pytest.raises(n.Blocked):
            n.render_ignore(result, policy)


def test_present_alias_missing_canonical_target_is_a_blocker(tmp_path):
    write(tmp_path, b"homeassistant/a")
    write(tmp_path, b"config/hidden")
    result = scan(tmp_path, [b"homeassistant"])
    assert any(i.path == b"config" and i.reason == "unconfirmed-config-alias" for i in result.blockers)
    assert not any(e.path.startswith(b"config/") for e in result.entries)
    with pytest.raises(n.Blocked):
        n.render_ignore(result)


@pytest.mark.parametrize("swapped", [".gitignore", "data/native/root-ignore-policy"])
def test_policy_copy_regular_to_fifo_race_returns_blocker(tmp_path, swapped):
    """Tester integration reproduction, bounded even on the broken version."""
    root = tmp_path.resolve()
    write(root, b".gitignore", b"/*\n")
    write(root, b"data/native/root-ignore-policy", b"/*\n")
    script = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("native_inventory", sys.argv[1])
n = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = n
spec.loader.exec_module(n)
root, swapped = sys.argv[2:]
original = os.open
def swap(path, flags, *args, **kwargs):
    if path == os.fsencode(swapped.rsplit("/", 1)[-1]) and not flags & os.O_DIRECTORY:
        os.unlink(root + "/" + swapped)
        os.mkfifo(root + "/" + swapped)
    return original(path, flags, *args, **kwargs)
os.open = swap
result = n.audit_policy_copies(root, b"/*\n")
assert any(i.path == os.fsencode(swapped) and i.reason.startswith("unsafe-policy:") for i in result.blockers), result
'''
    result = subprocess.run([sys.executable, "-c", script, n.__file__, str(root), swapped],
                            capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("data", [b"", b"1 0 bad / / rw - ext4 x rw\n", b"1 0 1:1 / /a\\999 rw - ext4 x rw\n", b"1 0 1:1 / / rw - ext4 x\n",
                                  b"1 0 1:1 / /a//file rw - ext4 x rw\n", b"1 0 1:1 /x/../a /a rw - ext4 x rw\n"])
def test_malformed_mount_evidence_blocks(data):
    with pytest.raises(n.Blocked):
        n.parse_mountinfo(data)


@pytest.mark.parametrize("point", [b"relative", b"/homeassistant//file", b"/homeassistant/../file"])
def test_malformed_injected_mount_evidence_cannot_hide_a_boundary(tmp_path, point):
    write(tmp_path, b"homeassistant/file")
    base = evidence(tmp_path)[0]
    invalid = dataclasses.replace(base, id=2, point=point)
    result = n.inventory(tmp_path, [b"homeassistant"], mounts=(base, invalid), confirmed_mounts=(1, 2))
    assert result.blockers and not result.managed_files


def test_alias_mismatch_not_inode_deduplication(tmp_path):
    write(tmp_path, b"config/a")
    write(tmp_path, b"addon_configs/07ef30c0_ha_ops/a")
    result = scan(tmp_path, [b"addon_configs"])
    assert any(i.reason == "unconfirmed-config-alias" for i in result.blockers)
    alias = next(e for e in result.entries if e.path == b"config")
    assert alias.alias_target == b"addon_configs/07ef30c0_ha_ops"
    assert b"config/a" not in {e.path for e in result.entries}


def test_alias_confirmed_using_filesystem_mount_root_and_identity(tmp_path, monkeypatch):
    write(tmp_path, b"config/a")
    write(tmp_path, b"addon_configs/07ef30c0_ha_ops/a")
    original = os.stat
    canonical = original(tmp_path / "addon_configs/07ef30c0_ha_ops")
    def same_identity(path, *args, **kwargs):
        if path == b"config" and kwargs.get("dir_fd") is not None:
            return canonical
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", same_identity)
    base = evidence(tmp_path)[0]
    addon = dataclasses.replace(base, id=2, point=os.fsencode(tmp_path) + b"/addon_configs", root=b"/apps")
    alias = dataclasses.replace(base, id=3, point=os.fsencode(tmp_path) + b"/config", root=b"/apps/07ef30c0_ha_ops")
    result = n.inventory(tmp_path, [b"addon_configs"], mounts=(base, addon, alias), confirmed_mounts=(1, 2, 3))
    assert not result.blockers and not result.topology_verified
    assert result.managed_files == (b"addon_configs/07ef30c0_ha_ops/a",)
    denied = n.inventory(tmp_path, [b"config"], mounts=(base, addon, alias), confirmed_mounts=(1, 2, 3))
    assert denied.blockers


@pytest.mark.parametrize("topology", ["unconfirmed-ancestor", "unconfirmed-self", "stacked-ancestor", "stacked-self", "confirmed-child-unknown-ancestor", "confirmed"])
def test_alias_probe_fences_each_canonical_component_before_open(tmp_path, monkeypatch, topology):
    """Contract round-1 no-open guards, with a confirmed positive counterpart."""
    (tmp_path / "config").mkdir()
    (tmp_path / "addon_configs/07ef30c0_ha_ops").mkdir(parents=True)
    base = evidence(tmp_path)[0]
    addon = dataclasses.replace(base, id=2, parent=1, point=os.fsencode(tmp_path) + b"/addon_configs", root=b"/apps")
    alias = dataclasses.replace(base, id=4, parent=1, point=os.fsencode(tmp_path) + b"/config", root=b"/apps/07ef30c0_ha_ops")
    mounts = (base, addon, alias)
    confirmed = (1, 4) if topology in ("unconfirmed-ancestor", "confirmed-child-unknown-ancestor") else (1, 2, 4)
    relative = b"addon_configs"
    if topology in ("unconfirmed-self", "stacked-self", "confirmed-child-unknown-ancestor"):
        self_path = b"addon_configs/07ef30c0_ha_ops"
        if topology != "confirmed-child-unknown-ancestor":
            relative = self_path
        self_mount = dataclasses.replace(addon, id=3, parent=2, point=os.fsencode(tmp_path) + b"/" + self_path, root=b"/apps/07ef30c0_ha_ops")
        mounts += (self_mount,)
        if topology != "unconfirmed-self":
            confirmed += (3,)
        if topology == "stacked-self":
            mounts += (dataclasses.replace(self_mount, id=5),)
            confirmed += (5,)
    if topology == "stacked-ancestor":
        mounts += (dataclasses.replace(addon, id=5),)
        confirmed += (5,)
    opened = []
    original_open, original_stat = os.open, os.stat
    canonical = original_stat(tmp_path / "addon_configs/07ef30c0_ha_ops")
    def same_alias(path, *args, **kwargs):
        if path == b"config" and kwargs.get("dir_fd") is not None:
            return canonical
        return original_stat(path, *args, **kwargs)
    def observed(path, flags, *args, **kwargs):
        if path in (b"addon_configs", b"07ef30c0_ha_ops") and kwargs.get("dir_fd") is not None:
            opened.append(path)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(n.os, "open", observed)
    monkeypatch.setattr(n.os, "stat", same_alias)
    result = n.inventory(tmp_path, mounts=mounts, confirmed_mounts=confirmed)
    if topology == "confirmed":
        assert not result.blockers and b"07ef30c0_ha_ops" in opened
    else:
        assert any(i.path == relative and i.reason == "unconfirmed-mount-boundary" for i in result.blockers)
        assert not any(e.path.startswith(relative + b"/") for e in result.entries)
        assert b"07ef30c0_ha_ops" not in opened
        if topology not in ("unconfirmed-self", "stacked-self"):
            assert b"addon_configs" not in opened


def test_inventory_acquisitions_share_component_guard_in_scan_and_recheck(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/nested/file")
    original = n._directory_at
    acquisitions, components = [], []
    def guarded(fd, path, *, before_open=None):
        assert before_open is not None
        acquisitions.append(path)
        def observe(component):
            components.append(component)
            before_open(component)
        return original(fd, path, before_open=observe)
    monkeypatch.setattr(n, "_directory_at", guarded)
    result = scan(tmp_path, [b"homeassistant"])
    assert not result.blockers
    for path in (b"", b"homeassistant", b"homeassistant/nested"):
        assert acquisitions.count(path) == 2  # scan and final namespace recheck
    assert components.count(b"homeassistant") >= 4
    assert components.count(b"homeassistant/nested") == 2


@pytest.mark.skipif(not Path("/proc/self/mountinfo").exists() or not Path("/workspace/output").exists(), reason="real Linux runner bind proof is a separate mandatory invocation")
def test_real_linux_mountinfo_same_device_bind_boundaries():
    mounts = n.linux_mounts()
    left = next(m for m in mounts if m.point == b"/workspace/input")
    right = next(m for m in mounts if m.point == b"/workspace/output")
    base = max((m for m in mounts if m.point == b"/" or b"/workspace" == m.point), key=lambda m: len(m.point))
    assert left.id != right.id and left.device == right.device
    policy = n.Policy(extra_live_roots=(b"output",))
    unconfirmed = n.inventory("/workspace", mounts=mounts, confirmed_mounts=(base.id,), policy=policy, topology_verified=True)
    assert any(i.path == b"output" and i.reason == "unconfirmed-mount-boundary" for i in unconfirmed.blockers)
    with tempfile.TemporaryDirectory(dir="/workspace/output", prefix="native-inventory-") as root:
        write(root, b"file")
        selected = b"output/" + os.fsencode(Path(root).name)
        result = n.inventory("/workspace", [selected], mounts=mounts, confirmed_mounts=(base.id, left.id, right.id), policy=policy, topology_verified=True)
        assert not result.blockers and result.topology_verified
        assert selected + b"/file" in result.managed_files
        assert all(not e.path.startswith(b"input/") for e in result.entries)
    print("real mountinfo proof: input/output IDs", left.id, right.id, "same device", left.device.decode())


def test_real_git_literal_whitelist_empty_scope_and_no_writes(tmp_path):
    names = [b"space name", b"trailing ", b"#hash", b"!bang", b"literal*", b"literal?", b"[literal]", b"back\\slash", b"tab\tname", b"\xff"]
    if sys.platform == "darwin":
        names.remove(b"\xff")
    for name in names:
        write(tmp_path, b"homeassistant/nested/" + name)
    write(tmp_path, b"homeassistant/nested/cousin")
    write(tmp_path, b"homeassistant/secrets.yaml")
    scope = tuple(b"homeassistant/nested/" + name for name in names)
    result, rules = fixture_git(tmp_path, scope)
    git(tmp_path, "add", "-A")
    paths = [e.path for e in scan(tmp_path).entries]
    before = bytes_snapshot(tmp_path)
    assert n.audit_index(tmp_path, scope).clean
    assert n.audit_ignore(tmp_path, paths, result).clean
    assert n.audit_policy_copies(tmp_path, rules).clean
    assert scan(tmp_path).managed_files == ()
    assert n.render_ignore(scan(tmp_path)) == b"/*\n!/.gitignore\n"
    assert n.render_ignore(scan(tmp_path, scope)) == rules
    assert bytes_snapshot(tmp_path) == before
    assert result.managed_files == tuple(sorted(scope))


@pytest.mark.parametrize("override,expected", [(b"!cousin\n", b"homeassistant/nested/cousin"), (b"selected\n", b"homeassistant/nested/selected")])
def test_nested_ignore_override_both_directions_and_tracked_paths(tmp_path, override, expected):
    write(tmp_path, b"homeassistant/nested/selected")
    write(tmp_path, b"homeassistant/nested/cousin")
    write(tmp_path, b"homeassistant/nested/.gitignore", override)
    scope = (b"homeassistant/nested/selected", b"homeassistant/nested/.gitignore")
    _, rules = fixture_git(tmp_path, scope)
    git(tmp_path, "add", "-f", "--", "homeassistant/nested/selected")
    assert n.audit_index(tmp_path, scope).clean
    assert n.audit_policy_copies(tmp_path, rules).clean
    report = n.audit_ignore(tmp_path, [expected], scope)
    assert any(i.path == expected and i.reason == "effective-ignore-mismatch" for i in report.blockers)


def test_complete_index_force_added_bytes_and_allowed_deletion(tmp_path):
    selected = b"homeassistant/selected"
    write(tmp_path, selected)
    prohibited = [b"data/line\nname", b"homeassistant/\xff", b"homeassistant/.storage/core.config_entries"]
    if sys.platform == "darwin":
        prohibited[1] = b"homeassistant/unselected"
    for path in prohibited:
        write(tmp_path, path)
    fixture_git(tmp_path, [selected])
    git(tmp_path, "add", "--", os.fsdecode(selected))
    git(tmp_path, "add", "-f", "--", *(os.fsdecode(p) for p in prohibited))
    (tmp_path / os.fsdecode(selected)).unlink()
    before = bytes_snapshot(tmp_path)
    report = n.audit_index(tmp_path, [selected])
    assert {i.path for i in report.blockers} == set(prohibited)
    assert n.audit_ignore(tmp_path, prohibited, [selected]).clean
    assert bytes_snapshot(tmp_path) == before


@pytest.mark.parametrize("mode,stage,reason", [(b"120000", b"0", "unsupported-index-mode"), (b"160000", b"0", "unsupported-index-mode"),
                                               (b"040000", b"0", "unsupported-index-mode"), (b"100644", b"2", "unmerged-index-stage")])
def test_index_modes_stages_and_exact_tab_filename(mode, stage, reason):
    path = b"homeassistant/tab\tline\n\xff"
    record = mode + b" " + b"a" * 40 + b" " + stage + b"\t" + path + b"\0"
    report = n.audit_index_output(record, [b"homeassistant"])
    assert any(i.path == path and i.reason == reason for i in report.blockers)
    assert n.audit_index_output(record + record, [b"homeassistant"]).blockers


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_real_git_unsupported_tracked_modes(tmp_path, mode):
    write(tmp_path, b"homeassistant/a")
    fixture_git(tmp_path, [b"homeassistant"])
    oid = git(tmp_path, "hash-object", "-w", "homeassistant/a").strip().decode()
    git(tmp_path, "update-index", "--add", "--cacheinfo", mode + "," + oid + ",homeassistant/a")
    report = n.audit_index(tmp_path, [b"homeassistant"])
    assert any(i.path == b"homeassistant/a" and i.reason == "unsupported-index-mode" for i in report.blockers)


def test_real_git_unmerged_stages(tmp_path):
    write(tmp_path, b"homeassistant/a")
    fixture_git(tmp_path, [b"homeassistant"])
    oid = git(tmp_path, "hash-object", "-w", "homeassistant/a").strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    data = b"100644 " + oid + b" 1\thomeassistant/a\n100644 " + oid + b" 2\thomeassistant/a\n"
    subprocess.run(["git", "--git-dir=" + str(tmp_path / METADATA), "--work-tree=" + str(tmp_path),
                    "-c", "core.hooksPath=/dev/null", "update-index", "--index-info"],
                   cwd=tmp_path, env=env, input=data, check=True, capture_output=True, timeout=15)
    report = n.audit_index(tmp_path, [b"homeassistant"])
    assert len([i for i in report.blockers if i.reason == "unmerged-index-stage"]) == 2


@pytest.mark.parametrize("data", [b"x", b"bad\0", b"100644 bad 0\thomeassistant/a\0", b"100644 " + b"a" * 40 + b" 0\t../a\0"])
def test_malformed_index_output(data):
    assert not n.audit_index_output(data, [b"homeassistant"]).clean


def test_effective_ignore_parser_nonmatch_negation_and_all_violations():
    paths = [b"homeassistant/a", b"data/b"]
    proper = b"\0\0\0homeassistant/a\0" + b".gitignore\0" + b"1\0/*\0data/b\0"
    assert n.audit_ignore_output(proper, paths, [b"homeassistant"], returncode=0).clean
    nonmatch = b"\0\0\0homeassistant/a\0"
    assert n.audit_ignore_output(nonmatch, paths[:1], [b"homeassistant"], returncode=1).clean
    negation = b".gitignore\0" + b"1\0!data/b\0data/b\0"
    assert n.audit_ignore_output(negation, paths[1:], []).blockers
    for bad in (proper[:-1], proper + proper, b"", b"x\0", proper.replace(b"1\0", b"zero\0")):
        assert n.audit_ignore_output(bad, paths, [b"homeassistant"]).blockers
    assert n.audit_ignore_output(proper, paths, [b"homeassistant"], returncode=128).blockers
    assert n.audit_index_output(b"", (), returncode=128).blockers


@pytest.mark.parametrize("damage", ["missing", "empty", "altered", "symlink", "hardlink", "ancestor-symlink"])
def test_policy_copies_must_be_exact_single_link_nofollow(tmp_path, damage):
    _, rules = fixture_git(tmp_path, [])
    saved = tmp_path / "data/native/root-ignore-policy"
    if damage == "missing":
        saved.unlink()
    elif damage == "empty":
        saved.write_bytes(b"")
    elif damage == "altered":
        saved.write_bytes(rules + b"!data/key\n")
    elif damage == "symlink":
        saved.unlink()
        saved.symlink_to(tmp_path / ".gitignore")
    elif damage == "hardlink":
        os.link(saved, tmp_path / "copy")
    else:
        (tmp_path / "data/native").rename(tmp_path / "moved")
        (tmp_path / "data/native").symlink_to(tmp_path / "moved", target_is_directory=True)
    before = bytes_snapshot(tmp_path)
    assert n.audit_policy_copies(tmp_path, rules).blockers
    assert bytes_snapshot(tmp_path) == before


def test_git_environment_excludes_and_fsmonitor_are_isolated(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/a")
    scope = [b"homeassistant/a"]
    fixture_git(tmp_path, scope)
    git(tmp_path, "add", "--", "homeassistant/a")
    hook = tmp_path / "monitor.sh"
    hook.write_text("#!/bin/sh\ntouch '" + str(tmp_path / "ran") + "'\n")
    hook.chmod(0o755)
    external = tmp_path / "excludes"
    external.write_text("homeassistant/a\n")
    git(tmp_path, "config", "core.fsmonitor", str(hook))
    git(tmp_path, "config", "core.excludesFile", str(external))
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(key, "/hostile")
    before = bytes_snapshot(tmp_path)
    assert n.audit_index(tmp_path, scope).clean
    assert n.audit_ignore(tmp_path, scope, scope).clean
    assert not (tmp_path / "ran").exists() and before == bytes_snapshot(tmp_path)
    write(tmp_path, b"data/native/repository.git/commondir", b"/hostile\n")
    assert n.audit_index(tmp_path, scope).blockers


def test_git_failures_timeouts_and_metadata_links_block(tmp_path, monkeypatch):
    fixture_git(tmp_path, [])
    assert n.audit_index(tmp_path, [], metadata=b"homeassistant/.ssh").blockers
    assert n.audit_policy_copies(tmp_path, b"x", saved=b"homeassistant/secrets.yaml").blockers
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 1)
    monkeypatch.setattr(n.subprocess, "run", timeout)
    assert n.audit_index(tmp_path, []).blockers
    assert n.audit_ignore(tmp_path, [b"data/a"], []).blockers
    monkeypatch.undo()
    metadata = tmp_path / METADATA
    metadata.rename(tmp_path / "moved")
    metadata.symlink_to(tmp_path / "moved", target_is_directory=True)
    assert n.audit_index(tmp_path, []).blockers


@pytest.mark.parametrize("leaf", ["index", "config", "HEAD"])
@pytest.mark.parametrize("replacement", ["before-validation", "before-git"])
def test_special_metadata_leaves_and_races_are_rejected_or_bounded(tmp_path, monkeypatch, leaf, replacement):
    write(tmp_path, b"homeassistant/a")
    fixture_git(tmp_path, [b"homeassistant/a"])
    git(tmp_path, "add", "--", "homeassistant/a")
    target = tmp_path / METADATA / leaf
    original = subprocess.run
    called = False
    def replace():
        target.unlink()
        os.mkfifo(target)
    def before_git(*args, **kwargs):
        nonlocal called
        called = True
        if replacement == "before-git":
            replace()
        assert kwargs["timeout"] == 0.2
        return original(*args, **kwargs)
    if replacement == "before-validation":
        replace()
    monkeypatch.setattr(n.subprocess, "run", before_git)
    assert n.audit_index(tmp_path, [b"homeassistant/a"], timeout=0.2).blockers
    assert called == (replacement == "before-git")


def test_no_production_import_or_legacy_policy_dependency():
    app = Path(__file__).resolve().parents[1] / "app"
    assert "EXPORT_EXCLUDES" not in (app / "native_inventory.py").read_text()
    for file in app.glob("*.py"):
        if file.name != "native_inventory.py":
            assert "import native_inventory" not in file.read_text()
