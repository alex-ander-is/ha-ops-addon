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
from native_preview import NativePreview, SourceObservation, PathChange, ParentDetail, FileState, Stale, _route, CHUNK


def _digest(value):
    return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()


def _authority_snapshot(value):
    """Detached exact typed fields, including nested frozen dataclasses."""
    from dataclasses import fields, is_dataclass
    kind=type(value)
    if value is None or kind in (bytes,str,int,bool): return kind,value
    if is_dataclass(value) and not isinstance(value,type):
        return kind,tuple((field.name,_authority_snapshot(getattr(value,field.name))) for field in fields(value))
    if kind in (tuple,list): return kind,tuple(_authority_snapshot(item) for item in value)
    if kind in (set,frozenset): return kind,tuple(sorted((_authority_snapshot(item) for item in value),key=repr))
    if kind is dict: return kind,tuple(sorted(((_authority_snapshot(key),_authority_snapshot(item)) for key,item in value.items()),key=repr))
    raise Blocked('unsupported-publication-authority-field')


def _blob(payload, width):
    return hashlib.new('sha1' if width == 40 else 'sha256',
                       b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()


@dataclass(frozen=True)
class ScopePolicy:
    revision: int
    allowlist: tuple[bytes, ...]
    exclude_digest: str

    def __post_init__(self):
        from native_inventory import normalize_scope
        if (type(self.revision) is not int or self.revision < 0 or
                tuple(self.allowlist) != normalize_scope(self.allowlist) or
                not re.fullmatch('[0-9a-f]{64}', self.exclude_digest)):
            raise Blocked('exact-normalized-scope-policy-required')
        object.__setattr__(self, 'allowlist', tuple(self.allowlist))

    def document(self):
        from native_operations import encode_native_path
        return dict(revision=self.revision, allowlist=sorted(encode_native_path(p) for p in self.allowlist),
                    exclude_digest=self.exclude_digest)


@dataclass(frozen=True)
class HistoricalContext:
    path: bytes
    commit_oid: str | None
    tree_oid: str | None
    state: FileState | None
    parents: tuple[metadata.Record, ...]
    content: bytes | None
    current_present: bool | None = None

    def __post_init__(self):
        from native_inventory import path_bytes
        path_bytes(self.path)
        if self.current_present is not None and type(self.current_present) is not bool:
            raise Blocked('explicit-history-current-presence-required')
        object.__setattr__(self,'parents',tuple(self.parents))
        if self.commit_oid is None:
            if any(value is not None for value in (self.tree_oid,self.state,self.content)) or self.parents:
                raise Blocked('explicit-no-managed-history-required')
        elif (type(self.commit_oid) is not str or type(self.tree_oid) is not str or
                not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',self.commit_oid) or
                not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',self.tree_oid) or
                type(self.state) is not FileState or type(self.content) is not bytes):
            raise Blocked('exact-last-managed-history-required')
        if self.state is not None and (self.state.record is None or self.state.record.path!=self.path or
                self.state.record.oid!=self.state.oid or self.state.size!=len(self.content) or
                _blob(self.content,len(self.state.oid))!=self.state.oid or
                any(type(parent) is not metadata.Record or parent.kind!='directory' for parent in self.parents)):
            raise Blocked('last-managed-content-metadata-mismatch')


@dataclass(frozen=True)
class ScopeReview:
    draft: object
    selected: tuple[PathChange, ...]
    parents: tuple[ParentDetail, ...]
    blockers: tuple[str, ...]
    old: ScopePolicy
    new: ScopePolicy
    rules: bytes
    observation_digest: str
    observation_scope: tuple[bytes, ...]
    live_members: tuple[bytes, ...]
    history: tuple[HistoricalContext, ...]

    def __post_init__(self):
        from native_preview import Draft
        from native_inventory import normalize_scope
        if (type(self.draft) is not Draft or self.draft.direction != 'scope' or
                type(self.old) is not ScopePolicy or type(self.new) is not ScopePolicy or
                self.new.revision != self.old.revision + 1 or type(self.rules) is not bytes or
                not re.fullmatch('[0-9a-f]{64}', self.observation_digest) or
                self.observation_digest!=self.draft.source_digest or
                tuple(self.live_members)!=tuple(sorted(set(self.live_members))) or
                tuple(self.observation_scope) != normalize_scope(self.old.allowlist+self.new.allowlist)):
            raise Blocked('exact-scope-review-required')
        from native_inventory import path_bytes
        if any(type(row) is not HistoricalContext for row in self.history):raise Blocked('typed-last-managed-context-required')
        for path in self.live_members:path_bytes(path)
        for field in ('selected','parents','blockers','observation_scope','live_members','history'):
            object.__setattr__(self, field, tuple(getattr(self,field)))

    @property
    def ready(self):
        return not self.blockers


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
    scope_review: ScopeReview | None = None
    active_policy: ScopePolicy | None = None

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
        if self.active_policy is not None and (type(self.active_policy) is not ScopePolicy or
                self.scope_review is not None or self.active_policy.allowlist!=self.remote.scope or
                self.active_policy.exclude_digest!=self.binding.policy.digest):
            raise Blocked('exact-candidate-active-policy-required')
        if self.scope_review is not None and (type(self.scope_review) is not ScopeReview or self.scope_review.selected != self.selected):
            raise Blocked('exact-scope-candidate-required')
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
        self._publication_receipt = None
        self._scope_authority = None

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

    def build(self, review, *, active_policy=None):
        preview, disk = self.preview, self.disk
        if active_policy is not None and (type(active_policy) is not ScopePolicy or
                active_policy.exclude_digest!=preview.binding.policy.digest):
            raise Blocked('exact-active-save-policy-required')
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
                remote_kind, review.parents, active_policy=active_policy)
        if active_policy is not None and active_policy.allowlist!=result.remote.scope:
            raise Stale('active-save-membership-mismatch')
        self._install_publication(result,review,None)
        return result

    @contextmanager
    def publication_context(self, candidate, review, *, preserve_unconsumed=False):
        """One live issuer grant, consumed before effects; no closing old-ref fence."""
        receipt = self._publication_receipt
        preview = self.preview
        if (receipt is None or receipt[0] is not candidate or receipt[1] is not review or
                receipt[2] is not preview or receipt[3] is not self.disk or
                receipt[4] is not preview.git or receipt[5] is not preview.binding or
                receipt[6] is not preview.git.policy or receipt[7] != preview._generation or
                receipt[8] is not self._scope_authority):
            raise Stale('issued-current-candidate-and-review-required')
        if _authority_snapshot((candidate,review,preview.binding,preview.git.policy))!=self._publication_snapshot:
            raise Stale('issued-publication-authority-fields-changed')
        deferred=None
        with preview._call():
            if self._publication_receipt is not receipt or receipt[8] is not self._scope_authority:
                raise Stale('publication-grant-replaced-or-revoked')
            observed = self._publication_fence(candidate, review)
            # The caller may validate without consuming, but every accepted
            # operation clears this exact in-memory grant before its first effect.
            try:yield observed
            except BaseException as error:
                if not preserve_unconsumed or self._publication_receipt is not receipt:
                    raise
                # Transport/admission failed before candidate consumption.
                # Recheck the actual local selection while still serialized;
                # only an unchanged local review may retain its issuer grant.
                try:
                    self._publication_fence(candidate, review)
                    if _authority_snapshot((candidate,review,preview.binding,preview.git.policy))!=self._publication_snapshot:
                        raise Stale('issued-publication-authority-fields-changed')
                except BaseException as secondary:
                    error.cleanup_errors=tuple(getattr(error,'cleanup_errors',()))+('unconsumed-review-requalification:'+type(secondary).__name__,)
                    raise error
                deferred=error
        if deferred is not None:raise deferred

    def consume_publication(self, candidate, review):
        receipt = self._publication_receipt
        if receipt is None or receipt[0] is not candidate or receipt[1] is not review or receipt[8] is not self._scope_authority:
            raise Stale('publication-grant-already-consumed')
        if _authority_snapshot((candidate,review,self.preview.binding,self.preview.git.policy))!=self._publication_snapshot:
            raise Stale('issued-publication-authority-fields-changed')
        self._publication_fence(candidate, review)
        self._publication_receipt = None


    def _publication_fence(self, candidate, review):
        if self._scope_authority is not None:
            store,token=self._scope_authority
            if store.scope_authority()!=token:
                raise Stale('canonical-scope-lifecycle-expired')
        if candidate.scope_review is None:
            return self.preview._candidate_fence(review)
        if type(review) is not ScopeReview or candidate.scope_review is not review:
            raise Stale('issued-scope-review-required')
        source, width = self.preview._source()
        if source != candidate.remote:
            raise Stale('scope-source-changed')
        from dataclasses import replace
        observed = self.preview._observe_once(replace(source,scope=review.observation_scope),width,scope_absence=True)
        if observed.digest != review.observation_digest or tuple(path for path,_ in observed.live)!=review.live_members:
            raise Stale('whole-scope-membership-or-content-changed')
        from native_inventory import beneath
        includes=lambda path,scope:any(beneath(path,p) for p in scope)
        expected={path for path,_ in observed.live if includes(path,review.new.allowlist) and not includes(path,review.old.allowlist)}
        if {row.path for row in review.selected if row.after is not None}!=expected:
            raise Stale('complete-new-scope-membership-required')
        live=dict(observed.live)
        remote={entry.path:entry for entry in observed.remote}
        records=observed.remote_manifest.by_path
        for row in review.selected:
            prior=remote.get(row.path)
            before=None if prior is None else FileState(prior.oid,records.get(row.path))
            if row.before!=before or row.after is not None and row.after!=live.get(row.path):
                raise Stale('exact-scope-leaf-observation-required')
        fresh=self.preview._live_records(observed)
        if any(parent.after!=fresh.get(parent.path) for parent in review.parents):
            raise Stale('exact-scope-parent-observation-required')
        return observed

    def _issue_scope(self, candidate, review, store=None):
        if type(candidate) is not Candidate or type(review) is not ScopeReview or candidate.scope_review is not review:
            raise Blocked('exact-scope-issuance-required')
        if store is None:raise Stale("canonical-scope-issuer-required")
        self._revoke_publication()
        token=store.scope_authority()
        if token is None:raise Stale("canonical-pending-scope-required")
        self._scope_authority=(store,token)
        self._publication_fence(candidate,review)
        self._install_publication(candidate,review,self._scope_authority)
        return candidate


    def _bind_scope_authority(self,store):
        receipt=self._publication_receipt
        try:
            token=store.scope_authority()
            if token is None or receipt is None or receipt[0].scope_review is not None or receipt[8] is not None:
                raise Stale('fresh-ordinary-save-grant-required')
        except BaseException:
            self._revoke_publication()
            raise
        self._install_publication(receipt[0],receipt[1],(store,token))

    def _revoke_publication(self):
        # Remove authority to submit before changing any associated binding.
        self._publication_receipt=None
        self._publication_snapshot=None
        self._scope_authority=None

    def _install_publication(self,candidate,review,authority):
        self._revoke_publication()
        p=self.preview
        snapshot=_authority_snapshot((candidate,review,p.binding,p.git.policy))
        receipt=(candidate,review,p,self.disk,p.git,p.binding,p.git.policy,p._generation,authority)
        self._scope_authority=authority
        self._publication_snapshot=snapshot
        self._publication_receipt=receipt  # Publish the whole grant last.
