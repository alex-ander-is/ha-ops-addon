"""Dormant native Save proofs: disposable real Git/live fixtures only."""
import ast
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import native_disk as d
import native_save as s
import native_preview as p
import native_metadata as m
from native_git import NativeGit, IndexEntry, TreeEntry
from test_native_preview import Rig, A, B, C, put, bindings, fixture_manifest, tree_evidence
from native_topology_helpers import git, METADATA


def reviewed(rig, selected, parents=(), direction='save'):
    snapshot = rig.preview.build(direction)
    rig.preview.select(snapshot.snapshot_id, selected, approved_parents=parents)
    review = rig.preview.review(snapshot.snapshot_id)
    assert review.ready, review.blockers
    return review


def build(rig, review):
    return s.NativeSave(rig.preview, d.NativeDisk(rig.root)).build(review)


def content(rig, tree):
    out = {}
    for entry in rig.adapter.tree_entries(tree):
        sink = io.BytesIO()
        rig.adapter.copy_blob(entry.oid, sink)
        out[entry.path] = sink.getvalue()
    return out


def canonical(rig):
    index = rig.root / METADATA / 'index'
    info = index.stat()
    return (index.read_bytes(), info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns,
            git(rig.root, 'show-ref'), (rig.root / METADATA / 'HEAD').read_bytes())


@pytest.mark.skipif(sys.platform != 'linux', reason='Qualified candidate admission requires Linux allocation/mount proof')
class TestCandidate:
    def test_remote_ab_selected_c_and_explicit_b(self, tmp_path):
        rig = Rig(tmp_path)
        oid = rig.commit({A:b'base\n', B:b'base-b\n', C:b'base-c'})
        git(rig.root, 'update-ref', rig.source.base_ref, oid)
        rig.source = replace(rig.source, base_oid=oid)
        rig.remote({A:b'R-A', B:b'R-B', C:b'base-c'}, lambda manifest:m.Manifest(tuple(
            replace(row,uid=123,gid=456,mode=0o4640) if row.path==B else row for row in manifest.entries)))
        rig.live({A:b'R-A', B:b'base-b\n', C:b'new-c'})
        review = reviewed(rig, (C,))
        before = canonical(rig)
        live = tree_evidence(rig.root / 'homeassistant')
        candidate = build(rig, review)
        assert canonical(rig) == before
        assert tree_evidence(rig.root / 'homeassistant') == live
        actual = content(rig, candidate.tree_oid)
        assert {path:actual[path] for path in (A,B,C)} == {A:b'R-A', B:b'R-B', C:b'new-c'}
        remote = content(rig, rig.source.remote_oid)
        records = m.decode(actual[m.MANIFEST_PATH], policy=rig.policy).by_path
        assert records[B] == m.decode(remote[m.MANIFEST_PATH], policy=rig.policy).by_path[B]
        with pytest.raises(FrozenInstanceError):
            candidate.tree_oid = '0'*40
        # Fixture-only handoff; product constructs no commit/ref.
        commit = subprocess.check_output(['git', '--git-dir='+str(rig.root/METADATA), 'commit-tree', candidate.tree_oid],
            input=b'fixture handoff\n', env={**os.environ, 'GIT_AUTHOR_NAME':'Fixture','GIT_AUTHOR_EMAIL':'f@invalid',
                'GIT_COMMITTER_NAME':'Fixture','GIT_COMMITTER_EMAIL':'f@invalid'}).decode().strip()
        git(rig.root, 'update-ref', rig.source.remote_ref, commit)
        rig.source = replace(rig.source, remote_oid=commit)
        preview = rig.preview.build('apply')
        assert B in {r.path for r in preview.rows}
        # Explicit B consent is independent and writes only selected B.
        review = reviewed(rig, (B,))
        candidate = build(rig, review)
        actual = content(rig, candidate.tree_oid)
        assert actual[B] == b'base-b\n' and actual[A] == b'R-A' and actual[C] == b'new-c'


class SaveRig(Rig):
    def __init__(self, tmp, object_format='sha1'):
        self.width = 40 if object_format == 'sha1' else 64
        self.root = (tmp / 'live').resolve()
        self.root.mkdir()
        self.scratch = (tmp / 'scratch').resolve()
        self.scratch.mkdir()
        from native_inventory import Policy
        self.policy = Policy(repository_files=(m.MANIFEST_PATH,))
        (self.root / METADATA).parent.mkdir(parents=True)
        git(self.root, 'init', '--object-format='+object_format)
        self.files = set()
        oid = self.commit({A:b'base\n', B:b'base-b\n'})
        git(self.root, 'update-ref', 'refs/remotes/origin/main', oid)
        git(self.root, 'update-ref', 'refs/heads/synchronized', oid)
        self.binding = bindings(self.root, self.policy)
        self.adapter = NativeGit(self.root, policy=self.policy)
        self.source = p.SourceObservation('refs/remotes/origin/main', oid, (b'homeassistant',),
            'scope-1','exclusions-1',base_ref='refs/heads/synchronized',base_oid=oid)
        self.observations, self.callback = 0, None
        self.preview = p.NativePreview(self.adapter,self.observe,self.binding,scratch_root=self.scratch)

    def commit(self, files, transform=None):
        for old in self.files:
            try: os.unlink(os.path.join(os.fsencode(self.root), old))
            except FileNotFoundError: pass
        self.files = set(files)
        records = []
        for path,data in sorted(files.items()):
            put(self.root,path,data)
            info = os.stat(os.path.join(os.fsencode(self.root),path))
            oid = s._blob(data,self.width)
            records.append(m.Record(path,'file',info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode),oid))
        for path in m.ancestors(files):
            info = os.stat(os.path.join(os.fsencode(self.root),path))
            records.append(m.Record(path,'directory',info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode),None))
        manifest = m.Manifest(tuple(sorted(records,key=lambda r:r.path)))
        if transform: manifest = transform(manifest)
        put(self.root,m.MANIFEST_PATH,m.encode(manifest,width=self.width,policy=self.policy))
        git(self.root,'add','-A','--','homeassistant',os.fsdecode(m.MANIFEST_PATH))
        git(self.root,'commit','--allow-empty','-m','disposable Save fixture')
        return git(self.root,'rev-parse','HEAD').decode().strip()


