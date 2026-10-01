# 2026-09-30

- Managed Targets
  - Persona should observe the explanation of the section and what's the impact between abstract choices
  - The table is
    - inside the section and is collapsible the same way as DIFFs inside the Change List
    - Is collapsed by default
    - Persona can expand and collapse the section
    - Reload of the page would serve collapsed section
      - No need to remember the state between
        - sessions / reloads
        - multiple tabs or windows

# 2026-10-01

- Proceed with Git to HA without backup
  - Implemented in HA Ops 2.2.0
  - Persona should be able to proceed with additional step of confirmation
    - When there is no Backup
      - The following messange on the same line as where the button [Apply Git to HA] is located will be displayed:
        - `[ERROR] No fresh system backup found within 24 hour(s)`
      - Following buttons: [Acknowledge & Proceed] [Retry Git to HA]
        - The button [Retry Git to HA] replaces disabled [Apply Git to HA]
        - While [Retry Git to HA] means re-check the presence of fresh backup and proceed immediately if the backup is fresh now
  - [Acknowledge & Proceed] will bypass the requirement to have a fresh backup
    - In this case there is no need to double check the freshness of backup as persona action already acknowledges the impact
  - No [Cancel] button is needed, persona can simply ignore furhter steps.
