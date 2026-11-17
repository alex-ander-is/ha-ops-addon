"""Host-only guards for the local VM wrapper; the real bind proof is separate."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "dev/virtualization/ha_ops_dev.py"


def test_reusable_image_declares_backend_dependencies():
    # The backend imports yaml at module load; a pytest-only image cannot
    # collect or execute its tests even though the topology probe succeeds.
    # Preview workflows also execute rsync in disposable repository fixtures.
    requirements = (SOURCE.parents[2] / "requirements-dev.txt").read_text().splitlines()
    assert any(line.lower().startswith("pyyaml==") for line in requirements)
    dockerfile = (SOURCE.parent / "Dockerfile").read_text()
    assert "ARG RSYNC_DEB_VERSION=" in dockerfile
    assert '"rsync=${RSYNC_DEB_VERSION}"' in dockerfile


def pinned_toolchain():
    return json.loads((SOURCE.parent / "toolchain.json").read_text())


def wrapper():
    spec = importlib.util.spec_from_file_location("ha_ops_dev", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_wrapper():
    source = SOURCE.parent / 'runtime_qualification.py'
    spec = importlib.util.spec_from_file_location('runtime_qualification_test', source)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(source.parent))
    try: spec.loader.exec_module(module)
    finally: sys.path.remove(str(source.parent))
    return module


def runtime_inspection():
    return {'Id': 'client', 'Config': {'Image': 'sha256:app', 'Labels': {'ha-ops.dev.run': 'owner'}, 'Env': []},
            'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True},
            'Mounts': [{'Type': 'bind', 'Destination': '/tests', 'Source': '/owned/tests', 'RW': False},
                       {'Type': 'volume', 'Name': 'owned-tmpfs', 'Destination': '/workspace/output', 'RW': True}]}


def test_original_runtime_accepts_only_exact_owned_mounts():
    subject = runtime_wrapper()
    subject.verify_container(runtime_inspection(), 'client', 'owner', 'sha256:app',
                             {'/tests': (Path('/owned/tests'), False)}, 'none', 'owned-tmpfs')


@pytest.mark.parametrize('mutation', ['network', 'image', 'app-overlay', 'foreign-volume', 'writable-tests',
                                   'privileged', 'devices', 'ports', 'proxy', 'rootfs'])
def test_original_runtime_refuses_foreign_execution_contour(mutation):
    subject = runtime_wrapper(); entry = runtime_inspection()
    if mutation == 'network': entry['HostConfig']['NetworkMode'] = 'host'
    elif mutation == 'image': entry['Config']['Image'] = 'foreign'
    elif mutation == 'app-overlay': entry['Mounts'].append({'Type': 'bind', 'Destination': '/app', 'Source': '/development/app', 'RW': False})
    elif mutation == 'foreign-volume': entry['Mounts'][1]['Name'] = 'foreign'
    elif mutation == 'writable-tests': entry['Mounts'][0]['RW'] = True
    elif mutation == 'privileged': entry['HostConfig']['Privileged'] = True
    elif mutation == 'devices': entry['HostConfig']['Devices'] = ['/dev/sda']
    elif mutation == 'ports': entry['HostConfig']['PortBindings'] = {'80/tcp': [{'HostPort': '80'}]}
    elif mutation == 'proxy': entry['Config']['Env'] = ['HTTPS_PROXY=http://foreign']
    elif mutation == 'rootfs': entry['HostConfig']['ReadonlyRootfs'] = False
    with pytest.raises(subject.vm.Refused):
        subject.verify_container(entry, 'client', 'owner', 'sha256:app',
                                 {'/tests': (Path('/owned/tests'), False)}, 'none', 'owned-tmpfs')


def test_runtime_skipped_required_test_is_not_qualification():
    subject = runtime_wrapper()
    result = subject.test_accounting('<testsuites><testsuite tests="1"><testcase classname="network" name="required"><skipped message="missing ssh"/></testcase></testsuite></testsuites>', expected=['network.py::required'])
    assert result['collected'] == 1 and result['passed'] == 0
    assert result['required_unexecuted'] == [{'id': 'network.py::required', 'reason': 'missing ssh'}]


def test_runtime_declared_scope_skip_cannot_waive_an_unrelated_obligation():
    subject = runtime_wrapper()
    reason = 'root SSH fixture; independent graph-loss recovery is qualified through HTTPS'
    unrelated = subject.test_accounting('<testsuites><testsuite tests="1"><testcase classname="network" name="required"><skipped message="' + reason + '"/></testcase></testsuite></testsuites>', expected=['network.py::required'])
    assert len(unrelated['required_unexecuted']) == 1
    declared = subject.test_accounting('<testsuites><testsuite tests="1"><testcase classname="test_native_scope" name="test_scope_authenticated_network_publication_and_same_commit_recovery[ssh-accepted-readback-loss]"><skipped message="' + reason + '"/></testcase></testsuite></testsuites>', expected=['test_native_scope.py::test_scope_authenticated_network_publication_and_same_commit_recovery[ssh-accepted-readback-loss]'])
    assert declared['required_unexecuted'] == []
    assert len(declared['declared_unexecuted']) == 1


def test_runtime_ext4_skip_is_bound_to_exact_case_and_profile():
    subject = runtime_wrapper()
    xml = '<testsuites><testsuite tests="1"><testcase classname="test_native_ext4" name="test_network_publication_ext4_frozen_physical_peak"><skipped message="different bounded ext4 profile"/></testcase></testsuite></testsuites>'
    expected = ['test_native_ext4.py::test_network_publication_ext4_frozen_physical_peak']
    assert len(subject.test_accounting(xml, expected=expected, ext4_profile='allocation')['required_unexecuted']) == 1
    assert subject.test_accounting(xml, expected=expected, ext4_profile='bytes')['required_unexecuted'] == []


@pytest.mark.parametrize('kind', ['absent', 'duplicate', 'unexpected', 'suite-count', 'no-manifest', 'contradictory'])
def test_runtime_accounting_requires_the_reviewed_complete_identity_set(kind):
    subject = runtime_wrapper()
    cases = '<testcase classname="network" name="first"/>'
    expected = ['network.py::first', 'network.py::second']
    count = 1
    if kind == 'duplicate': cases += cases; count = 2
    elif kind == 'unexpected': cases += '<testcase classname="network" name="foreign"/>'; count = 2
    elif kind == 'suite-count': count = 2; expected = ['network.py::first']
    elif kind == 'no-manifest': expected = None
    elif kind == 'contradictory':
        cases = '<testcase classname="network" name="first"><skipped/><failure/></testcase>'
        expected = ['network.py::first']
    result = subject.test_accounting('<testsuites><testsuite tests="' + str(count) + '">' + cases + '</testsuite></testsuites>', expected=expected)
    assert result['required_unexecuted']
    if kind == 'contradictory': assert any('Contradictory' in error for error in result['suite_errors'])


def dependency_fixture(tmp_path, monkeypatch):
    import hashlib
    import zipfile
    subject = runtime_wrapper()
    cache = tmp_path.resolve() / 'cache'; (cache / 'pip').mkdir(parents=True)
    wheel = cache / 'pip' / 'pytest-8.4.2-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive: archive.writestr('pytest/__init__.py', 'reviewed = True\n')
    (tmp_path / 'runtime-toolchain.json').write_text(json.dumps({'pytest_wheels': ['pytest==8.4.2 --hash=sha256:' + hashlib.sha256(wheel.read_bytes()).hexdigest()]}))
    monkeypatch.setattr(subject, 'CACHE', cache)
    monkeypatch.setattr(subject.vm, 'HERE', tmp_path.resolve())
    return subject, cache


def test_fresh_reviewed_dependencies_exclude_and_preserve_stale_ssl(tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    stale = cache / 'pytest-only'; stale.mkdir(); (stale / 'ssl.py').write_text('foreign = True\n')
    fresh = subject.pytest_dependencies()
    assert fresh != stale and not (fresh / 'ssl.py').exists()
    assert (stale / 'ssl.py').read_text() == 'foreign = True\n'
    assert subject.validate_dependencies(fresh)
    subject.cleanup_dependencies(fresh)


@pytest.mark.parametrize('kind', ['new-module', 'changed-file', 'symlink', 'empty-directory'])
def test_reviewed_dependency_namespace_refuses_changes(tmp_path, monkeypatch, kind):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    fresh = subject.pytest_dependencies()
    if kind == 'new-module': (fresh / 'ssl.py').write_text('foreign = True\n')
    elif kind == 'changed-file': (fresh / 'pytest/__init__.py').write_text('foreign = True\n')
    elif kind == 'symlink': (fresh / 'ssl.py').symlink_to(fresh / 'pytest/__init__.py')
    else: (fresh / 'foreign').mkdir()
    try:
        with pytest.raises(subject.vm.Refused): subject.validate_dependencies(fresh)
    finally:
        subject.DEPENDENCY_OWNERS[fresh].close()


def test_advertised_architectures_have_installable_profiles():
    import yaml
    root = SOURCE.parents[2]
    config = yaml.safe_load((root / 'ha-ops/config.yaml').read_text())
    build = yaml.safe_load((root / 'ha-ops/build.yaml').read_text())
    assert set(config['arch']) == set(build['build_from']) == {'aarch64', 'amd64'}
    for architecture in config['arch']:
        assert (root / ('ha-ops/runtime-apk-' + architecture + '.lock')).is_file()
    assert 'aarch64|amd64)' in (root / 'ha-ops/runtime-install.sh').read_text()
    profile = json.loads((root / 'ha-ops/runtime-profile.json').read_text())
    assert 'x86_64' not in profile['qualified']


def test_receiver_control_observes_acknowledged_changes_both_directions(monkeypatch):
    import network_publication_helpers as helper
    values = {}
    def request(**payload):
        if payload['operation'] == 'set': values[payload['key']] = payload['value']
        elif payload['operation'] == 'del': del values[payload['key']]
        return dict(values)
    monkeypatch.setattr(helper, 'receiver_request', request)
    control = helper.ReceiverControl({'stall_upload': True})
    assert values['stall_upload'] is True
    values['upload_started'] = True
    assert control.get('upload_started') is True
    control['release_upload'] = True
    assert values['release_upload'] is True
    control.clear()
    assert values == {}


def test_runtime_payload_copy_retains_directory_and_executable_modes(tmp_path, monkeypatch):
    subject = runtime_wrapper()
    source = tmp_path / 'source'; source.mkdir(mode=0o755)
    (source / 'app').mkdir(mode=0o755)
    executable = source / 'app' / 'tool'; executable.write_text('#!/bin/sh\n'); executable.chmod(0o751)
    destination = tmp_path / 'snapshot'
    snapshot = {'records': {(): ('directory', (1, 1, 0o40755)), ('app',): ('directory', (1, 2, 0o40755))}}
    def private_copy(source, destination, expected):
        import shutil
        shutil.copytree(source, destination)
        destination.chmod(0o700); (destination / 'app').chmod(0o700)
    monkeypatch.setattr(subject.vm, 'copy_input', private_copy)
    subject.copy_preserving_modes(source, destination, snapshot)
    assert destination.stat().st_mode & 0o777 == 0o755
    assert (destination / 'app').stat().st_mode & 0o777 == 0o755
    assert (destination / 'app' / 'tool').stat().st_mode & 0o777 == 0o751


def test_ext4_runtime_owner_extension_does_not_admit_arbitrary_identity():
    subject = wrapper()
    with pytest.raises(subject.Refused, match='nobody'):
        subject.ext4_guest('setup', 'a' * 32, owner=(0, 0))


def topology_runner():
    source = Path(__file__).resolve().parent / "integration/run_native_topology.py"
    spec = importlib.util.spec_from_file_location("native_runner_local", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(stdout=b"", stderr=b"", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_host_space_gate_precedes_install_or_vm_mutation(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "HERE", tmp_path)
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: (_ for _ in ()).throw(subject.Refused("low space")))
    monkeypatch.setattr(subject, "installed", lambda: pytest.fail("install before disk gate"))
    with pytest.raises(subject.Refused, match="low space"):
        subject.setup()


def test_profile_start_keeps_active_context_and_scopes_share(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    commands = []
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: commands.append(args) or completed(b"user-context\n"))
    monkeypatch.setattr(subject, "colima", lambda *args, **kw: commands.append(["colima", *args]) or b"")
    subject.start_vm()
    assert commands[0] == ["docker", "context", "show"]
    assert commands[-1] == commands[0]
    start = next(args for args in commands if args[:2] == ["colima", "start"])
    assert start[:3] == ["colima", "start", "ha-ops-dev"]
    assert "--activate=false" in start
    assert start[start.index("--mount") + 1] == str(subject.LOCAL) + ":w"
    assert (subject.LOCAL / "docker-cli/config.json").read_text() == "{}\n"


def test_docker_route_ignores_hostile_ambient_context(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "LOCAL", tmp_path)
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote.invalid")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setenv("BUILDX_BUILDER", "remote-builder")
    calls = []
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: calls.append((args, kw["env"])) or completed())
    subject.docker("info")
    args, environment = calls[0]
    assert args[:3] == ["docker", "--host", subject.ENDPOINT]
    assert not {"DOCKER_CONTEXT", "DOCKER_HOST", "BUILDX_BUILDER"} & environment.keys()
    assert environment["DOCKER_CONFIG"] == str(tmp_path / "docker-cli")


def test_colima_route_ignores_hostile_ambient_home(monkeypatch):
    subject = wrapper()
    monkeypatch.setenv("COLIMA_HOME", "/tmp/alternate")
    monkeypatch.setenv("COLIMA_PROFILE", "alternate")
    monkeypatch.setenv("LIMA_HOME", "/tmp/alternate-lima")
    calls = []
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: calls.append((args, kw["env"])) or completed())
    subject.colima("ssh", "--profile", subject.PROFILE, "--", "uname", "-m")
    args, environment = calls[0]
    assert args[:4] == ["colima", "ssh", "--profile", "ha-ops-dev"]
    assert not {"COLIMA_HOME", "COLIMA_PROFILE", "LIMA_HOME"} & environment.keys()


def test_wrong_socket_refused_before_docker_mutation(tmp_path, monkeypatch):
    subject = wrapper()
    bad = tmp_path / "docker.sock"
    bad.write_bytes(b"not a socket")
    monkeypatch.setattr(subject, "SOCKET", bad)
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    with pytest.raises(subject.Refused, match="Unix Docker socket"):
        subject.endpoint_info()


def test_endpoint_transport_failure_is_distinct_from_live_version_mismatch(tmp_path, monkeypatch):
    subject = wrapper()
    path = tmp_path / "docker.sock"
    path.touch()
    monkeypatch.setattr(subject, "SOCKET", path)
    monkeypatch.setattr(subject.stat, "S_ISSOCK", lambda mode: True)
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: completed(stderr=b"Cannot connect to the Docker daemon", code=1))
    with pytest.raises(subject.EndpointUnavailable):
        subject.endpoint_info()
    observed = {"OSType": "linux", "ServerVersion": "wrong", "Name": "colima-ha-ops-dev"}
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: completed(json.dumps(observed).encode()))
    with pytest.raises(subject.Refused, match="version differs") as error:
        subject.endpoint_info()
    assert not isinstance(error.value, subject.EndpointUnavailable)


def test_vm_source_byte_mismatch_refused_before_docker(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "guest", lambda *args: b"wrong bytes")
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    with pytest.raises(subject.Refused, match="different bytes"):
        subject.verify_source(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_low_vm_space_refused_before_build(monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "guest", lambda *args: b"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vda 10000 9000 1000 90% /var/lib/docker\n")
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    with pytest.raises(subject.Refused, match="Insufficient VM"):
        subject.vm_capacity()


@pytest.mark.parametrize("unsafe", [".git", ".local", "node_modules", ".codex-audit"])
def test_generated_inputs_refused(tmp_path, monkeypatch, unsafe):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    source = tmp_path / unsafe
    source.mkdir()
    with pytest.raises(subject.Refused):
        subject.copy_input(source, tmp_path / "copy")


def test_symlinked_input_refused_without_copy(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "escape").symlink_to(tmp_path)
    with pytest.raises(subject.Refused, match="Symlink"):
        subject.copy_input(safe, tmp_path / "copy")
    assert not (tmp_path / "copy/escape").exists()


@pytest.mark.parametrize("replacement", ["file-symlink", "directory-symlink", "regular-file"])
def test_copy_refuses_source_replacement_at_descriptor_open(tmp_path, monkeypatch, replacement):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(subject, "ROOT", repository)
    source = repository / "fixture"
    nested = source / "nested"
    nested.mkdir(parents=True)
    safe = nested / "safe.py"
    safe.write_text("print('safe')\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "safe.py").write_text("PRIVATE OUTSIDE BYTES")
    snapshot = subject.snapshot_input(source)
    original_open = subject.os.open
    target_name = "nested" if replacement == "directory-symlink" else "safe.py"
    triggered = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal triggered
        if path == target_name and kwargs.get("dir_fd") is not None and not triggered:
            triggered = True
            if replacement == "directory-symlink":
                safe.unlink()
                nested.rmdir()
                nested.symlink_to(outside, target_is_directory=True)
            else:
                safe.unlink()
                if replacement == "file-symlink":
                    safe.symlink_to(outside / "safe.py")
                else:
                    safe.write_text("PRIVATE OUTSIDE BYTES")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(subject.os, "open", racing_open)
    destination = tmp_path / "copied"
    with pytest.raises(subject.Refused, match="Input changed|cannot be copied safely"):
        subject.copy_input(source, destination, expected=snapshot)
    assert triggered
    assert not destination.exists()


def test_generic_run_rejects_file_swap_after_preflight_without_vm_or_docker(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "diagnostics")
    source = repository / "fixture"
    source.mkdir()
    safe = source / "safe.py"
    safe.write_text("print('safe')\n")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("PRIVATE OUTSIDE BYTES")
    def swap_after_preflight(*args):
        safe.unlink()
        safe.symlink_to(outside)
        return 40 * subject.GIB
    monkeypatch.setattr(subject, "capacity", swap_after_preflight)
    monkeypatch.setattr(subject, "setup", lambda **kw: pytest.fail("VM or Docker setup attempted"))
    with pytest.raises(subject.Refused):
        subject.generic_run(Path("fixture"), ["python3", "safe.py"])
    assert not subject.RUNS.exists() or not list(subject.RUNS.iterdir())


def test_copy_discards_bytes_if_open_file_changes_during_read(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(subject, "ROOT", repository)
    safe = repository / "safe.py"
    safe.write_text("print('safe')\n")
    snapshot = subject.snapshot_input(safe)
    inode = safe.stat().st_ino
    original_read = subject.os.read
    changed = False
    def racing_read(handle, count):
        nonlocal changed
        if not changed and subject.os.fstat(handle).st_ino == inode:
            changed = True
            safe.write_text("PRIVATE BYTES")
        return original_read(handle, count)
    monkeypatch.setattr(subject.os, "read", racing_read)
    destination = tmp_path / "copied"
    with pytest.raises(subject.Refused, match="changed during copy"):
        subject.copy_input(safe, destination, expected=snapshot)
    assert changed
    assert not destination.exists()


def test_build_fingerprint_tracks_all_inputs_and_platform(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    monkeypatch.setattr(subject, "mount_points", lambda: {Path("/")})
    inputs = {}
    for name in subject.BUILD_INPUTS:
        path = tmp_path / name
        path.write_bytes(name.encode())
        inputs[name] = path
    monkeypatch.setattr(subject, "BUILD_INPUTS", inputs)
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(b"fixed-tool-version"))
    monkeypatch.setattr(subject, "colima", lambda *args: b"fixed-colima-version")
    baseline = subject.fingerprint()
    for path in inputs.values():
        original = path.read_bytes()
        path.write_bytes(original + b" changed")
        assert subject.fingerprint() != baseline
        path.write_bytes(original)
    monkeypatch.setattr(subject.platform, "machine", lambda: "different-arch")
    assert subject.fingerprint() != baseline


def test_setup_reuses_image_and_builds_only_on_fingerprint_change(tmp_path, monkeypatch):
    subject = wrapper()
    subject.TOOLCHAIN.update(pinned_toolchain())
    monkeypatch.setattr(subject, "load_toolchain", lambda context: None)
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    subject.RUNS.mkdir(parents=True)
    monkeypatch.setattr(subject, "HERE", tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "installed", lambda: None)
    monkeypatch.setattr(subject, "start_vm", lambda: None)
    monkeypatch.setattr(subject, "ready", lambda **kw: {"host_free_bytes": 40 * subject.GIB})
    monkeypatch.setattr(subject, "guest_platform", lambda: "linux/arm64")
    monkeypatch.setattr(subject, "verify_image_arch", lambda *a: None)
    monkeypatch.setattr(subject, "fingerprint", lambda context=None: "fixed")
    monkeypatch.setattr(subject, "colima", lambda *a: b"colima 0.10")
    monkeypatch.setattr(subject, "guest", lambda *a: b"aarch64")
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(b"version"))
    monkeypatch.setattr(subject, "make_context", lambda: b"context")
    present = set()
    builds = []
    monkeypatch.setattr(subject, "image_present", lambda image: image in present)
    def fake_docker(*args, **kwargs):
        if args[0] == "build":
            builds.append(args)
            present.add(args[args.index("--tag") + 1])
        if args[0] == "tag":
            present.add(args[2])
        if args[:2] == ("image", "rm"):
            present.discard(args[2])
        return completed(b"git version 2.39.5\n")
    monkeypatch.setattr(subject, "docker", fake_docker)
    subject.setup()
    subject.setup()
    assert len(builds) == 1
    monkeypatch.setattr(subject, "fingerprint", lambda context=None: "changed")
    subject.setup()
    assert len(builds) == 2


def test_generic_run_rejects_bad_mount_and_cleans_exact_container(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "diagnostics")
    subject.RUNS.mkdir()
    (tmp_path / "input.txt").write_text("fixture")
    monkeypatch.setattr(subject, "setup", lambda **kw: {"image": "ha-ops-dev:fixed", "platform": "linux/arm64"})
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    monkeypatch.setattr(subject, "verify_source", lambda path: None)
    removed = []
    monkeypatch.setattr(subject, "remove_owned", lambda name, run_id: removed.append((name, run_id)))
    calls = []
    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return completed(json.dumps([{"Id": "container-id", "Config": {"Labels": {"ha-ops.dev.run": calls[0][4].split("=", 1)[1]}},
                                         "Mounts": [], "HostConfig": {"NetworkMode": "none"}}]).encode())
        return completed(b"container-id")
    monkeypatch.setattr(subject, "docker", fake_docker)
    with pytest.raises(subject.Refused, match="mount inspection"):
        subject.generic_run(Path("input.txt"), ["python3", "-V"])
    assert len(removed) == 1
    assert not list(subject.RUNS.iterdir())
    run_args = next(args for args in calls if args[0] == "create")
    assert "none" == run_args[run_args.index("--network") + 1]
    assert "--read-only" in run_args
    assert sum(part == "--mount" for part in run_args) == 2
    assert not any(args[0] == "start" for args in calls)


def test_cleanup_refuses_container_with_other_owner(monkeypatch):
    subject = wrapper()
    calls = []
    def fake_docker(*args, **kwargs):
        calls.append(args)
        return completed(json.dumps([{"Config": {"Labels": {"ha-ops.dev.run": "other"}}}]).encode())
    monkeypatch.setattr(subject, "docker", fake_docker)
    with pytest.raises(subject.Refused, match="another owner"):
        subject.remove_owned("ha-ops-dev-owned", "owned")
    assert calls == [("container", "inspect", "ha-ops-dev-owned")]


def test_topology_exact_source_mismatch_blocks_build(tmp_path, monkeypatch):
    subject = topology_runner()
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    monkeypatch.setattr(subject.subprocess, "run", lambda *a, **kw: completed(b"wrong bytes"))
    source = tmp_path / "exact-source"
    source.mkdir()
    with pytest.raises(RuntimeError, match="different bytes"):
        subject.verify_vm_sources({"data": source}, "ha-ops-dev")
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("filename", ["settings.yaml", "nested/secrets.json", "nested/api_token.json",
                                      "nested/passwords.txt", "nested/auth.json", "nested/service_account.json",
                                      "nested/.docker/config.json", "nested/.storage/core.config_entries",
                                      "nested/.env.local", "nested/private.pem", "nested/unknown.psd"])
def test_generic_copy_preserves_regular_repository_files(tmp_path, monkeypatch, filename):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    source = tmp_path / "fixture"
    path = source / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ordinary repository fixture")
    subject.copy_input(source, tmp_path / "copied")
    assert (tmp_path / "copied" / filename).read_bytes() == b"ordinary repository fixture"


@pytest.mark.parametrize("unsafe", ["missing", "fixture/link"])
def test_generic_unsafe_input_never_provisions_or_uses_docker(tmp_path, monkeypatch, unsafe):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    subject.RUNS.mkdir()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    if unsafe.endswith("link"):
        (fixture / "link").symlink_to(tmp_path)
    monkeypatch.setattr(subject, "setup", lambda **kw: pytest.fail("VM or Docker setup attempted"))
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker queried"))
    with pytest.raises(subject.Refused):
        subject.generic_run(Path(unsafe if unsafe == "missing" else "fixture"), ["python3", "-V"])
    assert not list(subject.RUNS.iterdir())


def test_generic_disk_gate_precedes_vm_or_docker(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    (tmp_path / "input.py").write_text("print('safe')\n")
    monkeypatch.setattr(subject, "capacity", lambda *a: (_ for _ in ()).throw(subject.Refused("low space")))
    monkeypatch.setattr(subject, "setup", lambda **kw: pytest.fail("VM setup attempted"))
    with pytest.raises(subject.Refused, match="low space"):
        subject.generic_run(Path("input.py"), ["python3", "input.py"])


def test_exact_share_gate_precedes_docker_build(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "installed", lambda: None)
    monkeypatch.setattr(subject, "start_vm", lambda: None)
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    monkeypatch.setattr(subject, "verify_source", lambda path: (_ for _ in ()).throw(subject.Refused("share mismatch")))
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker queried before exact share gate"))
    with pytest.raises(subject.Refused, match="share mismatch"):
        subject.setup(exact_sources=(tmp_path,))


def test_generic_copy_keeps_repository_root_layout(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(subject, "ROOT", repository)
    workflow = repository / ".github/workflows/native-topology.yml"
    test = repository / "ha-ops/tests/test_native_topology.py"
    workflow.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    workflow.write_text("on: push\n")
    test.write_text("fixture")
    (repository / "design.psd").write_text("unrelated binary")
    (repository / ".DS_Store").write_text("host metadata")
    (repository / ".git").mkdir()
    subject.copy_input(repository, tmp_path / "copied")
    assert (tmp_path / "copied/.github/workflows/native-topology.yml").read_text() == "on: push\n"
    assert (tmp_path / "copied/ha-ops/tests/test_native_topology.py").read_text() == "fixture"
    assert not (tmp_path / "copied/.git").exists()
    assert (tmp_path / "copied/design.psd").read_text() == "unrelated binary"
    assert not (tmp_path / "copied/.DS_Store").exists()


def test_nested_mount_refused_before_vm_or_docker(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    mounted = repository / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "settings.yaml").write_text("ordinary fixture")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "mount_points", lambda: {Path("/"), mounted})
    monkeypatch.setattr(subject, "setup", lambda **kw: pytest.fail("VM setup attempted"))
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker queried"))
    with pytest.raises(subject.Refused, match="mounted filesystem"):
        subject.generic_run(Path("."), ["true"])
    assert not subject.RUNS.exists()


@pytest.mark.parametrize("filesystem", ["ext4", "none"])
def test_linux_mount_output_rejects_exact_same_device_bind(tmp_path, monkeypatch, filesystem):
    subject = wrapper()
    repository = tmp_path / "repo"
    mounted = repository / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "settings.yaml").write_text("ordinary fixture")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject.platform, "system", lambda: "Linux")
    mount_output = (f"overlay on / type overlay (rw)\n"
                    f"/tmp/source on {mounted} type {filesystem} (rw,bind)\n").encode()
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(mount_output))
    assert mounted in subject.mount_points()
    with pytest.raises(subject.Refused, match="mounted filesystem"):
        subject.snapshot_input(mounted)


def test_darwin_mount_output_preserves_literal_type_in_path(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    mounted = repository / "mounted type data"
    mounted.mkdir(parents=True)
    (mounted / "settings.yaml").write_text("ordinary fixture")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject.platform, "system", lambda: "Darwin")
    output = f"/dev/disk on / (apfs, local)\n/dev/disk on {mounted} (apfs, local)\n".encode()
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(output))
    assert mounted in subject.mount_points()
    with pytest.raises(subject.Refused, match="mounted filesystem"):
        subject.snapshot_input(mounted)


@pytest.mark.parametrize("change", ["add", "remove"])
def test_directory_entry_race_discards_fixture_before_vm(tmp_path, monkeypatch, change):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "fixtures"
    source.mkdir()
    (source / "a.txt").write_text("A")
    extra = source / "z.txt"
    if change == "remove":
        extra.write_text("Z")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "diagnostics")
    original_read = subject.os.read
    changed = False
    def racing_read(handle, count):
        nonlocal changed
        if not changed and subject.os.fstat(handle).st_ino == (source / "a.txt").stat().st_ino:
            changed = True
            if change == "add":
                extra.write_text("Z")
            else:
                extra.unlink()
        return original_read(handle, count)
    monkeypatch.setattr(subject.os, "read", racing_read)
    monkeypatch.setattr(subject, "setup", lambda **kw: pytest.fail("VM setup attempted"))
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker queried"))
    with pytest.raises(subject.Refused, match="directory changed|Input changed"):
        subject.generic_run(Path("fixtures"), ["true"])
    assert changed
    assert not list(subject.RUNS.iterdir())


def test_build_context_refuses_source_change_while_copying(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    source = tmp_path / "input"
    source.write_bytes(b"original")
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"input": source})
    original_read = subject.os.read
    def racing_read(handle, count):
        if subject.os.fstat(handle).st_ino == source.stat().st_ino:
            source.write_bytes(b"modified")
        return original_read(handle, count)
    monkeypatch.setattr(subject.os, "read", racing_read)
    with pytest.raises(subject.Refused, match="Build input changed"):
        subject.make_context()


def test_build_context_rejects_outside_symlink_and_direct_path(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside bytes")
    link = repository / "input"
    link.symlink_to(outside)
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"input": link})
    with pytest.raises(subject.Refused, match="Symlink"):
        subject.make_context()
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"input": outside})
    with pytest.raises(subject.Refused, match="inside this repository"):
        subject.make_context()


def test_toolchain_symlink_is_never_read_before_host_setup(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    tools_dir = repository / "dev/virtualization"
    tools_dir.mkdir(parents=True)
    copied_wrapper = tools_dir / "ha_ops_dev.py"
    copied_wrapper.write_bytes(SOURCE.read_bytes())
    (repository / "dev/test_runner.py").write_bytes((SOURCE.parent.parent / "test_runner.py").read_bytes())
    outside = tmp_path / "outside-toolchain.json"
    outside.write_text(json.dumps({**pinned_toolchain(), "docker_cli": "outside-value"}))
    (tools_dir / "toolchain.json").symlink_to(outside)
    spec = importlib.util.spec_from_file_location("isolated_wrapper", copied_wrapper)
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    assert subject.TOOLCHAIN == {}
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"toolchain.json": tools_dir / "toolchain.json"})
    monkeypatch.setattr(subject, "capacity", lambda *a: pytest.fail("Host capacity gate used"))
    monkeypatch.setattr(subject, "installed", lambda: pytest.fail("Tool installation used"))
    monkeypatch.setattr(subject, "start_vm", lambda: pytest.fail("VM setup used"))
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker used"))
    with pytest.raises(subject.Refused, match="Symlink"):
        subject.setup()


def test_transient_source_mutation_cannot_change_build_stream(tmp_path, monkeypatch):
    subject = wrapper()
    subject.TOOLCHAIN.update(pinned_toolchain())
    monkeypatch.setattr(subject, "load_toolchain", lambda context: None)
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "input"
    source.write_bytes(b"original")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"input": source})
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    subject.RUNS.mkdir(parents=True)
    monkeypatch.setattr(subject, "HERE", tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "installed", lambda: None)
    monkeypatch.setattr(subject, "start_vm", lambda: None)
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    monkeypatch.setattr(subject, "guest_platform", lambda: "linux/arm64")
    monkeypatch.setattr(subject, "mount_points", lambda: {Path("/")})
    monkeypatch.setattr(subject, "colima", lambda *a: b"colima 0.10")
    monkeypatch.setattr(subject, "guest", lambda *a: b"aarch64")
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(b"version"))
    present = set()
    monkeypatch.setattr(subject, "image_present", lambda image: image in present)
    calls = []
    consumed = []
    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "build":
            assert args[-1] == "-"
            with subject.tarfile.open(fileobj=subject.io.BytesIO(kwargs["input"])) as archive:
                consumed.append(archive.extractfile("input").read())
            source.write_bytes(b"changed during build")
            source.write_bytes(b"original")
            present.add(args[args.index("--tag") + 1])
        if args[0] == "tag":
            present.add(args[2])
        if args[:2] == ("image", "rm"):
            present.discard(args[2])
        return completed(b"git version 2.39.5\n")
    monkeypatch.setattr(subject, "docker", fake_docker)
    monkeypatch.setattr(subject, "verify_image_arch", lambda *a: None)
    result = subject.setup()
    assert consumed == [b"original"]
    assert result["image"] in present
    assert any(args[0] == "tag" for args in calls)
    assert any(args[:2] == ("image", "rm") for args in calls)
    assert not list(subject.RUNS.iterdir())


def test_failed_temporary_image_tag_cleanup_refuses_setup(tmp_path, monkeypatch):
    subject = wrapper()
    subject.TOOLCHAIN.update(pinned_toolchain())
    monkeypatch.setattr(subject, "load_toolchain", lambda context: None)
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "Dockerfile"
    source.write_bytes(b"FROM scratch\n")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"Dockerfile": source})
    monkeypatch.setattr(subject, "HERE", tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "installed", lambda: None)
    monkeypatch.setattr(subject, "start_vm", lambda: None)
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    monkeypatch.setattr(subject, "guest_platform", lambda: "linux/arm64")
    monkeypatch.setattr(subject, "mount_points", lambda: {Path("/")})
    monkeypatch.setattr(subject, "colima", lambda *a: b"colima 0.10")
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(b"version"))
    present = set()
    monkeypatch.setattr(subject, "image_present", lambda image: image in present)
    def fake_docker(*args, **kw):
        if args[0] == "build":
            present.add(args[args.index("--tag") + 1])
        if args[0] == "tag":
            present.add(args[2])
        if args[:2] == ("image", "rm"):
            return completed(stderr=b"removal denied", code=1)
        return completed()
    monkeypatch.setattr(subject, "docker", fake_docker)
    with pytest.raises(subject.Refused, match=r"Temporary image tag cleanup failed for ha-ops-dev:build-[0-9a-f]+.*removal denied"):
        subject.setup()
    assert any(image.startswith("ha-ops-dev:build-") for image in present)


def test_build_fingerprint_uses_the_context_passed_to_docker(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    source = tmp_path / "input"
    source.write_bytes(b"original")
    monkeypatch.setattr(subject, "BUILD_INPUTS", {"input": source})
    monkeypatch.setattr(subject, "invoke", lambda args, **kw: completed(b"fixed-tool-version"))
    monkeypatch.setattr(subject, "colima", lambda *args: b"fixed-colima-version")
    monkeypatch.setattr(subject, "mount_points", lambda: {Path("/")})
    context = subject.make_context()
    expected = subject.fingerprint(context)
    source.write_bytes(b"later source version")
    assert subject.fingerprint(context) == expected
    assert subject.fingerprint() != expected


def test_generic_cleanup_uses_verified_immutable_id_after_name_race(monkeypatch):
    subject = wrapper()
    calls = []
    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            if args[2] == "owned-name":
                return completed(json.dumps([{"Id": "own-id", "Config": {"Labels": {"ha-ops.dev.run": "own"}}}]).encode())
            return completed(stderr=b"No such object", code=1)
        return completed()
    monkeypatch.setattr(subject, "docker", fake_docker)
    subject.remove_owned("owned-name", "own")
    assert ("rm", "--force", "own-id") in calls
    assert ("rm", "--force", "owned-name") not in calls


def test_topology_cleanup_refuses_foreign_name(monkeypatch):
    subject = topology_runner()
    calls = []
    def fake_docker(args, **kwargs):
        calls.append(args)
        return json.dumps([{"Id": "foreign-id", "Config": {"Labels": {"ha-ops.dev.run": "foreign"}}}]).encode()
    monkeypatch.setattr(subject, "docker", fake_docker)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        subject.cleanup("image", ("colliding-name",), retain_image=True, run_id="ours")
    assert not any(args[0] == "rm" for args in calls)


def test_local_topology_does_not_start_before_mount_validation(tmp_path, monkeypatch):
    subject = topology_runner()
    (tmp_path / "fixture").mkdir()
    monkeypatch.setattr(subject, "verify_vm_sources", lambda *a: None)
    monkeypatch.setattr(subject, "cleanup", lambda *a, **kw: None)
    calls = []
    def fake_docker(args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            return b"created-id\n"
        if args[0] == "inspect":
            return b'[{"Mounts":[]}]'
        return b""
    monkeypatch.setattr(subject, "docker", fake_docker)
    with pytest.raises(RuntimeError, match="Unexpected mount"):
        subject.exercise(tmp_path / "fixture", "image", "first", "second", skip_build=True,
                         retain_image=True, run_id="a" * 32, vm_profile="ha-ops-dev", container_platform="linux/arm64")
    assert any(args[0] == "create" for args in calls)
    assert not any(args[0] == "start" for args in calls)


def test_status_reports_stopped_profile_without_docker(monkeypatch, capsys):
    subject = wrapper()
    monkeypatch.setattr(subject.shutil, "which", lambda name: "/bin/colima")
    monkeypatch.setattr(subject, "profile_state", lambda: "Stopped")
    monkeypatch.setattr(subject, "endpoint_info", lambda: pytest.fail("Docker queried for inactive profile"))
    monkeypatch.setattr(sys, "argv", ["ha_ops_dev.py", "status"])
    assert subject.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"


@pytest.mark.parametrize("failure, expected", [("permission denied", "unverified"),
                                               ("stale socket", "broken")])
def test_status_distinguishes_restricted_inspection_from_broken_vm(failure, expected, monkeypatch, capsys):
    subject = wrapper()
    monkeypatch.setattr(subject.shutil, "which", lambda name: "/bin/colima")
    monkeypatch.setattr(subject, "profile_state", lambda: "Broken")
    monkeypatch.setattr(subject, "endpoint_info", lambda: (_ for _ in ()).throw(subject.Refused(failure)))
    monkeypatch.setattr(sys, "argv", ["ha_ops_dev.py", "status"])
    assert subject.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == expected


def test_status_accepts_verified_named_engine_when_colima_process_check_says_broken(monkeypatch, capsys):
    subject = wrapper()
    monkeypatch.setattr(subject.shutil, "which", lambda name: "/bin/colima")
    monkeypatch.setattr(subject, "profile_state", lambda: "Broken")
    monkeypatch.setattr(subject, "endpoint_info", lambda: {"ServerVersion": "29.5.2", "Name": "colima-ha-ops-dev"})
    monkeypatch.setattr(subject, "image_name", lambda: "ha-ops-dev:fixed")
    monkeypatch.setattr(subject, "vm_capacity", lambda: 3 * subject.GIB)
    monkeypatch.setattr(sys, "argv", ["ha_ops_dev.py", "status"])
    assert subject.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "running"


def test_broken_profile_recovery_stops_before_start(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "active_context", lambda: b"default")
    monkeypatch.setattr(subject, "profile_state", lambda: "Broken")
    monkeypatch.setattr(subject, "endpoint_info", lambda: (_ for _ in ()).throw(subject.EndpointUnavailable("stale socket")))
    calls = []
    monkeypatch.setattr(subject, "colima", lambda *args, **kw: calls.append(args) or b"")
    subject.start_vm()
    assert calls[0] == ("stop", "ha-ops-dev")
    assert calls[1][:2] == ("start", "ha-ops-dev")


@pytest.mark.parametrize("rejection", ["Docker engine version differs from pinned toolchain",
                                       "Docker engine name does not identify ha-ops-dev",
                                       "Named Docker endpoint is not a Linux engine"])
def test_broken_profile_never_stops_responding_unverified_engine(tmp_path, monkeypatch, rejection):
    subject = wrapper()
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "active_context", lambda: b"default")
    monkeypatch.setattr(subject, "profile_state", lambda: "Broken")
    monkeypatch.setattr(subject, "endpoint_info", lambda: (_ for _ in ()).throw(subject.Refused(rejection)))
    monkeypatch.setattr(subject, "colima", lambda *a, **kw: pytest.fail("Colima VM mutated"))
    with pytest.raises(subject.Refused, match="recovery refused"):
        subject.start_vm()


@pytest.mark.parametrize("endpoint, should_refuse", [("live", False), ("permission denied", True)])
def test_broken_process_check_never_stops_unverified_live_profile(tmp_path, monkeypatch, endpoint, should_refuse):
    subject = wrapper()
    monkeypatch.setattr(subject, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(subject, "RUNS", tmp_path / "local/runs")
    monkeypatch.setattr(subject.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "active_context", lambda: b"default")
    monkeypatch.setattr(subject, "profile_state", lambda: "Broken")
    if endpoint == "live":
        monkeypatch.setattr(subject, "endpoint_info", lambda: {})
    else:
        monkeypatch.setattr(subject, "endpoint_info", lambda: (_ for _ in ()).throw(subject.Refused(endpoint)))
    calls = []
    monkeypatch.setattr(subject, "colima", lambda *args, **kw: calls.append(args) or b"")
    if should_refuse:
        with pytest.raises(subject.Refused, match="recovery refused"):
            subject.start_vm()
        assert calls == []
    else:
        subject.start_vm()
        assert len(calls) == 1 and calls[0][:2] == ("start", "ha-ops-dev")


def test_generic_run_emits_stderr_and_nonzero_exit(tmp_path, monkeypatch, capfd):
    subject = wrapper()
    monkeypatch.setattr(subject, "ROOT", tmp_path)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "diagnostics")
    subject.RUNS.mkdir()
    (tmp_path / "input.txt").write_text("fixture")
    monkeypatch.setattr(subject, "setup", lambda **kw: {"image": "image", "platform": "linux/arm64"})
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    monkeypatch.setattr(subject, "verify_source", lambda path: None)
    monkeypatch.setattr(subject, "remove_owned", lambda *a: None)
    def fake_docker(*args, **kwargs):
        if args[0] == "create":
            fake_docker.created = args
            return completed(b"own-id\n")
        if args[:2] == ("container", "inspect"):
            mounts = []
            for index, arg in enumerate(fake_docker.created):
                if arg == "--mount":
                    spec = dict(piece.split("=", 1) for piece in fake_docker.created[index + 1].split(",") if "=" in piece)
                    mounts.append({"Destination": spec["target"], "Source": spec["source"],
                                   "Type": "bind", "RW": "readonly" not in fake_docker.created[index + 1]})
            return completed(json.dumps([{"Id": "own-id", "Config": {"Labels": {"ha-ops.dev.run": fake_docker.created[4].split("=", 1)[1]}},
                                          "Mounts": mounts, "HostConfig": {"NetworkMode": "none"}}]).encode())
        if args[0] == "wait":
            return completed(b"7\n")
        if args[0] == "logs":
            return completed(b"", b"expected failure\n")
        return completed()
    monkeypatch.setattr(subject, "docker", fake_docker)
    assert subject.generic_run(Path("input.txt"), ["false"]) == 7
    assert fake_docker.created[fake_docker.created.index("--tmpfs") + 1] == "/tmp:exec"
    assert "expected failure" in capfd.readouterr().err


def test_topology_timeout_invalidates_passed_report_and_cleans_run(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    subject.RUNS.mkdir()
    report = tmp_path / "report.json"
    report.write_text('{"status":"passed"}\n')
    monkeypatch.setattr(subject, "setup", lambda **kw: {"image": "image", "platform": "linux/arm64"})
    monkeypatch.setattr(subject, "ready", lambda **kw: {})
    cleaned = []
    monkeypatch.setattr(subject, "cleanup_run", lambda run_id, fixture, owned: cleaned.append((run_id, fixture)))
    monkeypatch.setattr(subject, "invoke_child", lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("child", 1)))
    with pytest.raises(subprocess.TimeoutExpired):
        subject.topology(report)
    assert len(cleaned) == 1
    assert json.loads(report.read_text())["status"] == "failed"


def test_topology_prepares_exact_sources_before_setup_or_docker(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    observed = []
    def checked_setup(*, exact_sources):
        observed.extend(exact_sources)
        assert {path.name for path in exact_sources} == {
            "homeassistant", "addon_configs", "data", "backup", "fixture_ha_ops"}
        assert all(path.is_dir() for path in exact_sources)
        raise subject.Refused("share mismatch")
    monkeypatch.setattr(subject, "setup", checked_setup)
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: pytest.fail("Docker used before exact share gate"))
    monkeypatch.setattr(subject, "invoke_child", lambda *a, **kw: pytest.fail("Child used before exact share gate"))
    with pytest.raises(subject.Refused, match="share mismatch"):
        subject.topology(tmp_path / "report.json")
    assert len(observed) == 5
    assert not list(subject.RUNS.iterdir())
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"


def test_topology_runner_preserves_fixture_after_cleanup_failure(tmp_path, monkeypatch):
    subject = topology_runner()
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(subject, "preflight", lambda: {"engine": "test", "os": "linux"})
    monkeypatch.setattr(subject, "present", lambda *a: False)
    def failing_exercise(directory, *a, **kw):
        (Path(directory) / "evidence").write_text("left for inspection")
        raise subject.CleanupFailure("Owned resource cleanup failed")
    monkeypatch.setattr(subject, "exercise", failing_exercise)
    monkeypatch.setattr(sys, "argv", ["run_native_topology.py", "--fixture-root", str(fixture)])
    assert subject.main() == 1
    assert (fixture / "evidence").read_text() == "left for inspection"


def test_reusable_topology_rejects_symlink_fixture_root_before_preflight(tmp_path, monkeypatch):
    subject = topology_runner()
    runs = tmp_path / "runs"
    runs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    run_id = "a" * 32
    fixture = runs / run_id
    fixture.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(subject, "LOCAL_RUNS", runs)
    monkeypatch.setattr(subject, "preflight", lambda: pytest.fail("Docker queried"))
    monkeypatch.setattr(subject, "exercise", lambda *a, **kw: pytest.fail("Fixture used"))
    monkeypatch.setattr(sys, "argv", ["run_native_topology.py", "--fixture-root", str(fixture),
                                        "--image", "image", "--skip-build", "--retain-image",
                                        "--run-id", run_id, "--vm-profile", "ha-ops-dev",
                                        "--platform", "linux/arm64"])
    with pytest.raises(SystemExit) as error:
        subject.main()
    assert error.value.code == 2


@pytest.mark.parametrize("replacement", ["root", "homeassistant", "config"])
def test_reusable_topology_rejects_fixture_swap_during_preflight(tmp_path, monkeypatch, replacement):
    subject = topology_runner()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_id = "b" * 32
    fixture = runs / run_id
    outside = tmp_path / "outside"
    for root in (fixture, outside):
        (root / "addon_configs" / "fixture_ha_ops").mkdir(parents=True)
        for name in ("homeassistant", "data", "backup"):
            (root / name).mkdir()
    monkeypatch.setattr(subject, "LOCAL_RUNS", runs)
    def swapped_preflight():
        if replacement == "root":
            fixture.rename(runs / "held-root")
            fixture.symlink_to(outside, target_is_directory=True)
        else:
            relative = ("homeassistant",) if replacement == "homeassistant" else ("addon_configs", "fixture_ha_ops")
            target = fixture.joinpath(*relative)
            target.rename(target.with_name("held-" + target.name))
            target.symlink_to(outside.joinpath(*relative), target_is_directory=True)
        return {"engine": "test", "os": "linux"}
    monkeypatch.setattr(subject, "preflight", swapped_preflight)
    monkeypatch.setattr(subject, "present", lambda *a: pytest.fail("Docker queried after fixture swap"))
    monkeypatch.setattr(subject, "exercise", lambda *a, **kw: pytest.fail("Fixture used after swap"))
    monkeypatch.setattr(sys, "argv", ["run_native_topology.py", "--fixture-root", str(fixture),
                                        "--image", "image", "--skip-build", "--retain-image",
                                        "--run-id", run_id, "--vm-profile", "ha-ops-dev",
                                        "--platform", "linux/arm64"])
    assert subject.main() == 1
    assert not list(outside.glob("**/.vm-source-*"))


def test_topology_timeout_kills_child_process_group(monkeypatch):
    subject = wrapper()
    killed = []
    class Child:
        pid = 4312
        def __init__(self):
            self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            return (b"", b"")
    child = Child()
    monkeypatch.setattr(subject.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(subject.os, "killpg", lambda pid, signal: killed.append((pid, signal)))
    with pytest.raises(subprocess.TimeoutExpired):
        subject.invoke_child(["python3", "child.py"], env={}, timeout=1)
    assert killed == [(4312, subject.signal.SIGKILL)]
    assert child.calls == 2


def test_platform_override_is_removed_and_image_arch_checked(monkeypatch):
    subject = wrapper()
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    assert "DOCKER_DEFAULT_PLATFORM" not in subject.docker_env()
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: completed(b"linux/amd64\n"))
    with pytest.raises(subject.Refused, match="architecture"):
        subject.verify_image_arch("image", "linux/arm64")


def test_homebrew_pin_mismatch_refuses_before_install(monkeypatch):
    subject = wrapper()
    subject.TOOLCHAIN.update(pinned_toolchain())
    monkeypatch.setattr(subject.shutil, "which", lambda name: "/bin/brew" if name == "brew" else None)
    calls = []
    def fake_invoke(args, **kwargs):
        calls.append(args)
        return completed(json.dumps({"formulae": [{"name": "colima", "versions": {"stable": "9.9.9"}},
                                              {"name": "docker", "versions": {"stable": "29.8.2"}}]}).encode())
    monkeypatch.setattr(subject, "invoke", fake_invoke)
    with pytest.raises(subject.Refused, match="no install attempted"):
        subject.installed()
    assert calls == [["brew", "info", "--json=v2", "colima", "docker"]]


def test_exact_cleanup_attempts_every_owned_name_after_first_failure(tmp_path, monkeypatch):
    subject = wrapper()
    run_id = "a" * 32
    fixture = tmp_path / run_id
    fixture.mkdir()
    calls = []
    def fake_remove(name, owner):
        calls.append((name, owner))
        if name == "ha-ops-dev-" + run_id:
            raise subject.Refused("first removal failed")
        if name.endswith("-second"):
            raise subject.Refused("foreign owner")
    monkeypatch.setattr(subject, "remove_owned", fake_remove)
    with pytest.raises(subject.Refused, match="first removal failed.*foreign owner"):
        subject.cleanup_run(run_id, fixture)
    assert calls == [("ha-ops-dev-" + run_id, run_id),
                     ("native-topology-" + run_id + "-first", run_id),
                     ("native-topology-" + run_id + "-second", run_id)]
    assert fixture.is_dir()


def test_cli_cleanup_uses_aggregate_exact_run_cleanup(tmp_path, monkeypatch):
    subject = wrapper()
    run_id = "b" * 32
    monkeypatch.setattr(subject, "RUNS", tmp_path)
    monkeypatch.setattr(subject, "endpoint_info", lambda: {})
    called = []
    monkeypatch.setattr(subject, "cleanup_run", lambda owner, fixture: called.append((owner, fixture)))
    monkeypatch.setattr(sys, "argv", ["ha_ops_dev.py", "cleanup", "--run", run_id])
    assert subject.main() == 0
    assert called == [(run_id, tmp_path / run_id)]


def test_homebrew_install_runs_only_after_matching_pin(monkeypatch):
    subject = wrapper()
    subject.TOOLCHAIN.update(pinned_toolchain())
    monkeypatch.setattr(subject.shutil, "which", lambda name: "/bin/brew" if name == "brew" else None)
    calls = []
    available = {"formulae": [{"name": "colima", "versions": {"stable": subject.TOOLCHAIN["colima"]}},
                              {"name": "docker", "versions": {"stable": subject.TOOLCHAIN["docker_cli"]}}]}
    def fake_invoke(args, **kwargs):
        calls.append(args)
        if args[:2] == ["brew", "info"]:
            return completed(json.dumps(available).encode())
        return completed(b"Docker version 29.8.2")
    monkeypatch.setattr(subject, "invoke", fake_invoke)
    monkeypatch.setattr(subject, "colima", lambda *a: b"colima version 0.10.3")
    subject.installed()
    assert calls[0][:2] == ["brew", "info"]
    assert calls[1] == ["brew", "install", "colima", "docker"]


def test_workflow_covers_wrapper_and_test_changes():
    import re

    workflow = (SOURCE.parents[2] / ".github/workflows/local-virtualization.yml").read_text()
    for event in ("push", "pull_request"):
        section = re.search(rf"^  {event}:\n(.*?)(?=^  [a-z_]+:|^jobs:|\Z)",
                            workflow, re.M | re.S).group(1)
        paths = re.findall(r"^      - '([^']+)'$", section, re.M)
        assert "dev/virtualization/**" in paths, event
        assert "ha-ops/tests/test_local_virtualization.py" in paths, event
        assert "ha-ops/tests/integration/fixture_cleanup.py" in paths, event
    assert "python3 -m pytest -q ha-ops/tests/test_local_virtualization.py" in workflow


def test_guest_git_package_is_pinned_to_toolchain():
    subject = wrapper()
    dockerfile = (SOURCE.parent / "Dockerfile").read_text()
    assert f'ARG GIT_DEB_VERSION={pinned_toolchain()["guest_git_debian"]}' in dockerfile
    assert '"git=${GIT_DEB_VERSION}"' in dockerfile


def topology_fixture(root):
    (root / "addon_configs" / "fixture_ha_ops").mkdir(parents=True)
    for name in ("homeassistant", "data", "backup"):
        (root / name).mkdir()


@pytest.mark.parametrize("skip_build", [True, False])
def test_topology_context_exists_only_when_building(tmp_path, monkeypatch, skip_build):
    subject = topology_runner()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    original_mkdir = Path.mkdir
    original_copy = subject.shutil.copyfile
    copied = []
    def checked_mkdir(path, *args, **kwargs):
        if path.name == "build-context" and skip_build:
            pytest.fail("skip-build created build context")
        return original_mkdir(path, *args, **kwargs)
    def checked_copy(source, target):
        if skip_build:
            pytest.fail("skip-build copied context input")
        copied.append(Path(target).name)
        return original_copy(source, target)
    monkeypatch.setattr(Path, "mkdir", checked_mkdir)
    monkeypatch.setattr(subject.shutil, "copyfile", checked_copy)
    calls = []
    identities = iter((b"first-id", b"second-id"))
    def fake_docker(args, **kwargs):
        calls.append(args)
        if args[0] == "build":
            assert not skip_build
            assert {path.name for path in Path(args[-1]).iterdir()} == {
                "native_topology_helpers.py", "container_probe.py", "Dockerfile"}
        if args[0] == "run":
            return next(identities)
        if args[0] == "exec":
            return b"{}"
        return b""
    monkeypatch.setattr(subject, "docker", fake_docker)
    monkeypatch.setattr(subject, "present", lambda *a: False)
    monkeypatch.setattr(subject, "check_mounts", lambda *a: None)
    cleaned = []
    monkeypatch.setattr(subject, "cleanup", lambda *a, **kw: cleaned.append(a))
    assert subject.exercise(fixture, "image", "first", "second", skip_build=skip_build)["distinct_containers"]
    assert len(copied) == (0 if skip_build else 3)
    assert any(args[0] == "build" for args in calls) is (not skip_build)
    assert len(cleaned) == 1


def test_held_cleanup_nested_tree_symlink_and_descriptor_ownership(tmp_path):
    subject = wrapper()
    root = tmp_path / "owned"
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "file").write_text("owned")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("preserve")
    (root / "link").symlink_to(outside, target_is_directory=True)
    owned = subject.OwnedDirectory(root)
    handles = (owned.root, owned.parent)
    try:
        owned.remove()
        assert not root.exists()
        assert (outside / "sentinel").read_text() == "preserve"
        assert all(os.fstat(handle) for handle in handles)
        owned.remove(missing_ok=True)
    finally:
        owned.close()
    owned.close()
    for handle in handles:
        with pytest.raises(OSError):
            os.fstat(handle)


@pytest.mark.parametrize("replacement", ["directory", "symlink", "missing"])
def test_held_cleanup_refuses_replaced_or_renamed_root(tmp_path, replacement):
    subject = wrapper()
    root = tmp_path / "owned"
    root.mkdir()
    (root / "evidence").write_text("original")
    owned = subject.OwnedDirectory(root)
    original = tmp_path / "renamed"
    root.rename(original)
    outside = tmp_path / "replacement"
    outside.mkdir()
    (outside / "sentinel").write_text("preserve")
    if replacement == "directory":
        outside.rename(root)
        outside = root
    elif replacement == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises((RuntimeError, FileNotFoundError)):
            owned.remove(missing_ok=True)
        assert (outside / "sentinel").read_text() == "preserve"
        assert (original / "evidence").read_text() == "original"
    finally:
        owned.close()


@pytest.mark.parametrize("outcome", ["success", "failure", "cleanup_failure", "swap_success", "swap_failure"])
def test_reusable_main_uses_held_cleanup_in_both_branches(tmp_path, monkeypatch, outcome):
    subject = topology_runner()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_id = "c" * 32
    root = runs / run_id
    topology_fixture(root)
    monkeypatch.setattr(subject, "LOCAL_RUNS", runs)
    monkeypatch.setattr(subject, "preflight", lambda: {"engine": "test", "os": "linux"})
    monkeypatch.setattr(subject, "present", lambda *a: False)
    closed = []
    actual_close = subject.FixtureGuard.close
    def counted_close(guard):
        closed.extend([guard.runs, *guard.handles.values()])
        actual_close(guard)
    monkeypatch.setattr(subject.FixtureGuard, "close", counted_close)
    def fake_exercise(directory, *a, **kw):
        (directory / "evidence").write_text("owned")
        if outcome.startswith("swap"):
            directory.rename(runs / "original")
            directory.mkdir()
            (directory / "sentinel").write_text("replacement")
        if outcome == "cleanup_failure":
            raise subject.CleanupFailure("container remains")
        if outcome.endswith("failure"):
            raise RuntimeError("exercise failed")
        return {}
    monkeypatch.setattr(subject, "exercise", fake_exercise)
    monkeypatch.setattr(sys, "argv", ["run_native_topology.py", "--fixture-root", str(root),
                                     "--image", "image", "--skip-build", "--retain-image",
                                     "--run-id", run_id, "--vm-profile", "ha-ops-dev", "--platform", "linux/arm64"])
    assert subject.main() == (0 if outcome == "success" else 1)
    if outcome.startswith("swap"):
        assert (root / "sentinel").read_text() == "replacement"
    elif outcome == "cleanup_failure":
        assert (root / "evidence").read_text() == "owned"
    else:
        assert not root.exists()
    assert len(closed) == 7 and len(set(closed)) == 7
    for handle in closed:
        with pytest.raises(OSError):
            os.fstat(handle)


@pytest.mark.parametrize("operation", ["generic", "topology_setup", "topology_child", "probe"])
def test_parent_cleanup_keeps_creation_identity_on_failure(tmp_path, monkeypatch, operation):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "input").write_text("fixture")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "diagnostics")
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "ready", lambda *a, **kw: {})
    monkeypatch.setattr(subject, "remove_owned", lambda *a: None)
    sentinel = []
    def replace(root):
        root.rename(subject.RUNS / "original")
        root.mkdir()
        marker = root / "sentinel"
        marker.write_text("replacement")
        sentinel.append(marker)
        raise subject.Refused("injected failure")
    def setup(*, exact_sources):
        if operation == "topology_child":
            return {"image": "image", "platform": "linux/arm64"}
        replace(exact_sources[0].parent)
    monkeypatch.setattr(subject, "setup", setup)
    monkeypatch.setattr(subject, "invoke_child", lambda *a, **kw: replace(next(path for path in subject.RUNS.iterdir() if path.is_dir())))
    monkeypatch.setattr(subject, "verify_source", replace)
    with pytest.raises(RuntimeError):
        if operation == "generic":
            subject.generic_run(Path("input"), ["true"])
        elif operation == "probe":
            subject.share_probe()
        else:
            report = tmp_path / "report.json"
            subject.topology(report)
    assert len(sentinel) == 1 and sentinel[0].read_text() == "replacement"
    if operation.startswith("topology"):
        assert json.loads(report.read_text())["status"] == "failed"


def test_parent_accepts_child_removal_but_refuses_new_root(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, "remove_owned", lambda *a: None)
    root = tmp_path / ("d" * 32)
    root.mkdir()
    owned = subject.OwnedDirectory(root)
    try:
        root.rmdir()
        subject.cleanup_run(root.name, root, owned)
        root.mkdir()
        (root / "sentinel").write_text("replacement")
        with pytest.raises(RuntimeError, match="changed"):
            subject.cleanup_run(root.name, root, owned)
        assert (root / "sentinel").read_text() == "replacement"
    finally:
        owned.close()


def test_generic_cleanup_failure_preserves_fixture_and_closes_anchor(tmp_path, monkeypatch):
    subject = wrapper()
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "input").write_text("fixture")
    monkeypatch.setattr(subject, "ROOT", repository)
    monkeypatch.setattr(subject, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(subject, "capacity", lambda *a: 40 * subject.GIB)
    monkeypatch.setattr(subject, "setup", lambda **kw: {"image": "image", "platform": "linux/arm64"})
    monkeypatch.setattr(subject, "docker", lambda *a, **kw: (_ for _ in ()).throw(subject.Refused("create failed")))
    monkeypatch.setattr(subject, "remove_owned", lambda *a: (_ for _ in ()).throw(subject.Refused("remove failed")))
    captured = []
    actual = subject.OwnedDirectory
    def capture(path):
        owned = actual(path)
        captured.append(owned)
        return owned
    monkeypatch.setattr(subject, "OwnedDirectory", capture)
    monkeypatch.setattr(subject, "DIAGNOSTICS", tmp_path / "evidence")
    with pytest.raises(subject.Refused, match="create failed"):
        subject.generic_run(Path("input"), ["true"])
    manifest = json.loads(next(subject.DIAGNOSTICS.glob("run-*/manifest.json")).read_text())
    assert manifest["failure"] == "Refused"
    assert manifest["status"] == "failed"
    assert manifest["primary_error"] == "create failed"
    assert any("container cleanup" in error and "remove failed" in error for error in manifest["cleanup_errors"])
    assert len(list(subject.RUNS.iterdir())) == 1
    assert all(owned.root is None for owned in captured)


@pytest.mark.parametrize("failure", ["removal", "inspection"])
def test_wrapper_checks_owned_container_absence_after_removal_error(monkeypatch, failure):
    subject = wrapper()
    calls = []
    def inspect(name, run_id):
        calls.append(("inspect", name))
        if name == "fixture-name":
            return "immutable-id"
        if failure == "inspection":
            raise subject.Refused("inspection failed")
        return None
    def remove(*args, **kwargs):
        calls.append(tuple(args))
        if failure == "removal":
            raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(subject, "owned_container", inspect)
    monkeypatch.setattr(subject, "docker", remove)
    with pytest.raises(subject.Refused, match="cleanup failed"):
        subject.remove_owned("fixture-name", "fixture-run")
    assert calls == [("inspect", "fixture-name"), ("rm", "--force", "immutable-id"), ("inspect", "immutable-id")]


@pytest.mark.parametrize("failure", ["cleanup", "ordinary", "success"])
def test_default_topology_fixture_retained_only_for_uncertain_cleanup(tmp_path, monkeypatch, capsys, failure):
    subject = topology_runner()
    fixture = tmp_path / "owned-fixture"
    fixture.mkdir()
    monkeypatch.setattr(subject.tempfile, "mkdtemp", lambda **kw: str(fixture))
    monkeypatch.setattr(subject, "preflight", lambda: {"engine": "test", "os": "linux"})
    monkeypatch.setattr(subject, "present", lambda *a: False)
    def exercise(directory, *a, **kw):
        (Path(directory) / "evidence").write_text("left for inspection")
        if failure == "cleanup":
            raise subject.CleanupFailure("container absence unconfirmed")
        if failure == "ordinary":
            raise RuntimeError("proof failed after clean resources")
        return {}
    monkeypatch.setattr(subject, "exercise", exercise)
    monkeypatch.setattr(sys, "argv", ["run_native_topology.py"])
    assert subject.main() == (0 if failure == "success" else 1)
    assert fixture.exists() == (failure == "cleanup")
    if failure == "cleanup":
        assert (fixture / "evidence").read_text() == "left for inspection"
        assert str(fixture) in capsys.readouterr().err


def ext4_helper():
    spec = importlib.util.spec_from_file_location('ext4_fixture', SOURCE.parent / 'ext4_fixture.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def ext4_inspection():
    expected = {'/workspace/input': ('/input', False), '/workspace/output': ('/output', True), '/workspace/ext4': ('/guest/mount', True)}
    return {'Image': 'imageid', 'Id': 'container', 'Config': {'User': '501:1000', 'Labels': {'ha-ops.dev.run': 'a' * 32}},
            'HostConfig': {'Privileged': False, 'Devices': [], 'CapAdd': [], 'CapDrop': ['ALL'],
                           'SecurityOpt': ['no-new-privileges'], 'ReadonlyRootfs': True, 'NetworkMode': 'none'},
            'Mounts': [{'Type': 'bind', 'Destination': k, 'Source': v[0], 'RW': v[1], 'Propagation': 'rprivate'} for k, v in expected.items()]}


@pytest.mark.parametrize('fault', ['owner', 'root', 'privileged', 'device', 'capadd', 'capdrop', 'nnp', 'readonly', 'network', 'source', 'rw', 'propagation', 'duplicate'])
def test_ext4_container_requires_exact_mounts_and_unprivileged_boundary(fault):
    subject = wrapper(); inspected = ext4_inspection()
    subject.verify_ext4_container(inspected, 'container', 'a' * 32, Path('/input'), Path('/output'), {'mount_path': '/guest/mount'}, '501:1000')
    if fault == 'owner': inspected['Config']['Labels']['ha-ops.dev.run'] = 'b' * 32
    elif fault == 'root': inspected['Config']['User'] = '0:1000'
    elif fault == 'privileged': inspected['HostConfig']['Privileged'] = True
    elif fault == 'device': inspected['HostConfig']['Devices'] = [{'PathOnHost': '/dev/loop0'}]
    elif fault == 'capadd': inspected['HostConfig']['CapAdd'] = ['SYS_ADMIN']
    elif fault == 'capdrop': inspected['HostConfig']['CapDrop'] = []
    elif fault == 'nnp': inspected['HostConfig']['SecurityOpt'] = []
    elif fault == 'readonly': inspected['HostConfig']['ReadonlyRootfs'] = False
    elif fault == 'network': inspected['HostConfig']['NetworkMode'] = 'default'
    elif fault == 'source': inspected['Mounts'][2]['Source'] = '/foreign'
    elif fault == 'rw': inspected['Mounts'][2]['RW'] = False
    elif fault == 'propagation': inspected['Mounts'][2]['Propagation'] = 'shared'
    elif fault == 'duplicate': inspected['Mounts'].append(inspected['Mounts'][0])
    with pytest.raises(subject.Refused): subject.verify_ext4_container(inspected, 'container', 'a' * 32, Path('/input'), Path('/output'), {'mount_path': '/guest/mount'}, '501:1000')


@pytest.mark.parametrize('run', ['../image', 'a' * 31, 'a' * 33, 'A' * 32, '/dev/loop0'])
def test_ext4_helper_rejects_hostile_run_before_guest_execution(run, monkeypatch):
    subject = wrapper(); monkeypatch.setattr(subject, 'invoke', lambda *a, **k: pytest.fail('guest invoked'))
    with pytest.raises(subject.Refused): subject.ext4_guest('cleanup', run)


@pytest.mark.parametrize('fault', ['bytes', 'inodes'])
def test_ext4_backing_capacity_checks_actual_parent(fault, tmp_path, monkeypatch):
    from types import SimpleNamespace
    subject = ext4_helper()
    monkeypatch.setattr(subject.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=1 if fault == 'bytes' else 10**9, f_frsize=4096, f_favail=1 if fault == 'inodes' else 100))
    with pytest.raises(subject.Refused, match='backing-parent'): subject.capacity(tmp_path, 256 * 1024**2)


@pytest.mark.parametrize('fault', ['inode', 'device', 'back-file', 'loop-name'])
def test_ext4_loop_identity_rejects_substitution(fault):
    subject = ext4_helper()
    info = {'identity': [os.makedev(8, 1), 123, 0o100600, 1]}
    row = {'name': '/dev/loop3', 'back-file': '/owned/image', 'back-ino': 123, 'back-maj:min': '8:1', 'maj:min': '7:3'}
    subject.verify_loop(row, Path('/owned/image'), info)
    if fault == 'inode': row['back-ino'] = 456
    elif fault == 'device': row['back-maj:min'] = '8:2'
    elif fault == 'back-file': row['back-file'] = '/foreign/image'
    else: row['name'] = '/dev/sda'
    with pytest.raises(subject.Refused): subject.verify_loop(row, Path('/owned/image'), info)


@pytest.mark.parametrize('fault', ['source', 'device', 'filesystem', 'root', 'options', 'quota'])
def test_ext4_actual_mount_profile_refuses_wrong_route(fault):
    subject = ext4_helper(); loop = {'name': '/dev/loop3', 'maj:min': '7:3'}
    mount = {'source': '/dev/loop3', 'device': '7:3', 'root': '/', 'filesystem': 'ext4', 'options': 'rw,nosuid,nodev,noexec', 'super_options': 'rw,data=ordered'}
    subject.verify_mount(mount, loop)
    if fault == 'source': mount['source'] = '/dev/loop4'
    elif fault == 'device': mount['device'] = '7:4'
    elif fault == 'filesystem': mount['filesystem'] = 'virtiofs'
    elif fault == 'root': mount['root'] = '/sub'
    elif fault == 'options': mount['options'] = 'ro,nodev,nosuid,noexec'
    else: mount['super_options'] += ',quota'
    with pytest.raises(subject.Refused): subject.verify_mount(mount, loop)


def test_ext4_cleanup_backing_removal_intent_survives_unlink_interruption(tmp_path, monkeypatch):
    subject = ext4_helper(); run = 'a' * 32; directory = tmp_path / run; directory.mkdir(mode=0o700)
    image = directory / 'image'; image.write_bytes(b'owned')
    mount = directory / 'mount'; mount.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    data = {'schema': 1, 'run': run, 'profile': 'allocation', 'directory': subject.identity(os.fstat(fd)),
            'backing': {'identity': subject.identity(image.stat())}, 'mount_directory': subject.identity(mount.stat()), 'stage': 'mounted'}
    subject.save(fd, data); monkeypatch.setattr(subject, 'BASE', tmp_path)
    def backing(path, expected=None, size=None):
        s = path.stat()
        assert expected == subject.identity(s)
        return {'identity': subject.identity(s)}
    monkeypatch.setattr(subject, 'backing', backing); monkeypatch.setattr(subject, 'loops', lambda p: [])
    monkeypatch.setattr(subject, 'mounts', lambda p: [])
    original = subject.os.unlink
    def interrupted(name, **kwargs):
        original(name, **kwargs)
        if name == 'image': raise OSError('interrupted after exact image unlink')
    monkeypatch.setattr(subject.os, 'unlink', interrupted)
    try:
        with pytest.raises(OSError): subject.cleanup(fd, data)
        assert json.loads((directory / 'manifest.json').read_text())['stage'] == 'backing-removal-pending'
        monkeypatch.setattr(subject.os, 'unlink', original)
        result = subject.cleanup(fd, json.loads((directory / 'manifest.json').read_text()))
        assert result['status'] == 'absent' and not image.exists()
    finally: os.close(fd)


def test_ext4_cleanup_unexplained_backing_loss_refuses(tmp_path, monkeypatch):
    subject = ext4_helper(); directory = tmp_path / ('a' * 32); directory.mkdir(); fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(subject, 'BASE', tmp_path)
    monkeypatch.setattr(subject, 'backing', lambda *a: (_ for _ in ()).throw(FileNotFoundError()))
    try:
        with pytest.raises(subject.Refused, match='disappeared'): subject.cleanup(fd, {'run': 'a' * 32, 'backing': {'identity': [1, 2]}, 'stage': 'mounted'})
    finally: os.close(fd)


def test_ext4_fixture_uses_nondiscard_preallocation_and_no_broad_cleanup():
    source = (SOURCE.parent / 'ext4_fixture.py').read_text()
    assert "call('fallocate', '-l'" in source and 'nodiscard,lazy_itable_init=0,lazy_journal_init=0' in source
    assert "s.st_blocks * 512 < size" in source
    assert 'loop -D' not in source and "'--detach', loop['name']" in source
    assert "call('umount', str(target))" in source and 'shutil.rmtree' not in source


def test_ext4_directory_identity_survives_owned_child_creation(tmp_path):
    subject = ext4_helper(); before = subject.identity(tmp_path.stat())
    (tmp_path / 'mount').mkdir()
    assert subject.identity(tmp_path.stat()) == before
    assert len(before) == 3


@pytest.mark.parametrize('missing', ['fallocate', 'mkfs.ext4', 'losetup', 'mount', 'umount', 'dumpe2fs', 'blkid'])
def test_ext4_guest_missing_tool_refuses_before_backing_mutation(missing, tmp_path, monkeypatch):
    subject = ext4_helper(); monkeypatch.setattr(subject, 'BASE', tmp_path / 'absent')
    monkeypatch.setattr(subject.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(subject.shutil, 'which', lambda name: None if name == missing else '/usr/bin/' + name)
    monkeypatch.setattr(sys, 'argv', ['ext4_fixture', 'setup', '--run', 'a' * 32])
    with pytest.raises(subject.Refused, match='unavailable'): subject.main()
    assert not subject.BASE.exists()


@pytest.mark.parametrize('boundary', ['fallocate', 'mkfs.ext4', 'losetup', 'mount'])
def test_ext4_setup_failure_retains_precise_partial_manifest(boundary, tmp_path, monkeypatch):
    subject = ext4_helper(); run = 'a' * 32; path = tmp_path / run; path.mkdir(mode=0o700)
    monkeypatch.setattr(subject, 'BASE', tmp_path); monkeypatch.setattr(subject, 'capacity', lambda *a: {'available_bytes': 10**10})
    monkeypatch.setattr(subject, 'backing', lambda path, expected=None, size=None: {'identity': subject.identity(path.stat()), 'size': size or 0, 'allocated': size or 0})
    attached = []
    def call(*args):
        if args[0] == boundary: raise subject.Refused('injected ' + boundary)
        if args[0] == 'losetup':
            s = (path / 'image').stat()
            attached.append({'name': '/dev/loop3', 'maj:min': '7:3', 'back-file': str(path / 'image'), 'back-ino': s.st_ino,
                             'back-maj:min': '%d:%d' % (os.major(s.st_dev), os.minor(s.st_dev))})
        return ''
    monkeypatch.setattr(subject, 'call', call); monkeypatch.setattr(subject, 'loops', lambda image: attached)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(subject.Refused, match='injected'): subject.setup(fd, run, 'allocation', 501, 1000)
        record = json.loads((path / 'manifest.json').read_text())
        assert record['run'] == run and record['backing']['identity'] == subject.identity((path / 'image').stat())
        expected = {'fallocate': 'directory', 'mkfs.ext4': 'reserved', 'losetup': 'formatted', 'mount': 'attached'}
        assert record['stage'] == expected[boundary]
        if boundary == 'mount': assert record['loop'] == attached[0] and 'mount_directory' in record
    finally: os.close(fd)


@pytest.mark.parametrize('boundary', ['setup', 'create', 'start', 'wait', 'security', 'container-cleanup', 'guest-cleanup', 'report', 'timeout', 'incomplete-matrix', 'missing-second-report', 'stale-second-report', 'fresh'])
def test_ext4_failures_never_publish_success_and_preserve_cleanup_order(boundary, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)
    subject = wrapper(); run_dirs = tmp_path / 'runs'; run_dirs.mkdir(); events = []; reports = []
    monkeypatch.setattr(subject, 'ROOT', tmp_path); monkeypatch.setattr(subject, 'HERE', tmp_path); monkeypatch.setattr(subject, 'RUNS', run_dirs)
    monkeypatch.setattr(subject, 'DIAGNOSTICS', tmp_path / 'diagnostics')
    monkeypatch.setattr(subject, 'snapshot_input', lambda p: {'total': 10})
    monkeypatch.setattr(subject, 'copy_input', lambda src, dst, **k: dst.mkdir())
    monkeypatch.setattr(subject, 'capacity', lambda *a: 10**10)
    monkeypatch.setattr(subject, 'make_context', lambda: b''); monkeypatch.setattr(subject, 'load_toolchain', lambda c: None)
    monkeypatch.setattr(subject, 'installed', lambda: None); monkeypatch.setattr(subject, 'ready', lambda: {})
    monkeypatch.setattr(subject, 'verify_source', lambda p: None); monkeypatch.setattr(subject, 'guest_platform', lambda: 'linux/arm64')
    monkeypatch.setattr(subject, 'fingerprint', lambda c: 'fixture'); monkeypatch.setattr(subject, 'image_present', lambda i: True)
    monkeypatch.setattr(subject, 'verify_image_arch', lambda *a: None); monkeypatch.setattr(subject, 'endpoint_info', lambda: {'Name': 'ha-ops-dev'})
    monkeypatch.setattr(subject.shutil, 'which', lambda name: '/usr/bin/' + name)
    fixture = {'mount_path': '/guest/mount', 'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}}
    def guest(action, run, profile):
        events.append('guest-' + action)
        if action == boundary or boundary == 'guest-cleanup' and action == 'cleanup': raise subject.Refused('primary ' + boundary)
        return fixture if action in {'setup', 'observe'} else {'status': 'absent'}
    monkeypatch.setattr(subject, 'ext4_guest', guest)
    run = []; environment = {}; past = []
    def docker(*args, **kw):
        events.append('docker-' + args[0])
        if args[0] == boundary: raise subject.Refused('primary ' + boundary)
        if boundary == 'timeout' and args[0] == 'wait': raise subprocess.TimeoutExpired('exact owned wait', 1)
        if args[0] == 'create':
            run.append(args[args.index('--label') + 1].split('=')[1])
            environment.update(value.split('=', 1) for i, value in enumerate(args) if i and args[i - 1] == '--env')
            return completed(b'container')
        if args[0] == 'container':
            info = ext4_inspection(); info['Config']['Labels']['ha-ops.dev.run'] = run[0]
            info['Config']['User'] = str(os.getuid()) + ':' + str(os.getgid())
            current = next(run_dirs.iterdir())
            for row in info['Mounts']:
                if row['Destination'] == '/workspace/input': row['Source'] = str(current / 'input')
                if row['Destination'] == '/workspace/output': row['Source'] = str(current / 'output')
            if boundary == 'security': info['HostConfig']['Privileged'] = True
            return completed(json.dumps([info]).encode())
        if args[0] == 'wait':
            current = next(run_dirs.iterdir())
            attempt = int(environment['HA_OPS_EXT4_ATTEMPT'])
            proof = complete_ext4_proof(run[0], attempt=attempt, token=environment['HA_OPS_EXT4_TOKEN'])
            if boundary == 'incomplete-matrix': proof['rows'] = {}
            if attempt and boundary == 'missing-second-report': return completed(b'0')
            if attempt and boundary == 'stale-second-report': proof = past[0]
            (current / 'output' / Path(environment['HA_OPS_EXT4_REPORT']).name).write_text(json.dumps(proof)); past.append(proof)
            return completed(b'0')
        if args[0] == 'image': return completed(b'imageid')
        return completed()
    monkeypatch.setattr(subject, 'docker', docker)
    def remove(*args):
        events.append('container-absence')
        if boundary == 'container-cleanup': raise subject.Refused('container absent unverified')
    monkeypatch.setattr(subject, 'remove_owned', remove)
    def report(path, value):
        reports.append(value)
        if boundary == 'report': raise OSError('report write failed')
    monkeypatch.setattr(subject, 'write_report', report)
    recreate = boundary in {'fresh', 'missing-second-report', 'stale-second-report'}
    if boundary == 'fresh':
        assert subject.ext4_run(Path('.'), ['fixture'], report=tmp_path / 'report.json', recreate=True) == 0
        assert reports[-1]['status'] == 'passed' and [p['attempt'] for p in reports[-1]['proofs']] == [0, 1]
        assert reports[-1]['proofs'][0]['attempt_token'] != reports[-1]['proofs'][1]['attempt_token']
        return
    with pytest.raises(subject.Refused): subject.ext4_run(Path('.'), ['fixture'], report=tmp_path / 'report.json', recreate=recreate)
    if boundary == 'container-cleanup': assert 'guest-cleanup' not in events
    elif 'guest-setup' in events: assert 'guest-cleanup' in events
    if boundary not in {'setup', 'report'}: assert reports[-1]['status'] == 'failed'
    if 'guest-cleanup' in events and 'container-absence' in events: assert events.index('container-absence') < events.index('guest-cleanup')


@pytest.mark.parametrize('fault', ['sparse', 'wrong-size', 'hardlink', 'wrong-owner', 'special', 'substitution'])
def test_ext4_backing_requires_exact_reserved_owned_inode(fault, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    subject = ext4_helper(); image = tmp_path / 'image'; image.write_bytes(b'fixture')
    s = SimpleNamespace(st_dev=8, st_ino=12, st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=0,
                        st_size=256 * 1024**2, st_blocks=256 * 1024**2 // 512)
    expected = subject.identity(s)
    if fault == 'sparse': s.st_blocks = 0
    elif fault == 'wrong-size': s.st_size -= 1
    elif fault == 'hardlink': s.st_nlink = 2
    elif fault == 'wrong-owner': s.st_uid = 501
    elif fault == 'special': s.st_mode = stat.S_IFBLK | 0o600
    else: s.st_ino += 1
    monkeypatch.setattr(subject.os, 'fstat', lambda fd: s)
    with pytest.raises(subject.Refused): subject.backing(image, expected, 256 * 1024**2)


def test_ext4_backing_never_follows_symlink(tmp_path):
    subject = ext4_helper(); image = tmp_path / 'image'; image.symlink_to(tmp_path / 'foreign')
    with pytest.raises(OSError): subject.backing(image)


@pytest.mark.parametrize('boundary', ['manifest-unlink', 'run-rmdir'])
def test_ext4_final_cleanup_receipt_recovers_interrupted_empty_directory(boundary, tmp_path, monkeypatch):
    from types import SimpleNamespace
    subject = ext4_helper(); run = 'a' * 32; path = tmp_path / run; path.mkdir(mode=0o700)
    original_fstat = os.fstat
    def root_stat(fd):
        s = original_fstat(fd)
        fields = {name: getattr(s, name) for name in dir(s) if name.startswith('st_')}
        fields['st_uid'] = 0
        return SimpleNamespace(**fields)
    monkeypatch.setattr(subject.os, 'fstat', root_stat); monkeypatch.setattr(subject.os, 'geteuid', lambda: 0)
    original_lstat = Path.lstat
    def guest_ancestors(path):
        if str(path) in {'/var', '/var/lib'}: return SimpleNamespace(st_mode=0o40755, st_uid=0)
        return original_lstat(path)
    monkeypatch.setattr(subject.Path, 'lstat', guest_ancestors)
    monkeypatch.setattr(subject, 'BASE', tmp_path); monkeypatch.setattr(subject.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(subject, 'loops', lambda p: []); monkeypatch.setattr(subject, 'mounts', lambda p: [])
    monkeypatch.setattr(sys, 'argv', ['ext4_fixture', 'cleanup', '--run', run])
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: subject.save(fd, {'schema': 1, 'run': run, 'profile': 'allocation', 'directory': subject.identity(os.fstat(fd)),
                          'stage': 'backing-removal-pending', 'backing': {'identity': [1, 2, 3, 1]}})
    finally: os.close(fd)
    original_unlink, original_rmdir = os.unlink, os.rmdir
    def interrupted_unlink(name, **kw):
        original_unlink(name, **kw)
        if name == 'manifest.json' and boundary == 'manifest-unlink': raise OSError('interrupted final unlink')
    def interrupted_rmdir(name, **kw):
        original_rmdir(name, **kw)
        if name == run and boundary == 'run-rmdir': raise OSError('interrupted final rmdir')
    monkeypatch.setattr(subject.os, 'unlink', interrupted_unlink); monkeypatch.setattr(subject.os, 'rmdir', interrupted_rmdir)
    with pytest.raises(OSError): subject.main()
    assert (tmp_path / (run + '.cleanup')).exists()
    monkeypatch.setattr(subject.os, 'unlink', original_unlink); monkeypatch.setattr(subject.os, 'rmdir', original_rmdir)
    subject.main(); subject.main()
    assert not path.exists() and not (tmp_path / (run + '.cleanup')).exists()


@pytest.mark.parametrize('checkpoint', range(1, 8))
def test_ext4_manifest_persistence_failures_keep_acknowledged_partial_ownership(checkpoint, tmp_path, monkeypatch):
    subject = ext4_helper(); run = 'a' * 32; path = tmp_path / run; path.mkdir(mode=0o700)
    monkeypatch.setattr(subject, 'BASE', tmp_path); monkeypatch.setattr(subject, 'capacity', lambda *a: {'available_bytes': 10**10})
    monkeypatch.setattr(subject, 'backing', lambda image, expected=None, size=None: {'identity': subject.identity(image.stat()), 'size': size or 0, 'allocated': size or 0})
    loop, mounted = [], []
    def call(*args):
        if args[0] == 'losetup':
            s = (path / 'image').stat()
            loop.append({'name': '/dev/loop3', 'maj:min': '7:3', 'back-file': str(path / 'image'), 'back-ino': s.st_ino,
                         'back-maj:min': '%d:%d' % (os.major(s.st_dev), os.minor(s.st_dev))})
        if args[0] == 'mount': mounted.append({'id': '42', 'source': '/dev/loop3', 'device': '7:3', 'root': '/', 'filesystem': 'ext4',
                                             'point': str(path / 'mount'), 'options': 'rw,nosuid,nodev,noexec', 'super_options': 'rw,data=ordered'})
        return ''
    monkeypatch.setattr(subject, 'call', call); monkeypatch.setattr(subject, 'loops', lambda image: loop); monkeypatch.setattr(subject, 'mounts', lambda target: mounted)
    original_save = subject.save; calls = []; acknowledged = []
    def save(fd, data):
        calls.append(data['stage'])
        if len(calls) == checkpoint: raise OSError('manifest persistence failure')
        original_save(fd, data); acknowledged.append(json.loads((path / 'manifest.json').read_text()))
    monkeypatch.setattr(subject, 'save', save); fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match='manifest persistence'): subject.setup(fd, run, 'allocation', 501, 1000)
        if acknowledged:
            current = json.loads((path / 'manifest.json').read_text())
            assert current == acknowledged[-1] and current['run'] == run
            if 'backing' in current: assert current['backing']['identity'] == subject.identity((path / 'image').stat())
            if 'loop' in current: assert current['loop'] == loop[0]
        else: assert not (path / 'manifest.json').exists() and not (path / 'image').exists()
    finally: os.close(fd)


def complete_ext4_proof(run, profile='allocation', attempt=0, token='c' * 32):
    """Portable complete report fixture; actual allocation evidence is separate."""
    def sample(family, outputs):
        allocations = []
        for output in outputs:
            name, logical = output if isinstance(output, tuple) else (output, 1)
            allocations.append({'output': name, 'logical': logical, 'allocated': ((logical + 4095) // 4096) * 2 * 4096 + 3 * 4096,
                                'inodes': 1, 'volume': [os.makedev(7, 3), '65787434', b'/dev/loop3'.hex()]})
        return {'family': family, 'logical_bytes': 4096, 'physical_bytes': 4096, 'new_inodes': 1,
                'blocks_delta': 4096, 'free_bytes_delta': 4096, 'free_inodes_delta': 1,
                'additional_peak': sum(a['allocated'] for a in allocations), 'required_inodes': len(allocations) + 2,
                'reserve': 4096, 'allocations': allocations}
    recreation = {'attempt': attempt, 'sentinel': {'device': os.makedev(7, 3), 'inode': 123, 'sha256': 'a' * 64}}
    rows = {'recreation': recreation}
    if profile == 'allocation':
        outputs = ['complete-index', 'index-lock-overlap', 'generated-tree:root', 'worktree-control:HEAD', 'commit-object',
                   'ref-atomic-output', 'ref-lock-overlap', 'full-reflog-copy-append', 'reflog-atomic-overlap']
        sizes = [0, 1, 4095, 4096, 4097, 1024**2] + [1] * 96
        outputs += [('stream-spool:' + str(i), size) for i, size in enumerate(sizes)]
        outputs += [('loose-blob:' + str(i), size + 32) for i, size in enumerate(sizes)]
        rows['loose-index-worktree-save'] = {'measurements': [sample(family, outputs) for family in
            ('loose-spool-fanout', 'complete-index-lock-trees', 'worktree-admin-materialization-save-ref-reflog')],
            'snapshot_count': 1, 'forecast_count': 1, 'phase_rescans': 0, 'selected_entries': 102, 'unselected_entries': 1,
            'existing_reflog': {**sample('existing-complete-reflog-copy-append-ref-lock', ['full-reflog-copy-append', 'reflog-atomic-overlap']), 'retained_log_bytes': 4096}}
        rows['controls'] = {'measurements': [sample('full-old-new-control-temp-diff', ['full-prospective-operation-state', 'full-pending-control', 'exact-diff-artifact'])],
                            'prior_control_retained_during_replace': True, 'full_pending_failure_envelope': True}
        rows['independent-copies'] = sample('two-independent-copies-metadata-replacement', ['independent-copy:first', 'independent-copy:second', 'exact-encoded-metadata:source', 'replacement-temporary:oid'])
        native_allocations=[[[os.makedev(7,3),'65787434',b'/dev/loop3'.hex()],name,1,4096,1] for name in ('recovery-copy','recovery-stage','recovery-control','recovery-forecast')]
        rows['independent-copies']['native_recovery']={'physical_peak':4096,'inode_peak':1,'additional_peak':16384,'required_inodes':6,'repeated_rollback':True,'discovery_only':True,
            'forecast':{'schema':1,'allocations':native_allocations,'volumes':[[native_allocations[0][0],16384,4096,6,1024**2,128]]}}
        rows['independent-copies']['native_recovery'].update(sample_boundaries=['control_written','marker_written','stage_durable','generation_durable','pointer_replaced','slot_durable','effect:replace'],sample_count=7,
            old_new_overlap=True,retained_baseline_physical=4096,overlap_total_physical=8192,
            process_crash=[{'direction':direction,'exit_code':73,'pending_before':True,'repeated_rollback':True,'target_device':os.makedev(7,3)} for direction in ('forward','rollback')])
        rows['repack'] = {**sample('all-object-kinds-old-pack-new-pack-index-full-temp', ['new-pack-old-packs-retained', 'pack-index', 'pack-temporary-overlap']),
                          'objects': 5, 'kinds': ['blob', 'tree', 'commit', 'tag'], 'unreachable': True,
                          'argv': ['pack-objects', '--window=0', '--depth=0', '--no-reuse-object', '--no-reuse-delta']}
        boundaries = ['artifact_written', 'artifact_fsynced', 'artifact_replaced', 'state_written', 'state_fsynced', 'state_replaced',
                      'artifact-fsync', 'state-fsync', 'state-directory-fsync-after-replace']
        rows['short-eio'] = {'positive_short_bytes': 8193, 'zero_progress_refused': True, 'copy_sink_eio_preserved': True,
                            'owner_persistence': [{'boundary': b, 'first_error_preserved': True, 'prior_revision': 0,
                                'published_revision': int(b in {'state_replaced', 'state-directory-fsync-after-replace'})} for b in boundaries],
                            'materialization_sink_eio': {'first_error_preserved': True, 'copy_blob_entered': True,
                                'selected_path': 'homeassistant/file', 'sink_path': '/fixture/tree/homeassistant/file',
                                'partial_bytes': 4096, 'expected_size': 1024**2, 'partial_sha256': 'a' * 64,
                                'reached_bytes': [65536], 'addressed_recovery': []}}
    elif profile == 'bytes':
        rows['byte-enospc'] = {'kernel_errno': 28, 'written_bytes': 1024**2, 'state_preserved': True, 'recovered_resources': [],
            'failures': [{'boundary': b, 'errno': 28, 'error': 'No space left on device'} for b in
                ('preview-payload', 'blob-payload', 'materialization', 'state-atomic-publication-after-durable-artifact')]}
        rows['byte-enospc']['native_recovery']={'active_generation_retained':True,'explicit_repeated_rollback':True,
            'failures':[{'boundary':name,'errno':28} for name in ('recovery-copy-preparation','target-volume-staging','before-effect-intent-control','rename-ack-control')],
            'cleanup':[{'generation':'a'*32,'status':'removed','errors':[]}]}
    else:
        rows['inode-enospc'] = {'kernel_errno': 28, 'state_preserved': True, 'recovered_resources': [],
                               'observed_total_inodes': 128, 'created_inodes': 63, 'remaining_bytes': 60 * 1024**2}
    return {'schema': 1, 'status': 'passed', 'run': run, 'profile': profile, 'attempt': attempt, 'attempt_token': token,
            'device': '7:3', 'filesystem': 'ext4', 'quantum': 4096, 'total_inodes': 128, 'rows': rows}


@pytest.mark.parametrize('profile', ['allocation', 'bytes', 'inodes'])
@pytest.mark.parametrize('mutation', ['none', 'missing', 'extra', 'list', 'empty', 'wrong-row-type', 'marker-only', 'wrong-profile', 'wrong-attempt', 'wrong-token', 'false-bound', 'wrong-device'])
def test_ext4_proof_validates_complete_current_substantive_matrix(profile, mutation):
    subject = wrapper(); proof = complete_ext4_proof('a' * 32, profile)
    key = next(k for k in proof['rows'] if k != 'recreation')
    if mutation == 'missing': proof['rows'].pop(key)
    elif mutation == 'extra': proof['rows']['foreign'] = {}
    elif mutation == 'list': proof['rows'] = list(proof['rows'])
    elif mutation == 'empty': proof['rows'] = {}
    elif mutation == 'wrong-row-type': proof['rows'][key] = []
    elif mutation == 'marker-only': proof['rows'][key] = {'passed': True}
    elif mutation == 'wrong-profile': proof['profile'] = 'foreign'
    elif mutation == 'wrong-attempt': proof['attempt'] = True
    elif mutation == 'wrong-token': proof['attempt_token'] = 'old'
    elif mutation == 'wrong-device': proof['rows']['recreation']['sentinel']['device'] = os.makedev(8, 1)
    elif mutation == 'false-bound':
        if profile == 'allocation': proof['rows'][key]['measurements'][0]['additional_peak'] = 0
        elif profile == 'bytes': proof['rows'][key]['failures'][0]['errno'] = 5
        else: proof['rows'][key]['remaining_bytes'] = 0
    kwargs = dict(run_id='a' * 32, profile=profile, attempt=0, token='c' * 32, fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}})
    if mutation == 'none': assert subject.validate_ext4_proof(proof, **kwargs) is proof
    else:
        with pytest.raises(subject.Refused): subject.validate_ext4_proof(proof, **kwargs)


@pytest.mark.parametrize('mutation', ['none', 'stale-attempt', 'stale-token', 'sentinel', 'retained-matrix'])
def test_ext4_recreation_requires_fresh_publication_and_preserved_observed_sentinel(mutation):
    subject = wrapper(); previous = complete_ext4_proof('a' * 32)
    current = complete_ext4_proof('a' * 32, attempt=1, token='d' * 32)
    if mutation == 'stale-attempt': current = previous
    elif mutation == 'stale-token': current['attempt_token'] = 'c' * 32
    elif mutation == 'sentinel': current['rows']['recreation']['sentinel']['inode'] += 1
    elif mutation == 'retained-matrix': current['rows']['repack']['objects'] += 1
    kwargs = dict(run_id='a' * 32, profile='allocation', attempt=1, token='d' * 32, fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}}, previous=previous)
    if mutation == 'none': subject.validate_ext4_proof(current, **kwargs)
    else:
        with pytest.raises(subject.Refused): subject.validate_ext4_proof(current, **kwargs)


@pytest.mark.parametrize('boundary', ['write', 'file-fsync', 'replace', 'directory-fsync'])
@pytest.mark.parametrize('cleanup_fault', ['unlink', 'close'])
def test_ext4_manifest_primary_errors_survive_secondary_cleanup(boundary, cleanup_fault, tmp_path, monkeypatch):
    import errno
    subject = ext4_helper(); fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original = b'{"acknowledged":"old"}'
    manifest = tmp_path / 'manifest.json'; manifest.write_bytes(original)
    first = OSError(errno.EIO, 'primary ' + boundary)
    second = OSError(errno.EACCES, 'secondary ' + cleanup_fault)
    real_write, real_fsync, real_replace, real_unlink, real_close = os.write, os.fsync, os.replace, os.unlink, os.close
    def write(target, data):
        if boundary == 'write': raise first
        return real_write(target, data)
    def fsync(target):
        if boundary == 'directory-fsync' and target == fd or boundary == 'file-fsync' and target != fd: raise first
        return real_fsync(target)
    def replace(*args, **kw):
        if boundary == 'replace': raise first
        return real_replace(*args, **kw)
    def unlink(name, **kw):
        if cleanup_fault == 'unlink': raise second
        return real_unlink(name, **kw)
    def close(target):
        real_close(target)
        if cleanup_fault == 'close' and target != fd: raise second
    monkeypatch.setattr(subject.os, 'write', write); monkeypatch.setattr(subject.os, 'fsync', fsync)
    monkeypatch.setattr(subject.os, 'replace', replace); monkeypatch.setattr(subject.os, 'unlink', unlink); monkeypatch.setattr(subject.os, 'close', close)
    try:
        with pytest.raises(OSError) as caught: subject.save(fd, {'acknowledged': 'new'})
        assert caught.value is first
        assert len(first.cleanup_errors) == 1 and str(second) in first.cleanup_errors[0]
        payload = subject.error_payload(first)
        assert payload['error'] == str(first) and payload['errno'] == errno.EIO and payload['cleanup_errors'] == list(first.cleanup_errors)
        if boundary == 'directory-fsync': assert json.loads(manifest.read_text()) == {'acknowledged': 'new'}
        else:
            assert manifest.read_bytes() == original
            if cleanup_fault == 'unlink': assert (tmp_path / 'manifest.tmp').exists()
    finally: real_close(fd)


def test_ext4_helper_failure_diagnostics_survive_process_boundary(monkeypatch):
    subject = wrapper(); payload = {'error': '[Errno 5] primary manifest fsync failure', 'type': 'OSError', 'errno': 5,
                                   'cleanup_errors': ['temporary unlink: [Errno 13] secondary denied']}
    monkeypatch.setattr(subject, 'invoke', lambda *a, **k: completed(stderr=('EXT4_FAILURE ' + json.dumps(payload) + '\ncolima exit status 1').encode(), code=1))
    with pytest.raises(subject.Refused) as caught: subject.ext4_guest('setup', 'a' * 32)
    assert str(caught.value) == payload['error'] and caught.value.guest_failure == payload
    assert subject.failure_payload(caught.value)['cleanup_errors'] == payload['cleanup_errors']


def test_ext4_report_write_preserves_replace_error_after_temporary_unlink_failure(tmp_path, monkeypatch):
    import errno
    subject = wrapper(); first = OSError(errno.EIO, 'primary report replace'); second = OSError(errno.EACCES, 'secondary report unlink')
    monkeypatch.setattr(subject.os, 'replace', lambda *a, **kw: (_ for _ in ()).throw(first))
    original_unlink = Path.unlink
    def unlink(path, *a, **k):
        if '.tmp-' in path.name: raise second
        return original_unlink(path, *a, **k)
    monkeypatch.setattr(subject.Path, 'unlink', unlink)
    with pytest.raises(OSError) as caught: subject.write_report(tmp_path / 'report.json', {'status': 'failed'})
    assert caught.value is first and first.cleanup_errors[0]['failure']['error'] == str(second)
    assert list(tmp_path.glob('report.json.tmp-*')) and not (tmp_path / 'report.json').exists()


def test_ext4_linux_sentinel_device_is_compared_as_guest_evidence_not_host_decoded(monkeypatch):
    subject = wrapper(); proof = complete_ext4_proof('a' * 32)
    proof['rows']['recreation']['sentinel']['device'] = 1795
    for crash in proof['rows']['independent-copies']['native_recovery']['process_crash']: crash['target_device']=1795
    for row in proof['rows'].values():
        samples = row.get('measurements', [row]) + ([row['existing_reflog']] if 'existing_reflog' in row else [])
        for sample in samples:
            for allocation in sample.get('allocations', []): allocation['volume'][0] = 1795
    monkeypatch.setattr(subject.os, 'major', lambda *a: pytest.fail('host decoded guest st_dev'))
    monkeypatch.setattr(subject.os, 'minor', lambda *a: pytest.fail('host decoded guest st_dev'))
    subject.validate_ext4_proof(proof, run_id='a' * 32, profile='allocation', attempt=0, token='c' * 32,
                                fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': 1795, 'loop': {'name': '/dev/loop3'}})


@pytest.mark.parametrize('owner', ['private_dir', 'backing', 'load'])
def test_ext4_adjacent_descriptor_finalizers_preserve_active_failure(owner, tmp_path, monkeypatch):
    import errno
    subject = ext4_helper(); first = OSError(errno.EIO, 'primary descriptor observation'); second = OSError(errno.EACCES, 'secondary descriptor close')
    image = tmp_path / 'image'; image.write_bytes(b'owned')
    (tmp_path / 'manifest.json').write_text('{"schema":1}')
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_close = os.close
    monkeypatch.setattr(subject.os, 'fstat', lambda fd: (_ for _ in ()).throw(first))
    def close(fd): original_close(fd); raise second
    monkeypatch.setattr(subject.os, 'close', close)
    try:
        with pytest.raises(OSError) as caught:
            if owner == 'private_dir': subject.private_dir(tmp_path)
            elif owner == 'backing': subject.backing(image)
            else: subject.load(parent, 'a' * 32)
        assert caught.value is first and len(first.cleanup_errors) == 1 and str(second) in first.cleanup_errors[0]
    finally: original_close(parent)


@pytest.mark.parametrize('mutation', ['volume', 'boundary-family', 'capacity-term', 'phase-rescan', 'journal-only-sink', 'partial-empty', 'publication-undo'])
def test_ext4_allocation_rows_cannot_substitute_weaker_proof(mutation):
    subject = wrapper(); proof = complete_ext4_proof('a' * 32)
    scope = proof['rows']['loose-index-worktree-save']
    if mutation == 'volume': scope['measurements'][0]['allocations'][0]['volume'] = [99, 'tmpfs', 'foreign']
    elif mutation == 'boundary-family': scope['measurements'][0]['allocations'][-1]['output'] = 'foreign'
    elif mutation == 'capacity-term': scope['measurements'][0]['required_inodes'] -= 1
    elif mutation == 'phase-rescan': scope['phase_rescans'] = 1
    elif mutation == 'journal-only-sink': proof['rows']['short-eio']['materialization_sink_eio']['sink_path'] = '/fixture/journal.tmp'
    elif mutation == 'partial-empty': proof['rows']['short-eio']['materialization_sink_eio']['partial_bytes'] = 0
    else: proof['rows']['short-eio']['owner_persistence'][-1]['published_revision'] = 0
    with pytest.raises(subject.Refused): subject.validate_ext4_proof(proof, run_id='a' * 32, profile='allocation', attempt=0, token='c' * 32,
        fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}})


def ext4_allocation_samples(proof):
    rows = proof['rows']; scope = rows['loose-index-worktree-save']
    return scope['measurements'] + [scope['existing_reflog']] + rows['controls']['measurements'] + [rows['independent-copies'], rows['repack']]


@pytest.mark.parametrize('family', range(7))
@pytest.mark.parametrize('field', ['logical_bytes', 'physical_bytes', 'new_inodes', 'blocks_delta', 'free_bytes_delta', 'free_inodes_delta'])
def test_ext4_each_nonempty_family_requires_each_observed_demand(field, family):
    subject = wrapper(); proof = complete_ext4_proof('a' * 32)
    ext4_allocation_samples(proof)[family][field] = 0
    with pytest.raises(subject.Refused):
        subject.validate_ext4_proof(proof, run_id='a' * 32, profile='allocation', attempt=0, token='c' * 32,
            fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}})


@pytest.mark.parametrize('fault', ['all-zero', 'physical-inflated', 'sub-quantum-blocks', 'sub-quantum-free', 'logical-above-physical', 'held-phase-decrease', 'missing-full-log'])
def test_ext4_observation_terms_must_be_consistent(fault):
    subject = wrapper(); proof = complete_ext4_proof('a' * 32); samples = ext4_allocation_samples(proof)
    if fault == 'all-zero':
        for row in samples:
            for field in ('logical_bytes', 'physical_bytes', 'new_inodes', 'blocks_delta', 'free_bytes_delta', 'free_inodes_delta'): row[field] = 0
    elif fault == 'physical-inflated': samples[0]['physical_bytes'] *= 2
    elif fault == 'sub-quantum-blocks': samples[0]['blocks_delta'] = 512
    elif fault == 'sub-quantum-free': samples[0]['free_bytes_delta'] = 512
    elif fault == 'logical-above-physical': samples[0]['logical_bytes'] = samples[0]['physical_bytes'] + 1
    elif fault == 'held-phase-decrease':
        for key in ('physical_bytes', 'blocks_delta', 'free_bytes_delta'): samples[0][key] *= 2
    else: samples[3]['retained_log_bytes'] = samples[3]['logical_bytes'] + 1
    with pytest.raises(subject.Refused):
        subject.validate_ext4_proof(proof, run_id='a' * 32, profile='allocation', attempt=0, token='c' * 32,
            fixture={'mount': {'device': '7:3'}, 'total_inodes': 128, 'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}})


@pytest.mark.parametrize('body_error', [False, True])
@pytest.mark.parametrize('boundaries', [('container',), ('guest',), ('host',), ('descriptor',), ('report',),
                                       ('guest', 'descriptor', 'report'), ('host', 'descriptor', 'report'), ('container', 'descriptor', 'report')])
def test_ext4_all_finalizers_preserve_primary_and_independent_structured_failures(body_error, boundaries, tmp_path, monkeypatch):
    import copy
    import errno
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)
    subject = wrapper(); runs = tmp_path / 'runs'; runs.mkdir(); events = []; reports = []; environment = {}; run = []
    attrs = {'ROOT': tmp_path, 'HERE': tmp_path, 'RUNS': runs, 'DIAGNOSTICS': tmp_path / 'diagnostics', 'snapshot_input': lambda p: {'total': 10},
             'copy_input': lambda src, dst, **k: dst.mkdir(), 'capacity': lambda *a: 10**10, 'make_context': lambda: b'',
             'load_toolchain': lambda c: None, 'installed': lambda: None, 'ready': lambda: {}, 'verify_source': lambda p: None,
             'guest_platform': lambda: 'linux/arm64', 'fingerprint': lambda c: 'fixture', 'image_present': lambda i: True,
             'verify_image_arch': lambda *a: None, 'endpoint_info': lambda: {'Name': 'colima-ha-ops-dev'}}
    for key, value in attrs.items(): monkeypatch.setattr(subject, key, value)
    monkeypatch.setattr(subject.shutil, 'which', lambda n: '/usr/bin/' + n)
    fixture = {'mount_path': '/guest/mount', 'mount': {'device': '7:3'}, 'total_inodes': 128,
               'mount_device_number': os.makedev(7, 3), 'loop': {'name': '/dev/loop3'}}
    errors = {}
    for phase in (*boundaries, 'body'):
        error = OSError(errno.EIO, 'primary ' + phase)
        error.cleanup_errors = ({'phase': phase + ' temporary', 'failure': {'error': 'secondary ' + phase, 'type': 'PermissionError', 'errno': errno.EACCES, 'debt': 'owned ' + phase + '.tmp'}},)
        if phase == 'guest': error.guest_failure = {'error': str(error), 'type': 'OSError', 'errno': errno.EIO, 'cleanup_errors': list(error.cleanup_errors)}
        errors[phase] = error
    def fail(phase):
        events.append(phase)
        if phase in boundaries: raise errors[phase]
    def guest(action, *args):
        if action == 'cleanup': fail('guest')
        return fixture if action in {'setup', 'observe'} else {'status': 'absent'}
    monkeypatch.setattr(subject, 'ext4_guest', guest)
    def remove(name, *args):
        if name != 'container': fail('container')
    monkeypatch.setattr(subject, 'remove_owned', remove)
    actual_owned = subject.OwnedDirectory
    class Owned:
        def __init__(self, path): self.inner = actual_owned(path)
        def remove(self): fail('host'); self.inner.remove()
        def close(self): self.inner.close(); fail('descriptor')
    monkeypatch.setattr(subject, 'OwnedDirectory', Owned)
    def docker(*args, **kwargs):
        if args[0] == 'create':
            run.append(args[args.index('--label') + 1].split('=')[1])
            environment.update(value.split('=', 1) for i, value in enumerate(args) if i and args[i-1] == '--env')
            return completed(b'container')
        if args[0] == 'container':
            info = ext4_inspection(); info['Config']['Labels']['ha-ops.dev.run'] = run[0]
            info['Config']['User'] = str(os.getuid()) + ':' + str(os.getgid()); current = next(runs.iterdir())
            for row in info['Mounts']:
                if row['Destination'] in {'/workspace/input', '/workspace/output'}: row['Source'] = str(current / Path(row['Destination']).name)
            return completed(json.dumps([info]).encode())
        if args[0] == 'wait':
            if body_error: raise errors['body']
            path = next(runs.iterdir()) / 'output' / Path(environment['HA_OPS_EXT4_REPORT']).name
            path.write_text(json.dumps(complete_ext4_proof(run[0], token=environment['HA_OPS_EXT4_TOKEN'])))
            return completed(b'0')
        return completed(b'imageid') if args[0] == 'image' else completed()
    monkeypatch.setattr(subject, 'docker', docker)
    def report(path, value): reports.append(copy.deepcopy(value)); fail('report')
    monkeypatch.setattr(subject, 'write_report', report)
    with pytest.raises(subject.Refused) as caught: subject.ext4_run(Path('.'), ['fixture'], report=tmp_path / 'report.json')
    expected = errors['body'] if body_error else errors[boundaries[0]]
    assert caught.value.__cause__ is expected
    assert caught.value.report['failure']['error'] == str(expected)
    assert caught.value.report['failure']['errno'] == errno.EIO
    assert caught.value.report['body_failure'] == (subject.failure_payload(errors['body']) if body_error else None)
    for phase in boundaries:
        assert str(errors[phase]) in str(caught.value) and 'secondary ' + phase in str(caught.value) and 'owned ' + phase + '.tmp' in str(caught.value)
        if phase != 'report':
            assert 'secondary ' + phase in json.dumps(reports[-1]) and 'owned ' + phase + '.tmp' in json.dumps(reports[-1])
    assert events[0] == 'container'
    if 'container' in boundaries: assert 'guest' not in events and 'host' not in events
    elif 'guest' in boundaries: assert 'host' not in events
    if 'guest' in events: assert events.index('container') < events.index('guest')
    if 'report' not in boundaries: assert reports[-1]['status'] == 'failed'


def test_ext4_subprocess_diagnostics_preserve_stderr_stdout_and_status():
    subject = wrapper(); error = subprocess.CalledProcessError(7, ['docker', 'inspect', 'owned'], output=b'partial output', stderr=b'exact owned refusal')
    payload = subject.failure_payload(error)
    assert payload['returncode'] == 7 and payload['cmd'] == error.cmd
    assert payload['stdout'] == 'partial output' and payload['stderr'] == 'exact owned refusal'


@pytest.mark.parametrize('phase', ['container', 'guest'])
def test_ext4_explicit_cleanup_cli_keeps_structured_debt_and_container_fence(phase, monkeypatch, capsys):
    import errno
    subject = wrapper(); events = []
    error = OSError(errno.EIO, 'primary ' + phase)
    error.cleanup_errors = ({'phase': 'temporary unlink', 'failure': {'error': 'secondary unlink denied', 'errno': errno.EACCES, 'debt': 'owned manifest.tmp'}},)
    monkeypatch.setattr(subject.sys, 'argv', ['ha_ops_dev', 'cleanup', '--ext4', '--run', 'a' * 32])
    monkeypatch.setattr(subject, 'endpoint_info', lambda: {})
    def remove(*args):
        events.append('container')
        if phase == 'container': raise error
    def guest(*args): events.append('guest'); raise error
    monkeypatch.setattr(subject, 'remove_owned', remove); monkeypatch.setattr(subject, 'ext4_guest', guest)
    monkeypatch.setattr(subject, 'cleanup_run', lambda *args: pytest.fail('host cleanup after guest/container refusal'))
    assert subject.main() == 1
    stderr = capsys.readouterr().err
    assert str(error) in stderr and 'secondary unlink denied' in stderr and 'owned manifest.tmp' in stderr
    assert '"errno": 5' in stderr and '"errno": 13' in stderr
    assert events[:2] == ['container', 'container']
    assert ('guest' in events) == (phase == 'guest')


@pytest.mark.parametrize('profile',['allocation','bytes'])
def test_ext4_recovery_execution_evidence_is_mandatory(profile):
    subject=wrapper(); run='d'*32; proof=complete_ext4_proof(run,profile)
    row=proof['rows']['independent-copies' if profile=='allocation' else 'byte-enospc']
    del row['native_recovery']
    fixture={'mount_device_number':os.makedev(7,3),'mount':{'device':'7:3'},'quantum':4096,'total_inodes':128,'loop':{'name':'/dev/loop3'}}
    with pytest.raises(subject.Refused,match='persistent recovery'):
        subject.validate_ext4_proof(proof,run_id=run,profile=profile,attempt=0,token='c'*32,fixture=fixture)


def test_ext4_host_guard_refuses_root_before_inspection(monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject.os, "getuid", lambda: 0)
    monkeypatch.setattr(subject, "snapshot_input", lambda *a: pytest.fail("root reached input inspection"))
    monkeypatch.setattr(subject, "docker", lambda *a, **k: pytest.fail("root reached Docker"))
    with pytest.raises(subject.Refused, match="nonroot application UID"):
        subject.ext4_run(Path("."), ["fixture"])


@pytest.mark.parametrize('field',['sample_boundaries','old_new_overlap','process_crash'])
def test_ext4_recovery_transient_overlap_and_child_proof_cannot_be_omitted(field):
    subject=wrapper(); run='d'*32; proof=complete_ext4_proof(run)
    del proof['rows']['independent-copies']['native_recovery'][field]
    fixture={'mount_device_number':os.makedev(7,3),'mount':{'device':'7:3'},'quantum':4096,'total_inodes':128,'loop':{'name':'/dev/loop3'}}
    with pytest.raises(subject.Refused,match='persistent recovery'):
        subject.validate_ext4_proof(proof,run_id=run,profile='allocation',attempt=0,token='c'*32,fixture=fixture)


def runtime_lifecycle_fixture(tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    runs = tmp_path.resolve() / 'runs'; runs.mkdir()
    app = tmp_path.resolve() / 'app'; (app / 'tests').mkdir(parents=True)
    (app / 'tests/test_fixture.py').write_text('fixture = True\n')
    (app / 'runtime-install.sh').write_text('#!/bin/sh\n')
    (app / 'runtime-apk-aarch64.lock').write_text('1' * 64 + '  https://invalid/example.apk\n')
    (tmp_path / 'runtime_receiver.py').write_text('receiver = True\n')
    monkeypatch.setattr(subject, 'APP', app)
    monkeypatch.setattr(subject.vm, 'LOCAL', tmp_path.resolve())
    monkeypatch.setattr(subject.vm, 'RUNS', runs)
    monkeypatch.setattr(subject.vm, 'ready', lambda **kw: None)
    monkeypatch.setattr(subject.vm, 'guest_platform', lambda: 'linux/arm64')
    monkeypatch.setattr(subject.vm, 'image_name', lambda: 'dev-image')
    monkeypatch.setattr(subject.vm, 'verify_source', lambda path: None)
    monkeypatch.setattr(subject.vm, 'snapshot_input', lambda path: {})
    def copy(source, destination, snapshot, *, fixture=None):
        import shutil
        shutil.copytree(source, destination)
        if fixture is not None: fixture.remember(destination.name)
    monkeypatch.setattr(subject, 'copy_preserving_modes', copy)
    monkeypatch.setattr(subject, 'runtime_image', lambda: {'image_id': 'image', 'app_manifest': {}})
    monkeypatch.setattr(subject, 'required_group', lambda *a, **kw: ('fixture', {'selectors': ['test_fixture.py'], 'nodeids': ['test_fixture.py::test_case']}, 'hash'))
    monkeypatch.setattr(subject, 'verify_container', lambda *a: None)
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *a: None)
    monkeypatch.setattr(subject.vm, 'owned_container', lambda *a: None)
    volume_exists = [False]
    def docker(*args, **kw):
        out, err, code = b'', b'', 0
        if args[:2] == ('volume', 'create'): volume_exists[0] = True
        elif args[:2] == ('volume', 'rm'): volume_exists[0] = False
        elif args[:2] == ('volume', 'inspect'):
            if volume_exists[0]: out = json.dumps([{'Driver': 'local', 'Labels': {'ha-ops.dev.run': args[2].removeprefix('ha-ops-runtime-')}, 'Options': {'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=1073741824,mode=1777'}}]).encode()
            else: code, err = 1, b'no such volume'
        elif args[0] == 'create': out = args[args.index('--name') + 1].encode()
        elif args[0] == 'inspect': out = b'[{}]'
        elif args[:2] == ('image', 'inspect'): out = b'image'
        elif args[0] == 'exec':
            if '--collect-only' in args: out = b'test_fixture.py::test_case\n'
            elif '/bin/cat' in args: out = b'<testsuites><testsuite tests="1"><testcase classname="test_fixture" name="test_case"/></testsuite></testsuites>'
            elif args[-1].startswith('import pathlib,hashlib,json;'): out = b'{}'
        return subprocess.CompletedProcess(args, code, out, err)
    monkeypatch.setattr(subject.vm, 'docker', docker)
    return subject, runs, cache, docker


@pytest.mark.parametrize('outcome', ['passed', 'nonzero', 'copy', 'interrupt'])
def test_runtime_run_removes_owned_fixtures_on_every_exit(outcome, tmp_path, monkeypatch, capsys):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = KeyboardInterrupt('original interrupt') if outcome == 'interrupt' else OSError('original copy')
    if outcome == 'copy':
        monkeypatch.setattr(subject, 'copy_preserving_modes', lambda *a, **kw: (_ for _ in ()).throw(first))
    else:
        def execute(*args, **kw):
            if args[0] == 'exec' and '--basetemp=/workspace/output/pytest' in args:
                if outcome == 'interrupt': raise first
                if outcome == 'nonzero': return subprocess.CompletedProcess(args, 1, b'failed stdout', b'failed stderr')
            return docker(*args, **kw)
        monkeypatch.setattr(subject.vm, 'docker', execute)
    report = tmp_path / 'report.json'
    if outcome in ('copy', 'interrupt'):
        with pytest.raises(type(first)) as caught: subject.run_tests([], report)
        assert caught.value is first
    else: assert subject.run_tests([], report) == (0 if outcome == 'passed' else 1)
    retained = outcome == 'interrupt'
    assert bool(list(runs.iterdir())) == bool(list(cache.glob('pytest-owned-*'))) == retained
    proof = json.loads(report.read_text())
    assert proof['cleanup']['status'] == ('retained' if retained else 'removed')
    assert proof['status'] == ('passed' if outcome == 'passed' else 'failed')
    if outcome == 'nonzero': assert proof['stdout'] == 'failed stdout' and proof['junit']


@pytest.mark.parametrize('mutation', ['replace', 'unknown', 'input', 'receiver', 'output-replace'])
def test_runtime_cleanup_preserves_changed_namespace(mutation, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir()
    fixture = subject.RuntimeFixture(root)
    (root / 'input').mkdir(); (root / 'input/source').write_text('owned')
    (root / 'receiver.py').write_text('owned')
    (root / 'output').mkdir()
    for name in ('input', 'receiver.py'): fixture.remember(name)
    fixture.remember('output', dynamic=True)
    if mutation == 'replace':
        root.rename(runs / 'original'); root.mkdir(); (root / 'user').write_text('foreign')
    elif mutation == 'unknown': (root / 'user').write_text('foreign')
    elif mutation == 'output-replace':
        (root / 'output').rename(root / 'old-output'); (root / 'output').mkdir()
    elif mutation == 'input': (root / 'input/source').write_text('changed')
    else: (root / 'receiver.py').write_text('changed')
    result = {'status': 'passed'}
    with pytest.raises(subject.vm.Refused): subject.finish_runtime(result, tmp_path / 'proof.json', 'owned', fixture)
    assert root.exists() and result['status'] == 'failed' and result['cleanup']['status'] == 'refused'
    assert fixture.owned.root is None


@pytest.mark.parametrize('publication', ['first', 'all', 'final'])
def test_runtime_report_failure_preserves_proof_and_is_never_success(publication, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    report = tmp_path / 'proof.json'
    actual = subject.vm.write_report
    calls = []
    def write(path, data):
        calls.append(path)
        if publication == 'all' or publication == 'first' and len(calls) == 1 or publication == 'final' and len(calls) == 2:
            raise OSError('report denied')
        if len(calls) == 1: assert root.exists() and data['status'] == 'finalizing'
        actual(path, data)
    monkeypatch.setattr(subject.vm, 'write_report', write)
    result = {'status': 'passed', 'stdout': 'causal proof', 'junit': '<testsuites/>'}
    with pytest.raises(subject.vm.Refused): subject.finish_runtime(result, report, 'owned', fixture)
    assert result['status'] == 'failed' and root.exists() == (publication == 'all')
    durable = report if publication == 'final' else tmp_path / 'runtime-cleanup-owned.json'
    if publication != 'all': assert json.loads(durable.read_text())['stdout'] == 'causal proof'
    if publication == 'final': assert json.loads(report.read_text())['status'] == 'finalizing'


@pytest.mark.parametrize('recovered', [True, False])
def test_runtime_consumer_fence_retries_exact_identity_and_retains_debt(recovered, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    dependencies = subject.pytest_dependencies()
    calls = []
    def remove(name, owner):
        calls.append(name)
        if name == 'first' and (not recovered or calls.count('first') == 1): raise subject.vm.Refused('consumer uncertain')
    monkeypatch.setattr(subject.vm, 'remove_owned', remove)
    result = {'status': 'passed'}
    if recovered: subject.finish_runtime(result, tmp_path / 'proof.json', 'owned', fixture, dependencies, ['second', 'first'])
    else:
        with pytest.raises(subject.vm.Refused): subject.finish_runtime(result, tmp_path / 'proof.json', 'owned', fixture, dependencies, ['second', 'first'])
    assert calls == ['first', 'first', 'second']
    assert root.exists() == dependencies.exists() == (not recovered)
    assert len(result['cleanup_attempts']) >= 3
    if not recovered: assert subject.DEPENDENCY_OWNERS[dependencies].root is None


@pytest.mark.parametrize('boundary', ['parse', 'hash', 'extract'])
def test_runtime_partial_dependency_failure_reclaims_known_extraction(boundary, tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    if boundary == 'parse': (tmp_path / 'runtime-toolchain.json').write_text('{')
    elif boundary == 'hash': (cache / 'pip/pytest-8.4.2-py3-none-any.whl').write_text('changed')
    else:
        import hashlib
        import zipfile
        wheel = cache / 'pip/pytest-8.4.2-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'a') as archive: archive.writestr('ssl.py', 'foreign')
        (tmp_path / 'runtime-toolchain.json').write_text(json.dumps({'pytest_wheels': ['pytest==8.4.2 --hash=sha256:' + hashlib.sha256(wheel.read_bytes()).hexdigest()]}))
    with pytest.raises((ValueError, subject.vm.Refused)): subject.pytest_dependencies()
    assert not list(cache.glob('pytest-owned-*')) and not subject.DEPENDENCY_OWNERS


def test_runtime_dependency_cleanup_preserves_changed_files(tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    fresh = subject.pytest_dependencies()
    (fresh / 'pytest/__init__.py').write_text('foreign')
    with pytest.raises(subject.vm.Refused): subject.cleanup_dependencies(fresh)
    assert fresh.exists() and fresh in subject.DEPENDENCY_TREES and subject.DEPENDENCY_OWNERS[fresh].root is None


@pytest.mark.parametrize('outcome', ['passed', 'timeout'])
def test_runtime_negative_probe_names_consumers_and_cleans_on_interrupt(outcome, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    consumers = []
    first = subprocess.TimeoutExpired('docker run', 120, output=b'causal output')
    def execute(*args, **kw):
        assert args[0] == 'run' and '--name' in args and '--label' in args
        consumers.append(args[args.index('--name') + 1])
        if outcome == 'timeout': raise first
        return subprocess.CompletedProcess(args, 1, b'FAILED Pinned runtime artifact unavailable', b'')
    monkeypatch.setattr(subject.vm, 'docker', execute)
    removed = []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda name, owner: removed.append(name))
    if outcome == 'timeout':
        with pytest.raises(subprocess.TimeoutExpired) as caught: subject.check_lock_failure(tmp_path / 'proof.json')
        assert caught.value is first
    else: subject.check_lock_failure(tmp_path / 'proof.json')
    assert set(removed) == set(consumers) and not list(runs.iterdir())


def test_runtime_build_copy_failure_uses_creation_anchor(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = OSError('copy preflight failed')
    monkeypatch.setattr(subject, 'copy_preserving_modes', lambda *a, **kw: (_ for _ in ()).throw(first))
    with pytest.raises(OSError) as caught: subject.build()
    assert caught.value is first and not list(runs.iterdir())
    assert list(tmp_path.glob('runtime-build-*.json'))


def test_runtime_report_inside_fixture_is_rejected_before_acquisition(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(subject.uuid, 'uuid4', lambda: type('UUID', (), {'hex': 'a' * 32})())
    with pytest.raises(subject.vm.Refused, match='outside disposable'):
        subject.run_tests([], runs / ('runtime-' + 'a' * 32) / 'proof.json')
    assert not list(runs.iterdir()) and not list(cache.glob('pytest-owned-*'))


def test_runtime_primary_interrupt_survives_cleanup_and_report_failures(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = KeyboardInterrupt('causal')
    monkeypatch.setattr(subject, 'copy_preserving_modes', lambda *a, **kw: (_ for _ in ()).throw(first))
    monkeypatch.setattr(subject.vm, 'write_report', lambda *a: (_ for _ in ()).throw(OSError('secondary report')))
    with pytest.raises(KeyboardInterrupt) as caught: subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first and first.cleanup_errors and list(runs.iterdir())


@pytest.mark.parametrize('boundary', ['partial-write', 'close'])
def test_runtime_dependency_acquisition_failure_cleans_partial_owned_bytes(boundary, tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    actual_write, actual_close = subject.os.write, subject.os.close
    first = OSError('owned file ' + boundary)
    writes = []
    def write(handle, payload):
        writes.append(handle)
        if len(writes) == 1: return actual_write(handle, payload[:3])
        raise first
    def close(handle):
        actual_close(handle)
        if handle in writes: raise OSError('secondary close')
    monkeypatch.setattr(subject.os, 'write', write)
    if boundary == 'close': monkeypatch.setattr(subject.os, 'close', close)
    with pytest.raises(OSError) as caught: subject.pytest_dependencies()
    assert caught.value is first and not list(cache.glob('pytest-owned-*'))
    if boundary == 'close': assert first.cleanup_errors[0]['phase'] == 'dependency file close'


def test_runtime_dependency_cleanup_refuses_replacement_root(tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    fresh = subject.pytest_dependencies()
    fresh.rename(cache / 'original')
    fresh.mkdir(); (fresh / 'user').write_text('foreign')
    with pytest.raises(subject.vm.Refused, match='root changed'): subject.cleanup_dependencies(fresh)
    assert (fresh / 'user').read_text() == 'foreign' and (cache / 'original/pytest/__init__.py').exists()


def test_runtime_consumer_absence_inspection_failure_retains_all_host_binds(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    dependencies = subject.pytest_dependencies()
    monkeypatch.setattr(subject.vm, 'owned_container', lambda *a: (_ for _ in ()).throw(subject.vm.Refused('inspection unavailable')))
    with pytest.raises(subject.vm.Refused):
        subject.finish_runtime({'status': 'passed'}, tmp_path / 'proof.json', 'owned', fixture, dependencies, ['exact-name'])
    assert root.exists() and dependencies.exists()


def test_runtime_dynamic_output_is_removed_but_neighbor_cache_survives(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    (root / 'output').mkdir(); fixture.remember('output', dynamic=True)
    (root / 'output/results').write_text('dynamic')
    neighbor = cache / 'user'; neighbor.write_text('preserved')
    subject.finish_runtime({'status': 'passed'}, tmp_path / 'proof.json', 'owned', fixture)
    assert not root.exists() and neighbor.read_text() == 'preserved'


def test_runtime_primary_failure_survives_anchor_close_failure(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    first = KeyboardInterrupt('original')
    actual = fixture.close
    def close():
        actual()
        raise OSError('secondary close')
    monkeypatch.setattr(fixture, 'close', close)
    result = {'status': 'passed'}
    subject.finish_runtime(result, tmp_path / 'proof.json', 'owned', fixture, primary=first)
    assert any(row['phase'] == 'host fixture close' for row in first.cleanup_errors)
    assert json.loads((tmp_path / 'proof.json').read_text())['status'] == 'failed'


def test_runtime_build_timeout_retains_context_with_causal_proof(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = subprocess.TimeoutExpired('docker build', 1200, output=b'build proof')
    monkeypatch.setattr(subject.vm, 'docker', lambda *a, **kw: (_ for _ in ()).throw(first))
    with pytest.raises(subprocess.TimeoutExpired) as caught: subject.build()
    assert caught.value is first and len(list(runs.iterdir())) == 1
    proof = json.loads(next(tmp_path.glob('runtime-build-*.json')).read_text())
    assert proof['status'] == 'failed' and proof['failure']['stdout'] == 'build proof'
    assert proof['cleanup']['status'] == 'retained'
def test_runtime_durable_proof_precedes_tmpfs_deletion_on_interrupt(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    events = []
    publish = subject.vm.write_report
    def write(path, data):
        publish(path, data)
        events.append('durable-proof')
    def execute(*args, **kwargs):
        if args[0] == 'exec' and '--basetemp=/workspace/output/pytest' in args:
            raise KeyboardInterrupt('interrupted with partial output')
        if args[:2] == ('volume', 'rm'):
            events.append('volume-delete')
        return docker(*args, **kwargs)
    monkeypatch.setattr(subject.vm, 'write_report', write)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(KeyboardInterrupt):
        subject.run_tests([], tmp_path / 'proof.json')
    assert 'durable-proof' in events and 'volume-delete' not in events
    # Also seed readable partial JUnit/ext4 bytes and deny all report writes:
    # raw bytes must either be durably preserved or survive in retained storage.


def test_runtime_recreated_junit_survives_cleanup(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    assert subject.run_tests([], tmp_path / 'proof.json', recreate=True) == 0
    report = json.loads((tmp_path / 'proof.json').read_text())
    assert report['recreated']['junit'].startswith('<testsuites>')


def test_runtime_post_copy_chmod_failure_reclaims_owned_input(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    (subject.APP / 'tests/nested').mkdir()
    real_copy = runtime_wrapper().copy_preserving_modes
    copy = subject.copy_preserving_modes
    monkeypatch.setattr(subject, 'copy_preserving_modes', real_copy)
    source_vm = wrapper()
    monkeypatch.setattr(source_vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(source_vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(subject.vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'snapshot_input', source_vm.snapshot_input)
    monkeypatch.setattr(subject, 'copy_preserving_modes', real_copy if 'real_copy' in locals() else actual)
    chmod = subject.os.fchmod
    first = OSError('chmod after owned copy')
    def fail_nested(handle, mode):
        if any(path.stat().st_ino == os.fstat(handle).st_ino for path in runs.glob('*/input/nested')): raise first
        return chmod(handle, mode)
    monkeypatch.setattr(subject.os, 'fchmod', fail_nested)
    with pytest.raises(OSError) as caught:
        subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first
    assert not list(runs.iterdir())


def test_runtime_negative_probe_does_not_publish_success_before_cleanup(tmp_path, monkeypatch, capsys):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(subject.vm, 'docker', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b'FAILED Pinned runtime artifact unavailable', b''))
    def refuse(*args):
        raise subject.vm.Refused('persistent removal failure')
    monkeypatch.setattr(subject.vm, 'remove_owned', refuse)
    with pytest.raises(subject.vm.Refused):
        subject.check_lock_failure(tmp_path / 'proof.json')
    assert '"status": "passed"' not in capsys.readouterr().out


@pytest.mark.parametrize('entrypoint', ['run', 'build'])
def test_runtime_actual_copy_mode_failure_keeps_creation_receipt(entrypoint, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    actual = runtime_wrapper().copy_preserving_modes
    copy = subject.copy_preserving_modes
    source_vm = wrapper()
    monkeypatch.setattr(source_vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(source_vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(subject.vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'snapshot_input', source_vm.snapshot_input)
    monkeypatch.setattr(subject, 'copy_preserving_modes', real_copy if 'real_copy' in locals() else actual)
    chmod = subject.os.fchmod
    first = OSError('chmod after owned copy')
    def fail_input(handle, mode):
        if any(path.stat().st_ino == os.fstat(handle).st_ino for path in runs.glob('*/input')): raise first
        return chmod(handle, mode)
    monkeypatch.setattr(subject.os, 'fchmod', fail_input)
    with pytest.raises(OSError) as caught:
        subject.run_tests([], tmp_path / 'proof.json') if entrypoint == 'run' else subject.build()
    assert caught.value is first and not list(runs.iterdir())


@pytest.mark.parametrize('entrypoint', ['run', 'negative'])
def test_runtime_partial_owned_file_writes_are_reclaimed(entrypoint, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    # Keep dependency extraction independent of the deliberately failed fixture write.
    dependency = subject.pytest_dependencies()
    monkeypatch.setattr(subject, 'pytest_dependencies', lambda: dependency)
    write = subject.os.write
    first = OSError('partial fixture write')
    calls = []
    def partial(handle, payload):
        calls.append(handle)
        if len(calls) == 1: return write(handle, payload[:3])
        raise first
    monkeypatch.setattr(subject.os, 'write', partial)
    with pytest.raises(OSError) as caught:
        subject.run_tests([], tmp_path / 'proof.json') if entrypoint == 'run' else subject.check_lock_failure(tmp_path / 'proof.json')
    assert caught.value is first and not list(runs.iterdir())
    if entrypoint == 'negative': subject.cleanup_dependencies(dependency)


def test_runtime_output_chmod_failure_reclaims_registered_output(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    chmod = subject.os.fchmod
    first = OSError('output chmod')
    def fail(handle, mode):
        if any(path.stat().st_ino == os.fstat(handle).st_ino for path in runs.glob('*/output')): raise first
        return chmod(handle, mode)
    monkeypatch.setattr(subject.os, 'fchmod', fail)
    with pytest.raises(OSError) as caught: subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first and not list(runs.iterdir())


@pytest.mark.parametrize('boundary', ['dependency', 'run', 'build'])
def test_runtime_anchor_acquisition_failure_has_durable_exact_debt(boundary, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    anchor = subject.vm.OwnedDirectory
    first = OSError('anchor unavailable')
    def fail(path):
        name = path.name
        if boundary == 'dependency' and name.startswith('pytest-owned-') or boundary == 'build' and name.startswith('runtime-build-') or boundary == 'run' and name.startswith('runtime-'):
            raise first
        return anchor(path)
    monkeypatch.setattr(subject.vm, 'OwnedDirectory', fail)
    with pytest.raises(OSError) as caught:
        subject.build() if boundary == 'build' else subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first
    retained = list(cache.glob('pytest-owned-*')) if boundary == 'dependency' else list(runs.iterdir())
    assert len(retained) == 1
    path = next(tmp_path.glob('runtime-build-*.json')) if boundary == 'build' else tmp_path / 'proof.json'
    report = json.loads(path.read_text())
    assert report['status'] == 'failed' and any(row['path'] == str(retained[0]) for row in report['acquisition_debt'])


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('recreated', [False, True])
def test_runtime_console_failure_preserves_completed_results_and_junit(stream, recreated, tmp_path, monkeypatch):
    import types
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = BrokenPipeError('closed ' + stream)
    calls = []
    def execute(*args, **kw):
        completed = docker(*args, **kw)
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args):
            completed.stdout, completed.stderr = b'completed stdout', b'completed stderr'
        return completed
    def echo(payload):
        calls.append(payload)
        if len(calls) == (2 if recreated else 1): raise first
        return len(payload)
    noop = types.SimpleNamespace(buffer=types.SimpleNamespace(write=lambda payload: len(payload)))
    broken = types.SimpleNamespace(buffer=types.SimpleNamespace(write=echo))
    monkeypatch.setattr(subject, 'sys', types.SimpleNamespace(stdout=broken if stream == 'stdout' else noop,
        stderr=broken if stream == 'stderr' else noop, exc_info=sys.exc_info))
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(BrokenPipeError) as caught: subject.run_tests([], tmp_path / 'proof.json', recreate=recreated)
    assert caught.value is first and not list(runs.iterdir())
    result = json.loads((tmp_path / 'proof.json').read_text())
    target = result['recreated'] if recreated else result
    assert target['stdout'] == 'completed stdout' and target['stderr'] == 'completed stderr' and target['returncode'] == 0
    assert target['junit'].startswith('<testsuites>')


@pytest.mark.parametrize('failure', ['transient-read', 'persistent-read', 'reports', 'interrupt'])
def test_runtime_output_storage_survives_until_proof_is_saved(failure, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    events, reads = [], []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *a: events.append('consumer-deleted'))
    publish = subject.vm.write_report
    first = KeyboardInterrupt('test interrupted') if failure == 'interrupt' else OSError('read failed')
    def execute(*args, **kw):
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args) and failure == 'interrupt': raise first
        if args[0] == 'exec' and '/bin/cat' in args:
            reads.append(args[-1])
            if failure == 'persistent-read' or failure == 'transient-read' and len(reads) == 1: raise first
        if args[:2] == ('volume', 'rm'): events.append('volume-deleted')
        return docker(*args, **kw)
    def write(path, report):
        if failure == 'reports': raise OSError('all publication denied')
        publish(path, report); events.append('proof-saved')
    monkeypatch.setattr(subject.vm, 'docker', execute)
    monkeypatch.setattr(subject.vm, 'write_report', write)
    with pytest.raises((OSError, KeyboardInterrupt, subject.vm.Refused)):
        subject.run_tests([], tmp_path / 'proof.json')
    retained = failure in ('persistent-read', 'reports', 'interrupt')
    assert bool(list(runs.iterdir())) == retained
    if retained: assert 'volume-deleted' not in events and 'consumer-deleted' not in events
    else:
        assert events.index('proof-saved') < events.index('consumer-deleted') < events.index('volume-deleted')
        assert json.loads((tmp_path / 'proof.json').read_text())['junit'].startswith('<testsuites>')
    if failure == 'persistent-read':
        report = json.loads((tmp_path / 'proof.json').read_text())
        assert report['cleanup']['volume']['status'] == 'retained' and report['cleanup']['dependencies']['status'] == 'retained'


def test_runtime_inner_copy_failure_removes_partial_input_without_parent_debt(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    # The existing copier removes its held partial destination before propagating failure.
    real_vm = wrapper()
    monkeypatch.setattr(real_vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(real_vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject, 'copy_preserving_modes', runtime_wrapper().copy_preserving_modes)
    monkeypatch.setattr(subject.vm, 'snapshot_input', real_vm.snapshot_input)
    monkeypatch.setattr(subject.vm, 'copy_input', real_vm.copy_input)
    first = OSError('source read')
    monkeypatch.setattr(subject.os, 'read', lambda *args: (_ for _ in ()).throw(first))
    with pytest.raises(OSError) as caught: subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first and not list(runs.iterdir())
    assert not json.loads((tmp_path / 'proof.json').read_text())['acquisition_debt']


@pytest.mark.parametrize('boundary', ['export', 'publication'])
def test_runtime_unpreserved_ext4_proof_retains_backing_storage(boundary, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    events = []
    def execute(*args, **kw):
        if args[:2] == ('volume', 'rm'): events.append('volume-delete')
        return docker(*args, **kw)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    monkeypatch.setattr(subject.vm, 'ext4_guest', lambda *a, **kw: events.append('ext4-delete'))
    if boundary == 'publication': monkeypatch.setattr(subject.vm, 'write_report', lambda *a: (_ for _ in ()).throw(OSError('proof publication denied')))
    report = {'status': 'failed', 'junit': '<testsuites/>', 'ext4_raw': '{partial proof'}
    with pytest.raises(subject.vm.Refused):
        subject.finish_runtime(report, tmp_path / 'proof.json', 'owned', fixture, volume='owned-volume', ext4_acquired=True,
            ext4_profile='allocation', proof_complete=boundary != 'export')
    assert root.exists() and not events
    if boundary == 'export':
        proof = json.loads((tmp_path / 'proof.json').read_text())
        assert proof['cleanup']['volume']['status'] == proof['cleanup']['ext4']['status'] == 'retained'
        assert proof['ext4_raw'] == '{partial proof'


def test_runtime_timeout_preserves_partial_process_output_and_available_junit(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = subprocess.TimeoutExpired('docker exec pytest', 1200, output=b'partial stdout', stderr=b'partial stderr')
    def execute(*args, **kw):
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args): raise first
        return docker(*args, **kw)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(subprocess.TimeoutExpired) as caught: subject.run_tests([], tmp_path / 'proof.json')
    proof = json.loads((tmp_path / 'proof.json').read_text())
    assert caught.value is first and proof['stdout'] == 'partial stdout' and proof['stderr'] == 'partial stderr'
    assert proof['junit'].startswith('<testsuites>') and list(runs.iterdir())
    assert proof['cleanup']['volume']['status'] == 'retained'


def test_runtime_partial_copy_with_unavailable_anchor_reports_debt_without_adoption(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    real_copy = runtime_wrapper().copy_preserving_modes
    first = OSError('inner destination anchor unavailable')
    def copy(source, destination, expected):
        destination.mkdir(); (destination / 'unknown').write_text('unproven')
        raise first
    monkeypatch.setattr(subject, 'copy_preserving_modes', real_copy)
    monkeypatch.setitem(real_copy.__globals__, 'copy_into_fixture', lambda src, dst, snap, fixture: copy(src, dst, snap))
    with pytest.raises(OSError) as caught: subject.run_tests([], tmp_path / 'proof.json')
    root = next(runs.iterdir())
    proof = json.loads((tmp_path / 'proof.json').read_text())
    assert caught.value is first and (root / 'input/unknown').read_text() == 'unproven'
    assert any(row['path'] == str(root / 'input') for row in proof['acquisition_debt'])
    assert proof['cleanup']['status'] == 'refused'


@pytest.mark.parametrize('completed', [False, True])
def test_runtime_proven_absent_junit_only_cleans_after_completed_execution(completed, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    removed = []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *a: removed.append(a))
    first = KeyboardInterrupt('execution outcome unknown')
    def execute(*args, **kw):
        if args[0] == 'exec' and '/bin/cat' in args:
            return subprocess.CompletedProcess(args, 1, b'', b'cat: results.xml: No such file or directory')
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args):
            if not completed: raise first
            return subprocess.CompletedProcess(args, 4, b'usage error', b'pytest usage error')
        return docker(*args, **kw)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    if completed: assert subject.run_tests([], tmp_path / 'proof.json') == 1
    else:
        with pytest.raises(KeyboardInterrupt) as caught: subject.run_tests([], tmp_path / 'proof.json')
        assert caught.value is first
    proof = json.loads((tmp_path / 'proof.json').read_text())
    assert bool(removed) == completed and bool(list(runs.iterdir())) != completed
    assert proof['status'] == 'failed'
    if completed: assert proof['junit_absent']['path'] == 'results.xml'


def test_runtime_owned_receiver_and_lock_remain_readable_to_nonroot_peer(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    try:
        fixture.write('receiver.py', b'receiver')
        assert (root / 'receiver.py').stat().st_mode & 0o444 == 0o444
        fixture.write('receiver.py', b'rewritten')
        fixture.validate()
    finally:
        fixture.remove(); fixture.close()
def test_runtime_unknown_exec_retains_consumer_even_with_readable_xml(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = subprocess.TimeoutExpired('docker exec pytest', 1200, output=b'partial stdout')
    removed = []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *args: removed.append(args))
    def execute(*args, **kwargs):
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args):
            raise first
        # A readable XML file is not a remote-process completion receipt.
        return docker(*args, **kwargs)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first
    report = json.loads((tmp_path / 'proof.json').read_text())
    assert 'execution_completed' not in report and report['junit']
    assert not removed
    assert report['cleanup']['volume']['status'] == 'retained'


def test_runtime_dependency_refusal_preserves_creation_identity_and_manifest(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    created = []
    dependencies = subject.pytest_dependencies
    def acquire():
        destination = dependencies()
        created.append((destination, destination.stat().st_dev, destination.stat().st_ino,
                        dict(subject.DEPENDENCY_TREES[destination])))
        return destination
    monkeypatch.setattr(subject, 'pytest_dependencies', acquire)
    def execute(*args, **kwargs):
        if args[0] == 'exec' and any(str(arg).startswith('--basetemp=') for arg in args):
            (created[0][0] / 'pytest/__init__.py').write_text('changed content')
        return docker(*args, **kwargs)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(subject.vm.Refused):
        subject.run_tests([], tmp_path / 'proof.json')
    row = json.loads((tmp_path / 'proof.json').read_text())['cleanup']['dependencies']
    assert row['status'] == 'refused'
    assert row['identity'] == list(created[0][1:3])
    assert row['expected_files'] == created[0][3]
    assert row['retry']


def test_runtime_recreation_publishes_original_proof_before_first_removal(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    events = []
    publish = subject.vm.write_report
    def write(path, result):
        publish(path, result)
        events.append('durable')
    monkeypatch.setattr(subject.vm, 'write_report', write)
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *args: events.append('consumer-remove'))
    assert subject.run_tests([], tmp_path / 'proof.json', recreate=True) == 0
    assert events.index('durable') < events.index('consumer-remove')


@pytest.mark.parametrize('recreated', [False, True])
@pytest.mark.parametrize('artifact', [b'<partial-junit', b'<testsuites><testsuite tests="0"/></testsuites>'])
def test_runtime_unknown_writer_retains_all_resources_despite_readable_xml(recreated, artifact, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = KeyboardInterrupt('writer completion unknown')
    removed, calls = [], []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *args: removed.append(args))
    def execute(*args, **kwargs):
        if args[0] == 'exec' and any(str(x).startswith('--basetemp=') for x in args):
            calls.append(args)
            if not recreated or len(calls) == 2: raise first
        if args[0] == 'exec' and '/bin/cat' in args and (not recreated or len(calls) == 2):
            return subprocess.CompletedProcess(args, 0, artifact, b'')
        return docker(*args, **kwargs)
    monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(KeyboardInterrupt) as caught: subject.run_tests([], tmp_path / 'proof.json', recreate=recreated)
    proof = json.loads((tmp_path / 'proof.json').read_text())
    target = proof['recreated'] if recreated else proof
    assert caught.value is first and not target.get('execution_completed') and target['junit'] == artifact.decode()
    assert len(removed) == int(recreated)  # Only checkpointed first client in recreation.
    assert proof['cleanup']['volume']['status'] == 'retained' and proof['cleanup']['dependencies']['identity']
    assert list(runs.iterdir())


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase', ['consumer', 'proof', 'volume', 'host', 'close', 'artifact-recovery'])
def test_runtime_first_finalization_cancellation_survives_unchanged(error_type, phase, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    first = error_type('first cancellation in ' + phase)
    calls = []
    def fail_once(*args):
        calls.append(args)
        if len(calls) == 1: raise first
    if phase == 'consumer': monkeypatch.setattr(subject.vm, 'remove_owned', fail_once)
    elif phase == 'proof':
        actual = subject.vm.write_report
        def write(*args): fail_once(*args); return actual(*args)
        monkeypatch.setattr(subject.vm, 'write_report', write)
    elif phase in ('host', 'close'):
        actual = getattr(subject.RuntimeFixture, 'remove' if phase == 'host' else 'close')
        def action(self):
            if phase == 'close': actual(self)
            fail_once(self)
            if phase == 'host': actual(self)
        monkeypatch.setattr(subject.RuntimeFixture, 'remove' if phase == 'host' else 'close', action)
    else:
        reads = []
        def execute(*args, **kw):
            if phase == 'volume' and args[:2] == ('volume', 'rm'): fail_once(*args)
            if phase == 'artifact-recovery' and args[0] == 'exec' and '/bin/cat' in args:
                reads.append(args)
                if len(reads) == 1: return subprocess.CompletedProcess(args, 1, b'', b'uncertain read')
                raise first
            return docker(*args, **kw)
        monkeypatch.setattr(subject.vm, 'docker', execute)
    with pytest.raises(error_type) as caught: subject.run_tests([], tmp_path / 'proof.json')
    assert caught.value is first
    reports = list(tmp_path.glob('proof.json')) + list(tmp_path.glob('runtime-cleanup-*.json'))
    assert reports and all(json.loads(path.read_text())['status'] != 'passed' for path in reports)


@pytest.mark.parametrize('phase', ['write-open', 'chmod-open', 'chmod-after-open', 'root-write', 'root-mkdir'])
def test_runtime_mutation_uses_original_root_and_verified_entry_descriptor(phase, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    fixture.write('lock', b'owned')
    fixture.mkdir('input')
    original_open, original_chmod, original_mkdir = subject.os.open, subject.os.fchmod, subject.os.mkdir
    swapped = []
    def swap_file():
        (root / 'lock').rename(root / 'old-lock'); (root / 'lock').write_bytes(b'foreign-keep')
    def swap_directory():
        (root / 'input').rename(root / 'old-input'); (root / 'input').mkdir(mode=0o700)
    def swap_root():
        root.rename(runs / 'held-root'); root.mkdir(); (root / 'user').write_bytes(b'foreign-root')
    def opened(path, flags, *args, **kw):
        if not swapped and phase in ('write-open', 'root-write', 'chmod-open') and kw.get('dir_fd') is not None and os.fstat(kw['dir_fd']).st_ino == os.fstat(fixture.owned.root).st_ino and str(path) == ('lock' if phase in ('write-open', 'root-write') else 'input'):
            if phase == 'write-open': swap_file()
            elif phase == 'chmod-open': swap_directory()
            elif phase == 'root-write': swap_root()
            swapped.append(True)
        return original_open(path, flags, *args, **kw)
    def chmod(handle, mode):
        if phase == 'chmod-after-open' and not swapped: swap_directory(); swapped.append(True)
        return original_chmod(handle, mode)
    def mkdir(path, *args, **kw):
        if phase == 'root-mkdir' and kw.get('dir_fd') == fixture.owned.root and not swapped:
            swap_root(); swapped.append(True)
        return original_mkdir(path, *args, **kw)
    monkeypatch.setattr(subject.os, 'open', opened)
    monkeypatch.setattr(subject.os, 'fchmod', chmod)
    monkeypatch.setattr(subject.os, 'mkdir', mkdir)
    try:
        if phase == 'chmod-after-open': fixture.chmod('input', Path('.'), 0o755)
        else:
            with pytest.raises(subject.vm.Refused):
                if phase == 'root-mkdir': fixture.mkdir('output')
                elif phase == 'chmod-open': fixture.chmod('input', Path('.'), 0o755)
                else: fixture.write('lock', b'rewritten')
        assert swapped
        if phase == 'write-open': assert (root / 'lock').read_bytes() == b'foreign-keep'
        elif phase.startswith('chmod'): assert (root / 'input').stat().st_mode & 0o777 == 0o700
        else: assert (root / 'user').read_bytes() == b'foreign-root' and not (root / 'output').exists()
    finally: fixture.close()


def test_runtime_recreation_checkpoint_publication_failure_preserves_consumers(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    removed = []
    monkeypatch.setattr(subject.vm, 'remove_owned', lambda *args: removed.append(args))
    first = OSError('checkpoint and final publication unavailable')
    monkeypatch.setattr(subject.vm, 'write_report', lambda *args: (_ for _ in ()).throw(first))
    with pytest.raises(OSError) as caught: subject.run_tests([], tmp_path / 'proof.json', recreate=True)
    assert caught.value is first and not removed and list(runs.iterdir())


def test_runtime_descriptor_copy_never_writes_into_replaced_root(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    source_vm = wrapper()
    monkeypatch.setattr(source_vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(source_vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'ROOT', subject.APP.parent)
    monkeypatch.setattr(subject.vm, 'mount_points', lambda: [])
    snapshot = source_vm.snapshot_input(subject.APP / 'tests')
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    mkdir = subject.os.mkdir
    swapped = []
    def swap(path, *args, **kw):
        if str(path) == 'input' and kw.get('dir_fd') == fixture.owned.root and not swapped:
            root.rename(runs / 'held-root'); root.mkdir(); (root / 'user').write_text('foreign')
            swapped.append(True)
        return mkdir(path, *args, **kw)
    monkeypatch.setattr(subject.os, 'mkdir', swap)
    try:
        with pytest.raises(subject.vm.Refused): subject.copy_into_fixture(subject.APP / 'tests', root / 'input', snapshot, fixture)
        assert (root / 'user').read_text() == 'foreign' and not (root / 'input').exists()
    finally: fixture.close()
def test_runtime_write_refuses_new_external_hardlink_before_truncate(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    try:
        fixture.write('lock', b'owned')
        foreign = tmp_path / 'external-user-alias'
        os.link(root / 'lock', foreign)
        with pytest.raises(subject.vm.Refused):
            fixture.write('lock', b'changed')
        assert foreign.read_bytes() == b'owned'
    finally:
        fixture.close()


def test_runtime_descriptor_copy_refuses_swapped_new_directory(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(subject, 'copy_preserving_modes', runtime_wrapper().copy_preserving_modes)
    source_vm = wrapper()
    monkeypatch.setattr(source_vm, 'ROOT', tmp_path.resolve())
    monkeypatch.setattr(source_vm, 'mount_points', lambda: [])
    monkeypatch.setattr(subject.vm, 'ROOT', tmp_path.resolve())
    monkeypatch.setattr(subject.vm, 'mount_points', lambda: [])
    source = tmp_path / 'source'; (source / 'nested').mkdir(parents=True)
    (source / 'nested/owned-file').write_text('owned')
    snapshot = source_vm.snapshot_input(source)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    foreign = tmp_path / 'user-empty-directory'; foreign.mkdir()
    foreign_identity = foreign.stat().st_ino
    opened = subject.os.open
    swapped = []
    def replace(path, flags, *args, **kwargs):
        if str(path) == 'nested' and flags & os.O_DIRECTORY and kwargs.get('dir_fd') is not None and (root / 'input/nested').exists() and not swapped:
            (root / 'input/nested').rename(tmp_path / 'saved-owned-directory')
            foreign.rename(root / 'input/nested')
            swapped.append(True)
        return opened(path, flags, *args, **kwargs)
    monkeypatch.setattr(subject.os, 'open', replace)
    try:
        with pytest.raises(subject.vm.Refused):
            subject.copy_preserving_modes(source, root / 'input', snapshot, fixture=fixture)
        assert swapped
        assert (root / 'input/nested').stat().st_ino == foreign_identity
        assert not (root / 'input/nested/owned-file').exists()
    finally:
        fixture.close()


@pytest.mark.parametrize('boundary', ['root', 'nested'])
def test_runtime_dependency_replacement_never_receives_mutations(boundary, tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    anchor, opened = subject.vm.OwnedDirectory, subject.os.open
    changed = []
    def acquire(path):
        owned = anchor(path)
        if boundary == 'root':
            path.rename(cache / 'held-original'); path.mkdir(mode=0o700); (path / 'user').write_text('foreign')
            changed.append(path)
        return owned
    def replace(path, flags, *args, **kw):
        roots = list(cache.glob('pytest-owned-*'))
        root = roots[0] if len(roots) == 1 else None
        parent = kw.get('dir_fd')
        if (boundary == 'nested' and str(path) == 'pytest' and flags & os.O_DIRECTORY and not changed
                and root is not None and (root / 'pytest').is_dir() and parent is not None
                and (os.fstat(parent).st_dev, os.fstat(parent).st_ino) == (root.stat().st_dev, root.stat().st_ino)):
            (root / 'pytest').rename(root / 'held-original'); (root / 'pytest').mkdir(mode=0o700)
            changed.append(root / 'pytest')
        return opened(path, flags, *args, **kw)
    monkeypatch.setattr(subject.vm, 'OwnedDirectory', acquire)
    monkeypatch.setattr(subject.os, 'open', replace)
    with pytest.raises(subject.vm.Refused): subject.pytest_dependencies()
    target = changed[0]
    assert target.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in target.iterdir()) == (['user'] if boundary == 'root' else [])


def test_runtime_mkdir_refuses_replacement_after_initial_receipt(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root)
    opened = subject.os.open
    def replace(path, flags, *args, **kw):
        if str(path) == 'output' and flags & os.O_DIRECTORY:
            (root / 'output').rename(root / 'held-output'); (root / 'output').mkdir(mode=0o700)
        return opened(path, flags, *args, **kw)
    monkeypatch.setattr(subject.os, 'open', replace)
    try:
        with pytest.raises(subject.vm.Refused): fixture.mkdir('output', dynamic=True)
        assert not list((root / 'output').iterdir()) and fixture.entries['output'][1]['.'][1] != (root / 'output').stat().st_ino
    finally: fixture.close()


@pytest.mark.parametrize('boundary', ['directory-parent', 'file-parent', 'chmod-parent'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('reuse_parent_number', [False, True])
def test_runtime_child_lifetime_encloses_failed_parent_close(boundary, error_type, reuse_parent_number, tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    opened, closed = subject.os.open, subject.os.close
    state = {}
    first = error_type('first parent close failure')
    fixture = None
    if boundary == 'chmod-parent':
        root = tmp_path / 'runtime'; root.mkdir(); fixture = subject.RuntimeFixture(root); fixture.mkdir('input')
    guard_path = tmp_path / 'external-fd-guard'; guard_path.write_bytes(b'keep')
    def acquire(path, flags, *args, **kw):
        handle = opened(path, flags, *args, **kw)
        parent = kw.get('dir_fd')
        target = 'input' if boundary == 'chmod-parent' else 'pytest' if boundary == 'directory-parent' else '__init__.py'
        if str(path) == target and parent is not None and 'child' not in state:
            valid = boundary == 'chmod-parent' and os.fstat(parent).st_ino == os.fstat(fixture.owned.root).st_ino
            roots = list(cache.glob('pytest-owned-*'))
            if boundary != 'chmod-parent' and len(roots) == 1:
                relative = roots[0] if boundary == 'directory-parent' else roots[0] / 'pytest'
                valid = relative.is_dir() and os.fstat(parent).st_ino == relative.stat().st_ino
            if valid: state.update(child=handle, child_identity=(os.fstat(handle).st_dev, os.fstat(handle).st_ino), parent=parent)
        return handle
    def close(handle):
        closed(handle)
        if handle == state.get('parent') and not state.get('failed'):
            state['failed'] = True
            if reuse_parent_number:
                guard = opened(guard_path, os.O_RDONLY)
                assert guard == handle
                state['guard'] = guard
            raise first
    monkeypatch.setattr(subject.os, 'open', acquire)
    monkeypatch.setattr(subject.os, 'close', close)
    try:
        with pytest.raises(error_type) as caught:
            if boundary == 'chmod-parent': fixture.chmod('input', Path('.'), 0o755)
            else: subject.pytest_dependencies()
        assert caught.value is first and state['failed']
        with pytest.raises(OSError): os.fstat(state['child'])
        if reuse_parent_number:
            assert os.read(state['guard'], 4) == b'keep'
        assert not list(cache.glob('pytest-owned-*'))
        if fixture is not None: fixture.validate()
    finally:
        if 'guard' in state: closed(state['guard'])
        if fixture is not None: fixture.remove(); fixture.close()


def test_runtime_chmod_primary_survives_receipt_and_close_errors(tmp_path, monkeypatch):
    subject, cache = dependency_fixture(tmp_path, monkeypatch)
    root = tmp_path / 'runtime'; root.mkdir(); fixture = subject.RuntimeFixture(root); fixture.mkdir('input')
    actual_stat, actual_close = subject.os.fstat, subject.os.close
    first, second = OSError('first chmod'), KeyboardInterrupt('secondary receipt')
    changed = []
    def chmod(*args): changed.append(True); raise first
    def receipt(handle):
        if changed and len(changed) == 1: changed.append(True); raise second
        return actual_stat(handle)
    monkeypatch.setattr(subject.os, 'fchmod', chmod)
    monkeypatch.setattr(subject.os, 'fstat', receipt)
    try:
        with pytest.raises(OSError) as caught: fixture.chmod('input', Path('.'), 0o755)
        assert caught.value is first and first.cleanup_errors[0]['failure']['type'] == 'KeyboardInterrupt'
    finally: fixture.remove(); fixture.close()


@pytest.mark.parametrize('lifecycle', ['fixture', 'dependency'])
@pytest.mark.parametrize('slot', ['root', 'parent'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('reuse', [False, True])
def test_runtime_creation_anchor_consumes_each_slot_before_close(lifecycle, slot, error_type, reuse, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    if lifecycle == 'fixture':
        root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root); anchor = fixture.owned
    else:
        destination = subject.pytest_dependencies(); anchor = subject.DEPENDENCY_OWNERS[destination]
    original = {'root': anchor.root, 'parent': anchor.parent}
    actual_close, actual_open = subject.os.close, subject.os.open
    guard_path = tmp_path / 'unrelated-anchor-guard'; guard_path.write_bytes(b'keep')
    first = error_type('first ' + slot + ' close')
    state, calls = {}, []
    def close(handle):
        if handle in original.values(): calls.append(handle)
        actual_close(handle)
        if handle == original[slot] and not state.get('failed'):
            state['failed'] = True
            assert anchor.root is None and anchor.parent is None
            if reuse:
                guard = actual_open(guard_path, os.O_RDONLY)
                if guard != handle:
                    os.dup2(guard, handle)
                    actual_close(guard)
                state['guard'] = handle
            raise first
    monkeypatch.setattr(subject.os, 'close', close)
    try:
        with pytest.raises(error_type) as caught:
            if lifecycle == 'fixture': subject.finish_runtime({'status': 'passed'}, tmp_path / 'proof.json', 'owned', fixture)
            else: subject.cleanup_dependencies(destination)
        assert caught.value is first and anchor.root is None and anchor.parent is None
        assert calls.count(original['root']) == calls.count(original['parent']) == 1
        if reuse: assert os.read(state['guard'], 4) == b'keep'
        if lifecycle == 'fixture':
            assert json.loads((tmp_path / 'proof.json').read_text())['status'] == 'failed'
            fixture.close()  # Idempotent; no retry of either consumed integer.
        else:
            assert not destination.exists() and subject.DEPENDENCY_OWNERS[destination] is anchor
            assert subject.DEPENDENCY_CLOSE_DEBT[destination]['namespace_status'] == 'removed'
            assert subject.DEPENDENCY_CLOSE_DEBT[destination]['anchor_slots_consumed']
            subject.close_runtime_anchor(anchor)
        assert calls.count(original['root']) == calls.count(original['parent']) == 1
    finally:
        if 'guard' in state: actual_close(state['guard'])


@pytest.mark.parametrize('lifecycle', ['fixture', 'dependency'])
def test_runtime_anchor_attempts_parent_after_root_failure_preserving_first(lifecycle, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    if lifecycle == 'fixture':
        root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root); anchor = fixture.owned
    else:
        destination = subject.pytest_dependencies(); anchor = subject.DEPENDENCY_OWNERS[destination]
    root_fd, parent_fd = anchor.root, anchor.parent
    first, second = KeyboardInterrupt('first root close'), OSError('secondary parent close')
    close = subject.os.close
    calls = []
    def fail(handle):
        close(handle); calls.append(handle)
        if handle == root_fd: raise first
        if handle == parent_fd: raise second
    monkeypatch.setattr(subject.os, 'close', fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        if lifecycle == 'fixture': fixture.close()
        else: subject.cleanup_dependencies(destination)
    assert caught.value is first and calls.count(root_fd) == calls.count(parent_fd) == 1
    assert anchor.root is None and anchor.parent is None
    assert any(row['failure']['error'] == str(second) for row in first.cleanup_errors)


def test_runtime_dependency_close_failure_is_durable_after_namespace_removal(tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    dependency = subject.pytest_dependencies(); anchor = subject.DEPENDENCY_OWNERS[dependency]
    root_fd = anchor.root
    monkeypatch.setattr(subject, 'pytest_dependencies', lambda: dependency)
    close = subject.os.close
    first = OSError('dependency close diagnostic')
    failed = []
    def fail(handle):
        close(handle)
        if handle == root_fd and not failed: failed.append(True); raise first
    monkeypatch.setattr(subject.os, 'close', fail)
    with pytest.raises(subject.vm.Refused): subject.run_tests([], tmp_path / 'proof.json')
    proof = json.loads((tmp_path / 'proof.json').read_text())
    debt = proof['cleanup']['dependencies']
    assert debt['status'] == 'removed' and debt['close_status'] == 'failed'
    assert debt['close_debt']['anchor_slots_consumed'] and debt['identity']
    assert debt['expected_files'] and str(first) in json.dumps(debt['close_debt'])
    assert anchor.root is None and anchor.parent is None and not dependency.exists()


@pytest.mark.parametrize('lifecycle', ['fixture', 'dependency'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt, SystemExit])
def test_runtime_fallback_finalizes_parent_only_anchor_without_reuse_collateral(lifecycle, error_type, tmp_path, monkeypatch):
    subject, runs, cache, docker = runtime_lifecycle_fixture(tmp_path, monkeypatch)
    if lifecycle == 'fixture':
        root = runs / 'runtime-owned'; root.mkdir(); fixture = subject.RuntimeFixture(root); fixture.remove(); anchor = fixture.owned
        dependency = None
    else:
        fixture = None; dependency = subject.pytest_dependencies(); anchor = subject.DEPENDENCY_OWNERS[dependency]; anchor.remove()
    close, opened = subject.os.close, subject.os.open
    close(anchor.root); anchor.root = None
    parent = anchor.parent
    first, second = OSError('first before fallback'), error_type('secondary parent close')
    guard_path = tmp_path / 'fallback-guard'; guard_path.write_bytes(b'keep')
    calls, state = [], {}
    def fail(handle):
        close(handle)
        if handle == parent:
            calls.append(handle)
            guard = opened(guard_path, os.O_RDONLY)
            if guard != handle: os.dup2(guard, handle); close(guard)
            state['guard'] = handle
            raise second
    monkeypatch.setattr(subject.os, 'close', fail)
    monkeypatch.setattr(subject, '_finish_runtime', lambda *args, **kw: (_ for _ in ()).throw(first))
    try:
        with pytest.raises(OSError) as caught: subject.finish_runtime({'status': 'passed'}, tmp_path / 'proof.json', 'owned', fixture, dependency)
        assert caught.value is first and calls == [parent]
        assert anchor.root is None and anchor.parent is None and os.read(state['guard'], 4) == b'keep'
        assert any(row['failure']['type'] == error_type.__name__ for row in first.cleanup_errors)
        if lifecycle == 'dependency':
            assert subject.DEPENDENCY_OWNERS[dependency] is anchor
            assert subject.DEPENDENCY_CLOSE_DEBT[dependency]['close_outcome'] == 'uncertain'
    finally: close(state['guard'])


@pytest.mark.parametrize('fault', ['none', 'symlink', 'fifo', 'missing'])
def test_ext4_machine_evidence_retained_before_owned_output_cleanup(tmp_path, fault):
    subject = wrapper()
    output = tmp_path / 'output'; output.mkdir()
    evidence_dir = tmp_path / 'evidence'; evidence_dir.mkdir()
    class Evidence:
        path = evidence_dir
        data = {}
    evidence = Evidence()
    machine = output / 'test-plan-allocation-0.json'
    if fault == 'none': machine.write_text('{"exitstatus":0}')
    if fault == 'symlink': machine.symlink_to(tmp_path / 'foreign')
    if fault == 'fifo': os.mkfifo(machine)
    if fault == 'none':
        subject.persist_machines(output, evidence, 'allocation', [{'id': 'owned-container'}])
        assert (evidence_dir / machine.name).read_text() == '{"exitstatus":0}'
        assert evidence.data['machine_records'] == [{'profile': 'allocation', 'attempt': 0, 'container': 'owned-container', 'file': machine.name}]
        machine.unlink(); output.rmdir()
        assert (evidence_dir / machine.name).is_file()
    else:
        with pytest.raises((subject.Refused, FileNotFoundError)):
            subject.persist_machines(output, evidence, 'allocation', [{'id': 'owned-container'}])


def test_ext4_machine_copy_runs_before_host_fixture_removal():
    source = SOURCE.read_text()
    body = source[source.index('def ext4_run('):source.index('def write_report(')]
    assert body.index('persist_machines(output') < body.index('owned.remove()') < body.index('evidence.finish(')
    assert 'return (0, evidence.path) if return_evidence else 0' in body


def test_real_descriptor_snapshot_has_bounded_json_evidence_before_fixture_allocation(tmp_path, monkeypatch):
    subject = wrapper()
    monkeypatch.setattr(subject, 'ROOT', tmp_path)
    monkeypatch.setattr(subject, 'HERE', tmp_path)
    monkeypatch.setattr(subject, 'RUNS', tmp_path / 'runs')
    monkeypatch.setattr(subject, 'mount_points', lambda: {tmp_path})
    monkeypatch.setattr(subject, 'capacity', lambda *args: None)
    monkeypatch.setattr(subject.os, 'getuid', lambda: 501)
    source = tmp_path / 'source'; source.mkdir()
    (source / 'nested').mkdir(); (source / 'nested/file').write_bytes(b'actual source')
    snapshot = subject.snapshot_input(source)
    assert () in snapshot['records'] and isinstance(snapshot['source'], Path)
    identity = subject.source_snapshot_evidence(snapshot)
    assert json.loads(json.dumps(identity)) == identity
    assert identity['entries'] == 3 and len(identity['manifest_sha256']) == 64
    assert len(json.dumps(identity)) < 1024
    assert subject.source_snapshot_evidence(snapshot) == identity
    def failed_evidence(root, metadata):
        assert metadata['source_snapshot'] == identity
        json.dumps(metadata)
        raise OSError('evidence init failed')
    monkeypatch.setattr(subject.guard, 'Evidence', failed_evidence)
    with pytest.raises(OSError, match='evidence init failed'):
        subject.ext4_run(source, ['fixture'])
    assert list(subject.RUNS.iterdir()) == []


@pytest.mark.parametrize('boundary', ['mkdir', 'mkdir-foreign', 'owner', 'owner-replaced'])
def test_ext4_real_startup_allocations_finalize_evidence_without_unanchored_deletion(tmp_path, monkeypatch, boundary):
    subject = wrapper()
    monkeypatch.setattr(subject, 'ROOT', tmp_path)
    monkeypatch.setattr(subject, 'HERE', tmp_path)
    monkeypatch.setattr(subject, 'RUNS', tmp_path / 'runs')
    monkeypatch.setattr(subject, 'DIAGNOSTICS', tmp_path / 'diagnostics')
    monkeypatch.setattr(subject, 'LOCAL', tmp_path / 'local')
    monkeypatch.setattr(subject, 'mount_points', lambda: {tmp_path})
    monkeypatch.setattr(subject, 'capacity', lambda *args: None)
    monkeypatch.setattr(subject.os, 'getuid', lambda: 501)
    source = tmp_path / 'source'; source.mkdir()
    (source / 'file').write_bytes(b'actual source')
    captured = []
    original_evidence = subject.guard.Evidence
    class RealEvidence(original_evidence):
        def __init__(self, root, metadata):
            super().__init__(root, metadata)
            captured.append(self)
    monkeypatch.setattr(subject.guard, 'Evidence', RealEvidence)
    original_mkdir = Path.mkdir
    primary = OSError('causal startup allocation failure')
    retained = []
    def mkdir(path, *args, **kwargs):
        if path.parent == subject.RUNS and boundary.startswith('mkdir'):
            if boundary == 'mkdir-foreign':
                original_mkdir(path, *args, **kwargs)
                (path / 'foreign').write_text('preserve')
            raise primary
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    def owner(path):
        if boundary == 'owner-replaced':
            moved = path.with_name(path.name + '-retained')
            path.rename(moved); retained.append(moved)
            original_mkdir(path)
            (path / 'foreign').write_text('preserve')
        raise primary
    monkeypatch.setattr(subject, 'OwnedDirectory', owner)
    monkeypatch.setattr(subject, 'docker', lambda *args, **kwargs: pytest.fail('startup failure reached Docker'))
    with pytest.raises(subject.Refused) as caught:
        subject.ext4_run(source, ['fixture'], report=tmp_path / 'report.json')
    assert caught.value.__cause__ is primary
    assert caught.value.failure['error'] == str(primary)
    evidence = captured[0]
    assert evidence.data['status'] == 'failed'
    assert evidence.data['primary_error']['error'] == str(primary)
    assert all(stream['file'].closed for stream in evidence.streams.values())
    assert evidence.sink.flags == {}
    final = json.loads((evidence.path / 'manifest.json').read_text())
    assert final['status'] == 'failed' and final['failure'] == 'OSError'
    if boundary == 'mkdir':
        assert list(subject.RUNS.iterdir()) == []
        assert final['cleanup_errors'] == [] and final['allocation']['created'] is False
    else:
        debt = final['allocation']['retained_unanchored']
        assert Path(debt).exists() and final['allocation']['anchored'] is False
        assert any(row['phase'] == 'host fixture unanchored' for row in final['cleanup_errors'])
        if boundary in ('mkdir-foreign', 'owner-replaced'):
            assert (Path(debt) / 'foreign').read_text() == 'preserve'
        assert all(path.exists() for path in retained)
