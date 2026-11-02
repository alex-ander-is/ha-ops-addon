"""Real candidate, complete review and explicit diagnostic decision contracts."""
import base64
import hashlib
import os
import sys
from pathlib import Path
from unittest import mock
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_migration_review as r
import native_migration_git_candidate as c
import native_migration as n
import native_disk as d
from test_native_migration_git_candidate import ready,build,git,write,synthetic_host_allocation,host_rename_adapter
from test_native_migration import binding

CONTEXT=('ingress','http://fixture.invalid','/api/hassio_ingress/fixture','fixture-user')


def setup_review(tmp_path, **kw):
    root,repo,p,prepared,state,parent=ready(tmp_path,**kw)
    value=build(p,prepared,state,parent)
    return root,repo,p,r.Review(c.Candidate(p)),value


def challenge(owner):
    model=owner.diagnose()['model']
    assert model is not None
    reviewed=owner.review(candidate=model['candidate'],scope=model['scope_digest'],context=CONTEXT)
    return dict(review_id=reviewed['review_id'],candidate=model['candidate'],scope=model['scope_digest'],context=CONTEXT)


@pytest.mark.parametrize('fmt',['sha1','sha256'])
@pytest.mark.parametrize('action',['approve','refuse','dismiss'])
def test_exact_explicit_decisions_are_inert_on_reopen(tmp_path,fmt,action):
    root,repo,p,owner,value=setup_review(tmp_path,fmt=fmt)
    before=owner.diagnose()
    assert before['review_id'] is None and before['decision'] is None
    ref=challenge(owner)
    result=owner.decide(action=action,**ref)
    assert result['decision']['action']==action and result['decision_authority'] is False
    with pytest.raises(n.Conflict):owner.decide(action=action,**ref)
    reopened=r.Review(c.Candidate(p))
    diagnostic=reopened.diagnose()
    assert diagnostic['required'] and diagnostic['decision']['action']==action
    assert diagnostic['decision_authority'] is False and diagnostic['review_id'] is None
    with pytest.raises(n.Conflict):reopened.decide(action='approve',**ref)


def test_full_model_rows_parents_and_unknown_history(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path)
    model=owner.diagnose()['model']
    assert model['total']==len(value['rows'])==len(model['rows'])
    assert model['parents'] and all(parent['after'] is None or parent['after']['kind']=='directory' for parent in model['parents'])
    assert all(row['before_metadata'] is None for row in model['rows'])
    assert any(row['observed_source_metadata'] for row in model['rows'])
    assert model['scope']['dispositions'] and model['complete']
    for row in model['rows']:
        original=value['rows'][row['index']]
        assert row['before']==original['before'] and row['after']==original['after']


@pytest.mark.parametrize('field,value',[('candidate','a'*64),('scope','b'*64),('review_id','c'*64),('context',('direct','http://other','user'))])
def test_forged_review_identity_refused(tmp_path,field,value):
    root,repo,p,owner,candidate=setup_review(tmp_path)
    ref=challenge(owner);ref[field]=value
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)
    assert owner.diagnose()['decision'] is None


def test_stale_unavailable_and_aba_never_reuse_challenge(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path)
    ref=challenge(owner)
    p.app='changed'
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)
    p.app='fixture-app-1'
    assert owner.c.revalidate()=='stale'
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)
    assert owner.diagnose()['decision'] is None


def test_drift_after_revalidation_before_admission_refuses(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path)
    ref=challenge(owner)
    original=p.disk.migration_review_guard
    from contextlib import contextmanager
    @contextmanager
    def drift(recipe):
        p.app='changed-after-revalidation'
        with original(recipe):yield
    with mock.patch.object(p.disk,'migration_review_guard',drift),pytest.raises((n.Blocked,RuntimeError)):
        owner.decide(action='approve',**ref)
    assert owner.diagnose()['decision'] is None


