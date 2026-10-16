"""Preview proof map: R001..R013 use real disposable Git/live byte fixtures.

Each class covers a failure class, not a mirrored implementation expectation.
Linux is authoritative for byte names, real mountinfo, UID/GID and descriptors.
"""
import ast
import base64
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import os
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import sys
import threading
import time
import json

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import native_preview as p
import native_metadata as m
from native_git import NativeGit, TreeEntry
from native_inventory import Policy, Mount, linux_mounts
from native_topology_helpers import git, METADATA

A = b'homeassistant/p/a'
B = b'homeassistant/p/b'
C = b'homeassistant/p/c'


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def put(root, path, data):
    name = os.path.join(os.fsencode(root), path)
    os.makedirs(os.path.dirname(name), exist_ok=True)
    with open(name, 'wb') as sink:
        sink.write(data)


def bindings(root, policy):
    root = os.fsencode(root.resolve())
    if sys.platform == 'linux':
        mounts = linux_mounts()
    else:
        s = os.stat(root)
        mounts = (Mount(1, 0, ('%d:%d' % (os.major(s.st_dev), os.minor(s.st_dev))).encode(),
                        b'/', b'/', b'rw', b'fixture', b'fixture', b'rw'),)
    return m.Binding(root, policy, mounts, tuple(v.id for v in mounts), True)


def fixture_manifest(root, entries):
    """Independent small owned-data oracle; never weakens product capture."""
    records = []
    for entry in entries:
        target = os.path.join(os.fsencode(root), entry.path)
        info = os.stat(target, follow_symlinks=False)
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        with open(target, 'rb') as stream:
            actual = blob(stream.read())
        assert actual == entry.oid
        records.append(m.Record(entry.path, 'file', info.st_uid, info.st_gid,
                                stat.S_IMODE(info.st_mode), actual))
    for path in m.ancestors(entry.path for entry in entries):
        info = os.stat(os.path.join(os.fsencode(root), path), follow_symlinks=False)
        assert stat.S_ISDIR(info.st_mode)
        records.append(m.Record(path, 'directory', info.st_uid, info.st_gid,
                                stat.S_IMODE(info.st_mode), None))
    return m.Manifest(tuple(sorted(records, key=lambda r:r.path)))


class Rig:
    def __init__(self, tmp):
        self.root = (tmp / 'live').resolve()
        self.root.mkdir()
        self.scratch = (tmp / 'scratch').resolve()
        self.scratch.mkdir()
        self.policy = Policy(repository_files=(m.MANIFEST_PATH,))
        (self.root / METADATA).parent.mkdir(parents=True)
        git(self.root, 'init')
        self.files = set()
        oid = self.commit({A: b'base\n', B: b'base-b\n'})
        git(self.root, 'update-ref', 'refs/remotes/origin/main', oid)
        git(self.root, 'update-ref', 'refs/heads/synchronized', oid)
        self.binding = bindings(self.root, self.policy)
        self.adapter = NativeGit(self.root, policy=self.policy)
        self.source = p.SourceObservation('refs/remotes/origin/main', oid, (b'homeassistant',),
            'active-scope-1', 'exclude-policy-1', base_ref='refs/heads/synchronized', base_oid=oid)
        self.observations = 0
        self.callback = None
        self.preview = p.NativePreview(self.adapter, self.observe, self.binding, scratch_root=self.scratch)

    def observe(self):
        self.observations += 1
        if self.callback:
            return self.callback(self.observations)
        return self.source

    def commit(self, files, transform=None):
        for old in self.files:
            try:
                os.unlink(os.path.join(os.fsencode(self.root), old))
            except FileNotFoundError:
                pass
        self.files = set(files)
        for path, data in files.items():
            put(self.root, path, data)
        entries = tuple(TreeEntry(path, b'100755' if os.stat(os.path.join(os.fsencode(self.root), path)).st_mode & 0o100 else b'100644', blob(data), b'blob') for path, data in sorted(files.items()))
        manifest = fixture_manifest(self.root, entries)
        if transform:
            manifest = transform(manifest)
        put(self.root, m.MANIFEST_PATH, m.encode(manifest, policy=self.policy))
        git(self.root, 'add', '-A', '--', 'homeassistant', os.fsdecode(m.MANIFEST_PATH))
        git(self.root, 'commit', '--allow-empty', '-m', 'disposable fixture')
        return git(self.root, 'rev-parse', 'HEAD').decode().strip()

    def remote(self, files, transform=None):
        oid = self.commit(files, transform)
        git(self.root, 'update-ref', self.source.remote_ref, oid)
        self.source = replace(self.source, remote_oid=oid)
        return oid

    def live(self, files):
        # Fixture construction only; API never sees a write capability.
        for old in self.files | {A, B, C}:
            try:
                os.unlink(os.path.join(os.fsencode(self.root), old))
            except FileNotFoundError:
                pass
        for path, data in files.items():
            put(self.root, path, data)
        self.files = set(files)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def by_path(snapshot):
    return {r.path: r for r in snapshot.rows}


def tree_evidence(root):
    result = []
    for parent, dirs, files in os.walk(os.fsencode(root)):
        for name in sorted(dirs + files):
            path = os.path.join(parent, name)
            s = os.lstat(path)
            data = None
            if stat.S_ISREG(s.st_mode):
                with open(path, 'rb') as f:
                    data = hashlib.sha256(f.read()).hexdigest()
            result.append((os.path.relpath(path, os.fsencode(root)), s.st_dev, s.st_ino,
                s.st_mode, s.st_uid, s.st_gid, s.st_nlink, data))
    return tuple(sorted(result))


class TestExactSourcesR001:
    @pytest.mark.parametrize('direction', ['save', 'apply'])
    def test_remote_ab_live_c_orientation_index_independence(self, rig, direction):
        rig.remote({A: b'remote-a\n', B: b'remote-b\n'})
        rig.live({A: b'remote-a\n', B: b'base-b\n', C: b'live-c\n'})
        put(rig.root, b'homeassistant/unrelated-stage', b'staged sentinel')
        git(rig.root, 'add', '--', 'homeassistant/unrelated-stage')
        # Select scope explicitly so sentinel is not a managed Preview input.
        rig.source = replace(rig.source, scope=(A, B, C))
        index = (rig.root / METADATA / 'index').read_bytes()
        s = rig.preview.build(direction)
        rows = by_path(s)
        assert set(rows) == {B, C}
        assert rows[B].before.oid == blob(b'remote-b\n' if direction == 'save' else b'base-b\n')
        assert rows[B].after.oid == blob(b'base-b\n' if direction == 'save' else b'remote-b\n')
        assert rows[C].content == ('addition' if direction == 'save' else 'deletion')
        rig.preview.select(s.snapshot_id, [C])
        assert rig.preview.review(s.snapshot_id).selected[0].path == C
        assert index == (rig.root / METADATA / 'index').read_bytes()

    def test_outside_scope_and_repository_only_paths_never_become_deletions(self, rig):
        rig.live({A: b'changed\n'})
        rig.source = replace(rig.source, scope=(A,))
        s = rig.preview.build('apply')
        assert set(by_path(s)) == {A}
        assert all(r.path != m.MANIFEST_PATH and r.path != b'.gitignore' for r in s.rows)

    def test_optional_base_is_unknown_never_guessed(self, rig):
        rig.live({A: b'live\n', B: b'base-b\n'})
        rig.source = replace(rig.source, base_ref=None, base_oid=None)
        s = rig.preview.build('save')
        assert by_path(s)[A].conflict == 'base-unavailable'
        rig.preview.select(s.snapshot_id, [A])
        assert not rig.preview.review(s.snapshot_id).ready
        output = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, output)
        assert b'+live' in output.getvalue()


class TestRefreshEvidenceR002:
    @pytest.mark.parametrize('field,value', [('reachable', False), ('refreshed', False),
        ('remote_oid', 'a'*39), ('remote_oid', 'A'*40), ('remote_oid', '0'*40),
        ('remote_oid', 'a'*40), ('remote_ref', 'HEAD'), ('base_oid', None)])
    def test_bad_evidence_revokes_and_next_build_is_fresh(self, rig, field, value):
        rig.live({A: b'changed', B: b'base-b\n'})
        old = rig.preview.build('save')
        rig.preview.select(old.snapshot_id, [A])
        good = rig.source
        rig.source = replace(good, **{field: value})
        with pytest.raises((p.PreviewFailure, p.Blocked)):
            rig.preview.build('save')
        assert rig.preview.draft is None
        rig.source = good
        new = rig.preview.build('save')
        assert new.snapshot_id != old.snapshot_id and new.rows
        assert rig.preview.draft.selected == ()

    @pytest.mark.parametrize('boundary', [1, 2, 3, 4])
    def test_callback_failure_at_entry_or_final(self, rig, boundary):
        def callback(count):
            if count == boundary:
                raise OSError('unavailable refresh')
            return rig.source
        rig.callback = callback
        with pytest.raises(p.Unavailable):
            rig.preview.build('apply')
        assert rig.preview.draft is None
        rig.callback = None
        assert rig.preview.build('apply').snapshot_id

    def test_ref_changes_between_observations(self, rig):
        good = rig.source
        oid = rig.remote({A: b'new', B: b'base-b\n'})
        git(rig.root, 'update-ref', good.remote_ref, good.remote_oid)
        rig.source = good
        def callback(count):
            if count == 2:
                git(rig.root, 'update-ref', good.remote_ref, oid)
            return good
        rig.callback = callback
        with pytest.raises(p.Stale):
            rig.preview.build('apply')

    def test_wrong_object_type_is_unavailable(self, rig):
        oid = git(rig.root, 'rev-parse', 'HEAD:homeassistant/p/a').decode().strip()
        git(rig.root, 'update-ref', rig.source.remote_ref, oid)
        rig.source = replace(rig.source, remote_oid=oid)
        with pytest.raises(p.Blocked):
            rig.preview.build('apply')


