import json
"""Ordinary directories prove Git mechanics only, never real bind mounts."""

import importlib.util
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from native_topology_helpers import (
    ALLOWED, Blocked, DESTINATIONS, METADATA, POLICY, SLUG, adverse_index_proof,
    audit_index, broad_add_proof, git, render_policy, seed, snapshot, stage,
    validate_layout, verify_snapshot,
)


SYNTHETIC = dict.fromkeys(DESTINATIONS, True)


def topology_workflow_paths(event):
    """Read the workflow's deliberately simple event/paths YAML subset."""
    source = (Path(__file__).resolve().parents[2] / ".github/workflows/native-topology.yml").read_text()
    on = re.search(r"^on:\n((?:[ \t].*\n|\n)+)", source, re.MULTILINE)
    assert on, "Topology workflow must declare event filters"
    block = re.search(rf"^  {event}:\n((?:    .*\n|\n)+)", on[1], re.MULTILINE)
    assert block, f"Topology workflow must run on {event}"
    lines = [line for line in block[1].splitlines() if line.strip()]
    assert lines[0] == "    paths:", f"Unexpected {event} filter restrictions"
    patterns = []
    for line in lines[1:]:
        match = re.fullmatch(r"      - '([^']+)'", line)
        assert match, f"Unexpected topology path filter: {line}"
        patterns.append(match[1])
    assert patterns
    return patterns


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_workflow_covers_native_topology_dependencies(event):
    patterns = topology_workflow_paths(event)
    # These filters use only exact names and trailing /**. Match that GitHub
    # subset directly, without approximating arbitrary glob/negation semantics.
    assert all("*" not in pattern or pattern.endswith("/**") and "*" not in pattern[:-3]
               for pattern in patterns)
    def covered(path):
        return any(path.startswith(pattern[:-2]) if pattern.endswith("/**") else path == pattern
                   for pattern in patterns)
    for path in (
        "ha-ops/app/server.py", "ha-ops/app/git_ops.py", "ha-ops/app/policies.py",
        "ha-ops/config.yaml", "ha-ops/Dockerfile", "ha-ops/run.sh",
        "ha-ops/frontend/package.json", "ha-ops/frontend/package-lock.json",
        "ha-ops/future/native/adapter.py", "ha-ops/tests/native_topology_helpers.py",
        "ha-ops/tests/test_native_topology.py", "ha-ops/tests/integration/run_native_topology.py",
        "ha-ops/tests/integration/container_probe.py", "ha-ops/tests/integration/Dockerfile",
        "ha-ops/docs/native-tree-topology.md", "repository.yaml", "requirements-dev.txt",
        "TESTING.md", ".github/workflows/native-topology.yml",
    ):
        assert covered(path), f"{event} omits topology dependency {path}"
    for path in ("TODO.md", "README.md", "other/app/server.py", "ha-ops-other/config.yaml"):
        assert not covered(path), f"{event} unexpectedly includes unrelated {path}"


def test_workflow_topology_filters_are_symmetric_and_conservative():
    assert topology_workflow_paths("push") == topology_workflow_paths("pull_request")
    assert set(topology_workflow_paths("push")) == {
        "ha-ops/**", "repository.yaml", "requirements-dev.txt", "TESTING.md",
        ".github/workflows/native-topology.yml",
    }


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "synthetic-root"
    root.mkdir()
    payloads = seed(root)
    return root, payloads


@pytest.mark.parametrize("repo_path", ["ha-config", "custom/deep/checkout"])
def test_root_staging_and_exact_index_with_legacy_checkout(tmp_path, repo_path):
    root = tmp_path / "root"
    root.mkdir()
    payloads = seed(root, repo_path)
    # A directory copy models the duplicate contents, not a duplicate bind.
    shutil.copytree(root / "addon_configs" / SLUG, root / "config", dirs_exist_ok=True)
    broad_add_proof(root, SYNTHETIC, repo_path)
    adverse_index_proof(root, SYNTHETIC)
    git(root, "commit", "-m", "Safe fixture tree")
    git(root, "update-ref", "refs/heads/persisted-proof", "HEAD")
    broad_add_proof(root, SYNTHETIC, repo_path)
    for path, data in payloads.items():
        assert (root / path).read_bytes() == data
    assert len(audit_index(root)) == len(ALLOWED)


