"""Actual direct handler with deterministic intake-deadline boundary control."""
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import direct_access as d
from test_native_migration_review_http import req


class ControlledTimer:
    def __init__(self,interval,callback):
        self.interval=interval;self.callback=callback;self.cancelled=False
    def start(self):pass
    def cancel(self):self.cancelled=True
    def fire(self):self.callback()  # Also model a callback already selected before cancel.


@pytest.fixture
def timed_direct(tmp_path,monkeypatch):
    timers=[];handlers=[];domain_calls=[];hook_calls=[]
    def timer(*args):
        value=ControlledTimer(*args);timers.append(value);return value
    monkeypatch.setattr(d.threading,'Timer',timer)
    class Domain:
        def call(self,name,result):
            domain_calls.append(name);timers[-1].fire();return result
        def forget(self,**kw):self.call('forget',None)
        def diagnose(self):return self.call('diagnose',dict(required=True,model=None))
        def review(self,**kw):return self.call('review',dict(review_id='e'*64))
        def decide(self,**kw):return self.call('decide',dict(decision=dict(action=kw['action']),decision_authority=False))
        def open_content(self,**kw):return self.call('open_content',dict(view_id='f'*64))
        def content_page(self,**kw):return self.call('content_page',dict(data='',eof=True,total=0))
        def close_content(self,**kw):return self.call('close_content',None)
    authority=d.Authority(tmp_path)
    base=d.create_handler(authority,migration_review=Domain())
    class Handler(base):
        def setup(self):
            super().setup();handlers.append(self)
        def validated_migration_input(self):
            hook_calls.append(self);return super().validated_migration_input()
    server=d.DirectServer(('127.0.0.1',0),Handler)
    address='http://127.0.0.1:'+str(server.server_port)
    grant=authority.issue(authority.nonce(()),(),address).split('#grant=')[1]
    cookie=authority.cookie(authority.exchange(grant,address))
    headers={'Cookie':cookie,'Origin':address,'Content-Type':'application/json'}
    threading.Thread(target=server.serve_forever,daemon=True).start()
    yield SimpleNamespace(server=server,headers=headers,authority=authority,timers=timers,handlers=handlers,calls=domain_calls,hooks=hook_calls)
    server.shutdown();server.server_close()


def session(rig):
    code,state=req(rig.server,'GET','/direct/api/migration/state',headers=rig.headers)
    assert code==200
    rig.calls.clear();rig.hooks.clear()
    return {**rig.headers,'X-CSRF-Token':state['csrf']}


@pytest.mark.parametrize('route,body,call',[
 ('state',None,'diagnose'),
 ('review',dict(candidate='a'*64,scope='b'*64),'review'),
 ('decision',dict(candidate='a'*64,scope='b'*64,review_id='e'*64,action='approve'),'decide'),
 ('content-open',dict(candidate='a'*64,scope='b'*64,review_id='e'*64,row=0,side='after'),'open_content'),
 ('content-page',dict(view_id='f'*64,offset=0),'content_page'),
 ('content-close',{},'close_content'),
])
def test_validated_domain_survives_queued_intake_expiry(timed_direct,route,body,call):
    rig=timed_direct;headers=session(rig)
    code,result=req(rig.server,'GET' if body is None else 'POST','/direct/api/migration/'+route,body,headers)
    assert code==200 and result['ok'] and call in rig.calls and len(rig.hooks)==1
    assert rig.timers[-1].interval==10 and rig.timers[-1].cancelled
    # The domain fired a callback even though Timer.cancel had already run.
    assert rig.handlers[-1].connection.gettimeout()==5


