"""Executed only inside the owned disposable integration container."""

import json
import os
from pathlib import Path
import sys

from native_topology_helpers import (
    Blocked, DESTINATIONS, POLICY, SLUG, adverse_index_proof, broad_add_proof,
    git, render_policy, seed, snapshot, stage, verify_snapshot, write,
)


def mount_evidence():
    rows = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        columns = line.split()
        destination = columns[4]
        if destination in {"/" + p for p in DESTINATIONS}:
            assert "rw" in columns[5].split(","), destination
            rows[destination[1:]] = True
    assert set(rows) == set(DESTINATIONS), "Missing real mountinfo destinations"
    for destination in DESTINATIONS:
        path = Path("/") / destination
        assert path.is_dir() and not path.is_symlink()
        assert os.access(path, os.R_OK | os.W_OK | os.X_OK)
    alias = Path("/config").stat()
    original = (Path("/addon_configs") / SLUG).stat()
    assert (alias.st_dev, alias.st_ino) == (original.st_dev, original.st_ino)
    return rows


def main():
    if not Path("/.dockerenv").is_file() or os.environ.get("NATIVE_TOPOLOGY_FIXTURE") != "owned-disposable-test":
        raise RuntimeError("Probe refuses host root execution")
    root = Path("/")
    evidence = mount_evidence()
    phase = sys.argv[1]
    if phase == "first":
        payloads = seed(root)
        write(root, "root-ephemeral-sentinel", b"writable-layer sentinel\n")
        assert Path("/config/settings.yaml").read_bytes() == payloads[f"addon_configs/{SLUG}/settings.yaml"]
        broad_add_proof(root, evidence)
        adverse_index_proof(root, evidence)
        for path in ("data/forbidden\nidentity", "homeassistant/nested/unselected.yaml"):
            payloads[path] = (root / path).read_bytes()
        git(root, "commit", "-m", "Safe disposable candidate tree")
        git(root, "update-ref", "refs/heads/persisted-proof", "HEAD")
        broad_add_proof(root, evidence)
        expected = snapshot(root, payloads)
        expected["files"]["config/settings.yaml"] = expected["files"][f"addon_configs/{SLUG}/settings.yaml"]
        expected["files"]["config/private.json"] = expected["files"][f"addon_configs/{SLUG}/private.json"]
        result = {"snapshot": expected, "mounts": evidence}
    elif phase == "second":
        expected = json.load(sys.stdin)["snapshot"]
        assert not Path("/.gitignore").exists(), "Root policy incorrectly retained"
        assert not Path("/root-ephemeral-sentinel").exists(), "Container layer incorrectly retained"
        verify_snapshot(root, expected)
        try:
            stage(root, evidence)
        except Blocked:
            pass
        else:
            raise AssertionError("Missing root policy allowed staging")
        Path("/.gitignore").write_bytes((root / POLICY).read_bytes())
        assert Path("/.gitignore").read_bytes() == render_policy()
        broad_add_proof(root, evidence)
        verify_snapshot(root, expected)
        result = {"recreation": "passed", "exact_bytes_refs_blobs": "passed",
                  "missing_policy_block": "passed", "broad_add_index_audit": "passed", "mounts": evidence}
    else:
        raise RuntimeError("Unknown probe phase")
    result["git"] = git(root, "--version").decode().strip()
    result["kernel"] = os.uname().release
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
