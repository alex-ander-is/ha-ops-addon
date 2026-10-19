"""Dormant Apply contract, genuine local transport and authenticated HTTP."""
import ast
from contextlib import contextmanager
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_apply as a
import native_disk as disk
import native_recovery as recovery
import native_services as services
from native_operations import NativeOperationStore
from native_status import NativeStatus
import direct_access
from direct_auth import Authority
from test_native_publication import fixture
from test_native_save import reviewed, A, B, canonical
from test_native_services import Adapter, item
from test_direct_access import request
from native_topology_helpers import METADATA


def rig(tmp_path):
    r,endpoint,transport=fixture(tmp_path)
    r.remote({A:b'git\n',B:b'base-b\n'})
    subprocess.run(['git','--git-dir='+str(r.root/METADATA),'push',str(endpoint),
                    r.source.remote_oid+':refs/heads/main'],check=True,capture_output=True)
    r.live({A:b'base\n',B:b'base-b\n'})
    d=disk.NativeDisk(r.root)
    rec=recovery.RecoveryStore(r.binding,disk=d)
    svc=services.ServiceStore(r.root/'data/native-services')
    ops=NativeOperationStore(r.root/'data')
    direct=a.DirectReadiness()
    worker=a.NativeApply(r.preview,d,rec,svc,ops,transport,direct)
    review=reviewed(r,(A,),direction='apply')
    return r,worker,review


@contextmanager
def browser_receipt(worker,prepared,tmp_path):
    (tmp_path/'authorization').mkdir(exist_ok=True)
    authority=Authority(tmp_path/'authorization')
    status=NativeStatus(worker.operations,worker.services,worker.recovery)
    server=direct_access.DirectServer(('127.0.0.1',0),direct_access.create_handler(authority,status,readiness=worker.direct))
    server._fixture_roundtrip_done=threading.Event()
    address='http://127.0.0.1:'+str(server.server_port)
    grant=authority.issue(authority.nonce(()),(),address).split('#grant=')[1]
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        code,h,_=request(server,'POST','/direct/bootstrap',json.dumps(dict(grant=grant)),{'Origin':address,'Content-Type':'application/json'})
        assert code==200
        cookie=h['Set-Cookie']
        headers={'Cookie':cookie}
        code,_,body=request(server,'GET','/direct/api/session',headers=headers)
        assert code==200;csrf=json.loads(body)['csrf']
        errors=[]
        import time
        def roundtrip():
            try:
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    code,_,body=request(server,'GET','/direct/api/status',headers=headers)
                    if code==200 and json.loads(body).get('available'):break
                    time.sleep(.01)
                else:raise AssertionError('qualified Apply status did not become available')
                code,_,body=request(server,'GET','/direct/api/apply-readiness',headers=headers)
                assert code==200,body
                challenge=json.loads(body)['challenge']
                payload=json.dumps(dict(challenge=challenge))
                post=dict(headers,Origin=address,**{'Content-Type':'application/json','X-CSRF-Token':csrf})
                assert request(server,'POST','/direct/api/apply-readiness',payload,dict(post,Origin='http://foreign.invalid'))[0]==403
                assert request(server,'POST','/direct/api/apply-readiness',payload,post)[0]==200
                assert request(server,'POST','/direct/api/apply-readiness',payload,post)[0]==403
            except BaseException as error:errors.append(error)
            finally:server._fixture_roundtrip_done.set()
        client=threading.Thread(target=roundtrip,daemon=True);client.start()
        yield server
        client.join(3)
        assert not client.is_alive() and not errors,errors
    finally:
        server.shutdown();server.server_close();thread.join()


def test_direct_opaque_roundtrip_expiry_forgery_and_reset(tmp_path):
    r,w,review=rig(tmp_path)
    # Preparation needs actual supported Linux allocation; direct handle protocol
    # itself is portable and does not fabricate a PreparedApply.
    binding=w.direct.bind(str(uuid.uuid4()),review)
    with pytest.raises(a.Refused):w.direct.consume(binding)
    w.direct.receipt=a.DirectReceipt(w.direct,('foreign',),'session','http://invalid',float('inf'))
    with pytest.raises(a.Refused):w.direct.consume(binding)
    w.direct.reset()
    with pytest.raises(a.Refused):w.direct.challenge('s','http://invalid',{'available':True})


