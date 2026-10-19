import ast
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


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("selection", [(), (b"homeassistant/automations.yaml",)])
def test_unselected_real_wal_append_is_diagnostic(tmp_path, monkeypatch, phase, selection):
    write(tmp_path, b"homeassistant/automations.yaml")
    wal = b"homeassistant/home-assistant_v2.db-wal"
    write(tmp_path, wal)
    original = os.stat
    calls = 0
    def changing(path, *args, **kwargs):
        nonlocal calls
        if path == b"home-assistant_v2.db-wal" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == phase:
                with open(os.fsencode(tmp_path) + b"/" + wal, "ab") as stream:
                    stream.write(b"real WAL append\n")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", changing)
    snapshot = scan(tmp_path, selection)
    assert not snapshot.blockers
    assert snapshot.managed_files == tuple(selection)
    assert snapshot.diagnostics
    assert n.render_ignore(snapshot).startswith(b"/*\n")


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
    assert set(broad.managed_files) == {b"homeassistant/" + p for p in names + [b"hardlink"]
                                       if b"\n" not in p and b"\r" not in p}
    assert len({e.path for e in broad.entries}) == len(broad.entries)
    assert [e.links for e in broad.entries if e.path in (b"homeassistant/foo", b"homeassistant/hardlink")] == [2, 2]
    with pytest.raises(dataclasses.FrozenInstanceError):
        broad.entries = ()
    assert scan(tmp_path).managed_files == ()
    assert selected.managed_files == (b"homeassistant/foo",)
    assert n.render_ignore(broad)
    for entry in broad.entries:
        assert "\n" not in entry.display_path and "\r" not in entry.display_path


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


def test_present_alias_missing_canonical_target_is_diagnostic(tmp_path):
    write(tmp_path, b"homeassistant/a")
    write(tmp_path, b"config/hidden")
    result = scan(tmp_path, [b"homeassistant"])
    assert not result.blockers
    assert any(i.path == b"config" and i.reason == "unconfirmed-config-alias" for i in result.diagnostics)
    assert not any(e.path.startswith(b"config/") for e in result.entries)
    assert n.render_ignore(result)


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
    assert not result.blockers
    assert any(i.reason == "unconfirmed-config-alias" for i in result.diagnostics)
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
@pytest.mark.parametrize("selected", [(), (b"homeassistant/a",), (b"addon_configs/07ef30c0_ha_ops",)])
def test_alias_probe_fences_each_canonical_component_before_open(tmp_path, monkeypatch, topology, selected):
    """Contract round-1 no-open guards, with a confirmed positive counterpart."""
    (tmp_path / "config").mkdir()
    write(tmp_path, b"homeassistant/a")
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
    result = n.inventory(tmp_path, selected, mounts=mounts, confirmed_mounts=confirmed)
    if topology == "confirmed":
        assert not result.blockers and b"07ef30c0_ha_ops" in opened
    else:
        assert bool(result.blockers) == bool(selected and selected[0].startswith(b"addon_configs/"))
        assert any(i.path == relative and i.reason == "unconfirmed-mount-boundary" for i in result.diagnostics)
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
        assert acquisitions.count(path) >= 2  # scan, final recheck and selected proof
    assert components.count(b"homeassistant") >= 4
    assert components.count(b"homeassistant/nested") >= 2


