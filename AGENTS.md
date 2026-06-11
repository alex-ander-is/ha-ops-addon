# Agent Instructions

All docs and interface must be written in US English.
For `ha-ops-addon`, every block/bunch of changes must bump `ha-ops/config.yaml` version and add a matching `ha-ops/CHANGELOG.md` entry before last of commits. If the work involves series of commits, bump should be performed at the end. The commit that bumps `ha-ops/config.yaml` must also create a Git tag with exactly the same version, for example version `0.6.9` must have tag `0.6.9`. This is required because Home Assistant add-on updates depend on the version field.
When a bug is found or fixed, always add or update regression tests that fail on the bug and pass with the fix. Do this automatically; the user should not need to ask for tests.
If tests are red after your changes, fix them yourself without waiting for an explicit user command or approval, then report what failed and what was fixed. If the fix belongs to the current unpushed work, include it in the current change set. If it fixes an older unrelated bug, make a separate commit named `Bugfix: ...`.
Avoid redundant test runs. This repository has a pre-push hook that runs the HA Ops test suite; do not also run that exact suite manually immediately before pushing unless code changed after the last run or you need faster feedback before committing. Prefer pushing branch and tags together when practical so the hook runs once.
When adding a new feature that persists UI or workflow state, define how that state is cleared on refresh, restart, and version update. Add or update tests that prove stale state does not reappear without the user starting that feature again.
Disabled buttons must be visibly disabled on their own, not only slightly different from enabled buttons. Use a pale gray background, muted text, and muted border for every disabled button, and keep this rule for new UI.

Before changing or reviewing the HA Ops 0.8 service-branch preview/save/apply
flow, read `ha-ops/docs/service-branch-merge-contract.md`. It documents the
parts of the branch, preview, conflict, `.storage`, and UI behavior that are
intentional versus unsafe shortcuts.