def test_directory_root_recreation_is_only_mechanics(tree, tmp_path):
    root, payloads = tree
    broad_add_proof(root, SYNTHETIC)
    git(root, "commit", "-m", "Safe fixture tree")
    git(root, "update-ref", "refs/heads/persisted-proof", "HEAD")
    expected = snapshot(root, payloads)
    recreated = tmp_path / "new-synthetic-root"
    recreated.mkdir()
    for destination in DESTINATIONS:
        shutil.move(str(root / destination), recreated / destination)
    verify_snapshot(recreated, expected)
    with pytest.raises(Blocked, match="policy missing"):
        stage(recreated, SYNTHETIC)
    (recreated / ".gitignore").write_bytes((recreated / POLICY).read_bytes())
    broad_add_proof(recreated, SYNTHETIC)


@pytest.mark.parametrize("corruption", ["missing", "empty", "changed", "symlink"])
def test_policy_blocked_before_git_add(tree, corruption, monkeypatch):
    root, _ = tree
    policy = root / ".gitignore"
    policy.unlink()
    if corruption == "symlink":
        policy.symlink_to(root / POLICY)
    elif corruption != "missing":
        policy.write_bytes(b"" if corruption == "empty" else b"*\n")
    def unexpected(*args):
        pytest.fail("Git was invoked after failed preconditions")
    monkeypatch.setattr("native_topology_helpers.git", unexpected)
    with pytest.raises(Blocked):
        stage(root, SYNTHETIC)


@pytest.mark.parametrize("evidence", [None, {}, {**SYNTHETIC, "data": False}])
def test_unconfirmed_or_read_only_destinations_block_before_add(tree, evidence, monkeypatch):
    root, _ = tree
    monkeypatch.setattr("native_topology_helpers.git", lambda *a: pytest.fail("Git invoked"))
    with pytest.raises(Blocked):
        stage(root, evidence)


@pytest.mark.parametrize("destination", DESTINATIONS)
def test_missing_or_symlink_destination_blocks(tree, destination):
    root, _ = tree
    retained = root / (destination + "-retained")
    (root / destination).rename(retained)
    with pytest.raises(Blocked):
        stage(root, SYNTHETIC)
    (root / destination).symlink_to(retained, target_is_directory=True)
    with pytest.raises(Blocked, match="Symlink"):
        stage(root, SYNTHETIC)


@pytest.mark.parametrize("repo_path", ["native", "native/repository.git", "native/repository.git/sub", "native/repository-files/sub", "native/root-ignore-policy", "../escape", "/absolute"])
def test_legacy_service_overlap_refused_without_writes(tmp_path, repo_path):
    with pytest.raises(Blocked):
        seed(tmp_path, repo_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("occupied", [METADATA, "data/native/repository-files", POLICY, "data/native", ".gitignore"])
def test_occupied_service_path_is_preserved(tmp_path, occupied):
    path = tmp_path / occupied
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"owned sentinel")
    with pytest.raises(Blocked):
        seed(tmp_path)
    assert path.read_bytes() == b"owned sentinel"


def test_symlink_service_parent_preserved(tmp_path):
    retained = tmp_path / "retained"
    retained.mkdir()
    (tmp_path / "data").symlink_to(retained, target_is_directory=True)
    with pytest.raises(Blocked):
        validate_layout(tmp_path, initializing=True)
    assert list(retained.iterdir()) == []


def test_ambient_git_variables_cannot_redirect_fixture(tree, monkeypatch, tmp_path):
    root, _ = tree
    poisoned = tmp_path / "unrelated"
    poisoned.mkdir()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(name, str(poisoned))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(poisoned))
    broad_add_proof(root, SYNTHETIC)
    assert list(poisoned.iterdir()) == []


def test_newline_allow_pattern_refused():
    with pytest.raises(Blocked):
        render_policy({"homeassistant/line\nbreak"})


def test_historical_tracked_forbidden_file_is_rejected(tree):
    root, _ = tree
    # Simulate an unsafe historical repository before installing the new policy.
    policy = (root / ".gitignore").read_bytes()
    (root / ".gitignore").unlink()
    git(root, "add", "--", "data/options.json")
    git(root, "commit", "-m", "Historical fixture baseline")
    (root / ".gitignore").write_bytes(policy)
    git(root, "add", "-A")
    assert b"data/options.json" in git(root, "ls-files", "-z").split(b"\0")
    with pytest.raises(Blocked, match="Unsafe index"):
        audit_index(root)


