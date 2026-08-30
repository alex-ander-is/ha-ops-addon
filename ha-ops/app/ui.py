import html
import json
import re

import i18n


def _(key, **values):
    return i18n.t(key, **values)


def js_string(value):
    return (
        json.dumps(str(value), ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def js_t(key, **values):
    return js_string(_(key, **values))


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


UNICODE_ESCAPE_RE = re.compile(r"\\(?:U[0-9A-Fa-f]{8}|u[0-9A-Fa-f]{4})")


def unicode_escape_character(value):
    digits = value[2:]
    codepoint = int(digits, 16)
    if 0xD800 <= codepoint <= 0xDFFF:
        return None
    try:
        return chr(codepoint)
    except ValueError:
        return None


def render_diff_text(text):
    parts = []
    last = 0
    for match in UNICODE_ESCAPE_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        value = match.group(0)
        character = unicode_escape_character(value)
        if character is None:
            parts.append(html.escape(value))
        else:
            escaped_character = html.escape(character, quote=True)
            parts.append(
                "<span class='unicode-escape' "
                f"title='{escaped_character}' data-unicode-char='{escaped_character}'>"
                f"{html.escape(value)}</span>"
            )
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def expand_changed_range_for_unicode_escapes(text, start, end):
    for match in UNICODE_ESCAPE_RE.finditer(text):
        if match.start() < end and start < match.end():
            start = min(start, match.start())
            end = max(end, match.end())
    return start, end


def render_changed_text(text, changed_range):
    start, end = changed_range
    start, end = expand_changed_range_for_unicode_escapes(text, start, end)
    if start >= end:
        return render_diff_text(text)
    return (
        render_diff_text(text[:start])
        + "<span class='diff-changed'>"
        + render_diff_text(text[start:end])
        + "</span>"
        + render_diff_text(text[end:])
    )


def render_diff_line(line, changed_range=None):
    class_name = diff_line_class(line)
    if changed_range and class_name in {"diff-add", "diff-del"}:
        content = html.escape(line[:1]) + render_changed_text(line[1:], changed_range)
    else:
        content = render_diff_text(line)
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


def render_conflict_detail(detail, include_wrap_control=True):
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
    wrap_control = (
        "<label class='diff-wrap-control'>"
        "<input type='checkbox' class='diff-wrap-toggle'>"
        f"<span>{_('button.wrap_lines_toggle')}</span>"
        "</label>"
        if include_wrap_control
        else ""
    )
    return (
        f"<div class='conflict-diff' role='region' aria-label='{_('title.conflict_diff')}'>"
        f"{wrap_control}"
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
        return f"<p>{html.escape(_('error.addon_discovery', error=str(exc)))}</p>"

    if not addons:
        return f"<p>{_('text.no_installed_addons')}</p>"

    rows = []
    for addon in addons:
        slug = addon_slug_value(addon)
        if not slug:
            continue
        checked = "checked" if slug in selected else ""
        name = html.escape(addon_display_name(addon))
        hint = _("value.zigbee2mqtt_candidate") if addon_is_zigbee2mqtt(addon) else ""
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


def render_homeassistant_organizer(enabled, projection_available=False, disabled=False):
    checked = " checked" if enabled and projection_available else ""
    disabled_attr = "" if projection_available and not disabled else " disabled"
    label_key = "text.split_organizer" if projection_available else "text.split_organizer_blocked"
    notice_key = "notice.organizer" if projection_available else "notice.organizer_blocked"
    return (
        "<form method='post' action='homeassistant-organizer' data-auto-submit='change'>"
        "<div class='check-list'>"
        "<label class='check-row'>"
        f"<input type='checkbox' name='homeassistant_organizer' value='1'{checked}{disabled_attr}>"
        f"<span>{_(label_key)}</span>"
        f"<small>{_(notice_key)}</small>"
        "</label>"
        "</div>"
        "</form>"
    )


def render_include_redundant_data(enabled, disabled=False):
    checked = " checked" if enabled else ""
    disabled_attr = " disabled" if disabled else ""
    return (
        "<form method='post' action='include-redundant-data' data-auto-submit='change'>"
        "<div class='check-list'>"
        "<label class='check-row'>"
        f"<input type='checkbox' name='include_redundant_data' value='1'{checked}{disabled_attr}>"
        f"<span>{_('label.include_redundant_data')}</span>"
        f"<small>{_('notice.include_redundant_data')}</small>"
        "</label>"
        "</div>"
        "</form>"
    )


def render_conflicts(conflicts, conflict_type=None, actions_disabled=False):
    if not conflicts:
        return f"<p>{_('text.no_unresolved_git_conflicts')}</p>"
    disabled = " disabled" if actions_disabled else ""
    approve_all = ""
    if conflict_type == "save_unknown_base":
        approve_all = (
            "<form method='post' action='approve-save-conflicts' data-async-form='true'>"
            f"<button type='submit'{disabled}>{_('action.approve_ha_to_git')}</button>"
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
            f"<button type='submit' class='secondary'{disabled}>{_('action.use_ha_version')}</button>"
            "</form>"
            "<form method='post' action='resolve-conflict' data-async-form='true'>"
            f"<input type='hidden' name='path' value='{html.escape(path, quote=True)}'>"
            "<input type='hidden' name='choice' value='git'>"
            f"<button type='submit' class='secondary'{disabled}>{_('action.use_git_version')}</button>"
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
        "<p class='muted'>"
        + html.escape(
            _("notice.conflict_resolution", ha_choice=_("action.use_ha_version"), git_choice=_("action.use_git_version"))
        )
        + "</p>"
        f"{approve_all}"
        "<div class='table-scroll'>"
        f"<table class='conflicts-table'><thead><tr><th>{_('label.file')}</th><th>{_('table.action')}</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</div>"
    )


def diff_path_relative(path):
    if not path or path == "/dev/null":
        return None
    path = path[2:] if path.startswith(("a/", "b/")) else path
    for marker in ("/baseline/", "/preview/", "/save-to-git-preview/", "/apply-preview/"):
        if marker in path:
            return path.rsplit(marker, 1)[1]
    return path


def match_preview_path(raw_path, paths, current_target=None):
    relative = diff_path_relative(raw_path)
    if not relative:
        return None
    candidates = [relative]
    if current_target and not relative.startswith(f"{current_target}/"):
        candidates.append(f"{current_target}/{relative}")
    for candidate in candidates:
        for path in paths:
            if candidate == path or candidate.endswith(f"/{path}"):
                return path
    return None


def diff_git_path(line):
    parts = line.split()
    if len(parts) >= 4:
        return parts[3]
    return None


def diff_command_path(line):
    parts = line.split()
    if len(parts) >= 4:
        return parts[-1]
    return None


def diff_header_path(line):
    return line[4:].split("\t", 1)[0]


def split_preview_diff_by_path(detail, paths):
    path_set = set(paths)
    chunks = {}
    summary = []
    current_target = None
    current_path = None
    current_lines = []
    pending_old_path = None

    def flush():
        nonlocal current_path, current_lines
        if not current_lines:
            return
        if current_path in path_set:
            chunks.setdefault(current_path, []).extend(current_lines)
        else:
            summary.extend(current_lines)
        current_lines = []
        current_path = None

    for line in detail.splitlines():
        if line.startswith("## "):
            flush()
            current_target = line[3:].strip()
            summary.append(line)
            continue
        if line.startswith("diff --git "):
            flush()
            current_path = match_preview_path(diff_git_path(line), paths, current_target)
            current_lines = [line]
            pending_old_path = None
            continue
        if line.startswith("diff "):
            flush()
            current_path = match_preview_path(diff_command_path(line), paths, current_target)
            current_lines = [line]
            pending_old_path = None
            continue
        if line.startswith("--- "):
            if current_lines and current_path is None:
                flush()
            if not current_lines:
                current_lines = []
            pending_old_path = diff_header_path(line)
            current_lines.append(line)
            continue
        if line.startswith("+++ "):
            new_path = diff_header_path(line)
            if current_path is None:
                current_path = match_preview_path(new_path, paths, current_target) or match_preview_path(
                    pending_old_path, paths, current_target
                )
            current_lines.append(line)
            continue
        if current_lines:
            current_lines.append(line)
        else:
            summary.append(line)
    flush()
    return {path: "\n".join(lines) for path, lines in chunks.items()}, "\n".join(summary).strip()


def render_deleted_devices_table(rows):
    if not rows:
        return f"<p>{_('text.no_deleted_devices')}</p>"

    def model_value(row):
        model = " / ".join(str(value) for value in (row.get("recovered_model"), row.get("recovered_model_id")) if value)
        return model

    def device_value(row):
        return "\n".join(
            value
            for value in (
                str(row.get("recovered_manufacturer") or ""),
                model_value(row),
            )
            if value
        )

    def identifiers_value(row):
        identifiers = row.get("recovered_identifiers") or []
        rendered_identifiers = []
        for identifier in identifiers[:3]:
            if isinstance(identifier, list):
                rendered_identifiers.append(":".join(str(item) for item in identifier))
            else:
                rendered_identifiers.append(str(identifier))
        return ", ".join(rendered_identifiers)

    def source_value(row):
        source_commit = str(row.get("source_commit") or "")
        return " ".join(
            value
            for value in (
                source_commit[:12] if source_commit else "",
                str(row.get("source_path") or ""),
            )
            if value
        )

    def plain_value(key):
        return lambda row: str(row.get(key) or "")

    columns = [
        ("area", _("label.area"), plain_value("area"), False),
        ("id", "ID", plain_value("id"), True),
        ("entity-id", "Entity ID", plain_value("entity_id"), True),
        ("name", _("label.name"), plain_value("recovered_name"), False),
        ("device", "Manufacturer / Model", device_value, False),
        ("identifiers", _("label.identifiers"), identifiers_value, True),
        ("original-name", _("label.original_name"), plain_value("original_name"), False),
        ("original-device-class", _("label.original_device_class"), plain_value("original_device_class"), False),
        ("source", _("label.source"), source_value, True),
    ]
    visible_columns = [
        column
        for column in columns
        if column[0] == "id" or any(column[2](row).strip() for row in rows)
    ]
    header = "".join(
        f"<div class='deleted-device-header-cell deleted-device-col-{key}'>{label}</div>"
        for key, label, _, _ in visible_columns
    )
    rendered_rows = []
    for row in rows:
        cells = []
        for key, _label, value_for_row, is_code in visible_columns:
            value = html.escape(value_for_row(row)).replace("\n", "<br>")
            class_attr = f" class='deleted-device-cell deleted-device-cell-{key} deleted-device-col-{key}'"
            content = f"<code>{value}</code>" if is_code else value
            cells.append(f"<div{class_attr}>{content}</div>")
        rendered_rows.append(f"<div class='deleted-device-row'>{''.join(cells)}</div>")
    return (
        "<div class='table-scroll'>"
        "<div class='deleted-devices-table'>"
        f"<div class='deleted-device-header'>{header}</div>"
        f"{''.join(rendered_rows)}"
        "</div>"
        "</div>"
    )


def render_retained_devices_table(rows):
    if not rows:
        return f"<p>{_('text.no_retained_devices')}</p>"
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
            f"<td class='checkbox-col'><input type='checkbox' name='candidate' value='{index}' {checked}></td>"
            f"<td><code>{identifiers}</code></td>"
            f"<td>{name}</td>"
            f"<td>{manufacturer} | {model}</td>"
            f"<td><pre>{topics}</pre></td>"
            "</tr>"
        )
    return (
        "<div class='table-scroll'>"
        "<table class='retained-devices-table'>"
        "<colgroup><col class='checkbox-col'><col><col><col><col></colgroup>"
        f"<thead><tr><th class='checkbox-col' aria-label='{_('label.delete')}'></th>"
        f"<th>{_('label.identifiers')}</th><th>{_('label.name')}</th>"
        f"<th>{_('label.manufacturer_model')}</th><th>{_('label.retained_discovery_topics')}</th></tr></thead>"
        f"<tbody>{''.join(rendered_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def render_internal_ids_table(rows, render_diff):
    if not rows:
        return f"<p>{_('text.no_internal_id_migration_candidates')}</p>"
    rendered_rows = []
    for index, row in enumerate(rows):
        can_migrate = bool(row.get("changes"))
        checked = "checked" if row.get("selected", True) and can_migrate else ""
        migrate_control = (
            f"<input type='checkbox' name='candidate' value='{index}' {checked} onclick='event.stopPropagation()'>"
            if can_migrate
            else f"<span class='no-candidates' title='{_('title.no_safe_candidates')}'>{_('label.none')}</span>"
        )
        path = html.escape(str(row.get("path") or ""))
        diff = str(row.get("diff") or "")
        unresolved_items = row.get("unresolved_items") or []
        if diff:
            details_html = render_diff(diff)
        elif unresolved_items:
            rendered_unresolved = []
            for item in unresolved_items:
                reason = html.escape(str(item.get("reason") or _("text.unsupported")))
                alias = html.escape(str(item.get("alias") or ""))
                yaml_text = html.escape(str(item.get("yaml") or item.get("item") or ""))
                rendered_unresolved.append(
                    "<div class='unresolved-block'>"
                    f"<p><strong>{alias}</strong> - {reason}</p>"
                    f"<pre>{yaml_text}</pre>"
                    "</div>"
                )
            details_html = "".join(rendered_unresolved)
        else:
            details_html = f"<p>{_('text.no_internal_id_diff')}</p>"
        rendered_rows.append(
            "<details class='internal-id-row'>"
            "<summary>"
            "<span class='internal-id-summary'>"
            f"<span class='select-col'>{migrate_control}</span>"
            f"<span class='file-col'><code>{path}</code></span>"
            f"<span class='metric-col'>{html.escape(str(row.get('changes') or 0))}</span>"
            f"<span class='metric-col'>{html.escape(str(row.get('unresolved') or 0))}</span>"
            "</span>"
            "</summary>"
            f"<div class='internal-id-diff'>{details_html}</div>"
            "</details>"
        )
    return (
        "<div class='action-row'>"
        f"<button type='button' class='secondary' data-checkbox-scope='internal-ids' data-checkbox-action='all'>{_('button.select_all')}</button>"
        f"<button type='button' class='secondary' data-checkbox-scope='internal-ids' data-checkbox-action='none'>{_('button.select_none')}</button>"
        "</div>"
        "<div class='internal-ids-list' data-checkbox-scope='internal-ids'>"
        "<div class='internal-id-header'>"
        f"<span></span><span>{_('label.migrate')}</span><span>{_('label.file')}</span>"
        f"<span>{_('label.candidates')}</span><span>{_('label.unresolved')}</span>"
        "</div>"
        f"{''.join(rendered_rows)}"
        "</div>"
    )


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
        f"<td class='checkbox-col'>{checkbox}</td>"
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
    disabled=False,
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
            addon_error = f"<p>{html.escape(_('error.addon_discovery', error=str(exc)))}</p>"
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
            disabled_attr = " disabled" if disabled else ""
            checkbox = f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}{disabled_attr}>"
            hint = _("value.zigbee2mqtt_candidate") if addon_is_zigbee2mqtt(addon) else ""
            rows.append(render_target_row(item, checkbox, addon_display_name(addon), hint))

        for slug, item in sorted(addon_targets.items()):
            if slug in seen:
                continue
            checked = "checked" if slug in selected_addons else ""
            disabled_attr = " disabled" if disabled else ""
            checkbox = f"<input type='checkbox' name='addon' value='{html.escape(slug, quote=True)}' {checked}{disabled_attr}>"
            rows.append(render_target_row(item, checkbox, item.get("id")))
    else:
        for item in items:
            if item.get("type") != "addon":
                continue
            checkbox = "<input type='checkbox' checked disabled>"
            rows.append(render_target_row(item, checkbox))

    if not rows:
        return f"<p>{_('text.no_targets')}</p>"

    return (
        f"{addon_error}"
        "<form method='post' action='addons' data-auto-submit='change'>"
        "<table class='managed-targets-table'>"
        "<colgroup><col class='checkbox-col'><col><col><col><col><col></colgroup>"
        f"<thead><tr><th class='checkbox-col'><span class='sr-only'>{_('label.managed')}</span></th><th>{_('label.target')}</th>"
        f"<th>{_('label.type')}</th><th>{_('label.source')}</th><th>{_('label.addon')}</th>"
        f"<th>{_('label.live_path')}</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</form>"
    )


def render_releases(releases, disabled=False):
    intro = f"<p>{_('notice.release_snapshots')}</p>"
    if not releases:
        return f"{intro}<p>{_('text.no_local_release_snapshots')}</p>"

    rows = []
    for release in releases[:12]:
        name = html.escape(release["name"])
        created_at = html.escape(str(release.get("created_at")))
        backup_slug = html.escape(str(release.get("backup_slug")))
        disabled_attr = " disabled" if disabled else ""
        rows.append(
            "<tr>"
            f"<td><code>{name}</code></td>"
            f"<td>{created_at}</td>"
            f"<td><code>{backup_slug}</code></td>"
            "<td>"
            f"<form method='post' action='rollback' data-async-form='true'>"
            f"<input type='hidden' name='release' value='{name}'>"
            f"<button type='submit' class='secondary'{disabled_attr}>{_('action.rollback')}</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    return (
        f"{intro}"
        f"<table><thead><tr><th>{_('label.release')}</th><th>{_('label.created')}</th>"
        f"<th>{_('label.ha_backup')}</th><th>{_('table.action')}</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def targets_allow_protected_storage(items):
    return any(bool(item.get("allow_protected_storage")) for item in items or [])


def render_git_auth(options, git_auth_mode, load_generated_public_key, disabled=False):
    mode = git_auth_mode(options)
    public_key = html.escape(load_generated_public_key())
    repo_url = options.get("repo_url", "")
    uses_ssh = repo_url.startswith("git@") or repo_url.startswith("ssh://")

    if mode == "manual":
        status = f"<p>{_('notice.git_auth_manual')}</p>"
        key_block = ""
    elif mode == "generated":
        status = f"<p>{_('notice.git_auth_generated')}</p>"
        key_block = (
            f"<p>{_('notice.git_auth_generated_hint')}</p>"
            f"<pre>{public_key}</pre>"
        )
    else:
        status = f"<p>{_('notice.git_auth_no_key')}</p>"
        key_block = ""

    hint = ""
    if uses_ssh and mode == "none":
        hint = f"<p>{_('notice.git_auth_ssh_hint')}</p>"
    elif not uses_ssh:
        hint = f"<p>{_('notice.git_auth_non_ssh')}</p>"

    disabled_attr = " disabled" if disabled else ""
    action = (
        "<form method='post' action='generate-key' data-async-form='true'>"
        f"<button type='submit' class='secondary'{disabled_attr}>{_('action.generate_deploy_key')}</button>"
        "</form>"
    )
    if mode == "generated":
        action = (
            "<form method='post' action='generate-key' data-async-form='true'>"
            f"<button type='submit' class='secondary'{disabled_attr}>{_('action.regenerate_deploy_key')}</button>"
            "</form>"
        )

    return f"{status}{hint}<div class='actions'>{action}</div>{key_block}"


def render_page(data):
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_('title.site')}</title>
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
      --lumo-primary-color: var(--ha-primary);
      --lumo-primary-text-color: var(--ha-primary);
      --lumo-body-text-color: var(--ha-text);
      --lumo-secondary-text-color: var(--ha-muted);
      --lumo-base-color: var(--ha-card-bg);
      --ha-ops-surface: var(--ha-card-bg);
      --ha-ops-muted-text: #6b7280;
      --ha-ops-muted-border: #d1d5db;
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
      height: var(--details-card-height, 500px);
      min-height: 0;
      overflow: hidden;
    }}
    .details-card ha-ops-log {{
      flex: 1 1 auto;
      min-height: 0;
      display: block;
    }}
    .details-card pre {{
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      overflow-y: auto;
    }}
    .details-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 0.5rem;
    }}
    .details-header h2 {{
      margin: 0;
    }}
    h1, h2 {{
      margin: 0 0 14px;
      color: var(--ha-text);
    }}
    h1 {{
      font-size: 2rem;
    }}
    .title-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }}
    .title-row h1 {{
      margin-bottom: 0;
    }}
    .header-badges {{
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .header-badges .badge {{
      white-space: nowrap;
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
    .badge.pending, .badge.warning {{
      background: color-mix(in srgb, var(--ha-warning) 14%, transparent);
      color: var(--ha-warning);
      border-color: color-mix(in srgb, var(--ha-warning) 30%, transparent);
    }}
    .badge.transport {{
      background: color-mix(in srgb, var(--ha-warning) 14%, transparent);
      color: var(--ha-warning);
      border-color: color-mix(in srgb, var(--ha-warning) 30%, transparent);
    }}
    .badge.version {{
      background: color-mix(in srgb, var(--ha-muted) 10%, transparent);
      color: var(--ha-muted);
      border-color: color-mix(in srgb, var(--ha-muted) 28%, transparent);
    }}
    .actions {{
      display: flex;
      flex-direction: column;
      gap: 18px;
      margin-top: 22px;
    }}
    .action-section {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-top: 14px;
      border-top: 1px solid var(--ha-border);
    }}
    .action-section:first-child {{
      padding-top: 0;
      border-top: 0;
    }}
    .action-section h2 {{
      margin: 0;
      color: var(--ha-muted);
      font-size: 0.86rem;
      line-height: 1.2;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .action-row {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .action-hint {{
      margin: -2px 0 0;
      color: var(--ha-muted);
      font-size: 0.88rem;
      line-height: 1.4;
      max-width: 44rem;
    }}
    .docker-prune-hint {{
      color: #d80;
    }}
    .action-flow {{
      margin: -2px 0 0;
      color: var(--ha-muted);
      font-size: 0.88rem;
      line-height: 1.4;
    }}
    .post-apply-alert {{
      display: grid;
      gap: 4px;
      margin: 18px 0 0;
      padding: 12px 14px;
      border: 1px solid color-mix(in srgb, var(--ha-warning) 45%, transparent);
      border-left: 5px solid var(--ha-warning);
      border-radius: calc(var(--ha-radius) - 4px);
      background: color-mix(in srgb, var(--ha-warning) 16%, var(--ha-card-bg));
      color: var(--ha-text);
    }}
    .post-apply-alert strong {{
      color: var(--ha-text);
    }}
    .post-apply-alert span {{
      color: var(--ha-muted);
      line-height: 1.4;
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
    .actions .check-list {{
      margin-top: 0;
    }}
    .check-row {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 8px 12px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--ha-border);
    }}
    .actions .check-row {{
      padding: 2px 0;
      border-bottom: 0;
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
    button.secondary {{
      background: var(--ha-card-bg);
      color: var(--ha-text);
      border-color: var(--ha-border);
    }}
    button.warning {{
      background: var(--ha-warning);
      color: #111827;
      border-color: color-mix(in srgb, var(--ha-warning) 65%, #111827);
    }}
    button:disabled,
    button.secondary:disabled,
    button.warning:disabled,
    vaadin-button[disabled] {{
      background: #e5e7eb;
      color: #6b7280;
      border-color: #d1d5db;
      cursor: default;
      opacity: 1;
      --vaadin-button-background: #e5e7eb;
      --vaadin-button-text-color: #6b7280;
    }}
    vaadin-button[disabled]::part(label) {{ color: #6b7280; }}
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
    .sr-only {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
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
    .retained-devices-table .checkbox-col {{
      width: 42px;
      min-width: 42px;
      max-width: 42px;
      text-align: center;
    }}
    .deleted-devices-table {{
      min-width: 760px;
    }}
    .deleted-device-header,
    .deleted-device-row {{
      display: flex;
      align-items: stretch;
      border-bottom: 1px solid #d0d7de;
    }}
    .deleted-device-header {{
      background: #f6f8fa;
      font-weight: 700;
    }}
    .deleted-device-header-cell,
    .deleted-device-cell {{
      box-sizing: border-box;
      min-width: 0;
      padding: 8px;
      overflow-wrap: anywhere;
      word-break: normal;
    }}
    .deleted-device-col-id {{
      flex: 0 1 11ch;
    }}
    .deleted-device-col-source {{
      flex: 1 2 20ch;
    }}
    .deleted-device-col-identifiers {{
      flex: 1 2 22ch;
    }}
    .deleted-device-col-area,
    .deleted-device-col-original-device-class {{
      flex: 0 1 12ch;
    }}
    .deleted-device-col-name,
    .deleted-device-col-original-name {{
      flex: 2 1 20ch;
    }}
    .deleted-device-col-device {{
      flex: 2 1 24ch;
    }}
    .deleted-device-cell code {{
      display: block;
      overflow-wrap: anywhere;
      white-space: normal;
      word-break: normal;
    }}
    .managed-targets-table {{
      table-layout: auto;
    }}
    .managed-targets-table .checkbox-col {{
      width: 42px;
      min-width: 42px;
      max-width: 42px;
      text-align: center;
      white-space: nowrap;
    }}
    .internal-ids-list {{
      font-size: 0.94rem;
    }}
    .internal-id-header,
    .internal-id-row summary {{
      display: grid;
      grid-template-columns: 24px 82px minmax(0, 1fr) 96px 96px;
      gap: 10px;
      align-items: center;
    }}
    .internal-id-header {{
      padding: 12px 10px;
      color: var(--ha-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--ha-border);
    }}
    .internal-id-row summary {{
      cursor: pointer;
      padding: 12px 10px;
      border-bottom: 1px solid var(--ha-border);
    }}
    .internal-id-row summary::-webkit-details-marker {{
      display: none;
    }}
    .internal-id-row summary::marker {{
      content: "";
    }}
    .internal-id-row summary::before {{
      content: "";
      width: 0;
      height: 0;
      border-top: 5px solid transparent;
      border-bottom: 5px solid transparent;
      border-left: 7px solid var(--ha-text);
      justify-self: center;
      transition: transform 120ms ease;
    }}
    .internal-id-row[open] summary::before {{
      transform: rotate(90deg);
    }}
    .internal-id-row[open] summary {{
      border-bottom: 0;
    }}
    .internal-id-summary {{
      display: contents;
    }}
    .internal-id-summary .metric-col {{
      text-align: center;
    }}
    .no-candidates {{
      color: var(--ha-muted);
      font-size: 0.86rem;
    }}
    .internal-id-summary .file-col {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .internal-id-summary .file-col code {{
      display: block;
      overflow: hidden;
      overflow-wrap: normal;
      text-overflow: ellipsis;
      white-space: nowrap;
      word-break: normal;
    }}
    .internal-id-diff {{
      padding: 0 10px 14px 44px;
      border-bottom: 1px solid var(--ha-border);
    }}
    .unresolved-block + .unresolved-block {{
      margin-top: 14px;
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
      border-radius: 0;
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
    .unicode-escape {{
      position: relative;
      border-bottom: 1px dotted currentColor;
      cursor: help;
    }}
    .unicode-escape:hover::after,
    .unicode-escape:focus-visible::after {{
      content: attr(data-unicode-char);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 6px);
      transform: translateX(-50%);
      z-index: 2;
      min-width: 32px;
      padding: 6px 8px;
      border: 1px solid var(--ha-border);
      border-radius: 6px;
      background: var(--ha-card-bg);
      color: var(--ha-text);
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.16);
      font-family: var(--ha-font);
      font-size: 1.4rem;
      line-height: 1;
      text-align: center;
      white-space: nowrap;
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
    @media (max-width: 1347px) {{
      .top-grid {{
        grid-template-columns: minmax(0, 1fr);
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
  <ha-ops-app data-testid="ha-ops-app">
  <main>
    <div class="top-grid">
      <section class="card control-card">
        <div class="title-row">
          <h1>{_('title.site')}</h1>
          <div class="header-badges">
            <div class="badge {data['badge_class']}" data-status-code="{data['status_code']}" data-testid="status-badge">{data['status']}</div>
            <div class="badge version" data-testid="version-badge">{data['version']}</div>
          </div>
        </div>
        <p>{_('site.description')}</p>
        <dl>
          <dt>{_('field.repo_url')}</dt>
          <dd><code>{data['repo_url'] or _('placeholder.not_configured')}</code></dd>
          <dt>{_('field.branch')}</dt>
          <dd><code>{data['branch']}</code></dd>
          <dt>{_('field.manifest')}</dt>
          <dd><code>{data['manifest_path']}</code></dd>
          <dt>{_('field.auth_mode')}</dt>
          <dd><code>{data['auth_mode']}</code></dd>
          <dt>{_('field.last_run')}</dt>
          <dd>{data['last_run']}</dd>
          <dt>{_('field.release_snapshot')}</dt>
          <dd><code>{data['last_release']}</code></dd>
          <dt>{_('field.ha_backup')}</dt>
          <dd><code>{data['last_backup_slug']}</code></dd>
          <dt>{_('field.latest_backup')}</dt>
          <dd>{data['latest_backup']}</dd>
        </dl>
        <p id="client-status" class="client-status"></p>
        {data['post_apply_notice_html']}
        {data['organizer_html']}
        <div class="actions">
          <section class="action-section">
            <h2>{_('heading.ha_to_git')}</h2>
            <div class="action-row">
              <form method="post" action="save-preview" data-async-form="true">
                <button type="submit" class="{data['save_preview_button_class']}" {data['action_disabled']}>{data['save_preview_button_text']}</button>
              </form>
            </div>
            {data['save_preview_hint_html']}
            <div class="action-row">
              {data['include_redundant_data_html']}
            </div>
          </section>
          <section class="action-section">
            <h2>{_('heading.git_to_ha')}</h2>
            <div class="action-row">
              <form method="post" action="preview" data-async-form="true">
                <button type="submit" class="secondary" {data['action_disabled']}>{_('action.preview_apply')}</button>
              </form>
            </div>
          </section>
          <section class="action-section">
            <h2>{_('heading.reset_git_state')}</h2>
            <div class="action-row">
              <form method="post" action="reset-git-state" data-async-form="true" data-confirm="{_('confirm.reset_git_state')}">
                <button type="submit" class="secondary" {data['action_disabled']}>{_('action.reset_git_state')}</button>
              </form>
            </div>
            <p class="action-flow">{_('text.reset_git_state')}</p>
          </section>
          <section class="action-section">
            <h2>{_('heading.disk_usage')}</h2>
            <div class="action-row">
              <form method="post" action="disk-usage" data-async-form="true">
                <button type="submit" class="secondary" {data['check_disk_usage_disabled']}>{_('action.check_disk_usage')}</button>
              </form>
              <form method="post" action="docker-build-cache-prune" data-confirm="{_('confirm.docker_build_cache_prune')}" data-async-form="false" data-capability-available="{data['docker_prune_available']}" data-action-ready="{data['docker_prune_ready']}">
                <button type="submit" class="secondary" {data['docker_prune_disabled']}>{_('action.clear_docker_build_cache')}</button>
              </form>
            </div>
            {data['docker_prune_hint_html']}
            <p class="action-flow">{_('text.disk_usage_flow')}</p>
            {data['docker_prune_status_html']}
          </section>
          <section class="action-section">
            <h2>{_('heading.deleted_devices')}</h2>
            <div class="action-row">
              <form method="post" action="deleted-devices-preview" data-async-form="true">
                <button type="submit" class="secondary" {data['check_deleted_devices_disabled']}>{_('action.check_deleted_devices')}</button>
              </form>
            </div>
            {data['deleted_devices_save_hint_html']}
            <p class="action-flow">{_('text.deleted_devices_flow')}</p>
          </section>
          <section class="action-section">
            <h2>{_('heading.retained_devices')}</h2>
            <div class="action-row">
              <form method="post" action="retained-devices-preview" data-async-form="true">
                <button type="submit" class="secondary" {data['check_retained_devices_disabled']}>{_('action.check_retained_devices')}</button>
              </form>
            </div>
            <p class="action-flow">{_('notice.retained_devices_flow')}</p>
          </section>
          <section class="action-section">
            <h2>{_('heading.actions_ids')}</h2>
            <div class="action-row">
              <form method="post" action="internal-ids-preview" data-async-form="true">
                <button type="submit" class="secondary" {data['check_internal_ids_disabled']}>{_('action.check_actions_ids')}</button>
              </form>
            </div>
            <p class="action-flow">{_('notice.internal_ids_flow')}</p>
          </section>
        </div>
      </section>
      <section class="card details-card">
        <div class="details-header">
          <h2>{_('heading.log')}</h2>
        </div>
        <ha-ops-log>{data['details_html']}</ha-ops-log>
      </section>
    </div>

    <div>{data['deleted_devices_section_html']}</div>

    <div>{data['retained_devices_section_html']}</div>

    <div>{data['internal_ids_section_html']}</div>

    <div>{data['conflicts_section_html']}</div>

    <section class="card wide">
      <h2>{_('heading.git_access')}</h2>
      {data['git_auth_html']}
    </section>

    <section class="card wide">
      <h2>{_('heading.managed_targets')}</h2>
      {data['targets_html']}
    </section>

    <section class="card wide">
      <h2>{_('heading.release_snapshots')}</h2>
      {data['releases_html']}
    </section>
  </main>
  </ha-ops-app>
  <script>
    window.__HA_OPS_REACTIVE_UI__ = true;
    window.__HA_OPS_TEXT__ = {{
      expand: {js_t('button.expand_diff')},
      collapse: {js_t('button.collapse_diff')},
      expandAll: {js_t('button.expand_all')},
      collapseAll: {js_t('button.collapse_all')},
      applyPreview: {js_t('heading.git_to_ha')},
      savePreview: {js_t('heading.ha_to_git')},
      apply: {js_t('action.apply')},
      save: {js_t('action.save')},
      loadingDiff: {js_t('message.loading_diff')},
      unavailableDiff: {js_t('text.diff_detail_unavailable')},
      confirm: {js_t('action.confirm')}
    }};
  </script>
  <script type="module" src="assets/ha-ops.js"></script>
</body>
</html>"""
    return page
