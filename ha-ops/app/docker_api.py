import http.client
import json
import socket
from pathlib import Path
from urllib.parse import urlencode


MIN_API = (1, 39)
MAX_API = (1, 52)
PREFERRED_API = (1, 41)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class DockerAPIError(RuntimeError):
    pass


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = Path(socket_path)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self.sock = sock


def parse_api_version(value):
    if not isinstance(value, str):
        raise DockerAPIError("Docker API version is malformed.")
    parts = value.split(".")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise DockerAPIError("Docker API version is malformed.")
    return tuple(int(part) for part in parts)


def format_api_version(version):
    return f"{version[0]}.{version[1]}"


def select_api_version(api_version, min_api_version):
    daemon_max = parse_api_version(api_version)
    daemon_min = parse_api_version(min_api_version)
    if daemon_min > daemon_max:
        raise DockerAPIError("Docker API version range is malformed.")
    mutual_min = max(MIN_API, daemon_min)
    mutual_max = min(MAX_API, daemon_max)
    if mutual_min > mutual_max:
        raise DockerAPIError("Docker API 1.39-1.52 is not supported by this Docker daemon.")
    if mutual_min <= PREFERRED_API <= mutual_max:
        return PREFERRED_API
    legacy_max = min(mutual_max, (1, 51))
    if mutual_min <= legacy_max:
        return legacy_max
    return mutual_max


def _nonnegative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DockerAPIError(f"Docker field {name} is malformed.")
    return value


def build_cache_usage(payload, api_version):
    if not isinstance(payload, dict):
        raise DockerAPIError("Docker disk usage response is malformed.")
    if api_version == (1, 52):
        usage = payload.get("BuildCacheUsage")
        if not isinstance(usage, dict):
            raise DockerAPIError("Docker build cache usage is unavailable.")
        total = _nonnegative_int(usage.get("TotalSize"), "BuildCacheUsage.TotalSize")
        reclaimable = _nonnegative_int(usage.get("Reclaimable"), "BuildCacheUsage.Reclaimable")
        if reclaimable > total:
            raise DockerAPIError("Docker build cache usage is inconsistent.")
        return {"count": None, "size": total, "reclaimable": reclaimable}

    records = payload.get("BuildCache")
    if not isinstance(records, list):
        raise DockerAPIError("Docker build cache usage is unavailable.")
    total = 0
    protected = 0
    for record in records:
        if not isinstance(record, dict):
            raise DockerAPIError("Docker build cache record is malformed.")
        size = _nonnegative_int(record.get("Size"), "BuildCache.Size")
        in_use = record.get("InUse")
        shared = record.get("Shared")
        if not isinstance(in_use, bool) or not isinstance(shared, bool):
            raise DockerAPIError("Docker build cache flags are malformed.")
        total += size
        if in_use and not shared:
            protected += size
    return {"count": len(records), "size": total, "reclaimable": total - protected}


class DockerAPI:
    def __init__(self, socket_path=Path("/var/run/docker.sock"), timeout=5, max_body=MAX_RESPONSE_BYTES, connection_factory=None):
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.max_body = max_body
        self.connection_factory = connection_factory or _UnixSocketHTTPConnection
        self._version = None

    def _request(self, method, path, body=None):
        connection = self.connection_factory(self.socket_path, timeout=self.timeout)
        try:
            headers = {"Content-Length": str(len(body or b""))}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(self.max_body + 1)
            if len(raw) > self.max_body:
                raise DockerAPIError("Docker response is too large.")
            if response.status < 200 or response.status >= 300:
                raise DockerAPIError(f"Docker API returned HTTP {response.status}.")
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DockerAPIError("Docker API returned malformed JSON.") from exc
        finally:
            connection.close()

    def negotiate(self):
        payload = self._request("GET", "/version")
        if not isinstance(payload, dict):
            raise DockerAPIError("Docker version response is malformed.")
        self._version = select_api_version(payload.get("ApiVersion"), payload.get("MinAPIVersion"))
        return self._version

    def version(self):
        return self._version or self.negotiate()

    def disk_usage(self):
        version = self.version()
        payload = self._request("GET", f"/v{format_api_version(version)}/system/df")
        usage = build_cache_usage(payload, version)
        return payload, usage

    def prune_build_cache(self):
        version = self.version()
        path = f"/v{format_api_version(version)}/build/prune?{urlencode({'all': 'true'})}"
        payload = self._request("POST", path, body=b"")
        if not isinstance(payload, dict):
            raise DockerAPIError("Docker prune response is malformed.")
        reclaimed = _nonnegative_int(payload.get("SpaceReclaimed"), "SpaceReclaimed")
        caches = payload.get("CachesDeleted")
        if caches is not None and (not isinstance(caches, list) or any(not isinstance(item, str) for item in caches)):
            raise DockerAPIError("Docker prune response cache IDs are malformed.")
        return {"space_reclaimed": reclaimed, "caches_deleted": caches or []}