@pytest.mark.parametrize("saved", ["missing", "corrupt"])
def test_saved_policy_failure_blocks_before_add(tree, saved, monkeypatch):
    root, _ = tree
    path = root / POLICY
    path.unlink()
    if saved == "corrupt":
        path.write_bytes(b"invalid")
    monkeypatch.setattr("native_topology_helpers.git", lambda *a: pytest.fail("Git invoked"))
    with pytest.raises(Blocked, match="Saved policy"):
        stage(root, SYNTHETIC)


def test_cleanup_failure_is_not_success(monkeypatch):
    runner = runner_module()
    def fake(args, **kwargs):
        return b'[{}]' if args[:2] == ["image", "inspect"] else b""
    monkeypatch.setattr(runner, "docker", fake)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        runner.cleanup("owned-fixture-image", ("owned-fixture-container",))


def test_cleanup_daemon_error_is_not_absent_resource(monkeypatch):
    runner = runner_module()
    runner._route = ("unix:///fixture/docker.sock", {})
    failed = runner.subprocess.CompletedProcess(["docker"], 1, stdout=b"", stderr=b"Cannot connect to Docker daemon")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: failed)
    with pytest.raises(RuntimeError, match="inspection/cleanup failed"):
        runner.present(["image", "inspect", "owned-fixture-image"])


@pytest.mark.parametrize("failed_operation", ["removal", "inspection"])
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, ValueError, subprocess.SubprocessError])
def test_cleanup_attempts_every_removal_and_inspection_after_failure(monkeypatch, failed_operation, error_type):
    runner = runner_module()
    calls = []
    first = "owned-fixture-first"
    target = ["rm", "--force", first] if failed_operation == "removal" else ["container", "inspect", first]
    def fake(args, **kwargs):
        calls.append(args)
        if args == target:
            raise error_type("first resource refused")
        return b"[]" if "inspect" in args else b""
    monkeypatch.setattr(runner, "docker", fake)
    with pytest.raises(RuntimeError, match="Owned resource cleanup failed"):
        runner.cleanup("owned-fixture-image", (first, "owned-fixture-second"))
    assert calls == [
        ["rm", "--force", first], ["container", "inspect", first],
        ["rm", "--force", "owned-fixture-second"], ["container", "inspect", "owned-fixture-second"],
        ["image", "rm", "owned-fixture-image"], ["image", "inspect", "owned-fixture-image"],
    ]


@pytest.mark.parametrize("switch_at", ["after-preflight", "mid-workflow"])
def test_validated_docker_endpoint_stays_pinned_through_cleanup(tmp_path, monkeypatch, switch_at):
    runner = runner_module()
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fixture/docker")
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDX_BUILDER"):
        monkeypatch.delenv(key, raising=False)
    local = "unix:///fixture/local.sock"
    default_context = "safe-local"
    calls, bindings = [], {}
    def change_context():
        nonlocal default_context
        default_context = "remote-over-ssh"
        monkeypatch.setenv("DOCKER_CONTEXT", default_context)
        monkeypatch.setenv("DOCKER_HOST", "ssh://remote.invalid")
        monkeypatch.setenv("BUILDX_BUILDER", "remote-builder")
    def fake_cli(command, **kwargs):
        args = command[1:]
        if args[:2] == ["context", "show"]:
            output = default_context.encode()
        elif args[:2] == ["context", "inspect"]:
            output = ('[{"Endpoints":{"docker":{"Host":"' + local + '"}}}]').encode()
        else:
            assert args[:2] == ["--host", local]
            assert not {"DOCKER_CONTEXT", "DOCKER_HOST", "BUILDX_BUILDER"} & kwargs["env"].keys()
            args = args[2:]
            calls.append(args)
            if args[0] == "info":
                output = b'{"OSType":"linux","ServerVersion":"fixture"}'
            elif args[0] == "run":
                name = args[args.index("--name") + 1]
                mounts = []
                for index, arg in enumerate(args):
                    if arg == "--mount":
                        parsed = dict(part.split("=", 1) for part in args[index + 1].split(","))
                        mounts.append({"Source": parsed["source"], "Destination": parsed["target"], "Type": "bind", "RW": True})
                bindings[name] = mounts
                output = name.encode()
            elif args[0] == "inspect":
                output = runner.json.dumps([{"Mounts": bindings[args[1]]}]).encode()
            elif args[0] == "exec":
                output = b'{"snapshot":{}}' if args[-1] == "first" else b'{}'
            else:
                output = b"[]" if "inspect" in args else b""
            if switch_at == "mid-workflow" and args[0] == "build":
                change_context()
        return runner.subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")
    monkeypatch.setattr(runner.subprocess, "run", fake_cli)
    runner.preflight()
    if switch_at == "after-preflight":
        change_context()
    # This mocked sequence proves CLI routing/error handling only, not mounts.
    runner.exercise(tmp_path, "owned-fixture-image", "owned-first", "owned-second")
    assert default_context == "remote-over-ssh"
    assert {args[0] for args in calls} >= {"info", "build", "run", "inspect", "exec", "rm", "container", "image"}


