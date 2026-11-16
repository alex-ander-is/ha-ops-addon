"""Private, bounded success receipts for canonical selective host jobs.

Local integrity/freshness evidence, not an attestation against the cache owner.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile

import test_plan as shared

__test__ = False
SCHEMA = 1
MAX_CACHE = 64 * 1024 * 1024
# These families inspect real checkout metadata or exercise the controller/tools.
# Source hashes cannot bound their external Git/process configuration contracts.
REFUSED_CATEGORIES = {'runner', 'release', 'virtualization'}


def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def zero(value):
    return type(value) is int and value == 0


def regular_bytes(path):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise shared.Refused('Symlink evidence/input is unsupported')
    # Reject special files before open, and keep a replacement FIFO from
    # blocking between lstat and fstat. The descriptor remains authoritative.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise shared.Refused('Evidence/input is not a regular file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise shared.Refused('Evidence/input is not a regular file')
        raw = handle.read(MAX_CACHE + 1)
        if len(raw) > MAX_CACHE:
            raise shared.Refused('Evidence/input exceeds receipt limit')
        return raw


def file_state(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'type': 'absent'}
    if not stat.S_ISREG(info.st_mode):
        raise shared.Refused('Nonregular receipt input: ' + str(path))
    return {'type': 'file', 'mode': stat.S_IMODE(info.st_mode),
            'sha256': shared.digest(regular_bytes(path))}


def runtime(root, *, qualified=False):
    # Match guarded()'s effective inherited environment; output paths are owned
    # launch metadata and normalized, never raw environment values in evidence.
    env = os.environ.copy()
    for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(key, None)
    env.update(PYTHONDONTWRITEBYTECODE='1', TEST_PLAN_OUTPUT='<owned-output>',
               TEST_PLAN_ROOT=str(root))
    env['PYTHONPATH'] = str(shared.HERE) + os.pathsep + env.get('PYTHONPATH', '')
    executables = {}
    for name, value in [('python', sys.executable), ('git', shutil.which('git')),
                        ('sh', shutil.which('sh')), ('node', shutil.which('node')),
                        ('docker', shutil.which('docker')), ('colima', shutil.which('colima'))]:
        if value:
            target = Path(value).resolve()
            executables[name] = {'path': str(target), **file_state(target)}
        else:
            executables[name] = None
    library = Path(sysconfig.get_config_var('LIBDIR') or '') / (sysconfig.get_config_var('LDLIBRARY') or '')
    if library.is_file():
        executables['python-library'] = file_state(library.resolve())
    result = {'executables': executables, 'python': sys.version,
              'implementation': platform.python_implementation(),
              'build': list(platform.python_build()), 'os': list(platform.uname()),
              'packages': shared.identity()['packages']}
    if not qualified:
        config = subprocess.run(['git', '-C', str(root), 'config', '--null', '--list', '--show-origin', '--includes'],
                                capture_output=True, env=env)
        if config.returncode:
            raise shared.Refused('Cannot bind effective Git configuration')
        result.update(environment=shared.digest(shared.canonical(env).encode()),
                      git_config=shared.digest(config.stdout))
    # Qualified tests receive the fixed immutable Linux ENV/config. Retain
    # controller code/executable identity without unrelated shell/TTY values.
    return result



def positive_qualification(root, data, area, nodes):
    """Only a reviewed execution/input contract may grant receipt authority.

    The AST inventory describes uncertainty; absence of lexical findings does
    not prove actual import, startup, fixture, filesystem or process closure.
    Until a qualified profile is implemented, execution remains available but
    production success cannot mint or reuse a receipt.
    """
    import test_receipt_linux
    return test_receipt_linux.contract(root, data, area, nodes)


def binding(root, data, area, nodes, profile, categorizer):
    if area in REFUSED_CATEGORIES:
        raise shared.Refused('Category requires unbounded checkout/controller qualification')
    if not nodes:
        raise shared.Refused('Empty receipt category')
    # The immutable reviewed Reset route supplies positive authority. Generic
    # inventory uncertainty (or its absence) never grants that authority.
    try:
        qualification = positive_qualification(root, data, area, nodes)
    except shared.Refused:
        if any(type(node.get('unknown')) is not list or node['unknown'] for node in nodes):
            raise shared.Refused('Unresolved runtime dependency influence; category cannot receive reusable proof')
        raise shared.Refused('No reviewed execution/input qualification; empty lexical unknown is not reusable proof')
    if (qualification.get('kind') != 'reviewed-reset-linux-v1' and
            any(type(node.get('unknown')) is not list or node['unknown'] for node in nodes)):
        raise shared.Refused('Unresolved runtime dependency influence; category cannot receive reusable proof')
    namespace = {os.fsdecode(p) for p in shared.git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0') if p}
    namespace = {p for p in namespace if not shared.generated_path(p)}
    dependencies = {p for node in nodes for p in node.get('dependencies', [])} | {n['source'] for n in nodes}
    # Only actual, independently collected test modules outside this category
    # are excluded. Uncollected helpers/data and all global providers stay common.
    other_tests = {n['source'] for n in data['nodes'] if categorizer(n) != area} - dependencies
    other_tests = {p for p in other_tests if Path(p).name.startswith('test_') and Path(p).name != 'conftest.py'}
    inputs = {p: file_state(root / p) for p in sorted((namespace | dependencies) - other_tests)}
    tools = {p.name: file_state(p) for p in sorted(shared.HERE.glob('test*.py'))}
    tools['selective_pre_push.py'] = file_state(shared.HERE / 'selective_pre_push.py')
    tools['reset-policy'] = file_state(shared.HERE / 'test_receipt_reset_policy.json')
    tools['linux-wrapper'] = file_state(shared.HERE / 'virtualization/ha_ops_dev.py')
    value = {'schema': SCHEMA, 'repository': str(root.resolve()), 'category': area,
            'nodes': sorted(nodes, key=lambda n: n['id']), 'profile': profile,
            'qualification': qualification, 'rules': data['rules'], 'inputs': inputs, 'tools': tools, 'runtime': runtime(root, qualified=True) if qualification.get('kind') == 'reviewed-reset-linux-v1' else runtime(root)}
    return strict_json(shared.canonical(value))


def machine_success(record, expected, workers):
    if (type(record) is not dict or type(record.get('schema')) is not int or record['schema'] != 1 or
            not zero(record.get('exitstatus')) or record.get('errors') != [] or record.get('deselected') != []):
        raise shared.Refused('Invalid receipt machine finalization')
    if not expected or len(expected) != len(set(expected)):
        raise shared.Refused('Invalid receipt expected membership')
    collections = record.get('worker_collections') if workers else [record.get('collected')]
    if type(collections) is not list or len(collections) != (workers or 1):
        raise shared.Refused('Incomplete worker collections')
    for rows in collections:
        if type(rows) is not list or len(rows) != len(expected) or set(rows) != set(expected):
            raise shared.Refused('Receipt collection membership differs')
    phases = set()
    reports = record.get('reports')
    if type(reports) is not list:
        raise shared.Refused('Missing receipt phases')
    for row in reports:
        if (type(row) is not dict or row.get('id') not in expected or row.get('when') not in ('setup', 'call', 'teardown') or
                row.get('outcome') != 'passed' or row.get('xfail') is not False):
            raise shared.Refused('Receipt requires every phase passed without xfail')
        key = (row['id'], row['when'])
        if key in phases:
            raise shared.Refused('Duplicate receipt phase')
        phases.add(key)
    if phases != {(node, phase) for node in expected for phase in ('setup', 'call', 'teardown')}:
        raise shared.Refused('Missing receipt phase')


def controller_success(manifest):
    if (type(manifest) is not dict or manifest.get('status') != 'passed' or manifest.get('reason') != 'completed' or
            not zero(manifest.get('raw_returncode')) or not zero(manifest.get('exit_code')) or
            not zero(manifest.get('omitted_error_count')) or manifest.get('errors') != [] or
            manifest.get('process_group_cleanup') != 'owned cleanup attempted' or manifest.get('signal', 'missing') is not None):
        raise shared.Refused('Invalid receipt controller finalization')


def validate_job(job):
    if (type(job) is not dict or type(job.get('workers')) is not int or not 1 <= job['workers'] <= 10 or
            type(job.get('expected')) is not list or not all(type(n) is str for n in job['expected'])):
        raise shared.Refused('Invalid receipt job shape')
    if not zero(job.get('outer_returncode')):
        raise shared.Refused('Missing successful outer invocation')
    machine = strict_json(job['machine'])
    manifest = strict_json(job['controller'])
    if shared.digest(job['machine'].encode()) != job['machine_hash'] or shared.digest(job['controller'].encode()) != job['controller_hash']:
        raise shared.Refused('Receipt evidence digest mismatch')
    controller_success(manifest)
    machine_success(machine, job['expected'], job['workers'])
    # Store the exact canonical route plus selectors as original execution evidence.
    if job.get('route') == 'qualified-reset-linux-v1':
        import test_receipt_linux as linux
        if shared.digest(job['outer'].encode()) != job['outer_hash']:
            raise shared.Refused('Outer qualified evidence digest mismatch')
        selectors = ['-n', str(job['workers']), '--dist', 'worksteal', *job['expected']]
        command = ['python3', 'dev/test_receipt_linux.py', '--child', json.dumps(selectors, separators=(',', ':'))]
        linux.validate_outer(strict_json(job['outer']), command)
        if job['command'] != command:
            raise shared.Refused('Qualified child command differs')
        return
    argv = job['argv']
    prefix = [sys.executable, str(shared.HERE / 'test_runner.py'), '--deadline', '1800',
              '--artifacts', job['runner_artifacts'], '--', '--rootdir', job['root'],
              '-p', 'test_plan_plugin', '-p', 'no:cacheprovider', '-n', str(job['workers']), '--dist', 'worksteal']
    if argv != prefix + ['@' + row['path'] for row in job['selectors']]:
        raise shared.Refused('Noncanonical receipt command')
    if [node for row in job['selectors'] for node in row['ids']] != job['expected']:
        raise shared.Refused('Receipt selectors differ from expected nodes')


def capture(root, record, evidence, expected, workers, arguments, directory):
    machine_raw = regular_bytes(evidence['records']).decode()
    controller_raw = regular_bytes(evidence['manifest']).decode()
    if strict_json(machine_raw) != record:
        raise shared.Refused('Machine evidence differs from returned record')
    job = {'machine': machine_raw, 'controller': controller_raw,
           'machine_hash': shared.digest(machine_raw.encode()), 'controller_hash': shared.digest(controller_raw.encode()),
           'outer_returncode': evidence.get('outer_returncode'), 'expected': expected, 'workers': workers,
           'argv': evidence['argv'], 'root': str(root), 'runner_artifacts': str(directory / 'runner'),
           'selectors': [{'path': arg[1:], 'ids': regular_bytes(arg[1:]).decode().splitlines()}
                         for arg in arguments if arg.startswith('@')]}
    validate_job(job)
    return job


class Store:
    def __init__(self, root):
        common = Path(os.fsdecode(shared.git(root, 'rev-parse', '--git-common-dir')).strip())
        common = common if common.is_absolute() else root / common
        self.path = common / 'ha-ops-test-receipts-v1'
        for parent in (self.path, *self.path.parents):
            if parent.is_symlink():
                raise shared.Refused('Symlink receipt cache is unsupported')
        self.path.mkdir(mode=0o700, exist_ok=True)
        info = self.path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise shared.Refused('Receipt cache must be private and owned')
        self.index = self.path / 'latest.json'
        self.value = {'schema': SCHEMA, 'categories': {}, 'jobs': {}}
        try:
            value = strict_json(regular_bytes(self.index))
            if type(value.get('schema')) is int and value['schema'] == SCHEMA and type(value.get('categories')) is dict and type(value.get('jobs')) is dict:
                self.value = value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def lookup(self, area, current):
        try:
            entry = self.value['categories'][area]
            if shared.canonical(entry['binding']) != shared.canonical(current):
                return False, 'source/membership/environment/profile binding changed'
            expected = {n['id'] for n in current['nodes']}
            seen = []
            if not entry['jobs'] or len(entry['jobs']) != len(set(entry['jobs'])):
                raise ValueError('Missing/duplicate jobs')
            workers = []
            for key in entry['jobs']:
                job = self.value['jobs'][key]
                if shared.digest(shared.canonical(job).encode()) != key:
                    raise ValueError('Job digest mismatch')
                validate_job(job)
                if job.get('route', 'canonical-host') != current['profile']['route']:
                    raise ValueError('Execution route differs from binding')
                if job['root'] != current['repository']:
                    raise ValueError('Foreign repository evidence')
                workers.append(job['workers'])
                seen.extend(node for node in job['expected'] if node in expected)
            if shared.canonical(workers) != shared.canonical(current['profile']['workers']):
                raise ValueError('Execution profile differs from binding')
            if len(seen) != len(expected) or set(seen) != expected:
                raise ValueError('Incomplete category evidence')
            return True, 'matching complete canonical success evidence'
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            return False, 'missing or invalid complete success evidence'

    def publish(self, candidates, jobs):
        if not candidates:
            return
        value = {'schema': SCHEMA, 'categories': dict(self.value['categories']), 'jobs': dict(self.value['jobs'])}
        value['categories'].update(candidates)
        value['jobs'].update(jobs)
        referenced = {key for row in value['categories'].values() for key in row.get('jobs', [])}
        value['jobs'] = {key: job for key, job in value['jobs'].items() if key in referenced}
        raw = shared.canonical(value).encode()
        if len(raw) > MAX_CACHE:
            raise shared.Refused('Receipt cache exceeds bounded limit')
        fd, name = tempfile.mkstemp(prefix='publish-', dir=self.path)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.index)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.value = value
