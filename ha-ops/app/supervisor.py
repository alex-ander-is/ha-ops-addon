import json
import os
import subprocess


def default_run_command(command):
    return subprocess.run(command, text=True, capture_output=True, check=False)


def call_supervisor(method, path, payload=None, run_command=default_run_command):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")

    command = [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        method,
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
    ]
    if payload is not None:
        command.extend(["-d", json.dumps(payload)])
    command.append(f"http://supervisor{path}")

    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"Supervisor API call failed for {path}:\n{result.stderr.strip() or result.stdout.strip()}")

    body = result.stdout.strip()
    if not body:
        return {}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def supervisor_ok(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("result") == "ok":
        return True
    return "data" in payload and "result" not in payload


def get_installed_addons(call_supervisor):
    payload = call_supervisor("GET", "/addons")
    addons = payload.get("data", {}).get("addons")
    if addons is None:
        addons = payload.get("addons", [])
    return addons


def get_addon_info(slug, call_supervisor):
    payload = call_supervisor("GET", f"/addons/{slug}/info")
    if "data" in payload:
        return payload["data"]
    return payload


def addon_action(slug, action, call_supervisor):
    payload = call_supervisor("POST", f"/addons/{slug}/{action}")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Add-on {slug} {action} failed: {payload}")


def core_stop(call_supervisor):
    payload = call_supervisor("POST", "/core/stop")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core stop failed: {payload}")


def core_start(call_supervisor):
    payload = call_supervisor("POST", "/core/start")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core start failed: {payload}")


def core_restart(call_supervisor):
    payload = call_supervisor("POST", "/core/restart")
    if not supervisor_ok(payload):
        raise RuntimeError(f"Core restart failed: {payload}")


def do_core_check(call_supervisor):
    payload = call_supervisor("POST", "/core/check")
    data = payload.get("data", {})
    if payload.get("result") == "ok" and data.get("result") == "valid":
        return
    raise RuntimeError(f"Home Assistant config check failed: {payload}")


def backup_mount_info(call_supervisor):
    payload = call_supervisor("GET", "/mounts")
    return payload.get("data", payload)


def default_backup_mount(backup_mount_info):
    try:
        return backup_mount_info().get("default_backup_mount")
    except Exception:
        return None


def create_ha_backup(name_prefix, backup_location, call_supervisor, release_now):
    payload = {"name": f"{name_prefix} {release_now()}"}
    payload["background"] = False
    if backup_location:
        payload["location"] = backup_location
    result = call_supervisor("POST", "/backups/new/full", payload)
    slug = result.get("data", {}).get("slug") or result.get("slug")
    if not slug:
        raise RuntimeError(f"Backup creation did not return a slug: {result}")
    return slug


def backup_manager_info(call_supervisor):
    payload = call_supervisor("GET", "/backups/info")
    return payload.get("data", payload)
