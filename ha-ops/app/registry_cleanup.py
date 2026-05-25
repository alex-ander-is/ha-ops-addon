import hashlib
import difflib
import json
import os
import re
import subprocess
from pathlib import Path


def device_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.device_registry"


def entity_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.entity_registry"


def area_registry_path(config_dir):
    return Path(config_dir) / ".storage" / "core.area_registry"


ZIGBEE_IEEE_RE = re.compile(r"0x[0-9a-fA-F]{16}")
ZIGBEE2MQTT_PATHS = (
    "zigbee2mqtt/database.db",
    "zigbee2mqtt/configuration.yaml",
    "zigbee2mqtt/state.json",
)
MQTT_DISCOVERY_CONFIG_RE = re.compile(r"^homeassistant/[^/]+/([^/]+)/[^/]+/config$")


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


def zigbee2mqtt_ieees(config_dir):
    root = Path(config_dir)
    values = set()
    scanned = []
    for relative in ZIGBEE2MQTT_PATHS:
        path = root / relative
        if not path.exists() or not path.is_file():
            continue
        scanned.append(relative)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        values.update(match.lower() for match in ZIGBEE_IEEE_RE.findall(text))
    return values, scanned


def mqtt_zigbee2mqtt_identifier(device):
    for identifier in device.get("identifiers") or []:
        if not isinstance(identifier, list) or len(identifier) < 2:
            continue
        domain, value = identifier[0], identifier[1]
        if domain != "mqtt" or not isinstance(value, str):
            continue
        prefix = "zigbee2mqtt_"
        if value.startswith(prefix):
            ieee = value[len(prefix) :].lower()
            if ZIGBEE_IEEE_RE.fullmatch(ieee):
                return ieee
    return None


def retained_discovery_topic_ieees(topics):
    by_ieee = {}
    for topic in topics or []:
        if not isinstance(topic, str):
            continue
        match = MQTT_DISCOVERY_CONFIG_RE.match(topic)
        if not match:
            continue
        object_id = match.group(1).lower()
        if ZIGBEE_IEEE_RE.fullmatch(object_id):
            by_ieee.setdefault(object_id, []).append(topic)
    return by_ieee


def stale_mqtt_discovery_candidates(config_dir, retained_topics=None):
    _path, text, data = read_device_registry(config_dir)
    known_ieees, scanned_paths = zigbee2mqtt_ieees(config_dir)
    retained_by_ieee = retained_discovery_topic_ieees(retained_topics)
    candidates = []
    for device in data.get("data", {}).get("devices", []):
        ieee = mqtt_zigbee2mqtt_identifier(device)
        if not ieee or ieee in known_ieees:
            continue
        topics = sorted(retained_by_ieee.get(ieee, []))
        candidates.append(
            {
                "id": device.get("id") or "",
                "ieee": ieee,
                "identifiers": ["mqtt", f"zigbee2mqtt_{ieee}"],
                "name": device.get("name_by_user") or device.get("name") or "",
                "manufacturer": device.get("manufacturer") or "",
                "model": device.get("model") or device.get("model_id") or "",
                "retained_topics": topics,
                "reason": "Device exists in Home Assistant MQTT registry but is missing from current Zigbee2MQTT files.",
            }
        )
    candidates.sort(key=lambda item: (item["name"], item["ieee"], item["id"]))
    return {
        "count": len(candidates),
        "fingerprint": fingerprint_text(text),
        "scanned_paths": scanned_paths,
        "candidates": candidates,
    }


def build_stale_mqtt_discovery_preview(config_dir, retained_topics=None):
    preview = stale_mqtt_discovery_candidates(config_dir, retained_topics)
    lines = [f"stale MQTT discovery candidates ({preview['count']}):"]
    lines.append(
        "These are retained Home Assistant MQTT discovery topics for Zigbee2MQTT devices that are no longer present in current Zigbee2MQTT files."
    )
    lines.append("Deleting retained devices clears MQTT retained discovery topics only; it does not delete files or registry/database records.")
    if preview["scanned_paths"]:
        lines.append(f"Scanned Zigbee2MQTT files: {', '.join(preview['scanned_paths'])}.")
    else:
        lines.append("No Zigbee2MQTT files were found; review candidates carefully.")
    if not preview["candidates"]:
        lines.append("No stale MQTT discovery candidates found.")
    for item in preview["candidates"]:
        label = " | ".join(part for part in [item["name"], item["manufacturer"], item["model"], item["ieee"]] if part)
        lines.append(f"- {label}")
        for topic in item["retained_topics"]:
            lines.append(f"  retained: {topic}")
    return {**preview, "summary": "\n".join(lines)}


def clear_stale_mqtt_discovery_topics(topics, publish_empty_retained):
    cleared = []
    for topic in sorted(set(topics or [])):
        publish_empty_retained(topic)
        cleared.append(topic)
    return cleared


def mosquitto_password_command():
    return "P=$(python3 -c 'import json; print(json.load(open(\"/data/system_user.json\"))[\"homeassistant\"][\"password\"])'); "


def list_retained_discovery_topics(run_command, timeout_seconds=8):
    command = [
        "sh",
        "-c",
        mosquitto_password_command()
        + f"timeout {int(timeout_seconds)} mosquitto_sub -h addon_core_mosquitto -u homeassistant -P \"$P\" "
        + "-t 'homeassistant/#' -v",
    ]
    result = run_command(command)
    if result.returncode not in (0, 124):
        raise RuntimeError(f"Failed to list retained MQTT discovery topics: {result.stderr.strip() or result.stdout.strip()}")
    topics = []
    for line in result.stdout.splitlines():
        topic = line.split(" ", 1)[0].strip()
        if topic.endswith("/config"):
            topics.append(topic)
    return topics


def publish_empty_retained_topic(run_command, topic):
    command = [
        "sh",
        "-c",
        mosquitto_password_command()
        + "mosquitto_pub -h addon_core_mosquitto -u homeassistant -P \"$P\" -r -n -t \"$1\"",
        "sh",
        topic,
    ]
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to clear retained MQTT topic {topic}: {result.stderr.strip() or result.stdout.strip()}")



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


def deleted_devices_pending_diff(config_dir, rollback_file):
    source = Path(rollback_file)
    if not source.exists():
        raise RuntimeError("deleted_devices rollback snapshot is missing.")
    rollback_data = json.loads(source.read_text(encoding="utf-8"))
    _path, _text, current_data = read_device_registry(config_dir)
    before_lines = json.dumps(deleted_devices(rollback_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    current_lines = json.dumps(deleted_devices(current_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    diff = list(
        difflib.unified_diff(
            before_lines,
            current_lines,
            fromfile="deleted_devices before cleanup",
            tofile="deleted_devices now",
            lineterm="",
        )
    )
    return "\n".join(diff) if diff else "No deleted_devices difference."


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
