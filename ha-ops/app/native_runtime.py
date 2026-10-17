"""Observational admission for the dormant, exactly reviewed native runtime.

No imports from this module activate native operations or recover state.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import ssl
import subprocess
import sys
import zlib

PROFILE = Path('/usr/share/ha-ops/runtime-profile.json')
REQUALIFY = 'python3 dev/virtualization/runtime_qualification.py build; then run the complete runtime qualification matrix'


class UnsupportedRuntime(RuntimeError):
    pass


def _command(argv):
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
    result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
    if result.returncode: raise UnsupportedRuntime('Runtime identity command failed: ' + argv[0])
    return result.stdout.decode().strip()


def observe():
    os_release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    git = _command(['/usr/bin/git', '--version'])
    identity = {'os': os_release['ID'].strip('"'), 'release': os_release['VERSION_ID'].strip('"'),
                'architecture': platform.machine(), 'git': git,
                'python': platform.python_version(), 'ssl': ssl.OPENSSL_VERSION}
    if identity['os'] != 'alpine': return identity
    identity['packages'] = _command(['/sbin/apk', 'info', '-v']).splitlines()
    identity['packages'].sort()
    ssh = subprocess.run(['/usr/bin/ssh', '-V'], capture_output=True, timeout=10,
                         env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if ssh.returncode: raise UnsupportedRuntime('Runtime SSH identity unavailable')
    identity['ssh'] = ssh.stderr.decode().strip()
    helper = _command(['/usr/bin/git', '--exec-path']) + '/git-remote-https'
    paths = {'/usr/bin/git', '/usr/bin/ssh', os.path.realpath(sys.executable), helper,
             ssl._ssl.__file__, zlib.__file__}
    for source in ('/usr/bin/git', '/usr/bin/ssh', os.path.realpath(sys.executable), helper):
        linked = _command(['/usr/bin/ldd', source])
        paths.update(path for path in re.findall(r'/[^\s()]+', linked) if Path(path).is_file())
    # Python extensions are already loaded by their actual interpreter. musl's
    # standalone ldd cannot resolve their Python C API symbols, so inspect the
    # interpreter's real loaded-library mappings instead of guessing that API.
    for line in Path('/proc/self/maps').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 6 and any(name in parts[5] for name in ('/libssl.', '/libcrypto.', '/ld-musl-', '/libpython', '/libz.so')) and Path(parts[5]).is_file():
            paths.add(parts[5])
    identity['files'] = {path: {'realpath': os.path.realpath(path),
                               'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                         for path in sorted(paths)}
    return identity


def qualify():
    try:
        observed = observe()
        # Preserve the independently qualified historical development profile.
        if (observed['os'], observed['release'], observed['git'], observed['python']) == (
                'debian', '12', 'git version 2.39.5', '3.12.10') and observed['architecture'] in ('aarch64', 'x86_64'):
            return 'debian-development-2.39.5'
        profile = json.loads(PROFILE.read_text())
        if not isinstance(profile, dict) or not isinstance(profile.get('qualified'), dict):
            raise ValueError('Malformed runtime profile')
        expected = profile['qualified'].get(observed['architecture'])
        if profile.get('schema') == 1 and expected is not None and observed == expected:
            return 'alpine-' + observed['architecture'] + '-2.54.0'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, UnsupportedRuntime) as error:
        raise UnsupportedRuntime('Native runtime identity is unavailable or malformed; requalify: ' + REQUALIFY) from error
    raise UnsupportedRuntime('Unsupported native runtime: ' + json.dumps(observed, sort_keys=True) +
                             '; supported profiles are immutable reviewed entries; requalify: ' + REQUALIFY)
