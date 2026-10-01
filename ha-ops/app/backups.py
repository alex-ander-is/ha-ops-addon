from datetime import datetime, timezone


class FreshBackupRequired(RuntimeError):
    """A completed policy check found no eligible fresh system backup."""

    def __init__(self, max_age_hours, detail):
        self.max_age_hours = max_age_hours
        super().__init__(f"No fresh system backup found within {max_age_hours} hour(s): {detail}")


def parse_backup_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def backup_slug(backup):
    return backup.get("slug") or backup.get("id")


def backup_name(backup):
    return backup.get("name") or backup_slug(backup) or "unknown backup"


def backup_locations(backup):
    """Validate locations before counting them, including legacy local storage."""
    def valid_location(value):
        # Supervisor uses null for /backup in both location and locations.
        return value is None or isinstance(value, str) and bool(value.strip())

    has_legacy = "location" in backup
    legacy = backup.get("location")
    if has_legacy and not valid_location(legacy):
        raise ValueError("Backup location is invalid.")
    if "locations" not in backup:
        return 1 if has_legacy else None

    locations = backup["locations"]
    if isinstance(locations, list):
        if not all(valid_location(location) for location in locations):
            raise ValueError("Backup locations contain an invalid entry.")
        if len(set(locations)) != len(locations):
            raise ValueError("Backup locations contain duplicate entries.")
        if has_legacy and legacy not in locations:
            raise ValueError("Backup location contradicts locations.")
    elif isinstance(locations, dict):
        # Retain the existing keyed location-metadata representation.
        if not all(isinstance(key, str) and key.strip() and isinstance(value, dict)
                   for key, value in locations.items()):
            raise ValueError("Backup location metadata is invalid.")
        if has_legacy and (legacy if legacy is not None else ".local") not in locations:
            raise ValueError("Backup location contradicts locations.")
    elif type(locations) is int and locations >= 0:
        if has_legacy and locations == 0:
            raise ValueError("Backup location contradicts locations.")
        return locations
    else:
        raise ValueError("Backup locations are invalid.")
    return len(locations)


def backup_has_location(backup):
    locations = backup_locations(backup)
    return locations is not None and locations > 0


def is_system_backup(backup):
    """Classify positively; unknown metadata must never mean no system backup."""
    backup_type = backup.get("type")
    if "type" in backup and (
        not isinstance(backup_type, str) or backup_type.lower() not in {"full", "partial", "automatic", "auto"}
    ):
        raise ValueError("Backup type is invalid or unknown.")
    content = backup.get("content", {})
    if not isinstance(content, dict):
        raise ValueError("Backup content is invalid.")
    has_homeassistant = content.get("homeassistant")
    if "homeassistant" in content and type(has_homeassistant) is not bool:
        raise ValueError("Backup Home Assistant content flag is invalid.")
    if isinstance(backup_type, str) and backup_type.lower() in {"full", "automatic", "auto"}:
        if has_homeassistant is False:
            raise ValueError("System backup type contradicts its content.")
        return True
    if has_homeassistant is not None:
        return has_homeassistant
    raise ValueError("Backup system content cannot be determined.")


def classify_backup(backup, require_location):
    """Validate every policy input before it can contribute to a refusal."""
    if not isinstance(backup, dict):
        raise ValueError("Backup entry is invalid.")
    if not any(key in backup for key in ("slug", "id")):
        raise ValueError("Backup identity is unavailable.")
    for key in ("slug", "id"):
        if key in backup and (not isinstance(backup[key], str) or not backup[key].strip()):
            raise ValueError("Backup identity is invalid.")
    system = is_system_backup(backup)
    date = parse_backup_date(backup.get("date"))
    if date is None or date.tzinfo is None:
        raise ValueError("Backup date is invalid or has no timezone.")
    locations = backup_locations(backup)
    if require_location and (locations is None or locations == 0):
        raise ValueError("Backup has no verified storage location.")
    return system, date