def test_complete_binary_spool_pages_one_stream_and_no_candidate_scan_per_page(tmp_path):
    payload=bytes(range(256))*513+b'final-byte\xff'
    def configure(root,repo):
        write(repo,b'homeassistant/binary',payload)
        git(repo,'add','.');git(repo,'commit','-qm','binary')
    root,repo,p,owner,value=setup_review(tmp_path,configure=configure)
    ref=challenge(owner)
    row=next(i for i,x in enumerate(value['rows']) if x['after'] and n.decode_path(x['after']['path'])==b'homeassistant/binary')
    original=owner.c.read_content
    with mock.patch.object(owner.c,'read_content',wraps=original) as stream:
        view=owner.open_content(row=row,side='after',**ref)
        assert stream.call_count==1
        with mock.patch.object(owner.c,'read',side_effect=AssertionError('full candidate read per page')):
            pages=[owner.content_page(view_id=view['view_id'],offset=offset,context=CONTEXT) for offset in range(0,len(payload),r.PAGE)]
            assert b''.join(base64.b64decode(page['data']) for page in pages)==payload
            assert pages[-1]['eof'] and view['sha256']==hashlib.sha256(payload).hexdigest()
            assert base64.b64decode(owner.content_page(view_id=view['view_id'],offset=len(payload)-1,context=CONTEXT)['data'])==payload[-1:]
            assert owner.content_page(view_id=view['view_id'],offset=len(payload),context=CONTEXT)['data']==''
        assert stream.call_count==1
    owner.close_content(context=CONTEXT)
    assert not list((root/os.fsdecode(r.STORE)).glob('view-*'))


@pytest.mark.parametrize('offset',[-1,True,1<<70])
def test_invalid_range_invalidates_completion(tmp_path,offset):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    with pytest.raises(n.Conflict):owner.content_page(view_id=view['view_id'],offset=offset,context=CONTEXT)
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)


@pytest.mark.parametrize('kind',['replace','mutate','hardlink'])
def test_spool_tamper_never_exposes_bytes_or_deletes_foreign(tmp_path,kind):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    target=root/os.fsdecode(r.STORE)/owner._view['name']
    if kind=='replace':
        target.unlink();target.write_bytes(b'foreign')
    elif kind=='mutate':target.write_bytes(b'foreign')
    else:os.link(target,target.parent/'foreign')
    with pytest.raises((n.Blocked,OSError)):owner.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)
    with pytest.raises((n.Blocked,d.Blocked,OSError)):owner.close_content(context=CONTEXT)
    assert target.exists()


def test_reopen_retains_spool_diagnostic_without_adoption(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    reopened=r.Review(c.Candidate(p));diagnostic=reopened.diagnose()
    assert diagnostic['retained'] and reopened._view is None and diagnostic['review_id'] is None
    with pytest.raises(n.Conflict):reopened.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)
    with pytest.raises(n.Conflict):reopened.open_content(row=0,side='before',**ref)
    ref2=challenge(reopened)
    new=reopened.open_content(row=0,side='before',**ref2)
    page=reopened.content_page(view_id=new['view_id'],offset=new['size'],context=CONTEXT)
    assert page['eof'] and page['data']=='' and new['view_id']!=view['view_id']
    assert len(list((root/os.fsdecode(r.STORE)).glob('view-*')))==1
    assert len(list((root/os.fsdecode(r.STORE)).glob('completed-*.json')))==1
    reopened.close_content(context=CONTEXT)
    os.close(owner._view['fd']);owner._view=None


@pytest.mark.parametrize('limit',['available','inodes'])
def test_low_capacity_refuses_before_review_store_write(tmp_path,limit):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    from dataclasses import replace
    original=p.disk._volume
    def low(*a,**kw):return replace(original(*a,**kw),**{limit:0})
    with mock.patch.object(p.disk,'_volume',low),pytest.raises((d.Blocked,n.Blocked)):
        owner.open_content(row=0,side='before',**ref)
    assert not (root/os.fsdecode(r.STORE)).exists()


@pytest.mark.parametrize('boundary',['review-write','review-file-fsync','review-replace','review-directory-fsync'])
def test_decision_interruption_never_restores_authority(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    def crash(event):
        if event==boundary:raise OSError('injected')
    owner.trace=crash
    with pytest.raises(OSError):owner.decide(action='approve',**ref)
    assert owner.diagnose()['review_id'] is None
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)
    reopened=r.Review(c.Candidate(p))
    assert reopened.diagnose()['decision_authority'] is False