@pytest.mark.skipif(sys.platform != 'linux', reason='Qualified real Linux candidate proof')
class TestSelectedFence:
    @pytest.mark.parametrize('churn',['edit','addition','deletion'])
    @pytest.mark.parametrize('when',['after-review','during-stream'])
    def test_unselected_churn_is_preserved_remote_and_visible_next_preview(self,tmp_path,monkeypatch,churn,when):
        rig=SaveRig(tmp_path)
        put(rig.root,A,b'selected change')
        review=reviewed(rig,(A,))
        def change():
            if churn=='edit': put(rig.root,B,b'unselected change')
            elif churn=='addition': put(rig.root,C,b'unselected addition')
            else: os.unlink(os.path.join(os.fsencode(rig.root),B))
        if when=='after-review': change()
        else:
            original=rig.adapter.write_blob_stream
            seen=[]
            def stream(producer,**kw):
                if not seen:
                    seen.append(True);change()
                return original(producer,**kw)
            monkeypatch.setattr(rig.adapter,'write_blob_stream',stream)
        candidate=build(rig,review)
        actual=content(rig,candidate.tree_oid)
        assert actual[A]==b'selected change' and actual[B]==b'base-b\n' and C not in actual
        fresh=rig.preview.build('save')
        assert (C if churn=='addition' else B) in {r.path for r in fresh.rows}

    @pytest.mark.parametrize('mutation',['bytes','inode','mode','uid','gid','absence','parent-inode','parent-mode',
        'remote','scope','exclude','binding'])
    def test_selected_source_parent_or_binding_change_refuses_before_resource(self,tmp_path,monkeypatch,mutation):
        rig=SaveRig(tmp_path)
        put(rig.root,A,b'reviewed')
        review=reviewed(rig,(A,))
        path=os.path.join(os.fsencode(rig.root),A)
        if mutation=='bytes':put(rig.root,A,b'not reviewed')
        elif mutation=='inode':os.unlink(path);put(rig.root,A,b'reviewed')
        elif mutation=='mode':os.chmod(path,0o640)
        elif mutation=='uid':os.chown(path,os.stat(path).st_uid+1,-1)
        elif mutation=='gid':os.chown(path,-1,os.stat(path).st_gid+1)
        elif mutation=='absence':os.unlink(path)
        elif mutation=='parent-inode':
            os.rename(rig.root/'homeassistant/p',rig.root/'retained-parent')
            put(rig.root,A,b'reviewed');put(rig.root,B,b'base-b\n')
        elif mutation=='parent-mode':os.chmod(rig.root/'homeassistant/p',0o750)
        elif mutation=='remote':rig.remote({A:b'base\n',B:b'new remote'})
        elif mutation=='scope':rig.source=replace(rig.source,scope_revision='changed')
        elif mutation=='exclude':rig.source=replace(rig.source,exclude_digest='changed')
        else:rig.preview.binding=replace(rig.binding,topology_verified=False)
        before=canonical(rig)
        monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**k:pytest.fail('effect before stale refusal'))
        with pytest.raises((p.Stale,p.Unavailable,ValueError,OSError)):
            build(rig,review)
        assert canonical(rig)==before

    @pytest.mark.parametrize('reason',['refresh','restart','update','new-session','copied-review','foreign','apply','empty'])
    def test_review_lifecycle_and_identity(self,tmp_path,reason):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
        if reason in ('refresh','restart','update'):rig.preview.reset(reason)
        elif reason=='new-session':rig.preview=p.NativePreview(rig.adapter,rig.observe,rig.binding,scratch_root=rig.scratch)
        elif reason=='copied-review':review=replace(review)
        elif reason=='foreign':
            other=tmp_path/'other';other.mkdir();foreign=SaveRig(other);put(foreign.root,A,b'foreign');review=reviewed(foreign,(A,))
        elif reason=='apply':review=reviewed(rig,(A,),direction='apply')
        else:review=reviewed(rig,())
        before=canonical(rig)
        with pytest.raises(p.Stale):build(rig,review)
        assert canonical(rig)==before

    def test_reentrant_reset_select_build_and_review_are_busy(self,tmp_path,monkeypatch):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
        original=rig.adapter.write_blob_stream
        seen=[]
        def stream(producer,**kw):
            if not seen:
                seen.append(True)
                for call in (lambda:rig.preview.reset(),lambda:rig.preview.build('save'),
                    lambda:rig.preview.select(review.draft.snapshot_id,(B,)),
                    lambda:rig.preview.review(review.draft.snapshot_id),lambda:build(rig,review)):
                    with pytest.raises(p.Busy):call()
            return original(producer,**kw)
        monkeypatch.setattr(rig.adapter,'write_blob_stream',stream)
        assert content(rig,build(rig,review).tree_oid)[A]==b'changed'


@pytest.mark.skipif(sys.platform != 'linux', reason='Qualified full tree/metadata Linux proof')
class TestWholeTree:
    @pytest.mark.parametrize('object_format',['sha1','sha256'])
    def test_add_delete_binary_byte_names_and_nested_parent_metadata(self,tmp_path,object_format):
        rig=SaveRig(tmp_path,object_format)
        names=(b'homeassistant/p/UPPER',b'homeassistant/p/upper',b'homeassistant/p/raw-\xff',
               'homeassistant/p/é'.encode(),'homeassistant/p/e\u0301'.encode(),b'homeassistant/new/deep/file')
        for name in names:put(rig.root,name,b'\x00binary\xff'+name)
        os.chown(os.path.join(os.fsencode(rig.root),names[0]),123,456)
        os.chmod(os.path.join(os.fsencode(rig.root),names[0]),0o6750)
        os.unlink(os.path.join(os.fsencode(rig.root),A))
        snapshot=rig.preview.build('save')
        selected=(A,)+names
        parents=tuple(v.path for v in snapshot.parents if any(x in selected for x in v.dependents))
        rig.preview.select(snapshot.snapshot_id,selected,approved_parents=parents)
        review=rig.preview.review(snapshot.snapshot_id);assert review.ready,review.blockers
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
        candidate=build(rig,review)
        actual=content(rig,candidate.tree_oid)
        assert A not in actual and actual[B]==b'base-b\n'
        assert all(actual[name]==b'\x00binary\xff'+name for name in names)
        manifest=m.decode(actual[m.MANIFEST_PATH],width=rig.width,policy=rig.policy)
        row=manifest.by_path[names[0]]
        assert (row.uid,row.gid,row.mode)==(123,456,0o6750)
        assert set(manifest.by_path)==set(names)|{B}|m.ancestors(set(names)|{B})
        assert candidate.object_format==object_format and len(candidate.tree_oid)==rig.width
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live

    @pytest.mark.parametrize('selected',[(A,), (A,B)])
    def test_deletion_preserves_shared_parents_or_prunes_last_leaf(self,tmp_path,selected):
        rig=SaveRig(tmp_path)
        for path in selected:os.unlink(os.path.join(os.fsencode(rig.root),path))
        review=reviewed(rig,selected)
        live=tree_evidence(rig.root/'homeassistant')
        candidate=build(rig,review);actual=content(rig,candidate.tree_oid)
        manifest=m.decode(actual[m.MANIFEST_PATH],policy=rig.policy)
        assert not set(selected)&actual.keys()
        assert set(manifest.by_path)==({B}|m.ancestors((B,)) if B not in selected else set())
        assert tree_evidence(rig.root/'homeassistant')==live

    def test_metadata_only_and_unapproved_shared_parent_retains_remote(self,tmp_path):
        rig=SaveRig(tmp_path)
        # Remote parent metadata differs from the current live value; selecting
        # a leaf is not implicit parent consent.
        old=rig.source.remote_oid
        remote=rig.remote({A:b'base\n',B:b'base-b\n'},lambda manifest:m.Manifest(tuple(
            replace(r,mode=0o750) if r.path==b'homeassistant/p' else r for r in manifest.entries)))
        os.chmod(os.path.join(os.fsencode(rig.root),A),0o4640)
        snapshot=rig.preview.build('save')
        # Parent change must be reviewed explicitly if offered, so give its
        # remote value back to live before the review used for this leaf test.
        os.chmod(rig.root/'homeassistant/p',0o750)
        review=reviewed(rig,(A,))
        candidate=build(rig,review);actual=content(rig,candidate.tree_oid)
        manifest=m.decode(actual[m.MANIFEST_PATH],policy=rig.policy)
        assert actual[A]==b'base\n' and manifest.by_path[A].mode==0o4640
        assert manifest.by_path[B].mode==0o644 and manifest.by_path[b'homeassistant/p'].mode==0o750


