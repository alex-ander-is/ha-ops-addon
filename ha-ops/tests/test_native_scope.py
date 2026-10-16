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
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_scope as scope
import native_save as save
import native_publication as pub
import native_disk as disk
import native_operations as operations
import native_metadata as metadata
from test_native_save import SaveRig,reviewed,put,A,B,canonical,content
from test_native_preview import tree_evidence


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


def test_constructor_and_diagnostic_are_inert(tmp_path):
    rig=SaveRig(tmp_path)
    saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    store=operations.NativeOperationStore(rig.root/'data')
    owner=scope.NativeScope(saver,store)
    assert owner.diagnose()==dict(transaction=None,apply_blocked=False,draft={})
    assert not (rig.root/'data/native-operations').exists()


def test_scope_policy_normalizes_and_does_not_allow_redundant_inputs():
    with pytest.raises(Exception,match='normalized'):
        save.ScopePolicy(1,(b'homeassistant',b'homeassistant/nested'),'a'*64)
    policy=save.ScopePolicy(3,(), 'a'*64)
    assert policy.document()==dict(revision=3,allowlist=[],exclude_digest='a'*64)


@pytest.mark.skipif(sys.platform!='linux',reason='Actual scope candidate allocation requires Linux')
class TestScope:
    def test_git_only_exclusion_preserves_live_and_old_policy(self,tmp_path):
        rig,saver,store,owner=fixture(tmp_path)
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant');old=store.read()['active_scope']
        candidate,review=owner.build((),expected_revision=store.read()['revision'])
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live
        actual=content(rig,candidate.tree_oid)
        assert set(actual)=={b'.gitignore',metadata.MANIFEST_PATH}
        assert metadata.decode(actual[metadata.MANIFEST_PATH],policy=rig.policy).entries==()
        assert actual[b'.gitignore']==review.rules
        assert store.read()['active_scope']==old and owner.diagnose()['apply_blocked']
        assert pub.validate_candidate(rig.adapter,candidate,review)
        complete=operations._parse(owner.reviewed_diff())
        assert complete['profile']=='native-scope-review-v1' and complete['excluded_unsaved']
        baseline=dict(pub._decode(complete['baseline_blobs']))
        assert baseline[metadata.MANIFEST_PATH]==content(rig,candidate.baseline_tree_oid)[metadata.MANIFEST_PATH]
        assert pub._decode(complete['baseline_missing'])==(b'.gitignore',)
        assert dict(pub._decode(complete['blobs']))[metadata.MANIFEST_PATH]==actual[metadata.MANIFEST_PATH]
        apply1=owner.fresh_apply_preview();put(rig.root,B,b'new apply observation')
        apply2=owner.fresh_apply_preview()
        assert apply2['snapshot'].snapshot_id!=apply1['snapshot'].snapshot_id and apply2['apply_blocked']
        assert apply2['draft']=={} and apply2['generation']==apply1['generation']+1
        owner.lifecycle('restart')
        assert owner.reviewed_diff()==operations.canonical(complete)
        assert owner.diagnose()['transaction'] is not None

    def test_include_current_bytes_preserve_unchanged_remote_and_fence_new_file(self,tmp_path):
        rig,saver,store,owner=fixture(tmp_path)
        added=b'addon_configs/new/value.yaml';put(rig.root,added,b'fresh inclusion')
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
        candidate,review=owner.build((b'homeassistant',b'addon_configs'),
            approved_parents=(b'addon_configs',b'addon_configs/new'),expected_revision=store.read()['revision'])
        actual=content(rig,candidate.tree_oid)
        assert actual[added]==b'fresh inclusion' and actual[A]==b'initial publication'
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live
        assert pub.validate_candidate(rig.adapter,candidate,review)
        put(rig.root,b'addon_configs/new/late.yaml',b'not reviewed')
        with pytest.raises(Exception,match='whole-scope'):
            saver._publication_fence(candidate,review)