class TestAcquisitionR003:
    def test_proven_absent_ancestor_and_leaf(self, rig):
        shutil.rmtree(rig.root / 'homeassistant/p')
        s = rig.preview.build('save')
        assert {r.content for r in s.rows} == {'deletion'}
        assert all(not r.issues for r in s.rows)

    @pytest.mark.parametrize('kind', ['fifo', 'symlink', 'hardlink', 'directory'])
    def test_unsafe_leaf_is_not_false_deletion_or_opened(self, rig, kind, monkeypatch):
        path = rig.root / os.fsdecode(A)
        path.unlink()
        if kind == 'fifo':
            os.mkfifo(path)
        elif kind == 'symlink':
            path.symlink_to(rig.root / os.fsdecode(B))
        elif kind == 'hardlink':
            os.link(rig.root / os.fsdecode(B), path)
        else:
            path.mkdir()
        opener = os.open
        def open_guard(name, flags, *args, **kwargs):
            if os.fsencode(name) == b'a' and not flags & os.O_DIRECTORY:
                raise AssertionError('unsafe leaf must not be opened')
            return opener(name, flags, *args, **kwargs)
        monkeypatch.setattr(os, 'open', open_guard)
        s = rig.preview.build('apply')
        assert by_path(s)[A].issues
        assert not by_path(s)[A].selectable
        assert not by_path(s)[A].viewable
        assert by_path(s)[A].content == 'unavailable'
        with pytest.raises(p.Blocked):
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        assert any(i.path == A for i in s.issues)

    @pytest.mark.parametrize('error', [PermissionError, OSError])
    def test_absence_query_error_is_explicit(self, rig, monkeypatch, error):
        (rig.root / os.fsdecode(A)).unlink()
        st = os.stat
        def fail(name, *args, **kwargs):
            if os.fsencode(name) == b'a' and kwargs.get('dir_fd') is not None:
                raise error('not proven absent')
            return st(name, *args, **kwargs)
        monkeypatch.setattr(os, 'stat', fail)
        s = rig.preview.build('save')
        assert by_path(s)[A].issues
        assert by_path(s)[A].content == 'unavailable' and not by_path(s)[A].viewable

    def test_final_absence_check_failure_does_not_issue_fresh_row(self, rig, monkeypatch):
        (rig.root / os.fsdecode(A)).unlink()
        st = os.stat
        calls = 0
        def fail(name, *args, **kwargs):
            nonlocal calls
            if os.fsencode(name) == b'a' and kwargs.get('dir_fd') is not None:
                calls += 1
                if calls == 2:
                    raise PermissionError('final absence failure')
            return st(name, *args, **kwargs)
        monkeypatch.setattr(os, 'stat', fail)
        with pytest.raises(p.Stale):
            rig.preview.build('save')
        assert rig.preview.draft is None

    def test_denied_child_never_read_and_safe_sibling_visible(self, rig, monkeypatch):
        put(rig.root, b'homeassistant/secrets.yaml', b'never read')
        put(rig.root, A, b'changed')
        opener = os.open
        def guard(name, *args, **kwargs):
            assert os.fsencode(name) != b'secrets.yaml'
            return opener(name, *args, **kwargs)
        monkeypatch.setattr(os, 'open', guard)
        assert set(by_path(rig.preview.build('save'))) == {A}

    def test_unconfirmed_mount_never_authorizes(self, rig):
        rig.preview.binding = replace(rig.binding, confirmed_mounts=())
        with pytest.raises(p.Blocked):
            rig.preview.build('save')


class TestCompleteReobservationR004:
    @pytest.mark.parametrize('mutation', ['earlier-bytes', 'addition', 'removal', 'parent-mode', 'parent-inode'])
    def test_later_read_invalidates_earlier_or_membership(self, rig, monkeypatch, mutation):
        reader = p._live_file
        fired = False
        def read(binding, path, width, sink=None):
            nonlocal fired
            result = reader(binding, path, width, sink)
            if path == B and not fired:
                fired = True
                if mutation == 'earlier-bytes':
                    put(rig.root, A, b'xxxx\n')
                elif mutation == 'addition':
                    put(rig.root, C, b'added')
                elif mutation == 'removal':
                    (rig.root / os.fsdecode(A)).unlink()
                elif mutation == 'parent-mode':
                    (rig.root / 'homeassistant/p').chmod(0o700)
                else:
                    (rig.root / 'homeassistant/p').rename(rig.root / 'homeassistant/old')
                    put(rig.root, A, b'base\n')
                    put(rig.root, B, b'base-b\n')
            return result
        monkeypatch.setattr(p, '_live_file', read)
        with pytest.raises(p.Stale):
            rig.preview.build('save')
        assert rig.preview.draft is None

    def test_unrelated_wal_and_parent_mtime_only_churn_does_not_stale(self, rig, monkeypatch):
        rig.source = replace(rig.source, scope=(A,))
        put(rig.root, A, b'changed')
        reader = p._live_file
        def read(binding, path, width, sink=None):
            result = reader(binding, path, width, sink)
            put(rig.root, b'homeassistant/p/runtime.db-wal', os.urandom(5))
            os.utime(rig.root / 'homeassistant/p', None)
            return result
        monkeypatch.setattr(p, '_live_file', read)
        s = rig.preview.build('save')
        rig.preview.select(s.snapshot_id, [A])
        assert rig.preview.review(s.snapshot_id).ready

    def test_same_length_reused_mtime_stales_all_decisions(self, rig):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        rig.preview.select(s.snapshot_id, [A])
        path = rig.root / os.fsdecode(A)
        old = path.stat()
        path.write_bytes(b'CHANGED')
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with pytest.raises(p.Stale):
            rig.preview.review(s.snapshot_id)
        assert rig.preview.draft is None
        assert rig.preview.build('save').snapshot_id != s.snapshot_id


class TestByteAuthorityR005:
    @pytest.mark.parametrize('name', [b'\xff space\t[*]?', b'-option', b'Case', b'case', b'\xc3\xa9', b'e\xcc\x81'])
    def test_lossless_bytes_labels_and_tokens(self, rig, name):
        if sys.platform != 'linux' and b'\xff' in name:
            pytest.skip('Linux invalid UTF-8 namespace proof')
        path = b'homeassistant/p/' + name
        put(rig.root, path, b'new')
        s = rig.preview.build('save')
        r = by_path(s)[path]
        assert p.decode_path(r.token) == path
        assert p.encode_path(path) == base64.b64encode(path).decode()
        assert tuple(r.path for r in s.rows) == tuple(sorted(r.path for r in s.rows))
        rig.preview.select(s.snapshot_id, [path])
        assert rig.preview.draft.selected == (path,)
        with pytest.raises((p.Blocked, p.Stale)):
            rig.preview.select(s.snapshot_id, [r.display_path])

    @pytest.mark.skipif(sys.platform != 'linux', reason='Linux full namespace proof')
    def test_all_distinct_case_normalization_and_invalid_utf8_coexist(self, rig):
        names = [b'Case', b'case', b'\xc3\xa9', b'e\xcc\x81', b'\xff', b'\xfe']
        for name in names:
            put(rig.root, b'homeassistant/p/' + name, name)
        s = rig.preview.build('save')
        assert len(s.rows) == len(names)
        rig.preview.select(s.snapshot_id, [r.path for r in s.rows])
        assert len(rig.preview.draft.selected) == len(names)

    @pytest.mark.parametrize('token', ['aG9tZWFzc2lzdGFudC9wL2E=\n', 'aG9tZWFzc2lzdGFudC9wL2F=', '../outside', '', 'AAAA', 1])
    def test_noncanonical_transport_rejected(self, token):
        with pytest.raises(p.Blocked):
            p.decode_path(token)

    @pytest.mark.parametrize('selection', [[A, A], [b'homeassistant/foreign'], [b'homeassistant/secrets.yaml'], ['homeassistant/p/a']])
    def test_duplicate_foreign_display_ids_rejected(self, rig, selection):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        with pytest.raises(p.Blocked):
            rig.preview.select(s.snapshot_id, selection)

    def test_immutable_records_and_cross_instance_ids(self, rig):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        with pytest.raises(FrozenInstanceError):
            s.rows[0].path = B
        other = p.NativePreview(rig.adapter, rig.observe, rig.binding, scratch_root=rig.scratch)
        other.build('save')
        with pytest.raises(p.Stale):
            other.view_file(s.snapshot_id, A, io.BytesIO())
        with pytest.raises(p.Blocked):
            rig.preview.select(s.snapshot_id, [s.rows[0]])


class TestMetadataR006:
    @pytest.mark.parametrize('fault', ['missing-file', 'missing-parent', 'wrong-oid', 'wrong-executable', 'malformed', 'missing-manifest'])
    def test_complete_manifest_fault_blocks_review_but_safe_view_survives(self, rig, fault):
        # Directly commit invalid metadata with the fixture Git, never through API.
        raw = fixture_manifest(rig.root, (TreeEntry(A, b'100644', blob(b'base\n'), b'blob'), TreeEntry(B, b'100644', blob(b'base-b\n'), b'blob')))
        if fault == 'missing-file':
            raw = m.Manifest(tuple(r for r in raw.entries if r.path != B))
        elif fault == 'missing-parent':
            raw = m.Manifest(tuple(r for r in raw.entries if r.path != b'homeassistant/p'))
        elif fault == 'wrong-oid':
            raw = m.Manifest(tuple(replace(r, oid='b'*40) if r.path == B else r for r in raw.entries))
        elif fault == 'wrong-executable':
            raw = m.Manifest(tuple(replace(r, mode=0o755) if r.path == B else r for r in raw.entries))
        payload = m._encode({r.path: r for r in raw.entries})
        if fault == 'malformed':
            payload = b'{"entries":'
        put(rig.root, m.MANIFEST_PATH, payload)
        if fault == 'missing-manifest':
            os.unlink(os.path.join(os.fsencode(rig.root), m.MANIFEST_PATH))
        git(rig.root, 'add', '-A', '--', os.fsdecode(m.MANIFEST_PATH))
        git(rig.root, 'commit', '-m', 'invalid disposable metadata')
        oid = git(rig.root, 'rev-parse', 'HEAD').decode().strip()
        git(rig.root, 'update-ref', rig.source.remote_ref, oid)
        rig.source = replace(rig.source, remote_oid=oid, scope=(A,))
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        assert s.issues and A in by_path(s)
        output = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, output)
        assert b'+changed' in output.getvalue()
        rig.preview.select(s.snapshot_id, [A])
        assert not rig.preview.review(s.snapshot_id).ready

    def test_oid_only_metadata_merge_does_not_claim_numeric_conflict(self, rig):
        base = b'0\n1\n2\n3\n4\n5\n6\n7\n8\n9\n'
        baseline = rig.remote({A: base, B: b'base-b\n'})
        git(rig.root, 'update-ref', rig.source.base_ref, baseline)
        rig.source = replace(rig.source, base_oid=baseline)
        rig.remote({A: base.replace(b'1\n', b'git\n'), B: b'base-b\n'})
        rig.live({A: base.replace(b'8\n', b'live\n'), B: b'base-b\n'})
        row = by_path(rig.preview.build('save'))[A]
        assert row.conflict == 'clean'
        assert not any('metadata-conflict' in i for i in row.issues)
        assert not row.metadata_changed

    @pytest.mark.parametrize('mode', [0o600, 0o4755, 0o2755, 0o1644])
    def test_full_modes_without_content_changes(self, rig, mode):
        if sys.platform != 'linux' and mode & 0o7000:
            pytest.skip('Linux full special-mode retention proof')
        (rig.root / os.fsdecode(A)).chmod(mode)
        s = rig.preview.build('save')
        row = by_path(s)[A]
        assert row.content == 'unchanged' and row.metadata_changed
        assert row.after.record.mode == mode