@pytest.mark.skipif(not Path("/proc/self/mountinfo").exists() or not Path("/workspace/output").exists(), reason="real Linux runner bind proof is a separate mandatory invocation")
def test_real_linux_mountinfo_same_device_bind_boundaries():
    mounts = n.linux_mounts()
    left = next(m for m in mounts if m.point == b"/workspace/input")
    right = next(m for m in mounts if m.point == b"/workspace/output")
    base = max((m for m in mounts if m.point == b"/" or b"/workspace" == m.point), key=lambda m: len(m.point))
    assert left.id != right.id and left.device == right.device
    policy = n.Policy(extra_live_roots=(b"output",))
    unconfirmed = n.inventory("/workspace", mounts=mounts, confirmed_mounts=(base.id,), policy=policy, topology_verified=True)
    assert not unconfirmed.blockers
    assert any(i.path == b"output" and i.reason == "unconfirmed-mount-boundary" for i in unconfirmed.diagnostics)
    with tempfile.TemporaryDirectory(dir="/workspace/output", prefix="native-inventory-") as root:
        write(root, b"file")
        selected = b"output/" + os.fsencode(Path(root).name)
        blocked = n.inventory("/workspace", [selected], mounts=mounts, confirmed_mounts=(base.id,),
                              policy=policy, topology_verified=True)
        assert blocked.blockers and not blocked.managed_files
        result = n.inventory("/workspace", [selected], mounts=mounts, confirmed_mounts=(base.id, left.id, right.id), policy=policy, topology_verified=True)
        assert not result.blockers and result.topology_verified
        assert selected + b"/file" in result.managed_files
        assert all(not e.path.startswith(b"input/") for e in result.entries)
    print("real mountinfo proof: input/output IDs", left.id, right.id, "same device", left.device.decode())


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("folder", [False, True])
@pytest.mark.parametrize("change", ["append", "add", "remove", "secret-add", "secret-remove", "secret-append", "line-add"])
def test_scope_local_real_membership_and_content(tmp_path, monkeypatch, phase, folder, change):
    write(tmp_path, b"homeassistant/a")
    write(tmp_path, b"homeassistant/sibling")
    write(tmp_path, b"homeassistant/secrets.yaml")
    original_list, original_stat = os.listdir, os.stat
    counts = {"list": 0, "stat": 0}
    def mutate():
        if change == "append":
            with open(tmp_path / "homeassistant/sibling", "ab") as stream:
                stream.write(b"changed")
        elif change == "add":
            write(tmp_path, b"homeassistant/new")
        elif change == "remove":
            (tmp_path / "homeassistant/sibling").unlink()
        elif change == "secret-add":
            write(tmp_path, b"homeassistant/.google.token")
        elif change == "secret-remove":
            (tmp_path / "homeassistant/secrets.yaml").unlink()
        elif change == "secret-append":
            with open(tmp_path / "homeassistant/secrets.yaml", "ab") as stream:
                stream.write(b"changed")
        else:
            write(tmp_path, b"homeassistant/line\nname")
    def listed(fd):
        names = original_list(fd)
        if "a" in names:
            counts["list"] += 1
            if change not in ("append", "secret-append") and counts["list"] == phase:
                mutate()
                names = original_list(fd)
        return names
    def stated(path, *args, **kwargs):
        trigger = b"secrets.yaml" if change == "secret-append" else b"sibling"
        if path == trigger and kwargs.get("dir_fd") is not None:
            counts["stat"] += 1
            if change in ("append", "secret-append") and counts["stat"] == phase:
                mutate()
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "listdir", listed)
    monkeypatch.setattr(os, "stat", stated)
    result = scan(tmp_path, [b"homeassistant" if folder else b"homeassistant/a"])
    assert bool(result.blockers) == (folder and change in ("append", "add", "remove"))
    assert result.diagnostics
    assert b"homeassistant/secrets.yaml" not in result.managed_files
    assert b"homeassistant/line\nname" not in result.managed_files
    if not result.blockers:
        rules = n.render_ignore(result)
        if not folder:
            write(tmp_path, b".gitignore", rules)
            write(tmp_path, b"data/native/root-ignore-policy", rules)
            git(tmp_path, "init", "--initial-branch=fixture")
            write(tmp_path, b"homeassistant/fresh")
            assert n.audit_ignore(tmp_path, [b"homeassistant/a", b"homeassistant/fresh",
                                             b"homeassistant/secrets.yaml"], result).clean


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("effect", ["size", "mtime", "links", "identity", "hardlink", "symlink", "fifo"])
def test_selected_observation_real_changes_block(tmp_path, monkeypatch, phase, effect):
    write(tmp_path, b"homeassistant/a")
    leaf = tmp_path / "homeassistant/a"
    if effect == "hardlink":
        os.link(leaf, tmp_path / "homeassistant/z")
    original = os.stat
    calls = 0
    def stated(path, *args, **kwargs):
        nonlocal calls
        if path == b"a" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == phase:
                if effect in ("size", "hardlink"):
                    with open(tmp_path / ("homeassistant/z" if effect == "hardlink" else "homeassistant/a"), "ab") as stream:
                        stream.write(b"changed")
                elif effect == "mtime":
                    os.utime(leaf, ns=(1, 1))
                elif effect == "links":
                    os.link(leaf, tmp_path / "homeassistant/z")
                else:
                    leaf.unlink()
                    if effect == "identity":
                        write(tmp_path, b"homeassistant/a")
                    elif effect == "symlink":
                        leaf.symlink_to("missing")
                    else:
                        os.mkfifo(leaf)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stated)
    result = scan(tmp_path, [b"homeassistant/a"])
    assert result.blockers and result.diagnostics
    with pytest.raises(n.Blocked):
        n.render_ignore(result)


