"""Real candidate, complete review and explicit diagnostic decision contracts."""
import base64
import errno
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

@pytest.fixture(autouse=True)
def close_review_instances(monkeypatch):
    owners=[];original=r.Review.__init__
    def init(owner,*args,**kwargs):original(owner,*args,**kwargs);owners.append(owner)
    monkeypatch.setattr(r.Review,'__init__',init)
    yield owners
    for owner in owners:
        for key,view in list(owner._views.items()):
            if 'fd' not in view:owner._views.pop(key)
        owner.shutdown()


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
    target=root/os.fsdecode(r.STORE)/owner._views.get(r._context(CONTEXT))['name']
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
    assert diagnostic['retained'] and not reopened._views and diagnostic['review_id'] is None
    with pytest.raises(n.Conflict):reopened.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)
    with pytest.raises(n.Conflict):reopened.open_content(row=0,side='before',**ref)
    ref2=challenge(reopened)
    new=reopened.open_content(row=0,side='before',**ref2)
    page=reopened.content_page(view_id=new['view_id'],offset=new['size'],context=CONTEXT)
    assert page['eof'] and page['data']=='' and new['view_id']!=view['view_id']
    assert len(list((root/os.fsdecode(r.STORE)).glob('view-*')))==1
    assert len(list((root/os.fsdecode(r.STORE)).glob('completed-*.json')))==1
    reopened.close_content(context=CONTEXT)
    owner.shutdown()


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
    assert not owner._views
    assert not list((root/os.fsdecode(r.STORE)).glob('view-*'))
    owner.trace=lambda event:None
    retry=challenge(owner);owner.open_content(row=0,side='before',**retry)
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
    reopened=r.Review(c.Candidate(p));assert not reopened._views
    ref2=challenge(reopened);new=reopened.open_content(row=0,side='before',**ref2)
    assert reopened.content_page(view_id=new['view_id'],offset=new['size'],context=CONTEXT)['eof']
    reopened.close_content(context=CONTEXT)
    if owner._views.get(r._context(CONTEXT)):owner.shutdown()


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
        spool=root/os.fsdecode(r.STORE)/owner._views.get(r._context(CONTEXT))['name'];spool.unlink();spool.write_bytes(b'foreign');spool.chmod(0o600)
    reopened=r.Review(c.Candidate(p))
    if kind in ('corrupt','replaced-spool'):
        grant=reopened.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
        assert grant['allowed_actions']==['refuse','dismiss'] and not grant['content_available']
    else:
        with pytest.raises((n.Blocked,d.Blocked,OSError,TypeError)):
            reopened.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
        assert not reopened._challenges
    assert receipt.exists() or receipt.is_symlink()
    owner.shutdown()


@pytest.mark.parametrize('action',['approve','refuse','dismiss'])
def test_invalid_completed_evidence_after_review_only_blocks_approval(tmp_path,action):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    owner.open_content(row=0,side='before',**ref)
    receipt=next((root/os.fsdecode(r.STORE)).glob('completed-*.json'))
    receipt.write_bytes(b'{}')
    before={str(x.relative_to(root)):x.read_bytes() for x in receipt.parent.rglob('*') if x.is_file()}
    if action=='approve':
        with pytest.raises((n.Blocked,OSError,ValueError)):owner.decide(action=action,**ref)
        assert not (receipt.parent/'decision.json').exists()
    else:
        result=owner.decide(action=action,**ref)
        assert result['decision']['action']==action and result['required'] and not result['decision_authority']
    assert not owner._challenges
    after={str(x.relative_to(root)):x.read_bytes() for x in receipt.parent.rglob('*') if x.is_file() and x.name!='decision.json'}
    assert before==after
    owner.shutdown()


@pytest.mark.parametrize('boundary',['review-receipt-open','review-receipt-write','review-receipt-file-fsync','review-receipt-replace','review-receipt-directory-fsync'])
def test_receipt_publication_failure_never_exposes_view_or_authority(tmp_path,boundary):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    def crash(event):
        if event==boundary:raise OSError('receipt interrupted')
    owner.trace=crash
    with pytest.raises(OSError):owner.open_content(row=0,side='before',**ref)
    assert not owner._views and not owner._challenges
    reopened=r.Review(c.Candidate(p));assert not reopened._views and reopened.diagnose()['review_id'] is None
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
    assert not reopened._views and diagnostic['review_id'] is None and pending.name in diagnostic['retained']
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    ref2=challenge(reopened);view=reopened.open_content(row=row,side='after',**ref2)
    pages=[reopened.content_page(view_id=view['view_id'],offset=i,context=CONTEXT) for i in range(0,len(payload),r.PAGE)]
    assert b''.join(base64.b64decode(page['data']) for page in pages)==payload and pages[-1]['eof']
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    reopened.close_content(context=CONTEXT)
    assert (r._stat(pending.stat()),pending.read_bytes())==before
    owner.shutdown()


@pytest.mark.parametrize('target_kind',['payload','receipt'])
@pytest.mark.parametrize('boundary',['rename','private-unlink'])
@pytest.mark.parametrize('entrypoint',['replacement','close'])
def test_retirement_capture_preserves_canonical_replacements(tmp_path,target_kind,boundary,entrypoint):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import native_migration_transition as transition
    disk=SimpleNamespace(migration_review_cleanup_guard=lambda owner:nullcontext())
    timer=lambda delay,callback:SimpleNamespace(start=lambda:None,cancel=lambda:None)
    owner=r.Review(SimpleNamespace(p=SimpleNamespace(disk=disk),root=os.fsencode(tmp_path)),scheduler=timer);tmp_path.chmod(0o700)
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
    owner._views[r._context(CONTEXT)]=dict(fd=file,name=name,stat=info,context=r._context(CONTEXT),
        view_id='e'*64,review_id='f'*64,deadline=owner._clock()+r.VIEW_LEASE)
    try:
        with mock.patch.object(transition,'_rename_noreplace',capture),mock.patch.object(os,'unlink',retire),pytest.raises(n.Blocked):
            if entrypoint=='close':
                # Only lock/store plumbing is stubbed; _completed and the full
                # receipt/capture/unlink ownership proof remain production code.
                with mock.patch.object(owner,'_store',lambda **kw:nullcontext(fd)):
                    owner.close_content(context=CONTEXT,view_id='e'*64)
            else:owner._retire_receipt(fd,control,receipt)
        assert swapped and (tmp_path/target).read_bytes()==b'foreign-user-evidence'
    finally:
        owner.shutdown()  # close_content may already have consumed the view FD.
        with pytest.raises(OSError):os.fstat(file)
        os.close(fd)


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
        assert not reopened._views and not reopened._challenges
        pairs=reopened._completed(fd,'b'*64,'c'*64);assert len(pairs)==1
        for logical,retained in pairs:reopened._retire_receipt(fd,logical,retained)
        assert reopened._completed(fd,'b'*64,'c'*64)==[]
        assert (tmp_path/qname).is_dir() and list((tmp_path/qname).iterdir())==[]
    finally:os.close(fd)


