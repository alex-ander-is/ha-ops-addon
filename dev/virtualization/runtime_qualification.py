#!/usr/bin/env python3
"""Build and qualify the original installable App on the named local VM.

All mutation goes through ha_ops_dev's explicit local endpoint and admission.
Receiver tooling is a remote peer, never part of the App client filesystem.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
import stat
import tempfile

import ha_ops_dev as vm

APP = vm.ROOT / 'ha-ops'
BASES = {
    'aarch64': 'ghcr.io/home-assistant/base:3.24-2026.08.0@sha256:1b29b917841ca169fa3bece7bb03ae366e9df94a4d382698796a44c5abb3f2b5',
    'amd64': 'ghcr.io/home-assistant/base:3.24-2026.08.0@sha256:59c99f645cd5bc2e20ca8acac0a6a6163749720e1a1fcdd49bcb5702b5db9688',
}
PACKAGES = 'bash curl diffutils git=2.54.0-r0 mosquitto-clients openssh-client openssh-keygen py3-pip py3-yaml python3 rsync'
CACHE = vm.LOCAL / 'runtime-cache'
DEPENDENCY_TREES = {}


def copy_preserving_modes(source, destination, snapshot):
    vm.copy_input(source, destination, expected=snapshot)
    # Generic dev snapshots intentionally make directories private. Build
    # contexts and nonroot test imports must retain the original source modes.
    for location, (kind, signature) in snapshot['records'].items():
        if kind == 'directory': destination.joinpath(*location).chmod(signature[2] & 0o777)


def test_accounting(xml, *, expected=None, ext4_profile=None, recreated=False):
    root = ET.fromstring(xml)
    cases = list(root.iter('testcase'))
    skips, failures, passed, case_errors = [], [], [], []
    for case in cases:
        module, _, classes = case.get('classname', '').partition('.')
        identity = module + '.py' + ('::' + classes.replace('.', '::') if classes else '') + '::' + case.get('name', '')
        skipped = case.find('skipped')
        if sum(case.find(name) is not None for name in ('skipped', 'failure', 'error')) > 1:
            case_errors.append('Contradictory testcase outcomes: ' + identity)
        if skipped is not None:
            skips.append({'id': identity, 'reason': skipped.get('message', '')})
        elif case.find('failure') is not None or case.find('error') is not None: failures.append(identity)
        else: passed.append(identity)
    def declared_skip(item):
        if item['reason'] == 'root SSH fixture; independent graph-loss recovery is qualified through HTTPS':
            return item['id'] == 'test_native_scope.py::test_scope_authenticated_network_publication_and_same_commit_recovery[ssh-accepted-readback-loss]'
        if ext4_profile and item['id'].startswith('test_native_ext4.py::'):
            name = item['id'].split('::', 1)[1]
            profiles = {
                'test_loose_index_worktree_save': 'allocation', 'test_existing_reflog_complete_atomic_overlap': 'allocation',
                'test_native_publication_physical_boundaries': 'allocation', 'test_control_atomic_overlap': 'allocation',
                'test_independent_recovery_copies': 'allocation', 'test_no_reuse_repack': 'allocation',
                'test_short_and_eio_boundaries': 'allocation', 'test_network_publication_ext4_frozen_physical_peak': 'allocation',
                'test_kernel_byte_enospc': 'bytes', 'test_network_graph_fetch_real_kernel_enospc_after_admission': 'bytes',
                'test_kernel_inode_enospc': 'inodes'}
            expected = profiles.get(name)
            return expected is not None and ((expected != ext4_profile and item['reason'] == 'different bounded ext4 profile') or
                (expected == ext4_profile and recreated and item['reason'] == 'recreation verifies retained filesystem; original matrix retained'))
        return False
    missing = [item for item in skips if not declared_skip(item)]
    identities = [case.get('classname', '').partition('.')[0] + '.py' +
        ('::' + case.get('classname', '').partition('.')[2].replace('.', '::') if '.' in case.get('classname', '') else '') +
        '::' + case.get('name', '') for case in cases]
    from collections import Counter
    duplicates = sorted(name for name, count in Counter(identities).items() if count != 1)
    suite_errors = list(case_errors)
    for suite in root.iter('testsuite'):
        direct = list(suite.findall('testcase'))
        for attribute, count in [('tests', len(direct)), ('failures', sum(case.find('failure') is not None for case in direct)),
                ('errors', sum(case.find('error') is not None for case in direct)), ('skipped', sum(case.find('skipped') is not None for case in direct))]:
            declared = suite.get(attribute)
            if declared is None and attribute != 'tests': continue
            if declared is None or not declared.isdigit() or int(declared) != count:
                suite_errors.append('Inconsistent testsuite ' + attribute)
    if root.tag == 'testsuites' and root.get('tests') is not None and root.get('tests') != str(len(cases)):
        suite_errors.append('Inconsistent testsuites tests')
    if expected is None:
        missing.append({'id': '*', 'reason': 'Reviewed required case manifest missing'})
        absent, extra = [], []
    else:
        if len(expected) != len(set(expected)): raise vm.Refused('Duplicate reviewed required case identity')
        absent = sorted(set(expected) - set(identities))
        extra = sorted(set(identities) - set(expected))
        missing.extend({'id': name, 'reason': 'Required case absent'} for name in absent)
    missing.extend({'id': name, 'reason': 'Duplicate case'} for name in duplicates)
    missing.extend({'id': name, 'reason': 'Unexpected case'} for name in extra)
    missing.extend({'id': '*', 'reason': error} for error in suite_errors)
    if not cases: missing.append({'id': '*', 'reason': 'No tests executed'})
    return {'collected': len(cases), 'passed': len(passed), 'failed': failures, 'skipped': skips,
            'declared_unexecuted': [item for item in skips if declared_skip(item)],
            'required_unexecuted': missing, 'passed_ids': passed, 'missing_ids': absent, 'extra_ids': extra, 'duplicate_ids': duplicates, 'suite_errors': suite_errors}


def resolve(arch):
    vm.ready(for_build=True)
    cache = CACHE / arch
    cache.mkdir(parents=True, exist_ok=True)
    vm.verify_source(cache)
    base = BASES[arch]
    native_arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[vm.guest_platform()]
    resolve_base = BASES[native_arch]
    vm.docker('pull', resolve_base)
    keys = ''
    if arch != native_arch:
        # Extract public signing keys from the immutable foreign base without
        # executing foreign code or enabling emulation.
        vm.docker('pull', '--platform', 'linux/amd64' if arch == 'amd64' else 'linux/arm64', base)
        owner = uuid.uuid4().hex
        name = 'ha-ops-runtime-keys-' + owner
        container = vm.docker('create', '--name', name, '--label', 'ha-ops.dev.run=' + owner,
            '--network', 'none', '--read-only', '--entrypoint', '/bin/true', base).stdout.decode().strip()
        try: vm.docker('cp', container + ':/etc/apk/keys', str(cache / 'keys'))
        finally: vm.remove_owned(container, owner)
        keys = ' --keys-dir /out/keys'
    # apk resolves and verifies the repository metadata. Installation later
    # checks archive SHA256 and APK signatures, entirely offline.
    apk = 'apk --arch ' + {'aarch64': 'aarch64', 'amd64': 'x86_64'}[arch] + keys
    script = (apk + ' update >/dev/null; ' + apk + ' fetch --recursive --url ' + PACKAGES +
              ' > /out/urls.txt; ' + apk + ' fetch --recursive --output /out ' + PACKAGES)
    result = vm.docker('run', '--rm', '--entrypoint', '/bin/sh', '--mount',
        f'type=bind,source={cache},target=/out', resolve_base, '-ec', script)
    sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)
    urls = [line.strip() for line in (cache / 'urls.txt').read_text().splitlines()
            if line.startswith('https://') and line.endswith('.apk')]
    if not urls or len(urls) != len(set(urls)):
        raise vm.Refused('Missing or duplicate APK closure URLs')
    lines = []
    for url in sorted(urls):
        archive = cache / url.rsplit('/', 1)[1]
        if not archive.is_file(): raise vm.Refused('Resolved archive absent: ' + archive.name)
        lines.append(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + url)
    (APP / ('runtime-apk-' + arch + '.lock')).write_text('\n'.join(lines) + '\n')
    print(json.dumps({'arch': arch, 'base': base, 'archives': len(lines), 'cache': str(cache)}))


def build():
    vm.ready(for_build=True)
    platform = vm.guest_platform()
    arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[platform]
    # The exact installable context is snapshotted before Docker sees it.
    snapshot = vm.snapshot_input(APP)
    run = vm.RUNS / ('runtime-build-' + uuid.uuid4().hex)
    run.mkdir(parents=True)
    source = run / 'input'
    copy_preserving_modes(APP, source, snapshot)
    vm.verify_source(source)
    image = 'ha-ops-runtime:' + snapshot['digest'][:20] if 'digest' in snapshot else 'ha-ops-runtime:' + uuid.uuid4().hex[:20]
    try:
        result = vm.docker('build', '--platform', platform, '--build-arg', 'BUILD_ARCH=' + arch,
            '--build-arg', 'BUILD_FROM=' + BASES[arch], '--tag', image, str(source), timeout=1200, check=False)
        sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)
        if result.returncode: raise vm.Refused('Original App image build failed; see complete build output')
        image_id = vm.docker('image', 'inspect', image, '--format', '{{.Id}}').stdout.decode().strip()
        vm.verify_image_arch(image_id, platform)
        print(json.dumps({'image': image, 'image_id': image_id, 'arch': arch, 'base': BASES[arch]}))
        app_manifest = {str(path.relative_to(source / 'app')): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in (source / 'app').rglob('*') if path.is_file() and '__pycache__' not in path.parts}
        (vm.LOCAL / 'runtime-image.json').write_text(json.dumps({'image': image, 'image_id': image_id, 'arch': arch, 'base': BASES[arch],
            'app_manifest': app_manifest}, indent=2) + '\n')
    finally:
        # Only our copied build input; reusable images and APK cache survive.
        from fixture_cleanup import OwnedDirectory
        owned = OwnedDirectory(run)
        try: owned.remove()
        finally: owned.close()


def resolve_pip():
    vm.ready(for_build=True)
    cache = CACHE / 'pip'
    cache.mkdir(parents=True, exist_ok=True)
    vm.verify_source(cache)
    image = vm.image_name()
    packages = ['rfc8785==0.1.4', 'pytest==8.4.2', 'pytest-xdist==3.8.0', 'execnet==2.1.1',
                'pluggy==1.6.0', 'packaging==25.0', 'iniconfig==2.1.0', 'pygments==2.19.2']
    vm.docker('run', '--rm', '--mount', f'type=bind,source={cache},target=/out', image,
        'python3', '-m', 'pip', 'download', '--python-version', '3.14', '--no-deps', '--dest', '/out', *packages)
    production, test = [], []
    for arch in ('aarch64', 'x86_64'):
        filename = 'annotatedyaml-1.0.2-cp314-cp314-musllinux_1_2_' + arch + '.whl'
        url = 'https://wheels.home-assistant.io/musllinux-index/' + filename
        vm.docker('run', '--rm', '--mount', f'type=bind,source={cache},target=/out', image,
            'python3', '-m', 'pip', 'download', '--python-version', '3.14', '--platform', 'musllinux_1_2_' + arch,
            '--no-deps', '--dest', '/out', url)
        digest = hashlib.sha256((cache / filename).read_bytes()).hexdigest()
        production.append(f'annotatedyaml @ {url} ; platform_machine == "{arch}" --hash=sha256:{digest}')
    for archive in sorted(cache.iterdir()):
        name, version = archive.name.removesuffix('.tar.gz').split('-')[:2]
        if name == 'annotatedyaml': continue
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        row = f'{name}=={version} --hash=sha256:{digest}'
        (production if name in ('annotatedyaml', 'rfc8785') else test).append(row)
    if len(production) != 3 or len(test) != 7: raise vm.Refused('Unexpected wheel closure')
    (APP / 'runtime-pip.lock').write_text('\n'.join(production) + '\n')
    path = vm.HERE / 'runtime-toolchain.json'
    metadata = json.loads(path.read_text()) if path.exists() else {}
    metadata['pytest_wheels'] = test
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')


def runtime_image():
    data = json.loads((vm.LOCAL / 'runtime-image.json').read_text())
    image = data['image_id']
    if vm.docker('image', 'inspect', image, '--format', '{{.Id}}').stdout.decode().strip() != image:
        raise vm.Refused('Original App image identity changed')
    vm.verify_image_arch(image, vm.guest_platform())
    return data


def probe():
    vm.ready()
    data = runtime_image()
    script = 'import json,sys;sys.path.insert(0,"/app");import native_runtime;print(json.dumps(native_runtime.observe(),sort_keys=True))'
    result = vm.docker('run', '--rm', '--network', 'none', '--read-only', '--entrypoint', '/usr/bin/python3',
        data['image_id'], '-c', script)
    observed = json.loads(result.stdout)
    vm.write_report(vm.LOCAL / 'runtime-observed.json', observed)
    print(json.dumps(observed, indent=2, sort_keys=True))


def diagnose():
    vm.ready()
    data = runtime_image()
    script = '''import json,os,pathlib,subprocess,tempfile
def git(*args):
 r=subprocess.run(['/usr/bin/git',*args],capture_output=True,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'});r.check_returncode();return r.stdout
with tempfile.TemporaryDirectory() as t:
 p=pathlib.Path(t);git('init',str(p/'repo'));git('-C',str(p/'repo'),'-c','user.name=Fixture','-c','user.email=f@invalid','commit','--allow-empty','-qm','fixture')
 git('-C',str(p/'repo'),'worktree','add','--detach','--no-checkout','--lock','--reason','fixture',str(p/'tree'),'HEAD')
 print(json.dumps({'worktree_admin':[str(x.relative_to(p/'repo'/'.git'/'worktrees')) for x in (p/'repo'/'.git'/'worktrees').rglob('*')],'passwd':pathlib.Path('/etc/passwd').read_text()}))
'''
    result = vm.docker('run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
        '--entrypoint', '/usr/bin/python3', data['image_id'], '-c', script)
    print(result.stdout.decode())


def check_lock_failure(report):
    """Prove changed checksums/missing archives stop before package install."""
    vm.ready()
    arch = {'linux/arm64': 'aarch64', 'linux/amd64': 'amd64'}[vm.guest_platform()]
    owner = uuid.uuid4().hex
    root = vm.RUNS / ('runtime-negative-' + owner)
    root.mkdir(parents=True)
    script = root / 'runtime-install.sh'; script.write_bytes((APP / 'runtime-install.sh').read_bytes())
    checksum, url = (APP / ('runtime-apk-' + arch + '.lock')).read_text().splitlines()[0].split()
    rows = []
    for kind in ('checksum', 'missing'):
        lock = root / ('runtime-apk-' + arch + '.lock')
        changed = ('0' * 64 + '  ' + url if kind == 'checksum' else
                   checksum + '  ' + url.rsplit('/', 1)[0] + '/ha-ops-deliberately-absent-0.0.0-r0.apk')
        lock.write_text(changed + '\n')
        vm.verify_source(root)
        result = vm.docker('run', '--rm', '--read-only', '--tmpfs', '/tmp', '--entrypoint', '/bin/sh',
            '--mount', f'type=bind,source={script},target=/runtime-install.sh,readonly',
            '--mount', f'type=bind,source={lock},target=/tmp/{lock.name},readonly',
            BASES[arch], '/runtime-install.sh', arch, timeout=120, check=False)
        logs = (result.stdout + result.stderr).decode(errors='replace')
        expected = 'FAILED' if kind == 'checksum' else 'Pinned runtime artifact unavailable'
        if result.returncode == 0 or expected not in logs or 'Installing ' in logs:
            raise vm.Refused('Pinned artifact negative proof did not fail closed: ' + kind + '\n' + logs)
        rows.append({'case': kind, 'returncode': result.returncode, 'logs': logs})
    vm.write_report(report.resolve(), {'status': 'passed', 'cases': rows, 'base': BASES[arch]})
    print(json.dumps({'status': 'passed', 'cases': [row['case'] for row in rows]}))


def pytest_dependencies():
    vm.no_symlinks(CACHE)
    destination = Path(tempfile.mkdtemp(prefix='pytest-owned-', dir=CACHE))
    destination.chmod(0o755)
    expected_files = {}
    locks = json.loads((vm.HERE / 'runtime-toolchain.json').read_text())['pytest_wheels']
    for line in locks:
        package, expected = line.split(' --hash=sha256:')
        name, version = package.split('==')
        matches = list((CACHE / 'pip').glob(name + '-' + version + '-*.whl'))
        for match in matches: vm.no_symlinks(match)
        if len(matches) != 1 or hashlib.sha256(matches[0].read_bytes()).hexdigest() != expected:
            raise vm.Refused('Pure Python pytest artifact absent or changed: ' + package)
        with zipfile.ZipFile(matches[0]) as wheel:
            for member in wheel.namelist():
                path = Path(member)
                info = wheel.getinfo(member)
                if path.is_absolute() or '..' in path.parts or path.parts[0].split('-')[0] not in (
                        'pytest', '_pytest', 'iniconfig', 'packaging', 'pluggy', 'pygments', 'pytest.py', 'py.py',
                        'xdist', 'pytest_xdist', 'execnet') or stat.S_ISLNK(info.external_attr >> 16) or re.search(r'\.(so(?:\..*)?|pyd|dll|dylib|pth)$', member):
                    raise vm.Refused('Test wheel contains unreviewed import path: ' + member)
                if info.is_dir(): continue
                if member in expected_files: raise vm.Refused('Conflicting test wheel member: ' + member)
                payload = wheel.read(member)
                expected_files[member] = hashlib.sha256(payload).hexdigest()
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream: stream.write(payload)
    DEPENDENCY_TREES[destination] = expected_files
    validate_dependencies(destination)
    return destination


def validate_dependencies(destination):
    expected = DEPENDENCY_TREES.get(destination)
    if expected is None: raise vm.Refused('Unknown test dependency extraction; preserved for inspection')
    vm.no_symlinks(destination)
    actual = {}
    expected_directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != '.'}
    for path in destination.rglob('*'):
        info = path.lstat(); name = str(path.relative_to(destination))
        if stat.S_ISDIR(info.st_mode) and name in expected_directories: continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise vm.Refused('Foreign test dependency entry: ' + name)
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected: raise vm.Refused('Changed or stale test dependency namespace; preserved for inspection')
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


def required_group(paths=None, *, group=None):
    path = vm.HERE / 'runtime-required-cases.json'
    raw = path.read_bytes()
    lock = json.loads((vm.HERE / 'runtime-toolchain.json').read_text())['required_matrix_sha256']
    if hashlib.sha256(raw).hexdigest() != lock: raise vm.Refused('Reviewed required-case manifest hash changed')
    document = json.loads(raw)
    if document.get('schema') != 1: raise vm.Refused('Unknown required-case manifest schema')
    groups = document['groups']
    if group is None:
        matches = [name for name, item in groups.items() if item['selectors'] == list(paths or [])]
        if len(matches) != 1: raise vm.Refused('Select one predeclared complete or focused runtime group')
        group = matches[0]
    if group not in groups or (paths and list(paths) != groups[group]['selectors']):
        raise vm.Refused('Runtime selectors do not match the reviewed group')
    result = groups[group]
    if len(result['nodeids']) != len(set(result['nodeids'])) or not result['nodeids']:
        raise vm.Refused('Empty or duplicate required-case manifest')
    return group, result, lock


def verify_container(entry, identity, owner, image, mounts, network, volume):
    config, host = entry['Config'], entry['HostConfig']
    if entry['Id'] != identity or config.get('Image') != image or config.get('Labels', {}).get('ha-ops.dev.run') != owner:
        raise vm.Refused('Runtime container identity changed')
    if host.get('NetworkMode') != network or not host.get('ReadonlyRootfs') or host.get('Privileged') or host.get('Devices') or host.get('PortBindings'):
        raise vm.Refused('Runtime container isolation failed')
    actual = {row['Destination']: (row['Source'], row['RW']) for row in entry['Mounts'] if row['Type'] == 'bind'}
    if actual != {target: (str(source), writable) for target, (source, writable) in mounts.items()}:
        raise vm.Refused('Runtime container exact mounts failed')
    volumes = [row for row in entry['Mounts'] if row['Type'] == 'volume']
    if len(volumes) != 1 or volumes[0].get('Name') != volume or volumes[0]['Destination'] != '/workspace/output' or not volumes[0]['RW']:
        raise vm.Refused('Runtime exact tmpfs volume failed')
    if any(item.split('=', 1)[0].lower() in ('http_proxy', 'https_proxy', 'all_proxy') for item in config.get('Env', [])):
        raise vm.Refused('Ambient proxy in isolated runtime')


def run_tests(paths, report, *, ext4_profile=None, recreate=False, group=None, workers=0):
    group, required, matrix_hash = required_group(paths, group=group)
    paths = required['selectors']
    if workers not in (0, 2, 4) or (workers and group not in ('native-git', 'native-save', 'native-scope', 'core')):
        raise vm.Refused('Parallel workers are restricted to the independent core test groups')
    vm.ready()
    data = runtime_image()
    dependencies = pytest_dependencies()
    owner = uuid.uuid4().hex
    root = vm.RUNS / ('runtime-' + owner)
    root.mkdir(mode=0o700, parents=True)
    source, output = root / 'input', root / 'output'
    snapshot = vm.snapshot_input(APP / 'tests')
    copy_preserving_modes(APP / 'tests', source, snapshot)
    receiver_code = root / 'receiver.py'
    receiver_code.write_bytes((vm.HERE / 'runtime_receiver.py').read_bytes())
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    for path in (source, output, dependencies): vm.verify_source(path)
    clients = []
    fixture = None
    volume = 'ha-ops-runtime-' + owner
    volume_created = False
    result = {'status': 'started', 'image': data, 'containers': [], 'checks': [],
              'required_group': group, 'required_manifest_sha256': matrix_hash, 'expected_nodeids': required['nodeids'], 'workers': workers}
    try:
        vm.docker('volume', 'create', '--driver', 'local', '--label', 'ha-ops.dev.run=' + owner,
            '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt', 'o=size=1073741824,mode=1777', volume)
        volume_created = True
        volume_info = json.loads(vm.docker('volume', 'inspect', volume).stdout)[0]
        if (volume_info.get('Driver'), volume_info.get('Labels', {}).get('ha-ops.dev.run'), volume_info.get('Options')) != (
                'local', owner, {'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=1073741824,mode=1777'}):
            raise vm.Refused('Owned bounded tmpfs identity failed')
        result['tmpfs_volume'] = volume_info
        client_mounts = {'/tests': (source, False), '/testdeps': (dependencies, False)}
        ext4_env = []
        if ext4_profile:
            if os.getuid() == 0: raise vm.Refused('Ext4 App proof requires the exact nonroot user')
            vm.ext4_guest('preflight', owner, ext4_profile, owner=(65534, 65534))
            fixture = vm.ext4_guest('setup', owner, ext4_profile, owner=(65534, 65534))
            client_mounts['/workspace/ext4'] = (Path(fixture['mount_path']), True)
            token = uuid.uuid4().hex
            ext4_env = ['--user', '65534:65534', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges']
            for name, value in {'ROOT': '/workspace/ext4', 'PROFILE': ext4_profile, 'RUN': owner,
                'DEVICE': fixture['mount']['device'], 'REPORT': '/workspace/output/ext4-proof.json',
                'PRIOR_REPORT': '/workspace/output/ext4-prior.json', 'TOKEN': token, 'ATTEMPT': '0'}.items():
                ext4_env += ['--env', 'HA_OPS_EXT4_' + name + '=' + value]
        args = ['create', '--name', 'ha-ops-runtime-' + owner, '--label', 'ha-ops.dev.run=' + owner,
                '--network', 'none', '--read-only', '--tmpfs', '/tmp:exec', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'PYTHONPATH=/app:/tests:/testdeps', '--env', 'PATH=/usr/bin:/bin',
                '--env', 'HA_OPS_RUNTIME_RECEIVER_SOCKET=127.0.0.1:39117', '--entrypoint', '/usr/bin/python3',
                '--mount', f'type=volume,source={volume},target=/workspace/output']
        args += ext4_env
        for target, (source_path, writable) in client_mounts.items():
            args += ['--mount', f'type=bind,source={source_path},target={target}' + ('' if writable else ',readonly')]
        client_args = list(args)
        client = vm.docker(*args, data['image_id'], '-c', 'import time;time.sleep(1800)').stdout.decode().strip()
        clients.append(client)
        entry = json.loads(vm.docker('inspect', client).stdout)[0]
        verify_container(entry, client, owner, data['image_id'], client_mounts, 'none', volume)
        vm.docker('start', client)
        receiver_mounts = {'/tests': (source, False), '/receiver.py': (receiver_code, False)}
        if fixture: receiver_mounts['/workspace/ext4'] = (Path(fixture['mount_path']), True)
        dev_image = vm.docker('image', 'inspect', vm.image_name(), '--format', '{{.Id}}').stdout.decode().strip()
        args = ['create', '--name', 'ha-ops-receiver-' + owner, '--label', 'ha-ops.dev.run=' + owner,
                '--network', 'container:' + client, '--read-only', '--tmpfs', '/tmp:exec', '--entrypoint', '/usr/local/bin/python3',
                '--mount', f'type=volume,source={volume},target=/workspace/output']
        for target, (source_path, writable) in receiver_mounts.items():
            args += ['--mount', f'type=bind,source={source_path},target={target}' + ('' if writable else ',readonly')]
        
        if fixture: args += ['--user', '65534:65534', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
        receiver = vm.docker(*args, dev_image, '/receiver.py', '39117').stdout.decode().strip()
        clients.append(receiver)
        peer = json.loads(vm.docker('inspect', receiver).stdout)[0]
        verify_container(peer, receiver, owner, dev_image, receiver_mounts, 'container:' + client, volume)
        vm.docker('start', receiver)
        deadline = time.monotonic() + 15
        ping = 'from network_publication_helpers import receiver_request;assert receiver_request(action="ping")=="ready"'
        while vm.docker('exec', client, '/usr/bin/python3', '-c', ping, check=False).returncode:
            if time.monotonic() >= deadline: raise vm.Refused('Receiver loopback control not ready')
            time.sleep(.05)
        # Verify original modules byte-for-byte, not imports from the test bind.
        code = 'import pathlib,hashlib,json;print(json.dumps({str(p.relative_to("/app")):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path("/app").rglob("*") if p.is_file() and p.name!="config.yaml"}))'
        manifest = json.loads(vm.docker('exec', client, '/usr/bin/python3', '-c', code).stdout)
        if manifest != data['app_manifest']: raise vm.Refused('Original /app content does not match built source manifest')
        result['test_dependency_manifest_sha256'] = validate_dependencies(dependencies)
        result['containers'] = [entry, peer]
        collection = vm.docker('exec', client, '/usr/bin/python3', '-m', 'pytest', '-p', 'no:cacheprovider',
            '--collect-only', '-q', *['/tests/' + path for path in paths], timeout=120, check=False)
        collected = [match.group(1) for line in collection.stdout.decode().splitlines()
                     if (match := re.fullmatch(r'(?:/tests/)?(test_[A-Za-z0-9_]+\.py::.+)', line))]
        if collection.returncode or len(collected) != len(set(collected)) or set(collected) != set(required['nodeids']):
            raise vm.Refused('Runtime collection differs from the predeclared required identity set: ' + collection.stdout.decode()[-2000:])
        result['collected_nodeids'] = collected
        command = ['/usr/bin/python3', '-m', 'pytest', '-p', 'no:cacheprovider', '-r', 'a',
                   '--durations=20', '--basetemp=/workspace/output/pytest', '--junitxml=/workspace/output/results.xml',
                   *(['-n', str(workers)] if workers else []), *['/tests/' + path for path in paths]]
        completed = vm.docker('exec', client, *command, timeout=1200, check=False)
        sys.stdout.buffer.write(completed.stdout); sys.stderr.buffer.write(completed.stderr)
        result['stdout'] = completed.stdout.decode(errors='replace')
        result['stderr'] = completed.stderr.decode(errors='replace')
        result['returncode'] = completed.returncode
        xml = vm.docker('exec', client, '/bin/cat', '/workspace/output/results.xml', check=False)
        if xml.returncode == 0: result['junit'] = xml.stdout.decode()
        result['status'] = 'passed' if completed.returncode == 0 else 'failed'
        if xml.returncode:
            result['status'] = 'failed'
            result['accounting_error'] = 'Required test report missing'
        else:
            result['accounting'] = test_accounting(result['junit'], expected=required['nodeids'], ext4_profile=ext4_profile)
            if result['accounting']['required_unexecuted']: result['status'] = 'failed'
        if fixture and completed.returncode == 0:
            proof = json.loads(vm.docker('exec', client, '/bin/cat', '/workspace/output/ext4-proof.json').stdout)
            vm.validate_ext4_proof(proof, run_id=owner, profile=ext4_profile, attempt=0, token=token, fixture=fixture)
            result['ext4_proofs'] = [proof]
        if recreate and completed.returncode == 0:
            # Receiver retains the exact original network-none namespace; the
            # original client is removed before a second original App starts.
            vm.remove_owned(client, owner)
            args = list(client_args)
            index = args.index('--name') + 1
            args[index] += '-recreated'
            args[args.index('--network') + 1] = 'container:' + receiver
            if fixture:
                vm.docker('exec', receiver, '/bin/cp', '/workspace/output/ext4-proof.json', '/workspace/output/ext4-prior.json')
                token2 = uuid.uuid4().hex
                args = [item.replace('HA_OPS_EXT4_ATTEMPT=0', 'HA_OPS_EXT4_ATTEMPT=1').replace(
                    'HA_OPS_EXT4_TOKEN=' + token, 'HA_OPS_EXT4_TOKEN=' + token2) for item in args]
            second = vm.docker(*args, data['image_id'], '-c', 'import time;time.sleep(1800)').stdout.decode().strip()
            clients.append(second)
            second_entry = json.loads(vm.docker('inspect', second).stdout)[0]
            verify_container(second_entry, second, owner, data['image_id'], client_mounts, 'container:' + receiver, volume)
            result['containers'].append(second_entry)
            vm.docker('start', second)
            second_command = [item.replace('--basetemp=/workspace/output/pytest', '--basetemp=/workspace/output/pytest-recreated') for item in command]
            repeated = vm.docker('exec', second, *second_command, timeout=1200, check=False)
            result['recreated'] = {'returncode': repeated.returncode, 'stdout': repeated.stdout.decode(errors='replace'),
                                   'stderr': repeated.stderr.decode(errors='replace')}
            recreated_xml = vm.docker('exec', second, '/bin/cat', '/workspace/output/results.xml', check=False)
            if recreated_xml.returncode == 0:
                result['recreated']['accounting'] = test_accounting(recreated_xml.stdout.decode(), expected=required['nodeids'], ext4_profile=ext4_profile, recreated=True)
                if result['recreated']['accounting']['required_unexecuted']: result['status'] = 'failed'
            else: result['status'] = 'failed'
            sys.stdout.buffer.write(repeated.stdout); sys.stderr.buffer.write(repeated.stderr)
            if repeated.returncode: result['status'] = 'failed'
            if fixture and repeated.returncode == 0:
                second_proof = json.loads(vm.docker('exec', second, '/bin/cat', '/workspace/output/ext4-proof.json').stdout)
                vm.validate_ext4_proof(second_proof, run_id=owner, profile=ext4_profile, attempt=1,
                    token=token2, fixture=fixture, previous=proof)
                result['ext4_proofs'].append(second_proof)
    except BaseException as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        failures = []
        for container in reversed(clients):
            try: vm.remove_owned(container, owner)
            except BaseException as error: failures.append(str(error))
        if volume_created and not failures:
            try:
                info = json.loads(vm.docker('volume', 'inspect', volume).stdout)[0]
                if info.get('Labels', {}).get('ha-ops.dev.run') != owner: raise vm.Refused('Tmpfs owner changed before cleanup')
                vm.docker('volume', 'rm', volume)
                absent = vm.docker('volume', 'inspect', volume, check=False)
                if absent.returncode == 0 or b'no such volume' not in absent.stderr.lower():
                    raise vm.Refused('Tmpfs volume absence not proven')
            except BaseException as error: failures.append('tmpfs cleanup: ' + str(error))
        result['cleanup_errors'] = failures
        if fixture and not failures:
            try:
                result['ext4_cleanup'] = vm.ext4_guest('cleanup', owner, ext4_profile)
                result['ext4_cleanup_repeat'] = vm.ext4_guest('cleanup', owner, ext4_profile)
            except BaseException as error: failures.append('ext4 cleanup: ' + str(error))
        if failures: result['status'] = 'failed'
        try:
            validate_dependencies(dependencies)
            from fixture_cleanup import OwnedDirectory
            owned_dependencies = OwnedDirectory(dependencies)
            try: owned_dependencies.remove()
            finally: owned_dependencies.close()
            DEPENDENCY_TREES.pop(dependencies, None)
        except BaseException as error:
            failures.append('test dependency cleanup: ' + str(error)); result['status'] = 'failed'
        result['fixture'] = str(root)
        vm.write_report(report.resolve(), result)
        if failures: raise vm.Refused('Owned runtime container cleanup failed: ' + repr(failures))
    return 0 if result['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    resolve_parser = commands.add_parser('resolve')
    resolve_parser.add_argument('--arch', choices=BASES, required=True)
    commands.add_parser('build')
    commands.add_parser('resolve-pip')
    commands.add_parser('probe')
    commands.add_parser('diagnose')
    negative = commands.add_parser('check-lock-failure')
    negative.add_argument('--report', required=True, type=Path)
    run = commands.add_parser('run')
    run.add_argument('--report', required=True, type=Path)
    run.add_argument('--ext4-profile', choices=vm.EXT4_SIZES)
    run.add_argument('--recreate', action='store_true')
    run.add_argument('--group')
    run.add_argument('--workers', type=int, choices=(0, 2, 4), default=0)
    run.add_argument('tests', nargs='*')
    args = parser.parse_args()
    if args.action == 'resolve': resolve(args.arch)
    elif args.action == 'build': build()
    elif args.action == 'resolve-pip': resolve_pip()
    elif args.action == 'probe': probe()
    elif args.action == 'diagnose': diagnose()
    elif args.action == 'check-lock-failure': check_lock_failure(args.report)
    elif args.action == 'run': return run_tests(args.tests, args.report, ext4_profile=args.ext4_profile, recreate=args.recreate, group=args.group, workers=args.workers)


if __name__ == '__main__': sys.exit(main())
