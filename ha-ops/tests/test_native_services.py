"""Dormant lifecycle: fixture capabilities never represent Supervisor receipts."""
import ast
import copy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import native_services as n


def item(target, startup="application", state="running", manageable=True):
    return dict(id=target, startup=startup, state=state, manageable=manageable,
                capability="manageable" if manageable else "cannot_manage")


class Adapter:
    def __init__(self, entries=None):
        self.entries = entries or [item("app:self"), item("app:a"), item("core", "core")]
        self.states = {e["id"]: e["state"] for e in self.entries}
        self.calls, self.timeouts = [], []
        self.fail, self.lost = set(), set()
        self.health = {}
        self.proof = "unknown"

    def inventory(self, *, timeout):
        self.timeouts.append(timeout)
        return dict(complete=True, self="app:self", entries=copy.deepcopy(self.entries))

    def observe(self, target, *, timeout):
        self.timeouts.append(timeout)
        return dict(target=target, state=self.states[target])

    def command(self, pending, *, timeout):
        self.timeouts.append(timeout)
        kind, target = pending["kind"], pending["target"]
        self.calls.append((kind, target))
        result = {key: pending[key] for key in ("operation", "owner", "attempt", "target", "kind")}
        if (kind, target) in self.fail:
            return dict(result, outcome="rejected")
        self.states[target] = "stopped" if kind == "stop" else "running"
        if (kind, target) in self.lost:
            raise RuntimeError("credential-secret")
        return dict(result, outcome="accepted")

    def readiness(self, target, *, timeout):
        self.timeouts.append(timeout)
        return copy.deepcopy(self.health.get(target, dict(target=target, status="healthy", recovery_mode=False)))

    def evidence(self, pending, *, timeout):
        self.timeouts.append(timeout)
        return dict({key: pending[key] for key in ("operation", "owner", "attempt", "target", "kind")}, outcome=self.proof)


def fence(state):
    return state["operation"], state["owner"], state["revision"]


def entry(state, target):
    return next(e for e in state["entries"] if e["id"] == target)


def setup(tmp_path, entries=None):
    adapter = Adapter(entries)
    store = n.ServiceStore(tmp_path / "services")
    return store, adapter, store.prepare(adapter)


def test_full_frozen_inventory_order_stopped_preservation_and_replay(tmp_path):
    entries = [item("app:self"), item("core", "core"), item("app:off", state="stopped"),
               item("app:excluded", manageable=False)]
    entries += [item("app:" + phase, phase) for phase in ("initialize", "system", "services", "application", "once")]
    store, adapter, state = setup(tmp_path, entries)
    assert adapter.calls == []
    assert len(state["entries"]) == len(entries)
    assert entry(state, "app:off")["exclusion"] == "originally_stopped"
    assert entry(state, "app:self")["exclusion"] == "self"
    assert entry(state, "app:excluded")["exclusion"] == "capability_excluded"
    stopped = store.stop(*fence(state), adapter)
    assert stopped["phase"] == "stopped"
    expected_stop = ["app:application", "app:once", "core", "app:services", "app:system", "app:initialize"]
    assert adapter.calls == [("stop", target) for target in expected_stop]
    restored = store.restore(*fence(stopped), adapter)
    assert restored["phase"] == "restored"
    assert adapter.calls[len(expected_stop):] == [("start", target) for target in
        ("app:initialize", "app:system", "app:services", "core", "app:application", "app:once")]
    before = copy.deepcopy(adapter.calls)
    assert store.restore(*fence(restored), adapter) == restored
    assert adapter.calls == before
    assert all(timeout > 0 and timeout <= 60 for timeout in adapter.timeouts)
    with pytest.raises(n.Conflict):
        store.prepare(adapter)
    replacement = store.prepare(adapter, replace=fence(restored))
    assert replacement["operation"] != restored["operation"]


def test_large_inventory_and_stopped_core(tmp_path):
    entries = [item("app:self"), item("core", "core", "stopped")]
    entries += [item("app:a" + str(i)) for i in range(150)]
    store, adapter, state = setup(tmp_path, entries)
    state = store.stop(*fence(state), adapter)
    state = store.restore(*fence(state), adapter)
    assert state["phase"] == "restored" and len(adapter.calls) == 300
    assert not any(target in {"core", "app:self"} for _, target in adapter.calls)


def test_finite_compensation_preserves_original_inventory_and_receipts(tmp_path):
    store, adapter, state = setup(tmp_path, [item('app:self'),item('app:a'),item('core','core'),item('app:off',state='stopped')])
    state=store.stop(*fence(state),adapter)
    adapter.health['core']=dict(target='core',status='unhealthy',recovery_mode=True)
    failed=store.restore(*fence(state),adapter)
    assert failed['phase']=='recovery_required'
    assert entry(failed,'core')['start'] is not None and entry(failed,'app:a')['start'] is None
    compensated=store.compensate(*fence(failed),adapter)
    assert compensated['phase']=='stopped'
    assert compensated['compensation']==failed
    assert adapter.calls.count(('stop','core'))==2
    assert adapter.calls.count(('stop','app:a'))==1
    adapter.health.clear()
    restored=store.restore(*fence(compensated),adapter)
    assert restored['phase']=='restored'
    assert not any(target in ('app:self','app:off') for _,target in adapter.calls)
    with pytest.raises(n.Conflict,match='already consumed'):
        store.compensate(*fence(restored),adapter)
    assert n.ServiceStore(store.directory).discover()==restored