@pytest.mark.parametrize('hash_active',[False,True])
@pytest.mark.parametrize('control_private',[False,True])
@pytest.mark.parametrize('payload_state',['canonical','private','absent','duplicate','corrupt','replaced','symlink','hardlink','mode','foreign'])
def test_completed_probe_validates_all_retirement_payload_locations(tmp_path,control_private,payload_state,hash_active):
    from types import SimpleNamespace
    owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)))
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json';qname='retire-'+'a'*32
    payload=tmp_path/name;payload.write_bytes(b'full bytes');payload.chmod(0o600)
    info=r._stat(payload.stat());(tmp_path/qname).mkdir(mode=0o700);qinfo=(tmp_path/qname).stat()
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=10,
        sha256=hashlib.sha256(b'full bytes').hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    try:
        receipt=owner._save_receipt(fd,control,value,None)
        if control_private:(tmp_path/control).rename(tmp_path/qname/'control')
        private=tmp_path/qname/'payload'
        if payload_state!='canonical':payload.rename(private)
        if payload_state=='absent':private.unlink()
        elif payload_state=='duplicate':payload.write_bytes(b'full bytes');payload.chmod(0o600)
        elif payload_state=='corrupt':
            private.write_bytes(b'evil bytes');os.utime(private,ns=(info[7],info[7]))
        elif payload_state=='replaced':private.unlink();private.write_bytes(b'full bytes');private.chmod(0o600)
        elif payload_state=='symlink':private.unlink();private.symlink_to(tmp_path/'missing')
        elif payload_state=='hardlink':os.link(private,tmp_path/qname/'foreign')
        elif payload_state=='mode':private.chmod(0o644)
        elif payload_state=='foreign':(tmp_path/qname/'foreign').write_bytes(b'foreign')
        before={str(x.relative_to(tmp_path)):r._stat(x.lstat()) for x in tmp_path.rglob('*')}
        valid=payload_state=='canonical' or control_private and payload_state in ('private','absent')
        if hash_active and control_private and payload_state=='corrupt':valid=True
        kwargs={'active_names':{name}} if hash_active else {}
        if valid:assert owner._completed(fd,'e'*64,'f'*64,**kwargs)==[(control,receipt)]
        else:
            with pytest.raises((n.Blocked,OSError)):owner._completed(fd,'e'*64,'f'*64,**kwargs)
        after={str(x.relative_to(tmp_path)):r._stat(x.lstat()) for x in tmp_path.rglob('*')}
        assert before==after
    finally:os.close(fd)


def test_uncertain_instance_refuses_every_new_authority_boundary(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    grants=owner._challenges.copy();owner._uncertain=True
    with pytest.raises(n.PersistenceUncertain):owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
    assert not owner._challenges
    for operation in ('decision','open'):
        owner._challenges=grants.copy()
        with pytest.raises(n.PersistenceUncertain):
            if operation=='decision':owner.decide(action='approve',**ref)
            else:owner.open_content(row=0,side='before',**ref)
        assert not owner._challenges
    assert not (root/os.fsdecode(r.STORE)).exists()


@pytest.mark.parametrize('boundary',['review-content-open','review-content-write','review-content-file-fsync','timeout','short-write'])
def test_caught_copy_failure_reclaims_only_pending_and_preserves_previous(tmp_path,boundary):
    import subprocess
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    first=owner.open_content(row=0,side='before',**ref)
    store=root/os.fsdecode(r.STORE);before={x.name:x.read_bytes() for x in store.glob('view-*')}
    def crash(event):
        if event==boundary:raise OSError('caught after syscall')
    owner.trace=crash
    with mock.patch.object(owner.c,'read_content',side_effect=lambda **kw: (kw['destination'].write(b'x'),(_ for _ in ()).throw(subprocess.TimeoutExpired('git',30)))) if boundary=='timeout' else mock.patch.object(os,'write',return_value=0) if boundary=='short-write' else __import__('contextlib').nullcontext():
        with pytest.raises((OSError,subprocess.TimeoutExpired)):owner.open_content(row=0,side='before',**ref)
    assert {x.name:x.read_bytes() for x in store.glob('view-*')}==before
    assert not list(store.glob('.pending-*'))
    assert owner.content_page(view_id=first['view_id'],offset=0,context=CONTEXT)['view_id']==first['view_id']
    owner.trace=lambda event:None
    reopened=r.Review(c.Candidate(p));fresh=challenge(reopened)
    replacement=reopened.open_content(row=0,side='before',**fresh)
    assert reopened.content_page(view_id=replacement['view_id'],offset=replacement['size'],context=CONTEXT)['eof']
    reopened.close_content(context=CONTEXT);owner.forget(context=CONTEXT)


def test_authenticated_context_views_remain_independent(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);a=challenge(owner)
    other=('ingress','http://fixture.invalid','/api/hassio_ingress/other','other-user')
    model=owner.diagnose()['model']
    b=dict(candidate=model['candidate'],scope=model['scope_digest'],context=other)
    av=owner.open_content(row=0,side='before',**a)
    b['review_id']=owner.review(**b)['review_id']
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    bv=owner.open_content(row=0,side='before',**b)
    assert len(owner._views)==2
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    with pytest.raises(n.Conflict):owner.content_page(view_id=av['view_id'],offset=0,context=other)
    assert r._context(CONTEXT) in owner._challenges
    b['review_id']=owner.review(candidate=b['candidate'],scope=b['scope'],context=other)['review_id']
    bv=owner.open_content(row=0,side='before',**b)
    owner.close_content(context=other)
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    b['review_id']=owner.review(candidate=b['candidate'],scope=b['scope'],context=other)['review_id']
    bv=owner.open_content(row=0,side='before',**b);held=owner._views[r._context(other)]['fd']
    owner.forget(context=other)
    with pytest.raises(OSError):os.fstat(held)
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    assert owner.decide(action='approve',**a)['decision']['action']=='approve'
    owner.close_content(context=CONTEXT)


@pytest.mark.parametrize('character',['é','€','😀'])
@pytest.mark.parametrize('split',[1,2,3])
def test_utf8_window_preserves_whole_boundary_characters_and_exact_bytes(tmp_path,character,split):
    encoded=character.encode()
    if split>=len(encoded):pytest.skip('not an interior boundary')
    payload=b'a'*(r.PAGE-split)+encoded+b'b'*(r.PAGE-len(encoded))+encoded+b'last'
    def configure(root,repo):
        write(repo,b'homeassistant/utf8',payload);git(repo,'add','.');git(repo,'commit','-qm','utf8')
    root,repo,p,owner,value=setup_review(tmp_path,configure=configure);ref=challenge(owner)
    row=next(i for i,x in enumerate(value['rows']) if x['after'] and n.decode_path(x['after']['path'])==b'homeassistant/utf8')
    view=owner.open_content(row=row,side='after',**ref)
    for offset in [r.PAGE,0,len(payload)-2,r.PAGE-1,2*r.PAGE]:
        page=owner.content_page(view_id=view['view_id'],offset=offset,context=CONTEXT)
        assert base64.b64decode(page['data'])==payload[offset:offset+r.PAGE]
        assert page['text'] is not None
        assert page['text'].encode()==payload[page['text_start']:page['text_end']]
        assert offset-3<=page['text_start']<=offset
        assert offset+len(base64.b64decode(page['data']))<=page['text_end']<=min(view['size'],offset+r.PAGE+3)
    assert character in owner.content_page(view_id=view['view_id'],offset=r.PAGE,context=CONTEXT)['text']
    owner.close_content(context=CONTEXT)


@pytest.mark.parametrize('payload,valid',[(b'\xef\xbb\xbfA',True),(b'A\xef\xbb\xbfB',True),(b'\xf0\x9f',False),(b'\xc0\x80',False),(b'\xed\xa0\x80',False),(b'\x80\x80\x80\x80',False)])
def test_text_window_strict_malformed_eof_and_bom(tmp_path,payload,valid):
    owner=object.__new__(r.Review)
    path=tmp_path/'payload';path.write_bytes(payload);fd=os.open(path,os.O_RDONLY)
    try:
        text,start,end=owner._text_window(dict(fd=fd,size=len(payload)),0,len(payload))
        assert (text is not None)==valid
        if valid:assert text.encode()==payload
        assert owner._text_window(dict(fd=fd,size=len(payload)),len(payload),0)==('',len(payload),len(payload))
    finally:os.close(fd)


@pytest.mark.parametrize('boundary',['receipt-create','review-receipt-open','review-receipt-write','review-receipt-file-fsync','receipt-write-zero'])
def test_prepublication_receipt_failure_preserves_old_view_and_fresh_retry(tmp_path,boundary):
    import errno
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    old=owner.open_content(row=0,side='before',**ref)
    original_open=os.open;original_write=os.write;promoted=False
    def trace(event):
        nonlocal promoted
        if event=='review-content-promote':promoted=True
        if event==boundary:raise OSError('receipt prepublication fault')
    def fail_open(path,flags,*args,**kw):
        if boundary=='receipt-create' and promoted and os.fsdecode(path).startswith('.pending-') and flags&os.O_CREAT:
            raise OSError(errno.ENOSPC,'receipt create ENOSPC')
        return original_open(path,flags,*args,**kw)
    def fail_write(*args,**kw):
        if boundary=='receipt-write-zero' and promoted:return 0
        return original_write(*args,**kw)
    owner.trace=trace
    with mock.patch.object(os,'open',fail_open),mock.patch.object(os,'write',fail_write),pytest.raises(OSError):
        owner.open_content(row=0,side='before',**ref)
    owner.trace=lambda event:None
    store=root/os.fsdecode(r.STORE)
    assert len(list(store.glob('view-*')))==len(list(store.glob('completed-*')))==1
    assert not list(store.glob('.pending-*'))
    assert owner.content_page(view_id=old['view_id'],offset=0,context=CONTEXT)['data']
    assert owner._uncertain is False
    reopened=r.Review(c.Candidate(p));fresh=challenge(reopened)
    replacement=reopened.open_content(row=0,side='before',**fresh)
    assert reopened.content_page(view_id=replacement['view_id'],offset=replacement['size'],context=CONTEXT)['eof']
    reopened.close_content(context=CONTEXT);owner.forget(context=CONTEXT)


@pytest.mark.parametrize('case',['unchanged','foreign-before','changed-candidate','changed-model','foreign-after'])
def test_binding_refresh_requires_previous_exact_pointer_and_current_model(case):
    from types import SimpleNamespace
    before=[1,2];after=[1,3];model={'scope':'same'};candidate='a'*64
    owner=r.Review(SimpleNamespace(p=None,root=b'fixture',saved_descriptor=lambda:after))
    view=dict(candidate_stat=before if case!='foreign-before' else [9,9],candidate=candidate,model=r._digest(model))
    owner._views['context']=view
    value={'candidate':candidate if case!='changed-candidate' else 'b'*64}
    current=model if case!='changed-model' else {'scope':'changed'}
    if case=='foreign-after':
        owner.c.saved_descriptor=lambda:[9,9]
        with pytest.raises(n.Conflict):owner._refresh_content_bindings((before,after),value,current)
    else:owner._refresh_content_bindings((before,after),value,current)
    assert (view['candidate_stat']==after)==(case=='unchanged')


@pytest.mark.parametrize('failure',['unavailable','stale','exception'])
def test_unsuccessful_revalidation_never_restores_content_binding(failure):
    from types import SimpleNamespace
    def revalidate():
        if failure=='exception':raise OSError('unavailable pointer')
        return failure
    owner=r.Review(SimpleNamespace(p=SimpleNamespace(read=lambda:{}),root=b'fixture',saved_descriptor=lambda:[1,2],
        read=lambda:{'candidate':'a'*64},revalidate=revalidate))
    owner._model=lambda *args:{'scope_digest':'b'*64}
    owner._views['context']={'candidate_stat':[1,2]};owner._challenges['context']={'token':'old'}
    with pytest.raises((n.Conflict,OSError)):owner._revalidate_content_bindings()
    assert owner._views['context']['candidate_stat'] is None and not owner._challenges


def test_same_context_review_and_other_context_decision_preserve_unchanged_view(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);a=challenge(owner)
    av=owner.open_content(row=0,side='before',**a)
    a=challenge(owner)
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    other=('ingress','http://fixture.invalid','/api/hassio_ingress/other','other-user')
    b={**a,'context':other};b.pop('review_id')
    b['review_id']=owner.review(**b)['review_id']
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    owner.decide(action='approve',**b)
    assert owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)['data']
    owner.close_content(context=CONTEXT)


