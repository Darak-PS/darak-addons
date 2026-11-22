"""The closed command set and explicit public result schemas."""

import asyncio
import base64
import json
import hashlib
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from . import __version__
from .supervisor import LOG_ADDON_RE, MAX_RESPONSE_BYTES, SLUG_RE, UpstreamError
from .ha_auth import HaCommandError, service_user_ensure, service_user_remove, token_issue, token_revoke
from .ha_auth import _login
from .ha_auth import tech_user_enable, tech_user_disable
from .ha_auth import managed_user_list, managed_user_create, managed_user_update, managed_user_delete, managed_user_sso_code
from .tech_sessions import ssh_open, ssh_close, _expiry
from .consent import (disable_with_consent, gated_ssh_open, gated_tech_user_enable,
                      session_start, session_status, session_mode)
from .ha_http import http_configure, http_promote, http_restore, http_status

MAX_LOG_BYTES = 256 * 1024
MAX_BACKUP_BYTES = 8 * 1024**3
MAX_CONFIG_BYTES = 256 * 1024
MAX_ENTITY_BYTES = 32 * 1024
# One writer for every Home Assistant configuration file, Alexa's included.
CONFIG_LOCK = asyncio.Lock()
CONFIG_FILES = frozenset({"configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml",
                          "customize.yaml", "secrets.yaml"})
ENTITY_DOMAINS = frozenset("light switch cover climate fan media_player scene script automation input_boolean input_number input_select input_button button vacuum humidifier water_heater number select siren valve homeassistant".split())
ENTITY_SERVICES = frozenset({"turn_on", "turn_off", "toggle", "update_entity"})
SECURITY_DOMAINS = frozenset({"lock", "alarm_control_panel"})
BLOCKED_COMMANDS = frozenset({"config_write", "config_revert", "core_restart", "entity_call", "ha_rest",
                              "oauth_install", "oauth_configure", "oauth_remove"})
ERROR_MESSAGES = {
    "invalid_params": "Invalid command parameters",
    "conflict": "Command not applicable in the current state",
    "upstream_unavailable": "Upstream unavailable",
    "upstream_error": "Invalid upstream response",
    "ha_error": "Home Assistant command failed",
    "busy": "Agent busy",
    "customer_blocked": "The customer has disabled support sessions",
    "approval_required": "Customer approval is required for support sessions",
}


class CommandError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message if code == "ha_error" and isinstance(message, str) else ERROR_MESSAGES[code]
        super().__init__(self.message)


def _no_params(params):
    if not isinstance(params, dict):
        raise CommandError("invalid_params")
    if params:
        raise CommandError("invalid_params")
    return {}


def _http_promote_params(params):
    if not isinstance(params, dict) or params.keys() - {"expect"}:
        raise CommandError("invalid_params")
    if params.get("expect", "trust") not in ("trust", "restore"):
        raise CommandError("invalid_params")
    return params


def _session_params(params):
    if not isinstance(params, dict) or params.keys() != {"password", "expires_at"}:
        raise CommandError("invalid_params")
    _expiry(params["expires_at"])
    return params


def _session_start_params(params):
    if (not isinstance(params, dict) or params.keys() - {"password", "minutes", "public_key"}
            or not {"password", "minutes"}.issubset(params)):
        raise CommandError("invalid_params")
    password, minutes = params["password"], params["minutes"]
    if (not isinstance(password, str) or not 24 <= len(password) <= 128
            or any(not 32 <= ord(char) <= 126 for char in password)
            or type(minutes) is not int or not 5 <= minutes <= 240):
        raise CommandError("invalid_params")
    if "public_key" in params:
        from .tech_sessions import _key
        _key(params["public_key"])
    return params


