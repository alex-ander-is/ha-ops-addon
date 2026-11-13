"""Intentional process death is confined to disposable children and asserted here."""
import importlib.util
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('test_guard_subject', ROOT / 'dev/test_runner.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def run_child(tmp_path, code, deadline=5):
    result, path = guard.supervise([sys.executable, '-u', '-c', code], timeout=deadline,
                                   artifact_root=tmp_path / 'evidence')
    return result, path, json.loads((path / 'manifest.json').read_text())


@pytest.mark.parametrize('code,expected,raw,number', [
    ('print("assertion ran"); assert False', 1, 1, None),
    ('import os; print("before exit"); os._exit(73)', 73, 73, None),
    ('import os,signal; print("before segv"); os.kill(os.getpid(),signal.SIGSEGV)', 139, -11, 11),
    ('import os,signal; print("before kill"); os.kill(os.getpid(),signal.SIGKILL)', 137, -9, 9),
    ('print("normal success")', 0, 0, None),
])
def test_explicit_isolated_child_status(tmp_path, code, expected, raw, number):
    status, path, manifest = run_child(tmp_path, code)
    assert status == expected
    assert manifest['raw_returncode'] == raw and manifest['signal'] == number
    assert manifest['status'] == ('passed' if expected == 0 else 'failed')
    assert (path / 'stdout.log').read_text()
    assert path.stat().st_mode & 0o777 == 0o700
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in path.iterdir())


def test_timeout_partial_streams_no_frame_dump_and_owned_descendants(tmp_path):
    start = time.monotonic()
    status, path, manifest = run_child(tmp_path, 'import subprocess,sys,time; print("milestone"); print("stderr milestone",file=sys.stderr); subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); time.sleep(60)', .3)
    assert status == 124 and manifest['reason'] == 'timeout'
    assert manifest['raw_returncode'] in (-15, -9)
    assert time.monotonic() - start < 4
    assert 'milestone' in (path / 'stdout.log').read_text()
    assert 'stderr milestone' in (path / 'stderr.log').read_text()


def test_early_success_with_inherited_pipes_is_bounded(tmp_path):
    start = time.monotonic()
    status, path, manifest = run_child(tmp_path, 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print("leader done")', 3)
    assert time.monotonic() - start < 3
    assert manifest['raw_returncode'] == 0 and status != 0
    assert manifest['reason'] == 'orphaned_descendants'
    assert 'leader done' in (path / 'stdout.log').read_text()


def test_output_is_bounded_and_split_secrets_redacted(tmp_path, capsys):
    status, path, manifest = run_child(tmp_path, 'import os; os.write(1,b"token=split"); os.write(1,b"credential\\n"); os.write(2,b"password=hunter2\\n"); os.write(1,b"x"*4000000); os.write(2,b"safe line\\n"*200000)')
    assert status == 0
    combined = ''.join(item.read_text() for item in path.iterdir()) + capsys.readouterr().out + capsys.readouterr().err
    assert 'splitcredential' not in combined and 'hunter2' not in combined
    assert manifest['streams']['stdout']['truncated']
    assert all((path / (name + '.log')).stat().st_size <= guard.CAP for name in ('stdout', 'stderr'))


def test_runs_never_overwrite_and_symlink_root_refused(tmp_path):
    _, first, _ = run_child(tmp_path, 'print("one")')
    _, second, _ = run_child(tmp_path, 'print("two")')
    assert first != second and 'one' in (first / 'stdout.log').read_text()
    link = tmp_path / 'link'; link.symlink_to(tmp_path / 'evidence', target_is_directory=True)
    with pytest.raises(guard.Refused): guard.supervise(['true'], artifact_root=link)


@pytest.mark.parametrize('arguments', [
    ['-o', 'faulthandler_timeout=9'], ['-ofaulthandler_timeout=9'],
    ['--override-ini=faulthandler_timeout=9', '-o', 'faulthandler_timeout=5'], [],
    ['--'], ['@args.txt'],
])
def test_effective_policy_and_real_assertions(tmp_path, monkeypatch, arguments):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pytest.ini').write_text('[pytest]\nfaulthandler_timeout = 0.001\naddopts = -o faulthandler_timeout=2\n')
    (tmp_path / 'sitecustomize.py').write_text('import faulthandler\ndef poison(*a,**k):\n raise RuntimeError("timed dump called")\nfaulthandler.dump_traceback_later=poison\n')
    (tmp_path / 'test_probe.py').write_text('def test_actual(pytestconfig):\n assert float(pytestconfig.getini("faulthandler_timeout")) == 0\n assert sum(range(10)) == 45\n import time\n start=time.monotonic()\n while time.monotonic()-start<.3: any(x in (b\"\",b\".\",b\"..\") for x in b\"a/b/c/d/e\".split(b\"/\"))\n')
    (tmp_path / 'args.txt').write_text('-o\nfaulthandler_timeout=4\n')
    env = os.environ.copy(); env['PYTEST_ADDOPTS'] = '-o faulthandler_timeout=3'; env['PYTHONPATH'] = str(tmp_path)
    command = guard.pytest_command([*arguments, 'test_probe.py'], env)
    status, path = guard.supervise(command, env=env, timeout=10, artifact_root=tmp_path / 'evidence')
    assert status == 0, (path / 'stderr.log').read_text()
    assert '1 passed' in (path / 'stdout.log').read_text()


@pytest.mark.parametrize('args,env', [
    (['-p', 'no:xdist'], {}), (['--reruns=3'], {}),
    ([], {'PYTEST_ADDOPTS': '--'}), ([], {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}),
])
def test_unsupported_policy_bypass_fails_closed(tmp_path, monkeypatch, args, env):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(guard.Refused): guard.pytest_command(args, env)


def test_config_terminator_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pytest.ini').write_text('[pytest]\naddopts = --\n')
    with pytest.raises(guard.Refused): guard.pytest_command([])


@pytest.mark.parametrize('workers', [[], ['-n', '2']])
def test_collection_no_tests_and_xdist_no_restart(tmp_path, monkeypatch, workers):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'test_probe.py').write_text('def test_ok(): assert True\n')
    command = guard.pytest_command([*workers, 'test_probe.py'])
    assert '--max-worker-restart=0' in command
    status, _ = guard.supervise(command, timeout=15, artifact_root=tmp_path / 'evidence')
    assert status == 0
    status, _ = guard.supervise(guard.pytest_command(['--collect-only', 'test_probe.py']), timeout=5, artifact_root=tmp_path / 'evidence')
    assert status == 0
    status, _ = guard.supervise(guard.pytest_command(['-k', 'absent', 'test_probe.py']), timeout=5, artifact_root=tmp_path / 'evidence')
    assert status == 5


def wrapper():
    spec = importlib.util.spec_from_file_location('guard_vm_subject', ROOT / 'dev/virtualization/ha_ops_dev.py')
    subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)
    return subject