def test_review_private_policy_and_recipe_issuer(tmp_path):
    from native_inventory import Policy
    path=r.STORE+b'/decision.json'
    assert Policy().reason(path)=='service-data'
    with pytest.raises(n.Blocked):Policy(repository_files=(path,))
    root,repo,p,owner,value=setup_review(tmp_path)
    with pytest.raises(n.Blocked):n.Preparation(binding(root,Policy(legacy_repo=r.STORE)),app_identity='x',manifest_digest='a'*64)
    recipe=owner._recipe(1)
    with p.disk.migration_review_guard(recipe):pass
    with pytest.raises(n.Blocked):
        with p.disk.migration_review_guard(recipe):pass


@pytest.mark.parametrize('boundary',['review-content-write','review-content-file-fsync','review-content-directory-fsync'])
def test_incomplete_spool_never_exposes_or_adopts_view(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    def crash(event):
        if event==boundary:raise OSError('interrupted content')
    owner.trace=crash
    with pytest.raises(OSError):owner.open_content(row=0,side='before',**ref)
    assert owner._view is None
    assert r.Review(c.Candidate(p)).diagnose()['retained']
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)


@pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,reason='requires root Linux byte/ownership fixture')
def test_linux_byte_paths_full_file_and_parent_metadata(tmp_path):
    def configure(root,repo):
        write(repo,b'homeassistant/parents/\xffleaf',b'\x00\xfflast')
        os.chown(os.fsencode(repo)+b'/homeassistant/parents/\xffleaf',23456,34567)
        os.chmod(os.fsencode(repo)+b'/homeassistant/parents/\xffleaf',0o4700)
        os.chown(repo/'homeassistant/parents',23456,34567);os.chmod(repo/'homeassistant/parents',0o2750)
        (root/'homeassistant/parents').mkdir()
        git(repo,'add','.');git(repo,'commit','-qm','byte metadata')
    root,repo,p,owner,value=setup_review(tmp_path,configure=configure)
    model=owner.diagnose()['model'];row=next(x for x in model['rows'] if x['after'] and n.decode_path(x['after']['path']).endswith(b'\xffleaf'))
    assert row['after_metadata']==dict(uid=23456,gid=34567,mode=0o4700,kind='file')
    parent=next(x for x in model['parents'] if n.decode_path(x['path'])==b'homeassistant/parents')
    assert parent['after']==dict(uid=23456,gid=34567,mode=0o2750,kind='directory')
    assert parent['observed_source_metadata']==parent['after']
    ref=challenge(owner);view=owner.open_content(row=row['index'],side='after',**ref)
    assert base64.b64decode(owner.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)['data'])==b'\x00\xfflast'


@pytest.mark.skipif('HA_OPS_EXT4_ROOT' not in os.environ,reason='requires explicit isolated ext4 allocation profile')
def test_ext4_review_exact_allocation_and_replacement_overlap():
    import tempfile
    from contextlib import contextmanager
    if os.environ.get('HA_OPS_EXT4_PROFILE')!='allocation':pytest.skip('allocation profile only')
    payload=bytes(range(256))*8193
    def configure(root,repo):
        write(repo,b'homeassistant/binary',payload);git(repo,'add','.');git(repo,'commit','-qm','allocation')
    with tempfile.TemporaryDirectory(dir=os.environ['HA_OPS_EXT4_ROOT'],prefix='migration-review-') as temporary:
        root,repo,p,owner,value=setup_review(Path(temporary),configure=configure);ref=challenge(owner)
        recipes=[];samples=[];volumes=[]
        original_guard=p.disk.migration_review_guard;original_volume=p.disk._volume
        @contextmanager
        def guard(recipe):
            recipes.append(recipe)
            with original_guard(recipe):yield
        def volume(*a,**kw):
            result=original_volume(*a,**kw);volumes.append(result);return result
        def sample(event):
            base=root/os.fsdecode(r.STORE)
            paths=[] if not base.exists() else [base,*base.iterdir()]
            samples.append((sum(p.lstat().st_blocks*512 for p in paths),len(paths)))
        row=next(i for i,x in enumerate(value['rows']) if x['after'] and n.decode_path(x['after']['path'])==b'homeassistant/binary')
        owner.trace=sample
        with mock.patch.object(p.disk,'migration_review_guard',guard),mock.patch.object(p.disk,'_volume',volume):
            owner.open_content(row=row,side='after',**ref)
            first=samples[-1];samples.clear()
            owner.open_content(row=row,side='after',**ref)
        recipe=recipes[-1];volume=volumes[-1];q=volume.quantum
        assert volume.filesystem==b'ext4' and q==4096
        bound=sum(d._round(n,q)*2+3*q for n in recipe.sizes)+recipe.entries*3*q
        assert max(n-first[0] for n,_ in samples)<=bound
        assert max(n-first[0] for n,_ in samples)>=len(payload)
        assert max(i-first[1] for _,i in samples)<=recipe.entries+32
        print('MIGRATION_REVIEW_EXT4 '+str(dict(additional_peak=max(n-first[0] for n,_ in samples),bound=bound)))
        owner.close_content(context=CONTEXT)


