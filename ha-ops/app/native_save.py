"""Dormant selected HA-to-Git tree builder; no commit/ref/publication capability.

NativeSave requires the exact current issued ready Save Review. One Preview
session lock spans selected-only fences, one qualified disk snapshot/project,
private Git resources, full tree auditing and successful teardown. Unselected
live churn is outside the selection. Candidate is a diagnostic immutable handoff,
never replay authority. Its unreferenced Git objects may be garbage collected.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import io
import os
import stat
import re
import uuid

from native_disk import NativeDisk, Evidence, InsufficientSpace, Blocked as DiskBlocked
from native_git import IndexEntry, TreeEntry
from native_inventory import Blocked
import native_metadata as metadata
from native_preview import NativePreview, SourceObservation, PathChange, ParentDetail, Stale, _route, CHUNK


def _digest(value):
    return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()


def _blob(payload, width):
    return hashlib.new('sha1' if width == 40 else 'sha256',
                       b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()


@dataclass(frozen=True)
class Candidate:
    tree_oid: str
    object_format: str
    remote: SourceObservation
    baseline_tree_oid: str
    binding: metadata.Binding
    snapshot_id: str
    generation: int
    selected: tuple[PathChange, ...]
    approved_parents: tuple[bytes, ...]
    reviewed_parents: tuple[metadata.ParentReview, ...]
    parent_dependencies: tuple[tuple[bytes, metadata.Record | None], ...]
    routes: tuple
    manifest_oid: str
    manifest_digest: str
    selection_digest: str
    candidate_digest: str
    remote_object_kind: bytes
    issued_parent_reviews: tuple[ParentDetail, ...]

    def __post_init__(self):
        from dataclasses import replace
        if (self.object_format not in ('sha1', 'sha256') or type(self.remote) is not SourceObservation or
                type(self.binding) is not metadata.Binding or type(self.generation) is not int or
                self.generation < 1 or type(self.snapshot_id) is not str or
                type(self.remote_object_kind) is not bytes or
                self.remote_object_kind not in (b'commit', b'tree')):
            raise Blocked('typed-candidate-identity-required')
        width = 40 if self.object_format == 'sha1' else 64
        for value in (self.tree_oid, self.baseline_tree_oid, self.manifest_oid):
            if type(value) is not str or not re.fullmatch('[0-9a-f]{%d}' % width, value) or not value.strip('0'):
                raise Blocked('exact-candidate-object-id-required')
        for value in (self.manifest_digest, self.selection_digest, self.candidate_digest):
            if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
                raise Blocked('exact-candidate-digest-required')
        selected = tuple(self.selected)
        if any(type(row) is not PathChange for row in selected):
            raise Blocked('typed-candidate-selection-required')
        object.__setattr__(self, 'selected', tuple(replace(row, issues=tuple(row.issues),
                          parents=tuple(row.parents)) for row in selected))
        object.__setattr__(self, 'approved_parents', tuple(self.approved_parents))
        final = tuple(self.reviewed_parents)
        if any(type(row) is not metadata.ParentReview for row in final):
            raise Blocked('typed-final-parent-review-required')
        object.__setattr__(self, 'reviewed_parents', tuple(replace(row,
            dependents=tuple(row.dependents)) for row in final))
        issued = tuple(self.issued_parent_reviews)
        if any(type(row) is not ParentDetail for row in issued):
            raise Blocked('typed-issued-parent-review-required')
        object.__setattr__(self, 'issued_parent_reviews', tuple(replace(row,
            dependents=tuple(row.dependents), issues=tuple(row.issues)) for row in issued))
        object.__setattr__(self, 'parent_dependencies', tuple((path, record) for path, record in self.parent_dependencies))
        object.__setattr__(self, 'routes', tuple((path, tuple(identity)) for path, identity in self.routes))


class NativeSave:
    def __init__(self, preview, disk):
        # No allocation, observation, replay or recovery on construction.
        if not isinstance(preview, NativePreview) or type(disk) is not NativeDisk:
            raise Blocked('native-preview-and-disk-required')
        self.preview, self.disk = preview, disk

    @contextmanager
    def _stream(self, row, observed):
        """Bounded exact live bytes under selected descriptor/namespace fences."""
        expected = row.after
        with _route(self.preview.binding, row.path, 'file') as (fd, routes):
            before = os.fstat(fd)
            record = metadata.Record(row.path, 'file', before.st_uid, before.st_gid,
                                     stat.S_IMODE(before.st_mode), expected.oid)
            if (record != expected.record or before.st_size != expected.size or
                    any(dict(observed.routes).get(p) != v for p, v in routes)):
                raise Stale('selected-stream-acquisition-changed')
            count, completed = 0, False
            def chunks():
                nonlocal count, completed
                while count < expected.size:
                    if metadata._stat(before) != metadata._stat(os.fstat(fd)):
                        raise Stale('selected-stream-changed-or-short')
                    data = os.read(fd, min(CHUNK, expected.size - count))
                    if (not data or len(data) > expected.size - count or
                            metadata._stat(before) != metadata._stat(os.fstat(fd))):
                        raise Stale('selected-stream-changed-or-short')
                    count += len(data)
                    yield data
                if os.read(fd, 1) or count != expected.size or metadata._stat(before) != metadata._stat(os.fstat(fd)):
                    raise Stale('selected-stream-changed-or-short')
                completed = True
            yield chunks()
            if not completed or metadata._stat(before) != metadata._stat(os.fstat(fd)):
                raise Stale('selected-stream-not-complete-or-changed')

    def build(self, review):
        preview, disk = self.preview, self.disk
        with preview.candidate_context(review) as observed:
            git, binding = preview.git, preview.binding
            if disk.root != binding.root or git.policy.repository_files != (metadata.MANIFEST_PATH,):
                raise Blocked('exact-native-root-and-manifest-policy-required')
            width = git._config()
            remote_kind, baseline_tree = git.resolve_tree_source(observed.source.remote_oid)
            remote = git.tree_entries(baseline_tree)
            issues = []
            baseline = preview._manifest(remote, width, 'remote', issues)
            if baseline is None or issues or baseline != observed.remote_manifest:
                raise Stale('complete-remote-manifest-required')
            records = baseline.by_path
            for row in review.selected:
                if row.after is None:
                    records.pop(row.path, None)
                else:
                    records[row.path] = row.after.record
            fresh = preview._live_records(observed)
            files = {p for p, r in records.items() if r.kind == 'file'}
            necessary = metadata.ancestors(files)
            surviving_approvals = tuple(p for p in review.draft.approved_parents
                                       if p in necessary and fresh.get(p) is not None)
            for path in surviving_approvals:
                records[path] = fresh[path]
            # Reviewed absence removes no surviving remote directory record:
            # unselected remote leaves still require its original attributes.
            records = {p:r for p,r in records.items() if r.kind == 'file' or p in necessary}
            hybrid = metadata.Manifest(tuple(records[p] for p in sorted(records)))
            selection = metadata.select(baseline, hybrid, review.draft.selected,
                approved_parents=surviving_approvals, width=width, policy=git.policy)
            payload = metadata.encode(selection.manifest, width=width, policy=git.policy)
            manifest_oid = _blob(payload, width)
            expected = {e.path:e for e in remote}
            entries, removals, sources = [], [], []
            for row in review.selected:
                if row.after is None:
                    removals.append(row.path)
                    expected.pop(row.path)
                else:
                    mode = b'100755' if row.after.record.mode & 0o100 else b'100644'
                    entry = IndexEntry(row.path, mode, row.after.oid, 0)
                    entries.append(entry)
                    expected[row.path] = TreeEntry(row.path, mode, row.after.oid, b'blob')
                    sources.append((row.path, disk.source(binding.root + b'/' + row.path)))
            expected[metadata.MANIFEST_PATH] = TreeEntry(metadata.MANIFEST_PATH, b'100644', manifest_oid, b'blob')
            expected = tuple(expected[p] for p in sorted(expected))
            metadata.validate(selection.manifest, expected, width=width, policy=git.policy)
            operation_id, owner_id = str(uuid.uuid4()), str(uuid.uuid4())
            evidence = Evidence(operation_id, owner_id, selected=tuple(sources), base_tree=baseline_tree,
                candidate_manifest=payload, candidate_removals=tuple(removals),
                candidate_final=tuple(e.path for e in expected))
            snapshot = disk.snapshot()
            plan = disk.project('candidate', snapshot, evidence)
            if not plan.supported or not plan.decisions:
                raise DiskBlocked('cannot-establish-admission:' + repr(plan.unknown))
            if any(decision.status == 'insufficient-space' for decision in plan.decisions):
                raise InsufficientSpace('insufficient-space:' + repr(plan.decisions))
            if any(decision.status != 'fits' for decision in plan.decisions):
                raise DiskBlocked('cannot-establish-admission:' + repr(plan.decisions))
            preview._candidate_fence(review)
            with disk.guard(plan) as admission:
                with git.isolated_index(baseline_tree, operation_id=operation_id, owner_id=owner_id,
                                        admission=admission) as handle:
                    for row in review.selected:
                        if row.after is not None:
                            with self._stream(row, observed) as stream:
                                result = git.write_blob_stream(stream, operation_id=operation_id,
                                    owner_id=owner_id, admission=admission)
                                if (result.oid, result.size) != (row.after.oid, row.after.size):
                                    raise Stale('selected-blob-identity-mismatch')
                            current_source, current_width = preview._source()
                            if current_source != observed.source or current_width != width:
                                raise Stale('candidate-source-changed-after-selected-stream')
                    tree = metadata.stage_candidate(git, handle, selection, entries=entries,
                        removals=removals, width=width, admission=admission)
                    if git.tree_entries(tree) != expected:
                        raise Blocked('complete-candidate-tree-mismatch')
                    sink = io.BytesIO()
                    git.copy_blob(manifest_oid, sink)
                    if sink.getvalue() != payload:
                        raise Blocked('complete-candidate-manifest-mismatch')
                    metadata.validate(metadata.decode(sink.getvalue(), width=width, policy=git.policy),
                                      expected, width=width, policy=git.policy)
                    preview._candidate_fence(review)
                    admission.revalidate()
                # Teardown is part of validity, while held authority still exists.
                admission.revalidate()
            paths = set(review.draft.selected) | metadata.ancestors(review.draft.selected) | {b''}
            routes = tuple((p,v) for p,v in observed.routes if p in paths)
            parents = tuple((p, fresh.get(p)) for p in sorted(paths - set(review.draft.selected)))
            selected_digest = _digest((observed.source, remote_kind, binding, review.draft, review.selected,
                                       review.parents, selection.parents, parents, routes, manifest_oid))
            result = Candidate(tree, 'sha1' if width == 40 else 'sha256', observed.source, baseline_tree,
                binding, review.draft.snapshot_id, review.draft.generation, review.selected,
                review.draft.approved_parents, selection.parents, parents, routes, manifest_oid,
                hashlib.sha256(payload).hexdigest(), selected_digest,
                _digest((tree, baseline_tree, remote_kind, selected_digest, manifest_oid)),
                remote_kind, review.parents)
        return result