@pytest.mark.skipif(sys.platform != 'linux', reason='Qualified Linux disk/resource ordering proof')
class TestQualification:
    def test_one_snapshot_exact_resource_owners_receipts_index_profile_and_peak(self,tmp_path,monkeypatch):
        import struct
        rig=SaveRig(tmp_path);put(rig.root,A,b'selected\x00data')
        review=reviewed(rig,(A,));disk=d.NativeDisk(rig.root)
        counts={'snapshot':0,'project':0};plans=[];events=[];peak=[];admitted=[]
        def physical():
            return sum(os.lstat(os.path.join(parent,name)).st_blocks*512 for parent,dirs,files in
                       os.walk(rig.root/'data/native') for name in dirs+files)
        initial=physical()
        original_snapshot,original_project=disk.snapshot,disk.project
        def snapshot():counts['snapshot']+=1;return original_snapshot()
        def project(*a,**kw):
            counts['project']+=1;plan=original_project(*a,**kw);plans.append(plan)
            for name in ('_scan','_volume','_destination','_objects','_git'):
                monkeypatch.setattr(disk,name,lambda *a,**kw:pytest.fail('phase rescan/capacity/Git observation'))
            return plan
        monkeypatch.setattr(disk,'snapshot',snapshot);monkeypatch.setattr(disk,'project',project)
        original_qualify=d.Admission.qualify_candidate
        def qualify(admission,*args):
            result=original_qualify(admission,*args)
            events.append(('receipt',len(admission._owner_controls),admission._owner_locks.copy()))
            if not admitted:admitted.append(admission)
            return result
        monkeypatch.setattr(d.Admission,'qualify_candidate',qualify)
        original_run=rig.adapter._run
        def run(command,**kw):
            if command[0] in ('read-tree','hash-object','update-index','write-tree'):
                assert events and events[-1][0]=='receipt'
                admission=admitted[0]
                assert admission._owner_locks[b'data/native/git-resources/journal.lock'] is not None
                journal=disk._control(os.fsencode(rig.root)+b'/data/native/git-resources/journal.json')
                assert journal['records']
                assert all((r['operation'],r['owner'])==(admission.plan.evidence.operation_id,admission.plan.evidence.owner_id)
                           for r in journal['records'].values())
                events.append(('effect',command[0]));peak.append(physical()-initial)
            result=original_run(command,**kw)
            if command[0] in ('read-tree','update-index','write-tree'):
                data=Path(os.fsdecode(kw['index'])).read_bytes()
                assert data[:4]==b'DIRC' and struct.unpack('!I',data[4:8])[0]==2
                pos=12
                for _ in range(struct.unpack('!I',data[8:12])[0]):
                    start=pos;pos=data.index(b'\0',pos+62)+1;pos=start+((pos-start+7)//8*8)
                while pos<len(data)-20:
                    name=data[pos:pos+4];size=struct.unpack('!I',data[pos+4:pos+8])[0]
                    assert name==b'TREE';pos+=8+size
                assert pos==len(data)-20
                assert len(data)<=disk._index_bound
            peak.append(physical()-initial)
            return result
        monkeypatch.setattr(rig.adapter,'_run',run)
        candidate=s.NativeSave(rig.preview,disk).build(review)
        assert counts=={'snapshot':1,'project':1}
        plan=plans[0];labels={a.output for a in plan.allocations}
        assert {'generated-manifest-spool','generated-manifest-blob','complete-index','index-lock-overlap'}<=labels
        assert not any('commit' in x or 'reflog' in x or 'ref-' in x or 'worktree' in x for x in labels)
        assert max(peak)<=sum(a.allocated for a in plan.allocations)
        assert plan.evidence.candidate_manifest and len(plan.evidence.candidate_manifest)>len(b'selected\x00data')
        assert not admitted[0].active and candidate.tree_oid
        assert len(events)>10

    def test_malformed_copied_foreign_inactive_admissions_and_plan_swap(self,tmp_path,monkeypatch):
        import copy
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
        original=rig.adapter.write_blob_stream;seen=[]
        def stream(producer,**kw):
            admission=kw['admission'];op,owner=kw['operation_id'],kw['owner_id']
            if not seen:
                seen.append(admission)
                for value,operation,ownership in ((object(),op,owner),(copy.copy(admission),op,owner),
                    (admission,str(uuid.uuid4()),owner),(admission,op,str(uuid.uuid4()))):
                    with pytest.raises(ValueError):original(b'bad',operation_id=operation,owner_id=ownership,admission=value)
                baseline=admission._baseline
                admission._baseline=replace(baseline)
                try:
                    with pytest.raises(ValueError):original(b'bad',**kw)
                finally:admission._baseline=baseline
            return original(producer,**kw)
        monkeypatch.setattr(rig.adapter,'write_blob_stream',stream)
        build(rig,review)
        with pytest.raises(ValueError):original(b'bad',operation_id=seen[0].plan.evidence.operation_id,
            owner_id=seen[0].plan.evidence.owner_id,admission=seen[0])
        with pytest.raises(ValueError):original(b'bad',operation_id=str(uuid.uuid4()),owner_id=str(uuid.uuid4()))

    @pytest.mark.parametrize('key,value',[('core.compression','9'),('core.looseCompression','1'),('index.version','4'),
        ('index.threads','8'),('core.splitIndex','true'),('index.sparse','true'),('core.sparseCheckout','true'),
        ('core.untrackedCache','true'),('core.fsmonitor','true'),('maintenance.auto','true')])
    def test_unsupported_profile_refuses_before_resource(self,tmp_path,monkeypatch,key,value):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
        git(rig.root,'config',key,value)
        monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('unsupported-profile effect'))
        with pytest.raises(d.Blocked,match='unsupported-git-config'):build(rig,review)

    @pytest.mark.parametrize('capacity',['warning','bytes','inodes','unknown'])
    def test_capacity_warning_is_advisory_but_shortage_or_unknown_refuses(self,tmp_path,monkeypatch,capacity):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));disk=d.NativeDisk(rig.root)
        original=disk._volume
        def volume(*a,**kw):
            v=original(*a,**kw)
            if capacity=='warning':return replace(v,available=400*1024**2)
            if capacity=='bytes':return replace(v,available=1)
            if capacity=='inodes':return replace(v,inodes=1)
            return replace(v,supported=False)
        monkeypatch.setattr(disk,'_volume',volume)
        if capacity=='warning':assert s.NativeSave(rig.preview,disk).build(review).tree_oid
        else:
            monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('capacity refusal before resource'))
            with pytest.raises(d.Blocked):s.NativeSave(rig.preview,disk).build(review)
            assert not (rig.root/'data/native/disk-admission.lock').exists()

    def test_deletion_heavy_index_forecast_includes_initial_layout(self,tmp_path,monkeypatch):
        rig=SaveRig(tmp_path)
        names=tuple(b'homeassistant/long/'+str(i).encode()+b'x'*150 for i in range(32))
        oid=rig.remote({name:b'x' for name in names})
        git(rig.root,'update-ref',rig.source.base_ref,oid);rig.source=replace(rig.source,base_oid=oid)
        for name in names:os.unlink(os.path.join(os.fsencode(rig.root),name))
        review=reviewed(rig,names);disk=d.NativeDisk(rig.root);plans=[];indexes=[]
        project=disk.project;run=rig.adapter._run
        def capture(*a,**kw):plan=project(*a,**kw);plans.append(plan);return plan
        def command(command,**kw):
            result=run(command,**kw)
            if command[0] in ('read-tree','update-index','write-tree'):
                indexes.append(Path(os.fsdecode(kw['index'])).stat().st_size)
            return result
        monkeypatch.setattr(disk,'project',capture);monkeypatch.setattr(rig.adapter,'_run',command)
        candidate=s.NativeSave(rig.preview,disk).build(review)
        assert set(content(rig,candidate.tree_oid))=={m.MANIFEST_PATH}
        assert max(indexes)>min(indexes)*5
        assert all(a.logical>=max(indexes) for a in plans[0].allocations if a.output in ('complete-index','index-lock-overlap'))
        assert plans[0].evidence.candidate_removals==tuple(sorted(names))


