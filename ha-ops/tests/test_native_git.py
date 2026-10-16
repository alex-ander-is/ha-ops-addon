"""Dormant native plumbing: real Git, Unix bytes, CAS and addressed resources."""
import ast
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import native_git as n
from native_topology_helpers import git, METADATA


def ids():
    return {"operation_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4())}


def write(root, path, data=b"fixture\n"):
    full = os.path.join(os.fsencode(root), os.fsencode(path))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as stream:
        stream.write(data)


@pytest.fixture
def repo(tmp_path):
    # resolve macOS's /var and /tmp aliases; the adapter intentionally refuses them.
    root = tmp_path.resolve()
    (root / "data/native").mkdir(parents=True)
    git(root, "init", "--initial-branch=fixture")
    write(root, b"homeassistant/a", b"A\0\r\ntrailing ")
    write(root, b"homeassistant/b", b"B")
    write(root, b".gitignore", b"/data/\n")
    git(root, "add", "--", "homeassistant/a", "homeassistant/b", ".gitignore")
    git(root, "commit", "-m", "fixture")
    return root, n.NativeGit(root)


def tree(root):
    return git(root, "rev-parse", "HEAD^{tree}").strip().decode()


def head(root):
    return git(root, "rev-parse", "HEAD").strip().decode()


def canonical(root):
    metadata = root / METADATA
    return ((metadata / "index").read_bytes(), (metadata / "HEAD").read_bytes(),
            tuple(sorted((os.fsencode(p.relative_to(root)), p.read_bytes(), p.stat().st_mode) for p in (root / "homeassistant").rglob("*") if p.is_file())))


def child(root, code, *arguments):
    source = str(Path(n.__file__).parent)
    prefix = "import sys,os,time,json,uuid;sys.path.insert(0," + repr(source) + ");import native_git as n;g=n.NativeGit(sys.argv[1]);"
    return subprocess.Popen([sys.executable, "-c", prefix + code, str(root), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_line(process):
    # A deadline keeps genuine interruption failures from hanging the suite.
    import selectors
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(15):
            process.kill()
            out, error = process.communicate()
            pytest.fail("child did not reach boundary: " + repr((out, error)))
        data = process.stdout.readline()
        if not data:
            out, error = process.communicate(timeout=5)
            pytest.fail("child failed: " + repr((out, error)))
        return data


@pytest.mark.parametrize("data", [b"x", b"\0", b"? \0", b"9 path\0", b"1 M. N... 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b"\0",
                                   b"2 R. N... 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" R100 path\0"])
def test_status_parser_refuses_truncation_unknown_and_missing_rename_partner(data):
    with pytest.raises(n.InvalidInput):
        n.parse_status(data)


@pytest.mark.parametrize("parser", [n.parse_index, n.parse_tree])
@pytest.mark.parametrize("data", [b"x", b"\0", b"100644 blob short\tp\0", b"100644 " + b"a" * 40 + b" 8\tp\0", b"100644 " + b"a" * 40 + b" 0\t../x\0"])
def test_structured_parsers_fail_closed(parser, data):
    with pytest.raises(n.InvalidInput):
        parser(data)


def test_status_rename_conflict_and_byte_identity(repo):
    root, adapter = repo
    git(root, "mv", "homeassistant/a", "homeassistant/renamed \t[?]*")
    write(root, b"homeassistant/renamed \t[?]*", b"unstaged")
    status = adapter.status()
    assert len(status) == 1
    assert status[0].kind == b"2" and status[0].original_path == b"homeassistant/a"
    assert status[0].path == b"homeassistant/renamed \t[?]*" and status[0].xy == b"RM"
    # Actual nonzero conflict stages in the canonical index remain diagnostics.
    oid = git(root, "rev-parse", "HEAD:homeassistant/b").strip()
    payload = b"0 " + b"0" * 40 + b"\thomeassistant/b\0" + b"".join(b"100644 " + oid + b" " + str(stage).encode() + b"\thomeassistant/b\0" for stage in (1, 2, 3))
    subprocess.run(["git", "--git-dir=" + str(root / METADATA), "--work-tree=" + str(root), "update-index", "-z", "--index-info"], input=payload, check=True)
    before = canonical(root)
    assert any(entry.kind == b"u" for entry in adapter.status())
    assert {entry.stage for entry in adapter.index_entries() if entry.path == b"homeassistant/b"} == {1, 2, 3}
    with adapter.isolated_index(tree(root), **ids()) as handle:
        assert all(e.stage == 0 for e in adapter.index_entries(handle))
    assert canonical(root) == before


def test_complete_linux_byte_set_roundtrips_through_index_tree_and_worktree(repo):
    root, adapter = repo
    names = [b"tab\tname", b"spaces ", b"-dash", b":(glob)*", b"back\\slash", b"[a]*?", b"Case", b"case", b"\xff", "é".encode(), "e\u0301".encode()]
    if sys.platform == "darwin":
        names = [p for p in names if p not in (b"Case", b"\xff", "é".encode(), "e\u0301".encode())]
    paths = [b"homeassistant/" + name for name in names]
    blob = adapter.write_blob_stream(b"exact\0\r\n").oid
    before = canonical(root)
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(path, b"100644", blob, 0) for path in paths])
        assert {e.path for e in adapter.index_entries(handle)} == set(paths)
        oid = adapter.write_tree(handle, allowed_paths=paths)
        assert {e.path for e in adapter.tree_entries(oid)} == set(paths)
    commit = git(root, "commit-tree", oid, "-m", "byte fixture").strip().decode()
    with adapter.temporary_worktree(commit, paths=paths, **ids()) as handle:
        base = adapter.worktree_path(handle)
        for path in paths:
            with open(os.path.join(base, path), "rb") as stream:
                assert stream.read() == b"exact\0\r\n"
    assert adapter.resources() == ()
    assert canonical(root) == before


@pytest.mark.parametrize("line", [b"homeassistant/LF\nname", b"homeassistant/CR\rname"])
def test_line_breaks_diagnostic_but_no_selection_authority(repo, line):
    root, adapter = repo
    blob = adapter.write_blob_stream(b"line").oid
    with adapter.isolated_index(None, **ids()) as handle:
        before = adapter.index_entries(handle)
        for invoke in (lambda: adapter.update_index(handle, [n.IndexEntry(line, b"100644", blob, 0)]),
                       lambda: adapter.update_index(handle, removals=[line]), lambda: adapter.write_tree(handle, allowed_paths=[line])):
            with pytest.raises(n.InvalidInput, match="unsupported-line-break-name"):
                invoke()
            assert adapter.index_entries(handle) == before
    assert adapter.resources() == ()
    with pytest.raises(n.InvalidInput, match="unsupported-line-break-name"):
        with adapter.temporary_worktree(head(root), paths=[line], **ids()):
            pass
    write(root, line)
    git(root, "add", "--", os.fsdecode(line))
    assert line in {entry.path for entry in adapter.index_entries()}
    assert line in {entry.path for entry in adapter.status()}
    assert adapter.resources() == ()


def test_private_index_complete_base_preserves_unselected_content(repo):
    root, adapter = repo
    before = canonical(root)
    entries = adapter.tree_entries(tree(root))
    b = next(e for e in entries if e.path == b"homeassistant/b")
    changed = adapter.write_blob_stream(b"changed A").oid
    with adapter.isolated_index(tree(root), **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(b"homeassistant/a", b"100755", changed, 0)])
        with pytest.raises(n.InvalidInput, match="outside-allowed"):
            adapter.write_tree(handle, allowed_paths=[b"homeassistant/a"])
        oid = adapter.write_tree(handle, allowed_paths=[e.path for e in entries])
        actual = {e.path: e for e in adapter.tree_entries(oid)}
        assert actual[b.path].oid == b.oid
        assert actual[b"homeassistant/a"].oid == changed
        assert actual[b"homeassistant/a"].mode == b"100755"
        adapter.update_index(handle, removals=[b"homeassistant/a"])
        assert b"homeassistant/a" not in {e.path for e in adapter.index_entries(handle)}
    assert canonical(root) == before
    with pytest.raises(n.InvalidInput, match="closed"):
        adapter.index_entries(handle)


@pytest.mark.parametrize("bad", [b"120000", b"160000", b"040000"])
def test_complete_index_rejects_unselected_unsupported_modes(repo, bad):
    root, adapter = repo
    oid = head(root) if bad == b"160000" else adapter.write_blob_stream(b"target").oid
    if bad == b"040000":
        oid = tree(root)
    with adapter.isolated_index(None, **ids()) as handle:
        # Test-only adversarial candidate injection; public entrypoint refuses all three modes.
        with pytest.raises(n.InvalidInput):
            adapter.update_index(handle, [n.IndexEntry(b"homeassistant/bad", bad, oid, 0)])
        if bad == b"040000":
            return
        adapter._run(("update-index", "-z", "--index-info"), index=adapter._index(handle), input=bad + b" " + oid.encode() + b"\thomeassistant/bad\0")
        adapter._capture(handle)
        with pytest.raises(n.InvalidInput, match="unsupported-complete-index"):
            adapter.write_tree(handle, allowed_paths=[b"homeassistant/bad"])


def test_expected_old_required_absence_update_delete_and_packed_refs(repo):
    root, adapter = repo
    first, second = head(root), adapter.write_blob_stream(b"other").oid
    name = "refs/ha-ops/test"
    with pytest.raises(TypeError):
        adapter.update_ref(name, first)
    with pytest.raises(TypeError):
        adapter.delete_ref(name)
    assert adapter.read_ref(name).oid is None
    assert adapter.update_ref(name, first, expected_old=None).oid == first
    with pytest.raises(n.RefConflict):
        adapter.update_ref(name, second, expected_old=None)
    git(root, "pack-refs", "--all")
    assert adapter.read_ref(name).oid == first
    with pytest.raises(n.RefConflict):
        adapter.delete_ref(name, expected_old=second)
    assert adapter.update_ref(name, second, expected_old=first).oid == second
    with pytest.raises(n.RefConflict):
        adapter.update_ref(name, first, expected_old=first)
    assert adapter.delete_ref(name, expected_old=second).oid is None
    with pytest.raises(n.InvalidInput):
        adapter.delete_ref(name, expected_old=None)


@pytest.mark.parametrize("name", ["HEAD", "refs/x..y", "refs/x~", "refs/a\n", "--help", "refs/a^{tree}"])
def test_ref_names_do_not_accept_revision_or_option_syntax(repo, name):
    root, adapter = repo
    before = git(root, "show-ref")
    with pytest.raises((n.InvalidInput, n.GitFailure)):
        adapter.update_ref(name, head(root), expected_old=None)
    assert git(root, "show-ref") == before


def test_symbolic_ref_refused_and_pointed_ref_preserved(repo):
    root, adapter = repo
    git(root, "symbolic-ref", "refs/ha-ops/symbolic", "refs/heads/fixture")
    before = git(root, "show-ref")
    with pytest.raises(n.InvalidInput, match="symbolic"):
        adapter.update_ref("refs/ha-ops/symbolic", head(root), expected_old=None)
    assert git(root, "show-ref") == before


def test_two_process_ref_cas_has_one_winner(repo):
    root, adapter = repo
    original = head(root)
    adapter.update_ref("refs/ha-ops/race", original, expected_old=None)
    candidates = [adapter.write_blob_stream(value).oid for value in (b"one", b"two")]
    code = "\ntry:\n g.update_ref('refs/ha-ops/race',sys.argv[2],expected_old=sys.argv[3]);print('winner')\nexcept n.RefConflict:\n print('conflict')\n"
    processes = [child(root, code, oid, original) for oid in candidates]
    output = []
    for process in processes:
        out, error = process.communicate(timeout=15)
        assert process.returncode == 0, error
        output.append(out.strip())
    assert sorted(output) == [b"conflict", b"winner"]
    assert adapter.read_ref("refs/ha-ops/race").oid in candidates


def test_ref_timeout_reobserves_effect_without_replay(repo, monkeypatch):
    root, adapter = repo
    oid = head(root)
    original, calls = adapter._run, []
    def timeout(command, **kwargs):
        if command[0] == "update-ref":
            calls.append(command)
            original(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 1)
        return original(command, **kwargs)
    monkeypatch.setattr(adapter, "_run", timeout)
    with pytest.raises(n.RefUncertain) as failure:
        adapter.update_ref("refs/ha-ops/uncertain", oid, expected_old=None)
    assert failure.value.before.oid is None and failure.value.after.oid == oid
    assert len(calls) == 1


def test_blob_stream_bounds_short_sink_and_exact_binary(repo):
    root, adapter = repo
    content = (bytes(range(256)) + b"\0\r\n") * 13000 + b"trailing \r"
    class Producer(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= n.CHUNK
            return super().read(count)
    class Sink(io.BytesIO):
        def write(self, data):
            assert len(data) <= n.CHUNK
            return super().write(data[:17])
    result = adapter.write_blob_stream(Producer(content))
    assert result.size == len(content)
    expected = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    assert result.oid == expected
    sink = Sink()
    assert adapter.copy_blob(result.oid, sink).size == len(content)
    assert sink.getvalue() == content
    assert adapter.resources() == ()
    assert adapter.accounting()["temporary_bytes"] == 0


@pytest.mark.parametrize("kind", ["tree", "commit", "tag", "short", "uppercase"])
def test_blob_requires_exact_full_blob_type(repo, kind):
    root, adapter = repo
    oid = tree(root) if kind == "tree" else head(root)
    if kind == "tag":
        git(root, "tag", "-a", "test", "-m", "tag")
        oid = git(root, "rev-parse", "refs/tags/test").strip().decode()
    elif kind == "short":
        oid = oid[:8]
    elif kind == "uppercase":
        oid = oid.upper()
    with pytest.raises(n.InvalidInput):
        adapter.copy_blob(oid, io.BytesIO())


def test_late_producer_and_consumer_failures_cleanup_and_reap(repo, monkeypatch):
    root, adapter = repo
    def chunks():
        yield b"x" * 100000
        raise RuntimeError("producer-failed")
    with pytest.raises(RuntimeError, match="producer-failed"):
        adapter.write_blob_stream(chunks())
    assert adapter.resources() == ()
    oid = adapter.write_blob_stream(b"x" * 1000000).oid
    class Sink:
        def write(self, value):
            raise RuntimeError("consumer-failed")
    spawned, original = [], subprocess.Popen
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", record)
    with pytest.raises(RuntimeError, match="consumer-failed"):
        adapter.copy_blob(oid, Sink())
    assert all(p.poll() is not None for p in spawned)


def test_stream_timeout_and_stderr_drain_are_bounded(repo, monkeypatch):
    root, adapter = repo
    oid = adapter.write_blob_stream(b"original").oid
    original = adapter._argv
    children = []
    def argv(command, **kwargs):
        args, env = original(command, **kwargs)
        if tuple(command) == ("cat-file", "blob", oid):
            args = [sys.executable, "-c", "import os,time;os.write(2,b'e'*200000);os.write(1,b'x'*100000);time.sleep(5)"]
        return args, env
    monkeypatch.setattr(adapter, "_argv", argv)
    adapter.timeout = .2
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.copy_blob(oid, io.BytesIO())


def test_sha256_object_format_fixture(tmp_path):
    root = tmp_path.resolve()
    (root / "data/native").mkdir(parents=True)
    git(root, "init", "--object-format=sha256", "--initial-branch=fixture")
    adapter = n.NativeGit(root)
    blob = adapter.write_blob_stream(b"sha256\0")
    assert len(blob.oid) == 64
    sink = io.BytesIO()
    adapter.copy_blob(blob.oid, sink)
    assert sink.getvalue() == b"sha256\0"
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(b"homeassistant/file", b"100644", blob.oid, 0)])
        oid = adapter.write_tree(handle, allowed_paths=[b"homeassistant/file"])
        assert len(oid) == 64
        assert adapter.tree_entries(oid)[0].oid == blob.oid


@pytest.mark.parametrize("route", ["objects", "refs", "logs", "worktrees", "objects/ab", "refs/ha-ops", "logs/refs"])
def test_nested_metadata_symlink_routes_refused_before_effect(repo, tmp_path, route):
    root, adapter = repo
    metadata = root / METADATA
    target = tmp_path / "foreign"
    target.mkdir()
    (target / "sentinel").write_bytes(b"foreign")
    path = metadata / route
    if path.exists():
        # Empty routing directories only; existing objects live elsewhere.
        path.rename(metadata / (route.replace("/", "_") + "-saved"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=True)
    before = (target / "sentinel").read_bytes()
    with pytest.raises(n.InvalidInput, match="unsafe"):
        adapter.write_blob_stream(b"must-not-route")
    assert (target / "sentinel").read_bytes() == before
    assert list(target.iterdir()) == [target / "sentinel"]


@pytest.mark.parametrize("route", ["packed-refs", "config.worktree", "objects/info/alternates", "commondir", "gitdir"])
def test_unsupported_metadata_file_routes_refused(repo, route):
    root, adapter = repo
    write(root, os.fsencode(METADATA + "/" + route), b"foreign route")
    with pytest.raises((n.InvalidInput, n.GitFailure)):
        adapter.status()


@pytest.mark.parametrize("key,value", [("include.path", "/does-not-exist"), ("includeIf.gitdir:/**.path", "/does-not-exist"),
                                      ("filter.sentinel.clean", "touch forbidden"), ("filter.sentinel.process", "touch forbidden"),
                                      ("extensions.worktreeConfig", "true")])
def test_execution_capable_local_config_fails_closed(repo, key, value):
    root, adapter = repo
    git(root, "config", key, value)
    before = canonical(root)
    with pytest.raises(n.InvalidInput, match="execution-capable"):
        adapter.status()
    assert canonical(root) == before


def test_environment_routing_hooks_fsmonitor_replacements_no_ambient_effects(repo, monkeypatch):
    root, adapter = repo
    sentinel = root / "executed"
    script = root / "script"
    script.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\n")
    script.chmod(0o755)
    git(root, "config", "core.fsmonitor", str(script))
    git(root, "config", "core.hooksPath", str(root))
    (root / "post-index-change").write_bytes(script.read_bytes())
    (root / "post-index-change").chmod(0o755)
    before = canonical(root)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_GLOB_PATHSPECS", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(name, "poison")
    base_tree = tree(root)
    allowed = [e.path for e in adapter.tree_entries(base_tree)]
    calls, original = [], subprocess.Popen
    def capture(argv, **kwargs):
        calls.append((argv, kwargs["env"], kwargs["cwd"]))
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", capture)
    adapter.status()
    blob = adapter.write_blob_stream(b"exact").oid
    with adapter.isolated_index(base_tree, **ids()) as handle:
        adapter.write_tree(handle, allowed_paths=allowed)
    assert not sentinel.exists()
    assert canonical(root) == before
    for argv, env, cwd in calls:
        assert "--git-dir=" + str(root / METADATA) in argv
        assert "--work-tree=" + str(root) in argv
        assert cwd == os.fsencode(root)
        assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert all(value != "poison" for name, value in env.items() if name.startswith("GIT_"))
        assert not {"checkout", "checkout-index", "reset", "clean", "switch", "merge", "fetch", "push", "prune", "repair"}.intersection(argv)
        if "read-tree" in argv or "write-tree" in argv:
            assert env["GIT_INDEX_FILE"].startswith(str(root / "data/native/git-resources"))


def test_replace_objects_and_promisor_missing_object_do_not_reinterpret_or_fetch(repo):
    root, adapter = repo
    original = adapter.write_blob_stream(b"original").oid
    replacement = adapter.write_blob_stream(b"replacement").oid
    git(root, "replace", original, replacement)
    destination = io.BytesIO()
    adapter.copy_blob(original, destination)
    assert destination.getvalue() == b"original"
    sentinel = root / "fetch-sentinel"
    helper = root / "helper"
    helper.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\nexit 1\n")
    helper.chmod(0o755)
    git(root, "config", "remote.origin.url", "ext::" + str(helper))
    git(root, "config", "remote.origin.promisor", "true")
    git(root, "config", "extensions.partialClone", "origin")
    with pytest.raises(n.GitFailure):
        adapter.copy_blob("1" * 40, io.BytesIO())
    assert not sentinel.exists()


def test_worktree_no_checkout_exact_executable_bytes_and_live_preservation(repo):
    root, adapter = repo
    before = canonical(root)
    with adapter.temporary_worktree(head(root), **ids()) as handle:
        assert set(os.listdir(adapter.worktree_path(handle))) == {b".git"}
        assert adapter.accounting()["active"] == 1
        assert adapter.accounting()["admin_bytes"] > 0
    assert canonical(root) == before
    with adapter.temporary_worktree(head(root), paths=[b"homeassistant/a"], **ids()) as handle:
        assert (Path(os.fsdecode(adapter.worktree_path(handle))) / "homeassistant/a").read_bytes() == b"A\0\r\ntrailing "
    assert canonical(root) == before
    assert adapter.resources() == ()
    assert not (root / METADATA / "worktrees").exists() or not list((root / METADATA / "worktrees").iterdir())


@pytest.mark.parametrize("path", [b"../x", b"/x", b"homeassistant/.git/x", b"homeassistant/secrets.yaml", b"homeassistant/a/x"])
def test_worktree_refuses_unsafe_or_nonregular_paths_before_allocation(repo, path):
    root, adapter = repo
    before = canonical(root)
    with pytest.raises(n.InvalidInput):
        with adapter.temporary_worktree(head(root), paths=[path], **ids()):
            pass
    assert adapter.resources() == () and canonical(root) == before


def test_foreign_worktree_content_retains_precise_debt_and_can_be_addressed(repo):
    root, adapter = repo
    binding = ids()
    with pytest.raises(n.CleanupDebt) as failure:
        with adapter.temporary_worktree(head(root), **binding) as handle:
            location = adapter.worktree_path(handle)
            write(location, b"foreign-sentinel", b"foreign")
    assert failure.value.result.state == "debt"
    assert Path(os.fsdecode(location), "foreign-sentinel").read_bytes() == b"foreign"
    assert adapter.resources()[0]["resource_id"] == handle.resource_id
    with pytest.raises(n.InvalidInput):
        adapter.recover_resource(handle.resource_id, owner_id=str(uuid.uuid4()), operation_id=binding["operation_id"])
    Path(os.fsdecode(location), "foreign-sentinel").unlink()  # fixture owner removes only its foreign fixture
    assert adapter.recover_resource(handle.resource_id, **binding).state == "removed"
    assert adapter.recover_resource(handle.resource_id, **binding).state == "absent"


@pytest.mark.parametrize("foreign", ["symlink", "fifo", "hardlink", "plain"])
def test_unknown_special_entries_are_preserved_without_hang(repo, foreign):
    root, adapter = repo
    binding = ids()
    with pytest.raises(n.CleanupDebt):
        with adapter.isolated_index(None, **binding) as handle:
            base = adapter._resource_path(handle.resource_id)
            target = os.path.join(base, b"foreign")
            if foreign == "symlink":
                os.symlink(root / "homeassistant/a", target)
            elif foreign == "fifo":
                os.mkfifo(target)
            elif foreign == "hardlink":
                os.link(root / "homeassistant/a", target)
            else:
                write(base, b"foreign", b"foreign")
    assert os.path.lexists(target)
    assert adapter.resources()[0]["phase"] == "debt"
    os.unlink(target)
    assert adapter.recover_resource(handle.resource_id, **binding).state == "removed"


def test_same_device_stacked_mount_evidence_blocks_cleanup(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    real = n.linux_mounts
    with pytest.raises(n.CleanupDebt):
        with adapter.isolated_index(None, **binding) as handle:
            base = adapter._resource_path(handle.resource_id)
            # A narrow kernel-topology seam; no privileged or live mount is created.
            from native_inventory import Mount
            mount = Mount(901, 1, b"1:1", b"/", base + b"/same-device", b"rw", b"ext4", b"fixture", b"rw")
            monkeypatch.setattr(n, "linux_mounts", lambda: (mount,))
            monkeypatch.setattr(n.os.path, "exists", lambda path, original=os.path.exists: True if path == "/proc/self/mountinfo" else original(path))
    assert os.path.isdir(base)
    monkeypatch.setattr(n, "linux_mounts", real)
    monkeypatch.undo()
    assert adapter.recover_resource(handle.resource_id, **binding).state == "removed"


def test_process_lease_blocks_recovery_then_kill_releases_for_addressed_cleanup(repo):
    root, adapter = repo
    binding = ids()
    code = "\nwith g.isolated_index(None,operation_id=sys.argv[2],owner_id=sys.argv[3]) as h:\n print(h.resource_id,flush=True);time.sleep(60)\n"
    process = child(root, code, binding["operation_id"], binding["owner_id"])
    try:
        rid = wait_line(process).strip().decode()
        lease = adapter._lease_path(rid)
        inode = os.lstat(lease).st_ino
        assert adapter.recover_resource(rid, **binding).state == "busy"
        assert os.lstat(lease).st_ino == inode
        process.kill()
        process.communicate(timeout=5)
        reopened = n.NativeGit(root)
        assert len(reopened.resources()) == 1
        assert reopened.recover_resource(rid, **binding).state == "removed"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize("boundary", ["intent", "lease", "root", "marker", "allocated", "ready", "removed"])
def test_real_process_crash_durable_boundaries(repo, boundary):
    root, adapter = repo
    binding = ids()
    code = "\noriginal=g._publish\ndef publish(j):\n original(j)\n records=list(j['records'].items())\n match=records and records[0][1]['phase']==sys.argv[4]\n if records and sys.argv[4]=='lease':\n  match=records[0][1]['lease'] is not None and records[0][1]['root'] is None\n if records and sys.argv[4]=='root':\n  match=records[0][1]['root'] is not None and not records[0][1]['entries']\n if match or (not records and sys.argv[4]=='removed'):\n  print(records[0][0] if records else 'removed',flush=True);os.kill(os.getpid(),9)\ng._publish=publish\noriginal_snapshot=g._snapshot\ndef snapshot(root):\n if sys.argv[4]=='marker' and os.path.exists(os.path.join(root,b'owner.json')):\n  print(os.fsdecode(os.path.basename(root)),flush=True);os.kill(os.getpid(),9)\n return original_snapshot(root)\ng._snapshot=snapshot\nwith g.isolated_index(None,operation_id=sys.argv[2],owner_id=sys.argv[3]):\n pass\n"
    process = child(root, code, binding["operation_id"], binding["owner_id"], boundary)
    rid = wait_line(process).strip().decode()
    process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL
    reopened = n.NativeGit(root)
    if boundary == "removed":
        assert reopened.resources() == ()
    else:
        assert reopened.resources()[0]["resource_id"] == rid
        result = reopened.recover_resource(rid, **binding)
        if boundary in ('root', 'marker'):
            assert result.state == 'debt' and result.cleanup_errors
            record = reopened.resources()[0]
            location = reopened._resource_path(rid)
            assert n._identity(location, directory=True) == record['root']
            marker = os.path.join(location, b'owner.json')
            if os.path.exists(marker):
                os.unlink(marker)  # exact test-owned interrupted allocation
            os.rmdir(location)
            assert reopened.recover_resource(rid, **binding).state == 'removed'
        else:
            assert result.state == 'removed'
        assert reopened.resources() == ()


def test_crash_after_git_registration_is_explicitly_recovered(repo):
    root, adapter = repo
    binding = ids()
    code = "\noriginal=g._run\ndef run(command,**kw):\n result=original(command,**kw)\n if command[:2]==('worktree','add'):\n  print(g.resources()[0]['resource_id'],flush=True);os.kill(os.getpid(),9)\n return result\ng._run=run\nwith g.temporary_worktree(sys.argv[4],operation_id=sys.argv[2],owner_id=sys.argv[3]):\n pass\n"
    process = child(root, code, binding["operation_id"], binding["owner_id"], head(root))
    rid = wait_line(process).strip().decode()
    process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL
    assert adapter.recover_resource(rid, **binding).state == "removed"
    assert adapter.resources() == ()


def test_nested_same_owner_resources_and_concurrent_allocations_preserve_records(repo):
    root, adapter = repo
    binding = ids()
    with adapter.isolated_index(None, **binding) as one:
        with adapter.isolated_index(None, **binding) as two:
            assert one.resource_id != two.resource_id
            assert len(adapter.resources()) == 2
            assert adapter.accounting()["active"] == 2
    assert adapter.resources() == ()
    code = "\nwith g.isolated_index(None,operation_id=sys.argv[2],owner_id=sys.argv[3]) as h:\n print(h.resource_id,flush=True);time.sleep(60)\n"
    processes = [child(root, code, *(lambda b: (b['operation_id'],b['owner_id']))(ids())) for _ in range(3)]
    try:
        resource_ids = [wait_line(p).strip().decode() for p in processes]
        assert {r['resource_id'] for r in adapter.resources()} == set(resource_ids)
    finally:
        for process in processes:
            process.kill()
            process.communicate(timeout=5)
    for record in adapter.resources():
        assert adapter.recover_resource(record['resource_id'], owner_id=record['owner'], operation_id=record['operation']).state == "removed"


def test_cleanup_aggregates_independent_errors_and_preserves_original_failure(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    original = os.unlink
    def fail(path, *args, **kwargs):
        if os.path.basename(os.fsencode(path)) == b"index":
            raise OSError("fixture-index-unlink-failure")
        return original(path, *args, **kwargs)
    with pytest.raises(RuntimeError, match="original-operation") as error:
        with adapter.isolated_index(None, **binding) as handle:
            monkeypatch.setattr(os, "unlink", fail)
            raise RuntimeError("original-operation")
    assert any("fixture-index-unlink-failure" in e for e in error.value.cleanup_errors)
    assert adapter.resources()[0]["errors"]
    monkeypatch.setattr(os, "unlink", original)
    assert adapter.recover_resource(handle.resource_id, **binding).state == "removed"


def test_constructor_dormancy_corrupt_journal_and_no_production_import(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    with pytest.raises(n.CleanupDebt):
        with adapter.isolated_index(None, **binding) as handle:
            write(adapter._resource_path(handle.resource_id), b"unknown")
    manifest = root / "data/native/git-resources/journal.json"
    before = manifest.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("constructor activated Git")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for _ in ("refresh", "restart", "update"):
        n.NativeGit(root)
        assert manifest.read_bytes() == before
    for mutation in ('admin-array', 'admin-null-entries', 'bool-anchor', 'invalid-digest'):
        malformed = json.loads(before)
        rid = next(iter(malformed['records']))
        record = malformed['records'][rid]
        if mutation.startswith('admin-'):
            record['kind'] = 'worktree'
            record['admin'] = [] if mutation == 'admin-array' else {'name': 'tree', 'identity': [1, 2, 448], 'entries': None}
        elif mutation == 'bool-anchor':
            record['root'][0] = True
        else:
            record['entries'][next(iter(record['entries']))][3] = 'unknown-digest'
        payload = json.dumps(malformed).encode()
        manifest.write_bytes(payload)
        with pytest.raises(n.InvalidInput):
            adapter.resources()
        assert manifest.read_bytes() == payload
    manifest.write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises(n.InvalidInput, match="duplicate-journal"):
        adapter.resources()
    app = Path(n.__file__).parent
    assert_dormant_import_contour({path.name: path.read_text() for path in app.glob("*.py")})


def assert_dormant_import_contour(sources):
    for name, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules = [part for alias in node.names for part in alias.name.split(".")]
            elif isinstance(node, ast.ImportFrom):
                modules = (node.module or "").split(".") + [part for alias in node.names for part in alias.name.split(".")]
            else:
                continue
            for module in modules:
                if module not in ("native_git", "native_disk", "native_publication"):
                    continue
                # Only this named dormant observational peer may reuse NativeGit.
                # Neither adapter may become an ordinary app entrypoint import.
                assert (name, module) in (("native_disk.py", "native_git"), ("native_preview.py", "native_git"),
                    ("native_save.py", "native_git"), ("native_save.py", "native_disk"),
                    ("native_git.py", "native_disk"), ("native_operations.py", "native_git"), ("native_publication.py", "native_git"),
                    ("native_publication.py", "native_disk"), ("native_operations.py", "native_publication"),
                    ("native_git.py", "native_publication"), ("native_disk.py", "native_publication"),
                    ("native_scope.py", "native_git"), ("native_scope.py", "native_disk"),
                    ("native_scope.py", "native_publication"), ("native_publication.py", "native_scope")), (name, module)


@pytest.mark.parametrize("module", ["native_publication", "app.native_publication", "native_git", "native_disk", "native_git.helpers", "native_disk.helpers", "app.native_git", "app.native_disk", "app.native_git.helpers", "app.native_disk.helpers"])
@pytest.mark.parametrize("syntax", ["import {module} as alias", "from {module} import Adapter"])
def test_dormant_import_sentinel_detects_production_imports(module, syntax):
    with pytest.raises(AssertionError):
        assert_dormant_import_contour({"server.py": syntax.format(module=module)})


@pytest.mark.parametrize("module", ["native_git", "native_disk", "native_publication"])
@pytest.mark.parametrize("package", ["app", ".", "..app"])
@pytest.mark.parametrize("nested", [False, True])
def test_dormant_import_sentinel_detects_package_imported_aliases(module, package, nested):
    source = "from " + package + " import " + module + " as alias"
    if nested:
        source = "def lazy():\n    " + source
    with pytest.raises(AssertionError):
        assert_dormant_import_contour({"server.py": source})


def test_dormant_import_sentinel_allows_only_named_observational_peer():
    assert_dormant_import_contour({"native_disk.py": "def observe():\n    from native_git import NativeGit"})
    assert_dormant_import_contour({"native_preview.py": "def observe():\n    from native_git import NativeGit"})
    with pytest.raises(AssertionError):
        assert_dormant_import_contour({"another_dormant.py": "from native_git import NativeGit"})


def test_forged_and_closed_handles_reject_before_subprocess(repo, monkeypatch):
    root, adapter = repo
    with adapter.isolated_index(None, **ids()) as handle:
        def forbidden(*args, **kwargs):
            raise AssertionError("forged handle launched Git")
        original = subprocess.Popen
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        for fake in (dataclasses.replace(handle), dataclasses.replace(handle, owner_id=str(uuid.uuid4())),
                     dataclasses.replace(handle, operation_id=str(uuid.uuid4())), dataclasses.replace(handle, _binding="foreign")):
            with pytest.raises(n.InvalidInput):
                adapter.index_entries(fake)
        monkeypatch.setattr(subprocess, "Popen", original)
    with pytest.raises(n.InvalidInput):
        adapter.index_entries(handle)


@pytest.mark.parametrize("kind", ["index", "spool"])
def test_crash_before_owned_file_acknowledgement_retains_exact_debt(repo, kind):
    root, adapter = repo
    binding = ids()
    if kind == "index":
        code = "\noriginal=g._run\ndef run(command,**kw):\n result=original(command,**kw)\n if command[0]=='read-tree':\n  print(g.resources()[0]['resource_id'],flush=True);os.kill(os.getpid(),9)\n return result\ng._run=run\nwith g.isolated_index(None,operation_id=sys.argv[2],owner_id=sys.argv[3]):\n pass\n"
        arguments = (binding["operation_id"], binding["owner_id"])
    else:
        code = "\ndef producer():\n yield b'x'*70000\n print(g.resources()[0]['resource_id'],flush=True);os.kill(os.getpid(),9)\ng.write_blob_stream(producer())\n"
        arguments = ()
    process = child(root, code, *arguments)
    rid = wait_line(process).strip().decode()
    process.communicate(timeout=5)
    record = adapter.resources()[0]
    assert record['resource_id'] == rid
    accounting = adapter.accounting()
    assert accounting['unresolved'] == 1 and accounting['temporary_bytes'] > 0
    owned = adapter._resource_path(rid)
    target = os.path.join(owned, kind.encode())
    before = Path(os.fsdecode(target)).read_bytes()
    result = adapter.recover_resource(rid, owner_id=record['owner'], operation_id=record['operation'])
    assert result.state == 'debt' and result.cleanup_errors
    assert Path(os.fsdecode(target)).read_bytes() == before
    assert adapter.resources()[0]['phase'] == 'debt'
    # The fixture owner resolves the unacknowledged content explicitly; recovery
    # never guesses a content identity absent from the durable record.
    os.unlink(target)
    assert adapter.recover_resource(rid, owner_id=record['owner'], operation_id=record['operation']).state == 'removed'


def test_real_crash_after_worktree_removal_before_journal_acknowledgement(repo):
    root, adapter = repo
    binding = ids()
    code = "\noriginal=g._run\ndef run(command,**kw):\n result=original(command,**kw)\n if command[:2]==('worktree','remove'):\n  print(os.path.basename(os.path.dirname(command[-1])),flush=True);os.kill(os.getpid(),9)\n return result\ng._run=run\nwith g.temporary_worktree(sys.argv[4],operation_id=sys.argv[2],owner_id=sys.argv[3]):\n pass\n"
    process = child(root, code, binding['operation_id'], binding['owner_id'], head(root))
    rid = wait_line(process).strip().decode()
    process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL
    assert adapter.recover_resource(rid, **binding).state == 'removed'
    assert adapter.resources() == ()


def test_replaced_marker_root_and_lease_preserve_foreign_identity(repo):
    root, adapter = repo
    binding = ids()
    with pytest.raises(n.CleanupDebt):
        with adapter.isolated_index(None, **binding) as handle:
            location = adapter._resource_path(handle.resource_id)
            marker = os.path.join(location, b'owner.json')
            original = Path(os.fsdecode(marker)).read_bytes()
            os.unlink(marker)
            write(location, b'owner.json', b'foreign-marker')
    result = adapter.recover_resource(handle.resource_id, **binding)
    assert result.state == 'debt'
    assert Path(os.fsdecode(marker)).read_bytes() == b'foreign-marker'
    assert adapter.accounting()['errors']


def test_independent_owned_cleanup_attempts_aggregate_errors_and_retry(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    attempted = []
    original = os.unlink
    def fail(path, *args, **kwargs):
        name = os.path.basename(os.fsencode(path))
        if name in (b'index', b'index.lock'):
            attempted.append(name)
            raise OSError('unlink-' + name.decode())
        return original(path, *args, **kwargs)
    with pytest.raises(n.CleanupDebt) as failure:
        with adapter.isolated_index(None, **binding) as handle:
            write(adapter._resource_path(handle.resource_id), b'index.lock', b'owned-git-fixture')
            adapter._capture(handle)
            monkeypatch.setattr(os, 'unlink', fail)
    assert set(attempted) == {b'index', b'index.lock'}
    assert len(failure.value.cleanup_errors) == 2
    monkeypatch.setattr(os, 'unlink', original)
    assert adapter.recover_resource(handle.resource_id, **binding).state == 'removed'


def test_publication_fsync_failure_is_debt_then_explicit_retry(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    original = adapter._fsync
    with pytest.raises(n.CleanupDebt) as failure:
        with adapter.isolated_index(None, **binding) as handle:
            def fail(path):
                if path == adapter.resources_root:
                    raise OSError('fsync-resource-parent-failed')
                original(path)
            monkeypatch.setattr(adapter, '_fsync', fail)
    assert any('fsync-resource-parent-failed' in e for e in failure.value.cleanup_errors)
    assert adapter.resources()[0]['phase'] == 'debt'
    monkeypatch.setattr(adapter, '_fsync', original)
    assert adapter.recover_resource(handle.resource_id, **binding).state == 'removed'


def test_capture_failure_preserves_producer_failure_and_ownership_debt(repo, monkeypatch):
    root, adapter = repo
    def producer():
        yield b'partial'
        raise RuntimeError('producer-is-primary')
    def fail(handle, **kwargs):
        raise OSError('ownership-publication-failed')
    monkeypatch.setattr(adapter, '_capture', fail)
    with pytest.raises(RuntimeError, match='producer-is-primary') as error:
        adapter.write_blob_stream(producer())
    assert any('ownership-publication-failed' in e for e in error.value.cleanup_errors)
    assert adapter.resources()[0]['phase'] == 'debt'


def test_complete_validation_rejects_duplicates_parent_collision_and_secret(repo):
    root, adapter = repo
    before = canonical(root)
    for paths in ([b'homeassistant/a', b'homeassistant/a'], [b'homeassistant/a', b'homeassistant/a/child'], [b'homeassistant/secrets.yaml']):
        with pytest.raises(n.InvalidInput):
            with adapter.temporary_worktree(head(root), paths=paths, **ids()):
                pass
        assert adapter.resources() == ()
    assert canonical(root) == before


def test_object_leaf_hardlink_refused_and_loose_history_not_scanned(repo, monkeypatch):
    root, adapter = repo
    blob = adapter.write_blob_stream(b'leaf').oid
    path = root / METADATA / 'objects' / blob[:2] / blob[2:]
    os.link(path, root / 'foreign-object-hardlink')
    with pytest.raises(n.InvalidInput, match='unsafe'):
        adapter.copy_blob(blob, io.BytesIO())
    original, visited = os.scandir, []
    def record(path):
        visited.append(os.fsencode(path))
        return original(path)
    monkeypatch.setattr(os, 'scandir', record)
    adapter.status()
    fanout = os.fsencode(root / METADATA / 'objects' / blob[:2])
    assert fanout not in visited


def test_empty_constructor_has_no_allocations(repo, monkeypatch):
    root, adapter = repo
    assert not (root / 'data/native/git-resources').exists()
    def forbidden(*args, **kwargs):
        raise AssertionError('constructor subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    n.NativeGit(root)
    assert not (root / 'data/native/git-resources').exists()


def test_read_ref_command_failure_is_not_absence(repo, monkeypatch):
    root, adapter = repo
    original = adapter._raw
    def failure(command, **kwargs):
        if command[:3] == ('show-ref', '--verify', '--quiet'):
            raise n.GitFailure('observation-unavailable')
        return original(command, **kwargs)
    monkeypatch.setattr(adapter, '_raw', failure)
    with pytest.raises(n.GitFailure, match='observation-unavailable'):
        adapter.read_ref('refs/ha-ops/missing')


def test_blob_content_corruption_never_returns_apparent_success(repo):
    import zlib
    root, adapter = repo
    oid = adapter.write_blob_stream(b'original').oid
    path = root / METADATA / 'objects' / oid[:2] / oid[2:]
    # A valid compressed object with the wrong hash can be read by Git cat-file;
    # the streaming adapter must independently refuse its apparent identity.
    path.chmod(0o600)
    path.write_bytes(zlib.compress(b'blob 8\0modified'))
    with pytest.raises(n.GitFailure, match='object-id-mismatch'):
        adapter.copy_blob(oid, io.BytesIO())


def test_failed_absence_publication_retains_resource_record_until_retry(repo, monkeypatch):
    root, adapter = repo
    original = adapter._publish
    failures = []
    def publish(journal):
        if not journal['records'] and not failures:
            failures.append(True)
            # Simulate replace acknowledgement followed by parent fsync failure.
            original(journal)
            raise OSError('absence-publication-not-durable')
        return original(journal)
    binding = ids()
    with pytest.raises(n.CleanupDebt):
        with adapter.isolated_index(None, **binding) as handle:
            monkeypatch.setattr(adapter, '_publish', publish)
    assert len(adapter.resources()) == 1
    assert adapter.resources()[0]['phase'] == 'debt'
    assert not os.path.exists(adapter._resource_path(handle.resource_id))
    assert adapter.recover_resource(handle.resource_id, **binding).state == 'removed'


def test_nested_tree_object_symlink_refused_before_traversal(repo):
    root, adapter = repo
    write(root, b'homeassistant/nested/file', b'content')
    git(root, 'add', '--', 'homeassistant/nested/file')
    git(root, 'commit', '-m', 'nested fixture')
    oid = git(root, 'rev-parse', 'HEAD:homeassistant/nested').strip().decode()
    path = root / METADATA / 'objects' / oid[:2] / oid[2:]
    foreign = root / 'foreign-tree-object'
    path.rename(foreign)
    path.symlink_to(foreign)
    with pytest.raises(n.InvalidInput, match='unsafe'):
        adapter.tree_entries(head(root))
    assert foreign.exists()


def test_blob_producer_failure_attempts_every_independent_cleanup(repo, monkeypatch):
    root, adapter = repo
    original, attempted = os.unlink, []
    def fail(path, *args, **kwargs):
        name = os.path.basename(os.fsencode(path))
        if name == b'spool':
            attempted.append(name)
            raise OSError('spool-unlink-debt')
        return original(path, *args, **kwargs)
    def chunks():
        yield b'owned-content'
        monkeypatch.setattr(os, 'unlink', fail)
        raise RuntimeError('late-producer-failure')
    with pytest.raises(RuntimeError, match='late-producer-failure') as error:
        adapter.write_blob_stream(chunks())
    assert attempted == [b'spool']
    assert any('spool-unlink-debt' in e for e in error.value.cleanup_errors)
    record = adapter.resources()[0]
    assert adapter.accounting()['temporary_bytes'] >= len(b'owned-content')
    monkeypatch.setattr(os, 'unlink', original)
    assert adapter.recover_resource(record['resource_id'], owner_id=record['owner'], operation_id=record['operation']).state == 'removed'


@pytest.mark.parametrize('timeout', [0, -1, True, float('inf'), float('nan'), '30'])
def test_timeout_must_be_finite_positive(repo, timeout):
    root, adapter = repo
    with pytest.raises(n.InvalidInput, match='finite-positive'):
        n.NativeGit(root, timeout=timeout)
    with pytest.raises(n.InvalidInput, match='finite-positive'):
        adapter.timeout = timeout


def test_attributes_filter_sentinel_never_executes(repo):
    root, adapter = repo
    sentinel = root / 'filter-executed'
    helper = root / 'clean-script'
    helper.write_text('#!/bin/sh\ntouch ' + str(sentinel) + '\ncat\n')
    helper.chmod(0o755)
    write(root, b'homeassistant/.gitattributes', b'* filter=sentinel\n')
    git(root, 'config', 'filter.sentinel.clean', str(helper))
    before = canonical(root)
    with pytest.raises(n.InvalidInput, match='execution-capable'):
        adapter.status()
    assert not sentinel.exists() and canonical(root) == before


def test_foreign_worktree_and_lock_remain_untouched(repo):
    root, adapter = repo
    foreign = root / 'foreign-worktree'
    git(root, 'worktree', 'add', '--detach', '--no-checkout', str(foreign), head(root))
    git(root, 'worktree', 'lock', '--reason', 'foreign-owner', str(foreign))
    write(foreign, b'sentinel', b'foreign-bytes')
    before = git(root, 'worktree', 'list', '--porcelain', '-z')
    with adapter.temporary_worktree(head(root), **ids()):
        pass
    assert git(root, 'worktree', 'list', '--porcelain', '-z') == before
    assert (foreign / 'sentinel').read_bytes() == b'foreign-bytes'
    git(root, 'worktree', 'unlock', str(foreign))
    git(root, 'worktree', 'remove', '--force', str(foreign))


@pytest.mark.parametrize('mode', [b'120000', b'160000'])
def test_worktree_never_materializes_symlink_or_gitlink(repo, mode):
    root, adapter = repo
    oid = head(root) if mode == b'160000' else adapter.write_blob_stream(b'../../foreign').oid
    with adapter.isolated_index(None, **ids()) as handle:
        adapter._run(('update-index', '-z', '--index-info'), index=adapter._index(handle), input=mode + b' ' + oid.encode() + b'\thomeassistant/link\0')
        adapter._capture(handle)
        tree_oid = adapter._run(('write-tree',), index=adapter._index(handle)).strip().decode()
        adapter._capture(handle)
    commit = git(root, 'commit-tree', tree_oid, '-m', 'unsupported fixture').strip().decode()
    before = canonical(root)
    with pytest.raises(n.InvalidInput, match='regular-tree-entry'):
        with adapter.temporary_worktree(commit, paths=[b'homeassistant/link'], **ids()):
            pass
    assert adapter.resources() == () and canonical(root) == before


@pytest.mark.parametrize('names', [(b'Case', b'case'), ('é'.encode(), 'e\u0301'.encode())])
def test_unrepresentable_filesystem_set_refused_before_blob_writes(repo, monkeypatch, names):
    root, adapter = repo
    blob = adapter.write_blob_stream(b'exact').oid
    paths = [b'homeassistant/' + name for name in names]
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(p, b'100644', blob, 0) for p in paths])
        tree_oid = adapter.write_tree(handle, allowed_paths=paths)
    commit = git(root, 'commit-tree', tree_oid, '-m', 'filename fixture').strip().decode()
    original, calls = adapter.copy_blob, []
    def copy(oid, destination):
        calls.append(oid)
        return original(oid, destination)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    if sys.platform == 'darwin':
        with pytest.raises(FileExistsError):
            with adapter.temporary_worktree(commit, paths=paths, **ids()):
                pass
        assert not calls
    else:
        with adapter.temporary_worktree(commit, paths=paths, **ids()):
            assert calls == [blob, blob]
    assert adapter.resources() == ()


def test_complete_private_conflict_stages_refused_without_canonical_effect(repo):
    root, adapter = repo
    blob = adapter.write_blob_stream(b'conflict').oid
    before = canonical(root)
    with adapter.isolated_index(None, **ids()) as handle:
        payload = b''.join(b'100644 ' + blob.encode() + b' ' + str(stage).encode() + b'\thomeassistant/conflict\0' for stage in (1, 2, 3))
        adapter._run(('update-index', '-z', '--index-info'), index=adapter._index(handle), input=payload)
        adapter._capture(handle)
        with pytest.raises(n.InvalidInput, match='unsupported-complete-index'):
            adapter.write_tree(handle, allowed_paths=[b'homeassistant/conflict'])
    assert canonical(root) == before


def test_symbolic_ref_with_alternate_whitespace_refused(repo):
    root, adapter = repo
    write(root, os.fsencode(METADATA + '/refs/ha-ops/tab-symbolic'), b'ref:\trefs/heads/fixture\n')
    with pytest.raises(n.InvalidInput, match='symbolic'):
        adapter.read_ref('refs/ha-ops/tab-symbolic')


def test_worktree_add_failure_remains_primary_when_backlink_publication_fails(repo, monkeypatch):
    root, adapter = repo
    original, binding = adapter._run, ids()
    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[:2] == ('worktree', 'add'):
            raise RuntimeError('worktree-add-primary')
        return result
    def fail(handle):
        raise OSError('backlink-publication-failed')
    monkeypatch.setattr(adapter, '_run', run)
    monkeypatch.setattr(adapter, '_admin_binding', fail)
    with pytest.raises(RuntimeError, match='worktree-add-primary') as error:
        with adapter.temporary_worktree(head(root), **binding):
            pass
    assert any('backlink-publication-failed' in e for e in error.value.cleanup_errors)
    record = adapter.resources()[0]
    assert record['phase'] == 'debt'
    monkeypatch.undo()
    assert adapter.recover_resource(record['resource_id'], **binding).state == 'removed'


def test_materialization_failure_remains_primary_when_capture_fails(repo, monkeypatch):
    root, adapter = repo
    def fail_capture(handle, rootfd, evidence):
        raise OSError('materialization-publication-failed')
    def copy(oid, destination):
        monkeypatch.setattr(adapter, '_capture_materialization', fail_capture)
        raise RuntimeError('materialization-primary')
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    with pytest.raises(RuntimeError, match='materialization-primary') as error:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **ids()):
            pass
    assert any('materialization-publication-failed' in e for e in error.value.cleanup_errors)
    assert adapter.resources() == ()


def object_format_fixture(tmp_path, object_format):
    root = (tmp_path / object_format).resolve()
    (root / 'data/native').mkdir(parents=True)
    git(root, 'init', '--object-format=' + object_format, '--initial-branch=fixture')
    write(root, b'homeassistant/a', b'A\0\r\ntrailing ')
    write(root, b'homeassistant/b', b'B')
    write(root, b'.gitignore', b'/data/\n')
    git(root, 'add', '--', 'homeassistant/a', 'homeassistant/b', '.gitignore')
    git(root, 'commit', '-m', 'object-format fixture')
    return root, n.NativeGit(root)


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('result_kind', ['root', 'subtree'])
@pytest.mark.parametrize('route_kind', ['symlink', 'hardlink'])
def test_write_tree_preflights_every_generated_object_route(tmp_path, monkeypatch, object_format, result_kind, route_kind):
    root, adapter = object_format_fixture(tmp_path, object_format)
    entries = adapter.index_entries()
    oid = tree(root) if result_kind == 'root' else git(root, 'rev-parse', 'HEAD:homeassistant').strip().decode()
    leaf = root / METADATA / 'objects' / oid[:2] / oid[2:]
    foreign = root / 'foreign-tree-object'
    foreign.write_bytes(leaf.read_bytes())
    leaf.unlink()
    if route_kind == 'symlink':
        leaf.symlink_to(foreign)
    else:
        os.link(foreign, leaf)
    os.utime(foreign, (1000000000, 1000000000))
    before = (foreign.read_bytes(), foreign.stat().st_mtime_ns, foreign.stat().st_mode, canonical(root))
    calls, original = [], adapter._raw
    def raw(command, **kwargs):
        if command[0] == 'write-tree':
            calls.append(command)
        return original(command, **kwargs)
    monkeypatch.setattr(adapter, '_raw', raw)
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, entries)
        private_before = adapter.index_entries(handle)
        with pytest.raises(n.InvalidInput, match='unsafe'):
            adapter.write_tree(handle, allowed_paths=[entry.path for entry in entries])
        assert adapter.index_entries(handle) == private_before
    assert not calls
    assert (foreign.read_bytes(), foreign.stat().st_mtime_ns, foreign.stat().st_mode, canonical(root)) == before
    assert adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('result_kind', ['root', 'subtree'])
def test_write_tree_refuses_corrupt_reused_result_content(tmp_path, object_format, result_kind):
    import zlib
    root, adapter = object_format_fixture(tmp_path, object_format)
    entries = adapter.index_entries()
    oid = tree(root) if result_kind == 'root' else git(root, 'rev-parse', 'HEAD:homeassistant').strip().decode()
    leaf = root / METADATA / 'objects' / oid[:2] / oid[2:]
    leaf.chmod(0o600)
    corrupt = zlib.compress(b'tree 0\0')
    leaf.write_bytes(corrupt)
    os.utime(leaf, (1000000000, 1000000000))
    before = (leaf.read_bytes(), leaf.stat().st_mtime_ns, canonical(root))
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, entries)
        with pytest.raises(n.GitFailure, match='object-id-mismatch'):
            adapter.write_tree(handle, allowed_paths=[entry.path for entry in entries])
    assert (leaf.read_bytes(), leaf.stat().st_mtime_ns, canonical(root)) == before
    assert adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('replacement', [b'blob 8\0modified', b'blob 999\0original', b'not a git object', b'tree 0\0'])
def test_blob_writer_verifies_existing_storage_before_freshening(tmp_path, object_format, replacement):
    import zlib
    root, adapter = object_format_fixture(tmp_path, object_format)
    oid = adapter.write_blob_stream(b'original').oid
    leaf = root / METADATA / 'objects' / oid[:2] / oid[2:]
    leaf.chmod(0o600)
    corrupt = zlib.compress(replacement)
    leaf.write_bytes(corrupt)
    os.utime(leaf, (1000000000, 1000000000))
    before = (leaf.read_bytes(), leaf.stat().st_mtime_ns, canonical(root))
    with pytest.raises((n.GitFailure, n.InvalidInput)):
        adapter.write_blob_stream(b'original')
    assert (leaf.read_bytes(), leaf.stat().st_mtime_ns, canonical(root)) == before
    assert adapter.resources() == ()


def test_explicit_untracked_requests_retain_rename_and_conflict_diagnostics(repo):
    root, adapter = repo
    git(root, 'mv', 'homeassistant/a', 'homeassistant/renamed \t[*]')
    write(root, b'homeassistant/renamed \t[*]', b'unstaged rename edit')
    blob = git(root, 'rev-parse', 'HEAD:homeassistant/b').strip()
    payload = b'0 ' + b'0' * 40 + b'\thomeassistant/b\0' + b''.join(
        b'100644 ' + blob + b' ' + str(stage).encode() + b'\thomeassistant/b\0' for stage in (1, 2, 3))
    subprocess.run(['git', '--git-dir=' + str(root / METADATA), '--work-tree=' + str(root), 'update-index', '-z', '--index-info'], input=payload, check=True)
    requested = [b'homeassistant/exact[*]', b'homeassistant/tab\tnew', b':(glob)*']
    for path in requested:
        write(root, path, b'untracked exact')
    write(root, b'homeassistant/exactX', b'unrequested sibling')
    before = canonical(root)
    tracked = adapter.status()
    assert {entry.kind for entry in tracked} == {b'2', b'u'}
    result = adapter.status(untracked_paths=requested)
    assert tuple(entry for entry in result if entry.kind != b'?') == tracked
    assert {entry.path for entry in result if entry.kind == b'?'} == set(requested)
    assert canonical(root) == before


def status_record(kind, xy, *, score=b'R100'):
    oid = b'a' * 40
    if kind == b'u':
        fields = b'100644 100644 100644 100644 ' + b' '.join([oid] * 3)
    else:
        fields = b'100644 100644 100644 ' + b' '.join([oid] * 2)
        if kind == b'2':
            fields += b' ' + score
    data = kind + b' ' + xy + b' N... ' + fields + b' homeassistant/a\0'
    return data + b'homeassistant/original\0' if kind == b'2' else data


@pytest.mark.parametrize('kind,xy,score', [
    (b'1', b'??', b'R100'), (b'1', b'!!', b'R100'), (b'1', b'UU', b'R100'),
    (b'1', b'R.', b'R100'), (b'1', b'C.', b'C100'), (b'1', b'AA', b'R100'),
    (b'1', b'DD', b'R100'), (b'1', b'..', b'R100'),
    (b'2', b'M.', b'R100'), (b'2', b'UU', b'R100'), (b'2', b'??', b'R100'),
    (b'2', b'R.', b'C100'), (b'2', b'C.', b'R100'),
    (b'u', b'M.', b'R100'), (b'u', b'R.', b'R100'), (b'u', b'??', b'R100'), (b'u', b'..', b'R100'),
])
def test_status_record_kind_and_xy_are_consistent(kind, xy, score):
    with pytest.raises(n.InvalidInput, match='malformed-status'):
        n.parse_status(status_record(kind, xy, score=score))


@pytest.mark.parametrize('kind,values', [
    (b'1', (b'.A', b'.M', b'.T', b'.D', b'M.', b'MM', b'MT', b'MD', b'T.', b'TM', b'TT', b'TD', b'A.', b'AM', b'AT', b'AD', b'D.')),
    (b'2', (b'R.', b'RM', b'RT', b'RD', b'C.', b'CM', b'CT', b'CD', b'.R', b'.C')),
    (b'u', (b'DD', b'AU', b'UD', b'UA', b'DU', b'AA', b'UU')),
])
def test_status_kind_accepts_pinned_git_positive_grammar(kind, values):
    for xy in values:
        score = b'C75' if b'C' in xy else b'R100'
        entry, = n.parse_status(status_record(kind, xy, score=score))
        assert entry.kind == kind and entry.xy == xy


@pytest.mark.parametrize('entrypoint', ['index_entries', 'update_index', 'write_tree'])
@pytest.mark.parametrize('mutation', ['replace-valid', 'replace-invalid', 'edit', 'mode', 'missing', 'foreign-lock'])
def test_acknowledged_private_index_cannot_be_adopted_after_mutation(repo, monkeypatch, entrypoint, mutation):
    root, adapter = repo
    blob = adapter.write_blob_stream(b'C').oid
    before = canonical(root)
    binding, consumers = ids(), []
    with pytest.raises(n.InvalidInput) as error:
        with adapter.isolated_index(tree(root), **binding) as handle:
            resource = Path(os.fsdecode(adapter._resource_path(handle.resource_id)))
            candidate = resource / 'index'
            original_bytes = candidate.read_bytes()
            old_evidence = adapter.resources()[0]['entries'][n._encode(b'index')]
            foreign_path = candidate
            foreign_bytes = original_bytes
            if mutation.startswith('replace-'):
                foreign_bytes = original_bytes if mutation == 'replace-valid' else b'foreign-content'
                replacement = resource / 'foreign-replacement'
                replacement.write_bytes(foreign_bytes)
                os.replace(replacement, candidate)
            elif mutation == 'edit':
                foreign_bytes = b'edited-in-place'
                candidate.write_bytes(foreign_bytes)
            elif mutation == 'mode':
                candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) ^ 0o100)
            elif mutation == 'missing':
                candidate.unlink()
            else:
                foreign_path = resource / 'index.lock'
                foreign_bytes = b'foreign-lock-content'
                foreign_path.write_bytes(foreign_bytes)
            original = adapter._raw
            def raw(command, **kwargs):
                if command[0] in ('ls-files', 'read-tree', 'update-index', 'write-tree') and kwargs.get('index') is not None:
                    consumers.append(command)
                return original(command, **kwargs)
            monkeypatch.setattr(adapter, '_raw', raw)
            if entrypoint == 'index_entries':
                adapter.index_entries(handle)
            elif entrypoint == 'update_index':
                adapter.update_index(handle, [n.IndexEntry(b'homeassistant/c', b'100644', blob, 0)])
            else:
                adapter.write_tree(handle, allowed_paths=[b'homeassistant/a', b'homeassistant/b', b'.gitignore'])
    assert not consumers
    assert canonical(root) == before
    if mutation == 'missing':
        assert not candidate.exists() and adapter.resources() == ()
    else:
        assert foreign_path.read_bytes() == foreign_bytes
        record, = adapter.resources()
        assert record['phase'] == 'debt' and error.value.cleanup_errors
        if mutation != 'foreign-lock':
            assert record['entries'][n._encode(b'index')] == old_evidence
        else:
            assert n._encode(b'index.lock') not in record['entries']
        foreign_path.unlink()  # the fixture owner resolves only its foreign content
        assert adapter.recover_resource(handle.resource_id, **binding).state == 'removed'


def test_failed_index_effect_never_recaptures_an_unacknowledged_result(repo, monkeypatch):
    root, adapter = repo
    binding = ids()
    original = adapter._run
    with pytest.raises(n.GitFailure, match='index-effect-uncertain') as error:
        with adapter.isolated_index(tree(root), **binding) as handle:
            old_evidence = adapter.resources()[0]['entries'][n._encode(b'index')]
            path = Path(os.fsdecode(adapter._resource_path(handle.resource_id))) / 'index'
            old_bytes = path.read_bytes()
            def run(command, **kwargs):
                output = original(command, **kwargs)
                if command[0] == 'update-index':
                    raise n.GitFailure('index-effect-uncertain')
                return output
            monkeypatch.setattr(adapter, '_run', run)
            adapter.update_index(handle, removals=[b'homeassistant/a'])
    record, = adapter.resources()
    assert record['phase'] == 'debt' and error.value.cleanup_errors
    assert record['entries'][n._encode(b'index')] == old_evidence
    assert path.read_bytes() != old_bytes
    path.unlink()  # only this fixture's explicitly unacknowledged result
    assert adapter.recover_resource(handle.resource_id, **binding).state == 'removed'


@pytest.mark.parametrize('content', [b'', b'garbage\n', b'0' * 40 + b'\n', b'a' * 40 + b'\n', b'ref: refs/ha-ops/missing\n'])
def test_existing_malformed_ref_is_never_absence(repo, content):
    root, adapter = repo
    path = root / METADATA / 'refs/ha-ops/broken'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    before = (path.read_bytes(), path.stat().st_mtime_ns, canonical(root))
    with pytest.raises((n.InvalidInput, n.GitFailure)):
        adapter.read_ref('refs/ha-ops/broken')
    assert (path.read_bytes(), path.stat().st_mtime_ns, canonical(root)) == before


@pytest.mark.parametrize('parents', [(b'Folder', b'folder'), ('é'.encode(), 'e\u0301'.encode())])
def test_parent_component_identity_is_reserved_before_blob_content(repo, monkeypatch, parents):
    root, adapter = repo
    probe = os.fsencode(root / 'parent-representation-probe')
    os.mkdir(probe)
    os.mkdir(os.path.join(probe, parents[0]))
    collides = False
    try:
        os.mkdir(os.path.join(probe, parents[1]))
    except FileExistsError:
        collides = True
    finally:
        for name in os.listdir(probe):
            os.rmdir(os.path.join(probe, name))
        os.rmdir(probe)
    paths = [b'homeassistant/' + parents[0] + b'/x', b'homeassistant/' + parents[1] + b'/y']
    blob = adapter.write_blob_stream(b'exact-parent').oid
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(path, b'100644', blob, 0) for path in paths])
        tree_oid = adapter.write_tree(handle, allowed_paths=paths)
    commit = git(root, 'commit-tree', tree_oid, '-m', 'parent component fixture').strip().decode()
    original, copies = adapter.copy_blob, []
    def copy(oid, destination):
        copies.append(oid)
        return original(oid, destination)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    if collides:
        with pytest.raises((n.InvalidInput, FileExistsError)):
            with adapter.temporary_worktree(commit, paths=paths, **ids()):
                pass
        assert not copies
    else:
        with adapter.temporary_worktree(commit, paths=paths, **ids()) as handle:
            base = adapter.worktree_path(handle)
            actual = set()
            for parent, directories, files in os.walk(base):
                actual.update(os.path.relpath(os.path.join(parent, name), base) for name in files if name != b'.git')
            assert actual == set(paths)
        assert copies == [blob, blob]
    assert adapter.resources() == ()