def test_diagnostic_control_short_writes_and_duplicate_rejection(tmp_path):
    from types import SimpleNamespace
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)))
    tmp_path.chmod(0o700)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    value=dict(schema=1,action='approve',candidate='a'*64,scope='b'*64,model='c'*64,
        preparation='d'*64,transition='e'*64,runtime='f'*64,review='0'*64,previous=None)
    original=os.write;calls=[]
    def short(file,data):
        calls.append(len(data));return original(file,data[:3])
    try:
        with mock.patch.object(os,'write',short):saved=owner._publish(fd,value,None)
        assert len(calls)>1 and owner._decision(fd)==saved
        target=tmp_path/'decision.json';target.write_bytes(target.read_bytes()[:-1]+b',"schema":1}')
        with pytest.raises(n.Blocked):owner._decision(fd)
    finally:os.close(fd)


@pytest.mark.parametrize('boundary',['content_info','current_proof','page_descriptor','close_view'])
def test_all_unavailable_content_evidence_invalidates_server_challenge(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    method='saved_descriptor' if boundary=='page_descriptor' else '_verify_view' if boundary=='close_view' else boundary
    if boundary in ('page_descriptor','close_view'):view=owner.open_content(row=0,side='before',**ref)
    with mock.patch.object(owner if boundary=='close_view' else owner.c,method,side_effect=OSError('transient evidence unavailable')):
        with pytest.raises(OSError):
            if boundary=='page_descriptor':owner.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)
            elif boundary=='close_view':owner.close_content(context=CONTEXT)
            else:owner.open_content(row=0,side='before',**ref)
    assert not owner._challenges
    with pytest.raises(n.Conflict):owner.decide(action='approve',**ref)
    assert owner.diagnose()['decision'] is None


