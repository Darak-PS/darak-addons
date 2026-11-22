"""Home Assistant HTTP integration configuration commands."""

import ipaddress
import json
from pathlib import Path

from .enrollment import write_private_json
from .supervisor import UpstreamError

# The Supervisor gives each add-on a dynamic address from this range, and the
# agent's forwarded traffic reaches Home Assistant from that address.
AGENT_PROXY = "172.30.33.0/24"
# Agents 0.1.25-0.1.56 trusted the Supervisor's own address instead, which never
# carries the agent's traffic; older agents trusted the whole Supervisor network.
PREVIOUS_AGENT_PROXY = "172.30.32.2/32"
LEGACY_SUPERVISOR_PROXY = "172.30.32.0/23"
_META_KEYS = ("created_at", "error", "error_message")


def _without_meta(stable):
    return {key: value for key, value in (stable or {}).items() if key not in _META_KEYS}


def _trusts(config):
    if not isinstance(config, dict) or config.get("use_x_forwarded_for") is not True:
        return False
    proxies = config.get("trusted_proxies", [])
    if not isinstance(proxies, list):
        return False
    return (any(_contains_agent(proxy) for proxy in proxies)
            and not any(_agent_proxy_network(proxy) and not _contains_agent(proxy) for proxy in proxies))


async def _config(client):
    result, = await client.admin_ws([{"type": "http/config"}])
    if (not isinstance(result, dict) or not {"stable", "pending", "active_config_type"} <= result.keys()
            or any(result.get(key) is not None and not isinstance(result[key], dict)
                   for key in ("stable", "pending"))
            or not isinstance(result.get("active_config_type"), str)):
        raise UpstreamError()
    pending = result.get("pending")
    if pending is not None and pending.get("error") is not None:
        # A pending config that failed its trial is never applied again; it only
        # records the failure, so callers treat the slot as empty and may stage anew.
        result = {**result, "pending": None}
    return result


async def http_configure(client, params=None):
    current = await _config(client)
    stable, pending = current.get("stable"), current.get("pending")
    if pending is not None:
        raise _conflict()
    if _trusts(stable):
        return {"status": "unchanged"}
    merged = _without_meta(stable)
    merged["use_x_forwarded_for"] = True
    proxies = merged.get("trusted_proxies", [])
    if not isinstance(proxies, list):
        raise UpstreamError()
    original_proxies = list(proxies)
    added_proxy = not any(_contains_agent(proxy) for proxy in original_proxies)
    proxies = [proxy for proxy in original_proxies
               if not (_agent_proxy_network(proxy) and not _contains_agent(proxy)) and not _is_previous_proxy(proxy)]
    if added_proxy:
        proxies.append(AGENT_PROXY)
    if proxies or "trusted_proxies" in merged:
        merged["trusted_proxies"] = proxies
    original_path = Path(client.http_original_path)
    if not original_path.exists():
        # A home that never saved HTTP settings reports "stable": null.
        original = {
            "use_x_forwarded_for": (stable or {}).get("use_x_forwarded_for"),
            "added_trusted_proxy": added_proxy,
        }
        if any(proxy not in proxies for proxy in original_proxies):
            original["trusted_proxies"] = original_proxies
        write_private_json(original_path, original)
    await client.admin_ws([{"type": "http/config/configure", "config": merged}])
    return {"status": "staged"}


def _same_network(proxy, network):
    try:
        return ipaddress.ip_network(proxy, strict=False) == ipaddress.ip_network(network)
    except (ValueError, TypeError):
        return False


def _contains_agent(proxy):
    return _same_network(proxy, AGENT_PROXY)


def _is_previous_proxy(proxy):
    return _same_network(proxy, PREVIOUS_AGENT_PROXY)


def _is_legacy_proxy(proxy):
    return _same_network(proxy, LEGACY_SUPERVISOR_PROXY)


def _is_managed_proxy(proxy):
    return _contains_agent(proxy) or _is_previous_proxy(proxy) or _is_legacy_proxy(proxy)


