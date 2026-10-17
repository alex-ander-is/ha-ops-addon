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


def generic_run(input_path, command, recreate=False):
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
        for attempt in range(2 if recreate else 1):
            args = ["create", "--name", name, "--label", f"ha-ops.dev.run={run_id}",
                    "--network", "none", "--read-only", "--tmpfs", "/tmp:exec",
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
            if code != 0:
                return code
            if recreate and attempt == 0:
                remove_owned(created_id, run_id)
                created_id = None
                create_attempted = False
        return code
    finally:
        try:
            if create_attempted:
                remove_owned(created_id or name, run_id)
            owned.remove()
        finally:
            owned.close()


EXT4_HELPER = HERE / "ext4_fixture.py"
EXT4_SIZES = {"allocation": 256 * 1024**2, "bytes": 256 * 1024**2, "inodes": 64 * 1024**2}


EXT4_ROWS = {
    'allocation': {'loose-index-worktree-save', 'controls', 'independent-copies', 'repack', 'short-eio', 'recreation'},
    'bytes': {'byte-enospc', 'recreation'}, 'inodes': {'inode-enospc', 'recreation'},
}


def validate_ext4_proof(proof, *, run_id, profile, attempt, token, fixture, previous=None):
    """Validate measured obligations and the current publication, not its flag."""
    def require(condition, message):
        if not condition: raise Refused('Ext4 proof invalid: ' + message)
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum
    def measurement(row, family, outputs=()):
        require(isinstance(row, dict) and row.get('family') == family, 'measurement family ' + family)
        for name in ('physical_bytes', 'logical_bytes', 'new_inodes', 'additional_peak', 'required_inodes', 'reserve'):
            require(integer(row.get(name)), 'measurement integer ' + name)
        for name in ('blocks_delta', 'free_bytes_delta', 'free_inodes_delta'):
            require(type(row.get(name)) is int, 'measurement delta ' + name)
        # Every held family here produces nonempty regular files and new
        # inodes. Forecast membership cannot stand in for observed demand.
        quantum = proof['quantum']
        require(row['logical_bytes'] > 0 and row['new_inodes'] > 0 and row['free_inodes_delta'] > 0,
                'empty produced byte/inode evidence ' + family)
        require(row['blocks_delta'] >= quantum and row['free_bytes_delta'] >= quantum and
                row['blocks_delta'] % quantum == 0 and row['free_bytes_delta'] % quantum == 0,
                'empty or inconsistent allocated observation ' + family)
        require(row['physical_bytes'] == max(0, row['blocks_delta'], row['free_bytes_delta']) and
                row['logical_bytes'] <= row['physical_bytes'], 'inconsistent physical demand')
        require(row['physical_bytes'] <= row['additional_peak'] and
                max(row['new_inodes'], row['free_inodes_delta']) <= row['required_inodes'], 'forecast overrun')
        allocations = row.get('allocations')
        require(isinstance(allocations, list) and allocations, 'allocation terms absent')
        for allocation in allocations:
            require(isinstance(allocation, dict) and isinstance(allocation.get('output'), str) and
                    all(integer(allocation.get(k)) for k in ('logical', 'allocated', 'inodes')), 'allocation terms malformed')
        expected_volume = [fixture['mount_device_number'], '65787434', fixture['loop']['name'].encode().hex()]
        require(all(a.get('volume') == expected_volume for a in allocations), 'forecast volume differs from actual ext4')
        require(row['physical_bytes'] % 512 == 0 and all(a['logical'] <= a['allocated'] for a in allocations), 'physical allocation terms')
        require(sum(a['allocated'] for a in allocations) == row['additional_peak'] and
                sum(a['inodes'] for a in allocations) + 2 * len({json.dumps(a.get('volume')) for a in allocations}) == row['required_inodes'], 'forecast terms inconsistent')
        names = [a['output'] for a in allocations]
        require(all(any(n.startswith(prefix) for n in names) for prefix in outputs), 'mandatory recipe terms absent')
    def recovery(rows):
        require(isinstance(rows, list), 'addressed recovery list')
        for row in rows:
            require(isinstance(row, dict) and row.get('status') in {'removed', 'absent', 'debt'}, 'addressed recovery outcome')
            require(all(isinstance(row.get(k), str) and re.fullmatch(r'[0-9a-f-]{36}', row[k]) for k in ('resource_id', 'owner', 'operation')), 'addressed recovery identity')
            require(isinstance(row.get('errors'), list), 'addressed recovery diagnostics')
            if row['status'] == 'debt':
                retained = row.get('retained')
                require(row['errors'] and isinstance(retained, dict) and retained.get('resource_id') == row['resource_id'] and
                        retained.get('owner') == row['owner'] and retained.get('operation') == row['operation'], 'unaddressed retained debt')
    require(isinstance(proof, dict), 'report object')
    require(type(proof.get('schema')) is int and proof['schema'] == 1 and proof.get('status') == 'passed' and proof.get('run') == run_id and
            proof.get('profile') == profile and type(proof.get('attempt')) is int and proof['attempt'] == attempt and
            proof.get('attempt_token') == token, 'current attempt identity')
    require(proof.get('filesystem') == 'ext4' and proof.get('device') == fixture['mount']['device'] and
            proof.get('quantum') == 4096 and proof.get('total_inodes') == fixture['total_inodes'], 'mounted profile')
    rows = proof.get('rows')
    require(isinstance(rows, dict) and set(rows) == EXT4_ROWS[profile] and all(isinstance(r, dict) for r in rows.values()), 'exact mandatory row set')
    recreation = rows['recreation']; sentinel = recreation.get('sentinel')
    require(type(recreation.get('attempt')) is int and recreation['attempt'] == attempt and
            isinstance(sentinel, dict) and set(sentinel) == {'device', 'inode', 'sha256'} and
            integer(sentinel.get('device'), 1) and integer(sentinel.get('inode'), 1) and
            isinstance(sentinel.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', sentinel['sha256']), 'fresh recreation sentinel')
    require(sentinel['device'] == fixture['mount_device_number'], 'sentinel device differs from ext4')
    if previous is not None:
        require(attempt == previous['attempt'] + 1 and sentinel == previous['rows']['recreation']['sentinel'], 'recreated sentinel persistence')
        require(all(row == previous['rows'][name] for name, row in rows.items() if name != 'recreation'), 'retained matrix changed on recreation')
    if profile == 'allocation':
        scope = rows['loose-index-worktree-save']; samples = scope.get('measurements')
        require(isinstance(samples, list) and len(samples) == 3, 'complete held scope phases')
        families = ('loose-spool-fanout', 'complete-index-lock-trees', 'worktree-admin-materialization-save-ref-reflog')
        recipe = ('stream-spool:', 'loose-blob:', 'complete-index', 'index-lock-overlap', 'generated-tree:',
                  'worktree-control:', 'commit-object', 'ref-atomic-output', 'ref-lock-overlap', 'full-reflog-copy-append', 'reflog-atomic-overlap')
        for sample, family in zip(samples, families): measurement(sample, family, recipe)
        sizes = [a['logical'] for a in samples[0]['allocations'] if a['output'].startswith('stream-spool:')]
        require(len(sizes) == scope.get('selected_entries') and {0, 1, 4095, 4096, 4097, 1024**2} <= set(sizes) and sizes.count(1) >= 96 and
                sum(a['output'].startswith('loose-blob:') for a in samples[0]['allocations']) == len(sizes), 'complete selected boundary/tiny recipe')
        require(all(all(later[k] >= earlier[k] for k in ('logical_bytes', 'physical_bytes', 'blocks_delta',
                'free_bytes_delta', 'new_inodes', 'free_inodes_delta')) for earlier, later in zip(samples, samples[1:])),
                'held scope observations decreased between phases')
        require(all(all(sample[k] == samples[0][k] for k in ('additional_peak', 'required_inodes', 'reserve', 'allocations')) for sample in samples), 'scope forecast replaced between phases')
        require(type(scope.get('snapshot_count')) is int and scope['snapshot_count'] == 1 and type(scope.get('forecast_count')) is int and scope['forecast_count'] == 1 and type(scope.get('phase_rescans')) is int and scope['phase_rescans'] == 0 and
                integer(scope.get('selected_entries'), 102) and integer(scope.get('unselected_entries'), 1), 'scope/phase coverage')
        existing = scope.get('existing_reflog'); measurement(existing, 'existing-complete-reflog-copy-append-ref-lock', ('full-reflog-copy-append', 'reflog-atomic-overlap'))
        require(integer(existing.get('retained_log_bytes'), 1) and existing['logical_bytes'] >= existing['retained_log_bytes'], 'full existing reflog absent')
        controls = rows['controls']; samples = controls.get('measurements')
        require(isinstance(samples, list) and len(samples) == 1 and controls.get('prior_control_retained_during_replace') is True and
                controls.get('full_pending_failure_envelope') is True, 'full control overlap')
        measurement(samples[0], 'full-old-new-control-temp-diff', ('full-prospective-operation-state', 'full-pending-control', 'exact-diff-artifact'))
        copies = rows['independent-copies']; measurement(copies, 'two-independent-copies-metadata-replacement', ('independent-copy:', 'exact-encoded-metadata:', 'replacement-temporary:'))
        require(sum(a['output'].startswith('independent-copy:') for a in copies['allocations']) == 2, 'two independent copy recipes')
        repack = rows['repack']; measurement(repack, 'all-object-kinds-old-pack-new-pack-index-full-temp', ('new-pack-old-packs-retained', 'pack-index', 'pack-temporary-overlap'))
        require(integer(repack.get('objects'), 5) and repack.get('kinds') == ['blob', 'tree', 'commit', 'tag'] and repack.get('unreachable') is True and
                repack.get('argv') == ['pack-objects', '--window=0', '--depth=0', '--no-reuse-object', '--no-reuse-delta'], 'complete no-reuse repack profile')
        faults = rows['short-eio']; persistence = faults.get('owner_persistence')
        expected = {'artifact_written', 'artifact_fsynced', 'artifact_replaced', 'state_written', 'state_fsynced', 'state_replaced',
                    'artifact-fsync', 'state-fsync', 'state-directory-fsync-after-replace'}
        require(faults.get('positive_short_bytes') == 8193 and faults.get('zero_progress_refused') is True and faults.get('copy_sink_eio_preserved') is True and
                isinstance(persistence, list) and len(persistence) == len(expected) and all(isinstance(r, dict) for r in persistence) and
                {r.get('boundary') for r in persistence} == expected, 'mandatory short/persistence faults')
        for row in persistence:
            require(row.get('first_error_preserved') is True and integer(row.get('prior_revision')) and integer(row.get('published_revision')), 'first persistence failure evidence')
            increment = 1 if row['boundary'] in {'state_replaced', 'state-directory-fsync-after-replace'} else 0
            require(row['published_revision'] == row['prior_revision'] + increment, 'publication reconciliation evidence')
        materialization = faults.get('materialization_sink_eio')
        require(isinstance(materialization, dict) and materialization.get('first_error_preserved') is True and materialization.get('copy_blob_entered') is True and
                materialization.get('selected_path') == 'homeassistant/file' and integer(materialization.get('partial_bytes'), 1) and
                integer(materialization.get('expected_size'), materialization['partial_bytes'] + 1) and
                isinstance(materialization.get('sink_path'), str) and materialization['sink_path'].endswith('/tree/homeassistant/file') and
                isinstance(materialization.get('partial_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', materialization['partial_sha256']) and
                isinstance(materialization.get('reached_bytes'), list) and len(materialization['reached_bytes']) == 1 and
                integer(materialization['reached_bytes'][0], materialization['partial_bytes']), 'actual selected materialization sink')
        recovery(materialization.get('addressed_recovery'))
    else:
        row = rows['byte-enospc' if profile == 'bytes' else 'inode-enospc']
        require(row.get('kernel_errno') == 28 and row.get('state_preserved') is True, 'kernel failure/preservation evidence')
        recovery(row.get('recovered_resources'))
        if profile == 'bytes':
            require(integer(row.get('written_bytes'), 1) and row['written_bytes'] <= EXT4_SIZES[profile], 'bounded byte exhaustion')
            failures = row.get('failures'); expected = {'preview-payload', 'blob-payload', 'materialization', 'state-atomic-publication-after-durable-artifact'}
            require(isinstance(failures, list) and len(failures) == 4 and all(isinstance(r, dict) for r in failures) and
                    {r.get('boundary') for r in failures} == expected, 'mandatory byte failure boundaries')
            require(all(r.get('errno') == 28 and isinstance(r.get('error'), str) and r['error'] for r in failures), 'actual byte ENOSPC errors')
        else:
            require(row.get('observed_total_inodes') == fixture['total_inodes'] and integer(row.get('created_inodes'), 1) and
                    row['created_inodes'] <= fixture['total_inodes'] + 8 and integer(row.get('remaining_bytes'), 16 * 1024**2), 'isolated inode exhaustion with free bytes')
    return proof


def failure_payload(error):
    payload = {'error': str(error), 'type': type(error).__name__, 'errno': getattr(error, 'errno', None),
               'cleanup_errors': list(getattr(error, 'cleanup_errors', ())), 'guest_failure': getattr(error, 'guest_failure', None)}
    for name in ('filename', 'filename2', 'failure', 'report_failure', 'guest_process'):
        value = getattr(error, name, None)
        if value is not None: payload[name] = os.fspath(value) if isinstance(value, Path) else value
    if isinstance(error, subprocess.SubprocessError):
        for name in ('cmd', 'returncode', 'timeout', 'stdout', 'stderr'):
            value = getattr(error, name, None)
            if value is not None: payload[name] = value.decode(errors='replace') if isinstance(value, bytes) else value
    return payload


def ext4_guest(action, run_id, profile="allocation", *, owner=None):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id) or profile not in EXT4_SIZES:
        raise Refused("Exact ext4 run identity/profile required")
    if owner is not None and owner != (65534, 65534):
        raise Refused('Only the explicit existing nobody runtime test owner is supported')
    uid, gid = (os.getuid(), os.getgid()) if owner is None else owner
    # The source is sent over stdin, never installed into the guest or image.
    environment = os.environ.copy()
    for key in ("COLIMA_HOME", "COLIMA_PROFILE", "LIMA_HOME"):
        environment.pop(key, None)
    result = invoke(["colima", "ssh", "--profile", PROFILE, "--", "sudo", "-n",
                     "/usr/bin/python3", "-", action, "--run", run_id, "--profile", profile,
                     "--uid", str(uid), "--gid", str(gid)],
                    input=EXT4_HELPER.read_bytes(), env=environment, timeout=180, check=False)
    if result.returncode:
        stderr = result.stderr.decode(errors="replace")
        payload = None
        for line in stderr.splitlines():
            if line.startswith('EXT4_FAILURE '):
                try: payload = json.loads(line[len('EXT4_FAILURE '):])
                except ValueError: pass
        error = Refused(payload['error'] if payload else stderr.strip())
        error.guest_failure = payload
        error.errno = payload.get('errno') if payload else None
        error.guest_process = {'returncode': result.returncode, 'stdout': result.stdout.decode(errors='replace'), 'stderr': stderr}
        error.cleanup_errors = tuple(payload.get('cleanup_errors', ())) if payload else ()
        raise error
    return json.loads(result.stdout)


def verify_ext4_container(inspected, container_id, run_id, source, output, fixture, user):
    if inspected.get("Id") != container_id or inspected.get("Config", {}).get("Labels", {}).get("ha-ops.dev.run") != run_id:
        raise Refused("Created ext4 container identity or owner changed")
    config, host = inspected["Config"], inspected["HostConfig"]
    if config.get("User") != user or user.split(":")[0] == "0":
        raise Refused("Ext4 application must run as the exact nonroot user")
    if host.get("Privileged") or host.get("Devices") or host.get("CapAdd") or set(host.get("CapDrop") or ()) != {"ALL"}:
        raise Refused("Ext4 application privilege boundary failed")
    if "no-new-privileges" not in (host.get("SecurityOpt") or ()) or not host.get("ReadonlyRootfs") or host.get("NetworkMode") != "none":
        raise Refused("Ext4 application security boundary failed")
    rows = inspected.get("Mounts", [])
    mounts = {row["Destination"]: row for row in rows}
    expected = {"/workspace/input": (str(source), False), "/workspace/output": (str(output), True),
                "/workspace/ext4": (fixture["mount_path"], True)}
    if len(rows) != 3 or set(mounts) != set(expected) or any(
        mounts[key].get("Type") != "bind" or mounts[key].get("Source") != path or mounts[key].get("RW") != rw or
        mounts[key].get("Propagation") != "rprivate" for key, (path, rw) in expected.items()):
        raise Refused("Exact private ext4 container mounts failed")


def ext4_run(input_path, command, *, profile="allocation", recreate=False, report=None):
    if not command or os.getuid() == 0 or profile not in EXT4_SIZES:
        raise Refused("Ext4 needs a command, bounded profile and nonroot application UID")
    input_source = ROOT / input_path
    snapshot = snapshot_input(input_source)
    capacity(HERE, HOST_RESERVE + RUN_HEADROOM + EXT4_SIZES[profile] + snapshot["total"])
    no_symlinks(RUNS); RUNS.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    run_dir = RUNS / run_id
    run_dir.mkdir(mode=0o700)
    owned = OwnedDirectory(run_dir)
    names, first, failures = [], None, []
    cleanup_causes = []
    def cleanup_failure(phase, error):
        failures.append({'phase': phase, 'failure': failure_payload(error)})
        cleanup_causes.append(error)
    result = {"status": "started", "run_id": run_id, "profile": profile, "containers": []}
    setup_attempted = False
    try:
        source, output = run_dir / "input", run_dir / "output"
        copy_input(input_source, source, expected=snapshot); output.mkdir(mode=0o700)
        # This opt-in route requires the reusable VM/image; it never builds or
        # downloads dependencies as an incidental part of a disk proof.
        missing = [tool for tool in ("colima", "docker") if not shutil.which(tool)]
        if missing: raise Refused("Ext4 requires existing tools; no installation attempted: " + ", ".join(missing))
        context = make_context(); load_toolchain(context); installed()
        measurements = ready()
        for path in (source, output): verify_source(path)
        target_platform = guest_platform()
        image = "ha-ops-dev:" + fingerprint(context)
        if not image_present(image): raise Refused("Ext4 requires the existing reusable image; run setup separately")
        verify_image_arch(image, target_platform)
        image_id = docker("image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip()
        result.update(local_environment=measurements, image=image, image_id=image_id,
                      guest_preflight=ext4_guest("preflight", run_id, profile))
        setup_attempted = True
        fixture = ext4_guest("setup", run_id, profile)
        result["fixture"] = fixture
        user = str(os.getuid()) + ":" + str(os.getgid())
        previous = None
        for attempt in range(2 if recreate else 1):
            attempt_token = uuid.uuid4().hex
            proof_name = 'ext4-proof-' + attempt_token + '.json'
            prior_name = 'ext4-prior-' + attempt_token + '.json'
            proof_path = output / proof_name
            if proof_path.exists(): raise Refused('Unexpected current-attempt report already exists')
            if previous is not None: write_report(output / prior_name, previous)
            observed = ext4_guest("observe", run_id, profile)
            if observed != fixture: raise Refused("Ext4 identity changed before container creation")
            name = "ha-ops-dev-" + run_id + "-ext4-" + str(attempt)
            names.append(name)
            args = ["create", "--name", name, "--label", "ha-ops.dev.run=" + run_id,
                    "--network", "none", "--read-only", "--tmpfs", "/tmp", "--user", user,
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--mount", f"type=bind,source={source},target=/workspace/input,readonly,bind-propagation=rprivate",
                    "--mount", f"type=bind,source={output},target=/workspace/output,bind-propagation=rprivate",
                    "--mount", f"type=bind,source={fixture['mount_path']},target=/workspace/ext4,bind-propagation=rprivate",
                    "--env", "HA_OPS_EXT4_ROOT=/workspace/ext4", "--env", "HA_OPS_EXT4_PROFILE=" + profile,
                    "--env", "HA_OPS_EXT4_RUN=" + run_id, "--env", "HA_OPS_EXT4_DEVICE=" + fixture["mount"]["device"],
                    "--env", "HA_OPS_EXT4_REPORT=/workspace/output/" + proof_name,
                    "--env", "HA_OPS_EXT4_PRIOR_REPORT=/workspace/output/" + prior_name,
                    "--env", "HA_OPS_EXT4_TOKEN=" + attempt_token,
                    "--env", "HA_OPS_EXT4_ATTEMPT=" + str(attempt), "--workdir", "/workspace/input",
                    "--platform", target_platform, image, *command]
            container_id = docker(*args).stdout.decode().strip()
            inspected = json.loads(docker("container", "inspect", container_id).stdout)[0]
            verify_ext4_container(inspected, container_id, run_id, source, output, fixture, user)
            result["containers"].append({"id": container_id, "name": name, "security": inspected["HostConfig"], "mounts": inspected["Mounts"]})
            docker("start", container_id)
            code = int(docker("wait", container_id, timeout=1800).stdout.strip())
            logs = docker("logs", container_id)
            sys.stdout.buffer.write(logs.stdout); sys.stderr.buffer.write(logs.stderr)
            if proof_path.exists():
                proof = json.loads(proof_path.read_text())
                result.setdefault("proofs", []).append(proof)
            else:
                proof = None
            if code:
                result["command_failure_logs"] = (logs.stdout + logs.stderr)[-65536:].decode(errors="replace")
                raise Refused("Ext4 proof command failed with exit " + str(code))
            if proof is None: raise Refused("Ext4 proof report is absent")
            validate_ext4_proof(proof, run_id=run_id, profile=profile, attempt=attempt, token=attempt_token, fixture=fixture, previous=previous)
            previous = proof
            remove_owned(container_id, run_id)
        if docker("image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip() != image_id:
            raise Refused("Reusable image identity changed")
    except BaseException as error:
        first = error
    finally:
        # Try every exact removal independently; backing is retained if even
        # one absence is not proven. No unmount below a surviving container.
        for name in names:
            try: remove_owned(name, run_id)
            except BaseException as error: cleanup_failure("container " + name, error)
        if not failures and setup_attempted:
            try:
                result["cleanup"] = ext4_guest("cleanup", run_id, profile)
                result["cleanup_repeat"] = ext4_guest("cleanup", run_id, profile)
                result["image_id_after_cleanup"] = docker("image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip()
                if result["image_id_after_cleanup"] != image_id: raise Refused("Reusable image changed during cleanup")
                result["vm_after_cleanup"] = endpoint_info().get("Name")
            except BaseException as error: cleanup_failure("guest fixture", error)
        if not failures:
            try: owned.remove()
            except BaseException as error: cleanup_failure("host fixture", error)
        try: owned.close()
        except BaseException as error: cleanup_failure("host descriptor", error)
    body_failure = failure_payload(first) if first is not None else None
    if first is None and cleanup_causes:
        first = cleanup_causes[0]
    result.update(status="failed" if first is not None else "passed", error=str(first) if first is not None else None,
                  cleanup_errors=failures, failure=failure_payload(first) if first is not None else None,
                  body_failure=body_failure)
    # Completion is published only after the exact cleanup verification.
    try: write_report(report or LOCAL / ("ext4-" + profile + "-report.json"), result)
    except BaseException as error:
        cleanup_failure("report persistence", error)
        if first is None: first = error
        result.update(status="failed", error=str(first), failure=failure_payload(first),
                      report_failure=failure_payload(error))
    if first is not None:
        diagnostics = {key: result.get(key) for key in ('error', 'failure', 'body_failure', 'cleanup_errors', 'report_failure')}
        refused = Refused("Ext4 incomplete: " + json.dumps(diagnostics, sort_keys=True))
        refused.failure = result['failure']
        refused.cleanup_errors = tuple(failures)
        refused.report_failure = result.get('report_failure')
        refused.report = result
        raise refused from first
    return 0


def write_report(path, payload):
    if path is None:
        return
    path = no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    first = None
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException as error:
        first = error
    try: temporary.unlink(missing_ok=True)
    except BaseException as error:
        if first is None: first = error
        else: first.cleanup_errors = tuple(getattr(first, 'cleanup_errors', ())) + ({'phase': 'report temporary unlink', 'failure': failure_payload(error)},)
    if first is not None: raise first


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
    run.add_argument("--recreate", action="store_true", help="Repeat command in a new container with the same output bind")
    run.add_argument("--ext4", action="store_true", help="Use the bounded guest-native ext4 fixture")
    run.add_argument("--ext4-profile", choices=EXT4_SIZES, default="allocation")
    run.add_argument("--report", type=Path)
    run.add_argument("program", nargs=argparse.REMAINDER)
    clean = commands.add_parser("cleanup")
    clean.add_argument("--run", required=True)
    clean.add_argument("--ext4", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            print(json.dumps(setup(), indent=2, sort_keys=True))
        elif args.command == "topology":
            print(json.dumps(topology(args.report), indent=2, sort_keys=True))
        elif args.command == "run":
            program = args.program[1:] if args.program[:1] == ["--"] else args.program
            if args.ext4:
                return ext4_run(args.input, program, profile=args.ext4_profile, recreate=args.recreate, report=args.report)
            if args.report or args.ext4_profile != "allocation":
                raise Refused("Ext4 options require --ext4")
            return generic_run(args.input, program, recreate=args.recreate)
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
            if args.ext4:
                cleanup_failures, cleanup_primary = [], None
                for attempt in range(2):
                    try: remove_owned("ha-ops-dev-" + args.run + "-ext4-" + str(attempt), args.run)
                    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                        if cleanup_primary is None: cleanup_primary = error
                        cleanup_failures.append({'phase': 'container attempt ' + str(attempt), 'failure': failure_payload(error)})
                if cleanup_failures:
                    refused = Refused("Ext4 container cleanup failed: " + json.dumps(cleanup_failures, sort_keys=True))
                    refused.cleanup_errors = tuple(cleanup_failures)
                    refused.failure = failure_payload(cleanup_primary)
                    raise refused from cleanup_primary
                ext4_guest("cleanup", args.run)
            cleanup_run(args.run, RUNS / args.run)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        diagnostic = "; diagnostics=" + json.dumps(failure_payload(error), sort_keys=True) if getattr(args, "ext4", False) else ""
        print(f"Local runner refused: {error}{diagnostic}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
