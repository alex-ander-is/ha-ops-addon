"""Strict owned worker protocol and approved default browser backend routing."""
import importlib.util
from pathlib import Path
import sys
from unittest import mock
import pytest

FIXTURE=Path(__file__).parent/'browser/migration_review_fixture.py'
spec=importlib.util.spec_from_file_location('migration_review_browser_bridge',FIXTURE)
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
NONCE='a'*32


def envelope(**changes):
    value=dict(schema=1,nonce=NONCE,id='b'*32,method='review',params=dict(candidate='c'*64,scope='d'*64,context=['direct','http://fixture.invalid','principal']))
    value.update(changes);return value


def test_strict_review_protocol_preserves_context_without_authority_fields():
    method,params=bridge.request(envelope(),NONCE)
    assert method=='review' and params['context']==('direct','http://fixture.invalid','principal')


@pytest.mark.parametrize('changes',[
 {'schema':True},{'nonce':'wrong'},{'id':'../escape'},{'method':'build'},
 {'params':dict(candidate='c',scope='s',context=['u'],approved=True)},
 {'params':dict(candidate='c',scope='s',context='u')},
 {'params':dict(candidate='c',scope='s',context=['u',None])},
])
def test_invalid_worker_request_refused(changes):
    with pytest.raises(ValueError):bridge.request(envelope(**changes),NONCE)


@pytest.mark.parametrize('platform,mode,source,portable,valid',[
 ('linux','linux','linux-real-domain',False,True),
 ('darwin','linux','linux-real-domain',False,False),
 ('linux','portable','portable-worker-real-domain',False,False),
 ('darwin','portable','portable-worker-real-domain',True,True),
 ('linux','portable','portable-worker-real-domain',True,True),
 ('darwin','portable','linux-real-domain',True,False),
])
def test_readiness_never_labels_portable_or_nonlinux_as_linux(platform,mode,source,portable,valid):
    value=dict(schema=1,nonce=NONCE,platform=platform,mode=mode,source=source)
    if valid:assert bridge.verify_ready(value,NONCE,portable)==value
    else:
        with pytest.raises(ValueError):bridge.verify_ready(value,NONCE,portable)


def test_default_backend_launches_approved_wrapper_and_never_local_worker():
    # Fail immediately after capturing argv, before any runner/socket/process work.
    class Captured(Exception):pass
    calls=[]
    def launch(argv,**kwargs):calls.append(argv);raise Captured()
    with mock.patch.object(bridge.subprocess,'Popen',launch),pytest.raises(Captured):
        with bridge.backend(False):pass
    argv=calls[0]
    assert str(bridge.ROOT/'dev/virtualization/ha_ops_dev.py') in argv
    assert argv[2:6]==['run','--input','.', '--']
    assert '--worker' in argv and '/workspace/output' in argv and '--portable' not in argv
    for key in ('GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL'):
        assert any(arg.startswith(key+'=') for arg in argv)


def test_worker_refuses_nonlinux_before_build_or_readiness(tmp_path):
    with mock.patch.object(bridge.sys,'platform','darwin'),pytest.raises(RuntimeError):bridge.worker(tmp_path,NONCE,False)
    assert list(tmp_path.iterdir())==[]


def test_duplicate_ipc_json_fields_are_not_adopted(tmp_path):
    path=tmp_path/'request.json';path.write_text('{"schema":1,"schema":1}')
    with pytest.raises(ValueError):bridge.read(path)


class ControlledClock:
    def __init__(self,on_tick=lambda now:None):self.now=0.;self.on_tick=on_tick
    def clock(self):return self.now
    def sleep(self,delay):self.now+=delay;self.on_tick(self.now)


def response(**changes):
    value=dict(schema=1,nonce=NONCE,id='b'*32,ok=True,result={'review_id':'c'*64})
    value.update(changes);return value


def test_actual_rpc_wait_accepts_correlated_reply_after_old_30_second_limit(tmp_path):
    target=tmp_path/'response.json';process=mock.Mock();process.poll.return_value=None
    def reply(now):
        if now>=31 and not target.exists():bridge.write(target,response())
    timer=ControlledClock(reply)
    result=bridge.wait_response(process,target,NONCE,'b'*32,clock=timer.clock,sleep=timer.sleep)
    assert result=={'review_id':'c'*64} and 30<timer.now<120


def test_actual_rpc_no_response_refuses_at_finite_120_second_deadline(tmp_path):
    timer=ControlledClock();process=mock.Mock();process.poll.return_value=None
    with pytest.raises(RuntimeError,match='response unavailable'):
        bridge.wait_response(process,tmp_path/'absent.json',NONCE,'b'*32,clock=timer.clock,sleep=timer.sleep)
    assert timer.now==120