@pytest.mark.parametrize('prefix', [[], ['env', 'GIT_AUTHOR_NAME=Fixture', 'GIT_AUTHOR_EMAIL=fixture@example.invalid', 'GIT_COMMITTER_NAME=Fixture', 'GIT_COMMITTER_EMAIL=fixture@example.invalid']])
@pytest.mark.parametrize('executable', [['python3', '-m', 'pytest'], ['pytest'], ['python3', 'dev/test_runner.py']])
def test_linux_recognition_preserves_selectors_workers_and_identity(prefix, executable):
    command = wrapper().protected_command(prefix + executable + ['--', '-n', '2', 'ha-ops/tests/test_server.py'])
    assert command[:len(prefix)] == prefix
    assert 'dev/test_runner.py' in command
    assert '/workspace/output/test-diagnostics' in command
    assert command[-3:] == ['-n', '2', 'ha-ops/tests/test_server.py']


def test_unsupported_env_flags_refused_and_arbitrary_command_explicit():
    subject = wrapper()
    with pytest.raises(subject.Refused): subject.protected_command(['env', '-u', 'X', 'python3', '-m', 'pytest'])
    command = ['python3', '-c', 'print(1)']
    assert subject.protected_command(command) == command


@pytest.mark.parametrize('name,contents', [
    ('tox.ini', '[pytest]\nfaulthandler_timeout=2\n'),
    ('setup.cfg', '[tool:pytest]\nfaulthandler_timeout=2\n'),
    ('pyproject.toml', '[tool.pytest.ini_options]\nfaulthandler_timeout=2\naddopts=["-o", "faulthandler_timeout=4"]\n'),
])
def test_alternate_configuration_policy(tmp_path, monkeypatch, name, contents):
    monkeypatch.chdir(tmp_path)
    (tmp_path / name).write_text(contents)
    (tmp_path / 'test_probe.py').write_text('def test_config(pytestconfig): assert float(pytestconfig.getini("faulthandler_timeout")) == 0\n')
    status, _ = guard.supervise(guard.pytest_command(['-c', name, 'test_probe.py']), timeout=5, artifact_root=tmp_path / 'evidence')
    assert status == 0


@pytest.mark.parametrize('addopts', ['["--"]', '"--"', '"\\u002d\\u002d"'])
def test_toml_terminators_fail_closed(tmp_path, monkeypatch, addopts):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pyproject.toml').write_text('[tool.pytest.ini_options]\naddopts=' + addopts + '\n')
    with pytest.raises(guard.Refused): guard.pytest_command([])


def test_finalization_failure_stays_red_and_reports_raw_status(tmp_path, monkeypatch, capsys):
    original = guard.Evidence.save
    calls = []
    def save(subject):
        calls.append(1)
        if subject.data.get('ended') is not None: raise OSError('fixture storage failure')
        return original(subject)
    monkeypatch.setattr(guard.Evidence, 'save', save)
    status, path = guard.supervise([sys.executable, '-c', 'print("executed")'], artifact_root=tmp_path)
    assert status != 0 and 'primary raw status=0' in capsys.readouterr().err
    assert json.loads((path / 'manifest.json').read_text())['status'] == 'running'


def test_container_timeout_keeps_primary_and_partial_logs(tmp_path, monkeypatch):
    subject = wrapper()
    evidence = guard.Evidence(tmp_path)
    error = subprocess.TimeoutExpired('owned fixture wait', 1)
    def docker(*args, **kwargs):
        if args[0] == 'wait': raise error
        if args[0] == 'logs': return subprocess.CompletedProcess(args, 0, b'last milestone\n', b'partial stderr\n')
        return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'own', 'Config': {'Labels': {'ha-ops.dev.run': 'run'}}, 'State': {'ExitCode': 137, 'OOMKilled': False}}]).encode(), b'')
    monkeypatch.setattr(subject, 'docker', docker)
    with pytest.raises(subprocess.TimeoutExpired) as caught: subject.container_wait('own', 'run', evidence)
    assert caught.value is error
    evidence.finish(status='failed')
    manifest = json.loads((evidence.path / 'manifest.json').read_text())
    assert manifest['containers'][0]['state']['ExitCode'] == 137
    assert manifest['containers'][0]['docker_wait_returncode'] is None
    assert 'signal' not in manifest['containers'][0]
    assert 'last milestone' in (evidence.path / 'stdout.log').read_text()


def test_container_nested_evidence_survives_owned_output_cleanup(tmp_path):
    subject = wrapper()
    output = tmp_path / 'output'; output.mkdir()
    nested = guard.Evidence(output / 'test-diagnostics'); nested.feed('stderr', b'failure evidence\n'); nested.finish(status='failed')
    outer = guard.Evidence(tmp_path / 'durable')
    subject.persist_inner(output, outer)
    import shutil
    shutil.rmtree(output)
    outer.finish(status='failed')
    assert 'failure evidence' in (outer.path / 'inner' / nested.path.name / 'stderr.log').read_text()


def test_xdist_crash_is_red_without_worker_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'test_crash.py').write_text('import os,signal\ndef test_intentional_isolated_worker_death():\n with open("attempts", "a") as f: f.write("one\\n")\n os.kill(os.getpid(),signal.SIGKILL)\n')
    status, path = guard.supervise(guard.pytest_command(['-n', '2', 'test_crash.py']), timeout=10, artifact_root=tmp_path / 'evidence')
    assert status != 0 and (tmp_path / 'attempts').read_text() == 'one\n'
    assert 'failed' in (path / 'stdout.log').read_text()


# Proposed additions to ha-ops/tests/test_test_runner_guard.py.
# Reuse the existing guard import and run_child helper in that file.

def test_keyed_quoted_credential_is_redacted_whole(tmp_path, capsys):
    status, path, _ = run_child(tmp_path, 'print(\'{"password": "fixture alpha omega"}\')')
    captured = capsys.readouterr()
    retained = (path / 'stdout.log').read_text()
    assert status == 0
    assert 'alpha omega' not in retained + captured.out + captured.err
    assert '[REDACTED]' in retained


def test_truncated_output_preserves_last_observed_failure_milestone(tmp_path):
    status, path, manifest = run_child(tmp_path,
        'import sys; sys.stdout.write("earlier passing progress\\n" * 60000); '
        'print("LAST_TEST_MILESTONE failure_tail_omega"); sys.exit(73)')
    assert status == 73 and manifest['raw_returncode'] == 73
    assert manifest['streams']['stdout']['truncated']
    # An independently bounded tail/milestone field is also acceptable; adapt
    # this retrieval to the developer's chosen evidence format.
    retained = ''.join(p.read_text() for p in path.iterdir() if p.is_file())
    assert 'LAST_TEST_MILESTONE failure_tail_omega' in retained
    assert (path / 'stdout.log').stat().st_size <= guard.CAP


def test_authorization_and_quoted_password_values_are_fully_redacted(tmp_path, capsys):
    code = 'print("Authorization: Bearer sentinel_bearer_123");print("Authorization: Basic sentinel_basic_456");print(\'password="sentinel phrase 789"\')'
    status, path = guard.supervise([sys.executable, '-u', '-c', code], artifact_root=tmp_path)
    assert status == 0
    captured = capsys.readouterr()
    combined = (path / 'stdout.log').read_text() + captured.out + captured.err
    for secret in ('sentinel_bearer_123', 'sentinel_basic_456', 'phrase 789'):
        assert secret not in combined