def test_complete_exclusion_recipe_exists_before_any_effect(tmp_path,monkeypatch):
    rig,saver,store,owner=fixture(tmp_path,monkeypatch)
    before=canonical(rig);state=(rig.root/'data/native-operations/state.json').read_bytes()
    observed=[]
    class ForecastStop(Exception):pass
    def forecast(kind,snapshot,evidence):
        assert kind=='candidate' and evidence.scope_preparation is not None
        prepared=evidence.scope_preparation
        value=operations._parse(prepared.artifact)
        assert value['profile']=='native-scope-review-v1'
        removed=pub._decode(value['removed_before'])
        assert {row[0] for row in removed}=={A,B}
        assert dict((row[0],row[2]) for row in removed)[A]==b'base\n'
        assert set(evidence.candidate_removals)=={A,B}
        assert evidence.candidate_rules and evidence.candidate_manifest
        assert set(evidence.candidate_final)=={b'.gitignore',metadata.MANIFEST_PATH}
        observed.append(prepared)
        raise ForecastStop()
    monkeypatch.setattr(saver.disk,'project',forecast)
    with pytest.raises(ForecastStop):owner.build((),expected_revision=store.read()['revision'])
    assert observed and canonical(rig)==before
    assert (rig.root/'data/native-operations/state.json').read_bytes()==state
    assert not (rig.root/'data/native/git-resources').exists()

@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux candidate and persistence admission')
def test_lifecycle_gc_reopen_same_candidate_and_cancel(tmp_path):
    rig,saver,store,owner=fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    identity=(candidate.tree_oid,candidate.candidate_digest,owner.reviewed_diff())
    old=owner.active_policy();rules=owner.active_rules();pending=owner.diagnose()['transaction']
    owner.lifecycle('update')
    __import__('native_topology_helpers').git(rig.root,'gc','--prune=now')
    fresh=scope.NativeScope(save.NativeSave(rig.preview,disk.NativeDisk(rig.root)),store)
    reopened,new_review=fresh.reopen_review(expected_revision=store.read()['revision'])
    assert (reopened.tree_oid,reopened.candidate_digest,fresh.reviewed_diff())==identity
    assert fresh.active_policy()==old and fresh.active_rules()==rules
    assert fresh.diagnose()['transaction']['id']==pending['id']
    live=tree_evidence(rig.root/'homeassistant')
    result=fresh.cancel(expected_revision=store.read()['revision'])
    assert result['transaction'] is None and result['active_policy']==old and not result['apply_blocked']
    assert tree_evidence(rig.root/'homeassistant')==live