def test_compensation_unknown_start_never_grants_stop_ownership(tmp_path):
    store,adapter,state=setup(tmp_path)
    stopped=store.stop(*fence(state),adapter)
    adapter.lost.add(('start','core'))
    uncertain=store.restore(*fence(stopped),adapter)
    before=list(adapter.calls)
    with pytest.raises(n.Conflict):store.compensate(*fence(uncertain),adapter)
    assert adapter.calls==before


@pytest.mark.parametrize("change", ["incomplete", "duplicate", "unknown", "self_missing", "core_missing", "capability", "extra"])
def test_invalid_inventory_zero_effects(tmp_path, change):
    adapter = Adapter()
    original = adapter.inventory
    def inventory(**kwargs):
        value = original(**kwargs)
        if change == "incomplete": value["complete"] = False
        if change == "duplicate": value["entries"].append(copy.deepcopy(value["entries"][0]))
        if change == "unknown": value["entries"][1]["state"] = adapter.states["app:a"] = "unknown"
        if change == "self_missing": value["entries"] = value["entries"][1:]
        if change == "core_missing": value["entries"].pop()
        if change == "capability": value["entries"][1]["capability"] = "unknown"
        if change == "extra": value["token"] = "credential-secret"
        return value
    adapter.inventory = inventory
    store = n.ServiceStore(tmp_path / "services")
    with pytest.raises(n.InvalidState): store.prepare(adapter, polls=2)
    assert store.discover() is None and adapter.calls == []


def test_transitional_inventory_has_bounded_fresh_resolution(tmp_path):
    adapter = Adapter()
    adapter.entries[1]["state"] = "unknown"
    store = n.ServiceStore(tmp_path / "services")
    state = store.prepare(adapter, polls=2)
    assert entry(state, "app:a")["original"] == "running"


@pytest.mark.parametrize("kind", ["stop", "start"])
def test_lost_ack_requires_qualified_evidence_never_state_only(tmp_path, kind):
    store, adapter, state = setup(tmp_path)
    if kind == "start": state = store.stop(*fence(state), adapter)
    target = "app:a" if kind == "stop" else "core"
    adapter.lost.add((kind, target))
    state = getattr(store, kind if kind == "stop" else "restore")(*fence(state), adapter)
    assert state["phase"] == "recovery_required"
    assert entry(state, target)["uncertain"] is not None
    before = copy.deepcopy(adapter.calls)
    reopened = n.ServiceStore(tmp_path / "services")
    assert reopened.discover() == state and adapter.calls == before
    state = reopened.reconcile(*fence(state), adapter)
    assert entry(state, target)["uncertain"] is not None and adapter.calls == before
    assert "credential-secret" not in (tmp_path / "services/state.json").read_text()
    adapter.proof = "completed"
    state = reopened.reconcile(*fence(state), adapter)
    assert entry(state, target)[kind]["proof"] == "injected_evidence"
    assert entry(state, target)["uncertain"] is None and adapter.calls == before


def test_partial_stop_failure_and_independent_restore(tmp_path):
    entries = [item("app:self"), item("app:a"), item("app:b"), item("core", "core")]
    store, adapter, state = setup(tmp_path, entries)
    adapter.lost.add(("stop", "app:b"))
    state = store.stop(*fence(state), adapter)
    assert adapter.calls == [("stop", "app:a"), ("stop", "app:b")]
    assert entry(state, "core")["stop"] is None
    state = store.restore(*fence(state), adapter)
    assert ("start", "app:a") in adapter.calls
    assert ("start", "app:b") not in adapter.calls
    assert state["phase"] == "recovery_required"


def test_failed_start_blocks_dependents_recovers_same_phase(tmp_path):
    entries = [item("app:self"), item("app:a", "services"), item("app:b", "services"),
               item("core", "core"), item("app:dependent")]
    store, adapter, state = setup(tmp_path, entries)
    state = store.stop(*fence(state), adapter)
    adapter.fail.add(("start", "app:a"))
    state = store.restore(*fence(state), adapter)
    assert ("start", "app:b") in adapter.calls and ("start", "core") not in adapter.calls
    assert entry(state, "core")["error"] == "dependency_blocked"
    assert entry(state, "app:a")["error"] == "command_rejected"
    adapter.fail.clear()
    state = store.restore(*fence(state), adapter)
    assert state["phase"] == "restored"
    assert adapter.calls.count(("start", "app:b")) == 1


@pytest.mark.parametrize("target,status,recovery,expected", [
    ("core", "healthy", True, "recovery_required"),
    ("core", "healthy", None, "recovery_required"),
    ("app:a", "unhealthy", False, "unhealthy"),
    ("app:a", "unknown", False, "uncertain"),
])
def test_readiness_distinct_from_command_running_and_recovery_mode(tmp_path, target, status, recovery, expected):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.health[target] = dict(target=target, status=status, recovery_mode=recovery)
    state = store.restore(*fence(state), adapter, polls=2)
    assert state["phase"] == expected
    assert entry(state, target)["health"] != "healthy"
    if target == "core": assert ("start", "app:a") not in adapter.calls


@pytest.mark.parametrize("malformed", [{}, {"target": "app:wrong", "status": "healthy", "recovery_mode": False},
                                       {"target": "core", "status": "healthy", "recovery_mode": "false"},
                                       {"target": "core", "status": "healthy", "recovery_mode": False, "token": "credential-secret"}])
def test_malformed_readiness_is_redacted_and_never_healthy(tmp_path, malformed):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.health["core"] = malformed
    state = store.restore(*fence(state), adapter)
    assert state["phase"] == "recovery_required" and entry(state, "core")["health"] != "healthy"
    assert "credential-secret" not in (tmp_path / "services/state.json").read_text()


