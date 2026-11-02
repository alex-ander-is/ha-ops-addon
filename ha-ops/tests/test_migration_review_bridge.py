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
