"""Disposable, explicit migration and syscall-boundary recovery proofs."""
import ast
import base64
import copy
import json
import os
from pathlib import Path
import stat
import shutil
import hashlib
import time
import sys
import uuid
from unittest import mock

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_migration_transition as t
import native_migration as n
from test_native_migration import setup, git, write, binding, synthetic_host_allocation


@pytest.fixture(autouse=True)
def host_rename_adapter(monkeypatch):
    # Portable control-flow evidence only. Linux always exercises renameat2.
    if sys.platform!='linux':
        def rename(left,source,right,target):
            try:os.stat(target,dir_fd=right,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise FileExistsError()
            os.rename(source,target,src_dir_fd=left,dst_dir_fd=right)
        monkeypatch.setattr(t,'_rename_noreplace',rename)


def prepare(tmp_path,fmt='sha1'):
    root,repo,p=setup(tmp_path,fmt)
    write(repo,b'homeassistant/a',b'staged');git(repo,'add','homeassistant/a')
    write(repo,b'homeassistant/a',b'working')
    git(repo,'branch','ha-ops/service');git(repo,'tag','keep-tag')
    git(repo,'update-ref','refs/remotes/origin/main','HEAD');git(repo,'update-ref','refs/stash','HEAD')
    git(repo,'pack-refs','--all')
    write(repo,b'untracked',b'kept');(repo/'empty').mkdir()
    nested=root/'nested';nested.mkdir();git(nested,'init','-q');write(nested,b'owned',b'nested')
    write(root,b'data/options.json',b'{"direct_https":false}')
    candidate=p.prepare(expected=None)
    assert not candidate['observation']['blockers']
    return root,repo,p,candidate


def matrix_fixture(tmp_path,fmt='sha1',template=None):
    root=tmp_path.resolve()
    if template is not None:
        shutil.copytree(template,root)
    else:
        repo=root/'data/repo';repo.mkdir(parents=True)
        git(repo,'init','--template=','--initial-branch=main','--object-format='+fmt)
        git(repo,'-c','core.logAllRefUpdates=false','commit','--allow-empty','-qm','matrix ancestry')
        (repo/'.git/COMMIT_EDITMSG').unlink()
        write(repo,b'homeassistant/a',b'staged');git(repo,'add','homeassistant/a')
        write(repo,b'homeassistant/a',b'working');write(repo,b'untracked',b'untouched')
        (repo/'empty').mkdir();write(root,b'homeassistant/a',b'live')
        # A real unreachable Git object forces multiple streamed copy chunks in
        # each format. Comprehensive hooks/ref/index variants stay separate.
        payload=b''.join(hashlib.sha256(i.to_bytes(4,'big')).digest() for i in range(t.CHUNK//32+32))
        oid=git(repo,'hash-object','-w','--stdin',input=payload).strip().decode()
        git(repo,'-c','core.logAllRefUpdates=false','update-ref','refs/fixture/payload',oid)
        git(repo,'repack','-adn')
        git(repo,'-c','core.logAllRefUpdates=false','update-ref','-d','refs/fixture/payload')
        git(repo,'pack-refs','--all')
        assert max(file.stat().st_size for file in (repo/'.git/objects/pack').iterdir())>t.CHUNK
    p=n.Preparation(binding(root),app_identity='fixture-app-1',manifest_digest='a'*64)
    candidate=p.prepare(expected=None)
    assert not candidate['observation']['blockers']
    return root,root/'data/repo',p,candidate


def begin(p,candidate,trace=lambda event:None,operation=None):
    transition=t.Transition(p,trace=trace);operation=operation or uuid.uuid4().hex
    confirmation=transition.confirm(candidate=candidate['digest'],operation=operation,action='start',approved=True)
    return transition,operation,confirmation


def freeze(root):
    """Independent full fixture inventory: no broad data/native exemptions."""
    rows={};base=os.fsencode(root)
    source=base+b'/data/repo/.git';store=base+b'/'+t.STORE
    for parent,dirs,files in os.walk(base,followlinks=False):
        dirs[:]=[name for name in dirs if parent+b'/'+name not in (source,store)]
        for name in dirs+files:
            path=parent+b'/'+name;relative=path[len(base)+1:]
            if path==store:continue
            info=os.lstat(path)
            # Exact service parents/outputs only. Native children are filtered
            # individually, never the whole data/native subtree.
            if relative in (b'data/native',b'data/native/repository.git',b'data/native/repository-files',
                            b'data/native/root-ignore-policy',b'.gitignore'):continue
            if relative.startswith((b'data/native/repository.git/',b'data/native/repository-files/')):continue
            raw=Path(os.fsdecode(path)).read_bytes() if stat.S_ISREG(info.st_mode) else None
            fields=(info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,info.st_mtime_ns,info.st_ctime_ns,raw)
            if relative in (b'data',b'data/repo'):
                fields=fields[:4] # Membership change: checkout mtime restored separately.
            rows[relative]=fields
    return rows


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_complete_independent_recovery_and_native_continuity(tmp_path,fmt):
    root,repo,p,candidate=prepare(tmp_path,fmt)
    index=(repo/'.git/index').read_bytes();head=(repo/'.git/HEAD').read_bytes()
    refs=git(repo,'for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)')
    staged=git(repo,'rev-parse',':homeassistant/a').strip().decode()
    index_rows=git(repo,'ls-files','--stage','-z');flags=git(repo,'ls-files','-v','-z')
    before=freeze(root);checkout_mtime=repo.stat().st_mtime_ns;events=[]
    transition,operation,confirmation=begin(p,candidate,events.append)
    result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert result['phase']==t.TERMINAL and result['recovery']['verified']
    assert repo.stat().st_mtime_ns==checkout_mtime
    assert freeze(root)==before and not (repo/'.git').exists() and not (root/'.git').exists()
    prefix=root/os.fsdecode(t.STORE)/operation
    assert (prefix/'backup/index').read_bytes()==index
    assert (prefix/'retired.git/index').read_bytes()==index
    assert (prefix/'backup/HEAD').read_bytes()==head
    restored=root/'restoration-proof';restored.mkdir()
    shutil.copytree(prefix/'backup',restored/'.git')
    assert (restored/'.git/index').read_bytes()==index
    assert git(restored,'ls-files','--stage','-z')==index_rows
    assert git(restored,'ls-files','-v','-z')==flags
    git(restored,'fsck','--full','--no-progress')
    shutil.rmtree(restored)
    # Original source is unavailable; verify all objects through independent backup.
    assert transition._git(t.STORE+b'/'+operation.encode()+b'/backup','cat-file','-p',staged)==b'staged'
    assert transition._git(b'data/native/repository.git','for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)')==refs
    assert not (root/'data/native/repository.git/index').exists()
    assert (root/'.gitignore').read_bytes()==t.POLICY
    assert sum(kind=='rename-no-replace' for kind,path in events)==2
    reopened=t.Transition(p,trace=lambda event:pytest.fail('read mutated'))
    assert reopened.read()==result
    with pytest.raises(n.Blocked):transition.resume(operation=operation,confirmation=confirmation)


def test_missing_foreign_stale_and_replayed_authority(tmp_path):
    root,repo,p,candidate=prepare(tmp_path)
    transition,operation,confirmation=begin(p,candidate)
    for value in (None,True,t.Confirmation(object(),confirmation.binding)):
        with pytest.raises(n.Blocked):transition.start(candidate=candidate['digest'],operation=operation,confirmation=value)
    write(repo,b'homeassistant/a',b'drift')
    with pytest.raises(n.Blocked):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    with pytest.raises(n.Blocked):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert (repo/'.git').is_dir() and not (root/os.fsdecode(t.STORE)).exists()


@pytest.mark.parametrize('boundary',['write','journal-replace','rename-no-replace','file-fsync','journal-parent-fsync'])
def test_interruption_explicit_resume_preserves_all(tmp_path,boundary):
    root,repo,p,candidate=prepare(tmp_path);before=freeze(root)
    class Crash(BaseException):pass
    seen=[]
    def crash(event):
        seen.append(event)
        if event[0]==boundary:raise Crash()
    transition,operation,confirmation=begin(p,candidate,crash)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert freeze(root)==before
    reopened=t.Transition(p,trace=lambda event:pytest.fail('read effect'))
    state=reopened.read()
    assert freeze(root)==before
    reopened.trace=lambda event:None
    if state is None or state['phase']=='bootstrap-pending':
        transition,operation,confirmation=begin(p,candidate,operation=state['operation'] if state else None)
        result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    else:
        confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        result=reopened.resume(operation=operation,confirmation=confirmation)
    assert result['phase']==t.TERMINAL and freeze(root)==before


def test_journal_shape_corruption_and_foreign_prefix_refuse(tmp_path):
    root,repo,p,candidate=prepare(tmp_path)
    class Crash(BaseException):pass
    def crash(event):
        if event[0]=='write':raise Crash()
    transition,operation,confirmation=begin(p,candidate,crash)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    state=t.Transition(p).read();pending=state['actions'][state['completed']]
    path=root/os.fsdecode(n.decode_path(pending['path']))
    path.write_bytes(b'foreign')
    reopened=t.Transition(p)
    confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    with pytest.raises(n.Blocked):reopened.resume(operation=operation,confirmation=confirmation)
    assert (repo/'.git').is_dir()
    journal=root/os.fsdecode(t.STORE)/'journal.json'
    journal.write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises(n.Blocked):t.Transition(p).read()


def test_dormant_import_and_exact_syscall_trace_contract():
    app=Path(t.__file__).parent
    for path in app.glob('*.py'):
        if path.name in ('native_migration_transition.py','native_disk.py','native_migration_git_candidate.py'):continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import):assert all(item.name!='native_migration_transition' for item in node.names)
            if isinstance(node,ast.ImportFrom):assert node.module!='native_migration_transition'
    tree=ast.parse(Path(t.__file__).read_text())
    # Every executor mutator is passed through the single action hook. The
    # no-replace ctypes adapter is itself wrapped at its only call site.
    parents={child:node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    mutators={'mkdir','write','replace','fsync','rename','unlink','utime','chmod','chown','ftruncate'}
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute):continue
        if not isinstance(node.func.value,ast.Name) or node.func.value.id!='os' or node.func.attr not in mutators:continue
        current=node
        while current in parents and not isinstance(current,ast.Lambda):current=parents[current]
        assert isinstance(current,ast.Lambda),ast.unparse(node)


@pytest.mark.skipif(sys.platform!='linux',reason='real Linux process interruption and renameat2')
@pytest.mark.parametrize('shard',range(8))
@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_linux_every_mutation_boundary_process_exit(tmp_path,shard,fmt):
    # Obtain the boundary count from this exact executor; never maintain a list
    # of phases. Each child exits without finally/close/exception cleanup.
    root,repo,p,candidate=matrix_fixture(tmp_path/'baseline',fmt);events=[]
    pristine=tmp_path/'pristine'
    shutil.copytree(root,pristine,ignore=shutil.ignore_patterns('native-migration-preparation','.native-disk-admission.lock'))
    transition,operation,confirmation=begin(p,candidate,events.append)
    transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    for target in range(shard+1,len(events)+1,8):
        case=tmp_path/('boundary-%d'%target)
        root,repo,p,candidate=matrix_fixture(case,fmt,pristine);before=freeze(root);operation=uuid.uuid4().hex
        pid=os.fork()
        if pid==0:
            count=[0]
            def kill(event):
                count[0]+=1
                if count[0]==target:os._exit(73)
            try:
                transition,operation,confirmation=begin(p,candidate,kill,operation)
                transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
            except BaseException:
                import traceback
                traceback.print_exc();os._exit(74)
            os._exit(75)
        _,status=os.waitpid(pid,0)
        assert os.waitstatus_to_exitcode(status)==73,(target,len(events))
        assert freeze(root)==before,target
        reopened=t.Transition(p,trace=lambda event:pytest.fail('lifecycle mutated'))
        state=reopened.read();assert freeze(root)==before
        reopened.trace=lambda event:None
        if state is None or state['phase']=='bootstrap-pending':
            transition,operation,confirmation=begin(p,candidate,operation=state['operation'] if state else None)
            result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
        else:
            confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
            result=reopened.resume(operation=operation,confirmation=confirmation)
        assert result['phase']==t.TERMINAL and freeze(root)==before,target
        shutil.rmtree(case)  # Exact successful test-owned case; retain failures.


def test_nested_source_boundary_refuses_without_effects(tmp_path):
    root,repo,p=setup(tmp_path)
    nested=repo/'nested';nested.mkdir();git(nested,'init','-q');write(nested,b'owned',b'nested')
    candidate=p.prepare(expected=None);before=freeze(root)
    transition=t.Transition(p,trace=lambda event:pytest.fail('blocked candidate mutated'))
    with pytest.raises(n.Blocked):transition.confirm(candidate=candidate['digest'],operation=uuid.uuid4().hex,action='start',approved=True)
    assert freeze(root)==before and (nested/'.git').is_dir() and (repo/'.git').is_dir()


@pytest.mark.parametrize('variant',['detached','absent-index','unborn'])
def test_head_and_optional_index_variants(tmp_path,variant):
    root,repo,p=setup(tmp_path)
    if variant=='detached':git(repo,'checkout','--detach','-q')
    if variant=='absent-index':(repo/'.git/index').unlink()
    if variant=='unborn':
        git(repo,'symbolic-ref','HEAD','refs/heads/unborn')
        (repo/'.git/index').unlink()
    candidate=p.prepare(expected=None);before=freeze(root)
    old_index=(repo/'.git/index').read_bytes() if (repo/'.git/index').exists() else None
    head=(repo/'.git/HEAD').read_bytes()
    transition,operation,confirmation=begin(p,candidate)
    result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert result['phase']==t.TERMINAL and freeze(root)==before
    backup=root/os.fsdecode(t.STORE)/operation/'backup'
    assert (backup/'HEAD').read_bytes()==head
    assert ((backup/'index').read_bytes() if (backup/'index').exists() else None)==old_index


@pytest.mark.parametrize('error',['space','inodes','mount','forged-recipe'])
def test_disk_admission_refuses_before_effects(tmp_path,error,monkeypatch):
    import dataclasses
    import native_disk as d
    root,repo,p,candidate=prepare(tmp_path);before=freeze(root)
    transition,operation,confirmation=begin(p,candidate,lambda event:pytest.fail('admission refusal mutated'))
    if error=='forged-recipe':
        recipe=t.Recipe(transition,operation,(1,),1,1)
        with pytest.raises((n.Blocked,d.Blocked)):
            with p.disk.migration_transition_guard(recipe,transition._effect):pass
        return
    original=p.disk._volume
    def volume(*args,**kwargs):
        value=original(*args,**kwargs)
        if error=='space':return dataclasses.replace(value,available=1)
        if error=='inodes':return dataclasses.replace(value,inodes=1)
        return dataclasses.replace(value,supported=False)
    monkeypatch.setattr(p.disk,'_volume',volume)
    with pytest.raises((n.Blocked,d.Blocked)):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert freeze(root)==before and (repo/'.git').is_dir()


@pytest.mark.parametrize('target',['escrow','native'])
def test_no_replace_destination_race_never_clobbers(tmp_path,target,monkeypatch):
    root,repo,p,candidate=prepare(tmp_path);original=t._rename_noreplace
    transition,operation,confirmation=begin(p,candidate)
    def race(left,source,right,destination):
        if (target=='escrow' and destination==b'retired.git') or (target=='native' and destination==b'repository.git'):
            os.mkdir(destination,0o700,dir_fd=right)
            fd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY,dir_fd=right)
            try:
                leaf=os.open(b'foreign',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
                os.write(leaf,b'foreign');os.close(leaf)
            finally:os.close(fd)
        original(left,source,right,destination)
    monkeypatch.setattr(t,'_rename_noreplace',race)
    with pytest.raises(FileExistsError):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    prefix=root/os.fsdecode(t.STORE)/operation
    path=prefix/'retired.git' if target=='escrow' else root/'data/native/repository.git'
    assert (path/'foreign').read_bytes()==b'foreign'
    assert (prefix/'backup/index').is_file()
    if target=='escrow':assert (repo/'.git/index').is_file()
    else:assert (prefix/'retired.git/index').is_file()


@pytest.mark.parametrize('which',['copy','journal','fsync'])
def test_short_writes_and_storage_errors_are_recoverable(tmp_path,which,monkeypatch):
    root,repo,p,candidate=prepare(tmp_path)
    transition,operation,confirmation=begin(p,candidate)
    original_write=os.write;original_sync=os.fsync;injected=[False]
    if which in ('copy','journal'):
        def short(fd,data):return original_write(fd,data[:max(1,len(data)//3)])
        monkeypatch.setattr(os,'write',short)
        result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
        assert result['phase']==t.TERMINAL
    else:
        def fail(fd):
            if not injected[0]:injected[0]=True;raise OSError(5,'injected EIO')
            return original_sync(fd)
        monkeypatch.setattr(os,'fsync',fail)
        with pytest.raises(OSError):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
        monkeypatch.setattr(os,'fsync',original_sync)
        reopened=t.Transition(p)
        state=reopened.read()
        if state is None or state['phase']=='bootstrap-pending':
            transition,operation,confirmation=begin(p,candidate,operation=state['operation'] if state else None)
            result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
        else:
            confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
            result=reopened.resume(operation=operation,confirmation=confirmation)
        assert result['phase']==t.TERMINAL


def test_resume_requires_compatible_runtime_and_unchanged_live_projection(tmp_path):
    root,repo,p,candidate=prepare(tmp_path)
    class Crash(BaseException):pass
    def crash(event):
        if event[0]=='write':raise Crash()
    transition,operation,confirmation=begin(p,candidate,crash)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    p.app='changed-app'
    reopened=t.Transition(p)
    with pytest.raises(n.Blocked):reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    p.app='fixture-app-1';write(root,b'homeassistant/a',b'changed-live')
    confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    with pytest.raises(n.Blocked):reopened.resume(operation=operation,confirmation=confirmation)
    assert (repo/'.git').is_dir()


@pytest.mark.parametrize('change',['unknown','sequence','receipt','manifest','phase','action','nonfinite'])
def test_strict_saved_journal_grammar(tmp_path,change):
    root,repo,p,candidate=prepare(tmp_path)
    class Crash(BaseException):pass
    def crash(event):
        if event[0]=='write':raise Crash()
    transition,operation,confirmation=begin(p,candidate,crash)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    journal=root/os.fsdecode(t.STORE)/'journal.json';value=json.loads(journal.read_bytes())
    if change=='unknown':value['unexpected']=True
    if change=='sequence':value['pending']=99999
    if change=='receipt':value['receipts']['0']=['bad']
    if change=='manifest':value['source_manifest'][0]['path']=base64.b64encode(b'../escape').decode()
    if change=='phase':value['phase']=t.TERMINAL
    if change=='action':value['actions'][0]['path']=n.encode_path(b'homeassistant')
    if change=='nonfinite':
        journal.write_bytes(b'{"x":1e999}');
        with pytest.raises(n.Blocked):t.Transition(p).read()
        return
    value['digest']=n._digest({key:item for key,item in value.items() if key!='digest'})
    journal.write_bytes(n._json(value))
    with pytest.raises(n.Blocked):t.Transition(p).read()
    assert (repo/'.git').is_dir()


def test_second_interruption_during_explicit_resume(tmp_path):
    root,repo,p,candidate=prepare(tmp_path);before=freeze(root)
    class Crash(BaseException):pass
    def kill(event):
        if event[0]=='write':raise Crash()
    first,operation,confirmation=begin(p,candidate,kill)
    with pytest.raises(Crash):first.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    second=t.Transition(p,trace=kill)
    confirmation=second.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    with pytest.raises(Crash):second.resume(operation=operation,confirmation=confirmation)
    assert freeze(root)==before
    third=t.Transition(p)
    confirmation=third.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    assert third.resume(operation=operation,confirmation=confirmation)['phase']==t.TERMINAL
    assert freeze(root)==before


def test_auth_cookie_and_private_bytes_survive_transition(tmp_path):
    from direct_auth import Authority
    root,repo,p=setup(tmp_path)
    authority=Authority(root/'data');address='http://ha.local:8100'
    bearer=authority.exchange(authority.issue(authority.nonce(()),(),address).split('#grant=')[1],address)
    cookie=authority.cookie(bearer);token=root/'data/direct-auth/tokens.json'
    raw=token.read_bytes();before=token.stat()
    candidate=p.prepare(expected=None)
    transition,operation,confirmation=begin(p,candidate)
    result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    after=token.stat()
    assert result['phase']==t.TERMINAL and token.read_bytes()==raw
    assert (before.st_ino,before.st_mode,before.st_uid,before.st_gid,before.st_mtime_ns,before.st_ctime_ns)==(after.st_ino,after.st_mode,after.st_uid,after.st_gid,after.st_mtime_ns,after.st_ctime_ns)
    assert Authority(root/'data').authenticate(cookie,address)
    serialized=n._json(result)
    assert bearer.encode() not in serialized and cookie.encode() not in serialized


def test_mutation_guard_does_not_nest_preparation_persistence(tmp_path,monkeypatch):
    root,repo,p,candidate=prepare(tmp_path)
    transition,operation,confirmation=begin(p,candidate)
    original=p.revalidate
    def revalidate():
        assert p.disk._active_operation is None
        return original()
    monkeypatch.setattr(p,'revalidate',revalidate)
    monkeypatch.setattr(p,'_persist',lambda *a,**k:pytest.fail('transition entered preparation write path'))
    assert transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)['phase']==t.TERMINAL


def test_transition_store_policy_and_legacy_overlap(tmp_path):
    from native_inventory import Policy
    from native_metadata import Binding
    root,repo,p=setup(tmp_path)
    policy=Policy(extra_live_roots=(t.STORE,))
    assert policy.reason(t.STORE+b'/anything')=='service-data'
    changed=Policy(legacy_repo=t.STORE+b'/old')
    with pytest.raises(n.Blocked):n.Preparation(Binding(p.binding.root,changed,p.binding.mounts,p.binding.confirmed_mounts,p.binding.topology_verified),app_identity=p.app,manifest_digest=p.manifest)


@pytest.mark.parametrize('phase',['verified','published'])
@pytest.mark.parametrize('foreign',['ref','object','file','directory','log'])
def test_review_regression_complete_native_membership(tmp_path,phase,foreign):
    root,repo,p,candidate=prepare(tmp_path);before=freeze(root)
    class Crash(BaseException):pass
    def pause(event):
        if phase=='published':
            if event[0]=='rename-no-replace' and n.decode_path(event[1])==b'data/native/repository.git':raise Crash()
        elif event[0]=='journal-parent-fsync':
            current=t.Transition(p).read()
            if current is not None and current['recovery']['verified']:raise Crash()
    transition,operation,confirmation=begin(p,candidate,pause)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    stage=(root/'data/native/repository.git' if phase=='published' else root/os.fsdecode(t.STORE)/operation/'native')
    names={'ref':'refs/heads/unissued','object':'objects/ff/unissued','file':'unissued',
           'directory':'unissued-directory','log':'logs/refs/heads/unissued'}
    extra=stage/names[foreign]
    if foreign=='directory':extra.mkdir(mode=0o700)
    else:
        extra.parent.mkdir(parents=True,exist_ok=True)
        extra.write_bytes(b'unissued\n');extra.chmod(0o600)
    reopened=t.Transition(p,trace=lambda event:pytest.fail('foreign image caused a new effect'))
    confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    with pytest.raises(n.Blocked,match='membership-drift'):reopened.resume(operation=operation,confirmation=confirmation)
    assert extra.exists() and freeze(root)==before
    assert (repo/'.git').exists()==(phase=='verified')


@pytest.mark.parametrize('phase',['before-retirement','after-retirement'])
@pytest.mark.parametrize('real_process',[False,True])
def test_review_regression_repeated_journal_interruptions(tmp_path,phase,real_process):
    if real_process and sys.platform!='linux':pytest.skip('Linux process crash proof')
    root,repo,p,candidate=prepare(tmp_path);before=freeze(root)
    class Crash(BaseException):pass
    def pause(event):
        if (phase=='before-retirement' and event[0]=='write' or
                phase=='after-retirement' and event[0]=='rename-no-replace' and n.decode_path(event[1]).endswith(b'/retired.git')):
            raise Crash()
    transition,operation,confirmation=begin(p,candidate,pause)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    for attempt in range(12):
        def interrupt(event):
            if event[0]=='journal-open-exclusive':
                if real_process:os._exit(73)
                raise Crash()
        obj=t.Transition(p,trace=interrupt)
        confirmation=obj.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        if real_process:
            pid=os.fork()
            if pid==0:
                try:obj.resume(operation=operation,confirmation=confirmation)
                except BaseException:os._exit(74)
                os._exit(75)
            _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==73
        else:
            with pytest.raises(Crash):obj.resume(operation=operation,confirmation=confirmation)
        inert=t.Transition(p,trace=lambda event:pytest.fail('read settled a fragment'))
        assert inert.read() is not None and freeze(root)==before
        store=root/os.fsdecode(t.STORE)
        assert len(list(store.glob('journal.pending')))==1
        assert not list(store.glob('.journal-*'))
    def crash_resume(obj,confirmation):
        if not real_process:
            with pytest.raises(Crash):obj.resume(operation=operation,confirmation=confirmation)
            return
        pid=os.fork()
        if pid==0:
            try:obj.resume(operation=operation,confirmation=confirmation)
            except BaseException:os._exit(74)
            os._exit(75)
        _,status=os.waitpid(pid,0)
        assert os.waitstatus_to_exitcode(status)==73
    # New mutation boundaries are exercised during explicit fragment settlement.
    for boundary in ('journal-fragment-unlink','journal-fragment-parent-fsync'):
        def interrupt(event):
            if event[0]==boundary:
                if real_process:os._exit(73)
                raise Crash()
        obj=t.Transition(p,trace=interrupt)
        confirmation=obj.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        crash_resume(obj,confirmation)
        # Recreate an attributable empty fragment through an interrupted worker
        # so the next settlement boundary is reachable independently.
        def interrupt_open(event):
            if event[0]=='journal-open-exclusive':
                if real_process:os._exit(73)
                raise Crash()
        obj=t.Transition(p,trace=interrupt_open)
        confirmation=obj.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        crash_resume(obj,confirmation)
    final=t.Transition(p);confirmation=final.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    assert final.resume(operation=operation,confirmation=confirmation)['phase']==t.TERMINAL
    assert freeze(root)==before


def test_review_regression_foreign_journal_fragment_is_retained(tmp_path):
    root,repo,p,candidate=prepare(tmp_path)
    class Crash(BaseException):pass
    transition,operation,confirmation=begin(p,candidate,lambda event:(_ for _ in ()).throw(Crash()) if event[0]=='write' else None)
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    fragment=root/os.fsdecode(t.STORE)/'journal.pending'
    fragment.write_bytes(b'foreign');fragment.chmod(0o600)
    obj=t.Transition(p);assert obj.read() is not None
    confirmation=obj.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    with pytest.raises(n.Blocked,match='foreign-journal-publication-fragment'):obj.resume(operation=operation,confirmation=confirmation)
    assert fragment.read_bytes()==b'foreign' and (repo/'.git').is_dir()


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_matrix_fixture_primitive_coverage(tmp_path,fmt):
    started=time.monotonic();root,repo,p,candidate=matrix_fixture(tmp_path,fmt)
    fixture_elapsed=time.monotonic()-started;events=[]
    obj,operation,confirmation=begin(p,candidate,events.append)
    started=time.monotonic();result=obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    kinds={event[0] for event in events}
    assert {'mkdir','open-exclusive','write','file-fsync','directory-fsync','directory-parent-fsync',
            'journal-open-exclusive','journal-write','journal-file-fsync','journal-replace','journal-parent-fsync',
            'rename-no-replace','rename-source-parent-fsync','rename-target-parent-fsync','checkout-mtime',
            'bootstrap-open-exclusive','bootstrap-write','bootstrap-file-fsync','bootstrap-parent-fsync'}<=kinds
    writes={}
    for kind,path in events:
        if kind=='write':writes[path]=writes.get(path,0)+1
    assert max(writes.values())>=2
    assert result['phase']==t.TERMINAL
    print('matrix benchmark',fmt,'events',len(events),'fixture_seconds',round(fixture_elapsed,3),
          'transition_seconds',round(time.monotonic()-started,3),flush=True)


def complete_from_public(p,candidate):
    obj=t.Transition(p);diagnostic=obj.read()
    if diagnostic is None or diagnostic['phase']=='bootstrap-pending':
        operation=diagnostic['operation'] if diagnostic else None
        obj,operation,confirmation=begin(p,candidate,operation=operation)
        return obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    operation=diagnostic['operation']
    confirmation=obj.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
    return obj.resume(operation=operation,confirmation=confirmation)


@pytest.mark.parametrize('real_process',[False,True])
def test_bootstrap_each_boundary_without_retained_operation(tmp_path,real_process):
    if real_process and sys.platform!='linux':pytest.skip('Linux fresh-process bootstrap proof')
    root,repo,p,candidate=matrix_fixture(tmp_path/'baseline');events=[]
    obj,operation,confirmation=begin(p,candidate,events.append)
    obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    stop=next(index for index,event in enumerate(events) if event[0]=='journal-parent-fsync')+1
    class Crash(BaseException):pass
    for target in range(1,stop+1):
        case=tmp_path/('bootstrap-boundary-%d'%target)
        root,repo,p,candidate=matrix_fixture(case);before=freeze(root)
        def initial():
            count=[0]
            def crash(event):
                count[0]+=1
                if count[0]==target:
                    if real_process:os._exit(73)
                    raise Crash()
            obj,hidden_operation,confirmation=begin(p,candidate,crash)
            obj.start(candidate=candidate['digest'],operation=hidden_operation,confirmation=confirmation)
        if real_process:
            pid=os.fork()
            if pid==0:
                try:initial()
                except BaseException:os._exit(74)
                os._exit(75)
            _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==73
        else:
            with pytest.raises(Crash):initial()
        # No UUID from the exited invocation escapes initial(). Reopen and
        # reconstruct the next explicit decision solely from public diagnostics.
        inert=t.Transition(p,trace=lambda event:pytest.fail('bootstrap read mutated'))
        diagnostic=inert.read();assert freeze(root)==before
        assert diagnostic is None or diagnostic['phase'] in ('bootstrap-pending','transition-pending')
        assert complete_from_public(p,candidate)['phase']==t.TERMINAL
        assert freeze(root)==before
        shutil.rmtree(case)


@pytest.mark.parametrize('boundary',['bootstrap-prefix-unlink','bootstrap-prefix-parent-fsync'])
@pytest.mark.parametrize('real_process',[False,True])
def test_partial_bootstrap_marker_settlement_boundaries(tmp_path,boundary,monkeypatch,real_process):
    if real_process and sys.platform!='linux':pytest.skip('Linux bootstrap settlement process proof')
    root,repo,p,candidate=matrix_fixture(tmp_path);before=freeze(root)
    class Crash(BaseException):pass
    original=os.write
    def short(fd,data):return original(fd,data[:1])
    monkeypatch.setattr(os,'write',short)
    obj,operation,confirmation=begin(p,candidate,lambda event:(_ for _ in ()).throw(Crash()) if event[0]=='bootstrap-write' else None)
    with pytest.raises(Crash):obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    monkeypatch.setattr(os,'write',original)
    assert t.Transition(p).read()['operation'] is None
    def interrupt(event):
        if event[0]==boundary:
            if real_process:os._exit(73)
            raise Crash()
    obj,operation,confirmation=begin(p,candidate,interrupt)
    if real_process:
        pid=os.fork()
        if pid==0:
            try:obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
            except BaseException:os._exit(74)
            os._exit(75)
        _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==73
    else:
        with pytest.raises(Crash):obj.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    assert complete_from_public(p,candidate)['phase']==t.TERMINAL and freeze(root)==before


def diagnostic_churn(root):
    write(root,b'homeassistant/home-assistant_v2.db',b'database changed')
    write(root,b'homeassistant/home-assistant_v2.db-wal',b'wal changed')
    write(root,b'homeassistant/diagnostics/rotated.log',b'log changed')
    old=root/'homeassistant/home-assistant.log'
    if old.exists():old.unlink()


def old_full_protected(transition):
    # The schema-1 executor's original full-live baseline, independently built.
    result=[]
    for base in [transition.p.legacy]+[target for _,target in transition.p.mappings]:
        full=Path(os.fsdecode(transition.root+b'/'+base))
        if not full.exists():continue
        for parent,dirs,files in os.walk(full):
            relative=os.fsencode(Path(parent).relative_to(os.fsdecode(transition.root)))
            if relative==transition.p.legacy:dirs[:]=[x for x in dirs if x!='.git']
            info=os.lstat(parent)
            result.append([n.encode_path(relative),t.identity(info) if relative==transition.p.legacy else n._stat(info)])
            for name in files:
                path=relative+b'/'+os.fsencode(name)
                result.append([n.encode_path(path),n._stat(os.lstat(parent+'/'+name))])
    return sorted(result)


@pytest.mark.parametrize('old_schema',[False,True])
@pytest.mark.parametrize('phase',['running','before-retirement','after-retirement'])
def test_diagnostic_churn_survives_transition_and_terminal_resume(tmp_path,old_schema,phase):
    root,repo,p,candidate=prepare(tmp_path)
    class Crash(BaseException):pass
    events=[];injected=[]
    transition,operation,confirmation=begin(p,candidate)
    baseline=old_full_protected(transition)
    if old_schema:
        original=transition._protected
        def first():
            transition._protected=original
            return baseline
        transition._protected=first
    def trace(event):
        events.append(event)
        state=transition._journal
        pending=state['pending']
        retiring=pending is not None and state['actions'][pending]['kind']=='retire'
        before=retiring and event[0]=='journal-parent-fsync'
        after=event[0]=='rename-no-replace' and n.decode_path(event[1]).endswith(b'/retired.git')
        if not injected and (before or phase=='after-retirement' and after):
            if phase=='after-retirement' and not after:return
            diagnostic_churn(root);injected.append(freeze(root))
            if phase!='running':raise Crash()
    transition.trace=trace
    if phase=='running':
        result=transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    else:
        with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
        reopened=t.Transition(p,trace=events.append)
        confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        result=reopened.resume(operation=operation,confirmation=confirmation)
    assert result['phase']==t.TERMINAL and injected
    assert freeze(root)==injected[0]
    for _ in range(2):
        diagnostic_churn(root);before=freeze(root);effects=[]
        reopened=t.Transition(p,trace=effects.append)
        confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        assert reopened.resume(operation=operation,confirmation=confirmation)['phase']==t.TERMINAL
        assert effects==[] and freeze(root)==before
    for kind,path in events:
        path=n.decode_path(path)
        assert (path.startswith(t.STORE+b'/') or path==t.STORE or
                path in (b'data/native',b'data/native/repository.git',b'data/native/repository-files',
                         b'data/native/root-ignore-policy',b'.gitignore',b'data/repo',b'data/repo/.git'))


@pytest.mark.parametrize('change',['mapped','source-file','source-membership','source-index','mapping','mapping-inode','absent-mapped'])
def test_scoped_live_guard_retains_strict_inputs(tmp_path,change):
    root,repo,p,candidate=prepare(tmp_path)
    if change=='absent-mapped':
        write(repo,b'homeassistant/new',b'new source');git(repo,'add','homeassistant/new')
        candidate=p.prepare(expected=candidate['digest'])
    class Crash(BaseException):pass
    transition,operation,confirmation=begin(p,candidate)
    def crash(event):
        pending=transition._journal['pending']
        if (event[0]=='journal-parent-fsync' and pending is not None and
                transition._journal['actions'][pending]['kind']=='retire'):raise Crash()
    transition.trace=crash
    with pytest.raises(Crash):transition.start(candidate=candidate['digest'],operation=operation,confirmation=confirmation)
    if change=='mapped':write(root,b'homeassistant/a',b'changed participating')
    elif change=='source-file':write(repo,b'homeassistant/a',b'changed source')
    elif change=='source-membership':write(repo,b'untracked-new',b'new source')
    elif change=='source-index':git(repo,'update-index','--assume-unchanged','homeassistant/a')
    elif change=='mapping':p.mappings=((b'homeassistant',b'different'),)
    elif change=='mapping-inode':
        (root/'homeassistant').rename(root/'old-homeassistant');shutil.copytree(root/'old-homeassistant',root/'homeassistant')
    elif change=='absent-mapped':write(root,b'homeassistant/new',b'unexpected creation')
    before=freeze(root);effects=[];reopened=t.Transition(p,trace=effects.append)
    with pytest.raises(n.Blocked):
        confirmation=reopened.confirm(candidate=candidate['digest'],operation=operation,action='resume',approved=True)
        reopened.resume(operation=operation,confirmation=confirmation)
    assert freeze(root)==before and (repo/'.git').is_dir()
    assert not any(kind=='rename-no-replace' for kind,path in effects)


def test_live_guard_never_enumerates_diagnostics(tmp_path,monkeypatch):
    root,repo,p,candidate=prepare(tmp_path)
    transition=t.Transition(p)
    expected=transition._protected()
    diagnostic_churn(root)
    original=os.listdir
    def listdir(fd):
        if isinstance(fd,int) and os.fstat(fd).st_ino==(root/'homeassistant').stat().st_ino:
            pytest.fail('live diagnostic tree enumerated')
        return original(fd)
    monkeypatch.setattr(os,'listdir',listdir)
    assert transition._protected()==expected


def test_old_schema_nested_mapping_anchors_use_candidate_evidence(tmp_path):
    root,repo,p,candidate=prepare(tmp_path)
    write(root,b'homeassistant/nested/a',b'live nested')
    p=n.Preparation(binding(root),mappings=((b'homeassistant',b'homeassistant/nested'),),
                    app_identity='fixture-app-1',manifest_digest='a'*64)
    candidate=p.prepare(expected=candidate['digest'])
    transition=t.Transition(p)
    old=old_full_protected(transition)
    assert n.encode_path(b'homeassistant') not in {row[0] for row in old}
    assert transition._preserved(old)==transition._preserved(transition._protected())
    (root/'homeassistant').rename(root/'old-homeassistant')
    shutil.copytree(root/'old-homeassistant',root/'homeassistant')
    assert transition._preserved(old)!=transition._preserved(transition._protected())


@pytest.mark.parametrize('change',['missing','duplicate'])
def test_scoped_saved_participating_rows_cannot_disappear(tmp_path,change):
    root,repo,p,candidate=prepare(tmp_path);transition=t.Transition(p)
    rows=transition._protected();target=n.encode_path(b'homeassistant/a')
    if change=='missing':rows=[row for row in rows if row[0]!=target]
    else:rows.append(next(row for row in rows if row[0]==target))
    with pytest.raises(n.Blocked):transition._preserved(rows)
