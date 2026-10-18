from pathlib import Path
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from native_status import NativeStatus
import native_services as n
from native_inventory import Policy


def test_dormant_projection_has_no_effect(tmp_path):
    result=NativeStatus().read()
    assert result['actions_enabled'] is False and not result['available']
    assert list(tmp_path.iterdir())==[]
    assert Policy(extra_live_roots=(b'data',)).reason(b'data/direct-auth/tokens.json')


def test_genuine_service_lock_busy(tmp_path):
    store=n.ServiceStore(tmp_path/'services')
    status=NativeStatus(services=store)
    with store._lock():
        result=status.read()
        assert result['services']['status']=='busy'
    assert status.read()['services'] is None


def test_recovery_cache_tracks_actual_files(tmp_path):
    class Recovery:
        directory=b'data/recovery'
        class binding:root=bytes(tmp_path)
        calls=0
        def discover(self):
            self.calls+=1
            return dict(manifest={'cleanup':{'files':{}}},journal=dict(operation='op',generation='g',revision=1 if file.read_text()=='a' else 2,phase='ready',changed=[],pending=None))
    root=tmp_path/'data/recovery';root.mkdir(parents=True);file=root/'active.json';file.write_text('a')
    store=Recovery();status=NativeStatus(recovery=store)
    for _ in range(5):assert status.read()['recovery']['revision']==1
    assert store.calls==2
    file.write_text('b');assert status.read()['recovery']['revision']==2


def test_existing_status_does_not_initialize_dormant_journals(tmp_path):
    from native_status import existing_status
    assert existing_status(tmp_path).read()['services'] is None
    assert list(tmp_path.iterdir()) == []


import pytest
@pytest.mark.skipif(sys.platform!='linux',reason='genuine recovery allocation requires Linux runner')
def test_genuine_recovery_validation_cached_and_corruption_blocks(tmp_path, monkeypatch):
    from test_native_recovery import point, write, file
    write(tmp_path,b'homeassistant/a',b'before')
    store,installed=point(tmp_path,{b'homeassistant/a':file(b'after')})
    status=NativeStatus(recovery=store)
    discover=store.discover;calls=[]
    def observe():calls.append(1);return discover()
    monkeypatch.setattr(store,'discover',observe)
    for _ in range(5):assert status.read()['recovery']['phase']==installed['journal']['phase']
    assert len(calls)==2
    store.execute()
    previous=len(calls)
    assert status.read()['recovery']['phase']=='applied' or status.read()['recovery']['phase']=='complete'
    assert len(calls)==previous+1
    generation=tmp_path/'data/native/recovery'/installed['pointer']['generation']
    blob=next(generation.glob('blob-*'));blob.write_bytes(b'corruption')
    assert status.read()['recovery']['status']=='blocked'


def test_genuine_service_journal_removal_clears_healthy_projection(tmp_path):
    from test_native_services import Adapter, fence
    store=n.ServiceStore(tmp_path/'native-services');adapter=Adapter()
    state=store.prepare(adapter);state=store.stop(*fence(state),adapter);store.restore(*fence(state),adapter)
    status=NativeStatus(services=store)
    assert status.read()['services']['phase']=='restored'
    (store.directory/'state.json').unlink()
    assert status.read()['services'] is None
    assert status.service_value is None
    store.prepare(adapter)
    assert status.read()['services']['phase']=='prepared'


def test_late_journals_attach_without_creating_dormant_state(tmp_path):
    from native_status import existing_status
    from native_operations import NativeOperationStore
    from test_native_services import Adapter
    status=existing_status(tmp_path)
    assert status.read()['operation'] is None and list(tmp_path.iterdir())==[]
    NativeOperationStore(tmp_path).read()
    n.ServiceStore(tmp_path/'native-services').prepare(Adapter())
    result=status.read()
    assert result['operation']['revision']==0 and result['services']['phase']=='prepared'


def test_operation_disappears_inside_observation_without_reinitialization(tmp_path,monkeypatch):
    from native_status import existing_status
    from native_operations import NativeOperationStore
    store=NativeOperationStore(tmp_path);store.read()
    status=existing_status(tmp_path);status.read()
    observed=status.operations;read_file=observed._read_file
    path=tmp_path/'native-operations/state.json'
    def disappear(directory,name):
        if name=='state.json':path.unlink()
        return read_file(directory,name)
    monkeypatch.setattr(observed,'_read_file',disappear)
    assert status.read()['operation']['status']=='blocked'
    assert not path.exists()
    empty=tmp_path/'empty';empty.mkdir()
    with pytest.raises(FileNotFoundError):NativeOperationStore(empty).read(observe=True)
    assert list(empty.iterdir())==[]


def test_service_fifo_observation_is_bounded_and_recovers(tmp_path):
    import multiprocessing
    import os
    from test_native_services import Adapter
    store=n.ServiceStore(tmp_path/'services');store.prepare(Adapter())
    status=NativeStatus(services=store);assert status.read()['services']['phase']=='prepared'
    path=store.directory/'state.json';before=path.read_bytes();path.unlink();os.mkfifo(path,0o600)
    context=multiprocessing.get_context('fork');queue=context.Queue()
    process=context.Process(target=lambda:queue.put(status.read()))
    process.start();process.join(2)
    try:
        assert not process.is_alive(),'FIFO blocked diagnostic reader'
        assert queue.get(timeout=1)['services']['status']=='blocked'
    finally:
        if process.is_alive():process.terminate();process.join(2)
        queue.close();queue.join_thread()
    path.unlink();path.write_bytes(before);path.chmod(0o600)
    assert status.read()['services']['phase']=='prepared'


