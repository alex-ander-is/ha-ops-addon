import html


def diff_line_class(line):
    if line.startswith("@@"):
        return "diff-hunk"
    if line.startswith("+++") or line.startswith("---"):
        return "diff-file"
    if line.startswith("+"):
        return "diff-add"
    if line.startswith("-"):
        return "diff-del"
    if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
        return "diff-marker"
    return "diff-context"


def changed_ranges(old_text, new_text):
    prefix_len = 0
    max_prefix = min(len(old_text), len(new_text))
    while prefix_len < max_prefix and old_text[prefix_len] == new_text[prefix_len]:
        prefix_len += 1

    suffix_len = 0
    max_suffix = min(len(old_text), len(new_text)) - prefix_len
    while (
        suffix_len < max_suffix
        and old_text[len(old_text) - suffix_len - 1] == new_text[len(new_text) - suffix_len - 1]
    ):
        suffix_len += 1

    return (
        (prefix_len, len(old_text) - suffix_len),
        (prefix_len, len(new_text) - suffix_len),
    )


def render_changed_text(text, changed_range):
    start, end = changed_range
    if start >= end:
        return html.escape(text)
    return (
        html.escape(text[:start])
        + "<span class='diff-changed'>"
        + html.escape(text[start:end])
        + "</span>"
        + html.escape(text[end:])
    )


def render_diff_line(line, changed_range=None):
    class_name = diff_line_class(line)
    if changed_range and class_name in {"diff-add", "diff-del"}:
        content = html.escape(line[:1]) + render_changed_text(line[1:], changed_range)
    else:
        content = html.escape(line)
    return f"<span class='diff-line {class_name}'>{content}</span>"


def render_diff_block(lines, start):
    removed = []
    added = []
    index = start
    while index < len(lines) and lines[index].startswith("-") and not lines[index].startswith("---"):
        removed.append(lines[index])
        index += 1
    while index < len(lines) and lines[index].startswith("+") and not lines[index].startswith("+++"):
        added.append(lines[index])
        index += 1

    rendered = []
    pairs = min(len(removed), len(added))
    for pair_index in range(pairs):
        old_range, new_range = changed_ranges(removed[pair_index][1:], added[pair_index][1:])
        rendered.append(render_diff_line(removed[pair_index], old_range))
        rendered.append(render_diff_line(added[pair_index], new_range))
    rendered.extend(render_diff_line(line) for line in removed[pairs:])
    rendered.extend(render_diff_line(line) for line in added[pairs:])
    return rendered, index


def render_conflict_detail(detail):
    lines = []
    raw_lines = detail.splitlines() or [""]
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        if line.startswith("-") and not line.startswith("---"):
            rendered, index = render_diff_block(raw_lines, index)
            lines.extend(rendered)
            continue
        lines.append(render_diff_line(line))
        index += 1
    return (
        "<div class='conflict-diff' role='region' aria-label='Conflict diff'>"
        "<label class='diff-wrap-control'>"
        "<input type='checkbox' class='diff-wrap-toggle'>"
        "<span>Wrap lines</span>"
        "</label>"
        "<div class='diff-lines'>"
        f"{''.join(lines)}"
        "</div>"
        "</div>"
    )


def render_addons(selected, get_installed_addons, addon_slug_value, addon_display_name, addon_is_zigbee2mqtt):
    selected = set(selected)
    try:
        addons = sorted(get_installed_addons(), key=lambda addon: addon_display_name(addon).lower())
    except Exception as exc:
        return f"<p>Add-on discovery unavailable: {html.escape(str(exc))}</p>"

    if not addons:
        return "<p>No installed add-ons found.</p>"

    rows = []
    for addon in addons:
        slug = addon_slug_value(addon)
        if not slug:
            continue
        checked = "checked" if slug in selected else ""
        name = html.escape(addon_display_name(addon))
        hint = "Zigbee2MQTT candidate" if addon_is_zigbee2mqtt(addon) else ""
        rows.append(
            "<label class='check-row'>"
            f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}>"
            f"<span>{name}</span>"
            f"<small>{html.escape(hint)}</small>"
            "</label>"
        )

    return (
        "<form method='post' action='addons' data-auto-submit='change'>"
        "<div class='check-list'>"
        f"{''.join(rows)}"
        "</div>"
        "</form>"
    )


def render_homeassistant_organizer(enabled):
    checked = "checked" if enabled else ""
    return (
        "<form method='post' action='homeassistant-organizer' data-auto-submit='change'>"
        "<div class='check-list'>"
        "<label class='check-row'>"
        f"<input type='checkbox' name='homeassistant_organizer' value='1' {checked}>"
        "<span>Split automations, scripts, and scenes by area in Git</span>"
        "<small>Home Assistant keeps using its normal YAML files.</small>"
        "</label>"
        "</div>"
        "</form>"
    )


