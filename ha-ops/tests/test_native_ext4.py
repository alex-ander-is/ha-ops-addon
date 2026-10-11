"""Opt-in actual ext4 qualification. Observer telemetry is not product admission."""
from contextlib import ExitStack
from dataclasses import asdict
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import native_disk as diskmod
import native_git as gitmod
import native_operations as operations
from native_inventory import linux_mounts

MIB = 1024**2
ROWS = {'allocation': {'loose-index-worktree-save', 'controls', 'independent-copies', 'repack', 'short-eio', 'recreation'},
        'bytes': {'byte-enospc', 'recreation'}, 'inodes': {'inode-enospc', 'recreation'}}


def git(root, *args, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
               GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
    r = subprocess.run(['git', '--git-dir=' + str(root / 'data/native/repository.git'), *args], input=input,
                       capture_output=True, env=env, timeout=30)
    if r.returncode: raise gitmod.GitFailure(r.stderr.decode(errors='replace'))
    return r.stdout


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    path.chmod(mode)
    return path


def sync_tree(root):
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('rb') as f: os.fsync(f.fileno())
    for path in [*reversed(list(root.rglob('*'))), root]:
        if path.is_dir():
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)


def observe(root):
    sync_tree(root)
    entries = {}
    for path in [root, *root.rglob('*')]:
        s = path.lstat()
        entries[(s.st_dev, s.st_ino)] = {'path': str(path.relative_to(root)), 'blocks': s.st_blocks * 512,
                                        'logical': s.st_size, 'directory': path.is_dir()}
    v = os.statvfs(root)
    return {'entries': entries, 'free_bytes': v.f_bavail * v.f_frsize, 'free_inodes': v.f_favail}


def measured(before, after, plan, family):
    allocations = sum(x['blocks'] for x in after['entries'].values()) - sum(x['blocks'] for x in before['entries'].values())
    new = set(after['entries']) - set(before['entries'])
    physical = max(0, allocations, before['free_bytes'] - after['free_bytes'])
    inodes = max(len(new), before['free_inodes'] - after['free_inodes'])
    byte_bound = sum(x.additional_peak for x in plan.decisions)
    inode_bound = sum(x.required_inodes for x in plan.decisions)
    assert physical <= byte_bound, (family, physical, byte_bound)
    assert inodes <= inode_bound, (family, inodes, inode_bound)
    return {'family': family, 'logical_bytes': sum(x['logical'] for k, x in after['entries'].items() if k in new and not x['directory']),
            'physical_bytes': physical, 'blocks_delta': allocations, 'free_bytes_delta': before['free_bytes'] - after['free_bytes'],
            'new_inodes': len(new), 'free_inodes_delta': before['free_inodes'] - after['free_inodes'],
            'additional_peak': byte_bound, 'required_inodes': inode_bound,
            'reserve': sum(x.reserve for x in plan.decisions),
            'allocations': [asdict(x) for x in plan.allocations]}


def control(op, owner, extra=None):
    return diskmod.Control(diskmod.Target(b'data/native/qualification-control', 'directory'),
                           json.dumps({'operation_id': op, 'owner_id': owner, 'full_pending_state': extra or {}}).encode())


def plan_for(root, kind='scope', **kw):
    disk = diskmod.NativeDisk(root)
    op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = diskmod.Evidence(op, owner, controls=(control(op, owner),), **kw)
    snapshot = disk.snapshot()
    assert snapshot.complete, snapshot.issues
    plan = disk.project(kind, snapshot, evidence)
    assert plan.supported, plan.unknown
    assert all(x.status == 'fits' and x.warning for x in plan.decisions)
    assert all(v.filesystem == b'ext4' and v.supported and v.quantum == 4096 and v.inodes is not None for v in snapshot.volumes)
    return disk, plan


def no_rescan(disk):
    stack = ExitStack()
    for name in ('snapshot', 'project', '_scan', '_entries', '_objects', '_profile', '_destination', '_volume'):
        stack.enter_context(mock.patch.object(disk, name, side_effect=AssertionError('phase rescan: ' + name)))
    # Observer uses os.statvfs; the product capacity route is os.fstatvfs.
    stack.enter_context(mock.patch.object(diskmod.os, 'fstatvfs', side_effect=AssertionError('phase capacity rescan')))
    return stack


def native_root(parent, name):
    root = parent / name
    (root / 'data/native').mkdir(parents=True)
    git(root, 'init', '--bare', '--initial-branch=fixture')
    write(root / 'homeassistant/sentinel', b'preserve live fixture')
    write(root / 'staging/sentinel', b'preserve staging fixture')
    return root


