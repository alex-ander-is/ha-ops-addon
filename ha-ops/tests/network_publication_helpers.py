"""Owned loopback SSH/TLS Git servers for isolated native publication proofs.

Servers have ephemeral keys and credentials and expose only their fixture root.
They are never configured from application options or a user's SSH profile.
"""
from contextlib import contextmanager
import base64
import http.server
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import threading
import time
import json
import uuid
from collections.abc import MutableMapping
from urllib.parse import urlsplit


def receiver_request(**request):
    endpoint = os.environ['HA_OPS_RUNTIME_RECEIVER_SOCKET']
    address = endpoint if endpoint.startswith('/') else ('127.0.0.1', int(endpoint.rsplit(':', 1)[1]))
    with socket.socket(socket.AF_UNIX if isinstance(address, str) else socket.AF_INET) as connection:
        connection.settimeout(15)
        connection.connect(address)
        connection.sendall(json.dumps(request).encode() + b'\n')
        with connection.makefile('rb') as stream: response = json.loads(stream.readline(1024 * 1024))
    if not response['ok']: raise RuntimeError('Receiver control failed: ' + response['error'])
    return response['result']


class ReceiverControl(MutableMapping):
    """Synchronous bidirectional flags, including receiver-originated updates."""
    def __init__(self, initial):
        self.identity = uuid.uuid4().hex
        self.update(initial)
    def _request(self, operation, **values):
        return receiver_request(action='control', id=self.identity, operation=operation, **values)
    def __getitem__(self, key):
        snapshot = self._request('snapshot')
        return snapshot[key]
    def __setitem__(self, key, value): self._request('set', key=key, value=value)
    def __delitem__(self, key): self._request('del', key=key)
    def __iter__(self): return iter(self._request('snapshot'))
    def __len__(self): return len(self._request('snapshot'))


def receiver_control(initial=None):
    values = {} if initial is None else initial
    return ReceiverControl(values) if os.environ.get('HA_OPS_RUNTIME_RECEIVER_SOCKET') else dict(values)


@contextmanager
def remote_server(protocol, root, control=None):
    if control is not None and not isinstance(control, ReceiverControl):
        raise RuntimeError('Runtime receiver controls require receiver_control for synchronous updates')
    response = receiver_request(action='start', protocol=protocol, root=str(root),
                               control=None if control is None else control.identity)
    descriptor = response['descriptor']
    try: yield (descriptor[0], *(Path(item) for item in descriptor[1:]))
    finally: receiver_request(action='stop', token=response['token'])


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, timeout=20, **kwargs)