def _agent_proxy_network(proxy):
    """True for the agent range and any wider network around it."""
    try:
        return ipaddress.ip_network(AGENT_PROXY).subnet_of(ipaddress.ip_network(proxy, strict=False))
    except (ValueError, TypeError):
        return False


async def http_restore(client, params=None):
    current = await _config(client)
    stable, pending = current.get("stable"), current.get("pending")
    if pending is not None:
        raise _conflict()
    original_path = Path(client.http_original_path)
    try:
        original = json.loads(original_path.read_text())
    except FileNotFoundError:
        original = None
    except (OSError, ValueError, TypeError):
        raise UpstreamError() from None
    if original is not None and (not isinstance(original, dict)
                                 or set(original) not in ({"use_x_forwarded_for", "added_trusted_proxy"},
                                                          {"use_x_forwarded_for", "added_trusted_proxy", "trusted_proxies"})
                                 or type(original["added_trusted_proxy"]) is not bool
                                 or "trusted_proxies" in original and not isinstance(original["trusted_proxies"], list)):
        raise UpstreamError()
    stable_proxies = stable.get("trusted_proxies", []) if isinstance(stable, dict) else []
    managed_proxy_present = (isinstance(stable_proxies, list)
                             and any(_is_managed_proxy(proxy) for proxy in stable_proxies))
    has_saved_proxies = original is not None and "trusted_proxies" in original
    if not managed_proxy_present and not has_saved_proxies:
        return {"status": "unchanged"}
    merged = _without_meta(stable)
    proxies = merged.get("trusted_proxies", [])
    if not isinstance(proxies, list):
        raise UpstreamError()
    if has_saved_proxies:
        proxies = list(original["trusted_proxies"])
        for proxy in stable_proxies:
            if not _is_managed_proxy(proxy) and proxy not in proxies:
                proxies.append(proxy)
    elif original is None or original.get("added_trusted_proxy") is True:
        proxies = [proxy for proxy in proxies if not _is_managed_proxy(proxy)]
    if proxies or "trusted_proxies" in merged:
        merged["trusted_proxies"] = proxies
    if original is not None:
        value = original.get("use_x_forwarded_for")
        if value is None:
            merged.pop("use_x_forwarded_for", None)
        else:
            merged["use_x_forwarded_for"] = value
    elif not proxies:
        merged.pop("use_x_forwarded_for", None)
    await client.admin_ws([{"type": "http/config/configure", "config": merged}])
    return {"status": "staged"}


async def http_status(client, params=None):
    current = await _config(client)
    stable, pending = current.get("stable"), current.get("pending")
    active = current.get("active_config_type")
    return {"active_config_type": active if isinstance(active, str) and len(active) <= 64 else "unknown",
            "pending": pending is not None, "stable_trusts_agent": _trusts(stable),
            "pending_trusts_agent": _trusts(pending)}


async def http_promote(client, params=None):
    params = params or {}
    expectation = params.get("expect", "trust")
    if expectation not in ("trust", "restore") or set(params) - {"expect"}:
        from .commands import CommandError
        raise CommandError("invalid_params")
    current = await _config(client)
    pending, stable = current.get("pending"), current.get("stable")
    trusts_agent = _trusts(pending)
    if current.get("active_config_type") == "pending" and ((expectation == "trust" and trusts_agent)
                                                              or (expectation == "restore" and not trusts_agent)):
        await client.admin_ws([{"type": "http/config/promote"}])
        if expectation == "restore":
            Path(client.http_original_path).unlink(missing_ok=True)
        return {"status": "promoted"}
    if pending is None and ((expectation == "trust" and _trusts(stable))
                            or (expectation == "restore" and not _trusts(stable))):
        if expectation == "restore":
            Path(client.http_original_path).unlink(missing_ok=True)
        return {"status": "unchanged"}
    raise _conflict()


def _conflict():
    from .commands import CommandError
    return CommandError("conflict")