def test_budget_includes_adapter_elapsed_and_no_later_command(tmp_path):
    now = [0.0]
    adapter = Adapter()
    store = n.ServiceStore(tmp_path / "services", clock=lambda: now[0], sleep=lambda value: None)
    state = store.prepare(adapter)
    original = adapter.command
    def command(pending, **kwargs):
        result = original(pending, **kwargs); now[0] += 2
        return result
    adapter.command = command
    state = store.stop(*fence(state), adapter, seconds=1)
    assert adapter.calls == [("stop", "app:a")]
    assert entry(state, "app:a")["uncertain"] is not None
    assert state["phase"] == "recovery_required"


@pytest.mark.parametrize("change", ["revision", "extra", "operation", "selection", "order", "ownership", "phase", "missing", "duplicate", "health"])
def test_persisted_cross_field_corruption_retained_and_effect_free(tmp_path, change):
    store, adapter, state = setup(tmp_path)
    path = tmp_path / "services/state.json"
    if change == "revision": state["revision"] = True
    if change == "extra": state["token"] = "credential-secret"
    if change == "operation": state["operation"] = "bad"
    if change == "selection": entry(state, "app:self")["selected"] = True
    if change == "order": state["order"].pop()
    if change == "ownership": entry(state, "app:a")["start"] = {"operation": state["operation"], "owner": state["owner"], "attempt": str(n.uuid.uuid4()), "target": "app:a", "kind": "start", "proof": "acknowledged"}
    if change == "phase": state["phase"] = "stopped"
    if change == "missing": state["entries"].pop()
    if change == "duplicate": state["entries"].append(copy.deepcopy(state["entries"][0]))
    if change == "health": entry(state, "app:a")["health"] = "healthy"
    path.write_text(json.dumps(state)); before = path.read_bytes()
    with pytest.raises(n.InvalidState): store.discover()
    with pytest.raises(n.InvalidState): store.stop(*fence(state), adapter)
    assert path.read_bytes() == before and not adapter.calls


def test_duplicate_json_keys_and_symlink_pointer_refused(tmp_path):
    store, adapter, state = setup(tmp_path)
    path = tmp_path / "services/state.json"
    raw = path.read_text(); path.write_text(raw[:-1] + ',"schema":1}')
    with pytest.raises(n.InvalidState): store.discover()
    path.unlink(); path.symlink_to(tmp_path / "foreign")
    with pytest.raises(OSError): store.discover()


def test_stale_fences_and_unresolved_replacement(tmp_path):
    store, adapter, state = setup(tmp_path)
    with pytest.raises(n.Conflict): store.stop(state["owner"], state["operation"], state["revision"], adapter)
    with pytest.raises(n.Conflict): store.stop(state["operation"], state["owner"], True, adapter)
    stopped = store.stop(*fence(state), adapter)
    for method in (store.stop, store.restore, store.reconcile):
        with pytest.raises(n.Conflict): method(*fence(state), adapter)
    with pytest.raises(n.Conflict): store.prepare(adapter, replace=fence(stopped))


@pytest.mark.parametrize("boundary", ["intent", "entered", "result", "observation", "ack"])
@pytest.mark.parametrize("kind", ["stop", "start"])
def test_crash_boundary_reopen_and_explicit_reconciliation(tmp_path, boundary, kind):
    # A real subprocess exits at each durable save boundary, without exception cleanup.
    store, adapter, state = setup(tmp_path)
    if kind == "start": state = store.stop(*fence(state), adapter)
    script = '''
import sys,os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1]); import native_services as n
sys.path.insert(0,sys.argv[2]); from test_native_services import Adapter,fence
s=n.ServiceStore(sys.argv[3]); a=Adapter(); st=s.discover()
if sys.argv[4]=='start': a.states={e['id']:('stopped' if e['stop'] else e['original']) for e in st['entries']}
command=a.command
def external(pending,**kwargs):
 result=command(pending,**kwargs)
 (Path(sys.argv[3]).parent/'external-effects.json').write_text(json.dumps(a.states))
 return result
a.command=external
old=s._save; count=[0]
def save(fd,state):
 old(fd,state); count[0]+=1
 if count[0]==int(sys.argv[5]): os._exit(77)
s._save=save
getattr(s,'stop' if sys.argv[4]=='stop' else 'restore')(*fence(st),a)
'''
    # Worker phase save then intent/dispatch/result/observation/ack.
    index = {"intent": 2, "entered": 3, "result": 4, "observation": 5, "ack": 6}[boundary]
    app_dir = str(Path(n.__file__).parent)
    result = subprocess.run([sys.executable, "-c", script, app_dir, str(Path(__file__).parent),
                             str(tmp_path / "services"), kind, str(index)], capture_output=True, text=True)
    assert result.returncode == 77, result.stderr
    fresh = n.ServiceStore(tmp_path / "services"); state = fresh.discover()
    assert adapter.calls == ([] if kind == "stop" else [("stop", "app:a"), ("stop", "core")])
    if boundary != "ack":
        assert state["pending"] is not None
        target = state["pending"]["target"]
        effects = tmp_path / "external-effects.json"
        if effects.exists(): adapter.states = json.loads(effects.read_text())
        state = fresh.reconcile(*fence(state), adapter)
        if boundary in {"intent", "entered"}: assert entry(state, target)["uncertain"] is not None
        else: assert entry(state, target)[kind]["proof"] == "acknowledged"


