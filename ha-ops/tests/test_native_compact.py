"""Linux process-tree ownership, exact retention and allocation failure proofs."""
from pathlib import Path
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock
import pytest
from test_native_storage import storage,git
import native_git as g
import native_disk as d

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='real Linux maintenance proof')


def alive(pid):
    try:return Path('/proc',str(pid),'stat').read_text().split()[2]!='Z'
    except FileNotFoundError:return False


def writer_program(marker):
    return "import os,time;child=os.fork();\nif child:open(%r,'w').write(str(os.getpid())+' '+str(child))\ntime.sleep(20)"%str(marker)


def test_guardian_timeout_kills_descendants_before_unlock(storage):
    root,s=storage;s.git.timeout=.3
    marker=root/'writers';lock=root/'fixture.lock';fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
    try:
        with pytest.raises(g.GitFailure):s.git._compact_guardian([sys.executable,'-c',writer_program(marker)],s.git._environment(),(fd,))
        pids=[int(v) for v in marker.read_text().split() if int(v)>0]
        assert all(not alive(pid) for pid in pids)
        other=os.open(lock,os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(other)
    finally:os.close(fd)


def test_guardian_parent_death_holds_lock_until_descendants_quiescent(storage):
    root,s=storage;marker=root/'writers';lock=root/'fixture.lock'
    source=str(Path(g.__file__).parent)
    code="""import sys,os,fcntl
sys.path.insert(0,sys.argv[1]);from native_git import NativeGit
owner=object.__new__(NativeGit);owner.root=os.fsencode(sys.argv[2]);owner.timeout=20
fd=os.open(sys.argv[3],os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
owner._compact_guardian([sys.executable,'-c',sys.argv[4]],os.environ.copy(),(fd,))
"""
    parent=subprocess.Popen([sys.executable,'-c',code,source,str(root),str(lock),writer_program(marker)])
    try:
        end=time.monotonic()+3
        while not marker.exists() and time.monotonic()<end:time.sleep(.01)
        assert marker.exists()
        pids=[int(v) for v in marker.read_text().split() if int(v)>0]
        fd=os.open(lock,os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            parent.kill();parent.wait(timeout=3)
            end=time.monotonic()+3
            while time.monotonic()<end:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:time.sleep(.01)
            else:pytest.fail('guardian did not settle inherited lock')
            assert all(not alive(pid) for pid in pids),[(pid,Path('/proc',str(pid),'stat').read_text() if Path('/proc',str(pid),'stat').exists() else 'gone') for pid in pids]
        finally:os.close(fd)
    finally:
        if parent.poll() is None:parent.kill();parent.wait(timeout=3)


@pytest.mark.parametrize('kind',['thread-store','thread-resource','process-store','process-resource'])
def test_nonblocking_owner_contention(storage,kind):
    root,s=storage
    if kind.startswith('thread'):
        lock=s.operations.mutex if kind.endswith('store') else s.git._mutex
        ready=threading.Event();release=threading.Event()
        def hold():
            with lock:ready.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start();assert ready.wait(1)
        try:
            begin=time.monotonic()
            with pytest.raises((ValueError,RuntimeError)):s.preview()
            assert time.monotonic()-begin<1
        finally:release.set();thread.join(2)
    else:
        path=root/('data/native-operations/lock' if kind.endswith('store') else 'data/native/git-resources/journal.lock')
        fd=os.open(path,os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            with pytest.raises((ValueError,RuntimeError,OSError)):s.preview()
        finally:os.close(fd)


@pytest.mark.parametrize('stage',['compact_fenced','diagnostic_opened','diagnostic_written','diagnostic_synced','diagnostic_replaced','diagnostic_durable'])
def test_interruption_never_restores_authority(storage,stage):
    root,s=storage;preview=s.preview();counts=[]
    def fault(value):
        if value==stage:raise OSError(errno.EIO,'injected')
    s.fault=fault
    with mock.patch.object(s.git,'compact',side_effect=lambda permit:counts.append(1)):
        with pytest.raises(OSError):s.confirm(preview['review']['id'])
    assert not counts
    with pytest.raises(d.Blocked):s.confirm(preview['review']['id'])
    assert s.status().get('maintenance') is None or s.status()['maintenance']['status']!='completed'


def test_actual_observed_allocation_below_fixed_peak(storage):
    root,s=storage
    blob=git(root,'hash-object','-w','--stdin',input=os.urandom(4*1024*1024)).strip()
    tree=git(root,'mktree',input=b'100644 blob '+blob+b'\tfixture\n').strip()
    commit=git(root,'commit-tree',tree.decode(),input=b'allocation proof\n').strip();git(root,'update-ref','refs/heads/fixture',commit.decode())
    def allocated():return sum(p.lstat().st_blocks*512 for p in root.rglob('*'))+root.stat().st_blocks*512
    preview=s.preview();plan=s._reviews[preview['review']['id']][0];before=allocated();peak=[before];stop=threading.Event()
    def sample():
        while not stop.is_set():
            try:peak.append(allocated())
            except FileNotFoundError:pass
            stop.wait(.001)
    thread=threading.Thread(target=sample);thread.start()
    try:result=s.confirm(preview['review']['id'])
    finally:stop.set();thread.join(2)
    assert result['maintenance']['status']=='completed'
    issued=sum(v.additional_peak+v.reserve for v in plan.decisions)
    assert max(peak)-before<=issued,(max(peak)-before,issued)
    print('ACTUAL Compact peak increase',max(peak)-before,'issued',issued)


def test_real_byte_enospc_in_private_container_shm(storage):
    import tempfile,shutil
    root,s=storage
    with tempfile.TemporaryDirectory(prefix='compact-byte-',dir='/dev/shm') as temporary:
        target=Path(temporary);info=os.statvfs(target)
        assert 0<info.f_blocks*info.f_frsize<=128*1024**2
        # Move the exact diagnostic destination to an isolated real tmpfs by
        # building this entire fixture there. No symlink or bind substitution.
        local=target/'fixture';local.mkdir()
        from test_native_storage import storage as fixture
        local_root,owner=fixture.__wrapped__(local)
        owner.calculate();preview=owner.preview();previous=owner.status()['measurement']
        def exhaust(stage):
            if stage=='compact_fenced':
                try:
                    with (target/'owned-filler').open('wb',buffering=0) as output:
                        while True:output.write(b'x'*4096)
                except OSError as error:
                    assert error.errno==errno.ENOSPC
        owner.fault=exhaust
        with pytest.raises(OSError) as error:owner.confirm(preview['review']['id'])
        assert error.value.errno==errno.ENOSPC
        (target/'owned-filler').unlink()
        assert owner.status()['measurement']==previous
        assert owner.status()['maintenance'] is None or owner.status()['maintenance']['status']!='completed'
        with pytest.raises(d.Blocked):owner.confirm(preview['review']['id'])


def test_real_inode_exhaustion_in_wrapper_ext4():
    if os.environ.get('HA_OPS_EXT4_PROFILE')!='inodes':pytest.skip('requires wrapper ext4 inodes profile')
    import tempfile
    base=Path(os.environ['HA_OPS_EXT4_ROOT'])
    with tempfile.TemporaryDirectory(prefix='compact-inodes-',dir=base) as temporary:
        target=Path(temporary)
        from test_native_storage import storage as fixture
        root,owner=fixture.__wrapped__(target)
        owner.calculate();preview=owner.preview();before=owner.status()['measurement']
        allocated=[]
        def exhaust(stage):
            if stage=='compact_fenced':
                for number in range(os.statvfs(target).f_files+8):
                    path=target/('owned-inode-'+str(number))
                    try:path.touch();allocated.append(path)
                    except OSError as error:
                        assert error.errno==errno.ENOSPC;break
                else:pytest.fail('actual inode exhaustion not reached')
                assert os.statvfs(target).f_bavail*os.statvfs(target).f_frsize>16*1024**2
        owner.fault=exhaust
        try:
            with pytest.raises(OSError) as error:owner.confirm(preview['review']['id'])
            assert error.value.errno==errno.ENOSPC
        finally:
            for path in allocated:path.unlink()
        assert owner.status()['measurement']==before
        assert owner.status()['maintenance'] is None or owner.status()['maintenance']['status']!='completed'