def test_same_handle_index_calls_serialize_provenance_and_effect(repo):
    import threading
    root, adapter = repo
    before = canonical(root)
    a = adapter.write_blob_stream(b'new A').oid
    c = adapter.write_blob_stream(b'new C').oid
    start, errors = threading.Barrier(3), []
    with adapter.isolated_index(tree(root), **ids()) as handle:
        def update(path, oid):
            try:
                start.wait(timeout=5)
                adapter.update_index(handle, [n.IndexEntry(path, b'100644', oid, 0)])
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=update, args=(b'homeassistant/a', a)),
                   threading.Thread(target=update, args=(b'homeassistant/c', c))]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert not errors
        entries = {entry.path: entry for entry in adapter.index_entries(handle)}
        assert entries[b'homeassistant/a'].oid == a and entries[b'homeassistant/c'].oid == c
        assert b'homeassistant/b' in entries
        adapter.write_tree(handle, allowed_paths=list(entries))
    assert canonical(root) == before and adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('source', ['selected', 'unselected-base'])
@pytest.mark.parametrize('mutation', ['symlink', 'hardlink', 'wrong-type', 'missing'])
def test_write_tree_rechecks_all_current_blob_dependencies(tmp_path, monkeypatch, object_format, source, mutation):
    import zlib
    root, adapter = object_format_fixture(tmp_path, object_format)
    binding, calls = ids(), []
    before = canonical(root)
    original = adapter._raw
    with adapter.isolated_index(tree(root), **binding) as handle:
        if source == 'selected':
            oid = adapter.write_blob_stream(b'new selected dependency').oid
            adapter.update_index(handle, [n.IndexEntry(b'homeassistant/new', b'100644', oid, 0)])
        else:
            oid = next(entry.oid for entry in adapter.index_entries(handle) if entry.path == b'homeassistant/b')
        entries = adapter.index_entries(handle)
        private = adapter._index(handle)
        private_before = Path(os.fsdecode(private)).read_bytes()
        leaf = root / METADATA / 'objects' / oid[:2] / oid[2:]
        foreign = root / 'foreign-current-dependency'
        leaf.rename(foreign)
        if mutation == 'symlink':
            leaf.symlink_to(foreign)
        elif mutation == 'hardlink':
            os.link(foreign, leaf)
        elif mutation == 'wrong-type':
            leaf.write_bytes(zlib.compress(b'tree 0\0'))
        snapshot = (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns)
        def raw(command, **kwargs):
            if command[0] == 'write-tree':
                calls.append(command)
            return original(command, **kwargs)
        monkeypatch.setattr(adapter, '_raw', raw)
        with pytest.raises((n.InvalidInput, n.GitFailure)):
            adapter.write_tree(handle, allowed_paths=[entry.path for entry in entries])
        assert not calls
        assert Path(os.fsdecode(private)).read_bytes() == private_before
        assert (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns) == snapshot
    assert canonical(root) == before and adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('scenario,expected_kind,expected_xy', [
    ('modify', b'1', b'.M'), ('staged-modify', b'1', b'M.'), ('modify-both', b'1', b'MM'),
    ('add', b'1', b'A.'), ('add-modify', b'1', b'AM'), ('add-delete', b'1', b'AD'),
    ('intent-add', b'1', b'.A'), ('delete-intent-add', b'1', b'DA'),
    ('intent-add-delete', b'1', b'.D'), ('intent-delete-missing', b'1', b'DD'),
    ('intent-delete-type', b'1', b'DA'), ('add-type', b'1', b'AT'), ('mode', b'1', b'.M'),
    ('delete', b'1', b'.D'), ('staged-delete', b'1', b'D.'),
    ('type', b'1', b'.T'), ('staged-type', b'1', b'T.'), ('type-back', b'1', b'TT'),
    ('rename', b'2', b'R.'), ('rename-modify', b'2', b'RM'), ('rename-delete', b'2', b'RD'), ('rename-type', b'2', b'RT'),
])
def test_status_accepts_independent_real_git_axes(tmp_path, object_format, scenario, expected_kind, expected_xy):
    root, adapter = object_format_fixture(tmp_path, object_format)
    path = b'homeassistant/a'
    full = root / os.fsdecode(path)
    if scenario.startswith('rename'):
        path = b'homeassistant/renamed'
        git(root, 'mv', '--', 'homeassistant/a', os.fsdecode(path))
        full = root / os.fsdecode(path)
        if scenario == 'rename-modify':
            full.write_bytes(b'renamed worktree content')
        elif scenario == 'rename-delete':
            full.unlink()
        elif scenario == 'rename-type':
            full.unlink()
            full.symlink_to('b')
    elif scenario in ('add', 'add-modify', 'add-delete', 'intent-add', 'intent-add-delete', 'add-type'):
        path = b'homeassistant/new'
        full = root / os.fsdecode(path)
        full.write_bytes(b'new file')
        git(root, 'add', *(['-N'] if scenario.startswith('intent-add') else []), '--', os.fsdecode(path))
        if scenario == 'add-modify':
            full.write_bytes(b'changed new file')
        elif scenario in ('add-delete', 'intent-add-delete'):
            full.unlink()
        elif scenario == 'add-type':
            full.unlink()
            full.symlink_to('b')
    elif scenario in ('delete-intent-add', 'intent-delete-missing', 'intent-delete-type'):
        git(root, 'rm', '--cached', '--', os.fsdecode(path))
        git(root, 'add', '-N', '--', os.fsdecode(path))
        if scenario != 'delete-intent-add':
            full.unlink()
            if scenario == 'intent-delete-type':
                full.symlink_to('b')
    elif scenario in ('delete', 'staged-delete'):
        full.unlink()
        if scenario == 'staged-delete':
            git(root, 'add', '-u', '--', os.fsdecode(path))
    elif scenario in ('type', 'staged-type', 'type-back'):
        full.unlink()
        full.symlink_to('b')
        if scenario != 'type':
            git(root, 'add', '--', os.fsdecode(path))
        if scenario == 'type-back':
            full.unlink()
            full.write_bytes(b'regular again')
    elif scenario == 'mode':
        full.chmod(0o755)
    else:
        full.write_bytes(b'staged or unstaged edit')
        if scenario != 'modify':
            git(root, 'add', '--', os.fsdecode(path))
        if scenario == 'modify-both':
            full.write_bytes(b'another worktree edit')
    # Expected identities are independently generated by Git, not parser tables.
    raw = git(root, 'status', '--porcelain=v2', '-z', '--untracked-files=no')
    assert raw.startswith(expected_kind + b' ' + expected_xy + b' ')
    before = canonical(root)
    records = adapter.status()
    assert len(records) == 1
    record, = records
    assert (record.kind, record.xy, record.path) == (expected_kind, expected_xy, path)
    if expected_kind == b'2':
        assert record.original_path == b'homeassistant/a'
    assert canonical(root) == before and adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_real_rename_then_intent_to_add_has_two_independent_records(tmp_path, object_format):
    root, adapter = object_format_fixture(tmp_path, object_format)
    git(root, 'mv', 'homeassistant/a', 'homeassistant/moved')
    git(root, 'rm', '--cached', 'homeassistant/moved')
    git(root, 'add', '-N', 'homeassistant/moved')
    before = canonical(root)
    records = adapter.status()
    assert [(r.kind, r.xy, r.path) for r in records] == [
        (b'1', b'D.', b'homeassistant/a'), (b'1', b'.A', b'homeassistant/moved')]
    assert canonical(root) == before


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
@pytest.mark.parametrize('query', ['named', 'absent'])
@pytest.mark.parametrize('corruption', ['short', 'zero', 'missing-object', 'truncated', 'duplicate', 'bad-name', 'bad-peel', 'orphan-peel', 'false-sorted'])
def test_corrupt_packed_storage_never_authorizes_ref_absence(tmp_path, object_format, query, corruption):
    root, adapter = object_format_fixture(tmp_path, object_format)
    name = 'refs/ha-ops/broken'
    git(root, 'update-ref', name, head(root))
    git(root, 'pack-refs', '--all')
    packed = root / METADATA / 'packed-refs'
    width = 40 if object_format == 'sha1' else 64
    original = packed.read_bytes()
    line = head(root).encode() + b' ' + name.encode() + b'\n'
    replacement = {
        'short': b'short ' + name.encode() + b'\n',
        'zero': b'0' * width + b' ' + name.encode() + b'\n',
        'missing-object': b'a' * width + b' ' + name.encode() + b'\n',
        'truncated': line[:-1], 'duplicate': line + line,
        'bad-name': head(root).encode() + b' refs/ha-ops/../bad\n',
        'bad-peel': line + b'^short\n', 'orphan-peel': b'^' + head(root).encode() + b'\n' + line,
        'false-sorted': line,
    }[corruption]
    data = original.replace(line, replacement)
    if corruption == 'false-sorted':
        header, *records = data.splitlines(keepends=True)
        data = header + b''.join(reversed(records))
    packed.write_bytes(data)
    before = canonical(root), packed.read_bytes(), packed.stat().st_mtime_ns
    with pytest.raises((n.InvalidInput, n.GitFailure)):
        adapter.read_ref(name if query == 'named' else 'refs/ha-ops/absent')
    assert (canonical(root), packed.read_bytes(), packed.stat().st_mtime_ns) == before
    assert adapter.resources() == ()


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_valid_packed_peeled_and_loose_override_observations(tmp_path, object_format):
    root, adapter = object_format_fixture(tmp_path, object_format)
    name, oid = 'refs/ha-ops/good', head(root)
    git(root, 'update-ref', name, oid)
    git(root, 'tag', '-a', 'fixture-tag', '-m', 'annotated fixture')
    git(root, 'pack-refs', '--all')
    packed = root / METADATA / 'packed-refs'
    before = packed.read_bytes(), packed.stat().st_mtime_ns, canonical(root)
    assert b'\n^' in before[0]
    assert adapter.read_ref(name).oid == oid
    assert adapter.read_ref('refs/ha-ops/absent').oid is None
    assert adapter.read_ref('refs/tags/fixture-tag').oid == git(root, 'rev-parse', 'refs/tags/fixture-tag').strip().decode()
    assert (packed.read_bytes(), packed.stat().st_mtime_ns, canonical(root)) == before
    write(root, b'homeassistant/a', b'next commit')
    git(root, 'add', '--', 'homeassistant/a')
    git(root, 'commit', '-m', 'loose override fixture')
    next_oid = head(root)
    adapter.update_ref(name, next_oid, expected_old=oid)
    assert adapter.read_ref(name).oid == next_oid
    assert packed.read_bytes() == before[0]