def test_docker_mutation_refuses_unvalidated_route(monkeypatch):
    runner = runner_module()
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("CLI mutation attempted"))
    with pytest.raises(RuntimeError, match="not been validated"):
        runner.docker(["build", "fixture"])


def test_non_linux_engine_refused_before_build(monkeypatch):
    runner = runner_module()
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fixture/docker")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/docker.sock")
    monkeypatch.setattr(runner, "docker", lambda *a, **kw: b'{"OSType":"windows"}')
    with pytest.raises(RuntimeError, match="must be Linux"):
        runner.preflight()


def runner_module():
    spec = importlib.util.spec_from_file_location("native_runner", Path(__file__).parent / "integration/run_native_topology.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("endpoint", ["ssh://host", "tcp://127.0.0.1:2375", "npipe://local", ""])
def test_remote_or_unsupported_docker_endpoint_refused(endpoint):
    with pytest.raises(RuntimeError, match="local Unix"):
        runner_module().validate_endpoint(endpoint)


def test_missing_docker_is_failure(monkeypatch):
    runner = runner_module()
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="unverified"):
        runner.preflight()


@pytest.mark.parametrize("failure_at", ["build", "first", "second", "timeout"])
def test_owned_docker_resources_cleaned_on_failure(tmp_path, monkeypatch, failure_at):
    runner = runner_module()
    calls = []
    def fake(args, **kwargs):
        calls.append(args)
        if args[:2] == ["image", "inspect"] or args[:2] == ["container", "inspect"]:
            return b"[]"
        if args[:2] == ["build", "--tag"] and failure_at == "build":
            raise RuntimeError("build failure")
        if args[0] == "run":
            count = sum(call[0] == "run" for call in calls)
            if failure_at == "first" or count == 2:
                raise RuntimeError("container failure")
            return b"distinct-container-id\n"
        if args[0] == "inspect":
            return b"[]"
        if args[0] == "exec":
            if failure_at == "timeout":
                raise runner.subprocess.TimeoutExpired(args, 30)
            return b'{}'
        return b""
    monkeypatch.setattr(runner, "docker", fake)
    monkeypatch.setattr(runner, "check_mounts", lambda *a: None)
    with pytest.raises((RuntimeError, runner.subprocess.TimeoutExpired, KeyError)):
        runner.exercise(tmp_path, "owned-fixture-image", "owned-fixture-first", "owned-fixture-second")
    assert ["image", "rm", "owned-fixture-image"] in calls
    assert ["rm", "--force", "owned-fixture-first"] in calls
    assert ["rm", "--force", "owned-fixture-second"] in calls
    assert not any("prune" in call for call in calls)


@pytest.mark.parametrize("failed_operation", ["removal", "inspection"])
def test_owned_cleanup_checks_absence_and_later_resources_after_failure(monkeypatch, failed_operation):
    runner = runner_module()
    calls = []
    inspections = {}
    def fake(args, **kwargs):
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            name = args[2]
            inspections[name] = inspections.get(name, 0) + 1
            if inspections[name] == 1:
                return json.dumps([{"Id": name, "Config": {"Labels": {"ha-ops.dev.run": "fixture-run"}}}]).encode()
            if name == "first" and failed_operation == "inspection":
                raise RuntimeError("absence check failed")
            return b"[]"
        if args == ["rm", "--force", "first"] and failed_operation == "removal":
            raise RuntimeError("removal failed")
        return b"[]" if "inspect" in args else b""
    monkeypatch.setattr(runner, "docker", fake)
    with pytest.raises(runner.CleanupFailure):
        runner.cleanup("fixture-image", ["first", "second"], run_id="fixture-run")
    assert calls == [
        ["container", "inspect", "first"], ["rm", "--force", "first"], ["container", "inspect", "first"],
        ["container", "inspect", "second"], ["rm", "--force", "second"], ["container", "inspect", "second"],
        ["image", "rm", "fixture-image"], ["image", "inspect", "fixture-image"],
    ]