@pytest.mark.parametrize("failure", ["before", "after", "listing"])
def test_exact_file_proof_independent_of_sibling_failure(tmp_path, monkeypatch, failure):
    for name in (b"a", b"m", b"z"):
        write(tmp_path, b"homeassistant/" + name)
    original_stat, original_list = os.stat, os.listdir
    def stated(path, *args, **kwargs):
        if path == (b"a" if failure == "before" else b"z") and failure != "listing" and kwargs.get("dir_fd") is not None:
            raise PermissionError("unrelated fixture stat")
        return original_stat(path, *args, **kwargs)
    def listed(fd):
        names = original_list(fd)
        if "m" in names and failure == "listing":
            raise PermissionError("unrelated fixture listing")
        return names
    monkeypatch.setattr(os, "stat", stated)
    monkeypatch.setattr(os, "listdir", listed)
    result = scan(tmp_path, [b"homeassistant/m"])
    assert not result.blockers and result.managed_files == (b"homeassistant/m",)
    assert result.diagnostics and n.render_ignore(result)


@pytest.mark.parametrize("name", [b"line\nname", b"cr\rname", b"dir\nname/child", b"dir\rname/child"])
def test_line_break_names_visible_excluded_and_git_audited(tmp_path, name):
    path = b"homeassistant/" + name
    write(tmp_path, path)
    write(tmp_path, b"homeassistant/a")
    result, rules = fixture_git(tmp_path, [b"homeassistant"])
    assert result.managed_files == (b"homeassistant/a",)
    rowpath = b"homeassistant/" + name.split(b"/")[0]
    row = next(e for e in result.entries if e.path == rowpath)
    assert row.reason == "unsupported-line-break-name" and not row.selected
    assert "\n" not in row.display_path and "\r" not in row.display_path
    assert not any(e.path.startswith(rowpath + b"/") for e in result.entries)
    explicit = scan(tmp_path, [path])
    assert explicit.blockers and not explicit.managed_files
    assert all("\n" not in i.display_path and "\r" not in i.display_path for i in explicit.diagnostics)
    assert n.audit_ignore(tmp_path, [b"homeassistant/a", path], result).clean
    git(tmp_path, "add", "-f", "--", path)
    assert any(i.path == path and i.reason == "unsupported-line-break-name"
               for i in n.audit_index(tmp_path, [b"homeassistant"]).blockers)
    with pytest.raises(n.Blocked, match="unrepresentable"):
        n._literal(path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.display_path = "changed"


def test_diagnostics_are_complete_immutable_and_compatibility_default(tmp_path):
    old = n.Inventory((), (), (), False)
    assert old.diagnostics == ()
    invalid = n.inventory(tmp_path, mounts=(), confirmed_mounts=())
    assert invalid.blockers and invalid.diagnostics == invalid.blockers
    with pytest.raises(dataclasses.FrozenInstanceError):
        invalid.diagnostics = ()


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("selection", [(b"homeassistant/home-assistant_v2.db-wal",), (b"homeassistant",)])
def test_runtime_warning_does_not_waive_selected_wal_stability(tmp_path, monkeypatch, phase, selection):
    path = b"homeassistant/home-assistant_v2.db-wal"
    write(tmp_path, path)
    original = os.stat
    calls = 0
    def stated(name, *args, **kwargs):
        nonlocal calls
        if name == b"home-assistant_v2.db-wal" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == phase:
                with open(os.fsencode(tmp_path) + b"/" + path, "ab") as stream:
                    stream.write(b"WAL append")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stated)
    assert scan(tmp_path, selection).blockers


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("denied", [False, True])
def test_selected_empty_directory_membership_is_proven(tmp_path, monkeypatch, phase, denied):
    folder = tmp_path / "homeassistant"
    folder.mkdir()
    identity = folder.stat().st_ino
    original = os.listdir
    calls = 0
    def listed(fd):
        nonlocal calls
        if os.fstat(fd).st_ino == identity:
            calls += 1
            if calls == phase:
                write(tmp_path, b"homeassistant/secrets.yaml" if denied else b"homeassistant/new")
        return original(fd)
    monkeypatch.setattr(os, "listdir", listed)
    result = scan(tmp_path, [b"homeassistant"])
    assert bool(result.blockers) == (not denied)
    if denied:
        assert not result.managed_files and n.render_ignore(result)


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("replacement", ["directory", "symlink", "fifo"])
def test_exact_file_required_parent_identity_replacement_blocks(tmp_path, monkeypatch, phase, replacement):
    write(tmp_path, b"homeassistant/a")
    original = os.listdir
    calls = 0
    done = False
    def listed(fd):
        nonlocal calls, done
        names = original(fd)
        if "a" in names:
            calls += 1
            if calls == phase and not done:
                done = True
                (tmp_path / "homeassistant").rename(tmp_path / "old-parent")
                if replacement == "directory":
                    write(tmp_path, b"homeassistant/a")
                elif replacement == "symlink":
                    (tmp_path / "homeassistant").symlink_to("old-parent", target_is_directory=True)
                else:
                    os.mkfifo(tmp_path / "homeassistant")
        return names
    monkeypatch.setattr(os, "listdir", listed)
    assert scan(tmp_path, [b"homeassistant/a"]).blockers