def test_container_logs_watchdog_failure_cannot_become_success(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('failure_review_vm', ROOT / 'dev/virtualization/ha_ops_dev.py')
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    evidence = guard.Evidence(tmp_path)
    def docker(*args, **kwargs):
        if args[0] == 'wait':
            return subprocess.CompletedProcess(args, 0, b'0\n', b'')
        if args[0] == 'logs':
            result = subprocess.CompletedProcess(args, 0, b'partial logs\n', b'')
            result.watchdog_reason = 'timeout'
            result.evidence_path = '/fixture/log-evidence'
            return result
        return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'owned', 'Config': {'Labels': {'ha-ops.dev.run': 'run'}}, 'State': {'ExitCode': 0, 'OOMKilled': False}}]).encode(), b'')
    monkeypatch.setattr(subject, 'docker', docker)
    try:
        with pytest.raises(subject.Refused, match='capture incomplete'):
            subject.container_wait('owned', 'run', evidence)
    finally:
        evidence.finish(status='failed')


@pytest.mark.parametrize('initially_full', [False, True])
def test_slow_console_reader_does_not_disable_run_deadline(tmp_path, initially_full):
    child_pid = tmp_path / 'child.pid'
    child = 'import os,time;open(' + repr(str(child_pid)) + ',"w").write(str(os.getpid()));print("safe milestone\\n"*200000,flush=True);time.sleep(60)'
    outer = ('import importlib.util,sys; s=importlib.util.spec_from_file_location("g",' + repr(str(ROOT / 'dev/test_runner.py')) + ');g=importlib.util.module_from_spec(s);s.loader.exec_module(g);g.supervise([sys.executable,"-u","-c",' + repr(child) + '],timeout=.3,artifact_root=' + repr(str(tmp_path / 'evidence')) + ')')
    if initially_full:
        prefill = 'import os;os.set_blocking(1,False)\ntry:\n while True:os.write(1,b"x"*4096)\nexcept BlockingIOError:pass\n'
        outer = prefill + outer
    process = subprocess.Popen([sys.executable, '-u', '-c', outer], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, preexec_fn=guard.no_core)
    completed = False
    try:
        try:
            process.wait(timeout=3)
            completed = True
        except subprocess.TimeoutExpired:
            pass
    finally:
        # The fixture owns both independent groups. Never leave intentional hang children.
        if child_pid.exists():
            try:
                os.killpg(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=2)
    assert completed, 'console backpressure blocked supervisor beyond its .3s deadline'


@pytest.mark.parametrize('reason,supervisor_code', [('controller_error', 1), ('completed', 1)])
def test_wait_controller_failure_with_raw_zero_is_red(tmp_path, monkeypatch, reason, supervisor_code):
    subject = wrapper(); evidence = guard.Evidence(tmp_path)
    def docker(*args, **kwargs):
        if args[0] == 'wait':
            result = subprocess.CompletedProcess(args, 0, b'0\n', b'')
            result.watchdog_reason = reason; result.supervisor_exit_code = supervisor_code
            result.evidence_path = '/fixture/wait-evidence'
            return result
        if args[0] == 'logs': return subprocess.CompletedProcess(args, 0, b'', b'')
        return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'own', 'Config': {'Labels': {'ha-ops.dev.run': 'run'}}, 'State': {'ExitCode': 0}}]).encode(), b'')
    monkeypatch.setattr(subject, 'docker', docker)
    with pytest.raises(subject.Refused): subject.container_wait('own', 'run', evidence)
    evidence.finish(status='failed')
    entry = json.loads((evidence.path / 'manifest.json').read_text())['containers'][0]
    assert entry['docker_wait_returncode'] == 0 and entry['supervisor_exit_code'] == 1
    assert entry['wait_evidence_path'] == '/fixture/wait-evidence'


def test_healthy_console_continues_after_prefix_cap(tmp_path, capsys):
    status, path, _ = run_child(tmp_path, 'print("progress\\n"*200000); print("AFTER_PREFIX_CAP")')
    captured = capsys.readouterr()
    assert status == 0 and 'AFTER_PREFIX_CAP' in captured.out
    assert 'AFTER_PREFIX_CAP' in (path / 'stdout.tail.log').read_text()


# Proposed additions to ha-ops/tests/test_test_runner_guard.py.
# Reuse guard/run_child and existing pytest/json imports.

@pytest.mark.parametrize('scheme', ['http', 'HTTP', 'https', 'hTtPs', 'HTTPS'])
def test_http_url_credentials_redacted_for_all_scheme_cases(tmp_path, capsys, scheme):
    value = scheme + '://fixture:scheme-password@fixture.invalid/path'
    status, path, _ = run_child(tmp_path, 'print(' + repr(value) + ')')
    captured = capsys.readouterr()
    retained = ''.join(p.read_text() for p in path.iterdir() if p.is_file())
    assert status == 0
    assert 'scheme-password' not in retained + captured.out + captured.err


def test_quoted_keyed_password_spanning_lines_is_fully_redacted(tmp_path, capsys):
    value = 'password="fixture alpha\nomega"'
    status, path, _ = run_child(tmp_path, 'print(' + repr(value) + ')')
    captured = capsys.readouterr()
    retained = ''.join(p.read_text() for p in path.iterdir() if p.is_file())
    assert status == 0
    combined = retained + captured.out + captured.err
    assert 'fixture alpha' not in combined and 'omega' not in combined


def test_pretty_json_key_and_value_on_separate_lines_are_redacted(tmp_path, capsys):
    value = '{"password":\n "sentinel_multiline_123"}'
    assert json.loads(value)['password'] == 'sentinel_multiline_123'
    code = 'print(' + repr(value) + ')'
    status, path = guard.supervise([sys.executable, '-u', '-c', code], artifact_root=tmp_path)
    captured = capsys.readouterr()
    assert status == 0
    combined = captured.out + captured.err + ''.join(p.read_text() for p in path.iterdir())
    assert 'sentinel_multiline_123' not in combined


def test_stream_open_failure_restores_inherited_console_flags(tmp_path):
    code = ('import importlib.util,sys; s=importlib.util.spec_from_file_location("g",' + repr(str(ROOT / 'dev/test_runner.py')) + ');g=importlib.util.module_from_spec(s);s.loader.exec_module(g)\n'
            'original=g.os.open\n'
            'def fail_stderr(path,*args,**kwargs):\n'
            ' if str(path).endswith("stderr.log"):raise OSError("fixture storage error")\n'
            ' return original(path,*args,**kwargs)\n'
            'g.os.open=fail_stderr\n'
            'try:g.Evidence(sys.argv[1])\n'
            'except OSError:pass\n')
    read_out, write_out = os.pipe()
    read_err, write_err = os.pipe()
    flags = {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in (write_out, write_err)}
    try:
        process = subprocess.Popen([sys.executable, '-u', '-c', code, str(tmp_path / 'evidence')], stdout=write_out, stderr=write_err)
        process.wait(timeout=3)
        assert process.returncode == 0
        assert {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in flags} == flags
    finally:
        # The test alone owns these descriptors; restore flags before closing.
        for fd, original in flags.items():
            fcntl.fcntl(fd, fcntl.F_SETFL, original)
        for fd in (read_out, write_out, read_err, write_err):
            os.close(fd)


