"""Explicit diagnostic migration preparation; disposable Git and Linux fixtures."""
import ast
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_migration as n
import native_disk as disk_module
from native_inventory import Mount, Policy, linux_mounts
from native_metadata import Binding
from direct_auth import Authority


@pytest.fixture(autouse=True)
def synthetic_host_allocation():
    """Portable logic only; real Linux tests retain kernel allocation profiles."""
    if sys.platform != 'linux':
        original=disk_module.NativeDisk._mount
        def mount(self,path,info,mounts):
            key,identity,fs,supported=original(self,path,info,mounts)
            return key,identity,b'tmpfs',True
        with mock.patch.object(disk_module.NativeDisk,'_mount',mount): yield
    else: yield


def git(root,*args,input=None):
    env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
         'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':'fixture@example.invalid',
         'GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}
    return subprocess.check_output(['git','-C',str(root),*args],input=input,env=env,stderr=subprocess.PIPE)


def write(root,path,data=b'A'):
    full=os.fsencode(root)+b'/'+os.fsencode(path)
    os.makedirs(os.path.dirname(full),exist_ok=True)
    with open(full,'wb') as stream: stream.write(data)


def binding(root,policy=None):
    root=os.fsencode(Path(root).resolve())
    if sys.platform=='linux': mounts=linux_mounts()
    else:
        s=os.stat(root)
        mounts=(Mount(1,0,f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}'.encode(),b'/',b'/',b'rw',b'fixture',b'fixture',b'rw'),)
    return Binding(root,policy or Policy(),mounts,tuple(m.id for m in mounts),True)


def setup(tmp_path,fmt='sha1',**kwargs):
    root=tmp_path.resolve();(root/'data/repo').mkdir(parents=True)
    repo=root/'data/repo';git(repo,'init','--initial-branch=main','--object-format='+fmt)
    write(repo,b'homeassistant/a',b'old');git(repo,'add','.');git(repo,'commit','-qm','initial')
    write(root,b'homeassistant/a',b'live')
    p=n.Preparation(binding(root),app_identity='fixture-app-1',manifest_digest='a'*64,**kwargs)
    return root,repo,p


def rows(observation):
    return {n.decode_path(r['source']):r for r in observation['rows'] if r['source'] is not None}


def frozen(root):
    result={}
    for base,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if d!='native-migration-preparation']
        for name in dirs+files:
            path=Path(base)/name;s=path.lstat()
            raw=path.read_bytes() if stat.S_ISREG(s.st_mode) else None
            result[os.fsencode(path.relative_to(root))]=(s.st_mode,s.st_uid,s.st_gid,s.st_mtime_ns,s.st_ctime_ns,raw)
    # /data membership legitimately gains the dedicated preparation directory.
    result.pop(b'data',None)
    result.pop(b'data/.native-disk-admission.lock',None)
    return result


@pytest.mark.parametrize('fmt',['sha1','sha256'])
def test_complete_git_filesystem_and_layout_inventory(tmp_path,fmt):
    root,repo,p=setup(tmp_path,fmt)
    write(repo,b'.gitignore',b'ignored/\n');write(repo,b'ignored/deep/a',b'ignored')
    (repo/'empty').mkdir();write(repo,b'untracked',b'new')
    write(repo,b'homeassistant/a',b'staged');git(repo,'add','homeassistant/a')
    staged=git(repo,'rev-parse',':homeassistant/a').strip().decode()
    write(repo,b'homeassistant/a',b'working')
    write(repo,b'homeassistant/missing',b'gone');git(repo,'add','homeassistant/missing');(repo/'homeassistant/missing').unlink()
    git(repo,'branch','ha-ops/service');git(repo,'tag','fixture-tag');git(repo,'update-ref','refs/remotes/origin/main','HEAD')
    git(repo,'update-ref','refs/stash','HEAD');git(repo,'pack-refs','--all')
    write(repo,b'.git/MERGE_HEAD',git(repo,'rev-parse','HEAD'))
    write(root,b'homeassistant/native-only',b'live only');write(root,b'homeassistant/empty-live/.keep')
    before=frozen(root);ob=p.observe();r=rows(ob)
    assert frozen(root)==before
    for path in (b'ignored/deep/a',b'empty',b'untracked',b'homeassistant/missing'):
        assert path in r
    assert ob['git']['format']==fmt and ob['git']['ignored'] and ob['git']['untracked']
    refs=__import__('base64').b64decode(ob['git']['refs'])
    for name in (b'refs/heads/ha-ops/service',b'refs/tags/fixture-tag',b'refs/stash',b'refs/remotes/origin/main'):
        assert name in refs
    ix=ob['git']['index'][r[b'homeassistant/a']['index'][0]]
    assert ix['oid']==staged and r[b'homeassistant/a']['after']['digest']==hashlib.sha256(b'working').hexdigest()
    assert r[b'homeassistant/a']['before']['digest']==hashlib.sha256(b'live').hexdigest()
    assert 'content' in r[b'homeassistant/a']['changes']
    assert r[b'homeassistant/missing']['after'] is None
    assert any('pending:' in issue for issue in ob['blockers'])
    assert any(row['source'] is None and row['destination']==n.encode_path(b'homeassistant/native-only') for row in ob['rows'])


def test_roundtrip_restart_stale_sticky_and_explicit_reprepare(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);before=frozen(root)
    first=p.prepare(expected=None);assert frozen(root)==before
    reopened=n.Preparation(p.binding,app_identity=p.app,manifest_digest=p.manifest)
    monkeypatch.setattr(reopened,'observe',lambda:(_ for _ in ()).throw(AssertionError('automatic source work')))
    assert reopened.read()==first
    monkeypatch.undo();assert p.revalidate()=='current'
    old=(repo/'homeassistant/a').read_bytes();write(repo,b'homeassistant/a',b'changed')
    assert p.revalidate()=='stale';write(repo,b'homeassistant/a',old)
    assert p.revalidate()=='stale'
    current=p.read();second=p.prepare(expected=current['digest'])
    assert second['generation']!=first['generation'] and p.revalidate()=='current'
    with pytest.raises(n.Conflict):p.prepare(expected=first['digest'])
    assert p.read()==second


@pytest.mark.parametrize('change',['bytes','mode','ref','index','pending','mapping','manifest','app','config','auth'])
def test_all_bound_inputs_invalidate(tmp_path,change):
    options={};root,repo,p=setup(tmp_path,options=lambda:options)
    p.prepare(expected=None)
    if change=='bytes':write(repo,b'homeassistant/a',b'changed')
    if change=='mode':os.chmod(repo/'homeassistant/a',0o4750)
    if change=='ref':git(repo,'update-ref','refs/heads/another','HEAD')
    if change=='index':write(repo,b'homeassistant/new');git(repo,'add','homeassistant/new')
    if change=='pending':write(repo,b'.git/MERGE_HEAD',git(repo,'rev-parse','HEAD'))
    if change=='mapping':p=n.Preparation(p.binding,mappings=((b'homeassistant',b'addon_configs/exact_slug'),),app_identity=p.app,manifest_digest=p.manifest)
    if change=='manifest':p.manifest='b'*64
    if change=='app':p.app='fixture-app-2'
    if change=='config':options['direct_https']=True
    if change=='auth':Authority(root/'data')
    assert p.revalidate()=='stale'


def test_private_auth_preserved_and_never_opened(tmp_path,monkeypatch):
    options={'direct_https':False,'direct_origin':'http://ha.local:8100','direct_keyfile':'tls.key',
             'direct_certfile':'tls.crt','unknown':'PRIVATE_CANARY_UNKNOWN'}
    root,repo,p=setup(tmp_path,options=lambda:options);authority=Authority(root/'data');address='http://ha.local:8100'
    bearer=authority.exchange(authority.issue(authority.nonce(()),(),address).split('#grant=')[1],address)
    cookie=authority.cookie(bearer);tokens=root/'data/direct-auth/tokens.json';raw=tokens.read_bytes()
    private=json.loads(raw);before=frozen(root);original=os.open
    def guarded(path,flags,*args,**kwargs):
        assert os.fsencode(path)!=b'tokens.json','auth payload opened'
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',guarded);candidate=p.prepare(expected=None);monkeypatch.undo()
    assert frozen(root)==before and tokens.read_bytes()==raw
    assert Authority(root/'data').authenticate(cookie,address)
    serialized=n._json(candidate)
    for value in (private['installation'],private['tokens'][0]['id'],private['tokens'][0]['hash'],bearer,cookie,options['unknown'],options['direct_keyfile'],options['direct_origin']):
        assert value.encode() not in serialized
    assert candidate['observation']['private']['auth_tokens']['digest'] is None


