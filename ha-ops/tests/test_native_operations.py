"""Native state contracts. All observations are inert, deterministic fixtures."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest import mock

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import native_operations as native


def uid():
    return str(uuid.uuid4())


def identity(domain="save"):
    scope = {"revision": 0, "allowlist": ["homeassistant"], "exclude_digest": "a" * 64}
    root = {"path": "/", "device": 1, "inode": 2, "mount_digest": "b" * 64, "topology_digest": "c" * 64}
    refs = {key: {"name": "refs/heads/" + key, "oid": "d" * 40} for key in ("base", "local", "remote")}
    result = {"direction": domain, "execution_policy": "inert-v1", "scope": scope,
              "root": root, "refs": refs, "paths": [
                  {"path": "homeassistant/a.yaml", "type": "file", "mode": 0o100640,
                   "content_digest": "e" * 64, "metadata_digest": "f" * 64},
                  {"path": "homeassistant/absent", "type": "missing", "mode": None,
                   "content_digest": None, "metadata_digest": None}],
              "effect": {"before": "1" * 64, "after": "2" * 64}}
    if domain == "migration":
        result["migration"] = {"original_checkout": dict(root, path="/data/old-checkout"),
                               "candidate_commit": "3" * 40, "post_refs": copy.deepcopy(refs)}
    if domain == "scope":
        result["scope_transaction"] = {"old": copy.deepcopy(scope), "new": dict(scope, revision=1, allowlist=["addons", "homeassistant"])}
    return result


def observed(value):
    return {"available": True, "identity": copy.deepcopy(value)}


def seed(store, domain="save"):
    value = identity(domain)
    record = store.preview(store.read()["revision"], domain, value, b"reviewed complete diff\n")
    decisions = {"homeassistant/a.yaml": "ha"} if domain in {"save", "apply"} else {}
    record = store.review(store.read()["revision"], record["id"], decisions, uid(), observed(value))
    return record


def claim(store, record, command=None):
    return store.claim(store.read()["revision"], command or uid(), record["id"],
                       record["approval"]["binding"], observed(record["identity"]))[1]


@pytest.fixture
def store(tmp_path):
    return native.NativeOperationStore(tmp_path.resolve())


def test_empty_store_strict_roundtrip_and_private_storage(store):
    state = store.read()
    assert state["schema"] == 1 and state["revision"] == 0 and not state["records"]
    assert native.NativeOperationStore(store.data_root).read() == state
    root = store.data_root / "native-operations"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "state.json").stat().st_mode & 0o777 == 0o600
    assert (root / "lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("domain", sorted(native.DOMAINS))
def test_refresh_update_preserve_artifact_without_page_draft(store, domain):
    record = seed(store, domain)
    before = store.read()
    view = store.refresh()
    assert view["draft"] == {} and "decisions" not in view["candidates"][domain]
    assert "approval" not in view["candidates"][domain]
    assert store.read_diff(record["id"]) == b"reviewed complete diff\n"
    other = native.NativeOperationStore(store.data_root)
    other.lifecycle(before["revision"], "future-version", {record["id"]: observed(record["identity"])})
    after = other.read()
    assert after["records"][record["id"]] == record
    assert after["app_version"] == "future-version" and after["active"] is None
    assert other.read_diff(record["id"]) == store.read_diff(record["id"])


CHANGES = ["bytes", "mode", "metadata", "delete", "new", "remote", "base", "local", "root", "mount", "topology", "scope", "policy", "direction", "unavailable", "missing_observation"]


def changed_observation(value, change):
    result = observed(value)
    current = result["identity"]
    if change == "bytes":
        current["paths"][0]["content_digest"] = "0" * 64
    elif change == "mode":
        current["paths"][0]["mode"] = 0o100600
    elif change == "metadata":
        current["paths"][0]["metadata_digest"] = "0" * 64
    elif change == "delete":
        current["paths"][0].update(type="missing", mode=None, content_digest=None, metadata_digest=None)
    elif change == "new":
        current["paths"][1].update(type="file", mode=0o100600, content_digest="0" * 64, metadata_digest="0" * 64)
    elif change in {"remote", "base", "local"}:
        current["refs"][change]["oid"] = "0" * 40
    elif change == "root":
        current["root"]["inode"] += 1
    elif change in {"mount", "topology"}:
        current["root"][change + "_digest"] = "0" * 64
    elif change == "scope":
        current["scope"]["revision"] += 1
    elif change == "policy":
        current["execution_policy"] = "incompatible-v2"
    elif change == "direction":
        current["direction"] = "apply"
    elif change == "unavailable":
        result["available"] = False
    else:
        return None
    return result


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("boundary", ["claim", "effect", "update"])
def test_each_identity_change_loses_authority_preserves_diagnostics(store, change, boundary):
    record = seed(store)
    observation = changed_observation(record["identity"], change)
    if boundary == "claim":
        with pytest.raises(native.Conflict, match="fresh Preview"):
            store.claim(store.read()["revision"], uid(), record["id"], record["approval"]["binding"], observation)
    elif boundary == "effect":
        worker = claim(store, record)
        with pytest.raises(native.Conflict, match="fresh Preview"):
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observation)
    else:
        store.lifecycle(store.read()["revision"], "new-app", {record["id"]: observation})
    state = store.read()
    stale = state["records"][record["id"]]
    assert stale["status"] == "stale" and stale["approval"] is None and stale["decisions"] == {}
    assert store.read_diff(record["id"]) == b"reviewed complete diff\n"
    with pytest.raises(native.Conflict):
        store.review(state["revision"], record["id"], {"homeassistant/a.yaml": "ha"}, uid(), observed(record["identity"]))
    replacement = store.preview(state["revision"], "save", identity(), b"replacement diff")
    assert replacement["id"] != record["id"] and replacement["decisions"] == {} and replacement["approval"] is None


def test_readers_do_not_take_over_restart_fences_old_owner(store):
    record = seed(store, "migration")
    worker = claim(store, record)
    observer = native.NativeOperationStore(store.data_root)
    assert observer.read()["workers"][worker["id"]] == worker
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    observer.lifecycle(observer.read()["revision"], "updated", {record["id"]: observed(record["identity"])})
    state = observer.read()
    recovered = state["workers"][worker["id"]]
    assert recovered["phase"] == "recovery_required" and recovered["epoch"] > worker["epoch"]
    assert state["records"][record["id"]]["approval"] == record["approval"]
    assert len(state["records"][record["id"]]["checkpoints"]) == 3
    evidence = dict(record["identity"]["effect"], marker=worker["marker"])
    with pytest.raises(native.Conflict, match="fenced"):
        store.checkpoint(state["revision"], worker["id"], worker["epoch"], "completed", evidence=evidence)
    with pytest.raises(native.Conflict, match="evidence"):
        observer.checkpoint(state["revision"], worker["id"], recovered["epoch"], "completed", evidence=dict(evidence, after="0" * 64))
    observer.checkpoint(state["revision"], worker["id"], recovered["epoch"], "completed", evidence=evidence)
    assert observer.read()["active"] is None


def test_accepted_restart_invalidates_and_never_relaunches(store):
    record = seed(store)
    worker = claim(store, record)
    other = native.NativeOperationStore(store.data_root)
    other.lifecycle(other.read()["revision"], "restarted", {record["id"]: observed(record["identity"])})
    state = other.read()
    assert state["active"] is None and state["workers"][worker["id"]]["phase"] == "invalidated"
    assert state["records"][record["id"]]["approval"] is None


def test_scope_cancellation_and_publication_evidence(store):
    record = seed(store, "scope")
    old = record["identity"]["scope_transaction"]["old"]
    assert store.read()["active_scope"] == old
    store.cancel(store.read()["revision"], record["id"])
    assert store.read()["active_scope"] == old
    record = seed(store, "scope")
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    other = native.NativeOperationStore(store.data_root)
    other.lifecycle(other.read()["revision"], "restart", {})
    state = other.read()
    assert state["active_scope"] == old
    assert state["records"][record["id"]]["status"] == "recovery_required"
    epoch = state["workers"][worker["id"]]["epoch"]
    other.checkpoint(state["revision"], worker["id"], epoch, "completed", evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    assert other.read()["active_scope"] == record["identity"]["scope_transaction"]["new"]


def test_replay_is_durable_binding_consumed_and_terminal(store):
    record = seed(store)
    command = uid()
    worker = claim(store, record, command)
    state = store.read()
    other = native.NativeOperationStore(store.data_root)
    accepted, replay = other.claim(0, command, record["id"], record["approval"]["binding"], None)
    assert not accepted and replay == worker and other.read() == state
    with pytest.raises(native.Conflict, match="different payload"):
        other.claim(state["revision"], command, record["id"], "0" * 64, observed(record["identity"]))
    with pytest.raises(native.Conflict, match="unavailable"):
        store.claim(state["revision"], uid(), record["id"], record["approval"]["binding"], observed(record["identity"]))
    store.checkpoint(state["revision"], worker["id"], worker["epoch"], "failed")
    assert not other.claim(0, command, record["id"], record["approval"]["binding"], None)[0]
    assert other.read()["workers"][worker["id"]]["outcome"] == "failed"


CORRUPT = ["schema", "schema_bool", "revision_bool", "negative_epoch", "missing", "unknown", "nested", "id", "approval", "worker", "duplicate", "nan", "empty", "truncated"]


@pytest.mark.parametrize("case", CORRUPT)
def test_corrupt_state_is_never_reset_on_read_or_mutation(store, case):
    record = seed(store)
    worker = claim(store, record)
    state = store.read()
    if case == "schema":
        state["schema"] = 99
    elif case == "schema_bool":
        state["schema"] = True
    elif case == "revision_bool":
        state["revision"] = False
    elif case == "negative_epoch":
        state["epoch"] = -1
    elif case == "missing":
        del state["records"]
    elif case == "unknown":
        state["surprise"] = True
    elif case == "nested":
        state["records"][record["id"]]["identity"]["paths"][0]["mode"] = True
    elif case == "id":
        state["records"][record["id"]]["id"] = "../escape"
    elif case == "approval":
        state["records"][record["id"]]["decisions"] = {"homeassistant/a.yaml": "git"}
    elif case == "worker":
        state["workers"][worker["id"]]["record_id"] = uid()
    encoded = native.canonical(state)
    if case == "duplicate":
        encoded = encoded[:-1] + b',"schema":1}'
    elif case == "nan":
        encoded = encoded.replace(b'"revision":3', b'"revision":NaN')
    elif case == "empty":
        encoded = b""
    elif case == "truncated":
        encoded = encoded[:20]
    path = store.data_root / "native-operations/state.json"
    path.write_bytes(encoded)
    for call in (store.read, lambda: store.preview(0, "save", identity(), b"new")):
        with pytest.raises(native.InvalidState):
            call()
        assert path.read_bytes() == encoded


@pytest.mark.parametrize("case", ["missing", "tamper", "oversize", "symlink", "fifo", "hardlink"])
def test_artifact_authority_rejects_unsafe_or_changed_bytes(store, case):
    record = seed(store)
    path = store.data_root / "native-operations/artifacts" / (record["artifact"] + ".diff")
    if case == "missing":
        path.unlink()
    elif case == "tamper":
        path.write_bytes(b"tampered")
    elif case == "oversize":
        with path.open("wb") as stream:
            stream.truncate(native.MAX_ARTIFACT + 1)
    elif case == "symlink":
        path.unlink()
        target = store.data_root / "untouched"
        target.write_bytes(b"untouched")
        path.symlink_to(target)
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        os.link(path, store.data_root / "linked")
    before = (store.data_root / "native-operations/state.json").read_bytes()
    with pytest.raises((native.InvalidState, OSError)):
        store.read()
    with pytest.raises((native.InvalidState, OSError)):
        store.preview(0, "save", identity(), b"reviewed complete diff\n")
    assert (store.data_root / "native-operations/state.json").read_bytes() == before
    if case == "symlink":
        assert target.read_bytes() == b"untouched"


@pytest.mark.parametrize("where", ["parent", "root", "artifacts", "state.json", "lock"])
def test_no_follow_paths_and_no_outside_writes(tmp_path, where):
    data = tmp_path / "data"
    data.mkdir()
    store = native.NativeOperationStore(data.resolve())
    store.read()
    root = data / "native-operations"
    if where in {"parent", "root", "artifacts"}:
        target = tmp_path / "target"
        target.mkdir()
        if where == "parent":
            link = tmp_path / "parent"
            link.symlink_to(data, target_is_directory=True)
            store = native.NativeOperationStore(link)
        else:
            current = root if where == "root" else root / "artifacts"
            current.rename(tmp_path / "original")
            current.symlink_to(target, target_is_directory=True)
    else:
        current = root / where
        current.unlink()
        target = tmp_path / "target"
        target.write_bytes(b"untouched")
        current.symlink_to(target)
    with pytest.raises((native.InvalidState, OSError)):
        store.read()
    assert list(target.iterdir()) == [] if target.is_dir() else target.read_bytes() == b"untouched"


@pytest.mark.parametrize("stage", ["artifact_written", "artifact_fsynced", "artifact_replaced", "artifact_durable", "state_written", "state_fsynced", "state_replaced", "state_durable"])
def test_io_failure_every_persistence_boundary_reopens_intact(store, stage):
    before = store.read()
    def fault(current):
        if current == stage:
            raise OSError("injected disk failure")
    broken = native.NativeOperationStore(store.data_root, fault=fault)
    with pytest.raises(OSError):
        broken.preview(before["revision"], "save", identity(), b"durability artifact")
    state = native.NativeOperationStore(store.data_root).read()
    assert state["revision"] in {before["revision"], before["revision"] + 1}
    if state["records"]:
        record = next(iter(state["records"].values()))
        assert store.read_diff(record["id"]) == b"durability artifact"
        assert record["status"] == "preview" and record["approval"] is None
    assert not list((store.data_root / "native-operations").glob(".tmp-*"))


def run_child(source, *args, **kwargs):
    env = dict(os.environ, PYTHONPATH=str(APP))
    return subprocess.run([sys.executable, "-c", source, *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=20, **kwargs)


@pytest.mark.parametrize("phase", ["accepted", "effect_started"])
def test_killed_process_restart_classification(store, phase):
    record = seed(store)
    source = '''import json,os,sys,uuid
from native_operations import NativeOperationStore
s=NativeOperationStore(sys.argv[1]);r=s.read()['records'][sys.argv[2]]
_,w=s.claim(s.read()['revision'],str(uuid.uuid4()),r['id'],r['approval']['binding'],{'available':True,'identity':r['identity']})
if sys.argv[3]=='effect_started':s.checkpoint(s.read()['revision'],w['id'],w['epoch'],'effect_started',{'available':True,'identity':r['identity']})
os._exit(73)
'''
    result = run_child(source, store.data_root, record["id"], phase)
    assert result.returncode == 73, result.stderr
    new = native.NativeOperationStore(store.data_root)
    new.lifecycle(new.read()["revision"], "after-crash", {record["id"]: observed(record["identity"])})
    state = new.read()
    assert state["records"][record["id"]]["status"] == ("stale" if phase == "accepted" else "recovery_required")
    assert (state["active"] is None) == (phase == "accepted")


@pytest.mark.parametrize("stage", ["artifact_written", "artifact_fsynced", "artifact_replaced", "artifact_durable", "state_written", "state_fsynced", "state_replaced", "state_durable"])
def test_hard_crash_every_write_checkpoint_in_new_process(store, stage):
    store.read()
    source = '''import json,os,sys
from native_operations import NativeOperationStore
identity=json.loads(sys.argv[3])
def fault(stage):
 if stage==sys.argv[2]:os._exit(74)
s=NativeOperationStore(sys.argv[1],fault=fault);s.preview(0,'save',identity,b'crash artifact')
'''
    result = run_child(source, store.data_root, stage, json.dumps(identity()))
    assert result.returncode == 74, result.stderr
    state = native.NativeOperationStore(store.data_root).read()
    assert state["revision"] in {0, 1}
    for record in state["records"].values():
        assert store.read_diff(record["id"]) == b"crash artifact" and record["approval"] is None


def test_after_replace_ambiguous_claim_replays_once(store):
    record = seed(store)
    command = uid()
    def fault(stage):
        if stage == "state_replaced":
            raise OSError("lost acknowledgement")
    broken = native.NativeOperationStore(store.data_root, fault=fault)
    with pytest.raises(OSError):
        claim(broken, record, command)
    accepted, replay = store.claim(0, command, record["id"], record["approval"]["binding"], None)
    assert not accepted and replay["phase"] == "accepted"
    assert len(store.read()["workers"]) == 1


def test_two_process_claim_cas_and_lock_inode_stability(store):
    first = seed(store, "save")
    second = seed(store, "apply")
    state = store.read()
    lock = store.data_root / "native-operations/lock"
    inode = lock.stat().st_ino
    source = '''import json,sys,uuid
from native_operations import NativeOperationStore,Conflict
s=NativeOperationStore(sys.argv[1]);r=json.loads(sys.argv[2]);print('ready',flush=True);sys.stdin.readline()
try:
 s.claim(int(sys.argv[3]),str(uuid.uuid4()),r['id'],r['approval']['binding'],{'available':True,'identity':r['identity']});print('accepted')
except Conflict:print('blocked')
'''
    children = [subprocess.Popen([sys.executable, "-c", source, str(store.data_root), json.dumps(record), str(state["revision"])],
                                 env=dict(os.environ, PYTHONPATH=str(APP)), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for record in (first, second)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=20) for child in children]
        assert sorted(output.strip() for output, error in results) == ["accepted", "blocked"], results
        assert all(child.returncode == 0 for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    assert lock.stat().st_ino == inode
    assert len(store.read()["workers"]) == 1
    with pytest.raises(native.Conflict, match="revision"):
        store.cancel(state["revision"], second["id"])


def test_killed_lock_holder_releases_without_deleting_inode(store):
    store.read()
    path = store.data_root / "native-operations/lock"
    inode = path.stat().st_ino
    child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)", str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        child.kill()
        child.wait(timeout=10)
        assert store.read()["revision"] == 0 and path.stat().st_ino == inode
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_native_api_has_no_external_effect_and_legacy_version_repair_is_independent(store):
    import state as legacy
    record = seed(store, "migration")
    before = store.read()
    legacy_path = store.data_root / "state.json"
    legacy.write_state(legacy_path, {"last_seen_addon_version": "old", "last_preview_commit": "legacy-apply", "last_save_preview_commit": "legacy-save", "apply_preview_selected_paths": ["secret"], "save_preview_selected_paths": ["secret"]})
    with mock.patch("subprocess.run", side_effect=AssertionError("unexpected external effect")):
        store.refresh()
        repaired = legacy.repair_startup_state(legacy_path, "now", addon_version="new")
        assert not repaired["last_preview_commit"] and not repaired["last_save_preview_commit"]
        store.lifecycle(before["revision"], "new", {record["id"]: observed(record["identity"])})
    assert store.read()["records"][record["id"]] == record
    assert not any("native_operations" in (APP / name).read_text() for name in ("state.py", "app_context.py", "server.py"))
    path = store.data_root / "native-operations/state.json"
    path.write_bytes(b'{"schema":999}')
    assert legacy.active_operation_blocks(legacy.read_state(legacy_path)) is False


def test_generic_run_recreates_exact_owned_container_with_same_bind(tmp_path):
    wrapper_path = APP.parents[1] / "dev/virtualization/ha_ops_dev.py"
    spec = importlib.util.spec_from_file_location("native_wrapper_contract", wrapper_path)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    calls = []
    removes = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            return subprocess.CompletedProcess(args, 0, stdout=b"owned-id\n", stderr=b"")
        if args[:2] == ("container", "inspect"):
            create = next(row for row in reversed(calls) if row[0] == "create")
            mount_values = [create[i+1] for i, arg in enumerate(create) if arg == "--mount"]
            mounts = []
            for raw in mount_values:
                fields = dict(item.split("=", 1) for item in raw.split(",") if "=" in item)
                mounts.append({"Destination": fields["target"], "Source": fields["source"], "RW": "readonly" not in raw.split(","), "Type": "bind"})
            run_id = create[create.index("--label")+1].split("=", 1)[1]
            result = [{"Id": "owned-id", "Config": {"Labels": {"ha-ops.dev.run": run_id}}, "Mounts": mounts, "HostConfig": {"NetworkMode": "none"}}]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(result).encode(), stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout=b"0\n" if args[0] == "wait" else b"", stderr=b"")
    with mock.patch.object(wrapper, "RUNS", tmp_path / "runs"), mock.patch.object(wrapper, "snapshot_input", return_value={"total": 0}), mock.patch.object(wrapper, "capacity"), mock.patch.object(wrapper, "copy_input", side_effect=lambda source, target, **kw: target.mkdir()), mock.patch.object(wrapper, "setup", return_value={"platform": "linux/arm64", "image": "existing"}), mock.patch.object(wrapper, "docker", side_effect=docker), mock.patch.object(wrapper, "remove_owned", side_effect=lambda *args: removes.append(args)):
        assert wrapper.generic_run(Path("ha-ops"), ["python3", "probe.py"], recreate=True) == 0
    creates = [row for row in calls if row[0] == "create"]
    assert len(creates) == 2 and creates[0] == creates[1]
    assert len(removes) == 2 and removes[0] == removes[1]
    assert not list((tmp_path / "runs").iterdir())


def container_recreation_probe():
    """Run twice via wrapper --recreate; output bind persists, container does not."""
    root = Path("/workspace/output")
    store = native.NativeOperationStore(root)
    proof = root / "proof.json"
    container_id = Path("/etc/hostname").read_text().strip()
    if not proof.exists():
        records = {domain: seed(store, domain) for domain in sorted(native.DOMAINS)}
        record = records["migration"]
        worker = claim(store, record)
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
        manifest = (root / "native-operations/state.json").read_bytes()
        proof.write_text(json.dumps({"container_id": container_id, "manifest": native.digest(manifest.hex()), "worker": worker,
                                     "records": records}))
        print("Native persistence first container: effect intent durable; four domains and reviewed artifacts saved")
    else:
        previous = json.loads(proof.read_text())
        assert previous["container_id"] != container_id
        assert native.digest((root / "native-operations/state.json").read_bytes().hex()) == previous["manifest"]
        state = store.read()
        assert len(state["records"]) == 4
        for record in previous["records"].values():
            assert store.read_diff(record["id"]) == b"reviewed complete diff\n"
        store.lifecycle(state["revision"], "container-update", {record["id"]: observed(record["identity"]) for record in previous["records"].values()})
        state = store.read()
        worker = state["workers"][previous["worker"]["id"]]
        assert worker["phase"] == "recovery_required" and worker["epoch"] > previous["worker"]["epoch"]
        assert state["active_scope"] == identity("scope")["scope_transaction"]["old"]
        print("Native persistence second container: distinct container, identical manifest/artifact bytes, fenced epoch, recovery_required, old scope retained")


if __name__ == "__main__" and sys.argv[1:] == ["--container-recreation"]:
    container_recreation_probe()


@pytest.mark.parametrize("terminal", ["failed", "invalidated"])
def test_cancel_stale_after_claim_retains_terminal_replay(store, terminal):
    record = seed(store)
    command = uid()
    worker = claim(store, record, command)
    if terminal == "failed":
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "failed")
    else:
        with pytest.raises(native.Conflict):
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", None)
    store.cancel(store.read()["revision"], record["id"])
    assert store.read()["records"][record["id"]]["status"] == "cancelled"
    accepted, result = store.claim(0, command, record["id"], record["approval"]["binding"], None)
    assert not accepted and result["outcome"] == terminal


@pytest.mark.parametrize("phase", ["completed", "effect_started", "recovery_required"])
def test_corrupt_worker_graph_never_confers_completion(store, phase):
    record = seed(store)
    worker = claim(store, record)
    state = store.read()
    state["active"] = None
    state["workers"][worker["id"]]["phase"] = phase
    path = store.data_root / "native-operations/state.json"
    encoded = native.canonical(state)
    path.write_bytes(encoded)
    with pytest.raises(native.InvalidState):
        store.read()
    assert path.read_bytes() == encoded


def test_reject_terminal_shortcuts_epoch_bool_and_empty_selection(store):
    value = identity()
    record = store.preview(0, "save", value, b"diff")
    with pytest.raises(native.InvalidState, match="empty selection"):
        store.review(store.read()["revision"], record["id"], {}, uid(), observed(value))
    with pytest.raises(native.Conflict, match="fresh Preview"):
        store.review(store.read()["revision"], record["id"], {"homeassistant/a.yaml": "ha"}, uid(), None)
    assert store.read()["records"][record["id"]]["status"] == "stale"
    record = seed(store)
    worker = claim(store, record)
    with pytest.raises(native.Conflict, match="transition"):
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(value["effect"], marker=worker["marker"]))
    with pytest.raises(native.InvalidState, match="integer"):
        store.checkpoint(store.read()["revision"], worker["id"], True, "failed")
    view = store.refresh()
    assert set(view["worker"]) == {"id", "phase", "epoch", "outcome"}


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("error", [OSError(28, "no space"), PermissionError(13, "denied")], ids=["ENOSPC", "permission"])
def test_system_io_failures_preserve_previous_manifest(store, operation, error):
    before = store.read()
    with mock.patch.object(native.os, operation, side_effect=error):
        with pytest.raises(OSError):
            store.preview(before["revision"], "save", identity(), b"test io error")
    assert store.read() == before


@pytest.mark.parametrize("name", ["refs/heads/a.lock", "refs/heads/.hidden", "refs/heads/ends.", "refs/heads/a//b", "refs/heads/a/", "refs/heads/a@{b", "refs/heads/a\n", "refs/heads/a\x7f"])
def test_ref_identity_rejects_git_invalid_names(store, name):
    value = identity()
    value["refs"]["remote"]["name"] = name
    with pytest.raises(native.InvalidState, match="invalid ref"):
        store.preview(0, "save", value, b"diff")
    assert store.read()["records"] == {}


@pytest.mark.parametrize("mode", [0o040700, 0o120777, 0o600])
def test_path_fingerprint_rejects_type_mode_disagreement(store, mode):
    value = identity()
    value["paths"][0]["mode"] = mode
    with pytest.raises(native.InvalidState, match="mode/type"):
        store.preview(0, "save", value, b"diff")
    assert store.read()["records"] == {}


def assert_corrupt_store_blocks_all_operations(store, state, record, worker=None):
    path = store.data_root / "native-operations/state.json"
    encoded = native.canonical(state)
    path.write_bytes(encoded)
    calls = [store.read, store.refresh, lambda: store.read_diff(record["id"]),
             lambda: store.lifecycle(state["revision"], "restart", {}),
             lambda: store.preview(state["revision"], "save", identity(), b"new preview"),
             lambda: store.review(state["revision"], record["id"], {}, uid(), observed(record["identity"])),
             lambda: store.claim(state["revision"], uid(), record["id"], "0" * 64, observed(record["identity"]))]
    if worker:
        calls.append(lambda: store.checkpoint(state["revision"], worker["id"], worker["epoch"], "completed",
                                             evidence=dict(record["identity"]["effect"], marker=worker["marker"])))
    for call in calls:
        with pytest.raises(native.InvalidState):
            call()
        assert path.read_bytes() == encoded


@pytest.mark.parametrize("case", ["accepted_downgrade", "failed_downgrade", "invalidated_downgrade", "missing_intent", "missing_acceptance", "duplicate_intent", "intent_after_terminal", "completion_without_intent", "failed_after_intent", "last_epoch_mismatch", "recovery_same_epoch", "recovery_epoch_jump", "owner_mismatch"])
def test_worker_history_corruption_blocks_every_authority_path(store, case):
    record = seed(store, "migration")
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    if case in {"intent_after_terminal", "completion_without_intent"}:
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    if case in {"recovery_same_epoch", "recovery_epoch_jump"}:
        store.lifecycle(store.read()["revision"], "restart", {})
    state = store.read()
    current = state["workers"][worker["id"]]
    stored = state["records"][record["id"]]
    history = stored["checkpoints"]
    intent = next(checkpoint for checkpoint in history if checkpoint["phase"] == "effect_started")
    if case.endswith("downgrade"):
        phase = case.split("_")[0]
        current["phase"] = phase
        if phase != "accepted":
            current["outcome"] = phase
            state["active"] = None
            stored.update(status="stale", decisions={}, approval=None)
    elif case == "missing_intent" or case == "completion_without_intent":
        stored["checkpoints"] = [checkpoint for checkpoint in history if checkpoint["phase"] != "effect_started"]
    elif case == "missing_acceptance":
        stored["checkpoints"] = [checkpoint for checkpoint in history if checkpoint["phase"] != "accepted"]
    elif case == "duplicate_intent":
        history.append(copy.deepcopy(intent))
    elif case == "intent_after_terminal":
        history.append(copy.deepcopy(intent))
    elif case == "failed_after_intent":
        history.append(dict(intent, phase="failed"))
        current.update(phase="failed", outcome="failed")
        state["active"] = None
        stored.update(status="stale", decisions={}, approval=None)
    elif case == "last_epoch_mismatch":
        current["epoch"] += 1
        state["epoch"] += 1
    elif case == "recovery_same_epoch":
        history[-1]["epoch"] = intent["epoch"]
        current["epoch"] = intent["epoch"]
    elif case == "recovery_epoch_jump":
        history[-1]["epoch"] += 2
        current["epoch"] += 2
        state["epoch"] += 2
    else:
        current["owner"] = uid()
    assert_corrupt_store_blocks_all_operations(store, state, record, worker)


@pytest.mark.parametrize("case", ["unpublished_new", "cancelled_new", "missing_baseline", "changed_baseline", "missing_publication_history", "rolled_back_active", "wrong_active_policy"])
def test_active_scope_requires_initial_baseline_and_confirmed_chain(store, case):
    record = seed(store, "scope")
    if case in {"missing_publication_history", "rolled_back_active", "wrong_active_policy"}:
        worker = claim(store, record)
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    if case == "cancelled_new":
        store.cancel(store.read()["revision"], record["id"])
    state = store.read()
    old = record["identity"]["scope_transaction"]["old"]
    new = record["identity"]["scope_transaction"]["new"]
    if case in {"unpublished_new", "cancelled_new"}:
        state["active_scope"] = copy.deepcopy(new)
    elif case == "missing_baseline":
        state["baseline_scope"] = None
    elif case == "changed_baseline":
        state["baseline_scope"] = copy.deepcopy(new)
    elif case == "missing_publication_history":
        state["commands"] = {}
        state["workers"] = {}
        state["records"][record["id"]].update(status="reviewed", checkpoints=[])
    elif case == "rolled_back_active":
        state["active_scope"] = copy.deepcopy(old)
    else:
        state["active_scope"]["allowlist"] = ["new-unpublished-path"]
    assert_corrupt_store_blocks_all_operations(store, state, record)


def publish_scope(store, old=None, takeover=False):
    value = identity("scope")
    if old is not None:
        value["scope"] = copy.deepcopy(old)
        value["scope_transaction"]["old"] = copy.deepcopy(old)
        value["scope_transaction"]["new"] = dict(old, revision=old["revision"] + 1, exclude_digest="9" * 64)
    record = store.preview(store.read()["revision"], "scope", value, b"scope reviewed diff")
    record = store.review(store.read()["revision"], record["id"], {}, uid(), observed(value))
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
    if takeover:
        store.lifecycle(store.read()["revision"], "scope-recovery", {})
        worker = store.read()["workers"][worker["id"]]
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(value["effect"], marker=worker["marker"]))
    return record, worker


def test_scope_history_two_publications_and_cancelled_third_reopens(store):
    first, _ = publish_scope(store)
    second, _ = publish_scope(store, first["identity"]["scope_transaction"]["new"])
    active = second["identity"]["scope_transaction"]["new"]
    value = identity("scope")
    value["scope"] = copy.deepcopy(active)
    value["scope_transaction"] = {"old": copy.deepcopy(active), "new": dict(active, revision=3)}
    cancelled = store.preview(store.read()["revision"], "scope", value, b"cancelled scope diff")
    store.cancel(store.read()["revision"], cancelled["id"])
    new = native.NativeOperationStore(store.data_root)
    state = new.read()
    new.lifecycle(state["revision"], "update", {})
    assert new.read()["baseline_scope"] == first["identity"]["scope_transaction"]["old"]
    assert new.read()["active_scope"] == active
    for record in (first, second):
        assert new.read()["records"][record["id"]]["status"] == "completed"


def test_repeated_recovery_takeovers_keep_intent_and_complete_legally(store):
    record = seed(store, "migration")
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "recovery_required")
    for number in range(2):
        new = native.NativeOperationStore(store.data_root)
        new.lifecycle(new.read()["revision"], "restart", {})
        store = new
    current = store.read()["workers"][worker["id"]]
    assert current["epoch"] == worker["epoch"] + 2 and current["phase"] == "recovery_required"
    store.checkpoint(store.read()["revision"], current["id"], current["epoch"], "completed", evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    history = store.read()["records"][record["id"]]["checkpoints"]
    assert [checkpoint["phase"] for checkpoint in history] == ["accepted", "effect_started", "recovery_required", "recovery_required", "recovery_required", "completed"]
    assert store.read()["active"] is None


@pytest.mark.parametrize("case", ["gap", "overlap", "clock_without_claim"])
def test_global_epoch_provenance_blocks_gaps_and_reuse(store, case):
    first = seed(store)
    first_worker = claim(store, first)
    store.checkpoint(store.read()["revision"], first_worker["id"], first_worker["epoch"], "failed")
    record = seed(store, "apply")
    worker = claim(store, record)
    state = store.read()
    if case == "clock_without_claim":
        state["epoch"] += 1
    else:
        epoch = 3 if case == "gap" else 1
        state["epoch"] = epoch
        state["workers"][worker["id"]]["epoch"] = epoch
        state["records"][record["id"]]["checkpoints"][-1]["epoch"] = epoch
    assert_corrupt_store_blocks_all_operations(store, state, record, worker)


@pytest.mark.parametrize("case", ["duplicate_publication", "missing_initial_publication", "forked_policy"])
def test_scope_publication_chain_rejects_forks_and_missing_history(store, case):
    first, first_worker = publish_scope(store)
    second, second_worker = publish_scope(store, first["identity"]["scope_transaction"]["new"])
    state = store.read()
    if case == "missing_initial_publication":
        del state["records"][first["id"]]
        del state["workers"][first_worker["id"]]
        state["commands"] = {key: command for key, command in state["commands"].items() if command["operation_id"] != first_worker["id"]}
        state["epoch"] = 1
        state["workers"][second_worker["id"]]["epoch"] = 1
        for checkpoint in state["records"][second["id"]]["checkpoints"]:
            checkpoint["epoch"] = 1
    else:
        target = state["records"][second["id"] if case == "duplicate_publication" else first["id"]]
        if case == "duplicate_publication":
            baseline = state["baseline_scope"]
            target["identity"]["scope"] = copy.deepcopy(baseline)
            target["identity"]["scope_transaction"]["old"] = copy.deepcopy(baseline)
            target["identity"]["scope_transaction"]["new"]["revision"] = 1
        else:
            target["identity"]["scope_transaction"]["new"]["allowlist"] = ["forked-policy"]
        target["identity_digest"] = native.digest(target["identity"])
        binding = native._binding(target, target["decisions"])
        target["approval"]["binding"] = binding
        affected = second_worker if case == "duplicate_publication" else first_worker
        state["workers"][affected["id"]]["binding"] = binding
        for command in state["commands"].values():
            if command["operation_id"] == affected["id"]:
                command["payload_digest"] = native.digest({"record_id": target["id"], "binding": binding})
    assert_corrupt_store_blocks_all_operations(store, state, second)


def publish_save(store, scope):
    value = identity("save")
    value["scope"] = copy.deepcopy(scope)
    record = store.preview(store.read()["revision"], "save", value, b"chronological Save diff")
    record = store.review(store.read()["revision"], record["id"], {"homeassistant/a.yaml": "ha"}, uid(), observed(value))
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(value["effect"], marker=worker["marker"]))
    return record, worker


@pytest.mark.parametrize("case", ["reversed_publications", "reversed_takeover_ranges", "save_before_its_scope", "save_after_scope_expired"])
def test_worker_chronology_binds_publications_and_sync_scope(store, case):
    if case.startswith("reversed"):
        first, first_worker = publish_scope(store, takeover=case == "reversed_takeover_ranges")
        second, second_worker = publish_scope(store, first["identity"]["scope_transaction"]["new"], takeover=case == "reversed_takeover_ranges")
    elif case == "save_before_its_scope":
        first, first_worker = publish_scope(store)
        second, second_worker = publish_save(store, first["identity"]["scope_transaction"]["new"])
    else:
        first, first_worker = publish_save(store, identity("scope")["scope_transaction"]["old"])
        second, second_worker = publish_scope(store)
    state = store.read()
    width = 2 if case == "reversed_takeover_ranges" else 1
    for record, worker, delta in ((first, first_worker, width), (second, second_worker, -width)):
        state["workers"][worker["id"]]["epoch"] += delta
        for checkpoint in state["records"][record["id"]]["checkpoints"]:
            checkpoint["epoch"] += delta
    assert_corrupt_store_blocks_all_operations(store, state, second)


def test_chronological_scope_with_prior_save_and_intervening_recovery_remains_valid(store):
    baseline = identity("scope")["scope_transaction"]["old"]
    prior, _ = publish_save(store, baseline)
    first, _ = publish_scope(store, takeover=True)
    between, _ = publish_save(store, first["identity"]["scope_transaction"]["new"])
    second, _ = publish_scope(store, first["identity"]["scope_transaction"]["new"], takeover=True)
    after, _ = publish_save(store, second["identity"]["scope_transaction"]["new"])
    new = native.NativeOperationStore(store.data_root)
    state = new.read()
    assert state["epoch"] == 7 and state["active_scope"]["revision"] == 2
    new.lifecycle(state["revision"], "version-update", {})
    assert new.read()["active_scope"] == second["identity"]["scope_transaction"]["new"]
    for record in (prior, first, between, second, after):
        assert new.read()["records"][record["id"]]["status"] == "completed"


def candidate_at_scope(domain, policy):
    value = identity(domain)
    value["scope"] = copy.deepcopy(policy)
    if domain == "scope":
        value["scope_transaction"] = {"old": copy.deepcopy(policy), "new": dict(policy, revision=policy["revision"] + 1)}
    path_root = "addons" if "addons" in policy["allowlist"] else policy["allowlist"][0]
    value["paths"][0]["path"] = path_root + "/a.yaml"
    value["paths"][1]["path"] = path_root + "/absent"
    return value


def review_candidate(store, value):
    record = store.preview(store.read()["revision"], value["direction"], value, b"bootstrap policy diff")
    choices = {value["paths"][0]["path"]: "ha"} if value["direction"] in {"save", "apply"} else {}
    return store.review(store.read()["revision"], record["id"], choices, uid(), observed(value))


@pytest.mark.parametrize("initial", ["save", "apply", "migration", "scope"])
@pytest.mark.parametrize("next_domain", ["save", "apply", "migration", "scope"])
def test_bootstrap_all_domains_cannot_silently_replace_policy(store, initial, next_domain):
    record = seed(store, initial)
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "failed")
    policy = dict(record["identity"]["scope"], revision=1, allowlist=["addons", "homeassistant"])
    value = candidate_at_scope(next_domain, policy)
    with pytest.raises((native.Conflict, native.InvalidState)):
        claimed = review_candidate(store, value)
        claim(store, claimed)
    assert len(store.read()["workers"]) == 1


@pytest.mark.parametrize("stage", ["preview", "reviewed", "cancelled", "failed", "restart", "completed"])
@pytest.mark.parametrize("change", ["revision", "allowlist", "exclude_digest"])
def test_initial_policy_survives_every_prepublication_lifecycle(store, stage, change):
    value = identity()
    record = store.preview(0, "save", value, b"initial preview")
    if stage not in {"preview", "cancelled"}:
        record = store.review(store.read()["revision"], record["id"], {"homeassistant/a.yaml": "ha"}, uid(), observed(value))
    if stage == "cancelled":
        store.cancel(store.read()["revision"], record["id"])
    if stage in {"failed", "restart", "completed"}:
        worker = claim(store, record)
        if stage == "failed":
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "failed")
        elif stage == "restart":
            new = native.NativeOperationStore(store.data_root)
            new.lifecycle(new.read()["revision"], "restart", {})
            store = new
        else:
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(value["effect"], marker=worker["marker"]))
    new = native.NativeOperationStore(store.data_root)
    new.lifecycle(new.read()["revision"], "version-update", {})
    proposed = copy.deepcopy(value["scope"])
    if change == "revision":
        proposed["revision"] += 1
    elif change == "allowlist":
        proposed["allowlist"] = ["addons", "homeassistant"]
    else:
        proposed["exclude_digest"] = "0" * 64
    with pytest.raises((native.Conflict, native.InvalidState)):
        changed = review_candidate(new, candidate_at_scope("save", proposed))
        claim(new, changed)


@pytest.mark.parametrize("domain", ["save", "apply"])
@pytest.mark.parametrize("path", ["addons/a.yaml", "homeassistant-other/a.yaml", "homeassistant2/a.yaml"])
def test_submitted_selection_cannot_escape_declared_scope(store, domain, path):
    value = identity(domain)
    value["paths"] = [dict(value["paths"][0], path=path)]
    record = store.preview(0, domain, value, b"unmanaged diagnostic path")
    with pytest.raises((native.Conflict, native.InvalidState)):
        store.review(store.read()["revision"], record["id"], {path: "ha"}, uid(), observed(value))
    assert not store.read()["workers"]


@pytest.mark.parametrize("domain", sorted(native.DOMAINS))
def test_empty_reopens_then_first_preview_atomically_anchors_each_domain(store, domain):
    for version in ("restart", "version-update"):
        store = native.NativeOperationStore(store.data_root)
        empty = store.read()
        assert all(empty[key] is None for key in ("baseline_record_id", "baseline_scope", "active_scope"))
        store.lifecycle(empty["revision"], version, {})
        assert not store.read()["records"]
    value = candidate_at_scope(domain, dict(identity()["scope"], revision=7))
    with mock.patch("os.scandir", side_effect=AssertionError("unexpected inventory scan")), \
            mock.patch("subprocess.run", side_effect=AssertionError("unexpected external query")):
        record = store.preview(store.read()["revision"], domain, value, b"bootstrap evidence")
    reopened = native.NativeOperationStore(store.data_root)
    before = reopened.read()
    assert before["baseline_record_id"] == record["id"]
    assert before["baseline_scope"] == before["active_scope"] == value["scope"]
    reopened.cancel(before["revision"], record["id"])
    replacement = reopened.preview(reopened.read()["revision"], domain, value, b"replacement evidence")
    assert replacement["id"] != record["id"]
    assert reopened.read()["baseline_record_id"] == record["id"]
    assert reopened.read()["records"][record["id"]]["status"] == "cancelled"


@pytest.mark.parametrize("case", ["null_policy", "missing_source", "unknown_source", "changed_policy", "later_policy_source", "unpublished_record"])
def test_bootstrap_provenance_corruption_blocks_every_authority_path(store, case):
    record = seed(store)
    if case == "later_policy_source":
        publication, _ = publish_scope(store)
        record = review_candidate(store, candidate_at_scope("apply", publication["identity"]["scope_transaction"]["new"]))
    state = store.read()
    if case == "null_policy":
        for key in ("baseline_record_id", "baseline_scope", "active_scope"):
            state[key] = None
    elif case == "missing_source":
        state["baseline_record_id"] = None
    elif case == "unknown_source":
        state["baseline_record_id"] = uid()
    elif case == "changed_policy":
        state["baseline_scope"]["exclude_digest"] = "0" * 64
        state["active_scope"] = copy.deepcopy(state["baseline_scope"])
    elif case == "later_policy_source":
        state["baseline_record_id"] = record["id"]
    else:
        extra = store.preview(state["revision"], "apply", identity("apply"), b"diagnostic")
        state = store.read()
        target = state["records"][extra["id"]]
        target["identity"]["scope"]["revision"] += 1
        target["identity_digest"] = native.digest(target["identity"])
    assert_corrupt_store_blocks_all_operations(store, state, record)


@pytest.mark.parametrize("initial", sorted(native.DOMAINS))
@pytest.mark.parametrize("next_domain", ["save", "apply"])
def test_completed_publication_after_each_bootstrap_allows_new_folder(store, initial, next_domain):
    first = seed(store, initial)
    worker = claim(store, first)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "failed")
    publication, _ = publish_scope(store)
    value = candidate_at_scope(next_domain, publication["identity"]["scope_transaction"]["new"])
    assert value["paths"][0]["path"] == "addons/a.yaml"
    with mock.patch("os.scandir", side_effect=AssertionError("unexpected inventory scan")), \
            mock.patch("subprocess.run", side_effect=AssertionError("unexpected external query")):
        approved = review_candidate(store, value)
        accepted = claim(store, approved)
    assert accepted["phase"] == "accepted"
    assert store.read()["baseline_record_id"] == first["id"]


@pytest.mark.parametrize("domain", ["save", "apply"])
@pytest.mark.parametrize("path", ["homeassistant", "homeassistant/nested/a.yaml"])
def test_selection_allowlist_matches_complete_path_components(store, domain, path):
    value = identity(domain)
    value["paths"] = [dict(value["paths"][0], path=path)]
    approved = review_candidate(store, value)
    assert claim(store, approved)["phase"] == "accepted"


def test_competing_process_bootstraps_anchor_one_policy(store):
    store.read()
    source = '''import json,sys
from native_operations import NativeOperationStore,Conflict
s=NativeOperationStore(sys.argv[1]);value=json.loads(sys.argv[2]);print('ready',flush=True);sys.stdin.readline()
try:
 r=s.preview(0,value['direction'],value,b'competing bootstrap');print(r['id'])
except Conflict:print('blocked')
'''
    values = [identity("save"), candidate_at_scope("apply", dict(identity()["scope"], allowlist=["addons", "homeassistant"]))]
    children = [subprocess.Popen([sys.executable, "-c", source, str(store.data_root), json.dumps(value)],
                                 env=dict(os.environ, PYTHONPATH=str(APP)), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for value in values]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=20) for child in children]
        assert all(child.returncode == 0 and not error for child, (_, error) in zip(children, results)), results
        outputs = [output.strip() for output, _ in results]
        assert outputs.count("blocked") == 1
        state = native.NativeOperationStore(store.data_root).read()
        assert len(state["records"]) == 1
        assert state["baseline_record_id"] == next(output for output in outputs if output != "blocked")
        assert state["baseline_scope"] == state["active_scope"] == state["records"][state["baseline_record_id"]]["identity"]["scope"]
        loser = values[outputs.index("blocked")]
        with pytest.raises(native.Conflict, match="completed publication"):
            store.preview(state["revision"], loser["direction"], loser, b"loser retry")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def advance_selection_record(store, domain, phase):
    record = seed(store, domain)
    worker = None
    if phase != "reviewed":
        worker = claim(store, record)
        if phase != "accepted":
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
        if phase == "completed":
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed",
                             evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
        elif phase == "recovery_required":
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "recovery_required")
    return record, worker


@pytest.mark.parametrize("domain", ["save", "apply"])
@pytest.mark.parametrize("phase", ["reviewed", "accepted", "effect_started", "completed", "recovery_required"])
def test_persisted_ordinary_approval_requires_nonempty_selection(store, domain, phase):
    record, worker = advance_selection_record(store, domain, phase)
    state = store.read()
    target = state["records"][record["id"]]
    target["decisions"] = {}
    binding = native._binding(target, {})
    target["approval"]["binding"] = binding
    if worker:
        state["workers"][worker["id"]]["binding"] = binding
        for command in state["commands"].values():
            if command["operation_id"] == worker["id"]:
                command["payload_digest"] = native.digest({"record_id": record["id"], "binding": binding})
    assert_corrupt_store_blocks_all_operations(store, state, record, worker)


@pytest.mark.parametrize("domain", ["save", "apply"])
@pytest.mark.parametrize("status", ["preview", "stale", "cancelled"])
def test_unapproved_ordinary_empty_selection_remains_diagnostic(store, domain, status):
    value = identity(domain)
    record = store.preview(0, domain, value, b"no submitted selection")
    if status == "stale":
        store.lifecycle(store.read()["revision"], "restart", {})
    elif status == "cancelled":
        store.cancel(store.read()["revision"], record["id"])
    new = native.NativeOperationStore(store.data_root)
    retained = new.read()["records"][record["id"]]
    assert retained["status"] == status and retained["decisions"] == {} and retained["approval"] is None
    assert new.refresh()["draft"] == {}
    assert new.read_diff(record["id"]) == b"no submitted selection"


@pytest.mark.parametrize("domain", ["migration", "scope"])
@pytest.mark.parametrize("phase", ["reviewed", "accepted", "effect_started", "completed", "recovery_required"])
def test_policy_operation_empty_selection_retains_legitimate_approval(store, domain, phase):
    record, _ = advance_selection_record(store, domain, phase)
    new = native.NativeOperationStore(store.data_root)
    retained = new.read()["records"][record["id"]]
    assert retained["decisions"] == {} and retained["approval"] == record["approval"]
    assert retained["status"] == ("consumed" if phase in {"accepted", "effect_started"} else phase)
    assert new.read_diff(record["id"]) == b"reviewed complete diff\n"


@pytest.mark.parametrize("domain", ["save", "apply"])
def test_empty_selection_review_refused_without_creating_authority(store, domain):
    value = identity(domain)
    record = store.preview(0, domain, value, b"empty review diff")
    before = store.read()
    with pytest.raises(native.InvalidState, match="empty selection"):
        store.review(before["revision"], record["id"], {}, uid(), observed(value))
    assert store.read() == before
