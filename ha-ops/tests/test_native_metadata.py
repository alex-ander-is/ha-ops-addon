"""Fixed metadata format, selection, Git transport and exact inode proof."""
import ast
import base64
import dataclasses as d
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import native_metadata as n
from native_git import NativeGit, TreeEntry, IndexEntry
from native_inventory import Mount, Policy, linux_mounts
from native_topology_helpers import git, METADATA


def oid(data, width=40):
    h = hashlib.sha256() if width == 64 else hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def entry(path=b"homeassistant/p/a", data=b"A", mode=b"100644", width=40):
    return TreeEntry(path, mode, oid(data, width), b"blob")


def manifest(entries=(entry(),), *, uid=0, gid=0, mode=0o644):
    rows = [n.Record(e.path, "file", uid, gid, mode, e.oid) for e in entries]
    rows += [n.Record(p, "directory", uid, gid, 0o755, None) for p in n.ancestors(e.path for e in entries)]
    return n.Manifest(tuple(sorted(rows, key=lambda r: r.path)))


def replace(m, path, **kwargs):
    return n.Manifest(tuple(d.replace(r, **kwargs) if r.path == path else r for r in m.entries))


def write(root, path, data=b"A"):
    target = os.path.join(os.fsencode(root), path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)


def binding(root, policy=None):
    root = os.fsencode(Path(root).resolve())
    if sys.platform == "linux":
        mounts = linux_mounts()
    else:
        s = os.stat(root)
        mounts = (Mount(1, 0, f"{os.major(s.st_dev)}:{os.minor(s.st_dev)}".encode(), b"/", b"/", b"rw", b"fixture", b"fixture", b"rw"),)
    return n.Binding(root, policy or Policy(), mounts, tuple(m.id for m in mounts), True)


@pytest.fixture
def materialized(tmp_path):
    root = tmp_path.resolve()
    write(root, b"homeassistant/p/a")
    b = binding(root)
    e = (entry(),)
    return root, b, e, n.capture(b, e)


def test_canonical_complete_format_and_digest():
    m = manifest((entry(), entry(b"homeassistant/p/\xff space\t[*]?", b"B")))
    assert n.decode(n.encode(m)) == m
    assert n.encode(n.Manifest(tuple(reversed(m.entries)))) == n.encode(m)
    assert m.encoded_size == len(n.encode(m))
    assert m.digest == hashlib.sha256(n.encode(m)).hexdigest()
    assert n.decode(b'{"entries":[]}') == n.Manifest(())
    e = (entry(b"custom/deep/a", width=64),)
    p = Policy(extra_live_roots=(b"custom/deep",))
    m = manifest(e)
    n.validate(m, e, width=64, policy=p)
    assert n.decode(n.encode(m, width=64, policy=p), width=64, policy=p) == m
    assert m.encoded_size and m.digest


@pytest.mark.parametrize("payload", [b"\xff", b'{"entries":[],"entries":[]}', b'{"entries":[],"version":1}', b'{"entries":NaN}', b'{"entries":Infinity}', b'[]', b'{"entries":{}}'])
def test_bad_json(payload):
    with pytest.raises(n.Blocked):
        n.decode(payload)


@pytest.mark.parametrize("key,value", [("uid", True), ("gid", False), ("mode", True), ("uid", -1), ("gid", 2**32-1), ("uid", 2**32), ("mode", 0o10000), ("kind", "symlink"), ("oid", "a"*64), ("oid", "A"*40), ("extra", 0), ("path", "aG9tZWFzc2lzdGFudC9wL2E=\n"), ("path", base64.b64encode(b"../escape").decode()), ("path", base64.b64encode(b"homeassistant/secrets.yaml").decode())])
def test_invalid_later_record_blocks_all_effects(materialized, monkeypatch, key, value):
    root, b, tree, m = materialized
    raw = json.loads(n.encode(m))
    raw["entries"][-1][key] = value
    writes = []
    monkeypatch.setattr(os, "fchmod", lambda *a: writes.append(a))
    monkeypatch.setattr(os, "fchown", lambda *a: writes.append(a))
    with pytest.raises(n.Blocked):
        n.decode(json.dumps(raw).encode())
    assert writes == []


@pytest.mark.parametrize("change", ["duplicate", "missing-parent", "extra-parent", "file-parent", "oid", "executable", "missing-file"])
def test_complete_validation(change):
    e = (entry(),)
    m = manifest(e)
    if change == "duplicate":
        m = n.Manifest(m.entries + (m.entries[-1],))
    elif change == "missing-parent":
        m = n.Manifest(m.entries[1:])
    elif change == "extra-parent":
        m = n.Manifest(m.entries + (n.Record(b"homeassistant/orphan", "directory", 0, 0, 0o755, None),))
    elif change == "file-parent":
        m = replace(m, b"homeassistant/p", kind="file", oid="a"*40)
    elif change == "oid":
        m = replace(m, e[0].path, oid="b"*40)
    elif change == "executable":
        m = replace(m, e[0].path, mode=0o755)
    else:
        e = ()
    with pytest.raises(n.Blocked):
        n.validate(m, e)


def test_duplicate_row_keys_and_paths():
    data = n.encode(manifest())
    with pytest.raises(n.Blocked):
        n.decode(data.replace(b'"uid":0', b'"uid":0,"uid":1', 1))
    raw = json.loads(data)
    raw["entries"].append(raw["entries"][-1])
    with pytest.raises(n.Blocked):
        n.decode(json.dumps(raw).encode())


def test_selection_shared_parent_new_parent_and_pruning():
    es = (entry(), entry(b"homeassistant/p/b", b"B"), entry(b"homeassistant/q/c", b"C"))
    baseline = manifest(es)
    fresh = replace(replace(replace(baseline, es[0].path, uid=123), es[1].path, mode=0o600, oid=oid(b"changed B")), b"homeassistant/p", gid=456)
    chosen = n.select(baseline, fresh, (es[0].path,))
    assert chosen.manifest.by_path[es[1].path] == baseline.by_path[es[1].path]
    assert chosen.manifest.by_path[b"homeassistant/p"] == baseline.by_path[b"homeassistant/p"]
    assert chosen.parents == ()
    parent = n.select(baseline, fresh, (es[0].path,), approved_parents=(b"homeassistant/p",))
    assert parent.parents[0].dependents == (es[0].path, es[1].path)
    assert parent.manifest.by_path[es[1].path] == baseline.by_path[es[1].path]
    new = manifest(es + (entry(b"homeassistant/new/d", b"D"),))
    with pytest.raises(n.Blocked, match="review-required"):
        n.select(baseline, new, (b"homeassistant/new/d",))
    assert n.select(baseline, new, (b"homeassistant/new/d",), approved_parents=(b"homeassistant/new",)).manifest
    gone = manifest(es[:2])
    result = n.select(baseline, gone, (es[2].path,))
    assert b"homeassistant/q" not in result.manifest.by_path
    assert b"homeassistant" in result.manifest.by_path


