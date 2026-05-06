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
        "<form method='post' action='addons' data-async-form='true'>"
        "<div class='check-list'>"
        f"{''.join(rows)}"
        "</div>"
        "<div class='actions'><button type='submit' class='secondary'>Save Add-on Selection</button></div>"
        "</form>"
    )


def render_conflicts(conflicts):
    if not conflicts:
        return "<p>No unresolved Git conflicts.</p>"
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
        "<div class='table-scroll'>"
        "<table class='conflicts-table'><thead><tr><th>File</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</div>"
    )


def render_targets(items):
    if not items:
        return "<p>No target preview yet. Run an apply after configuring the repository.</p>"

    rows = []
    for item in items:
        target = html.escape(str(item.get("id")))
        target_type = html.escape(str(item.get("type")))
        source = html.escape(str(item.get("source") or item.get("source_path")))
        live_path = html.escape(str(item.get("live_path", "")))
        addon = html.escape(str(item.get("resolved_slug") or item.get("addon_slug") or item.get("addon_slug_suffix") or ""))
        protected_storage = "yes" if item.get("allow_protected_storage") else "no"
        rows.append(
            "<tr>"
            f"<td><code>{target}</code></td>"
            f"<td>{target_type}</td>"
            f"<td><code>{source}</code></td>"
            f"<td><code>{addon}</code></td>"
            f"<td><code>{live_path}</code></td>"
            f"<td>{protected_storage}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Target</th><th>Type</th><th>Source</th><th>Add-on</th><th>Live Path</th><th>Protected Storage</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_releases(releases):
    if not releases:
        return "<p>No local release snapshots yet.</p>"

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
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--ha-card-bg);
      border: 1px solid var(--ha-border);
      border-radius: var(--ha-radius);
      padding: 20px;
      box-shadow: var(--ha-shadow);
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
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 22px;
    }}
    td.actions {{
      margin-top: 0;
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
    .table-scroll {{
      max-width: 100%;
      overflow-x: auto;
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
    }}
    .conflict-diff {{
      max-width: 100%;
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
    @media (max-width: 640px) {{
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
    <div class="grid">
      <section class="card">
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
          <dt>Preview deletions</dt>
          <dd><code>{data['preview_deletions']}</code></dd>
        </dl>
        <p>{data['message']}</p>
        <p id="client-status" class="client-status"></p>
        <div class="actions">
          <form method="post" action="save" data-async-form="true">
            <button type="submit" {data['action_disabled']}>Save HA to Git</button>
          </form>
          <form method="post" action="preview" data-async-form="true">
            <button type="submit" class="secondary" {data['action_disabled']}>Preview Git to HA</button>
          </form>
          <form method="post" action="apply" data-async-form="true" {data['apply_confirm']}>
            <button type="submit" class="secondary" {data['action_disabled']}>Apply Git to HA</button>
          </form>
        </div>
      </section>
      <section class="card">
        <h2>Last Run Details</h2>
        <pre>{data['details_html']}</pre>
      </section>
    </div>

    <section class="card wide">
      <h2>Apply Preview</h2>
      <p>Generated at {data['diff_generated_at']}</p>
      <pre>{data['diff_html']}</pre>
    </section>

    <section class="card wide">
      <h2>Save Candidates</h2>
      <pre>{data['save_candidates_html']}</pre>
    </section>

    <section class="card wide">
      <h2>Git Conflicts</h2>
      {data['conflicts_html']}
    </section>

    <section class="card wide">
      <h2>Git Access</h2>
      {data['git_auth_html']}
    </section>

    <section class="card wide">
      <h2>Managed Targets</h2>
      {data['targets_html']}
    </section>

    <section class="card wide">
      <h2>Managed Add-ons</h2>
      {data['addons_html']}
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

      function setClientStatus(message) {{
        if (clientStatus) {{
          clientStatus.textContent = message || "";
        }}
      }}

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
            window.setTimeout(() => window.location.reload(), 600);
          }} else {{
            setClientStatus(payload.message || "Done. Refreshing...");
            window.setTimeout(() => window.location.reload(), 350);
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

      for (const toggle of document.querySelectorAll(".diff-wrap-toggle")) {{
        toggle.addEventListener("change", () => {{
          const diff = toggle.closest(".conflict-diff");
          if (diff) {{
            diff.classList.toggle("wrap-lines", toggle.checked);
          }}
        }});
      }}

      const badge = document.querySelector(".badge");
      if (badge && badge.textContent.trim().toLowerCase() === "running") {{
        window.setTimeout(() => window.location.reload(), 3000);
      }}
    }})();
  </script>
</body>
</html>"""
