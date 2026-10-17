"""Inactive recovery primitive: independent bytes/stat and real crash evidence."""
import dataclasses
import errno
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
import native_recovery as n
import native_disk as d
from native_inventory import Mount, Policy, linux_mounts
from native_metadata import Binding


def binding(root):
    root=os.fsencode(Path(root).resolve()); s=os.stat(root)
    mounts=linux_mounts() if sys.platform=='linux' else (Mount(1,0,f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}'.encode(),b'/',b'/',b'rw',b'fixture',b'fixture',b'rw'),)
    return Binding(root,Policy(),mounts,tuple(m.id for m in mounts),True)


def write(root,path,data=b'old',mode=0o640):
    p=root/os.fsdecode(path); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(data); p.chmod(mode); return p


def file(data=b'new',mode=0o600,uid=None,gid=None):
    return n.File(data,os.geteuid() if uid is None else uid,os.getegid() if gid is None else gid,mode)


def store(root): return n.RecoveryStore(binding(root))


def frozen(disk):
    stack=mock.ExitStack() if hasattr(mock,'ExitStack') else __import__('contextlib').ExitStack()
    for name in ('snapshot','project','_scan','_destination','_volume','_profile','_entries','_objects'):
        stack.enter_context(mock.patch.object(disk,name,side_effect=AssertionError('phase rescan '+name)))
    stack.enter_context(mock.patch.object(d.os,'fstatvfs',side_effect=AssertionError('phase capacity rescan')))
    return stack


def point(root,targets):
    (root/'data/native').mkdir(parents=True,exist_ok=True)
    (root/'data/native-operations').mkdir(parents=True,exist_ok=True)
    s=store(root); p=s.prepare(targets); installed=s.install(p); return s,installed


@pytest.mark.skipif(sys.platform!='linux',reason='actual supported allocation profile requires Linux')
def test_exact_repeated_rollback_absence_and_parents(tmp_path):
    a=write(tmp_path,b'homeassistant/a',b'original',0o6751)
    sibling=write(tmp_path,b'homeassistant/sibling',b'unselected')
    deleted=write(tmp_path,b'homeassistant/deleted',b'deleted',0o1750)
    expected={p:(p.read_bytes(),p.stat().st_uid,p.stat().st_gid,stat.S_IMODE(p.stat().st_mode)) for p in (a,deleted,sibling)}
    s,installed=point(tmp_path,{b'homeassistant/a':file(),b'homeassistant/deleted':None,b'homeassistant/new/deep/b':file(b'added')})
    forecast=installed['manifest']['forecast']; generation=installed['pointer']['generation']
    with frozen(s.disk): s.execute()
    sibling.write_bytes(b'external untouched')
    fresh=store(tmp_path)
    with frozen(fresh.disk):
        discovered=fresh.discover(); assert discovered['manifest']['forecast']==forecast
        assert a.read_bytes()==b'new'
        fresh.rollback(); first=fresh.discover(); fresh.rollback(); second=fresh.discover()
    assert first==second
    assert second['pointer']['generation']==generation
    for p in (a,deleted): assert (p.read_bytes(),p.stat().st_uid,p.stat().st_gid,stat.S_IMODE(p.stat().st_mode))==expected[p]
    assert sibling.read_bytes()==b'external untouched' and not (tmp_path/'homeassistant/new').exists()


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('transition',['file-directory','directory-file'])
def test_both_structural_transitions(tmp_path,transition):
    if transition=='file-directory':
        write(tmp_path,b'homeassistant/x',b'old file')
        targets={b'homeassistant/x':n.Directory(os.getuid(),os.getgid(),0o751),b'homeassistant/x/y':file()}
    else:
        write(tmp_path,b'homeassistant/x/y',b'old child')
        targets={b'homeassistant/x':file(),b'homeassistant/x/y':None}
    s,_=point(tmp_path,targets); s.execute(); s.rollback(); s.rollback()
    if transition=='file-directory': assert (tmp_path/'homeassistant/x').read_bytes()==b'old file'
    else: assert (tmp_path/'homeassistant/x/y').read_bytes()==b'old child'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_unselected_child_refused_before_point(tmp_path):
    write(tmp_path,b'homeassistant/x/y')
    with pytest.raises(n.Blocked,match='unselected'): store(tmp_path).prepare({b'homeassistant/x':file()})
    assert (tmp_path/'homeassistant/x/y').read_bytes()==b'old'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('change',['bytes','mode','inode','symlink','parent'])
def test_foreign_write_conflicts_without_overwrite(tmp_path,change):
    p=write(tmp_path,b'homeassistant/x/a')
    s,_=point(tmp_path,{b'homeassistant/x/a':file()}); s.execute()
    if change=='bytes': p.write_bytes(b'foreign')
    elif change=='mode': p.chmod(0o777)
    elif change=='inode':
        replacement=p.with_name('foreign'); replacement.write_bytes(p.read_bytes()); replacement.chmod(p.stat().st_mode); os.replace(replacement,p)
    elif change=='symlink': p.unlink(); p.symlink_to('foreign')
    else: p.parent.chmod(0o700)
    before=p.lstat()
    with pytest.raises((n.Blocked,OSError)): s.rollback()
    assert n.RecoveryStore._stable(p.lstat())==n.RecoveryStore._stable(before)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_external_child_preserved_and_repeat_after_removal(tmp_path):
    (tmp_path/'homeassistant').mkdir()
    s,_=point(tmp_path,{b'homeassistant/new/a':file()}); s.execute()
    foreign=write(tmp_path,b'homeassistant/new/foreign',b'foreign')
    with pytest.raises(OSError): s.rollback()
    assert foreign.read_bytes()==b'foreign'
    foreign.unlink(); s.rollback(); s.rollback(); assert not (tmp_path/'homeassistant/new').exists()


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('boundary',['blob_durable','stage_durable','generation_durable','control_written','control_fsynced','control_replaced','control_durable','slot_durable'])
def test_failed_point_replacement_keeps_valid_generation(tmp_path,boundary):
    write(tmp_path,b'homeassistant/a',b'first')
    s,first=point(tmp_path,{b'homeassistant/a':file()})
    p=s.prepare({b'homeassistant/a':file(b'second')})
    called=[]
    def fault(stage):
        if stage==boundary and not called: called.append(stage); raise OSError(errno.EIO,'injected')
    s.fault=fault
    with pytest.raises(OSError): s.install(p)
    fresh=store(tmp_path).discover()
    assert fresh['manifest']['before'][n._key(b'homeassistant/a')]['digest']==hashlib.sha256(b'first').hexdigest()
    assert (tmp_path/'homeassistant/a').read_bytes()==b'first'
    assert fresh['pointer']['generation'] in (first['pointer']['generation'],n.parse(p.manifest)['generation'])


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('boundary',['before_intent:replace','intent_durable:replace','effect:replace','effect_durable:replace','ack_durable:replace'])
@pytest.mark.parametrize('direction',['forward','rollback'])
def test_separate_process_crash_discovery_and_explicit_recovery(tmp_path,boundary,direction):
    p=write(tmp_path,b'homeassistant/a',b'old')
    s,_=point(tmp_path,{b'homeassistant/a':file()})
    if direction=='rollback': s.execute()
    script='''import os,sys\nsys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])\nfrom test_native_recovery import store\ns=store(__import__('pathlib').Path(sys.argv[3]))\ns.fault=lambda stage: os._exit(73) if stage==sys.argv[4] else None\ngetattr(s,sys.argv[5])()\n'''
    proc=subprocess.run([sys.executable,'-c',script,str(Path(n.__file__).parent),str(Path(__file__).parent),str(tmp_path),boundary,'execute' if direction=='forward' else 'rollback'],capture_output=True,timeout=20)
    assert proc.returncode==73,proc.stderr
    before=p.read_bytes(); fresh=store(tmp_path)
    # Reopening/discovering never starts recovery or writes a control file.
    found=fresh.discover(); journal=canonical_journal(fresh,found)
    assert p.read_bytes()==before and canonical_journal(fresh,fresh.discover())==journal
    with frozen(fresh.disk): fresh.rollback(); fresh.rollback()
    assert p.read_bytes()==b'old'


def canonical_journal(s,found): return s._read(found['pointer']['generation'].encode()+b'/journal.json')


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='root Linux numeric ownership')
def test_full_numeric_metadata_and_parent_chown_crashes(tmp_path):
    p=write(tmp_path,b'homeassistant/parent/a',b'old',0o6751)
    os.chown(p,12345,23456); p.chmod(0o6751)
    parent=p.parent; os.chown(parent,12346,23457); parent.chmod(0o3771)
    s,_=point(tmp_path,{b'homeassistant/parent':n.Directory(12347,23458,0o2750),b'homeassistant/parent/a':file(b'new',0o4750,12348,23459)})
    hit=[]
    def fault(stage):
        if stage=='effect:chown' and not hit: hit.append(stage); raise OSError(errno.EIO,'after chown')
    s.fault=fault
    with pytest.raises(OSError): s.execute()
    s=store(tmp_path); s.rollback(); s.rollback()
    assert (p.read_bytes(),p.stat().st_uid,p.stat().st_gid,stat.S_IMODE(p.stat().st_mode))==(b'old',12345,23456,0o6751)
    assert (parent.stat().st_uid,parent.stat().st_gid,stat.S_IMODE(parent.stat().st_mode))==(12346,23457,0o3771)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('corruption',['duplicate','unknown','truncated','blob','bool','owner','phase'])