class TestParentDependenciesR007:
    def test_parent_only_change_requires_explicit_detail_including_unselected(self, rig):
        (rig.root / 'homeassistant/p').chmod(0o700)
        s = rig.preview.build('apply')
        assert set(by_path(s)) == {A, B}
        parent = next(v for v in s.parents if v.path == b'homeassistant/p')
        assert parent.dependents == (A, B)
        rig.preview.select(s.snapshot_id, [A])
        review = rig.preview.review(s.snapshot_id)
        assert not review.ready and review.parents == (parent,)
        assert B in review.parents[0].dependents
        rig.preview.select(s.snapshot_id, [A], approved_parents=[parent.path])
        assert rig.preview.review(s.snapshot_id).ready
        output = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, output)
        assert b'homeassistant/p/b' in output.getvalue()

    @pytest.mark.parametrize('approvals', [[b'homeassistant/foreign'], [b'homeassistant/p', b'homeassistant/p']])
    def test_invalid_parent_approval(self, rig, approvals):
        (rig.root / 'homeassistant/p').chmod(0o700)
        s = rig.preview.build('save')
        with pytest.raises(p.Blocked):
            rig.preview.select(s.snapshot_id, [A], approved_parents=approvals)

    def test_file_to_directory_all_descendants_block_partial_selection(self, rig):
        rig.remote({A: b'leaf', B: b'base-b\n'})
        (rig.root / os.fsdecode(A)).unlink()
        put(rig.root, A + b'/x', b'child-x')
        put(rig.root, A + b'/y', b'child-y')
        s = rig.preview.build('apply')
        rows = by_path(s)
        assert {A, A+b'/x', A+b'/y'} <= set(rows)
        assert all('unsupported-shape-replacement' in rows[q].issues for q in (A, A+b'/x', A+b'/y'))
        with pytest.raises(p.Blocked):
            rig.preview.select(s.snapshot_id, [A+b'/x'])


class TestGitConflictOracleR008:
    @pytest.mark.parametrize('kind,expected', [('overlap', 'unresolved'), ('nearby', 'unresolved'),
        ('separated', 'clean'), ('markers', 'clean'), ('binary-concurrent', 'binary-concurrent'),
        ('binary-unchanged-side', 'clean'), ('same', 'clean'), ('modify-delete', 'modify-delete'),
        ('add-add', 'add-add')])
    @pytest.mark.parametrize('direction', ['save', 'apply'])
    def test_real_git_three_way_and_direct_view(self, rig, kind, expected, direction):
        base = b'0\n1\n2\n3\n4\n5\n6\n7\n8\n9\n'
        baseline = rig.remote({A: base, B: b'base-b\n'})
        git(rig.root, 'update-ref', rig.source.base_ref, baseline)
        rig.source = replace(rig.source, base_oid=baseline)
        remote, live = base.replace(b'1\n', b'git\n'), base.replace(b'1\n', b'live\n')
        if kind == 'nearby':
            live = base.replace(b'2\n', b'live\n')
        elif kind == 'separated':
            live = base.replace(b'8\n', b'live\n')
        elif kind == 'markers':
            remote, live = base, b'<<<<<<< harmless\n=======\n>>>>>>> literal\n'
        elif kind == 'binary-concurrent':
            remote, live = b'git\0', b'live\0'
        elif kind == 'binary-unchanged-side':
            remote, live = base, b'live\0'
        elif kind == 'same':
            remote = live = b'same'
        elif kind == 'modify-delete':
            live = None
        elif kind == 'add-add':
            # New path absent in base; existence cannot become empty file.
            remote, live = b'git', b'live'
        remote_files = {A: remote, B: b'base-b\n'}
        live_files = {A: live, B: b'base-b\n'} if live is not None else {B: b'base-b\n'}
        if kind == 'add-add':
            remote_files[C] = remote_files.pop(A)
            live_files[C] = live_files.pop(A)
        rig.remote(remote_files)
        rig.live(live_files)
        s = rig.preview.build(direction)
        path = C if kind == 'add-add' else A
        if kind == 'same':
            assert path not in by_path(s)
            return
        row = by_path(s)[path]
        assert row.conflict == expected
        output = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, path, output)
        assert output.getvalue()
        if kind.startswith('binary'):
            assert b'Binary file:' in output.getvalue() and b'\0' not in output.getvalue()
        rig.preview.select(s.snapshot_id, [path])
        assert rig.preview.review(s.snapshot_id).ready == (expected == 'clean')

    @pytest.mark.parametrize('code', [255, -9, 128])
    def test_merge_errors_never_clean_or_confirmed_conflict(self, rig, monkeypatch, code):
        rig.remote({A: b'remote', B: b'base-b\n'})
        rig.live({A: b'live', B: b'base-b\n'})
        monkeypatch.setattr(rig.preview, '_git_tool', lambda *a: (code, b'failure'))
        row = by_path(rig.preview.build('save'))[A]
        assert row.conflict == 'merge-unavailable'


class TestScratchAndProcessR009:
    def test_hostile_git_environment_and_ancestor_config_do_not_execute(self, rig, monkeypatch, tmp_path):
        put(rig.root, A, b'changed')
        sentinel = tmp_path / 'executed'
        evil = tmp_path / 'evil-config'
        evil.write_text('[diff]\n external = touch ' + str(sentinel) + '\n[core]\n attributesFile = ' + str(tmp_path / 'attrs') + '\n')
        (tmp_path / 'attrs').write_text('* diff=evil\n')
        subprocess.run(['git', 'init', str(rig.scratch)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name, value in {'GIT_DIR':str(rig.scratch / '.git'), 'GIT_WORK_TREE':str(rig.root),
            'GIT_INDEX_FILE':str(tmp_path / 'foreign-index'), 'GIT_CONFIG_GLOBAL':str(evil),
            'GIT_CONFIG_SYSTEM':str(evil), 'GIT_CONFIG_COUNT':'1', 'GIT_CONFIG_KEY_0':'diff.external',
            'GIT_CONFIG_VALUE_0':'touch '+str(sentinel), 'GIT_TEMPLATE_DIR':str(rig.scratch),
            'GIT_DISCOVERY_ACROSS_FILESYSTEM':'1', 'GIT_EXTERNAL_DIFF':'touch '+str(sentinel)}.items():
            monkeypatch.setenv(name, value)
        s = rig.preview.build('save')
        out = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, out)
        assert b'+changed' in out.getvalue() and not sentinel.exists()
        assert not (tmp_path / 'foreign-index').exists()

    @pytest.mark.parametrize('kind', ['live', 'git', 'symlink', 'ancestor'])
    def test_unsafe_scratch_route_refused(self, rig, tmp_path, kind):
        put(rig.root, A, b'changed')
        if kind == 'live':
            scratch = rig.root
        elif kind == 'git':
            scratch = rig.root / METADATA
        elif kind == 'ancestor':
            scratch = tmp_path
        else:
            scratch = tmp_path / 'link'
            scratch.symlink_to(rig.scratch, target_is_directory=True)
        rig.preview.scratch_root = os.fsencode(scratch)
        s = rig.preview.build('save')
        with pytest.raises((p.Blocked, OSError)):
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())

    def test_foreign_scratch_file_preserved_and_debt_exposed(self, rig, monkeypatch):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        tool = rig.preview._git_tool
        foreign = []
        def replace_after(scratch, command, output):
            result = tool(scratch, command, output)
            path = Path(os.fsdecode(scratch.path)) / 'after'
            path.unlink()
            path.write_bytes(b'foreign')
            foreign.append(path)
            raise OSError('primary diff failure')
        monkeypatch.setattr(rig.preview, '_git_tool', replace_after)
        with pytest.raises(OSError, match='primary diff failure') as error:
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        assert error.value.cleanup_errors
        assert foreign[0].read_bytes() == b'foreign'
        # Only exact test-owned directory removed by fixture owner.
        shutil.rmtree(foreign[0].parent)


class TestStreamingAndRenderingR010:
    def test_large_content_bounded_producer_writes_and_complete_identity(self, rig):
        data = (b'large line for streaming\n' * 16000) + b'last\n'
        put(rig.root, A, data)
        s = rig.preview.build('save')
        class Sink:
            def __init__(self):
                self.total = self.maximum = 0
            def write(self, data):
                self.maximum = max(self.maximum, len(data))
                self.total += len(data)
                return len(data)
        sink = Sink()
        result = rig.preview.view_file(s.snapshot_id, A, sink)
        assert sink.maximum <= p.CHUNK and sink.total > len(data)
        assert by_path(s)[A].after.oid == blob(data)
        assert result.scratch_bytes >= len(data)
        assert list(rig.scratch.iterdir()) == []

    def test_split_utf8_markup_invalid_controls_bidi_and_labels_are_inert(self, rig):
        data = b'x'*(p.CHUNK-3) + '<script>"&\t\r'.encode() + b'\xff\x1b[31m' + '\u202eé'.encode() + b'\n'
        put(rig.root, A, data)
        s = rig.preview.build('save')
        out = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, out)
        text = out.getvalue().decode()
        assert '<script>' not in text and '&lt;script&gt;' in text
        assert '\x1b' not in text and '\u202e' not in text and '\r' not in text
        assert '\\x1b' in text and '\\u202e' in text and '\\xff' in text
        assert 'é' in text and '\n+' in text


