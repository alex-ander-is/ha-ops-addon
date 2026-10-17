"""Dormant explicit recovery points. No production caller or automatic recovery.

A generation is immutable; only its bounded journal is replaced. The pointer
is published after all independent preimages and both-direction staging inodes
are durable. Namespace effects therefore always have an identifiable postimage.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import uuid

from native_inventory import Blocked, path_bytes, linux_mounts
from native_metadata import Binding, _opened, ancestors, MAX_ID
from native_disk import NativeDisk, recovery_project, recovery_resume

U64 = 2**64 - 1


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                       allow_nan=False) + '\n').encode('ascii')


def parse(raw):
    def pairs(rows):
        obj = {}
        for k, v in rows:
            if k in obj: raise Blocked('duplicate-recovery-key')
            obj[k] = v
        return obj
    try:
        return json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Blocked('nonfinite-recovery-number')))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise Blocked('invalid-recovery-json') from exc


def _fields(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        raise Blocked('invalid-recovery-fields')


def _uint(value, maximum=U64):
    if type(value) is not int or not 0 <= value <= maximum: raise Blocked('invalid-recovery-integer')


def _key(path): return base64.b64encode(path).decode('ascii')


def _path(key):
    try: path = base64.b64decode(key, validate=True)
    except (ValueError, TypeError) as exc: raise Blocked('invalid-recovery-path') from exc
    if _key(path) != key: raise Blocked('noncanonical-recovery-path')
    return path_bytes(path)


def _digest(raw): return hashlib.sha256(raw).hexdigest()


def _id(info): return [info.st_dev, info.st_ino]


@dataclass(frozen=True)
class File:
    data: bytes
    uid: int
    gid: int
    mode: int

    def __post_init__(self):
        object.__setattr__(self, 'data', bytes(self.data))
        _uint(self.uid, MAX_ID); _uint(self.gid, MAX_ID); _uint(self.mode, 0o7777)


@dataclass(frozen=True)
class Directory:
    uid: int
    gid: int
    mode: int

    def __post_init__(self):
        _uint(self.uid, MAX_ID); _uint(self.gid, MAX_ID); _uint(self.mode, 0o7777)


def _semantic(state):
    return None if state is None else {k: v for k, v in state.items() if k not in ('dev', 'ino')}


def _state(info, data=None):
    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
    if kind == 'file' and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise Blocked('unsafe-recovery-object')
    return dict(kind=kind, uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode),
                dev=info.st_dev, ino=info.st_ino,
                size=len(data) if data is not None else 0,
                digest=_digest(data) if data is not None else None)


def _validate_state(value, *, semantic=False):
    if value is None: return
    keys = 'kind uid gid mode size digest' + ('' if semantic else ' dev ino')
    _fields(value, keys)
    for key in ('uid', 'gid'): _uint(value[key], MAX_ID)
    _uint(value['mode'], 0o7777); _uint(value['size'])
    if not semantic:
        _uint(value['dev']); _uint(value['ino'])
    if value['kind'] == 'directory':
        if value['size'] != 0 or value['digest'] is not None: raise Blocked('invalid-directory-recovery-state')
    elif value['kind'] != 'file' or not isinstance(value['digest'], str) or not re.fullmatch('[0-9a-f]{64}', value['digest']):
        raise Blocked('invalid-file-recovery-state')


@dataclass(frozen=True)
class Payload:
    data: bytes | None
    source: bytes | None
    state: bytes

    @property
    def size(self): return len(self.data) if self.data is not None else parse(self.state)['size']
    @property
    def digest(self): return _digest(self.data) if self.data is not None else parse(self.state)['digest']


@dataclass(frozen=True, eq=False)
class Prepared:
    store: object
    manifest: bytes
    payloads: tuple
    prior: bytes | None

    def check(self):
        if self.store._issued.get(id(self)) is not self: raise Blocked('unissued-recovery-preparation')
        return parse(self.manifest)


class RecoveryStore:
    def __init__(self, binding, *, directory=b'data/native/recovery', disk=None, fault=None):
        if type(binding) is not Binding: raise Blocked('explicit-recovery-binding-required')
        self.binding = binding
        self.directory = path_bytes(directory)
        if not self.directory.startswith(b'data/native/recovery'): raise Blocked('private-recovery-directory-required')
        self.disk = disk or NativeDisk(binding.root)
        if self.disk.root != binding.root: raise Blocked('different-recovery-disk-root')
        self.fault = fault or (lambda _: None)
        self._issued = {}
        self._held_routes=None

    def _target_path(self, path):
        if self.binding.policy.reason(path) or path == b'.gitignore' or path in self.binding.policy.repository_files:
            raise Blocked('non-managed-recovery-target')
        return path

    def _observe(self, path, *, content=False):
        parts = path.split(b'/')
        for i in range(1, len(parts) + 1):
            prefix=b'/'.join(parts[:i]); parent=b'/'.join(parts[:i-1])
            with _opened(self.binding,parent,'directory') as (fd,_):
                try: info=os.stat(parts[i-1],dir_fd=fd,follow_symlinks=False)
                except FileNotFoundError:
                    try: probe=os.open(parts[i-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                    except FileNotFoundError: return (None,None) if content else None
                    else: os.close(probe); raise Blocked('changed-recovery-absence')
                if i<len(parts) and not stat.S_ISDIR(info.st_mode):
                    if stat.S_ISREG(info.st_mode): return (None,None) if content else None
                    raise Blocked('unsafe-recovery-route')
                self.binding.check(prefix,info,linux_mounts() if os.path.exists('/proc/self/mountinfo') else None)
        kind='directory' if stat.S_ISDIR(info.st_mode) else 'file'
        with _opened(self.binding,path,kind) as (fd,_):
            before=os.fstat(fd); h=hashlib.sha256(); size=0
            if kind=='file':
                while True:
                    chunk=os.read(fd,65536)
                    if not chunk: break
                    h.update(chunk); size+=len(chunk)
            after=os.fstat(fd)
            if self._stable(before)!=self._stable(after): raise Blocked('changed-recovery-capture')
            result=_state(after)
            if kind=='file': result.update(size=size,digest=h.hexdigest())
        payload=Payload(None,path,canonical(result)) if kind=='file' else None
        return (result,payload) if content else result

    def _emit(self,payload,descriptor):
        if payload.data is not None:
            source=None; chunks=(payload.data[i:i+65536] for i in range(0,len(payload.data),65536))
            self._emit_chunks(chunks,descriptor,payload.digest,payload.size)
        else:
            expected=parse(payload.state)
            with _opened(self.binding,payload.source,'file') as (fd,_):
                before=os.fstat(fd)
                def chunks():
                    remaining=expected['size']
                    while remaining:
                        chunk=os.read(fd,min(65536,remaining))
                        if not chunk: break
                        remaining-=len(chunk)
                        yield chunk
                    # Growth is a source conflict, never extra destination
                    # demand. The EOF probe is deliberately not yielded.
                    if os.read(fd,1): raise Blocked('changed-recovery-source')
                if _id(before)!=[expected['dev'],expected['ino']] or (before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode),before.st_size)!=(expected['uid'],expected['gid'],expected['mode'],expected['size']):
                    raise Blocked('changed-recovery-source')
                self._emit_chunks(chunks(),descriptor,expected['digest'],expected['size'])
                if self._stable(before)!=self._stable(os.fstat(fd)) or self._observe(payload.source)!=expected:
                    raise Blocked('changed-recovery-source')

    @staticmethod
    def _emit_chunks(chunks,descriptor,digest,size):
        h=hashlib.sha256(); total=0
        for chunk in chunks:
            if len(chunk)>size-total: raise Blocked('recovery-payload-exceeds-qualified-size')
            h.update(chunk); total+=len(chunk); view=memoryview(chunk)
            while view:
                n=os.write(descriptor,view)
                if n<=0: raise OSError('short-recovery-write')
                view=view[n:]
        if total!=size or h.hexdigest()!=digest: raise Blocked('changed-recovery-payload')

    def _verify_blob(self,relative,state):
        # Blob verification is streamed. Only bounded control JSON is loaded.
        path=self.directory+b'/'+relative
        actual=self._observe(path)
        if actual is None or actual['kind']!='file' or actual['size']!=state['size'] or actual['digest']!=state['digest'] or actual['uid']!=os.geteuid() or actual['mode']!=0o600:
            raise Blocked('corrupt-recovery-blob')

    def _private(self, relative):
        return self.binding.root + b'/' + self.directory + (b'/' + relative if relative else b'')

    @contextmanager
    def _directory(self, relative=b''):
        self._assert_held_routes()
        with _opened(self.binding, self.directory + (b'/' + relative if relative else b''), 'directory') as (fd, _):
            s = os.fstat(fd)
            if s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
                raise Blocked('unsafe-private-recovery-directory')
            yield fd

    @staticmethod
    def _stable(info):
        return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_uid,info.st_gid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

    def _assert_held_routes(self):
        if self._held_routes is not None:
            for path,state in self._held_routes.items():
                if self._observe(path)!=state: raise Blocked('changed-held-recovery-store-route')

    def _read(self, relative):
        parent, _, name = relative.rpartition(b'/')
        with self._directory(parent) as fd:
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            primary=None
            try:
                before = os.fstat(child)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
                    raise Blocked('unsafe-private-recovery-file')
                chunks = []
                while True:
                    chunk = os.read(child, 65536)
                    if not chunk: break
                    chunks.append(chunk)
                after = os.fstat(child)
                if self._stable(before) != self._stable(after) or self._stable(os.stat(name, dir_fd=fd, follow_symlinks=False)) != self._stable(after):
                    raise Blocked('changed-recovery-control')
                return b''.join(chunks)
            except BaseException as exc:
                primary=exc; raise
            finally:
                try: os.close(child)
                except BaseException as cleanup:
                    if primary is None: raise
                    primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('recovery-read-close:'+str(cleanup),)

    def _write(self,relative,raw,*,replace=True):
        parent,_,name=relative.rpartition(b'/')
        with self._directory(parent) as fd:
            temp=b'.write-'+uuid.uuid4().hex.encode(); child=None; identity=None; primary=None
            try:
                child=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                identity=_id(os.fstat(child))
                if callable(raw): raw=raw(identity)
                if type(raw) is Payload: self._emit(raw,child)
                else:
                    view=memoryview(raw)
                    while view:
                        n=os.write(child,view)
                        if n<=0: raise OSError('short-recovery-write')
                        view=view[n:]
                self.fault('control_written'); self.fault('control_written:'+os.fsdecode(relative)); os.fsync(child); self.fault('control_fsynced'); self.fault('control_fsynced:'+os.fsdecode(relative))
                os.close(child); child=None
                if not replace:
                    try: os.stat(name,dir_fd=fd,follow_symlinks=False)
                    except FileNotFoundError: pass
                    else: raise Blocked('existing-immutable-recovery-file')
                if _id(os.stat(temp,dir_fd=fd,follow_symlinks=False))!=identity: raise Blocked('substituted-recovery-control-temp')
                os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
                self.fault('control_replaced'); self.fault('control_replaced:'+os.fsdecode(relative)); os.fsync(fd); self.fault('control_durable'); self.fault('control_durable:'+os.fsdecode(relative))
            except BaseException as exc:
                primary=exc
                if identity is not None:
                    try:
                        info=os.stat(temp,dir_fd=fd,follow_symlinks=False)
                        if _id(info)!=identity: raise Blocked('foreign-recovery-control-temp')
                        os.unlink(temp,dir_fd=fd); os.fsync(fd)
                    except FileNotFoundError: pass
                    except BaseException as cleanup:
                        exc.cleanup_errors=tuple(getattr(exc,'cleanup_errors',()))+('control-temp:'+repr(relative+b'/'+temp)+':'+str(cleanup),)
                raise
            finally:
                if child is not None:
                    try: os.close(child)
                    except BaseException as cleanup:
                        if primary is None: raise
                        primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('control-close:'+str(cleanup),)
            return self._observe(self.directory+b'/'+relative)

    def _ensure(self):
        # Explicit mutating entry only. Every component is checked no-follow.
        prefix = b''
        for part in self.directory.split(b'/'):
            parent = prefix; prefix = prefix + b'/' + part if prefix else part
            with _opened(self.binding, parent, 'directory') as (fd, _):
                try: os.mkdir(part, 0o700, dir_fd=fd); os.fsync(fd)
                except FileExistsError: pass
            with _opened(self.binding, prefix, 'directory'): pass
        with self._directory() as fd:
            try:
                lock = os.open(b'lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            except FileExistsError: pass
            else: os.fsync(lock); os.close(lock); os.fsync(fd)
            try: key=os.open(b'authority.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            except FileExistsError: pass
            else:
                try:
                    value=os.urandom(32); view=memoryview(value)
                    while view:
                        n=os.write(key,view)
                        if n<=0: raise OSError('short-recovery-authority-write')
                        view=view[n:]
                    os.fsync(key)
                finally: os.close(key)
                os.fsync(fd)
        if len(self._read(b'authority.key'))!=32: raise Blocked('invalid-recovery-authority-key')

    @contextmanager
    def _lock(self):
        with self._directory() as parent:
            fd = os.open(b'lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
            previous=self._held_routes; primary=None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise Blocked('unsafe-recovery-lock')
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc: raise Blocked('recovery-busy') from exc
                if os.stat(b'lock', dir_fd=parent, follow_symlinks=False) != info: raise Blocked('replaced-recovery-lock')
                self._held_routes={p:self._observe(p) for p in sorted(set(ancestors((self.directory,)))|{self.directory,self.directory+b'/authority.key'})}
                yield
            except BaseException as exc:
                primary=exc; raise
            finally:
                self._held_routes=previous
                try: os.close(fd)
                except BaseException as cleanup:
                    if primary is None: raise
                    primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('recovery-lock-close:'+str(cleanup),)

    def _pointer(self):
        try: raw = self._read(b'active.json')
        except FileNotFoundError: return None
        p = parse(raw); _fields(p, 'schema generation manifest identity')
        if p['schema'] != 1 or type(p['schema']) is not int or not re.fullmatch('[0-9a-f]{32}', p['generation']) or not re.fullmatch('[0-9a-f]{64}', p['manifest']):
            raise Blocked('invalid-recovery-pointer')
        if type(p['identity']) is not list or len(p['identity'])!=2: raise Blocked('invalid-generation-identity')
        for value in p['identity']: _uint(value)
        return raw

    def _replaceable_point(self):
        found=self.discover()
        if found is None: raise Blocked('missing-prior-recovery-point')
        journal=found['journal']
        if journal['phase'] not in ('ready','applied','restored') or journal['pending'] is not None:
            raise Blocked('pending-recovery-point-cannot-be-replaced')

    def prepare(self, targets, *, operation_id=None, owner_id=None):
        if type(targets) is not dict or not targets: raise Blocked('explicit-recovery-targets-required')
        op, owner = operation_id or str(uuid.uuid4()), owner_id or str(uuid.uuid4())
        for value in (op, owner):
            if str(uuid.UUID(value)) != value: raise Blocked('invalid-recovery-operation-identity')
        explicit = {}
        for path, target in targets.items():
            if type(path) is not bytes: raise Blocked('byte-recovery-path-required')
            path = self._target_path(path_bytes(path))
            if target is not None and type(target) not in (File, Directory): raise Blocked('invalid-recovery-target')
            explicit[path] = target
        paths = set(explicit) | set(ancestors(explicit)); before = {}; payloads = {}
        for path in sorted(paths):
            value, raw = self._observe(path, content=True); before[_key(path)] = value
            if raw is not None: payloads[raw.digest]=raw
        after = {}
        for path in sorted(paths):
            old = before[_key(path)]
            target = explicit.get(path, 'parent')
            if target == 'parent':
                required=any(v is not None and child.startswith(path+b'/') for child,v in explicit.items())
                if not required:
                    after[_key(path)]=_semantic(old); continue
                if old is not None and old['kind']!='directory':
                    raise Blocked('unselected-file-parent-transition')
                # Only materialized descendants require new parent directories.
                target = Directory(old['uid'], old['gid'], old['mode']) if old and old['kind'] == 'directory' else Directory(os.geteuid(), os.getegid(), 0o755)
            if target is None: value = None
            elif type(target) is File:
                value = dict(kind='file', uid=target.uid, gid=target.gid, mode=target.mode,
                             size=len(target.data), digest=_digest(target.data))
                payloads[value['digest']]=Payload(target.data,None,b'')
            else: value = dict(kind='directory', uid=target.uid, gid=target.gid, mode=target.mode, size=0, digest=None)
            after[_key(path)] = value
        for key, value in after.items():
            path = _path(key)
            for ancestor in ancestors((path,)):
                a = after[_key(ancestor)]
                if value is not None and (a is None or a['kind'] != 'directory'): raise Blocked('conflicting-recovery-parent-target')
            old = before[key]
            if old and old['kind'] == 'directory' and (value is None or value['kind'] != 'directory'):
                with _opened(self.binding, path, 'directory') as (fd, _):
                    for name in os.listdir(fd):
                        child = path + b'/' + os.fsencode(name)
                        if child not in explicit or after[_key(child)] is not None:
                            raise Blocked('unselected-recovery-directory-child')
        prior = self._pointer()
        if prior is not None: self._replaceable_point()
        manifest = dict(schema=1, operation=op, owner=owner, generation=uuid.uuid4().hex,
                        root=_id(os.stat(self.binding.root, follow_symlinks=False))+[stat.S_IMODE(os.stat(self.binding.root,follow_symlinks=False).st_mode)], directory=_key(self.directory),
                        before=before, after=after, explicit=[_key(p) for p in sorted(explicit)], stages={}, pools={}, store_routes=None, binding_seal=None, cleanup=None, previous=parse(prior) if prior else None, forecast=None)
        prepared = Prepared(self, canonical(manifest), tuple(sorted(payloads.items())), prior)
        self._issued[id(prepared)] = prepared
        return prepared

    def _journal(self, manifest):
        return dict(schema=1, operation=manifest['operation'], owner=manifest['owner'], generation=manifest['generation'],
                    revision=0, phase='ready', current=manifest['before'], changed=[], pending=None, seal=None, identity=None, removed={})

    def _control_template(self, manifest):
        state = dict(kind='file', uid=MAX_ID, gid=MAX_ID, mode=0o7777, size=U64, digest='f'*64, dev=U64, ino=U64)
        journal = self._journal(manifest)
        journal.update(revision=U64, seal='f'*64, identity=[U64,U64],removed={d+':'+k:state for k in manifest['before'] for d in ('forward','rollback')}, phase='restoring', current={k: state for k in manifest['before']},
                       changed=list(manifest['before']), pending=dict(path=max(manifest['before'], key=len),
                       action='replace', direction='rollback', before=state, after=[state, state, state, state], stage='removed:rollback:'+max(manifest['before'],key=len)))
        # Includes persisted forecast and stage identities; bounds are derived
        # from the fixed schema and uint64 scalar encoding, never user budgets.
        m=dict(manifest,cleanup=dict(marker_identity=[U64,U64],generation_identity=[U64,U64],manifest_identity=[U64,U64],files={name:state for name in ['manifest.json','journal.json']+sorted({'blob-'+v['digest'] for states in (manifest['before'],manifest['after']) for v in states.values() if v and v['kind']=='file'})}),binding_seal='f'*64,store_routes={_key(p):state for p in sorted(set(ancestors((self.directory,)))|{self.directory,self.directory+b'/authority.key'})},stages={d+':'+k:dict(path=_key(self._pool_for(manifest,k)+b'/stage-'+b'f'*64),state=state) for k in manifest['before'] for d in ('forward','rollback')},pools={_key(self._pool_for(manifest,k)):[U64,U64] for k in manifest['before']})
        entries={v['path']:state for v in m['stages'].values()}
        entries.update({_key(self._tomb_path(manifest,k,d)):state for k in manifest['before'] for d in ('forward','rollback')})
        prep=dict(schema=1,generation=manifest['generation'],operation=manifest['operation'],owner=manifest['owner'],root=[U64,U64,0o7777],identity=[U64,U64],pools=m['pools'],entries=entries,
            files=m['cleanup']['files'],marker_identity=[U64,U64],seal='f'*64,blobs={name[5:]:dict(size=U64,digest='f'*64) for name in m['cleanup']['files'] if name.startswith('blob-')},manifest='f'*64)
        metadata=max((canonical(m),canonical(prep)),key=len)
        return canonical(journal), metadata

    def _pool_for(self,manifest,key):
        path=_path(key); parent=os.path.dirname(path)
        # Staging must live on the destination volume, outside directories the
        # operation may remove. The nearest stable existing ancestor qualifies.
        while parent:
            before=manifest['before'].get(_key(parent)); after=manifest['after'].get(_key(parent))
            if before and before['kind']=='directory' and after and after['kind']=='directory': break
            parent=os.path.dirname(parent)
        suffix=hashlib.sha256(parent).hexdigest()[:16].encode()
        return parent+(b'/' if parent else b'')+b'.ha-ops-recovery-'+manifest['generation'].encode()+b'-'+suffix

    def _allocations(self,prepared):
        manifest=prepared.check(); journal,metadata=self._control_template(manifest)
        outputs=[(self.directory+b'/'+manifest['generation'].encode()+b'/blob-'+h.encode(),payload.size,payload.digest,'recovery-copy') for h,payload in prepared.payloads]
        pools=set()
        for key in manifest['before']:
            pool=self._pool_for(manifest,key); pools.add(pool)
            for direction,state in (('forward',manifest['after'][key]),('rollback',_semantic(manifest['before'][key]))):
                if state is not None:
                    name=b'stage-'+hashlib.sha256((direction+key).encode()).hexdigest().encode()
                    outputs.append((pool+b'/'+name,state['size'],state['digest'] or _digest(b''),'recovery-stage'))
        for key in manifest['before']:
            for direction in ('forward','rollback'): outputs.append((self._tomb_path(manifest,key,direction),0,_digest(b''),'recovery-tombstone'))
        for pool in sorted(pools): outputs.append((pool+b'/entry',0,_digest(b''),'recovery-pool'))
        for label,raw in (('journal',journal),('manifest',metadata),('preparation',metadata),('pointer',canonical(dict(schema=1,generation='f'*32,manifest='f'*64,identity=[U64,U64])))):
            for _ in range(2): outputs.append((self.directory+b'/'+label.encode(),len(raw),_digest(raw),'recovery-control'))
        return tuple(outputs)

    def _tomb_path(self,m,key,direction):
        return self._pool_for(m,key)+b'/removed-'+hashlib.sha256((direction+key).encode()).hexdigest().encode()

    def _removed_receipt(self,m,p,*,allow_children=False):
        path=self._tomb_path(m,p['path'],p['direction'])
        receipt=self._observe(path)
        if receipt!=p['before']: raise Blocked('unowned-removal-absence-or-substituted-tombstone')
        if receipt['kind']=='directory' and not allow_children:
            with _opened(self.binding,path,'directory') as (fd,_):
                if os.listdir(fd): raise Blocked('removed-directory-gained-unselected-child')
        return receipt

    def _resource(self,m,j,stage):
        if stage in m['stages']: return m['stages'][stage]
        if type(stage) is not str or not stage.startswith('removed:'): raise Blocked('unknown-recovery-resource')
        _,direction,key=stage.split(':',2); receipt=j['removed'].get(direction+':'+key)
        if receipt is None: raise Blocked('unknown-removal-resource')
        return dict(path=_key(self._tomb_path(m,key,direction)),state=receipt)

    @contextmanager
    def _stage_directory(self,path,identity=None):
        with _opened(self.binding,path,'directory') as (fd,_):
            info=os.fstat(fd)
            if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700 or identity is not None and _id(info)!=identity:
                raise Blocked('changed-recovery-stage-pool')
            yield fd

    def install(self,prepared):
        manifest=prepared.check(); forecast=recovery_project(self.disk,prepared)
        self._ensure()
        forecast['seal']=hmac.new(self._read(b'authority.key'),canonical(forecast),hashlib.sha256).hexdigest()
        with self._lock():
            if self._pointer()!=prepared.prior: raise Blocked('stale-recovery-slot')
            # An unchanged pointer does not authorize retiring a point whose
            # signed journal advanced after prepare. Check under this same
            # stable lock before allocating any new generation resources.
            if prepared.prior is not None: self._replaceable_point()
            for key,before in manifest['before'].items():
                if self._observe(_path(key))!=before: raise Blocked('changed-recovery-preimage')
            manifest['forecast']=forecast
            manifest['store_routes']={_key(p):self._observe(p) for p in sorted(set(ancestors((self.directory,)))|{self.directory,self.directory+b'/authority.key'})}
            generation=manifest['generation'].encode()
            prep=dict(schema=1,generation=manifest['generation'],operation=manifest['operation'],owner=manifest['owner'],root=manifest['root'],identity=None,pools={},entries={},files={},marker_identity=None,seal=None,blobs={h:{'size':payload.size,'digest':h} for h,payload in prepared.payloads},manifest=None)
            self._marker_write(generation,prep,create=True)
            try:
                with self._directory() as fd:
                    os.mkdir(generation,0o700,dir_fd=fd); os.fsync(fd)
                    prep['identity']=_id(os.stat(generation,dir_fd=fd,follow_symlinks=False))
                self._marker_write(generation,prep); self.fault('generation_created')
                for digest,payload in prepared.payloads:
                    relative=generation+b'/blob-'+digest.encode()
                    receipt=self._write(relative,payload,replace=False)
                    prep['files']['blob-'+digest]=receipt
                    self._marker_write(generation,prep)
                    self._verify_blob(relative,dict(size=payload.size,digest=digest)); self.fault('blob_durable')
                for key in manifest['before']:
                    pool=self._pool_for(manifest,key); poolkey=_key(pool)
                    if poolkey not in manifest['pools']:
                        # Persist ownership intent before allocation. If process
                        # death precedes identity acknowledgment, explicit
                        # cleanup refuses that uncertain directory.
                        prep['pools'][poolkey]=None
                        self._marker_write(generation,prep)
                        parent,_,name=pool.rpartition(b'/')
                        with _opened(self.binding,parent,'directory') as (fd,_):
                            os.mkdir(name,0o700,dir_fd=fd)
                            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                            try: os.fchown(child,os.geteuid(),os.getegid()); os.fchmod(child,0o700); os.fsync(child)
                            finally: os.close(child)
                            os.fsync(fd)
                            identity=_id(os.stat(name,dir_fd=fd,follow_symlinks=False))
                        prep['pools'][poolkey]=identity; manifest['pools'][poolkey]=identity
                        self._marker_write(generation,prep); self.fault('pool_durable')
                    for direction,state in (('forward',manifest['after'][key]),('rollback',_semantic(manifest['before'][key]))):
                        if state is None: continue
                        name=b'stage-'+hashlib.sha256((direction+key).encode()).hexdigest().encode()
                        with self._stage_directory(pool,manifest['pools'][poolkey]) as fd:
                            if state['kind']=='directory': os.mkdir(name,0o700,dir_fd=fd)
                            else:
                                child=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                                primary=None; own=_id(os.fstat(child))
                                try:
                                    self._emit(dict(prepared.payloads)[state['digest']],child)
                                    os.fchown(child,state['uid'],state['gid']); os.fchmod(child,state['mode']); os.fsync(child)
                                except BaseException as exc:
                                    primary=exc
                                    try:
                                        if _id(os.stat(name,dir_fd=fd,follow_symlinks=False))!=own: raise Blocked('foreign-failed-recovery-stage')
                                        os.unlink(name,dir_fd=fd); os.fsync(fd)
                                    except BaseException as cleanup:
                                        exc.cleanup_errors=tuple(getattr(exc,'cleanup_errors',()))+('stage-cleanup:'+str(cleanup),)
                                    raise
                                finally:
                                    try: os.close(child)
                                    except BaseException as cleanup:
                                        if primary is None: raise
                                        primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('stage-close:'+str(cleanup),)
                            os.fsync(fd)
                        stagepath=pool+b'/'+name; stage_state=self._observe(stagepath)
                        manifest['stages'][direction+':'+key]={'path':_key(stagepath),'state':stage_state}
                        prep['entries'][_key(stagepath)]=stage_state
                        self._marker_write(generation,prep)
                        self.fault('stage_durable')
                manifest['cleanup']=dict(marker_identity=prep['marker_identity'],generation_identity=prep['identity'],manifest_identity=None,files=dict(prep['files']))
                def final_manifest(identity):
                    manifest['cleanup']['manifest_identity']=identity
                    manifest['binding_seal']=self._seal(manifest,'binding_seal')
                    return canonical(manifest)
                receipt=self._write(generation+b'/manifest.json',final_manifest,replace=False)
                prep['manifest']=receipt['digest']; prep['files']['manifest.json']=receipt
                self._marker_write(generation,prep)
                def first_journal(identity):
                    j=self._journal(manifest); j['identity']=identity
                    return canonical(self._signed_journal(j))
                receipt=self._write(generation+b'/journal.json',first_journal,replace=False)
                prep['files']['journal.json']=receipt
                self._marker_write(generation,prep)
                raw=canonical(manifest)
                self.fault('generation_durable')
                for key,before in manifest['before'].items():
                    if self._observe(_path(key))!=before: raise Blocked('changed-recovery-preimage-before-publication')
                pointer=canonical(dict(schema=1,generation=manifest['generation'],manifest=_digest(raw),identity=prep['identity']))
                self._write(b'active.json',pointer); self.fault('slot_durable')
                # Only after pointer and directory durability may old immutable
                # resources be reclaimed. Cleanup has its own explicit retry API.
                cleanup=self._cleanup_debt_locked(exclude=manifest['generation'])
                self._issued.pop(id(prepared),None)
                result=self.discover(); result['cleanup']=cleanup
                return result
            except BaseException as exc:
                exc.cleanup_errors=tuple(getattr(exc,'cleanup_errors',()))+('recovery-preparation-retained:'+os.fsdecode(self._private(b'preparation-'+generation+b'.json')),)
                raise

    def _marker_write(self,generation,prep,*,create=False,expected=None):
        name=b'preparation-'+generation+b'.json'
        with self._directory() as parent:
            flags=os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if create else 0)
            fd=os.open(name,flags,0o600,dir_fd=parent)
            primary=None
            try:
                info=os.fstat(fd); identity=_id(info)
                wanted=expected if expected is not None else prep['marker_identity']
                if not create and identity!=wanted: raise Blocked('substituted-recovery-marker')
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600: raise Blocked('unsafe-recovery-marker')
                prep['marker_identity']=identity; prep['seal']=self._seal(prep,'seal'); raw=canonical(prep)
                os.lseek(fd,0,os.SEEK_SET); view=memoryview(raw)
                while view:
                    n=os.write(fd,view)
                    if n<=0: raise OSError('short-recovery-marker-write')
                    view=view[n:]
                os.ftruncate(fd,len(raw)); self.fault('marker_written'); os.fsync(fd)
                if _id(os.stat(name,dir_fd=parent,follow_symlinks=False))!=identity: raise Blocked('changed-recovery-marker')
                os.fsync(parent)
            except BaseException as exc:
                primary=exc; raise
            finally:
                try: os.close(fd)
                except BaseException as cleanup:
                    if primary is None: raise
                    primary.cleanup_errors=tuple(getattr(primary,'cleanup_errors',()))+('recovery-marker-close:'+str(cleanup),)

    def _read_marker(self,generation):
        name=b'preparation-'+generation+b'.json'; prep=parse(self._read(name))
        _fields(prep,'schema generation operation owner root identity pools entries files marker_identity seal blobs manifest')
        actual=self._observe(self.directory+b'/'+name)
        if type(prep['seal']) is not str or not hmac.compare_digest(prep['seal'],self._seal(prep,'seal')) or actual is None or [actual['dev'],actual['ino']]!=prep['marker_identity']:
            raise Blocked('substituted-or-corrupt-recovery-marker')
        return prep

    def _update_marker(self,m,journal_receipt):
        generation=m['generation'].encode()
        try: prep=self._read_marker(generation)
        except (OSError,ValueError):
            # Interrupted in-place marker persistence is repairable only through
            # its immutable manifest-pinned inode, never a replacement inode.
            prep=dict(schema=1,generation=m['generation'],operation=m['operation'],owner=m['owner'],root=m['root'],identity=m['cleanup']['generation_identity'],
                pools=m['pools'],entries={v['path']:v['state'] for v in m['stages'].values()},files=dict(m['cleanup']['files']),marker_identity=m['cleanup']['marker_identity'],seal=None,
                blobs={name[5:]:dict(size=value['size'],digest=value['digest']) for name,value in m['cleanup']['files'].items() if name.startswith('blob-')},manifest=_digest(canonical(m)))
            manifest_receipt=self._observe(self.directory+b'/'+generation+b'/manifest.json')
            if manifest_receipt is None or [manifest_receipt['dev'],manifest_receipt['ino']]!=m['cleanup']['manifest_identity']: raise Blocked('substituted-repair-manifest')
            prep['files']['manifest.json']=manifest_receipt
        journal=parse(self._read(generation+b'/journal.json'))
        for key,state in journal['removed'].items():
            direction,pathkey=key.split(':',1); prep['entries'][_key(self._tomb_path(m,pathkey,direction))]=state
        prep['files']['journal.json']=journal_receipt
        self._marker_write(generation,prep,expected=m['cleanup']['marker_identity'])

    def _cleanup_debt_locked(self,*,exclude):
        results=[]
        with self._directory() as fd:
            names=os.listdir(fd)
        for name in sorted(names):
            if not re.fullmatch(r'preparation-[0-9a-f]{32}\.json',name): continue
            generation=name[len('preparation-'):-len('.json')]
            if generation==exclude: continue
            try:
                self._cleanup_generation(name.encode(),generation)
                results.append(dict(generation=generation,status='removed',errors=[]))
            except (OSError,ValueError) as exc:
                results.append(dict(generation=generation,status='debt',errors=[str(exc)]))
        return results

    def _cleanup_generation(self,marker,generation):
        generation_bytes=generation.encode()
        # The immutable self-pinned manifest and self-pinned signed journal can
        # requalify a completed own control publication whose marker update was
        # interrupted. They cannot adopt an identical-content replacement inode.
        manifest=None; journal_receipt=None
        try: manifest_raw=self._read(generation_bytes+b'/manifest.json')
        except FileNotFoundError: pass
        else:
            manifest=parse(manifest_raw)
            if manifest.get('generation')!=generation or type(manifest.get('binding_seal')) is not str or not hmac.compare_digest(manifest['binding_seal'],self._seal(manifest,'binding_seal')): raise Blocked('unqualified-cleanup-manifest')
            actual=self._observe(self.directory+b'/'+generation_bytes+b'/manifest.json')
            if [actual['dev'],actual['ino']]!=manifest['cleanup']['manifest_identity']: raise Blocked('substituted-cleanup-manifest')
            try: journal=parse(self._read(generation_bytes+b'/journal.json'))
            except FileNotFoundError: pass
            else:
                self._validate_journal(manifest,journal); self._validate_authority(manifest,journal)
                journal_receipt=self._observe(self.directory+b'/'+generation_bytes+b'/journal.json')
        try: prep=self._read_marker(generation_bytes)
        except (OSError,ValueError):
            if manifest is None or journal_receipt is None: raise
            self._update_marker(manifest,journal_receipt); prep=self._read_marker(generation_bytes)
        if journal_receipt is not None and prep['files'].get('journal.json')!=journal_receipt:
            prep['files']['journal.json']=journal_receipt
            self._marker_write(generation_bytes,prep,expected=manifest['cleanup']['marker_identity'])
        raw=self._read(marker)
        if type(prep['schema']) is not int or prep['schema']!=1 or prep['generation']!=generation or prep['root']!=_id(os.stat(self.binding.root,follow_symlinks=False))+[stat.S_IMODE(os.stat(self.binding.root,follow_symlinks=False).st_mode)]:
            raise Blocked('different-recovery-cleanup-owner')
        for value in (prep['operation'],prep['owner']):
            if type(value) is not str or str(uuid.UUID(value))!=value: raise Blocked('invalid-cleanup-operation')
        pointer=self._pointer()
        if pointer is not None and parse(pointer)['generation']==generation: raise Blocked('active-recovery-generation-cannot-be-cleaned')
        if type(prep['identity']) is not list or len(prep['identity'])!=2: raise Blocked('unacknowledged-preparation-generation')
        for value in prep['identity']: _uint(value)
        if type(prep['pools']) is not dict or type(prep['entries']) is not dict or type(prep['blobs']) is not dict: raise Blocked('invalid-cleanup-resources')
        generation_bytes=generation.encode(); expected={b'manifest.json',b'journal.json'}
        for digest,value in prep['blobs'].items():
            _fields(value,'size digest'); _uint(value['size'])
            if value['digest']!=digest or not re.fullmatch('[0-9a-f]{64}',digest): raise Blocked('invalid-cleanup-blob')
            expected.add(b'blob-'+digest.encode())
        # Verify every known namespace and byte preimage before reclaiming any.
        generation_present=self._observe(self.directory+b'/'+generation_bytes) is not None
        present=set()
        if generation_present:
            with self._directory(generation_bytes) as fd:
                if _id(os.fstat(fd))!=prep['identity']: raise Blocked('replaced-cleanup-generation')
                present={os.fsencode(n) for n in os.listdir(fd)}
                if not present<=expected: raise Blocked('foreign-generation-cleanup-entry')
        for name in present:
            if name.decode() not in prep['files'] or self._observe(self.directory+b'/'+generation_bytes+b'/'+name)!=prep['files'][name.decode()]: raise Blocked('substituted-generation-cleanup-file')
            if name.startswith(b'blob-'): self._verify_blob(generation_bytes+b'/'+name,prep['blobs'][name[5:].decode()])
            elif name==b'manifest.json':
                if prep['manifest'] is None or _digest(self._read(generation_bytes+b'/'+name))!=prep['manifest']: raise Blocked('changed-cleanup-manifest')
            else:
                journal=parse(self._read(generation_bytes+b'/'+name))
                if any(journal.get(k)!=prep[k] for k in ('generation','operation','owner')): raise Blocked('changed-cleanup-journal-owner')
        for key,identity in prep['pools'].items():
            pool=_path(key)
            if b'.ha-ops-recovery-'+generation_bytes+b'-' not in os.path.basename(pool) or type(identity) is not list or len(identity)!=2:
                raise Blocked('unacknowledged-or-invalid-cleanup-pool')
            if self._observe(pool) is None: continue
            with self._stage_directory(pool,identity) as fd:
                for name in os.listdir(fd):
                    path=pool+b'/'+os.fsencode(name); encoded=_key(path)
                    if encoded not in prep['entries'] or self._observe(path)!=prep['entries'][encoded]: raise Blocked('foreign-stage-cleanup-entry')
        # Each unlink/rmdir is nonrecursive and independently rechecks ownership.
        for key,identity in prep['pools'].items():
            pool=_path(key)
            if self._observe(pool) is None: continue
            with self._stage_directory(pool,identity) as fd:
                for name in os.listdir(fd):
                    path=pool+b'/'+os.fsencode(name); state=prep['entries'][_key(path)]
                    if self._observe(path)!=state: raise Blocked('changed-stage-during-cleanup')
                    if state['kind']=='directory': os.rmdir(name,dir_fd=fd)
                    else: os.unlink(name,dir_fd=fd)
                os.fsync(fd)
            parent,_,name=pool.rpartition(b'/')
            with _opened(self.binding,parent,'directory') as (fd,_):
                if _id(os.stat(name,dir_fd=fd,follow_symlinks=False))!=identity: raise Blocked('replaced-cleanup-pool')
                os.rmdir(name,dir_fd=fd); os.fsync(fd)
        if generation_present:
            with self._directory(generation_bytes) as fd:
                if _id(os.fstat(fd))!=prep['identity']: raise Blocked('changed-cleanup-generation')
                for name in present:
                    if self._observe(self.directory+b'/'+generation_bytes+b'/'+name)!=prep['files'][name.decode()]: raise Blocked('changed-cleanup-file-before-unlink')
                    os.unlink(name,dir_fd=fd)
                os.fsync(fd)
        with self._directory() as fd:
            if generation_present:
                if _id(os.stat(generation_bytes,dir_fd=fd,follow_symlinks=False))!=prep['identity']: raise Blocked('replaced-cleanup-generation')
                os.rmdir(generation_bytes,dir_fd=fd); os.fsync(fd)
            if self._read(marker)!=raw or self._read_marker(generation_bytes)['marker_identity']!=prep['marker_identity']: raise Blocked('changed-cleanup-marker')
            os.unlink(marker,dir_fd=fd); os.fsync(fd)

    def cleanup(self):
        """Explicitly retry addressed inactive generations, retaining conflicts."""
        with self._lock():
            p=self._pointer()
            return self._cleanup_debt_locked(exclude=parse(p)['generation'] if p else None)

    def discover(self):
        pointer=self._pointer()
        if pointer is None: return None
        p=parse(pointer); generation=p['generation'].encode()
        with self._directory(generation) as generation_fd:
            if _id(os.fstat(generation_fd))!=p['identity']: raise Blocked('changed-recovery-generation')
        raw=self._read(generation+b'/manifest.json')
        if _digest(raw)!=p['manifest']: raise Blocked('changed-recovery-manifest')
        m=parse(raw); _fields(m,'schema operation owner generation root directory before after explicit stages pools store_routes binding_seal cleanup previous forecast')
        if type(m['binding_seal']) is not str or not hmac.compare_digest(m['binding_seal'],self._seal(m,'binding_seal')): raise Blocked('unissued-or-modified-recovery-manifest')
        if type(m['schema']) is not int or m['schema']!=1 or m['generation']!=p['generation'] or m['root']!=_id(os.stat(self.binding.root,follow_symlinks=False))+[stat.S_IMODE(os.stat(self.binding.root,follow_symlinks=False).st_mode)] or m['directory']!=_key(self.directory):
            raise Blocked('different-recovery-root-or-generation')
        _fields(m['cleanup'],'marker_identity generation_identity manifest_identity files')
        actual_manifest=self._observe(self.directory+b'/'+generation+b'/manifest.json')
        if [actual_manifest['dev'],actual_manifest['ino']]!=m['cleanup']['manifest_identity']: raise Blocked('substituted-recovery-manifest')
        for value in (m['operation'],m['owner']):
            if type(value) is not str or str(uuid.UUID(value))!=value: raise Blocked('invalid-recovery-owner')
        if type(m['before']) is not dict or not m['before'] or type(m['after']) is not dict or set(m['before'])!=set(m['after']): raise Blocked('invalid-recovery-selection')
        if type(m['explicit']) is not list or len(set(m['explicit']))!=len(m['explicit']) or not set(m['explicit'])<=set(m['before']): raise Blocked('invalid-recovery-explicit-selection')
        for key in m['before']:
            path=_path(key)
            if key in m['explicit']: self._target_path(path)
            _validate_state(m['before'][key]); _validate_state(m['after'][key],semantic=True)
            for state in (m['before'][key],m['after'][key]):
                if state and state['kind']=='file':
                    self._verify_blob(generation+b'/blob-'+state['digest'].encode(),state)
                    if self._observe(self.directory+b'/'+generation+b'/blob-'+state['digest'].encode())!=m['cleanup']['files'].get('blob-'+state['digest']): raise Blocked('substituted-recovery-blob')
        if type(m['stages']) is not dict: raise Blocked('invalid-recovery-stages')
        for key,value in m['stages'].items():
            if key.split(':',1)[0] not in ('forward','rollback') or key.split(':',1)[1] not in m['before']: raise Blocked('invalid-stage-key')
            _fields(value,'path state'); _validate_state(value['state'])
            stagepath=_path(value['path']); pool=os.path.dirname(stagepath)
            if _key(pool) not in m['pools'] or not re.fullmatch(rb'stage-[0-9a-f]{64}',os.path.basename(stagepath)): raise Blocked('invalid-recovery-stage-path')
        if type(m['store_routes']) is not dict: raise Blocked('invalid-recovery-store-routes')
        for key,state in m['store_routes'].items():
            if self._observe(_path(key))!=state: raise Blocked('changed-recovery-store-route')
        j=parse(self._read(generation+b'/journal.json'))
        self._validate_journal(m,j); self._validate_authority(m,j); self._verify_recipe(m)
        recovery_resume(self.disk,self,m,j)
        return dict(pointer=p,manifest=m,journal=j)

    def _validate_journal(self,m,j):
        _fields(j,'schema operation owner generation revision phase current changed pending seal identity removed')
        if type(j['schema']) is not int or j['schema']!=1 or any(j[k]!=m[k] for k in ('operation','owner','generation')): raise Blocked('different-recovery-journal')
        if type(j['seal']) is not str or not hmac.compare_digest(j['seal'],self._seal(j,'seal')): raise Blocked('unissued-or-modified-recovery-journal')
        if type(j['identity']) is not list or len(j['identity'])!=2 or self._observe(self.directory+b'/'+m['generation'].encode()+b'/journal.json') is None: raise Blocked('invalid-journal-inode-receipt')
        actual=self._observe(self.directory+b'/'+m['generation'].encode()+b'/journal.json')
        if [actual['dev'],actual['ino']]!=j['identity']: raise Blocked('substituted-recovery-journal')
        _uint(j['revision'])
        if j['phase'] not in ('ready','forward','applied','restoring','restored') or type(j['current']) is not dict or set(j['current'])!=set(m['before']): raise Blocked('invalid-recovery-journal-state')
        if type(j['changed']) is not list or len(set(j['changed']))!=len(j['changed']) or not set(j['changed'])<=set(m['before']): raise Blocked('invalid-recovery-changed-set')
        for s in j['current'].values(): _validate_state(s)
        if type(j['removed']) is not dict: raise Blocked('invalid-removal-receipts')
        for key,value in j['removed'].items():
            if key.split(':',1)[0] not in ('forward','rollback') or key.split(':',1)[1] not in m['before']: raise Blocked('invalid-removal-receipt-key')
            _validate_state(value)
        p=j['pending']
        if p is not None:
            _fields(p,'path action direction before after stage')
            if p['path'] not in m['before'] or p['action'] not in ('replace','remove','chown','chmod') or p['direction'] not in ('forward','rollback') or type(p['after']) is not list or not p['after']:
                raise Blocked('invalid-recovery-intent')
            _validate_state(p['before'])
            for state in p['after']: _validate_state(state)
            if p['before']!=j['current'][p['path']]: raise Blocked('inconsistent-recovery-intent')
            if p['stage'] is not None:
                self._resource(m,j,p['stage'])

    def _known_state(self,m,key,value):
        if value is None: return True
        baseline=m['before'][key]
        stage_states=[v['state'] for k,v in m['stages'].items() if k.split(':',1)[1]==key]
        known=([baseline] if baseline else [])+stage_states
        identity=[v for v in known if (v['dev'],v['ino'],v['kind'])==(value['dev'],value['ino'],value['kind'])]
        if not identity: return False
        if value['kind']=='file': return value in identity
        targets=[v for v in (baseline,m['after'][key],*stage_states) if v is not None and v['kind']=='directory']
        owners={(v['uid'],v['gid']) for v in targets}
        modes={v['mode']&~mask for v in targets for mask in (0,0o2000,0o4000,0o6000)}
        return (value['uid'],value['gid']) in owners and value['mode'] in modes

    def _validate_authority(self,m,j):
        expected_stages={direction+':'+key for key in m['before'] for direction,state in (('forward',m['after'][key]),('rollback',m['before'][key])) if state is not None}
        if set(m['stages'])!=expected_stages or type(m['pools']) is not dict: raise Blocked('incomplete-recovery-stages')
        for key,stage in m['stages'].items():
            direction,pathkey=key.split(':',1); desired=m['after'][pathkey] if direction=='forward' else _semantic(m['before'][pathkey])
            path=_path(stage['path']); pool=self._pool_for(m,pathkey)
            if path!=pool+b'/stage-'+hashlib.sha256((direction+pathkey).encode()).hexdigest().encode(): raise Blocked('different-recovery-stage-route')
            state=stage['state']
            if state['kind']!=desired['kind'] or state['kind']=='file' and _semantic(state)!=desired:
                raise Blocked('different-recovery-stage-content')
            identity=m['pools'].get(_key(pool))
            if type(identity) is not list or len(identity)!=2: raise Blocked('invalid-recovery-pool-identity')
            for n in identity: _uint(n)
        for key,value in j['removed'].items():
            pathkey=key.split(':',1)[1]
            if value is None or not self._known_state(m,pathkey,value): raise Blocked('unqualified-removal-receipt')
        for key,current in j['current'].items():
            if not self._known_state(m,key,current): raise Blocked('unqualified-recovery-current-state')
            if current!=m['before'][key] and key not in j['changed']: raise Blocked('missing-recovery-changed-receipt')
        if j['phase']=='ready' and (j['current']!=m['before'] or j['changed'] or j['pending'] is not None): raise Blocked('inconsistent-ready-recovery')
        if j['phase']=='applied' and any(_semantic(j['current'][k])!=v for k,v in m['after'].items()): raise Blocked('inconsistent-applied-recovery')
        if j['phase']=='restored' and any(_semantic(j['current'][k])!=_semantic(m['before'][k]) for k in j['changed']): raise Blocked('inconsistent-restored-recovery')
        p=j['pending']
        if p is None: return
        if j['phase']!=('forward' if p['direction']=='forward' else 'restoring'): raise Blocked('intent-phase-mismatch')
        key=p['path']; desired=m['after'][key] if p['direction']=='forward' else _semantic(m['before'][key]); before=p['before']
        if p['action']=='replace':
            stage=p['direction']+':'+key
            if p['stage']==stage:
                if p['after']!=[m['stages'][stage]['state']] or desired is None: raise Blocked('unqualified-recovery-rename')
            elif type(p['stage']) is str and p['stage'].startswith('removed:'):
                resource=self._resource(m,j,p['stage'])
                if p['stage'].split(':',2)[2]!=key or p['after']!=[resource['state']]: raise Blocked('unqualified-tombstone-restoration')
            else: raise Blocked('unqualified-recovery-rename')
        elif p['action']=='remove':
            if p['stage'] is not None or p['after']!=[None] or before is None or desired is not None and before['kind']==desired['kind']: raise Blocked('unqualified-recovery-remove')
        else:
            if p['stage'] is not None or before is None or desired is None or before['kind']!='directory': raise Blocked('unqualified-recovery-metadata')
            if p['action']=='chmod': expected=[dict(before,mode=desired['mode'])]
            else:
                after=dict(before,uid=desired['uid'],gid=desired['gid'])
                expected=[dict(after,mode=after['mode']&~mask) for mask in (0,0o2000,0o4000,0o6000)]
            if p['after']!=expected: raise Blocked('unqualified-recovery-metadata-state')

    def _seal(self,value,field):
        unsigned=dict(value); unsigned[field]=None
        return hmac.new(self._read(b'authority.key'),canonical(unsigned),hashlib.sha256).hexdigest()

    def _signed_journal(self,journal):
        journal['seal']=self._seal(journal,'seal'); return journal

    def _verify_seal(self,forecast):
        value=dict(forecast); seal=value.pop('seal',None)
        if type(seal) is not str or not re.fullmatch('[0-9a-f]{64}',seal) or not hmac.compare_digest(seal,hmac.new(self._read(b'authority.key'),canonical(value),hashlib.sha256).hexdigest()):
            raise Blocked('unissued-or-modified-recovery-forecast')

    def _verify_recipe(self,m):
        original=dict(m,stages={},pools={},store_routes=None,binding_seal=None,cleanup=None,forecast=None)
        payloads={}
        for states in (m['before'],m['after']):
            for state in states.values():
                if state and state['kind']=='file': payloads[state['digest']]=Payload(None,b'',canonical(state))
        prepared=Prepared(self,canonical(original),tuple(sorted(payloads.items())),None)
        self._issued[id(prepared)]=prepared
        try:
            outputs=self._allocations(prepared)
            digest=_digest(prepared.manifest+b'\0'+canonical([[p.hex(),size,h,label] for p,size,h,label in outputs]))
        finally: self._issued.pop(id(prepared),None)
        if digest!=m['forecast']['recipe']: raise Blocked('changed-recovery-accounting-recipe')

    def _persist(self,m,j):
        if j['revision']>=U64: raise Blocked('exhausted-recovery-revision')
        j['revision']+=1
        def encoded(identity):
            j['identity']=identity; self._signed_journal(j)
            raw=canonical(j); template,_=self._control_template(m)
            if len(raw)>len(template): raise Blocked('recovery-control-envelope-exceeded')
            return raw
        receipt=self._write(m['generation'].encode()+b'/journal.json',encoded)
        self._update_marker(m,receipt)

    def _routes(self,m,j,path):
        for parent in sorted(ancestors((path,))):
            key=_key(parent)
            if key not in j['current'] or self._observe(parent)!=j['current'][key]: raise Blocked('changed-recovery-parent-route')

    def _reconcile(self,m,j):
        p=j['pending']
        if p is None: return
        path=_path(p['path']); self._routes(m,j,path); actual=self._observe(path)
        if actual==p['before']:
            j['pending']=None; self._persist(m,j); return
        if actual not in p['after']: raise Blocked('recovery-conflict:unacknowledged-effect')
        if p['action']=='remove':
            receipt=self._removed_receipt(m,p,allow_children=True); j['removed'][p['direction']+':'+p['path']]=receipt
        self._harden_effect(m,j,p,actual,allow_tomb_children=True)
        j['current'][p['path']]=actual
        if p['path'] not in j['changed']: j['changed'].append(p['path'])
        j['pending']=None; self._persist(m,j)
        if p['action']=='remove' and receipt['kind']=='directory':
            tomb=self._tomb_path(m,p['path'],p['direction'])
            with _opened(self.binding,tomb,'directory') as (fd,_): nonempty=bool(os.listdir(fd))
            if nonempty:
                self._step(m,j,p['path'],'replace',[receipt],direction=p['direction'],stage='removed:'+p['direction']+':'+p['path'])
                raise Blocked('nonempty-removal-restored-unselected-child')

    def _harden_effect(self,m,j,p,actual,*,allow_tomb_children=False):
        path=_path(p['path']); self._assert_held_routes(); self._routes(m,j,path)
        parent=os.path.dirname(path)
        with _opened(self.binding,parent,'directory') as (destination,_):
            if self._observe(path)!=actual: raise Blocked('changed-reconcile-postimage')
            if actual is not None:
                with _opened(self.binding,path,actual['kind']) as (child,_):
                    if _state(os.fstat(child))['ino']!=actual['ino']: raise Blocked('changed-reconcile-descriptor')
                    os.fsync(child)
            if p['action']=='remove':
                receipt=self._removed_receipt(m,p,allow_children=allow_tomb_children); tomb=self._tomb_path(m,p['path'],p['direction']); pool=os.path.dirname(tomb)
                with _opened(self.binding,tomb,receipt['kind']) as (child,_): os.fsync(child)
                with self._stage_directory(pool,m['pools'][_key(pool)]) as source: os.fsync(source)
            if p['action']=='replace':
                resource=self._resource(m,j,p['stage']); pool=os.path.dirname(_path(resource['path']))
                with self._stage_directory(pool,m['pools'][_key(pool)]) as source: os.fsync(source)
            os.fsync(destination)
        if self._observe(path)!=actual: raise Blocked('changed-reconcile-durable-postimage')
        self.fault('reconciled_effect_durable:'+p['action'])

    def _step(self,m,j,key,action,after,*,direction,stage=None):
        path=_path(key); self._routes(m,j,path)
        before=j['current'][key]
        if self._observe(path)!=before: raise Blocked('recovery-conflict:changed-target')
        if before in after: return
        if action=='remove' and self._observe(self._tomb_path(m,key,direction)) is not None: raise Blocked('occupied-removal-tombstone')
        pending=dict(path=key,action=action,direction=direction,before=before,after=after,stage=stage)
        self.fault('before_intent:'+action); j['pending']=pending; self._persist(m,j)
        self.fault('intent_durable:'+action)
        self._assert_held_routes()
        parent,_,name=path.rpartition(b'/')
        with _opened(self.binding,parent,'directory') as (fd,_):
            if self._observe(path)!=before: raise Blocked('recovery-conflict:before-syscall')
            if action=='replace':
                resource=self._resource(m,j,stage)
                relative=_path(resource['path'])
                if self._observe(relative)!=resource['state']: raise Blocked('changed-recovery-staging-inode')
                if os.fstat(fd).st_dev!=resource['state']['dev']: raise Blocked('cross-volume-recovery-publication')
                pool=os.path.dirname(relative)
                with self._stage_directory(pool,m['pools'][_key(pool)]) as source:
                    os.rename(os.path.basename(relative),name,src_dir_fd=source,dst_dir_fd=fd)
                    self.fault('effect:'+action); os.fsync(source)
            elif action=='remove':
                tomb=self._tomb_path(m,key,direction); pool=os.path.dirname(tomb)
                if before['kind']=='directory':
                    with _opened(self.binding,path,'directory') as (child,_):
                        if os.listdir(child): raise OSError(39,'Directory not empty')
                if self._observe(tomb) is not None: raise Blocked('occupied-removal-tombstone')
                with self._stage_directory(pool,m['pools'][_key(pool)]) as destination:
                    os.rename(name,os.path.basename(tomb),src_dir_fd=fd,dst_dir_fd=destination)
                    self.fault('effect:'+action)
                    receipt=self._removed_receipt(m,pending)
                    with _opened(self.binding,tomb,receipt['kind']) as (child,_): os.fsync(child)
                    os.fsync(destination)
                j['removed'][direction+':'+key]=receipt
            else:
                with _opened(self.binding,path,before['kind']) as (child,_):
                    if action=='chown': os.fchown(child,after[0]['uid'],after[0]['gid'])
                    else: os.fchmod(child,after[0]['mode'])
                    self.fault('effect:'+action); os.fsync(child)
            os.fsync(fd)
        actual=self._observe(path)
        if action=='remove': self._removed_receipt(m,pending)
        if actual not in after: raise Blocked('recovery-conflict:effect-readback')
        self.fault('effect_durable:'+action)
        j['current'][key]=actual
        if key not in j['changed']: j['changed'].append(key)
        j['pending']=None; self._persist(m,j); self.fault('ack_durable:'+action)

    def _run(self,m,j,direction):
        self._reconcile(m,j)
        if direction=='forward':
            if j['phase']=='restored': raise Blocked('recovery-point-already-restored')
            if j['phase']=='applied': return
            keys=set(m['before']); desired=m['after']; j['phase']='forward'
        else:
            keys=set(j['changed']); desired={k:_semantic(v) for k,v in m['before'].items()}
            if j['phase']=='restored':
                for k in keys:
                    if self._observe(_path(k))!=j['current'][k]: raise Blocked('recovery-conflict:restored-target-changed')
                return
            j['phase']='restoring'
        # Refuse foreign changed paths before any rollback effect; unmodified
        # selections are deliberately excluded from restoration.
        for key in keys:
            if self._observe(_path(key))!=j['current'][key]: raise Blocked('recovery-conflict:changed-target')
        self._persist(m,j)
        # Remove changed structure deepest first. No recursive target deletion.
        for key in sorted(keys,key=lambda k:(-_path(k).count(b'/'),_path(k))):
            current=j['current'][key]; target=desired[key]
            if current and (target is None or current['kind']!=target['kind']):
                self._step(m,j,key,'remove',[None],direction=direction)
        # Known staging inodes make rename-before-ACK independently classifiable.
        for key in sorted(keys,key=lambda k:(_path(k).count(b'/'),_path(k))):
            target=desired[key]; current=j['current'][key]
            if target is None: continue
            if current is None or (target['kind']=='file' and _semantic(current)!=target):
                stage=direction+':'+key
                if direction=='rollback' and 'forward:'+key in j['removed']:
                    tomb=self._tomb_path(m,key,'forward')
                    if self._observe(tomb)==j['removed']['forward:'+key]: stage='removed:forward:'+key
                resource=self._resource(m,j,stage)
                self._step(m,j,key,'replace',[resource['state']],direction=direction,stage=stage)
        # Finalize parents only after descendants, one syscall per intent.
        for key in sorted(keys,key=lambda k:(-_path(k).count(b'/'),_path(k))):
            target=desired[key]; current=j['current'][key]
            if target is None: continue
            if (current['uid'],current['gid'])!=(target['uid'],target['gid']):
                after=dict(current,uid=target['uid'],gid=target['gid'])
                variants=[dict(after,mode=after['mode']&~mask) for mask in (0,0o2000,0o4000,0o6000)]
                self._step(m,j,key,'chown',variants,direction=direction)
                current=j['current'][key]
            if current['mode']!=target['mode']:
                self._step(m,j,key,'chmod',[dict(current,mode=target['mode'])],direction=direction)
        j['phase']='applied' if direction=='forward' else 'restored'; self._persist(m,j)

    def _mutate(self,action,*,operation_id=None,owner_id=None,revision=None):
        with self._lock():
            found=self.discover()
            if found is None: raise Blocked('no-recovery-point')
            m,j=found['manifest'],found['journal']
            if operation_id is not None and operation_id!=m['operation'] or owner_id is not None and owner_id!=m['owner'] or revision is not None and (type(revision) is not int or revision!=j['revision']):
                raise Blocked('stale-recovery-owner-or-revision')
            if action=='reconcile': self._reconcile(m,j)
            else: self._run(m,j,action)
            return self.discover()

    def execute(self,**identity): return self._mutate('forward',**identity)
    def reconcile(self,**identity): return self._mutate('reconcile',**identity)
    def rollback(self,**identity): return self._mutate('rollback',**identity)