def test_foreign_same_byte_candidate_pointer_is_not_rebound_by_explicit_review(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    av=owner.open_content(row=0,side='before',**ref)
    pointer=root/os.fsdecode(c.STORE)/'candidate.json';foreign=pointer.with_name('.pending-'+'e'*32)
    foreign.write_bytes(pointer.read_bytes());foreign.chmod(0o600);os.replace(foreign,pointer)
    challenge(owner)
    with pytest.raises(n.Conflict):owner.content_page(view_id=av['view_id'],offset=0,context=CONTEXT)
    owner.forget(context=CONTEXT)


@pytest.mark.parametrize('wrong',['candidate','scope'])
def test_wrong_review_identity_has_no_shared_revalidation_or_other_context_effect(wrong):
    from types import SimpleNamespace
    from contextlib import contextmanager
    candidate='a'*64;scope='b'*64;descriptor=[1,2]
    source=SimpleNamespace(p=SimpleNamespace(read=lambda:{}),root=b'fixture',read=lambda:{'candidate':candidate},
        saved_descriptor=lambda:descriptor,revalidate=mock.Mock(side_effect=AssertionError('invalid request revalidated')))
    owner=r.Review(source);owner._retire_inactive=lambda:None;owner._model=lambda *args:{'scope_digest':scope}
    a=r._context(CONTEXT);other=('ingress','http://fixture.invalid','/other','other-user')
    view=dict(view_id='d'*64,context=a,candidate_stat=descriptor,fd=123,deadline=owner._clock()+r.VIEW_LEASE,name='view-fixture',size=1,sha256='e'*64)
    owner._views[a]=view;owner._challenges[a]={'token':'retained'}
    @contextmanager
    def store():yield 456
    owner._store=store;owner._verify_view=lambda *args:None
    owner._text_window=lambda *args:('x',0,1)
    identity=dict(candidate=candidate,scope=scope);identity[wrong]='f'*64
    with pytest.raises(n.Conflict):owner.review(context=other,**identity)
    source.revalidate.assert_not_called()
    assert owner._views[a] is view and view['candidate_stat']==descriptor
    assert owner._challenges[a]=={'token':'retained'}
    with mock.patch.object(os,'pread',return_value=b'x'):
        assert owner.content_page(view_id=view['view_id'],offset=0,context=CONTEXT)['data']=='eA=='
    assert owner._challenges[a]=={'token':'retained'}
    owner._views.clear()


@pytest.mark.parametrize('method',['review','decide'])
@pytest.mark.parametrize('failure',['bytes','inodes','enospc','io','store'])
def test_later_request_failure_preserves_trusted_other_context_continuity(tmp_path,method,failure):
    import errno
    from types import SimpleNamespace
    from contextlib import contextmanager
    candidate='a'*64;scope='b'*64;descriptor=[1,2];model={'scope_digest':scope}
    saved=dict(candidate=candidate,validation='current',stale=False)
    def revalidate():descriptor[:]=[1,3];return 'current'
    source=SimpleNamespace(p=SimpleNamespace(read=lambda:{}),root=b'fixture',read=lambda:dict(saved),
        saved_descriptor=lambda:list(descriptor),revalidate=revalidate)
    owner=r.Review(source);owner._retire_inactive=lambda:None;owner._model=lambda *args:model
    a=r._context(CONTEXT);other=('ingress','http://fixture.invalid','/other','other-user');b=r._context(other)
    payload=tmp_path/'held';payload.write_bytes(b'x');fd=os.open(payload,os.O_RDONLY)
    owner._views[a]=dict(view_id='d'*64,context=a,candidate_stat=[1,2],candidate=candidate,model=r._digest(model),fd=fd,deadline=owner._clock()+r.VIEW_LEASE,name='view-held',size=1,sha256='e'*64)
    owner._challenges[a]={'token':'retained'}
    owner._challenges[b]=dict(token='c'*64,candidate=candidate,scope=scope,model=r._digest(model),previous=None,allowed_actions=['approve','refuse','dismiss'],content_available=True)
    @contextmanager
    def admission(*args):
        if failure=='bytes':raise d.Blocked('insufficient-review-bytes')
        if failure=='inodes':raise d.Blocked('insufficient-review-inodes')
        if failure=='enospc':raise OSError(errno.ENOSPC,'second admission')
        if failure=='io':raise OSError(errno.EIO,'second admission')
        yield
    @contextmanager
    def store(*args,**kwargs):
        if failure=='store':raise OSError(errno.EIO,'later store failure')
        yield 456
    owner._admission=admission;owner._store=store;owner._verify_view=lambda *args:None
    try:
        ref=dict(candidate=candidate,scope=scope,context=other)
        if method=='decide':ref.update(review_id='c'*64,action='approve')
        with pytest.raises((d.Blocked,OSError)):getattr(owner,method)(**ref)
        assert owner._views[a]['candidate_stat']==descriptor==[1,3]
        assert owner._challenges[a]=={'token':'retained'}
        @contextmanager
        def readable_store():yield 456
        owner._store=readable_store
        assert owner.content_page(view_id='d'*64,offset=0,context=CONTEXT)['data']=='eA=='
        assert owner._challenges[a]=={'token':'retained'}
    finally:owner.shutdown()


@pytest.mark.parametrize('failure',['candidate','model','pending','foreign-after'])
def test_trusted_continuity_refuses_semantic_or_pointer_change(failure):
    from types import SimpleNamespace
    descriptor=[1,2];phase=False;candidate='a'*64
    def revalidate():
        nonlocal phase
        phase=True;descriptor[:]=[1,3];return 'current'
    def read():return dict(candidate='c'*64 if phase and failure=='candidate' else candidate,
        validation='pending' if phase and failure=='pending' else 'current',stale=False)
    source=SimpleNamespace(p=SimpleNamespace(read=lambda:{}),root=b'fixture',saved_descriptor=lambda:list(descriptor),read=read,revalidate=revalidate)
    owner=r.Review(source)
    def model(*args):
        if phase and failure=='foreign-after':descriptor[:]=[9,9]
        return {'scope':'changed' if phase and failure=='model' else 'same'}
    owner._model=model;owner._views['a']={'candidate_stat':[1,2]};owner._challenges['a']={'token':'old'}
    with pytest.raises(n.Conflict):owner._revalidate_content_bindings()
    assert owner._views['a']['candidate_stat'] is None and not owner._challenges


class LeaseTimer:
    def __init__(self,delay,call):self.delay=delay;self.call=call;self.cancelled=False;self.started=False
    def start(self):self.started=True
    def cancel(self):self.cancelled=True


def test_lease_exact_identity_idle_scheduler_and_gc(tmp_path,close_review_instances):
    import gc,weakref
    from types import SimpleNamespace
    now=[0];timers=[]
    def scheduler(delay,call):timer=LeaseTimer(delay,call);timers.append(timer);return timer
    source=SimpleNamespace(p=SimpleNamespace(),root=b'fixture',saved_descriptor=lambda:[1])
    owner=r.Review(source,clock=lambda:now[0],scheduler=scheduler)
    owner._retire_inactive=lambda:None;owner._verify_view=lambda *a:None
    from contextlib import contextmanager
    @contextmanager
    def store():yield 1
    owner._store=store
    def view(ctx,token):
        path=tmp_path/token;path.write_bytes(b'abc');fd=os.open(path,os.O_RDONLY)
        owner._views[r._context(ctx)]=dict(fd=fd,view_id=token,deadline=300,context=r._context(ctx),candidate_stat=[1],review_id='grant')
        return fd
    other=('direct','http://fixture','other')
    a=view(CONTEXT,'a'*64);b=view(other,'b'*64);owner._schedule();assert len(timers)==1 and timers[0].delay==30
    now[0]=299;owner.renew_content(context=other,view_id='b'*64)
    with pytest.raises(n.Conflict):owner.renew_content(context=CONTEXT,view_id='c'*64)
    now[0]=300;timers[0].call()
    with pytest.raises(OSError):os.fstat(a)
    assert os.fstat(b) and len(timers)==2
    with pytest.raises(n.Conflict):owner.renew_content(context=CONTEXT,view_id='a'*64)
    with pytest.raises(n.Conflict):owner.close_content(context=other,view_id='a'*64)
    assert os.fstat(b)
    owner.shutdown();assert timers[-1].cancelled
    timers[-1].call();assert len(timers)==2
    with pytest.raises(OSError):os.fstat(b)
    garbage=r.Review(source,clock=lambda:now[0],scheduler=scheduler)
    close_review_instances.remove(garbage)
    payload=tmp_path/'gc-held';payload.write_bytes(b'x');fd=os.open(payload,os.O_RDONLY)
    garbage._views['gc']={'fd':fd};garbage._schedule();reference=weakref.ref(garbage)
    del garbage;gc.collect()
    assert reference() is None
    with pytest.raises(OSError):os.fstat(fd)
    timers[-1].call()


def test_lease_timer_failure_closes_handles(tmp_path):
    from types import SimpleNamespace
    class Broken(LeaseTimer):
        def start(self):raise OSError('timer failed')
    owner=r.Review(SimpleNamespace(p=None,root=b'fixture'),scheduler=Broken)
    path=tmp_path/'held';path.write_bytes(b'x');fd=os.open(path,os.O_RDONLY)
    owner._views['ctx']={'fd':fd}
    with pytest.raises(OSError,match='timer failed'):owner._schedule()
    assert not owner._views
    with pytest.raises(OSError):os.fstat(fd)


def test_cleanup_guard_precedes_admission_and_preserves_active_context(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    now=[0.];owner._clock=lambda:now[0];timers=[]
    owner._scheduler=lambda delay,call:timers.append(LeaseTimer(delay,call)) or timers[-1]
    a=owner.open_content(row=0,side='before',**ref);afd=owner._views[r._context(CONTEXT)]['fd']
    a_name=owner._views[r._context(CONTEXT)]['name']
    other=('direct','http://fixture.invalid','other')
    model=owner.diagnose()['model'];grant=owner.review(candidate=model['candidate'],scope=model['scope_digest'],context=other)
    br=dict(candidate=model['candidate'],scope=model['scope_digest'],review_id=grant['review_id'],context=other)
    b=owner.open_content(row=0,side='before',**br)
    now[0]=299;owner.renew_content(view_id=b['view_id'],context=other);now[0]=300
    original=p.disk.migration_review_guard;observed=[]
    from contextlib import contextmanager
    @contextmanager
    def guard(recipe):
        assert not (root/os.fsdecode(r.STORE)/a_name).exists()
        with pytest.raises(OSError):os.fstat(afd)
        assert len(list((root/os.fsdecode(r.STORE)).glob('completed-*.json')))==1
        observed.append(True)
        with original(recipe):yield
    with mock.patch.object(p.disk,'migration_review_guard',guard):
        # Review uses ordinary admission only after actual stale retirement.
        owner.review(candidate=model['candidate'],scope=model['scope_digest'],context=CONTEXT)
    assert observed
    assert owner.content_page(view_id=b['view_id'],offset=0,context=other)['view_id']==b['view_id']


def test_corrupt_cache_fresh_safe_review_never_upgrades(tmp_path):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    view=owner.open_content(row=0,side='before',**ref)
    held=owner._views[r._context(CONTEXT)]
    payload=root/os.fsdecode(r.STORE)/held['name'];payload.write_bytes(b'corrupt-cache');payload.chmod(0o600)
    owner.forget(context=CONTEXT)
    previous_bytes=payload.read_bytes()
    for action in ('refuse','dismiss'):
        grant=owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
        assert grant['allowed_actions']==['refuse','dismiss'] and grant['content_available'] is False
        safe={**ref,'review_id':grant['review_id']}
        with pytest.raises(n.Conflict):owner.open_content(row=0,side='before',**safe)
        # A failed content attempt consumes authority, so obtain a fresh exact review.
        safe['review_id']=owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)['review_id']
        with pytest.raises(n.Conflict):owner.decide(action='approve',**safe)
        safe['review_id']=owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)['review_id']
        result=owner.decide(action=action,**safe)
        assert result['required'] and not result['decision_authority'] and result['decision']['action']==action
        assert payload.read_bytes()==previous_bytes
    # A syntactically valid but unproved receipt also permits only safe actions.
    receipt=next(payload.parent.glob('completed-*.json'));receipt.write_bytes(b'{}')
    grant=owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
    assert grant['allowed_actions']==['refuse','dismiss'] and not grant['content_available']
    assert owner.decide(action='dismiss',**{**ref,'review_id':grant['review_id']})['required']
    assert receipt.read_bytes()==b'{}' and payload.read_bytes()==previous_bytes


