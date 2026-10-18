"""Independent exact-route listener. Never dispatches legacy ingress actions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit

from direct_auth import Authority, Refused, origin
from native_status import NativeStatus, existing_status


STATIC = Path(__file__).parent / 'static'
BOOTSTRAP = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HA Ops direct access</title></head><body><ha-ops-direct></ha-ops-direct><script>
const grant = new URLSearchParams(location.hash.slice(1)).get('grant');
history.replaceState(null, '', '/direct/');
window.haOpsBootstrap = grant ? fetch('/direct/bootstrap', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant})}).then(async r=>{if(!r.ok)throw Error('Authorization expired. Allow this browser from Home Assistant again.');}) : Promise.resolve();
</script><script type="module" src="/direct/assets/ha-ops.js"></script></body></html>'''


def json_body(handler):
    lengths = handler.headers.get_all('Content-Length', [])
    if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,5}', lengths[0]) or int(lengths[0]) > 8192 or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
        raise Refused('Invalid browser authorization request')
    try:
        body = json.loads(handler.rfile.read(int(lengths[0])))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('Invalid browser authorization request') from None
    if not isinstance(body, dict):
        raise Refused('Invalid browser authorization request')
    return body


def ingress_context(handler):
    # Actual TCP transport is the trust boundary, never caller-supplied headers.
    if handler.client_address[0] != '172.30.32.2':
        raise Refused('Browser authorization requires Home Assistant ingress')
    host = handler.headers.get('X-Forwarded-Host', '')
    proto = handler.headers.get('X-Forwarded-Proto', '')
    source = origin(proto + '://' + host)
    return source, handler.headers.get('X-Ingress-Path', ''), handler.headers.get('X-Remote-User-Id', 'unknown')


def ingress_request(handler, ctx):
    """Handle only the two new setup endpoints, before legacy suffix routing."""
    path = urlsplit(handler.path).path
    match = re.fullmatch(r'(?:/api/hassio_ingress/[^/]+)?/api/direct-access/(setup|allow)', path)
    if not match:
        return False
    try:
        context = ingress_context(handler)
        authority = getattr(ctx, 'direct_authority', None)
        if authority is None:
            raise Refused(getattr(ctx, 'direct_error', 'Direct access is unavailable'))
        if match[1] == 'setup' and handler.command == 'GET':
            options = ctx.load_options()
            payload = dict(nonce=authority.nonce(context), https=bool(options.get('direct_https', False)), suggested_port=8100, message='Port 8100 is a suggestion; use the host port configured in Home Assistant.')
        elif match[1] == 'allow' and handler.command == 'POST':
            if handler.headers.get('Origin') != context[0] or handler.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise Refused('Browser authorization origin refused')
            body = json_body(handler)
            if set(body) != {'nonce', 'address'} or not all(isinstance(v, str) for v in body.values()):
                raise Refused('Invalid browser authorization request')
            selected = origin(body['address'])
            if selected.startswith('https:') != bool(ctx.load_options().get('direct_https', False)):
                raise Refused('Use the configured direct HTTP or HTTPS scheme')
            payload = dict(url=authority.issue(body['nonce'], context, selected))
        else:
            handler.send_json(dict(ok=False, message='Method not allowed'), 405)
            return True
        handler.send_json(dict(ok=True, **payload))
    except (Refused, OSError, ValueError):
        handler.send_json(dict(ok=False, message='Direct browser authorization unavailable. Check direct access configuration and reopen setup.'), 403)
    return True


class DirectServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def create_handler(authority, status=None, *, secure=False, configured_origin=None):
    status = status or NativeStatus()

    class DirectHandler(BaseHTTPRequestHandler):
        server_version = 'HAOpsDirect'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(5)
            def expire():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.request_deadline = threading.Timer(10, expire)
            self.request_deadline.daemon = True
            self.request_deadline.start()

        def finish(self):
            try:
                super().finish()
            finally:
                self.request_deadline.cancel()

        def log_message(self, *_):
            pass  # Never include request URLs, cookies, fragments or exception text.

        def send(self, code, body, content_type='application/json', cookie=None):
            if isinstance(body, dict):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self):
            try:
                if sum(len(k) + len(v) for k, v in self.headers.items()) > 16384 or len(self.headers.get_all('Host', [])) != 1:
                    return self.send(400, dict(message='Invalid request'))
                address = origin(('https' if secure else 'http') + '://' + self.headers['Host'])
                if not (address == configured_origin or authority.allowed(address)):
                    return self.send(403, dict(message='Allow this address from Home Assistant ingress'))
                # No path decoding, suffix dispatch, shell fallback or legacy handler.
                if self.path == '/' and self.command == 'GET':
                    self.send_response(303)
                    self.send_header('Location', '/direct/')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/direct/' and self.command == 'GET':
                    auth = authority.authenticate(self.headers.get('Cookie'), address)
                    return self.send(200, BOOTSTRAP, 'text/html; charset=utf-8', authority.cookie(auth[1], secure) if auth else None)
                if self.path == '/direct/assets/ha-ops.js' and self.command == 'GET':
                    return self.send(200, (STATIC / 'ha-ops.js').read_bytes(), 'text/javascript')
                if self.path == '/direct/bootstrap' and self.command == 'POST':
                    if not authority.permit(self.client_address[0]):
                        return self.send(429, dict(message='Try again later'))
                    if self.headers.get('Origin') != address or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                        raise Refused('Origin refused')
                    body = json_body(self)
                    if set(body) != {'grant'} or not isinstance(body['grant'], str):
                        raise Refused('Invalid grant')
                    bearer = authority.exchange(body['grant'], address)
                    return self.send(200, dict(ok=True), cookie=authority.cookie(bearer, secure))
                if self.command == 'GET' and self.path in ('/direct/api/session', '/direct/api/status'):
                    auth = authority.authenticate(self.headers.get('Cookie'), address)
                    if not auth:
                        return self.send(401 if authority.permit(self.client_address[0]) else 429, dict(message='Allow this browser from Home Assistant ingress'))
                    return self.send(200, dict(csrf=authority.session(auth[0])) if self.path.endswith('/session') else status.read())
                return self.send(404, dict(message='Not found'))
            except (Refused, ValueError, OSError, socket.timeout):
                return self.send(403, dict(message='Direct access refused. Reopen authorization from Home Assistant ingress.'))

        do_GET = dispatch
        do_POST = dispatch
        do_HEAD = dispatch
        do_OPTIONS = dispatch
        do_PUT = dispatch
        do_DELETE = dispatch
        do_PATCH = dispatch

    return DirectHandler


def start(ctx):
    """Optional listener failure leaves ingress usable and never downgrades TLS."""
    ctx.direct_authority = None
    try:
        options = ctx.load_options()
        authority = Authority(ctx.data_dir)
        secure = bool(options.get('direct_https', False))
        configured = options.get('direct_origin') or None
        if configured:
            configured = origin(configured)
            if configured.startswith('https:') != secure:
                raise Refused('Direct scheme mismatch')
        httpd = DirectServer(('0.0.0.0', 8100), create_handler(authority, existing_status(ctx.data_dir), secure=secure, configured_origin=configured))
        try:
            if secure:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                def certificate(name):
                    if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
                        raise Refused('Invalid certificate filename')
                    return '/ssl/' + name
                context.load_cert_chain(certificate(options.get('direct_certfile', 'fullchain.pem')), certificate(options.get('direct_keyfile', 'privkey.pem')))
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        except BaseException:
            httpd.server_close()
            raise
        ctx.direct_authority = authority
        ctx.direct_server = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    except (OSError, ValueError):
        ctx.direct_error = 'Direct access unavailable. Check host port, private authorization data and configured /ssl certificate files.'
        ctx.log(ctx.direct_error)
