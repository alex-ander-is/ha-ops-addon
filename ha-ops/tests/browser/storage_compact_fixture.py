"""Opt-in isolated Linux policy worker and bounded localhost browser relay.

No production import/route, token storage or live HA access. Linux uses genuine
NativeScope persistence/admission and disposable Git; the host serves shipped
assets and relays only the canonical discovery request schema through owned IPC.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[3]
APP=ROOT/'ha-ops/app'


def write(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(path)


def worker(output,nonce):
    sys.path[:0]=[str(APP),str(ROOT/'ha-ops/tests')]
    import native_storage,native_disk,native_git,native_operations
    from test_native_storage import storage,git
    with tempfile.TemporaryDirectory(prefix='storage-compact-') as temporary:
        root,owner=storage.__wrapped__(Path(temporary))
        blob=git(root,'hash-object','-w','--stdin',input=b'local fixture payload'*2000).strip()
        tree=git(root,'mktree',input=b'100644 blob '+blob+b'\tfixture\n').strip()
        commit=git(root,'commit-tree',tree.decode(),input=b'fixture\n').strip();git(root,'update-ref','refs/heads/fixture',commit.decode())
        def reopen():return native_storage.NativeStorage(native_disk.NativeDisk(root),native_git.NativeGit(root),native_operations.NativeOperationStore(root/'data'))
        mutations=0
        def model():return dict(**owner.status(),revision=0,mutation_count=mutations)
        write(output/('ready-'+nonce+'.json'),model())
        deadline=time.monotonic()+240
        while time.monotonic()<deadline and not (output/('stop-'+nonce)).exists():
            for request in sorted(output.glob('request-'+nonce+'-*.json')):
                target=request.with_name(request.name.replace('request-','response-',1))
                if target.exists():continue
                try:
                    value=json.loads(request.read_text());action=value.get('action')
                    if action=='restart':owner.lifecycle('restart');owner=reopen();result=model()
                    elif action=='refresh':owner.lifecycle('refresh');result=model()
                    elif action=='status':result=model()
                    else:
                        result=owner.request(value)
                        if action=='confirm':mutations+=1
                        result=dict(**result,revision=0,mutation_count=mutations)
                    result=dict(ok=True,model=result)
                except Exception as error:result=dict(ok=False,message=type(error).__name__+':'+str(error))
                write(target,result)
            time.sleep(.02)


def host():
    sys.path.insert(0,str(APP))
    from i18n import EN_TEXT
    nonce=uuid.uuid4().hex
    runs=ROOT/'dev/virtualization/.local/runs'
    process=subprocess.Popen([sys.executable,str(ROOT/'dev/virtualization/ha_ops_dev.py'),'run','--input','.', '--','env','GIT_AUTHOR_NAME=Fixture','GIT_AUTHOR_EMAIL=fixture@example.invalid','GIT_COMMITTER_NAME=Fixture','GIT_COMMITTER_EMAIL=fixture@example.invalid','python3','ha-ops/tests/browser/storage_compact_fixture.py','--worker','/workspace/output','--nonce',nonce],cwd=ROOT,stdout=subprocess.PIPE,stderr=sys.stderr)
    output=None;deadline=time.monotonic()+60
    try:
        while time.monotonic()<deadline and process.poll() is None:
            matches=list(runs.glob('*/output/ready-'+nonce+'.json'))
            if matches:output=matches[0].parent;break
            time.sleep(.05)
        if output is None:raise RuntimeError('Linux storage worker did not become ready')
        lock=threading.Lock()
        def rpc(value):
            with lock:
                key=uuid.uuid4().hex
                request=output/('request-'+nonce+'-'+key+'.json');response=output/('response-'+nonce+'-'+key+'.json')
                write(request,value);end=time.monotonic()+10
                while time.monotonic()<end:
                    if response.exists():return json.loads(response.read_text())
                    time.sleep(.02)
                raise RuntimeError('Linux fixture request timed out')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,code,payload,kind='application/json'):
                raw=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
                self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                route=self.path.split('?',1)[0]
                if route in ('/','/legacy/'):
                    rpc(dict(action='refresh'))
                    self.send(200,b'<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/assets/ha-ops.css"></head><body><ha-ops-app></ha-ops-app><script>window.WebSocket=undefined</script><script type="module" src="/assets/ha-ops.js"></script></body></html>','text/html');return
                if route.startswith('/assets/'):
                    name=route[len('/assets/'):]
                    if name not in ('ha-ops.js','ha-ops.css'):self.send(404,{});return
                    self.send(200,(APP/'static'/name).read_bytes(),'application/javascript' if name.endswith('.js') else 'text/css');return
                if route.endswith('/api/v1/state'):
                    result=rpc(dict(action='status'));model=result['model']
                    self.send(200,dict(schema_version=1,revision=model['revision'],backend_version='fixture',state=dict(last_status='idle'),text=EN_TEXT,view=dict(branch='isolated fixture',native_storage=model if not route.startswith('/legacy/') else None)));return
                if route.endswith('/api/direct-access/setup'):
                    self.send(200,dict(suggested_port=8100,https=False,nonce='opaque-fixture-only'));return
                if route=='/fixture/status':self.send(200,rpc(dict(action='status')));return
                self.send(404,{})
            def do_POST(self):
                length=int(self.headers.get('Content-Length','0'))
                if length>65536:self.send(413,{});return
                value=json.loads(self.rfile.read(length))
                if self.path=='/fixture/restart':value=dict(action='restart')
                elif self.path!='/api/v1/storage':self.send(404,{});return
                result=rpc(value)
                self.send(200 if result['ok'] else 409,result.get('model',result))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        def stop(*args):threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        print(json.dumps(dict(baseUrl='http://127.0.0.1:'+str(server.server_port)+'/',pid=os.getpid(),source='qualified disposable Linux NativeStorage',nonce=nonce)),flush=True)
        timer=threading.Timer(200,stop);timer.daemon=True;timer.start()
        try:server.serve_forever()
        finally:server.server_close();timer.cancel()
    finally:
        if output is not None:(output/('stop-'+nonce)).touch()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',type=Path);parser.add_argument('--nonce');args=parser.parse_args()
    if args.worker:worker(args.worker,args.nonce)
    else:host()