def test_expiry_close_failure_does_not_retain_other_expired_descriptors(tmp_path):
    from types import SimpleNamespace
    now=[300.];timers=[]
    owner=r.Review(SimpleNamespace(p=None,root=b'fixture'),clock=lambda:now[0],scheduler=lambda delay,call:timers.append(LeaseTimer(delay,call)) or timers[-1])
    descriptors=[]
    for index in range(3):
        path=tmp_path/str(index);path.write_bytes(b'x');fd=os.open(path,os.O_RDONLY);descriptors.append(fd)
        owner._views[str(index)]={'fd':fd,'deadline':300 if index<2 else 600,'review_id':'old'}
    os.close(descriptors[0])
    def failed_cleanup():owner._cleanup_debt=True;owner.cleanup_diagnostic='retained foreign evidence'
    owner._retire_inactive=failed_cleanup
    owner._sweep()
    assert list(owner._views)==['2'] and owner.cleanup_diagnostic=='retained foreign evidence'
    with pytest.raises(OSError):os.fstat(descriptors[1])
    assert os.fstat(descriptors[2])
    with mock.patch.object(os,'pread',side_effect=AssertionError('expired bytes read')):
        with pytest.raises(n.Conflict):owner.content_page(view_id='a'*64,offset=0,context=CONTEXT)


