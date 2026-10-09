"""Real candidate-tool evaluation; no production metadata adapter.

Linux prerequisites are mandatory. macOS collection skips this group explicitly;
only the owned development runner's /tmp tmpfs qualifies the observations.
"""

import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest


BINARY = b"fixture\x00\xff\x80value"
TRACKED = ("parents", "parents/nested", "parents/nested/a 'quoted.yaml", "b.yaml")
MANIFEST = {"etckeeper": ".etckeeper", "metastore": ".metadata"}


def command(root, *args, check=True, env=None, demote=None):
    environment = {**os.environ, "LC_ALL": "C", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", **(env or {})}
    result = subprocess.run(args, cwd=root, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20, preexec_fn=demote)
    if check:
        assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
    return result


def git(root, *args, **kw):
    return command(root, "git", "-c", "user.name=Metadata fixture", "-c", "user.email=fixture@example.invalid",
                   "-c", "commit.gpgsign=false", *args, **kw)


def emit(case, **observed):
    print("METADATA_EVALUATION " + json.dumps({"case": case, **observed}, sort_keys=True))


def acl(path):
    # Compare numerical ACL entries, excluding headers and display-only comments.
    output = command(path.parent, "getfacl", "-c", "-n", "--", str(path)).stdout.decode()
    return tuple(sorted(line.split("\t#", 1)[0].strip() for line in output.splitlines() if line.strip()))


def readback(path):
    info = path.lstat()
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
            "type": stat.S_IFMT(info.st_mode), "acl": acl(path),
            "xattrs": {name: os.getxattr(path, name).hex() for name in sorted(os.listxattr(path))},
            "bytes": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def snapshot(root):
    return {name: readback(root / name) for name in TRACKED}


def basic(value):
    return {key: value[key] for key in ("uid", "gid", "mode", "type", "bytes")}


def configure(path, directory=False, rich=True):
    os.chown(path, pwd.getpwnam("daemon").pw_uid, grp.getgrnam("daemon").gr_gid)
    os.chmod(path, 0o3750 if directory else 0o640)
    if rich:
        command(path.parent, "setfacl", "-m", "u:65534:r-x,m::r-x" if directory else "u:65534:r--,m::r--", str(path))
        if directory:
            command(path.parent, "setfacl", "-m", "d:u::rwx,d:u:65534:r-x,d:g::r-x,d:m::r-x,d:o::---", str(path))
        os.setxattr(path, "user.ha_ops_fixture", BINARY)


def seed(root, rich=True):
    (root / "parents/nested").mkdir(parents=True)
    (root / TRACKED[2]).write_bytes(b"historical: bytes\n")
    (root / "b.yaml").write_bytes(b"unselected: original\n")
    for name in TRACKED[:3]:
        configure(root / name, (root / name).is_dir(), rich)
    os.chmod(root / "b.yaml", 0o600)
    if rich:
        os.setxattr(root / "b.yaml", "user.ha_ops_fixture", b"sentinel\x00")


@pytest.fixture(scope="session")
def tools():
    if sys.platform != "linux":
        pytest.skip("requires real Linux metadata tools")
    for executable in ("git", "etckeeper", "metastore", "getfacl", "setfacl"):
        assert shutil.which(executable), f"mandatory Linux executable missing: {executable}; run approved setup"
    assert os.geteuid() == 0, "metadata evaluation requires isolated root runner"
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    tmp_mounts = [line for line in mounts if line.split()[4] == "/tmp"]
    assert len(tmp_mounts) == 1 and " - tmpfs " in tmp_mounts[0], "use runner /tmp tmpfs, never output bind"
    caps = next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("CapEff:"))
    assert int(caps, 16) & 1 and int(caps, 16) & (1 << 3), "CAP_CHOWN and CAP_FOWNER required"
    versions = command(Path("/tmp"), "dpkg-query", "-W", "-f=${Package}=${Version}\n", "metastore", "acl", "libacl1", "libbsd0").stdout.decode().splitlines()
    assert set(versions) == {"metastore=1.1.2-4+b1", "acl=2.3.1-3", "libacl1=2.3.1-3", "libbsd0=0.11.7-2"}
    package_root = Path("/opt/metadata-tools")
    assert (package_root / "etckeeper.version").read_text().strip() == "1.18.20-1"
    config = package_root / "etc/etckeeper"
    hooks = [config / "pre-commit.d/30store-metadata", config / "init.d/20restore-etckeeper", package_root / "usr/bin/etckeeper"]
    hashes = {str(path.relative_to(package_root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in hooks}
    metastore_version = command(Path("/tmp"), "metastore", "--version").stdout.decode().strip()
    assert metastore_version == "metastore v1.1.2"
    assert command(Path("/tmp"), "etckeeper", "--version").stdout.decode().strip() == "Version: 1.18.20"
    with tempfile.TemporaryDirectory(prefix="metadata-prerequisite-", dir="/tmp") as name:
        root = Path(name)
        seed(root)
        observed = snapshot(root)
        assert observed[TRACKED[2]]["uid"] == pwd.getpwnam("daemon").pw_uid
        assert observed[TRACKED[0]]["mode"] == 0o3750
        assert observed[TRACKED[2]]["xattrs"]["user.ha_ops_fixture"] == BINARY.hex()
        assert "user:65534:r--" in observed[TRACKED[2]]["acl"]
        assert "default:user:65534:r-x" in observed[TRACKED[0]]["acl"]
    emit("prerequisites", packages=versions, etckeeper="1.18.20-1 (extracted)", hooks_sha256=hashes,
         kernel=os.uname().release, architecture=os.uname().machine, cap_eff=caps, mount=tmp_mounts[0],
         metastore_version=metastore_version,
         metastore_sha256=hashlib.sha256(Path(shutil.which("metastore")).read_bytes()).hexdigest())
    return config


@pytest.fixture
def owned(tools):
    with tempfile.TemporaryDirectory(prefix="metadata-evaluation-", dir="/tmp") as name:
        root = Path(name)
        # Traversable only for explicit nonprivileged fixture checks.
        root.chmod(0o755)
        yield root


def initialize(root):
    root.mkdir(exist_ok=True)
    git(root, "init", "-b", "main")


def candidate_env(root, tools):
    # Real packaged hooks copied into a disposable, scope-specific config.
    config = root.parent / (root.name + "-etckeeper-config")
    if not config.exists():
        config.mkdir()
        shutil.copyfile(tools / "etckeeper.conf", config / "etckeeper.conf")
        # /tmp is deliberately noexec. Keep exact executable package hooks on
        # the read-only image; only configuration is disposable.
        for path in tools.iterdir():
            if path.is_dir():
                (config / path.name).symlink_to(path, target_is_directory=True)
        with (config / "etckeeper.conf").open("a") as stream:
            stream.write('\nVCS="git"\nAVOID_DAILY_AUTOCOMMITS=1\nAVOID_COMMIT_BEFORE_INSTALL=1\n')
    return {"ETCKEEPER_CONF_DIR": str(config)}


def capture(root, tool, tools):
    if tool == "metastore":
        return command(root, "metastore", "-s")
    return command(root, "etckeeper", "pre-commit", "-d", str(root), env=candidate_env(root, tools))


def apply(root, tool, tools, **kw):
    if tool == "metastore":
        return command(root, "metastore", "-a", **kw)
    return command(root, "etckeeper", "init", "-d", str(root), env=candidate_env(root, tools), **kw)


def commit(root, message):
    git(root, "add", "--all")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD").stdout.decode().strip()


def assert_restored(root, expected, tool, route):
    actual = snapshot(root)
    if tool == "metastore":
        assert actual == expected, (route, expected, actual)
        outcome = "exact fixture metadata restored"
    else:
        assert {path: basic(value) for path, value in actual.items()} == {path: basic(value) for path, value in expected.items()}
        assert actual[TRACKED[2]]["xattrs"] != expected[TRACKED[2]]["xattrs"]
        assert actual[TRACKED[0]]["acl"] != expected[TRACKED[0]]["acl"]
        outcome = "basic restored; access/default ACL and xattr gap observed"
    emit("restoration", candidate=tool, route=route, outcome=outcome)


@pytest.mark.parametrize("tool", MANIFEST)
def test_deleted_inode_local_revert_and_git_only_transport(owned, tools, tool):
    publisher = owned / "publisher"
    initialize(publisher)
    seed(publisher)
    capture(publisher, tool, tools)
    expected = snapshot(publisher)
    original = commit(publisher, "Historical content and metadata")
    shutil.rmtree(publisher / "parents")
    assert not (publisher / TRACKED[2]).exists() and not (publisher / "parents").exists()
    capture(publisher, tool, tools)
    deletion = commit(publisher, "Delete historical inode and parents")
    remote = owned / "remote.git"
    git(owned, "init", "--bare", "--initial-branch=main", str(remote))
    git(publisher, "remote", "add", "origin", str(remote))
    git(publisher, "push", "origin", "main")
    destination = owned / "destination"
    git(owned, "clone", "--no-hardlinks", str(remote), str(destination))
    remote_publisher = owned / "remote-publisher"
    git(owned, "clone", "--no-hardlinks", str(remote), str(remote_publisher))
    assert not (destination / "parents").exists()
    assert not (destination / ".git/objects/info/alternates").exists()
    assert not (remote_publisher / "parents").exists()
    assert not (remote_publisher / ".git/objects/info/alternates").exists()
    git(publisher, "revert", "--no-edit", deletion)
    assert (publisher / TRACKED[2]).read_bytes() == b"historical: bytes\n"
    assert git(publisher, "show", "HEAD:" + MANIFEST[tool]).stdout == git(publisher, "show", original + ":" + MANIFEST[tool]).stdout
    apply(publisher, tool, tools)
    assert_restored(publisher, expected, tool, "local revert")
    # Independent publisher has only transported Git history, no old inode or
    # sidecar baseline. Its restoring revision is created independently.
    git(remote_publisher, "revert", "--no-edit", deletion)
    assert git(remote_publisher, "show", "HEAD:" + MANIFEST[tool]).stdout == git(publisher, "show", original + ":" + MANIFEST[tool]).stdout
    git(remote_publisher, "push", "origin", "main")
    git(destination, "fetch", "origin")
    git(destination, "reset", "--hard", "origin/main")
    apply(destination, tool, tools)
    assert_restored(destination, expected, tool, "independent deletion clone fetch")
    fresh = owned / "fresh"
    git(owned, "clone", "--no-hardlinks", str(remote), str(fresh))
    assert not (fresh / ".git/objects/info/alternates").exists()
    assert (fresh / TRACKED[2]).stat().st_ino != (publisher / TRACKED[2]).stat().st_ino
    apply(fresh, tool, tools)
    assert_restored(fresh, expected, tool, "fresh restoring clone")


def test_metastore_cannot_materialize_missing_regular_bytes(owned, tools):
    seed(owned)
    command(owned, "metastore", "-s")
    target = owned / TRACKED[2]
    target.unlink()
    result = command(owned, "metastore", "-a", check=False)
    assert not target.exists()
    emit("missing bytes", returncode=result.returncode, stderr=result.stderr.decode(), still_absent=True)


def test_metastore_exact_xattr_replacement_and_acl_ordering(owned, tools):
    seed(owned)
    expected = snapshot(owned)
    command(owned, "metastore", "-s")
    for name in TRACKED:
        path = owned / name
        os.chown(path, 0, 0)
        command(owned, "setfacl", "-b", str(path))
        if path.is_dir():
            command(owned, "setfacl", "-k", str(path))
            command(owned, "setfacl", "-m", "d:u::rwx,d:g::rwx,d:o::rwx", str(path))
        os.chmod(path, 0o777)
        os.setxattr(path, "user.ha_ops_fixture", b"changed")
        os.setxattr(path, "user.stale", b"remove me")
    result = command(owned, "metastore", "-a", check=False)
    actual = snapshot(owned)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert actual == expected, (result.returncode, result.stderr, expected, actual)
    emit("xattr and ACL replacement", returncode=result.returncode, exact=True)


@pytest.mark.parametrize("tool", MANIFEST)
def test_owner_only_special_bits_comparison(owned, tools, tool):
    initialize(owned)
    target = owned / "executable"
    target.write_bytes(b"fixture")
    os.chown(target, pwd.getpwnam("daemon").pw_uid, grp.getgrnam("daemon").gr_gid)
    os.chmod(target, 0o6750)
    expected = readback(target)
    capture(owned, tool, tools)
    os.chown(target, 0, 0)
    os.chmod(target, 0o6750)
    assert readback(target)["mode"] == expected["mode"] and target.stat().st_uid != expected["uid"]
    result = apply(owned, tool, tools, check=False)
    actual = readback(target)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert actual["uid"] == expected["uid"] and actual["gid"] == expected["gid"]
    # Pinned-tool candidate limitation, checked explicitly instead of xfail.
    assert actual["mode"] == (0o750 if tool == "metastore" else 0o6750), (result.returncode, result.stderr, actual)
    emit("owner-only special bits", candidate=tool, returncode=result.returncode,
         expected_mode=oct(expected["mode"]), observed_mode=oct(actual["mode"]), exact=actual == expected)


def test_metastore_unavailable_nss_identity_is_not_exact_capture(owned, tools):
    unknown = 60001
    while True:
        try:
            pwd.getpwuid(unknown)
        except KeyError:
            try:
                grp.getgrgid(unknown)
            except KeyError:
                break
        unknown += 1
    target = owned / "unknown"
    target.write_bytes(b"unknown")
    os.chown(target, unknown, unknown)
    result = command(owned, "metastore", "-s", check=False)
    os.chown(target, 0, 0)
    command(owned, "metastore", "-a", check=False)
    assert target.stat().st_uid != unknown and target.stat().st_gid != unknown
    assert result.stderr, "must expose unresolved NSS capture diagnostic"
    emit("unavailable identity", returncode=result.returncode, stderr=result.stderr.decode(), uid=unknown, omitted=True)


def test_metastore_nonprivileged_partial_apply_requires_readback(owned, tools):
    root = owned / "nonprivileged"
    root.mkdir(mode=0o755)
    target = root / "file"
    target.write_bytes(b"fixture")
    configure(target)
    expected = readback(target)
    command(root, "metastore", "-s")
    (root / ".metadata").chmod(0o644)
    os.chown(target, 0, 0)
    command(root, "setfacl", "-b", str(target))
    os.removexattr(target, "user.ha_ops_fixture")
    os.chmod(target, 0o644)
    def demote():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    result = command(root, "metastore", "-a", check=False, demote=demote)
    assert readback(target) != expected
    assert target.stat().st_uid != expected["uid"]
    assert readback(target)["xattrs"] != expected["xattrs"]
    assert readback(target)["acl"] != expected["acl"]
    emit("nonprivileged partial apply", returncode=result.returncode, stderr=result.stderr.decode(),
         stdout=result.stdout.decode(), ownership_acl_xattr_denied=True, exact=False)


@pytest.mark.parametrize("case", ["malformed", "type-mismatch"])
def test_metastore_adverse_inputs_are_bounded_and_verified(owned, tools, case):
    root = owned / "scope"
    root.mkdir()
    outside = owned / "outside"
    outside.write_bytes(b"outside sentinel")
    outside_expected = readback(outside)
    target = root / "file"
    target.write_bytes(b"historical")
    os.chmod(target, 0o640)
    command(root, "metastore", "-s")
    if case == "malformed":
        (root / ".metadata").write_bytes(b"not a metastore record\x00\xff")
        expected = readback(target)
    else:
        target.unlink()
        target.mkdir(mode=0o700)
        expected = readback(target)
    result = command(root, "metastore", "-a", check=False)
    actual = readback(target)
    assert readback(outside) == outside_expected
    assert actual == expected, (case, result.returncode, result.stderr, expected, actual)
    assert result.returncode != 0 or result.stderr or b"new type" in result.stdout
    emit("adverse input", input=case, returncode=result.returncode, stderr=result.stderr.decode(),
         stdout=result.stdout.decode(), unchanged=True)


@pytest.mark.parametrize("tool", MANIFEST)
def test_whole_scope_capture_couples_unselected_metadata_and_preserves_isolated_index(owned, tools, tool):
    root = owned / "selection"
    initialize(root)
    seed(root, rich=False)
    capture(root, tool, tools)
    base = commit(root, "Baseline A and B")
    (root / "sentinel").write_bytes(b"canonical staged sentinel")
    git(root, "add", "sentinel")
    canonical = (root / ".git/index").read_bytes()
    before_b = readback(root / "b.yaml")
    before_parents = {name: basic(readback(root / name)) for name in TRACKED[:2]}
    for name in (TRACKED[2], "b.yaml"):
        (root / name).write_bytes(b"changed " + name.encode())
        os.chmod(root / name, 0o604)
    # Raw etckeeper stages its shared manifest. Isolate that actual effect.
    isolated = owned / "selection.index"
    env = {"GIT_INDEX_FILE": str(isolated)}
    git(root, "read-tree", base, env=env)
    if tool == "etckeeper":
        command(root, "etckeeper", "pre-commit", "-d", str(root), env={**candidate_env(root, tools), **env})
    else:
        capture(root, tool, tools)
    assert (root / ".git/index").read_bytes() == canonical
    current_manifest = (root / MANIFEST[tool]).read_bytes()
    baseline_manifest = git(root, "show", base + ":" + MANIFEST[tool]).stdout
    assert current_manifest != baseline_manifest
    git(root, "add", "--", TRACKED[2], MANIFEST[tool], env=env)
    tree = git(root, "write-tree", env=env).stdout.decode().strip()
    assert git(root, "show", tree + ":b.yaml").stdout == b"unselected: original\n"
    candidate = owned / "candidate"
    candidate.mkdir()
    # Content is exclusively materialized from the isolated index tree.
    git(root, "--work-tree=" + str(candidate), "checkout-index", "--all", "--force", env=env)
    apply(candidate, tool, tools)
    assert readback(candidate / "b.yaml")["mode"] == 0o604
    assert readback(candidate / "b.yaml")["mode"] != before_b["mode"]
    assert (root / ".git/index").read_bytes() == canonical
    # Experimental proposal: capture a separate reconstructed baseline tree with
    # selected A and its required parents, retaining baseline B metadata.
    proposal = owned / "proposal"
    git(owned, "clone", "--no-hardlinks", str(root), str(proposal))
    apply(proposal, tool, tools)
    shutil.copyfile(root / TRACKED[2], proposal / TRACKED[2])
    os.chmod(proposal / TRACKED[2], 0o604)
    capture(proposal, tool, tools)
    commit(proposal, "Experimental A-only reconstruction")
    apply(proposal, tool, tools)
    assert basic(readback(proposal / "b.yaml")) == basic(before_b)
    assert basic(readback(proposal / TRACKED[2])) == basic(readback(root / TRACKED[2]))
    assert {name: basic(readback(proposal / name)) for name in TRACKED[:2]} == before_parents
    assert (root / ".git/index").read_bytes() == canonical
    emit("selection coupling", candidate=tool, unselected_b_leaked=True, isolated_index_preserved=True,
         experimental_baseline_reconstruction=True)


def assert_merge_observation(tool, same_path, result, unmerged, manifest):
    """A comparison row requires its declared outcome, not any Git response."""
    text_conflict = b"<<<<<<<" in manifest or b"=======" in manifest or b">>>>>>>" in manifest
    if same_path or tool == "metastore":
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
        entries = [line.split() for line in unmerged.splitlines()]
        assert entries and all(len(entry) == 4 for entry in entries), unmerged
        assert {entry[2] for entry in entries} == {"1", "2", "3"}, unmerged
        assert all(entry[3] == MANIFEST[tool] for entry in entries), unmerged
        assert text_conflict == (tool == "etckeeper"), manifest
    else:
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        assert not unmerged, unmerged
        assert not text_conflict, manifest
    return text_conflict


@pytest.mark.parametrize("invalid", ["fatal-refusal", "unmerged-stages", "text-conflict"])
def test_clean_merge_observation_rejects_invalid_outcome(invalid):
    # Portable regression guard: runs on the host without Linux tools or inode
    # claims. Each previously accepted malformed positive result must fail.
    result = subprocess.CompletedProcess(["git", "merge"], 128 if invalid == "fatal-refusal" else 0,
                                        b"", b"fatal: injected refusal" if invalid == "fatal-refusal" else b"")
    unmerged = "" if invalid != "unmerged-stages" else "".join(
        f"100644 {'a' * 40} {stage}\t.etckeeper\n" for stage in (1, 2, 3))
    manifest = b"<<<<<<< ours\n=======\n>>>>>>> theirs\n" if invalid == "text-conflict" else b"clean fixture"
    with pytest.raises(AssertionError):
        assert_merge_observation("etckeeper", False, result, unmerged, manifest)


@pytest.mark.parametrize("invalid", ["fatal-refusal", "unmerged-stages", "text-conflict"])
def test_real_clean_merge_fixture_rejects_injected_invalid_outcome(owned, tools, monkeypatch, invalid):
    real_git = git
    def injected_git(root, *args, **kwargs):
        if args[0] == "merge":
            if invalid == "text-conflict":
                manifest = root / ".etckeeper"
                manifest.write_bytes(b"<<<<<<< ours\n" + manifest.read_bytes() + b"=======\n>>>>>>> theirs\n")
            return subprocess.CompletedProcess(args, 128 if invalid == "fatal-refusal" else 0,
                                               b"", b"fatal: injected refusal" if invalid == "fatal-refusal" else b"")
        if args[:2] == ("ls-files", "--unmerged") and invalid == "unmerged-stages":
            entries = "".join(f"100644 {'a' * 40} {stage}\t.etckeeper\n" for stage in (1, 2, 3))
            return subprocess.CompletedProcess(args, 0, entries.encode(), b"")
        return real_git(root, *args, **kwargs)
    monkeypatch.setitem(globals(), "git", injected_git)
    with pytest.raises(AssertionError):
        test_actual_shared_manifest_merge_behavior(owned, tools, "etckeeper", False)


@pytest.mark.parametrize("tool", MANIFEST)
@pytest.mark.parametrize("same_path", [True, False], ids=["same-path", "different-path"])
def test_actual_shared_manifest_merge_behavior(owned, tools, tool, same_path):
    root = owned / "merge"
    initialize(root)
    seed(root, rich=False)
    capture(root, tool, tools)
    base = commit(root, "Merge baseline")
    git(root, "checkout", "-b", "ours")
    os.chmod(root / TRACKED[2], 0o604)
    capture(root, tool, tools)
    commit(root, "Ours metadata")
    git(root, "checkout", "-b", "theirs", base)
    apply(root, tool, tools)
    os.chmod(root / (TRACKED[2] if same_path else "b.yaml"), 0o620)
    capture(root, tool, tools)
    commit(root, "Theirs metadata")
    git(root, "checkout", "ours")
    result = git(root, "merge", "--no-edit", "theirs", check=False)
    unmerged = git(root, "ls-files", "--unmerged").stdout.decode()
    text_conflict = assert_merge_observation(tool, same_path, result, unmerged,
                                             (root / MANIFEST[tool]).read_bytes())
    if tool == "etckeeper" and not same_path:
        apply(root, tool, tools)
        assert readback(root / TRACKED[2])["mode"] == 0o604
        assert readback(root / "b.yaml")["mode"] == 0o620
    emit("merge", candidate=tool, same_path=same_path, returncode=result.returncode,
         unmerged=bool(unmerged), text_conflict=text_conflict,
         stdout=result.stdout.decode(), stderr=result.stderr.decode())