@pytest.fixture(scope='session')
def proof():
    value = os.environ.get('HA_OPS_EXT4_ROOT')
    if value is None: pytest.skip('requires explicit --ext4 runner; host skips are not qualification')
    root = Path(value)
    profile, run = os.environ['HA_OPS_EXT4_PROFILE'], os.environ['HA_OPS_EXT4_RUN']
    assert root == Path('/workspace/ext4') and profile in ROWS and os.geteuid() != 0
    mounts = [m for m in linux_mounts() if m.point == os.fsencode(root)]
    assert len(mounts) == 1
    mounted = mounts[0]
    flags = set(mounted.options.split(b',')) | set(mounted.super_options.split(b','))
    assert mounted.filesystem == b'ext4' and mounted.device.decode() == os.environ['HA_OPS_EXT4_DEVICE']
    assert {b'rw', b'nodev', b'nosuid', b'noexec', b'data=ordered'} <= flags
    assert not flags & {b'ro', b'quota', b'usrquota', b'grpquota', b'prjquota'}
    v = os.statvfs(root)
    assert v.f_frsize == 4096 and v.f_files > 0
    report = {'schema': 1, 'attempt': int(os.environ['HA_OPS_EXT4_ATTEMPT']), 'attempt_token': os.environ['HA_OPS_EXT4_TOKEN'],
              'status': 'failed', 'profile': profile, 'run': run, 'quantum': v.f_frsize, 'device': mounted.device.decode(),
              'filesystem': 'ext4', 'total_inodes': v.f_files, 'rows': {}}
    if os.environ['HA_OPS_EXT4_ATTEMPT'] != '0':
        previous = json.loads(Path(os.environ['HA_OPS_EXT4_PRIOR_REPORT']).read_text())
        assert previous['status'] == 'passed' and previous['run'] == run and previous['profile'] == profile
        report['rows'] = previous['rows']
    yield root, report
    report['status'] = 'passed' if set(report['rows']) == ROWS[profile] else 'failed'
    target = Path(os.environ['HA_OPS_EXT4_REPORT'])
    target.write_text(json.dumps(report, indent=2, sort_keys=True, default=lambda x: x.hex() if isinstance(x, bytes) else str(x)) + '\n')
    print('EXT4_MATRIX ' + json.dumps({'status': report['status'], 'profile': profile, 'rows': sorted(report['rows'])}))
    assert report['status'] == 'passed', {'missing_rows': sorted(ROWS[profile] - set(report['rows']))}


def only(proof, profile):
    if proof[1]['profile'] != profile: pytest.skip('different bounded ext4 profile')
    if os.environ['HA_OPS_EXT4_ATTEMPT'] != '0': pytest.skip('recreation verifies retained filesystem; original matrix retained')