@pytest.mark.skipif(sys.platform != 'linux', reason='Qualified Linux effect/fault and cleanup proof')
class TestFailures:
    @pytest.mark.parametrize('phase',['read-tree','hash-object','update-index','write-tree'])
    @pytest.mark.parametrize('failure',['ENOSPC','EDQUOT','EIO','interrupt'])
    def test_child_or_io_failure_retains_primary_and_preserves_live_canonical(self,tmp_path,monkeypatch,phase,failure):
        import errno
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
        primary=KeyboardInterrupt('fixture interrupt') if failure=='interrupt' else OSError(getattr(errno,failure),failure)
        original=rig.adapter._run
        def run(command,**kw):
            if command[0]==phase:raise primary
            return original(command,**kw)
        monkeypatch.setattr(rig.adapter,'_run',run)
        with pytest.raises(BaseException) as caught:build(rig,review)
        assert caught.value is primary
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live
        assert not rig.adapter.resources()

    @pytest.mark.parametrize('fault',['short-read','selected-mutated','stored-object-corrupt','unselected-corrupt','wrong-tree','terminal-selected-change'])
    def test_stream_full_tree_and_terminal_proofs_refuse(self,tmp_path,monkeypatch,fault):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed'*20000);review=reviewed(rig,(A,))
        before=canonical(rig)
        if fault=='short-read':
            original=os.read;inode=os.stat(os.path.join(os.fsencode(rig.root),A)).st_ino
            def read(fd,size):
                if os.fstat(fd).st_ino==inode:return b''
                return original(fd,size)
            monkeypatch.setattr(os,'read',read)
        elif fault=='selected-mutated':
            original=s.NativeSave._stream
            from contextlib import contextmanager
            @contextmanager
            def stream(self,row,observed):
                with original(self,row,observed) as producer:
                    def chunks():
                        first=True
                        for data in producer:
                            yield data
                            if first:put(rig.root,A,b'not reviewed');first=False
                    yield chunks()
            monkeypatch.setattr(s.NativeSave,'_stream',stream)
        elif fault=='stored-object-corrupt':
            original=rig.adapter.write_blob_stream
            def blob(producer,**kw):
                result=original(producer,**kw)
                target=rig.root/METADATA/'objects'/result.oid[:2]/result.oid[2:]
                target.chmod(0o644);target.write_bytes(b'corrupt')
                return result
            monkeypatch.setattr(rig.adapter,'write_blob_stream',blob)
        elif fault=='unselected-corrupt':
            original=rig.adapter.write_tree
            def tree(handle,**kw):
                oid=next(e.oid for e in rig.adapter.index_entries(handle) if e.path==B)
                target=rig.root/METADATA/'objects'/oid[:2]/oid[2:]
                target.chmod(0o644);target.write_bytes(b'corrupt')
                return original(handle,**kw)
            monkeypatch.setattr(rig.adapter,'write_tree',tree)
        else:
            original=rig.adapter.write_tree
            def tree(handle,**kw):
                result=original(handle,**kw)
                if fault=='wrong-tree':return rig.adapter.resolve_tree(rig.source.remote_oid)
                put(rig.root,A,b'terminal selected mutation');return result
            monkeypatch.setattr(rig.adapter,'write_tree',tree)
        with pytest.raises((RuntimeError,ValueError,OSError)):build(rig,review)
        assert canonical(rig)==before

    @pytest.mark.parametrize('fault',['admission-lock','owner-lock','marker','resource-parent'])
    def test_ownership_loss_at_receipt_stops_next_effect(self,tmp_path,monkeypatch,fault):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));before=canonical(rig)
        original=d.Admission.qualify_candidate;mutated=[]
        def qualify(admission,*args):
            if not mutated and admission._owner_locks.get(b'data/native/git-resources/journal.lock') is not None:
                mutated.append(True)
                if fault=='admission-lock':target=Path(os.fsdecode(admission.path))
                elif fault=='owner-lock':target=rig.root/'data/native/git-resources/journal.lock'
                else:
                    journal=admission.disk._control(os.fsencode(rig.root)+b'/data/native/git-resources/journal.json')
                    rid=next(iter(journal['records']))
                    resource=rig.root/'data/native/git-resources'/rid
                    target=resource/'owner.json' if fault=='marker' else resource
                target.rename(target.with_name(target.name+'-retained'))
                if fault=='resource-parent':target.mkdir(mode=0o700)
                else:target.write_bytes(b'foreign');target.chmod(0o600)
            return original(admission,*args)
        monkeypatch.setattr(d.Admission,'qualify_candidate',qualify)
        with pytest.raises((ValueError,RuntimeError,OSError)):build(rig,review)
        assert mutated and canonical(rig)==before

    def test_primary_plus_cleanup_double_fault_has_exact_addressed_debt(self,tmp_path,monkeypatch):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));before=canonical(rig)
        primary=OSError(5,'fixture hash-object EIO');run=rig.adapter._run;unlink=rig.adapter._unlink_file
        def command(command,**kw):
            if command[0]=='hash-object':raise primary
            return run(command,**kw)
        def removal(root,path,item):
            if path==b'spool':raise OSError(5,'fixture owned spool cleanup EIO')
            return unlink(root,path,item)
        monkeypatch.setattr(rig.adapter,'_run',command);monkeypatch.setattr(rig.adapter,'_unlink_file',removal)
        with pytest.raises(OSError) as caught:build(rig,review)
        assert caught.value is primary and primary.cleanup_errors
        assert any('spool' in v for v in primary.cleanup_errors)
        assert canonical(rig)==before
        assert rig.adapter.resources()
        journal=json.loads((rig.root/'data/native/git-resources/journal.json').read_bytes())
        assert any(v['phase']=='debt' for v in journal['records'].values())

    @pytest.mark.parametrize('stage',['addition','edit','deletion','unmerged','intent'])
    def test_foreign_canonical_staging_and_lock_are_untouched(self,tmp_path,stage):
        rig=SaveRig(tmp_path);put(rig.root,A,b'changed');rig.source=replace(rig.source,scope=(A,B))
        if stage=='addition':put(rig.root,C,b'staged');git(rig.root,'add','--',os.fsdecode(C))
        elif stage=='edit':put(rig.root,B,b'staged-edit');git(rig.root,'add','--',os.fsdecode(B));put(rig.root,B,b'base-b\n')
        elif stage=='deletion':git(rig.root,'update-index','--force-remove','--',os.fsdecode(B))
        elif stage=='intent':put(rig.root,C,b'intent');git(rig.root,'add','-N','--',os.fsdecode(C))
        else:
            oid=s._blob(b'base-b\n',40)
            env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
            subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'update-index','--index-info'],
                input=b'0 '+b'0'*40+b'\t'+B+b'\n'+b'100644 '+oid.encode()+b' 1\t'+B+b'\n',
                env=env,check=True,capture_output=True)
        review=reviewed(rig,(A,));lock=rig.root/METADATA/'index.lock';lock.write_bytes(b'foreign lock');lock.chmod(0o600)
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant');info=lock.stat()
        assert content(rig,build(rig,review).tree_oid)[B]==b'base-b\n'
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live
        assert lock.read_bytes()==b'foreign lock' and lock.stat().st_ino==info.st_ino


