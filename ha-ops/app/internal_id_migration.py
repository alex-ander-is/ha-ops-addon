import hashlib
import difflib
import json
import re
from pathlib import Path

import yaml


ZIGBEE_IEEE_RE = re.compile(r"0x[0-9a-fA-F]{16}")
Z2M_FILES = (
    "zigbee2mqtt/configuration.yaml",
    "zigbee2mqtt/state.json",
    "zigbee2mqtt/database.db",
)
Z2M_ADDON_FILES = (
    "configuration.yaml",
    "state.json",
    "database.db",
    *Z2M_FILES,
)


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def dump_yaml(data):
    return yaml.dump(data, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=1000)


def fingerprint_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_parts(parts):
    text = "\n".join(f"{name}\0{value}" for name, value in sorted(parts))
    return fingerprint_text(text)


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_registries(config_dir):
    storage = Path(config_dir) / ".storage"
    entities = load_json(storage / "core.entity_registry", {"data": {"entities": []}})
    devices = load_json(storage / "core.device_registry", {"data": {"devices": []}})
    return (
        {item["id"]: item for item in entities.get("data", {}).get("entities", []) if item.get("id")},
        {item["id"]: item for item in devices.get("data", {}).get("devices", []) if item.get("id")},
    )


def device_ieee(device):
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


def collect_z2m_names_from_value(value, names):
    if isinstance(value, dict):
        ieee = value.get("ieee_address") or value.get("ieeeAddr") or value.get("ieee")
        friendly = value.get("friendly_name") or value.get("friendlyName")
        if isinstance(ieee, str) and isinstance(friendly, str) and ZIGBEE_IEEE_RE.fullmatch(ieee):
            names[ieee.lower()] = friendly
        for key, child in value.items():
            if isinstance(key, str) and ZIGBEE_IEEE_RE.fullmatch(key):
                if isinstance(child, dict):
                    friendly = child.get("friendly_name") or child.get("friendlyName")
                else:
                    friendly = None
                if isinstance(friendly, str):
                    names[key.lower()] = friendly
            collect_z2m_names_from_value(child, names)
    elif isinstance(value, list):
        for child in value:
            collect_z2m_names_from_value(child, names)


def zigbee2mqtt_source_files(config_dir, z2m_dirs=None):
    seen = set()
    candidates = []
    root = Path(config_dir)
    candidates.extend(root / relative for relative in Z2M_FILES)
    for z2m_dir in z2m_dirs or []:
        z2m_root = Path(z2m_dir)
        candidates.extend(z2m_root / relative for relative in Z2M_ADDON_FILES)
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_file():
            yield path


def zigbee2mqtt_friendly_names(config_dir, z2m_dirs=None):
    names = {}
    for path in zigbee2mqtt_source_files(config_dir, z2m_dirs):
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = yaml.safe_load(text)
        except Exception:
            parsed = None
        collect_z2m_names_from_value(parsed, names)
    return names


def managed_files(config_dir):
    root = Path(config_dir) / ".ha-ops"
    if not root.exists():
        return []
    return [
        path
        for path in sorted(root.rglob("*.yaml"))
        if path.name in {"automations.yaml", "scripts.yaml", "scenes.yaml"}
    ]


def relative_path(config_dir, path):
    try:
        return Path(path).relative_to(config_dir).as_posix()
    except ValueError:
        return Path(path).as_posix()