class TestFailureCleanupR011:
    @pytest.mark.parametrize('failure', ['short', 'zero', 'error', 'interrupt'])
    def test_sink_failures_cleanup_and_immediate_rebuild(self, rig, failure):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        class Sink:
            def write(self, data):
                if failure == 'short':
                    return min(2, len(data))
                if failure == 'zero':
                    return 0
                if failure == 'interrupt':
                    raise KeyboardInterrupt('primary interruption')
                raise OSError('sink ENOSPC')
        if failure == 'short':
            assert rig.preview.view_file(s.snapshot_id, A, Sink()).path == A
        else:
            with pytest.raises((OSError, KeyboardInterrupt)):
                rig.preview.view_file(s.snapshot_id, A, Sink())
            assert rig.preview.draft is None
            assert rig.preview.build('save').snapshot_id
        assert list(rig.scratch.iterdir()) == []

    def test_timeout_kills_and_reaps_only_spawned_child(self, rig, monkeypatch):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        real = subprocess.Popen
        children = []
        def slow(argv, **kwargs):
            if 'diff' not in argv:
                return real(argv, **kwargs)
            child = real([sys.executable, '-c', 'import time; time.sleep(20)'], **kwargs)
            children.append(child)
            return child
        monkeypatch.setattr(p.subprocess, 'Popen', slow)
        rig.preview.timeout = .02
        with pytest.raises(subprocess.TimeoutExpired):
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        assert children and all(c.poll() is not None for c in children)
        assert list(rig.scratch.iterdir()) == []

    def test_spool_primary_failure_survives_unlink_debt(self, rig, monkeypatch):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        copy = rig.adapter.copy_blob
        def primary(oid, sink):
            if oid == blob(b'base\n'):
                raise OSError('primary copy ENOSPC')
            return copy(oid, sink)
        monkeypatch.setattr(rig.adapter, 'copy_blob', primary)
        unlink = os.unlink
        def fail(name, *args, **kwargs):
            if name == 'before':
                raise OSError('secondary unlink')
            return unlink(name, *args, **kwargs)
        monkeypatch.setattr(os, 'unlink', fail)
        with pytest.raises(OSError, match='primary copy ENOSPC') as exc:
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        assert any('secondary unlink' in v for v in exc.value.cleanup_errors)
        monkeypatch.setattr(os, 'unlink', unlink)
        for owned in rig.scratch.iterdir():
            shutil.rmtree(owned)

    @pytest.mark.skipif(sys.platform != 'linux', reason='Linux descriptor proof')
    def test_low_nofile_many_paths_do_not_accumulate_descriptors(self, rig):
        for i in range(80):
            put(rig.root, ('homeassistant/p/f%d'%i).encode(), b'new')
        old = resource.getrlimit(resource.RLIMIT_NOFILE)
        before = len(os.listdir('/proc/self/fd'))
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, old[1]))
            s = rig.preview.build('save')
            assert len(s.rows) == 80
            for row in s.rows[:4]:
                rig.preview.view_file(s.snapshot_id, row.path, io.BytesIO())
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, old)
        assert len(os.listdir('/proc/self/fd')) == before


class TestLifecycleR012:
    @pytest.mark.parametrize('reason', ['refresh', 'restart', 'update'])
    def test_reset_revokes_without_blocking_fresh_build(self, rig, reason):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        rig.preview.select(s.snapshot_id, [A])
        review = rig.preview.review(s.snapshot_id)
        rig.preview.reset(reason)
        assert rig.preview.draft is None
        with pytest.raises(p.Stale):
            rig.preview.review(s.snapshot_id)
        fresh = rig.preview.build('save')
        assert fresh.snapshot_id != s.snapshot_id and rig.preview.draft.selected == ()
        assert review.draft.snapshot_id == s.snapshot_id

    def test_unchanged_build_always_empty_and_new_instance_empty(self, rig):
        put(rig.root, A, b'changed')
        old = rig.preview.build('save')
        rig.preview.select(old.snapshot_id, [A])
        fresh = rig.preview.build('save')
        assert fresh.snapshot_id != old.snapshot_id and fresh.source_digest == old.source_digest
        assert rig.preview.draft.selected == ()
        new = p.NativePreview(rig.adapter, rig.observe, rig.binding, scratch_root=rig.scratch)
        assert new.draft is None

    @pytest.mark.parametrize('action', ['build', 'reset', 'select'])
    def test_reentrant_sink_refused_without_corrupting_current_generation(self, rig, action):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        class Sink:
            def write(self, data):
                with pytest.raises(p.Busy):
                    if action == 'build':
                        rig.preview.build('save')
                    elif action == 'reset':
                        rig.preview.reset()
                    else:
                        rig.preview.select(s.snapshot_id, [A])
                return len(data)
        assert rig.preview.view_file(s.snapshot_id, A, Sink()).snapshot_id == s.snapshot_id
        assert rig.preview.draft.selected == ()
        assert rig.preview.build('save').snapshot_id != s.snapshot_id

    def test_source_change_in_sink_never_returns_valid_file_view(self, rig):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        class Sink:
            def write(self, data):
                put(rig.root, A, b'changed again')
                return len(data)
        with pytest.raises(p.Stale):
            rig.preview.view_file(s.snapshot_id, A, Sink())
        assert rig.preview.draft is None
        assert rig.preview.build('save').snapshot_id


class TestDormancyAndNoEffectsR013:
    def test_constructor_no_io_process_or_replay(self, rig, monkeypatch):
        def forbidden(*a, **kw):
            raise AssertionError('constructor IO')
        monkeypatch.setattr(os, 'open', forbidden)
        monkeypatch.setattr(subprocess, 'Popen', forbidden)
        monkeypatch.setattr(rig, 'observe', forbidden)
        api = p.NativePreview(rig.adapter, rig.observe, rig.binding, scratch_root=rig.scratch)
        assert api.draft is None

    def test_all_api_success_and_fault_preserve_live_git_staging(self, rig, monkeypatch):
        put(rig.root, A, b'changed')
        put(rig.root, b'homeassistant/staged', b'staged sentinel')
        git(rig.root, 'add', '--', 'homeassistant/staged')
        rig.source = replace(rig.source, scope=(A, B))
        expected = tree_evidence(rig.root)
        def forbidden(*a, **kw):
            raise AssertionError('effect API')
        for name in ('write_blob_stream','update_index','isolated_index','write_tree','update_ref','delete_ref','temporary_worktree'):
            monkeypatch.setattr(rig.adapter, name, forbidden)
        for name in ('restore','stage_candidate'):
            monkeypatch.setattr(m, name, forbidden)
        for direction in ('save', 'apply'):
            s = rig.preview.build(direction)
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
            rig.preview.select(s.snapshot_id, [A])
            rig.preview.review(s.snapshot_id)
            rig.preview.reset('update')
        assert tree_evidence(rig.root) == expected
        assert list(rig.scratch.iterdir()) == []
        s = rig.preview.build('save')
        class Broken:
            def write(self, data):
                raise OSError('sink failure')
        with pytest.raises(OSError):
            rig.preview.view_file(s.snapshot_id, A, Broken())
        assert tree_evidence(rig.root) == expected

    @staticmethod
    def guard(sources):
        for name, source in sources.items():
            for node in ast.walk(ast.parse(source)):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [part for a in node.names for part in a.name.split('.')]
                elif isinstance(node, ast.ImportFrom):
                    modules = (node.module or '').split('.') + [part for a in node.names for part in a.name.split('.')]
                assert 'native_preview' not in modules or name in ('native_save.py','native_publication.py','native_scope.py'), name

    @pytest.mark.parametrize('source', ['import native_preview as alias', 'from app.native_preview import NativePreview',
        'from app import native_preview as alias', 'def lazy():\n    from ..app import native_preview as alias'])
    def test_guard_detects_alias_package_lazy_production_import(self, source):
        with pytest.raises(AssertionError):
            self.guard({'server.py':source})
        with pytest.raises(AssertionError):
            self.guard({'another_dormant.py':source})

    def test_production_has_no_preview_import(self):
        app = Path(p.__file__).parent
        self.guard({path.name:path.read_text() for path in app.glob('*.py') if path.name != 'native_preview.py'})


class TestAdditionalClassProofs:
    @pytest.mark.skipif(sys.platform != 'linux', reason='Linux exact owner/group proof')
    def test_numeric_whole_record_conflict_remains_viewable_without_readiness(self, rig):
        rig.remote({A:b'base\n', B:b'base-b\n'}, lambda manifest: m.Manifest(tuple(
            replace(r, uid=11, gid=12) if r.path == A else r for r in manifest.entries)))
        os.chown(rig.root / os.fsdecode(A), 13, 14)
        s = rig.preview.build('apply')
        row = by_path(s)[A]
        assert row.metadata_changed and row.selectable
        assert 'metadata-conflict:uid,gid' in row.issues
        out = io.BytesIO()
        rig.preview.view_file(s.snapshot_id, A, out)
        assert b'Metadata:' in out.getvalue()
        rig.preview.select(s.snapshot_id, [A])
        assert not rig.preview.review(s.snapshot_id).ready

    def test_missing_historical_metadata_blocks_without_live_defaults(self, rig):
        put(rig.root, m.MANIFEST_PATH, b'{"entries":')
        git(rig.root, 'add', '--', os.fsdecode(m.MANIFEST_PATH))
        git(rig.root, 'commit', '-m', 'invalid historical metadata')
        oid = git(rig.root, 'rev-parse', 'HEAD').decode().strip()
        git(rig.root, 'update-ref', rig.source.base_ref, oid)
        rig.source = replace(rig.source, base_oid=oid)
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        assert any(i.reason.startswith('base-metadata-unavailable') for i in s.issues)
        rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        rig.preview.select(s.snapshot_id, [A])
        assert not rig.preview.review(s.snapshot_id).ready

    def test_reverse_shape_dependency_includes_all_descendants(self, rig):
        (rig.root / os.fsdecode(A)).unlink()
        rig.files.remove(A)
        rig.remote({A+b'/x':b'x', A+b'/y':b'y', B:b'base-b\n'})
        shutil.rmtree(rig.root / os.fsdecode(A))
        put(rig.root, A, b'leaf')
        s = rig.preview.build('save')
        rows = by_path(s)
        assert {A, A+b'/x', A+b'/y'} <= set(rows)
        assert all('unsupported-shape-replacement' in rows[q].issues for q in (A,A+b'/x',A+b'/y'))
        with pytest.raises(p.Blocked):
            rig.preview.select(s.snapshot_id, [A])

    def test_clean_addition_stays_visible_beside_conflict(self, rig):
        rig.remote({A:b'remote', B:b'base-b\n'})
        rig.live({A:b'live', B:b'base-b\n', C:b'clean addition'})
        s = rig.preview.build('save')
        rows = by_path(s)
        assert rows[A].conflict == 'unresolved' and rows[C].conflict == 'clean'
        rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        rig.preview.select(s.snapshot_id, [C])
        assert rig.preview.review(s.snapshot_id).ready

    @pytest.mark.parametrize('failure', [OSError('Git I/O failure'), subprocess.TimeoutExpired(['git'], .01)])
    def test_text_merge_failure_is_explicit_and_safe_rows_survive(self, rig, monkeypatch, failure):
        rig.remote({A:b'remote', B:b'base-b\n'})
        rig.live({A:b'live', B:b'base-b\n', C:b'addition'})
        tool = rig.preview._git_tool
        def fail(scratch, command, output):
            if 'merge-file' in command:
                raise failure
            return tool(scratch, command, output)
        monkeypatch.setattr(rig.preview, '_git_tool', fail)
        s = rig.preview.build('apply')
        assert by_path(s)[A].conflict == 'merge-unavailable'
        assert C in by_path(s)
        rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        rig.preview.select(s.snapshot_id, [A])
        assert not rig.preview.review(s.snapshot_id).ready
        assert list(rig.scratch.iterdir()) == []

    def test_real_parallel_session_refuses_then_unlocks(self, rig):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        entered, release = threading.Event(), threading.Event()
        results = []
        class Sink:
            def write(self, data):
                entered.set()
                assert release.wait(10)
                return len(data)
        def render():
            results.append(rig.preview.view_file(s.snapshot_id, A, Sink()))
        child = threading.Thread(target=render)
        child.start()
        try:
            assert entered.wait(10)
            with pytest.raises(p.Busy):
                rig.preview.build('save')
            with pytest.raises(p.Busy):
                rig.preview.reset()
        finally:
            release.set()
            child.join(10)
        assert not child.is_alive() and results[0].snapshot_id == s.snapshot_id
        assert rig.preview.build('save').snapshot_id != s.snapshot_id

    def test_scratch_directory_open_failure_cleans_exact_created_leaf(self, rig, monkeypatch):
        put(rig.root, A, b'changed')
        s = rig.preview.build('save')
        opener = os.open
        def fail(name, *args, **kwargs):
            if os.fsencode(name).startswith(b'native-preview-'):
                raise OSError('owned scratch open failed')
            return opener(name, *args, **kwargs)
        monkeypatch.setattr(os, 'open', fail)
        with pytest.raises(OSError, match='owned scratch open failed'):
            rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
        assert list(rig.scratch.iterdir()) == []

    def test_merge_timeout_and_cleanup_fault_preserve_original_cause(self, rig, monkeypatch):
        rig.remote({A:b'remote', B:b'base-b\n'})
        rig.live({A:b'live', B:b'base-b\n'})
        def fail(scratch, command, output):
            raise subprocess.TimeoutExpired(['git', 'merge-file'], .01)
        monkeypatch.setattr(rig.preview, '_git_tool', fail)
        unlink = os.unlink
        def debt(name, *args, **kwargs):
            if name == 'base':
                raise OSError('secondary cleanup fault')
            return unlink(name, *args, **kwargs)
        monkeypatch.setattr(os, 'unlink', debt)
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            rig.preview.build('save')
        assert any('secondary cleanup fault' in v for v in exc.value.cleanup_errors)
        assert rig.preview.draft is None
        monkeypatch.setattr(os, 'unlink', unlink)
        for owned in rig.scratch.iterdir():
            shutil.rmtree(owned)