def tree_bytes(rows):
    nodes = {}
    for path, mode, oid in rows:
        parts = path.split(b'/'); parent = b''
        for part in parts[:-1]:
            child = parent + (b'/' if parent else b'') + part
            nodes.setdefault(parent, {})[part] = ('tree', child)
            parent = child
        nodes.setdefault(parent, {})[parts[-1]] = (mode, oid)
    blobs = {}
    def build(path):
        data = b''
        for name, (mode, value) in sorted(nodes.get(path, {}).items(), key=lambda x: x[0] + (b'/' if x[1][0] == 'tree' else b'')):
            if mode == 'tree': mode, value = '40000', build(value)
            data += str(mode).encode() + b' ' + name + b'\0' + bytes.fromhex(value)
        oid = hashlib.sha1(b'tree ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        blobs[oid] = data
        return oid
    return build(b''), blobs


def test_loose_index_worktree_save(proof):
    only(proof, 'allocation'); parent, report = proof
    root = native_root(parent, 'scope')
    adapter = gitmod.NativeGit(root)
    selected, final, payloads = [], [], {}
    sizes = [0, 1, 4095, 4096, 4097, MIB] + [1] * 96
    for i, size in enumerate(sizes):
        path = ('homeassistant/deep/p%03d/file' % i).encode()
        content = os.urandom(size); payloads[path] = content
        source = write(root / os.fsdecode(path), content, 0o700 if i % 2 else 0o600)
        selected.append((path, diskmod.NativeDisk(root).source(source)))
        oid = hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + content).hexdigest()
        final.append((path, '100755' if i % 2 else '100644', oid))
    unselected = git(root, 'hash-object', '-w', '--stdin', input=b'untouched base').strip().decode()
    base_subtree = git(root, 'mktree', input=('100644 blob ' + unselected + '\tunselected\n').encode()).strip().decode()
    base_tree = git(root, 'mktree', input=('040000 tree ' + base_subtree + '\thomeassistant\n').encode()).strip().decode()
    base_commit = git(root, 'commit-tree', base_tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', base_commit)
    final.append((b'homeassistant/unselected', '100644', unselected))
    expected_tree, _ = tree_bytes(final)
    commit = (f'tree {expected_tree}\nparent {base_commit}\nauthor Fixture <fixture@example.invalid> 100 +0000\ncommitter Fixture <fixture@example.invalid> 100 +0000\n\nSave fixture\n').encode()
    commit_oid = hashlib.sha1(b'commit ' + str(len(commit)).encode() + b'\0' + commit).hexdigest()
    ref = b'refs/heads/deep/one/two/candidate'
    log_entry = b'0' * 40 + b' ' + commit_oid.encode() + b' Fixture <fixture@example.invalid> 100 +0000\tSave fixture\n'
    disk, plan = plan_for(root, 'save', selected=tuple(selected), base_tree=base_tree, materialize=True,
                          commit=commit, ref=ref, reflog_entry=log_entry)
    baseline = observe(root); rows = []
    initial = plan.allocations, plan.reserves, plan.decisions
    sentinel = {p: p.read_bytes() for p in (root / 'homeassistant/sentinel', root / 'staging/sentinel')}
    with disk.guard(plan) as admission:
        with no_rescan(disk):
            admission.revalidate(); admission.revalidate()
            # Preserve explicit full spools to hold the recipe's concurrent
            # overlap; actual NativeGit owns its own streaming spool lifecycle.
            for path, content in payloads.items():
                write(root / 'data/native/held-spools' / os.fsdecode(path), content)
                actual = adapter.write_blob_stream(iter([content]))
                assert actual.oid == next(oid for p, _, oid in final if p == path)
            rows.append(measured(baseline, observe(root), plan, 'loose-spool-fanout'))
            with adapter.isolated_index(base_tree, operation_id=plan.evidence.operation_id, owner_id=plan.evidence.owner_id) as index:
                adapter.update_index(index, tuple(gitmod.IndexEntry(path, mode.encode(), oid, 0) for path, mode, oid in final))
                entries = adapter.index_entries(index)
                assert len(entries) == len(final) and any(e.path == b'homeassistant/unselected' for e in entries)
                tree = adapter.write_tree(index, allowed_paths=[p for p, _, _ in final])
                assert tree == expected_tree
                index_path = Path(os.fsdecode(adapter._index(index)))
                write(index_path.with_name('index.lock'), index_path.read_bytes())
                rows.append(measured(baseline, observe(root), plan, 'complete-index-lock-trees'))
                assert git(root, 'hash-object', '-t', 'commit', '-w', '--stdin', input=commit).strip().decode() == commit_oid
                with adapter.temporary_worktree(commit_oid, operation_id=plan.evidence.operation_id,
                                               owner_id=plan.evidence.owner_id, paths=[p for p, _, _ in final]) as worktree:
                    actual_tree = Path(os.fsdecode(adapter.worktree_path(worktree)))
                    assert all((actual_tree / os.fsdecode(p)).read_bytes() == content for p, content in payloads.items())
                    assert all((actual_tree / os.fsdecode(p)).stat().st_mode & 0o777 == (0o755 if mode == '100755' else 0o644) for p, mode, _ in final)
                    for suffix in (b'', b'.lock'):
                        write(root / 'data/native/repository.git' / os.fsdecode(ref + suffix), commit_oid.encode() + b'\n')
                        write(root / 'data/native/repository.git' / os.fsdecode(b'logs/' + ref + suffix), log_entry)
                    rows.append(measured(baseline, observe(root), plan, 'worktree-admin-materialization-save-ref-reflog'))
                index_path.with_name('index.lock').unlink()
            assert (plan.allocations, plan.reserves, plan.decisions) == initial
            with pytest.raises(diskmod.Blocked): admission.revalidate()  # Published object/ref identities changed.
    assert all(p.read_bytes() == content for p, content in sentinel.items())
    report['rows']['loose-index-worktree-save'] = {'measurements': rows, 'snapshot_count': 1, 'forecast_count': 1,
                                                 'phase_rescans': 0, 'unselected_entries': 1, 'selected_entries': len(selected)}


def test_existing_reflog_complete_atomic_overlap(proof):
    only(proof, 'allocation'); parent, report = proof
    root = native_root(parent, 'existing-save')
    tree = git(root, 'mktree', input=b'').strip().decode()
    base = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    ref = b'refs/heads/deep/existing/candidate'
    git(root, 'update-ref', '--create-reflog', ref.decode(), base)
    log = root / 'data/native/repository.git' / os.fsdecode(b'logs/' + ref)
    line = (base + ' ' + base + ' Fixture <fixture@example.invalid> 100 +0000\tretained history\n').encode()
    original_log = line * 1024
    write(log, original_log)
    original_ref = (root / 'data/native/repository.git' / os.fsdecode(ref)).read_bytes()
    commit = (f'tree {tree}\nparent {base}\nauthor Fixture <fixture@example.invalid> 100 +0000\ncommitter Fixture <fixture@example.invalid> 100 +0000\n\nSave next\n').encode()
    oid = hashlib.sha1(b'commit ' + str(len(commit)).encode() + b'\0' + commit).hexdigest()
    entry = (base + ' ' + oid + ' Fixture <fixture@example.invalid> 100 +0000\tSave next\n').encode()
    disk, plan = plan_for(root, 'save', base_tree=tree, commit=commit, ref=ref, reflog_entry=entry)
    baseline = observe(root)
    with disk.guard(plan) as admission, no_rescan(disk):
        admission.revalidate(); admission.revalidate()
        assert git(root, 'hash-object', '-t', 'commit', '-w', '--stdin', input=commit).strip().decode() == oid
        write(root / 'data/native/repository.git' / os.fsdecode(ref + b'.lock'), oid.encode() + b'\n')
        write(log.with_name(log.name + '.lock'), original_log + entry)
        # Hold old controls until the exact atomic replacement boundary, then
        # retain full lock copies to qualify the required concurrent demand.
        assert log.read_bytes() == original_log and (root / 'data/native/repository.git' / os.fsdecode(ref)).read_bytes() == original_ref
        os.replace(root / 'data/native/repository.git' / os.fsdecode(ref + b'.lock'), root / 'data/native/repository.git' / os.fsdecode(ref))
        os.replace(log.with_name(log.name + '.lock'), log)
        write(root / 'data/native/repository.git' / os.fsdecode(ref + b'.lock'), oid.encode() + b'\n')
        write(log.with_name(log.name + '.lock'), original_log + entry)
        result = measured(baseline, observe(root), plan, 'existing-complete-reflog-copy-append-ref-lock')
        assert next(a.logical for a in plan.allocations if a.output == 'full-reflog-copy-append') == len(original_log) + len(entry)
        with pytest.raises(diskmod.Blocked): admission.revalidate()
    assert log.read_bytes() == original_log + entry
    report['rows']['loose-index-worktree-save']['existing_reflog'] = {**result, 'retained_log_bytes': len(original_log)}


def test_native_publication_physical_boundaries(proof,monkeypatch):
    only(proof,'allocation'); parent,report=proof
    from test_native_publication import publication_allocation_measurement
    results=[]
    for object_format in ('sha1','sha256'):
        for packed in (False,True):
            directory=parent/('publication-'+object_format+('-packed' if packed else '-loose')); directory.mkdir()
            with monkeypatch.context() as patch:
                results.append(publication_allocation_measurement(directory,object_format,observe,measured,patch,packed=packed))
    # Keep the existing mandatory matrix and its prior proofs unchanged. This
    # distinct nested measurement is independently asserted above and retained
    # in the existing durable report and stdout.
    report['rows']['loose-index-worktree-save']['native_publication']=results
    print('NATIVE_PUBLICATION_EXT4 '+json.dumps(results,default=lambda value:value.hex() if isinstance(value,bytes) else str(value)))


def identity():
    scope = {'revision': 0, 'allowlist': ['homeassistant'], 'exclude_digest': 'a' * 64}
    root = {'path': '/', 'device': 1, 'inode': 2, 'mount_digest': 'b' * 64, 'topology_digest': 'c' * 64}
    refs = {k: {'name': 'refs/heads/' + k, 'oid': 'd' * 40} for k in ('base', 'local', 'remote')}
    return {'direction': 'save', 'execution_policy': 'inert-v1', 'scope': scope, 'root': root, 'refs': refs,
            'paths': [{'path': 'homeassistant/a.yaml', 'type': 'file', 'mode': 0o100640,
                       'content_digest': 'e' * 64, 'metadata_digest': 'f' * 64}], 'effect': {'before': '1' * 64, 'after': '2' * 64}}


def test_control_atomic_overlap(proof):
    only(proof, 'allocation'); parent, report = proof
    root = native_root(parent, 'controls'); store = operations.NativeOperationStore(root / 'data')
    state = store.read(); data = os.urandom(256 * 1024)
    # Produce an actual complete prospective state without touching this owner.
    clone = parent / 'control-prospective'; shutil.copytree(root / 'data', clone)
    prospective = operations.NativeOperationStore(clone)
    prospective.preview_stream(state['revision'], 'save', identity(), [data])
    payload = operations.canonical(prospective.read())
    diff = write(root / 'source-diff', data)
    disk = diskmod.NativeDisk(root); op, owner = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = diskmod.Evidence(op, owner, controls=(control(op, owner, {'full_prospective_state': json.loads(payload), 'failure': {'message': 'E' * 4096, 'cleanup_debt': ['resource']}}),), diff=disk.source(diff))
    snap = disk.snapshot(); assert snap.complete, snap.issues
    plan = disk.project('scope', snap, evidence); assert plan.supported, plan.unknown
    baseline = observe(root); held = []
    manifest = root / 'data/native-operations/state.json'; old = manifest.read_bytes()
    def fault(stage):
        if stage == 'state_written':
            temporary = next((root / 'data/native-operations').glob('.tmp-*'))
            assert manifest.read_bytes() == old
            held.append(measured(baseline, observe(root), plan, 'full-old-new-control-temp-diff'))
    with disk.guard(plan) as admission, no_rescan(disk):
        admission.revalidate(); store.fault = fault
        write(root / 'data/native/qualification-control/full-pending.json', evidence.controls[0].payload)
        record = store.preview_stream(state['revision'], 'save', identity(), [data])
        store.fault = lambda _: None
        assert store.read_diff(record['id']) == data
    assert held
    report['rows']['controls'] = {'measurements': held, 'prior_control_retained_during_replace': True,
                                  'full_pending_failure_envelope': any(a.output == 'full-prospective-operation-state' for a in plan.allocations)}


def test_independent_recovery_copies(proof):
    only(proof, 'allocation'); parent, report = proof
    root = native_root(parent, 'copies'); disk = diskmod.NativeDisk(root)
    source = write(root / 'source', os.urandom(MIB))
    metadata = write(root / 'metadata', json.dumps({'mode': 0o640, 'uid': os.getuid(), 'gid': os.getgid(), 'mtime_ns': source.stat().st_mtime_ns}).encode())
    blob = git(root, 'hash-object', '-w', '--stdin', input=source.read_bytes()).strip().decode()
    targets = (b'data/native/recovery/first', b'data/native/recovery/second')
    disk, plan = plan_for(root, 'apply', copies=tuple((disk.source(source), t) for t in targets),
                          metadata=((disk.source(metadata), b'data/native/recovery/metadata'),),
                          replacements=((blob, b'homeassistant/replacement.tmp'),))
    baseline = observe(root)
    with disk.guard(plan) as admission, no_rescan(disk):
        admission.revalidate()
        outputs = [write(root / os.fsdecode(t), source.read_bytes()) for t in targets]
        write(root / 'data/native/recovery/metadata', metadata.read_bytes())
        with (root / 'homeassistant/replacement.tmp').open('xb') as stream:
            gitmod.NativeGit(root).copy_blob(blob, stream); stream.flush(); os.fsync(stream.fileno())
        assert len({p.stat().st_ino for p in [source, *outputs]}) == 3
        assert all(p.stat().st_blocks * 512 >= MIB for p in outputs)
        observation = measured(baseline, observe(root), plan, 'two-independent-copies-metadata-replacement')
    report['rows']['independent-copies'] = observation


def test_no_reuse_repack(proof):
    only(proof, 'allocation'); parent, report = proof
    root = native_root(parent, 'repack'); disk = diskmod.NativeDisk(root)
    blob = git(root, 'hash-object', '-w', '--stdin', input=os.urandom(MIB)).strip()
    tree = git(root, 'mktree', input=b'100644 blob ' + blob + b'\tfile\n').strip()
    commit = git(root, 'commit-tree', tree.decode(), input=b'base\n').strip()
    git(root, 'update-ref', 'refs/heads/fixture', commit.decode())
    tag = b'object ' + commit + b'\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 100 +0000\n\nfixture\n'
    git(root, 'hash-object', '-t', 'tag', '-w', '--stdin', input=tag)
    git(root, 'hash-object', '-w', '--stdin', input=os.urandom(4097))  # unreachable
    objects = git(root, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype) %(objectsize)').splitlines()
    assert {row.split()[1] for row in objects} == {b'blob', b'tree', b'commit', b'tag'}
    all_oids = b''.join(row.split()[0] + b'\n' for row in objects)
    args = ('pack-objects', '--window=0', '--depth=0', '--no-reuse-object', '--no-reuse-delta')
    old = git(root, *args, str(root / 'data/native/repository.git/objects/pack/old'), input=all_oids).strip().decode()
    old_paths = [root / ('data/native/repository.git/objects/pack/old-' + old + suffix) for suffix in ('.pack', '.idx')]
    old_hash = [hashlib.sha256(p.read_bytes()).hexdigest() for p in old_paths]
    disk, plan = plan_for(root, 'repack'); baseline = observe(root)
    with disk.guard(plan) as admission, no_rescan(disk):
        admission.revalidate(); admission.revalidate()
        name = git(root, *args, str(root / 'data/native/repository.git/objects/pack/new'), input=all_oids).strip().decode()
        new = root / ('data/native/repository.git/objects/pack/new-' + name + '.pack')
        write(new.with_name('held-pack.tmp'), new.read_bytes())
        observation = measured(baseline, observe(root), plan, 'all-object-kinds-old-pack-new-pack-index-full-temp')
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in old_paths] == old_hash
    report['rows']['repack'] = {**observation, 'objects': len(objects), 'kinds': ['blob', 'tree', 'commit', 'tag'], 'unreachable': True, 'argv': list(args)}