def test_atomic_path_merge_and_closure_conflicts():
    es = (entry(), entry(b"homeassistant/p/b", b"B"))
    base = manifest(es)
    ours = replace(base, es[0].path, uid=100)
    theirs = replace(base, es[1].path, gid=200)
    clean = n.merge(base, ours, theirs)
    assert clean.conflicts == ()
    clean.resolved(es)
    clash = n.merge(base, ours, replace(base, es[0].path, mode=0o600))
    assert clash.conflicts[0].path == es[0].path
    assert set(clash.conflicts[0].facets) == {"uid", "mode"}
    with pytest.raises(n.Blocked):
        clash.resolved(es)
    deleted = manifest((es[1],))
    assert n.merge(base, ours, deleted).conflicts[0].facets == ("existence",)
    clash = n.merge(base, replace(base, b"homeassistant/p", uid=1), replace(base, b"homeassistant/p", gid=2))
    assert clash.conflicts[0].dependents == (es[0].path, es[1].path)
    base = manifest((entry(),))
    ours = n.Manifest(())
    theirs = manifest((entry(), entry(b"homeassistant/p/b", b"B")))
    assert any(c.facets == ("parent-closure",) for c in n.merge(base, ours, theirs).conflicts)
    # Both branches are individually valid, but merge would make a file its own parent.
    base = n.Manifest(())
    ours = manifest((entry(b"homeassistant/p", b"P"),))
    theirs = manifest((entry(),))
    assert any(c.path == b"homeassistant/p" for c in n.merge(base, ours, theirs).conflicts)


def test_capture_refuses_content_metadata_and_namespace_races(materialized, monkeypatch):
    root, b, es, m = materialized
    original = os.read
    fired = False
    def mutate(fd, size):
        nonlocal fired
        data = original(fd, size)
        if data and not fired:
            fired = True
            write(root, es[0].path, b"changed")
        return data
    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(n.Blocked, match="changed"):
        n.capture(b, es)


@pytest.mark.parametrize("unsafe", ["leaf-symlink", "parent-symlink", "fifo", "hardlink"])
def test_unsafe_types_have_no_metadata_effect(materialized, tmp_path, monkeypatch, unsafe):
    root, b, es, m = materialized
    leaf = root / "homeassistant/p/a"
    outside = root / "outside"
    outside.write_bytes(b"sentinel")
    if unsafe == "parent-symlink":
        shutil.rmtree(leaf.parent)
        (root / "homeassistant/p").symlink_to(root, target_is_directory=True)
    elif unsafe == "hardlink":
        os.link(leaf, outside.with_name("alias"))
    else:
        leaf.unlink()
        if unsafe == "leaf-symlink":
            leaf.symlink_to(outside)
        else:
            os.mkfifo(leaf)
    calls = []
    monkeypatch.setattr(os, "fchmod", lambda *a: calls.append(a))
    with pytest.raises((n.Blocked, OSError)):
        n.observe(b, m, (es[0].path,))
    assert not calls
    assert outside.read_bytes() == b"sentinel"


def test_before_required_and_stale_whole_set_prevalidates(materialized, monkeypatch):
    root, b, es, m = materialized
    before = n.observe(b, m, (es[0].path,), approved_parents=n.ancestors((es[0].path,)))
    calls = []
    monkeypatch.setattr(os, "fchmod", lambda *a: calls.append(a))
    with pytest.raises(n.Blocked, match="incomplete"):
        n.restore(b, m, es, (es[0].path,), d.replace(before, entries=before.entries[:-1]), approved_parents=n.ancestors((es[0].path,)))
    os.chmod(root / "homeassistant/p/a", 0o600)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, m, es, (es[0].path,), before, approved_parents=n.ancestors((es[0].path,)))
    assert failure.value.effects.attempted == ()
    assert calls == []


def test_mount_facts_refuse_unknown_device_and_stale_boundary(materialized):
    root, b, es, m = materialized
    with pytest.raises(n.Blocked):
        n.observe(d.replace(b, confirmed_mounts=()), m, (es[0].path,))
    mount = max((x for x in b.mounts if b.root == x.point or b.root.startswith(x.point.rstrip(b"/") + b"/")), key=lambda x:len(x.point))
    bad = d.replace(mount, id=999999, point=b.root, device=b"999:999")
    with pytest.raises(n.Blocked, match="device"):
        n.observe(d.replace(b, mounts=b.mounts + (bad,), confirmed_mounts=b.confirmed_mounts + (bad.id,)), m, (es[0].path,))
    if sys.platform == "linux":
        bad = d.replace(mount, id=999999, point=b.root)
        with pytest.raises(n.Blocked, match="stale"):
            n.observe(d.replace(b, mounts=b.mounts + (bad,), confirmed_mounts=b.confirmed_mounts + (bad.id,)), m, (es[0].path,))


def test_common_root_and_custom_nested_parent():
    b = binding(Path("/"))
    with n._opened(b, b"", "directory") as (fd, routes):
        assert stat.S_ISDIR(os.fstat(fd).st_mode) and b"" in routes
    assert n.observe(b, n.Manifest(()), ()).entries[0].path == b""
    e = (entry(b"unmanaged/deep/a"),)
    p = Policy(extra_live_roots=(b"unmanaged/deep",))
    n.validate(manifest(e), e, policy=p)


