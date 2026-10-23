"""Dormant migration preparation. Observations are never operation authority.

Constructors and read() do no source work. Only explicit observe/prepare and
revalidate inspect sources. No content payload or browser authority is exported.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from native_git import parse_status
from native_disk import NativeDisk, Control, Evidence, Blocked as DiskBlocked
from native_inventory import (Blocked, Policy, path_bytes, beneath, linux_mounts,
                              _open_absolute_directory, _directory_at, Mount, _validate_mount)

SCHEMA = 2
MAX_CONTROL = 32 * 1024 * 1024
MAX_ROWS = 100000
CHUNK = 65536
_HEX = re.compile(r'[0-9a-f]{64}')


class Conflict(Blocked):
    """Expected candidate no longer matches; no replacement was performed."""


class PersistenceUncertain(OSError):
    """A complete replacement exists but its directory durability is uncertain."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def encode_path(path):
    return base64.b64encode(path_bytes(path)).decode('ascii')


def decode_path(value):
    try:
        if type(value) is not str:
            raise ValueError()
        raw = base64.b64decode(value, validate=True)
        if encode_path(raw) != value:
            raise ValueError()
        return raw
    except (ValueError, TypeError):
        raise Blocked('invalid-byte-path') from None


def _bytes(value):
    if type(value) is not str: raise Blocked('invalid-byte-codec')
    try: raw=base64.b64decode(value,validate=True)
    except ValueError: raise Blocked('invalid-byte-codec') from None
    if base64.b64encode(raw).decode('ascii')!=value: raise Blocked('noncanonical-byte-codec')
    return raw


def _ref(value):
    if (not value.startswith(b'refs/') or any(c<=32 or c==127 or c in b'~^:?*[\\' for c in value)
            or b'..' in value or b'@{' in value or value.endswith((b'.',b'/'))): return False
    return all(part and not part.startswith(b'.') and not part.endswith(b'.lock') for part in value.split(b'/'))


def _head(value,width):
    if value.startswith(b'ref: ') and value.endswith(b'\n'):
        return _ref(value[5:-1])
    return bool(re.fullmatch(rb'[0-9a-f]{%d}\n'%width,value)) and value.strip()!=b'0'*width


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Blocked('duplicate-candidate-key')
        result[key] = value
    return result


