"""Real Linux Git/recovery/service composition, isolated from live HA."""
import os
from pathlib import Path
import sys
import uuid
import json
import signal
import subprocess
import time
from unittest import mock
import pytest

from test_native_apply import rig,browser_receipt,A,B,canonical,Adapter,item

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='actual native allocation requires Linux')


class Lifecycle(Adapter):
    def validate_core(self,*,timeout):
        return {'valid':True}


class ProcessLifecycle:
    """Owned Linux processes and durable fixture receipts, never Supervisor."""
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(exist_ok=True)
        self.path=self.directory/'processes.json'
        if self.path.exists():self.value=json.loads(self.path.read_text())
        else:
            self.value=dict(pids={},receipts=[])
            for target in ('core','app:on'):self._start(target)
            self._save()

    @staticmethod
    def identity(pid):
        try:
            text=Path('/proc/'+str(pid)+'/stat').read_text();tail=text[text.rfind(')')+2:].split()
            return None if tail[0]=='Z' else tail[19]
        except FileNotFoundError:return None

    def _running(self,target):
        value=self.value['pids'].get(target)
        return value is not None and self.identity(value[0])==value[1]

    def _start(self,target):
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3600)'],start_new_session=True,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.value['pids'][target]=[process.pid,self.identity(process.pid)]

    def _save(self):
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(self.value));os.replace(temporary,self.path)

    def inventory(self,*,timeout):
        return dict(complete=True,self='app:self',entries=[item('app:self'),item('core','core','running' if self._running('core') else 'stopped'),
            item('app:on',state='running' if self._running('app:on') else 'stopped'),item('app:off',state='stopped')])

    def observe(self,target,*,timeout):
        return dict(target=target,state='running' if self._running(target) else 'stopped')

    def command(self,pending,*,timeout):
        target=pending['target'];assert target in ('core','app:on')
        if pending['kind']=='start':self._start(target)
        else:
            pid,identity=self.value['pids'][target]
            if self.identity(pid)!=identity:raise RuntimeError('foreign fixture process')
            os.kill(pid,signal.SIGTERM)
            deadline=time.monotonic()+min(timeout,5)
            while self._running(target) and time.monotonic()<deadline:time.sleep(.005)
            if self._running(target):raise RuntimeError('fixture stop timeout')
        receipt={key:pending[key] for key in ('operation','owner','attempt','target','kind')}
        self.value['receipts'].append(receipt);self._save()
        return dict(receipt,outcome='accepted')

    def readiness(self,target,*,timeout):
        return dict(target=target,status='healthy' if self._running(target) else 'unhealthy',recovery_mode=False)

    def evidence(self,pending,*,timeout):
        receipt={key:pending[key] for key in ('operation','owner','attempt','target','kind')}
        return dict(receipt,outcome='completed' if receipt in self.value['receipts'] else 'unknown')

    def validate_core(self,*,timeout):return {'valid':True}

    def cleanup(self):
        for target,value in self.value['pids'].items():
            if self.identity(value[0])==value[1]:os.kill(value[0],signal.SIGTERM)


def crash_worker(directory):
    tmp=Path(directory);r,w,review=rig(tmp);adapter=ProcessLifecycle(tmp/'processes')
    prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp):
        try:w.run(prepared,adapter,lambda:{'backups':[]})
        except Exception:pass
        w.recovery.fault=lambda phase:os._exit(93) if phase=='effect:replace' else None
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
        print('UNREACHED_CRASH_RESULT '+json.dumps(result),flush=True)
    raise RuntimeError('required crash boundary was not reached')


def test_process_death_before_file_ack_reopen_is_observational_then_explicit_exact_rollback(tmp_path):
    script='import sys;sys.path[:0]=sys.argv[1:3];from test_native_apply_integration import crash_worker;crash_worker(sys.argv[3])'
    app=Path(__file__).resolve().parents[1]/'app'
    child=subprocess.run([sys.executable,'-c',script,str(app),str(Path(__file__).parent),str(tmp_path)],capture_output=True,timeout=40)
    assert child.returncode==93,(child.stdout+child.stderr).decode()
    import native_recovery as recovery
    import native_services as services
    from native_inventory import Policy
    from native_metadata import MANIFEST_PATH
    from test_native_preview import bindings
    root=tmp_path/'live'
    rec=recovery.RecoveryStore(bindings(root,Policy(repository_files=(MANIFEST_PATH,))))
    svc=services.ServiceStore(root/'data/native-services')
    adapter=ProcessLifecycle(tmp_path/'processes')
    before=json.loads(adapter.path.read_text())['receipts']
    try:
        for _ in range(3):
            point=rec.discover();state=svc.discover()
            assert point['journal']['pending'] is not None
            assert (root/os.fsdecode(A)).read_bytes()==b'git\n'
            assert json.loads(adapter.path.read_text())['receipts']==before
        m=point['manifest']
        rec.reconcile(operation_id=m['operation'],owner_id=m['owner'])
        rec.rollback(operation_id=m['operation'],owner_id=m['owner'])
        restored=svc.restore(state['operation'],state['owner'],state['revision'],adapter)
        assert restored['phase']=='restored'
        assert (root/os.fsdecode(A)).read_bytes()==b'base\n'
        receipts=list(adapter.value['receipts'])
        rec.rollback(operation_id=m['operation'],owner_id=m['owner'])
        svc.restore(restored['operation'],restored['owner'],restored['revision'],adapter)
        assert adapter.value['receipts']==receipts
        assert rec.discover() is not None
    finally:adapter.cleanup()