@pytest.mark.parametrize('exit_code', [0, 1])
@pytest.mark.parametrize('channel', ['stderr', 'stdout'])
def test_ref_observation_diagnostics_are_failures_even_with_successful_exit(repo, monkeypatch, exit_code, channel):
    root, adapter = repo
    original = adapter._argv
    def argv(command, **kwargs):
        args, env = original(command, **kwargs)
        if command[:3] == ('show-ref', '--verify', '--quiet'):
            script = "import sys;sys." + channel + ".buffer.write(b'observation warning\\n');sys.exit(" + str(exit_code) + ")"
            return [sys.executable, '-c', script], env
        return args, env
    monkeypatch.setattr(adapter, '_argv', argv)
    before = canonical(root)
    with pytest.raises((n.GitFailure, n.InvalidInput)) as error:
        adapter.read_ref('refs/ha-ops/absent')
    if channel == 'stderr':
        assert error.value.stderr == b'observation warning\n'
        assert str(error.value) == 'git-observation-diagnostics'
    else:
        assert str(error.value) == 'malformed-quiet-ref-output'
    assert canonical(root) == before


def observable_empty_roundtrip(target):
    before = n.NativeGit._materialization_stat(os.lstat(target))
    with open(target, 'wb') as stream:
        stream.write(b'changed-and-truncated')
    with open(target, 'wb'):
        pass
    # Natural same-clock-tick roundtrips may preserve every observed field.
    # Establish distinct timestamp evidence rather than depending on clock speed.
    info = os.lstat(target)
    os.utime(target, ns=(info.st_atime_ns, before[6] + 1_000_000_000))
    after = n.NativeGit._materialization_stat(os.lstat(target))
    assert before[:6] == after[:6] and before[6:] != after[6:], (before, after)


