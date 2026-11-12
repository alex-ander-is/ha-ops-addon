#!/usr/bin/env python3
"""Bounded external pytest supervisor. Raw Python is a diagnostic opt-out."""
import argparse
import configparser
import fcntl
import importlib.metadata
import io
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import selectors
import shlex
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS = ROOT / 'dev/virtualization/.local/diagnostics'
CAP = 1024 * 1024
LINE_CAP = 8192
GRACE = 0.5
DRAIN = 1.0


class Refused(ValueError):
    pass


class Redactor:
    """Finite credential-region state shared across completed lines/reads."""
    label_pattern = r'\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b'
    credential_labels = re.compile(label_pattern, re.IGNORECASE)
    regions = re.compile(r'(?P<url>https?://)|(?P<scheme>\b(?:bearer|basic)\s+)|(?P<key>' + label_pattern + ')', re.IGNORECASE)

    def __init__(self):
        self.state, self.quote, self.escape = 'normal', None, False
        self.quarantined = False

    @staticmethod
    def url_end(value, index):
        escaped = False
        while index < len(value):
            char = value[index]
            if not escaped and (char.isspace() or char in '"\x27<>'):
                break
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            index += 1
        return index

    def feed(self, value):
        if self.quarantined:
            return ''
        # Parse original delimiters; replacements must never mutate escapes.
        result, index = [], 0
        while index < len(value):
            if self.state == 'normal':
                match = self.regions.search(value, index)
                if match is None:
                    result.append(value[index:])
                    break
                result.append(value[index:match.start()])
                if match.lastgroup == 'url':
                    end = self.url_end(value, match.end())
                    raw_url = value[match.start():end]
                    authority = re.match(r'(?i)https?://([^/?#]*)', raw_url).group(1)
                    # A URL is one opaque raw region. Do not implement a second
                    # partial key/value grammar for its path/query/fragment.
                    sensitive = '@' in authority or self.credential_labels.search(raw_url)
                    result.append('[REDACTED URL]' if sensitive else raw_url)
                    index = end
                    continue
                result.append(match.group() + '[REDACTED]')
                self.state = 'atom_wait' if match.lastgroup == 'scheme' else 'separator'
                self.escape = False
                index = match.end()
                continue
            char = value[index]
            if self.state == 'separator':
                if char in ':=':
                    self.state = 'value'
                elif not char.isspace() and char not in '\"\x27':
                    self.state = 'normal'
                    continue
            elif self.state == 'value':
                if char in '[{|>':
                    # Structured/block values need a full grammar to delimit.
                    # Conservatively omit this unresolved stream instead.
                    self.quarantined = True
                    break
                if char in '\"\x27':
                    self.quote, self.state, self.escape = char, 'quoted', False
                elif not char.isspace():
                    self.state = 'bare'
                    continue
            elif self.state == 'quoted':
                if self.escape:
                    self.escape = False
                elif char == '\\':
                    self.escape = True
                elif char == self.quote:
                    self.state = 'normal'
            elif self.state == 'atom_wait':
                if not char.isspace():
                    self.state = 'atom'
                    continue
            elif self.state == 'atom':
                if self.escape:
                    self.escape = False
                elif char == '\\':
                    self.escape = True
                elif char.isspace() or char in '"\x27,;}':
                    self.state = 'normal'
                    continue
            elif self.state == 'bare':
                if char in ',;}\r\n':
                    self.state = 'normal'
                    continue
            index += 1
        return ''.join(result)


def redact(value):
    return Redactor().feed(value)