def test_compatible_afterstop_edit_publishes_successor_and_fresh_warning(tmp_path):
    from native_topology_helpers import METADATA
    import subprocess
    from test_native_save import reviewed
    r,w,review=rig(tmp_path)
    base=b'first\n'+b'unchanged\n'*20+b'last\n'
    oid=r.commit({A:base,B:b'base-b\n'})
    from dataclasses import replace
    from native_topology_helpers import git
    git(r.root,'update-ref',r.source.base_ref,oid)
    r.source=replace(r.source,base_oid=oid)
    r.remote({A:base.replace(b'first\n',b'git first\n'),B:b'base-b\n'})
    subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',os.fsdecode(w.transport.endpoint),
        r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    r.live({A:base,B:b'base-b\n'})
    review=reviewed(r,(A,),direction='apply')
    class Changed(Lifecycle):
        def command(self,pending,*,timeout):
            value=super().command(pending,timeout=timeout)
            if pending['kind']=='stop' and pending['target']=='core':
                (r.root/os.fsdecode(A)).write_bytes(base.replace(b'last\n',b'live last\n'))
            return value
    adapter=Changed()
    with mock.patch.object(w.disk,'snapshot',wraps=w.disk.snapshot) as snapshots:
        prepared=w.prepare(review,str(uuid.uuid4()))
        with browser_receipt(w,prepared,tmp_path):
            with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
            result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
        assert result['status']=='WARNING',result
        assert snapshots.call_count==1
    expected=base.replace(b'first\n',b'git first\n').replace(b'last\n',b'live last\n')
    assert (r.root/os.fsdecode(A)).read_bytes()==expected
    assert result['selected']==[] and len(result['residual'])==1
    assert w.recovery.discover() is None
    before=list(adapter.calls)
    assert w.command_status(prepared.command)==result
    assert w.run(prepared,adapter,lambda:{'backups':[]})==result
    assert adapter.calls==before


def test_listener_dies_during_earlier_app_stop_core_never_stops(tmp_path):
    r,w,review=rig(tmp_path)
    class Gone(Lifecycle):
        def command(self,pending,*,timeout):
            result=super().command(pending,timeout=timeout)
            if pending['kind']=='stop' and pending['target']=='app:a':
                self.listener._fixture_roundtrip_done.wait(2)
                self.listener.shutdown();self.listener.server_close()
            return result
    adapter=Gone();prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp_path) as listener:
        adapter.listener=listener
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert result['status']=='ERROR',result
    assert ('stop','core') not in adapter.calls
    assert adapter.states['app:a']=='running'
    assert w.recovery.discover() is not None


def test_real_success_http_receipt_exact_files_original_inventory_one_snapshot(tmp_path):
    r,w,review=rig(tmp_path)
    adapter=Lifecycle([item('app:self'),item('app:on'),item('app:off',state='stopped'),item('core','core')])
    before=canonical(r)
    with mock.patch.object(w.disk,'snapshot',wraps=w.disk.snapshot) as snapshots:
        prepared=w.prepare(review,str(uuid.uuid4()))
        with browser_receipt(w,prepared,tmp_path):
            with pytest.raises(Exception,match='backup-missing'):
                w.run(prepared,adapter,lambda:{'backups':[]})
            assert adapter.calls==[]
            result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
        assert result['status']=='SUCCESS',result
        assert snapshots.call_count==1
    assert (r.root/os.fsdecode(A)).read_bytes()==b'git\n'
    assert canonical(r)==before
    assert adapter.calls==[('stop','app:on'),('stop','core'),('start','core'),('start','app:on')]
    assert w.recovery.discover() is None


def test_afterstop_conflict_restores_services_and_retains_point(tmp_path):
    r,w,review=rig(tmp_path)
    class Changed(Lifecycle):
        def command(self,pending,*,timeout):
            value=super().command(pending,timeout=timeout)
            if pending['target']=='core' and pending['kind']=='stop':
                (r.root/os.fsdecode(A)).write_bytes(b'foreign conflict\n')
            return value
    adapter=Changed()
    prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert result['status']=='ERROR' and result['recovery_retained'],result
    assert (r.root/os.fsdecode(A)).read_bytes()==b'foreign conflict\n'
    assert adapter.states['core']=='running'
    assert w.recovery.discover() is not None


def test_startup_failure_quiesces_acknowledged_starts_before_exact_rollback(tmp_path):
    r,w,review=rig(tmp_path)
    adapter=Lifecycle()
    adapter.health['core']=dict(target='core',status='unhealthy',recovery_mode=True)
    prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert result['status']=='ERROR' and result['recovery_retained'],result
    assert (r.root/os.fsdecode(A)).read_bytes()==b'base\n'
    assert adapter.calls.count(('stop','core'))==2
    assert w.services.discover()['compensation']['operation']==prepared.operation


def test_later_untouched_path_successor_preserves_earlier_effect_and_latest_rollback_preimage(tmp_path):
    from dataclasses import replace
    from test_native_save import reviewed
    from native_topology_helpers import git,METADATA
    r,w,_=rig(tmp_path)
    base=b'first\n'+b'middle\n'*20+b'last\n'
    oid=r.commit({A:base,B:base});git(r.root,'update-ref',r.source.base_ref,oid);r.source=replace(r.source,base_oid=oid)
    r.remote({A:b'git A\n',B:base.replace(b'first\n',b'git first\n')})
    subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',os.fsdecode(w.transport.endpoint),
                    r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    r.live({A:base,B:base});review=reviewed(r,(A,B),direction='apply')
    class Invalid(Lifecycle):
        def validate_core(self,*,timeout):return {'valid':False}
    adapter=Invalid();prepared=w.prepare(review,str(uuid.uuid4()));changed=[]
    def late(phase):
        if phase=='ack_durable:replace' and not changed:
            changed.append(True);(r.root/os.fsdecode(B)).write_bytes(base.replace(b'last\n',b'live last\n'))
    w.recovery.fault=late
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert result['status']=='ERROR' and result['recovery_retained'],result
    assert (r.root/os.fsdecode(A)).read_bytes()==base
    assert (r.root/os.fsdecode(B)).read_bytes()==base.replace(b'last\n',b'live last\n')
    point=w.recovery.discover();assert 'lineage' in point['manifest'] and point['journal']['phase']=='restored'


def execute_fixture(w,review,adapter,tmp_path):
    prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    return prepared,result


def test_concurrent_advertisement_update_never_overwrites_new_live_bytes(tmp_path,monkeypatch):
    r,w,review=rig(tmp_path);adapter=Lifecycle();original=w.transport.advertise;changed=[]
    def advertise():
        result=original()
        if not changed:
            point=w.recovery.discover()
            if point is not None and point['journal']['pending'] is not None:
                changed.append(True);(r.root/os.fsdecode(A)).write_bytes(b'foreign during advertisement\n')
        return result
    monkeypatch.setattr(w.transport,'advertise',advertise)
    _,result=execute_fixture(w,review,adapter,tmp_path)
    assert changed and result['status']=='ERROR',result
    assert (r.root/os.fsdecode(A)).read_bytes()==b'foreign during advertisement\n'
    assert adapter.states['core']=='running'