def test_fallback_does_not_reset_established_selected_observation(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/a")
    original = os.listdir
    calls = 0
    def listed(fd):
        nonlocal calls
        names = original(fd)
        if "a" in names:
            calls += 1
            if calls == 2:
                write(tmp_path, b"homeassistant/a", b"changed selected contents")
                raise PermissionError("diagnostic listing failure")
        return names
    monkeypatch.setattr(os, "listdir", listed)
    assert scan(tmp_path, [b"homeassistant/a"]).blockers


def test_unselected_root_namespace_replacement_blocks(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    original = os.listdir
    changed = False
    def listed(fd):
        nonlocal changed
        if not changed:
            changed = True
            root.rename(tmp_path / "old-root")
            root.mkdir()
        return original(fd)
    monkeypatch.setattr(os, "listdir", listed)
    result = scan(root)
    assert result.blockers and set(result.blockers).issubset(set(result.diagnostics))


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("name", [b"line\nname", b"cr\rname"])
@pytest.mark.parametrize("change", ["append", "remove"])
def test_excluded_line_break_churn_does_not_block_folder(tmp_path, monkeypatch, phase, name, change):
    write(tmp_path, b"homeassistant/a")
    write(tmp_path, b"homeassistant/" + name)
    original = os.stat
    calls = 0
    def stated(path, *args, **kwargs):
        nonlocal calls
        if path == name and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == phase:
                leaf = os.fsencode(tmp_path) + b"/homeassistant/" + name
                if change == "remove":
                    os.unlink(leaf)
                else:
                    with open(leaf, "ab") as stream:
                        stream.write(b"changed")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stated)
    result = scan(tmp_path, [b"homeassistant"])
    assert not result.blockers and result.managed_files == (b"homeassistant/a",)
    assert result.diagnostics and n.render_ignore(result)


@pytest.mark.parametrize("phase", [2, 3])
def test_unsupported_sibling_removal_is_diagnostic(tmp_path, monkeypatch, phase):
    write(tmp_path, b"homeassistant/a")
    os.mkfifo(tmp_path / "homeassistant/fifo")
    original = os.listdir
    calls = 0
    def listed(fd):
        nonlocal calls
        names = original(fd)
        if "a" in names:
            calls += 1
            if calls == phase:
                (tmp_path / "homeassistant/fifo").unlink()
                names = original(fd)
        return names
    monkeypatch.setattr(os, "listdir", listed)
    result = scan(tmp_path, [b"homeassistant"])
    assert not result.blockers and result.managed_files == (b"homeassistant/a",)
    assert result.diagnostics and n.render_ignore(result)


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
    assert_inventory_dormancy({p.name:p.read_text() for p in app.glob('*.py') if p.name!='native_inventory.py'})


def assert_inventory_dormancy(sources):
    peers={'native_metadata.py','native_disk.py','native_preview.py','native_save.py','native_git.py','native_publication.py','native_scope.py','native_recovery.py','native_apply.py','native_operations.py'}
    for name,source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.Import):
                modules=[part for alias in node.names for part in alias.name.split('.')]
            elif isinstance(node,ast.ImportFrom):
                modules=(node.module or '').split('.')+[part for alias in node.names for part in alias.name.split('.')]
            else: continue
            if 'native_inventory' in modules: assert name in peers,name