@pytest.mark.parametrize('phase',['weakref','constructor','daemon','start','cancel'])
def test_all_scheduler_setup_failures_release_owned_handles(tmp_path,phase):
    from types import SimpleNamespace
    class FailedTimer:
        def __init__(self,delay,callback):self.callback=callback
        def __setattr__(self,name,value):
            if name=='daemon' and phase=='daemon':raise MemoryError('daemon setup')
            object.__setattr__(self,name,value)
        def start(self):raise MemoryError('start')
        def cancel(self):
            if phase=='cancel':raise OSError('cancel failed')
    source=SimpleNamespace(p=None,root=os.fsencode(tmp_path))
    owner=r.Review(source,**({} if phase=='constructor' else {'scheduler':FailedTimer}))
    fds=[]
    for index in range(2):
        path=tmp_path/str(index);path.write_bytes(b'x');fd=os.open(path,os.O_RDONLY);fds.append(fd)
        owner._views[str(index)]={'fd':fd};owner._challenges[str(index)]={'token':'old'}
    patch=(mock.patch.object(r.weakref,'ref',side_effect=MemoryError('weakref setup')) if phase=='weakref' else
           mock.patch.object(r.threading,'Lock',side_effect=MemoryError('constructor')) if phase=='constructor' else __import__('contextlib').nullcontext())
    with patch,pytest.raises(MemoryError):owner._schedule()
    assert owner._timer is None and not owner._views and not owner._challenges
    for fd in fds:
        with pytest.raises(OSError):os.fstat(fd)
    owner._schedule();assert owner._timer is None


def test_review_cleanup_guard_close_error_releases_local_lock(tmp_path):
    import errno
    from types import SimpleNamespace
    (tmp_path/'data').mkdir();lock=tmp_path/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    disk=d.NativeDisk.__new__(d.NativeDisk);disk.root=os.fsencode(tmp_path);disk._active_operation=None
    owner=r.Review(SimpleNamespace(p=SimpleNamespace(disk=disk),root=disk.root))
    original_open,original_close=os.open,os.close;held=[None];failed=[False]
    def opened(name,*args,**kwargs):
        fd=original_open(name,*args,**kwargs)
        if name==d.ADMISSION_LOCK.split(b'/')[-1]:held[0]=fd
        return fd
    def closed(fd):
        original_close(fd)
        if fd==held[0] and not failed[0]:failed[0]=True;raise OSError(errno.EIO,'close reported failure')
    with mock.patch.object(os,'open',opened),mock.patch.object(os,'close',closed),pytest.raises(OSError) as error:
        with disk.migration_review_cleanup_guard(owner):pass
    assert error.value.errno==errno.EIO and disk._active_operation is None
    assert not d._THREAD_LOCKS[disk.root+b'/'+d.ADMISSION_LOCK].locked()
    with disk.migration_review_cleanup_guard(owner):pass