def test_absent_auth_stays_absent_and_secrets_not_hashed(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path)
    secret=b'CANARY_SECRET_PAYLOAD'
    for path in (b'homeassistant/secrets.yaml',b'homeassistant/.storage/auth',b'.storage_managed/core.config_entries.json'):
        write(repo,path,secret)
    write(root,b'homeassistant/secrets.yaml',secret)
    original=p._read
    def guarded(fd,path,**kw):
        assert not path.endswith((b'secrets.yaml',b'/auth',b'core.config_entries.json'))
        return original(fd,path,**kw)
    monkeypatch.setattr(p,'_read',guarded);ob=p.prepare(expected=None)['observation']
    assert not (root/'data/direct-auth').exists()
    assert hashlib.sha256(secret).hexdigest().encode() not in n._json(ob)
    assert rows(ob)[b'.storage_managed/core.config_entries.json']['disposition']=='retired'


@pytest.mark.parametrize('mappings',[
    ((b'homeassistant',b'homeassistant'),(b'homeassistant/a',b'addon_configs/exact')),
    ((b'a',b'homeassistant/x'),(b'ab',b'homeassistant/x/y')),
    ((b'../x',b'homeassistant'),),((b'a',b'data/direct-auth'),),((b'a',b'/homeassistant'),)])
def test_ambiguous_or_unsafe_mappings_refused(tmp_path,mappings):
    root,repo,p=setup(tmp_path)
    with pytest.raises(n.Blocked):n.Preparation(p.binding,mappings=mappings,app_identity=p.app,manifest_digest=p.manifest)


def test_exact_mapping_unresolved_retired_and_required_parents(tmp_path):
    root,repo,p=setup(tmp_path,mappings=((b'homeassistant',b'homeassistant'),(b'addons/source',b'addon_configs/exact_slug/deep')))
    write(repo,b'addons/source/settings.yaml');write(repo,b'README.md');ob=p.observe();r=rows(ob)
    assert r[b'addons/source/settings.yaml']['destination']==n.encode_path(b'addon_configs/exact_slug/deep/settings.yaml')
    assert r[b'README.md']['disposition']=='unresolved'
    assert any(row['disposition']=='required-parent' and row['destination']==n.encode_path(b'addon_configs') for row in ob['rows'])


@pytest.mark.skipif(sys.platform!='linux',reason='Linux exact byte names and numeric ownership')
def test_linux_arbitrary_bytes_full_modes_and_owners(tmp_path):
    root,repo,p=setup(tmp_path)
    names=(b'\xff literal [*]?\t ',b'line\nbreak',b'cr\rname')
    for name in names:write(repo,b'homeassistant/'+name)
    leaf=os.fsencode(repo)+b'/homeassistant/'+names[0];os.chmod(leaf,0o6751)
    if os.geteuid()==0:os.chown(leaf,1234,2345);os.chmod(leaf,0o6751)
    r=rows(p.observe())
    assert r[b'homeassistant/'+names[0]]['after']['mode']==0o6751
    if os.geteuid()==0:assert (r[b'homeassistant/'+names[0]]['after']['uid'],r[b'homeassistant/'+names[0]]['after']['gid'])==(1234,2345)
    for name in names[1:]:assert r[b'homeassistant/'+name]['disposition']=='excluded'
    assert n.decode_path(n.encode_path(names[0]))==names[0]


@pytest.mark.parametrize('damage',['auth-symlink','auth-fifo','auth-hardlink','auth-mode','root-symlink','root-unverified','occupied','git-file','alternates'])
def test_unsafe_descriptors_and_routes_fail_closed(tmp_path,damage):
    root,repo,p=setup(tmp_path)
    if damage.startswith('auth-'):
        Authority(root/'data');token=root/'data/direct-auth/tokens.json'
        if damage=='auth-symlink':token.rename(token.with_suffix('.old'));token.symlink_to(token.with_suffix('.old'))
        if damage=='auth-fifo':token.unlink();os.mkfifo(token,0o600)
        if damage=='auth-hardlink':os.link(token,root/'data/second')
        if damage=='auth-mode':token.chmod(0o644)
        with pytest.raises((OSError,n.Blocked)):p.prepare(expected=None)
    elif damage=='root-symlink':
        alias=root.parent/(root.name+'-alias');alias.symlink_to(root,target_is_directory=True)
        p.binding=dataclasses.replace(p.binding,root=os.fsencode(alias));p.root=os.fsencode(alias)
        with pytest.raises((OSError,n.Blocked)):p.prepare(expected=None)
    elif damage=='root-unverified':
        p.binding=dataclasses.replace(p.binding,topology_verified=False)
        with pytest.raises(n.Blocked):p.prepare(expected=None)
    elif damage=='occupied':
        write(root,b'data/native/repository.git/sentinel');ob=p.observe()
        assert any('occupied-native-anchor' in i for i in ob['blockers'])
    else:
        if damage=='git-file':(repo/'.git').rename(repo/'.git-old');write(repo,b'.git',b'gitdir: /foreign\n')
        else:write(repo,b'.git/objects/info/alternates',b'/foreign')
        assert any('git-' in i for i in p.observe()['blockers'])


def test_executable_git_config_and_ambient_routing_never_run(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);sentinel=root/'executed';hook=root/'hook'
    hook.write_text('#!/bin/sh\ntouch '+str(sentinel)+'\n');hook.chmod(0o755)
    git(repo,'config','core.fsmonitor',str(hook));git(repo,'config','filter.evil.clean',str(hook))
    monkeypatch.setenv('GIT_DIR','/foreign');monkeypatch.setenv('GIT_INDEX_FILE','/foreign');monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    before=frozen(root);ob=p.observe()
    assert ob['blockers'] and not sentinel.exists() and frozen(root)==before


def test_conflict_stages_and_detached_unborn_head(tmp_path):
    root,repo,p=setup(tmp_path);oid=git(repo,'rev-parse',':homeassistant/a').strip()
    git(repo,'update-index','--index-info',input=b'0 '+b'0'*40+b'\thomeassistant/a\n'+b''.join(b'100644 '+oid+b' '+str(i).encode()+b'\thomeassistant/a\n' for i in (1,2,3)))
    assert [r['stage'] for r in p.observe()['git']['index']]==[1,2,3]
    git(repo,'checkout','--detach','-f');assert __import__('base64').b64decode(p.observe()['git']['head']).strip()==git(repo,'rev-parse','HEAD').strip()
    git(repo,'symbolic-ref','HEAD','refs/heads/unborn');assert b'unborn' in __import__('base64').b64decode(p.observe()['git']['head'])


def test_cooperative_deep_churn_never_publishes(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);original=p._observe_once;count=0
    def changing():
        nonlocal count
        result=original();count+=1
        if count==1:write(repo,b'homeassistant/deep/new',b'new')
        return result
    monkeypatch.setattr(p,'_observe_once',changing)
    with pytest.raises(n.Blocked,match='inputs-changed'):p.prepare(expected=None)
    assert p.read() is None


@pytest.mark.parametrize('failure',['write','file-fsync','replace','directory-fsync'])
def test_atomic_persistence_failure_is_truthful(tmp_path,monkeypatch,failure):
    root,repo,p=setup(tmp_path);first=p.prepare(expected=None)
    write(repo,b'homeassistant/a',b'new');original_fsync=os.fsync
    if failure=='write':monkeypatch.setattr(n.os,'write',mock.Mock(side_effect=OSError('fixture ENOSPC')))
    if failure=='replace':monkeypatch.setattr(n.os,'replace',mock.Mock(side_effect=OSError('fixture replace')))
    if failure.endswith('fsync'):
        def failing(fd):
            is_directory=stat.S_ISDIR(os.fstat(fd).st_mode)
            target=(root/'data/native-migration-preparation').stat().st_ino
            if (failure=='directory-fsync' and is_directory and os.fstat(fd).st_ino==target) or (failure=='file-fsync' and not is_directory):raise OSError('fixture fsync')
            return original_fsync(fd)
        monkeypatch.setattr(n.os,'fsync',failing)
    with pytest.raises(OSError):p.prepare(expected=first['digest'])
    monkeypatch.undo();current=p.read()
    if failure=='directory-fsync':assert current['generation']!=first['generation']
    else:assert current==first
    assert not list((root/'data/native-migration-preparation').glob('.pending-*'))