def test_cleanup_failure_is_durable_consistent_without_rollback(tmp_path):
    r,w,review=rig(tmp_path);adapter=Lifecycle()
    def fail(phase):
        if phase=='retirement_unlinked':raise OSError('fixture IO')
    w.recovery.fault=fail
    prepared,result=execute_fixture(w,review,adapter,tmp_path)
    assert result['status']=='ERROR' and result['cleanup_debt'] and result['recovery_retained'] is False
    assert w.command_status(prepared.command)==result
    assert w.run(prepared,adapter,lambda:{'backups':[]})==result
    assert (r.root/os.fsdecode(A)).read_bytes()==b'git\n' and adapter.states['core']=='running'


def test_new_apply_after_success_and_unaccepted_reset_retire_exact_resources(tmp_path):
    from test_native_save import reviewed
    r,w,review=rig(tmp_path);adapter=Lifecycle()
    first,result=execute_fixture(w,review,adapter,tmp_path);assert result['status']=='SUCCESS',result
    (r.root/os.fsdecode(A)).write_bytes(b'base\n')
    review=reviewed(r,(A,),direction='apply');draft=w.prepare(review,str(uuid.uuid4()))
    directory=r.root/os.fsdecode(w._admission.directory)
    assert directory.exists();w.reset();assert not directory.exists()
    review=reviewed(r,(A,),direction='apply');second,result=execute_fixture(w,review,adapter,tmp_path)
    assert result['status']=='SUCCESS' and second.operation!=first.operation,result


def test_reviewed_close_resolves_matching_pin_and_allows_fresh_apply(tmp_path):
    from test_native_save import reviewed
    class Invalid(Lifecycle):
        def validate_core(self,*,timeout):return {'valid':False}
    r,w,review=rig(tmp_path);adapter=Invalid();prepared,result=execute_fixture(w,review,adapter,tmp_path)
    assert result['status']=='ERROR'
    point=w.recovery.discover();m,j=point['manifest'],point['journal']
    approval=w.recovery.review_close(operation_id=m['operation'],owner_id=m['owner'],revision=j['revision'],command_id=str(uuid.uuid4()))
    closed=w.close_recovery(approval)
    assert closed['error']=='recovery-point-closed' and w.operations.read()['recovery'] is None
    assert w.resolve_closed_recovery(approval.command)==closed
    review=reviewed(r,(A,),direction='apply')
    _,result=execute_fixture(w,review,Lifecycle(),tmp_path)
    assert result['status']=='SUCCESS',result


def test_unselected_scope_difference_is_in_fresh_warning(tmp_path):
    from test_native_save import reviewed
    r,w,_=rig(tmp_path);r.live({A:b'base\n',B:b'keep unselected live\n'})
    review=reviewed(r,(A,),direction='apply')
    _,result=execute_fixture(w,review,Lifecycle(),tmp_path)
    from native_operations import encode_native_path
    assert result['status']=='WARNING' and result['selected']==[]
    assert {row['path'] for row in result['residual']}=={encode_native_path(B)}
    assert (r.root/os.fsdecode(B)).read_bytes()==b'keep unselected live\n'


def test_expired_validation_never_records_success_and_uses_remaining_budget(tmp_path):
    r,w,review=rig(tmp_path);timeouts=[]
    class Slow(Lifecycle):
        def validate_core(self,*,timeout):
            timeouts.append(timeout);w._deadline=time.monotonic()-.01
            return {'valid':True}
    adapter=Slow();prepared=w.prepare(review,str(uuid.uuid4()))
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]},seconds=30)
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True,seconds=30)
    assert result['status']=='ERROR' and result['error']=='apply-deadline-exceeded',result
    assert timeouts and 0<timeouts[0]<30
    assert (r.root/os.fsdecode(A)).read_bytes()==b'base\n'
    assert w.command_status(prepared.command)==result


@pytest.mark.parametrize('boundary',['unlink','intent','ack'])
def test_partial_draft_cleanup_retries_only_remaining_owned_resources(tmp_path,monkeypatch,boundary):
    r,w,review=rig(tmp_path);w.prepare(review,str(uuid.uuid4()))
    sources=list(w._spools);assert len(sources)>=2
    directory=r.root/os.fsdecode(w._admission.directory)
    original=os.unlink;fired=[]
    if boundary=='unlink':
        def fail(name,*args,**kwargs):
            if name==os.path.basename(sources[1].source) and not fired:
                fired.append(True);raise OSError(5,'fixture unlink')
            return original(name,*args,**kwargs)
        monkeypatch.setattr(os,'unlink',fail)
    else:
        publish=w._admission._publish
        def fail(**kwargs):
            value=w._admission._cleanup.get(sources[0].source.hex())
            expected='unlinking' if boundary=='intent' else 'removed'
            if value==expected and not fired:fired.append(True);raise OSError(5,'fixture control')
            return publish(**kwargs)
        monkeypatch.setattr(w._admission,'_publish',fail)
    with pytest.raises(OSError):w.reset()
    assert fired
    if boundary=='intent':assert (r.root/os.fsdecode(sources[0].source)).exists()
    w.reset();assert not directory.exists()


@pytest.mark.parametrize('pointer_present',[True,False])
def test_explicit_cleanup_resolves_completed_debt_and_old_close_replay_preserves_new_draft(tmp_path,pointer_present):
    from test_native_save import reviewed
    r,w,review=rig(tmp_path);adapter=Lifecycle()
    original=w.recovery.retire
    if pointer_present:
        def retire(*args,**kwargs):raise OSError(5,'fixture pre-retire')
        w.recovery.retire=retire
    else:
        w.recovery.fault=lambda phase:(_ for _ in ()).throw(OSError(5,'fixture retired')) if phase=='retirement_unlinked' else None
    prepared,result=execute_fixture(w,review,adapter,tmp_path)
    assert result['status']=='ERROR' and result['cleanup_debt']
    w.recovery.retire=original;w.recovery.fault=lambda _:None
    command=str(uuid.uuid4());approval=w.review_cleanup(prepared.operation,command)
    closed=w.close_recovery(approval)
    assert closed['status']=='SUCCESS' and w.operations.read()['recovery'] is None
    (r.root/os.fsdecode(A)).write_bytes(b'base\n')
    review=reviewed(r,(A,),direction='apply');new=w.prepare(review,str(uuid.uuid4()))
    current=[(s.source,s.state) for s in w._spools]
    assert w.resolve_closed_recovery(command)==closed
    assert w._prepared is new and [(s.source,s.state) for s in w._spools]==current
    assert all((r.root/os.fsdecode(path)).exists() for path,_ in current)
    w.reset()