def test_fsync_failure_before_first_effect(tmp_path):
    store = n.ServiceStore(tmp_path / "services"); adapter = Adapter()
    with mock.patch.object(n.os, "fsync", side_effect=OSError("fixture")):
        with pytest.raises(OSError): store.prepare(adapter)
    assert adapter.calls == []


def _held_worker(path, ready, release):
    store = n.ServiceStore(path)
    adapter = Adapter()
    original = adapter.command
    def command(pending, **kwargs):
        ready.set(); release.wait(10)
        return original(pending, **kwargs)
    adapter.command = command
    state = store.discover(); store.stop(*fence(state), adapter)


def test_two_process_stable_flock_excludes_worker(tmp_path):
    store, adapter, state = setup(tmp_path)
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_held_worker, args=(tmp_path / "services", ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(n.Conflict, match="busy"): store.stop(*fence(state), adapter)
        assert adapter.calls == []
    finally:
        release.set(); process.join(10)
        if process.is_alive(): process.terminate(); process.join()
    assert process.exitcode == 0


def test_not_sent_evidence_requires_fresh_before_state(tmp_path):
    store, adapter, state = setup(tmp_path)
    adapter.lost.add(("stop", "app:a"))
    state = store.stop(*fence(state), adapter)
    adapter.proof = "not_sent"
    state = store.reconcile(*fence(state), adapter)
    assert entry(state, "app:a")["uncertain"] is not None
    adapter.states["app:a"] = "running"
    state = store.reconcile(*fence(state), adapter)
    assert entry(state, "app:a")["uncertain"] is None
    adapter.lost.clear()
    state = store.stop(*fence(state), adapter)
    assert state["phase"] == "stopped"


def test_wrong_command_identity_retains_unknown_ownership(tmp_path):
    store, adapter, state = setup(tmp_path)
    original = adapter.command
    def command(pending, **kwargs):
        return dict(original(pending, **kwargs), target="app:wrong")
    adapter.command = command
    state = store.stop(*fence(state), adapter)
    assert entry(state, "app:a")["uncertain"] is not None
    assert entry(state, "app:a")["stop"] is None


def test_accepted_but_unobserved_start_does_not_strand_independent_peer(tmp_path):
    entries = [item("app:self"), item("app:a", "services"), item("app:b", "services"), item("core", "core")]
    store, adapter, state = setup(tmp_path, entries)
    state = store.stop(*fence(state), adapter)
    original = adapter.command
    def command(pending, **kwargs):
        value = original(pending, **kwargs)
        if pending["kind"] == "start" and pending["target"] == "app:a":
            adapter.states["app:a"] = "stopped"
        return value
    adapter.command = command
    state = store.restore(*fence(state), adapter)
    assert entry(state, "app:a")["uncertain"] is not None
    assert ("start", "app:b") in adapter.calls and ("start", "core") not in adapter.calls


def test_stale_healthy_evidence_is_not_reused_on_failed_probe(tmp_path):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.health["app:a"] = dict(target="app:a", status="unknown", recovery_mode=False)
    state = store.restore(*fence(state), adapter, polls=1)
    assert entry(state, "core")["health"] == "healthy"
    def unavailable(*args, **kwargs): raise RuntimeError("credential-secret")
    adapter.readiness = unavailable
    state = store.restore(*fence(state), adapter, polls=1)
    assert entry(state, "core")["health"] == "unknown"
    assert state["phase"] == "uncertain"
    assert "credential-secret" not in (tmp_path / "services/state.json").read_text()


@pytest.mark.parametrize("field", ["phase", "revision", "order", "pending", "health", "startup", "original", "capability", "stop"])
def test_wrong_json_types_fail_closed(tmp_path, field):
    store, adapter, state = setup(tmp_path)
    if field in state: state[field] = []
    else: entry(state, "app:a")[field] = []
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state))
    with pytest.raises(n.InvalidState): store.discover()
    assert adapter.calls == []


@pytest.mark.parametrize("field", ["target", "attempt", "kind", "owner", "operation", "result", "before"])
def test_pending_cross_field_corruption_refuses_all_effects(tmp_path, field):
    store, adapter, state = setup(tmp_path)
    adapter.lost.add(("stop", "app:a")); state = store.stop(*fence(state), adapter)
    debt = entry(state, "app:a")["uncertain"]
    debt[field] = {"target": "core", "attempt": "invalid", "kind": "start", "owner": state["operation"],
                   "operation": state["owner"], "result": "rejected", "before": {"target": "app:a", "state": "stopped"}}[field]
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    calls = copy.deepcopy(adapter.calls)
    for method in (store.discover, lambda: store.restore(*fence(state), adapter), lambda: store.reconcile(*fence(state), adapter)):
        with pytest.raises(n.InvalidState): method()
    assert path.read_bytes() == before and adapter.calls == calls


def test_lock_name_replacement_during_worker_cannot_bypass_directory_flock(tmp_path):
    store, adapter, state = setup(tmp_path)
    replacement = n.ServiceStore(tmp_path / "services")
    original = adapter.command
    def command(pending, **kwargs):
        lock = tmp_path / "services/lock"; lock.unlink(); lock.touch(mode=0o600)
        with pytest.raises(n.Conflict, match="busy"):
            replacement.stop(*fence(state), Adapter())
        return original(pending, **kwargs)
    adapter.command = command
    with pytest.raises(n.InvalidState): store.stop(*fence(state), adapter)
    assert adapter.calls == [("stop", "app:a")]