def test_short_writes_finish_and_foreign_temp_survives(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);original=os.write
    monkeypatch.setattr(n.os,'write',lambda fd,data:original(fd,data[:7]))
    assert p.prepare(expected=None)['state']=='prepared'
    monkeypatch.undo()
    # An unrecognized extra temp is never a cleanup target.
    extra=root/'data/native-migration-preparation/.pending-foreign';extra.write_bytes(b'foreign')
    with pytest.raises(n.Blocked):p.read()
    assert extra.read_bytes()==b'foreign'


@pytest.mark.parametrize('damage',['duplicate','unknown','schema','path','digest','mode','symlink','hardlink','fifo'])
def test_strict_candidate_corruption_refusal(tmp_path,damage):
    root,repo,p=setup(tmp_path);p.prepare(expected=None);path=root/'data/native-migration-preparation/candidate.json'
    raw=json.loads(path.read_bytes())
    if damage=='duplicate':path.write_bytes(b'{"schema":1,"schema":1}')
    elif damage in ('unknown','schema','path','digest'):
        if damage=='unknown':raw['operation']={}
        if damage=='schema':raw['schema']=3
        if damage=='path':raw['observation']['source'][0]['path']='Li4veA=='
        if damage=='digest':raw['digest']='0'*64
        path.write_bytes(n._json(raw))
    elif damage=='mode':path.chmod(0o644)
    elif damage=='symlink':path.rename(path.with_suffix('.old'));path.symlink_to(path.with_suffix('.old'))
    elif damage=='hardlink':os.link(path,path.with_suffix('.copy'))
    else:path.unlink();os.mkfifo(path,0o600)
    with pytest.raises((OSError,n.Blocked)):p.read()


def test_no_authority_or_production_import():
    app=Path(n.__file__).parent
    for path in app.glob('*.py'):
        if path.name in ('native_migration.py','native_disk.py','native_migration_transition.py','native_migration_git_candidate.py'):continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import):assert all(i.name.split('.')[-1]!='native_migration' for i in node.names)
            if isinstance(node,ast.ImportFrom):
                assert (node.module or '').split('.')[-1]!='native_migration'
                assert all(i.name!='native_migration' for i in node.names)
    for forbidden in ('apply','execute','publish','restore','activate','claim','approve'):
        assert not hasattr(n.Preparation,forbidden)


def test_pending_ha_ops_state_is_separate_safe_and_invalidates(tmp_path):
    root,repo,p=setup(tmp_path)
    write(root,b'data/state.json',n._json(dict(active_operation={'command':'apply','secret':'CANARY_OPERATION_SECRET'},
        save_push_retry_pending=True,last_diff='CANARY_DIFF_PAYLOAD')))
    write(root,b'data/native-operations/state.json',n._json(dict(active='CANARY_OPERATION_ID',
        workers={'x':{'phase':'accepted'}},records={'x':{}},scope_pending={'x':'CANARY_SCOPE_PAYLOAD'})))
    before=frozen(root);first={'observation':p.observe()}
    with pytest.raises(n.Blocked):p.prepare(expected=None)
    assert p.read() is None
    assert frozen(root)==before
    op=first['observation']['operations']
    assert op['legacy']['summary']['active_present'] and op['native']['summary']['pending_present']
    assert op['native']['summary']['workers']==1 and 'pending-ha-ops-operation' in first['observation']['blockers']
    for canary in (b'CANARY_OPERATION_SECRET',b'CANARY_DIFF_PAYLOAD',b'CANARY_OPERATION_ID',b'CANARY_SCOPE_PAYLOAD'):
        assert canary not in n._json(first)
    assert not (root/'data/native-migration-preparation').exists()


def test_missing_index_secret_gets_excluded_before_layout(tmp_path):
    root,repo,p=setup(tmp_path);write(repo,b'homeassistant/secrets.yaml',b'SECRET')
    git(repo,'add','homeassistant/secrets.yaml');(repo/'homeassistant/secrets.yaml').unlink()
    row=rows(p.observe())[b'homeassistant/secrets.yaml']
    assert row['disposition']=='excluded' and row['destination'] is None and row['after'] is None


def test_metadata_unchanged_directory_and_type_diff(tmp_path):
    root,repo,p=setup(tmp_path)
    write(root,b'homeassistant/a',b'old');os.chmod(root/'homeassistant/a',0o644);os.chmod(repo/'homeassistant/a',0o644)
    write(repo,b'homeassistant/meta',b'same');write(root,b'homeassistant/meta',b'same')
    os.chmod(repo/'homeassistant/meta',0o4751);os.chmod(root/'homeassistant/meta',0o600)
    write(repo,b'homeassistant/replacement',b'new');(root/'homeassistant/replacement').mkdir()
    r=rows(p.observe())
    assert r[b'homeassistant/a']['changes']==['unchanged']
    assert r[b'homeassistant/meta']['changes']==['metadata']
    assert 'type' in r[b'homeassistant/replacement']['changes']
    assert r[b'homeassistant']['after']['kind']=='directory'


def test_native_secret_directory_never_traversed(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);write(root,b'homeassistant/.cloud/nested/credential',b'NEVER_READ')
    original=p._directory
    def guarded(fd,path):
        assert not path.endswith(b'/.cloud')
        return original(fd,path)
    monkeypatch.setattr(p,'_directory',guarded)
    ob=p.observe()
    assert all(not n.decode_path(row['path']).endswith(b'credential') for row in ob['native'])


def test_unsafe_revalidation_never_revives_after_repair(tmp_path):
    root,repo,p=setup(tmp_path);Authority(root/'data');p.prepare(expected=None)
    token=root/'data/direct-auth/tokens.json';token.chmod(0o644)
    assert p.revalidate()=='unavailable';token.chmod(0o600)
    assert p.revalidate()=='stale'


def test_changed_mount_evidence_refused(tmp_path):
    root,repo,p=setup(tmp_path)
    mounts=tuple(dataclasses.replace(m,source=b'changed-source') for m in p.binding.mounts)
    p.binding=dataclasses.replace(p.binding,mounts=mounts)
    if sys.platform=='linux':
        with pytest.raises(n.Blocked,match='stale'):p.observe()
    else:
        p.binding=dataclasses.replace(p.binding,confirmed_mounts=())
        with pytest.raises(n.Blocked):p.observe()


def test_foreign_candidate_during_write_is_preserved(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);first=p.prepare(expected=None)
    foreign=copy.deepcopy(first);foreign['generation']='f'*32
    foreign['digest']=n._digest({k:v for k,v in foreign.items() if k!='digest'})
    candidate=root/'data/native-migration-preparation/candidate.json';original=os.write;done=False
    def replace_during_write(fd,data):
        nonlocal done
        if not done:
            done=True;temporary=candidate.with_suffix('.foreign');temporary.write_bytes(n._json(foreign));temporary.chmod(0o600);temporary.replace(candidate)
        return original(fd,data)
    monkeypatch.setattr(n.os,'write',replace_during_write)
    with pytest.raises(n.Conflict):p.prepare(expected=first['digest'])
    monkeypatch.undo();assert p.read()==foreign


def test_create_open_failure_preserves_absence(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);original=os.open
    def denied(path,flags,*args,**kwargs):
        if flags & os.O_CREAT and os.fsencode(path).startswith(b'.pending-'):raise OSError('fixture O_EXCL ENOSPC')
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(n.os,'open',denied)
    with pytest.raises(OSError):p.prepare(expected=None)
    assert p.read() is None


def test_independent_process_cas_has_exactly_one_winner(tmp_path):
    import multiprocessing
    root,repo,p=setup(tmp_path);ctx=multiprocessing.get_context('fork');start=ctx.Event();queue=ctx.Queue()
    def worker():
        start.wait(5)
        try:queue.put(('prepared',p.prepare(expected=None)['digest']))
        except n.Conflict:queue.put(('conflict',None))
        except n.Blocked:queue.put(('blocked',None))
        except BaseException as error:queue.put(('error',type(error).__name__))
    children=[ctx.Process(target=worker) for _ in range(2)]
    for child in children:child.start()
    start.set()
    try:
        results=[queue.get(timeout=15) for _ in children]
        for child in children:child.join(5);assert child.exitcode==0
        assert sum(result[0]=='prepared' for result in results)==1
        assert all(result[0] in ('prepared','conflict','blocked') for result in results)
        assert p.read()['digest']==next(value for status,value in results if status=='prepared')
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(5)
        queue.close();queue.join_thread()


