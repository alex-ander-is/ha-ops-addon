"""Physical local proofs for dormant storage accounting/admission; no live HA."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import native_disk as d
import native_inventory as inv


def write(root, path, data=b"fixture"):
    full = os.path.join(os.fsencode(root), os.fsencode(path))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return full


def git(root, *args, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.check_output(["git", "--git-dir=" + str(Path(root) / "data/native/repository.git"), *args], input=input, env=env)


@pytest.fixture
def fixture(tmp_path):
    # macOS tempfile roots can pass through a /var symlink; acquire real paths.
    root = Path(os.path.realpath(tmp_path))
    (root / "data/native").mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--initial-branch=fixture",
                    str(root / "data/native/repository.git")], check=True, capture_output=True)
    return root, d.NativeDisk(root)


def controls(operation="op", owner="owner", extra=None):
    payload = json.dumps({"operation_id": operation, "owner_id": owner,
                          "full_pending_state": extra or {"scope": [], "error": ""}}, sort_keys=True).encode()
    return (d.Control(d.Target(b"data/native-operations", "directory"), payload),)


def request(**kw):
    return d.Evidence("op", "owner", controls=controls(), **kw)


def selected(disk, root, path):
    s = disk.source(root / path)
    return ((os.fsencode(path), s),)


def operational(disk):
    """Only arithmetic host tests use this seam; Linux proofs use kernel data."""
    original = disk._mount
    def mount(path, info, mounts):
        key, identity, fs, supported = original(path, info, mounts)
        return key, identity, b"tmpfs", True
    return mock.patch.object(disk, "_mount", side_effect=mount)


def test_import_construction_and_snapshot_observe_without_writes(fixture):
    root, disk = fixture
    def filesystem_evidence():
        result = {}
        for p in root.rglob("*"):
            s = p.lstat()
            # Read-only traversal may update atime; preserve all write-sensitive
            # identity/ownership/content-allocation and nanosecond time fields.
            result[str(p.relative_to(root))] = (s.st_dev, s.st_ino, s.st_mode, s.st_nlink,
                s.st_uid, s.st_gid, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_blocks)
        return result
    before = filesystem_evidence()
    with mock.patch.object(d.os, "open", side_effect=AssertionError("constructor opened storage")), \
            mock.patch.object(d.os, "stat", side_effect=AssertionError("constructor scanned storage")), \
            mock.patch.object(d.subprocess, "Popen", side_effect=AssertionError("constructor launched subprocess")):
        fresh = d.NativeDisk(root)
    with mock.patch.object(d.subprocess, "Popen", side_effect=AssertionError("subprocess")):
        report = fresh.snapshot()
    assert report.complete, report.issues
    after = filesystem_evidence()
    assert before == after
    assert not (root / "data/native/disk-admission.lock").exists()
    app = Path(d.__file__).parent
    assert all("import native_disk" not in p.read_text() and "from native_disk" not in p.read_text()
               for p in app.glob("*.py") if p.name not in ("native_disk.py", "native_git.py", "native_save.py", "native_publication.py", "native_scope.py", "native_recovery.py", "native_apply.py", "native_services.py", "native_operations.py", "native_storage.py"))


def test_categories_orphans_sparse_directories_and_original_exclusion(fixture):
    root, disk = fixture
    for path in ("data/native/git-resources/orphan/spool", "data/native/diffs/debt.tmp",
                 "data/native/recovery/old/file", "data/native/service.tmp"):
        write(root, path, os.urandom(5000))
    sparse = write(root, "data/native/git-resources/sparse")
    with open(sparse, "r+b") as f:
        f.truncate(16 * 1024**2)
    write(root, "homeassistant/original", b"x" * 10000)
    write(root, "backup/user.tar", b"y" * 10000)
    report = disk.snapshot()
    assert report.complete, report.issues
    expected = sum(p.lstat().st_blocks * 512 for p in (root / "data/native").rglob("*")) + (root / "data/native").stat().st_blocks * 512
    assert report.allocated == expected
    assert report.logical > report.allocated
    assert report.allocated == sum(v.allocated for v in report.volumes)
    assert {row[0] for row in report.categories} == set(d.CATEGORIES)
    assert dict((row[0], row[2]) for row in report.categories)["recovery"] > 0
    assert all(b"homeassistant" not in path and b"/backup/" not in path for path, _ in report.observations)


def test_overlapping_legacy_anchors_and_internal_hardlink_deduplicate(fixture):
    root, disk = fixture
    first = write(root, "data/work/a", b"x" * 10000)
    os.link(first, root / "data/work/b")
    disk = d.NativeDisk(root, layout=d.Layout((b"data/work", b"data/work/sub")))
    write(root, "data/work/sub/c")
    report = disk.snapshot()
    assert report.complete, report.issues
    seen = {}
    for path, obs in report.observations:
        if obs is not None:
            seen[obs[:2]] = obs[-1] * 512
    assert report.allocated == sum(seen.values())


def test_cross_boundary_hardlink_is_unknown(fixture):
    root, disk = fixture
    first = write(root, "homeassistant/live")
    os.link(first, root / "data/native/adopted")
    assert any("cross-boundary-hardlink" in s for s in disk.snapshot().issues)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "parent"])
def test_no_follow_special_objects(fixture, kind):
    root, disk = fixture
    if kind == "symlink":
        os.symlink("/etc", root / "data/native/link")
    elif kind == "fifo":
        os.mkfifo(root / "data/native/pipe")
    else:
        os.rename(root / "data/native", root / "data/kept")
        os.symlink("kept", root / "data/native")
    report = disk.snapshot()
    assert not report.complete
    assert report.duration < 3


def test_missing_optional_anchor_requires_safe_parent(fixture):
    root, disk = fixture
    assert disk.snapshot().complete
    os.symlink("/tmp", root / "data/native-operations")
    assert not disk.snapshot().complete


@pytest.mark.parametrize("limits", [d.Limits(entries=2), d.Limits(seconds=1e-12)])
def test_observation_budget_never_reports_complete_zero(fixture, limits):
    root, _ = fixture
    report = d.NativeDisk(root, limits=limits).snapshot()
    assert not report.complete
    assert any("budget" in i for i in report.issues)


def test_two_pass_growth_retains_first_error(fixture, monkeypatch):
    root, disk = fixture
    original = disk._scan
    calls = []
    def scan(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 1:
            write(root, "data/native/new")
            return (*result[:4], ("first-read-error",))
        return result
    monkeypatch.setattr(disk, "_scan", scan)
    report = disk.snapshot()
    assert "first-read-error" in report.issues and "changed-two-pass-snapshot" in report.issues


def test_stat_and_mount_failure_are_visible(fixture, monkeypatch):
    root, disk = fixture
    original = d.os.stat
    def fail(path, *a, **kw):
        if path == b"fault":
            raise PermissionError("fixture-stat-denied")
        return original(path, *a, **kw)
    write(root, "data/native/fault")
    monkeypatch.setattr(d.os, "stat", fail)
    assert any("stat-denied" in i for i in disk.snapshot().issues)
    if sys.platform == "linux":
        monkeypatch.setattr(d, "linux_mounts", lambda: ())
        assert any("mount" in i for i in disk.snapshot().issues)


def test_removed_system_backup_surface_and_user_exclusion(fixture):
    import inspect
    root, disk = fixture
    assert not hasattr(d, "BackupItem") and not hasattr(d, "BackupReceipt")
    assert "receipts" not in inspect.signature(d.NativeDisk).parameters
    assert "backup" not in inspect.signature(d.Evidence).parameters
    assert "backup" not in d.CATEGORIES
    write(root, "backup/user.tar", b"user managed" * 1000)
    report = disk.snapshot()
    assert report.complete
    assert not any(b"/backup/" in path for path, _ in report.observations)
    with operational(disk):
        plan = disk.project("backup", report, request())
    assert "unsupported-operation-kind" in plan.unknown


def test_selection_spool_loose_index_trees_and_exact_reserve(fixture):
    root, disk = fixture
    write(root, "homeassistant/a", os.urandom(128 * 1024))
    evidence = request(selected=selected(disk, root, "homeassistant/a"), materialize=True)
    with operational(disk):
        plan = disk.project("initial_selection", disk.snapshot(), evidence)
    assert plan.supported, plan.unknown
    names = [a.output for a in plan.allocations]
    assert any(n.startswith("stream-spool") for n in names)
    assert any(n.startswith("loose-blob") for n in names)
    assert "complete-index" in names and "index-lock-overlap" in names
    # Git 2.54 always creates this extra empty per-worktree directory. Its
    # physical block and inode must belong to the original frozen forecast.
    refs = [a for a in plan.allocations if a.output == 'worktree-admin-refs-directory']
    assert len(refs) == 1 and refs[0].inodes == 1
    assert refs[0].logical == 0
    assert any(n.startswith("generated-tree") for n in names)
    assert sum(a.logical for a in plan.allocations) > 3 * 128 * 1024
    for r in plan.reserves:
        assert r.pending_control >= d._round(len(evidence.controls[0].payload), r.quantum)
        assert r.history == () and r.observed_growth == 0


def test_sparse_source_uses_logical_length_and_no_file_cap(fixture):
    root, disk = fixture
    path = write(root, "homeassistant/large")
    with open(path, "r+b") as f:
        f.truncate(512 * 1024**2)
    evidence = request(selected=selected(disk, root, "homeassistant/large"))
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), evidence)
    assert plan.supported, plan.unknown
    spool = next(a for a in plan.allocations if a.output.startswith("stream-spool"))
    assert spool.logical == 512 * 1024**2
    assert spool.allocated >= spool.logical


def test_exact_inventory_captures_timestamp_and_rejects_stale_source(fixture):
    root, disk = fixture
    write(root, "homeassistant/a")
    s = disk.source(root / "homeassistant/a")
    entry = inv.Entry(b"homeassistant/a", "file", s.size, s.observation[0], s.observation[1],
                      s.observation[2], 1, None, True, None)
    inventory = inv.Inventory((b"homeassistant",), (entry,), (), True)
    e = disk.selection(inventory, root, operation_id="op", owner_id="owner", controls=controls())
    write(root, "homeassistant/a", b"longer")
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), e)
    assert "stale-source-evidence" in plan.unknown
    with pytest.raises(d.Blocked, match="stale-native-inventory"):
        disk.selection(inventory, root, operation_id="op", owner_id="owner", controls=controls())


def test_complete_large_base_deep_byte_paths_index_sizes(fixture):
    root, disk = fixture
    oid = git(root, "hash-object", "-w", "--stdin", input=b"existing").strip()
    paths = [b"homeassistant/deep/" + str(i).encode() + b"/entry" for i in range(300)]
    data = b"".join(b"100644 " + oid + b"\t" + p + b"\0" for p in paths)
    index = root / "fixture-index"
    env = os.environ.copy()
    env.update(GIT_INDEX_FILE=str(index), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "--git-dir=" + str(root / "data/native/repository.git"), "update-index", "-z", "--index-info"], input=data, env=env, check=True)
    tree = subprocess.check_output(["git", "--git-dir=" + str(root / "data/native/repository.git"), "write-tree"], env=env).strip().decode()
    write(root, "homeassistant/changed")
    e = request(selected=selected(disk, root, "homeassistant/changed"), base_tree=tree)
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), e)
    assert plan.supported, plan.unknown
    projected = next(a.logical for a in plan.allocations if a.output == "complete-index")
    assert projected >= index.stat().st_size
    assert len([a for a in plan.allocations if a.output.startswith("generated-tree")]) >= 303


@pytest.mark.parametrize("key,value", [("core.compression", "9"), ("index.version", "4"), ("core.splitIndex", "true"), ("remote.origin.promisor", "true")])
def test_unsupported_git_executor_config_is_explicit(fixture, key, value):
    root, disk = fixture
    git(root, "config", key, value)
    with operational(disk):
        plan = disk.project("repack", disk.snapshot(), request())
    assert any("unsupported-git-config" in i for i in plan.unknown)


def test_ordinary_remote_and_branch_routing_remains_usable_without_network(fixture):
    root, disk = fixture
    git(root, "config", "remote.origin.url", "https://invalid.example/config.git")
    git(root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(root, "config", "branch.fixture.remote", "origin")
    git(root, "config", "branch.fixture.merge", "refs/heads/fixture")
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
    assert p.supported, p.unknown


def test_repack_complete_all_kinds_unreachable_and_old_pack_overlap(fixture):
    root, disk = fixture
    blob = git(root, "hash-object", "-w", "--stdin", input=os.urandom(128 * 1024)).strip()
    tree = git(root, "mktree", input=b"100644 blob " + blob + b"\tfixture\n").strip()
    commit = git(root, "commit-tree", tree.decode(), input=b"fixture commit\n").strip()
    tag_payload = b"object " + commit + b"\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 100 +0000\n\nfixture\n"
    git(root, "hash-object", "-w", "-t", "tag", "--stdin", input=tag_payload)
    git(root, "hash-object", "-w", "--stdin", input=b"unreachable")
    git(root, "update-ref", "refs/heads/fixture", commit.decode())
    git(root, "repack", "-a", "-d")
    old = disk.snapshot()
    with operational(disk):
        plan = disk.project("repack", disk.snapshot(), request())
    assert plan.supported, plan.unknown
    objects = disk._objects(20)
    assert {kind for _, kind, _ in objects} == {b"blob", b"tree", b"commit", b"tag"}
    assert len(objects) >= 5
    assert any(a.output == "new-pack-old-packs-retained" for a in plan.allocations)
    assert disk.snapshot().allocated == old.allocated


def test_malformed_truncated_git_object_evidence_is_not_zero(fixture, monkeypatch):
    root, disk = fixture
    original = disk._git
    def response(args):
        return b"malformed\n" if args[0] == "cat-file" else original(args)
    monkeypatch.setattr(disk, "_git", response)
    with operational(disk):
        plan = disk.project("repack", disk.snapshot(), request())
    assert "malformed-object-observation" in plan.unknown


def test_verified_local_incoming_pack_has_usable_recipe(fixture):
    root, disk = fixture
    blob = git(root, "hash-object", "-w", "--stdin", input=os.urandom(10000)).strip()
    prefix = root / "incoming"
    name = git(root, "pack-objects", "--window=0", "--depth=0", "--no-reuse-object", str(prefix), input=blob + b"\n").strip()
    pack = Path(str(prefix) + "-" + name.decode() + ".pack")
    idx = pack.with_suffix(".idx")
    e = request(incoming=disk.source(pack), incoming_index=disk.source(idx))
    with operational(disk):
        plan = disk.project("fetch", disk.snapshot(), e)
    assert plan.supported, plan.unknown
    assert any(a.output == "incoming-spool" for a in plan.allocations)
    assert any(a.output == "copied-complete-pack" for a in plan.allocations)


@pytest.mark.parametrize("kind,e", [("fetch", request()), ("apply", request())])
def test_real_external_evidence_limits_are_explicit(fixture, kind, e):
    root, disk = fixture
    with operational(disk):
        plan = disk.project(kind, disk.snapshot(), e)
    assert not plan.supported
    assert all(dec.status == "cannot-establish-admission" for dec in plan.decisions)
    assert plan.provenance


def test_apply_independent_recovery_copies_and_metadata(fixture):
    root, disk = fixture
    s = disk.source(write(root, "homeassistant/before", os.urandom(5000)))
    m = disk.source(write(root, "exact-metadata", b'{"mode":384,"acl":[],"xattrs":[]}'))
    e = request(copies=((s, d.Target(b"data/native/recovery/slot", "directory")), (s, d.Target(b"homeassistant", "directory"))),
                metadata=((m, d.Target(b"data/native/recovery/slot", "directory")),))
    with operational(disk):
        plan = disk.project("apply", disk.snapshot(), e)
    assert plan.supported, plan.unknown
    assert len([a for a in plan.allocations if a.output.startswith("independent-copy")]) == 2
    assert not any("backup" in a.output for a in plan.allocations)


def test_apply_git_blob_replacement_and_deletion_before_copy(fixture):
    root, disk = fixture
    before = disk.source(write(root, "homeassistant/deleted", b"recover these bytes"))
    metadata = disk.source(write(root, "metadata", b'{"mode":384,"xattrs":[]}'))
    oid = git(root, "hash-object", "-w", "--stdin", input=os.urandom(7000)).strip().decode()
    e = request(copies=((before, d.Target(b"data/native/recovery", "directory")),), replacements=((oid, d.Target(b"homeassistant", "directory")),),
                metadata=((metadata, d.Target(b"data/native/recovery", "directory")),))
    with operational(disk):
        p = disk.project("apply", disk.snapshot(), e)
    assert p.supported, p.unknown
    assert next(a for a in p.allocations if a.output.startswith("replacement-temporary")).logical == 7000
    assert any(a.logical == before.size for a in p.allocations)


def test_distinct_volumes_never_pool_free_bytes(fixture, monkeypatch):
    root, disk = fixture
    s = disk.source(write(root, "homeassistant/input", b"x" * 10000))
    original = disk._volume
    def volume(path, fd, mounts):
        v = original(path, fd, mounts)
        small = path.startswith(os.fsencode(root / "homeassistant"))
        return replace(v, key=("small" if small else "large",), filesystem=b"tmpfs", supported=True,
                       available=1 if small else 10**9)
    monkeypatch.setattr(disk, "_volume", volume)
    p = disk.project("scope", disk.snapshot(), request(copies=((s, d.Target(b"homeassistant", "directory")),)))
    assert {r.volume: r.status for r in p.decisions} == {("large",): "fits", ("small",): "insufficient-space"}


def test_unknown_readonly_quota_or_stacked_volume_never_claims_authority(fixture, monkeypatch):
    root, disk = fixture
    with operational(disk):
        original = disk._volume
        monkeypatch.setattr(disk, "_volume", lambda *a: replace(original(*a), supported=False))
        p = disk.project("scope", disk.snapshot(), request())
    assert any("unsupported-destination" in u for u in p.unknown)
    assert all(r.status == "cannot-establish-admission" for r in p.decisions)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_supported_object_format_index_tree_recipe(tmp_path, object_format):
    root = Path(os.path.realpath(tmp_path))
    (root / "data/native").mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--object-format=" + object_format,
                    str(root / "data/native/repository.git")], check=True, capture_output=True)
    disk = d.NativeDisk(root)
    write(root, "homeassistant/a")
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request(selected=selected(disk, root, "homeassistant/a")))
    assert p.supported, p.unknown
    assert any(a.output.startswith("generated-tree") for a in p.allocations)


def test_growth_reserve_is_local_to_affected_volume(fixture, monkeypatch):
    root, disk = fixture
    s = disk.source(write(root, "homeassistant/input"))
    original = disk._volume
    def volume(path, fd, mounts):
        v = original(path, fd, mounts)
        key = ("live",) if path.startswith(os.fsencode(root / "homeassistant")) else ("private",)
        return replace(v, key=key, filesystem=b"tmpfs", supported=True, available=10**9)
    monkeypatch.setattr(disk, "_volume", volume)
    monkeypatch.setattr(disk, "_volume", lambda *a: replace(volume(*a), available=10**9 + 10000))
    previous = disk.snapshot()
    monkeypatch.setattr(disk, "_volume", volume)
    p = disk.project("scope", disk.snapshot(), request(copies=((s, d.Target(b"homeassistant", "directory")),)), previous=previous)
    reserves = {r.volume: r for r in p.reserves}
    assert reserves[("private",)].observed_growth == 10000
    assert reserves[("live",)].observed_growth == 0


def test_source_change_during_git_observation_stays_blocked(fixture, monkeypatch):
    root, disk = fixture
    write(root, "homeassistant/a")
    e = request(selected=selected(disk, root, "homeassistant/a"))
    original = disk._profile
    def profile():
        result = original()
        write(root, "homeassistant/a", b"changed length")
        return result
    monkeypatch.setattr(disk, "_profile", profile)
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), e)
    assert "stale-source-evidence" in p.unknown


def test_migration_keeps_legacy_and_cleanup_debt_baseline(fixture):
    root, _ = fixture
    write(root, "data/repo/old", os.urandom(12000))
    disk = d.NativeDisk(root, layout=d.Layout((b"data/repo",)))
    s = disk.source(root / "data/repo/old")
    before = disk.snapshot()
    with operational(disk):
        plan = disk.project("migration", disk.snapshot(), request(copies=((s, d.Target(b"data/native/repository.git", "directory")),)))
    assert plan.supported, plan.unknown
    assert any("independent-copy" in a.output for a in plan.allocations)
    assert disk.snapshot().allocated == before.allocated


@pytest.mark.parametrize("available,warning", [(d.WARNING_BYTES - 1, True), (d.WARNING_BYTES, False), (d.WARNING_BYTES + 1, False)])
def test_warning_exact_binary_threshold(available, warning):
    assert d.Volume((1,), (), available, 100, 4096, b"tmpfs", True).warning is warning


@pytest.mark.parametrize("offset,expected", [(0, "fits"), (-1, "insufficient-space"), (1, "fits")])
def test_fit_equality_and_one_byte_shortfall(fixture, monkeypatch, offset, expected):
    root, disk = fixture
    with operational(disk):
        snapshot = disk.snapshot()
        first = disk.project("scope", snapshot, request())
        needed = first.decisions[0].additional_peak + first.decisions[0].reserve
        original = disk._volume
        monkeypatch.setattr(disk, "_volume", lambda *a: replace(original(*a), available=needed + offset))
        plan = disk.project("scope", disk.snapshot(), request())
    assert plan.decisions[0].status == expected


def test_inode_scarcity_and_unknown_metric(fixture, monkeypatch):
    root, disk = fixture
    with operational(disk):
        original = disk._volume
        monkeypatch.setattr(disk, "_volume", lambda *a: replace(original(*a), inodes=0))
        plan = disk.project("scope", disk.snapshot(), request())
        assert plan.decisions[0].status == "insufficient-space"
        monkeypatch.setattr(disk, "_volume", lambda *a: replace(original(*a), inodes=None))
        plan = disk.project("scope", disk.snapshot(), request())
        assert plan.decisions[0].status == "fits"
        assert plan.decisions[0].available_inodes is None


def test_full_current_and_growing_pending_control_reserve(fixture, monkeypatch):
    root, disk = fixture
    import native_operations
    native_operations.NativeOperationStore(root / "data").read()
    with operational(disk):
        original = disk._volume
        monkeypatch.setattr(disk, "_volume", lambda *args: replace(original(*args), available=100_000_000))
        monkeypatch.setattr(disk, "_volume", lambda *args: replace(original(*args), available=100_010_000))
        old = disk.snapshot()
        monkeypatch.setattr(disk, "_volume", lambda *args: replace(original(*args), available=100_000_000))
        e = d.Evidence("op", "owner", controls=controls(extra={"error": "details" * 10000}))
        plan = disk.project("scope", disk.snapshot(), e, previous=old)
    r = plan.reserves[0]
    assert r.current_control > 0 and r.pending_control >= 70000
    assert r.observed_growth == 10000 and r.history


def test_guard_retains_issued_initial_forecast_and_refuses_recreated_authority(fixture):
    root, disk = fixture
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="unissued"):
            with disk.guard(replace(plan, allocations=())):
                pass
        with pytest.raises(d.Blocked, match="unissued"):
            with d.NativeDisk(root).guard(plan):
                pass
        with disk.guard(plan) as guard:
            assert guard.current.decisions[0].status == "fits"
            with pytest.raises(d.Blocked, match="nested"):
                with disk.guard(plan):
                    pass
        with pytest.raises(d.Blocked, match="context-ended"):
            guard.revalidate()


def test_stale_same_inode_timestamp_source_and_lock_replacement(fixture):
    root, disk = fixture
    write(root, "homeassistant/a", b"before")
    e = request(selected=selected(disk, root, "homeassistant/a"))
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), e)
        with disk.guard(plan) as guard:
            write(root, "homeassistant/a", b"after!")
            os.utime(root / "homeassistant/a", ns=(e.selected[0][1].observation[5] + 1_000_000_000,) * 2)
            with pytest.raises(d.Blocked, match="stale-source"):
                guard.revalidate()
        plan = disk.project("scope", disk.snapshot(), request())
        with disk.guard(plan) as guard:
            os.unlink(root / "data/native/disk-admission.lock")
            write(root, "data/native/disk-admission.lock")
            with pytest.raises(d.Blocked, match="replaced-admission-lock"):
                guard.revalidate()


@pytest.mark.parametrize("state", ["resource", "operation", "orphan"])
def test_active_or_unreconciled_writers_cannot_get_guard(fixture, state):
    root, disk = fixture
    if state == "resource":
        write(root, "data/native/git-resources/journal.json", b'{"records":{"x":{}}}')
    elif state == "operation":
        write(root, "data/native-operations/state.json", b'{"active":"someone"}')
    else:
        write(root, "data/native/git-resources/unknown/spool")
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="unreconciled|competing|unverifiable|resource|private-journal"):
            with disk.guard(plan):
                pass


def test_threads_across_instances_are_fenced(fixture):
    root, disk = fixture
    disk2 = d.NativeDisk(root)
    result = []
    with operational(disk), operational(disk2):
        p = disk.project("scope", disk.snapshot(), request())
        p2 = disk2.project("scope", disk2.snapshot(), request())
        def contender():
            try:
                with disk2.guard(p2):
                    result.append("unsafe")
            except d.Blocked:
                result.append("fenced")
        with disk.guard(p):
            t = threading.Thread(target=contender)
            t.start()
            t.join(3)
            assert not t.is_alive()
    assert result == ["fenced"]


def test_same_operation_real_native_index_can_revalidate_and_inactive_residue_counts(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    e = d.Evidence(op, owner, controls=controls(op, owner))
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), e)
        with disk.guard(p) as guard:
            with adapter.isolated_index(None, operation_id=op, owner_id=owner):
                with d._directory(root / "data/native/git-resources") as created:
                    guard.record_directory_creation(b"data/native/git-resources", created)
                guard.revalidate()
        other_op, other_owner = str(uuid.uuid4()), str(uuid.uuid4())
        p = disk.project("scope", disk.snapshot(), e)
        with adapter.isolated_index(None, operation_id=other_op, owner_id=other_owner):
            with pytest.raises(d.Blocked, match="competing-native-resource-writer"):
                with disk.guard(p):
                    pass
        # Simulate interrupted addressed cleanup while the kernel lease is
        # released. The old bytes remain in accounting, but are not active demand.
        from types import SimpleNamespace
        monkeypatch.setattr(adapter, "_cleanup", lambda *args, **kwargs: SimpleNamespace(cleanup_errors=("injected retained debt",)))
        with pytest.raises(native_git.CleanupDebt):
            with adapter.isolated_index(None, operation_id=other_op, owner_id=other_owner):
                pass
        residue = disk.snapshot().allocated
        p = disk.project("scope", disk.snapshot(), e)
        with disk.guard(p):
            pass
        assert disk.snapshot().allocated >= residue


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux physical guard proof")
def test_linux_real_allocations_with_capped_initial_capacity_warning(fixture, monkeypatch, capsys):
    root, disk = fixture
    write(root, "homeassistant/source", os.urandom(128 * 1024))
    e = request(selected=selected(disk, root, "homeassistant/source"))
    original = disk._volume
    def capped_initial_capacity(*args):
        volume = original(*args)
        return replace(volume, available=min(volume.available, 16 * 1024**2))
    # Advisory arithmetic uses a conservative initial cap; all following
    # filesystem allocation/free-space observations are actual kernel facts.
    monkeypatch.setattr(disk, "_volume", capped_initial_capacity)
    plan = disk.project("scope", disk.snapshot(), e)
    assert plan.supported, plan.unknown
    with disk.guard(plan) as guard:
        assert guard.current.decisions[0].warning
        before = os.statvfs(root)
        spool = write(root, "data/native/measured-spool", (root / "homeassistant/source").read_bytes())
        git(root, "hash-object", "-w", "--stdin", input=Path(os.fsdecode(spool)).read_bytes())
        after = os.statvfs(root)
        consumed = max(0, before.f_bavail - after.f_bavail) * before.f_frsize
        assert consumed <= guard.current.decisions[0].additional_peak
        print("LINUX actual spool+incompressible blob allocation", consumed, "bound", guard.current.decisions[0].additional_peak,
              "st_blocks", os.stat(spool).st_blocks, "f_frsize", before.f_frsize)


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux large guard proof")
def test_linux_large_measurable_source_obtains_guard(fixture):
    root, disk = fixture
    path = write(root, "homeassistant/large")
    with open(path, "r+b") as f:
        f.truncate(32 * 1024**2)
    p = disk.project("scope", disk.snapshot(), request(selected=selected(disk, root, "homeassistant/large")))
    with disk.guard(p) as guard:
        assert guard.current.decisions[0].status == "fits"
        print("LINUX large sparse logical source guarded", 32 * 1024**2, "available", guard.current.decisions[0].available)


@pytest.mark.skipif(sys.platform != "linux", reason="actual invalid UTF-8 filename proof")
def test_linux_invalid_byte_names(fixture):
    root, disk = fixture
    write(root, b"data/native/invalid-\xff", b"bytes")
    report = disk.snapshot()
    assert report.complete, report.issues
    assert any(path.endswith(b"\xff") for path, _ in report.observations)
    print("LINUX invalid-byte names retained")


@pytest.mark.skipif(sys.platform != "linux", reason="actual mountinfo bind observations")
def test_linux_wrapper_real_bind_capacity_aliases():
    if not Path("/workspace/input").is_dir():
        pytest.skip("requires repository Linux wrapper bind mounts")
    disk = d.NativeDisk("/workspace")
    mounts = d.linux_mounts()
    one, _ = disk._destination(b"input", mounts)
    two, _ = disk._destination(b"output", mounts)
    assert one.mounts != two.mounts
    assert one.key == two.key
    assert one.quantum == two.quantum
    print("LINUX distinct bind mount IDs, shared volume", one.mounts, two.mounts, one.key,
          "minimum available", min(one.available, two.available))


@pytest.mark.skipif(sys.platform != "linux", reason="actual independent process flock proof")
def test_linux_process_fencing_and_death_releases_lock(fixture):
    root, disk = fixture
    script = '''import sys,json,time
sys.path.insert(0,sys.argv[2]); import native_disk as d
disk=d.NativeDisk(sys.argv[1]); e=d.Evidence("child","owner",controls=(d.Control(d.Target(b"data/native-operations", "directory"),json.dumps({"operation_id":"child","owner_id":"owner"}).encode()),))
p=disk.project("scope",disk.snapshot(),e)
with disk.guard(p):
 print("HELD",flush=True); time.sleep(30)
'''
    proc = subprocess.Popen([sys.executable, "-u", "-c", script, str(root), str(Path(d.__file__).parent)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"HELD"
        p = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="competing-process"):
            with disk.guard(p):
                pass
        proc.kill()
        proc.wait(timeout=3)
        with disk.guard(p):
            pass
        print("LINUX independent child flock fenced; kernel release after kill verified")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(sys.platform != "linux", reason="actual local incoming/repack guard proofs")
def test_linux_repack_and_verified_incoming_guard_allocate(fixture):
    root, disk = fixture
    oid = git(root, "hash-object", "-w", "--stdin", input=os.urandom(256 * 1024)).strip()
    git(root, "hash-object", "-w", "--stdin", input=b"unreachable")
    p = disk.project("repack", disk.snapshot(), request())
    with disk.guard(p) as guard:
        before = os.statvfs(root)
        all_objects = b"".join(item[0] + b"\n" for item in disk._objects(20))
        name = git(root, "pack-objects", "--window=0", "--depth=0", "--no-reuse-object", "--no-reuse-delta",
                   str(root / "data/native/repository.git/objects/pack/pack"), input=all_objects).strip().decode()
        after = os.statvfs(root)
        consumed = max(0, before.f_bavail - after.f_bavail) * before.f_frsize
        assert consumed <= guard.current.decisions[0].additional_peak
        print("LINUX repack old objects retained, actual new pack+index", consumed,
              "bound", guard.current.decisions[0].additional_peak)
    prefix = root / "data/native/repository.git/objects/pack" / ("pack-" + name)
    e = request(incoming=disk.source(str(prefix) + ".pack"), incoming_index=disk.source(str(prefix) + ".idx"))
    p = disk.project("fetch", disk.snapshot(), e)
    with disk.guard(p) as guard:
        assert guard.current.decisions[0].status == "fits"
        print("LINUX verified standalone incoming pack guard obtained", guard.current.decisions[0].additional_peak)


@pytest.mark.skipif(sys.platform != "linux", reason="actual many tiny file allocation proof")
def test_linux_many_tiny_outputs_and_descriptor_budget(fixture):
    root, disk = fixture
    sources = []
    for i in range(150):
        path = "homeassistant/deep/" + str(i)
        write(root, path, b"x")
        sources.extend(selected(disk, root, path))
    p = disk.project("scope", disk.snapshot(), request(selected=tuple(sources)))
    with disk.guard(p) as guard:
        before = os.statvfs(root)
        for path, source in sources:
            write(root, b"data/native/tiny/" + path, b"x")
            git(root, "hash-object", "-w", "--stdin", input=b"x")
        after = os.statvfs(root)
        consumed = max(0, before.f_bavail - after.f_bavail) * before.f_frsize
        assert consumed <= guard.current.decisions[0].additional_peak
        print("LINUX 150 tiny nested outputs actual", consumed, "bound", guard.current.decisions[0].additional_peak)
    import resource
    old = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(32, old[1]), old[1]))
        assert disk.snapshot().complete
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, old)


def test_issued_evidence_cannot_shrink_through_mutable_controls(fixture):
    root, disk = fixture
    mutable = list(controls(extra={"error": "X" * 100000}))
    e = d.Evidence("op", "owner", controls=mutable)
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), e)
        needed = sum(x.additional_peak + x.reserve for x in p.decisions)
        mutable[0] = controls()[0]
        with disk.guard(p) as admission:
            assert sum(x.additional_peak + x.reserve for x in admission.current.decisions) >= needed


def test_nested_evidence_lists_and_buffers_are_captured(fixture):
    root, disk = fixture
    source = disk.source(write(root, "homeassistant/selected", b"x" * 100000))
    pair = [b"homeassistant/selected", source]
    selected_rows, copies, metadata, replacements = [pair], [[source, d.Target(b"data/native/recovery", "directory")]], [], []
    mutable_payload = bytearray(controls()[0].payload)
    e = d.Evidence("op", "owner", selected=selected_rows, copies=copies, metadata=metadata,
                   replacements=replacements, controls=[d.Control(d.Target(b"data/native-operations", "directory"), mutable_payload)])
    selected_rows.clear()
    pair[0] = b"different"
    copies[0][1] = b"different"
    mutable_payload[:] = b"{}"
    assert e.selected == ((b"homeassistant/selected", source),)
    assert e.copies == ((source, d.Target(b"data/native/recovery", "directory")),)
    assert json.loads(e.controls[0].payload)["operation_id"] == "op"


@pytest.mark.parametrize("field", ["plan", "disk", "fd", "path", "identity", "active", "last_snapshot", "current"])
def test_guard_authority_properties_are_readonly(fixture, field):
    root, disk = fixture
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
        with disk.guard(p) as admission:
            with pytest.raises(AttributeError):
                setattr(admission, field, None)


def test_admission_cannot_switch_to_smaller_issued_plan(fixture):
    root, disk = fixture
    write(root, "selected", b"a" * 1024**2)
    with operational(disk):
        large = disk.project("scope", disk.snapshot(), request(selected=selected(disk, root, "selected")))
        with disk.guard(large) as admission:
            with pytest.raises(d.Blocked, match="already-held"):
                disk.snapshot()
            with pytest.raises(d.Blocked, match="already-held"):
                disk.project("scope", large and admission.last_snapshot, request())
            with pytest.raises(AttributeError):
                admission.plan = large
            assert admission.revalidate().evidence == large.evidence


def test_same_volume_destination_substitution_revokes_guard(fixture):
    root, disk = fixture
    (root / "homeassistant").mkdir()
    oid = git(root, "hash-object", "-w", "--stdin", input=b"replacement").strip().decode()
    m = disk.source(write(root, "metadata", b"{}"))
    e = request(replacements=((oid, d.Target(b"homeassistant", "directory")),), metadata=((m, d.Target(b"data/native/recovery", "directory")),))
    with operational(disk):
        p = disk.project("apply", disk.snapshot(), e)
        with disk.guard(p) as admission:
            os.rename(root / "homeassistant", root / "old-homeassistant")
            (root / "homeassistant").mkdir()
            with pytest.raises(d.Blocked, match="namespace"):
                admission.revalidate()


def test_planned_directory_receipt_binds_creation_and_later_substitution(fixture):
    root, disk = fixture
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
        with disk.guard(p) as admission:
            (root / "data/native-operations").mkdir()
            with pytest.raises(d.Blocked, match="unacknowledged"):
                admission.revalidate()
            with d._directory(root / "data/native-operations") as created:
                admission.record_directory_creation(b"data/native-operations", created)
            admission.revalidate()
            os.rename(root / "data/native-operations", root / "data/old-operations")
            (root / "data/native-operations").mkdir()
            with pytest.raises(d.Blocked, match="namespace"):
                admission.revalidate()


def test_save_without_bound_ref_destination_stays_unknown(fixture):
    root, disk = fixture
    tree = git(root, "mktree", input=b"").strip().decode()
    oid = git(root, "commit-tree", tree, input=b"fixture\n").strip().decode()
    payload = git(root, "cat-file", "commit", oid)
    with operational(disk):
        p = disk.project("save", disk.snapshot(), request(commit=payload))
    assert not p.supported
    assert "exact-save-ref-and-reflog-route-required" in p.unknown


def save_evidence(root, disk, ref):
    tree = git(root, "mktree", input=b"").strip().decode()
    oid = git(root, "commit-tree", tree, input=b"fixture\n").strip()
    entry = b"0" * len(oid) + b" " + oid + b" Fixture <fixture@example.invalid> 100 +0000\tSave fixture\n"
    return request(commit=git(root, "cat-file", "commit", oid.decode()), ref=ref, reflog_entry=entry), oid


def test_save_deep_ref_bound_parents_inodes_and_real_git_outputs(fixture):
    root, disk = fixture
    ref = b"refs/heads/" + b"/".join(b"level" + str(i).encode() for i in range(60)) + b"/candidate"
    e, oid = save_evidence(root, disk, ref)
    with operational(disk):
        p = disk.project("save", disk.snapshot(), e)
        assert p.supported, p.unknown
        before = {os.fsencode(path) for path in (root / "data/native/repository.git").rglob("*")}
        git(root, "update-ref", "--create-reflog", ref.decode(), oid.decode())
        after = {os.fsencode(path) for path in (root / "data/native/repository.git").rglob("*")}
        assert sum(x.required_inodes for x in p.decisions) >= len(after - before)
        assert len(p.future_directories) >= 120
        print("Save 60-level real ref/reflog new entries", len(after - before),
              "projected inodes", sum(x.required_inodes for x in p.decisions))


def test_existing_ref_reflog_and_apply_filename_are_usable(fixture):
    root, disk = fixture
    e, oid = save_evidence(root, disk, b"refs/heads/candidate")
    git(root, "update-ref", "--create-reflog", "refs/heads/candidate", oid.decode())
    existing_log = (root / "data/native/repository.git/logs/refs/heads/candidate").stat().st_size
    target = write(root, "homeassistant/filename", b"before")
    metadata = disk.source(write(root, "metadata", b"{}"))
    blob = git(root, "hash-object", "-w", "--stdin", input=b"replace").strip().decode()
    with operational(disk):
        save = disk.project("save", disk.snapshot(), e)
        assert save.supported, save.unknown
        assert next(a.logical for a in save.allocations if a.output == "full-reflog-copy-append") == existing_log + len(e.reflog_entry)
        with disk.guard(save):
            pass
        apply = disk.project("apply", disk.snapshot(), request(replacements=((blob, b"homeassistant/filename"),), metadata=((metadata, d.Target(b"data/native/recovery", "directory")),)))
        assert apply.supported, apply.unknown
        with disk.guard(apply):
            pass


def test_malformed_operation_graph_cannot_establish_admission(fixture):
    import native_operations
    root, disk = fixture
    store = native_operations.NativeOperationStore(root / "data")
    state = store.read()
    state.update(schema=999, active=None, workers={"foreign": {"owner": "another", "phase": "effect_started"}})
    (root / "data/native-operations/state.json").write_bytes(native_operations.canonical(state))
    with operational(disk):
        plan = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="unsupported schema"):
            with disk.guard(plan):
                pass


def test_guard_rejects_corrupt_operation_graph_with_hidden_unresolved_worker(fixture):
    import native_operations as native
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root / "data")
    worker = claim(store, seed(store))
    state = store.read()
    assert state["workers"][worker["id"]]["phase"] == "accepted"
    state["active"] = None
    (root / "data/native-operations/state.json").write_bytes(native.canonical(state))
    with pytest.raises(native.InvalidState, match="unresolved"):
        store.read()
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="unresolved"):
            with disk.guard(p):
                pass


@pytest.mark.parametrize("kind", ["git", "operation"])
def test_existing_executor_critical_section_cannot_overlap_guard(fixture, kind):
    import native_git
    import native_operations
    root, disk = fixture
    if kind == "git":
        adapter = native_git.NativeGit(root)
        context = adapter._journal(create=True)
    else:
        adapter = native_operations.NativeOperationStore(root / "data")
        adapter.read()
        context = adapter._locked()
    with context as value:
        if kind == "git":
            adapter._publish(value)
        with operational(disk):
            plan = disk.project("scope", disk.snapshot(), request())
            with pytest.raises(d.Blocked, match="critical-section"):
                with disk.guard(plan):
                    pass


def test_existing_valid_idle_state_is_observational_and_usable(fixture):
    import native_operations
    root, disk = fixture
    store = native_operations.NativeOperationStore(root / "data")
    store.read()
    before = (root / "data/native-operations/state.json").read_bytes()
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
        with disk.guard(p):
            pass
    assert before == (root / "data/native-operations/state.json").read_bytes()
    os.chmod(root / "data/native-operations/artifacts", 0o755)
    with operational(disk):
        p = disk.project("scope", disk.snapshot(), request())
        with pytest.raises(d.Blocked, match="private-state-directories"):
            with disk.guard(p):
                pass


def test_regular_leaf_other_device_is_not_parent_volume(fixture, monkeypatch):
    from types import SimpleNamespace
    root, disk = fixture
    write(root, "data/native/leaf", b"x" * 4096)
    original = os.stat
    def foreign_stat(name, *args, **kwargs):
        value = original(name, *args, **kwargs)
        if os.fsencode(name) == b"leaf":
            values = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
            values["st_dev"] = value.st_dev + 1
            return SimpleNamespace(**values)
        return value
    monkeypatch.setattr(d.os, "stat", foreign_stat)
    report = disk.snapshot()
    assert not report.complete
    assert any("leaf" in issue for issue in report.issues)


def output_case(root, disk, route):
    target = b'root-control-output' if route == 'root-control' else b'data/native/' + route.encode() + b'-output'
    payload = b'exact replacement\n'
    if route in ('ref', 'reflog', 'ref-lock', 'reflog-lock'):
        (root / 'data/native/repository.git/logs/refs/heads').mkdir(parents=True)
        evidence, oid = save_evidence(root, disk, b'refs/heads/candidate')
        ref = b'data/native/repository.git/refs/heads/candidate'
        log = b'data/native/repository.git/logs/refs/heads/candidate'
        target = log if route.startswith('reflog') else ref
        if route.endswith('-lock'):
            target += b'.lock'
        return 'save', evidence, target, evidence.reflog_entry if route.startswith('reflog') else oid + b'\n'
    if route in ('control', 'root-control'):
        payload = json.dumps({'operation_id': 'op', 'owner_id': 'owner'}).encode()
        evidence = d.Evidence('op', 'owner', controls=(d.Control(target, payload),))
    elif route == 'apply':
        blob = git(root, 'hash-object', '-w', '--stdin', input=payload).strip().decode()
        metadata = disk.source(write(root, 'metadata-source', b'{}'))
        evidence = request(replacements=((blob, target),), metadata=((metadata, d.Target(b'data/native/recovery', 'directory')),))
        return 'apply', evidence, target, payload
    else:
        source = disk.source(write(root, 'copy-source', payload))
        evidence = request(**{route: ((source, target),)})
    return 'scope', evidence, target, payload


@pytest.mark.parametrize('route', ['control', 'root-control', 'copies', 'metadata', 'apply', 'ref', 'reflog', 'ref-lock', 'reflog-lock'])
def test_every_exact_output_binds_absence_and_requires_creation_receipt(fixture, route):
    root, disk = fixture
    kind, evidence, target, payload = output_case(root, disk, route)
    with operational(disk):
        plan = disk.project(kind, disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        absolute = os.fsencode(root) + b'/' + target
        assert dict(plan.namespace)[absolute] is None
        assert absolute not in plan.future_directories
        with disk.guard(plan) as admission:
            needed = sum(x.additional_peak for x in admission.current.decisions)
            fd = os.open(absolute, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, payload)
                with pytest.raises(d.Blocked, match='unacknowledged'):
                    admission.revalidate()
                admission.record_file_creation(target, fd)
                admission.revalidate()
                assert sum(x.additional_peak for x in admission.current.decisions) >= needed
                os.rename(absolute, absolute + b'.old')
                write(root, os.fsdecode(target), payload)
                with pytest.raises(d.Blocked, match='namespace'):
                    admission.revalidate()
            finally:
                os.close(fd)


def test_typed_file_with_missing_parent_receipts_keeps_original_demand(fixture):
    root, disk = fixture
    source = disk.source(write(root, 'source', b'copy'))
    target = b'data/native/new/leaf'
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(copies=((source, target),)))
        with disk.guard(plan) as admission:
            needed = sum(x.additional_peak for x in admission.current.decisions)
            (root / 'data/native/new').mkdir()
            with d._directory(root / 'data/native/new') as fd:
                admission.record_directory_creation(b'data/native/new', fd)
            admission.revalidate()
            fd = os.open(root / os.fsdecode(target), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, b'copy')
                admission.record_file_creation(target, fd)
                admission.revalidate()
            finally:
                os.close(fd)
            assert sum(x.additional_peak for x in admission.current.decisions) >= needed
            assert sum(x.required_inodes for x in admission.current.decisions) >= sum(x.required_inodes for x in plan.decisions)


@pytest.mark.parametrize('fault', ['wrong-content', 'wrong-fd', 'wrong-type', 'unplanned', 'post-receipt-change'])
def test_file_receipt_requires_exact_factual_planned_output(fixture, fault):
    root, disk = fixture
    kind, evidence, target, payload = output_case(root, disk, 'control')
    with operational(disk):
        plan = disk.project(kind, disk.snapshot(), evidence)
        with disk.guard(plan) as admission:
            absolute = root / os.fsdecode(target)
            if fault == 'wrong-type':
                absolute.mkdir()
                fd = os.open(absolute, d._DIR_FLAGS)
            else:
                fd = os.open(absolute, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b'wrong' if fault == 'wrong-content' else payload)
            try:
                if fault == 'wrong-fd':
                    other = os.open(write(root, 'unrelated', payload), os.O_RDONLY)
                    try:
                        with pytest.raises(d.Blocked):
                            admission.record_file_creation(target, other)
                    finally:
                        os.close(other)
                elif fault == 'post-receipt-change':
                    admission.record_file_creation(target, fd)
                    os.pwrite(fd, b'x', 0)
                    before = admission._created_files[os.fsencode(absolute)]
                    os.utime(absolute, ns=(before[5] + 1, before[5] + 1))
                    with pytest.raises(d.Blocked, match='created-file'):
                        admission.revalidate()
                else:
                    with pytest.raises(d.Blocked):
                        admission.record_file_creation(b'data/native/unplanned' if fault == 'unplanned' else target, fd)
            finally:
                os.close(fd)


@pytest.mark.parametrize('field', ['schema', 'root', 'metadata', 'storage', 'record', 'record-id', 'entry', 'admin'])
def test_both_observers_reject_canonically_corrupt_resource_journal(fixture, field):
    import native_git
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    with adapter._journal(create=True) as journal:
        if field in ('schema', 'root', 'metadata', 'storage'):
            journal[field] = 999 if field == 'schema' else [0, 0, 0]
        else:
            rid = '11111111-1111-4111-8111-111111111111'
            record = {'operation': rid, 'owner': rid, 'kind': 'index', 'phase': 'allocated',
                      'root': None, 'lease': None, 'entries': {}, 'admin': None, 'errors': []}
            if field == 'record':
                record['phase'] = 'unrecognized'
            elif field == 'record-id':
                rid = 'bad-id'
            elif field == 'entry':
                record['entries'] = {'not base64!': []}
            else:
                record['admin'] = {'name': 'bad'}
            journal['records'][rid] = record
        adapter._publish(journal)
    with pytest.raises(native_git.InvalidInput):
        with adapter._journal():
            pass
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
        assert not plan.supported
        with pytest.raises(d.Blocked, match='admission'):
            with disk.guard(plan):
                pass


def historical_artifact(root):
    from test_native_operations import NamespacedStore, identity
    store = NamespacedStore(root / 'data')
    store.preview(store.read()['revision'], 'save', identity(), b'old unrelated diff\n' * 65536)
    return next((root / 'data/native-operations/artifacts').glob('*.diff'))


@pytest.mark.parametrize('change', ['unchanged', 'content', 'inode', 'parent', 'mode'])
def test_guard_artifact_cache_is_local_bound_and_invalidates_changes(fixture, monkeypatch, change):
    root, disk = fixture
    artifact = historical_artifact(root)
    original_read = os.read
    reads = []
    def observed_read(fd, size):
        chunk = original_read(fd, size)
        if os.fstat(fd).st_size == artifact.stat().st_size:
            reads.append(len(chunk))
        return chunk
    with operational(disk):
        monkeypatch.setattr(os, 'read', observed_read)
        plan = disk.project('scope', disk.snapshot(), request())
        with disk.guard(plan) as admission:
            initial = sum(reads)
            assert initial == artifact.stat().st_size
            admission.revalidate()
            admission.revalidate()
            assert sum(reads) == initial
            if change == 'content':
                with artifact.open('r+b') as stream:
                    stream.write(b'X')
                before = artifact.stat()
                os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
            elif change == 'inode':
                payload = artifact.read_bytes()
                artifact.unlink()
                artifact.write_bytes(payload)
                artifact.chmod(0o600)
            elif change == 'parent':
                old = artifact.parent.with_name('old-artifacts')
                artifact.parent.rename(old)
                artifact.parent.mkdir(mode=0o700)
                artifact.write_bytes((old / artifact.name).read_bytes())
                artifact.chmod(0o600)
            elif change == 'mode':
                artifact.chmod(0o644)
            if change in ('content', 'mode', 'parent'):
                with pytest.raises(d.Blocked):
                    admission.revalidate()
            else:
                admission.revalidate()
                assert sum(reads) == initial if change == 'unchanged' else sum(reads) > initial
        if change == 'unchanged':
            with pytest.raises(d.Blocked, match='used-or-ended'):
                with disk.guard(plan):
                    pass
            assert sum(reads) == initial


def test_artifact_initial_verification_obeys_deadline_without_size_cap(fixture, monkeypatch):
    root, disk = fixture
    artifact = historical_artifact(root)
    original_read, reads = os.read, []
    def slow_read(fd, size):
        chunk = original_read(fd, size)
        if os.fstat(fd).st_ino == artifact.stat().st_ino:
            reads.append(len(chunk))
            time.sleep(.005)
        return chunk
    disk.limits = d.Limits(seconds=.001)
    monkeypatch.setattr(os, 'read', slow_read)
    with pytest.raises(d.Blocked, match='deadline'):
        disk._competitors(request())
    assert 0 < sum(reads) < artifact.stat().st_size


@pytest.mark.parametrize('field,value', [('seconds', 0), ('seconds', -1), ('seconds', True), ('seconds', float('nan')),
    ('seconds', float('inf')), ('entries', 0), ('entries', True), ('control_bytes', -1), ('control_bytes', 1.5),
    ('git_bytes', False), ('git_bytes', 0)])
def test_invalid_work_limits_cannot_disable_budget(field, value):
    with pytest.raises(d.Blocked):
        d.Limits(**{field: value})


def test_existing_reflog_atomic_copy_receipt_has_exact_append_proof(fixture):
    root, disk = fixture
    evidence, oid = save_evidence(root, disk, b'refs/heads/candidate')
    git(root, 'update-ref', '--create-reflog', 'refs/heads/candidate', oid.decode())
    target = b'data/native/repository.git/logs/refs/heads/candidate.lock'
    payload = (root / 'data/native/repository.git/logs/refs/heads/candidate').read_bytes() + evidence.reflog_entry
    with operational(disk):
        plan = disk.project('save', disk.snapshot(), evidence)
        with disk.guard(plan) as admission:
            fd = os.open(root / os.fsdecode(target), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
                admission.record_file_creation(target, fd)
                admission.revalidate()
            finally:
                os.close(fd)


STRICT_CONTROL_BYTES = [
    b'{"operation_id":"op","owner_id":"owner","owner_id":"owner"}',
    b'{"operation_id":"foreign","operation_id":"op","owner_id":"owner"}',
    b'{"operation_id":"op","owner_id":"owner","nested":{"a":1,"a":1}}',
    b'{"operation_id":"op","owner_id":"owner","nested":{"a":2,"a":1}}',
    b'{"operation_id":"op","owner_id":"owner","nested":{"a":2,"\\u0061":1}}',
    b'{"operation_id":"op","owner_id":"owner","nested":[{"a":1,"a":1}]}',
    b'{"operation_id":"op","owner_id":"owner","nested":NaN}',
    b'{"operation_id":"op","owner_id":"owner","nested":Infinity}',
    b'{"operation_id":"op","owner_id":"owner","nested":-Infinity}',
    b'{"operation_id":"op","owner_id":"owner","nested":"\xff"}',
    b'{"operation_id":"op","owner_id":"owner",',
]


@pytest.mark.parametrize('payload', STRICT_CONTROL_BYTES)
def test_strict_control_bytes_match_both_owner_parsers_and_prospective_refusal(fixture, payload):
    import native_git
    import native_operations
    root, disk = fixture
    for parser in (native_git._json, native_operations._parse):
        with pytest.raises(ValueError):
            parser(payload)
    path = write(root, 'pending-bytes.json', payload)
    with pytest.raises(d.Blocked, match='control-json'):
        disk._control(path)
    evidence = d.Evidence('op', 'owner', controls=(d.Control(b'data/native/pending.json', payload),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert not plan.supported
        with pytest.raises(d.Blocked):
            with disk.guard(plan):
                pass


def test_prospective_exponent_overflow_cannot_supply_nonfinite_control(fixture):
    root, disk = fixture
    payload = b'{"operation_id":"op","owner_id":"owner","nested":1e999}'
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence('op', 'owner', controls=(d.Control(b'data/native/pending.json', payload),)))
    assert not plan.supported
    assert any('nonfinite' in error for error in plan.unknown)


@pytest.mark.parametrize('owner', ['git', 'operation'])
@pytest.mark.parametrize('duplicate', ['identical', 'conflicting', 'escaped', 'nested-identical', 'nested-conflicting', 'nested-escaped'])
def test_persisted_complete_owner_rejects_duplicate_bytes_before_dict_validation(fixture, owner, duplicate):
    from contextlib import ExitStack
    import native_git
    import native_operations
    from test_native_operations import NamespacedStore, seed, claim
    import uuid
    root, disk = fixture
    with ExitStack() as stack:
        if owner == 'git':
            adapter = native_git.NativeGit(root)
            operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
            stack.enter_context(adapter.isolated_index(None, operation_id=operation, owner_id=identity))
            path = root / 'data/native/git-resources/journal.json'
            reject = lambda: adapter.resources()
        else:
            adapter = NamespacedStore(root / 'data')
            worker = claim(adapter, seed(adapter))
            operation, identity = worker['id'], worker['owner']
            path = root / 'data/native-operations/state.json'
            reject = lambda: adapter.read()
        original = path.read_bytes()
        if duplicate.startswith('nested'):
            key = b'phase'
            value = b'"ready"' if owner == 'git' else b'"accepted"'
        else:
            key, value = b'schema', b'1' if owner == 'git' else b'2'
        marker = b'"' + key + b'":' + value
        alias = b'sch\\u0065ma' if key == b'schema' else b'ph\\u0061se'
        first = marker if duplicate.endswith('identical') else b'"' + (alias if duplicate.endswith('escaped') else key) + b'":999'
        malformed = original.replace(marker, first + b',' + marker, 1)
        assert malformed != original
        path.write_bytes(malformed)
        with pytest.raises(ValueError):
            reject()
        with operational(disk):
            plan = disk.project('scope', disk.snapshot(), d.Evidence(operation, identity, controls=controls(operation, identity)))
            with pytest.raises(d.Blocked, match='control-json'):
                with disk.guard(plan):
                    pass
        path.write_bytes(original)  # Owner cleanup must see its valid original bytes.


@pytest.mark.parametrize('phase', ['idle', 'own', 'foreign'])
def test_exact_canonical_operation_envelope_keeps_bytes_and_binds_owner(fixture, phase):
    import native_operations as native
    from test_native_operations import NamespacedStore, seed, claim
    import uuid
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    if phase == 'idle':
        store.read()
        operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
    else:
        worker = claim(store, seed(store))
        operation, identity = worker['id'], worker['owner']
    state = store.read()
    exact = native.canonical(state)
    if phase == 'foreign':
        operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = d.Evidence(operation, identity, controls=(d.Control(b'data/native-operations/state.json', exact, operation, identity),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        if phase == 'foreign':
            assert not plan.supported
            with pytest.raises(d.Blocked):
                with disk.guard(plan):
                    pass
        else:
            assert plan.supported, plan.unknown
            assert next(a.logical for a in plan.allocations if a.output == 'full-pending-control') == len(exact)
            assert plan.evidence.controls[0].payload == exact
            with disk.guard(plan) as admission:
                admission.revalidate()
    assert (root / 'data/native-operations/state.json').read_bytes() == exact


def test_exact_canonical_active_operation_envelope_can_derive_identity(fixture):
    import native_operations as native
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    worker = claim(store, seed(store))
    exact = native.canonical(store.read())
    evidence = d.Evidence(worker['id'], worker['owner'], controls=(d.Control(b'data/native-operations/state.json', exact),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        with disk.guard(plan) as admission:
            admission.revalidate()


@pytest.mark.parametrize('records', ['idle', 'own', 'foreign'])
def test_exact_canonical_resource_journal_envelope_keeps_bytes_and_bound_metadata(fixture, records):
    from contextlib import ExitStack
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
    with ExitStack() as stack:
        if records == 'idle':
            with adapter._journal(create=True) as journal:
                adapter._publish(journal)
        else:
            stack.enter_context(adapter.isolated_index(None, operation_id=operation, owner_id=identity))
        path = root / 'data/native/git-resources/journal.json'
        exact = path.read_bytes()
        if records == 'foreign':
            operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
        evidence = d.Evidence(operation, identity, controls=(d.Control(b'data/native/git-resources/journal.json', exact, operation, identity),))
        with operational(disk):
            plan = disk.project('scope', disk.snapshot(), evidence)
            assert plan.supported, plan.unknown
            assert next(a.logical for a in plan.allocations if a.output == 'full-pending-control') == len(exact)
            assert plan.evidence.controls[0].payload == exact
            if records == 'foreign':
                with pytest.raises(d.Blocked, match='competing'):
                    with disk.guard(plan):
                        pass
            else:
                with disk.guard(plan) as admission:
                    admission.revalidate()
        assert path.read_bytes() == exact


def test_exact_canonical_control_does_not_rehash_unchanged_history_at_phases(fixture, monkeypatch):
    import native_operations as native
    import uuid
    root, disk = fixture
    artifact = historical_artifact(root)
    original_read, reads = os.read, []
    def read(fd, size):
        data = original_read(fd, size)
        if os.fstat(fd).st_ino == artifact.stat().st_ino:
            reads.append(len(data))
        return data
    state = native._parse((root / 'data/native-operations/state.json').read_bytes())
    operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = d.Evidence(operation, identity, controls=(d.Control(b'data/native-operations/state.json', native.canonical(state), operation, identity),))
    with operational(disk):
        monkeypatch.setattr(os, 'read', read)
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        with disk.guard(plan) as admission:
            initial = sum(reads)
            assert initial == artifact.stat().st_size
            admission.revalidate()
            admission.revalidate()
            assert sum(reads) == initial


@pytest.mark.parametrize('identity', [(None, 'owner'), ('op', None), (['op'], 'owner'), ('op', ['owner']), ('', 'owner')])
def test_control_binding_metadata_is_complete_immutable_text(identity):
    with pytest.raises(d.Blocked):
        d.Control(b'data/native/pending.json', b'{}', *identity)


@pytest.mark.parametrize('mismatch', ['declared', 'embedded'])
def test_control_identity_metadata_cannot_override_foreign_binding(fixture, mismatch):
    root, disk = fixture
    embedded = 'foreign' if mismatch == 'embedded' else 'op'
    declared = 'foreign' if mismatch == 'declared' else 'op'
    payload = json.dumps({'operation_id': embedded, 'owner_id': 'owner'}).encode()
    evidence = d.Evidence('op', 'owner', controls=(d.Control(b'data/native/pending.json', payload, declared, 'owner'),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
    assert not plan.supported
    assert 'control-operation-binding-required' in plan.unknown


@pytest.mark.parametrize('owner', ['operation', 'git'])
def test_exact_prospective_owner_format_keeps_canonical_schema_validation(fixture, owner):
    import native_operations
    import native_git
    import uuid
    root, disk = fixture
    operation, identity = str(uuid.uuid4()), str(uuid.uuid4())
    if owner == 'operation':
        value = native_operations.NativeOperationStore(root / 'data').read()
        target = b'data/native-operations/state.json'
    else:
        adapter = native_git.NativeGit(root)
        with adapter._journal(create=True) as value:
            adapter._publish(value)
        target = b'data/native/git-resources/journal.json'
    value['schema'] = 999
    evidence = d.Evidence(operation, identity, controls=(d.Control(target, native_operations.canonical(value), operation, identity),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
    assert not plan.supported
    assert any('schema' in error or 'journal' in error for error in plan.unknown)


def forbid_full_phase_observations(disk, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('phase attempted full accounting, capacity or Git enumeration')
    for name in ('snapshot', 'project', '_scan', '_entries', '_objects', '_volume', '_destination', '_profile', '_git'):
        monkeypatch.setattr(disk, name, forbidden)
    monkeypatch.setattr(d.os, 'fstatvfs', forbidden)


def test_one_initial_forecast_survives_phases_time_and_actual_own_index(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with operational(disk):
        snapshot = disk.snapshot()
        plan = disk.project('scope', snapshot, d.Evidence(op, owner, controls=controls(op, owner)))
        context = disk._contexts[id(plan)]
        assert context.snapshot is snapshot
        initial_clock = time.monotonic
        adapter = native_git.NativeGit(root)
        forbid_full_phase_observations(disk, monkeypatch)
        monkeypatch.setattr(d.time, 'monotonic', lambda: initial_clock() + 1000)
        with disk.guard(plan) as admission:
            assert admission.last_snapshot is snapshot
            assert admission.revalidate() is plan
            with adapter.isolated_index(None, operation_id=op, owner_id=owner) as handle:
                with d._directory(root / 'data/native/git-resources') as fd:
                    admission.record_directory_creation(b'data/native/git-resources', fd)
                admission.revalidate()
                adapter.update_index(handle)
                assert admission.revalidate() is plan
            assert admission.revalidate().decisions == plan.decisions
        with pytest.raises(d.Blocked, match='used-or-ended'):
            with disk.guard(plan):
                pass


@pytest.mark.parametrize('change', ['available', 'inodes', 'digest', 'cross-instance'])
def test_reconstructed_snapshot_cannot_issue_authority(fixture, monkeypatch, change):
    root, disk = fixture
    with operational(disk):
        snapshot = disk.snapshot()
    if change == 'available':
        snapshot = replace(snapshot, volumes=tuple(replace(v, available=10**30) for v in snapshot.volumes))
    elif change == 'inodes':
        snapshot = replace(snapshot, volumes=tuple(replace(v, inodes=10**30) for v in snapshot.volumes))
    elif change == 'digest':
        snapshot = replace(snapshot, digest='0' * 64)
    else:
        disk = d.NativeDisk(root)
    monkeypatch.setattr(disk, '_git', lambda *args: pytest.fail('unissued snapshot launched Git'))
    with pytest.raises(d.Blocked, match='unissued'):
        disk.project('scope', snapshot, request())


def test_unreferenced_diagnostic_contexts_are_collected(fixture):
    import gc
    root, disk = fixture
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
    key = id(plan)
    assert key in disk._contexts
    del plan
    gc.collect()
    assert key not in disk._contexts and key not in disk._plan_refs


@pytest.mark.parametrize('change', ['config', 'head', 'loose-existing', 'loose-appears', 'packed', 'alternates', 'object-disappears'])
def test_targeted_git_dependencies_refuse_changes_without_whole_git_reads(fixture, monkeypatch, change):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    metadata = root / 'data/native/repository.git'
    if change != 'loose-appears':
        git(root, 'update-ref', 'refs/heads/fixture', oid)
    if change == 'packed':
        git(root, 'pack-refs', '--all')
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=oid))
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            if change == 'config':
                path = metadata / 'config'
                path.write_bytes(path.read_bytes() + b'\n[include]\npath = outside\n')
            elif change == 'head':
                (metadata / 'HEAD').write_bytes(b'ref: refs/heads/another\n')
            elif change == 'loose-existing':
                (metadata / 'refs/heads/fixture').write_bytes(b'0' * 40 + b'\n')
            elif change == 'loose-appears':
                write(root, 'data/native/repository.git/refs/heads/fixture', oid.encode() + b'\n')
            elif change == 'packed':
                path = metadata / 'packed-refs'
                path.write_bytes(path.read_bytes() + b'\n')
            elif change == 'alternates':
                write(root, 'data/native/repository.git/objects/info/alternates', b'/elsewhere\n')
            else:
                (metadata / 'objects' / oid[:2] / oid[2:]).unlink()
            with pytest.raises(d.Blocked, match='git-dependency'):
                admission.revalidate()


@pytest.mark.parametrize('kind', ['index', 'spool', 'worktree'])
@pytest.mark.parametrize('mutation', ['unchanged', 'newline', 'whitespace', 'reordered', 'ack-noncanonical', 'digest', 'inode', 'parent'])
def test_actual_marker_bytes_match_native_git_owner_for_all_resources(fixture, kind, mutation):
    from contextlib import ExitStack
    import hashlib
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    operation, owner = str(uuid.uuid4()), str(uuid.uuid4())
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'empty\n').strip().decode()
    with ExitStack() as stack:
        if kind == 'index':
            handle = stack.enter_context(adapter.isolated_index(None, operation_id=operation, owner_id=owner))
        elif kind == 'spool':
            handle = stack.enter_context(adapter._resource('spool', operation, owner))
        else:
            handle = stack.enter_context(adapter.temporary_worktree(oid, operation_id=operation, owner_id=owner))
        resource = root / 'data/native/git-resources' / handle.resource_id
        path = resource / 'owner.json'
        original = path.read_bytes()
        journal_path = resource.parent / 'journal.json'
        journal_bytes = journal_path.read_bytes()
        old_resource = resource.with_name(resource.name + '-held')
        try:
            if mutation == 'newline':
                path.write_bytes(original + b'\n')
            elif mutation == 'whitespace':
                path.write_bytes(original.replace(b': ', b':  ', 1))
            elif mutation == 'reordered':
                path.write_bytes(json.dumps(dict(reversed(list(json.loads(original).items())))).encode())
            elif mutation == 'ack-noncanonical':
                path.write_bytes(original + b'\n')
                value = json.loads(journal_bytes)
                value['records'][handle.resource_id]['entries']['b3duZXIuanNvbg=='][3] = hashlib.sha256(path.read_bytes()).hexdigest()
                journal_path.write_bytes(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
            elif mutation == 'digest':
                path.write_bytes(original.replace(operation.encode(), owner.encode()))
            elif mutation == 'inode':
                path.unlink()
                path.write_bytes(original)
                path.chmod(0o600)
            elif mutation == 'parent':
                resource.rename(old_resource)
                resource.mkdir(mode=0o700)
                path.write_bytes(original)
                path.chmod(0o600)
            with adapter._journal() as journal:
                record = journal['records'][handle.resource_id]
                check_owner = adapter._materialization_check_owner if kind == 'worktree' else adapter._check_owner
                if mutation == 'unchanged':
                    check_owner(handle.resource_id, record)
                else:
                    with pytest.raises(native_git.InvalidInput):
                        check_owner(handle.resource_id, record)
            evidence = d.Evidence(operation, owner, controls=controls(operation, owner), base_tree=oid, materialize=kind == 'worktree')
            with operational(disk):
                plan = disk.project('scope', disk.snapshot(), evidence)
                if mutation == 'unchanged':
                    with disk.guard(plan) as admission:
                        admission.revalidate()
                else:
                    with pytest.raises(d.Blocked, match='marker|resource|namespace'):
                        with disk.guard(plan):
                            pass
        finally:
            if mutation == 'parent':
                path.unlink()
                resource.rmdir()
                old_resource.rename(resource)
            path.write_bytes(original)
            # inode replacement deliberately cannot be restored by bytes; bind
            # the exact restored fixture identity so cleanup sees factual proof.
            if mutation == 'inode':
                value = json.loads(journal_bytes)
                value['records'][handle.resource_id]['entries']['b3duZXIuanNvbg=='] = adapter._record_file(os.fsencode(resource), b'owner.json')
                journal_bytes = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            journal_path.write_bytes(journal_bytes)


@pytest.mark.parametrize('transition', ['effect_started', 'recovery_required', 'completed', 'failed', 'same-owner-takeover', 'foreign-takeover'])
def test_canonical_owner_phase_and_lifecycle_fences_initial_context(fixture, monkeypatch, transition):
    import native_operations as native
    from test_native_operations import NamespacedStore, seed, claim, observed
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    worker = claim(store, seed(store))
    evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            state = store.read()
            record = state['records'][worker['record_id']]
            if transition in ('effect_started', 'recovery_required', 'completed'):
                store.checkpoint(state['revision'], worker['id'], worker['epoch'], 'effect_started', observed(record['identity']))
                assert admission.revalidate() is plan
                if transition != 'effect_started':
                    phase = 'recovery_required' if transition == 'recovery_required' else 'completed'
                    exact = dict(record['identity']['effect'], marker=worker['marker']) if phase == 'completed' else None
                    store.checkpoint(store.read()['revision'], worker['id'], worker['epoch'], phase, evidence=exact)
            elif transition == 'failed':
                store.checkpoint(state['revision'], worker['id'], worker['epoch'], 'failed')
            else:
                takeover = store if transition == 'same-owner-takeover' else NamespacedStore(root / 'data')
                takeover.lifecycle(state['revision'], 'updated', {worker['record_id']: observed(record['identity'])})
            if transition == 'effect_started':
                admission.revalidate()
            else:
                with pytest.raises(d.Blocked, match='fenced|normal-effects'):
                    admission.revalidate()


def test_same_operation_new_resources_are_bounded_by_initial_forecast(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, controls=controls(op, owner)))
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            with adapter.isolated_index(None, operation_id=op, owner_id=owner):
                assert admission.revalidate() is plan
                with adapter.isolated_index(None, operation_id=op, owner_id=owner):
                    with pytest.raises(d.Blocked, match='resource-transition'):
                        admission.revalidate()
                assert admission.revalidate() is plan


def test_new_spool_has_exact_declared_length_ceiling(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    source = disk.source(write(root, 'homeassistant/a', b'exact payload'))
    evidence = d.Evidence(op, owner, selected=((b'homeassistant/a', source),), controls=controls(op, owner))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            with adapter._resource('spool', op, owner) as handle:
                resource = Path(os.fsdecode(adapter._resource_path(handle.resource_id)))
                with adapter._capture_after(handle):
                    write(resource, 'spool', b'exact payload')
                    (resource / 'spool').chmod(0o600)
                assert admission.revalidate() is plan
                with adapter._capture_after(handle):
                    (resource / 'spool').write_bytes(b'exact payload' + b'X')
                with pytest.raises(d.Blocked, match='output-exceeds'):
                    admission.revalidate()


def test_new_real_worktree_uses_qualified_controls_without_rescan(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'empty\n').strip().decode()
    evidence = d.Evidence(op, owner, base_tree=oid, materialize=True, controls=controls(op, owner))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        floor = (plan.allocations, plan.reserves, plan.decisions)
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            with adapter.temporary_worktree(oid, operation_id=op, owner_id=owner):
                assert admission.revalidate() is plan
                assert (admission.current.allocations, admission.current.reserves, admission.current.decisions) == floor
            admission.revalidate()


def test_declared_canonical_preclaim_state_can_advance_to_bound_worker(fixture, monkeypatch):
    import native_operations as native
    from test_native_operations import NamespacedStore, seed, claim, observed
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    record = seed(store)
    before = store.read()
    worker = claim(store, record)
    future = store.read()
    manifest = root / 'data/native-operations/state.json'
    manifest.write_bytes(native.canonical(before))
    evidence = d.Evidence(worker['id'], worker['owner'], controls=(d.Control(b'data/native-operations/state.json', native.canonical(future), worker['id'], worker['owner']),))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            manifest.write_bytes(native.canonical(future))
            assert admission.revalidate() is plan
            store.checkpoint(future['revision'], worker['id'], worker['epoch'], 'effect_started', observed(record['identity']))
            assert admission.revalidate() is plan


def test_unqualified_new_worker_cannot_borrow_idle_initial_forecast(fixture):
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    record = seed(store)
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
        with disk.guard(plan) as admission:
            claim(store, record)
            with pytest.raises(d.Blocked, match='competing|unqualified'):
                admission.revalidate()


def test_phase_checks_do_not_revisit_unrelated_loose_history(fixture, monkeypatch):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'relevant\n').strip().decode()
    unrelated = git(root, 'hash-object', '-w', '--stdin', input=b'unreachable unrelated').strip().decode()
    path = os.fsencode(root / 'data/native/repository.git/objects' / unrelated[:2] / unrelated[2:])
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=oid))
        assert path not in dict(disk._contexts[id(plan)].dependencies)
        original_source = disk.source
        def source(candidate):
            if os.fsencode(candidate) == path:
                pytest.fail('phase revisited unrelated loose history')
            return original_source(candidate)
        monkeypatch.setattr(disk, 'source', source)
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            os.unlink(path)
            assert admission.revalidate() is plan


def test_relevant_packed_object_storage_is_bound_without_phase_git_reads(fixture, monkeypatch):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'packed\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', oid)
    git(root, 'repack', '-ad')
    index = next((root / 'data/native/repository.git/objects/pack').glob('*.idx'))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=oid))
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            index.unlink()
            with pytest.raises(d.Blocked, match='git-dependency'):
                admission.revalidate()


def test_real_base_worktree_materialization_is_forecast_and_phase_qualified(fixture, monkeypatch):
    import native_git
    import uuid
    root, disk = fixture
    data = b'complete base materialization' * 100
    blob = git(root, 'hash-object', '-w', '--stdin', input=data).strip()
    subtree = git(root, 'mktree', input=b'100644 blob ' + blob + b'\ta\n').strip()
    tree = git(root, 'mktree', input=b'040000 tree ' + subtree + b'\thomeassistant\n').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, base_tree=oid, materialize=True, controls=controls(op, owner)))
        assert plan.supported, plan.unknown
        assert any(a.output.startswith('base-materialization:') and a.logical == len(data) for a in plan.allocations)
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            with adapter.temporary_worktree(oid, operation_id=op, owner_id=owner, paths=[b'homeassistant/a']):
                assert admission.revalidate() is plan


@pytest.mark.skipif(sys.platform != 'linux', reason='actual Linux volume consumption proof')
def test_linux_user_backups_reduce_capacity_without_attribution(fixture):
    root, disk = fixture
    before = disk.snapshot()
    assert before.complete
    write(root, 'backup/user.tar', os.urandom(1024 * 1024))
    after = disk.snapshot()
    assert after.complete
    assert after.allocated == before.allocated and after.logical == before.logical
    old = {v.key: v.available for v in before.volumes}
    assert all(v.available <= old[v.key] - 1024 * 1024 for v in after.volumes)
    assert not any(b'/backup/' in path for path, _ in after.observations)


@pytest.mark.parametrize('kind', ['directory', 'file'])
def test_targeted_mount_identity_changes_refuse_without_capacity_refresh(fixture, monkeypatch, kind):
    root, disk = fixture
    target = b'data/native/destination'
    if kind == 'directory':
        (root / os.fsdecode(target)).mkdir()
        output = d.Target(target, 'directory')
    else:
        write(root, target, b'preexisting')
        output = d.Target(target)
    source = disk.source(write(root, 'source', b'copy'))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(copies=((source, output),)))
        mount = disk._mount
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            def changed(path, info, mounts):
                key, identity, fs, supported = mount(path, info, mounts)
                if path == disk.root + b'/' + target:
                    identity = identity + ('changed-bind-or-quota-options',)
                return key, identity, fs, supported
            monkeypatch.setattr(disk, '_mount', changed)
            with pytest.raises(d.Blocked, match='mount'):
                admission.revalidate()


@pytest.mark.parametrize('route',['head','save'])
def test_symbolic_alias_target_is_refused_or_bound(fixture,monkeypatch,route):
    root,disk=fixture; meta=root/'data/native/repository.git'
    tree=git(root,'mktree',input=b'').strip().decode()
    oid=git(root,'commit-tree',tree,input=b'base\n').strip().decode()
    git(root,'update-ref','refs/heads/actual',oid)
    alias='fixture' if route=='head' else 'candidate'
    write(root,'data/native/repository.git/refs/heads/'+alias,b'ref: refs/heads/actual\n')
    evidence=request(base_tree=oid) if route=='head' else save_evidence(root,disk,b'refs/heads/candidate')[0]
    with operational(disk):
        p=disk.project('scope' if route=='head' else 'save',disk.snapshot(),evidence)
        if not p.supported:return
        forbid_full_phase_observations(disk,monkeypatch)
        with disk.guard(p) as a:
            (meta/'refs/heads/actual').write_bytes(b'0'*40+b'\n')
            with pytest.raises(d.Blocked):a.revalidate()

@pytest.mark.parametrize('storage',['new-fanout','existing-fanout'])
def test_repack_new_objects_are_refused_without_rescan(fixture,monkeypatch,storage):
    root,disk=fixture; meta=root/'data/native/repository.git'
    seed=git(root,'hash-object','-w','--stdin',input=b'seed').strip().decode()
    if storage=='existing-fanout':
        for n in range(5000):
            payload=(f'new-object-{n}'.encode())*300
            oid=git(root,'hash-object','--stdin',input=payload).strip().decode()
            if oid[:2]==seed[:2]:break
        else:pytest.fail('fixture could not find fanout')
    else:payload=os.urandom(131072)
    with operational(disk):
        p=disk.project('repack',disk.snapshot(),request()); assert p.supported,p.unknown
        forbid_full_phase_observations(disk,monkeypatch)
        with disk.guard(p) as a:
            new=git(root,'hash-object','-w','--stdin',input=payload).strip().decode()
            assert new!=seed
            with pytest.raises(d.Blocked):a.revalidate()

@pytest.mark.parametrize('kind',['apply','scope'])
def test_git_replace_cannot_understate_native_output(fixture,kind):
    import native_git
    root,disk=fixture
    payload=os.urandom(131072)
    oid=git(root,'hash-object','-w','--stdin',input=payload).strip().decode()
    small=git(root,'hash-object','-w','--stdin',input=b'tiny').strip().decode()
    git(root,'replace',oid,small)
    metadata=disk.source(write(root,'metadata',b'{}'))
    if kind=='apply':
        e=request(replacements=((oid,d.Target(b'homeassistant/output','file')),),metadata=((metadata,d.Target(b'data/native/recovery','directory')),))
    else:
        tree=git(root,'mktree',input=b'100644 blob '+oid.encode()+b'\toutput\n').strip().decode()
        e=request(base_tree=tree,materialize=True)
    with operational(disk):
        p=disk.project(kind,disk.snapshot(),e)
        if not p.supported:return
        adapter=native_git.NativeGit(root)
        actual=int(adapter._run(('cat-file','-s',oid)).strip())
        candidates=[a.logical for a in p.allocations if a.output.startswith('replacement-temporary:' if kind=='apply' else 'base-materialization:')]
        assert actual==len(payload)
        assert candidates and max(candidates)>=actual,(candidates,actual,p.unknown)

# Append to ha-ops/tests/test_native_disk.py; uses its existing test helpers.
@pytest.mark.parametrize('route,mutation', [
    ('head', 'edit'), ('head', 'delete'), ('head-depth2', 'edit'), ('explicit', 'edit')])
def test_symbolic_ref_complete_chain_is_bound_or_explicitly_refused(fixture, route, mutation):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    old = git(root, 'commit-tree', tree, input=b'old\n').strip().decode()
    new = git(root, 'commit-tree', tree, input=b'new\n').strip().decode()
    meta = root / 'data/native/repository.git'
    git(root, 'update-ref', 'refs/heads/actual', old)
    target = meta / 'refs/heads/actual'
    if route == 'explicit':
        write(root, 'data/native/repository.git/refs/heads/savealias', b'ref: refs/heads/actual\n')
        evidence, _ = save_evidence(root, disk, b'refs/heads/savealias')
        kind = 'save'
    else:
        write(root, 'data/native/repository.git/refs/heads/fixture', b'ref: refs/heads/actual\n')
        if route == 'head-depth2':
            target.write_bytes(b'ref: refs/heads/final\n')
            git(root, 'update-ref', 'refs/heads/final', old)
            target = meta / 'refs/heads/final'
        evidence, kind = request(base_tree=old), 'scope'
    with operational(disk):
        plan = disk.project(kind, disk.snapshot(), evidence)
        if not plan.supported:
            assert any('symbolic' in x or 'ref' in x for x in plan.unknown)
            return
        with disk.guard(plan) as admission:
            disk._git = mock.Mock(side_effect=AssertionError('phase Git observation'))
            if mutation == 'delete':
                target.unlink()
            else:
                target.write_bytes(new.encode() + b'\n')
            with pytest.raises(d.Blocked, match='ref|dependency'):
                admission.revalidate()

def test_git_replacement_observation_matches_actual_nativegit_executor(fixture):
    import native_git
    root, disk = fixture
    large = git(root, 'hash-object', '-w', '--stdin', input=b'L' * 100000).strip().decode()
    small = git(root, 'hash-object', '-w', '--stdin', input=b's').strip().decode()
    tree = git(root, 'mktree', input=b'100644 blob ' + large.encode() + b'\tbase.txt\n').strip().decode()
    owner = native_git.NativeGit(root)
    git(root, 'replace', large, small)
    actual = int(subprocess.check_output(['git', '--git-dir=' + str(root / 'data/native/repository.git'),
                                         'cat-file', '-s', large], env=owner._environment()))
    assert actual == 100000
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=tree, materialize=True))
        if not plan.supported:
            assert any('replace' in x or 'routing' in x for x in plan.unknown)
            return
        assert [a.logical for a in plan.allocations if a.output == "base-materialization:b'base.txt'"] == [actual]

@pytest.mark.parametrize('kind', ['index', 'spool', 'worktree'])
@pytest.mark.parametrize('mutation', ['journal-mode', 'missing-journal-lock'])
def test_disk_refuses_journal_routes_rejected_by_actual_owner(fixture, kind, mutation):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'empty\n').strip().decode()
    if kind == 'index':
        manager = adapter.isolated_index(None, operation_id=op, owner_id=owner)
    elif kind == 'spool':
        manager = adapter._resource('spool', op, owner)
    else:
        manager = adapter.temporary_worktree(oid, operation_id=op, owner_id=owner)
    with manager:
        directory = root / 'data/native/git-resources'
        journal, lock = directory / 'journal.json', directory / 'journal.lock'
        held = root / 'held-journal-lock'
        with operational(disk):
            evidence = d.Evidence(op, owner, base_tree=oid, materialize=kind == 'worktree', controls=controls(op, owner))
            plan = disk.project('scope', disk.snapshot(), evidence)
            assert plan.supported, plan.unknown
            with disk.guard(plan) as admission:
                try:
                    if mutation == 'journal-mode':
                        journal.chmod(0o644)
                    else:
                        lock.rename(held)
                    check = adapter._materialization_journal if kind == 'worktree' else adapter._journal
                    with pytest.raises(native_git.InvalidInput, match='private-journal|missing-stable-journal-lock'):
                        with check():
                            pass
                    with pytest.raises(d.Blocked, match='journal|lock|resource'):
                        admission.revalidate()
                finally:
                    journal.chmod(0o600)
                    if held.exists():
                        held.rename(lock)

def test_replaced_journal_lock_cannot_hide_actual_owner_critical_section(fixture):
    import native_git
    import uuid
    import fcntl
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with adapter.isolated_index(None, operation_id=op, owner_id=owner):
        with operational(disk):
            plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, controls=controls(op, owner)))
            with disk.guard(plan) as admission:
                lock = root / 'data/native/git-resources/journal.lock'
                held = root / 'held-journal-lock'
                with adapter._journal():
                    lock.rename(held)
                    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    try:
                        probe = os.open(held, os.O_RDONLY)
                        try:
                            with pytest.raises(BlockingIOError):
                                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        finally:
                            os.close(probe)
                        with pytest.raises(d.Blocked, match='journal|lock|resource'):
                            admission.revalidate()
                    finally:
                        lock.unlink()
                        held.rename(lock)


@pytest.mark.parametrize('case', ['head-alias', 'save-alias', 'replace-added', 'replace-existing', 'repack-new-object'])
def test_phase_dependency_closure_indirect_routes_and_object_membership(fixture, monkeypatch, case):
    root, disk = fixture
    meta = root / 'data/native/repository.git'
    tree = git(root, 'mktree', input=b'').strip().decode()
    old = git(root, 'commit-tree', tree, input=b'old\n').strip().decode()
    alternate = git(root, 'commit-tree', tree, input=b'alternate\n').strip().decode()
    blob = git(root, 'hash-object', '-w', '--stdin', input=b'x'*100000).strip()
    other_tree = git(root, 'mktree', input=b'100644 blob '+blob+b'\tlarger\n').strip().decode()
    new = git(root, 'commit-tree', other_tree, input=b'new\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', old)
    evidence = request(base_tree=old, materialize=True)
    if case in ('head-alias', 'save-alias'):
        write(root, 'data/native/repository.git/refs/heads/actual', old.encode()+b'\n')
        alias = 'fixture' if case == 'head-alias' else 'candidate'
        write(root, 'data/native/repository.git/refs/heads/'+alias, b'ref: refs/heads/actual\n')
        if case == 'save-alias':
            evidence, _ = save_evidence(root, disk, b'refs/heads/candidate')
    elif case == 'replace-existing':
        git(root, 'replace', old, new)
    kind = 'save' if case == 'save-alias' else ('repack' if case == 'repack-new-object' else 'scope')
    with operational(disk):
        plan = disk.project(kind, disk.snapshot(), evidence)
        if not plan.supported:
            assert plan.unknown  # Explicit unsupported-route refusal is acceptable.
            return
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            if case in ('head-alias', 'save-alias'):
                (meta/'refs/heads/actual').write_bytes(new.encode()+b'\n')
            elif case == 'replace-added':
                git(root, 'replace', old, new)
            elif case == 'replace-existing':
                (meta/'refs/replace'/old).write_bytes(alternate.encode()+b'\n')
            else:
                git(root, 'hash-object', '-w', '--stdin', input=os.urandom(1000000))
            if case.startswith('replace-'):
                # Both observer and executor now ignore replacements. These
                # routes legitimately do not change the qualified output.
                import native_git
                actual = native_git.NativeGit(root)._run(('ls-tree', '-r', old))
                assert actual == b''
                assert admission.revalidate() is plan
            else:
                with pytest.raises(d.Blocked):
                    admission.revalidate()


@pytest.mark.parametrize('route', ['head', 'save'])
@pytest.mark.parametrize('target', ['loose', 'packed', 'depth2', 'absent', 'cycle'])
def test_symbolic_loose_routes_are_initially_refused_in_owner_profile(fixture, route, target):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/actual', oid)
    if target == 'packed':
        git(root, 'pack-refs', '--all')
    elif target == 'depth2':
        git(root, 'update-ref', 'refs/heads/final', oid)
        write(root, 'data/native/repository.git/refs/heads/actual', b'ref: refs/heads/final\n')
    elif target == 'absent':
        (root/'data/native/repository.git/refs/heads/actual').unlink()
    elif target == 'cycle':
        write(root, 'data/native/repository.git/refs/heads/actual', b'ref: refs/heads/actual\n')
    alias = 'fixture' if route == 'head' else 'candidate'
    write(root, 'data/native/repository.git/refs/heads/'+alias, b'ref: refs/heads/actual\n')
    evidence = request(base_tree=oid) if route == 'head' else save_evidence(root, disk, b'refs/heads/candidate')[0]
    with operational(disk):
        plan = disk.project('scope' if route == 'head' else 'save', disk.snapshot(), evidence)
    assert not plan.supported
    assert any('symbolic' in issue or 'ref' in issue for issue in plan.unknown)
    import native_git
    with pytest.raises(native_git.InvalidInput, match='symbolic-ref-not-supported'):
        native_git.NativeGit(root).read_ref('refs/heads/'+alias)


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('route', ['head', 'save'])
def test_direct_branch_routes_remain_supported_and_bound(fixture, monkeypatch, packed, route):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    name = 'refs/heads/fixture' if route == 'head' else 'refs/heads/candidate'
    git(root, 'update-ref', name, oid)
    if packed:
        git(root, 'pack-refs', '--all')
    evidence = request(base_tree=oid) if route == 'head' else save_evidence(root, disk, name.encode())[0]
    with operational(disk):
        plan = disk.project('scope' if route == 'head' else 'save', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan
            path = root/'data/native/repository.git'/('packed-refs' if packed else name)
            path.write_bytes(path.read_bytes().replace(oid.encode(), b'0'*40))
            with pytest.raises(d.Blocked, match='dependency'):
                admission.revalidate()


@pytest.mark.parametrize('mutation', ['add', 'change', 'remove'])
def test_ignored_replace_routes_preserve_canonical_materialization(fixture, monkeypatch, mutation):
    import native_git
    root, disk = fixture
    blob = git(root, 'hash-object', '-w', '--stdin', input=b'original'*16000).strip().decode()
    tiny = git(root, 'hash-object', '-w', '--stdin', input=b'x').strip().decode()
    other = git(root, 'hash-object', '-w', '--stdin', input=b'other').strip().decode()
    tree = git(root, 'mktree', input=b'100644 blob '+blob.encode()+b'\tbase\n').strip().decode()
    if mutation != 'add':
        git(root, 'replace', blob, tiny)
    owner = native_git.NativeGit(root)
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=tree, materialize=True))
        assert plan.supported, plan.unknown
        allocation = [a.logical for a in plan.allocations if a.output == "base-materialization:b'base'"]
        assert allocation == [128000]
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            if mutation == 'add':
                git(root, 'replace', blob, tiny)
            elif mutation == 'change':
                (root/'data/native/repository.git/refs/replace'/blob).write_bytes(other.encode()+b'\n')
            else:
                git(root, 'replace', '-d', blob)
            assert int(owner._run(('cat-file', '-s', blob))) == 128000
            assert admission.revalidate() is plan
            assert [a.logical for a in plan.allocations if a.output == "base-materialization:b'base'"] == allocation


def test_observer_git_profile_matches_canonical_owner(fixture, monkeypatch):
    import native_git
    root, disk = fixture
    captured = {}
    original = d.subprocess.Popen
    def popen(argv, **kwargs):
        captured.update(argv=argv, env=kwargs['env'])
        return original(argv, **kwargs)
    monkeypatch.setattr(d.subprocess, 'Popen', popen)
    disk._git(['rev-parse', '--is-bare-repository'])
    owner = native_git.NativeGit(root)
    # Constructor also calls Git; capture the observer invocation separately.
    disk._git(['rev-parse', '--is-bare-repository'])
    argv, env = owner._argv(('rev-parse', '--is-bare-repository'))
    assert captured['env'] == env
    assert captured['argv'] == argv


@pytest.mark.parametrize('mutation', ['new-pack', 'new-pack-directory', 'delete-fanout', 'replace-fanout'])
def test_repack_membership_and_pack_routing_are_bound_without_enumeration(fixture, monkeypatch, mutation):
    root, disk = fixture
    oid = git(root, 'hash-object', '-w', '--stdin', input=b'seed').strip().decode()
    objects = root/'data/native/repository.git/objects'
    if mutation == 'new-pack-directory':
        (objects/'pack').rmdir()
    with operational(disk):
        plan = disk.project('repack', disk.snapshot(), request())
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            if mutation == 'new-pack':
                (objects/'pack/new.pack').write_bytes(b'new routed storage')
            elif mutation == 'new-pack-directory':
                (objects/'pack').mkdir()
            else:
                fanout = objects/oid[:2]
                (fanout/oid[2:]).unlink()
                fanout.rmdir()
                if mutation == 'replace-fanout':
                    fanout.mkdir()
            with pytest.raises(d.Blocked, match='membership|dependency|route|destination'):
                admission.revalidate()


@pytest.mark.parametrize('when', ['initial', 'phase'])
@pytest.mark.parametrize('mutation', ['journal-mode', 'journal-hardlink', 'lock-mode', 'lock-hardlink', 'storage-mode'])
def test_canonical_resource_filesystem_predicates_at_initial_and_phase(fixture, when, mutation):
    import native_git
    import uuid
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with adapter.isolated_index(None, operation_id=op, owner_id=owner):
        storage = root/'data/native/git-resources'
        journal, lock = storage/'journal.json', storage/'journal.lock'
        alias = root/'extra-owner-link'
        def change():
            if mutation == 'journal-mode': journal.chmod(0o644)
            elif mutation == 'journal-hardlink': os.link(journal, alias)
            elif mutation == 'lock-mode': lock.chmod(0o644)
            elif mutation == 'lock-hardlink': os.link(lock, alias)
            else: storage.chmod(0o755)
        try:
            with operational(disk):
                if when == 'initial': change()
                plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, controls=controls(op, owner)))
                if when == 'initial':
                    assert not plan.supported
                else:
                    assert plan.supported, plan.unknown
                    with disk.guard(plan) as admission:
                        change()
                        with pytest.raises(d.Blocked): admission.revalidate()
        finally:
            if alias.exists(): alias.unlink()
            journal.chmod(0o600); lock.chmod(0o600); storage.chmod(0o700)


@pytest.mark.parametrize('kind', ['index', 'spool', 'worktree'])
def test_new_resource_lock_receipt_stays_bound_after_canonical_creation(fixture, monkeypatch, kind):
    import native_git
    import uuid
    root, disk = fixture
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    source = disk.source(write(root, 'homeassistant/source', b'selected'))
    adapter = native_git.NativeGit(root)
    evidence = d.Evidence(op, owner, selected=((b'source', source),), base_tree=oid,
                          materialize=kind == 'worktree', controls=controls(op, owner))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            manager = (adapter.isolated_index(None, operation_id=op, owner_id=owner) if kind == 'index'
                       else adapter._resource('spool', op, owner) if kind == 'spool'
                       else adapter.temporary_worktree(oid, operation_id=op, owner_id=owner))
            with manager:
                assert admission.revalidate() is plan
                lock = root/'data/native/git-resources/journal.lock'
                held = root/'held-journal-lock'
                lock.rename(held)
                lock.write_bytes(b''); lock.chmod(0o600)
                try:
                    with pytest.raises(d.Blocked, match='lock'):
                        admission.revalidate()
                finally:
                    lock.unlink(); held.rename(lock)


@pytest.mark.parametrize('mutation', ['mode', 'hardlink'])
@pytest.mark.parametrize('when', ['initial', 'phase'])
def test_persisted_operation_state_requires_actual_owner_file_predicates(fixture, mutation, when):
    import native_operations
    root, disk = fixture
    store = native_operations.NativeOperationStore(root/'data')
    store.read()
    state = root/'data/native-operations/state.json'
    alias = root/'extra-state-link'
    def change():
        if mutation == 'mode': state.chmod(0o644)
        else: os.link(state, alias)
    try:
        with operational(disk):
            if when == 'initial': change()
            plan = disk.project('scope', disk.snapshot(), request())
            if when == 'initial': assert not plan.supported
            else:
                assert plan.supported, plan.unknown
                with disk.guard(plan) as admission:
                    change()
                    with pytest.raises(d.Blocked): admission.revalidate()
    finally:
        if alias.exists(): alias.unlink()
        state.chmod(0o600)


@pytest.mark.parametrize('kind', ['index', 'spool', 'worktree'])
def test_snapshot_lock_identity_cannot_change_during_initial_compilation(fixture, kind):
    import native_git
    import uuid
    root, disk = fixture
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    adapter = native_git.NativeGit(root)
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    manager = (adapter.isolated_index(None, operation_id=op, owner_id=owner) if kind == 'index'
               else adapter._resource('spool', op, owner) if kind == 'spool'
               else adapter.temporary_worktree(oid, operation_id=op, owner_id=owner))
    with manager:
        with operational(disk):
            snapshot = disk.snapshot()
            lock = root/'data/native/git-resources/journal.lock'
            held = root/'held-journal-lock'
            lock.rename(held)
            lock.write_bytes(b''); lock.chmod(0o600)
            try:
                plan = disk.project('scope', snapshot, d.Evidence(op, owner, base_tree=oid,
                                    materialize=kind == 'worktree', controls=controls(op, owner)))
                assert not plan.supported
                assert any('owner-lock' in issue for issue in plan.unknown)
            finally:
                lock.unlink(); held.rename(lock)


def test_new_resource_lock_has_initial_demand_without_reclaim(fixture):
    import native_git
    import uuid
    root, disk = fixture
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, controls=controls(op, owner)))
        assert [a.logical for a in plan.allocations if a.output == 'resource-journal-lock'] == [0]
        floor = plan.allocations, plan.decisions
        with disk.guard(plan) as admission:
            with native_git.NativeGit(root).isolated_index(None, operation_id=op, owner_id=owner):
                assert admission.revalidate() is plan
                assert (plan.allocations, plan.decisions) == floor


def test_first_resource_lock_receipt_has_no_unobserved_descriptor_history(fixture):
    """Disclose the cooperative call-site limit, not a historical-FD proof.

    A future executor must take the first receipt immediately outside the owner
    critical section. A substituted lock before that receipt violates this
    obligation and cannot be distinguished by the unchanged owner schema.
    """
    import native_git
    import uuid
    import fcntl
    root, disk = fixture
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    adapter = native_git.NativeGit(root)
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner, controls=controls(op, owner)))
        with disk.guard(plan) as admission:
            assert admission._owner_locks[b'data/native/git-resources/journal.lock'] is None
            with adapter.isolated_index(None, operation_id=op, owner_id=owner):
                lock = root/'data/native/git-resources/journal.lock'
                held = root/'unobserved-original-lock'
                with adapter._journal():
                    lock.rename(held)
                    lock.write_bytes(b''); lock.chmod(0o600)
                    try:
                        fd = os.open(held, os.O_RDONLY)
                        try:
                            with pytest.raises(BlockingIOError):
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        finally:
                            os.close(fd)
                        # No initial/previous receipt knew the old inode. The
                        # current canonical graph contains no journal.lock ID.
                        assert admission.revalidate() is plan
                        assert admission._owner_locks[b'data/native/git-resources/journal.lock'][1] == lock.stat().st_ino
                    finally:
                        lock.unlink(); held.rename(lock)
                with pytest.raises(d.Blocked, match='lock'):
                    admission.revalidate()


@pytest.mark.parametrize('route', ['head', 'save'])
@pytest.mark.parametrize('invalid', ['wrong-width', 'zero', 'missing-object'])
def test_initial_direct_ref_matches_actual_oid_width_and_local_object(fixture, route, invalid):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    name = 'refs/heads/fixture' if route == 'head' else 'refs/heads/candidate'
    value = b'a'*64 if invalid == 'wrong-width' else b'0'*40 if invalid == 'zero' else b'a'*40
    write(root, 'data/native/repository.git/'+name, value+b'\n')
    evidence = request(base_tree=base) if route == 'head' else save_evidence(root, disk, name.encode())[0]
    with operational(disk):
        plan = disk.project('scope' if route == 'head' else 'save', disk.snapshot(), evidence)
    assert not plan.supported
    import native_git
    with pytest.raises((native_git.InvalidInput, native_git.GitFailure)):
        native_git.NativeGit(root).read_ref(name)


@pytest.mark.parametrize('route', ['head', 'save', 'detached'])
@pytest.mark.parametrize('packed', [False, True])
def test_direct_ref_object_outside_base_is_initially_compiled_and_bound(fixture, monkeypatch, route, packed):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'declared base\n').strip().decode()
    other = git(root, 'commit-tree', tree, input=b'direct ref distinct from base\n').strip().decode()
    name = 'refs/heads/candidate' if route == 'save' else 'refs/heads/fixture'
    git(root, 'update-ref', name, other)
    if route == 'detached':
        write(root, 'data/native/repository.git/HEAD', other.encode()+b'\n')
    if packed:
        git(root, 'repack', '-ad')
    evidence = request(base_tree=base) if route != 'save' else save_evidence(root, disk, name.encode())[0]
    with operational(disk):
        plan = disk.project('scope' if route != 'save' else 'save', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan
            if packed:
                path = next((root/'data/native/repository.git/objects/pack').glob('*.pack'))
            else:
                path = root/'data/native/repository.git/objects'/other[:2]/other[2:]
            path.unlink()
            with pytest.raises(d.Blocked, match='dependency'):
                admission.revalidate()


@pytest.mark.parametrize('fanout', ['new', 'existing'])
def test_scope_allows_unrelated_object_additions_without_phase_scan(fixture, monkeypatch, fanout):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', base)
    for n in range(5000):
        payload = ('unrelated-'+str(n)).encode()
        oid = git(root, 'hash-object', '--stdin', input=payload).strip().decode()
        exists = (root/'data/native/repository.git/objects'/oid[:2]).exists()
        if exists == (fanout == 'existing'):
            break
    else:
        pytest.fail('cannot find requested fanout fixture')
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=base))
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            git(root, 'hash-object', '-w', '--stdin', input=payload)
            assert admission.revalidate() is plan


@pytest.mark.parametrize('route', ['head', 'save'])
@pytest.mark.parametrize('target', ['distinct', 'missing', 'zero', 'malformed'])
def test_relevant_packed_ref_target_is_qualified_and_storage_bound(fixture, monkeypatch, route, target):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'declared base\n').strip().decode()
    other = git(root, 'commit-tree', tree, input=b'packed ref distinct from base\n').strip().decode()
    name = 'refs/heads/fixture' if route == 'head' else 'refs/heads/candidate'
    git(root, 'update-ref', name, other)
    git(root, 'pack-refs', '--all')
    packed = root/'data/native/repository.git/packed-refs'
    if target != 'distinct':
        value = b'a'*40 if target == 'missing' else b'0'*40 if target == 'zero' else b'ref: refs/heads/other'
        packed.write_bytes(packed.read_bytes().replace(other.encode(), value))
    evidence = request(base_tree=base) if route == 'head' else save_evidence(root, disk, name.encode())[0]
    with operational(disk):
        plan = disk.project('scope' if route == 'head' else 'save', disk.snapshot(), evidence)
        if target != 'distinct':
            assert not plan.supported
            return
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan
            path = root/'data/native/repository.git/objects'/other[:2]/other[2:]
            path.unlink()
            with pytest.raises(d.Blocked, match='dependency'):
                admission.revalidate()


@pytest.mark.parametrize('name', [b'refs/heads/bad name', b'refs/heads/bad..name', b'refs/heads/bad\nname', b'refs/heads/bad.lock'])
def test_initial_head_branch_name_requires_git_ref_grammar(fixture, name):
    root, disk = fixture
    write(root, 'data/native/repository.git/HEAD', b'ref: '+name+b'\n')
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
    assert not plan.supported


@pytest.mark.parametrize('route',['head','save'])
@pytest.mark.parametrize('syntax',['tab','missing-separator','trailing-junk'])
def test_malformed_relevant_packed_ref_is_not_treated_as_absent(fixture,monkeypatch,route,syntax):
    import native_git
    root,disk=fixture
    meta=root/'data/native/repository.git'
    tree=git(root,'mktree',input=b'').strip().decode()
    oid=git(root,'commit-tree',tree,input=b'base\n').strip().decode()
    name=b'refs/heads/fixture' if route=='head' else b'refs/heads/candidate'
    git(root,'update-ref',name.decode(),oid)
    git(root,'pack-refs','--all')
    packed=meta/'packed-refs'
    old=oid.encode()+b' '+name+b'\n'
    changed=oid.encode()+b'\t'+name+b'\n' if syntax=='tab' else oid.encode()+name+b'\n' if syntax=='missing-separator' else oid.encode()+b' '+name+b' trailing\n'
    packed.write_bytes(packed.read_bytes().replace(old,changed))
    with pytest.raises((native_git.InvalidInput,native_git.GitFailure)):
        native_git.NativeGit(root).read_ref(name.decode())
    evidence=request(base_tree=oid) if route=='head' else save_evidence(root,disk,name)[0]
    with operational(disk):
        p=disk.project('scope' if route=='head' else 'save',disk.snapshot(),evidence)
        if p.supported:
            forbid_full_phase_observations(disk,monkeypatch)
            with disk.guard(p) as admission:
                assert admission.revalidate() is p
    assert not p.supported,p.unknown

@pytest.mark.parametrize('held',['idle','actual-critical-section'])
def test_known_snapshot_operation_lock_cannot_disappear_before_compilation(fixture,monkeypatch,held):
    import native_operations, fcntl
    from contextlib import nullcontext
    root,disk=fixture
    store=native_operations.NativeOperationStore(root/'data'); store.read()
    lock=root/'data/native-operations/lock'
    old=root/'known-held-operation-lock'
    manager=store._locked() if held=='actual-critical-section' else nullcontext()
    with manager:
        with operational(disk):
            snapshot=disk.snapshot()
            assert os.fsencode(lock) in dict(snapshot.observations)
            lock.rename(old)
            try:
                if held=='actual-critical-section':
                    fd=os.open(old,os.O_RDONLY)
                    try:
                        with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally:os.close(fd)
                p=disk.project('scope',snapshot,request())
                if p.supported:
                    forbid_full_phase_observations(disk,monkeypatch)
                    with disk.guard(p) as admission:
                        assert admission.revalidate() is p
                assert not p.supported,p.unknown
            finally:old.rename(lock)

# Append to ha-ops/tests/test_native_disk.py; uses its existing helpers.


@pytest.mark.parametrize('active', [False, True])
def test_snapshot_observed_operation_lock_loss_still_refuses_held_owner(fixture, active):
    from test_native_operations import NamespacedStore, seed, claim
    import fcntl
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    store.read()
    if active:
        worker = claim(store, seed(store))
        evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
    else:
        evidence = request()
    lock = root / 'data/native-operations/lock'
    held = root / 'snapshot-observed-operation-lock'
    with operational(disk):
        with store._locked():
            snapshot = disk.snapshot()
            assert os.fsencode(lock) in dict(snapshot.observations)
            lock.rename(held)
            try:
                probe = os.open(held, os.O_RDONLY)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                plan = disk.project('scope', snapshot, evidence)
                if plan.supported:
                    with pytest.raises(d.Blocked, match='lock|owner|snapshot'):
                        with disk.guard(plan):
                            pass
                else:
                    assert any('lock' in message for message in plan.unknown)
            finally:
                held.rename(lock)


def test_snapshot_observed_empty_journal_and_lock_loss_refuses_held_owner(fixture):
    import native_git
    import uuid
    import fcntl
    root, disk = fixture
    adapter = native_git.NativeGit(root)
    with adapter.isolated_index(None, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())):
        pass
    directory = root / 'data/native/git-resources'
    lock, journal = directory / 'journal.lock', directory / 'journal.json'
    held_lock, held_journal = root / 'snapshot-observed-journal-lock', root / 'snapshot-observed-journal'
    with operational(disk):
        with adapter._journal():
            snapshot = disk.snapshot()
            assert os.fsencode(lock) in dict(snapshot.observations)
            lock.rename(held_lock)
            journal.rename(held_journal)
            try:
                probe = os.open(held_lock, os.O_RDONLY)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                plan = disk.project('scope', snapshot, request())
                if plan.supported:
                    with pytest.raises(d.Blocked, match='lock|owner|snapshot|journal'):
                        with disk.guard(plan):
                            pass
                else:
                    assert any('lock' in message or 'journal' in message for message in plan.unknown)
            finally:
                held_lock.rename(lock)
                held_journal.rename(journal)


def test_local_initial_io_failure_releases_fence_without_claiming_entry(fixture, monkeypatch):
    import errno
    root, disk = fixture
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
        original = os.open
        def fail_admission(path, *args, **kw):
            if path in (b'disk-admission.lock', 'disk-admission.lock'):
                raise OSError(errno.ENOSPC, 'isolated admission lock allocation failure')
            return original(path, *args, **kw)
        with monkeypatch.context() as patch:
            patch.setattr(os, 'open', fail_admission)
            with pytest.raises(OSError) as error:
                with disk.guard(plan):
                    pass
            assert error.value.errno == errno.ENOSPC
        assert disk._active_operation is None
        assert id(plan) in disk._contexts
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan


def test_missing_source_stops_without_new_forecast_and_ends_context(fixture, monkeypatch):
    root, disk = fixture
    write(root, 'homeassistant/a', b'private fixture source')
    evidence = request(selected=selected(disk, root, 'homeassistant/a'))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        floor = plan.allocations, plan.reserves, plan.decisions
        forbid_full_phase_observations(disk, monkeypatch)
        with pytest.raises((d.Blocked, FileNotFoundError)):
            with disk.guard(plan) as admission:
                (root / 'homeassistant/a').unlink()
                admission.revalidate()
        assert not admission.active
        assert (plan.allocations, plan.reserves, plan.decisions) == floor
        with pytest.raises(d.Blocked, match='ended'):
            admission.revalidate()
        with pytest.raises(d.Blocked, match='used-or-ended'):
            with disk.guard(plan):
                pass


def test_interrupted_guard_keeps_recovery_and_original_demand(fixture):
    import errno
    root, disk = fixture
    recovery = Path(os.fsdecode(write(root, 'data/native/recovery/retained', b'before-write recovery')))
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request())
        floor = plan.allocations, plan.reserves, plan.decisions
        with pytest.raises(OSError) as error:
            with disk.guard(plan) as admission:
                raise OSError(errno.ENOSPC, 'isolated future write failure')
        assert error.value.errno == errno.ENOSPC
        assert recovery.read_bytes() == b'before-write recovery'
        assert (plan.allocations, plan.reserves, plan.decisions) == floor
        assert disk._active_operation is None
        assert not admission.active
        assert admission._artifact_cache == {}
        with pytest.raises(d.Blocked, match='used-or-ended'):
            with disk.guard(plan):
                pass
        fresh = disk.project('scope', disk.snapshot(), request())
        with disk.guard(fresh):
            pass


def malformed_packed_storage(data, oid, name, syntax):
    """Persisted grammar counterexamples independent of observer parsing."""
    row = oid + b' ' + name + b'\n'
    replacement = {
        'tab': oid+b'\t'+name+b'\n',
        'no-separator': oid+name+b'\n',
        'trailing-junk': row[:-1]+b' trailing\n',
        'leading-space': b' '+row,
        'double-space': oid+b'  '+name+b'\n',
        'crlf': row[:-1]+b'\r\n',
        'short-oid': oid[:-1]+b' '+name+b'\n',
        'long-oid': oid+b'a '+name+b'\n',
        'upper-oid': oid.upper()+b' '+name+b'\n',
        'zero-oid': b'0'*len(oid)+b' '+name+b'\n',
        'nonhex-oid': b'g'*len(oid)+b' '+name+b'\n',
        'unqualified-name': oid+b' heads/invalid\n',
        'dot-name': oid+b' refs/heads/bad..name\n',
        'lock-name': oid+b' refs/heads/bad.lock\n',
        'dot-component': oid+b' refs/heads/.bad\n',
        'empty-component': oid+b' refs/heads//bad\n',
        'nul-name': oid+b' refs/heads/bad\0name\n',
        'duplicate-row': row+row,
        'peeled-double': row+b'^'+oid+b'\n^'+oid+b'\n',
        'peeled-short': row+b'^'+oid[:-1]+b'\n',
        'peeled-zero': row+b'^'+b'0'*len(oid)+b'\n',
        'peeled-junk': row+b'^'+oid+b' junk\n',
    }.get(syntax)
    if replacement is not None:
        return data.replace(row, replacement)
    if syntax == 'missing-final-lf': return data[:-1]
    if syntax == 'blank-line': return data+b'\n'
    if syntax == 'duplicate-header': return data.split(b'\n', 1)[0]+b'\n'+data
    if syntax == 'late-header': return data+b'# pack-refs with: sorted\n'
    if syntax == 'unknown-comment': return data+b'# invalid-comment\n'
    if syntax == 'orphan-peeled': return b'^'+oid+b'\n'+data
    if syntax == 'sorted-reversal':
        header, *rows = data.splitlines(keepends=True)
        return header+b''.join(reversed(rows))
    if syntax == 'peeled-missing-lf': return data+b'^'+oid
    raise AssertionError(syntax)


@pytest.mark.parametrize('where', ['relevant', 'irrelevant', 'loose-override'])
@pytest.mark.parametrize('syntax', [
    'tab', 'no-separator', 'trailing-junk', 'leading-space', 'double-space',
    'crlf', 'short-oid', 'long-oid', 'upper-oid', 'zero-oid', 'nonhex-oid',
    'unqualified-name', 'dot-name', 'lock-name', 'dot-component',
    'empty-component', 'nul-name', 'duplicate-row', 'peeled-double',
    'peeled-short', 'peeled-zero', 'peeled-junk', 'missing-final-lf',
    'blank-line', 'duplicate-header', 'late-header', 'unknown-comment',
    'orphan-peeled', 'sorted-reversal', 'peeled-missing-lf'])
def test_entire_packed_grammar_refuses_before_relevant_lookup(fixture, where, syntax):
    import native_git
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'base\n').strip()
    other = git(root, 'commit-tree', tree, input=b'unrelated\n').strip()
    git(root, 'update-ref', 'refs/heads/fixture', base.decode())
    git(root, 'update-ref', 'refs/heads/extra', other.decode())
    git(root, 'pack-refs', '--all')
    packed = root/'data/native/repository.git/packed-refs'
    name, oid = (b'refs/heads/extra', other) if where == 'irrelevant' else (b'refs/heads/fixture', base)
    packed.write_bytes(malformed_packed_storage(packed.read_bytes(), oid, name, syntax))
    if where == 'loose-override':
        write(root, 'data/native/repository.git/refs/heads/fixture', base+b'\n')
    with pytest.raises((native_git.InvalidInput, native_git.GitFailure)):
        native_git.NativeGit(root).read_ref('refs/heads/fixture')
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=base.decode()))
    assert not plan.supported, plan.unknown


@pytest.mark.parametrize('grammar', ['empty', 'header-only', 'no-header', 'unsorted', 'unknown-flags', 'peeled', 'header-sorted', 'repeated-oid'])
def test_valid_packed_structure_preserves_genuine_absence_and_overrides(fixture, monkeypatch, grammar):
    import native_git
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip()
    row = oid+b' refs/heads/fixture\n'
    other = oid+b' refs/heads/unrelated\n'
    data = {
        'empty': b'', 'header-only': b'# pack-refs with: sorted\n',
        'no-header': row, 'unsorted': other+row,
        'unknown-flags': b'# pack-refs with: unknown-feature\n'+other+row,
        'peeled': row+b'^'+oid+b'\n',
        'header-sorted': b'# pack-refs with: sorted\n'+row+other,
        'repeated-oid': row+other,
    }[grammar]
    write(root, 'data/native/repository.git/packed-refs', data)
    write(root, 'data/native/repository.git/refs/heads/fixture', oid+b'\n')
    owner = native_git.NativeGit(root)
    assert owner.read_ref('refs/heads/fixture').oid == oid.decode()
    assert owner.read_ref('refs/heads/actually-absent').oid is None
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=oid.decode()))
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan


@pytest.mark.parametrize('route', ['head', 'save'])
@pytest.mark.parametrize('entry', ['unrelated', 'peeled'])
@pytest.mark.parametrize('override', [False, True])
def test_nonexistent_packed_object_is_not_qualified(fixture, route, entry, override):
    import native_git
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    name = b'refs/heads/fixture' if route == 'head' else b'refs/heads/candidate'
    git(root, 'update-ref', name.decode(), oid)
    git(root, 'pack-refs', '--all')
    packed = root/'data/native/repository.git/packed-refs'
    row = (b'a'*40 if entry == 'unrelated' else oid.encode())+b' refs/heads/unrelated\n'
    if entry == 'peeled': row += b'^'+b'a'*40+b'\n'
    packed.write_bytes(packed.read_bytes()+row)
    if override: write(root, 'data/native/repository.git/'+name.decode(), oid.encode()+b'\n')
    with pytest.raises((native_git.InvalidInput, native_git.GitFailure)):
        native_git.NativeGit(root).read_ref(name.decode())
    evidence = request(base_tree=oid) if route == 'head' else save_evidence(root, disk, name)[0]
    with operational(disk):
        plan = disk.project('scope' if route == 'head' else 'save', disk.snapshot(), evidence)
    assert not plan.supported, plan.unknown


@pytest.mark.parametrize('peeled', [False, True])
def test_packed_object_predicates_are_one_batch_and_remain_bound(fixture, monkeypatch, peeled):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'base\n').strip()
    extra = [git(root, 'hash-object', '-w', '--stdin', input=('object'+str(n)).encode()).strip() for n in range(12)]
    rows = base+b' refs/heads/fixture\n'
    for n, oid in enumerate(extra):
        rows += oid+b' refs/tags/extra-'+str(n).encode()+b'\n'
        if peeled: rows += b'^'+oid+b'\n'
    write(root, 'data/native/repository.git/packed-refs', rows)
    calls, original = [], disk._git
    def capture(args, **kw):
        if args[0] == 'cat-file': calls.append((args, kw.get('input')))
        return original(args, **kw)
    monkeypatch.setattr(disk, '_git', capture)
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=base.decode()))
        assert plan.supported, plan.unknown
        packed_batches = [payload for args, payload in calls if payload is not None and extra[0]+b'\n' in payload]
        assert len(packed_batches) == 1
        assert set(packed_batches[0].splitlines()) == set(extra)
        assert not any(args[:2] == ['cat-file', '--batch-all-objects'] for args, _ in calls)
        assert not any(len(args) >= 3 and args[1] == '-t' and args[2].encode() in extra for args, _ in calls)
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan
            oid = extra[-1].decode()
            (root/'data/native/repository.git/objects'/oid[:2]/oid[2:]).unlink()
            with pytest.raises(d.Blocked, match='dependency'):
                admission.revalidate()


@pytest.mark.parametrize('state', ['operation-idle', 'operation-active', 'journal-empty', 'journal-index', 'journal-spool', 'journal-worktree'])
@pytest.mark.parametrize('stage', ['before-project', 'after-guard'])
@pytest.mark.parametrize('loss', ['lock', 'lock-and-control', 'parent'])
def test_known_owner_lock_loss_is_fenced_through_all_boundaries(fixture, monkeypatch, state, stage, loss):
    import fcntl
    import native_git
    import uuid
    from contextlib import ExitStack
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    with ExitStack() as resources:
        if state.startswith('operation-'):
            owner = NamespacedStore(root/'data'); owner.read()
            if state == 'operation-active':
                worker = claim(owner, seed(owner))
                evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
            else: evidence = request()
            parent = root/'data/native-operations'
            lock, control = parent/'lock', parent/'state.json'
            critical = owner._locked
        else:
            owner = native_git.NativeGit(root)
            op, identity = str(uuid.uuid4()), str(uuid.uuid4())
            if state == 'journal-empty':
                with owner.isolated_index(None, operation_id=op, owner_id=identity): pass
                evidence = request()
            elif state == 'journal-index':
                resources.enter_context(owner.isolated_index(None, operation_id=op, owner_id=identity))
                evidence = d.Evidence(op, identity, controls=controls(op, identity))
            elif state == 'journal-spool':
                resources.enter_context(owner._resource('spool', op, identity))
                evidence = d.Evidence(op, identity, controls=controls(op, identity))
            else:
                tree = git(root, 'mktree', input=b'').strip().decode()
                oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
                resources.enter_context(owner.temporary_worktree(oid, operation_id=op, owner_id=identity))
                evidence = d.Evidence(op, identity, base_tree=oid, materialize=True, controls=controls(op, identity))
            parent = root/'data/native/git-resources'
            lock, control = parent/'journal.lock', parent/'journal.json'
            critical = owner._journal
        held_parent, held_lock, held_control = root/'held-owner-parent', root/'held-owner-lock', root/'held-owner-control'
        def move():
            if loss == 'parent': parent.rename(held_parent)
            else:
                lock.rename(held_lock)
                if loss == 'lock-and-control': control.rename(held_control)
        def restore():
            if loss == 'parent': held_parent.rename(parent)
            else:
                held_lock.rename(lock)
                if loss == 'lock-and-control': held_control.rename(control)
        def assert_actual_held_lock():
            path = held_parent/lock.name if loss == 'parent' else held_lock
            fd = os.open(path, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally: os.close(fd)
        with operational(disk):
            if stage == 'before-project':
                with critical():
                    snapshot = disk.snapshot()
                    assert dict(snapshot.observations)[os.fsencode(lock)] is not None
                    move()
                    try:
                        assert_actual_held_lock()
                        plan = disk.project('scope', snapshot, evidence)
                        assert not plan.supported, plan.unknown
                        assert any('lock' in error or 'journal' in error or 'state' in error for error in plan.unknown)
                    finally: restore()
            else:
                plan = disk.project('scope', disk.snapshot(), evidence)
                assert plan.supported, plan.unknown
                floor = plan.allocations, plan.reserves, plan.decisions
                forbid_full_phase_observations(disk, monkeypatch)
                with disk.guard(plan) as admission:
                    with critical():
                        move()
                        try:
                            assert_actual_held_lock()
                            for _ in range(2):
                                with pytest.raises(d.Blocked, match='lock|owner|journal'):
                                    admission.revalidate()
                                assert (plan.allocations, plan.reserves, plan.decisions) == floor
                        finally: restore()
                        with pytest.raises(d.Blocked, match='critical-section'):
                            admission.revalidate()
                    assert admission.revalidate() is plan


@pytest.mark.parametrize('active', [False, True])
def test_genuinely_absent_operation_lock_can_take_immediate_first_receipt(fixture, monkeypatch, active):
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root/'data'); store.read()
    if active:
        worker = claim(store, seed(store))
        evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
    else: evidence = request()
    lock = root/'data/native-operations/lock'
    lock.unlink()  # No held owner FD; absent before the actual snapshot.
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations).get(os.fsencode(lock)) is None
        plan = disk.project('scope', snapshot, evidence)
        assert plan.supported, plan.unknown
        floor = plan.allocations, plan.reserves, plan.decisions
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            assert admission.revalidate() is plan
            store.read()  # Canonical creates the missing lock, critical section ends.
            assert admission.revalidate() is plan
            assert (plan.allocations, plan.reserves, plan.decisions) == floor
            lock.unlink()
            with pytest.raises(d.Blocked, match='lock'):
                admission.revalidate()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('valid', [False, True])
def test_packed_ref_grammar_uses_actual_configured_object_width(fixture, object_format, valid):
    import native_git
    root, _ = fixture
    root = root/'format-fixture'
    (root/'data/native').mkdir(parents=True)
    subprocess.run(['git', 'init', '--bare', '--initial-branch=fixture', '--object-format='+object_format,
                    str(root/'data/native/repository.git')], check=True, capture_output=True)
    disk = d.NativeDisk(root)
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    value = oid.encode() if valid else b'a'*(64 if object_format == 'sha1' else 40)
    write(root, 'data/native/repository.git/packed-refs', value+b' refs/heads/fixture\n')
    owner = native_git.NativeGit(root)
    if valid: assert owner.read_ref('refs/heads/fixture').oid == oid
    else:
        with pytest.raises((native_git.InvalidInput, native_git.GitFailure)):
            owner.read_ref('refs/heads/fixture')
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), request(base_tree=oid))
        assert plan.supported is valid, plan.unknown
        if valid:
            with disk.guard(plan) as admission: assert admission.revalidate() is plan


@pytest.mark.parametrize('budget', ['bytes', 'entries', 'time'])
def test_initial_packed_grammar_has_finite_observation_budgets(fixture, monkeypatch, budget):
    root, _ = fixture
    rows = b'a'*40+b' refs/heads/first\n'+b'b'*40+b' refs/heads/second\n'
    limits = d.Limits(git_bytes=32) if budget == 'bytes' else d.Limits(entries=1) if budget == 'entries' else d.Limits(seconds=1)
    disk = d.NativeDisk(root, limits=limits)
    if budget in ('bytes', 'time'):
        monkeypatch.setattr(disk, '_git', lambda *args, **kw: pytest.fail('exhausted grammar started Git'))
    if budget == 'time': monkeypatch.setattr(d.time, 'monotonic', mock.Mock(side_effect=[0, 2]))
    with pytest.raises(d.Blocked, match='budget|unbounded'):
        disk._packed_ref_structure(rows, 40)


@pytest.mark.parametrize('known', [False, True])
def test_owner_lock_file_not_found_error_cannot_hide_existing_route(fixture, monkeypatch, known):
    from test_native_operations import NamespacedStore
    root, disk = fixture
    store = NamespacedStore(root/'data'); store.read()
    lock = root/'data/native-operations/lock'
    if not known: lock.unlink()
    with operational(disk):
        snapshot = disk.snapshot()
        with store._locked():
            original = disk.source
            def uncertain(path):
                if os.fsencode(path) == os.fsencode(lock): raise FileNotFoundError('injected path acquisition loss')
                return original(path)
            monkeypatch.setattr(disk, 'source', uncertain)
            plan = disk.project('scope', snapshot, request())
            assert not plan.supported
            assert any('owner-lock' in error for error in plan.unknown)
# Append to ha-ops/tests/test_native_disk.py; uses existing helpers.


@pytest.mark.parametrize('active', [False, True])
def test_phase_existing_new_operation_lock_acquisition_error_refuses(fixture, monkeypatch, active):
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    store.read()
    if active:
        worker = claim(store, seed(store))
        evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
    else:
        evidence = request()
    lock = root / 'data/native-operations/lock'
    lock.unlink()
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations).get(os.fsencode(lock)) is None
        plan = disk.project('scope', snapshot, evidence)
        assert plan.supported, plan.unknown
        with disk.guard(plan) as admission:
            store.read()
            assert lock.is_file()
            # Ordinary missing-file acquisition failure must not be confused
            # with proved absence of the current route during a phase.
            with store._locked():
                with pytest.raises(d.Blocked, match='critical-section'):
                    admission.revalidate()
            # The required first receipt is requested outside the owner
            # critical section, exactly as the cooperative caller contract says.
            original = disk.source
            def acquisition_error(path):
                if os.fsencode(path) == os.fsencode(lock):
                    raise FileNotFoundError('isolated lock acquisition I/O failure')
                return original(path)
            with monkeypatch.context() as patch:
                patch.setattr(disk, 'source', acquisition_error)
                with pytest.raises(d.Blocked, match='lock|owner|presence|unverifiable'):
                    admission.revalidate()


@pytest.mark.parametrize('active', [False, True])
def test_current_new_operation_lock_outside_error_takes_receipt_and_fences_later_loss(fixture, active):
    from test_native_operations import NamespacedStore, seed, claim
    root, disk = fixture
    store = NamespacedStore(root / 'data')
    store.read()
    if active:
        worker = claim(store, seed(store))
        evidence = d.Evidence(worker['id'], worker['owner'], controls=controls(worker['id'], worker['owner']))
    else:
        evidence = request()
    lock = root / 'data/native-operations/lock'
    lock.unlink()
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), evidence)
        floor = plan.allocations, plan.reserves, plan.decisions
        with disk.guard(plan) as admission:
            store.read()
            assert admission.revalidate() is plan
            assert admission._owner_locks[b'data/native-operations/lock'] is not None
            for _ in range(2):
                assert admission.revalidate() is plan
                assert (plan.allocations, plan.reserves, plan.decisions) == floor
            lock.unlink()
            with pytest.raises(d.Blocked, match='lock'):
                admission.revalidate()


@pytest.mark.parametrize('route', ['HEAD', 'loose-ref', 'packed-refs'])
def test_initial_existing_git_dependency_acquisition_error_is_not_absence(fixture, monkeypatch, route):
    import native_git
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', oid)
    if route == 'packed-refs':
        git(root, 'pack-refs', '--all')
    metadata = root / 'data/native/repository.git'
    target = metadata / ('refs/heads/fixture' if route == 'loose-ref' else route)
    assert target.is_file()
    assert native_git.NativeGit(root).read_ref('refs/heads/fixture').oid == oid
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations)[os.fsencode(target)] is not None
        original = disk.source
        def acquisition_error(path):
            if os.fsencode(path) == os.fsencode(target):
                raise FileNotFoundError('isolated Git dependency acquisition I/O failure')
            return original(path)
        monkeypatch.setattr(disk, 'source', acquisition_error)
        plan = disk.project('scope', snapshot, request(base_tree=oid))
        if plan.supported:
            with pytest.raises(d.Blocked, match='dependency|presence|unverifiable|Git|git'):
                with disk.guard(plan) as admission:
                    admission.revalidate()
        else:
            assert plan.unknown


@pytest.mark.parametrize('route', ['HEAD', 'loose-ref', 'packed-refs', 'config', 'loose-object', 'pack-object'])
@pytest.mark.parametrize('stage', ['project', 'guard', 'phase'])
@pytest.mark.parametrize('failure', ['acquisition', 'unlink', 'parent-loss'])
def test_known_git_optional_route_cannot_become_absent(fixture, monkeypatch, route, stage, failure):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', oid)
    metadata = root / 'data/native/repository.git'
    if route == 'packed-refs': git(root, 'pack-refs', '--all')
    if route == 'pack-object': git(root, 'repack', '-ad')
    target = (metadata/'refs/heads/fixture' if route == 'loose-ref' else
              metadata/'objects'/oid[:2]/oid[2:] if route == 'loose-object' else
              next((metadata/'objects/pack').glob('*.pack')) if route == 'pack-object' else metadata/route)
    assert target.is_file()
    def break_route():
        if failure == 'unlink': target.unlink()
        elif failure == 'parent-loss': target.parent.rename(target.parent.with_name(target.parent.name+'-lost'))
        else:
            original = disk.source
            def uncertain(path):
                if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated acquisition uncertainty')
                return original(path)
            monkeypatch.setattr(disk, 'source', uncertain)
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations)[os.fsencode(target)] is not None
        if stage == 'project':
            break_route()
            plan = disk.project('scope', snapshot, request(base_tree=oid))
            assert not plan.supported, plan.unknown
        else:
            plan = disk.project('scope', snapshot, request(base_tree=oid))
            assert plan.supported, plan.unknown
            forbid_full_phase_observations(disk, monkeypatch)
            if stage == 'guard':
                break_route()
                with pytest.raises((d.Blocked, OSError)):
                    with disk.guard(plan): pytest.fail('uncertain authority admitted')
            else:
                with disk.guard(plan) as admission:
                    break_route()
                    with pytest.raises((d.Blocked, OSError)): admission.revalidate()


@pytest.mark.parametrize('kind', ['operation', 'journal'])
@pytest.mark.parametrize('stage', ['project', 'guard', 'phase'])
@pytest.mark.parametrize('failure', ['source', 'control-read', 'parent-open'])
def test_optional_owner_control_acquisition_failure_is_not_empty_state(fixture, monkeypatch, kind, stage, failure):
    root, disk = fixture
    if kind == 'operation':
        from test_native_operations import NamespacedStore
        NamespacedStore(root/'data').read()
        target = root/'data/native-operations/state.json'
    else:
        import native_git
        owner = native_git.NativeGit(root)
        with owner._journal(create=True) as journal: owner._publish(journal)
        target = root/'data/native/git-resources/journal.json'
    assert target.is_file()
    def break_route():
        if failure == 'control-read':
            original = disk._control
            def uncertain(path):
                if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated control-read uncertainty')
                return original(path)
            monkeypatch.setattr(disk, '_control', uncertain)
        elif failure == 'source':
            original = disk.source
            def uncertain(path):
                if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated source uncertainty')
                return original(path)
            monkeypatch.setattr(disk, 'source', uncertain)
        else:
            original = d.os.open
            def uncertain(path, *args, **kw):
                if os.fsencode(path) == os.fsencode(target.parent.name): raise FileNotFoundError('isolated parent-open uncertainty')
                return original(path, *args, **kw)
            monkeypatch.setattr(d.os, 'open', uncertain)
    with operational(disk):
        snapshot = disk.snapshot()
        if stage == 'project':
            break_route()
            plan = disk.project('scope', snapshot, request())
            assert not plan.supported, plan.unknown
        else:
            plan = disk.project('scope', snapshot, request())
            assert plan.supported, plan.unknown
            forbid_full_phase_observations(disk, monkeypatch)
            if stage == 'guard':
                break_route()
                with pytest.raises((d.Blocked, OSError)):
                    with disk.guard(plan): pytest.fail('uncertain control admitted')
            else:
                with disk.guard(plan) as admission:
                    break_route()
                    with pytest.raises((d.Blocked, OSError)): admission.revalidate()


@pytest.mark.parametrize('target_kind', ['file', 'directory', 'missing-leaf', 'missing-parent'])
@pytest.mark.parametrize('fault', ['stat', 'open'])
def test_optional_presence_probe_refuses_uncertain_existing_component(fixture, monkeypatch, target_kind, fault):
    root, disk = fixture
    parent = root/'data/native/probe'
    parent.mkdir()
    target = parent/'leaf'
    if target_kind == 'file': target.write_bytes(b'proof')
    elif target_kind == 'directory': target.mkdir()
    elif target_kind == 'missing-parent': target = parent/'absent/leaf'
    injected = target.name if target_kind in ('file', 'directory') else parent.name
    original = getattr(d.os, fault)
    def uncertain(path, *args, **kw):
        if os.fsencode(path) == os.fsencode(injected): raise FileNotFoundError('isolated syscall acquisition failure')
        return original(path, *args, **kw)
    monkeypatch.setattr(d.os, fault, uncertain)
    if fault == 'open' and target_kind in ('file', 'directory'):
        # Presence alone never opens a present leaf; acquisition is the source
        # observer's duty. Its failed open must still refuse optional admission.
        with pytest.raises((d.Blocked, OSError)):
            disk._optional_source(target)
    else:
        with pytest.raises(d.Blocked): d._optional_stat(target)


@pytest.mark.parametrize('absent', ['leaf', 'nested-parent'])
def test_optional_absence_is_proved_without_enumeration_or_content_reads(fixture, monkeypatch, absent):
    root, disk = fixture
    target = root/'data/native'/('missing' if absent == 'leaf' else 'missing/child/leaf')
    monkeypatch.setattr(d.os, 'scandir', lambda *a, **kw: pytest.fail('optional proof enumerated'))
    monkeypatch.setattr(d.os, 'read', lambda *a, **kw: pytest.fail('optional proof read contents'))
    assert d._optional_stat(target) is None
    assert disk._optional_source(target) is None
    with disk._idle_lock(os.fsencode(target.relative_to(root))) as identity: assert identity is None


@pytest.mark.parametrize('initial', [False, True])
@pytest.mark.parametrize('fault', ['source', 'control-read', 'unlink'])
def test_empty_journal_known_or_first_observed_presence_remains_bound(fixture, monkeypatch, initial, fault):
    import native_git
    import uuid
    root, disk = fixture
    owner = native_git.NativeGit(root)
    op, owner_id = str(uuid.uuid4()), str(uuid.uuid4())
    target = root/'data/native/git-resources/journal.json'
    def create():
        with owner._journal(create=True) as journal: owner._publish(journal)
        assert target.is_file()
    if initial: create()
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner_id, controls=controls(op, owner_id)))
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            if not initial:
                with owner.isolated_index(None, operation_id=op, owner_id=owner_id):
                    assert admission.revalidate() is plan
                assert admission.revalidate() is plan
            if fault == 'unlink': target.unlink()
            else:
                method = 'source' if fault == 'source' else '_control'
                original = getattr(disk, method)
                def uncertain(path):
                    if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated established control acquisition failure')
                    return original(path)
                monkeypatch.setattr(disk, method, uncertain)
            with pytest.raises(d.Blocked): admission.revalidate()


@pytest.mark.parametrize('kind', ['operation', 'journal'])
@pytest.mark.parametrize('failure', ['source', 'parent-open', None])
def test_prospective_owner_lock_visible_acquisition_error_cannot_delay_receipt(fixture, monkeypatch, kind, failure):
    from contextlib import ExitStack
    import native_git
    import uuid
    root, disk = fixture
    op, owner_id = str(uuid.uuid4()), str(uuid.uuid4())
    if kind == 'operation':
        from test_native_operations import NamespacedStore
        owner = NamespacedStore(root/'data'); owner.read()
        target = root/'data/native-operations/lock'
        target.unlink()
    else:
        owner = native_git.NativeGit(root)
        target = root/'data/native/git-resources/journal.lock'
    with operational(disk):
        plan = disk.project('scope', disk.snapshot(), d.Evidence(op, owner_id, controls=controls(op, owner_id)))
        assert plan.supported, plan.unknown
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission, ExitStack() as resources:
            if kind == 'operation': owner.read()
            else: resources.enter_context(owner.isolated_index(None, operation_id=op, owner_id=owner_id))
            # Canonical mutator leaves its critical section before first receipt.
            assert target.is_file()
            with monkeypatch.context() as patch:
                if failure == 'source':
                    original = disk.source
                    def uncertain(path):
                        if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated new lock source uncertainty')
                        return original(path)
                    patch.setattr(disk, 'source', uncertain)
                elif failure == 'parent-open':
                    original = d.os.open
                    def uncertain(path, *args, **kw):
                        if os.fsencode(path) == os.fsencode(target.parent.name): raise FileNotFoundError('isolated owner parent uncertainty')
                        return original(path, *args, **kw)
                    patch.setattr(d.os, 'open', uncertain)
                if failure is None:
                    floor = plan.allocations, plan.reserves, plan.decisions
                    assert admission.revalidate() is plan
                    assert admission.revalidate() is plan
                    assert (plan.allocations, plan.reserves, plan.decisions) == floor
                else:
                    with pytest.raises(d.Blocked): admission.revalidate()


@pytest.mark.parametrize('failure', ['source', 'unlink', 'parent-loss'])
def test_save_known_reflog_cannot_become_empty_preimage(fixture, monkeypatch, failure):
    root, disk = fixture
    evidence, oid = save_evidence(root, disk, b'refs/heads/candidate')
    target = Path(os.fsdecode(write(root, 'data/native/repository.git/logs/refs/heads/candidate', b'previous log\n')))
    with operational(disk):
        snapshot = disk.snapshot()
        if failure == 'unlink': target.unlink()
        elif failure == 'parent-loss': target.parent.rename(target.parent.with_name('lost-heads'))
        else:
            original = disk.source
            def uncertain(path):
                if os.fsencode(path) == os.fsencode(target): raise FileNotFoundError('isolated preimage acquisition error')
                return original(path)
            monkeypatch.setattr(disk, 'source', uncertain)
        plan = disk.project('save', snapshot, evidence)
        assert not plan.supported, plan.unknown


@pytest.mark.parametrize('kind', ['directory', 'file'])
@pytest.mark.parametrize('fault', ['stat', 'open'])
def test_destination_existing_acquisition_failure_is_not_future_namespace(fixture, monkeypatch, kind, fault):
    root, disk = fixture
    target = root/'data/native/output-proof'
    if kind == 'directory': target.mkdir()
    else: target.write_bytes(b'initial output')
    original = getattr(d.os, fault)
    def uncertain(path, *args, **kw):
        if os.fsencode(path) == os.fsencode(target.name): raise FileNotFoundError('isolated destination acquisition error')
        return original(path, *args, **kw)
    monkeypatch.setattr(d.os, fault, uncertain)
    with operational(disk), pytest.raises((d.Blocked, OSError)):
        disk._destination(b'data/native/output-proof', None, file_target=kind == 'file')


# Append to ha-ops/tests/test_native_disk.py; uses existing helpers.

@pytest.mark.parametrize('route', ['HEAD', 'loose-ref', 'packed-refs'])
def test_known_git_leaf_temporary_disappearance_cannot_compile_absence(fixture, monkeypatch, route):
    root, disk = fixture
    tree = git(root, 'mktree', input=b'').strip().decode()
    oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', oid)
    if route == 'packed-refs':
        git(root, 'pack-refs', '--all')
    metadata = root / 'data/native/repository.git'
    target = metadata / ('refs/heads/fixture' if route == 'loose-ref' else route)
    held = root / 'temporarily-missing-known-git-leaf'
    original_source, original_open = disk.source, os.open
    def source_during_missing(path):
        if os.fsencode(path) == os.fsencode(target):
            target.rename(held)
        return original_source(path)
    def restore_after_missing_open(path, *args, **kw):
        try:
            return original_open(path, *args, **kw)
        except FileNotFoundError:
            if os.fsencode(path) == os.fsencode(target.name) and held.exists():
                held.rename(target)
            raise
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations)[os.fsencode(target)] is not None
        monkeypatch.setattr(disk, 'source', source_during_missing)
        monkeypatch.setattr(os, 'open', restore_after_missing_open)
        try:
            plan = disk.project('scope', snapshot, request(base_tree=oid))
            if plan.supported:
                with pytest.raises(d.Blocked, match='known|presence|dependency|Git|git'):
                    with disk.guard(plan) as admission:
                        admission.revalidate()
            else:
                assert plan.unknown
        finally:
            if held.exists():
                held.rename(target)

def test_known_reflog_temporary_disappearance_keeps_complete_append_preimage(fixture, monkeypatch):
    root, disk = fixture
    evidence, _ = save_evidence(root, disk, b'refs/heads/candidate')
    original_bytes = b'previous log content\n' * 5000
    target = Path(os.fsdecode(write(root, 'data/native/repository.git/logs/refs/heads/candidate', original_bytes)))
    held = root / 'temporarily-missing-known-reflog'
    original_source, original_open = disk.source, os.open
    def source_during_missing(path):
        if os.fsencode(path) == os.fsencode(target):
            target.rename(held)
        return original_source(path)
    def restore_after_missing_open(path, *args, **kw):
        try:
            return original_open(path, *args, **kw)
        except FileNotFoundError:
            if os.fsencode(path) == os.fsencode(target.name) and held.exists():
                held.rename(target)
            raise
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations)[os.fsencode(target)] is not None
        monkeypatch.setattr(disk, 'source', source_during_missing)
        monkeypatch.setattr(os, 'open', restore_after_missing_open)
        try:
            plan = disk.project('save', snapshot, evidence)
            if plan.supported:
                projected = [a.logical for a in plan.allocations if a.output == 'full-reflog-copy-append']
                assert projected == [len(original_bytes) + len(evidence.reflog_entry)]
            else:
                assert plan.unknown
        finally:
            if held.exists():
                held.rename(target)


def test_snapshot_known_reappearing_packed_file_never_becomes_none(fixture,monkeypatch):
    import native_git
    root,disk=fixture
    tree=git(root,'mktree',input=b'').strip().decode()
    oid=git(root,'commit-tree',tree,input=b'base\n').strip().decode()
    git(root,'update-ref','refs/heads/fixture',oid)
    git(root,'pack-refs','--all')
    path=root/'data/native/repository.git/packed-refs'
    held=root/'transiently-moved-packed-refs'
    with operational(disk):
        snapshot=disk.snapshot()
        assert os.fsencode(path) in dict(snapshot.observations)
        path.write_bytes(path.read_bytes().replace(oid.encode()+b' ',oid.encode()+b'\t'))
        with pytest.raises((native_git.InvalidInput,native_git.GitFailure)):
            native_git.NativeGit(root).read_ref('refs/heads/fixture')
        original_source=disk.source
        original_stat=d._optional_stat
        def source(candidate):
            if os.fsencode(candidate)==os.fsencode(path):
                assert path.exists() and not held.exists()
                path.rename(held)
                # Actual original nofollow acquisition gets genuine ENOENT.
                return original_source(candidate)
            return original_source(candidate)
        def presence(candidate, observations=None):
            result=original_stat(candidate,observations)
            if os.fsencode(candidate)==os.fsencode(path) and result is None and held.exists():
                # Concurrent writer restores the same inode after the held
                # absence probe, before snapshot-known follow-up validation.
                held.rename(path)
            return result
        monkeypatch.setattr(disk,'source',source)
        monkeypatch.setattr(d,'_optional_stat',presence)
        try:
            p=disk.project('scope',snapshot,request(base_tree=oid))
            if p.supported:
                assert dict(disk._contexts[id(p)].dependencies)[os.fsencode(path)] is None
                forbid_full_phase_observations(disk,monkeypatch)
                with disk.guard(p) as admission:
                    assert admission.revalidate() is p
                    assert path.exists()
            assert not p.supported,p.unknown
        finally:
            if held.exists():held.rename(path)


@pytest.mark.parametrize('route', ['HEAD', 'loose-ref', 'packed-refs', 'reflog'])
@pytest.mark.parametrize('stage', ['project', 'guard', 'phase'])
@pytest.mark.parametrize('different_inode', [False, True])
@pytest.mark.parametrize('changed_bytes', [False, True])
def test_known_leaf_loss_is_not_erased_by_later_reappearance(fixture, monkeypatch, route, stage, different_inode, changed_bytes):
    root, disk = fixture
    metadata = root/'data/native/repository.git'
    if route == 'reflog':
        evidence, _ = save_evidence(root, disk, b'refs/heads/candidate')
        target = Path(os.fsdecode(write(root, 'data/native/repository.git/logs/refs/heads/candidate', b'previous log content\n'*5000)))
        kind = 'save'
    else:
        tree = git(root, 'mktree', input=b'').strip().decode()
        oid = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
        git(root, 'update-ref', 'refs/heads/fixture', oid)
        if route == 'packed-refs': git(root, 'pack-refs', '--all')
        target = metadata/('refs/heads/fixture' if route == 'loose-ref' else route)
        kind, evidence = 'scope', request(base_tree=oid)
    original_bytes = target.read_bytes()
    held = root/'known-leaf-during-loss'
    replacement = root/'known-leaf-replacement'
    replacement.write_bytes(original_bytes + (b'changed\n' if changed_bytes else b''))
    original_source, original_stat = disk.source, d._optional_stat
    events = []
    def lost_source(path):
        if os.fsencode(path) == os.fsencode(target):
            target.rename(held)
            events.append('lost-source')
        return original_source(path)  # genuine kernel ENOENT at the known leaf
    def presence(path, observations=None):
        result = original_stat(path, observations)
        if os.fsencode(path) == os.fsencode(target) and result is None and held.exists():
            events.append('proved-missing')
            if different_inode:
                replacement.rename(target)
            else:
                if changed_bytes: held.write_bytes(original_bytes+b'changed\n')
                held.rename(target)
            events.append('reappeared')
        return result
    def disturb(patch):
        patch.setattr(disk, 'source', lost_source)
        patch.setattr(d, '_optional_stat', presence)
    with operational(disk):
        snapshot = disk.snapshot()
        assert dict(snapshot.observations)[os.fsencode(target)] is not None
        try:
            if stage == 'project':
                with monkeypatch.context() as patch:
                    disturb(patch)
                    plan = disk.project(kind, snapshot, evidence)
                    assert not plan.supported, plan.unknown
            else:
                plan = disk.project(kind, snapshot, evidence)
                assert plan.supported, plan.unknown
                floor = plan.allocations, plan.reserves, plan.decisions
                forbid_full_phase_observations(disk, monkeypatch)
                if stage == 'guard':
                    with monkeypatch.context() as patch:
                        disturb(patch)
                        with pytest.raises((d.Blocked, OSError)):
                            with disk.guard(plan): pytest.fail('known leaf loss admitted')
                else:
                    with disk.guard(plan) as admission, monkeypatch.context() as patch:
                        disturb(patch)
                        with pytest.raises((d.Blocked, OSError)): admission.revalidate()
                assert (plan.allocations, plan.reserves, plan.decisions) == floor
            assert 'lost-source' in events
            # Finite refusal can precede reappearance. In particular a known
            # source failure must not trigger an absence/requalification loop.
            if route != 'reflog' or stage == 'project':
                assert 'proved-missing' not in events
        finally:
            if held.exists():
                if target.exists(): target.unlink()
                held.rename(target)
            if target.exists(): target.write_bytes(original_bytes)


@pytest.mark.parametrize('different_inode', [False, True])
@pytest.mark.parametrize('changed_bytes', [False, True])
def test_already_returned_known_leaf_still_refuses_failed_acquisition(fixture, monkeypatch, different_inode, changed_bytes):
    root, disk = fixture
    target = root/'data/native/repository.git/HEAD'
    held = root/'known-leaf-lost-before-error'
    original_bytes = target.read_bytes()
    original_source = disk.source
    events = []
    def returned_source(path):
        if os.fsencode(path) != os.fsencode(target): return original_source(path)
        target.rename(held)
        try:
            original_source(path)
        except FileNotFoundError:
            events.append('real-ENOENT')
            if different_inode:
                target.write_bytes(original_bytes+(b'changed\n' if changed_bytes else b''))
            else:
                if changed_bytes: held.write_bytes(original_bytes+b'changed\n')
                held.rename(target)
            assert target.is_file()
            raise
        pytest.fail('missing leaf did not produce ENOENT')
    with operational(disk):
        snapshot = disk.snapshot()
        monkeypatch.setattr(disk, 'source', returned_source)
        original_stat = d._optional_stat
        def no_known_leaf_probe(path, observations=None):
            if os.fsencode(path) == os.fsencode(target): pytest.fail('known leaf failure probed for absence')
            return original_stat(path, observations)
        monkeypatch.setattr(d, '_optional_stat', no_known_leaf_probe)
        try:
            plan = disk.project('scope', snapshot, request())
            assert not plan.supported, plan.unknown
            assert events == ['real-ENOENT']
        finally:
            if held.exists():
                if target.exists(): target.unlink()
                held.rename(target)
            target.write_bytes(original_bytes)


@pytest.mark.parametrize('different_inode', [False, True])
@pytest.mark.parametrize('initially_known', [False, True])
def test_observed_missing_parent_cannot_be_erased_by_parent_reappearance(fixture, monkeypatch, different_inode, initially_known):
    root, disk = fixture
    parent = root/'data/native/parent-proof'
    if initially_known: parent.mkdir()
    with operational(disk): snapshot = disk.snapshot()
    initial = dict(snapshot.observations)
    if not initially_known: parent.mkdir()
    target = parent/'genuinely-never-observed-leaf'
    held = root/'missing-parent-during-probe'
    parent.rename(held)
    original = d._optional_stat
    events = []
    def restore(path, observations=None):
        result = original(path, observations)
        if os.fsencode(path) == os.fsencode(target) and result is None and held.exists():
            events.append('missing-parent-observed')
            if different_inode: parent.mkdir()
            else: held.rename(parent)
        return result
    monkeypatch.setattr(d, '_optional_stat', restore)
    try:
        if initially_known:
            with pytest.raises(d.Blocked, match='known-ancestor'): disk._require_absent(target, initial)
        else:
            # No historical parent identity was asserted; current true leaf
            # absence remains permissible under the cooperative caller model.
            disk._require_absent(target, initial)
        assert events == ['missing-parent-observed']
    finally:
        if held.exists():
            if parent.exists(): parent.rmdir()
            held.rename(parent)


def test_known_leaf_refusal_does_not_probe_or_retry_absence(fixture, monkeypatch):
    root, disk = fixture
    target = root/'data/native/repository.git/HEAD'
    with operational(disk): snapshot = disk.snapshot()
    monkeypatch.setattr(d, '_optional_stat', lambda *a, **kw: pytest.fail('known leaf absence retried'))
    with pytest.raises(d.Blocked, match='known-dependency'):
        disk._require_absent(target, dict(snapshot.observations))


def test_known_reflog_complete_preimage_positive_keeps_frozen_floor(fixture, monkeypatch):
    root, disk = fixture
    evidence, _ = save_evidence(root, disk, b'refs/heads/candidate')
    original = b'previous log content\n'*5000
    write(root, 'data/native/repository.git/logs/refs/heads/candidate', original)
    with operational(disk):
        plan = disk.project('save', disk.snapshot(), evidence)
        assert plan.supported, plan.unknown
        assert [a.logical for a in plan.allocations if a.output == 'full-reflog-copy-append'] == [105000+len(evidence.reflog_entry)]
        floor = plan.allocations, plan.reserves, plan.decisions
        forbid_full_phase_observations(disk, monkeypatch)
        with disk.guard(plan) as admission:
            for _ in range(2): assert admission.revalidate() is plan
            assert (plan.allocations, plan.reserves, plan.decisions) == floor