def fingerprints(root, store):
    paths = [root / 'homeassistant/sentinel', root / 'staging/sentinel', root / 'data/native-operations/state.json',
             root / 'data/native/repository.git/refs/heads/fixture', root / 'data/native/recovery/sentinel']
    paths.extend((root / 'data/native/repository.git/objects').rglob('[0-9a-f]' * 38))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def prepare_failure(parent, name):
    root = native_root(parent, name)
    store = operations.NativeOperationStore(root / 'data'); store.read()
    record = store.preview(0, 'save', identity(), b'recovery diff')
    store.recovery_pin(store.read()['revision'], None, record['id'], operations.digest(record['identity']))
    blob = git(root, 'hash-object', '-w', '--stdin', input=os.urandom(MIB)).strip().decode()
    sub = git(root, 'mktree', input=('100644 blob ' + blob + '\tfile\n').encode()).strip().decode()
    tree = git(root, 'mktree', input=('040000 tree ' + sub + '\thomeassistant\n').encode()).strip().decode()
    commit = git(root, 'commit-tree', tree, input=b'base\n').strip().decode()
    git(root, 'update-ref', 'refs/heads/fixture', commit)
    write(root / 'data/native/recovery/sentinel', b'recovery retained')
    adapter = gitmod.NativeGit(root)
    with adapter.isolated_index(tree, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4())): pass
    return root, store, adapter, commit


