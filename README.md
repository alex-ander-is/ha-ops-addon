# ha-ops-addon

Home Assistant App repository for HA Ops.

## Install in Home Assistant

1. Open Home Assistant.
2. Go to `Settings` -> `Apps` -> `Install app` (`App store`).
3. Open the three-dot menu in the top right and choose `Repositories`.
4. Paste this URL:

```text
https://github.com/alex-ander-is/ha-ops-addon
```

5. Click `Add`, close the dialog, and wait until `HA Ops` appears in the store.
6. Open `HA Ops`, click `Install`, then open the `Configuration` tab.
7. Set your Git repository settings, especially `repo_url`, `repo_branch`, and `git_ssh_key` if the repository is private.
8. Click `Save`, then `Start`.

`ha-ops` is an ingress UI for managing a Git-backed Home Assistant configuration:

- Preview live Home Assistant config before saving it to Git
- Save selected Home Assistant preview changes to the configured Git branch
- Preview Git changes through service branch merges before applying them to Home Assistant
- Select exactly which preview files Save or Apply should process
- Apply Git config as an overlay, with deletions only when explicitly selected from a preview
- Discover installed Apps and let the user choose which App configs are managed
- Require fresh system backups and create full Home Assistant backups when needed
- Create pruned local release snapshots
- Roll back from saved local releases

See [`ha-ops/README.md`](./ha-ops/README.md) for setup and behavior.

## Releasing HA Ops

Classify the change before preparing a release: `major` for an incompatible
change (the first digit increases), `minor` for new or changed compatible
functionality, or `patch` for a compatible fix or maintenance. This rule also
applies while the App is below `1.0`: an incompatible `0.Y.Z` release becomes
`1.0.0`. The history of `0.10.x` patch tags is not a version-selection rule.

Stage the intended code changes, then run `./release --kind minor` (or the
appropriate kind). It computes the version from `ha-ops/config.yaml`, asks for
an impact reason and multiline changelog entry, and shows the staged release
files before the final confirmation. Answering `N` stops without a commit, tag,
or push. Answering `y` commits the staged changes, creates the matching
annotated tag, and publishes through `./publish-release`. The commit records
`Release-Type` and `Release-Impact`; both the publisher and pre-push hook
verify them against the version increment and changelog.

## Presentation Assets

- `ha-ops/icon.png` is the Home Assistant Apps list icon.
- `ha-ops/logo.png` is the App presentation logo.
- Home Assistant update indicators come from Supervisor version state, not from these image files.

For local HAOS development, clone this repository into:

```text
/addons/ha-ops-addon
```
