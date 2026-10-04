# Candidate native container tree: step 1

**Step 1 local topology proof passed on October 5, 2026; production migration
remains unverified.** The
ordinary-directory fixture passes Git mechanics, and the local Colima runner
passed the real Linux bind-mount proof with full container replacement and
successful cleanup. Its local report is
`dev/virtualization/.local/topology-report.json` (ignored by Git). This does
not recreate the live HA Ops container or activate the native-tree design.
Adding a workflow is not CI execution. The required proof also runs for
topology dependency changes: the workflow has identical
`push` and `pull_request` filters for all `ha-ops/**` code, configuration and
build inputs, plus `repository.yaml`, `requirements-dev.txt`, `TESTING.md` and
the workflow itself. Publishing for CI requires separate authorization and a
publishing route consistent with the repository's release policy.

The candidate Git working tree is the filesystem root `/` **inside the HA Ops
container**. The HA host root is not a working tree. This work adds proof
tooling and documentation only: production Save/Apply, UI, migration, and
public APIs retain their existing behavior.

## Read-only live inventory

The existing read-only snapshot from October 4, 2026 observed HA Ops 2.3.1,
Core 2026.9.4, and HA OS 18.3. The container root is a writable overlay. All
five destinations below are reachable real directories with read/write/search
access, and Docker reports writable bind mounts backed by ext4, device 179:4.
The paths are inventory facts, never test inputs.

| Container destination | Observed host source | Access |
| --- | --- | --- |
| `/homeassistant` | `/mnt/data/supervisor/homeassistant` | RW |
| `/addon_configs` | `/mnt/data/supervisor/app_configs` | RW |
| `/config` | `/mnt/data/supervisor/app_configs/07ef30c0_ha_ops` | RW |
| `/data` | `/mnt/data/supervisor/apps/data/07ef30c0_ha_ops` | RW |
| `/backup` | `/mnt/data/supervisor/backup` | RW |

`/config` and `/addon_configs/07ef30c0_ha_ops` share inode 409983 and the same
device: they expose the same directory. `/data` matches its host source inode
394126. Neither `/.git` nor `/.ha-ops-repository` exists. Device equality alone
does not prove bind topology. Docker mount destinations/types/sources and
container mount information establish the current mapping; no live container
was recreated, so this inventory does not establish persistence after replacement.

## Candidate layout and proof boundaries

Git receives explicit `--git-dir=/data/native/repository.git` and
`--work-tree=/`. Specific repository-only files live under
`/data/native/repository-files`; a policy copy lives at
`/data/native/root-ignore-policy`. These are candidate service paths tested in
isolation, not active production paths. The current legacy `repo_path` is a
relative directory inside `/data`; default and nested relative values are
covered. Absolute values and overlap with the candidate service paths block
initialization.

The portable fixture uses ordinary temporary directories. It checks explicit
Git locations, exact policy bytes, staging and persistence mechanics. Its
synthetic destination evidence must never be interpreted as real mount proof.

The separate Docker runner requires a local Unix endpoint and a Linux engine.
It pins that validated endpoint with `--host` and freezes its routing
environment before the engine query and every build/run/inspect/exec/cleanup
command. Later CLI default-context or environment changes cannot redirect it.
It builds a disposable fixture image from only test tooling, then binds fresh
temporary sources at `/homeassistant`, `/addon_configs`, `/config`, `/data`,
and `/backup`. `/config` binds the exact subdirectory used beneath
`/addon_configs`. Runtime network is disabled; no host root, Docker socket,
live HA source, existing container, or privileged mode is used. Build setup
downloads the base image and Git package.

In both containers the runner checks exact Docker bind source/destination/type
and writable access, `/proc/self/mountinfo`, directory availability, and the
duplicate directory's inode/device identity. It records the first container's
exact file bytes, refs, commit and blob IDs/bytes outside the container before
removing that container entirely. A distinct replacement uses the same five
sources. Persistent live files, excluded sentinels, repository-only files and
Git objects must match. Root-layer sentinels and `/.gitignore` must disappear.
Staging must first fail for the missing policy, then the saved policy is copied
back to `/.gitignore`, validated, and staging/index checks are repeated. A
restart or retained writable layer cannot satisfy this proof.

## Mandatory future staging contract

The root ignore policy denies every path by default and reopens only exact
selected fixture files, `/.gitignore`, and specific repository-only files.
Selected names include spaces and literal glob characters. Newline allow
patterns are refused; a prohibited newline filename exercises exact NUL path
identity in the index auditor.

Both `git add .` and `git add -A` run from the container root. An independent
auditor compares the **entire** `git ls-files -z` index against an exact allowed
byte-path set. Git metadata, every other `/data` path, the configured old
checkout, recovery/releases/work, `/backup`, duplicate `/config`, unrelated
root files and unselected siblings must stay out. Nested ignore overrides,
previously tracked prohibited files and `git add -f` can bypass ignore; the
tests first prove those paths enter the index, then require the auditor to
reject it. A future adapter must audit before commit, staging acceptance or
migration. This change does not activate that adapter.

Missing, empty, altered, or symlinked root policy; a missing or inconsistent
saved policy; unavailable, read-only, symlinked, or unconfirmed destinations;
and occupied, symlinked, or overlapping service paths are blockers for staging
and migration. Initialization must preserve existing sentinel bytes. Merely
passing `git check-ignore` is insufficient.

The runner bounds subprocesses and removes only its exact generated container
and image identities and owned temporary directory. Removal and absence
verification are independent attempts for every owned resource; an earlier
error cannot skip later resources. Errors are aggregated after all attempts,
and cleanup failure is a test failure. Missing Docker, inaccessible daemon or non-Linux engine is a nonzero
unverified result, never a successful pytest skip.

## References

- [Git explicit repository and work-tree options](https://git-scm.com/docs/git)
- [Git ignore precedence and tracked-file behavior](https://git-scm.com/docs/gitignore)
- [Git add and force](https://git-scm.com/docs/git-add)
- [NUL-delimited Git index paths](https://git-scm.com/docs/git-ls-files)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)
- [Docker CLI daemon and context options](https://docs.docker.com/reference/cli/docker/)
- [Home Assistant App configuration and persistent data](https://developers.home-assistant.io/docs/apps/configuration/)