@pytest.mark.parametrize('chunks', [
    [b'{"password":\n', b'\n', b'"sentinel late value"}\n'],
    [b'password="sentinel first\n', b'\n', b'last part"\n'],
    [b'password="sentinel escaped \\" quote\n', b'last part"\n'],
    [b'pass', b'word', b'"\n', b':\n', b'"sentinel delayed"\n'],
    [b'password="sentinel unfinished'],
    [b'password: >\n', b'sentinel block value\n'],
    [b'password: [\n', b'"sentinel array value"]\n'],
    [b'password: {\n', b'"nested": {"value": "sentinel nested value"}}\n'],
    [b'password="' + b'x' * 20000, b'\nsentinel after oversize\n'],
])
def test_stream_state_masks_credential_regions_before_every_sink(tmp_path, capsys, chunks):
    evidence = guard.Evidence(tmp_path)
    for chunk in chunks: evidence.feed('stdout', chunk)
    evidence.feed('stderr', b'healthy independent progress\n')
    evidence.finish(status='passed')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(path.read_text() for path in evidence.path.iterdir())
    assert 'sentinel' not in combined and 'last part' not in combined
    assert 'healthy independent progress' in (evidence.path / 'stderr.log').read_text()
    assert len(evidence.streams['stdout']['pending']) <= guard.LINE_CAP


@pytest.mark.parametrize('boundary', ['stdout.log', 'stderr.log', 'manifest'])
def test_initialization_failure_restores_flags_and_partial_handles(tmp_path, monkeypatch, boundary):
    original_open, original_fdopen = guard.os.open, guard.os.fdopen
    handles = []
    actual_console = guard.Console
    consoles = []
    def console():
        sink = actual_console(); consoles.append(sink); return sink
    def opening(path, *args, **kwargs):
        if str(path).endswith(boundary): raise OSError('original fixture failure')
        return original_open(path, *args, **kwargs)
    def fdopen(*args, **kwargs):
        handle = original_fdopen(*args, **kwargs); handles.append(handle); return handle
    monkeypatch.setattr(guard, 'Console', console)
    monkeypatch.setattr(guard.os, 'fdopen', fdopen)
    if boundary == 'manifest':
        monkeypatch.setattr(guard.Evidence, 'save', lambda self: (_ for _ in ()).throw(OSError('original fixture failure')))
    else:
        monkeypatch.setattr(guard.os, 'open', opening)
    with pytest.raises(OSError, match='original fixture failure'): guard.Evidence(tmp_path)
    assert consoles and all(not sink.flags for sink in consoles)
    assert all(handle.closed for handle in handles)


# Proposed additions to ha-ops/tests/test_test_runner_guard.py.
# Reuse existing json, pytest, guard and run_child imports/helpers.

@pytest.mark.parametrize('scheme', ['Bearer', 'Basic'])
@pytest.mark.parametrize('key', ['password', 'token'])
def test_nested_scheme_redaction_preserves_raw_quoted_escape_state(tmp_path, capsys, scheme, key):
    value = json.dumps({key: scheme + ' fixture"hidden-part'})
    assert json.loads(value)[key].endswith('"hidden-part')
    status, path, _ = run_child(tmp_path, 'print(' + repr(value) + ')')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(p.read_text() for p in path.iterdir() if p.is_file())
    assert status == 0
    assert 'fixture' not in combined and 'hidden-part' not in combined


def test_initial_manifest_fdopen_failure_closes_raw_descriptor(tmp_path, monkeypatch):
    original_open, original_fdopen = guard.os.open, guard.os.fdopen
    manifest_fds = []
    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if Path(path).name.startswith('manifest-'):
            manifest_fds.append(fd)
        return fd
    def wrapping(fd, *args, **kwargs):
        if fd in manifest_fds:
            raise OSError('original manifest fdopen failure')
        return original_fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(guard.os, 'open', opening)
    monkeypatch.setattr(guard.os, 'fdopen', wrapping)
    leaked = []
    try:
        with pytest.raises(OSError, match='original manifest fdopen failure'):
            guard.Evidence(tmp_path)
        assert manifest_fds
        for fd in manifest_fds:
            try:
                os.fstat(fd)
            except OSError:
                continue
            leaked.append(fd)
        assert not leaked, 'initial manifest raw descriptor survives failed fdopen'
    finally:
        for fd in leaked:
            os.close(fd)


@pytest.mark.parametrize('secret_value', [
    'Bearer fixture"hidden-part\\tail',
    'Basic fixture\\\\"hidden-part',
    'HTTPS://user:password-token@fixture.invalid/path"hidden-part',
    'Bearer HTTPS://user:password-token@fixture.invalid/path\\"hidden-part',
])
@pytest.mark.parametrize('key', ['password', 'token', 'authorization'])
def test_nested_raw_quote_and_url_regions_cannot_expose_keyed_suffix(tmp_path, capsys, key, secret_value):
    value = json.dumps({key: secret_value})
    evidence = guard.Evidence(tmp_path)
    for byte in (value + '\n').encode(): evidence.feed('stdout', bytes([byte]))
    evidence.feed('stderr', b'healthy adjacent progress\n')
    evidence.finish(status='passed')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(path.read_text() for path in evidence.path.iterdir())
    assert 'fixture.invalid' not in combined and 'hidden-part' not in combined
    assert 'healthy adjacent progress' in (evidence.path / 'stderr.log').read_text()


@pytest.mark.parametrize('userinfo', ['password-user:password-token', ':password-token', 'user:password-token@nested'])
def test_url_region_precedes_nested_key_words_and_redacts_query_values(tmp_path, capsys, userinfo):
    evidence = guard.Evidence(tmp_path)
    evidence.feed('stdout', ('HTTPS://' + userinfo + '@fixture.invalid/path?token=query-sentinel\n').encode())
    evidence.finish(status='passed')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(path.read_text() for path in evidence.path.iterdir())
    assert 'password-token' not in combined and 'query-sentinel' not in combined


@pytest.mark.parametrize('boundary', ['stdout.log', 'stderr.log', 'manifest-initial', 'stdout.tail.log', 'stderr.tail.log', 'manifest-final'])
def test_all_fdopen_acquisitions_release_owned_raw_descriptor(tmp_path, monkeypatch, boundary):
    original_open, original_fdopen = guard.os.open, guard.os.fdopen
    observed, evidence = [], None
    final = boundary.endswith('.tail.log') or boundary == 'manifest-final'
    if final: evidence = guard.Evidence(tmp_path)
    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        match = Path(path).name.startswith('manifest-') if boundary.startswith('manifest-') else Path(path).name == boundary
        if match: observed.append(fd)
        return fd
    def fdopen(fd, *args, **kwargs):
        if fd in observed: raise OSError('original owned descriptor wrapping error')
        return original_fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(guard.os, 'open', opening); monkeypatch.setattr(guard.os, 'fdopen', fdopen)
    with pytest.raises(OSError, match='original owned descriptor wrapping error'):
        if final: evidence.finish(status='passed')
        else: guard.Evidence(tmp_path)
    assert observed
    for fd in observed:
        with pytest.raises(OSError): os.fstat(fd)
    if evidence is not None:
        assert not evidence.sink.flags
        assert all(stream['file'].closed for stream in evidence.streams.values())


