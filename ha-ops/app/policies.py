EXPORT_EXCLUDES = [
    ".cloud/",
    ".cache/",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage/",
    ".vscode/",
    "__pycache__/",
    "backups/",
    "deps/",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    "*.pyc",
    "*.pyo",
    ".tmp-*",
    "node_modules",
    "node_modules/",
    "tts/",
    "www/community/",
    "www/media/",
    "www/tmp/",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]

STORAGE_EXPORT_ALLOWLIST = [
    "core.area_registry",
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.floor_registry",
    "core.label_registry",
    "core.logger",
    "core.uuid",
    "counter",
    "energy",
    "frontend_theme",
    "homeassistant.exposed_entities",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "lovelace",
    "lovelace.lovelace",
    "lovelace.map",
    "lovelace_dashboards",
    "lovelace_resources",
    "person",
    "schedule",
    "scene",
    "script",
    "tag",
    "timer",
    "zone",
]

PROTECTED_STORAGE_FILES = {
    "core.config",
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
    "core.uuid",
    "person",
}

DEFAULT_BACKUP_MAX_AGE_HOURS = 24
DEFAULT_MAX_APPLY_DELETIONS = 25
DEFAULT_RELEASE_KEEP_COUNT = 5
DEFAULT_RELEASE_KEEP_DAYS = 7

HOMEASSISTANT_EXPORT_ROOT_PATTERNS = ["*.yaml", "*.yml"]
HOMEASSISTANT_EXPORT_ROOT_EXCLUDES = {"secrets.yaml"}
HOMEASSISTANT_EXPORT_DIRS = [
    "blueprints",
    "custom_templates",
    "dashboards",
    "packages",
    "templates",
    "themes",
    "ui_lovelace_minimalist",
]
ZIGBEE2MQTT_CONFIG_PATHS = [
    "zigbee2mqtt/configuration.yaml",
    "zigbee2mqtt/external_converters",
    "zigbee2mqtt/scripts",
]

EXPORT_CLEAN_PATHS = [
    ".cloud",
    ".cache",
    ".DS_Store",
    ".google.token",
    ".ha_run.lock",
    ".storage",
    ".vscode",
    "backups",
    "deps",
    "home-assistant.log*",
    "home-assistant_v2.db*",
    "*.db",
    "*.db-*",
    "*.log",
    ".tmp-*",
    "node_modules",
    "tts",
    "www/community",
    "www/media",
    "www/tmp",
    "zigbee2mqtt/coordinator_backup*.json",
    "zigbee2mqtt/database.db*",
    "zigbee2mqtt/state.json",
]
EXPORT_CLEAN_DIR_NAMES = {"__pycache__", "node_modules"}
EXPORT_CLEAN_FILE_PATTERNS = ["*.pyc", "*.pyo"]


def bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def option_bool(options, name, default):
    return bool_value(options.get(name, default))


def option_int(options, name, default, minimum=0):
    try:
        value = int(options.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def bool_option(values, name, default):
    return bool_value(values.get(name, default))


def policy_bool(values, name, default, legacy_names=()):
    if name in values:
        return bool_value(values.get(name))
    for legacy_name in legacy_names:
        if legacy_name in values:
            return bool_value(values.get(legacy_name))
    return default


def policy_bool_with_options(values, options, name, default, legacy_names=()):
    if name in values:
        return bool_value(values.get(name))
    if name in options:
        return bool_value(options.get(name))
    for legacy_name in legacy_names:
        if legacy_name in values:
            return bool_value(values.get(legacy_name))
        if legacy_name in options:
            return bool_value(options.get(legacy_name))
    return default