def _session_status_params(params):
    if (not isinstance(params, dict) or set(params) != {"request_id"}
            or not isinstance(params["request_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", params["request_id"])):
        raise CommandError("invalid_params")
    return params


def _ssh_params(params):
    if not isinstance(params, dict) or params.keys() != {"public_key", "expires_at"}:
        raise CommandError("invalid_params")
    from .tech_sessions import _key
    _key(params["public_key"])
    _expiry(params["expires_at"])
    return params


def _logs_params(params):
    if not isinstance(params, dict):
        raise CommandError("invalid_params")
    if params.keys() - {"source", "addon", "lines"}:
        raise CommandError("invalid_params")
    source = params.get("source")
    lines = params.get("lines", 200)
    addon = params.get("addon")
    if (not isinstance(source, str) or source not in {"core", "supervisor", "host", "addon"}
            or type(lines) is not int or not 1 <= lines <= 2000):
        raise CommandError("invalid_params")
    if source == "addon":
        if not isinstance(addon, str) or not LOG_ADDON_RE.fullmatch(addon):
            raise CommandError("invalid_params")
    elif "addon" in params:
        raise CommandError("invalid_params")
    return {"source": source, "addon": addon, "lines": lines}


def _purpose_params(params):
    if (not isinstance(params, dict) or set(params) != {"purpose"}
            or not isinstance(params["purpose"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", params["purpose"])):
        raise CommandError("invalid_params")
    return params


def validate_params(name, params):
    try:
        return COMMANDS[name].validate(params)
    except KeyError:
        raise CommandError("invalid_params") from None


def _number(value):
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise UpstreamError()
    return value


def _uptime(host, info):
    # Supervisor reports boot_timestamp in microseconds since the epoch, not an uptime.
    boot = host.get("boot_timestamp")
    if type(boot) is int and boot > 0:
        return max(0, round(time.time() - boot / 1_000_000))
    return _number(host.get("uptime", info.get("uptime")))


def _bool(value):
    if value is not None and type(value) is not bool:
        raise UpstreamError()
    return value


def _text(client, value):
    return None if value is None else client.safe_text(value)


def _records(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise UpstreamError()
    return value


def _version(client, data):
    return {"version": _text(client, data.get("version")),
            "latest": _text(client, data.get("version_latest")),
            "update_available": _bool(data.get("update_available"))}


async def versions(client, params):
    core = await client.get_json("/core/info")
    supervisor = await client.get_json("/supervisor/info")
    os_info = await client.get_json("/os/info")
    return {"agent_version": __version__, "core": _version(client, core), "supervisor": _version(client, supervisor),
            "os": {**_version(client, os_info), "board": _text(client, os_info.get("board"))}}


async def health(client, params):
    # Each stats call samples CPU for about a second; run the reads together.
    async def tolerant_core_stats():
        try:
            return await client.get_json("/core/stats")
        except UpstreamError:
            return {}

    async def core_state():
        for attempt in range(2):
            try:
                async with asyncio.timeout(4):
                    value = (await client.get_json("/core/api/config")).get("state")
                break
            except TimeoutError:
                if attempt == 1:
                    return "unavailable"
            except (UpstreamError, aiohttp.ClientError, OSError):
                return "unavailable"
        state = value.lower() if isinstance(value, str) else ""
        return state if state in {"running", "starting", "not_running", "stopping", "final_write", "stopped"} else "unknown"

    (info, host, os_info, core, supervisor, core_stats, supervisor_stats, resolution,
     addons, state) = await asyncio.gather(
        client.get_json("/info"), client.get_json("/host/info"), client.get_json("/os/info"),
        client.get_json("/core/info"), client.get_json("/supervisor/info"), tolerant_core_stats(),
        client.get_json("/supervisor/stats"), client.get_json("/resolution/info"),
        client.get_json("/addons"), core_state())
    reasons = {}
    for key in ("unhealthy", "unsupported"):
        values = resolution.get(key)
        if not isinstance(values, list):
            raise UpstreamError()
        reasons[key] = [client.safe_text(value) for value in values]
        reasons[key + "_count"] = len(values)
    addon_list = []
    for addon in _records(addons.get("addons")):
        addon_list.append({
            **{key: _text(client, addon.get(key)) for key in ("slug", "name", "version", "state")},
            "update_available": _bool(addon.get("update_available")),
        })
    stats_keys = ("cpu_percent", "memory_usage", "memory_limit", "memory_percent")
    return {
        "online": True, "agent_version": __version__,
        "core": {**_version(client, core), "state": state}, "supervisor": _version(client, supervisor),
        "os": {**_version(client, os_info), "board": _text(client, os_info.get("board"))},
        "host": {
            "hostname": _text(client, host.get("hostname")),
            **{f"disk_{key}_gb": _number(host.get(f"disk_{key}")) for key in ("total", "used", "free")},
            "uptime_seconds": _uptime(host, info),
            "cpu_temperature_celsius": client.temperature(),
        },
        "core_stats": {key: _number(core_stats.get(key)) for key in stats_keys},
        "supervisor_stats": {key: _number(supervisor_stats.get(key)) for key in stats_keys},
        "resolution": reasons, "addons": {"count": len(addon_list), "items": addon_list},
    }


async def logs(client, params):
    text = await client.logs(**params)
    lines = text.splitlines(keepends=True)
    truncated = len(lines) > params["lines"]
    text = "".join(lines[-params["lines"]:])
    encoded = text.encode("utf-8")
    truncated |= len(encoded) > MAX_LOG_BYTES
    text = encoded[:MAX_LOG_BYTES].decode("utf-8", errors="ignore")
    return {"source": params["source"], "addon": params["addon"],
            "lines_returned": len(text.splitlines()), "truncated": truncated, "text": text}


async def integrations(client, params):
    entries = [
        {key: client.safe_text(entry[key]) for key in ("domain", "title", "state", "entry_id")}
        for entry in _records(await client.integrations())
    ]
    entries.sort(key=lambda entry: (entry["domain"], entry["title"]))
    return {"entries": entries, "counts_by_state": dict(sorted(Counter(
        entry["state"] for entry in entries
    ).items()))}


async def entities_summary(client, params):
    states = _records(await client.get_json("/core/api/states"))
    domains = Counter()
    unavailable = unknown = 0
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not re.fullmatch(r"[a-z0-9_]{1,64}\.[a-z0-9_]+", entity_id):
            raise UpstreamError()
        if not isinstance(state.get("state"), str):
            raise UpstreamError()
        domains[entity_id.split(".", 1)[0]] += 1
        unavailable += state["state"] == "unavailable"
        unknown += state["state"] == "unknown"
    return {"total": len(states), "counts_by_domain": dict(sorted(domains.items())),
            "unavailable": unavailable, "unknown": unknown}


async def backups(client, params):
    data = await client.get_json("/backups")
    items = [
        {**{key: client.safe_text(item[key]) for key in ("slug", "name", "date", "type")},
         "size_mb": _number(item.get("size")), "protected": _bool(item.get("protected"))}
        for item in _records(data.get("backups"))
    ]
    def timestamp(item):
        date = datetime.fromisoformat(item["date"])
        return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()

    items.sort(key=timestamp, reverse=True)
    return {"backups": items}


def _backup_slug_params(params):
    if not isinstance(params, dict) or set(params) != {"slug"} or not isinstance(params["slug"], str) or not re.fullmatch(r"[0-9a-f]{8}", params["slug"]):
        raise CommandError("invalid_params")
    return params


def _backup_job_params(params):
    if not isinstance(params, dict) or set(params) != {"job_id"}:
        raise CommandError("invalid_params")
    try:
        if str(uuid.UUID(params["job_id"])) != params["job_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CommandError("invalid_params") from None
    return params


async def backup_create(client, params):
    name = "darak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    installed = _records((await client.get_json("/addons")).get("addons"))
    if any(not isinstance(item.get("slug"), str) for item in installed):
        raise UpstreamError()
    own = await _own_slug(client)
    excluded = {"local_darak_agent", own}
    data = await client.request_json("POST", "/backups/new/partial", {
        "name": name, "compressed": True, "background": True, "homeassistant": True,
        "folders": ["ssl", "share", "addons/local", "media"],
        "addons": [item["slug"] for item in installed if item["slug"] not in excluded],
    })
    job_id = data.get("job_id")
    try:
        job_id = str(uuid.UUID(job_id))
    except (ValueError, TypeError, AttributeError):
        raise UpstreamError() from None
    return {"job_id": job_id, "name": name}


async def backup_job(client, params):
    # Supervisor keys jobs by the undashed hex form of the UUID.
    data = await client.get_json("/jobs/" + uuid.UUID(params["job_id"]).hex)
    done = data.get("done")
    if type(done) is not bool:
        raise UpstreamError()
    errors = data.get("errors", [])
    if not isinstance(errors, list):
        errors = []
    # Supervisor reports job errors as {"type", "message", "stage"} objects.
    messages = [value.get("message") if isinstance(value, dict) else value for value in errors[:5]]
    errors = [client.safe_text(value) if isinstance(value, str) and value else "error" for value in messages]
    slug = data.get("reference") if done else None
    if not isinstance(slug, str) or not re.fullmatch(r"[0-9a-f]{8}", slug):
        slug = None
    return {"done": done, "errors": errors, "backup_slug": slug}


async def _hash_backup(client, slug):
    digest, size = hashlib.sha256(), 0
    response = await client.backup_download(slug)
    try:
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BACKUP_BYTES:
                raise UpstreamError()
            digest.update(chunk)
    finally:
        await response.release()
    return {"size": size, "sha256": digest.hexdigest()}


async def backup_prepare(client, params):
    slug = params["slug"]
    cache = getattr(client, "_backup_hashes", None)
    if cache is None:
        cache = client._backup_hashes = {}
    if slug not in cache:
        while len(cache) >= 4:
            cache.pop(next(iter(cache)))
        cache[slug] = asyncio.create_task(_hash_backup(client, slug))
    task = cache[slug]
    if not task.done():
        return {"state": "hashing"}
    try:
        return {"state": "ready", **task.result()}
    except Exception:
        cache.pop(slug, None)
        return {"state": "upstream_error"}


async def backup_delete(client, params):
    slug = params["slug"]
    data = await client.get_json(f"/backups/{slug}/info")
    if not isinstance(data.get("name"), str) or not data["name"].startswith("darak-"):
        raise CommandError("conflict")
    await client.delete_json(f"/backups/{slug}")
    return {"deleted": True}


async def core_restart(client, params):
    await client.post_json("/core/restart")
    return {"status": "restarting"}


async def host_reboot(client, params):
    await client.post_json("/host/reboot")
    return {"status": "rebooting"}


def _update_params(params):
    if not isinstance(params, dict) or params.keys() - {"target", "addon", "version"}:
        raise CommandError("invalid_params")
    target, addon, version = params.get("target"), params.get("addon"), params.get("version")
    if not isinstance(target, str) or target not in {"core", "supervisor", "os", "addon"}:
        raise CommandError("invalid_params")
    if target == "addon":
        if not isinstance(addon, str) or not SLUG_RE.fullmatch(addon):
            raise CommandError("invalid_params")
    elif addon is not None:
        raise CommandError("invalid_params")
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", version)):
        raise CommandError("invalid_params")
    return {key: value for key, value in (("target", target), ("addon", addon), ("version", version)) if value is not None}


async def update(client, params):
    target, addon = params["target"], params.get("addon")
    if target == "addon":
        try:
            self_info = await client.get_json("/addons/self/info")
            self_slug = self_info.get("slug") or self_info.get("slug_name")
        except Exception:
            self_slug = "local_darak_agent"
        if not isinstance(self_slug, str):
            self_slug = "local_darak_agent"
        if addon in {"local_darak_agent", self_slug}:
            raise CommandError("conflict")
    jobs = getattr(client, "_update_jobs", None)
    if jobs is None:
        jobs = client._update_jobs = {}
    if any(not task.done() for task in jobs.values()):
        raise CommandError("busy")
    update_id = str(uuid.uuid4())
    path = {"core": "/core/update", "supervisor": "/supervisor/update", "os": "/os/update"}.get(target)
    body = None
    if target == "core":
        body = {"backup": False}
        if "version" in params:
            body["version"] = params["version"]
    if target == "addon":
        path, body = f"/addons/{addon}/update", {"backup": False}
    async def run():
        try:
            await client.post_json(path, body)
            return None
        except Exception:
            return "update failed"
    jobs[update_id] = asyncio.create_task(run())
    return {"state": "started", "update_id": update_id}


def _update_status_params(params):
    if not isinstance(params, dict) or set(params) != {"update_id"}:
        raise CommandError("invalid_params")
    try:
        if str(uuid.UUID(params["update_id"])) != params["update_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CommandError("invalid_params") from None
    return params


async def update_status(client, params):
    entry = getattr(client, "_update_jobs", {}).get(params["update_id"])
    if entry is None:
        return {"state": "unknown"}
    task = entry
    if not task.done():
        return {"state": "running"}
    error = task.result()
    return {"state": "failed", "error": error} if error else {"state": "done"}


def _restore_params(params):
    if (not isinstance(params, dict) or set(params) != {"slug", "mode"}
            or not isinstance(params["slug"], str) or not re.fullmatch(r"[0-9a-f]{8}", params["slug"])
            or not isinstance(params["mode"], str) or params["mode"] not in {"full", "config"}):
        raise CommandError("invalid_params")
    return params


async def _own_slug(client):
    try:
        own = (await client.get_json("/addons/self/info")).get("slug")
    except Exception:
        own = None
    return own if isinstance(own, str) and own else "local_darak_agent"


async def backup_restore(client, params):
    data = await client.get_json(f"/backups/{params['slug']}/info")
    body = {"homeassistant": True, "background": True}
    if params["mode"] == "full":
        folders = data.get("folders")
        addons = data.get("addons")
        if not isinstance(folders, list) or not isinstance(addons, list):
            raise UpstreamError()
        own = await _own_slug(client)
        body["folders"] = [folder for folder in folders if not (
            isinstance(folder, str) and (folder == "addons/local" or folder.startswith("addons/local/")))]
        excluded = {"darak_agent", "local_darak_agent", own}
        body["addons"] = [item for item in addons if not (
            isinstance(item, str) and item in excluded
            or isinstance(item, dict) and item.get("slug") in excluded)]
    result = await client.post_json(f"/backups/{params['slug']}/restore/partial", body)
    try:
        return {"job_id": str(uuid.UUID(result["job_id"]))}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise UpstreamError() from None


def _replace_config(path, content, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".configuration.yaml.")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def alexa_configure(client, params):
    async with CONFIG_LOCK:
        return await _alexa_configure(client, params)


async def _alexa_configure(client, params):
    path = Path(client.config_dir) / "configuration.yaml"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
        original = path.read_text()
    except (OSError, ValueError, UnicodeError):
        raise CommandError("conflict") from None
    lines = original.splitlines()
    indexes = [index for index, line in enumerate(lines) if re.match(r"^alexa:\s*(?:#.*)?$", line)]
    if len(indexes) > 1:
        raise CommandError("conflict")
    if indexes:
        start = indexes[0]
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*", lines[index])), len(lines))
        if any(re.match(r"^\s+smart_home:", line) for line in lines[start + 1:end]):
            return {"status": "unchanged"}
        lines.insert(start + 1, "  smart_home:")
    else:
        if any(re.match(r"^alexa:\s*\S", line) for line in lines):
            raise CommandError("conflict")
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(["alexa:", "  smart_home:"])
    backup_dir = Path(client.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"configuration.yaml.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copyfile(path, backup)
    backup.chmod(0o600)
    mode = stat.S_IMODE(info.st_mode)
    _replace_config(path, "\n".join(lines) + "\n", mode)
    try:
        await client.post_json("/core/check")
    except Exception:
        _replace_config(path, original, mode)
        raise CommandError("conflict") from None
    return {"status": "changed"}


def _config_file(client, value):
    if not isinstance(value, str) or value.startswith("/") or "\\" in value:
        raise CommandError("invalid_params")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CommandError("invalid_params")
    if (len(parts) == 1 and parts[0] in CONFIG_FILES - {"secrets.yaml"}
            or len(parts) == 2 and parts[0] == "packages"
            and re.fullmatch(r"[a-z0-9_-]{1,64}\.yaml", parts[1])):
        pass
    else:
        raise CommandError("invalid_params")
    root = Path(client.config_dir)
    current = root
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError
        if root.is_symlink():
            raise ValueError
        if current.exists() and not stat.S_ISREG(current.lstat().st_mode):
            raise ValueError
    except OSError:
        raise CommandError("conflict") from None
    return current


def _config_params(params, fields):
    if not isinstance(params, dict) or set(params) != fields or "path" not in params:
        raise CommandError("invalid_params")
    value = params["path"]
    if not isinstance(value, str) or value.startswith("/") or "\\" in value:
        raise CommandError("invalid_params")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or not (
            len(parts) == 1 and parts[0] in CONFIG_FILES - {"secrets.yaml"}
            or len(parts) == 2 and parts[0] == "packages"
            and re.fullmatch(r"[a-z0-9_-]{1,64}\.yaml", parts[1])):
        raise CommandError("invalid_params")
    return params


def _config_write_params(params):
    params = _config_params(params, {"path", "content", "base_sha256"})
    if (not isinstance(params["content"], str)
            or params["base_sha256"] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(params["base_sha256"]))):
        raise CommandError("invalid_params")
    return params


def _config_path_params(params):
    return _config_params(params, {"path"})


def _config_read(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        raise CommandError("conflict") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
        raise CommandError("conflict")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, ValueError):
        raise CommandError("conflict") from None


async def config_read(client, params):
    path = _config_file(client, params["path"])
    content, sha = _config_read(path)
    if content is None:
        return {"path": params["path"], "content": None}
    size = len(content.encode("utf-8"))
    return {"path": params["path"], "content": content, "sha256": sha, "size": size}


def _atomic_bytes(path, raw, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".darak-config.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def _config_apply(client, path, raw, old_raw, old_mode):
    previous_sha = hashlib.sha256(old_raw).hexdigest() if old_raw is not None else None
    backup = path.with_name(path.name + ".darak-prev")
    try:
        if old_raw is not None:
            _atomic_bytes(backup, old_raw, 0o600)
        _atomic_bytes(path, raw, old_mode if old_mode is not None else 0o644)
        await client.post_json("/core/check")
    except Exception:
        try:
            if old_raw is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes(path, old_raw, old_mode)
        except OSError:
            pass
        raise CommandError("conflict") from None
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "previous_sha256": previous_sha, "check": "ok"}


async def config_write(client, params):
    async with CONFIG_LOCK:
        path = _config_file(client, params["path"])
        content = params["content"]
        if not isinstance(content, str):
            raise CommandError("invalid_params")
        try:
            raw = content.encode("utf-8")
        except UnicodeError:
            raise CommandError("invalid_params") from None
        if len(raw) > MAX_CONFIG_BYTES:
            raise CommandError("invalid_params")
        current, current_sha = _config_read(path)
        if current_sha != params["base_sha256"]:
            raise CommandError("conflict")
        old_raw = current.encode() if current is not None else None
        old_mode = stat.S_IMODE(path.lstat().st_mode) if old_raw is not None else None
        if path.parent != Path(client.config_dir):
            if path.parent.is_symlink():
                raise CommandError("conflict")
            if path.parent.exists() and not path.parent.is_dir():
                raise CommandError("conflict")
            if not path.parent.exists():
                path.parent.mkdir(mode=0o755)
        result = await _config_apply(client, path, raw, old_raw, old_mode)
        result["path"] = params["path"]
        return result


async def config_revert(client, params):
    async with CONFIG_LOCK:
        path = _config_file(client, params["path"])
        backup = path.with_name(path.name + ".darak-prev")
        if backup.is_symlink():
            raise CommandError("conflict")
        content, _ = _config_read(backup)
        if content is None:
            raise CommandError("conflict")
        backup_raw = content.encode()
        backup_mode = stat.S_IMODE(backup.lstat().st_mode)
        old_content, _ = _config_read(path)
        old_raw = old_content.encode() if old_content is not None else None
        old_mode = stat.S_IMODE(path.lstat().st_mode) if old_raw is not None else None
        if path.parent != Path(client.config_dir) and not path.parent.exists():
            path.parent.mkdir(mode=0o755, parents=True)
        try:
            result = await _config_apply(client, path, backup_raw, old_raw, old_mode)
        except CommandError:
            _atomic_bytes(backup, backup_raw, backup_mode)
            raise
        return {"path": params["path"], "sha256": result["sha256"]}


def _entity_id(value):
    return isinstance(value, str) and len(value) <= 255 and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value)


def _entity_params(params, fields):
    if not isinstance(params, dict) or set(params) - fields or not fields.issubset(params):
        raise CommandError("invalid_params")
    return params


def _entity_get_params(params):
    if not isinstance(params, dict) or set(params) != {"entity_id"} or not _entity_id(params["entity_id"]):
        raise CommandError("invalid_params")
    return params


async def entity_get(client, params):
    access, refresh = await _login(client)
    try:
        status, raw = await client.ha_http("GET", "/api/states/" + params["entity_id"], token=access)
        if status == 404:
            return {"found": False}
        if status != 200:
            raise UpstreamError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise UpstreamError()
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_ENTITY_BYTES:
            value.pop("attributes", None)
            value["attributes_truncated"] = True
            if len(json.dumps(value, allow_nan=False).encode()) > MAX_ENTITY_BYTES:
                raise UpstreamError()
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise UpstreamError() from None
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass


def _entity_call_params(params):
    if not isinstance(params, dict) or set(params) - {"domain", "service", "entity_id", "data"} or not {"domain", "service", "entity_id"}.issubset(params):
        raise CommandError("invalid_params")
    domain, service, entity_id = (params[k] for k in ("domain", "service", "entity_id"))
    if not isinstance(domain, str) or domain in SECURITY_DOMAINS or domain not in ENTITY_DOMAINS:
        raise CommandError("invalid_params")
    if not isinstance(service, str) or not re.fullmatch(r"[a-z_]{1,64}", service):
        raise CommandError("invalid_params")
    if domain == "homeassistant":
        if service not in ENTITY_SERVICES:
            raise CommandError("invalid_params")
    elif not _entity_id(entity_id) or entity_id.split(".", 1)[0] != domain:
        raise CommandError("invalid_params")
    # Locks and alarms are physical security: never operated remotely, not
    # even through the generic homeassistant.* services.
    if domain == "homeassistant" and (not _entity_id(entity_id)
                                      or entity_id.split(".", 1)[0] in SECURITY_DOMAINS):
        raise CommandError("invalid_params")
    if (_entity_id(entity_id) and entity_id.startswith("cover.")
            and any(part in entity_id for part in ("garage", "gate"))):
        raise CommandError("invalid_params")
    data = params.get("data", {})
    if not isinstance(data, dict) or data.keys() & {"entity_id", "device_id", "area_id", "floor_id"}:
        raise CommandError("invalid_params")
    try:
        if len(json.dumps(data, allow_nan=False).encode()) > 4096:
            raise CommandError("invalid_params")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise CommandError("invalid_params") from None
    return {**params, "data": data}


async def entity_call(client, params):
    access, refresh = await _login(client)
    try:
        results = await client.user_ws(access, [{"type": "call_service", "domain": params["domain"],
            "service": params["service"], "service_data": params["data"],
            "target": {"entity_id": [params["entity_id"]]}}])
        result = {"ok": True}
        if results and results[0] is not None:
            encoded = json.dumps(results[0], allow_nan=False).encode()
            if len(encoded) <= MAX_ENTITY_BYTES:
                result["response"] = results[0]
        return result
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass


VOICE_WS_TYPES = frozenset({
    "config_entries/get", "config/entity_registry/list", "assist_pipeline/pipeline/list",
    "config/entity_registry/get_entries",
    "config/entity_registry/update",
    "config/device_registry/update",
    "config/device_registry/list", "get_states", "config/entity_registry/list_for_display",
    "vacuum/get_segments",
    "darak_voice/cleaning_modes",
    "config/area_registry/list", "homeassistant/expose_entity/list", "homeassistant/expose_entity",
    "config/area_registry/update",
    "assist_pipeline/pipeline/create", "assist_pipeline/pipeline/update",
    "assist_pipeline/pipeline/delete", "assist_pipeline/pipeline/set_preferred",
    "conversation/process", "call_service",
})
VOICE_WS_BARE_TYPES = frozenset({
    "config/device_registry/list", "get_states", "config/entity_registry/list_for_display",
})
VOICE_ENTITY_ID_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\Z")
VOICE_EXPOSE_DENY_DOMAINS = frozenset({"alarm_control_panel", "camera", "siren", "update"})
VOICE_REGISTRY_DENY_DOMAINS = frozenset({"alarm_control_panel", "camera", "siren", "update"})
VOICE_REST_PATH = re.compile(
    r"/api/config/config_entries/(?:flow|flow/[0-9A-Za-z_-]{1,64}|"
    r"options/flow|options/flow/[0-9A-Za-z_-]{1,64}|entry/[0-9A-Za-z_-]{1,64})\Z"
)
HA_REST_DENY_PATHS = (re.compile(r"/api/services/(?:lock|alarm_control_panel)(?:/|\Z)"),)
VOICE_TOKEN_RE = re.compile(r"dvt1\.[A-Za-z0-9_-]{43}\Z")
VACUUM_MODE_NAMES = {
    "suction": frozenset({"quiet", "normal", "strong", "max"}),
    "type": frozenset({"vacuum", "mop", "vacuum_and_mop", "smart", "smart_deep"}),
    "water": frozenset({"low", "medium", "high"}),
}
VACUUM_MODE_ENTITY_ID_RE = re.compile(r"(?:select|vacuum)\.[a-z0-9_]{1,128}\Z")


def _valid_vacuum_modes_override(override):
    if not isinstance(override, dict) or set(override) - {"suction", "type", "water", "smart_mode_off"}:
        return False
    for dimension, names in VACUUM_MODE_NAMES.items():
        recipes = override.get(dimension, {})
        if not isinstance(recipes, dict) or set(recipes) - names:
            return False
        if not all(_valid_vacuum_mode_recipe(recipe) for recipe in recipes.values()):
            return False
    return "smart_mode_off" not in override or _valid_vacuum_mode_recipe(override["smart_mode_off"])


def _valid_vacuum_mode_recipe(recipe):
    return (isinstance(recipe, list) and len(recipe) <= 3
            and all(isinstance(assignment, dict)
                    and set(assignment) == {"entity_id", "option"}
                    and isinstance(assignment["entity_id"], str)
                    and VACUUM_MODE_ENTITY_ID_RE.fullmatch(assignment["entity_id"])
                    and isinstance(assignment["option"], str)
                    and 1 <= len(assignment["option"]) <= 100
                    and not any(ord(char) < 32 or 127 <= ord(char) <= 159
                                for char in assignment["option"])
                    for assignment in recipe))


def _ha_ws_params(params):
    messages = params.get("messages") if isinstance(params, dict) and set(params) == {"messages"} else None
    if not isinstance(messages, list) or not 1 <= len(messages) <= 10:
        raise CommandError("invalid_params")
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get("type"), str)
                or message["type"] not in VOICE_WS_TYPES
                or "id" in message):
            raise CommandError("invalid_params")
        if message["type"] == "homeassistant/expose_entity/list" and set(message) != {"type"}:
            raise CommandError("invalid_params")
        if message["type"] == "config/area_registry/update":
            area_id, aliases = message.get("area_id"), message.get("aliases")
            if (set(message) != {"type", "area_id", "aliases"}
                    or not isinstance(area_id, str) or not re.fullmatch(r"[a-z0-9_]{1,64}\Z", area_id)
                    or not isinstance(aliases, list) or len(aliases) > 20
                    or any(not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > 100
                           or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in alias)
                           for alias in aliases)):
                raise CommandError("invalid_params")
        if message["type"] in VOICE_WS_BARE_TYPES and set(message) != {"type"}:
            raise CommandError("invalid_params")
        if message["type"] == "config/entity_registry/get_entries":
            entity_ids = message.get("entity_ids")
            if (set(message) != {"type", "entity_ids"}
                    or not isinstance(entity_ids, list) or not 1 <= len(entity_ids) <= 100
                    or any(not isinstance(entity_id, str) or not VOICE_ENTITY_ID_RE.fullmatch(entity_id)
                           for entity_id in entity_ids)):
                raise CommandError("invalid_params")
        if message["type"] == "vacuum/get_segments":
            entity_id = message.get("entity_id")
            if (set(message) != {"type", "entity_id"}
                    or not isinstance(entity_id, str)
                    or not re.fullmatch(r"vacuum\.[a-z0-9_]+", entity_id)):
                raise CommandError("invalid_params")
        if message["type"] == "darak_voice/cleaning_modes":
            entity_id = message.get("entity_id")
            if (set(message) != {"type", "entity_id"}
                    or not isinstance(entity_id, str)
                    or not re.fullmatch(r"vacuum\.[a-z0-9_]{1,128}", entity_id)):
                raise CommandError("invalid_params")
        if message["type"] == "homeassistant/expose_entity":
            entity_ids = message.get("entity_ids")
            if (set(message) != {"type", "assistants", "entity_ids", "should_expose"}
                    or message.get("assistants") != ["conversation"]
                    or not isinstance(entity_ids, list) or not 1 <= len(entity_ids) <= 200
                    or any(not isinstance(entity_id, str) or not VOICE_ENTITY_ID_RE.fullmatch(entity_id)
                           or entity_id.split(".", 1)[0] in VOICE_EXPOSE_DENY_DOMAINS
                           or entity_id.split(".", 1)[0] not in {"cover", "group", "lock", "valve"}
                           and any(part in entity_id.lower() for part in ("garage", "gate"))
                           for entity_id in entity_ids)
                    or type(message.get("should_expose")) is not bool):
                raise CommandError("invalid_params")
        if message["type"] == "config/entity_registry/update":
            entity_id = message.get("entity_id")
            aliases = message.get("aliases")
            area_id = message.get("area_id")
            options = message.get("options")
            common_invalid = (not isinstance(entity_id, str) or not VOICE_ENTITY_ID_RE.fullmatch(entity_id)
                    or entity_id.split(".", 1)[0] in VOICE_REGISTRY_DENY_DOMAINS
                    or entity_id.split(".", 1)[0] not in {"cover", "group", "lock", "valve"}
                    and any(part in entity_id.lower() for part in ("garage", "gate")))
            alias_update = (set(message) == {"type", "entity_id", "aliases", "area_id"}
                    and not common_invalid
                    and isinstance(aliases, list) and 1 <= len(aliases) <= 20
                    and not any(not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 100
                                or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in alias)
                                for alias in aliases)
                    and (area_id is None or isinstance(area_id, str)
                         and re.fullmatch(r"[a-z0-9_]{1,64}", area_id)))
            options_update = (set(message) == {"type", "entity_id", "options_domain", "options"}
                    and not common_invalid
                    and message.get("options_domain") == "darak_voice"
                    and isinstance(options, dict)
                    and set(options) in ({"kind", "confirmed", "version"},
                                         {"kind", "confirmed", "version", "security_voice"})
                    and isinstance(options.get("kind"), str)
                    and options.get("kind") in {"light", "ac", "heater", "fan", "curtain", "shade", "tv", "appliance", "other",
                                                 "vacuum", "water_heater", "humidifier", "valve", "button", "lock"}
                    and type(options.get("confirmed")) is bool
                    and type(options.get("version")) is int and options.get("version") == 1)
            if options_update and "security_voice" in options:
                options_update = type(options["security_voice"]) is bool
            area_mapping = options.get("area_mapping") if isinstance(options, dict) else None
            last_seen_segments = options.get("last_seen_segments") if isinstance(options, dict) else None
            vacuum_options_update = (
                set(message) == {"type", "entity_id", "options_domain", "options"}
                and isinstance(entity_id, str) and re.fullmatch(r"vacuum\.[a-z0-9_]+", entity_id)
                and message.get("options_domain") == "vacuum"
                and isinstance(options, dict) and set(options) == {"area_mapping", "last_seen_segments"}
                and isinstance(area_mapping, dict) and len(area_mapping) <= 200
                and all(isinstance(area_id, str) and re.fullmatch(r"[a-z0-9_]{1,64}", area_id)
                        and isinstance(segment_ids, list) and 1 <= len(segment_ids) <= 50
                        and all(isinstance(segment_id, str) and 1 <= len(segment_id) <= 64
                                and not any(ord(char) < 32 or 127 <= ord(char) <= 159
                                            for char in segment_id)
                                for segment_id in segment_ids)
                        for area_id, segment_ids in area_mapping.items())
                and isinstance(last_seen_segments, list) and len(last_seen_segments) <= 200
                and all(isinstance(segment, dict) and set(segment) == {"id", "name", "group"}
                        and isinstance(segment["id"], str) and len(segment["id"]) <= 64
                        and isinstance(segment["name"], str) and len(segment["name"]) <= 100
                        and (segment["group"] is None or isinstance(segment["group"], str)
                             and len(segment["group"]) <= 100)
                        for segment in last_seen_segments)
            )
            vacuum_modes_update = (
                set(message) == {"type", "entity_id", "options_domain", "options"}
                and isinstance(entity_id, str)
                and re.fullmatch(r"vacuum\.[a-z0-9_]{1,128}", entity_id)
                and message.get("options_domain") == "darak_vacuum_modes"
                and isinstance(options, dict) and set(options) == {"modes"}
                and _valid_vacuum_modes_override(options["modes"])
            )
            if not (alias_update or options_update or vacuum_options_update or vacuum_modes_update):
                raise CommandError("invalid_params")
        if message["type"] == "config/device_registry/update":
            device_id = message.get("device_id")
            area_id = message.get("area_id")
            if (set(message) != {"type", "device_id", "area_id"}
                    or not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id)
                    or area_id is not None and (not isinstance(area_id, str)
                        or not re.fullmatch(r"[a-z0-9_]{1,64}", area_id))):
                raise CommandError("invalid_params")
        if message["type"] == "conversation/process":
            allowed = {"type", "text", "language", "agent_id", "conversation_id"}
            text_value, language, agent_id = message.get("text"), message.get("language"), message.get("agent_id")
            conversation_id = message.get("conversation_id")
            if (set(message) - allowed or not {"type", "text", "language", "agent_id"} <= set(message)
                    or not isinstance(text_value, str) or not 1 <= len(text_value) <= 300
                    or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in text_value)
                    or language not in {"ar", "en"}
                    or not isinstance(agent_id, str) or not re.fullmatch(r"conversation\.[a-z0-9_]{1,64}", agent_id)
                    or conversation_id is not None and (not isinstance(conversation_id, str)
                        or not 1 <= len(conversation_id) <= 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id))):
                raise CommandError("invalid_params")
        if message["type"] == "call_service":
            service_data = message.get("service_data")
            if (set(message) != {"type", "domain", "service", "service_data", "return_response"}
                    or message.get("domain") != "darak_voice" or message.get("service") != "try_phrase"
                    or message.get("return_response") is not True or not isinstance(service_data, dict)
                    or set(service_data) - {"text", "audio", "language"}
                    or ("text" in service_data) == ("audio" in service_data)
                    or "language" in service_data and (not isinstance(service_data["language"], str)
                                                        or service_data["language"] not in {"ar", "en"})):
                raise CommandError("invalid_params")
            if "text" in service_data:
                text_value = service_data["text"]
                if not isinstance(text_value, str) or not 1 <= len(text_value.strip()) <= 512:
                    raise CommandError("invalid_params")
            else:
                audio = service_data["audio"]
                if not isinstance(audio, str) or len(audio) > 350000:
                    raise CommandError("invalid_params")
                try:
                    pcm = base64.b64decode(audio, validate=True)
                except (ValueError, TypeError):
                    raise CommandError("invalid_params") from None
                if not 9600 <= len(pcm) <= 256000 or len(pcm) % 2:
                    raise CommandError("invalid_params")
        try:
            max_message_bytes = 400000 if message["type"] == "call_service" else 65536
            if len(json.dumps(message, allow_nan=False).encode()) > max_message_bytes:
                raise CommandError("invalid_params")
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise CommandError("invalid_params") from None
    return params