# Proposed additions to ha-ops/tests/test_test_runner_guard.py.
# Reuse existing guard/run_child/pytest helpers.

@pytest.mark.parametrize('key', ['password', 'token', 'api_key'])
def test_raw_url_region_does_not_exempt_recognized_fragment_credentials(tmp_path, capsys, key):
    value = 'https://fixture.invalid/callback#' + key + '=fragment-sentinel'
    status, path, _ = run_child(tmp_path, 'print(' + repr(value) + ')')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(p.read_text() for p in path.iterdir() if p.is_file())
    assert status == 0
    assert 'fragment-sentinel' not in combined


def test_url_fragment_does_not_bypass_recognized_key_redaction(tmp_path, capsys):
    value = 'https://fixture.invalid/#token=sentinel_fragment_123'
    assert urllib.parse.urlsplit(value).fragment == 'token=sentinel_fragment_123'
    evidence = guard.Evidence(tmp_path)
    evidence.feed('stdout', (value + '\n').encode())
    evidence.finish(status='passed')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(p.read_text() for p in evidence.path.iterdir())
    assert 'sentinel_fragment_123' not in combined


def test_nested_copy_fdopen_failure_closes_owned_raw_descriptor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('failure_r3_wrapper', ROOT / 'dev/virtualization/ha_ops_dev.py')
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    output = tmp_path / 'output'
    output.mkdir()
    inner = guard.Evidence(output / 'test-diagnostics')
    inner.finish(status='failed')
    outer = guard.Evidence(tmp_path / 'durable')
    original_open, original_fdopen = subject.os.open, subject.os.fdopen
    owned, leaked = [], []
    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(str(outer.path / 'inner')):
            owned.append(fd)
        return fd
    def wrapping(fd, *args, **kwargs):
        if fd in owned:
            raise OSError('original nested evidence wrapping failure')
        return original_fdopen(fd, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subject.os, 'open', opening)
            patch.setattr(subject.os, 'fdopen', wrapping)
            with pytest.raises(OSError, match='original nested evidence wrapping failure'):
                subject.persist_inner(output, outer)
            assert owned
            for fd in owned:
                try:
                    os.fstat(fd)
                except OSError:
                    continue
                leaked.append(fd)
        assert not leaked, 'nested evidence manifest raw descriptor survives fdopen failure'
    finally:
        for fd in leaked:
            os.close(fd)
        outer.finish(status='failed')


@pytest.mark.parametrize('key', ['password', 'passwd', 'token', 'secret', 'api_key', 'api-key', 'authorization'])
@pytest.mark.parametrize('location', ['query', 'fragment', 'path', 'userinfo'])
@pytest.mark.parametrize('separator', ['=', ':'])
def test_entire_raw_url_credential_regions_are_omitted(tmp_path, capsys, key, location, separator):
    field = key.upper() + separator + 'location-sentinel'
    urls = {
        'query': 'hTtPs://fixture.invalid/callback?' + field + '&safe=tail-sentinel',
        'fragment': 'HTTPS://fixture.invalid/callback#' + field + '&nested=tail-sentinel',
        'path': 'http://fixture.invalid/' + field + '/nested-tail-sentinel',
        'userinfo': 'https://fixture:' + field + '@fixture.invalid/path',
    }
    value = urls[location]
    evidence = guard.Evidence(tmp_path, {'safe_url': guard.redact(value)})
    for byte in ('BEFORE_PROGRESS ' + value + ' AFTER_PROGRESS https://fixture.invalid/guide?view=plain\n').encode():
        evidence.feed('stdout', bytes([byte]))
    evidence.finish(status='passed')
    captured = capsys.readouterr()
    combined = captured.out + captured.err + ''.join(path.read_text() for path in evidence.path.iterdir())
    assert 'location-sentinel' not in combined and 'tail-sentinel' not in combined
    assert '[REDACTED URL]' in combined
    assert 'BEFORE_PROGRESS' in (evidence.path / 'stdout.log').read_text()
    assert 'AFTER_PROGRESS https://fixture.invalid/guide?view=plain' in (evidence.path / 'stdout.log').read_text()


@pytest.mark.parametrize('copied_name', ['manifest.json', 'stdout.log', 'stderr.log', 'stdout.tail.log', 'stderr.tail.log'])
def test_every_nested_copy_fdopen_failure_closes_owned_descriptor(tmp_path, monkeypatch, copied_name):
    subject = wrapper()
    output = tmp_path / 'output'; output.mkdir()
    inner = guard.Evidence(output / 'test-diagnostics'); inner.finish(status='failed')
    outer = guard.Evidence(tmp_path / 'durable')
    original_open, original_fdopen = subject.os.open, subject.os.fdopen
    failed_fds = []
    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(str(outer.path / 'inner')) and Path(path).name == copied_name:
            failed_fds.append(fd)
        return fd
    def wrapping(fd, *args, **kwargs):
        if fd in failed_fds: raise OSError('original copied-file wrapping failure')
        return original_fdopen(fd, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subject.os, 'open', opening); patch.setattr(subject.os, 'fdopen', wrapping)
            with pytest.raises(OSError, match='original copied-file wrapping failure'):
                subject.persist_inner(output, outer)
            assert failed_fds
            for fd in failed_fds:
                with pytest.raises(OSError): os.fstat(fd)
    finally:
        outer.finish(status='failed')