def test_multiple_successors_retire_predecessors_and_keep_original_affected_preimages(tmp_path):
    from dataclasses import replace
    from test_native_save import reviewed
    from native_topology_helpers import git,METADATA
    paths=(A,B,b'homeassistant/p/c')
    r,w,_=rig(tmp_path)
    r.source=replace(r.source,scope=paths)
    base=b'first\n'+b'middle\n'*40+b'last\n'
    oid=r.commit({p:base for p in paths});git(r.root,'update-ref',r.source.base_ref,oid);r.source=replace(r.source,base_oid=oid)
    r.remote({p:base.replace(b'first\n',b'git first\n') for p in paths})
    subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',os.fsdecode(w.transport.endpoint),r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    r.live({p:base for p in paths});review=reviewed(r,paths,direction='apply')
    class Invalid(Lifecycle):
        def validate_core(self,*,timeout):return {'valid':False}
    changed=[];published=[]
    def fault(phase):
        if phase=='ack_durable:replace' and len(changed)<2:
            path=paths[len(changed)+1];changed.append(path)
            (r.root/os.fsdecode(path)).write_bytes(base.replace(b'last\n',b'live last\n'))
        if phase=='successor_published':
            generations=[p for p in Path(os.fsdecode(w.disk.root+b'/'+w.recovery.directory)).iterdir() if p.is_dir() and len(p.name)==32]
            assert len(generations)==1
            published.append(generations[0].name)
    w.recovery.fault=fault
    _,result=execute_fixture(w,review,Invalid(),tmp_path)
    assert result['status']=='ERROR' and result['recovery_retained'],result
    assert len(published)==2 and published[0]!=published[1]
    assert (r.root/os.fsdecode(A)).read_bytes()==base
    for path in paths[1:]:assert (r.root/os.fsdecode(path)).read_bytes()==base.replace(b'last\n',b'live last\n')
    point=w.recovery.discover()
    assert point['manifest']['lineage']['schema']==2 and point['journal']['phase']=='restored'


def test_explicit_close_after_claim_before_participant_install_releases_owned_spools(tmp_path,monkeypatch):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()))
    monkeypatch.setattr(w.services,'prepare',lambda *a,**k:(_ for _ in ()).throw(OSError(5,'fixture early install')))
    with pytest.raises(Exception):w.run(prepared,Lifecycle(),lambda:{'backups':[]})
    result=w.run(prepared,Lifecycle(),lambda:{'backups':[]},bypass_backup=True)
    assert result['status']=='ERROR' and w.recovery.discover() is None and w.services.discover() is None
    w._resources={}
    command=str(uuid.uuid4());approval=w.review_cleanup(prepared.operation,command)
    assert w.close_recovery(approval)['recovery_retained'] is False
    assert w.operations.read()['recovery'] is None
    assert not (r.root/'data/native/apply'/prepared.operation).exists()


def crash_close_worker(directory,boundary):
    tmp=Path(directory);r,w,review=rig(tmp)
    class Invalid(Lifecycle):
        def validate_core(self,*,timeout):return {'valid':False}
    prepared,result=execute_fixture(w,review,Invalid(),tmp)
    assert result['recovery_retained']
    command=str(uuid.uuid4());(tmp/'close.json').write_text(json.dumps(dict(command=command,operation=prepared.operation)))
    w.recovery.fault=lambda phase:os._exit(95) if phase==boundary else None
    if boundary=='close_operation_commit':
        original=w.operations.apply_closed
        def closed(*args,**kwargs):
            original(*args,**kwargs);os._exit(95)
        w.operations.apply_closed=closed
    approval=w.review_cleanup(prepared.operation,command);w.close_recovery(approval)
    raise AssertionError('close crash not reached')


@pytest.mark.parametrize('boundary',('close_reviewed','close_intent_durable','close_pointer_unlinked','close_operation_commit'))
def test_explicit_close_process_death_reopens_owned_resources_without_replay(tmp_path,boundary):
    script='import sys;sys.path[:0]=sys.argv[1:3];from test_native_apply_integration import crash_close_worker;crash_close_worker(sys.argv[3],sys.argv[4])'
    app=Path(__file__).resolve().parents[1]/'app'
    child=subprocess.run([sys.executable,'-c',script,str(app),str(Path(__file__).parent),str(tmp_path),boundary],capture_output=True,timeout=40)
    assert child.returncode==95,(child.stdout+child.stderr).decode()
    import native_apply as apply
    import native_disk as disk
    import native_recovery as recovery
    import native_services as services
    from native_operations import NativeOperationStore
    from native_inventory import Policy
    from native_metadata import MANIFEST_PATH
    from test_native_preview import bindings
    from types import SimpleNamespace
    root=tmp_path/'live';binding=bindings(root,Policy(repository_files=(MANIFEST_PATH,)))
    w=object.__new__(apply.NativeApply);w.disk=disk.NativeDisk(root);w.recovery=recovery.RecoveryStore(binding,disk=w.disk)
    w.services=services.ServiceStore(root/'data/native-services');w.operations=NativeOperationStore(root/'data')
    w.preview=SimpleNamespace(binding=binding);w._resources={}
    command=json.loads((tmp_path/'close.json').read_text())
    if boundary=='close_reviewed':
        approval=w.review_cleanup(command['operation'],command['command']);result=w.close_recovery(approval)
    else:result=w.resolve_closed_recovery(command['command'])
    assert result['status']=='ERROR' and result['recovery_retained'] is False
    assert w.resolve_closed_recovery(command['command'])==result
    assert w.operations.read()['recovery'] is None
    assert not (root/'data/native/apply'/command['operation']).exists()
    assert (root/os.fsdecode(A)).read_bytes()==b'base\n'


def test_owned_cleanup_recipe_reopen_has_no_arbitrary_control_size_ceiling(tmp_path):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()))
    control=r.root/'data/native/apply'/prepared.operation/'admission.json'
    with control.open('ab') as stream:stream.write(b' '*(16*1024*1024+1));stream.flush();os.fsync(stream.fileno())
    w._resources={}
    w.cleanup_draft(prepared.operation)
    assert not control.parent.exists()


