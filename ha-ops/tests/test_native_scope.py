"""Dormant scope API uses only disposable native Git/live fixtures."""
import ast
from dataclasses import replace
import io
import os
from pathlib import Path
import sys
import uuid
from types import SimpleNamespace

import pytest
from network_publication_helpers import receiver_control
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_scope as scope
import native_save as save
import native_publication as pub
import native_disk as disk
import native_operations as operations
import native_metadata as metadata
from test_native_save import SaveRig,reviewed,put,A,B,canonical,content
from test_native_preview import tree_evidence


def content_ref(data):
    sink=scope.ContentSink(len(data),'sha1');sink.write(data);return sink.reference()


def review_bytes(owner,section,path,**kwargs):
    sink=io.BytesIO();owner.copy_review_content(section,path,sink,**kwargs);return sink.getvalue()


def fixture(tmp_path,monkeypatch=None,retention=32):
    rig=SaveRig(tmp_path)
    saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    store=operations.NativeOperationStore(rig.root/'data',retention=retention)
    if monkeypatch is None:
        # A real issued Save publication establishes the native baseline.
        put(rig.root,A,b'initial publication')
        review=reviewed(rig,(A,));candidate=saver.build(review)
        endpoint=tmp_path/'receiver.git'
        __import__('subprocess').run(['git','init','--bare',str(endpoint)],check=True,capture_output=True)
        __import__('subprocess').run(['git','--git-dir='+str(rig.root/'data/native/repository.git'),
            'push',str(endpoint),rig.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
        transport=pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')
        executor=pub.NativePublication(saver,store,transport)
        sig=pub.Signature('Scope Fixture','fixture@invalid',1700000000,'+0000')
        result=executor.publish(candidate,review,author=sig,committer=sig,message=b'establish fixture baseline\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
        oid=result['commit_oid']
        __import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
        rig.source=replace(rig.source,remote_oid=oid,base_oid=oid)
    else:
        # Portable compiler-only proof uses the store's declared-observation
        # contract under a test-only native codec. It cannot publish anything.
        monkeypatch.setitem(operations._NATIVE_PROFILES,'native-fixture-observation','native-bytes-v1')
        observed=rig.preview._observe_once(rig.source,rig.width)
        selected=tuple(__import__('native_preview').PathChange(path,None,state,None,'added',True,'none')
                       for path,state in observed.live)
        descriptor=SimpleNamespace(routes=observed.routes,selected=selected,
            parent_dependencies=(),remote=rig.source,binding=rig.binding,scope_review=None,active_policy=None)
        identity=pub._candidate_identity(descriptor,rig.source.remote_oid,b'/fixture',
            'refs/heads/main',rig.source.base_ref,rig.source.base_oid)
        identity['execution_policy']='native-fixture-observation'
        store.preview(0,'save',identity,b'actual complete initial source observation')
    put(rig.root,A,b'initial reviewed divergence')
    return rig,saver,store,scope.NativeScope(saver,store)


def fresh(tmp_path):
    rig=SaveRig(tmp_path)
    saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    store=operations.NativeOperationStore(rig.root/'data')
    return rig,saver,store,scope.NativeScope(saver,store)


def test_constructor_and_diagnostic_are_inert(tmp_path):
    rig,saver,store,owner=fresh(tmp_path)
    result=owner.diagnose()
    assert result['active_policy']['rules']==[] and not result['apply_blocked']
    assert result['draft']=={} and result['selected_files']==[] and result['consent'] is None
    assert result['published_policy'] is None
    assert not (rig.root/'data/native-operations').exists()


def test_discovery_longest_prefix_and_canonical_rules():
    encoded=operations.encode_native_path
    rules=((b'homeassistant',True),(b'homeassistant/B',False),(b'homeassistant/B/C',True))
    policy=save.DiscoveryPolicy(4,rules,'a'*64).document()
    assert operations.discovery_includes(policy,encoded(b'homeassistant/direct.yaml'))
    assert not operations.discovery_includes(policy,encoded(b'homeassistant/B/hidden.yaml'))
    assert operations.discovery_includes(policy,encoded(b'homeassistant/B/C/restored.yaml'))
    for bad in (((b'homeassistant',False),),((b'homeassistant',True),(b'homeassistant/B',True)),tuple(reversed(rules))):
        with pytest.raises(Exception):save.DiscoveryPolicy(1,bad,'a'*64)


def test_capacity_projection_precedes_first_canonical_write(tmp_path,monkeypatch):
    rig,saver,store,owner=fresh(tmp_path);before=canonical(rig)
    observed=[]
    class Stop(Exception):pass
    def project(kind,snapshot,evidence):
        assert kind=='scope_control'
        prepared=evidence.scope_preparation;prepared.check()
        assert prepared.initial_bytes is None and prepared.payloads==() and prepared.artifact==b''
        state=operations._parse(prepared.state)
        assert state['active_scope'] is None and state['baseline_provenance'] is None and state['records']=={}
        assert state['discovery']['effective']['rules']
        observed.append(prepared);raise Stop()
    monkeypatch.setattr(saver.disk,'project',project)
    with pytest.raises(Stop):owner.build((b'homeassistant',),expected_revision=0)
    assert observed and canonical(rig)==before
    assert not (rig.root/'data/native-operations').exists()


def test_prohibited_or_redundant_scopes_never_write(tmp_path):
    rig,saver,store,owner=fresh(tmp_path)
    for paths in ((b'data',),(b'homeassistant/secrets.yaml',),(b'homeassistant/.storage/auth',),(b'../homeassistant',),(b'homeassistant',b'homeassistant/p')):
        with pytest.raises(Exception):owner.build(paths,expected_revision=0)
    assert not (rig.root/'data/native-operations').exists()


@pytest.mark.skipif(sys.platform!='linux',reason='actual Linux scope-control admission')
class TestDiscovery:
    def test_include_exclude_reinclude_only_changes_policy_and_survives_reopen(self,tmp_path):
        rig,saver,store,owner=fresh(tmp_path)
        put(rig.root,A,b'divergent current HA');put(rig.root,b'homeassistant/secrets.yaml',b'excluded fixture secret')
        from native_topology_helpers import git
        put(rig.root,b'homeassistant/foreign',b'foreign staged')
        git(rig.root,'add','--','homeassistant/foreign')
        index=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
        for paths in ((b'homeassistant',),(),(b'homeassistant',)):
            result=owner.build(paths,expected_revision=owner.diagnose()['revision'])
            assert not result['apply_blocked'] and result['selected_files']==[] and result['consent'] is None
            assert canonical(rig)==index and tree_evidence(rig.root/'homeassistant')==live
            state=store.read();assert state['active_scope'] is None and state['records']=={} and state['workers']=={}
            assert state['baseline_provenance'] is None
        independent=operations.NativeOperationStore(rig.root/'data')
        other=scope.NativeScope(save.NativeSave(__import__('native_preview').NativePreview(rig.adapter,rig.observe,rig.binding,scratch_root=rig.scratch),disk.NativeDisk(rig.root)),independent)
        assert other.diagnose()['active_policy']==owner.diagnose()['active_policy']
        pending=other.diagnose()['transaction']
        with pytest.raises(operations.Conflict,match='fresh explicit'):other.confirm(expected_revision=other.diagnose()['revision'],review_id=pending['id'])
        old_revision=other.diagnose()['active_policy']['revision']
        result=other.cancel(expected_revision=other.diagnose()['revision'])
        assert result['active_policy']['rules']==[] and result['active_policy']['revision']>old_revision
        assert canonical(rig)==index and tree_evidence(rig.root/'homeassistant')==live

    def test_pending_policy_preview_empty_draft_and_selected_save_keeps_remote_sibling(self,tmp_path):
        rig,saver,store,owner=fresh(tmp_path)
        result=owner.build((b'homeassistant',),expected_revision=0)
        put(rig.root,A,b'only selected HA bytes');put(rig.root,B,b'unselected HA churn')
        snapshot=rig.preview.build('save')
        assert rig.preview.draft.selected==()
        rig.preview.select(snapshot.snapshot_id,(A,));review=rig.preview.review(snapshot.snapshot_id)
        candidate=saver.build(review)
        assert candidate.scope_review is None and candidate.discovery_policy.document()==result['active_policy']
        assert candidate.baseline_policy.allowlist==()
        actual=content(rig,candidate.tree_oid)
        assert actual[A]==b'only selected HA bytes' and actual[B]==b'base-b\n'
        assert not owner.diagnose()['apply_blocked']
        # Revocation at edit and change-back cannot revive the issuer grant.
        owner.build((),expected_revision=owner.diagnose()['revision'])
        owner.build((b'homeassistant',),expected_revision=owner.diagnose()['revision'])
        with pytest.raises(Exception):saver._publication_fence(candidate,review)

    def test_nested_exclusion_and_reinclusion_preserve_parent_direct_files(self,tmp_path):
        rig,saver,store,owner=fresh(tmp_path)
        files={b'homeassistant/direct.yaml':b'direct',b'homeassistant/B/hidden.yaml':b'hidden',b'homeassistant/B/C/restored.yaml':b'restored',b'homeassistant/D/sibling.yaml':b'sibling',b'homeassistant/B/C/secrets.yaml':b'secret'}
        for path,value in files.items():put(rig.root,path,value)
        owner.build(((b'homeassistant',True),(b'homeassistant/B',False),(b'homeassistant/B/C',True)),expected_revision=0)
        snapshot=rig.preview.build('save');paths={row.path for row in snapshot.rows}
        assert b'homeassistant/direct.yaml' in paths and b'homeassistant/B/C/restored.yaml' in paths
        assert b'homeassistant/D/sibling.yaml' in paths
        assert b'homeassistant/B/hidden.yaml' not in paths and b'homeassistant/B/C/secrets.yaml' not in paths
        assert rig.preview.draft.selected==()

    def test_confirm_and_lifecycle_do_not_resurrect_approval(self,tmp_path):
        rig,saver,store,owner=fresh(tmp_path)
        result=owner.build((b'homeassistant',),expected_revision=0)
        confirmed=owner.confirm(expected_revision=result['revision'],review_id=result['transaction']['id'])
        assert confirmed['transaction'] is None
        assert confirmed['active_policy']==confirmed['confirmed_policy']
        result=owner.build((),expected_revision=confirmed['revision'])
        owner.lifecycle('update')
        assert owner.diagnose()['transaction']==result['transaction']
        with pytest.raises(operations.Conflict):owner.confirm(expected_revision=result['revision'],review_id=result['transaction']['id'])
        cancelled=owner.cancel(expected_revision=result['revision'])
        assert cancelled['active_policy']['rules']==confirmed['active_policy']['rules']
        with pytest.raises(Exception):owner.build((),expected_revision=0)



@pytest.mark.skipif(sys.platform!='linux',reason='actual Linux discovery/publication admission')
@pytest.mark.parametrize('established',[False,True])
def test_selected_save_publishes_under_pending_discovery_and_preserves_baseline(tmp_path,established):
    import subprocess
    rig,saver,store,owner=fixture(tmp_path) if established else fresh(tmp_path)
    original_baseline=store.read()['baseline_provenance'] if established else None
    endpoint=tmp_path/'receiver.git'
    if not endpoint.exists():
        subprocess.run(['git','init','--bare',str(endpoint)],check=True,capture_output=True)
        subprocess.run(['git','--git-dir='+str(rig.root/'data/native/repository.git'),'push',str(endpoint),rig.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    owner.build((b'homeassistant',),expected_revision=owner.diagnose()['revision'])
    put(rig.root,A,b'explicit selected HA');put(rig.root,B,b'unselected HA churn')
    review=reviewed(rig,(A,));candidate=saver.build(review)
    executor=pub.NativePublication(saver,store,pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main'))
    sig=pub.Signature('Discovery Fixture','fixture@invalid',1700000000,'+0000')
    result=executor.publish(candidate,review,author=sig,committer=sig,message=b'one explicitly selected file\n',local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
    assert result['status']=='completed'
    actual=content(rig,result['commit_oid'])
    assert actual[A]==b'explicit selected HA' and actual[B]==b'base-b\n'
    state=store.read();baseline=state['baseline_provenance']
    assert state['active_scope']['allowlist']==([operations.encode_native_path(b'homeassistant')] if established else [])
    if established:assert baseline==original_baseline
    assert state['discovery']['effective']==candidate.discovery_policy.document()
    assert state['discovery']['pending'] is not None
    store.compact(state['revision'])
    reopened=operations.NativeOperationStore(rig.root/'data').read()
    assert reopened['baseline_provenance']==baseline and reopened['active_scope']==state['active_scope']


@pytest.mark.skipif(sys.platform!='linux',reason='real authority store alongside admitted scope')
def test_scope_lifecycle_keeps_actual_remembered_browser_token_store(tmp_path):
    from direct_auth import Authority
    import hashlib
    rig,saver,store,owner=fresh(tmp_path)
    auth=Authority(rig.root/'data');address='http://127.0.0.1:8100'
    grant=auth.issue(auth.nonce(()),(),address).split('#grant=')[1]
    bearer=auth.exchange(grant,address);cookie=auth.cookie(bearer)
    path=auth.directory/'tokens.json';before=hashlib.sha256(path.read_bytes()).hexdigest()
    for reason in ('refresh','restart','update'):
        result=owner.build((b'homeassistant',),expected_revision=owner.diagnose()['revision'])
        owner.lifecycle(reason);owner.cancel(expected_revision=result['revision'])
        other=Authority(rig.root/'data')
        assert other.authenticate(cookie,address)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before


@pytest.mark.skipif(sys.platform!='linux',reason='real scope state atomic fault/reopen')
@pytest.mark.parametrize('point',['state_written','state_fsynced','state_replaced','state_durable'])
def test_discovery_atomic_fault_never_creates_content_authority(tmp_path,point):
    rig,saver,store,owner=fresh(tmp_path)
    def fault(current):
        if current==point:raise KeyboardInterrupt(point)
    store.fault=fault
    with pytest.raises(KeyboardInterrupt):owner.build((b'homeassistant',),expected_revision=0)
    fresh_store=operations.NativeOperationStore(rig.root/'data')
    try:state=fresh_store.read(observe=True)
    except FileNotFoundError:
        assert point in ('state_written','state_fsynced');return
    assert state['records']=={} and state['workers']=={} and state['active_scope'] is None
    assert state['discovery']['effective']['rules'] and state['discovery']['pending']
    assert canonical(rig)

def test_no_production_native_scope_import_or_dispatch():
    app=Path(scope.__file__).parent
    peers={'native_scope.py','native_save.py','native_publication.py'}
    for file in app.glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node,ast.Import):modules=[p for v in node.names for p in v.name.split('.')]
            elif isinstance(node,ast.ImportFrom):modules=(node.module or '').split('.')+[p for v in node.names for p in v.name.split('.')]
            else:continue
            assert 'native_scope' not in modules or file.name in peers,file.name


def test_content_sink_memoryview_binary_flag_and_partial_consumer():
    class Partial:
        data=bytearray()
        def write(self,chunk):self.data.extend(chunk[:1]);return 1
    target=Partial();sink=scope.ContentSink(4,'sha1',target)
    sink.write(memoryview(b'a\0\xffb'))
    assert sink.reference()==content_ref(b'a\0\xffb') and bytes(target.data)==b'a\0\xffb'



def test_unowned_binary_cleanup_needs_digest_identity_and_reports_debt(tmp_path):
    store=operations.NativeOperationStore(tmp_path/'data')
    objects=tmp_path/'objects';objects.mkdir(mode=0o700)
    foreign=objects/('a'*64+'.blob');foreign.write_bytes(b'foreign bytes');foreign.chmod(0o600)
    good=objects/(content_ref(b'owned orphan').key+'.blob');good.write_bytes(b'owned orphan');good.chmod(0o600)
    fd=os.open(objects,os.O_RDONLY|os.O_DIRECTORY)
    try:store._sweep(fd,dict(records={},scope_pending=None))
    finally:os.close(fd)
    assert foreign.exists() and not good.exists()
    assert any('unowned or changed' in error for error in store.cleanup_errors)



def test_binary_sweep_ignores_read_induced_atime_changes(tmp_path):
    store=operations.NativeOperationStore(tmp_path/'data')
    objects=tmp_path/'objects';objects.mkdir(mode=0o700)
    ref=content_ref(b'valid released binary')
    path=objects/(ref.key+'.blob');path.write_bytes(b'valid released binary');path.chmod(0o600)
    os.utime(path,ns=(1,2))
    fd=os.open(objects,os.O_RDONLY|os.O_DIRECTORY)
    try:store._sweep(fd,dict(records={},scope_pending=None))
    finally:os.close(fd)
    assert not path.exists() and store.cleanup_errors==[]


@pytest.mark.parametrize('change',['content','mode','route','link','unlink-failure'])
def test_binary_sweep_retains_actual_mutation_or_unlink_debt(tmp_path,monkeypatch,change):
    store=operations.NativeOperationStore(tmp_path/'data')
    objects=tmp_path/'objects';objects.mkdir(mode=0o700)
    data=b'valid released binary';ref=content_ref(data)
    path=objects/(ref.key+'.blob');path.write_bytes(data);path.chmod(0o600)
    def fault(point):
        if point!='cleanup_before_unlink':return
        if change=='content':path.write_bytes(b'x'*len(data))
        elif change=='mode':path.chmod(0o644)
        elif change=='route':
            other=objects/'foreign';other.write_bytes(data);other.chmod(0o600);other.replace(path)
        elif change=='link':os.link(path,objects/'foreign')
    store.fault=fault
    if change=='unlink-failure':
        original=os.unlink
        def failed(name,*args,**kwargs):
            if name==path.name:raise PermissionError('persistent test-owned unlink failure')
            return original(name,*args,**kwargs)
        monkeypatch.setattr(os,'unlink',failed)
    fd=os.open(objects,os.O_RDONLY|os.O_DIRECTORY)
    try:store._sweep(fd,dict(records={},scope_pending=None))
    finally:os.close(fd)
    assert path.exists() and store.cleanup_errors



def test_binary_sweep_hashes_after_interleaved_same_size_write(tmp_path,monkeypatch):
    store=operations.NativeOperationStore(tmp_path/'data')
    objects=tmp_path/'objects';objects.mkdir(mode=0o700)
    data=b'original binary content';ref=content_ref(data)
    path=objects/(ref.key+'.blob');path.write_bytes(data);path.chmod(0o600)
    # Model a filesystem whose timestamps cannot distinguish this immediate
    # same-size write. Real inode/mode/link/size checks remain effective.
    original_identity=operations._scope_content_identity
    times=original_identity(path.stat())[-2:]
    monkeypatch.setattr(operations,'_scope_content_identity',lambda info:original_identity(info)[:-2]+times)
    seen=[]
    def change(point):
        if point=='cleanup_before_unlink':
            path.write_bytes(b'x'*len(data));seen.append(point)
    store.fault=change
    fd=os.open(objects,os.O_RDONLY|os.O_DIRECTORY)
    try:store._sweep(fd,dict(records={},scope_pending=None))
    finally:os.close(fd)
    assert seen and path.exists()
    assert any('unowned or changed binary cleanup object' in error for error in store.cleanup_errors)


# Historical recovery fixtures deliberately synthesize records produced by the
# retired implementation. Public NativeScope and prepare_scope cannot build these.
class LegacyFixtureScope(scope._LegacyScope):
    build=scope._LegacyScope._retired_build
    def __init__(self,saver,store):
        saver.preview.discovery_store=None
        super().__init__(saver,store)
        store.prepare_scope=lambda *args,**kwargs:operations._retired_prepare_scope(store,*args,**kwargs)


def legacy_fixture(tmp_path,monkeypatch=None,retention=32):
    rig,saver,store,_=fixture(tmp_path,monkeypatch,retention)
    return rig,saver,store,LegacyFixtureScope(saver,store)


@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux candidate and persistence admission')
def test_retained_legacy_lifecycle_gc_reopen_same_candidate_and_cancel(tmp_path):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    identity=(candidate.tree_oid,candidate.candidate_digest,owner.reviewed_diff())
    old=owner.active_policy();rules=owner.active_rules();pending=owner.diagnose()['transaction']
    owner.lifecycle('update')
    __import__('native_topology_helpers').git(rig.root,'gc','--prune=now')
    fresh=LegacyFixtureScope(save.NativeSave(rig.preview,disk.NativeDisk(rig.root)),store)
    reopened,new_review=fresh.reopen_review(expected_revision=store.read()['revision'])
    assert (reopened.tree_oid,reopened.candidate_digest,fresh.reviewed_diff())==identity
    assert fresh.active_policy()==old and fresh.active_rules()==rules
    assert fresh.diagnose()['transaction']['id']==pending['id']
    live=tree_evidence(rig.root/'homeassistant')
    result=fresh.cancel(expected_revision=store.read()['revision'])
    assert result['transaction'] is None and result['active_policy']==old and not result['apply_blocked']
    assert tree_evidence(rig.root/'homeassistant')==live



@pytest.mark.skipif(sys.platform!='linux' or not Path('/workspace/output').is_dir(),reason='Authenticated isolated Linux TLS receiver')
@pytest.mark.parametrize('failure',['none','receiver-reject','lost-ack','accepted-readback-loss'])
@pytest.mark.parametrize('protocol',['https','ssh'])
def test_retained_legacy_scope_authenticated_network_publication_and_same_commit_recovery(tmp_path,monkeypatch,failure,protocol):
    from network_publication_helpers import tls_server,ssh_server,command,isolated_environment
    if protocol=='ssh' and (os.geteuid()!=0 or failure=='accepted-readback-loss'):
        pytest.skip('root SSH fixture; independent graph-loss recovery is qualified through HTTPS')
    import tempfile
    rig,saver,store,owner=legacy_fixture(tmp_path)
    old=owner.active_policy();oldrules=owner.active_rules()
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    endpoint=tmp_path/'receiver.git'
    command(['git','--git-dir='+str(endpoint),'config','http.receivepack','true'],env=isolated_environment())
    control=receiver_control()
    if failure in ('lost-ack','accepted-readback-loss'):control['drop_receive_response']=True
    if failure=='accepted-readback-loss':control['fail_upload_after_receive']=True
    with tempfile.TemporaryDirectory(prefix='scope-receiver-hooks-',dir='/workspace/output') as hooks:
        if failure=='receiver-reject':
            hook=Path(hooks)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o700)
            command(['git','--git-dir='+str(endpoint),'config','core.hooksPath',hooks])
        if protocol=='ssh' and failure=='lost-ack':
            hook=Path(hooks)/'post-receive';hook.write_text('#!/bin/sh\nkill -KILL "$PPID"\n');hook.chmod(0o700)
            command(['git','--git-dir='+str(endpoint),'config','core.hooksPath',hooks])
        with (ssh_server(tmp_path) if protocol=='ssh' else tls_server(tmp_path,control)) as descriptor:
            url=descriptor[0];options=dict(repo_url=url,repo_branch='main')
            if protocol=='https':
                options['repo_url']=url.replace('https://','https://fixture:fixture-password@')
                transport=pub.NetworkGitCLITransport(rig.adapter,options,Path('unused'),Path('unused'),ca_file=descriptor[1])
            else:
                transport=pub.NetworkGitCLITransport(rig.adapter,options,descriptor[1],descriptor[1].with_suffix('.pub'),known_hosts=descriptor[2])
            executor=pub.NativePublication(saver,store,transport)
            sig=pub.Signature('Scope Network Fixture','fixture@invalid',1700000003,'+0000')
            kwargs=dict(author=sig,committer=sig,message=b'exact scope over authenticated HTTPS\n',
                local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
            if failure=='accepted-readback-loss':
                rmdir=os.rmdir;cleanup_failed=[]
                def retain_owned_pack(path,*args,**kwargs):
                    if (control.get('fail_upload') and path==b'pack'
                            and kwargs.get('dir_fd') is not None and not cleanup_failed):
                        cleanup_failed.append(True)
                        raise OSError(5,'owned readback scratch cleanup interrupted')
                    return rmdir(path,*args,**kwargs)
                with monkeypatch.context() as initial_confirm:
                    initial_confirm.setattr(os,'rmdir',retain_owned_pack)
                    with pytest.raises(pub.PublicationError,match='graph fetch unavailable') as error:
                        owner.confirm(candidate,review,executor,**kwargs)
                operation_id=error.value.operation_id;state=store.read()
                worker=state['workers'][operation_id]
                assert cleanup_failed and len(executor.transport_debts())==1
                intent=pub.validate_intent(store._prepared_publication.artifact)
                assert owner.active_policy()==old and owner.active_rules()==oldrules and owner.diagnose()['apply_blocked']
                fresh=owner.fresh_apply_preview();assert fresh['apply_blocked'] and fresh['transaction']
                calls=[];original=transport._command
                monkeypatch.setattr(transport,'_command',lambda *a,**k:calls.append(a) or original(*a,**k))
                replacement=operations.NativeOperationStore(rig.root/'data')
                replacement.lifecycle(state['revision'],'scope-lifecycle-fixture',{})
                resumed=pub.NativePublication(saver,replacement,transport)
                assert not calls
                control.clear()
                # Another publisher advances the receiver after accepted C.
                # Recovery must prove C through a freshly fetched descendant graph.
                from test_native_publication import next_commit
                child,raw=next_commit(rig,intent.commit_oid,message=b'fixture receiver descendant\n')
                command(['git','--git-dir='+str(endpoint),'hash-object','-t','commit','-w','--stdin'],input=raw)
                command(['git','--git-dir='+str(endpoint),'update-ref','refs/heads/main',child,intent.commit_oid])
                monkeypatch.setattr(transport,'push',lambda *a,**k:pytest.fail('same-commit reconciliation retried send'))
                fresh_owner=LegacyFixtureScope(saver,replacement)
                current=replacement.read()
                with pytest.raises(disk.Blocked,match='prior-transport-scratch-requires-explicit-settlement'):
                    fresh_owner.recover_publication(resumed,operation_id,expected_revision=current['revision'],command_id=str(uuid.uuid4()))
                assert replacement.read()==current
                assert fresh_owner.active_policy()==old and fresh_owner.active_rules()==oldrules and fresh_owner.diagnose()['apply_blocked']
                debts=resumed.transport_debts();assert len(debts)==1
                before_calls=len(calls)
                for debt in debts:
                    assert debt['operation']==operation_id and debt['owner']==worker['owner'] and debt['phase']=='debt'
                    settled=resumed.settle_transport_resource(debt['resource_id'],operation_id=operation_id,owner_id=debt['owner'])
                    assert settled.state=='removed' and not settled.cleanup_errors
                assert len(calls)==before_calls and not resumed.transport_debts()
                assert replacement.read()==current
                result=fresh_owner.recover_publication(resumed,operation_id,expected_revision=replacement.read()['revision'],command_id=str(uuid.uuid4()))
                assert result['commit_oid']==intent.commit_oid and result['status']=='completed'
                assert fresh_owner.active_policy()==review.new.document() and fresh_owner.active_rules()==review.rules
            else:
                result=owner.confirm(candidate,review,executor,**kwargs)
                assert result['status']==('refused' if failure=='receiver-reject' else 'completed')
                if failure=='receiver-reject':
                    assert owner.active_policy()==old and owner.active_rules()==oldrules and owner.diagnose()['apply_blocked']
                else:
                    assert owner.active_policy()==review.new.document() and owner.active_rules()==review.rules



@pytest.mark.skipif(sys.platform!='linux',reason='Actual Linux issuer/capacity behavior')
@pytest.mark.parametrize('change',['added','mode','deleted'])
def test_retained_legacy_whole_scope_fence_and_forged_grants_refuse_without_effect(tmp_path,change):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    with pytest.raises(Exception,match='issued'):
        with saver.publication_context(replace(candidate),review):pass
    with pytest.raises(Exception,match='issued'):
        with saver.publication_context(candidate,replace(review)):pass
    if change=='added':put(rig.root,b'homeassistant/p/late',b'new member')
    elif change=='mode':os.chmod(rig.root/os.fsdecode(A),0o4640)
    else:os.unlink(rig.root/os.fsdecode(A))
    before=canonical(rig);state=(rig.root/'data/native-operations/state.json').read_bytes()
    with pytest.raises(Exception,match='whole-scope'):
        saver._publication_fence(candidate,review)
    assert canonical(rig)==before and (rig.root/'data/native-operations/state.json').read_bytes()==state
    assert owner.fresh_apply_preview()['apply_blocked']



@pytest.mark.skipif(sys.platform!='linux',reason='Real native scope admission and publication')
def test_retained_legacy_scope_lifecycle_fences_other_owner_issued_confirmation(tmp_path,monkeypatch):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    old_policy=owner.active_policy()
    replacement=operations.NativeOperationStore(rig.root/'data')
    replacement.lifecycle(store.read()['revision'],'scope-restart-regression',{})
    state=replacement.read()
    assert state['records'][state['scope_pending']['record_id']]['status']=='stale'
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(tmp_path/'receiver.git'),'refs/heads/main')
    executor=pub.NativePublication(saver,store,transport)
    sig=pub.Signature('Scope Fixture','fixture@invalid',1700000001,'+0000')
    old_diff=owner.reviewed_diff()
    monkeypatch.setattr(transport,'advertise',lambda:pytest.fail('expired grant reached transport'))
    # A fresh revision number must not revive the original owner's old grant.
    with pytest.raises((operations.Conflict,__import__('native_preview').Stale)):
        owner.confirm(candidate,review,executor,author=sig,committer=sig,
            message=b'stale issuer must never publish\n',local_ref=rig.source.base_ref,
            expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),
            expected_revision=state['revision'])
    assert replacement.read()==state
    assert owner.active_policy()==old_policy and owner.diagnose()['apply_blocked']

    # Direct publication is fenced too, before admission or transport.
    with pytest.raises((operations.Conflict,__import__('native_preview').Stale)):
        executor.publish(candidate,review,author=sig,committer=sig,message=b'old direct grant\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=state['revision'])
    fresh_saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    fresh_owner=LegacyFixtureScope(fresh_saver,replacement)
    restored,new_review=fresh_owner.reopen_review(expected_revision=state['revision'])
    assert restored.tree_oid==candidate.tree_oid and restored.candidate_digest==candidate.candidate_digest
    assert fresh_owner.reviewed_diff()==old_diff
    with fresh_saver.publication_context(restored,new_review):pass



def test_retained_legacy_last_managed_context_explicit_absence_and_metadata_from_ancestry(tmp_path):
    from native_topology_helpers import git
    rig=SaveRig(tmp_path);original=rig.source.remote_oid
    empty=rig.commit({})
    rows=scope.last_managed_context(rig.adapter,empty,(A,b'homeassistant/never'))
    old=next(row for row in rows if row.path==A)
    never=next(row for row in rows if row.path!=A)
    assert old.commit_oid==original and old.content==content_ref(b'base\n')
    assert old.state.record.path==A and {r.path for r in old.parents}==metadata.ancestors((A,))
    assert never.commit_oid is None and never.state is None and never.content is None
    assert not content(rig,empty).get(A)



def test_retained_legacy_last_managed_context_keeps_merge_peers_and_refuses_missing_metadata(tmp_path):
    from native_topology_helpers import git
    rig=SaveRig(tmp_path);original=rig.source.remote_oid
    left=rig.commit({A:b'left historical bytes'})
    git(rig.root,'update-ref','HEAD',original)
    right=rig.commit({A:b'right historical bytes'})
    empty=rig.commit({});tree=git(rig.root,'rev-parse',empty+'^{tree}').decode().strip()
    merge=git(rig.root,'commit-tree',tree,'-p',left,'-p',right,'-m','fixture historical merge').decode().strip()
    rows=scope.last_managed_context(rig.adapter,merge,(A,))
    assert {row.commit_oid for row in rows}=={left,right}
    assert {row.content for row in rows}=={content_ref(b'left historical bytes'),content_ref(b'right historical bytes')}
    # A tracked leaf without its supported metadata cannot be misreported as
    # never managed or silently borrowed from a different historical revision.
    git(rig.root,'update-ref','HEAD',left)
    git(rig.root,'read-tree',left)
    git(rig.root,'rm','--cached','--',os.fsdecode(metadata.MANIFEST_PATH))
    git(rig.root,'commit','-m','fixture unavailable historical metadata')
    broken=git(rig.root,'rev-parse','HEAD').decode().strip()
    with pytest.raises(Exception,match='last-managed-metadata-unavailable'):
        scope.last_managed_context(rig.adapter,broken,(A,))



@pytest.mark.parametrize('change',['size','key','oid','binary','format','mode','symlink','multilink','replace'])
def test_retained_legacy_content_reference_verification_refuses_corruption_before_emission(tmp_path,change):
    data=b'complete\0binary\xffcontent';ref=content_ref(data)
    store=operations.NativeOperationStore(tmp_path/'data')
    directory=tmp_path/'objects';directory.mkdir(mode=0o700)
    path=directory/(ref.key+'.blob');path.write_bytes(data);path.chmod(0o600)
    candidate=ref
    if change=='size':candidate=replace(ref,size=ref.size+1)
    elif change=='key':path.write_bytes(b'x'*len(data))
    elif change=='oid':candidate=replace(ref,oid='a'*40)
    elif change=='binary':candidate=replace(ref,binary=False)
    elif change=='format':candidate=replace(ref,object_format='sha256',oid='a'*64)
    elif change=='mode':path.chmod(0o644)
    elif change=='symlink':
        foreign=directory/'foreign';path.rename(foreign);path.symlink_to(foreign)
    elif change=='multilink':os.link(path,directory/'other')
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises((operations.InvalidState,OSError)):
            with store._verified_scope_blob(fd,candidate):
                if change=='replace':
                    foreign=directory/'new';foreign.write_bytes(data);foreign.chmod(0o600);foreign.replace(path)
                else:pytest.fail('corrupt object emitted content')
    finally:os.close(fd)



def test_retained_legacy_streamed_content_bound_stops_before_target_growth():
    target=io.BytesIO();sink=scope.ContentSink(3,'sha1',target)
    sink.write(b'ab')
    with pytest.raises(Exception,match='bound'):sink.write(b'cd')
    assert target.getvalue()==b'ab'
    with pytest.raises(Exception,match='size-changed'):sink.reference()



@pytest.mark.skipif(sys.platform!='linux',reason='Qualified Linux scope transaction')
@pytest.mark.parametrize('point',['scope_blob_written','scope_blob_fsynced','scope_blob_installed','scope_blob_durable','artifact_fsynced'])
def test_retained_legacy_scope_payload_interruptions_leave_old_authority_and_account_orphans(tmp_path,point):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    owner.build((),expected_revision=store.read()['revision'])
    before=store.read();old=owner.reviewed_diff()
    added=b'addon_configs/new';put(rig.root,added,b'new\0binary\xffpayload')
    def fault(observed):
        if observed==point:raise KeyboardInterrupt(point)
    store.fault=fault
    with pytest.raises(KeyboardInterrupt):
        owner.build((b'homeassistant',b'addon_configs'),approved_parents=(b'addon_configs',),expected_revision=before['revision'])
    store.fault=lambda point:None
    assert store.read()==before and owner.reviewed_diff()==old
    assert store.accounting()['artifact_bytes']>=sum(p.stat().st_size for p in (rig.root/'data/native-operations/artifacts').glob('*.blob'))
    store.compact(store.read()['revision'])
    assert owner.reviewed_diff()==old



@pytest.mark.skipif(sys.platform!='linux',reason='Actual canonical scope critical section')
def test_retained_legacy_scope_install_excludes_compaction_and_foreign_lock_owners(tmp_path):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    observations=[]
    def fault(point):
        if point!='scope_blob_installed':return
        with pytest.raises(Exception,match='scope execution owns'):
            store.compact(store.read()['revision'])
        other=operations.NativeOperationStore(rig.root/'data')
        with pytest.raises(operations.Conflict,match='busy'):
            with operations._publication_observe(other):pass
        observations.append(point)
    store.fault=fault
    owner.build((),expected_revision=store.read()['revision'])
    assert observations



@pytest.mark.skipif(sys.platform!='linux',reason='Actual Linux payload ownership and complete review')
def test_retained_legacy_complete_payload_review_gc_ownership_and_cancel_release(tmp_path):
    rig,saver,store,owner=legacy_fixture(tmp_path)
    added=b'addon_configs/new';payload=b'fresh\0binary\xffpayload';put(rig.root,added,payload)
    candidate,review=owner.build((b'homeassistant',b'addon_configs'),approved_parents=(b'addon_configs',),expected_revision=store.read()['revision'])
    assert review_bytes(owner,'final',added)==payload
    assert review_bytes(owner,'baseline',A)==b'initial publication'
    with pytest.raises(operations.Conflict):review_bytes(owner,'history',added)
    with pytest.raises(operations.Conflict):review_bytes(owner,'final',b'unknown')
    objects=rig.root/'data/native-operations/artifacts'
    refs=set(objects.glob('*.blob'));assert refs
    owner.lifecycle('update');store.compact(store.read()['revision'])
    assert refs<=set(objects.glob('*.blob'))
    from native_topology_helpers import git
    git(rig.root,'reflog','expire','--expire=now','--all');git(rig.root,'gc','--prune=now')
    fresh=LegacyFixtureScope(save.NativeSave(rig.preview,disk.NativeDisk(rig.root)),store)
    rebuilt,_=fresh.reopen_review(expected_revision=store.read()['revision'])
    assert rebuilt.tree_oid==candidate.tree_oid and review_bytes(fresh,'final',added)==payload
    fresh.cancel(expected_revision=store.read()['revision'])
    store.compact(store.read()['revision'])
    assert not list(objects.glob('*.blob'))



@pytest.mark.skipif(sys.platform!='linux',reason='Real admitted legacy cancellation')
def test_retained_legacy_legacy_v1_diagnose_cancel_streams_without_readall_and_reopen_refuses(tmp_path,monkeypatch):
    import hashlib
    rig,saver,store,owner=legacy_fixture(tmp_path)
    owner.build((),expected_revision=store.read()['revision'])
    state=store.read();record=state['records'][state['scope_pending']['record_id']]
    artifacts=rig.root/'data/native-operations/artifacts'
    temporary=artifacts/'legacy-fixture';h=hashlib.sha256();size=0
    with temporary.open('wb') as stream:
        for chunk in (b'{"profile":"native-scope-review-v1","blobs":"',*[b'00'*65536]*160,b'"}'):
            stream.write(chunk);h.update(chunk);size+=len(chunk)
    key=h.hexdigest();temporary.rename(artifacts/(key+'.diff'));(artifacts/(key+'.diff')).chmod(0o600)
    record.update(artifact=key,artifact_size=size)
    record['identity']['execution_policy']='native-scope-v1'
    record['identity']['effect']['after']=operations.digest([state['scope_pending']['candidate_digest'],key])
    record['identity_digest']=operations.digest(record['identity'])
    with store._locked() as (root,_):store._write(root,'state.json',operations.canonical(state))
    def forbidden(*args,**kwargs):raise AssertionError('legacy aggregate read')
    monkeypatch.setattr(store,'_artifact',forbidden)
    assert owner.diagnose()['transaction']
    with pytest.raises(operations.Conflict,match='legacy-scope-v1'):
        owner.reopen_review(expected_revision=state['revision'])
    with pytest.raises(operations.Conflict,match='legacy-scope-v1'):
        owner.reviewed_diff()
    with pytest.raises(operations.Conflict,match='legacy-scope-v1'):
        owner.last_save_offer()
    old=owner.active_policy()
    result=owner.cancel(expected_revision=state['revision'])
    assert result['transaction'] is None and result['active_policy']==old


@pytest.mark.skipif(sys.platform!='linux',reason='Linux selected-side tree admission')
def test_save_choices_pair_whole_sides_and_all_kept_refuses_without_effect(tmp_path):
    rig,saver,store,owner=fresh(tmp_path)
    owner.build((b'homeassistant',),expected_revision=0)
    put(rig.root,A,b'whole selected HA');put(rig.root,B,b'keep Git not this live version')
    os.chmod(rig.root/os.fsdecode(B),0o4640)
    snapshot=rig.preview.build('save')
    rig.preview.select(snapshot.snapshot_id,(A,B),resolutions={A:'ha',B:'git'})
    review=rig.preview.review(snapshot.snapshot_id);assert review.ready,review.blockers
    candidate=saver.build(review);actual=content(rig,candidate.tree_oid)
    baseline=content(rig,rig.source.remote_oid)
    assert actual[A]==b'whole selected HA' and actual[B]==baseline[B]
    records=metadata.decode(actual[metadata.MANIFEST_PATH],policy=rig.policy).by_path
    assert records[B]==metadata.decode(baseline[metadata.MANIFEST_PATH],policy=rig.policy).by_path[B]
    assert pub.validate_candidate(rig.adapter,candidate,review)
    snapshot=rig.preview.build('save');rig.preview.select(snapshot.snapshot_id,(B,),resolutions={B:'git'})
    review=rig.preview.review(snapshot.snapshot_id);before=canonical(rig)
    with pytest.raises(Exception,match='no-save-effects'):saver.build(review)
    assert canonical(rig)==before


@pytest.mark.skipif(sys.platform!='linux',reason='Linux discovery traversal proof')
def test_excluded_subtree_not_listed_and_component_boundary_survives(tmp_path,monkeypatch):
    rig,saver,store,owner=fresh(tmp_path)
    put(rig.root,b'homeassistant/B/trapped/never',b'never read')
    put(rig.root,b'homeassistant/B/C/included',b'included')
    put(rig.root,b'homeassistant/BC/sibling',b'sibling')
    owner.build(((b'homeassistant',True),(b'homeassistant/B',False),(b'homeassistant/B/C',True)),expected_revision=0)
    original=os.listdir
    def checked(directory):
        if isinstance(directory,int):
            path=os.readlink('/proc/self/fd/'+str(directory))
            assert not path.endswith('/homeassistant/B/trapped'),'excluded subtree enumerated'
        return original(directory)
    monkeypatch.setattr(os,'listdir',checked)
    snapshot=rig.preview.build('save');paths={row.path for row in snapshot.rows}
    assert b'homeassistant/B/C/included' in paths and b'homeassistant/BC/sibling' in paths
    assert b'homeassistant/B/trapped/never' not in paths


def test_folder_diagnostics_separate_exclusions_from_selectable_runtime_warnings(tmp_path):
    rig,saver,store,owner=fresh(tmp_path)
    put(rig.root,b'homeassistant/secrets.yaml',b'private')
    put(rig.root,b'homeassistant/recorder.db-wal',b'wal')
    node=next(node for node in owner.folders() if node['label']=='homeassistant')
    assert any('secrets.yaml' in warning for warning in node['exclusions'])
    assert all('database' not in warning for warning in node['exclusions'])
    assert any('remain selectable' in warning for warning in node['advisories'])


@pytest.mark.skipif(sys.platform!='linux',reason='actual scope-control admission')
@pytest.mark.parametrize('offline',['raises','empty'])
def test_local_discovery_and_explicit_re_review_never_observe_remote(tmp_path,offline):
    rig,saver,store,owner=fresh(tmp_path);calls=[]
    def unavailable():
        calls.append(True)
        if offline=='raises':raise OSError('remote offline')
        return replace(rig.source,remote_oid='0'*40,reachable=False,refreshed=False)
    rig.preview.observer=unavailable
    pending=owner.build((b'homeassistant',),expected_revision=0)
    assert pending['review_ready']
    owner.lifecycle('restart')
    assert not owner.diagnose()['review_ready']
    with pytest.raises(operations.Conflict,match='fresh explicit'):owner.confirm(expected_revision=pending['revision'],review_id=pending['transaction']['id'])
    fresh_review=owner.review_pending(expected_revision=pending['revision'],review_id=pending['transaction']['id'])
    assert fresh_review['review_ready'] and fresh_review['active_policy']==pending['active_policy']
    confirmed=owner.confirm(expected_revision=fresh_review['revision'],review_id=fresh_review['transaction']['id'])
    changed=owner.build((),expected_revision=confirmed['revision'])
    cancelled=owner.cancel(expected_revision=changed['revision'])
    assert cancelled['active_policy']['rules']==confirmed['active_policy']['rules']
    assert calls==[] and store.read()['active_scope'] is None