@pytest.mark.parametrize('route', ['generic', 'ext4'])
def test_both_wrapper_routes_preserve_primary_and_record_nested_copy_debt(tmp_path, monkeypatch, route):
    subject = wrapper()
    monkeypatch.setattr(subject, 'ROOT', tmp_path)
    monkeypatch.setattr(subject, 'RUNS', tmp_path / 'runs')
    monkeypatch.setattr(subject, 'LOCAL', tmp_path / 'local')
    monkeypatch.setattr(subject, 'DIAGNOSTICS', tmp_path / 'durable')
    monkeypatch.setattr(subject, 'capacity', lambda *args: 10**12)
    monkeypatch.setattr(subject, 'snapshot_input', lambda path: {'total': 0})
    monkeypatch.setattr(subject, 'copy_input', lambda source, destination, **kwargs: destination.mkdir())
    monkeypatch.setattr(subject, 'remove_owned', lambda *args: None)
    monkeypatch.setattr(subject.os, 'getuid', lambda: 1000)
    def seed_output(output):
        if not (output / 'test-diagnostics').exists():
            inner = guard.Evidence(output / 'test-diagnostics'); inner.finish(status='failed')
    def setup(**kwargs):
        seed_output(kwargs['exact_sources'][1])
        return {'image': 'fixture-image', 'platform': 'linux/arm64'}
    monkeypatch.setattr(subject, 'setup', setup)
    def unavailable_tool(name):
        seed_output(next(subject.RUNS.iterdir()) / 'output')
        return None
    monkeypatch.setattr(subject.shutil, 'which', unavailable_tool)
    def refuse_docker(*args, **kwargs):
        raise subject.Refused('original create refusal')
    monkeypatch.setattr(subject, 'docker', refuse_docker)
    original_open, original_fdopen = subject.os.open, subject.os.fdopen
    owned = []
    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(str(subject.DIAGNOSTICS)) and 'inner' in Path(path).parts:
            owned.append(fd)
        return fd
    def wrapping(fd, *args, **kwargs):
        if fd in owned: raise OSError('original nested copy wrapping failure')
        return original_fdopen(fd, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(subject.os, 'open', opening); patch.setattr(subject.os, 'fdopen', wrapping)
        with pytest.raises(subject.Refused) as caught:
            if route == 'generic': subject.generic_run(Path('.'), ['true'])
            else: subject.ext4_run(Path('.'), ['true'])
        assert owned
        for fd in owned:
            with pytest.raises(OSError): os.fstat(fd)
    manifest = json.loads(next(subject.DIAGNOSTICS.glob('run-*/manifest.json')).read_text())
    assert manifest['status'] == 'failed'
    assert 'inner evidence' in str(manifest['cleanup_errors'])
    assert len(list(subject.RUNS.iterdir())) == 1
    if route == 'generic':
        assert str(caught.value) == 'original create refusal'
        assert manifest['primary_error'] == 'original create refusal'
    else:
        assert 'Ext4 requires existing tools' in str(caught.value.__cause__)


# Proposed addition to ha-ops/tests/test_test_runner_guard.py.
def test_cleanup_signal_failure_still_finalizes_red_evidence(tmp_path, monkeypatch):
    captured = []
    actual_evidence = guard.Evidence
    def evidence(*args, **kwargs):
        value = actual_evidence(*args, **kwargs)
        captured.append(value)
        return value
    actual_signal = guard.signal_group
    def denied_kill(process, number):
        if number == signal.SIGKILL:
            raise PermissionError("fixture cleanup signal refused")
        return actual_signal(process, number)
    monkeypatch.setattr(guard, "Evidence", evidence)
    monkeypatch.setattr(guard, "signal_group", denied_kill)
    try:
        # The fixture child exits on its own; simulated cleanup refusal must
        # not suppress final status, handle cleanup or descriptor restoration.
        status, path = guard.supervise([sys.executable, "-u", "-c", "print('completed fixture')"],
                                        timeout=3, artifact_root=tmp_path)
        assert status != 0
        manifest = json.loads((path / "manifest.json").read_text())
        assert manifest["status"] == "failed"
        assert "PermissionError" in str(manifest)
        assert all(stream["file"].closed for stream in captured[0].streams.values())
        assert not captured[0].sink.flags
    finally:
        # Keep this failing proposal resource-safe on the unfixed source.
        for value in captured:
            for stream in value.streams.values():
                stream["file"].close()
            value.sink.close()


@pytest.mark.parametrize('stage', ['TERM', 'KILL', 'probe', 'reap', 'pipe-close', 'selector-close'])
def test_cleanup_faults_preserve_primary_and_finalize_independently(tmp_path, monkeypatch, stage):
    captured, children = [], []
    real_evidence, real_popen = guard.Evidence, guard.subprocess.Popen
    real_signal, real_killpg = guard.signal_group, guard.os.killpg
    real_selector = guard.selectors.DefaultSelector
    def evidence(*args, **kwargs):
        value = real_evidence(*args, **kwargs); captured.append(value); return value
    class Pipe:
        def __init__(self, value): self.value, self.denied = value, False
        def fileno(self): return self.value.fileno()
        def close(self):
            self.value.close()
            if not self.denied:
                self.denied = True
                raise PermissionError('fixture pipe close refusal')
    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs); children.append(child)
        if stage == 'reap':
            child.real_wait = child.wait
            child.wait = lambda **kwargs: (_ for _ in ()).throw(PermissionError('fixture reap refusal'))
        if stage == 'pipe-close':
            child.stdout, child.stderr = Pipe(child.stdout), Pipe(child.stderr)
        return child
    def signaling(process, number):
        if (stage == 'TERM' and number == signal.SIGTERM) or (stage == 'KILL' and number == signal.SIGKILL):
            raise PermissionError('fixture owned signal refusal')
        return real_signal(process, number)
    def probing(pid, number):
        if stage == 'probe' and number == 0: raise PermissionError('fixture probe refusal')
        return real_killpg(pid, number)
    def selecting():
        value = real_selector()
        if stage == 'selector-close':
            close = value.close
            def denied():
                close(); raise PermissionError('fixture selector close refusal')
            value.close = denied
        return value
    monkeypatch.setattr(guard, 'Evidence', evidence); monkeypatch.setattr(guard.subprocess, 'Popen', popen)
    monkeypatch.setattr(guard, 'signal_group', signaling); monkeypatch.setattr(guard.os, 'killpg', probing)
    monkeypatch.setattr(guard.selectors, 'DefaultSelector', selecting)
    timed = stage == 'TERM'
    try:
        start = time.monotonic()
        code, path = guard.supervise([sys.executable, '-u', '-c', 'import time; print("owned milestone"); time.sleep(.15)'], timeout=.03 if timed else 3, artifact_root=tmp_path)
        assert time.monotonic() - start < 4
        manifest = json.loads((path / 'manifest.json').read_text())
        assert code != 0 and manifest['status'] == 'failed'
        assert manifest['reason'] == ('timeout' if timed else 'completed')
        assert manifest['raw_returncode'] == 0
        assert 'PermissionError' in str(manifest['errors'])
        assert manifest['child_pid'] == manifest['owned_pgid'] == children[0].pid
        if stage in ('TERM', 'KILL', 'probe', 'reap'):
            assert manifest['process_group_cleanup'] == 'incomplete or unknown'
        assert all(stream['file'].closed for stream in captured[0].streams.values())
        assert not captured[0].sink.flags
    finally:
        for child in children:
            if hasattr(child, 'real_wait'): child.real_wait(timeout=2)


@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
def test_primary_controller_reason_survives_failed_final_signal(tmp_path, monkeypatch, failure):
    real_selector, real_signal = guard.selectors.DefaultSelector, guard.signal_group
    def selector():
        value = real_selector()
        value.select = lambda *args: (_ for _ in ()).throw(failure('fixture primary controller failure'))
        return value
    def signal_then_refuse(process, number):
        real_signal(process, number)
        raise PermissionError('fixture final signal acknowledgement refusal')
    monkeypatch.setattr(guard.selectors, 'DefaultSelector', selector)
    monkeypatch.setattr(guard, 'signal_group', signal_then_refuse)
    code, path = guard.supervise([sys.executable, '-c', 'import time;time.sleep(1)'], artifact_root=tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text())
    assert code != 0 and manifest['status'] == 'failed'
    assert manifest['reason'] == ('interrupted' if failure is KeyboardInterrupt else 'controller_error')
    assert manifest['raw_returncode'] == -signal.SIGKILL
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'
    assert 'PermissionError' in str(manifest['errors'])