@pytest.mark.parametrize('boundary',['before-replace','after-replace'])
def test_process_death_keeps_only_complete_diagnostics(tmp_path,boundary):
    import multiprocessing
    root,repo,p=setup(tmp_path);first=p.prepare(expected=None);write(repo,b'homeassistant/a',b'new')
    def worker():
        original=os.replace
        def crash(*args,**kwargs):
            if boundary=='before-replace':os._exit(73)
            original(*args,**kwargs);os._exit(74)
        os.replace=crash;p.prepare(expected=first['digest'])
    child=multiprocessing.get_context('fork').Process(target=worker);child.start();child.join(15)
    try:
        assert child.exitcode==(73 if boundary=='before-replace' else 74)
        current=p.read()
        if boundary=='before-replace':assert current==first
        else:assert current['generation']!=first['generation'] and current['state']=='prepared'
        assert not any(key in current for key in ('approval','selection','claim','command','worker','candidate_commit','active_scope'))
    finally:
        if child.is_alive():child.terminate();child.join(5)


def test_service_and_recovery_descriptors_are_explicit_blockers(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path)
    write(root,b'data/native-services/state.json',n._json(dict(operation='PRIVATE_OPERATION_ID',phase='stopping',pending={'credential':'PRIVATE_PAYLOAD'})))
    write(root,b'data/native/recovery/active.json',b'{"generation":"../../foreign","key":"PRIVATE_KEY"}')
    original=p._read
    def guarded(fd,path,**kwargs):
        assert not path.endswith(b'/active.json')
        return original(fd,path,**kwargs)
    monkeypatch.setattr(p,'_read',guarded);before=frozen(root);candidate=p.prepare(expected=None)
    assert frozen(root)==before
    operations=candidate['observation']['operations']
    assert operations['services']['summary']['pending_present'] and operations['recovery']['summary']['pending_present']
    assert 'unvalidated-recovery-pointer' in candidate['observation']['blockers']
    assert all(value not in n._json(candidate) for value in (b'PRIVATE_OPERATION_ID',b'PRIVATE_PAYLOAD',b'PRIVATE_KEY',b'../../foreign'))


def test_native_unsupported_leaves_keep_detailed_blockers(tmp_path):
    root,repo,p=setup(tmp_path)
    (root/'homeassistant/link').symlink_to('/foreign')
    os.mkfifo(root/'homeassistant/fifo',0o600)
    ob=p.observe();native={n.decode_path(row['path']):row for row in ob['native']}
    assert native[b'homeassistant/link']['reason']=='diagnostic-only:unsupported-symlink'
    assert native[b'homeassistant/fifo']['reason']=='diagnostic-only:unsupported-special'
    assert not any(reason.startswith('unsupported-symlink:') for reason in ob['blockers'])
    assert not any(reason.startswith('unsupported-special:') for reason in ob['blockers'])


def test_git_subprocess_never_reads_tracked_excluded_payloads(tmp_path):
    root,repo,p=setup(tmp_path)
    paths=(b'homeassistant/.storage/auth',b'homeassistant/secrets.yaml',b'.storage_managed/core.config_entries.json')
    for path in paths:write(repo,path,b'AAAA')
    git(repo,'add','.');git(repo,'commit','-qm','excluded fixture')
    for path in paths:
        write(repo,path,b'BBBB');full=os.fsencode(repo)+b'/'+path
        os.utime(full,ns=(1,os.stat(full).st_mtime_ns))
    write(repo,b'homeassistant/a',b'eligible changed')
    ob=p.observe()
    # Isolated access markers prove subprocess non-reading, not atime invariance.
    for path in paths:assert os.stat(os.fsencode(repo)+b'/'+path).st_atime_ns==1
    assert all(n.encode_path(path) not in ob['git']['status_paths'] for path in paths)
    assert n.encode_path(b'homeassistant/a') in ob['git']['status_paths']
    assert n._bytes(ob['git']['status'])


@pytest.mark.parametrize('status,unresolved',[('accepted',True),('running',True),('failed_unknown',True),('terminal',False),('failed',False),('cancelled',False)])
def test_legacy_command_lifecycle_debt(tmp_path,status,unresolved):
    root,repo,p=setup(tmp_path)
    write(root,b'data/state.json',n._json(dict(active_operation=None,command_records={'PRIVATE_ID':dict(status=status,payload='PRIVATE_PAYLOAD')})))
    ob=p.observe();summary=ob['operations']['legacy']['summary']
    assert summary['records']==1 and summary['workers']==int(unresolved)
    assert summary['active_present']==unresolved and summary['pending_present']==unresolved
    assert ('pending-ha-ops-operation' in ob['blockers'])==unresolved
    assert b'PRIVATE_ID' not in n._json(ob) and b'PRIVATE_PAYLOAD' not in n._json(ob)


def test_real_service_abandon_is_terminal_history(tmp_path):
    from native_services import ServiceStore
    root,repo,p=setup(tmp_path)
    class Adapter:
        def inventory(self,*,timeout):
            return dict(complete=True,self='app:ha_ops',entries=[
                dict(id='core',startup='core',state='stopped',manageable=True,capability='manageable'),
                dict(id='app:ha_ops',startup='application',state='running',manageable=True,capability='manageable')])
    store=ServiceStore(root/'data/native-services');state=store.prepare(Adapter())
    assert p.observe()['operations']['services']['summary']['active_present']
    state=store.abandon_prepared(state['operation'],state['owner'],state['revision'])
    ob=p.observe();summary=ob['operations']['services']['summary']
    assert state['phase']=='restored' and not summary['active_present'] and not summary['pending_present']
    assert 'pending-ha-ops-operation' not in ob['blockers']
    assert ob['operations']['services']['object'] is not None


@pytest.mark.parametrize('kind',['file','directory','tracked','ignored'])
def test_literal_legacy_root_namespace_is_lossless(tmp_path,kind):
    root,repo,p=setup(tmp_path)
    if kind=='directory':(repo/'.legacy-root').mkdir()
    else:write(repo,b'.legacy-root',b'literal user bytes')
    if kind=='tracked':git(repo,'add','.legacy-root')
    if kind=='ignored':write(repo,b'.gitignore',b'.legacy-root\n')
    candidate=p.prepare(expected=None);ob=candidate['observation']
    assert b'.legacy-root' in rows(ob)
    assert len([row for row in ob['source'] if n.decode_path(row['path'])==b'.legacy-root'])==1
    assert len(ob['binding']['legacy_identity'])==9 and p.read()==candidate
    os.chmod(repo,0o750);assert p.revalidate()=='stale'


@pytest.mark.parametrize('field,value',[
    ('head','not base64'),('head','cmVmOiByZWZzL2hlYWRzL21haW4=\n'),
    ('head',__import__('base64').b64encode(b'not a HEAD record\n').decode()),
    ('head',__import__('base64').b64encode(b'0'*39+b'\n').decode()),
    ('refs',__import__('base64').b64encode(b'invalid refs\n').decode()),
    ('status',__import__('base64').b64encode(b'broken status\0').decode()),
    ('ignored',__import__('base64').b64encode(b'../escape\0').decode()),
    ('untracked',__import__('base64').b64encode(b'truncated').decode())])
def test_recomputed_candidate_digest_does_not_bypass_git_codecs(tmp_path,field,value):
    root,repo,p=setup(tmp_path);candidate=p.prepare(expected=None)
    candidate['observation']['git'][field]=value
    candidate['input_digest']=n._digest(candidate['observation'])
    candidate['digest']=n._digest({k:v for k,v in candidate.items() if k!='digest'})
    path=root/'data/native-migration-preparation/candidate.json';path.write_bytes(n._json(candidate))
    with pytest.raises(n.Blocked):p.read()


@pytest.mark.parametrize('route',[b'data/direct-auth',b'data/direct-auth/nested',b'data/native-operations',b'data/native-services',b'data/native/recovery',b'data/native-migration-preparation'])
def test_legacy_route_cannot_overlap_private_namespaces(tmp_path,route):
    root=tmp_path.resolve()
    with pytest.raises(n.Blocked):
        n.Preparation(binding(root,Policy(legacy_repo=route)),app_identity='fixture',manifest_digest='a'*64)
    assert not list(root.iterdir())


