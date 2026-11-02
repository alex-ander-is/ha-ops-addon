"""Actual protected handler factories; invalid requests never reach domain IO."""
import http.client
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import mock
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import web
import direct_access as d


class Spy:
    def __init__(self):self.calls=[]
    def forget(self,**kw):self.calls.append(('forget',kw))
    def diagnose(self):self.calls.append(('diagnose',{}));return dict(required=True,model=None)
    def review(self,**kw):self.calls.append(('review',kw));return dict(review_id='e'*64)
    def decide(self,**kw):self.calls.append(('decide',kw));return dict(decision_authority=False)


@pytest.fixture(params=['ingress','direct'])
def listener(tmp_path,request):
    spy=Spy();mode=request.param
    if mode=='ingress':
        handler=web.create_handler(SimpleNamespace(),migration_review=spy)
        class TrustedFixture(handler):
            def setup(self):
                super().setup();self.client_address=('172.30.32.2',self.client_address[1])
            def log_message(self,*args):pass
        handler=TrustedFixture
    else:
        authority=d.Authority(tmp_path)
        handler=d.create_handler(authority,migration_review=spy)
    server=d.DirectServer(('127.0.0.1',0),handler)
    address='http://127.0.0.1:'+str(server.server_port)
    headers={'Origin':address}
    if mode=='ingress':headers.update({'X-Forwarded-Host':'127.0.0.1:'+str(server.server_port),'X-Forwarded-Proto':'http','X-Ingress-Path':'/api/hassio_ingress/fixture','X-Remote-User-Id':'fixture'})
    else:
        grant=authority.issue(authority.nonce(()),(),address).split('#grant=')[1]
        headers['Cookie']=authority.cookie(authority.exchange(grant,address))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    yield server,spy,('/api/v1/migration/' if mode=='ingress' else '/direct/api/migration/'),headers
    server.shutdown();server.server_close()


def req(server,method,path,value=None,headers=None,raw=None):
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
    data=raw if raw is not None else None if value is None else json.dumps(value)
    connection.request(method,path,data,headers or {})
    response=connection.getresponse();status=response.status;body=response.read();connection.close()
    return status,json.loads(body)


def csrf(listener):
    server,spy,prefix,headers=listener
    code,value=req(server,'GET',prefix+'state',headers=headers)
    assert code==200
    spy.calls.clear()
    return {**headers,'Content-Type':'application/json','X-CSRF-Token':value['csrf']}


def test_actual_handler_explicit_review_and_decision(listener):
    server,spy,prefix,headers=listener;headers=csrf(listener)
    assert req(server,'POST',prefix+'review',dict(candidate='a'*64,scope='b'*64),headers)[0]==200
    assert spy.calls[0][0]=='review'
    assert req(server,'POST',prefix+'decision',dict(review_id='e'*64,candidate='a'*64,scope='b'*64,action='approve'),headers)[0]==200
    assert spy.calls[-1][0]=='decide'


@pytest.mark.parametrize('case',['origin','csrf','cross-site','missing-origin','content-type','duplicate-json','bad-fields','transfer'])
def test_security_failure_precedes_domain(listener,case):
    server,spy,prefix,headers=listener;headers=csrf(listener)
    raw=None
    if case=='origin':headers['Origin']='http://attacker.invalid'
    elif case=='csrf':headers['X-CSRF-Token']='wrong'
    elif case=='cross-site':headers['Sec-Fetch-Site']='cross-site'
    elif case=='missing-origin':del headers['Origin']
    elif case=='content-type':headers['Content-Type']='text/plain'
    elif case=='transfer':headers['Transfer-Encoding']='chunked'
    elif case=='duplicate-json':raw='{"candidate":"a","candidate":"b","scope":"x"}'
    value=dict(candidate='a'*64,scope='b'*64)
    if case=='bad-fields':value['approved']=True
    assert req(server,'POST',prefix+'review',value,headers,raw)[0]==403
    assert spy.calls==[]


@pytest.mark.parametrize('suffix',['decision/apply','decision?x=1','%64ecision','/decision','decision/','decision/save'])
def test_reserved_variants_never_legacy_dispatch(listener,suffix):
    server,spy,prefix,headers=listener;headers=csrf(listener)
    with mock.patch.object(web,'dispatch_command',side_effect=AssertionError('legacy dispatch')):
        assert req(server,'POST',prefix+suffix,{},headers)[0]==404
    assert spy.calls==[]


def test_wrong_method_never_domain(listener):
    server,spy,prefix,headers=listener
    assert req(server,'GET',prefix+'decision',headers=headers)[0]==405
    assert req(server,'PUT',prefix+'review',{},headers)[0]==405
    assert spy.calls==[]


def test_duplicate_origin_never_domain(listener):
    server,spy,prefix,headers=listener;headers=csrf(listener)
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
    connection.putrequest('POST',prefix+'review')
    for k,v in headers.items():connection.putheader(k,v)
    connection.putheader('Origin',headers['Origin']);connection.putheader('Content-Length','2');connection.endheaders(b'{}')
    response=connection.getresponse();assert response.status==403;response.read();connection.close()
    assert spy.calls==[]


def test_untrusted_ingress_transport_before_domain():
    spy=Spy();server=d.DirectServer(('127.0.0.1',0),web.create_handler(SimpleNamespace(),migration_review=spy))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        assert req(server,'GET','/api/v1/migration/state',headers={'X-Forwarded-Host':'ha.invalid','X-Forwarded-Proto':'http'})[0]==403
        assert spy.calls==[]
    finally:server.shutdown();server.server_close()


def test_production_omitted_peer_closed_before_legacy():
    server=d.DirectServer(('127.0.0.1',0),web.create_handler(SimpleNamespace()))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with mock.patch.object(web,'dispatch_command',side_effect=AssertionError('legacy dispatch')):
            for path in ('/api/v1/migration/state','/api/v1/migration/decision/apply','/api/v1/%6digration/decision/save'):
                assert req(server,'POST',path,{})[0]==404
    finally:server.shutdown();server.server_close()


def test_ingress_internal_host_may_differ_from_forwarded_browser_origin(listener):
    server,spy,prefix,headers=listener
    if prefix.startswith('/direct'):return
    headers={**headers,'X-Forwarded-Host':'ha.fixture.invalid:8123','Origin':'http://ha.fixture.invalid:8123'}
    code,result=req(server,'GET',prefix+'state',headers=headers)
    assert code==200 and result['csrf'] and spy.calls