def test_pointer_substitution_during_observation_prevents_dispatch(tmp_path):
    store, adapter, state = setup(tmp_path)
    original = adapter.observe
    def observe(target, **kwargs):
        path = tmp_path / "services/state.json"
        temporary = path.with_suffix(".foreign"); temporary.write_bytes(path.read_bytes()); temporary.chmod(0o600)
        temporary.replace(path)
        return original(target, **kwargs)
    adapter.observe = observe
    with pytest.raises(n.InvalidState): store.stop(*fence(state), adapter)
    assert adapter.calls == []


@pytest.mark.parametrize("boundary", ["replace", "file_fsync", "directory_fsync"])
def test_io_failure_after_effect_retains_explicit_recovery(tmp_path, boundary):
    store, adapter, state = setup(tmp_path)
    original_command = adapter.command
    def command(pending, **kwargs):
        value = original_command(pending, **kwargs)
        if boundary == "replace":
            patch = mock.patch.object(n.os, "replace", side_effect=OSError("fixture IO"))
        else:
            original_fsync = n.os.fsync
            def fsync(fd):
                is_directory = __import__('stat').S_ISDIR(os.fstat(fd).st_mode)
                if is_directory == (boundary == "directory_fsync"): raise OSError("fixture IO")
                return original_fsync(fd)
            patch = mock.patch.object(n.os, "fsync", side_effect=fsync)
        patch.start(); patches.append(patch)
        return value
    patches = []; adapter.command = command
    try:
        with pytest.raises(OSError): store.stop(*fence(state), adapter)
    finally:
        for patch in patches: patch.stop()
    fresh = n.ServiceStore(tmp_path / "services"); state = fresh.discover()
    calls = copy.deepcopy(adapter.calls)
    assert state["pending"] is not None and adapter.calls == [("stop", "app:a")]
    state = fresh.reconcile(*fence(state), adapter)
    assert adapter.calls == calls
    if boundary == "directory_fsync": assert entry(state, "app:a")["stop"] is not None
    else: assert entry(state, "app:a")["uncertain"] is not None


def test_replayed_stop_gate_checks_current_process_state(tmp_path):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.states["app:a"] = "running"
    calls = copy.deepcopy(adapter.calls)
    state = store.stop(*fence(state), adapter)
    assert state["phase"] == "recovery_required" and adapter.calls == calls
    assert entry(state, "app:a")["stop_invalidated"]
    # A later unrelated stop cannot resurrect our earlier stop ownership.
    adapter.states["app:a"] = "stopped"
    state = store.restore(*fence(state), adapter)
    assert ("start", "app:a") not in adapter.calls
    assert state["phase"] == "recovery_required"


def test_restore_observed_external_restart_permanently_invalidates_stop(tmp_path):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.states["app:a"] = "running"
    state = store.restore(*fence(state), adapter)
    assert entry(state, "app:a")["stop_invalidated"]
    adapter.states["app:a"] = "stopped"
    state = store.restore(*fence(state), adapter)
    assert ("start", "app:a") not in adapter.calls


def test_nonprivate_directory_and_journal_refused(tmp_path):
    directory = tmp_path / "public"; directory.mkdir(mode=0o755)
    with pytest.raises(n.InvalidState): n.ServiceStore(directory)
    store, adapter, state = setup(tmp_path)
    (tmp_path / "services/state.json").chmod(0o644)
    with pytest.raises(n.InvalidState): store.discover()


def test_active_worker_survives_ingress_consumer_disconnect_with_core_broker_down(tmp_path):
    entries = [item("app:self"), item("app:a"), item("core", "core"), item("app:broker", "services")]
    store, adapter, state = setup(tmp_path, entries)
    script = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]); import native_services as n
sys.path.insert(0,sys.argv[2]); from test_native_services import Adapter,item,fence
root=Path(sys.argv[3]); s=n.ServiceStore(root/'services')
a=Adapter([item('app:self'),item('app:a'),item('core','core'),item('app:broker','services')])
old=a.command
def command(pending,**kwargs):
 result=old(pending,**kwargs)
 if pending['kind']=='stop' and pending['target']=='app:broker':
  (root/'down.json').write_text(json.dumps(a.states))
  end=time.monotonic()+10
  while not (root/'release').exists():
   if time.monotonic()>end: raise RuntimeError('fixture timeout')
   time.sleep(.01)
 return result
a.command=command
st=s.stop(*fence(s.discover()),a)
st=s.restore(*fence(st),a)
(root/'complete.json').write_text(json.dumps({'phase':st['phase'],'calls':a.calls,'states':a.states}))
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(Path(n.__file__).parent),
                                str(Path(__file__).parent), str(tmp_path)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        end = time.monotonic() + 5
        while not (tmp_path / "down.json").exists() and time.monotonic() < end:
            time.sleep(.01)
        down = json.loads((tmp_path / "down.json").read_text())
        assert down["core"] == down["app:broker"] == "stopped"
        # The pipe is the disconnected request/ingress consumer. The worker
        # remains alive and owns neither that consumer nor a broker connection.
        process.stdin.close(); process.stdin = None
        (tmp_path / "release").touch()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        result = json.loads((tmp_path / "complete.json").read_text())
        assert result["phase"] == "restored"
        assert result["states"]["core"] == result["states"]["app:broker"] == "running"
        assert len(result["calls"]) == 6
    finally:
        if process.poll() is None:
            process.terminate(); process.communicate(timeout=5)


