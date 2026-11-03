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
import native_migration_review_http as migration_http
from test_native_migration_git_candidate import synthetic_host_allocation,host_rename_adapter


@pytest.mark.parametrize('path',[
    '/diff-get?path=homeassistant/migration.yaml',
    '/diff-get?path=%2Fapi%2Fv1%2Fmigration%2Freview',
    '/api/hassio_ingress/migration-token/diff-get?path=migration.yaml',
    '/api/v1/migration.yaml','/direct/api/migration-backup','/some-migration/review',
])
def test_unrelated_migration_names_are_not_reserved(path):
    assert not migration_http.reserved(path)


@pytest.mark.parametrize('path',[
    '/api/v1/migration','/api/v1/migration/review?invalid=1',
    '/api/v1/%256digration/decision/save','/API/V1/MIGRATION/state',
    '/api/hassio_ingress/token/api/v1/migration/decision/apply',
    '/direct/api/migration/decision/apply','/direct/api/%6digration/review',
])
def test_exact_native_namespace_variants_stay_reserved(path):
    assert migration_http.reserved(path)


@pytest.mark.parametrize('peer',[False,True])
@pytest.mark.parametrize('path',['homeassistant/migration.yaml','homeassistant/%6digration.yaml'])
def test_existing_diff_get_migration_paths_reach_legacy(peer,path):
    spy=Spy() if peer else None
    server=d.DirectServer(('127.0.0.1',0),web.create_handler(SimpleNamespace(),migration_review=spy))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with mock.patch.object(web,'dispatch_command',return_value={'ok':True,'diff':'existing exact content'}) as dispatch:
            code,value=req(server,'GET','/diff-get?path='+path)
            assert code==200 and value['diff']=='existing exact content'
            assert dispatch.call_args.args[1]=='diff_get'
            assert dispatch.call_args.args[2]['path']=='homeassistant/migration.yaml'
        assert spy is None or spy.calls==[]
    finally:server.shutdown();server.server_close()


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
        spy.fixture_authority=authority
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


def req(server,method,path,value=None,headers=None,raw=None,*,timeout=10):
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=timeout)
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


@pytest.mark.parametrize('kind',['timeout','contexts','invalid-candidate','invalid-scope','resource-review','resource-decision'])
def test_real_authenticated_content_failure_and_context_isolation(listener,tmp_path,kind):
    import subprocess
    import native_migration as native
    import native_migration_review as review_domain
    from test_native_migration_review import setup_review
    original_req=req
    def domain_req(*args,**kwargs):return original_req(*args,**kwargs,timeout=120)
    server,spy,prefix,headers=listener
    root,repo,p,owner,value=setup_review(tmp_path/'real-review')
    for method in ('forget','diagnose','review','decide','open_content','content_page','close_content'):
        setattr(spy,method,getattr(owner,method))
    a=csrf(listener)
    def explicit(h):
        code,state=domain_req(server,'GET',prefix+'state',headers=h);assert code==200
        h={**h,'Content-Type':'application/json','X-CSRF-Token':state['csrf']}
        model=state['model'];ref=dict(candidate=model['candidate'],scope=model['scope_digest'])
        code,grant=domain_req(server,'POST',prefix+'review',ref,h);assert code==200
        return h,dict(**ref,review_id=grant['review_id'])
    a,ar=explicit(a)
    code,av=domain_req(server,'POST',prefix+'content-open',dict(**ar,row=0,side='before'),a);assert code==200
    if kind=='timeout':
        def fail(**kw):
            kw['destination'].write(b'x');raise subprocess.TimeoutExpired('git',30)
        with mock.patch.object(owner.c,'read_content',side_effect=fail):
            code,error=domain_req(server,'POST',prefix+'content-open',dict(**ar,row=0,side='before'),a)
        assert code==403 and error['ok'] is False and 'new explicit review' in error['message']
        assert domain_req(server,'POST',prefix+'content-page',dict(view_id=av['view_id'],offset=0),a)[0]==200
        a,ar=explicit(a)
        assert domain_req(server,'POST',prefix+'content-open',dict(**ar,row=0,side='before'),a)[0]==200
    else:
        if prefix.startswith('/direct'):
            authority=spy.fixture_authority;address=headers['Origin']
            grant=authority.issue(authority.nonce(()),(),address).split('#grant=')[1]
            b={**headers,'Cookie':authority.cookie(authority.exchange(grant,address))}
        else:b={**headers,'X-Remote-User-Id':'second-authenticated-user'}
        if kind.startswith('resource-'):
            import errno
            from contextlib import contextmanager
            b,br=explicit(b)
            @contextmanager
            def no_space(*args):raise OSError(errno.ENOSPC,'second Review admission');yield
            route='review' if kind=='resource-review' else 'decision'
            body=dict(candidate=br['candidate'],scope=br['scope']) if route=='review' else dict(**br,action='approve')
            with mock.patch.object(owner,'_admission',no_space):
                assert domain_req(server,'POST',prefix+route,body,b)[0]==403
        elif kind.startswith('invalid-'):
            code,state=domain_req(server,'GET',prefix+'state',headers=b);assert code==200
            b={**b,'Content-Type':'application/json','X-CSRF-Token':state['csrf']}
            invalid=dict(candidate=ar['candidate'],scope=ar['scope']);invalid[kind[8:]]='f'*64
            assert domain_req(server,'POST',prefix+'review',invalid,b)[0]==403
        else:
            b,br=explicit(b)
            assert domain_req(server,'POST',prefix+'content-page',dict(view_id=av['view_id'],offset=0),a)[0]==200
            assert domain_req(server,'POST',prefix+'content-open',dict(**br,row=0,side='before'),b)[0]==200
            assert domain_req(server,'POST',prefix+'content-page',dict(view_id=av['view_id'],offset=0),b)[0]==403
            assert domain_req(server,'GET',prefix+'state',headers=b)[0]==200
        assert domain_req(server,'POST',prefix+'content-page',dict(view_id=av['view_id'],offset=0),a)[0]==200
        assert domain_req(server,'POST',prefix+'decision',dict(**ar,action='approve'),a)[0]==200
    for key in list(owner._views):
        view=owner._views.pop(key);__import__('os').close(view['fd'])