def materialization_commit(root, adapter, paths):
    blobs = [adapter.write_blob_stream(b'materialized\0\r\n').oid,
             adapter.write_blob_stream(b'').oid]
    with adapter.isolated_index(None, **ids()) as handle:
        adapter.update_index(handle, [n.IndexEntry(path, b'100755' if i % 2 else b'100644',
                                                  blobs[i == 0], 0) for i, path in enumerate(paths)])
        oid = adapter.write_tree(handle, allowed_paths=paths)
    return git(root, 'commit-tree', oid, '-m', 'materialization fixture').strip().decode()


def test_materialization_real_low_nofile_reserves_all_before_first_write(repo):
    root, adapter = repo
    paths = [(b'homeassistant/shared/deep/' if i % 3 == 0 else
              b'homeassistant/parent%03d/deep/' % i if i % 3 == 1 else
              b'homeassistant/') + b'file%03d' % i for i in range(120)]
    commit = materialization_commit(root, adapter, paths)
    before = canonical(root)
    code = '''
import resource,stat
paths = ''' + repr(paths) + '''
original = g.copy_blob
# Warm Python's entropy source before measuring task-owned descriptors.
uuid.uuid4()
fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
descriptors_before = len(os.listdir(fd_directory))
calls = []
def copy(oid, destination):
 if not calls:
  base = os.path.join(g._resource_path(g.resources()[0]['resource_id']),b'tree')
  for path in paths:
   info = os.stat(os.path.join(base,path),follow_symlinks=False)
   assert stat.S_ISREG(info.st_mode) and info.st_size == 0 and info.st_nlink == 1
 calls.append(oid)
 return original(oid,destination)
g.copy_blob = copy
soft,hard = resource.getrlimit(resource.RLIMIT_NOFILE)
assert hard >= 64
resource.setrlimit(resource.RLIMIT_NOFILE,(64,hard))
with g.temporary_worktree(sys.argv[4],paths=paths,operation_id=sys.argv[2],owner_id=sys.argv[3]) as handle:
 base = g.worktree_path(handle)
 for i,path in enumerate(paths):
  with open(os.path.join(base,path),'rb') as stream:
   assert stream.read() == (b'' if i == 0 else b'materialized\\0\\r\\n')
  assert stat.S_IMODE(os.stat(os.path.join(base,path)).st_mode) == (0o755 if i % 2 else 0o644)
assert len(calls) == len(paths)
assert len(os.listdir(fd_directory)) == descriptors_before, (descriptors_before,os.listdir(fd_directory))
assert not g.resources()
assert not os.path.exists(os.path.join(g.metadata,b'worktrees')) or not os.listdir(os.path.join(g.metadata,b'worktrees'))
assert set(os.listdir(g.resources_root)) == {b'journal.json',b'journal.lock'}
print('120 files; soft RLIMIT_NOFILE=64; exact bytes/modes; all reservations empty before first copy; cleanup complete')
'''
    binding = ids()
    process = child(root, code, binding['operation_id'], binding['owner_id'], commit)
    out, error = process.communicate(timeout=45)
    assert process.returncode == 0, (out, error)
    assert b'120 files; soft RLIMIT_NOFILE=64' in out
    assert adapter.resources() == ()
    assert canonical(root) == before


