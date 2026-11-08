"""Real Review worker over strict owned IPC; actual protected host HTTP adapters.

Default mode uses the approved Linux wrapper. --portable explicitly selects a
local supplemental worker and never reports Linux qualification.
"""
import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'ha-ops/app'),str(ROOT/'ha-ops/tests')]
METHODS={
 'diagnose':set(),'forget':{'context'},'review':{'candidate','scope','context'},
 'decide':{'review_id','candidate','scope','action','context'},
 'open_content':{'review_id','candidate','scope','row','side','context'},
 'content_page':{'view_id','offset','context'},'close_content':{'context','view_id'},'renew_content':{'context','view_id'},
 'fixture-stale':set(),'fixture-restart':set(),'fixture-corrupt':set(),'fixture-clean-cache':set(),'fixture-expire':set(),'fixture-near-expiry':set(),'fixture-tick':set(),
}


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate IPC field')
        result[key]=value
    return result


def read(path):return json.loads(path.read_text(),object_pairs_hook=unique)


def write(path,value):
    pending=path.with_name(path.name+'.pending-'+uuid.uuid4().hex)
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as output:json.dump(value,output,sort_keys=True,separators=(',',':'))
    pending.replace(path)


def request(value,nonce):
    if (type(value) is not dict or set(value)!={'schema','nonce','id','method','params'}
            or type(value['schema']) is not int or value['schema']!=1 or value['nonce']!=nonce
            or type(value['id']) is not str or not re.fullmatch('[0-9a-f]{32}',value['id'])
            or type(value['method']) is not str or value['method'] not in METHODS
            or type(value['params']) is not dict or set(value['params'])!=METHODS[value['method']]):
        raise ValueError('invalid review IPC request')
    params=value['params'].copy()
    if 'context' in params:
        context=params['context']
        if type(context) is not list or not context or not all(type(x) is str and x for x in context):raise ValueError('invalid review IPC context')
        params['context']=tuple(context)
    return value['method'],params


def verify_ready(value,nonce,portable):
    if (type(value) is not dict or set(value)!={'schema','nonce','platform','mode','source'}
            or value['schema']!=1 or type(value['schema']) is not int or value['nonce']!=nonce):raise ValueError('invalid worker readiness')
    expected='portable-worker-real-domain' if portable else 'linux-real-domain'
    if value['source']!=expected or value['mode']!=('portable' if portable else 'linux'):
        raise ValueError('wrong worker qualification')
    if not portable and value['platform']!='linux':raise ValueError('Linux worker required')
    if type(value['platform']) is not str:raise ValueError('invalid worker platform')
    return value


def wait_response(process,target,nonce,key,*,clock=time.monotonic,sleep=time.sleep):
    """Bound the known full-domain fixture work without changing App timeouts."""
    deadline=clock()+120
    while process.poll() is None and clock()<deadline:
        if target.is_file():
            response=read(target)
            if (type(response) is not dict or set(response)!={'schema','nonce','id','ok','result'} or response['schema']!=1
                    or type(response['schema']) is not int or response['nonce']!=nonce or response['id']!=key or type(response['ok']) is not bool):raise RuntimeError('Invalid worker response')
            if not response['ok']:raise ValueError('Migration review worker refused')
            return response['result']
        sleep(min(.02,max(0,deadline-clock())))
    raise RuntimeError('Migration worker response unavailable')


def close_worker_views(owner):
    """Release every test-owned context handle without removing durable evidence."""
    owner.shutdown()


def clean_fixture_cache(owner,root,temporary):
    """Explicit fault-test reset, confined to this worker's private temp root."""
    import native_migration_review as review
    owned=Path(root).resolve(strict=True);workspace=Path(temporary).resolve(strict=True)
    if owned==workspace or workspace not in owned.parents:raise ValueError('private worker root required')
    cache=owned/os.fsdecode(review.STORE)
    if cache.parent.resolve(strict=True)!=owned/'data' or cache.is_symlink():raise ValueError('private review cache required')
    if cache.exists() and not cache.is_dir():raise ValueError('private review directory required')
    close_worker_views(owner)
    if cache.exists():shutil.rmtree(cache)