# Proposed append-only regressions for ha-ops/tests/test_native_preview.py.
# Integration is developer-owned; this external source never edits the repository.

@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_contract_metadata_only_without_base_is_unknown(rig, direction):
    (rig.root / os.fsdecode(A)).chmod(0o600)
    rig.source = replace(rig.source, base_ref=None, base_oid=None)
    snapshot = rig.preview.build(direction)
    row = by_path(snapshot)[A]
    assert row.content == 'unchanged' and row.metadata_changed
    output = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, A, output)
    rig.preview.select(snapshot.snapshot_id, [A])
    assert row.conflict == 'base-unavailable'
    assert not rig.preview.review(snapshot.snapshot_id).ready


def test_contract_parent_detail_includes_known_outside_scope_managed_sibling(rig):
    rig.source = replace(rig.source, scope=(A,))
    (rig.root / 'homeassistant/p').chmod(0o700)
    snapshot = rig.preview.build('apply')
    parent = next(v for v in snapshot.parents if v.path == b'homeassistant/p')
    assert B not in by_path(snapshot), 'outside scope must remain excluded from leaf selection'
    assert (rig.root / os.fsdecode(B)).is_file()
    assert A in parent.dependents and B in parent.dependents
    output = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, A, output)
    assert B in output.getvalue(), 'shared-parent approval must disclose unselected effect'


def test_contract_renderer_fdopen_failure_closes_acquired_descriptor(rig, monkeypatch):
    put(rig.root, A, b'changed')
    snapshot = rig.preview.build('save')
    original = os.fdopen
    acquired = []
    def fail(fd, mode='r', *args, **kwargs):
        if mode == 'rb':
            acquired.append(fd)
            raise OSError('injected render allocation failure')
        return original(fd, mode, *args, **kwargs)
    monkeypatch.setattr(os, 'fdopen', fail)
    try:
        with pytest.raises(OSError, match='injected render allocation failure'):
            rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())
        assert acquired
        for fd in acquired:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert not list(rig.scratch.iterdir())
        assert rig.preview.draft is None
    finally:
        # Exact proof-owned descriptor cleanup for the failing implementation.
        for fd in acquired:
            try:
                os.close(fd)
            except OSError:
                pass


def test_contract_file_and_parent_modes_are_explicit_octal(rig):
    (rig.root / os.fsdecode(A)).chmod(0o600)
    (rig.root / 'homeassistant/p').chmod(0o700)
    snapshot = rig.preview.build('save')
    output = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, A, output)
    lines = output.getvalue().decode().splitlines()
    leaf = next(line for line in lines if line.startswith('Metadata:'))
    parent = next(line for line in lines if line.startswith('Parent: homeassistant/p '))
    # Accept conventional fixed-width octal or Python's explicit octal prefix.
    import re
    assert re.search(r'(?<![0-9])(?:0o600|0600)(?![0-9])', leaf)
    assert re.search(r'(?<![0-9])(?:0o700|0700)(?![0-9])', parent)


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_exact_absent_active_leaf_stays_viewable(rig, direction):
    rig.live({A: b'base\n'})
    rig.source = replace(rig.source, scope=(A, B))
    snapshot = rig.preview.build(direction)
    row = by_path(snapshot)[B]
    assert row.content == ('deletion' if direction == 'save' else 'addition')
    assert row.selectable
    output = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, B, output)
    assert b'base-b' in output.getvalue()
    rig.preview.select(snapshot.snapshot_id, [B])
    assert rig.preview.review(snapshot.snapshot_id).ready


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_parent_review_discloses_inactive_tracked_descendant(rig, direction):
    rig.source = replace(rig.source, scope=(A,))
    (rig.root / 'homeassistant/p').chmod(0o700)
    snapshot = rig.preview.build(direction)
    parent = next(row for row in snapshot.parents if row.path == b'homeassistant/p')
    assert parent.dependents == (A, B)
    # Disclosure does not turn the inactive sibling into a selectable operation.
    assert B not in by_path(snapshot)
    rig.preview.select(snapshot.snapshot_id, [A], approved_parents=[parent.path])
    review = rig.preview.review(snapshot.snapshot_id)
    assert review.ready
    assert B in review.parents[0].dependents
    output = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, A, output)
    assert b'homeassistant/p/b' in output.getvalue()



# Independent Preview failure-class regressions.

@pytest.mark.parametrize('phase', ['scratch-first-fstat', 'render-fdopen'])
def test_descriptor_acquisition_failure_releases_raw_fd(rig, monkeypatch, phase):
    put(rig.root, A, b'changed content\n')
    s = rig.preview.build('save')
    opener, fstat, fdopen = os.open, os.fstat, os.fdopen
    raw = []
    fired = []
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if (phase == 'scratch-first-fstat' and name == 'before') or (phase == 'render-fdopen' and name == 'patch' and not args[0] & os.O_CREAT):
            raw.append(fd)
        return fd
    def inspected(fd):
        if phase == 'scratch-first-fstat' and raw and fd == raw[-1] and not fired:
            fired.append(True)
            raise OSError('injected acquisition fstat EIO')
        return fstat(fd)
    def wrapped(fd, *args, **kwargs):
        if phase == 'render-fdopen' and raw and fd == raw[-1] and args == ('rb',) and not fired:
            fired.append(True)
            raise OSError('injected render fdopen allocation failure')
        return fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', inspected)
    monkeypatch.setattr(os, 'fdopen', wrapped)
    with pytest.raises(OSError) as raised:
        rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
    monkeypatch.setattr(os, 'open', opener)
    monkeypatch.setattr(os, 'fstat', fstat)
    monkeypatch.setattr(os, 'fdopen', fdopen)
    leaked = []
    for fd in raw:
        try:
            info = os.fstat(fd)
        except OSError:
            continue
        leaked.append((fd, info.st_dev, info.st_ino))
        os.close(fd)  # Proof owner closes only its exact leaked acquisition.
    print(json.dumps({'phase':phase, 'primary':str(raised.value), 'leaked':leaked,
        'cleanup_errors':list(getattr(raised.value, 'cleanup_errors', ())),
        'retained_scratch':[str(v) for v in rig.scratch.iterdir()], 'draft_cleared':rig.preview.draft is None}))
    assert not leaked, 'Raw file descriptor remains open after acquisition failure'


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_safe_descendant_diff_remains_viewable_during_shape_conflict(rig, direction):
    (rig.root / os.fsdecode(A)).unlink()
    x = A + b'/x'
    put(rig.root, x, b'safe child content\n')
    put(rig.root, A + b'/y', b'other child\n')
    s = rig.preview.build(direction)
    row = by_path(s)[x]
    assert 'unsupported-shape-replacement' in row.issues
    assert row.before is None or row.after is None
    before = tree_evidence(rig.root)
    out = io.BytesIO()
    try:
        view = rig.preview.view_file(s.snapshot_id, x, out)
    except p.Blocked as exc:
        print(json.dumps({'direction':direction,'path':os.fsdecode(x),'issues':row.issues,
            'conflict':row.conflict,'actual':str(exc),'output_bytes':len(out.getvalue()),
            'live_unchanged':tree_evidence(rig.root)==before}))
        raise
    assert view.path == x and b'safe child content' in out.getvalue()
    assert tree_evidence(rig.root) == before
    with pytest.raises(p.Blocked):
        rig.preview.select(s.snapshot_id, [x])


def test_timeout_cleanup_does_not_depend_on_child_voluntary_exit(rig, monkeypatch):
    put(rig.root, A, b'changed\n')
    s = rig.preview.build('save')
    real = subprocess.Popen
    owned = []
    def child(argv, **kwargs):
        if 'diff' not in argv:
            return real(argv, **kwargs)
        instance = real([sys.executable, '-c', 'import time; time.sleep(0.65)'], **kwargs)
        kill = instance.kill
        calls, waits = [], []
        wait = instance.wait
        def measured_wait(*a, **kw):
            start = time.monotonic()
            try:
                return wait(*a, **kw)
            finally:
                waits.append((kw.get('timeout'),time.monotonic()-start))
        def interrupted_kill():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('injected transient signal EIO')
            return kill()
        instance.kill = interrupted_kill
        instance.wait = measured_wait
        owned.append((instance, kill, calls, waits))
        return instance
    monkeypatch.setattr(p.subprocess, 'Popen', child)
    rig.preview.timeout = .01
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        rig.preview.view_file(s.snapshot_id, A, io.BytesIO())
    elapsed = time.monotonic() - started
    print({'elapsed':elapsed,'kill_attempts':owned[0][2],
        'waits':owned[0][3], 'cleanup_errors':getattr(exc.value,'cleanup_errors',()),
        'child_returncode':owned[0][0].returncode})
    assert all(seconds < .25 for timeout,seconds in owned[0][3] if timeout is None), 'Timeout cleanup blocked awaiting child voluntary exit after kill failure'


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_parent_only_change_without_base_cannot_be_ready(rig, direction):
    rig.source = replace(rig.source, base_ref=None, base_oid=None)
    (rig.root / 'homeassistant/p').chmod(0o700)
    snapshot = rig.preview.build(direction)
    row = by_path(snapshot)[A]
    assert row.content == 'unchanged' and not row.metadata_changed
    assert row.conflict == 'base-unavailable' and row.viewable
    with pytest.raises(p.Blocked, match='related-resolved-parent'):
        rig.preview.select(snapshot.snapshot_id, [A], approved_parents=row.parents)
    snapshot = rig.preview.build(direction)
    rig.preview.select(snapshot.snapshot_id, [A])
    review = rig.preview.review(snapshot.snapshot_id)
    assert not review.ready and 'base-unavailable' in review.parents[0].issues


