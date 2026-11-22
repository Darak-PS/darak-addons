"""Short lived technician access and the narrowly scoped SSH add-on bridge."""

import base64
import json
import re
from datetime import datetime, timedelta, timezone

from .enrollment import write_private_json
from .supervisor import UpstreamError

ADDONS = ("core_ssh", "a0d7b954_ssh")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def _expiry(value, now=None):
    from .commands import CommandError
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now = now or datetime.now(timezone.utc)
        if not value.endswith("Z") or expiry.tzinfo != timezone.utc or not now < expiry <= now + timedelta(hours=4, seconds=60):
            raise ValueError
        return expiry
    except (ValueError, TypeError, AttributeError):
        raise CommandError("invalid_params") from None


def _key(value):
    from .commands import CommandError
    try:
        if (not isinstance(value, str) or len(value) > 200 or not value.isascii()
                or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [ -~]*)?", value)):
            raise ValueError
        encoded = value.split(" ", 2)[1]
        if not base64.b64decode(encoded, validate=True):
            raise ValueError
    except (ValueError, TypeError):
        raise CommandError("invalid_params") from None


async def _addon(client):
    data = await client.get_json("/addons")
    entries = data.get("addons", []) if isinstance(data, dict) else []
    for slug in ADDONS:
        if any(isinstance(item, dict) and item.get("slug") == slug and item.get("state") == "started" for item in entries):
            return slug
    from .commands import CommandError
    raise CommandError("conflict")


async def ssh_open(client, params):
    _key(params.get("public_key"))
    _expiry(params.get("expires_at"))
    slug = await _addon(client)
    info = await client.get_json(f"/addons/{slug}/info")
    options = info.get("options") if isinstance(info, dict) else None
    if not isinstance(options, dict):
        raise UpstreamError()
    saved = None
    try:
        saved = json.loads(client.ssh_path.read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise UpstreamError() from None
    if saved:
        if saved.get("addon") != slug:
            from .commands import CommandError
            raise CommandError("conflict")
        original = saved["original"]
    else:
        original = (options.get("authorized_keys") if slug == "core_ssh"
                    else (options.get("ssh") or {}).get("authorized_keys"))
        if not isinstance(original, list) or any(not isinstance(item, str) for item in original):
            raise UpstreamError()
        saved = {"addon": slug, "original": original}
        write_private_json(client.ssh_path, saved)
    if slug == "core_ssh":
        options["authorized_keys"] = list(original) + [params["public_key"]]
        username = "root"
    else:
        ssh = options.get("ssh")
        if not isinstance(ssh, dict):
            raise UpstreamError()
        ssh["authorized_keys"] = list(original) + [params["public_key"]]
        username = ssh.get("username", "hassio")
    if not isinstance(username, str) or not USER_RE.fullmatch(username):
        raise UpstreamError()
    saved.update(addon=slug, expires_at=params["expires_at"], key=params["public_key"])
    write_private_json(client.ssh_path, saved)
    await client.post_json(f"/addons/{slug}/options", {"options": options})
    await client.post_json(f"/addons/{slug}/restart")
    await client.start_ssh_forwarder(slug.replace("_", "-"))
    return {"addon": slug, "port": 2222, "expires_at": params["expires_at"], "user": username}


async def ssh_close(client, params=None):
    try:
        saved = json.loads(client.ssh_path.read_text())
    except FileNotFoundError:
        return {"closed": False}
    slug = saved["addon"]
    try:
        info = await client.get_json(f"/addons/{slug}/info")
        options = info["options"]
        if slug == "core_ssh":
            options["authorized_keys"] = [key for key in options.get("authorized_keys", []) if key != saved["key"]]
        else:
            options["ssh"]["authorized_keys"] = [key for key in options["ssh"].get("authorized_keys", []) if key != saved["key"]]
        await client.post_json(f"/addons/{slug}/options", {"options": options})
        await client.post_json(f"/addons/{slug}/restart")
        client.ssh_path.unlink(missing_ok=True)
    except Exception:
        try:
            await client.stop_ssh_forwarder()
        except Exception:
            pass
        raise
    await client.stop_ssh_forwarder()
    return {"closed": True}