def test_kernel_byte_enospc(proof):
    only(proof, 'bytes'); parent, report = proof
    root, store, adapter, commit = prepare_failure(parent, 'bytes')
    before = fingerprints(root, store); state = store.read(); filler = root / 'filler'
    written, deadline = 0, time.monotonic() + 60
    failure = None
    with filler.open('xb', buffering=0) as f:
        while written <= 256 * MIB and time.monotonic() < deadline:
            try:
                n = f.write(b'F' * MIB); written += n; os.fsync(f.fileno())
            except OSError as e: failure = e; break
    assert failure is not None and failure.errno == errno.ENOSPC and written <= 256 * MIB
    failures = []
    # Regain bounded metadata headroom so failures reach the payload rather
    # than merely initial locks. The full original filler remains owned.
    for label, call in [
        ('preview-payload', lambda: store.preview_stream(state['revision'], 'save', identity(), [os.urandom(4 * MIB)])),
        ('blob-payload', lambda: adapter.write_blob_stream([os.urandom(4 * MIB)])),
        ('materialization', lambda: materialize_failure(adapter, commit))]:
        with filler.open('r+b') as f:
            f.truncate(max(0, filler.stat().st_size - 256 * 1024)); f.flush(); os.fsync(f.fileno())
        with pytest.raises((OSError, gitmod.GitFailure)) as caught: call()
        error = caught.value
        assert getattr(error, 'errno', None) == errno.ENOSPC or 'space' in str(error).lower(), str(error)
        failures.append({'boundary': label, 'error': str(error), 'errno': getattr(error, 'errno', None),
                         'cleanup_debt': list(getattr(error, 'cleanup_errors', ()))})
        assert fingerprints(root, store) == before
    # Force the next full state atomic write to fail after the artifact has
    # already become durable; this reaches publication, not initial creation.
    with filler.open('r+b') as f: f.truncate(max(0, filler.stat().st_size - 512 * 1024)); f.flush(); os.fsync(f.fileno())
    reached = []
    def publication(stage):
        if stage == 'artifact_durable':
            reached.append(stage)
            with filler.open('ab', buffering=0) as f:
                for _ in range(256 * MIB // 4096):
                    try: f.write(b'P' * 4096); os.fsync(f.fileno())
                    except OSError as error:
                        assert error.errno == errno.ENOSPC; return
            pytest.fail('publication filler did not reach bounded ENOSPC')
    store.fault = publication
    with pytest.raises(OSError) as caught: store.preview_stream(state['revision'], 'save', identity(), [b'atomic publication'])
    store.fault = lambda _: None
    assert caught.value.errno == errno.ENOSPC and reached == ['artifact_durable']
    assert fingerprints(root, store) == before
    failures.append({'boundary': 'state-atomic-publication-after-durable-artifact', 'errno': caught.value.errno, 'error': str(caught.value)})
    filler.unlink()
    reopened = operations.NativeOperationStore(root / 'data'); assert reopened.read() == state
    debts = recover(adapter, root)
    assert fingerprints(root, store) == before
    report['rows']['byte-enospc'] = {'kernel_errno': failure.errno, 'written_bytes': written,
                                     'failures': failures, 'recovered_resources': debts, 'state_preserved': True}


def materialize_failure(adapter, commit):
    with adapter.temporary_worktree(commit, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4()), paths=[b'homeassistant/file']):
        pytest.fail('full fixture unexpectedly materialized')