def test_preserved_parent_sibling_disclosure_never_reads_excluded_content(rig, monkeypatch):
    rig.source = replace(rig.source, scope=(A,))
    (rig.root / 'homeassistant/p').chmod(0o700)
    opener = os.open
    def guarded(name, *args, **kwargs):
        if name == b'b' or name == os.path.join(os.fsencode(rig.root), B):
            raise AssertionError('inactive sibling content acquisition')
        return opener(name, *args, **kwargs)
    monkeypatch.setattr(os, 'open', guarded)
    snapshot = rig.preview.build('apply')
    out = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, A, out)
    assert B in out.getvalue() and B not in by_path(snapshot)


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_reverse_shape_safe_remote_descendant_is_viewable(rig, direction):
    (rig.root / os.fsdecode(A)).unlink()
    x = A + b'/x'
    rig.remote({x: b'remote child\n', B: b'base-b\n'})
    shutil.rmtree(rig.root / os.fsdecode(A))
    put(rig.root, A, b'live parent file\n')
    snapshot = rig.preview.build(direction)
    row = by_path(snapshot)[x]
    assert row.viewable and not row.selectable
    assert 'unsupported-shape-replacement' in row.issues
    out = io.BytesIO()
    rig.preview.view_file(snapshot.snapshot_id, x, out)
    assert b'remote child' in out.getvalue()
    with pytest.raises(p.Blocked):
        rig.preview.select(snapshot.snapshot_id, [x])


@pytest.mark.parametrize('phase', ['absolute-directory', 'writer', 'reader'])
def test_raw_acquisition_baseexception_preserves_primary_and_close_debt(rig, monkeypatch, phase):
    class Abort(BaseException):
        pass
    raw, closed = [], []
    opener, fstat, fdopen, close = os.open, os.fstat, os.fdopen, os.close
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if ((phase == 'absolute-directory' and name != b'/' and not raw) or
                (phase == 'writer' and name == 'before') or
                (phase == 'reader' and name == 'patch' and not args[0] & os.O_CREAT)):
            raw.append(fd)
        return fd
    def inspected(fd):
        if phase != 'reader' and raw and fd == raw[-1]:
            raise Abort('primary acquisition abort')
        return fstat(fd)
    def wrapped(fd, *args, **kwargs):
        if phase == 'reader' and raw and fd == raw[-1]:
            raise Abort('primary acquisition abort')
        return fdopen(fd, *args, **kwargs)
    def closing(fd):
        close(fd)
        if raw and fd == raw[-1]:
            closed.append(fd)
            raise OSError('secondary close fault after close')
    put(rig.root, A, b'changed\n')
    snapshot = rig.preview.build('save')
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', inspected)
    monkeypatch.setattr(os, 'fdopen', wrapped)
    monkeypatch.setattr(os, 'close', closing)
    with pytest.raises(Abort, match='primary acquisition abort') as raised:
        if phase == 'absolute-directory':
            p._open_absolute_directory(os.fsencode(rig.scratch))
        else:
            rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())
    assert raw and closed
    assert any('secondary close fault' in item for item in raised.value.cleanup_errors)
    for fd in raw:
        with pytest.raises(OSError):
            fstat(fd)


def test_persistent_child_kill_fault_is_bounded_addressed_and_unlocks(rig, monkeypatch):
    put(rig.root, A, b'changed\n')
    snapshot = rig.preview.build('save')
    real, owned = subprocess.Popen, []
    def child(argv, **kwargs):
        if 'diff' not in argv:
            return real(argv, **kwargs)
        instance = real([sys.executable, '-c', 'import time; time.sleep(20)'], **kwargs)
        kill = instance.kill
        def failed_kill():
            raise OSError('persistent signal failure')
        instance.kill = failed_kill
        owned.append((instance, kill))
        return instance
    monkeypatch.setattr(p.subprocess, 'Popen', child)
    rig.preview.timeout = .01
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())
        assert time.monotonic() - started < 5
        instance, kill = owned[0]
        assert raised.value.pending_children == (instance,) and instance.poll() is None
        assert any('pid=%d' % instance.pid in item for item in raised.value.cleanup_errors)
        assert list(rig.scratch.iterdir()) and rig.preview.draft is None
        # A failed owned child cannot retain the session lock or old choices.
        fresh = rig.preview.build('save')
        assert fresh.snapshot_id != snapshot.snapshot_id and not rig.preview.draft.selected
    finally:
        for instance, kill in owned:
            kill()
            instance.wait(timeout=2)
        # Exact fixture owner cleans only its retained private scratch after reap.
        for path in rig.scratch.iterdir():
            shutil.rmtree(path)


def test_fixture_manifest_is_independent_of_product_capture_and_ancestor_churn(rig, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('fixture must not invoke product capture')
    monkeypatch.setattr(m, 'capture', forbidden)
    marker = rig.root.parent / 'unrelated-shared-parent-churn'
    marker.write_bytes(b'fixture owned')
    try:
        rig.remote({A: b'fixture independent\n', B: b'base-b\n'})
        put(rig.root, A, b'base\n')
        snapshot = rig.preview.build('apply')
        assert A in by_path(snapshot)
    finally:
        marker.unlink()


@pytest.mark.parametrize('helper', ['_open_absolute_directory', '_directory_at'])
@pytest.mark.parametrize('abort', [False, True])
def test_whole_preview_inventory_acquisition_closes_child_and_preserves_abort(rig, monkeypatch, helper, abort):
    import native_inventory as inventory_module
    class Abort(BaseException):
        pass
    original = getattr(inventory_module, helper)
    opener, fstat, close = os.open, os.fstat, os.close
    inside, raw, fired = [], [], []
    def guarded(*args, **kwargs):
        inside.append(True)
        try:
            return original(*args, **kwargs)
        finally:
            inside.pop()
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if inside and name != b'/' and not fired and not raw:
            raw.append(fd)
        return fd
    def inspected(fd):
        if inside and raw and fd == raw[0] and not fired:
            fired.append(True)
            if abort:
                raise Abort('primary inventory acquisition abort')
            raise OSError('primary inventory acquisition EIO')
        return fstat(fd)
    def closing(fd):
        close(fd)
        if abort and raw and fd == raw[0] and inside:
            raise OSError('secondary inventory close fault after close')
    monkeypatch.setattr(inventory_module, helper, guarded)
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', inspected)
    monkeypatch.setattr(os, 'close', closing)
    if abort:
        with pytest.raises(Abort, match='primary inventory acquisition abort') as raised:
            rig.preview.build('save')
        assert any('secondary inventory close fault' in item for item in raised.value.cleanup_errors)
    else:
        # Inventory may report uncertainty; repeated observation must not publish
        # contradictory authority. Either way acquired descriptors are released.
        with pytest.raises(p.Stale):
            rig.preview.build('save')
    assert raw and fired and rig.preview.draft is None
    for fd in raw:
        with pytest.raises(OSError):
            fstat(fd)


@pytest.mark.parametrize('direction', ['save', 'apply'])
def test_unsupported_remote_kind_is_unknown_never_empty_diff_side(rig, direction):
    link = git(rig.root, 'hash-object', '-w', '--', os.fsdecode(A)).decode().strip()
    git(rig.root, 'update-index', '--cacheinfo', '120000,%s,%s' % (link, os.fsdecode(A)))
    git(rig.root, 'commit', '-m', 'disposable unsafe tracked kind')
    oid = git(rig.root, 'rev-parse', 'HEAD').decode().strip()
    git(rig.root, 'update-ref', rig.source.remote_ref, oid)
    rig.source = replace(rig.source, remote_oid=oid)
    snapshot = rig.preview.build(direction)
    row = by_path(snapshot)[A]
    assert row.content == 'unavailable' and row.conflict == 'source-unavailable'
    assert not row.viewable and not row.selectable
    with pytest.raises(p.Blocked):
        rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())


@pytest.mark.parametrize('helper', ['_open_absolute_directory', '_directory_at'])
def test_inventory_parent_handoff_close_fault_is_never_blindly_retried(rig, monkeypatch, helper):
    import native_inventory as inventory_module
    class Abort(BaseException):
        pass
    opener, duplicate, close, fstat = os.open, os.dup, os.close, os.fstat
    root = opener(os.fsencode(rig.root), os.O_RDONLY | os.O_DIRECTORY)
    raw, closes = [], []
    def opened(*args, **kwargs):
        fd = opener(*args, **kwargs)
        raw.append(fd)
        return fd
    def duplicated(fd):
        result = duplicate(fd)
        raw.append(result)
        return result
    def closing(fd):
        closes.append(fd)
        close(fd)
        if raw and fd == raw[0]:
            raise Abort('parent handoff close after close')
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'dup', duplicated)
    monkeypatch.setattr(os, 'close', closing)
    try:
        with pytest.raises(Abort, match='parent handoff close after close') as raised:
            if helper == '_open_absolute_directory':
                inventory_module._open_absolute_directory(os.fsencode(rig.root))
            else:
                inventory_module._directory_at(root, b'homeassistant')
        assert closes.count(raw[0]) == 1
        assert any('parent-handoff-close:fd=%d' % raw[0] in item for item in raised.value.cleanup_errors)
        for fd in raw:
            with pytest.raises(OSError):
                fstat(fd)
    finally:
        close(root)