@pytest.mark.parametrize('boundary',['review-content-retire','review-content-retire-fsync','review-receipt-retire','review-receipt-retire-fsync'])
def test_interrupted_owned_retirement_is_explicitly_settled_after_restart(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    def crash(event):
        if event==boundary:raise OSError('interrupted owned retirement')
    owner.trace=crash
    with pytest.raises(OSError):owner.close_content(context=CONTEXT)
    reopened=r.Review(c.Candidate(p));assert reopened._view is None
    ref2=challenge(reopened);new=reopened.open_content(row=0,side='before',**ref2)
    assert reopened.content_page(view_id=new['view_id'],offset=new['size'],context=CONTEXT)['eof']
    reopened.close_content(context=CONTEXT)
    if owner._view:os.close(owner._view['fd']);owner._view=None


@pytest.mark.parametrize('kind',['corrupt','symlink','hardlink','replaced-spool'])
def test_completed_receipt_corruption_never_adopted_or_deleted(tmp_path,kind):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    receipt=next((root/os.fsdecode(r.STORE)).glob('completed-*.json'))
    if kind=='corrupt':receipt.write_bytes(b'{}')
    elif kind=='symlink':
        receipt.rename(receipt.with_suffix('.foreign'));receipt.symlink_to(receipt.with_suffix('.foreign'))
    elif kind=='hardlink':os.link(receipt,receipt.with_suffix('.foreign'))
    else:
        spool=root/os.fsdecode(r.STORE)/owner._view['name'];spool.unlink();spool.write_bytes(b'foreign');spool.chmod(0o600)
    reopened=r.Review(c.Candidate(p))
    # Unsafe metadata can refuse review itself; valid-looking corrupt controls refuse explicit open.
    with pytest.raises((n.Blocked,d.Blocked,OSError,TypeError)):
        reviewed=reopened.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
        ref2=dict(ref,review_id=reviewed['review_id']);reopened.open_content(row=0,side='before',**ref2)
    assert receipt.exists() or receipt.is_symlink()
    os.close(owner._view['fd']);owner._view=None


@pytest.mark.parametrize('boundary',['review-receipt-write','review-receipt-file-fsync','review-receipt-replace','review-receipt-directory-fsync'])
def test_receipt_publication_failure_never_exposes_view_or_authority(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    def crash(event):
        if event==boundary:raise OSError('receipt interrupted')
    owner.trace=crash
    with pytest.raises(OSError):owner.open_content(row=0,side='before',**ref)
    assert owner._view is None and not owner._challenges
    reopened=r.Review(c.Candidate(p));assert reopened._view is None and reopened.diagnose()['review_id'] is None
    if boundary in ('review-receipt-replace','review-receipt-directory-fsync'):
        ref2=challenge(reopened);new=reopened.open_content(row=0,side='before',**ref2)
        assert reopened.content_page(view_id=new['view_id'],offset=new['size'],context=CONTEXT)['eof']
        reopened.close_content(context=CONTEXT)


def test_completed_control_identity_and_interrupted_retirement_protocol(tmp_path):
    from types import SimpleNamespace
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)));tmp_path.chmod(0o700)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json'
    file=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd);os.write(file,b'full bytes');os.fsync(file);info=r._stat(os.fstat(file));os.close(file)
    qname='retire-'+'a'*32;os.mkdir(qname,0o700,dir_fd=fd);qinfo=os.stat(qname,dir_fd=fd)
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=10,
        sha256=hashlib.sha256(b'full bytes').hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    try:
        receipt=owner._save_receipt(fd,control,value,None)
        assert owner._completed(fd,'b'*64,'c'*64)==[(control,receipt)]
        # An older owned candidate is eligible only for cache retirement, never byte reuse.
        assert owner._completed(fd,'e'*64,'f'*64)==[(control,receipt)]
        owner.trace=lambda event: (_ for _ in ()).throw(OSError('crash')) if event=='review-content-retire' else None
        with pytest.raises(OSError):owner._retire_receipt(fd,control,receipt)
        child=os.open(qname,os.O_RDONLY|os.O_DIRECTORY,dir_fd=fd)
        try:retiring=owner._receipt(child,'control',store_fd=fd,logical=control)
        finally:os.close(child)
        assert retiring['status']=='complete'
        assert not (tmp_path/name).exists()
        owner.trace=lambda event:None;owner._retire_receipt(fd,control,retiring)
        assert not (tmp_path/control).exists()
    finally:os.close(fd)


def test_same_bytes_completed_control_inode_replacement_refused(tmp_path):
    from types import SimpleNamespace
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)));tmp_path.chmod(0o700)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json'
    file=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd);info=r._stat(os.fstat(file));os.close(file)
    qname='retire-'+'a'*32;os.mkdir(qname,0o700,dir_fd=fd);qinfo=os.stat(qname,dir_fd=fd)
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=0,
        sha256=hashlib.sha256(b'').hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    try:
        owner._save_receipt(fd,control,value,None)
        raw=(tmp_path/control).read_bytes();(tmp_path/control).rename(tmp_path/'old')
        (tmp_path/control).write_bytes(raw);(tmp_path/control).chmod(0o600)
        with pytest.raises(n.Blocked):owner._receipt(fd,control)
    finally:os.close(fd)


def test_interrupted_retirement_pending_is_preserved_while_fresh_review_works(tmp_path):
    payload=b'\x00\xff'*32769+b'final'
    def configure(root,repo):
        write(repo,b'homeassistant/binary',payload);git(repo,'add','.');git(repo,'commit','-qm','pending crash binary')
    root,repo,p,owner,value=setup_review(tmp_path,configure=configure);ref=challenge(owner)
    row=next(i for i,x in enumerate(value['rows']) if x['after'] and n.decode_path(x['after']['path'])==b'homeassistant/binary')
    owner.open_content(row=row,side='after',**ref)
    def crash(event):
        if event=='review-retirement-capture-fsync':raise OSError('retirement capture interruption')
    owner.trace=crash
    with pytest.raises(OSError):owner.close_content(context=CONTEXT)
    pending=root/os.fsdecode(r.STORE)/('.pending-'+'f'*32)
    pending.write_bytes(b'retained unrelated pending');pending.chmod(0o600)
    before=(r._stat(pending.stat()),pending.read_bytes())
    reopened=r.Review(c.Candidate(p));diagnostic=reopened.diagnose()
    assert reopened._view is None and diagnostic['review_id'] is None and pending.name in diagnostic['retained']
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    ref2=challenge(reopened);view=reopened.open_content(row=row,side='after',**ref2)
    pages=[reopened.content_page(view_id=view['view_id'],offset=i,context=CONTEXT) for i in range(0,len(payload),r.PAGE)]
    assert b''.join(base64.b64decode(page['data']) for page in pages)==payload and pages[-1]['eof']
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    reopened.close_content(context=CONTEXT)
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    os.close(owner._view['fd']);owner._view=None


