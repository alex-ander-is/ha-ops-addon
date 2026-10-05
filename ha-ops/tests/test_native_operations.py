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


class NamespacedStore(native.NativeOperationStore):
    """Existing contract scenarios send the new explicit request envelope."""
    def claim(self, expected_revision, command_id, record_id, binding, observation, **kwargs):
        record = self.read()["records"].get(record_id)
        if record is not None and "instance_id" in record and not kwargs:
            kwargs = {"instance_id": record["instance_id"], "generation": record["generation"]}
        return super().claim(expected_revision, command_id, record_id, binding, observation, **kwargs)


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
    return NamespacedStore(tmp_path.resolve())


def test_empty_store_strict_roundtrip_and_private_storage(store):
    state = store.read()
    assert state["schema"] == 2 and state["revision"] == 0 and not state["records"]
    assert NamespacedStore(store.data_root).read() == state
    root = store.data_root / "native-operations"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "state.json").stat().st_mode & 0o777 == 0o600
    assert (root / "lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("case", ["epoch", "generation", "epoch_and_generation", "frontier", "epoch_and_frontier", "publications"])
def test_empty_store_rejects_uncovered_high_water_marks_before_any_operation(store, case):
    state = store.read()
    if case in {"epoch", "epoch_and_generation", "epoch_and_frontier"}:
        state["epoch"] = 1
    if case in {"generation", "epoch_and_generation"}:
        state["generation"] = 1
    if case in {"frontier", "epoch_and_frontier", "publications"}:
        state["checkpoint"] = store._checkpoint(
            through_epoch=1 if case != "publications" else 0,
            publications=1 if case == "publications" else 0)
    manifest = store.data_root / "native-operations/state.json"
    original = native.canonical(state)
    manifest.write_bytes(original)
    record_id, operation_id = uid(), uid()
    producer, destination = mock.Mock(), mock.Mock()
    calls = [store.read, store.refresh, store.accounting,
             lambda: store.compact(state["revision"]),
             lambda: store.lifecycle(state["revision"], "updated", {}),
             lambda: store.recovery_pin(state["revision"], None),
             lambda: store.upgrade(state["revision"]),
             lambda: store.preview(state["revision"], "save", identity(), b"diff"),
             lambda: store.preview_stream(state["revision"], "save", identity(), producer),
             lambda: store.read_diff(record_id),
             lambda: store.copy_diff(record_id, destination),
             lambda: store.review(state["revision"], record_id, {}, uid(), observed(identity())),
             lambda: store.claim(state["revision"], uid(), record_id, "0" * 64, observed(identity())),
             lambda: store.checkpoint(state["revision"], operation_id, 1, "failed"),
             lambda: store.cancel(state["revision"], record_id)]
    for call in calls:
        with pytest.raises(native.InvalidState):
            call()
        assert manifest.read_bytes() == original
    producer.assert_not_called()
    destination.write.assert_not_called()


@pytest.mark.parametrize("domain", sorted(native.DOMAINS))
def test_empty_store_metadata_commits_preserve_zero_history_then_allow_first_preview(store, domain):
    initial = store.read()
    store.compact(initial["revision"])
    store.lifecycle(store.read()["revision"], "updated", {})
    store.recovery_pin(store.read()["revision"], None)
    state = NamespacedStore(store.data_root).read()
    assert state["revision"] == 3 and state["app_version"] == "updated"
    assert state["epoch"] == state["generation"] == 0
    assert state["baseline_provenance"] is None and state["checkpoint"] == store._checkpoint()
    assert store.upgrade(state["revision"]) == store._projection(state)
    candidate = store.preview(state["revision"], domain, identity(domain), b"first diff")
    reopened = NamespacedStore(store.data_root).read()
    assert candidate["generation"] == reopened["generation"] == 1 and reopened["epoch"] == 0
    assert reopened["baseline_record_id"] == candidate["id"]


@pytest.mark.parametrize("domain", sorted(native.DOMAINS))
def test_refresh_update_preserve_artifact_without_page_draft(store, domain):
    record = seed(store, domain)
    before = store.read()
    view = store.refresh()
    assert view["draft"] == {} and "decisions" not in view["candidates"][domain]
    assert "approval" not in view["candidates"][domain]
    assert store.read_diff(record["id"]) == b"reviewed complete diff\n"
    other = NamespacedStore(store.data_root)
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
    observer = NamespacedStore(store.data_root)
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
    other = NamespacedStore(store.data_root)
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
    other = NamespacedStore(store.data_root)
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
    other = NamespacedStore(store.data_root)
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
            stream.truncate(record["artifact_size"] + 1)
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
    store = NamespacedStore(data.resolve())
    store.read()
    root = data / "native-operations"
    if where in {"parent", "root", "artifacts"}:
        target = tmp_path / "target"
        target.mkdir()
        if where == "parent":
            link = tmp_path / "parent"
            link.symlink_to(data, target_is_directory=True)
            store = NamespacedStore(link)
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
    broken = NamespacedStore(store.data_root, fault=fault)
    with pytest.raises(OSError):
        broken.preview(before["revision"], "save", identity(), b"durability artifact")
    state = NamespacedStore(store.data_root).read()
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
_,w=s.claim(s.read()['revision'],str(uuid.uuid4()),r['id'],r['approval']['binding'],{'available':True,'identity':r['identity']},instance_id=r['instance_id'],generation=r['generation'])
if sys.argv[3]=='effect_started':s.checkpoint(s.read()['revision'],w['id'],w['epoch'],'effect_started',{'available':True,'identity':r['identity']})
os._exit(73)
'''
    result = run_child(source, store.data_root, record["id"], phase)
    assert result.returncode == 73, result.stderr
    new = NamespacedStore(store.data_root)
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
    state = NamespacedStore(store.data_root).read()
    assert state["revision"] in {0, 1}
    for record in state["records"].values():
        assert store.read_diff(record["id"]) == b"crash artifact" and record["approval"] is None


def test_after_replace_ambiguous_claim_replays_once(store):
    record = seed(store)
    command = uid()
    def fault(stage):
        if stage == "state_replaced":
            raise OSError("lost acknowledgement")
    broken = NamespacedStore(store.data_root, fault=fault)
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
 s.claim(int(sys.argv[3]),str(uuid.uuid4()),r['id'],r['approval']['binding'],{'available':True,'identity':r['identity']},instance_id=r['instance_id'],generation=r['generation']);print('accepted')
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
    store = NamespacedStore(root, retention=2)
    proof = root / "proof.json"
    container_id = Path("/etc/hostname").read_text().strip()
    if not proof.exists():
        migration, _ = complete_candidate(store, identity("migration"))
        recovery, _ = complete_candidate(store, identity("save"), b"independent persisted recovery")
        slot = store.recovery_pin(store.read()["revision"], None, recovery["id"], recovery["identity_digest"])
        for number in range(40):
            complete_candidate(store, identity("save"), str(number).encode())
        records = {"migration": migration, "scope": seed(store, "scope"), "save": seed(store, "save")}
        value = identity("apply")
        record = store.preview_stream(store.read()["revision"], "apply", value, ShortNonseekable(17 * 1024 * 1024 + 19))
        record = store.review(store.read()["revision"], record["id"], {value["paths"][0]["path"]: "ha"}, uid(), observed(value))
        records["apply"] = record
        worker = claim(store, record)
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
        state = store.read()
        manifest = (root / "native-operations/state.json").read_bytes()
        proof.write_text(json.dumps({"container_id": container_id, "manifest": native.digest(manifest.hex()), "worker": worker,
                                     "records": records, "record_count": len(state["records"]), "slot": slot,
                                     "generation": state["generation"], "frontier": state["checkpoint"]["through_epoch"]}))
        print("Native first container: bounded compacted state, archived migration/recovery and 17 MiB exact diff with effect intent")
    else:
        previous = json.loads(proof.read_text())
        assert previous["container_id"] != container_id
        assert native.digest((root / "native-operations/state.json").read_bytes().hex()) == previous["manifest"]
        state = store.read()
        assert len(state["records"]) == previous["record_count"] <= 8
        assert state["generation"] == previous["generation"]
        assert state["checkpoint"]["through_epoch"] == previous["frontier"] > 30
        assert state["recovery"] == previous["slot"]
        for record in state["records"].values():
            consumer = DigestConsumer()
            assert store.copy_diff(record["id"], consumer) == record["artifact_size"]
            assert consumer.hasher.hexdigest() == record["artifact"]
        store.lifecycle(state["revision"], "container-update", {record["id"]: observed(record["identity"]) for record in previous["records"].values()})
        state = store.read()
        worker = state["workers"][previous["worker"]["id"]]
        assert worker["phase"] == "recovery_required" and worker["epoch"] > previous["worker"]["epoch"]
        assert state["active_scope"] == identity("scope")["scope_transaction"]["old"]
        assert state["recovery"] == previous["slot"]
        print("Native second container: distinct identity, exact graph/artifacts/generation/frontier, independent recovery pin and fenced uncertain 17 MiB operation")


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
    new = NamespacedStore(store.data_root)
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
        new = NamespacedStore(store.data_root)
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
                command["payload_digest"] = native.NativeOperationStore._payload(state, target, binding, command["request_version"])
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
    new = NamespacedStore(store.data_root)
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
            new = NamespacedStore(store.data_root)
            new.lifecycle(new.read()["revision"], "restart", {})
            store = new
        else:
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
            store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed", evidence=dict(value["effect"], marker=worker["marker"]))
    new = NamespacedStore(store.data_root)
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
        store = NamespacedStore(store.data_root)
        empty = store.read()
        assert all(empty[key] is None for key in ("baseline_record_id", "baseline_scope", "active_scope"))
        store.lifecycle(empty["revision"], version, {})
        assert not store.read()["records"]
    value = candidate_at_scope(domain, dict(identity()["scope"], revision=7))
    with mock.patch("os.scandir", side_effect=AssertionError("unexpected inventory scan")), \
            mock.patch("subprocess.run", side_effect=AssertionError("unexpected external query")):
        record = store.preview(store.read()["revision"], domain, value, b"bootstrap evidence")
    reopened = NamespacedStore(store.data_root)
    before = reopened.read()
    assert before["baseline_record_id"] == record["id"]
    assert before["baseline_scope"] == before["active_scope"] == value["scope"]
    reopened.cancel(before["revision"], record["id"])
    replacement = reopened.preview(reopened.read()["revision"], domain, value, b"replacement evidence")
    assert replacement["id"] != record["id"]
    assert reopened.read()["baseline_record_id"] == record["id"]
    assert record["id"] not in reopened.read()["records"]
    assert reopened.read()["baseline_provenance"]["id"] == record["id"]


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
        state = NamespacedStore(store.data_root).read()
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
    new = NamespacedStore(store.data_root)
    retained = new.read()["records"][record["id"]]
    assert retained["status"] == status and retained["decisions"] == {} and retained["approval"] is None
    assert new.refresh()["draft"] == {}
    assert new.read_diff(record["id"]) == b"no submitted selection"


@pytest.mark.parametrize("domain", ["migration", "scope"])
@pytest.mark.parametrize("phase", ["reviewed", "accepted", "effect_started", "completed", "recovery_required"])
def test_policy_operation_empty_selection_retains_legitimate_approval(store, domain, phase):
    record, _ = advance_selection_record(store, domain, phase)
    new = NamespacedStore(store.data_root)
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


def complete_candidate(store, value, content=b"retention diff"):
    record = store.preview(store.read()["revision"], value["direction"], value, content)
    choices = {value["paths"][0]["path"]: "ha"} if value["direction"] in {"save", "apply"} else {}
    record = store.review(store.read()["revision"], record["id"], choices, uid(), observed(value))
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed",
                     evidence=dict(value["effect"], marker=worker["marker"]))
    return store.read()["records"][record["id"]], worker


def test_thousands_of_previews_plateau_and_hash_only_retained_graph(tmp_path):
    store = NamespacedStore(tmp_path.resolve(), retention=2)
    counts = []
    for number in range(1001):
        domain = sorted(native.DOMAINS)[number % 4]
        store.preview(store.read()["revision"], domain, identity(domain), str(number % 8).encode())
        if number > 8:
            state = store.read()
            assert len(state["records"]) == 4 and not state["workers"] and not state["commands"]
            assert len(list((tmp_path / "native-operations/artifacts").iterdir())) <= 4
            assert len(state["anchors"]) == 1
            counts.append(len(native.canonical(state)))
    assert max(counts) - min(counts) < 100
    with mock.patch.object(store, "_verify", wraps=store._verify) as verify:
        store.read()
        assert verify.call_count == 4
    assert store.read()["generation"] == 1001


def test_ancient_migration_recovery_and_distinct_active_have_advancing_frontier(tmp_path):
    store = NamespacedStore(tmp_path.resolve(), retention=2)
    migration, _ = complete_candidate(store, identity("migration"))
    recovery, _ = complete_candidate(store, identity("save"), b"independent recovery bytes")
    slot = store.recovery_pin(store.read()["revision"], None, recovery["id"], recovery["identity_digest"])
    for number in range(70):
        policy = store.read()["active_scope"]
        complete_candidate(store, candidate_at_scope("scope", policy), str(number).encode())
        policy = store.read()["active_scope"]
        complete_candidate(store, candidate_at_scope("save", policy), str(number).encode())
        state = store.read()
        assert len(state["records"]) <= 8 and len(state["workers"]) <= 8 and len(state["commands"]) <= 8
        assert len(state["anchors"]) <= 10
    current = review_candidate(store, candidate_at_scope("apply", store.read()["active_scope"]))
    worker = claim(store, current)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(current["identity"]))
    store.compact(store.read()["revision"])
    state = store.read()
    assert state["checkpoint"]["through_epoch"] > 100
    assert migration["id"] in state["records"] and recovery["id"] in state["records"]
    assert current["id"] in state["records"] and state["recovery"] == slot
    assert state["checkpoint"]["migration"]["record"]["id"] == migration["id"]
    assert state["baseline_provenance"]["id"] == migration["id"]
    assert store.read_diff(recovery["id"]) == b"independent recovery bytes"
    for number in range(1001):
        other = NamespacedStore(tmp_path.resolve())
        other.lifecycle(other.read()["revision"], "updated", {})
        store = other
    state = store.read()
    history = state["records"][current["id"]]
    assert len(history["checkpoints"]) == 4 and history["recovery_range"]["count"] == 1001
    assert state["epoch"] == worker["epoch"] + 1001 and state["active"] == worker["id"]
    assert state["recovery"] == slot and state["records"][recovery["id"]] == recovery
    assert store.refresh()["draft"] == {}
    with pytest.raises(native.Conflict, match="slot"):
        store.recovery_pin(state["revision"], None)
    with pytest.raises(native.Conflict, match="identity"):
        store.recovery_pin(state["revision"], slot, migration["id"], "0" * 64)
    store.recovery_pin(state["revision"], slot)
    store.compact(store.read()["revision"])
    assert recovery["id"] not in store.read()["records"]


def raw_claim(store, record, command, **kwargs):
    return native.NativeOperationStore.claim(store, store.read()["revision"], command, record["id"],
                                             record["approval"]["binding"], observed(record["identity"]), **kwargs)


def test_generation_replay_retirement_and_explicit_uuid_reuse(tmp_path):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    record = seed(store)
    command = uid()
    for namespace in ({}, {"generation": record["generation"]},
                      {"instance_id": uid(), "generation": record["generation"]},
                      {"instance_id": record["instance_id"], "generation": record["generation"] + 1}):
        before = store.read()
        with pytest.raises(native.Conflict):
            raw_claim(store, record, command, **namespace)
        assert store.read() == before
    worker = claim(store, record, command)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "failed")
    replacement = seed(store)
    before = store.read()
    with pytest.raises(native.Conflict, match="generation"):
        raw_claim(store, record, command, instance_id=record["instance_id"], generation=record["generation"])
    assert store.read() == before
    accepted, second = raw_claim(store, replacement, command, instance_id=replacement["instance_id"], generation=replacement["generation"])
    assert accepted and second["id"] != worker["id"]
    before = store.read()
    other = native.NativeOperationStore(tmp_path.resolve())
    accepted, replay = other.claim(0, command, replacement["id"], replacement["approval"]["binding"], None,
                                   instance_id=replacement["instance_id"], generation=replacement["generation"])
    assert not accepted and replay == second and other.read() == before
    with pytest.raises(native.Conflict):
        other.claim(0, command, replacement["id"], replacement["approval"]["binding"], None)


class ShortNonseekable:
    def __init__(self, size):
        self.remaining = size
        self.requests = []
        self.hasher = native.hashlib.sha256()

    def read(self, size):
        assert 0 < size <= native.CHUNK_SIZE
        self.requests.append(size)
        count = min(size // 3, self.remaining)
        chunk = b"x" * count if count < self.remaining else b"z" * count
        self.hasher.update(chunk)
        self.remaining -= count
        return chunk


class DigestConsumer:
    def __init__(self):
        self.hasher, self.size, self.largest = native.hashlib.sha256(), 0, 0

    def write(self, value):
        self.largest = max(self.largest, len(value))
        # Exercise partial consumer writes as well as partial producer reads.
        accepted = min(len(value), 17777)
        self.hasher.update(value[:accepted])
        self.size += accepted
        return accepted


def test_streamed_17mib_exact_content_review_claim_effect_and_reopen(store):
    producer = ShortNonseekable(17 * 1024 * 1024 + 19)
    value = identity("apply")
    record = store.preview_stream(store.read()["revision"], "apply", value, producer)
    assert record["artifact"] == producer.hasher.hexdigest()
    assert record["artifact_size"] == 17 * 1024 * 1024 + 19
    record = store.review(store.read()["revision"], record["id"], {value["paths"][0]["path"]: "git"}, uid(), observed(value))
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(value))
    other = NamespacedStore(store.data_root)
    consumer = DigestConsumer()
    assert other.copy_diff(record["id"], consumer) == record["artifact_size"]
    assert consumer.hasher.hexdigest() == record["artifact"] and consumer.largest <= native.CHUNK_SIZE
    assert native.hashlib.sha256(other.read_diff(record["id"])).hexdigest() == record["artifact"]
    other.lifecycle(other.read()["revision"], "large-diff-update", {})
    assert other.read()["workers"][worker["id"]]["phase"] == "recovery_required"


@pytest.mark.parametrize("tamper", ["append", "truncate", "same_size"])
@pytest.mark.parametrize("boundary", ["read", "review", "claim", "effect", "copy"])
def test_large_diff_late_tampering_blocks_authority(store, tamper, boundary):
    value = identity()
    content = b"q" * (9 * 1024 * 1024 + 13)
    record = store.preview(0, "save", value, content)
    if boundary in {"claim", "effect"}:
        record = store.review(store.read()["revision"], record["id"], {value["paths"][0]["path"]: "ha"}, uid(), observed(value))
    if boundary == "effect":
        worker = claim(store, record)
    before = store.read()
    path = store.data_root / "native-operations/artifacts" / (record["artifact"] + ".diff")
    with path.open("r+b") as stream:
        if tamper == "append":
            stream.seek(0, 2)
            stream.write(b"late")
        elif tamper == "truncate":
            stream.truncate(len(content) - 1)
        else:
            stream.seek(len(content) - 1)
            stream.write(b"z")
    calls = {"read": store.read,
             "review": lambda: store.review(before["revision"], record["id"], {value["paths"][0]["path"]: "ha"}, uid(), observed(value)),
             "claim": lambda: store.claim(before["revision"], uid(), record["id"], record["approval"]["binding"], observed(value)),
             "effect": lambda: store.checkpoint(before["revision"], worker["id"], worker["epoch"], "effect_started", observed(value)),
             "copy": lambda: store.copy_diff(record["id"], DigestConsumer())}
    with pytest.raises(native.InvalidState, match="artifact"):
        calls[boundary]()
    assert (store.data_root / "native-operations/state.json").read_bytes() == native.canonical(before)


@pytest.mark.parametrize("producer", [[b"okay", None], [b"okay", "text"], [b"", b"valid", b""]])
def test_stream_chunk_validation_and_empty_chunks(store, producer):
    before = store.read()
    if None in producer or "text" in producer:
        with pytest.raises(native.InvalidState, match="chunk"):
            store.preview_stream(before["revision"], "save", identity(), producer)
        assert store.read() == before
    else:
        record = store.preview_stream(before["revision"], "save", identity(), producer)
        assert store.read_diff(record["id"]) == b"valid"


def test_stream_producer_exception_and_giant_chunk_do_not_publish_partial_graph(store):
    before = store.read()
    def failing():
        yield b"first chunk"
        raise OSError(28, "producer storage exhausted")
    with pytest.raises(OSError):
        store.preview_stream(before["revision"], "save", identity(), failing())
    assert store.read() == before
    large = b"a" * (17 * 1024 * 1024)
    record = store.preview_stream(before["revision"], "save", identity(), [large])
    assert store.read_diff(record["id"]) == large


def test_selected_fingerprint_graph_larger_than_old_manifest_ceiling(store):
    value = identity()
    template = value["paths"][0]
    value["paths"] = [dict(template, path="homeassistant/" + str(number).zfill(6) + ".yaml") for number in range(19000)]
    assert len(native.canonical(value)) > 4 * 1024 * 1024
    record = store.preview(0, "save", value, b"large metadata, tiny diff")
    record = store.review(store.read()["revision"], record["id"], {value["paths"][0]["path"]: "ha"}, uid(), observed(value))
    worker = claim(store, record)
    other = NamespacedStore(store.data_root)
    assert other.read()["workers"][worker["id"]] == worker
    assert other.accounting()["manifest_bytes"] > 4 * 1024 * 1024


def frozen_v1(tmp_path, reviewed_domain=None):
    """Build original schema-1 fields/formulas directly, independent of v2 writer."""
    state = {"schema": 1, "revision": 13, "instance_id": uid(), "updated_at": "2026-10-05T12:00:00+00:00",
             "app_version": "frozen-legacy", "epoch": 0, "records": {}, "current": dict.fromkeys(sorted(native.DOMAINS)),
             "workers": {}, "active": None, "commands": {}, "baseline_record_id": None, "baseline_scope": None, "active_scope": None}
    root = tmp_path / "native-operations"
    root.mkdir(mode=0o700)
    artifacts = root / "artifacts"
    artifacts.mkdir(mode=0o700)
    requests = []
    domains = [reviewed_domain] if reviewed_domain else ["migration", "scope", "save", "apply"]
    policy = dict(identity()["scope"], revision=7)
    for domain in domains:
        value = candidate_at_scope(domain, policy)
        content = ("frozen original " + domain).encode()
        key = native.hashlib.sha256(content).hexdigest()
        artifact = artifacts / (key + ".diff")
        artifact.write_bytes(content)
        artifact.chmod(0o600)
        choices = {value["paths"][0]["path"]: "ha"} if domain in {"save", "apply"} else {}
        binding = native.digest({"identity": value, "artifact": key, "decisions": choices})
        record = {"id": uid(), "domain": domain, "identity": value, "artifact": key, "identity_digest": native.digest(value),
                  "status": "reviewed", "decisions": choices, "approval": {"id": uid(), "binding": binding},
                  "created_at": state["updated_at"], "validated_at": state["updated_at"], "checkpoints": []}
        state["records"][record["id"]] = record
        state["current"][domain] = record["id"]
        if state["baseline_record_id"] is None:
            state.update(baseline_record_id=record["id"], baseline_scope=copy.deepcopy(policy), active_scope=copy.deepcopy(policy))
        if reviewed_domain:
            continue
        state["epoch"] += 1
        operation, owner, marker, command = uid(), uid(), uid(), uid()
        worker = {"id": operation, "record_id": record["id"], "owner": owner, "epoch": state["epoch"],
                  "phase": "accepted", "marker": marker, "evidence": None, "outcome": None, "binding": binding}
        phases = ["accepted", "failed"] if domain == "save" else ["accepted", "effect_started", "completed"]
        if domain == "apply":
            phases = ["accepted", "effect_started", "recovery_required", "recovery_required", "recovery_required"]
        for index, phase in enumerate(phases):
            if phase == "recovery_required":
                state["epoch"] += 1
                owner = uid()
            evidence = dict(value["effect"], marker=marker) if phase == "completed" else None
            record["checkpoints"].append({"operation_id": operation, "owner": owner, "epoch": state["epoch"], "phase": phase, "evidence": evidence})
            worker.update(owner=owner, epoch=state["epoch"], phase=phase, evidence=evidence,
                          outcome=phase if phase in {"completed", "failed"} else None)
        record["status"] = {"failed": "stale", "completed": "completed", "recovery_required": "recovery_required"}[worker["phase"]]
        if worker["phase"] == "failed":
            record.update(approval=None, decisions={})
        if domain == "scope":
            policy = value["scope_transaction"]["new"]
            state["active_scope"] = copy.deepcopy(policy)
        if domain == "apply":
            state["active"] = operation
        state["workers"][operation] = worker
        state["commands"][command] = {"payload_digest": native.digest({"record_id": record["id"], "binding": binding}), "operation_id": operation}
        requests.append((command, record["id"], binding, copy.deepcopy(worker)))
    manifest = root / "state.json"
    manifest.write_bytes(native.canonical(state))
    manifest.chmod(0o600)
    return state, requests


def test_explicit_v1_upgrade_retains_original_request_binding_and_exact_replay(tmp_path):
    legacy, requests = frozen_v1(tmp_path)
    store = native.NativeOperationStore(tmp_path.resolve(), retention=2)
    manifest = tmp_path / "native-operations/state.json"
    original = manifest.read_bytes()
    assert store.read() == legacy
    assert store.refresh()["draft"] == {} and manifest.read_bytes() == original
    store.upgrade(legacy["revision"])
    state = store.read()
    assert state["schema"] == 2 and state["instance_id"] == legacy["instance_id"] and state["epoch"] == legacy["epoch"]
    assert state["active"] == legacy["active"] and state["current"] == legacy["current"]
    for command, record_id, binding, worker in requests:
        accepted, replay = store.claim(0, command, record_id, binding, None)
        assert not accepted and replay == worker and store.read() == state
        record = state["records"][record_id]
        assert record["binding_version"] == 1
        receipt = state["commands"][str(record["generation"]) + ":" + command]
        assert receipt["payload_digest"] == legacy["commands"][command]["payload_digest"] and receipt["request_version"] == 1
        with pytest.raises(native.Conflict):
            store.claim(state["revision"], command, record_id, "0" * 64, None)
    with pytest.raises(native.Conflict):
        store.claim(state["revision"], uid(), requests[0][1], requests[0][2], None)


@pytest.mark.parametrize("domain", sorted(native.DOMAINS))
def test_unconsumed_v1_review_needs_v2_request_and_keeps_v1_approval(tmp_path, domain):
    legacy, _ = frozen_v1(tmp_path, reviewed_domain=domain)
    store = native.NativeOperationStore(tmp_path.resolve())
    store.upgrade(legacy["revision"])
    record = next(iter(store.read()["records"].values()))
    original = legacy["records"][record["id"]]
    assert record["approval"] == original["approval"] and record["binding_version"] == 1
    command = uid()
    with pytest.raises(native.Conflict):
        raw_claim(store, record, command)
    accepted, worker = raw_claim(store, record, command, instance_id=record["instance_id"], generation=record["generation"])
    assert accepted and worker["binding"] == original["approval"]["binding"]
    assert next(iter(store.read()["commands"].values()))["request_version"] == 2
    with pytest.raises(native.Conflict):
        store.claim(0, command, record["id"], worker["binding"], None)


@pytest.mark.parametrize("stage", ["state_written", "state_fsynced", "state_replaced", "state_durable"])
def test_v1_upgrade_hard_crash_preserves_old_or_complete_new_graph(tmp_path, stage):
    legacy, requests = frozen_v1(tmp_path)
    result = run_child('''import os,sys
from native_operations import NativeOperationStore
def fault(stage):
 if stage==sys.argv[2]:os._exit(81)
s=NativeOperationStore(sys.argv[1],fault=fault);s.upgrade(13)
''', tmp_path, stage)
    assert result.returncode == 81, result.stderr
    store = native.NativeOperationStore(tmp_path.resolve())
    state = store.read()
    assert state["schema"] == (2 if stage in {"state_replaced", "state_durable"} else 1)
    for command, record_id, binding, worker in requests:
        assert store.claim(0, command, record_id, binding, None) == (False, worker)
    for record in state["records"].values():
        assert store.read_diff(record["id"]) == ("frozen original " + record["domain"]).encode()


@pytest.mark.parametrize("corruption", ["unknown", "epoch_gap", "lost_effect_intent", "approval", "artifact"])
def test_invalid_v1_upgrade_preserves_original_bytes(tmp_path, corruption):
    legacy, _ = frozen_v1(tmp_path)
    if corruption == "unknown":
        legacy["schema"] = 99
    elif corruption == "epoch_gap":
        legacy["epoch"] += 1
    elif corruption == "lost_effect_intent":
        record = legacy["records"][legacy["current"]["apply"]]
        record["checkpoints"] = [point for point in record["checkpoints"] if point["phase"] != "effect_started"]
    elif corruption == "approval":
        legacy["records"][legacy["current"]["migration"]]["approval"]["binding"] = "0" * 64
    else:
        record = legacy["records"][legacy["current"]["migration"]]
        (tmp_path / "native-operations/artifacts" / (record["artifact"] + ".diff")).write_bytes(b"broken")
    path = tmp_path / "native-operations/state.json"
    path.write_bytes(native.canonical(legacy))
    before = path.read_bytes()
    with pytest.raises(native.InvalidState):
        native.NativeOperationStore(tmp_path.resolve()).upgrade(legacy["revision"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry", ["unknown", "malformed_temp", "symlink", "fifo", "hardlink"])
def test_sweep_preserves_unknown_and_unsafe_entries(store, entry):
    seed(store)
    directory = store.data_root / "native-operations/artifacts"
    outside = store.data_root / "sentinel"
    outside.write_bytes(b"outside original")
    outside.chmod(0o600)
    path = directory / ("0" * 64 + ".diff")
    if entry == "unknown":
        path = directory / "user-notes"
        path.write_bytes(b"unknown")
    elif entry == "malformed_temp":
        path = directory / ".tmp-not-a-canonical-uuid"
        path.write_bytes(b"unknown")
    elif entry == "symlink":
        path.symlink_to(outside)
    elif entry == "fifo":
        os.mkfifo(path, 0o600)
    else:
        os.link(outside, path)
    store.compact(store.read()["revision"])
    assert path.exists() and outside.read_bytes() == b"outside original"


def test_sweep_deduplicates_and_retries_cleanup_debt_without_rolling_back(store):
    first = store.preview(0, "save", identity(), b"shared bytes")
    second = store.preview(store.read()["revision"], "apply", identity("apply"), b"shared bytes")
    path = store.data_root / "native-operations/artifacts" / (first["artifact"] + ".diff")
    store.preview(store.read()["revision"], "save", identity(), b"new save")
    assert path.exists()
    def fault(stage):
        if stage == "cleanup_before_unlink":
            raise OSError("cleanup unavailable")
    broken = NamespacedStore(store.data_root, fault=fault)
    replacement = broken.preview(broken.read()["revision"], "apply", identity("apply"), b"new apply")
    assert broken.cleanup_errors and path.exists()
    before = store.read()
    assert before["current"]["apply"] == replacement["id"] and second["id"] not in before["records"]
    assert store.refresh()["draft"] == {} and path.exists()
    assert store.accounting()["reclaimable_bytes"] == len(b"shared bytes")
    store.compact(before["revision"])
    assert not path.exists() and store.accounting()["reclaimable_bytes"] == 0


@pytest.mark.parametrize("stage", ["state_written", "state_replaced", "cleanup_before_unlink", "cleanup_unlinked", "cleanup_durable"])
def test_pruning_hard_crash_never_deletes_authoritative_artifact(store, stage):
    old = store.preview(0, "save", identity(), b"old graph")
    before = store.read()
    result = run_child('''import os,sys,json
from native_operations import NativeOperationStore
def fault(stage):
 if stage==sys.argv[2]:os._exit(82)
s=NativeOperationStore(sys.argv[1],fault=fault);s.preview(int(sys.argv[3]),'save',json.loads(sys.argv[4]),b'new graph')
''', store.data_root, stage, str(before["revision"]), json.dumps(identity()))
    assert result.returncode == 82, result.stderr
    state = store.read()
    assert state["revision"] in {before["revision"], before["revision"] + 1}
    for record in state["records"].values():
        assert store.read_diff(record["id"]) in {b"old graph", b"new graph"}
    store.compact(state["revision"])
    assert len(list((store.data_root / "native-operations/artifacts").iterdir())) == 1


def test_killed_producer_temporary_is_observationally_retained_then_swept(store):
    before = store.read()
    result = run_child('''import os,sys,json
from native_operations import NativeOperationStore
def producer():
 yield b'interrupted private chunk'
 os._exit(83)
s=NativeOperationStore(sys.argv[1]);s.preview_stream(0,'save',json.loads(sys.argv[2]),producer())
''', store.data_root, json.dumps(identity()))
    assert result.returncode == 83, result.stderr
    directory = store.data_root / "native-operations/artifacts"
    assert len(list(directory.glob(".tmp-*"))) == 1
    assert store.read() == before and len(list(directory.glob(".tmp-*"))) == 1
    store.compact(before["revision"])
    assert not list(directory.iterdir())


@pytest.mark.parametrize("case", ["frontier", "count", "anchor_source", "anchor_interval", "baseline", "migration", "active", "crossing", "missing_anchor"])
def test_compacted_provenance_corruption_fails_closed(tmp_path, case):
    store = NamespacedStore(tmp_path.resolve(), retention=2)
    first, _ = complete_candidate(store, identity("migration"))
    for number in range(5):
        complete_candidate(store, candidate_at_scope("scope", store.read()["active_scope"]))
    current = review_candidate(store, candidate_at_scope("save", store.read()["active_scope"]))
    worker = claim(store, current)
    state = store.read()
    if case == "frontier":
        state["checkpoint"]["through_epoch"] += 1
    elif case == "count":
        state["checkpoint"]["publications"] += 1
    elif case == "anchor_source":
        anchor = state["anchors"][native.digest(state["active_scope"])]
        anchor["source"]["worker"]["evidence"]["after"] = "0" * 64
        anchor["source"]["digest"] = native.digest({key: item for key, item in anchor["source"].items() if key != "digest"})
    elif case == "anchor_interval":
        state["anchors"][native.digest(state["active_scope"])]["start_after_epoch"] += 1
    elif case == "baseline":
        state["baseline_scope"]["revision"] += 1
    elif case == "migration":
        state["checkpoint"]["migration"]["record"]["checkpoints"] = []
        capsule = state["checkpoint"]["migration"]
        capsule["digest"] = native.digest({key: item for key, item in capsule.items() if key != "digest"})
    elif case == "active":
        state["active_scope"] = state["baseline_scope"]
    elif case == "crossing":
        state["records"][current["id"]]["checkpoints"][0]["epoch"] = state["checkpoint"]["through_epoch"]
    else:
        del state["anchors"][native.digest(first["identity"]["scope"])]
    state["checkpoint"]["digest"] = native.digest({key: item for key, item in state["checkpoint"].items() if key != "digest"})
    assert_corrupt_store_blocks_all_operations(store, state, current, worker)


@pytest.mark.parametrize("case", ["count", "last_epoch", "first_epoch", "latest_owner", "lost_intent", "phase_downgrade", "crossed_frontier"])
def test_summarized_recovery_corruption_keeps_every_authority_fenced(store, case):
    record = seed(store, "apply")
    worker = claim(store, record)
    store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    for number in range(8):
        store.lifecycle(store.read()["revision"], "takeover", {})
    state = store.read()
    current = state["workers"][worker["id"]]
    stored = state["records"][record["id"]]
    summary = stored["recovery_range"]
    if case in {"count", "last_epoch", "first_epoch"}:
        summary[case] += 1
    elif case == "latest_owner":
        summary["owner"] = uid()
    elif case == "lost_intent":
        stored["checkpoints"] = [point for point in stored["checkpoints"] if point["phase"] != "effect_started"]
    elif case == "phase_downgrade":
        stored["checkpoints"][-1]["phase"] = "accepted"
        current["phase"] = "accepted"
        stored["status"] = "consumed"
    else:
        state["checkpoint"]["through_epoch"] = current["epoch"] - 1
        state["checkpoint"]["digest"] = native.digest({key: item for key, item in state["checkpoint"].items() if key != "digest"})
    assert_corrupt_store_blocks_all_operations(store, state, record, worker)


def test_migrated_known_receipt_wrong_namespace_never_falls_back_to_legacy(tmp_path):
    legacy, requests = frozen_v1(tmp_path)
    store = native.NativeOperationStore(tmp_path.resolve())
    store.upgrade(legacy["revision"])
    command, record_id, binding, worker = requests[0]
    state = store.read()
    record = state["records"][record_id]
    for namespace in ({"instance_id": uid(), "generation": record["generation"]},
                      {"instance_id": record["instance_id"], "generation": record["generation"] + 1},
                      {"instance_id": record["instance_id"]}):
        with pytest.raises(native.Conflict):
            store.claim(0, command, record_id, binding, None, **namespace)
        assert store.read() == state
    assert store.claim(0, command, record_id, binding, None,
                       instance_id=record["instance_id"], generation=record["generation"]) == (False, worker)


@pytest.mark.parametrize("stage", ["artifact_written", "state_written", "state_fsynced", "state_replaced", "state_durable"])
def test_large_stream_storage_failure_preserves_committed_graph(store, stage):
    old = seed(store)
    before = store.read()
    def fault(point):
        if point == stage:
            raise OSError(28, "injected volume exhausted")
    broken = NamespacedStore(store.data_root, fault=fault)
    with pytest.raises(OSError):
        broken.preview_stream(before["revision"], "apply", identity("apply"), ShortNonseekable(17 * 1024 * 1024 + 19))
    reopened = store.read()
    assert reopened["records"][old["id"]] == old
    if stage in {"state_replaced", "state_durable"}:
        record = reopened["records"][reopened["current"]["apply"]]
        assert record["artifact_size"] == 17 * 1024 * 1024 + 19 and record["approval"] is None
    else:
        assert reopened == before
    store.compact(reopened["revision"])
    assert not list((store.data_root / "native-operations/artifacts").glob(".tmp-*"))


def test_verified_copy_emits_nothing_before_full_integrity_validation(store):
    record = store.preview(0, "save", identity(), b"q" * (17 * 1024 * 1024))
    path = store.data_root / "native-operations/artifacts" / (record["artifact"] + ".diff")
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"z")
    consumer = DigestConsumer()
    with pytest.raises(native.InvalidState):
        store.copy_diff(record["id"], consumer)
    assert consumer.size == 0


@pytest.mark.parametrize("fail_adoption_sync", [False, True])
def test_orphan_digest_adoption_syncs_artifact_directory_before_manifest(store, fail_adoption_sync):
    before = store.read()
    def interrupted(stage):
        if stage == "artifact_replaced":
            raise OSError(28, "orphan name not yet durable")
    broken = NamespacedStore(store.data_root, fault=interrupted)
    with pytest.raises(OSError):
        broken.preview(before["revision"], "save", identity(), b"orphan adoption bytes")
    directory = store.data_root / "native-operations/artifacts"
    events = []
    real_fsync = native.os.fsync
    def tracked(fd):
        if os.path.samestat(os.fstat(fd), directory.stat()):
            events.append("artifact_directory_fsync")
            if fail_adoption_sync:
                raise OSError(5, "adopted name fsync failed")
        return real_fsync(fd)
    def stop_before_sweep(stage):
        events.append(stage)
        if stage == "state_durable":
            raise OSError(5, "stop after publication before cleanup")
    retry = NamespacedStore(store.data_root, fault=stop_before_sweep)
    with mock.patch.object(native.os, "fsync", tracked), pytest.raises(OSError):
        retry.preview(before["revision"], "save", identity(), b"orphan adoption bytes")
    assert "artifact_directory_fsync" in events
    if fail_adoption_sync:
        assert "state_written" not in events and store.read() == before
    else:
        assert events.index("artifact_directory_fsync") < events.index("state_written")
        after = store.read()
        assert after["revision"] == before["revision"] + 1
        assert store.read_diff(after["current"]["save"]) == b"orphan adoption bytes"


@pytest.mark.parametrize("invalid", [bytearray(), memoryview(b""), bytearray(b"data"), memoryview(b"data"), None, ""])
@pytest.mark.parametrize("kind", ["reader", "iterator"])
def test_every_producer_chunk_is_typed_before_eof(store, invalid, kind):
    before = store.read()
    chunks = iter([b"valid prefix", invalid, b"unread suffix", b""])
    class Reader:
        def read(self, size):
            assert size <= native.CHUNK_SIZE
            return next(chunks)
    producer = Reader() if kind == "reader" else chunks
    with pytest.raises(native.InvalidState, match="chunk"):
        store.preview_stream(before["revision"], "save", identity(), producer)
    assert store.read() == before
    assert not list((store.data_root / "native-operations/artifacts").iterdir())


@pytest.mark.parametrize("namespace_kind", ["foreign", "correct", "partial_instance", "partial_generation", "boolean", "explicit_nulls", "null_instance", "null_generation"])
def test_unupgraded_v1_never_replays_a_supplied_v2_namespace(tmp_path, namespace_kind):
    legacy, requests = frozen_v1(tmp_path)
    store = native.NativeOperationStore(tmp_path.resolve())
    command, record_id, binding, worker = requests[0]
    namespace = {"foreign": {"instance_id": uid(), "generation": 1},
                 "correct": {"instance_id": legacy["instance_id"], "generation": 1},
                 "partial_instance": {"instance_id": legacy["instance_id"]},
                 "partial_generation": {"generation": 1},
                 "boolean": {"generation": False},
                 "explicit_nulls": {"instance_id": None, "generation": None},
                 "null_instance": {"instance_id": None},
                 "null_generation": {"generation": None}}[namespace_kind]
    original = (tmp_path / "native-operations/state.json").read_bytes()
    with pytest.raises(native.Conflict):
        store.claim(0, command, record_id, binding, None, **namespace)
    assert store.claim(0, command, record_id, binding, None) == (False, worker)
    assert (tmp_path / "native-operations/state.json").read_bytes() == original


@pytest.mark.parametrize("directory", ["artifacts", "manifest", "both"])
@pytest.mark.parametrize("mutation", ["preview", "claim", "completion"])
def test_postcommit_cleanup_enumeration_is_debt_and_retries(store, directory, mutation):
    record = seed(store)
    command = uid()
    if mutation == "completion":
        worker = claim(store, record, command)
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
    before = store.read()
    root = store.data_root / "native-operations"
    artifact_dir = root / "artifacts"
    obsolete = artifact_dir / ("0" * 64 + ".diff")
    obsolete.write_bytes(b"owned obsolete bytes")
    obsolete.chmod(0o600)
    temporary = root / (".tmp-" + uid())
    temporary.write_bytes(b"interrupted manifest")
    temporary.chmod(0o600)
    attempted = []
    real_listdir = native.os.listdir
    def failing(fd):
        kind = "artifacts" if os.path.samestat(os.fstat(fd), artifact_dir.stat()) else "manifest"
        attempted.append(kind)
        if directory in {kind, "both"}:
            raise OSError(5, kind + " cleanup enumeration failed")
        return real_listdir(fd)
    with mock.patch.object(native.os, "listdir", failing):
        if mutation == "preview":
            result = store.preview(before["revision"], "apply", identity("apply"), b"new durable candidate")
        elif mutation == "claim":
            result = claim(store, record, command)
        else:
            result = store.checkpoint(before["revision"], worker["id"], worker["epoch"], "completed",
                                      evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    assert attempted == ["artifacts", "manifest"] and store.cleanup_errors
    state = store.read()
    assert state["revision"] == before["revision"] + 1
    if mutation == "preview":
        assert state["current"]["apply"] == result["id"] and store.read_diff(result["id"]) == b"new durable candidate"
    else:
        assert store.claim(0, command, record["id"], record["approval"]["binding"], None) == (False, result)
    if directory in {"artifacts", "both"}:
        assert obsolete.exists()
    if directory in {"manifest", "both"}:
        assert temporary.exists()
    store.compact(state["revision"])
    assert not obsolete.exists() and not temporary.exists() and not store.cleanup_errors


def rehash_capsule_and_checkpoint(state):
    capsule = state["checkpoint"]["migration"]
    if capsule is not None:
        capsule["digest"] = native.digest({key: value for key, value in capsule.items() if key != "digest"})
    state["checkpoint"]["digest"] = native.digest({key: value for key, value in state["checkpoint"].items() if key != "digest"})


@pytest.mark.parametrize("binding_version", [1, 2])
@pytest.mark.parametrize("case", ["missing", "foreign_instance", "future_generation", "generation_zero", "generation_alias", "owner", "marker", "record_uuid", "worker_uuid", "interval", "scope", "older_latest"])
def test_folded_migration_matches_namespace_and_retained_sources(tmp_path, binding_version, case):
    if binding_version == 1:
        legacy, _ = frozen_v1(tmp_path, reviewed_domain="migration")
        store = NamespacedStore(tmp_path.resolve(), retention=0)
        store.upgrade(legacy["revision"])
        record = next(iter(store.read()["records"].values()))
        worker = claim(store, record)
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "effect_started", observed(record["identity"]))
        store.checkpoint(store.read()["revision"], worker["id"], worker["epoch"], "completed",
                         evidence=dict(record["identity"]["effect"], marker=worker["marker"]))
    else:
        store = NamespacedStore(tmp_path.resolve(), retention=0)
        record, worker = complete_candidate(store, identity("migration"))
    older = copy.deepcopy(store.read()["checkpoint"]["migration"])
    if case == "older_latest":
        store.recovery_pin(store.read()["revision"], None, record["id"], record["identity_digest"])
        complete_candidate(store, candidate_at_scope("migration", store.read()["active_scope"]), b"later completed migration")
    complete_candidate(store, candidate_at_scope("save", store.read()["active_scope"]))
    state = store.read()
    capsule = state["checkpoint"]["migration"]
    cap_record, cap_worker = capsule["record"], capsule["worker"]
    if case == "missing":
        state["checkpoint"]["migration"] = None
    elif case == "older_latest":
        state["checkpoint"]["migration"] = older
    elif case == "foreign_instance":
        cap_record["instance_id"] = uid()
    elif case in {"future_generation", "generation_zero", "generation_alias"}:
        cap_record["generation"] = {"future_generation": state["generation"] + 50,
                                    "generation_zero": 0,
                                    "generation_alias": state["records"][state["current"]["save"]]["generation"]}[case]
    elif case == "owner":
        cap_worker["owner"] = uid()
        for point in cap_record["checkpoints"]:
            point["owner"] = cap_worker["owner"]
    elif case == "marker":
        cap_worker["marker"] = uid()
        cap_worker["evidence"]["marker"] = cap_worker["marker"]
        cap_record["checkpoints"][-1]["evidence"]["marker"] = cap_worker["marker"]
    elif case == "record_uuid":
        cap_record["id"] = uid()
        cap_worker["record_id"] = cap_record["id"]
    elif case == "worker_uuid":
        cap_worker["id"] = uid()
        for point in cap_record["checkpoints"]:
            point["operation_id"] = cap_worker["id"]
    elif case == "interval":
        cap_worker["epoch"] += 1
        for point in cap_record["checkpoints"]:
            point["epoch"] += 1
    else:
        cap_record["identity"]["scope"]["allowlist"] = ["changed-policy", "homeassistant"]
        cap_record["identity_digest"] = native.digest(cap_record["identity"])
    if state["checkpoint"]["migration"] is not None:
        cap_record = state["checkpoint"]["migration"]["record"]
        cap_worker = state["checkpoint"]["migration"]["worker"]
        cap_record["approval"]["binding"] = native._binding(cap_record, cap_record["decisions"])
        cap_worker["binding"] = cap_record["approval"]["binding"]
    rehash_capsule_and_checkpoint(state)
    assert_corrupt_store_blocks_all_operations(store, state, record)


def test_retired_full_migration_capsule_remains_valid_without_diff_reference(tmp_path):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    record, _ = complete_candidate(store, identity("migration"), b"retired migration unique bytes")
    original_capsule = copy.deepcopy(store.read()["checkpoint"]["migration"])
    publication, _ = complete_candidate(store, candidate_at_scope("scope", store.read()["active_scope"]))
    store.preview(store.read()["revision"], "migration", candidate_at_scope("migration", store.read()["active_scope"]), b"replacement unclaimed")
    store.preview(store.read()["revision"], "scope", candidate_at_scope("scope", store.read()["active_scope"]), b"new unclaimed scope")
    state = store.read()
    assert record["id"] not in state["records"]
    assert not (tmp_path / "native-operations/artifacts" / (record["artifact"] + ".diff")).exists()
    assert native.digest(record["identity"]["scope"]) not in state["anchors"]
    assert state["checkpoint"]["migration"] == original_capsule
    assert NamespacedStore(tmp_path.resolve()).read() == state


@pytest.mark.parametrize("case", ["foreign_instance", "future_generation", "boolean_generation", "publication_overlap", "baseline_identity", "future_policy", "publication_generation_alias", "publication_record_uuid", "publication_worker_uuid"])
def test_retired_migration_capsule_checks_available_namespace_and_certificates(tmp_path, case):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    record, _ = complete_candidate(store, identity("migration"), b"retired unique migration")
    complete_candidate(store, candidate_at_scope("scope", store.read()["active_scope"]))
    current = store.preview(store.read()["revision"], "migration", candidate_at_scope("migration", store.read()["active_scope"]), b"unclaimed replacement")
    store.preview(store.read()["revision"], "scope", candidate_at_scope("scope", store.read()["active_scope"]), b"unclaimed scope replacement")
    state = store.read()
    assert record["id"] not in state["records"] and not state["workers"]
    capsule = state["checkpoint"]["migration"]
    cap_record, cap_worker = capsule["record"], capsule["worker"]
    if case == "foreign_instance":
        cap_record["instance_id"] = uid()
    elif case == "future_generation":
        cap_record["generation"] = state["generation"] + 1
    elif case == "boolean_generation":
        cap_record["generation"] = True
    elif case == "publication_overlap":
        cap_worker["epoch"] += 1
        for point in cap_record["checkpoints"]:
            point["epoch"] += 1
    elif case == "baseline_identity":
        cap_record["identity"]["refs"]["base"]["oid"] = "4" * 40
    elif case.startswith("publication_"):
        source = state["anchors"][native.digest(state["active_scope"])]["source"]
        if case == "publication_generation_alias":
            cap_record["generation"] = source["record"]["generation"]
        elif case == "publication_record_uuid":
            cap_record["id"] = source["record"]["id"]
            cap_worker["record_id"] = cap_record["id"]
        else:
            cap_worker["id"] = source["worker"]["id"]
            for point in cap_record["checkpoints"]:
                point["operation_id"] = cap_worker["id"]
    else:
        cap_record["identity"]["scope"]["revision"] = state["active_scope"]["revision"] + 1
    cap_record["identity_digest"] = native.digest(cap_record["identity"])
    cap_record["approval"]["binding"] = native._binding(cap_record, cap_record["decisions"])
    cap_worker["binding"] = cap_record["approval"]["binding"]
    rehash_capsule_and_checkpoint(state)
    assert_corrupt_store_blocks_all_operations(store, state, current)


@pytest.mark.parametrize("namespace", [{"instance_id": None}, {"generation": None}, {"instance_id": None, "generation": None}])
def test_explicit_null_namespace_cannot_enter_migrated_legacy_lookup(tmp_path, namespace):
    legacy, requests = frozen_v1(tmp_path)
    store = native.NativeOperationStore(tmp_path.resolve())
    store.upgrade(legacy["revision"])
    state = store.read()
    command, record_id, binding, worker = requests[0]
    with pytest.raises(native.Conflict):
        store.claim(0, command, record_id, binding, None, **namespace)
    assert store.claim(0, command, record_id, binding, None) == (False, worker)
    assert store.read() == state


@pytest.mark.parametrize("domain", ["save", "apply", "migration", "scope"])
@pytest.mark.parametrize("alias", ["generation", "record_uuid", "operation_uuid"])
def test_archived_publication_capsule_reconciles_every_retained_tuple(tmp_path, domain, alias):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    publication, _ = complete_candidate(store, identity("scope"), b"original publication")
    store.preview(store.read()["revision"], "scope", candidate_at_scope("scope", store.read()["active_scope"]), b"superseded full scope source")
    retained, worker = complete_candidate(store, candidate_at_scope(domain, store.read()["active_scope"]), b"distinct retained tuple")
    state = store.read()
    source_policy = publication["identity"]["scope_transaction"]["new"]
    capsule = state["anchors"][native.digest(source_policy)]["source"]
    source_record, source_worker = capsule["record"], capsule["worker"]
    assert source_record["id"] not in state["records"]
    if alias == "generation":
        source_record["generation"] = retained["generation"]
    elif alias == "record_uuid":
        source_record["id"] = retained["id"]
        source_worker["record_id"] = retained["id"]
    else:
        source_worker["id"] = worker["id"]
        for point in source_record["checkpoints"]:
            point["operation_id"] = worker["id"]
    source_record["approval"]["binding"] = native._binding(source_record, source_record["decisions"])
    source_worker["binding"] = source_record["approval"]["binding"]
    capsule["digest"] = native.digest({key: value for key, value in capsule.items() if key != "digest"})
    assert_corrupt_store_blocks_all_operations(store, state, retained)


@pytest.mark.parametrize("alias", ["generation", "record_uuid", "operation_uuid", "epoch_interval"])
def test_retired_publication_capsules_reconcile_each_other_without_full_sources(tmp_path, alias):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    first, _ = complete_candidate(store, identity("scope"), b"first publication")
    policy = store.read()["active_scope"]
    old_scope_candidate = store.preview(store.read()["revision"], "save", candidate_at_scope("save", policy), b"old policy current pin")
    second, _ = complete_candidate(store, candidate_at_scope("scope", policy), b"second publication")
    current = store.preview(store.read()["revision"], "scope", candidate_at_scope("scope", store.read()["active_scope"]), b"retired second full source")
    state = store.read()
    left = state["anchors"][native.digest(first["identity"]["scope_transaction"]["new"])]["source"]
    right = state["anchors"][native.digest(second["identity"]["scope_transaction"]["new"])]["source"]
    assert first["id"] not in state["records"] and second["id"] not in state["records"] and not state["workers"]
    if alias == "generation":
        left["record"]["generation"] = right["record"]["generation"]
    elif alias == "record_uuid":
        left["record"]["id"] = right["record"]["id"]
        left["worker"]["record_id"] = right["record"]["id"]
    elif alias == "operation_uuid":
        left["worker"]["id"] = right["worker"]["id"]
        for point in left["record"]["checkpoints"]:
            point["operation_id"] = right["worker"]["id"]
    else:
        # Both source histories must stay internally legal and terminal epochs
        # still establish their anchors; an acceptance interval may not overlap.
        for point in right["record"]["checkpoints"]:
            point["epoch"] = left["worker"]["epoch"]
        right["worker"]["epoch"] = left["worker"]["epoch"]
        right_anchor = state["anchors"][native.digest(second["identity"]["scope_transaction"]["new"])]
        right_anchor["start_after_epoch"] = left["worker"]["epoch"]
    for capsule in (left, right):
        capsule["record"]["approval"]["binding"] = native._binding(capsule["record"], capsule["record"]["decisions"])
        capsule["worker"]["binding"] = capsule["record"]["approval"]["binding"]
        capsule["digest"] = native.digest({key: value for key, value in capsule.items() if key != "digest"})
    assert_corrupt_store_blocks_all_operations(store, state, current)


def test_retired_publication_capsule_matches_minimal_original_baseline(tmp_path):
    store = NamespacedStore(tmp_path.resolve(), retention=0)
    baseline = store.preview(0, "save", identity(), b"original Save baseline")
    publication, _ = complete_candidate(store, identity("scope"), b"publication after baseline")
    store.preview(store.read()["revision"], "save", candidate_at_scope("save", store.read()["active_scope"]), b"retired baseline full tuple")
    current = store.preview(store.read()["revision"], "scope", candidate_at_scope("scope", store.read()["active_scope"]), b"retired publication full tuple")
    state = store.read()
    assert baseline["id"] not in state["records"] and publication["id"] not in state["records"]
    capsule = state["anchors"][native.digest(state["active_scope"])]["source"]
    capsule["record"]["id"] = baseline["id"]
    capsule["worker"]["record_id"] = baseline["id"]
    capsule["digest"] = native.digest({key: value for key, value in capsule.items() if key != "digest"})
    assert_corrupt_store_blocks_all_operations(store, state, current)


if __name__ == "__main__" and sys.argv[1:] == ["--container-recreation"]:
    container_recreation_probe()
