"""Explain normalized Home Assistant registry patches without changing their contents."""

import json
from collections import Counter


REGISTRY_NAMES = {"core.device_registry", "core.entity_registry"}
REVIEW_FIELDS = {"id", "entity_id", "unique_id", "device_id", "config_entry_id", "platform", "area_id"}


def _changed_records(diff, registry_name):
    before, after = [], []
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ", "diff ", "@@", "\\ No newline")):
            continue
        if not line.startswith(("-", "+")):
            continue
        try:
            record = json.loads(line[1:].strip().rstrip(","))
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            return None
        if registry_name == "core.entity_registry" and not isinstance(record.get("entity_id"), str):
            return None
        if registry_name == "core.device_registry" and not any(
            key in record for key in ("identifiers", "connections", "name", "name_by_user")
        ):
            return None
        (before if line[0] == "-" else after).append(record)
    return before, after


def _stable_device_keys(record):
    keys = set()
    for field in ("identifiers", "connections"):
        for value in record.get(field) or []:
            if isinstance(value, (list, tuple)) and len(value) == 2:
                keys.add((field, str(value[0]), str(value[1])))
    return keys


def _pair(before, after, registry_name):
    pairs = []
    old = {record["id"]: record for record in before}
    new = {record["id"]: record for record in after}
    for key in sorted(old.keys() & new.keys()):
        pairs.append((old.pop(key), new.pop(key)))

    # A device can acquire a new internal ID while keeping a unique hardware
    # identifier. Pair only unambiguous matches; never guess from its name.
    if registry_name == "core.device_registry":
        for old_id, old_record in list(old.items()):
            candidates = [new_id for new_id, new_record in new.items()
                          if _stable_device_keys(old_record) & _stable_device_keys(new_record)]
            if len(candidates) != 1:
                continue
            new_id = candidates[0]
            reverse = [candidate_id for candidate_id, candidate in old.items()
                       if _stable_device_keys(candidate) & _stable_device_keys(new[new_id])]
            if len(reverse) == 1:
                pairs.append((old.pop(old_id), new.pop(new_id)))
    return pairs, list(old.values()), list(new.values())


def _label(record, registry_name):
    if registry_name == "core.entity_registry":
        return record["entity_id"]
    return record.get("name_by_user") or record.get("name") or record.get("model") or record["id"]


def summarize_registry_diff(diff, path):
    """Return a complete semantic view, or None when the patch is ambiguous."""
    registry_name = path.rsplit("/", 1)[-1]
    if registry_name not in REGISTRY_NAMES:
        return None
    changed = _changed_records(diff, registry_name)
    if changed is None:
        return None
    before, after = changed
    if not before and not after:
        return None
    if any(count > 1 for count in Counter(item["id"] for item in before).values()):
        return None
    if any(count > 1 for count in Counter(item["id"] for item in after).values()):
        return None
    paired, removed, added = _pair(before, after, registry_name)
    rows = []
    for old, new in paired:
        fields = sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key))
        if fields:
            rows.append({"kind": "changed", "label": _label(new, registry_name), "fields": fields,
                         "old_label": _label(old, registry_name) if _label(old, registry_name) != _label(new, registry_name) else None,
                         "review": bool(REVIEW_FIELDS.intersection(fields))})
    rows.extend({"kind": "removed", "label": _label(item, registry_name), "fields": [], "review": True} for item in removed)
    rows.extend({"kind": "added", "label": _label(item, registry_name), "fields": [], "review": False} for item in added)
    rows.sort(key=lambda row: (row["kind"], row["label"]))
    return {"counts": dict(Counter(row["kind"] for row in rows)), "rows": rows} if rows else None