@pytest.mark.parametrize('target_kind',['payload','receipt'])
@pytest.mark.parametrize('boundary',['rename','private-unlink'])
@pytest.mark.parametrize('entrypoint',['replacement','close'])
def test_retirement_capture_preserves_canonical_replacements(tmp_path,target_kind,boundary,entrypoint):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import native_migration_transition as transition
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)));tmp_path.chmod(0o700)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json';qname='retire-'+'a'*32
    os.mkdir(qname,0o700,dir_fd=fd);qinfo=os.stat(qname,dir_fd=fd)
    file=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd);os.write(file,b'owned');os.fsync(file);info=r._stat(os.fstat(file))
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=5,
        sha256=hashlib.sha256(b'owned').hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    receipt=owner._save_receipt(fd,control,value,None)
    target=name if target_kind=='payload' else control;private='payload' if target_kind=='payload' else 'control';swapped=[]
    rename=transition._rename_noreplace;unlink=os.unlink
    def foreign():
        if (tmp_path/target).exists():os.rename(target,target+'.original',src_dir_fd=fd,dst_dir_fd=fd)
        other=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd);os.write(other,b'foreign-user-evidence');os.close(other);swapped.append(True)
    def capture(left,source,right,destination):
        if os.fsdecode(source)==target and boundary=='rename' and not swapped:foreign()
        return rename(left,source,right,destination)
    def retire(path,*args,**kwargs):
        if path==private and boundary=='private-unlink' and not swapped:foreign()
        return unlink(path,*args,**kwargs)
    owner._view=dict(fd=file,name=name,stat=info,context=r._context(CONTEXT))
    try:
        with mock.patch.object(transition,'_rename_noreplace',capture),mock.patch.object(os,'unlink',retire),pytest.raises(n.Blocked):
            if entrypoint=='close':
                with mock.patch.object(owner,'_admission',lambda *args:nullcontext()),mock.patch.object(owner,'_store',lambda **kw:nullcontext(fd)):
                    owner.close_content(context=CONTEXT)
            else:owner._retire_receipt(fd,control,receipt)
        assert swapped and (tmp_path/target).read_bytes()==b'foreign-user-evidence'
    finally:os.close(file);os.close(fd)


@pytest.mark.parametrize('capture_number',[1,2])
def test_retirement_quarantine_capture_crash_recovers_without_authority(tmp_path,capture_number):
    from types import SimpleNamespace
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)));tmp_path.chmod(0o700)
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json';qname='retire-'+'a'*32
    os.mkdir(qname,0o700,dir_fd=fd);qinfo=os.stat(qname,dir_fd=fd)
    file=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd);os.write(file,b'owned');os.fsync(file);info=r._stat(os.fstat(file));os.close(file)
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=5,
        sha256=hashlib.sha256(b'owned').hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    receipt=owner._save_receipt(fd,control,value,None);captures=[]
    def crash(event):
        if event=='review-retirement-capture-fsync':
            captures.append(event)
            if len(captures)==capture_number:raise OSError('capture crash')
    owner.trace=crash
    try:
        with pytest.raises(OSError):owner._retire_receipt(fd,control,receipt)
        reopened=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)))
        assert reopened._view is None and not reopened._challenges
        pairs=reopened._completed(fd,'b'*64,'c'*64);assert len(pairs)==1
        for logical,retained in pairs:reopened._retire_receipt(fd,logical,retained)
        assert reopened._completed(fd,'b'*64,'c'*64)==[]
        assert (tmp_path/qname).is_dir() and list((tmp_path/qname).iterdir())==[]
    finally:os.close(fd)