@pytest.mark.parametrize('boundary',['shutdown','idle-sweep'])
def test_real_timer_cancel_error_detaches_and_finishes_lifecycle(tmp_path,boundary):
    from types import SimpleNamespace
    now=[0.];owner=r.Review(SimpleNamespace(p=None,root=os.fsencode(tmp_path)),clock=lambda:now[0]);owner._retire_inactive=lambda:None
    fds=[]
    for index in range(2):
        path=tmp_path/str(index);path.write_bytes(b'x');fd=os.open(path,os.O_RDONLY);fds.append(fd)
        owner._views[str(index)]={'fd':fd,'deadline':300,'review_id':'old'};owner._challenges[str(index)]={'token':'old'}
    owner._schedule();old=owner._timer;cancel=old.cancel;callback=old.function
    def cancel_error():cancel();raise OSError('cancel after release')
    old.cancel=cancel_error
    try:
        if boundary=='shutdown':
            with pytest.raises(OSError):owner.shutdown()
            owner.shutdown()
        else:
            now[0]=300
            with pytest.raises(OSError):owner.sweep_content()
        old.join(1)
        assert owner._timer is None and not owner._views and not owner._challenges
        for fd in fds:
            with pytest.raises(OSError):os.fstat(fd)
        callback();assert owner._timer is None
        if boundary=='idle-sweep':
            path=tmp_path/'new';path.write_bytes(b'x');fd=os.open(path,os.O_RDONLY)
            owner._views['new']={'fd':fd,'deadline':600,'review_id':'new'};owner._schedule();new=owner._timer
            assert new is not old and new.is_alive()
            callback();assert owner._timer is new and os.fstat(fd)
            owner.shutdown();new.join(1)
            with pytest.raises(OSError):os.fstat(fd)
    finally:
        cancel();old.join(1);owner.shutdown()