def save_import_guard(sources):
    for name,source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.Import):modules=[part for alias in node.names for part in alias.name.split('.')]
            elif isinstance(node,ast.ImportFrom):modules=(node.module or '').split('.')+[part for alias in node.names for part in alias.name.split('.')]
            else:continue
            assert 'native_save' not in modules or name in ('native_publication.py','native_scope.py'), name


@pytest.mark.parametrize('source',['import native_save as alias','from app.native_save import NativeSave',
    'from app import native_save as alias','def lazy():\n    from ..app import native_save as alias'])
def test_source_guard_detects_production_package_alias_and_lazy_imports(source):
    with pytest.raises(AssertionError):save_import_guard({'server.py':source})


def test_dormancy_constructor_and_effect_capability_contour(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path)
    def forbidden(*a,**kw):raise AssertionError('constructor IO')
    monkeypatch.setattr(os,'open',forbidden);monkeypatch.setattr(subprocess,'Popen',forbidden)
    assert s.NativeSave(rig.preview,d.NativeDisk(rig.root))
    app=Path(s.__file__).parent
    save_import_guard({path.name:path.read_text() for path in app.glob('*.py') if path.name!='native_save.py'})
    source=(app/'native_save.py').read_text()
    tree=ast.parse(source)
    names={node.attr for node in ast.walk(tree) if isinstance(node,ast.Attribute)}
    assert not names & {'update_ref','delete_ref','temporary_worktree','restore','fetch','push','apply','backup','supervisor'}


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux descriptor/stream fault proof')
@pytest.mark.parametrize('fault',['short-write','fsync','spool-close','selected-close-plus-child','descriptor-acquisition'])
def test_stream_descriptor_fsync_and_close_faults_propagate_addressed_primary(tmp_path,monkeypatch,fault):
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));before=canonical(rig)
    primary=OSError(5,'fixture '+fault);seen=[]
    inode=os.stat(os.path.join(os.fsencode(rig.root),A)).st_ino
    def spool(fd):
        try:return os.readlink('/proc/self/fd/'+str(fd)).endswith('/spool')
        except OSError:return False
    if fault=='short-write':
        import native_git as ng
        original=ng._write_all
        def write(stream,data):
            try: fd=stream.fileno()
            except (OSError,AttributeError):return original(stream,data)
            if spool(fd) and not seen:
                seen.append(True);stream.write(bytes(data[:1]));raise primary
            return original(stream,data)
        monkeypatch.setattr(ng,'_write_all',write)
    elif fault=='fsync':
        original=os.fsync
        def fsync(fd):
            if spool(fd) and os.fstat(fd).st_size and not seen:seen.append(True);raise primary
            return original(fd)
        monkeypatch.setattr(os,'fsync',fsync)
    elif fault=='spool-close':
        original=os.fdopen
        class Stream:
            def __init__(self,stream):self.stream=stream
            def __getattr__(self,name):return getattr(self.stream,name)
            def close(self):self.stream.close();seen.append(True);raise primary
        def fdopen(fd,*args,**kw):
            stream=original(fd,*args,**kw)
            return Stream(stream) if spool(fd) else stream
        monkeypatch.setattr(os,'fdopen',fdopen)
    elif fault=='selected-close-plus-child':
        run=rig.adapter._run;child_failed=[]
        def command(command,**kw):
            if command[0]=='hash-object':child_failed.append(True);raise primary
            return run(command,**kw)
        monkeypatch.setattr(rig.adapter,'_run',command)
        original=os.close
        def close(fd):
            if os.fstat(fd).st_ino==inode and child_failed and not seen:
                original(fd);seen.append(True);raise OSError(5,'fixture selected close debt')
            return original(fd)
        monkeypatch.setattr(os,'close',close)
    else:
        original=os.open
        def opening(path,flags,*args,**kw):
            if path==b'a' and flags & os.O_NOFOLLOW and rig.adapter._active and not seen:
                seen.append(True);raise primary
            return original(path,flags,*args,**kw)
        monkeypatch.setattr(os,'open',opening)
    with pytest.raises(OSError) as caught:build(rig,review)
    assert caught.value is primary and seen
    if fault=='selected-close-plus-child':assert any('route-fd-close' in x for x in primary.cleanup_errors)
    if fault=='spool-close':assert any('spool-close' in x for x in primary.cleanup_errors)
    assert canonical(rig)==before


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux pinned Git environment proof')
def test_ambient_git_index_config_filters_and_hooks_cannot_affect_candidate(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path);put(rig.root,A,b'\x00raw\r\ncontent');review=reviewed(rig,(A,))
    global_config=tmp_path/'global-config'
    global_config.write_text('[core]\ncompression=9\nhooksPath=/nonexistent\n[index]\nversion=4\nthreads=8\n[filter "bad"]\nclean=false\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(global_config));monkeypatch.setenv('GIT_INDEX_VERSION','4')
    monkeypatch.setenv('GIT_CONFIG_COUNT','1');monkeypatch.setenv('GIT_CONFIG_KEY_0','core.compression')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0','9')
    original=rig.adapter._argv;commands=[]
    def argv(command,**kw):
        result,env=original(command,**kw);commands.append((command,dict(env),result))
        assert env['GIT_CONFIG_GLOBAL']==os.devnull and 'GIT_INDEX_VERSION' not in env and 'GIT_CONFIG_COUNT' not in env
        assert 'index.version=2' in result and 'index.threads=1' in result and 'core.looseCompression=-1' in result
        if command[0] in ('read-tree','update-index','write-tree'):
            assert env['GIT_INDEX_FILE']!=str(rig.root/METADATA/'index')
        return result,env
    monkeypatch.setattr(rig.adapter,'_argv',argv)
    candidate=build(rig,review)
    assert content(rig,candidate.tree_oid)[A]==b'\x00raw\r\ncontent'
    assert not {cmd[0][0] for cmd in commands}&{'add','rm','checkout','reset','clean','switch','fetch','push','commit','commit-tree','update-ref'}


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux exact object and effect timing proof')
@pytest.mark.parametrize('shape',['tree','malformed-commit','blob-as-tree','corrupt-commit'])
def test_exact_source_commit_to_tree_resolution_never_guesses(shape,tmp_path):
    rig=SaveRig(tmp_path)
    tree=git(rig.root,'rev-parse',rig.source.remote_oid+'^{tree}').decode().strip()
    before=canonical(rig)
    if shape=='tree':assert rig.adapter.resolve_tree(tree)==tree
    elif shape=='corrupt-commit':
        target=rig.root/METADATA/'objects'/rig.source.remote_oid[:2]/rig.source.remote_oid[2:]
        target.chmod(0o644);target.write_bytes(b'corrupt')
        with pytest.raises((ValueError,RuntimeError)):rig.adapter.resolve_tree(rig.source.remote_oid)
    else:
        payload=b'bad-header\n' if shape=='malformed-commit' else b'tree '+s._blob(b'base\n',40).encode()+b'\n\nfixture\n'
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        result=subprocess.run(['git','--git-dir='+str(rig.root/METADATA),'hash-object','--literally','-w','-t','commit','--stdin'],
            input=payload,env=env,capture_output=True,check=True).stdout.decode().strip()
        with pytest.raises((ValueError,RuntimeError)):rig.adapter.resolve_tree(result)
    assert canonical(rig)==before


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux owner allocation pre-receipt ordering proof')
def test_owner_lock_loss_before_first_resource_receipt_prevents_read_tree(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));published=[]
    original=rig.adapter._publish;run=rig.adapter._run
    def publish(journal):
        result=original(journal)
        if not published and any(r['phase']=='allocated' for r in journal['records'].values()):
            published.append(True)
            target=rig.root/'data/native/git-resources/journal.lock'
            target.rename(target.with_name('retained-foreign-lock'))
            target.write_bytes(b'foreign');target.chmod(0o600)
        return result
    def command(command,**kw):
        if command[0]=='read-tree':pytest.fail('read-tree after owner allocation lost')
        return run(command,**kw)
    monkeypatch.setattr(rig.adapter,'_publish',publish);monkeypatch.setattr(rig.adapter,'_run',command)
    with pytest.raises((ValueError,RuntimeError,OSError)):build(rig,review)
    assert published


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux selected absence acquisition proof')
@pytest.mark.parametrize('change',['reappeared','ancestor-acquisition-error'])
def test_reviewed_selected_absence_is_proved_and_never_inventory_omission(tmp_path,monkeypatch,change):
    rig=SaveRig(tmp_path);os.unlink(os.path.join(os.fsencode(rig.root),A));review=reviewed(rig,(A,))
    if change=='reappeared':put(rig.root,A,b'reappeared')
    else:
        original=os.open
        def opening(path,flags,*a,**kw):
            if path==b'p' and flags&os.O_DIRECTORY:raise OSError(5,'fixture necessary absence ancestor acquisition EIO')
            return original(path,flags,*a,**kw)
        monkeypatch.setattr(os,'open',opening)
    monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('effect after unproved selected absence'))
    with pytest.raises((p.Stale,OSError)):build(rig,review)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux qualified guard teardown proof')