def test_corrupt_persistent_state_refuses_before_effect(tmp_path,corruption):
    p=write(tmp_path,b'homeassistant/a')
    s,found=point(tmp_path,{b'homeassistant/a':file()}); gen=found['pointer']['generation'].encode()
    j=found['journal']; name=gen+b'/journal.json'
    if corruption=='duplicate': raw=b'{"schema":1,"schema":1}'
    elif corruption=='unknown': raw=n.canonical(dict(j,unknown=1))
    elif corruption=='truncated': raw=b'{'
    elif corruption=='blob':
        state=found['manifest']['before'][n._key(b'homeassistant/a')]
        s._write(gen+b'/blob-'+state['digest'].encode(),b'corrupt'); raw=n.canonical(j)
    else:
        j['revision' if corruption=='bool' else corruption]=True if corruption=='bool' else 'wrong'; raw=n.canonical(j)
    s._write(name,raw)
    with pytest.raises((n.Blocked,ValueError)): store(tmp_path).execute()
    assert p.read_bytes()==b'old'


def test_inert_constructor_and_issuer_boundary(tmp_path,monkeypatch):
    b=binding(tmp_path)
    with mock.patch.object(os,'open',side_effect=AssertionError('constructor IO')): s=n.RecoveryStore(b)
    p=s.prepare({b'homeassistant/a':file()})
    with pytest.raises(n.Blocked): dataclasses.replace(p).check()
    with pytest.raises(d.Blocked): d.recovery_project(d.NativeDisk(tmp_path),p)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_successful_replacement_reclaims_old_exact_generation(tmp_path):
    write(tmp_path,b'homeassistant/a')
    s,first=point(tmp_path,{b'homeassistant/a':file()})
    old=tmp_path/'data/native/recovery'/first['pointer']['generation']
    second=s.install(s.prepare({b'homeassistant/a':file(b'second')}))
    assert not old.exists() and second['cleanup'] and all(r['status']=='removed' for r in second['cleanup'])
    generations=[p for p in (tmp_path/'data/native/recovery').iterdir() if p.is_dir()]
    assert len(generations)==1 and s.cleanup()==[]


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
@pytest.mark.parametrize('corruption',['foreign-current','pending-after','ready-changed','missing-receipt','forecast'])
def test_shape_valid_persistent_corruption_cannot_create_authority(tmp_path,corruption):
    p=write(tmp_path,b'homeassistant/a')
    s,found=point(tmp_path,{b'homeassistant/a':file()}); m=found['manifest']; j=found['journal']; gen=m['generation'].encode(); key=n._key(b'homeassistant/a')
    if corruption=='foreign-current': j['current'][key]=dict(j['current'][key],ino=j['current'][key]['ino']+100)
    elif corruption=='ready-changed': j['changed']=[key]
    elif corruption=='missing-receipt': j['phase']='forward'; j['current'][key]=m['stages']['forward:'+key]['state']
    elif corruption=='pending-after':
        j['phase']='forward'; j['pending']=dict(path=key,action='replace',direction='forward',before=j['current'][key],after=[dict(j['current'][key],mode=0o777)],stage='forward:'+key)
    else:
        m['forecast']['recipe']='f'*64; raw=n.canonical(m); s._write(gen+b'/manifest.json',raw)
        pointer=dict(found['pointer'],manifest=hashlib.sha256(raw).hexdigest()); s._write(b'active.json',n.canonical(pointer))
    s._write(gen+b'/journal.json',n.canonical(j))
    with pytest.raises((n.Blocked,d.Blocked)): store(tmp_path).rollback()
    assert p.read_bytes()==b'old'


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='root Linux syscall crash matrix')
@pytest.mark.parametrize('action',['remove','chown','chmod','directory-replace'])
@pytest.mark.parametrize('direction',['forward','rollback'])
@pytest.mark.parametrize('edge',['before_intent','intent_durable','effect','effect_durable','ack_durable'])
def test_all_structural_and_parent_metadata_crash_boundaries(tmp_path,action,direction,edge):
    write(tmp_path,b'homeassistant/parent/a')
    if action=='remove':
        if direction=='forward': targets={b'homeassistant/parent/a':None}
        else: targets={b'homeassistant/new':file()}
    elif action=='directory-replace':
        if direction=='forward': targets={b'homeassistant/new/a':file()}
        else: targets={b'homeassistant/parent':file(),b'homeassistant/parent/a':None}
    else: targets={b'homeassistant/parent':n.Directory(12345,23456,0o2750)}
    s,_=point(tmp_path,targets)
    if direction=='rollback': s.execute()
    boundary=edge+':'+('replace' if action=='directory-replace' else action)
    script='''import os,sys\nsys.path[:0]=sys.argv[1:3]\nfrom test_native_recovery import store\ns=store(__import__('pathlib').Path(sys.argv[3]))\ns.fault=lambda stage: os._exit(74) if stage==sys.argv[4] else None\ngetattr(s,sys.argv[5])()\n'''
    child=subprocess.run([sys.executable,'-c',script,str(Path(n.__file__).parent),str(Path(__file__).parent),str(tmp_path),boundary,'execute' if direction=='forward' else 'rollback'],capture_output=True,timeout=20)
    assert child.returncode==74,child.stderr
    fresh=store(tmp_path)
    with frozen(fresh.disk): fresh.discover(); fresh.rollback(); first=fresh.discover(); fresh.rollback(); assert first==fresh.discover()
    assert (tmp_path/'homeassistant/parent/a').read_bytes()==b'old'
    assert not (tmp_path/'homeassistant/new').exists()
    parent=tmp_path/'homeassistant/parent'
    assert (parent.stat().st_uid,parent.stat().st_gid,stat.S_IMODE(parent.stat().st_mode))==(os.getuid(),os.getgid(),0o755)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_one_initial_forecast_and_no_resume_resampling(tmp_path,monkeypatch):
    write(tmp_path,b'homeassistant/a'); (tmp_path/'data/native').mkdir(parents=True); (tmp_path/'data/native-operations').mkdir(parents=True)
    s=store(tmp_path); counts={'snapshot':0,'project':0}; snapshot=s.disk.snapshot; project=n.recovery_project
    def snap(): counts['snapshot']+=1; return snapshot()
    def compile(*args): counts['project']+=1; return project(*args)
    monkeypatch.setattr(s.disk,'snapshot',snap); monkeypatch.setattr(n,'recovery_project',compile)
    s.install(s.prepare({b'homeassistant/a':file()})); assert counts=={'snapshot':1,'project':1}
    assert next(row for row in s.discover()['manifest']['forecast']['allocations'] if row[1]=='recovery-authority-key')[2]==32
    with frozen(s.disk): s.execute(); s.reconcile(); s.rollback(); s.rollback()
    fresh=store(tmp_path)
    with frozen(fresh.disk): fresh.discover(); fresh.rollback()
    assert counts=={'snapshot':1,'project':1}


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_store_route_substitution_after_durable_intent_refuses_target(tmp_path):
    p=write(tmp_path,b'homeassistant/a'); s,_=point(tmp_path,{b'homeassistant/a':file()}); hit=[]
    def fault(stage):
        if stage=='intent_durable:replace' and not hit:
            hit.append(stage); os.rename(tmp_path/'data',tmp_path/'original-data'); (tmp_path/'data').mkdir()
    s.fault=fault
    with pytest.raises(n.Blocked): s.execute()
    assert hit and p.read_bytes()==b'old'
    (tmp_path/'data').rmdir(); os.rename(tmp_path/'original-data',tmp_path/'data'); store(tmp_path).rollback()


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_streamed_preimage_memory_does_not_scale_with_content(tmp_path):
    import tracemalloc
    peaks=[]
    for size in (MIB:=1024**2,16*MIB):
        root=tmp_path/str(size); root.mkdir(); target=write(root,b'homeassistant/a',b'A')
        with target.open('wb') as stream:
            for _ in range(size//65536): stream.write(b'A'*65536)
        (root/'data/native').mkdir(parents=True); (root/'data/native-operations').mkdir(parents=True)
        s=store(root); replacement=file(b'B')
        tracemalloc.start(); prepared=s.prepare({b'homeassistant/a':replacement}); s.install(prepared); _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop(); peaks.append(peak)
    assert peaks[1]-peaks[0]<2*MIB,peaks


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_cleanup_interruption_retries_only_owned_inactive_resources(tmp_path,monkeypatch):
    write(tmp_path,b'homeassistant/a'); s,first=point(tmp_path,{b'homeassistant/a':file()}); old=first['pointer']['generation']; original=os.rmdir; failed=[]
    def fail(name,*args,**kwargs):
        if os.fsencode(name).startswith(b'.ha-ops-recovery-'+old.encode()) and not failed:
            failed.append(name); raise OSError(errno.EIO,'owned cleanup interrupted')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(os,'rmdir',fail)
    found=s.install(s.prepare({b'homeassistant/a':file(b'second')}))
    assert failed and found['cleanup'][0]['status']=='debt'
    fresh=store(tmp_path); result=fresh.cleanup(); assert result[0]['status']=='removed' and fresh.cleanup()==[]
    assert fresh.discover()['pointer']==found['pointer']


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='root Linux restrictive parent metadata')
def test_restrictive_parent_finalization_and_restore(tmp_path):
    p=write(tmp_path,b'homeassistant/parent/a'); p.parent.chmod(0)
    s,_=point(tmp_path,{b'homeassistant/parent':n.Directory(os.getuid(),os.getgid(),0o1000),b'homeassistant/parent/a':file()})
    s.execute(); assert stat.S_IMODE(p.parent.stat().st_mode)==0o1000
    s.rollback(); s.rollback(); assert stat.S_IMODE(p.parent.stat().st_mode)==0 and p.read_bytes()==b'old'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux flock')
