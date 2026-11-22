"""Collect the bounded Home Assistant data used for Alexa exposure."""

import json
import re
import stat

from .commands import CommandError, MAX_CONFIG_BYTES
from .ha_auth import _login
from .supervisor import MAX_RESPONSE_BYTES, UpstreamError

_REGISTRY_FIELDS = ("entity_id", "id", "disabled_by", "entity_category", "hidden_by", "name",
                    "original_name", "aliases", "area_id", "device_id", "platform", "device_class",
                    "original_device_class", "config_entry_id")
_DEVICE_FIELDS = ("id", "area_id", "name_by_user", "name", "manufacturer", "model")
_STRING_LIST_FIELDS = {"aliases"}


def _project(items, fields):
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        projected = {}
        for field in fields:
            value = item.get(field)
            if field in _STRING_LIST_FIELDS:
                if isinstance(value, list):
                    projected[field] = [entry for entry in value if isinstance(entry, str)]
            elif isinstance(value, (str, bool)) or value is None:
                projected[field] = value
        result.append(projected)
    return result


def _ha_filter(client):
    path = client.config_dir / "configuration.yaml"
    try:
        if client.config_dir.is_symlink():
            raise ValueError
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError
        lines = raw.decode("utf-8").splitlines()
    except FileNotFoundError:
        return "none"
    except (OSError, UnicodeError, ValueError):
        raise CommandError("conflict") from None

    alexa = next((i for i, line in enumerate(lines)
                  if re.match(r"^alexa:\s*(?:#.*)?$", line)), None)
    if alexa is None:
        return "none"
    end = next((i for i in range(alexa + 1, len(lines))
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*", lines[i])), len(lines))
    smart = next((i for i in range(alexa + 1, end)
                  if re.match(r"^\s+smart_home:\s*(?:#.*)?$", lines[i])), None)
    if smart is None:
        return "none"
    indent = len(lines[smart]) - len(lines[smart].lstrip())
    smart_end = next((i for i in range(smart + 1, end)
                      if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= indent), end)
    found = any(re.match(r"^\s+filter\s*:", line) and
                len(line) - len(line.lstrip()) > indent for line in lines[smart + 1:smart_end])
    return "custom" if found else "none"


async def alexa_inventory(client, params):
    results = await client.admin_ws([
        {"type": "config/entity_registry/list"},
        {"type": "config/device_registry/list"},
        {"type": "config/area_registry/list"},
        {"type": "get_states"},
        {"type": "config_entries/get"},
    ])
    if not isinstance(results, list) or len(results) != 5 or any(not isinstance(item, list) for item in results):
        raise UpstreamError()
    registry, devices, areas, raw_states, raw_entries = results
    if len(registry) > 5000 or len(raw_states) > 5000:
        raise CommandError("conflict")

    access, refresh = await _login(client)
    try:
        directive = {"directive": {"header": {"namespace": "Alexa.Discovery", "name": "Discover",
                       "messageId": "darak-inventory", "payloadVersion": "3"},
                       "payload": {"scope": {"type": "BearerToken", "token": "darak-inventory"}}}}
        status, raw = await client.ha_http("POST", "/api/alexa/smart_home", json=directive, token=access)
        if status != 200 or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise UpstreamError()
        try:
            discovery = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise UpstreamError() from None
        if not isinstance(discovery, dict):
            raise UpstreamError()
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass

    event = discovery.get("event") if isinstance(discovery, dict) else None
    payload = event.get("payload") if isinstance(event, dict) else None
    endpoints = []
    raw_endpoints = payload.get("endpoints", []) if isinstance(payload, dict) else []
    if isinstance(raw_endpoints, list):
        for endpoint in raw_endpoints:
            if not isinstance(endpoint, dict):
                continue
            capabilities = endpoint.get("capabilities", [])
            endpoints.append({
                "endpointId": endpoint.get("endpointId") if isinstance(endpoint.get("endpointId"), str) else None,
                "displayCategories": endpoint.get("displayCategories") or [],
                "interfaces": [item["interface"] for item in capabilities if isinstance(item, dict)
                               and isinstance(item.get("interface"), str)] if isinstance(capabilities, list) else [],
            })

    states = []
    for item in raw_states:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        states.append(_project([{**item, "friendly_name": attributes.get("friendly_name"),
                                 "device_class": attributes.get("device_class")}],
                               ("entity_id", "state", "friendly_name", "device_class"))[0])

    return {"ha_filter": _ha_filter(client), "registry": _project(registry, _REGISTRY_FIELDS),
            "devices": _project(devices, _DEVICE_FIELDS), "areas": _project(areas, ("area_id", "name")),
            "states": states, "entries": _project(raw_entries, ("entry_id", "title")),
            "endpoints": endpoints}