@pytest.mark.parametrize('name',['server.py','another_dormant.py'])
@pytest.mark.parametrize('source',['import app.native_inventory as alias','from app import native_inventory as alias','from .native_inventory import Policy','def lazy():\n    from ..app import native_inventory as alias'])
def test_inventory_dormancy_exact_reviewed_peers(name,source):
    assert_inventory_dormancy({'native_publication.py':source})
    with pytest.raises(AssertionError): assert_inventory_dormancy({name:source})


@pytest.mark.parametrize('acquisition,replacement', [(1,'directory'), (1,'symlink'), (2,'symlink')])
@pytest.mark.parametrize("leaf", [b"homeassistant/a", b"homeassistant/nested/a",
                                  b"data/native/repository-files/nested/a"])
def test_required_parent_transient_acquisition_failure_retained(tmp_path, monkeypatch, acquisition, replacement, leaf):
    write(tmp_path, leaf)
    relative = leaf.rpartition(b"/")[0]
    parent = tmp_path / os.fsdecode(relative)
    old = parent.parent / 'held'
    transient = parent.parent / 'temporary'
    policy = n.Policy(repository_files=(leaf,)) if leaf.startswith(b"data/") else n.Policy()
    original = n._directory_at
    calls = 0
    def acquired(root_fd, path, **kwargs):
        nonlocal calls
        if path != relative:
            return original(root_fd, path, **kwargs)
        calls += 1
        if calls != acquisition:
            return original(root_fd, path, **kwargs)
        parent.rename(old)
        if replacement == 'directory':
            parent.mkdir()
            write(tmp_path, leaf, b'replacement')
        else:
            parent.symlink_to('held', target_is_directory=True)
        try:
            return original(root_fd, path, **kwargs)
        finally:
            if replacement == 'symlink':
                parent.unlink()
            else:
                parent.rename(transient)
            old.rename(parent)
    monkeypatch.setattr(n, '_directory_at', acquired)
    result = scan(tmp_path, [leaf], policy=policy)
    assert result.diagnostics
    assert result.blockers, (result.diagnostics, n.render_ignore(result))
