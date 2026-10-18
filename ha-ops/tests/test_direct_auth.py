import concurrent.futures
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import sys
from unittest import mock
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from direct_auth import Authority, Refused, origin


def enrolled(tmp_path, address='http://ha.local:8100'):
    a = Authority(tmp_path)
    url = a.issue(a.nonce(('ingress',)), ('ingress',), address)
    return a, a.exchange(url.split('#grant=')[1], address)


def test_persistent_distinct_browser_tokens_and_cookie(tmp_path):
    a, bearer = enrolled(tmp_path)
    address = 'http://ha.local:8100'
    cookie = a.cookie(bearer)
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Max-Age=34560000' in cookie and 'Secure' not in cookie and 'Domain' not in cookie
    assert 'Secure' in a.cookie(bearer, True)
    b = Authority(tmp_path)
    assert a.cookie_name == b.cookie_name
    assert b.authenticate(cookie, address)
    assert b.authenticate(cookie, 'http://ha.local:8101') is None
    second = a.exchange(a.issue(a.nonce(()), (), address).split('#grant=')[1], address)
    assert bearer != second and len(json.loads((tmp_path/'direct-auth/tokens.json').read_text())['tokens']) == 2
    raw = (tmp_path/'direct-auth/tokens.json').read_text()
    assert bearer not in raw and bearer.split('.')[1] not in raw
    assert (tmp_path/'direct-auth').stat().st_mode & 0o777 == 0o700
    assert (tmp_path/'direct-auth/tokens.json').stat().st_mode & 0o777 == 0o600


def test_grant_context_expiry_restart_single_consumption(tmp_path):
    now = [0]
    a = Authority(tmp_path, clock=lambda:now[0]); address='http://ha.local:8100'
    nonce=a.nonce(('user',))
    with pytest.raises(Refused): a.issue(nonce, ('other',), address)
    grant=a.issue(a.nonce(()), (), address).split('#grant=')[1]
    def exchange(_):
        try:return a.exchange(grant,address)
        except Refused:return None
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        assert sum(x is not None for x in pool.map(exchange,range(8))) == 1
    grant=a.issue(a.nonce(()), (), address).split('#grant=')[1]
    with pytest.raises(Refused): Authority(tmp_path).exchange(grant,address)
    now[0]=121
    with pytest.raises(Refused):a.exchange(grant,address)


@pytest.mark.parametrize('value', ['https://user@ha.local', 'http://ha.local/path','http://ha.local?x','http://ha.local#x','http://ha.local:0','http://ha.local\r\nX:x','javascript:alert(1)'])
def test_invalid_addresses(value):
    with pytest.raises(Refused):origin(value)


def test_canonical_ipv6_and_default_port():
    assert origin('http://[fd00::1]:8100/') == 'http://[fd00::1]:8100'
    assert origin('https://HA.LOCAL:443') == 'https://ha.local'


@pytest.mark.parametrize('damage', ['mode','directory-mode','symlink','corrupt','schema','missing'])
def test_store_fails_closed(tmp_path, damage):
    a,_=enrolled(tmp_path); p=tmp_path/'direct-auth/tokens.json'
    if damage=='mode':p.chmod(0o644)
    if damage=='directory-mode':p.parent.chmod(0o755)
    if damage=='symlink':p.rename(p.with_suffix('.old'));p.symlink_to(p.with_suffix('.old'))
    if damage=='corrupt':p.write_text('{')
    if damage=='schema':p.write_text('{"schema":2}')
    if damage=='missing':p.unlink()
    with pytest.raises((Refused,OSError)):Authority(tmp_path)


def test_persistence_failure_never_returns_bearer(tmp_path):
    a,_=enrolled(tmp_path);address='http://ha.local:8100';grant=a.issue(a.nonce(()),(),address).split('#grant=')[1]
    with mock.patch('direct_auth.os.replace', side_effect=OSError('fixture')):
        with pytest.raises(OSError):a.exchange(grant,address)
    with pytest.raises(Refused):a.exchange(grant,address)
    assert len(json.loads((tmp_path/'direct-auth/tokens.json').read_text())['tokens'])==1


@pytest.mark.parametrize('field,value', [('installation',111111111111111111111111),('installation',[]),('id',11111111111111111111111111111111),('id',[]),('hash',int('1'*64)),('hash',{}),('origin',123),('created','123')])
def test_persisted_types_are_exact_and_optional_startup_is_contained(tmp_path,field,value):
    import direct_access
    from types import SimpleNamespace
    enrolled(tmp_path)
    path=tmp_path/'direct-auth/tokens.json';raw=json.loads(path.read_text())
    if field=='installation':raw[field]=value
    else:raw['tokens'][0][field]=value
    path.write_text(json.dumps(raw));before=path.read_bytes()
    with pytest.raises(Refused):Authority(tmp_path)
    ctx=SimpleNamespace(data_dir=tmp_path,load_options=lambda:{},log=lambda _:None)
    direct_access.start(ctx)
    assert ctx.direct_authority is None and ctx.direct_error
    assert path.read_bytes()==before


def test_deep_json_and_fifo_refuse_without_blocking_startup(tmp_path):
    import direct_access
    from types import SimpleNamespace
    Authority(tmp_path);path=tmp_path/'direct-auth/tokens.json'
    ctx=SimpleNamespace(data_dir=tmp_path,load_options=lambda:{},log=lambda _:None)
    path.write_text('['*2000+'0'+']'*2000)
    direct_access.start(ctx);assert ctx.direct_authority is None and ctx.direct_error
    path.unlink();os.mkfifo(path,0o600)
    direct_access.start(ctx);assert ctx.direct_authority is None and ctx.direct_error
