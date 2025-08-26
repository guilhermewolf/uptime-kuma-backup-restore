#!/usr/bin/env python3
"""
Restore Uptime Kuma monitors (and notifications) from a backup JSON.

Features
- Minimal, valid payloads for groups and monitors (filters unsupported keys)
- Robust Socket.IO handling with retry on BadNamespaceError/Timeout
- Tolerant ID extraction across library/server versions
- Preserves group hierarchy (topological order)
- Maps old->new IDs for notifications and groups, applies to monitors
- Dry-run support
- Short-lived API sessions per phase to reduce stale sockets
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple, Optional

from socketio.exceptions import BadNamespaceError

from uptime_kuma_api import (
    UptimeKumaApi,
    MonitorType,
    AuthMethod,
    NotificationType,
    UptimeKumaException,
)
from uptime_kuma_api.exceptions import Timeout as KumaTimeout


# ----------------------------- Logging & utils -----------------------------

def log(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")

def die(msg: str, code: int = 1) -> None:
    log("ERROR", msg)
    sys.exit(code)

def env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        die(f"Missing required env var: {name}")
    return val

def as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

def load_backup(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_notification_ids(nmap: Any) -> List[int]:
    """
    Backups may store notification selection as a dict like {"3": true}.
    Normalise to a list of ints: [3].
    """
    if not nmap:
        return []
    if isinstance(nmap, dict):
        return [int(k) for k, v in nmap.items() if v]
    if isinstance(nmap, list):
        return [int(x) for x in nmap]
    return []

# ----------------------------- API client helpers -----------------------------

DEFAULT_TIMEOUT = int(os.getenv("KUMA_TIMEOUT", "60"))

def fresh_api() -> UptimeKumaApi:
    """
    Return a logged-in UptimeKumaApi client with a longer timeout.
    """
    url = env("KUMA_URL")
    username = env("KUMA_USERNAME")
    password = env("KUMA_PASSWORD")
    api = UptimeKumaApi(url, timeout=DEFAULT_TIMEOUT)
    api.login(username, password)
    # Prime connection; ignore failures (some servers throttle 'info' early on)
    with contextlib.suppress(Exception):
        _ = api.get_version()
    return api

def safe_call(fn, *args, **kwargs):
    """
    Execute a Kuma API call and, on BadNamespaceError/Timeout, reconnect once and retry.
    """
    try:
        return fn(*args, **kwargs)
    except (BadNamespaceError, KumaTimeout):
        with contextlib.suppress(Exception):
            # Close the original API instance if it's a bound method
            api = getattr(fn, "__self__", None)
            if api:
                api.disconnect()
        api2 = fresh_api()
        try:
            rebound = getattr(api2, fn.__name__) if hasattr(fn, "__name__") else None
            if rebound is None:
                # Fallback: call via the original callable
                return fn(*args, **kwargs)
            return rebound(*args, **kwargs)
        finally:
            with contextlib.suppress(Exception):
                api2.disconnect()

def safe_add_monitor(api: UptimeKumaApi, **kwargs) -> Any:
    return safe_call(api.add_monitor, **kwargs)

def safe_add_notification(api: UptimeKumaApi, **kwargs) -> Any:
    return safe_call(api.add_notification, **kwargs)

def safe_pause_monitor(api: UptimeKumaApi, monitor_id: int) -> Any:
    return safe_call(api.pause_monitor, monitor_id)

def safe_get_monitors(api: UptimeKumaApi) -> List[Dict[str, Any]]:
    return safe_call(api.get_monitors)

def safe_get_notifications(api: UptimeKumaApi) -> List[Dict[str, Any]]:
    return safe_call(api.get_notifications)

# ----------------------------- ID extraction -----------------------------

def extract_monitor_id(res: Any, *, name_for_fallback: Optional[str] = None, api_for_fallback: Optional[UptimeKumaApi] = None) -> int:
    """
    Extract a monitor ID from various add_monitor() return shapes, optionally
    falling back to fetching monitors by name when the ID isn't present.
    """
    if res is None:
        raise ValueError("add_monitor() returned None")

    if isinstance(res, (int, str)):
        return int(res)

    if isinstance(res, dict):
        for k in ("monitorId", "monitorID", "id"):
            if k in res and res[k] is not None:
                return int(res[k])
        mon = res.get("monitor") or res.get("data") or {}
        if isinstance(mon, dict):
            for k in ("id", "monitorId", "monitorID"):
                if k in mon and mon[k] is not None:
                    return int(mon[k])

    # Fallback: look up by name if provided
    if name_for_fallback and api_for_fallback:
        monitors = safe_get_monitors(api_for_fallback)
        candidates = [m for m in monitors if m.get("name") == name_for_fallback]
        if candidates:
            return int(candidates[0]["id"])

    raise ValueError(f"Could not find monitor ID in add_monitor() response: {repr(res)}")

def extract_notification_id(res: Any) -> int:
    """
    Extract a notification ID from various add_notification() return shapes.
    """
    if res is None:
        raise ValueError("add_notification() returned None")

    if isinstance(res, (int, str)):
        return int(res)

    if isinstance(res, dict):
        for k in ("id", "notificationId", "notificationID"):
            if k in res and res[k] is not None:
                return int(res[k])
        node = res.get("notification") or res.get("data") or {}
        if isinstance(node, dict):
            for k in ("id", "notificationId", "notificationID"):
                if k in node and node[k] is not None:
                    return int(node[k])

    raise ValueError(f"Could not find notification ID in add_notification() response: {repr(res)}")

# ----------------------------- Mapping constants -----------------------------

MONITOR_TYPE_MAP: Dict[str, MonitorType] = {
    "group": MonitorType.GROUP,
    "http": MonitorType.HTTP,
    "ping": MonitorType.PING,
    "dns": MonitorType.DNS,
    "port": MonitorType.PORT,
    "keyword": MonitorType.KEYWORD,
    "json-query": MonitorType.JSON_QUERY,
    "grpc-keyword": MonitorType.GRPC_KEYWORD,
    "docker": MonitorType.DOCKER,
    "real-browser": MonitorType.REAL_BROWSER,
    "push": MonitorType.PUSH,
    "steam": MonitorType.STEAM,
    "gamedig": MonitorType.GAMEDIG,
    "mqtt": MonitorType.MQTT,
    "kafka-producer": MonitorType.KAFKA_PRODUCER,
    "sqlserver": MonitorType.SQLSERVER,
    "postgres": MonitorType.POSTGRES,
    "mysql": MonitorType.MYSQL,
    "mongodb": MonitorType.MONGODB,
    "radius": MonitorType.RADIUS,
    "redis": MonitorType.REDIS,
    "tailscale-ping": MonitorType.TAILSCALE_PING,
}

AUTH_METHOD_MAP: Dict[Optional[str], AuthMethod] = {
    None: AuthMethod.NONE,
    "": AuthMethod.NONE,
    "basic": AuthMethod.HTTP_BASIC,
    "ntlm": AuthMethod.NTLM,
    "mtls": AuthMethod.MTLS,
    "oauth2-cc": AuthMethod.OAUTH2_CC,
}

# ----------------------------- Creation routines -----------------------------

def topological_groups(monitor_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups = [m for m in monitor_list if m.get("type") == "group"]
    by_id = {m["id"]: m for m in groups}

    def depth(node: Dict[str, Any]) -> int:
        d = 0
        cur = node.get("parent")
        while cur and cur in by_id:
            d += 1
            cur = by_id[cur].get("parent")
        return d

    return sorted(groups, key=depth)

def create_notifications(
    api: UptimeKumaApi,
    notification_list: List[Dict[str, Any]],
    dry_run: bool,
) -> Dict[int, int]:
    id_map: Dict[int, int] = {}
    if not notification_list:
        return id_map

    existing_by_name = {n["name"]: n["id"] for n in safe_get_notifications(api)}

    for notif in notification_list:
        old_id = int(notif["id"])
        name = (notif.get("name") or f"Imported {old_id}").strip()

        if name in existing_by_name:
            id_map[old_id] = int(existing_by_name[name])
            log("SKIP", f"Notification '{name}' already exists -> id {existing_by_name[name]}")
            continue

        cfg_raw = notif.get("config") or "{}"
        try:
            cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        except json.JSONDecodeError:
            log("WARN", f"Notification '{name}': invalid JSON in 'config'; using empty object.")
            cfg = {}

        notif_type_str = cfg.get("type") or cfg.get("name")  # some backups put the provider in 'name'
        if not notif_type_str:
            log("WARN", f"Notification '{name}' missing provider type; defaulting to PUSHOVER")
            notif_type = NotificationType.PUSHOVER
        else:
            notif_type = getattr(NotificationType, str(notif_type_str).upper(), notif_type_str)

        cfg_clean = {k: v for k, v in cfg.items() if k not in {"type", "name", "applyExisting", "isDefault"}}

        if dry_run:
            log("DRY", f"Would create notification '{name}' (provider: {notif_type_str})")
            new_id = -old_id
        else:
            res = safe_add_notification(
                api,
                type=notif_type,
                name=name,
                config=cfg_clean,
                isDefault=bool(notif.get("isDefault", False)),
                applyExisting=bool(cfg.get("applyExisting", False)),
            )
            new_id = extract_notification_id(res)
            log("OK ", f"Created notification '{name}' -> id {new_id}")

        id_map[old_id] = int(new_id)

    return id_map

def create_groups(
    api: UptimeKumaApi,
    monitor_list: List[Dict[str, Any]],
    dry_run: bool,
) -> Dict[int, int]:
    """
    Create 'group' monitors with minimal payload only: {type, name, parent}.
    Returns old_id -> new_id mapping.
    """
    id_map: Dict[int, int] = {}
    groups = topological_groups(monitor_list)

    existing = safe_get_monitors(api)
    existing_groups = {m["name"]: m["id"] for m in existing if str(m["type"]) == str(MonitorType.GROUP)}

    for g in groups:
        old_id = int(g["id"])
        name = str(g.get("name") or f"Group {old_id}").strip()
        parent_old = g.get("parent")
        parent_new = id_map.get(parent_old) if parent_old else None

        if name in existing_groups:
            id_map[old_id] = int(existing_groups[name])
            log("SKIP", f"Group '{name}' already exists -> id {existing_groups[name]}")
            continue

        kwargs = dict(type=MonitorType.GROUP, name=name, parent=parent_new)

        if dry_run:
            log("DRY", f"Would create group '{name}' (parent id {parent_new})")
            new_id = -old_id
        else:
            res = safe_add_monitor(api, **kwargs)
            new_id = extract_monitor_id(res, name_for_fallback=name, api_for_fallback=api)
            log("OK ", f"Created group '{name}' -> id {new_id}")

        id_map[old_id] = int(new_id)

    return id_map

def create_monitors(
    api: UptimeKumaApi,
    monitor_list: List[Dict[str, Any]],
    group_id_map: Dict[int, int],
    notif_id_map: Dict[int, int],
    only_active: bool,
    dry_run: bool,
) -> Tuple[int, int, int]:
    """
    Create non-group monitors. Returns (created_count, paused_count, skipped_count).
    """
    created = paused = skipped = 0

    DROP_KEYS = {
        "weight",          # UI sort hint (not accepted)
        "resendInterval",  # not supported in add_monitor payload
        "description",     # not accepted by API
    }

    def map_auth(method_str: Optional[str]) -> AuthMethod:
        return AUTH_METHOD_MAP.get(method_str, AuthMethod.NONE)

    for m in monitor_list:
        if m.get("type") == "group":
            continue

        name = (m.get("name") or "Unnamed").strip()
        type_str = str(m.get("type") or "").strip().lower()

        if only_active and not as_bool(m.get("active"), True):
            skipped += 1
            log("SKIP", f"Inactive monitor '{name}' (only_active enabled)")
            continue

        mtype = MONITOR_TYPE_MAP.get(type_str)
        if mtype is None:
            skipped += 1
            log("WARN", f"Unknown monitor type '{type_str}' for '{name}', skipping")
            continue

        parent_new = group_id_map.get(m.get("parent")) if m.get("parent") else None
        notif_old_ids = normalize_notification_ids(m.get("notificationIDList"))
        notif_new_ids = [notif_id_map[i] for i in notif_old_ids if i in notif_id_map] or None

        # Base kwargs common to most monitors
        kwargs: Dict[str, Any] = {
            "type": mtype,
            "name": name,
            "parent": parent_new,
            "interval": m.get("interval", 60),
            "retryInterval": m.get("retryInterval", 60),
            "maxretries": m.get("maxretries", 0),
            "upsideDown": as_bool(m.get("upsideDown"), False),
            "timeout": m.get("timeout", 48),
            "notificationIDList": notif_new_ids,
        }

        # Type-specific settings
        if mtype in {MonitorType.HTTP, MonitorType.KEYWORD, MonitorType.JSON_QUERY, MonitorType.REAL_BROWSER, MonitorType.PUSH}:
            kwargs.update({
                "url": m.get("url"),
                "method": m.get("method", "GET"),
                "ignoreTls": as_bool(m.get("ignoreTls"), False),
                "maxredirects": m.get("maxredirects", 10),
                "accepted_statuscodes": m.get("accepted_statuscodes") or None,
                "httpBodyEncoding": m.get("httpBodyEncoding") or "json",
                "headers": m.get("headers"),
                "body": m.get("body"),
                "keyword": m.get("keyword"),
                "invertKeyword": as_bool(m.get("invertKeyword"), False),
                "jsonPath": m.get("jsonPath"),
                "expectedValue": m.get("expectedValue"),
                "authMethod": map_auth(m.get("authMethod")),
                "basic_auth_user": m.get("basic_auth_user"),
                "basic_auth_pass": m.get("basic_auth_pass"),
                "oauth_client_id": m.get("oauth_client_id"),
                "oauth_client_secret": m.get("oauth_client_secret"),
                "oauth_token_url": m.get("oauth_token_url"),
                "oauth_scopes": m.get("oauth_scopes"),
                "oauth_auth_method": m.get("oauth_auth_method"),
                "tlsCa": m.get("tlsCa"),
                "tlsCert": m.get("tlsCert"),
                "tlsKey": m.get("tlsKey"),
            })

        if mtype == MonitorType.PING:
            kwargs.update({
                "hostname": m.get("hostname"),
                "packetSize": m.get("packetSize", 56),
            })

        if mtype == MonitorType.DNS:
            kwargs.update({
                "hostname": m.get("hostname"),
                "port": m.get("port") or 53,
                "dns_resolve_server": m.get("dns_resolve_server") or "1.1.1.1",
                "dns_resolve_type": m.get("dns_resolve_type") or "A",
            })

        if mtype == MonitorType.PORT:
            kwargs.update({
                "hostname": m.get("hostname"),
                "port": m.get("port"),
            })

        # Remove unsupported or None keys
        for k in list(kwargs.keys()):
            if k in DROP_KEYS or kwargs[k] is None:
                kwargs.pop(k, None)

        active = as_bool(m.get("active"), True)

        if dry_run:
            log("DRY", f"Would create monitor '{name}' (type={type_str}, parent={parent_new}, active={active})")
            continue

        try:
            res = safe_add_monitor(api, **kwargs)
            new_id = extract_monitor_id(res, name_for_fallback=name, api_for_fallback=api)
            created += 1
            log("OK ", f"Created monitor '{name}' -> id {new_id}")
            if not active:
                safe_pause_monitor(api, new_id)
                paused += 1
                log("OK ", f"Paused monitor '{name}'")
        except UptimeKumaException as e:
            skipped += 1
            log("FAIL", f"Could not create monitor '{name}': {e}")

    return created, paused, skipped

# ----------------------------- Main -----------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Recreate Uptime Kuma monitors from a backup JSON.")
    parser.add_argument("--backup", required=True, help="Path to Uptime Kuma backup JSON")
    parser.add_argument("--dry-run", action="store_true", help="Only print actions; do not create anything")
    parser.add_argument("--skip-notifications", action="store_true", help="Do not (re)create notification channels")
    parser.add_argument("--only-active", action="store_true", help="Only create monitors that are active in the backup")
    args = parser.parse_args()

    url = env("KUMA_URL")
    username = env("KUMA_USERNAME")
    # We check the password lazily so env() gives a sensible error if missing
    _ = env("KUMA_PASSWORD")

    data = load_backup(args.backup)
    monitor_list = data.get("monitorList", []) or []
    notification_list = data.get("notificationList", []) or []

    if not monitor_list and not notification_list:
        die("Backup has no monitors or notifications")

    log("INFO", f"Connecting to {url} as {username}")

    # Notifications
    notif_id_map: Dict[int, int] = {}
    if args.skip_notifications:
        log("INFO", "Skipping notifications as requested.")
    else:
        with UptimeKumaApi(url, timeout=DEFAULT_TIMEOUT) as api:
            api.login(username, env("KUMA_PASSWORD"))
            log("INFO", "Creating notifications…")
            notif_id_map = create_notifications(api, notification_list, args.dry_run)

    # Groups
    with UptimeKumaApi(url, timeout=DEFAULT_TIMEOUT) as api:
        api.login(username, env("KUMA_PASSWORD"))
        log("INFO", "Creating groups…")
        group_id_map = create_groups(api, monitor_list, args.dry_run)

    # Monitors
    with UptimeKumaApi(url, timeout=DEFAULT_TIMEOUT) as api:
        api.login(username, env("KUMA_PASSWORD"))
        log("INFO", "Creating monitors…")
        created, paused, skipped = create_monitors(
            api=api,
            monitor_list=monitor_list,
            group_id_map=group_id_map,
            notif_id_map=notif_id_map,
            only_active=args.only_active,
            dry_run=args.dry_run,
        )

    # Summary
    total_groups = len([m for m in monitor_list if m.get("type") == "group"])
    total_monitors = len([m for m in monitor_list if m.get("type") != "group"])
    log("DONE", f"Groups in backup: {total_groups}; Monitors in backup: {total_monitors}")
    if not args.dry_run:
        log("DONE", f"Monitors created: {created} (paused: {paused}, skipped: {skipped})")
    else:
        log("DONE", "Dry-run complete; no changes were made.")

if __name__ == "__main__":
    main()