@pytest.mark.parametrize('phase', [2, 3])
@pytest.mark.parametrize('kind', ['fifo', 'symlink'])
def test_known_unsupported_disappears_between_listing_and_stat(tmp_path, monkeypatch, phase, kind):
    write(tmp_path, b'homeassistant/a')
    leaf = tmp_path / 'homeassistant/unsupported'
    if kind == 'fifo':
        os.mkfifo(leaf)
    else:
        leaf.symlink_to('absent')
    original = os.stat
    calls = 0
    def stated(path, *args, **kwargs):
        nonlocal calls
        if path == b'unsupported' and kwargs.get('dir_fd') is not None:
            calls += 1
            if calls == phase:
                leaf.unlink()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stated)
    result = scan(tmp_path, [b'homeassistant'])
    assert not result.blockers
    assert result.managed_files == (b'homeassistant/a',)
    assert result.diagnostics and n.render_ignore(result)


@pytest.mark.parametrize('component', [b'dir\nname', b'dir\rname', b'.git', b'configured-deny'])
def test_repository_file_request_never_traverses_line_break_scaffold(tmp_path, monkeypatch, component):
    leaf = b'data/native/repository-files/' + component + b'/nested/file'
    write(tmp_path, leaf)
    ancestor = leaf.split(b"/nested/")[0]
    policy = n.Policy(repository_files=(leaf,), extra_denies=(ancestor,) if component == b"configured-deny" else ())
    original = os.open
    opened = []
    def observed(path, flags, *args, **kwargs):
        if path == component and kwargs.get('dir_fd') is not None:
            opened.append(path)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', observed)
    result = scan(tmp_path, [leaf], policy=policy)
    assert result.blockers and not result.managed_files
    assert not opened


@pytest.mark.parametrize("acquisition", [1, 2])
@pytest.mark.parametrize("selected", [(), (b"homeassistant/a",)])
def test_root_acquisition_failure_is_retained_even_after_recovery(tmp_path, monkeypatch, acquisition, selected):
    write(tmp_path, b"homeassistant/a")
    original = n._directory_at
    calls = 0
    def acquired(fd, path, **kwargs):
        nonlocal calls
        if path == b"":
            calls += 1
            if calls == acquisition:
                raise PermissionError("root anchor acquisition fixture failure")
        return original(fd, path, **kwargs)
    monkeypatch.setattr(n, "_directory_at", acquired)
    result = scan(tmp_path, selected)
    assert result.blockers and set(result.blockers).issubset(set(result.diagnostics))


def test_unknown_eligible_child_stat_failure_still_blocks_folder(tmp_path, monkeypatch):
    write(tmp_path, b"homeassistant/a")
    original_list, original_stat = os.listdir, os.stat
    calls = 0
    def listed(fd):
        nonlocal calls
        names = original_list(fd)
        if "a" in names:
            calls += 1
            if calls == 2:
                write(tmp_path, b"homeassistant/new")
                names = original_list(fd)
        return names
    def stated(path, *args, **kwargs):
        if path == b"new" and kwargs.get("dir_fd") is not None:
            raise PermissionError("unknown eligible child")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "listdir", listed)
    monkeypatch.setattr(os, "stat", stated)
    assert scan(tmp_path, [b"homeassistant"]).blockers


@pytest.mark.parametrize('phase', [2, 3])
@pytest.mark.parametrize('kind', ['regular', 'fifo', 'symlink'])
@pytest.mark.parametrize('relative', [b'homeassistant/member', b'homeassistant/nested/member',
                                    b'data/native/repository-files/member'])
def test_required_prefix_nondirectory_baseline_cannot_become_allowed_directory(tmp_path, monkeypatch, phase, kind, relative):
    leaf = relative + b'/child'
    policy = n.Policy(repository_files=(leaf,)) if relative.startswith(b'data/') else n.Policy()
    full = os.fsencode(tmp_path) + b'/' + relative
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if kind == 'regular':
        write(tmp_path, relative)
    elif kind == 'fifo':
        os.mkfifo(full)
    else:
        os.symlink(b'missing', full)
    original = os.stat
    calls = 0
    def stated(path, *args, **kwargs):
        nonlocal calls
        if path == b'member' and kwargs.get('dir_fd') is not None:
            calls += 1
            if calls == phase:
                os.unlink(full)
                os.mkdir(full)
                write(tmp_path, leaf)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stated)
    result = scan(tmp_path, [leaf], policy=policy)
    assert result.diagnostics
    assert result.blockers
    assert any(i.path == relative and i.reason == "unsafe-required-parent" for i in result.blockers)
    with pytest.raises(n.Blocked):
        n.render_ignore(result, policy)


