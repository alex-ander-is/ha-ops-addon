"""Exact dormant review routes, with independent ingress/direct authorization."""
import hashlib
import hmac
import json
import re
import secrets
import threading
from urllib.parse import unquote, urlsplit

from direct_auth import Refused, origin


ROUTES = ('state','review','decision','content-open','content-page','content-close')


def reserved(path):
    # Reserve only known path namespaces; query data is never routing authority.
    value=urlsplit(path).path
    for _ in range(len(path)+1):
        if re.match(r'^(?:(?:/api/hassio_ingress/[^/?#]+)?/api/v1|/direct/api)/migration(?:/|$)',value,re.IGNORECASE):return True
        decoded=unquote(value)
        if decoded==value:return False
        value=decoded
    return False


def single(handler, name, required=True):
    values=handler.headers.get_all(name,[])
    if len(values)>1 or required and len(values)!=1:raise Refused('Ambiguous review headers')
    return values[0] if values else None


def body(handler):
    length=single(handler,'Content-Length')
    if (not re.fullmatch(r'(?:0|[1-9][0-9]{0,4})',length) or int(length)>8192
            or handler.headers.get_all('Transfer-Encoding',[])
            or single(handler,'Content-Type').strip()!='application/json'):
        raise Refused('Invalid review request')
    def unique(pairs):
        value={}
        for k,v in pairs:
            if k in value:raise Refused('Duplicate review fields')
            value[k]=v
        return value
    raw=handler.rfile.read(int(length))
    if len(raw)!=int(length):raise Refused('Incomplete review body')
    value=json.loads(raw,object_pairs_hook=unique,
                     parse_constant=lambda _: (_ for _ in ()).throw(Refused('Invalid constant')))
    if type(value) is not dict:raise Refused('Invalid review body')
    return value


class Adapter:
    def __init__(self, review):
        self.review=review
        self._secret=secrets.token_bytes(32)

    def csrf(self, context):
        return hmac.new(self._secret,json.dumps(context,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

    def request(self, handler, *, direct=False, authority=None, address=None, validated_input=None):
        prefix=r'/direct/api/migration/' if direct else r'(?:/api/hassio_ingress/[^/%?#]+)?/api/v1/migration/'
        match=re.fullmatch(prefix+'('+'|'.join(ROUTES)+')',handler.path)
        if not match:return 404,dict(ok=False,message='Migration review unavailable')
        route=match[1]
        expected='GET' if route=='state' else 'POST'
        if handler.command!=expected:return 405,dict(ok=False,message='Method not allowed')
        try:
            if sum(len(k)+len(v) for k,v in handler.headers.items())>16384 or handler.headers.get_all('Transfer-Encoding',[]):
                raise Refused('Invalid review framing')
            length_header=single(handler,'Content-Length',False)
            single(handler,'Content-Type',False)
            if route=='state' and length_header not in (None,'0'):raise Refused('Invalid review framing')
            host_header=single(handler,'Host')
            origin('http://'+host_header)
            csrf_header=single(handler,'X-CSRF-Token',False)
            for key in ('X-Forwarded-Host','X-Forwarded-Proto','X-Ingress-Path','X-Remote-User-Id'):
                single(handler,key,False)
            supplied=single(handler,'Origin',False)
            site=single(handler,'Sec-Fetch-Site',False)
            if site not in (None,'same-origin','same-site','none'):raise Refused('Review origin refused')
            if direct:
                cookie=single(handler,'Cookie')
                auth=authority.authenticate(cookie,address)
                if not auth:raise Refused('Review authentication required')
                context=('direct',address,auth[0])
            else:
                if handler.client_address[0]!='172.30.32.2':raise Refused('Trusted ingress transport required')
                host=single(handler,'X-Forwarded-Host')
                proto=single(handler,'X-Forwarded-Proto')
                address=origin(proto+'://'+host)
                ingress=single(handler,'X-Ingress-Path')
                principal=single(handler,'X-Remote-User-Id')
                if not re.fullmatch(r'/api/hassio_ingress/[^/%?#]+',ingress) or not principal.strip():raise Refused('Ingress identity required')
                if handler.path.startswith('/api/hassio_ingress/') and not handler.path.startswith(ingress+'/'):raise Refused('Ingress prefix mismatch')
                single(handler,'Cookie',False)
                context=('ingress',address,ingress,principal)
            if supplied is not None and supplied!=address:raise Refused('Review origin refused')
            if route=='state':
                if validated_input is not None:validated_input()
                self.review.forget(context=context)
                return 200,dict(ok=True,csrf=self.csrf(context),**self.review.diagnose())
            if supplied!=address or not hmac.compare_digest(csrf_header or '',self.csrf(context)):
                raise Refused('Review origin or CSRF refused')
            value=body(handler)
            fields={'review':{'candidate','scope'},'decision':{'review_id','candidate','scope','action'},
                'content-open':{'review_id','candidate','scope','row','side'},'content-page':{'view_id','offset'},'content-close':set()}[route]
            if set(value)!=fields:raise Refused('Invalid review fields')
            if any(type(v) is not str for k,v in value.items() if k not in ('row','offset')):raise Refused('Invalid review fields')
            for key in ('candidate','scope','review_id','view_id'):
                if key in value and not re.fullmatch('[0-9a-f]{64}',value[key]):raise Refused('Invalid review identity')
            for key in ('row','offset'):
                if key in value and (type(value[key]) is not int or not 0<=value[key]<=(1<<63)-1):raise Refused('Invalid review range')
            if 'action' in value and value['action'] not in ('approve','refuse','dismiss'):raise Refused('Invalid review decision')
            if 'side' in value and value['side'] not in ('before','after'):raise Refused('Invalid review side')
            if validated_input is not None:validated_input()
            method={'review':'review','decision':'decide','content-open':'open_content','content-page':'content_page','content-close':'close_content'}[route]
            result=getattr(self.review,method)(context=context,**value)
            return 200,dict(ok=True,**(result or {}))
        except (ValueError,OSError,RuntimeError,TypeError,RecursionError):
            return 403,dict(ok=False,message='Migration review unavailable. Start a new explicit review.')


def ingress(handler, adapter):
    if not reserved(handler.path):return False
    code,payload=(404,dict(ok=False,message='Migration review unavailable')) if adapter is None else adapter.request(handler)
    raw=json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type","application/json")
    handler.send_header("Content-Length",str(len(raw)))
    handler.send_header("Cache-Control","no-store")
    handler.send_header("X-Content-Type-Options","nosniff")
    handler.end_headers()
    if handler.command!="HEAD":handler.wfile.write(raw)
    handler.close_connection=True
    return True