def render_include_redundant_data(enabled):
    checked = "checked" if enabled else ""
    return (
        "<form method='post' action='include-redundant-data' data-auto-submit='change'>"
        "<div class='check-list'>"
        "<label class='check-row'>"
        f"<input type='checkbox' name='include_redundant_data' value='1' {checked}>"
        "<span>Include redundant data</span>"
        "<small>Save HA to Git keeps registry noise exactly as Home Assistant writes it.</small>"
        "</label>"
        "</div>"
        "</form>"
    )


def render_conflicts(conflicts, conflict_type=None):
    if not conflicts:
        return "<p>No unresolved Git conflicts.</p>"
    approve_all = ""
    if conflict_type == "save_unknown_base":
        approve_all = (
            "<form method='post' action='approve-save-conflicts' data-async-form='true'>"
            "<button type='submit'>Approve HA to Git</button>"
            "</form>"
        )
    rows = []
    for item in conflicts:
        if isinstance(item, dict):
            path = item.get("path", "")
            detail = item.get("detail", "")
        else:
            path = item
            detail = ""
        escaped = html.escape(path)
        rows.append(
            "<tr>"
            f"<td><code>{escaped}</code></td>"
            "<td class='actions'>"
            "<form method='post' action='resolve-conflict' data-async-form='true'>"
            f"<input type='hidden' name='path' value='{html.escape(path, quote=True)}'>"
            "<input type='hidden' name='choice' value='ha'>"
            "<button type='submit' class='secondary'>Use HA Version</button>"
            "</form>"
            "<form method='post' action='resolve-conflict' data-async-form='true'>"
            f"<input type='hidden' name='path' value='{html.escape(path, quote=True)}'>"
            "<input type='hidden' name='choice' value='git'>"
            "<button type='submit' class='secondary'>Use Git Version</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
        if detail:
            rows.append(
                "<tr class='conflict-detail'>"
                f"<td colspan='2'>{render_conflict_detail(detail)}</td>"
                "</tr>"
            )
    return (
        "<p class='muted'>HA Ops stopped before changing Git because these files differ between live "
        "Home Assistant and the repository, and there is no trusted common base. Choose "
        "<strong>Use HA Version</strong> to save the live Home Assistant file to Git, or choose "
        "<strong>Use Git Version</strong> to keep the repository file unchanged.</p>"
        f"{approve_all}"
        "<div class='table-scroll'>"
        "<table class='conflicts-table'><thead><tr><th>File</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</div>"
    )


def render_deleted_devices_table(rows):
    if not rows:
        return "<p>No deleted_devices entries found.</p>"
    rendered_rows = []
    for row in rows:
        rendered_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('area') or ''))}</td>"
            f"<td><code>{html.escape(str(row.get('id') or ''))}</code></td>"
            f"<td>{html.escape(str(row.get('original_name') or ''))}</td>"
            f"<td>{html.escape(str(row.get('original_device_class') or ''))}</td>"
            "</tr>"
        )
    return (
        "<div class='table-scroll'>"
        "<table class='deleted-devices-table'>"
        "<thead><tr><th>Area</th><th>ID</th><th>Original Name</th>"
        "<th>Original Device Class</th></tr></thead>"
        f"<tbody>{''.join(rendered_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def render_retained_devices_table(rows):
    if not rows:
        return "<p>No retained devices candidates found.</p>"
    rendered_rows = []
    for index, row in enumerate(rows):
        checked = "checked" if row.get("selected", True) else ""
        identifiers = html.escape(str(row.get("identifiers") or ""))
        name = html.escape(str(row.get("name") or ""))
        manufacturer = html.escape(str(row.get("manufacturer") or ""))
        model = html.escape(str(row.get("model") or ""))
        topics = html.escape("\n".join(row.get("retained_topics") or []))
        rendered_rows.append(
            "<tr>"
            f"<td><input type='checkbox' name='candidate' value='{index}' {checked}></td>"
            f"<td><code>{identifiers}</code></td>"
            f"<td>{name}</td>"
            f"<td>{manufacturer} | {model}</td>"
            f"<td><pre>{topics}</pre></td>"
            "</tr>"
        )
    return (
        "<div class='table-scroll'>"
        "<table class='retained-devices-table'>"
        "<thead><tr><th>Delete</th><th>Identifiers</th><th>Name</th>"
        "<th>Manufacturer | Model</th><th>Retained Discovery Topics</th></tr></thead>"
        f"<tbody>{''.join(rendered_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def render_internal_ids_table(rows):
    if not rows:
        return "<p>No internal id migration candidates found.</p>"
    rendered_rows = []
    for index, row in enumerate(rows):
        can_migrate = bool(row.get("changes"))
        checked = "checked" if row.get("selected", True) and can_migrate else ""
        disabled = "" if can_migrate else "disabled"
        path = html.escape(str(row.get("path") or ""))
        rendered_rows.append(
            "<tr>"
            f"<td class='select-col'><input type='checkbox' name='candidate' value='{index}' {checked} {disabled}></td>"
            f"<td class='file-col'><code>{path}</code></td>"
            f"<td class='metric-col'>{html.escape(str(row.get('entity_triggers') or 0))}</td>"
            f"<td class='metric-col'>{html.escape(str(row.get('mqtt_triggers') or 0))}</td>"
            f"<td class='metric-col'>{html.escape(str(row.get('actions') or 0))}</td>"
            f"<td class='metric-col'>{html.escape(str(row.get('conditions') or 0))}</td>"
            f"<td class='metric-col'>{html.escape(str(row.get('unresolved') or 0))}</td>"
            "</tr>"
        )
    return (
        "<div class='action-row'>"
        "<button type='button' class='secondary' data-checkbox-scope='internal-ids' data-checkbox-action='all'>Select all</button>"
        "<button type='button' class='secondary' data-checkbox-scope='internal-ids' data-checkbox-action='none'>Select none</button>"
        "</div>"
        "<div class='table-scroll'>"
        "<table class='internal-ids-table' data-checkbox-scope='internal-ids'>"
        "<colgroup><col class='select-col'><col class='file-col'>"
        "<col class='metric-col'><col class='metric-col'><col class='metric-col'>"
        "<col class='metric-col'><col class='metric-col'></colgroup>"
        "<thead><tr><th>Migrate</th><th>File</th><th>Entity</th>"
        "<th>Z2M</th><th>Actions</th><th>Conditions</th><th>Unresolved</th></tr></thead>"
        f"<tbody>{''.join(rendered_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def render_internal_ids_diffs(rows, render_diff):
    diff_rows = [row for row in rows if row.get("diff")]
    if not diff_rows:
        return "<p>No internal id migration diff available.</p>"
    rendered = []
    for row in diff_rows:
        path = html.escape(str(row.get("path") or ""))
        rendered.append(
            "<details>"
            f"<summary>View diff: <code>{path}</code></summary>"
            f"{render_diff(str(row.get('diff') or ''))}"
            "</details>"
        )
    return "".join(rendered)


def target_addon_slug(item):
    return item.get("resolved_slug") or item.get("addon_slug") or item.get("addon_slug_suffix") or ""


def render_target_row(item, checkbox, label=None, hint=""):
    target = html.escape(str(label or item.get("id")))
    target_type = html.escape(str(item.get("type")))
    source = html.escape(str(item.get("source") or item.get("source_path") or ""))
    live_path = html.escape(str(item.get("live_path", "")))
    addon = html.escape(str(target_addon_slug(item)))
    hint_html = f"<small>{html.escape(hint)}</small>" if hint else ""
    return (
        "<tr>"
        f"<td>{checkbox}</td>"
        f"<td><code>{target}</code>{hint_html}</td>"
        f"<td>{target_type}</td>"
        f"<td><code>{source}</code></td>"
        f"<td><code>{addon}</code></td>"
        f"<td><code>{live_path}</code></td>"
        "</tr>"
    )


def render_targets(
    items,
    selected_addons=None,
    get_installed_addons=None,
    addon_slug_value=None,
    addon_display_name=None,
    addon_is_zigbee2mqtt=None,
):
    selected_addons = set(selected_addons or [])
    items = items or []

    rows = []
    addon_targets = {}
    for item in items:
        if item.get("type") == "addon":
            slug = target_addon_slug(item)
            if slug:
                addon_targets[slug] = item
            continue
        checkbox = "<input type='checkbox' checked disabled>"
        rows.append(render_target_row(item, checkbox))

    addon_error = ""
    if get_installed_addons:
        try:
            addons = sorted(get_installed_addons(), key=lambda addon: addon_display_name(addon).lower())
        except Exception as exc:
            addons = []
            addon_error = f"<p>Add-on discovery unavailable: {html.escape(str(exc))}</p>"
        seen = set()
        for addon in addons:
            slug = addon_slug_value(addon)
            if not slug:
                continue
            seen.add(slug)
            item = addon_targets.get(
                slug,
                {
                    "id": f"addon-{slug}",
                    "type": "addon",
                    "source": f"addons/{slug}",
                    "addon_slug": slug,
                    "allow_protected_storage": False,
                },
            )
            checked = "checked" if slug in selected_addons else ""
            checkbox = f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}>"
            hint = "Zigbee2MQTT candidate" if addon_is_zigbee2mqtt(addon) else ""
            rows.append(render_target_row(item, checkbox, addon_display_name(addon), hint))

        for slug, item in sorted(addon_targets.items()):
            if slug in seen:
                continue
            checked = "checked" if slug in selected_addons else ""
            checkbox = f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}>"
            rows.append(render_target_row(item, checkbox, item.get("id")))
    else:
        for item in items:
            if item.get("type") != "addon":
                continue
            checkbox = "<input type='checkbox' checked disabled>"
            rows.append(render_target_row(item, checkbox))

    if not rows:
        return "<p>No target preview yet. Run an apply after configuring the repository.</p>"

    return (
        f"{addon_error}"
        "<form method='post' action='addons' data-auto-submit='change'>"
        "<table><thead><tr><th>Managed</th><th>Target</th><th>Type</th><th>Source</th><th>Add-on</th><th>Live Path</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</form>"
    )


def render_releases(releases):
    intro = "<p>Snapshots let HA Ops roll back a Git-to-HA apply to a saved local state.</p>"
    if not releases:
        return f"{intro}<p>No local release snapshots yet.</p>"

    rows = []
    for release in releases[:12]:
        name = html.escape(release["name"])
        created_at = html.escape(str(release.get("created_at")))
        backup_slug = html.escape(str(release.get("backup_slug")))
        rows.append(
            "<tr>"
            f"<td><code>{name}</code></td>"
            f"<td>{created_at}</td>"
            f"<td><code>{backup_slug}</code></td>"
            "<td>"
            f"<form method='post' action='rollback' data-async-form='true'>"
            f"<input type='hidden' name='release' value='{name}'>"
            "<button type='submit' class='secondary'>Rollback</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    return (
        f"{intro}"
        "<table><thead><tr><th>Release</th><th>Created</th><th>HA Backup</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def targets_allow_protected_storage(items):
    return any(bool(item.get("allow_protected_storage")) for item in items or [])


def render_git_auth(options, git_auth_mode, load_generated_public_key):
    mode = git_auth_mode(options)
    public_key = html.escape(load_generated_public_key())
    repo_url = options.get("repo_url", "")
    uses_ssh = repo_url.startswith("git@") or repo_url.startswith("ssh://")

    if mode == "manual":
        status = "<p>Using the private key from <code>git_ssh_key</code> in add-on configuration.</p>"
        key_block = ""
    elif mode == "generated":
        status = "<p>Using the deploy key generated and stored inside HA Ops.</p>"
        key_block = (
            "<p>Add this public key to GitHub as a Deploy Key with write access for <code>ha-config</code>.</p>"
            f"<pre>{public_key}</pre>"
        )
    else:
        status = "<p>No SSH key is configured yet.</p>"
        key_block = ""

    hint = ""
    if uses_ssh and mode == "none":
        hint = "<p>Click <strong>Generate Deploy Key</strong>, then paste the public key into GitHub Deploy Keys.</p>"
    elif not uses_ssh:
        hint = "<p>Your repository URL is not SSH-based, so a deploy key may not be needed.</p>"

    action = (
        "<form method='post' action='generate-key' data-async-form='true'>"
        "<button type='submit' class='secondary'>Generate Deploy Key</button>"
        "</form>"
    )
    if mode == "generated":
        action = (
            "<form method='post' action='generate-key' data-async-form='true'>"
            "<button type='submit' class='secondary'>Regenerate Deploy Key</button>"
            "</form>"
        )

    return f"{status}{hint}<div class='actions'>{action}</div>{key_block}"


def render_page(data):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HA Ops</title>
  <style>
    :root {{
      color-scheme: light dark;
      --ha-bg: var(--primary-background-color, #f6f8fb);
      --ha-card-bg: var(--card-background-color, #ffffff);
      --ha-text: var(--primary-text-color, #111827);
      --ha-muted: var(--secondary-text-color, #6b7280);
      --ha-border: var(--divider-color, rgba(0, 0, 0, 0.12));
      --ha-primary: var(--primary-color, #03a9f4);
      --ha-primary-contrast: var(--text-primary-color, #ffffff);
      --ha-error: var(--error-color, #db4437);
      --ha-success: var(--success-color, #43a047);
      --ha-info: var(--info-color, #039be5);
      --ha-warning: var(--warning-color, #f9ab00);
      --ha-radius: var(--ha-card-border-radius, 12px);
      --ha-shadow: var(--ha-card-box-shadow, none);
      --ha-font: var(--paper-font-common-base_-_font-family, system-ui, sans-serif);
      --ha-code-bg: var(--secondary-background-color, rgba(127, 127, 127, 0.08));
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: var(--ha-font);
      color: var(--ha-text);
      background: var(--ha-bg);
      overflow-x: hidden;
    }}
    main {{
      width: 100%;
      max-width: none;
      padding: 16px;
    }}
    .top-grid {{
      display: grid;
      grid-template-columns: 700px minmax(500px, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .card {{
      background: var(--ha-card-bg);
      border: 1px solid var(--ha-border);
      border-radius: var(--ha-radius);
      padding: 20px;
      box-shadow: var(--ha-shadow);
      min-width: 0;
    }}
    .details-card {{
      display: flex;
      flex-direction: column;
      height: var(--details-card-height, auto);
      min-height: 0;
      overflow: hidden;
    }}
    .details-card pre {{
      flex: 1 1 auto;
      min-height: 0;
      white-space: pre;
      overflow-wrap: normal;
    }}
    h1, h2 {{
      margin: 0 0 14px;
      color: var(--ha-text);
    }}
    h1 {{
      font-size: 2rem;
    }}
    h2 {{
      font-size: 1.1rem;
    }}
    p, li {{
      color: var(--ha-muted);
      line-height: 1.55;
    }}
    dl {{
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 10px 14px;
      margin: 18px 0 0;
    }}
    dt {{
      color: var(--ha-muted);
    }}
    dd {{
      margin: 0;
      word-break: break-word;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 0.8rem;
      text-transform: uppercase;
      background: color-mix(in srgb, var(--ha-success) 14%, transparent);
      color: var(--ha-success);
      border: 1px solid color-mix(in srgb, var(--ha-success) 30%, transparent);
    }}
    .badge.error {{
      background: color-mix(in srgb, var(--ha-error) 14%, transparent);
      color: var(--ha-error);
      border-color: color-mix(in srgb, var(--ha-error) 30%, transparent);
    }}
    .badge.running {{
      background: color-mix(in srgb, var(--ha-info) 14%, transparent);
      color: var(--ha-info);
      border-color: color-mix(in srgb, var(--ha-info) 30%, transparent);
    }}
    .badge.conflicts {{
      background: color-mix(in srgb, var(--ha-info) 14%, transparent);
      color: var(--ha-info);
      border-color: color-mix(in srgb, var(--ha-info) 30%, transparent);
    }}
    .badge.interrupted {{
      background: color-mix(in srgb, var(--ha-warning) 14%, transparent);
      color: var(--ha-warning);
      border-color: color-mix(in srgb, var(--ha-warning) 30%, transparent);
    }}
    .badge.pending {{
      background: color-mix(in srgb, var(--ha-warning) 14%, transparent);
      color: var(--ha-warning);
      border-color: color-mix(in srgb, var(--ha-warning) 30%, transparent);
    }}
    .actions {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 22px;
    }}
    .action-row {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    td.actions {{
      margin-top: 0;
      flex-direction: row;
    }}
    .check-list {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .check-row {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 8px 12px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--ha-border);
    }}
    .check-row small {{
      grid-column: 2;
      color: var(--ha-muted);
    }}
    button {{
      border: 1px solid color-mix(in srgb, var(--ha-primary) 35%, transparent);
      border-radius: 999px;
      background: var(--ha-primary);
      color: var(--ha-primary-contrast);
      font-size: 0.96rem;
      padding: 10px 16px;
      cursor: pointer;
      font-weight: 600;
    }}
    button:disabled {{
      opacity: 0.6;
      cursor: default;
    }}
    button.secondary {{
      background: var(--ha-card-bg);
      color: var(--ha-text);
      border-color: var(--ha-border);
    }}
    pre {{
      margin: 0;
      background: var(--ha-code-bg);
      border: 1px solid var(--ha-border);
      border-radius: calc(var(--ha-radius) - 2px);
      padding: 14px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
      color: var(--ha-text);
    }}
    pre + pre {{
      margin-top: 14px;
    }}
    .table-scroll {{
      max-width: 100%;
      overflow-x: auto;
      min-width: 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
      table-layout: fixed;
    }}
    .conflicts-table th:last-child, .conflicts-table td:last-child {{
      width: 430px;
    }}
    .internal-ids-table col.select-col {{
      width: 82px;
    }}
    .internal-ids-table col.file-col {{
      width: auto;
    }}
    .internal-ids-table col.metric-col {{
      width: 96px;
    }}
    .internal-ids-table th,
    .internal-ids-table td {{
      white-space: nowrap;
    }}
    .internal-ids-table .metric-col {{
      text-align: center;
    }}
    .internal-ids-table .file-col {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .internal-ids-table .file-col code {{
      display: block;
      overflow: hidden;
      overflow-wrap: normal;
      text-overflow: ellipsis;
      white-space: nowrap;
      word-break: normal;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--ha-border);
      vertical-align: top;
    }}
    th {{
      color: var(--ha-muted);
      font-weight: 600;
    }}
    code {{
      font-family: ui-monospace, monospace;
      font-size: 0.92em;
      color: var(--ha-text);
      overflow-wrap: anywhere;
    }}
    .conflict-detail td {{
      padding-top: 4px;
      background: color-mix(in srgb, var(--ha-code-bg) 52%, transparent);
      min-width: 0;
    }}
    .conflict-diff {{
      max-width: 100%;
      min-width: 0;
      overflow-x: auto;
      border: 1px solid var(--ha-border);
      border-radius: calc(var(--ha-radius) - 2px);
      background: var(--ha-code-bg);
      font-family: ui-monospace, monospace;
      font-size: 0.92rem;
      line-height: 1.45;
    }}
    .diff-wrap-control {{
      position: sticky;
      left: 0;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      color: var(--ha-muted);
      background: var(--ha-code-bg);
      border-bottom: 1px solid var(--ha-border);
      font-family: inherit;
      font-size: 0.86rem;
      z-index: 1;
    }}
    .diff-lines {{
      min-width: max-content;
    }}
    .diff-line {{
      display: block;
      min-width: max-content;
      padding: 0 12px;
      white-space: pre;
      color: var(--ha-text);
    }}
    .conflict-diff.wrap-lines {{
      overflow-x: hidden;
    }}
    .conflict-diff.wrap-lines .diff-lines {{
      min-width: 0;
    }}
    .conflict-diff.wrap-lines .diff-line {{
      min-width: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .diff-lines .diff-line:first-child {{
      padding-top: 10px;
    }}
    .diff-lines .diff-line:last-child {{
      padding-bottom: 10px;
    }}
    .preview-summary {{
      margin: 0 0 12px;
    }}
    .diff-add {{
      background: color-mix(in srgb, var(--ha-success) 17%, transparent);
    }}
    .diff-del {{
      background: color-mix(in srgb, var(--ha-error) 16%, transparent);
    }}
    .diff-changed {{
      border-radius: 3px;
      padding: 0 1px;
      font-weight: 700;
    }}
    .diff-add .diff-changed {{
      background: color-mix(in srgb, var(--ha-success) 42%, transparent);
      box-shadow: inset 0 -1px 0 color-mix(in srgb, var(--ha-success) 70%, transparent);
    }}
    .diff-del .diff-changed {{
      background: color-mix(in srgb, var(--ha-error) 38%, transparent);
      box-shadow: inset 0 -1px 0 color-mix(in srgb, var(--ha-error) 65%, transparent);
    }}
    .diff-hunk, .diff-file, .diff-marker {{
      color: var(--ha-muted);
      background: color-mix(in srgb, var(--ha-muted) 10%, transparent);
    }}
    .wide {{
      margin-top: 18px;
      min-width: 0;
    }}
    .client-status {{
      margin-top: 14px;
      min-height: 1.4em;
      color: var(--ha-muted);
    }}
    footer {{
      margin-top: 18px;
      color: var(--ha-muted);
      font-size: 0.86rem;
      text-align: center;
    }}
    @media (max-width: 1347px) {{
      .top-grid {{
        grid-template-columns: minmax(0, 1fr);
      }}
      .details-card {{
        height: auto;
        overflow: visible;
      }}
      .details-card pre {{
        flex: none;
      }}
    }}
    @media (max-width: 700px) {{
      main {{
        padding: 12px;
      }}
      .card {{
        padding: 16px;
      }}
      dl {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="top-grid">
      <section class="card control-card">
        <h1>HA Ops</h1>
        <p>Git-backed config deployer for Home Assistant, Mosquitto, and Zigbee2MQTT.</p>
        <div class="badge {data['badge_class']}">{data['status']}</div>
        <dl>
          <dt>Repo URL</dt>
          <dd><code>{data['repo_url'] or "(not configured)"}</code></dd>
          <dt>Branch</dt>
          <dd><code>{data['branch']}</code></dd>
          <dt>Manifest</dt>
          <dd><code>{data['manifest_path']}</code></dd>
          <dt>Git auth</dt>
          <dd><code>{data['auth_mode']}</code></dd>
          <dt>Last run</dt>
          <dd>{data['last_run']}</dd>
          <dt>Release snapshot</dt>
          <dd><code>{data['last_release']}</code></dd>
          <dt>HA backup</dt>
          <dd><code>{data['last_backup_slug']}</code></dd>
          <dt>Latest system backup</dt>
          <dd>{data['latest_backup']}</dd>
        </dl>
        <p id="client-status" class="client-status"></p>
        {data['organizer_html']}
        <div class="actions">
          <div class="action-row">
            <form method="post" action="save-preview" data-async-form="true">
              <button type="submit" class="secondary" {data['action_disabled']}>Preview HA to Git</button>
            </form>
            <form method="post" action="save" data-async-form="true">
              <button type="submit" {data['action_disabled']}>Save HA to Git</button>
            </form>
          </div>
          <div class="action-row">
            {data['include_redundant_data_html']}
          </div>
          <div class="action-row">
            <form method="post" action="preview" data-async-form="true">
              <button type="submit" class="secondary" {data['action_disabled']}>Preview Git to HA</button>
            </form>
            <form method="post" action="{data['apply_action']}" data-async-form="true" {data['apply_confirm']}>
              <button type="submit" {data['action_disabled']}>{data['apply_button_text']}</button>
            </form>
          </div>
          <div class="action-row">
            <form method="post" action="deleted-devices-preview" data-async-form="true">
              <button type="submit" class="secondary" {data['check_deleted_devices_disabled']}>Check deleted_devices</button>
            </form>
          </div>
          <div class="action-row">
            <form method="post" action="retained-devices-preview" data-async-form="true">
              <button type="submit" class="secondary" {data['check_retained_devices_disabled']}>Check retained devices</button>
            </form>
          </div>
          <div class="action-row">
            <form method="post" action="internal-ids-preview" data-async-form="true">
              <button type="submit" class="secondary" {data['check_internal_ids_disabled']}>Check internal ids</button>
            </form>
          </div>
        </div>
      </section>
      <section class="card details-card">
        <h2>Log</h2>
        <pre data-transient="details">{data['details_html']}</pre>
      </section>
    </div>

    {data['apply_preview_section_html']}

    {data['save_preview_section_html']}

    {data['deleted_devices_section_html']}

    {data['retained_devices_section_html']}

    {data['internal_ids_section_html']}

    {data['conflicts_section_html']}

    <section class="card wide">
      <h2>Git Access</h2>
      {data['git_auth_html']}
    </section>

    <section class="card wide">
      <h2>Managed Targets</h2>
      {data['targets_html']}
    </section>

    <section class="card wide">
      <h2>Release Snapshots</h2>
      {data['releases_html']}
    </section>
    <footer>HA Ops {data['version']}</footer>
  </main>
  <script>
    (() => {{
      const clientStatus = document.getElementById("client-status");
      const controlCard = document.querySelector(".control-card");
      const detailsCard = document.querySelector(".details-card");

      function setClientStatus(message) {{
        if (clientStatus) {{
          clientStatus.textContent = message || "";
        }}
      }}

      function isRunning() {{
        const badge = document.querySelector(".badge");
        return badge && badge.textContent.trim().toLowerCase() === "running";
      }}

      function clearTransientDisplay() {{
        const details = document.querySelector("[data-transient='details']");
        const applyPreview = document.querySelector("[data-transient='apply-preview']");
        const savePreview = document.querySelector("[data-transient='save-preview']");
        const deletedDevicesPreview = document.querySelector("[data-transient='deleted-devices-preview']");
        const applyGenerated = document.querySelector("[data-transient='apply-generated']");
        const saveGenerated = document.querySelector("[data-transient='save-generated']");
        const deletedDevicesGenerated = document.querySelector("[data-transient='deleted-devices-generated']");
        if (details) {{
          details.textContent = "No log entries yet.";
        }}
        if (applyPreview) {{
          applyPreview.textContent = "";
        }}
        if (savePreview) {{
          savePreview.textContent = "";
        }}
        if (deletedDevicesPreview) {{
          deletedDevicesPreview.innerHTML = "";
        }}
        if (applyGenerated) {{
          applyGenerated.textContent = "";
        }}
        if (saveGenerated) {{
          saveGenerated.textContent = "";
        }}
        if (deletedDevicesGenerated) {{
          deletedDevicesGenerated.textContent = "";
        }}
      }}

      function syncDetailsHeight() {{
        if (!controlCard || !detailsCard) {{
          return;
        }}
        const controlRect = controlCard.getBoundingClientRect();
        const detailsRect = detailsCard.getBoundingClientRect();
        const sameRow = Math.abs(controlRect.top - detailsRect.top) < 2;
        if (sameRow) {{
          detailsCard.style.setProperty("--details-card-height", `${{controlRect.height}}px`);
        }} else {{
          detailsCard.style.removeProperty("--details-card-height");
        }}
      }}

      if (controlCard && detailsCard) {{
        const resizeObserver = new ResizeObserver(syncDetailsHeight);
        resizeObserver.observe(controlCard);
        window.addEventListener("resize", syncDetailsHeight);
        requestAnimationFrame(syncDetailsHeight);
      }}

      function clearDisplayState() {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon("clear-display-state", new Blob([""], {{
            type: "application/x-www-form-urlencoded"
          }}));
          return;
        }}
        fetch("clear-display-state", {{
          method: "POST",
          keepalive: true,
          headers: {{
            "Accept": "application/json",
            "X-Requested-With": "fetch"
          }}
        }}).catch(() => {{}});
      }}

      function markInternalReload() {{
        try {{
          sessionStorage.setItem("haOpsInternalReload", "1");
        }} catch (_error) {{}}
      }}

      function consumeInternalReload() {{
        try {{
          const value = sessionStorage.getItem("haOpsInternalReload") === "1";
          sessionStorage.removeItem("haOpsInternalReload");
          return value;
        }} catch (_error) {{
          return false;
        }}
      }}

      function reloadSoon(delay) {{
        window.setTimeout(() => {{
          markInternalReload();
          window.location.reload();
        }}, delay);
      }}

      function navigationType() {{
        const entries = performance.getEntriesByType
          ? performance.getEntriesByType("navigation")
          : [];
        if (entries && entries[0] && entries[0].type) {{
          return entries[0].type;
        }}
        if (performance.navigation && performance.navigation.type === 1) {{
          return "reload";
        }}
        if (performance.navigation && performance.navigation.type === 2) {{
          return "back_forward";
        }}
        return "navigate";
      }}

      window.addEventListener("pageshow", (event) => {{
        const internalReload = consumeInternalReload();
        const type = navigationType();
        if (!internalReload && (event.persisted || type === "reload" || type === "back_forward")) {{
          clearTransientDisplay();
          clearDisplayState();
        }}
      }});

      window.addEventListener("pagehide", () => {{
        let internalReload = false;
        try {{
          internalReload = sessionStorage.getItem("haOpsInternalReload") === "1";
        }} catch (_error) {{}}
        if (!internalReload && !isRunning()) {{
          clearDisplayState();
        }}
      }});

      async function submitAsyncForm(form) {{
        const confirmation = form.getAttribute("data-confirm");
        if (confirmation && !window.confirm(confirmation)) {{
          return;
        }}
        const button = form.querySelector("button[type='submit']");
        const originalText = button ? button.textContent : "";
        if (button) {{
          button.disabled = true;
          button.textContent = "Working...";
        }}
        setClientStatus("Working...");
        const preserveDisplayState = form.getAttribute("data-preserve-display-state") === "true";
        if (!preserveDisplayState) {{
          clearTransientDisplay();
        }}

        try {{
          const response = await fetch(form.getAttribute("action"), {{
            method: "POST",
            headers: {{
              "Accept": "application/json",
              "X-Requested-With": "fetch"
            }},
            body: new URLSearchParams(new FormData(form))
          }});

          let payload = {{}};
          try {{
            payload = await response.json();
          }} catch (_error) {{
            payload = {{}};
          }}

          if (!response.ok || payload.ok === false) {{
            setClientStatus(payload.message || "Request failed.");
            reloadSoon(600);
          }} else {{
            setClientStatus(payload.message || "Done. Refreshing...");
            reloadSoon(350);
          }}
        }} catch (error) {{
          setClientStatus(error?.message || "Network error.");
        }} finally {{
          if (button) {{
            button.disabled = false;
            button.textContent = originalText;
          }}
        }}
      }}

      for (const form of document.querySelectorAll("form[data-async-form='true']")) {{
        form.addEventListener("submit", (event) => {{
          event.preventDefault();
          submitAsyncForm(form);
        }});
      }}

      for (const form of document.querySelectorAll("form[data-auto-submit='change']")) {{
        for (const input of form.querySelectorAll("input, select")) {{
          input.addEventListener("change", () => {{
            submitAsyncForm(form);
          }});
        }}
      }}

      for (const button of document.querySelectorAll("button[data-checkbox-scope]")) {{
        button.addEventListener("click", () => {{
          const scope = button.getAttribute("data-checkbox-scope");
          const action = button.getAttribute("data-checkbox-action");
          const checked = action === "all";
          for (const input of document.querySelectorAll(`table[data-checkbox-scope="${{scope}}"] input[type="checkbox"]`)) {{
            if (!input.disabled) {{
              input.checked = checked;
            }}
          }}
        }});
      }}

      for (const toggle of document.querySelectorAll(".diff-wrap-toggle")) {{
        toggle.addEventListener("change", () => {{
          const diff = toggle.closest(".conflict-diff");
          if (diff) {{
            diff.classList.toggle("wrap-lines", toggle.checked);
          }}
        }});
      }}

      if (isRunning()) {{
        reloadSoon(3000);
      }}
    }})();
  </script>
</body>
</html>"""