def recover(adapter, root):
    fresh = gitmod.NativeGit(root); addressed = []
    for row in fresh.resources():
        # Inspect actual API shape, retaining exact owner and operation IDs.
        result = fresh.recover_resource(row['resource_id'], owner_id=row['owner'], operation_id=row['operation'])
        assert result.state in {'removed', 'absent', 'debt'}
        current = {r['resource_id']: r for r in fresh.resources()}
        if result.state == 'debt':
            assert result.cleanup_errors and row['resource_id'] in current
            assert current[row['resource_id']]['owner'] == row['owner'] and current[row['resource_id']]['operation'] == row['operation']
        else: assert row['resource_id'] not in current
        addressed.append({'resource_id': row['resource_id'], 'owner': row['owner'], 'operation': row['operation'],
                          'status': result.state, 'errors': list(result.cleanup_errors), 'retained': current.get(row['resource_id'])})
    return addressed


def test_kernel_inode_enospc(proof):
    only(proof, 'inodes'); parent, report = proof
    root, store, adapter, _ = prepare_failure(parent, 'inodes')
    before = fingerprints(root, store); state = store.read()
    filler = root / 'inode-filler'; filler.mkdir(); paths = []; initial = os.statvfs(root)
    failure = None
    for i in range(initial.f_files + 8):
        path = filler / str(i)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd); paths.append(path)
        except OSError as e: failure = e; break
    assert failure is not None and failure.errno == errno.ENOSPC
    current = os.statvfs(root); assert current.f_bavail * current.f_frsize > 16 * MIB
    with pytest.raises(OSError) as caught: store.preview_stream(state['revision'], 'save', identity(), [b'new'])
    assert caught.value.errno == errno.ENOSPC
    with pytest.raises((OSError, gitmod.GitFailure)): adapter.write_blob_stream([b'new'])
    assert fingerprints(root, store) == before
    for path in paths: path.unlink()
    filler.rmdir()
    assert operations.NativeOperationStore(root / 'data').read() == state
    addressed = recover(adapter, root)
    report['rows']['inode-enospc'] = {'kernel_errno': failure.errno, 'created_inodes': len(paths), 'observed_total_inodes': initial.f_files,
                                     'remaining_bytes': current.f_bavail * current.f_frsize, 'state_preserved': True, 'recovered_resources': addressed}