@pytest.mark.parametrize('when', ['initial', 'after-write', 'failure'])
@pytest.mark.parametrize('change', ['regular', 'symlink', 'fifo', 'hardlink', 'grow', 'grow-truncate', 'chmod', 'addition'])
def test_materialization_refuses_foreign_capture_and_retains_addressed_debt(repo, monkeypatch, when, change):
    root, adapter = repo
    paths = [b'homeassistant/a', b'homeassistant/b']
    binding, before = ids(), canonical(root)
    original_capture, original_copy = adapter._capture_materialization, adapter.copy_blob
    state, copies = {}, []
    foreign = root / 'foreign-sentinel'
    foreign.write_bytes(b'FOREIGN')
    os.chmod(foreign, 0o640)
    sentinel = (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns)

    def mutate(handle):
        if state:
            return
        base = adapter.worktree_path(handle)
        target = os.path.join(base, paths[1])
        saved = os.path.join(os.fsencode(root), b'saved-owned-reservation')
        state.update(rid=handle.resource_id, target=target, saved=saved)
        if change in ('regular', 'symlink', 'fifo', 'hardlink'):
            os.rename(target, saved)
            if change == 'regular':
                # Same bytes as the empty reservation still have foreign identity.
                write(base, paths[1], b'')
            elif change == 'symlink':
                os.symlink(os.fsencode(foreign), target)
            elif change == 'fifo':
                os.mkfifo(target)
            else:
                os.link(foreign, target)
        elif change == 'addition':
            state['target'] = os.path.join(base, b'foreign-addition')
            write(base, b'foreign-addition', b'FOREIGN ADDITION')
        elif change == 'chmod':
            os.chmod(target, 0o644)
        elif change == 'grow-truncate':
            observable_empty_roundtrip(target)
        else:
            with open(target, 'wb') as stream:
                stream.write(b'FOREIGN CONTENT')
        state['foreign_info'] = os.lstat(state['target'])
        if change == 'regular':
            state['foreign_bytes'] = Path(os.fsdecode(target)).read_bytes()

    def capture(handle, rootfd, evidence):
        if when == 'initial' or (when == 'after-write' and copies):
            mutate(handle)
        return original_capture(handle, rootfd, evidence)

    def copy(oid, destination):
        copies.append(oid)
        result = original_copy(oid, destination)
        if when == 'failure':
            mutate(next(value[1] for value in adapter._active.values()))
            raise RuntimeError('producer-primary')
        return result

    monkeypatch.setattr(adapter, '_capture_materialization', capture)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    expected = RuntimeError if when == 'failure' else n.InvalidInput
    with pytest.raises(expected) as error:
        with adapter.temporary_worktree(head(root), paths=paths, **binding):
            pytest.fail('foreign mutation was accepted')
    if when == 'failure':
        assert str(error.value) == 'producer-primary'
    assert len(copies) == (0 if when == 'initial' else 1)
    record = adapter.resources()[0]
    assert record['resource_id'] == state['rid'] and record['phase'] == 'debt'
    assert record['errors']
    if change in ('regular', 'symlink', 'fifo', 'hardlink'):
        item = record['entries'].get(n._encode(b'tree/' + paths[1]))
        assert item is None or item[1] != state['foreign_info'].st_ino
        assert os.lstat(state['target']).st_ino == state['foreign_info'].st_ino
        if change == 'regular':
            assert Path(os.fsdecode(state['target'])).read_bytes() == state['foreign_bytes']
    elif change == 'addition':
        assert n._encode(b'tree/foreign-addition') not in record['entries']
        assert Path(os.fsdecode(state['target'])).read_bytes() == b'FOREIGN ADDITION'
    assert (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns) == sentinel
    monkeypatch.undo()
    if change in ('regular', 'symlink', 'fifo', 'hardlink'):
        os.unlink(state['target'])
        os.rename(state['saved'], state['target'])
    elif change == 'addition':
        os.unlink(state['target'])
    else:
        # Restore only this fixture's mutation; durable cleanup uses exact
        # identity/mode/digest, not unstable reservation timestamps.
        with open(state['target'], 'wb'):
            pass
        os.chmod(state['target'], 0o600)
    assert adapter.recover_resource(state['rid'], **binding).state == 'removed'
    assert adapter.resources() == ()
    assert canonical(root) == before


