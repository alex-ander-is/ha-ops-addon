import hashlib
import difflib
import json
import os
import re
import subprocess
from pathlib import Path

import i18n


def _(key, **values):
    return i18n.t(key, **values)


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


def fingerprint_json(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return fingerprint_text(text)


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
    device_registry_fingerprint = fingerprint_text(text)
    candidates = []
    for device in data.get("data", {}).get("devices", []):
        ieee = mqtt_zigbee2mqtt_identifier(device)
        if not ieee or ieee in known_ieees:
            continue
        topics = sorted(retained_by_ieee.get(ieee, []))
        identity_payload = {
            "device_id": device.get("id") or "",
            "ieee": ieee,
            "identifiers": ["mqtt", f"zigbee2mqtt_{ieee}"],
            "retained_topics": topics,
        }
        candidates.append(
            {
                "id": device.get("id") or "",
                "ieee": ieee,
                "identity": fingerprint_json(identity_payload),
                "identifiers": ["mqtt", f"zigbee2mqtt_{ieee}"],
                "name": device.get("name_by_user") or device.get("name") or "",
                "manufacturer": device.get("manufacturer") or "",
                "model": device.get("model") or device.get("model_id") or "",
                "retained_topics": topics,
                "reason": "Device exists in Home Assistant MQTT registry but is missing from current Zigbee2MQTT files.",
            }
        )
    candidates.sort(key=lambda item: (item["name"], item["ieee"], item["id"]))
    fingerprint_payload = {
        "schema": 1,
        "device_registry_fingerprint": device_registry_fingerprint,
        "known_zigbee2mqtt_ieees": sorted(known_ieees),
        "scanned_paths": sorted(scanned_paths),
        "retained_topics": sorted(str(topic) for topic in retained_topics or [] if isinstance(topic, str)),
        "candidates": [
            {
                "id": item["id"],
                "ieee": item["ieee"],
                "identity": item["identity"],
                "identifiers": item["identifiers"],
                "retained_topics": item["retained_topics"],
            }
            for item in candidates
        ],
    }
    return {
        "count": len(candidates),
        "device_registry_fingerprint": device_registry_fingerprint,
        "fingerprint": fingerprint_json(fingerprint_payload),
        "scanned_paths": scanned_paths,
        "candidates": candidates,
    }


def build_stale_mqtt_discovery_preview(config_dir, retained_topics=None):
    preview = stale_mqtt_discovery_candidates(config_dir, retained_topics)
    lines = [_("preview.retained_title", count=preview["count"])]
    lines.append(_("preview.retained_description"))
    lines.append(_("preview.retained_effect"))
    if preview["scanned_paths"]:
        lines.append(_("preview.retained_scanned_paths", paths=", ".join(preview["scanned_paths"])))
    else:
        lines.append(_("preview.retained_no_zigbee2mqtt_files"))
    if not preview["candidates"]:
        lines.append(_("preview.retained_none"))
    for item in preview["candidates"]:
        label = " | ".join(part for part in [item["name"], item["manufacturer"], item["model"], item["ieee"]] if part)
        lines.append(f"- {label}")
        for topic in item["retained_topics"]:
            lines.append(_("preview.retained_topic", topic=topic))
    return {**preview, "summary": "\n".join(lines)}


def clear_stale_mqtt_discovery_topics(topics, publish_empty_retained):
    cleared = []
    for topic in sorted(set(topics or [])):
        publish_empty_retained(topic)
        cleared.append(topic)
    return cleared


def mqtt_service_value(config, *names, default=None):
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return value
    return default


def mosquitto_command(base, mqtt_config, args):
    host = mqtt_service_value(mqtt_config, "host", "hostname", default="addon_core_mosquitto")
    port = mqtt_service_value(mqtt_config, "port", default=1883)
    username = mqtt_service_value(mqtt_config, "username", "user")
    password = mqtt_service_value(mqtt_config, "password")
    command = [*base, "-h", str(host), "-p", str(port)]
    if username:
        command.extend(["-u", str(username)])
    if password:
        command.extend(["-P", str(password)])
    command.extend(args)
    return command


def list_retained_discovery_topics(run_command, mqtt_config, timeout_seconds=8):
    command = [
        "timeout",
        str(int(timeout_seconds)),
        "mosquitto_sub",
    ]
    command = mosquitto_command(command, mqtt_config, ["-t", "homeassistant/#", "-v"])
    result = run_command(command)
    if result.returncode not in (0, 124):
        raise RuntimeError(f"Failed to list retained MQTT discovery topics: {result.stderr.strip() or result.stdout.strip()}")
    topics = []
    for line in result.stdout.splitlines():
        topic = line.split(" ", 1)[0].strip()
        if topic.endswith("/config"):
            topics.append(topic)
    return topics


def publish_empty_retained_topic(run_command, topic, mqtt_config):
    command = [
        "mosquitto_pub",
    ]
    command = mosquitto_command(command, mqtt_config, ["-r", "-n", "-t", topic])
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to clear retained MQTT topic {topic}: {result.stderr.strip() or result.stdout.strip()}")



def rollback_dir(work_dir):
    return Path(work_dir) / "deleted-devices-rollback"


def rollback_path(work_dir):
    return rollback_dir(work_dir) / "core.device_registry"


def entity_rollback_path(rollback_file):
    return Path(rollback_file).with_name("core.entity_registry")


# v1 is deliberately separate from the old pair of files.  The old files have
# no lifecycle information and are therefore only safe when state.json points
# at them explicitly.  This self-describing file is the sole startup-discovered
# rollback source.
ROLLBACK_MANIFEST_NAME = "rollback-manifest-v1.json"
ROLLBACK_MANIFEST_VERSION = 1
ROLLBACK_PHASE_RESTORE_REQUIRED = "restore_required"
ROLLBACK_PHASE_RECOVERING = "recovering"
ROLLBACK_PHASE_PENDING = "pending_confirmation"
ROLLBACK_PHASE_CONFIRMED = "confirmed"
ROLLBACK_PHASE_REVERTED = "reverted"
ROLLBACK_NONTERMINAL_PHASES = {ROLLBACK_PHASE_RESTORE_REQUIRED, ROLLBACK_PHASE_RECOVERING}
ROLLBACK_TERMINAL_PHASES = {ROLLBACK_PHASE_CONFIRMED, ROLLBACK_PHASE_REVERTED}


def rollback_manifest_path(work_dir):
    return rollback_dir(work_dir) / ROLLBACK_MANIFEST_NAME


def manifest_sidecar_path(manifest_file, registry_name):
    return Path(manifest_file).with_name(f"{registry_name}.snapshot")


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace_bytes(path, payload, *, sync_directory=True):
    """Write bytes durably using a same-directory temp + atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if sync_directory:
            _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _durable_unlink(path):
    path = Path(path)
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


def _snapshot_record(manifest_file, registry_name, text):
    if text is None:
        return {"present": False, "sidecar": None, "sha256": None, "length": 0}
    payload = text.encode("utf-8")
    sidecar = manifest_sidecar_path(manifest_file, registry_name)
    _durable_replace_bytes(sidecar, payload)
    return {
        "present": True,
        "sidecar": sidecar.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "length": len(payload),
    }


def _manifest_payload(manifest_file, manifest):
    try:
        if not isinstance(manifest, dict) or manifest.get("version") != ROLLBACK_MANIFEST_VERSION:
            raise ValueError("unsupported rollback manifest version")
        if manifest.get("phase") not in (ROLLBACK_NONTERMINAL_PHASES | ROLLBACK_TERMINAL_PHASES | {ROLLBACK_PHASE_PENDING}):
            raise ValueError("invalid rollback manifest phase")
        registries = manifest["registries"]
        result = {}
        for registry_name in ("core.device_registry", "core.entity_registry"):
            record = registries[registry_name]
            present = record.get("present")
            if not isinstance(present, bool):
                raise ValueError(f"invalid presence for {registry_name}")
            if not present:
                if record.get("sidecar") is not None or record.get("sha256") is not None:
                    raise ValueError(f"unexpected sidecar for absent {registry_name}")
                result[registry_name] = None
                continue
            name = record.get("sidecar")
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError(f"invalid sidecar name for {registry_name}")
            payload = (Path(manifest_file).parent / name).read_bytes()
            if record.get("length") != len(payload) or record.get("sha256") != hashlib.sha256(payload).hexdigest():
                raise ValueError(f"invalid sidecar payload for {registry_name}")
            result[registry_name] = payload
        return result
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Deleted devices rollback manifest is invalid: {exc}") from exc


def load_rollback_manifest(manifest_file):
    manifest_file = Path(manifest_file)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Deleted devices rollback manifest is invalid: {exc}") from exc
    return manifest, _manifest_payload(manifest_file, manifest)


def load_rollback_manifest_metadata(manifest_file):
    """Load v1 lifecycle metadata without requiring terminal sidecars."""
    manifest_file = Path(manifest_file)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != ROLLBACK_MANIFEST_VERSION:
            raise ValueError("unsupported rollback manifest version")
        if manifest.get("phase") not in (
            ROLLBACK_NONTERMINAL_PHASES | ROLLBACK_TERMINAL_PHASES | {ROLLBACK_PHASE_PENDING}
        ):
            raise ValueError("invalid rollback manifest phase")
        registries = manifest["registries"]
        for registry_name in ("core.device_registry", "core.entity_registry"):
            record = registries[registry_name]
            if not isinstance(record.get("present"), bool):
                raise ValueError(f"invalid presence for {registry_name}")
            sidecar = record.get("sidecar")
            if record["present"]:
                if not isinstance(sidecar, str) or Path(sidecar).name != sidecar:
                    raise ValueError(f"invalid sidecar name for {registry_name}")
            elif sidecar is not None:
                raise ValueError(f"unexpected sidecar for absent {registry_name}")
        return manifest
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Deleted devices rollback manifest is invalid: {exc}") from exc


def rollback_manifest_is_v1(rollback_file):
    """Do not trust stale state metadata to reinterpret a legacy snapshot."""
    return Path(rollback_file).name == ROLLBACK_MANIFEST_NAME


def rollback_manifest_status(work_dir):
    """Return the canonical v1 artifact status without consulting state."""
    path = rollback_manifest_path(work_dir)
    if not path.exists():
        return {"status": "absent", "path": path}
    try:
        manifest = load_rollback_manifest_metadata(path)
        # Terminal manifests are already committed. Sidecars may have been
        # durably removed before a later unlink failed, so they are not needed
        # to complete their artifact cleanup on the next startup.
        if manifest["phase"] in ROLLBACK_TERMINAL_PHASES:
            return {"status": "valid", "path": path, "manifest": manifest, "payloads": None}
        manifest, payloads = load_rollback_manifest(path)
        return {"status": "valid", "path": path, "manifest": manifest, "payloads": payloads}
    except RuntimeError as exc:
        return {"status": "invalid", "path": path, "error": exc}


def rollback_manifest_phase(manifest_file, phase):
    manifest, _payloads = load_rollback_manifest(manifest_file)
    manifest["phase"] = phase
    _durable_replace_bytes(Path(manifest_file), (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return manifest


def read_entity_registry(config_dir):
    path = entity_registry_path(config_dir)
    if not path.exists():
        return path, None, {}
    text = path.read_text(encoding="utf-8")
    return path, text, json.loads(text)


def _parse_registry_text(registry_name, text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise i18n.error("error.registry_json_invalid", registry=registry_name, error=str(exc)) from exc


def registry_collection(data, registry_name, collection_name):
    if not isinstance(data, dict):
        raise i18n.error("error.registry_json_must_be_object", registry=registry_name)
    registry_data = data.get("data")
    if not isinstance(registry_data, dict):
        raise i18n.error("error.registry_data_must_be_object", registry=registry_name)
    value = registry_data.get(collection_name)
    if not isinstance(value, list):
        raise i18n.error("error.registry_collection_must_be_array", registry=registry_name, path=f"data.{collection_name}")
    return value


def validated_deleted_devices(data):
    return registry_collection(data, "core.device_registry", "deleted_devices")


def validated_deleted_entities(data):
    return registry_collection(data, "core.entity_registry", "deleted_entities")


def validated_devices(data):
    return registry_collection(data, "core.device_registry", "devices")


def validated_entities(data):
    return registry_collection(data, "core.entity_registry", "entities")


def validated_areas(data):
    return registry_collection(data, "core.area_registry", "areas")


def read_validated_device_registry(config_dir):
    path = device_registry_path(config_dir)
    if not path.exists():
        raise RuntimeError(f"Home Assistant device registry not found: {path}")
    text = path.read_text(encoding="utf-8")
    data = _parse_registry_text("core.device_registry", text)
    validated_devices(data)
    validated_deleted_devices(data)
    return path, text, data


def read_validated_entity_registry(config_dir):
    path = entity_registry_path(config_dir)
    if not path.exists():
        return path, None, None
    text = path.read_text(encoding="utf-8")
    data = _parse_registry_text("core.entity_registry", text)
    validated_entities(data)
    validated_deleted_entities(data)
    return path, text, data


def read_validated_area_registry(config_dir):
    path = area_registry_path(config_dir)
    if not path.exists():
        return path, None, None
    text = path.read_text(encoding="utf-8")
    data = _parse_registry_text("core.area_registry", text)
    validated_areas(data)
    return path, text, data


def registry_fingerprint(device_text, entity_text):
    entity_component = entity_text if entity_text is not None else "<missing core.entity_registry>"
    return fingerprint_text(f"{device_text}\n\0\n{entity_component}")


def create_deleted_devices_rollback(config_dir, work_dir, expected_fingerprint, enrichment=None):
    path, text, _data = read_validated_device_registry(config_dir)
    _entity_path, entity_text, _entity_data = read_validated_entity_registry(config_dir)
    current_fingerprint = registry_fingerprint(text, entity_text)
    if expected_fingerprint and current_fingerprint != expected_fingerprint:
        raise RuntimeError("Deleted devices changed since preview. Run Check deleted devices again.")
    manifest_file = rollback_manifest_path(work_dir)
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    # Sidecars are fully committed before the manifest makes them discoverable.
    device_record = _snapshot_record(manifest_file, "core.device_registry", text)
    entity_record = _snapshot_record(manifest_file, "core.entity_registry", entity_text)
    device_count = len(validated_deleted_devices(_parse_registry_text("core.device_registry", text)))
    entity_count = len(validated_deleted_entities(_parse_registry_text("core.entity_registry", entity_text))) if entity_text is not None else 0
    manifest = {
        "version": ROLLBACK_MANIFEST_VERSION,
        "phase": ROLLBACK_PHASE_RESTORE_REQUIRED,
        "fingerprint": current_fingerprint,
        "device_count": device_count,
        "entity_count": entity_count,
        "registries": {
            "core.device_registry": device_record,
            "core.entity_registry": entity_record,
        },
    }
    if enrichment:
        manifest["deleted_devices_enrichment"] = sanitize_manifest_deleted_devices_enrichment(enrichment)
    # The directory fsync in _durable_replace_bytes is the commit point.  No
    # state publication or registry mutation happens before this returns.
    _durable_replace_bytes(
        manifest_file,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return {
        "path": str(manifest_file),
        "fingerprint": current_fingerprint,
        "format": "manifest_v1",
        "device_count": device_count,
        "entity_count": entity_count,
    }


def sanitize_manifest_deleted_devices_enrichment(enrichment):
    sanitized = sanitize_deleted_devices_enrichment(enrichment)
    manifest_enrichment = {
        "schema": 1,
        "devices": [
            {"id": device_id, **values}
            for device_id, values in sorted(sanitized["by_id"].items())
        ],
    }
    if sanitized["warnings"]:
        manifest_enrichment["warnings"] = sanitized["warnings"]
    return manifest_enrichment


def _replace_registry_from_snapshot(destination, payload):
    _durable_replace_bytes(destination, payload)


def _restore_v1_tombstones_into_current(current_data, snapshot_entries, name):
    if name == "core.device_registry":
        validated_devices(current_data)
        current_data.setdefault("data", {})["deleted_devices"] = list(snapshot_entries)
        return len(snapshot_entries), 0
    validated_entities(current_data)
    current_data.setdefault("data", {})["deleted_entities"] = list(snapshot_entries)
    return 0, len(snapshot_entries)


def _restore_v1_registry(config_dir, source, manifest, payloads):
    source = Path(source)
    destinations = {
        "core.device_registry": device_registry_path(config_dir),
        "core.entity_registry": entity_registry_path(config_dir),
    }
    restored_devices = restored_entities = 0
    merged_devices = merged_entities = 0
    preserved_devices = preserved_entities = 0
    result_texts = {}
    for name, destination in destinations.items():
        original = payloads[name]
        if original is None:
            # The optional entity registry did not exist at the snapshot
            # point. It may have been created by Home Assistant later; a
            # rollback must never remove or rewrite that newer registry.
            try:
                result_texts[name] = destination.read_text(encoding="utf-8")
            except FileNotFoundError:
                result_texts[name] = None
            continue
        snapshot_data = json.loads(original.decode("utf-8"))
        snapshot_entries = validated_deleted_devices(snapshot_data) if name == "core.device_registry" else validated_deleted_entities(snapshot_data)
        try:
            current_text = destination.read_text(encoding="utf-8")
            current_data = json.loads(current_text)
        except (OSError, json.JSONDecodeError):
            # A torn/missing registry has no later tombstones to merge. Restore
            # the exact verified bytes rather than attempting to parse it.
            _replace_registry_from_snapshot(destination, original)
            result_texts[name] = original.decode("utf-8")
            if name == "core.device_registry":
                restored_devices = len(snapshot_entries)
                merged_devices = len(snapshot_entries)
            else:
                restored_entities = len(snapshot_entries)
                merged_entities = len(snapshot_entries)
            continue
        try:
            current_entries = validated_deleted_devices(current_data) if name == "core.device_registry" else validated_deleted_entities(current_data)
        except RuntimeError:
            # If only the tombstone collection is malformed, preserve valid
            # live registry records and repair just the tombstones from the
            # verified rollback sidecar. Broader shape failures fall back to
            # the established full snapshot restore below.
            try:
                repaired_devices, repaired_entities = _restore_v1_tombstones_into_current(current_data, snapshot_entries, name)
            except RuntimeError:
                _replace_registry_from_snapshot(destination, original)
                result_texts[name] = original.decode("utf-8")
                if name == "core.device_registry":
                    restored_devices = len(snapshot_entries)
                    merged_devices = len(snapshot_entries)
                else:
                    restored_entities = len(snapshot_entries)
                    merged_entities = len(snapshot_entries)
                continue
            _durable_replace_bytes(destination, (json.dumps(current_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            result_texts[name] = destination.read_text(encoding="utf-8")
            if name == "core.device_registry":
                restored_devices = repaired_devices
                merged_devices = repaired_devices
            else:
                restored_entities = repaired_entities
                merged_entities = repaired_entities
            continue
        merged = merge_deleted_devices(current_entries, snapshot_entries) if name == "core.device_registry" else merge_deleted_entities(current_entries, snapshot_entries)
        if name == "core.device_registry":
            current_data.setdefault("data", {})["deleted_devices"] = merged
            restored_devices, merged_devices, preserved_devices = len(snapshot_entries), len(merged), len(current_entries)
        else:
            current_data.setdefault("data", {})["deleted_entities"] = merged
            restored_entities, merged_entities, preserved_entities = len(snapshot_entries), len(merged), len(current_entries)
        _durable_replace_bytes(destination, (json.dumps(current_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        result_texts[name] = destination.read_text(encoding="utf-8")
    return {
        "fingerprint": registry_fingerprint(result_texts["core.device_registry"], result_texts["core.entity_registry"]),
        "restored": restored_devices + restored_entities,
        "merged": merged_devices + merged_entities,
        "preserved": preserved_devices + preserved_entities,
        "restored_devices": restored_devices,
        "restored_entities": restored_entities,
    }


def _pending_tree_from_v1_manifest(config_dir, source):
    manifest, payloads = load_rollback_manifest(source)
    rollback_data = _parse_registry_text("core.device_registry", payloads["core.device_registry"].decode("utf-8"))
    validated_devices(rollback_data)
    validated_deleted_devices(rollback_data)
    entity_payload = payloads["core.entity_registry"]
    rollback_entity_data = None
    if entity_payload is not None:
        rollback_entity_data = _parse_registry_text("core.entity_registry", entity_payload.decode("utf-8"))
        validated_entities(rollback_entity_data)
        validated_deleted_entities(rollback_entity_data)
    _current_path, _current_text, current_data = read_validated_device_registry(config_dir)
    _entity_path, _entity_text, current_entity_data = read_validated_entity_registry(config_dir)
    _area_path, _area_text, area_data = read_validated_area_registry(config_dir)
    # Active entities come from current context, deleted tombstones from the
    # verified rollback sidecars.
    if rollback_entity_data is not None and current_entity_data is not None:
        context_entity_data = {
            "data": {
                "entities": validated_entities(current_entity_data),
                "deleted_entities": validated_deleted_entities(rollback_entity_data),
            }
        }
    else:
        context_entity_data = rollback_entity_data
    return build_deleted_devices_tree_from_data(
        rollback_data,
        context_entity_data,
        area_data,
        generated_from="pending",
        enrichment=manifest.get("deleted_devices_enrichment"),
    )


def build_deleted_devices_pending_tree(config_dir, rollback_file):
    source = Path(rollback_file)
    if rollback_manifest_is_v1(source):
        return _pending_tree_from_v1_manifest(config_dir, source)
    if not source.exists():
        raise RuntimeError("Deleted devices rollback snapshot is missing.")
    rollback_text = source.read_text(encoding="utf-8")
    rollback_data = _parse_registry_text("core.device_registry", rollback_text)
    validated_devices(rollback_data)
    validated_deleted_devices(rollback_data)
    _area_path, _area_text, area_data = read_validated_area_registry(config_dir)
    entity_snapshot = entity_rollback_path(source)
    entity_data = None
    if entity_snapshot.exists():
        entity_data = _parse_registry_text("core.entity_registry", entity_snapshot.read_text(encoding="utf-8"))
        validated_entities(entity_data)
        validated_deleted_entities(entity_data)
    return build_deleted_devices_tree_from_data(rollback_data, entity_data, area_data, generated_from="pending")


def restore_deleted_devices_rollback(config_dir, rollback_file):
    source = Path(rollback_file)
    if source.name == ROLLBACK_MANIFEST_NAME:
        manifest, payloads = load_rollback_manifest(source)
        return _restore_v1_registry(config_dir, source, manifest, payloads)
    if not source.exists():
        # Older cleanup state did not persist whether the rollback included
        # entities.  Preserve the established device-only fallback rather than
        # presenting a vague registry label.
        raise RuntimeError("Deleted devices rollback snapshot is missing.")
    rollback_data = _parse_registry_text("core.device_registry", source.read_text(encoding="utf-8"))
    restored_devices = validated_deleted_devices(rollback_data)

    dest, _text, current_data = read_validated_device_registry(config_dir)
    current_devices = validated_deleted_devices(current_data)
    merged_devices = merge_deleted_devices(current_devices, restored_devices)
    current_data.setdefault("data", {})["deleted_devices"] = merged_devices

    tmp_path = dest.with_name(f".{dest.name}.tmp")
    tmp_path.write_text(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, dest)
    entity_snapshot = entity_rollback_path(source)
    restored_entities = []
    current_entities = []
    merged_entities = []
    entity_text = None
    if entity_snapshot.exists():
        rollback_entity_data = _parse_registry_text("core.entity_registry", entity_snapshot.read_text(encoding="utf-8"))
        restored_entities = validated_deleted_entities(rollback_entity_data)
        entity_dest, _current_entity_text, current_entity_data = read_validated_entity_registry(config_dir)
        current_entities = validated_deleted_entities(current_entity_data)
        merged_entities = merge_deleted_entities(current_entities, restored_entities)
        current_entity_data.setdefault("data", {})["deleted_entities"] = merged_entities
        tmp_entity_path = entity_dest.with_name(f".{entity_dest.name}.tmp")
        tmp_entity_path.write_text(json.dumps(current_entity_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_entity_path, entity_dest)
        entity_text = entity_dest.read_text(encoding="utf-8")
    text = dest.read_text(encoding="utf-8")
    return {
        "fingerprint": registry_fingerprint(text, entity_text),
        "restored": len(restored_devices) + len(restored_entities),
        "merged": len(merged_devices) + len(merged_entities),
        "preserved": len(current_devices) + len(current_entities),
        "restored_devices": len(restored_devices),
        "restored_entities": len(restored_entities),
    }


def discard_deleted_devices_rollback(rollback_file):
    path = Path(rollback_file)
    if rollback_manifest_is_v1(path):
        # Do not unlink the manifest in a finally block. It is the retry
        # record when a sidecar unlink or its directory fsync fails. Metadata
        # remains readable even after some already-durable sidecar removals.
        manifest = load_rollback_manifest_metadata(path)
        records = manifest["registries"]
        for name in ("core.device_registry", "core.entity_registry"):
            sidecar = records[name].get("sidecar")
            if sidecar:
                _durable_unlink(path.parent / sidecar)
        _durable_unlink(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return
    if path.exists():
        path.unlink()
    entity_path = entity_rollback_path(path)
    if entity_path.exists():
        entity_path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass


def deleted_devices(data):
    return data.get("data", {}).get("deleted_devices", [])


def deleted_entities(data):
    return data.get("data", {}).get("deleted_entities", [])


def deleted_entries_label(device_count, entity_count):
    if device_count and entity_count:
        return _("label.deleted_devices_and_entities")
    if device_count:
        return _("label.deleted_devices")
    if entity_count:
        return _("label.deleted_entities")
    # Older pending cleanups did not persist a scope.  Keep their established
    # device-only wording rather than exposing a vague registry term.
    return _("label.deleted_devices")


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


def deleted_entity_key(entity):
    if isinstance(entity, dict):
        for key in ("id", "entity_id", "unique_id"):
            if entity.get(key):
                return (key, str(entity[key]))
    return ("json", json.dumps(entity, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def merge_deleted_entities(current_entities, restored_entities):
    merged = list(current_entities)
    seen = {deleted_entity_key(entity) for entity in merged}
    for entity in restored_entities:
        key = deleted_entity_key(entity)
        if key in seen:
            continue
        merged.append(entity)
        seen.add(key)
    return merged


def deleted_devices_cleanup_status(config_dir, rollback_file):
    source = Path(rollback_file)
    if rollback_manifest_is_v1(source):
        metadata = load_rollback_manifest_metadata(source)
        if metadata["phase"] in ROLLBACK_TERMINAL_PHASES:
            # A terminal phase is the durable outcome. Sidecars may already
            # be gone after an interrupted artifact cleanup, so do not turn a
            # safe retry into a manual-recovery error by loading them again.
            return {
                "fingerprint": None,
                "removed": 0,
                "current": 0,
                "returned": 0,
                "added": 0,
                "returned_entities": 0,
                "removed_devices": 0,
                "removed_entities": 0,
                "current_devices": 0,
                "current_entities": 0,
                "added_devices": 0,
                "added_entities": 0,
                "returned_devices": 0,
                "terminal_phase": metadata["phase"],
            }
        _manifest, payloads = load_rollback_manifest(source)
        rollback_data = _parse_registry_text("core.device_registry", payloads["core.device_registry"].decode("utf-8"))
        removed_devices = validated_deleted_devices(rollback_data)
        removed_keys = {deleted_device_key(device) for device in removed_devices}
        _path, text, current_data = read_validated_device_registry(config_dir)
        current_devices = validated_deleted_devices(current_data)
        returned = [device for device in current_devices if deleted_device_key(device) in removed_keys]
        added = [device for device in current_devices if deleted_device_key(device) not in removed_keys]
        entity_payload = payloads["core.entity_registry"]
        removed_entities = current_entities = returned_entities = added_entities = []
        entity_text = None
        if entity_payload is not None:
            rollback_entity_data = _parse_registry_text("core.entity_registry", entity_payload.decode("utf-8"))
            removed_entities = validated_deleted_entities(rollback_entity_data)
            removed_entity_keys = {deleted_entity_key(entity) for entity in removed_entities}
            _entity_path, entity_text, current_entity_data = read_validated_entity_registry(config_dir)
            current_entities = validated_deleted_entities(current_entity_data)
            returned_entities = [entity for entity in current_entities if deleted_entity_key(entity) in removed_entity_keys]
            added_entities = [entity for entity in current_entities if deleted_entity_key(entity) not in removed_entity_keys]
        return {
            "fingerprint": registry_fingerprint(text, entity_text),
            "removed": len(removed_devices) + len(removed_entities),
            "current": len(current_devices) + len(current_entities),
            "returned": len(returned) + len(returned_entities),
            "added": len(added) + len(added_entities),
            "returned_entities": len(returned_entities),
            "removed_devices": len(removed_devices), "removed_entities": len(removed_entities),
            "current_devices": len(current_devices), "current_entities": len(current_entities),
            "added_devices": len(added), "added_entities": len(added_entities),
            "returned_devices": len(returned),
            "terminal_phase": None,
        }
    if not source.exists():
        raise RuntimeError("Deleted devices rollback snapshot is missing.")
    rollback_data = _parse_registry_text("core.device_registry", source.read_text(encoding="utf-8"))
    removed_devices = validated_deleted_devices(rollback_data)
    removed_keys = {deleted_device_key(device) for device in removed_devices}
    _path, text, current_data = read_validated_device_registry(config_dir)
    current_devices = validated_deleted_devices(current_data)
    returned = [device for device in current_devices if deleted_device_key(device) in removed_keys]
    added = [device for device in current_devices if deleted_device_key(device) not in removed_keys]
    entity_snapshot = entity_rollback_path(source)
    removed_entities = []
    current_entities = []
    returned_entities = []
    added_entities = []
    entity_text = None
    if entity_snapshot.exists():
        rollback_entity_data = _parse_registry_text("core.entity_registry", entity_snapshot.read_text(encoding="utf-8"))
        removed_entities = validated_deleted_entities(rollback_entity_data)
        removed_entity_keys = {deleted_entity_key(entity) for entity in removed_entities}
        _entity_path, entity_text, current_entity_data = read_validated_entity_registry(config_dir)
        current_entities = validated_deleted_entities(current_entity_data)
        returned_entities = [entity for entity in current_entities if deleted_entity_key(entity) in removed_entity_keys]
        added_entities = [entity for entity in current_entities if deleted_entity_key(entity) not in removed_entity_keys]
    return {
        "fingerprint": registry_fingerprint(text, entity_text),
        "removed": len(removed_devices) + len(removed_entities),
        "current": len(current_devices) + len(current_entities),
        "returned": len(returned) + len(returned_entities),
        "added": len(added) + len(added_entities),
        "returned_entities": len(returned_entities),
        "removed_devices": len(removed_devices),
        "removed_entities": len(removed_entities),
        "current_devices": len(current_devices),
        "current_entities": len(current_entities),
        "added_devices": len(added),
        "added_entities": len(added_entities),
        "returned_devices": len(returned),
        "terminal_phase": None,
    }


def deleted_devices_pending_diff(config_dir, rollback_file):
    source = Path(rollback_file)
    if source.name == ROLLBACK_MANIFEST_NAME:
        _manifest, payloads = load_rollback_manifest(source)
        rollback_data = _parse_registry_text("core.device_registry", payloads["core.device_registry"].decode("utf-8"))
        _path, _text, current_data = read_validated_device_registry(config_dir)
        removed_devices = validated_deleted_devices(rollback_data)
        diff = []
        if removed_devices:
            diff.extend(difflib.unified_diff(json.dumps(removed_devices, ensure_ascii=False, indent=2, sort_keys=True).splitlines(), json.dumps(validated_deleted_devices(current_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines(), fromfile="deleted devices before cleanup", tofile="deleted devices now", lineterm=""))
        if payloads["core.entity_registry"] is not None:
            rollback_entity_data = _parse_registry_text("core.entity_registry", payloads["core.entity_registry"].decode("utf-8"))
            _entity_path, _entity_text, current_entity_data = read_validated_entity_registry(config_dir)
            removed_entities = validated_deleted_entities(rollback_entity_data)
            if removed_entities:
                diff.extend(difflib.unified_diff(json.dumps(removed_entities, ensure_ascii=False, indent=2, sort_keys=True).splitlines(), json.dumps(validated_deleted_entities(current_entity_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines(), fromfile="deleted entities before cleanup", tofile="deleted entities now", lineterm=""))
        return "\n".join(diff) if diff else "No deleted devices difference."
    if not source.exists():
        raise RuntimeError("Deleted devices rollback snapshot is missing.")
    rollback_data = _parse_registry_text("core.device_registry", source.read_text(encoding="utf-8"))
    _path, _text, current_data = read_validated_device_registry(config_dir)
    removed_devices = validated_deleted_devices(rollback_data)
    diff = []
    if removed_devices:
        before_lines = json.dumps(removed_devices, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        current_lines = json.dumps(validated_deleted_devices(current_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        diff.extend(
            difflib.unified_diff(
                before_lines,
                current_lines,
                fromfile="deleted devices before cleanup",
                tofile="deleted devices now",
                lineterm="",
            )
        )
    entity_snapshot = entity_rollback_path(source)
    if entity_snapshot.exists():
        rollback_entity_data = _parse_registry_text("core.entity_registry", entity_snapshot.read_text(encoding="utf-8"))
        _entity_path, _entity_text, current_entity_data = read_validated_entity_registry(config_dir)
        removed_entities = validated_deleted_entities(rollback_entity_data)
        if removed_entities:
            before_entity_lines = json.dumps(removed_entities, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
            current_entity_lines = json.dumps(validated_deleted_entities(current_entity_data), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
            diff.extend(
                difflib.unified_diff(
                    before_entity_lines,
                    current_entity_lines,
                    fromfile="deleted entities before cleanup",
                    tofile="deleted entities now",
                    lineterm="",
                )
            )
    return "\n".join(diff) if diff else "No deleted devices difference."


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


def normalize_recovered_name(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"^[^\w]+", "", text, flags=re.UNICODE).strip() or text


def identifier_key(identifier):
    if not isinstance(identifier, list) or len(identifier) < 2:
        return None
    return tuple(str(item) for item in identifier[:2])


def deleted_device_identifier_keys(device):
    keys = set()
    for identifier in device.get("identifiers") or []:
        key = identifier_key(identifier)
        if key:
            keys.add(key)
    ieee = mqtt_zigbee2mqtt_identifier(device)
    if ieee:
        keys.add(("mqtt", f"zigbee2mqtt_{ieee}"))
    return keys


def historical_devices_from_snapshot(text):
    data = json.loads(text)
    devices = data.get("data", {}).get("devices")
    if not isinstance(devices, list):
        raise RuntimeError("Historical device registry snapshot has no device list.")
    return [device for device in devices if isinstance(device, dict)]


def enrich_row_from_history(row, device, source_commit, source_path):
    enriched = dict(row)
    enriched.update(
        {
            "recovered_name": normalize_recovered_name(device.get("name_by_user") or device.get("name")),
            "recovered_manufacturer": device.get("manufacturer") or "",
            "recovered_model": device.get("model") or "",
            "recovered_model_id": device.get("model_id") or "",
            "recovered_identifiers": device.get("identifiers") or [],
            "source_commit": source_commit,
            "source_path": source_path,
        }
    )
    return enriched


def enrich_deleted_device_rows_from_history(rows, devices, history_context):
    if not rows or not devices or not history_context:
        return rows
    repo_path = Path(history_context.get("repo_path") or "")
    registry_path = history_context.get("registry_path") or ""
    run_command = history_context.get("run_command")
    if not repo_path or not registry_path or not run_command:
        return rows
    try:
        repo_path = repo_path.resolve()
        if not (repo_path / ".git").exists():
            return rows
        registry_path = str(Path(registry_path).as_posix())
        if not registry_path or registry_path.startswith("../") or registry_path.startswith("/") or registry_path == ".":
            return rows
        log_result = run_command(
            ["git", "log", "--format=%H", "--max-count=50", "--", registry_path],
            cwd=repo_path,
        )
        if log_result.returncode != 0:
            return rows
        commits = [line.strip() for line in log_result.stdout.splitlines() if line.strip()]
    except Exception:
        return rows

    by_id = {str(device.get("id")): device for device in devices if isinstance(device, dict) and device.get("id")}
    row_ids = {str(row.get("id")) for row in rows if row.get("id")}
    wanted_identifiers = {
        str(device.get("id")): deleted_device_identifier_keys(device)
        for device in devices
        if isinstance(device, dict) and device.get("id")
    }
    recovered = {}
    for commit in commits:
        if row_ids.issubset(recovered):
            break
        try:
            show_result = run_command(["git", "show", f"{commit}:{registry_path}"], cwd=repo_path)
            if show_result.returncode != 0:
                continue
            historical_devices = historical_devices_from_snapshot(show_result.stdout)
        except Exception:
            continue
        historical_by_id = {str(device.get("id")): device for device in historical_devices if device.get("id")}
        historical_by_identifier = {}
        for device in historical_devices:
            for key in deleted_device_identifier_keys(device):
                historical_by_identifier.setdefault(key, device)
        for device_id, current_device in by_id.items():
            if device_id in recovered:
                continue
            historical = historical_by_id.get(device_id)
            if not historical:
                for key in wanted_identifiers.get(device_id, set()):
                    historical = historical_by_identifier.get(key)
                    if historical:
                        break
            if historical:
                recovered[device_id] = (historical, commit)

    if not recovered:
        return rows
    return [
        enrich_row_from_history(row, *recovered[str(row.get("id"))], registry_path)
        if row.get("id") and str(row.get("id")) in recovered
        else row
        for row in rows
    ]


def area_names_from_data(area_data):
    areas = validated_areas(area_data) if area_data is not None else []
    return {area.get("id"): area.get("name") for area in areas if area.get("id")}


def area_names(config_dir):
    _path, _text, data = read_validated_area_registry(config_dir)
    return area_names_from_data(data)


def entities_by_device_from_data(entity_data):
    entities = []
    if entity_data is not None:
        entities = validated_entities(entity_data) + validated_deleted_entities(entity_data)
    grouped = {}
    for entity in entities:
        device_id = entity.get("device_id")
        if not device_id:
            continue
        grouped.setdefault(device_id, []).append(entity)
    return grouped


def entities_by_device(config_dir):
    _path, _text, data = read_validated_entity_registry(config_dir)
    return entities_by_device_from_data(data)


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


def deleted_entity_rows(config_dir, entities):
    areas = area_names(config_dir)
    rows = []
    for entity in entities:
        area_id = entity.get("area_id") or ""
        rows.append(
            {
                "area": areas.get(area_id) or area_id,
                "id": entity.get("id") or "",
                "entity_id": entity.get("entity_id") or "",
                "original_name": entity.get("original_name") or entity.get("name") or "",
                "original_device_class": entity.get("original_device_class") or "",
                "kind": "deleted_entity",
            }
        )
    return rows


def _text_value(value):
    return str(value).strip() if value not in (None, "") else ""


def _semantic_slug(value):
    text = _text_value(value).lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _humanize_semantic_slug(value):
    slug = _semantic_slug(value)
    if not slug:
        return ""
    special = {
        "zigbee2mqtt": "Zigbee2MQTT",
        "z2m": "Zigbee2MQTT",
        "mqtt": "MQTT",
        "ha": "HA",
        "hassio": "Supervisor",
        "rssi": "RSSI",
        "lqi": "LQI",
    }
    return " ".join(special.get(part, part.upper() if len(part) <= 3 and part.isalpha() else part.capitalize()) for part in slug.split("_"))


def _hassio_addon_display_from_identifiers(identifiers):
    for identifier in identifiers or []:
        if not isinstance(identifier, list) or len(identifier) < 2:
            continue
        domain = _semantic_slug(identifier[0])
        value = _semantic_slug(identifier[1])
        if domain != "hassio" or not value:
            continue
        match = re.fullmatch(r"[0-9a-f]{6,}_(.+)", value)
        addon_slug = match.group(1) if match else value
        label = _humanize_semantic_slug(addon_slug)
        if label:
            return {
                "label": label,
                "manufacturer": "App",
                "model": "Supervisor",
            }
    return {}


def _entity_object_id(entity):
    entity_id = _text_value((entity or {}).get("entity_id"))
    return _semantic_slug(entity_id.split(".", 1)[1] if "." in entity_id else entity_id)


def _identifier_slug_candidates(identifiers):
    candidates = set()
    for identifier in identifiers or []:
        if not isinstance(identifier, list):
            continue
        for part in identifier[1:3]:
            slug = _semantic_slug(part)
            if not slug:
                continue
            candidates.add(slug)
            match = re.fullmatch(r"[0-9a-f]{6,}_(.+)", slug)
            if match:
                candidates.add(match.group(1))
    return candidates


def _identifiers_display(identifiers, limit=3):
    result = []
    for identifier in identifiers or []:
        if not isinstance(identifier, list):
            continue
        parts = [_text_value(part) for part in identifier[:2]]
        parts = [part for part in parts if part]
        if parts:
            result.append(parts)
        if len(result) >= limit:
            break
    return result


def _entity_summary(entity):
    return {
        "id": _text_value(entity.get("id")),
        "entity_id": _text_value(entity.get("entity_id")),
        "name": _text_value(entity.get("original_name") or entity.get("name")),
        "device_class": _text_value(entity.get("original_device_class")),
    }


def _device_display(device, areas, row_by_id=None, enrichment_by_id=None):
    device_id = _text_value(device.get("id"))
    row = (row_by_id or {}).get(device_id, {})
    enrichment = (enrichment_by_id or {}).get(device_id, {})
    hassio_display = _hassio_addon_display_from_identifiers(
        enrichment.get("identifiers") or row.get("recovered_identifiers") or device.get("identifiers")
    )
    name = _text_value(
        enrichment.get("recovered_name")
        or row.get("recovered_name")
        or normalize_recovered_name(device.get("name_by_user") or device.get("name"))
        or device.get("name_by_user")
        or device.get("name")
        or hassio_display.get("label")
        or device_id
    )
    area_id = _text_value(device.get("area_id"))
    manufacturer = _text_value(
        enrichment.get("manufacturer") or row.get("recovered_manufacturer") or device.get("manufacturer") or hassio_display.get("manufacturer")
    )
    model = _text_value(enrichment.get("model") or row.get("recovered_model") or device.get("model") or hassio_display.get("model"))
    model_id = _text_value(enrichment.get("model_id") or row.get("recovered_model_id") or device.get("model_id"))
    return {
        "id": device_id,
        "label": name or device_id or _("label.deleted_devices"),
        "name": name,
        "manufacturer": manufacturer,
        "model": model,
        "model_id": model_id,
        "area": areas.get(area_id) or area_id,
        "identifiers": enrichment.get("identifiers") or row.get("recovered_identifiers") or _identifiers_display(device.get("identifiers")),
        "source_commit": _text_value(enrichment.get("source_commit") or row.get("source_commit")),
        "source_path": _text_value(enrichment.get("source_path") or row.get("source_path")),
    }


def _device_entity_prefix_candidates(device, display):
    candidates = {_semantic_slug(display.get("id")), _semantic_slug(display.get("label")), _semantic_slug(display.get("name"))}
    candidates.update(_identifier_slug_candidates(display.get("identifiers")))
    candidates.update(_identifier_slug_candidates(device.get("identifiers")))
    label = _text_value(display.get("label") or display.get("name"))
    for part in re.split(r"\s+[•·-]\s+|[()]", label):
        candidates.add(_semantic_slug(part))
    return {candidate for candidate in candidates if len(candidate) >= 3}


def _infer_deleted_entity_device_id(entity, candidates_by_device_id):
    object_id = _entity_object_id(entity)
    if not object_id:
        return ""
    matches = []
    for device_id, candidates in candidates_by_device_id.items():
        best = ""
        for candidate in candidates:
            if object_id == candidate or object_id.startswith(f"{candidate}_"):
                if len(candidate) > len(best):
                    best = candidate
        if best:
            matches.append((device_id, best))
    if not matches:
        return ""
    longest = max(len(match[1]) for match in matches)
    winners = [device_id for device_id, candidate in matches if len(candidate) == longest]
    unique = sorted(set(winners))
    return unique[0] if len(unique) == 1 else ""


def _deleted_entity_probable_group_key(entity):
    object_id = _entity_object_id(entity)
    parts = [part for part in object_id.split("_") if part]
    if len(parts) < 3:
        return ""
    if parts[0].startswith("tze") and len(parts) >= 3:
        return "_".join(parts[:3])
    return "_".join(parts[:2])


def _synthetic_deleted_entity_groups(entities):
    buckets = {}
    for entity in entities:
        key = _deleted_entity_probable_group_key(entity)
        if key:
            buckets.setdefault(key, []).append(entity)
    grouped = []
    consumed_ids = set()
    for key, bucket in sorted(buckets.items()):
        if len(bucket) < 2:
            continue
        consumed_ids.update(id(entity) for entity in bucket)
        grouped.append(
            {
                "device": {
                    "id": key,
                    "label": _humanize_semantic_slug(key),
                    "name": _humanize_semantic_slug(key),
                    "manufacturer": "",
                    "model": "Probable group",
                    "model_id": "",
                    "area": "",
                    "identifiers": [],
                    "source_commit": "",
                    "source_path": "",
                },
                "deleted_entities": [_entity_summary(entity) for entity in bucket],
                "active_entities": [],
                "counts": {"deleted_entities": len(bucket), "active_entities": 0},
            }
        )
    remaining = [entity for entity in entities if id(entity) not in consumed_ids]
    return grouped, remaining


def build_deleted_devices_tree_from_data(
    device_data,
    entity_data=None,
    area_data=None,
    generated_from="preview",
    rows=None,
    enrichment=None,
):
    deleted = [device for device in validated_deleted_devices(device_data) if isinstance(device, dict)]
    current_devices = [device for device in validated_devices(device_data) if isinstance(device, dict)]
    deleted_entities_list = [
        entity for entity in (validated_deleted_entities(entity_data) if entity_data is not None else []) if isinstance(entity, dict)
    ]
    active_entities = [
        entity for entity in (validated_entities(entity_data) if entity_data is not None else []) if isinstance(entity, dict)
    ]
    areas = area_names_from_data(area_data) if area_data is not None else {}
    row_by_id = {
        str(row.get("id")): row
        for row in rows or []
        if isinstance(row, dict) and row.get("id") and row.get("kind") != "deleted_entity"
    }
    enrichment_result = sanitize_deleted_devices_enrichment(enrichment)
    enrichment_by_id = enrichment_result["by_id"]
    warnings = list(enrichment_result["warnings"])
    device_displays = {}
    candidates_by_device_id = {}
    for device in deleted:
        device_id = _text_value(device.get("id"))
        display = _device_display(device, areas, row_by_id=row_by_id, enrichment_by_id=enrichment_by_id)
        device_displays[device_id] = display
        candidates_by_device_id[device_id] = _device_entity_prefix_candidates(device, display)
    deleted_entity_groups = {}
    for entity in deleted_entities_list:
        device_id = _text_value(entity.get("device_id"))
        if not device_id:
            device_id = _infer_deleted_entity_device_id(entity, candidates_by_device_id)
        deleted_entity_groups.setdefault(device_id, []).append(entity)
    active_entity_groups = {}
    for entity in active_entities:
        active_entity_groups.setdefault(_text_value(entity.get("device_id")), []).append(entity)
    device_groups = []
    deleted_device_ids = set()
    for device in deleted:
        device_id = _text_value(device.get("id"))
        deleted_device_ids.add(device_id)
        related_deleted = deleted_entity_groups.pop(device_id, [])
        related_active = active_entity_groups.get(device_id, [])
        display = device_displays.get(device_id) or _device_display(device, areas, row_by_id=row_by_id, enrichment_by_id=enrichment_by_id)
        device_groups.append(
            {
                "device": display,
                "deleted_entities": [_entity_summary(entity) for entity in related_deleted],
                "active_entities": [_entity_summary(entity) for entity in related_active],
                "counts": {
                    "deleted_entities": len(related_deleted),
                    "active_entities": len(related_active),
                },
            }
        )
    synthetic_groups = []
    regrouped_deleted_entity_groups = {}
    for device_id, entities in deleted_entity_groups.items():
        if device_id:
            regrouped_deleted_entity_groups[device_id] = entities
            continue
        groups, remaining = _synthetic_deleted_entity_groups(entities)
        synthetic_groups.extend(groups)
        if remaining:
            regrouped_deleted_entity_groups[device_id] = remaining
    deleted_entity_groups = regrouped_deleted_entity_groups
    device_groups.extend(synthetic_groups)
    orphan_entities = []
    for device_id, entities in sorted(deleted_entity_groups.items()):
        if device_id and device_id in deleted_device_ids:
            continue
        orphan_entities.append(
            {
                "device_id": device_id,
                "label": device_id or _("label.deleted_entities"),
                "deleted_entities": [_entity_summary(entity) for entity in entities],
                "counts": {"deleted_entities": len(entities)},
            }
        )
    return {
        "schema": 1,
        "generated_from": generated_from,
        "device_groups": device_groups,
        "orphan_entity_groups": orphan_entities,
        "counts": {
            "devices": len(device_groups),
            "deleted_entities": len(deleted_entities_list),
            "active_entities": len(active_entities),
            "orphan_deleted_entities": sum(group["counts"]["deleted_entities"] for group in orphan_entities),
        },
        "warnings": warnings,
    }


def build_deleted_devices_enrichment_from_tree(tree):
    devices = []
    for group in (tree or {}).get("device_groups") or []:
        device = group.get("device") if isinstance(group, dict) else None
        if not isinstance(device, dict) or not device.get("id"):
            continue
        devices.append(
            {
                "id": str(device.get("id")),
                "recovered_name": _text_value(device.get("name") or device.get("label")),
                "manufacturer": _text_value(device.get("manufacturer")),
                "model": _text_value(device.get("model")),
                "model_id": _text_value(device.get("model_id")),
                "identifiers": _identifiers_display(device.get("identifiers")),
                "source_commit": _text_value(device.get("source_commit"))[:40],
                "source_path": _text_value(device.get("source_path"))[:240],
            }
        )
        if len(devices) >= 200:
            break
    return {"schema": 1, "devices": devices}


def sanitize_deleted_devices_enrichment(enrichment):
    warnings = []
    if not enrichment:
        return {"by_id": {}, "warnings": warnings}
    if not isinstance(enrichment, dict) or enrichment.get("schema") != 1:
        return {"by_id": {}, "warnings": [_("warning.deleted_devices_enrichment_ignored")]}
    manifest_warnings = enrichment.get("warnings")
    if isinstance(manifest_warnings, list):
        warnings.extend(str(warning) for warning in manifest_warnings if warning)
    devices = enrichment.get("devices")
    if not isinstance(devices, list) or len(devices) > 200:
        return {"by_id": {}, "warnings": [_("warning.deleted_devices_enrichment_ignored")]}
    by_id = {}
    for item in devices:
        if not isinstance(item, dict) or not item.get("id"):
            warnings.append(_("warning.deleted_devices_enrichment_ignored"))
            continue
        by_id[str(item["id"])] = {
            "recovered_name": _text_value(item.get("recovered_name"))[:160],
            "manufacturer": _text_value(item.get("manufacturer"))[:120],
            "model": _text_value(item.get("model"))[:120],
            "model_id": _text_value(item.get("model_id"))[:120],
            "identifiers": _identifiers_display(item.get("identifiers")),
            "source_commit": _text_value(item.get("source_commit"))[:40],
            "source_path": _text_value(item.get("source_path"))[:240],
        }
    return {"by_id": by_id, "warnings": sorted(set(warnings))}


def build_deleted_devices_preview(config_dir, history_context=None):
    _path, text, data = read_validated_device_registry(config_dir)
    _entity_path, entity_text, entity_data = read_validated_entity_registry(config_dir)
    _area_path, _area_text, area_data = read_validated_area_registry(config_dir)
    devices = validated_deleted_devices(data)
    entities = validated_deleted_entities(entity_data) if entity_data is not None else []
    total = len(devices) + len(entities)
    label = deleted_entries_label(len(devices), len(entities))
    lines = [_("preview.deleted_entries_title", entries=label, count=total)]
    if devices:
        lines.append(_("preview.deleted_devices_title", count=len(devices)))
        lines.extend(f"- {deleted_device_label(device)}" for device in devices)
    if entities:
        lines.append(_("preview.deleted_entities_title", count=len(entities)))
        lines.extend(f"- {entity.get('entity_id') or entity.get('id') or json.dumps(entity, ensure_ascii=False, sort_keys=True)}" for entity in entities)
    if not total:
        lines.append(_("preview.deleted_entries_none"))
    rows = [
        *enrich_deleted_device_rows_from_history(deleted_device_rows(config_dir, devices), devices, history_context),
        *deleted_entity_rows(config_dir, entities),
    ]
    tree = build_deleted_devices_tree_from_data(data, entity_data, area_data, generated_from="preview", rows=rows)
    return {
        "count": total,
        "device_count": len(devices),
        "entity_count": len(entities),
        "fingerprint": registry_fingerprint(text, entity_text),
        "summary": "\n".join(lines),
        "rows": rows,
        "tree": tree,
        "enrichment": build_deleted_devices_enrichment_from_tree(tree),
    }


def device_registry_fingerprint(config_dir):
    _path, text, _data = read_validated_device_registry(config_dir)
    _entity_path, entity_text, _entity_data = read_validated_entity_registry(config_dir)
    return registry_fingerprint(text, entity_text)


def clear_deleted_devices(config_dir, expected_fingerprint):
    path, text, data = read_validated_device_registry(config_dir)
    entity_path, entity_text, entity_data = read_validated_entity_registry(config_dir)
    current_fingerprint = registry_fingerprint(text, entity_text)
    if expected_fingerprint and current_fingerprint != expected_fingerprint:
        raise RuntimeError("Deleted devices changed since preview. Run Check deleted devices again.")

    devices = validated_deleted_devices(data)
    entities = validated_deleted_entities(entity_data) if entity_data is not None else []
    removed = len(devices) + len(entities)
    data.setdefault("data", {})["deleted_devices"] = []
    device_temp = path.with_name(f".{path.name}.deleted-entries.tmp")
    device_restore_temp = path.with_name(f".{path.name}.deleted-entries.restore.tmp")
    entity_temp = entity_path.with_name(f".{entity_path.name}.deleted-entries.tmp")
    device_replaced = False
    try:
        device_temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if entity_text is not None:
            entity_data.setdefault("data", {})["deleted_entities"] = []
            entity_temp.write_text(json.dumps(entity_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            device_restore_temp.write_text(text, encoding="utf-8")
        os.replace(device_temp, path)
        device_replaced = True
        if entity_text is not None:
            try:
                os.replace(entity_temp, entity_path)
            except Exception as entity_error:
                try:
                    os.replace(device_restore_temp, path)
                except Exception as restore_error:
                    error = RuntimeError(
                        f"Failed to update deleted entities and restore deleted devices: {entity_error}; {restore_error}"
                    )
                    error.manual_recovery = True
                    raise error from entity_error
                raise RuntimeError(
                    f"Failed to update deleted entities; restored deleted devices: {entity_error}"
                ) from entity_error
            entity_text = entity_path.read_text(encoding="utf-8")
    finally:
        for temp in (device_temp, device_restore_temp, entity_temp):
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
    return {
        "removed": removed,
        "removed_devices": len(devices),
        "removed_entities": len(entities),
        "fingerprint": registry_fingerprint(path.read_text(encoding="utf-8"), entity_text),
    }