def test_short_and_eio_boundaries(proof):
    only(proof, 'allocation'); parent, report = proof
    root, store, adapter, commit = prepare_failure(parent, 'faults'); data = os.urandom(8193)
    class Sink:
        def __init__(self, step): self.step, self.bytes = step, bytearray()
        def write(self, chunk):
            n = min(self.step, len(chunk)); self.bytes.extend(chunk[:n]); return n
    sink = Sink(7); gitmod._write_all(sink, data); assert bytes(sink.bytes) == data
    with pytest.raises(gitmod.GitFailure): gitmod._write_all(Sink(0), data)
    failures = []
    for boundary in ('artifact_written', 'artifact_fsynced', 'artifact_replaced', 'state_written', 'state_fsynced', 'state_replaced'):
        current = store.read(); old = (root / 'data/native-operations/state.json').read_bytes(); reached = []
        first = OSError(errno.EIO, 'exact injected ' + boundary)
        def fault(stage):
            if stage == boundary: reached.append(stage); raise first
        store.fault = fault
        with pytest.raises(OSError) as caught: store.preview_stream(current['revision'], 'save', identity(), [os.urandom(4097)])
        assert caught.value is first and reached == [boundary]
        store.fault = lambda _: None
        reopened = operations.NativeOperationStore(root / 'data'); new = reopened.read()
        if boundary == 'state_replaced': assert new['revision'] == current['revision'] + 1
        else: assert (root / 'data/native-operations/state.json').read_bytes() == old
        assert all(w['phase'] != 'completed' for w in new['workers'].values())
        failures.append({'boundary': boundary, 'first_error_preserved': True, 'published_revision': new['revision'], 'prior_revision': current['revision']})
    # Real adapter materialization sink: positive short writes, zero progress,
    # EIO and cleanup evidence are scoped to this copy's sink, not observer I/O.
    blob = git(root, 'hash-object', '-w', '--stdin', input=data).strip().decode()
    sink = Sink(7); assert adapter.copy_blob(blob, sink).size == len(data); assert bytes(sink.bytes) == data
    with pytest.raises(gitmod.GitFailure): adapter.copy_blob(blob, Sink(0))
    first = OSError(errno.EIO, 'exact copy sink EIO')
    class BadSink:
        def write(self, data): raise first
    with pytest.raises(OSError) as caught: adapter.copy_blob(blob, BadSink())
    assert caught.value is first
    for boundary in ('artifact-fsync', 'state-fsync', 'state-directory-fsync-after-replace'):
        current = store.read(); old = (root / 'data/native-operations/state.json').read_bytes(); reached = []
        first = OSError(errno.EIO, 'exact actual fsync ' + boundary)
        original = os.fsync
        def fsync(fd):
            route = os.readlink('/proc/self/fd/' + str(fd))
            artifact = '/native-operations/artifacts/.tmp-' in route
            state_temp = '/native-operations/.tmp-' in route
            state_dir = route == str(root / 'data/native-operations')
            if (boundary == 'artifact-fsync' and artifact) or (boundary == 'state-fsync' and state_temp) or (boundary == 'state-directory-fsync-after-replace' and state_dir):
                reached.append(route); raise first
            return original(fd)
        with mock.patch.object(operations.os, 'fsync', side_effect=fsync):
            with pytest.raises(OSError) as caught: store.preview_stream(current['revision'], 'save', identity(), [os.urandom(4097)])
        assert caught.value is first and reached
        new = operations.NativeOperationStore(root / 'data').read()
        if boundary == 'state-directory-fsync-after-replace': assert new['revision'] == current['revision'] + 1
        else: assert (root / 'data/native-operations/state.json').read_bytes() == old
        failures.append({'boundary': boundary, 'first_error_preserved': True, 'published_revision': new['revision'], 'prior_revision': current['revision']})
    first = OSError(errno.EIO, 'selected materialization sink EIO'); reached = []; copied = []
    entry = next(e for e in adapter.tree_entries(commit) if e.path == b'homeassistant/file')
    expected_payload = git(root, 'cat-file', 'blob', entry.oid)
    original_write_all = gitmod._write_all
    original_copy = adapter.copy_blob
    observed = {}
    def copy_blob(oid, sink):
        copied.append(oid)
        return original_copy(oid, sink)
    def fail_sink(sink, data):
        route = os.readlink('/proc/self/fd/' + str(sink.fileno()))
        if route.startswith(str(root / 'data/native/git-resources') + '/') and route.endswith('/tree/homeassistant/file'):
            assert copied == [entry.oid] and bytes(data[:4096]) == expected_payload[:4096]
            original_write_all(sink, data[:4096]); sink.flush(); os.fsync(sink.fileno())
            partial = Path(route).read_bytes()
            assert partial == expected_payload[:4096]
            observed.update(sink_path=route, selected_path='homeassistant/file', partial_bytes=len(partial),
                            partial_sha256=hashlib.sha256(partial).hexdigest(), expected_size=len(expected_payload))
            reached.append(len(data)); raise first
        return original_write_all(sink, data)
    with mock.patch.object(adapter, 'copy_blob', side_effect=copy_blob), mock.patch.object(gitmod, '_write_all', side_effect=fail_sink):
        with pytest.raises(OSError) as caught:
            with adapter.temporary_worktree(commit, operation_id=str(uuid.uuid4()), owner_id=str(uuid.uuid4()), paths=[b'homeassistant/file']): pytest.fail('EIO acknowledged success')
    assert caught.value is first and len(reached) == 1 and copied == [entry.oid] and observed['partial_bytes'] == 4096
    materialization = recover(adapter, root)
    report['rows']['short-eio'] = {'positive_short_bytes': len(data), 'zero_progress_refused': True,
                                  'copy_sink_eio_preserved': True, 'owner_persistence': failures, 'materialization_sink_eio': {'first_error_preserved': True, 'copy_blob_entered': copied == [entry.oid], 'reached_bytes': reached, **observed, 'addressed_recovery': materialization}}


def test_container_recreation(proof):
    parent, report = proof
    path = parent / 'recreation-sentinel'; outside = Path(os.environ['HA_OPS_EXT4_REPORT']).parent / 'recreation-identity.json'
    if os.environ['HA_OPS_EXT4_ATTEMPT'] == '0':
        write(path, os.urandom(8193))
        evidence = {'inode': path.stat().st_ino, 'device': path.stat().st_dev, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        outside.write_text(json.dumps(evidence))
    else:
        evidence = json.loads(outside.read_text())
        assert evidence == {'inode': path.stat().st_ino, 'device': path.stat().st_dev, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    report['rows']['recreation'] = {'attempt': int(os.environ['HA_OPS_EXT4_ATTEMPT']), 'sentinel': evidence}