class Console:
    """Best-effort nonblocking output, without queues or writer threads."""
    def __init__(self):
        self.targets, self.flags, self.memory = {}, {}, {}
        self.stats = {name: {'bytes_written': 0, 'bytes_dropped': 0} for name in ('stdout', 'stderr')}
        try:
            for name, target in (('stdout', sys.stdout), ('stderr', sys.stderr)):
                try:
                    fd = target.fileno()
                    if fd not in self.flags:
                        self.flags[fd] = fcntl.fcntl(fd, fcntl.F_GETFL)
                        fcntl.fcntl(fd, fcntl.F_SETFL, self.flags[fd] | os.O_NONBLOCK)
                    self.targets[name] = fd
                except (AttributeError, OSError, ValueError):
                    self.targets[name] = None
                    if isinstance(target, io.StringIO) or (isinstance(target, io.TextIOBase) and isinstance(getattr(target, 'buffer', None), io.BytesIO)):
                        self.memory[name] = target
        except BaseException:
            try: self.close()
            except BaseException: pass
            raise

    def write(self, name, data):
        fd, offset = self.targets[name], 0
        if name in self.memory:
            self.memory[name].write(data.decode('utf-8', errors='replace'))
            offset = len(data)
        if fd is not None:
            while offset < len(data):
                try:
                    written = os.write(fd, data[offset:offset + 4096])
                    if written <= 0: break
                    offset += written
                except (BlockingIOError, OSError):
                    break
        self.stats[name]['bytes_written'] += offset
        self.stats[name]['bytes_dropped'] += len(data) - offset

    def close(self):
        failures = []
        for fd, flags in list(self.flags.items()):
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                del self.flags[fd]
            except OSError as error:
                failures.append(error)
        if failures:
            raise OSError('Console flag restoration incomplete: ' + str(len(failures))) from failures[0]


def notice(message, name='stderr'):
    console = Console()
    try: console.write(name, (redact(message) + '\n').encode('utf-8', errors='replace'))
    finally: console.close()