def test_apply_context_rejects_save_copies_and_refresh(tmp_path):
    r,w,review=rig(tmp_path)
    with pytest.raises(a.preview.Stale):
        with r.preview.apply_context(replace(review)):pass
    # Rejection invalidates the session; a new explicit preview is necessary.
    review=reviewed(r,(A,),direction='apply')
    with r.preview.apply_context(review):pass
    r.preview.reset()
    with pytest.raises(a.preview.Stale):
        with r.preview.apply_context(review):pass


@pytest.mark.skipif(sys.platform!='linux',reason='qualified Linux allocation')
def test_backup_warning_recheck_bypass_and_zero_creation(tmp_path):
    r,w,review=rig(tmp_path);p=w.prepare(review,str(uuid.uuid4()))
    calls=[]
    inventory=lambda:(calls.append('inventory') or {'backups':[]})
    with pytest.raises(a.Refused,match='backup-missing'):
        w._backup(p,{'create_backup':True},inventory)
    assert w._backup(p,{'create_backup':True},inventory,bypass=True) is None
    with pytest.raises(a.Refused,match='one-use'):
        w._backup(p,{'create_backup':True},inventory,bypass=True)
    assert calls==['inventory']*3


def test_no_apply_production_import_or_worker_registration():
    app=Path(a.__file__).parent
    peers={'native_apply.py','native_operations.py','native_recovery.py','native_services.py','native_status.py'}
    for file in app.glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node,ast.Import):modules=[p for v in node.names for p in v.name.split('.')]
            elif isinstance(node,ast.ImportFrom):modules=(node.module or '').split('.')+[p for v in node.names for p in v.name.split('.')]
            else:continue
            assert 'native_apply' not in modules or file.name in peers,file.name



def test_coordinator_capability_is_context_pid_thread_and_identity_bound(tmp_path):
    import copy
    r,w,_=rig(tmp_path);operation=str(uuid.uuid4());errors=[]
    with w._transaction(operation,'prepare') as capability:
        assert capability.check(w,operation) is capability
        for foreign in (None,copy.copy(capability),object.__new__(disk.ApplyCapability)):
            with pytest.raises(disk.Blocked):disk.require_apply(foreign,w,operation)
        with pytest.raises(disk.Blocked):capability.check(w,str(uuid.uuid4()))
        thread=threading.Thread(target=lambda:_reject_capability_thread(capability,w,errors));thread.start();thread.join(2)
        assert errors==['different-apply-capability-context'] and not thread.is_alive()
        if hasattr(os,'fork'):
            pid=os.fork()
            if pid==0:
                try:capability.check(w,operation)
                except disk.Blocked:os._exit(0)
                os._exit(1)
            assert os.waitpid(pid,0)[1]==0
        capability.check(w,operation)
    with pytest.raises(disk.Blocked):capability.check(w,operation)
    with w._transaction(operation,'prepare') as fresh:fresh.check(w,operation)


def _reject_capability_thread(capability,worker,errors):
    try:capability.check(worker)
    except disk.Blocked as error:errors.append(str(error))


@pytest.mark.parametrize('entrance',['reset','prepare','cleanup_draft','private_cleanup'])
def test_all_draft_mutation_entrances_obey_existing_root_lease_before_bootstrap(tmp_path,entrance):
    r,w,review=rig(tmp_path);operation=str(uuid.uuid4())
    assert not (r.root/'data/native/recovery').exists()
    with w._transaction(operation,'worker'):
        if entrance=='private_cleanup':
            with pytest.raises(disk.Blocked,match='capability'):w._cleanup_spools(operation)
        else:
            action={'reset':lambda:w.reset(),'prepare':lambda:w.prepare(review,str(uuid.uuid4())),
                    'cleanup_draft':lambda:w.cleanup_draft(operation)}[entrance]
            with pytest.raises(disk.Blocked,match='busy'):action()
    assert not (r.root/'data/native/recovery').exists()
    assert not (r.root/'data/native/apply').exists()