@pytest.mark.parametrize('changed',['receipt','phase','schema'])
def test_reopened_cleanup_rejects_modified_receipt_phase_or_schema(tmp_path,changed):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()))
    control=r.root/'data/native/apply'/prepared.operation/'admission.json';document=json.loads(control.read_text())
    source=w._spools[0];path=r.root/os.fsdecode(source.source)
    if changed=='receipt':
        path.unlink();path.write_bytes(b'foreign replacement')
        document['receipts'][source.source.hex()]=w.recovery._observe(source.source)
    elif changed=='phase':document['cleanup'][source.source.hex()]='removed'
    else:document.pop('snapshot')
    control.write_text(json.dumps(document));control.chmod(0o600);w._resources={}
    with pytest.raises(Exception,match='modified-apply-cleanup-control'):w.cleanup_draft(prepared.operation)
    assert path.exists() and control.exists()


@pytest.mark.parametrize('replace_namespace',[False,True])
def test_missing_control_retry_requires_original_immutable_namespace(tmp_path,monkeypatch,replace_namespace):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));directory=r.root/'data/native/apply'/prepared.operation
    original=os.rmdir
    def fail(path,*a,**k):
        if os.fsencode(path)==prepared.operation.encode():raise OSError(5,'fixture after control unlink')
        return original(path,*a,**k)
    monkeypatch.setattr(os,'rmdir',fail)
    with pytest.raises(OSError):w.cleanup_draft(prepared.operation)
    assert directory.exists() and list(directory.iterdir())==[]
    monkeypatch.setattr(os,'rmdir',original)
    if replace_namespace:
        directory.rename(directory.with_name('retained-original'));directory.mkdir(mode=0o700)
    w._resources={}
    if replace_namespace:
        with pytest.raises(Exception):w.cleanup_draft(prepared.operation)
        assert directory.exists()
    else:
        w.cleanup_draft(prepared.operation);assert not directory.exists()


@pytest.mark.parametrize('prior',[False,True])
@pytest.mark.parametrize('boundary',['pin','service','install'])
def test_no_effect_settlement_covers_pin_and_current_previous_participants(tmp_path,monkeypatch,prior,boundary):
    from test_native_save import reviewed
    r,w,review=rig(tmp_path);adapter=Lifecycle()
    if prior:
        execute_fixture(w,review,adapter,tmp_path)
        (r.root/os.fsdecode(A)).write_bytes(b'base\n');review=reviewed(r,(A,),direction='apply')
    previous=w.services.discover();calls=list(adapter.calls)
    prepared=w.prepare(review,str(uuid.uuid4()))
    target,name=(w.operations,'recovery_pin') if boundary=='pin' else ((w.services,'prepare') if boundary=='service' else (w.recovery,'install'))
    original=getattr(target,name)
    monkeypatch.setattr(target,name,lambda *a,**k:(_ for _ in ()).throw(OSError(5,'fixture pre-effect')))
    with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
    result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert result['no_effect'] is True and adapter.calls==calls
    monkeypatch.setattr(target,name,original)
    before=w.services.discover();w._resources={}
    command=str(uuid.uuid4());approval=w.review_cleanup(prepared.operation,command)
    assert w.close_recovery(approval)['no_effect'] is True
    assert adapter.calls==calls and w.operations.read()['recovery'] is None and w.operations.read()['active'] is None
    if before is not None and before['operation']!=prepared.operation:assert w.services.discover()==previous
    assert not (r.root/'data/native/apply'/prepared.operation).exists()


def test_reopened_cleanup_rejects_identical_signed_control_on_foreign_inode(tmp_path):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()))
    control=r.root/'data/native/apply'/prepared.operation/'admission.json';raw=control.read_bytes();source=w._spools[0]
    with control.open('rb') as original:
        old=os.fstat(original.fileno()).st_ino;control.unlink();control.write_bytes(raw);control.chmod(0o600)
        assert control.stat().st_ino!=old
        w._resources={}
        with pytest.raises(Exception,match='substituted-apply-cleanup-control'):w.cleanup_draft(prepared.operation)
        assert control.exists() and (r.root/os.fsdecode(source.source)).exists()