@pytest.mark.parametrize("body_error", [False, True])
def test_lock_close_error_still_closes_directory_once_and_preserves_primary(tmp_path, body_error):
    store, adapter, state = setup(tmp_path)
    if body_error:
        (tmp_path / "services/state.json").write_text("invalid")
    original = os.close; closed = []
    def close(fd):
        closed.append(fd); original(fd)
        raise OSError("fixture close")
    with mock.patch.object(n.os, "close", side_effect=close):
        with pytest.raises(n.InvalidState if body_error else OSError): store.discover()
    assert len(closed) == 2 and len(set(closed)) == 2
    for fd in closed:
        with pytest.raises(OSError): os.fstat(fd)


def test_new_and_reopened_namespace_links_are_durable_before_effects(tmp_path):
    directory = tmp_path / "data" / "nested" / "services"
    synced = set(); original = os.fsync
    def fsync(fd):
        st = os.fstat(fd); synced.add((st.st_dev, st.st_ino)); return original(fd)
    adapter = Adapter(); command = adapter.command
    def verify(pending, **kwargs):
        for parent in (tmp_path, tmp_path / "data", tmp_path / "data/nested"):
            st = parent.stat(); assert (st.st_dev, st.st_ino) in synced
        return command(pending, **kwargs)
    adapter.command = verify
    with mock.patch.object(n.os, "fsync", side_effect=fsync):
        store = n.ServiceStore(directory)
        state = store.prepare(adapter)
        state = store.stop(*fence(state), adapter)
        synced.clear()
        store = n.ServiceStore(directory)
        assert synced
        state = store.restore(*fence(state), adapter)
    assert state["phase"] == "restored"


@pytest.mark.parametrize("failing_parent", ["", "data", "data/nested"])
def test_namespace_fsync_failure_has_zero_effects_and_reopen_requalifies(tmp_path, failing_parent):
    directory = tmp_path / "data/nested/services"
    original = os.fsync; failed = [False]
    target = tmp_path / failing_parent
    def fail(fd):
        st = os.fstat(fd)
        if target.exists() and (st.st_dev, st.st_ino) == (target.stat().st_dev, target.stat().st_ino):
            failed[0] = True; raise OSError("fixture namespace fsync")
        return original(fd)
    adapter = Adapter()
    with mock.patch.object(n.os, "fsync", side_effect=fail):
        with pytest.raises(OSError, match="namespace fsync"): n.ServiceStore(directory)
    assert failed[0] and adapter.calls == []
    synced = set()
    def record(fd):
        st = os.fstat(fd); synced.add((st.st_dev, st.st_ino)); return original(fd)
    with mock.patch.object(n.os, "fsync", side_effect=record):
        store = n.ServiceStore(directory)
        state = store.prepare(adapter)
        before = adapter.command
        def verify(pending, **kwargs):
            for parent in (tmp_path, tmp_path / "data", tmp_path / "data/nested"):
                st = parent.stat(); assert (st.st_dev, st.st_ino) in synced
            return before(pending, **kwargs)
        adapter.command = verify
        assert store.stop(*fence(state), adapter)["phase"] == "stopped"


@pytest.mark.parametrize("mutation", ["flag", "error", "receipt", "start", "debt"])
def test_invalidated_authority_contradictions_refuse_all_mutations(tmp_path, mutation):
    store, adapter, state = setup(tmp_path)
    state = store.stop(*fence(state), adapter)
    adapter.states["app:a"] = "running"
    state = store.restore(*fence(state), adapter)
    value = entry(state, "app:a")
    if mutation == "flag": value["stop_invalidated"] = False
    if mutation == "error": value["error"] = None
    if mutation == "receipt": value["stop"] = None
    if mutation == "start": value["start"] = copy.deepcopy(entry(state, "core")["start"])
    if mutation == "debt": value["uncertain"] = {}
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    adapter.states["app:a"] = "stopped"; calls = copy.deepcopy(adapter.calls)
    for action in (store.discover, lambda: store.restore(*fence(state), adapter),
                   lambda: store.reconcile(*fence(state), adapter), lambda: store.prepare(adapter, replace=fence(state))):
        with pytest.raises(n.InvalidState): action()
    assert path.read_bytes() == before and adapter.calls == calls


@pytest.mark.parametrize("transient", ["unknown", "exception"])
def test_owned_stop_recovers_transient_observation_without_resending(tmp_path, transient):
    store, adapter, state = setup(tmp_path); state = store.stop(*fence(state), adapter)
    original = adapter.observe
    def observe(target, **kwargs):
        if target == "app:a":
            if transient == "exception": raise RuntimeError("temporary read failure")
            return dict(target=target, state="unknown")
        return original(target, **kwargs)
    adapter.observe = observe; state = store.stop(*fence(state), adapter)
    assert state["phase"] == "recovery_required"
    adapter.observe = original; calls = copy.deepcopy(adapter.calls)
    state = store.stop(*fence(state), adapter)
    assert state["phase"] == "stopped" and adapter.calls == calls
    assert entry(state, "app:a")["error"] is None
    # Fresh running evidence still permanently revokes authority after recovery.
    adapter.states["app:a"] = "running"; state = store.stop(*fence(state), adapter)
    adapter.states["app:a"] = "stopped"; state = store.restore(*fence(state), adapter)
    assert entry(state, "app:a")["stop_invalidated"]
    assert ("start", "app:a") not in adapter.calls


@pytest.mark.parametrize("phase", ["restored", "restoring", "recovery_required", "uncertain"])
def test_erased_stop_receipts_with_unexplained_observations_refused(tmp_path, phase):
    store, adapter, state = setup(tmp_path); state = store.stop(*fence(state), adapter)
    state["phase"] = phase
    for value in state["entries"]:
        if value["selected"]: value["stop"] = None
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    calls = copy.deepcopy(adapter.calls)
    for action in (store.discover, lambda: store.restore(*fence(state), adapter),
                   lambda: store.prepare(adapter, replace=fence(state))):
        with pytest.raises(n.InvalidState): action()
    assert path.read_bytes() == before and adapter.calls == calls