async def ha_ws(client, params):
    exposure_ids = sorted({entity_id for message in params["messages"]
                           if message.get("type") == "homeassistant/expose_entity"
                           and message.get("should_expose") is True
                           for entity_id in message.get("entity_ids", [])
                           if isinstance(entity_id, str)
                           and entity_id.split(".", 1)[0] in {"lock", "valve", "cover", "group"}})
    if exposure_ids:
        entry_chunks = [exposure_ids[offset:offset + 100] for offset in range(0, len(exposure_ids), 100)]
        if len(entry_chunks) > 9:
            raise CommandError("invalid_params")
        try:
            metadata = await client.admin_ws([
                *({"type": "config/entity_registry/get_entries", "entity_ids": chunk} for chunk in entry_chunks),
                {"type": "get_states"},
            ])
        except HaCommandError:
            raise CommandError("invalid_params") from None
        if not isinstance(metadata, list) or len(metadata) != len(entry_chunks) + 1:
            raise CommandError("invalid_params")
        entry_results, states = metadata[:-1], metadata[-1]
        if any(not isinstance(result, dict) for result in entry_results) or not isinstance(states, list):
            raise CommandError("invalid_params")
        entries = {entity_id: entry for result in entry_results for entity_id, entry in result.items()}
        states_by_id = {state.get("entity_id"): state for state in states
                        if isinstance(state, dict) and isinstance(state.get("entity_id"), str)}

        def member_is_security(member_id, depth, seen):
            if not isinstance(member_id, str) or "." not in member_id or member_id in seen:
                return False
            if depth > 3 or len(seen) >= 200:
                return True
            seen.add(member_id)
            member_state = states_by_id.get(member_id, {})
            member_attributes = member_state.get("attributes") if isinstance(member_state, dict) else {}
            member_attributes = member_attributes if isinstance(member_attributes, dict) else {}
            member_class = member_attributes.get("device_class")
            member_words = set(re.findall(r"[a-z0-9]+|[\u0600-\u06ff]+", " ".join((
                member_id, member_attributes.get("friendly_name", "")
            )).casefold()))
            member_domain = member_id.split(".", 1)[0]
            security_member = (member_domain == "lock" or member_domain == "valve" and member_class != "water"
                               or member_domain == "cover" and (member_class in {"garage", "gate", "door"}
                                   or member_words & {"garage", "gate", "door", "كراج", "الكراج", "جراج",
                                                      "الجراج", "بوابة", "بوابه", "البوابة", "البوابه",
                                                      "باب", "الباب"}))
            if security_member:
                return True
            nested = member_attributes.get("entity_id")
            if isinstance(nested, list):
                return any(member_is_security(child, depth + 1, seen) for child in nested)
            return False

        for message in params["messages"]:
            if message.get("type") != "homeassistant/expose_entity" or message.get("should_expose") is not True:
                continue
            for entity_id in message["entity_ids"]:
                if entity_id not in exposure_ids:
                    continue
                entry = entries.get(entity_id, {})
                state = states_by_id.get(entity_id, {})
                attributes = state.get("attributes") if isinstance(state, dict) else {}
                attributes = attributes if isinstance(attributes, dict) else {}
                device_class = (entry.get("device_class") or entry.get("original_device_class")
                                or attributes.get("device_class")) if isinstance(entry, dict) else attributes.get("device_class")
                name = " ".join(str(value) for value in (
                    entity_id, entry.get("name"), entry.get("original_name"),
                    attributes.get("friendly_name")) if isinstance(value, str)).casefold() \
                    if isinstance(entry, dict) else entity_id.casefold()
                words = set(re.findall(r"[a-z0-9]+|[\u0600-\u06ff]+", name))
                domain = entity_id.split(".", 1)[0]
                security = (domain == "lock" or domain == "valve" and device_class != "water"
                            or domain == "cover" and (device_class in {"garage", "gate", "door"}
                                                       or words & {"garage", "gate", "door", "كراج", "الكراج",
                                                                   "جراج", "الجراج", "بوابة", "بوابه", "البوابة",
                                                                   "البوابه", "باب", "الباب"}))
                member_ids = attributes.get("entity_id")
                if isinstance(member_ids, list):
                    seen = {entity_id}
                    security = security or any(member_is_security(member_id, 1, seen) for member_id in member_ids)
                # Keep the old garage/gate name protection for non-security entities.
                legacy_protected = any(part in entity_id.casefold() for part in ("garage", "gate"))
                if security:
                    options = entry.get("options") if isinstance(entry, dict) else None
                    voice = options.get("darak_voice") if isinstance(options, dict) else None
                    if not isinstance(voice, dict) or voice.get("security_voice") is not True:
                        raise CommandError("invalid_params")
                elif legacy_protected:
                    raise CommandError("invalid_params")
    try:
        results = await client.admin_ws(params["messages"])
    except HaCommandError as exc:
        raise CommandError("ha_error", f"{exc.code}: {exc.message}") from None
    return {"results": results}


