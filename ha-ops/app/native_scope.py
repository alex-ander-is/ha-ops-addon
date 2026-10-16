"""Dormant folder-scope transaction API. Never imported by production entrypoints.

All entrypoints are explicit. Constructors and diagnostics do not fetch, publish,
rebuild, restore selections or touch live data. Durable review artifacts contain
complete candidate blobs; Git garbage collection cannot destroy reviewed bytes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import os
import stat
import threading
import uuid

import native_metadata as metadata
import native_inventory as inventory
import native_preview as preview
import native_save as save
from native_disk import NativeDisk, Evidence, InsufficientSpace, Blocked as DiskBlocked
from native_git import IndexEntry, TreeEntry
from native_operations import (NativeOperationStore, canonical, digest, encode_native_path,
                               decode_native_path, Conflict, _parse, _publication_observe)


ContentSink=save.ContentSink


def _git_ref(git,oid):
    # copy_blob already verifies Git identity and declared length. The first
    # pass counts without a Git header; the second derives the raw/Git receipt.
    class Count:
        size=0
        def write(self,chunk):self.size+=len(chunk);return len(chunk)
    count=Count();git.copy_blob(oid,count)
    sink=ContentSink(count.size,'sha1' if len(oid)==40 else 'sha256');git.copy_blob(oid,sink)
    ref=sink.reference()
    if ref.oid!=oid:raise Conflict('git-content-identity-mismatch')
    return ref


def _inside(path,scope):
    return any(inventory.beneath(path,p) for p in scope)


def _policy(document):
    return save.ScopePolicy(document['revision'],inventory.normalize_scope(
        decode_native_path(p) for p in document['allowlist']),document['exclude_digest'])


def _tree(entries,width):
    """Exact Git tree identity from complete independently known leaf entries."""
    nodes={b'':{}}
    for e in entries:
        parts=e.path.split(b'/')
        for n,part in enumerate(parts):
            parent=b'/'.join(parts[:n]);path=b'/'.join(parts[:n+1])
            if n==len(parts)-1:
                if part in nodes[parent]:raise inventory.Blocked('scope-file-parent-collision')
                nodes[parent][part]=(e.mode,e.oid)
            else:
                nodes[parent].setdefault(part,(b'40000',path));nodes.setdefault(path,{})
    def build(path):
        rows=[]
        for name,(mode,value) in nodes[path].items():
            directory=mode==b'40000';oid=build(value) if directory else value
            rows.append((name+b'/' if directory else name,mode+b' '+name+b'\0'+bytes.fromhex(oid)))
        raw=b''.join(raw for _,raw in sorted(rows))
        return hashlib.new('sha1' if width==40 else 'sha256',b'tree '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    return build(b'')


def rules_bytes(files,policy,live_policy):
    # Render through the qualified literal grammar, with the fixed manifest
    # exception and the scope identity in canonical comment bytes.
    from types import SimpleNamespace
    rows=inventory.render_ignore(SimpleNamespace(blockers=(),managed_files=tuple(files)+(metadata.MANIFEST_PATH,)),policy=live_policy)
    return rows+b'# ha-ops-scope '+canonical(policy.document())+b'\n'


def reconstruct(git,review,baseline,remote):
    """Independent R + whole-scope delta; unchanged remote values survive."""
    width=git._config();old,new=review.old.allowlist,review.new.allowlist
    records=baseline.by_path;expected={e.path:e for e in remote}
    remote_files={p for p,r in records.items() if r.kind=='file'}
    removed={p for p in remote_files if not _inside(p,new) or
             not _inside(p,old) and p not in review.live_members}
    selected={r.path:r for r in review.selected}
    newly_included={p for p in review.live_members if _inside(p,new) and not _inside(p,old)}
    if {p for p,r in selected.items() if r.after is not None}!=newly_included:
        raise inventory.Blocked('complete-new-scope-membership-required')
    if {p for p,r in selected.items() if r.after is None}!=removed:
        raise inventory.Blocked('complete-scope-removal-set-required')
    for path,row in selected.items():
        if row.after is None:
            records.pop(path,None);expected.pop(path,None)
        else:
            if not _inside(path,new) or _inside(path,old) or row.after.record is None:
                raise inventory.Blocked('scope-inclusion-membership-mismatch')
            records[path]=row.after.record
            mode=b'100755' if row.after.record.mode&0o100 else b'100644'
            expected[path]=TreeEntry(path,mode,row.after.oid,b'blob')
    required=metadata.ancestors(p for p,r in records.items() if r.kind=='file')
    fresh={p:r.after for p,r in ((p,pd) for pd in review.parents for p in (pd.path,))}
    for path in review.draft.approved_parents:
        if path in required and fresh.get(path) is not None:records[path]=fresh[path]
    records={p:r for p,r in records.items() if r.kind=='file' or p in required}
    hybrid=metadata.Manifest(tuple(records[p] for p in sorted(records)))
    selection=metadata.select(baseline,hybrid,review.draft.selected,
        approved_parents=review.draft.approved_parents,width=width,policy=git.policy)
    payload=metadata.encode(selection.manifest,width=width,policy=git.policy)
    oid=save._blob(payload,width)
    expected[metadata.MANIFEST_PATH]=TreeEntry(metadata.MANIFEST_PATH,b'100644',oid,b'blob')
    rule_files={p for p,r in records.items() if r.kind=='file'}
    rules=rules_bytes(rule_files,review.new,git.policy)
    if rules!=review.rules:raise inventory.Blocked('exact-generated-scope-rules-mismatch')
    expected[b'.gitignore']=TreeEntry(b'.gitignore',b'100644',save._blob(rules,width),b'blob')
    return selection,payload,tuple(expected[p] for p in sorted(expected))


def last_managed_context(git,tip,paths):
    """Nearest managed ancestors of authoritative R, including merge peers.

    These bytes are comparison context only. They never enter the proposed tree.
    Every observation uses NativeGit's exact protected object readers.
    """
    unresolved=set(paths);frontier=(tip,);seen=set();result=[];width=git._config()
    while frontier and unresolved:
        level=[];next_level=[]
        for oid in frontier:
            if oid in seen:continue
            if len(seen)>=10000:raise inventory.Blocked('scope-history-observation-budget-exhausted')
            seen.add(oid);raw,tree,parents=git.exact_commit(oid)
            entries=git.tree_entries(tree);by_path={entry.path:entry for entry in entries}
            relevant=unresolved.intersection(by_path)
            if relevant:
                entry=by_path.get(metadata.MANIFEST_PATH)
                if entry is None:raise inventory.Blocked('last-managed-metadata-unavailable')
                sink=io.BytesIO();git.copy_blob(entry.oid,sink)
                manifest=metadata.decode(sink.getvalue(),width=width,policy=git.policy)
                metadata.validate(manifest,entries,width=width,policy=git.policy)
                records=manifest.by_path
                for path in sorted(relevant):
                    record=records.get(path)
                    if record is None or record.kind!='file':raise inventory.Blocked('last-managed-leaf-metadata-unavailable')
                    content=_git_ref(git,by_path[path].oid)
                    state=preview.FileState(by_path[path].oid,record,content.size,content.binary)
                    level.append(save.HistoricalContext(path,oid,tree,state,
                        tuple(records[parent] for parent in sorted(metadata.ancestors((path,)))),content))
            next_level.extend(parents)
        result.extend(level);unresolved.difference_update(row.path for row in level)
        frontier=tuple(dict.fromkeys(next_level))
    result.extend(save.HistoricalContext(path,None,None,None,(),None) for path in sorted(unresolved))
    return tuple(sorted(result,key=lambda row:(row.path,row.commit_oid or '')))


def scope_history_context(git,tip,old,new,current):
    """Complete historical leaf universe of the newly included folder region."""
    current=set(current);paths=set(current)
    if not paths and all(_inside(root,old) for root in new):return ()
    pending=[tip];seen=set();width=git._config()
    while pending:
        oid=pending.pop()
        if oid in seen:continue
        if len(seen)>=10000:raise inventory.Blocked('scope-history-observation-budget-exhausted')
        seen.add(oid);raw,tree,parents=git.exact_commit(oid)
        entries=git.tree_entries(tree)
        eligible={e.path for e in entries if _inside(e.path,new) and not _inside(e.path,old) and
                  git.policy.reason(e.path) is None and e.path not in (b'.gitignore',metadata.MANIFEST_PATH)}
        if eligible:
            entry=next((e for e in entries if e.path==metadata.MANIFEST_PATH),None)
            if entry is None:raise inventory.Blocked('last-managed-metadata-unavailable')
            sink=io.BytesIO();git.copy_blob(entry.oid,sink)
            manifest=metadata.decode(sink.getvalue(),width=width,policy=git.policy)
            metadata.validate(manifest,entries,width=width,policy=git.policy)
            paths.update(p for p in eligible if manifest.by_path[p].kind=='file')
        pending.extend(parents)
    return tuple(replace(row,current_present=row.path in current) for row in last_managed_context(git,tip,paths))


class NativeScope:
    def __init__(self,saver,store):
        if type(saver) is not save.NativeSave or not isinstance(store,NativeOperationStore):
            raise inventory.Blocked('exact-native-scope-peers-required')
        if os.fsencode(store.data_root)!=saver.preview.binding.root+b'/data' or saver.disk.root!=saver.preview.binding.root:
            raise inventory.Blocked('fixed-canonical-scope-root-required')
        self.saver,self.store=saver,store
        self._peers=(saver,store,saver.preview,saver.disk,saver.preview.git,saver.preview.binding)
        self._lock=threading.Lock();self._issued=None;self._apply=None;self._apply_generation=0
        self._last_save=None

    @contextmanager
    def _call(self):
        if not self._lock.acquire(blocking=False):raise Conflict('scope-session-busy')
        try:
            p=self.saver.preview
            if self._peers!=(self.saver,self.store,p,self.saver.disk,p.git,p.binding):
                raise Conflict('scope-peer-binding-changed')
            yield
        finally:self._lock.release()

    def diagnose(self):
        # Unlike store.read(), a diagnostic does not initialize missing state.
        with _publication_observe(self.store) as (root,artifacts):
            if root is None:return dict(transaction=None,apply_blocked=False,draft={})
            state=_parse(self.store._read_file(root,'state.json'));self.store._validate(state,artifacts)
            pending=state.get('scope_pending')
            return dict(transaction=pending,revision=state['revision'],active_policy=state['active_scope'],
                apply_blocked=bool(pending or state['active'] or state['recovery']),draft={})

    def active_policy(self):
        return self.diagnose().get('active_policy')

    def active_rules(self):
        import native_publication as publication
        with _publication_observe(self.store) as (root,artifacts):
            if root is None:return None
            state=_parse(self.store._read_file(root,'state.json'));self.store._validate(state,artifacts)
            key=state.get('scope_rules_record_id')
            if key is not None:
                record=state['records'][key]
                intent=publication.validate_intent(self.store._artifact(artifacts,record['artifact'],record['artifact_size']))
                return intent.candidate.scope_review.rules
            # The original native baseline provenance is immutable; ordinary
            # Save preserves its rule bytes until the first scope publication.
            oid=state['baseline_provenance']['identity']['refs']['remote']['oid']
            entry=next((e for e in self.saver.preview.git.tree_entries(oid) if e.path==b'.gitignore'),None)
            if entry is None:return b''
            sink=io.BytesIO();self.saver.preview.git.copy_blob(entry.oid,sink)
            return sink.getvalue()

    def _old(self,state,source):
        if state['active_scope'] is None:raise Conflict('established-native-baseline-required')
        old=_policy(state['active_scope'])
        if old.allowlist!=source.scope or old.exclude_digest!=self.saver.preview.binding.policy.digest:
            raise Conflict('current-source-policy-disagrees-with-canonical-baseline')
        return old

    def build(self,proposed,*,approved_parents=(),expected_revision,transaction_id=None):
        with self._call(),self.saver.preview._call():
            p=self.saver.preview;git=p.git;disk=self.saver.disk
            with _publication_observe(self.store) as (root,artifacts):
                if root is None:raise Conflict('established-native-baseline-required')
                state=_parse(self.store._read_file(root,'state.json'));self.store._validate(state,artifacts)
            self.store._cas(state,expected_revision)
            if state.get('scope_pending') is not None:self.store.scope_authority()
            self.saver._revoke_publication()
            source,width=p._source();old=self._old(state,source)
            if git.resolve_tree_source(source.remote_oid)[0]!=b'commit':raise Conflict('scope-requires-exact-remote-commit')
            new=save.ScopePolicy(old.revision+1,inventory.normalize_scope(proposed),old.exclude_digest)
            for path in new.allowlist:
                if p.binding.policy.reason(path):raise inventory.Blocked('prohibited-scope')
            union=inventory.normalize_scope(old.allowlist+new.allowlist)
            combined=replace(source,scope=union)
            observed=p._observe_once(combined,width,scope_absence=True)
            again=p._observe_once(combined,width,scope_absence=True)
            if observed.digest!=again.digest or p._source()!=(source,width):raise preview.Stale('scope-observation-changed')
            if observed.issues or observed.remote_manifest is None:raise inventory.Blocked('complete-safe-scope-observation-required:'+repr(observed.issues))
            live=dict(observed.live);baseline=observed.remote_manifest
            remote=observed.remote;remote_states={e.path:preview.FileState(e.oid,baseline.by_path.get(e.path)) for e in remote}
            removed={r.path for r in baseline.entries if r.kind=='file' and (not _inside(r.path,new.allowlist) or
                     not _inside(r.path,old.allowlist) and r.path not in live)}
            added={path for path in live if _inside(path,new.allowlist) and not _inside(path,old.allowlist)}
            history=scope_history_context(git,source.remote_oid,old.allowlist,new.allowlist,added)
            historical={path:[row for row in history if row.path==path and row.state is not None] for path in added}
            rows=[]
            for path in sorted(removed|added):
                before=remote_states.get(path);after=None if path in removed else live[path]
                context=historical.get(path,())
                base=context[0].state if len(context)==1 else None
                rows.append(preview.PathChange(path,before,after,base,'removed' if after is None else 'added',
                    before is None or before.record!=after.record if after else True,'none',(),tuple(sorted(metadata.ancestors((path,)))),True))
            rows=tuple(rows);records=baseline.by_path;fresh=p._live_records(observed)
            finalfiles=({r.path for r in baseline.entries if r.kind=='file'}-removed)|added
            necessary=metadata.ancestors(finalfiles)
            # Existing shared parents retain remote attributes; new directories
            # are explicit review dependencies and require their own approval.
            required=tuple(sorted(path for path in necessary if path not in records))
            approvals=tuple(sorted(set(approved_parents)))
            if set(approvals)!=set(required):raise inventory.Blocked('explicit-new-parent-approval-required:'+repr(required))
            parents=tuple(preview.ParentDetail(path,records.get(path),fresh.get(path),
                tuple(sorted(q for q in finalfiles if q.startswith(path+b'/')))) for path in required)
            if any(row.after is None for row in parents):raise inventory.Blocked('complete-new-parent-metadata-required')
            rules=rules_bytes(finalfiles,new,git.policy)
            generation=state['generation']+1;sid=str(uuid.uuid4())
            draft=preview.Draft(sid,observed.digest,'scope',generation,tuple(r.path for r in rows),approvals)
            review=save.ScopeReview(draft,rows,parents,(),old,new,rules,observed.digest,union,tuple(path for path,_ in observed.live),history)
            selection,payload,expected=reconstruct(git,review,baseline,remote)
            tree=_tree(expected,width);manifest_oid=save._blob(payload,width)
            paths=set(draft.selected)|metadata.ancestors(draft.selected)|{b''}
            routes=tuple((path,value) for path,value in observed.routes if path in paths)
            dependencies=tuple((path,fresh.get(path)) for path in sorted(paths-set(draft.selected)))
            sd=save._digest((source,b'commit',p.binding,draft,rows,parents,selection.parents,dependencies,routes,manifest_oid,old,new,rules))
            candidate=save.Candidate(tree,'sha1' if width==40 else 'sha256',source,
                git.resolve_tree_source(source.remote_oid)[1],p.binding,sid,generation,rows,approvals,
                selection.parents,dependencies,routes,manifest_oid,hashlib.sha256(payload).hexdigest(),sd,
                save._digest((tree,git.resolve_tree_source(source.remote_oid)[1],b'commit',sd,manifest_oid)),b'commit',parents,review)
            # Complete durable diff exposes every blob, both metadata manifests,
            # exact scope/rules, and excluded unsaved bytes in a separate section.
            blobs={};excluded=[];before_blobs=[];producers={}
            def remember(ref,producer):
                previous=producers.setdefault(ref.key,(ref,producer))
                if previous[0]!=ref:raise Conflict('conflicting-content-receipt')
                return ref
            def git_content(oid):
                ref=_git_ref(git,oid)
                return remember(ref,lambda sink,oid=oid:git.copy_blob(oid,sink))
            def live_content(path,state):
                def produce(sink):
                    current,_=preview._live_file(p.binding,path,width,sink)
                    if current!=state:raise preview.Stale('scope-live-copy-changed')
                sink=ContentSink(state.size,candidate.object_format);produce(sink)
                return remember(sink.reference(),produce)
            def generated(data):
                sink=ContentSink(len(data),candidate.object_format);sink.write(data)
                return remember(sink.reference(),lambda sink,data=data:sink.write(data))
            for entry in expected:
                if entry.path==metadata.MANIFEST_PATH:ref=generated(payload)
                elif entry.path==b'.gitignore':ref=generated(rules)
                elif entry.path in added:ref=live_content(entry.path,live[entry.path])
                else:ref=git_content(entry.oid)
                blobs[entry.path]=ref
            for path in sorted(removed):
                before_blobs.append((path,remote_states[path],git_content(remote_states[path].oid)))
            excluded_paths={path for path in live if _inside(path,old.allowlist) and not _inside(path,new.allowlist)}|removed
            for path in sorted(excluded_paths):
                actual=live.get(path);prior=remote_states.get(path)
                if actual is not None and (prior is None or (actual.oid,actual.record)!=(prior.oid,prior.record)):
                    excluded.append((path,actual,live_content(path,actual)))
                elif actual is None:excluded.append((path,None,None))
            baseline_blobs=[(entry.path,git_content(entry.oid)) for entry in remote]
            for row in history:
                if row.content is not None:
                    remember(row.content,lambda sink,oid=row.content.oid:git.copy_blob(oid,sink))
            baseline_missing=tuple(path for path in (b'.gitignore',metadata.MANIFEST_PATH) if path not in {entry.path for entry in remote})
            import native_publication as publication
            artifact=canonical(dict(profile='native-scope-review-v2',candidate=publication._encode(candidate),
                review=publication._encode(review),blobs=publication._encode(tuple(sorted(blobs.items()))),
                excluded_unsaved=publication._encode(tuple(excluded)),removed_before=publication._encode(tuple(before_blobs)),baseline_blobs=publication._encode(tuple(baseline_blobs)),
                baseline_missing=publication._encode(baseline_missing)))
            identity=publication._candidate_identity(candidate,source.remote_oid,b'/scope-review','refs/heads/scope-review',source.base_ref,source.base_oid)
            identity['execution_policy']='native-scope-v2'
            identity['effect']['after']=digest([candidate.candidate_digest,hashlib.sha256(artifact).hexdigest()])
            transaction_id=transaction_id or (state.get('scope_pending') or {}).get('id')
            prepared=self.store.prepare_scope(expected_revision,identity,artifact,candidate.candidate_digest,transaction_id,payloads=tuple(producers[key] for key in sorted(producers)))
            sources=tuple((path,disk.source(p.binding.root+b'/'+path)) for path in sorted(added))
            evidence=Evidence(prepared.operation_id,prepared.owner_id,selected=sources,base_tree=candidate.baseline_tree_oid,
                candidate_manifest=payload,candidate_removals=tuple(sorted(removed)),candidate_final=tuple(e.path for e in expected),
                scope_preparation=prepared,candidate_rules=rules)
            snapshot=disk.snapshot();plan=disk.project('candidate',snapshot,evidence)
            if not plan.supported or not plan.decisions:raise DiskBlocked('scope-capacity-unknown:'+repr(plan.unknown))
            if any(row.status=='insufficient-space' for row in plan.decisions):raise InsufficientSpace('scope-capacity-insufficient')
            if any(row.status!='fits' for row in plan.decisions):raise DiskBlocked('scope-capacity-unknown')
            self.saver._publication_fence(candidate,review)
            with disk.guard(plan) as admission:
                admission.create_scope_directories(prepared)
                with self.store.mutex,_publication_observe(self.store,scope_execution=(prepared,admission)):
                    self.store.install_scope_blobs(prepared,admission)
                    self._materialize(candidate,review,selection,blobs,admission,prepared.operation_id,prepared.owner_id)
                    self.saver._publication_fence(candidate,review)
                    self.store.accept_scope(prepared,admission)
            self.saver._issue_scope(candidate,review,self.store);self._issued=(candidate,review)
            return candidate,review

    def _materialize(self,candidate,review,selection,blobs,admission,operation_id,owner_id):
        git=self.saver.preview.git;width=40 if candidate.object_format=='sha1' else 64
        with git.isolated_index(candidate.baseline_tree_oid,operation_id=operation_id,owner_id=owner_id,admission=admission) as handle:
            entries=[]
            with _publication_observe(self.store) as (_,directory):
                for path,ref in sorted(blobs.items()):
                    if path not in {row.path for row in review.selected if row.after is not None}|{metadata.MANIFEST_PATH,b'.gitignore'}:continue
                    with self.store._verified_scope_blob(directory,ref) as fd:
                        with os.fdopen(os.dup(fd),'rb') as source:
                            result=git.write_blob_stream(source,operation_id=operation_id,owner_id=owner_id,admission=admission)
                    if result.oid!=ref.oid:raise preview.Stale('scope-blob-mismatch')
                    record=selection.manifest.by_path.get(path)
                    mode=b'100755' if record is not None and record.mode&0o100 else b'100644'
                    entries.append(IndexEntry(path,mode,result.oid,0))
            removals=tuple(row.path for row in review.selected if row.after is None)
            git.update_index(handle,entries,removals=removals)
            actual=git.write_tree(handle,allowed_paths=tuple(blobs))
            if actual!=candidate.tree_oid:raise inventory.Blocked('exact-scope-tree-mismatch')

    begin=build
    edit=build

    def reviewed_diff(self):
        diagnostic=self.diagnose();pending=diagnostic['transaction']
        if pending is None:raise Conflict('no-pending-scope')
        with _publication_observe(self.store) as (root,_):
            state=_parse(self.store._read_file(root,'state.json'))
            record=state['records'][pending['record_id']]
            if record['identity']['execution_policy']!='native-scope-v2':
                raise Conflict('legacy-scope-v1-review-requires-explicit-migration')
        return self.store.read_diff(pending['record_id'])

    def copy_review_content(self,section,path,destination,*,commit_oid=None):
        import native_publication as publication
        value=_parse(self.reviewed_diff())
        if value.get('profile')!='native-scope-review-v2':raise Conflict('legacy-scope-v1-reopen-requires-explicit-migration')
        fields={'final':'blobs','baseline':'baseline_blobs','removed':'removed_before','excluded':'excluded_unsaved'}
        if section=='history':
            if commit_oid is None:raise Conflict('exact-history-commit-required')
            rows=publication._decode(value['review']).history
            refs=[row.content for row in rows if row.path==path and row.commit_oid==commit_oid]
        elif section in fields and commit_oid is None:
            rows=publication._decode(value[fields[section]])
            refs=[row[-1] for row in rows if row[0]==path]
        else:raise Conflict('unknown-review-section')
        if len(refs)!=1 or type(refs[0]) is not save.ContentRef:raise Conflict('review-content-absent-or-unknown')
        ref=refs[0]
        with _publication_observe(self.store) as (_,directory):
            with self.store._verified_scope_blob(directory,ref) as fd:
                sink=ContentSink(ref.size,ref.object_format,destination)
                while True:
                    chunk=os.read(fd,65536)
                    if not chunk:break
                    sink.write(chunk)
                if sink.reference()!=ref:raise Conflict('review-content-changed')
        return ref.size

    def lifecycle(self,reason='refresh'):
        if reason not in ('refresh','restart','update'):raise Conflict('invalid-scope-lifecycle')
        with self._call():
            self._issued=None;self.saver._revoke_publication();self._apply=None
            if self._last_save is not None:
                self._last_save[0]._revoke_publication()
                self._last_save[0].preview.reset(reason)
            self._last_save=None
            self.saver.preview.reset(reason)
            return self.diagnose()

    def fresh_apply_preview(self):
        with self._call():
            p=self.saver.preview
            # Independent session leaves the scope issuer and recovery untouched.
            diagnostic=self.diagnose()
            policy=diagnostic.get('active_policy')
            def observe_old_policy():
                source=p.observer()
                if type(source) is not preview.SourceObservation:raise Conflict('fresh-source-observation-required')
                if policy is None:return source
                active=_policy(policy)
                return replace(source,scope=active.allowlist,scope_revision=str(active.revision),exclude_digest=active.exclude_digest)
            independent=preview.NativePreview(p.git,observe_old_policy,p.binding,scratch_root=p.scratch_root,timeout=p.timeout)
            snapshot=independent.build('apply');self._apply_generation+=1
            self._apply=(independent,snapshot)
            diagnostic=self.diagnose()
            return dict(snapshot=snapshot,generation=self._apply_generation,draft={},
                transaction=diagnostic['transaction'],apply_blocked=diagnostic['apply_blocked'])

    def confirm(self,candidate,review,publication,**confirmation):
        with self._call():
            if self._issued!=(candidate,review) or self._issued[0] is not candidate or self._issued[1] is not review:
                raise Conflict('issued-current-scope-confirmation-required')
            if publication.saver is not self.saver or publication.store is not self.store:
                raise Conflict('exact-scope-publication-peers-required')
            pending=self.diagnose()['transaction']
            if pending is None or pending['candidate_digest']!=candidate.candidate_digest:
                raise Conflict('pending-scope-candidate-changed')
            return publication.publish(candidate,review,**confirmation)

    def reopen_review(self,*,expected_revision):
        """Explicit validation/re-materialization; never authority on deserialize."""
        with self._call(),self.saver.preview._call():
            diagnostic=self.diagnose();self.store._cas(dict(revision=diagnostic['revision']),expected_revision)
            pending=diagnostic['transaction']
            if pending is None:raise Conflict('pending-scope-required')
            prepared=self.store.prepare_scope_transition(expected_revision,pending['id'])
            import native_publication as publication
            value=_parse(prepared.artifact)
            if canonical(value)!=prepared.artifact or value.get('profile')!='native-scope-review-v2':
                raise Conflict('canonical-durable-scope-artifact-required')
            candidate=publication._decode(value['candidate']);review=publication._decode(value['review'])
            if (type(candidate) is not save.Candidate or type(review) is not save.ScopeReview or
                    candidate.scope_review!=review or candidate.binding!=self.saver.preview.binding or
                    candidate.candidate_digest!=pending['candidate_digest'] or review.old.document()!=pending['old'] or
                    review.new.document()!=pending['new']):raise Conflict('durable-scope-envelope-mismatch')
            candidate=replace(candidate,scope_review=review)
            # Reopen deliberately expires any retained old in-memory grant.
            self.saver._revoke_publication()
            observed=self.saver._publication_fence(candidate,review)
            git=self.saver.preview.git;disk=self.saver.disk
            if review.history!=scope_history_context(git,candidate.remote.remote_oid,review.old.allowlist,review.new.allowlist,
                    (path for path in review.live_members if _inside(path,review.new.allowlist) and not _inside(path,review.old.allowlist))):
                raise Conflict('last-managed-context-changed')
            selection,payload,expected=reconstruct(git,review,observed.remote_manifest,observed.remote)
            blobs=dict(publication._decode(value['blobs']))
            if set(blobs)!={e.path for e in expected}:raise Conflict('complete-durable-scope-blobs-required')
            width=40 if candidate.object_format=='sha1' else 64
            for entry in expected:
                if type(blobs[entry.path]) is not save.ContentRef or blobs[entry.path].oid!=entry.oid:raise Conflict('durable-scope-blob-mismatch')
            if _tree(expected,width)!=candidate.tree_oid:raise Conflict('durable-scope-tree-mismatch')
            # Reopen has no new payload outputs. Existing canonical closure is
            # verified by diagnose and every held consumption.
            sources=tuple((row.path,disk.source(candidate.binding.root+b'/'+row.path)) for row in review.selected if row.after is not None)
            evidence=Evidence(prepared.operation_id,prepared.owner_id,selected=sources,base_tree=candidate.baseline_tree_oid,
                candidate_manifest=payload,candidate_removals=tuple(row.path for row in review.selected if row.after is None),
                candidate_final=tuple(e.path for e in expected),scope_preparation=prepared,candidate_rules=review.rules)
            plan=disk.project('candidate',disk.snapshot(),evidence)
            self._admitted(plan)
            with disk.guard(plan) as admission:
                admission.create_scope_directories(prepared)
                with self.store.mutex,_publication_observe(self.store,scope_execution=(prepared,admission)):
                    self.store.install_scope_blobs(prepared,admission)
                    self._materialize(candidate,review,selection,blobs,admission,prepared.operation_id,prepared.owner_id)
                    self.saver._publication_fence(candidate,review)
                    self.store.accept_scope(prepared,admission)
            self.saver._issue_scope(candidate,review,self.store);self._issued=(candidate,review)
            return candidate,review

    @staticmethod
    def _admitted(plan):
        if not plan.supported or not plan.decisions:raise DiskBlocked('scope-capacity-unknown:'+repr(plan.unknown))
        if any(row.status=='insufficient-space' for row in plan.decisions):raise InsufficientSpace('scope-capacity-insufficient')
        if any(row.status!='fits' for row in plan.decisions):raise DiskBlocked('scope-capacity-unknown')

    def cancel(self,*,expected_revision):
        with self._call():
            pending=self.diagnose()['transaction']
            if pending is None:raise Conflict('pending-scope-required')
            prepared=self.store.prepare_scope_transition(expected_revision,pending['id'],cancel=True)
            disk=self.saver.disk
            evidence=Evidence(prepared.operation_id,prepared.owner_id,scope_preparation=prepared)
            plan=disk.project('scope_control',disk.snapshot(),evidence);self._admitted(plan)
            with disk.guard(plan) as admission:
                admission.create_scope_directories(prepared);self.store.accept_scope(prepared,admission)
            self._issued=None;self.saver._revoke_publication();self._last_save=None
            return self.diagnose()

    def last_save_offer(self):
        """Full excluded unsaved contents; no selection or Save is implicit."""
        import native_publication as publication
        value=_parse(self.reviewed_diff())
        return publication._decode(value['excluded_unsaved'])

    def review_last_save(self,*,selected=None,approved_parents=()):
        with self._call():
            pending=self.diagnose()['transaction']
            if pending is None:raise Conflict('pending-scope-required')
            self.store.scope_authority()
            rows=self.last_save_offer();allowed={row[0] for row in rows}
            selected=tuple(sorted(allowed if selected is None else selected))
            if not selected or not set(selected)<=allowed:raise Conflict('excluded-unsaved-selection-required')
            p=self.saver.preview
            isolated=preview.NativePreview(p.git,p.observer,p.binding,scratch_root=p.scratch_root,timeout=p.timeout)
            snap=isolated.build('save')
            isolated.select(snap.snapshot_id,selected,approved_parents=approved_parents)
            review=isolated.review(snap.snapshot_id)
            if not review.ready:raise Conflict('last-save-review-not-ready:'+repr(review.blockers))
            saver=save.NativeSave(isolated,self.saver.disk)
            candidate=saver.build(review,active_policy=_policy(pending['old']))
            saver._bind_scope_authority(self.store)
            self._last_save=(saver,candidate,review)
            # Save review invalidates scope authority, even if Save is declined.
            self._issued=None;self.saver._revoke_publication()
            return saver,candidate,review

    def confirm_last_save(self,saver,candidate,review,publication,**confirmation):
        with self._call():
            if (self._last_save is None or any(a is not b for a,b in zip(self._last_save,(saver,candidate,review))) or
                    publication.saver is not saver or publication.store is not self.store):
                raise Conflict('issued-old-scope-save-confirmation-required')
            result=publication.publish(candidate,review,**confirmation)
            # Always retain the transaction. A success changes authoritative R;
            # explicit build against fresh source is mandatory before scope Save.
            self._last_save=None;self._issued=None;self.saver._revoke_publication()
            return result

    def recover_publication(self,publication,operation_id,*,expected_revision,command_id,pending_effect='reconcile'):
        with self._call():
            if publication.store is not self.store:raise Conflict('same-scope-operation-store-required')
            pending=self.diagnose()['transaction']
            if pending is None:raise Conflict('pending-scope-required')
            grant=publication.approve_recovery(operation_id,expected_revision=expected_revision,
                command_id=command_id,pending_effect=pending_effect)
            self._issued=None;self.saver._revoke_publication()
            return publication.resume(grant)