def test_real_control_temp_fsync_failure_removes_only_owned_temp_and_retry_succeeds(tmp_path,monkeypatch):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));original=os.fsync;failed=[]
    def fsync(fd):
        try:name=os.readlink('/proc/self/fd/'+str(fd))
        except OSError:name=''
        if not failed and '/admission-' in name and name.endswith('.tmp'):
            failed.append(True);raise OSError(5,'fixture temp data fsync')
        return original(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises(OSError):w.reset()
    assert failed and not list((r.root/'data/native/apply'/prepared.operation).glob('*.tmp'))
    w.reset();assert not (r.root/'data/native/apply'/prepared.operation).exists()


def test_diagnostic_and_worker_reads_share_local_service_lock(tmp_path,monkeypatch):
    import threading
    r,w,review=rig(tmp_path);entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
    w.services.prepare(Lifecycle())
    original=w.services._load
    def load(fd):
        if threading.current_thread().name=='diagnostic':
            entered.set();assert release.wait(3)
        return original(fd)
    monkeypatch.setattr(w.services,'_load',load)
    def read(finish=False):
        try:w.services.discover()
        except BaseException as error:errors.append(error)
        finally:
            if finish:done.set()
    diagnostic=threading.Thread(target=read,name='diagnostic');diagnostic.start();assert entered.wait(3)
    worker=threading.Thread(target=lambda:read(True),name='worker');worker.start()
    assert not done.wait(.05)
    release.set();diagnostic.join(3);worker.join(3)
    assert done.is_set() and not errors


def restart_prefix_worker(directory,prefix):
    tmp=Path(directory);r,w,review=rig(tmp);prepared=w.prepare(review,str(uuid.uuid4()))
    (tmp/'prefix.json').write_text(json.dumps(dict(operation=prepared.operation,command=prepared.command)))
    if prefix=='draft':os._exit(97)
    original=w.operations.claim
    def claim(*a,**k):
        result=original(*a,**k)
        if prefix=='claim':os._exit(97)
        return result
    w.operations.claim=claim
    if prefix=='service':w.recovery.install=lambda *a,**k:os._exit(97)
    with pytest.raises(Exception):w.run(prepared,Lifecycle(),lambda:{'backups':[]})
    w.run(prepared,Lifecycle(),lambda:{'backups':[]},bypass_backup=True)
    raise AssertionError('restart prefix was not reached')


def fresh_cleanup_worker(tmp_path):
    import native_apply as apply
    import native_disk as disk
    import native_recovery as recovery
    import native_services as services
    from native_operations import NativeOperationStore
    from native_inventory import Policy
    from native_metadata import MANIFEST_PATH
    from test_native_preview import bindings
    from types import SimpleNamespace
    root=tmp_path/'live';binding=bindings(root,Policy(repository_files=(MANIFEST_PATH,)))
    w=object.__new__(apply.NativeApply);w.disk=disk.NativeDisk(root);w.recovery=recovery.RecoveryStore(binding,disk=w.disk)
    w.services=services.ServiceStore(root/'data/native-services');w.operations=NativeOperationStore(root/'data')
    w.preview=SimpleNamespace(binding=binding);w._resources={};w._prepared=None
    return w


@pytest.mark.parametrize('prefix',['draft','claim','service'])
def test_real_restart_preserves_cleanup_without_resurrecting_consent(tmp_path,prefix):
    script='import sys;sys.path[:0]=sys.argv[1:3];from test_native_apply_integration import restart_prefix_worker;restart_prefix_worker(sys.argv[3],sys.argv[4])'
    app=Path(__file__).resolve().parents[1]/'app'
    child=subprocess.run([sys.executable,'-c',script,str(app),str(Path(__file__).parent),str(tmp_path),prefix],capture_output=True,timeout=40)
    assert child.returncode==97,(child.stdout+child.stderr).decode()
    value=json.loads((tmp_path/'prefix.json').read_text());operation=value['operation'];w=fresh_cleanup_worker(tmp_path)
    assert w._prepared is None
    directory=tmp_path/'live/data/native/apply'/operation
    assert directory.exists()
    if prefix=='draft':
        assert w.cleanup_drafts()==[dict(operation=operation,namespace_bound=True)]
        assert not (tmp_path/'live/data/native-operations/state.json').exists()
        w.cleanup_draft(operation);w.cleanup_draft(operation)
        assert w.cleanup_drafts()==[]
    else:
        state=w.operations.read();assert state['workers'][operation]['phase']=='accepted'
        for version in ('restart-fixture-v1','update-fixture-v2'):
            w.operations.lifecycle(w.operations.read()['revision'],version,{})
            state=w.operations.read();assert state['workers'][operation]['phase']=='recovery_required'
            assert state['workers'][operation]['apply_result']['no_effect'] is True
        approval=w.review_cleanup(operation,str(uuid.uuid4()));result=w.close_recovery(approval)
        assert result['no_effect'] is True and w.operations.read()['active'] is None and w.operations.read()['recovery'] is None
        assert w.command_status(value['command'])==result
    assert not directory.exists() and w._prepared is None
    assert (tmp_path/'live'/os.fsdecode(A)).read_bytes()==b'base\n'


def test_unclaimed_draft_capsule_and_namespace_substitution_refuse_cleanup(tmp_path):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));operation=prepared.operation
    fresh=fresh_cleanup_worker(tmp_path)
    directory=r.root/'data/native/apply'/operation
    directory.rename(directory.with_name('held-original'));directory.mkdir(mode=0o700)
    with pytest.raises(Exception):fresh.cleanup_draft(operation)
    assert directory.exists()
    capsule=r.root/os.fsdecode(w.recovery.directory)/('apply-draft-'+operation+'.json')
    data=json.loads(capsule.read_text());data['authority']['namespace']=list(__import__('native_disk')._identity(directory.stat()))
    capsule.write_text(json.dumps(data));capsule.chmod(0o600)
    with pytest.raises(Exception):fresh.cleanup_drafts()
    assert directory.exists()


def draft_bootstrap_crash_worker(directory,boundary):
    tmp=Path(directory);r,w,review=rig(tmp)
    def fault(phase):
        if phase==boundary:
            (tmp/'draft-prefix.json').write_text(json.dumps(w.recovery.cleanup_drafts()[0]))
            os._exit(98)
    w.recovery.fault=fault;w.prepare(review,str(uuid.uuid4()))
    raise AssertionError('draft bootstrap boundary not reached')


@pytest.mark.parametrize('boundary',['cleanup_draft_registered','cleanup_draft_namespace_created','cleanup_draft_bound'])
def test_draft_bootstrap_process_prefixes_are_cleanup_only_or_addressed_debt(tmp_path,boundary):
    script='import sys;sys.path[:0]=sys.argv[1:3];from test_native_apply_integration import draft_bootstrap_crash_worker;draft_bootstrap_crash_worker(sys.argv[3],sys.argv[4])'
    app=Path(__file__).resolve().parents[1]/'app'
    child=subprocess.run([sys.executable,'-c',script,str(app),str(Path(__file__).parent),str(tmp_path),boundary],capture_output=True,timeout=40)
    assert child.returncode==98,(child.stdout+child.stderr).decode()
    value=json.loads((tmp_path/'draft-prefix.json').read_text());w=fresh_cleanup_worker(tmp_path)
    operation=value['operation'];assert w._prepared is None and w.cleanup_drafts()==[value]
    directory=tmp_path/'live/data/native/apply'/operation
    if boundary=='cleanup_draft_namespace_created':
        with pytest.raises(Exception,match='unacknowledged-cleanup-draft-namespace'):w.cleanup_draft(operation)
        assert directory.exists() and list(directory.iterdir())==[] and w.cleanup_drafts()==[value]
    else:
        w.cleanup_draft(operation);w.cleanup_draft(operation)
        assert not directory.exists() and w.cleanup_drafts()==[]
    assert w._prepared is None and not (tmp_path/'live/data/native-operations/state.json').exists()