def test_guard_teardown_failure_returns_no_candidate_and_ends_capability(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,));disk=d.NativeDisk(rig.root)
    original=d.Admission._close;retained=[];primary=OSError(5,'fixture guard teardown EIO')
    def close(admission):retained.append(admission);original(admission);raise primary
    monkeypatch.setattr(d.Admission,'_close',close)
    with pytest.raises(OSError) as caught:s.NativeSave(rig.preview,disk).build(review)
    assert caught.value is primary and not retained[0].active and disk._candidate_admission is None
    assert disk._active_operation is None


def test_import_and_candidate_nested_values_are_inert_and_immutable(tmp_path,monkeypatch):
    # Reloading replaces exact typed codec classes. Prove import inertness in
    # a child so this test cannot change the shared publication registry peers.
    original_content_ref=s.ContentRef
    script="""import importlib, os, subprocess, sys
sys.path.insert(0, APP_PATH)
import native_save as module
def forbidden(*args, **kwargs):
    raise AssertionError('import IO')
os.open=forbidden
subprocess.Popen=forbidden
importlib.reload(module)
""".replace('APP_PATH',repr(str(Path(s.__file__).parent)))
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert s.ContentRef is original_content_ref
    # Diagnostic values never grant review authority, even before reset.
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed')
    review=reviewed(rig,(A,))
    record=review.selected[0]
    digest='f'*64
    selected=[replace(record,issues=[],parents=[])]
    parents=[[b'homeassistant',None]];routes=[[b'', [1,2,3,4,5,6]]]
    issued=[p.ParentDetail(b'homeassistant',None,None,[A,B],[])]
    final_dependents=[A];final=[m.ParentReview(b'homeassistant',None,None,final_dependents)]
    value=s.Candidate('1'*40,'sha1',rig.source,'2'*40,rig.binding,review.draft.snapshot_id,
        review.draft.generation,selected,[],final,parents,routes,'3'*40,digest,digest,digest,b'commit',issued)
    selected.clear();parents.clear();routes[0][1].clear()
    issued[0].dependents.clear();issued[0].issues.clear();final_dependents.clear()
    assert value.selected and value.parent_dependencies and value.routes==((b'',(1,2,3,4,5,6)),)
    assert value.issued_parent_reviews[0].dependents==(A,B) and value.reviewed_parents[0].dependents==(A,)
    with pytest.raises(m.Blocked):replace(value,remote_object_kind=bytearray(b'commit'))
    with pytest.raises(p.Stale):build(rig,value)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux remote commit identity freshness proof')