@pytest.mark.skipif(sys.platform!='linux',reason='Actual Linux native publication and canonical activation')
@pytest.mark.parametrize('retention',[0,32])
def test_publication_scope_domain_cleanup_activation_and_second_revision(tmp_path,retention):
    rig,saver,store,owner=fixture(tmp_path,retention=retention)
    old=owner.active_policy();oldrules=owner.active_rules()
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    endpoint=tmp_path/'receiver.git'
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')
    executor=pub.NativePublication(saver,store,transport)
    sig=pub.Signature('Scope Fixture','fixture@invalid',1700000001,'+0000')
    def cut(point):
        if point=='publication-completed':raise KeyboardInterrupt('before final scope cleanup')
    store.fault=cut
    with pytest.raises(KeyboardInterrupt) as error:
        owner.confirm(candidate,review,executor,author=sig,committer=sig,message=b'publish exact empty scope\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
    operation_id=error.value.operation_id
    assert owner.active_policy()==old and owner.active_rules()==oldrules and owner.diagnose()['apply_blocked']
    state=store.read();record=state['records'][state['workers'][operation_id]['record_id']]
    assert record['domain']=='scope' and state['workers'][operation_id]['phase']=='completed'
    store.fault=lambda point:None
    result=owner.recover_publication(executor,operation_id,expected_revision=state['revision'],command_id=str(uuid.uuid4()),pending_effect='cleanup')
    assert result['status']=='completed'
    assert owner.active_policy()['revision']==1 and owner.active_policy()['allowlist']==[]
    assert owner.active_rules()==review.rules and not owner.diagnose()['apply_blocked']
    oid=result['commit_oid']
    __import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
    rig.source=replace(rig.source,scope=(),remote_oid=oid,base_oid=oid,scope_revision='scope-2')
    # Re-inclusion captures current live bytes, without restoring history.
    again,again_review=owner.build((b'homeassistant',),approved_parents=(b'homeassistant',b'homeassistant/p'),expected_revision=store.read()['revision'])
    assert again_review.old.revision==1 and again_review.new.revision==2
    assert content(rig,again.tree_oid)[A]==b'initial reviewed divergence'
    historical=next(row for row in again_review.history if row.path==A)
    assert historical.commit_oid==candidate.remote.remote_oid
    assert historical.content==b'initial publication' and historical.state.record.path==A
    assert {row.path for row in historical.parents}==metadata.ancestors((A,))
    assert next(row for row in again_review.selected if row.path==A).base==historical.state
    assert pub._decode(operations._parse(owner.reviewed_diff())['review']).history==again_review.history
    assert owner.active_rules()==review.rules
    full=operations._parse(owner.reviewed_diff())
    assert dict(pub._decode(full['baseline_blobs']))[b'.gitignore']==review.rules
    owner.lifecycle('refresh')
    store.compact(store.read()['revision'])
    assert owner.active_rules()==review.rules
    owner.cancel(expected_revision=store.read()['revision'])
    assert owner.active_rules()==review.rules and owner.active_policy()['revision']==1


@pytest.mark.skipif(sys.platform!='linux',reason='Actual separate old-scope Save and publication')
@pytest.mark.parametrize('starting_revision',[0,1])
def test_optional_final_save_requires_confirmation_and_scope_rebuild(tmp_path,starting_revision):
    rig,saver,store,owner=fixture(tmp_path)
    if starting_revision:
        first,first_review=owner.build((b'homeassistant',),expected_revision=store.read()['revision'])
        t=pub.GitCLITransport(rig.adapter,os.fsencode(tmp_path/'receiver.git'),'refs/heads/main')
        x=pub.NativePublication(saver,store,t);sig=pub.Signature('Scope Fixture','fixture@invalid',1700000001,'+0000')
        result=owner.confirm(first,first_review,x,author=sig,committer=sig,message=b'policy-only first scope revision\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
        oid=result['commit_oid'];__import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
        rig.source=replace(rig.source,remote_oid=oid,base_oid=oid,scope_revision='scope-2')
    untracked=b'homeassistant/p/new.yaml';put(rig.root,untracked,b'never tracked before exclusion')
    original,original_review=owner.build((),expected_revision=store.read()['revision'])
    offer=owner.last_save_offer()
    assert {row[0] for row in offer}=={A,untracked}
    before=canonical(rig)
    last_saver,candidate,review=owner.review_last_save(selected=(A,))
    assert canonical(rig)==before and candidate.active_policy.revision==starting_revision
    endpoint=tmp_path/'receiver.git';transport=pub.GitCLITransport(rig.adapter,os.fsencode(endpoint),'refs/heads/main')
    executor=pub.NativePublication(last_saver,store,transport)
    sig=pub.Signature('Scope Fixture','fixture@invalid',1700000002,'+0000')
    result=owner.confirm_last_save(last_saver,candidate,review,executor,author=sig,committer=sig,message=b'explicit final old-scope Save\n',
        local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
    assert result['status']=='completed' and owner.diagnose()['transaction'] is not None
    with pytest.raises(operations.Conflict,match='issued-current'):
        owner.confirm(original,original_review,pub.NativePublication(saver,store,transport))
    oid=result['commit_oid'];__import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
    rig.source=replace(rig.source,remote_oid=oid,base_oid=oid)
    replacement,replacement_review=owner.build((),expected_revision=store.read()['revision'])
    assert replacement.remote.remote_oid==oid and replacement.candidate_digest!=original.candidate_digest
    assert {row[0] for row in owner.last_save_offer()}=={untracked}

@pytest.mark.skipif(sys.platform!='linux' or not Path('/workspace/output').is_dir(),reason='Authenticated isolated Linux TLS receiver')
@pytest.mark.parametrize('failure',['none','receiver-reject','lost-ack','accepted-readback-loss'])
@pytest.mark.parametrize('protocol',['https','ssh'])
def test_scope_authenticated_network_publication_and_same_commit_recovery(tmp_path,monkeypatch,failure,protocol):
    from network_publication_helpers import tls_server,ssh_server,command,isolated_environment
    if protocol=='ssh' and (os.geteuid()!=0 or failure=='accepted-readback-loss'):
        pytest.skip('root SSH fixture; independent graph-loss recovery is qualified through HTTPS')
    import tempfile
    rig,saver,store,owner=fixture(tmp_path)
    old=owner.active_policy();oldrules=owner.active_rules()
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    endpoint=tmp_path/'receiver.git'
    command(['git','--git-dir='+str(endpoint),'config','http.receivepack','true'],env=isolated_environment())
    control={}
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
                with pytest.raises(pub.PublicationError,match='graph fetch unavailable') as error:owner.confirm(candidate,review,executor,**kwargs)
                operation_id=error.value.operation_id;state=store.read()
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
                fresh_owner=scope.NativeScope(saver,replacement)
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
def test_whole_scope_fence_and_forged_grants_refuse_without_effect(tmp_path,change):
    rig,saver,store,owner=fixture(tmp_path)
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

@pytest.mark.skipif(sys.platform!='linux',reason='Actual unsafe tracked baseline refusal on Linux')
def test_unsafe_tracked_baseline_blocks_without_writes(tmp_path):
    from native_topology_helpers import git
    rig,saver,store,owner=fixture(tmp_path)
    secret=b'homeassistant/secrets.yaml';put(rig.root,secret,b'fixture-only-disallowed-content')
    git(rig.root,'add','-f','--',os.fsdecode(secret));git(rig.root,'commit','-m','fixture incompatible tracked path')
    oid=git(rig.root,'rev-parse','HEAD').decode().strip()
    git(rig.root,'update-ref',rig.source.remote_ref,oid);git(rig.root,'update-ref',rig.source.base_ref,oid)
    rig.source=replace(rig.source,remote_oid=oid,base_oid=oid)
    before=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
    state=(rig.root/'data/native-operations/state.json').read_bytes()
    with pytest.raises(Exception,match='complete-safe-scope-observation'):
        owner.build((),expected_revision=store.read()['revision'])
    assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live
    assert (rig.root/'data/native-operations/state.json').read_bytes()==state

@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='Root-capable Linux full owner/group/mode proof')
def test_inclusion_preserves_full_numeric_owner_group_and_mode(tmp_path):
    rig,saver,store,owner=fixture(tmp_path)
    path=b'addon_configs/owned/value';put(rig.root,path,b'exact attributed bytes')
    os.chown(rig.root/'addon_configs',123,456);os.chmod(rig.root/'addon_configs',0o2751)
    os.chown(rig.root/'addon_configs/owned',234,567);os.chmod(rig.root/'addon_configs/owned',0o1750)
    os.chown(rig.root/os.fsdecode(path),345,678);os.chmod(rig.root/os.fsdecode(path),0o6751)
    live=tree_evidence(rig.root/'addon_configs');before=canonical(rig)
    candidate,review=owner.build((b'addon_configs',b'homeassistant'),approved_parents=(b'addon_configs',b'addon_configs/owned'),expected_revision=store.read()['revision'])
    records=metadata.decode(content(rig,candidate.tree_oid)[metadata.MANIFEST_PATH],policy=rig.policy).by_path
    assert (records[path].uid,records[path].gid,records[path].mode)==(345,678,0o6751)
    assert (records[b'addon_configs'].uid,records[b'addon_configs'].gid,records[b'addon_configs'].mode)==(123,456,0o2751)
    assert canonical(rig)==before and tree_evidence(rig.root/'addon_configs')==live
    assert pub.validate_candidate(rig.adapter,candidate,review)

def scope_import_guard(sources):
    for name,source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.Import):modules=[p for alias in node.names for p in alias.name.split('.')]
            elif isinstance(node,ast.ImportFrom):modules=(node.module or '').split('.')+[p for alias in node.names for p in alias.name.split('.')]
            else:continue
            assert 'native_scope' not in modules or name=='native_publication.py',name


def test_scope_import_contour_is_exact_and_detects_production_aliases():
    app=Path(scope.__file__).parent
    scope_import_guard({p.name:p.read_text() for p in app.glob('*.py') if p.name!='native_scope.py'})
    for source in ('import app.native_scope as alias','from app import native_scope',
                   'def lazy():\n    from .native_scope import NativeScope'):
        with pytest.raises(AssertionError):scope_import_guard({'server.py':source})

@pytest.mark.skipif(sys.platform!='linux',reason='Real native scope admission and publication')
def test_scope_lifecycle_fences_other_owner_issued_confirmation(tmp_path,monkeypatch):
    rig,saver,store,owner=fixture(tmp_path)
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
    fresh_owner=scope.NativeScope(fresh_saver,replacement)
    restored,new_review=fresh_owner.reopen_review(expected_revision=state['revision'])
    assert restored.tree_oid==candidate.tree_oid and restored.candidate_digest==candidate.candidate_digest
    assert fresh_owner.reviewed_diff()==old_diff
    with fresh_saver.publication_context(restored,new_review):pass


@pytest.mark.skipif(sys.platform!='linux',reason='Actual optional final Save lifecycle fence')
def test_optional_final_save_grant_expires_on_canonical_lifecycle(tmp_path,monkeypatch):
    rig,saver,store,owner=fixture(tmp_path)
    owner.build((),expected_revision=store.read()['revision'])
    last_saver,candidate,review=owner.review_last_save(selected=(A,))
    replacement=operations.NativeOperationStore(rig.root/'data')
    replacement.lifecycle(store.read()['revision'],'optional-save-restart',{})
    state=replacement.read()
    t=pub.GitCLITransport(rig.adapter,os.fsencode(tmp_path/'receiver.git'),'refs/heads/main')
    x=pub.NativePublication(last_saver,store,t);sig=pub.Signature('Fixture','fixture@invalid',1700000004,'+0000')
    monkeypatch.setattr(t,'advertise',lambda:pytest.fail('old optional Save reached transport'))
    with pytest.raises((operations.Conflict,__import__('native_preview').Stale)):
        x.publish(candidate,review,author=sig,committer=sig,message=b'expired optional Save\n',
            local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=state['revision'])
    assert replacement.read()==state


def test_last_managed_context_explicit_absence_and_metadata_from_ancestry(tmp_path):
    from native_topology_helpers import git
    rig=SaveRig(tmp_path);original=rig.source.remote_oid
    empty=rig.commit({})
    rows=scope.last_managed_context(rig.adapter,empty,(A,b'homeassistant/never'))
    old=next(row for row in rows if row.path==A)
    never=next(row for row in rows if row.path!=A)
    assert old.commit_oid==original and old.content==b'base\n'
    assert old.state.record.path==A and {r.path for r in old.parents}==metadata.ancestors((A,))
    assert never.commit_oid is None and never.state is None and never.content is None
    assert not content(rig,empty).get(A)

def test_last_managed_context_keeps_merge_peers_and_refuses_missing_metadata(tmp_path):
    from native_topology_helpers import git
    rig=SaveRig(tmp_path);original=rig.source.remote_oid
    left=rig.commit({A:b'left historical bytes'})
    git(rig.root,'update-ref','HEAD',original)
    right=rig.commit({A:b'right historical bytes'})
    empty=rig.commit({});tree=git(rig.root,'rev-parse',empty+'^{tree}').decode().strip()
    merge=git(rig.root,'commit-tree',tree,'-p',left,'-p',right,'-m','fixture historical merge').decode().strip()
    rows=scope.last_managed_context(rig.adapter,merge,(A,))
    assert {row.commit_oid for row in rows}=={left,right}
    assert {row.content for row in rows}=={b'left historical bytes',b'right historical bytes'}
    # A tracked leaf without its supported metadata cannot be misreported as
    # never managed or silently borrowed from a different historical revision.
    git(rig.root,'update-ref','HEAD',left)
    git(rig.root,'read-tree',left)
    git(rig.root,'rm','--cached','--',os.fsdecode(metadata.MANIFEST_PATH))
    git(rig.root,'commit','-m','fixture unavailable historical metadata')
    broken=git(rig.root,'rev-parse','HEAD').decode().strip()
    with pytest.raises(Exception,match='last-managed-metadata-unavailable'):
        scope.last_managed_context(rig.adapter,broken,(A,))


@pytest.mark.skipif(sys.platform!='linux',reason='Actual exclusion publication and re-inclusion history')
def test_reinclusion_context_includes_files_deleted_while_excluded(tmp_path):
    rig,saver,store,owner=fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(tmp_path/'receiver.git'),'refs/heads/main')
    executor=pub.NativePublication(saver,store,transport)
    sig=pub.Signature('Scope Fixture','fixture@invalid',1700000030,'+0000')
    result=owner.confirm(candidate,review,executor,author=sig,committer=sig,
        message=b'fixture exclusion before live deletion\n',local_ref=rig.source.base_ref,
        expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
    assert result['status']=='completed'
    oid=result['commit_oid']
    __import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
    rig.source=replace(rig.source,scope=(),remote_oid=oid,base_oid=oid,scope_revision='scope-2')
    (rig.root/os.fsdecode(A)).unlink()
    put(rig.root,b'homeassistant/p/new.yaml',b'current new file')
    live=tree_evidence(rig.root/'homeassistant')
    again,again_review=owner.build((b'homeassistant',),approved_parents=(b'homeassistant',b'homeassistant/p'),
        expected_revision=store.read()['revision'])
    historical=next((row for row in again_review.history if row.path==A),None)
    assert historical is not None,'deleted formerly managed leaf is missing from re-inclusion comparison'
    assert historical.content==b'initial publication' and historical.current_present is False
    assert A not in content(rig,again.tree_oid),'historical comparison must never restore absent live bytes'
    assert tree_evidence(rig.root/'homeassistant')==live
    exact=owner.reviewed_diff()
    owner.lifecycle('refresh')
    reopened,new_review=owner.reopen_review(expected_revision=store.read()['revision'])
    assert owner.reviewed_diff()==exact and new_review.history==again_review.history


def test_rejected_replacement_build_never_unbinds_retained_scope_authority(tmp_path):
    rig=SaveRig(tmp_path)
    saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    retained=object()
    token=(object(),('retained-canonical-epoch',))
    saver._publication_receipt=retained
    saver._scope_authority=token
    with pytest.raises(Exception,match='exact-active-save-policy-required'):
        saver.build(None,active_policy=object())
    assert saver._publication_receipt is None or saver._scope_authority is token

@pytest.mark.skipif(sys.platform!='linux',reason='Actual scope/last-Save lifecycle publication fence')
@pytest.mark.parametrize('attempt',['scope-edit','reopen-admission','optional-save'])
def test_failed_scope_edit_does_not_restore_same_owner_expired_grant(tmp_path,monkeypatch,attempt):
    rig,saver,store,owner=fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    store.lifecycle(store.read()['revision'],'same-owner-epoch-revocation',{})
    with monkeypatch.context() as patch:
        if attempt=='reopen-admission':
            patch.setattr(saver.disk,'project',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('reopen admission refusal')))
        with pytest.raises(Exception):
            if attempt=='scope-edit':owner.build((b'../invalid',),expected_revision=store.read()['revision'])
            elif attempt=='reopen-admission':owner.reopen_review(expected_revision=store.read()['revision'])
            else:owner.review_last_save(selected=(A,),approved_parents=(b'unrelated',))
    with pytest.raises(Exception):
        with saver.publication_context(candidate,review):pass
    restored,current=owner.reopen_review(expected_revision=store.read()['revision'])
    assert restored.candidate_digest==candidate.candidate_digest
    with saver.publication_context(restored,current):pass


