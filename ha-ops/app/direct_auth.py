"""Private remembered-browser authority; no operation or worker authority."""
from contextlib import contextmanager
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlsplit
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import time


class Refused(ValueError):
    pass


def origin(value):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise Refused('Invalid direct address')
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment or not p.hostname:
            raise ValueError()
        host = p.hostname.lower()
        try:
            parsed = ipaddress.ip_address(host)
            host = '[' + str(parsed) + ']' if parsed.version == 6 else str(parsed)
        except ValueError:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
                raise ValueError()
        port = p.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        return p.scheme + '://' + host + (':' + str(port) if port and port != (443 if p.scheme == 'https' else 80) else '')
    except (ValueError, TypeError):
        raise Refused('Invalid direct address') from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('Invalid private browser authorization store')
        result[key] = value
    return result


class Authority:
    def __init__(self, data_dir, *, clock=time.monotonic):
        self.directory = Path(data_dir) / 'direct-auth'
        self.clock = clock
        self.new_store = False
        try:
            self.directory.mkdir(mode=0o700)
            self.new_store = True
            parent_fd = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except FileExistsError:
            pass
        self.lock = threading.RLock()
        self.grants, self.nonces, self.csrf, self.rates = {}, {}, {}, {}
        with self._disk() as fd:
            self.installation = self._load(fd)['installation']
        self.cookie_name = 'ha_ops_' + self.installation

    @contextmanager
    def _disk(self):
        with self.lock:
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                s = os.fstat(fd)
                if s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
                    raise Refused('Unsafe private browser authorization store')
                fcntl.flock(fd, fcntl.LOCK_EX)
                now = self.directory.lstat()
                if (s.st_dev, s.st_ino) != (now.st_dev, now.st_ino):
                    raise Refused('Changed private browser authorization store')
                yield fd
            finally:
                os.close(fd)

    def _load(self, fd):
        try:
            token_fd = os.open('tokens.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            # A missing token file in an existing initialized store is corruption.
            if not self.new_store or os.listdir(fd):
                raise Refused('Missing private browser authorization store')
            value = dict(schema=1, installation=secrets.token_hex(12), tokens=[])
            self._save(fd, value)
            self.new_store = False
            return value
        try:
            s = os.fstat(token_fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > 4 * 1024 * 1024:
                raise Refused('Unsafe private browser authorization store')
            with os.fdopen(token_fd, 'r', closefd=False) as stream:
                value = json.load(stream, object_pairs_hook=_unique)
        except (ValueError, TypeError, RecursionError):
            raise Refused('Invalid private browser authorization store') from None
        finally:
            os.close(token_fd)
        if not isinstance(value, dict) or set(value) != {'schema', 'installation', 'tokens'} or type(value['schema']) is not int or value['schema'] != 1 or type(value['installation']) is not str or not re.fullmatch('[0-9a-f]{24}', value['installation']) or not isinstance(value['tokens'], list) or len(value['tokens']) > 4096:
            raise Refused('Invalid private browser authorization store')
        ids = set()
        for token in value['tokens']:
            if not isinstance(token, dict) or set(token) != {'id', 'hash', 'origin', 'created'} or type(token['id']) is not str or not re.fullmatch('[0-9a-f]{32}', token['id']) or token['id'] in ids or type(token['hash']) is not str or not re.fullmatch('[0-9a-f]{64}', token['hash']) or type(token['created']) is not int or origin(token['origin']) != token['origin']:
                raise Refused('Invalid private browser authorization store')
            ids.add(token['id'])
        return value

    def _save(self, fd, value):
        name = '.pending-' + secrets.token_hex(12)
        file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(file_fd, 'w', closefd=False) as stream:
                json.dump(value, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(file_fd)
            os.replace(name, 'tokens.json', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(file_fd)
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass

    def _prune(self):
        now = self.clock()
        for table in (self.grants, self.nonces, self.rates):
            for key, value in list(table.items()):
                if value[0] <= now:
                    del table[key]

    def permit(self, peer):
        with self.lock:
            self._prune()
            if peer not in self.rates:
                if len(self.rates) >= 1024:
                    return False
                self.rates[peer] = (self.clock() + 60, 0)
            expiry, count = self.rates[peer]
            self.rates[peer] = (expiry, count + 1)
            return count < 60

    def nonce(self, context):
        with self.lock:
            self._prune()
            if len(self.nonces) >= 1024:
                raise Refused('Try browser authorization again later')
            value = secrets.token_urlsafe(32)
            self.nonces[value] = (self.clock() + 120, context)
            return value

    def issue(self, nonce, context, address):
        address = origin(address)
        with self.lock:
            self._prune()
            entry = self.nonces.pop(nonce, None)
            if not entry or entry[1] != context or len(self.grants) >= 1024:
                raise Refused('Reopen browser authorization from Home Assistant')
            grant = secrets.token_urlsafe(32)
            self.grants[grant] = (self.clock() + 120, address)
            return address + '/direct/#grant=' + grant

    def allowed(self, address):
        with self.lock:
            self._prune()
            if any(v[1] == address for v in self.grants.values()):
                return True
            with self._disk() as fd:
                return any(t['origin'] == address for t in self._load(fd)['tokens'])

    def exchange(self, grant, address):
        with self.lock:
            self._prune()
            entry = self.grants.get(grant)
            if not entry or entry[1] != address:
                raise Refused('Authorization expired; allow this browser from Home Assistant again')
            del self.grants[grant]
            identifier, secret = secrets.token_hex(16), secrets.token_urlsafe(32)
            with self._disk() as fd:
                value = self._load(fd)
                if len(value['tokens']) >= 4096:
                    raise Refused('Private browser authorization capacity reached')
                value['tokens'].append(dict(id=identifier, hash=hashlib.sha256(secret.encode()).hexdigest(), origin=address, created=int(time.time())))
                self._save(fd, value)
            return identifier + '.' + secret

    def authenticate(self, raw, address):
        try:
            cookie = SimpleCookie()
            cookie.load(raw or '')
            bearer = cookie[self.cookie_name].value
            identifier, secret = bearer.split('.', 1)
            if not re.fullmatch('[0-9a-f]{32}', identifier) or not re.fullmatch('[A-Za-z0-9_-]{43}', secret):
                return None
            digest = hashlib.sha256(secret.encode()).hexdigest()
            with self._disk() as fd:
                for token in self._load(fd)['tokens']:
                    if token['id'] == identifier and token['origin'] == address and hmac.compare_digest(token['hash'], digest):
                        return identifier, bearer
        except (KeyError, ValueError, CookieError):
            return None
        return None

    def cookie(self, bearer, secure=False):
        value = SimpleCookie()
        value[self.cookie_name] = bearer
        token = value[self.cookie_name]
        token['httponly'], token['samesite'], token['path'], token['max-age'] = True, 'Strict', '/direct/', 400 * 86400
        if secure:
            token['secure'] = True
        return token.OutputString()

    def session(self, identifier):
        with self.lock:
            # At most one process-scoped CSRF value per remembered browser.
            return self.csrf.setdefault(identifier, secrets.token_urlsafe(32))