def test_final_drain_refusal_preserves_controller_reason_and_closes_handles(tmp_path, monkeypatch):
    captured, final = [], []
    real_evidence, real_popen, real_selector, real_read = guard.Evidence, guard.subprocess.Popen, guard.selectors.DefaultSelector, guard.os.read
    def evidence(*args, **kwargs):
        value = real_evidence(*args, **kwargs); captured.append(value); return value
    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs); waiting = child.wait
        def wait(**kwargs):
            result = waiting(**kwargs); final.append(True); return result
        child.wait = wait
        return child
    def selector():
        value = real_selector()
        value.select = lambda *args: (_ for _ in ()).throw(RuntimeError('original controller refusal'))
        return value
    def reading(*args):
        if final: raise PermissionError('fixture final drain refusal')
        return real_read(*args)
    monkeypatch.setattr(guard, 'Evidence', evidence); monkeypatch.setattr(guard.subprocess, 'Popen', popen)
    monkeypatch.setattr(guard.selectors, 'DefaultSelector', selector); monkeypatch.setattr(guard.os, 'read', reading)
    code, path = guard.supervise([sys.executable, '-c', 'import time;time.sleep(1)'], artifact_root=tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text())
    assert code != 0 and manifest['reason'] == 'controller_error'
    assert manifest['raw_returncode'] == -signal.SIGKILL
    assert any('final pipe drain' in error and 'PermissionError' in error for error in manifest['errors'])
    assert all(stream['file'].closed for stream in captured[0].streams.values())
    assert not captured[0].sink.flags


def test_console_restore_failure_is_observable_and_retains_unknown_flags(monkeypatch):
    read_fd, write_fd = os.pipe()
    sink = guard.Console()
    original_flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
    fcntl.fcntl(write_fd, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
    sink.flags[write_fd] = original_flags
    original_fcntl = guard.fcntl.fcntl
    def refused(fd, operation, *args):
        if fd == write_fd and operation == fcntl.F_SETFL: raise PermissionError('fixture flag restore refusal')
        return original_fcntl(fd, operation, *args)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(guard.fcntl, 'fcntl', refused)
            with pytest.raises(OSError, match='restoration incomplete'): sink.close()
            assert write_fd in sink.flags
        sink.close()
        assert not sink.flags and fcntl.fcntl(write_fd, fcntl.F_GETFL) == original_flags
    finally:
        fcntl.fcntl(write_fd, fcntl.F_SETFL, original_flags)
        os.close(read_fd); os.close(write_fd)



def test_nonzero_child_status_remains_primary_after_cleanup_refusal(tmp_path, monkeypatch):
    actual = guard.signal_group
    def refused(process, number):
        actual(process, number)
        raise PermissionError('fixture cleanup acknowledgement failure')
    monkeypatch.setattr(guard, 'signal_group', refused)
    code, path = guard.supervise([sys.executable, '-c', 'import sys;sys.exit(73)'], artifact_root=tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text())
    assert code == 73 and manifest['raw_returncode'] == 73
    assert manifest['status'] == 'failed' and manifest['reason'] == 'completed'
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'


@pytest.mark.parametrize('terminal', [0, 'ECHILD', 'child-error'])
def test_owned_child_reaper_selects_only_exact_group(tmp_path, monkeypatch, terminal):
    calls = []
    def waiting(selector, flags):
        calls.append((selector, flags))
        if len(calls) == 1: return 456, 73 << 8
        if terminal == 'ECHILD': raise OSError(errno.ECHILD, 'fixture no children')
        if terminal == 'child-error': raise ChildProcessError('fixture no children')
        return 0, 0
    monkeypatch.setattr(guard.os, 'waitpid', waiting)
    guard.OwnedChildReaper().drain(123)
    assert calls == [(-123, os.WNOHANG)] * 2


@pytest.mark.parametrize('pgid', [-123, -1, 0, 1])
def test_owned_child_reaper_refuses_unrestricted_selectors(monkeypatch, pgid):
    monkeypatch.setattr(guard.os, 'waitpid', lambda *args: pytest.fail('unowned wait'))
    with pytest.raises(ValueError): guard.OwnedChildReaper().drain(pgid)


@pytest.mark.parametrize('children,exhausted', [(0, False), (2, False), (3, True)])
def test_owned_child_reap_syscall_budget_is_shared(monkeypatch, children, exhausted):
    monkeypatch.setattr(guard, 'MAX_OWNED_REAP_CALLS', 3)
    calls = []
    def waiting(*args):
        calls.append(args)
        return (456, 0) if len(calls) <= children else (0, 0)
    monkeypatch.setattr(guard.os, 'waitpid', waiting)
    reaper = guard.OwnedChildReaper()
    if exhausted:
        with pytest.raises(RuntimeError, match='syscall limit'): reaper.drain(123)
    else:
        reaper.drain(123)
    assert len(calls) <= 3
    # A second phase cannot renew the invocation's finite syscall allowance.
    reaper.remaining = 0
    with pytest.raises(RuntimeError, match='syscall limit'): reaper.drain(123)
    assert len(calls) <= 3


def test_owned_child_reap_elapsed_budget_is_not_renewed(monkeypatch):
    ticks = iter([10, 10, 11, 12])
    monkeypatch.setattr(guard.time, 'monotonic', lambda: next(ticks))
    calls = []
    monkeypatch.setattr(guard.os, 'waitpid', lambda *args: (calls.append(args) or (456, 0)))
    reaper = guard.OwnedChildReaper()
    with pytest.raises(RuntimeError, match='deadline'): reaper.drain(123)
    with pytest.raises(RuntimeError, match='deadline'): reaper.drain(123)
    assert len(calls) == 1


@pytest.mark.parametrize('phase', ['post-leader', 'final'])
@pytest.mark.parametrize('failure', [PermissionError, InterruptedError, RuntimeError])
@pytest.mark.parametrize('raw', [0, 73])
def test_owned_child_reap_fault_keeps_raw_and_finalizes(tmp_path, monkeypatch, phase, failure, raw):
    real_popen, real_evidence = guard.subprocess.Popen, guard.Evidence
    children, captured = [], []
    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs); children.append(child); return child
    def evidence(*args, **kwargs):
        value = real_evidence(*args, **kwargs); captured.append(value); return value
    drains, observed_statuses = [], []
    def drain(self, pgid):
        observed_statuses.append((children[0].returncode, pgid))
        drains.append(pgid)
        if phase == 'post-leader' or len(drains) == 2:
            raise failure('fixture adopted child reap refusal')
    monkeypatch.setattr(guard.subprocess, 'Popen', popen)
    monkeypatch.setattr(guard, 'Evidence', evidence)
    monkeypatch.setattr(guard.OwnedChildReaper, 'drain', drain)
    status, path = guard.supervise([sys.executable, '-c', 'import sys;sys.exit(' + str(raw) + ')'], artifact_root=tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text())
    assert status == (raw or 1) and manifest['raw_returncode'] == raw
    assert manifest['reason'] == 'completed' and manifest['status'] == 'failed'
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'
    assert any(phase + ' owned child reap' in error for error in manifest['errors'])
    assert observed_statuses == [(raw, children[0].pid)] * 2
    assert all(stream['file'].closed for stream in captured[0].streams.values())
    assert not captured[0].sink.flags