class Migration:
    def __init__(self, config_dir, z2m_dirs=None):
        self.config_dir = Path(config_dir)
        self.entities, self.devices = load_registries(self.config_dir)
        self.z2m_names = zigbee2mqtt_friendly_names(self.config_dir, z2m_dirs)
        self.z2m_known_ieees = set(self.z2m_names)
        self.stats = {"mqtt_triggers": 0, "entity_triggers": 0, "actions": 0, "conditions": 0}
        self.unresolved = []

    def entity_name(self, registry_id):
        if not isinstance(registry_id, str):
            return None
        entity = self.entities.get(registry_id)
        return entity.get("entity_id") if entity else None

    def z2m_topic(self, device_id):
        if not isinstance(device_id, str):
            return None
        device = self.devices.get(device_id)
        if not device:
            return None
        ieee = device_ieee(device)
        if ieee and ieee not in self.z2m_known_ieees:
            return None
        name = self.z2m_names.get(ieee) if ieee else None
        name = name or device.get("name_by_user") or device.get("name")
        return f"z2m/{name}" if name else None

    def unresolved_reason(self, item):
        device = self.devices.get(item.get("device_id"))
        ieee = device_ieee(device) if device else None
        if ieee and ieee not in self.z2m_known_ieees:
            return "Zigbee2MQTT device is missing from current Zigbee2MQTT files; check retained devices first"
        return "unsupported device trigger"

    @staticmethod
    def trigger_marker(item):
        if item.get("trigger") == "device":
            return "trigger"
        if item.get("platform") == "device":
            return "platform"
        return "trigger"

    def mqtt_trigger(self, item):
        if item.get("domain") != "mqtt" or item.get("type") != "action":
            return None
        if "subtype" not in item:
            return None
        if item.get("trigger") != "device" and item.get("platform") != "device":
            return None
        topic = self.z2m_topic(item.get("device_id"))
        if not topic:
            return None
        marker = "trigger" if item.get("trigger") == "device" else "platform"
        return {"topic": topic, marker: "mqtt"}, item["subtype"]

    def state_trigger(self, item):
        entity_id = self.entity_name(item.get("entity_id"))
        if not entity_id:
            return None
        domain = item.get("domain")
        trigger_type = item.get("type")
        marker = self.trigger_marker(item)
        if domain in ("switch", "light") and trigger_type in ("turned_on", "turned_off"):
            return {"entity_id": [entity_id], "to": ["on" if trigger_type == "turned_on" else "off"], marker: "state"}
        binary_on = {"opened", "occupied", "turned_on", "smoke"}
        binary_types = binary_on | {"not_opened", "not_occupied"}
        if domain == "binary_sensor" and trigger_type in binary_types:
            return {"entity_id": [entity_id], "to": ["on" if trigger_type in binary_on else "off"], marker: "state"}
        numeric_types = {"temperature", "humidity", "illuminance", "power"}
        if domain == "sensor" and trigger_type in numeric_types and ("above" in item or "below" in item):
            output = {"entity_id": [entity_id], marker: "numeric_state"}
            if "above" in item:
                output["above"] = item["above"]
            if "below" in item:
                output["below"] = item["below"]
            return output
        return None

    def service_action(self, item):
        if not isinstance(item, dict) or "device_id" not in item or "type" not in item or "domain" not in item:
            return None
        entity_id = self.entity_name(item.get("entity_id"))
        if not entity_id:
            return None
        domain = item.get("domain")
        action_type = item.get("type")
        if domain == "switch" and action_type in ("turn_on", "turn_off", "toggle"):
            return {"action": f"switch.{action_type}", "target": {"entity_id": entity_id}}
        if domain == "cover" and action_type == "stop":
            return {"action": "cover.stop_cover", "target": {"entity_id": entity_id}, "data": {}}
        if domain == "cover" and action_type == "set_position":
            data = {}
            if "position" in item:
                data["position"] = item["position"]
            return {"action": "cover.set_cover_position", "target": {"entity_id": entity_id}, "data": data}
        return None

    def state_condition(self, item):
        if not isinstance(item, dict) or item.get("condition") != "device":
            return None
        entity_id = self.entity_name(item.get("entity_id"))
        if not entity_id:
            return None
        domain = item.get("domain")
        condition_type = item.get("type")
        if domain == "binary_sensor" and condition_type in ("is_open", "is_not_open", "is_occupied"):
            return {"condition": "state", "entity_id": entity_id, "state": "on" if condition_type in ("is_open", "is_occupied") else "off"}
        if domain == "switch" and condition_type in ("is_on", "is_off"):
            return {"condition": "state", "entity_id": entity_id, "state": "on" if condition_type == "is_on" else "off"}
        cover_states = {"is_open": "open", "is_closed": "closed", "is_opening": "opening", "is_closing": "closing"}
        if domain == "cover" and condition_type in cover_states:
            return {"condition": "state", "entity_id": entity_id, "state": cover_states[condition_type]}
        if domain == "sensor" and isinstance(condition_type, str) and condition_type.startswith("is_"):
            if "above" in item or "below" in item:
                output = {"condition": "numeric_state", "entity_id": entity_id}
                if "above" in item:
                    output["above"] = item["above"]
                if "below" in item:
                    output["below"] = item["below"]
                return output
        return None

    @staticmethod
    def payload_condition(subtypes, mixed_triggers):
        values = list(dict.fromkeys(subtypes))
        if len(values) == 1:
            action_expr = "trigger.payload_json.action == %r" % values[0]
        else:
            action_expr = "trigger.payload_json.action in %r" % values
        if mixed_triggers:
            action_expr = "trigger.platform != 'mqtt' or " + action_expr
        return {"condition": "template", "value_template": "{{ " + action_expr + " }}"}

    @staticmethod
    def condition_key(automation):
        if "conditions" in automation:
            return "conditions"
        if "condition" in automation:
            return "condition"
        return "conditions"

    def add_payload_condition(self, automation, subtypes, mixed_triggers):
        if not subtypes:
            return
        key = self.condition_key(automation)
        conditions = automation.get(key)
        payload = self.payload_condition(subtypes, mixed_triggers)
        if conditions in (None, []):
            automation[key] = [payload]
        elif isinstance(conditions, list):
            if payload not in conditions:
                conditions.insert(0, payload)
        else:
            automation[key] = [payload, conditions]

    def transform_trigger_list(self, triggers, path, alias):
        if not isinstance(triggers, list):
            return []
        subtypes = []
        for index, item in enumerate(list(triggers)):
            if not isinstance(item, dict):
                continue
            mqtt = self.mqtt_trigger(item)
            if mqtt:
                triggers[index], subtype = mqtt
                subtypes.append(subtype)
                self.stats["mqtt_triggers"] += 1
                continue
            state = self.state_trigger(item)
            if state:
                triggers[index] = state
                self.stats["entity_triggers"] += 1
                continue
            if "device_id" in item and (item.get("trigger") == "device" or item.get("platform") == "device"):
                self.unresolved.append(
                    {
                        "path": relative_path(self.config_dir, path),
                        "alias": alias,
                        "reason": self.unresolved_reason(item),
                        "item": item,
                        "yaml": dump_yaml(item).strip(),
                    }
                )
        return subtypes

    def transform_node(self, node):
        if isinstance(node, list):
            for index, value in enumerate(list(node)):
                if isinstance(value, dict):
                    action = self.service_action(value)
                    if action:
                        node[index] = action
                        self.stats["actions"] += 1
                        continue
                    condition = self.state_condition(value)
                    if condition:
                        node[index] = condition
                        self.stats["conditions"] += 1
                        continue
                self.transform_node(node[index])
        elif isinstance(node, dict):
            for value in list(node.values()):
                self.transform_node(value)

    def transform_automation(self, automation, path):
        alias = str(automation.get("alias", ""))
        for key in ("triggers", "trigger"):
            if key not in automation:
                continue
            subtypes = self.transform_trigger_list(automation[key], path, alias)
            triggers = automation.get(key) or []
            mixed_triggers = any(
                isinstance(item, dict) and item.get("trigger", item.get("platform")) != "mqtt"
                for item in triggers
            )
            self.add_payload_condition(automation, subtypes, mixed_triggers)
        for key, value in list(automation.items()):
            if key in ("triggers", "trigger"):
                continue
            self.transform_node(value)

    def migrate_data(self, data, path):
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self.transform_automation(item, path)
        else:
            self.transform_node(data)


