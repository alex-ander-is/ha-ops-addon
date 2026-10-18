import http.client
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from direct_auth import Authority
import direct_access as d
import web


@pytest.fixture
def listener(tmp_path):
    a=d.Authority(tmp_path)
    server=d.DirectServer(('127.0.0.1',0), d.create_handler(a))
    address='http://127.0.0.1:'+str(server.server_port)
    url=a.issue(a.nonce(()),(),address);grant=url.split('#grant=')[1]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    yield a,server,address,grant
    server.shutdown();server.server_close()


def request(server,method,path,body=None,headers=None):
    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
    c.request(method,path,body,headers or {})
    r=c.getresponse();value=(r.status,dict(r.getheaders()),r.read());c.close();return value


def test_direct_bootstrap_and_route_isolation(listener):
    a,s,address,grant=listener
    code,_,body=request(s,'GET','/direct/api/status');assert code==401 and b'actions_enabled' not in body
    code,h,_=request(s,'POST','/direct/bootstrap',json.dumps({'grant':grant}),{'Origin':address,'Content-Type':'application/json'})
    assert code==200;cookie=h['Set-Cookie']
    assert request(s,'GET','/direct/api/status',headers={'Cookie':cookie})[0]==200
    assert request(s,'POST','/direct/bootstrap',json.dumps({'grant':grant}),{'Origin':address,'Content-Type':'application/json'})[0]==403
    paths=set(web.GET_ENDPOINTS)|set(web.POST_ENDPOINTS)|{'/apply','/save','/rollback','/direct/../apply','/direct/%2e%2e/apply','/direct/api/status?x=1','/x/direct/api/status','/direct/api/status/x'}
    for method in ('GET','POST','OPTIONS','PUT','DELETE'):
        for path in paths:
            for headers in ({},{'Cookie':cookie}):
                assert request(s,method,path,headers=headers)[0]==404,(method,path)
    assert request(s,'GET','/direct/',headers={'Host':'evil.local'})[0]==403


def test_wrong_origin_body_and_headers(listener):
    a,s,address,grant=listener
    for headers,body in [({'Origin':'http://evil.local','Content-Type':'application/json'},json.dumps({'grant':grant})),({'Origin':address,'Content-Type':'text/plain'},'x'),({'Origin':address,'Content-Type':'application/json'},'{')]:
        assert request(s,'POST','/direct/bootstrap',body,headers)[0]==403
    assert request(s,'GET','/direct/',headers={'X-Ingress-Path':'/x','X-Remote-User-Id':'admin'})[0]==200
    html=request(s,'GET','/direct/')[2].decode();assert html.index('history.replaceState')<html.index("fetch('/direct/bootstrap'")


def test_ingress_transport_nonce_and_origin(tmp_path):
    a=Authority(tmp_path);ctx=SimpleNamespace(direct_authority=a,load_options=lambda:{})
    class H:
        path='/api/direct-access/setup';command='GET';client_address=('127.0.0.1',1)
        headers={'X-Forwarded-Host':'ha.local:8123','X-Forwarded-Proto':'http','X-Remote-User-Id':'u'}
        def send_json(self,value,status=200):self.result=(value,status)
    h=H();assert d.ingress_request(h,ctx) and h.result[1]==403
    h.client_address=('172.30.32.2',1);d.ingress_request(h,ctx);assert h.result[0]['nonce']
    h.command='POST';h.path='/api/direct-access/allow';h.headers['Origin']='http://evil.local';d.ingress_request(h,ctx);assert h.result[1]==403


def test_browser_runner_without_existing_cdp_fails_before_fixture_or_browser_start():
    import os
    import shutil
    import subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Node browser launcher is host-only')
    environment=dict(os.environ);environment.pop('HA_OPS_BROWSER_CDP_URL',None)
    script=Path(__file__).parent/'browser/direct-access.mjs'
    result=subprocess.run([node,str(script)],env=environment,capture_output=True,text=True,timeout=5)
    assert result.returncode!=0 and 'HA_OPS_BROWSER_CDP_URL must name' in result.stderr
    assert not result.stdout