def _stat(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _kind(info):
    return ('directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode)
            else 'symlink' if stat.S_ISLNK(info.st_mode) else 'special')


def _descriptor(info):
    return dict(kind=_kind(info), uid=info.st_uid, gid=info.st_gid,
                mode=stat.S_IMODE(info.st_mode), size=info.st_size,
                identity=_stat(info), digest=None)


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise Blocked('invalid-candidate-shape')


def _integer(value):
    return type(value) is int and 0 <= value < 2**64


def _validate_descriptor(value):
    if value is None:
        return
    _keys(value, 'kind uid gid mode size identity digest')
    if (value['kind'] not in ('directory', 'file', 'symlink', 'special')
            or not all(_integer(value[k]) for k in ('uid', 'gid', 'mode', 'size'))
            or value['mode'] > 0o7777 or type(value['identity']) is not list
            or len(value['identity']) != 9 or not all(_integer(i) for i in value['identity'])
            or (value['digest'] is not None and (type(value['digest']) is not str or not _HEX.fullmatch(value['digest'])))):
        raise Blocked('invalid-candidate-descriptor')
    identity=value['identity']
    kind=('directory' if stat.S_ISDIR(identity[2]) else 'file' if stat.S_ISREG(identity[2]) else 'symlink' if stat.S_ISLNK(identity[2]) else 'special')
    if (kind!=value['kind'] or stat.S_IMODE(identity[2])!=value['mode']
            or identity[4]!=value['uid'] or identity[5]!=value['gid'] or identity[6]!=value['size']
            or value['digest'] is not None and kind!='file'):
        raise Blocked('inconsistent-candidate-descriptor')


def _validate_tree(rows):
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise Blocked('invalid-candidate-rows')
    seen = set()
    for row in rows:
        _keys(row, 'path object reason')
        path = decode_path(row['path'])
        if path in seen or (row['reason'] is not None and type(row['reason']) is not str):
            raise Blocked('invalid-candidate-row')
        seen.add(path)
        _validate_descriptor(row['object'])
        if row['object'] is None:
            raise Blocked('missing-candidate-object')


def _validate_observation(value):
    _keys(value, 'binding source native git private operations rows blockers')
    _keys(value['binding'], 'root root_identity data_identity legacy_identity anchors legacy mappings policy mounts app manifest')
    b = value['binding']
    if (type(b['root']) is not str or type(b['app']) is not str or not b['app']
            or type(b['manifest']) is not str or not _HEX.fullmatch(b['manifest'])
            or type(b['policy']) is not str or not _HEX.fullmatch(b['policy'])):
        raise Blocked('invalid-candidate-binding')
    try:
        absolute=base64.b64decode(b['root'],validate=True)
        if base64.b64encode(absolute).decode()!=b['root'] or not absolute.startswith(b'/') or os.path.normpath(absolute)!=absolute or b'\0' in absolute:
            raise ValueError()
    except ValueError: raise Blocked('invalid-candidate-root') from None
    _validate_tree(b['anchors'])
    decode_path(b['legacy'])
    for key in ('root_identity', 'data_identity','legacy_identity'):
        if (type(b[key]) is not list or len(b[key]) != 9 or not all(_integer(i) for i in b[key])
                or not stat.S_ISDIR(b[key][2]) or b[key][1]==0 or b[key][4]>=2**32 or b[key][5]>=2**32
                or key in ('root_identity','data_identity') and b[key][6:]!=[0,0,0]):
            raise Blocked('invalid-candidate-identity')
    if type(b['mounts']) is not list or type(b['mappings']) is not list:
        raise Blocked('invalid-candidate-binding')
    mapping_pairs=[]
    for pair in b['mappings']:
        if type(pair) is not list or len(pair) != 2:
            raise Blocked('invalid-candidate-mapping')
        decoded=tuple(decode_path(path) for path in pair)
        for prior in mapping_pairs:
            if any(beneath(decoded[i],prior[i]) or beneath(prior[i],decoded[i]) for i in (0,1)):
                raise Blocked('overlapping-persisted-mapping')
        mapping_pairs.append(decoded)
    if mapping_pairs!=sorted(mapping_pairs): raise Blocked('noncanonical-mapping-order')
    mount_ids=set();decoded_mounts=[]
    for mount in b['mounts']:
        _keys(mount, 'id parent device root point options filesystem source super_options')
        if not _integer(mount['id']) or not _integer(mount['parent']) or mount['id'] in mount_ids:
            raise Blocked('invalid-candidate-mount')
        mount_ids.add(mount['id'])
        for key in set(mount) - {'id', 'parent'}:
            if type(mount[key]) is not str:
                raise Blocked('invalid-candidate-mount')
            try:
                _bytes(mount[key])
            except ValueError:
                raise Blocked('invalid-candidate-mount') from None
        decoded_mount={key:_bytes(item) if key not in ('id','parent') else item for key,item in mount.items()}
        if any(not decoded_mount[key] or b'\0' in decoded_mount[key] for key in ('device','root','point','options','filesystem','source','super_options')):
            raise Blocked('invalid-empty-mount-field')
        _validate_mount(Mount(**decoded_mount))
        decoded_mounts.append(Mount(**decoded_mount))
    # Match Binding.check's deepest-route/device invariant using the saved
    # descriptors, without consulting or changing the current filesystem.
    anchors=[(b'',b['root_identity']),(b'data',b['data_identity']),
             (decode_path(b['legacy']),b['legacy_identity'])]
    anchors.extend((decode_path(row['path']),row['object']['identity']) for row in b['anchors'])
    for path,identity in anchors:
        anchor=(absolute.rstrip(b'/')+(b'/'+path if path else b'')) or b'/'
        matches=[m for m in decoded_mounts if anchor==m.point or anchor.startswith(m.point.rstrip(b'/')+b'/')]
        if not matches:raise Blocked('missing-persisted-mount-route')
        depth=max(len(m.point) for m in matches)
        deepest=[m for m in matches if len(m.point)==depth]
        if len(deepest)!=1:raise Blocked('ambiguous-persisted-mount-route')
        if deepest[0].device!=f'{os.major(identity[0])}:{os.minor(identity[0])}'.encode():
            raise Blocked('persisted-descriptor-mount-device-mismatch')
    _validate_tree(value['source']); _validate_tree(value['native'])
    g = value['git']
    _keys(g, 'format head refs index status status_paths ignored untracked controls issues')
    if g['format'] not in (None, 'sha1', 'sha256') or type(g['head']) is not str:
        raise Blocked('invalid-candidate-git')
    head=_bytes(g['head'])
    width=64 if g['format']=='sha256' else 40
    if (head and not _head(head,width)) or (g['format'] is not None and not head):
        raise Blocked('invalid-candidate-head')
    if g['format'] is None and head: raise Blocked('invalid-unavailable-head')
    if type(g['status_paths']) is not list or len(g['status_paths'])>MAX_ROWS or not all(type(path) is str for path in g['status_paths']) or len(set(g['status_paths']))!=len(g['status_paths']):
        raise Blocked('invalid-status-paths')
    status_paths={decode_path(path) for path in g['status_paths']}
    for key in ('refs', 'status', 'ignored', 'untracked'):
        if type(g[key]) is not str:
            raise Blocked('invalid-candidate-git')
        try:
            _bytes(g[key])
        except ValueError:
            raise Blocked('invalid-candidate-git') from None
    for key in ('ignored','untracked'):
        raw=_bytes(g[key])
        if raw and not raw.endswith(b'\0'): raise Blocked('truncated-git-path-list')
        paths=raw[:-1].split(b'\0') if raw else []
        if len(set(paths))!=len(paths): raise Blocked('duplicate-git-path')
        for path in paths:
            path_bytes(path)
            if path not in status_paths: raise Blocked('git-discovery-outside-safe-scope')
    for record in parse_status(_bytes(g['status']),width=width):
        if record.path not in status_paths or record.original_path is not None:
            raise Blocked('status-path-outside-safe-scope')
    refs=_bytes(g['refs'])
    if refs and not refs.endswith(b'\n'): raise Blocked('malformed-refs')
    ref_names=set()
    for record in refs.splitlines():
        fields=record.split(b'\0')
        if len(fields)!=3 or not _ref(fields[0]) or not re.fullmatch(rb'[0-9a-f]{%d}'%width,fields[1]) or fields[1]==b'0'*width or fields[0] in ref_names or fields[2] and (not _ref(fields[2]) or fields[2]==fields[0]):
            raise Blocked('malformed-refs')
        ref_names.add(fields[0])
    if type(g['index']) is not list:
        raise Blocked('invalid-candidate-index')
    index_seen=set()
    for row in g['index']:
        _keys(row, 'path mode oid stage')
        path=decode_path(row['path'])
        if (row['mode'] not in ('100644','100755','120000','160000','040000')
                or type(row['stage']) is not int or row['stage'] not in range(4)
                or type(row['oid']) is not str
                or not re.fullmatch('[0-9a-f]{%d}' % (64 if g['format'] == 'sha256' else 40), row['oid'])
                or row['oid']=='0'*width or (path,row['stage']) in index_seen or g['format'] is None):
            raise Blocked('invalid-candidate-index')
        index_seen.add((path,row['stage']))
    _validate_tree(g['controls'])
    for issues in (g['issues'], value['blockers']):
        if type(issues) is not list or not all(type(i) is str for i in issues):
            raise Blocked('invalid-candidate-issues')
    operations=value['operations']
    _keys(operations,'legacy native services recovery')
    for operation in operations.values():
        _keys(operation,'object summary')
        _validate_descriptor(operation['object'])
        if operation['object'] is not None and operation['object']['digest'] is not None:
            raise Blocked('operation-payload-digest-forbidden')
        _keys(operation['summary'],'active_present pending_present workers records')
        if (type(operation['summary']['active_present']) is not bool or type(operation['summary']['pending_present']) is not bool
                or not _integer(operation['summary']['workers']) or not _integer(operation['summary']['records'])):
            raise Blocked('invalid-operation-summary')
    p = value['private']
    _keys(p, 'auth_directory auth_tokens configuration')
    for key in ('auth_directory', 'auth_tokens'):
        _validate_descriptor(p[key])
        if p[key] is not None and p[key]['digest'] is not None:
            raise Blocked('private-content-digest-forbidden')
    _keys(p['configuration'], 'fields commitment')
    if (type(p['configuration']['fields']) is not list
            or any(k not in ('direct_https','direct_origin','direct_certfile','direct_keyfile') for k in p['configuration']['fields'])
            or type(p['configuration']['commitment']) is not str or not _HEX.fullmatch(p['configuration']['commitment'])):
        raise Blocked('invalid-private-config')
    if type(value['rows']) is not list or len(value['rows']) > MAX_ROWS * 2:
        raise Blocked('invalid-diff-rows')
    row_sources=set();row_destinations=set()
    for row in value['rows']:
        _keys(row, 'source destination disposition before after changes index')
        for key in ('source', 'destination'):
            if row[key] is not None:
                decode_path(row[key])
        if row['source'] is None and row['destination'] is None: raise Blocked('unaddressed-diff-row')
        for key,seen in (('source',row_sources),('destination',row_destinations)):
            if row[key] is not None:
                if row[key] in seen: raise Blocked('duplicate-diff-address')
                seen.add(row[key])
        if row['disposition'] not in ('mapped','unresolved','retired','excluded','native-only','required-parent','diagnostic-only'):
            raise Blocked('invalid-diff-disposition')
        for key in ('before','after'):
            _validate_descriptor(row[key])
        if (type(row['changes']) is not list or not row['changes'] or not all(type(change) is str for change in row['changes']) or len(set(row['changes']))!=len(row['changes']) or any(i not in ('move','addition','deletion','type','content','metadata','unchanged','collision','diagnostic') for i in row['changes'])
                or type(row['index']) is not list or any(type(i) is not int or not 0 <= i < len(g['index']) for i in row['index'])):
            raise Blocked('invalid-diff-changes')


def _stability(value):
    """One commitment for every observation boundary; diagnostics have no vote."""
    value = __import__('copy').deepcopy(value)
    def stable(obj):
        if obj is not None and obj['kind'] == 'directory':
            # Never mutate an aliased descriptor: serialized observations have
            # separate before/after/source/native objects, while fresh ones may
            # share them. Source directory metadata must remain strict.
            obj = {**obj, 'identity': list(obj['identity'])}
            obj['size'] = 0
            obj['identity'][3] = 0
            obj['identity'][6:] = [0, 0, 0]
        return obj
    value['native'] = [row for row in value['native'] if not (row['reason'] or '').startswith('diagnostic-only')]
    for row in value['native']: row['object'] = stable(row['object'])
    value['rows'] = [row for row in value['rows'] if row['disposition'] != 'diagnostic-only']
    for row in value['rows']:
        if row['disposition'] in ('mapped', 'required-parent'):
            row['before'] = stable(row['before'])
            if row['disposition'] == 'required-parent': row['after'] = stable(row['after'])
        else:
            # Excluded/retired/unresolved rows still retain strict source/index
            # evidence. Their live counterpart and changes derived from it are
            # review diagnostics, including an index-only excluded source.
            row['before'] = None
            row['changes'] = ['diagnostic']
    return value


def _validate_candidate(value):
    _keys(value, 'schema generation state observation input_digest digest')
    if (type(value['schema']) is not int or value['schema'] != SCHEMA
            or type(value['generation']) is not str or not re.fullmatch('[0-9a-f]{32}', value['generation'])
            or value['state'] not in ('prepared', 'stale')
            or type(value['input_digest']) is not str or not _HEX.fullmatch(value['input_digest'])
            or type(value['digest']) is not str or not _HEX.fullmatch(value['digest'])):
        raise Blocked('invalid-candidate-schema')
    _validate_observation(value['observation'])
    if (value['input_digest'] != _digest(_stability(value['observation']))
            or value['digest'] != _digest({k:v for k,v in value.items() if k != 'digest'})):
        raise Blocked('candidate-digest-mismatch')
    return value


class Preparation:
    """Explicit diagnostic API. `expected` is None only for an absent store.

    binding is native_metadata.Binding with real current mount evidence. The
    host test seam supplies synthetic evidence only when Linux mountinfo is absent.
    Caller mappings must already contain exact resolved App slugs.
    options is an explicit callable returning current direct-access options.
    manifest_digest binds the already resolved legacy manifest, never its payload.
    """
    def __init__(self, binding, *, mappings=((b'homeassistant', b'homeassistant'),),
                 app_identity, manifest_digest, options=lambda: {}, disk=None):
        self.binding = binding
        self.policy = binding.policy
        self.root = os.fsencode(binding.root)
        if self.root != os.fsencode(os.path.abspath(self.root)):
            raise Blocked('canonical-absolute-root-required')
        self.legacy = self.policy.legacy_repo
        for reserved in (b'data/direct-auth',b'data/native-operations',b'data/native-services',b'data/native',b'data/native-migration-preparation',b'data/native-migration-transition'):
            if beneath(self.legacy,reserved) or beneath(reserved,self.legacy):
                raise Blocked('legacy-private-service-route-overlap')
        self.store = b'data/native-migration-preparation'
        self.app = app_identity
        self.manifest = manifest_digest
        self.options = options
        self._store_fds={}
        self.disk = disk if disk is not None else NativeDisk(self.root)
        if not isinstance(self.disk, NativeDisk) or self.disk.root != self.root:
            raise Blocked('same-root-native-disk-required')
        if type(self.app) is not str or not self.app or type(self.manifest) is not str or not _HEX.fullmatch(self.manifest):
            raise Blocked('explicit-app-and-manifest-identity-required')
        self.mappings = tuple(sorted((path_bytes(a), path_bytes(b)) for a,b in mappings))
        for i,(source,target) in enumerate(self.mappings):
            if self.policy.reason(target) is not None:
                raise Blocked('unsafe-mapping-target')
            for other,dest in self.mappings[:i]:
                if beneath(source,other) or beneath(other,source) or beneath(target,dest) or beneath(dest,target):
                    raise Blocked('overlapping-mapping')
        for path in (self.legacy, self.store):
            if not beneath(path,b'data') or path == b'data' or beneath(path,b'data/native') and path != self.store:
                raise Blocked('unsafe-service-route')
        if beneath(self.legacy,self.store) or beneath(self.store,self.legacy):
            raise Blocked('overlapping-service-route')

    def _mounts(self):
        return linux_mounts() if os.path.exists('/proc/self/mountinfo') else None

    @contextmanager
    def _root(self):
        fd = _open_absolute_directory(self.root)
        try:
            self.binding.check(b'',os.fstat(fd),self._mounts())
            yield fd
        finally:
            os.close(fd)

    def _directory(self, root, path):
        def check(prefix):
            info = self._lstat(root,prefix)
            self.binding.check(prefix,info,self._mounts())
        return _directory_at(root,path,before_open=check)

    def _route(self,path):
        absolute=self.root.rstrip(b'/')+b'/'+path
        def matches(mounts):
            found=[m for m in mounts if absolute==m.point or absolute.startswith(m.point.rstrip(b'/')+b'/')]
            if not found: raise Blocked('unknown-mount-boundary')
            length=max(len(m.point) for m in found)
            return [m for m in found if len(m.point)==length]
        expected=matches(self.binding.mounts)
        if len(expected)!=1 or expected[0].id not in self.binding.confirmed_mounts:
            raise Blocked('unconfirmed-mount-boundary')
        current=self._mounts()
        if current is not None and matches(current)!=expected:
            raise Blocked('mount-evidence-stale')

    def _lstat(self, root, path):
        self._route(path)
        parent, _, name = path.rpartition(b'/')
        fd = _directory_at(root,parent,before_open=self._route)
        try:
            return os.stat(name,dir_fd=fd,follow_symlinks=False)
        finally:
            os.close(fd)

    def _read(self, root, path, *, control=False):
        parent,_,name=path.rpartition(b'/')
        directory=self._directory(root,parent)
        try:
            before=os.stat(name,dir_fd=directory,follow_symlinks=False)
            self.binding.check(path,before,self._mounts())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Blocked('unsafe-content-leaf')
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                if _stat(before)!=_stat(os.fstat(fd)):
                    raise Blocked('changed-content-leaf')
                digest=hashlib.sha256(); chunks=[]; total=0
                while True:
                    data=os.read(fd,CHUNK)
                    if not data: break
                    total+=len(data)
                    if control:
                        if total>MAX_CONTROL: raise Blocked('control-resource-limit')
                        chunks.append(data)
                    digest.update(data)
                if _stat(before)!=_stat(os.fstat(fd)) or _stat(before)!=_stat(os.stat(name,dir_fd=directory,follow_symlinks=False)):
                    raise Blocked('changed-content-leaf')
                return b''.join(chunks) if control else digest.hexdigest()
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def _destination(self,path):
        for source,target in self.mappings:
            if beneath(path,source): return target+path[len(source):]
        return None

    def _reason(self,path,destination):
        parts=path.split(b'/')
        if len(parts)>1 and parts[-2:]==[b'.storage_managed',b'core.config_entries.json']:
            return 'retired-core-config-entries'
        reason=self.policy.reason(path if parts[0] in self.policy.live_roots else b'homeassistant/'+path)
        if reason is not None: return reason
        if destination is not None: return self.policy.reason(destination)
        return None

    def _scan(self,root,base,*,source,participating=frozenset()):
        result=[]; pending=[(base,b'')]
        while pending:
            absolute,relative=pending.pop()
            try: fd=self._directory(root,absolute)
            except FileNotFoundError:
                if source or absolute in participating: raise
                continue
            try:
                names=sorted(os.fsencode(n) for n in os.listdir(fd))
                for name in names:
                    path=relative+b'/'+name if relative else name
                    full=absolute+b'/'+name
                    try: info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    except FileNotFoundError:
                        if source or full in participating: raise
                        continue
                    self.binding.check(full,info,self._mounts())
                    destination=self._destination(path) if source else path
                    reason=self._reason(path,destination) if source else self.policy.reason(base+b'/'+path)
                    obj=_descriptor(info)
                    if name==b'.git': reason='git-boundary'
                    if obj['kind'] in ('symlink','special'): reason=reason or 'unsupported-'+obj['kind']
                    if obj['kind']=='file' and info.st_nlink!=1: reason=reason or 'unsupported-hardlink'
                    if obj['kind']=='file' and reason is None and (source or full in participating): obj['digest']=self._read(root,full)
                    diagnostic = not source and full not in participating
                    result.append(dict(path=encode_path(path),object=obj,reason=('diagnostic-only'+(':'+reason if reason else '')) if diagnostic else reason))
                    if len(result)>MAX_ROWS: raise Blocked('inventory-resource-limit-incomplete')
                    if obj['kind']=='directory' and reason is None: pending.append((full,path))
            finally:
                os.close(fd)
        return sorted(result,key=lambda r:decode_path(r['path']))

    def _git_command(self,args,*,_config=False):
        if not _config:
            self._safe_config()
        # No shell, inherited GIT_* or inherited configuration; exact read-only commands.
        executable='/usr/bin/git'
        if not os.path.isfile(executable): raise Blocked('qualified-git-unavailable')
        env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LC_ALL':'C','TMPDIR':'/tmp',
             'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_SYSTEM':'/dev/null','GIT_CONFIG_GLOBAL':'/dev/null',
             'GIT_OPTIONAL_LOCKS':'0','GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1',
             'GIT_NO_REPLACE_OBJECTS':'1','GIT_ALLOW_PROTOCOL':'','GIT_ATTR_NOSYSTEM':'1'}
        command=[executable,'--no-pager','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',
                 '-c','core.excludesFile=/dev/null','-c','core.attributesFile=/dev/null',
                 '-c','diff.external=','-c','submodule.recurse=false','-c','maintenance.auto=false',
                 '-c','gc.auto=0','-C',os.fsdecode(self.root+b'/'+self.legacy)]+args
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
        output=[];errors=[];total=0;deadline=time.monotonic()+10
        selector=selectors.DefaultSelector()
        selector.register(process.stdout,selectors.EVENT_READ,output)
        selector.register(process.stderr,selectors.EVENT_READ,errors)
        try:
            while selector.get_map():
                if time.monotonic()>deadline: raise Blocked('git-observation-timeout')
                for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),CHUNK)
                    if not data: selector.unregister(key.fileobj);continue
                    total+=len(data)
                    if total>MAX_CONTROL: raise Blocked('git-output-resource-limit-incomplete')
                    key.data.append(data)
            process.wait(timeout=max(.01,deadline-time.monotonic()))
            if process.returncode or errors: raise Blocked('git-observation-failed')
            return b''.join(output)
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
            process.stdout.close();process.stderr.close()

    def _safe_config(self):
        # Config listing with includes disabled is itself read-only and executes
        # no filters/hooks. Repeat before every other Git command.
        with self._root() as root:
            directory=self._directory(root,self.legacy+b'/.git')
            os.close(directory)
        config=self._git_command(['config','--local','--no-includes','--null','--list'],_config=True)
        allowed={'core.repositoryformatversion','core.filemode','core.bare','core.logallrefupdates',
                 'core.ignorecase','core.precomposeunicode','user.name','user.email',
                 'extensions.objectformat'}
        for record in config.split(b'\0'):
            if not record: continue
            key,separator,value=record.partition(b'\n')
            if not separator: raise Blocked('unsupported-git-config')
            key=key.decode('ascii').lower()
            if key not in allowed and not re.fullmatch(r'(remote\.[^.]+\.(url|fetch)|branch\.[^.]+\.(remote|merge))',key):
                raise Blocked('unsupported-git-config')
            if key=='core.bare' and value!=b'false': raise Blocked('unsupported-bare-repository')

    def _git(self,root,source):
        g=dict(format=None,head='',refs='',index=[],status='',status_paths=[],ignored='',untracked='',controls=[],issues=[])
        gitpath=self.legacy+b'/.git'
        try:
            info=self._lstat(root,gitpath)
            if not stat.S_ISDIR(info.st_mode): raise Blocked('unsupported-git-routing')
            # Read complete metadata directory descriptors and safe control digests.
            # Object payloads, config URLs and auth material never enter review.
            controls=[];pending=[(gitpath,b'')]
            while pending:
                absolute,relative=pending.pop();fd=self._directory(root,absolute)
                try:
                    for name in sorted(os.fsencode(n) for n in os.listdir(fd)):
                        path=relative+b'/'+name if relative else name
                        s=os.stat(name,dir_fd=fd,follow_symlinks=False);self.binding.check(absolute+b'/'+name,s,self._mounts())
                        obj=_descriptor(s);reason=None
                        if obj['kind'] in ('symlink','special') or (obj['kind']=='file' and s.st_nlink!=1):
                            reason='unsupported-git-control';g['issues'].append(reason)
                        if name in (b'commondir',b'gitdir',b'alternates') or name.startswith(b'sharedindex.'):
                            reason='unsupported-git-routing';g['issues'].append(reason)
                        if name.endswith(b'.lock') or name in (b'MERGE_HEAD',b'CHERRY_PICK_HEAD',b'REVERT_HEAD',b'REBASE_HEAD',b'rebase-apply',b'rebase-merge',b'sequencer',b'BISECT_START'):
                            reason='pending-git-operation';g['issues'].append('pending:'+encode_path(path))
                        if obj['kind']=='file' and reason is None and path not in (b'config',) and not beneath(path,b'objects'):
                            obj['digest']=self._read(root,absolute+b'/'+name)
                        controls.append(dict(path=encode_path(path),object=obj,reason=reason))
                        if len(controls)>MAX_ROWS: raise Blocked('git-control-resource-limit-incomplete')
                        if obj['kind']=='directory' and reason is None and name not in (b'hooks',):
                            pending.append((absolute+b'/'+name,path))
                finally: os.close(fd)
            g['controls']=sorted(controls,key=lambda r:decode_path(r['path']))
            for route in (b'objects/info/alternates',b'objects/info/http-alternates',b'commondir',b'gitdir'):
                try: self._lstat(root,gitpath+b'/'+route);g['issues'].append('unsupported-git-routing')
                except FileNotFoundError: pass
            if any(i.startswith('unsupported-') for i in g['issues']): return g
            self._safe_config()
            fmt=self._git_command(['rev-parse','--show-object-format']).strip().decode('ascii')
            if fmt not in ('sha1','sha256'): raise Blocked('unsupported-object-format')
            g['format']=fmt;width=64 if fmt=='sha256' else 40
            head=self._read(root,gitpath+b'/HEAD',control=True)
            if not _head(head,width):
                raise Blocked('malformed-head')
            g['head']=base64.b64encode(head).decode('ascii')
            g['refs']=base64.b64encode(self._git_command(['for-each-ref','--format=%(refname)%00%(objectname)%00%(symref)'])).decode('ascii')
            index=self._git_command(['ls-files','--stage','--sparse','-z'])
            seen=set()
            for record in index.split(b'\0'):
                if not record: continue
                prefix,path=record.split(b'\t',1);mode,oid,stage=prefix.split(b' ')
                if mode==b'040000': path=path.rstrip(b'/');g['issues'].append('unsupported-sparse-index')
                path=path_bytes(path)
                if (mode not in (b'100644',b'100755',b'120000',b'160000',b'040000') or stage not in (b'0',b'1',b'2',b'3')
                        or not re.fullmatch(rb'[0-9a-f]{%d}'%width,oid) or (path,stage) in seen):
                    raise Blocked('invalid-index')
                seen.add((path,stage))
                g['index'].append(dict(path=encode_path(path),mode=mode.decode(),oid=oid.decode(),stage=int(stage)))
                if stage!=b'0' or mode not in (b'100644',b'100755'): g['issues'].append('unsupported-index-entry:'+encode_path(path))
            observed={decode_path(row['path']):row for row in source}
            safe_paths={path for path,row in observed.items() if row['reason'] is None and row['object']['kind']=='file'}
            for row in g['index']:
                path=decode_path(row['path'])
                if path in observed or self._reason(path,self._destination(path)) is not None: continue
                try: self._lstat(root,self.legacy+b'/'+path)
                except FileNotFoundError: safe_paths.add(path)
                except (OSError,Blocked): pass
            g['status_paths']=[encode_path(path) for path in sorted(safe_paths)]
            if safe_paths:
                args=['status','--porcelain=v2','-z','--untracked-files=no','--ignore-submodules=all','--no-renames','--']
                args += [b':(top,literal)'+path for path in sorted(safe_paths)]
                g['status']=base64.b64encode(self._git_command(args)).decode('ascii')
            for key,args in [('ignored',['ls-files','--others','--ignored','--exclude-standard','-z']),
                             ('untracked',['ls-files','--others','--exclude-standard','-z'])]:
                if safe_paths:
                    args += ['--']+[b':(top,literal)'+path for path in sorted(safe_paths)]
                    g[key]=base64.b64encode(self._git_command(args)).decode('ascii')
        except (OSError,Blocked,ValueError,UnicodeError):
            g['issues'].append('git-inventory-unavailable-or-unsafe')
        return g

    def _private(self,root):
        result=dict(auth_directory=None,auth_tokens=None,configuration=None)
        try:
            info=self._lstat(root,b'data/direct-auth');self.binding.check(b'data/direct-auth',info,self._mounts())
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
                raise Blocked('unsafe-private-auth-directory')
            result['auth_directory']=_descriptor(info)
            try:
                token=self._lstat(root,b'data/direct-auth/tokens.json')
                self.binding.check(b'data/direct-auth/tokens.json',token,self._mounts())
                if not stat.S_ISREG(token.st_mode) or token.st_nlink!=1 or token.st_uid!=os.geteuid() or stat.S_IMODE(token.st_mode)!=0o600:
                    raise Blocked('unsafe-private-auth-file')
                result['auth_tokens']=_descriptor(token)
            except FileNotFoundError: pass
        except FileNotFoundError: pass
        options=self.options()
        if type(options) is not dict: raise Blocked('invalid-direct-configuration')
        safe={}
        for key in ('direct_https','direct_origin','direct_certfile','direct_keyfile'):
            if key not in options: continue
            value=options[key]
            if key=='direct_https':
                if type(value) is not bool: raise Blocked('invalid-direct-configuration')
            else:
                if type(value) is not str or any(ord(c)<32 for c in value): raise Blocked('invalid-direct-configuration')
                if key=='direct_origin' and value:
                    p=urlsplit(value)
                    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
                        raise Blocked('invalid-direct-configuration')
                if key in ('direct_certfile','direct_keyfile') and value and (b'/' in os.fsencode(value) or value in ('.','..')):
                    raise Blocked('invalid-direct-configuration')
            safe[key]=value
        result['configuration']=dict(fields=sorted(safe),commitment=_digest(safe))
        return result

    def _operations(self,root):
        result={}
        for name,path in (('legacy',b'data/state.json'),('native',b'data/native-operations/state.json'),
                          ('services',b'data/native-services/state.json'),('recovery',b'data/native/recovery/active.json')):
            observation=dict(object=None,summary=dict(active_present=False,pending_present=False,workers=0,records=0))
            try:
                info=self._lstat(root,path);self.binding.check(path,info,self._mounts())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
                    raise Blocked('unsafe-operation-state')
                if name=='recovery':
                    observation['object']=_descriptor(info)
                    observation['summary'].update(active_present=True,pending_present=True)
                    result[name]=observation
                    continue
                raw=self._read(root,path,control=True)
                try: state=json.loads(raw,object_pairs_hook=_unique)
                except (ValueError,RecursionError): raise Blocked('invalid-operation-state') from None
                if type(state) is not dict: raise Blocked('invalid-operation-state')
                observation['object']=_descriptor(info)
                if name=='legacy':
                    commands=state.get('command_records') or {}
                    if type(commands) is not dict or any(type(record) is not dict for record in commands.values()):
                        raise Blocked('invalid-legacy-command-records')
                    debt=sum(record.get('status') in ('accepted','running','failed_unknown') for record in commands.values())
                    observation['summary']['records']=len(commands)
                    observation['summary']['workers']=debt
                    observation['summary']['active_present']=bool(state.get('active_operation')) or bool(debt)
                    observation['summary']['pending_present']=any(bool(state.get(k)) for k in
                        ('save_push_retry_pending','deleted_devices_pending_confirmation','docker_build_cache_prune_fence')) or bool(debt)
                elif name=='services':
                    observation['summary']['active_present']=bool(state.get('operation')) and (state.get('phase')!='restored' or bool(state.get('pending')))
                    observation['summary']['pending_present']=bool(state.get('pending')) or state.get('phase') not in (None,'restored')
                else:
                    observation['summary']['active_present']=bool(state.get('active'))
                    observation['summary']['pending_present']=any(bool(state.get(k)) for k in ('scope_pending','recovery'))
                    for key in ('workers','records'):
                        value=state.get(key,{})
                        if type(value) not in (dict,list): raise Blocked('invalid-operation-state')
                        observation['summary'][key]=len(value)
            except FileNotFoundError: pass
            result[name]=observation
        return result

    def _observe_once(self):
        with self._root() as root:
            data=self._directory(root,b'data')
            try: data_info=_stat(os.fstat(data))
            finally: os.close(data)
            legacyfd=self._directory(root,self.legacy)
            try: legacy_info=os.fstat(legacyfd)
            finally: os.close(legacyfd)
            blockers=[];anchors=[]
            # These reserved targets must be absent; future steps own recognition.
            for path in (b'data/native/repository.git',b'data/native/root-ignore-policy',b'data/native/repository-files',b'.git'):
                try:
                    info=self._lstat(root,path);self.binding.check(path,info,self._mounts())
                    anchors.append(dict(path=encode_path(path),object=_descriptor(info),reason='occupied-native-anchor'))
                    blockers.append('occupied-native-anchor:'+encode_path(path))
                except FileNotFoundError: pass
            alias=self.root+b'/config'
            if os.path.lexists(alias):
                canonical=self.root+b'/addon_configs/'+self.policy.self_slug
                try:
                    a=self._lstat(root,b'config');b=self._lstat(root,b'addon_configs/'+self.policy.self_slug)
                    mounts=self._mounts()
                    self.binding.check(b'config',a,mounts);self.binding.check(b'addon_configs/'+self.policy.self_slug,b,mounts)
                    if _stat(a)[:3]!=_stat(b)[:3] or mounts is None: raise Blocked('unverified-config-alias')
                    def mount_for(path):
                        absolute=self.root.rstrip(b'/')+b'/'+path
                        found=[m for m in mounts if absolute==m.point or absolute.startswith(m.point.rstrip(b'/')+b'/')]
                        return max(found,key=lambda m:len(m.point))
                    left=mount_for(b'config');right=mount_for(b'addon_configs/'+self.policy.self_slug)
                    def underlying(m,path):
                        absolute=self.root.rstrip(b'/')+b'/'+path
                        return m.root.rstrip(b'/')+b'/'+absolute[len(m.point.rstrip(b'/')):].lstrip(b'/')
                    if (left.id==right.id or left.source!=right.source or left.device!=right.device
                            or underlying(left,b'config')!=underlying(right,b'addon_configs/'+self.policy.self_slug)):
                        raise Blocked('unverified-config-alias')
                except (OSError,Blocked): blockers.append('unverified-config-alias')
            source=self._scan(root,self.legacy,source=True)
            git=self._git(root,source);blockers.extend(git['issues'])
            participating=set()
            source_paths={decode_path(row['path']) for row in source if row['reason'] is None}
            source_paths.update(decode_path(row['path']) for row in git['index'])
            for path in source_paths:
                destination=self._destination(path)
                if destination is not None and self._reason(path,destination) is None:
                    parts=destination.split(b'/')
                    participating.update(b'/'.join(parts[:i]) for i in range(1,len(parts)+1))
            for _,target in self.mappings:
                parts=target.split(b'/')
                participating.update(b'/'.join(parts[:i]) for i in range(1,len(parts)+1))
            native=[]
            # Scan each exact mapping target, preserving its own root and parents.
            all_native={}
            for _,target in self.mappings:
                prefixes=[b'/'.join(target.split(b'/')[:i]) for i in range(1,len(target.split(b'/'))+1)]
                for prefix in prefixes:
                    try:
                        info=self._lstat(root,prefix);self.binding.check(prefix,info,self._mounts())
                        obj=_descriptor(info)
                        reason=self.policy.reason(prefix)
                        if obj['kind'] in ('symlink','special'): reason=reason or 'unsupported-'+obj['kind']
                        if obj['kind']=='file' and info.st_nlink!=1: reason=reason or 'unsupported-hardlink'
                        if obj['kind']=='file' and reason is None: obj['digest']=self._read(root,prefix)
                        all_native[prefix]=dict(path=encode_path(prefix),object=obj,reason=reason)
                    except FileNotFoundError: continue
                try:
                    info=self._lstat(root,target)
                    if stat.S_ISDIR(info.st_mode):
                        for row in self._scan(root,target,source=False,participating=participating):
                            path=target+b'/'+decode_path(row['path']);row['path']=encode_path(path)
                            # Relative native scans classify using the full destination.
                            reason=row['reason'] or self.policy.reason(path);row['reason']=reason
                            if path in participating and row['object']['kind']=='file' and reason is None and row['object']['digest'] is None and row['object']['identity'][3]==1:
                                row['object']['digest']=self._read(root,path)
                            if reason is not None: row['object']['digest']=None
                            all_native[path]=row
                except FileNotFoundError: pass
            native=[all_native[p] for p in sorted(all_native)]
            private=self._private(root)
            operations=self._operations(root)
            if any(o['summary']['active_present'] or o['summary']['pending_present'] for o in operations.values()):
                blockers.append('pending-ha-ops-operation')
            if operations['recovery']['object'] is not None:
                blockers.append('unvalidated-recovery-pointer')
            for row in source+native:
                reason=row['reason'];path=decode_path(row['path'])
                if reason and (reason.startswith('unsupported-') or reason=='git-boundary' and path!=b'.git'):
                    blockers.append(reason+':'+row['path'])
            rows=self._diff(source,native,git,blockers)
            mounts=[]
            for mount in self.binding.mounts:
                item=dataclasses.asdict(mount)
                mounts.append({k:base64.b64encode(v).decode('ascii') if isinstance(v,bytes) else v for k,v in item.items()})
            # Membership/mtime of /data includes our own private store. Bind its
            # descriptor identity/owner/mode, excluding mutable service membership.
            data_info[3]=0;data_info[6:]=[0,0,0]
            root_info=_stat(os.fstat(root));root_info[6:]=[0,0,0]
            result=dict(binding=dict(root=base64.b64encode(self.root).decode(),root_identity=root_info,
                        data_identity=data_info,legacy_identity=_stat(legacy_info),anchors=anchors,legacy=encode_path(self.legacy),
                        mappings=[[encode_path(a),encode_path(b)] for a,b in self.mappings],policy=self.policy.digest,
                        mounts=mounts,app=self.app,manifest=self.manifest),
                        source=source,native=native,git=git,private=private,operations=operations,rows=rows,blockers=sorted(set(blockers)))
            _validate_observation(result)
            return result

    def _diff(self,source,native,git,blockers):
        current={decode_path(r['path']):r['object'] for r in native};native_reasons={decode_path(r['path']):r['reason'] for r in native};rows=[];used=set();destinations={}
        indexed={}
        for i,row in enumerate(git['index']): indexed.setdefault(decode_path(row['path']),[]).append(i)
        sources={decode_path(r['path']):r for r in source}
        for path in indexed:
            sources.setdefault(path,dict(path=encode_path(path),object=None,reason=self._reason(path,self._destination(path))))
        for path,row in sorted(sources.items()):
            destination=self._destination(path);reason=row['reason'];after=row['object'];before=current.get(destination)
            disposition='mapped'
            if reason=='retired-core-config-entries': disposition='retired';destination=None
            elif reason is not None: disposition='excluded';destination=None
            elif destination is None: disposition='unresolved'
            if destination is not None:
                used.add(destination)
                if destination in destinations:
                    blockers.append('destination-collision:'+encode_path(destination))
                destinations[destination]=path
            changes=[]
            if destination is not None and destination!=path: changes.append('move')
            if before is None and after is not None: changes.append('addition')
            elif before is not None and after is None: changes.append('deletion')
            elif before is not None and after is not None:
                if before['kind']!=after['kind']: changes.append('type')
                if before['digest']!=after['digest']: changes.append('content')
                if any(before[k]!=after[k] for k in ('uid','gid','mode')): changes.append('metadata')
            if not changes: changes=['unchanged']
            rows.append(dict(source=encode_path(path),destination=encode_path(destination) if destination is not None else None,
                             disposition=disposition,before=before,after=after,changes=changes,index=indexed.get(path,[])))
        # Existing required parents remain in the proposed layout, with exact
        # retained metadata. Absent parents have no invented ownership policy.
        parents=set()
        for destination in destinations:
            parts=destination.split(b'/')
            parents.update(b'/'.join(parts[:i]) for i in range(1,len(parts)))
        for path,obj in sorted(current.items()):
            if path not in used:
                parent=path in parents
                rows.append(dict(source=None,destination=encode_path(path),
                    disposition='required-parent' if parent else 'diagnostic-only',
                    before=obj,after=obj if parent else None,changes=['unchanged'] if parent else ['diagnostic'],index=[]))
        # Required destination parents are visible even when absent on both sides.
        for path in sorted(parents-set(destinations)-set(current)):
            blockers.append('unresolved-required-parent-metadata:'+encode_path(path))
            rows.append(dict(source=None,destination=encode_path(path),disposition='required-parent',before=None,after=None,changes=['addition'],index=[]))
        return rows

    def observe(self):
        first=self._observe_once();second=self._observe_once()
        if _stability(first)!=_stability(second): raise Blocked('inputs-changed-during-observation')
        return first

    @contextmanager
    def _disk(self,create=False,admission=None):
        with self._root() as root:
            data=self._directory(root,b'data')
            try:
                if create:
                    if admission is None: raise Blocked('candidate-admission-required')
                    admission.revalidate()
                    try:
                        os.mkdir(b'native-migration-preparation',0o700,dir_fd=data)
                        created=self._directory(root,self.store)
                        try: admission.record_directory_creation(self.store,created)
                        finally: os.close(created)
                    except FileExistsError: pass
                    os.fsync(data)
                try: fd=self._directory(root,self.store)
                except FileNotFoundError:
                    if create: raise
                    yield None
                    return
                try:
                    info=os.fstat(fd)
                    if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700: raise Blocked('unsafe-preparation-directory')
                    fcntl.flock(fd,fcntl.LOCK_EX)
                    for name in os.listdir(fd):
                        if name!='candidate.json' and not re.fullmatch(r'\.pending-[0-9a-f]{32}',name):
                            raise Blocked('unrecognized-preparation-entry')
                    if (_stat(info)[:3]+_stat(info)[4:6])!=(_stat(self._lstat(root,self.store))[:3]+_stat(self._lstat(root,self.store))[4:6]): raise Blocked('changed-preparation-directory')
                    self._store_fds[fd]=(_stat(info)[:3]+_stat(info)[4:6])
                    yield fd
                    if (_stat(info)[:3]+_stat(info)[4:6])!=(_stat(self._lstat(root,self.store))[:3]+_stat(self._lstat(root,self.store))[4:6]): raise Blocked('changed-preparation-directory')
                finally:
                    self._store_fds.pop(fd,None)
                    os.close(fd)
            finally: os.close(data)

    def _verified_store(self,fd):
        if type(fd) is not int or fd<0 or fd not in self._store_fds:
            raise Blocked('verified-preparation-descriptor-required')
        info=os.fstat(fd);identity=_stat(info)[:3]+_stat(info)[4:6]
        if not stat.S_ISDIR(info.st_mode) or identity!=self._store_fds[fd]:
            raise Blocked('changed-preparation-descriptor')
        with self._root() as root:
            current=self._lstat(root,self.store)
            if identity!=_stat(current)[:3]+_stat(current)[4:6]:
                raise Blocked('changed-preparation-anchor')

    def _load(self,fd):
        self._verified_store(fd)
        try: info=os.stat(b'candidate.json',dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError: return None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>MAX_CONTROL): raise Blocked('unsafe-candidate-file')
        file=os.open(b'candidate.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if _stat(info)!=_stat(os.fstat(file)): raise Blocked('changed-candidate-file')
            chunks=[];total=0
            while True:
                data=os.read(file,CHUNK)
                if not data: break
                total+=len(data)
                if total>MAX_CONTROL: raise Blocked('candidate-resource-limit')
                chunks.append(data)
            if _stat(info)!=_stat(os.fstat(file)) or _stat(info)!=_stat(os.stat(b'candidate.json',dir_fd=fd,follow_symlinks=False)):
                raise Blocked('changed-candidate-file')
            try: value=json.loads(b''.join(chunks),object_pairs_hook=_unique,parse_constant=lambda _: (_ for _ in ()).throw(Blocked('invalid-json-number')))
            except (ValueError,RecursionError): raise Blocked('invalid-candidate-json') from None
            return _validate_candidate(value)
        finally: os.close(file)

    def read(self):
        with self._disk() as fd:
            return None if fd is None else self._load(fd)

    def _save(self,fd,value,expected,*,admission=None,payload=None,name=None):
        self._verified_store(fd)
        current=self._load(fd)
        original_identity=_stat(os.stat(b'candidate.json',dir_fd=fd,follow_symlinks=False)) if current is not None else None
        actual=current['digest'] if current is not None else None
        if actual!=expected: raise Conflict('candidate-cas-conflict')
        if admission is None or payload != _json(value) or name is None:
            raise Blocked('candidate-admission-required')
        admission.revalidate()
        file=None;owned=None;replaced=False
        try:
            file=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            owned=_stat(os.fstat(file))[:2]
            view=memoryview(payload)
            while view:
                count=os.write(file,view)
                if count<=0: raise OSError('short-candidate-write')
                view=view[count:]
            os.fsync(file)
            # Preserve the candidate-specific CAS outcome before generic namespace
            # receipts can diagnose the same noncooperative replacement.
            again=self._load(fd)
            current_identity=_stat(os.stat(b'candidate.json',dir_fd=fd,follow_symlinks=False)) if again is not None else None
            if (again['digest'] if again else None)!=expected or current_identity!=original_identity: raise Conflict('candidate-cas-conflict')
            admission.record_file_creation(self.store+b'/'+name,file)
            admission.revalidate()
            if _stat(os.stat(name,dir_fd=fd,follow_symlinks=False))[:2]!=owned: raise Blocked('changed-candidate-temp')
            # Recheck CAS after writing, including noncooperative replacement.
            again=self._load(fd)
            current_identity=_stat(os.stat(b'candidate.json',dir_fd=fd,follow_symlinks=False)) if again is not None else None
            if (again['digest'] if again else None)!=expected or current_identity!=original_identity: raise Conflict('candidate-cas-conflict')
            admission.revalidate()
            os.replace(name,b'candidate.json',src_dir_fd=fd,dst_dir_fd=fd);replaced=True
            try: os.fsync(fd)
            except OSError: raise PersistenceUncertain('candidate-replaced-directory-durability-uncertain') from None
            return value
        finally:
            if file is not None: os.close(file)
            if not replaced and owned is not None:
                try:
                    if _stat(os.stat(name,dir_fd=fd,follow_symlinks=False))[:2]==owned: os.unlink(name,dir_fd=fd)
                except FileNotFoundError: pass

    def _persist(self,value,expected,*,observation=None,create=False):
        # Freeze serialization and exact temp address before the sole Disk scan.
        value['digest']=_digest({k:v for k,v in value.items() if k!='digest'})
        _validate_candidate(value);payload=_json(value)
        if len(payload)>MAX_CONTROL: raise Blocked('candidate-resource-limit-incomplete')
        name=('.pending-'+uuid.uuid4().hex).encode()
        operation=value['generation'];owner=value['digest']
        controls=tuple(Control(self.store+b'/'+target,payload,operation,owner)
                       for target in (b'candidate.json',name))
        if self.disk.root!=self.root: raise Blocked('same-root-native-disk-required')
        snapshot=self.disk.snapshot()
        plan=self.disk.project('storage_control',snapshot,Evidence(operation,owner,controls=controls))
        try:
            # Common Disk mutex precedes store flock in every persistence path.
            with self.disk.guard(plan) as admission:
                if observation is not None and _stability(observation)!=_stability(self._observe_once()):
                    raise Blocked('inputs-changed-before-persistence')
                with self._disk(create=create,admission=admission) as fd:
                    if fd is None: raise Blocked('missing-preparation-store')
                    return self._save(fd,value,expected,admission=admission,payload=payload,name=name)
        except DiskBlocked as exc:
            raise Blocked(str(exc)) from exc

    def prepare(self,*,expected):
        observation=self.observe()
        value=dict(schema=SCHEMA,generation=uuid.uuid4().hex,state='prepared',observation=observation,
                   input_digest=_digest(_stability(observation)),digest='0'*64)
        return self._persist(value,expected,observation=observation,create=True)

    def revalidate(self):
        try: candidate=self.read()
        except (OSError,Blocked): return 'unavailable'
        if candidate is None: return 'absent'
        if candidate['state']=='stale': return 'stale'
        try: current=self.observe()
        except (OSError,Blocked):
            candidate['state']='stale'
            try: self._persist(candidate,candidate['digest'])
            except (OSError,Blocked,DiskBlocked): pass
            return 'unavailable'
        if candidate['input_digest']==_digest(_stability(current)):
            try:
                with self._disk() as fd:
                    if fd is None: return 'unavailable'
                    stored=self._load(fd)
                    return 'current' if stored is not None and stored['digest']==candidate['digest'] else 'unavailable'
            except (OSError,Blocked): return 'unavailable'
        candidate['state']='stale'
        try: self._persist(candidate,candidate['digest'])
        except (OSError,Blocked,DiskBlocked): return 'unavailable'
        return 'stale'
