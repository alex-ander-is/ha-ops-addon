"""Source-reviewed Reset receipt route through the existing cached Linux wrapper."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import test_plan as shared

__test__ = False
IMAGE = 'sha256:71bc9525ffea04b43a63ddc32551b00977203c0b4346359b8257d16faeaa297e'
PROFILE = {'image': IMAGE, 'platform': 'linux/arm64'}
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp/receipt-home',
       'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC',
       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '/workspace/input/dev',
       'TEST_PLAN_ROOT': '/workspace/input', 'TEST_PLAN_OUTPUT': '/workspace/output/test-plan.json',
       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null',
       'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/tmp/receipt-hooks',
       'GIT_CONFIG_KEY_1': 'init.templateDir', 'GIT_CONFIG_VALUE_1': '/tmp/receipt-template',
       'GIT_CONFIG_KEY_2': 'core.excludesFile', 'GIT_CONFIG_VALUE_2': '/dev/null',
       'GIT_TEMPLATE_DIR': '/tmp/receipt-template', 'GIT_TERMINAL_PROMPT': '0',
       'GIT_NO_REPLACE_OBJECTS': '1',
       'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
       'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def contract(root, data, area, nodes):
    if area != 'reset':
        raise shared.Refused('No reviewed execution/input qualification for category')
    path = shared.HERE / 'test_receipt_reset_policy.json'
    from test_receipts import regular_bytes, strict_json
    policy_bytes = regular_bytes(path)
    policy = strict_json(policy_bytes)
    if type(policy) is not dict or type(policy.get('schema')) is not int or policy['schema'] != 1 or policy.get('image') != IMAGE:
        raise shared.Refused('Unsupported reviewed Reset qualification')
    sources = policy['sources']
    # Exact source namespace: no new importable App module or global fixture can
    # enter a reviewed contract merely because it was hashed after appearing.
    actual_app = {p.relative_to(root).as_posix() for p in (root / 'ha-ops/app').rglob('*.py')}
    if actual_app != {p for p in sources if p.startswith('ha-ops/app/')}:
        raise shared.Refused('Reviewed Reset App source namespace changed')
    if any(Path(p).name == 'conftest.py' for p in data['inputs']):
        raise shared.Refused('Unreviewed global fixture provider')
    if len(nodes) != policy['membership_count'] or {n['source'] for n in nodes} != set(policy['test_sources']):
        raise shared.Refused('Reviewed Reset generated membership changed')
    for name, expected in sources.items():
        target = root / name
        if target.is_symlink() or not target.is_file() or shared.digest(regular_bytes(target)) != expected:
            raise shared.Refused('Reviewed Reset source changed: ' + name)
    # Collection/config hooks themselves form part of this source qualification.
    configs = {'pytest.ini', '.pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini'}
    for name in data['inputs']:
        if Path(name).name in configs:
            raise shared.Refused('Unreviewed pytest configuration: ' + name)
    for name in configs:
        if (root / name).exists():
            raise shared.Refused('Unreviewed pytest configuration: ' + name)
    return {'kind': 'reviewed-reset-linux-v1', 'policy': shared.digest(policy_bytes),
            'image': IMAGE, 'platform': PROFILE['platform'], 'runtime': cached_runtime(), 'environment': shared.digest(shared.canonical(ENV).encode())}


def wrapper():
    spec = importlib.util.spec_from_file_location('receipt_owned_linux_wrapper', shared.HERE / 'virtualization/ha_ops_dev.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def cached_runtime():
    """Observe the actual existing engine/kernel/image before admission/reuse."""
    module = wrapper()
    try:
        engine = module.endpoint_info()
        image = json.loads(module.docker('image', 'inspect', IMAGE).stdout)[0]
        if image.get('Id') != IMAGE or image.get('Os') != 'linux' or image.get('Architecture') != 'arm64':
            raise shared.Refused('Qualified cached image identity differs')
        fields = ('KernelVersion', 'ServerVersion', 'Architecture', 'OSType', 'Name')
        if any(type(engine.get(key)) is not str or not engine[key] for key in fields):
            raise shared.Refused('Missing qualified engine/kernel identity')
        return {key: engine[key] for key in fields}
    except (module.Refused, OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        raise shared.Refused('Qualified cached runtime unavailable: ' + type(error).__name__) from error

def validate_outer(value, expected_command):
    from test_receipts import zero
    p = value.get('receipt_profile', {})
    if (value.get('status') != 'passed' or not zero(value.get('command_exit_code')) or
            value.get('failure') is not None or value.get('primary_error') is not None or
            value.get('cleanup_errors') != [] or value.get('capture_errors') != [] or
            p.get('image') != IMAGE or p.get('readonly') is not True or p.get('network') != 'none' or
            p.get('tmpfs') != {'/tmp': 'exec'} or p.get('privileged') is not False or
            p.get('devices') not in (None, []) or p.get('command') != expected_command or
            p.get('mounts') != [{'destination': '/workspace/input', 'rw': False}, {'destination': '/workspace/output', 'rw': True}]):
        raise shared.Refused('Invalid qualified outer controller/security evidence')
    if any(type(row.get('rw')) is not bool for row in p.get('mounts', [])):
        raise shared.Refused('Nonboolean qualified mount authority')
    containers = value.get('containers')
    if type(containers) is not list or len(containers) != 1:
        raise shared.Refused('Missing exact qualified container outcome')
    row = containers[0]
    if (not zero(row.get('wait_exit_code')) or not zero(row.get('docker_wait_returncode')) or
            not zero(row.get('supervisor_exit_code')) or row.get('watchdog_reason') != 'completed' or
            row.get('wait_failure') is not None or row.get('state', {}).get('OOMKilled') is not False or
            not zero(row.get('state', {}).get('ExitCode')) or row.get('state', {}).get('Status') != 'exited'):
        raise shared.Refused('Failed/incomplete qualified container outcome')



def make_stage(root):
    # The established wrapper anchors snapshots inside its repository. This
    # existing ignored artifact parent avoids macOS /var symlinks and never
    # enters the fingerprinted Git namespace.
    module = wrapper()
    parent = root / 'ha-ops/tests/browser/artifacts'
    module.no_symlinks(parent)
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix='receipt-qualified-source-', dir=parent))

def launch(root, data, nodes, arguments, directory, included):
    from test_receipts import regular_bytes, strict_json, controller_success, machine_success
    directory.mkdir(mode=0o700)
    expected = [line for arg in arguments if arg.startswith('@') for line in Path(arg[1:]).read_text().splitlines()]
    workers = int(arguments[1])
    selectors = ['-n', str(workers), '--dist', 'worksteal', *expected]
    command = ['python3', 'dev/test_receipt_linux.py', '--child', json.dumps(selectors, separators=(',', ':'))]
    stage = make_stage(root)
    module = wrapper()
    try:
        for name in included:
            source = root / name
            if not source.exists():
                continue
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
        code, evidence = module.generic_run(stage, command, receipt_profile=PROFILE)
        if code != 0:
            raise shared.Refused('Qualified Linux invocation failed: ' + str(evidence))
        outer_raw = regular_bytes(evidence / 'manifest.json').decode()
        validate_outer(strict_json(outer_raw), command)
        manifests = list((evidence / 'inner').glob('run-*/manifest.json'))
        if len(manifests) != 1:
            raise shared.Refused('Missing exact qualified inner controller')
        controller_raw = regular_bytes(manifests[0]).decode()
        machine_raw = regular_bytes(evidence / 'test-plan.json').decode()
        controller_success(strict_json(controller_raw))
        record = strict_json(machine_raw)
        machine_success(record, expected, workers)
        job = {'route': 'qualified-reset-linux-v1', 'root': str(root), 'workers': workers,
               'expected': expected, 'outer_returncode': code, 'command': command,
               'controller': controller_raw, 'machine': machine_raw, 'outer': outer_raw,
               'controller_hash': shared.digest(controller_raw.encode()),
               'machine_hash': shared.digest(machine_raw.encode()), 'outer_hash': shared.digest(outer_raw.encode())}
        shared.write_json(directory / 'job.json', job)
        return record, {'qualified_job': job, 'outer_manifest': str(evidence / 'manifest.json')}
    except (module.Refused, subprocess.SubprocessError) as error:
        raise shared.Refused('Qualified Linux route failed: ' + module.guard.redact(str(error))) from error
    finally:
        shutil.rmtree(stage)



def prepare_child_environment(base=Path('/tmp')):
    env = dict(ENV)
    for key in ('HOME', 'GIT_CONFIG_VALUE_0', 'GIT_CONFIG_VALUE_1', 'GIT_TEMPLATE_DIR'):
        env[key] = str(base / Path(env[key]).relative_to('/tmp'))
        Path(env[key]).mkdir(mode=0o700, parents=True, exist_ok=True)
    # Normal git-init fixtures edit this file; an entirely empty template
    # removes that Git directory contract. Keep it owned and deterministic.
    info = Path(env['GIT_TEMPLATE_DIR']) / 'info'
    info.mkdir(mode=0o700)
    (info / 'exclude').write_text('')
    return env

def child(selectors):
    # Rootfs and image are checked externally. No host startup/config/home/env is
    # inherited; this clean process and all subprocesses live inside that image.
    if sys.platform != 'linux' or Path('/data').exists():
        raise shared.Refused('Qualified Reset requires Linux and absent default /data')
    env = prepare_child_environment()
    command = ['/usr/local/bin/python3', 'dev/test_runner.py', '--deadline', '240',
               '--artifacts', '/workspace/output/test-diagnostics', '--',
               '--rootdir', '/workspace/input', '-p', 'test_plan_plugin', '-p', 'no:cacheprovider', *selectors]
    os.execve(command[0], command, env)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child':
        raise SystemExit('Only the owned qualified child entrypoint is supported')
    child(json.loads(sys.argv[2]))