def test_separate_process_lock_and_stale_identity_refuse(tmp_path):
    p=write(tmp_path,b'homeassistant/a'); s,found=point(tmp_path,{b'homeassistant/a':file()})
    lock=tmp_path/'data/native/recovery/lock'
    script='import fcntl,os,sys;f=open(sys.argv[1],"rb");fcntl.flock(f,fcntl.LOCK_EX);print("held",flush=True);sys.stdin.read()'
    child=subprocess.Popen([sys.executable,'-c',script,str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        with pytest.raises(n.Blocked,match='busy'): s.execute()
    finally: child.stdin.close(); child.wait(timeout=10); child.stdout.close()
    with pytest.raises(n.Blocked,match='stale'): s.execute(revision=found['journal']['revision']+1)
    with pytest.raises(n.Blocked,match='stale'): s.rollback(owner_id='foreign')
    assert p.read_bytes()==b'old'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux allocation')
def test_private_read_retains_primary_error_and_close_debt(tmp_path,monkeypatch):
    write(tmp_path,b'homeassistant/a'); s,_=point(tmp_path,{b'homeassistant/a':file()})
    first=OSError(errno.EIO,'primary read'); original_close=os.close; closed=[]
    def read(*args): raise first
    def close(fd):
        original_close(fd)
        if not closed: closed.append(fd); raise OSError(errno.EBADF,'secondary close')
    monkeypatch.setattr(os,'read',read); monkeypatch.setattr(os,'close',close)
    with pytest.raises(OSError) as caught: s._read(b'active.json')
    assert caught.value is first and any('recovery-read-close' in e for e in first.cleanup_errors)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery')
@pytest.mark.parametrize('parents',[b'homeassistant/missing/deleted',b'homeassistant/missing/deep/deleted'])
def test_absent_deletion_does_not_materialize_absent_parents(tmp_path,parents):
    write(tmp_path,b'homeassistant/sentinel')
    s,_=point(tmp_path,{parents:None}); s.execute(); s.rollback(); s.rollback()
    assert not (tmp_path/'homeassistant/missing').exists()
    assert (tmp_path/'homeassistant/sentinel').read_bytes()==b'old'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery')
@pytest.mark.parametrize('kind',['blob','manifest','journal','marker'])
def test_cleanup_preserves_identical_content_foreign_inode_every_file_class(tmp_path,kind):
    write(tmp_path,b'homeassistant/a'); s,a=point(tmp_path,{b'homeassistant/a':file()}); prepared=s.prepare({b'homeassistant/a':file(b'B')})
    gen=a['pointer']['generation']; base=tmp_path/'data/native/recovery'; names={'blob':next((base/gen).glob('blob-*')),'manifest':base/gen/'manifest.json','journal':base/gen/'journal.json','marker':base/('preparation-'+gen+'.json')}
    target=names[kind]; inodes=[]
    def replace_inactive_inode(stage):
        if stage=='slot_durable':
            assert s.discover()['pointer']['generation']!=gen
            foreign=target.with_name('foreign-copy'); foreign.write_bytes(target.read_bytes())
            foreign.chmod(stat.S_IMODE(target.stat().st_mode)); inodes.append(foreign.stat().st_ino)
            os.replace(foreign,target)
    s.fault=replace_inactive_inode
    found=s.install(prepared); s.fault=lambda _:None
    assert len(inodes)==1
    assert any(row['generation']==gen and row['status']=='debt' for row in found['cleanup'])
    assert target.exists() and target.stat().st_ino==inodes[0]
    assert any(row['status']=='debt' for row in s.cleanup()) and target.stat().st_ino==inodes[0]


def durability_case(tmp_path,action,direction):
    p=write(tmp_path,b'homeassistant/parent/a')
    if action=='remove': targets={b'homeassistant/parent/a':None} if direction=='forward' else {b'homeassistant/new':file()}
    elif action in ('chown','chmod'): targets={b'homeassistant/parent':n.Directory(12345 if action=='chown' else os.getuid(),23456 if action=='chown' else os.getgid(),0o2750)}
    else: targets={b'homeassistant/parent/a':file()}
    s,_=point(tmp_path,targets)
    if direction=='rollback': s.execute()
    def fault(stage):
        if stage=='effect:'+action: raise RuntimeError('effect before durability')
    s.fault=fault
    with pytest.raises(RuntimeError): (s.execute if direction=='forward' else s.rollback)()
    fresh=store(tmp_path); found=fresh.discover(); pending=found['journal']['pending']; assert pending
    path=tmp_path/os.fsdecode(n._path(pending['path'])); expected={str(path.parent)}
    if path.exists(): expected.add(str(path))
    if action=='replace': expected.add(str(tmp_path/os.fsdecode(os.path.dirname(n._path(found['manifest']['stages'][pending['stage']]['path'])))))
    return fresh,expected


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='root Linux recovery durability')
@pytest.mark.parametrize('action',['replace','remove','chown','chmod'])
@pytest.mark.parametrize('direction',['forward','rollback'])
@pytest.mark.parametrize('failure',[False,True])
def test_reconcile_hardens_every_effect_before_ack(tmp_path,monkeypatch,action,direction,failure):
    fresh,expected=durability_case(tmp_path,action,direction); original=os.fsync; observed=[]; raised=[]
    def fsync(fd):
        path=os.readlink('/proc/self/fd/'+str(fd)); observed.append(path)
        if failure and path in expected and not raised: raised.append(path); raise OSError(errno.EIO,'target durability')
        return original(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    if failure:
        with pytest.raises(OSError,match='target durability'): fresh.reconcile()
        assert fresh.discover()['journal']['pending'] is not None
        monkeypatch.setattr(os,'fsync',original)
    else:
        fresh.reconcile(); assert expected<=set(observed)
        assert fresh.discover()['journal']['pending'] is None
    fresh.rollback(); fresh.rollback()


@pytest.mark.skipif(sys.platform!='linux',reason='Linux atomic pointer')
@pytest.mark.parametrize('step',['control_written','control_fsynced','control_replaced','control_durable'])
def test_active_pointer_specific_atomic_substeps_retain_a_or_complete_b(tmp_path,step):
    p=write(tmp_path,b'homeassistant/a'); s,a=point(tmp_path,{b'homeassistant/a':file()}); prepared=s.prepare({b'homeassistant/a':file(b'B')}); reached=[]
    def fault(stage):
        if stage==step+':active.json': reached.append(stage); raise OSError(errno.EIO,'pointer boundary')
    s.fault=fault
    with pytest.raises(OSError): s.install(prepared)
    assert reached==[step+':active.json']
    fresh=store(tmp_path).discover(); assert fresh['pointer']['generation'] in (a['pointer']['generation'],n.parse(prepared.manifest)['generation'])
    assert p.read_bytes()==b'old'
    for state in fresh['manifest']['before'].values():
        if state and state['kind']=='file': assert state['digest']==hashlib.sha256(b'old').hexdigest()


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='isolated root Linux /dev tmpfs proof')
def test_real_distinct_control_target_volumes_and_stale_mount_refusal():
    import shutil,uuid
    owned_pools=set()
    root=Path('/dev'); nonce='ha-ops-proof-'+uuid.uuid4().hex; left=root/nonce; right=root/'shm'/nonce; data=root/'data'
    assert not data.exists(),'isolated runner must have no preexisting /dev/data fixture'
    assert root.stat().st_dev!=(root/'shm').stat().st_dev,'two actual supported mounts required'
    try:
        (data/'native').mkdir(parents=True,mode=0o700); (data/'native-operations').mkdir(mode=0o700)
        left.mkdir(); right.mkdir(); (left/'a').write_bytes(b'left'); (right/'b').write_bytes(b'right')
        b=binding(root); b=dataclasses.replace(b,policy=Policy(extra_live_roots=(nonce.encode(),b'shm/'+nonce.encode())))
        s=n.RecoveryStore(b); found=s.install(s.prepare({(nonce+'/a').encode():file(b'LEFT'),('shm/'+nonce+'/b').encode():file(b'RIGHT')}))
        owned_pools.update(n._path(k) for k in found['manifest']['pools'])
        assert len(found['manifest']['forecast']['volumes'])==2
        for path in ((nonce+'/a').encode(),('shm/'+nonce+'/b').encode()):
            stage=found['manifest']['stages']['forward:'+n._key(path)]; assert stage['state']['dev']==(root/os.fsdecode(path)).stat().st_dev
        with frozen(s.disk): s.execute(); s.rollback(); s.rollback()
        assert (left/'a').read_bytes()==b'left' and (right/'b').read_bytes()==b'right'
        fresh=n.RecoveryStore(b); installed=fresh.install(fresh.prepare({('shm/'+nonce+'/b').encode():file(b'changed')})); owned_pools.update(n._path(k) for k in installed['manifest']['pools'])
        # Substitute the real target route with a symlink to the other actual
        # filesystem, without creating mounts or requiring mount capabilities.
        saved=right.with_name(nonce+'-original'); right.rename(saved)
        foreign=left/'b'; foreign.write_bytes(b'foreign other volume')
        right.symlink_to(left,target_is_directory=True)
        try:
            with pytest.raises((n.Blocked,OSError)): fresh.execute()
            assert foreign.read_bytes()==b'foreign other volume' and (saved/'b').read_bytes()==b'right'
        finally: right.unlink(); saved.rename(right)
        mounts=tuple(dataclasses.replace(m,source=m.source+b'-substituted') if m.point==b'/dev/shm' else m for m in b.mounts)
        fresh.binding=dataclasses.replace(b,mounts=mounts)
        with pytest.raises(n.Blocked): fresh.execute()
        assert (right/'b').read_bytes()==b'right'
        # Return the original explicit binding for owned cleanup.
        fresh.binding=b
        active=fresh.discover(); pools=[n._path(k) for k in active['manifest']['pools']]
        for pool in pools:
            absolute=root/os.fsdecode(pool)
            if absolute.exists(): shutil.rmtree(absolute)
    finally:
        for path in (left,right,data):
            if path.exists(): shutil.rmtree(path)
        # Exact generation-tagged pools below /dev only; no broad /dev cleanup.
        for pool in owned_pools:
            path=root/os.fsdecode(pool)
            if path.exists(): shutil.rmtree(path)


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery')
def test_absent_deletion_below_existing_file_preserves_unselected_ancestor(tmp_path):
    p=write(tmp_path,b'homeassistant/existing-file',b'foreign ancestor')
    s,_=point(tmp_path,{b'homeassistant/existing-file/absent':None}); s.execute(); s.rollback(); s.rollback()
    assert p.read_bytes()==b'foreign ancestor' and p.is_file()
    with pytest.raises(n.Blocked,match='unselected-file-parent'):
        store(tmp_path).prepare({b'homeassistant/existing-file/new':file()})


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery')
@pytest.mark.parametrize('interruption',['partial-marker','unacknowledged-marker-receipt'])
def test_cleanup_requalifies_only_signed_self_pinned_interrupted_controls(tmp_path,interruption):
    write(tmp_path,b'homeassistant/a'); s,a=point(tmp_path,{b'homeassistant/a':file()}); gen=a['pointer']['generation']
    if interruption=='partial-marker':
        marker=tmp_path/'data/native/recovery'/('preparation-'+gen+'.json')
        with marker.open('wb') as stream: stream.write(b'{')
    else:
        calls=[]
        def fault(stage):
            if stage=='control_durable:'+gen+'/journal.json':
                calls.append(stage)
                if len(calls)==4: raise OSError(errno.EIO,'marker receipt interruption')
        s.fault=fault
        with pytest.raises(OSError): s.execute()
        assert (tmp_path/'homeassistant/a').read_bytes()==b'new'
        s=store(tmp_path); assert s.discover()['journal']['phase']=='applied'
    s.fault=lambda _:None
    b=s.install(s.prepare({b'homeassistant/a':file(b'next')}))
    assert any(row['generation']==gen and row['status']=='removed' for row in b['cleanup'])
    assert not (tmp_path/'data/native/recovery'/gen).exists()


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery attribution')
def test_external_delete_after_durable_intent_is_never_adopted_as_own(tmp_path):
    p=write(tmp_path,b'homeassistant/a'); s,_=point(tmp_path,{b'homeassistant/a':None})
    def fault(stage):
        if stage=='intent_durable:remove': raise RuntimeError('before own removal')
    s.fault=fault
    with pytest.raises(RuntimeError): s.execute()
    p.unlink(); fresh=store(tmp_path)
    with frozen(fresh.disk):
        with pytest.raises(n.Blocked,match='unowned-removal'): fresh.rollback()
    assert not p.exists() and fresh.discover()['journal']['pending'] is not None


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery directory race')
@pytest.mark.parametrize('direction',['forward','rollback'])
def test_nonempty_directory_tombstone_is_restored_before_refusal(tmp_path,direction):
    (tmp_path/'homeassistant').mkdir()
    if direction=='forward':
        (tmp_path/'homeassistant/d').mkdir(); targets={b'homeassistant/d':None}
    else: targets={b'homeassistant/d':n.Directory(os.getuid(),os.getgid(),0o755)}
    s,_=point(tmp_path,targets)
    if direction=='rollback': s.execute()
    def race(stage):
        if stage=='effect:remove':
            found=s.discover(); p=found['journal']['pending']; tomb=s._tomb_path(found['manifest'],p['path'],p['direction'])
            (tmp_path/os.fsdecode(tomb)/'foreign').write_bytes(b'foreign child')
    s.fault=race
    with pytest.raises(n.Blocked,match='unselected-child'): (s.execute if direction=='forward' else s.rollback)()
    fresh=store(tmp_path)
    with pytest.raises(n.Blocked,match='nonempty-removal-restored'): fresh.rollback()
    assert (tmp_path/'homeassistant/d/foreign').read_bytes()==b'foreign child'
    if direction=='forward': fresh.rollback(); fresh.rollback()
    else:
        with pytest.raises(OSError): fresh.rollback()
    assert (tmp_path/'homeassistant/d/foreign').read_bytes()==b'foreign child'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery allocation')
def test_private_pool_policy_coexists_with_recovery_and_refuses_pool_targets(tmp_path):
    import base64
    import native_inventory as inventory
    write(tmp_path,b'homeassistant/nested/a',b'original')
    s,installed=point(tmp_path,{b'homeassistant/nested/a':file()})
    pool_paths=[base64.b64decode(p,validate=True) for p in installed['manifest']['pools']]
    assert pool_paths and all(s.binding.policy.reason(p)=='private-recovery-pool' for p in pool_paths)
    for pool in pool_paths:
        assert s._observe(pool)['kind']=='directory'
        with pytest.raises(n.Blocked,match='non-managed-recovery-target'):
            s.prepare({pool+b'/attempt':file()})
    snapshot=inventory.inventory(s.binding.root,(b'homeassistant',),policy=s.binding.policy,
        mounts=s.binding.mounts,confirmed_mounts=s.binding.confirmed_mounts)
    assert not snapshot.blockers
    assert snapshot.managed_files==(b'homeassistant/nested/a',)
    rules=inventory.render_ignore(snapshot,s.binding.policy)
    assert b'.ha-ops-recovery' not in rules
    with frozen(s.disk): s.execute(); s.rollback(); s.rollback()
    assert (tmp_path/'homeassistant/nested/a').read_bytes()==b'original'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery allocation')
@pytest.mark.parametrize('direction',['execute','rollback'])
@pytest.mark.parametrize('boundary',['before_intent:replace','intent_durable:replace','effect:replace','ack_durable:replace'])
def test_install_rechecks_prior_journal_under_lock_before_retirement(tmp_path,direction,boundary):
    original=write(tmp_path,b'homeassistant/a',b'original')
    write(tmp_path,b'homeassistant/b',b'other original')
    s,first=point(tmp_path,{b'homeassistant/a':file(b'applied')})
    prepared=s.prepare({b'homeassistant/b':file(b'other new')})
    if direction=='rollback':s.execute()
    def interrupt(stage):
        if stage==boundary:raise OSError(5,'prior point interrupted')
    s.fault=interrupt
    with pytest.raises(OSError,match='prior point interrupted'):getattr(s,direction)()
    s.fault=lambda _:None
    active=s.discover();assert active['journal']['phase'] in ('forward','restoring')
    before_entries=set((tmp_path/'data/native/recovery').iterdir())
    with pytest.raises(n.Blocked,match='pending-recovery-point-cannot-be-replaced'):s.install(prepared)
    assert s.discover()==active
    assert set((tmp_path/'data/native/recovery').iterdir())==before_entries
    assert (tmp_path/'data/native/recovery'/first['pointer']['generation']).is_dir()
    assert (tmp_path/'homeassistant/b').read_bytes()==b'other original'
    with frozen(s.disk):s.rollback();s.rollback()
    assert original.read_bytes()==b'original'


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery allocation')
@pytest.mark.parametrize('phase',['ready','applied','restored'])
def test_terminal_prior_foreign_edit_does_not_block_disjoint_replacement(tmp_path,phase):
    original=write(tmp_path,b'homeassistant/a',b'original')
    write(tmp_path,b'homeassistant/b',b'other original')
    s,first=point(tmp_path,{b'homeassistant/a':file()})
    prepared=s.prepare({b'homeassistant/b':file()})
    if phase!='ready':s.execute()
    if phase=='restored':s.rollback()
    original.write_bytes(b'foreign')
    second=s.install(prepared)
    assert s.discover()['pointer']==second['pointer']
    assert second['pointer']!=first['pointer']
    assert not (tmp_path/'data/native/recovery'/first['pointer']['generation']).exists()
    s.execute();s.rollback();s.rollback()
    assert original.read_bytes()==b'foreign'
    assert (tmp_path/'homeassistant/b').read_bytes()==b'other original'


@pytest.mark.parametrize('growth_at',['first','last'])
def test_streamed_source_growth_never_exceeds_frozen_destination_size(tmp_path,monkeypatch,growth_at):
    size=2*65536+17
    source=write(tmp_path,b'homeassistant/source',b'A'*size)
    s=store(tmp_path);state,payload=s._observe(b'homeassistant/source',content=True)
    destination=tmp_path/'destination';read=os.read;fired=[];requests=[]
    with source.open('rb') as identity:
        source_inode=os.fstat(identity.fileno()).st_ino
    def growing(fd,count):
        if os.fstat(fd).st_ino==source_inode:
            position=os.lseek(fd,0,os.SEEK_CUR);requests.append((position,count))
            if not fired and (growth_at=='first' or position>=size-17):
                fired.append(True)
                with source.open('ab') as stream:stream.write(b'UNQUALIFIED'*10000)
        return read(fd,count)
    monkeypatch.setattr(os,'read',growing)
    with destination.open('wb') as output:
        with pytest.raises(n.Blocked,match='changed-recovery-source'):s._emit(payload,output.fileno())
    assert fired and destination.stat().st_size==size
    assert destination.read_bytes()==b'A'*size
    assert requests[-1]==(size,1)
    assert all(count<=size-position for position,count in requests[:-1])


@pytest.mark.parametrize('chunks,size,expected',[
    ([b'oversized'],3,b''),([b'ab',b'oversized'],3,b'ab'),([b'ab',b'c',b'extra'],3,b'abc')])
def test_chunk_writer_refuses_oversized_chunk_before_write(tmp_path,chunks,size,expected):
    destination=tmp_path/'destination'
    with destination.open('wb') as output:
        with pytest.raises(n.Blocked,match='exceeds-qualified-size'):
            n.RecoveryStore._emit_chunks(iter(chunks),output.fileno(),hashlib.sha256(b'abc').hexdigest(),size)
    assert destination.read_bytes()==expected
    assert destination.stat().st_size<=size


@pytest.mark.parametrize('limit',[1,7])
def test_chunk_writer_short_writes_stay_within_qualified_size(tmp_path,monkeypatch,limit):
    destination=tmp_path/'destination';write=os.write;data=b'bounded bytes'*20
    monkeypatch.setattr(os,'write',lambda fd,chunk:write(fd,chunk[:limit]))
    with destination.open('wb') as output:
        n.RecoveryStore._emit_chunks(iter((data[:13],data[13:])),output.fileno(),hashlib.sha256(data).hexdigest(),len(data))
    assert destination.read_bytes()==data


@pytest.mark.skipif(sys.platform!='linux',reason='Linux recovery allocation')
@pytest.mark.parametrize('kind',['blob','manifest','journal'])
def test_corrupt_active_point_refuses_replacement_before_generation_effects(tmp_path,kind):
    write(tmp_path,b'homeassistant/a');write(tmp_path,b'homeassistant/b')
    s,first=point(tmp_path,{b'homeassistant/a':file()})
    prepared=s.prepare({b'homeassistant/b':file(b'B')})
    base=tmp_path/'data/native/recovery';generation=base/first['pointer']['generation']
    target={'blob':next(generation.glob('blob-*')),'manifest':generation/'manifest.json','journal':generation/'journal.json'}[kind]
    foreign=target.with_name('foreign-copy');foreign.write_bytes(target.read_bytes())
    foreign.chmod(stat.S_IMODE(target.stat().st_mode));inode=foreign.stat().st_ino;os.replace(foreign,target)
    before={p.relative_to(base):(p.stat().st_ino,p.read_bytes()) for p in base.rglob('*') if p.is_file()}
    before_dirs={p.relative_to(base):p.stat().st_ino for p in base.rglob('*') if p.is_dir()}
    with mock.patch.object(s,'_cleanup_debt_locked',side_effect=AssertionError('active corruption cannot authorize cleanup')):
        with pytest.raises(n.Blocked,match='substituted-recovery'):s.install(prepared)
    assert n.parse(s._pointer())==first['pointer']
    assert {p.relative_to(base):(p.stat().st_ino,p.read_bytes()) for p in base.rglob('*') if p.is_file()}==before
    assert {p.relative_to(base):p.stat().st_ino for p in base.rglob('*') if p.is_dir()}==before_dirs
    assert target.stat().st_ino==inode and (tmp_path/'homeassistant/b').read_bytes()==b'old'