def _ha_rest_params(params):
    if not isinstance(params, dict) or set(params) - {"method", "path", "body"}:
        raise CommandError("invalid_params")
    method, path = params.get("method"), params.get("path")
    if (not isinstance(path, str) or any(pattern.fullmatch(path) for pattern in HA_REST_DENY_PATHS)
            or not VOICE_REST_PATH.fullmatch(path)
            or (method == "DELETE") != ("/entry/" in path)
            or not isinstance(method, str) or method not in {"POST", "DELETE"}):
        raise CommandError("invalid_params")
    if "body" in params:
        if not isinstance(params["body"], dict):
            raise CommandError("invalid_params")
        try:
            if len(json.dumps(params["body"], allow_nan=False).encode()) > 16384:
                raise CommandError("invalid_params")
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise CommandError("invalid_params") from None
    if path in {"/api/config/config_entries/flow", "/api/config/config_entries/options/flow"}:
        if method != "POST" or not isinstance(params.get("body"), dict):
            raise CommandError("invalid_params")
        handler = params["body"].get("handler")
        if path == "/api/config/config_entries/flow":
            if not isinstance(handler, str) or handler not in {"darak_voice", "group"}:
                raise CommandError("invalid_params")
        elif not isinstance(handler, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", handler):
            raise CommandError("invalid_params")
    return params


def _managed_username(value):
    return isinstance(value, str) and re.fullmatch(r"darak_[0-9a-f]{32}", value)


def _managed_create_params(params):
    if (not isinstance(params, dict) or set(params) != {"username", "name", "group"}
            or not _managed_username(params["username"])
            or not isinstance(params["name"], str) or not 1 <= len(params["name"]) <= 128
            or params["group"] not in {"system-admin", "system-users"}):
        raise CommandError("invalid_params")
    return params


def _managed_update_params(params):
    if (not isinstance(params, dict) or params.keys() - {"user_id", "name", "group", "is_active"}
            or "user_id" not in params
            or not isinstance(params["user_id"], str) or not 1 <= len(params["user_id"]) <= 128
            or not (set(params) & {"name", "group", "is_active"})
            or ("name" in params and (not isinstance(params["name"], str) or not 1 <= len(params["name"]) <= 128))
            or ("group" in params and params["group"] not in {"system-admin", "system-users"})
            or ("is_active" in params and type(params["is_active"]) is not bool)):
        raise CommandError("invalid_params")
    return params


def _managed_delete_params(params):
    if (not isinstance(params, dict) or set(params) != {"user_id"}
            or not isinstance(params["user_id"], str) or not 1 <= len(params["user_id"]) <= 128):
        raise CommandError("invalid_params")
    return params


def _managed_sso_params(params):
    if (not isinstance(params, dict) or set(params) != {"user_id", "username", "client_id", "redirect_uri"}
            or not isinstance(params["user_id"], str) or not 1 <= len(params["user_id"]) <= 128
            or not _managed_username(params["username"])
            or not isinstance(params["client_id"], str) or not isinstance(params["redirect_uri"], str)):
        raise CommandError("invalid_params")
    return params


async def _require_entry_domain(client, entry_id, allowed_domains):
    try:
        metadata = await client.admin_ws([{"type": "config_entries/get"}])
    except HaCommandError:
        raise CommandError("invalid_params") from None
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], list):
        raise CommandError("invalid_params")
    entries = metadata[0]
    if not isinstance(entries, list) or not any(
            isinstance(entry, dict) and entry.get("entry_id") == entry_id
            and isinstance(entry.get("domain"), str) and entry.get("domain") in allowed_domains
            for entry in entries):
        raise CommandError("invalid_params")