@pytest.mark.parametrize("failure", ["rejected", "already_stopped"])
def test_legitimate_partial_stop_restoration_remains_terminal(tmp_path, failure):
    store, adapter, state = setup(tmp_path)
    if failure == "rejected": adapter.fail.add(("stop", "core"))
    else: adapter.states["core"] = "stopped"
    state = store.stop(*fence(state), adapter)
    assert entry(state, "app:a")["stop"] is not None and entry(state, "core")["stop"] is None
    state = store.restore(*fence(state), adapter)
    assert state["phase"] == "restored" and ("start", "core") not in adapter.calls
    assert store.discover() == state
    assert store.prepare(adapter, replace=fence(state))["operation"] != state["operation"]


@pytest.mark.parametrize("latest", ["stopped", "unknown", "exception", "running", "restored"])
@pytest.mark.parametrize("phase", ["recovery_required", "restored"])
def test_owned_stop_disposition_rejects_receipt_erasure_independent_of_observation(tmp_path, latest, phase):
    store, adapter, state = setup(tmp_path); state = store.stop(*fence(state), adapter)
    if latest in {"unknown", "exception"}:
        original = adapter.observe
        def observe(target, **kwargs):
            if target == "app:a":
                if latest == "exception": raise RuntimeError("temporary read failure")
                return dict(target=target, state="unknown")
            return original(target, **kwargs)
        adapter.observe = observe; state = store.stop(*fence(state), adapter); adapter.observe = original
    elif latest == "running":
        adapter.states["app:a"] = "running"; state = store.stop(*fence(state), adapter)
    elif latest == "restored":
        state = store.restore(*fence(state), adapter)
    assert store.discover() == state
    disposition = entry(state, "app:a")["stop_disposition"]
    assert disposition == {"running": "invalidated", "restored": "started"}.get(latest, "owned")
    # Receipt deletion cannot change the independently persisted obligation.
    entry(state, "app:a")["stop"] = None; state["phase"] = phase
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    calls = copy.deepcopy(adapter.calls)
    for action in (store.discover, lambda: store.stop(*fence(state), adapter),
                   lambda: store.restore(*fence(state), adapter), lambda: store.reconcile(*fence(state), adapter),
                   lambda: store.prepare(adapter, replace=fence(state))):
        with pytest.raises(n.InvalidState): action()
    assert path.read_bytes() == before and adapter.calls == calls


@pytest.mark.parametrize("disposition", ["untouched", "started", "invalidated", None, []])
def test_owned_stop_disposition_contradiction_refused(tmp_path, disposition):
    store, adapter, state = setup(tmp_path); state = store.stop(*fence(state), adapter)
    entry(state, "app:a")["stop_disposition"] = disposition
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state))
    calls = copy.deepcopy(adapter.calls)
    with pytest.raises(n.InvalidState): store.restore(*fence(state), adapter)
    assert adapter.calls == calls