@pytest.mark.parametrize('anchor',['root','data'])
def test_unavailable_anchor_is_never_absence(tmp_path,anchor):
    root,repo,p=setup(tmp_path);p.prepare(expected=None)
    path=root if anchor=='root' else root/'data';moved=path.with_name(path.name+'-away');path.rename(moved)
    try:
        with pytest.raises(FileNotFoundError):p.read()
        assert p.revalidate()=='unavailable'
    finally:moved.rename(path)
    assert p.read() is not None


def test_parent_creation_fsync_is_retried_for_existing_directory(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);data_inode=(root/'data').stat().st_ino;original=os.fsync;failed=False;seen=[]
    def fail_parent(fd):
        nonlocal failed
        if os.fstat(fd).st_ino==data_inode and not failed:
            failed=True;raise OSError('fixture first parent fsync')
        return original(fd)
    monkeypatch.setattr(n.os,'fsync',fail_parent)
    with pytest.raises(OSError):p.prepare(expected=None)
    assert (root/'data/native-migration-preparation').is_dir()
    def recorded(fd):seen.append(os.fstat(fd).st_ino);return original(fd)
    monkeypatch.setattr(n.os,'fsync',recorded);assert p.prepare(expected=None)['state']=='prepared'
    assert data_inode in seen


def test_every_git_command_preserves_private_scan_boundaries(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path)
    private=(b'homeassistant/.ssh',b'homeassistant/.gnupg',b'homeassistant/.cloud',b'homeassistant/.storage/auth')
    markers=[]
    for directory in private:
        write(repo,directory+b'/.gitignore',b'PRIVATE_IGNORE_CANARY\n')
        write(repo,directory+b'/private-key',b'PRIVATE_KEY_CANARY')
        markers.append(os.fsencode(repo)+b'/'+directory+b'/.gitignore')
    original=p._git_command;commands=[]
    def checked(args,**kwargs):
        for marker in markers:os.utime(marker,ns=(1,os.stat(marker).st_mtime_ns))
        result=original(args,**kwargs)
        assert all(os.stat(marker).st_atime_ns==1 for marker in markers),args[0]
        commands.append(args[0]);return result
    monkeypatch.setattr(p,'_git_command',checked);ob=p.observe()
    assert {'config','rev-parse','for-each-ref','ls-files','status'}<=set(commands)
    for key in ('ignored','untracked'):
        raw=n._bytes(ob['git'][key])
        assert b'private-key' not in raw and b'PRIVATE' not in raw
    source={n.decode_path(row['path']):row for row in ob['source']}
    for directory in private:assert source[directory]['reason'] is not None


@pytest.mark.parametrize('unavailable_observation',[False,True,'current'])
def test_missing_store_after_observation_never_mutates_foreign_cwd(tmp_path,monkeypatch,unavailable_observation):
    root,repo,p=setup(tmp_path/'root');candidate=p.prepare(expected=None)
    outside=tmp_path/'outside';outside.mkdir();foreign=outside/'candidate.json';foreign.write_bytes(n._json(candidate));foreign.chmod(0o600)
    before=foreign.read_bytes()
    if unavailable_observation!='current':write(repo,b'homeassistant/a',b'changed')
    original=p.observe
    def lose_anchor():
        observation=original()
        (root/'data/native-migration-preparation').rename(root/'data/disconnected-preparation')
        if unavailable_observation is True:raise n.Blocked('fixture unavailable observation')
        return observation
    monkeypatch.setattr(p,'observe',lose_anchor);monkeypatch.chdir(outside)
    assert p.revalidate()=='unavailable'
    assert foreign.read_bytes()==before and sorted(path.name for path in outside.iterdir())==['candidate.json']
    assert json.loads((root/'data/disconnected-preparation/candidate.json').read_bytes())['state']=='prepared'


@pytest.mark.parametrize('descriptor',[None,False,True,-1,'0'])
def test_store_io_rejects_nonregistered_descriptors_before_os_access(tmp_path,monkeypatch,descriptor):
    root,repo,p=setup(tmp_path);candidate=p.prepare(expected=None)
    def forbidden(*args,**kwargs):raise AssertionError('ambient filesystem IO')
    monkeypatch.setattr(n.os,'stat',forbidden)
    with pytest.raises(n.Blocked):p._load(descriptor)
    with pytest.raises(n.Blocked):p._save(descriptor,candidate,candidate['digest'])


