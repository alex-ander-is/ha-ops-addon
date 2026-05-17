import hashlib
import json
import os
from pathlib import Path


def device_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.device_registry"


def entity_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.entity_registry"


def area_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.area_registry"


def fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_device_registry(config_dir):
    path = device_registry_path(config_dir)
    if not path.exists():
        raise RuntimeError(f"Home Assistant device registry not found: {path}")
    text = path.read_text(encoding="utf-8")
    return path, text, json.loads(text)


def read_optional_registry(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def rollback_dir(work_dir):
    return Path(work_dir) / "deleted-devices-rollback"


def rollback_path(work_dir):
    return rollback_dir(work_dir) / "core.device_registry"


def create_deleted_devices_rollback(config_dir, work_dir, expected_fingerprint):
    path, text, _data = read_device_registry(config_dir)
    current_fingerprint = fingerprint_text(text)
    if expected_fingerprint and current_fingerprint != expected_fingerprint:
        raise RuntimeError("Device registry changed since preview. Run Check deleted_devices again.")
    dest = rollback_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return {"path": str(dest), "fingerprint": current_fingerprint}


def restore_deleted_devices_rollback(config_dir, rollback_file):
    source = Path(rollback_file)
    if not source.exists():
        raise RuntimeError("deleted_devices rollback snapshot is missing.")
    rollback_data = json.loads(source.read_text(encoding="utf-8"))
    restored_devices = deleted_devices(rollback_data)

    dest, _text, current_data = read_device_registry(config_dir)
    current_devices = deleted_devices(current_data)
    merged_devices = merge_deleted_devices(current_devices, restored_devices)
    current_data.setdefault("data", {})["deleted_devices"] = merged_devices

    tmp_path = dest.with_name(f".{dest.name}.tmp")
    tmp_path.write_text(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, dest)
    text = dest.read_text(encoding="utf-8")
    return {
        "fingerprint": fingerprint_text(text),
        "restored": len(restored_devices),
        "merged": len(merged_devices),
        "preserved": len(current_devices),
    }


def discard_deleted_devices_rollback(rollback_file):
    path = Path(rollback_file)
    if path.exists():
        path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass


def deleted_devices(data):
    return data.get("data", {}).get("deleted_devices", [])


def deleted_device_key(device):
    if isinstance(device, dict) and device.get("id"):
        return ("id", str(device["id"]))
    return ("json", json.dumps(device, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def merge_deleted_devices(current_devices, restored_devices):
    merged = list(current_devices)
    seen = {deleted_device_key(device) for device in merged}
    for device in restored_devices:
        key = deleted_device_key(device)
        if key in seen:
            continue
        merged.append(device)
        seen.add(key)
    return merged


def deleted_devices_cleanup_status(config_dir, rollback_file):
    source = Path(rollback_file)
    if not source.exists():
        raise RuntimeError("deleted_devices rollback snapshot is missing.")
    rollback_data = json.loads(source.read_text(encoding="utf-8"))
    removed_devices = deleted_devices(rollback_data)
    removed_keys = {deleted_device_key(device) for device in removed_devices}
    _path, text, current_data = read_device_registry(config_dir)
    current_devices = deleted_devices(current_data)
    returned = [device for device in current_devices if deleted_device_key(device) in removed_keys]
    added = [device for device in current_devices if deleted_device_key(device) not in removed_keys]
    return {
        "fingerprint": fingerprint_text(text),
        "removed": len(removed_devices),
        "current": len(current_devices),
        "returned": len(returned),
        "added": len(added),
    }


def deleted_device_label(device):
    name = device.get("name_by_user") or device.get("name")
    model = device.get("model") or device.get("model_id")
    manufacturer = device.get("manufacturer")
    pieces = []
    if name:
        pieces.append(str(name))
    if manufacturer or model:
        pieces.append(" ".join(str(item) for item in (manufacturer, model) if item))
    if device.get("id"):
        pieces.append(f"id={device['id']}")
    identifiers = device.get("identifiers") or []
    if identifiers:
        rendered = []
        for identifier in identifiers[:3]:
            if isinstance(identifier, list):
                rendered.append(":".join(str(item) for item in identifier))
            else:
                rendered.append(str(identifier))
        pieces.append(f"identifiers={', '.join(rendered)}")
    return " | ".join(pieces) or json.dumps(device, ensure_ascii=False, sort_keys=True)


def area_names(config_dir):
    data = read_optional_registry(area_registry_path(config_dir))
    areas = data.get("data", {}).get("areas", [])
    return {area.get("id"): area.get("name") for area in areas if area.get("id")}


def entities_by_device(config_dir):
    data = read_optional_registry(entity_registry_path(config_dir))
    entities = data.get("data", {}).get("entities", []) + data.get("data", {}).get("deleted_entities", [])
    grouped = {}
    for entity in entities:
        device_id = entity.get("device_id")
        if not device_id:
            continue
        grouped.setdefault(device_id, []).append(entity)
    return grouped


def deleted_device_rows(config_dir, devices):
    areas = area_names(config_dir)
    entities = entities_by_device(config_dir)
    rows = []
    for device in devices:
        device_id = device.get("id") or ""
        device_area = areas.get(device.get("area_id")) or device.get("area_id") or ""
        related_entities = entities.get(device_id) or [None]
        for entity in related_entities:
            entity = entity or {}
            area_id = entity.get("area_id") or device.get("area_id")
            rows.append(
                {
                    "area": areas.get(area_id) or area_id or device_area,
                    "entity_id": entity.get("entity_id") or "",
                    "original_name": entity.get("original_name") or device.get("name") or device.get("name_by_user") or "",
                    "original_device_class": entity.get("original_device_class") or "",
                    "id": device_id,
                }
            )
    return rows


def build_deleted_devices_preview(config_dir):
    _path, text, data = read_device_registry(config_dir)
    devices = deleted_devices(data)
    lines = [f"deleted_devices entries to remove ({len(devices)}):"]
    if devices:
        lines.extend(f"- {deleted_device_label(device)}" for device in devices)
    else:
        lines.append("No deleted_devices entries found.")
    return {
        "count": len(devices),
        "fingerprint": fingerprint_text(text),
        "summary": "\n".join(lines),
        "rows": deleted_device_rows(config_dir, devices),
    }


def device_registry_fingerprint(config_dir):
    _path, text, _data = read_device_registry(config_dir)
    return fingerprint_text(text)


def clear_deleted_devices(config_dir, expected_fingerprint):
    path, text, data = read_device_registry(config_dir)
    current_fingerprint = fingerprint_text(text)
    if expected_fingerprint and current_fingerprint != expected_fingerprint:
        raise RuntimeError("Device registry changed since preview. Run Check deleted_devices again.")

    devices = deleted_devices(data)
    removed = len(devices)
    data.setdefault("data", {})["deleted_devices"] = []

    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return {"removed": removed, "fingerprint": fingerprint_text(path.read_text(encoding="utf-8"))}