def test_recovery_control_content_invalidates_when_stat_identity_is_equal(tmp_path,monkeypatch):
    class Recovery:
        directory=b'data/recovery'
        class binding:root=bytes(tmp_path)
        calls=0
        def discover(self):
            self.calls+=1
            return dict(manifest={'cleanup':{'files':{}}},journal=dict(operation='op',generation='g',revision=1 if control.read_text()=='a' else 2,phase='ready',changed=[],pending=None))
    directory=tmp_path/'data/recovery';directory.mkdir(parents=True)
    control=directory/'active.json';control.write_text('a')
    saved=control.lstat();original=Path.lstat
    monkeypatch.setattr(Path,'lstat',lambda self: saved if self==control else original(self))
    store=Recovery();status=NativeStatus(recovery=store)
    assert status.read()['recovery']['revision']==1
    control.write_text('b')
    assert status.read()['recovery']['revision']==2
    assert store.calls==3


def test_operation_observer_does_not_recreate_missing_artifacts(tmp_path):
    from native_operations import NativeOperationStore
    store=NativeOperationStore(tmp_path);store.read()
    artifacts=tmp_path/'native-operations/artifacts';artifacts.rmdir()
    before=(tmp_path/'native-operations/state.json').read_bytes()
    with pytest.raises(FileNotFoundError):store.read(observe=True)
    assert not artifacts.exists()
    assert (tmp_path/'native-operations/state.json').read_bytes()==before


@pytest.mark.parametrize('cold', [True,False])
@pytest.mark.parametrize('change', ['journal','blob','generation'])
def test_recovery_cache_does_not_publish_old_snapshot_under_new_key(tmp_path,cold,change):
    import json
    import os
    root=tmp_path/'data/recovery';generation=root/'g';generation.mkdir(parents=True)
    pointer=root/'active.json';pointer.write_text('g')
    journal=generation/'journal.json';blob=generation/'blob-fixture';blob.write_text('before')
    def publish(revision):
        pending=generation/'new';pending.write_text(json.dumps(dict(operation='op',generation=generation.name,revision=revision,phase='ready',changed=[],pending=None)));os.replace(pending,journal)
    publish(1)
    class Recovery:
        directory=b'data/recovery'
        class binding:root=bytes(tmp_path)
        calls=0;race=False
        def discover(self):
            nonlocal generation,journal,blob
            self.calls+=1;validated=json.loads(journal.read_text())
            if self.race:
                self.race=False
                if change=='generation':
                    generation=root/'next';generation.mkdir();journal=generation/'journal.json';blob=generation/'blob-fixture';blob.write_text('after');pointer.write_text('next')
                elif change=='blob':blob.write_text('after')
                publish(validated['revision']+1)
            return dict(manifest={'cleanup':{'files':{'blob-fixture':{}}}},journal=validated)
    store=Recovery();status=NativeStatus(recovery=store)
    if not cold:assert status.read()['recovery']['revision']==1;publish(2)
    store.race=True
    status.read()
    actual=json.loads(journal.read_text())['revision']
    stable=status.read()['recovery'];assert stable['revision']==actual
    calls=store.calls
    for _ in range(3):assert status.read()['recovery']['revision']==actual
    assert store.calls==calls


@pytest.mark.skipif(sys.platform!='linux',reason='genuine recovery allocation requires Linux runner')
def test_genuine_recovery_worker_update_after_discovery_is_not_cached_old(tmp_path,monkeypatch):
    from test_native_recovery import point,write,file
    write(tmp_path,b'homeassistant/a',b'before')
    store,_=point(tmp_path,{b'homeassistant/a':file(b'after')})
    status=NativeStatus(recovery=store);assert status.read()['recovery']['phase']=='ready'
    discover=store.discover;ran=[]
    def race():
        validated=discover()
        if not ran:
            ran.append(True)
            # Genuine worker executes after the snapshot was validated. Its
            # actual atomic journal/blob changes precede cache publication.
            store.execute()
        return validated
    monkeypatch.setattr(store,'discover',race)
    status.recovery_key=None
    status.read()
    assert status.read()['recovery']['phase']=='applied'
    revision=status.read()['recovery']['revision']
    assert (tmp_path/'homeassistant/a').read_bytes()==b'after'
    assert status.read()['recovery']['revision']==revision and len(ran)==1


def test_continuous_recovery_changes_are_bounded_then_stable_poll_recovers(tmp_path):
    root=tmp_path/'data/recovery';root.mkdir(parents=True)
    control=root/'active.json';control.write_text('1')
    class Recovery:
        directory=b'data/recovery'
        class binding:root=bytes(tmp_path)
        calls=0;changing=True
        def discover(self):
            self.calls+=1;revision=int(control.read_text())
            if self.changing:control.write_text(str(revision+1))
            return dict(manifest={'cleanup':{'files':{}}},journal=dict(operation='op',generation='g',revision=revision,phase='ready',changed=[],pending=None))
    store=Recovery();status=NativeStatus(recovery=store)
    assert status.read()['recovery']['status']=='busy' and store.calls==2
    assert status.read()['recovery']['status']=='busy' and store.calls==4
    store.changing=False
    assert status.read()['recovery']['revision']==int(control.read_text())
    calls=store.calls;status.read();assert store.calls==calls