def isolated_environment():
    env={k:v for k,v in os.environ.items() if not k.startswith(('GIT_', 'SSH_')) and
         k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1')
    return env


class TLSGitHandler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def log_message(self,*args): pass

    def _request(self):
        server=self.server
        if self.headers.get('Authorization')!=server.authorization:
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="isolated-native-fixture"')
            self.send_header('Content-Length','0')
            self.end_headers()
            return
        route=urlsplit(self.path)
        if server.control.get('redirect'):
            self.send_response(302)
            self.send_header('Location',server.control['redirect'])
            self.send_header('Content-Length','0');self.end_headers();return
        if server.control.get('stall_receive') and route.path.endswith('/git-receive-pack'):
            server.control['receive_started']=True
            time.sleep(2)
            self.close_connection=True
            return
        if server.control.get('stall_upload') and route.path.endswith('/git-upload-pack'):
            server.control['upload_started']=True
            deadline=time.monotonic()+5
            while not server.control.get('release_upload') and time.monotonic()<deadline:time.sleep(.01)
        if not route.path.startswith('/receiver.git/'):
            self.send_error(404)
            return
        if server.control.get('fail_upload') and route.path.endswith('/git-upload-pack'):
            self.send_error(503)
            return
        body=b''
        if self.headers.get('Transfer-Encoding','').lower()=='chunked':
            chunks=[]
            while True:
                size=int(self.rfile.readline().split(b';',1)[0],16)
                if not size:
                    while self.rfile.readline() not in (b'\r\n',b'\n',b''): pass
                    break
                chunks.append(self.rfile.read(size))
                if self.rfile.read(2)!=b'\r\n': raise ValueError('invalid fixture chunk')
            body=b''.join(chunks)
        else:
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        env=isolated_environment()
        env.update(GIT_PROJECT_ROOT=str(server.root),GIT_HTTP_EXPORT_ALL='1',
                   PATH_INFO=route.path,QUERY_STRING=route.query,REQUEST_METHOD=self.command,
                   CONTENT_TYPE=self.headers.get('Content-Type',''),CONTENT_LENGTH=str(len(body)),
                   REMOTE_USER='fixture',REMOTE_ADDR='127.0.0.1',SERVER_PROTOCOL='HTTP/1.1')
        if self.headers.get('Git-Protocol'): env['HTTP_GIT_PROTOCOL']=self.headers['Git-Protocol']
        if self.headers.get('Content-Encoding'): env['HTTP_CONTENT_ENCODING']=self.headers['Content-Encoding']
        result=command(['git','http-backend'],input=body,env=env)
        if server.control.get('fail_upload_after_receive') and route.path.endswith('/git-receive-pack'):
            server.control['fail_upload']=True
        if server.control.get('drop_receive_response') and route.path.endswith('/git-receive-pack'):
            self.close_connection=True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        headers,separator,payload=result.stdout.partition(b'\r\n\r\n')
        if not separator: headers,separator,payload=result.stdout.partition(b'\n\n')
        if not separator: raise ValueError('invalid fixture CGI result')
        pairs=[line.split(b':',1) for line in headers.splitlines()]
        status=next((int(v.strip().split()[0]) for k,v in pairs if k.lower()==b'status'),200)
        self.send_response(status)
        for key,value in pairs:
            if key.lower() not in (b'status',b'content-length'):
                self.send_header(key.decode('ascii'),value.strip().decode('ascii'))
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET=_request
    do_POST=_request


@contextmanager
def tls_server(root,control=None):
    if os.environ.get('HA_OPS_RUNTIME_RECEIVER_SOCKET'):
        with remote_server('https', root, control) as descriptor: yield descriptor
        return
    root=Path(root)
    cert,key=root/'test-ca.pem',root/'test-ca.key'
    command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
             '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
             '-keyout',str(key),'-out',str(cert)])
    key.chmod(0o600)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),TLSGitHandler)
    server.daemon_threads=True
    server.root=root
    server.control={} if control is None else control
    server.authorization='Basic '+base64.b64encode(b'fixture:fixture-password').decode('ascii')
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield 'https://127.0.0.1:%d/receiver.git'%server.server_port,cert
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive(): raise RuntimeError('owned TLS Git server did not stop')


@contextmanager
def ssh_server(root):
    if os.environ.get('HA_OPS_RUNTIME_RECEIVER_SOCKET'):
        with remote_server('ssh', root) as descriptor: yield descriptor
        return
    root=Path(root)
    client,host=root/'client-key',root/'host-key'
    for key in (client,host):
        command(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    config=root/'sshd.conf'
    config.write_text('Port %d\nListenAddress 127.0.0.1\nHostKey %s\n'
        'AuthorizedKeysFile %s\nPidFile %s\nPermitRootLogin yes\n'
        'StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
        'UsePAM yes\nPermitUserEnvironment no\nLogLevel ERROR\n'%(port,host,client.with_suffix('.pub'),root/'sshd.pid'))
    known=root/'known-hosts'
    known.write_text('[127.0.0.1]:%d '%port+host.with_suffix('.pub').read_text())
    process=subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)],
                             stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
    try:
        deadline=time.monotonic()+5
        while True:
            if process.poll() is not None:
                raise RuntimeError('owned SSH fixture failed: '+process.stderr.read().decode())
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1): break
            except OSError:
                if time.monotonic()>=deadline: raise RuntimeError('owned SSH fixture unavailable')
                time.sleep(.01)
        yield 'ssh://root@127.0.0.1:%d%s'%(port,root/'receiver.git'),client,known
    finally:
        if process.poll() is None: os.killpg(process.pid,signal.SIGKILL)
        process.wait(timeout=5)
        process.stderr.close()