def test_claim_and_draft_cleanup_share_lease_claim_wins(tmp_path,monkeypatch):
    import threading
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));adapter=Lifecycle()
    with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
    claimed=threading.Event();release=threading.Event();results=[];original=w.operations.claim
    def claim(*a,**k):
        result=original(*a,**k);claimed.set();assert release.wait(5);return result
    monkeypatch.setattr(w.operations,'claim',claim)
    monkeypatch.setattr(w.services,'prepare',lambda *a,**k:(_ for _ in ()).throw(OSError(5,'fixture postclaim')))
    thread=threading.Thread(target=lambda:results.append(w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)))
    thread.start();assert claimed.wait(5)
    fresh=fresh_cleanup_worker(tmp_path)
    with pytest.raises(Exception,match='draft-claim-or-cleanup-busy'):fresh.cleanup_draft(prepared.operation)
    assert (r.root/'data/native/apply'/prepared.operation).exists()
    release.set();thread.join(5);assert not thread.is_alive() and results[0]['no_effect'] is True
    w.close_recovery(w.review_cleanup(prepared.operation,str(uuid.uuid4())))
    assert adapter.calls==[] and w.operations.read()['recovery'] is None


def test_claim_and_draft_cleanup_share_lease_cleanup_wins(tmp_path,monkeypatch):
    import threading
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));adapter=Lifecycle()
    with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
    fresh=fresh_cleanup_worker(tmp_path);entered=threading.Event();release=threading.Event();original=fresh._cleanup_spools;errors=[]
    def cleanup(operation,**kwargs):entered.set();assert release.wait(5);return original(operation,**kwargs)
    monkeypatch.setattr(fresh,'_cleanup_spools',cleanup)
    def run_cleanup():
        try:fresh.cleanup_draft(prepared.operation)
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=run_cleanup);thread.start();assert entered.wait(5)
    with pytest.raises(Exception,match='draft-claim-or-cleanup-busy'):w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    release.set();thread.join(5);assert not errors
    with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert prepared.operation not in w.operations.read()['workers'] and not (r.root/'data/native/apply'/prepared.operation).exists()
    assert adapter.calls==[]


def test_draft_registration_refuses_existing_fresh_leaf(tmp_path):
    r,w,review=rig(tmp_path);foreign=[]
    def fault(phase):
        if phase=='cleanup_draft_registered':
            op=w.recovery.cleanup_drafts()[0]['operation'];directory=r.root/'data/native/apply'/op
            directory.parent.mkdir(parents=True,exist_ok=True);directory.parent.chmod(0o700);directory.mkdir(mode=0o700);foreign.append(directory)
    w.recovery.fault=fault
    with pytest.raises(Exception,match='foreign-draft-leaf-namespace'):w.prepare(review,str(uuid.uuid4()))
    fresh=fresh_cleanup_worker(tmp_path)
    with pytest.raises(Exception,match='unacknowledged-cleanup-draft-namespace'):fresh.cleanup_draft(foreign[0].name)
    assert foreign[0].exists()


@pytest.mark.parametrize('current,expected',[(0o700,None),(0o750,0o750),(0o755,0o750)])
def test_required_parent_threeway_metadata_revalidation(tmp_path,current,expected):
    from types import SimpleNamespace
    from native_metadata import Record
    from native_recovery import Directory
    r,w,_=rig(tmp_path);parent=b'homeassistant/p';path=r.root/os.fsdecode(parent);path.chmod(0o755)
    uid,gid=os.geteuid(),os.getegid();before=Record(parent,'directory',uid,gid,0o755,None);after=Record(parent,'directory',uid,gid,0o750,None)
    # Use actual route qualification and target compiler against real live inode.
    with __import__('native_preview')._route(w.preview.binding,parent,'directory') as (_,routes):pass
    w.preview._observed=SimpleNamespace(routes=routes)
    prepared=SimpleNamespace(targets=((parent,Directory(uid,gid,0o750)),),preimages=(),review=SimpleNamespace(parents=(SimpleNamespace(path=parent,before=before,after=after),),selected=()))
    path.chmod(current)
    if expected is None:
        with pytest.raises(Exception,match='apply-metadata-conflict'):w._current_targets(prepared)
    else:assert w._current_targets(prepared)[parent].mode==expected
    assert path.stat().st_mode&0o7777==current and w.recovery.discover() is None


@pytest.mark.parametrize('kind',['parent-mode','leaf-content','leaf-mode','leaf-aba'])
def test_compiled_manifest_binds_complete_frozen_capture(tmp_path,monkeypatch,kind):
    """Mutate real live objects during capture; no new preimage may be adopted."""
    from test_native_save import reviewed
    from dataclasses import replace
    import native_metadata as metadata
    from native_topology_helpers import METADATA
    r,w,_=rig(tmp_path);parent=b'homeassistant/p';directory=r.root/os.fsdecode(parent);directory.chmod(0o755)
    r.remote({A:b'git\n',B:b'base-b\n'},lambda manifest:metadata.Manifest(tuple(replace(row,mode=0o750) if row.path==parent else row for row in manifest.entries)))
    subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',os.fsdecode(w.transport.endpoint),r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    r.live({A:b'base\n',B:b'base-b\n'});directory.chmod(0o755)
    review=reviewed(r,(A,),parents=(parent,),direction='apply');prepared=w.prepare(review,str(uuid.uuid4()))
    original=w._capture_live;path=r.root/os.fsdecode(A);fired=[]
    def capture(leaf,**kwargs):
        if kind=='leaf-aba' and not fired:
            path.write_bytes(b'new!\n')
            result=original(leaf,**kwargs)
            path.write_bytes(b'base\n');fired.append(True);return result
        result=original(leaf,**kwargs)
        if not fired:
            if kind=='parent-mode':directory.chmod(0o700)
            elif kind=='leaf-mode':path.chmod(0o600)
            else:path.write_bytes(b'new!\n')
            fired.append(True)
        return result
    monkeypatch.setattr(w,'_capture_live',capture)
    with w._transaction(prepared.operation,'worker') as capability:
        with w.preview.apply_context(prepared.review,reviewed_live=False):
            captured={}
            with pytest.raises(Exception,match='capture|policy'):
                targets=w._current_targets(prepared,capability=capability,capture=captured)
                w.recovery.prepare(targets,operation_id=prepared.operation,owner_id=w.operations.owner,
                    apply_authority=prepared,capability=capability,capture=captured)
    assert fired and w.recovery.discover() is None
    assert not (r.root/'data/native-operations/state.json').exists()
    if kind=='parent-mode':assert directory.stat().st_mode&0o7777==0o700
    elif kind=='leaf-mode':assert path.stat().st_mode&0o7777==0o600
    else:assert path.read_bytes()==(b'base\n' if kind=='leaf-aba' else b'new!\n')


