"""Real isolated dual-format candidate proofs; no configured remote or live HA."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from unittest import mock

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_migration_git_candidate as c
import native_migration_transition as t
import native_migration as n
from test_native_migration import setup, git, write, binding, synthetic_host_allocation
from test_native_migration_transition import host_rename_adapter, begin

IDENTITY=b'Fixture <fixture@example.invalid> 1720000000 +0000'


def ready(tmp_path,fmt='sha1',mappings=None,policy=None,configure=lambda root,repo:None):
    root,repo,p=setup(tmp_path,fmt)
    if mappings is not None:
        p=n.Preparation(binding(root,policy),mappings=mappings,app_identity='fixture-app-1',manifest_digest='a'*64)
    configure(root,repo)
    parent=git(repo,'rev-parse','HEAD').strip().decode()
    git(repo,'update-ref','refs/remotes/origin/main',parent)
    write(repo,b'homeassistant/a',b'local commit');git(repo,'add','.');git(repo,'commit','-qm','local only')
    write(repo,b'homeassistant/a',b'staged');git(repo,'add','homeassistant/a')
    write(repo,b'homeassistant/a',b'working')
    write(repo,b'homeassistant/untracked',b'not selected')
    prepared=p.prepare(expected=None)
    assert not prepared['observation']['blockers']
    transition,operation,confirmation=begin(p,prepared)
    state=transition.start(candidate=prepared['digest'],operation=operation,confirmation=confirmation)
    return root,repo,p,prepared,state,parent


def build(p,prepared,state,parent,**kw):
    return c.Candidate(p).build(expected_preparation=prepared['digest'],transition_operation=state['operation'],
        parent_ref=b'refs/remotes/origin/main',parent_oid=parent,operation=kw.pop('operation',uuid.uuid4().hex),
        expected_candidate=None,identity_line=IDENTITY,**kw)


def inventory(root):
    result={}
    for base,dirs,files in os.walk(root):
        if base==str(root/'data'):
            dirs[:]=[d for d in dirs if d!='native-migration-git-candidate']
        for name in dirs+files:
            p=Path(base)/name;relative=p.relative_to(root)
            if str(relative) in ('data','data/native-migration-git-candidate'):continue
            s=p.lstat();result[str(relative)]=(s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_mtime_ns,s.st_ctime_ns,p.read_bytes() if p.is_file() else None)
    return result


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_remote_parent_exact_tree_full_review_and_reopen(tmp_path,fmt):
    root,repo,p,prepared,state,parent=ready(tmp_path,fmt)
    before=inventory(root);owner=c.Candidate(p)
    assert owner.read() is None
    candidate=build(p,prepared,state,parent)
    assert inventory(root)==before
    assert c.Candidate(p).read()==candidate
    assert c.Candidate(p).revalidate()=='current'
    assert t.Transition(p).completed_evidence(candidate=prepared['digest'],operation=state['operation'])==state
    nested=root/os.fsdecode(c.STORE)/candidate['operation']/'root/data/native/repository.git'
    assert git(nested,'--git-dir=.', 'rev-list','--parents','-1',candidate['commit']).split()==[candidate['commit'].encode(),parent.encode()]
    rows=candidate['rows'];a=next(i for i,r in enumerate(rows) if r['after'] and n.decode_path(r['after']['path'])==b'homeassistant/a')
    output=io.BytesIO();owner.read_content(candidate=candidate['candidate'],row=a,side='after',destination=output)
    assert output.getvalue()==b'old'
    assert any(r['action']=='unchanged' for r in rows)
    assert not (nested/'index').exists()
    assert not (root/'data/native/repository-files/etckeeper-metadata.json').exists()
    assert not (root/'data/native/repository.git/index').exists()
    assert owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],parent_ref=b'refs/remotes/origin/main',
        parent_oid=parent,operation=candidate['operation'],expected_candidate=None,identity_line=IDENTITY)['candidate']==candidate['candidate']
    assert candidate['stale'] is False
    assert not set(candidate)&{'approval','consent','publication','activation'}


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_invalid_parent_and_drift_refuse_before_candidate_effect(tmp_path,fmt):
    root,repo,p,prepared,state,parent=ready(tmp_path,fmt)
    for oid in (parent[:8],'0'*len(parent),git(root/'data/native/repository.git','--git-dir=.','rev-parse','HEAD').strip().decode()):
        with pytest.raises((n.Blocked,RuntimeError)):build(p,prepared,state,oid)
    assert not (root/os.fsdecode(c.STORE)).exists()
    write(repo,b'homeassistant/a',b'drift')
    with pytest.raises(n.Blocked):build(p,prepared,state,parent)
    assert not (root/os.fsdecode(c.STORE)).exists()


def test_sticky_stale_and_update_diagnostics(tmp_path):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    candidate=build(p,prepared,state,parent);owner=c.Candidate(p)
    p.app='new-app'
    assert owner.read()==candidate
    assert owner.revalidate()=='stale'
    p.app='fixture-app-1'
    assert c.Candidate(p).revalidate()=='stale'
    stale=owner.read();assert stale['rows']==candidate['rows'] and stale['raw']==candidate['raw']


@pytest.mark.parametrize('kind',['duplicate','unknown','artifact','missing'])
def test_corruption_refuses_without_repair(tmp_path,kind):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    candidate=build(p,prepared,state,parent)
    control=root/os.fsdecode(c.STORE)/'candidate.json'
    if kind=='duplicate':control.write_bytes(control.read_bytes()[:-1]+b',"schema":1}')
    elif kind=='unknown':
        raw=json.loads(control.read_bytes());raw['unknown']=False;control.write_text(json.dumps(raw))
    else:
        artifact=next(r for r in candidate['artifacts'] if r['digest'] is not None)
        path=root/os.fsdecode(c.STORE)/candidate['operation']/os.fsdecode(n.decode_path(artifact['path']))
        if kind=='missing':path.unlink()
        else:path.write_bytes(b'corrupt')
    with pytest.raises((n.Blocked,ValueError,OSError)):c.Candidate(p).read()


@pytest.mark.parametrize('limit',['available','inodes'])
def test_insufficient_storage_and_one_snapshot(tmp_path,limit):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    original=p.disk.snapshot;count=[];capacity=[]
    original_capacity=os.fstatvfs
    def observe_capacity(fd):capacity.append(1);return original_capacity(fd)
    def snapshot():count.append(1);return original()
    with mock.patch.object(p.disk,'snapshot',snapshot),mock.patch.object(os,'fstatvfs',observe_capacity):candidate=build(p,prepared,state,parent)
    assert len(count)==1 and len(capacity)==1
    root2,repo2,p2,prepared2,state2,parent2=ready(tmp_path/'other')
    from dataclasses import replace
    volume=p2.disk._volume
    def low(*a,**kw):return replace(volume(*a,**kw),**{limit:0})
    from native_disk import InsufficientSpace
    with mock.patch.object(p2.disk,'_volume',low),pytest.raises(InsufficientSpace):build(p2,prepared2,state2,parent2)
    assert not (root2/os.fsdecode(c.STORE)).exists()


@pytest.mark.parametrize('boundary',['candidate-write','candidate-file-fsync','candidate-replace','candidate-publish-fsync'])
def test_interruption_retains_explicit_incomplete_or_uncertain(tmp_path,boundary):
    root,repo,p,prepared,state,parent=ready(tmp_path);before=inventory(root)
    class Crash(OSError):pass
    def trace(event):
        if event[0]==boundary:raise Crash('injected')
    owner=c.Candidate(p,trace=trace);operation=uuid.uuid4().hex
    with pytest.raises(OSError):owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],
        parent_ref=b'refs/remotes/origin/main',parent_oid=parent,operation=operation,expected_candidate=None,identity_line=IDENTITY)
    assert inventory(root)==before
    if boundary in ('candidate-replace','candidate-publish-fsync'):
        assert c.Candidate(p).read() is not None
    else:
        assert c.Candidate(p).read() is None
        with pytest.raises(n.Blocked):build(p,prepared,state,parent,operation=operation)


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_repository_only_binary_moves_modes_and_index_only_removals(tmp_path,fmt):
    from native_inventory import Policy
    doc=b'data/native/repository-files/README.md'
    mappings=((b'homeassistant',b'homeassistant'),(b'README.md',doc))
    policy=Policy(repository_files=(doc,))
    binary=b'\x00\xffcomplete binary\n'*1000
    def configure(root,repo):
        write(repo,b'README.md',binary)
        write(repo,b'homeassistant/secrets.yaml',b'never selected')
        write(repo,b'homeassistant/.storage_managed/core.config_entries.json',b'retired')
        os.chmod(repo/'homeassistant/a',0o4750)
        git(repo,'add','.');git(repo,'commit','-qm','frozen parent payload')
    root,repo,p,prepared,state,parent=ready(tmp_path,fmt,mappings,policy,configure)
    before=inventory(root);candidate=build(p,prepared,state,parent);owner=c.Candidate(p)
    assert inventory(root)==before
    assert not (root/'data/native/repository-files/README.md').exists()
    i=next(i for i,r in enumerate(candidate['rows']) if r['after'] and n.decode_path(r['after']['path'])==doc)
    assert candidate['rows'][i]['action']=='move'
    output=io.BytesIO();owner.read_content(candidate=candidate['candidate'],row=i,side='after',destination=output)
    assert output.getvalue()==binary
    removed={n.decode_path(r['before']['path']) for r in candidate['rows'] if r['action']=='remove'}
    assert b'homeassistant/secrets.yaml' in removed
    assert b'homeassistant/.storage_managed/core.config_entries.json' in removed
    record=next(r['metadata'] for r in candidate['rows'] if r['after'] and n.decode_path(r['after']['path'])==b'homeassistant/a')
    frozen=next(r['object'] for r in prepared['observation']['source'] if n.decode_path(r['path'])==b'homeassistant/a')
    assert record['mode']==frozen['mode']
    write(repo,b'README.md',b'drift')
    assert owner.revalidate()=='stale'


def test_retained_pending_does_not_block_new_build_or_stale_marker(tmp_path):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    class Crash(OSError):pass
    def trace(event):
        if event[0]=='candidate-write' and n.decode_path(event[1]).split(b'/')[-1].startswith(b'.pending-'):raise Crash('pending interruption')
    with pytest.raises(Crash):
        owner=c.Candidate(p,trace=trace)
        owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],parent_ref=b'refs/remotes/origin/main',parent_oid=parent,
            operation=uuid.uuid4().hex,expected_candidate=None,identity_line=IDENTITY)
    store=root/os.fsdecode(c.STORE)
    pending=next(store.glob('.pending-*'));before=(pending.stat().st_ino,pending.read_bytes())
    diagnostic=c.Candidate(p).diagnostics()
    assert diagnostic['candidate'] is None and len(diagnostic['incomplete'])==1 and len(diagnostic['pending'])==1
    candidate=build(p,prepared,state,parent)
    assert (pending.stat().st_ino,pending.read_bytes())==before
    p.app='changed';assert c.Candidate(p).revalidate()=='stale'
    assert (pending.stat().st_ino,pending.read_bytes())==before


@pytest.mark.parametrize('kind',['missing','symlink','unknown'])
def test_missing_metadata_unsupported_tree_and_unresolved_leaf(tmp_path,kind):
    def configure(root,repo):
        if kind=='missing':
            write(repo,b'homeassistant/remote-only',b'frozen');git(repo,'add','.');git(repo,'commit','-qm','parent');(repo/'homeassistant/remote-only').unlink()
        elif kind=='symlink':
            (repo/'homeassistant/link').symlink_to('a');git(repo,'add','.');git(repo,'commit','-qm','parent')
        else:
            write(repo,b'unknown',b'undeclared');git(repo,'add','.');git(repo,'commit','-qm','parent')
    if kind=='symlink':
        with pytest.raises(AssertionError):ready(tmp_path,configure=configure)
    else:
        root,repo,p,prepared,state,parent=ready(tmp_path,configure=configure)
        with pytest.raises(n.Blocked):build(p,prepared,state,parent)
        assert not (root/os.fsdecode(c.STORE)).exists()


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='numeric ownership/full mode requires root Linux fixture')
@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_linux_exact_metadata_and_byte_paths(tmp_path,fmt):
    def configure(root,repo):
        write(repo,b'homeassistant/parents/\xffbinary',b'\x00\xffdata')
        write(repo,b'homeassistant/group-execute',b'group only')
        os.chown(os.fsencode(repo)+b'/homeassistant/parents/\xffbinary',23456,34567)
        os.chmod(os.fsencode(repo)+b'/homeassistant/parents/\xffbinary',0o4700)
        os.chown(repo/'homeassistant/parents',23456,34567)
        os.chmod(repo/'homeassistant/parents',0o2750)
        os.chmod(repo/'homeassistant/group-execute',0o6010)
        git(repo,'add','.');git(repo,'commit','-qm','numeric metadata')
        # Preparation requires native target parents; their origin is explicit.
        (root/'homeassistant/parents').mkdir()
    root,repo,p,prepared,state,parent=ready(tmp_path,fmt,configure=configure)
    candidate=build(p,prepared,state,parent)
    from native_metadata import decode
    manifest=decode(base64.b64decode(candidate['metadata']),width=len(parent),policy=c.Candidate(p)._policy(()))
    file=manifest.by_path[b'homeassistant/parents/\xffbinary']
    assert (file.uid,file.gid,file.mode)==(23456,34567,0o4700)
    directory=manifest.by_path[b'homeassistant/parents']
    assert (directory.uid,directory.gid,directory.mode)==(23456,34567,0o2750)
    group=next(r['after'] for r in candidate['rows'] if r['after'] and n.decode_path(r['after']['path'])==b'homeassistant/group-execute')
    assert group['mode']=='100644'


@pytest.mark.skipif('HA_OPS_EXT4_ROOT' not in os.environ,reason='requires explicit bounded ext4 runner')
@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_ext4_candidate_allocation_checkpoints(fmt):
    import tempfile
    import native_disk as d
    from contextlib import contextmanager
    if os.environ.get('HA_OPS_EXT4_PROFILE')!='allocation':pytest.skip('allocation profile only')
    with tempfile.TemporaryDirectory(prefix='migration-git-',dir=os.environ['HA_OPS_EXT4_ROOT']) as directory:
        root,repo,p,prepared,state,parent=ready(Path(directory),fmt)
        recipe_seen=[];volumes=[];samples=[]
        original_guard=p.disk.migration_git_candidate_guard;original_volume=p.disk._volume
        def volume(*a,**kw):
            v=original_volume(*a,**kw);volumes.append(v);return v
        @contextmanager
        def guard(recipe):
            recipe_seen.append(recipe)
            with original_guard(recipe):yield
        def sample(event):
            base=root/os.fsdecode(c.STORE);allocated=0;inodes=0
            if base.exists():
                for path in [base,*base.rglob('*')]:
                    s=path.lstat();allocated+=s.st_blocks*512;inodes+=1
            samples.append((allocated,inodes))
        with mock.patch.object(p.disk,'migration_git_candidate_guard',guard),mock.patch.object(p.disk,'_volume',volume):
            owner=c.Candidate(p,trace=sample)
            result=owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],parent_ref=b'refs/remotes/origin/main',parent_oid=parent,
                operation=uuid.uuid4().hex,expected_candidate=None,identity_line=IDENTITY)
        assert owner.read()==result
        recipe=recipe_seen[0];v=volumes[-1];q=v.quantum
        assert v.filesystem==b'ext4' and q==4096
        bound=sum(d._round(n,q)*2+3*q for n in recipe.sizes)+recipe.entries*3*q
        assert max(n for n,_ in samples)<=bound
        assert max(n for _,n in samples)<=recipe.entries+32
        print('MIGRATION_GIT_EXT4 '+json.dumps(dict(format=fmt,checkpoint_peak=max(n for n,_ in samples),inode_peak=max(n for _,n in samples),byte_bound=bound,inode_bound=recipe.entries+32)))
        baseline=samples[-1];samples.clear()
        with mock.patch.object(p.disk,'migration_git_candidate_guard',guard),mock.patch.object(p.disk,'_volume',volume):
            assert owner.revalidate()=='current'
        validation=recipe_seen[-1];q=volumes[-1].quantum
        validation_bound=sum(d._round(n,q)*2+3*q for n in validation.sizes)+validation.entries*3*q
        assert max(n-baseline[0] for n,_ in samples)<=validation_bound
        assert max(n-baseline[1] for _,n in samples)<=validation.entries+32
        print('MIGRATION_GIT_VALIDATION_EXT4 '+json.dumps(dict(format=fmt,checkpoint_additional_peak=max(n-baseline[0] for n,_ in samples),inode_additional_peak=max(n-baseline[1] for _,n in samples),byte_bound=validation_bound,inode_bound=validation.entries+32)))



def test_candidate_private_route_and_production_boundary(tmp_path):
    from native_inventory import Policy
    root=tmp_path.resolve();(root/'data/native-migration-git-candidate').mkdir(parents=True)
    with pytest.raises(n.Blocked):
        n.Preparation(binding(root,Policy(legacy_repo=c.STORE)),app_identity='fixture',manifest_digest='a'*64)
    import ast
    for path in Path(c.__file__).parent.glob('*.py'):
        if path.name in ('native_migration_git_candidate.py','native_disk.py'):continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import):assert all(alias.name.split('.')[-1]!='native_migration_git_candidate' for alias in node.names)
            elif isinstance(node,ast.ImportFrom):
                assert (node.module or '').split('.')[-1]!='native_migration_git_candidate'
                assert all(alias.name!='native_migration_git_candidate' for alias in node.names)



@pytest.mark.parametrize('failure',[OSError,c.DiskBlocked])
def test_failed_validation_admission_never_observes_sources(tmp_path,failure):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    candidate=build(p,prepared,state,parent);owner=c.Candidate(p)
    with mock.patch.object(p.disk,'migration_git_candidate_guard',side_effect=failure('unavailable')),mock.patch.object(owner,'_proof',side_effect=AssertionError('source read before admission')):
        assert owner.revalidate()=='unavailable'
    assert owner.read()==candidate


def test_validation_pending_survives_restart_and_aba(tmp_path):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    candidate=build(p,prepared,state,parent);owner=c.Candidate(p)
    p.app='changed';original=owner._publish;calls=[]
    def fail_final(value,expected,recipe):
        calls.append(value['validation'])
        if len(calls)==2:raise OSError('final unavailable')
        return original(value,expected,recipe)
    with mock.patch.object(owner,'_publish',fail_final):assert owner.revalidate()=='unavailable'
    assert calls==['pending','stale']
    p.app='fixture-app-1'
    reopened=c.Candidate(p);saved=reopened.read()
    assert saved['validation']=='pending' and saved['candidate']==candidate['candidate']
    assert reopened.revalidate()=='unavailable'
    assert saved['rows']==candidate['rows'] and saved['raw']==candidate['raw']


def test_same_byte_pending_inode_replacement_refuses(tmp_path):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    replaced=[]
    def trace(event):
        path=n.decode_path(event[1])
        if event[0]=='candidate-directory-fsync' and path.split(b'/')[-1].startswith(b'.pending-') and not replaced:
            target=root/os.fsdecode(path)
            replacement=target.parent/('.pending-'+uuid.uuid4().hex)
            fd=os.open(replacement,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:os.write(fd,target.read_bytes());os.fsync(fd)
            finally:os.close(fd)
            os.replace(replacement,target);replaced.append(True)
    owner=c.Candidate(p,trace=trace)
    with pytest.raises(n.Blocked):owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],parent_ref=b'refs/remotes/origin/main',parent_oid=parent,
        operation=uuid.uuid4().hex,expected_candidate=None,identity_line=IDENTITY)
    assert replaced and owner.read() is None


@pytest.mark.parametrize('part',['message','identity'])
def test_oversize_commit_inputs_refuse_before_any_effect(tmp_path,part):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    events=[];owner=c.Candidate(p,trace=events.append)
    identity=IDENTITY if part=='message' else b'x'*(13*1024*1024)+b' <x@example.invalid> 1720000000 +0000'
    message=b'x'*(25*1024*1024) if part=='message' else b'valid\n'
    with pytest.raises(n.Blocked):owner.build(expected_preparation=prepared['digest'],transition_operation=state['operation'],parent_ref=b'refs/remotes/origin/main',parent_oid=parent,
        operation=uuid.uuid4().hex,expected_candidate=None,identity_line=identity,message=message)
    assert not events and not (root/os.fsdecode(c.STORE)).exists()


def test_exact_retry_semantic_rows_and_literal_ignore_policy(tmp_path):
    root,repo,p,prepared,state,parent=ready(tmp_path)
    operation=uuid.uuid4().hex;owner=c.Candidate(p)
    candidate=build(p,prepared,state,parent,operation=operation,message=b'Original full message\n')
    for message,transition,identity in ((b'Original',state['operation'],IDENTITY),(b'Original full message\n','0'*32,IDENTITY),(b'Original full message\n',state['operation'],b'Other <other@example.invalid> 1720000000 +0000')):
        with pytest.raises(n.Conflict):owner.build(expected_preparation=prepared['digest'],transition_operation=transition,parent_ref=b'refs/remotes/origin/main',parent_oid=parent,
            operation=operation,expected_candidate=None,identity_line=identity,message=message)
    control=root/os.fsdecode(c.STORE)/'candidate.json';original=control.read_bytes()
    mutations=[]
    empty=copy.deepcopy(candidate);empty['rows'].append(dict(before=None,after=None,action='remove',metadata=None));mutations.append(empty)
    for action in ('remove','generate','move','mode'):
        changed=copy.deepcopy(candidate);changed['rows'][0]['action']=action;mutations.append(changed)
    absent=copy.deepcopy(candidate);absent['rows'][0]['metadata']=None;mutations.append(absent)
    for changed in mutations:
        changed['candidate']=n._digest({k:v for k,v in changed.items() if k not in ('digest','candidate','stale','validation')})
        changed['digest']=n._digest({k:v for k,v in changed.items() if k!='digest'})
        control.write_bytes(n._json(changed))
        with pytest.raises(n.Blocked):owner.read()
        control.write_bytes(original)
    i=next(i for i,r in enumerate(candidate['rows']) if r['after'] and n.decode_path(r['after']['path'])==b'.gitignore')
    sink=io.BytesIO();owner.read_content(candidate=candidate['candidate'],row=i,side='after',destination=sink)
    policy=tmp_path/'policy';policy.mkdir();git(policy,'init','-q');write(policy,b'.gitignore',sink.getvalue())
    import subprocess
    for path,ignored in ((b'homeassistant/a',False),(b'homeassistant/unrelated',True),(b'homeassistant/secrets.yaml',True),(b'data/private',True),(b'data/native/repository.git/config',True),(b'data/native/repository-files/unlisted',True),(c.MANIFEST_PATH,False),(b'.gitignore',False)):
        result=subprocess.run(['git','-C',str(policy),'check-ignore','--no-index',os.fsdecode(path)],capture_output=True)
        assert result.returncode==(0 if ignored else 1),(path,sink.getvalue())


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_remote_repository_only_leaf_absent_from_checkout(tmp_path,fmt):
    from native_inventory import Policy
    doc=b'data/native/repository-files/README.md'
    def configure(root,repo):
        write(repo,b'README.md',b'remote documentation');git(repo,'add','.');git(repo,'commit','-qm','remote doc')
        (repo/'README.md').unlink()
    root,repo,p,prepared,state,parent=ready(tmp_path,fmt,((b'homeassistant',b'homeassistant'),(b'README.md',doc)),Policy(repository_files=(doc,)),configure)
    candidate=build(p,prepared,state,parent);owner=c.Candidate(p)
    i=next(i for i,r in enumerate(candidate['rows']) if r['after'] and n.decode_path(r['after']['path'])==doc)
    sink=io.BytesIO();owner.read_content(candidate=candidate['candidate'],row=i,side='after',destination=sink)
    assert sink.getvalue()==b'remote documentation' and not (root/os.fsdecode(doc)).exists()
