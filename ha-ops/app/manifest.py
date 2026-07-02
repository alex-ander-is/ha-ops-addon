from pathlib import Path

import policies


HOMEASSISTANT_ORGANIZER_STATE_KEY = "homeassistant_organizer_enabled"
ORGANIZER_PROJECTION_AVAILABLE = False


def selected_addon_slugs(read_state):
    state = read_state()
    return sorted(str(slug) for slug in state.get("managed_addons", []) if slug)


def set_selected_addon_slugs(slugs, write_state):
    cleaned = sorted(set(str(slug) for slug in slugs if slug))
    write_state({"managed_addons": cleaned})
    return cleaned


def homeassistant_organizer_preference(read_state):
    value = read_state().get(HOMEASSISTANT_ORGANIZER_STATE_KEY)
    return value if isinstance(value, bool) else None


def set_homeassistant_organizer_enabled(enabled, write_state):
    value = bool(enabled)
    if value and not ORGANIZER_PROJECTION_AVAILABLE:
        raise RuntimeError("Home Assistant organizer area split is paused while the projection rewrite is pending.")
    write_state({HOMEASSISTANT_ORGANIZER_STATE_KEY: value})
    return value


def organizer_target_enabled(target):
    value = target.get("organizer")
    if value is True:
        return True
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return False


def with_homeassistant_organizer_preference(target, organizer_enabled):
    if target.get("type") != "homeassistant" or organizer_enabled is None:
        return target

    updated = dict(target)
    if not organizer_enabled:
        updated["organizer"] = False
        return updated

    value = updated.get("organizer")
    if isinstance(value, dict):
        organizer = dict(value)
        organizer["enabled"] = True
    else:
        organizer = {"enabled": True}
    updated["organizer"] = organizer
    return updated


def apply_homeassistant_organizer_preference(manifest, organizer_enabled):
    if organizer_enabled is None:
        return manifest
    if organizer_enabled and not ORGANIZER_PROJECTION_AVAILABLE:
        return manifest
    effective = dict(manifest)
    effective["targets"] = [
        with_homeassistant_organizer_preference(target, organizer_enabled)
        for target in manifest.get("targets", [])
    ]
    return effective


def default_homeassistant_manifest(options, organizer_enabled=None):
    target = {
        "id": "homeassistant",
        "type": "homeassistant",
        "source": options.get("apply_path", "homeassistant"),
        "delete": False,
        "allow_protected_storage": False,
    }
    target.update(policies.default_homeassistant_lifecycle_policy(options))
    target = with_homeassistant_organizer_preference(target, organizer_enabled)
    return {
        "version": 1,
        "targets": [target],
    }


def default_addon_target(slug):
    return {
        "id": f"addon-{slug}",
        "type": "addon",
        "source": f"addons/{slug}",
        "addon_slug": slug,
        "delete": False,
        "restore_delete": True,
        "restart_after_sync": True,
        "optional": True,
    }


def addon_target_slug(target, addons=None):
    exact = target.get("addon_slug")
    if exact:
        return exact
    if addons is None:
        return None
    try:
        return resolve_addon_slug(target, addons)
    except RuntimeError:
        return None


def selected_addon_target(slug, template=None):
    target = dict(template or default_addon_target(slug))
    target["type"] = "addon"
    target["addon_slug"] = slug
    target.pop("addon_slug_suffix", None)
    target.pop("addon_name_contains", None)
    target.setdefault("id", f"addon-{slug}")
    target.setdefault("source", f"addons/{slug}")
    target.setdefault("delete", False)
    target.setdefault("restore_delete", True)
    target.setdefault("restart_after_sync", True)
    target.setdefault("optional", True)
    return target


def manifest_with_selected_addons(manifest, selected, addons=None, organizer_enabled=None):
    targets = []
    addon_templates = {}

    for target in manifest.get("targets", []):
        if target.get("type") != "addon":
            targets.append(target)
            continue
        slug = addon_target_slug(target, addons)
        if slug:
            addon_templates[slug] = target

    for slug in selected:
        targets.append(selected_addon_target(slug, addon_templates.get(slug)))

    effective = dict(manifest)
    effective["targets"] = targets
    return apply_homeassistant_organizer_preference(effective, organizer_enabled)


def default_manifest(options, selected, organizer_enabled=None):
    return manifest_with_selected_addons(
        default_homeassistant_manifest(options),
        selected,
        organizer_enabled=organizer_enabled,
    )


def load_manifest(repo_dir, options, selected, load_json, addons=None, organizer_enabled=None):
    manifest_path = repo_dir / options.get("manifest_path", "ha-ops.json")
    if not manifest_path.exists():
        return (
            manifest_with_selected_addons(
                default_homeassistant_manifest(options),
                selected,
                addons,
                organizer_enabled,
            ),
            manifest_path,
        )

    return (
        manifest_with_selected_addons(
            load_json(manifest_path, {}),
            selected,
            addons,
            organizer_enabled,
        ),
        manifest_path,
    )