@pytest.mark.parametrize('case',['origin','csrf','auth','schema','identity','enum','range-bool','duplicate','content-type','transfer'])
def test_invalid_intake_never_releases_deadline_or_reaches_domain(timed_direct,case):
    rig=timed_direct;headers=session(rig);body=dict(candidate='a'*64,scope='b'*64);route='review';raw=None
    if case=='origin':headers['Origin']='http://attacker.invalid'
    elif case=='csrf':headers['X-CSRF-Token']='bad'
    elif case=='auth':headers.pop('Cookie')
    elif case=='schema':body['approved']=True
    elif case=='identity':body['candidate']='not-a-digest'
    elif case=='enum':route='decision';body.update(review_id='e'*64,action='publish')
    elif case=='range-bool':route='content-page';body=dict(view_id='f'*64,offset=True)
    elif case=='duplicate':raw='{"candidate":"a","candidate":"b","scope":"c"}'
    elif case=='content-type':headers['Content-Type']='text/plain'
    elif case=='transfer':headers['Transfer-Encoding']='chunked'
    assert req(rig.server,'POST','/direct/api/migration/'+route,body,headers,raw)[0]==403
    assert rig.hooks==[] and rig.calls==[]


def raw_request(rig,headers,*,partial_headers=False,short_eof=False):
    before=len(rig.timers)
    client=socket.create_connection(('127.0.0.1',rig.server.server_port),timeout=3)
    prefix='POST /direct/api/migration/content-close HTTP/1.0\r\nHost: 127.0.0.1:'+str(rig.server.server_port)+'\r\n'
    if partial_headers:wire=prefix+'X-Pending: '
    else:wire=prefix+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'Content-Length: 8\r\n\r\n{}'
    client.sendall(wire.encode())
    # Wait only for handler construction, not any duration threshold.
    deadline=threading.Event()
    for _ in range(300):
        if len(rig.timers)>before:break
        deadline.wait(.005)
    assert len(rig.timers)>before
    if short_eof:client.shutdown(socket.SHUT_WR)
    return client


@pytest.mark.parametrize('partial_headers',[False,True])
def test_slow_headers_or_body_keep_intake_deadline(timed_direct,partial_headers):
    rig=timed_direct;headers=session(rig)
    client=raw_request(rig,headers,partial_headers=partial_headers)
    try:
        rig.timers[-1].fire()
        try:received=client.recv(8192)
        except ConnectionResetError:received=b''
        assert received==b'' or b'403' in received
        assert rig.calls==[] and rig.hooks==[]
    finally:client.close()


def test_complete_json_with_short_declared_body_is_not_complete_intake(timed_direct):
    rig=timed_direct;headers=session(rig)
    client=raw_request(rig,headers,short_eof=True)
    try:
        response=b''
        while True:
            part=client.recv(8192)
            if not part:break
            response+=part
        assert b'403' in response and rig.calls==[] and rig.hooks==[]
    finally:client.close()


def test_expiry_selected_before_handoff_prevents_domain(timed_direct,monkeypatch):
    rig=timed_direct;headers=session(rig)
    handler=type(rig.handlers[-1]);original=handler.validated_migration_input
    def expire_first(self):
        self.request_deadline.fire();return original(self)
    monkeypatch.setattr(handler,'validated_migration_input',expire_first)
    try:code,_=req(rig.server,'POST','/direct/api/migration/review',dict(candidate='a'*64,scope='b'*64),headers)
    except (http.client.RemoteDisconnected,ConnectionResetError):pass
    else:assert code==403
    assert rig.calls==[]


def test_nonmigration_routes_keep_original_request_deadline(timed_direct,monkeypatch):
    rig=timed_direct
    def expire_on_auth(*args):rig.timers[-1].fire();return None
    monkeypatch.setattr(rig.authority,'authenticate',expire_on_auth)
    try:code,_=req(rig.server,'GET','/direct/api/status',headers=rig.headers)
    except (http.client.RemoteDisconnected,ConnectionResetError):pass
    else:assert code==401
    assert rig.hooks==[] and rig.calls==[]


def test_absolute_intake_deadline_refuses_even_if_timer_thread_is_delayed(timed_direct,monkeypatch):
    rig=timed_direct;headers=session(rig)
    handler=type(rig.handlers[-1]);original=handler.validated_migration_input
    def delayed_timer(self):
        self._intake_expires=0
        return original(self)
    monkeypatch.setattr(handler,'validated_migration_input',delayed_timer)
    assert req(rig.server,'POST','/direct/api/migration/review',dict(candidate='a'*64,scope='b'*64),headers)[0]==403
    assert rig.calls==[]
