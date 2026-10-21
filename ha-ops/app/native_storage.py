"""Dormant Calculate/Compact coordinator for explicitly qualified adapters only.

No production route imports this module. Diagnostics survive lifecycle changes;
review authority lives only in one instance and is consumed before guard entry.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid

from native_disk import Blocked, Control, Evidence, Plan, _directory, _observation, _identity, _digest, _optional_stat
from native_operations import canonical

STATUS = b"data/native/storage-status.json"
MAX_STATUS = 65536
MAX_CLEANUP = 64


def _validate_diagnostic(value):
    """Exact bounded display schema; never accept authority or arbitrary fields."""
    def require(condition):
        if not condition:raise Blocked("invalid bounded storage diagnostic")
    def number(value):return type(value) in (int,float) and 0<=value<2**64 and __import__('math').isfinite(value)
    def text(value,limit=512):return type(value) is str and len(value)<=limit
    def forecast(value):
        require(type(value) is dict and set(value)=={"status","reason","volumes"})
        require(value['status'] in ('none','known','unknown') and text(value['reason']) and type(value['volumes']) is list and len(value['volumes'])<=32)
        for row in value['volumes']:
            require(type(row) is dict and set(row)=={'status','volume','additional_peak','reserve','required_inodes','available','available_inodes','shortfall','warning'})
            require(row['status'] in ('fits','insufficient-space','cannot-establish-admission') and text(row['volume'],32) and type(row['warning']) is bool)
            require(all(number(row[key]) for key in ('additional_peak','reserve','required_inodes','available','shortfall')))
            require(row['available_inodes'] is None or number(row['available_inodes']))
    require(type(value) is dict and set(value)=={'schema','measurement','attempt','maintenance','attempt_marker'} and type(value['schema']) is int and value['schema']==1)
    require(type(value["attempt_marker"]) is str and re.fullmatch(r"[0-9a-f]{64}",value["attempt_marker"]))
    m=value['measurement']
    if m is not None:
        require(type(m) is dict and set(m)=={'measured_at','completed_at','complete','categories','volumes','issues','pending','authorization'})
        require(number(m['measured_at']) and number(m['completed_at']) and type(m['complete']) is bool)
        require(type(m['categories']) is list and len(m['categories'])==6)
        require({row.get('category') for row in m['categories'] if type(row) is dict}=={'git','staging','diff','recovery','service','authorization'})
        for row in m['categories']:
            require(type(row) is dict and set(row)=={'category','logical_bytes','allocated_bytes'} and number(row['logical_bytes']) and number(row['allocated_bytes']))
        require(type(m['volumes']) is list and len(m['volumes'])<=32)
        for row in m['volumes']:
            require(type(row) is dict and set(row)=={'volume','available_bytes','available_inodes','warning','filesystem'})
            require(text(row['volume'],32) and text(row['filesystem'],32) and number(row['available_bytes']) and type(row['warning']) is bool and (row['available_inodes'] is None or number(row['available_inodes'])))
        require(type(m['issues']) is list and len(m['issues'])<=32 and all(text(item,128) for item in m['issues']))
        require(m['authorization'] in ('absent','opaque aggregate only'))
        forecast(m['pending'])
    a=value['attempt']
    if a is not None:
        require(type(a) is dict and not set(a)-{'status','at','error','issues'} and a.get('status') in ('completed','incomplete','failed','unavailable'))
        require('at' not in a or number(a['at']))
        require('error' not in a or text(a['error'],256))
        require('issues' not in a or type(a['issues']) is list and len(a['issues'])<=32 and all(text(item,128) for item in a['issues']))
    m=value['maintenance']
    if m is not None:
        require(type(m) is dict and not set(m)-{'status','at','completed','remaining','message','error','error_type','cleanup_errors'} and m.get('status') in ('effect-started','completed','uncertain'))
        require(number(m.get('at')))
        for key in ('completed','remaining'):
            require(type(m.get(key)) is list and len(m[key])<=MAX_CLEANUP and all(type(item) is str and re.fullmatch(r'[0-9a-f]{64}\.(diff|blob)',item) for item in m[key]))
        require(all(text(m[key],512) for key in ('message','error','error_type') if key in m))
        require('cleanup_errors' not in m or type(m['cleanup_errors']) is list and len(m['cleanup_errors'])<=8 and all(text(item,128) for item in m['cleanup_errors']))
    return value


@dataclass(frozen=True)
class _CompactExecution:
    owner: object
    admission: object
    fds: tuple


class NativeStorage:
    def __init__(self, disk, git, operations, *, fault=None):
        # No file access, scan, lock or implicit lifecycle operation.
        self.disk, self.git, self.operations = disk, git, operations
        self.fault = fault or (lambda stage: None)
        self._reviews = {}
        self._execution = None
        self._mutex = threading.Lock()
        self.instance = uuid.uuid4().hex
        self._client_lifetime = None
        self._client_generation = 0
        self._retired_lifetimes = __import__("collections").deque(maxlen=128)
        if (git.root != disk.root or git.metadata != disk.root + b"/data/native/repository.git"
                or os.fsencode(operations.data_root) != disk.root + b"/data"):
            raise Blocked("storage participant anchors differ")

    def lifecycle(self, reason):
        if reason not in ("refresh", "restart", "update", "disconnect", "cancel"):
            raise Blocked("explicit storage lifecycle required")
        self._reviews.clear()

    def request(self, value):
        """Qualified adapter lifetime, independent of the ordinary command queue."""
        if type(value) is not dict or set(value)-{"action","review_id","lifetime","generation"}:
            raise Blocked("canonical storage request required")
        action=value.get("action");lifetime=value.get("lifetime");generation=value.get("generation")
        if action not in ("calculate","preview","confirm","cancel") or type(lifetime) is not str or not re.fullmatch(r"[0-9a-f-]{36}",lifetime) or type(generation) is not int or not 0<generation<2**53:
            raise Blocked("qualified storage lifetime required")
        if lifetime in self._retired_lifetimes:raise Blocked("storage lifetime retired")
        if self._client_lifetime!=lifetime:
            if action=="confirm":raise Blocked("confirmation belongs to another lifetime")
            if self._client_lifetime is not None:self._retired_lifetimes.append(self._client_lifetime)
            self._client_lifetime=lifetime;self._client_generation=0;self._reviews.clear()
        if generation<=self._client_generation:raise Blocked("stale storage request")
        self._client_generation=generation
        if action=="cancel":self.lifecycle("cancel");return self.status()
        if action=="calculate":result=self.calculate()
        elif action=="preview":result=self.preview()
        else:result=self.confirm(value.get("review_id"))
        if self._client_lifetime!=lifetime or self._client_generation!=generation:
            self._reviews.clear();raise Blocked("storage reply belongs to an ended lifetime")
        return result

    def _load(self, *, receipt=False):
        try:
            with _directory(self.disk.root + b"/data/native") as parent:
                fd = os.open(b"storage-status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                        raise Blocked("unsafe storage diagnostic")
                    raw = os.read(fd, MAX_STATUS + 1)
                    if len(raw) > MAX_STATUS or _observation(info) != _observation(os.fstat(fd)):
                        raise Blocked("invalid storage diagnostic")
                    from native_operations import _parse
                    try:value = _validate_diagnostic(_parse(raw))
                    except (TypeError,KeyError,RecursionError) as error:
                        raise Blocked("invalid bounded storage diagnostic") from error
                    if canonical(value)!=raw:raise Blocked("noncanonical storage diagnostic")
                    if not isinstance(value,dict) or set(value)!={"schema","measurement","attempt","maintenance","attempt_marker"} or value["schema"]!=1:
                        raise Blocked("unsupported storage diagnostic")
                    # Never hydrate execution fields, even from a modified file.
                    result={key:copy.deepcopy(value[key]) for key in ("schema","measurement","attempt","maintenance","attempt_marker")}
                    return (result,_observation(info)) if receipt else result
                finally: os.close(fd)
        except FileNotFoundError:
            result=dict(schema=1,measurement=None,attempt=None,maintenance=None,attempt_marker="0"*64)
            return (result,None) if receipt else result

    def status(self):
        try: value = self._load()
        except (OSError,ValueError):
            value = dict(schema=1,measurement=None,attempt={"status":"unavailable","error":"Storage diagnostic unavailable"},maintenance=None)
        marker=value.pop("attempt_marker","0"*64)
        if marker!="0"*64:
            value["attempt"]=dict(status="uncertain",error="Latest Calculate attempt did not complete; previous measurement retained")
        if value.get("maintenance",{} ) and value["maintenance"].get("status") == "effect-started":
            value["maintenance"] = {**value["maintenance"],"status":"uncertain","message":"Interrupted Compact; no automatic retry. Review again explicitly."}
        return dict(qualified=True, instance=self.instance, **value)

    def _save(self, value, *, expected, settle_attempt=False):
        value={**value,"attempt_marker":"0"*64 if settle_attempt else value.get("attempt_marker","0"*64)}
        raw = canonical(_validate_diagnostic(value))
        if len(raw) > MAX_STATUS: raise Blocked("bounded storage diagnostic exceeded")
        with _directory(self.disk.root + b"/data/native") as parent:
            # Target regularity is checked before replace, including hardlinks.
            try:
                before = os.stat(b"storage-status.json",dir_fd=parent,follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600:
                    raise Blocked("unsafe storage diagnostic target")
            except FileNotFoundError: before=None
            if (None if before is None else _observation(before)) != expected:
                raise Blocked("storage diagnostic ownership changed")
            before_raw=None
            if before is not None:
                old=os.open(b"storage-status.json",os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                try:
                    if _observation(os.fstat(old))!=_observation(before):raise Blocked("diagnostic preimage changed")
                    before_raw=os.read(old,MAX_STATUS+1)
                    if len(before_raw)>MAX_STATUS:raise Blocked("diagnostic preimage exceeds envelope")
                finally:os.close(old)
            name=(".storage-"+uuid.uuid4().hex).encode()
            fd=None;replaced=False;primary=None;issued_receipt=expected
            try:
                fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                self.fault("diagnostic_opened")
                remaining=memoryview(raw)
                while remaining:
                    count=os.write(fd,remaining)
                    if count<=0:raise OSError("short diagnostic write")
                    remaining=remaining[count:]
                self.fault("diagnostic_written");os.fsync(fd);self.fault("diagnostic_synced")
                # Exact target route, never overwrite a new foreign replacement.
                try:current=os.stat(b"storage-status.json",dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError:current=None
                if (None if current is None else _observation(current)) != (None if before is None else _observation(before)):
                    raise Blocked("storage diagnostic changed")
                os.replace(name,b"storage-status.json",src_dir_fd=parent,dst_dir_fd=parent);replaced=True
                issued_receipt=_observation(os.fstat(fd))
                self.fault("diagnostic_replaced");os.fsync(parent);self.fault("diagnostic_durable")
                if _observation(os.stat(b"storage-status.json",dir_fd=parent,follow_symlinks=False))!=issued_receipt:
                    raise Blocked("published diagnostic receipt changed")
            except BaseException as error:
                primary=error
                if replaced:
                    # Restore the exact last completed preimage if publication
                    # failed after replace. This bounded cleanup never adopts a
                    # foreign replacement and never suppresses the first fault.
                    rollback=None;rollback_fd=None
                    try:
                        current=os.stat(b"storage-status.json",dir_fd=parent,follow_symlinks=False)
                        if _observation(current)!=issued_receipt:raise Blocked("diagnostic rollback ownership changed")
                        if before_raw is None:
                            os.unlink(b"storage-status.json",dir_fd=parent);issued_receipt=None
                        else:
                            rollback=(".storage-"+uuid.uuid4().hex).encode()
                            rollback_fd=os.open(rollback,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                            remaining=memoryview(before_raw)
                            while remaining:
                                count=os.write(rollback_fd,remaining)
                                if count<=0:raise OSError("short diagnostic rollback")
                                remaining=remaining[count:]
                            os.fsync(rollback_fd)
                            os.replace(rollback,b"storage-status.json",src_dir_fd=parent,dst_dir_fd=parent);rollback=None
                            issued_receipt=_observation(os.fstat(rollback_fd))
                        os.fsync(parent)
                    except BaseException as secondary:
                        error.cleanup_errors=tuple(getattr(error,"cleanup_errors",()))+("diagnostic preimage restore: "+type(secondary).__name__,)
                    finally:
                        for close in ((lambda:os.close(rollback_fd)) if rollback_fd is not None else None,
                                      (lambda:os.unlink(rollback,dir_fd=parent)) if rollback is not None else None):
                            if close is not None:
                                try:close()
                                except BaseException as secondary:error.cleanup_errors=tuple(getattr(error,"cleanup_errors",()))+("diagnostic rollback cleanup: "+type(secondary).__name__,)
                error.diagnostic_receipt=issued_receipt
                raise
            finally:
                for close in ((lambda:os.close(fd)) if fd is not None else None,
                              (lambda:os.unlink(name,dir_fd=parent)) if not replaced else None):
                    if close is not None:
                        try:close()
                        except FileNotFoundError:pass
                        except BaseException as secondary:
                            if primary is None:raise
                            primary.cleanup_errors=tuple(getattr(primary,"cleanup_errors",()))+("diagnostic cleanup: "+type(secondary).__name__,)

            return issued_receipt

    @staticmethod
    def _forecast(plan):
        if plan is None:return dict(status="none",reason="No pending operation",volumes=[])
        known=plan.supported and bool(plan.decisions) and all(row.available_inodes is not None for row in plan.decisions)
        reason="; ".join(plan.unknown)[:512]
        if not known and not reason:reason="Pending byte/inode capacity could not be established"
        return dict(status="known" if known else "unknown",reason=reason,
                    volumes=[{**asdict(row),"volume":"volume-"+_digest(row.volume)[:12]} for row in plan.decisions])

    def _controls(self, operation):
        # A finite maximum of the exact schema: 6 categories, <=32 volumes,
        # <=32 fixed-size issues, <=64 SHA-256 artifact names, bounded messages.
        # Charge full old/new/failure atomic overlap, not today's short payload.
        envelope=canonical(dict(operation_id=operation,owner_id=self.operations.owner,
            schema=1,maximum_storage_status="x"*(MAX_STATUS-256)))
        return (Control(STATUS,envelope,operation,self.operations.owner),
                Control(STATUS,envelope,operation,self.operations.owner))

    def _mark_attempt(self,receipt):
        """Use only an already allocated fixed-width slot before acquisition.

        No mkdir, inode/block allocation, capacity scan, saved admission or
        waiting for an active disk guard. Missing/unqualified preallocation is
        explicit unavailability, never permission to allocate behind its back.
        """
        if receipt is None:return None
        path=self.disk.root+b"/"+STATUS
        from native_inventory import linux_mounts
        with _directory(os.path.dirname(path)) as parent:
            fd=os.open(os.path.basename(path),os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                self.operations._regular(fd)
                if _observation(os.fstat(fd))!=receipt:raise Blocked("attempt diagnostic ownership changed")
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                info=os.fstat(fd)
                key,mount,filesystem,supported=self.disk._mount(path,info,linux_mounts())
                if not supported or filesystem not in (b"ext4",b"tmpfs") or info.st_blocks*512<info.st_size:
                    raise Blocked("preallocated attempt diagnostic unavailable on this filesystem")
                raw=os.read(fd,MAX_STATUS+1)
                prefix=b'"attempt_marker":"';offset=raw.index(prefix)+len(prefix)
                if len(raw)>MAX_STATUS or not re.fullmatch(rb"[0-9a-f]{64}",raw[offset:offset+64]):raise Blocked("invalid preallocated attempt marker")
                marker=("1"+format(time.time_ns(),"016x")+"0"*47).encode()
                done=0
                while done<len(marker):
                    if _observation(os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False))!=_observation(os.fstat(fd)):
                        raise Blocked("attempt diagnostic route changed")
                    count=os.pwrite(fd,marker[done:],offset+done)
                    if count<=0:raise OSError("short attempt marker write")
                    done+=count;os.fsync(fd);self.fault("attempt_marker_written")
                if _identity(os.stat(os.path.basename(path),dir_fd=parent,follow_symlinks=False))!=_identity(info):raise Blocked("attempt diagnostic replaced")
                return _observation(os.fstat(fd))
            finally:os.close(fd)

    def calculate(self, *, pending=None):
        if not self._mutex.acquire(blocking=False):raise Blocked("storage action busy")
        snapshot=None;previous=None;receipt=None
        try:
            self._reviews.clear()
            previous,receipt=self._load(receipt=True)
            receipt=self._mark_attempt(receipt)
            snapshot=self.disk.snapshot()
            if len(snapshot.volumes)>32:raise Blocked("storage volume diagnostic bound exceeded")
            # Only an actual same-instance issued pending plan may supply a peak.
            if pending is None:forecast=self._forecast(None)
            elif type(pending) is Plan and self.disk._plans.get(id(pending)) is pending and id(pending) in self.disk._contexts:
                forecast=self._forecast(pending)
            else:forecast=dict(status="unknown",reason="Pending operation has no qualified recipe",volumes=[])
            measured=dict(measured_at=snapshot.timestamp,completed_at=time.time(),complete=snapshot.complete,
                categories=[dict(category=c,logical_bytes=l,allocated_bytes=a) for c,l,a in snapshot.categories],
                volumes=[dict(volume="volume-"+_digest(v.key)[:12],available_bytes=v.available,available_inodes=v.inodes,warning=v.warning,
                              filesystem=os.fsdecode(v.filesystem)) for v in snapshot.volumes],
                issues=[str(issue)[:128] for issue in snapshot.issues[:32]],pending=forecast,
                authorization="absent" if dict(snapshot.observations).get(self.disk.root+b"/data/direct-auth") is None else "opaque aggregate only")
            if not snapshot.complete:
                attempt=dict(status="incomplete",at=time.time(),error="Accounting incomplete",issues=measured["issues"])
                self._persist_attempt(snapshot,previous,attempt,receipt)
                return {**self.status(),"incomplete_measurement":measured}
            operation=str(uuid.uuid4())
            plan=self.disk.project("storage_control",snapshot,Evidence(operation,self.operations.owner,controls=self._controls(operation)))
            with self.disk.guard(plan):
                self._save(dict(schema=1,measurement=measured,attempt=dict(status="completed",at=time.time()),maintenance=previous["maintenance"]),expected=receipt,settle_attempt=True)
            return self.status()
        except BaseException as primary:
            attempt=dict(status="failed",at=time.time(),error="Calculate failed; previous completed measurement retained" + ("; durable attempt diagnostic unavailable before first admitted Calculate" if receipt is None else ""))
            if receipt is None:
                primary.cleanup_errors=tuple(getattr(primary,"cleanup_errors",()))+("durable attempt diagnostic unavailable before first admitted Calculate",)
            if snapshot is not None and previous is not None:
                try:self._persist_attempt(snapshot,previous,attempt,getattr(primary,"diagnostic_receipt",receipt))
                except BaseException as secondary:
                    primary.cleanup_errors=tuple(getattr(primary,"cleanup_errors",()))+("failed attempt persistence: "+type(secondary).__name__,)
            raise
        finally:self._mutex.release()

    def _persist_attempt(self,snapshot,previous,attempt,receipt):
        operation=str(uuid.uuid4())
        plan=self.disk.project("storage_control",snapshot,Evidence(operation,self.operations.owner,controls=self._controls(operation)))
        with self.disk.guard(plan):self._save({**previous,"attempt":attempt},expected=receipt,settle_attempt=True)

    @contextmanager
    def _locks(self):
        with self.operations.compact_observe() as (root,artifacts,state,store_lock,routes):
            if not self.git._mutex.acquire(blocking=False):raise Blocked("Git resource owner busy")
            try:
                with _directory(self.git.resources_root) as resources:
                    lock=os.open(b"journal.lock",os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=resources)
                    try:
                        self.operations._regular(lock)
                        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        initial=_observation(os.fstat(lock))
                        journal=self.disk._control(self.git.resources_root+b"/journal.json")
                        self.disk._validate_resource_journal(journal)
                        if journal["records"]:raise Blocked("Existing resources remain protected; settle their ownership first")
                        yield artifacts,state,(store_lock,lock),routes
                        if initial!=_observation(os.stat(b"journal.lock",dir_fd=resources,follow_symlinks=False)):
                            raise Blocked("resource owner lock replaced")
                    finally:os.close(lock)
            finally:self.git._mutex.release()

    def _stable(self, snapshot):
        metadata=self.git.metadata
        # Object changes belong only to Git's reviewed maintenance phase.
        result = tuple((path,observed) for path,observed in snapshot.observations if observed is not None and
            path.startswith(metadata+b"/") and not path.startswith(metadata+b"/objects/") and
            (stat.S_ISREG(observed[2]) or path.startswith(metadata+b"/refs") or path.startswith(metadata+b"/logs")))
        known=dict(result)
        for relative in (b"index",b"index.lock",b"packed-refs",b"refs",b"logs"):
            path=metadata+b"/"+relative
            if path not in known:
                value=_optional_stat(path)
                result+=((path,None if value is None else _observation(value)),)
        return result

    def _check_stable(self, stable):
        for path,observed in stable:
            value=_optional_stat(path)
            if (None if value is None else _observation(value))!=observed:raise Blocked("Git refs, configuration or index changed")

    def preview(self):
        if not self._mutex.acquire(blocking=False):raise Blocked("storage action busy")
        try:
            self._reviews.clear()
            snapshot=self.disk.snapshot()
            operation=str(uuid.uuid4())
            with self._locks() as (artifacts,state,fds,routes):
                if state["active"]:raise Blocked("Active operation remains protected")
                recovery=state.get("recovery")
                if recovery and any(worker["record_id"]==recovery["record_id"] and
                    (worker["phase"]!="completed" or (worker.get("apply_result") or {}).get("cleanup_debt"))
                    for worker in state["workers"].values()):
                    raise Blocked("Unresolved recovery ownership remains protected")
                initial=canonical(state)
                obsolete=self.operations.compact_artifacts(state,artifacts,snapshot.observations)
            # Reject ancillary object-store outputs the qualified recipe does not own.
            for path,observed in snapshot.observations:
                if path.startswith(self.git.metadata+b"/objects/pack/") and not re.fullmatch(rb"pack-[0-9a-f]+\.(pack|idx)",os.path.basename(path)):
                    raise Blocked("Unsupported ancillary pack storage remains protected")
            if os.path.lexists(self.git.metadata+b"/index.lock"):raise Blocked("Foreign Git index lock")
            plan=self.disk.project("repack",snapshot,Evidence(operation,self.operations.owner,controls=self._controls(operation)))
            objects=next((item[1] for item in plan.provenance if item[0]=="maintenance-objects"),())
            review=uuid.uuid4().hex
            rows=obsolete[:MAX_CLEANUP]
            model=dict(id=review,actions=["Repack all local Git objects, retaining unreachable objects",f"Remove {len(rows)} exact obsolete owned artifacts"],
                savings=dict(artifact_allocated_bytes=sum(row[2] for row in rows),git="Unknown; recompression may save little or increase storage"),
                protected=["All Git history and objects","Reviewed, pending and recovery data","Browser authorizations","Live files, backups and foreign temporary files"],
                forecast=self._forecast(plan),available=self._forecast(plan)["status"]=="known" and all(row.status=="fits" for row in plan.decisions),
                uncertainty="Approximate savings; no trial repack. Remaining ambiguous data is protected.")
            self._reviews[review]=(plan,initial,rows,objects,self._stable(snapshot),copy.deepcopy(model))
            return {**self.status(),"review":model}
        finally:self._mutex.release()

    def confirm(self, review_id):
        if not self._mutex.acquire(blocking=False):raise Blocked("storage action busy")
        completed=[];started=False;previous=None;receipt=None
        try:
            prepared=self._reviews.pop(review_id,None)
            self._reviews.clear()
            if prepared is None:raise Blocked("Compact review expired; review again")
            plan,initial,rows,objects,stable,model=prepared
            if not model["available"]:raise Blocked("Compact forecast cannot admit operation")
            previous,receipt=self._load(receipt=True)
            with self.disk.guard(plan) as admission:
                with self._locks() as (artifacts,state,fds,routes):
                    if canonical(state)!=initial:raise Blocked("Canonical operation graph changed")
                    # Existing guard's full fixed context checks, under held
                    # canonical locks, without acquiring duplicate flocks.
                    admission._check_namespace();admission._validate_context()
                    self._check_stable(stable)
                    self.fault("compact_fenced")
                    # Fault/interleaving hooks precede the final proof.
                    admission._check_namespace();admission._validate_context()
                    self._check_stable(stable)
                    self.operations.compact_remove(artifacts,rows,[],dry_run=True,routes=routes)
                    started=True
                    receipt=self._save({**previous,"maintenance":dict(status="effect-started",at=time.time(),completed=[],remaining=[row[0] for row in rows])},expected=receipt)
                    permit=_CompactExecution(self,admission,(admission.fd,*fds));self._execution=permit
                    try:self.git.compact(permit)
                    finally:self._execution=None
                    self._check_stable(stable)
                    # Targeted original OIDs only: never enumerate or recalculate.
                    output=self.git._run(("cat-file","--batch-check=%(objectname) %(objecttype) %(objectsize)"),
                        input=b"".join(oid+b"\n" for oid,kind,size in objects))
                    expected=b"".join(oid+b" "+kind+b" "+str(size).encode()+b"\n" for oid,kind,size in objects)
                    if output!=expected:raise Blocked("Original Git object postcondition failed")
                    with _directory(self.disk.root+b"/data/native-operations") as graph:
                        if canonical(self.operations._load(graph,artifacts,create=False))!=initial:
                            raise Blocked("Canonical graph changed during maintenance")
                    self.operations.compact_remove(artifacts,rows,completed,routes=routes)
                    receipt=self._save({**previous,"maintenance":dict(status="completed",at=time.time(),completed=completed,remaining=[],message="Compact completed. Calculate again explicitly for current usage.")},expected=receipt)
            return self.status()
        except BaseException as primary:
            if started and previous is not None:
                diagnostic=dict(status="uncertain",at=time.time(),error="Compact failed; effects may have started",error_type=type(primary).__name__[:64],
                    completed=completed,remaining=[row[0] for row in rows if row[0] not in completed],
                    cleanup_errors=[str(item)[:128] for item in getattr(primary,"cleanup_errors",())[:8]])
                try:self._save({**previous,"maintenance":diagnostic},expected=getattr(primary,"diagnostic_receipt",receipt))
                except BaseException as secondary:
                    primary.cleanup_errors=tuple(getattr(primary,"cleanup_errors",()))+("terminal diagnostic persistence failed: "+type(secondary).__name__,)
            raise
        finally:self._execution=None;self._mutex.release()