def resolve_addon_slug(target, addons):
    exact = target.get("addon_slug")
    if exact:
        for addon in addons:
            if addon.get("slug") == exact:
                return exact
        if target.get("optional"):
            return None
        raise RuntimeError(f"Configured App slug was not found: {exact}")

    suffix = target.get("addon_slug_suffix")
    if suffix:
        matches = [addon for addon in addons if addon.get("slug", "").endswith(suffix)]
        if len(matches) == 1:
            return matches[0]["slug"]
        if not matches and target.get("optional"):
            return None
        raise RuntimeError(f"Expected one App slug ending with '{suffix}', found {len(matches)}")

    name_contains = target.get("addon_name_contains")
    if name_contains:
        matches = [
            addon
            for addon in addons
            if name_contains.lower() in addon.get("name", "").lower()
        ]
        if len(matches) == 1:
            return matches[0]["slug"]
        if not matches and target.get("optional"):
            return None
        raise RuntimeError(f"Expected one App name containing '{name_contains}', found {len(matches)}")

    raise RuntimeError(f"App target '{target.get('id')}' is missing resolver fields")


def addon_by_slug(addons, slug):
    for addon in addons:
        if addon.get("slug") == slug:
            return addon
    return {}


def path_from_metadata(value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute():
        return path
    return None


def path_is_within(path, root):
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        resolved_path = path
        resolved_root = root
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def validate_addon_live_path(path, allowed_roots, target_id):
    if any(path_is_within(path, root) for root in allowed_roots):
        return path
    roots = ", ".join(str(root) for root in allowed_roots)
    raise RuntimeError(f"App live path escapes allowed config roots for target '{target_id}': {path}. Allowed roots: {roots}")


def addon_config_path_candidates(target, slug, addon, addon_configs_dir, config_dir, addon_is_zigbee2mqtt):
    candidates = []
    target_id = str(target.get("id") or slug)
    allowed_roots = [addon_configs_dir / slug]
    if addon_is_zigbee2mqtt(addon or {"slug": slug}):
        allowed_roots.extend([config_dir / "zigbee2mqtt", Path("/share/zigbee2mqtt")])

    for source in (target, addon):
        for key in ("live_path", "config_path", "configuration_path", "addon_config_path", "data_path"):
            path = path_from_metadata(source.get(key))
            if path:
                candidates.append(validate_addon_live_path(path, allowed_roots, target_id))

    candidates.append(addon_configs_dir / slug)

    if addon_is_zigbee2mqtt(addon or {"slug": slug}):
        candidates.append(config_dir / "zigbee2mqtt")
        candidates.append(Path("/share/zigbee2mqtt"))

    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def resolve_addon_live_path(target, slug, addons, addon_configs_dir, config_dir, addon_is_zigbee2mqtt):
    addon = addon_by_slug(addons, slug)
    candidates = addon_config_path_candidates(target, slug, addon, addon_configs_dir, config_dir, addon_is_zigbee2mqtt)
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def resolve_targets(
    repo_dir,
    manifest,
    addons,
    options,
    config_dir,
    addon_configs_dir,
    addon_is_zigbee2mqtt,
    require_source=True,
):
    targets = []
    for target in manifest.get("targets", []):
        target_id = str(target.get("id") or "")
        validate_target_id(target_id)
        target_type = target.get("type")
        source = repo_source_path(repo_dir, target.get("source", ""), target_id)
        optional = bool(target.get("optional", False))

        if require_source and not source.exists():
            if optional:
                continue
            raise RuntimeError(f"Source path does not exist for target '{target.get('id')}': {source}")
        resolved = dict(target)
        resolved["source_path"] = str(source)
        resolved["restart_after_sync"] = policies.restart_after_sync(target, options)
        if target_type == "homeassistant":
            resolved.update(policies.homeassistant_lifecycle_policy(target, options))

        if target_type == "homeassistant":
            resolved["resolved_slug"] = None
            resolved["live_path"] = str(config_dir)
        elif target_type == "addon":
            slug = resolve_addon_slug(target, addons)
            if slug is None:
                continue
            resolved["resolved_slug"] = slug
            resolved["live_path"] = str(
                resolve_addon_live_path(target, slug, addons, addon_configs_dir, config_dir, addon_is_zigbee2mqtt)
            )
        else:
            raise RuntimeError(f"Unsupported target type: {target_type}")

        targets.append(resolved)

    return targets


def validate_target_id(target_id):
    if not target_id or Path(target_id).name != target_id:
        raise RuntimeError(f"Invalid target id: {target_id}")


def repo_source_path(repo_dir, source, target_id):
    source_value = source or ""
    source_path = (repo_dir / source_value).resolve()
    repo_root = repo_dir.resolve()
    if source_path != repo_root and repo_root not in source_path.parents:
        raise RuntimeError(f"Source path escapes repository for target '{target_id}': {source_value}")
    return source_path
