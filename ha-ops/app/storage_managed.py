import copy
import json
from pathlib import Path


MANAGED_DIR = ".storage_managed"
CORE_CONFIG_ENTRIES_PROJECTION = "core.config_entries.json"
CORE_CONFIG_ENTRIES_RAW = "core.config_entries"

SAFE_DOMAIN_FIELDS = {
    "season": {"data": {"type"}, "options": set()},
    "systemmonitor": {"data": set(), "options": set()},
    "template": {"data": set(), "options": {"name", "state", "template_type"}},
    "time_date": {"data": set(), "options": {"display_options"}},
    "workday": {
        "data": set(),
        "options": {
            "add_holidays",
            "country",
            "days_offset",
            "excludes",
            "language",
            "name",
            "remove_holidays",
            "workdays",
        },
    },
}


def load_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def pick_fields(values, names):
    values = values or {}
    return {name: copy.deepcopy(values[name]) for name in sorted(names) if name in values}


def entry_identity(entry):
    return {
        "domain": entry.get("domain"),
        "entry_id": entry.get("entry_id"),
        "source": entry.get("source"),
        "title": entry.get("title"),
        "unique_id": entry.get("unique_id"),
    }


def project_entry(entry):
    domain = entry.get("domain")
    fields = SAFE_DOMAIN_FIELDS.get(domain, {"data": set(), "options": set()})
    apply_mode = "update" if domain in SAFE_DOMAIN_FIELDS else "ignore"
    projected = {
        **entry_identity(entry),
        "apply": apply_mode,
        "data": pick_fields(entry.get("data"), fields["data"]),
        "options": pick_fields(entry.get("options"), fields["options"]),
    }
    return projected


def project_core_config_entries(raw):
    entries = raw.get("data", {}).get("entries", [])
    return {
        "version": 1,
        "source": CORE_CONFIG_ENTRIES_RAW,
        "entries": [project_entry(entry) for entry in entries],
    }


def export_core_config_entries_projection(src_config_dir, dest_config_dir):
    raw_path = Path(src_config_dir) / ".storage" / CORE_CONFIG_ENTRIES_RAW
    if not raw_path.exists():
        return 0

    projection = project_core_config_entries(load_json(raw_path))
    write_json(Path(dest_config_dir) / MANAGED_DIR / CORE_CONFIG_ENTRIES_PROJECTION, projection)
    return 1


def find_entry(entries, projected):
    entry_id = projected.get("entry_id")
    if entry_id:
        for entry in entries:
            if entry.get("entry_id") == entry_id:
                return entry

    domain = projected.get("domain")
    unique_id = projected.get("unique_id")
    if domain and unique_id is not None:
        for entry in entries:
            if entry.get("domain") == domain and entry.get("unique_id") == unique_id:
                return entry
    return None


def merge_fields(target, section, values, allowed):
    if not values:
        return False
    target_section = target.setdefault(section, {})
    changed = False
    for key in sorted(allowed):
        if key not in values:
            continue
        value = copy.deepcopy(values[key])
        if target_section.get(key) != value:
            target_section[key] = value
            changed = True
    return changed


def apply_core_config_entries_projection(src_config_dir, dest_config_dir):
    projection_path = Path(src_config_dir) / MANAGED_DIR / CORE_CONFIG_ENTRIES_PROJECTION
    raw_path = Path(dest_config_dir) / ".storage" / CORE_CONFIG_ENTRIES_RAW
    if not projection_path.exists():
        return {"updated": 0, "skipped": 0}
    if not raw_path.exists():
        raise RuntimeError("Cannot apply managed core.config_entries: live .storage/core.config_entries is missing.")

    projection = load_json(projection_path)
    raw = load_json(raw_path)
    entries = raw.setdefault("data", {}).setdefault("entries", [])
    updated = 0
    skipped = 0

    for projected in projection.get("entries", []):
        domain = projected.get("domain")
        fields = SAFE_DOMAIN_FIELDS.get(domain)
        if projected.get("apply", "ignore") == "ignore" or fields is None:
            skipped += 1
            continue

        entry = find_entry(entries, projected)
        if entry is None:
            skipped += 1
            continue

        changed = False
        changed |= merge_fields(entry, "data", projected.get("data"), fields["data"])
        changed |= merge_fields(entry, "options", projected.get("options"), fields["options"])
        if changed:
            updated += 1

    if updated:
        write_json(raw_path, raw)
    return {"updated": updated, "skipped": skipped}


def source_has_managed_projection(path):
    return (Path(path) / MANAGED_DIR / CORE_CONFIG_ENTRIES_PROJECTION).exists()
