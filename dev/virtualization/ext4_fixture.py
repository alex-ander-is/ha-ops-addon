#!/usr/bin/env python3
"""Privileged guest-only bounded fixture owner; never imported by the App."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

BASE = Path('/var/lib/ha-ops-dev-ext4')
PROFILES = {'allocation': (256 * 1024**2, 8192), 'bytes': (256 * 1024**2, 8192),
            'inodes': (64 * 1024**2, 128)}
RESERVE = 2 * 1024**3
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Refused(RuntimeError):
    pass


def error_payload(error):
    return {'error': str(error), 'type': type(error).__name__,
            'errno': getattr(error, 'errno', None),
            'cleanup_errors': list(getattr(error, 'cleanup_errors', ()))}


def finish_actions(actions, first=None):
    """Attempt all safe cleanup actions without replacing the causal failure."""
    active = sys.exc_info()[1]
    first = active if first is None else first
    for label, action in actions:
        try: action()
        except FileNotFoundError:
            if label != 'temporary unlink':
                error = sys.exc_info()[1]
                if first is None: first = error
                else: first.cleanup_errors = tuple(getattr(first, 'cleanup_errors', ())) + (label + ': ' + str(error),)
        except BaseException as error:
            if first is None: first = error
            else: first.cleanup_errors = tuple(getattr(first, 'cleanup_errors', ())) + (label + ': ' + str(error),)
    if active is None and first is not None: raise first


def close_owned(fd, label='descriptor close'):
    finish_actions([(label, lambda: os.close(fd))])


def call(*args):
    r = subprocess.run(args, capture_output=True, timeout=60)
    if r.returncode:
        raise Refused('%s: %s' % (args[0], r.stderr.decode(errors='replace').strip()))
    return r.stdout.decode().strip()


def identity(s):
    value = [s.st_dev, s.st_ino, s.st_mode]
    return value if stat.S_ISDIR(s.st_mode) else value + [s.st_nlink]


def private_dir(path):
    fd = os.open(path, FLAGS)
    try:
        s = os.fstat(fd)
        if s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o700:
            raise Refused('root-owned private directory required')
        return fd
    except BaseException:
        close_owned(fd, 'private directory close')
        raise


def backing(path, expected=None, size=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != 0:
            raise Refused('exclusive root-owned regular backing required')
        if expected is not None and identity(s) != expected:
            raise Refused('backing identity changed')
        if size is not None and (s.st_size != size or s.st_blocks * 512 < size):
            raise Refused('backing must remain fully reserved at exact size')
        return {'identity': identity(s), 'size': s.st_size, 'allocated': s.st_blocks * 512}
    finally:
        close_owned(fd)


def capacity(path, size):
    v = os.statvfs(path)
    data = {'path': str(path), 'available_bytes': v.f_bavail * v.f_frsize,
            'available_inodes': v.f_favail}
    if data['available_bytes'] < size + RESERVE or data['available_inodes'] < 32:
        raise Refused('insufficient actual backing-parent capacity')
    return data


def mounts(path):
    rows = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        a, b = left.split(), right.split()
        if a[4] == str(path):
            rows.append({'id': a[0], 'device': a[2], 'root': a[3], 'point': a[4],
                         'options': a[5], 'filesystem': b[0], 'source': b[1], 'super_options': b[2]})
    return rows


def loops(image):
    result = json.loads(call('losetup', '--json', '--list', '--output', 'NAME,MAJ:MIN,BACK-FILE,BACK-INO,BACK-MAJ:MIN', '--associated', str(image)))
    return result.get('loopdevices', [])


def verify_loop(row, image, info):
    if not re.fullmatch(r'/dev/loop[0-9]+', row.get('name', '')):
        raise Refused('unrecognized loop identity')
    if row.get('back-file') != str(image) or int(row['back-ino']) != info['identity'][1]:
        raise Refused('loop backing identity changed')
    device = '%d:%d' % (os.major(info['identity'][0]), os.minor(info['identity'][0]))
    if row['back-maj:min'] != device:
        raise Refused('loop backing device changed')
    return row


def verify_mount(row, loop):
    opts = set(row['options'].split(',')) | set(row['super_options'].split(','))
    if row['filesystem'] != 'ext4' or row['source'] != loop['name'] or row['device'] != loop['maj:min'] or row['root'] != '/':
        raise Refused('mount/loop identity mismatch')
    if not {'rw', 'nosuid', 'nodev', 'noexec', 'data=ordered'} <= opts or opts & {'ro', 'quota', 'usrquota', 'grpquota', 'prjquota'}:
        raise Refused('unsupported mount options')
    return row


def save(fd, data):
    # Fixed single owned temporary, exclusive/nofollow. A failed checkpoint
    # leaves the preceding manifest available for exact-resource reconciliation.
    blob = json.dumps(data, sort_keys=True).encode()
    t = os.open('manifest.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    first = None
    try:
        pending = memoryview(blob)
        while pending:
            n = os.write(t, pending)
            if n <= 0: raise Refused('manifest write made no progress')
            pending = pending[n:]
        os.fsync(t)
        os.replace('manifest.tmp', 'manifest.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException as error:
        first = error
    finish_actions([('manifest descriptor close', lambda: os.close(t)),
                    ('temporary unlink', lambda: os.unlink('manifest.tmp', dir_fd=fd))], first)


def load(fd, run):
    m = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        s = os.fstat(m)
        if s.st_uid != 0 or s.st_nlink != 1 or not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > 65536:
            raise Refused('private bounded ownership manifest required')
        with os.fdopen(m, 'r', closefd=False) as f: data = json.load(f)
    finally: close_owned(m)
    if data.get('schema') != 1 or data.get('run') != run or data.get('profile') not in PROFILES or data.get('directory', [])[:3] != identity(os.fstat(fd))[:3]:
        raise Refused('manifest/run owner mismatch')
    data['directory'] = identity(os.fstat(fd))
    return data


def observe(fd, data):
    run = BASE / data['run']
    image, target = run / 'image', run / 'mount'
    info = backing(image, data.get('backing', {}).get('identity'), PROFILES[data['profile']][0])
    current = loops(image)
    if len(current) != 1:
        raise Refused('exact single loop attachment required')
    loop = verify_loop(current[0], image, info)
    if data.get('loop') is not None and data['loop'] != loop:
        raise Refused('loop identity changed')
    rows = mounts(target)
    if len(rows) != 1: raise Refused('exact single mount required')
    mounted = verify_mount(rows[0], loop)
    if data.get('mount') is not None and data['mount'] != mounted:
        raise Refused('mount identity changed')
    uuid = call('blkid', '-s', 'UUID', '-o', 'value', loop['name'])
    if data.get('uuid') is not None and data['uuid'] != uuid: raise Refused('filesystem UUID changed')
    header = call('dumpe2fs', '-h', image)
    features = next(line.split(':', 1)[1].strip() for line in header.splitlines() if line.startswith('Filesystem features:'))
    if {'bigalloc', 'quota'} & set(features.split()): raise Refused('unsupported filesystem features')
    v = os.statvfs(target)
    if v.f_frsize != 4096 or v.f_files <= 0: raise Refused('unsupported ext4 quantum/inodes')
    return {**data, 'backing': info, 'loop': loop, 'mount': mounted, 'uuid': uuid,
            'features': features, 'quantum': v.f_frsize, 'total_inodes': v.f_files,
            'mount_path': str(target), 'mount_device_number': os.stat(target).st_dev}


def setup(fd, run, profile, uid, gid):
    image, target = BASE / run / 'image', BASE / run / 'mount'
    size, count = PROFILES[profile]
    data = {'schema': 1, 'run': run, 'profile': profile, 'directory': identity(os.fstat(fd)), 'stage': 'directory'}
    save(fd, data)
    data['capacity'] = capacity(BASE, size)
    b = os.open('image', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        data['backing'] = {'identity': identity(os.fstat(b))}
        save(fd, data)  # Capture before preallocation or format.
        capacity(BASE, size)
        call('fallocate', '-l', str(size), str(image))
        os.fsync(b)
    finally: close_owned(b)
    data['backing'] = backing(image, data['backing']['identity'], size)
    data['stage'] = 'reserved'; save(fd, data)
    if loops(image): raise Refused('backing already attached before format')
    call('mkfs.ext4', '-q', '-F', '-b', '4096', '-N', str(count), '-m', '0', '-E',
         'nodiscard,lazy_itable_init=0,lazy_journal_init=0', str(image))
    data['backing'] = backing(image, data['backing']['identity'], size)
    data['stage'] = 'formatted'; save(fd, data)
    call('losetup', '--find', '--show', '--nooverlap', str(image))
    current = loops(image)
    if len(current) != 1: raise Refused('unexpected loop attachment count')
    data['loop'] = verify_loop(current[0], image, data['backing'])
    data['stage'] = 'attached'; save(fd, data)
    os.mkdir('mount', mode=0o700, dir_fd=fd)
    data['mount_directory'] = identity(os.stat('mount', dir_fd=fd, follow_symlinks=False))
    save(fd, data)
    call('mount', '-t', 'ext4', '-o', 'rw,nosuid,nodev,noexec,data=ordered', data['loop']['name'], str(target))
    rows = mounts(target)
    if len(rows) != 1: raise Refused('unexpected mount count')
    data['mount'] = verify_mount(rows[0], data['loop'])
    data['stage'] = 'mounted'; save(fd, data)
    os.chown(target, uid, gid); os.chmod(target, 0o700)
    data = observe(fd, data); save(fd, data)
    return data


def cleanup(fd, data):
    run = BASE / data['run']; image, target = run / 'image', run / 'mount'
    try: info = backing(image, data.get('backing', {}).get('identity'))
    except FileNotFoundError:
        if data.get('backing') and data.get('stage') != 'backing-removal-pending': raise Refused('recorded backing disappeared')
        info = None
    if info and not data.get('backing'): raise Refused('unrecorded backing retains ownership debt')
    current = loops(image) if info else []
    if len(current) > 1: raise Refused('multiple loop attachments refuse cleanup')
    loop = verify_loop(current[0], image, info) if current else None
    if loop and data.get('loop') and loop != data['loop']: raise Refused('substituted loop refuses cleanup')
    rows = mounts(target)
    if len(rows) > 1: raise Refused('stacked mount refuses cleanup')
    if rows:
        if not loop: raise Refused('mounted fixture has no verified loop')
        mounted = verify_mount(rows[0], loop)
        if data.get('mount') and data['mount'] != mounted: raise Refused('substituted mount refuses cleanup')
        # Refuse aliases/binds of the loop anywhere else before removing it.
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
        if sum(line.split()[2] == loop['maj:min'] for line in lines) != 1:
            raise Refused('unexpected mount reference retains fixture')
        call('umount', str(target))
        if mounts(target): raise Refused('mount remains after unmount')
    if loop:
        if any(line.split()[2] == loop['maj:min'] for line in Path('/proc/self/mountinfo').read_text().splitlines()):
            raise Refused('loop still mounted')
        call('losetup', '--detach', loop['name'])
        for _ in range(20):
            if not loops(image): break
            time.sleep(.05)
        if loops(image): raise Refused('loop attachment remains')
    if info:
        backing(image, info['identity'])
        data['stage'] = 'backing-removal-pending'; save(fd, data)
        os.unlink('image', dir_fd=fd); os.fsync(fd)
    try:
        actual = os.stat('mount', dir_fd=fd, follow_symlinks=False)
        if data.get('mount_directory', [])[:3] != identity(actual)[:3]: raise Refused('substituted mount directory')
        os.rmdir('mount', dir_fd=fd)
    except FileNotFoundError: pass
    # Keep a tombstone until the empty directory is removed by the caller.
    data['stage'] = 'backing-removal-pending'; save(fd, data)
    # Caller removes this final manifest immediately before rmdir while holding
    # the root lock; an interrupted empty directory remains explicit debt.
    os.fsync(fd)
    return {'status': 'absent', 'run': data['run'], 'mount': False, 'loop': False, 'backing': False}


def main():
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['preflight', 'setup', 'observe', 'cleanup'])
    p.add_argument('--run', required=True); p.add_argument('--profile', choices=PROFILES, default='allocation')
    p.add_argument('--uid', type=int, default=501); p.add_argument('--gid', type=int, default=1000)
    a = p.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{32}', a.run) or a.uid <= 0 or a.gid < 0:
        raise Refused('root helper, strict run ID and nonzero application UID required')
    for tool in ('fallocate', 'mkfs.ext4', 'losetup', 'mount', 'umount', 'dumpe2fs', 'blkid'):
        if not shutil.which(tool): raise Refused('required guest tool unavailable: ' + tool)
    # Never traverse a caller-controlled root, symlink or nonprivate ancestor.
    for ancestor in (Path('/var'), Path('/var/lib')):
        s = ancestor.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) & 0o022:
            raise Refused('unsafe guest backing ancestor')
    if a.action == 'preflight':
        path = BASE if BASE.exists() else BASE.parent
        if BASE.exists():
            probe = private_dir(BASE); os.close(probe)
        print(json.dumps({'capacity': capacity(path, PROFILES[a.profile][0]),
                          'python': sys.version, 'losetup': call('losetup', '--version'),
                          'mkfs': subprocess.run(['mkfs.ext4', '-V'], capture_output=True).stderr.decode().strip()})); return
    BASE.mkdir(mode=0o700, exist_ok=True)
    base = private_dir(BASE)
    lock = os.open('lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=base)
    try:
        s = os.fstat(lock)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_nlink != 1: raise Refused('unsafe helper lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = BASE / a.run
        receipt_name = a.run + '.cleanup'
        try:
            rfd = os.open(receipt_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=base)
        except FileNotFoundError:
            receipt = None
        else:
            try:
                rs = os.fstat(rfd)
                if rs.st_uid != 0 or rs.st_nlink != 1 or not stat.S_ISREG(rs.st_mode) or rs.st_size > 65536:
                    raise Refused('invalid cleanup receipt')
                receipt = json.loads(os.read(rfd, 65536))
                if receipt.get('run') != a.run or receipt.get('schema') != 1: raise Refused('cleanup receipt owner mismatch')
            finally: close_owned(rfd)
        if receipt is not None:
            if a.action != 'cleanup': raise Refused('cleanup receipt requires exact cleanup')
            if path.exists():
                tfd = private_dir(path)
                try:
                    if identity(os.fstat(tfd)) != receipt['directory']: raise Refused('cleanup directory substituted')
                    names = set(os.listdir(tfd))
                    if names - {'manifest.json'}: raise Refused('unexpected final cleanup entries')
                    if names:
                        state = load(tfd, a.run)
                        if state['stage'] != 'backing-removal-pending': raise Refused('invalid final cleanup state')
                        os.unlink('manifest.json', dir_fd=tfd); os.fsync(tfd)
                finally: close_owned(tfd)
                os.rmdir(a.run, dir_fd=base); os.fsync(base)
            if loops(path / 'image') or mounts(path / 'mount'): raise Refused('resource remains after directory removal')
            os.unlink(receipt_name, dir_fd=base); os.fsync(base)
            print(json.dumps({'status': 'absent', 'run': a.run})); return
        if a.action == 'cleanup' and not path.exists():
            if loops(path / 'image') or mounts(path / 'mount'): raise Refused('unrecorded orphan resources')
            print(json.dumps({'status': 'absent', 'run': a.run})); return
        if a.action == 'setup' and not path.exists():
            # One bounded fixture at a time; unrecognized debt needs exact cleanup.
            if any(re.fullmatch('[0-9a-f]{32}', n) for n in os.listdir(base)):
                raise Refused('existing fixture debt must be cleaned first')
            os.mkdir(a.run, mode=0o700, dir_fd=base); os.fsync(base)
            fd = private_dir(path)
            try: result = setup(fd, a.run, a.profile, a.uid, a.gid)
            finally: close_owned(fd)
        else:
            fd = private_dir(path)
            try:
                data = load(fd, a.run)
                if a.action == 'cleanup': result = cleanup(fd, data)
                else:
                    if data['profile'] != a.profile: raise Refused('fixture profile changed')
                    result = observe(fd, data)
            finally: close_owned(fd)
            if a.action == 'cleanup':
                # Durable outside-directory removal intent closes the unlink /
                # rmdir interruption gap without trusting an empty basename.
                receipt = {'schema': 1, 'run': a.run, 'directory': data['directory']}
                rfd = os.open(receipt_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=base)
                try:
                    pending = memoryview(json.dumps(receipt).encode())
                    while pending:
                        n = os.write(rfd, pending)
                        if n <= 0: raise Refused('cleanup receipt write made no progress')
                        pending = pending[n:]
                    os.fsync(rfd)
                finally: close_owned(rfd)
                os.fsync(base)
                tfd = private_dir(path)
                try:
                    verified = load(tfd, a.run)
                    if verified['stage'] != 'backing-removal-pending' or set(os.listdir(tfd)) != {'manifest.json'}: raise Refused('invalid final cleanup manifest')
                    os.unlink('manifest.json', dir_fd=tfd); os.fsync(tfd)
                finally: close_owned(tfd)
                os.rmdir(a.run, dir_fd=base); os.fsync(base)
                os.unlink(receipt_name, dir_fd=base); os.fsync(base)
        print(json.dumps(result, sort_keys=True))
    finally: finish_actions([('lock close', lambda: os.close(lock)), ('base close', lambda: os.close(base))])


if __name__ == '__main__':
    try: main()
    except BaseException as error:
        print('EXT4_FAILURE ' + json.dumps(error_payload(error), sort_keys=True), file=sys.stderr); sys.exit(1)
