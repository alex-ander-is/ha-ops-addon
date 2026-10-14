"""Exact publication proofs on disposable repositories; no configured remotes."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_publication as pub
import native_save as save
import native_disk as disk
from native_operations import NativeOperationStore, Conflict, InvalidState
from native_git import parse_commit
from native_topology_helpers import git, METADATA
from test_native_save import SaveRig, reviewed, put, A, B, canonical


SIG=pub.Signature('Publication Fixture','fixture@invalid',1700000000,'+0000')


def fixture(tmp_path,object_format='sha1'):
    rig=SaveRig(tmp_path,object_format)
    endpoint=(tmp_path/'receiver.git').resolve()
    subprocess.run(['git','init','--bare','--object-format='+object_format,str(endpoint)],check=True,capture_output=True)
    subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'push',str(endpoint),
                    rig.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')
    return rig,endpoint,transport


def next_commit(rig,parent,*,message=b'publication fixture\n'):
    tree=git(rig.root,'rev-parse',parent+'^{tree}').decode().strip()
    raw=pub.commit_bytes(SimpleNamespace(tree_oid=tree,remote=SimpleNamespace(remote_oid=parent)),SIG,SIG,message)
    oid=subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'hash-object','-t','commit','-w','--stdin'],input=raw,check=True,capture_output=True).stdout.decode().strip()
    return oid,raw


def transport_intent(transport,parent,oid):
    return SimpleNamespace(endpoint=transport.endpoint,endpoint_identity=transport.endpoint_identity,
        destination=transport.destination,candidate=SimpleNamespace(remote=SimpleNamespace(remote_oid=parent)),commit_oid=oid)


@pytest.mark.skipif(sys.platform!='linux',reason='Actual terminal publication admission')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_terminal_cleanup_needs_no_remote_authentication_or_graph(tmp_path,monkeypatch,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    def cut(point):
        if point=='publication-completed': raise KeyboardInterrupt('before terminal retirement')
    o.fault=cut
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id
    before=o.read();worker=before['workers'][op]
    assert worker['phase']=='completed' and before['recovery'] is not None
    o.fault=lambda point:None
    def unavailable(*args,**kwargs): raise AssertionError('terminal cleanup must remain offline')
    monkeypatch.setattr(t,'advertise',unavailable)
    monkeypatch.setattr(t,'reachability',unavailable)
    monkeypatch.setattr(t,'push',unavailable)
    grant=x.approve_recovery(op,expected_revision=before['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert grant.observation is None
    assert x.resume(grant)['status']=='completed'
    final=o.read()
    assert final['recovery'] is None and final['active'] is None
    assert final['workers'][op]['publication_outcome']==worker['publication_outcome']
    assert rig.adapter.read_ref(kw['local_ref']).oid==worker['publication_outcome']['tip']


@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_real_transport_push_exact_parent_and_complete_graph(tmp_path,object_format):
    rig,endpoint,transport=fixture(tmp_path,object_format)
    observation=transport.advertise()
    oid,raw=next_commit(rig,observation.oid)
    intent=transport_intent(transport,observation.oid,oid)
    assert transport.reachability(observation,oid)[0] is False
    outcome=transport.push(intent)
    assert outcome.kind=='accepted'
    assert transport.qualify_outcome(outcome,intent) is outcome
    fresh=transport.advertise()
    assert fresh.oid==oid and transport.reachability(fresh,observation.oid)[0]
    assert parse_commit(raw,rig.width)[1]==(observation.oid,)
    assert git(rig.root,'rev-parse',rig.source.base_ref).decode().strip()==observation.oid


@pytest.mark.parametrize('hook',['pre-receive','post-receive'])
def test_receiver_hooks_preserved_and_failure_not_blindly_terminal(tmp_path,hook,request):
    rig,endpoint,transport=fixture(tmp_path)
    marker=tmp_path/'hook-ran'
    # Runner /tmp is deliberately noexec; receiver hooks use a private owned
    # executable output directory while repositories/admission stay on tmpfs.
    hook_dir=tempfile.TemporaryDirectory(prefix='n611-receiver-hooks-',dir='/workspace/output' if Path('/workspace/output').is_dir() else tmp_path)
    request.addfinalizer(hook_dir.cleanup)
    subprocess.run(['git','--git-dir='+str(endpoint),'config','core.hooksPath',hook_dir.name],check=True)
    target=Path(hook_dir.name)/hook
    target.write_text('#!/bin/sh\nprintf ran > "'+str(marker)+'"\nexit 1\n')
    target.chmod(0o755)
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')
    parent=transport.advertise().oid
    oid,_=next_commit(rig,parent)
    outcome=transport.push(transport_intent(transport,parent,oid))
    assert marker.read_text()=='ran'
    fresh=transport.advertise()
    assert fresh.oid==(parent if hook=='pre-receive' else oid)
    if hook=='pre-receive':
        assert outcome.kind=='no-update' and not transport.reachability(fresh,oid)[0]
    else:
        assert transport.reachability(fresh,oid)[0]


@pytest.mark.parametrize('extra',[b'foreign status\n',b'!\tforeign:refs/heads/other\t[remote rejected] (no)\n',b'Done\n'])
def test_foreign_or_mixed_transcript_never_proves_refusal(tmp_path,extra):
    rig,endpoint,transport=fixture(tmp_path)
    parent=transport.advertise().oid
    oid,_=next_commit(rig,parent)
    intent=transport_intent(transport,parent,oid)
    raw=b'To '+transport.endpoint+b'\n!\t'+oid.encode()+b':refs/heads/main\t[remote rejected] (pre-receive hook declined)\nDone\n'
    assert transport._push_kind(intent,1,raw,b'error: failed to push some refs\n')=='no-update'
    assert transport._push_kind(intent,1,raw+extra,b'error')=='ambiguous'
    assert transport._push_kind(intent,1,raw.replace(b'[remote rejected]',b'[remote failure]'),b'error')=='ambiguous'


def test_object_format_error_refuses_constructor(tmp_path,monkeypatch):
    rig,endpoint,transport=fixture(tmp_path)
    original=pub.GitCLITransport._command
    def command(self,args,**kwargs):
        if args==('rev-parse','--show-object-format'): return 128,b'',b'format failed'
        return original(self,args,**kwargs)
    monkeypatch.setattr(pub.GitCLITransport,'_command',command)
    with pytest.raises(InvalidState,match='exact endpoint object format'):
        pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')


@pytest.mark.skipif(sys.platform!='linux',reason='Qualified Linux native allocation and admission proof')
class TestPublication:
    @pytest.mark.parametrize('object_format',['sha1','sha256'])
    def test_remote_before_local_exact_commit_and_preserved_unselected(self,tmp_path,object_format):
        rig,endpoint,transport=fixture(tmp_path,object_format)
        put(rig.root,A,b'selected content\n')
        put(rig.root,B,b'unselected churn\n')
        review=reviewed(rig,(A,))
        saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
        candidate=saver.build(review)
        index=(rig.root/METADATA/'index').read_bytes()
        store=NativeOperationStore(rig.root/'data')
        executor=pub.NativePublication(saver,store,transport)
        result=executor.publish(candidate,review,author=SIG,committer=SIG,message=b'exact publication\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
        assert result['status']=='completed'
        oid=result['commit_oid']
        assert transport.advertise().oid==oid
        assert rig.adapter.read_ref(rig.source.base_ref).oid==oid
        assert (rig.root/METADATA/'index').read_bytes()==index
        assert (rig.root/os.fsdecode(B)).read_bytes()==b'unselected churn\n'
        raw,tree,parents=rig.adapter.exact_commit(oid)
        assert tree==candidate.tree_oid and parents==(candidate.remote.remote_oid,)
        state=store.read()
        assert state['workers'][result['operation_id']]['phase']=='completed'
        assert state['recovery'] is None and state['active'] is None


def publication_fixture(tmp_path,path=A,object_format='sha1'):
    rig,endpoint,transport=fixture(tmp_path,object_format)
    put(rig.root,path,b'new reviewed bytes\n')
    parents=tuple(p for p in __import__('native_metadata').ancestors((path,)) if p!=b'homeassistant') if path!=A else ()
    review=reviewed(rig,(path,),parents)
    saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    candidate=saver.build(review)
    store=NativeOperationStore(rig.root/'data')
    executor=pub.NativePublication(saver,store,transport)
    kwargs=dict(author=SIG,committer=SIG,message=b'exact publication\n',local_ref=rig.source.base_ref,
                expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
    return rig,endpoint,transport,review,saver,candidate,store,executor,kwargs


def publication_allocation_measurement(parent,object_format,observer,measurement,monkeypatch,*,packed=False):
    """Observer-only telemetry at actual allocator/atomic overlap boundaries."""
    from contextlib import contextmanager
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(parent,object_format=object_format)
    kw['message']=b'exact publication allocation\n'+b'x'*16384
    baseline=observer(rig.root); rows=[]; held=[]
    original_admission=x._admission
    @contextmanager
    def admitted(prepared,intent):
        with original_admission(prepared,intent) as admission:
            held.append(admission)
            try: yield admission
            finally: held.pop()
    monkeypatch.setattr(x,'_admission',admitted)
    def sample(boundary):
        if held:
            row=measurement(baseline,observer(rig.root),held[0].plan,'publication-'+boundary)
            rows.append(row)
    def fault(boundary):
        sample(boundary)
        if packed and boundary=='publication-completed': raise KeyboardInterrupt('explicit packed cleanup fixture')
    o.fault=fault
    original_creation=disk.Admission.record_file_creation
    def creation(admission,relative,descriptor):
        value=original_creation(admission,relative,descriptor)
        if held and admission is held[0]: sample('held-file:'+os.fsdecode(relative))
        return value
    monkeypatch.setattr(disk.Admission,'record_file_creation',creation)
    if packed:
        with pytest.raises(KeyboardInterrupt) as interrupted: x.publish(c,r,**kw)
        op=interrupted.value.operation_id
        o.fault=sample
        git(rig.root,'update-ref','refs/foreign/allocation-keep',kw['expected_local'])
        git(rig.root,'gc','--prune=now')
        baseline=observer(rig.root); rows.clear()
        state=o.read()
        approval=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
        result=x.resume(approval)
    else: result=x.publish(c,r,**kw)
    assert result['status']=='completed'
    assert any('state_written' in row['family'] for row in rows)
    assert any('held-file:' in row['family'] and '/refs/' in row['family'] for row in rows)
    assert all(row['physical_bytes']<=row['additional_peak'] and row['new_inodes']<=row['required_inodes'] for row in rows)
    if packed: assert any('packed-refs.lock' in row['family'] for row in rows)
    return {'object_format':object_format,'packed_cleanup':packed,'measurements':rows,'observed_physical_peak':max(row['physical_bytes'] for row in rows),
            'observed_new_inode_peak':max(row['new_inodes'] for row in rows),'commit_oid':result['commit_oid']}


@pytest.mark.skipif(sys.platform!='linux',reason='Actual Linux allocator observations')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('packed',[False,True])
def test_publication_tmpfs_actual_allocation_bounds(tmp_path,monkeypatch,object_format,packed):
    from test_native_ext4 import observe
    from dataclasses import asdict
    def measured(before,after,plan,family):
        # xdist peers share tmpfs statvfs; qualify only this owned fixture's
        # actual allocated inode/block inventory. Keep global deltas as telemetry.
        physical=max(0,sum(v['blocks'] for v in after['entries'].values())-sum(v['blocks'] for v in before['entries'].values()))
        new=set(after['entries'])-set(before['entries'])
        byte_bound=sum(v.additional_peak for v in plan.decisions); inode_bound=sum(v.required_inodes for v in plan.decisions)
        assert physical<=byte_bound and len(new)<=inode_bound,(family,physical,len(new),byte_bound,inode_bound)
        return dict(family=family,physical_bytes=physical,new_inodes=len(new),additional_peak=byte_bound,required_inodes=inode_bound,
            logical_bytes=sum(v['logical'] for k,v in after['entries'].items() if k in new and not v['directory']),
            global_free_bytes_delta=before['free_bytes']-after['free_bytes'],global_free_inodes_delta=before['free_inodes']-after['free_inodes'],
            measurement_scope='owned fixture blocks/inodes; shared tmpfs statvfs is telemetry',allocations=[asdict(v) for v in plan.allocations])
    if os.environ.get('PYTEST_XDIST_WORKER_COUNT')=='1':
        from test_native_ext4 import measured  # isolated worker also qualifies physical statvfs deltas
    result=publication_allocation_measurement(tmp_path,object_format,observe,measured,monkeypatch,packed=packed)
    assert result['observed_physical_peak']>0 and result['observed_new_inode_peak']>0
    import json
    print('PUBLICATION_TMPFS_PEAK '+json.dumps(result,default=lambda value:value.hex() if isinstance(value,bytes) else str(value)))


@pytest.mark.skipif(sys.platform!='linux',reason='Qualified Linux native admission')
class TestPublicationFailures:
    def test_same_issued_candidate_recomputed_valid_subset_cannot_replace_full_review(self,tmp_path):
        rig,ep,t=fixture(tmp_path)
        put(rig.root,A,b'approved A'); put(rig.root,B,b'approved B')
        saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
        subset=saver.build(reviewed(rig,(A,)))
        review=reviewed(rig,(A,B)); candidate=saver.build(review)
        before=canonical(rig); remote=t.advertise()
        # These exact T/manifest bytes were really built and verified for A.
        for name in ('selected','tree_oid','manifest_oid','manifest_digest','reviewed_parents'):
            object.__setattr__(candidate,name,getattr(subset,name))
        selection=save._digest((candidate.remote,candidate.remote_object_kind,candidate.binding,
            review.draft,review.selected,review.parents,candidate.reviewed_parents,
            candidate.parent_dependencies,candidate.routes,candidate.manifest_oid))
        object.__setattr__(candidate,'selection_digest',selection)
        object.__setattr__(candidate,'candidate_digest',save._digest((candidate.tree_oid,candidate.baseline_tree_oid,
            candidate.remote_object_kind,selection,candidate.manifest_oid)))
        owner=NativeOperationStore(rig.root/'data'); executor=pub.NativePublication(saver,owner,t)
        with pytest.raises(Exception,match='authority-fields-changed'):
            executor.publish(candidate,review,author=SIG,committer=SIG,message=b'cannot reduce approval\n',
                local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
        assert canonical(rig)==before and t.advertise()==remote and not (rig.root/'data/native-operations').exists()

    @pytest.mark.parametrize('field',['selected','draft','nested-row'])
    def test_same_issued_review_nested_field_mutation_refuses_before_allocation(self,tmp_path,field):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        before=canonical(rig)
        if field=='selected': object.__setattr__(r,'selected',())
        elif field=='draft': object.__setattr__(r.draft,'approved_parents',(b'homeassistant',))
        else: object.__setattr__(r.selected[0],'content','different diagnostic')
        with pytest.raises(Exception,match='authority-fields-changed'): x.publish(c,r,**kw)
        assert canonical(rig)==before and not (rig.root/'data/native-operations').exists()

    def test_no_send_commit_pin_crash_explicit_same_commit_push(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=rig.adapter.publication_ref
        def ref(name,*args,**kwargs):
            value=original(name,*args,**kwargs)
            if name.endswith('/commit'): raise KeyboardInterrupt('before possible attempt')
            return value
        monkeypatch.setattr(rig.adapter,'publication_ref',ref)
        with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
        state=o.read(); op=error.value.operation_id
        assert state['workers'][op]['publication_attempt']=='not-sent'
        monkeypatch.setattr(rig.adapter,'publication_ref',original)
        approval=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='push')
        oid=pub.validate_intent(approval.prepared.artifact).commit_oid
        result=x.resume(approval)
        assert result['status']=='completed' and result['commit_oid']==oid==t.advertise().oid
        assert o.read()['active'] is None

    @pytest.mark.parametrize('copied',['candidate','review'])
    def test_copied_grant_cannot_execute(self,tmp_path,copied):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        before=canonical(rig)
        with pytest.raises(Exception,match='issued|receipt'):
            x.publish(replace(c) if copied=='candidate' else c,replace(r) if copied=='review' else r,**kw)
        assert canonical(rig)==before and not (rig.root/'data/native-operations').exists()

    @pytest.mark.parametrize('field',['artifact','states','record_id','operation_id','owner_id','_current'])
    def test_same_issued_prepared_handle_tampering_refuses_before_writes(self,tmp_path,monkeypatch,field):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=x._admission
        def admission(prepared,intent):
            with pytest.raises(Conflict,match='immutable'):
                setattr(prepared,field,None)
            object.__setattr__(prepared,field,None)
            return original(prepared,intent)
        monkeypatch.setattr(x,'_admission',admission)
        with pytest.raises((Conflict,InvalidState,TypeError),match='foreign|expired|publication'):
            x.publish(c,r,**kw)
        assert not (rig.root/'data/native-operations').exists()

    @pytest.mark.parametrize('when',['before-start','before-send','at-push'])
    def test_remote_race_definite_refusal_and_fresh_operation(self,tmp_path,monkeypatch,when):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        descendant,_=next_commit(rig,c.remote.remote_oid,message=b'independent descendant\n')
        def move():
            subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'push',str(ep),descendant+':refs/heads/main'],check=True,capture_output=True)
        if when=='before-start': move()
        elif when=='before-send':
            original=rig.adapter.publication_ref
            def update(name,*args,**kwargs):
                result=original(name,*args,**kwargs)
                if name.endswith('/commit'): move()
                return result
            monkeypatch.setattr(rig.adapter,'publication_ref',update)
        else:
            original=t.push
            def push(intent): move(); return original(intent)
            monkeypatch.setattr(t,'push',push)
        if when=='before-start':
            with pytest.raises(pub.PublicationError,match='fresh review'): x.publish(c,r,**kw)
            assert not (rig.root/'data/native-operations').exists()
        else:
            result=x.publish(c,r,**kw)
            assert result['status']=='refused'
            state=o.read(); assert state['active'] is None
            assert state['workers'][result['operation_id']]['phase']=='failed'
            assert rig.adapter.read_ref(rig.source.base_ref).oid==kw['expected_local']
            assert t.advertise().oid==descendant
            monkeypatch.undo()
        # A new explicit command and a fresh review against D creates a new C.
        git(rig.root,'update-ref',rig.source.remote_ref,descendant)
        rig.source=replace(rig.source,remote_oid=descendant)
        r2=reviewed(rig,(A,)); c2=s.build(r2)
        kw['command_id']=str(uuid.uuid4())
        kw['expected_revision']=o.read()['revision'] if (rig.root/'data/native-operations/state.json').exists() else 0
        result=x.publish(c2,r2,**kw)
        assert result['status']=='completed'
        assert rig.adapter.exact_commit(result['commit_oid'])[2]==(descendant,)

    def test_remote_descendant_containing_candidate_then_local_cas_recovery(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=t.push; pushed=[]
        other,_=next_commit(rig,kw['expected_local'],message=b'unrelated local change\n')
        def push(intent):
            outcome=original(intent); pushed.append(intent.commit_oid)
            descendant,_=next_commit(rig,intent.commit_oid,message=b'after publication\n')
            subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'push',str(ep),descendant+':refs/heads/main'],check=True,capture_output=True)
            git(rig.root,'update-ref',rig.source.base_ref,other)
            return outcome
        monkeypatch.setattr(t,'push',push)
        with pytest.raises(Exception,match='local expected-old conflict|changed-destination-namespace') as error:
            x.publish(c,r,**kw)
        op=error.value.operation_id; state=o.read()
        assert state['workers'][op]['phase']=='recovery_required'
        assert state['workers'][op]['publication_attempt']=='possible'
        assert rig.adapter.read_ref(rig.source.base_ref).oid==other
        diagnostic=x.diagnose(op); assert diagnostic['execution_authority'] is False
        assert t.reachability(t.advertise(),pushed[0])[0]
        # Explicitly restore fixture's expected-old and approve one readback/CAS.
        git(rig.root,'update-ref',rig.source.base_ref,kw['expected_local'],other)
        monkeypatch.undo()
        grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()))
        result=x.resume(grant)
        assert result['status']=='completed' and result['commit_oid']==pushed[0]
        with pytest.raises(pub.PublicationError,match='one-attempt'): x.resume(grant)

    def test_before_pin_crash_explicit_no_send_abandonment(self,tmp_path):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        def crash(point):
            if point=='publication-effect_started': raise KeyboardInterrupt('fixture before pin')
        o.fault=crash
        with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
        op=error.value.operation_id; state=o.read()
        assert state['workers'][op]['publication_attempt']=='not-sent'
        assert state['workers'][op]['phase']=='recovery_required'
        o.fault=lambda point:None
        grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='abandon')
        result=x.resume(grant)
        assert result['status']=='refused' and o.read()['active'] is None
        assert t.advertise().oid==c.remote.remote_oid

    @pytest.mark.parametrize('raw',[b'homeassistant/'+b'/'.join([b'x'*200]*6)+b'/changed.yaml',
                                  b'homeassistant/'+b'/'.join([b'x'*170]*3)+b'/changed.yaml',
                                  b'homeassistant/nonutf8-\xff\\file.yaml'])
    def test_lossless_long_and_nonutf8_publication_then_reopen(self,tmp_path,raw):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,raw)
        result=x.publish(c,r,**kw)
        assert result['status']=='completed'
        entries=rig.adapter.tree_entries(rig.adapter.exact_commit(result['commit_oid'])[1])
        assert raw in {e.path for e in entries}
        reopened=NativeOperationStore(rig.root/'data',owner=o.owner)
        state=reopened.read()
        intent=pub.validate_intent((rig.root/'data/native-operations/artifacts'/
            (state['records'][state['workers'][result['operation_id']]['record_id']]['artifact']+'.diff')).read_bytes())
        assert intent.candidate.selected[0].path==raw


def test_same_issued_transport_outcome_mutation_refused(tmp_path):
    rig,ep,t=fixture(tmp_path)
    parent=t.advertise().oid; oid,_=next_commit(rig,parent)
    intent=transport_intent(t,parent,oid)
    outcome=t.push(intent)
    object.__setattr__(outcome,'kind','no-update')
    with pytest.raises(pub.PublicationError,match='issued exact'): t.qualify_outcome(outcome,intent)


@pytest.mark.skipif(sys.platform!='linux',reason='Qualified Linux publication fault proof')
class TestPublicationAuthority:
    @pytest.mark.parametrize('field',['graph','epoch','observation','outcome','issuer'])
    def test_same_issued_positive_proof_tampering_denies_before_cas(self,tmp_path,monkeypatch,field):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=o.publication_proof
        def proof(prepared,admission,issued):
            if field=='graph': object.__setattr__(issued,'graph_digest','a'*64)
            elif field=='epoch': object.__setattr__(issued,'epoch',issued.epoch+1)
            elif field=='observation': object.__setattr__(issued,'observation',replace(issued.observation,oid=kw['expected_local']))
            elif field=='outcome': object.__setattr__(issued,'outcome',replace(issued.outcome))
            else: object.__setattr__(issued,'issuer',SimpleNamespace(qualify_publication_proof=lambda *args:None))
            return original(prepared,admission,issued)
        monkeypatch.setattr(o,'publication_proof',proof)
        with pytest.raises(Exception) as error: x.publish(c,r,**kw)
        state=o.read(); worker=state['workers'][error.value.operation_id]
        assert worker['phase']=='recovery_required' and worker['publication_outcome'] is None
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        assert t.advertise().oid!=kw['expected_local']

    @pytest.mark.parametrize('field',['tip-width','tip-escaped','graph','transcript','request','owner','epoch','mode'])
    def test_owner_compiler_accepts_only_exact_fixed_ascii_slots(self,tmp_path,monkeypatch,field):
        import native_operations as owner
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=owner._compile_publication_outcome
        def compile(store,prepared,name,outcome,**kwargs):
            value=dict(outcome)
            if field=='tip-width': value['tip']=value['tip'][:-1]
            elif field=='tip-escaped': value['tip']='\n'+value['tip'][1:]
            elif field=='graph': value['graph']='g'*64
            elif field=='transcript': value['transcript']=None
            elif field=='request': value['request']='a'*64
            elif field=='owner': value['owner']=str(uuid.uuid4())
            elif field=='epoch': value['epoch']+=1
            else: value.update(mode='readback',transcript=None)
            return original(store,prepared,name,value,**kwargs)
        monkeypatch.setattr(owner,'_compile_publication_outcome',compile)
        with pytest.raises(Exception) as error: x.publish(c,r,**kw)
        worker=o.read()['workers'][error.value.operation_id]
        assert worker['phase']=='recovery_required' and worker['publication_outcome'] is None
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']

    def test_templates_are_accounting_only_and_actual_proof_persists_before_cas(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original_checkpoint=o.publication_checkpoint; original_ref=rig.adapter.publication_ref; checked=[]
        def checkpoint(prepared,admission,phase):
            value=original_checkpoint(prepared,admission,phase)
            if phase=='attempt_started':
                template=dict(prepared.states)['proved_original-push']
                template_state=pub._parse(template)
                fabricated=template_state['workers'][prepared.operation_id]['publication_confirmation']
                with pytest.raises(Conflict,match='capability required'):
                    __import__('native_operations')._compile_publication_outcome(o,prepared,'proved_original-push',fabricated)
                with pytest.raises(Conflict,match='issuer compiled'):
                    admission.qualify_publication_outcome(prepared,template)
            return value
        def ref(name,*args,**kwargs):
            if name==kw['local_ref']:
                state=o.read(); worker=state['workers'][state['active']]
                actual=worker['publication_confirmation']; checked.append(actual)
                assert worker['phase']=='effect_started' and actual['kind']=='published'
                assert actual['tip']==t.advertise().oid
                assert actual['transcript']==t._outcome.transcript_digest
                assert actual['request']==__import__('native_operations').publication_request_commitment(state['records'][worker['record_id']],worker)
            return original_ref(name,*args,**kwargs)
        monkeypatch.setattr(o,'publication_checkpoint',checkpoint); monkeypatch.setattr(rig.adapter,'publication_ref',ref)
        result=x.publish(c,r,**kw)
        assert result['status']=='completed' and len(checked)==1
        assert o.read()['workers'][result['operation_id']]['publication_outcome']==checked[0]

    @pytest.mark.parametrize('mutation',['worker-reset','checkpoint-reset','marker-missing','unknown-marker','legacy-extra'])
    def test_attempt_marker_reopen_tampering_refused(self,tmp_path,mutation):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        def crash(point):
            if point=='publication-effect_started' and o._prepared_publication.state('accepted')['workers']:
                # Only the second effect_started fault is the durable possible marker.
                current=pub._parse(o._prepared_publication._current)
                if current['workers'][o._prepared_publication.operation_id]['publication_attempt']=='possible':
                    raise KeyboardInterrupt('after durable possible')
        o.fault=crash
        with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
        op=error.value.operation_id; state=o.read(); worker=state['workers'][op]
        record=state['records'][worker['record_id']]
        assert worker['publication_attempt']=='possible'
        if mutation=='worker-reset': worker['publication_attempt']='not-sent'
        elif mutation=='checkpoint-reset': record['checkpoints'][-1]['publication_attempt']='not-sent'
        elif mutation=='marker-missing': del worker['publication_attempt']
        elif mutation=='unknown-marker': worker['publication_attempt']='unknown'
        else:
            # An old reader's exact generic worker grammar refuses this extension.
            from native_operations import _fields
            with pytest.raises(InvalidState,match='invalid fields'):
                _fields(worker,'id record_id owner epoch phase marker evidence outcome binding')
            return
        target=rig.root/'data/native-operations/state.json'
        target.write_bytes(pub.canonical(state)); target.chmod(0o600)
        with pytest.raises(InvalidState): NativeOperationStore(rig.root/'data',owner=o.owner).read()

    def test_possible_attempt_not_abandoned_from_later_absence(self,tmp_path):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        def crash(point):
            if point=='publication-effect_started':
                current=pub._parse(o._prepared_publication._current)
                if current['workers'][o._prepared_publication.operation_id]['publication_attempt']=='possible':
                    raise KeyboardInterrupt('fixture before actual child')
        o.fault=crash
        with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
        state=o.read(); op=error.value.operation_id
        assert t.advertise().oid==c.remote.remote_oid
        with pytest.raises(Conflict,match='prior possible'):
            x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='abandon')
        assert o.read()['active']==op

    @pytest.mark.parametrize('fault',['lost-ack','mixed-status','post-receive'])
    def test_fresh_positive_graph_reconciles_real_acceptance(self,tmp_path,monkeypatch,request,fault):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        if fault=='post-receive':
            hooks=tempfile.TemporaryDirectory(prefix='n611-post-receive-',dir='/workspace/output')
            request.addfinalizer(hooks.cleanup)
            subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
            hook=Path(hooks.name)/'post-receive'; hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
            t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main')
            x=pub.NativePublication(s,o,t)
        else:
            original=t._command
            def command(args,**kwargs):
                result=original(args,**kwargs)
                if args[0]=='push':
                    if fault=='lost-ack':
                        error=pub.PublicationError('lost accepted acknowledgement'); error.started=True; raise error
                    return result[0],result[1]+b'foreign status\n',result[2]
                return result
            monkeypatch.setattr(t,'_command',command)
        result=x.publish(c,r,**kw)
        assert result['status']=='completed' and t.advertise().oid==result['commit_oid']
        assert rig.adapter.read_ref(kw['local_ref']).oid==result['commit_oid']

    def test_unknown_remote_graph_preserves_local_and_recovery(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=t.push
        def push(intent):
            result=original(intent)
            # Corrupt the exact receiver commit object after actual acceptance.
            route=ep/'objects'/intent.commit_oid[:2]/intent.commit_oid[2:]
            route.write_bytes(b'corrupt received object')
            return result
        monkeypatch.setattr(t,'push',push)
        with pytest.raises(pub.PublicationError,match='remote.*unavailable') as error: x.publish(c,r,**kw)
        assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
        assert o.read()['workers'][error.value.operation_id]['phase']=='recovery_required'

    def test_reopened_original_command_cannot_create_another_operation(self,tmp_path):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        result=x.publish(c,r,**kw)
        original_command=kw['command_id']
        git(rig.root,'update-ref',rig.source.remote_ref,result['commit_oid'])
        rig.source=replace(rig.source,remote_oid=result['commit_oid'],base_oid=result['commit_oid'])
        put(rig.root,A,b'another explicitly reviewed change')
        review=reviewed(rig,(A,)); candidate=s.build(review)
        reopened=NativeOperationStore(rig.root/'data',owner=o.owner)
        executor=pub.NativePublication(s,reopened,t)
        kw.update(expected_revision=reopened.read()['revision'],expected_local=result['commit_oid'])
        with pytest.raises(Conflict,match='command already recorded'):
            executor.publish(candidate,review,**kw)
        assert len(reopened.read()['workers'])==1


def test_raw_commit_serialization_and_hash_independent_oracle():
    import hashlib
    candidate=SimpleNamespace(tree_oid='a'*40,remote=SimpleNamespace(remote_oid='b'*40))
    raw=pub.commit_bytes(candidate,SIG,SIG,b'opaque message \xff\n')
    expected=(b'tree '+b'a'*40+b'\nparent '+b'b'*40+
        b'\nauthor Publication Fixture <fixture@invalid> 1700000000 +0000'+
        b'\ncommitter Publication Fixture <fixture@invalid> 1700000000 +0000\n\nopaque message \xff\n')
    assert raw==expected
    assert pub.object_id(b'commit',raw,'sha1')==hashlib.sha1(b'commit '+str(len(expected)).encode()+b'\0'+expected).hexdigest()


@pytest.mark.skipif(sys.platform!='linux',reason='Qualified Linux publication dependency proof')
class TestPublicationDependencies:
    def test_direct_tree_candidate_refused_before_owner_allocation(self,tmp_path):
        rig,ep,t=fixture(tmp_path)
        tree=git(rig.root,'rev-parse',rig.source.remote_oid+'^{tree}').decode().strip()
        git(rig.root,'update-ref',rig.source.remote_ref,tree)
        rig.source=replace(rig.source,remote_oid=tree)
        put(rig.root,A,b'changed selected')
        review=reviewed(rig,(A,)); saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
        candidate=saver.build(review); assert candidate.remote_object_kind==b'tree'
        store=NativeOperationStore(rig.root/'data'); executor=pub.NativePublication(saver,store,t)
        with pytest.raises(InvalidState,match='commit source'):
            executor.publish(candidate,review,author=SIG,committer=SIG,message=b'no guessed parent',
                local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
        assert not (rig.root/'data/native-operations').exists()

    @pytest.mark.parametrize('dependency',['unselected-blob','manifest','tree'])
    def test_corrupt_complete_candidate_dependency_refused_before_effects(self,tmp_path,dependency):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        if dependency=='tree': oid=c.tree_oid
        elif dependency=='manifest': oid=c.manifest_oid
        else: oid=next(e.oid for e in rig.adapter.tree_entries(c.tree_oid) if e.path==B)
        target=rig.root/METADATA/'objects'/oid[:2]/oid[2:]
        target.write_bytes(b'corrupt exact dependency')
        with pytest.raises(Exception): x.publish(c,r,**kw)
        assert not (rig.root/'data/native-operations').exists()
        assert t.advertise().oid==c.remote.remote_oid


@pytest.mark.parametrize('profile',['url-rewrite','promisor','shallow','alternates'])
def test_transport_rejects_routing_or_incomplete_graph_profiles(tmp_path,profile):
    rig,ep,t=fixture(tmp_path)
    if profile=='url-rewrite': git(rig.root,'config','url.file:///foreign.insteadOf',str(ep))
    elif profile=='promisor': subprocess.run(['git','--git-dir='+str(ep),'config','remote.foreign.promisor','true'],check=True)
    elif profile=='shallow': (ep/'shallow').write_text(rig.source.remote_oid+'\n')
    else: (ep/'objects/info/alternates').write_text(str(rig.root/METADATA/'objects')+'\n')
    with pytest.raises(pub.PublicationError,match='unsupported'):
        pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main')


@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux process group and executable receiver fixture')
def test_transport_deadline_terminates_exact_child_group(tmp_path,request):
    rig,ep,t=fixture(tmp_path)
    hooks=tempfile.TemporaryDirectory(prefix='n611-deadline-hook-',dir='/workspace/output')
    request.addfinalizer(hooks.cleanup)
    subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
    hook=Path(hooks.name)/'pre-receive'; pid_file=tmp_path/'hook.pid'; child_file=tmp_path/'child.pid'
    hook.write_text('#!/bin/sh\nprintf "%s" "$$" > "'+str(pid_file)+'"\nsleep 30 &\nprintf "%s" "$!" > "'+str(child_file)+'"\nwait\n')
    hook.chmod(0o755)
    t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main',timeout=.5)
    parent=t.advertise().oid; oid,_=next_commit(rig,parent)
    intent=transport_intent(t,parent,oid)
    with pytest.raises(pub.PublicationError,match='deadline'): t.push(intent)
    assert t.qualify_outcome(t._outcome,intent).kind=='ambiguous'
    for path in (pid_file,child_file):
        pid=int(path.read_text()); proc=Path('/proc')/str(pid)/'stat'
        import time
        deadline=time.monotonic()+1
        while proc.exists() and proc.read_text().split(') ',1)[1].split()[0]!='Z' and time.monotonic()<deadline:
            time.sleep(.01)
        assert not proc.exists() or proc.read_text().split(') ',1)[1].split()[0]=='Z'
    assert t.advertise().oid==parent


@pytest.mark.skipif(sys.platform!='linux',reason='Actual bounded Linux publication cache')
def test_one_snapshot_and_one_historical_artifact_stream_per_attempt(tmp_path,monkeypatch):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    kw['message']=b'large immutable intent\n'+b'x'*60000
    result=x.publish(c,r,**kw)
    state=o.read(); first=state['records'][state['workers'][result['operation_id']]['record_id']]
    artifact=rig.root/'data/native-operations/artifacts'/(first['artifact']+'.diff')
    info=artifact.stat(); length=artifact.stat().st_size
    git(rig.root,'update-ref',rig.source.remote_ref,result['commit_oid'])
    rig.source=replace(rig.source,remote_oid=result['commit_oid'],base_oid=result['commit_oid'])
    put(rig.root,A,b'next exact reviewed bytes')
    review=reviewed(rig,(A,)); candidate=s.build(review)
    counters={'artifact_bytes':0,'snapshots':0,'source_config':0}
    original_read=os.read; original_snapshot=s.disk.snapshot; original_raw=rig.adapter._raw
    def read(fd,count):
        metadata=os.fstat(fd); data=original_read(fd,count)
        if (metadata.st_dev,metadata.st_ino)==(info.st_dev,info.st_ino): counters['artifact_bytes']+=len(data)
        return data
    def snapshot(): counters['snapshots']+=1; return original_snapshot()
    def raw(command,**kwargs):
        if command and command[0]=='config': counters['source_config']+=1
        return original_raw(command,**kwargs)
    monkeypatch.setattr(os,'read',read); monkeypatch.setattr(s.disk,'snapshot',snapshot); monkeypatch.setattr(rig.adapter,'_raw',raw)
    kw.update(message=b'next commit\n',command_id=str(uuid.uuid4()),expected_revision=state['revision'],expected_local=result['commit_oid'])
    result2=x.publish(candidate,review,**kw)
    assert result2['status']=='completed'
    assert counters=={'artifact_bytes':length,'snapshots':1,'source_config':1}


@pytest.mark.skipif(sys.platform!='linux',reason='Real child death at durable publication boundaries')
@pytest.mark.parametrize('boundary',['accepted','tree-pin','commit-pin','push','proof','local-cas','completed'])
@pytest.mark.parametrize('object_format,gc',[('sha1',False),('sha1',True),('sha256',True)])
def test_real_child_death_reopen_explicit_same_commit_recovery(tmp_path,boundary,object_format,gc):
    import json
    parent=tmp_path/'child'; parent.mkdir()
    code=r'''
import sys,os,signal,json
from pathlib import Path
from test_native_publication import publication_fixture
rig,ep,t,r,s,c,o,x,kw=publication_fixture(Path(sys.argv[1]),object_format=sys.argv[3])
boundary=sys.argv[2]
def stop():
    prepared=o._prepared_publication
    print(json.dumps({'operation':prepared.operation_id,'owner':o.owner}),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
def fault(point):
    if point=='publication-'+boundary: stop()
o.fault=fault
original_ref=rig.adapter.publication_ref
def ref(name,*args,**kwargs):
    value=original_ref(name,*args,**kwargs)
    if boundary=='tree-pin' and name.endswith('/tree') or boundary=='commit-pin' and name.endswith('/commit') or boundary=='local-cas' and name==kw['local_ref']: stop()
    return value
rig.adapter.publication_ref=ref
original_push=t.push
def push(intent):
    value=original_push(intent)
    if boundary=='push': stop()
    return value
t.push=push
x.publish(c,r,**kw)
raise AssertionError('fixture did not die at boundary')
'''
    result=subprocess.run([sys.executable,'-c',code,str(parent),boundary,object_format],capture_output=True,timeout=45,
        env={**os.environ,'PYTHONPATH':str(Path(__file__).parent),'PYTHONDONTWRITEBYTECODE':'1'})
    assert result.returncode==-9,result.stderr.decode(errors='replace')
    receipt=json.loads(result.stdout.splitlines()[-1]); root=parent/'live'
    store=NativeOperationStore(root/'data',owner=receipt['owner']); state=store.read()
    worker=state['workers'][receipt['operation']]; record=state['records'][worker['record_id']]
    intent=pub.validate_intent((root/'data/native-operations/artifacts'/(record['artifact']+'.diff')).read_bytes())
    original_checkpoints=list(record['checkpoints'])
    descendant=None
    if boundary=='proof':
        raw=pub.commit_bytes(SimpleNamespace(tree_oid=intent.candidate.tree_oid,remote=SimpleNamespace(remote_oid=intent.commit_oid)),
            SIG,SIG,b'descendant after persisted positive proof\n')
        descendant=subprocess.run(['git','--git-dir='+os.fsdecode(intent.endpoint),'hash-object','-t','commit','-w','--stdin'],
            input=raw,capture_output=True,check=True).stdout.decode().strip()
        subprocess.run(['git','--git-dir='+os.fsdecode(intent.endpoint),'update-ref',intent.destination,descendant,intent.commit_oid],check=True)
    expected_packed=None
    if gc:
        git(root,'update-ref','refs/foreign/keep',intent.expected_local)
        git(root,'gc','--prune=now')
        packed=(root/METADATA/'packed-refs').read_bytes()
        assert intent.expected_local.encode()+b' refs/foreign/keep\n' in packed
        expected_packed=pub.packed_pin_removal(packed,intent.commit_pin,intent.commit_oid)
        expected_packed=pub.packed_pin_removal(expected_packed,intent.tree_pin,intent.candidate.tree_oid)
    from native_git import NativeGit
    from native_preview import NativePreview
    adapter=NativeGit(root,policy=intent.candidate.binding.policy)
    preview=NativePreview(adapter,lambda:intent.candidate.remote,intent.candidate.binding,scratch_root=parent/'scratch')
    saver=save.NativeSave(preview,disk.NativeDisk(root))
    transport=pub.GitCLITransport(adapter,intent.endpoint,intent.destination)
    executor=pub.NativePublication(saver,store,transport)
    before=state['revision']; diagnostic=executor.diagnose(worker['id'])
    assert diagnostic['execution_authority'] is False and store.read()['revision']==before
    pending='cleanup' if boundary=='completed' else 'reconcile' if boundary in ('push','proof','local-cas') else 'abandon'
    grant=executor.approve_recovery(worker['id'],expected_revision=before,command_id=str(uuid.uuid4()),pending_effect=pending)
    result=executor.resume(grant)
    assert result['status']==('completed' if pending!='abandon' else 'refused')
    assert result['commit_oid']==intent.commit_oid and store.read()['active'] is None
    assert adapter.read_ref(intent.local_ref).oid==(intent.expected_local if pending=='abandon' else intent.commit_oid)
    if boundary=='proof':
        final=store.read(); final_worker=final['workers'][worker['id']]
        assert final['records'][record['id']]['checkpoints'][:len(original_checkpoints)]==original_checkpoints
        assert final_worker['publication_outcome']==worker['publication_outcome']
        confirmation=final_worker['publication_confirmation']
        assert confirmation['mode']=='readback' and confirmation['tip']==descendant and confirmation['transcript'] is None
    if gc:
        assert (root/METADATA/'packed-refs').read_bytes()==expected_packed
        assert adapter.read_ref('refs/foreign/keep').oid==intent.expected_local
        assert adapter.read_ref(intent.tree_pin).oid is None and adapter.read_ref(intent.commit_pin).oid is None


@pytest.mark.skipif(sys.platform!='linux',reason='Actual local CAS and sole-owner route failure')
class TestPublicationDoubleFaults:
    def test_missing_reflogs_cannot_auto_allocate_unadmitted_routes(self,tmp_path):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        paths=(rig.root/METADATA/'logs'/kw['local_ref'],rig.root/METADATA/'logs/HEAD')
        for path in paths:
            if path.exists(): path.unlink()
        result=x.publish(c,r,**kw)
        assert result['status']=='completed' and all(not path.exists() for path in paths)

    def test_admitted_reflog_fsync_failure_retains_exact_recovery(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        log=rig.root/METADATA/'logs'/kw['local_ref']; inode=log.stat().st_ino
        original=os.fsync; errors=[]
        def fsync(fd):
            if not errors and os.fstat(fd).st_ino==inode:
                error=OSError(5,'admitted reflog fsync'); errors.append(error); raise error
            return original(fd)
        monkeypatch.setattr(os,'fsync',fsync)
        with pytest.raises(OSError) as error: x.publish(c,r,**kw)
        assert error.value is errors[0]
        state=o.read(); worker=state['workers'][error.value.operation_id]
        assert worker['phase']=='recovery_required' and worker['publication_outcome']['kind']=='published'
        assert rig.adapter.read_ref(kw['local_ref']).oid==t.advertise().oid

    @pytest.mark.parametrize('replace_owner',[False,True])
    def test_local_lost_ack_no_second_cas_and_owner_substitution_denial(self,tmp_path,monkeypatch,replace_owner):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        original=subprocess.run; calls=[]
        def run(argv,**kwargs):
            result=original(argv,**kwargs)
            if 'update-ref' in argv and kw['local_ref'] in argv:
                calls.append(tuple(argv))
                if replace_owner:
                    target=rig.root/'data/native-operations/state.json'; replacement=target.with_name('foreign-state')
                    replacement.write_bytes(target.read_bytes()); replacement.chmod(0o600); os.replace(replacement,target)
                return subprocess.CompletedProcess(argv,result.returncode,b'corrupt acknowledgement',result.stderr)
            return result
        monkeypatch.setattr(subprocess,'run',run)
        with pytest.raises(Exception) as error: x.publish(c,r,**kw)
        assert len(calls)==1
        op=error.value.operation_id; state=o.read()
        assert rig.adapter.read_ref(kw['local_ref']).oid==t.advertise().oid
        if replace_owner:
            assert state['workers'][op]['phase']=='effect_started'
            assert 'changed-destination-namespace' in str(error.value.cleanup_errors)
        else:
            assert state['workers'][op]['phase']=='recovery_required'
            grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()))
            result=x.resume(grant)
            assert result['status']=='completed' and len(calls)==1

    def test_object_fsync_primary_survives_secondary_close(self,tmp_path,monkeypatch):
        rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
        target=rig.root/METADATA/'objects'/c.tree_oid[:2]/c.tree_oid[2:]; info=target.stat()
        original_fsync=os.fsync; original_close=os.close; primary=OSError(5,'primary object fsync')
        failed=[]
        def fsync(fd):
            observed=os.fstat(fd)
            if not failed and (observed.st_dev,observed.st_ino)==(info.st_dev,info.st_ino):
                failed.append(fd); raise primary
            return original_fsync(fd)
        def close(fd):
            if failed and fd==failed[0]:
                failed[0]=None; original_close(fd); raise OSError(5,'secondary object close')
            return original_close(fd)
        monkeypatch.setattr(os,'fsync',fsync); monkeypatch.setattr(os,'close',close)
        with pytest.raises(OSError) as error: x.publish(c,r,**kw)
        assert error.value is primary
        assert 'publication-object-close:' in str(error.value.cleanup_errors)
        assert o.read()['workers'][error.value.operation_id]['phase']=='recovery_required'
        assert t.advertise().oid==c.remote.remote_oid


@pytest.mark.skipif(sys.platform!='linux',reason='Actual retained proof lifecycle and compaction')
def test_positive_proof_survives_takeovers_summary_capsule_and_recovery(tmp_path):
    import copy
    import native_operations as owner
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    def stop(point):
        if point=='publication-proof': raise KeyboardInterrupt('persisted positive proof')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id; state=o.read(); first=copy.deepcopy(state['workers'][op]['publication_outcome'])
    store=o
    for number in range(5):
        store=NativeOperationStore(rig.root/'data')
        store.lifecycle(store.read()['revision'],'proof-update-'+str(number),{})
        current=store.read(); worker=current['workers'][op]; record=current['records'][worker['record_id']]
        assert worker['publication_outcome']==first and worker['publication_attempt']=='possible'
        assert all(p['publication_outcome'] in (None,first) for p in record['checkpoints'])
    assert record['recovery_range']['count']>=5
    executor=pub.NativePublication(s,store,t)
    grant=executor.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='reconcile')
    assert executor.resume(grant)['status']=='completed'
    final=store.read(); worker=final['workers'][op]; record=final['records'][worker['record_id']]
    assert worker['publication_outcome']==first
    assert worker['publication_confirmation']['owner']==store.owner
    assert worker['publication_confirmation']['epoch']==worker['epoch']
    capsule=store._capsule(record,worker); store._validate_capsule(capsule,'save')
    for field,value in [('graph','z'*64),('tip',first['tip'][:-1]),('owner',str(uuid.uuid4()))]:
        bad=copy.deepcopy(capsule); bad['worker']['publication_outcome'][field]=value
        bad['digest']=owner.digest({k:v for k,v in bad.items() if k!='digest'})
        with pytest.raises(InvalidState): store._validate_capsule(bad,'save')


@pytest.mark.skipif(sys.platform!='linux',reason='Actual canonical native publication retention')
def test_native_publication_compaction_retains_codec_and_actual_commitment(tmp_path):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    store=NativeOperationStore(rig.root/'data',retention=2)
    executor=pub.NativePublication(s,store,t); first_record=None
    for number in range(5):
        if number:
            git(rig.root,'update-ref',rig.source.remote_ref,result['commit_oid'])
            rig.source=replace(rig.source,remote_oid=result['commit_oid'],base_oid=result['commit_oid'])
            put(rig.root,A,('explicit reviewed change '+str(number)).encode())
            r=reviewed(rig,(A,)); c=s.build(r)
        kw.update(expected_revision=0 if number==0 else store.read()['revision'],
            expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()))
        result=executor.publish(c,r,**kw)
        state=store.read(); worker=state['workers'][result['operation_id']]
        if first_record is None: first_record=worker['record_id']
        assert worker['publication_outcome']['tip']==result['commit_oid']
    assert first_record not in state['records'] and len(state['records'])<=2
    assert state['checkpoint']['through_epoch']>0
    assert state['baseline_provenance']['identity']['execution_policy']=='native-publication-v1'
    assert NativeOperationStore(rig.root/'data',owner=store.owner).read()==state


@pytest.mark.skipif(sys.platform!='linux',reason='Actual historical artifact substitution after admission')
@pytest.mark.parametrize('substitution',['bytes','directory'])
def test_historical_artifact_substitution_after_preparation_denies_effect(tmp_path,monkeypatch,substitution):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    first=x.publish(c,r,**kw); state=o.read()
    historical=state['records'][state['workers'][first['operation_id']]['record_id']]
    directory=rig.root/'data/native-operations/artifacts'; artifact=directory/(historical['artifact']+'.diff')
    git(rig.root,'update-ref',rig.source.remote_ref,first['commit_oid'])
    rig.source=replace(rig.source,remote_oid=first['commit_oid'],base_oid=first['commit_oid'])
    put(rig.root,A,b'next selected bytes')
    review=reviewed(rig,(A,)); candidate=s.build(review)
    kw.update(expected_revision=state['revision'],expected_local=first['commit_oid'],command_id=str(uuid.uuid4()))
    original=o.publication_checkpoint; changed=[]
    def checkpoint(prepared,admission,phase):
        result=original(prepared,admission,phase)
        if phase=='effect_started' and not changed:
            changed.append(True)
            if substitution=='bytes': artifact.write_bytes(b'x'*artifact.stat().st_size)
            else:
                directory.rename(directory.with_name('artifacts-original'))
                directory.mkdir(mode=0o700)
                for item in directory.with_name('artifacts-original').iterdir():
                    (directory/item.name).write_bytes(item.read_bytes()); (directory/item.name).chmod(0o600)
        return result
    monkeypatch.setattr(o,'publication_checkpoint',checkpoint)
    with pytest.raises(Exception): x.publish(candidate,review,**kw)
    assert changed and rig.adapter.read_ref(kw['local_ref']).oid==first['commit_oid']
    assert t.advertise().oid==first['commit_oid']


@pytest.mark.skipif(sys.platform!='linux',reason='Actual long and non-UTF-8 publication roots')
@pytest.mark.parametrize('components',[(b'x'*200,)*6,(b'root-\xff\\',)])
def test_lossless_long_and_nonutf8_root_publication_then_reopen(tmp_path,components):
    parent=tmp_path
    for part in components: parent=parent/os.fsdecode(part)
    parent.mkdir(parents=True)
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(parent)
    result=x.publish(c,r,**kw); state=o.read(); worker=state['workers'][result['operation_id']]
    identity=state['records'][worker['record_id']]['identity']
    from native_operations import decode_native_path
    assert decode_native_path(identity['root']['path'],absolute=True)==os.fsencode(rig.root)
    assert NativeOperationStore(rig.root/'data',owner=o.owner).read()==state


@pytest.mark.skipif(sys.platform!='linux',reason='R1 real interrupted owned terminal cleanup')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('cut',[1,2])
def test_r1_terminal_cleanup_resumes_acknowledged_deletions(tmp_path,monkeypatch,object_format,cut):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    original=rig.adapter.publication_delete_pin; deleted=[]
    def delete(name,**kwargs):
        original(name,**kwargs); deleted.append(name)
        if len(deleted)==cut: raise KeyboardInterrupt('after actual own pin deletion')
    monkeypatch.setattr(rig.adapter,'publication_delete_pin',delete)
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id; monkeypatch.setattr(rig.adapter,'publication_delete_pin',original)
    state=o.read(); assert state['workers'][op]['phase']=='completed' and state['recovery']
    git(rig.root,'gc','--prune=now')
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner); executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='completed' and reopened.read()['recovery'] is None


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual local/ref/reflog durability retry')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_already_c_rehardens_without_cas_or_append(tmp_path,monkeypatch,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    log=rig.root/METADATA/'logs'/kw['local_ref']; info=log.stat(); real=os.fsync; hits=[]
    def fsync(fd):
        stat=os.fstat(fd)
        if (stat.st_dev,stat.st_ino)==(info.st_dev,info.st_ino): hits.append(True); raise OSError(5,'persistent local log hardening')
        return real(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises(OSError) as error: x.publish(c,r,**kw)
    op=error.value.operation_id; actual=log.read_bytes(); original=rig.adapter.publication_ref; cas=[]
    def ref(name,*args,**kwargs):
        if name==kw['local_ref']: cas.append(name)
        return original(name,*args,**kwargs)
    monkeypatch.setattr(rig.adapter,'publication_ref',ref)
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner); executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()))
    with pytest.raises(OSError,match='persistent local log hardening'): executor.resume(grant)
    assert len(hits)>=2 and not cas and log.read_bytes()==actual
    assert reopened.read()['workers'][op]['phase']=='recovery_required'
    monkeypatch.setattr(os,'fsync',real)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()))
    assert executor.resume(grant)['status']=='completed' and not cas and log.read_bytes()==actual


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual retained prior terminal cleanup debt')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_new_publication_preserves_prior_cleanup_debt(tmp_path,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    def stop(point):
        if point=='publication-completed': raise KeyboardInterrupt('prior unresolved own cleanup')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    state=o.read(); old=state['recovery']; intent=pub.validate_intent(o._prepared_publication.artifact)
    o.fault=lambda point:None
    git(rig.root,'update-ref',rig.source.remote_ref,intent.commit_oid)
    rig.source=replace(rig.source,remote_oid=intent.commit_oid,base_oid=intent.commit_oid)
    put(rig.root,A,b'new separately reviewed change'); r=reviewed(rig,(A,)); c=s.build(r)
    kw.update(expected_revision=state['revision'],expected_local=intent.commit_oid,command_id=str(uuid.uuid4()))
    with pytest.raises(Conflict,match='cleanup'): x.publish(c,r,**kw)
    assert o.read()['recovery']==old
    assert all(rig.adapter.read_ref(name).oid==oid for name,oid in ((intent.commit_pin,intent.commit_oid),(intent.tree_pin,intent.candidate.tree_oid)))


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual refusal-state failure retains reachability')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_refusal_state_failure_keeps_both_pins_through_gc(tmp_path,monkeypatch,request,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    hooks=tempfile.TemporaryDirectory(prefix='n611-r1-refusal-hooks-',dir='/workspace/output');request.addfinalizer(hooks.cleanup)
    hook=Path(hooks.name)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
    subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
    t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main'); x=pub.NativePublication(s,o,t)
    original=o._write; fired=[]
    def write(fd,name,payload,**kwargs):
        if name=='state.json' and not fired and any(w['phase']=='failed' for w in pub._parse(payload)['workers'].values()):
            fired.append(True);raise OSError(5,'refusal owner publication failure')
        return original(fd,name,payload,**kwargs)
    monkeypatch.setattr(o,'_write',write)
    with pytest.raises(OSError) as error: x.publish(c,r,**kw)
    intent=pub.validate_intent(o._prepared_publication.artifact)
    assert all(rig.adapter.read_ref(name).oid==oid for name,oid in ((intent.commit_pin,intent.commit_oid),(intent.tree_pin,intent.candidate.tree_oid)))
    git(rig.root,'gc','--prune=now'); assert rig.adapter.exact_commit(intent.commit_oid)[1]==c.tree_oid


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual receiver hook repeated command receipt')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_recovery_command_replay_after_reopen_cannot_push(tmp_path,monkeypatch,request,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    hooks=tempfile.TemporaryDirectory(prefix='n611-r1-replay-hooks-',dir='/workspace/output');request.addfinalizer(hooks.cleanup)
    marker=tmp_path/'received-attempts';hook=Path(hooks.name)/'pre-receive'
    hook.write_text('#!/bin/sh\nprintf x >> "'+str(marker)+'"\nexit 1\n');hook.chmod(0o755)
    subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
    t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main'); x=pub.NativePublication(s,o,t)
    original=rig.adapter.publication_ref
    def ref(name,*args,**kwargs):
        value=original(name,*args,**kwargs)
        if name.endswith('/commit'): raise KeyboardInterrupt('before possible send')
        return value
    monkeypatch.setattr(rig.adapter,'publication_ref',ref)
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;monkeypatch.setattr(rig.adapter,'publication_ref',original)
    command=str(uuid.uuid4());grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=command,pending_effect='push')
    with pytest.raises(pub.PublicationError): x.resume(grant)
    assert marker.read_text()=='x'
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner); executor=pub.NativePublication(s,reopened,t)
    with pytest.raises(Conflict,match='command'): executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=command,pending_effect='push')
    assert marker.read_text()=='x'
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(replacement.read()['revision'],'r1-command-takeover',{})
    executor=pub.NativePublication(s,replacement,t)
    with pytest.raises(Conflict,match='command'): executor.approve_recovery(op,expected_revision=replacement.read()['revision'],command_id=command,pending_effect='push')
    assert marker.read_text()=='x'


@pytest.mark.skipif(sys.platform!='linux',reason='R1 incompatible explicit Git signature before effects')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('field,value',[('name',' Fixture '),('name','Fixture.'),('name','\tFixture'),('email','fixture@invalid;'),('timezone','-0000')])
def test_r1_signature_incompatible_git_serialization_refuses_before_effect(tmp_path,object_format,field,value):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    values=dict(name=SIG.name,email=SIG.email,timestamp=SIG.timestamp,timezone=SIG.timezone);values[field]=value
    old=t.advertise()
    with pytest.raises((InvalidState,pub.PublicationError,__import__('native_git').InvalidInput)):
        kw['committer']=pub.Signature(**values);x.publish(c,r,**kw)
    assert t.advertise()==old and rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local']
    assert not (rig.root/'data/native-operations').exists()


@pytest.mark.skipif(sys.platform!='linux',reason='R1 exact terminal cleanup owner-write debts')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('progress',[2,4,5])
def test_r1_cleanup_owner_write_fault_reopens_exact_remaining_debt(tmp_path,monkeypatch,object_format,progress):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    original=o._write;fired=[]
    def write(fd,name,payload,**kwargs):
        if name=='state.json':
            state=pub._parse(payload); workers=list(state['workers'].values())
            if workers and workers[-1]['phase']=='completed':
                current=workers[-1]['publication_cleanup']
                if not fired and (current==progress or progress==5 and current==4 and state['recovery'] is None):
                    fired.append(True);raise OSError(5,'terminal cleanup owner write')
        return original(fd,name,payload,**kwargs)
    monkeypatch.setattr(o,'_write',write)
    with pytest.raises(OSError) as error: x.publish(c,r,**kw)
    op=error.value.operation_id; assert fired and o.read()['recovery']
    git(rig.root,'gc','--prune=now')
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner); executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='completed' and reopened.read()['recovery'] is None


@pytest.mark.skipif(sys.platform!='linux',reason='R1 absence authority and foreign cleanup occupants')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('occupant',['unexplained-absence','foreign','symbolic'])
def test_r1_cleanup_never_adopts_missing_or_foreign_pin(tmp_path,object_format,occupant):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    def stop(point):
        target='publication-completed' if occupant=='unexplained-absence' else 'publication-pin-retirement-1'
        if point==target: raise KeyboardInterrupt('durable cleanup boundary')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;intent=pub.validate_intent(o._prepared_publication.artifact);route=rig.root/METADATA/intent.commit_pin
    if occupant=='unexplained-absence': git(rig.root,'update-ref','-d',intent.commit_pin)
    elif occupant=='foreign': git(rig.root,'update-ref',intent.commit_pin,kw['expected_local'])
    else: route.write_bytes(b'ref: '+kw['local_ref'].encode()+b'\n')
    original=route.read_bytes() if route.exists() else None
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner); executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(Exception): executor.resume(grant)
    assert (route.read_bytes() if route.exists() else None)==original and reopened.read()['recovery']


@pytest.mark.skipif(sys.platform!='linux',reason='R1 durable failed refusal cleanup survives GC/reopen')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('cut',[1,2])
def test_r1_refused_cleanup_cut_retains_proof_and_resumes(tmp_path,monkeypatch,request,object_format,cut):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    hooks=tempfile.TemporaryDirectory(prefix='n611-r1-refusal-cleanup-',dir='/workspace/output');request.addfinalizer(hooks.cleanup)
    hook=Path(hooks.name)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
    subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
    t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main');x=pub.NativePublication(s,o,t)
    original=rig.adapter.publication_delete_pin;deleted=[]
    def delete(name,**kwargs):
        original(name,**kwargs);deleted.append(name)
        if len(deleted)==cut: raise KeyboardInterrupt('refusal own retirement interruption')
    monkeypatch.setattr(rig.adapter,'publication_delete_pin',delete)
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;state=o.read();assert state['workers'][op]['phase']=='failed'
    proof=state['workers'][op]['publication_outcome'];assert proof['kind']=='no-update' and state['recovery']
    monkeypatch.setattr(rig.adapter,'publication_delete_pin',original);git(rig.root,'gc','--prune=now')
    reopened=NativeOperationStore(rig.root/'data',owner=o.owner);executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='refused'
    assert reopened.read()['recovery'] is None and reopened.read()['workers'][op]['publication_outcome']==proof


@pytest.mark.skipif(sys.platform!='linux',reason='R1 exact accepted nontrivial Git identity')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_accepted_unicode_interior_punctuation_matches_actual_git(tmp_path,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    kw['committer']=pub.Signature('Český Fixture; Interior','fixture+exact@invalid',SIG.timestamp,'+0530')
    assert x.publish(c,r,**kw)['status']=='completed'


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual own retirement issuer binding')
@pytest.mark.parametrize('tamper',['same-object','copy'])
def test_r1_retirement_compiler_rejects_changed_issued_handle(tmp_path,monkeypatch,tamper):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    original=rig.adapter.publication_retirement_proof
    def issue(*args,**kwargs):
        proof=original(*args,**kwargs)
        if tamper=='same-object': object.__setattr__(proof,'oid',kw['expected_local'])
        else: proof=replace(proof)
        return proof
    monkeypatch.setattr(rig.adapter,'publication_retirement_proof',issue)
    with pytest.raises(__import__('native_git').InvalidInput,match='issued unchanged') as error: x.publish(c,r,**kw)
    state=o.read();worker=state['workers'][error.value.operation_id]
    assert worker['phase']=='completed' and worker['publication_cleanup']==0 and state['recovery']
    intent=pub.validate_intent(o._prepared_publication.artifact)
    assert rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid
    assert rig.adapter.read_ref(intent.tree_pin).oid==intent.candidate.tree_oid


@pytest.mark.skipif(sys.platform!='linux',reason='R1 bounded canonical ledger with actual settlement')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_full_push_ledger_reserves_settlement_and_compacts_retired_receipts(tmp_path,object_format):
    import copy
    import native_operations as owner
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    store=NativeOperationStore(rig.root/'data',retention=2);executor=pub.NativePublication(s,store,t)
    def stop(point):
        if point=='publication-completed': raise KeyboardInterrupt('retained cleanup')
    store.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: executor.publish(c,r,**kw)
    op=error.value.operation_id;state=store.read();worker=state['workers'][op];record=state['records'][worker['record_id']]
    # Canonical owner grammar boundary fixture, not 128 real transport attempts.
    ids=[]
    for number in range(128):
        command=str(uuid.uuid4());ids.append(command)
        receipt={'command_id':command,'generation':record['generation'],'request_version':3,
            'operation_id':op,'owner':worker['owner'],'epoch':worker['epoch'],'pending_effect':'push'}
        receipt['payload_digest']=owner._publication_command_payload(record,worker,receipt)
        state['commands'][store._command_key(record['generation'],command)]=receipt
    with store._locked() as (root,artifacts):
        store._validate(state,artifacts);store._write(root,'state.json',pub.canonical(state))
    before=copy.deepcopy(state['commands']);store.fault=lambda point:None
    replacement=NativeOperationStore(rig.root/'data',retention=2);replacement.lifecycle(state['revision'],'r1-ledger-takeover',{})
    current=replacement.read();assert current['commands']==before
    executor=pub.NativePublication(s,replacement,t)
    with pytest.raises(InvalidState,match='capacity'): executor.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='push')
    with pytest.raises(Conflict,match='command'): executor.approve_recovery(op,expected_revision=current['revision'],command_id=ids[-1],pending_effect='cleanup')
    grant=executor.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='completed' and replacement.read()['recovery'] is None
    assert all(any(v['command_id']==command for v in replacement.read()['commands'].values()) for command in ids)
    result={'commit_oid':worker['publication_outcome']['tip']}
    for number in range(3):
        git(rig.root,'update-ref',rig.source.remote_ref,result['commit_oid'])
        rig.source=replace(rig.source,remote_oid=result['commit_oid'],base_oid=result['commit_oid'])
        put(rig.root,A,('ledger retirement change '+str(number)).encode());r=reviewed(rig,(A,));c=s.build(r)
        kw.update(expected_revision=replacement.read()['revision'],expected_local=result['commit_oid'],command_id=str(uuid.uuid4()))
        result=executor.publish(c,r,**kw)
    final=replacement.read();assert op not in final['workers']
    assert not any(receipt['command_id'] in ids for receipt in final['commands'].values())
    with pytest.raises(Conflict): executor.approve_recovery(op,expected_revision=final['revision'],command_id=ids[-1],pending_effect='cleanup')


@pytest.mark.skipif(sys.platform!='linux',reason='R1 fixed settlement bound fails closed')
def test_r1_full_settlement_ledger_keeps_diagnostics_and_pins_after_takeover(tmp_path):
    import native_operations as owner
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    def stop(point):
        if point=='publication-completed': raise KeyboardInterrupt('retained cleanup')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;state=o.read();worker=state['workers'][op];record=state['records'][worker['record_id']]
    ids=[]
    for number in range(16):
        command=str(uuid.uuid4());ids.append(command)
        receipt={'command_id':command,'generation':record['generation'],'request_version':3,
            'operation_id':op,'owner':worker['owner'],'epoch':worker['epoch'],'pending_effect':'cleanup'}
        receipt['payload_digest']=owner._publication_command_payload(record,worker,receipt)
        state['commands'][o._command_key(record['generation'],command)]=receipt
    with o._locked() as (root,artifacts):
        o._validate(state,artifacts);o._write(root,'state.json',pub.canonical(state))
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(state['revision'],'r1-settlement-cap-takeover',{})
    current=replacement.read();assert current['commands']==state['commands']
    executor=pub.NativePublication(s,replacement,t);intent=pub.validate_intent(o._prepared_publication.artifact)
    old=t.advertise();pins=tuple(rig.adapter.read_ref(name) for name in (intent.commit_pin,intent.tree_pin))
    with pytest.raises(InvalidState,match='capacity'): executor.approve_recovery(op,expected_revision=current['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(Conflict,match='command'): executor.approve_recovery(op,expected_revision=current['revision'],command_id=ids[-1],pending_effect='cleanup')
    assert replacement.read()==current and t.advertise()==old
    assert tuple(rig.adapter.read_ref(name) for name in (intent.commit_pin,intent.tree_pin))==pins
    assert executor.diagnose(op)['execution_authority'] is False


@pytest.mark.skipif(sys.platform!='linux',reason='R1 exact terminal cleanup lifecycle authority')
@pytest.mark.parametrize('terminal',['completed','failed'])
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_terminal_cleanup_takeover_preserves_first_proof_and_invalidates_old_grant(tmp_path,request,object_format,terminal):
    import copy
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    if terminal=='failed':
        hooks=tempfile.TemporaryDirectory(prefix='n611-r1-takeover-hooks-',dir='/workspace/output');request.addfinalizer(hooks.cleanup)
        hook=Path(hooks.name)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
        t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main');x=pub.NativePublication(s,o,t)
    def stop(point):
        if point=='publication-pin-retirement-1': raise KeyboardInterrupt('before exact own deletion')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;initial=o.read();original=copy.deepcopy(initial['workers'][op]);record=copy.deepcopy(initial['records'][original['record_id']])
    o.fault=lambda point:None
    old=x.approve_recovery(op,expected_revision=initial['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    intent=pub.validate_intent(old.prepared.artifact);before=tuple(rig.adapter.read_ref(name) for name in (intent.commit_pin,intent.tree_pin));remote=t.advertise()
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(initial['revision'],'r1-terminal-version-update',{})
    state=replacement.read();worker=state['workers'][op]
    assert worker['publication_cleanup_owner']=={'owner':replacement.owner,'epoch':state['epoch']}
    assert {k:v for k,v in worker.items() if k!='publication_cleanup_owner'}=={k:v for k,v in original.items() if k!='publication_cleanup_owner'}
    assert state['records'][worker['record_id']]['checkpoints']==record['checkpoints']
    assert tuple(rig.adapter.read_ref(name) for name in (intent.commit_pin,intent.tree_pin))==before and t.advertise()==remote
    with pytest.raises(Exception): x.resume(old)
    assert replacement.read()==state
    for changed in ({'owner':str(uuid.uuid4()),'epoch':original['epoch']},{'owner':replacement.owner,'epoch':state['epoch']-1}):
        bad=copy.deepcopy(state);bad['workers'][op]['publication_cleanup_owner']=changed
        with replacement._locked() as (root,artifacts):
            with pytest.raises(InvalidState): replacement._validate(bad,artifacts)
    executor=pub.NativePublication(s,replacement,t)
    with pytest.raises((Conflict,pub.PublicationError)): executor.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='push')
    grant=executor.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']==('completed' if terminal=='completed' else 'refused')
    final=replacement.read();assert final['recovery'] is None and final['workers'][op]['publication_outcome']==original['publication_outcome']


@pytest.mark.skipif(sys.platform!='linux',reason='R1 actual refusal owner durability boundaries')
@pytest.mark.parametrize('point',['state_written','state_fsynced','state_replaced','state_durable'])
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r1_refusal_durability_failure_never_unpins_before_owner_receipt(tmp_path,monkeypatch,request,object_format,point):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    hooks=tempfile.TemporaryDirectory(prefix='n611-r1-refusal-durability-',dir='/workspace/output');request.addfinalizer(hooks.cleanup)
    hook=Path(hooks.name)/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
    subprocess.run(['git','--git-dir='+str(ep),'config','core.hooksPath',hooks.name],check=True)
    t=pub.GitCLITransport(rig.adapter,os.fsencode(ep),'refs/heads/main');x=pub.NativePublication(s,o,t)
    original=o._write;armed=[];fired=[]
    def write(fd,name,payload,**kwargs):
        if name=='state.json' and any(w['phase']=='failed' for w in pub._parse(payload)['workers'].values()): armed.append(True)
        try: return original(fd,name,payload,**kwargs)
        finally: armed.clear()
    def fault(current):
        if armed and current==point and not fired: fired.append(True);raise OSError(5,'refusal durability '+point)
    monkeypatch.setattr(o,'_write',write);o.fault=fault
    with pytest.raises(OSError,match='refusal durability'): x.publish(c,r,**kw)
    intent=pub.validate_intent(o._prepared_publication.artifact)
    assert fired and rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid
    assert rig.adapter.read_ref(intent.tree_pin).oid==intent.candidate.tree_oid
    git(rig.root,'gc','--prune=now');assert rig.adapter.exact_commit(intent.commit_oid)[1]==c.tree_oid
    current=NativeOperationStore(rig.root/'data',owner=o.owner).read()
    worker=current['workers'][intent.namespace['operation_id']]
    if worker['phase']=='failed':
        assert worker['publication_outcome']['tip']==t.advertise().oid and worker['publication_cleanup']==0 and current['recovery']


@pytest.mark.skipif(sys.platform!='linux',reason='R1 retained positive proof is not fresh CAS authority')
def test_r1_reopened_positive_history_cannot_directly_grant_local_cas(tmp_path):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    def stop(point):
        if point=='publication-proof': raise KeyboardInterrupt('before local CAS')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;reopened=NativeOperationStore(rig.root/'data',owner=o.owner);executor=pub.NativePublication(s,reopened,t)
    grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()))
    prepared=grant.prepared;intent=pub.validate_intent(prepared.artifact)
    with rig.adapter.publication_local(),executor._admission(prepared,intent) as admission:
        admission.create_publication_directories(prepared);reopened.publication_command(prepared,admission)
        with pytest.raises(__import__('native_git').InvalidInput,match='current positive'):
            rig.adapter.publication_ref(intent.local_ref,intent.commit_oid,expected_old=intent.expected_local,admission=admission,committer=intent.committer)
    assert rig.adapter.read_ref(intent.local_ref).oid==kw['expected_local']


@pytest.mark.skipif(sys.platform!='linux',reason='R1 cleanup authority cannot create Git objects')
def test_r1_terminal_cleanup_cannot_call_commit_writer(tmp_path,monkeypatch):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    def stop(point):
        if point=='publication-completed': raise KeyboardInterrupt('terminal own cleanup')
    o.fault=stop
    with pytest.raises(KeyboardInterrupt) as error: x.publish(c,r,**kw)
    o.fault=lambda point:None
    grant=x.approve_recovery(error.value.operation_id,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    prepared=grant.prepared;intent=pub.validate_intent(prepared.artifact)
    def object_write(*args,**kwargs): raise AssertionError('terminal cleanup reached object writer')
    monkeypatch.setattr(rig.adapter,'_preflight_object_write',object_write)
    with rig.adapter.publication_local(),x._admission(prepared,intent) as admission:
        admission.create_publication_directories(prepared);o.publication_command(prepared,admission)
        with pytest.raises(__import__('native_git').InvalidInput,match='initial unsent'):
            rig.adapter.write_exact_commit(intent.raw_commit,admission=admission)

@pytest.mark.skipif(sys.platform!='linux',reason='R2 actual native owner/primitive closure')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('route',['protection','checkpoint','lock','parent'])
def test_r2_native_authority_and_residual_cleanup(tmp_path,monkeypatch,object_format,route):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    real_unlink=os.unlink; real_fsync=os.fsync; hits=[]; armed=[False]
    if route in ('protection','checkpoint'):
        cut='publication-completed' if route=='protection' else 'publication-proof'
        o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('R2 authority cut')) if point==cut else None
    elif route=='lock':
        def unlink(name,*args,**kwargs):
            if name==b'commit.lock' and kwargs.get('dir_fd') is not None:
                hits.append(True); raise OSError(5,'R2 persistent own lock unlink')
            return real_unlink(name,*args,**kwargs)
        monkeypatch.setattr(os,'unlink',unlink)
    else:
        def unlink(name,*args,**kwargs):
            result=real_unlink(name,*args,**kwargs)
            if name==b'tree' and kwargs.get('dir_fd') is not None: armed[0]=True
            return result
        def fsync(fd):
            if armed[0] and os.path.isdir('/proc/self/fd/'+str(fd)) and '/refs/ha-ops/native-publication/' in os.readlink('/proc/self/fd/'+str(fd)):
                hits.append(True); raise OSError(5,'R2 persistent own parent durability')
            return real_fsync(fd)
        monkeypatch.setattr(os,'unlink',unlink);monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises((KeyboardInterrupt,OSError)) as error: x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None;state=o.read()
    if route=='protection':
        for target in (None,state['recovery']['record_id']):
            with pytest.raises(Conflict,match='native publication'):
                o.recovery_pin(state['revision'],state['recovery'],target,
                    None if target is None else state['recovery']['identity_digest'])
        assert o.read()==state
        return
    replacement=NativeOperationStore(rig.root/'data',owner=o.owner)
    if route=='checkpoint':
        replacement.lifecycle(state['revision'],'R2 owner update',{}); state=replacement.read();w=state['workers'][op];record=state['records'][w['record_id']]
        with pytest.raises(Conflict,match='native publication'):
            replacement.checkpoint(state['revision'],op,w['epoch'],'completed',evidence=dict(record['identity']['effect'],marker=w['marker']))
        assert replacement.read()==state and rig.adapter.read_ref(kw['local_ref']).oid!=w['publication_outcome']['tip']
        return
    executor=pub.NativePublication(s,replacement,t)
    grant=executor.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(OSError): executor.resume(grant)
    assert len(hits)>=2 and replacement.read()['recovery']
    monkeypatch.setattr(os,'unlink',real_unlink);monkeypatch.setattr(os,'fsync',real_fsync)
    again=NativeOperationStore(rig.root/'data',owner=o.owner);executor=pub.NativePublication(s,again,t)
    grant=executor.approve_recovery(op,expected_revision=again.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='completed' and again.read()['recovery'] is None
    intent=pub.validate_intent(o._prepared_publication.artifact)
    assert not any((rig.root/METADATA/(name+'.lock')).exists() for name in (intent.commit_pin,intent.tree_pin))

@pytest.mark.skipif(sys.platform!='linux',reason='R2 real generic canonical owner closure')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r2_every_generic_owner_route_preserves_native_debt(tmp_path,object_format):
    from unittest.mock import Mock
    from test_native_operations import identity, observed
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('R2 terminal')) if point=='publication-completed' else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    o.fault=lambda point:None;state=o.read();w=state['workers'][error.value.operation_id];rec=state['records'][w['record_id']];producer=Mock()
    calls=[lambda:o.preview(state['revision'],'save',rec['identity'],b'caller bytes'),
        lambda:o.preview_stream(state['revision'],'save',rec['identity'],producer),
        lambda:o.preview_stream(state['revision'],'save',identity(),producer),
        lambda:o.review(state['revision'],rec['id'],rec['decisions'],str(uuid.uuid4()),observed(rec['identity'])),
        lambda:o.claim(state['revision'],str(uuid.uuid4()),rec['id'],w['binding'],observed(rec['identity']),instance_id=rec['instance_id'],generation=rec['generation']),
        lambda:o.checkpoint(state['revision'],w['id'],w['epoch'],'completed',evidence=w['evidence']),
        lambda:o.recovery_pin(state['revision'],state['recovery']),lambda:o.cancel(state['revision'],rec['id'])]
    for call in calls:
        with pytest.raises(Conflict):call()
        assert o.read()==state
    producer.assert_not_called()
    for value in (o.refresh(),o.accounting(),o.upgrade(state['revision'])):assert value is not None
    before=w.copy();o.compact(state['revision']);after=o.read()
    assert after['workers'][w['id']]==before and after['recovery']==state['recovery'] and after['commands']==state['commands']
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(after['revision'],'R2 cleanup takeover',{})
    later=replacement.read();assert later['recovery']==state['recovery'] and later['commands']==state['commands']
    assert {k:v for k,v in later['workers'][w['id']].items() if k!='publication_cleanup_owner'}=={k:v for k,v in w.items() if k!='publication_cleanup_owner'}


@pytest.mark.skipif(sys.platform!='linux',reason='R2 actual own cleanup syscall boundaries')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('cut',['lock-created','resource','pin-unlink','lock-unlink','parent-fsync','pin-ack','lock-close','packed-resource','packed-replace','packed-parent','packed-ack','packed-close'])
def test_r2_cleanup_primitive_boundary_closure(tmp_path,monkeypatch,object_format,cut):
    packed=cut.startswith('packed-')
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('before boundary')) if point=='publication-completed' else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None;state=o.read();intent=pub.validate_intent(o._prepared_publication.artifact)
    if packed:
        git(rig.root,'update-ref','refs/foreign/r2-keep',kw['expected_local']);git(rig.root,'gc','--prune=now')
    hits=[];armed=[True];replaced=[False]
    unlink=os.unlink;fsync=os.fsync;write=os.write;replace=os.replace;close=os.close
    ack=disk.Admission.acknowledge_publication_pin_deletion;pack_ack=disk.Admission.acknowledge_publication_packed
    def route(fd):
        try:return os.readlink('/proc/self/fd/'+str(fd))
        except OSError:return ''
    def fail():
        hits.append(True);raise OSError(5,'R2 boundary '+cut)
    def write_cut(fd,payload):
        if armed[0] and cut=='lock-created' and route(fd).endswith('/commit.lock'):fail()
        return write(fd,payload)
    def unlink_cut(name,*args,**kwargs):
        if armed[0] and kwargs.get('dir_fd') is not None and ((cut=='pin-unlink' and name==b'commit') or (cut=='lock-unlink' and name==b'commit.lock')):fail()
        return unlink(name,*args,**kwargs)
    def fsync_cut(fd):
        path=route(fd)
        if armed[0] and cut=='parent-fsync' and '/refs/ha-ops/native-publication/' in path and os.path.isdir(path) and not (Path(path)/'commit').exists() and (Path(path)/'commit.lock').exists():fail()
        if armed[0] and cut=='packed-parent' and replaced[0] and path==str(rig.root/METADATA):fail()
        return fsync(fd)
    def replace_cut(src,dst,*args,**kwargs):
        if src==b'packed-refs.lock':
            if armed[0] and cut=='packed-replace':fail()
            result=replace(src,dst,*args,**kwargs);replaced[0]=True;return result
        return replace(src,dst,*args,**kwargs)
    def close_cut(fd):
        path=route(fd);result=close(fd)
        if armed[0] and ((cut=='lock-close' and path.endswith('/commit.lock (deleted)')) or (cut=='packed-close' and replaced[0] and path.endswith('/packed-refs'))):fail()
        return result
    def ack_cut(admission,name,*args,**kwargs):
        result=ack(admission,name,*args,**kwargs)
        if armed[0] and cut=='pin-ack' and name==intent.commit_pin:fail()
        return result
    def pack_ack_cut(admission,name,*args,**kwargs):
        result=pack_ack(admission,name,*args,**kwargs)
        if armed[0] and cut=='packed-ack':fail()
        return result
    def owner_cut(point):
        if armed[0] and point=='publication-resource-'+('commit-packed' if cut=='packed-resource' else 'commit') and cut in ('resource','packed-resource'):fail()
    with monkeypatch.context() as patch:
        patch.setattr(os,'write',write_cut);patch.setattr(os,'unlink',unlink_cut);patch.setattr(os,'fsync',fsync_cut);patch.setattr(os,'replace',replace_cut);patch.setattr(os,'close',close_cut)
        patch.setattr(disk.Admission,'acknowledge_publication_pin_deletion',ack_cut);patch.setattr(disk.Admission,'acknowledge_publication_packed',pack_ack_cut)
        o.fault=owner_cut
        grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
        with pytest.raises(OSError):x.resume(grant)
        pending=o.read();assert hits and pending['recovery'] and pending['workers'][op]['publication_cleanup']<4
        first=pending['workers'][op]['publication_outcome']
        # Unknown creation-before-record never gains ownership from its path.
        if cut=='lock-created':
            armed[0]=False
            executor=pub.NativePublication(s,NativeOperationStore(rig.root/'data',owner=o.owner),t)
            grant=executor.approve_recovery(op,expected_revision=executor.store.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
            with pytest.raises(Exception,match='unknown publication lock'):executor.resume(grant)
            assert (rig.root/METADATA/(intent.commit_pin+'.lock')).exists() and executor.store.read()['recovery']
            return
        # Retry persistent durability/unlink/ACK errors: never manufacture ACK.
        if cut in ('pin-unlink','lock-unlink','parent-fsync','pin-ack','packed-replace','packed-parent'):
            reopened=NativeOperationStore(rig.root/'data',owner=o.owner);executor=pub.NativePublication(s,reopened,t)
            grant=executor.approve_recovery(op,expected_revision=reopened.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
            with pytest.raises(OSError):executor.resume(grant)
            assert reopened.read()['recovery'] and len(hits)>=2
        armed[0]=False
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(replacement.read()['revision'],'R2 explicit cleanup takeover',{})
    executor=pub.NativePublication(s,replacement,t)
    grant=executor.approve_recovery(op,expected_revision=replacement.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert executor.resume(grant)['status']=='completed'
    after=replacement.read();assert after['recovery'] is None and after['workers'][op]['publication_outcome']==first
    assert not any((rig.root/METADATA/(name+'.lock')).exists() for name in (intent.commit_pin,intent.tree_pin))
    assert not (rig.root/METADATA/'packed-refs.lock').exists()
    if packed:assert git(rig.root,'rev-parse','refs/foreign/r2-keep').strip().decode()==kw['expected_local']

@pytest.mark.skipif(sys.platform!='linux',reason='R2 actual immutable own resource capability')
@pytest.mark.parametrize('tamper',['copy','token','slot','fd','fabricate'])
def test_r2_resource_compiler_requires_unchanged_issuer_capability(tmp_path,monkeypatch,tamper):
    from native_git import PublicationResource
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    issue=rig.adapter.publication_resource_proof
    def changed(*args,**kwargs):
        proof=issue(*args,**kwargs)
        if tamper=='copy':return replace(proof)
        if tamper=='fabricate':return PublicationResource(proof.issuer,proof.prepared,proof.admission,proof.current,proof.slot,'1'*176,proof.descriptor,proof.parent,proof.path,proof.payload)
        field={'token':'token','slot':'slot','fd':'descriptor'}[tamper]
        object.__setattr__(proof,field,{'token':'1'*176,'slot':'tree','fd':-1}[tamper]);return proof
    monkeypatch.setattr(rig.adapter,'publication_resource_proof',changed)
    with pytest.raises(Exception,match='issued unchanged publication resource'):x.publish(c,r,**kw)
    state=o.read();worker=next(iter(state['workers'].values()));intent=pub.validate_intent(o._prepared_publication.artifact)
    assert worker['publication_cleanup']==1 and state['recovery']
    assert rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid and rig.adapter.read_ref(intent.tree_pin).oid==intent.candidate.tree_oid


def test_r2_resource_stat_unsigned_fixed_extent_and_strict_validation():
    from native_git import publication_resource_token,InvalidInput
    from native_operations import _publication_resources,PUBLICATION_RESOURCE_EMPTY,PUBLICATION_RESOURCE_SLOTS
    from types import SimpleNamespace
    info=SimpleNamespace(st_dev=1,st_ino=10,st_mode=0o100600,st_size=41,st_nlink=1,st_mtime_ns=3,st_ctime_ns=4,st_blocks=8)
    parent=SimpleNamespace(st_dev=1,st_ino=9,st_mode=0o40700)
    token=publication_resource_token(info,parent);assert len(token)==176 and token.isascii()
    for bad in (-1,2**64):
        with pytest.raises(InvalidInput,match='overflow'):publication_resource_token(SimpleNamespace(**dict(vars(info),st_ino=bad)),parent)
    for bad in (token[:-1],token+'0',token.upper(),token[:64]+'0000000000000002'+token[80:]):
        value=dict.fromkeys(PUBLICATION_RESOURCE_SLOTS,PUBLICATION_RESOURCE_EMPTY);value['commit']=bad
        with pytest.raises(InvalidState):_publication_resources(value)

@pytest.mark.skipif(sys.platform!='linux',reason='R2 actual foreign/unknown resource ownership')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('kind',['unknown','replaced'])
def test_r2_lock_name_and_exact_payload_never_substitute_for_ownership(tmp_path,object_format,kind):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    cut='publication-pin-retirement-1' if kind=='unknown' else 'publication-resource-commit'
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('own ownership cut')) if point==cut else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None;state=o.read();intent=pub.validate_intent(o._prepared_publication.artifact)
    lock=rig.root/METADATA/(intent.commit_pin+'.lock');payload=intent.commit_oid.encode()+b'\n'
    if kind=='replaced':
        replacement=lock.with_name('foreign-replacement');replacement.write_bytes(payload);replacement.chmod(0o600);os.replace(replacement,lock)
    else:lock.write_bytes(payload);lock.chmod(0o600)
    original=lock.stat();store=NativeOperationStore(rig.root/'data');store.lifecycle(state['revision'],'R2 foreign lock takeover',{})
    executor=pub.NativePublication(s,store,t);grant=executor.approve_recovery(op,expected_revision=store.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(Exception,match=('unknown' if kind=='unknown' else 'foreign')+' publication lock'):executor.resume(grant)
    assert lock.read_bytes()==payload and lock.stat().st_ino==original.st_ino
    assert store.read()['recovery'] and store.read()['workers'][op]['publication_cleanup']==1
    assert rig.adapter.read_ref(intent.commit_pin).oid==intent.commit_oid


@pytest.mark.skipif(sys.platform!='linux',reason='R2 owner recipes exact control revision chronology')
def test_r2_resource_and_retirement_owner_revisions_are_strictly_monotone(tmp_path):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path)
    revisions=[];resources=[]
    def trace(point):
        if point.startswith(('publication-resource-','publication-pin-retirement-')):
            state=o.read();revisions.append(state['revision']);resources.append(next(iter(state['workers'].values()))['publication_resources'].copy())
    o.fault=trace
    assert x.publish(c,r,**kw)['status']=='completed'
    assert len(revisions)==6 and revisions==sorted(set(revisions))
    from native_operations import PUBLICATION_RESOURCE_EMPTY
    assert resources[1]['commit']!=PUBLICATION_RESOURCE_EMPTY and resources[-1]['commit']==resources[1]['commit']

@pytest.mark.skipif(sys.platform!='linux',reason='R2 absence authorization is not resource ACK')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r2_authorized_but_never_acquired_resource_absence_refuses(tmp_path,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('before owned resource')) if point=='publication-pin-retirement-1' else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None;intent=pub.validate_intent(o._prepared_publication.artifact)
    git(rig.root,'update-ref','-d',intent.commit_pin)
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(Exception,match='unexplained absent publication resource'):x.resume(grant)
    assert o.read()['recovery'] and o.read()['workers'][op]['publication_cleanup']==1


def _r3_owned_descriptors(root):
    result={}
    for entry in os.listdir('/proc/self/fd'):
        try: route=os.readlink('/proc/self/fd/'+entry)
        except OSError: continue
        if route.startswith(str(root)):result[int(entry)]=route
    return result


@pytest.mark.skipif(sys.platform!='linux',reason='R3 actual publication descriptor ownership')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('packed',[False,True])
def test_r3_first_close_failure_finishes_all_other_owned_descriptors(tmp_path,monkeypatch,object_format,packed):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('before cleanup')) if point=='publication-completed' else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None
    if packed:
        git(rig.root,'update-ref','refs/foreign/r3-keep',kw['expected_local']);git(rig.root,'gc','--prune=now')
    before=_r3_owned_descriptors(rig.root);real_close=os.close;hits=[];fault=OSError(5,'R3 first close failure')
    def close(fd):
        try:route=os.readlink('/proc/self/fd/'+str(fd))
        except OSError:route=''
        real_close(fd)
        match=route.endswith('/packed-refs (deleted)') if packed else route.endswith('/commit.lock (deleted)')
        if match and not hits:hits.append(fd);raise fault
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with monkeypatch.context() as patch:
        patch.setattr(os,'close',close)
        with pytest.raises(OSError) as caught:x.resume(grant)
    after=_r3_owned_descriptors(rig.root);leaked={fd:route for fd,route in after.items() if fd not in before}
    # On the red baseline release only exactly observed test-owned descriptors.
    for fd in leaked:real_close(fd)
    assert hits and caught.value is fault and not leaked
    state=o.read();assert state['recovery'] and state['workers'][op]['publication_cleanup']==1
    grant=x.approve_recovery(op,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert x.resume(grant)['status']=='completed' and not _r3_owned_descriptors(rig.root)


@pytest.mark.skipif(sys.platform!='linux',reason='R3 actual registered own-lock receipt')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
def test_r3_recovered_lock_primary_identity_survives_secondary_close(tmp_path,monkeypatch,object_format):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('before cleanup')) if point=='publication-completed' else None
    with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;o.fault=lambda point:None;real_unlink=os.unlink;real_close=os.close
    def unlink(name,*args,**kwargs):
        if name==b'commit' and kwargs.get('dir_fd') is not None:raise OSError(5,'retain registered lock')
        return real_unlink(name,*args,**kwargs)
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with monkeypatch.context() as patch:
        patch.setattr(os,'unlink',unlink)
        with pytest.raises(OSError):x.resume(grant)
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    primary=OSError(5,'R3 original receipt fault');secondary=OSError(5,'R3 secondary lock close fault');hits=[]
    def acknowledge(*args,**kwargs):hits.append('primary');raise primary
    def close(fd):
        try:route=os.readlink('/proc/self/fd/'+str(fd))
        except OSError:route=''
        real_close(fd)
        if route.endswith('/commit.lock') and hits==['primary']:hits.append('secondary');raise secondary
    with monkeypatch.context() as patch:
        patch.setattr(disk.Admission,'acknowledge_publication_lock',acknowledge);patch.setattr(os,'close',close)
        with pytest.raises(OSError) as caught:x.resume(grant)
    assert hits==['primary','secondary'] and caught.value is primary
    assert any('publication-lock-close:' in item and str(secondary) in item for item in primary.cleanup_errors)
    assert o.read()['recovery'] and not _r3_owned_descriptors(rig.root)
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert x.resume(grant)['status']=='completed'


@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('command',['transport','raw','raw-stdin','raw-stdin-empty','copy'])
def test_r3_git_command_close_attempts_each_pipe_preserving_primary(tmp_path,monkeypatch,object_format,command):
    rig,ep,t=fixture(tmp_path,object_format)
    real_popen=subprocess.Popen;processes=[];fault=OSError(5,'R3 pipe close fault');attempts=[]
    def popen(*args,**kwargs):
        process=real_popen(*args,**kwargs);processes.append(process)
        stream=process.stdin if command.startswith('raw-stdin') else process.stdout;close=stream.close
        def bad_close():attempts.append(True);close();raise fault
        stream.close=bad_close;return process
    with monkeypatch.context() as patch:
        patch.setattr(subprocess,'Popen',popen)
        with pytest.raises(OSError) as caught:
            if command=='transport':t.advertise()
            elif command=='raw':rig.adapter._raw(('rev-parse','--show-object-format'))
            elif command=='raw-stdin':rig.adapter._raw(('hash-object','--stdin'),input=b'R3 raw stdin fixture')
            elif command=='raw-stdin-empty':rig.adapter._raw(('rev-parse','--show-object-format'))
            else:rig.adapter._copy_object(rig.source.remote_oid,b'commit',__import__('io').BytesIO())
    process=processes[0];closed=process.stderr.closed
    if not closed:process.stderr.close()
    assert attempts==[True] and caught.value is fault and process.stdout.closed and closed and process.poll() is not None


@pytest.mark.skipif(sys.platform!='linux',reason='R3 real no-send refusal and lifecycle continuation')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('initial',['no-pins','tree-only'])
@pytest.mark.parametrize('cut',[0,1,2,3])
@pytest.mark.parametrize('takeover',[False,True])
def test_r3_proved_no_send_abandonment_continues_terminal_cleanup(tmp_path,monkeypatch,object_format,initial,cut,takeover):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    def owner(point):
        if initial=='no-pins' and point=='publication-effect_started':raise KeyboardInterrupt('before pins')
    ref=rig.adapter.publication_ref
    def tree_ref(name,*args,**kwargs):
        result=ref(name,*args,**kwargs)
        if initial=='tree-only' and name.endswith('/tree'):raise KeyboardInterrupt('after tree before commit')
        return result
    o.fault=owner
    with monkeypatch.context() as patch:
        patch.setattr(rig.adapter,'publication_ref',tree_ref)
        with pytest.raises(KeyboardInterrupt) as error:x.publish(c,r,**kw)
    op=error.value.operation_id;refusal=o.publication_refusal
    def refused(*args,**kwargs):
        result=refusal(*args,**kwargs)
        if cut==0:raise KeyboardInterrupt('after durable no-send refusal')
        return result
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('after retirement')) if cut and point=='publication-pin-retirement-'+str(cut) else None
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='abandon')
    with monkeypatch.context() as patch:
        patch.setattr(o,'publication_refusal',refused)
        with pytest.raises(KeyboardInterrupt):x.resume(grant)
    state=o.read();proof=state['workers'][op]['publication_outcome'];commands=state['commands'].copy()
    replacement=NativeOperationStore(rig.root/'data',**({} if takeover else {'owner':o.owner}))
    if takeover:replacement.lifecycle(state['revision'],'R3 no-send cleanup takeover',{})
    executor=pub.NativePublication(s,replacement,t)
    with monkeypatch.context() as patch:
        patch.setattr(t,'push',lambda *args,**kwargs:pytest.fail('cleanup must never send'))
        patch.setattr(rig.adapter,'publication_ref',lambda *args,**kwargs:pytest.fail('cleanup must never advance local'))
        grant=executor.approve_recovery(op,expected_revision=replacement.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
        assert executor.resume(grant)['status']=='refused'
    current=replacement.read();assert current['recovery'] is None and current['workers'][op]['publication_cleanup']==4
    assert current['workers'][op]['publication_outcome']==proof
    assert all(current['commands'].get(key)==value for key,value in commands.items())
    assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local'] and t.advertise().oid==c.remote.remote_oid
    assert not _r3_owned_descriptors(rig.root)


@pytest.mark.parametrize('active_primary',[False,True])
def test_r3_owned_closes_preserve_first_error_and_address_every_secondary(active_primary):
    from native_git import _close_owned
    first=KeyboardInterrupt('original effect' if active_primary else 'first close')
    secondary=SystemExit('secondary close');attempts=[]
    def close(label,error=None):
        attempts.append(label)
        if error is not None:raise error
    resources=(('resource:first',lambda:close('first',secondary if active_primary else first)),
               ('resource:second',lambda:close('second',secondary)),
               ('resource:last',lambda:close('last')))
    if active_primary:_close_owned(resources,first)
    else:
        with pytest.raises(KeyboardInterrupt) as error:_close_owned(resources)
        assert error.value is first
    assert attempts==['first','second','last']
    debt=getattr(first,'cleanup_errors',())
    assert any('resource:second:' in item and str(secondary) in item for item in debt)
    if active_primary:assert any('resource:first:' in item for item in debt)


@pytest.mark.skipif(sys.platform!='linux',reason='R3 no-send absence never adopts unknown ownership')
@pytest.mark.parametrize('object_format',['sha1','sha256'])
@pytest.mark.parametrize('kind',['foreign-lock','creation-before-record'])
def test_r3_no_send_cleanup_preserves_unknown_lock_debt(tmp_path,monkeypatch,object_format,kind):
    rig,ep,t,r,s,c,o,x,kw=publication_fixture(tmp_path,object_format=object_format)
    ref=rig.adapter.publication_ref
    def tree_ref(name,*args,**kwargs):
        result=ref(name,*args,**kwargs)
        if kind=='creation-before-record' and name.endswith('/tree'):raise KeyboardInterrupt('tree-only')
        return result
    o.fault=lambda point: (_ for _ in ()).throw(KeyboardInterrupt('no pins')) if kind=='foreign-lock' and point=='publication-effect_started' else None
    with monkeypatch.context() as patch:
        patch.setattr(rig.adapter,'publication_ref',tree_ref)
        with pytest.raises(KeyboardInterrupt) as interrupted:x.publish(c,r,**kw)
    op=interrupted.value.operation_id;intent=pub.validate_intent(o._prepared_publication.artifact);real_write=os.write
    def write(fd,payload):
        try:route=os.readlink('/proc/self/fd/'+str(fd))
        except OSError:route=''
        if route.endswith('/tree.lock'):raise OSError(5,'created before ownership record')
        return real_write(fd,payload)
    refusal=o.publication_refusal
    def refused(*args,**kwargs):
        value=refusal(*args,**kwargs)
        if kind=='foreign-lock':raise KeyboardInterrupt('durable refusal')
        return value
    o.fault=lambda point:None
    grant=x.approve_recovery(op,expected_revision=o.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='abandon')
    with monkeypatch.context() as patch:
        patch.setattr(o,'publication_refusal',refused);patch.setattr(os,'write',write)
        with pytest.raises((OSError,KeyboardInterrupt)):x.resume(grant)
    lock=rig.root/METADATA/((intent.commit_pin if kind=='foreign-lock' else intent.tree_pin)+'.lock')
    if kind=='foreign-lock':lock.write_bytes(intent.commit_oid.encode()+b'\n');lock.chmod(0o600)
    content=lock.read_bytes();inode=lock.stat().st_ino;before=o.read()
    replacement=NativeOperationStore(rig.root/'data');replacement.lifecycle(before['revision'],'R3 unknown resource takeover',{})
    executor=pub.NativePublication(s,replacement,t)
    grant=executor.approve_recovery(op,expected_revision=replacement.read()['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    with pytest.raises(Exception,match='unknown publication lock'):executor.resume(grant)
    current=replacement.read();assert current['recovery'] and lock.read_bytes()==content and lock.stat().st_ino==inode
    assert current['workers'][op]['publication_outcome']==before['workers'][op]['publication_outcome']
    assert rig.adapter.read_ref(kw['local_ref']).oid==kw['expected_local'] and t.advertise().oid==c.remote.remote_oid
    assert not _r3_owned_descriptors(rig.root)