def worker(output,nonce,portable=False):
    if not portable and sys.platform!='linux':raise RuntimeError('Linux worker must execute on Linux')
    if not re.fullmatch('[0-9a-f]{32}',nonce):raise ValueError('invalid worker nonce')
    import pytest
    import native_migration_review as r
    import native_migration_git_candidate as c
    from test_native_migration_git_candidate import ready,build,write as put,git,synthetic_host_allocation,host_rename_adapter
    with tempfile.TemporaryDirectory(prefix='ha-ops-migration-review-domain-') as temporary,ExitStack() as stack:
        if portable:
            allocation=synthetic_host_allocation.__wrapped__();next(allocation);stack.callback(lambda:next(allocation,None))
            patch=pytest.MonkeyPatch();host_rename_adapter.__wrapped__(patch);stack.callback(patch.undo)
        def configure(root,repo):
            put(repo,b'homeassistant/utf8',b'a'*16383+'é'.encode()+b'b'*16380+'😀'.encode()+b'last')
            put(repo,b'homeassistant/binary',bytes(range(256))*513+b'LAST-BYTE\xff')
            put(repo,b'homeassistant/literal-<img src=x onerror=alert(1)>',b'{"__proto__":{"polluted":true},"i18n":"literal"}')
            git(repo,'add','.');git(repo,'commit','-qm','complete review fixture')
        root,repo,p,prepared,state,parent=ready(Path(temporary)/'candidate',configure=configure)
        now=[time.monotonic()]
        build(p,prepared,state,parent);owner=r.Review(c.Candidate(p),clock=lambda:now[0])
        write(output/('review-ready-'+nonce+'.json'),dict(schema=1,nonce=nonce,platform=sys.platform,
            mode='portable' if portable else 'linux',source='portable-worker-real-domain' if portable else 'linux-real-domain'))
        stopped=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stopped.set());signal.signal(signal.SIGINT,lambda *_:stopped.set())
        deadline=time.monotonic()+1200
        try:
            while not stopped.is_set() and time.monotonic()<deadline and not (output/('review-stop-'+nonce)).exists():
                for incoming in sorted(output.glob('review-request-'+nonce+'-*.json')):
                    target=incoming.with_name(incoming.name.replace('review-request-','review-response-',1))
                    if target.exists():continue
                    envelope=read(incoming)
                    request_id=envelope.get('id') if type(envelope) is dict else None
                    try:
                        method,params=request(envelope,nonce)
                        if method=='fixture-stale':p.app='fixture-app-updated';result=None
                        elif method=='fixture-corrupt':
                            with owner._store() as fd:
                                names=[n for n in os.listdir(fd) if n.startswith('completed-')]
                                if not names:raise ValueError('owned completed fixture required')
                                receipt=owner._receipt(fd,names[0])
                                file=os.open(receipt['name'],os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
                                try:os.pwrite(file,b'X',0);os.fsync(file)
                                finally:os.close(file)
                            result=None
                        elif method in ('fixture-expire','fixture-near-expiry','fixture-tick'):
                            now[0]+={'fixture-expire':r.VIEW_LEASE,'fixture-near-expiry':r.VIEW_LEASE-1,'fixture-tick':1}[method]
                            owner.sweep_content();result=dict(active=len(owner._views))
                        elif method=='fixture-clean-cache':
                            clean_fixture_cache(owner,root,temporary)
                            owner=r.Review(c.Candidate(p),clock=lambda:now[0]);result=None
                        elif method=='fixture-restart':
                            close_worker_views(owner)
                            owner=r.Review(c.Candidate(p),clock=lambda:now[0]);result=None
                        else:result=getattr(owner,method)(**params)
                        response=dict(schema=1,nonce=nonce,id=request_id,ok=True,result=result)
                    except (ValueError,OSError,RuntimeError,TypeError):
                        response=dict(schema=1,nonce=nonce,id=request_id,ok=False,result=None)
                    write(target,response)
                stopped.wait(.02)
        finally:
            close_worker_views(owner)


@contextmanager
def backend(portable):
    nonce=uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='ha-ops-review-ipc-') as scratch:
        if portable:
            output=Path(scratch)
            argv=[sys.executable,__file__,'--worker',str(output),'--nonce',nonce,'--portable']
        else:
            output=None
            argv=[sys.executable,str(ROOT/'dev/virtualization/ha_ops_dev.py'),'run','--input','.', '--','env',
                'GIT_AUTHOR_NAME=Fixture','GIT_AUTHOR_EMAIL=fixture@example.invalid','GIT_COMMITTER_NAME=Fixture','GIT_COMMITTER_EMAIL=fixture@example.invalid',
                'python3','ha-ops/tests/browser/migration_review_fixture.py','--worker','/workspace/output','--nonce',nonce]
        process=subprocess.Popen(argv,cwd=ROOT,stdout=sys.stderr,stderr=sys.stderr)
        try:
            deadline=time.monotonic()+90
            while process.poll() is None and time.monotonic()<deadline:
                candidates=[output/('review-ready-'+nonce+'.json')] if output is not None else list((ROOT/'dev/virtualization/.local/runs').glob('*/output/review-ready-'+nonce+'.json'))
                ready=next((p for p in candidates if p.is_file()),None)
                if ready:
                    output=ready.parent;readiness=verify_ready(read(ready),nonce,portable);break
                time.sleep(.05)
            else:raise RuntimeError('Qualified migration worker unavailable (wrapper capacity/runner readiness)')
            lock=threading.Lock()
            def rpc(method,params):
                key=uuid.uuid4().hex
                value=dict(schema=1,nonce=nonce,id=key,method=method,params=params)
                # Validate the same serialized schema before dispatch.
                request(json.loads(json.dumps(value)),nonce)
                with lock:
                    incoming=output/('review-request-'+nonce+'-'+key+'.json')
                    target=output/('review-response-'+nonce+'-'+key+'.json')
                    write(incoming,value)
                    return wait_response(process,target,nonce,key)
            yield rpc,readiness
        finally:
            if output is not None:write(output/('review-stop-'+nonce),{})
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)


