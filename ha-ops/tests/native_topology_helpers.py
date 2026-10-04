"""Test-only contracts for a candidate container-root Git tree; no runtime adapter."""

import base64
import os
from pathlib import Path, PurePosixPath
import subprocess


SLUG = "fixture_ha_ops"
DESTINATIONS = ("homeassistant", "addon_configs", "config", "data", "backup")
METADATA = "data/native/repository.git"
REPOSITORY_FILES = "data/native/repository-files"
POLICY = "data/native/root-ignore-policy"
# Independent expected paths: never derive the index expectation from ignore rules.
ALLOWED = frozenset({
    ".gitignore", "homeassistant/configuration.yaml",
    "homeassistant/nested/selected [literal]*?.yaml",
    f"addon_configs/{SLUG}/settings.yaml",
    f"{REPOSITORY_FILES}/README.md",
})


class Blocked(RuntimeError):
    pass


def git(root, *args):
    root = Path(root).absolute()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(
        ["git", "--literal-pathspecs", f"--git-dir={root / METADATA}",
         f"--work-tree={root}", "-c", "core.hooksPath=/dev/null",
         "-c", "core.attributesFile=/dev/null", "-c", "core.autocrlf=false",
         "-c", "core.safecrlf=false", *args],
        cwd=root, env=env, check=True, capture_output=True, timeout=30,
    ).stdout


def render_policy(paths=ALLOWED):
    def escape(path):
        if "\n" in path or "\r" in path or "\0" in path:
            raise Blocked("Line-based ignore policy cannot allow this filename")
        return "".join("\\" + c if c in "\\*?[]!# " else c for c in path)

    parents = set()
    for path in paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path:
            raise Blocked("Allowlist paths must be canonical relative paths")
        parents.update(str(p) for p in parsed.parents if str(p) != ".")
    lines = ["# Candidate root policy: deny every unselected path", "/*"]
    for parent in sorted(parents, key=lambda p: (p.count("/"), p)):
        lines.extend((f"!/{escape(parent)}/", f"/{escape(parent)}/*"))
    lines.extend(f"!/{escape(path)}" for path in sorted(paths))
    return ("\n".join(lines) + "\n").encode()


def real_path(root, relative):
    path = Path(root)
    if path.is_symlink():
        raise Blocked("Root is a symlink")
    for part in PurePosixPath(relative).parts:
        path /= part
        if path.is_symlink():
            raise Blocked(f"Symlink service or mount path: {relative}")
    return path


def validate_layout(root, repo_path="ha-config", initializing=False):
    root_policy = real_path(root, ".gitignore")
    if initializing and root_policy.exists():
        raise Blocked("Root policy path already occupied")
    legacy = PurePosixPath(repo_path)
    if legacy.is_absolute() or str(legacy) in ("", ".") or ".." in legacy.parts:
        raise Blocked("Legacy repo_path must be relative inside data")
    legacy = PurePosixPath("data") / legacy
    real_path(root, str(legacy))
    for service in (METADATA, REPOSITORY_FILES, POLICY):
        reserved = PurePosixPath(service)
        if legacy == reserved or legacy in reserved.parents or reserved in legacy.parents:
            raise Blocked("Legacy checkout overlaps a reserved service path")
        path = real_path(root, service)
        if initializing and path.exists():
            raise Blocked("Reserved service path already occupied")
        for ancestor in path.parents:
            if ancestor == Path(root):
                break
            if ancestor.exists() and not ancestor.is_dir():
                raise Blocked("Service parent is not a directory")


def validate_preconditions(root, evidence, expected_policy, repo_path="ha-config"):
    validate_layout(root, repo_path)
    if evidence is None or set(evidence) != set(DESTINATIONS):
        raise Blocked("Mount topology unconfirmed")
    for destination in DESTINATIONS:
        path = real_path(root, destination)
        if not evidence[destination] or not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise Blocked("Required destination unavailable or read-only")
    policy = real_path(root, ".gitignore")
    if not policy.is_file() or policy.read_bytes() != expected_policy:
        raise Blocked("Root ignore policy missing or corrupted")
    saved = real_path(root, POLICY)
    if not saved.is_file() or saved.read_bytes() != expected_policy:
        raise Blocked("Saved policy differs")
    if not real_path(root, METADATA).is_dir() or not real_path(root, REPOSITORY_FILES).is_dir():
        raise Blocked("Reserved service layout unavailable")


def index_paths(root):
    raw = git(root, "ls-files", "-z")
    if raw and not raw.endswith(b"\0"):
        raise Blocked("Malformed NUL index output")
    return frozenset(raw[:-1].split(b"\0")) if raw else frozenset()


def audit_index(root):
    actual = index_paths(root)
    expected = frozenset(os.fsencode(p) for p in ALLOWED)
    if actual != expected:
        raise Blocked(f"Unsafe index: extra={actual - expected!r}, missing={expected - actual!r}")
    return actual


