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
    with pytest.raises(subject.Refused, match="remove failed"):
        subject.generic_run(Path("input"), ["true"])
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