@pytest.mark.parametrize("phase", [2, 3])
@pytest.mark.parametrize("change", ["fifo-symlink", "symlink-fifo", "file-directory", "fifo-file"])
def test_folder_unsupported_retypes_keep_semantic_membership(tmp_path, monkeypatch, phase, change):
    write(tmp_path, b"homeassistant/a")
    full = tmp_path / "homeassistant/member"
    before, after = change.split("-")
    if before == "fifo":
        os.mkfifo(full)
    elif before == "symlink":
        full.symlink_to("missing")
    else:
        write(tmp_path, b"homeassistant/member")
    original = os.stat
    calls = 0
    def stated(path, *args, **kwargs):
        nonlocal calls
        if path == b"member" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == phase:
                full.unlink()
                if after == "fifo":
                    os.mkfifo(full)
                elif after == "symlink":
                    full.symlink_to("missing")
                elif after == "directory":
                    full.mkdir()
                else:
                    write(tmp_path, b"homeassistant/member")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stated)
    result = scan(tmp_path, [b"homeassistant"])
    assert bool(result.blockers) == (change in ("file-directory", "fifo-file"))
    if not result.blockers:
        assert result.managed_files == (b"homeassistant/a",)
        assert result.diagnostics and n.render_ignore(result)


@pytest.mark.parametrize("root", [b"homeassistant", b"addon_configs/example", b"custom/live"])
@pytest.mark.parametrize("name", [b".ha-ops-recovery", b".ha-ops-recovery-", b".ha-ops-recovery-stale", b".ha-ops-recovery-" + b"a"*32 + b"-" + b"b"*16])
def test_recovery_pool_namespace_is_reserved_at_every_live_depth(root, name):
    policy = n.Policy(extra_live_roots=(b"custom/live",))
    for path in (root+b"/"+name, root+b"/nested/"+name+b"/stage-"+b"c"*64):
        assert policy.reason(path) == "private-recovery-pool"
        assert not n.allowed_path(path, (root,), policy)
    for name in (b".manual", b"manualdata", b".ha-ops-recoverynotes"):
        assert policy.reason(root+b"/nested/"+name+b"/file") is None


def test_recovery_pools_never_enter_inventory_whitelist_or_complete_index(tmp_path):
    private = [b"homeassistant/.ha-ops-recovery-"+b"a"*32+b"-"+b"b"*16+b"/stage-"+b"c"*64,
               b"homeassistant/nested/.ha-ops-recovery-stale/removed-original",
               b"addon_configs/example/.ha-ops-recovery-broken/blob-original"]
    ordinary = [b"homeassistant/.manual/settings", b"homeassistant/manualdata/settings",
                b"addon_configs/example/config.yaml"]
    for path in private+ordinary:
        write(tmp_path, path)
    scope = (b"homeassistant", b"addon_configs")
    result, rules = fixture_git(tmp_path, scope)
    assert not result.blockers
    assert set(result.managed_files) == set(ordinary)
    assert all(path not in rules for path in private)
    assert {e.reason for e in result.entries if b".ha-ops-recovery" in e.path} == {"private-recovery-pool"}
    git(tmp_path, "add", "-A")
    assert n.audit_index(tmp_path, scope).clean
    assert n.audit_ignore(tmp_path, private+ordinary, result).clean
    git(tmp_path, "add", "-f", "--", *(os.fsdecode(p) for p in private))
    audit = n.audit_index(tmp_path, scope)
    assert {(i.path, i.reason) for i in audit.blockers} == {(p, "private-recovery-pool") for p in private}
