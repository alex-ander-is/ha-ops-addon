"""Real Linux bind/recreation proof. Missing Docker is a failure, never a skip."""

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_cleanup import open_directory_chain, remove_tree


SLUG = "fixture_ha_ops"
DESTINATIONS = ("homeassistant", "addon_configs", "config", "data", "backup")
LOCAL_RUNS = Path(__file__).resolve().parents[3] / "dev/virtualization/.local/runs"
_route = None


class CleanupFailure(RuntimeError):
    pass


class FixtureGuard:
    """Hold the owned run root and bind directories across preflight and use."""

    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self.handles = {}
        self.runs = open_directory_chain(LOCAL_RUNS)
        try:
            self.handles[""] = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=self.runs)
            for name in ("homeassistant", "addon_configs", "data", "backup"):
                self.handles[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                             dir_fd=self.handles[""])
            self.handles["config"] = os.open(SLUG, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                             dir_fd=self.handles["addon_configs"])
            self.validate()
        except BaseException:
            self.close()
            raise

    def close(self):
        for handle in self.handles.values():
            os.close(handle)
        self.handles.clear()
        if self.runs is not None:
            os.close(self.runs)
            self.runs = None

    def remove(self):
        remove_tree(self.runs, self.root.name, self.handles[""])

    def validate(self):
        fresh = open_directory_chain(LOCAL_RUNS)
        try:
            if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (
                    os.fstat(self.runs).st_dev, os.fstat(self.runs).st_ino):
                raise RuntimeError("Topology runs directory changed")
        finally:
            os.close(fresh)
        relationships = [(self.runs, self.root.name, self.handles[""])]
        relationships.extend((self.handles[""], name, self.handles[name])
                             for name in ("homeassistant", "addon_configs", "data", "backup"))
        relationships.append((self.handles["addon_configs"], SLUG, self.handles["config"]))
        for parent, name, held in relationships:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            original = os.fstat(held)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                    original.st_dev, original.st_ino):
                raise RuntimeError(f"Topology fixture path changed: {name}")


def docker(args, *, input=None, check=True, timeout=60):
    if _route is None:
        raise RuntimeError("Docker endpoint has not been validated")
    endpoint, environment = _route
    if endpoint is None and args[:2] not in (["context", "show"], ["context", "inspect"]):
        raise RuntimeError("Docker endpoint has not been validated")
    route = ["--host", endpoint] if endpoint else []
    result = subprocess.run(["docker", *route, *args], env=environment,
                            input=input, capture_output=True, check=check, timeout=timeout)
    if not check and result.returncode:
        message = result.stderr.decode(errors="replace")
        if not any(phrase in message.lower() for phrase in ("no such object", "no such image", "no such container")):
            raise RuntimeError("Docker resource inspection/cleanup failed: " + message.strip())
    return result.stdout


def validate_endpoint(endpoint):
    if not endpoint.startswith("unix:///"):
        raise RuntimeError("Topology unverified: a local Unix Docker endpoint is required")