def test_history_universe_includes_absent_nested_leaves_and_new_paths(tmp_path):
    rig=SaveRig(tmp_path);original=rig.source.remote_oid
    empty=rig.commit({})
    rows=scope.scope_history_context(rig.adapter,empty,(),(b'homeassistant',),(b'homeassistant/p/new',))
    assert {row.path for row in rows}=={A,B,b'homeassistant/p/new'}
    assert {row.commit_oid for row in rows if row.path in (A,B)}=={original}
    nested=scope.scope_history_context(rig.adapter,empty,(b'homeassistant/p/a',),(b'homeassistant',),())
    assert {row.path for row in nested}=={B}
    assert not content(rig,empty).get(A) and not content(rig,empty).get(B)


@pytest.mark.parametrize('attempt',['invalid-policy','invalid-review','busy'])
def test_failed_save_attempt_retains_no_unbound_scope_receipt(tmp_path,attempt):
    rig=SaveRig(tmp_path);saver=save.NativeSave(rig.preview,disk.NativeDisk(rig.root))
    retained=object();authority=(object(),('expired-scope-token',))
    saver._publication_receipt=retained;saver._scope_authority=authority
    if attempt=='busy':rig.preview._lock.acquire()
    try:
        with pytest.raises(Exception):saver.build(None,active_policy=object() if attempt=='invalid-policy' else None)
    finally:
        if attempt=='busy':rig.preview._lock.release()
    assert saver._publication_receipt is None or saver._scope_authority is authority


