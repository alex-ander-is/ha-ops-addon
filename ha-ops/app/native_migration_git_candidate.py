"""Dormant local migration Git candidate; no startup or production entrypoint.

The frozen remote observation is ancestry, not a claim of network freshness.
Saved diagnostics never confer review, publication or activation authority.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import base64
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import uuid

from native_inventory import Blocked, beneath
from native_disk import Blocked as DiskBlocked
from native_git import NativeGit, IndexEntry
from native_metadata import Manifest, Record, MANIFEST_PATH, ancestors, encode, validate
from native_migration import (PersistenceUncertain, Conflict, _digest, _json, _unique,
                              _stat, _ref, encode_path, decode_path, MAX_CONTROL)
from native_migration_transition import Transition, identity

STORE = b'data/native-migration-git-candidate'
CHUNK = 65536
MAX_ROWS = 20000
MAX_BYTES = (1 << 63) - 1


@dataclass(frozen=True)
class Recipe:
    owner: object
    operation: str
    sizes: tuple
    entries: int
    pending: str
    expected: object
    control_limit: int = MAX_CONTROL

    def check(self, root):
        if (self.owner.root != root or self.owner._recipes.get(id(self)) is not self
                or not re.fullmatch('[0-9a-f]{32}', self.operation)
                or type(self.pending) is not str or not re.fullmatch('[0-9a-f]{32}',self.pending)
                or type(self.control_limit) is not int or not 0<self.control_limit<=MAX_CONTROL
                or self.pending==self.operation
                or self.expected is not None and (type(self.expected) is not str or not re.fullmatch('[0-9a-f]{64}',self.expected))
                or type(self.sizes) is not tuple or len(self.sizes) > MAX_ROWS * 4
                or any(type(n) is not int or not 0 <= n <= MAX_BYTES for n in self.sizes)
                or sum(self.sizes) > MAX_BYTES or type(self.entries) is not int
                or not 0 < self.entries <= MAX_ROWS * 8):
            raise Blocked('issued-bounded-migration-git-recipe-required')


def _oid(raw, fmt, kind=b'blob'):
    h = hashlib.new(fmt)
    h.update(kind + b' ' + str(len(raw)).encode() + b'\0' + raw)
    return h.hexdigest()


def _raw(data):
    if type(data) is not str:
        raise Blocked('invalid-byte-content')
    try: value = base64.b64decode(data, validate=True)
    except ValueError: raise Blocked('invalid-byte-content') from None
    if base64.b64encode(value).decode() != data:
        raise Blocked('noncanonical-byte-content')
    return value


class Candidate:
    """Explicit builder, immutable diagnostic reads and sticky revalidation."""
    def __init__(self, preparation, *, trace=lambda event: None):
        self.p, self.root = preparation, preparation.root
        self.t = Transition(preparation, trace=trace)
        self.trace = trace
        self._recipes = {}

    def _info(self, path):
        return self.t._info(path)

    def _effect(self, event, path, call):
        value = call()
        self.trace((event, encode_path(path)))
        return value

    def _directory(self, path):
        info = self._info(path)
        if (info is None or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise Blocked('unsafe-candidate-directory')
        return info

    def _mkdir(self, path):
        with self.t._parent(path) as (parent, name):
            issued=self._effect('candidate-mkdir', path, lambda: (os.mkdir(name, 0o700, dir_fd=parent),
                os.stat(name,dir_fd=parent,follow_symlinks=False))[1])
            self._effect('candidate-directory-fsync', path, lambda: os.fsync(parent))
        if identity(self._directory(path))!=identity(issued):raise Blocked('candidate-directory-replaced')
        return identity(issued)

    def _file(self, path, chunks):
        with self.t._parent(path) as (parent, name):
            fd = self._effect('candidate-open', path, lambda: os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent))
            try:
                source = (chunks,) if isinstance(chunks, bytes) else chunks
                for chunk in source:
                    view = memoryview(chunk)
                    while view:
                        n = self._effect('candidate-write', path, lambda: os.write(fd, view))
                        if n <= 0: raise OSError('short-candidate-write')
                        view = view[n:]
                self._effect('candidate-file-fsync', path, lambda: os.fsync(fd))
                issued=_stat(os.fstat(fd))
            finally: os.close(fd)
            self._effect('candidate-directory-fsync', path, lambda: os.fsync(parent))
        return issued

    @contextmanager
    def _lease(self):
        self._directory(STORE)
        with self.t._parent(STORE) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = identity(os.fstat(fd))
                yield
                if identity(self._directory(STORE)) != before:
                    raise Blocked('candidate-store-replaced')
            finally: os.close(fd)

    def _manifest(self, base):
        rows = []; todo = [b'']
        while todo:
            relative = todo.pop()
            path = base + (b'/' + relative if relative else b'')
            with self.p._root() as root:
                fd = self.p._directory(root, path)
                try:
                    info = os.fstat(fd)
                    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise Blocked('unsafe-candidate-directory')
                    for name in sorted(os.fsencode(n) for n in os.listdir(fd)):
                        rel = relative + b'/' + name if relative else name
                        s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        self.p.binding.check(base+b'/'+rel, s, self.p._mounts())
                        directory = stat.S_ISDIR(s.st_mode)
                        git_object = beneath(rel, b'root/data/native/repository.git/objects')
                        if (s.st_uid != os.geteuid() or not (directory or stat.S_ISREG(s.st_mode))
                                or not directory and s.st_nlink != 1
                                or stat.S_IMODE(s.st_mode) not in ((0o700,) if directory else ((0o600,0o444) if git_object else (0o600,)))):
                            raise Blocked('unsafe-candidate-artifact')
                        rows.append(dict(path=encode_path(rel), stat=_stat(s),
                            digest=None if directory else self.t._hash(base+b'/'+rel)))
                        if directory: todo.append(rel)
                        if len(rows) > MAX_ROWS: raise Blocked('candidate-artifact-limit')
                finally: os.close(fd)
        return sorted(rows, key=lambda r: decode_path(r['path']))

    def _policy(self, repository):
        return replace(self.p.policy, repository_files=tuple(sorted(set(repository) | {MANIFEST_PATH})))

    def _validate(self, value):
        keys = 'schema operation preparation transition runtime transition_operation previous parent_ref parent_oid parent_tree format tree commit raw rows metadata repository artifacts generation store stale validation candidate digest'.split()
        if (type(value) is not dict or set(value) != set(keys) or type(value['schema']) is not int
                or value['schema'] != 1 or type(value['operation']) is not str
                or not re.fullmatch('[0-9a-f]{32}', value['operation'])
                or type(value['stale']) is not bool or value['validation'] not in ('unchecked','pending','current','stale')
                or value['stale'] != (value['validation']=='stale') or value['format'] not in ('sha1','sha256')
                or value['digest'] != _digest({k:v for k,v in value.items() if k != 'digest'})):
            raise Blocked('invalid-migration-git-candidate')
        if value['candidate']!=_digest({k:v for k,v in value.items() if k not in ('digest','candidate','stale','validation')}):raise Blocked('candidate-immutable-identity-mismatch')
        width = 40 if value['format'] == 'sha1' else 64
        for key in ('parent_oid','parent_tree','tree','commit'):
            if type(value[key]) is not str or not re.fullmatch('[0-9a-f]{%d}' % width, value[key]):
                raise Blocked('invalid-candidate-object-id')
        if value['previous'] is not None and (type(value['previous']) is not str or not re.fullmatch('[0-9a-f]{64}',value['previous'])):raise Blocked('invalid-previous-candidate')
        if type(value['transition_operation']) is not str or not re.fullmatch('[0-9a-f]{32}',value['transition_operation']):raise Blocked('invalid-transition-operation')
        for key in ('preparation','transition'):
            if type(value[key]) is not str or not re.fullmatch('[0-9a-f]{64}',value[key]):
                raise Blocked('invalid-candidate-binding')
        for key in ('generation','store'):
            if (type(value[key]) is not list or len(value[key]) != 5
                    or any(type(n) is not int or n < 0 for n in value[key])):
                raise Blocked('invalid-candidate-identity')
        ref = _raw(value['parent_ref'])
        if not _ref(ref) or not ref.startswith(b'refs/remotes/') or not ref[len(b'refs/remotes/'):]:
            raise Blocked('remote-tracking-parent-required')
        if (type(value['rows']) is not list or len(value['rows']) > MAX_ROWS
                or type(value['artifacts']) is not list or len(value['artifacts']) > MAX_ROWS
                or type(value['repository']) is not list):
            raise Blocked('invalid-candidate-rows')
        seen = set()
        for row in value['rows']:
            if type(row) is not dict or set(row) != {'before','after','action','metadata'}:
                raise Blocked('invalid-candidate-review-row')
            if row['action'] not in ('unchanged','move','remove','generate','mode'):
                raise Blocked('invalid-candidate-review-action')
            before,after,action=row['before'],row['after'],row['action']
            if (action=='remove' and (before is None or after is not None or row['metadata'] is not None)
                    or action=='generate' and (before is not None or after is None or row['metadata'] is not None)
                    or action in ('unchanged','mode','move') and (before is None or after is None)):
                raise Blocked('impossible-candidate-review-row')
            if row['metadata'] is not None:
                m=row['metadata']
                if (type(m) is not dict or set(m)!={'uid','gid','mode','origin'}
                        or any(type(m[k]) is not int or not 0<=m[k]<(2**32-1) for k in ('uid','gid'))
                        or type(m['mode']) is not int or not 0<=m['mode']<=0o7777):raise Blocked('invalid-review-metadata')
                decode_path(m['origin'])
            for side in ('before','after'):
                entry = row[side]
                if entry is None: continue
                if type(entry) is not dict or set(entry) != {'path','mode','oid'}:
                    raise Blocked('invalid-candidate-review-entry')
                path = decode_path(entry['path'])
                if (entry['mode'] not in ('100644','100755') or type(entry['oid']) is not str
                        or not re.fullmatch('[0-9a-f]{%d}' % width,entry['oid'])):
                    raise Blocked('invalid-candidate-review-object')
                if side == 'after':
                    if path in seen: raise Blocked('duplicate-candidate-output')
                    seen.add(path)
        artifact_paths=set()
        policy=self._policy(tuple(decode_path(p) for p in value['repository']))
        for row in value['rows']:
            before,after,action=row['before'],row['after'],row['action']
            if action=='unchanged' and before!=after:raise Blocked('mislabeled-unchanged-review')
            if action=='mode' and (before['path']!=after['path'] or before['oid']!=after['oid'] or before['mode']==after['mode']):raise Blocked('mislabeled-mode-review')
            if action=='move' and (before['path']==after['path'] or before['oid']!=after['oid']):raise Blocked('mislabeled-move-review')
            if action in ('move','mode','unchanged'):
                source=decode_path(before['path']);target=decode_path(after['path'])
                if self.p._destination(source)!=target or self.p._reason(source,target) is not None:raise Blocked('invalid-review-mapping')
                managed=target not in policy.repository_files
                if managed != (row['metadata'] is not None):raise Blocked('missing-or-unexpected-review-metadata')
                if managed and decode_path(row['metadata']['origin'])!=source:raise Blocked('invalid-review-metadata-origin')
            if action=='generate' and decode_path(after['path']) not in (b'.gitignore',MANIFEST_PATH):raise Blocked('unexpected-generated-review')
            if action=='remove' and decode_path(before['path'])!=b'.gitignore' and self.p._reason(decode_path(before['path']),self.p._destination(decode_path(before['path']))) is None:raise Blocked('unexpected-removal-review')
        for row in value['artifacts']:
            if type(row) is not dict or set(row) != {'path','stat','digest'}:
                raise Blocked('invalid-candidate-artifact-record')
            path=decode_path(row['path'])
            if path in artifact_paths:raise Blocked('duplicate-candidate-artifact')
            artifact_paths.add(path)
            if (type(row['stat']) is not list or len(row['stat']) != 9
                    or any(type(n) is not int or n < 0 for n in row['stat'])
                    or row['digest'] is not None and (type(row['digest']) is not str
                        or not re.fullmatch('[0-9a-f]{64}',row['digest']))):
                raise Blocked('invalid-candidate-artifact-record')
        repository = tuple(decode_path(p) for p in value['repository'])
        if len(set(repository)) != len(repository): raise Blocked('duplicate-repository-file')
        raw = _raw(value['raw'])
        if _oid(raw,value['format'],b'commit') != value['commit']:
            raise Blocked('candidate-commit-content-mismatch')
        return value

    def read(self):
        """No source observation, resume, repair, allocation or Git writes."""
        if self._info(STORE) is None: return None
        self._directory(STORE)
        with self.p._root() as root:
            fd = self.p._directory(root, STORE)
            try:
                for name in (os.fsencode(n) for n in os.listdir(fd)):
                    info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    directory=bool(re.fullmatch(rb'[0-9a-f]{32}',name))
                    safe=(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode)==0o700 if directory
                          else stat.S_ISREG(info.st_mode) and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600)
                    if not safe or info.st_uid!=os.geteuid():
                        raise Blocked('unsafe-candidate-store-entry')
                    if name != b'candidate.json' and not re.fullmatch(rb'(?:[0-9a-f]{32}|\.pending-[0-9a-f]{32})',name):
                        raise Blocked('foreign-candidate-store-entry')
            finally: os.close(fd)
        path = STORE+b'/candidate.json'
        info = self._info(path)
        if info is None: return None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise Blocked('unsafe-candidate-control')
        value = self._validate(json.loads(self.t._read(path,MAX_CONTROL),object_pairs_hook=_unique))
        base = STORE+b'/'+value['operation'].encode()
        if (identity(self._directory(STORE)) != value['store']
                or identity(self._directory(base)) != value['generation']
                or self._manifest(base) != value['artifacts']):
            raise Blocked('candidate-artifacts-replaced-or-corrupt')
        git = NativeGit(self.root+b'/'+base+b'/root',policy=self._policy(tuple(decode_path(p) for p in value['repository'])))
        raw, tree, parents = git.exact_commit(value['commit'])
        if raw != _raw(value['raw']) or tree != value['tree'] or parents != (value['parent_oid'],):
            raise Blocked('candidate-commit-review-mismatch')
        _,parent_tree,_=git.exact_commit(value['parent_oid'])
        if parent_tree!=value['parent_tree']:raise Blocked('candidate-parent-tree-mismatch')
        before=sorted((decode_path(r['before']['path']),r['before']['mode'].encode(),r['before']['oid']) for r in value['rows'] if r['before'])
        if before!=sorted((e.path,e.mode,e.oid) for e in git.tree_entries(parent_tree)):raise Blocked('incomplete-candidate-before-review')
        expected = sorted((decode_path(r['after']['path']),r['after']['mode'].encode(),r['after']['oid']) for r in value['rows'] if r['after'])
        if sorted((e.path,e.mode,e.oid) for e in git.tree_entries(tree)) != expected:
            raise Blocked('candidate-tree-review-mismatch')
        from native_metadata import decode
        sink = io.BytesIO(); manifest_oid = next(e.oid for e in git.tree_entries(tree) if e.path==MANIFEST_PATH)
        git.copy_blob(manifest_oid,sink)
        if base64.b64encode(sink.getvalue()).decode() != value['metadata']:
            raise Blocked('candidate-metadata-review-mismatch')
        manifest=decode(sink.getvalue(),width=git._config(),policy=git.policy)
        validate(manifest,git.tree_entries(tree),width=git._config(),policy=git.policy)
        for row in value['rows']:
            if row['metadata'] is not None:
                record=manifest.by_path.get(decode_path(row['after']['path'])) if row['after'] else None
                if record is None or any(getattr(record,k)!=row['metadata'][k] for k in ('uid','gid','mode')):raise Blocked('review-metadata-content-mismatch')
        return value

    def diagnostics(self):
        """Expose retained incomplete evidence without adopting or resuming it."""
        current=self.read()
        if self._info(STORE) is None:return dict(candidate=current,incomplete=[],pending=[])
        with self.p._root() as root:
            fd=self.p._directory(root,STORE)
            try:names=sorted(os.fsencode(n) for n in os.listdir(fd))
            finally:os.close(fd)
        selected=None if current is None else current['operation'].encode()
        return dict(candidate=current,
                    incomplete=[n.decode() for n in names if re.fullmatch(rb'[0-9a-f]{32}',n) and n!=selected],
                    pending=[n.decode() for n in names if re.fullmatch(rb'\.pending-[0-9a-f]{32}',n)])

    def read_content(self, *, candidate, row, side, destination):
        value = self.read()
        if value is None or candidate not in (value['digest'],value['candidate']): raise Conflict('candidate-cas-conflict')
        if type(row) is not int or not 0 <= row < len(value['rows']) or side not in ('before','after'):
            raise Blocked('invalid-content-reference')
        entry = value['rows'][row][side]
        if entry is None: raise Blocked('absent-content-reference')
        base=STORE+b'/'+value['operation'].encode()+b'/root'
        NativeGit(self.root+b'/'+base).copy_blob(entry['oid'],destination)

    def content_info(self, *, candidate, row, side):
        """Exact immutable blob descriptor, without reading the blob into RAM."""
        value = self.read()
        if value is None or candidate != value['candidate']:
            raise Conflict('candidate-cas-conflict')
        if type(row) is not int or not 0 <= row < len(value['rows']) or side not in ('before', 'after'):
            raise Blocked('invalid-content-reference')
        entry = value['rows'][row][side]
        if entry is None: raise Blocked('absent-content-reference')
        base = STORE+b'/'+value['operation'].encode()+b'/root'
        git = NativeGit(self.root+b'/'+base)
        git._object(entry['oid'], b'blob')
        raw = git._run(('cat-file', '-s', entry['oid']))
        if not re.fullmatch(rb'(?:0|[1-9][0-9]*)\n', raw): raise Blocked('invalid-blob-size')
        return dict(oid=entry['oid'], size=int(raw), path=entry['path'])

    def current_proof(self, expected):
        """Pure proof for callers already holding common disk admission."""
        value = self.read()
        if (value is None or value['candidate'] != expected['candidate'] or value['stale']
                or value['validation'] != 'current'):
            raise Conflict('current-candidate-required')
        state, accepted, _, _ = self._proof(value['preparation'], value['transition_operation'],
                                           _raw(value['parent_ref']), value['parent_oid'])
        if state['digest'] != value['transition'] or state['runtime'] != value['runtime']:
            raise Conflict('candidate-transition-drift')
        return value, accepted

    def saved_descriptor(self):
        """Bounded no-follow control identity for a held immutable view."""
        info=self._info(STORE+b'/candidate.json')
        if (info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid()):
            raise Blocked('unsafe-candidate-control')
        return _stat(info)

    def saved_identity(self):
        """Pointer-only check for immutable content-page receipts; no source IO."""
        path = STORE+b'/candidate.json'
        info = self._info(path)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise Blocked('unsafe-candidate-control')
        return self._validate(json.loads(self.t._read(path, MAX_CONTROL), object_pairs_hook=_unique))['candidate']

    def _proof(self, expected_preparation, transition_operation, parent_ref, parent_oid):
        state=self.t.completed_evidence(candidate=expected_preparation,operation=transition_operation)
        accepted=self.p.read()
        git=NativeGit(self.root)
        if (type(parent_ref) is not bytes or not _ref(parent_ref) or not parent_ref.startswith(b'refs/remotes/')
                or git.read_ref(os.fsdecode(parent_ref)).oid != parent_oid):
            raise Blocked('exact-frozen-remote-parent-required')
        refs=base64.b64decode(accepted['observation']['git']['refs'])
        matches=[line.split(b'\0') for line in refs.splitlines() if line.split(b'\0')[0]==parent_ref]
        if len(matches)!=1 or matches[0]!=[parent_ref,parent_oid.encode(),b'']:
            raise Blocked('parent-not-in-frozen-direct-remote-observation')
        raw,tree,parents=git.exact_commit(parent_oid)
        return state,accepted,git,tree

    def _layout(self, accepted, git, tree):
        source={decode_path(r['path']):r for r in accepted['observation']['source']}
        native={decode_path(r['path']):r for r in accepted['observation']['native']}
        entries=[]; records={}; rows=[]; repository=[]
        def entry(e,path=None,mode=None):
            return dict(path=encode_path(path if path is not None else e.path),mode=(mode or e.mode).decode(),oid=e.oid)
        def metadata(path,obj,oid):
            if obj is None or obj['kind'] != ('directory' if oid is None else 'file'):
                raise Blocked('missing-frozen-metadata-origin')
            return Record(path,obj['kind'],obj['uid'],obj['gid'],obj['mode'],oid)
        for e in git.tree_entries(tree):
            if e.mode not in (b'100644',b'100755') or e.kind!=b'blob': raise Blocked('unsupported-parent-tree-leaf')
            destination=self.p._destination(e.path);reason=self.p._reason(e.path,destination)
            before=entry(e)
            if e.path==b'.gitignore':
                rows.append(dict(before=before,after=None,action='remove',metadata=None));continue
            if reason is not None:
                rows.append(dict(before=before,after=None,action='remove',metadata=None));continue
            if destination is None: raise Blocked('unresolved-parent-tree-leaf')
            if destination==MANIFEST_PATH or destination==b'.gitignore':raise Blocked('generated-control-collision')
            obj=source.get(e.path,{}).get('object')
            if destination in self.p.policy.repository_files:
                repository.append(destination);mode=e.mode;record=None
            else:
                record=metadata(destination,obj,e.oid);records[destination]=record
                mode=b'100755' if record.mode & 0o100 else b'100644'
                # Prove the exact metadata origin still names the frozen bytes.
                with self.p._root() as root:
                    actual=self.p._lstat(root,self.p.legacy+b'/'+e.path)
                    if _stat(actual)!=obj['identity'] or self.p._read(root,self.p.legacy+b'/'+e.path)!=obj['digest']:
                        raise Blocked('metadata-origin-drift')
            entries.append(IndexEntry(destination,mode,e.oid,0))
            rows.append(dict(before=before,after=entry(e,destination,mode),action='move' if e.path!=destination else 'mode' if mode!=e.mode else 'unchanged',
                metadata=None if record is None else dict(uid=record.uid,gid=record.gid,mode=record.mode,origin=encode_path(e.path))))
        paths=[e.path for e in entries]
        if len(paths)!=len(set(paths)) or set(paths)&ancestors(paths):raise Blocked('candidate-layout-collision')
        for parent in ancestors(records):
            # Mapped source directory is preferred; immutable native anchors
            # supply above-mapping parents. Never invent defaults.
            origins=[a+parent[len(b):] for a,b in self.p.mappings if beneath(parent,b)]
            obj=source.get(origins[0],{}).get('object') if origins else native.get(parent,{}).get('object')
            if obj is None:obj=native.get(parent,{}).get('object')
            records[parent]=metadata(parent,obj,None)
        policy=self._policy(repository)
        manifest=Manifest(tuple(records.values()));encoded=encode(manifest,width=git._config(),policy=policy)
        # Literal path allow rules include every ancestor, never a subtree glob.
        rules={b'.gitignore',MANIFEST_PATH}|set(paths)
        directories=ancestors(rules)
        def literal(p):
            return b''.join(b'\\'+bytes([c]) if c in b'\\*?[]!# ' else bytes([c]) for c in p)
        ignore=b'*\n'+b''.join(b'!/'+literal(p)+b'/\n' for p in sorted(directories))+b''.join(b'!/'+literal(p)+b'\n' for p in sorted(rules))
        return entries,rows,encoded,ignore,policy

    def _publish(self, value, expected, recipe):
        recipe.check(self.root)
        if recipe.expected!=expected:raise Blocked('publication-recipe-binding-conflict')
        current=self.read()
        if (None if current is None else current['digest']) != expected:raise Conflict('candidate-cas-conflict')
        value['candidate']=_digest({k:v for k,v in value.items() if k not in ('digest','candidate','stale','validation')})
        value['digest']=_digest({k:v for k,v in value.items() if k!='digest'})
        self._validate(value);payload=_json(value)
        if len(payload)>recipe.control_limit:raise Blocked('candidate-control-limit')
        pending=STORE+b'/.pending-'+recipe.pending.encode()
        pointer_before=self._info(STORE+b'/candidate.json')
        before_stat=None if pointer_before is None else _stat(pointer_before)
        # The exact final payload is bound to this issued token before any
        # control bytes are opened; the recipe already charged its full bound.
        binding=(pending,hashlib.sha256(payload).hexdigest(),expected)
        issued=self._file(pending,payload)
        if self.t._read(pending,MAX_CONTROL)!=payload:raise Blocked('candidate-control-readback')
        current=self.read()
        now=self._info(STORE+b'/candidate.json')
        if (None if current is None else current['digest'])!=expected or (None if now is None else _stat(now))!=before_stat:raise Conflict('candidate-pointer-cas-conflict')
        if binding!=(pending,hashlib.sha256(self.t._read(pending,MAX_CONTROL)).hexdigest(),recipe.expected):raise Blocked('publication-payload-binding-conflict')
        with self.t._parent(pending) as (parent,name):
            info=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if _stat(info)!=issued or info.st_uid!=os.geteuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:raise Blocked('unsafe-or-replaced-pending-control')
            try:
                self._effect('candidate-replace',pending,lambda:os.replace(name,b'candidate.json',src_dir_fd=parent,dst_dir_fd=parent))
                self._effect('candidate-publish-fsync',STORE,lambda:os.fsync(parent))
            except OSError as exc:raise PersistenceUncertain('candidate-pointer-durability-uncertain') from exc

    def build(self, *, expected_preparation, transition_operation, parent_ref, parent_oid,
              operation, expected_candidate, identity_line, message=b'Build native migration layout\n'):
        if type(operation) is not str or not re.fullmatch('[0-9a-f]{32}',operation):raise Blocked('canonical-operation-required')
        if (type(identity_line) is not bytes or not re.fullmatch(rb'[^\x00-\x20<>][^\x00\n\r<>]* <[^\x00-\x20<>]+> [0-9]{1,19} [+-][0-9]{4}',identity_line)
                or type(message) is not bytes or not message or b'\0' in message):raise Blocked('frozen-valid-commit-identity-required')
        zone=identity_line[-5:]
        timestamp=identity_line.rsplit(b' ',2)[1]
        if int(zone[1:3])>23 or int(zone[3:])>59 or int(timestamp)>MAX_BYTES:raise Blocked('invalid-commit-time')
        raw_commit_bound=2*len(identity_line)+len(message)+400
        if ((raw_commit_bound+2)//3)*4>MAX_CONTROL:raise Blocked('candidate-commit-envelope-limit-before-write')
        existing=self.read()
        if existing is not None and existing['operation']==operation:
            if (existing['previous']!=expected_candidate or existing['preparation']!=expected_preparation or existing['transition_operation']!=transition_operation or existing['parent_oid']!=parent_oid or _raw(existing['parent_ref'])!=parent_ref):raise Conflict('operation-binding-conflict')
            old_raw=_raw(existing['raw'])
            expected_raw=b'tree '+existing['tree'].encode()+b'\nparent '+parent_oid.encode()+b'\nauthor '+identity_line+b'\ncommitter '+identity_line+b'\n\n'+message
            if expected_raw!=old_raw:raise Conflict('operation-identity-conflict')
            return existing
        if (None if existing is None else existing['digest'])!=expected_candidate:raise Conflict('candidate-cas-conflict')
        state,accepted,source,tree=self._proof(expected_preparation,transition_operation,parent_ref,parent_oid)
        entries,rows,metadata,ignore,policy=self._layout(accepted,source,tree)
        objects=[r for r in self.t._manifest(b'data/native/repository.git/objects')]
        if len(objects)+len(ancestors(e.path for e in entries))+300>MAX_ROWS or len(rows)+2>MAX_ROWS:raise Blocked('candidate-row-limit-before-write')
        artifact_bound=sum(len(r['path'])*2+1024 for r in objects)+(len(ancestors(e.path for e in entries))+400)*2048
        envelope_bound=len(_json(rows))+len(_json(state['runtime']))+((len(metadata)+2)//3)*4+((raw_commit_bound+2)//3)*4+artifact_bound+len(_json([encode_path(p) for p in policy.repository_files]))+8192
        if envelope_bound>MAX_CONTROL:raise Blocked('candidate-control-limit-before-write')
        # Independent full history copy plus worst-case new objects, isolated
        # index/spool/resource journals and two maximum pointer envelopes.
        sizes=tuple(r['size'] for r in objects if not r['directory'])
        extra=(len(metadata)*2+1024,len(ignore)*2+1024,raw_commit_bound*2+1024,MAX_CONTROL,MAX_CONTROL,
               16*1024**2+sum(len(e.path)+256 for e in entries)*4)
        recipe=Recipe(self,operation,sizes+extra,len(objects)+len(ancestors(e.path for e in entries))+len(entries)*4+600,uuid.uuid4().hex,expected_candidate)
        self._recipes[id(recipe)]=recipe
        with self.p.disk.migration_git_candidate_guard(recipe):
            self._proof(expected_preparation,transition_operation,parent_ref,parent_oid)
            if self._info(STORE) is None:self._mkdir(STORE)
            with self._lease():
                current=self.read()
                if (None if current is None else current['digest'])!=expected_candidate:raise Conflict('candidate-cas-conflict')
                base=STORE+b'/'+operation.encode()
                if self._info(base) is not None:raise Blocked('incomplete-or-foreign-candidate-generation')
                generation=self._mkdir(base)
                for path in (b'root',b'root/data',b'root/data/native',b'root/data/native/repository.git',b'root/data/native/repository.git/objects',b'root/data/native/repository.git/refs'):
                    self._mkdir(base+b'/'+path)
                target=base+b'/root/data/native/repository.git'
                self._file(target+b'/HEAD',b'ref: refs/heads/migration-candidate\n')
                self._file(target+b'/config',self.t._config())
                for row in objects:
                    relative=decode_path(row['path']);dest=target+b'/objects/'+relative
                    if row['directory']:self._mkdir(dest)
                    else:
                        origin=b'data/native/repository.git/objects/'+relative
                        def chunks(origin=origin,row=row):
                            with self.t._parent(origin) as (fd,name):
                                file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
                                try:
                                    if _stat(os.fstat(file))!=row['stat']:raise Blocked('source-object-replaced')
                                    h=hashlib.sha256();count=0
                                    while True:
                                        data=os.read(file,CHUNK)
                                        if not data:break
                                        h.update(data);count+=len(data);yield data
                                    if h.hexdigest()!=row['digest'] or count!=row['size'] or _stat(os.fstat(file))!=row['stat']:raise Blocked('source-object-copy-drift')
                                finally:os.close(file)
                        self._file(dest,chunks())
                        if self.t._hash(dest)!=row['digest']:raise Blocked('candidate-object-copy-readback')
                for number in range(256):
                    fanout=target+b'/objects/'+('%02x'%number).encode()
                    if self._info(fanout) is None:self._mkdir(fanout)
                git=NativeGit(self.root+b'/'+base+b'/root',policy=policy)
                if git.exact_commit(parent_oid)!=source.exact_commit(parent_oid):raise Blocked('candidate-parent-copy-mismatch')
                for path,raw in ((MANIFEST_PATH,metadata),(b'.gitignore',ignore)):
                    blob=git.write_blob_stream(raw)
                    entries.append(IndexEntry(path,b'100644',blob.oid,0))
                    rows.append(dict(before=None,after=dict(path=encode_path(path),mode='100644',oid=blob.oid),action='generate',metadata=None))
                from native_metadata import decode
                validate(decode(metadata,width=git._config(),policy=policy),entries,width=git._config(),policy=policy)
                with git.isolated_index(None,operation_id=str(uuid.UUID(hex=operation)),owner_id=str(uuid.uuid4())) as index:
                    git.update_index(index,entries)
                    if sorted(git.index_entries(index),key=lambda e:e.path)!=sorted(entries,key=lambda e:e.path):raise Blocked('candidate-index-mismatch')
                    output_tree=git.write_tree(index,allowed_paths=[e.path for e in entries])
                raw=b'tree '+output_tree.encode()+b'\nparent '+parent_oid.encode()+b'\nauthor '+identity_line+b'\ncommitter '+identity_line+b'\n\n'+message
                commit=_oid(raw,state['recovery']['format'],b'commit')
                result=git._run(('hash-object','-t','commit','--stdin','-w'),input=raw).strip().decode()
                if result!=commit or git.exact_commit(commit)!=(raw,output_tree,(parent_oid,)):raise Blocked('candidate-exact-commit-mismatch')
                self._proof(expected_preparation,transition_operation,parent_ref,parent_oid)
                artifacts=self._manifest(base)
                # NativeGit fsyncs its resources; additionally persist every new
                # object/directory before the final pointer can name it.
                for r in reversed(artifacts):
                    path=base+b'/'+decode_path(r['path'])
                    with self.t._parent(path) as (fd,name):
                        file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
                        try:self._effect('candidate-artifact-fsync',path,lambda:os.fsync(file))
                        finally:os.close(file)
                value=dict(schema=1,operation=operation,preparation=expected_preparation,transition=state['digest'],runtime=state['runtime'],transition_operation=transition_operation,previous=expected_candidate,
                    parent_ref=base64.b64encode(parent_ref).decode(),parent_oid=parent_oid,parent_tree=tree,format=state['recovery']['format'],
                    tree=output_tree,commit=commit,raw=base64.b64encode(raw).decode(),rows=rows,metadata=base64.b64encode(metadata).decode(),
                    repository=[encode_path(p) for p in policy.repository_files],artifacts=artifacts,generation=generation,store=identity(self._directory(STORE)),stale=False,validation='unchecked',candidate='',digest='')
                self._publish(value,expected_candidate,recipe)
                return self.read()

    def revalidate(self):
        value=self.read()
        if value is None:return 'absent'
        if value['stale']:return 'stale'
        if value['validation']=='pending':return 'unavailable'
        # The complete durable fence precedes every source/ref observation.
        # A crash or failed final publication cannot make an observed B vanish
        # on reopen: a pending pointer is permanently ineligible until rebuild.
        control_limit=min(MAX_CONTROL,len(_json(value))+512)
        recipe=Recipe(self,uuid.uuid4().hex,(control_limit,)*4,12,uuid.uuid4().hex,value['digest'],control_limit)
        self._recipes[id(recipe)]=recipe
        try:
            with self.p.disk.migration_git_candidate_guard(recipe):
                with self._lease():
                    pending=copy.deepcopy(value);pending['validation']='pending'
                    self._publish(pending,value['digest'],recipe)
                    fenced=self.read()
                    stale=False
                    try:
                        state=self.t.read()
                        if state is None:raise Blocked('missing-transition-evidence')
                        self._proof(value['preparation'],state['operation'],_raw(value['parent_ref']),value['parent_oid'])
                        if state['digest']!=value['transition'] or state['runtime']!=value['runtime']:
                            raise Blocked('candidate-transition-drift')
                    except (Blocked,OSError,RuntimeError):stale=True
                    final=copy.deepcopy(fenced)
                    final['stale']=stale;final['validation']='stale' if stale else 'current'
                    final_recipe=Recipe(self,recipe.operation,recipe.sizes,recipe.entries,uuid.uuid4().hex,fenced['digest'],control_limit)
                    self._recipes[id(final_recipe)]=final_recipe
                    self._publish(final,fenced['digest'],final_recipe)
                    return 'stale' if stale else 'current'
        except (Blocked,DiskBlocked,OSError,RuntimeError):return 'unavailable'
