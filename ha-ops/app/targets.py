from typing import Any, Mapping, Optional, TypedDict


class Target(TypedDict, total=False):
    id: str
    type: str
    source: str
    addon_slug: str
    addon_slug_suffix: str
    addon_name_contains: str
    delete: bool
    save_delete: bool
    restore_delete: bool
    allow_protected_storage: bool
    stop_addon_before_sync: bool
    stop_core_before_sync_if_storage: bool
    restart_after_sync: bool
    optional: bool


class ResolvedTarget(Target, total=False):
    source_path: str
    live_path: str
    resolved_slug: Optional[str]


class ReleaseTarget(ResolvedTarget, total=False):
    existed: bool


def bool_option(target: Mapping[str, Any], name: str, default: bool) -> bool:
    return bool(target.get(name, default))


def apply_delete(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "delete", False)


def save_delete(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "save_delete", True)


def restore_delete(target: Mapping[str, Any]) -> bool:
    return bool(target.get("restore_delete", target.get("delete", True)))


def allow_protected_storage(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "allow_protected_storage", False)