def test_failed_leader_wait_never_reaps_adopted_children(tmp_path, monkeypatch):
    real_popen, real_selector = guard.subprocess.Popen, guard.selectors.DefaultSelector
    children, drains = [], []
    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs); children.append(child)
        child.real_wait = child.wait
        child.wait = lambda **kwargs: (_ for _ in ()).throw(PermissionError('fixture leader wait refusal'))
        return child
    def selector():
        value = real_selector()
        value.select = lambda *args: (_ for _ in ()).throw(RuntimeError('fixture controller failure'))
        return value
    monkeypatch.setattr(guard.subprocess, 'Popen', popen)
    monkeypatch.setattr(guard.selectors, 'DefaultSelector', selector)
    monkeypatch.setattr(guard.OwnedChildReaper, 'drain', lambda *args: drains.append(args))
    try:
        status, path = guard.supervise([sys.executable, '-c', 'import time;time.sleep(10)'], artifact_root=tmp_path)
        manifest = json.loads((path / 'manifest.json').read_text())
        assert status != 0 and manifest['raw_returncode'] is None
        assert manifest['reason'] == 'controller_error'
        assert not drains
    finally:
        for child in children: child.real_wait(timeout=2)


@pytest.mark.parametrize('primary', ['timeout', 'interrupted', 'controller_error'])
def test_final_owned_reap_failure_preserves_primary_reason(tmp_path, monkeypatch, primary):
    if primary != 'timeout':
        real_selector = guard.selectors.DefaultSelector
        def selector():
            value = real_selector()
            error = KeyboardInterrupt if primary == 'interrupted' else RuntimeError
            value.select = lambda *args: (_ for _ in ()).throw(error('fixture controller failure'))
            return value
        monkeypatch.setattr(guard.selectors, 'DefaultSelector', selector)
    monkeypatch.setattr(guard.OwnedChildReaper, 'drain', lambda *args: (_ for _ in ()).throw(PermissionError('fixture final adopted child failure')))
    status, path = guard.supervise([sys.executable, '-c', 'import time;time.sleep(10)'],
                                   timeout=.05 if primary == 'timeout' else 3, artifact_root=tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text())
    assert status == {'timeout': 124, 'interrupted': 130, 'controller_error': 137}[primary]
    assert manifest['reason'] == primary and manifest['raw_returncode'] < 0
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'
    assert any('final owned child reap' in error for error in manifest['errors'])


def test_owned_reap_exhaustion_stays_red_with_bounded_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, 'MAX_OWNED_REAP_CALLS', 0)
    status, path, manifest = run_child(tmp_path, 'print("successful leader")')
    assert status == 1 and manifest['raw_returncode'] == 0
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'
    assert len(manifest['errors']) == 2
    assert all('syscall limit' in error for error in manifest['errors'])


def test_final_owned_group_persistence_remains_unknown_and_red(tmp_path, monkeypatch):
    real_probe, probes = guard.os.killpg, []
    def probing(pgid, number):
        if number == 0:
            probes.append(pgid)
            if len(probes) == 2: return  # Fixture models late adoption/death.
        return real_probe(pgid, number)
    monkeypatch.setattr(guard.os, 'killpg', probing)
    status, path, manifest = run_child(tmp_path, 'print("successful leader")')
    assert status == 1 and manifest['raw_returncode'] == 0
    assert manifest['process_group_cleanup'] == 'incomplete or unknown'
    assert 'Owned process group remained after final cleanup' in manifest['errors']
    assert probes == [manifest['owned_pgid']] * 2


def _proc_identity(pid):
    fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
    return {'state': fields[0], 'ppid': int(fields[1]), 'pgid': int(fields[2])}


def owned_zombie_driver(directory, *, require_pid1):
    """Disposable Linux init/subreaper proof; never enable subreaping in pytest."""
    assert sys.platform == 'linux'
    if require_pid1:
        assert os.getpid() == 1, 'real PID1 qualification requires a container init invocation'
    else:
        import ctypes
        assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    original_drain = guard.OwnedChildReaper.drain
    observations, results = [], []
    foreign = os.fork()
    if foreign == 0:
        os.setsid(); os._exit(37)
    owned = []
    try:
        for mode in ('ordinary', 'zombie', 'live'):
            pidfile = directory / (mode + '.pid')
            child_code = ("import os,time,pathlib\n"
                          "pid=os.fork()\n"
                          "if pid==0:\n"
                          " os.close(1);os.close(2)\n"
                          + (" os._exit(23)\n" if mode == 'zombie' else " time.sleep(60);os._exit(0)\n") +
                          "pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(pid))\n"
                          + ("while pathlib.Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[0]!='Z':time.sleep(.001)\n" if mode == 'zombie' else '') +
                          "os._exit(0)\n")
            seen = []
            def drain(self, pgid):
                if mode != 'ordinary' and not seen:
                    pid = int(pidfile.read_text()); owned.append((pgid, pid))
                    identity = _proc_identity(pid)
                    assert identity['ppid'] == os.getpid() and identity['pgid'] == pgid
                    assert (identity['state'] == 'Z') == (mode == 'zombie')
                    seen.append(identity); observations.append({'mode': mode, **identity})
                return original_drain(self, pgid)
            guard.OwnedChildReaper.drain = drain
            code = 'print("ordinary control")' if mode == 'ordinary' else child_code
            status, path = guard.supervise([sys.executable, '-u', '-c', code], timeout=3,
                                           artifact_root=directory / 'evidence', metadata={'_console': False})
            manifest = json.loads((path / 'manifest.json').read_text())
            results.append({'mode': mode, 'status': status, 'manifest': str(path / 'manifest.json')})
            assert manifest['raw_returncode'] == 0
            assert status == 0 if mode != 'live' else status != 0
            if mode == 'zombie':
                assert seen and not Path('/proc/' + pidfile.read_text()).exists()
                with pytest.raises(ProcessLookupError): os.killpg(manifest['owned_pgid'], 0)
            if mode == 'live':
                assert 'Owned descendants survived the leader' in manifest['errors']
        assert os.waitpid(foreign, 0) == (foreign, 37 << 8)
        foreign = None
        report = {'pid': os.getpid(), 'pid1': require_pid1, 'observations': observations, 'results': results}
        (directory / 'owned-zombie-proof.json').write_text(json.dumps(report, indent=2))
        return report
    finally:
        guard.OwnedChildReaper.drain = original_drain
        # Only this disposable fixture's known child PIDs/groups may be cleaned.
        for pgid, pid in owned:
            try: os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError: pass
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass
        if foreign is not None:
            try: os.waitpid(foreign, 0)
            except ChildProcessError: pass


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux adoption proof; real PID1 is a separate mandatory driver')
def test_owned_zombie_adoption_in_disposable_subreaper(tmp_path):
    status, path = guard.supervise([sys.executable, '-u', __file__, '--owned-zombie-subreaper', str(tmp_path / 'probe')],
                                   timeout=10, artifact_root=tmp_path / 'outer')
    assert status == 0, (path / 'stderr.log').read_text()
    assert json.loads((tmp_path / 'probe/owned-zombie-proof.json').read_text())['pid1'] is False


if __name__ == '__main__':
    assert len(sys.argv) == 3 and sys.argv[1] in ('--owned-zombie-pid1', '--owned-zombie-subreaper')
    print(json.dumps(owned_zombie_driver(sys.argv[2], require_pid1=sys.argv[1] == '--owned-zombie-pid1')))
