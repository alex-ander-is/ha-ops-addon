from datetime import datetime, timezone


def parse_backup_date(value):
    if not value:
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
    locations = backup.get("locations")
    if isinstance(locations, list):
        return len(locations)
    location = backup.get("location")
    if location:
        return 1
    return None


def backup_has_location(backup):
    locations = backup_locations(backup)
    return locations is not None and locations > 0


def is_system_backup(backup):
    backup_type = str(backup.get("type", "")).lower()
    return backup_type in {"full", "automatic", "auto"}


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


def latest_system_backup_status(options, max_age_default, option_int, option_bool, backup_manager_info):
    max_age_hours = option_int(options, "backup_max_age_hours", max_age_default, minimum=1)
    require_location = option_bool(options, "backup_require_location", True)
    try:
        info = backup_manager_info()
        backup_items = info.get("backups", [])
        dated_backups = [
            (parse_backup_date(backup.get("date")), backup)
            for backup in backup_items
            if is_system_backup(backup) and (not require_location or backup_has_location(backup))
        ]
        dated_backups = [(date, backup) for date, backup in dated_backups if date is not None]
        if not dated_backups:
            return {
                "available": True,
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
        raise RuntimeError(
            f"No fresh system backup found within {status['max_age_hours']} hour(s): {status['message']}"
        )

    backup_location = default_backup_mount() if option_bool(options, "backup_require_location", True) else None
    if option_bool(options, "backup_require_location", True) and not backup_location:
        raise RuntimeError("No default backup location is configured. Configure Store in NAS or disable backup_require_location.")

    add_detail(details, f"No fresh system backup found within {status['max_age_hours']} hour(s). Creating full system backup.")
    slug = create_ha_backup(options.get("ha_backup_name_prefix", "ha-ops"), backup_location=backup_location)
    info = backup_manager_info()
    backup = find_backup_by_slug(info.get("backups", []), slug)
    if not backup:
        raise RuntimeError(f"Created backup {slug}, but it is not visible in Home Assistant backups.")

    backup_date = parse_backup_date(backup.get("date"))
    if not backup_date:
        raise RuntimeError(f"Created backup {slug}, but its date is unavailable.")
    if option_bool(options, "backup_require_location", True) and not backup_has_location(backup):
        raise RuntimeError(f"Created backup {slug}, but it is not stored in a configured backup location.")
    add_detail(details, f"Created fresh system backup: {backup_status_message(backup, backup_date)}")
    return slug
