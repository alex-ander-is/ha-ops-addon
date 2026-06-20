import http.client
import inspect
import json
import queue
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


PATH_WALK_MAX_SECONDS = 2
PATH_WALK_MAX_ENTRIES = 5000
PATH_WALK_MAX_DEPTH = 8
OPTIONAL_COMMAND_TIMEOUT_SECONDS = 3


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


def _docker_usage_lines(socket_path, docker_system_df=None):
    if docker_system_df is None:
        try:
            payload, error = _docker_system_df(socket_path)
        except Exception as exc:
            payload, error = None, str(exc)
    else:
        try:
            payload = docker_system_df()
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


def _append_path_section(
    lines,
    title,
    path,
    max_seconds=PATH_WALK_MAX_SECONDS,
    max_entries=PATH_WALK_MAX_ENTRIES,
    max_depth=PATH_WALK_MAX_DEPTH,
):
    if not path.exists():
        lines.append(_("detail.disk_usage_path_unavailable", title=title, path=path))
        return
    total, top_entries, partial_reason = _path_summary(
        path,
        max_seconds=max_seconds,
        max_entries=max_entries,
        max_depth=max_depth,
    )
    lines.append(_("detail.disk_usage_path_total", title=title, path=path, size=format_size(total)))
    if partial_reason:
        lines.append(
            _(
                "detail.disk_usage_path_partial",
                entries=max_entries,
                seconds=max_seconds,
                depth=max_depth,
                reason=partial_reason,
            )
        )
    for name, size in top_entries:
        lines.append(_("detail.disk_usage_path_entry", name=name, size=format_size(size)))


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
    if existing_paths:
        output, error = _command_lines(
            run_command,
            ["df", "-h", *[str(path) for path in existing_paths]],
            optional_timeout_seconds,
        )
        if output:
            lines.append(_("detail.disk_usage_filesystems"))
            lines.extend(output)
        else:
            lines.append(_("detail.disk_usage_filesystems_unavailable", error=error))
    else:
        lines.append(_("detail.disk_usage_no_mapped_paths"))

    lines.append("")
    lines.append(_("detail.disk_usage_mapped_paths"))
    for title, path in (
        (_("label.homeassistant_config"), Path(config_dir)),
        (_("label.ha_ops_data"), Path(data_dir)),
        (_("label.addon_configs"), Path(addon_configs_dir)),
        (_("label.backups"), Path(backup_dir)),
    ):
        _append_path_section(
            lines,
            title,
            path,
            path_walk_max_seconds,
            path_walk_max_entries,
            path_walk_max_depth,
        )

    lines.append("")
    lines.append(_("detail.disk_usage_optional_diagnostics"))

    host_output, host_error = _host_info_lines(call_supervisor, optional_timeout_seconds)
    if host_output is None:
        lines.append(_("detail.disk_usage_optional_unavailable", title=_("label.host"), error=host_error))
    else:
        lines.append(_("detail.disk_usage_optional_title", title=_("label.host")))
        lines.extend(host_output)

    docker_output, docker_error = _docker_usage_lines(Path(docker_socket_path), docker_system_df)
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
