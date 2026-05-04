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
    reload_yaml_after_apply: bool
    restart_core_after_apply: bool
    stop_core_before_storage_apply: bool
    start_core_after_storage_apply: bool
    reload_yaml_after_rollback: bool
    restart_core_after_rollback: bool
    stop_core_before_storage_rollback: bool
    start_core_after_storage_rollback: bool
    optional: bool


class ResolvedTarget(Target, total=False):
    source_path: str
    live_path: str
    resolved_slug: Optional[str]


class ReleaseTarget(ResolvedTarget, total=False):
    existed: bool


def bool_option(target: Mapping[str, Any], name: str, default: bool) -> bool:
    return bool(target.get(name, default))


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def policy_bool(target: Mapping[str, Any], name: str, default: bool, legacy_names=()) -> bool:
    if name in target:
        return bool_value(target.get(name))
    for legacy_name in legacy_names:
        if legacy_name in target:
            return bool_value(target.get(legacy_name))
    return default


def apply_delete(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "delete", False)


def save_delete(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "save_delete", True)


def restore_delete(target: Mapping[str, Any]) -> bool:
    return bool(target.get("restore_delete", target.get("delete", True)))


def allow_protected_storage(target: Mapping[str, Any]) -> bool:
    return bool_option(target, "allow_protected_storage", False)


def reload_yaml_after_apply(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "reload_yaml_after_apply", True)


def restart_core_after_apply(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "restart_core_after_apply", False, ("restart_after_sync",))


def stop_core_before_storage_apply(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "stop_core_before_storage_apply", True, ("stop_core_before_sync_if_storage",))


def start_core_after_storage_apply(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "start_core_after_storage_apply", True, ("restart_after_sync",))


def reload_yaml_after_rollback(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "reload_yaml_after_rollback", False)


def restart_core_after_rollback(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "restart_core_after_rollback", False, ("restart_after_sync",))


def stop_core_before_storage_rollback(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "stop_core_before_storage_rollback", True, ("stop_core_before_sync_if_storage",))


def start_core_after_storage_rollback(target: Mapping[str, Any]) -> bool:
    return policy_bool(target, "start_core_after_storage_rollback", True, ("restart_after_sync",))