@pytest.mark.skipif(sys.platform!='linux',reason='Complete folder-absence review and durable reissue')
def test_entire_reincluded_folder_absence_keeps_history_without_restoration(tmp_path):
    import shutil
    rig,saver,store,owner=fixture(tmp_path)
    candidate,review=owner.build((),expected_revision=store.read()['revision'])
    transport=pub.GitCLITransport(rig.adapter,os.fsencode(tmp_path/'receiver.git'),'refs/heads/main')
    x=pub.NativePublication(saver,store,transport);sig=pub.Signature('Fixture','fixture@invalid',1700000031,'+0000')
    result=owner.confirm(candidate,review,x,author=sig,committer=sig,message=b'exclude before folder absence\n',
        local_ref=rig.source.base_ref,expected_local=rig.source.base_oid,command_id=str(uuid.uuid4()),expected_revision=store.read()['revision'])
    oid=result['commit_oid'];__import__('native_topology_helpers').git(rig.root,'update-ref',rig.source.remote_ref,oid)
    rig.source=replace(rig.source,scope=(),remote_oid=oid,base_oid=oid,scope_revision='scope-2')
    shutil.rmtree(rig.root/'homeassistant')
    again,again_review=owner.build((b'homeassistant',),expected_revision=store.read()['revision'])
    assert {row.path for row in again_review.history}=={A,B}
    assert all(row.current_present is False for row in again_review.history)
    assert not again_review.selected and not (rig.root/'homeassistant').exists()
    assert set(content(rig,again.tree_oid))=={b'.gitignore',metadata.MANIFEST_PATH}
    exact=owner.reviewed_diff();owner.lifecycle('refresh')
    reopened,new_review=owner.reopen_review(expected_revision=store.read()['revision'])
    assert new_review.history==again_review.history and owner.reviewed_diff()==exact
    assert not (rig.root/'homeassistant').exists()