def private_root(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Refused('Artifact path contains a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def private_file(path, mode):
    """Own each raw descriptor until fdopen successfully takes ownership."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return os.fdopen(fd, mode)
    except BaseException:
        try:
            os.close(fd)
        except BaseException:
            pass
        raise


class Evidence:
    """A private unique run; each stream is capped and redacted before any sink."""
    def __init__(self, root, metadata=None):
        self.path = private_root(root) / ('run-' + uuid.uuid4().hex)
        self.path.mkdir(mode=0o700)
        self.data = {'status': 'running', 'started': time.time(), 'python': platform.python_version(),
                     'platform': platform.system(), 'errors': [], **(metadata or {})}
        self.console = self.data.pop('_console', True)
        self.data['console_enabled'] = self.console
        self.sink, self.streams = None, {}
        try:
            self.sink = Console()
            for name in ('stdout', 'stderr'):
                handle = private_file(self.path / (name + '.log'), 'wb')
                self.streams[name] = {'file': handle, 'pending': b'', 'dropping': False,
                                      'bytes_seen': 0, 'bytes_retained': 0, 'truncated': False,
                                      'tail': b'', 'redactor': Redactor(), 'redaction_quarantined': False}
            self.save()
            self.sink.write('stdout', ('Test evidence: ' + str(self.path) + '\n').encode())
        except BaseException:
            for stream in self.streams.values():
                try: stream['file'].close()
                except BaseException: pass
            if self.sink is not None:
                try: self.sink.close()
                except BaseException: pass
            raise

    def save(self):
        temp = self.path / ('manifest-' + uuid.uuid4().hex + '.tmp')
        with private_file(temp, 'w') as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path / 'manifest.json')

    def emit(self, name, line):
        stream = self.streams[name]
        safe = stream['redactor'].feed(line.decode('utf-8', errors='replace')).encode('utf-8')
        stream['redaction_quarantined'] = stream['redactor'].quarantined
        stream['tail'] = (stream['tail'] + safe)[-65536:].decode('utf-8', errors='ignore').encode('utf-8')
        remaining = CAP - stream['bytes_retained']
        if len(safe) > remaining:
            stream['truncated'] = True
        retained = safe[:remaining].decode('utf-8', errors='ignore').encode('utf-8')
        if retained:
            stream['file'].write(retained)
            stream['file'].flush()
            stream['bytes_retained'] += len(retained)
        if self.console:
            self.sink.write(name, safe)

    def feed(self, name, chunk, final=False):
        stream = self.streams[name]
        stream['bytes_seen'] += len(chunk)
        if stream['redaction_quarantined']:
            return
        # Never release a partial credential-bearing line. Oversize lines are
        # discarded whole, with bounded memory, even across arbitrary reads.
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith((b'\n', b'\r'))
            if not stream['dropping']:
                if len(stream['pending']) + len(part) > LINE_CAP:
                    stream['pending'] = b''
                    stream['dropping'] = True
                    stream['truncated'] = True
                    # An oversized line can hide a key/open quote beyond our
                    # finite buffer. Withhold the unresolved remainder of this
                    # stream rather than release a possible later value.
                    stream['redaction_quarantined'] = True
                    stream['redactor'].quarantined = True
                else:
                    stream['pending'] += part
            if ended:
                if not stream['dropping']:
                    self.emit(name, stream['pending'])
                stream['pending'], stream['dropping'] = b'', False
        if final and stream['pending']:
            self.emit(name, stream['pending'])
            stream['pending'] = b''

    def finish(self, **values):
        self.data.update(values, ended=time.time())
        try:
            for name, stream in self.streams.items():
                self.feed(name, b'', final=True)
                stream['file'].close()
                with private_file(self.path / (name + '.tail.log'), 'wb') as handle:
                    handle.write(stream['tail'])
            self.data['streams'] = {name: {key: value for key, value in stream.items()
                                          if key in ('bytes_seen', 'bytes_retained', 'truncated', 'redaction_quarantined')}
                                    for name, stream in self.streams.items()}
            self.data['console'] = self.sink.stats
            self.save()
        finally:
            for stream in self.streams.values():
                try: stream['file'].close()
                except BaseException: pass
            self.sink.close()


def expand_args(args, seen=None):
    seen = set() if seen is None else seen
    expanded = []
    for arg in args:
        if arg.startswith('@'):
            path = Path(arg[1:]).resolve()
            if path in seen or len(seen) >= 8:
                raise Refused('Recursive or excessive pytest argument files')
            seen.add(path)
            if path.stat().st_size > 65536:
                raise Refused('Pytest argument file exceeds 64 KiB')
            expanded.extend(expand_args(path.read_text().splitlines(), seen))
        else:
            expanded.append(arg)
    return expanded


def config_safety(args):
    # Config discovery is left to pytest. Conservatively inspect possible
    # sources only for preludes that could consume our trailing policy options.
    explicit = []
    for index, arg in enumerate(args):
        if arg in ('-c', '--config-file') and index + 1 < len(args):
            explicit.append(Path(args[index + 1]))
        elif arg.startswith('--config-file='):
            explicit.append(Path(arg.split('=', 1)[1]))
        elif arg.startswith('-c') and len(arg) > 2:
            explicit.append(Path(arg[2:]))
    roots = {Path.cwd(), *Path.cwd().parents}
    for arg in args:
        if not arg.startswith('-') and Path(arg.split('::')[0]).exists():
            path = Path(arg.split('::')[0]).resolve()
            roots.update((path if path.is_dir() else path.parent, *path.parents))
    paths = explicit or [root / name for root in roots for name in
                         ('pytest.ini', '.pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml')]
    for path in paths:
        if not path.is_file():
            continue
        if path.stat().st_size > 1024 * 1024:
            raise Refused('Oversize pytest config; use a minimal explicit -c config')
        text = path.read_text()
        if path.suffix == '.toml':
            # Only addopts spans, allowing multiline strings/lists. Fail closed
            # for ambiguous TOML instead of importing a new parser on Python3.9.
            spans = re.findall(r'(?ms)^\s*addopts\s*=\s*(.*?)(?=^\s*[\w.\[\]]+\s*(?:=|\])|\Z)', text)
            addopts = ' '.join(spans)
            if '\\' in addopts:
                raise Refused('Escaped TOML addopts cannot be protected on every supported Python; use plain options')
            addopts = addopts.replace('[', ' ').replace(']', ' ').replace(',', ' ')
        else:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            addopts = ' '.join(parser.get(section, 'addopts', fallback='') for section in parser.sections())
        tokens = shlex.split(addopts.replace('"""', ' ').replace("'''", ' '))
        if '--' in tokens or any(token.startswith('@') for token in tokens):
            raise Refused('Config addopts containing -- or @files cannot be protected; move them to CLI')
        reject_plugins(tokens)


def reject_plugins(args):
    text = ' '.join(args)
    if any(value.startswith('addopts=') or value.startswith('--override-ini=addopts=') or value.startswith('-oaddopts=') for value in args):
        raise Refused('Override addopts through config or PYTEST_ADDOPTS, not -o addopts')
    if re.search(r'(?:^|\s)(?:--(?:reruns|count|force-flaky|max-runs|min-passes)(?:[=\s]|$))', text):
        raise Refused('Retry/repeat options are not supported in protected runs')
    if re.search(r'no:(?:xdist(?:\.plugin)?|faulthandler|terminal)(?:\s|$)', text):
        raise Refused('Protected runs require xdist and fatal faulthandler plugins')


def pytest_command(args, env=None):
    env = os.environ if env is None else env
    if env.get('PYTEST_DISABLE_PLUGIN_AUTOLOAD'):
        raise Refused('Protected runs require pinned xdist autoload; unset PYTEST_DISABLE_PLUGIN_AUTOLOAD')
    if importlib.util.find_spec('xdist') is None:
        raise Refused('Protected runner requires requirements-dev.txt (pytest-xdist)')
    prelude = shlex.split(env.get('PYTEST_ADDOPTS', ''))
    if '--' in prelude or any(arg.startswith('@') for arg in prelude):
        raise Refused('PYTEST_ADDOPTS cannot contain -- or @files in protected runs')
    args = expand_args(args)
    reject_plugins(prelude + args)
    config_safety(prelude + args)
    split = args.index('--') if '--' in args else len(args)
    policy = ['-o', 'faulthandler_timeout=0', '--max-worker-restart=0', '-vv',
              '-p', 'no:rerunfailures', '-p', 'no:flaky', '-p', 'no:repeat']
    return [sys.executable, '-u', '-m', 'pytest', *args[:split], *policy, *args[split:]]


def signal_group(process, number):
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


def no_core():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def supervise(command, *, timeout=1800, artifact_root=DEFAULT_ARTIFACTS, env=None, metadata=None):
    if not math.isfinite(timeout) or timeout <= 0:
        raise Refused('Deadline must be positive and finite')
    evidence = Evidence(artifact_root, metadata)
    process, selector = None, None
    reason, errors, raw = 'completed', [], None
    began = time.monotonic()
    cleanup_at, end_drain, leader_at = None, None, None
    kill_sent, cleanup_unknown, omitted_errors = False, False, 0

    def record(phase, error):
        nonlocal omitted_errors
        message = phase + ': ' + type(error).__name__ + ': ' + redact(str(error))[:512]
        if len(errors) < 32:
            errors.append(message)
        else:
            omitted_errors += 1

    def attempt(phase, operation):
        try:
            return operation()
        except BaseException as error:
            record(phase, error)
            return None

    def group_action(number, phase):
        nonlocal cleanup_unknown
        try:
            signal_group(process, number)
        except BaseException as error:
            cleanup_unknown = True
            record(phase, error)

    try:
        selector = selectors.DefaultSelector()
        child_env = dict(os.environ if env is None else env)
        child_env['PYTHONUNBUFFERED'] = '1'
        process = subprocess.Popen(command, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, preexec_fn=no_core)
        evidence.data.update(child_pid=process.pid, owned_pgid=process.pid,
                             process_identity_scope='owned invocation only; never reuse for later signaling')
        evidence.save()
        for name, pipe in (('stdout', process.stdout), ('stderr', process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while True:
            now = time.monotonic()
            observed = process.poll()
            if observed is not None:
                raw = observed
            if raw is not None and leader_at is None:
                leader_at = now
            if cleanup_at is None and (now - began >= timeout or (leader_at is not None and now - leader_at >= 0.2)):
                reason = 'timeout' if now - began >= timeout else 'orphaned_descendants'
                if reason == 'orphaned_descendants':
                    errors.append('Owned descendants retained output after leader exit')
                cleanup_at, end_drain = now, now + GRACE + DRAIN
                group_action(signal.SIGTERM, 'owned group TERM')
            if cleanup_at is not None and now >= cleanup_at + GRACE and not kill_sent:
                kill_sent = True
                group_action(signal.SIGKILL, 'owned group KILL')
            if end_drain is not None and now >= end_drain:
                if selector.get_map():
                    errors.append('Pipe drain deadline reached')
                break
            if raw is not None and not selector.get_map():
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                except BaseException as error:
                    cleanup_unknown = True
                    record('owned group probe', error)
                else:
                    errors.append('Owned descendants survived the leader')
                group_action(signal.SIGTERM, 'post-leader TERM')
                group_action(signal.SIGKILL, 'post-leader KILL')
                break
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    evidence.feed(key.data, chunk)
                else:
                    selector.unregister(key.fileobj)
    except KeyboardInterrupt:
        reason = 'interrupted'
    except BaseException as error:
        if reason == 'completed':
            reason = 'controller_error'
        record('controller', error)
    finally:
        if process is not None:
            group_action(signal.SIGKILL, 'final owned group KILL')
            observed = attempt('child reap', lambda: process.wait(timeout=DRAIN))
            if observed is not None:
                raw = observed
            else:
                cleanup_unknown = True
            mapping = attempt('pipe inventory', lambda: list(selector.get_map().values())) if selector is not None else []
            for key in mapping or []:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        evidence.feed(key.data, chunk)
                except BlockingIOError:
                    pass
                except BaseException as error:
                    record('final pipe drain', error)
                attempt('registered pipe close', key.fileobj.close)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    attempt('child pipe close', pipe.close)
        if selector is not None:
            attempt('selector close', selector.close)
        outcome = (124 if reason == 'timeout' else 130 if reason == 'interrupted' else
                   raw if raw is not None and raw != 0 else 1 if errors or raw is None else 0)
        values = dict(status='passed' if outcome == 0 else 'failed', reason=reason,
                      raw_returncode=raw, signal=(-raw if raw is not None and raw < 0 else None),
                      exit_code=(128 - outcome if outcome < 0 else outcome),
                      elapsed=time.monotonic() - began, deadline_seconds=timeout, errors=errors,
                      omitted_error_count=omitted_errors,
                      process_group_cleanup='incomplete or unknown' if cleanup_unknown else 'owned cleanup attempted')
        try:
            evidence.finish(**values)
        except BaseException as error:
            record('evidence finalization', error)
            if outcome == 0:
                outcome = 1
            # Artifact finalization faults must not skip independent closure,
            # flag restoration or a final truthful manifest attempt.
            for stream in evidence.streams.values():
                attempt('evidence stream close', stream['file'].close)
            attempt('console restore', evidence.sink.close)
            values.update(status='failed', errors=errors, omitted_error_count=omitted_errors,
                          exit_code=(128 - outcome if outcome < 0 else outcome))
            values['console_restore'] = 'incomplete or unknown' if evidence.sink.flags else 'restored'
            evidence.data.update(values)
            attempt('failed manifest persistence', evidence.save)
            attempt('final error notice', lambda: notice('Evidence finalization failed; primary raw status=' + str(raw) + '; reason=' + reason))
    return (128 - outcome if outcome < 0 else outcome), evidence.path

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deadline', type=float, default=1800)
    parser.add_argument('--artifacts', type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    params = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
    try:
        command = pytest_command(params)
        versions = {name: importlib.metadata.version(name) for name in ('pytest', 'pytest-xdist')}
        code, _ = supervise(command, timeout=args.deadline, artifact_root=args.artifacts,
                            metadata={'versions': versions, 'policy': {'faulthandler_timeout': 0, 'max_worker_restart': 0},
                                      'selectors': [redact(value)[:256] for value in params[:64] if '.py' in value and not value.startswith('-')]})
        return code
    except (ValueError, OSError, configparser.Error) as error:
        notice('Protected test runner refused: ' + str(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
