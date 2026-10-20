"""Explicit dormant whole-file Apply. No startup registration or live adapter."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
import copy
import hashlib
import hmac
import io
import json
import os
import secrets
import stat
import threading
import time
import math
import uuid

import backups
import native_metadata as metadata
import native_preview as preview
from native_disk import NativeDisk, ApplyAdmission, apply_transaction, require_apply
from native_inventory import Blocked
from native_operations import NativeOperationStore, digest, encode_native_path
from native_publication import GitCLITransport, NetworkGitCLITransport, _encode
from native_recovery import RecoveryStore, File, Directory, SourceFile, parse, canonical
from native_services import ServiceStore


@dataclass(frozen=True,eq=False)
class _NoEffectClose:
    issuer: object
    operation: str
    command: str
    revision: int
    participant: bytes
    pin: bytes


class Refused(Blocked):
    pass


def _authority(value):
    kind=type(value)
    if value is None or kind in (bytes,str,int,bool,float):return kind,value
    if is_dataclass(value):return kind,tuple((f.name,_authority(getattr(value,f.name))) for f in fields(value) if f.name not in ('issuer','store'))
    if kind in (tuple,list):return kind,tuple(_authority(v) for v in value)
    if kind is dict:return kind,tuple(sorted(((_authority(k),_authority(v)) for k,v in value.items()),key=repr))
    raise Refused('unsupported-apply-authority-field')


@dataclass(frozen=True, eq=False)
class DirectReceipt:
    issuer: object
    binding: tuple
    session: str
    address: str
    expires: float


class DirectReadiness:
    """Single-use browser status roundtrip, never operation consent.

    Only the optional authenticated DirectServer endpoints issue a challenge.
    Refresh or process restart destroys pending browser authority.
    """
    def __init__(self, *, clock=time.monotonic, ttl=120):
        if type(ttl) not in (int, float) or not 0 < ttl <= 300:
            raise Refused('invalid-direct-readiness-lifetime')
        self.clock, self.ttl = clock, ttl
        self.lock = threading.Lock()
        self.binding = self.pending = self.receipt = self.listener = self._issued_receipt = self._consumed = None
        self.prepared=self.status=None

    def attach_status(self,status):
        from native_status import NativeStatus
        if type(status) is not NativeStatus:raise Refused('qualified-native-status-required')
        self.status=status
        if self.prepared is not None:status.bind_apply(self.prepared)

    def bind(self, operation, review):
        if type(review) is not preview.Review or review.draft.direction != 'apply':
            raise Refused('issued-apply-review-required')
        with self.lock:
            self.binding = (operation, review.draft.snapshot_id, review.draft.source_digest,
                            review.draft.generation, tuple(review.draft.selected))
            self.pending = self.receipt = None
        return self.binding

    def reset(self):
        with self.lock:
            self.binding = self.pending = self.receipt = self.listener = self._issued_receipt = self._consumed = None

    def challenge(self, session, address, status, *, listener=None):
        from direct_access import DirectServer
        with self.lock:
            if (type(listener) is not DirectServer or listener.socket.fileno()<0 or
                    self.binding is None or type(status) is not dict or
                    not {'available','actions_enabled','operation','services','recovery'} <= set(status) or
                    status['available'] is not True or type(status['actions_enabled']) is not bool or
                    (status.get('operation') or {}).get('worker',{}).get('id')!=self.binding[0] or
                    (status.get('recovery') or {}).get('operation')!=self.binding[0] or
                    any(type(status[k]) is dict and status[k].get('status') in ('busy','blocked','failed')
                        for k in ('operation','services','recovery'))):
                raise Refused('direct-status-unavailable')
            self.listener=listener
            challenge = secrets.token_urlsafe(32)
            self.pending = (challenge, session, address, self.binding, self.clock() + self.ttl)
            self.receipt = None
            return dict(challenge=challenge, operation=self.binding[0], status=status)

    def acknowledge(self, session, address, challenge):
        with self.lock:
            p = self.pending
            if (p is None or p[1:4] != (session, address, self.binding) or
                    self.clock() >= p[4] or not hmac.compare_digest(p[0], challenge)):
                raise Refused('direct-readiness-response-refused')
            self.pending = None
            self.receipt = DirectReceipt(self, self.binding, session, address, p[4])
            self._issued_receipt=self.receipt

    def consume(self, binding):
        with self.lock:
            receipt = self.receipt
            if (type(receipt) is not DirectReceipt or receipt is not self._issued_receipt or receipt.issuer is not self or
                    receipt.binding != binding or self.binding != binding or self.clock() >= receipt.expires or
                    self.listener is None or self.listener.socket.fileno()<0 or
                    self.listener._BaseServer__is_shut_down.is_set()):
                raise Refused('authenticated-direct-browser-roundtrip-required')
            self.receipt = self.pending = self.binding = None
            self._consumed=receipt
            return receipt

    def qualify(self,receipt):
        with self.lock:
            if (receipt is not self._consumed or self.listener is None or self.listener.socket.fileno()<0 or
                    self.listener._BaseServer__is_shut_down.is_set() or self.clock()>=receipt.expires):
                raise Refused('direct-browser-interface-no-longer-ready')


@dataclass(frozen=True, eq=False)
class PreparedApply:
    issuer: object
    review: preview.Review
    command: str
    operation: str
    targets: tuple
    preimages: tuple
    advertisement: object
    direct_binding: tuple

    def check(self, *, fresh=True):
        if self.issuer._prepared is not self or self.issuer._prepared_authority != _authority(self):
            raise Refused('current-issued-apply-preparation-required')
        if fresh:
            self.issuer.preview._apply_fence(self.review)
        return self


class _ApplyGuard:
    def __init__(self,worker,prepared,capability):
        self.worker,self.prepared,self.capability=worker,prepared,capability
        self.recovery,self.services=worker.recovery,worker.services
    def check(self):
        require_apply(self.capability,self.worker,self.prepared.operation)
        self.prepared.check(fresh=False)
        if self.capability.purpose!='worker':raise Refused('worker-effect-capability-required')
        from native_disk import qualified_apply
        qualified_apply(self.capability,self.worker)
    def before_effect(self,*args):
        self.check();return self.worker.before_effect(*args,prepared=self.prepared,capability=self.capability)
    def before_dispatch(self,*args):
        self.check();return self.worker.before_dispatch(*args,prepared=self.prepared,capability=self.capability)
    def before_service_command(self,*args):
        self.check();return self.worker.before_service_command(*args,capability=self.capability)


class NativeApply:
    """Browser-independent explicit worker over exact native participants.

    The caller supplies an existing qualified transport and bounded service
    adapter; this module never constructs a live Supervisor or backup client.
    """
    def __init__(self, session, disk, recovery, services, operations, transport, direct):
        if (type(session) is not preview.NativePreview or type(disk) is not NativeDisk or
                type(recovery) is not RecoveryStore or type(services) is not ServiceStore or
                type(operations) is not NativeOperationStore or
                type(transport) not in (GitCLITransport, NetworkGitCLITransport) or
                type(direct) is not DirectReadiness or recovery.disk is not disk or
                recovery.binding != session.binding or disk.root != session.binding.root):
            raise Refused('exact-native-apply-participants-required')
        self.preview, self.disk, self.recovery = session, disk, recovery
        self.services, self.operations, self.transport, self.direct = services, operations, transport, direct
        self._prepared = None
        self._warnings = {}
        self._running = threading.Lock()
        self._resources={}

    def _transaction(self,operation,purpose):
        return apply_transaction(self.preview.binding,(self,self.disk,self.recovery,self.services,self.operations),operation,purpose)

    def _remote(self, review):
        advertisement = self.transport.advertise()
        source = self.preview._apply_fence(review).source
        if advertisement.oid != source.remote_oid:
            raise Refused('reviewed-remote-ref-changed')
        # Transport qualification binds endpoint and exact destination; a remote
        # tracking alias must name that same branch, never any advertised ref.
        expected = source.remote_ref
        if expected.startswith('refs/remotes/origin/'):
            expected = 'refs/heads/' + expected[len('refs/remotes/origin/'):]
        if advertisement.destination != expected:
            raise Refused('reviewed-remote-destination-changed')
        return advertisement

    def _live(self, path):
        state,routes=preview._live_file(self.preview.binding,path,self.preview.git._config())
        return state,None,routes

    def _spool(self, producer, size, record, *, capability=None):
        require_apply(capability,self,self._spool_operation)
        if capability.purpose not in ('prepare','worker'):raise Refused('allocating-apply-capability-required')
        relative=b'data/native/apply/'+self._spool_operation.encode()+b'/'+uuid.uuid4().hex.encode()
        self._admission.extend(((relative,size,'0'*64,'apply-spool'),),capability=capability)
        # Acquire every private namespace component without following links.
        parent=os.path.dirname(relative)
        prefix=b''
        for component in parent.split(b'/'):
            with metadata._opened(self.preview.binding,prefix,'directory') as (fd,_):
                try:os.mkdir(component,0o700,dir_fd=fd);os.fsync(fd)
                except FileExistsError:pass
                info=os.stat(component,dir_fd=fd,follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode) or
                        prefix in (b'data/native',b'data/native/apply') and
                        (info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700)):
                    raise Refused('unsafe-apply-spool-parent')
            prefix=prefix+(b'/' if prefix else b'')+component
        with metadata._opened(self.preview.binding,parent,'directory') as (fd,_):
            child=os.open(os.path.basename(relative),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            identity=(os.fstat(child).st_dev,os.fstat(child).st_ino)
            try:
                with os.fdopen(child,'wb',closefd=False) as sink:
                    class BoundedSink:
                        def __init__(self):self.count=0
                        def write(self,data):
                            if len(data)>size-self.count:raise Refused('apply-source-growth-exceeds-qualified-demand')
                            n=sink.write(data);self.count+=n;return n
                        def fileno(self):return sink.fileno()
                    producer(BoundedSink())
                    sink.flush()
                    if os.fstat(child).st_size>size:raise Refused('apply-source-growth-exceeds-qualified-demand')
                    os.fchown(child,record.uid,record.gid);os.fchmod(child,record.mode);os.fsync(child)
                os.fsync(fd)
            except BaseException as error:
                try:
                    actual=os.stat(os.path.basename(relative),dir_fd=fd,follow_symlinks=False)
                    if (actual.st_dev,actual.st_ino)!=identity:raise Refused('foreign-failed-apply-spool')
                    os.unlink(os.path.basename(relative),dir_fd=fd);os.fsync(fd)
                except BaseException as cleanup:
                    error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('failed-apply-spool:'+type(cleanup).__name__,)
                raise
            finally:os.close(child)
        source=self.recovery.source_file(relative)
        self._spools.append(source)
        self._admission.record_source(source,capability=capability)
        return source

    def _capture_live(self,path,*,capability=None):
        expected,_,_=self._live(path)
        if expected is None:return None,None
        def copy(sink):
            actual,_=preview._live_file(self.preview.binding,path,self.preview.git._config(),sink)
            if actual!=expected:raise Refused('live-source-changed-during-capture')
        return expected,self._spool(copy,expected.size,expected.record,capability=capability)

    def _git_source(self,state,*,capability=None):
        if state is None:return None
        self.preview.git._object(state.oid,b'blob')
        declared=self.preview.git._run(('cat-file','-s',state.oid))
        if not declared.endswith(b'\n') or not declared[:-1].isdigit():raise Refused('invalid-approved-blob-size')
        size=int(declared)
        return self._spool(lambda sink:self.preview.git.copy_blob(state.oid,sink),size,state.record,capability=capability)

    def _copy_source(self,source,sink):
        if source is None:return
        source.check()
        self.recovery._emit(__import__('native_recovery').Payload(None,source.source,source.state),sink.fileno())

    def _source_equal(self,a,b):
        if a is None or b is None:return a is b
        aa,bb=parse(a.state),parse(b.state)
        return (aa['size'],aa['digest'])==(bb['size'],bb['digest'])

    def _merge_sources(self,path,current,base,target,record,*,capability=None):
        if self._source_equal(current,base) or self._source_equal(current,target):chosen=target
        elif self._source_equal(target,base):chosen=current
        else:
            if current is None or base is None or target is None:raise Refused('apply-content-conflict')
            if os.stat(self.preview.scratch_root).st_dev!=os.stat(self.disk.root).st_dev:
                raise Refused('qualified-apply-scratch-volume-required')
            sizes=[parse(s.state)['size'] for s in (current,base,target)]
            # Each input byte can be a line. Fixed diff3 markers/labels are
            # bounded per line, independent of content and without a size cap.
            bound=sum(sizes)+96*(sum(sizes)+3)
            self._admission.extend(tuple((b'data/native/merge/'+str(i).encode(),size,'0'*64,'apply-merge') for i,size in enumerate(sizes+[bound,0])),capability=capability)
            with preview._Scratch(self.preview) as scratch:
                for name,source in (('before',current),('base',base),('after',target)):
                    with scratch.file(name) as sink:
                        self._copy_source(source,sink);sink.flush();sink.seek(0)
                        while True:
                            chunk=sink.read(65536)
                            if not chunk:break
                            if b'\0' in chunk:raise Refused('apply-binary-concurrent-conflict')
                code,error=self.preview._git_tool(scratch,['merge-file','--stdout','--diff3','-L','Live','-L','Base','-L','Git','before','base','after'],'merged')
                if code!=0 or error:raise Refused('apply-content-conflict')
                with scratch.read('merged') as result:
                    size=os.fstat(result.fileno()).st_size
                    if size>bound:raise Refused('merge-output-exceeded-qualified-demand')
                    def copy(sink):
                        while True:
                            chunk=result.read(65536)
                            if not chunk:break
                            sink.write(chunk)
                    return self._spool(copy,size,record,capability=capability)
        if chosen is None:return None
        return self._spool(lambda sink:self._copy_source(chosen,sink),parse(chosen.state)['size'],record,capability=capability)

    @staticmethod
    def _attributes(base, live, target):
        def attrs(record):
            return None if record is None else (record.kind, record.uid, record.gid, record.mode)
        b, l, t = map(attrs, (base, live, target))
        if l == t or l == b:
            return target
        if t == b:
            return live
        raise Refused('apply-metadata-conflict')

    def _claimed(self):
        return (self._prepared is not None and self._prepared.operation==getattr(self,'_spool_operation',None) and (self.operations.data_root/'native-operations/state.json').is_file() and
                self._prepared.operation in self.operations.read(observe=True)['workers'])

    def prepare(self,review,command_id):
        # Remote step zero precedes retirement of even a previous local draft.
        advertisement=self._remote(review)
        operation=str(uuid.uuid4())
        with self._transaction(operation,'prepare') as capability:
            if getattr(self,'_draft_resources',False):
                old=self._spool_operation
                # A separate exact operation capability is required for old debt.
                # Delegate under this same held descriptor, never reopen flock.
                self._retire_previous(capability,old)
            try:return self._prepare(review,command_id,operation,advertisement,capability)
            except BaseException as error:
                if getattr(self,'_draft_resources',False) and self._spool_operation==operation:
                    try:self._cleanup_spools(operation,capability=capability)
                    except BaseException as cleanup:error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('apply-preparation-cleanup-debt:'+type(cleanup).__name__,)
                raise

    def _retire_previous(self,capability,operation):
        from native_disk import delegate_apply
        with delegate_apply(capability,operation,'retire') as retirement:
            self._cleanup_spools(operation,capability=retirement)

    def _prepare(self, review, command_id, operation, advertisement, capability):
        if str(uuid.UUID(command_id)) != command_id:
            raise Refused('invalid-apply-command')
        # Actual advertisement is the first observation. No recovery/service or
        # backup participant is touched on unreachable/stale remote.
        with self.preview.apply_context(review) as observed:
            if review.draft.resolutions and all(dict(review.draft.resolutions).get(row.path)=="ha" for row in review.selected):
                raise Refused("whole-file-choices-have-no-apply-effects")
            if self.recovery._pointer() is not None:
                raise Refused('retained-recovery-point-blocks-apply')
            if ((self.operations.data_root/'native-operations/state.json').is_file() and
                    self.operations.read(observe=True)['recovery'] is not None):
                raise Refused('retained-apply-operation-debt-blocks-apply')
            if (self.operations.data_root/'native-operations/state.json').is_file() and any((w.get('apply_result') or {}).get('no_effect') is True and not (w.get('apply_result') or {}).get('close_command') for w in self.operations.read(observe=True)['workers'].values()):raise Refused('unclosed-no-effect-operation-blocks-apply')
            prior_service=self.services.discover()
            if prior_service is not None and prior_service['phase']!='restored':
                raise Refused('retained-service-lifecycle-blocks-apply')
            self._admission=ApplyAdmission(self.disk,self.disk.snapshot(),operation=operation,capability=capability)
            capsule=canonical(self.recovery.cleanup_draft_template(operation))
            self._admission.extend(((self.recovery.directory+b'/authority.key',32,'0'*64,'apply-draft-key'),(self.recovery.directory+b'/lock',0,'0'*64,'apply-draft-lock'))+tuple((self.recovery.directory+b'/apply-draft-'+operation.encode()+b'.json',len(capsule),'0'*64,'apply-draft-authority') for _ in range(2)),publish=False,capability=capability)
            self.recovery.register_cleanup_draft(self._admission,capability=capability)
            self._draft_resources=True
            self._spool_operation=operation
            self._spools=[]
            self._resources[operation]=dict(admission=self._admission,sources=self._spools)
            targets, preimages = {}, {}
            for row in review.selected:
                live, raw = self._capture_live(row.path,capability=capability)
                if live != row.before:
                    raise Refused('reviewed-live-preimage-changed')
                remote, base = self._git_source(row.after,capability=capability), self._git_source(row.base,capability=capability)
                choice=dict(review.draft.resolutions).get(row.path)
                if choice is not None:
                    preimages[row.path]=(live,raw)
                    if choice=="git":targets[row.path]=remote
                    continue
                record = self._attributes(row.base.record if row.base else None,
                                          live.record if live else None, row.after.record if row.after else None)
                targets[row.path] = self._merge_sources(row.path, raw, base, remote, record,capability=capability)
                preimages[row.path] = (live, raw)
            for parent in review.parents:
                if review.draft.resolutions and all(dict(review.draft.resolutions).get(path)=="ha" for path in parent.dependents if path in review.draft.selected):continue
                if parent.path not in review.draft.approved_parents or (parent.issues and not (parent.after is not None and set(parent.issues)<={"existence"} and any(q in dict(review.draft.resolutions) for q in parent.dependents))):
                    raise Refused('explicit-reviewed-parent-required')
                if parent.after is not None:
                    r = parent.after
                    targets[parent.path] = Directory(r.uid, r.gid, r.mode)
            binding = self.direct.bind(operation, review)
            prepared = PreparedApply(self, review, command_id, operation, tuple(sorted(targets.items())),
                                     tuple(sorted(preimages.items())), advertisement, binding)
            self._prepared = prepared
            self._prepared_authority=_authority(prepared)
            self.direct.prepared=prepared
            if self.direct.status is not None:self.direct.status.bind_apply(prepared)
            return prepared

    def reset(self, reason='refresh'):
        operation=getattr(self,'_spool_operation',str(uuid.UUID(int=0)))
        with self._transaction(operation,'reset') as capability:
            if getattr(self,'_draft_resources',False):self._cleanup_spools(operation,capability=capability)
            self.preview.reset(reason);self.direct.reset();self._prepared=None;self._warnings.clear()

    def _backup(self, prepared, options, inventory, *, bypass=False):
        def integer(opts, name, default, minimum=1):
            value = opts.get(name, default)
            if type(value) is not int or value < minimum:
                raise Refused('invalid-backup-policy')
            return value
        def boolean(opts, name, default):
            value = opts.get(name, default)
            if type(value) is not bool:
                raise Refused('invalid-backup-policy')
            return value
        policy = (integer(options, 'backup_max_age_hours', 24), boolean(options, 'backup_require_location', True))
        key = (prepared.command, prepared.operation, prepared.review.draft.snapshot_id,
               prepared.review.draft.generation, policy)
        status = backups.latest_system_backup_status(options, 24, integer, boolean, inventory)
        if not status['available']:
            raise Refused('backup-inventory-unavailable')
        if not status['stale']:
            self._warnings.pop(key, None)
            return backups.backup_slug(status['backup'])
        if status['refusal_reason'] not in ('missing', 'stale'):
            raise Refused('invalid-backup-classification')
        if bypass:
            if self._warnings.pop(key, None) is not True:
                raise Refused('matching-one-use-backup-warning-required')
            return None
        self._warnings[key] = True
        raise Refused('backup-' + status['refusal_reason'] + ':explicit-recheck-or-bypass-required')

    def _identity(self, prepared, recovery):
        observed=self.preview._apply_fence(prepared.review)
        source=observed.source
        root=os.stat(self.preview.binding.root,follow_symlinks=False)
        paths=[]
        for path,target in prepared.targets:
            paths.append(dict(path=encode_native_path(path),type='missing' if target is None else
                ('file' if type(target) in (File,SourceFile) else 'directory'),
                mode=None if target is None else (stat.S_IFREG if type(target) in (File,SourceFile) else stat.S_IFDIR)|target.mode,
                content_digest=parse(target.state)['digest'] if type(target) is SourceFile else (hashlib.sha256(target.data).hexdigest() if type(target) is File else None),
                metadata_digest=None if target is None else digest([target.uid,target.gid,target.mode])))
        ref=dict(name=source.remote_ref,oid=source.remote_oid)
        identity=dict(direction='apply',execution_policy='native-apply-v1',
            scope=dict(revision=0,allowlist=sorted(encode_native_path(p) for p in source.scope),
                       exclude_digest=self.preview.binding.policy.digest),
            refs=dict(base=dict(name=source.base_ref or source.remote_ref,oid=source.base_oid or source.remote_oid),
                      local=ref,remote=dict(name=self.transport.destination,oid=source.remote_oid)),
            root=dict(path=encode_native_path(self.disk.root,absolute=True),device=root.st_dev,inode=root.st_ino,
                      mount_digest=digest(_encode(self.preview.binding.mounts)),topology_digest=digest(_encode(self.preview.binding))),
            paths=sorted(paths,key=lambda p:p['path']),
            effect=dict(before=prepared.review.draft.source_digest,
                        after=digest([paths,hashlib.sha256(recovery.manifest).hexdigest(),prepared.operation,self.operations.owner])))
        if self.preview.discovery_store is not None:
            effective,baseline,_=self.operations.discovery_policy(self.preview.binding.policy.digest)
            if source.scope_revision!='discovery:'+str(effective['revision'])+':'+digest(effective):raise Refused('discovery-changed-during-identity')
            identity['discovery']=effective
            identity['scope']=baseline or dict(revision=0,allowlist=[],exclude_digest=self.preview.binding.policy.digest)
        return identity

    @staticmethod
    def _service_identity(state):
        return state['operation'],state['owner'],state['revision']

    def _checkpoint(self, worker, phase, identity=None, result=None, *, capability=None):
        state=self.operations.read()
        self._qualify_operation_control(capability,state,worker=worker,identity=identity,result=result)
        return self.operations.checkpoint(state['revision'],worker['id'],worker['epoch'],phase,
            observation=None if identity is None else dict(available=True,identity=identity),
            evidence=dict(identity['effect'],marker=worker['marker']) if phase=='completed' else None,apply_result=result,capability=capability)

    def _qualify_operation_control(self,capability,state,**changes):
        # Schema-independent envelope: serializing the complete existing state
        # and all replacement values bounds a single atomic state publication.
        raw=canonical(dict(state=state,changes=changes))
        size=len(raw)+len(canonical(dict(revision=2**64-1,epoch=2**64-1)))
        self._admission.extend(tuple((b'data/native-operations/late-'+str(i).encode(),size,'0'*64,'apply-exact-operation-control') for i in range(2)),capability=capability)

    def _remaining(self):
        remaining=self._deadline-time.monotonic()
        if remaining<=0:raise Refused('apply-deadline-exceeded')
        return remaining

    def run(self,prepared,adapter,backup_inventory,**options):
        with self._transaction(prepared.operation,'worker') as capability:
            return self._run(prepared,adapter,backup_inventory,capability=capability,**options)

    def _run(self, prepared, adapter, backup_inventory, *, options=None, bypass_backup=False, seconds=120,recovery_seconds=30,capability):
        """Explicit accepted worker; refresh/auth/reconnect never calls this method."""
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (seconds,recovery_seconds)):
            raise Refused('finite-positive-apply-budgets-required')
        if not self._running.acquire(False):
            raise Refused('apply-worker-busy')
        worker=service=None
        installed=False
        healthy=False
        try:
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (seconds,recovery_seconds)):
                raise Refused('finite-positive-apply-budgets-required')
            self._deadline=time.monotonic()+seconds
            prepared.check(fresh=False)
            if (self.operations.data_root/'native-operations/state.json').is_file():
                previous=self.operations.read(observe=True)['workers'].get(prepared.operation)
                if previous is not None:
                    return copy.deepcopy(previous['apply_result']) if previous['apply_result'] is not None else dict(
                        status='ERROR',operation=prepared.operation,error='explicit-recovery-required',recovery_retained=True)
            prepared.check()
            # First external observation for every explicit attempt.
            if self._remote(prepared.review)!=prepared.advertisement:
                raise Refused('reviewed-remote-route-or-ref-changed')
            if (self.operations.data_root/'native-operations/state.json').is_file():
                previous=self.operations.read(observe=True)['workers'].get(prepared.operation)
                if previous is not None:
                    return copy.deepcopy(previous['apply_result']) if previous['apply_result'] is not None else dict(
                        status='ERROR',operation=prepared.operation,error='explicit-recovery-required',recovery_retained=True)
            self._backup(prepared,options or {},backup_inventory,bypass=bypass_backup)
            self._remaining()
            with self.preview.apply_context(prepared.review,reviewed_live=False), self.operations.apply_authority(prepared,capability=capability):
                if self.recovery._pointer() is not None:
                    raise Refused('retained-recovery-point-blocks-apply')
                with __import__('contextlib').nullcontext():
                    authority=self.recovery.cleanup_draft_authority(prepared.operation)
                    if self.recovery._cleanup_draft(prepared.operation)['phase']!='draft':raise Refused('retiring-draft-cannot-be-claimed')
                    if authority['namespace']!=list(self._admission._directory_identity) or authority['key']!=self._admission._cleanup_key.hex():raise Refused('draft-authority-changed-before-claim')
                    for source in self._spools:source.check()
                    # Capture current selected inputs again before installing the
                    # point. Compatible concurrent edits remain part of forward data.
                    capture={}
                    targets=self._current_targets(prepared,capability=capability,capture=capture)
                    recovery=self.recovery.prepare(targets,operation_id=prepared.operation,owner_id=self.operations.owner,apply_authority=prepared,capability=capability,capture=capture)
                    identity=self._identity(prepared,recovery)
                    from native_disk import bind_apply_identity
                    bind_apply_identity(capability,self.operations,identity)
                    prior_service=self.services.discover()
                    artifact=json.dumps(dict(resources=self._admission.authority(),prior_service=prior_service,command=prepared.command,operation=prepared.operation,
                        recovery_generation=recovery.check()['generation'],service_operation=prepared.operation,
                        service_owner=self.operations.owner,review=prepared.review.draft.snapshot_id),sort_keys=True).encode()
                    initial_size=len(canonical(self.operations._initial_state()))
                    self._admission.extend(((b'data/native-operations/lock',0,'0'*64,'apply-operation-lock'),
                        (b'data/native-operations/state.json',initial_size,'0'*64,'apply-operation-bootstrap'),
                        (b'data/native-operations/artifacts/bootstrap',0,'0'*64,'apply-operation-artifacts-directory')),capability=capability)
                    current=self.operations.read()
                    # Exact current state plus three bounded worker/checkpoint
                    # generations and the full selected identity. No count ceiling.
                    control=len(json.dumps(current,sort_keys=True).encode())+8*(len(json.dumps(identity).encode())+len(artifact)+4096)
                    self._admission.extend(tuple((b'data/native-operations/control-'+str(i).encode(),control,'0'*64,'apply-operation-control') for i in range(2)),capability=capability)
                    record=self.operations.preview(current['revision'],'apply',identity,artifact,capability=capability)
                    record=self.operations.review(self.operations.read()['revision'],record['id'],
                        {encode_native_path(p):'git' for p in prepared.review.draft.selected if p in dict(prepared.targets)},str(uuid.uuid4()),dict(available=True,identity=identity),capability=capability)
                    _,worker=self.operations.claim(self.operations.read()['revision'],prepared.command,record['id'],
                        record['approval']['binding'],dict(available=True,identity=identity),
                        instance_id=record['instance_id'],generation=record['generation'],capability=capability)
                    self.operations.recovery_pin(self.operations.read()['revision'],self.operations.read()['recovery'],record['id'],record['identity_digest'],capability=capability)
                prior_service=self.services.discover()
                replacement=None if prior_service is None else self._service_identity(prior_service)
                service=self.services.prepare(adapter,operation_id=prepared.operation,owner_id=self.operations.owner,
                                              admission=self._admission,seconds=self._remaining(),replace=replacement,capability=capability)
                self.recovery.install(recovery,admission=self._admission,capability=capability)
                installed=True
                self._checkpoint(worker,'effect_started',identity,capability=capability)
                while True:
                    try:self._direct_receipt=self.direct.consume(prepared.direct_binding);break
                    except Refused:
                        if time.monotonic()>=self._deadline:raise Refused('authenticated-direct-browser-roundtrip-required')
                        time.sleep(.01)
                service=self.services.stop(*self._service_identity(service),adapter,seconds=self._remaining(),guard=_ApplyGuard(self,prepared,capability))
                if service['phase']!='stopped':
                    raise Refused('service-stop-incomplete')
                self._remote(prepared.review)
                self.recovery.execute(guard=_ApplyGuard(self,prepared,capability))
                validation=adapter.validate_core(timeout=self._remaining())
                self._remaining()
                if type(validation) is not dict or validation!={'valid':True}:
                    raise Refused('core-validation-failed')
                service=self.services.restore(*self._service_identity(service),adapter,seconds=self._remaining())
                self._remaining()
                if service['phase']!='restored':
                    raise Refused('service-readiness-failed')
                residual=self._residual(prepared)
                self._remaining()
                result=dict(status='WARNING' if residual else 'SUCCESS',operation=prepared.operation,residual=residual,selected=[])
                pending=dict(status='ERROR',operation=prepared.operation,error='healthy-apply-cleanup-pending',
                             cleanup_debt=True,recovery_retained='unknown',healthy_status=result['status'],residual=residual,selected=[])
                self._qualify_operation_control(capability,self.operations.read(),result=result,pending=pending)
                self._checkpoint(worker,'completed',identity,pending,capability=capability)
                healthy=True
                # A retained point is closed only after durable healthy outcome.
                self.recovery.retire(prepared,revision=self.recovery.discover()['journal']['revision'],capability=capability)
                self._cleanup_spools(capability=capability)
                self._remaining()
                return self.operations.apply_cleanup_result(prepared,result,release=True,capability=capability)
        except Exception as error:
            recovery_errors=[]
            recovery_deadline=time.monotonic()+recovery_seconds
            def recovery_remaining():
                left=recovery_deadline-time.monotonic()
                if left<=0:raise Refused('apply-recovery-deadline-exceeded')
                return left
            if worker is not None:
                if healthy:
                    debt=dict(pending,error='healthy-apply-cleanup-debt')
                    try:debt['recovery_retained']=self.recovery._pointer() is not None
                    except Exception:pass
                    try:return self.operations.apply_cleanup_result(prepared,debt,capability=capability)
                    except Exception:return copy.deepcopy(self.operations.read()['workers'][prepared.operation]['apply_result'])
                try:
                    service=self.services.discover()
                    if service is not None and (service['operation']!=prepared.operation or service['owner']!=self.operations.owner):service=None
                    if service and any(e['start'] is not None for e in service['entries']):
                        service=self.services.compensate(*self._service_identity(service),adapter,seconds=recovery_remaining())
                        if service['phase']!='stopped':
                            raise Refused('compensating-stop-incomplete')
                    if installed:
                        if service is None or service['phase']!='stopped':
                            raise Refused('rollback-services-not-quiescent')
                        self.recovery.rollback(operation_id=prepared.operation,owner_id=self.operations.owner,capability=capability)
                except Exception as recovery_error:
                    recovery_errors.append(type(recovery_error).__name__)
                try:
                    if service is not None:
                        restored=self.services.restore(*self._service_identity(self.services.discover()),adapter,seconds=recovery_remaining())
                        if restored['phase']!='restored':
                            recovery_errors.append('service-restoration-incomplete:'+restored['phase'])
                except Exception as service_error:
                    recovery_errors.append(type(service_error).__name__)
                try:
                    with self.operations.apply_authority(prepared,capability=capability):
                        phase=self.operations.read()['workers'][prepared.operation]['phase']
                        self._checkpoint(worker,'failed' if phase=='accepted' else 'recovery_required',result=dict(status='ERROR',operation=prepared.operation,no_effect=phase=='accepted',
                            error=str(error) if type(error) is Refused else type(error).__name__,recovery_errors=recovery_errors,recovery_retained=installed),capability=capability)
                except Exception as journal_error:
                    recovery_errors.append(type(journal_error).__name__)
                durable=self.operations.read()['workers'][prepared.operation].get('apply_result')
                return copy.deepcopy(durable) if durable is not None else dict(status='ERROR',operation=prepared.operation,error=str(error) if type(error) is Refused else type(error).__name__,
                            recovery_errors=recovery_errors,recovery_retained=installed)
            raise
        finally:
            self._running.release()

    def _cleanup_spools(self,operation=None,*,capability=None):
        operation=operation or self._spool_operation
        require_apply(capability,self,operation,deleting=True)
        state=self.operations.read(observe=True) if (self.operations.data_root/'native-operations/state.json').is_file() else None
        worker=None if state is None else state['workers'].get(operation)
        if worker is not None:
            if capability.purpose not in ('worker','close'):raise Refused('claimed-operation-requires-reviewed-close')
            if capability.purpose=='worker' and (worker['phase']!='completed' or not (worker.get('apply_result') or {}).get('cleanup_debt')):raise Refused('durable-terminal-retirement-required')
            if capability.purpose=='close':
                no_effect=worker['phase']=='accepted' or (worker.get('apply_result') or {}).get('no_effect') is True
                if no_effect:
                    self._no_effect_proof(state,operation,self.services.discover())
                    approvals=getattr(self,'_abandon_issued',{}).values()
                    if not any(a[1]==operation for a in approvals):raise Refused('explicit-no-effect-close-required')
                else:
                    receipt=parse(self.recovery._read(b'closed.json'))
                    checked=self.recovery.closed_receipt(receipt['command'])
                    if checked['operation']!=operation:raise Refused('closed-retirement-operation-changed')
        self.recovery.begin_draft_retirement(operation,capability=capability)
        resources=self._resources.get(operation)
        if resources is None:
            state=self.operations.read() if (self.operations.data_root/'native-operations/state.json').is_file() else None
            worker=None if state is None else state['workers'].get(operation)
            if worker is not None:authority=json.loads(self.operations.read_diff(worker['record_id']))['resources']
            elif getattr(self,'_admission',None) is not None and self._admission.operation==operation:
                self._admission.check();authority=self._admission.authority()
            else:
                try:authority=self.recovery.cleanup_draft_authority(operation)
                except FileNotFoundError:
                    if os.path.lexists(self.disk.root+b'/data/native/apply/'+operation.encode()):raise Refused('missing-durable-draft-authority')
                    return
            admission=ApplyAdmission.reopen_cleanup(self.disk,operation,authority,capability=capability)
            if admission is None:
                self.recovery.retire_cleanup_draft(operation,capability=capability)
                return
            sources=[]
            for key,state in admission._receipts.items():
                source=SourceFile(self.recovery,bytes.fromhex(key),canonical(state))
                self.recovery._source_issued[id(source)]=(source,source.source,source.state)
                sources.append(source)
            resources=dict(admission=admission,sources=sources);self._resources[operation]=resources
        admission,sources=resources['admission'],resources['sources']
        while sources:
            source=sources[0]
            phase=admission.cleanup_intent(source,capability=capability)
            if phase=='removed':sources.pop(0);continue
            parent,_,name=source.source.rpartition(b'/')
            with metadata._opened(self.preview.binding,parent,'directory') as (fd,_):
                actual=self.recovery._observe(source.source)
                if actual is not None:
                    if actual!=parse(source.state):raise Refused('changed-cleanup-spool')
                    os.unlink(name,dir_fd=fd)
                os.fsync(fd)
            admission.cleanup_ack(source,capability=capability)
            sources.pop(0)
        admission.cleanup(capability=capability)
        self.recovery.retire_cleanup_draft(operation,capability=capability)
        self._resources.pop(operation,None)
        if operation==getattr(self,'_spool_operation',None):self._draft_resources=False

    def cleanup_drafts(self):
        """Read-only discovery; this never restores Review or Apply consent."""
        state=self.operations.read(observe=True) if (self.operations.data_root/'native-operations/state.json').is_file() else None
        claimed=set() if state is None else set(state['workers'])
        return [item for item in self.recovery.cleanup_drafts() if item['operation'] not in claimed]

    def cleanup_draft(self,operation):
        """Explicit cleanup-only action for an unclaimed durable draft."""
        if str(uuid.UUID(operation))!=operation:raise Refused('invalid-cleanup-draft-operation')
        with self._transaction(operation,'retire') as capability:
            if (self.operations.data_root/'native-operations/state.json').is_file() and operation in self.operations.read()['workers']:raise Refused('claimed-operation-requires-reviewed-close')
            try:
                if self.recovery.retire_unallocated_cleanup_draft(operation,capability=capability):return
            except FileNotFoundError:
                if os.path.lexists(self.disk.root+b'/data/native/apply/'+operation.encode()):raise Refused('missing-durable-draft-authority')
                return
            self._cleanup_spools(operation,capability=capability)

    def command_status(self,command_id):
        """Reconnect diagnosis only; no consent, claim, effects or recovery."""
        if str(uuid.UUID(command_id))!=command_id:raise Refused('invalid-apply-command')
        state=self.operations.read(observe=True)
        matches=[r for r in state['commands'].values() if r['command_id']==command_id]
        if not matches:return None
        worker=state['workers'][matches[-1]['operation_id']]
        return copy.deepcopy(worker.get('apply_result')) or dict(status='ERROR',operation=worker['id'],error='explicit-recovery-required')

    def _no_effect_proof(self,state,operation,service):
        worker=state['workers'].get(operation)
        if worker is None or not (worker['phase']=='accepted' or worker['phase'] in ('failed','recovery_required') and (worker.get('apply_result') or {}).get('no_effect') is True):raise Refused('no-effect-worker-proof-required')
        record=state['records'][worker['record_id']]
        expected={'record_id':record['id'],'identity_digest':record['identity_digest']}
        if record['identity']['execution_policy']!='native-apply-v1' or state['active'] not in (None,operation) or state['recovery'] not in (None,expected):raise Refused('no-effect-operation-fence-changed')
        intent=json.loads(self.operations.read_diff(record['id']))
        if service is not None:
            if service['operation']!=operation:
                if service!=intent['prior_service'] or service['phase']!='restored':raise Refused('prior-service-participant-changed')
            elif (service['owner']!=intent['service_owner'] or service['phase'] not in ('prepared','restored') or service['pending'] is not None or service.get('compensation') is not None or any(e['stop'] is not None or e['start'] is not None or e['uncertain'] is not None for e in service['entries'])):raise Refused('service-effects-prevent-abandonment')
        return worker

    def close_recovery(self,approval):
        with self._transaction(approval.operation,'close') as capability:
            return self._close_recovery(approval,capability=capability)

    def _close_recovery(self,approval,*,capability):
        if type(approval) is _NoEffectClose:
            if getattr(self,'_abandon_issued',{}).get(id(approval))!=(approval,approval.operation,approval.command,approval.revision,approval.participant,approval.pin):raise Refused('issued-no-effect-close-required')
            state=self.operations.read()
            if state['revision']!=approval.revision:raise Refused('stale-no-effect-close')
            service=self.services.discover()
            self._no_effect_proof(state,approval.operation,service)
            if self.recovery.discover() is not None or canonical(service)!=approval.participant or canonical(state['recovery'])!=approval.pin:raise Refused('no-effect-participants-changed')
            if service is not None and service['operation']==approval.operation and service['phase']=='prepared':
                self.services.abandon_prepared(*self._service_identity(service))
            self.recovery.cleanup_no_effect(approval.operation,json.loads(self.operations.read_diff(state['workers'][approval.operation]['record_id']))['service_owner'],capability=capability)
            self._cleanup_spools(approval.operation,capability=capability)
            result=self.operations.apply_abandoned(self,approval,capability=capability)
            self._abandon_issued.pop(id(approval),None)
            return result
        service=self.services.discover()
        if service is not None and (service['phase']!='restored' or service['operation']!=approval.operation):
            raise Refused('restore-original-services-before-reviewed-close')
        self.recovery.close(approval,capability=capability)
        return self._resolve_closed_recovery(approval.command,capability=capability)

    def review_cleanup(self,operation_id,command_id):
        with self._transaction(operation_id,'close') as capability:
            return self._review_cleanup(operation_id,command_id,capability=capability)

    def _review_cleanup(self,operation_id,command_id,*,capability):
        state=self.operations.read();worker=state['workers'].get(operation_id)
        service=self.services.discover()
        if worker is None:raise Refused('current-command-worker-required')
        point=self.recovery.discover()
        if point is None and (worker['phase']=='accepted' or worker['phase'] in ('failed','recovery_required') and (worker.get('apply_result') or {}).get('no_effect') is True):
            self._no_effect_proof(state,operation_id,service)
            if str(uuid.UUID(command_id))!=command_id:raise Refused('invalid-close-command')
            approval=_NoEffectClose(self,operation_id,command_id,state['revision'],canonical(service),canonical(state['recovery']))
            if not hasattr(self,'_abandon_issued'):self._abandon_issued={}
            self._abandon_issued[id(approval)]=(approval,operation_id,command_id,state['revision'],approval.participant,approval.pin)
            return approval
        if service is not None and (service['operation']!=operation_id or service['phase']!='restored'):raise Refused('restored-command-participants-required')
        if point is None:return self.recovery.review_absent_close(self,operation_id=operation_id,command_id=command_id)
        if point['manifest']['operation']!=operation_id:raise Refused('cleanup-recovery-operation-changed')
        return self.recovery.review_close(operation_id=operation_id,owner_id=point['manifest']['owner'],
            revision=point['journal']['revision'],command_id=command_id)

    def resolve_closed_recovery(self,command_id):
        # Completed close replay is observation, independent of mutable point
        # receipts or the lease owned by a later unrelated operation.
        if str(uuid.UUID(command_id))!=command_id:raise Refused('invalid-close-command')
        state=self.operations.read(observe=True)
        completed=next((w for w in state['workers'].values()
            if w['phase'] in ('failed','completed') and
            state['records'][w['record_id']]['identity']['execution_policy']=='native-apply-v1' and
            (w.get('apply_result') or {}).get('close_command')==command_id),None)
        if completed is not None:return copy.deepcopy(completed['apply_result'])
        value=parse(self.recovery._read(b'closed.json'))
        with self._transaction(value['operation'],'close') as capability:
            return self._resolve_closed_recovery(command_id,capability=capability)

    def _resolve_closed_recovery(self,command_id,*,capability):
        """Explicitly finish a recorded close after a coordinator interruption."""
        state=self.operations.read()
        completed=next((w for w in state['workers'].values() if (w.get('apply_result') or {}).get('close_command')==command_id),None)
        if completed is not None:return copy.deepcopy(completed['apply_result'])
        receipt=self.recovery.resume_close(command_id,capability=capability)
        worker=state['workers'].get(receipt['operation'])
        if worker is not None and (worker.get('apply_result') or {}).get('close_command')==command_id:
            return copy.deepcopy(worker['apply_result'])
        self._cleanup_spools(receipt['operation'],capability=capability)
        return self.operations.apply_closed(self,command_id,capability=capability)

    def _current_targets(self, prepared,*,capability=None,capture=None):
        capture={} if capture is None else capture
        from native_metadata import ancestors
        for path in sorted(set(p for p,_ in prepared.targets)|set(ancestors(p for p,_ in prepared.targets))):
            capture[path]=self.recovery._observe(path)
        result=dict(prepared.targets)
        original_routes=dict(self.preview._observed.routes)
        reviewed_parents={row.path for row in prepared.review.parents}
        for path,state in capture.items():
            original=original_routes.get(path)
            if original is not None and state is not None and state['kind']=='directory':
                actual=(state['dev'],state['ino'],stat.S_IFDIR,state['mode'],state['uid'],state['gid'])
                if actual[:3]!=original[:3]:
                    raise Refused('captured-ancestor-route-changed')
        parent_paths={row.path for row in prepared.review.parents}
        for row in prepared.review.parents:
            if prepared.review.draft.resolutions and all(dict(prepared.review.draft.resolutions).get(path)=="ha" for path in row.dependents if path in prepared.review.draft.selected):continue
            with preview._route(self.preview.binding,row.path,'directory',absent=True) as (fd,routes):
                frozen=capture[row.path]
                actual=None if frozen is None else metadata.Record(row.path,frozen['kind'],frozen['uid'],frozen['gid'],frozen['mode'],None)
                for path,state in routes:
                    expected=original_routes.get(path)
                    if expected is not None and (state[:3]!=expected[:3] if path in parent_paths else state!=expected):raise Refused('reviewed-parent-route-changed')
            if any(dict(prepared.review.draft.resolutions).get(path)=="git" for path in row.dependents) and actual!=row.before:
                raise Refused("explicit-whole-file-choice-parent-changed")
            # Required parent observation is not permission to add a new
            # effect outside the exact prepared target closure.
            if row.path not in result:continue
            merged=self._attributes(row.before,actual,row.after)
            result[row.path]=None if merged is None else Directory(merged.uid,merged.gid,merged.mode)
        for row in prepared.review.selected:
            if dict(prepared.review.draft.resolutions).get(row.path)=="ha":continue
            state,raw=self._capture_live(row.path,capability=capability)
            current=self.recovery._observe(row.path)
            if raw is not None and any(parse(raw.state)[k]!=current[k] for k in ('uid','gid','mode','digest','size')):raise Refused('captured-source-policy-mismatch')
            if capture[row.path]!=current:raise Refused('selected-capture-unstable')
            old_state,old_raw=dict(prepared.preimages)[row.path]
            target=result[row.path]
            if row.path in dict(prepared.review.draft.resolutions):
                original=original_routes.get(row.path)
                captured=capture[row.path]
                if original is not None and captured is not None and (captured["dev"],captured["ino"])!=original[:2]:raise Refused("explicit-whole-file-choice-route-changed")
                if state!=old_state:raise Refused("explicit-whole-file-choice-live-changed")
                continue
            record=self._attributes(old_state.record if old_state else None,state.record if state else None,
                                    row.after.record if row.after else None)
            result[row.path]=self._merge_sources(row.path,raw,old_raw,target,record,capability=capability)
        if capability is not None:self.recovery.qualify_capture(prepared,capture,result,capability=capability)
        return result

    def before_effect(self, store, manifest, journal, path,*,prepared,capability):
        if store is not self.recovery or prepared is None:
            raise Refused('issued-active-apply-guard-required')
        prepared.check()
        if time.monotonic()>=self._deadline:
            raise Refused('apply-deadline-exceeded')
        if self._remote(prepared.review)!=prepared.advertisement:
            raise Refused('approved-git-changed-before-write')
        if time.monotonic()>=self._deadline:raise Refused('apply-deadline-exceeded')
        key=__import__('base64').b64encode(path).decode('ascii')
        actual=store._observe(path)
        if actual!=journal['current'][key]:
            row=next((r for r in prepared.review.selected if r.path==path),None)
            if row is None or key in journal['changed']:
                raise Refused('affected-or-parent-live-state-changed')
            if path in dict(prepared.review.draft.resolutions):raise Refused('explicit-whole-file-choice-live-changed')
            captured=store._observe(path)
            live,current=self._capture_live(path,capability=capability)
            old_state,base=dict(prepared.preimages)[path]
            target=dict(prepared.targets)[path]
            record=self._attributes(old_state.record if old_state else None,live.record if live else None,
                                    row.after.record if row.after else None)
            merged=self._merge_sources(path,current,base,target,record,capability=capability)
            store.amend(self,path,current,merged,self._admission,capability=capability,captured=captured)

    def before_service_command(self,store,entry,kind,*,capability):
        require_apply(capability,self)
        if store is not self.services or kind!='stop':raise Refused('issued-apply-service-guard-required')
        self.direct.qualify(self._direct_receipt)
        if time.monotonic()>=self._deadline:raise Refused('apply-deadline-exceeded')

    def before_dispatch(self,store,path,*,prepared,capability):
        require_apply(capability,self,prepared.operation)
        if store is not self.recovery:raise Refused('issued-apply-dispatch-required')
        prepared.check()
        if self._remote(prepared.review)!=prepared.advertisement:
            raise Refused('approved-git-changed-before-syscall')
        source=self.preview._apply_fence(prepared.review).source
        entries=self.preview.git.tree_entries(source.remote_oid)
        manifest=next((e for e in entries if e.path==metadata.MANIFEST_PATH),None)
        if manifest is None:raise Refused('approved-metadata-manifest-missing')
        class VerifySink:
            def write(self,data):return len(data)
        self.preview.git.copy_blob(manifest.oid,VerifySink())
        row=next((r for r in prepared.review.selected if r.path==path),None)
        if row is not None and row.after is not None:self.preview.git.copy_blob(row.after.oid,VerifySink())
        if time.monotonic()>=self._deadline:raise Refused('apply-deadline-exceeded')

    def _residual(self, prepared):
        rows=[]
        observed=self.preview._observe()
        remote=self.preview._states(observed.remote,observed.remote_manifest,observed.source)
        live=dict(observed.live)
        for path in sorted(set(remote)|set(live)):
            actual,target=live.get(path),remote.get(path)
            if (not preview._same(actual,target) or actual is not None and target is not None and actual.record!=target.record):
                rows.append(dict(path=encode_native_path(path),before=None if actual is None else
                                 dict(oid=actual.oid,metadata=_encode(actual.record)),
                                 after=None if target is None else dict(oid=target.oid,metadata=_encode(target.record))))
        return rows