def test_equal_remote_tree_with_a_different_commit_still_invalidates_review(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed');review=reviewed(rig,(A,))
    old=rig.source.remote_oid;tree=rig.adapter.resolve_tree(old)
    new=rig.remote({A:b'base\n',B:b'base-b\n'})
    assert new!=old and rig.adapter.resolve_tree(new)==tree
    monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('effect after remote commit changed'))
    with pytest.raises(p.Stale):build(rig,review)


def test_malformed_diagnostic_review_never_reaches_authority_fields(tmp_path,monkeypatch):
    rig=SaveRig(tmp_path);put(rig.root,A,b'changed');reviewed(rig,(A,))
    monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('forged review effect'))
    with pytest.raises(p.Stale):build(rig,p.Review(None,(),(),()))


@pytest.mark.skipif(sys.platform != 'linux', reason='Real qualified candidate correction proof')
class TestCorrection:
    def test_issued_parent_disclosure_is_distinct_and_bound(self,tmp_path):
        rig=SaveRig(tmp_path);put(rig.root,A,b'selected change');put(rig.root,C,b'unselected added child')
        os.chmod(rig.root/'homeassistant/p',0o750)
        review=reviewed(rig,(A,),(b'homeassistant/p',))
        candidate=build(rig,review)
        assert C in review.parents[0].dependents
        assert candidate.issued_parent_reviews==review.parents
        final=next(row for row in candidate.reviewed_parents if row.path==b'homeassistant/p')
        assert final.dependents==(A,B)
        assert C not in content(rig,candidate.tree_oid)
        # Both exact structured sets, rather than only final closure, are bound.
        expected=s._digest((candidate.remote,candidate.remote_object_kind,candidate.binding,
            review.draft,review.selected,review.parents,candidate.reviewed_parents,
            candidate.parent_dependencies,candidate.routes,candidate.manifest_oid))
        assert candidate.selection_digest==expected
        assert candidate.candidate_digest==s._digest((candidate.tree_oid,candidate.baseline_tree_oid,
            candidate.remote_object_kind,expected,candidate.manifest_oid))

    @pytest.mark.parametrize('kind',[b'commit',b'tree'])
    def test_source_kind_retained(self,tmp_path,kind):
        rig=SaveRig(tmp_path)
        if kind==b'tree':
            tree=rig.adapter.resolve_tree(rig.source.remote_oid)
            git(rig.root,'update-ref',rig.source.remote_ref,tree)
            rig.source=replace(rig.source,remote_oid=tree)
        put(rig.root,A,b'changed')
        candidate=build(rig,reviewed(rig,(A,)))
        assert candidate.remote_object_kind==kind
        assert candidate.remote==rig.source
        assert candidate.baseline_tree_oid==rig.adapter.resolve_tree(rig.source.remote_oid)
        if kind==b'tree':assert candidate.remote.remote_oid==candidate.baseline_tree_oid

    @pytest.mark.parametrize('mutation',['mode','absence','ancestor-absence','shared-mode','shared-absence'])
    def test_reviewed_orphan_parent_deletion(self,tmp_path,mutation):
        rig=SaveRig(tmp_path);leaf=b'homeassistant/orphan/nest/only.yaml';parent=b'homeassistant/orphan/nest'
        sibling=b'homeassistant/orphan/nest/kept.yaml'
        files={A:b'base\n',B:b'base-b\n',leaf:b'delete'}
        if mutation.startswith('shared'):files[sibling]=b'keep remote'
        oid=rig.commit(files)
        for ref in (rig.source.remote_ref,rig.source.base_ref):git(rig.root,'update-ref',ref,oid)
        rig.source=replace(rig.source,remote_oid=oid,base_oid=oid)
        os.unlink(os.path.join(os.fsencode(rig.root),leaf))
        if mutation=='shared-absence':os.unlink(os.path.join(os.fsencode(rig.root),sibling))
        if mutation.endswith('mode'):os.chmod(os.path.join(os.fsencode(rig.root),parent),0o750)
        else:
            os.rmdir(os.path.join(os.fsencode(rig.root),parent))
            if mutation=='ancestor-absence':os.rmdir(rig.root/'homeassistant/orphan')
        snapshot=rig.preview.build('save')
        approved=tuple(row.path for row in snapshot.parents if leaf in row.dependents)
        rig.preview.select(snapshot.snapshot_id,(leaf,),approved_parents=approved)
        review=rig.preview.review(snapshot.snapshot_id);assert review.ready,review.blockers
        before=canonical(rig);live=tree_evidence(rig.root/'homeassistant')
        candidate=build(rig,review);actual=content(rig,candidate.tree_oid)
        assert leaf not in actual and actual[A]==files[A] and actual[B]==files[B]
        records=m.decode(actual[m.MANIFEST_PATH],policy=rig.policy).by_path
        if mutation.startswith('shared'):
            assert actual[sibling]==files[sibling]
            if mutation=='shared-mode':
                assert records[parent].mode==0o750
                assert next(row for row in candidate.reviewed_parents if row.path==parent).dependents==(sibling,)
            else:
                remote_records=m.decode(content(rig,oid)[m.MANIFEST_PATH],policy=rig.policy).by_path
                assert records[parent]==remote_records[parent] and not candidate.reviewed_parents
        else:assert parent not in records and not candidate.reviewed_parents
        assert candidate.issued_parent_reviews==review.parents
        assert canonical(rig)==before and tree_evidence(rig.root/'homeassistant')==live

    @pytest.mark.parametrize('change',['reappear','remove','attributes','eacces','eio'])
    def test_orphan_parent_post_review_changes_or_uncertainty_refuse(self,tmp_path,monkeypatch,change):
        rig=SaveRig(tmp_path);leaf=b'homeassistant/orphan/only.yaml';parent=b'homeassistant/orphan'
        oid=rig.commit({A:b'base\n',B:b'base-b\n',leaf:b'delete'})
        for ref in (rig.source.remote_ref,rig.source.base_ref):git(rig.root,'update-ref',ref,oid)
        rig.source=replace(rig.source,remote_oid=oid,base_oid=oid)
        os.unlink(os.path.join(os.fsencode(rig.root),leaf))
        if change=='reappear':os.rmdir(rig.root/'homeassistant/orphan')
        else:os.chmod(rig.root/'homeassistant/orphan',0o750)
        review=reviewed(rig,(leaf,),(parent,))
        if change=='reappear':(rig.root/'homeassistant/orphan').mkdir()
        elif change=='remove':os.rmdir(rig.root/'homeassistant/orphan')
        elif change=='attributes':os.chmod(rig.root/'homeassistant/orphan',0o700)
        else:
            original=os.stat
            def uncertain(path,*a,**kw):
                if path==b'orphan':raise OSError(13 if change=='eacces' else 5,'uncertain parent')
                return original(path,*a,**kw)
            monkeypatch.setattr(os,'stat',uncertain)
        monkeypatch.setattr(rig.adapter,'_resource',lambda *a,**kw:pytest.fail('effect after parent change'))
        with pytest.raises((p.Stale,OSError)):build(rig,review)

    @pytest.mark.parametrize('size,at,multiple',[(131072,'first',False),(1,'last',False),
        (p.CHUNK+1,'last',False),(2*p.CHUNK+17,'first',False),(3*p.CHUNK,'last',True)])
    def test_growing_selected_stream_never_exceeds_issued_spool(self,tmp_path,monkeypatch,size,at,multiple):
        import native_git as ng
        rig=SaveRig(tmp_path);put(rig.root,A,b'x'*size)
        selected=(A,)
        if multiple:put(rig.root,B,b'z'*size);selected=(A,B)
        review=reviewed(rig,selected);before=canonical(rig)
        disk=d.NativeDisk(rig.root);counts={'snapshot':0,'project':0};plans=[]
        for method in counts:
            original=getattr(disk,method)
            def count(*a,_method=method,_original=original,**kw):
                counts[_method]+=1;result=_original(*a,**kw)
                if _method=='project':plans.append(result)
                return result
            monkeypatch.setattr(disk,method,count)
        original=ng._write_all;written={};physical={};mutated=False;target_path=B if multiple else A
        def write(stream,data):
            nonlocal mutated
            try:target=os.readlink('/proc/self/fd/'+str(stream.fileno()))
            except (AttributeError,OSError):return original(stream,data)
            result=original(stream,data)
            if target.endswith('/spool'):
                written[target]=written.get(target,0)+len(data);stream.flush()
                physical[target]=max(physical.get(target,0),os.fstat(stream.fileno()).st_blocks*512)
                number=len(written)
                if number==(2 if multiple else 1) and not mutated and (at=='first' or written[target]==size):
                    mutated=True
                    with open(os.path.join(os.fsencode(rig.root),target_path),'ab') as out:out.write(b'g'*(8*1024*1024))
            return result
        monkeypatch.setattr(ng,'_write_all',write)
        with pytest.raises(p.Stale):s.NativeSave(rig.preview,disk).build(review)
        assert mutated and counts=={'snapshot':1,'project':1}
        assert written and max(written.values())<=size
        assert list(written.values())[-1]==(min(p.CHUNK,size) if at=='first' else size)
        assert sum(physical.values())<=sum(row.allocated for row in plans[0].allocations)
        assert canonical(rig)==before


