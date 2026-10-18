"""Owned localhost fixture; trusted transport injection exists only here."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
import subprocess
import signal
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
sys.path[:0] = [str(Path(__file__).resolve().parents[2]/'app'), str(Path(__file__).resolve().parents[1])]
import direct_access as d
from direct_auth import Authority

root=Path(sys.argv[1]);root.mkdir(exist_ok=True)
a=Authority(root)
bridge_output=os.environ.get('HA_OPS_LINUX_BRIDGE_OUTPUT')
bridge_nonce=os.environ.get('HA_OPS_LINUX_BRIDGE_NONCE')
if not bridge_output or not bridge_nonce:
    raise RuntimeError('Genuine Linux bridge output and nonce are required')
class BridgeStatus:
    def read(self):
        value=json.loads((Path(bridge_output)/('projection-'+bridge_nonce+'.json')).read_text())
        if value['fixture_linux']['nonce']!=bridge_nonce:raise RuntimeError('Wrong owned Linux bridge')
        return value
status=BridgeStatus()
server=None

def direct(port=0):
    global server
    server=d.DirectServer(('127.0.0.1',port),d.create_handler(Authority(root),status))
    # Same persisted authority in ingress and listener; volatile grants are shared.
    server.RequestHandlerClass=d.create_handler(a,status)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server.server_port
port=direct(int(os.environ.get('HA_OPS_FIXTURE_DIRECT_PORT','0')));address='http://127.0.0.1:'+str(port)
tls_enabled=False
ctx=SimpleNamespace(direct_authority=a,load_options=lambda:{'direct_https':tls_enabled})
cert=root/'cert.pem';key=root/'key.pem'
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
https=d.DirectServer(('127.0.0.1',int(os.environ.get('HA_OPS_FIXTURE_TLS_PORT','0'))),d.create_handler(a,status,secure=True))
tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key);https.socket=tls.wrap_socket(https.socket,server_side=True,do_handshake_on_connect=False)
threading.Thread(target=https.serve_forever,daemon=True).start()
https_address='https://localhost:'+str(https.server_port)+'/direct/'



class Ingress(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def send_json(self,value,status=200):
        raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/':
            raw=b'<ha-ops-direct></ha-ops-direct><script type="module" src="/assets/ha-ops.js"></script>'
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(raw);return
        if self.path=='/assets/ha-ops.js':
            self.send_response(200);self.send_header('Content-Type','text/javascript');self.end_headers();self.wfile.write((d.STATIC/'ha-ops.js').read_bytes());return
        self.fixture_request()
    def do_POST(self):self.fixture_request()
    def fixture_request(self):
        global tls_enabled
        if self.path=='/fixture/https':
            tls_enabled=True;self.send_json({'ok':True});return
        if self.path=='/fixture/http':
            tls_enabled=False;self.send_json({'ok':True});return
        if self.path=='/fixture/worker':
            (Path(bridge_output)/('start-'+bridge_nonce)).touch();self.send_json({'ok':True});return
        if self.path=='/fixture/restart':
            global a
            old=server;old.shutdown();old.server_close();a=Authority(root);ctx.direct_authority=a;direct(port);self.send_json({'ok':True});return
        if self.path=='/fixture/outage':
            self.send_json({'ok':True});threading.Thread(target=ingress.shutdown,daemon=True).start();return
        # Explicit fixture TCP-peer injection: no production localhost override.
        self.client_address=('172.30.32.2',self.client_address[1])
        self.headers['X-Forwarded-Proto']='http';self.headers['X-Forwarded-Host']=self.headers['Host']
        if not d.ingress_request(self,ctx):self.send_json({'ok':False},404)

def cleanup(*_):
    raise SystemExit(0)


if __name__=='__main__':
    signal.signal(signal.SIGTERM,cleanup)
    ingress=ThreadingHTTPServer(('127.0.0.1',0),Ingress)
    print(json.dumps({'ingress':'http://127.0.0.1:'+str(ingress.server_port)+'/', 'direct':address+'/direct/', 'https':https_address}),flush=True)
    if os.environ.get('HA_OPS_FIXTURE_INGRESS_OUTAGE')!='1':
        threading.Thread(target=ingress.serve_forever,daemon=True).start()
    else:
        ingress.server_close()
    while True:time.sleep(1)