@pytest.mark.parametrize("fault", ["chmod", "chown", "readback", "fsync", "interrupt"])
def test_failed_effect_is_uncertain_and_no_later_path_runs(materialized, monkeypatch, fault):
    root, b, es, m = materialized
    target = replace(m, es[0].path, mode=0o600)
    if fault == "chown":
        target = replace(target, es[0].path, uid=os.getuid()+1)
    before = n.observe(b, target, (es[0].path,), approved_parents=n.ancestors((es[0].path,)))
    def fail(*args):
        raise OSError("injected " + fault)
    if fault == "chmod":
        monkeypatch.setattr(os, "fchmod", fail)
    elif fault == "chown":
        monkeypatch.setattr(os, "fchown", fail)
    elif fault == "readback":
        monkeypatch.setattr(os, "fchmod", lambda *a: None)
    elif fault == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    else:
        def interrupt(*args):
            raise KeyboardInterrupt("injected interruption")
        monkeypatch.setattr(os, "fchmod", interrupt)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, target, es, (es[0].path,), before, approved_parents=n.ancestors((es[0].path,)))
    effects = failure.value.effects
    assert effects.attempted == effects.uncertain == (es[0].path,)
    assert effects.completed == () and effects.failed == es[0].path
    assert set(effects.unattempted) == n.ancestors((es[0].path,))


def test_later_failure_preserves_completed_and_primary_double_fault(materialized, monkeypatch):
    root, b, es, m = materialized
    target = replace(m, es[0].path, mode=0o600)
    before = n.observe(b, target, (es[0].path,), approved_parents=(b"homeassistant/p",))
    real_chmod, real_close = os.fchmod, os.close
    fired = False
    def chmod(fd, mode):
        nonlocal fired
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            fired = True
            raise OSError("primary chmod")
        return real_chmod(fd, mode)
    def close(fd):
        real_close(fd)
        if fired:
            raise OSError("secondary close")
    monkeypatch.setattr(os, "fchmod", chmod)
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, target, es, (es[0].path,), before, approved_parents=(b"homeassistant/p",))
    assert str(failure.value.cause) == "primary chmod"
    assert failure.value.effects.completed == (es[0].path,)
    assert failure.value.effects.uncertain == (b"homeassistant/p",)
    assert failure.value.effects.secondary


def test_replaced_leaf_and_parent_after_review(materialized):
    root, b, es, m = materialized
    before = n.observe(b, m, (es[0].path,))
    (root / "homeassistant/p").rename(root / "homeassistant/old")
    write(root, es[0].path)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, m, es, (es[0].path,), before)
    assert failure.value.effects.attempted == ()


def initialize(root, width=40):
    (root / "data/native").mkdir(parents=True)
    git(root, "init", "--initial-branch=fixture", *(("--object-format=sha256",) if width == 64 else ()))
    write(root, b"homeassistant/p/a")
    write(root, b"homeassistant/p/b", b"B")
    p = Policy(repository_files=(n.MANIFEST_PATH,))
    g = NativeGit(root, policy=p)
    es = (entry(width=width), entry(b"homeassistant/p/b", b"B", width=width))
    m = n.capture(binding(root, p), es, width=width)
    write(root, n.MANIFEST_PATH, n.encode(m, policy=p, width=width))
    git(root, "add", "--", "homeassistant")
    git(root, "add", "-f", "--", os.fsdecode(n.MANIFEST_PATH))
    git(root, "commit", "-m", "tracked metadata fixture")
    return g, p, es, m