def test_unregistered_integer_directory_cannot_become_store_authority(tmp_path):
    root,repo,p=setup(tmp_path);candidate=p.prepare(expected=None)
    fd=os.open(root/'data',os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(n.Blocked):p._save(fd,candidate,candidate['digest'])
        with pytest.raises(n.Blocked):p._load(fd)
    finally:os.close(fd)


def test_real_completed_compensation_is_terminal_history(tmp_path):
    from native_services import ServiceStore,validate
    root,repo,p=setup(tmp_path)
    class Adapter:
        def __init__(self):self.states={'core':'running','app:ha_ops':'running'};self.unhealthy=True
        def inventory(self,*,timeout):
            return dict(complete=True,self='app:ha_ops',entries=[dict(id=target,startup='core' if target=='core' else 'application',state='running',manageable=True,capability='manageable') for target in self.states])
        def observe(self,target,*,timeout):return dict(target=target,state=self.states[target])
        def command(self,pending,*,timeout):
            self.states[pending['target']]='stopped' if pending['kind']=='stop' else 'running'
            return dict({key:pending[key] for key in ('operation','owner','attempt','target','kind')},outcome='accepted')
        def readiness(self,target,*,timeout):return dict(target=target,status='unhealthy' if self.unhealthy else 'healthy',recovery_mode=self.unhealthy)
    def fence(state):return state['operation'],state['owner'],state['revision']
    store=ServiceStore(root/'data/native-services');adapter=Adapter();state=store.prepare(adapter)
    state=store.stop(*fence(state),adapter);state=store.restore(*fence(state),adapter)
    assert state['phase']!='restored' and p.observe()['operations']['services']['summary']['pending_present']
    state=store.compensate(*fence(state),adapter)
    assert p.observe()['operations']['services']['summary']['pending_present']
    adapter.unhealthy=False;state=store.restore(*fence(state),adapter);validate(state)
    assert state['phase']=='restored' and state['compensation'] and state['pending'] is None
    ob=p.observe();summary=ob['operations']['services']['summary']
    assert not summary['active_present'] and not summary['pending_present'] and 'pending-ha-ops-operation' not in ob['blockers']
    assert p.prepare(expected=None)['observation']['operations']['services']['object'] is not None


@pytest.mark.parametrize('damage',['null-ref','duplicate-ref','duplicate-mount','file-root','file-data','file-legacy','zero-inode','inconsistent-descriptor','duplicate-index','duplicate-mapping','empty-mount','null-root','duplicate-row'])
def test_semantic_record_invariants_survive_recomputed_digests(tmp_path,damage):
    import base64
    root,repo,p=setup(tmp_path);candidate=p.prepare(expected=None);ob=candidate['observation']
    if damage in ('null-ref','duplicate-ref'):
        raw=n._bytes(ob['git']['refs'])
        if damage=='null-ref':
            fields=raw.split(b'\0');fields[1]=b'0'*40;raw=b'\0'.join(fields)
        else:raw+=raw
        ob['git']['refs']=base64.b64encode(raw).decode()
    elif damage=='duplicate-mount':ob['binding']['mounts'].append(copy.deepcopy(ob['binding']['mounts'][0]))
    elif damage.startswith('file-'):ob['binding'][damage[5:]+'_identity'][2]=stat.S_IFREG|0o700
    elif damage=='zero-inode':ob['binding']['root_identity'][1]=0
    elif damage=='inconsistent-descriptor':ob['source'][0]['object']['uid']+=1
    elif damage=='duplicate-mapping':ob['binding']['mappings'].append(copy.deepcopy(ob['binding']['mappings'][0]))
    elif damage=='empty-mount':ob['binding']['mounts'][0]['source']=''
    elif damage=='null-root':ob['binding']['root']=base64.b64encode(b'/fixture\0').decode()
    elif damage=='duplicate-row':ob['rows'].append(copy.deepcopy(ob['rows'][0]))
    else:ob['git']['index'].append(copy.deepcopy(ob['git']['index'][0]))
    candidate['input_digest']=n._digest(ob);candidate['digest']=n._digest({k:v for k,v in candidate.items() if k!='digest'})
    (root/'data/native-migration-preparation/candidate.json').write_bytes(n._json(candidate))
    with pytest.raises(n.Blocked):p.read()


@pytest.mark.parametrize('damage',['empty','noncovering','wrong-device','data-route','legacy-route','ambiguous-route'])
def test_persisted_topology_covers_each_bound_anchor(tmp_path,damage):
    import base64
    root,repo,p=setup(tmp_path);candidate=p.prepare(expected=None)
    binding=candidate['observation']['binding'];mounts=binding['mounts']
    absolute=os.fsencode(root)
    covering=[m for m in mounts if absolute==n._bytes(m['point']) or absolute.startswith(n._bytes(m['point']).rstrip(b'/')+b'/')]
    deepest=max(covering,key=lambda m:len(n._bytes(m['point'])))
    if damage=='empty':binding['mounts']=[]
    elif damage=='noncovering':
        for mount in mounts:mount['point']=base64.b64encode(b'/unrelated').decode()
    elif damage=='wrong-device':
        for mount in mounts:mount['device']=base64.b64encode(b'999:999').decode()
    else:
        extra=copy.deepcopy(deepest);extra['id']=max(m['id'] for m in mounts)+1
        if damage!='ambiguous-route':
            suffix=b'data' if damage=='data-route' else n.decode_path(binding['legacy'])
            extra['point']=base64.b64encode(absolute+b'/'+suffix).decode()
            extra['device']=base64.b64encode(b'999:999').decode()
        mounts.append(extra)
    candidate['input_digest']=n._digest(candidate['observation'])
    candidate['digest']=n._digest({k:v for k,v in candidate.items() if k!='digest'})
    (root/'data/native-migration-preparation/candidate.json').write_bytes(n._json(candidate))
    with pytest.raises(n.Blocked):p.read()

@pytest.mark.parametrize('boundary',[1,2,3])
def test_diagnostic_churn_never_reads_payload_or_changes_commitment(tmp_path,monkeypatch,boundary):
    root,repo,p=setup(tmp_path)
    db=root/'homeassistant/home-assistant_v2.db';db.write_bytes(b'diagnostic'*100000)
    original=p._read;seen=[]
    def read(fd,path,**kw):
        seen.append(path)
        assert path not in (b'homeassistant/home-assistant_v2.db',b'homeassistant/new-diagnostic/wal')
        return original(fd,path,**kw)
    monkeypatch.setattr(p,'_read',read)
    observe=p._observe_once;calls=0
    def changing():
        nonlocal calls
        calls+=1
        if calls==boundary:
            with db.open('ab') as stream:stream.write(b'churn')
            write(root,b'homeassistant/new-diagnostic/wal',b'live')
        return observe()
    monkeypatch.setattr(p,'_observe_once',changing)
    candidate=p.prepare(expected=None)
    diagnostic=[r for r in candidate['observation']['native'] if (r['reason'] or '').startswith('diagnostic-only')]
    assert diagnostic and all(row['object']['digest'] is None for row in diagnostic)
    with db.open('ab') as stream:stream.write(b'more')
    (root/'homeassistant/new-diagnostic/wal').unlink()
    assert p.revalidate()=='current'
    assert any(r['disposition']=='diagnostic-only' for r in candidate['observation']['rows'])


@pytest.mark.parametrize('change',['index-only','absent-create','parent-mode','native-content','native-mode'])
def test_real_participating_inputs_remain_strict(tmp_path,change):
    root,repo,p=setup(tmp_path)
    if change=='index-only':
        write(repo,b'homeassistant/index-only',b'index');git(repo,'add','homeassistant/index-only')
        (repo/'homeassistant/index-only').unlink();write(root,b'homeassistant/index-only',b'live')
    if change=='absent-create':
        write(repo,b'homeassistant/absent',b'index');git(repo,'add','homeassistant/absent')
    p.prepare(expected=None)
    if change=='index-only':write(root,b'homeassistant/index-only',b'changed')
    elif change=='absent-create':write(root,b'homeassistant/absent',b'appeared')
    elif change=='parent-mode':os.chmod(root/'homeassistant',0o750)
    elif change=='native-mode':os.chmod(root/'homeassistant/a',0o600)
    else:write(root,b'homeassistant/a',b'changed')
    assert p.revalidate()=='stale'


@pytest.mark.parametrize('operation',['create','replace','stale','failed-observation'])
def test_each_candidate_persistence_has_one_real_disk_snapshot(tmp_path,monkeypatch,operation):
    root,repo,p=setup(tmp_path)
    first=None if operation=='create' else p.prepare(expected=None)
    if operation=='stale':write(repo,b'homeassistant/a',b'changed')
    if operation=='failed-observation':monkeypatch.setattr(p,'observe',mock.Mock(side_effect=n.Blocked('injected')))
    with mock.patch.object(p.disk,'snapshot',wraps=p.disk.snapshot) as snapshot:
        if operation in ('create','replace'):p.prepare(expected=None if first is None else first['digest'])
        else:assert p.revalidate()==('stale' if operation=='stale' else 'unavailable')
        assert snapshot.call_count==1
    assert not (root/'data/native').exists()


@pytest.mark.parametrize('unavailable',[False,True])
def test_capacity_denial_cannot_claim_durable_stale(tmp_path,monkeypatch,unavailable):
    root,repo,p=setup(tmp_path);p.prepare(expected=None)
    path=root/'data/native-migration-preparation/candidate.json';before=path.read_bytes()
    if unavailable:monkeypatch.setattr(p,'observe',mock.Mock(side_effect=n.Blocked('injected')))
    else:write(repo,b'homeassistant/a',b'changed')
    original=p.disk._volume
    def full(*args,**kwargs):return dataclasses.replace(original(*args,**kwargs),available=0,inodes=0)
    monkeypatch.setattr(p.disk,'_volume',full)
    assert p.revalidate()=='unavailable' and path.read_bytes()==before
    assert not list(path.parent.glob('.pending-*'))


def test_unqualified_default_disk_refuses_before_candidate_mkdir(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path)
    original=p.disk._volume
    monkeypatch.setattr(p.disk,'_volume',lambda *a,**kw:dataclasses.replace(original(*a,**kw),supported=False))
    with pytest.raises(n.Blocked):p.prepare(expected=None)
    assert not (root/'data/native-migration-preparation').exists()
    assert not (root/'data/.native-disk-admission.lock').exists()


def test_disappearing_diagnostic_entry_is_best_effort(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);write(root,b'homeassistant/diagnostic',b'live')
    original=n.os.stat
    def missing(path,*args,**kwargs):
        if path==b'diagnostic':raise FileNotFoundError('diagnostic churn')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(n.os,'stat',missing)
    assert p.prepare(expected=None)['state']=='prepared'


def migration_recipe(p):
    observation=p.observe()
    value=dict(schema=n.SCHEMA,generation='a'*32,state='prepared',observation=observation,
               input_digest=n._digest(n._stability(observation)),digest='0'*64)
    value['digest']=n._digest({k:v for k,v in value.items() if k!='digest'})
    payload=n._json(value);name=b'.pending-'+b'b'*32
    controls=tuple(disk_module.Control(p.store+b'/'+target,payload,value['generation'],value['digest']) for target in (b'candidate.json',name))
    return value,payload,name,disk_module.Evidence(value['generation'],value['digest'],controls=controls)


@pytest.mark.parametrize('first',['migration','status'])
@pytest.mark.parametrize('resource',['bytes','inodes'])
@pytest.mark.parametrize('fits',[False,True])
def test_disjoint_preissued_migration_and_status_current_capacity(tmp_path,monkeypatch,first,resource,fits):
    root,repo,p=setup(tmp_path);(root/'data/native').mkdir()
    lock=root/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    status_disk=disk_module.NativeDisk(root);capacity={'bytes':10**9,'inodes':10**6};checks=[]
    for disk in (p.disk,status_disk):
        original=disk._volume
        def volume(path,fd,mounts,original=original):
            checks.append(path)
            return dataclasses.replace(original(path,fd,mounts),available=capacity['bytes'],inodes=capacity['inodes'])
        monkeypatch.setattr(disk,'_volume',volume)
    value,payload,name,evidence=migration_recipe(p)
    status_payload=b'{"operation_id":"status","owner_id":"owner"}'
    status_evidence=disk_module.Evidence('status','owner',controls=tuple(disk_module.Control(b'data/native/storage-status.json',status_payload,'status','owner') for _ in range(2)))
    with mock.patch.object(p.disk,'snapshot',wraps=p.disk.snapshot) as mscan,mock.patch.object(status_disk,'snapshot',wraps=status_disk.snapshot) as sscan:
        migration_plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
        status_plan=status_disk.project('storage_control',status_disk.snapshot(),status_evidence)
        assert mscan.call_count==sscan.call_count==1
        assert all(d.status=='fits' for d in migration_plan.decisions+status_plan.decisions)
        def execute(which):
            disk,plan=(p.disk,migration_plan) if which=='migration' else (status_disk,status_plan)
            before=len(checks)
            with disk.guard(plan) as admission:
                at_entry=len(checks)
                assert at_entry-before==len(plan.decisions)
                if which=='migration':
                    with p._disk(create=True,admission=admission) as fd:p._save(fd,value,None,admission=admission,payload=payload,name=name)
                else:
                    target=root/'data/native/storage-status.json';target.write_bytes(status_payload);target.chmod(0o600)
                # Own executor receipts never re-charge the original full peak.
                if which=='migration':assert len(checks)==at_entry
        execute(first)
        second='status' if first=='migration' else 'migration'
        delayed=status_plan if second=='status' else migration_plan
        decision=delayed.decisions[0]
        capacity[resource]=(decision.additional_peak+decision.reserve if resource=='bytes' else decision.required_inodes)+(1 if fits else -1)
        if fits:execute(second)
        else:
            with pytest.raises(disk_module.InsufficientSpace):execute(second)
            assert not (root/('data/native/storage-status.json' if second=='status' else 'data/native-migration-preparation')).exists()
        assert mscan.call_count==sscan.call_count==1


def test_only_data_bootstrap_boundary_credits_only_realized_shared_lock(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path)
    value,payload,name,evidence=migration_recipe(p)
    original=p.disk._volume;capacity={'base':10**9,'credit':0};calls=[]
    def volume(path,fd,mounts):
        calls.append(path)
        spent=capacity['credit'] if (root/'data/.native-disk-admission.lock').exists() else 0
        return dataclasses.replace(original(path,fd,mounts),available=capacity['base']-spent,inodes=100000)
    monkeypatch.setattr(p.disk,'_volume',volume)
    with mock.patch.object(p.disk,'snapshot',wraps=p.disk.snapshot) as snapshot:
        plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
        decision=plan.decisions[0]
        capacity['base']=decision.additional_peak+decision.reserve
        capacity['credit']=sum(a.allocated for a in plan.allocations if a.output=='admission-lock-infrastructure')
        assert capacity['credit']>0
        before=len(calls)
        with p.disk.guard(plan) as admission:
            at_entry=len(calls);assert at_entry-before==2
            # Further own allocation cannot trigger a full-peak capacity charge.
            capacity['base']=0
            with p._disk(create=True,admission=admission) as fd:p._save(fd,value,None,admission=admission,payload=payload,name=name)
            assert len(calls)==at_entry
        assert snapshot.call_count==1
    assert not (root/'data/native').exists() and p.read()==value


@pytest.mark.parametrize('change',['key','quantum','unsupported'])
def test_capacity_witness_profile_change_refuses_before_candidate_effect(tmp_path,monkeypatch,change):
    root,repo,p=setup(tmp_path);value,payload,name,evidence=migration_recipe(p)
    plan=p.disk.project('storage_control',p.disk.snapshot(),evidence);original=p.disk._volume
    def changed(*a,**kw):
        current=original(*a,**kw)
        return dataclasses.replace(current,**({'key':('foreign',)} if change=='key' else {'quantum':current.quantum*2} if change=='quantum' else {'supported':False}))
    monkeypatch.setattr(p.disk,'_volume',changed)
    with pytest.raises(disk_module.Blocked):
        with p.disk.guard(plan):raise AssertionError('candidate write admitted')
    assert not (root/'data/native-migration-preparation').exists()
    assert not (root/'data/.native-disk-admission.lock').exists()


@pytest.mark.skipif(sys.platform!='linux',reason='real Linux streaming diagnostic-churn proof')
def test_linux_streaming_recorder_and_diagnostic_directory_churn(tmp_path,monkeypatch):
    import threading
    root,repo,p=setup(tmp_path);db=root/'homeassistant/home-assistant_v2.db'
    db.write_bytes(b'initial'*200000)
    read=p._read
    def no_diagnostic_payload(fd,path,**kwargs):
        assert path!=b'homeassistant/home-assistant_v2.db'
        return read(fd,path,**kwargs)
    monkeypatch.setattr(p,'_read',no_diagnostic_payload)
    stop=threading.Event();started=threading.Event();writes=[]
    def recorder():
        while not stop.is_set():
            with db.open('ab') as stream:stream.write(b'wal-churn'*100)
            directory=root/'homeassistant/diagnostic-churn'
            try:directory.mkdir();directory.rmdir()
            except FileNotFoundError:pass
            writes.append(1);started.set();stop.wait(.001)
    worker=threading.Thread(target=recorder);worker.start();assert started.wait(5)
    try:
        p.prepare(expected=None);assert p.revalidate()=='current'
    finally:stop.set();worker.join(5)
    assert not worker.is_alive() and writes


@pytest.mark.parametrize('resource',['bytes','inodes'])
def test_preissued_cross_process_status_plan_refuses_after_migration(tmp_path,monkeypatch,resource):
    import multiprocessing
    root,repo,p=setup(tmp_path);(root/'data/native').mkdir()
    lock=root/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    ctx=multiprocessing.get_context('fork');capacity=ctx.Value('q',10**9)
    status_disk=disk_module.NativeDisk(root)
    for participant in (p.disk,status_disk):
        original=participant._volume
        def volume(path,fd,mounts,original=original):
            return dataclasses.replace(original(path,fd,mounts),available=capacity.value if resource=='bytes' else 10**9,
                                       inodes=capacity.value if resource=='inodes' else 10**6)
        monkeypatch.setattr(participant,'_volume',volume)
    value,payload,name,evidence=migration_recipe(p)
    migration_plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
    status_payload=b'{"operation_id":"status","owner_id":"owner"}'
    status_evidence=disk_module.Evidence('status','owner',controls=tuple(disk_module.Control(b'data/native/storage-status.json',status_payload,'status','owner') for _ in range(2)))
    delayed=status_disk.project('storage_control',status_disk.snapshot(),status_evidence)
    go=ctx.Event();result=ctx.Queue()
    def status_worker():
        if not go.wait(10):result.put('timeout');return
        try:
            with status_disk.guard(delayed):result.put('unsafe-admitted')
        except disk_module.InsufficientSpace:result.put('refused-current-capacity')
        except BaseException as exc:result.put(type(exc).__name__)
    child=ctx.Process(target=status_worker);child.start()
    try:
        with p.disk.guard(migration_plan) as admission:
            with p._disk(create=True,admission=admission) as fd:p._save(fd,value,None,admission=admission,payload=payload,name=name)
        decision=delayed.decisions[0]
        capacity.value=(decision.additional_peak+decision.reserve if resource=='bytes' else decision.required_inodes)-1
        go.set();assert result.get(timeout=10)=='refused-current-capacity'
        child.join(5);assert child.exitcode==0
    finally:
        if child.is_alive():child.terminate();child.join(5)
    assert not (root/'data/native/storage-status.json').exists()


def test_final_input_change_refuses_before_candidate_directory_creation(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);original=p._observe_once;calls=0
    def changing():
        nonlocal calls
        calls+=1
        if calls==3:write(repo,b'homeassistant/a',b'changed after initial observation')
        return original()
    monkeypatch.setattr(p,'_observe_once',changing)
    with pytest.raises(n.Blocked,match='inputs-changed-before-persistence'):p.prepare(expected=None)
    assert not (root/'data/native-migration-preparation').exists()


def test_queued_diagnostic_directory_disappearance_has_no_stability_authority(tmp_path,monkeypatch):
    root,repo,p=setup(tmp_path);directory=root/'homeassistant/queued-diagnostic';directory.mkdir()
    original=p._directory;removed=[]
    def disappeared(fd,path):
        if path==b'homeassistant/queued-diagnostic':
            directory.rmdir();removed.append(path);raise FileNotFoundError('queued diagnostic disappeared')
        return original(fd,path)
    monkeypatch.setattr(p,'_directory',disappeared)
    assert p.prepare(expected=None)['state']=='prepared' and removed
    assert p.revalidate()=='current'


@pytest.mark.parametrize('target',[b'homeassistant/nested',b'addon_configs/slug'])
def test_required_parent_projection_is_json_invariant_and_preserves_source_metadata(tmp_path,target):
    root,repo,p=setup(tmp_path,mappings=((b'homeassistant',target),))
    write(root,target+b'/a',b'live')
    observation=p.observe()
    assert any(row['disposition']=='required-parent' for row in observation['rows'])
    assert n._stability(observation)==n._stability(json.loads(n._json(observation)))
    candidate=p.prepare(expected=None)
    assert p.read()==candidate and p.revalidate()=='current'
    parent=target.split(b'/')[0]
    write(root,parent+b'/diagnostic-sibling',b'churn')
    assert p.revalidate()=='current'
    os.chmod(os.fsdecode(os.fsencode(root)+b'/'+parent),0o750)
    assert p.revalidate()=='stale'
    candidate=p.prepare(expected=p.read()['digest'])
    os.chmod(repo/'homeassistant',0o750)
    assert p.revalidate()=='stale'


@pytest.mark.parametrize('disposition',['excluded','excluded-index','retired','unresolved'])
@pytest.mark.parametrize('boundary',[1,2,3,'revalidate'])
def test_all_nonparticipating_derived_rows_exclude_live_diagnostics(tmp_path,monkeypatch,disposition,boundary):
    root,repo,p=setup(tmp_path)
    source=b'homeassistant/secrets.yaml' if disposition.startswith('excluded') else b'.storage_managed/core.config_entries.json' if disposition=='retired' else b'unmapped/path'
    destination=b'homeassistant/secrets.yaml' if disposition.startswith('excluded') else b'homeassistant/diagnostic-retired' if disposition=='retired' else b'homeassistant/diagnostic-unresolved'
    write(repo,source,b'source unchanged');write(root,destination,b'live before')
    if disposition=='excluded-index':
        git(repo,'add','-f',os.fsdecode(source));os.unlink(os.fsencode(repo)+b'/'+source)
    original=p._observe_once;calls=0
    def changing():
        nonlocal calls
        calls+=1
        if calls==boundary:write(root,destination,b'live changed with new metadata and size')
        return original()
    monkeypatch.setattr(p,'_observe_once',changing)
    candidate=p.prepare(expected=None)
    assert n._stability(candidate['observation'])==n._stability(json.loads(n._json(candidate['observation'])))
    write(root,destination,b'further live diagnostic churn')
    assert n._stability(candidate['observation'])==n._stability(p.observe())
    assert p.revalidate()=='current'
    # The actual source descriptors/index remain strict, including exclusion.
    if disposition!='excluded-index':os.chmod(os.fsdecode(os.fsencode(repo)+b'/'+source),0o600)
    else:git(repo,'update-index','--force-remove',os.fsdecode(source))
    assert p.revalidate()=='stale'


@pytest.mark.parametrize('resource',['bytes','inodes'])
def test_shared_lock_appearing_after_snapshot_retires_only_verified_infrastructure(tmp_path,monkeypatch,resource):
    root,repo,p=setup(tmp_path);value,payload,name,evidence=migration_recipe(p)
    plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
    decision=plan.decisions[0]
    allocation=next(a for a in plan.allocations if a.output=='admission-lock-infrastructure')
    lock=root/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    original=p.disk._volume;calls=[]
    def fitting(*args,**kwargs):
        calls.append(args[0])
        return dataclasses.replace(original(*args,**kwargs),
            available=decision.additional_peak+decision.reserve-(allocation.allocated if resource=='bytes' else 0),
            inodes=decision.required_inodes-(allocation.inodes if resource=='inodes' else 0))
    monkeypatch.setattr(p.disk,'_volume',fitting)
    original_open=os.open;creations=[]
    def no_recreation(path,flags,*args,**kwargs):
        if os.fsencode(path)==b'.native-disk-admission.lock' and flags & os.O_CREAT:creations.append(path)
        return original_open(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',no_recreation)
    with p.disk.guard(plan) as admission:
        with p._disk(create=True,admission=admission) as fd:p._save(fd,value,None,admission=admission,payload=payload,name=name)
    assert p.read()==value and len(calls)==1 and not creations


@pytest.mark.parametrize('resource',['bytes','inodes'])
def test_shared_lock_appearance_during_bootstrap_shortage_is_qualified(tmp_path,monkeypatch,resource):
    root,repo,p=setup(tmp_path);_,_,_,evidence=migration_recipe(p)
    plan=p.disk.project('storage_control',p.disk.snapshot(),evidence);decision=plan.decisions[0]
    allocation=next(a for a in plan.allocations if a.output=='admission-lock-infrastructure')
    original=p.disk._volume;lock=root/'data/.native-disk-admission.lock';calls=[]
    def appeared(*args,**kwargs):
        if not lock.exists():lock.write_bytes(b'');lock.chmod(0o600)
        calls.append(args[0])
        return dataclasses.replace(original(*args,**kwargs),available=decision.additional_peak+decision.reserve-(allocation.allocated if resource=='bytes' else 0),
                                   inodes=decision.required_inodes-(allocation.inodes if resource=='inodes' else 0))
    monkeypatch.setattr(p.disk,'_volume',appeared)
    with p.disk.guard(plan):pass
    assert len(calls)==2 and not (root/'data/native-migration-preparation').exists()


@pytest.mark.parametrize('unsafe',['mode','symlink','hardlink'])
def test_appeared_shared_lock_does_not_accept_foreign_or_linked_authority(tmp_path,unsafe):
    root,repo,p=setup(tmp_path);_,_,_,evidence=migration_recipe(p)
    plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
    lock=root/'data/.native-disk-admission.lock'
    if unsafe=='symlink':lock.symlink_to('repo/homeassistant/a')
    else:
        lock.write_bytes(b'');lock.chmod(0o644 if unsafe=='mode' else 0o600)
        if unsafe=='hardlink':os.link(lock,root/'data/foreign-lock-link')
    with pytest.raises((OSError,disk_module.Blocked)):
        with p.disk.guard(plan):raise AssertionError('unsafe authority admitted')
    assert not (root/'data/native-migration-preparation').exists()


def test_required_parent_replacement_retains_strict_identity(tmp_path):
    root,repo,p=setup(tmp_path,mappings=((b'homeassistant',b'addon_configs/slug'),))
    write(root,b'addon_configs/slug/a',b'live');p.prepare(expected=None)
    (root/'addon_configs').rename(root/'retained-addon-configs')
    write(root,b'addon_configs/slug/a',b'live')
    assert p.revalidate()=='stale'


@pytest.mark.parametrize('loss',['before-guard','during-open','after-open'])
def test_snapshot_known_shared_mutex_loss_never_recreates_infrastructure(tmp_path,monkeypatch,loss):
    root,repo,p=setup(tmp_path);lock=root/'data/.native-disk-admission.lock'
    lock.write_bytes(b'');lock.chmod(0o600)
    _,_,_,evidence=migration_recipe(p)
    plan=p.disk.project('storage_control',p.disk.snapshot(),evidence)
    assert not any(a.output=='admission-lock-infrastructure' for a in plan.allocations)
    original_open=os.open;effects=[];removed=[]
    with lock.open('rb') as held:
        if loss=='before-guard':lock.unlink()
        def race(path,flags,*args,**kwargs):
            if os.fsencode(path)==b'.native-disk-admission.lock':
                if flags & os.O_CREAT:effects.append('created-lock')
                elif flags & os.O_RDWR and loss in ('during-open','after-open') and not removed:
                    removed.append(True)
                    if loss=='after-open':
                        result=original_open(path,flags,*args,**kwargs);lock.unlink();return result
                    lock.unlink()
            return original_open(path,flags,*args,**kwargs)
        monkeypatch.setattr(os,'open',race)
        with pytest.raises((disk_module.Blocked,OSError)):
            with p.disk.guard(plan):raise AssertionError('lost mutex authority admitted')
        assert not effects and not lock.exists()
    assert not (root/'data/native-migration-preparation').exists()