@pytest.mark.parametrize('changes',[
    {'nonce':'other'},{'id':'d'*32},{'schema':True},{'schema':2},{'ok':1},{'extra':'unbound'},
])
def test_actual_rpc_wait_refuses_uncorrelated_or_invalid_reply(tmp_path,changes):
    target=tmp_path/'response.json';bridge.write(target,response(**changes))
    timer=ControlledClock();process=mock.Mock();process.poll.return_value=None
    with pytest.raises(RuntimeError,match='Invalid worker response'):
        bridge.wait_response(process,target,NONCE,'b'*32,clock=timer.clock,sleep=timer.sleep)
    assert timer.now==0


def test_actual_rpc_wait_preserves_worker_refusal_and_early_exit(tmp_path):
    target=tmp_path/'response.json';bridge.write(target,response(ok=False,result=None))
    timer=ControlledClock();process=mock.Mock();process.poll.return_value=None
    with pytest.raises(ValueError,match='worker refused'):
        bridge.wait_response(process,target,NONCE,'b'*32,clock=timer.clock,sleep=timer.sleep)
    process.poll.return_value=1
    with pytest.raises(RuntimeError,match='response unavailable'):
        bridge.wait_response(process,target,NONCE,'b'*32,clock=timer.clock,sleep=timer.sleep)
    assert timer.now==0


@pytest.mark.parametrize('lifecycle',['restart','shutdown'])
def test_actual_worker_lifecycle_closes_all_contexts_without_changing_persisted_bytes(tmp_path,lifecycle):
    import os
    from types import SimpleNamespace
    import native_migration_review as domain
    import native_migration_git_candidate as candidate
    import test_native_migration_git_candidate as fixture
    key='b'*32;owners=[];handles=[];persisted={}
    for label in ('a','b'):
        payload=tmp_path/('view-'+label);payload.write_bytes(('complete-'+label).encode())
        receipt=tmp_path/('completed-'+label+'.json');receipt.write_bytes(('receipt-'+label).encode())
        persisted[payload]=payload.read_bytes();persisted[receipt]=receipt.read_bytes()
    real_review=domain.Review
    def review(source):
        owner=real_review(source);owners.append(owner)
        assert not owner._views and not owner._challenges
        if len(owners)==1:
            for label in ('a','b'):
                fd=os.open(tmp_path/('view-'+label),os.O_RDONLY);handles.append(fd)
                owner._views[label]={'fd':fd};owner._challenges[label]={'token':'old-'+label}
        return owner
    original_write=bridge.write
    def write(path,value):
        original_write(path,value)
        stop=(lifecycle=='shutdown' and path.name=='review-ready-'+NONCE+'.json'
              or lifecycle=='restart' and path.name=='review-response-'+NONCE+'-'+key+'.json')
        if stop:original_write(tmp_path/('review-stop-'+NONCE),{})
    if lifecycle=='restart':
        original_write(tmp_path/('review-request-'+NONCE+'-'+key+'.json'),
            dict(schema=1,nonce=NONCE,id=key,method='fixture-restart',params={}))
    preparation=SimpleNamespace();ready=(tmp_path,tmp_path,preparation,{}, {},'parent')
    try:
        with mock.patch.object(bridge.sys,'platform','linux'),mock.patch.object(bridge.signal,'signal'), \
                mock.patch.object(fixture,'ready',return_value=ready),mock.patch.object(fixture,'build'), \
                mock.patch.object(candidate,'Candidate',side_effect=lambda p:SimpleNamespace(p=p,root=os.fsencode(tmp_path))), \
                mock.patch.object(domain,'Review',side_effect=review),mock.patch.object(bridge,'write',side_effect=write):
            bridge.worker(tmp_path,NONCE,False)
        assert len(owners)==(2 if lifecycle=='restart' else 1)
        assert all(not owner._views and not owner._challenges for owner in owners)
        assert len(handles)==2
        for fd in handles:
            with pytest.raises(OSError):os.fstat(fd)
        assert all(path.read_bytes()==data for path,data in persisted.items())
        if lifecycle=='restart':
            assert bridge.read(tmp_path/('review-response-'+NONCE+'-'+key+'.json'))==dict(schema=1,nonce=NONCE,id=key,ok=True,result=None)
    finally:
        for fd in handles:
            try:os.close(fd)
            except OSError:pass