@pytest.fixture
def cleanup_receipt(tmp_path):
    """Genuine immutable controls and payload; only store/lock plumbing is isolated."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    now=[0.];timers=[]
    disk=SimpleNamespace(migration_review_cleanup_guard=lambda owner:nullcontext())
    owner=r.Review(SimpleNamespace(p=SimpleNamespace(disk=disk),root=os.fsencode(tmp_path)),
        clock=lambda:now[0],scheduler=lambda delay,call:timers.append(LeaseTimer(delay,call)) or timers[-1])
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    name='view-'+'a'*32;control='completed-'+'a'*32+'.json';qname='retire-'+'a'*32
    payload=tmp_path/name;payload.write_bytes(b'full payload bytes'*2000);payload.chmod(0o600)
    info=r._stat(payload.stat());(tmp_path/qname).mkdir(mode=0o700);qinfo=(tmp_path/qname).stat()
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=name,
        candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=info[6],
        sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    receipt=owner._save_receipt(fd,control,value,None)
    owner._store=lambda **kw:nullcontext(fd)
    try:yield owner,fd,payload,control,receipt,now,timers
    finally:owner.shutdown();os.close(fd)


def payload_reads(monkeypatch,identity):
    total=[0];pread=os.pread
    def read(fd,size,offset):
        data=pread(fd,size,offset);info=os.fstat(fd)
        if (info.st_dev,info.st_ino)==identity:total[0]+=len(data)
        return data
    monkeypatch.setattr(os,'pread',read)
    return total


@pytest.mark.parametrize('control_private',[False,True])
def test_cleanup_active_payload_checks_without_hashing(cleanup_receipt,monkeypatch,control_private):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    if control_private:payload.parent.joinpath(control).rename(payload.parent/receipt['quarantine'][0]/'control')
    info=payload.stat();reads=payload_reads(monkeypatch,(info.st_dev,info.st_ino))
    owner._views['healthy']=dict(name=payload.name,fd=os.open(payload,os.O_RDONLY),deadline=300,review_id='grant')
    owner._retire_inactive()
    assert reads[0]==0 and not owner._cleanup_debt and owner.cleanup_diagnostic is None
    assert owner._completed(fd,None,None)==[(control,receipt)]
    assert reads[0]==info.st_size


@pytest.mark.parametrize('entrypoint',['review','open'])
def test_explicit_request_hashes_existing_active_payload_once(tmp_path,monkeypatch,entrypoint):
    payload=b'multi-page content'*3000
    def configure(root,repo):
        write(repo,b'homeassistant/read-count',payload)
        git(repo,'add','.');git(repo,'commit','-qm','read-count')
    root,repo,p,owner,value=setup_review(tmp_path,configure=configure);ref=challenge(owner)
    row=next(i for i,x in enumerate(value['rows']) if x['after'] and n.decode_path(x['after']['path'])==b'homeassistant/read-count')
    owner.open_content(row=row,side='after',**ref)
    held=owner._views[r._context(CONTEXT)];info=os.fstat(held['fd'])
    assert info.st_size>2*r.PAGE
    other=('direct','http://fixture.invalid','other')
    other_grant=owner.review(candidate=ref['candidate'],scope=ref['scope'],context=other)
    reads=payload_reads(monkeypatch,(info.st_dev,info.st_ino))
    if entrypoint=='review':owner.review(candidate=ref['candidate'],scope=ref['scope'],context=other)
    else:owner.open_content(row=row,side='after',**{**ref,'context':other,'review_id':other_grant['review_id']})
    assert reads[0]==info.st_size
    assert os.fstat(held['fd'])


def test_terminal_hash_failure_stops_repeated_reads_but_expires_healthy_view(cleanup_receipt,monkeypatch):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    # Preserve the receipt's own inode and the payload stat: this must reach SHA.
    bad={**receipt,'sha256':'0'*64};bad['digest']=r._digest({k:v for k,v in bad.items() if k!='digest'})
    payload.parent.joinpath(control).write_bytes(r._json(bad))
    info=payload.stat();reads=payload_reads(monkeypatch,(info.st_dev,info.st_ino))
    held,writer=os.pipe();os.close(writer)
    owner._views['healthy']=dict(name='view-'+'e'*32,fd=held,deadline=300,review_id='grant')
    owner._challenges['healthy']={'token':'grant'}
    owner._sweep(reclaim=True)
    assert reads[0]>=info.st_size and not owner._cleanup_debt
    error=owner._cleanup_error;diagnostic=owner.cleanup_diagnostic;initial=reads[0]
    assert 'corrupt' in diagnostic and payload.exists()
    for tick in (30,60,90):
        now[0]=tick;timers[-1].call()
        assert reads[0]==initial and owner._cleanup_error is error and owner.cleanup_diagnostic==diagnostic
        assert os.fstat(held) and owner._timer is timers[-1]
    # Explicit reclaim retries and still refuses strict evidence.
    owner._sweep(reclaim=True);assert reads[0]==initial+info.st_size
    now[0]=300;timers[-1].call()
    assert not owner._views and not owner._challenges and owner._timer is None and not owner._cleanup_debt
    with pytest.raises(OSError):os.fstat(held)
    # Repair same owned control; a future explicit request can settle it.
    payload.parent.joinpath(control).write_bytes(r._json(receipt))
    owner._sweep(reclaim=True)
    assert owner.cleanup_diagnostic is None and not owner._cleanup_debt and not payload.exists()


@pytest.mark.parametrize('failure,retry',[
    (d.Blocked('nested-or-competing-guard'),True),(d.Blocked('competing-process-guard'),True),
    (n.Blocked('nested-or-competing-guard'),False),(d.Blocked('unsafe-transition-admission-lock'),False),
    (n.Conflict('completed-spool-corrupt'),False),(ValueError('invalid JSON'),False),(RuntimeError('invalid evidence'),False),
    *[(OSError(number,'injected'),number in (errno.EIO,errno.EINTR,errno.EAGAIN)) for number in
        (errno.EIO,errno.EINTR,errno.EAGAIN,errno.EACCES,errno.EPERM,errno.EROFS,errno.ELOOP,errno.ENOTDIR,errno.ENOENT)],
])
def test_cleanup_retry_classification_and_timer_recovery(cleanup_receipt,failure,retry):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    from contextlib import contextmanager
    original=owner.p.disk.migration_review_cleanup_guard
    @contextmanager
    def failed(owner):raise failure;yield
    owner.p.disk.migration_review_cleanup_guard=failed
    owner._sweep(reclaim=True)
    assert owner._cleanup_debt is retry and owner._cleanup_error is failure and owner.cleanup_diagnostic==str(failure)
    assert (owner._timer is not None) is retry and payload.exists()
    owner.p.disk.migration_review_cleanup_guard=original
    if retry:timers[-1].call()
    else:owner._sweep();assert payload.exists();owner._sweep(reclaim=True)
    assert not owner._cleanup_debt and owner.cleanup_diagnostic is None and owner._timer is None and not payload.exists()


def healthy_cleanup_view(owner, directory, context, identity):
    """Another genuine owned spool, kept active while an inactive one fails."""
    payload=directory/('view-'+identity*32);payload.write_bytes(b'healthy text'*100);payload.chmod(0o600)
    qname='retire-'+identity*32;(directory/qname).mkdir(mode=0o700)
    qinfo=(directory/qname).stat();info=r._stat(payload.stat());fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    value=dict(quarantine=[qname,qinfo.st_dev,qinfo.st_ino],schema=1,status='complete',name=payload.name,
        candidate='b'*64,scope='c'*64,row=0,side='after',oid='d'*40,size=info[6],
        sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),stat=info,store=[os.fstat(fd).st_dev,os.fstat(fd).st_ino])
    try:owner._save_receipt(fd,'completed-'+identity*32+'.json',value,None)
    finally:os.close(fd)
    ctx=r._context(context);held=os.open(payload,os.O_RDONLY)
    owner.c.saved_descriptor=lambda:[1]
    owner._views[ctx]=dict(name=payload.name,fd=held,stat=info,size=info[6],sha256=value['sha256'],
        deadline=300,review_id='grant',view_id=identity*64,context=ctx,candidate_stat=[1])
    owner._challenges[ctx]={'token':'grant'}
    return held


@pytest.mark.parametrize('phase',['preflight','postcapture'])
def test_persistent_cleanup_prefix_eio_parks_all_passive_paths(cleanup_receipt,monkeypatch,phase):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    contexts=[CONTEXT,('direct','http://fixture','close'),('direct','http://fixture','forget')]
    handles=[healthy_cleanup_view(owner,payload.parent,ctx,identity) for ctx,identity in zip(contexts,'ef9')]
    info=payload.stat();identity=(info.st_dev,info.st_ino);original=os.pread
    reads=[0];failures=[0];guards=[0];enabled=[True]
    original_guard=owner.p.disk.migration_review_cleanup_guard
    from contextlib import contextmanager
    @contextmanager
    def guard(review):
        guards[0]+=1
        with original_guard(review):yield
    owner.p.disk.migration_review_cleanup_guard=guard
    def read(file,size,offset):
        current=os.fstat(file)
        if (current.st_dev,current.st_ino)==identity:
            if enabled[0] and offset>=r.PAGE and (phase=='preflight' or not payload.exists()):
                failures[0]+=1
                raise OSError(errno.EIO,'persistent read failure '+str(failures[0]))
            data=original(file,size,offset);reads[0]+=len(data);return data
        return original(file,size,offset)
    monkeypatch.setattr(os,'pread',read)
    owner._sweep(reclaim=True)
    for tick in range(30,300,30):
        now[0]=tick
        owner.renew_content(view_id='e'*64,context=CONTEXT)
        assert base64.b64decode(owner.content_page(view_id='e'*64,offset=0,context=CONTEXT)['data']).startswith(b'healthy')
        timers[-1].call()
    assert failures[0]==3 and guards[0]==3 and not owner._cleanup_debt
    assert reads[0]==3*r.PAGE+(info.st_size if phase=='postcapture' else 0)
    error=owner._cleanup_error;diagnostic=owner.cleanup_diagnostic;parked_reads=reads[0]
    # A close may enter sweep and direct retirement in one call; neither may rearm.
    with pytest.raises(OSError,match='persistent read failure'):owner.close_content(context=contexts[1])
    owner.forget(context=contexts[2]);owner._retire_inactive();owner.sweep_content()
    now[0]=1000;stale=timers[-1];stale.call();stale.call()
    assert failures[0]==3 and guards[0]==3 and reads[0]==parked_reads
    assert owner._cleanup_error is error and owner.cleanup_diagnostic==diagnostic
    assert not owner._views and not owner._challenges and not owner._cleanup_debt and owner._timer is None
    for held in handles:
        with pytest.raises(OSError):os.fstat(held)
    retained=payload if payload.exists() else payload.parent/receipt['quarantine'][0]/'payload'
    assert retained.stat().st_ino==info.st_ino and retained.read_bytes()==b'full payload bytes'*2000
    enabled[0]=False
    owner._sweep(reclaim=True)
    assert owner.cleanup_diagnostic is None and owner._cleanup_error is None and owner._cleanup_remaining==3
    assert not retained.exists()


@pytest.mark.parametrize('boundary',['store-exit','guard-exit'])
def test_cleanup_full_pass_exit_failures_consume_budget(cleanup_receipt,boundary):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    from contextlib import contextmanager
    # Active owned content lets each body finish without changing its evidence.
    held=healthy_cleanup_view(owner,payload.parent,CONTEXT,'e')
    owner._views['target']=dict(name=payload.name,fd=os.open(payload,os.O_RDONLY),deadline=300,review_id='target')
    original=owner._store if boundary=='store-exit' else owner.p.disk.migration_review_cleanup_guard
    calls=[0]
    @contextmanager
    def failed(*args,**kwargs):
        calls[0]+=1
        with original(*args,**kwargs) as value:yield value
        raise OSError(errno.EIO,'failed '+boundary)
    if boundary=='store-exit':owner._store=failed
    else:owner.p.disk.migration_review_cleanup_guard=failed
    owner._sweep(reclaim=True)
    for _ in range(20):timers[-1].call()
    assert calls[0]==3 and owner._cleanup_remaining==0 and not owner._cleanup_debt
    assert owner.cleanup_diagnostic.endswith(boundary) and payload.exists() and os.fstat(held)
    if boundary=='store-exit':owner._store=original
    else:owner.p.disk.migration_review_cleanup_guard=original
    owner._sweep(reclaim=True)
    assert owner._cleanup_remaining==3 and owner._cleanup_error is None and owner.cleanup_diagnostic is None


@pytest.mark.parametrize('entrypoint',['review','open'])
def test_explicit_cleanup_retry_preserves_diagnostic_then_recovers(tmp_path,monkeypatch,entrypoint):
    root,repo,p,owner,value=setup_review(tmp_path);ref=challenge(owner)
    owner.open_content(row=0,side='before',**ref)
    target=owner._views[r._context(CONTEXT)]
    # Keep the grant usable for an explicit open while making this spool inactive.
    owner._views.pop(r._context(CONTEXT));os.close(target['fd'])
    from contextlib import contextmanager
    original=p.disk.migration_review_cleanup_guard;calls=[0]
    @contextmanager
    def failed(review):
        calls[0]+=1;raise OSError(errno.EIO,'still broken '+str(calls[0]));yield
    p.disk.migration_review_cleanup_guard=failed
    owner._sweep(reclaim=True)
    owner._sweep();owner._sweep()
    assert calls[0]==3 and owner._cleanup_remaining==0
    if entrypoint=='review':owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
    else:owner.open_content(row=0,side='before',**ref)
    assert calls[0]==4 and owner._cleanup_remaining==2 and owner.cleanup_diagnostic=='[Errno 5] still broken 4'
    # Explicit retry does not erase the previous warning before cleanup succeeds.
    p.disk.migration_review_cleanup_guard=original
    if entrypoint=='review':owner.review(candidate=ref['candidate'],scope=ref['scope'],context=CONTEXT)
    else:owner.open_content(row=0,side='before',**ref)
    assert owner._cleanup_remaining==3 and owner.cleanup_diagnostic is None and owner._cleanup_error is None


def test_cleanup_mixed_failure_episode_and_restart_stay_parked(cleanup_receipt):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    from contextlib import contextmanager
    failures=iter([OSError(errno.EIO,'first'),OSError(errno.EINTR,'different'),d.Blocked('competing-process-guard')]);calls=[0]
    @contextmanager
    def failed(review):calls[0]+=1;raise next(failures);yield
    owner.p.disk.migration_review_cleanup_guard=failed
    owner._sweep(reclaim=True)
    for _ in range(3):owner.sweep_content()
    assert calls[0]==3 and owner._cleanup_remaining==0 and owner._timer is None
    owner.shutdown()
    fresh=r.Review(owner.c,clock=lambda:now[0],scheduler=lambda *a:pytest.fail('restart scheduled cleanup'))
    assert not fresh._views and not fresh._challenges and not fresh._cleanup_debt and fresh._timer is None
    fresh.sweep_content()
    assert calls[0]==3 and payload.exists()


def test_cleanup_close_double_entry_cannot_exceed_failure_budget(cleanup_receipt,monkeypatch):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    held=healthy_cleanup_view(owner,payload.parent,CONTEXT,'e')
    original=os.pread;identity=(payload.stat().st_dev,payload.stat().st_ino);calls=[0]
    def read(file,size,offset):
        info=os.fstat(file)
        if (info.st_dev,info.st_ino)==identity and offset>=r.PAGE:
            calls[0]+=1;raise OSError(errno.EIO,'still unavailable')
        return original(file,size,offset)
    monkeypatch.setattr(os,'pread',read)
    owner._sweep(reclaim=True)
    with pytest.raises(OSError,match='still unavailable'):owner.close_content(context=CONTEXT)
    assert calls[0]==3 and owner._cleanup_remaining==0 and not owner._cleanup_debt and owner._timer is None
    with pytest.raises(OSError):os.fstat(held)
    for _ in range(20):owner.sweep_content()
    assert calls[0]==3


@pytest.mark.parametrize('persistent',[False,True])
def test_real_cleanup_lock_contention_recovers_or_parks(cleanup_receipt,persistent):
    import fcntl
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    # Keep the lock namespace separate from the isolated receipt-store adapter.
    root=payload.parent.with_name(payload.parent.name+'-disk-root');(root/'data').mkdir(parents=True)
    lock=root/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    disk=d.NativeDisk.__new__(d.NativeDisk);disk.root=os.fsencode(root);disk._active_operation=None
    owner.root=disk.root;owner.p.disk=disk
    competing=os.open(lock,os.O_RDWR);fcntl.flock(competing,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        owner._sweep(reclaim=True)
        assert isinstance(owner._cleanup_error,d.Blocked) and owner._cleanup_remaining==2
        if persistent:
            for _ in range(20):owner.sweep_content()
            assert owner._cleanup_remaining==0 and not owner._cleanup_debt and owner._timer is None
    finally:os.close(competing)
    if persistent:
        owner.sweep_content();assert payload.exists()
        owner._sweep(reclaim=True)
    else:timers[-1].call()
    assert not payload.exists() and owner._cleanup_remaining==3 and owner.cleanup_diagnostic is None


def test_terminal_cleanup_failure_after_transient_parks_immediately(cleanup_receipt):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    from contextlib import contextmanager
    failures=iter([OSError(errno.EIO,'transient'),n.Conflict('completed-spool-corrupt')]);calls=[0]
    @contextmanager
    def failed(review):calls[0]+=1;raise next(failures);yield
    owner.p.disk.migration_review_cleanup_guard=failed
    owner._sweep(reclaim=True);owner.sweep_content()
    for _ in range(20):owner.sweep_content()
    assert calls[0]==2 and owner._cleanup_remaining==0 and not owner._cleanup_debt and owner._timer is None
    assert owner.cleanup_diagnostic=='completed-spool-corrupt' and payload.exists()


def test_cleanup_prefix_transient_recovers_and_success_replenishes(cleanup_receipt,monkeypatch):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    original=os.pread;identity=(payload.stat().st_dev,payload.stat().st_ino);failures=[0]
    def read(file,size,offset):
        info=os.fstat(file)
        if (info.st_dev,info.st_ino)==identity and offset>=r.PAGE and not failures[0]:
            failures[0]+=1;raise OSError(errno.EIO,'transient payload read')
        return original(file,size,offset)
    monkeypatch.setattr(os,'pread',read)
    owner._sweep(reclaim=True)
    assert owner._cleanup_remaining==2 and owner._cleanup_debt and payload.exists()
    timers[-1].call()
    assert not payload.exists() and owner._cleanup_remaining==3 and owner._cleanup_error is None and owner._timer is None
    from contextlib import contextmanager
    @contextmanager
    def failed(review):raise OSError(errno.EIO,'another episode');yield
    owner.p.disk.migration_review_cleanup_guard=failed
    owner._retire_inactive()
    assert owner._cleanup_remaining==2 and owner._cleanup_debt


def test_real_cleanup_guard_exit_eio_counts_failed_empty_passes(cleanup_receipt,monkeypatch):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    root=payload.parent.with_name(payload.parent.name+'-disk-root');(root/'data').mkdir(parents=True)
    lock=root/'data/.native-disk-admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
    disk=d.NativeDisk.__new__(d.NativeDisk);disk.root=os.fsencode(root);disk._active_operation=None
    owner.root=disk.root;owner.p.disk=disk
    original_open,original_close=os.open,os.close;held=[None];failures=[0]
    def opened(name,*args,**kwargs):
        file=original_open(name,*args,**kwargs)
        if name==d.ADMISSION_LOCK.split(b'/')[-1]:held[0]=file
        return file
    def closed(file):
        original_close(file)
        if file==held[0]:
            held[0]=None;failures[0]+=1;raise OSError(errno.EIO,'guard exit failed')
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'close',closed)
    owner._sweep(reclaim=True)
    assert not payload.exists() and owner._cleanup_remaining==2 and owner.cleanup_diagnostic
    for _ in range(20):owner.sweep_content()
    assert failures[0]==3 and owner._cleanup_remaining==0 and not owner._cleanup_debt and owner._timer is None
    assert disk._active_operation is None and not d._THREAD_LOCKS[disk.root+b'/'+d.ADMISSION_LOCK].locked()
    monkeypatch.setattr(os,'close',original_close)
    owner._sweep(reclaim=True)
    assert owner._cleanup_remaining==3 and owner._cleanup_error is None and owner.cleanup_diagnostic is None


def test_cleanup_reentrant_failure_cannot_underflow_and_rearm(cleanup_receipt):
    owner,fd,payload,control,receipt,now,timers=cleanup_receipt
    from contextlib import contextmanager
    calls=[0];owner._cleanup_remaining=1
    @contextmanager
    def failed(review):
        calls[0]+=1
        if calls[0]==1:owner._retire_inactive()
        raise OSError(errno.EIO,'nested failure');yield
    owner.p.disk.migration_review_cleanup_guard=failed
    owner._retire_inactive()
    for _ in range(20):owner._retire_inactive();owner.sweep_content()
    assert calls[0]==2 and owner._cleanup_remaining==0 and not owner._cleanup_debt and owner._timer is None