@pytest.mark.parametrize('sink',[False,True])
def test_live_file_growth_is_bounded_before_sink_write(tmp_path,monkeypatch,sink):
    rig=SaveRig(tmp_path);size=p.CHUNK+1;put(rig.root,A,b'x'*size)
    original=os.read;grown=False;reads=[]
    def read(fd,n):
        nonlocal grown
        data=original(fd,n)
        if data:
            reads.append(len(data))
            if not grown:
                grown=True
                with open(os.path.join(os.fsencode(rig.root),A),'ab') as out:out.write(b'g'*(8*1024*1024))
        return data
    monkeypatch.setattr(os,'read',read);output=io.BytesIO() if sink else None
    with pytest.raises(p.Stale):p._live_file(rig.binding,A,rig.width,output)
    assert sum(reads)<=size
    if sink:assert len(output.getvalue())<=size


@pytest.mark.parametrize('size',[0,1,p.CHUNK+1])
def test_selected_growth_before_first_read_yields_nothing(tmp_path,size):
    rig=SaveRig(tmp_path);put(rig.root,A,b'x'*size);review=reviewed(rig,(A,))
    save=s.NativeSave(rig.preview,d.NativeDisk(rig.root))
    with pytest.raises(p.Stale):
        with save._stream(review.selected[0],rig.preview._observed) as chunks:
            with open(os.path.join(os.fsencode(rig.root),A),'ab') as out:out.write(b'g'*(8*1024*1024))
            next(chunks)
