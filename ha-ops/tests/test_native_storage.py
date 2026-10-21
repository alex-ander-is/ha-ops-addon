"""Calculate and genuine reviewed Compact, in disposable local Linux storage."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import uuid
from unittest import mock
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'app')]
import native_disk as d
import native_git as g
import native_operations as o
from native_storage import NativeStorage


def git(root,*args,input=None):
    env=os.environ.copy();env.update(GIT_AUTHOR_NAME='Fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid')
    return subprocess.check_output(['git','--git-dir='+str(root/'data/native/repository.git'),*args],input=input,env=env)


def make_storage(tmp_path,object_format='sha1'):
    root=Path(os.path.realpath(tmp_path));(root/'data/native').mkdir(parents=True)
    subprocess.run(['git','init','--bare','--object-format='+object_format,'--initial-branch=fixture',str(root/'data/native/repository.git')],check=True,capture_output=True)
    operations=o.NativeOperationStore(root/'data');operations.read()
    adapter=g.NativeGit(root)
    with adapter._journal(create=True) as journal:adapter._publish(journal)
    auth=root/'data/direct-auth';auth.mkdir(mode=0o700);(auth/'PRIVATE-CANARY').write_bytes(b'PRIVATE-TOKEN-CANARY')
    return root,NativeStorage(d.NativeDisk(root),adapter,operations)


@pytest.fixture
def storage(tmp_path):
    if sys.platform!="linux":pytest.skip("requires qualified Linux ext4/tmpfs storage fixture; host skips are not proof")
    return make_storage(tmp_path)


def test_calculate_persistence_one_snapshot_and_lifecycle(storage):
    root,s=storage
    with mock.patch.object(s.disk,'snapshot',wraps=s.disk.snapshot) as acquisition:
        view=s.calculate()
        assert acquisition.call_count==1
    report=view['measurement'];assert report['complete'];assert report['pending']['status']=='none'
    assert next(row for row in report['categories'] if row['category']=='authorization')['logical_bytes']>0
    assert b'PRIVATE' not in json.dumps(view).encode()
    for reason in ('refresh','restart','update','disconnect'):
        with mock.patch.object(s.disk,'snapshot',side_effect=AssertionError('status scanned')):
            s.lifecycle(reason);assert s.status()['measurement']==report
            fresh=NativeStorage(s.disk,s.git,s.operations);assert fresh.status()['measurement']==report
    assert not any(key in json.dumps(view) for key in ('review_id','nonce','confirmation'))


def test_calculate_failure_retains_measurement(storage):
    root,s=storage;s.calculate();before=s.status()['measurement']
    with mock.patch.object(s.disk,'snapshot',side_effect=OSError('injected')):
        with pytest.raises(OSError):s.calculate()
    assert s.status()['measurement']==before
    assert s.status()['attempt']['status']=='uncertain'


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='Linux guardian/allocation proof')
def test_real_compact_retains_every_object_and_exact_cleanup(storage):
    root,s=storage
    tree=git(root,'mktree',input=b'').strip()
    commit=git(root,'commit-tree',tree.decode(),input=b'fixture commit\n').strip()
    git(root,'update-ref','refs/heads/fixture',commit.decode())
    loose=git(root,'hash-object','-w','--stdin',input=b'unreachable loose payload').strip()
    packed=git(root,'hash-object','-w','--stdin',input=b'unreachable packed payload').strip()
    # Keep the first unreachable object in a pack before creating later loose data.
    git(root,'-c','pack.writeReverseIndex=false','repack','-a','-d','--keep-unreachable','--no-write-bitmap-index','--no-write-midx','-n')
    git(root,'hash-object','-w','--stdin',input=os.urandom(50000))
    artifact=b'obsolete';key=hashlib.sha256(artifact).hexdigest()
    path=root/'data/native-operations/artifacts'/f'{key}.diff';path.write_bytes(artifact);path.chmod(0o600)
    foreign=path.parent/'.tmp-foreign';foreign.write_bytes(b'leave')
    s.calculate();timestamp=s.status()['measurement']['completed_at']
    with mock.patch.object(s.disk,'snapshot',wraps=s.disk.snapshot) as acquisition:
        preview=s.preview();assert acquisition.call_count==1
    assert preview['review']['available'],preview['review']['forecast']
    before=git(root,'cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype) %(objectsize)')
    with mock.patch.object(s.disk,'snapshot',side_effect=AssertionError('execution scanned')),mock.patch.object(s.disk,'project',side_effect=AssertionError('execution projected')),mock.patch.object(s.disk,'_objects',side_effect=AssertionError('execution enumerated')):
        result=s.confirm(preview['review']['id'])
    assert result['maintenance']['status']=='completed'
    assert not path.exists() and foreign.read_bytes()==b'leave'
    assert (root/'data/direct-auth/PRIVATE-CANARY').read_bytes()==b'PRIVATE-TOKEN-CANARY'
    assert git(root,'cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype) %(objectsize)')==before
    assert list((root/'data/native/repository.git/objects/pack').glob('*.pack'))
    assert result['measurement']['completed_at']==timestamp
    with pytest.raises(d.Blocked):s.confirm(preview['review']['id'])


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='Linux maintenance proof')
@pytest.mark.parametrize('changed',['state','index','config','object','artifact','lifecycle'])
def test_stale_review_refuses_without_effect(storage,changed):
    root,s=storage
    artifact=b'obsolete';key=hashlib.sha256(artifact).hexdigest();path=root/'data/native-operations/artifacts'/f'{key}.diff'
    path.write_bytes(artifact);path.chmod(0o600)
    preview=s.preview();review=preview['review']['id']
    if changed=='state':
        state_path=root/'data/native-operations/state.json'
        state=json.loads(state_path.read_bytes());state['revision']+=1;state_path.write_bytes(o.canonical(state))
    elif changed=='index':(root/'data/native/repository.git/index').write_bytes(b'foreign')
    elif changed=='config':
        with (root/'data/native/repository.git/config').open('ab') as f:f.write(b'\n# changed\n')
    elif changed=='object':git(root,'hash-object','-w','--stdin',input=b'new object')
    elif changed=='artifact':path.write_bytes(b'foreign!')
    else:s.lifecycle('refresh')
    with mock.patch.object(s.git,'compact',side_effect=AssertionError('stale effect')):
        with pytest.raises((ValueError,OSError,RuntimeError)):s.confirm(review)
    assert path.exists()


def test_warning_boundary():
    base=d.Volume((1,),(),d.WARNING_BYTES,100,4096,b'tmpfs',True)
    assert not base.warning
    from dataclasses import replace
    assert replace(base,available=d.WARNING_BYTES-1).warning


@pytest.mark.skipif(sys.platform!='linux',reason='original Linux maintenance')
def test_sha256_compact_retains_every_object(tmp_path):
    test_real_compact_retains_every_object_and_exact_cleanup(make_storage(tmp_path,'sha256'))


@pytest.mark.skipif(sys.platform!='linux',reason='real no-ref maintenance')
def test_no_refs_no_savings_repeated_explicit_compact(storage):
    root,s=storage
    loose=git(root,'hash-object','-w','--stdin',input=b'pinned orphan').strip()
    for number in range(2):
        view=s.preview();assert view['review']['savings']['artifact_allocated_bytes']==0
        result=s.confirm(view['review']['id']);assert result['maintenance']['status']=='completed'
        assert git(root,'cat-file','-p',loose.decode())==b'pinned orphan'


def test_diagnostic_rejects_authority_fields_and_auth_canaries(storage):
    root,s=storage;s.calculate();path=root/'data/native/storage-status.json'
    value=json.loads(path.read_bytes());value['measurement']['nonce']='PRIVATE-CANARY'
    path.write_bytes(o.canonical(value))
    assert s.status()['measurement'] is None
    assert 'PRIVATE-CANARY' not in json.dumps(s.status())


@pytest.mark.parametrize('stage',['diagnostic_opened','diagnostic_written','diagnostic_synced','diagnostic_replaced','diagnostic_durable'])
def test_failed_calculate_persists_attempt_and_prior_measurement_after_restart(storage,stage):
    root,s=storage;s.calculate();before=s.status()['measurement'];hit=[]
    def fault(value):
        if value==stage and not hit:hit.append(1);raise OSError('injected')
    s.fault=fault
    with pytest.raises(OSError):s.calculate()
    fresh=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert fresh.status()['measurement']==before
    assert fresh.status()['attempt']['status']=='failed'


def test_incomplete_calculate_persists_attempt_without_another_snapshot(storage):
    root,s=storage;s.calculate();before=s.status()['measurement']
    os.symlink('/does-not-exist',root/'data/direct-auth/PRIVATE-BROKEN')
    with mock.patch.object(s.disk,'snapshot',wraps=s.disk.snapshot) as calls:
        value=s.calculate();assert calls.call_count==1
    assert value['attempt']['status']=='incomplete'
    fresh=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert fresh.status()['measurement']==before
    assert fresh.status()['attempt']['status']=='incomplete'
    assert 'PRIVATE' not in json.dumps(fresh.status())


def test_many_lifetimes_remain_usable_and_old_confirm_never_adopts(storage):
    root,s=storage;lifetimes=[]
    for number in range(140):
        lifetime=str(uuid.uuid4());lifetimes.append(lifetime)
        response=s.request(dict(action='cancel',lifetime=lifetime,generation=1))
        assert response['qualified']
    current=lifetimes[-1];view=s.request(dict(action='preview',lifetime=current,generation=2));review=view['review']['id']
    with pytest.raises(d.Blocked):s.request(dict(action='confirm',lifetime=lifetimes[0],generation=3,review_id=review))
    s.request(dict(action='cancel',lifetime=current,generation=3))
    assert s.request(dict(action='calculate',lifetime=str(uuid.uuid4()),generation=1))['measurement']


@pytest.mark.skipif(sys.platform!='linux',reason='real protected graph maintenance')
def test_healthy_retained_recovery_graph_artifact_survives(storage):
    from test_native_operations import seed
    root,s=storage;record=seed(s.operations)
    state=s.operations.read();s.operations.recovery_pin(state['revision'],None,record['id'],record['identity_digest'])
    path=root/'data/native-operations/artifacts'/(record['artifact']+'.diff');before=path.read_bytes()
    preview=s.preview();result=s.confirm(preview['review']['id'])
    assert result['maintenance']['status']=='completed'
    assert path.read_bytes()==before
    assert s.operations.read()['recovery']['record_id']==record['id']



def test_qualified_pending_forecast_and_reconstructed_unknown(storage):
    from dataclasses import replace
    root,s=storage;operation=str(uuid.uuid4())
    pending=s.disk.project('repack',s.disk.snapshot(),d.Evidence(operation,s.operations.owner,controls=s._controls(operation)))
    result=s.calculate(pending=pending)['measurement']['pending']
    assert result['status']=='known' and result['volumes']
    assert all('additional_peak' in row and 'required_inodes' in row for row in result['volumes'])
    assert s.calculate(pending=replace(pending))['measurement']['pending']['status']=='unknown'


@pytest.mark.skipif(sys.platform!='linux',reason='real Compact output fault proof')
@pytest.mark.parametrize('target',['config','index','graph','artifact'])
def test_foreign_changes_beside_own_git_effects_are_uncertain(storage,target):
    root,s=storage;content=b'obsolete';key=hashlib.sha256(content).hexdigest();artifact=root/'data/native-operations/artifacts'/(key+'.diff')
    artifact.write_bytes(content);artifact.chmod(0o600);preview=s.preview()
    execute=s.git.compact
    def interleave(capability):
        result=execute(capability)
        if target=='config':
            with (root/'data/native/repository.git/config').open('ab') as file:file.write(b'\n# foreign\n')
        elif target=='index':(root/'data/native/repository.git/index').write_bytes(b'foreign')
        elif target=='graph':
            path=root/'data/native-operations/state.json';value=json.loads(path.read_bytes());value['revision']+=1;path.write_bytes(o.canonical(value))
        else:artifact.write_bytes(b'foreign!')
        return result
    with mock.patch.object(s.git,'compact',side_effect=interleave):
        with pytest.raises((ValueError,RuntimeError)):s.confirm(preview['review']['id'])
    assert artifact.exists() and s.status()['maintenance']['status']=='uncertain'


@pytest.mark.parametrize('corruption',['category-type','maintenance-type','duplicate','deep'])
def test_malformed_bounded_diagnostic_is_explicit_unavailable(storage,corruption):
    root,s=storage;s.calculate();path=root/'data/native/storage-status.json';value=json.loads(path.read_bytes())
    if corruption=='category-type':value['measurement']['categories'][0]['category']=[];raw=json.dumps(value).encode()
    elif corruption=='maintenance-type':value['maintenance']=[];raw=json.dumps(value).encode()
    elif corruption=='duplicate':raw=b'{"schema":1,"schema":1}'
    else:raw=b'['*5000+b'0'+b']'*5000
    path.write_bytes(raw)
    assert s.status()['attempt']['status']=='unavailable'
# Append these regression cases to ha-ops/tests/test_native_storage.py.
# Existing imports and the storage fixture in that file are reused.

@pytest.mark.skipif(sys.platform != 'linux', reason='real Compact receipt proof')
@pytest.mark.parametrize('observe_status', [False, True])
def test_status_read_cannot_adopt_foreign_active_diagnostic(storage, observe_status):
    root, s = storage
    review = s.preview()['review']['id']
    replacements = []
    def fault(stage):
        if stage == 'diagnostic_durable' and not replacements:
            target = root / 'data/native/storage-status.json'
            value = json.loads(target.read_bytes())
            value['maintenance']['message'] = 'foreign replacement'
            raw = o.canonical(value)
            alternate = target.with_name('foreign-replacement')
            alternate.write_bytes(raw)
            alternate.chmod(0o600)
            alternate.replace(target)
            replacements.append(raw)
            if observe_status:
                s.status()
    s.fault = fault
    with pytest.raises(d.Blocked):
        s.confirm(review)
    assert (root / 'data/native/storage-status.json').read_bytes() == replacements[0]


@pytest.mark.skipif(sys.platform != 'linux', reason='real Compact parent-route proof')
def test_compact_refuses_displaced_artifact_directory_after_git(storage):
    root, s = storage
    content = b'owned obsolete'
    key = hashlib.sha256(content).hexdigest()
    artifacts = root / 'data/native-operations/artifacts'
    target = artifacts / (key + '.diff')
    target.write_bytes(content)
    target.chmod(0o600)
    review = s.preview()['review']['id']
    execute = s.git.compact
    displaced = artifacts.with_name('displaced-artifacts')
    def interleave(capability):
        result = execute(capability)
        artifacts.rename(displaced)
        artifacts.mkdir(mode=0o700)
        (artifacts / 'FOREIGN').write_bytes(b'preserve')
        return result
    s.git.compact = interleave
    with pytest.raises((d.Blocked, ValueError, OSError)):
        s.confirm(review)
    assert (displaced / target.name).read_bytes() == content
    assert (artifacts / 'FOREIGN').read_bytes() == b'preserve'
    assert s.status()['maintenance']['status'] == 'uncertain'



@pytest.mark.skipif(sys.platform!='linux',reason='qualified preallocated marker')
@pytest.mark.parametrize('cause',['snapshot','held-disk','partial'])
def test_pre_acquisition_failure_is_durable_uncertainty_without_rescan(storage,cause):
    root,s=storage;s.calculate();before=s.status()['measurement'];calls=[]
    if cause=='snapshot':
        with mock.patch.object(s.disk,'snapshot',side_effect=OSError('acquisition')) as acquisition:
            with pytest.raises(OSError):s.calculate()
            assert acquisition.call_count==1
    elif cause=='held-disk':
        operation=str(uuid.uuid4());plan=s.disk.project('repack',s.disk.snapshot(),d.Evidence(operation,s.operations.owner,controls=s._controls(operation)))
        with s.disk.guard(plan):
            with pytest.raises(d.Blocked):s.calculate()
    else:
        original=os.pwrite
        def short(fd,data,offset):return original(fd,data[:1],offset)
        def fail(stage):
            if stage=='attempt_marker_written':raise OSError('partial durable marker')
        s.fault=fail
        with mock.patch.object(os,'pwrite',side_effect=short),mock.patch.object(s.disk,'snapshot',side_effect=AssertionError('must not scan')):
            with pytest.raises(OSError):s.calculate()
    fresh=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert fresh.status()['measurement']==before
    assert fresh.status()['attempt']['status']=='uncertain'


@pytest.mark.skipif(sys.platform!='linux',reason='real route interleavings')
@pytest.mark.parametrize('extension',['diff','blob'])
@pytest.mark.parametrize('ancestor',['artifacts','native-operations'])
@pytest.mark.parametrize('at_hook',[False,True])
def test_cleanup_routes_after_git_and_unlink_hooks(storage,extension,ancestor,at_hook):
    root,s=storage;content=b'obsolete exact owner';key=hashlib.sha256(content).hexdigest()
    artifacts=root/'data/native-operations/artifacts';target=artifacts/(key+'.'+extension)
    target.write_bytes(content);target.chmod(0o600);review=s.preview()['review']['id'];effect=[];moved=[]
    parent=artifacts if ancestor=='artifacts' else artifacts.parent
    displaced=root/('displaced-'+ancestor)
    def replace():
        if moved:return
        parent.rename(displaced);parent.mkdir(mode=0o700)
        if ancestor=='native-operations':(parent/'artifacts').mkdir(mode=0o700)
        (parent/'FOREIGN').write_bytes(b'preserve');moved.append(1)
    execute=s.git.compact
    def run(capability):
        result=execute(capability);effect.append(1)
        if not at_hook:replace()
        return result
    def hook(stage):
        if stage=='compact_before_unlink' and effect and at_hook:replace()
    s.git.compact=run;s.operations.fault=hook
    with pytest.raises((ValueError,OSError,RuntimeError)):s.confirm(review)
    retained=displaced/target.name if ancestor=='artifacts' else displaced/'artifacts'/target.name
    assert retained.read_bytes()==content and (parent/'FOREIGN').read_bytes()==b'preserve'
    assert s.status()['maintenance']['status']=='uncertain'


@pytest.mark.skipif(sys.platform!='linux',reason='actual partial marker process death')
def test_partial_attempt_marker_process_death_preserves_measurement(storage):
    root,s=storage;s.calculate();before=s.status()['measurement']
    code="""import os,sys
