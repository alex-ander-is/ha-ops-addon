#!/usr/bin/env python3
"""Disposable receiver-side control plane; no client tools cross this boundary."""
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
import uuid

sys.path.insert(0, '/tests')
from network_publication_helpers import ssh_server, tls_server

contexts = {}
controls = {}
lock = threading.RLock()


def dispatch(request):
    action = request['action']
    if action == 'ping': return 'ready'
    if action == 'control':
        with lock:
            control = controls.setdefault(request['id'], {})
            operation = request['operation']
            if operation == 'set': control[request['key']] = request['value']
            elif operation == 'del': del control[request['key']]
            elif operation == 'get': return control[request['key']]
            elif operation != 'snapshot': raise ValueError('unknown control operation')
            return dict(control)
    if action == 'start':
        root = Path(request['root'])
        if not root.is_absolute() or '..' in root.parts or not any(
            parent == root or parent in root.parents for parent in (Path('/workspace/output'), Path('/workspace/ext4'))):
            raise ValueError('receiver root outside owned fixture')
        if root.is_symlink() or not root.is_dir(): raise ValueError('receiver root unavailable')
        control = controls.setdefault(request['control'], {}) if request.get('control') else None
        manager = ssh_server(root) if request['protocol'] == 'ssh' else tls_server(root, control)
        descriptor = manager.__enter__()
        token = uuid.uuid4().hex
        contexts[token] = manager
        return {'token': token, 'descriptor': [str(item) for item in descriptor]}
    if action == 'stop':
        contexts.pop(request['token']).__exit__(None, None, None)
        return True
    raise ValueError('unknown receiver request')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(1024 * 1024 + 1)
            if len(line) > 1024 * 1024: raise ValueError('oversized receiver request')
            response = {'ok': True, 'result': dispatch(json.loads(line))}
        except BaseException as error:
            response = {'ok': False, 'error': type(error).__name__ + ': ' + str(error)}
        self.wfile.write(json.dumps(response).encode() + b'\n')


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


if __name__ == '__main__':
    port = int(sys.argv[1])
    with Server(('127.0.0.1', port), Handler) as server:
        server.serve_forever()