FLOW_PATH = re.compile(r"(/api/config/config_entries/(?:options/)?flow)(?:/([^/]+))?")
MAX_OPEN_FLOWS = 64


def _open_flows(client):
    if getattr(client, "open_flows", None) is None:
        client.open_flows = {}
    return client.open_flows


async def ha_rest(client, params):
    path, body = params["path"], params.get("body")
    if (path == "/api/config/config_entries/options/flow"
            or path.startswith("/api/config/config_entries/entry/")):
        entry_id = body.get("handler") if path.endswith("/options/flow") and isinstance(body, dict) else path.rsplit("/", 1)[-1]
        await _require_entry_domain(client, entry_id, {"group"} if path.endswith("/options/flow") else {"darak_voice", "group"})
    # Reading a flow in HA re-runs its step, so only flows this agent opened itself may be continued.
    flow, open_flows = FLOW_PATH.fullmatch(path), _open_flows(client)
    if flow and flow[2] and (flow[1], flow[2]) not in open_flows:
        raise CommandError("invalid_params")
    access, refresh = await _login(client)
    try:
        status, raw = await client.ha_http(params["method"], params["path"],
                                           json=params.get("body"), token=access)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise UpstreamError()
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, UnicodeError, RecursionError):
            body = None
        if flow and isinstance(body, dict) and isinstance(body.get("flow_id"), str):
            key = (flow[1], flow[2] or body["flow_id"])
            if status == 200 and body.get("type") not in {"create_entry", "abort"}:
                open_flows[key] = True
                while len(open_flows) > MAX_OPEN_FLOWS:
                    del open_flows[next(iter(open_flows))]
            else:
                open_flows.pop(key, None)
        return {"status": status, "body": body}
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass


def _integration_tree(path):
    if path.is_symlink() or not path.is_dir():
        raise CommandError("conflict")
    files = {}
    for item in path.rglob("*"):
        # Home Assistant writes bytecode caches once it loads the integration.
        if "__pycache__" in item.relative_to(path).parts:
            continue
        if item.is_symlink():
            raise CommandError("conflict")
        if item.is_file():
            files[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
        elif item.is_dir():
            files[str(item.relative_to(path)) + "/"] = ""
        else:
            raise CommandError("conflict")
    return files


def _integration_paths(client, domain):
    parent = client.config_dir / "custom_components"
    if client.config_dir.is_symlink() or parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise CommandError("conflict")
    return parent, parent / domain


def _integration_backup(client, destination, domain):
    backup_dir = client.backup_dir
    if backup_dir.is_symlink():
        raise CommandError("conflict")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (domain + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    shutil.move(str(destination), str(backup))
    return backup


async def _integration_install(client, domain):
    source = client.integrations_dir / domain
    if client.integrations_dir.is_symlink():
        raise CommandError("conflict")
    source_files = _integration_tree(source)
    parent, destination = _integration_paths(client, domain)
    if destination.exists() or destination.is_symlink():
        if _integration_tree(destination) == source_files:
            return {"status": "unchanged"}
    created_parent = not parent.exists()
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if created_parent:
        parent.chmod(0o755)
    pending = parent / ("." + domain + ".new")
    if pending.exists() or pending.is_symlink():
        raise CommandError("conflict")
    shutil.copytree(source, pending)
    try:
        if _integration_tree(pending) != source_files:
            raise CommandError("conflict")
        backup = None
        if destination.exists():
            backup = _integration_backup(client, destination, domain)
        try:
            pending.rename(destination)
        except OSError:
            if backup is not None:
                shutil.move(str(backup), str(destination))
            raise
    finally:
        if pending.exists():
            shutil.rmtree(pending)
    return {"status": "changed"}


async def _integration_uninstall(client, domain):
    _parent, destination = _integration_paths(client, domain)
    if not destination.exists() and not destination.is_symlink():
        return {"removed": False}
    _integration_tree(destination)
    try:
        manifest = json.loads((destination / "manifest.json").read_text())
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CommandError("conflict") from None
    if not isinstance(manifest, dict) or manifest.get("domain") != domain:
        raise CommandError("conflict")
    _integration_backup(client, destination, domain)
    return {"removed": True}


async def voice_install(client, params):
    return await _integration_install(client, "darak_voice")


async def voice_uninstall(client, params):
    return await _integration_uninstall(client, "darak_voice")


OAUTH_FIELDS = {
    "google_client_id": re.compile(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com"),
    "redirect_uri": re.compile(r"https://[a-z0-9.-]+/oauth/google-calendar/callback"),
    "token_url": re.compile(r"https://[a-z0-9.-]+/api/google-oauth/token"),
    "darak_key": re.compile(r"dgo1\.[A-Za-z0-9_-]{43}"),
}
OAUTH_ENTRIES = "/api/config/config_entries/entry"
OAUTH_FLOW = "/api/config/config_entries/flow"
OAUTH_LOAD_POLLS = 10
OAUTH_LOAD_INTERVAL = 0.5
MAX_ENTRIES_STORE_BYTES = 8 * 1024 * 1024


def _oauth_configure_params(params):
    if (not isinstance(params, dict) or set(params) != set(OAUTH_FIELDS)
            or any(not isinstance(params[name], str) or not pattern.fullmatch(params[name])
                   for name, pattern in OAUTH_FIELDS.items())):
        raise CommandError("invalid_params")
    return params


async def oauth_install(client, params):
    return await _integration_install(client, "darak_oauth")


async def _oauth_rest(client, token, method, path, body=None):
    status, raw = await client.ha_http(method, path, json=body, token=token)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise UpstreamError()
    try:
        parsed = json.loads(raw) if raw else None
    except (ValueError, UnicodeError, RecursionError):
        parsed = None
    return status, parsed


async def _oauth_entries(client, token, domain):
    status, entries = await _oauth_rest(client, token, "GET", f"{OAUTH_ENTRIES}?domain={domain}")
    if status != 200 or not isinstance(entries, list):
        raise UpstreamError()
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("domain") == domain
            and isinstance(entry.get("entry_id"), str) and re.fullmatch(r"[0-9A-Za-z_-]{1,64}", entry["entry_id"])]


async def _oauth_delete(client, token, entry_ids):
    for entry_id in entry_ids:
        status, _body = await _oauth_rest(client, token, "DELETE", f"{OAUTH_ENTRIES}/{entry_id}")
        if status != 200:
            raise UpstreamError()


async def _oauth_session(client, work):
    access, refresh = await _login(client)
    try:
        return await work(access)
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass


async def oauth_configure(client, params):
    async def work(token):
        await _oauth_delete(client, token, [e["entry_id"] for e in await _oauth_entries(client, token, "darak_oauth")])
        status, flow = await _oauth_rest(client, token, "POST", OAUTH_FLOW,
                                         {"handler": "darak_oauth", "show_advanced_options": False})
        if (status != 200 or not isinstance(flow, dict) or flow.get("type") != "form"
                or not isinstance(flow.get("flow_id"), str)
                or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", flow["flow_id"])):
            raise UpstreamError()
        status, result = await _oauth_rest(client, token, "POST", f"{OAUTH_FLOW}/{flow['flow_id']}",
                                           {name: params[name] for name in OAUTH_FIELDS})
        if status != 200 or not isinstance(result, dict) or result.get("type") != "create_entry":
            raise CommandError("conflict")
        state = None
        for attempt in range(OAUTH_LOAD_POLLS):
            entries = await _oauth_entries(client, token, "darak_oauth")
            state = entries[0].get("state") if entries else None
            if state == "loaded":
                break
            if attempt + 1 < OAUTH_LOAD_POLLS:
                await asyncio.sleep(OAUTH_LOAD_INTERVAL)
        return {"status": "configured", "state": state if isinstance(state, str) and re.fullmatch(r"[a-z_]{1,32}", state) else "unknown"}
    return await _oauth_session(client, work)


def _oauth_google_entry_ids(client):
    path = client.config_dir / ".storage" / "core.config_entries"
    try:
        if client.config_dir.is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise CommandError("conflict")
        if path.stat().st_size > MAX_ENTRIES_STORE_BYTES:
            raise CommandError("conflict")
        entries = json.loads(path.read_text())["data"]["entries"]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, RecursionError):
        raise CommandError("conflict") from None
    if not isinstance(entries, list):
        raise CommandError("conflict")
    return [entry["entry_id"] for entry in entries
            if isinstance(entry, dict) and entry.get("domain") == "google"
            and isinstance(entry.get("data"), dict)
            and entry["data"].get("auth_implementation") == "darak_oauth"
            and isinstance(entry.get("entry_id"), str)]


async def oauth_remove(client, params):
    google_ids = _oauth_google_entry_ids(client)
    async def work(token):
        # Only entries the REST list also shows are deleted, so a stale store id cannot name another entry.
        listed = {e["entry_id"] for e in await _oauth_entries(client, token, "google")}
        ids = [i for i in google_ids if i in listed]
        ids += [e["entry_id"] for e in await _oauth_entries(client, token, "darak_oauth")]
        await _oauth_delete(client, token, ids)
        return bool(ids)
    deleted = await _oauth_session(client, work)
    removed = (await _integration_uninstall(client, "darak_oauth"))["removed"]
    return {"status": "removed" if deleted or removed else "absent"}


def _voice_check_params(params):
    if (not isinstance(params, dict) or set(params) != {"token"}
            or not isinstance(params["token"], str) or not VOICE_TOKEN_RE.fullmatch(params["token"])):
        raise CommandError("invalid_params")
    return params


async def voice_check(client, params):
    async def default_get(url, headers, timeout):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                return response.status, raw
    async with asyncio.timeout(10):
        status, raw = await (client.voice_http or default_get)(
            f"http://127.0.0.1:{client.voice_forward_port}/v1/whoami",
            {"Authorization": "Bearer " + params["token"]}, 10)
    if type(status) is not int or not 100 <= status <= 599 or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise UpstreamError()
    try:
        body = json.loads(raw)
        body = body if isinstance(body, dict) else {}
    except (ValueError, UnicodeError, RecursionError):
        body = {}
    tenant = body.get("tenant")
    result = {"status": status, "tenant": tenant if isinstance(tenant, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,31}", tenant) else None}

    def booleans(value, names):
        return {name: value[name] for name in names if isinstance(value, dict) and type(value.get(name)) is bool}

    providers = booleans(body.get("stt_providers"), ("cohere", "azure", "deepgram", "google"))
    fallback = body.get("ai_fallback")
    clean_fallback = {}
    if isinstance(fallback, dict):
        if isinstance(fallback.get("selected"), str) and fallback["selected"] in {"off", "jev", "qwen"}:
            clean_fallback["selected"] = fallback["selected"]
        available = booleans(fallback.get("available"), ("jev", "qwen"))
        if available:
            clean_fallback["available"] = available
    result["stt_providers"] = providers
    result["ai_fallback"] = clean_fallback
    result["llm_fallback"] = booleans(body.get("llm_fallback"), ("enabled", "available"))
    general = body.get("general_questions")
    clean_general = booleans(general, ("enabled",))
    available = booleans(general.get("available") if isinstance(general, dict) else None, ("llm", "search"))
    if available:
        clean_general["available"] = available
    result["general_questions"] = clean_general
    result["followups"] = booleans(body.get("followups"), ("enabled", "available"))
    result["text_to_speech"] = booleans(body.get("text_to_speech"), ("enabled", "available"))
    result["capture"] = body.get("capture") if type(body.get("capture")) is bool else None
    result["audio_frontend"] = {
        "mode": body.get("audio_frontend", {}).get("mode")
        if isinstance(body.get("audio_frontend"), dict)
        and body["audio_frontend"].get("mode") in {"off", "observe", "on"} else None,
        **booleans(body.get("audio_frontend"), ("loaded", "denoise")),
    }
    result["voice_profiles"] = booleans(body.get("voice_profiles"), ("enabled", "available"))
    def integration_version(path):
        try:
            manifest = json.loads(path.read_text())
            version = manifest.get("version") if isinstance(manifest, dict) else None
            return version if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", version) else None
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None
    _parent, installed = _integration_paths(client, "darak_voice")
    result["integration"] = {
        "installed": integration_version(installed / "manifest.json"),
        "bundled": integration_version(client.integrations_dir / "darak_voice" / "manifest.json"),
    }
    return result


@dataclass(frozen=True)
class Command:
    name: str
    handler: Callable
    validate: Callable = _no_params

    async def run(self, client, params):
        params = self.validate(params)
        try:
            guarded = self.name in BLOCKED_COMMANDS or self.name == "ha_ws" and any(
                message.get("type") == "conversation/process" for message in params["messages"])
            if guarded and await session_mode(client) == "block":
                raise CommandError("customer_blocked")
            result = {"schema": 1, "command": self.name, **await self.handler(client, params)}
            if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
                raise UpstreamError()
            return result
        except (TimeoutError, aiohttp.ClientConnectionError, OSError):
            raise CommandError("upstream_unavailable") from None
        except (UpstreamError, aiohttp.ClientError, ValueError, TypeError, KeyError,
                AttributeError, RecursionError, OverflowError):
            raise CommandError("upstream_error") from None


COMMANDS: dict[str, Command] = {
    handler.__name__: Command(handler.__name__, handler, _logs_params if handler is logs else
                              _purpose_params if handler in {token_issue, token_revoke} else _no_params)
    for handler in (health, versions, logs, integrations, entities_summary, backups,
                    service_user_ensure, token_issue, token_revoke, service_user_remove,
                    http_configure, http_restore, http_status, core_restart, alexa_configure,
                    ha_ws, ha_rest, voice_install, voice_uninstall, voice_check)
}
COMMANDS["http_promote"] = Command("http_promote", http_promote, _http_promote_params)
COMMANDS.update({
    "backup_create": Command("backup_create", backup_create),
    "backup_job": Command("backup_job", backup_job, _backup_job_params),
    "backup_prepare": Command("backup_prepare", backup_prepare, _backup_slug_params),
    "backup_delete": Command("backup_delete", backup_delete, _backup_slug_params),
    "host_reboot": Command("host_reboot", host_reboot),
    "update": Command("update", update, _update_params),
    "update_status": Command("update_status", update_status, _update_status_params),
    "backup_restore": Command("backup_restore", backup_restore, _restore_params),
})
COMMANDS.update({
    "tech_user_enable": Command("tech_user_enable", gated_tech_user_enable, _session_params),
    "tech_user_disable": Command("tech_user_disable", disable_with_consent),
    "ssh_open": Command("ssh_open", gated_ssh_open, _ssh_params),
    "ssh_close": Command("ssh_close", ssh_close),
    "session_start": Command("session_start", session_start, _session_start_params),
    "session_status": Command("session_status", session_status, _session_status_params),
})
COMMANDS["ha_ws"] = Command("ha_ws", ha_ws, _ha_ws_params)
COMMANDS["ha_rest"] = Command("ha_rest", ha_rest, _ha_rest_params)
COMMANDS["voice_check"] = Command("voice_check", voice_check, _voice_check_params)
COMMANDS.update({
    "oauth_install": Command("oauth_install", oauth_install),
    "oauth_configure": Command("oauth_configure", oauth_configure, _oauth_configure_params),
    "oauth_remove": Command("oauth_remove", oauth_remove),
})
COMMANDS.update({
    "config_read": Command("config_read", config_read, _config_path_params),
    "config_write": Command("config_write", config_write, _config_write_params),
    "config_revert": Command("config_revert", config_revert, _config_path_params),
    "entity_get": Command("entity_get", entity_get, _entity_get_params),
    "entity_call": Command("entity_call", entity_call, _entity_call_params),
})
COMMANDS.update({
    "managed_user_list": Command("managed_user_list", managed_user_list),
    "managed_user_create": Command("managed_user_create", managed_user_create, _managed_create_params),
    "managed_user_update": Command("managed_user_update", managed_user_update, _managed_update_params),
    "managed_user_delete": Command("managed_user_delete", managed_user_delete, _managed_delete_params),
    "sso_code": Command("sso_code", managed_user_sso_code, _managed_sso_params),
})
from .alexa_inventory import alexa_inventory
COMMANDS["alexa_inventory"] = Command("alexa_inventory", alexa_inventory)
from .automation_context import (automation_context, automation_delete, automation_upsert,
                                 _automation_delete_params, _automation_upsert_params, _automation_validate_params,
                                 _context_params, automation_validate)
COMMANDS["automation_context"] = Command("automation_context", automation_context, _context_params)
COMMANDS["automation_upsert"] = Command("automation_upsert", automation_upsert, _automation_upsert_params)
COMMANDS["automation_delete"] = Command("automation_delete", automation_delete, _automation_delete_params)
COMMANDS["automation_validate"] = Command("automation_validate", automation_validate, _automation_validate_params)