from pathlib import Path
sys.path[:0]=sys.argv[2:]
import native_disk,native_git,native_operations
from native_storage import NativeStorage
root=Path(sys.argv[1]);owner=NativeStorage(native_disk.NativeDisk(root),native_git.NativeGit(root),native_operations.NativeOperationStore(root/'data'))
write=os.pwrite
os.pwrite=lambda fd,data,offset:write(fd,data[:1],offset)
def fault(stage):
    if stage=='attempt_marker_written':os._exit(73)
owner.fault=fault;owner.calculate()
"""
    result=subprocess.run([sys.executable,'-c',code,str(root),str(Path(d.__file__).parent),str(Path(__file__).parent)],timeout=10)
    assert result.returncode==73
    fresh=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert fresh.status()['measurement']==before and fresh.status()['attempt']['status']=='uncertain'


@pytest.mark.parametrize('maintenance_fails',[False,True])
def test_unrelated_compact_never_settles_calculate_fact(storage,maintenance_fails):
    root,s=storage;s.calculate();before=s.status()['measurement']
    with mock.patch.object(s.disk,'snapshot',side_effect=OSError('acquisition')):
        with pytest.raises(OSError):s.calculate()
    owner=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert owner.status()['attempt']['status']=='uncertain'
    review=owner.preview()['review']['id']
    if maintenance_fails:
        with mock.patch.object(owner.git,'compact',side_effect=RuntimeError('maintenance')):
            with pytest.raises(RuntimeError):owner.confirm(review)
    else:owner.confirm(review)
    fresh=NativeStorage(d.NativeDisk(root),s.git,s.operations)
    assert fresh.status()['measurement']==before and fresh.status()['attempt']['status']=='uncertain'
    fresh.status();fresh.calculate()
    assert NativeStorage(d.NativeDisk(root),s.git,s.operations).status()['attempt']['status']=='completed'


@pytest.mark.parametrize('outcome',['success','incomplete','failure'])
@pytest.mark.parametrize('clock',[100.0,1.0])
def test_canonical_attempt_is_authoritative_across_coordinators(storage,outcome,clock):
    import native_storage
    root,old=storage
    with mock.patch.object(native_storage.time,'time',return_value=100.0),mock.patch.object(native_storage.time,'time_ns',return_value=100):old.calculate()
    with mock.patch.object(old.disk,'snapshot',side_effect=OSError('old acquisition')):
        with pytest.raises(OSError):old.calculate()
    current=NativeStorage(d.NativeDisk(root),old.git,old.operations)
    with mock.patch.object(native_storage.time,'time',return_value=clock),mock.patch.object(native_storage.time,'time_ns',return_value=100):
        if outcome=='success':current.calculate()
        elif outcome=='incomplete':
            os.symlink('/unavailable',root/'data/direct-auth/PRIVATE-BROKEN');current.calculate()
        else:
            with mock.patch.object(current.disk,'snapshot',side_effect=OSError('new acquisition')):
                with pytest.raises(OSError):current.calculate()
    expected=current.status()
    for reason in ('refresh','restart','update'):
        old.lifecycle(reason)
        for owner in (old,current,NativeStorage(d.NativeDisk(root),old.git,old.operations)):
            view=owner.status();assert view['attempt']==expected['attempt'] and view['measurement']==expected['measurement']
            assert owner.status()['attempt']==expected['attempt']
    if outcome!='incomplete':
        for fails in (False,True):
            review=current.preview()['review']['id']
            if fails:
                with mock.patch.object(current.git,'compact',side_effect=RuntimeError('maintenance')):
                    with pytest.raises(RuntimeError):current.confirm(review)
            else:current.confirm(review)
            assert old.status()['attempt']==current.status()['attempt']==expected['attempt']


def test_foreign_or_unavailable_generation_never_adopts_old_error(storage):
    root,old=storage;old.calculate()
    with mock.patch.object(old.disk,'snapshot',side_effect=OSError('old error')):
        with pytest.raises(OSError):old.calculate()
    current=NativeStorage(d.NativeDisk(root),old.git,old.operations);current.calculate()
    path=root/'data/native/storage-status.json';raw=path.read_bytes()
    replacement=path.with_name('foreign-generation');replacement.write_bytes(raw);replacement.chmod(0o600);replacement.replace(path)
    assert old.status()['attempt']==current.status()['attempt']
    path.write_bytes(b'{"schema":99}')
    assert old.status()['attempt']==current.status()['attempt'] and old.status()['attempt']['status']=='unavailable'
    path.unlink()
    assert old.status()['attempt'] is None and current.status()['attempt'] is None
    with mock.patch.object(old.disk,'snapshot',side_effect=OSError('no preallocation')):
        with pytest.raises(OSError) as failure:old.calculate()
    assert any('durable attempt diagnostic unavailable' in item for item in failure.value.cleanup_errors)
    assert old.status()['attempt'] is None
    with mock.patch.object(old,'_mark_attempt',side_effect=d.Blocked('unsupported allocator')):
        with pytest.raises(d.Blocked):old.calculate()
    assert old.status()['attempt'] is None