def test_stop_disposition_initial_precommand_partial_debt_and_reconcile_positive_controls(tmp_path):
    store, adapter, state = setup(tmp_path)
    assert all(e["stop_disposition"] == "untouched" for e in state["entries"])
    original = adapter.observe
    def unavailable(target, **kwargs):
        if target == "app:a": raise RuntimeError("precommand read unavailable")
        return original(target, **kwargs)
    adapter.observe = unavailable; state = store.stop(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "untouched" and not adapter.calls
    assert store.discover() == state
    adapter.observe = original; adapter.lost.add(("stop", "app:a"))
    state = store.stop(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "stop_pending"
    assert entry(state, "app:a")["uncertain"] is not None
    assert store.discover() == state
    state = store.reconcile(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "stop_pending"
    adapter.proof = "completed"; state = store.reconcile(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "owned" and store.discover() == state
    adapter.lost.clear(); adapter.lost.add(("start", "app:a"))
    state = store.restore(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "start_pending"
    assert entry(state, "app:a")["uncertain"] is not None
    state = store.reconcile(*fence(state), adapter)
    assert entry(state, "app:a")["stop_disposition"] == "started"
    state = store.restore(*fence(state), adapter)
    assert state["phase"] == "restored" and store.discover() == state
    assert entry(state, "core")["stop_disposition"] == "untouched"
    assert store.prepare(adapter, replace=fence(state))["operation"] != state["operation"]


@pytest.mark.parametrize("kind", ["stop", "start"])
@pytest.mark.parametrize("boundary", ["intent", "entered", "accepted", "observed"])
@pytest.mark.parametrize("location", ["pending", "uncertain"])
def test_unacknowledged_obligation_erasure_refused_at_every_durable_boundary(tmp_path, kind, boundary, location):
    store, adapter, state = setup(tmp_path)
    if kind == "start": state = store.stop(*fence(state), adapter)
    class Interrupted(BaseException): pass
    original = store._save; count = [0]
    index = {"intent": 2, "entered": 3, "accepted": 4, "observed": 5}[boundary]
    def save(directory, current):
        original(directory, current); count[0] += 1
        if count[0] == index: raise Interrupted()
    with mock.patch.object(store, "_save", side_effect=save):
        with pytest.raises(Interrupted):
            (store.stop if kind == "stop" else store.restore)(*fence(state), adapter)
    state = store.discover(); pending = state["pending"]; target = pending["target"]
    assert entry(state, target)["stop_disposition"] == kind + "_pending"
    if location == "uncertain":
        entry(state, target)["uncertain"] = pending; state["pending"] = None; state["phase"] = "recovery_required"
        n.validate(state)
        entry(state, target)["uncertain"] = None
    else:
        state["pending"] = None
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    calls, reads = copy.deepcopy(adapter.calls), len(adapter.timeouts)
    for action in (store.discover, lambda: store.stop(*fence(state), adapter),
                   lambda: store.restore(*fence(state), adapter), lambda: store.reconcile(*fence(state), adapter),
                   lambda: store.prepare(adapter, replace=fence(state))):
        with pytest.raises(n.InvalidState): action()
    assert path.read_bytes() == before and adapter.calls == calls and len(adapter.timeouts) == reads


def test_lost_ack_stop_debt_cannot_be_deleted_as_untouched(tmp_path):
    store, adapter, state = setup(tmp_path); adapter.lost.add(("stop", "app:a"))
    state = store.stop(*fence(state), adapter)
    value = entry(state, "app:a")
    assert value["stop_disposition"] == "stop_pending" and value["stop"] is None
    assert value["uncertain"] is not None and adapter.states["app:a"] == "stopped"
    value["uncertain"] = None
    (tmp_path / "services/state.json").write_text(json.dumps(state))
    with pytest.raises(n.InvalidState): store.discover()


@pytest.mark.parametrize("kind", ["stop", "start"])
def test_qualified_not_sent_resets_only_pending_obligation_to_its_prior_stage(tmp_path, kind):
    store, adapter, state = setup(tmp_path)
    if kind == "start": state = store.stop(*fence(state), adapter)
    original = adapter.command
    def not_sent(*args, **kwargs): raise RuntimeError("fixture command not dispatched")
    adapter.command = not_sent
    state = (store.stop if kind == "stop" else store.restore)(*fence(state), adapter)
    target = "app:a" if kind == "stop" else "core"
    assert entry(state, target)["stop_disposition"] == kind + "_pending"
    adapter.proof = "unknown"; state = store.reconcile(*fence(state), adapter)
    assert entry(state, target)["stop_disposition"] == kind + "_pending"
    adapter.proof = "not_sent"; state = store.reconcile(*fence(state), adapter)
    assert entry(state, target)["stop_disposition"] == ("untouched" if kind == "stop" else "owned")
    assert entry(state, target)["uncertain"] is None and store.discover() == state
    adapter.command = original
    state = (store.stop if kind == "stop" else store.restore)(*fence(state), adapter)
    assert state["phase"] == ("stopped" if kind == "stop" else "restored")


@pytest.mark.parametrize("kind", ["stop", "start"])
def test_rejected_command_resets_only_its_pending_obligation(tmp_path, kind):
    store, adapter, state = setup(tmp_path)
    if kind == "start": state = store.stop(*fence(state), adapter)
    target = "app:a" if kind == "stop" else "core"; adapter.fail.add((kind, target))
    state = (store.stop if kind == "stop" else store.restore)(*fence(state), adapter)
    assert entry(state, target)["stop_disposition"] == ("untouched" if kind == "stop" else "owned")
    assert entry(state, target)["uncertain"] is None and state["pending"] is None
    assert store.discover() == state


@pytest.mark.parametrize("location", ["pending", "uncertain"])
def test_invalidated_stop_rejects_any_attempt_before_adapter_reads(tmp_path, location):
    store, adapter, state = setup(tmp_path); state = store.stop(*fence(state), adapter)
    adapter.states["app:a"] = "running"; state = store.restore(*fence(state), adapter)
    pending = dict(operation=state["operation"], owner=state["owner"], attempt=str(n.uuid.uuid4()),
                   target="app:a", kind="start", before=dict(target="app:a", state="stopped"), dispatch="entered", result=None)
    if location == "pending": state["pending"] = pending
    else: entry(state, "app:a")["uncertain"] = pending
    path = tmp_path / "services/state.json"; path.write_text(json.dumps(state)); before = path.read_bytes()
    reads, calls = len(adapter.timeouts), copy.deepcopy(adapter.calls)
    for action in (store.discover, lambda: store.stop(*fence(state), adapter),
                   lambda: store.restore(*fence(state), adapter), lambda: store.reconcile(*fence(state), adapter),
                   lambda: store.prepare(adapter, replace=fence(state))):
        with pytest.raises(n.InvalidState): action()
    assert len(adapter.timeouts) == reads and adapter.calls == calls and path.read_bytes() == before


def test_no_production_import_hook_or_ingress_broker_dependency():
    directory = Path(n.__file__).parent
    for path in directory.glob("*.py"):
        if path.name in ("native_services.py", "native_apply.py"): continue
        if path.name == "native_status.py":
            # Exact observational composition only; never expose worker methods.
            source = path.read_text()
            assert not any("." + name + "(" in source for name in ("prepare", "stop", "restore", "reconcile"))
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): assert all(alias.name != "native_services" for alias in node.names)
            if isinstance(node, ast.ImportFrom): assert node.module != "native_services"
    source = Path(n.__file__).read_text()
    assert "import subprocess" not in source and "import socket" not in source
    # Every worker fixture above runs using only its private adapter/store; there
    # is no request, ingress or broker object to survive or recreate.


def test_absent_service_discovery_does_not_bootstrap_mutation_lock(tmp_path):
    store=n.ServiceStore(tmp_path/'services')
    before=set(store.directory.iterdir())
    assert store.discover() is None
    assert set(store.directory.iterdir())==before