def main(portable):
    def interrupted(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    import direct_access as d
    import web
    with backend(portable) as (rpc,readiness),tempfile.TemporaryDirectory(prefix='ha-ops-migration-review-http-') as temporary:
        direct=d.DirectServer(('127.0.0.1',0),d.create_handler(None))
        address='http://127.0.0.1:'+str(direct.server_port)
        (Path(temporary)/'auth').mkdir()
        authority=d.Authority(Path(temporary)/'auth')
        enrollment=authority.issue(authority.nonce(()),(),address)
        class Peer:
            def __getattr__(self,name):
                if name not in METHODS:raise AttributeError(name)
                def invoke(**params):return rpc(name,params)
                return invoke
        peer=Peer()
        direct.RequestHandlerClass=d.create_handler(authority,migration_review=peer)
        trace=[]
        actual=web.create_handler(SimpleNamespace(),migration_review=peer)
        class Ingress(actual):
            def log_message(self,*args):pass
            def send_json(self,value,status=200):
                raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def setup(self):
                super().setup();self.client_address=('172.30.32.2',self.client_address[1])
            def do_GET(self):
                if self.path=='/':
                    html=b'<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><ha-ops-migration-review endpoint="/api/v1/migration/"></ha-ops-migration-review><script type="module" src="/assets/ha-ops.js"></script></body></html>'
                    self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html);return
                if self.path=='/assets/ha-ops.js':
                    raw=(ROOT/'ha-ops/app/static/ha-ops.js').read_bytes();self.send_response(200);self.send_header('Content-Type','text/javascript');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                self.headers['X-Forwarded-Host']=self.headers['Host'];self.headers['X-Forwarded-Proto']='http';self.headers['X-Ingress-Path']='/api/hassio_ingress/fixture';self.headers['X-Remote-User-Id']='fixture'
                super().do_GET()
            def do_POST(self):
                if self.path in ('/fixture/corrupt','/fixture/expire','/fixture/near-expiry','/fixture/tick','/fixture/clean-cache'):
                    result=rpc('fixture-'+self.path.rsplit('/',1)[1],{});self.send_json(dict(ok=True,result=result));return
                if self.path=='/fixture/stale':
                    rpc('fixture-stale',{});self.send_json(dict(ok=True));return
                if self.path=='/fixture/restart':
                    rpc('fixture-restart',{});self.send_json(dict(ok=True));return
                self.headers['X-Forwarded-Host']=self.headers['Host'];self.headers['X-Forwarded-Proto']='http';self.headers['X-Ingress-Path']='/api/hassio_ingress/fixture';self.headers['X-Remote-User-Id']='fixture'
                super().do_POST()
        ingress=d.DirectServer(('127.0.0.1',0),Ingress)
        closed=d.DirectServer(('127.0.0.1',0),web.create_handler(SimpleNamespace()))
        servers=[ingress,direct,closed]
        for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
        stop=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
        print(json.dumps(dict(baseUrl='http://127.0.0.1:'+str(ingress.server_port)+'/',directUrl=enrollment,
            closedUrl='http://127.0.0.1:'+str(closed.server_port)+'/',source=readiness['source'],worker_platform=readiness['platform'],size=131338)),flush=True)
        try:stop.wait(1800)
        finally:
            for server in servers:server.shutdown();server.server_close()



if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--portable',action='store_true');parser.add_argument('--worker',type=Path);parser.add_argument('--nonce');args=parser.parse_args()
    if args.worker:worker(args.worker,args.nonce,args.portable)
    else:main(args.portable)
