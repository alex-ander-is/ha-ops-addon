#!/usr/bin/env python3
"""Local, profile-scoped Linux runner for disposable HA Ops fixtures."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ha-ops/tests/integration"))
from fixture_cleanup import OwnedDirectory

HERE = Path(__file__).resolve().parent
LOCAL = HERE / ".local"
RUNS = LOCAL / "runs"
PROFILE = "ha-ops-dev"
CPU = 2
MEMORY_GIB = 4
DISK_GIB = 12
ROOT_DISK_GIB = 8
GIB = 1024 ** 3
HOST_RESERVE = 8 * GIB
SETUP_HEADROOM = 2 * GIB
RUN_HEADROOM = 2 * GIB
VM_RESERVE_KIB = 2 * 1024 * 1024
SOCKET = Path.home() / ".colima" / PROFILE / "docker.sock"
ENDPOINT = "unix://" + str(SOCKET)
BUILD_INPUTS = {
    "Dockerfile": HERE / "Dockerfile",
    "toolchain.json": HERE / "toolchain.json",
    "requirements-dev.txt": ROOT / "requirements-dev.txt",
    "native_topology_helpers.py": ROOT / "ha-ops/tests/native_topology_helpers.py",
    "container_probe.py": ROOT / "ha-ops/tests/integration/container_probe.py",
}
TOOLCHAIN = {}


class Refused(RuntimeError):
    pass


class EndpointUnavailable(Refused):
    """The named local socket exists in no usable running engine."""


def invoke(args, *, env=None, input=None, timeout=180, check=True):
    result = subprocess.run(args, env=env, input=input, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise Refused(f"{' '.join(args[:3])} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def invoke_child(args, *, env, timeout):
    # A timed-out topology child may itself be waiting for a Docker CLI. Kill
    # the whole child process group before inspecting and cleaning containers.
    process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise Refused(f"Symlink path component: {component}")
    if any(char in str(path) for char in (",", "\n", "\r")):
        raise Refused("Path contains a Docker mount separator")
    return path


def capacity(path, required):
    free = shutil.disk_usage(path).free
    if free < required:
        raise Refused(f"Insufficient host space: {free // GIB} GiB free, {required // GIB} GiB required")
    return free


def colima(*args, timeout=600):
    environment = os.environ.copy()
    for name in ("COLIMA_HOME", "COLIMA_PROFILE", "LIMA_HOME"):
        environment.pop(name, None)
    return invoke(["colima", *args], env=environment, timeout=timeout).stdout


def guest(*args):
    return colima("ssh", "--profile", PROFILE, "--", *args, timeout=60)


def docker_env():
    env = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                 "BUILDX_BUILDER", "DOCKER_DEFAULT_PLATFORM"):
        env.pop(name, None)
    env["DOCKER_CONFIG"] = str(LOCAL / "docker-cli")
    return env


def active_context():
    environment = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        environment.pop(name, None)
    return invoke(["docker", "context", "show"], env=environment).stdout.strip()


def docker(*args, input=None, timeout=600, check=True):
    return invoke(["docker", "--host", ENDPOINT, *args], env=docker_env(), input=input,
                  timeout=timeout, check=check)


def profile_state():
    result = colima("list", "--json", timeout=30).decode()
    for line in result.splitlines():
        entry = json.loads(line)
        if entry.get("name") == PROFILE:
            return entry.get("status", "Unknown")
    return "Absent"


def guest_platform():
    guest_arch = guest("uname", "-m").decode().strip()
    mapping = {"aarch64": ("arm64", "linux/arm64"), "x86_64": ("x86_64", "linux/amd64")}
    if guest_arch not in mapping or platform.machine() != mapping[guest_arch][0]:
        raise Refused(f"Unexpected guest architecture {guest_arch} for host {platform.machine()}")
    return mapping[guest_arch][1]


def verify_image_arch(image, target_platform):
    observed = docker("image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}").stdout.decode().strip()
    if observed != target_platform:
        raise Refused(f"Reusable image architecture {observed} differs from {target_platform}")


def endpoint_info():
    no_symlinks(SOCKET)
    if not SOCKET.exists():
        raise EndpointUnavailable("Named Colima profile has no local Unix Docker socket")
    if not stat.S_ISSOCK(SOCKET.stat().st_mode):
        raise Refused("Named Colima profile has no local Unix Docker socket")
    # A Unix socket can be stale. The profile-specific path and Linux engine
    # must both pass before any Docker mutation.
    try:
        response = docker("info", "--format", "{{json .}}", timeout=30, check=False)
        if response.returncode:
            error = response.stderr.decode(errors="replace")
            if "cannot connect to the docker daemon" in error.lower() or "connection refused" in error.lower():
                raise EndpointUnavailable("Named Docker endpoint is not responding")
            raise Refused("Cannot verify named Docker endpoint: " + error.strip())
        data = json.loads(response.stdout)
    except (ValueError, KeyError) as error:
        raise Refused("Cannot verify local Docker engine") from error
    if data.get("OSType") != "linux":
        raise Refused("Named Docker endpoint is not a Linux engine")
    if not TOOLCHAIN:
        load_toolchain(make_context())
    if data.get("ServerVersion") != TOOLCHAIN["docker_engine"]:
        raise Refused("Docker engine version differs from pinned toolchain")
    name = data.get("Name", "")
    if PROFILE not in name:
        raise Refused(f"Docker engine name does not identify {PROFILE}: {name}")
    return data


def vm_capacity():
    lines = guest("df", "-Pk", "/var/lib/docker").decode().splitlines()
    try:
        free_kib = int(lines[-1].split()[3])
    except (IndexError, ValueError) as error:
        raise Refused("Cannot measure VM Docker space") from error
    if free_kib < VM_RESERVE_KIB:
        raise Refused(f"Insufficient VM Docker space: {free_kib // 1048576} GiB free")
    return free_kib * 1024


def share_probe():
    no_symlinks(RUNS)
    RUNS.mkdir(parents=True, exist_ok=True)
    probe = RUNS / ("preflight-" + uuid.uuid4().hex)
    probe.mkdir(mode=0o700)
    owned = OwnedDirectory(probe)
    try:
        verify_source(probe)
    finally:
        try:
            owned.remove()
        finally:
            owned.close()


def verify_source(source):
    import shlex
    source = no_symlinks(source)
    if not source.is_dir():
        raise Refused("Exact bind source is not a directory")
    prefix = ".vm-source-" + uuid.uuid4().hex
    host_path = source / (prefix + "-host")
    guest_path = source / (prefix + "-guest")
    try:
        host_bytes = os.urandom(32)
        host_path.write_bytes(host_bytes)
        if guest("cat", str(host_path)) != host_bytes:
            raise Refused("VM sees different bytes at the exact bind source")
        token = uuid.uuid4().hex.encode()
        command = f"printf %s {shlex.quote(token.decode())} > {shlex.quote(str(guest_path))}"
        guest("sh", "-c", command)
        if guest_path.read_bytes() != token:
            raise Refused("VM share is not writable or does not round-trip bytes")
    finally:
        host_path.unlink(missing_ok=True)
        guest_path.unlink(missing_ok=True)


def fingerprint(context=None):
    context = make_context() if context is None else context
    digest = hashlib.sha256()
    digest.update(platform.machine().encode())
    digest.update(platform.system().encode())
    digest.update(colima("version"))
    digest.update(invoke(["docker", "--version"]).stdout)
    digest.update(context)
    return digest.hexdigest()[:20]


def image_name():
    return "ha-ops-dev:" + fingerprint()


def image_present(image):
    result = docker("image", "inspect", image, check=False)
    if result.returncode and "No such image" not in result.stderr.decode(errors="replace"):
        raise Refused("Cannot inspect reusable image: " + result.stderr.decode(errors="replace"))
    return result.returncode == 0


def installed():
    if not TOOLCHAIN:
        load_toolchain(make_context())
    missing = [name for name in ("colima", "docker") if not shutil.which(name)]
    if missing:
        if not shutil.which("brew"):
            raise Refused("Homebrew is required to provision Colima and Docker CLI")
        environment = os.environ.copy()
        environment["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        available = json.loads(invoke(["brew", "info", "--json=v2", *missing], env=environment).stdout)
        formulae = {item["name"]: item["versions"]["stable"] for item in available["formulae"]}
        for name in missing:
            pin = TOOLCHAIN["docker_cli" if name == "docker" else "colima"]
            if formulae.get(name) != pin:
                raise Refused(f"Homebrew {name} {formulae.get(name)} differs from pinned {pin}; no install attempted")
        # Homebrew's dependency resolver installs the Lima/QEMU/VZ support used
        # by Colima; no global Docker context or service is changed here.
        invoke(["brew", "install", *missing], env=environment, timeout=1800)
    colima_output = colima("version").decode()
    docker_output = invoke(["docker", "--version"]).stdout.decode()
    if not re.search(r"\bversion " + re.escape(TOOLCHAIN["colima"]) + r"\b", colima_output):
        raise Refused("Colima version differs from pinned toolchain")
    if not re.search(r"\bversion " + re.escape(TOOLCHAIN["docker_cli"]) + r"\b", docker_output):
        raise Refused("Docker CLI version differs from pinned toolchain")


def start_vm():
    no_symlinks(LOCAL)
    LOCAL.mkdir(parents=True, exist_ok=True)
    no_symlinks(RUNS)
    RUNS.mkdir(parents=True, exist_ok=True)
    config = LOCAL / "docker-cli"
    no_symlinks(config)
    config.mkdir(mode=0o700, exist_ok=True)
    settings = config / "config.json"
    no_symlinks(settings)
    if not settings.exists():
        settings.write_text("{}\n")
    if settings.read_text() != "{}\n":
        raise Refused("Local Docker CLI config is unexpectedly modified")
    new_profile = not (Path.home() / ".colima" / PROFILE / "colima.yaml").exists()
    capacity(LOCAL, HOST_RESERVE + SETUP_HEADROOM + ((DISK_GIB + ROOT_DISK_GIB) * GIB if new_profile else 0))
    before = active_context()
    state = profile_state()
    if state == "Broken":
        try:
            endpoint_info()
        except EndpointUnavailable:
            # A genuinely broken profile can be stopped without deleting its
            # disk, then restarted with the same profile settings.
            colima("stop", PROFILE)
        except (Refused, OSError) as error:
            raise Refused("Cannot inspect named Colima profile; recovery refused: " + str(error)) from error
    colima("start", PROFILE, "--runtime", "docker", "--cpus", str(CPU),
           "--memory", str(MEMORY_GIB), "--disk", str(DISK_GIB), "--root-disk", str(ROOT_DISK_GIB),
           "--mount", f"{LOCAL}:w", "--activate=false", timeout=1200)
    after = active_context()
    if after != before:
        raise Refused("Colima unexpectedly changed the active Docker context")


def ready(*, for_build=False):
    host_free = capacity(LOCAL, HOST_RESERVE + (SETUP_HEADROOM if for_build else RUN_HEADROOM))
    info = endpoint_info()
    share_probe()
    vm_free = vm_capacity()
    return {"host_free_bytes": host_free, "vm_free_bytes": vm_free,
            "docker_engine": info.get("ServerVersion"), "docker_name": info.get("Name")}


def make_context():
    """Build one immutable tar stream; its bytes are hashed and sent to Docker."""
    contents = {}
    for name, source in sorted(BUILD_INPUTS.items()):
        source = no_symlinks(source)
        snapshot = snapshot_input(source)
        kind, signature = snapshot["records"][()]
        if kind != "file":
            raise Refused(f"Build input is not a regular file: {name}")
        handle = _open_input(source)
        try:
            if _input_signature(os.fstat(handle)) != signature:
                raise Refused(f"Build input changed before copying: {name}")
            chunks = []
            while True:
                block = os.read(handle, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
            if _input_signature(os.fstat(handle)) != signature:
                raise Refused(f"Build input changed while copying: {name}")
        finally:
            os.close(handle)
        check = _open_input(source)
        try:
            if _input_signature(os.fstat(check)) != signature or mount_points() != snapshot["mounts"]:
                raise Refused(f"Build input changed while copying: {name}")
        finally:
            os.close(check)
        contents[name] = b"".join(chunks)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in sorted(contents.items()):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o644
            item.mtime = 0
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


def load_toolchain(context):
    """Use the exact verified archive bytes that will be sent to Docker."""
    try:
        with tarfile.open(fileobj=io.BytesIO(context), mode="r:") as archive:
            source = archive.extractfile("toolchain.json")
            if source is None:
                raise Refused("Build archive has no toolchain.json")
            parsed = json.load(source)
        required = ("colima", "docker_cli", "docker_engine", "guest_git", "guest_git_debian")
        if not isinstance(parsed, dict) or any(not isinstance(parsed.get(name), str) for name in required):
            raise Refused("Invalid pinned toolchain data")
    except (tarfile.TarError, KeyError, ValueError, TypeError) as error:
        raise Refused("Cannot load pinned toolchain from verified build archive") from error
    TOOLCHAIN.clear()
    TOOLCHAIN.update(parsed)


def setup(*, exact_sources=()):
    context = make_context()
    load_toolchain(context)
    new_profile = not (Path.home() / ".colima" / PROFILE / "colima.yaml").exists()
    capacity(HERE, HOST_RESERVE + SETUP_HEADROOM + ((DISK_GIB + ROOT_DISK_GIB) * GIB if new_profile else 0))
    installed()
    start_vm()
    measurements = ready(for_build=True)
    # Check the actual fixture paths while the VM is available, before an
    # image build or smoke container can mutate Docker state.
    for source in exact_sources:
        verify_source(source)
    target_platform = guest_platform()
    build_fingerprint = fingerprint(context)
    image = "ha-ops-dev:" + build_fingerprint
    candidate = None
    build_error = None
    try:
        if not image_present(image):
            candidate = "ha-ops-dev:build-" + uuid.uuid4().hex
            docker("build", "--platform", target_platform, "--tag", candidate,
                   "-", input=context, timeout=1800)
            docker("tag", candidate, image)
    except BaseException as error:
        build_error = error
    cleanup_error = None
    if candidate:
        try:
            removed = docker("image", "rm", candidate, check=False)
            if image_present(candidate):
                detail = removed.stderr.decode(errors="replace").strip() if removed.returncode else "tag remains"
                raise Refused(detail)
        except (Refused, OSError, ValueError, subprocess.SubprocessError) as error:
            cleanup_error = error
    if cleanup_error:
        detail = f"Temporary image tag cleanup failed for {candidate}: {cleanup_error}"
        if build_error:
            detail = f"Image build failed: {build_error}; {detail}"
        raise Refused(detail) from cleanup_error
    if build_error:
        raise build_error
    # No bind mount is needed for this smoke test. A failed smoke test cannot
    # falsely certify the persistent image.
    verify_image_arch(image, target_platform)
    smoke = docker("run", "--rm", "--network", "none", "--platform", target_platform, image, "sh", "-c",
                   "python3 -c 'import pytest' && git --version", timeout=120).stdout.decode()
    if "git version " + TOOLCHAIN["guest_git"] not in smoke:
        raise Refused("Guest Git version differs from pinned toolchain")
    return {"image": image, "fingerprint": build_fingerprint, "platform": target_platform, **measurements,
            "colima": colima("version").decode().strip(),
            "docker_cli": invoke(["docker", "--version"]).stdout.decode().strip(),
            "git_host": invoke(["git", "--version"]).stdout.decode().strip(),
            "python_host": platform.python_version(), "host_arch": platform.machine(),
            "vm_arch": guest("uname", "-m").decode().strip(),
            "profile": PROFILE, "socket": ENDPOINT}


def owned_container(name, run_id):
    result = docker("container", "inspect", name, check=False)
    if result.returncode:
        if "no such" in result.stderr.decode(errors="replace").lower():
            return None
        raise Refused("Cannot inspect owned container")
    entry = json.loads(result.stdout)[0]
    if entry["Config"]["Labels"].get("ha-ops.dev.run") != run_id:
        raise Refused("Container name collision with another owner")
    container_id = entry.get("Id")
    if not container_id:
        raise Refused("Owned container has no immutable ID")
    return container_id


def remove_owned(name, run_id):
    container_id = owned_container(name, run_id)
    if not container_id:
        return
    failures = []
    errors = (OSError, RuntimeError, subprocess.SubprocessError, ValueError)
    try:
        docker("rm", "--force", container_id)
    except errors as error:
        failures.append(f"removal: {error}")
    try:
        if owned_container(container_id, run_id):
            failures.append("Owned container remains after removal")
    except errors as error:
        failures.append(f"inspection: {error}")
    if failures:
        raise Refused("Owned container cleanup failed: " + ", ".join(failures))


GENERATED_INPUTS = {".git", ".local", ".codex-audit", "node_modules", "__pycache__", ".pytest_cache", ".DS_Store"}


def mount_points():
    """Read the host mount table; a nested mount is outside the checkout."""
    result = invoke(["mount"], timeout=30).stdout.decode(errors="replace")
    points = set()
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        raise Refused(f"Unsupported host mount format: {system}")
    for line in result.splitlines():
        _, separator, tail = line.partition(" on ")
        if not separator or " (" not in tail:
            raise Refused("Cannot parse host mount boundaries")
        location = tail.rsplit(" (", 1)[0]
        if system == "Linux":
            # Linux adds `type FSTYPE`; Darwin reports the full path directly.
            location, delimiter, filesystem = location.rpartition(" type ")
            if not delimiter or not filesystem:
                raise Refused("Cannot parse Linux mount boundary")
        if not location.startswith("/"):
            raise Refused("Cannot parse host mount boundary path")
        points.add(Path(location.replace("\\040", " ")))
    if not points:
        raise Refused("Cannot verify host mount boundaries")
    return points


def _visible_children(handle):
    return tuple(sorted(name for name in os.listdir(handle) if name not in GENERATED_INPUTS))


def _reject_mount_crossings(source, points):
    for point in points:
        if point != ROOT and (point == ROOT or ROOT in point.parents):
            if point == source or point in source.parents or source in point.parents:
                raise Refused(f"Input crosses a mounted filesystem: {point}")


def _input_signature(entry):
    identity = (entry.st_dev, entry.st_ino, entry.st_mode)
    if stat.S_ISREG(entry.st_mode):
        return identity + (entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)
    return identity


def _open_directory_chain(path):
    """Open every absolute ancestor without following a replaceable symlink."""
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in Path(path).parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_input(source):
    current = _open_directory_chain(ROOT)
    try:
        for component in source.relative_to(ROOT).parts:
            before = os.stat(component, dir_fd=current, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise Refused(f"Symlink in input: {source}")
            if stat.S_ISDIR(before.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            elif stat.S_ISREG(before.st_mode):
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
            else:
                raise Refused(f"Input is not a regular file or directory: {source}")
            following = os.open(component, flags, dir_fd=current)
            if _input_signature(os.fstat(following)) != _input_signature(before):
                os.close(following)
                raise Refused(f"Input changed while opening: {source}")
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_input_child(parent, name, expected):
    kind, signature = expected
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= os.O_DIRECTORY if kind == "directory" else os.O_NONBLOCK
    child = os.open(name, flags, dir_fd=parent)
    if _input_signature(os.fstat(child)) != signature:
        os.close(child)
        raise Refused(f"Input changed after preflight: {name}")
    return child


def snapshot_input(source):
    """Record a safe source tree through descriptors anchored at the repo root."""
    source = no_symlinks(source)
    if source != ROOT and ROOT not in source.parents:
        raise Refused("Input must be inside this repository")
    parts = source.relative_to(ROOT).parts
    if any(part in GENERATED_INPUTS for part in parts):
        raise Refused("Generated input path is not allowed")
    mounts = mount_points()
    _reject_mount_crossings(source, mounts)
    records = {}
    children = {}
    total = 0
    root_device = ROOT.stat().st_dev

    def scan(handle, relative):
        nonlocal total
        entry = os.fstat(handle)
        if entry.st_dev != root_device:
            raise Refused(f"Input crosses a mounted filesystem: {relative}")
        if stat.S_ISREG(entry.st_mode):
            if entry.st_nlink != 1:
                raise Refused(f"Hard-linked input file is not allowed: {relative}")
            records[relative] = ("file", _input_signature(entry))
            total += entry.st_size
            return
        if not stat.S_ISDIR(entry.st_mode):
            raise Refused(f"Input is not a regular file or directory: {relative}")
        records[relative] = ("directory", _input_signature(entry))
        names = _visible_children(handle)
        children[relative] = names
        for name in names:
            before = os.stat(name, dir_fd=handle, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise Refused(f"Symlink in input: {source / Path(*relative) / name}")
            directory = stat.S_ISDIR(before.st_mode)
            if not directory and not stat.S_ISREG(before.st_mode):
                raise Refused(f"Input is not a regular file or directory: {name}")
            child = _open_input_child(handle, name,
                                      ("directory" if directory else "file", _input_signature(before)))
            try:
                location = relative + (name,)
                scan(child, location)
            finally:
                os.close(child)
        if _visible_children(handle) != names:
            raise Refused(f"Input directory changed during preflight: {relative}")

    try:
        handle = _open_input(source)
        try:
            scan(handle, ())
        finally:
            os.close(handle)
    except OSError as error:
        raise Refused(f"Input changed or cannot be opened safely: {error}") from error
    return {"source": source, "records": records, "children": children, "total": total,
            "mounts": mounts}


def validate_input(source):
    return snapshot_input(source)["total"]


def copy_input(source, destination, *, expected=None):
    source = no_symlinks(source)
    destination = no_symlinks(destination)
    snapshot = snapshot_input(source) if expected is None else expected
    if snapshot["source"] != source:
        raise Refused("Input snapshot does not match requested source")
    if mount_points() != snapshot["mounts"]:
        raise Refused("Host mounts changed after input preflight")
    _reject_mount_crossings(source, snapshot["mounts"])
    owned = None

    def copy_entry(handle, relative, target):
        kind, signature = snapshot["records"][relative]
        entry = os.fstat(handle)
        if _input_signature(entry) != signature:
            raise Refused(f"Input changed after preflight: {relative}")
        if kind == "file":
            with target.open("xb") as output:
                while True:
                    block = os.read(handle, 1024 * 1024)
                    if not block:
                        break
                    output.write(block)
            if _input_signature(os.fstat(handle)) != signature:
                raise Refused(f"Input changed during copy: {relative}")
            target.chmod(stat.S_IMODE(entry.st_mode) & 0o777)
            return
        if _visible_children(handle) != snapshot["children"][relative]:
            raise Refused(f"Input directory changed after preflight: {relative}")
        target.mkdir(mode=0o700)
        for name in snapshot["children"][relative]:
            location = relative + (name,)
            child = _open_input_child(handle, name, snapshot["records"][location])
            try:
                copy_entry(child, location, target / name)
            finally:
                os.close(child)
        if _visible_children(handle) != snapshot["children"][relative]:
            raise Refused(f"Input directory changed during copy: {relative}")

    try:
        handle = _open_input(source)
        try:
            if _input_signature(os.fstat(handle)) != snapshot["records"][()][1]:
                raise Refused("Input root changed after preflight")
            destination.mkdir(mode=0o700)
            owned = OwnedDirectory(destination)
            if snapshot["records"][()][0] == "file":
                copy_entry(handle, (), destination / source.name)
            else:
                if _visible_children(handle) != snapshot["children"][()]:
                    raise Refused("Input root directory changed after preflight")
                for name in snapshot["children"][()]:
                    location = (name,)
                    child = _open_input_child(handle, name, snapshot["records"][location])
                    try:
                        copy_entry(child, location, destination / name)
                    finally:
                        os.close(child)
                if _visible_children(handle) != snapshot["children"][()]:
                    raise Refused("Input root directory changed during copy")
        finally:
            os.close(handle)
    except BaseException as error:
        if owned is not None:
            try:
                owned.remove()
            finally:
                owned.close()
        if isinstance(error, OSError):
            raise Refused(f"Input changed or cannot be copied safely: {error}") from error
        raise
    try:
        if mount_points() != snapshot["mounts"]:
            owned.remove()
            raise Refused("Host mounts changed during input copy")
    finally:
        owned.close()


def generic_run(input_path, command):
    if not command:
        raise Refused("A command after -- is required")
    input_source = ROOT / input_path
    input_snapshot = snapshot_input(input_source)
    capacity(HERE, HOST_RESERVE + SETUP_HEADROOM + RUN_HEADROOM + input_snapshot["total"])
    no_symlinks(RUNS)
    RUNS.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    run_dir = RUNS / run_id
    run_dir.mkdir(mode=0o700)
    owned = OwnedDirectory(run_dir)
    name = "ha-ops-dev-" + run_id
    created_id = None
    create_attempted = False
    try:
        source = run_dir / "input"
        output = run_dir / "output"
        copy_input(input_source, source, expected=input_snapshot)
        output.mkdir()
        setup_data = setup(exact_sources=(source, output))
        args = ["create", "--name", name, "--label", f"ha-ops.dev.run={run_id}",
                "--network", "none", "--read-only", "--tmpfs", "/tmp",
                "--mount", f"type=bind,source={source},target=/workspace/input,readonly",
                "--mount", f"type=bind,source={output},target=/workspace/output",
                "--workdir", "/workspace/input", "--platform", setup_data["platform"],
                setup_data["image"], *command]
        create_attempted = True
        created_id = docker(*args).stdout.decode().strip()
        inspected = json.loads(docker("container", "inspect", created_id).stdout)[0]
        if inspected.get("Id") != created_id or inspected["Config"]["Labels"].get("ha-ops.dev.run") != run_id:
            raise Refused("Created container identity or owner changed")
        mounts = {row["Destination"]: row for row in inspected["Mounts"]}
        expected = {"/workspace/input": (source, False), "/workspace/output": (output, True)}
        if set(mounts) != set(expected) or any(
            mounts[key]["Type"] != "bind" or mounts[key]["Source"] != str(path) or mounts[key]["RW"] != writable
            for key, (path, writable) in expected.items()
        ):
            raise Refused("Container mount inspection failed")
        if inspected["HostConfig"]["NetworkMode"] != "none":
            raise Refused("Container network isolation failed")
        docker("start", created_id)
        result = docker("wait", created_id, timeout=1800)
        logs = docker("logs", created_id)
        sys.stdout.buffer.write(logs.stdout)
        sys.stderr.buffer.write(logs.stderr)
        code = int(result.stdout.strip())
        return code
    finally:
        try:
            if create_attempted:
                remove_owned(created_id or name, run_id)
            owned.remove()
        finally:
            owned.close()


def write_report(path, payload):
    if path is None:
        return
    path = no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_run(run_id, fixture, owned=None):
    failures = []
    names = ("ha-ops-dev-" + run_id, "native-topology-" + run_id + "-first",
             "native-topology-" + run_id + "-second")
    for name in names:
        try:
            remove_owned(name, run_id)
        except (Refused, OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(f"{name}: {error}")
    if failures:
        raise Refused("Exact run cleanup failed: " + "; ".join(failures))
    if owned is not None:
        owned.remove(missing_ok=True)
    elif fixture.exists() or fixture.is_symlink():
        no_symlinks(fixture)
        acquired = OwnedDirectory(fixture)
        try:
            acquired.remove()
        finally:
            acquired.close()


def topology(report):
    run_id = uuid.uuid4().hex
    fixture = RUNS / run_id
    proof_path = RUNS / ("proof-" + run_id + ".json")
    write_report(report, {"status": "started", "run_id": run_id})
    runner = ROOT / "ha-ops/tests/integration/run_native_topology.py"
    child_launched = False
    owned = None
    try:
        no_symlinks(RUNS)
        RUNS.mkdir(parents=True, exist_ok=True)
        fixture.mkdir(mode=0o700)
        owned = OwnedDirectory(fixture)
        sources = {name: fixture / name for name in ("homeassistant", "addon_configs", "data", "backup")}
        for source in sources.values():
            source.mkdir()
        sources["config"] = sources["addon_configs"] / "fixture_ha_ops"
        sources["config"].mkdir()
        setup_data = setup(exact_sources=tuple(sources.values()))
        ready()
        command = [sys.executable, str(runner), "--fixture-root", str(fixture),
                   "--image", setup_data["image"], "--skip-build", "--retain-image", "--run-id", run_id,
                   "--vm-profile", PROFILE, "--platform", setup_data["platform"],
                   "--report", str(proof_path)]
        env = docker_env()
        env["DOCKER_HOST"] = ENDPOINT
        child_launched = True
        result = invoke_child(command, env=env, timeout=1800)
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        if result.returncode:
            raise Refused("Real topology proof failed")
        proof = json.loads(proof_path.read_text())
        if proof.get("status") != "passed":
            raise Refused("Topology child did not report a passed proof")
        cleanup_run(run_id, fixture, owned)
        proof["local_environment"] = setup_data
        proof["image_id"] = docker("image", "inspect", setup_data["image"],
                                    "--format", "{{.Id}}").stdout.decode().strip()
        write_report(report, proof)
        return setup_data
    except BaseException as error:
        cleanup_error = None
        try:
            if child_launched:
                cleanup_run(run_id, fixture, owned)
            elif owned is not None:
                owned.remove()
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as failure:
            cleanup_error = failure
        write_report(report, {"status": "failed", "run_id": run_id,
                              "error": str(error), "cleanup_error": str(cleanup_error) if cleanup_error else None})
        if cleanup_error:
            raise Refused("Topology failed and exact cleanup failed: " + str(cleanup_error)) from error
        raise
    finally:
        if owned is not None:
            owned.close()
        proof_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    commands.add_parser("status")
    commands.add_parser("stop")
    top = commands.add_parser("topology")
    top.add_argument("--report", type=Path, default=LOCAL / "topology-report.json")
    run = commands.add_parser("run")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("program", nargs=argparse.REMAINDER)
    clean = commands.add_parser("cleanup")
    clean.add_argument("--run", required=True)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            print(json.dumps(setup(), indent=2, sort_keys=True))
        elif args.command == "topology":
            print(json.dumps(topology(args.report), indent=2, sort_keys=True))
        elif args.command == "run":
            program = args.program[1:] if args.program[:1] == ["--"] else args.program
            return generic_run(args.input, program)
        elif args.command == "status":
            if not shutil.which("colima"):
                print(json.dumps({"profile": PROFILE, "status": "absent"}))
            else:
                state = profile_state()
                if state == "Broken":
                    # Restricted process inspection can make Colima report a
                    # live VM as Broken. A verified named Docker endpoint is
                    # stronger evidence; access denial remains unverified.
                    try:
                        info = endpoint_info()
                    except (Refused, OSError) as error:
                        message = str(error).lower()
                        if "permission denied" in message or "operation not permitted" in message:
                            print(json.dumps({"profile": PROFILE, "status": "unverified",
                                              "reason": "Local process or socket access denied"}, indent=2))
                            return 0
                        print(json.dumps({"profile": PROFILE, "status": "broken"}, indent=2))
                        return 0
                elif state != "Running":
                    print(json.dumps({"profile": PROFILE, "status": state.lower()}, indent=2))
                    return 0
                else:
                    info = endpoint_info()
                print(json.dumps({"profile": PROFILE, "status": "running", "image": image_name(),
                                  "host_free_bytes": shutil.disk_usage(HERE).free,
                                  "vm_free_bytes": vm_capacity(),
                                  "docker_engine": info.get("ServerVersion"),
                                  "docker_name": info.get("Name")}, indent=2))
        elif args.command == "stop":
            colima("stop", PROFILE)
        else:
            if not args.run or any(char not in "0123456789abcdef" for char in args.run) or len(args.run) != 32:
                raise Refused("Cleanup requires an exact 32-character run ID")
            endpoint_info()
            cleanup_run(args.run, RUNS / args.run)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        print(f"Local runner refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