@pytest.mark.parametrize('where', ['root', 'parent'])
@pytest.mark.parametrize('when', ['initial', 'later'])
def test_materialization_refuses_directory_substitution_without_cleanup_adoption(repo, monkeypatch, where, when):
    root, adapter = repo
    binding, copies, state = ids(), [], {}
    original_capture, original_copy = adapter._capture_materialization, adapter.copy_blob
    def capture(handle, rootfd, evidence):
        if not state and (when == 'initial' or copies):
            base = adapter.worktree_path(handle)
            target = base if where == 'root' else os.path.join(base, b'homeassistant')
            saved = os.path.join(os.fsencode(root), b'saved-owned-directory')
            os.rename(target, saved)
            os.mkdir(target, 0o700)
            write(target, b'foreign', b'FOREIGN DIRECTORY')
            state.update(rid=handle.resource_id, target=target, saved=saved)
        return original_capture(handle, rootfd, evidence)
    def copy(oid, destination):
        copies.append(oid)
        return original_copy(oid, destination)
    monkeypatch.setattr(adapter, '_capture_materialization', capture)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    with pytest.raises(n.InvalidInput):
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a', b'homeassistant/b'], **binding):
            pass
    assert len(copies) == (0 if when == 'initial' else 1)
    assert adapter.resources()[0]['phase'] == 'debt'
    assert Path(os.fsdecode(os.path.join(state['target'], b'foreign'))).read_bytes() == b'FOREIGN DIRECTORY'
    monkeypatch.undo()
    os.unlink(os.path.join(state['target'], b'foreign'))
    os.rmdir(state['target'])
    os.rename(state['saved'], state['target'])
    assert adapter.recover_resource(state['rid'], **binding).state == 'removed'


@pytest.mark.parametrize('change', ['regular', 'symlink', 'fifo', 'hardlink', 'grow', 'grow-truncate', 'chmod'])
def test_materialization_reopen_fstat_refuses_stat_open_substitution(repo, monkeypatch, change):
    root, adapter = repo
    binding, state, copies = ids(), {}, []
    original_open, original_copy = os.open, adapter.copy_blob
    foreign = root / 'stat-open-foreign'
    foreign.write_bytes(b'SENTINEL')
    before = (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns)
    def open_file(path, flags, *args, **kwargs):
        if (not state and path == b'a' and kwargs.get('dir_fd') is not None
                and flags & os.O_WRONLY and not flags & os.O_CREAT):
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
            assert not flags & os.O_TRUNC
            handle = next(value[1] for value in adapter._active.values())
            target = os.path.join(adapter.worktree_path(handle), b'homeassistant/a')
            saved = os.path.join(os.fsencode(root), b'stat-open-owned')
            state.update(rid=handle.resource_id, target=target, saved=saved)
            if change in ('regular', 'symlink', 'fifo', 'hardlink'):
                os.rename(target, saved)
                if change == 'regular':
                    write(os.path.dirname(target), b'a', b'FOREIGN')
                elif change == 'symlink':
                    os.symlink(os.fsencode(foreign), target)
                elif change == 'fifo':
                    os.mkfifo(target)
                else:
                    os.link(foreign, target)
            elif change == 'chmod':
                os.chmod(target, 0o644)
            elif change == 'grow-truncate':
                observable_empty_roundtrip(target)
            else:
                with open(target, 'wb') as stream:
                    stream.write(b'changed')
        return original_open(path, flags, *args, **kwargs)
    def copy(oid, destination):
        copies.append(oid)
        return original_copy(oid, destination)
    monkeypatch.setattr(os, 'open', open_file)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    with pytest.raises((n.InvalidInput, OSError)):
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pass
    assert not copies and state
    assert adapter.resources()[0]['phase'] == 'debt'
    assert (foreign.read_bytes(), foreign.stat().st_mode, foreign.stat().st_mtime_ns) == before
    monkeypatch.undo()
    if change in ('regular', 'symlink', 'fifo', 'hardlink'):
        os.unlink(state['target'])
        os.rename(state['saved'], state['target'])
    else:
        with open(state['target'], 'wb'):
            pass
        os.chmod(state['target'], 0o600)
    assert adapter.recover_resource(state['rid'], **binding).state == 'removed'


@pytest.mark.parametrize('failure', ['reserve-open', 'reserve-fstat', 'parent-fstat', 'reopen-fstat',
                                      'fdopen', 'copy', 'interrupt', 'flush', 'fchmod', 'fsync', 'publish'])