def stage(root, evidence, args=("add", "."), repo_path="ha-config"):
    validate_preconditions(root, evidence, render_policy(), repo_path)
    git(root, *args)
    return audit_index(root)


def write(root, relative, data):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def seed(root, repo_path="ha-config"):
    validate_layout(root, repo_path, initializing=True)
    for destination in DESTINATIONS:
        (Path(root) / destination).mkdir(parents=True, exist_ok=True)
    payloads = {
        "homeassistant/configuration.yaml": b"name: fixture\n# exact bytes\n",
        "homeassistant/nested/selected [literal]*?.yaml": b"\xff\x00\r\nfixture\n",
        f"addon_configs/{SLUG}/settings.yaml": b"enabled: true\n",
        f"{REPOSITORY_FILES}/README.md": b"Repository-only content\n",
        f"{REPOSITORY_FILES}/unselected.txt": b"repository sibling\n",
        "homeassistant/unselected.yaml": b"unselected\n",
        "homeassistant/nested/selected lX.yaml": b"glob cousin\n",
        f"addon_configs/{SLUG}/private.json": b"fixture private\n",
        "data/options.json": b"options sentinel\n",
        f"data/{repo_path}/old-checkout": b"legacy sentinel\n",
        "data/recovery/sentinel": b"recovery\n", "data/releases/sentinel": b"release\n",
        "data/work/sentinel": b"work\n", "backup/sentinel": b"backup\n",
    }
    for path, data in payloads.items():
        write(root, path, data)
    write(root, "unselected-root-sentinel", b"root sentinel\n")
    write(root, POLICY, render_policy())
    write(root, ".gitignore", render_policy())
    git(root, "init", "--initial-branch=fixture")
    write(root, f"{METADATA}/excluded-sentinel", b"metadata sentinel\n")
    assert git(root, "rev-parse", "--absolute-git-dir").strip() == os.fsencode(Path(root) / METADATA)
    assert git(root, "rev-parse", "--show-toplevel").strip() == os.fsencode(Path(root))
    return payloads


def broad_add_proof(root, evidence, repo_path="ha-config"):
    for args in (("add", "."), ("add", "-A")):
        stage(root, evidence, args, repo_path)


def adverse_index_proof(root, evidence):
    # Show the failure first, then reject it independently. Ignore is not a fence.
    forbidden = "data/forbidden\nidentity"
    write(root, forbidden, b"force sentinel\n")
    git(root, "add", "-f", "--", forbidden)
    assert os.fsencode(forbidden) in index_paths(root)
    try:
        audit_index(root)
    except Blocked:
        pass
    else:
        raise AssertionError("Force-added path accepted")
    git(root, "add", "-A")
    assert os.fsencode(forbidden) in index_paths(root)  # already tracked survives ignore
    try:
        audit_index(root)
    except Blocked:
        pass
    else:
        raise AssertionError("Previously tracked path accepted")
    git(root, "update-index", "--force-remove", "--", forbidden)
    override = "homeassistant/nested/.gitignore"
    write(root, override, b"!unselected.yaml\n")
    write(root, "homeassistant/nested/unselected.yaml", b"override attack\n")
    git(root, "add", ".")
    assert b"homeassistant/nested/unselected.yaml" in index_paths(root)
    try:
        audit_index(root)
    except Blocked:
        pass
    else:
        raise AssertionError("Nested override accepted")
    git(root, "update-index", "--force-remove", "--", "homeassistant/nested/unselected.yaml")
    (Path(root) / override).unlink()
    broad_add_proof(root, evidence)


def snapshot(root, payloads):
    encode = lambda data: base64.b64encode(data).decode("ascii")
    paths = set(payloads) | {POLICY, f"{METADATA}/excluded-sentinel"}
    refs = git(root, "show-ref").decode("ascii")
    blobs = {}
    for path in ALLOWED:
        oid = git(root, "rev-parse", f"HEAD:{path}").strip().decode("ascii")
        blobs[path] = {"oid": oid, "bytes": encode(git(root, "cat-file", "blob", oid))}
    return {"files": {path: encode((Path(root) / path).read_bytes()) for path in sorted(paths)},
            "refs": refs, "head": git(root, "rev-parse", "HEAD").decode("ascii"), "blobs": blobs}


def verify_snapshot(root, expected):
    for path, encoded in expected["files"].items():
        assert (Path(root) / path).read_bytes() == base64.b64decode(encoded), path
    assert git(root, "show-ref").decode("ascii") == expected["refs"]
    assert git(root, "rev-parse", "HEAD").decode("ascii") == expected["head"]
    for path, blob in expected["blobs"].items():
        assert git(root, "rev-parse", f"HEAD:{path}").strip().decode("ascii") == blob["oid"]
        assert git(root, "cat-file", "blob", blob["oid"]) == base64.b64decode(blob["bytes"])