def test_real_nativegit_selection_and_canonical_index(tmp_path):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    canonical = (root / METADATA / "index").read_bytes()
    write(root, es[0].path, b"new A")
    write(root, es[1].path, b"new B")
    os.chmod(root / "homeassistant/p/b", 0o600)
    os.chmod(root / "homeassistant/p", 0o700)
    fresh_es = (entry(es[0].path, b"new A"), entry(es[1].path, b"new B"))
    fresh = n.capture(binding(root, p), fresh_es)
    chosen = n.select(m, fresh, (es[0].path,), policy=p)
    blob = g.write_blob_stream(b"new A")
    with g.isolated_index(git(root, "rev-parse", "HEAD^{tree}").strip().decode(), operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        candidate = n.stage_candidate(g, handle, chosen, entries=(IndexEntry(es[0].path, b"100644", blob.oid, 0),))
        complete = g.tree_entries(candidate)
        n.validate(chosen.manifest, complete, policy=p)
        assert next(e.oid for e in complete if e.path == es[1].path) == es[1].oid
    assert canonical == (root / METADATA / "index").read_bytes()
    assert chosen.manifest.by_path[b"homeassistant/p"] == m.by_path[b"homeassistant/p"]
    assert p.reason(b"data/native/elsewhere") and p.reason(b"homeassistant/secrets.yaml")


@pytest.mark.parametrize("bad", ["missing", "stale", "after-update"])
def test_staging_failure_returns_no_complete_candidate(tmp_path, monkeypatch, bad):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    canonical = (root / METADATA / "index").read_bytes()
    choice = n.select(m, m, (es[0].path,), policy=p)
    with g.isolated_index(git(root, "rev-parse", "HEAD^{tree}").strip().decode(), operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        if bad == "missing":
            g.update_index(handle, removals=(n.MANIFEST_PATH,))
        elif bad == "stale":
            blob = g.write_blob_stream(n.encode(replace(m, es[0].path, uid=m.by_path[es[0].path].uid+1), policy=p))
            g.update_index(handle, (IndexEntry(n.MANIFEST_PATH, b"100644", blob.oid, 0),))
        else:
            update = g.update_index
            def fail(*args, **kwargs):
                update(*args, **kwargs)
                raise OSError("after private update")
            monkeypatch.setattr(g, "update_index", fail)
        with pytest.raises((n.Blocked, OSError)):
            n.stage_candidate(g, handle, choice)
    assert canonical == (root / METADATA / "index").read_bytes()


def test_dormancy_and_no_executable_manifest():
    source = Path(n.__file__)
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert getattr(node, "module", "") not in ("subprocess", "pwd", "grp")
            assert not any(a.name in ("subprocess", "pwd", "grp") for a in getattr(node, "names", ()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("eval", "exec")
    for path in source.parent.glob("*.py"):
        if path == source:
            continue
        t = ast.parse(path.read_text())
        for node in ast.walk(t):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "native_metadata"
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[-1] != "native_metadata" for a in node.names)


@pytest.fixture
def linux_root():
    if sys.platform != "linux":
        pytest.skip("Root-capable Linux ownership proof is mandatory separately")
    assert os.geteuid() == 0, "root-capable generic runner required"


def exact(root, m):
    # Independent stat/read oracle; no product observations as expected values.
    for r in m.entries:
        s = os.stat(os.path.join(os.fsencode(root), r.path), follow_symlinks=False)
        assert (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) == (r.uid, r.gid, r.mode)
        assert stat.S_ISREG(s.st_mode) if r.kind == "file" else stat.S_ISDIR(s.st_mode)


def materialize(g, target, m, tree):
    assert not (target / "homeassistant").exists()
    for e in tree:
        if e.path not in m.by_path or m.by_path[e.path].kind != "file":
            continue
        full = os.path.join(os.fsencode(target), e.path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as sink:
            g.copy_blob(e.oid, sink)
    b = binding(target, g.policy)
    selected = tuple(r.path for r in m.entries if r.kind == "file")
    parents = n.ancestors(selected)
    before = n.observe(b, m, selected, approved_parents=parents)
    result = n.restore(b, m, tree, selected, before, approved_parents=parents)
    assert result.uncertain == () and result.unattempted == ()
    exact(target, m)
    for r in m.entries:
        if r.kind == "file":
            data = Path(os.fsdecode(os.path.join(os.fsencode(target), r.path))).read_bytes()
            assert oid(data) == r.oid


def test_linux_historical_delete_revert_owner_bits_and_parents(tmp_path, linux_root):
    root = tmp_path.resolve()
    g, p, es, ignored = initialize(root)
    for path, uid, gid, mode in [(b"homeassistant", 230001, 230002, 0o3750), (b"homeassistant/p", 230003, 230004, 0o2710), (es[0].path, 230005, 230006, 0o6750), (es[1].path, 230007, 230008, 0o1640)]:
        os.chown(os.path.join(os.fsencode(root), path), uid, gid)
        os.chmod(os.path.join(os.fsencode(root), path), mode)
    es = (entry(es[0].path, mode=b"100755"), es[1])
    m = n.capture(binding(root, p), es)
    write(root, n.MANIFEST_PATH, n.encode(m, policy=p))
    git(root, "add", "-f", "--", "homeassistant", os.fsdecode(n.MANIFEST_PATH))
    git(root, "commit", "-m", "historical numeric identity and full bits")
    historical = git(root, "rev-parse", "HEAD").strip().decode()
    shutil.rmtree(root / "homeassistant")
    assert not (root / "homeassistant/p").exists()
    git(root, "rm", "-r", "--cached", "--", "homeassistant")
    write(root, n.MANIFEST_PATH, n.encode(n.Manifest(()), policy=p))
    git(root, "add", "-f", "--", os.fsdecode(n.MANIFEST_PATH))
    git(root, "commit", "-m", "physical deletion")
    deletion = git(root, "rev-parse", "HEAD").strip().decode()
    git(root, "revert", "--no-edit", deletion)
    restored_tree = g.tree_entries(git(root, "rev-parse", "HEAD^{tree}").strip().decode())
    sink = io.BytesIO()
    g.copy_blob(next(e.oid for e in restored_tree if e.path == n.MANIFEST_PATH), sink)
    transported = n.decode(sink.getvalue(), policy=p)
    assert transported == m  # Assertion only; restoration input came from Git.
    shutil.rmtree(root / "homeassistant")
    assert not (root / "homeassistant").exists()
    materialize(g, root, transported, restored_tree)
    # Ownership-only change starts at the final full mode, requiring post-chown chmod.
    leaf = root / "homeassistant/p/a"
    os.chown(leaf, 0, 0)
    os.chmod(leaf, 0o6750)
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o6750
    b = binding(root, p)
    before = n.observe(b, m, (es[0].path,))
    n.restore(b, m, restored_tree, (es[0].path,), before)
    assert (leaf.stat().st_uid, leaf.stat().st_gid, stat.S_IMODE(leaf.stat().st_mode)) == (230005, 230006, 0o6750)
    assert historical != deletion


def external_git(*args):
    env = {k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *map(str,args)], env=env, check=True, capture_output=True, timeout=30).stdout


def test_linux_independent_git_transport_and_fresh_clone(tmp_path, linux_root):
    publisher = tmp_path.resolve() / "publisher"
    publisher.mkdir()
    g, p, es, m = initialize(publisher)
    os.chown(publisher / "homeassistant/p/a", 231001, 231002)
    os.chmod(publisher / "homeassistant/p/a", 0o6740)
    os.chown(publisher / "homeassistant/p", 231003, 231004)
    os.chmod(publisher / "homeassistant/p", 0o3710)
    es = (entry(es[0].path, mode=b"100755"), es[1])
    m = n.capture(binding(publisher, p), es)
    git(publisher, "add", "--", "homeassistant")
    write(publisher, n.MANIFEST_PATH, n.encode(m, policy=p))
    git(publisher, "add", "-f", "--", os.fsdecode(n.MANIFEST_PATH))
    git(publisher, "commit", "-m", "transport metadata")
    remote = tmp_path.resolve() / "remote.git"
    external_git("clone", "--bare", "--no-hardlinks", publisher / METADATA, remote)
    destination = tmp_path.resolve() / "destination"
    external_git("clone", "--no-hardlinks", remote, destination)
    (destination / "data/native").mkdir(parents=True, exist_ok=True)
    shutil.move(destination / ".git", destination / METADATA)
    git(publisher, "rm", "-r", "--", "homeassistant")
    write(publisher, n.MANIFEST_PATH, n.encode(n.Manifest(()), policy=p))
    git(publisher, "add", "-f", "--", os.fsdecode(n.MANIFEST_PATH))
    git(publisher, "commit", "-m", "delete for transport")
    deletion = git(publisher, "rev-parse", "HEAD").strip().decode()
    git(publisher, "push", str(remote), "HEAD:refs/heads/fixture")
    git(destination, "fetch", "origin")
    git(destination, "reset", "--hard", "origin/fixture")
    assert not (destination / "homeassistant").exists()
    git(publisher, "revert", "--no-edit", deletion)
    git(publisher, "push", str(remote), "HEAD:refs/heads/fixture")
    git(destination, "fetch", "origin")
    # Fetch does not reuse or materialize deleted inodes.
    dest_g = NativeGit(destination, policy=p)
    tree = dest_g.tree_entries(git(destination, "rev-parse", "origin/fixture^{tree}").strip().decode())
    def from_git(adapter, entries):
        sink = io.BytesIO()
        adapter.copy_blob(next(e.oid for e in entries if e.path == n.MANIFEST_PATH), sink)
        return n.decode(sink.getvalue(), policy=p)
    transported = from_git(dest_g, tree)
    assert transported == m
    materialize(dest_g, destination, transported, tree)
    fresh = tmp_path.resolve() / "fresh"
    external_git("clone", "--no-checkout", "--no-hardlinks", remote, fresh)
    (fresh / "data/native").mkdir(parents=True)
    shutil.move(fresh / ".git", fresh / METADATA)
    fresh_g = NativeGit(fresh, policy=p)
    fresh_tree = fresh_g.tree_entries(git(fresh, "rev-parse", "HEAD^{tree}").strip().decode())
    assert not (fresh / METADATA / "objects/info/alternates").exists()
    assert not (destination / METADATA / "objects/info/alternates").exists()
    materialize(fresh_g, fresh, from_git(fresh_g, fresh_tree), fresh_tree)


def test_linux_nonprivileged_refusal(tmp_path, linux_root):
    root = tmp_path.resolve()
    root.chmod(0o755)
    # pytest's containing directory is root-only; child uses an independently owned /tmp tree.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="native-metadata-unprivileged-") as owned:
        path = Path(owned)
        path.chmod(0o755)
        write(path, entry().path)
        os.chown(path / "homeassistant/p/a", 65534, 65534)
        m = manifest(uid=230001, gid=230002)
        b = binding(path)
        before = n.observe(b, m, (entry().path,))
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
                try:
                    n.restore(b, m, (entry(),), (entry().path,), before)
                except n.RestoreFailure as exc:
                    os._exit(0 if isinstance(exc.cause, PermissionError) and exc.effects.uncertain else 2)
                os._exit(3)
            except BaseException:
                os._exit(4)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert (path / "homeassistant/p/a").stat().st_uid == 65534


def test_linux_many_files_low_descriptor_limit(tmp_path, linux_root):
    root = tmp_path.resolve()
    es = tuple(entry(b"homeassistant/p/file%03d" % i, b"A") for i in range(120))
    for e in es:
        write(root, e.path)
    pid = os.fork()
    if pid == 0:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            b = binding(root)
            m = n.capture(b, es)
            before = n.observe(b, m, (e.path for e in es))
            n.restore(b, m, es, (e.path for e in es), before)
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize("mutation", ["append", "truncate", "mode", "leaf", "parent"])
def test_streamed_capture_observable_races(materialized, monkeypatch, mutation):
    root, b, es, m = materialized
    read = os.read
    fired = False
    def race(fd, size):
        nonlocal fired
        data = read(fd, size)
        if data and not fired:
            fired = True
            path = root / "homeassistant/p/a"
            if mutation == "append":
                with path.open("ab") as f:
                    f.write(b"grow")
            elif mutation == "truncate":
                path.write_bytes(b"")
            elif mutation == "mode":
                path.chmod(0o600)
            elif mutation == "leaf":
                path.rename(path.with_name("old"))
                path.write_bytes(b"A")
            else:
                path.parent.rename(path.parent.with_name("old"))
                write(root, es[0].path)
        return data
    monkeypatch.setattr(os, "read", race)
    with pytest.raises(n.Blocked, match="changed"):
        n.capture(b, es)


def test_capture_later_read_cannot_stale_earlier_leaf(materialized, monkeypatch):
    root, b, es, m = materialized
    second = entry(b"homeassistant/p/b", b"B")
    write(root, second.path, b"B")
    read = os.read
    fired = False
    def race(fd, size):
        nonlocal fired
        data = read(fd, size)
        if data == b"B" and not fired:
            fired = True
            os.chmod(root / "homeassistant/p/a", 0o600)
        return data
    monkeypatch.setattr(os, "read", race)
    with pytest.raises(n.Blocked, match="final"):
        n.capture(b, es + (second,))


def test_fifo_substituted_immediately_before_nonblocking_open(materialized, monkeypatch):
    root, b, es, m = materialized
    opener = os.open
    fired = False
    def race(path, flags, *args, **kwargs):
        nonlocal fired
        if path == b"a" and not fired:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            fired = True
            (root / "homeassistant/p/a").unlink()
            os.mkfifo(root / "homeassistant/p/a")
        return opener(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", race)
    with pytest.raises(n.Blocked):
        n.observe(b, m, (es[0].path,))
    assert fired


def test_noop_successful_chown_is_not_exact_completion(materialized, monkeypatch):
    root, b, es, m = materialized
    target = replace(m, es[0].path, uid=os.getuid()+1)
    before = n.observe(b, target, (es[0].path,))
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, target, es, (es[0].path,), before)
    assert "attribute-readback" in str(failure.value.cause)
    assert failure.value.effects.uncertain == (es[0].path,)


@pytest.mark.parametrize("fault", ["close", "fstat"])
def test_post_effect_close_or_readback_never_completes(materialized, monkeypatch, fault):
    root, b, es, m = materialized
    target = replace(m, es[0].path, mode=0o600)
    before = n.observe(b, target, (es[0].path,))
    chmod, close, fstat = os.fchmod, os.close, os.fstat
    fired = False
    failed = False
    def mode(fd, value):
        nonlocal fired
        chmod(fd, value)
        fired = True
    def closing(fd):
        nonlocal failed
        close(fd)
        if fired and not failed:
            failed = True
            raise OSError("final close")
    def readback(fd):
        nonlocal failed
        if fired and not failed:
            failed = True
            raise OSError("readback failed")
        return fstat(fd)
    monkeypatch.setattr(os, "fchmod", mode)
    monkeypatch.setattr(os, "close" if fault == "close" else "fstat", closing if fault == "close" else readback)
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, target, es, (es[0].path,), before)
    assert failure.value.effects.completed == ()
    assert failure.value.effects.uncertain == (es[0].path,)
    assert stat.S_IMODE((root / "homeassistant/p/a").stat().st_mode) == 0o600


def test_post_chown_namespace_substitution_stops_before_chmod(materialized, monkeypatch):
    root, b, es, m = materialized
    target = replace(m, es[0].path, uid=os.getuid()+1)
    calls = []
    outside = root / "outside-sentinel"
    outside.write_bytes(b"outside")
    before = n.observe(b, target, (es[0].path,))
    def chown(fd, uid, gid):
        leaf = root / "homeassistant/p/a"
        leaf.rename(leaf.with_name("prior"))
        leaf.symlink_to(outside)
    monkeypatch.setattr(os, "fchown", chown)
    monkeypatch.setattr(os, "fchmod", lambda *a: calls.append(a))
    with pytest.raises(n.RestoreFailure) as failure:
        n.restore(b, target, es, (es[0].path,), before)
    assert calls == [] and outside.read_bytes() == b"outside"
    assert failure.value.effects.uncertain == (es[0].path,)


def test_single_leaf_conflict_is_one_atomic_row():
    base = manifest()
    clash = n.merge(base, replace(base, entry().path, uid=1), replace(base, entry().path, mode=0o600))
    assert tuple(c.path for c in clash.conflicts) == (entry().path,)


def test_observation_rejects_bound_root_replacement(materialized):
    root, b, es, m = materialized
    before = n.observe(b, m, (es[0].path,))
    moved = root.with_name(root.name + "-owned-prior")
    root.rename(moved)
    try:
        root.mkdir()
        write(root, es[0].path)
        with pytest.raises(n.RestoreFailure) as failure:
            n.restore(b, m, es, (es[0].path,), before)
        assert failure.value.effects.attempted == ()
    finally:
        shutil.rmtree(moved)


def test_linux_unassigned_numeric_identity_is_supported(materialized, linux_root):
    import pwd
    import grp
    root, b, es, m = materialized
    uid = gid = n.MAX_ID
    with pytest.raises(KeyError):
        pwd.getpwuid(uid)
    with pytest.raises(KeyError):
        grp.getgrgid(gid)
    target = replace(m, es[0].path, uid=uid, gid=gid, mode=0o7640)
    before = n.observe(b, target, (es[0].path,))
    n.restore(b, target, es, (es[0].path,), before)
    s = (root / "homeassistant/p/a").stat()
    assert (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) == (uid, gid, 0o7640)


def test_explicit_empty_baseline_bootstrap_and_reviewed_new_parents(tmp_path):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    canonical = (root / METADATA / "index").read_bytes()
    empty = n.Manifest(())
    fresh = n.capture(binding(root, p), (es[0],))
    with g.isolated_index(None, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        blob = g.write_blob_stream(n.encode(empty, policy=p))
        g.update_index(handle, (IndexEntry(n.MANIFEST_PATH, b"100644", blob.oid, 0),))
        n.validate(empty, g.index_entries(handle), policy=p)
        g.write_tree(handle, allowed_paths=(n.MANIFEST_PATH,))
        with pytest.raises(n.Blocked, match="review-required"):
            n.select(empty, fresh, (es[0].path,), policy=p)
        chosen = n.select(empty, fresh, (es[0].path,), approved_parents=n.ancestors((es[0].path,)), policy=p)
        result = n.stage_candidate(g, handle, chosen, entries=(IndexEntry(es[0].path, es[0].mode, es[0].oid, 0),))
        n.validate(chosen.manifest, g.tree_entries(result), policy=p)
    assert canonical == (root / METADATA / "index").read_bytes()


def test_invalid_last_row_restore_does_not_write_valid_first_row(materialized, monkeypatch):
    root, b, es, m = materialized
    second = entry(b"homeassistant/p/z", b"Z")
    write(root, second.path, b"Z")
    complete = n.capture(b, es + (second,))
    before = n.observe(b, complete, (es[0].path, second.path))
    target = replace(replace(complete, es[0].path, mode=0o600), second.path, uid=-1)
    calls = []
    monkeypatch.setattr(os, "fchmod", lambda *a: calls.append(a))
    monkeypatch.setattr(os, "fchown", lambda *a: calls.append(a))
    with pytest.raises(n.Blocked):
        n.restore(b, target, es + (second,), (es[0].path, second.path), before)
    assert calls == []
    assert stat.S_IMODE((root / "homeassistant/p/a").stat().st_mode) == complete.by_path[es[0].path].mode


def test_observation_inputs_copy_sequence_values(materialized):
    root, b, es, m = materialized
    rows = list(m.entries)
    copied = n.Manifest(rows)
    rows.clear()
    assert copied == m
    mounts, confirmed = list(b.mounts), list(b.confirmed_mounts)
    copied_binding = d.replace(b, mounts=mounts, confirmed_mounts=confirmed)
    mounts.clear()
    confirmed.clear()
    assert copied_binding == b
    before = n.observe(b, m, (es[0].path,))
    rows = list(before.entries)
    copied_before = n.Before(b, rows)
    rows.clear()
    assert copied_before == before
    choice = n.select(m, m, (es[0].path,), approved_parents=(b"homeassistant/p",))
    dependents = list(choice.parents[0].dependents)
    review = d.replace(choice.parents[0], dependents=dependents)
    reviews, selected = [review], list(choice.selected)
    copied_choice = d.replace(choice, parents=reviews, selected=selected)
    dependents.clear()
    reviews.clear()
    selected.clear()
    assert copied_choice == choice


@pytest.mark.parametrize("width", [40, 64])
def test_stage_semantic_baseline_order_and_genuine_stale_data(tmp_path, width):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root, width=width)
    reordered = n.Manifest(tuple(reversed(m.entries)))
    n.validate(reordered, es, width=width, policy=p)
    assert n.encode(reordered, width=width, policy=p) == n.encode(m, width=width, policy=p)
    base = git(root, "rev-parse", "HEAD^{tree}").strip().decode()
    canonical = (root / METADATA / "index").read_bytes()
    with g.isolated_index(base, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        choice = n.select(reordered, reordered, (es[0].path,), width=width, policy=p)
        tree = n.stage_candidate(g, handle, choice, width=width)
        n.validate(choice.manifest, g.tree_entries(tree), width=width, policy=p)
        stale = replace(reordered, es[0].path, uid=reordered.by_path[es[0].path].uid+1)
        stale_choice = n.select(stale, stale, (es[0].path,), width=width, policy=p)
        with pytest.raises(n.Blocked, match="stale"):
            n.stage_candidate(g, handle, stale_choice, width=width)
    assert canonical == (root / METADATA / "index").read_bytes()


@pytest.mark.parametrize("mutation", ["unselected-uid", "unselected-gid", "unselected-special-mode", "unreviewed-parent", "missing-review", "duplicate-review", "wrong-before", "wrong-after", "boolean-before", "boolean-after", "missing-dependent", "duplicate-dependent", "foreign-dependent", "foreign-parent"])
def test_stage_revalidates_all_metadata_deltas_and_parent_review(tmp_path, monkeypatch, mutation):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    parent = b"homeassistant/p"
    fresh = replace(m, parent, mode=0o700)
    valid = n.select(m, fresh, (es[0].path,), approved_parents=(parent,), policy=p)
    plain = n.select(m, m, (es[0].path,), policy=p)
    review = valid.parents[0]
    if mutation.startswith("unselected"):
        facet = {"unselected-uid": dict(uid=m.by_path[es[1].path].uid+7), "unselected-gid": dict(gid=m.by_path[es[1].path].gid+7), "unselected-special-mode": dict(mode=0o4644)}[mutation]
        invalid = d.replace(plain, manifest=replace(m, es[1].path, **facet))
    elif mutation == "unreviewed-parent":
        invalid = d.replace(plain, manifest=fresh)
    elif mutation == "missing-review":
        invalid = d.replace(valid, parents=())
    elif mutation == "duplicate-review":
        invalid = d.replace(valid, parents=(review, review))
    elif mutation == "wrong-before":
        invalid = d.replace(valid, parents=(d.replace(review, before=d.replace(review.before, gid=review.before.gid+1)),))
    elif mutation == "wrong-after":
        invalid = d.replace(valid, parents=(d.replace(review, after=d.replace(review.after, mode=0o750)),))
    elif mutation == "boolean-before":
        invalid = d.replace(valid, parents=(d.replace(review, before=d.replace(review.before, uid=bool(review.before.uid))),))
    elif mutation == "boolean-after":
        invalid = d.replace(valid, parents=(d.replace(review, after=d.replace(review.after, uid=bool(review.after.uid))),))
    elif mutation == "missing-dependent":
        invalid = d.replace(valid, parents=(d.replace(review, dependents=(es[0].path,)),))
    elif mutation == "duplicate-dependent":
        invalid = d.replace(valid, parents=(d.replace(review, dependents=review.dependents + (es[0].path,)),))
    elif mutation == "foreign-dependent":
        invalid = d.replace(valid, parents=(d.replace(review, dependents=review.dependents + (b"homeassistant/foreign",)),))
    else:
        invalid = d.replace(valid, parents=(review, d.replace(review, path=b"homeassistant/foreign")))
    base = git(root, "rev-parse", "HEAD^{tree}").strip().decode()
    canonical = (root / METADATA / "index").read_bytes()
    with g.isolated_index(base, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        initial = g.index_entries(handle)
        mutations = []
        monkeypatch.setattr(g, "write_blob_stream", lambda *a, **kw: mutations.append("blob"))
        monkeypatch.setattr(g, "update_index", lambda *a, **kw: mutations.append("index"))
        with pytest.raises(n.Blocked):
            n.stage_candidate(g, handle, invalid)
        assert mutations == []
        assert initial == g.index_entries(handle)
    assert canonical == (root / METADATA / "index").read_bytes()


def test_reviewed_shape_selection_and_unreviewed_or_partial_reverse_refusal():
    old = manifest((entry(b"homeassistant/p", b"P"),))
    new = manifest((entry(),))
    choice = n.select(old, new, (b"homeassistant/p", entry().path), approved_parents=(b"homeassistant/p",))
    assert choice.manifest == new
    assert choice.parents[0].before == old.by_path[b"homeassistant/p"]
    assert choice.parents[0].after == new.by_path[b"homeassistant/p"]
    assert choice.parents[0].dependents == (entry().path,)
    with pytest.raises(n.Blocked, match="review-required"):
        n.select(old, new, (b"homeassistant/p", entry().path))
    reverse = n.select(new, old, (entry().path, b"homeassistant/p"))
    assert reverse.manifest == old and reverse.parents == ()
    with pytest.raises(n.Blocked):
        n.select(new, old, (b"homeassistant/p",))  # Unselected descendant cannot disappear.


def test_unrelated_existing_parent_review_is_refused():
    es = (entry(), entry(b"homeassistant/q/b", b"B"))
    m = manifest(es)
    with pytest.raises(n.Blocked, match="unrelated"):
        n.select(m, replace(m, b"homeassistant/q", uid=1), (es[0].path,), approved_parents=(b"homeassistant/q",))


@pytest.mark.parametrize("direction", ["file-to-directory", "directory-to-file"])
def test_nativegit_both_reviewed_shape_transitions_preserve_unselected(tmp_path, direction):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    g.write_blob_stream(b"P")
    keep = entry(b"homeassistant/q/keep", b"B")
    file_entry = entry(b"homeassistant/p", b"P")
    directory_entry = entry()
    file_shape = manifest((file_entry, keep))
    directory_shape = manifest((directory_entry, keep))
    before, target = (file_shape, directory_shape) if direction == "file-to-directory" else (directory_shape, file_shape)
    new_entry, deleted = (directory_entry, file_entry) if direction == "file-to-directory" else (file_entry, directory_entry)
    approved = (b"homeassistant/p",) if direction == "file-to-directory" else ()
    choice = n.select(before, target, (file_entry.path, directory_entry.path), approved_parents=approved, policy=p)
    canonical = (root / METADATA / "index").read_bytes()
    original_head = git(root, "rev-parse", "HEAD").strip()
    with g.isolated_index(None, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        seed = g.write_blob_stream(n.encode(before, policy=p))
        seed_entries = tuple(IndexEntry(r.path, b"100644", r.oid, 0) for r in before.entries if r.kind == "file")
        g.update_index(handle, seed_entries + (IndexEntry(n.MANIFEST_PATH, b"100644", seed.oid, 0),))
        result = n.stage_candidate(g, handle, choice, entries=(IndexEntry(new_entry.path, new_entry.mode, new_entry.oid, 0),), removals=(deleted.path,))
        complete = g.tree_entries(result)
        n.validate(target, complete, policy=p)
        assert choice.manifest.by_path[keep.path] == before.by_path[keep.path]
        assert choice.manifest.by_path[b"homeassistant/q"] == before.by_path[b"homeassistant/q"]
        assert next(e.oid for e in complete if e.path == keep.path) == keep.oid
    assert canonical == (root / METADATA / "index").read_bytes()
    assert original_head == git(root, "rev-parse", "HEAD").strip()


def test_shape_transition_failure_between_private_updates_has_no_result(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    old = manifest((entry(b"homeassistant/p", b"P"),))
    target = manifest((entry(),))
    g.write_blob_stream(b"P")
    choice = n.select(old, target, (b"homeassistant/p", entry().path), approved_parents=(b"homeassistant/p",), policy=p)
    canonical = (root / METADATA / "index").read_bytes()
    original_head = git(root, "rev-parse", "HEAD").strip()
    with g.isolated_index(None, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        seed = g.write_blob_stream(n.encode(old, policy=p))
        g.update_index(handle, (IndexEntry(b"homeassistant/p", b"100644", oid(b"P"), 0), IndexEntry(n.MANIFEST_PATH, b"100644", seed.oid, 0)))
        update = g.update_index
        calls = []
        def failing(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise OSError("between private shape updates")
            return update(*args, **kwargs)
        monkeypatch.setattr(g, "update_index", failing)
        with pytest.raises(OSError, match="between"):
            n.stage_candidate(g, handle, choice, entries=(IndexEntry(entry().path, b"100644", entry().oid, 0),), removals=(b"homeassistant/p",))
        assert len(calls) == 2 and calls[0] == {"removals": (b"homeassistant/p",)}
        assert tuple(e.path for e in g.index_entries(handle)) == (n.MANIFEST_PATH,)
    assert canonical == (root / METADATA / "index").read_bytes()
    assert original_head == git(root, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("deleted", ["one", "all"])
def test_staged_deletion_prunes_only_orphan_rows(tmp_path, deleted):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    remaining = (es[1],) if deleted == "one" else ()
    selected = (es[0].path,) if deleted == "one" else tuple(e.path for e in es)
    fresh = manifest(remaining, uid=os.getuid(), gid=os.getgid())
    # Fresh values are irrelevant for retained unselected baseline records.
    choice = n.select(m, fresh, selected, policy=p)
    canonical = (root / METADATA / "index").read_bytes()
    base = git(root, "rev-parse", "HEAD^{tree}").strip().decode()
    with g.isolated_index(base, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        tree = n.stage_candidate(g, handle, choice, removals=selected)
        n.validate(choice.manifest, g.tree_entries(tree), policy=p)
    if remaining:
        assert choice.manifest.by_path[es[1].path] == m.by_path[es[1].path]
        assert choice.manifest.by_path[b"homeassistant/p"] == m.by_path[b"homeassistant/p"]
    else:
        assert choice.manifest == n.Manifest(())
        with pytest.raises(n.Blocked, match="unrelated"):
            n.select(m, fresh, selected, approved_parents=(b"homeassistant/p",), policy=p)
    assert (root / "homeassistant/p").is_dir()
    assert (root / "homeassistant/p/a").read_bytes() == b"A"
    assert canonical == (root / METADATA / "index").read_bytes()


def test_reviewed_metadata_only_staging_and_semantic_dependents(tmp_path):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    target = replace(replace(m, es[0].path, uid=m.by_path[es[0].path].uid+7), b"homeassistant/p", mode=0o2700)
    choice = n.select(m, target, (es[0].path,), approved_parents=(b"homeassistant/p",), policy=p)
    choice = d.replace(choice, parents=(d.replace(choice.parents[0], dependents=tuple(reversed(choice.parents[0].dependents))),))
    base = git(root, "rev-parse", "HEAD^{tree}").strip().decode()
    canonical = (root / METADATA / "index").read_bytes()
    with g.isolated_index(base, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        tree = n.stage_candidate(g, handle, choice)
        complete = g.tree_entries(tree)
        n.validate(target, complete, policy=p)
        sink = io.BytesIO()
        g.copy_blob(next(e.oid for e in complete if e.path == n.MANIFEST_PATH), sink)
        assert n.decode(sink.getvalue(), policy=p) == target
        assert tuple((e.path,e.oid) for e in complete if e.path != n.MANIFEST_PATH) == tuple((e.path,e.oid) for e in es)
    assert canonical == (root / METADATA / "index").read_bytes()


def test_consistent_review_of_foreign_existing_parent_still_blocks(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    g, p, es, m = initialize(root)
    keep = entry(b"homeassistant/q/keep", b"B")
    before = manifest((es[0], keep))
    candidate = replace(before, b"homeassistant/q", gid=7)
    plain = n.select(before, before, (es[0].path,), policy=p)
    review = n.ParentReview(b"homeassistant/q", before.by_path[b"homeassistant/q"], candidate.by_path[b"homeassistant/q"], (keep.path,))
    invalid = d.replace(plain, manifest=candidate, parents=(review,))
    canonical = (root / METADATA / "index").read_bytes()
    with g.isolated_index(None, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())) as handle:
        seed = g.write_blob_stream(n.encode(before, policy=p))
        g.update_index(handle, (IndexEntry(es[0].path, b"100644", es[0].oid, 0), IndexEntry(keep.path, b"100644", keep.oid, 0), IndexEntry(n.MANIFEST_PATH, b"100644", seed.oid, 0)))
        initial = g.index_entries(handle)
        effects = []
        monkeypatch.setattr(g, "write_blob_stream", lambda *a, **kw: effects.append("blob"))
        monkeypatch.setattr(g, "update_index", lambda *a, **kw: effects.append("index"))
        with pytest.raises(n.Blocked, match="unrelated"):
            n.stage_candidate(g, handle, invalid)
        assert effects == [] and initial == g.index_entries(handle)
    assert canonical == (root / METADATA / "index").read_bytes()
