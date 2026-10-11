"""Dormant exact native publication. Constructors and diagnostics have no effects.

Only an issued NativeSave Candidate/original Review or a fresh one-attempt
recovery grant can execute. The only qualified transport currently is a local
bare endpoint; auth/network transports require their own endpoint qualification.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import io
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid

import native_metadata as metadata
import native_preview as preview
import native_save as save
import native_inventory as inventory
from native_git import NativeGit, TreeEntry, parse_tree, parse_commit, _components, _identity, _close_owned
from native_disk import NativeDisk, Evidence, Control, Target, Blocked as DiskBlocked, InsufficientSpace, _observation
from native_operations import (NativeOperationStore, canonical, digest, _parse, _fields, _require,
    _id, _refs, encode_native_path, decode_native_path, validate_identity, Conflict, InvalidState,
    _publication_abandonment_cleanup)


class PublicationError(RuntimeError):
    def __init__(self, reason, *, operation_id=None, cleanup_errors=()):
        super().__init__(reason)
        self.operation_id, self.cleanup_errors = operation_id, tuple(cleanup_errors)


@dataclass(frozen=True)
class Signature:
    name: str
    email: str
    timestamp: int
    timezone: str

    def __post_init__(self):
        for text in (self.name,self.email):
            _require(type(text) is str and 0 < len(text.encode('utf-8')) <= 1024 and
                     not any(c in text for c in '\0\r\n<>'), 'invalid explicit commit identity')
            encoded=text.encode('utf-8')
            crud=b'.,:;<>"\\\''
            _require(all(byte>32 and byte not in crud for byte in (encoded[0],encoded[-1])),
                     'explicit identity would be canonicalized by Git')
        _require(type(self.timestamp) is int and 0 <= self.timestamp < 10**12, 'invalid commit timestamp')
        _require(type(self.timezone) is str and re.fullmatch(r'[+-](?:0[0-9]|1[0-4])[0-5][0-9]',self.timezone), 'invalid commit timezone')
        _require(self.timezone!='-0000','Git canonicalizes negative zero timezone')

    def raw(self):
        return ('%s <%s> %d %s' % (self.name,self.email,self.timestamp,self.timezone)).encode('utf-8')


def commit_bytes(candidate,author,committer,message):
    _require(type(author) is Signature and type(committer) is Signature, 'explicit commit signatures required')
    _require(type(message) is bytes and message and len(message) <= 65536 and b'\0' not in message,
             'bounded exact commit message required')
    return (b'tree '+candidate.tree_oid.encode()+b'\nparent '+candidate.remote.remote_oid.encode()+
            b'\nauthor '+author.raw()+b'\ncommitter '+committer.raw()+b'\n\n'+message)


def object_id(kind,raw,object_format):
    return hashlib.new(object_format,kind+b' '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


_TYPES = {cls.__name__:cls for cls in (save.Candidate,preview.SourceObservation,preview.FileState,
    preview.PathChange,preview.ParentDetail,preview.Review,preview.Draft,metadata.Record,
    metadata.ParentReview,metadata.Binding,inventory.Policy,inventory.Mount,Signature)}


def _encode(value):
    if type(value) is bytes: return {'bytes':value.hex()}
    if type(value) is tuple: return {'tuple':[_encode(v) for v in value]}
    if type(value) in (str,int,bool) or value is None: return value
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        return {'type':type(value).__name__,'fields':{f.name:_encode(getattr(value,f.name)) for f in fields(value)}}
    raise InvalidState('unsupported exact publication descriptor')


def _decode(value):
    if type(value) in (str,int,bool) or value is None: return value
    _require(type(value) is dict, 'typed publication descriptor required')
    if set(value) == {'bytes'}:
        text=value['bytes']
        _require(type(text) is str and re.fullmatch(r'(?:[0-9a-f]{2})*',text), 'canonical descriptor bytes required')
        return bytes.fromhex(text)
    if set(value) == {'tuple'}:
        _require(type(value['tuple']) is list,'descriptor tuple required')
        return tuple(_decode(v) for v in value['tuple'])
    _fields(value,'type fields')
    _require(type(value['type']) is str,'registered descriptor type required')
    cls=_TYPES.get(value['type'])
    _require(cls is not None and type(value['fields']) is dict and set(value['fields'])=={f.name for f in fields(cls)}, 'exact descriptor fields required')
    try: return cls(**{key:_decode(v) for key,v in value['fields'].items()})
    except (TypeError,ValueError) as error: raise InvalidState('invalid publication descriptor') from error


def _qualified_ref(name):
    _refs({k:{'name':name,'oid':'1'*40} for k in ('base','local','remote')})
    return name


@dataclass(frozen=True)
class Intent:
    candidate: save.Candidate
    review: preview.Review
    raw_commit: bytes
    commit_oid: str
    author: Signature
    committer: Signature
    message: bytes
    endpoint: bytes
    endpoint_identity: tuple
    destination: str
    local_ref: str
    expected_local: str
    tree_pin: str
    commit_pin: str
    namespace: dict
    identity: dict
    reflogs: tuple
    parent_graph_digest: str


def _candidate_identity(candidate,commit_oid,endpoint,destination,local_ref,expected_local):
    root_observation=dict(candidate.routes).get(b'')
    _require(type(root_observation) is tuple and len(root_observation)>=2,'captured exact root route required')
    paths=[]
    by_path={row.path:row.after.record if row.after is not None else None for row in candidate.selected}
    by_path.update((p,r) for p,r in candidate.parent_dependencies if p)
    for path,record in by_path.items():
        paths.append({'path':encode_native_path(path),'type':'missing' if record is None else record.kind,
                      'mode':None if record is None else (stat.S_IFREG if record.kind=='file' else stat.S_IFDIR)|record.mode,
                      'content_digest':None if record is None or record.kind=='directory' else digest(record.oid),
                      'metadata_digest':None if record is None else digest(_encode(record))})
    result={'direction':'save','execution_policy':'native-publication-v1',
            'scope':{'revision':0,'allowlist':sorted(encode_native_path(p) for p in candidate.remote.scope),
                     'exclude_digest':candidate.binding.policy.digest},
            'refs':{'base':{'name':candidate.remote.base_ref,'oid':candidate.remote.base_oid},
                    'local':{'name':local_ref,'oid':expected_local},
                    'remote':{'name':destination,'oid':candidate.remote.remote_oid}},
            'root':{'path':encode_native_path(candidate.binding.root,absolute=True),'device':root_observation[0],
                    'inode':root_observation[1],'mount_digest':digest(_encode(candidate.binding.mounts)),
                    'topology_digest':digest(_encode(candidate.binding))},
            'paths':sorted(paths,key=lambda p:p['path']),
            'effect':{'before':digest([candidate.remote.remote_oid,expected_local]),
                      'after':digest([commit_oid,encode_native_path(endpoint,absolute=True),destination,local_ref])}}
    validate_identity(result,'save')
    return result


def _intent_bytes(candidate,review,raw,author,committer,message,transport,local_ref,expected_local,identity,reflogs,parent_graph_digest,namespace):
    prefix='refs/ha-ops/native-publication/'+namespace['record_id']+'/'+namespace['operation_id']
    value={'schema':1,'profile':'native-publication-v1','codec':'native-bytes-v1',
           'candidate':_encode(candidate),'review':_encode(review),'commit':raw.hex(),
           'commit_oid':object_id(b'commit',raw,candidate.object_format),'author':_encode(author),
           'committer':_encode(committer),'message':message.hex(),
           'endpoint':encode_native_path(transport.endpoint,absolute=True),'endpoint_identity':list(transport.endpoint_identity),
           'destination':transport.destination,'local_ref':local_ref,'expected_local':expected_local,
           'tree_pin':prefix+'/tree','commit_pin':prefix+'/commit','namespace':namespace,'identity':identity,
           'reflogs':[[encode_native_path(route),data.hex()] for route,data in reflogs],
           'parent_graph_digest':parent_graph_digest}
    return canonical(value)


def validate_intent(payload, *, identity=None, namespace=None):
    _require(type(payload) is bytes, 'immutable publication bytes required')
    value=_parse(payload)
    _fields(value,'schema profile codec candidate review commit commit_oid author committer message endpoint endpoint_identity destination local_ref expected_local tree_pin commit_pin namespace identity reflogs parent_graph_digest')
    _require(canonical(value)==payload and value['schema']==1 and value['profile']=='native-publication-v1' and value['codec']=='native-bytes-v1', 'unsupported publication intent')
    candidate,review=_decode(value['candidate']),_decode(value['review'])
    _require(type(candidate) is save.Candidate and type(review) is preview.Review and review.ready and
             candidate.remote_object_kind==b'commit' and candidate.selected==review.selected and
             candidate.issued_parent_reviews==review.parents and candidate.generation==review.draft.generation and
             candidate.snapshot_id==review.draft.snapshot_id and tuple(r.path for r in candidate.selected)==review.draft.selected and
             candidate.approved_parents==review.draft.approved_parents, 'exact original candidate review required')
    author,committer=_decode(value['author']),_decode(value['committer'])
    for key in ('commit','message'):
        _require(type(value[key]) is str and re.fullmatch(r'(?:[0-9a-f]{2})*',value[key]), 'canonical raw commit required')
    raw,message=bytes.fromhex(value['commit']),bytes.fromhex(value['message'])
    _require(raw==commit_bytes(candidate,author,committer,message) and
             value['commit_oid']==object_id(b'commit',raw,candidate.object_format), 'immutable commit mismatch')
    tree,parents=parse_commit(raw,40 if candidate.object_format=='sha1' else 64)
    _require(tree==candidate.tree_oid and parents==(candidate.remote.remote_oid,), 'exact single reviewed parent required')
    endpoint=decode_native_path(value['endpoint'],absolute=True)
    for key in ('destination','local_ref','tree_pin','commit_pin'): _qualified_ref(value[key])
    _require(value['destination'].startswith('refs/heads/'), 'explicit destination branch required')
    width=40 if candidate.object_format=='sha1' else 64
    _require(candidate.selected==review.selected,'exact issued review selection required')
    _require(type(value['expected_local']) is str and re.fullmatch('[0-9a-f]{%d}'%width,value['expected_local']) and value['expected_local'].strip('0'),'exact expected local object required')
    ns=value['namespace']; _fields(ns,'instance_id generation record_id operation_id owner epoch marker')
    for key in ('instance_id','record_id','operation_id','owner','marker'): _id(ns[key])
    _require(type(ns['generation']) is int and ns['generation']>0 and type(ns['epoch']) is int and ns['epoch']>0, 'publication namespace required')
    prefix='refs/ha-ops/native-publication/'+ns['record_id']+'/'+ns['operation_id']
    _require(value['tree_pin']==prefix+'/tree' and value['commit_pin']==prefix+'/commit','owned publication pin names required')
    validate_identity(value['identity'],'save')
    owner_identity=value['identity']
    _require(owner_identity['execution_policy']=='native-publication-v1' and
             decode_native_path(owner_identity['root']['path'],absolute=True)==candidate.binding.root and
             set(decode_native_path(p['path']) for p in owner_identity['paths'])==
                set(r.path for r in candidate.selected)|{p for p,_ in candidate.parent_dependencies if p} and
             set(decode_native_path(p) for p in owner_identity['scope']['allowlist'])==set(candidate.remote.scope) and
             owner_identity['refs']['remote']=={'name':value['destination'],'oid':candidate.remote.remote_oid} and
             owner_identity['refs']['local']=={'name':value['local_ref'],'oid':value['expected_local']} and
             owner_identity['effect']=={'before':digest([candidate.remote.remote_oid,value['expected_local']]),
                'after':digest([value['commit_oid'],value['endpoint'],value['destination'],value['local_ref']])},'publication identity mapping mismatch')
    _require(owner_identity==_candidate_identity(candidate,value['commit_oid'],endpoint,value['destination'],value['local_ref'],value['expected_local']),
             'exact candidate byte identity mapping required')
    if identity is not None: _require(identity==owner_identity,'prepared publication identity mismatch')
    if namespace is not None: _require(namespace==ns,'prepared publication namespace mismatch')
    _require(type(value['endpoint_identity']) is list and len(value['endpoint_identity'])==3 and all(type(n) is int for n in value['endpoint_identity']), 'endpoint descriptor required')
    _require(type(value['reflogs']) is list,'exact reflogs required')
    reflogs=[]
    for route,data in value['reflogs']:
        path=decode_native_path(route)
        _require(path in (b'data/native/repository.git/logs/'+value['local_ref'].encode(),b'data/native/repository.git/logs/HEAD'), 'unrelated reflog route')
        _require(type(data) is str and re.fullmatch(r'(?:[0-9a-f]{2})+',data),'canonical reflog bytes required')
        entry=bytes.fromhex(data)
        _require(entry==value['expected_local'].encode()+b' '+value['commit_oid'].encode()+b' '+committer.raw()+b'\n','exact frozen reflog append required')
        reflogs.append((path,entry))
    _require(len(set(p for p,_ in reflogs))==len(reflogs),'duplicate reflog route')
    _require(type(value['parent_graph_digest']) is str and re.fullmatch('[0-9a-f]{64}',value['parent_graph_digest']),'complete parent graph commitment required')
    return Intent(candidate,review,raw,value['commit_oid'],author,committer,message,endpoint,tuple(value['endpoint_identity']),
                  value['destination'],value['local_ref'],value['expected_local'],value['tree_pin'],value['commit_pin'],ns,owner_identity,tuple(reflogs),value['parent_graph_digest'])


@dataclass(frozen=True)
class Advertisement:
    endpoint: bytes
    endpoint_identity: tuple
    destination: str
    oid: str | None
    object_format: str


@dataclass(frozen=True)
class PushOutcome:
    request: tuple
    kind: str
    transcript_digest: str


class GitCLITransport:
    """Qualified local-bare Git CLI, explicit one-ref expected-old publication."""
    def __init__(self,git,endpoint,destination,*,timeout=30,max_output=8*1024*1024,max_commits=10000):
        _require(isinstance(git,NativeGit),'native local source required')
        _require(type(endpoint) in (str,bytes) and os.path.isabs(endpoint),'qualified local bare endpoint required')
        self.git=git
        self.endpoint=_components(os.fsencode(endpoint))
        self.endpoint_identity=tuple(_identity(self.endpoint,directory=True))
        self.destination=_qualified_ref(destination)
        _require(destination.startswith('refs/heads/'),'explicit destination branch required')
        _require(type(timeout) in (int,float) and 0<timeout<=300,'bounded transport deadline required')
        _require(type(max_output) is int and max_output>=1024 and type(max_commits) is int and max_commits>0,'transport work budgets required')
        self.timeout,self.max_output,self.max_commits=timeout,max_output,max_commits
        self._binding=(git,self.endpoint,self.endpoint_identity,self.destination,timeout,max_output,max_commits)
        self._outcome=None; self._outcome_snapshot=None; self._route_binding=()
        self._source_binding=(git.root,git.metadata,git.policy)
        # Freeze only execution/routing controls; receiver refs/objects may move.
        routes=[]
        for root in (git.metadata,self.endpoint):
            _components(root+b'/objects')
            for suffix in (b'/config',b'/shallow',b'/info/grafts',b'/objects/info/alternates',b'/objects/info/http-alternates'):
                path=root+suffix
                try: info=os.lstat(path)
                except FileNotFoundError: routes.append((path,None)); continue
                if suffix!=b'/config': raise PublicationError('unsupported alternate or incomplete graph profile')
                _identity(path); routes.append((path,_observation(info)))
        self._route_binding=tuple(routes)
        for endpoint_profile in (False,True):
            code,data,error=self._command(('config','--null','--local','--no-includes','--list'),endpoint=endpoint_profile)
            if code or error or data and not data.endswith(b'\0'): raise PublicationError('exact transport profile unavailable')
            for token in data.split(b'\0')[:-1]:
                key=token.split(b'\n',1)[0].lower()
                if (key.startswith((b'include.',b'includeif.',b'url.')) or key in
                    (b'extensions.partialclone',b'extensions.worktreeconfig',b'core.alternaterefscommand') or
                    key.startswith(b'remote.') and (not endpoint_profile or key.endswith((b'.promisor',b'.partialclonefilter')))):
                    raise PublicationError('unsupported transport routing or lazy-fetch profile')
        code,data,error=self._command(('rev-parse','--is-bare-repository'),endpoint=True)
        _require(code==0 and data==b'true\n' and not error,'bare endpoint required')
        code,data,error=self._command(('rev-parse','--show-object-format'),endpoint=True)
        _require(code==0 and not error and data in (b'sha1\n',b'sha256\n'),'exact endpoint object format required')
        self.object_format=data[:-1].decode('ascii')
        _require(self.object_format==('sha1' if git._config()==40 else 'sha256'),'same endpoint object format required')

    def _check(self):
        if (self.git.root,self.git.metadata,self.git.policy)!=self._source_binding:
            raise PublicationError('source transport binding changed')
        for path,observed in self._route_binding:
            try: info=os.lstat(path)
            except FileNotFoundError:
                if observed is not None: raise PublicationError('transport profile disappeared')
                continue
            if observed is None or _observation(info)!=observed:
                raise PublicationError('transport profile changed')
        if (self.git,self.endpoint,self.endpoint_identity,self.destination,self.timeout,self.max_output,self.max_commits)!=self._binding or tuple(_identity(_components(self.endpoint),directory=True))!=self.endpoint_identity:
            raise PublicationError('endpoint or transport binding changed')

    def _command(self,args,*,endpoint=False):
        self._check()
        env=self.git._environment()
        root=self.endpoint if endpoint else self.git.metadata
        options=('core.fsmonitor=false','gc.auto=0','maintenance.auto=false',
                 'push.negotiate=false','push.followTags=false','push.autoSetupRemote=false','push.useForceIfIncludes=false',
                 'push.pushOption=','push.gpgSign=false','submodule.recurse=false','protocol.allow=never','protocol.file.allow=always',
                 'core.sshCommand=','credential.helper=','core.attributesFile='+os.devnull)
        argv=['git','--no-pager','--git-dir='+os.fsdecode(root)]
        for option in options: argv+=['-c',option]
        argv+=list(args)
        process=None; selector=selectors.DefaultSelector(); out=bytearray(); err=bytearray(); primary=None
        try:
            process=subprocess.Popen(argv,cwd=self.git.root,env=env,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False); selector.register(stream,selectors.EVENT_READ)
            deadline=time.monotonic()+self.timeout
            while selector.get_map():
                left=deadline-time.monotonic()
                if left<=0: raise PublicationError('transport deadline exceeded')
                for key,_ in selector.select(min(left,.1)):
                    chunk=os.read(key.fd,65536)
                    if not chunk: selector.unregister(key.fileobj); continue
                    buffer=out if key.fileobj is process.stdout else err
                    limit=self.max_output if buffer is out else 65536
                    if len(buffer)+len(chunk)>limit: raise PublicationError('transport output budget exhausted')
                    buffer.extend(chunk)
            process.wait(timeout=max(.001,deadline-time.monotonic()))
            return process.returncode,bytes(out),bytes(err)
        except BaseException as error:
            primary=error
            error.started=process is not None
            if process is not None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                except BaseException as cleanup: error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('transport-process-group:%d:%s'%(process.pid,cleanup),)
                try: process.wait(timeout=5)
                except BaseException as cleanup: error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('transport-process-wait:%d:%s'%(process.pid,cleanup),)
            raise
        finally:
            resources=[('transport-selector-close',selector.close)]
            if process is not None:
                resources.extend(('transport-pipe-close:'+label,stream.close)
                    for label,stream in (('stdout',process.stdout),('stderr',process.stderr)))
            _close_owned(resources,primary)

    def advertise(self):
        code,data,error=self._command(('ls-remote','--refs','--exit-code',os.fsdecode(self.endpoint),self.destination))
        if code==2 and not data and not error:
            oid=None
        else:
            width=40 if self.object_format=='sha1' else 64
            if code or error or not re.fullmatch(rb'[0-9a-f]{%d}\t'%width+re.escape(self.destination.encode())+rb'\n',data):
                raise PublicationError('fresh exact remote advertisement unavailable')
            oid=data.split(b'\t',1)[0].decode('ascii')
            _require(bool(oid.strip('0')),'nonzero remote OID required')
        return Advertisement(self.endpoint,self.endpoint_identity,self.destination,oid,self.object_format)

    def _commit(self,oid):
        code,kind,error=self._command(('cat-file','-t',oid),endpoint=True)
        if code or error or kind!=b'commit\n': raise PublicationError('remote commit graph unavailable')
        code,raw,error=self._command(('cat-file','commit',oid),endpoint=True)
        if code or error or object_id(b'commit',raw,self.object_format)!=oid:
            raise PublicationError('remote exact commit hash unavailable')
        return raw,parse_commit(raw,40 if self.object_format=='sha1' else 64)

    def reachability(self,observation,target):
        if type(observation) is not Advertisement or (observation.endpoint,observation.endpoint_identity,observation.destination,observation.object_format)!=(self.endpoint,self.endpoint_identity,self.destination,self.object_format) or observation.oid is None:
            raise PublicationError('qualified existing remote graph required')
        pending=[observation.oid]; seen=set(); edges=[]; found=False; total=0
        while pending:
            oid=pending.pop()
            if oid in seen: continue
            if len(seen)>=self.max_commits: raise PublicationError('remote graph budget exhausted')
            raw,(tree,parents)=self._commit(oid); total+=len(raw)
            if total>self.max_output: raise PublicationError('remote graph byte budget exhausted')
            seen.add(oid); edges.append((oid,parents))
            if oid==target: found=True; break
            pending.extend(parent for parent in parents if parent not in seen)
        if self.advertise()!=observation: raise PublicationError('remote changed during graph observation')
        return found,digest(_encode(tuple(edges)))

    def push(self,intent):
        request=(self.endpoint,self.endpoint_identity,self.destination,intent.candidate.remote.remote_oid,intent.commit_oid)
        if (intent.endpoint,intent.endpoint_identity,intent.destination)!=(self.endpoint,self.endpoint_identity,self.destination):
            raise PublicationError('frozen endpoint mismatch')
        try:
            code,out,error=self._command(('push','--porcelain','--no-verify','--no-follow-tags','--recurse-submodules=no',
                '--receive-pack=env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT git receive-pack',
                '--force-with-lease='+self.destination+':'+intent.candidate.remote.remote_oid,
                os.fsdecode(self.endpoint),intent.commit_oid+':'+self.destination))
            kind=self._push_kind(intent,code,out,error)
            outcome=PushOutcome(request,kind,hashlib.sha256(out+b'\0'+error+str(code).encode()).hexdigest())
        except BaseException as error:
            outcome=PushOutcome(request,'ambiguous' if getattr(error,'started',True) else 'known-not-sent',
                                digest(type(error).__name__))
            self._outcome=outcome; self._outcome_snapshot=(outcome.request,outcome.kind,outcome.transcript_digest)
            raise
        self._outcome=outcome; self._outcome_snapshot=(outcome.request,outcome.kind,outcome.transcript_digest)
        return outcome

    def known_not_sent(self,intent):
        self._check()
        if (intent.endpoint,intent.endpoint_identity,intent.destination)!=(self.endpoint,self.endpoint_identity,self.destination):
            raise PublicationError('frozen endpoint mismatch')
        request=(self.endpoint,self.endpoint_identity,self.destination,intent.candidate.remote.remote_oid,intent.commit_oid)
        outcome=PushOutcome(request,'known-not-sent',digest(['explicit-before-push',request[2:]]))
        self._outcome=outcome; self._outcome_snapshot=(outcome.request,outcome.kind,outcome.transcript_digest)
        return outcome

    def _push_kind(self,intent,code,out,error):
        # Git 2.39.5 porcelain is one exact To line, one ref status and Done.
        # Extra status, foreign output or incomplete report never proves refusal.
        lines=out.splitlines(keepends=True)
        if len(lines)!=3 or lines[0]!=b'To '+self.endpoint+b'\n' or lines[2]!=b'Done\n':
            return 'ambiguous'
        status=lines[1][:-1].split(b'\t') if lines[1].endswith(b'\n') else []
        expected=(intent.commit_oid+':'+self.destination).encode()
        if len(status)!=3 or status[1]!=expected: return 'ambiguous'
        flag,_,summary=status
        if code==0 and flag in (b' ',b'=',b'*') and summary and not error:
            return 'accepted'
        # Only receive-pack's exact single-reference rejection and local stale
        # lease report qualify; orchestration additionally requires a complete
        # fresh negative graph in the original epoch with no prior ambiguity.
        if code==1 and flag==b'!' and (summary==b'[rejected] (stale info)' or
                re.fullmatch(rb'\[remote rejected\] \([^\r\n\x00()]+\)',summary)):
            return 'no-update'
        return 'ambiguous'

    def qualify_outcome(self,outcome,intent):
        self._check()
        if outcome is not self._outcome or (outcome.request,outcome.kind,outcome.transcript_digest)!=self._outcome_snapshot or outcome.request!=(self.endpoint,self.endpoint_identity,self.destination,
                intent.candidate.remote.remote_oid,intent.commit_oid):
            raise PublicationError('issued exact transport outcome required')
        return outcome


def validate_candidate(git,candidate,review):
    """Independent remote-plus-selection reconstruction and full object proof."""
    width=40 if candidate.object_format=='sha1' else 64
    _require(git._config()==width and candidate.remote_object_kind==b'commit','reviewed exact commit source required')
    raw,tree,parents=git.exact_commit(candidate.remote.remote_oid)
    _require(tree==candidate.baseline_tree_oid,'original remote commit tree mismatch')
    proved=set()
    def complete(tree_oid):
        pending=[tree_oid]
        while pending:
            oid=pending.pop()
            if oid in proved: continue
            git._verify_stored_object(oid,b'tree'); proved.add(oid)
            for entry in parse_tree(git._run(('ls-tree','-z',oid)),width):
                if entry.kind==b'tree': pending.append(entry.oid)
                elif entry.kind==b'blob' and entry.mode in (b'100644',b'100755'):
                    if entry.oid not in proved: git._verify_stored_object(entry.oid,b'blob'); proved.add(entry.oid)
                else: raise PublicationError('unsupported candidate dependency')
        return git.tree_entries(tree_oid)
    remote=complete(tree)
    sink=io.BytesIO(); manifest_entry=next((e for e in remote if e.path==metadata.MANIFEST_PATH),None)
    _require(manifest_entry is not None,'complete baseline manifest required')
    git.copy_blob(manifest_entry.oid,sink)
    baseline=metadata.decode(sink.getvalue(),width=width,policy=git.policy)
    metadata.validate(baseline,remote,width=width,policy=git.policy)
    records=baseline.by_path
    expected={e.path:e for e in remote}
    for row in candidate.selected:
        _require(row in review.selected and (row.after is None or row.after.record is not None),
                 'complete selected metadata required')
        if row.after is None:
            records.pop(row.path,None); expected.pop(row.path,None)
        else:
            records[row.path]=row.after.record
            mode=b'100755' if row.after.record.mode & 0o100 else b'100644'
            expected[row.path]=TreeEntry(row.path,mode,row.after.oid,b'blob')
    necessary=metadata.ancestors(p for p,r in records.items() if r.kind=='file')
    fresh=dict(candidate.parent_dependencies)
    approvals=tuple(p for p in candidate.approved_parents if p in necessary and fresh.get(p) is not None)
    for path in approvals: records[path]=fresh[path]
    records={p:r for p,r in records.items() if r.kind=='file' or p in necessary}
    hybrid=metadata.Manifest(tuple(records[p] for p in sorted(records)))
    selection=metadata.select(baseline,hybrid,tuple(r.path for r in candidate.selected),
        approved_parents=approvals,width=width,policy=git.policy)
    payload=metadata.encode(selection.manifest,width=width,policy=git.policy)
    oid=object_id(b'blob',payload,candidate.object_format)
    _require(oid==candidate.manifest_oid and hashlib.sha256(payload).hexdigest()==candidate.manifest_digest and
             selection.parents==candidate.reviewed_parents,'exact candidate manifest and parents required')
    expected[metadata.MANIFEST_PATH]=TreeEntry(metadata.MANIFEST_PATH,b'100644',oid,b'blob')
    expected=tuple(expected[p] for p in sorted(expected))
    _require(complete(candidate.tree_oid)==expected,'complete candidate tree mismatch')
    sink=io.BytesIO(); git.copy_blob(oid,sink)
    _require(sink.getvalue()==payload,'complete candidate manifest bytes mismatch')
    metadata.validate(selection.manifest,expected,width=width,policy=git.policy)
    selection_digest=save._digest((candidate.remote,candidate.remote_object_kind,candidate.binding,review.draft,
        review.selected,review.parents,selection.parents,candidate.parent_dependencies,candidate.routes,oid))
    _require(selection_digest==candidate.selection_digest and candidate.candidate_digest==save._digest((
        candidate.tree_oid,candidate.baseline_tree_oid,candidate.remote_object_kind,selection_digest,oid)), 'complete candidate digest mismatch')
    return tuple(proved)


@dataclass(frozen=True)
class KnownRefusal:
    issuer: object
    prepared: object
    epoch: int
    outcome: PushOutcome
    observation: Advertisement
    graph_digest: str


@dataclass(frozen=True)
class PublicationProof:
    issuer: object
    prepared: object
    epoch: int
    outcome: object
    observation: Advertisement
    graph_digest: str


@dataclass(frozen=True)
class RecoveryApproval:
    issuer: object
    prepared: object
    operation_id: str
    epoch: int
    namespace: tuple
    intent_digest: str
    observation: Advertisement
    pending_effect: str
    command_id: str


def packed_pin_removal(raw,name,expected):
    """Derive an exact own-row removal from an already qualified packed file.

    This preserves every foreign byte, including headers and peeled rows.
    It grants no general packed-ref parsing or rewriting authority.
    """
    target=name.encode('ascii'); result=[]; found=False; previous=None
    for line in raw.splitlines(keepends=True):
        if line.startswith(b'^'):
            if previous==target: raise PublicationError('owned publication pin cannot be peeled')
            result.append(line); continue
        if line.startswith(b'#'):
            result.append(line); continue
        oid,separator,ref=line.rstrip(b'\n').partition(b' ')
        previous=ref
        if ref==target:
            if found or not separator or oid!=expected.encode('ascii'):
                raise PublicationError('packed owned publication pin changed')
            found=True
        else: result.append(line)
    return b''.join(result)


class NativePublication:
    def __init__(self,saver,store,transport):
        _require(type(saver) is save.NativeSave and isinstance(store,NativeOperationStore) and
                 type(transport) is GitCLITransport,'exact dormant publication peers required')
        self.saver,self.store,self.transport=saver,store,transport
        self._binding=(saver,store,transport,saver.preview,saver.preview.git,saver.disk,saver.preview.binding)
        self._recovery=None; self._refusal=None; self._commands=set(); self._recovery_snapshot=None; self._refusal_snapshot=None
        self._proof=None; self._proof_snapshot=None

    def _check(self):
        if (self.saver,self.store,self.transport,self.saver.preview,self.saver.preview.git,
                self.saver.disk,self.saver.preview.binding)!=self._binding or self.transport.git is not self.saver.preview.git:
            raise PublicationError('publication peer binding changed')
        root=self.saver.preview.binding.root
        _require(os.fsencode(self.store.data_root)==root+b'/data' and self.saver.disk.root==root,'fixed canonical owner root required')

    def _admission(self,prepared,intent):
        prepared.check()
        controls=tuple(Control(Target(b'data/native-operations/state.json','file'),payload,
            prepared.operation_id,prepared.owner_id) for name,payload in prepared.states if not prepared.is_outcome_recipe(name))
        terminal=prepared.purpose=='recovery' and _parse(prepared.initial)['workers'][prepared.operation_id]['phase'] in ('completed','failed')
        evidence=Evidence(prepared.operation_id,prepared.owner_id,base_tree=intent.candidate.baseline_tree_oid if terminal else intent.candidate.tree_oid,
            controls=controls,commit=intent.raw_commit,ref=intent.local_ref.encode(),publication=prepared)
        snapshot=self.saver.disk.snapshot()
        plan=self.saver.disk.project('publication',snapshot,evidence)
        if not plan.supported or not plan.decisions:
            raise DiskBlocked('cannot establish publication admission:'+repr(plan.unknown))
        if any(row.status=='insufficient-space' for row in plan.decisions): raise InsufficientSpace('publication capacity insufficient')
        if any(row.status!='fits' for row in plan.decisions): raise DiskBlocked('publication capacity unknown')
        return self.saver.disk.guard(plan)

    def _reflogs(self,local_ref,old,commit_oid,committer):
        git=self.saver.preview.git
        entry=old.encode()+b' '+commit_oid.encode()+b' '+committer.raw()+b'\n'
        paths=[b'data/native/repository.git/logs/'+local_ref.encode()]
        # Git appends existing logs even when automatic reflog creation is off.
        head=(git.metadata+b'/HEAD')
        with open(head,'rb') as stream: head_data=stream.read()
        if head_data==b'ref: '+local_ref.encode()+b'\n': paths.append(b'data/native/repository.git/logs/HEAD')
        return tuple((p,entry) for p in paths if os.path.lexists(git.root+b'/'+p))

    def _cleanup_pins(self,intent,admission):
        git=self.saver.preview.git; prepared=admission.plan.evidence.publication
        for number,(name,oid) in enumerate(((intent.commit_pin,intent.commit_oid),(intent.tree_pin,intent.candidate.tree_oid))):
            admission.qualify_publication(prepared)
            state=_parse(prepared._current);worker=state['workers'][prepared.operation_id]
            allow_absent=_publication_abandonment_cleanup(state,worker)
            authorized=number*2+1; retired=authorized+1; progress=worker['publication_cleanup']
            before=git.read_ref(name)
            if before.oid is not None and before.oid!=oid: raise PublicationError('owned pin changed:'+name)
            if progress>=retired:
                if before.oid is not None: raise PublicationError('retired owned pin reoccupied:'+name)
                continue
            if progress<authorized:
                if before.oid is None and not allow_absent: raise PublicationError('owned pin missing before retirement:'+name)
                proof=git.publication_retirement_proof(prepared,admission,authorized)
                self.store.publication_retirement(prepared,admission,authorized,proof)
            git.publication_delete_pin(name,expected_old=oid,admission=admission)
            # A durable authorization binds exact namespace/OID before removal;
            # only that authorized own absence can reconcile an interrupted delete.
            proof=git.publication_retirement_proof(prepared,admission,retired)
            self.store.publication_retirement(prepared,admission,retired,proof)


    def _local(self,intent,admission):
        git=self.saver.preview.git
        current=git.read_ref(intent.local_ref).oid
        if current==intent.commit_oid:
            git.publication_harden_local(intent,admission=admission)
            return 'already-advanced'
        if current!=intent.expected_local: raise PublicationError('local expected-old conflict; unrelated ref preserved')
        git.publication_ref(intent.local_ref,intent.commit_oid,expected_old=intent.expected_local,
            admission=admission,committer=intent.committer)
        return 'advanced'

    def _known_refusal(self,prepared,intent,outcome,observation,graph):
        self.transport.qualify_outcome(outcome,intent)
        if outcome.kind not in ('known-not-sent','no-update') or observation.oid is None:
            raise PublicationError('publication refusal remains uncertain')
        epoch=prepared.state('accepted')['workers'][prepared.operation_id]['epoch']
        proof=KnownRefusal(self,prepared,epoch,outcome,observation,graph)
        self._refusal=proof; self._refusal_snapshot=tuple(getattr(proof,f.name) for f in fields(proof))
        return proof

    def qualify_refusal(self,proof,prepared):
        self._check()
        if proof is not self._refusal or tuple(getattr(proof,f.name) for f in fields(proof))!=self._refusal_snapshot or proof.prepared is not prepared or proof.issuer is not self:
            raise PublicationError('issued original known refusal required')
        intent=validate_intent(prepared.artifact)
        self.transport.qualify_outcome(proof.outcome,intent)
        if proof.observation != self.transport.advertise():
            raise PublicationError('refusal proof became stale')
        reachable,graph=self.transport.reachability(proof.observation,intent.commit_oid)
        if reachable or graph!=proof.graph_digest: raise PublicationError('refusal graph became stale')
        self._refusal=None
        return proof.outcome.kind

    def _positive_proof(self,prepared,intent,observation,graph,outcome=None):
        if outcome is not None: self.transport.qualify_outcome(outcome,intent)
        epoch=_parse(prepared._current)['workers'][prepared.operation_id]['epoch']
        proof=PublicationProof(self,prepared,epoch,outcome,observation,graph)
        self._proof=proof; self._proof_snapshot=tuple(getattr(proof,f.name) for f in fields(proof))
        return proof

    def qualify_publication_proof(self,proof,prepared):
        self._check()
        if proof is not self._proof or tuple(getattr(proof,f.name) for f in fields(proof))!=self._proof_snapshot or proof.issuer is not self or proof.prepared is not prepared:
            raise PublicationError('issued current positive publication proof required')
        intent=validate_intent(prepared.artifact)
        if proof.outcome is not None: self.transport.qualify_outcome(proof.outcome,intent)
        if proof.observation!=self.transport.advertise(): raise PublicationError('positive publication observation became stale')
        reachable,graph=self.transport.reachability(proof.observation,intent.commit_oid)
        if not reachable or graph!=proof.graph_digest: raise PublicationError('positive publication graph became stale')
        self._proof=None
        return proof

    def publish(self,candidate,review,*,author,committer,message,local_ref,expected_local,command_id,expected_revision=0):
        self._check(); _id(command_id)
        if command_id in self._commands:
            return {'status':'duplicate-diagnostics-only'}
        git=self.saver.preview.git; prepared=None; admission=None
        with git.publication_local(),self.saver.publication_context(candidate,review):
            git.qualify_publication_signature(author); git.qualify_publication_signature(committer)
            proved=validate_candidate(git,candidate,review)
            observation=self.transport.advertise()
            if observation.oid!=candidate.remote.remote_oid: raise PublicationError('remote changed; fresh review required')
            _qualified_ref(local_ref)
            if git.read_ref(local_ref).oid!=expected_local: raise PublicationError('local expectation changed before acceptance')
            raw=commit_bytes(candidate,author,committer,message); commit_oid=object_id(b'commit',raw,candidate.object_format)
            found,parent_graph=self.transport.reachability(observation,commit_oid)
            if found: raise PublicationError('candidate commit is already in original parent graph')
            identity=_candidate_identity(candidate,commit_oid,self.transport.endpoint,self.transport.destination,local_ref,expected_local)
            reflogs=self._reflogs(local_ref,expected_local,commit_oid,committer)
            factory=lambda ns:_intent_bytes(candidate,review,raw,author,committer,message,self.transport,
                local_ref,expected_local,identity,reflogs,parent_graph,ns)
            prepared=self.store.prepare_publication(expected_revision,identity,factory,
                {encode_native_path(row.path):'ha' for row in candidate.selected},str(uuid.uuid4()),command_id)
            intent=validate_intent(prepared.artifact)
            with self._admission(prepared,intent) as admission:
                try:
                    self.saver.consume_publication(candidate,review)
                    self._commands.add(command_id)
                    admission.create_publication_directories(prepared)
                    self.store.accept_publication(prepared,admission)
                    self.store.publication_checkpoint(prepared,admission,'effect_started')
                    for oid in proved: git.harden_object(oid,admission=admission)
                    git.publication_ref(intent.tree_pin,candidate.tree_oid,expected_old=None,admission=admission)
                    git.write_exact_commit(raw,admission=admission)
                    git.publication_ref(intent.commit_pin,commit_oid,expected_old=None,admission=admission)
                    self.saver.preview._candidate_fence(review)
                    admission.qualify_publication(prepared)
                    current=self.transport.advertise()
                    if current!=observation:
                        outcome=self.transport.known_not_sent(intent)
                        reachable,graph=self.transport.reachability(current,commit_oid)
                        if reachable: raise PublicationError('remote unexpectedly already contains candidate')
                        proof=self._known_refusal(prepared,intent,outcome,current,graph)
                        self.store.publication_refusal(prepared,admission,proof)
                        self._cleanup_pins(intent,admission)
                        self.store.publication_cleanup(prepared,admission)
                        return {'status':'refused','operation_id':prepared.operation_id,'commit_oid':commit_oid}
                    if git.read_ref(local_ref).oid!=expected_local: raise PublicationError('local changed before push')
                    self.store.publication_checkpoint(prepared,admission,'attempt_started')
                    transport_error=None
                    try: outcome=self.transport.push(intent)
                    except Exception as error:
                        transport_error=str(error)
                        outcome=self.transport._outcome
                        if outcome is None: raise
                    fresh=self.transport.advertise(); reachable,graph=self.transport.reachability(fresh,commit_oid)
                    if not reachable:
                        proof=self._known_refusal(prepared,intent,outcome,fresh,graph)
                        self.store.publication_refusal(prepared,admission,proof)
                        self._cleanup_pins(intent,admission)
                        self.store.publication_cleanup(prepared,admission)
                        return {'status':'refused','operation_id':prepared.operation_id,'commit_oid':commit_oid}
                    proof=self._positive_proof(prepared,intent,fresh,graph,outcome)
                    self.store.publication_proof(prepared,admission,proof)
                    self._local(intent,admission)
                    self.store.publication_checkpoint(prepared,admission,'completed')
                    self._cleanup_pins(intent,admission)
                    self.store.publication_cleanup(prepared,admission)
                    return {'status':'completed','operation_id':prepared.operation_id,'commit_oid':commit_oid,'transport_diagnostic':transport_error}
                except BaseException as error:
                    error.operation_id=prepared.operation_id
                    try:
                        if prepared._used and _parse(prepared._current)['workers'][prepared.operation_id]['phase']=='effect_started':
                            self.store.publication_checkpoint(prepared,admission,'recovery_required')
                    except BaseException as secondary:
                        error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('publication-recovery-checkpoint:operation=%s:%s'%(prepared.operation_id,secondary),)
                    raise

    def diagnose(self,operation_id):
        """Explicit read-only diagnostics: no initialization or lifecycle takeover."""
        self._check(); _id(operation_id)
        from native_operations import _publication_observe
        with _publication_observe(self.store) as (root,artifacts):
            if root is None: raise PublicationError('publication store absent')
            state=_parse(self.store._read_file(root,'state.json'))
            self.store._validate(state,artifacts)
            worker=state['workers'].get(operation_id)
            if worker is None: raise PublicationError('publication operation unavailable')
            record=state['records'][worker['record_id']]
            intent=validate_intent(self.store._artifact(artifacts,record['artifact'],record['artifact_size']))
        with self.saver.preview.git.publication_local():
            pins={}
            for name in (intent.tree_pin,intent.commit_pin):
                try: pins[name]=self.saver.preview.git.read_ref(name).oid
                except Exception as error: pins[name]={'unavailable':str(error)}
            local=self.saver.preview.git.read_ref(intent.local_ref).oid
        return {'operation_id':operation_id,'phase':worker['phase'],'owner':worker['owner'],'epoch':worker['epoch'],
                'original_epoch':intent.namespace['epoch'],'commit_oid':intent.commit_oid,'intent_digest':record['artifact'],
                'instance_id':state['instance_id'],'generation':record['generation'],'pins':pins,'local_oid':local,
                'recovery_pin':state['recovery'],'execution_authority':False}

    def approve_recovery(self,operation_id,*,expected_revision,command_id,pending_effect='reconcile'):
        """Called only for a current explicit user action; one live attempt."""
        self._check(); _id(command_id)
        if pending_effect not in ('reconcile','push','cleanup','abandon') or command_id in self._commands:
            raise PublicationError('fresh explicit recovery command required')
        prepared=self.store.prepare_publication_recovery(operation_id,expected_revision,command_id=command_id,pending_effect=pending_effect,abandon=pending_effect=='abandon')
        intent=validate_intent(prepared.artifact)
        if (intent.endpoint,intent.endpoint_identity,intent.destination)!=(self.transport.endpoint,self.transport.endpoint_identity,self.transport.destination):
            raise PublicationError('recovery endpoint changed')
        worker=_parse(prepared.initial)['workers'][operation_id]
        if (worker['phase'] in ('completed','failed')) != (pending_effect=='cleanup'):
            raise PublicationError('pending recovery effect does not match worker')
        observation=self.transport.advertise()
        authority=worker['publication_cleanup_owner'] if worker['phase'] in ('completed','failed') else {'owner':worker['owner'],'epoch':worker['epoch']}
        approval=RecoveryApproval(self,prepared,operation_id,authority['epoch'],prepared.namespace,
            hashlib.sha256(prepared.artifact).hexdigest(),observation,pending_effect,command_id)
        self._recovery=approval; self._recovery_snapshot=tuple(getattr(approval,f.name) for f in fields(approval))
        return approval

    def resume(self,approval):
        self._check()
        if type(approval) is not RecoveryApproval or approval is not self._recovery or approval.issuer is not self or tuple(getattr(approval,f.name) for f in fields(approval))!=self._recovery_snapshot:
            raise PublicationError('issued one-attempt recovery approval required')
        prepared=approval.prepared.check(); intent=validate_intent(prepared.artifact)
        state=_parse(prepared.initial); worker=state['workers'][approval.operation_id]
        authority=worker['publication_cleanup_owner'] if worker['phase'] in ('completed','failed') else {'owner':worker['owner'],'epoch':worker['epoch']}
        if (authority['epoch']!=approval.epoch or authority['owner']!=self.store.owner or
                prepared.namespace!=approval.namespace or hashlib.sha256(prepared.artifact).hexdigest()!=approval.intent_digest or
                approval.command_id in self._commands):
            raise PublicationError('recovery approval namespace or owner changed')
        self._recovery=None; self._commands.add(approval.command_id)
        git=self.saver.preview.git
        with git.publication_local():
            if self.transport.advertise()!=approval.observation: raise PublicationError('remote observation changed; approve a new attempt')
            if approval.pending_effect not in ('abandon','cleanup'):
                validate_candidate(git,intent.candidate,intent.review)
                git.exact_commit(intent.commit_oid)
            for name,oid in ((intent.tree_pin,intent.candidate.tree_oid),(intent.commit_pin,intent.commit_oid)):
                observed=git.read_ref(name).oid
                retired_at=1 if name==intent.commit_pin else 3
                authorized_absence=approval.pending_effect=='cleanup' and (
                    worker['publication_cleanup']>=retired_at or _publication_abandonment_cleanup(state,worker))
                if observed!=oid and not (observed is None and (approval.pending_effect=='abandon' or authorized_absence)):
                    raise PublicationError('missing or changed acknowledged publication pin:'+name)
            with self._admission(prepared,intent) as admission:
                try:
                    admission.create_publication_directories(prepared)
                    self.store.publication_command(prepared,admission)
                    if approval.pending_effect=='abandon':
                        reachable,graph=self.transport.reachability(approval.observation,intent.commit_oid)
                        if reachable: raise PublicationError('candidate reachable; abandonment forbidden')
                        outcome=self.transport.known_not_sent(intent)
                        proof=self._known_refusal(prepared,intent,outcome,approval.observation,graph)
                        self.store.publication_refusal(prepared,admission,proof)
                        self._cleanup_pins(intent,admission)
                        self.store.publication_cleanup(prepared,admission)
                        return {'status':'refused','operation_id':approval.operation_id,'commit_oid':intent.commit_oid}
                    if approval.pending_effect=='cleanup':
                        self._cleanup_pins(intent,admission)
                        self.store.publication_cleanup(prepared,admission)
                        return {'status':'completed' if worker['phase']=='completed' else 'refused','operation_id':approval.operation_id,'commit_oid':intent.commit_oid}
                    reachable,graph=self.transport.reachability(approval.observation,intent.commit_oid)
                    observation=approval.observation; outcome=None
                    if not reachable:
                        if approval.pending_effect!='push' or approval.observation.oid!=intent.candidate.remote.remote_oid:
                            raise PublicationError('publication unproved; fresh review or explicit same-commit push required')
                        admission.qualify_publication(prepared)
                        if self.transport.advertise()!=approval.observation: raise PublicationError('remote changed before explicit resume push')
                        if worker['publication_attempt']=='not-sent':
                            self.store.publication_checkpoint(prepared,admission,'attempt_started')
                        try: outcome=self.transport.push(intent)
                        except Exception: pass
                        fresh=self.transport.advertise()
                        reachable,graph=self.transport.reachability(fresh,intent.commit_oid)
                        if not reachable: raise PublicationError('prior publication remains uncertain; recovery retained')
                        observation=fresh
                        if outcome is None: outcome=self.transport._outcome
                    proof=self._positive_proof(prepared,intent,observation,graph,outcome)
                    self.store.publication_proof(prepared,admission,proof)
                    self._local(intent,admission)
                    phase='recovered_completed' if worker['phase']=='recovery_required' else 'completed'
                    self.store.publication_checkpoint(prepared,admission,phase)
                    self._cleanup_pins(intent,admission)
                    self.store.publication_cleanup(prepared,admission)
                    return {'status':'completed','operation_id':approval.operation_id,'commit_oid':intent.commit_oid}
                except BaseException as error:
                    error.operation_id=approval.operation_id
                    try:
                        if _parse(prepared._current)['workers'][approval.operation_id]['phase']=='effect_started':
                            self.store.publication_checkpoint(prepared,admission,'recovery_required')
                    except BaseException as secondary:
                        error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('publication-recovery-checkpoint:operation=%s:%s'%(approval.operation_id,secondary),)
                    raise