def test_replacement_remote_failure_preserves_old_draft_and_offline_reset(tmp_path,monkeypatch):
    r,w,review=rig(tmp_path);prepared=w.prepare(review,str(uuid.uuid4()));directory=r.root/'data/native/apply'/prepared.operation
    before={p.name:p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(w.transport,'advertise',lambda:(_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(OSError):w.prepare(review,str(uuid.uuid4()))
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before
    w.reset();assert not directory.exists()


def retiring_draft_crash_worker(directory,boundary):
    r,w,review=rig(Path(directory));prepared=w.prepare(review,str(uuid.uuid4()))
    (Path(directory)/'retiring.json').write_text(json.dumps({'operation':prepared.operation}))
    if boundary=='intent':w.recovery.fault=lambda phase:os._exit(99) if phase=='draft_retirement_durable' else None
    else:
        original=os.unlink if boundary in ('source','control') else os.rmdir
        def effect(name,*args,**kwargs):
            result=original(name,*args,**kwargs)
            encoded=os.fsencode(name)
            hit=(boundary=='source' and encoded==os.path.basename(w._spools[0].source) or
                 boundary=='control' and encoded==b'admission.json' or
                 boundary=='namespace' and encoded==prepared.operation.encode())
            if hit:os._exit(99)
            return result
        if boundary=='namespace':os.rmdir=effect
        else:os.unlink=effect
    w.reset();raise RuntimeError('retirement crash boundary not reached')


@pytest.mark.parametrize('boundary',['intent','source','control','namespace'])
def test_draft_retirement_crash_reopen_preserves_exact_cleanup_authority(tmp_path,boundary):
    script='import sys;sys.path[:0]=sys.argv[1:3];from test_native_apply_integration import retiring_draft_crash_worker;retiring_draft_crash_worker(sys.argv[3],sys.argv[4])'
    child=subprocess.run([sys.executable,'-c',script,str(Path(__file__).resolve().parents[1]/'app'),str(Path(__file__).parent),str(tmp_path),boundary],capture_output=True,timeout=40)
    assert child.returncode==99,(child.stdout+child.stderr).decode()
    operation=json.loads((tmp_path/'retiring.json').read_text())['operation'];fresh=fresh_cleanup_worker(tmp_path)
    assert fresh.recovery._cleanup_draft(operation)['phase']=='retiring'
    fresh.cleanup_draft(operation);fresh.cleanup_draft(operation)
    assert fresh.cleanup_drafts()==[] and not (tmp_path/'live/data/native/apply'/operation).exists()
    assert not (tmp_path/'live/data/native-operations/state.json').exists()


@pytest.mark.parametrize('point_close',[False,True])
@pytest.mark.parametrize('receipt',['missing','malformed'])
def test_terminal_close_replay_precedes_receipt_and_new_draft_lease(tmp_path,monkeypatch,point_close,receipt):
    from test_native_save import reviewed
    r,w,review=rig(tmp_path);adapter=Lifecycle();prepared=w.prepare(review,str(uuid.uuid4()))
    if point_close:
        adapter.validate_core=lambda **kwargs:{'valid':False}
        with browser_receipt(w,prepared,tmp_path):
            with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
            result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    else:
        original=w.recovery.install
        monkeypatch.setattr(w.recovery,'install',lambda *a,**k:(_ for _ in ()).throw(OSError(5,'fixture before installation')))
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
        monkeypatch.setattr(w.recovery,'install',original)
        assert result['no_effect'] is True
    assert result['status']=='ERROR'
    command=str(uuid.uuid4());closed=w.close_recovery(w.review_cleanup(prepared.operation,command))
    path=r.root/'data/native/recovery/closed.json'
    if path.exists():path.unlink()
    if receipt=='malformed':path.write_bytes(b'not a close receipt')
    assert w.resolve_closed_recovery(command)==closed
    review=reviewed(r,(A,),direction='apply');draft=w.prepare(review,str(uuid.uuid4()))
    directory=r.root/'data/native/apply'/draft.operation
    before={p.name:p.read_bytes() for p in directory.iterdir()}
    with w._transaction(draft.operation,'worker'):
        assert w.resolve_closed_recovery(command)==closed
    assert {p.name:p.read_bytes() for p in directory.iterdir()}==before
    assert draft.operation not in w.operations.read()['workers']
    w.reset()


def test_install_accepts_unrelated_absolute_ancestor_directory_churn(tmp_path,monkeypatch):
    """Real qualified Linux worker, at generation creation before first blob ACK."""
    import threading
    owner=threading.get_ident()
    r,w,review=rig(tmp_path);adapter=Lifecycle();prepared=w.prepare(review,str(uuid.uuid4()))
    bound=(r.root.stat().st_dev,r.root.stat().st_ino);selected=r.root/os.fsdecode(A)
    parent=os.stat(tmp_path.parent);armed=[];fired=[];diagnostics=[];original_open=os.open;original_install=w.recovery.install
    def fault(phase):
        if phase=='generation_created':armed.append(True)
    w.recovery.fault=fault
    def opening(path,flags,*args,**kwargs):
        parent_fd=kwargs.get('dir_fd')
        if (threading.get_ident()==owner and armed and not fired and parent_fd is not None and os.fsencode(path)==os.fsencode(tmp_path.name) and
                (os.fstat(parent_fd).st_dev,os.fstat(parent_fd).st_ino)==(parent.st_dev,parent.st_ino)):
            fired.append(True);(tmp_path/'unrelated-install-child').mkdir()
        return original_open(path,flags,*args,**kwargs)
    def install(*args,**kwargs):
        try:return original_install(*args,**kwargs)
        except Exception as error:
            diagnostics.append((type(error).__name__,str(error)));raise
    monkeypatch.setattr(os,'open',opening);monkeypatch.setattr(w.recovery,'install',install)
    with browser_receipt(w,prepared,tmp_path):
        with pytest.raises(Exception):w.run(prepared,adapter,lambda:{'backups':[]})
        result=w.run(prepared,adapter,lambda:{'backups':[]},bypass_backup=True)
    assert fired and not diagnostics,(result,diagnostics)
    assert result['status']=='SUCCESS',result
    assert (r.root.stat().st_dev,r.root.stat().st_ino)==bound and selected.read_bytes()==b'git\n'
    assert any(call[0]=='stop' for call in adapter.calls) and w.recovery.discover() is None
    assert (tmp_path/'unrelated-install-child').is_dir()