def preflight():
    global _route
    environment = os.environ.copy()
    _route = (None, environment)
    if not shutil.which("docker"):
        raise RuntimeError("Docker executable is unavailable; topology unverified")
    # Resolve the same precedence as the CLI. Refuse remote endpoints before mutation.
    context = environment.get("DOCKER_CONTEXT")
    endpoint = environment.get("DOCKER_HOST") if not context else None
    if endpoint is None:
        context = context or docker(["context", "show"]).decode().strip()
        endpoint = json.loads(docker(["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    validate_endpoint(endpoint)
    # Pin the validated daemon before even querying info. No later command may
    # resolve a mutable CLI default context or inherit a changed routing env.
    environment = {key: value for key, value in environment.items()
                   if key not in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                                  "BUILDX_BUILDER", "DOCKER_DEFAULT_PLATFORM")}
    _route = (endpoint, environment)
    info = json.loads(docker(["info", "--format", "{{json .}}"], timeout=30))
    if info["OSType"] != "linux":
        raise RuntimeError("Topology unverified: Docker engine must be Linux")
    return {"engine": info["ServerVersion"], "os": info["OSType"]}


def check_mounts(name, sources, *, run_id=None):
    inspected = json.loads(docker(["inspect", name]))[0]
    actual = {item["Destination"]: item for item in inspected["Mounts"]}
    if set(actual) != {"/" + p for p in DESTINATIONS}:
        raise RuntimeError("Unexpected mount destinations")
    for destination, source in sources.items():
        mount = actual["/" + destination]
        if mount["Type"] != "bind" or not mount["RW"] or mount["Source"] != str(source):
            raise RuntimeError("Bind source/type/access mismatch")
    if run_id:
        if inspected["Config"]["Labels"].get("ha-ops.dev.run") != run_id:
            raise RuntimeError("Container ownership label mismatch")
        if inspected["HostConfig"]["NetworkMode"] != "none":
            raise RuntimeError("Container network isolation mismatch")


def present(args):
    raw = docker(args, check=False)
    return bool(raw.strip()) and bool(json.loads(raw))


def owned_id(name, run_id):
    raw = docker(["container", "inspect", name], check=False)
    if not raw.strip() or not json.loads(raw):
        return None
    inspected = json.loads(raw)[0]
    if inspected["Config"]["Labels"].get("ha-ops.dev.run") != run_id:
        raise RuntimeError("Container name belongs to another run")
    identity = inspected.get("Id")
    if not identity:
        raise RuntimeError("Owned container has no immutable ID")
    return identity


def remove_owned(name, run_id):
    identity = owned_id(name, run_id)
    if identity:
        docker(["rm", "--force", identity])
        if owned_id(identity, run_id):
            raise RuntimeError("Owned container remains after removal")


def cleanup(image, names, *, retain_image=False, run_id=None):
    failures = []
    if run_id:
        for name in names:
            try:
                remove_owned(name, run_id)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                failures.append(f"{name} removal: {error}")
        if failures:
            raise CleanupFailure("Owned resource cleanup failed: " + ", ".join(failures))
        if retain_image:
            return
        docker(["image", "rm", image], check=False)
        if present(["image", "inspect", image]):
            raise RuntimeError("Owned CI image remains after cleanup")
        return
    resources = [(name, ["rm", "--force", name], ["container", "inspect", name]) for name in names]
    if not retain_image:
        resources.append((image, ["image", "rm", image], ["image", "inspect", image]))
    errors = (OSError, RuntimeError, subprocess.SubprocessError, ValueError)
    for name, removal, inspection in resources:
        try:
            docker(removal, check=False)
        except errors as error:
            failures.append(f"{name} removal: {error}")
        try:
            if present(inspection):
                failures.append(f"{name} remains present")
        except errors as error:
            failures.append(f"{name} inspection: {error}")
    if failures:
        raise CleanupFailure("Owned resource cleanup failed: " + ", ".join(failures))


def verify_vm_sources(sources, profile, fixture_guard=None):
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("COLIMA_HOME", "COLIMA_PROFILE", "LIMA_HOME")}
    for name, source in sources.items():
        marker = source / (".vm-source-" + uuid.uuid4().hex)
        payload = os.urandom(32)
        try:
            if fixture_guard:
                fixture_guard.validate()
                handle = os.open(marker.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=fixture_guard.handles[name])
                try:
                    os.write(handle, payload)
                finally:
                    os.close(handle)
                fixture_guard.validate()
            else:
                marker.write_bytes(payload)
            observed = subprocess.run(["colima", "ssh", "--profile", profile, "--", "cat", str(marker)],
                                      env=environment, capture_output=True, check=True, timeout=30).stdout
            if observed != payload:
                raise RuntimeError("VM sees different bytes at exact bind source: " + str(source))
            if fixture_guard:
                fixture_guard.validate()
        finally:
            if fixture_guard:
                try:
                    os.unlink(marker.name, dir_fd=fixture_guard.handles[name])
                except FileNotFoundError:
                    pass
            else:
                marker.unlink(missing_ok=True)


def exercise(directory, image, first, second, *, skip_build=False, retain_image=False,
             run_id=None, vm_profile=None, container_platform=None, fixture_guard=None):
    directory = Path(os.path.abspath(directory))
    if fixture_guard:
        fixture_guard.validate()
    elif directory.is_symlink():
        raise RuntimeError("Topology fixture root is a symlink")
    sources = {p: directory / p for p in DESTINATIONS if p != "config"}
    if not fixture_guard:
        for source in sources.values():
            source.mkdir(exist_ok=bool(vm_profile))
    sources["config"] = sources["addon_configs"] / SLUG
    if not fixture_guard:
        sources["config"].mkdir(exist_ok=bool(vm_profile))
    if vm_profile:
        verify_vm_sources(sources, vm_profile, fixture_guard)
    if fixture_guard:
        fixture_guard.validate()
    try:
        if not skip_build:
            context = directory / "build-context"
            context.mkdir()
            tests = Path(__file__).resolve().parents[1]
            for source in (tests / "native_topology_helpers.py", tests / "integration/container_probe.py", tests / "integration/Dockerfile"):
                shutil.copyfile(source, context / source.name)
            docker(["build", "--tag", image, str(context)], timeout=600)
        def start(name):
            if fixture_guard:
                fixture_guard.validate()
            # Main supplies a generated run ID in both CI and local mode.
            # Direct legacy exercise() calls keep their existing test contract.
            safe_start = bool(run_id)
            args = ["create" if safe_start else "run", "--name", name, "--network", "none"]
            if not safe_start:
                args.insert(1, "--detach")
            if container_platform:
                args.extend(["--platform", container_platform])
            if run_id:
                args.extend(["--label", f"ha-ops.dev.run={run_id}"])
            for destination, source in sources.items():
                args.extend(["--mount", f"type=bind,source={source},target=/{destination}"])
            identity = docker([*args, image]).decode().strip()
            if run_id:
                check_mounts(identity, sources, run_id=run_id)
            else:
                check_mounts(identity, sources)
            if fixture_guard:
                fixture_guard.validate()
            if safe_start:
                docker(["start", identity])
            return identity
        first_id = start(first)
        if fixture_guard:
            fixture_guard.validate()
        expected = json.loads(docker(["exec", first_id, "python3", "/opt/topology-fixture/container_probe.py", "first"], timeout=120))
        # Fully remove the first writable layer before creating the replacement.
        if run_id:
            remove_owned(first_id, run_id)
        else:
            docker(["rm", "--force", first])
        if present(["container", "inspect", first_id]):
            raise RuntimeError("First container was not removed")
        second_id = start(second)
        if first_id == second_id:
            raise RuntimeError("Container identity did not change")
        if fixture_guard:
            fixture_guard.validate()
        result = json.loads(docker(["exec", "-i", second_id, "python3", "/opt/topology-fixture/container_probe.py", "second"],
                                  input=json.dumps(expected).encode(), timeout=120))
        result["distinct_containers"] = True
        result["bind_sources_reused"] = True
        result["inspect_bind_type_source_rw"] = "passed"
        return result
    finally:
        cleanup(image, (first, second), retain_image=retain_image, run_id=run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--retain-image", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--vm-profile")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"))
    args = parser.parse_args()
    if (args.skip_build or args.retain_image) and not (args.image and args.fixture_root and args.run_id):
        parser.error("reusable mode requires --image, --fixture-root and --run-id")
    if args.skip_build or args.retain_image:
        if args.vm_profile != "ha-ops-dev" or not args.fixture_root.is_absolute() or args.fixture_root.parent != LOCAL_RUNS:
            parser.error("reusable mode requires the named VM and its private fixture directory")
        if args.fixture_root.is_symlink() or any(parent.is_symlink() for parent in args.fixture_root.parents):
            parser.error("reusable fixture has a symlinked path component")
        if len(args.run_id) != 32 or any(char not in "0123456789abcdef" for char in args.run_id):
            parser.error("reusable mode requires an exact generated run ID")
        if not args.platform:
            parser.error("reusable mode requires an explicit Linux platform")
    fixture_guard = None
    try:
        if args.skip_build or args.retain_image:
            fixture_guard = FixtureGuard(args.fixture_root)
        versions = preflight()
        if fixture_guard:
            fixture_guard.validate()
        generated_run_id = args.run_id or uuid.uuid4().hex
        identity = "native-topology-" + generated_run_id
        image = args.image or identity + ":fixture"
        first, second = identity + "-first", identity + "-second"
        if (not args.skip_build and present(["image", "inspect", image])) or any(present(["container", "inspect", p]) for p in (first, second)):
            raise RuntimeError("Owned resource name collision")
        if args.fixture_root:
            if fixture_guard:
                fixture_guard.validate()
            if args.fixture_root.exists():
                expected = {"homeassistant", "addon_configs", "data", "backup"}
                observed = {entry.name for entry in args.fixture_root.iterdir()}
                if not args.skip_build or observed != expected:
                    raise RuntimeError("Unexpected pre-created topology fixture")
                for name in expected:
                    path = args.fixture_root / name
                    if path.is_symlink() or not path.is_dir():
                        raise RuntimeError("Unexpected topology bind source")
                config = args.fixture_root / "addon_configs" / SLUG
                if config.is_symlink() or not config.is_dir():
                    raise RuntimeError("Unexpected topology config bind source")
                if fixture_guard:
                    fixture_guard.validate()
            else:
                args.fixture_root.mkdir(parents=True, exist_ok=False)
            try:
                result = exercise(args.fixture_root, image, first, second,
                                  skip_build=args.skip_build, retain_image=args.retain_image,
                                  run_id=generated_run_id, vm_profile=args.vm_profile,
                                  container_platform=args.platform, fixture_guard=fixture_guard)
            except CleanupFailure as error:
                raise CleanupFailure(f"Run {generated_run_id}: {error}") from error
            except BaseException:
                if fixture_guard:
                    fixture_guard.remove()
                else:
                    shutil.rmtree(args.fixture_root)
                raise
            else:
                if fixture_guard:
                    fixture_guard.remove()
                else:
                    shutil.rmtree(args.fixture_root)
        else:
            with tempfile.TemporaryDirectory(prefix="native-topology-") as directory:
                result = exercise(directory, image, first, second, run_id=generated_run_id)
        result["docker"] = versions
        result["status"] = "passed"
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.write_text(rendered)
        print(rendered, end="")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, AssertionError) as error:
        print(f"Topology unverified: {error}", file=sys.stderr)
        return 1
    finally:
        if fixture_guard:
            fixture_guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
