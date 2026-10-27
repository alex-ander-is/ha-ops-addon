"""Dormant, explicit local transition. No production entrypoint imports this module.

The cooperative writer boundary is the shared NativeDisk lock. A saved intent
is evidence, never permission: every start/resume consumes a fresh in-memory
confirmation. Private partial outputs are retained; there is no cleanup API.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import time
import stat
import subprocess
import sys
import uuid

from native_inventory import Blocked, beneath, linux_mounts
from native_migration import (PersistenceUncertain, _digest, _json, _stability, _unique, _stat,
                              encode_path, decode_path, MAX_CONTROL)

STORE = b'data/native-migration-transition'
TERMINAL = 'local-transition-complete-awaiting-review'
MAX_ROWS = 20000
MAX_BYTES = (1 << 63) - 1
JOURNAL_LIMIT = 8 * 1024**2
CHUNK = 65536
POLICY = b'# HA Ops: no content selected; explicit routing only\n/*\n'


def identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]


def _rename_noreplace(source_fd, source, target_fd, target):
    if sys.platform != 'linux':
        raise Blocked('linux-atomic-no-replace-required')
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise Blocked('renameat2-unavailable')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, source, target_fd, target, 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


@dataclass(frozen=True)
class Recipe:
    owner: object
    operation: str
    sizes: tuple
    entries: int
    source_bytes: int
    journal_bytes: int = JOURNAL_LIMIT

    def check(self, root):
        if (self.owner._recipes.get(id(self)) is not self or self.owner.root != root
                or not re.fullmatch('[0-9a-f]{32}', self.operation)
                or type(self.sizes) is not tuple or len(self.sizes) > MAX_ROWS * 2 + 30
                or any(type(n) is not int or not 0 <= n <= MAX_BYTES for n in self.sizes)
                or sum(self.sizes) > MAX_BYTES
                or type(self.entries) is not int or not 0 < self.entries <= MAX_ROWS * 2 + 40
                or not 0 <= self.source_bytes <= MAX_BYTES
                or type(self.journal_bytes) is not int or not 0 < self.journal_bytes <= JOURNAL_LIMIT):
            raise Blocked('issued-bounded-transition-recipe-required')


class Confirmation:
    __slots__ = ('issuer', 'binding', 'used')
    def __init__(self, issuer, binding):
        self.issuer, self.binding, self.used = issuer, binding, False


class Transition:
    def __init__(self, preparation, *, trace=lambda event: None):
        self.p, self.root, self.trace = preparation, preparation.root, trace
        self._issuer = object()
        self._recipes = {}
        self._journal = None

    def _effect(self, kind, path, call):
        result = call()
        self.trace((kind, encode_path(path)))
        return result

    @contextmanager
    def _parent(self, path):
        parent, _, name = path.rpartition(b'/')
        with self.p._root() as root:
            fd = self.p._directory(root, parent) if parent else os.dup(root)
            try:
                yield fd, name
                current = self.p._directory(root, parent) if parent else os.dup(root)
                try:
                    if identity(os.fstat(fd)) != identity(os.fstat(current)):
                        raise Blocked('transition-parent-replaced')
                finally: os.close(current)
            finally: os.close(fd)

    def _info(self, path):
        try:
            with self._parent(path) as (fd, name):
                return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: return None

    def _read(self, path, limit=MAX_BYTES):
        with self._parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise Blocked('unsafe-transition-file')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if _stat(os.fstat(fd)) != _stat(info): raise Blocked('transition-file-replaced')
                chunks = []; total = 0
                while True:
                    chunk = os.read(fd, CHUNK)
                    if not chunk: break
                    total += len(chunk)
                    if total > limit: raise Blocked('transition-file-limit')
                    chunks.append(chunk)
                if (_stat(os.fstat(fd)) != _stat(info) or
                        _stat(os.stat(name,dir_fd=parent,follow_symlinks=False))!=_stat(info)):
                    raise Blocked('transition-file-changed')
                return b''.join(chunks)
            finally: os.close(fd)

    def _hash(self, path):
        with self.p._root() as root:
            return self.p._read(root,path)

    def _manifest(self, base):
        rows = []; todo = [b'']; total = 0
        while todo:
            relative = todo.pop()
            path = base + (b'/' + relative if relative else b'')
            with self.p._root() as root:
                fd = self.p._directory(root, path)
                try:
                    for name in sorted(os.fsencode(n) for n in os.listdir(fd)):
                        rel = relative + b'/' + name if relative else name
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        self.p.binding.check(base+b'/'+rel, info, self.p._mounts())
                        directory = stat.S_ISDIR(info.st_mode)
                        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                            raise Blocked('unsupported-metadata-object')
                        if name in (b'alternates', b'http-alternates', b'commondir', b'gitdir') or name.startswith(b'sharedindex.') or name.endswith(b'.lock'):
                            raise Blocked('unsupported-metadata-routing-or-pending')
                        total += 0 if directory else info.st_size
                        if total > MAX_BYTES or len(rows) >= MAX_ROWS: raise Blocked('metadata-resource-limit')
                        rows.append(dict(path=encode_path(rel), directory=directory,
                                         identity=identity(info), stat=_stat(info),
                                         size=0 if directory else info.st_size,
                                         digest=None if directory else self._hash(base+b'/'+rel)))
                        if directory: todo.append(rel)
                finally: os.close(fd)
        return sorted(rows, key=lambda row: decode_path(row['path']))

    @staticmethod
    def _contents(rows):
        return [(r['path'], r['directory'], r['size'], r['digest']) for r in rows]

    def _protected(self):
        """Strict checkout plus immutable candidate-scoped live descriptors."""
        result=[];todo=[self.p.legacy]
        source=self.p.legacy+b'/.git'
        omitted=(STORE,source,b'data/native/repository.git',b'data/native/repository-files',
                 b'data/native/root-ignore-policy',b'.gitignore')
        relaxed=(b'',b'data',b'data/native',self.p.legacy)
        while todo:
            path=todo.pop()
            with self.p._root() as root:
                fd=self.p._directory(root,path) if path else os.dup(root)
                try:
                    info=os.fstat(fd)
                    if True:
                        result.append([encode_path(path) if path else '',identity(info) if path in relaxed else _stat(info)])
                    for name in sorted(os.fsencode(n) for n in os.listdir(fd)):
                        child=path+b'/'+name if path else name
                        if any(beneath(child,p) for p in omitted):continue
                        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):todo.append(child)
                        else:result.append([encode_path(child),_stat(info)])
                        if len(result)>MAX_ROWS:raise Blocked('preservation-resource-limit')
                finally:os.close(fd)
        existing={decode_path(row['path']) for row in self.p.read()['observation']['native']}
        for path in self._live_scope():
            info=self._info(path)
            if info is None and path in existing:raise Blocked('missing-preservation-path')
            if info is not None:
                result.append([encode_path(path),identity(info) if stat.S_ISDIR(info.st_mode) else _stat(info)])
        return sorted(result)

    def _live_scope(self):
        accepted=self.p.read()
        if self._journal is not None and accepted['digest']!=self._journal['candidate']:
            raise Blocked('candidate-cas-conflict')
        observation=accepted['observation']
        if observation['binding']['mappings']!=[[encode_path(a),encode_path(b)] for a,b in self.p.mappings]:
            raise Blocked('transition-mapping-drift')
        paths={decode_path(row['destination']) for row in observation['rows']
               if row['disposition'] in ('mapped','required-parent') and row['destination'] is not None
               and decode_path(row['destination']) not in self.p.policy.repository_files}
        paths.update(decode_path(pair[1]) for pair in observation['binding']['mappings']
                     if decode_path(pair[1]) not in self.p.policy.repository_files)
        return {b'/'.join(path.split(b'/')[:i]) for path in paths for i in range(1,len(path.split(b'/'))+1)}

    def _preserved(self, rows):
        # Apply the same projection to old full-live schema-1 journals and fresh
        # scoped snapshots. Absence remains absence: a newly created mapped path
        # appears only in the current side and therefore refuses.
        scope=self._live_scope();result=[];seen=set()
        accepted=self.p.read()['observation']
        expected={row['path'] for row in accepted['native'] if decode_path(row['path']) in scope}
        for path,descriptor in rows:
            raw=decode_path(path)
            if beneath(raw,self.p.legacy) or raw in scope:
                if path in seen:raise Blocked('duplicate-preservation-path')
                seen.add(path)
                if raw in scope and stat.S_ISDIR(descriptor[2]) and len(descriptor)==9:
                    descriptor=descriptor[:3]+descriptor[4:6]
                result.append([path,descriptor])
        # Original schema-1 snapshots began at each mapping target, so their
        # above-target anchors were omitted. Recover only those anchors from
        # immutable accepted evidence, never from the mutable current tree.
        targets=[decode_path(pair[1]) for pair in accepted['binding']['mappings']]
        above={b'/'.join(target.split(b'/')[:i]) for target in targets
               for i in range(1,len(target.split(b'/')))}
        native={row['path']:row['object']['identity'] for row in accepted['native']}
        for path in expected-seen:
            if decode_path(path) not in above:raise Blocked('missing-preservation-path')
            descriptor=native[path]
            if not stat.S_ISDIR(descriptor[2]):raise Blocked('invalid-preservation-anchor')
            result.append([path,descriptor[:3]+descriptor[4:6]])
        return sorted(result)

    def _check_protected(self):
        if self._preserved(self._protected())!=self._preserved(self._journal['protected']):
            raise Blocked('nonservice-descriptor-drift')

    def _runtime(self):
        with self.p._root() as root:
            return dict(app=self.p.app, manifest=self.p.manifest, policy=self.p.policy.digest,
                        root=identity(os.fstat(root)),data=identity(self._info(b'data')),
                        legacy=identity(self._info(self.p.legacy)),private=self.p._private(root),
                        operations=self.p._operations(root),
                        mounts=[repr(m) for m in (self.p._mounts() or self.p.binding.mounts)])

    def _binding(self, operation, action, candidate):
        return _digest(dict(operation=operation, action=action, candidate=candidate,
                            runtime=self._runtime()))

    def confirm(self, *, candidate, operation, action, approved):
        if approved is not True or action not in ('start', 'resume') or type(operation) is not str or not re.fullmatch('[0-9a-f]{32}', operation):
            raise Blocked('explicit-transition-confirmation-required')
        if action == 'start':
            if self.p.revalidate() != 'current': raise Blocked('current-candidate-required')
            current = self.p.read()
            if current['digest'] != candidate or current['observation']['blockers']:
                raise Blocked('candidate-conflict-or-blockers')
        else:
            current = self.read()
            if current is not None and current['phase']=='bootstrap-pending':raise Blocked('bootstrap-start-confirmation-required')
            if current is None or (current['operation'], current['candidate']) != (operation, candidate):
                raise Blocked('addressed-transition-required')
            self._live_scope()
            if current['runtime'] != self._runtime(): raise Blocked('incompatible-transition-runtime')
        return Confirmation(self._issuer, self._binding(operation, action, candidate))

    def _consume(self, confirmation, operation, action, candidate):
        if (type(confirmation) is not Confirmation or confirmation.issuer is not self._issuer
                or confirmation.used or confirmation.binding != self._binding(operation, action, candidate)):
            raise Blocked('missing-stale-or-replayed-confirmation')
        confirmation.used = True

    def _validate(self, value):
        keys = 'schema operation candidate runtime source source_identity source_manifest protected checkout_mtime store_identity actions completed pending receipts recovery phase digest'.split()
        if type(value) is not dict or set(value) != set(keys): raise Blocked('invalid-transition-journal')
        if (type(value['schema']) is not int or value['schema'] != 1
                or type(value['operation']) is not str or type(value['candidate']) is not str
                or type(value['actions']) is not list or type(value['receipts']) is not dict
                or type(value['checkout_mtime']) is not int or value['checkout_mtime']<0
                or value['pending'] is not None and type(value['pending']) is not int or not re.fullmatch('[0-9a-f]{32}', value['operation'])
                or not re.fullmatch('[0-9a-f]{64}', value['candidate'])
                or value['source'] != encode_path(self.p.legacy+b'/.git')
                or value['digest'] != _digest({k:v for k,v in value.items() if k != 'digest'})
                or type(value['completed']) is not int or not 0 <= value['completed'] <= len(value['actions'])
                or len(value['actions']) > MAX_ROWS * 2 + 20
                or value['pending'] not in (None, value['completed'])
                or value['phase'] not in ('transition-pending', TERMINAL)
                or (value['phase'] == TERMINAL) != (value['completed'] == len(value['actions']))):
            raise Blocked('invalid-transition-state-grammar')
        rows=value['source_manifest']
        if type(rows) is not list or len(rows)>MAX_ROWS:raise Blocked('invalid-source-manifest')
        seen=set()
        for row in rows:
            if type(row) is not dict or set(row)!={'path','directory','identity','stat','size','digest'}:
                raise Blocked('invalid-source-manifest-row')
            path=decode_path(row['path'])
            if path in seen or type(row['directory']) is not bool:raise Blocked('invalid-source-manifest-path')
            seen.add(path)
            if (type(row['size']) is not int or not 0<=row['size']<=MAX_BYTES
                    or type(row['identity']) is not list or len(row['identity'])!=5
                    or type(row['stat']) is not list or len(row['stat'])!=9
                    or any(type(i) is not int or not 0<=i<2**64 for i in row['identity']+row['stat'])
                    or row['identity']!=row['stat'][:3]+row['stat'][4:6]
                    or (stat.S_ISDIR(row['stat'][2]) if row['directory'] else stat.S_ISREG(row['stat'][2])) is not True
                    or row['size']!=(0 if row['directory'] else row['stat'][6])
                    or row['directory'] and row['digest'] is not None
                    or not row['directory'] and (row['stat'][3]!=1 or type(row['digest']) is not str or not re.fullmatch('[0-9a-f]{64}',row['digest']))):
                raise Blocked('invalid-source-manifest-descriptor')
        if [decode_path(row['path']) for row in rows]!=sorted(seen):raise Blocked('noncanonical-source-manifest')
        if (type(value['runtime']) is not dict or set(value['runtime'])!={'app','manifest','policy','root','data','legacy','private','operations','mounts'}
                or type(value['source_identity']) is not list or len(value['source_identity'])!=5
                or type(value['actions']) is not list or type(value['receipts']) is not dict
                or type(value['protected']) is not list or len(value['protected'])>MAX_ROWS
                or type(value['recovery']) is not dict or set(value['recovery'])!={'format','refs','index_oids','verified'}
                or value['recovery']['format'] not in ('sha1','sha256')
                or type(value['recovery']['verified']) is not bool):raise Blocked('invalid-transition-binding')
        for receipt in value['receipts'].values():
            if type(receipt) is not list or len(receipt)!=5 or any(type(i) is not int or not 0<=i<2**64 for i in receipt):
                raise Blocked('invalid-transition-inode-receipt')
        for row in value['protected']:
            if type(row) is not list or len(row)!=2 or type(row[1]) is not list or len(row[1]) not in (5,9):
                raise Blocked('invalid-preservation-projection')
            decode_path(row[0])
            if any(type(i) is not int or i<0 for i in row[1]):raise Blocked('invalid-preservation-projection')
        if value['store_identity'] is not None and (type(value['store_identity']) is not list or len(value['store_identity'])!=5 or any(type(i) is not int or i<0 for i in value['store_identity'])):
            raise Blocked('invalid-transition-store-identity')
        expected = self._actions(value['operation'], value['source_manifest'])
        if value['actions'] != expected: raise Blocked('forged-transition-actions')
        if set(value['receipts']) != {str(n) for n in range(value['completed'])}:
            raise Blocked('invalid-transition-receipt-sequence')
        verify=next(i for i,a in enumerate(expected) if a['kind']=='verify')
        if value['recovery']['verified'] != (value['completed']>verify):raise Blocked('invalid-recovery-receipt-phase')
        return value

    def read(self):
        # Reading never creates storage, repairs a temp, or invokes Git.
        if self._info(STORE) is None: return None
        info = self._info(STORE)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Blocked('unsafe-transition-store')
        with self.p._root() as root:
            fd=self.p._directory(root,STORE)
            try:
                names=[os.fsencode(name) for name in os.listdir(fd)]
                temps=[name for name in names if name==b'journal.pending']
                for name in names:
                    if name in (b'journal.json',b'bootstrap.id') or name in temps:continue
                    if not re.fullmatch(rb'[0-9a-f]{32}',name):raise Blocked('foreign-transition-store-entry')
                for name in temps:
                    info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>JOURNAL_LIMIT):
                        raise Blocked('unsafe-incomplete-journal')
                if b'journal.json' not in names and any(name not in temps and name!=b'bootstrap.id' for name in names):
                    raise Blocked('unowned-transition-bootstrap')
            finally:os.close(fd)
        path = STORE+b'/journal.json'
        if self._info(path) is None:
            marker=self._info(STORE+b'/bootstrap.id')
            if marker is None:
                if temps:raise Blocked('journal-fragment-without-bootstrap-identity')
                return None
            if (not stat.S_ISREG(marker.st_mode) or marker.st_nlink!=1 or marker.st_uid!=os.geteuid()
                    or stat.S_IMODE(marker.st_mode)!=0o600 or marker.st_size>32):
                raise Blocked('unsafe-bootstrap-identity')
            raw=self._read(STORE+b'/bootstrap.id',32)
            if not re.fullmatch(rb'[0-9a-f]{0,32}',raw):raise Blocked('foreign-bootstrap-identity')
            if len(raw)!=32 and temps:raise Blocked('journal-fragment-with-incomplete-bootstrap')
            candidate=self.p.read()
            return dict(schema=1,phase='bootstrap-pending',operation=raw.decode() if len(raw)==32 else None,
                        candidate=candidate['digest'] if candidate else None)
        info = self._info(path)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise Blocked('unsafe-transition-journal')
        try:
            value = json.loads(self._read(path, JOURNAL_LIMIT), object_pairs_hook=_unique,
                               parse_constant=lambda value:(_ for _ in ()).throw(Blocked('nonfinite-transition-json')))
            result=self._validate(value)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise Blocked('invalid-transition-json-or-schema') from None
        marker=self._info(STORE+b'/bootstrap.id')
        if (marker is None or marker.st_uid!=os.geteuid() or stat.S_IMODE(marker.st_mode)!=0o600
                or self._read(STORE+b'/bootstrap.id',32)!=result['operation'].encode()):
            raise Blocked('bootstrap-identity-changed')
        if result['store_identity']!=identity(self._info(STORE)):raise Blocked('transition-store-replaced')
        if any(re.fullmatch(rb'[0-9a-f]{32}',name) and name!=result['operation'].encode() for name in names):
            raise Blocked('foreign-transition-operation')
        return result

    def _bootstrap(self, operation):
        path=STORE+b'/bootstrap.id';payload=operation.encode()
        with self._parent(path) as (parent,name):
            info=self._info(path)
            if info is not None:
                raw=self._read(path,32)
                if not re.fullmatch(rb'[0-9a-f]{0,32}',raw):raise Blocked('foreign-bootstrap-identity')
                if len(raw)==32:
                    if raw!=payload:raise Blocked('addressed-bootstrap-required')
                else:
                    # No initial canonical or payload can precede a durable full
                    # marker. This narrowly qualified no-action prefix can be
                    # settled only by a freshly confirmed explicit start.
                    if self._info(STORE+b'/journal.json') or self._info(STORE+b'/journal.pending'):
                        raise Blocked('ambiguous-incomplete-bootstrap')
                    if _stat(os.stat(name,dir_fd=parent,follow_symlinks=False))!=_stat(info):
                        raise Blocked('bootstrap-identity-replaced')
                    self._effect('bootstrap-prefix-unlink',path,lambda:os.unlink(name,dir_fd=parent))
                    self._effect('bootstrap-prefix-parent-fsync',STORE,lambda:os.fsync(parent))
                    info=None
            if info is None:
                fd=self._effect('bootstrap-open-exclusive',path,lambda:os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent))
                try:
                    view=memoryview(payload)
                    while view:
                        count=self._effect('bootstrap-write',path,lambda:os.write(fd,view))
                        if count<=0:raise OSError('short-bootstrap-write')
                        view=view[count:]
                    self._effect('bootstrap-file-fsync',path,lambda:os.fsync(fd))
                finally:os.close(fd)
            else:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                try:
                    if _stat(os.fstat(fd))!=_stat(info):raise Blocked('bootstrap-identity-replaced')
                    self._effect('bootstrap-file-fsync',path,lambda:os.fsync(fd))
                finally:os.close(fd)
            self._effect('bootstrap-parent-fsync',STORE,lambda:os.fsync(parent))

    def _save(self):
        value = self._journal
        value['digest'] = _digest({k:v for k,v in value.items() if k != 'digest'})
        self._validate(value)
        raw = _json(value)
        if len(raw) > JOURNAL_LIMIT: raise Blocked('transition-journal-limit')
        path = STORE+b'/journal.pending'
        with self._parent(path) as (parent, name):
            previous=self._info(path)
            if previous is not None:
                # A single fixed ephemeral address belongs to this publication
                # protocol. Only the exact intended payload prefix may be
                # settled by an explicit worker; read never removes fragments.
                if (previous.st_uid!=os.geteuid() or stat.S_IMODE(previous.st_mode)!=0o600
                        or not raw.startswith(self._read(path,JOURNAL_LIMIT))):
                    raise Blocked('foreign-journal-publication-fragment')
                if _stat(os.stat(name,dir_fd=parent,follow_symlinks=False))!=_stat(previous):
                    raise Blocked('journal-fragment-replaced')
                self._effect('journal-fragment-unlink',path,lambda:os.unlink(name,dir_fd=parent))
                self._effect('journal-fragment-parent-fsync',STORE,lambda:os.fsync(parent))
            fd = self._effect('journal-open-exclusive', path, lambda:os.open(name, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600, dir_fd=parent))
            try:
                view = memoryview(raw)
                while view:
                    count = self._effect('journal-write',path,lambda:os.write(fd,view[:CHUNK]))
                    if count <= 0: raise OSError('short-journal-write')
                    view = view[count:]
                self._effect('journal-file-fsync',path,lambda:os.fsync(fd))
            finally: os.close(fd)
            self._effect('journal-replace',path,lambda:os.replace(name,b'journal.json',src_dir_fd=parent,dst_dir_fd=parent))
            try:self._effect('journal-parent-fsync',STORE,lambda:os.fsync(parent))
            except OSError as exc:raise PersistenceUncertain('transition-journal-publication-durability-uncertain') from exc

    def _mkdir(self, path):
        info = self._info(path)
        if info is None:
            with self._parent(path) as (parent,name):
                self._effect('mkdir',path,lambda:os.mkdir(name,0o700,dir_fd=parent))
                self._effect('directory-parent-fsync',path,lambda:os.fsync(parent))
            info = self._info(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Blocked('foreign-transition-directory')
        with self.p._root() as root:
            fd=self.p._directory(root,path)
            try:self._effect('directory-fsync',path,lambda:os.fsync(fd))
            finally:os.close(fd)
        with self._parent(path) as (parent,name):
            self._effect('directory-parent-fsync',path,lambda:os.fsync(parent))
        return identity(info)

    def _actions(self, operation, rows):
        prefix = STORE+b'/'+operation.encode()
        actions = [dict(kind='directory', path=encode_path(prefix)),
                   dict(kind='directory', path=encode_path(prefix+b'/backup')),
                   dict(kind='directory', path=encode_path(prefix+b'/native'))]
        for role in ('backup','native'):
            for row in rows:
                rel = decode_path(row['path'])
                if role == 'native' and not (rel in (b'HEAD',b'packed-refs') or rel.split(b'/')[0] in (b'objects',b'refs',b'logs')): continue
                actions.append(dict(kind='directory' if row['directory'] else 'copy',
                                    path=encode_path(prefix+b'/'+role.encode()+b'/'+rel), source=row['path']))
        actions += [dict(kind='config',path=encode_path(prefix+b'/native/config')),
                    dict(kind='verify',path=encode_path(prefix+b'/backup')),
                    dict(kind='retire',path=encode_path(prefix+b'/retired.git')),
                    dict(kind='checkout-mtime',path=encode_path(self.p.legacy)),
                    dict(kind='directory',path=encode_path(b'data/native')),
                    dict(kind='publish',path=encode_path(b'data/native/repository.git')),
                    dict(kind='directory',path=encode_path(b'data/native/repository-files')),
                    dict(kind='policy',path=encode_path(b'data/native/root-ignore-policy')),
                    dict(kind='policy',path=encode_path(b'.gitignore'))]
        return actions

    def _config(self):
        fmt = self._journal['recovery']['format']
        return (b'[core]\n\trepositoryformatversion = '+(b'1' if fmt=='sha256' else b'0')+
                b'\n\tbare = false\n\tlogallrefupdates = true\n'+
                (b'[extensions]\n\tobjectformat = sha256\n' if fmt=='sha256' else b''))

    def _file(self, path, payload):
        # Pending expected-absent intent and exact prefix allow cooperative crash
        # reconciliation, including death between exclusive creation and receipt.
        with self._parent(path) as (parent,name):
            info = self._info(path)
            if info is None:
                fd = self._effect('open-exclusive',path,lambda:os.open(name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=parent))
                offset = 0
            else:
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise Blocked('foreign-transition-output')
                actual = self._read(path)
                if not payload.startswith(actual): raise Blocked('foreign-or-corrupt-transition-prefix')
                fd = os.open(name,os.O_WRONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                if identity(os.fstat(fd)) != identity(info): os.close(fd);raise Blocked('changed-transition-output')
                offset = len(actual);os.lseek(fd,offset,os.SEEK_SET)
            try:
                while offset < len(payload):
                    count = self._effect('write',path,lambda:os.write(fd,payload[offset:offset+CHUNK]))
                    if count <= 0: raise OSError('short-transition-write')
                    offset += count
                self._effect('file-fsync',path,lambda:os.fsync(fd))
                receipt = identity(os.fstat(fd))
            finally: os.close(fd)
            self._effect('file-parent-fsync',path,lambda:os.fsync(parent))
        if self._read(path) != payload: raise Blocked('transition-readback-mismatch')
        return receipt

    def _copy(self, path, source, row):
        """Stream exact source bytes and reconcile an owned expected prefix."""
        with self._parent(source) as (sp,sn),self._parent(path) as (dp,dn):
            before=os.stat(sn,dir_fd=sp,follow_symlinks=False)
            if _stat(before)!=row['stat']:raise Blocked('copy-source-drift')
            src=os.open(sn,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=sp)
            dest=None
            try:
                if _stat(os.fstat(src))!=row['stat']:raise Blocked('copy-source-replaced')
                info=self._info(path)
                if info is None:
                    dest=self._effect('open-exclusive',path,lambda:os.open(dn,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dp))
                    prefix=0
                else:
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>row['size']):
                        raise Blocked('foreign-transition-copy')
                    dest=os.open(dn,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dp)
                    if _stat(info)!=_stat(os.fstat(dest)):raise Blocked('copy-target-replaced')
                    prefix=info.st_size
                remaining=prefix;digest=hashlib.sha256()
                while remaining:
                    chunk=os.read(src,min(CHUNK,remaining));actual=os.read(dest,len(chunk))
                    if not chunk or actual!=chunk:raise Blocked('foreign-copy-prefix')
                    digest.update(chunk);remaining-=len(chunk)
                total=prefix
                while True:
                    chunk=os.read(src,CHUNK)
                    if not chunk:break
                    digest.update(chunk);total+=len(chunk)
                    view=memoryview(chunk)
                    while view:
                        count=self._effect('write',path,lambda:os.write(dest,view))
                        if count<=0:raise OSError('short-transition-copy-write')
                        view=view[count:]
                if total!=row['size'] or digest.hexdigest()!=row['digest'] or _stat(os.fstat(src))!=row['stat']:
                    raise Blocked('copy-source-content-drift')
                self._effect('file-fsync',path,lambda:os.fsync(dest))
                receipt=identity(os.fstat(dest))
            finally:
                os.close(src)
                if dest is not None:os.close(dest)
            self._effect('file-parent-fsync',path,lambda:os.fsync(dp))
        if self._hash(path)!=row['digest']:raise Blocked('copy-readback-mismatch')
        return receipt

    def _git(self, base, *args):
        env = dict(PATH='/usr/bin:/bin',HOME='/nonexistent',LC_ALL='C',GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null',GIT_OPTIONAL_LOCKS='0',
                   GIT_NO_LAZY_FETCH='1',GIT_NO_REPLACE_OBJECTS='1',GIT_ALLOW_PROTOCOL='',GIT_TERMINAL_PROMPT='0')
        command=['/usr/bin/git','--no-pager','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
                 '-c','maintenance.auto=false','-c','gc.auto=0','--git-dir='+os.fsdecode(self.root+b'/'+base),
                 '--work-tree='+os.fsdecode(self.root),*args]
        process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL,start_new_session=True)
        selector=selectors.DefaultSelector();output=[];errors=[];total=0
        selector.register(process.stdout,selectors.EVENT_READ,output)
        selector.register(process.stderr,selectors.EVENT_READ,errors)
        deadline=time.monotonic()+10
        try:
            while selector.get_map():
                if time.monotonic()>=deadline:raise Blocked('transition-git-verification-timeout')
                for key,_ in selector.select(min(.1,max(.001,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),CHUNK)
                    if not data:selector.unregister(key.fileobj);continue
                    total+=len(data)
                    if total>MAX_CONTROL:raise Blocked('transition-git-verification-output-limit')
                    key.data.append(data)
            process.wait(timeout=max(.01,deadline-time.monotonic()))
            if process.returncode:raise Blocked('transition-git-verification-failed')
            return b''.join(output)
        finally:
            selector.close()
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
            process.stdout.close();process.stderr.close()

    def _check_runtime(self):
        self._live_scope()
        if self._runtime() != self._journal['runtime']: raise Blocked('transition-runtime-drift')
        if self.p.read()['digest'] != self._journal['candidate']: raise Blocked('candidate-cas-conflict')

    def _verify(self):
        state=self._journal;prefix=STORE+b'/'+state['operation'].encode()
        source=self.p.legacy+b'/.git'
        original=self._manifest(source)
        if original != state['source_manifest'] or identity(self._info(source)) != state['source_identity']:
            raise Blocked('metadata-source-drift')
        backup=self._manifest(prefix+b'/backup')
        if self._contents(backup) != self._contents(original): raise Blocked('incomplete-recovery-backup')
        # The old config is previously allowlisted by Preparation. Git reads the
        # independent backup explicitly; no original metadata is consulted.
        native_expected=[r for r in original if decode_path(r['path']) in (b'HEAD',b'packed-refs') or decode_path(r['path']).split(b'/')[0] in (b'objects',b'refs',b'logs')]
        native_actual=[r for r in self._manifest(prefix+b'/native') if decode_path(r['path'])!=b'config']
        if self._contents(native_expected)!=self._contents(native_actual):raise Blocked('native-image-content-mismatch')
        for role in ('backup','native'):
            base=prefix+b'/'+role.encode()
            if self._git(base,'rev-parse','--show-object-format').strip().decode()!=state['recovery']['format']:
                raise Blocked('recovery-object-format-mismatch')
            if self._git(base,'for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)')!=base64.b64decode(state['recovery']['refs']):
                raise Blocked('recovery-refs-mismatch')
            self._git(base,'fsck','--full','--no-progress')
            for oid in state['recovery']['index_oids']:
                self._git(base,'cat-file','-e',oid)
        state['recovery']['verified']=True
        self._check_runtime()
        return identity(self._info(prefix+b'/backup'))

    def _rename(self, source, target, expected):
        before,after=self._info(source),self._info(target)
        if before is None:
            if after is None or identity(after)!=expected: raise Blocked('ambiguous-transition-rename')
        else:
            if identity(before)!=expected or after is not None: raise Blocked('transition-rename-preimage-conflict')
            with self._parent(source) as (left,name),self._parent(target) as (right,dest):
                if os.fstat(left).st_dev!=os.fstat(right).st_dev: raise Blocked('cross-filesystem-transition')
                if identity(os.stat(name,dir_fd=left,follow_symlinks=False))!=expected: raise Blocked('transition-source-replaced')
                self._effect('rename-no-replace',target,lambda:_rename_noreplace(left,name,right,dest))
                self._effect('rename-source-parent-fsync',source,lambda:os.fsync(left))
                self._effect('rename-target-parent-fsync',target,lambda:os.fsync(right))
        with self._parent(source) as (left,name),self._parent(target) as (right,dest):
            self._effect('rename-source-parent-fsync',source,lambda:os.fsync(left))
            self._effect('rename-target-parent-fsync',target,lambda:os.fsync(right))
        return expected

    def _check_receipts(self):
        state=self._journal;prefix=STORE+b'/'+state['operation'].encode()
        native=prefix+b'/native'
        published=self._info(native) is None and self._info(b'data/native/repository.git') is not None
        manifest={row['path']:row for row in state['source_manifest']}
        for index in range(state['completed']):
            action=state['actions'][index];kind=action['kind'];path=decode_path(action['path'])
            if kind=='verify':continue
            if published and beneath(path,native):path=b'data/native/repository.git'+path[len(native):]
            info=self._info(path)
            if info is None or identity(info)!=state['receipts'][str(index)]:raise Blocked('issued-output-replaced')
            if kind=='copy' and self._hash(path)!=manifest[action['source']]['digest']:
                raise Blocked('issued-copy-content-drift')
            if kind=='config' and self._read(path)!=self._config():raise Blocked('issued-config-drift')
            if kind=='policy' and self._read(path)!=POLICY:raise Blocked('issued-policy-drift')
        retired=prefix+b'/retired.git'
        if self._info(retired) is not None and self._contents(self._manifest(retired))!=self._contents(state['source_manifest']):
            raise Blocked('retired-metadata-content-drift')
        if state['recovery']['verified']:
            # Verification is a frozen complete image, not a permit to adopt
            # safe-looking descendants added between explicit invocations.
            expected=[row for row in state['source_manifest']
                      if decode_path(row['path']) in (b'HEAD',b'packed-refs')
                      or decode_path(row['path']).split(b'/')[0] in (b'objects',b'refs',b'logs')]
            contents=self._contents(expected)
            config=self._config()
            contents.append((encode_path(b'config'),False,len(config),hashlib.sha256(config).hexdigest()))
            current=b'data/native/repository.git' if published else native
            if sorted(contents)!=sorted(self._contents(self._manifest(current))):
                raise Blocked('verified-native-image-membership-drift')
            backup=prefix+b'/backup'
            if self._contents(self._manifest(backup))!=self._contents(state['source_manifest']):
                raise Blocked('verified-recovery-drift')

    def completed_evidence(self, *, candidate, operation):
        """Explicit read-only reproof after the authorized topology change."""
        accepted = self.p.read()
        state = self.read()
        if (accepted is None or accepted['digest'] != candidate
                or accepted['state'] != 'prepared' or accepted['observation']['blockers']
                or state is None or state.get('phase') != TERMINAL
                or state['operation'] != operation or state['candidate'] != candidate
                or state['pending'] is not None or not state['recovery']['verified']):
            raise Blocked('completed-current-transition-required')
        self._journal = state
        self._check_runtime()
        self._check_protected()
        self._check_receipts()
        return state

    def _execute(self):
        state=self._journal;prefix=STORE+b'/'+state['operation'].encode()
        while state['completed']<len(state['actions']):
            index=state['completed'];action=state['actions'][index];path=decode_path(action['path']);kind=action['kind']
            self._check_runtime()
            if state['pending'] is None:
                state['pending']=index;self._save()
            if kind=='directory': receipt=self._mkdir(path)
            elif kind=='copy':
                src=self.p.legacy+b'/.git/'+decode_path(action['source'])
                row=next(r for r in state['source_manifest'] if r['path']==action['source'])
                receipt=self._copy(path,src,row)
            elif kind=='config': receipt=self._file(path,self._config())
            elif kind=='policy': receipt=self._file(path,POLICY)
            elif kind=='verify': receipt=self._verify()
            elif kind=='retire':
                self._check_protected()
                self._check_receipts()
                if not state['recovery']['verified']: raise Blocked('verified-recovery-required')
                current=self._info(self.p.legacy+b'/.git')
                if current is not None and self._manifest(self.p.legacy+b'/.git')!=state['source_manifest']:
                    raise Blocked('metadata-source-drift-before-retirement')
                receipt=self._rename(self.p.legacy+b'/.git',path,state['source_identity'])
            elif kind=='checkout-mtime':
                with self.p._root() as root:
                    fd=self.p._directory(root,self.p.legacy)
                    try:
                        info=os.fstat(fd)
                        self._effect('checkout-mtime',path,lambda:os.utime(fd,ns=(info.st_atime_ns,state['checkout_mtime'])))
                        self._effect('checkout-directory-fsync',path,lambda:os.fsync(fd))
                        receipt=identity(os.fstat(fd))
                    finally:os.close(fd)
            elif kind=='publish':
                self._check_receipts()
                stage=prefix+b'/native'
                stage_index=next(n for n,a in enumerate(state['actions']) if a['path']==encode_path(stage))
                receipt=self._rename(stage,path,state['receipts'][str(stage_index)])
            state['receipts'][str(index)]=receipt
            state['completed']+=1;state['pending']=None
            if state['completed']==len(state['actions']):state['phase']=TERMINAL
            self._save()
        return copy.deepcopy(state)

    def _journal_bound(self, state):
        initial=copy.deepcopy(state)
        initial.update(completed=0,pending=None,receipts={},phase='transition-pending',digest='0'*64)
        initial['recovery']['verified']=False
        # Every future receipt has five unsigned kernel attributes, bounded to
        # 64 bits, plus its decimal sequence key. Include pending/phase overlap.
        bound=len(_json(initial))+len(initial['actions'])*144+256
        if bound>JOURNAL_LIMIT:raise Blocked('complete-transition-journal-envelope-limit')
        return bound

    def _recipe(self, operation, rows, state=None):
        by_path={row['path']:row for row in rows}
        actions=self._actions(operation,rows)
        completed=state['completed'] if state else 0
        sizes=[];entries=0
        for index,action in enumerate(actions):
            if index<completed:continue
            kind=action['kind'];path=decode_path(action['path'])
            if kind=='directory':
                if self._info(path) is None:entries+=1
            elif kind in ('copy','config','policy'):
                size=(by_path[action['source']]['size'] if kind=='copy' else
                      256 if kind=='config' else len(POLICY))
                existing=self._info(path)
                if existing is not None:
                    if state is None or state['pending']!=index:raise Blocked('foreign-unissued-transition-output')
                    if kind=='copy':
                        row=by_path[action['source']]
                        source=self.p.legacy+b'/.git/'+decode_path(action['source'])
                        # Verify exact existing prefix before allowing allocation
                        # credit. No output is opened for writing here.
                        with self._parent(path) as (dp,dn),self._parent(source) as (sp,sn):
                            left=os.open(dn,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dp)
                            right=os.open(sn,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=sp)
                            try:
                                info=os.fstat(left)
                                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                                        or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>size
                                        or _stat(os.fstat(right))!=row['stat']):raise Blocked('unverified-partial-allocation')
                                while True:
                                    chunk=os.read(left,CHUNK)
                                    if not chunk:break
                                    if os.read(right,len(chunk))!=chunk:raise Blocked('foreign-partial-allocation')
                                size-=info.st_size
                            finally:os.close(left);os.close(right)
                    else:
                        expected=POLICY if kind=='policy' else self._config()
                        actual=self._read(path)
                        if not expected.startswith(actual):raise Blocked('foreign-control-allocation')
                        size-=len(actual)
                else:entries+=1
                sizes.append(size)
        temps=0;journal=0
        if self._info(STORE) is not None:
            with self.p._root() as root:
                fd=self.p._directory(root,STORE)
                try:
                    names=os.listdir(fd)
                    temps=int('journal.pending' in names)
                    journal=int('journal.json' in names)
                finally:os.close(fd)
        journal_bound=self._journal_bound(state or self._journal)
        marker=self._info(STORE+b'/bootstrap.id')
        if marker is None:
            sizes.append(32);entries+=1
        elif state is None and marker.st_size<32:
            sizes.append(32-marker.st_size)  # only unfinished bootstrap growth
        sizes.extend([journal_bound]*(3-temps-journal))
        entries+=5-temps-journal
        recipe=Recipe(self,operation,tuple(sizes),entries,sum(row['size'] for row in rows),journal_bound)
        self._recipes[id(recipe)]=recipe
        return recipe

    def start(self, *, candidate, operation, confirmation):
        self._consume(confirmation,operation,'start',candidate)
        if self.p.revalidate()!='current': raise Blocked('current-candidate-required')
        accepted=self.p.read()
        if accepted['digest']!=candidate or accepted['observation']['blockers']: raise Blocked('candidate-conflict-or-blockers')
        existing=self.read()
        if existing is not None and existing['phase']!='bootstrap-pending':raise Blocked('transition-already-present')
        if existing is not None and existing['operation'] not in (None,operation):raise Blocked('addressed-bootstrap-required')
        source=self.p.legacy+b'/.git';rows=self._manifest(source)
        git=accepted['observation']['git']
        self._journal=dict(schema=1,operation=operation,candidate=candidate,runtime=self._runtime(),
            source=encode_path(source),source_identity=identity(self._info(source)),source_manifest=rows,
            protected=self._protected(),checkout_mtime=self._info(self.p.legacy).st_mtime_ns,store_identity=None,
            actions=self._actions(operation,rows),completed=0,pending=None,receipts={},
            recovery=dict(format=git['format'],refs=git['refs'],index_oids=sorted({r['oid'] for r in git['index']}),verified=False),
            phase='transition-pending',digest='')
        self._journal['digest']=_digest({k:v for k,v in self._journal.items() if k!='digest'})
        self._validate(self._journal)
        recipe=self._recipe(operation,rows)
        with self.p.disk.migration_transition_guard(recipe,self._effect):
            if self.p.read()['digest']!=candidate or _digest(_stability(self.p.observe()))!=accepted['input_digest']:
                raise Blocked('candidate-drift-before-transition')
            if self._manifest(source)!=rows: raise Blocked('source-drift-before-transition')
            for path in (b'data/native/repository.git',b'data/native/repository-files',b'data/native/root-ignore-policy',b'.git',b'.gitignore'):
                if self._info(path) is not None: raise Blocked('occupied-transition-anchor')
            native=self._info(b'data/native')
            if native is not None and (not stat.S_ISDIR(native.st_mode) or native.st_uid!=os.geteuid() or stat.S_IMODE(native.st_mode)!=0o700):
                raise Blocked('unsafe-existing-native-parent')
            if self._info(self.p.legacy).st_dev!=self._info(b'data').st_dev:
                raise Blocked('cross-filesystem-transition')
            self._journal['store_identity']=self._mkdir(STORE)
            with self._parent(STORE) as (parent,name):
                fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    existing=self.read()
                    if existing is not None and existing['phase']!='bootstrap-pending':raise Blocked('transition-already-present')
                    if existing is not None and existing['operation'] not in (None,operation):raise Blocked('addressed-bootstrap-required')
                    self._bootstrap(operation)
                    self._save()
                    return self._execute()
                finally:os.close(fd)

    def resume(self, *, operation, confirmation):
        state=self.read()
        if state is not None and state['phase']=='bootstrap-pending':raise Blocked('bootstrap-start-confirmation-required')
        if state is None or state['operation']!=operation:raise Blocked('addressed-transition-required')
        self._consume(confirmation,operation,'resume',state['candidate'])
        self._journal=state
        self._check_receipts()
        recipe=self._recipe(operation,state['source_manifest'],state)
        with self.p.disk.migration_transition_guard(recipe,self._effect):
            with self._parent(STORE) as (parent,name):
                fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    self._journal=self.read()
                    if self._journal!=state:raise Blocked('transition-cas-conflict')
                    self._check_protected()
                    self._check_receipts()
                    return self._execute()
                finally:os.close(fd)