def migrate_file(config_dir, path, z2m_dirs=None):
    migration = Migration(config_dir, z2m_dirs)
    original = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(original)
    migration.migrate_data(data, path)
    migrated = dump_yaml(data)
    changed = migrated != original
    return {
        "path": relative_path(config_dir, path),
        "changed": changed,
        "text": migrated,
        "original": original,
        "stats": migration.stats,
        "unresolved": migration.unresolved,
    }


def migration_diff(path, original, migrated):
    if original == migrated:
        return ""
    return "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            migrated.splitlines(),
            fromfile=f"{path} before internal id migration",
            tofile=f"{path} after internal id migration",
            lineterm="",
        )
    )


def build_internal_ids_preview(config_dir, z2m_dirs=None):
    config_dir = Path(config_dir)
    parts = []
    rows = []
    totals = {"mqtt_triggers": 0, "entity_triggers": 0, "actions": 0, "conditions": 0}
    unresolved = []
    for path in managed_files(config_dir):
        original = path.read_text(encoding="utf-8")
        parts.append((relative_path(config_dir, path), original))
        result = migrate_file(config_dir, path, z2m_dirs)
        changed_count = sum(result["stats"].values())
        diff = migration_diff(result["path"], result["original"], result["text"])
        if changed_count or result["unresolved"]:
            rows.append(
                {
                    "selected": bool(changed_count),
                    "path": result["path"],
                    "changes": changed_count,
                    "mqtt_triggers": result["stats"]["mqtt_triggers"],
                    "entity_triggers": result["stats"]["entity_triggers"],
                    "actions": result["stats"]["actions"],
                    "conditions": result["stats"]["conditions"],
                    "unresolved": len(result["unresolved"]),
                    "unresolved_items": result["unresolved"],
                    "diff": diff,
                }
            )
        for key in totals:
            totals[key] += result["stats"][key]
        unresolved.extend(result["unresolved"])
    for relative in (".storage/core.entity_registry", ".storage/core.device_registry", *Z2M_FILES):
        path = config_dir / relative
        if path.exists():
            parts.append((relative, path.read_text(encoding="utf-8", errors="replace")))
    for path in zigbee2mqtt_source_files(config_dir, z2m_dirs):
        parts.append((str(path), path.read_text(encoding="utf-8", errors="replace")))
    changed_files = [row for row in rows if row["changes"]]
    lines = [
        f"internal id migration candidates ({len(changed_files)} file(s)):",
        "This migrates HA Ops YAML only. It does not change live Home Assistant until the normal Git to HA apply flow.",
    ]
    if not changed_files:
        lines.append("No safe internal id migrations found.")
    if unresolved:
        lines.append(f"Unresolved unsupported device blocks: {len(unresolved)}.")
    return {
        "count": len(changed_files),
        "rows": rows,
        "totals": totals,
        "unresolved": unresolved,
        "fingerprint": fingerprint_parts(parts),
        "summary": "\n".join(lines),
    }


def apply_internal_ids_migration(config_dir, expected_fingerprint, selected_paths, z2m_dirs=None):
    preview = build_internal_ids_preview(config_dir, z2m_dirs)
    if expected_fingerprint and preview["fingerprint"] != expected_fingerprint:
        raise RuntimeError("Internal id migration candidates changed since preview. Run Check actions IDs again.")
    selected_paths = set(selected_paths or [])
    if not selected_paths:
        raise RuntimeError("Select at least one internal id migration file.")
    changed = []
    for row in preview["rows"]:
        if row["path"] not in selected_paths or not row["changes"]:
            continue
        if not row.get("diff"):
            raise RuntimeError(f"Internal id migration diff is missing for {row['path']}. Run Check actions IDs again.")
        path = Path(config_dir) / row["path"]
        result = migrate_file(config_dir, path, z2m_dirs)
        if result["changed"]:
            path.write_text(result["text"], encoding="utf-8")
            changed.append(row)
    return {
        "changed": changed,
        "changed_count": len(changed),
        "preview": build_internal_ids_preview(config_dir, z2m_dirs),
    }
