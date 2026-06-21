import http.client
import inspect
import json
import queue
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import i18n


def _(key, **values):
    return i18n.t(key, **values)


def format_size(size):
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(0, int(size)))
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def _parse_size(value):
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", text)
    if not match:
        raise ValueError(f"invalid size: {value}")
    number, unit = match.groups()
    multiplier = DF_SIZE_UNITS.get(unit.upper())
    if multiplier is None:
        raise ValueError(f"invalid size unit: {value}")
    return int(float(number) * multiplier)


def _dedupe_df_rows(rows):
    deduped = []
    seen = set()
    for row in rows:
        key = (row["filesystem"], row["total"], row["used"], row["available"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _parse_df_output(lines):
    rows = []
    for line in lines:
        if line.lower().startswith("filesystem "):
            continue
        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            continue
        filesystem, total, used, available, percent, mount = parts
        try:
            rows.append(
                {
                    "filesystem": filesystem,
                    "total": _parse_size(total),
                    "used": _parse_size(used),
                    "available": _parse_size(available),
                    "percent": percent,
                    "mount": mount,
                }
            )
        except ValueError:
            continue
    return _dedupe_df_rows(rows)


def _storage_totals(filesystems):
    return {
        "total": sum(row["total"] for row in filesystems),
        "used": sum(row["used"] for row in filesystems),
        "available": sum(row["available"] for row in filesystems),
    }


PATH_WALK_MAX_SECONDS = 2
PATH_WALK_MAX_ENTRIES = 5000
PATH_WALK_MAX_DEPTH = 8
OPTIONAL_COMMAND_TIMEOUT_SECONDS = 3

DF_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}

HOMEASSISTANT_DETAIL_GROUPS = (
    ("DB", ("home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal")),
    ("zigbee2mqtt", ("zigbee2mqtt",)),
    ("custom_components", ("custom_components",)),
    (".storage", (".storage",)),
    ("www", ("www",)),
    ("logs", ("logs", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault")),
)


class _PathWalkBudget:
    def __init__(
        self,
        max_seconds=PATH_WALK_MAX_SECONDS,
        max_entries=PATH_WALK_MAX_ENTRIES,
        max_depth=PATH_WALK_MAX_DEPTH,
    ):
        self.deadline = time.monotonic() + max_seconds
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.entries = 0
        self.hit_time_limit = False
        self.hit_entry_limit = False
        self.hit_depth_limit = False

    def allow(self, depth):
        if depth > self.max_depth:
            self.hit_depth_limit = True
            return False
        if self.entries >= self.max_entries:
            self.hit_entry_limit = True
            return False
        if time.monotonic() >= self.deadline:
            self.hit_time_limit = True
            return False
        self.entries += 1
        return True

    def partial_reason(self):
        reasons = []
        if self.hit_time_limit:
            reasons.append(_("label.time"))
        if self.hit_entry_limit:
            reasons.append(_("label.entries"))
        if self.hit_depth_limit:
            reasons.append(_("label.depth"))
        if not reasons:
            return None
        return ", ".join(reasons)


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self.sock = sock


def _path_size(path, budget, depth=0):
    if not budget.allow(depth):
        return 0
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    try:
        entries = path.iterdir()
    except OSError:
        return 0
    for entry in entries:
        total += _path_size(entry, budget, depth + 1)
        if budget.partial_reason():
            break
    return total


def _path_summary(
    path,
    limit=8,
    max_seconds=PATH_WALK_MAX_SECONDS,
    max_entries=PATH_WALK_MAX_ENTRIES,
    max_depth=PATH_WALK_MAX_DEPTH,
):
    try:
        if path.is_symlink():
            return path.lstat().st_size, [], None
        if path.is_file():
            return path.stat().st_size, [], None
        if not path.is_dir():
            return 0, [], None
    except OSError:
        return 0, [], None

    budget = _PathWalkBudget(max_seconds, max_entries, max_depth)
    sized = []
    total = 0
    try:
        entries = path.iterdir()
    except OSError:
        return 0, [], None
    for entry in entries:
        size = _path_size(entry, budget, 1)
        total += size
        sized.append((entry.name, size))
        if budget.partial_reason():
            break
    sized.sort(key=lambda item: item[1], reverse=True)
    return total, sized[:limit], budget.partial_reason()


def _filesystem_rows(paths, run_command, timeout):
    if not paths:
        return [], _("detail.disk_usage_no_mapped_paths")
    output, error = _command_lines(run_command, ["df", "-B1", "-P", *[str(path) for path in paths]], timeout)
    if output is None:
        return None, error
    rows = _parse_df_output(output)
    if not rows:
        return None, _("detail.disk_usage_filesystems_empty")
    return rows, None


def _summarize_path(
    title,
    path,
    limit=8,
    max_seconds=PATH_WALK_MAX_SECONDS,
    max_entries=PATH_WALK_MAX_ENTRIES,
    max_depth=PATH_WALK_MAX_DEPTH,
):
    path = Path(path)
    if not path.exists():
        return {
            "title": title,
            "path": path,
            "available": False,
            "size": 0,
            "entries": [],
            "partial_reason": None,
            "max_seconds": max_seconds,
            "max_entries": max_entries,
            "max_depth": max_depth,
        }
    total, entries, partial_reason = _path_summary(
        path,
        limit=limit,
        max_seconds=max_seconds,
        max_entries=max_entries,
        max_depth=max_depth,
    )
    return {
        "title": title,
        "path": path,
        "available": True,
        "size": total,
        "entries": entries,
        "partial_reason": partial_reason,
        "max_seconds": max_seconds,
        "max_entries": max_entries,
        "max_depth": max_depth,
    }


def _path_label(summary):
    return _("detail.disk_usage_path_label", title=summary["title"], path=summary["path"])


def _append_partial_line(lines, summary, indent="    "):
    if not summary["partial_reason"]:
        return
    lines.append(
        _(
            "detail.disk_usage_path_partial",
            indent=indent,
            entries=summary["max_entries"],
            seconds=summary["max_seconds"],
            depth=summary["max_depth"],
            reason=summary["partial_reason"],
        )
    )


def _append_app_data_details(lines, summaries):
    for summary in summaries:
        if not summary["available"]:
            lines.append(_("detail.disk_usage_tree_unavailable", indent="    ", title=_path_label(summary)))
            continue
        lines.append(_("detail.disk_usage_tree_item", indent="    ", title=_path_label(summary), size=format_size(summary["size"])))
        _append_partial_line(lines, summary, indent="      ")


def _homeassistant_detail_entries(summary):
    if not summary["available"]:
        return []
    entries = dict(summary["entries"])
    used_names = set()
    details = []
    for title, names in HOMEASSISTANT_DETAIL_GROUPS:
        size = 0
        matched = False
        for name in names:
            if name not in entries:
                continue
            matched = True
            used_names.add(name)
            size += entries[name]
        if matched:
            details.append((title, size))
    for name, size in summary["entries"]:
        if name in used_names:
            continue
        details.append((name, size))
        if len(details) >= 8:
            break
    return details


def _append_homeassistant_details(lines, summary):
    if not summary["available"]:
        lines.append(_("detail.disk_usage_tree_unavailable", indent="    ", title=_path_label(summary)))
        return
    _append_partial_line(lines, summary, indent="    ")
    for name, size in _homeassistant_detail_entries(summary):
        lines.append(_("detail.disk_usage_tree_item", indent="    ", title=name, size=format_size(size)))


def _append_filesystem_details(lines, filesystems):
    if not filesystems:
        return
    lines.append("")
    lines.append(_("detail.disk_usage_filesystems_deduped"))
    for row in filesystems:
        lines.append(
            _(
                "detail.disk_usage_filesystem_row",
                mount=row["mount"],
                filesystem=row["filesystem"],
                used=format_size(row["used"]),
                total=format_size(row["total"]),
                available=format_size(row["available"]),
            )
        )


def _call_with_timeout(func, args=(), kwargs=None, timeout=OPTIONAL_COMMAND_TIMEOUT_SECONDS):
    kwargs = kwargs or {}
    result_queue = queue.Queue(maxsize=1)

    def target():
        try:
            result_queue.put((True, func(*args, **kwargs)))
        except Exception as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(_("detail.disk_usage_section_timed_out", seconds=timeout))
    ok, result = result_queue.get_nowait()
    if ok:
        return result
    raise result


def _supports_timeout_argument(func):
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return "timeout" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )


def _run_command_with_timeout(run_command, command, timeout):
    try:
        if _supports_timeout_argument(run_command):
            return run_command(command, timeout=timeout)
        return _call_with_timeout(run_command, (command,), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(_("detail.disk_usage_section_timed_out", seconds=timeout)) from exc


def _call_supervisor_with_timeout(call_supervisor, method, path, timeout):
    try:
        if _supports_timeout_argument(call_supervisor):
            return call_supervisor(method, path, timeout=timeout)
        return _call_with_timeout(call_supervisor, (method, path), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(_("detail.disk_usage_section_timed_out", seconds=timeout)) from exc


def _command_lines(run_command, command, timeout=OPTIONAL_COMMAND_TIMEOUT_SECONDS):
    if run_command is None:
        return None, _("detail.disk_usage_command_unavailable")
    try:
        result = _run_command_with_timeout(run_command, command, timeout)
    except Exception as exc:
        return None, str(exc)
    output = "\n".join(part for part in (getattr(result, "stdout", ""), getattr(result, "stderr", "")) if part)
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if getattr(result, "returncode", 1) == 0:
        return lines, None
    return None, lines[0] if lines else _("detail.disk_usage_command_failed")


def _append_command_section(lines, title, command, run_command, max_lines=8, timeout=OPTIONAL_COMMAND_TIMEOUT_SECONDS):
    output, error = _command_lines(run_command, command, timeout)
    if output is None:
        lines.append(_("detail.disk_usage_optional_unavailable", title=title, error=error))
        return
    lines.append(_("detail.disk_usage_optional_title", title=title))
    lines.extend(output[:max_lines])


def _host_info_lines(call_supervisor, timeout=OPTIONAL_COMMAND_TIMEOUT_SECONDS):
    if call_supervisor is None:
        return None, _("detail.disk_usage_supervisor_unavailable")
    try:
        payload = _call_supervisor_with_timeout(call_supervisor, "GET", "/host/info", timeout)
    except Exception as exc:
        return None, str(exc)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return None, _("detail.disk_usage_supervisor_unavailable")
    values = []
    for key in ("disk_total", "disk_used", "disk_free", "disk_life_time"):
        if key in data:
            values.append(_("detail.disk_usage_host_field", name=key, value=data[key]))
    if not values:
        return None, _("detail.disk_usage_host_no_disk_fields")
    return values, None


def _docker_system_df(socket_path, timeout=5):
    if not socket_path.exists():
        return None, _("detail.disk_usage_docker_socket_unavailable", path=socket_path)
    connection = _UnixSocketHTTPConnection(socket_path, timeout=timeout)
    try:
        connection.request("GET", "/system/df")
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status >= 400:
        return None, _("detail.disk_usage_docker_api_failed", status=response.status)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    return payload, None


def _docker_ref(image):
    tags = image.get("RepoTags") or []
    if tags:
        return tags[0]
    return image.get("Id") or _("label.unknown")


def _docker_usage_lines(socket_path, docker_system_df=None, timeout=OPTIONAL_COMMAND_TIMEOUT_SECONDS):
    if docker_system_df is None:
        try:
            if _supports_timeout_argument(_docker_system_df):
                payload, error = _docker_system_df(socket_path, timeout=timeout)
            else:
                payload, error = _call_with_timeout(_docker_system_df, (socket_path,), timeout=timeout)
        except Exception as exc:
            payload, error = None, str(exc)
    else:
        try:
            payload = _call_with_timeout(docker_system_df, timeout=timeout)
            error = None
        except Exception as exc:
            payload, error = None, str(exc)
    if payload is None:
        return None, error
    try:
        if not isinstance(payload, dict):
            raise TypeError(_("detail.disk_usage_docker_payload_invalid"))

        lines = [_("detail.disk_usage_docker_layers", size=format_size(payload.get("LayersSize", 0)))]

        images = payload.get("Images") or []
        if not isinstance(images, list):
            raise TypeError(_("detail.disk_usage_docker_payload_invalid"))
        image_size = sum(int(item.get("Size") or 0) for item in images if isinstance(item, dict))
        lines.append(_("detail.disk_usage_docker_images", count=len(images), size=format_size(image_size)))
        for image in sorted((item for item in images if isinstance(item, dict)), key=lambda item: int(item.get("Size") or 0), reverse=True)[:5]:
            lines.append(_("detail.disk_usage_docker_item", name=_docker_ref(image), size=format_size(image.get("Size") or 0)))

        containers = payload.get("Containers") or []
        if not isinstance(containers, list):
            raise TypeError(_("detail.disk_usage_docker_payload_invalid"))
        writable_size = sum(int(item.get("SizeRw") or 0) for item in containers if isinstance(item, dict))
        lines.append(_("detail.disk_usage_docker_containers", count=len(containers), size=format_size(writable_size)))

        volumes = payload.get("Volumes") or []
        if not isinstance(volumes, list):
            raise TypeError(_("detail.disk_usage_docker_payload_invalid"))
        volumes_with_size = []
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            usage = volume.get("UsageData") or {}
            if not isinstance(usage, dict):
                usage = {}
            volumes_with_size.append((volume.get("Name") or _("label.unknown"), int(usage.get("Size") or 0)))
        volume_size = sum(size for _name, size in volumes_with_size)
        lines.append(_("detail.disk_usage_docker_volumes", count=len(volumes), size=format_size(volume_size)))
        for name, size in sorted(volumes_with_size, key=lambda item: item[1], reverse=True)[:5]:
            lines.append(_("detail.disk_usage_docker_item", name=name, size=format_size(size)))

        build_cache = payload.get("BuildCache") or []
        if not isinstance(build_cache, list):
            raise TypeError(_("detail.disk_usage_docker_payload_invalid"))
        build_cache_size = sum(int(item.get("Size") or 0) for item in build_cache if isinstance(item, dict))
        lines.append(_("detail.disk_usage_docker_build_cache", count=len(build_cache), size=format_size(build_cache_size)))
        return lines, None
    except Exception as exc:
        return None, str(exc)


def build_disk_usage_summary(
    config_dir,
    data_dir,
    addon_configs_dir,
    backup_dir=Path("/backup"),
    run_command=None,
    call_supervisor=None,
    docker_socket_path=Path("/var/run/docker.sock"),
    docker_system_df=None,
    optional_timeout_seconds=OPTIONAL_COMMAND_TIMEOUT_SECONDS,
    path_walk_max_seconds=PATH_WALK_MAX_SECONDS,
    path_walk_max_entries=PATH_WALK_MAX_ENTRIES,
    path_walk_max_depth=PATH_WALK_MAX_DEPTH,
):
    paths = [
        Path(config_dir),
        Path(data_dir),
        Path(addon_configs_dir),
        Path(backup_dir),
    ]
    existing_paths = []
    seen = set()
    for path in paths:
        if path.exists() and path not in seen:
            existing_paths.append(path)
            seen.add(path)

    lines = [_("detail.disk_usage_summary_title")]
    filesystems, filesystem_error = _filesystem_rows(existing_paths, run_command, optional_timeout_seconds)
    storage_totals = _storage_totals(filesystems) if filesystems else None

    homeassistant_summary = _summarize_path(
        _("label.homeassistant_config"),
        Path(config_dir),
        limit=64,
        max_seconds=path_walk_max_seconds,
        max_entries=path_walk_max_entries,
        max_depth=path_walk_max_depth,
    )
    app_summaries = [
        _summarize_path(
            _("label.ha_ops_data"),
            Path(data_dir),
            max_seconds=path_walk_max_seconds,
            max_entries=path_walk_max_entries,
            max_depth=path_walk_max_depth,
        ),
        _summarize_path(
            _("label.addon_configs"),
            Path(addon_configs_dir),
            max_seconds=path_walk_max_seconds,
            max_entries=path_walk_max_entries,
            max_depth=path_walk_max_depth,
        ),
        _summarize_path(
            _("label.backups"),
            Path(backup_dir),
            max_seconds=path_walk_max_seconds,
            max_entries=path_walk_max_entries,
            max_depth=path_walk_max_depth,
        ),
    ]
    app_size = sum(summary["size"] for summary in app_summaries if summary["available"])
    homeassistant_size = homeassistant_summary["size"] if homeassistant_summary["available"] else 0

    if storage_totals:
        system_size = max(0, storage_totals["used"] - app_size - homeassistant_size)
        lines.append(
            _(
                "detail.disk_usage_storage_title",
                used=format_size(storage_totals["used"]),
                total=format_size(storage_totals["total"]),
            )
        )
        lines.append(_("detail.disk_usage_tree_item", indent="  ", title=_("label.system"), size=format_size(system_size)))
        lines.append(_("detail.disk_usage_system_partial"))
    else:
        lines.append(_("detail.disk_usage_storage_unavailable", error=filesystem_error))
        lines.append(_("detail.disk_usage_tree_unavailable", indent="  ", title=_("label.system")))

    lines.append(_("detail.disk_usage_tree_item", indent="  ", title=_("label.app_data"), size=format_size(app_size)))
    _append_app_data_details(lines, app_summaries)
    lines.append(
        _("detail.disk_usage_tree_item", indent="  ", title=_("label.homeassistant"), size=format_size(homeassistant_size))
    )
    _append_homeassistant_details(lines, homeassistant_summary)
    if storage_totals:
        lines.append(
            _(
                "detail.disk_usage_tree_item",
                indent="  ",
                title=_("label.free_space"),
                size=format_size(storage_totals["available"]),
            )
        )
    else:
        lines.append(_("detail.disk_usage_tree_unavailable", indent="  ", title=_("label.free_space")))

    _append_filesystem_details(lines, filesystems or [])

    lines.append("")
    lines.append(_("detail.disk_usage_optional_diagnostics"))

    host_output, host_error = _host_info_lines(call_supervisor, optional_timeout_seconds)
    if host_output is None:
        lines.append(_("detail.disk_usage_optional_unavailable", title=_("label.host"), error=host_error))
    else:
        lines.append(_("detail.disk_usage_optional_title", title=_("label.host")))
        lines.extend(host_output)

    docker_output, docker_error = _docker_usage_lines(Path(docker_socket_path), docker_system_df, optional_timeout_seconds)
    if docker_output is None:
        lines.append(_("detail.disk_usage_optional_unavailable", title=_("label.docker"), error=docker_error))
    else:
        lines.append(_("detail.disk_usage_optional_title", title=_("label.docker")))
        lines.extend(docker_output)

    _append_command_section(
        lines,
        _("label.system_journal"),
        ["journalctl", "--disk-usage"],
        run_command,
        timeout=optional_timeout_seconds,
    )
    return lines
