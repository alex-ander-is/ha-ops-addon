"""Fail-closed Docker build-cache capability checks for HA Ops."""


AVAILABLE = "available"
PROTECTION_ENABLED = "protection_enabled"
DOCKER_API_UNAVAILABLE = "docker_api_unavailable"
RUNTIME_SOCKET_UNAVAILABLE = "runtime_socket_unavailable"
UNKNOWN = "unknown"


def classify_self_info(payload):
    """Classify the two Supervisor-granted capabilities required by Docker.

    Supervisor responses are intentionally treated as untrusted at this
    boundary: missing or non-boolean fields must never enable a destructive
    action.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return UNKNOWN
    protected = data.get("protected")
    docker_api = data.get("docker_api")
    if type(protected) is not bool or type(docker_api) is not bool:
        return UNKNOWN
    if protected:
        return PROTECTION_ENABLED
    if not docker_api:
        return DOCKER_API_UNAVAILABLE
    return AVAILABLE


def details(kind, translate):
    """Return catalogued user-facing reason/remedy for a classifier result."""
    key = {
        PROTECTION_ENABLED: "docker_capability.protection_enabled",
        DOCKER_API_UNAVAILABLE: "docker_capability.docker_api_unavailable",
        RUNTIME_SOCKET_UNAVAILABLE: "docker_capability.runtime_socket_unavailable",
        UNKNOWN: "docker_capability.unknown",
    }.get(kind)
    if key is None:
        return {"available": True, "reason": "", "remedy": ""}
    return {
        "available": False,
        "reason": translate(f"{key}.reason"),
        "remedy": translate(f"{key}.remedy"),
    }