def test_materialization_descriptor_balance_and_primary_failures(repo, monkeypatch, failure):
    root, adapter = repo
    binding, before = ids(), canonical(root)
    originals = {name: getattr(os, name) for name in ('open', 'fstat', 'fdopen', 'fchmod', 'fsync')}
    original_materialize, original_copy, original_publish = adapter._materialize, adapter.copy_blob, adapter._materialization_publish
    state = {'active': False, 'failed': False, 'target': None, 'writing': False, 'publishes': 0}
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def fail():
        state['failed'] = True
        if failure == 'interrupt':
            raise KeyboardInterrupt('materialization-injected-interrupt')
        raise OSError('materialization-injected-' + failure)
    def materialize(handle, entries):
        state['active'] = True
        try:
            return original_materialize(handle, entries)
        finally:
            state['active'] = False
    def open_file(path, flags, *args, **kwargs):
        reserve = state['active'] and path == b'a' and flags & os.O_CREAT
        reopen = state['active'] and path == b'a' and flags & os.O_WRONLY and not flags & os.O_CREAT
        if reserve and failure == 'reserve-open' and not state['failed']:
            fail()
        fd = originals['open'](path, flags, *args, **kwargs)
        if (reserve and failure == 'reserve-fstat') or (reopen and failure in ('reopen-fstat', 'fdopen', 'flush', 'fchmod', 'fsync')):
            state['target'] = fd
            state['writing'] = bool(reopen)
        if state['active'] and path == b'homeassistant' and flags & os.O_DIRECTORY and failure == 'parent-fstat':
            state['target'] = fd
        return fd
    def fstat(fd):
        if (state['active'] and fd == state['target'] and not state['failed']
                and failure in ('reserve-fstat', 'parent-fstat', 'reopen-fstat')):
            fail()
        return originals['fstat'](fd)
    def fdopen(fd, *args, **kwargs):
        if state['active'] and fd == state['target'] and failure == 'fdopen' and not state['failed']:
            fail()
        stream = originals['fdopen'](fd, *args, **kwargs)
        if state['active'] and fd == state['target'] and failure == 'flush':
            class Stream:
                def write(self, data):
                    return stream.write(data)
                def fileno(self):
                    return stream.fileno()
                def flush(self):
                    if not state['failed']:
                        fail()
                    return stream.flush()
                def close(self):
                    return stream.close()
            return Stream()
        return stream
    def fchmod(fd, mode):
        if state['active'] and fd == state['target'] and failure == 'fchmod' and not state['failed']:
            fail()
        return originals['fchmod'](fd, mode)
    def fsync(fd):
        if state['active'] and state['writing'] and fd == state['target'] and failure == 'fsync' and not state['failed']:
            fail()
        return originals['fsync'](fd)
    def copy(oid, destination):
        if failure in ('copy', 'interrupt') and not state['failed']:
            destination.write(b'partial-owned-output')
            fail()
        return original_copy(oid, destination)
    def publish(journal):
        if state['active']:
            state['publishes'] += 1
            if failure == 'publish' and state['publishes'] == 2 and not state['failed']:
                fail()
        return original_publish(journal)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    monkeypatch.setattr(adapter, '_materialization_publish', publish)
    for name, function in [('open', open_file), ('fstat', fstat), ('fdopen', fdopen), ('fchmod', fchmod), ('fsync', fsync)]:
        monkeypatch.setattr(os, name, function)
    expected = KeyboardInterrupt if failure == 'interrupt' else OSError
    with pytest.raises(expected, match='materialization-injected-' + failure):
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pass
    assert state['failed']
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert canonical(root) == before
    monkeypatch.undo()
    if adapter.resources():
        record = adapter.resources()[0]
        assert failure in ('reserve-fstat', 'parent-fstat')
        # These faults occur before metadata evidence exists. Remove only this
        # disposable fixture's unacknowledged reservation, then retry by ID.
        base = os.path.join(adapter._resource_path(record['resource_id']), b'tree/homeassistant')
        if failure == 'reserve-fstat':
            os.unlink(os.path.join(base, b'a'))
        else:
            os.rmdir(base)
        assert adapter.recover_resource(record['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()
    assert len(os.listdir(fd_directory)) == descriptors_before


def test_materialization_same_prefix_replacement_during_reservation_is_refused(repo, monkeypatch):
    root, adapter = repo
    binding, state, copies = ids(), {}, []
    original_mkdir, original_copy = os.mkdir, adapter.copy_blob
    def mkdir(path, *args, **kwargs):
        if path == b'homeassistant' and kwargs.get('dir_fd') is not None:
            state['visits'] = state.get('visits', 0) + 1
            if state['visits'] == 2:
                handle = next(value[1] for value in adapter._active.values())
                base = adapter.worktree_path(handle)
                target = os.path.join(base, path)
                saved = os.path.join(os.fsencode(root), b'first-reserved-parent')
                os.rename(target, saved)
                original_mkdir(target, 0o700)
                write(target, b'foreign', b'FOREIGN PREFIX')
                state.update(rid=handle.resource_id, target=target, saved=saved)
        return original_mkdir(path, *args, **kwargs)
    def copy(oid, destination):
        copies.append(oid)
        return original_copy(oid, destination)
    monkeypatch.setattr(os, 'mkdir', mkdir)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    with pytest.raises(n.InvalidInput, match='replaced-materialization-parent'):
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a', b'homeassistant/b'], **binding):
            pass
    assert not copies
    assert adapter.resources()[0]['phase'] == 'debt'
    assert Path(os.fsdecode(os.path.join(state['target'], b'foreign'))).read_bytes() == b'FOREIGN PREFIX'
    monkeypatch.undo()
    os.unlink(os.path.join(state['target'], b'foreign'))
    os.rmdir(state['target'])
    os.rename(state['saved'], state['target'])
    assert adapter.recover_resource(state['rid'], **binding).state == 'removed'


def test_materialization_hash_reads_grow_linearly_without_blob_buffering(repo, monkeypatch):
    root, adapter = repo
    paths = [b'homeassistant/many/file%02d' % i for i in range(30)]
    commit = materialization_commit(root, adapter, paths)
    original_safe, original_common, calls = adapter._materialization_record_file, adapter._record_file, []
    def record(original, resource, path, **kwargs):
        if path.startswith(b'tree/homeassistant/') and not kwargs.get('directory', False):
            calls.append(path)
        return original(resource, path, **kwargs)
    monkeypatch.setattr(adapter, '_materialization_record_file', lambda *args, **kwargs: record(original_safe, *args, **kwargs))
    monkeypatch.setattr(adapter, '_record_file', lambda *args, **kwargs: record(original_common, *args, **kwargs))
    with adapter.temporary_worktree(commit, paths=paths, **ids()):
        assert calls == [b'tree/' + path for path in paths]
    # One hash per new blob plus one exact complete cleanup verification.
    assert sorted(calls) == sorted([b'tree/' + path for path in paths] * 2)
    assert adapter.resources() == ()


@pytest.mark.parametrize('ack_failure', ['fstat', 'fsync', 'readback', 'publish'])
def test_materialization_partial_producer_error_survives_acknowledgement_failure(repo, monkeypatch, ack_failure):
    root, adapter = repo
    binding, state = ids(), {}
    original_fstat, original_fsync = os.fstat, os.fsync
    original_record, original_publish = adapter._materialization_record_file, adapter._materialization_publish
    before = canonical(root)
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def fstat(fd):
        if state.get('fd') == fd and not state.get('ack_failed'):
            state['ack_failed'] = True
            raise OSError('acknowledgement-fstat-failed')
        return original_fstat(fd)
    def fsync(fd):
        if state.get('fd') == fd and not state.get('ack_failed'):
            state['ack_failed'] = True
            raise OSError('acknowledgement-fsync-failed')
        return original_fsync(fd)
    def record(resource, path, **kwargs):
        if path == b'tree/homeassistant/a' and not state.get('ack_failed'):
            state['ack_failed'] = True
            raise OSError('acknowledgement-readback-failed')
        return original_record(resource, path, **kwargs)
    def publish(journal):
        if not state.get('ack_failed'):
            state['ack_failed'] = True
            raise OSError('acknowledgement-publish-failed')
        return original_publish(journal)
    def copy(oid, destination):
        destination.write(b'owned-partial-output')
        state['fd'] = destination.fileno()
        targets = {'fstat': (os, 'fstat', fstat), 'fsync': (os, 'fsync', fsync),
                   'readback': (adapter, '_materialization_record_file', record), 'publish': (adapter, '_materialization_publish', publish)}
        monkeypatch.setattr(*targets[ack_failure])
        raise RuntimeError('partial-producer-is-primary')
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    with pytest.raises(RuntimeError, match='partial-producer-is-primary') as error:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pass
    assert any('acknowledgement-' + ack_failure + '-failed' in message for message in error.value.cleanup_errors)
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert canonical(root) == before
    monkeypatch.undo()
    record = adapter.resources()[0]
    assert record['phase'] == 'debt'
    # The test owns this disposable ambiguous partial output. Restoring only
    # its prior acknowledged bytes permits exact addressed recovery.
    path = os.path.join(adapter._resource_path(record['resource_id']), b'tree/homeassistant/a')
    with open(path, 'wb'):
        pass
    assert adapter.recover_resource(record['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()


@pytest.mark.parametrize('primary_stage', ['fstat', 'readback', 'fsync', 'producer', 'interrupt'])
@pytest.mark.parametrize('close_where', ['stream', 'parent', 'root'])
def test_materialization_first_error_survives_writer_parent_root_close_double_fault(repo, monkeypatch, primary_stage, close_where):
    root, adapter = repo
    binding, before = ids(), canonical(root)
    error = KeyboardInterrupt('interrupt-primary') if primary_stage == 'interrupt' else (
        RuntimeError('producer-primary') if primary_stage == 'producer' else OSError('ack-' + primary_stage + '-primary'))
    state = {'active': False, 'armed': False, 'primary_raised': False, 'secondary_raised': False, 'close_attempts': 0, 'syncs': 0}
    originals = {name: getattr(os, name) for name in ('open', 'close', 'fdopen', 'fstat', 'fsync')}
    original_materialize, original_copy, original_record = adapter._materialize, adapter.copy_blob, adapter._materialization_record_file
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(handle, entries):
        state['active'] = True
        try:
            return original_materialize(handle, entries)
        finally:
            state['active'] = False
    def open_file(path, flags, *args, **kwargs):
        fd = originals['open'](path, flags, *args, **kwargs)
        if state['active'] and os.path.basename(os.fsencode(path)) == b'tree' and kwargs.get('dir_fd') is None:
            state.setdefault('root', fd)
        if state['active'] and path == b'a' and flags & os.O_WRONLY and not flags & os.O_CREAT:
            state['parent'] = kwargs['dir_fd']
        return fd
    def close(fd):
        if state['armed'] and state['active'] and close_where != 'stream' and fd == state.get(close_where) and not state['secondary_raised']:
            state['close_attempts'] += 1
            originals['close'](fd)
            state['secondary_raised'] = True
            raise OSError(close_where + '-close-secondary')
        return originals['close'](fd)
    def fdopen(fd, *args, **kwargs):
        stream = originals['fdopen'](fd, *args, **kwargs)
        if kwargs.get('buffering') != 0 or close_where != 'stream':
            return stream
        class Stream:
            def write(self, data):
                return stream.write(data)
            def flush(self):
                return stream.flush()
            def fileno(self):
                return stream.fileno()
            def close(self):
                state['close_attempts'] += 1
                stream.close()
                state['secondary_raised'] = True
                raise OSError('stream-close-secondary')
        return Stream()
    def raise_primary():
        state['primary_raised'] = True
        raise error
    def copy(oid, destination):
        state['writer'] = destination.fileno()
        if primary_stage in ('producer', 'interrupt'):
            destination.write(b'owned-partial')
            state['armed'] = True
            raise_primary()
        result = original_copy(oid, destination)
        state['armed'] = True
        return result
    def fstat(fd):
        if state['armed'] and fd == state['writer'] and primary_stage == 'fstat' and not state['primary_raised']:
            raise_primary()
        return originals['fstat'](fd)
    def fsync(fd):
        if state['armed'] and fd == state['writer']:
            state['syncs'] += 1
            if primary_stage == 'fsync' and state['syncs'] == 2 and not state['primary_raised']:
                raise_primary()
        return originals['fsync'](fd)
    def record(resource, path, **kwargs):
        if state['armed'] and path == b'tree/homeassistant/a' and primary_stage == 'readback' and not state['primary_raised']:
            raise_primary()
        return original_record(resource, path, **kwargs)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    monkeypatch.setattr(adapter, '_materialization_record_file', record)
    for name, function in [('open', open_file), ('close', close), ('fdopen', fdopen), ('fstat', fstat), ('fsync', fsync)]:
        monkeypatch.setattr(os, name, function)
    with pytest.raises(type(error)) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pytest.fail('double fault was accepted')
    assert raised.value is error
    assert state['primary_raised'] and state['secondary_raised'] and state['close_attempts'] == 1
    assert any(close_where + '-close-secondary' in message for message in error.cleanup_errors)
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert canonical(root) == before
    monkeypatch.undo()
    if adapter.resources():
        record = adapter.resources()[0]
        path = os.path.join(adapter._resource_path(record['resource_id']), b'tree/homeassistant/a')
        with open(path, 'wb'):
            pass
        os.chmod(path, 0o600)
        assert adapter.recover_resource(record['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()


@pytest.mark.parametrize('primary_kind', ['error', 'interrupt'])
@pytest.mark.parametrize('boundary', ['reservation-leaf', 'reservation-next-parent', 'reservation-parent',
                                      'reopen-leaf', 'verified-next-parent', 'predecessor-transfer'])
def test_materialization_traversal_reservation_close_preserves_primary_without_retry(repo, monkeypatch, primary_kind, boundary):
    root, adapter = repo
    binding = ids()
    primary = KeyboardInterrupt('boundary-primary') if primary_kind == 'interrupt' else OSError('boundary-primary')
    state = {'active': False, 'target': None, 'primary_raised': False, 'secondary_raised': False, 'closes': 0,
             'materialize_parents': 0, 'closed_number': None, 'unsafe_retry': False}
    originals = {name: getattr(os, name) for name in ('open', 'close', 'fstat', 'dup')}
    original_materialize, original_parent = adapter._materialize, adapter._materialization_parent
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(handle, entries):
        state['active'] = True
        try:
            return original_materialize(handle, entries)
        finally:
            state['active'] = False
    def parent(*args):
        state['materialize_parents'] += 1
        return original_parent(*args)
    def dup(fd):
        result = originals['dup'](fd)
        if result == state['closed_number']:
            state['closed_number'] = None
        if state['active'] and state['target'] is None and boundary == 'predecessor-transfer':
            state['target'] = result
        return result
    def open_file(path, flags, *args, **kwargs):
        if state['active'] and path == b'a' and flags & os.O_CREAT and boundary == 'reservation-parent' and not state['primary_raised']:
            state['target'] = kwargs['dir_fd']
            state['primary_raised'] = True
            raise primary
        fd = originals['open'](path, flags, *args, **kwargs)
        if fd == state['closed_number']:
            state['closed_number'] = None
        if state['active'] and path == b'homeassistant' and flags & os.O_DIRECTORY and boundary == 'predecessor-transfer':
            state.setdefault('successor', fd)
        if state['active'] and state['target'] is None:
            if boundary == 'reservation-leaf' and path == b'a' and flags & os.O_CREAT:
                state['target'] = fd
            elif boundary == 'reopen-leaf' and path == b'a' and flags & os.O_WRONLY and not flags & os.O_CREAT:
                state['target'] = fd
            elif boundary == 'reservation-next-parent' and path == b'homeassistant' and flags & os.O_DIRECTORY:
                state['target'] = fd
            elif boundary == 'verified-next-parent' and state['materialize_parents'] and path == b'homeassistant' and flags & os.O_DIRECTORY:
                state['target'] = fd
        return fd
    def fstat(fd):
        if state['active'] and fd == state['target'] and boundary not in ('reservation-parent', 'predecessor-transfer') and not state['primary_raised']:
            state['primary_raised'] = True
            raise primary
        return originals['fstat'](fd)
    def close(fd):
        if state['active'] and fd == state['closed_number']:
            state['unsafe_retry'] = True
        if state['active'] and fd == state['target'] and not state['primary_raised'] and boundary == 'predecessor-transfer':
            state['closes'] += 1
            originals['close'](fd)
            state['closed_number'] = fd
            state['primary_raised'] = True
            raise primary
        successor = boundary == 'predecessor-transfer' and state['primary_raised'] and fd == state.get('successor')
        if state['active'] and (fd == state['target'] or successor) and not state['secondary_raised']:
            if not successor:
                state['closes'] += 1
            originals['close'](fd)
            state['closed_number'] = fd
            state['secondary_raised'] = True
            raise OSError('boundary-close-secondary')
        return originals['close'](fd)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(adapter, '_materialization_parent', parent)
    for name, function in [('open', open_file), ('close', close), ('fstat', fstat), ('dup', dup)]:
        monkeypatch.setattr(os, name, function)
    with pytest.raises(type(primary)) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pass
    assert raised.value is primary and state['primary_raised'] and state['secondary_raised'] and state['closes'] == 1
    assert not state['unsafe_retry']
    assert any('boundary-close-secondary' in message for message in primary.cleanup_errors)
    assert len(os.listdir(fd_directory)) == descriptors_before
    monkeypatch.undo()
    if adapter.resources():
        record = adapter.resources()[0]
        base = os.path.join(adapter._resource_path(record['resource_id']), b'tree/homeassistant')
        if boundary == 'reservation-leaf':
            os.unlink(os.path.join(base, b'a'))
        elif boundary == 'reservation-next-parent':
            os.rmdir(base)
        assert adapter.recover_resource(record['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()


_READBACK_FAULTS = [(target, fault) for target in ('selected', 'owner', 'backlink')
                   for fault in ('file-fstat', 'file-read', 'file-close', 'parent-open', 'parent-fstat',
                                 'predecessor-close', 'predecessor-single', 'parent-final-close', 'parent-final-double')
                   if not (target == 'owner' and fault in ('predecessor-close', 'predecessor-single'))]


@pytest.mark.parametrize('primary_kind', ['error', 'interrupt'])
@pytest.mark.parametrize('target,fault', _READBACK_FAULTS)
def test_materialization_real_readback_syscalls_preserve_first_error_and_fd_balance(repo, monkeypatch, target, fault, primary_kind):
    root, adapter = repo
    binding, before = ids(), canonical(root)
    primary = KeyboardInterrupt('readback-syscall-primary') if primary_kind == 'interrupt' else OSError('readback-syscall-primary')
    path = {'selected': b'tree/homeassistant/a', 'owner': b'owner.json', 'backlink': b'tree/.git'}[target]
    state = {'active': False, 'reading': False, 'copied': False, 'primary_hit': False, 'secondary_hit': False,
             'root_fd': None, 'file_fd': None, 'parent_fd': None, 'previous': None, 'successor': None,
             'closed_numbers': set(), 'unsafe_retry': False, 'fault_closes': []}
    originals = {name: getattr(os, name) for name in ('open', 'close', 'fstat', 'read', 'dup')}
    original_materialize, original_copy = adapter._materialize, adapter.copy_blob
    # The old helper name supports executing these actual syscall guards against
    # the isolated pre-correction module; no helper is mocked to raise a fault.
    record_name = '_materialization_record_file' if hasattr(adapter, '_materialization_record_file') else '_record_file'
    original_record = getattr(adapter, record_name)
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(handle, entries):
        state['active'] = True
        try:
            return original_materialize(handle, entries)
        finally:
            state['active'] = False
    def record(resource, candidate, **kwargs):
        previous = state['reading']
        state['reading'] = state['active'] and candidate == path and (target != 'selected' or state['copied'])
        try:
            return original_record(resource, candidate, **kwargs)
        finally:
            state['reading'] = previous
    def copy(oid, destination):
        result = original_copy(oid, destination)
        state['copied'] = True
        return result
    def raise_primary():
        state['primary_hit'] = True
        raise primary
    def open_file(name, flags, *args, **kwargs):
        if state['reading'] and fault == 'parent-open' and kwargs.get('dir_fd') is None and not state['primary_hit']:
            raise_primary()
        fd = originals['open'](name, flags, *args, **kwargs)
        state['closed_numbers'].discard(fd)
        if state['reading'] and not state['primary_hit']:
            if flags & os.O_DIRECTORY:
                if kwargs.get('dir_fd') is None:
                    state['root_fd'] = fd
                elif fault in ('predecessor-close', 'predecessor-single') and state['previous'] is None:
                    state['previous'], state['successor'] = kwargs['dir_fd'], fd
            else:
                state['file_fd'], state['parent_fd'] = fd, kwargs['dir_fd']
        return fd
    def dup(fd):
        value = originals['dup'](fd)
        state['closed_numbers'].discard(value)
        return value
    def fstat(fd):
        if state['reading'] and not state['primary_hit']:
            if (fault == 'file-fstat' and fd == state['file_fd']) or (fault == 'parent-fstat' and fd == state['root_fd']):
                raise_primary()
        return originals['fstat'](fd)
    def read(fd, count):
        if state['reading'] and fd == state['file_fd'] and fault in ('file-read', 'parent-final-double') and not state['primary_hit']:
            raise_primary()
        return originals['read'](fd, count)
    def close(fd):
        if state['reading'] and fd in state['closed_numbers']:
            state['unsafe_retry'] = True
        if state['reading'] and not state['primary_hit']:
            first = ((fault == 'file-close' and fd == state['file_fd']) or
                     (fault in ('predecessor-close', 'predecessor-single') and fd == state['previous']) or
                     (fault == 'parent-final-close' and fd == state['parent_fd']))
            if first:
                originals['close'](fd)
                state['closed_numbers'].add(fd)
                state['fault_closes'].append(fd)
                raise_primary()
        if state['reading'] and state['primary_hit'] and not state['secondary_hit']:
            second = ((fault in ('file-fstat', 'file-read') and fd == state['file_fd']) or
                      (fault == 'parent-fstat' and fd == state['root_fd']) or
                      (fault in ('file-close', 'parent-final-double') and fd == state['parent_fd']) or
                      (fault == 'predecessor-close' and fd == state['successor']))
            if second:
                originals['close'](fd)
                state['closed_numbers'].add(fd)
                state['fault_closes'].append(fd)
                state['secondary_hit'] = True
                raise OSError('readback-close-secondary')
        return originals['close'](fd)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(adapter, record_name, record)
    monkeypatch.setattr(adapter, 'copy_blob', copy)
    for name, function in [('open', open_file), ('dup', dup), ('fstat', fstat), ('read', read), ('close', close)]:
        monkeypatch.setattr(os, name, function)
    with pytest.raises(type(primary)) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pytest.fail('readback fault was not reached')
    assert state['primary_hit'] and raised.value is primary
    assert not state['unsafe_retry']
    assert len(state['fault_closes']) == len(set(state['fault_closes']))
    if fault not in ('parent-open', 'parent-final-close', 'predecessor-single'):
        assert state['secondary_hit']
        assert any('readback-close-secondary' in value for value in primary.cleanup_errors)
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert canonical(root) == before
    monkeypatch.undo()
    if adapter.resources():
        resource = adapter.resources()[0]
        if target == 'selected':
            # This fixture alone owns the unacknowledged copied bytes. Restore
            # the last durable reservation evidence; marker/backlink stay intact.
            selected = os.path.join(adapter._resource_path(resource['resource_id']), path)
            with open(selected, 'wb'):
                pass
            os.chmod(selected, 0o600)
        assert adapter.recover_resource(resource['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()


_JOURNAL_PUBLICATION_FAULTS = [
    'lock-open', 'lock-acquire', 'lock-fstat', 'manifest-open', 'manifest-fstat',
    'manifest-read', 'manifest-close', 'owner-read', 'lock-unlock', 'lock-close',
    'publication-open', 'publication-fstat', 'publication-fdopen', 'publication-write',
    'publication-flush', 'publication-fsync', 'publication-close', 'publication-replace',
    'directory-open', 'directory-fstat', 'directory-fsync', 'directory-close',
    'publication-unlink',
]
_CAPTURE_LATER_FAULTS = ['root-fsync', 'owner-read', 'lock-acquire', 'lock-fstat',
                        'lock-unlock', 'lock-close', 'publication-fdopen',
                        'publication-write', 'publication-flush', 'publication-fsync',
                        'publication-close', 'directory-open', 'directory-fstat',
                        'directory-fsync', 'directory-close', 'publication-unlink']


@pytest.mark.parametrize('primary_kind', ['error', 'interrupt'])
@pytest.mark.parametrize('pending,target,fault',
    [(False, None, fault) for fault in _JOURNAL_PUBLICATION_FAULTS] +
    [(True, target, fault) for target in ('owner', 'backlink') for fault in _CAPTURE_LATER_FAULTS])
def test_materialization_real_journal_publication_syscalls_preserve_first_error(
        repo, monkeypatch, pending, target, fault, primary_kind):
    import fcntl
    root, adapter = repo
    binding, before = ids(), canonical(root)
    primary = KeyboardInterrupt('journal-publication-primary') if primary_kind == 'interrupt' else OSError('journal-publication-primary')
    secondary = OSError('journal-publication-secondary')
    state = {'active': False, 'capture': False, 'primary': False, 'secondary': False,
             'extra': False, 'fds': {}, 'closed': set(), 'unsafe_retry': False,
             'publications': {}, 'rootfd': None, 'baseline_read': False}
    originals = {name: getattr(os, name) for name in ('open', 'dup', 'close', 'fstat', 'read', 'fdopen', 'fsync', 'replace', 'unlink')}
    original_flock, original_materialize, original_capture = fcntl.flock, adapter._materialize, adapter._capture_materialization
    original_record = adapter._materialization_record_file
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(*args):
        state['active'] = True
        try:
            return original_materialize(*args)
        finally:
            state['active'] = False
    def capture(handle, rootfd, evidence):
        previous = state['capture']
        state['capture'], state['rootfd'] = True, rootfd
        try:
            return original_capture(handle, rootfd, evidence)
        finally:
            state['capture'] = previous
    def record(resource, path, **kwargs):
        previous = state['baseline_read']
        state['baseline_read'] = pending and state['capture'] and path == {'owner': b'owner.json', 'backlink': b'tree/.git'}.get(target)
        try:
            return original_record(resource, path, **kwargs)
        finally:
            state['baseline_read'] = previous
    def hit(point):
        if not state['active']:
            return
        if point == 'baseline-read' and pending and not state['primary']:
            state['primary'] = True
            raise primary
        if point != fault or (pending and not state['primary']):
            return
        key = 'secondary' if pending else 'primary'
        if not state[key]:
            state[key] = True
            raise secondary if pending else primary
    def open_file(path, flags, *args, **kwargs):
        kind = None
        if state['active']:
            name = os.path.basename(path)
            if name == b'journal.lock': kind = 'lock'
            elif name == b'journal.json': kind = 'manifest'
            elif name.startswith(b'.publish-'): kind = 'publication'
            elif path == adapter.resources_root and flags & os.O_DIRECTORY: kind = 'directory'
            elif name == b'owner.json': kind = 'owner'
            elif name == b'.git': kind = 'backlink'
            if kind: hit(kind + '-open')
        fd = originals['open'](path, flags, *args, **kwargs)
        state['closed'].discard(fd)
        state['fds'][fd] = kind
        if kind == 'publication': state['publications'][path] = fd
        return fd
    def dup(fd):
        value = originals['dup'](fd)
        state['closed'].discard(value)
        state['fds'][value] = None
        return value
    def fstat(fd):
        kind = state['fds'].get(fd)
        if kind: hit(kind + '-fstat')
        return originals['fstat'](fd)
    def read(fd, count):
        if state['baseline_read']: hit('baseline-read')
        kind = state['fds'].get(fd)
        if kind: hit(kind + '-read')
        return originals['read'](fd, count)
    def release_extra(point):
        # Exercise a second actual release fault with every first fault. Unlock
        # executes before reporting failure, and close executes exactly once.
        if state['active'] and state['primary'] and not state['extra'] and point == 'lock-unlock':
            state['extra'] = True
            raise OSError('journal-release-extra')
    def flock(fd, operation):
        if operation == fcntl.LOCK_EX: hit('lock-acquire')
        result = original_flock(fd, operation)
        if operation == fcntl.LOCK_UN:
            hit('lock-unlock')
            release_extra('lock-unlock')
        return result
    def close(fd):
        if state['active'] and fd in state['closed']: state['unsafe_retry'] = True
        result = originals['close'](fd)
        state['closed'].add(fd)
        kind = state['fds'].get(fd)
        if kind: hit(kind + '-close')
        if (state['active'] and state['primary'] and (not pending or state['secondary'])
                and kind in ('publication', 'directory', 'lock') and not state.get(kind + '_release')):
            state[kind + '_release'] = True
            raise OSError(kind + '-close-extra')
        return result
    def fdopen(fd, *args, **kwargs):
        kind = state['fds'].get(fd)
        if kind == 'publication': hit('publication-fdopen')
        stream = originals['fdopen'](fd, *args, **kwargs)
        if kind != 'publication': return stream
        class Stream:
            def fileno(self): return stream.fileno()
            def write(self, data):
                value = stream.write(data)
                hit('publication-write')
                # Unlink failure needs a still-owned temporary pathname.
                if fault == 'publication-unlink' and state['active'] and not state['extra']:
                    state['extra'] = True
                    raise OSError('publication-write-before-unlink') if pending else primary
                return value
            def flush(self):
                value = stream.flush(); hit('publication-flush'); return value
            def close(self):
                value = stream.close()
                state['closed'].add(fd)
                hit('publication-close')
                if (state['active'] and state['primary'] and (not pending or state['secondary'])
                        and not state.get('publication_release')):
                    state['publication_release'] = True
                    raise OSError('publication-close-extra')
                return value
        return Stream()
    def fsync(fd):
        if state['capture'] and fd == state['rootfd']: hit('root-fsync')
        kind = state['fds'].get(fd)
        if kind: hit(kind + '-fsync')
        return originals['fsync'](fd)
    def replace(source, destination):
        if source in state['publications']: hit('publication-replace')
        return originals['replace'](source, destination)
    def unlink(path, *args, **kwargs):
        if path in state['publications']:
            hit('publication-unlink')
        return originals['unlink'](path, *args, **kwargs)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(adapter, '_capture_materialization', capture)
    monkeypatch.setattr(adapter, '_materialization_record_file', record)
    monkeypatch.setattr(fcntl, 'flock', flock)
    for name, fn in [('open', open_file), ('dup', dup), ('fstat', fstat), ('read', read), ('close', close),
                     ('fdopen', fdopen), ('fsync', fsync), ('replace', replace), ('unlink', unlink)]:
        monkeypatch.setattr(os, name, fn)
    # An unlink fault follows an earlier write fault, so that exact write error
    # is the primary even when there was no collected baseline read error.
    if not pending and fault == 'publication-unlink':
        primary = KeyboardInterrupt('publication-write-before-unlink') if primary_kind == 'interrupt' else OSError('publication-write-before-unlink')
        original_hit = hit
        def hit(point):
            if point == 'publication-unlink' and state['active'] and not state['secondary']:
                state['primary'], state['secondary'] = True, True
                raise secondary
            return original_hit(point)
    with pytest.raises(BaseException) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **binding):
            pytest.fail('actual syscall fault was not reached')
    assert state['primary'] and (not pending or state['secondary'])
    assert raised.value is primary
    assert not state['unsafe_retry']
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert canonical(root) == before
    if pending:
        assert any('journal-publication-secondary' in value for value in primary.cleanup_errors)
    monkeypatch.undo()
    # Only these exact exclusive publication paths belong to this fixture. A
    # failed initial fstat cannot prove ownership to product cleanup; retain it
    # rather than adopt a current occupant. The fixture can remove its own file.
    for path in state['publications']:
        if os.path.lexists(path): os.unlink(path)
    if adapter.resources():
        resource = adapter.resources()[0]
        # Multiple publication/release faults can prevent all durable extension
        # of the baseline. Recovery must refuse those unacknowledged additions;
        # this fixture alone can remove its own empty reservations first.
        base = adapter._resource_path(resource['resource_id'])
        for candidate in (b'tree/homeassistant/a', b'tree/homeassistant'):
            path = os.path.join(base, candidate)
            if n._encode(candidate) not in resource['entries'] and os.path.lexists(path):
                if candidate.endswith(b'/a'):
                    assert os.stat(path).st_size == 0 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
                    os.unlink(path)
                else:
                    os.rmdir(path)
        assert adapter.recover_resource(resource['resource_id'], **binding).state == 'removed'
    assert adapter.resources() == ()


@pytest.mark.parametrize('primary_kind', ['parse', 'interrupt'])
def test_materialization_manifest_parse_and_three_release_failures(repo, monkeypatch, primary_kind):
    import fcntl
    root, adapter = repo
    state = {'active': False, 'manifest': None, 'lock': None, 'primary': None,
             'read': False, 'manifest_close': False, 'unlock': False, 'lock_close': False}
    originals = {name: getattr(os, name) for name in ('open', 'read', 'close')}
    original_flock, original_materialize = fcntl.flock, adapter._materialize
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(*args):
        state['active'] = True
        try: return original_materialize(*args)
        finally: state['active'] = False
    def open_file(path, *args, **kwargs):
        fd = originals['open'](path, *args, **kwargs)
        if state['active'] and os.path.basename(path) in (b'journal.lock', b'journal.json'):
            state['lock' if path.endswith(b'journal.lock') else 'manifest'] = fd
        return fd
    def read(fd, count):
        value = originals['read'](fd, count)
        if state['active'] and fd == state['manifest'] and not state['read']:
            state['read'] = True
            if primary_kind == 'interrupt':
                state['primary'] = KeyboardInterrupt('manifest-read-interruption')
                raise state['primary']
            # Exercise the real parser and retain its exact exception object.
            try: n._json(b'{invalid')
            except BaseException as exc:
                state['primary'] = exc
                raise
        return value
    def close(fd):
        value = originals['close'](fd)
        if state['active'] and state['primary'] is not None:
            key = 'manifest_close' if fd == state['manifest'] else 'lock_close' if fd == state['lock'] else None
            if key and not state[key]:
                state[key] = True
                raise OSError(key + '-secondary')
        return value
    def flock(fd, operation):
        value = original_flock(fd, operation)
        if state['active'] and state['primary'] is not None and operation == fcntl.LOCK_UN and not state['unlock']:
            state['unlock'] = True
            raise OSError('unlock-secondary')
        return value
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(fcntl, 'flock', flock)
    for name, fn in [('open', open_file), ('read', read), ('close', close)]: monkeypatch.setattr(os, name, fn)
    with pytest.raises(BaseException) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **ids()): pass
    assert raised.value is state['primary'] and state['read']
    assert state['manifest_close'] and state['unlock'] and state['lock_close']
    assert all(any(label + '-secondary' in text for text in raised.value.cleanup_errors)
               for label in ('manifest_close', 'unlock', 'lock_close'))
    assert len(os.listdir(fd_directory)) == descriptors_before
    monkeypatch.undo()
    assert adapter.resources() == ()


@pytest.mark.parametrize('primary_kind', ['error', 'interrupt'])
def test_materialization_publication_cleanup_refuses_substituted_temporary_file(repo, monkeypatch, primary_kind):
    root, adapter = repo
    primary = OSError('publication-transfer-primary') if primary_kind == 'error' else KeyboardInterrupt('publication-transfer-primary')
    state = {'active': False, 'fd': None, 'path': None, 'injected': False}
    original_materialize, original_open, original_fdopen = adapter._materialize, os.open, os.fdopen
    fd_directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    descriptors_before = len(os.listdir(fd_directory))
    def materialize(*args):
        state['active'] = True
        try: return original_materialize(*args)
        finally: state['active'] = False
    def open_file(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if state['active'] and os.path.basename(path).startswith(b'.publish-') and state['fd'] is None:
            state['fd'], state['path'] = fd, path
        return fd
    def fdopen(fd, *args, **kwargs):
        if state['active'] and fd == state['fd'] and not state['injected']:
            state['injected'] = True
            os.unlink(state['path'])
            with open(state['path'], 'wb') as stream: stream.write(b'foreign publication sentinel')
            os.chmod(state['path'], 0o600)
            raise primary
        return original_fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(adapter, '_materialize', materialize)
    monkeypatch.setattr(os, 'open', open_file)
    monkeypatch.setattr(os, 'fdopen', fdopen)
    with pytest.raises(BaseException) as raised:
        with adapter.temporary_worktree(head(root), paths=[b'homeassistant/a'], **ids()): pass
    assert state['injected'] and raised.value is primary
    assert any('unproven-materialization-publication-file' in value for value in primary.cleanup_errors)
    assert len(os.listdir(fd_directory)) == descriptors_before
    assert Path(os.fsdecode(state['path'])).read_bytes() == b'foreign publication sentinel'
    monkeypatch.undo()
    os.unlink(state['path'])  # Only the foreign sentinel created by this fixture.
    assert adapter.resources() == ()
