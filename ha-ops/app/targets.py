from typing import Any, Mapping, Optional, TypedDict

import policies


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


def apply_delete(target: Mapping[str, Any]) -> bool:
    return policies.apply_delete(target)


def save_delete(target: Mapping[str, Any]) -> bool:
    return policies.save_delete(target)


def restore_delete(target: Mapping[str, Any]) -> bool:
    return policies.restore_delete(target)


def allow_protected_storage(target: Mapping[str, Any]) -> bool:
    return policies.allow_protected_storage(target)


def reload_yaml_after_apply(target: Mapping[str, Any]) -> bool:
    return policies.reload_yaml_after_apply(target)


def restart_core_after_apply(target: Mapping[str, Any]) -> bool:
    return policies.restart_core_after_apply(target)


def stop_core_before_storage_apply(target: Mapping[str, Any]) -> bool:
    return policies.stop_core_before_storage_apply(target)


def start_core_after_storage_apply(target: Mapping[str, Any]) -> bool:
    return policies.start_core_after_storage_apply(target)


def reload_yaml_after_rollback(target: Mapping[str, Any]) -> bool:
    return policies.reload_yaml_after_rollback(target)


def restart_core_after_rollback(target: Mapping[str, Any]) -> bool:
    return policies.restart_core_after_rollback(target)


def stop_core_before_storage_rollback(target: Mapping[str, Any]) -> bool:
    return policies.stop_core_before_storage_rollback(target)


def start_core_after_storage_rollback(target: Mapping[str, Any]) -> bool:
    return policies.start_core_after_storage_rollback(target)
