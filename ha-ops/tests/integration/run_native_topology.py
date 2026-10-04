"""Real Linux bind/recreation proof. Missing Docker is a failure, never a skip."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


SLUG = "fixture_ha_ops"
DESTINATIONS = ("homeassistant", "addon_configs", "config", "data", "backup")
_route = None


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
                   if key not in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "BUILDX_BUILDER")}
    _route = (endpoint, environment)
    info = json.loads(docker(["info", "--format", "{{json .}}"], timeout=30))
    if info["OSType"] != "linux":
        raise RuntimeError("Topology unverified: Docker engine must be Linux")
    return {"engine": info["ServerVersion"], "os": info["OSType"]}


def check_mounts(name, sources):
    inspected = json.loads(docker(["inspect", name]))[0]
    actual = {item["Destination"]: item for item in inspected["Mounts"]}
    if set(actual) != {"/" + p for p in DESTINATIONS}:
        raise RuntimeError("Unexpected mount destinations")
    for destination, source in sources.items():
        mount = actual["/" + destination]
        if mount["Type"] != "bind" or not mount["RW"] or mount["Source"] != str(source):
            raise RuntimeError("Bind source/type/access mismatch")


def present(args):
    raw = docker(args, check=False)
    return bool(raw.strip()) and bool(json.loads(raw))


def cleanup(image, names):
    failures = []
    resources = [(name, ["rm", "--force", name], ["container", "inspect", name]) for name in names]
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
        raise RuntimeError("Owned resource cleanup failed: " + ", ".join(failures))


def exercise(directory, image, first, second):
    directory = Path(directory).resolve()
    sources = {p: directory / p for p in DESTINATIONS if p != "config"}
    for source in sources.values():
        source.mkdir()
    sources["config"] = sources["addon_configs"] / SLUG
    sources["config"].mkdir()
    context = directory / "build-context"
    context.mkdir()
    tests = Path(__file__).resolve().parents[1]
    for source in (tests / "native_topology_helpers.py", tests / "integration/container_probe.py", tests / "integration/Dockerfile"):
        shutil.copyfile(source, context / source.name)
    try:
        docker(["build", "--tag", image, str(context)], timeout=600)
        def start(name):
            args = ["run", "--detach", "--name", name, "--network", "none"]
            for destination, source in sources.items():
                args.extend(["--mount", f"type=bind,source={source},target=/{destination}"])
            identity = docker([*args, image]).decode().strip()
            check_mounts(name, sources)
            return identity
        first_id = start(first)
        expected = json.loads(docker(["exec", first, "python3", "/opt/topology-fixture/container_probe.py", "first"], timeout=120))
        # Fully remove the first writable layer before creating the replacement.
        docker(["rm", "--force", first])
        if present(["container", "inspect", first]):
            raise RuntimeError("First container was not removed")
        second_id = start(second)
        if first_id == second_id:
            raise RuntimeError("Container identity did not change")
        result = json.loads(docker(["exec", "-i", second, "python3", "/opt/topology-fixture/container_probe.py", "second"],
                                  input=json.dumps(expected).encode(), timeout=120))
        result["distinct_containers"] = True
        result["bind_sources_reused"] = True
        result["inspect_bind_type_source_rw"] = "passed"
        return result
    finally:
        cleanup(image, (first, second))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        versions = preflight()
        identity = "native-topology-" + uuid.uuid4().hex
        image = identity + ":fixture"
        first, second = identity + "-first", identity + "-second"
        if present(["image", "inspect", image]) or any(present(["container", "inspect", p]) for p in (first, second)):
            raise RuntimeError("Owned resource name collision")
        with tempfile.TemporaryDirectory(prefix="native-topology-") as directory:
            result = exercise(directory, image, first, second)
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


if __name__ == "__main__":
    raise SystemExit(main())
