import hashlib
import json
import os
import shutil
from pathlib import Path


def device_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.device_registry"


def fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_device_registry(config_dir):
    path = device_registry_path(config_dir)
    if not path.exists():
        raise RuntimeError(f"Home Assistant device registry not found: {path}")
    text = path.read_text(encoding="utf-8")
    return path, text, json.loads(text)


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
    dest = device_registry_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(source, tmp_path)
    os.replace(tmp_path, dest)
    text = dest.read_text(encoding="utf-8")
    return {"fingerprint": fingerprint_text(text)}


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