def backup_age_hours(backup_date):
    return max(0, int(backup_age_seconds(backup_date) // 3600))


def backup_age_seconds(backup_date):
    now = datetime.now(timezone.utc)
    return max(0, int((now - backup_date.astimezone(timezone.utc)).total_seconds()))


def backup_status_message(backup, backup_date):
    age_hours = backup_age_hours(backup_date)
    locations = backup_locations(backup)
    location_text = f", {locations} location(s)" if locations is not None else ""
    return f"{backup_name(backup)} at {backup.get('date')} ({age_hours} hour(s) ago{location_text})."


def find_backup_by_slug(backups, slug):
    for backup in backups:
        if backup_slug(backup) == slug:
            return backup
    return None


def classify_backup_inventory(info, require_location):
    """Validate the entire inventory before selecting any backup from it."""
    backup_items = info.get("backups") if isinstance(info, dict) else None
    if not isinstance(backup_items, list):
        raise ValueError("Backup list is unavailable or invalid.")
    classified = {}
    seen = set()
    for backup in backup_items:
        system, date = classify_backup(backup, require_location)
        # Both supported identity fields must remain unambiguous across entries,
        # even when an entry supplies a slug as well as a fallback id.
        identities = {backup[key] for key in ("slug", "id") if key in backup}
        if identities & seen:
            raise ValueError("Backup inventory contains duplicate identities.")
        seen.update(identities)
        classified[backup_slug(backup)] = (system, date, backup)
    return classified


def latest_system_backup_status(options, max_age_default, option_int, option_bool, backup_manager_info):
    max_age_hours = option_int(options, "backup_max_age_hours", max_age_default, minimum=1)
    require_location = option_bool(options, "backup_require_location", True)
    try:
        # A filtered list cannot prove absence/staleness: a discarded entry may
        # be the fresh system backup. Classify the entire inventory first.
        inventory = classify_backup_inventory(backup_manager_info(), require_location)
        dated_backups = [(date, backup) for system, date, backup in inventory.values() if system]
        if not dated_backups:
            return {
                "available": True,
                "refusal_reason": "missing",
                "message": "No system Home Assistant backups found.",
                "stale": True,
                "backup": None,
                "age_hours": None,
                "max_age_hours": max_age_hours,
                "require_location": require_location,
            }

        latest_date, latest = max(dated_backups, key=lambda item: item[0])
        age_hours = backup_age_hours(latest_date)
        stale = backup_age_seconds(latest_date) > max_age_hours * 3600
        return {
            "available": True,
            "refusal_reason": "stale" if stale else None,
            "message": backup_status_message(latest, latest_date),
            "stale": stale,
            "backup": latest,
            "age_hours": age_hours,
            "max_age_hours": max_age_hours,
            "require_location": require_location,
        }
    except Exception as exc:
        return {
            "available": False,
            "refusal_reason": None,
            "message": f"Backup status unavailable: {exc}",
            "stale": True,
            "backup": None,
            "age_hours": None,
            "max_age_hours": max_age_hours,
            "require_location": require_location,
        }


def ensure_fresh_system_backup(
    options,
    details,
    option_bool,
    add_detail,
    latest_system_backup_status,
    default_backup_mount,
    create_ha_backup,
    backup_manager_info,
):
    if not option_bool(options, "require_fresh_backup", True):
        add_detail(details, "Fresh system backup requirement is disabled.")
        return None

    status = latest_system_backup_status(options)
    if not status["stale"]:
        backup = status.get("backup") or {}
        add_detail(details, f"Fresh system backup found: {status['message']}")
        return backup_slug(backup)

    if not option_bool(options, "create_ha_backup", True):
        if status.get("available") is True and status.get("refusal_reason") in {"missing", "stale"}:
            raise FreshBackupRequired(status["max_age_hours"], status["message"])
        raise RuntimeError(status["message"])

    backup_location = default_backup_mount() if option_bool(options, "backup_require_location", True) else None
    if option_bool(options, "backup_require_location", True) and not backup_location:
        raise RuntimeError("No default backup location is configured. Configure Store in NAS or disable backup_require_location.")

    add_detail(details, f"No fresh system backup found within {status['max_age_hours']} hour(s). Creating full system backup.")
    slug = create_ha_backup(options.get("ha_backup_name_prefix", "ha-ops"), backup_location=backup_location)
    info = backup_manager_info()
    try:
        inventory = classify_backup_inventory(info, option_bool(options, "backup_require_location", True))
    except ValueError as exc:
        raise RuntimeError(f"Created backup {slug}, but its inventory is invalid: {exc}") from exc
    if slug not in inventory:
        raise RuntimeError(f"Created backup {slug}, but it is not visible in Home Assistant backups.")
    system, backup_date, backup = inventory[slug]
    if not system:
        raise RuntimeError(f"Created backup {slug}, but it does not contain Home Assistant.")
    if backup_age_seconds(backup_date) > status["max_age_hours"] * 3600:
        # Creation failures remain ordinary errors, never an acknowledgement
        # opportunity. A returned slug can still refer to an old backup.
        raise RuntimeError(f"Created backup {slug}, but it is older than {status['max_age_hours']} hour(s).")
    add_detail(details, f"Created fresh system backup: {backup_status_message(backup, backup_date)}")
    return slug