@pytest.mark.parametrize('helper', ['_open_absolute_directory', '_directory_at'])
def test_contract_oserror_cleanup_debt_survives_inventory_composition(rig, monkeypatch, helper):
    import native_inventory as inventory_module
    original = getattr(inventory_module, helper)
    opener, fstat, close = os.open, os.fstat, os.close
    inside, raw, fired, close_failures = [], [], set(), set()
    captured_errors = []
    def guarded(*args, **kwargs):
        inside.append(True)
        try:
            return original(*args, **kwargs)
        except BaseException as exc:
            captured_errors.append(exc)
            raise
        finally:
            inside.pop()
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if inside and os.fsencode(name) != b'/':
            raw.append(fd)
        return fd
    def inspected(fd):
        if inside and fd in raw and fd not in fired:
            fired.add(fd)
            raise OSError('primary child acquisition EIO')
        return fstat(fd)
    def closing(fd):
        if inside and fd in fired and fd not in close_failures:
            close_failures.add(fd)
            raise OSError('secondary child close failed before close')
        return close(fd)
    put(rig.root, A, b'changed\n')
    monkeypatch.setattr(inventory_module, helper, guarded)
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', inspected)
    monkeypatch.setattr(os, 'close', closing)
    try:
        # Acquisition uncertainty may be a diagnostic, but original exceptional
        # cleanup debt must not disappear into an apparently completed snapshot.
        with pytest.raises(OSError, match='primary child acquisition EIO') as raised:
            rig.preview.build('save')
        assert captured_errors
        assert raised.value is captured_errors[0]
        assert 'secondary child close failed before close' in str(raised.value.cleanup_errors)
        assert rig.preview.draft is None
    finally:
        # Exact proof-owned still-open descriptors; restore normal close first.
        monkeypatch.setattr(os, 'close', close)
        for fd in set(raw):
            try:
                fstat(fd)
            except OSError:
                continue
            close(fd)
        monkeypatch.setattr(os, 'open', opener)
        monkeypatch.setattr(os, 'fstat', fstat)
        monkeypatch.setattr(inventory_module, helper, original)
    fresh = rig.preview.build('save')
    assert fresh.snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('phase', ['live', 'parent'])
@pytest.mark.parametrize('error_type', [OSError, p.Blocked])
def test_preview_live_parent_acquisition_debt_preserves_exact_primary(rig, monkeypatch, phase, error_type):
    put(rig.root, A, b'changed\n')
    old = rig.preview.build('save')
    rig.preview.select(old.snapshot_id, [A])
    original_live, original_route = p._live_file, p._route
    opener, fstat, close = os.open, os.fstat, os.close
    inside, raw, fired, close_faults = [], [], [], []
    primary = error_type('primary exact %s acquisition failure' % phase)
    def live(binding, path, *args):
        if phase == 'live' and path == A:
            inside.append(True)
        try:
            return original_live(binding, path, *args)
        finally:
            if phase == 'live' and path == A:
                inside.pop()
    @p.contextmanager
    def route(binding, path, kind, **kwargs):
        target = phase == 'parent' and path == b'homeassistant/p' and kind == 'directory'
        if target:
            inside.append(True)
        try:
            with original_route(binding, path, kind, **kwargs) as result:
                yield result
        finally:
            if target:
                inside.pop()
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if inside and os.fsencode(name) == (b'a' if phase == 'live' else b'p'):
            raw.append(fd)
        return fd
    def inspected(fd):
        if inside and raw and fd == raw[-1] and not fired:
            fired.append(True)
            raise primary
        return fstat(fd)
    def closing(fd):
        if inside and raw and fd == raw[-1] and not close_faults:
            close_faults.append(fd)
            raise OSError('secondary exact Preview close failed before close')
        return close(fd)
    monkeypatch.setattr(p, '_live_file', live)
    monkeypatch.setattr(p, '_route', route)
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', inspected)
    monkeypatch.setattr(os, 'close', closing)
    try:
        with pytest.raises(error_type) as raised:
            rig.preview.build('save')
        assert raised.value is primary
        assert 'secondary exact Preview close' in str(primary.cleanup_errors)
        assert raw and close_faults and rig.preview.draft is None
    finally:
        monkeypatch.setattr(os, 'close', close)
        for fd in raw:
            try:
                fstat(fd)
            except OSError:
                continue
            close(fd)
        monkeypatch.setattr(os, 'open', opener)
        monkeypatch.setattr(os, 'fstat', fstat)
        monkeypatch.setattr(p, '_live_file', original_live)
        monkeypatch.setattr(p, '_route', original_route)
    fresh = rig.preview.build('save')
    assert fresh.snapshot_id != old.snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('phase', ['source', 'manifest', 'metadata-merge', 'merge-tool'])
@pytest.mark.parametrize('debt_kind', ['cleanup_errors', 'pending_children'])
def test_adjacent_preview_conversion_never_flattens_addressed_debt(rig, monkeypatch, phase, debt_kind):
    # Real resource cleanup is covered above and by persistent-child tests.
    # This matrix proves preservation across the remaining conversion seams.
    rig.remote({A: b'remote', B: b'base-b\n'})
    rig.live({A: b'live', B: b'base-b\n'})
    error = (p.Blocked if phase in ('manifest', 'metadata-merge') else OSError)('primary ' + phase)
    debt = ('exact fixture cleanup debt',) if debt_kind == 'cleanup_errors' else (object(),)
    setattr(error, debt_kind, debt)
    def fail(*args, **kwargs):
        raise error
    with monkeypatch.context() as patch:
        if phase == 'source':
            patch.setattr(rig.preview, 'observer', fail)
        elif phase == 'manifest':
            patch.setattr(m, 'decode', fail)
        elif phase == 'metadata-merge':
            patch.setattr(m, 'merge', fail)
        else:
            patch.setattr(rig.preview, '_git_tool', fail)
        with pytest.raises(type(error)) as raised:
            rig.preview.build('apply')
        assert raised.value is error and getattr(raised.value, debt_kind) is debt
        assert rig.preview.draft is None
    fresh = rig.preview.build('apply')
    assert fresh.snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('primary_debt', [False, True])
@pytest.mark.parametrize('caller_handling_error', [False, True])
def test_inventory_owned_root_close_debt_never_becomes_issue_or_masks_primary(rig, monkeypatch, primary_debt, caller_handling_error):
    import native_inventory as inventory_module
    original_root, original_directory = inventory_module._open_absolute_directory, inventory_module._directory_at
    close, fstat = os.close, os.fstat
    roots, attempts = [], []
    primary = OSError('primary inventory descendant debt')
    primary.cleanup_errors = ('exact descendant cleanup debt',)
    caller_error = OSError('unrelated caller handled error')
    def build():
        if not caller_handling_error:
            return rig.preview.build('save')
        try:
            raise caller_error
        except OSError:
            return rig.preview.build('save')
    def opened_root(*args, **kwargs):
        fd = original_root(*args, **kwargs)
        roots.append(fd)
        return fd
    def directory(*args, **kwargs):
        if primary_debt:
            raise primary
        return original_directory(*args, **kwargs)
    def closing(fd):
        if roots and fd == roots[0]:
            attempts.append(fd)
            raise OSError('owned root close failed before close')
        return close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(inventory_module, '_open_absolute_directory', opened_root)
        patch.setattr(inventory_module, '_directory_at', directory)
        patch.setattr(os, 'close', closing)
        try:
            with pytest.raises(OSError) as raised:
                build()
            if primary_debt:
                assert raised.value is primary
            assert 'inventory-fd-close:fd=%d' % roots[0] in str(raised.value.cleanup_errors)
            assert 'owned root close failed before close' in str(raised.value.cleanup_errors)
            assert attempts == [roots[0]] and rig.preview.draft is None
            assert not getattr(caller_error, 'cleanup_errors', ())
        finally:
            for fd in roots:
                try:
                    fstat(fd)
                except OSError:
                    continue
                close(fd)
    assert rig.preview.build('save').snapshot_id and not rig.preview.draft.selected


# Exact addressed Preview raw-fd cleanup regressions.
@pytest.mark.parametrize('primary_error',[False,True])
def test_preview_route_close_debt_addresses_its_live_fd(rig,monkeypatch,primary_error):
    put(rig.root,A,b'changed\n')
    opener,fstat,close=os.open,os.fstat,os.close
    raw=[];fired=[];close_fault=[]
    primary=OSError('primary live acquisition EIO')
    def opened(name,*a,**k):
        fd=opener(name,*a,**k)
        if os.fsencode(name)==b'a' and not a[0]&os.O_DIRECTORY:raw.append((fd,fstat(fd)))
        return fd
    def statfd(fd):
        if primary_error and raw and fd==raw[-1][0] and not fired:
            fired.append(1);raise primary
        return fstat(fd)
    def closing(fd):
        if raw and fd==raw[-1][0] and not close_fault:
            close_fault.append(fd);raise OSError('own route close EIO before close')
        return close(fd)
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'fstat',statfd);monkeypatch.setattr(os,'close',closing)
    try:
        with pytest.raises((p.PreviewFailure,OSError)) as raised:rig.preview.build('save')
        assert raw and close_fault and rig.preview.draft is None
        if primary_error:assert raised.value is primary
        fd,identity=raw[-1]
        assert (fstat(fd).st_dev,fstat(fd).st_ino)==(identity.st_dev,identity.st_ino)
        debt=getattr(raised.value,'cleanup_errors',())
        print(json.dumps({'primary_error':primary_error,'still_open_fd':fd,'exception_type':type(raised.value).__name__,'primary_identity':raised.value is primary,'cleanup_errors':debt}))
        assert any('fd=%d'%fd in item for item in debt), 'Owned leaking Preview fd is absent from the addressed cleanup debt'
    finally:
        monkeypatch.setattr(os,'open',opener);monkeypatch.setattr(os,'fstat',fstat);monkeypatch.setattr(os,'close',close)
        for fd,identity in raw:
            try:info=fstat(fd)
            except OSError:continue
            assert (info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino);close(fd)
    assert rig.preview.build('save').snapshot_id


@pytest.mark.parametrize('primary_error',[False,True])
def test_preview_scratch_close_debt_addresses_its_private_fd(rig,monkeypatch,primary_error):
    put(rig.root,A,b'changed\n')
    snapshot=rig.preview.build('save')
    enter=p._Scratch.__enter__
    close=os.close
    raw=[];attempts=[]
    primary=OSError('primary output sink failure')
    def owned(scratch):
        result=enter(scratch)
        raw.append((scratch.fd,os.fstat(scratch.fd)))
        return result
    def closing(fd):
        if raw and fd==raw[-1][0] and not attempts:
            attempts.append(fd);raise OSError('own private scratch close EIO before close')
        return close(fd)
    class Sink:
        def write(self,data):raise primary
    monkeypatch.setattr(p._Scratch,'__enter__',owned);monkeypatch.setattr(os,'close',closing)
    try:
        with pytest.raises((p.PreviewFailure,OSError)) as raised:
            rig.preview.view_file(snapshot.snapshot_id,A,Sink() if primary_error else io.BytesIO())
        if primary_error:assert raised.value is primary
        fd,identity=raw[-1]
        info=os.fstat(fd)
        assert (info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino)
        debt=getattr(raised.value,'cleanup_errors',())
        print(json.dumps({'scratch_primary_error':primary_error,'still_open_fd':fd,
            'exception_type':type(raised.value).__name__,'cleanup_errors':debt,'draft_cleared':rig.preview.draft is None}))
        assert any('fd=%d'%fd in item for item in debt), 'Owned leaking scratch fd is absent from addressed cleanup debt'
    finally:
        monkeypatch.setattr(p._Scratch,'__enter__',enter);monkeypatch.setattr(os,'close',close)
        for fd,identity in raw:
            try:info=os.fstat(fd)
            except OSError:continue
            assert (info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino);close(fd)



