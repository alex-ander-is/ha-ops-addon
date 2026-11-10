"""Dormant complete migration review. Saved decisions are diagnostic evidence only.

No constructor, read, reload or authentication restores a review challenge.
There is deliberately no publication/transition/activation operation here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import uuid
import time
import weakref

from native_inventory import Blocked
from native_disk import Blocked as DiskBlocked
from native_migration import Conflict, PersistenceUncertain, MAX_CONTROL, _json, _digest, _unique, _stat, decode_path, encode_path
from native_metadata import decode, ancestors

STORE = b'data/native-migration-review'
PAGE = 16384
VIEW_LEASE = 300
LEASE_TICK = 30
CLEANUP_ATTEMPTS = 3
MAX_SIZE = (1 << 63)-1


@dataclass(frozen=True)
class Recipe:
    owner: object
    operation: str
    sizes: tuple
    entries: int

    def check(self, root):
        if (type(self.owner) is not Review or self.owner.root != root or self.owner._recipes.get(id(self)) is not self
                or not re.fullmatch('[0-9a-f]{32}', self.operation)
                or type(self.sizes) is not tuple or len(self.sizes) > 8
                or any(type(n) is not int or not 0 <= n <= MAX_SIZE for n in self.sizes)
                or sum(self.sizes) > MAX_SIZE or type(self.entries) is not int or not 0 < self.entries <= 16):
            raise Blocked('issued-review-recipe-required')


def _context(value):
    if type(value) is not tuple or not value or not all(type(v) is str and v for v in value):
        raise Blocked('authenticated-review-context-required')
    return _digest(value)


def _name(value):
    # Byte identity is always retained separately, including invalid UTF-8.
    return repr(decode_path(value))[2:-1]


def _attrs(obj):
    return None if obj is None else {k: obj[k] for k in ('uid', 'gid', 'mode', 'kind')}


def _close_handles(views):
    for view in list(views.values()):
        try:os.close(view['fd'])
        except OSError:pass
    views.clear()


class Review:
    def __init__(self, candidate, *, trace=lambda event: None, clock=time.monotonic, scheduler=None):
        self.c, self.p, self.root = candidate, candidate.p, candidate.root
        self.trace = trace
        self._recipes, self._challenges = {}, {}
        self._views = {}
        self._finalizer=weakref.finalize(self,_close_handles,self._views)
        self._mutex = threading.RLock()
        self._uncertain = False
        self._clock=clock
        self._scheduler=scheduler or threading.Timer
        self._timer=None
        self._closed=False
        self._cleanup_debt=False
        self._cleanup_remaining=CLEANUP_ATTEMPTS
        self._cleanup_error=None
        self.cleanup_diagnostic=None

    def _schedule(self):
        if not self._views and not self._cleanup_debt and self._timer is not None:
            timer,self._timer=self._timer,None
            timer.cancel()
        if self._closed or self._timer is not None or not (self._views or self._cleanup_debt):return
        timer=None
        try:
            owner=weakref.ref(self)
            def tick():
                current=owner()
                if current is None:return
                with current._mutex:
                    if current._timer is not timer or current._closed:return
                    current._timer=None
                    current._sweep()
            timer=self._scheduler(LEASE_TICK,tick)
            timer.daemon=True
            self._timer=timer
            timer.start()
        except BaseException:
            self._timer=None
            try:
                if timer is not None:timer.cancel()
            except BaseException:pass
            finally:
                _close_handles(self._views);self._challenges.clear()
            raise

    def _retire_inactive(self):
        # Every caller, including expiry and direct close, obeys the same bound.
        if self._cleanup_remaining<=0:return
        try:
            with self.p.disk.migration_review_cleanup_guard(self):
                with self._store() as fd:
                    if fd is not None:
                        active={v['name'] for v in self._views.values()}
                        for name,value in self._completed(fd,None,None,active_names=active):
                            if value['name'] not in active:self._retire_receipt(fd,name,value)
            self._cleanup_remaining=CLEANUP_ATTEMPTS
            self._cleanup_debt=False;self.cleanup_diagnostic=None;self._cleanup_error=None
        except (ValueError,OSError,RuntimeError) as error:
            self._cleanup_remaining=max(0,self._cleanup_remaining-1)
            retryable=(isinstance(error,DiskBlocked) and str(error) in
                ('nested-or-competing-guard','competing-process-guard') or
                isinstance(error,OSError) and error.errno in
                (errno.EAGAIN,errno.EWOULDBLOCK,errno.EINTR,errno.EIO,errno.EBUSY,errno.ETIMEDOUT))
            if not retryable:self._cleanup_remaining=0
            self._cleanup_debt=bool(self._cleanup_remaining)
            self.cleanup_diagnostic=str(error);self._cleanup_error=error

    def _expire_content(self):
        """Release expired descriptors without entering cleanup or rearming it."""
        if self._closed:raise Conflict('review-closed')
        now=self._clock();expired=False
        for ctx,view in list(self._views.items()):
            if now>=view['deadline']:
                self._views.pop(ctx);_close_handles({ctx:view});expired=True
                grant=self._challenges.get(ctx)
                if grant is not None and grant['token']==view['review_id']:self._challenges.pop(ctx,None)
        # An early request refusal must not strand newly inactive owned spools.
        if expired:self._cleanup_debt=bool(self._cleanup_remaining)
        self._schedule()
        return expired

    def _sweep(self, *, reclaim=False, before_reclaim=None):
        # A request callback owns the final expiry/metadata/authorization order.
        expired=False if reclaim and before_reclaim is not None else self._expire_content()
        if reclaim:
            if before_reclaim is not None:before_reclaim()
            self._cleanup_remaining=CLEANUP_ATTEMPTS
        if expired or reclaim or self._cleanup_debt:self._retire_inactive()
        if not self._views and not self._cleanup_debt and self._timer is not None:
            timer,self._timer=self._timer,None
            timer.cancel()
        self._schedule()

    def sweep_content(self):
        with self._mutex:self._sweep()

    def shutdown(self):
        with self._mutex:
            self._closed=True
            timer,self._timer=self._timer,None
            try:
                if timer is not None:timer.cancel()
            finally:
                self._challenges.clear()
                _close_handles(self._views)

    def renew_content(self, *, view_id, context):
        ctx=_context(context)
        with self._mutex:
            self._sweep()
            view=self._views.get(ctx)
            if (view is None or type(view_id) is not str or not secrets.compare_digest(view_id,view['view_id'])
                    or self.c.saved_descriptor()!=view['candidate_stat']):raise Conflict('invalid-content-view-reference')
            with self._store() as fd:self._verify_view(view,fd)
            view['deadline']=self._clock()+VIEW_LEASE
            return dict(view_id=view_id)

    def _recipe(self, *sizes):
        value = Recipe(self, uuid.uuid4().hex, tuple(sizes), 8)
        self._recipes[id(value)] = value
        return value

    @contextmanager
    def _admission(self, *sizes):
        recipe=self._recipe(*sizes)
        try:
            with self.p.disk.migration_review_guard(recipe):yield
        finally:self._recipes.pop(id(recipe),None)

    def _effect(self, name, call):
        result = call()
        self.trace(name)
        return result

    @contextmanager
    def _store(self, create=False):
        if self.c._info(STORE) is None:
            if not create:
                yield None
                return
            self.c._mkdir(STORE)
        self.c._directory(STORE)
        with self.c.t._parent(STORE) as (parent, name):
            fd = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                before = os.fstat(fd)
                for name in os.listdir(fd):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if re.fullmatch(r'retire-[0-9a-f]{32}',name):
                        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.geteuid():raise Blocked('unsafe-retirement-directory')
                        continue
                    if (not re.fullmatch(r'(?:decision.json|completed-[0-9a-f]{32}\.json|\.pending-[0-9a-f]{32}|view-[0-9a-f]{32})', name)
                            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                        raise Blocked('unsafe-review-store-entry')
                yield fd
                now = self.c._directory(STORE)
                if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
                    raise Blocked('review-directory-replaced')
            finally:
                os.close(fd)

    def _decision(self, fd):
        if fd is None: return None
        try: control = os.open('decision.json', os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError: return None
        try:
            before = os.fstat(control)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 8192):
                raise Blocked('unsafe-review-decision')
            raw = os.pread(control, 8193, 0)
            value = json.loads(raw, object_pairs_hook=_unique)
            keys = {'schema','action','candidate','scope','model','preparation','transition','runtime','review','previous','digest'}
            if (type(value) is not dict or set(value) != keys or type(value['schema']) is not int or value['schema'] != 1
                    or value['action'] not in ('approve','refuse','dismiss')
                    or any(type(value[k]) is not str or not re.fullmatch('[0-9a-f]{64}',value[k])
                           for k in keys-{'schema','action','previous'})
                    or value['previous'] is not None and (type(value['previous']) is not str or not re.fullmatch('[0-9a-f]{64}',value['previous']))
                    or value['digest'] != _digest({k:v for k,v in value.items() if k!='digest'}) or _json(value) != raw
                    or _stat(before) != _stat(os.fstat(control))
                    or _stat(before) != _stat(os.stat('decision.json',dir_fd=fd,follow_symlinks=False))):
                raise Blocked('invalid-review-decision')
            return value
        finally: os.close(control)

    def _receipt(self, fd, name, *, store_fd=None, logical=None):
        logical=logical or name
        store_fd=fd if store_fd is None else store_fd
        if not re.fullmatch(r'completed-[0-9a-f]{32}\.json', logical):raise Blocked('invalid-completed-receipt-name')
        try:file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:return None
        try:
            before=os.fstat(file)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.geteuid()
                    or stat.S_IMODE(before.st_mode)!=0o600 or before.st_size>8192):raise Blocked('unsafe-completed-receipt')
            raw=os.pread(file,8193,0);value=json.loads(raw,object_pairs_hook=_unique)
            keys={'schema','status','name','candidate','scope','row','side','oid','size','sha256','stat','store','control','quarantine','digest'}
            if (type(value) is not dict or set(value)!=keys or type(value['schema']) is not int or value['schema']!=1
                    or value['status'] not in ('complete','retiring') or value['name']!='view-'+logical[10:-5]
                    or any(type(value[k]) is not str or not re.fullmatch('[0-9a-f]{64}',value[k]) for k in ('candidate','scope','sha256','digest'))
                    or type(value['oid']) is not str or not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})',value['oid'])
                    or type(value['row']) is not int or value['row']<0 or value['side'] not in ('before','after')
                    or type(value['size']) is not int or not 0<=value['size']<=MAX_SIZE
                    or type(value['stat']) is not list or len(value['stat'])!=9 or any(type(n) is not int for n in value['stat'])
                    or value['stat'][6]!=value['size'] or value['stat'][3]!=1 or value['stat'][4]!=os.geteuid()
                    or not stat.S_ISREG(value['stat'][2]) or stat.S_IMODE(value['stat'][2])!=0o600
                    or type(value['store']) is not list or len(value['store'])!=2 or any(type(n) is not int for n in value['store'])
                    or value['store']!=[os.fstat(store_fd).st_dev,os.fstat(store_fd).st_ino]
                    or type(value['quarantine']) is not list or len(value['quarantine'])!=3
                    or value['quarantine'][0]!='retire-'+logical[10:-5] or any(type(n) is not int for n in value['quarantine'][1:])
                    or type(value['control']) is not list or len(value['control'])!=2 or any(type(n) is not int for n in value['control'])
                    or value['control']!=[before.st_dev,before.st_ino]
                    or value['digest']!=_digest({k:v for k,v in value.items() if k!='digest'}) or _json(value)!=raw
                    or _stat(before)!=_stat(os.fstat(file)) or _stat(before)!=_stat(os.stat(name,dir_fd=fd,follow_symlinks=False))):
                raise Blocked('invalid-completed-receipt')
            return value
        finally:os.close(file)

    def _save_receipt(self, fd, name, value, expected, *, before_publish=lambda:None):
        current=self._receipt(fd,name)
        if (None if current is None else current['digest'])!=expected:raise Conflict('completed-receipt-cas-conflict')
        if current is not None:raise Conflict('immutable-completed-receipt-required')
        value=copy.deepcopy(value);value.pop('digest',None)
        pending='.pending-'+uuid.uuid4().hex
        file=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        publishing=False
        try:
            self.trace('review-receipt-open')
            info=os.fstat(file);value['control']=[info.st_dev,info.st_ino]
            value['digest']=_digest(value);raw=_json(value)
            remaining=memoryview(raw)
            while remaining:
                n=self._effect('review-receipt-write',lambda:os.write(file,remaining))
                if n<=0:raise OSError('short-completed-receipt-write')
                remaining=remaining[n:]
            self._effect('review-receipt-file-fsync',lambda:os.fsync(file));issued=_stat(os.fstat(file))
            if (_stat(os.stat(pending,dir_fd=fd,follow_symlinks=False))!=issued or self._receipt(fd,name)!=current):
                raise Conflict('completed-receipt-replaced')
            try:
                import native_migration_transition as transition
                before_publish();publishing=True
                self._effect('review-receipt-replace',lambda:transition._rename_noreplace(fd,os.fsencode(pending),fd,os.fsencode(name)))
                self._effect('review-receipt-directory-fsync',lambda:os.fsync(fd))
            except BaseException as exc:
                self._uncertain=True
                raise PersistenceUncertain('completed-receipt-durability-uncertain') from exc
            return value
        finally:
            try:
                if not publishing:
                    with self._quarantine(fd,value) as child:
                        self._discard_owned(fd,pending,file,child,'control')
            finally:os.close(file)

    @contextmanager
    def _quarantine(self, fd, value):
        name,dev,ino=value['quarantine']
        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            info=os.fstat(child)
            if ([info.st_dev,info.st_ino]!=[dev,ino] or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.geteuid()
                    or set(os.listdir(child))-{'payload','control'}):raise Conflict('retirement-directory-replaced')
            yield child
            now=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if [now.st_dev,now.st_ino]!=[dev,ino]:raise Conflict('retirement-directory-replaced')
        finally:os.close(child)

    def _completed_payload(self, fd, child, value, *, retiring, hash_payload=True):
        """Read-only proof for either payload location, including settled deletion."""
        present=[]
        try:
            for source,target in ((fd,value['name']),(child,'payload')):
                try:file=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=source)
                except FileNotFoundError:continue
                present.append((source,target,file))
            if len(present)>1:raise Conflict('duplicate-retirement-payload')
            if not present:
                if not retiring:raise Conflict('completed-spool-missing')
                return
            source,target,file=present[0]
            if source!=fd and not retiring:raise Conflict('completed-spool-missing')
            actual=_stat(os.fstat(file))
            if actual[:8]!=value['stat'][:8] or source==fd and actual!=value['stat']:
                raise Conflict('completed-spool-replaced')
            if hash_payload:
                digest=hashlib.sha256();offset=0
                while offset<value['size']:
                    chunk=os.pread(file,min(PAGE,value['size']-offset),offset)
                    if not chunk:raise Conflict('completed-spool-incomplete')
                    digest.update(chunk);offset+=len(chunk)
                if digest.hexdigest()!=value['sha256']:raise Conflict('completed-spool-corrupt')
            if (_stat(os.fstat(file))!=actual
                    or _stat(os.stat(target,dir_fd=source,follow_symlinks=False))!=actual):
                raise Conflict('completed-spool-corrupt')
        finally:
            for _,_,file in present:os.close(file)

    def _completed(self, fd, candidate, scope, *, active_names=frozenset()):
        names=set(os.listdir(fd));receipts=[];owned={'decision.json'}
        owned.update(n for n in names if re.fullmatch(r'\.pending-[0-9a-f]{32}',n))
        for qname in sorted(n for n in names if re.fullmatch(r'retire-[0-9a-f]{32}',n)):
            logical='completed-'+qname[7:]+'.json'
            if logical in names:continue
            child=os.open(qname,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                children=set(os.listdir(child))
                if not children:owned.add(qname);continue
                if 'control' not in children:raise Conflict('unowned-retirement-payload')
                value=self._receipt(child,'control',store_fd=fd,logical=logical)
                if value is None:raise Conflict('retirement-control-missing')
                # Captured immutable control is durable retirement intent.
                with self._quarantine(fd,value) as held:
                    self._completed_payload(fd,held,value,retiring=True,hash_payload=value['name'] not in active_names)
                owned.update((qname,value['name']));receipts.append((logical,value))
            finally:os.close(child)
        for name in sorted(n for n in names if n.startswith('completed-')):
            value=self._receipt(fd,name)
            if value is None:raise Conflict('completed-control-missing')
            owned.update((name,value['name'],value['quarantine'][0]))
            with self._quarantine(fd,value) as child:
                if 'control' in os.listdir(child):raise Conflict('duplicate-retirement-control')
                self._completed_payload(fd,child,value,retiring=value['status']=='retiring',hash_payload=value['name'] not in active_names)
            receipts.append((name,value))
        if names-owned:raise Blocked('retained-review-content-requires-diagnosis')
        return receipts

    def _available(self, fd, candidate, scope, grant=None, *, completed=True):
        """Shared pure admission proof, called with mutex/disk/store locks held."""
        if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
        value,accepted=self.c.current_proof(self.c.read())
        model=self._model(value,accepted)
        if (candidate!=value['candidate'] or scope!=model['scope_digest']
                or grant is not None and grant['model']!=_digest(model)):
            raise Conflict('review-model-changed')
        receipts=[] if fd is None or not completed else self._completed(fd,candidate,scope)
        return value,model,receipts

    def _capture(self, source, name, child, target, expected):
        # Only the canonical namespace is untrusted. The exclusive private
        # quarantine is addressed through its held, receipt-bound descriptor.
        import native_migration_transition as transition
        try:info=os.stat(name,dir_fd=source,follow_symlinks=False)
        except FileNotFoundError:
            try:captured=os.stat(target,dir_fd=child,follow_symlinks=False)
            except FileNotFoundError:return False
        else:
            if _stat(info)[:8]!=expected[:8]:raise Conflict('retirement-source-replaced')
            transition._rename_noreplace(source,os.fsencode(name),child,os.fsencode(target))
            captured=os.stat(target,dir_fd=child,follow_symlinks=False)
            self._effect('review-retirement-capture-fsync',lambda:os.fsync(child))
            self._effect('review-retirement-source-fsync',lambda:os.fsync(source))
        if _stat(captured)[:8]!=expected[:8]:
            try:transition._rename_noreplace(child,os.fsencode(target),source,os.fsencode(name))
            except OSError:pass
            raise Conflict('retirement-captured-foreign-entry')
        try:os.stat(name,dir_fd=source,follow_symlinks=False)
        except FileNotFoundError:return True
        raise Conflict('retirement-source-reappeared')

    def _discard_owned(self, fd, name, file, child, target):
        """Reclaim an unpublished attempt only through its held private capture."""
        expected=_stat(os.fstat(file))
        if self._capture(fd,name,child,target,expected):
            if _stat(os.stat(target,dir_fd=child,follow_symlinks=False))[:8]!=expected[:8]:
                raise Conflict('pending-capture-replaced')
            self._effect('review-pending-retire',lambda:os.unlink(target,dir_fd=child))
            self._effect('review-pending-retire-fsync',lambda:os.fsync(child))

    def _retire_receipt(self, fd, name, value):
        with self._quarantine(fd,value) as child:
            captured=self._receipt(child,'control',store_fd=fd,logical=name)
            if captured is None:
                current=self._receipt(fd,name)
                if current!=value:raise Conflict('completed-receipt-cas-conflict')
                expected=_stat(os.stat(name,dir_fd=fd,follow_symlinks=False))
                self._capture(fd,name,child,'control',expected)
                captured=self._receipt(child,'control',store_fd=fd,logical=name)
            if captured!=value:raise Conflict('completed-receipt-cas-conflict')
            if self._capture(fd,value['name'],child,'payload',value['stat']):
                held=os.open('payload',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=child)
                try:
                    before=_stat(os.fstat(held));digest=hashlib.sha256();offset=0
                    while offset<value['size']:
                        data=os.pread(held,min(PAGE,value['size']-offset),offset)
                        if not data:raise Conflict('retirement-payload-incomplete')
                        digest.update(data);offset+=len(data)
                    if digest.hexdigest()!=value['sha256'] or _stat(os.fstat(held))!=before:raise Conflict('retirement-payload-corrupt')
                finally:os.close(held)
                self._effect('review-content-retire',lambda:os.unlink('payload',dir_fd=child))
                self._effect('review-content-retire-fsync',lambda:os.fsync(child))
                if self.c is not None:
                    try:os.stat(value['name'],dir_fd=fd,follow_symlinks=False)
                    except FileNotFoundError:pass
                    else:raise Conflict('retirement-source-reappeared')
            captured=self._receipt(child,'control',store_fd=fd,logical=name)
            if captured!=value:raise Conflict('completed-receipt-cas-conflict')
            self._effect('review-receipt-retire',lambda:os.unlink('control',dir_fd=child))
            self._effect('review-receipt-retire-fsync',lambda:os.fsync(child))
            try:os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise Conflict('retirement-source-reappeared')
        # Empty private directories remain inert baseline diagnostics. Never
        # rmdir an untrusted canonical pathname after a separate identity check.

    def _model(self, value, accepted):
        policy = self.c._policy(tuple(decode_path(p) for p in value['repository']))
        manifest = decode(base64.b64decode(value['metadata']),width=len(value['parent_oid']),policy=policy)
        records = manifest.by_path
        observed = {r['path']:r['object'] for r in accepted['observation']['source']}
        native = {r['path']:r['object'] for r in accepted['observation']['native']}
        rows = []
        for i, row in enumerate(value['rows']):
            record = records.get(decode_path(row['after']['path'])) if row['after'] else None
            after = None if record is None else dict(uid=record.uid,gid=record.gid,mode=record.mode,kind=record.kind)
            rows.append(dict(index=i,action=row['action'],before=row['before'],after=row['after'],
                before_metadata=None, before_metadata_status='Historical metadata unavailable',
                after_metadata=after, after_metadata_status='Exact candidate metadata' if after else 'Metadata unavailable / repository-only control',
                observed_source_metadata=_attrs(observed.get(row['before']['path'])) if row['before'] else None,
                before_name=_name(row['before']['path']) if row['before'] else None,
                after_name=_name(row['after']['path']) if row['after'] else None))
        paths=[decode_path(row[side]['path']) for row in value['rows'] for side in ('before','after') if row[side]]
        parents=[]
        for path in sorted(ancestors(paths)):
            record=records.get(path)
            parent=dict(path=encode_path(path),name=_name(encode_path(path)),before=None,
                before_status='Historical metadata unavailable',after=None if record is None else
                dict(uid=record.uid,gid=record.gid,mode=record.mode,kind=record.kind),
                after_status='Exact candidate metadata' if record else 'Git-only parent metadata unavailable; no live directory creation')
            origins=[a+path[len(b):] for a,b in self.p.mappings if path==b or path.startswith(b+b'/')]
            parent['observed_source_metadata']=_attrs(observed.get(encode_path(origins[0] if origins else path)))
            parent['observed_native_metadata']=_attrs(native.get(parent['path']))
            parents.append(parent)
        scope = dict(candidate=value['candidate'],preparation=value['preparation'],transition=value['transition'],runtime=value['runtime'],
            policy=self.p.policy.digest,mappings=[[encode_path(a),encode_path(b)] for a,b in self.p.mappings],
            rows=value['rows'],parents=parents,repository=value['repository'],dispositions=accepted['observation']['rows'])
        return dict(candidate=value['candidate'],parent_ref=value['parent_ref'],parent_ref_name=_name(value['parent_ref']),parent_oid=value['parent_oid'],commit=value['commit'],tree=value['tree'],
            scope=scope,scope_digest=_digest(scope),rows=rows,total=len(rows),parents=parents,
            consequences=['This decision does not publish or activate migration.',
                'The proposed commit changes Git layout only; repository-only files stay outside live roots.',
                'Removed and retired paths are Git proposals, never live deletions.'],complete=True)

    def diagnose(self):
        with self._mutex:
            try:
                value = self.c.read()
                accepted = self.p.read() if value else None
                model = self._model(value,accepted) if value else None
                with self._store() as fd:
                    decision = self._decision(fd)
                    retained = [] if fd is None else sorted(n for n in os.listdir(fd) if n != 'decision.json')
                return dict(required=True,status='unavailable' if self._uncertain else 'absent' if value is None else value['validation'],
                    model=model,decision=decision,decision_authority=False,retained=retained,cleanup_diagnostic=self.cleanup_diagnostic,review_id=None)
            except (ValueError,OSError,RuntimeError):
                self._challenges.clear()
                return dict(required=True,status='unavailable',model=None,decision=None,decision_authority=False,cleanup_diagnostic=self.cleanup_diagnostic,review_id=None)

    def _revalidate_content_bindings(self):
        """Finish trusted existing-view continuity before any requester-specific refusal.

        Candidate.revalidate owns its durable fence and admission. These bounded
        saved-state/model proofs only carry existing bindings; they grant nothing.
        """
        try:
            before=self.c.saved_descriptor()
            saved=self.c.read()
            if saved is None:raise Conflict('new-current-review-required')
            frozen=self._model(saved,self.p.read())
            if self.c.saved_descriptor()!=before:raise Conflict('candidate-control-changed-before-revalidation')
            if self.c.revalidate()!='current':raise Conflict('new-current-review-required')
            after=self.c.saved_descriptor()
            current=self.c.read()
            if (current is None or current['stale'] or current['validation']!='current'
                    or current['candidate']!=saved['candidate']):
                raise Conflict('new-current-review-required')
            model=self._model(current,self.p.read())
            if _digest(model)!=_digest(frozen):raise Conflict('review-model-changed')
            self._refresh_content_bindings((before,after),current,model)
        except BaseException:
            self._challenges.clear()
            for view in self._views.values():view['candidate_stat']=None
            raise

    def _refresh_content_bindings(self, descriptors, value, model):
        before,after=descriptors
        if self.c.saved_descriptor()!=after:raise Conflict('candidate-control-changed-after-revalidation')
        digest=_digest(model)
        for view in self._views.values():
            if (view['candidate_stat']==before and view['candidate']==value['candidate']
                    and view['model']==digest):
                view['candidate_stat']=after

    def _request_model(self, candidate, scope, grant=None):
        """Bounded saved eligibility; full current proof still needs admission."""
        if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
        descriptor=self.c.saved_descriptor()
        saved=self.c.read()
        allowed=('unchecked','current') if grant is None else ('current',)
        if saved is None or saved['stale'] or saved['validation'] not in allowed:
            raise Conflict('new-current-review-required')
        accepted=self.p.read()
        if accepted is None or accepted['digest']!=saved['preparation']:
            raise Conflict('review-model-changed')
        model=self._model(saved,accepted)
        if (candidate!=saved['candidate'] or scope!=model['scope_digest']
                or grant is not None and _digest(model)!=grant['model']):
            raise Conflict('review-model-changed')
        self._request_descriptor(descriptor)
        return descriptor

    def _request_descriptor(self, descriptor):
        if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
        if self.c.saved_descriptor()!=descriptor:raise Conflict('candidate-control-changed-before-reclaim')

    def _authorize_request(self, descriptor, validate=lambda:None):
        # Closing FDs, scheduling and participant metadata can all take time or
        # invalidate a grant. Only pure, fresh checks may follow them. Each
        # repeated pass releases at least one newly expired held view.
        while True:
            self._expire_content()
            self._request_descriptor(descriptor)
            now=self._clock()
            if self._closed:raise Conflict('review-closed')
            if any(now>=view['deadline'] for view in self._views.values()):continue
            if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
            validate()
            return

    def review(self, *, candidate, scope, context):
        with self._mutex:
            self._expire_content()
            ctx = _context(context)
            self._challenges.pop(ctx,None)
            try:
                # Reject obsolete client identities before revalidation rotates the shared pointer.
                descriptor=self._request_model(candidate,scope)
                self._sweep(reclaim=True,before_reclaim=lambda:self._authorize_request(descriptor))
                self._revalidate_content_bindings()
                with self._admission(8192,8192):
                    with self._store() as fd:
                        value,model,_=self._available(fd,candidate,scope,completed=False)
                        available=True;diagnostic=None
                        try:
                            if fd is not None:self._completed(fd,candidate,scope)
                        except (ValueError,OSError,RuntimeError) as error:
                            available=False;diagnostic='Cached content unavailable. You can refuse or dismiss migration; approval and content inspection are unavailable.'
                        actions=['approve','refuse','dismiss'] if available else ['refuse','dismiss']
                        previous=self._decision(fd)
                        token = secrets.token_hex(32)
                        self._challenges[ctx] = dict(token=token,model=_digest(model),scope=model['scope_digest'],candidate=candidate,
                            previous=None if previous is None else previous['digest'],allowed_actions=actions,content_available=available)
                        return dict(model=model,review_id=token,status='current',allowed_actions=actions,content_available=available,cache_diagnostic=diagnostic)
            except BaseException:
                self._challenges.pop(ctx,None)
                raise

    def _grant(self, context, review_id, candidate, scope):
        ctx = _context(context)
        grant = self._challenges.get(ctx)
        if (grant is None or type(review_id) is not str or not secrets.compare_digest(grant['token'],review_id)
                or candidate != grant['candidate'] or scope != grant['scope']):
            raise Conflict('new-explicit-review-required')
        return ctx, grant

    def _publish(self, fd, value, expected):
        current = self._decision(fd)
        if (None if current is None else current['digest']) != expected: raise Conflict('decision-cas-conflict')
        value['digest'] = _digest(value)
        raw = _json(value)
        name = '.pending-'+uuid.uuid4().hex
        file = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            self.trace('review-open')
            view = memoryview(raw)
            while view:
                n = self._effect('review-write',lambda:os.write(file,view))
                if n<=0: raise OSError('short-review-write')
                view=view[n:]
            self._effect('review-file-fsync',lambda:os.fsync(file))
            receipt = _stat(os.fstat(file))
        finally: os.close(file)
        if _stat(os.stat(name,dir_fd=fd,follow_symlinks=False))!=receipt: raise Conflict('review-pending-replaced')
        if self._decision(fd)!=current: raise Conflict('decision-cas-conflict')
        try:
            self._effect('review-replace',lambda:os.replace(name,'decision.json',src_dir_fd=fd,dst_dir_fd=fd))
            self._effect('review-directory-fsync',lambda:os.fsync(fd))
        except BaseException as exc:
            self._uncertain=True
            raise PersistenceUncertain('review-decision-durability-uncertain') from exc
        return copy.deepcopy(value)

    def decide(self, *, review_id, candidate, scope, action, context):
        if action not in ('approve','refuse','dismiss'): raise Blocked('invalid-review-action')
        with self._mutex:
            self._sweep()
            ctx, grant = self._grant(context,review_id,candidate,scope)
            try:
                if action not in grant.get('allowed_actions',()):raise Conflict('review-action-unavailable')
                if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
                self._revalidate_content_bindings()
                with self._admission(8192,8192):
                    if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
                    with self._store(create=True) as fd:
                        value,model,_=self._available(fd,candidate,scope,grant,completed=action=='approve')
                        decision=dict(schema=1,action=action,candidate=candidate,scope=scope,model=grant['model'],
                            preparation=value['preparation'],transition=value['transition'],runtime=_digest(value['runtime']),
                            review=_digest(review_id),previous=grant['previous'])
                        result=self._publish(fd,decision,grant['previous'])
                    return dict(decision=result,decision_authority=False,required=True)
            finally:
                self._challenges.pop(ctx,None)

    def _verify_view(self, view, fd):
        if (_stat(os.fstat(view['fd'])) != view['stat'] or
                _stat(os.stat(view['name'],dir_fd=fd,follow_symlinks=False)) != view['stat']):
            raise Conflict('review-content-replaced')

    def open_content(self, *, review_id, candidate, scope, row, side, context):
        with self._mutex:
            self._expire_content()
            ctx,grant=self._grant(context,review_id,candidate,scope)
            try:
                if not grant.get('content_available',False):raise Conflict('review-content-unavailable')
                frozen_grant=copy.deepcopy(grant)
                descriptor=self._request_model(candidate,scope,grant)
                info=self.c.content_info(candidate=candidate,row=row,side=side)
                def current_grant():
                    current=self._challenges.get(ctx)
                    if current is not grant or current!=frozen_grant or not current.get('content_available',False):
                        raise Conflict('new-explicit-review-required')
                def current_request():self._authorize_request(descriptor,current_grant)
                self._sweep(reclaim=True,before_reclaim=current_request)
                current_request()
                with self._admission(info['size'],8192,8192):
                    if self._uncertain:raise PersistenceUncertain('review-durability-uncertain')
                    with self._store(create=True) as fd:
                        value,model,completed=self._available(fd,candidate,scope,grant)
                        previous=self._views.get(ctx)
                        identity=uuid.uuid4().hex
                        pending='.pending-'+identity
                        name='view-'+identity
                        quarantine='retire-'+identity
                        os.mkdir(quarantine,0o700,dir_fd=fd)
                        child=os.open(quarantine,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                        file=None
                        promoted=False
                        publication_started=False
                        digest=hashlib.sha256();count=0
                        class Sink:
                            def write(inner,data):
                                nonlocal count
                                remaining=memoryview(data)
                                while remaining:
                                    n=self._effect('review-content-write',lambda:os.write(file,remaining))
                                    if n<=0:raise OSError('short-content-write')
                                    remaining=remaining[n:]
                                count+=len(data);digest.update(data)
                                return len(data)
                        try:
                            qinfo=os.fstat(child)
                            if (not stat.S_ISDIR(qinfo.st_mode) or stat.S_IMODE(qinfo.st_mode)!=0o700
                                    or qinfo.st_uid!=os.geteuid() or os.listdir(child)
                                    or _stat(qinfo)!=_stat(os.stat(quarantine,dir_fd=fd,follow_symlinks=False))):
                                raise Conflict('retirement-directory-replaced')
                            self.trace('review-content-quarantine-open')
                            file=os.open(pending,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                            self.trace('review-content-open')
                            self.c.read_content(candidate=candidate,row=row,side=side,destination=Sink())
                            if count!=info['size']:raise Conflict('content-size-changed')
                            self._effect('review-content-file-fsync',lambda:os.fsync(file))
                            receipt=_stat(os.fstat(file))
                            if receipt[3]!=1 or receipt[6]!=count or stat.S_IMODE(receipt[2])!=0o600: raise Conflict('content-descriptor-changed')
                            self.c.current_proof(value)
                            self._effect('review-content-directory-fsync',lambda:os.fsync(fd))
                            import native_migration_transition as transition
                            if _stat(os.stat(pending,dir_fd=fd,follow_symlinks=False))!=receipt:
                                raise Conflict('review-pending-replaced')
                            transition._rename_noreplace(fd,os.fsencode(pending),fd,os.fsencode(name))
                            promoted=True
                            receipt=_stat(os.fstat(file))
                            self.trace('review-content-promote')
                            view=dict(view_id=secrets.token_hex(32),name=name,fd=file,stat=receipt,size=count,sha256=digest.hexdigest(),
                                candidate=candidate,context=ctx,row=row,side=side,oid=info['oid'],model=grant['model'],candidate_stat=self.c.saved_descriptor(),deadline=self._clock()+VIEW_LEASE,review_id=grant['token'])
                            self._verify_view(view,fd)
                            self._effect('review-content-promotion-fsync',lambda:os.fsync(fd))
                            receipt_name='completed-'+name[5:]+'.json'
                            os.fsync(fd)
                            durable=dict(schema=1,status='complete',name=name,candidate=candidate,scope=scope,row=row,side=side,
                                oid=info['oid'],size=count,sha256=digest.hexdigest(),stat=receipt,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino],quarantine=[quarantine,qinfo.st_dev,qinfo.st_ino])
                            def publishing():
                                nonlocal publication_started
                                publication_started=True
                            self._save_receipt(fd,receipt_name,durable,None,before_publish=publishing)
                            active={v['name'] for key,v in self._views.items() if key!=ctx}
                            for old_name,old_value in completed:
                                if old_value['name'] not in active:self._retire_receipt(fd,old_name,old_value)
                            if previous:os.close(previous['fd'])
                            self._views[ctx]=view
                            file=None
                            self._schedule()
                        finally:
                            try:
                                if file is not None and not publication_started:
                                    # Promotion is reversible while receipt publication has not begun.
                                    self._discard_owned(fd,name if promoted else pending,file,child,'payload')
                            finally:
                                if file is not None:os.close(file)
                                os.close(child)
                return {k:view[k] for k in ('view_id','size','sha256','candidate','row','side','oid')}
            except BaseException:
                self._challenges.pop(ctx,None)
                raise

    def _text_window(self, view, offset, length):
        """Independent strict UTF-8 window, with at most three neighbor bytes per side."""
        if offset==view['size']:return '',offset,offset
        low=max(0,offset-3);high=min(view['size'],offset+length+3)
        data=os.pread(view['fd'],high-low,low)
        if len(data)!=high-low:raise Conflict('incomplete-content-text-window')
        start=offset-low;end=offset+length-low
        while start>0 and 0x80<=data[start]<=0xbf:start-=1
        while end<len(data) and 0x80<=data[end]<=0xbf:end+=1
        try:text=data[start:end].decode('utf-8',errors='strict')
        except UnicodeDecodeError:text=None
        return text,low+start,low+end

    def content_page(self, *, view_id, offset, context):
        ctx=_context(context)
        with self._mutex:
            self._sweep()
            try:
                view=self._views.get(ctx)
                if (view is None or type(view_id) is not str or not secrets.compare_digest(view_id,view['view_id'])
                        or ctx!=view['context'] or type(offset) is not int or not 0<=offset<=view['size']
                        or self.c.saved_descriptor()!=view['candidate_stat']):
                    raise Conflict('invalid-content-page-reference')
                with self._store() as fd:
                    self._verify_view(view,fd)
                    raw=os.pread(view['fd'],min(PAGE,view['size']-offset),offset)
                    text,text_start,text_end=self._text_window(view,offset,len(raw))
                    self._verify_view(view,fd)
                    if len(raw)!=min(PAGE,view['size']-offset):raise Conflict('incomplete-content-page')
                view['deadline']=self._clock()+VIEW_LEASE
                return dict(view_id=view_id,offset=offset,total=view['size'],data=base64.b64encode(raw).decode(),eof=offset+len(raw)==view['size'],sha256=view['sha256'],text=text,text_start=text_start,text_end=text_end)
            except BaseException:
                self._challenges.pop(ctx,None)
                raise

    def close_content(self, *, context, view_id=None):
        ctx=_context(context)
        with self._mutex:
            self._sweep()
            view=self._views.get(ctx)
            if view is None:return
            if view_id is not None and (type(view_id) is not str or not secrets.compare_digest(view_id,view['view_id'])):
                raise Conflict('invalid-content-view-reference')
            try:
                with self._store() as fd:self._verify_view(view,fd)
            except BaseException:
                self._challenges.pop(ctx,None)
                raise
            finally:
                self._views.pop(ctx,None);_close_handles({ctx:view})
                self._cleanup_debt=bool(self._cleanup_remaining);self._schedule()
            self._retire_inactive()
            self._schedule()
            if self.cleanup_diagnostic:
                self._challenges.pop(ctx,None)
                raise self._cleanup_error


    def forget(self, *, context):
        """Explicit state reset removes authority, leaving evidence for owned cleanup."""
        with self._mutex:
            ctx=_context(context)
            self._challenges.pop(ctx,None)
            view=self._views.pop(ctx,None)
            if view is not None:
                _close_handles({ctx:view});self._cleanup_debt=bool(self._cleanup_remaining)
                self._schedule()