@pytest.mark.parametrize('resource', ['root', 'ancestor', 'leaf'])
@pytest.mark.parametrize('primary_error', [False, True])
@pytest.mark.parametrize('closed_first', [False, True])
def test_all_route_owners_have_exact_single_attempt_close_debt(rig, monkeypatch, resource, primary_error, closed_first):
    put(rig.root, A, b'changed\n')
    original_route, original_root = p._route, p._open_absolute_directory
    opener, fstat, close = os.open, os.fstat, os.close
    active, raw, attempts, fired = [], [], [], []
    primary = OSError('earlier route failure')
    primary.cleanup_errors = ('earlier addressed route debt',)
    @p.contextmanager
    def route(binding, path, kind, **kwargs):
        target = path == A
        if target: active.append(True)
        try:
            with original_route(binding, path, kind, **kwargs) as result: yield result
        finally:
            if target: active.pop()
    def root(*args, **kwargs):
        fd = original_root(*args, **kwargs)
        if active and resource == 'root': raw.append((fd, fstat(fd)))
        return fd
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if active and os.fsencode(name) == {'ancestor':b'homeassistant', 'leaf':b'a'}.get(resource):
            raw.append((fd, fstat(fd)))
        return fd
    def inspected(fd):
        if primary_error and raw and fd == raw[-1][0] and not fired:
            fired.append(True); raise primary
        return fstat(fd)
    def closing(fd):
        if active and raw and fd == raw[-1][0]:
            attempts.append(fd)
            if closed_first: close(fd)
            raise OSError('route close uncertainty')
        return close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(p, '_route', route); patch.setattr(p, '_open_absolute_directory', root)
        patch.setattr(os, 'open', opened); patch.setattr(os, 'fstat', inspected); patch.setattr(os, 'close', closing)
        try:
            with pytest.raises((p.PreviewFailure, OSError)) as raised: rig.preview.build('save')
            assert attempts == [raw[-1][0]] and rig.preview.draft is None
            assert any('fd=%d' % raw[-1][0] in debt for debt in raised.value.cleanup_errors)
            if primary_error:
                assert raised.value is primary and primary.cleanup_errors[0] == 'earlier addressed route debt'
            fd, identity = raw[-1]
            if closed_first:
                with pytest.raises(OSError): fstat(fd)
            else:
                assert (fstat(fd).st_dev, fstat(fd).st_ino) == (identity.st_dev, identity.st_ino)
        finally:
            for fd, identity in raw:
                try: info = fstat(fd)
                except OSError: continue
                assert (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
                close(fd)
    assert rig.preview.build('save').snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('resource', ['fd', 'rootfd'])
@pytest.mark.parametrize('primary_error', [False, True])
@pytest.mark.parametrize('closed_first', [False, True])
def test_all_scratch_directory_owners_have_addressed_close_uncertainty(rig, monkeypatch, resource, primary_error, closed_first):
    put(rig.root, A, b'changed\n')
    snapshot = rig.preview.build('save')
    enter, close, fstat = p._Scratch.__enter__, os.close, os.fstat
    raw, attempts = [], []
    primary = OSError('earlier scratch sink failure')
    primary.cleanup_errors = ('earlier addressed scratch debt',)
    def entered(scratch):
        result = enter(scratch); fd = getattr(scratch, resource)
        raw.append((fd, fstat(fd))); return result
    def closing(fd):
        if raw and fd == raw[-1][0]:
            attempts.append(fd)
            if closed_first: close(fd)
            raise OSError('scratch close uncertainty')
        return close(fd)
    class Sink:
        def write(self, data): raise primary
    with monkeypatch.context() as patch:
        patch.setattr(p._Scratch, '__enter__', entered); patch.setattr(os, 'close', closing)
        try:
            with pytest.raises((p.PreviewFailure, OSError)) as raised:
                rig.preview.view_file(snapshot.snapshot_id, A, Sink() if primary_error else io.BytesIO())
            fd, identity = raw[-1]
            assert attempts == [fd] and rig.preview.draft is None
            assert any('fd=%d' % fd in debt for debt in raised.value.cleanup_errors)
            if primary_error:
                assert raised.value is primary and primary.cleanup_errors[0] == 'earlier addressed scratch debt'
            if closed_first:
                with pytest.raises(OSError): fstat(fd)
            else:
                assert (fstat(fd).st_dev, fstat(fd).st_ino) == (identity.st_dev, identity.st_ino)
        finally:
            for fd, identity in raw:
                try: info = fstat(fd)
                except OSError: continue
                assert (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
                close(fd)
    assert rig.preview.build('save').snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('resource', ['writer', 'reader'])
@pytest.mark.parametrize('primary_error', [False, True])
@pytest.mark.parametrize('closed_first', [False, True])
def test_owned_stream_close_captures_fd_owner_and_all_existing_debt(rig, monkeypatch, resource, primary_error, closed_first):
    put(rig.root, A, b'changed\n')
    snapshot = rig.preview.build('save')
    opener, fdopen, fstat = os.open, os.fdopen, os.fstat
    raw, owners, attempts = [], [], []
    primary = OSError('earlier stream transfer failure')
    primary.cleanup_errors = ('earlier stream debt',)
    earlier_child, cleanup_child, earlier_stream = object(), object(), object()
    primary.pending_children = (earlier_child,)
    primary.pending_streams = (earlier_stream,)
    close_error = OSError('stream close uncertainty')
    close_error.cleanup_errors = ('earlier secondary cleanup debt',)
    close_error.pending_children = (cleanup_child,)
    class Owner:
        def __init__(self, real, fd): self.real, self.fd = real, fd
        def __getattr__(self, name): return getattr(self.real, name)
        def write(self, data):
            if primary_error and resource == 'writer': raise primary
            return self.real.write(data)
        def read(self, *args):
            if primary_error and resource == 'reader': raise primary
            return self.real.read(*args)
        def close(self):
            attempts.append(self.fd)
            if closed_first: self.real.close()
            close_error.pending_streams = (self,)
            raise close_error
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if ((resource == 'writer' and name == 'before') or
                (resource == 'reader' and name == 'patch' and not args[0] & os.O_CREAT)):
            raw.append((fd, fstat(fd)))
        return fd
    def wrapped(fd, mode='r', *args, **kwargs):
        real = fdopen(fd, mode, *args, **kwargs)
        if raw and fd == raw[-1][0] and mode == ('w+b' if resource == 'writer' else 'rb'):
            owner = Owner(real, fd); owners.append(owner); return owner
        return real
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', opened); patch.setattr(os, 'fdopen', wrapped)
        try:
            with pytest.raises(OSError) as raised:
                rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())
            error = raised.value
            assert error is (primary if primary_error else close_error)
            assert attempts == [raw[-1][0]] and rig.preview.draft is None
            assert any('stream-close:fd=%d' % raw[-1][0] in debt for debt in error.cleanup_errors)
            assert 'earlier secondary cleanup debt' in error.cleanup_errors
            assert error.pending_streams.count(owners[0]) == 1
            if primary_error:
                assert error.cleanup_errors[0] == 'earlier stream debt'
                assert error.pending_children == (earlier_child, cleanup_child)
                assert error.pending_streams == (earlier_stream, owners[0])
            else:
                assert error.pending_children == (cleanup_child,)
            fd, identity = raw[-1]
            if closed_first:
                with pytest.raises(OSError): fstat(fd)
            else:
                assert (fstat(fd).st_dev, fstat(fd).st_ino) == (identity.st_dev, identity.st_ino)
        finally:
            # Exact retained owner, not a raw fd retry or implicit destructor.
            for owner in owners: owner.real.close()
    assert rig.preview.build('save').snapshot_id and not rig.preview.draft.selected


@pytest.mark.parametrize('resource', ['writer', 'reader'])
@pytest.mark.parametrize('closed_first', [False, True])
def test_raw_stream_acquisition_close_preserves_exact_fd_and_primary_debt(rig, monkeypatch, resource, closed_first):
    put(rig.root, A, b'changed\n')
    snapshot = rig.preview.build('save')
    opener, fdopen, close, fstat = os.open, os.fdopen, os.close, os.fstat
    raw, attempts = [], []
    primary = OSError('raw fdopen allocation failure')
    primary.cleanup_errors = ('earlier raw allocation debt',)
    cleanup = OSError('raw close uncertainty')
    cleanup.cleanup_errors = ('earlier raw secondary debt',)
    def opened(name, *args, **kwargs):
        fd = opener(name, *args, **kwargs)
        if ((resource == 'writer' and name == 'before') or
                (resource == 'reader' and name == 'patch' and not args[0] & os.O_CREAT)):
            raw.append((fd, fstat(fd)))
        return fd
    def wrapped(fd, mode='r', *args, **kwargs):
        if raw and fd == raw[-1][0] and mode == ('w+b' if resource == 'writer' else 'rb'): raise primary
        return fdopen(fd, mode, *args, **kwargs)
    def closing(fd):
        if raw and fd == raw[-1][0]:
            attempts.append(fd)
            if closed_first: close(fd)
            raise cleanup
        return close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', opened); patch.setattr(os, 'fdopen', wrapped); patch.setattr(os, 'close', closing)
        try:
            with pytest.raises(OSError) as raised:
                rig.preview.view_file(snapshot.snapshot_id, A, io.BytesIO())
            assert raised.value is primary and primary.cleanup_errors[0] == 'earlier raw allocation debt'
            assert 'earlier raw secondary debt' in primary.cleanup_errors
            assert any('raw-fd-close:fd=%d' % raw[-1][0] in debt for debt in primary.cleanup_errors)
            assert attempts == [raw[-1][0]] and rig.preview.draft is None
            fd, identity = raw[-1]
            if closed_first:
                with pytest.raises(OSError): fstat(fd)
            else:
                assert (fstat(fd).st_dev, fstat(fd).st_ino) == (identity.st_dev, identity.st_ino)
        finally:
            for fd, identity in raw:
                try: info = fstat(fd)
                except OSError: continue
                assert (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
                close(fd)
    assert rig.preview.build('save').snapshot_id and not rig.preview.draft.selected
